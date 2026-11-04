"""Ingest: идемпотентное слияние версии ответа в «здание проекта» (дорожная карта, решения, бэклог, журнал, отчёты).

Каждая функция читает/пишет свой файл `data/state/<room>/…` через `statefiles` и возвращает,
что добавилось; повторный вызов с теми же данными ничего не меняет.
"""
from __future__ import annotations

import re
from pathlib import Path

from .ingest_common import (PHASES,
        STATUSES,
        _as_int,
        _backlog_path,
        _clean,
        _current_phase,
        _decisions_path,
        _journal_path,
        _now,
        _phase_template,
        _reports_path,
        _roadmap_path)
from . import statefiles as sf
from . import rooms as rooms_mod

def _merge_roadmap(cfg, room_id: str, root_id: str, at: int, phases: list[dict]) -> tuple[list[str], dict]:
    path = _roadmap_path(cfg, room_id)
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        if not isinstance(data, dict):
            data = {"room": room_id, "phases": [], "updated_at": 0}
        stored = {p.get("id"): p for p in (data.get("phases") or []) if isinstance(p, dict)}
        ordered = []
        for phase_id, title in PHASES:
            base = _phase_template(phase_id, title)
            base.update({k: v for k, v in (stored.get(phase_id) or {}).items() if v is not None})
            base["id"], base["title"] = phase_id, (stored.get(phase_id) or {}).get("title") or title
            ordered.append(base)
        changed: list[str] = []
        for incoming in phases:
            current = next((p for p in ordered if p["id"] == incoming["id"]), None)
            if current is None:
                continue
            if incoming["status"] == "next" and not incoming["note"]:
                continue  # «заглушка» контракта: запрос ничего не сказал об этой фазе
            if at < _as_int(current.get("source_at")):
                continue  # более свежий запрос уже описал эту фазу
            if (current.get("status") != incoming["status"] or current.get("note") != incoming["note"]
                    or current.get("source_request") != root_id):
                changed.append(incoming["id"])
            current.update({"status": incoming["status"], "note": incoming["note"],
                            "updated_at": at or None, "source_request": root_id, "source_at": at})
        new_data = {"room": room_id, "updated_at": at or data.get("updated_at") or 0,
                    "current": _current_phase(ordered), "phases": ordered}
        if changed or new_data != data:
            sf.write_json_atomic(path, new_data)
    return changed, new_data


def _text_key(text) -> str:
    """Нормализованный текст для стабильных id: пробелы, регистр, знаки."""
    return " ".join(str(_clean(text) or "").lower().split())


def _stable_ids(incoming: list[dict], stored: dict, root_id: str, kinds: tuple = (),
                prefixed: bool = True) -> list[dict]:
    """Стабильные id элементов между версиями ответа (без дублей).

    Два правила по порядку:

    1. **тот же текст** того же вида в том же запросе → тот же id (нормализатор
       выдаёт id по тексту, поэтому совпавший пункт не должен менять id);
    2. иначе — **та же позиция** среди элементов этого вида: доработка переписала
       пункт, и он обновляет прежний элемент на месте, а не плодит копию.

    ``prefixed`` — id в хранилище полные (``<root>:ns_x``) или короткие (``d_x``).
    """
    def short(item_id) -> str:
        item_id = str(item_id or "")
        if not prefixed and item_id.startswith(root_id + ":"):
            return item_id[len(root_id) + 1:]
        return item_id

    by_text: dict = {}
    by_pos: dict = {}
    for item in stored.values():
        if not isinstance(item, dict) or item.get("source_request") != root_id:
            continue
        if kinds and item.get("kind") not in kinds:
            continue
        kind = item.get("kind")
        by_text.setdefault((kind, _text_key(item.get("text"))), short(item.get("id")))
        by_pos.setdefault(kind, []).append(item)
    for kind, items in by_pos.items():
        items.sort(key=lambda i: (_as_int(i.get("created_at")), str(i.get("id"))))
        by_pos[kind] = [short(i.get("id")) for i in items]
    used: set = set()
    seen: dict = {}
    out = []
    for raw in incoming:
        raw = dict(raw)
        kind = raw.get("kind")
        idx = seen.get(kind, 0)
        seen[kind] = idx + 1
        same = by_text.get((kind, _text_key(raw.get("text"))))
        if same and same not in used:
            raw["id"] = same
        else:
            positions = by_pos.get(kind) or []
            if idx < len(positions) and positions[idx] not in used:
                raw["id"] = positions[idx]
        used.add(raw.get("id"))
        out.append(raw)
    return out


