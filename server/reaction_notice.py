"""Уведомление о реакции руководителя (W20-U4, P9) — лёгкий счётчик для бейджа.

Задача (аудит 06.11, P9): «руководитель отреагировал на мой ответ» должно быть видно
без открытия запроса — бейджем на вкладке «От вас нужно» и отметкой в «Недавнем».
Точное «прочитано» знает только браузер (отметка живёт в localStorage), поэтому
сервер отдаёт две вещи:

* ``last_reaction_at`` — время последней реакции руководителя в комнате;
* ``reactions`` — сколько реакций и когда было по каждому запросу.

Как считается (и почему дёшево)
-------------------------------
Реакция руководителя — это либо комментарий в ветке с меткой
``[Руководитель · реакция на ответ|шаг|решение …]``, либо ``metadata.reaction``
в его карточке. Оба источника лежат в базе доски, и оба читаются ОДНИМ-ДВУМЯ
запросами на комнату (``task_links`` + ``task_comments`` + ``metadata LIKE``),
без сборки деталей запросов: сводку бокового меню опрашивают часто, и тяжёлый
путь (``engine_read.request_detail``) там недопустим.

Смотрим только запросы, по которым человек что-то отвечал (в реестре есть
``answers``/``step_actions``) — без ответа реагировать не на что. Ошибки чтения
доски не роняют ответ API: пустая сводка = бейджа нет (лучше без уведомления,
чем без страницы). Ничего не пишем: соединение открывается ``mode=ro``.
"""
from __future__ import annotations

import json
import sqlite3

try:                                            # пакетный импорт
    from . import answers as answers_mod
    from .engine_db import EngineError, _chunks, _conn, _require_tables
except ImportError:                             # python server/app.py (плоский импорт)
    import answers as answers_mod               # type: ignore
    from engine_db import EngineError, _chunks, _conn, _require_tables  # type: ignore

#: Метки реакции руководителя (единый словарь — server/answers.py).
REACTION_PREFIXES = (
    answers_mod.REACTION_MARK,
    answers_mod.STEP_REACTION_MARK,
    answers_mod.DECISION_REACTION_MARK,
)

#: Сколько запросов комнаты просматриваем за один счёт (свежие сверху).
MAX_ROOTS = 40
#: Глубина дерева: реакция приходит на карточку-ребёнка, редко глубже двух уровней.
MAX_DEPTH = 2
#: Сколько реакций отдаём в ``items`` (для отчёта и тестов).
MAX_ITEMS = 20


def _lead_profiles(room) -> set:
    """Профили руководителя комнаты — реакция считается только от них."""
    out = set()
    if isinstance(room, dict):
        for key in ("orchestrator", "director", "lead", "room_lead"):
            value = str(room.get(key) or "").strip().lower()
            if value:
                out.add(value)
    return out


def _kind_of(text) -> str:
    """Вид реакции по метке: ответ / шаг / решение."""
    raw = str(text or "")
    if raw.startswith(answers_mod.STEP_REACTION_MARK):
        return "step"
    if raw.startswith(answers_mod.DECISION_REACTION_MARK):
        return "decision"
    return "answer"


def _owner_map(con, roots, depth: int = MAX_DEPTH) -> dict:
    """``task_id → root_id`` для корней и их потомков (≤ depth уровней)."""
    owner = {str(r): str(r) for r in roots if r}
    if not owner:
        return owner
    frontier = list(owner)
    for _ in range(max(0, depth)):
        fresh = []
        for chunk in _chunks(frontier, 300):
            ph = ",".join("?" * len(chunk))
            try:
                rows = con.execute("SELECT parent_id, child_id FROM task_links "
                                   "WHERE parent_id IN (%s)" % ph, chunk).fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                pid = str(row["parent_id"] or "")
                cid = str(row["child_id"] or "")
                if not cid or cid in owner:
                    continue
                owner[cid] = owner.get(pid, pid)
                fresh.append(cid)
        frontier = fresh
        if not frontier:
            break
    return owner


