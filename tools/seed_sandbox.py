#!/usr/bin/env python
"""Песочница канбана со всеми состояниями запроса комнаты «Лето».

Что делает: создаёт во временном каталоге `<root>/kanban_home`, `<root>/data`,
`<root>/vault`, доску `team` в песочнице и 8 деревьев карточек с tenant
`room:cafe` — по одному на каждое состояние запроса (`queued`, `working`,
`reviewing`, `needs_you`, `done_modern`, `done_legacy`, `failed` и
`decomposed`-кандидат вне реестра). Само содержимое деревьев — в
`tools/seed_content.py`.

Изоляция: доска создаётся CLI `hermes kanban` с `HERMES_KANBAN_HOME=<root>/kanban_home`
(как в `tools/probe_sandbox.py`). Карточки, связи, run'ы и события пишутся
напрямую в SQLite — но ТОЛЬКО в БД внутри `root`: перед каждым соединением
`_assert_writable_db()` проверяет, что файл лежит под `root` и НЕ под реальным
`%HERMES_HOME%`. Это единственный способ выставить статусы `running`/`review`,
которых CLI не умеет (ENGINE-FACTS §4.1, §8.3).

Реальная доска не трогается вообще: её путь не открывается даже на запись.

Запуск:
    python tools/seed_sandbox.py --out C:/tmp/room-sandbox
    python -m unittest tests.test_seed -v
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

if not __package__:  # `python tools/seed_sandbox.py`: корень проекта на путь ДО импорта tools.*
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.seed_content import ROOM, TENANT, TREES, report_spec, vault_files
from server import proc as spawn  # noqa: E402  (единый скрытый запуск процессов)
from server import safety  # noqa: E402  (корень проекта уже на sys.path)

BOARD = "team"
BOARD_NAME = "Team"
MARKER = ".seeded.json"


# ---------------------------------------------------------------- утилиты


def _real_hermes_home() -> Path:
    """Реальный Hermes home — то, что трогать нельзя ни при каких условиях."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return (Path(local) / "hermes").resolve()


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _assert_writable_db(db: Path, root: Path) -> None:
    """Перед любой прямой записью: БД внутри песочницы и не в реальном Hermes."""
    real = _real_hermes_home()
    assert db.name == "kanban.db", f"не kanban.db: {db}"
    assert _is_under(db, root), f"БД вне песочницы {root}: {db}"
    assert _is_under(root, real) is False, f"песочница внутри реального Hermes {real}: {root}"
    assert _is_under(db, real) is False, f"БД внутри реального Hermes {real}: {db}"


# ---------------------------------------------------------------- запись


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _insert(con: sqlite3.Connection, table: str, row: dict) -> None:
    cols = [c for c in row if c in _columns(con, table)]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    con.execute(sql, [row[c] for c in cols])


def _insert_tree(con: sqlite3.Connection, tree: dict, *, created_by_default: str = "Михаил") -> None:
    for card in tree["cards"].values():
        _insert(con, "tasks", {
            "id": card["id"], "title": card["title"], "body": card["body"],
            "assignee": card["assignee"], "status": card["status"],
            "created_by": card["created_by"] or created_by_default,
            "created_at": card["created_at"], "started_at": card["started_at"],
            "completed_at": card["completed_at"], "tenant": card["tenant"],
            "idempotency_key": card["idempotency_key"], "priority": 1,
            "consecutive_failures": card["consecutive_failures"],
            "last_failure_error": card["last_failure_error"],
            "result": None,
        })
        run_id = None
        for run in card["runs"]:
            cur = con.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, summary, metadata, "
                "error, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (card["id"], run["profile"], run["status"], run["outcome"], run["summary"],
                 json.dumps(run["metadata"], ensure_ascii=False) if run["metadata"] else None,
                 run["error"], run["started_at"], run["ended_at"]))
            run_id = cur.lastrowid
        if card["status"] == "running" and run_id is not None:
            con.execute("UPDATE tasks SET current_run_id=?, last_heartbeat_at=?, claim_lock=? "
                        "WHERE id=?", (run_id, card["started_at"], f"worker:{card['assignee']}",
                                       card["id"]))
        for comment in card["comments"]:
            _insert(con, "task_comments", {"task_id": card["id"], "author": comment["author"],
                                           "body": comment["body"],
                                           "created_at": comment["created_at"]})
        _insert(con, "task_events", {"task_id": card["id"], "kind": "created",
                                     "payload": json.dumps({"status": card["status"]}),
                                     "created_at": card["created_at"]})
        for event in card["events"]:
            _insert(con, "task_events", {"task_id": card["id"], "run_id": run_id,
                                         "kind": event["kind"],
                                         "payload": json.dumps(event["payload"], ensure_ascii=False),
                                         "created_at": event["created_at"]})
        kinds = {e["kind"] for e in card["events"]}
        if card["status"] == "done" and "completed" not in kinds:
            _insert(con, "task_events", {"task_id": card["id"], "run_id": run_id,
                                         "kind": "completed",
                                         "payload": json.dumps({"status": "done"}),
                                         "created_at": card["completed_at"] or card["created_at"]})
        elif card["status"] == "blocked" and "blocked" not in kinds:
            _insert(con, "task_events", {"task_id": card["id"], "run_id": run_id,
                                         "kind": "blocked",
                                         "payload": json.dumps({"reason": "ожидает человека"},
                                                               ensure_ascii=False),
                                         "created_at": card["completed_at"] or card["created_at"]})
    for parent_id, child_id in tree["links"]:
        _insert(con, "task_links", {"parent_id": parent_id, "child_id": child_id})