def _merge_decisions(cfg, room_id: str, root_id: str, at: int, decisions: list[dict]) -> tuple[list[dict], list[str]]:
    path = _decisions_path(cfg, room_id)
    added, changed = [], []
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
            data = {"room": room_id, "decisions": [], "updated_at": 0}
        stored = {d.get("id"): d for d in data["decisions"] if isinstance(d, dict)}
        decisions = _stable_ids(decisions, stored, root_id, (), prefixed=False)
        for raw in decisions:
            item_id = f"{root_id}:{raw['id']}"
            old = stored.get(item_id)
            if old is None:
                rec = {"id": item_id, "text": raw["text"], "why": raw["why"], "status": raw["status"],
                       "owner": raw.get("owner") or "mikhail", "at": at or None, "updated_at": at or None,
                       "source_request": root_id}
                data["decisions"].append(rec)
                stored[item_id] = rec
                added.append(rec)
                continue
            new_status = old.get("status")
            if raw["status"] == "accepted" and new_status == "proposed":
                new_status = "accepted"
            elif raw["status"] == "rejected" and new_status in ("proposed", "accepted"):
                new_status = "rejected"
            updates = {}
            if new_status != old.get("status"):
                updates["status"] = new_status
            if at >= _as_int(old.get("updated_at")) and raw["text"] != old.get("text"):
                updates["text"] = raw["text"]
            if raw.get("why") and at >= _as_int(old.get("updated_at")) and raw["why"] != old.get("why"):
                updates["why"] = raw["why"]
            if updates:
                old.update(updates)
                old["updated_at"] = at or old.get("updated_at")
                changed.append(item_id)
        if added or changed:
            data["room"] = room_id
            data["updated_at"] = at or data.get("updated_at") or 0
            sf.write_json_atomic(path, data)
    return added, changed


def _backlog_rec(item_id: str, kind: str, text: str, owner, due, status: str, root_id: str,
                 source_title: str, at: int) -> dict:
    return {"id": item_id, "kind": kind, "text": text, "owner": owner, "due": due, "status": status,
            "source_request": root_id, "source_title": source_title, "asked_request": None,
            "created_at": at or _now(), "updated_at": at or _now()}


def _merge_backlog(cfg, room_id: str, root_id: str, source_title: str, at: int,
                   incoming: list[dict]) -> tuple[list[dict], list[str]]:
    path = _backlog_path(cfg, room_id)
    added, changed = [], []
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = {"room": room_id, "items": [], "updated_at": 0}
        stored = {i.get("id"): i for i in data["items"] if isinstance(i, dict)}
        incoming = _stable_ids(incoming, stored, root_id, ())
        for raw in incoming:
            old = stored.get(raw["id"])
            if old is None:
                rec = _backlog_rec(raw["id"], raw["kind"], raw["text"], raw.get("owner"), raw.get("due"),
                                   raw["status"], root_id, source_title, at)
                data["items"].append(rec)
                stored[raw["id"]] = rec
                added.append(rec)
                continue
            if old.get("status") in ("done", "dismissed"):
                continue  # закрытое не воскрешаем
            updates = {}
            if raw["text"] and raw["text"] != old.get("text"):
                updates["text"] = raw["text"]
            if raw.get("owner") and raw["owner"] != old.get("owner"):
                updates["owner"] = raw["owner"]
            if raw.get("due") and raw["due"] != old.get("due"):
                updates["due"] = raw["due"]
            new_status = raw["status"]
            if new_status == "done" and old.get("status") in ("open", "asked"):
                updates["status"] = "done"
            elif new_status == "asked" and old.get("status") == "open":
                updates["status"] = "asked"
            if updates:
                old.update(updates)
                old["updated_at"] = at or _now()
                changed.append(raw["id"])
        if added or changed:
            data["room"] = room_id
            data["updated_at"] = at or data.get("updated_at") or 0
            sf.write_json_atomic(path, data)
    return added, changed


