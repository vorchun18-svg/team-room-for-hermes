"""HTTP ручки «Результаты агентов» (W30-E, docs/API.md §AGENT-RESULTS).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_agent_results import install_agent_results_routes
    install_agent_results_routes(App, ROUTES)

Что здесь:

* ``GET /api/requests/{id}/agent-results`` → ``{"agents": [...], "counts": {...}}``
  — кто из исполнителей что сделал, вывод, файлы с путями, что осталось;
* ``_agent_results_view(room, detail)`` — тот же блок кладётся в деталь задачи
  ключом ``agent_results`` (странице не нужен второй запрос).

Сборка — ``server/agent_results.py`` из дерева карточек и их метаданных; вызовов
моделей нет, записи нет. Сбой сборки не роняет страницу: блок остаётся пустым.
"""
from __future__ import annotations

import re as _re

try:                                     # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError
except ImportError:                      # pragma: no cover
    from app_common import err           # type: ignore
    from http_util import ApiError       # type: ignore

ROUTES = [
    ("GET", r"/api/requests/(?P<rid>t_[0-9a-f]{8})/agent-results", "h_agent_results"),
]


class AgentResultsRoutes:
    """Хендлеры блока «Результаты агентов»."""

    def _agent_results_mod(self):
        return self.mod("agent_results")

    def h_agent_results(self, query, body, rid):
        """GET /api/requests/{id}/agent-results — результаты исполнителей задачи."""
        room, rec = self._find_record(rid)
        try:
            detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                                  rec.get("answers"))
        except ApiError:
            raise
        except Exception as exc:         # noqa: BLE001 — движок недоступен
            self.log.error("результаты агентов %s" % rid, exc)
            raise ApiError(502, "Движок не отдал дерево задачи — результаты недоступны",
                           detail={"rid": rid}) from exc
        payload = self._agent_results_mod().attach(detail, room=room)
        return 200, payload, {"Cache-Control": "no-cache"}

    def _agent_results_view(self, room, detail) -> None:
        """W30-E: ключ ``agent_results`` в детали задачи (одно место сборки)."""
        if not isinstance(detail, dict) or not self._has_mod("agent_results"):
            return
        try:
            self.mod("agent_results").attach(detail, room=room)
        except Exception as exc:         # noqa: BLE001 — блок вторичен для страницы
            self.log.warning("результаты агентов %s: %s" % (detail.get("id"), exc))
            detail.setdefault("agent_results", {"agents": [], "counts": {}})


def install_agent_results_routes(app_cls, routes: list) -> None:
    """Подключить блок к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(AgentResultsRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        if not hasattr(app_cls, name):
            setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
