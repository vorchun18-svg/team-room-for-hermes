"""HTTP-маршруты защиты выбора модели на доске (W29-P10, docs/API.md).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_models_trigger import install_model_trigger_routes
    install_model_trigger_routes(App, ROUTES)

* ``GET  /api/rooms/{r}/model-trigger`` — состояние (только чтение, ничего не ставит);
* ``POST /api/rooms/{r}/model-trigger`` — ``{"action": "install" | "uninstall" | "sync"}``.

Установка и удаление — ТОЛЬКО по явному действию человека: без ``action`` ручка отвечает
400 и ничего не делает. Старт сервера и тики сторожа сюда не ходят.
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
ROUTES = [
    ("GET", r"/api/rooms/" + ROOM_RE + r"/model-trigger", "h_model_trigger_get"),
    ("POST", r"/api/rooms/" + ROOM_RE + r"/model-trigger", "h_model_trigger_post"),
]
ACTIONS = ("install", "uninstall", "sync")
_STATUS = {"bad_request": 400, "not_found": 404, "unavailable": 503, "bad_schema": 409,
           "conflict": 409}


class ModelTriggerRoutes:
    """Обработчики защиты выбора модели (``self`` — экземпляр ``server.app.App``)."""

    def _trigger_mod(self):
        return self.mod("model_trigger")

    def _trigger_db(self, room: dict):
        board = str(room.get("board") or self.board or "").strip()
        return self.cfg.kanban_db(board), board

    def _trigger_room(self, room_id: str) -> dict:
        with self.engine():
            room = self._room(room_id)
        if room.get("archived"):
            raise err(409, "Комната в архиве — защита не меняется", room=room_id)
        return room

    def _trigger_error(self, exc: BaseException) -> ApiError:
        code = str(getattr(exc, "code", "bad_request"))
        message = getattr(exc, "message", None) or str(exc) or "Не удалось выполнить действие"
        detail = getattr(exc, "detail", None)
        return ApiError(_STATUS.get(code, 400), message, code=code,
                        detail=detail if isinstance(detail, dict) else {})

    def _trigger_payload(self, room: dict, state: dict) -> dict:
        """Наружу — понятное состояние без сырых колонок схемы."""
        return {"ok": bool(state.get("healthy")),
                "room": str(room.get("id") or ""),
                "board": str(room.get("board") or self.board or ""),
                "installed": bool(state.get("installed")),
                "healthy": bool(state.get("healthy")),
                "table": bool(state.get("table")),
                "triggers": dict(state.get("triggers") or {}),
                "rows": int(state.get("rows") or 0),
                "version": str(state.get("version") or ""),
                "installed_at": int(state.get("installed_at") or 0),
                "problems": list(state.get("problems") or []),
                "text": str(state.get("text") or "")}

    def h_model_trigger_get(self, query, body, room_id):
        """``GET /api/rooms/{id}/model-trigger`` — установлена ли защита (только чтение)."""
        room = self._trigger_room(room_id)
        db, _board = self._trigger_db(room)
        mod = self._trigger_mod()
        try:
            state = mod.status(db)
        except mod.TriggerError as exc:
            raise self._trigger_error(exc) from exc
        except Exception as exc:                     # noqa: BLE001 - чтение не должно давать 500
            self.log.error("model-trigger status %s" % room_id, exc)
            raise ApiError(503, "Состояние защиты не прочитано: %s" % str(exc)[:160],
                           code="unavailable") from exc
        payload = self._trigger_payload(room, state)
        payload["rows_expected"] = len(mod.rows_of_room(room) or {})
        return 200, payload, {"Cache-Control": "no-store"}

    def h_model_trigger_post(self, query, body, room_id):
        """``POST /api/rooms/{id}/model-trigger`` — установить / снять / обновить таблицу.

        Тело: ``{"action": "install"|"uninstall"|"sync", "purge": false}``. Без ``action``
        ничего не меняется: 400 с подсказкой, что именно принимается.
        """
        room = self._trigger_room(room_id)
        data = body if isinstance(body, dict) else {}
        action = str(data.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise err(400, "Укажите действие защиты модели", field="action",
                      allowed=list(ACTIONS))
        db, board = self._trigger_db(room)
        mod = self._trigger_mod()
        try:
            if action == "install":
                state = mod.install(db, rows=mod.rows_of_room(room),
                                    tenant=str(room.get("tenant") or ""),
                                    app_version=str(getattr(self.cfg, "version", "") or ""))
            elif action == "uninstall":
                state = mod.uninstall(db, drop_table=bool(data.get("purge")))
            else:
                state = mod.status(db)
                synced = mod.sync(db, str(room.get("tenant") or ""), mod.rows_of_room(room))
                state["synced"] = int(synced.get("synced") or 0)
                state["skipped"] = str(synced.get("skipped") or "")
        except mod.TriggerError as exc:
            raise self._trigger_error(exc) from exc
        except Exception as exc:                     # noqa: BLE001 - отказ, а не 500
            self.log.error("model-trigger %s %s" % (action, room_id), exc)
            raise ApiError(400, "Не удалось выполнить действие: %s" % str(exc)[:200],
                           code="bad_request") from exc
        payload = self._trigger_payload(room, state)
        payload["action"] = action
        payload["backup"] = str(state.get("backup") or "")
        if "synced" in state:
            payload["synced"] = int(state.get("synced") or 0)
            payload["skipped"] = str(state.get("skipped") or "")
        if action == "install":
            payload["ok"] = bool(state.get("healthy"))
        return 200, payload, {"Cache-Control": "no-store"}


def install_model_trigger_routes(app_cls, routes: list) -> None:
    """Подключить защиту выбора модели к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(ModelTriggerRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
