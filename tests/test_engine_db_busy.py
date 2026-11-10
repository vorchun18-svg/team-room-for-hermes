"""D6: блокировка/сбой SQLite — 503 engine_busy/engine_error, а не 500 и не зависание."""
from __future__ import annotations

import dataclasses
import http.client
import json
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import app as app_mod          # noqa: E402
from server import engine_db, engine_read, registry, watcher  # noqa: E402
from tests.helpers import Sandbox          # noqa: E402

MINIMAL_SCHEMA = """
CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER);
CREATE TABLE task_runs(task_id TEXT, status TEXT, outcome TEXT, summary TEXT);
CREATE TABLE task_comments(task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
CREATE TABLE task_events(task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
"""


class FakeCursor:
    def __init__(self, exc):
        self.exc = exc

    def __iter__(self):
        raise self.exc


class FakeCon:
    """Соединение, чей execute падает заданной ошибкой SQLite."""

    def __init__(self, exc):
        self.exc = exc

    def execute(self, *_args, **_kwargs):
        raise self.exc


class TestColsErrors(unittest.TestCase):
    def test_locked_is_db_busy_not_schema_drift(self):
        """Раньше `_cols` глотал ошибку → `_require_tables` рапортовал schema_drift."""
        con = FakeCon(sqlite3.OperationalError("database is locked"))
        with self.assertRaises(engine_db.EngineError) as ctx:
            engine_db._cols(con, "tasks")
        self.assertEqual(ctx.exception.code, "db_busy")
        self.assertIn("занята", ctx.exception.message)

    def test_other_sqlite_error_is_db_error(self):
        con = FakeCon(sqlite3.DatabaseError("file is encrypted or is not a database"))
        with self.assertRaises(engine_db.EngineError) as ctx:
            engine_db._cols(con, "tasks")
        self.assertEqual(ctx.exception.code, "db_error")

    def test_require_tables_keeps_schema_drift_for_real_absence(self):
        con = FakeCon(sqlite3.OperationalError("no such table"))  # не «locked»
        with self.assertRaises(engine_db.EngineError) as ctx:
            engine_db._cols(con, "tasks")
        self.assertEqual(ctx.exception.code, "db_error")


class TestBusyTimeout(unittest.TestCase):
    def test_busy_timeout_is_capped(self):
        self.assertLessEqual(engine_db.BUSY_TIMEOUT_MS, 3000)

    def test_deadline_exhaustion_is_busy(self):
        with engine_db.deadline(0):
            self.assertEqual(engine_db.remaining_ms(), 0)
            with Sandbox() as sb:
                with self.assertRaises(engine_db.EngineError) as ctx:
                    with engine_db._conn(sb.cfg, "team"):
                        pass
        self.assertEqual(ctx.exception.code, "db_busy")

    def test_connection_sets_busy_timeout(self):
        with Sandbox() as sb:
            with engine_db._conn(sb.cfg, "team") as con:
                value = con.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertGreater(value, 0)
        self.assertLessEqual(value, engine_db.BUSY_TIMEOUT_MS)


class TestLockedBoard(unittest.TestCase):
    """Реальная эксклюзивная блокировка БД: health ≤ бюджета, API — 503."""

    def setUp(self):
        self._sandbox = Sandbox()
        self.sb = self._sandbox.__enter__()
        self.home = Path(self.sb.root) / "locked_home"
        self.db = self.home / "kanban" / "boards" / "team" / "kanban.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db))
        con.executescript(MINIMAL_SCHEMA)
        con.commit()
        con.close()
        self.cfg = dataclasses.replace(self.sb.cfg, kanban_home=self.home)
        registry.add_request(self.cfg, "cafe", {"root_id": "t_00000001", "title": "Запрос",
                                               "text": "x", "created_at": int(time.time())})
        self.locker = sqlite3.connect(str(self.db), timeout=1, isolation_level=None)
        self.locker.execute("PRAGMA journal_mode=DELETE")
        self.locker.execute("BEGIN EXCLUSIVE")     # держим эксклюзивную блокировку
        self.locker.execute("INSERT INTO tasks(id, title, status) VALUES ('t_00000001', 'x', 'todo')")

    def tearDown(self):
        try:
            self.locker.execute("ROLLBACK")
        finally:
            self.locker.close()
        self._sandbox.__exit__(None, None, None)

    def test_engine_read_reports_db_busy(self):
        with engine_db.deadline(1.0):
            with self.assertRaises(engine_db.EngineError) as ctx:
                engine_read.request_summary(self.cfg, "team", "t_00000001")
        self.assertEqual(ctx.exception.code, "db_busy")

    def test_health_is_fast_under_lock(self):
        started = time.monotonic()
        server = app_mod.create_app(self.cfg, start_watcher=False, port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status, 200)
        self.assertLess(elapsed, 8.0, "health ждал блокировку БД %.1f с" % elapsed)
        self.assertFalse(payload["engine"]["ok"])
        self.assertTrue(payload["engine"]["warning"])

    def test_requests_endpoint_returns_503_engine_busy(self):
        server = app_mod.create_app(self.cfg, start_watcher=False, port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("GET", "/api/rooms/cafe/requests")
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        elapsed = time.monotonic() - started
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(resp.status, 503, body)
        self.assertEqual(body["error"]["code"], "engine_busy")
        self.assertIn("занята", body["error"]["message"])
        self.assertLess(elapsed, 10.0, "запрос висел %.1f с" % elapsed)


class TestWatcherTickBudget(unittest.TestCase):
    def test_tick_stops_at_budget(self):
        """D6: тик watcher не может висеть дольше TICK_BUDGET."""
        with Sandbox() as sb:
            from server import rooms
            rooms.seed_defaults(sb.cfg)
            registry.add_request(sb.cfg, "cafe", {"root_id": "t_aaaa0001", "text": "x",
                                                 "created_at": int(time.time())})
            app = app_mod.App(sb.cfg, port=0)
            app.watcher = watcher.Watcher(app)
            with mock.patch.object(watcher, "TICK_BUDGET", 0.0):
                stats = app.watcher.tick_once()
            self.assertTrue(stats.get("truncated"))
            self.assertGreaterEqual(stats["errors"], 1)
            self.assertIn("бюджет", app.watcher.last_error or "")


if __name__ == "__main__":
    unittest.main()
