"""Диктовка: локальное распознавание речи, словарь терминов и автозамены.

Модуль — единственная точка правды для контракта STT (WAVE7-RULES §3):

    status(cfg)                                  → доступность/модель/загрузка
    transcribe(cfg, audio, content_type, …)      → текст + замены
    get_dictionary(cfg) / put_dictionary(cfg,o)  → словарь data/dictation/dictionary.json
    learn(cfg, raw, final, apply=False)          → предложения замен по правке

Тяжёлого здесь нет: faster-whisper живёт в отдельном процессе
(``server/stt_worker.py``), сервер остаётся stdlib. В журнал пишутся только
длины и тайминги — тексты диктовки не попадают никуда, кроме ответа.

Исключение — **аудио длинной записи** (WAVE13 §2): чтобы обрыв окна или сети не
унёс наговоренное, части записи кладутся на диск (``data/stt-recordings/<rid>/``)
и живут там до 72 часов (не больше 30 записей), после чего убираются сами.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from difflib import SequenceMatcher
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from . import proc, stt_cloud, stt_worker
except ImportError:  # pragma: no cover
    import proc  # type: ignore
    import stt_cloud  # type: ignore
    import stt_worker  # type: ignore

MAX_AUDIO = 64 * 1024 * 1024        # лимит тела POST /api/stt/transcribe
MAX_PART_BYTES = 64 * 1024 * 1024   # лимит тела одной части длинной записи
MAX_RECORDING_BYTES = 256 * 1024 * 1024   # суммарный размер одной записи
MAX_PARTS = 64                      # частей в одной записи (2 мин × 64 = 2 ч)
MIN_RECORD_SECONDS = 600            # меньше 10 минут лимит записи не опускаем
DEFAULT_RECORD_SECONDS = 600        # запасной лимит, если в Hermes значения нет
DEFAULT_PART_SECONDS = 120          # длина части длинной записи (2 минуты)
RECORDING_TTL_HOURS = 72            # сколько храним аудио незавершённых записей
MAX_RECORDINGS = 30                 # сколько записей держим одновременно
MAX_TERMS = 300                     # терминов в словаре
MAX_TERM_LEN = 60                   # символов в термине
MAX_REPLACEMENTS = 200              # записей замен
MAX_PAIR_LEN = 200                 # символов в from/to замены
PROMPT_LIMIT = 800                 # символов в подсказке распознаванию
MAX_LEARN_TEXT = 20000             # символов raw/final в learn
MAX_SUGGESTIONS = 20

AUDIO_SUFFIX = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav",
                "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/mp4": ".m4a",
                "audio/mpeg": ".mp3", "audio/mp4;codecs=mp4a": ".m4a"}
AUDIO_TYPES = ("audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/wave",
                "audio/mp4", "audio/mpeg")


class SttError(Exception):
    """Ошибка диктовки: код API, HTTP-статус и русское сообщение.

    ``stt_unavailable`` 503, ``audio_too_big`` 413, ``audio_empty`` 400,
    ``stt_failed`` 500, ``bad_request`` 400 (словарь и параметры).
    """

    def __init__(self, code: str, status: int, message_ru: str, detail: dict | None = None) -> None:
        super().__init__(message_ru)
        self.code = str(code)
        self.status = int(status)
        self.message_ru = str(message_ru)
        self.detail = dict(detail or {})


# --- конфигурация Hermes (только чтение) -----------------------------------
def hermes_config_path(cfg) -> Path:
    """``<hermes_home>/config.yaml`` — реальный конфиг Hermes, только чтение."""
    home = getattr(cfg, "hermes_home", None)
    if not home:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return Path(home) / "config.yaml"


def _scalar(text: str):
    text = text.strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1:] == text[0] and len(text) >= 2:
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


def parse_simple_block(text: str, name: str = "stt") -> dict:
    """Минимальный разбор блока ``stt:`` (или другого, ``name``) без PyYAML.

    Понимает вложенность по отступам, кавычки и true/false — ровно то, что
    встречается в конфиге Hermes. Нужен, если сервер запущен на stdlib-interpreter.
    """
    lines = (text or "").lstrip("\ufeff").splitlines()
    head = re.compile(r"^%s:\s*(#.*)?$" % re.escape(str(name)))
    start = None
    for index, line in enumerate(lines):
        if head.match(line):
            start = index + 1
            break
    if start is None:
        return {}
    items: list[tuple[int, str]] = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            break
        items.append((indent, line.strip()))

    def parse(pos: int, base_indent: int):
        result: dict = {}
        while pos < len(items):
            indent, raw = items[pos]
            if indent < base_indent:
                break
            if ":" not in raw:
                pos += 1
                continue
            key, _, value = raw.partition(":")
            key = key.strip().strip("\"'")
            value = value.strip()
            if value:
                result[key] = _scalar(value)
                pos += 1
                continue
            child, pos = parse(pos + 1, indent + 1)
            result[key] = child
        return result, pos

    return parse(0, items[0][0])[0] if items else {}


def parse_stt_block(text: str) -> dict:
    """Блок ``stt:`` из config.yaml Hermes (запасной разбор, без PyYAML)."""
    return parse_simple_block(text, "stt")


_CONFIG_CACHE: dict = {"key": None, "doc": {}}


def _config_doc(cfg) -> dict:
    """Весь config.yaml Hermes как словарь — только чтение, с кешем по mtime.

    Нужен для настроек вне блока ``stt:`` (например ``voice.max_recording_seconds``).
    Битый или отсутствующий файл — пустой словарь, а не исключение.
    """
    path = hermes_config_path(cfg)
    try:
        stat = path.stat()
        key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return {}
    if _CONFIG_CACHE.get("key") == key:
        return dict(_CONFIG_CACHE.get("doc") or {})
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError:
        return {}
    data: dict = {}
    if text:
        try:
            import yaml                                       # noqa: PLC0415 - необязательный
            loaded = yaml.safe_load(text)
            data = loaded if isinstance(loaded, dict) else {}
        except Exception:                                      # noqa: BLE001 - нет PyYAML
            data = {}
        if not data:
            # запасной путь: два блока верхнего уровня, которых достаточно диктовке
            data = {"stt": parse_simple_block(text, "stt"),
                    "voice": parse_simple_block(text, "voice")}
            data = {key_: value for key_, value in data.items() if value}
    _CONFIG_CACHE["key"], _CONFIG_CACHE["doc"] = key, data
    return dict(data)


def part_seconds() -> float:
    """Длина одной части длинной записи (секунды).

    Своих настроек не заводим: значение задаётся переменной окружения
    ``TEAMROOM_STT_PART_SECONDS`` (стенды и проверки), по умолчанию — 2 минуты.
    Короткие части дают две вещи: каждая часть — самостоятельный файл, который
    распознаётся отдельно (обрыв одной не убивает остальные), и прогресс.
    """
    raw = str(os.environ.get("TEAMROOM_STT_PART_SECONDS") or "").strip()
    try:
        value = float(raw.replace(",", ".")) if raw else DEFAULT_PART_SECONDS
    except ValueError:
        value = DEFAULT_PART_SECONDS
    if value <= 0:
        value = DEFAULT_PART_SECONDS
    return float(min(600.0, max(10.0, value)))


def record_limit(cfg) -> dict:
    """Лимит одной записи — из настроек Hermes, а не из своих констант.

    Читаем ``voice.max_recording_seconds`` (у Hermes это «лимит диктовки»), при
    отсутствии — ``stt.max_recording_seconds``. Значение только на чтение: файлы
    Hermes не меняются. Меньше 10 минут лимит не опускаем (WAVE13, пункт 2:
    «лимит длины записи — минимум 10 минут»), иначе поле комнаты окажется короче,
    чем требует Михаил. Нет значения вовсе — 600 с.
    """
    doc = _config_doc(cfg)
    voice = doc.get("voice") if isinstance(doc.get("voice"), dict) else {}
    stt_block = doc.get("stt") if isinstance(doc.get("stt"), dict) else {}
    raw = voice.get("max_recording_seconds")
    where = "voice.max_recording_seconds"
    if raw is None:
        raw = stt_block.get("max_recording_seconds")
        where = "stt.max_recording_seconds"
    seconds = 0
    try:
        if raw is not None and not isinstance(raw, bool):
            seconds = int(round(float(raw)))
    except (TypeError, ValueError):
        seconds = 0
    source = "hermes" if seconds > 0 else "fallback"
    if seconds <= 0:
        seconds = DEFAULT_RECORD_SECONDS
    return {"seconds": max(MIN_RECORD_SECONDS, seconds),
            "hermes_seconds": seconds if source == "hermes" else None,
            "source": source, "setting": where if source == "hermes" else None,
            "minimum": MIN_RECORD_SECONDS, "part_seconds": part_seconds()}


def read_settings(cfg) -> dict:
    """Настройки распознавания из config.yaml: model/device/compute_type/prompt/vad/language."""
    data = _config_doc(cfg)
    stt = data.get("stt") if isinstance(data.get("stt"), dict) else {}
    local = stt.get("local") if isinstance(stt.get("local"), dict) else {}
    vad = local.get("vad", True)
    if isinstance(vad, dict):
        vad = bool(vad.get("filter", True))
    return {"model": str(local.get("model") or stt_worker.DEFAULT_MODEL),
            "device": str(local.get("device") or stt_worker.DEFAULT_DEVICE),
            "compute_type": str(local.get("compute_type") or stt_worker.DEFAULT_COMPUTE),
            "initial_prompt": str(local.get("initial_prompt") or ""),
            "vad": bool(vad),
            "language": str(stt.get("language") or stt_worker.DEFAULT_LANGUAGE)}


# --- словарь ---------------------------------------------------------------
def dict_path(cfg) -> Path:
    """``data/dictation/dictionary.json`` — словарь комнаты (одна установка)."""
    return Path(cfg.data_dir) / "dictation" / "dictionary.json"


def empty_dictionary() -> dict:
    return {"terms": [], "replacements": [], "updated_at": 0}


def _text_field(value, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SttError("bad_request", 400, "Поле %s должно быть строкой" % field, {"field": field})
    text = value.replace("\x00", "").strip()
    if len(text) > limit:
        raise SttError("bad_request", 400, "Поле %s длиннее %d символов" % (field, limit),
                       {"field": field, "limit": limit, "length": len(text)})
    return text


def normalise_dictionary(obj) -> dict:
    """Проверить и схлопнуть словарь: без дублей, с счётчиками и лимитами.

    Термины — до 300 штук по 60 символов; замены — до 200 пар, ``from`` ≠ ``to``.
    Дубли (без учёта регистра) складываются: ``count`` суммируется.
    """
    if not isinstance(obj, dict):
        raise SttError("bad_request", 400, "Словарь должен быть объектом", {"field": "body"})
    raw_terms = obj.get("terms")
    raw_reps = obj.get("replacements")
    if raw_terms is None:
        raw_terms = []
    if raw_reps is None:
        raw_reps = []
    if not isinstance(raw_terms, list):
        raise SttError("bad_request", 400, "terms должно быть списком строк", {"field": "terms"})
    if not isinstance(raw_reps, list):
        raise SttError("bad_request", 400, "replacements должно быть списком объектов",
                       {"field": "replacements"})

    terms: list[str] = []
    seen_terms: set[str] = set()
    for item in raw_terms:
        term = _text_field(item, "terms", MAX_TERM_LEN)
        if not term:
            continue
        key = term.casefold()
        if key in seen_terms:
            continue
        seen_terms.add(key)
        terms.append(term)
    if len(terms) > MAX_TERMS:
        raise SttError("bad_request", 400, "Слишком много терминов: максимум %d" % MAX_TERMS,
                       {"field": "terms", "limit": MAX_TERMS, "length": len(terms)})

    now = int(time.time())
    replacements: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    for item in raw_reps:
        if not isinstance(item, dict):
            raise SttError("bad_request", 400, "replacements: нужны объекты {from, to}",
                           {"field": "replacements"})
        src = _text_field(item.get("from"), "replacements.from", MAX_PAIR_LEN)
        dst = _text_field(item.get("to"), "replacements.to", MAX_PAIR_LEN)
        if not src or not dst:
            raise SttError("bad_request", 400, "Замена не может быть пустой",
                           {"field": "replacements", "from": src, "to": dst})
        if src.casefold() == dst.casefold():
            raise SttError("bad_request", 400, "Замена совпадает с исходным: «%s»" % src,
                           {"field": "replacements", "from": src, "to": dst})
        try:
            count = max(1, int(item.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        try:
            added_at = int(item.get("added_at") or now)
        except (TypeError, ValueError):
            added_at = now
        key = (src.casefold(), dst.casefold())
        if key in index:
            index[key]["count"] += count
            continue
        record = {"from": src, "to": dst, "count": count, "added_at": added_at}
        index[key] = record
        replacements.append(record)
    if len(replacements) > MAX_REPLACEMENTS:
        raise SttError("bad_request", 400, "Слишком много замен: максимум %d" % MAX_REPLACEMENTS,
                       {"field": "replacements", "limit": MAX_REPLACEMENTS,
                        "length": len(replacements)})

    try:
        updated_at = int(obj.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    return {"terms": terms, "replacements": replacements, "updated_at": updated_at}


def load_dictionary(cfg) -> dict:
    """Прочитать словарь; битый файл не роняет диктовку — отдаём пустой словарь."""
    path = dict_path(cfg)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return empty_dictionary()
    try:
        doc = normalise_dictionary(data)
    except SttError:
        return empty_dictionary()
    doc["updated_at"] = int(doc.get("updated_at") or 0)
    return doc


def _replace_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """``os.replace`` с повторами: Windows держит файл, пока его читают."""
    delay = 0.02
    for attempt in range(1, attempts + 1):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if attempt >= attempts:
                raise
            time.sleep(delay)
            delay = min(0.2, delay * 2)


def _atomic_write(path: Path, payload: str) -> None:
    """Запись через временный файл рядом + ``os.replace`` (читатель видит целое)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex[:8]))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def save_dictionary(cfg, doc: dict) -> dict:
    """Проверить, проставить ``updated_at`` и атомарно записать словарь."""
    result = normalise_dictionary(doc)
    result["updated_at"] = int(time.time())
    _atomic_write(dict_path(cfg), json.dumps(result, ensure_ascii=False, indent=2))
    return result


