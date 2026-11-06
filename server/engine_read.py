"""Чтение канбана Hermes из SQLite (только ``mode=ro``) для «Комнаты команды».

Формы ответов — docs/API.md §3.3–3.5, §4 с поправками ARCHITECTURE.md §12
(версии ответа, критик, роли дерева, outside_room).

Модуль ничего не пишет: ``sqlite3.connect("file:<db>?mode=ro", uri=True, timeout=5)``,
``row_factory=sqlite3.Row``, соединение закрывается всегда. Отсутствие БД/таблиц —
``EngineError(code, message_ru)`` для функций, которым нужны данные; списки и
``health`` деградируют в пустой результат/диагностику.
"""
from __future__ import annotations

import sqlite3

try:  # запуск как пакет (server.engine_read)
    from . import read_view as _view
    from .config import VERSION, path_allowed
except ImportError:  # запуск как скрипт из каталога server/
    import read_view as _view  # type: ignore
    from config import VERSION, path_allowed  # type: ignore

try:  # тело карточки: на чтении показываем только текст пользователя (D1)
    from .contract import visible_text
except ImportError:  # pragma: no cover
    from contract import visible_text  # type: ignore

try:  # W14-D: статусы ответов и связка вопрос↔шаг — в одном месте (server/answers.py)
    from . import answers as _answers
except ImportError:  # pragma: no cover
    import answers as _answers  # type: ignore

# --- раскладка по файлам: перенесённые имена доступны отсюда, как и раньше ---
try:
    from .engine_db import (ACTIVE, CLOSED, EVENT_TEXT, EngineError, FAILED_STATUS, MAX_DEPTH, MAX_NODES,
        OPTIONAL_COLUMNS, REQUIRED_COLUMNS, REQUIRED_TABLES, RUN_FIELDS, SCHEMA_VERSION,
        TASK_FIELDS, _chunks, _cols, _comment_rows, _conn, _json, _require_tables,
        _run_rows, _schema_missing, _select, _task_rows)  # noqa: F401
    from .engine_tree import (_as_list, _canon, _canons, _collect, _critic_block, _last_at,
        _last_ok, _legacy, _needs_human_node, _node_error, _pick_run, _raw_meta, _roles,
        _room_dict, _run_ok, _run_out, _status, _synth_title, _synthesis, _timeline, _tree)  # noqa: F401
    from .engine_health import _pid_alive, _task_counts, dispatcher_status, health  # noqa: F401
except ImportError:  # pragma: no cover
    from engine_db import (ACTIVE, CLOSED, EVENT_TEXT, EngineError, FAILED_STATUS, MAX_DEPTH, MAX_NODES,
        OPTIONAL_COLUMNS, REQUIRED_COLUMNS, REQUIRED_TABLES, RUN_FIELDS, SCHEMA_VERSION,
        TASK_FIELDS, _chunks, _cols, _comment_rows, _conn, _json, _require_tables,
        _run_rows, _schema_missing, _select, _task_rows)  # noqa: F401  # type: ignore
    from engine_tree import (_as_list, _canon, _canons, _collect, _critic_block, _last_at,
        _last_ok, _legacy, _needs_human_node, _node_error, _pick_run, _raw_meta, _roles,
        _room_dict, _run_ok, _run_out, _status, _synth_title, _synthesis, _timeline, _tree)  # noqa: F401  # type: ignore
    from engine_health import _pid_alive, _task_counts, dispatcher_status, health  # noqa: F401  # type: ignore

__all__ = [
    "EngineError", "health", "dispatcher_status", "list_roots", "list_candidates",
    "request_summary", "request_detail", "team_state", "needs_human", "task_events",
    "is_decomposed", "root_assignee_status",
]


# «metadata → UI» живёт в read_view.py; имена оставлены как были — на них
# завязаны app.py и тесты, менять публичные имена нельзя
CRITIC_STATES = _view.CRITIC_STATES
PREVIEW_MAX = _view.PREVIEW_MAX
PHASES = _view.PHASES
PHASE_IDS = _view.PHASE_IDS
_progress = _view.progress
_preview = _view.preview
_critic_brief = _view.critic_brief
_stage = _view.stage
_steps = _view.steps
_questions = _view.questions
_decisions = _view.decisions
_future = _view.future
_all_questions = _view.all_questions
_stage_filled = _view.stage_filled
_merge_canon = _view.merge_canon
_kind_of = _view.kind_of
_artifacts = _view.artifacts
_report = _view.report


