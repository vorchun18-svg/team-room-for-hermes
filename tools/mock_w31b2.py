# -*- coding: utf-8 -*-
"""W31-B2 — демо-поля окна задачи для мок-сервера (волна 31, пакет B2).

Пакеты A1/A2 делают серверную часть параллельно. Пока их нет, мок обязан
отвечать по ТОМУ ЖЕ контракту (`docs/WAVE31-PLAN.md`, «Общий контракт по
статусам»), иначе проверять клиент не на чем:

* ``GET /api/requests/{id}`` → ``board_column`` (new/working/review/done) и
  ``allowed_moves`` (список допустимых ``to``);
* ``POST /api/requests/{id}/actions {action:'move', to, comment?, confirm?}``;
  при открытых вопросах сначала ``{needs_confirm: true, open_questions: N}``,
  повтор с ``confirm: true`` принимает и закрывает вопросы («задача принята»);
* ``agent_results[].agent_name`` / ``agent_role`` — имя участника комнаты и
  роль по-русски (дефект 31, серверная часть — A2).

Состояние — только в памяти мок-сервера, фикстуры не перезаписываются.
Данные вымышленные; модели не вызываются.
"""
from __future__ import annotations

import re
import time

#: Колонки доски — контракт волны 31 (четыре, без needs_you/accepted).
COLUMNS = ("new", "working", "review", "done")

#: Что разрешено из каждой колонки (правила контракта §«Действие перемещения»).
ALLOWED = {
    "new": ["working"],
    "working": ["review"],
    "review": ["done", "working"],
    "done": ["working"],
}

#: Имена участников демо-комнаты (вымышленные, нейтральные).
_NAME = {
    "team-director": "Руководитель",
    "team-critic": "Критик",
    "team-lawyer": "Анна",
    "team-ops": "Марина",
    "team-strategy": "Атлас",
    "team-marketing": "Лада",
    "team-finance": "Тимур",
}

#: Роль по-русски из роли карточки.
_ROLE = {
    "root": "руководитель",
    "synthesizer": "руководитель",
    "orchestrator": "руководитель",
    "critic": "критик",
    "worker": "исполнитель",
    "revision": "доработка",
}

#: Колонка по старой стадии/статусу (как в контракте перехода).
_STAGE = {
    "new": "new",
    "working": "working",
    "needs_you": "working",
    "review": "review",
    "done_system": "review",
    "accepted": "done",
    "archived": "done",
}

_STATE: dict[str, str] = {}
_CLOSED: set[str] = set()


def _stage_of(detail: dict) -> str:
    stage = detail.get("stage")
    if isinstance(stage, dict):
        stage = stage.get("id") or stage.get("current") or ""
    return str(stage or "").strip().lower()


def base_column(detail: dict) -> str:
    """Колонка по данным фикстуры (пока никто не двигал)."""
    stage = _stage_of(detail)
    if stage in _STAGE:
        return _STAGE[stage]
    column = str(detail.get("board_column") or "").strip().lower()
    if column in COLUMNS:
        return column
    status = str(detail.get("status") or "").strip().lower()
    if status == "done":
        return "review"
    if status == "new":
        return "new"
    if status == "archived":
        return "done"
    return "working"


def _open_questions(detail: dict) -> int:
    n = 0
    for q in detail.get("open_questions") or []:
        if not isinstance(q, dict):
            continue
        if q.get("answered") or q.get("dismissed"):
            continue
        if str(q.get("state") or "open").lower() not in ("open", ""):
            continue
        n += 1
    return n


def attach(server, detail: dict) -> dict:
    """Положить в деталь ``board_column``, ``allowed_moves`` и имена агентов."""
    if not isinstance(detail, dict):
        return detail
    rid = str(detail.get("id") or "")
    # A1 уже отвечает по контракту для задач доски — не спорим с ним: свои
    # значения ставим только там, где полей нет, или где задача сдвинута нами.
    given = str(detail.get("board_column") or "").strip().lower()
    if rid in _STATE:
        column = _STATE[rid]
    elif given in COLUMNS:
        column = given
    else:
        column = base_column(detail)
    if column not in COLUMNS:
        column = "working"
    detail["board_column"] = column
    detail["allowed_moves"] = list(detail.get("allowed_moves") or ALLOWED.get(column, []))
    if rid in _CLOSED:
        for q in detail.get("open_questions") or []:
            if isinstance(q, dict) and not q.get("answered") and not q.get("dismissed"):
                q["state"] = "closed"
                q["answered"] = True
                q["closed_reason"] = "задача принята"
    try:
        rows = (detail.get("agent_results") or {}).get("agents") or []
    except AttributeError:
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        agent = str(row.get("agent") or "")
        # Сервер уже кладёт `agent_name` из подписи комнаты; если это тот же
        # технический профиль (демо без подписей), показываем имя участника.
        current = str(row.get("agent_name") or "").strip()
        if not current or current == agent:
            row["agent_name"] = _NAME.get(agent) or agent or "Исполнитель"
        if not row.get("agent_role"):
            raw = str(row.get("role_raw") or "").strip().lower()
            row["agent_role"] = _ROLE.get(raw) or str(row.get("role") or "исполнитель")
        row.setdefault("label", row["agent_name"])
    return detail


def move(server, rid: str, body: dict) -> dict:
    """`move` по контракту: allowed_moves, вопросы, подтверждение."""
    detail = server.detail(rid)
    column = _STATE.get(rid) or base_column(detail)
    to = str((body or {}).get("to") or "").strip().lower()
    if to not in COLUMNS:
        return {"ok": False, "message": "Такой колонки нет: " + (to or "пусто")}
    if to not in ALLOWED.get(column, []):
        human = {"done": "принять", "working": "вернуть в работу", "review": "отправить на проверку"}
        return {"ok": False,
                "message": "Так перенести нельзя: из «%s» нельзя %s"
                           % (column, human.get(to, "перенести"))}
    open_questions = _open_questions(detail)
    if to == "done" and open_questions and (body or {}).get("confirm") is not True:
        return {"needs_confirm": True, "open_questions": open_questions}
    _STATE[rid] = to
    if to == "done":
        _CLOSED.add(rid)
    else:
        _CLOSED.discard(rid)
    return {"ok": True, "action": "move", "to": to, "board_column": to,
            "allowed_moves": list(ALLOWED.get(to, [])),
            "closed_questions": open_questions if to == "done" else 0,
            "updated_at": int(time.time())}


def handle(server, method: str, path: str, query: dict, body: dict):
    """Ветка общего `POST /api/requests/{id}/actions`; `None` — не наше действие."""
    if method != "POST":
        return None
    if str((body or {}).get("action") or "") != "move":
        return None
    m = re.fullmatch(r"/api/requests/([A-Za-z0-9_.-]{1,64})/actions", str(path))
    if not m:
        return None
    res = move(server, m.group(1), body or {})
    if res.get("needs_confirm"):
        return 200, res
    if not res.get("ok"):
        from mock_core import ApiError
        raise ApiError(409, "conflict", res.get("message") or "Так перенести нельзя",
                       {"reason": "not_allowed"})
    return 200, res