def get_dictionary(cfg) -> dict:
    """GET-форма словаря: ``{terms, replacements, updated_at}``."""
    return load_dictionary(cfg)


def put_dictionary(cfg, obj) -> dict:
    """PUT словаря: валидация, слияние с текущим (не переданное — сохраняется)."""
    if not isinstance(obj, dict):
        raise SttError("bad_request", 400, "Словарь должен быть объектом", {"field": "body"})
    current = load_dictionary(cfg)
    merged = {"terms": obj.get("terms", current["terms"]),
              "replacements": obj.get("replacements", current["replacements"]),
              "updated_at": current.get("updated_at") or 0}
    return save_dictionary(cfg, merged)


# --- замены и подсказка ----------------------------------------------------
def _case_like(matched: str, replacement: str) -> str:
    """Сохранить регистр первой буквы (и КАПС целиком), как в продиктованном."""
    if not matched:
        return replacement
    if len(matched) > 1 and matched.isupper():
        return replacement.upper()
    if matched[0].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def apply_replacements(text: str, replacements: list[dict] | None) -> tuple[str, list[dict]]:
    """Применить замены: без учёта регистра, по границам слов, регистр первой буквы.

    Все правила собираются в одну регулярку (длинные фразы первыми), поэтому
    результат одной замены не переписывается другой: «гранит управление» →
    «Гранит-управление» не портится коротким правилом «гранит». Возвращает
    ``(текст, сработавшие пары)``.
    """
    out = str(text or "")
    rules = [item for item in (replacements or []) if isinstance(item, dict)
             and str(item.get("from") or "").strip() and str(item.get("to") or "").strip()]
    if not rules:
        return out, []
    rules.sort(key=lambda item: len(str(item["from"])), reverse=True)
    mapping: dict[str, tuple[str, str]] = {}
    parts: list[str] = []
    for item in rules:
        source = " ".join(str(item["from"]).split())
        key = source.casefold()
        if key in mapping:
            continue                                  # дубль правила — один раз
        mapping[key] = (str(item["from"]).strip(), str(item["to"]).strip())
        parts.append(r"\s+".join(re.escape(piece) for piece in source.split()))
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)",
                         re.IGNORECASE | re.UNICODE)
    used: list[tuple[str, str]] = []

    def substitute(match):
        key = " ".join(match.group(0).split()).casefold()
        source, target = mapping.get(key, (match.group(0), match.group(0)))
        used.append((source, target))
        return _case_like(match.group(0), target)

    out = pattern.sub(substitute, out)
    applied: list[dict] = []
    for source, target in used:
        pair = {"from": source, "to": target}
        if pair not in applied:
            applied.append(pair)
    return out, applied