def list_roots(cfg, board, tenant=None, limit: int = 50) -> list:
    """Корни запросов комнат: карточки с tenant (``room:*``), новые сверху.

    Дети декомпозиции тоже носят tenant, поэтому корень определяется так:
    ``idempotency_key`` вида ``r-…`` (карточки, созданные приложением) ИЛИ
    событие ``decomposed`` у карточки, созданной не авто-декомпозером.
    """
    try:
        with _conn(cfg, board) as con:
            _require_tables(con, "tasks")
            have = _cols(con, "tasks")
            if "tenant" not in have:
                return []
            sel, limit = _select(TASK_FIELDS, have), max(1, min(int(limit or 50), 500))
            has_events = bool(_cols(con, "task_events"))
            root_cond = "COALESCE(t.idempotency_key,'') LIKE 'r-%'"
            if has_events:
                root_cond += (" OR (COALESCE(t.created_by,'') <> 'auto-decomposer' AND EXISTS ("
                              "SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.kind = 'decomposed'))")
            where = "t.tenant = ?" if tenant else "t.tenant LIKE 'room:%'"
            params = ([tenant] if tenant else []) + [limit]
            rows = con.execute(f"SELECT {sel} FROM tasks t WHERE {where} AND ({root_cond}) "
                               "ORDER BY t.created_at DESC, t.id DESC LIMIT ?", params).fetchall()
    except EngineError:
        raise
    except sqlite3.Error as exc:
        raise EngineError("db_error", f"Не удалось прочитать корни запросов: {exc}", {"board": board}) from exc
    return [{"id": r["id"], "title": r["title"] or "", "assignee": r["assignee"], "status": r["status"],
             "tenant": r["tenant"], "created_at": r["created_at"], "started_at": r["started_at"],
             "finished_at": r["completed_at"], "archived": r["status"] == "archived",
             "created_by": r["created_by"], "idempotency_key": r["idempotency_key"],
             "preview": _preview(r["body"])} for r in rows]


def list_candidates(cfg, board, registered_ids: set) -> list:
    """Корни с событием ``decomposed``, которых нет в реестре комнаты (кандидаты на привязку)."""
    registered = {str(i) for i in (registered_ids or set())}
    try:
        with _conn(cfg, board) as con:
            _require_tables(con, "tasks", "task_events")
            sel = _select(TASK_FIELDS, _cols(con, "tasks"))
            rows = con.execute(f"SELECT {sel} FROM tasks t WHERE EXISTS (SELECT 1 FROM task_events e "
                               "WHERE e.task_id = t.id AND e.kind = 'decomposed') "
                               "AND COALESCE(t.status, '') <> 'archived' ORDER BY t.created_at DESC").fetchall()
            tasks = [dict(r) for r in rows if r["id"] not in registered]
            counts, decomposed_at = {}, {}
            if tasks:
                ids = [t["id"] for t in tasks]
                for chunk in _chunks(ids):
                    ph = ", ".join("?" * len(chunk))
                    for row in con.execute(
                            f"SELECT x.id, count(DISTINCT l.other) n FROM tasks x JOIN ("
                            f" SELECT parent_id id, child_id other FROM task_links WHERE parent_id IN ({ph})"
                            f" UNION SELECT child_id id, parent_id other FROM task_links WHERE child_id IN ({ph})"
                            f") l ON l.id = x.id GROUP BY x.id", chunk + chunk):
                        counts[row["id"]] = row["n"]
                    for row in con.execute(
                            f"SELECT task_id, min(created_at) at FROM task_events WHERE kind = 'decomposed' "
                            f"AND task_id IN ({ph}) GROUP BY task_id", chunk):
                        decomposed_at[row["task_id"]] = row["at"]
    except EngineError:
        raise
    except sqlite3.Error as exc:
        raise EngineError("db_error", f"Не удалось прочитать кандидатов: {exc}", {"board": board}) from exc
    return [{"task_id": t["id"], "id": t["id"], "title": t["title"] or "", "assignee": t["assignee"],
             "status": t["status"], "created_at": t["created_at"], "tenant": t["tenant"],
             "children": int(counts.get(t["id"], 0)),
             "decomposed_at": decomposed_at.get(t["id"]),
             "preview": _preview(t.get("body"))} for t in tasks]


