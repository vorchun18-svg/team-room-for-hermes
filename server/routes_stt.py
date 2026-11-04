"""HTTP-маршруты диктовки: статус, распознавание, словарь, обучение.

Миксин для ``App`` (server/app.py). Сам ничего не считает: проверяет форму
запроса и зовёт ``server/stt.py``, превращая ``SttError`` в единый конверт
ошибки API (``{error: {code, message, detail}}``, docs/API.md §1).
"""
from __future__ import annotations

import re

try:  # пакетный и «плоский» импорт
    from .http_util import ApiError, err
    from .app_common import try_call
except ImportError:  # pragma: no cover
    from http_util import ApiError, err  # type: ignore
    from app_common import try_call  # type: ignore

STT_MAX_BODY = 64 * 1024 * 1024          # только для POST /api/stt/transcribe
AUDIO_TYPES = ("audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/wave",
               "audio/mp4", "audio/mpeg")
LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z-]{1,9}$")
TRANSCRIBE_PATH = "/api/stt/transcribe"


def body_policy(path: str) -> dict:
    """Политика тела для транспорта: аудио — 64 МиБ и без разбора JSON."""
    if path == TRANSCRIBE_PATH:
        return {"limit": STT_MAX_BODY, "binary": True, "code": "audio_too_big",
                "message": "Аудио больше 64 МиБ — сократите запись"}
    return {}


class SttRoutes:
    """Обработчики ``/api/stt/*`` (``self`` — экземпляр ``server.app.App``)."""

    # --- помощники
    def _stt_call(self, name: str, *attempts):
        """Вызвать функцию stt.py, а её SttError отдать как ошибку API."""
        module = self.mod("stt")
        func = getattr(module, name, None)
        if not callable(func):
            raise ApiError(501, "Модуль stt не реализует %s" % name,
                           detail={"module": "stt", "function": name})
        try:
            return try_call(func, *attempts)
        except module.SttError as exc:
            detail = getattr(exc, "detail", None)
            raise ApiError(int(exc.status), str(exc.message_ru), code=exc.code,
                           detail=detail if isinstance(detail, dict) else {}) from exc

    @staticmethod
    def _json_body(body) -> dict:
        if not isinstance(body, dict):
            raise err(400, "Тело должно быть JSON-объектом", field="body")
        return body

    # --- GET /api/stt/status
    def h_stt_status(self, query, body):
        return 200, self._stt_call("status", ((self.cfg,), {})), {"Cache-Control": "no-store"}

    # --- POST /api/stt/transcribe (тело — байты аудио)
    def h_stt_transcribe(self, query, body):
        data = getattr(body, "data", None)
        if not isinstance(data, (bytes, bytearray)):
            raise err(400, "Тело запроса должно быть аудио (webm, ogg, wav, mp4)", field="body")
        if not data:
            raise ApiError(400, "Пустое аудио: запись не содержит данных", code="audio_empty",
                           detail={"field": "body", "length": 0})
        content_type = str(getattr(body, "content_type", "") or "")
        base_type = content_type.split(";")[0].strip().lower()
        if base_type and base_type not in AUDIO_TYPES:
            raise ApiError(415, "Ожидается аудио: webm, ogg, wav или mp4", code="bad_request",
                           detail={"content_type": base_type, "allowed": list(AUDIO_TYPES)})
        raw_language = (query.get("language") or [None])[0]
        language = None
        if raw_language:
            language = str(raw_language).strip()
            if not LANGUAGE_RE.fullmatch(language):
                raise err(400, "language: код языка, например ru", field="language",
                          value=str(raw_language)[:40])
        return 200, self._stt_call("transcribe",
                                   ((self.cfg, bytes(data), base_type or "audio/webm"),
                                    {"language": language})), {}

    # --- GET|PUT /api/stt/dictionary
    def h_stt_dictionary_get(self, query, body):
        return 200, self._stt_call("get_dictionary", ((self.cfg,), {})), {}

    def h_stt_dictionary_put(self, query, body):
        payload = self._json_body(body)
        return 200, self._stt_call("put_dictionary", ((self.cfg, dict(payload)), {})), {}

    # --- POST /api/stt/learn
    def h_stt_learn(self, query, body):
        payload = self._json_body(body)
        raw, final = payload.get("raw"), payload.get("final")
        if not isinstance(raw, str) or not isinstance(final, str):
            raise err(400, "Нужны строки raw и final", field="raw" if not isinstance(raw, str) else "final")
        apply = payload.get("apply", False)
        if apply is None:
            apply = False
        if not isinstance(apply, bool):
            raise err(400, "apply: true или false", field="apply",
                      type=type(apply).__name__)
        return 200, self._stt_call("learn", ((self.cfg, raw, final), {"apply": apply})), {}
