"""Ingest: превращает завершённый запрос в «здание проекта» комнаты.

Строит roadmap.json / decisions.json / backlog.json / journal.jsonl / reports/index.json
и зеркало для людей в Vault (STATE.md, Журнал.md) — по-русски, детерминированно.

Идемпотентность: пара (root_id, run_id) помечается в реестре (registry.mark_ingested);
повторный вызов с тем же run ничего не добавляет и не воскрешает закрытые пункты.
"""
from __future__ import annotations

import datetime as _dt

from . import registry
from . import rooms as rooms_mod
from . import statefiles as sf
from .config import journal_md_path, state_md_path

# --- раскладка по файлам: перенесённые имена доступны отсюда, как и раньше ---
from .ingest_common import (IngestError, KINDS, MONTHS_NOM, MONTHS_RU, PHASES, PHASE_IDS, PHASE_STATUSES,
    PHASE_TITLES, STATUSES, STATUS_ORDER, STATUS_RU, _as_int, _backlog_path, _clean,
    _current_phase, _decisions_path, _journal_path, _now, _phase_template,
    _reports_path, _roadmap_path, _room_id, _sha, _state_dir)  # noqa: F401
from .ingest_extract import (_artifact_kind, _canonical, _extract, _meta_from_detail, _needs_normalize,
    _normalizer, _run_id, version_key)  # noqa: F401
from .ingest_merge import (_append_journal, _backlog_incoming, _backlog_rec, _decisions_list, _empty_added,
    _journal_events, _merge_backlog, _merge_decisions, _merge_reports, _merge_roadmap,
    _next_journal_no, _stable_ids, _sync_decision_items, _text_key)  # noqa: F401
from .vault_mirror import (_cell, _fmt_date, _fmt_dt, _fmt_due, _journal_md, _latest_at, _link, _state_md)  # noqa: F401


# --- волна 9: карта проекта из ответа руководителя ---------------------------

def _plan_metadata(detail: dict) -> dict:
    """Сырая metadata версии ответа с блоком plan/plan_patch (последняя по времени)."""
    best = None
    for node in (detail or {}).get("tree") or []:
        if not isinstance(node, dict):
            continue
        meta = node.get("metadata")
        if not isinstance(meta, dict) or not ("plan" in meta or "plan_patch" in meta):
            continue
        key = (_as_int(node.get("finished_at")), str(node.get("id") or ""))
        if best is None or key >= best[0]:
            best = (key, meta)
    return best[1] if best else {}


def _is_brief_request(detail: dict, rec: dict | None) -> bool:
    """Запрос — бриф проекта (флаг в реестре, вид запроса или пометка в detail)."""
    rec = rec if isinstance(rec, dict) else {}
    if str(rec.get("kind") or "") in ("brief", "map_from_existing"):
        return True
    return bool(rec.get("brief") or (detail or {}).get("brief"))


def apply_plan_from_detail(cfg, room, detail: dict, rec: dict | None = None) -> dict:
    """WAVE9 §3: ответ брифа → черновик карты; ответ с plan_patch → правка карты.

    Модуль `server/plan.py` пишется параллельно (PLAN-BE): его отсутствие и любые
    ошибки только добавляют предупреждение — приём запроса не падает.
    """
    result: dict = {"applied": None, "warnings": []}
    meta = _plan_metadata(detail)
    if not meta:
        return result
    try:
        from . import contract as contract_mod
        from . import plan as plan_mod
    except Exception:  # noqa: BLE001 - модуля карты ещё нет (или он сломан) — пропускаем
        return result
    room_id = _room_id(room)
    root_id = str((detail or {}).get("id") or (detail or {}).get("root_id") or "")
    try:
        blocks = contract_mod.parse_plan_metadata(meta, room if isinstance(room, dict) else None)
        result["warnings"] = list(blocks.get("warnings") or [])
        if blocks.get("plan_patch"):
            result["applied"] = "plan_patch"
            result["result"] = plan_mod.apply_patch(cfg, room_id, blocks["plan_patch"],
                                                    source="manager")
        elif blocks.get("plan") and _is_brief_request(detail, rec):
            result["applied"] = "plan"
            result["result"] = plan_mod.ingest_plan(cfg, room_id, root_id, meta)
    except Exception as exc:  # noqa: BLE001 - карта вторична по отношению к приёму ответа
        result["warnings"].append("Карту проекта не удалось сохранить: %s" % exc)
        result["error"] = str(exc)[:300]
    return result


