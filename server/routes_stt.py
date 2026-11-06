"""HTTP-маршруты диктовки: статус, распознавание, словарь, обучение.

Миксин для ``App`` (server/app.py). Сам ничего не считает: проверяет форму
запроса и зовёт ``server/stt.py``, превращая ``SttError`` в единый конверт
ошибки API (``{error: {code, message, detail}}``, docs/API.md §1).
"""
from __future__ import annotations

import re

try:  # пакетный и «плоский» импорт
    from . import http_util
    from .http_util import ApiError, err
    from .app_common import try_call
except ImportError:  # pragma: no cover
    import http_util  # type: ignore
    from http_util import ApiError, err  # type: ignore
    from app_common import try_call  # type: ignore

STT_MAX_BODY = 64 * 1024 * 1024          # только для POST /api/stt/transcribe
AUDIO_TYPES = ("audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/wave",
               "audio/mp4", "audio/mpeg")
LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z-]{1,9}$")
TRANSCRIBE_PATH = "/api/stt/transcribe"

# --- длинная запись: части на диск (WAVE13 §2) -----------------------------
RID_RE = r"(?P<rid>[a-z0-9][a-z0-9_-]{5,40})"
PART_PATH_RE = re.compile(r"^/api/stt/recordings/[^/]{1,64}/parts/\d{1,3}$")

ROUTES = [
    # части записи: кладём и читаем по одной (обрыв не уносит наговоренное)
    ("PUT", r"/api/stt/recordings/" + RID_RE + r"/parts/(?P<part>\d{1,3})", "h_stt_part_save"),
    ("POST", r"/api/stt/recordings/" + RID_RE + r"/parts/(?P<part>\d{1,3})", "h_stt_part_save"),
    ("GET", r"/api/stt/recordings/" + RID_RE + r"/parts/(?P<part>\d{1,3})", "h_stt_part_get"),
    ("GET", r"/api/stt/recordings/" + RID_RE, "h_stt_recording_get"),
    ("DELETE", r"/api/stt/recordings/" + RID_RE, "h_stt_recording_delete"),
    ("GET", r"/api/stt/recordings", "h_stt_recordings"),
]


def body_policy(path: str) -> dict:
    """Политика тела для транспорта: аудио — 64 МиБ и без разбора JSON."""
    if path == TRANSCRIBE_PATH or PART_PATH_RE.match(str(path or "")):
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

    # --- части длинной записи (WAVE13 §2)
    def h_stt_part_save(self, query, body, rid=None, part=None):
        """PUT|POST /api/stt/recordings/{rid}/parts/{n} — сохранить часть записи.

        Части шлются по ходу записи, поэтому обрыв связи, закрытие окна или
        перезагрузка страницы не уносят уже наговоренное: на диске остаётся всё
        до последней целой части, и это можно распознать повторно.
        """
        rid = str(rid or "")
        data = getattr(body, "data", None)
        content_type = str(getattr(body, "content_type", "") or "")
        if not isinstance(data, (bytes, bytearray)):
            raise err(400, "Тело запроса должно быть аудио (webm, ogg, wav, mp4)", field="body")
        return 200, self._stt_call("save_part",
                                   ((self.cfg, rid, part, bytes(data), content_type), {})), {}

    def h_stt_recording_get(self, query, body, rid=None):
        """GET /api/stt/recordings/{rid} — что уже сохранено по этой записи."""
        return 200, self._stt_call("recording_view", ((self.cfg, str(rid or "")), {})), \
            {"Cache-Control": "no-store"}

    def h_stt_part_get(self, query, body, rid=None, part=None):
        """GET /api/stt/recordings/{rid}/parts/{n} — аудио части (для повтора)."""
        try:
            module = self.mod("stt")
            payload, ctype = try_call(module.read_part, ((self.cfg, str(rid or ""), part), {}))
        except Exception as exc:  # noqa: BLE001 - SttError → ошибка API
            code = getattr(exc, "code", None)
            if code is None:
                raise err(500, "Не удалось отдать часть записи", field="part") from exc
            raise ApiError(int(getattr(exc, "status", 500)),
                           str(getattr(exc, "message_ru", "Часть записи недоступна")),
                           code=str(code), detail={}) from exc
        return 200, http_util.Raw(payload, str(ctype or "audio/webm"),
                                  {"Cache-Control": "no-store"}), {}

    def h_stt_recordings(self, query, body):
        """GET /api/stt/recordings — последние записи (для восстановления)."""
        raw_limit = (query.get("limit") or [None])[0]
        limit = 20
        if raw_limit not in (None, ""):
            try:
                limit = max(1, min(50, int(str(raw_limit))))
            except (TypeError, ValueError):
                raise err(400, "limit: целое число 1…50", field="limit",
                          value=str(raw_limit)[:20]) from None
        return 200, {"items": self._stt_call("list_recordings", ((self.cfg, limit), {})),
                     "limit": limit}, {"Cache-Control": "no-store"}

    def h_stt_recording_delete(self, query, body, rid=None):
        """DELETE /api/stt/recordings/{rid} — убрать запись после вставки текста."""
        removed = self._stt_call("drop_recording", ((self.cfg, str(rid or "")), {}))
        return 200, {"rid": str(rid or ""), "removed": bool(removed)}, {}


def install_stt_routes(app_cls, routes: list) -> None:
    """Подключить части длинной записи к ``App``: методы миксина + маршруты."""
    for name, value in vars(SttRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        if not hasattr(app_cls, name):
            setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, re.compile(pattern), handler))
