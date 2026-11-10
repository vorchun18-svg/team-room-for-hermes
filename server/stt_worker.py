"""Постоянный рабочий процесс диктовки: faster-whisper в отдельном интерпретаторе.

Зачем отдельный процесс: сервер «Комнаты команды» — stdlib, а faster-whisper живёт
в venv Hermes. Процесс держит модель в памяти между запросами, грузит её лениво
при первом распознавании и выгружает после ``IDLE_SECONDS`` простоя, после чего
выходит; сервер поднимет новый при следующей диктовке.

Протокол: по одной JSON-строке в stdin и stdout, один запрос за раз.

    → {"op": "transcribe", "path": "…webm", "language": "ru", "prompt": "…"}
    ← {"ok": true, "text": "…", "duration_s": 7.3, "elapsed_s": 1.6,
       "language": "ru", "model": "large-v3-turbo", "device": "cuda", "reason": null}
    → {"op": "status"}   ← {"ok": true, "loaded": true, "model": …, "device": …}
    → {"op": "shutdown"} ← процесс завершается

Конфиг модели — только чтение из ``%LOCALAPPDATA%\\hermes\\config.yaml``
(``stt.local.*`` и ``stt.language``). PATH дополняется ``…\\hermes\\bin``: там
жёсткие ссылки на cublas64_12.dll/cudnn64_9.dll, без них CUDA падает на первом
же распознавании («cublas64_12.dll is not found»).
"""
from __future__ import annotations

import glob
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:                                     # import server.stt_worker
    from . import proc                  # запуск без окна — только через него
except ImportError:                       # `python server/stt_worker.py` (скрипт)
    import proc                         # type: ignore

IDLE_SECONDS = 600            # 10 минут простоя → выгрузить модель и выйти
PROBE_TIMEOUT = 20            # проверка «есть ли faster_whisper» в кандидате
FFMPEG_TIMEOUT = 120          # запасная конвертация в wav 16 кГц mono

DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_DEVICE = "cuda"
DEFAULT_COMPUTE = "float16"
DEFAULT_LANGUAGE = "ru"

_LOOKUP_LOCK = threading.Lock()
_LOOKUP_DONE = False
_LOOKUP_RESULT: str | None = None

#: faster-whisper импортируется ДО запуска каких-либо потоков: на Windows загрузка
#: скомпилированного numpy/_multiarray_umath (*.pyd) берёт блокировку загрузчика
#: DLL, и если в этот момент другой поток висит в чтении stdin, процесс встаёт
#: навсегда (проверено: 0.4 с против вечного ожидания). Поэтому импорт — на уровне
#: модуля и только когда файл запущен как процесс: сервер (``import stt_worker``)
#: остаётся stdlib и numpy в него не тянется. Модель всё равно грузится лениво.
_WhisperModel = None
IMPORT_ERROR: str | None = None
if __name__ == "__main__":
    try:
        from faster_whisper import WhisperModel as _WhisperModel       # noqa: PLC0415
    except Exception as _exc:                                          # noqa: BLE001
        IMPORT_ERROR = "%s: %s" % (_exc.__class__.__name__, str(_exc)[:300])


