# -*- coding: utf-8 -*-
"""Бэклог комнаты в моке: строится из ВСЕХ запросов комнаты (блок B WAVE8).

Шаги (`next_steps`), вопросы (`open_questions`), решения (`decisions`) и
`future_questions` каждого запроса попадают в бэклог своей комнаты; id —
`<root_id>:<local>` (`ns_1`, `oq_1`, `d_1`, `fq_1`).

Установка статуса и «спросить» для неизвестного id сначала ищут элемент в
запросе и дописывают (upsert) его в бэклог — это чинит «Элемент бэклога не
найден» на демо, когда шаг ещё не пришёл через приём карточек.
"""
from __future__ import annotations

from mock_core import ApiError, backlog_counts, now

STATUSES = ("open", "asked", "done", "dismissed")
DECISION_STATUS = {"accepted": "done", "rejected": "dismissed", "proposed": "open"}


def _item(rid: str, detail: dict, local: str, kind: str, text: str, *, owner=None, due=None,
          status: str = "open") -> dict:
    created = int(detail.get("created_at") or now())
    return {"id": "%s:%s" % (rid, local), "kind": kind, "text": str(text or "").strip(),
            "owner": owner or None, "due": due or None, "status": status if status in STATUSES else "open",
            "source_request": rid, "source_title": str(detail.get("title") or ""),
            "asked_request": None, "created_at": created, "updated_at": created}


def items_from_detail(rid: str, detail: dict) -> list:
    """Элементы бэклога из одного запроса — детерминированно, без обращения к доске."""
    out = []
    for index, step in enumerate(detail.get("next_steps") or [], 1):
        local = str(step.get("id") or ("ns_%d" % index))
        out.append(_item(rid, detail, local, "next_step", step.get("text"),
                         owner=step.get("owner"), due=step.get("due"),
                         status=str(step.get("state") or "open")))
    for index, question in enumerate(detail.get("open_questions") or [], 1):
        out.append(_item(rid, detail, "oq_%d" % index, "open_question", question.get("text"),
                         owner=question.get("for") or "mikhail",
                         status="done" if question.get("answered") else "open"))
    for index, decision in enumerate(detail.get("decisions") or [], 1):
        local = str(decision.get("id") or ("d_%d" % index))
        out.append(_item(rid, detail, local, "decision", decision.get("text"),
                         owner=decision.get("owner") or detail.get("assignee"),
                         status=DECISION_STATUS.get(str(decision.get("status") or "proposed"), "open")))
    for index, question in enumerate(detail.get("future_questions") or [], 1):
        text = question if isinstance(question, str) else (question or {}).get("text")
        out.append(_item(rid, detail, "fq_%d" % index, "future_question", text, status="open"))
    return [item for item in out if item["text"]]


def merge(existing: list, derived: list) -> int:
    """Дописать недостающие элементы, сохранив статусы, выставленные Михаилом."""
    known = {item.get("id") for item in existing}
    added = 0
    for item in derived:
        if item["id"] in known:
            continue
        existing.append(item)
        known.add(item["id"])
        added += 1
    return added


def empty(room_id: str) -> dict:
    return {"room": room_id, "updated_at": now(),
            "counts": {"open": 0, "asked": 0, "done": 0, "dismissed": 0, "total": 0}, "items": []}


def refresh_counts(data: dict) -> dict:
    data["counts"] = backlog_counts(data["items"])
    return data


def find(data: dict, item_id: str):
    return next((item for item in data.get("items") or [] if item.get("id") == item_id), None)


def missing(item_id: str) -> ApiError:
    return ApiError(404, "not_found", "Элемент бэклога не найден", {"item_id": item_id})
