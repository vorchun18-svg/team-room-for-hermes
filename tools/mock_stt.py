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
