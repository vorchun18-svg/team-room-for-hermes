"""Чтение канбана: дерево карточек запроса, ответ (версии), критик, статус и хронология.

Статус запроса вычисляется здесь (`_status`, порядок проверок — docs/API.md §4);
metadata приводится к канону через `server.normalize` (`_canon`).
"""
from __future__ import annotations

import sqlite3

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .engine_db import (ACTIVE,
            CLOSED,
            EVENT_TEXT,
            EngineError,
            FAILED_STATUS,
            MAX_DEPTH,
            MAX_NODES,
            _chunks,
            _comment_rows,
            _json,
            _require_tables,
            _run_rows,
            _task_rows)
    from . import read_view as _view
except ImportError:  # pragma: no cover
    from engine_db import (ACTIVE,
            CLOSED,
            EVENT_TEXT,
            EngineError,
            FAILED_STATUS,
            MAX_DEPTH,
            MAX_NODES,
            _chunks,
            _comment_rows,
            _json,
            _require_tables,
            _run_rows,
            _task_rows)  # type: ignore
    import read_view as _view  # type: ignore

_as_list = _view.as_list
# ------------------------------------------------------- нормализация metadata ---
_NORMALIZE, _TRIED = None, False


def _canon(meta, summary_md=None) -> dict:
    """Каноническая форма metadata. Единственная реализация — server/normalize.py."""
    global _NORMALIZE, _TRIED
    if not _TRIED:
        _TRIED = True
        for mod in ("server.normalize", "normalize"):
            try:
                fn = getattr(__import__(mod, fromlist=["normalize_metadata"]), "normalize_metadata")
                if callable(fn):
                    _NORMALIZE = fn
                    break
            except Exception:  # noqa: BLE001 — модуль ещё не написан
                continue
    if _NORMALIZE is None:
        return {}
    try:
        out = _NORMALIZE(meta, summary_md)
    except TypeError:
        try:
            out = _NORMALIZE(meta)
        except Exception:  # noqa: BLE001
            return {}
    except Exception:  # noqa: BLE001 — нормализатор не должен ронять чтение
        return {}
    return out if isinstance(out, dict) else {}


def _canons(nodes: list) -> list:
    """Пары (узел, каноническая metadata) для карточек, у которых metadata непустая."""
    return [(n, _canon(_raw_meta(n), n.get("summary_md"))) for n in nodes if _raw_meta(n)]


def _legacy(meta: dict, canon: dict) -> bool:
    """Старый формат: строки в next_steps / stage словарём «область → статус»."""
    flag = canon.get("legacy", canon.get("legacy_metadata"))
    if isinstance(flag, bool):
        return flag
    steps = meta.get("next_steps")
    if isinstance(steps, list) and any(isinstance(s, str) for s in steps):
        return True
    stage = meta.get("stage")
    return isinstance(stage, dict) and not isinstance(stage.get("phases"), list)


# --------------------------------------------------------------------- дерево ---
def _collect(con, root_id: str) -> dict:
    """Корень и всё дерево в обе стороны (task_links.child_id=root ∪ parent_id=root)."""
    _require_tables(con, "tasks", "task_links")
    tasks = _task_rows(con, [root_id])
    if root_id not in tasks:
        raise EngineError("not_found", f"Карточка {root_id} не найдена на этой доске.", {"task_id": root_id})
    links, seen, depth, parents = [], {root_id}, {root_id: 0}, {root_id: []}
    frontier = [root_id]
    for level in range(MAX_DEPTH):
        if not frontier or len(seen) >= MAX_NODES:
            break
        ph = ",".join("?" * len(frontier))
        try:
            rows = con.execute(f"SELECT parent_id, child_id FROM task_links "
                               f"WHERE parent_id IN ({ph}) OR child_id IN ({ph})",
                               frontier + frontier).fetchall()
        except sqlite3.Error:
            rows = []
        fresh = []
        for row in rows:
            if (row["parent_id"], row["child_id"]) not in links:
                links.append((row["parent_id"], row["child_id"]))
            fresh += [i for i in (row["parent_id"], row["child_id"]) if i and i not in seen]
        fresh = [i for i in dict.fromkeys(fresh)][: max(0, MAX_NODES - len(seen))]
        if not fresh:
            break
        tasks.update(_task_rows(con, fresh))
        fresh = [i for i in fresh if i in tasks]
        for nid in fresh:
            depth[nid] = level + 1
            parents[nid] = [a if b == nid else b for a, b in links
                            if nid in (a, b) and depth.get(a if b == nid else b) == level]
        seen.update(fresh)
        frontier = fresh
    order = sorted(seen & set(tasks), key=lambda i: (depth.get(i, 99), tasks[i].get("created_at") or 0, i))
    ids = [root_id] + [i for i in order if i != root_id]
    return {"root_id": root_id, "tasks": tasks, "ids": ids, "depth": depth, "parents": parents,
            "links": links, "runs": _run_rows(con, ids), "comments": _comment_rows(con, ids)}


