# -*- coding: utf-8 -*-
"""Мост мок-сервера к распознаванию речи (`server/stt.py`) — ленивый импорт.

Мок не изобретает текст: если рабочий модуль STT ещё не подключён (или в нём нет
нужной функции), все маршруты `/api/stt/*` честно отвечают
503 `stt_unavailable` с сообщением «Распознавание речи на этом стенде ещё не
подключено». Так демо не показывает выдуманную расшифровку.

Словарь демо пишется отдельно от боевого: по умолчанию
`data/dictation-demo/dictation/dictionary.json` (как `cfg.data_dir/dictation/`
в `server/stt.py`), корень можно переопределить переменной `TEAMROOM_DATA_DICT`.
В cfg дополнительно кладутся `dictation_dir` и `dictionary_path`.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from mock_core import ROOT, ApiError

UNAVAILABLE_MESSAGE = "Распознавание речи на этом стенде ещё не подключено"
STT_MAX_BODY = 64 * 1024 * 1024
ALLOWED_AUDIO = ("audio/webm", "audio/ogg", "audio/wav", "audio/mp4")

# Тесты подменяют модуль или принудительно выключают движок; в бою — None/False.
MODULE_OVERRIDE = None
FORCE_UNAVAILABLE = False
_cached = None
_loaded = False


def demo_dir() -> Path:
    """Каталог демо-словаря: TEAMROOM_DATA_DICT или data/dictation-demo."""
    raw = (os.environ.get("TEAMROOM_DATA_DICT") or "").strip()
    return Path(raw).expanduser() if raw else Path(ROOT) / "data" / "dictation-demo"


def dictionary_path() -> Path:
    """Файл словаря демо: `<demo_dir>/dictation/dictionary.json` (как в server/stt.py)."""
    return demo_dir() / "dictation" / "dictionary.json"


def demo_cfg(port: int | None = None):
    """Песочничный cfg для STT: data_dir указывает на каталог демо-словаря."""
    path = dictionary_path()
    data_dir = path.parent.parent
    cfg = None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from server import config as server_config
        cfg = server_config.load()
        cfg.data_dir = data_dir
        cfg.sandbox = True
    except Exception:  # noqa: BLE001 — cfg не должен ронять мок
        cfg = SimpleNamespace(hermes_home=Path(os.environ.get("LOCALAPPDATA") or Path.home()),
                              kanban_home=Path(ROOT), data_dir=data_dir,
                              vault_dir=Path(ROOT) / "data" / "vault", sandbox=True)
    cfg.dictation_dir = path.parent
    cfg.dictionary_path = path
    if port is not None:
        cfg.port = port
    return cfg


def _unavailable() -> ApiError:
    return ApiError(503, "stt_unavailable", UNAVAILABLE_MESSAGE, {"reason": "engine_not_ready"})


def reset(force_unavailable: bool = False) -> None:
    """Сбросить кэш импорта (тесты)."""
    global MODULE_OVERRIDE, FORCE_UNAVAILABLE, _cached, _loaded
    MODULE_OVERRIDE = None
    FORCE_UNAVAILABLE = bool(force_unavailable)
    _cached = None
    _loaded = False


def load_module():
    """Лениво импортировать server.stt; None — движок ещё не готов."""
    global _cached, _loaded
    if FORCE_UNAVAILABLE:
        return None
    if MODULE_OVERRIDE is not None:
        return MODULE_OVERRIDE
    if _loaded:
        return _cached
    _loaded = True
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from server import stt as module  # noqa: WPS433 — намеренно ленивый импорт
        _cached = module
    except Exception:  # noqa: BLE001 — нет модуля/зависимостей → честный 503
        _cached = None
    return _cached


def ensure_available() -> None:
    """До чтения тела убедиться, что движок есть (иначе 503 сразу)."""
    if load_module() is None:
        raise _unavailable()


def _call(name: str, *args, **kwargs):
    module = load_module()
    if module is None:
        raise _unavailable()
    fn = getattr(module, name, None)
    if not callable(fn):
        raise _unavailable()
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        message = getattr(exc, "message_ru", None)
        if isinstance(status, int) and code:
            raise ApiError(status, code, message or str(exc), getattr(exc, "detail", None) or {})
        raise ApiError(500, "stt_failed", "Не удалось распознать речь",
                       {"error": str(exc)[:200]})


def status() -> dict:
    return _call("status", demo_cfg())


def transcribe(audio: bytes, content_type: str, language: str | None = None) -> dict:
    return _call("transcribe", demo_cfg(), audio, content_type=content_type, language=language)


def get_dictionary() -> dict:
    return _call("get_dictionary", demo_cfg())


def put_dictionary(obj: dict) -> dict:
    return _call("put_dictionary", demo_cfg(), obj)


def learn(raw: str, final: str, apply: bool = False) -> dict:
    return _call("learn", demo_cfg(), raw, final, apply=apply)


# --- части длинной записи (WAVE13 §2) --------------------------------------
# Демо ничего не пишет на диск: части живут в памяти процесса мока.
import re as _re                                                     # noqa: E402

RID_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{5,40}$")
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_PARTS = 64
_PARTS: dict = {}          # rid → {n: (bytes, ctype)}
_CREATED: dict = {}        # rid → время создания


def _rid(rid) -> str:
    text = str(rid or "")
    if not RID_RE.fullmatch(text):
        raise ApiError(400, "bad_request", "Некорректный номер записи", {"field": "rid"})
    return text


def reset_parts() -> None:
    """Забыть все части (тесты и перезапуск демо)."""
    _PARTS.clear()
    _CREATED.clear()


def save_part(rid, part, audio: bytes, ctype: str) -> dict:
    """Сохранить часть записи в памяти демо (`{"rid","part","bytes",...}`)."""
    key = _rid(rid)
    try:
        number = int(part)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", "Номер части должен быть числом", {"field": "part"})
    if not 1 <= number <= MAX_PARTS:
        raise ApiError(400, "bad_request", "Номер части вне диапазона 1…%d" % MAX_PARTS,
                       {"field": "part", "limit": MAX_PARTS})
    payload = bytes(audio or b"")
    if not payload:
        raise ApiError(400, "audio_empty", "Пустая часть записи", {"field": "body"})
    if len(payload) > MAX_PART_BYTES:
        raise ApiError(413, "audio_too_big", "Часть записи больше 64 МиБ",
                       {"limit": MAX_PART_BYTES, "length": len(payload)})
    base = str(ctype or "audio/webm").split(";")[0].strip().lower() or "audio/webm"
    store = _PARTS.setdefault(key, {})
    store[number] = (payload, base)
    _CREATED.setdefault(key, int(time.time()))
    return {"rid": key, "part": number, "bytes": len(payload),
            "seconds_est": round(len(payload) / 3000.0, 2), "parts_total": len(store)}


def recording(rid) -> dict:
    """Сколько частей записи уже есть (демо)."""
    key = _rid(rid)
    store = _PARTS.get(key, {})
    parts = [{"n": n, "bytes": len(chunk), "seconds_est": round(len(chunk) / 3000.0, 2)}
             for n, (chunk, _ctype) in sorted(store.items())]
    return {"rid": key, "created_at": int(_CREATED.get(key) or 0), "updated_at": 0,
            "mime": "audio/webm", "parts": parts, "parts_total": len(parts),
            "bytes": sum(row["bytes"] for row in parts),
            "seconds_est": round(sum(row["seconds_est"] for row in parts), 2)}


def part(rid, part) -> tuple:
    """Байты части и её тип (для повторного распознавания в демо)."""
    key = _rid(rid)
    try:
        number = int(part)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", "Номер части должен быть числом", {"field": "part"})
    chunk = (_PARTS.get(key) or {}).get(number)
    if not chunk:
        raise ApiError(404, "bad_request", "Часть записи не найдена", {"field": "part"})
    return chunk[0], chunk[1]


def recordings(limit: int = 20) -> dict:
    """Последние записи демо (для «Распознать запись»)."""
    rows = [recording(key) for key in _PARTS]
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return {"items": rows[:max(1, int(limit))], "limit": limit}


def drop(rid) -> dict:
    """Убрать запись из памяти демо."""
    key = _rid(rid)
    existed = bool(_PARTS.pop(key, None))
    _CREATED.pop(key, None)
    return {"rid": key, "removed": existed}