def task_events(cfg, board, task_id, since_id: int = 0) -> list:
    """События карточки (для watcher'а): ``id``, ``kind``, распарсенный ``payload``."""
    try:
        with _conn(cfg, board) as con:
            _require_tables(con, "task_events")
            rows = con.execute("SELECT id, task_id, run_id, kind, payload, created_at FROM task_events "
                               "WHERE task_id = ? AND id > ? ORDER BY id", (task_id, int(since_id or 0))).fetchall()
    except EngineError:
        raise
    except sqlite3.Error as exc:
        raise EngineError("db_error", f"Не удалось прочитать события карточки: {exc}", {"task_id": task_id}) from exc
    return [{"id": r["id"], "task_id": r["task_id"], "run_id": r["run_id"], "kind": r["kind"],
             "payload": _json(r["payload"]), "created_at": r["created_at"]} for r in rows]


def is_decomposed(cfg, board, root_id) -> bool:
    """Есть ли на карточке событие ``decomposed`` (движок разложил запрос)."""
    try:
        with _conn(cfg, board) as con:
            _require_tables(con, "task_events")
            return con.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'decomposed' LIMIT 1",
                               (root_id,)).fetchone() is not None
    except EngineError:
        raise
    except sqlite3.Error:
        return False


def root_assignee_status(cfg, board, root_id) -> dict:
    """Карточка корня: исполнитель, статус карточки и признак живого run'а (для watcher'а)."""
    try:
        with _conn(cfg, board) as con:
            _require_tables(con, "tasks")
            row = _task_rows(con, [root_id]).get(root_id)
            if row is None:
                raise EngineError("not_found", f"Карточка {root_id} не найдена на этой доске.", {"task_id": root_id})
            running = (row.get("status") or "") == "running"
            if not running:
                try:
                    running = con.execute("SELECT 1 FROM task_runs WHERE task_id = ? AND status = 'running' "
                                          "LIMIT 1", (root_id,)).fetchone() is not None
                except sqlite3.Error:
                    running = False
    except EngineError:
        raise
    except sqlite3.Error as exc:
        raise EngineError("db_error", f"Не удалось прочитать карточку корня: {exc}", {"task_id": root_id}) from exc
    return {"assignee": row.get("assignee"), "status": row.get("status"), "running": bool(running)}


# ------------------------------------------------------------- summary/detail ---
def _apply_answers(questions: list, answers, reactions=None) -> list:
    """Наложить ответы Михаила на вопросы (реестр `answers`: q_id → запись).

    Ответ хранится в реестре запросов, а не в metadata карточки: движок не даёт
    переписывать metadata run'а, поэтому `POST actions {action:"answer"}` пишет
    комментарий + запись в реестр, а чтение накладывает её на вопросы ответа.

    W14-D (§1, §3): ответ — всегда ответ на КОНКРЕТНЫЙ вопрос; встречный вопрос
    («поясни, что это») вопрос НЕ закрывает: `answered=False` и статус
    «ждёт вашего решения после пояснения». Реакция руководителя (`reactions`)
    меняет статус на `received → applied → no_change`.
    """
    if not isinstance(answers, dict) or not answers:
        return questions
    reactions = reactions if isinstance(reactions, dict) else {}
    for question in questions:
        qid = str(question.get("id"))
        record = answers.get(qid)
        if not isinstance(record, dict):
            # ответ мог прийти из metadata (старые версии ответа) — не теряем его
            if question.get("answered") and str(question.get("answer") or "").strip():
                record = {"text": str(question.get("answer")), "at": question.get("answered_at")}
            else:
                continue
        _answers.apply_question(question, record, reactions.get(qid))
    return questions


