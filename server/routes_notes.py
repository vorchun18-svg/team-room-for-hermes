"""HTTP-маршруты «Особенности для этой комнаты» (WAVE14 пакет I, пункт 13).

Указания — добавка к SOUL.md участника ТОЛЬКО в этой комнате; файлы профилей
(SOUL.md, config.yaml, skills) не меняются никогда. Записи живут в реестре комнат
(`data/rooms.json`, поле `agent_notes`), изменения — в журнале комнаты
(`agent_notes_log`) с возможностью откатить конкретное изменение.

Подключение — одним блоком в ``server/app.py``::

    from server.routes_notes import install_notes_routes
    install_notes_routes(App, ROUTES)

Маршруты (все ошибки — по-русски, 4xx, никогда 500):

    GET    /api/rooms/{r}/notes                              сводка по всем участникам
    GET    /api/rooms/{r}/agents/{p}/notes                   записи участника + лимиты
    PUT    /api/rooms/{r}/agents/{p}/notes                   полный текст (старый контракт)
    POST   /api/rooms/{r}/agents/{p}/notes/entries           добавить запись
    PATCH  /api/rooms/{r}/agents/{p}/notes/entries/{e}       изменить запись
    DELETE /api/rooms/{r}/agents/{p}/notes/entries/{e}       убрать запись
    POST   /api/rooms/{r}/agents/{p}/notes/review            переоценка (слияние/удаление)
    POST   /api/rooms/{r}/agents/{p}/notes/used              отметить «пригодилось»
    GET    /api/rooms/{r}/notes/journal                      журнал комнаты
    POST   /api/rooms/{r}/notes/journal/{e}/rollback         откатить изменение
"""
from __future__ import annotations

import re as _re

try:  # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    from app_common import err  # type: ignore
    from http_util import ApiError  # type: ignore

ROOM_RE = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"
PROFILE_RE = r"(?P<profile>[A-Za-z0-9_.@+\-]{1,64})"
ENTRY_RE = r"(?P<entry_id>[A-Za-z0-9_\-]{2,40})"
EVENT_RE = r"(?P<event_id>[A-Za-z0-9_\-]{2,40})"
BASE = r"/api/rooms/" + ROOM_RE

ROUTES = [
    ("GET", BASE + r"/notes", "h_notes_room"),
    ("GET", BASE + r"/agents/" + PROFILE_RE + r"/notes", "h_notes_get"),
    # PUT уже объявлен в server/routes_write.py (старый контракт «полный текст») — не дублируем.
    ("POST", BASE + r"/agents/" + PROFILE_RE + r"/notes/entries", "h_notes_add"),
    ("PATCH", BASE + r"/agents/" + PROFILE_RE + r"/notes/entries/" + ENTRY_RE, "h_notes_update"),
    ("DELETE", BASE + r"/agents/" + PROFILE_RE + r"/notes/entries/" + ENTRY_RE, "h_notes_remove"),
    ("POST", BASE + r"/agents/" + PROFILE_RE + r"/notes/review", "h_notes_review"),
    ("POST", BASE + r"/agents/" + PROFILE_RE + r"/notes/used", "h_notes_used"),
    ("GET", BASE + r"/notes/journal", "h_notes_journal"),
    ("POST", BASE + r"/notes/journal/" + EVENT_RE + r"/rollback", "h_notes_rollback"),
]

_STATUS = {"bad_request": 400, "not_found": 404, "conflict": 409, "unavailable": 503}


def _status(code) -> int:
    return _STATUS.get(str(code), 400)