# --- окружение -------------------------------------------------------------
def hermes_home() -> Path:
    """Корень Hermes: ``HERMES_HOME``, иначе ``%LOCALAPPDATA%\\hermes``."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "hermes"


def bin_dir() -> Path:
    """``…\\hermes\\bin`` — жёсткие ссылки на cuBLAS/cuDNN для CUDA."""
    return hermes_home() / "bin"


def config_path() -> Path:
    """Файл конфигурации Hermes (только чтение)."""
    env = os.environ.get("TEAMROOM_STT_CONFIG")
    return Path(env) if env else hermes_home() / "config.yaml"


def no_window() -> int:
    """``CREATE_NO_WINDOW`` на Windows, иначе 0 (единый помощник — ``server/proc.py``)."""
    return proc.no_window()


def worker_env(base: dict | None = None) -> dict:
    """Окружение процесса: PATH с ``hermes\\bin`` впереди, UTF-8 для stdin/stdout."""
    env = dict(os.environ if base is None else base)
    extra = str(bin_dir())
    parts = [part for part in str(env.get("PATH") or "").split(os.pathsep) if part]
    if extra and extra not in parts:
        parts.insert(0, extra)
    env["PATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return env


# --- поиск интерпретатора с faster_whisper --------------------------------
def python_candidates(env: dict | None = None) -> list[str]:
    """Кандидаты: явный TEAMROOM_STT_PYTHON, затем текущий интерпретатор и venv'ы Hermes."""
    env = os.environ if env is None else env
    explicit = env.get("TEAMROOM_STT_PYTHON")
    if explicit is not None:
        # явное указание (в том числе пустое — «локального распознавания нет»)
        return [str(explicit).strip()] if str(explicit).strip() else []
    home = env.get("HERMES_HOME")
    root = Path(home) if home else hermes_home()
    local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    roots = [root, Path(local) / "hermes"]
    found: list[str] = []
    for path in [sys.executable]:
        if path and path not in found:
            found.append(path)
    for base in roots:
        pattern = str(base / "installs" / "*" / "environments" / "*" /
                      "venv" / "Scripts" / "python.exe")
        for path in sorted(glob.glob(pattern), reverse=True):
            if path not in found:
                found.append(path)
    return found


