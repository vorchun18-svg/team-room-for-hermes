"""Экран «Сегодня»: маршруты `GET /api/today` и `POST /api/today/seen` (W24 §3.1).

Здесь только HTTP-слой: собрать настоящие источники (``LiveSources``), отдать
ответ из короткого кэша и записать отметку захода. Сами правила «что ждёт
человека» живут в ``server/today.py`` и проверяются без сервера.

Источники — существующее чтение, ничего нового:

* ``registry.list_requests`` — запросы комнаты (через ``App._records``);
* ``ingest.backlog_view`` — пункты «От вас нужно» (через ``App._backlog``, с
  наложением ответов);
* ``engine_read.today_states`` — состояние корней на доске одним-двумя запросами;
* ``approvals.collect`` — блокировки, только у корней со статусом ``needs_you``
  (не больше :data:`APPROVAL_SCAN_MAX` на комнату: экран не должен перечитывать
  сессии всех участников);
* ``plan.load`` — черновик карты проекта.

Ответ кэшируется на ``today.CACHE_TTL_S`` (≤ 10 с): экран обновляется раз в
20 секунд, а «Кэш ≤ 10 с, ответ ≤ 200 мс» — требование контракта.

Подключение — одним блоком в ``server/app.py``::

    from server.routes_today import install_today_routes
    install_today_routes(App, ROUTES)
"""
from __future__ import annotations

import re as _re
import time

try:  # пакетный и «плоский» импорт
    from . import today as today_mod
    from .app_common import err
except ImportError:  # pragma: no cover
    import today as today_mod  # type: ignore
    from app_common import err  # type: ignore

#: Сколько корней со статусом «ждёт вас» просматривать на предмет блокировок.
#: Блокировка — редкое событие, а чтение сессий участников дорогое: ограничение
#: держит ответ быстрым на больших комнатах (см. отчёт N2, замер).
APPROVAL_SCAN_MAX = 8

ROUTES = [
    ("GET", r"/api/today", "h_today"),
    ("POST", r"/api/today/seen", "h_today_seen"),
]


