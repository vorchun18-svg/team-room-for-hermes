"""HTTP-маршруты «Карты проекта» (WAVE 9, §4) — миксин для ``App``.

Подключение — одним блоком в ``server/app.py``::

    from server.routes_plan import install_plan_routes
    install_plan_routes(App, ROUTES)

Каждый хендлер получает комнату через ``self._room`` и работает через модуль
``server.plan``. Ошибки — по-русски, 4xx (карта не должна отдавать 500 на
нормальной ошибке пользователя).
"""
from __future__ import annotations

import re as _re

try:  # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    from app_common import err  # type: ignore
    from http_util import ApiError  # type: ignore

ROUTES = [
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/plan", "h_plan_get"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/plan/approve", "h_plan_approve"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/plan/rebuild", "h_plan_rebuild"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/plan/streams", "h_plan_stream_add"),
    ("PATCH", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/plan/streams/(?P<stream_id>[A-Za-z0-9_-]{1,16})",
     "h_plan_stream_patch"),
    ("DELETE", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/plan/streams/(?P<stream_id>[A-Za-z0-9_-]{1,16})",
     "h_plan_stream_delete"),
]


class PlanRoutes:
    """Обработчики карты проекта (``self`` — экземпляр ``server.app.App``)."""

    # --- помощники ---------------------------------------------------------
    def _plan_room(self, room_id: str) -> dict:
        with self.engine():                       # ошибка реестра → 404/503, не 500
            room = self._room(room_id)
        if room.get("archived"):
            raise err(409, "Комната в архиве — карта проекта недоступна", room=room_id)
        return room

    def _plan_call(self, room_obj: dict, attr: str, *args, **kwargs):
        """Вызвать ``plan.<attr>`` с комнатой и её id (4xx-ошибки — через ``engine``)."""
        room_id = room_obj.get("id")
        with self.engine():
            return self.call("plan", attr, ((self.cfg, room_id) + args, kwargs),
                             ((self.cfg, room_id) + args, {}))

    def _plan_payload(self, room: dict, result) -> dict:
        result = result if isinstance(result, dict) else {}
        plan = result.get("plan")
        if plan is None:
            try:
                plan = self.call("plan", "load", ((self.cfg, room.get("id")), {}))
            except Exception:  # noqa: BLE001 - карта вторична для ответа
                plan = None
        try:                      # русские подписи исполнителей (id остаётся в owner)
            if isinstance(plan, dict):
                plan = self.call("plan", "fill_labels", ((plan, room), {}))
        except Exception:  # noqa: BLE001 - подпись вторична, интерфейс умеет и без неё
            pass
        payload = {"plan": plan}
        for key in ("started", "queued", "warnings", "applied", "stream", "comment", "skipped", "auto"):
            if key in result:
                payload[key] = result[key]
        return payload

    # --- чтение ------------------------------------------------------------
    def h_plan_get(self, query, body, room_id):
        """``GET /api/rooms/{r}/plan`` → ``{plan}`` или ``{plan: null}``."""
        self._plan_room(room_id)
        plan = None
        try:
            plan = self.call("plan", "load", ((self.cfg, room_id), {}))
        except ApiError as exc:
            if exc.status == 501:
                return 200, {"plan": None}, {}
            raise
        except Exception as exc:  # noqa: BLE001 - битый файл: карты просто нет
            self.log.warning("plan %s: %s" % (room_id, exc))
            plan = None
        mode = None
        try:  # сводка для шапки: сколько идёт и в каком состоянии карта
            mode = self.call("plan", "streams_summary", ((self.cfg, room_id), {}))
        except Exception:  # noqa: BLE001
            mode = None
        try:  # подписи исполнителей по-русски (owner остаётся техническим id)
            if isinstance(plan, dict):
                plan = self.call("plan", "fill_labels", ((plan, self._plan_room(room_id)), {}))
        except Exception:  # noqa: BLE001 - подпись не должна ломать страницу карты
            pass
        return 200, {"plan": plan, "summary": mode}, {}

    # --- запуск ------------------------------------------------------------
    def h_plan_approve(self, query, body, room_id):
        """``POST /api/rooms/{r}/plan/approve`` ``{streams: [...]|"all"}`` → ``{plan, started}``."""
        room = self._plan_room(room_id)
        streams = (body or {}).get("streams", "all")
        if streams is None:
            streams = "all"
        if not isinstance(streams, (str, list)):
            raise err(400, "streams: список id или «all»", field="streams")
        if isinstance(streams, list) and not all(isinstance(s, str) and s.strip() for s in streams):
            raise err(400, "streams: в списке должны быть id направлений", field="streams")
        res = self._plan_call(room, "approve", streams, room=room)
        return 200, self._plan_payload(room, res), {}

    # --- правка направлений ------------------------------------------------
    def h_plan_stream_patch(self, query, body, room_id, stream_id):
        """``PATCH /api/rooms/{r}/plan/streams/{id}`` — ручная правка Михаила."""
        room = self._plan_room(room_id)
        fields = dict(body or {})
        if not fields:
            raise err(400, "Нечего менять: передайте поля направления", field="fields")
        if "status" in fields and not isinstance(fields["status"], str):
            raise err(400, "status: строка", field="status")
        if "members" in fields and not isinstance(fields["members"], list):
            raise err(400, "members: список исполнителей", field="members")
        res = self._plan_call(room, "update_stream", stream_id, fields, room=room)
        return 200, self._plan_payload(room, res), {}

    def h_plan_stream_add(self, query, body, room_id):
        """``POST /api/rooms/{r}/plan/streams`` — добавить направление вручную."""
        room = self._plan_room(room_id)
        data = dict(body or {})
        if not str(data.get("title") or "").strip():
            raise err(400, "Нужно название направления", field="title")
        res = self._plan_call(room, "add_stream", data, room=room)
        return 201, self._plan_payload(room, res), {}

    def h_plan_stream_delete(self, query, body, room_id, stream_id):
        """``DELETE /api/rooms/{r}/plan/streams/{id}`` — «Отложить» (не удаление)."""
        room = self._plan_room(room_id)
        reason = str((body or {}).get("reason") or "").strip()
        res = self._plan_call(room, "drop_stream", stream_id, reason, room=room)
        return 200, self._plan_payload(room, res), {}

    # --- карта из запросов (A7) --------------------------------------------
    def h_plan_rebuild(self, query, body, room_id):
        """``POST /api/rooms/{r}/plan/rebuild`` → ``{request}``."""
        room = self._plan_room(room_id)
        res = self._plan_call(room, "rebuild", room=room)
        payload = dict(res) if isinstance(res, dict) else {}
        payload.setdefault("ok", True)
        payload["dispatcher"] = self._dispatcher_short()
        return 201, payload, {}


def install_plan_routes(app_cls, routes: list) -> None:
    """Подключить карту проекта к ``App``: методы миксина + строки таблицы маршрутов.

    ``routes`` — готовая таблица ``ROUTES`` приложения (метод, скомпилированный
    шаблон, имя хендлера); дописываем в неё свои строки, не создавая второй таблицы.
    Копируются и хендлеры, и помощники ``PlanRoutes`` (``_plan_room`` и т. п.).
    """
    for name, value in vars(PlanRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