# --- публичный ingest -------------------------------------------------------


def ingest_request(cfg, room, detail: dict) -> dict:
    """Разобрать завершённый запрос в здание проекта. Идемпотентно по (root_id, run_id)."""
    room_id = _room_id(room)
    if not room_id or not isinstance(detail, dict):
        raise IngestError("Нужны комната и detail запроса")
    root_id = str(detail.get("id") or detail.get("root_id") or "").strip()
    if not root_id:
        raise IngestError("В detail нет id запроса")
    title = _clean(detail.get("title")) or root_id
    syn = detail.get("synthesis") or {}
    syn = syn if isinstance(syn, dict) else {}
    if syn.get("state") == "pending" and not detail.get("next_steps") and not detail.get("stage"):
        return {"room": room_id, "root_id": root_id, "run_id": None, "changed": False,
                "added": _empty_added(), "added_counts": {}, "skipped": "not_finished"}
    at = (_as_int(detail.get("finished_at")) or _as_int(syn.get("at"))
          or _as_int(detail.get("created_at")) or _now())
    run_id = version_key(detail)

    rec = registry.get(cfg, room_id, root_id)
    if rec is None:  # карточка могла прийти извне (adopt) — заводим запись, чтобы работал учёт ingest
        registry.add_request(cfg, room_id, {
            "root_id": root_id, "slug": str(detail.get("slug") or ""), "title": title,
            "text": str(detail.get("text") or ""), "created_at": _as_int(detail.get("created_at")) or at,
            "critic": bool(detail.get("critic")), "mode": str(detail.get("mode") or "auto"),
        })
        rec = {}
    is_first = not (rec or {}).get("ingested")
    if run_id in ((rec or {}).get("ingested") or {}):
        return {"room": room_id, "root_id": root_id, "run_id": run_id, "changed": False,
                "added": _empty_added(), "added_counts": {}, "skipped": "already_ingested"}

    extracted = _extract(detail)
    phase_ids, _roadmap = _merge_roadmap(cfg, room_id, root_id, at, extracted["phases"])
    new_decisions, changed_decisions = _merge_decisions(cfg, room_id, root_id, at, extracted["decisions"])
    incoming = _backlog_incoming(extracted, root_id)
    new_items, changed_items = _merge_backlog(cfg, room_id, root_id, title, at, incoming)
    _sync_decision_items(cfg, room_id)
    new_reports = _merge_reports(cfg, room_id, root_id, at, extracted["report"])

    by_kind = {}
    for item in new_items:
        by_kind.setdefault(item["kind"], []).append(item)
    counts = {
        "next_steps": len(by_kind.get("next_step", [])),
        "open_questions": len(by_kind.get("open_question", [])),
        "future_questions": len(by_kind.get("future_question", [])),
        "decisions": len(new_decisions),
        "phases": len(phase_ids),
        "artifacts": len(extracted["artifacts"]),
    }
    events = _journal_events(detail, extracted, root_id, title, at, counts, is_first,
                             (room or {}).get("critic") if isinstance(room, dict) else None)
    for report in new_reports:
        events.append({"at": at, "kind": "report_added", "request_id": root_id, "task_id": root_id,
                       "actor": "system", "text": f"Добавлен отчёт «{report['title']}»",
                       "detail": {"key": report["key"], "path": report["path"]}})
    journal = _append_journal(cfg, room_id, events)
    # отметка — ПОСЛЕ записи: если процесс умрёт посреди ingest, повтор версии доделает
    # работу (слияния идемпотентны), а не пропустит её навсегда
    if not registry.mark_ingested(cfg, room_id, root_id, run_id):
        self_skipped = "raced"
    else:
        self_skipped = None
    # короткий ответ — в реестр: по нему ищет search и показывает лента (без чтения доски)
    registry.update(cfg, room_id, root_id, {
        "summary": _clean(syn.get("text_md"))[:600],
        "answer_at": _as_int(syn.get("at")) or at,
        "version": _as_int(syn.get("version")) or 1,
    })
    rec = registry.get(cfg, room_id, root_id) or {}

    # волна 9 (§3): ответ брифа → черновик карты; ответ с plan_patch → правка карты
    plan_res = apply_plan_from_detail(cfg, room, detail, rec)

    added = _empty_added()
    added["next_steps"] = by_kind.get("next_step", [])
    added["open_questions"] = by_kind.get("open_question", [])
    added["future_questions"] = by_kind.get("future_question", [])
    added["decisions"] = new_decisions
    added["phases"] = phase_ids
    added["artifacts"] = extracted["artifacts"]
    changed = bool(phase_ids or new_decisions or changed_decisions or new_items or changed_items
                   or new_reports or journal)
    return {"room": room_id, "root_id": root_id, "run_id": run_id, "changed": changed,
            "added": added, "added_counts": counts, "skipped": self_skipped,
            "ingested": dict(rec.get("ingested") or {}),
            "warnings": list(detail.get("warnings") or []) + list(plan_res.get("warnings") or []),
            "plan": plan_res}


