"""HTTP «Входящие комнаты» (W26, пакет D9): быстрая запись и разбор.

Подключение — одним блоком в ``server/app.py``::

    from server.routes_inbox import install_inbox_routes
    install_inbox_routes(App, ROUTES)

Маршруты:

* ``GET  /api/rooms/{id}/inbox?status=active|new|processing|processed|all``
* ``POST /api/rooms/{id}/inbox {text, source:"dictation|manual"}`` — записать мысль;
* ``POST /api/rooms/{id}/inbox/{item}/actions {action, …}`` — разбор
  (``propose|to_delo|to_step|to_question|to_duty|to_note|edit|accept|reject|delete|restore``).

Ничего не отправляет наружу и не запускает агентов: разбор — заготовка в
``data/state/<room>/inbox.json`` плюс карточка в очередь руководителя.
"""
from __future__ import annotations

import re as _re

try:                                     # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError
except ImportError:                      # pragma: no cover
    from app_common import err           # type: ignore
    from http_util import ApiError       # type: ignore

ROOM = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"
ITEM = r"(?P<item_id>[a-z]{2}_[a-z0-9]{4,32})"

ROUTES = [
    ("GET", r"/api/rooms/" + ROOM + r"/inbox", "h_inbox_get"),
    ("POST", r"/api/rooms/" + ROOM + r"/inbox", "h_inbox_post"),
    ("POST", r"/api/rooms/" + ROOM + r"/inbox/" + ITEM + r"/actions", "h_inbox_action"),
]


def _first(query: dict, name: str) -> str:
    try:
        values = (query or {}).get(name) or []
    except AttributeError:               # pragma: no cover - query уже строка
        return ""
    if isinstance(values, str):
        return values
    for value in values:
        return "" if value is None else str(value)
    return ""


class InboxRoutes:
    """Обработчики входящих (``self`` — экземпляр ``server.app.App``)."""

    def _inbox_mod(self):
        return self.mod("inbox")

    def _raise_inbox(self, exc):
        raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc

    # --- GET /api/rooms/{id}/inbox ---------------------------------------
    def h_inbox_get(self, query, body, room_id):
        room = self._room(room_id)
        mod = self._inbox_mod()
        try:
            payload = mod.items(self.cfg, str(room.get("id")), _first(query, "status").strip() or "active")
        except mod.InboxError as exc:
            self._raise_inbox(exc)
        except Exception as exc:          # noqa: BLE001 — чтение не должно ронять сервер
            self.log.error("входящие: чтение", exc)
            raise err(500, "Не удалось прочитать входящие комнаты") from exc
        return 200, payload, {"Cache-Control": "no-store"}

    # --- POST /api/rooms/{id}/inbox --------------------------------------
    def h_inbox_post(self, query, body, room_id):
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        if "text" not in data:
            raise err(400, "Нужен текст записи: text", missing=["text"])
        mod = self._inbox_mod()
        try:
            item = mod.add(self.cfg, str(room.get("id")), data.get("text"),
                           data.get("source") or "manual")
            listing = mod.items(self.cfg, str(room.get("id")), "active")
        except mod.InboxError as exc:
            self._raise_inbox(exc)
        except Exception as exc:          # noqa: BLE001
            self.log.error("входящие: запись", exc)
            raise err(500, "Не удалось сохранить запись во входящие") from exc
        return 201, {"ok": True, "item": item, "counts": listing["counts"],
                     "generated_at": listing["generated_at"]}, {"Cache-Control": "no-store"}

    # --- POST /api/rooms/{id}/inbox/{item}/actions -----------------------
    def h_inbox_action(self, query, body, room_id, item_id):
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        action = str(data.get("action") or "").strip()
        if not action:
            raise err(400, "Нужно действие: action", missing=["action"])
        mod = self._inbox_mod()
        try:
            payload = mod.act(self.cfg, str(room.get("id")), item_id, action, data)
        except mod.InboxError as exc:
            self._raise_inbox(exc)
        except Exception as exc:          # noqa: BLE001
            self.log.error("входящие: действие", exc)
            raise err(500, "Не удалось разобрать запись входящих") from exc
        return 200, payload, {"Cache-Control": "no-store"}


def install_inbox_routes(app_cls, routes: list) -> None:
    """Подключить входящие к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(InboxRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
