"""Синтетическая фикстура «Хода работы»: `state.db` и мини-доска — ВЫМЫШЛЕННЫЕ данные.

Зачем: показывать и проверять ленту событий агентов, не трогая живые базы Hermes.
Все имена, названия и тексты придуманы (кафе «Лето», поставщик «Ромашка», сотрудники
Анна / Лев / Марина); никаких данных из боевой доски и `state.db` здесь нет и быть не может.

Что строит:

* `state.db` — таблицы `sessions` и `messages` в той же форме, что у Hermes, с рассуждениями,
  вызовами инструментов, результатом с ошибкой, длинным рассуждением, «секретом» в тексте и
  сжатой/наблюдённой записью (её показывать нельзя);
* `kanban.db` — две карточки (руководитель и исполнитель) и связь между ними, чтобы было
  видно передачу работы («Лев → Анна: …»).

    python tools/worklog_fixture_db.py --out <папка>         # обе базы
    python tools/worklog_fixture_db.py --out <папка> --state-only --profile anna

В тестах используется напрямую:

    from tools.worklog_fixture_db import create_state_db, create_kanban_db, SCENARIO
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: id карточек вымышленного запроса
ROOT_CARD = "t_1a2b3c4d"
WORKER_CARD = "t_5e6f7a8b"
#: профили участников
LEADER = "lev"
WORKER = "anna"
#: сеансы (движок пишет их в state.db профиля)
LEADER_SESSION = "s_0000aa11"
WORKER_SESSION = "s_0000bb22"

_LONG_THOUGHT = (
    "Сначала проверю, что в запросе кафе «Лето» действительно нужны три поставщика, а не два: "
    "в письме Марины сказано «сравнить условия», но не сказано, по каким именно позициям. "
    "Дальше соберу сроки поставки из договора с «Ромашкой» и сверю с прошлой закупкой, "
    "потому что там был срыв на две недели и за это уже платили неустойку. "
    "Отдельно проверю, кто отвечает за приёмку товара на складе, иначе снова получится, "
    "что счёт закрыт, а коробки никто не пересчитал. "
) * 3

SCENARIO = {
    "root_card": ROOT_CARD,
    "worker_card": WORKER_CARD,
    "leader": LEADER,
    "worker": WORKER,
    "leader_session": LEADER_SESSION,
    "worker_session": WORKER_SESSION,
}

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'kanban', user_id TEXT,
    model TEXT, started_at REAL NOT NULL, ended_at REAL, message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0, last_activity_at REAL, profile_name TEXT,
    title TEXT, billing_provider TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    timestamp REAL NOT NULL, reasoning TEXT, reasoning_content TEXT,
    observed INTEGER DEFAULT 0, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0
);
"""

KANBAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
    created_by TEXT, created_at INTEGER, started_at INTEGER, last_heartbeat_at INTEGER,
    tenant TEXT, priority INTEGER DEFAULT 1, consecutive_failures INTEGER DEFAULT 0,
    current_run_id INTEGER, model_override TEXT, provider_override TEXT, reasoning_effort TEXT
);
CREATE TABLE IF NOT EXISTS task_links (parent_id TEXT, child_id TEXT);
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, status TEXT,
    started_at INTEGER, ended_at INTEGER, metadata TEXT, summary TEXT, outcome TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER
);
"""


def _call(name: str, arguments: dict) -> dict:
    tag = hashlib.sha1(("%s|%s" % (name, json.dumps(arguments, sort_keys=True))).encode("utf-8"))
    return {"id": "call_%s" % tag.hexdigest()[:8], "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def _messages(now: int) -> list[dict]:
    """Сценарий ленты: рассуждение → действие → результат → ошибка → ответ."""
    return [
        {"sid": LEADER_SESSION, "role": "user", "content":
            "Запрос кафе «Лето»: сравнить условия двух поставщиков и предложить, с кем работать. "
            "Карточка %s." % ROOT_CARD, "ts": now - 600},
        {"sid": LEADER_SESSION, "role": "assistant", "content": "",
         "reasoning": "Разберу запрос: нужны сроки, цены и штрафы. Отдам проверку договора Анне.",
         "tool_calls": [_call("kanban_create", {"title": "Проверить договор с «Ромашкой»",
                                                "assignee": WORKER})],
         "ts": now - 570},
        {"sid": LEADER_SESSION, "role": "tool", "tool_name": "kanban_create",
         "content": json.dumps({"ok": True, "id": WORKER_CARD}), "ts": now - 568},
        {"sid": LEADER_SESSION, "role": "assistant",
         "content": "Анна проверяет договор, я пока соберу сроки поставки по прошлой закупке.",
         "reasoning": "Проверю прошлую закупку: там был срыв на две недели.",
         "tool_calls": [_call("search_files", {"pattern": "неустойка", "path": "C:\\Дела\\Лето"})],
         "ts": now - 540},
        {"sid": LEADER_SESSION, "role": "tool", "tool_name": "search_files",
         "content": "Найдено 2 фрагмента в договоре поставки.", "ts": now - 538},
        # сжатая запись: показывать нельзя
        {"sid": LEADER_SESSION, "role": "assistant", "content": "СЖАТАЯ ЗАПИСЬ — не показывать",
         "reasoning": "тоже не показывать", "compacted": 1, "ts": now - 500},
        {"sid": WORKER_SESSION, "role": "user", "content":
            "Проверь договор с «Ромашкой». Карточка %s." % WORKER_CARD, "ts": now - 500},
        {"sid": WORKER_SESSION, "role": "assistant", "content": "", "reasoning": _LONG_THOUGHT,
         "tool_calls": [_call("read_file", {"path": "C:\\Дела\\Лето\\Договор_Ромашка.md"})],
         "ts": now - 480},
        {"sid": WORKER_SESSION, "role": "tool", "tool_name": "read_file",
         "content": "Договор поставки. Срок — 14 дней. Неустойка 0,1% за день просрочки.",
         "ts": now - 478},
        {"sid": WORKER_SESSION, "role": "assistant", "content": "", "reasoning": "Проверю сроки оплаты.",
         "tool_calls": [_call("terminal", {"command": "type config.yaml | findstr password="})],
         "ts": now - 460},
        {"sid": WORKER_SESSION, "role": "tool", "tool_name": "terminal",
         "content": "Ошибка: файл config.yaml не найден", "ts": now - 458},
        {"sid": WORKER_SESSION, "role": "assistant",
         "content": "Договор проверила: срок 14 дней, неустойка 0,1%. Ключ доступа sk-test000000000000 не нужен.",
         "reasoning": "Сравню с условиями второго поставщика.", "ts": now - 430},
        {"sid": WORKER_SESSION, "role": "tool", "tool_name": "read_file",
         "content": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+P0A=",
         "ts": now - 428},
        # наблюдённая служебная запись: показывать нельзя
        {"sid": WORKER_SESSION, "role": "system", "content": "наблюдено", "observed": 1,
         "ts": now - 420},
    ]


def create_state_db(path, profile: str, *, now: int | None = None,
                    sessions: list[tuple[str, float, float | None]] | None = None,
                    shift: int = 0) -> Path:
    """Построить `state.db` профиля с вымышленным сеансом (схема — как у Hermes).

    `shift` — сдвинуть всю активность в прошлое на N секунд (для проверки «завис»).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    now = int(time.time()) if now is None else int(now)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(STATE_SCHEMA)
        rows = [row for row in _messages(now) if row["sid"] in
                (LEADER_SESSION, WORKER_SESSION)]
        if shift:
            rows = [dict(row, ts=row["ts"] - int(shift)) for row in rows]
        mine = [row for row in rows if (row["sid"] == LEADER_SESSION) == (profile == LEADER)]
        if not mine:
            mine = rows
        sids = {row["sid"] for row in mine}
        for sid in sorted(sids):
            first = min(row["ts"] for row in mine if row["sid"] == sid)
            last = max(row["ts"] for row in mine if row["sid"] == sid)
            con.execute(
                "INSERT OR REPLACE INTO sessions (id, source, model, started_at, ended_at, "
                "message_count, tool_call_count, last_activity_at, profile_name, title) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, "kanban", "fictional-model-1", float(first), None,
                 len([r for r in mine if r["sid"] == sid]),
                 len([r for r in mine if r["sid"] == sid and r.get("tool_name")]),
                 float(last), profile, "Вымышленный сеанс «Лето»"))
        for row in mine:
            con.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                "timestamp, reasoning, reasoning_content, observed, active, compacted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["sid"], row["role"], row.get("content") or "",
                 json.dumps(row["tool_calls"], ensure_ascii=False) if row.get("tool_calls") else None,
                 row.get("tool_name"), float(row["ts"]), row.get("reasoning"),
                 row.get("reasoning_content"), int(row.get("observed") or 0),
                 int(row.get("active", 1)), int(row.get("compacted") or 0)))
        con.commit()
    finally:
        con.close()
    return path


