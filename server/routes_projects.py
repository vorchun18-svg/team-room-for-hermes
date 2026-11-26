"""Предложение задач и проект «задача → подзадачи»: HTTP-слой (W30 §2, пакет B).

Здесь только маршруты и аккуратные ответы. Правила (разбор комментария руководителя,
решения человека, создание отдельных задач-корней, связь в `projects.json`) живут в
``server/projects.py``; текст правил для руководителя — в ``server/contract_project.py``.

Маршруты::

    GET  /api/requests/{rid}/proposal            — предложение (или null) и признак готовности
    POST /api/requests/{rid}/proposal/decision   — {branch_id|null, decision, text?}
    POST /api/requests/{rid}/proposal/resolve    — {action: accept_all|reject_all|custom|apply, text?}

Плюс два места в общих файлах (точечно, по одной строке):
* детали задачи получают ``proposal`` и ``project`` (`_project_view` вызывается из
  ``routes_read.h_request_get``);
* действие ``split_to_tasks`` («Разбить на задачи») — ветка в ``routes_write``,
  обработчик `_split_action` здесь.

Подключение — одним блоком в ``server/app.py``::

    from server.routes_projects import install_project_routes
    install_project_routes(App, ROUTES)
"""
from __future__ import annotations

import re as _re
import time

try:  # пакетный и «плоский» импорт
    from . import projects as projects_mod
    from .app_common import err
except ImportError:  # pragma: no cover
    import projects as projects_mod        # type: ignore
    from app_common import err             # type: ignore

_ROUTE_RID = r"t_[A-Za-z0-9_]+"

ROUTES = [
    ("GET", r"/api/requests/(?P<rid>" + _ROUTE_RID + r")/proposal", "h_proposal_get"),
    ("POST", r"/api/requests/(?P<rid>" + _ROUTE_RID + r")/proposal/decision", "h_proposal_decision"),
    ("POST", r"/api/requests/(?P<rid>" + _ROUTE_RID + r")/proposal/resolve", "h_proposal_resolve"),
]


def _fail(exc: "projects_mod.ProjectError") -> Exception:
    """Отказ домена → ошибка API с русским текстом и машинным кодом."""
    return err(exc.http, exc.message, exc.code, **exc.detail)


class ProjectRoutes:
    """Методы ``App`` для предложения задач и проекта."""

    # --- чтение ---------------------------------------------------------
    def h_proposal_get(self, query, body, rid):
        room, rec = self._find_record(rid)
        try:
            proposal = projects_mod.sync(self.cfg, room, rid)
        except projects_mod.ProjectError as exc:
            raise _fail(exc) from exc
        return 200, {"proposal": proposal, "ready": projects_mod.ready(proposal),
                     "root_id": rid, "at": int(time.time())}, {"Cache-Control": "no-store"}

    # --- решения человека ------------------------------------------------
    def h_proposal_decision(self, query, body, rid):
        body = body if isinstance(body, dict) else {}
        room, rec = self._find_record(rid)
        decision = body.get("decision")
        if not isinstance(decision, str) or not decision.strip():
            raise err(400, "Не передано решение", field="decision")
        branch_id = body.get("branch_id")
        if branch_id is not None and not isinstance(branch_id, str):
            raise err(400, "branch_id должен быть строкой", field="branch_id")
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            raise err(400, "text должен быть строкой", field="text")
        projects_mod.sync(self.cfg, room, rid)
        try:
            result = projects_mod.decide(self.cfg, room, rid, branch_id, decision, text or "")
        except projects_mod.ProjectError as exc:
            raise _fail(exc) from exc
        payload = {"ok": True, "root_id": rid}
        payload.update(result)
        return 200, payload, {"Cache-Control": "no-store"}

    def h_proposal_resolve(self, query, body, rid):
        body = body if isinstance(body, dict) else {}
        room, rec = self._find_record(rid)
        action = body.get("action")
        if not isinstance(action, str) or not action.strip():
            raise err(400, "Не передано решение по предложению", field="action")
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            raise err(400, "text должен быть строкой", field="text")
        projects_mod.sync(self.cfg, room, rid)
        try:
            result = projects_mod.resolve(self.cfg, room, rec, rid, action, text or "",
                                          board=room.get("board") or self.board)
        except projects_mod.ProjectError as exc:
            raise _fail(exc) from exc
        payload = {"ok": True, "root_id": rid}
        payload.update(result)
        return 200, payload, {"Cache-Control": "no-store"}

    # --- детали задачи: proposal + project -------------------------------
    def _project_view(self, room: dict, rid: str, rec: dict, detail: dict) -> None:
        """W30 §2: ``proposal`` и ``project`` в деталях задачи. Сбой страницу не роняет."""
        if not isinstance(detail, dict):
            return
        try:
            projects_mod.attach(self.cfg, room, rec, detail)
        except Exception as exc:  # noqa: BLE001 - проект вторичен для страницы задачи
            self.log.warning("предложение задач %s: %s" % (rid, exc))
            detail.setdefault("proposal", None)
            detail.setdefault("project", {"parent_id": None, "children": []})

    # --- действие «Разбить на задачи» ------------------------------------
    def _split_action(self, room: dict, rec: dict, rid: str, body: dict):
        """`{action:"split_to_tasks"}` — карточка руководителю «предложи ветки».

        Только по явному нажатию: ни на одном существующем запросе это действие не
        запускается само, и предложение задач не создаётся без ответа руководителя.
        """
        if not self._has_mod("projects"):
            raise err(501, "Модуль «проект» не готов — разбиение недоступно",
                      code="module_missing", module="projects")
        try:
            result = projects_mod.split(self.cfg, room, rec, rid,
                                        board=room.get("board") or self.board)
        except projects_mod.ProjectError as exc:
            raise _fail(exc) from exc
        payload = {"ok": True, "action": "split_to_tasks", "root_id": rid}
        payload.update(result)
        return 200, payload, {"Cache-Control": "no-store"}


def install_project_routes(app_cls, routes: list) -> None:
    """Подключить предложение задач и проект к ``App``: методы + строки таблицы маршрутов."""
    for name, value in vars(ProjectRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