def _steps_with_source(canon: dict) -> list:
    """Шаги ответа + источник (`from_question`) из metadata, если руководитель его указал."""
    steps = _steps(canon)
    raw = canon.get("next_steps")
    raw = raw if isinstance(raw, list) else []
    source: dict = {}
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            source.setdefault("ns_%d" % index, None)
            continue
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "ns_%d" % index)
        if item.get("from_question"):
            source[key] = str(item["from_question"])
    for step in steps:
        sid = str(step.get("id") or "")
        step["from_question"] = source.get(sid)
    return steps


def _load(cfg, board, root_id, room=None, extra_cards=None, enabled=None, answers=None):
    extra = [c for c in _as_list(extra_cards) if isinstance(c, dict) and c.get("id")]
    room_dict = _room_dict(cfg, room)
    with _conn(cfg, board) as con:
        ctx = _collect(con, root_id)
        nodes = _tree(ctx, room_dict, extra, cfg, board)
        synth, canon, warns = _synthesis(ctx, nodes, extra)
        canons = _canons(nodes)
        if canons:
            canon = _merge_canon(canon, canons)
        questions = _apply_answers(_all_questions(canon, canons), answers)
        critic = _critic_block(nodes, extra, room_dict, ctx, enabled)
        status, reason = _status(ctx["tasks"][root_id], nodes, questions, critic)
        # `needs_you` — флаг «нужен человек»: статус needs_you ИЛИ заблокированная карточка,
        # ИЛИ неотвечённый вопрос Михаилу в последней версии ответа (решение контракта:
        # у завершённого запроса вопрос не перебивает `done`, но флаг и счётчик растут).
        open_pavel = [q for q in questions
                      if (q.get("for") or "mikhail") == "mikhail" and not q.get("answered")]
        needs_you = status == "needs_you" or bool(open_pavel) or any(n.get("needs_human") for n in nodes[1:])
        timeline = _timeline(con, ctx)
    return {"ctx": ctx, "nodes": nodes, "synth": synth, "canon": canon, "warnings": warns,
            "questions": questions, "critic": critic, "status": status, "reason": reason,
            "needs_you": needs_you, "timeline": timeline, "room": room_dict}


def _summary_fields(ctx, nodes, status, reason, extra, critic=None, room=None, needs_you=None) -> dict:
    root, runs = ctx["tasks"][ctx["root_id"]], ctx["runs"].get(ctx["root_id"], [])
    last = _last_ok(runs) or (runs[-1] if runs else None)
    started = root.get("started_at") or (last or {}).get("started_at")
    if not isinstance(started, int):
        # корень ещё не брали в работу, но дети уже начаты — запрос идёт
        child_starts = [n.get("started_at") for n in nodes[1:] if isinstance(n.get("started_at"), int)]
        started = min(child_starts) if child_starts else None
    finished = root.get("completed_at") or ((last or {}).get("ended_at") if root.get("status") in CLOSED else None)
    duration = finished - started if isinstance(started, int) and isinstance(finished, int) and finished >= started else None
    tenant = root.get("tenant") or ""
    room_id = tenant[5:] if tenant.startswith("room:") else None
    if not room_id and isinstance(room, dict):
        # adopted-карточка живёт в реестре комнаты, хотя tenant у неё не менялся (может быть null)
        room_id = room.get("id")
    assignee = root.get("assignee")
    if not assignee and isinstance(room, dict):
        assignee = room.get("orchestrator")  # запрос ведёт директор комнаты
    if needs_you is None:
        needs_you = status == "needs_you"
    return {"id": ctx["root_id"], "room": room_id,
            "title": root.get("title") or "", "status": status, "status_reason": reason, "mode": "auto",
            "critic": _critic_brief(critic), "created_at": root.get("created_at"),
            "started_at": started, "finished_at": finished, "duration_s": duration,
            "progress": _progress(nodes, status), "assignee": assignee,
            "needs_you": bool(needs_you), "preview": _preview(visible_text(root.get("body") or "")),
            "archived": root.get("status") == "archived"}