# --- чтение состояния -------------------------------------------------------

def _phases_public(cfg, room_id: str) -> tuple[list[dict], int, dict | None]:
    data = sf.read_json(_roadmap_path(cfg, room_id), None)
    stored = {}
    if isinstance(data, dict):
        stored = {p.get("id"): p for p in (data.get("phases") or []) if isinstance(p, dict)}
    phases = []
    for phase_id, title in PHASES:
        base = _phase_template(phase_id, title)
        base.update({k: v for k, v in (stored.get(phase_id) or {}).items() if v is not None})
        base["id"], base["title"] = phase_id, (stored.get(phase_id) or {}).get("title") or title
        phases.append(base)
    updated = _as_int(data.get("updated_at")) if isinstance(data, dict) else 0
    return phases, updated, _current_phase(phases)


def _backlog_items(cfg, room_id: str) -> list[dict]:
    data = sf.read_json(_backlog_path(cfg, room_id), None)
    items = data.get("items") if isinstance(data, dict) else []
    return [i for i in (items or []) if isinstance(i, dict)]


def _backlog_counts(items: list[dict]) -> dict:
    counts = {status: 0 for status in STATUSES}
    for item in items:
        status = str(item.get("status") or "open")
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(items)
    return counts


def roadmap_view(cfg, room_id: str) -> dict:
    """Roadmap в форме API §3.5: 6 фаз + решения комнаты."""
    phases, updated, current = _phases_public(cfg, room_id)
    return {"room": str(room_id), "updated_at": updated, "current": current,
            "phases": phases, "decisions": _decisions_list(cfg, room_id)}


def backlog_view(cfg, room_id: str) -> dict:
    """Бэклог в форме API §3.5 + группы «ждёт тебя / шаги / будущие вопросы»."""
    items = _backlog_items(cfg, room_id)
    items.sort(key=lambda i: (STATUS_ORDER.get(str(i.get("status")), 9), str(i.get("due") or "9999-12-31"),
                              _as_int(i.get("created_at")), str(i.get("id"))))
    sections = {
        "inbox": [i for i in items if i.get("status") in ("open", "asked")
                  and i.get("kind") in ("open_question", "decision")],
        "next_steps": [i for i in items if i.get("kind") == "next_step" and i.get("status") in ("open", "asked")],
        "future_questions": [i for i in items if i.get("kind") == "future_question"],
        "decisions": [i for i in items if i.get("kind") == "decision"],
    }
    updated = 0
    for item in items:
        updated = max(updated, _as_int(item.get("updated_at")))
    return {"room": str(room_id), "updated_at": updated, "counts": _backlog_counts(items),
            "items": items, "sections": sections}


def _reports_list(cfg, room_id: str) -> list[dict]:
    data = sf.read_json(_reports_path(cfg, room_id), None)
    items = data.get("items") if isinstance(data, dict) else []
    out = [i for i in (items or []) if isinstance(i, dict)]
    out.sort(key=lambda i: _as_int(i.get("created_at")), reverse=True)
    return out


def journal_view(cfg, room_id: str, limit: int = 50, q: str | None = None) -> dict:
    """Журнал комнаты свежими сверху, с поиском по тексту/виду события."""
    events = sf.read_jsonl(_journal_path(cfg, room_id))
    needle = str(q or "").strip().lower()
    if needle:
        events = [e for e in events if needle in str(e.get("text") or "").lower()
                  or needle in str(e.get("kind") or "").lower()]
    limit = max(1, min(int(limit or 50), 200))
    total = len(events)
    items = list(reversed(events))[:limit]
    return {"room": str(room_id), "items": items,
            "next_cursor": str(items[-1].get("at")) if len(items) < total and items else None,
            "has_more": total > len(items)}


