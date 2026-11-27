# -*- coding: utf-8 -*-
"""Демо доски «Задачи» комнаты для мок-сервера (W31-B1: контракт четырёх колонок).

Форма ответов — общий контракт волны 31:
* ``GET /api/rooms/{room}/tasks`` → ``{tasks, columns, counts, updated_at}``,
  у карточки есть ``column`` (``new|working|review|done``), ``waiting_you``,
  ``waiting_count``, ``allowed_moves``; ``stage`` повторяет колонку для старых
  читателей;
* ``POST /api/requests/{id}/actions {action:'move', to, comment?, confirm?}`` —
  ровно переходы контракта: ``review→done`` (принять), ``done→working``
  (вернуть в работу), ``review→working`` (на доработку, комментарий),
  ``working→review`` (на проверку). При открытых вопросах приёмка сначала
  отвечает ``{needs_confirm:true, open_questions:N}``, повтор с ``confirm:true``
  принимает и закрывает вопросы («закрыт: задача принята»);
* ``GET /api/requests/{id}`` получает ``board_column`` и ``allowed_moves``.

Старые действия ``accept`` / ``reopen_accepted`` поддерживаются как синонимы
``move`` — старые клиенты и тесты продолжают работать.

Данные вымышленные (кофейня, химчистка, грузоперевозки). Состояние меняется
только в памяти демо-сервера и живёт до перезапуска; фикстура не перезаписывается.

    python tools/mock_server.py --port 8861
    # → вкладка «Задачи» комнаты cafe
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "w30d_tasks.json"

#: Порядок колонок — контракт 31 (подписи рисует клиент).
COLUMNS = ["new", "working", "review", "done"]

#: Старая стадия → колонка (фикстура W30 писалась в старых стадиях).
LEGACY = {
    "new": "new",
    "working": "working",
    "needs_you": "working",
    "review": "review",
    "done_system": "review",
    "accepted": "done",
    "done": "done",
    "archived": "archived",
}

#: Допустимые переносы контракта 31.
MOVES = {
    "new": [],
    "working": ["review"],
    "review": ["done", "working"],
    "done": ["working"],
    "archived": [],
}

_STATE: dict | None = None


def _norm(task: dict) -> dict:
    """Старая стадия → колонка; метка «Ждёт вас» — только у карточки в работе."""
    raw = str(task.get("column") or task.get("stage") or "new")
    column = LEGACY.get(raw, "new")
    task["column"] = column
    task["stage"] = column                      # совместимость со старыми читателями
    needs = task.get("needs_you") if isinstance(task.get("needs_you"), dict) else {}
    count = int(task.get("waiting_count") or needs.get("count") or 0)
    if column in ("done", "new"):
        count = 0                                # в «Готово» вопросов нет по контракту
    task["waiting_count"] = max(0, count)
    task["waiting_you"] = bool(count > 0)
    task["needs_you"] = {"count": task["waiting_count"], "blocking": bool(needs.get("blocking"))}
    task["accepted"] = column == "done"
    task["allowed_moves"] = list(MOVES.get(column, []))
    return task


def _load() -> dict:
    global _STATE
    if _STATE is None:
        try:
            raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        rooms = copy.deepcopy(raw.get("rooms") or {})
        for entry in rooms.values():
            for task in entry.get("tasks") or []:
                _norm(task)
        _STATE = rooms
    return _STATE


def _room_tasks(room: str) -> list:
    state = _load()
    entry = state.setdefault(str(room), {"tasks": []})
    return entry.setdefault("tasks", [])


def _counts(tasks: list) -> dict:
    out = {col: 0 for col in COLUMNS}
    for task in tasks:
        column = str(task.get("column") or "new")
        if column in out:
            out[column] += 1
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
        if not rid:
            continue
        detail = known.get(rid)
        if not isinstance(detail, dict):
            detail = copy.deepcopy(base)
            detail.update({
                "id": rid,
                "room": task.get("room") or "cafe",
                "title": task.get("title") or "Задача",
                "title_short": task.get("title") or "Задача",
                "archived": False,
            })
            known[rid] = detail
        detail["board_column"] = task.get("column")
        detail["allowed_moves"] = list(task.get("allowed_moves") or [])
        detail["accepted"] = bool(task.get("accepted"))
        detail["status"] = "done" if task.get("column") in ("review", "done") else "working"


def listing(app, room: str) -> dict:
    """`GET /api/rooms/{room}/tasks` — доска комнаты."""
    tasks = copy.deepcopy(_room_tasks(room))
    for task in tasks:
        task.setdefault("room", str(room))
        _norm(task)
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


def _any_task(rid: str) -> dict | None:
    for room in list(_load().keys()):
        task = _find(room, rid)
        if task is not None:
            return task
    return None


def act(app, rid: str, body: dict) -> dict:
    """`move` / `accept` / `reopen_accepted` — контракт 31, с идемпотентностью."""
    body = body or {}
    action = str(body.get("action") or "")
    task = _any_task(rid)
    if task is None:
        return {"ok": False, "reason": "not_board_task"}
    _norm(task)
    column = str(task.get("column") or "new")

    if action == "accept":
        body = {"action": "move", "to": "done", "confirm": body.get("confirm")}
        action = "move"
    elif action == "reopen_accepted":
        body = {"action": "move", "to": "working"}
        action = "move"
    if action != "move":
        return {"ok": False, "reason": "unknown_action"}

    target = str(body.get("to") or "")
    if target not in COLUMNS:
        return {"ok": False, "reason": "bad_target",
                "message": "Такой колонки нет на доске."}
    if target == column:
        return {"ok": True, "task": copy.deepcopy(task), "action": "move", "noop": True}
    if target not in MOVES.get(column, []):
        return {"ok": False, "reason": "not_allowed",
                "message": "так перенести нельзя: из «%s» в «%s» доска не переставляет"
                           % (column, target)}

    # Приёмка при открытых вопросах — сначала подтверждение (контракт 31).
    if target == "done" and int(task.get("waiting_count") or 0) > 0 and not body.get("confirm"):
        return {"ok": False, "needs_confirm": True,
                "open_questions": int(task.get("waiting_count") or 0),
                "message": "У задачи %d открытых вопросов — при приёмке они закроются."
                           % int(task.get("waiting_count") or 0)}

    if target == "done":
        # Все висящие вопросы закрываются («закрыт: задача принята»), обратимо.
        task["_closed_questions"] = int(task.get("waiting_count") or 0)
        task["waiting_count"] = 0
        task["waiting_you"] = False
        task["accepted"] = True
    if target == "working" and column == "done":
        closed = int(task.pop("_closed_questions", 0) or 0)
        task["waiting_count"] = closed
        task["waiting_you"] = closed > 0
        task["accepted"] = False
    if target == "working" and column == "review":
        task["accepted"] = False
    task["column"] = target
    task["stage"] = target
    task["allowed_moves"] = list(MOVES.get(target, []))
    task["updated_at"] = int(time.time())
    if body.get("comment"):
        task["last_comment"] = str(body.get("comment"))[:500]
    _register_pages(app, [task])
    return {"ok": True, "task": copy.deepcopy(task), "action": "move", "to": target}


def handle(app, method: str, path: str, query: dict, body: dict):
    """Ветка для общего `POST /api/requests/{id}/actions` (как mock_batchb.handle).

    Возвращает `None`, если действие не наше — тогда работает обычный обработчик.
    """
    if method != "POST":
        return None
    action = str((body or {}).get("action") or "")
    if action not in ("move", "accept", "reopen_accepted"):
        return None
    m = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})/actions", str(path))
    if not m:
        return None
    res = act(app, m.group(1), body or {})
    if res.get("reason") == "not_board_task":
        return None                               # обычный запрос — обычный обработчик
    if res.get("needs_confirm"):
        return 200, res
    if not res.get("ok"):
        from mock_core import ApiError
        raise ApiError(409, "conflict", res.get("message") or "Действие сейчас невозможно",
                       {"reason": res.get("reason")})
    return 200, res


def detail(app, rid: str):
    """Поля детали задачи для контракта 31 (`board_column`, `allowed_moves`)."""
    task = _any_task(rid)
    if task is None:
        return None
    _norm(task)
    return {"board_column": task.get("column"), "allowed_moves": list(task.get("allowed_moves") or [])}
