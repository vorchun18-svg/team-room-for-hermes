# -*- coding: utf-8 -*-
"""Демо доски «Задачи» комнаты для мок-сервера (W30-D2).

Форма ответов — как у боевого сервера (контракт §3 волны 30, пакет D1):
`GET /api/rooms/{room}/tasks` → `{tasks, columns, counts}`; действия `accept`
и `reopen_accepted` идут обычной ручкой `/api/requests/{id}/actions`.

Данные вымышленные (кофейня, химчистка, грузоперевозки). Состояние меняется
только в памяти демо-сервера и живёт до перезапуска; фикстура не перезаписывается.

    python tools/mock_server.py --port 8858
    # → вкладка «Задачи» комнаты cafe
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "w30d_tasks.json"

#: Порядок колонок — контракт §3 (подписи рисует клиент).
COLUMNS = ["new", "working", "needs_you", "review", "done_system", "accepted"]

#: Стадии, с которых сервер разрешает действие (`accept` — только из «Готово»).
ACCEPT_FROM = "done_system"
REOPEN_FROM = "accepted"

_STATE: dict | None = None


def _load() -> dict:
    global _STATE
    if _STATE is None:
        try:
            raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _STATE = copy.deepcopy(raw.get("rooms") or {})
    return _STATE


def _room_tasks(room: str) -> list:
    state = _load()
    entry = state.setdefault(str(room), {"tasks": []})
    return entry.setdefault("tasks", [])


def _counts(tasks: list) -> dict:
    out = {col: 0 for col in COLUMNS}
    for task in tasks:
        stage = str(task.get("stage") or "new")
        if stage in out:
            out[stage] += 1
    return out


def _register_pages(app, tasks: list) -> None:
    """Синтетические задачи получают страницу: иначе клик по карточке — тупик.

    Демо-сервер отдаёт деталь запроса только для известных `rid`; карточки
    принятых задач и подзадач в фикстурах отсутствуют. Здесь им выдаётся
    нейтральная деталь на основе готового запроса — только в памяти мока.
    """
    if app is None:
        return
    known = getattr(app, "requests", None)
    if not isinstance(known, dict):
        return
    base = getattr(app, "fx", {}).get("request_done.json")
    if not isinstance(base, dict):
        return
    for task in tasks:
        rid = str(task.get("id") or "")
        if not rid or rid in known:
            continue
        detail = copy.deepcopy(base)
        detail.update({
            "id": rid,
            "room": task.get("room") or "cafe",
            "title": task.get("title") or "Задача",
            "title_short": task.get("title") or "Задача",
            "status": "done" if task.get("stage") in ("done_system", "accepted") else "working",
            "archived": task.get("stage") == "archived",
            "accepted": bool(task.get("accepted")),
        })
        known[rid] = detail


def listing(app, room: str) -> dict:
    """`GET /api/rooms/{room}/tasks` — доска комнаты."""
    tasks = copy.deepcopy(_room_tasks(room))
    for task in tasks:
        task.setdefault("room", str(room))
    _register_pages(app, tasks)
    return {
        "tasks": tasks,
        "columns": list(COLUMNS),
        "counts": _counts(tasks),
        "updated_at": int(time.time()),
    }


def _find(room: str, rid: str) -> dict | None:
    for task in _room_tasks(room):
        if str(task.get("id")) == str(rid):
            return task
    return None


def act(app, rid: str, body: dict) -> dict:
    """`accept` / `reopen_accepted` — по контракту §3, с идемпотентностью."""
    action = str((body or {}).get("action") or "")
    task = None
    for room in list(_load().keys()):
        task = _find(room, rid)
        if task is not None:
            break
    if task is None:
        return {"ok": False, "reason": "not_board_task"}
    stage = str(task.get("stage") or "new")
    if action == "accept":
        if stage == ACCEPT_FROM:
            task["stage"] = "accepted"
            task["accepted"] = True
            task["updated_at"] = int(time.time())
        elif stage == "accepted":
            pass                                  # идемпотентно: уже принята
        else:
            return {"ok": False, "reason": "not_done",
                    "message": "Принять можно только готовую задачу — работа ещё не дошла до «Готово»."}
    elif action == "reopen_accepted":
        if stage == REOPEN_FROM:
            task["stage"] = "working"
            task["accepted"] = False
            task["updated_at"] = int(time.time())
        elif stage != REOPEN_FROM:
            return {"ok": False, "reason": "not_accepted",
                    "message": "Вернуть в работу можно только принятую задачу."}
    else:
        return {"ok": False, "reason": "unknown_action"}
    return {"ok": True, "task": copy.deepcopy(task), "action": action}


def handle(app, method: str, path: str, query: dict, body: dict):
    """Ветка для общего `POST /api/requests/{id}/actions` (как mock_batchb.handle).

    Возвращает `None`, если действие не наше — тогда работает обычный обработчик.
    """
    import re
    if method != "POST":
        return None
    action = str((body or {}).get("action") or "")
    if action not in ("accept", "reopen_accepted"):
        return None
    m = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})/actions", str(path))
    if not m:
        return None
    res = act(app, m.group(1), body or {})
    if res.get("reason") == "not_board_task":
        return None                               # обычный запрос — обычный обработчик
    if not res.get("ok"):
        from mock_core import ApiError
        raise ApiError(409, "conflict", res.get("message") or "Действие сейчас невозможно",
                       {"reason": res.get("reason")})
    return 200, res