def _write_report(data_dir: Path, slug: str, title: str) -> Path:
    out = data_dir / "reports" / ROOM / slug / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report_spec(title), ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _resolve_placeholders(tree: dict, vault: dict[str, Path], report_path: Path | None) -> None:
    """Подставить реальные пути песочницы вместо VAULT/REPORT в metadata."""
    def fix(value):
        if isinstance(value, str):
            if value.startswith("VAULT/"):
                rel = value[len("VAULT/"):]
                return str(vault["state"].parent.parent / rel)
            if value == "REPORT":
                return str(report_path) if report_path else value
            return value
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        return value

    for card in tree["cards"].values():
        for run in card["runs"]:
            if run["metadata"]:
                run["metadata"] = fix(run["metadata"])


def _cli_env(kanban_home: Path) -> dict:
    env = dict(os.environ)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        env.pop(key, None)
    return env


def _create_board(kanban_home: Path, cli: str) -> None:
    env = _cli_env(kanban_home)
    proc = spawn.run([cli, "kanban", "boards", "create", BOARD, "--name", BOARD_NAME],
                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                     env=env, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"не удалось создать доску {BOARD} в песочнице: "
                           f"{proc.stdout}\n{proc.stderr}")


def _verify(con: sqlite3.Connection, result: dict) -> None:
    """Проверить целостность посева до коммита: всё связано, все корни на месте."""
    orphans = con.execute(
        "SELECT count(*) FROM task_links WHERE parent_id NOT IN (SELECT id FROM tasks) "
        "OR child_id NOT IN (SELECT id FROM tasks)").fetchone()[0]
    if orphans:
        raise RuntimeError(f"в песочнице {orphans} связей на несуществующие карточки")
    for name in result:
        if name in ("board", "tenant", "room", "paths"):
            continue
        ids = [result[name]["root_id"]] + list(result[name]["child_ids"])
        found = con.execute(
            f"SELECT count(*) FROM tasks WHERE id IN ({','.join('?' for _ in ids)})",
            ids).fetchone()[0]
        if found != len(ids):
            raise RuntimeError(f"состояние {name}: найдено {found} из {len(ids)} карточек")


def build_sandbox(root: Path, *, cli: str | None = None) -> dict:
    """Создать песочницу со всеми состояниями в `root` и вернуть её описание.

    `root` должен быть пустым (первый запуск) либо уже содержать маркер прошлого
    запуска — тогда возвращается сохранённый результат.
    """
    root = Path(root).expanduser().resolve()
    real = _real_hermes_home()
    if _is_under(root, real):
        raise RuntimeError(f"песочница внутри реального Hermes {real}: {root}")
    safety.assert_not_real(root, "создание песочницы")

    marker = root / MARKER
    if marker.is_file():
        return json.loads(marker.read_text(encoding="utf-8"))
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"каталог песочницы не пуст: {root}")

    kanban_home = root / "kanban_home"
    data_dir = root / "data"
    vault_dir = root / "vault"
    for d in (kanban_home, data_dir, vault_dir):
        d.mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / ROOM).mkdir(parents=True, exist_ok=True)

    _create_board(kanban_home, cli or os.environ.get("TEAMROOM_HERMES_CLI") or shutil.which("hermes") or "hermes")

    vault = vault_files(vault_dir)
    db = kanban_home / "kanban" / "boards" / BOARD / "kanban.db"
    if not db.exists():
        raise RuntimeError(f"CLI не создал БД песочницы: {db}")
    _assert_writable_db(db, root)

    result: dict = {"board": BOARD, "tenant": TENANT, "room": ROOM}
    con = sqlite3.connect(str(db), timeout=10)
    try:
        con.execute("BEGIN")
        for factory in TREES:
            tree = factory()
            report_path = None
            if tree.get("report"):
                report_path = _write_report(data_dir, tree["slug"], tree["cards"]["root"]["title"])
            _resolve_placeholders(tree, vault, report_path)
            _insert_tree(con, tree)
            root_card = tree["cards"][tree["root_key"]]
            result[tree["name"]] = {
                "root_id": root_card["id"],
                "slug": tree["slug"],
                "status": root_card["status"],
                "child_ids": [tree["cards"][k]["id"] for k in tree["child_keys"]],
                "extra_cards": tree["extra_cards"],
                "report_path": str(report_path) if report_path else None,
            }
        _verify(con, result)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    result["paths"] = {
        "kanban_home": str(kanban_home),
        "data": str(data_dir),
        "vault": str(vault_dir),
        "db": str(db),
        "env": {
            "TEAMROOM_HERMES_HOME": str(kanban_home),
            "TEAMROOM_DATA": str(data_dir),
            "TEAMROOM_VAULT_DIR": str(vault_dir),
            "HERMES_KANBAN_HOME": str(kanban_home),
        },
    }
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    """Внешняя точка входа: запрет записи в реальный Hermes/Vault и агентского CLI."""
    with safety.forbid_real_writes(), safety.forbid_real_subprocess():
        return _main(argv)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Песочница канбана со всеми состояниями запроса Лето")
    ap.add_argument("--out", required=True, help="каталог песочницы (пустой или несуществующий)")
    ap.add_argument("--cli", default=None, help="путь к hermes (по умолчанию из PATH)")
    args = ap.parse_args(argv)
    try:
        out = build_sandbox(Path(args.out), cli=args.cli)
    except Exception as exc:  # noqa: BLE001 - CLI должен показать причину
        print(f"seed_sandbox: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