def _comment_items(con, owner: dict, leads: set) -> list:
    """Реакции из комментариев ветки (одна выборка на комнату)."""
    out = []
    for chunk in _chunks(list(owner.keys()), 300):
        ph = ",".join("?" * len(chunk))
        try:
            rows = con.execute("SELECT task_id, author, body, created_at FROM task_comments "
                               "WHERE task_id IN (%s)" % ph, chunk).fetchall()
        except sqlite3.Error:
            return out
        for row in rows:
            author = str(row["author"] or "").strip().lower()
            if author not in leads:
                continue
            text = str(row["body"] or "")
            if not text.lstrip().startswith(REACTION_PREFIXES):
                continue
            root = owner.get(str(row["task_id"] or ""))
            if not root:
                continue
            out.append({"request_id": root, "at": int(row["created_at"] or 0),
                        "kind": _kind_of(text.lstrip()), "text": text.strip()[:400],
                        "task_id": str(row["task_id"] or "")})
    return out


def _metadata_items(con, owner: dict) -> list:
    """Реакции из ``metadata.reaction`` карточек: явный исход, время — завершение."""
    out = []
    for chunk in _chunks(list(owner.keys()), 300):
        ph = ",".join("?" * len(chunk))
        try:
            rows = con.execute("SELECT id, metadata, completed_at FROM tasks "
                               "WHERE id IN (%s) AND metadata LIKE '%%reaction%%'" % ph,
                               chunk).fetchall()
        except sqlite3.Error:
            return out
        for row in rows:
            raw = row["metadata"]
            if not raw:
                continue
            try:
                meta = json.loads(str(raw), strict=False)
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            reactions = answers_mod.reactions_from_tree([{
                "id": str(row["id"] or ""), "metadata": meta,
                "ended_at": int(row["completed_at"] or 0),
            }])
            root = owner.get(str(row["id"] or ""))
            if not reactions or not root:
                continue
            for key, value in reactions.items():
                if not isinstance(value, dict):
                    continue
                out.append({"request_id": root, "at": int(value.get("at") or 0),
                            "kind": "step" if str(key).startswith("ns_") else "answer",
                            "text": str(value.get("text") or value.get("label") or "")[:400],
                            "task_id": str(row["id"] or ""), "question_id": str(key)})
    return out


def room_reactions(cfg, board: str, room, records, *, max_roots: int = MAX_ROOTS) -> dict:
    """Счёт реакций руководителя по комнате: ``{count, last_at, requests, items}``.

    ``records`` — реестр запросов комнаты (``self._records(room_id)``): из него берём
    корни, по которым человек уже отвечал. Нет доски/таблиц/руководителя — пустая
    сводка без исключения.
    """
    empty = {"count": 0, "last_at": None, "requests": {}, "items": []}
    leads = _lead_profiles(room)
    if not leads:
        return empty
    roots = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        root = str(rec.get("root_id") or "")
        if not root:
            continue
        # Реагировать можно только на ответ/действие человека: без записи в реестре
        # ответов реакции быть не может — такие запросы не читаем вовсе.
        if not (rec.get("answers") or rec.get("step_actions")):
            continue
        roots.append((root, int(rec.get("created_at") or 0)))
    roots = [r for r, _ in sorted(roots, key=lambda p: (p[1], p[0]), reverse=True)[:max(1, int(max_roots))]]
    if not roots:
        return empty
    try:
        with _conn(cfg, board) as con:
            _require_tables(con, "tasks", "task_links")
            owner = _owner_map(con, roots)
            items = _comment_items(con, owner, leads) + _metadata_items(con, owner)
    except EngineError:
        return empty
    except sqlite3.Error:
        return empty
    except Exception:                            # noqa: BLE001 — бейдж не важнее страницы
        return empty

    items = [i for i in items if i.get("request_id")]
    items.sort(key=lambda i: (int(i.get("at") or 0), str(i.get("request_id"))), reverse=True)
    requests: dict = {}
    total = 0
    last_at = 0
    for item in items:
        rid = str(item["request_id"])
        at = int(item.get("at") or 0)
        slot = requests.setdefault(rid, {"count": 0, "last_at": 0})
        slot["count"] += 1
        slot["last_at"] = max(int(slot.get("last_at") or 0), at)
        total += 1
        last_at = max(last_at, at)
    return {"count": total, "last_at": last_at or None, "requests": requests,
            "items": items[:MAX_ITEMS]}