def _run_ok(run: dict) -> bool:
    return run.get("status") in ("done", "completed") or run.get("outcome") == "completed"


def _last_ok(runs: list):
    ok = [r for r in runs if _run_ok(r)]
    return ok[-1] if ok else None


def _node_error(runs: list):
    bad = [r for r in runs if r.get("status") in FAILED_STATUS or r.get("outcome") in FAILED_STATUS
           or r.get("error")]
    if not bad:
        return None
    run = bad[-1]
    return {"code": run.get("status") or run.get("outcome") or "error",
            "message": (run.get("error") or "исполнитель не смог завершить работу").strip()[:500]}


def _run_out(run: dict) -> dict:
    start, end = run.get("started_at"), run.get("ended_at")
    return {"id": run.get("id"), "profile": run.get("profile"), "status": run.get("status"),
            "outcome": run.get("outcome"), "started_at": start, "ended_at": end,
            "summary": run.get("summary") or "", "error": run.get("error"),
            "metadata": _json(run.get("metadata")),
            "duration_s": (end - start) if isinstance(start, int) and isinstance(end, int) and end >= start else None}


def _raw_meta(node: dict) -> dict:
    meta = node.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _last_at(node: dict):
    for run in reversed(node.get("runs") or []):
        if run.get("ended_at"):
            return run["ended_at"]
    return None


