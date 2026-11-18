"""HTTP «Дела» (WAVE26 §1–2, пакет D5; docs/API.md §W26-DELO).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_delo import install_delo_routes
    install_delo_routes(App, ROUTES)

Маршруты:

* ``GET  /api/rooms/{id}/delos`` — дела комнаты, ``{items:[Delo…], generated_at}``;
* ``POST /api/rooms/{id}/delos`` — открыть дело (из запроса ``request_id`` или вручную);
* ``GET  /api/delos?room=<id|all>`` — дела всех комнат (бейдж в меню);
* ``GET  /api/delos/{id}`` — одно дело;
* ``POST /api/delos/{id}/actions`` — действие (15 имён из §1).

Откуда «ждёт вашего решения»: ``DeloSources`` спрашивает существующие модули —
бэклог комнаты (``App._backlog``: вопросы, решения, шаги) и разрешения
(``approvals.collect`` для корней «ждёт вас», как на экране «Сегодня»). Своих
правил «что ждёт человека» здесь нет. Действия по пункту (approve/reject/answer)
уходят в штатный обработчик запроса (``App.h_request_actions``) — то есть в тот же
механизм, что и кнопки на странице запроса.

Наружу ничего не отправляется: ``mark_sent`` только меняет состояние исходящего.
"""
from __future__ import annotations

import re as _re
import time

try:                                     # пакетный и «плоский» импорт
    from . import delo as delo_mod
    from .app_common import err
    from .http_util import ApiError
except ImportError:                      # pragma: no cover
    import delo as delo_mod              # type: ignore
    from app_common import err           # type: ignore
    from http_util import ApiError       # type: ignore

ROOM_PATTERN = r"[a-z][a-z0-9_-]{1,31}"
DELO_PATTERN = r"dl_[A-Za-z0-9]{2,24}"

ROUTES = [
    ("GET", r"/api/rooms/(?P<room_id>%s)/delos" % ROOM_PATTERN, "h_room_delos"),
    ("POST", r"/api/rooms/(?P<room_id>%s)/delos" % ROOM_PATTERN, "h_room_delos_create"),
    ("GET", r"/api/delos", "h_delos_list"),
    ("GET", r"/api/delos/(?P<delo_id>%s)" % DELO_PATTERN, "h_delo"),
    ("POST", r"/api/delos/(?P<delo_id>%s)/actions" % DELO_PATTERN, "h_delo_action"),
]

#: Общий режим «все комнаты» (как у дежурств).
ROOM_ALL = "all"
#: Сколько корней запросов просматривать на разрешения за комнату (как в «Сегодня»).
APPROVAL_SCAN_MAX = 8
#: Владелец пункта «ждёт человека» в бэклоге (единственное такое имя в данных).
HUMAN = "mikhail"
WAITING_STATUSES = ("open", "asked")
#: Виды пунктов бэклога, которые ждут человека.
POINT_KINDS = ("open_question", "decision", "next_step")


def _first(query: dict, name: str) -> str:
    """Первое значение параметра запроса (``?room=…``) как строка."""
    try:
        values = (query or {}).get(name) or []
    except AttributeError:               # pragma: no cover - query уже строка
        return ""
    if isinstance(values, str):
        return values
    for value in values:
        return "" if value is None else str(value)
    return ""


def _local(item_id) -> str:
    """``<rid>:q_1`` → ``q_1``: механизм запроса знает только локальные id."""
    return str(item_id or "").split(":", 1)[-1].strip()