def create_kanban_db(path, *, now: int | None = None, stuck: bool = False) -> Path:
    """Мини-доска: корень руководителя, карточка исполнителя и прогоны с сеансами."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    now = int(time.time()) if now is None else int(now)
    old = now - (STUCK_MIN_TEST * 60 if stuck else 60)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(KANBAN_SCHEMA)
        con.execute("INSERT INTO tasks (id, title, body, assignee, status, created_by, created_at, "
                    "started_at, tenant, priority, consecutive_failures) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ROOT_CARD, "Сравнить поставщиков для кафе «Лето»", "Вымышленный запрос",
                     LEADER, "running", "Марина", now - 700, old, "room:demo", 1, 0))
        con.execute("INSERT INTO tasks (id, title, body, assignee, status, created_by, created_at, "
                    "started_at, tenant, priority, consecutive_failures) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (WORKER_CARD, "Проверить договор с «Ромашкой»", "Вымышленная задача",
                     WORKER, "running", LEADER, now - 560, old, "room:demo", 1, 0))
        con.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)",
                    (ROOT_CARD, WORKER_CARD))
        for card, profile, sid in ((ROOT_CARD, LEADER, LEADER_SESSION),
                                   (WORKER_CARD, WORKER, WORKER_SESSION)):
            con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                        "VALUES (?,?,?,?,?)",
                        (card, profile, "running", old,
                         json.dumps({"worker_session_id": sid})))
            con.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                        "VALUES (?,?,?,?)",
                        (card, "claimed", json.dumps({"profile": profile}), old))
        con.commit()
    finally:
        con.close()
    return path


#: для «зависшей» карточки: активность на 40 минут назад
STUCK_MIN_TEST = 40


def build(out, *, now: int | None = None, stuck: bool = False,
          state_only: bool = False, profile: str | None = None) -> dict:
    """Построить фикстуру в папке `out`.

    Раскладка как у Hermes: `hermes/<профиль>/state.db` (для `Cfg.hermes_home = out/hermes`)
    и `kanban_home/kanban/boards/demo/kanban.db` (для `Cfg.kanban_home = out/kanban_home`,
    доска `demo`).
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    now = int(time.time()) if now is None else int(now)
    made: dict = {"out": str(out), "boards": {}}
    if not state_only:
        db = create_kanban_db(out / "kanban_home" / "kanban" / "boards" / "demo" / "kanban.db",
                              now=now, stuck=stuck)
        made["kanban"] = str(db)
        made["kanban_home"] = str(out / "kanban_home")
    for name in ([profile] if profile else [LEADER, WORKER]):
        made["state_%s" % name] = str(create_state_db(
            out / "hermes" / "profiles" / name / "state.db", name, now=now,
            shift=STUCK_MIN_TEST * 60 if stuck else 0))
    made["hermes_home"] = str(out / "hermes")
    return made


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Синтетическая фикстура «Хода работы» (вымышленные данные)")
    parser.add_argument("--out", required=True, help="папка для фикстуры")
    parser.add_argument("--state-only", action="store_true", help="только state.db, без мини-доски")
    parser.add_argument("--profile", default="", help="один профиль (по умолчанию оба)")
    parser.add_argument("--stuck", action="store_true", help="карточки «зависли» (нет активности 40 мин)")
    args = parser.parse_args(argv)
    made = build(args.out, stuck=args.stuck, state_only=args.state_only,
                 profile=args.profile or None)
    print(json.dumps(made, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