def _changes_of(meta: dict) -> list:
    """`metadata.changes` руководителя → список строк (W17-S §3, «Что изменилось»).

    Принимает `["пункт", …]`, `[{"text": …}]` и одну строку; лишнее отбрасывает.
    """
    raw = (meta or {}).get("changes") if isinstance(meta, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        text = item.get("text") if isinstance(item, dict) else item
        text = " ".join(str(text or "").split())
        if not text:
            continue
        out.append(text if len(text) <= 300 else text[:299].rstrip() + "…")
        if len(out) >= 10:
            break
    return out


def _synth_title(title: str) -> bool:
    low = (title or "").lower()
    return any(k in low for k in ("свест", "сводн", "synth"))


#: короткие подписи уровней мышления для `model_label` (совпадают с agent_models.EFFORT_SHORT)
_EFFORT_SHORT = {"none": "без размышления", "minimal": "минимальные", "low": "низкие",
                 "medium": "средние", "high": "высокие", "xhigh": "очень высокие",
                 "max": "максимальные", "ultra": "предельные"}


def _model_label(task: dict):
    """«провайдер · модель · мышление» по факту карточки (WAVE10); None — переопределения нет."""
    provider = str((task or {}).get("provider_override") or "")
    model = str((task or {}).get("model_override") or "")
    effort = str((task or {}).get("reasoning_effort") or "")
    if not (provider or model or effort):
        return None
    parts = [p for p in (provider, model) if p]
    if effort:
        parts.append(_EFFORT_SHORT.get(effort.lower(), effort))
    return " · ".join(parts)


def _model_sent(cfg, room, task: dict, runs: list, board: str | None = None):
    """«Ушло на: …» карточки по факту запуска (W13-A). None — сказать нечего/модуля нет.

    Модель берётся из сессии воркера профиля (её id лежит в `task_runs.metadata`), а не
    из подписи карточки: подпись показывает выбор, факт — только запуск. Если в metadata
    прогона id сессии нет (старые записи), `sent_of` берёт его из события `owner_admitted`
    — поэтому доска передаётся дальше.
    """
    if cfg is None or not isinstance(task, dict):
        return None
    try:
        try:
            from . import agent_models as models_mod
        except ImportError:  # pragma: no cover - «плоский» импорт
            import agent_models as models_mod  # type: ignore
        last = next((r for r in reversed(runs or []) if r.get("id") is not None), None)
        value = models_mod.sent_of(cfg, room, task, run=last, board=board)
        return value or None
    except Exception:  # noqa: BLE001 - подпись модели не должна ронять дерево
        return None


def _roles(ctx: dict, extra_cards: list) -> dict:
    extra = {c["id"]: c for c in extra_cards if c.get("id")}
    roles, rounds = {}, {}
    for tid in ctx["ids"]:
        task = ctx["tasks"].get(tid) or {}
        if tid == ctx["root_id"]:
            roles[tid] = "root"
        elif tid in extra:
            roles[tid] = extra[tid].get("kind") if extra[tid].get("kind") in ("critic", "revision") else "worker"
            rounds[tid] = extra[tid].get("round")
        elif _synth_title(task.get("title", "")) and ctx["depth"].get(tid) == 1:
            roles[tid] = "synthesizer"
        else:
            roles[tid] = "worker"
    return {"roles": roles, "rounds": rounds}


def _room_dict(cfg, room):
    """Комната как dict: принять dict или id комнаты (app.py передаёт id) — иначе None."""
    if isinstance(room, dict):
        return room
    if isinstance(room, str) and room:
        try:
            try:
                from . import rooms as rooms_mod
            except ImportError:
                import rooms as rooms_mod  # type: ignore
            found = rooms_mod.get_room(cfg, room)
            return found if isinstance(found, dict) else None
        except Exception:  # noqa: BLE001 — реестр комнат не обязателен для чтения канбана
            return None
    return None


def _tree(ctx: dict, room, extra_cards: list, cfg=None, board=None) -> list:
    meta = _roles(ctx, extra_cards)
    allowed = set()
    if isinstance(room, dict):
        for member in _as_list(room.get("members")):
            if isinstance(member, dict) and member.get("profile"):
                allowed.add(member["profile"])
            elif isinstance(member, str):
                allowed.add(member)
        allowed |= {room[k] for k in ("orchestrator", "critic") if room.get(k)}
    nodes = []
    orchestrator = (room or {}).get("orchestrator") if isinstance(room, dict) else None
    # корень часто не «клеймится» движком: работой над запросом считаем начало его детей
    child_starts = [ctx["tasks"][tid].get("started_at") for tid in ctx["ids"]
                    if tid != ctx["root_id"] and isinstance(ctx["tasks"][tid].get("started_at"), int)]
    root_started = min(child_starts) if child_starts else None
    for tid in ctx["ids"]:
        task, runs = ctx["tasks"][tid], [_run_out(r) for r in ctx["runs"].get(tid, [])]
        summary, raw_meta = _pick_run(runs)
        assignee = task.get("assignee")
        if tid == ctx["root_id"] and not assignee:
            assignee = orchestrator  # запрос ведёт директор комнаты, даже если карточку ещё не взяли
        started_at = task.get("started_at")
        if tid == ctx["root_id"] and not isinstance(started_at, int):
            started_at = root_started
        node = {"id": tid, "parent_ids": ctx["parents"].get(tid, []), "title": task.get("title") or "",
                "assignee": assignee, "role": meta["roles"].get(tid, "worker"),
                "status": task.get("status") or "todo", "started_at": started_at,
                "finished_at": task.get("completed_at"), "summary_md": summary,
                "metadata": raw_meta, "comments": ctx["comments"].get(tid, []),
                "runs": runs, "needs_human": _needs_human_node(task, ctx, tid), "error": _node_error(runs),
                "model_label": _model_label(task),
                "model_sent": _model_sent(cfg, room, task, runs, board),
                "outside_room": bool(allowed) and bool(task.get("assignee")) and task.get("assignee") not in allowed}
        if meta["rounds"].get(tid) is not None:
            node["round"] = meta["rounds"][tid]
        nodes.append(node)
    return nodes


def _pick_run(runs: list) -> tuple:
    """summary и metadata последних непустых run'ов карточки (metadata бывает и у blocked/running)."""
    summary = next((r.get("summary") for r in reversed(runs) if r.get("summary")), "")
    meta = next((r.get("metadata") for r in reversed(runs) if r.get("metadata")), {})
    return summary, (meta if isinstance(meta, dict) else {})


def _needs_human_node(task: dict, ctx: dict, tid: str) -> bool:
    if (task.get("status") or "") != "blocked":
        return False
    if "human" in str(task.get("block_kind") or "").lower():
        return True
    parent_ids = [p for p in ctx["parents"].get(tid, []) if p in ctx["tasks"]]
    return all((ctx["tasks"][p].get("status") or "") in CLOSED for p in parent_ids)


# ---------------------------------------------- synthesis / версии / критик ---
def _synthesis(ctx: dict, nodes: list, extra_cards: list) -> tuple:
    """Вернуть (synthesis, каноническая metadata показанной версии, warnings)."""
    warnings, history = [], []
    by_id = {n["id"]: n for n in nodes}
    root_done = (by_id.get(ctx["root_id"], {}).get("status") or "") in CLOSED
    src_id = ctx["root_id"] if by_id.get(ctx["root_id"], {}).get("summary_md") else next(
        (n["id"] for n in nodes if n["role"] == "synthesizer" and n.get("summary_md")), None)
    base = by_id.get(src_id) if src_id else None
    canon = {}
    if base:
        canon = _canon(_raw_meta(base), base.get("summary_md"))
        history.append({"version": 1, "kind": "initial", "task_id": base["id"],
                        "author": base.get("assignee"), "at": _last_at(base), "verdict": None, "note": "",
                        "reason": "initial", "changes": _changes_of(_raw_meta(base))})
    revisions = sorted([c for c in extra_cards if c.get("kind") == "revision" and c.get("id") in by_id],
                       key=lambda c: (c.get("at") or 0, str(c.get("id"))))
    for card in revisions:
        node = by_id[card["id"]]
        if node.get("summary_md"):
            # W17-S §3: у версии есть причина («ваше замечание» / «ответы / критика»)
            # и список «что изменилось» — из реестра карточки доработки и/или metadata.
            raw = _raw_meta(node) if isinstance(_raw_meta(node), dict) else {}
            history.append({"version": len(history) + 1, "kind": "revision", "task_id": node["id"],
                            "author": node.get("assignee"), "at": _last_at(node), "verdict": None,
                            "note": str(card.get("note") or "") or "Доработка после замечаний",
                            "reason": str(card.get("reason") or ""),
                            "answers_count": int(card.get("answers_count") or 0),
                            "changes": _changes_of(raw) or list(card.get("changes") or [])})
    critics = [n for n in nodes if n["role"] == "critic"]
    for i, entry in enumerate(history):
        nxt = history[i + 1]["at"] if i + 1 < len(history) else None
        for cnode in critics:
            at = _last_at(cnode)
            if at is None or (entry["at"] or 0) > at or (nxt is not None and at >= nxt):
                continue
            verdict = str(_canon(_raw_meta(cnode), cnode.get("summary_md")).get("verdict") or "").lower()
            if verdict in ("approve", "accept", "accepted", "approve_with_notes", "revise"):
                entry["verdict"] = "approve" if verdict.startswith(("approve", "accept")) else "revise"
                entry["note"] = ("Принято критиком" if entry["verdict"] == "approve"
                                 else "Учтены замечания критика")
    last_node = None
    if history:
        last = history[-1]
        last_node = by_id[last["task_id"]]
        canon = _canon(_raw_meta(last_node), last_node.get("summary_md"))
        text, version, source, author, at, state = (last_node.get("summary_md") or "", last["version"],
                                                    last["task_id"], last_node.get("assignee"), last["at"], "ready")
    else:
        text, version, source, author, at = "", 0, None, None, None
        # API §3.4: `none` — завершён, но текста нет (прежнее имя `missing` снято)
        state = "none" if root_done else "pending"
    legacy = _legacy(_raw_meta(last_node or {}), canon)
    if legacy:
        warnings.append({"code": "legacy_metadata",
                         "message": "Ответ карточки в старом формате metadata — показан нормализованным.",
                         "detail": {"source_task": source}})
    if state == "none":
        warnings.append({"code": "no_synthesis", "message": "Запрос завершён, но текста ответа в run'ах нет.",
                         "detail": {"rid": ctx["root_id"]}})
    return ({"state": state, "version": version, "text_md": text, "author": author, "at": at,
             "source_task": source, "legacy": legacy, "history": history}, canon, warnings)


def _critic_block(nodes: list, extra_cards: list, room, ctx: dict, enabled=None) -> dict:
    cards = [c for c in extra_cards if c.get("kind") == "critic" and c.get("id")]
    critics = [n for n in nodes if n["role"] == "critic"]
    if enabled is None:
        enabled = bool(room.get("critic")) if isinstance(room, dict) else bool(cards or critics)
    enabled = bool(enabled) or bool(cards or critics)
    max_rounds = int((room or {}).get("max_rounds") or 1) if isinstance(room, dict) else 1
    round_no = max([int(c.get("round") or 0) for c in cards] or [0])
    active = [n for n in critics if n.get("status") in ("triage", "todo") + ACTIVE]
    done = sorted([n for n in critics if n.get("status") in CLOSED], key=lambda n: n.get("finished_at") or 0)
    verdict, issues, task_id = None, [], None
    if done:
        canon = _canon(_raw_meta(done[-1]), done[-1].get("summary_md"))
        verdict = str(canon.get("verdict") or "").lower() or None
        issues = canon.get("issues") if isinstance(canon.get("issues"), list) else []
        task_id = done[-1]["id"]
    if not enabled:
        state = "off"
    elif active:
        state, task_id = "reviewing", active[0]["id"]
    elif done and str(verdict or "").startswith(("approve", "accept")):
        state = "approved"
    elif done and verdict in ("revise", "revision"):
        state = "revised"
    elif any(n.get("error") and n.get("status") not in CLOSED for n in critics):
        state = "failed"
    elif not done:
        # включён, но проверки ещё не было (карточки критика нет или она не начиналась)
        state = "waiting"
    else:
        state = "off"
    return {"enabled": enabled, "round": round_no, "max_rounds": max_rounds, "state": state,
            "verdict": verdict if verdict in ("approve", "revise") else None, "issues": issues, "task_id": task_id}


# -------------------------------------------------------------------- статус ---
def _status(root: dict, nodes: list, questions: list, critic=None) -> tuple:
    """Статус запроса по docs/API.md §4 + ARCHITECTURE §12.5. Возвращает (status, reason)."""
    root_status = root.get("status") or ""
    critic = critic if isinstance(critic, dict) else {}
    if any(n["role"] == "critic" and n.get("status") in ACTIVE for n in nodes):
        return "reviewing", "Критик проверяет ответ"
    if any(n["role"] == "revision" and n.get("status") in ACTIVE for n in nodes):
        return "synthesizing", "Идёт доработка ответа по замечаниям"
    if critic.get("state") == "revised" and not any(n["role"] == "revision" for n in nodes):
        # критик уже вернул revise, карточку доработки ещё не создали — ответ не финальный
        return "synthesizing", "Критик просит доработку — готовится карточка доработки"
    if root_status == "done":
        return "done", None
    human = [n for n in nodes[1:] if n.get("needs_human")]
    if human:
        return "needs_you", f"Ждёт ответа: карточка «{human[0]['title'][:60]}» заблокирована"
    open_q = [q for q in questions if (q.get("for") or "mikhail") == "mikhail" and not q.get("answered")]
    if open_q:
        qid = str(open_q[0].get("id") or "q").replace("q_", "Q")
        return "needs_you", f"Ждёт решения Михаила по {qid}: {(open_q[0].get('text') or '')[:120]}"
    tries, limit = root.get("consecutive_failures") or 0, root.get("max_retries") or 2
    if root.get("last_failure_error") or (isinstance(tries, int) and tries >= int(limit) and root_status != "done"):
        return "failed", f"Воркер упал {tries or limit} раз(а): {(root.get('last_failure_error') or '')[:120]}".strip()
    leaves = [n for n in nodes[1:] if n["id"] not in {p for m in nodes[1:] for p in m["parent_ids"]}]
    if leaves and all(n.get("status") in FAILED_STATUS or (n.get("error") and n.get("status") not in CLOSED)
                      for n in leaves) and not any(n.get("status") in ACTIVE for n in nodes):
        return "failed", "Все листья дерева упали, активной работы нет"
    if root_status == "review":
        return "reviewing", "Ответ ждёт проверки"
    if root_status == "running" and all(n.get("status") in CLOSED for n in nodes[1:]):
        return "synthesizing", "Директор сводит ответ из готовых частей"
    if any(n.get("status") in ACTIVE for n in nodes[1:]):
        return "working", None
    if root_status == "running":
        return "working", "Работа идёт"
    return "queued", None


# ------------------------------------------------------------------ timeline ---
def _timeline(con, ctx: dict) -> list:
    events = []
    ids = [i for i in ctx["ids"] if i]
    for chunk in _chunks(ids):
        try:
            rows = con.execute(f"SELECT id, task_id, kind, payload, created_at FROM task_events "
                               f"WHERE task_id IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            if (row["kind"] or "") == "commented":
                continue  # комментарии берём из task_comments, чтобы не дублировать
            payload = _json(row["payload"])
            actor = payload.get("profile") or payload.get("actor") or payload.get("assignee") or "system"
            kind = row["kind"] or "created"
            events.append({"at": row["created_at"] or 0, "kind": kind, "task_id": row["task_id"],
                           "actor": actor, "text": EVENT_TEXT.get(kind) or f"Событие {kind}", "_id": row["id"]})
    for tid, comments in ctx["comments"].items():
        for c in comments:
            events.append({"at": c.get("at") or 0, "kind": "commented", "task_id": tid,
                           "actor": c.get("author") or "system",
                           "text": (c.get("text") or "").strip().replace("\n", " ")[:200], "_id": None})
    events.sort(key=lambda e: (e["at"], e["_id"] or 0, e["task_id"] or ""))
    for event in events:
        event.pop("_id", None)
    return events