def has_faster_whisper(python: str, env: dict | None = None, timeout: int = PROBE_TIMEOUT) -> bool:
    """Проверить импорт ``faster_whisper`` в указанном интерпретаторе."""
    if not python or not Path(python).is_file():
        return False
    try:
        res = proc.run([python, "-c", "import faster_whisper"],
                       env=worker_env(env), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return res.returncode == 0


def find_python(candidates: list[str] | None = None, probe=None, refresh: bool = False) -> str | None:
    """Интерпретатор с faster_whisper (результат поиска кешируется в памяти).

    Порядок: текущий ``sys.executable``, затем ``installs/*/environments/*/venv``.
    Проверка — реальный запуск ``python -c "import faster_whisper"``, иначе
    «нашёлся» интерпретатор без пакета и диктовка падает на первом запросе.
    """
    global _LOOKUP_DONE, _LOOKUP_RESULT
    with _LOOKUP_LOCK:
        if _LOOKUP_DONE and not refresh and candidates is None and probe is None:
            return _LOOKUP_RESULT
    check = probe or has_faster_whisper
    result = None
    for path in (candidates if candidates is not None else python_candidates()):
        if check(path):
            result = str(path)
            break
    if candidates is None and probe is None:
        with _LOOKUP_LOCK:
            _LOOKUP_DONE = True
            _LOOKUP_RESULT = result
    return result


def reset_lookup() -> None:
    """Сбросить кеш поиска (тесты)."""
    global _LOOKUP_DONE, _LOOKUP_RESULT
    with _LOOKUP_LOCK:
        _LOOKUP_DONE = False
        _LOOKUP_RESULT = None


# --- конфигурация модели ---------------------------------------------------
def read_settings(path: Path | None = None) -> dict:
    """``stt.*`` из config.yaml: model/device/compute_type/initial_prompt/vad/language."""
    path = Path(path) if path is not None else config_path()
    data: dict = {}
    try:
        import yaml                                   # noqa: PLC0415 - в воркере есть PyYAML
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:                                  # noqa: BLE001 - конфиг не обязателен
        data = {}
    if not isinstance(data, dict):
        data = {}
    stt = data.get("stt") if isinstance(data.get("stt"), dict) else {}
    local = stt.get("local") if isinstance(stt.get("local"), dict) else {}
    vad = local.get("vad", True)
    if isinstance(vad, dict):
        vad = bool(vad.get("filter", True))
    return {"model": str(local.get("model") or DEFAULT_MODEL),
            "device": str(local.get("device") or DEFAULT_DEVICE),
            "compute_type": str(local.get("compute_type") or DEFAULT_COMPUTE),
            "initial_prompt": str(local.get("initial_prompt") or ""),
            "vad": bool(vad),
            "language": str(stt.get("language") or DEFAULT_LANGUAGE)}


# --- ffmpeg (запасное декодирование) --------------------------------------
def ffmpeg_path() -> str | None:
    """ffmpeg: env, PATH, затем tools/ffmpeg-*/bin Hermes."""
    env = os.environ.get("TEAMROOM_FFMPEG")
    if env and Path(env).is_file():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    pattern = str(hermes_home() / "tools" / "ffmpeg-*" / "bin" / name)
    for candidate in sorted(glob.glob(pattern), reverse=True):
        if Path(candidate).is_file():
            return candidate
    return None


def to_wav16(path: str, target: str | None = None) -> str:
    """Сконвертировать файл в wav 16 кГц mono (ffmpeg). Возвращает путь к wav."""
    exe = ffmpeg_path()
    if not exe:
        raise RuntimeError("ffmpeg не найден — нечем перекодировать аудио")
    out = target or str(Path(path).with_suffix(".16k.wav"))
    res = proc.run([exe, "-y", "-loglevel", "error", "-i", str(path),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out],
                   env=worker_env(), stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   timeout=FFMPEG_TIMEOUT)
    if res.returncode != 0 or not Path(out).is_file():
        tail = (res.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError("ffmpeg не перекодировал аудио: %s" % (tail or res.returncode))
    return out


# --- распознавание ---------------------------------------------------------
def _short(exc: BaseException, limit: int = 300) -> str:
    """Короткий однострочный текст ошибки (без переводов строк из stderr)."""
    text = str(exc) or exc.__class__.__name__
    return " ".join(text.replace("\r", " ").split())[:limit]


def _looks_cuda(exc: BaseException) -> bool:
    low = (str(exc) or "").lower()
    return any(word in low for word in ("cublas", "cudnn", "cuda", "no kernel image",
                                        "libcublas", "gpu"))


def _looks_decode(exc: BaseException) -> bool:
    """Похоже на «файл не декодируется», а не на сбой модели."""
    low = (str(exc) or "").lower()
    return any(word in low for word in ("end of file", "invalid data", "could not open",
                                        "decode", "moov atom", "unsupported", "invalid argument",
                                        "ebml", "no audio", "audio stream", "format"))


class _Engine:
    """Ленивая загрузка модели + один откат CUDA → CPU и один — через ffmpeg."""

    def __init__(self, settings: dict) -> None:
        self.settings = dict(settings)
        self.model = None
        self.model_name = self.settings["model"]
        self.device = self.settings["device"]
        self.compute_type = self.settings["compute_type"]
        self.reason: str | None = None

    # -- модель
    def ensure_model(self):
        if self.model is not None:
            return self.model
        if _WhisperModel is None:
            raise RuntimeError("faster-whisper не импортируется: %s" % (IMPORT_ERROR or "нет пакета"))
        self.model = _WhisperModel(self.model_name, device=self.device,
                                   compute_type=self.compute_type)
        return self.model

    def unload(self) -> None:
        self.model = None

    def info(self) -> dict:
        return {"loaded": self.model is not None, "model": self.model_name,
                "device": self.device, "reason": self.reason}

    # -- один проход
    def _run(self, path: str, language: str | None, prompt: str | None) -> dict:
        started = time.time()
        model = self.ensure_model()
        segments, info = model.transcribe(
            path, language=language or None, initial_prompt=prompt or None,
            vad_filter=bool(self.settings.get("vad", True)),
            condition_on_previous_text=False, beam_size=5, temperature=0.0)
        parts = [getattr(segment, "text", "") or "" for segment in segments]
        text = " ".join(" ".join(parts).split())
        return {"text": text, "duration_s": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
                "language": str(getattr(info, "language", "") or ""),
                "elapsed_s": round(time.time() - started, 2)}

    def transcribe(self, path: str, language: str | None = None, prompt: str | None = None,
                   tmp_dir: str | None = None) -> dict:
        first_error: BaseException | None = None
        try:
            return self._run(path, language, prompt)
        except Exception as exc:                          # noqa: BLE001 - разбираем причину
            first_error = exc
        # 1) CUDA не поднялась → один раз CPU/int8
        if self.device.lower() != "cpu" and _looks_cuda(first_error):
            self.reason = "CUDA не поднялась (%s) — работаю на CPU/int8" % _short(first_error, 160)
            self.device, self.compute_type = "cpu", "int8"
            self.unload()
            try:
                return self._run(path, language, prompt)
            except Exception as exc:                      # noqa: BLE001
                first_error = exc
        # 2) декодирование не удалось → wav 16 кГц mono через ffmpeg
        if _looks_decode(first_error) or not str(path).lower().endswith(".wav"):
            wav = None
            try:
                wav = to_wav16(path, str(Path(tmp_dir or Path(path).parent) /
                                         (Path(path).stem + ".16k.wav")))
                return self._run(wav, language, prompt)
            except Exception as exc:                      # noqa: BLE001
                raise RuntimeError("%s; перекодирование не помогло: %s"
                                   % (_short(first_error, 200), _short(exc, 160))) from exc
            finally:
                if wav:
                    try:
                        Path(wav).unlink()
                    except OSError:
                        pass
        raise RuntimeError(_short(first_error))


# --- протокол --------------------------------------------------------------
def _utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def send(obj: dict) -> None:
    """Одна JSON-строка в stdout + сброс буфера (иначе сервер ждёт вечно)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _pump(requests: "queue.Queue[str | None]") -> None:
    """Читать stdin в потоке: основной цикл ждёт с таймаутом простоя."""
    try:
        for line in sys.stdin:
            requests.put(line)
    except Exception:                                     # noqa: BLE001 - закрытый stdin
        pass
    requests.put(None)


def main(argv=None) -> int:
    _utf8_stdio()
    settings = read_settings()
    engine = _Engine(settings)
    requests: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_pump, args=(requests,), daemon=True).start()
    while True:
        try:
            line = requests.get(timeout=IDLE_SECONDS)
        except queue.Empty:
            break                                        # простой → выгружаем и выходим
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            send({"ok": False, "code": "stt_failed", "message": "Некорректный запрос к распознаванию"})
            continue
        if not isinstance(req, dict):
            send({"ok": False, "code": "stt_failed", "message": "Запрос должен быть объектом"})
            continue
        op = req.get("op") or "transcribe"
        if op == "shutdown":
            break
        if op == "status":
            payload = {"ok": True}
            payload.update(engine.info())
            if IMPORT_ERROR:
                payload["reason"] = "faster-whisper не импортируется: %s" % IMPORT_ERROR
            send(payload)
            continue
        if op == "ping":
            send({"ok": True, "pong": True})
            continue
        if op != "transcribe":
            send({"ok": False, "code": "stt_failed", "message": "Неизвестная операция: %s" % op})
            continue
        path = str(req.get("path") or "")
        try:
            if not path or not Path(path).is_file():
                raise RuntimeError("файл аудио не найден")
            result = engine.transcribe(path, language=req.get("language"),
                                       prompt=req.get("prompt"), tmp_dir=req.get("tmp_dir"))
            payload = {"ok": True}
            payload.update(result)
            payload.update({"model": engine.model_name, "device": engine.device,
                            "reason": engine.reason})
            send(payload)
        except Exception as exc:                          # noqa: BLE001 - наружу текстом
            if _looks_decode(exc):
                send({"ok": False, "code": "audio_empty",
                      "message": "В записи нет аудио: файл не декодируется",
                      "detail": _short(exc, 300)})
            else:
                send({"ok": False, "code": "stt_failed",
                      "message": "Не удалось распознать аудио: %s" % _short(exc, 240)})
    engine.unload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