def request_summary(cfg, board, root_id, extra_cards=None, room=None, critic=None, answers=None) -> dict:
    """Лёгкая карточка запроса (RequestSummary, docs/API.md §3.3 + ARCH §12.5)."""
    extra = _as_list(extra_cards)
    data = _load(cfg, board, root_id, room, extra, critic, answers)
    return _summary_fields(data["ctx"], data["nodes"], data["status"], data["reason"], extra,
                           data["critic"], room=data["room"], needs_you=data["needs_you"])


def request_detail(cfg, board, root_id, room=None, extra_cards=None, critic=None, answers=None) -> dict:
    """Полная карточка запроса (RequestDetail, docs/API.md §3.4 + ARCHITECTURE §12.5)."""
    extra = [c for c in _as_list(extra_cards) if isinstance(c, dict) and c.get("id")]
    data = _load(cfg, board, root_id, room, extra, critic, answers)
    ctx, nodes, canon, warnings = data["ctx"], data["nodes"], data["canon"], list(data["warnings"])
    out = _summary_fields(ctx, nodes, data["status"], data["reason"], extra, data["critic"],
                          room=data["room"], needs_you=data["needs_you"])
    out.update({"text": visible_text(ctx["tasks"][root_id].get("body") or ""),
                "body_full": ctx["tasks"][root_id].get("body") or "",
                "dispatcher": _dispatcher_short(cfg),
                "synthesis": data["synth"], "next_steps": _steps_with_source(canon),
                "open_questions": data["questions"],
                "decisions": _decisions(canon, root_id, data["synth"].get("at")), "stage": _stage(canon),
                "future_questions": _future(canon), "artifacts": _artifacts(cfg, canon, warnings),
                "report": _report(cfg, canon), "tree": nodes, "timeline": data["timeline"],
                "warnings": _dedupe_warnings(warnings), "critic": data["critic"]})
    outside = [n for n in nodes if n.get("outside_room")]
    if outside:
        out["warnings"].append({"code": "outside_room",
                                "message": "Карточки назначены вне состава комнаты: "
                                           f"{', '.join(sorted({str(n.get('assignee')) for n in outside}))}. "
                                           "Нужно переназначить.",
                                "detail": {"task_ids": [n["id"] for n in outside]}})
    failed = [n for n in nodes if n.get("error") and n.get("status") not in CLOSED]
    if failed:
        out["warnings"].append({"code": "card_failed",
                                "message": f"Есть упавшие карточки: {len(failed)}. Подробности — в дереве работ.",
                                "detail": {"task_ids": [n["id"] for n in failed]}})
    return out


def _dispatcher_short(cfg) -> dict:
    try:
        disp = dispatcher_status(cfg)
    except Exception:  # noqa: BLE001
        return {"ok": False, "warning": "Не удалось определить состояние диспетчера"}
    return {"ok": bool(disp.get("ok")), "warning": disp.get("warning")}


def _dedupe_warnings(warnings: list) -> list:
    seen, out = set(), []
    for warning in warnings:
        key = (warning.get("code"), (warning.get("detail") or {}).get("path") or "")
        if key not in seen:
            seen.add(key)
            out.append(warning)
    return out


