"""«Проверить доску сейчас» (WAVE13 §8, пакет F): что запущено и кто чего ждёт.

Кнопка запускает один проход диспетчера Hermes. Технический ответ (`spawned`,
`reclaimed`, `promoted`) ничего не говорит Михаилу, поэтому здесь считается
понятный итог: сколько работ готово к запуску, какие уже идут, какие зависли
(нет heartbeat), кто ждёт ответа человека, а кто — других задач.

Модуль только ЧИТАЕТ доску: ``sqlite3.connect("file:<db>?mode=ro", uri=True)``,
соединение закрывается всегда, исключения наружу не выходят (``board_state``
деградирует в ``ok: False`` + ``error``) — итог не должен ломать сам проход.

Поля карточки (``counts``/``cards``) — машинные коды причин: русский текст
собирает интерфейс (``web/js/views/kick-panel.js``), чтобы у одного итога было
одно место с правильными падежами; ``reason`` — короткая подпись для API,
журналов и тестов.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

#: префикс tenant карточек приложения: доска общая с другими работами Hermes
TENANT_PREFIX = "room:"
#: running без heartbeat дольше этого — «зависла», проход вернёт её в работу
STUCK_AFTER_S = 15 * 60
#: сколько карточек отдавать в ответе (счётчики всегда полные)
MAX_CARDS = 60

#: машинный код причины → короткая подпись (RU)
REASONS = {
    "ready": "готова к запуску",
    "stuck": "зависла: нет отклика",
    "running": "уже идёт",
    "input": "ждёт вашего ответа",
    "blocked": "ждёт решения",
    "review": "на проверке",
    "deps": "ждёт других задач",
    "triage": "ждёт разбора",
    "scheduled": "отложена по времени",
    "queued": "в очереди",
}
#: статусы карточки → причина «почему не идёт»
_STATUS_REASON = {
    "ready": "ready",
    "running": "running",
    "review": "review",
    "blocked": "blocked",
    "todo": "deps",
    "triage": "triage",
    "scheduled": "scheduled",
    "queued": "queued",
}
#: порядок в списке: сначала то, что проход может сдвинуть
_PRIORITY = {"ready": 0, "stuck": 1, "blocked": 2, "input": 3, "running": 4,
             "review": 5, "deps": 6, "triage": 7, "scheduled": 8, "queued": 9}
_COUNT_KEYS = ("ready", "stuck", "running", "review", "input", "blocked",
               "deps", "triage", "scheduled", "queued", "done", "total")


def _empty_counts() -> dict:
    return {key: 0 for key in _COUNT_KEYS}


def _num(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _classify(row: sqlite3.Row, stamp: int) -> str:
    """Статус карточки (+ heartbeat для running) → код причины W13-F."""
    status = str(row["status"] or "")
    if status == "running":
        expires = _num(row["claim_expires"])
        beat = _num(row["last_heartbeat_at"])
        stale = (expires and expires < stamp) or (beat and beat < stamp - STUCK_AFTER_S)
        return "stuck" if stale else "running"
    if status == "blocked":
        kind = str(row["block_kind"] or "").lower()
        if "input" in kind or "human" in kind:
            return "input"
        return "blocked"
    return _STATUS_REASON.get(status, status or "other")


def board_state(cfg, board, *, now=None, limit: int = MAX_CARDS) -> dict:
    """Счётчики и «на чём стоят» карточки комнат на доске. Только чтение.

    ``counts`` — по кодам причин (см. ``REASONS``) + ``done``/``total``;
    ``cards`` — не более ``limit`` карточек с ``id/title/assignee/status/reason``;
    ``useful`` — есть ли смысл нажимать кнопку (готовые к запуску или зависшие).
    """
    stamp = int(time.time() if now is None else float(now))
    out = {"ok": False, "counts": _empty_counts(), "cards": [], "useful": False,
           "board": str(board or ""), "error": None, "at": stamp}
    try:
        db = Path(cfg.kanban_db(board))
    except Exception as exc:  # noqa: BLE001 - итог не должен падать
        out["error"] = "не удалось определить файл доски: %s" % exc
        return out
    if not db.is_file():
        out["error"] = "нет файла доски: %s" % db
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=3)
    except sqlite3.Error as exc:
        out["error"] = "доска недоступна: %s" % exc
        return out
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, title, assignee, status, tenant, created_at, started_at, "
            "completed_at, block_kind, claim_expires, last_heartbeat_at "
            "FROM tasks WHERE COALESCE(tenant,'') LIKE ? "
            "AND COALESCE(status,'') <> 'archived'",
            (TENANT_PREFIX + "%",)).fetchall()
    except sqlite3.Error as exc:
        out["error"] = "не удалось прочитать доску: %s" % exc
        return out
    finally:
        con.close()

    counts = _empty_counts()
    cards = []
    for row in rows:
        code = _classify(row, stamp)
        if code in counts:
            counts[code] += 1
        counts["total"] += 1
        if code in ("done", "other"):
            continue
        cards.append({
            "id": str(row["id"] or ""),
            "title": str(row["title"] or ""),
            "assignee": row["assignee"],
            "status": str(row["status"] or ""),
            "reason_code": code,
            "reason": REASONS.get(code, code),
            "tenant": row["tenant"],
            "since": row["started_at"] or row["created_at"],
        })
    cards.sort(key=lambda c: (_PRIORITY.get(c["reason_code"], 99), -(c["since"] or 0)))
    out["counts"] = counts
    out["cards"] = cards[:max(1, min(_num(limit) or MAX_CARDS, 500))]
    out["useful"] = (counts["ready"] + counts["stuck"]) > 0
    out["ok"] = True
    return out


def _kind(spawned: int, reclaimed: int, promoted: int, preview: bool) -> str:
    if preview:
        return "preview"
    if spawned:
        return "started"
    if reclaimed:
        return "reclaimed"
    if promoted:
        return "promoted"
    return "nothing"


def report(cfg, board, res=None, *, now=None) -> dict:
    """Итог прохода: числа прохода + состояние доски + кто чего ждёт.

    ``res`` — ответ прохода (``spawned/reclaimed/promoted/planned/max``).
    Пустой ``res`` — предпросмотр: ничего не запускалось, показано только
    состояние доски (это то, что решает, нужна ли кнопка вообще).
    """
    stamp = int(time.time() if now is None else float(now))
    res = res if isinstance(res, dict) else {}
    preview = res.get("preview") is True
    state = board_state(cfg, board, now=stamp)
    spawned = _num(res.get("spawned"))
    reclaimed = _num(res.get("reclaimed"))
    promoted = _num(res.get("promoted"))
    titles = {card["id"]: card["title"] for card in state["cards"]}
    started = []
    for item in res.get("planned") if isinstance(res.get("planned"), list) else []:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or "")
        started.append({"id": task_id, "assignee": item.get("assignee"),
                        "title": titles.get(task_id, "")})
    return {
        "ok": bool(state.get("ok")),
        "kind": _kind(spawned, reclaimed, promoted, preview),
        "preview": preview,
        "spawned": spawned,
        "reclaimed": reclaimed,
        "promoted": promoted,
        "max": res.get("max"),
        "started": started,
        "counts": state["counts"],
        "cards": state["cards"],
        "useful": state["useful"],
        "error": state.get("error"),
        "at": stamp,
    }


__all__ = ["REASONS", "TENANT_PREFIX", "STUCK_AFTER_S", "MAX_CARDS",
           "board_state", "report"]
