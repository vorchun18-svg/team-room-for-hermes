"""Песочница для backend-тестов и защита реального канбана.

`Sandbox` — контекстный менеджер: делает временный каталог ВНЕ реального
`%LOCALAPPDATA%\\hermes` (иначе `kanban_home()` всё равно вернёт настоящий корень
и изоляции не будет — ENGINE-FACTS §8.1), наполняет его `tools.seed_sandbox.py`,
выставляет `TEAMROOM_HERMES_HOME` / `TEAMROOM_DATA` / `TEAMROOM_VAULT_DIR`,
отдаёт `server.config.load()` и убирает за собой.

    from tests.helpers import Sandbox
    with Sandbox() as sb:
        before = sb.snapshot_real()
        ...
        sb.assert_real_untouched(before)

`snapshot_real()` читает реальную `kanban/boards/team/kanban.db` только в режиме
`mode=ro` (размер, mtime, число карточек, максимальная отметка времени) —
сравнение до/после доказывает, что тест не писал в живую доску (приёмка G).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import config as config_mod            # noqa: E402  (после настройки sys.path)
from server import safety                         # noqa: E402
from tools.seed_sandbox import (                  # noqa: E402
    BOARD,
    _assert_writable_db,
    _insert,
    _is_under,
    _real_hermes_home,
    build_sandbox,
)

SANDBOX_ENV_KEYS = ("TEAMROOM_HERMES_HOME", "TEAMROOM_DATA",
                    "TEAMROOM_VAULT_DIR", "HERMES_KANBAN_HOME")
REAL_BOARD = "team"


def _outside_hermes_base() -> Path:
    """Каталог для временных песочниц, гарантированно вне реального Hermes."""
    real = _real_hermes_home()
    candidates: list[Path] = []
    if os.environ.get("TEAMROOM_TEST_TMP"):
        candidates.append(Path(os.environ["TEAMROOM_TEST_TMP"]))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Temp")
    candidates.append(Path(tempfile.gettempdir()))
    candidates.append(PROJECT_ROOT / "data" / "_tests")
    for base in candidates:
        try:
            if _is_under(base, real):
                continue
            base.mkdir(parents=True, exist_ok=True)
            probe = tempfile.mkdtemp(prefix=".probe-", dir=str(base))
            os.rmdir(probe)
            return base
        except OSError:
            continue
    raise RuntimeError(f"не нашёл каталог вне реального Hermes {real} для песочницы тестов")


def real_hermes_home() -> Path:
    """Реальный `%HERMES_HOME%` — корень, который тесты не имеют права менять."""
    return _real_hermes_home()


def real_db_path(cfg: Any = None) -> Path:
    """Путь к реальной БД доски `team` (только чтение, никогда не песочная)."""
    home = Path(cfg.hermes_home) if cfg is not None else _real_hermes_home()
    return home / "kanban" / "boards" / REAL_BOARD / "kanban.db"


def snapshot_real_db(cfg: Any = None) -> dict:
    """Слепок реальной kanban.db: размер, mtime, число задач, max(*_at), mode=ro."""
    db = real_db_path(cfg)
    if not db.exists():
        return {"exists": False, "size": 0, "mtime": 0, "tasks_count": 0, "max_updated": 0}
    stat = db.stat()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        tasks_count = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
        max_updated = con.execute(
            "SELECT coalesce(max(max(coalesce(completed_at,0), "
            "coalesce(started_at,0), coalesce(created_at,0))), 0) FROM tasks").fetchone()[0]
    finally:
        con.close()
    return {"exists": True, "size": stat.st_size, "mtime": int(stat.st_mtime),
            "tasks_count": int(tasks_count), "max_updated": int(max_updated)}


def assert_real_untouched(before: dict, cfg: Any = None) -> dict:
    """Упасть, если реальная доска изменилась после слепка `before`."""
    after = snapshot_real_db(cfg)
    if after != before:
        changed = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
                   if before.get(k) != after.get(k)}
        raise AssertionError(f"реальная kanban.db изменилась: {changed}")
    return after


class SimAgents:
    """Имитация движка и агентов прямой записью в ПЕСОЧНУЮ kanban.db.

    Зачем: CLI не умеет ставить ``running``/``review`` и не «играет» за агентов,
    а сквозной сценарий требует настоящих состояний движка (decompose, run'ы,
    ``kanban_complete`` с metadata/вердиктом). Поэтому тест сам пишет карточки —
    но ТОЛЬКО в БД внутри песочницы: перед каждым соединением
    ``_assert_writable_db`` проверяет, что файл не в реальном Hermes.

        sa = SimAgents(sb)
        kids = sa.decompose(root_id, [{"title": "Срез", "assignee": "biz-cfo"}])
        sa.finish(kids[0], "Готово", {"next_steps": [...]})
    """

    def __init__(self, sandbox: "Sandbox") -> None:
        self.sb = sandbox
        self.db = Path(sandbox.db)
        self.root = Path(sandbox.root)
        self._seq = 0

    # --- служебное ---------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Соединение на запись — только песочная БД (иначе AssertionError)."""
        _assert_writable_db(self.db, self.root)
        con = sqlite3.connect(str(self.db), timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def _next_id(self, key: str) -> str:
        self._seq += 1
        raw = "%s:%s:%s" % (key, self._seq, time.time_ns())
        return "t_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]

    def _row(self, con, task_id: str):
        return con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def _event(self, con, task_id: str, kind: str, payload: dict | None = None, at: int | None = None):
        _insert(con, "task_events", {"task_id": task_id, "kind": kind,
                                     "payload": json.dumps(payload or {}, ensure_ascii=False),
                                     "created_at": at or int(time.time())})

    # --- карточки ----------------------------------------------------------
    def card(self, title: str, assignee: str, status: str = "todo", body: str = "",
             tenant: str | None = "room:cafe", parent: str | None = None,
             created_by: str = "Михаил", card_id: str | None = None) -> str:
        """Создать карточку (по умолчанию ``todo``) и, если нужно, связь с родителем."""
        cid = card_id or self._next_id(title)
        now = int(time.time())
        con = self.connect()
        try:
            _insert(con, "tasks", {"id": cid, "title": title, "body": body or title, "assignee": assignee,
                                   "status": status, "created_by": created_by, "created_at": now,
                                   "tenant": tenant, "priority": 1, "consecutive_failures": 0})
            self._event(con, cid, "created", {"status": status}, now)
            if parent:
                _insert(con, "task_links", {"parent_id": parent, "child_id": cid})
            con.commit()
        finally:
            con.close()
        return cid

    def decompose(self, root_id: str, children: list, assignee: str = "auto-decomposer") -> list:
        """Разложить корень: дети ``running`` + связи child_id=root + событие decomposed.

        ``children`` — список dict'ов ``{title, assignee, body?, status?}``.
        Корень остаётся ``todo``: движок ждёт детей; ``assignee`` — как в реальном
        событии ``decomposed`` (у комнаты другой директор → watcher переназначит).
        """
        con = self.connect()
        ids: list = []
        try:
            row = self._row(con, root_id)
            if row is None:
                raise AssertionError("SimAgents.decompose: нет карточки %s" % root_id)
            tenant = row["tenant"]
            now = int(time.time())
            for spec in children:
                cid = spec.get("id") or self._next_id(str(spec.get("title")))
                status = spec.get("status") or "running"
                _insert(con, "tasks", {"id": cid, "title": spec.get("title") or "Срез",
                                       "body": spec.get("body") or spec.get("title") or "Срез",
                                       "assignee": spec.get("assignee"), "status": status,
                                       "created_by": "auto-decomposer", "created_at": now,
                                       "started_at": now if status == "running" else None,
                                       "tenant": tenant, "priority": 1, "consecutive_failures": 0})
                self._event(con, cid, "created", {"status": status}, now)
                if status == "running":
                    con.execute(
                        "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                        (cid, spec.get("assignee"), "running", now))
                    con.execute("UPDATE tasks SET current_run_id=(SELECT max(id) FROM task_runs "
                                "WHERE task_id=?), last_heartbeat_at=? WHERE id=?", (cid, now, cid))
                    self._event(con, cid, "claimed", {"profile": spec.get("assignee")}, now)
                _insert(con, "task_links", {"parent_id": cid, "child_id": root_id})
                ids.append(cid)
            con.execute("UPDATE tasks SET assignee = ?, status = 'todo' WHERE id = ?", (assignee, root_id))
            self._event(con, root_id, "decomposed",
                        {"child_ids": ids, "root_assignee": assignee}, now)
            con.commit()
        finally:
            con.close()
        return ids

    def start(self, task_id: str) -> None:
        """Взять карточку в работу: running + открытый run."""
        now = int(time.time())
        con = self.connect()
        try:
            row = self._row(con, task_id)
            con.execute("UPDATE tasks SET status='running', started_at=coalesce(started_at, ?) "
                        "WHERE id=?", (now, task_id))
            con.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                        (task_id, row["assignee"], "running", now))
            con.execute("UPDATE tasks SET current_run_id=(SELECT max(id) FROM task_runs "
                        "WHERE task_id=?), last_heartbeat_at=? WHERE id=?", (task_id, now, task_id))
            self._event(con, task_id, "claimed", {"profile": row["assignee"]}, now)
            con.commit()
        finally:
            con.close()

    def finish(self, task_id: str, summary: str, metadata: dict | None = None,
               verdict: str | None = None, issues: list | None = None) -> None:
        """Завершить карточку как агент: run done/completed + summary/metadata."""
        meta = dict(metadata or {})
        if verdict is not None:
            meta["verdict"] = verdict
        if issues is not None:
            meta["issues"] = issues
        now = int(time.time())
        con = self.connect()
        try:
            row = self._row(con, task_id)
            if row is None:
                raise AssertionError("SimAgents.finish: нет карточки %s" % task_id)
            open_run = con.execute("SELECT id FROM task_runs WHERE task_id=? AND status='running' "
                                   "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            payload = json.dumps(meta, ensure_ascii=False) if meta else None
            if open_run:
                con.execute("UPDATE task_runs SET status='done', outcome='completed', summary=?, "
                            "metadata=?, ended_at=? WHERE id=?",
                            (summary, payload, now, open_run["id"]))
                run_id = open_run["id"]
            else:
                cur = con.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, summary, metadata, "
                    "started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
                    (task_id, row["assignee"], "done", "completed", summary, payload, now, now))
                run_id = cur.lastrowid
            con.execute("UPDATE tasks SET status='done', completed_at=?, current_run_id=?, "
                        "claim_lock=NULL, last_failure_error=NULL WHERE id=?",
                        (now, run_id, task_id))
            self._event(con, task_id, "completed", {"run_id": run_id, "summary": summary[:200]}, now)
            con.commit()
        finally:
            con.close()

    def comment(self, task_id: str, author: str, text: str) -> None:
        con = self.connect()
        try:
            _insert(con, "task_comments", {"task_id": task_id, "author": author, "body": text,
                                           "created_at": int(time.time())})
            con.commit()
        finally:
            con.close()

    def card_status(self, task_id: str) -> str:
        con = self.connect()
        try:
            row = self._row(con, task_id)
            return str(row["status"]) if row else ""
        finally:
            con.close()


class Sandbox:
    """Временная песочница комнаты: дерево карточек во всех состояниях."""

    def __init__(self) -> None:
        self._base = _outside_hermes_base()
        self.root = Path(tempfile.mkdtemp(prefix="teamroom-", dir=str(self._base)))
        self._saved_env: dict[str, str | None] = {}
        self._entered = False
        self._guards: list = []
        self.ids: dict = {}
        self.cfg: Any = None

    # --- жизненный цикл -----------------------------------------------------
    def _enter_guards(self) -> None:
        """Включить запрет записи в реальный Hermes/Vault и агентского CLI.

        С этого момента любой код внутри песочницы (и тесты, и сервер, и
        инструменты) получает `SafetyError` при попытке писать в реальный
        `%LOCALAPPDATA%\\hermes` или `C:\\TeamRoom\\Vault` — см. docs/SAFETY-RULES.md.
        """
        self._guards = []
        for cm in (safety.forbid_real_writes(), safety.forbid_real_subprocess()):
            cm.__enter__()
            self._guards.append(cm)

    def _exit_guards(self) -> None:
        while self._guards:
            cm = self._guards.pop()
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - снятие патчей не должно ронять уборку
                pass

    def __enter__(self) -> "Sandbox":
        self._enter_guards()
        try:
            self.ids = build_sandbox(self.root)
            for key, value in self.ids["paths"]["env"].items():
                self._saved_env[key] = os.environ.get(key)
                os.environ[key] = str(value)
            self.cfg = config_mod.load()
            self._entered = True
            db = Path(self.cfg.kanban_db(BOARD))
            if not _is_under(db, self.root):
                raise RuntimeError(f"cfg.kanban_db вне песочницы: {db}")
            if not self.cfg.sandbox:
                raise RuntimeError("cfg.sandbox=False: TEAMROOM_HERMES_HOME не применился")
            return self
        except Exception:
            self._exit_guards()
            self._restore_env()
            self._rmtree()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._exit_guards()
        self._restore_env()
        self._rmtree()
        return False

    def _restore_env(self) -> None:
        for key, old in self._saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._saved_env.clear()

    def _rmtree(self) -> None:
        if not self.root.exists():
            return

        def _onerror(func, path, _exc):
            try:  # read-only файлы/каталоги Windows
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass

        for _attempt in range(3):
            shutil.rmtree(self.root, onerror=_onerror)
            if not self.root.exists():
                return
            time.sleep(0.1)  # дать отпустить WAL/SHM после закрытия соединений
        shutil.rmtree(self.root, ignore_errors=True)

    # --- доступ к песочнице -------------------------------------------------
    @property
    def paths(self) -> dict:
        return self.ids.get("paths", {})

    @property
    def db(self) -> Path:
        """БД песочной доски `team`."""
        if self.cfg is not None:
            return Path(self.cfg.kanban_db(BOARD))
        return Path(self.ids["paths"]["db"])

    @property
    def data_dir(self) -> Path:
        return Path(self.ids["paths"]["data"])

    @property
    def vault_dir(self) -> Path:
        return Path(self.ids["paths"]["vault"])

    @property
    def kanban_home(self) -> Path:
        return Path(self.ids["paths"]["kanban_home"])

    def state(self, name: str) -> dict:
        """Описание одного дерева: `queued`, `working`, `done_modern`, …"""
        return self.ids[name]

    def connect(self) -> sqlite3.Connection:
        """Соединение с песочной БД (read-only) — только для проверок в тестах."""
        return sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True, timeout=10)

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        con = self.connect()
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def assert_writable_db_guard(self) -> None:
        """Проверка защиты: запись в реальную БД и за пределы песочницы запрещена."""
        _assert_writable_db(self.db, self.root)
        outside = self.root.parent / ".outside-sandbox" / "kanban.db"
        for bad in (real_db_path(self.cfg), outside):
            try:
                _assert_writable_db(bad, self.root)
            except AssertionError:
                continue
            raise AssertionError(f"защита не сработала для {bad}")

    # --- защита реальной доски ---------------------------------------------
    def snapshot_real(self, cfg: Any = None) -> dict:
        """Слепок реальной kanban.db: размер, mtime, число задач, max(*_at)."""
        return snapshot_real_db(cfg or self.cfg)

    def assert_real_untouched(self, before: dict) -> dict:
        """Упасть, если реальная доска изменилась после `before`."""
        return assert_real_untouched(before, self.cfg)