# --------------------------------------------------------- команда и вопросы ---
def team_state(cfg, board, members: list, tenant=None) -> list:
    """Кто чем занят: TeamMember[] (docs/API.md §3.5). ``members`` — состав комнаты.

    Принимает и dict'ы ``{profile, role, title, note, pinned}``, и просто имена профилей
    (app.py передаёт список строк — тогда роль ``worker``).
    """
    members = [m if isinstance(m, dict) else {"profile": m}
               for m in _as_list(members) if (m.get("profile") if isinstance(m, dict) else m)]
    members = [m for m in members if m.get("profile")]
    if not members:
        return []
    profiles = [m["profile"] for m in members]
    ph, params = ",".join("?" * len(profiles)), list(profiles)
    extra = ""
    if tenant:
        extra, params = " AND COALESCE(tenant,'') = ?", params + [tenant]
    try:
        with _conn(cfg, board) as con:
            _require_tables(con, "tasks")
            sel = _select(TASK_FIELDS, _cols(con, "tasks"))
            cur = [dict(r) for r in con.execute(
                f"SELECT {sel} FROM tasks WHERE assignee IN ({ph}) AND status IN ('running','review'){extra} "
                "ORDER BY COALESCE(started_at, created_at) DESC", params)]
            blocked = [dict(r) for r in con.execute(
                f"SELECT {sel} FROM tasks WHERE assignee IN ({ph}) AND status = 'blocked'{extra}", params)]
            counted = [dict(r) for r in con.execute(
                f"SELECT assignee, status, count(*) n FROM tasks WHERE assignee IN ({ph}){extra} "
                "AND COALESCE(status,'') <> 'archived' GROUP BY assignee, status", params)]
            last = [dict(r) for r in con.execute(
                f"SELECT {sel} FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY assignee "
                "ORDER BY COALESCE(completed_at, 0) DESC, created_at DESC) rn FROM tasks "
                f"WHERE assignee IN ({ph}) AND status IN ('done','archived'){extra}) WHERE rn = 1", params)]
            last_runs = _run_rows(con, [r["id"] for r in last])
    except EngineError:
        raise
    except sqlite3.Error as exc:
        raise EngineError("db_error", f"Не удалось прочитать состояние команды: {exc}", {"board": board}) from exc
    counts = {}
    for row in counted:
        bucket = counts.setdefault(row["assignee"], {"done": 0, "blocked": 0, "active": 0})
        key = "done" if row["status"] in CLOSED else ("blocked" if row["status"] == "blocked" else "active")
        bucket[key] += row["n"]
    current = {r["assignee"]: r for r in cur}
    waiting = {r["assignee"]: r for r in blocked}
    latest = {r["assignee"]: r for r in last}
    out = []
    for member in members:
        profile = member["profile"]
        now, blk, prev = current.get(profile), waiting.get(profile), latest.get(profile)
        shown = now or blk  # «что делает сейчас»: активная работа или блокировка, на которой стоит
        cnt = counts.get(profile) or {"done": 0, "blocked": 0, "active": 0}
        run = (_last_ok(last_runs.get(prev["id"], [])) or {}) if prev else {}
        out.append({
            "profile": profile, "title": member.get("title") or profile,
            "role": member.get("role") or "worker", "note": member.get("note") or "",
            "pinned": bool(member.get("pinned")), "state": "working" if now else ("waiting" if blk else "idle"),
            "current": {"task_id": shown["id"], "title": shown["title"] or "", "status": shown["status"],
                        "started_at": shown.get("started_at"),
                        "since": shown.get("started_at") or shown.get("created_at")} if shown else None,
            "last": {"task_id": prev["id"], "title": prev["title"] or "", "status": prev["status"],
                     "finished_at": prev.get("completed_at"), "at": prev.get("completed_at"),
                     "summary": (run.get("summary") or "")[:400]} if prev else None,
            "counts": cnt, "load": cnt["active"]})
    return out


def needs_human(cfg, board, root_ids) -> list:
    """Что ждёт человека по корням: blocked-карточки и открытые вопросы к Михаилу/профилю."""
    out = []
    for root_id in _as_list(root_ids):
        try:
            data = _load(cfg, board, root_id, None, None)
        except EngineError:
            continue
        for node in data["nodes"][1:]:
            if node.get("needs_human"):
                out.append({"root_id": root_id, "kind": "blocked", "task_id": node["id"],
                            "title": node.get("title") or "", "assignee": node.get("assignee"),
                            "since": node.get("started_at"), "question_id": None,
                            "text": node.get("title") or "", "status": node.get("status")})
        for q in data["questions"]:
            if not q.get("answered"):
                out.append({"root_id": root_id, "kind": "question", "task_id": root_id,
                            "title": data["ctx"]["tasks"][root_id].get("title") or "",
                            "assignee": q.get("for"), "since": data["synth"].get("at"),
                            "question_id": q.get("id"), "text": q.get("text") or "", "status": data["status"]})
    return out
