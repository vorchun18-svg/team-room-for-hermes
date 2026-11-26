"""HTTP-слой канбана задач комнаты (W30-D1): ``GET /api/rooms/{room}/tasks``.

Правила стадий, строки и счётчики — ``server/tasks_board.py``; принятие,
паспорт и заморозка — ``server/passports.py``. Здесь только сбор данных из уже
существующих чтений приложения:

* реестр комнаты (``self._records``) — какие задачи есть;
* доска (``engine_read.today_states``) — статус и признак принятия;
* сводка запроса (``self._summary``) — название, прогресс, подпись;
* пункты «ждёт вас» — ТОТ ЖЕ расчёт, что на странице запроса и на экране
  «Сегодня» (``routes_today.LiveSources.waiting``): число пунктов на доске и на
  странице обязано совпадать, второго счёта нет.

Подключение — одной строкой в ``server/app.py``::

    from server.routes_tasks import install_tasks_routes
    install_tasks_routes(App, ROUTES)
"""
from __future__ import annotations

import re as _re
import time

try:  # пакетный и «плоский» импорт
    from . import passports as passports_mod
    from . import tasks_board as board_mod
    from .app_common import err
except ImportError:  # pragma: no cover
    import passports as passports_mod        # type: ignore
    import tasks_board as board_mod          # type: ignore
    from app_common import err               # type: ignore

ROUTES = [
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/tasks", "h_room_tasks"),
]


class TasksRoutes:
    """Методы ``App`` для канбана задач комнаты."""

    def _tasks_waiting(self, room: dict) -> dict:
        """``{root_id: [пункты]}`` — один расчёт на комнату (страница = доска).

        Источник — ``routes_today.LiveSources.waiting``: он уже умеет дешёвый
        отбор (``engine_read.waiting_candidates``) и отдаёт только пункты с
        действием. Нет модуля/сбой — пустой словарь: доска покажет задачи без
        числа пунктов, но не упадёт.
        """
        try:
            from . import routes_today as today_routes
        except ImportError:  # pragma: no cover
            import routes_today as today_routes      # type: ignore
        try:
            found = today_routes.LiveSources(self).waiting(room)
        except Exception:  # noqa: BLE001 - пункты вторичны для доски
            return {}
        return found if isinstance(found, dict) else {}

    def _tasks_summary(self, room: dict, rec: dict) -> dict:
        """Сводка запроса; любой сбой чтения — «сводки нет», карточка остаётся."""
        root_id = str(rec.get("root_id") or "")
        if not root_id:
            return {}
        try:
            return self._summary(room, root_id, rec.get("extra_cards"),
                                 rec.get("critic"), rec.get("answers")) or {}
        except Exception:  # noqa: BLE001 - одна карточка не валит доску
            return {}

    def h_room_tasks(self, query, body, room_id):
        """``GET /api/rooms/{room}/tasks`` — доска задач комнаты (контракт §3)."""
        room_id = str(room_id or "")
        if not room_id:
            raise err(400, "Не передана комната", field="room")
        room = self._room(room_id)
        board = room.get("board") or self.board
        records = [rec for rec in self._records(room_id)
                   if isinstance(rec, dict) and rec.get("root_id")]
        root_ids = [str(rec["root_id"]) for rec in records]
        states: dict = {}
        if root_ids:
            try:
                found = self.call("engine_read", "today_states", ((self.cfg, board, root_ids), {}))
                states = found if isinstance(found, dict) else {}
            except Exception as exc:  # noqa: BLE001 - доска без статусов, но с задачами
                self.log.warning("доска задач %s: статусы недоступны: %s" % (room_id, exc))
                states = {}
        try:
            accepted = passports_mod.accepted_ids(self.cfg, room)
        except Exception as exc:  # noqa: BLE001 - отметка вторична
            self.log.warning("доска задач %s: принятые недоступны: %s" % (room_id, exc))
            accepted = set()
        waiting = self._tasks_waiting(room)
        rows = board_mod.build_rows(
            records, states=states, accepted=accepted, waiting=waiting,
            links_map=board_mod.links(self.cfg, room_id),
            summary_of=lambda rec: self._tasks_summary(room, rec))
        return 200, board_mod.payload(room_id, rows), {"Cache-Control": "no-store"}


def install_tasks_routes(app_cls, routes: list) -> None:
    """Подключить доску задач к ``App``: методы + строки таблицы маршрутов."""
    for name, value in vars(TasksRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