class DeloSources(delo_mod.Sources):
    """Настоящие источники пунктов «ждёт вашего решения» для дела.

    Каждый источник читается один раз на комнату за запрос (``_memo``): список дел
    спрашивает пункты у каждого дела, а бэклог и разрешения — общие для комнаты.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.cfg = app.cfg
        self._memo: dict = {}

    def _once(self, key: tuple, build):
        if key in self._memo:
            return self._memo[key]
        value = build()
        self._memo[key] = value
        return value

    # --- источники ---
    def records(self, room_id: str) -> list[dict]:
        def build():
            try:
                return [rec for rec in (self.app._records(room_id, quiet=True) or [])
                        if isinstance(rec, dict) and rec.get("root_id")]
            except Exception:            # noqa: BLE001 — реестр вторичен для чтения дела
                return []
        return self._once(("records", room_id), build)

    def request(self, room_id: str, request_id: str) -> dict | None:
        for rec in self.records(room_id):
            if str(rec.get("root_id")) == str(request_id):
                return {"id": str(rec.get("root_id")), "title": str(rec.get("title") or "")}
        return None

    def backlog(self, room_id: str) -> list[dict]:
        def build():
            try:
                view = self.app._backlog(room_id)
            except Exception:            # noqa: BLE001 — бэклог вторичен
                return []
            items = (view or {}).get("items") if isinstance(view, dict) else view
            return [item for item in (items or []) if isinstance(item, dict)]
        return self._once(("backlog", room_id), build)

    def approvals(self, room_id: str, request_id: str) -> list[dict]:
        """Блокировки запроса — только если корень «ждёт вас» (как на «Сегодня»)."""

        def build():
            try:
                mod = self.app.mod("approvals")
            except Exception:            # noqa: BLE001 — модуль может быть не готов
                return []
            try:
                room = self.app._room(room_id)
                board = room.get("board") or getattr(self.app, "board", None)
                states = self.app.call("engine_read", "today_states",
                                       ((self.cfg, board, [request_id]), {}))
                state = (states or {}).get(str(request_id)) if isinstance(states, dict) else {}
                if str((state or {}).get("status") or "") != "needs_you":
                    return []
                found = mod.collect(self.cfg, room, request_id)
                return [item for item in mod.pending(found) if isinstance(item, dict)]
            except Exception:            # noqa: BLE001 — блокировки вторичны
                return []

        return self._once(("approvals", room_id, request_id), build)

    def needs(self, room_id: str, request_id: str) -> list[dict]:
        """Пункты запроса в общем виде (вопросы, решения, шаги, разрешения)."""
        out: list[dict] = []
        for item in self.backlog(room_id):
            if str(item.get("source_request") or "") != str(request_id):
                continue
            if str(item.get("status") or "open") not in WAITING_STATUSES:
                continue
            kind = str(item.get("kind") or "")
            if kind not in POINT_KINDS:
                continue
            if str(item.get("owner") or "") != HUMAN:
                continue
            point = {"id": _local(item.get("id")), "kind": kind,
                     "text": item.get("text"), "at": item.get("created_at") or item.get("at"),
                     "options": item.get("options")}
            if kind == "open_question":
                point["question_id"] = _local(item.get("id"))
            elif kind == "decision":
                point["decision_id"] = _local(item.get("id"))
            else:
                point["step_id"] = _local(item.get("id"))
            out.append(point)
        for item in self.approvals(room_id, request_id):
            if item.get("grantable") is False:
                continue                      # «нельзя разрешить штатно» — не ждёт решения
            label = str(item.get("member_title") or item.get("profile") or "").strip()
            description = str(item.get("description") or "").strip()
            text = ("%s просит разрешить: %s" % (label, description)).strip(" :") \
                if label else description
            out.append({"id": _local(item.get("id")), "kind": "approval", "text": text,
                        "at": item.get("at"), "approval_id": _local(item.get("id"))})
        return out


class DeloRoutes:
    """Обработчики дел (``self`` — экземпляр ``server.app.App``)."""

    def _delo_mod(self):
        return self.mod("delo")

    def _delo_sources(self):
        return DeloSources(self)

    def _delo_call(self, func, *args, **kw):
        """Вызвать модуль дел и перевести его ошибку в HTTP-ответ по-русски."""
        mod = self._delo_mod()
        try:
            return func(*args, **kw)
        except mod.DeloError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except ApiError:
            raise
        except Exception as exc:             # noqa: BLE001 — сервер не должен падать
            self.log.error("дела: %s" % getattr(func, "__name__", "вызов"), exc)
            raise err(500, "Не удалось выполнить действие с делом") from exc

    # --- чтение ---------------------------------------------------------
    def h_room_delos(self, query, body, room_id):
        room = self._room(room_id)
        payload = self._delo_call(self._delo_mod().list_delos, self.cfg, room["id"],
                                  sources=self._delo_sources())
        return 200, payload, {"Cache-Control": "no-store"}

    def h_delos_list(self, query, body):
        room = _first(query, "room").strip() or ROOM_ALL
        mod = self._delo_mod()
        if room != ROOM_ALL:
            known = self._room(room)
            return 200, self._delo_call(mod.list_delos, self.cfg, known["id"],
                                        sources=self._delo_sources()), {"Cache-Control": "no-store"}
        items: list[dict] = []
        sources = self._delo_sources()
        for known in self._rooms(include_archived=False):
            try:
                payload = mod.list_delos(self.cfg, str(known.get("id")), sources=sources)
            except mod.DeloError as exc:     # noqa: PERF203 — одна битая комната не ломает список
                if int(exc.http) < 500:
                    continue
                raise ApiError(int(exc.http), exc.message, code=exc.code,
                               detail=exc.detail) from exc
            items.extend(payload.get("items") or [])
        items.sort(key=lambda item: (-int(item.get("updated_at") or 0), str(item.get("id"))))
        return 200, {"items": items, "generated_at": int(time.time())}, \
            {"Cache-Control": "no-store"}

    def h_delo(self, query, body, delo_id):
        payload = self._delo_call(self._delo_mod().get, self.cfg, delo_id,
                                  sources=self._delo_sources())
        return 200, payload, {"Cache-Control": "no-store"}

    # --- запись ---------------------------------------------------------
    def h_room_delos_create(self, query, body, room_id):
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        payload = self._delo_call(self._delo_mod().create, self.cfg, room, data,
                                  sources=self._delo_sources())
        return 201, payload, {"Cache-Control": "no-store"}

    def h_delo_action(self, query, body, delo_id):
        mod = self._delo_mod()
        room_id, _delo = self._delo_call(mod.find, self.cfg, delo_id)
        data = body if isinstance(body, dict) else {}
        payload = self._delo_call(mod.act, self.cfg, room_id, delo_id, data,
                                  sources=self._delo_sources(),
                                  delegate=self._delo_delegate)
        return 200, payload, {"Cache-Control": "no-store"}

    def _delo_delegate(self, request_id: str, body: dict) -> dict:
        """Действие по пункту — штатным обработчиком запроса (вопрос/решение/разрешение).

        Ошибки обработчика (409 «уже не ждёт», 400 «нет текста») уходят наверх как
        есть: дело не должно записывать «сделано», если запрос отказал.
        """
        status, payload, _headers = self.h_request_actions({}, dict(body or {}), request_id)
        if int(status) >= 400:               # pragma: no cover — err() уже бросил бы
            raise err(int(status), "Запрос не принял действие")
        return payload if isinstance(payload, dict) else {"ok": True}


def install_delo_routes(app_cls, routes: list) -> None:
    """Подключить дела к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(DeloRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
