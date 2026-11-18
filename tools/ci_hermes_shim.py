#!/usr/bin/env python3
"""Тонкая замена CLI `hermes` для CI: только вызовы песочницы и тестов.

Зачем: на чистом раннере GitHub Actions нет Hermes Desktop и его CLI, а тестам
приложения нужен ровно один его глагол — создание доски канбана в песочнице
(`tools/seed_sandbox.py` → `hermes kanban boards create`), плюс те сквозные
проверки, которые пишут карточки настоящей командой (критик/доработка).

Что делает shim:
  * `hermes kanban boards create <slug> [--name <имя>]` — создаёт
    `<HERMES_KANBAN_HOME>/kanban/boards/<slug>/kanban.db` со схемой Hermes и
    `board.json` рядом;
  * `hermes kanban --board <b> create <заголовок> [...]` — карточка (поддерживает
    `--triage`, `--assignee`, `--parent`, `--tenant`, `--created-by`,
    `--idempotency-key`, `--body-file`, `--model`, `--provider`, `--json`);
  * `complete <id> --summary … [--metadata <json>]` — завершение карточки
    (`task_runs` + событие `completed`);
  * `comment <id> [--author <кто>] -- <текст>`, `show <id> [--json]`,
    `list [--json]`, `boards list`.

Чего НЕ делает (и не должен): диспетчер, декомпозиция, запуск агентов, работа с
моделями, профилями и настоящей доской. На такие команды shim отвечает понятной
ошибкой с кодом 2 — тест падает честно, а не получает выдуманный результат.

Рядом лежат `ci_hermes_shim.cmd` (Windows) и `ci_hermes_shim.sh` (macOS/Linux):
CI кладёт один из них в PATH под именем `hermes`.

    HERMES_KANBAN_HOME=/tmp/kh hermes kanban boards create team --name Team
    python tools/ci_hermes_shim.py --version      # «team-room-ci-hermes-shim …»

Признак подмены для `tests/ci_marks.py` — переменная `TEAMROOM_CI_HERMES_SHIM=1`
(её ставит workflow) и строка `--version`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

SHIM_VERSION = "1.0"
SHIM_MARK = "team-room-ci-hermes-shim"
DEFAULT_BOARD = "team"
VERSION_LINE = "%s %s (замена hermes для проверок CI)" % (SHIM_MARK, SHIM_VERSION)

#: Схема доски канбана — ровно та, что создаёт `hermes kanban boards create`
#: (сверено с новым файлом доски; комментарии убраны, поведение то же).
SCHEMA_SQL = """
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    worker_started_at    INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER,
    workflow_template_id TEXT,
    current_step_key     TEXT,
    skills               TEXT,
    model_override       TEXT,
    provider_override    TEXT,
    reasoning_effort     TEXT,
    max_retries          INTEGER,
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    goal_max_turns       INTEGER,
    session_id           TEXT,
    block_kind           TEXT,
    block_recurrences    INTEGER NOT NULL DEFAULT 0
, completion_contract TEXT);

CREATE TABLE task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    worker_started_at   INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

CREATE TABLE kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    user_id_alt   TEXT,
    chat_type     TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

CREATE INDEX idx_tasks_status          ON tasks(status);

CREATE INDEX idx_links_child           ON task_links(child_id);

CREATE INDEX idx_links_parent          ON task_links(parent_id);

CREATE INDEX idx_comments_task         ON task_comments(task_id, created_at);

CREATE INDEX idx_events_task           ON task_events(task_id, created_at);

CREATE INDEX idx_runs_task             ON task_runs(task_id, started_at);

CREATE INDEX idx_runs_status           ON task_runs(status);

CREATE INDEX idx_attachments_task      ON task_attachments(task_id, created_at);

CREATE INDEX idx_notify_task           ON kanban_notify_subs(task_id);

CREATE INDEX idx_tasks_assignee_status ON tasks(assignee, status);