def load_state(cfg, room_id: str) -> dict:
    """Всё «здание проекта» комнаты: дорожная карта, решения, бэклог, журнал, отчёты."""
    return {
        "room": str(room_id),
        "roadmap": roadmap_view(cfg, room_id),
        "decisions": _decisions_list(cfg, room_id),
        "backlog": backlog_view(cfg, room_id),
        "journal": sf.read_jsonl(_journal_path(cfg, room_id))[-500:],
        "reports": _reports_list(cfg, room_id),
    }


# --- действия с бэклогом ----------------------------------------------------

def _find_item(items: list[dict], item_id: str) -> dict | None:
    return next((i for i in items if str(i.get("id")) == str(item_id)), None)


def _local_part(item_id: str) -> str:
    """`<root>:ns_1` → `ns_1`; короткий id возвращается как есть."""
    return str(item_id or "").split(":", 1)[-1]


def ensure_backlog_item(cfg, room_id: str, item_id: str, detail: dict | None = None) -> dict | None:
    """Пункт бэклога по id из метаданных запроса: создать, если watcher ещё не принял.

    Шаги на странице запроса берут id из `RequestDetail.next_steps` (`<root_id>:ns_1`),
    а в бэклоге комнаты они появляются только после ingest. Пока приёма не было,
    отметка «шаг выполнен» падала с 404 — здесь пункт переносится из нормализованных
    метаданных запроса и дальше действие идёт обычным путём.

    `None` — пункта нет ни в бэклоге, ни в запросе (тогда вызывающий отдаёт 404).
    """
    item_id = str(item_id or "").strip()
    if not item_id:
        return None
    found = _find_item(_backlog_items(cfg, room_id), item_id)
    if found is not None:
        return found
    if not isinstance(detail, dict) or ":" not in item_id:
        return None
    root_id = item_id.split(":", 1)[0]
    detail_id = str(detail.get("id") or detail.get("root_id") or "").strip()
    if not root_id or detail_id != root_id:
        return None  # чужой id: элемент не принадлежит этому запросу
    extracted = _extract(detail)
    local = _local_part(item_id)
    target = None
    for raw in _backlog_incoming(extracted, root_id):
        raw_id = str(raw.get("id") or "")
        if raw_id == item_id or _local_part(raw_id) == local:
            target = raw
            break
    if target is None:
        return None
    at = _now()
    title = _clean(detail.get("title")) or root_id
    path = _backlog_path(cfg, room_id)
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = {"room": room_id, "items": [], "updated_at": 0}
        existing = _find_item(data["items"], item_id)
        if existing is not None:
            return existing
        rec = _backlog_rec(item_id, target["kind"], target["text"], target.get("owner"),
                           target.get("due"), target.get("status") or "open", root_id, title, at)
        data["items"].append(rec)
        data["updated_at"] = at
        sf.write_json_atomic(path, data)
    if rec["kind"] == "next_step":
        _append_journal(cfg, room_id, [{
            "at": at, "kind": "step_added", "request_id": root_id, "task_id": root_id,
            "actor": "system", "text": f"Шаг добавлен в бэклог: «{_clean(rec['text'])[:120]}»",
            "detail": {"item_id": item_id, "status": rec["status"], "late": True},
        }])
    return rec