class LiveSources(today_mod.Sources):
    """Настоящие источники экрана: комнаты, реестр, бэклог, доска, разрешения.

    Читает каждый источник ровно один раз на комнату за запрос (``_memo``):
    ``build`` спрашивает состояние корней и разрешения в разных местах, а диск и
    доску дважды за один ответ трогать незачем.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.cfg = app.cfg
        self._memo: dict = {}
        self._states: dict = {}

    # --- служебное ---
    def _once(self, key: tuple, build):
        if key in self._memo:
            return self._memo[key]
        value = build()
        self._memo[key] = value
        return value

    def _room_id(self, room) -> str:
        return str((room or {}).get("id") or "")

    # --- источники ---
    def rooms(self) -> list[dict]:
        return [room for room in self.app._rooms(include_archived=False)
                if isinstance(room, dict) and room.get("id") and not room.get("archived")]

    def requests(self, room: dict) -> list[dict]:
        room_id = self._room_id(room)
        return self._once(("requests", room_id),
                          lambda: self.app._records(room_id, quiet=True) or [])

    def states(self, room: dict) -> dict:
        room_id = self._room_id(room)
        if room_id in self._states:
            return self._states[room_id]
        records = self.requests(room)
        root_ids = [str(rec.get("root_id")) for rec in records if rec.get("root_id")]
        board = room.get("board") or getattr(self.app, "board", None)
        result: dict = {}
        if root_ids:
            found = self.app.call("engine_read", "today_states", ((self.cfg, board, root_ids), {}))
            result = found if isinstance(found, dict) else {}
        self._states[room_id] = result
        return result

    def backlog(self, room: dict) -> list[dict]:
        room_id = self._room_id(room)
        view = self._once(("backlog", room_id), lambda: self.app._backlog(room_id))
        items = (view or {}).get("items") if isinstance(view, dict) else view
        return [item for item in (items or []) if isinstance(item, dict)]

    def approvals(self, room: dict) -> list[dict]:
        """Блокировки, которые правда ждут человека: только «можно разрешить» (W26-D1).

        W26-D1 (T1): недоговорная блокировка (`grantable:false`/execute_code) и жёсткий
        запрет (`floor`) в «Сегодня» не попадают — у них нет решения, которое человек
        может нажать, поэтому это не «ждёт вас», а статус. Закрытые автозакрытием
        (карточка завершилась) сюда тоже не идут.
        """
        room_id = self._room_id(room)

        def build() -> list[dict]:
            try:
                mod = self.app.mod("approvals")
            except Exception:                      # noqa: BLE001 — модуль может быть не готов
                return []
            states = self.states(room)
            roots = [str(root_id) for root_id, state in states.items()
                     if str((state or {}).get("status") or "") == "needs_you"]
            roots.sort()
            out: list[dict] = []
            for root_id in roots[:APPROVAL_SCAN_MAX]:
                try:
                    found = mod.collect(self.cfg, room, root_id)
                    waiting = mod.pending(found)   # состояние pending + есть действие
                except Exception:                  # noqa: BLE001 — блокировки вторичны
                    continue
                for item in waiting:
                    out.append({"request_id": root_id,
                                "profile": item.get("profile"),
                                "member_title": item.get("member_title"),
                                "description": item.get("description") or item.get("note") or "",
                                "at": item.get("at"),
                                "state": item.get("state"),
                                "mode": item.get("mode"),
                                "actionable": bool(item.get("actions")),
                                "closed": bool(item.get("closed_reason"))})
            return out

        return self._once(("approvals", room_id), build)

    def plan(self, room: dict) -> dict | None:
        room_id = self._room_id(room)
        return self._once(("plan", room_id),
                          lambda: self.app.call("plan", "load", ((self.cfg, room_id), {})))

    def stalled(self, room: dict) -> list[dict]:
        """Карточки без признаков жизни — из состояния сторожа зависаний (W25-M2).

        Читается только файл `data/state/<room>/watchdog.json`: доска в «Сегодня»
        не трогается, экран не ждёт sqlite.
        """
        room_id = self._room_id(room)

        def build() -> list[dict]:
            try:
                mod = self.app.mod("watchdog")
            except Exception:                      # noqa: BLE001 — модуль может быть не готов
                return []
            try:
                return mod.stalled(self.cfg, room) or []
            except Exception:                      # noqa: BLE001 — сторож вторичен
                return []

        return self._once(("stalled", room_id), build)


class TodayRoutes:
    """Обработчики экрана «Сегодня» (``self`` — экземпляр ``server.app.App``)."""

    def _today_cache(self) -> dict:
        cache = getattr(self, "__today_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "__today_cache", cache)
        return cache

    def h_today(self, query, body):
        """`GET /api/today` — что ждёт человека по всем комнатам (контракт §3.1)."""
        mod = self.mod("today")
        now = int(time.time())
        last = mod.last_visit(self.cfg)
        cache = self._today_cache()
        started = time.monotonic()
        hit = cache.get(last)
        if hit and started - hit[0] < mod.CACHE_TTL_S:
            return 200, hit[1], {"Cache-Control": "no-store"}
        with self.engine():
            payload = mod.build(LiveSources(self), last_visit_at=last, now=now)
        spent = time.monotonic() - started
        if spent > 0.2:                     # контракт: ответ ≤ 200 мс — честно предупреждаем
            self.log.warning("сегодня: сборка ответа %.0f мс" % (spent * 1000))
        cache.clear()
        cache[last] = (started, payload)
        return 200, payload, {"Cache-Control": "no-store"}

    def h_today_seen(self, query, body):
        """`POST /api/today/seen` — отметить заход; идемпотентно, не чаще раза в 60 с."""
        mod = self.mod("today")
        with self.engine():
            result = mod.mark_seen(self.cfg)
        if not result.get("ok"):
            raise err(503, result.get("warning") or "Не удалось отметить заход",
                      code="state_unavailable")
        self._today_cache().clear()         # «изменилось» считается от новой отметки
        return 200, result, {"Cache-Control": "no-store"}


def install_today_routes(app_cls, routes: list) -> None:
    """Подключить экран «Сегодня» к ``App``: методы + строки таблицы маршрутов."""
    for name, value in vars(TodayRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