CREATE INDEX idx_tasks_tenant ON tasks(tenant);

CREATE INDEX idx_tasks_idempotency ON tasks(idempotency_key);

CREATE INDEX idx_tasks_session_id ON tasks(session_id);

CREATE INDEX idx_events_run ON task_events(run_id, id);
"""


# --------------------------------------------------------------------- утилиты

def die(message: str, code: int = 2) -> "NoReturn":  # type: ignore[valid-type]
    sys.stderr.write("kanban: %s\n" % message)
    raise SystemExit(code)


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def kanban_home() -> Path:
    """Корень канбана: `HERMES_KANBAN_HOME` (иначе — от HERMES_HOME)."""
    env = os.environ.get("HERMES_KANBAN_HOME")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home).expanduser() / "kanban"
    die("не задан HERMES_KANBAN_HOME — shim работает только с песочницей")


def board_db(board: str) -> Path:
    return kanban_home() / "kanban" / "boards" / board / "kanban.db"


def board_json(board: str) -> Path:
    return kanban_home() / "kanban" / "boards" / board / "board.json"


def connect(board: str, *, create: bool = False) -> sqlite3.Connection:
    db = board_db(board)
    if not db.is_file():
        if not create:
            die("доска «%s» не создана (%s)" % (board, db))
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=15)
    con.row_factory = sqlite3.Row
    if create:
        con.executescript(SCHEMA_SQL)
        con.commit()
    return con


def new_id() -> str:
    return "t_" + uuid.uuid4().hex[:8]


def event(con, task_id: str, kind: str, payload: dict | None = None, run_id=None) -> None:
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, run_id, kind,
                 json.dumps(payload or {}, ensure_ascii=False), int(time.time())))


def task_dict(row) -> dict:
    keys = ("id", "title", "body", "assignee", "status", "priority", "created_by",
            "created_at", "started_at", "completed_at", "tenant", "idempotency_key",
            "result", "current_run_id")
    data = {}
    for key in keys:
        try:
            data[key] = row[key]
        except (IndexError, KeyError):
            data[key] = None
    return data


# ---------------------------------------------------------------- разбор команд

GLOBAL_WITH_VALUE = ("--board", "--profile", "--home")


def split_global(argv: list[str]) -> tuple[dict, list[str]]:
    """Отделить глобальные флаги (`--board X`, `--board=X`) от команды."""
    opts: dict = {"board": os.environ.get("HERMES_KANBAN_BOARD") or DEFAULT_BOARD}
    rest: list[str] = []
    pre: list[str] = []          # флаги перед командой (`kanban --json list`)
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "kanban" and not rest:
            index += 1
            continue
        if token in GLOBAL_WITH_VALUE and index + 1 < len(argv):
            name = token[2:]
            value = argv[index + 1]
            if name == "board":
                opts["board"] = value
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            name, _, value = token[2:].partition("=")
            if name in ("board", "profile", "home"):
                if name == "board":
                    opts["board"] = value
                index += 1
                continue
        if token.startswith("--board") and token.endswith("="):
            index += 1
            continue
        if token.startswith("--") and not rest:
            pre.append(token)          # флаг перед командой — вернём в конец списка
            index += 1
            continue
        rest.append(token)
        index += 1
    return opts, rest + pre


def take_flag(args: list[str], *names: str) -> tuple:
    """Забрать флаг со значением: `--name X`, `--name=X`. Возвращает (значение, args)."""
    value = None
    rest: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        matched = None
        for name in names:
            if token == "--" + name:
                matched = ("next", name)
                break
            if token.startswith("--" + name + "="):
                matched = ("inline", token.split("=", 1)[1])
                break
        if not matched:
            rest.append(token)
            index += 1
            continue
        if matched[0] == "next":
            if index + 1 >= len(args):
                die("флаг --%s требует значения" % matched[1])
            value = args[index + 1]
            index += 2
        else:
            value = matched[1]
            index += 1
    return value, rest


def has_flag(args: list[str], name: str) -> tuple:
    return ("--" + name) in args, [t for t in args if t != "--" + name]


def positional_after_dashes(args: list[str]) -> list[str]:
    """То, что идёт после `--` (текст, начинающийся с дефиса, — тоже текст)."""
    if "--" not in args:
        return []
    return args[args.index("--") + 1:]


# ------------------------------------------------------------------- глаголы

def cmd_boards(args: list[str]) -> int:
    if not args:
        die("boards: нужна подкоманда")
    sub = args[0]
    tail = args[1:]
    if sub == "create":
        if not tail:
            die("boards create: нужен слаг доски")
        slug = tail[0]
        if not slug.replace("-", "").replace("_", "").isalnum():
            die("boards create: недопустимый слаг «%s»" % slug)
        name, _rest = take_flag(tail[1:], "name")
        db = board_db(slug)
        if db.is_file():
            die("доска «%s» уже есть" % slug, 1)
        con = connect(slug, create=True)
        con.close()
        meta = {"slug": slug, "name": name or slug, "description": "", "icon": "", "color": "",
                "default_workdir": None, "project_id": None,
                "created_at": int(time.time()), "archived": False}
        board_json(slug).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        out("Board '%s' created." % slug)
        out("  Display name: %s" % meta["name"])
        out("  DB path:      %s" % db)
        return 0
    if sub == "list":
        root = kanban_home() / "kanban" / "boards"
        if not root.is_dir():
            out("(доск нет)")
            return 0
        for entry in sorted(root.iterdir()):
            if (entry / "kanban.db").is_file():
                out(entry.name)
        return 0
    die("boards %s: shim это не поддерживает" % sub)


def cmd_create(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    as_json, args = has_flag(args, "json")
    triage, args = has_flag(args, "triage")
    assignee, args = take_flag(args, "assignee")
    parent, args = take_flag(args, "parent")
    tenant, args = take_flag(args, "tenant")
    created_by, args = take_flag(args, "created-by")
    idem, args = take_flag(args, "idempotency-key")
    body_file, args = take_flag(args, "body-file")
    title_arg, args = take_flag(args, "title")
    for flag in ("model", "provider", "workspace", "priority", "skills", "reason"):
        _value, args = take_flag(args, flag)
    positional = [t for t in args if not t.startswith("--")]
    title = title_arg or (positional[0] if positional else "")
    if not title:
        die("create: нужен заголовок карточки")
    body = ""
    if body_file:
        try:
            body = Path(body_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            die("create: не прочитать --body-file: %s" % exc)
    con = connect(board)
    try:
        if idem:
            row = con.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (idem,)).fetchone()
            if row is not None:                       # повтор — та же карточка
                data = task_dict(row)
                out(json.dumps(data, ensure_ascii=False) if as_json else data["id"])
                return 0
        if parent:
            exists = con.execute("SELECT id FROM tasks WHERE id = ?", (parent,)).fetchone()
            if exists is None:
                die("create: нет родительской карточки %s" % parent)
        card_id = new_id()
        status = "triage" if triage else ("ready" if assignee else "todo")
        now = int(time.time())
        con.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, "
            "created_at, tenant, idempotency_key, consecutive_failures) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (card_id, title, body, assignee, status, 0, created_by or "cli", now,
             tenant, idem))
        event(con, card_id, "created", {"status": status})
        if parent:
            con.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?,?)",
                        (parent, card_id))
            event(con, card_id, "linked", {"parent_id": parent})
        con.commit()
        data = {"id": card_id, "title": title, "assignee": assignee, "status": status,
                "tenant": tenant, "parent": parent}
        if as_json:
            out(json.dumps(data, ensure_ascii=False))
        else:
            out(card_id)
        return 0
    finally:
        con.close()


def cmd_complete(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    as_json, args = has_flag(args, "json")
    summary, args = take_flag(args, "summary")
    metadata, args = take_flag(args, "metadata")
    result, args = take_flag(args, "result")
    positional = [t for t in args if not t.startswith("--")]
    if not positional:
        die("complete: нужен id карточки")
    task_id = positional[0]
    con = connect(board)
    try:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            die("unknown task %s" % task_id)
        now = int(time.time())
        meta_text = None
        if metadata:
            try:
                json.loads(metadata)
            except ValueError:
                die("complete: --metadata должен быть JSON")
            meta_text = metadata
        run = con.execute("SELECT id FROM task_runs WHERE task_id = ? AND status = 'running' "
                          "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if run:
            run_id = run["id"]
            con.execute("UPDATE task_runs SET status='done', outcome='completed', summary=?, "
                        "metadata=?, ended_at=? WHERE id=?",
                        (summary, meta_text, now, run_id))
        else:
            run_id = con.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, summary, metadata, "
                "started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, row["assignee"], "done", "completed", summary, meta_text, now, now)
            ).lastrowid
        con.execute("UPDATE tasks SET status='done', completed_at=?, current_run_id=?, "
                    "claim_lock=NULL, last_failure_error=NULL, result=? WHERE id=?",
                    (now, run_id, result, task_id))
        event(con, task_id, "completed", {"run_id": run_id, "summary": (summary or "")[:200]},
              run_id=run_id)
        con.commit()
        if as_json:
            out(json.dumps({"id": task_id, "status": "done", "run_id": run_id},
                           ensure_ascii=False))
        else:
            out("Task %s completed." % task_id)
        return 0
    finally:
        con.close()


def cmd_comment(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    author, args = take_flag(args, "author")
    text = " ".join(positional_after_dashes(args)).strip()
    positional = [t for t in args if not t.startswith("--")]
    if not positional:
        die("comment: нужен id карточки")
    task_id = positional[0]
    if not text:
        die("comment: пустой текст")
    con = connect(board)
    try:
        if con.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            die("unknown task %s" % task_id)
        con.execute("INSERT INTO task_comments (task_id, author, body, created_at) "
                    "VALUES (?,?,?,?)", (task_id, author or "Михаил", text, int(time.time())))
        event(con, task_id, "commented", {"author": author or ""})
        con.commit()
        out("Comment added to %s." % task_id)
        return 0
    finally:
        con.close()


def cmd_show(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    as_json, args = has_flag(args, "json")
    positional = [t for t in args if not t.startswith("--")]
    if not positional:
        die("show: нужен id карточки")
    con = connect(board)
    try:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (positional[0],)).fetchone()
        if row is None:
            die("unknown task %s" % positional[0])
        data = task_dict(row)
        if as_json:
            out(json.dumps(data, ensure_ascii=False))
        else:
            for key, value in data.items():
                out("%s: %s" % (key, "" if value is None else value))
        return 0
    finally:
        con.close()


def cmd_list(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    as_json, args = has_flag(args, "json")
    status, args = take_flag(args, "status")
    tenant, args = take_flag(args, "tenant")
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if tenant:
        sql += " AND tenant = ?"
        params.append(tenant)
    sql += " ORDER BY created_at DESC"
    con = connect(board)
    try:
        rows = [task_dict(row) for row in con.execute(sql, params)]
        if as_json:
            out(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                out("%s  %-8s  %s" % (row["id"], row["status"], row["title"]))
        return 0
    finally:
        con.close()


def positional_values(args: list[str]) -> list[str]:
    """Позиционные значения: всё после `--`, иначе токены без флагов."""
    if "--" in args:
        return args[args.index("--") + 1:]
    return [token for token in args if not token.startswith("--")]


def change_status(board: str, task_id: str, status: str, kind: str,
                  payload: dict | None = None, assignee: str | None = None) -> None:
    """Сменить статус карточки (и исполнителя) + событие — как делает движок."""
    con = connect(board)
    try:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            die("unknown task %s" % task_id)
        sets = ["status = ?"]
        params: list = [status]
        if assignee is not None:
            sets.append("assignee = ?")
            params.append(assignee)
        if status == "done":
            sets.append("completed_at = ?")
            params.append(int(time.time()))
        if status in ("todo", "ready", "triage"):
            sets += ["block_kind = NULL", "claim_lock = NULL"]
        con.execute("UPDATE tasks SET %s WHERE id = ?" % ", ".join(sets), params + [task_id])
        event(con, task_id, kind, payload or {"status": status})
        con.commit()
        out("Task %s: %s." % (task_id, status))
    finally:
        con.close()


def cmd_unblock(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    reason, args = take_flag(args, "reason")
    ids = positional_values(args)
    if not ids:
        die("unblock: нужен id карточки")
    for task_id in ids:
        # снятая блокировка возвращает карточку в очередь диспетчера
        change_status(board, task_id, "ready", "unblocked", {"reason": reason or ""})
    return 0


def cmd_assign(board: str, args: list[str], *, reclaim_note: str = "assigned") -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    _reason, args = take_flag(args, "reason")
    values = positional_values(args)
    if len(values) < 2:
        die("assign: нужны id карточки и профиль исполнителя")
    task_id, profile = values[0], values[1]
    con = connect(board)
    try:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            die("unknown task %s" % task_id)
        con.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        event(con, task_id, reclaim_note, {"assignee": profile})
        con.commit()
        out("Task %s → %s." % (task_id, profile))
        return 0
    finally:
        con.close()


def cmd_reassign(board: str, args: list[str]) -> int:
    return cmd_assign(board, args, reclaim_note="reassigned")


def cmd_promote(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    reason, args = take_flag(args, "reason")
    ids = positional_values(args)
    if not ids:
        die("promote: нужен id карточки")
    for task_id in ids:
        change_status(board, task_id, "ready", "promoted", {"reason": reason or ""})
    return 0


def cmd_reopen_review(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    reason, args = take_flag(args, "reason")
    ids = positional_values(args)
    if not ids:
        die("reopen-review: нужен id карточки")
    for task_id in ids:
        change_status(board, task_id, "ready", "reopened", {"reason": reason or ""})
    return 0


def cmd_archive(board: str, args: list[str]) -> int:
    board_arg, args = take_flag(args, "board")
    if board_arg:
        board = board_arg
    ids = positional_values(args)
    if not ids:
        die("archive: нужен id карточки")
    for task_id in ids:
        change_status(board, task_id, "archived", "archived")
    return 0


HANDLERS = {"create": cmd_create, "complete": cmd_complete, "comment": cmd_comment,
            "show": cmd_show, "list": cmd_list,
            "unblock": cmd_unblock, "assign": cmd_assign, "reassign": cmd_reassign,
            "promote": cmd_promote, "reopen-review": cmd_reopen_review,
            "archive": cmd_archive}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        out(VERSION_LINE)
        out("Shim умеет: kanban boards create|list, kanban create|complete|comment|show|list.")
        return 0
    if argv[0] in ("--version", "-V", "version"):
        out(VERSION_LINE)
        return 0
    try:
        index = argv.index("kanban")
    except ValueError:
        die("shim поддерживает только «hermes kanban …» (получено: %s)" % " ".join(argv[:3]))
    opts, rest = split_global(argv[index:])
    if not rest:
        out(VERSION_LINE)
        return 0
    if rest[0] == "boards":
        return cmd_boards(rest[1:])
    handler = HANDLERS.get(rest[0])
    if handler is None:
        die("shim не поддерживает команду «kanban %s»: она требует настоящего Hermes CLI "
            "(диспетчер, агенты, модели). В CI такие тесты пропускаются через tests/ci_marks.py."
            % rest[0])
    return handler(opts["board"], rest[1:])


if __name__ == "__main__":
    raise SystemExit(main())