def set_backlog_status(cfg, room_id: str, item_id: str, status: str, due=None, detail=None) -> dict:
    """Сменить статус элемента (и, если передана, дату). Пишет событие в журнал."""
    if status not in STATUSES:
        raise IngestError("status: open|asked|done|dismissed", "bad_request", {"allowed": list(STATUSES)})
    if due is not None:
        try:
            _dt.date.fromisoformat(str(due))
        except ValueError as exc:
            raise IngestError("due: YYYY-MM-DD", "bad_request", {"due": due}) from exc
    path = _backlog_path(cfg, room_id)
    if detail is not None and _find_item(_backlog_items(cfg, room_id), item_id) is None:
        ensure_backlog_item(cfg, room_id, item_id, detail)
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise IngestError("Элемент бэклога не найден", "not_found", {"item_id": item_id})
        target = _find_item(items, item_id)
        if target is None:
            raise IngestError("Элемент бэклога не найден", "not_found", {"item_id": item_id})
        target["status"] = status
        if due is not None:
            target["due"] = str(due) if due else None
        target["updated_at"] = _now()
        data["updated_at"] = target["updated_at"]
        sf.write_json_atomic(path, data)
    kind = "question_answered" if target.get("kind") in ("open_question", "future_question") else "step_done"
    _append_journal(cfg, room_id, [{
        "at": target["updated_at"], "kind": kind, "request_id": target.get("source_request"),
        "task_id": target.get("source_request"), "actor": "mikhail",
        "text": f"«{_clean(target.get('text'))[:120]}» → {STATUS_RU.get(status, status)}",
        "detail": {"item_id": item_id, "status": status},
    }])
    return target


def mark_asked(cfg, room_id: str, item_id: str, root_id: str | None = None) -> dict:
    """Пометить элемент «задан»: статус asked и ссылка на созданный запрос."""
    path = _backlog_path(cfg, room_id)
    with sf.lock_for(path):
        data = sf.read_json(path, None)
        items = data.get("items") if isinstance(data, dict) else None
        target = _find_item(items or [], item_id)
        if target is None:
            raise IngestError("Элемент бэклога не найден", "not_found", {"item_id": item_id})
        if target.get("status") in ("open", "asked"):
            target["status"] = "asked"
        if root_id:
            target["asked_request"] = str(root_id)
        target["updated_at"] = _now()
        data["updated_at"] = target["updated_at"]
        sf.write_json_atomic(path, data)
    return target


def ask_item(cfg, room_id: str, item_id: str, text: str | None = None, detail: dict | None = None) -> str:
    """Текст нового запроса из элемента бэклога (карточку создаёт вызывающий)."""
    item = _find_item(_backlog_items(cfg, room_id), item_id)
    if item is None and detail is not None:
        item = ensure_backlog_item(cfg, room_id, item_id, detail)
    if item is None:
        raise IngestError("Элемент бэклога не найден", "not_found", {"item_id": item_id})
    body = _clean(text) or _clean(item.get("text"))
    kind = item.get("kind")
    lead = {
        "next_step": "Следующий шаг по проекту",
        "future_question": "Вопрос на будущее по проекту",
        "open_question": "Вопрос, который ждёт решения",
        "decision": "Решение, которое нужно принять",
    }.get(kind, "Пункт бэклога")
    source = _clean(item.get("source_title")) or str(item.get("source_request") or "")
    lines = [f"{lead}: {body}", "", f"Источник: «{source}» ({item.get('source_request')})."]
    if item.get("owner"):
        lines.append(f"Владелец: {item['owner']}.")
    if item.get("due"):
        lines.append(f"Срок: {item['due']}.")
    lines.append("Нужен разбор и следующий шаг, как в прошлый раз.")
    return "\n".join(lines).strip()


# --- зеркало для людей в Vault ---------------------------------------------


def write_vault_mirror(cfg, room_id: str, dry_run: bool = False) -> bool:
    """Записать <vault_dir>/<room.id>/STATE.md и Журнал.md. True — содержимое изменилось.

    Пути — из `config.state_md_path/journal_md_path` (их же отдаёт contract агентам).
    Детерминированно (никаких «сейчас»), сравнение по байтам: без изменений файл
    не перезаписывается. `dry_run=True` — только посчитать, изменилось бы ли.
    Если зеркало выключено (Vault не найден), тихо ничего не делает.
    """
    if not getattr(cfg, "vault_enabled", True):
        return False
    room = rooms_mod.get_room(cfg, room_id) or {}
    title = str(room.get("title") or room_id)
    state = load_state(cfg, room_id)
    files = {
        state_md_path(cfg, room_id): _state_md(cfg, room_id, title, state),
        journal_md_path(cfg, room_id): _journal_md(cfg, room_id, title, state),
    }
    changed = False
    for path, text in files.items():
        if dry_run:
            try:
                current = path.read_bytes()
            except OSError:
                current = None
            changed = changed or current != text.encode("utf-8")
        else:
            changed = sf.write_text_if_changed(path, text) or changed
    return changed