def build_prompt(cfg, terms: list[str] | None = None, limit: int = PROMPT_LIMIT) -> str:
    """Подсказка распознаванию: ``initial_prompt`` + термины словаря.

    Новые термины в приоритете: они дописываются с конца, а базовая подсказка
    усекается, чтобы всё уложилось в ``limit`` символов.
    """
    settings = read_settings(cfg)
    base = str(settings.get("initial_prompt") or "").strip()
    items = [str(term).strip() for term in (terms if terms is not None
                                            else load_dictionary(cfg).get("terms") or [])]
    items = [term for term in items if term]
    if not items:
        return base[:limit]
    header = "Термины: "
    budget = max(0, limit // 2)
    chosen: list[str] = []
    used = len(header) + 1
    for term in reversed(items):                     # с конца: новые важнее
        piece = term if not chosen else term + ", "
        if used + len(piece) > budget:
            break
        chosen.append(term)
        used += len(piece)
    if not chosen:
        return base[:limit]
    chosen.reverse()
    suffix = " " + header + ", ".join(chosen) + "."
    if len(suffix) >= limit:
        return suffix.strip()[:limit]
    room = limit - len(suffix)
    return (base[:room].rstrip() + suffix).strip()[:limit]


# --- обучение на правке ---------------------------------------------------
def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"\s+", str(text or "").strip()) if token]