def _backlog_incoming(extracted: dict, root_id: str) -> list[dict]:
    items = []
    for step in extracted["next_steps"]:
        status = step["state"] if step["state"] in STATUSES else "open"
        items.append({"id": f"{root_id}:{step['id']}", "kind": "next_step", "text": step["text"],
                      "owner": step["owner"], "due": step["due"], "status": status})
    for q in extracted["open_questions"]:
        status = "done" if q["answered"] else (q.get("state") if q.get("state") in STATUSES else "open")
        items.append({"id": f"{root_id}:{q['id']}", "kind": "open_question", "text": q["text"],
                      "owner": q["for"], "due": None, "status": status})
    for fq in extracted["future_questions"]:
        items.append({"id": f"{root_id}:{fq['id']}", "kind": "future_question", "text": fq["text"],
                      "owner": "mikhail", "due": None, "status": "open"})
    for d in extracted["decisions"]:
        status = "dismissed" if d["status"] == "rejected" else "open"
        items.append({"id": f"{root_id}:{d['id']}", "kind": "decision", "text": d["text"],
                      "owner": d.get("owner") or "mikhail", "due": None, "status": status})
    return items


def _sync_decision_items(cfg, room_id: str) -> None:
    """Принятые решения закрывают свои пункты бэклога (id решения = id пункта)."""
    decisions = {d.get("id"): d for d in _decisions_list(cfg, room_id)}
    path = _backlog_path(cfg, room_id)
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        changed = False
        for item in items:
            if not isinstance(item, dict) or item.get("kind") != "decision":
                continue
            decision = decisions.get(item.get("id"))
            if not decision or item.get("status") not in ("open", "asked"):
                continue
            if decision.get("status") == "accepted":
                item["status"] = "done"
            elif decision.get("status") == "rejected":
                item["status"] = "dismissed"
            else:
                continue
            item["updated_at"] = _now()
            changed = True
        if changed:
            data["updated_at"] = _now()
            sf.write_json_atomic(path, data)


def _next_journal_no(events: list[dict]) -> int:
    number = 0
    for event in events:
        match = re.fullmatch(r"j_(\d+)", str(event.get("id") or ""))
        if match:
            number = max(number, int(match.group(1)))
    return number + 1


def _append_journal(cfg, room_id: str, events: list[dict]) -> list[dict]:
    """Дописать события, выдав им последовательные id j_0001… (под блокировкой файла)."""
    path = _journal_path(cfg, room_id)
    written: list[dict] = []
    if not events:
        return written
    with sf.lock_for(path):
        number = _next_journal_no(sf.read_jsonl(path))
        for event in events:
            rec = {"id": f"j_{number:04d}", "room": room_id, **event}
            sf.append_jsonl(path, rec)
            written.append(rec)
            number += 1
    return written


