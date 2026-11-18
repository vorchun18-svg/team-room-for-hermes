"""HTTP-маршрут «Хода работы» (WAVE20 §1.6, docs/API.md §W20-WORKLOG).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_worklog import install_worklog_routes
    install_worklog_routes(App, ROUTES)

Отдаёт ``GET /api/requests/{id}/worklog``: лента событий агентов запроса (рассуждения,
действия, результаты, события карточек, итог сторожа), живое состояние «кто сейчас
работает» и пометки «возможно завис». Всё — только чтение; режим ``off`` выключает чтение
полностью. Ошибки по-русски и 4xx: лента не имеет права отдавать 500.
"""
from __future__ import annotations

import re as _re
import time as _time

try:  # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    from app_common import err  # type: ignore
    from http_util import ApiError  # type: ignore

RID_RE = r"(?P<rid>t_[A-Za-z0-9_]+)"

ROUTES = [
    ("GET", r"/api/requests/" + RID_RE + r"/worklog", "h_worklog_get"),
]

_STATUS = {"bad_request": 400, "not_found": 404, "unavailable": 503}


def _first(query: dict, name: str) -> str:
    """Первое значение параметра запроса (``?since=…``) как строка."""
    try:
        values = (query or {}).get(name) or []
    except AttributeError:                   # pragma: no cover - query уже строка
        return ""
    if isinstance(values, str):
        return values
    for value in values:
        return "" if value is None else str(value)
    return ""


class WorklogRoutes:
    """Обработчик «Хода работы» (``self`` — экземпляр ``server.app.App``)."""

    def _worklog_mod(self):
        return self.mod("worklog")

    def h_worklog_get(self, query, body, rid):
        """``GET /api/requests/{id}/worklog?since=&level=&limit=`` — лента хода работы.

        ``since`` — курсор из предыдущего ответа (непрозрачная строка). ``level`` —
        ``thoughts`` (с рассуждениями) или ``steps`` (только действия); настройка комнаты
        ``brief`` всё равно оставляет ``steps``. Отсутствие ``state.db`` — не ошибка:
        ответ придёт пустым с предупреждением.
        """
        room, rec = self._find_record(rid)
        mod = self._worklog_mod()
        since = _first(query, "since")
        before = _first(query, "before")
        level = _first(query, "level")
        limit = _first(query, "limit")
        extra = rec.get("extra_cards") or []
        try:
            payload = mod.worklog(self.cfg, room, rid, since=since, before=before,
                                  level=level, limit=limit or mod.DEFAULT_LIMIT, extra=extra)
        except mod.WorklogError as exc:
            status = _STATUS.get(str(getattr(exc, "code", "")), 400)
            raise ApiError(status, getattr(exc, "message", None) or str(exc) or
                           "Не удалось прочитать ход работы",
                           code=str(getattr(exc, "code", "bad_request")),
                           detail=getattr(exc, "detail", None) or {}) from exc
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - лента не должна ронять страницу
            self.log.error("worklog %s: %s" % (rid, exc))
            raise err(503, "Ход работы сейчас недоступен — попробуйте ещё раз",
                      reason="worklog_unavailable") from exc
        payload.setdefault("generated_at", int(_time.time()))
        payload.setdefault("warnings", [])
        return 200, payload, {"Cache-Control": "no-store"}


def install_worklog_routes(app_cls, routes: list) -> None:
    """Подключить «Ход работы» к ``App``: методы миксина + строки таблицы маршрутов."""
    for name, value in vars(WorklogRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
