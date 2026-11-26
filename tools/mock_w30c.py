# -*- coding: utf-8 -*-
"""Демо «Вопросы и предложение руководителя» для мок-сервера (W30-C).

Клиент пакета C рисует по ОБЩЕМУ КОНТРАКТУ (docs/WAVE30-PACKAGES.md §1–§2), а
серверные пакеты A и B делают его параллельно. Чтобы проверить интерфейс до их
готовности, этот мок отдаёт ровно ту же форму ответа: `why`, `context`,
`options[]`, `blocking`, `topic_key`, `explanation`, `plain_ok`, `plain_issues`,
`answer_state:"explained"`, `questions_summary`, `stage_hint`, `proposal`,
`project` — и принимает решения человека (`answer_option`, `answer_custom`,
`explain_more`, `postpone`, `dismiss`, `restore`, `proposal/decision`,
`proposal/resolve`).

Состояние живёт в памяти процесса (как у остальных моков): правка вопроса или
решение по предложению видны до перезапуска, фикстуры не перезаписываются.

    python tools/mock_server.py --port 8855
    # → #/room/cafe/request/t_c0ffee01  (разовая задача)
    #   #/room/cafe/request/t_c0ffee02  (объёмная: ветки)
    #   #/room/cafe/request/t_c0ffee03  (предложение применено, подзадачи)
    #   #/room/cafe/request/t_c0ffee04  (подзадача: «Часть проекта»)

Данные — вымышленные и нейтральные: химчистка и грузоперевозки.
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_FIXTURE = ROOT / "fixtures" / "w30_questions.json"
PROPOSAL_FIXTURE = ROOT / "fixtures" / "w30_proposal.json"

# Базовая фикстура: комната cafe, живая задача «ждёт вас». Из неё берём дерево,
# свод, диалог — своё подменяем только по контракту §1–§2.
BASE_RID = "t_5c8e4a02"
ROOM = "cafe"

# Демо-задачи пакета C (только шестнадцатеричные цифры — как настоящие id).
DEMO_RIDS = ("t_c0ffee01", "t_c0ffee02", "t_c0ffee03", "t_c0ffee04")

# Действия вопроса, которых нет в старом моке (или которые здесь ведут себя иначе).
QUESTION_ACTIONS = ("answer_option", "answer_custom", "explain_more", "postpone")

_CACHE: dict | None = None
# Правки состояния: {rid: {"questions": {qid: patch}, "proposal": {...}}}
_STATE: dict = {}


# --- фикстуры ---------------------------------------------------------------
def _fixtures() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            questions = json.loads(QUESTIONS_FIXTURE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            questions = {}
        try:
            proposal = json.loads(PROPOSAL_FIXTURE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            proposal = {}
        _CACHE = {"questions": questions, "proposal": proposal}
    return _CACHE


def _now() -> int:
    return int(time.time())


def _demo_state(rid: str) -> dict:
    """Состояние демо-задачи: копия фикстуры + правки человека в памяти."""
    bucket = _STATE.setdefault(rid, {})
    if "ready" not in bucket:
        fixtures = _fixtures()
        base = copy.deepcopy(fixtures["questions"].get(rid) or {})
        proposals = fixtures["proposal"].get("proposals") or {}
        projects = fixtures["proposal"].get("projects") or {}
        bucket["ready"] = True
        bucket["title"] = base.get("title") or "Демо-задача"
        bucket["text"] = base.get("text") or ""
        bucket["hint"] = base.get("stage_hint") or ""
        bucket["questions"] = copy.deepcopy(base.get("questions") or [])
        bucket["proposal"] = copy.deepcopy(proposals.get(rid)) if rid in proposals else None
        bucket["project"] = copy.deepcopy(projects.get(rid)) if rid in projects else None
        bucket["duplicates"] = int(base.get("duplicates") or 0)
        bucket["seq"] = 0
    return bucket


def _find_question(state: dict, qid: str) -> dict | None:
    for item in state.get("questions") or []:
        if str(item.get("id")) == str(qid):
            return item
    return None


def _open_questions(state: dict) -> list:
    return [q for q in (state.get("questions") or [])
            if not q.get("answered") and not q.get("dismissed")
            and str(q.get("answer_state") or "open") not in ("postponed",)]


def _counts(state: dict) -> dict:
    opened = _open_questions(state)
    blocking = [q for q in opened if q.get("blocking") is True]
    return {"open": len(opened), "blocking": len(blocking),
            "hidden_duplicates": int(state.get("duplicates") or 0),
            "closed_topics": 1}


# --- регистрация демо-задач в моке -----------------------------------------
def ensure_demo(app) -> None:
    """Задачи пакета C становятся известны моку (в памяти, фикстура не меняется)."""
    base = app.fx.get("request_needs_you.json")
    if not base:
        return
    for rid in DEMO_RIDS:
        if rid not in app.requests:
            app.requests[rid] = copy.deepcopy(base)


# --- наложение на деталь ----------------------------------------------------
def overlay(app, detail: dict, rid: str) -> None:
    """Наложить поля контракта §1–§2 на собранную деталь (вызывается из mock_read)."""
    if rid not in DEMO_RIDS or not isinstance(detail, dict):
        return
    state = _demo_state(rid)
    detail["id"] = rid
    detail["room"] = ROOM
    detail["title"] = state["title"]
    detail["title_short"] = state["title"][:60]
    detail["text"] = state["text"]
    # Причина статуса из базовой фикстуры — про её вопросы, а не про наши.
    detail["status_reason"] = None
    detail["status"] = "needs_you" if _open_questions(state) else "working"
    detail["needs_you"] = bool(_open_questions(state))
    detail["open_questions"] = copy.deepcopy(state["questions"])
    detail["proposal"] = copy.deepcopy(state["proposal"])
    detail["project"] = copy.deepcopy(state["project"]) if state.get("project") else {"parent_id": None, "children": []}
    summary = _counts(state)
    detail["questions_summary"] = summary
    hint = state.get("hint") or ""
    if not hint:
        if summary["blocking"]:
            hint = "работа ждёт вашего ответа: %d %s" % (
                summary["blocking"], "вопрос блокирует" if summary["blocking"] == 1 else "вопроса блокируют")
        else:
            hint = "работа идёт, %d %s ждут вас" % (
                summary["open"], "вопрос" if summary["open"] == 1 else "вопросов")
    detail["stage_hint"] = hint
    detail["needs_you_count"] = {"questions": summary["open"], "decisions": 0,
                                 "approvals": 0, "steps": 0, "total": summary["open"]}


# --- чтение предложения -----------------------------------------------------
def proposal_payload(rid: str) -> dict:
    state = _demo_state(rid)
    return {"proposal": copy.deepcopy(state.get("proposal")), "ready": bool(state.get("proposal"))}


# --- решения по предложению -------------------------------------------------
def _branch(state: dict, branch_id: str | None) -> dict | None:
    proposal = state.get("proposal") or {}
    for item in proposal.get("branches") or []:
        if str(item.get("id")) == str(branch_id):
            return item
    return None


def _touch(proposal: dict) -> None:
    proposal["updated_at"] = _now()


def decision(rid: str, body: dict) -> dict:
    """POST …/proposal/decision {branch_id|null, decision, text?} (контракт §2)."""
    state = _demo_state(rid)
    proposal = state.get("proposal")
    if not proposal:
        raise ValueError(("not_found", "Предложения по этой задаче нет"))
    if str(proposal.get("state")) in ("applied", "rejected"):
        raise ValueError(("conflict", "Предложение уже закрыто — решение не изменить"))
    choice = str(body.get("decision") or "").strip()
    text = str(body.get("text") or "").strip()
    branch_id = body.get("branch_id")
    if str(proposal.get("kind")) == "single":
        if choice not in ("agree", "disagree", "custom"):
            raise ValueError(("bad_request", "decision: agree|disagree|custom"))
        if choice == "custom" and not text:
            raise ValueError(("bad_request", "Свой вариант — нужен текст"))
        proposal.setdefault("single", {})["decision"] = choice
        proposal["single"]["text"] = text
        proposal["state"] = "custom" if choice == "custom" else "approved"
        _touch(proposal)
        return {"ok": True, "proposal": copy.deepcopy(proposal)}
    if choice not in ("yes", "no", "custom"):
        raise ValueError(("bad_request", "decision: yes|no|custom"))
    if choice == "custom" and not text:
        raise ValueError(("bad_request", "Свой вариант — нужен текст"))
    item = _branch(state, branch_id)
    if item is None:
        raise ValueError(("not_found", "Ветка не найдена: %s" % branch_id))
    item["decision"] = choice
    item["custom_text"] = text if choice == "custom" else ""
    _touch(proposal)
    return {"ok": True, "proposal": copy.deepcopy(proposal)}


def resolve(rid: str, body: dict) -> dict:
    """POST …/proposal/resolve {action:accept_all|reject_all|custom|apply, text?}."""
    state = _demo_state(rid)
    proposal = state.get("proposal")
    if not proposal:
        raise ValueError(("not_found", "Предложения по этой задаче нет"))
    action = str(body.get("action") or "").strip()
    text = str(body.get("text") or "").strip()
    if action == "accept_all":
        for item in proposal.get("branches") or []:
            item["decision"] = "yes"
        proposal["resolution"] = {"action": "accept_all", "text": text, "at": _now()}
        _touch(proposal)
        return {"ok": True, "proposal": copy.deepcopy(proposal)}
    if action == "reject_all":
        proposal["state"] = "rejected"
        proposal["resolution"] = {"action": "reject_all", "text": text, "at": _now()}
        _touch(proposal)
        return {"ok": True, "proposal": copy.deepcopy(proposal)}
    if action == "custom":
        if not text:
            raise ValueError(("bad_request", "Свой вариант — нужен текст"))
        proposal["state"] = "custom"
        proposal["resolution"] = {"action": "custom", "text": text, "at": _now()}
        _touch(proposal)
        return {"ok": True, "proposal": copy.deepcopy(proposal)}
    if action != "apply":
        raise ValueError(("bad_request", "action: accept_all|reject_all|custom|apply"))
    created = []
    for item in proposal.get("branches") or []:
        if item.get("task_id"):
            continue                     # идемпотентность: повторное «применить» не дублирует
        if str(item.get("decision")) not in ("yes", "custom"):
            continue
        state["seq"] = int(state.get("seq") or 0) + 1
        task_id = "t_c0ff%04d" % (100 + state["seq"])
        item["task_id"] = task_id
        created.append({"id": task_id, "title": item.get("title") or "Подзадача",
                        "status": "triage", "stage": "new"})
    project = state.get("project") or {"parent_id": None, "children": []}
    project["children"] = list(project.get("children") or []) + created
    state["project"] = project
    proposal["state"] = "applied"
    proposal["resolution"] = {"action": "apply", "text": text, "at": _now()}
    _touch(proposal)
    return {"ok": True, "proposal": copy.deepcopy(proposal), "created": created,
            "duplicate": not created}


# --- действия вопроса -------------------------------------------------------
def question_action(rid: str, body: dict) -> dict | None:
    """Новые действия вопроса (§1). Возвращает `None`, если действие не наше."""
    action = str(body.get("action") or "")
    target = body.get("target") if isinstance(body.get("target"), dict) else None
    ours = action in QUESTION_ACTIONS or (
        action in ("dismiss", "restore") and target and str(target.get("kind")) == "question")
    if not ours:
        return None
    state = _demo_state(rid)
    qid = str(body.get("question_id") or (target or {}).get("id") or "")
    question = _find_question(state, qid)
    if question is None:
        raise ValueError(("not_found", "Вопрос не найден: %s" % qid))
    stamp = _now()
    if action == "answer_option":
        option_id = str(body.get("option_id") or "")
        option = next((o for o in question.get("options") or []
                       if str(o.get("id")) == option_id), None)
        if option is None:
            raise ValueError(("bad_request", "Такого варианта нет: %s" % option_id))
        question.update({"answered": True, "answer_state": "sent", "answer": option.get("label"),
                         "answer_option": option_id, "answer_at": stamp, "answered_at": stamp,
                         "answer_label": "ответ отправлен"})
    elif action == "answer_custom":
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError(("bad_request", "Нужен текст своего ответа"))
        question.update({"answered": True, "answer_state": "sent", "answer": text,
                         "answer_at": stamp, "answered_at": stamp,
                         "answer_label": "ответ отправлен"})
    elif action == "explain_more":
        question.update({"answer_state": "waiting_explanation", "needs_explanation": True,
                         "explained_more_at": stamp})
    elif action == "postpone":
        question.update({"answer_state": "postponed", "postponed": True, "postponed_at": stamp})
    elif action == "dismiss":
        question.update({"dismissed": True, "answer_state": "dismissed", "state": "dismissed",
                         "closed_reason": "dismissed", "dismissed_at": stamp})
    elif action == "restore":
        question.update({"dismissed": False, "postponed": False, "answer_state": "open",
                         "state": "open"})
        question.pop("closed_reason", None)
        question.pop("postponed_at", None)
    return {"ok": True, "action": action, "question": copy.deepcopy(question),
            "message": "Готово"}


# --- HTTP-вход --------------------------------------------------------------
RID_RE = r"(t_c0ffee0[1-4])"


def handle(app, method: str, path: str, query: dict, body: dict):
    """Точка подключения мока (одна строка в mock_read/mock_write)."""
    if not re.search(r"t_c0ffee0[1-4]", path):
        return None
    ensure_demo(app)
    m = re.fullmatch(r"/api/requests/" + RID_RE + r"/proposal", path)
    if m and method == "GET":
        return 200, proposal_payload(m.group(1))
    m = re.fullmatch(r"/api/requests/" + RID_RE + r"/proposal/(decision|resolve)", path)
    if m and method == "POST":
        rid, what = m.group(1), m.group(2)
        try:
            if what == "decision":
                return 200, decision(rid, body or {})
            return 200, resolve(rid, body or {})
        except ValueError as exc:
            code, message = exc.args[0] if exc.args and isinstance(exc.args[0], tuple) else ("bad_request", str(exc))
            from mock_core import ApiError
            status = {"not_found": 404, "conflict": 409}.get(code, 400)
            raise ApiError(status, code, message, {"action": what}) from exc
    m = re.fullmatch(r"/api/requests/" + RID_RE + r"/actions", path)
    if m and method == "POST":
        try:
            hit = question_action(m.group(1), body or {})
        except ValueError as exc:
            code, message = exc.args[0] if exc.args and isinstance(exc.args[0], tuple) else ("bad_request", str(exc))
            from mock_core import ApiError
            status = {"not_found": 404, "conflict": 409}.get(code, 400)
            raise ApiError(status, code, message, {}) from exc
        if hit is not None:
            return 200, hit
        return None
    return None