def _journal_events(detail: dict, extracted: dict, root_id: str, title: str, at: int,
                    counts: dict, is_first: bool, critic_profile: str | None) -> list[dict]:
    syn = detail.get("synthesis") or {}
    syn = syn if isinstance(syn, dict) else {}
    events: list[dict] = []
    if is_first:
        events.append({
            "at": _as_int(detail.get("created_at")) or at, "kind": "request_created",
            "request_id": root_id, "task_id": root_id, "actor": "mikhail",
            "text": f"Новый запрос: «{title}»", "detail": {"mode": detail.get("mode") or "auto"},
        })
    version = _as_int(syn.get("version")) or 1
    if syn.get("state") == "ready" or syn.get("text_md"):
        suffix = f" (версия {version})" if version > 1 else ""
        events.append({
            "at": at, "kind": "request_done", "request_id": root_id, "task_id": root_id,
            "actor": str(syn.get("author") or "system"),
            "text": f"Готов ответ по запросу «{title}»{suffix}",
            "detail": {"version": version, "source_task": syn.get("source_task")},
        })
    critic = detail.get("critic") or {}
    if isinstance(critic, dict) and critic.get("enabled") and critic.get("verdict"):
        ok = critic.get("verdict") == "approve"
        issues = critic.get("issues") or []
        events.append({
            "at": at, "kind": "critic", "request_id": root_id,
            "task_id": critic.get("task_id") or root_id,
            "actor": str(critic.get("profile") or critic_profile or "critic"),
            "text": "Критик: согласовано" if ok else f"Критик: на доработку ({len(issues)} замеч.)",
            "detail": {"verdict": critic.get("verdict"), "round": critic.get("round")},
        })
    for step in syn.get("history") or []:
        if isinstance(step, dict) and step.get("kind") == "revision":
            note = _clean(step.get("note")) or "по замечаниям"
            events.append({
                "at": _as_int(step.get("at")) or at, "kind": "revision", "request_id": root_id,
                "task_id": step.get("task_id") or root_id, "actor": str(step.get("author") or "system"),
                "text": f"Доработка v{step.get('version') or version}: {note}",
                "detail": {"version": step.get("version")},
            })
    events.append({
        "at": at, "kind": "ingest", "request_id": root_id, "task_id": root_id, "actor": "system",
        "text": f"Здание проекта обновлено: шагов {counts.get('next_steps', 0)}, "
                f"вопросов {counts.get('open_questions', 0) + counts.get('future_questions', 0)}, "
                f"решений {counts.get('decisions', 0)}, фаз {counts.get('phases', 0)}.",
        "detail": dict(counts),
    })
    return events


def _merge_reports(cfg, room_id: str, root_id: str, at: int, report: dict | None) -> list[dict]:
    if not report or not report.get("path"):
        return []
    path = _reports_path(cfg, room_id)
    title = report.get("title") or Path(report["path"]).stem
    key = rooms_mod.slugify(title) or root_id
    blocks = 0
    if report.get("kind") == "spec":
        spec = sf.read_json(report["path"], None)
        if isinstance(spec, dict) and isinstance(spec.get("blocks"), list):
            blocks = len(spec["blocks"])
    item = {"key": key, "title": title, "subtitle": "", "kind": report.get("kind") or "spec",
            "path": report["path"], "request_id": root_id, "blocks": blocks,
            "created_at": at or _now(), "updated_at": at or _now(),
            "exists": Path(report["path"]).is_file()}
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = {"room": room_id, "items": [], "updated_at": 0}
        stored = {i.get("key"): i for i in data["items"] if isinstance(i, dict)}
        old = stored.get(key)
        if old is not None and old.get("path") != item["path"]:
            key = f"{key}-{root_id.replace('t_', '')[:6]}"
            item["key"] = key
            old = stored.get(key)
        if old is None:
            data["items"].append(item)
            data["updated_at"] = at or data.get("updated_at") or 0
            data["room"] = room_id
            sf.write_json_atomic(path, data)
            return [item]
        if {k: v for k, v in old.items() if k != "updated_at"} == {k: v for k, v in item.items() if k != "updated_at"}:
            return []
        old.update({k: v for k, v in item.items() if k != "created_at"})
        data["updated_at"] = at or data.get("updated_at") or 0
        sf.write_json_atomic(path, data)
    return []


def _empty_added() -> dict:
    return {"next_steps": [], "open_questions": [], "decisions": [], "future_questions": [],
            "phases": [], "artifacts": []}


def _decisions_list(cfg, room_id: str) -> list[dict]:
    data = sf.read_json(_decisions_path(cfg, room_id), None)
    items = data.get("decisions") if isinstance(data, dict) else []
    out = [d for d in (items or []) if isinstance(d, dict)]
    out.sort(key=lambda d: (_as_int(d.get("at")), str(d.get("id"))))
    return out