class NotesRoutes:
    """Обработчики указаний (``self`` — экземпляр ``server.app.App``)."""

    # --- помощники ---------------------------------------------------------
    def _notes_mod(self):
        return self.mod("agent_notes")

    def _notes_call(self, attr: str, args: tuple, kwargs: dict | None = None):
        """Вызвать ``agent_notes.<attr>``; доменные ошибки — в 4xx по-русски."""
        mod = self._notes_mod()
        func = getattr(mod, attr, None)
        if not callable(func):
            raise ApiError(501, "Модуль agent_notes не умеет %s" % attr,
                           detail={"module": "agent_notes", "function": attr})
        try:
            with self.engine():
                return func(*args, **(kwargs or {}))
        except ApiError:
            raise
        except mod.NotesError as exc:
            raise ApiError(_status(exc.code), exc.message, code=str(exc.code),
                           detail=dict(exc.detail or {})) from exc

    def _notes_room(self, room_id: str) -> dict:
        with self.engine():
            room = self._room(room_id)
        if room.get("archived"):
            raise err(409, "Комната в архиве — указания не меняются", room=room_id)
        return room

    @staticmethod
    def _profile(profile) -> str:
        name = str(profile or "").strip()
        if not name:
            raise err(400, "Не указан участник", field="profile")
        return name

    def _in_room(self, room: dict, profile: str) -> None:
        people = [str(room.get("orchestrator") or ""), str(room.get("critic") or "")]
        people += [str((m or {}).get("profile") or "") for m in room.get("members") or []
                   if isinstance(m, dict)]
        if profile not in [p for p in people if p]:
            raise err(404, "Участника нет в комнате", profile=profile, room=room.get("id"))

    # --- чтение ------------------------------------------------------------
    def h_notes_room(self, query, body, room_id):
        """``GET /api/rooms/{id}/notes`` — записи всех участников одной сводкой."""
        room = self._notes_room(room_id)
        mod = self._notes_mod()
        try:
            notes = mod.notes_of(room)
            people = mod.participants(room)
            entries_of = mod.entries_of
        except Exception as exc:  # noqa: BLE001 - чтение не должно отдавать 500
            raise ApiError(500, "Не удалось прочитать указания комнаты",
                           detail={"error": str(exc)[:200]}) from exc
        rows = []
        for profile, role in people:
            item = notes.get(profile) or {}
            rows.append({"profile": profile, "role": role,
                         "entries": entries_of(item), "text": item.get("text") or "",
                         "updated_at": int(item.get("updated_at") or 0),
                         "review": item.get("review") or {"count_at_review": 0,
                                                          "reviewed_at": 0, "due": False}})
        return 200, {"ok": True, "room": room_id, "participants": rows,
                     "limits": mod.limits(),
                     "count": sum(len(r["entries"]) for r in rows)}, {}

    def h_notes_get(self, query, body, room_id, profile):
        """``GET …/agents/{profile}/notes`` — записи участника, лимиты, состояние переоценки."""
        room = self._notes_room(room_id)
        name = self._profile(profile)
        self._in_room(room, name)
        res = self._notes_call("get", (self.cfg, room_id, name))
        payload = {"ok": True}
        payload.update(res if isinstance(res, dict) else {})
        payload["journal"] = self._notes_call("journal", (self.cfg, room_id, 20))
        return 200, payload, {}

    def h_notes_journal(self, query, body, room_id):
        """``GET /api/rooms/{id}/notes/journal`` — журнал изменений комнаты."""
        self._notes_room(room_id)
        try:
            limit = int((query or {}).get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        items = self._notes_call("journal", (self.cfg, room_id, limit))
        return 200, {"ok": True, "room": room_id, "events": items,
                     "count": len(items)}, {}

    # --- изменение записей --------------------------------------------------
    def _body(self, body) -> dict:
        return body if isinstance(body, dict) else {}

    def h_notes_add(self, query, body, room_id, profile):
        """``POST …/notes/entries`` — дописать запись (руководитель делает это сам)."""
        room = self._notes_room(room_id)
        name = self._profile(profile)
        self._in_room(room, name)
        data = self._body(body)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise err(400, "Нужен текст записи", field="text")
        source = str(data.get("source") or "user").strip().lower()
        res = self._notes_call("add", (self.cfg, room_id, name, text), {
            "source": source,
            "origin": str(data.get("origin") or ""),
            "why": str(data.get("why") or ""),
            "actor": str(data.get("actor") or ""),
            "pinned": bool(data.get("pinned")),
        })
        payload = {"ok": True}
        payload.update(res if isinstance(res, dict) else {})
        return 201, payload, {}

    def h_notes_update(self, query, body, room_id, profile, entry_id):
        """``PATCH …/notes/entries/{entry}`` — изменить текст записи."""
        room = self._notes_room(room_id)
        name = self._profile(profile)
        self._in_room(room, name)
        data = self._body(body)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise err(400, "Нужен текст записи", field="text")
        res = self._notes_call("update_entry", (self.cfg, room_id, name, entry_id, text), {
            "why": str(data.get("why") or ""), "actor": str(data.get("actor") or "")})
        payload = {"ok": True}
        payload.update(res if isinstance(res, dict) else {})
        return 200, payload, {}

    @staticmethod
    def _q(query, key) -> str:
        """Значение query-параметра: parse_qs отдаёт список, а внутренние вызовы — строку."""
        raw = (query or {}).get(key)
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else ""
        return "" if raw is None else str(raw)

    def h_notes_remove(self, query, body, room_id, profile, entry_id):
        """``DELETE …/notes/entries/{entry}`` — убрать запись (закреплённую — только Михаил).

        `force` принимается и в теле, и в строке запроса (`?force=1`) — некоторые клиенты
        не передают тело у DELETE.
        """
        room = self._notes_room(room_id)
        name = self._profile(profile)
        self._in_room(room, name)
        data = self._body(body)
        force = bool(data.get("force")) or self._q(query, "force").lower() in ("1", "true", "yes")
        res = self._notes_call("remove_entry", (self.cfg, room_id, name, entry_id), {
            "why": str(data.get("why") or self._q(query, "why")),
            "actor": str(data.get("actor") or ""), "force": force})
        payload = {"ok": True}
        payload.update(res if isinstance(res, dict) else {})
        return 200, payload, {}

    def h_notes_review(self, query, body, room_id, profile):
        """``POST …/notes/review`` — переоценка: слить дубли, убрать устаревшее, сократить."""
        room = self._notes_room(room_id)
        name = self._profile(profile)
        self._in_room(room, name)
        data = self._body(body)
        apply = data.get("apply")
        apply = False if apply is False or str(apply).lower() in ("0", "false", "no") else True
        res = self._notes_call("review", (self.cfg, room_id, name), {
            "apply": apply, "actor": str(data.get("actor") or "Михаил"),
            "source": "user" if data.get("by") == "user" else "auto",
            "why": str(data.get("why") or ""), "origin": str(data.get("origin") or "")})
        payload = {"ok": True}
        payload.update(res if isinstance(res, dict) else {})
        payload["journal"] = self._notes_call("journal", (self.cfg, room_id, 20))
        return 200, payload, {}

    def h_notes_used(self, query, body, room_id, profile):
        """``POST …/notes/used`` — отметить, что записи пригодились (`used_count`)."""
        room = self._notes_room(room_id)
        name = self._profile(profile)
        self._in_room(room, name)
        data = self._body(body)
        ids = data.get("entry_ids")
        if ids is not None and not isinstance(ids, list):
            raise err(400, "entry_ids: список id записей", field="entry_ids")
        touched = self._notes_call("touch_used", (self.cfg, room_id, name, ids))
        return 200, {"ok": True, "profile": name, "touched": int(touched or 0)}, {}

    def h_notes_rollback(self, query, body, room_id, event_id):
        """``POST /api/rooms/{id}/notes/journal/{event}/rollback`` — откатить изменение."""
        room = self._notes_room(room_id)
        data = self._body(body)
        res = self._notes_call("rollback", (self.cfg, room_id, event_id), {
            "actor": str(data.get("actor") or "Михаил"),
            "why": str(data.get("why") or "")})
        payload = {"ok": True}
        payload.update(res if isinstance(res, dict) else {})
        payload["journal"] = self._notes_call("journal", (self.cfg, room_id, 20))
        return 200, payload, {}


def install_notes_routes(app_cls, routes: list) -> None:
    """Подключить указания к ``App``: методы миксина + строки таблицы маршрутов."""
    for name, value in vars(NotesRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        if getattr(app_cls, name, None) is not None and name.startswith("h_"):
            continue                      # не перекрываем одноимённый обработчик
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