def _norm(token: str) -> str:
    """Нормализация слова: без пунктуации, в нижнем регистре."""
    return re.sub(r"[^\w]+", "", token, flags=re.UNICODE).casefold()


def _clean(token: str) -> str:
    """Слово без обрамляющей пунктуации (для from/to)."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", token, flags=re.UNICODE)


def learn(cfg, raw: str, final: str, *, apply: bool = False) -> dict:
    """Сравнить продиктованное и отправленное пословно и предложить замены 1–3 слов.

    Пунктуация и регистр не в счёт; вставки/удаления целых фраз игнорируются —
    предлагаются только замены, где с обеих сторон 1–3 слова.
    """
    if not isinstance(raw, str) or not isinstance(final, str):
        raise SttError("bad_request", 400, "Нужны строки raw и final", {"field": "raw"})
    if len(raw) > MAX_LEARN_TEXT or len(final) > MAX_LEARN_TEXT:
        raise SttError("bad_request", 400, "Текст длиннее %d символов" % MAX_LEARN_TEXT,
                       {"field": "raw", "limit": MAX_LEARN_TEXT})
    raw_tokens, final_tokens = _tokens(raw), _tokens(final)
    left = [_norm(token) for token in raw_tokens]
    right = [_norm(token) for token in final_tokens]
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    suggestions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue                                   # вставки/удаления фраз не предлагаем
        if not (1 <= i2 - i1 <= 3 and 1 <= j2 - j1 <= 3):
            continue
        source = [word for word in (_clean(token) for token in raw_tokens[i1:i2]) if word]
        target = [word for word in (_clean(token) for token in final_tokens[j1:j2]) if word]
        if not source or not target:
            continue                                   # только пунктуация
        from_text, to_text = " ".join(source), " ".join(target)
        if from_text.casefold() == to_text.casefold():
            continue                                   # регистр/пунктуация — не правка
        if len(from_text) > MAX_TERM_LEN * 3 or len(to_text) > MAX_TERM_LEN * 3:
            continue
        key = (from_text.casefold(), to_text.casefold())
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({"from": from_text, "to": to_text})
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    saved: list[dict] = []
    if apply and suggestions:
        doc = load_dictionary(cfg)
        for item in suggestions:
            doc["replacements"].append({"from": item["from"], "to": item["to"],
                                        "count": 1, "added_at": int(time.time())})
            if len(item["to"]) <= MAX_TERM_LEN and item["to"].casefold() not in \
                    {term.casefold() for term in doc["terms"]}:
                doc["terms"].append(item["to"])
            saved.append(dict(item))
        doc["terms"] = doc["terms"][-MAX_TERMS:]
        doc["replacements"] = doc["replacements"][-MAX_REPLACEMENTS:]
        save_dictionary(cfg, doc)
    return {"suggestions": suggestions, "saved": saved}


# --- клиент постоянного процесса -----------------------------------------
def _estimate_duration(size: int, suffix: str) -> float:
    """Оценка длительности по размеру: wav — из байтов, иначе ~3 КБ/с (opus 24k)."""
    if suffix == ".wav":
        return max(0.0, size / (16000 * 2.0))
    return max(0.0, size / 3000.0)


def _timeout_for(size: int, suffix: str, cold: bool = False) -> float:
    """Таймаут распознавания: max(60 с, 3×длительность); холодный старт — +120 с.

    Первый запрос поднимает модель (large-v3-turbo на CUDA — 15–45 с), поэтому
    ему нужен запас, иначе сервер убил бы нормальную загрузку.
    """
    base = max(60.0, 3.0 * _estimate_duration(size, suffix))
    if cold:
        base += 120.0
    return min(3600.0, base)


class _WorkerClient:
    """Один процесс на сервер: очередь запросов, ленивый старт, перезапуск при падении."""

    def __init__(self) -> None:
        self._queue = threading.Lock()          # один запрос за раз (очередь)
        self._state_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stderr: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._state = {"loaded": False, "loading": False, "model": None,
                       "device": None, "reason": None}

    # -- состояние
    def snapshot(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def _set_state(self, **values) -> None:
        with self._state_lock:
            self._state.update(values)

    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # -- процесс
    def _spawn(self) -> "subprocess.Popen":
        python = stt_worker.find_python()
        if not python:
            raise SttError("stt_unavailable", 503,
                           "Распознавание речи не запущено: не найден Python с faster-whisper",
                           {"reason": "no_python"})
        worker = Path(__file__).resolve().with_name("stt_worker.py")
        if not worker.is_file():
            raise SttError("stt_unavailable", 503,
                           "Распознавание речи не запущено: нет файла stt_worker.py",
                           {"reason": "no_worker_file"})
        try:
            # CREATE_NO_WINDOW: python воркера не должен мигать консолью.
            process = proc.popen(
                [python, str(worker)], cwd=str(worker.parent), env=stt_worker.worker_env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            raise SttError("stt_unavailable", 503,
                           "Распознавание речи не запущено: %s" % str(exc)[:160],
                           {"reason": "spawn_failed"}) from exc
        self._proc = process
        self._stderr = []
        self._stderr_thread = threading.Thread(target=self._drain, args=(process,), daemon=True)
        self._stderr_thread.start()
        return process

    def _drain(self, proc: subprocess.Popen) -> None:
        """Читать stderr воркера, чтобы он не залип на полном буфере (в журнал — хвост)."""
        try:
            for line in proc.stderr or []:
                text = str(line).strip()
                if text:
                    self._stderr.append(text[-300:])
                    del self._stderr[:-20]
        except (OSError, ValueError):
            pass

    def ensure(self) -> "subprocess.Popen":
        if self.alive():
            return self._proc
        self._kill()
        return self._spawn()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        self._set_state(loaded=False, loading=False)

    def stop(self) -> None:
        """Выгрузить модель и погасить процесс (остановка сервера, тесты)."""
        with self._queue:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
            self._kill()

    # -- обмен
    def _readline(self, timeout: float) -> str:
        box: dict = {}

        def work() -> None:
            try:
                box["line"] = self._proc.stdout.readline()
            except Exception as exc:                      # noqa: BLE001 - сломанный канал
                box["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(max(0.1, timeout))
        if thread.is_alive():
            raise TimeoutError("воркер не ответил за %.0f с" % timeout)
        if "error" in box:
            raise box["error"]
        return str(box.get("line") or "")

    def _exchange(self, payload: dict, timeout: float) -> dict:
        proc = self.ensure()
        line = json.dumps(payload, ensure_ascii=False)
        if proc.stdin is None or proc.stdout is None:
            raise OSError("канал воркера закрыт")
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        raw = self._readline(timeout)
        if not raw:
            raise EOFError("воркер закрыл канал (код %s)" % proc.poll())
        return json.loads(raw)

    def transcribe(self, path: str, language: str | None, prompt: str, timeout: float) -> dict:
        with self._queue:
            self._set_state(loading=not self.snapshot().get("loaded"))
            payload = {"op": "transcribe", "path": path, "language": language,
                       "prompt": prompt, "tmp_dir": str(Path(path).parent)}
            last_error: BaseException | None = None
            for attempt in (1, 2):
                try:
                    response = self._exchange(payload, timeout)
                except TimeoutError as exc:
                    self._kill()
                    self._set_state(loading=False)
                    raise SttError("stt_failed", 500,
                                   "Распознавание не уложилось во время: %s" % str(exc)[:160],
                                   {"reason": "timeout"}) from exc
                except (OSError, EOFError, ValueError) as exc:
                    last_error = exc
                    self._kill()
                    if attempt == 1:
                        continue                           # перезапуск после падения
                    self._set_state(loading=False)
                    tail = " | ".join(self._stderr[-3:])
                    raise SttError("stt_failed", 500,
                                   "Распознавание речи упало: %s" % str(exc)[:160],
                                   {"reason": "worker_crashed", "stderr": tail[:500]}) from exc
                self._set_state(loaded=bool(response.get("loaded", response.get("ok"))),
                                loading=False, model=response.get("model"),
                                device=response.get("device"), reason=response.get("reason"))
                if not response.get("ok"):
                    code = str(response.get("code") or "stt_failed")
                    status = {"audio_empty": 400, "audio_too_big": 413,
                              "stt_unavailable": 503}.get(code, 500)
                    detail = {"reason": code}
                    if response.get("detail"):
                        detail["stderr"] = str(response["detail"])[:400]
                    raise SttError(code if code in ("audio_empty", "audio_too_big",
                                                    "stt_unavailable") else "stt_failed",
                                   status,
                                   str(response.get("message") or "Не удалось распознать аудио"),
                                   detail)
                return response
            raise SttError("stt_failed", 500, "Распознавание речи недоступно",
                           {"reason": "worker_crashed", "error": str(last_error)[:200]})


_CLIENT = _WorkerClient()
atexit.register(_CLIENT.stop)


def shutdown() -> None:
    """Погасить рабочий процесс (остановка сервера, тесты)."""
    _CLIENT.stop()


def reset_worker() -> None:
    """Сбросить процесс и кеш поиска интерпретатора (тесты)."""
    _CLIENT.stop()
    stt_worker.reset_lookup()


# --- длинные записи: части на диск (ничего не теряется при обрыве) --------
RECORDING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{5,40}$")


def recordings_root(cfg) -> Path:
    """``data/stt-recordings`` — аудио длинных записей (по одной папке на запись)."""
    return Path(cfg.data_dir) / "stt-recordings"


def valid_recording_id(rid) -> bool:
    """Номер записи приходит от браузера — только буквы, цифры, дефис."""
    return bool(RECORDING_ID_RE.fullmatch(str(rid or "")))


def recording_dir(cfg, rid) -> Path:
    """Папка записи; некорректный номер — 400, а не путь наружу."""
    if not valid_recording_id(rid):
        raise SttError("bad_request", 400, "Некорректный номер записи",
                       {"field": "rid", "value": str(rid)[:40]})
    return recordings_root(cfg) / str(rid)


def _recording_meta(cfg, rid) -> dict:
    """Метаданные записи: время, тип, части. Битый файл — как пустой."""
    path = recording_dir(cfg, rid) / "recording.json"
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    parts = data.get("parts")
    meta = {"rid": str(rid), "created_at": int(data.get("created_at") or 0),
            "updated_at": int(data.get("updated_at") or 0),
            "mime": str(data.get("mime") or "audio/webm"), "parts": {}}
    if isinstance(parts, dict):
        for key, item in parts.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict) and 1 <= number <= MAX_PARTS:
                meta["parts"][str(number)] = item
    return meta


def _write_recording_meta(cfg, rid, meta: dict) -> None:
    """Атомарно записать метаданные записи (читатель видит целый файл)."""
    directory = recording_dir(cfg, rid)
    directory.mkdir(parents=True, exist_ok=True)
    meta = dict(meta)
    meta["updated_at"] = int(time.time())
    _atomic_write(directory / "recording.json",
                  json.dumps(meta, ensure_ascii=False, indent=2))


_PRUNED: dict = {}   # {каталог: время последней уборки}


def prune_recordings(cfg, keep_hours: float = RECORDING_TTL_HOURS,
                     max_count: int = MAX_RECORDINGS, force: bool = False) -> int:
    """Убрать старые записи: старше ``keep_hours`` и сверх ``max_count``.

    Уборка ленивая (при обращении) и не чаще раза в минуту: отдельного таймера
    в сервере нет. Возвращает число удалённых записей.
    """
    root = recordings_root(cfg)
    key = str(root)
    now = time.time()
    if not force and now - float(_PRUNED.get(key) or 0) < 60.0:
        return 0
    _PRUNED[key] = now
    try:
        dirs = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return 0
    rows = []
    for directory in dirs:
        try:
            rows.append((directory.stat().st_mtime, directory))
        except OSError:
            continue
    rows.sort(reverse=True)
    removed = 0
    for index, (mtime, directory) in enumerate(rows):
        too_old = keep_hours > 0 and (now - mtime) > keep_hours * 3600.0
        too_many = max_count > 0 and index >= max_count
        if not (too_old or too_many):
            continue
        if drop_recording(cfg, directory.name):
            removed += 1
    return removed


def save_part(cfg, rid, number, data: bytes, content_type: str = "audio/webm") -> dict:
    """Сохранить одну часть записи на диск и вернуть её размер/оценку длительности.

    Части пишутся по мере записи (каждые ``part_seconds``), поэтому обрыв
    браузера, обновление страницы или ошибка сети не уносят уже наговоренное.
    """
    try:
        part_no = int(number)
    except (TypeError, ValueError):
        raise SttError("bad_request", 400, "Номер части должен быть числом",
                       {"field": "part"}) from None
    if not (1 <= part_no <= MAX_PARTS):
        raise SttError("bad_request", 400, "Номер части вне диапазона 1…%d" % MAX_PARTS,
                       {"field": "part", "limit": MAX_PARTS, "value": part_no})
    payload = bytes(data or b"")
    if not payload:
        raise SttError("audio_empty", 400, "Пустая часть записи",
                       {"field": "body", "length": 0})
    if len(payload) > MAX_PART_BYTES:
        raise SttError("audio_too_big", 413,
                       "Часть записи больше %d МиБ — уменьшите длину части" %
                       (MAX_PART_BYTES // (1024 * 1024)),
                       {"length": len(payload), "limit": MAX_PART_BYTES})
    base_type = str(content_type or "").split(";")[0].strip().lower() or "audio/webm"
    if base_type not in AUDIO_TYPES:
        raise SttError("bad_request", 400, "Ожидается аудио: webm, ogg, wav или mp4",
                       {"field": "content_type", "allowed": list(AUDIO_TYPES)})
    suffix = AUDIO_SUFFIX.get(base_type, ".webm")
    meta = _recording_meta(cfg, rid)
    total = sum(int((item or {}).get("bytes") or 0) for item in meta["parts"].values())
    if total + len(payload) > MAX_RECORDING_BYTES:
        raise SttError("audio_too_big", 413,
                       "Запись больше %d МиБ — разбейте её на части покороче" %
                       (MAX_RECORDING_BYTES // (1024 * 1024)),
                       {"limit": MAX_RECORDING_BYTES, "length": total + len(payload)})
    directory = recording_dir(cfg, rid)
    directory.mkdir(parents=True, exist_ok=True)
    # Прошлая часть могла быть с другим типом (webm → wav) — старый файл убираем.
    for stale in directory.glob("part-%04d.*" % part_no):
        try:
            stale.unlink()
        except OSError:
            pass
    path = directory / ("part-%04d%s" % (part_no, suffix))
    tmp = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex[:8]))
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    seconds = round(_estimate_duration(len(payload), suffix), 2)
    now = int(time.time())
    if not meta["created_at"]:
        meta["created_at"] = now
    meta["mime"] = base_type
    meta["parts"][str(part_no)] = {"n": part_no, "bytes": len(payload), "mime": base_type,
                                   "seconds_est": seconds, "saved_at": now}
    _write_recording_meta(cfg, rid, meta)
    prune_recordings(cfg)          # диск не растёт: старше 72 ч и сверх 30 записей — вон
    return {"rid": str(rid), "part": part_no, "bytes": len(payload),
            "seconds_est": seconds, "parts_total": len(meta["parts"])}


def list_parts(cfg, rid) -> list[dict]:
    """Части записи по порядку: ``[{n, bytes, mime, seconds_est, path}]``."""
    directory = recording_dir(cfg, rid)
    meta = _recording_meta(cfg, rid)
    found = []
    for key, item in meta["parts"].items():
        number = int(key)
        suffix = AUDIO_SUFFIX.get(str(item.get("mime") or "audio/webm"), ".webm")
        path = directory / ("part-%04d%s" % (number, suffix))
        if not path.is_file():
            continue
        found.append({"n": number, "bytes": int(item.get("bytes") or path.stat().st_size),
                      "mime": str(item.get("mime") or "audio/webm"),
                      "seconds_est": float(item.get("seconds_est") or 0.0),
                      "saved_at": int(item.get("saved_at") or 0), "path": str(path)})
    found.sort(key=lambda row: row["n"])
    return found


def read_part(cfg, rid, number) -> tuple[bytes, str]:
    """Байты части и её тип (для повторного распознавания)."""
    try:
        part_no = int(number)
    except (TypeError, ValueError):
        raise SttError("bad_request", 400, "Номер части должен быть числом",
                       {"field": "part"}) from None
    for row in list_parts(cfg, rid):
        if row["n"] == part_no:
            try:
                with open(row["path"], "rb") as handle:
                    return handle.read(), row["mime"]
            except OSError as exc:
                raise SttError("stt_failed", 500, "Не удалось прочитать часть записи",
                               {"reason": "read_failed", "error": str(exc)[:200]}) from exc
    raise SttError("bad_request", 404,
                   "Часть записи не найдена" if str(rid) else "Нет такой записи",
                   {"field": "part", "value": part_no})


def recording_view(cfg, rid) -> dict:
    """Состояние записи для интерфейса: части, размеры, оценка длительности."""
    meta = _recording_meta(cfg, rid)
    parts = list_parts(cfg, rid)
    seconds = round(sum(row["seconds_est"] for row in parts), 2)
    return {"rid": str(rid), "created_at": int(meta.get("created_at") or 0),
            "updated_at": int(meta.get("updated_at") or 0),
            "mime": str(meta.get("mime") or "audio/webm"),
            "parts": [{"n": row["n"], "bytes": row["bytes"], "seconds_est": row["seconds_est"]}
                      for row in parts],
            "parts_total": len(parts), "bytes": sum(row["bytes"] for row in parts),
            "seconds_est": seconds}


def list_recordings(cfg, limit: int = 20) -> list[dict]:
    """Последние записи (для восстановления нерасшифрованного)."""
    prune_recordings(cfg)
    root = recordings_root(cfg)
    try:
        dirs = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return []
    dirs.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    rows = []
    for directory in dirs[:max(1, int(limit))]:
        if not valid_recording_id(directory.name):
            continue
        try:
            rows.append(recording_view(cfg, directory.name))
        except SttError:
            continue
        except OSError:
            continue
    return rows


def drop_recording(cfg, rid) -> bool:
    """Удалить запись целиком (после успешной вставки текста)."""
    directory = recording_dir(cfg, rid)
    if not directory.is_dir():
        return False
    try:
        shutil.rmtree(directory)
        return True
    except OSError:
        return False




# --- публичный контракт ---------------------------------------------------
_MODE_LOGGED: dict = {}      # {(cfg.data_dir, mode, provider): True} — пишем только смену


def _log_mode(cfg, mode: str, source: str, provider: str) -> None:
    """Строка в data/logs/stt.log при смене режима (без ключей и текстов)."""
    try:
        key = (str(getattr(cfg, "data_dir", "")), mode, provider)
    except Exception:  # noqa: BLE001
        return
    if _MODE_LOGGED.get(key):
        return
    _MODE_LOGGED[key] = True
    try:
        log_dir = Path(cfg.data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_dir / "stt.log", "a", encoding="utf-8") as fh:
            print("%s | режим=%s | источник=%s | провайдер=%s"
                  % (stamp, mode, source, provider or "-"), file=fh)
    except OSError:
        pass


def status(cfg) -> dict:
    """``{available, loaded, loading, model, device, mode, source, provider, reason}``.

    Плюс лимит записи (``max_recording_s``) из настроек Hermes и длина части
    длинной записи (``part_seconds``) — их берёт интерфейс, своих настроек
    диктовка не заводит.

    Режим выбирается по возможностям системы, а не по нашим ожиданиям:
    ``local`` — есть faster-whisper; ``provider`` — в Hermes настроен облачный STT
    и есть ключ; ``none`` — ничего нет, диктовка недоступна (остальное работает).
    """
    settings = read_settings(cfg)
    limit = record_limit(cfg)
    python = stt_worker.find_python()
    state = _CLIENT.snapshot()
    loaded = bool(state.get("loaded")) and _CLIENT.alive()
    reason = state.get("reason")
    cloud = stt_cloud.resolve(cfg) if not python else {}
    if python:
        mode, source, provider = "local", "local", "local"
        model = state.get("model") or settings["model"]
        device = state.get("device") or settings["device"]
        available = True
    elif cloud.get("mode") == "provider":
        mode, source, provider = "provider", "provider", str(cloud.get("provider") or "")
        model = str(cloud.get("model") or "")
        device = "cloud"
        available = True
        reason = None
    else:
        mode = "none"
        source = str(cloud.get("source") or "none")
        provider = str(cloud.get("provider") or "")
        model = settings["model"]
        device = settings["device"]
        available = False
        reason = str(cloud.get("reason") or stt_cloud.NO_PROVIDER_TEXT).strip()
        if stt_cloud.HINT_LOCAL not in reason:
            if not reason.endswith((".", "!", "?")):
                reason += "."
            reason += " Если нужна локальная диктовка: %s." % stt_cloud.HINT_LOCAL
    _log_mode(cfg, mode, source, provider)
    return {"available": bool(available), "loaded": loaded,
            "loading": bool(state.get("loading")) and _CLIENT.alive(),
            "model": model, "device": device, "mode": mode, "source": source,
            "provider": provider, "reason": reason,
            "max_recording_s": int(limit["seconds"]),
            "max_recording_source": limit["source"],
            "max_recording_setting": limit["setting"],
            "max_recording_hermes_s": limit["hermes_seconds"],
            "max_recording_min_s": int(limit["minimum"]),
            "part_seconds": float(limit["part_seconds"])}


def transcribe(cfg, audio: bytes, content_type: str = "audio/webm", *, language=None) -> dict:
    """Распознать аудио: текст + замены словаря.

    ``{"text", "raw", "duration_s", "elapsed_s", "language", "model", "device",
    "corrections": [{"from", "to"}]}``. Тишина — успех с ``text: ""``.
    """
    data = bytes(audio or b"")
    if not data:
        raise SttError("audio_empty", 400, "Пустое аудио: запись не содержит данных",
                       {"field": "body", "length": 0})
    if len(data) > MAX_AUDIO:
        raise SttError("audio_too_big", 413, "Аудио больше 64 МиБ — сократите запись",
                       {"length": len(data), "limit": MAX_AUDIO})
    base_type = str(content_type or "").split(";")[0].strip().lower()
    if base_type and base_type not in AUDIO_TYPES:
        raise SttError("bad_request", 400, "Ожидается аудио: webm, ogg, wav или mp4",
                       {"field": "content_type", "allowed": list(AUDIO_TYPES)})
    suffix = AUDIO_SUFFIX.get(base_type, ".webm")
    settings = read_settings(cfg)
    if not stt_worker.find_python():
        # Локального движка нет — работаем через провайдера, настроенного в Hermes.
        info = stt_cloud.resolve(cfg)
        if info.get("mode") != "provider":
            raise SttError("stt_unavailable", 503,
                           str(info.get("reason") or stt_cloud.NO_PROVIDER_TEXT),
                           {"mode": "none", "source": info.get("source") or "none"})
        started = time.time()
        try:
            answer = stt_cloud.transcribe(cfg, data, suffix, language=language or settings["language"])
        except stt_cloud.CloudError as exc:
            _log_mode(cfg, "provider", "provider", str(info.get("provider") or ""))
            raise SttError(exc.code, 502, exc.message_ru, exc.detail) from exc
        _log_mode(cfg, "provider", "provider", str(answer.get("provider") or ""))
        raw_text = str(answer.get("text") or "")
        text, applied = apply_replacements(raw_text, load_dictionary(cfg)["replacements"])
        return {"text": text, "raw": raw_text, "duration_s": 0.0,
                "elapsed_s": round(time.time() - started, 2),
                "language": str(language or settings["language"]),
                "model": str(answer.get("model") or ""),
                "device": "cloud:%s" % (answer.get("provider") or ""),
                "corrections": applied}
    prompt = build_prompt(cfg)
    tmp_dir = Path(cfg.tmp_dir)
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    path = tmp_dir / ("stt-%s%s" % (uuid.uuid4().hex[:10], suffix))
    started = time.time()
    try:
        try:
            path.write_bytes(data)
        except OSError as exc:
            raise SttError("stt_failed", 500,
                           "Не удалось сохранить запись во временный файл",
                           {"reason": "tmp_write", "error": str(exc)[:200]}) from exc
        cold = not _CLIENT.snapshot().get("loaded")
        response = _CLIENT.transcribe(str(path), language or settings["language"], prompt,
                                      _timeout_for(len(data), suffix, cold))
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    raw_text = str(response.get("text") or "")
    text, applied = apply_replacements(raw_text, load_dictionary(cfg)["replacements"])
    return {"text": text, "raw": raw_text,
            "duration_s": float(response.get("duration_s") or 0.0),
            "elapsed_s": round(time.time() - started, 2),
            "language": str(response.get("language") or language or settings["language"]),
            "model": str(response.get("model") or settings["model"]),
            "device": str(response.get("device") or settings["device"]),
            "corrections": applied}


__all__ = ["SttError", "MAX_AUDIO", "MAX_TERMS", "MAX_REPLACEMENTS", "PROMPT_LIMIT",
           "status", "transcribe", "get_dictionary", "put_dictionary", "learn",
           "apply_replacements", "build_prompt", "normalise_dictionary", "load_dictionary",
           "save_dictionary", "read_settings", "parse_stt_block", "dict_path", "shutdown",
           "reset_worker", "stt_cloud",
           # длинная запись: лимит из Hermes и части на диск
           "record_limit", "part_seconds", "save_part", "list_parts", "read_part",
           "recording_view", "list_recordings", "drop_recording", "prune_recordings",
           "valid_recording_id", "recordings_root", "MIN_RECORD_SECONDS"]
