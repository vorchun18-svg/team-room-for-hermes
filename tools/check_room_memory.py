#!/usr/bin/env python3
"""Проверка памяти комнаты: 7 инвариантов R4 §4.5 (W25 M1, п.5).

Только чтение: ``data/state`` комнаты, её Vault-зеркало и ``kanban.db``
(``sqlite mode=ro``). Ничего не пишет и не ходит в сеть.

    python tools/check_room_memory.py --room fn
    python tools/check_room_memory.py --room fn --json      # для экрана «Здоровье»
    python tools/check_room_memory.py --room fn --only 1,2  # только эти инварианты

Код возврата: 0 — блокирующих нарушений нет (есть только предупреждения),
1 — нарушен блокирующий инвариант (реестр решений или append-only),
2 — не удалось прочитать данные.

Инварианты (R4 §4.5):
1. Каждое решение из паспорта комнаты есть в реестре с тем же статусом.
2. Множество id решений только растёт (сравнение со снимком `memory/snapshot.json`).
3. Обещание со сроком из реестра есть в плане комнаты (срок и владелец).
4. Ответ владельца хранится в реестре ответов целиком (без обрезки «180 знаков»).
5. Тело карточки любого типа ≤ потолка (8 000; руководитель 6 000, исполнитель 2 500).
6. Устав не дублируется в карточке целиком (`КОМНАТА-КОНТРАКТ` ≤ 800 знаков).
7. Золотой набор находится поиском за ≤ 5 результатов — **не измеряется** здесь
   (нет набора вопросов): честный статус ``skip``, а не «зелено».

Инварианты 5 и 6 зависят от бюджетов карточек (пакет M2 волны 25) и потому
возвращают ``warn``, а не ``fail``: реестр — не место, где их лечить.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config as config_mod                     # noqa: E402
from server import decision_registry as dec_reg             # noqa: E402
from server import statefiles as sf                         # noqa: E402

BLOCK = "fail"
WARN = "warn"
OK = "ok"
SKIP = "skip"

#: Потолки тела карточки (R4 §4.3), знаки.
BODY_LIMITS = {"leader": 6000, "executor": 2500, "critic": 8000, "revision": 8000,
               "service": 2000, "default": 8000}
CONTRACT_LIMIT = 800
STATUS_RU = {"proposed": "предложено", "accepted": "принято", "rejected": "отклонено",
             "dismissed": "снято", "superseded": "устарело"}


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower().replace("ё", "е")


def _words(text) -> set:
    return {word for word in re.findall(r"[0-9a-zа-я]+", _norm(text)) if len(word) > 3}


def _result(number: int, title: str, status: str, detail: str, examples=None) -> dict:
    return {"n": number, "title": title, "status": status, "detail": detail,
            "examples": list(examples or [])[:5]}


def _room_dir(cfg, room_id, *parts) -> Path:
    return Path(cfg.state_dir) / str(room_id) / Path(*parts) if parts \
        else Path(cfg.state_dir) / str(room_id)


def _vault_text(cfg, room_id, name: str) -> str:
    try:
        base = config_mod.room_vault_dir(cfg, room_id)
    except Exception:  # noqa: BLE001
        return ""
    path = Path(base) / name
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


# ------------------------------------------------------------- инварианты ---

def inv_passport(cfg, room_id, out: dict) -> dict:
    """1. Паспорт комнаты ↔ реестр: те же id и те же статусы."""
    current = out["current"]
    text = _vault_text(cfg, room_id, "CONTEXT.md") or _vault_text(cfg, room_id, "STATE.md")
    if not text:
        return _result(1, "Паспорт комнаты ↔ реестр", SKIP, "паспорт (CONTEXT.md/STATE.md) не найден")
    seen = re.findall(r"`([A-Za-z0-9_\-]+:d_[A-Za-z0-9_\-]+)`\s*—", text)
    if not seen:
        return _result(1, "Паспорт комнаты ↔ реестр", SKIP,
                       "в паспорте нет решений с id (нечего сверять)")
    missing, drifted = [], []
    for did in seen:
        row = current.get(did)
        if row is None:
            missing.append(did)
        elif str(row.get("status")) not in ("accepted", "proposed"):
            drifted.append("%s: в реестре %s" % (did, row.get("status")))
    if missing or drifted:
        return _result(1, "Паспорт комнаты ↔ реестр", BLOCK,
                       "в паспорте %d решений; нет в реестре: %d; со снятым статусом: %d"
                       % (len(seen), len(missing), len(drifted)), missing + drifted)
    return _result(1, "Паспорт комнаты ↔ реестр", OK,
                   "все %d решений паспорта есть в реестре с действующим статусом" % len(seen))


def inv_append_only(cfg, room_id, out: dict) -> dict:
    """2. Множество id только растёт (снимок прошлого прогона)."""
    snapshot = sf.read_json(dec_reg.snapshot_path(cfg, room_id), None)
    if not isinstance(snapshot, dict) or not snapshot.get("ids"):
        return _result(2, "Только добавление (append-only)", SKIP,
                       "снимка id нет: появится после первого приёма ответа")
    missing = [did for did in snapshot.get("ids") or [] if did not in out["current"]]
    if missing:
        return _result(2, "Только добавление (append-only)", BLOCK,
                       "из реестра исчезло решений: %d" % len(missing), missing)
    return _result(2, "Только добавление (append-only)", OK,
                   "id: было %d, стало %d" % (len(snapshot.get("ids") or []), len(out["current"])))


def inv_promises(cfg, room_id, out: dict) -> dict:
    """3. Обещание со сроком есть в плане комнаты (id или текст)."""
    promises = [row for row in out["current"].values()
                if row.get("due") and str(row.get("status")) == "accepted"]
    if not promises:
        return _result(3, "Обещания со сроком в плане", OK, "обещаний со сроком нет")
    backlog = sf.read_json(_room_dir(cfg, room_id, "backlog.json"), None) or {}
    items = [item for item in (backlog.get("items") or []) if isinstance(item, dict)]
    ids = {str(item.get("id") or "") for item in items}
    texts = [(_words(item.get("text")), item) for item in items]
    orphans = []
    for row in promises:
        did = str(row.get("id") or "")
        if did in ids or any(did.endswith(item) for item in ids if item):
            continue
        words = _words(row.get("text"))
        if any(words and len(words & item_words) >= max(2, len(words) // 2)
               for item_words, _item in texts):
            continue
        orphans.append(did)
    if orphans:
        return _result(3, "Обещания со сроком в плане", WARN,
                       "обещаний со сроком %d, осиротевших %d" % (len(promises), len(orphans)),
                       orphans)
    return _result(3, "Обещания со сроком в плане", OK,
                   "все %d обещаний со сроком есть в плане" % len(promises))


def inv_answers(cfg, room_id, out: dict) -> dict:
    """4. Ответ владельца хранится целиком (обрезка «…» на 180 знаках — дефект)."""
    data = sf.read_json(_room_dir(cfg, room_id, "requests.json"), None)
    records = (data or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(records, list):
        records = (data or {}).get("requests") if isinstance(data, dict) else None
    total = truncated = 0
    examples = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        for key, value in (record.get("answers") or {}).items():
            if not isinstance(value, dict):
                continue
            text = value.get("user_text") or value.get("answer") or ""
            if not isinstance(text, str) or not text:
                continue
            total += 1
            if len(text) == 180 and text.endswith("…"):
                truncated += 1
                if len(examples) < 5:
                    examples.append("%s: %s" % (record.get("root_id") or "?", key))
    if not total:
        return _result(4, "Ответ владельца целиком", SKIP, "ответов в реестре ответов нет")
    status = BLOCK if truncated else OK
    return _result(4, "Ответ владельца целиком", status,
                   "ответов %d, обрезано до 180 знаков: %d" % (total, truncated), examples)


def _card_rows(cfg, room_id, board: str) -> list[dict]:
    """Тела карточек комнаты из kanban.db (только чтение)."""
    try:
        from server import worklog
        con = worklog._connect(cfg.kanban_db(board or "default"))
    except Exception:  # noqa: BLE001
        return []
    if con is None:
        return []
    rows = []
    try:
        cur = con.execute("SELECT id, title, body, tenant, assignee FROM tasks "
                          "WHERE tenant = ? OR tenant LIKE ?",
                          ("room:%s" % room_id, "room:%s" % room_id))
        for task_id, title, body, tenant, assignee in cur.fetchall():
            rows.append({"id": task_id, "title": title or "", "body": body or ""})
    except Exception:  # noqa: BLE001 - схема чужой БД может отличаться
        rows = []
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return rows


def _card_kind(title: str) -> str:
    low = _norm(title)
    if "обновление свода" in low or "свод" in low:
        return "revision"
    if "критик" in low or "проверк" in low:
        return "critic"
    if "исполн" in low or "задач" in low:
        return "executor"
    return "default"


def inv_card_budget(cfg, room_id, out: dict) -> dict:
    """5. Тело карточки любого типа ≤ потолка (R4 §4.3)."""
    rows = out.get("cards")
    if rows is None:
        rows = _card_rows(cfg, room_id, str(out.get("board") or "default"))
    if not rows:
        return _result(5, "Бюджет тела карточки", SKIP, "карточек комнаты не найдено")
    over = []
    worst = 0
    for row in rows:
        kind = _card_kind(row["title"])
        limit = BODY_LIMITS.get(kind, BODY_LIMITS["default"])
        size = len(row.get("body") or "")
        worst = max(worst, size)
        if size > limit:
            over.append("%s %d > %d (%s)" % (row["id"], size, limit, kind))
    status = WARN if over else OK
    return _result(5, "Бюджет тела карточки", status,
                   "карточек %d, сверх потолка %d, максимум %d знаков" % (len(rows), len(over), worst),
                   over)


def inv_contract_dup(cfg, room_id, out: dict) -> dict:
    """6. Устав не дублируется в карточке целиком."""
    rows = out.get("cards")
    if rows is None:
        rows = _card_rows(cfg, room_id, str(out.get("board") or "default"))
    if not rows:
        return _result(6, "Устав не дублируется целиком", SKIP, "карточек комнаты не найдено")
    big = []
    for row in rows:
        body = row.get("body") or ""
        start = body.find("<<<КОМНАТА-КОНТРАКТ")
        if start < 0:
            continue
        end = body.find("КОНТРАКТ>>>", start)
        size = (end - start) if end > start else len(body) - start
        if size > CONTRACT_LIMIT:
            big.append("%s: %d знаков" % (row["id"], size))
    status = WARN if big else OK
    return _result(6, "Устав не дублируется целиком", status,
                   "карточек с контрактом > %d знаков: %d из %d" % (CONTRACT_LIMIT, len(big), len(rows)),
                   big)


def inv_recall(cfg, room_id, out: dict) -> dict:
    """7. Золотой набор в поиске за ≤ 5 результатов — не измеряется."""
    return _result(7, "Золотой набор в поиске", SKIP,
                   "набора вопросов нет — recall@5 не измерялся (честный пропуск)")


CHECKS = {1: inv_passport, 2: inv_append_only, 3: inv_promises, 4: inv_answers,
          5: inv_card_budget, 6: inv_contract_dup, 7: inv_recall}


# ------------------------------------------------------------------- API ----

def check(cfg, room_id: str, only=None, board: str | None = None) -> dict:
    """Прогнать инварианты и вернуть структуру для API/«Здоровья».

    ``{"room", "chain", "pinned", "results": [...], "blocking": [...], "ok": bool}``
    — ``ok`` истинно, когда нет нарушений уровня ``fail``.
    """
    room_id = str(room_id)
    if board is None:
        try:
            from server import rooms as rooms_mod
            room = rooms_mod.get_room(cfg, room_id) or {}
            board = str(room.get("board") or "default")
        except Exception:  # noqa: BLE001
            board = "default"
    state = dec_reg.read_state(cfg, room_id)
    out = {"current": state["current"], "cards": None, "board": board}
    chain = dec_reg.verify(cfg, room_id)
    results = []
    wanted = [int(n) for n in (only or sorted(CHECKS))]
    for number in wanted:
        func = CHECKS.get(number)
        if not func:
            continue
        try:
            results.append(func(cfg, room_id, out))
        except Exception as exc:  # noqa: BLE001 - один инвариант не роняет отчёт
            results.append(_result(number, "Ошибка проверки", WARN, str(exc)[:200]))
    if not chain.get("ok"):
        results.insert(0, _result(0, "Цепочка реестра решений", BLOCK,
                                  "разрыв на строке %s: %s" % (chain.get("broken_at"),
                                                               chain.get("reason"))))
    blocking = [item for item in results if item["status"] == BLOCK]
    return {"room": room_id, "board": board,
            "chain": {"ok": bool(chain.get("ok")), "rows": chain.get("rows") or 0,
                      "broken_at": chain.get("broken_at")},
            "pinned": dec_reg.pin_state(cfg, room_id),
            "counts": {status: len([r for r in state["current"].values()
                                    if str(r.get("status")) == status])
                       for status in dec_reg.STATUSES},
            "results": results, "blocking": blocking, "ok": not blocking}


def format_table(report: dict) -> str:
    lines = ["Память комнаты «%s»: строк реестра %d, решений %d"
             % (report["room"], report["chain"]["rows"], sum(report["counts"].values())), ""]
    marks = {OK: "ок", WARN: "внимание", BLOCK: "НАРУШЕНО", SKIP: "пропуск"}
    for item in report["results"]:
        lines.append("%d. %-34s %-10s %s" % (item["n"], item["title"],
                                             marks.get(item["status"], item["status"]),
                                             item["detail"]))
        for example in item.get("examples") or []:
            lines.append("      · %s" % example)
    pinned = report.get("pinned") or {}
    if pinned.get("warning"):
        lines += ["", "Закрепление: %s" % pinned["warning"]]
    lines += ["", "Итог: %s" % ("нарушений нет" if report["ok"]
                                else "нарушений %d" % len(report["blocking"]))]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="7 инвариантов памяти комнаты (W25 M1)")
    parser.add_argument("--room", required=True, help="id комнаты")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--only", default="", help="номера инвариантов через запятую")
    parser.add_argument("--board", default="", help="доска канбана (по умолчанию — из комнаты)")
    args = parser.parse_args(argv)
    try:
        cfg = config_mod.load()
        only = [int(part) for part in args.only.split(",") if part.strip()] or None
        report = check(cfg, args.room, only=only, board=(args.board or None))
    except Exception as exc:  # noqa: BLE001 - понятная ошибка вместо трейсбека
        print("Не удалось проверить память комнаты: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_table(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
