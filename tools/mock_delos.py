# -*- coding: utf-8 -*-
"""W26-D5: демо «Дела» для мок-сервера (форма — fixtures/delo.json, контракт §1).

Мок показывает интерфейсу то же, что отдаёт живой сервер: список дел, одно дело,
создание и 15 действий. Состояние — в памяти процесса, форма — из фикстуры пакета D6.
Наружу ничего не уходит: `mark_sent` только меняет состояние исходящего.

    MOCK: fixtures/delo.json + действия в памяти (перезапуск мока — снова как в файле)
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re

from mock_core import ApiError, ROOT, now

ACTIONS = ("approve", "reject", "answer", "set_stage", "add_part", "set_part",
           "approve_outbox", "edit_outbox", "cancel_outbox", "mark_sent",
           "pause", "resume", "done", "add_waiting", "clear_waiting")
STAGE_STATES = ("done", "now", "todo")
PART_STATES = ("agreed", "discussing", "todo")
DEFAULT_STAGES = ("Сбор", "Черновик", "Согласование", "Исполнение", "Готово")
STORE: dict | None = None


def _store() -> dict:
    """Фикстура один раз на процесс: дальше действия правят копию в памяти."""
    global STORE
    if STORE is None:
        data = {}
        try:
            with open(os.path.join(ROOT, "fixtures", "delo.json"), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        items = data.get("items") if isinstance(data, dict) else data
        STORE = {"items": [copy.deepcopy(item) for item in (items or []) if isinstance(item, dict)]}
    return STORE


def items() -> list[dict]:
    return _store()["items"]


def _find(delo_id: str) -> dict:
    for item in items():
        if str(item.get("id")) == str(delo_id):
            return item
    raise ApiError(404, "not_found", "Дело не найдено", {"id": str(delo_id)[:40]})


def _timeline(delo: dict, who: str, text: str, kind: str) -> None:
    delo.setdefault("timeline", []).append({"at": now(), "who": who, "text": text, "kind": kind})


def _touch(delo: dict) -> None:
    delo["updated_at"] = now()


def payload(query: dict, path: str = "") -> dict:
    """`GET /api/rooms/{id}/delos` и `GET /api/delos?room=<id|all>`."""
    room = ""
    match = re.fullmatch(r"/api/rooms/([a-z][a-z0-9_-]{1,31})/delos", path or "")
    if match:
        room = match.group(1)
    else:
        room = str((query.get("room") or ["all"])[0]).strip()
    out = copy.deepcopy(items())
    if room and room not in ("all", "*"):
        out = [item for item in out if str(item.get("room")) == room]
    out.sort(key=lambda item: (-int(item.get("updated_at") or 0), str(item.get("id"))))
    return {"items": out, "generated_at": now()}


def one(delo_id: str) -> dict:
    return copy.deepcopy(_find(delo_id))


def _default_stages() -> list[dict]:
    return [{"id": "st_%d" % number, "title": title,
             "state": "now" if number == 1 else "todo"}
            for number, title in enumerate(DEFAULT_STAGES, 1)]


def create(room_id: str, body: dict) -> dict:
    """`POST /api/rooms/{id}/delos`: дело из запроса или вручную (демо)."""
    title = str((body or {}).get("title") or "").strip()
    request_id = str((body or {}).get("request_id") or "").strip() or None
    if not title:
        raise ApiError(400, "bad_request", "Нужно название дела", {"field": "title"})
    ident = "dl_" + hashlib.md5(("%s|%s|%s" % (room_id, title, now())).encode("utf-8")).hexdigest()[:8]
    delo = {"id": ident, "room": room_id, "title": title, "status": "open",
            "due": (body or {}).get("due") or None, "owner": "manager",
            "members": list((body or {}).get("members") or []), "updated_at": now(),
            "request_id": request_id, "stages": _default_stages(), "waiting_on": [],
            "needs_you": [], "result": {"parts": [], "files": []},
            "outbox": [{"id": "ob_%d" % number, "kind": str(entry.get("kind") or "email"),
                        "to": str(entry.get("to") or ""), "subject": str(entry.get("subject") or ""),
                        "body": str(entry.get("body") or ""),
                        "attachments": list(entry.get("attachments") or []), "state": "draft"}
                       for number, entry in enumerate((body or {}).get("outbox") or [], 1)
                       if isinstance(entry, dict)],
            "found": copy.deepcopy((body or {}).get("found") or []), "timeline": [],
            "duties": copy.deepcopy((body or {}).get("duties") or [])}
    _timeline(delo, "manager", "Дело открыто %s: %s" % (
        "из запроса %s" % request_id if request_id else "вручную", title), "created")
    items().append(delo)
    return copy.deepcopy(delo)


def _stage(delo: dict, stage_id: str) -> dict:
    for stage in delo.get("stages") or []:
        if str(stage.get("id")) == str(stage_id):
            return stage
    raise ApiError(404, "not_found", "Этап не найден", {"stage_id": str(stage_id)[:40]})


def _outbox(delo: dict, outbox_id: str) -> dict:
    for entry in delo.get("outbox") or []:
        if str(entry.get("id")) == str(outbox_id):
            return entry
    raise ApiError(404, "not_found", "Исходящее не найдено", {"outbox_id": str(outbox_id)[:40]})


def _part(delo: dict, part_id: str) -> dict:
    for part in (delo.get("result") or {}).get("parts") or []:
        if str(part.get("id")) == str(part_id):
            return part
    raise ApiError(404, "not_found", "Часть результата не найдена", {"part_id": str(part_id)[:40]})


def act(delo_id: str, body: dict) -> dict:
    """`POST /api/delos/{id}/actions`: 15 действий контракта §1 (демо-правила)."""
    action = str((body or {}).get("action") or "").strip()
    if action not in ACTIONS:
        raise ApiError(400, "bad_request", "Неизвестное действие",
                       {"field": "action", "value": action[:40], "allowed": list(ACTIONS)})
    delo = _find(delo_id)
    if action in ("approve", "reject", "answer"):
        point_id = str((body or {}).get("item_id") or (body or {}).get("need_id") or "")
        point = next((item for item in delo.get("needs_you") or []
                      if str(item.get("id")) == point_id), None)
        if point is None:
            raise ApiError(409, "conflict", "Пункт уже не ждёт решения", {"item_id": point_id[:60]})
        if action not in (point.get("actions") or []):
            raise ApiError(400, "bad_request", "Этот пункт не поддерживает действие",
                           {"field": "action", "value": action, "allowed": point.get("actions")})
        text = str((body or {}).get("text") or "").strip()
        option_id = str((body or {}).get("option_id") or "").strip()
        if not text and option_id:
            option = next((item for item in point.get("options") or []
                           if str(item.get("id")) == option_id), None)
            if option is None:
                raise ApiError(400, "bad_request", "Такого варианта ответа нет",
                               {"field": "option_id", "value": option_id[:40]})
            text = str(option.get("label") or "")
        if action == "answer" and not text:
            raise ApiError(400, "bad_request", "Нужен текст ответа", {"field": "text"})
        delo["needs_you"] = [item for item in delo["needs_you"] if item is not point]
        _timeline(delo, "you", "«%s» — %s" % (str(point.get("text"))[:120],
                                              {"approve": "утверждено", "reject": "отклонено",
                                               "answer": "отвечено"}[action]), "decision")
    elif action == "set_stage":
        stage = _stage(delo, (body or {}).get("stage_id") or "")
        title = str((body or {}).get("title") or "").strip()
        if title:
            stage["title"] = title
        state = str((body or {}).get("state") or "").strip().lower()
        if not state and not title:
            state = "now"                   # «сделать этап текущим» — так подписана кнопка
        if state:
            if state not in STAGE_STATES:
                raise ApiError(400, "bad_request", "state: done|now|todo",
                               {"field": "state", "allowed": list(STAGE_STATES)})
            chain = delo.get("stages") or []
            index = chain.index(stage)
            if state == "now":
                for before in chain[:index]:
                    before["state"] = "done"
                stage["state"] = "now"
                for after in chain[index + 1:]:
                    if after.get("state") == "now":
                        after["state"] = "todo"
            elif state == "done":
                stage["state"] = "done"
                for before in chain[:index]:
                    before["state"] = "done"
                for after in chain[index + 1:]:
                    if after.get("state") != "done":
                        after["state"] = "now"
                        break
            else:
                stage["state"] = "todo"
                for after in chain[index + 1:]:
                    after["state"] = "todo"
        _timeline(delo, "you", "Этап «%s»" % stage.get("title"), "stage")
    elif action == "add_part":
        title = str((body or {}).get("title") or "").strip()
        if not title:
            raise ApiError(400, "bad_request", "Нужно поле title", {"field": "title"})
        parts = delo.setdefault("result", {}).setdefault("parts", [])
        state = str((body or {}).get("state") or "todo").strip().lower()
        parts.append({"id": "pt_%d" % (len(parts) + 1), "title": title,
                      "state": state if state in PART_STATES else "todo"})
        _timeline(delo, "manager", "В результат добавлена часть: %s" % title, "part")
    elif action == "set_part":
        part = _part(delo, (body or {}).get("part_id") or "")
        state = str((body or {}).get("state") or "").strip().lower()
        if state not in PART_STATES:
            raise ApiError(400, "bad_request", "state: agreed|discussing|todo",
                           {"field": "state", "allowed": list(PART_STATES)})
        part["state"] = state
        _timeline(delo, "you", "Часть «%s»: %s" % (part.get("title"), state), "part")
    elif action == "approve_outbox":
        entry = _outbox(delo, (body or {}).get("outbox_id") or "")
        if entry.get("state") not in ("draft", "failed"):
            raise ApiError(409, "conflict", "Утвердить можно только черновик",
                           {"state": entry.get("state")})
        entry["state"] = "approved"
        entry.pop("error", None)
        _timeline(delo, "you", "Исходящее утверждено — можно отправлять", "outbox")
    elif action == "edit_outbox":
        entry = _outbox(delo, (body or {}).get("outbox_id") or "")
        if entry.get("state") in ("sent", "cancelled"):
            raise ApiError(409, "conflict", "Отправленное и отменённое не правится",
                           {"state": entry.get("state")})
        changed = False
        for field in ("to", "subject", "body"):
            if (body or {}).get(field) is not None:
                value = str((body or {}).get(field))
                if value != str(entry.get(field) or ""):
                    entry[field] = value
                    changed = True
        if (body or {}).get("attachments") is not None:
            entry["attachments"] = [str(path) for path in ((body or {}).get("attachments") or [])]
            changed = True
        if not changed:
            raise ApiError(400, "bad_request", "Нечего править: to, subject, body, attachments",
                           {"field": "body"})
        if entry.get("state") == "approved":
            entry["state"] = "draft"        # правка после «да» — снова на утверждение
        _timeline(delo, "you", "Исходящее поправлено", "outbox")
    elif action == "cancel_outbox":
        entry = _outbox(delo, (body or {}).get("outbox_id") or "")
        if entry.get("state") == "sent":
            raise ApiError(409, "conflict", "Отправленное не отменяется", {"state": "sent"})
        entry["state"] = "cancelled"
        _timeline(delo, "you", "Исходящее отменено", "outbox")
    elif action == "mark_sent":
        entry = _outbox(delo, (body or {}).get("outbox_id") or "")
        if entry.get("state") != "approved":
            raise ApiError(409, "conflict", "Отметить отправленным можно только утверждённое",
                           {"state": entry.get("state")})
        error = str((body or {}).get("error") or "").strip()
        entry["sent_at"] = now()
        if error:
            entry["state"] = "failed"
            entry["error"] = error
            _timeline(delo, "system", "Отправка не удалась: %s" % error, "outbox")
        else:
            entry["state"] = "sent"
            entry.pop("error", None)
            _timeline(delo, "system", "Исходящее отмечено отправленным", "outbox")
    elif action == "pause":
        if delo.get("status") == "paused":
            raise ApiError(409, "conflict", "Дело уже на паузе", {"status": "paused"})
        delo["status"] = "paused"
        _timeline(delo, "you", "Дело поставлено на паузу", "status")
    elif action == "resume":
        if delo.get("status") != "paused":
            raise ApiError(409, "conflict", "Дело не на паузе", {"status": delo.get("status")})
        delo["status"] = "waiting" if delo.get("waiting_on") else "open"
        _timeline(delo, "you", "Дело продолжено", "status")
    elif action == "done":
        if delo.get("status") == "done":
            raise ApiError(409, "conflict", "Дело уже закрыто", {"status": "done"})
        delo["status"] = "done"
        _timeline(delo, "you", "Дело закрыто", "status")
    elif action == "add_waiting":
        who = str((body or {}).get("who") or "").strip()
        what = str((body or {}).get("what") or "").strip()
        if not who or not what:
            raise ApiError(400, "bad_request", "Нужны who и what",
                           {"missing": [field for field, value in (("who", who), ("what", what))
                                        if not value]})
        delo.setdefault("waiting_on", []).append({"who": who, "what": what, "since": now()})
        _timeline(delo, "manager", "Ждём «%s»: %s" % (who, what), "waiting")
    elif action == "clear_waiting":
        entries = delo.get("waiting_on") or []
        who = str((body or {}).get("who") or "").strip()
        cleared = [entry for entry in entries
                   if (body or {}).get("all") is True or (who and entry.get("who") == who)]
        if not cleared:
            raise ApiError(404, "not_found", "Нечего снимать: нет такого ожидания")
        delo["waiting_on"] = [entry for entry in entries if entry not in cleared]
        _timeline(delo, "you", "Снято ожидание: %s" % "; ".join(
            "%s — %s" % (entry.get("who"), entry.get("what")) for entry in cleared), "waiting")
    _touch(delo)
    return {"ok": True, "action": action, "delo": copy.deepcopy(delo)}


__all__ = ["payload", "one", "create", "act", "items", "ACTIONS"]
