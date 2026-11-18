# -*- coding: utf-8 -*-
"""Сторож зависаний на песочнице: подставная карточка останавливается ровно на 20 мин.

Здесь проверяется ПРОХОД (`watchdog.tick`) целиком, но без живой доски и без
реальных профилей: доска — обычный sqlite в песочнице, `state.db` — поддельный
(как в тестах `worklog`), остановка — подставная функция (в бою это штатный
`kanban_atomic.stop` из W23). Время подменяется: ни одной реальной минуты не ждём.

Живая проверка «настоящая карточка не трогается» — в отчёте
`docs/status/card-budgets-watchdog.md` (на копии данных, только чтение).

Запуск:
    python -m unittest tests.test_watchdog_tick -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import watchdog  # noqa: E402

NOW = 1_800_000_000


class FakeCfg:
    def __init__(self, base: Path):
        self.data_dir = base / "data"
        self.board = "team"
        self.mock = False
        self.demo = False
        self.profiles = base / "profiles"
        #: поддельный HERMES_HOME: реальный `%LOCALAPPDATA%/hermes` в тестах не читаем
        self.hermes_home = base / "hermes"

    def kanban_db(self, board=""):
        return self.data_dir / "kanban" / ("%s.db" % (board or "team"))




def make_board(cfg, cards):
    """Минимальная доска: tasks, task_links, task_runs (как читает `agent_models`)."""
    path = cfg.kanban_db("team")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, "
                "assignee TEXT, tenant TEXT, created_at INTEGER, started_at INTEGER, "
                "finished_at INTEGER, model_override TEXT, provider_override TEXT, "
                "reasoning_effort TEXT)")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, "
                "status TEXT, started_at INTEGER, ended_at INTEGER, metadata TEXT)")
    for card in cards:
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (card["id"], card.get("title", ""), card["status"], card.get("assignee", ""),
                     card.get("tenant", "room:studio"), card.get("created_at", NOW - 3600),
                     card.get("started_at", NOW - 3600), card.get("finished_at"), None, None, None))
        con.execute("INSERT INTO task_links VALUES (?,?)", ("t_root", card["id"]))
        con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, metadata) "
                    "VALUES (?,?,?,?,?,?)",
                    (card["id"], card.get("assignee", ""), "running", NOW - 3600, None,
                     '{"session_id": "%s"}' % card.get("sid", "")))
    con.commit()
    con.close()


def make_session(cfg, profile, sid, rows):
    """Поддельный `state.db` профиля: сообщения сеанса карточки (только чтение)."""
    path = (cfg.hermes_home / "state.db" if profile in ("", "default")
            else cfg.hermes_home / "profiles" / profile / "state.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                "content TEXT, tool_calls TEXT, tool_name TEXT, timestamp INTEGER, "
                "active INTEGER, compacted INTEGER)")
    for role, content, calls, name, at in rows:
        con.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                    "timestamp, active, compacted) VALUES (?,?,?,?,?,?,1,0)",
                    (sid, role, content, calls, name, at))
    con.commit()
    con.close()


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-wd-tick-")
        self.base = Path(self.tmp.name)
        self.cfg = FakeCfg(self.base)
        self.stopped: list = []
        self.notes: list = []
        self._patches = []
        self._patch()

    def tearDown(self):
        for func, attr, original in self._patches:
            setattr(func, attr, original)
        self.tmp.cleanup()

    def _patch(self):
        """Подменить движок и запись в ветку: реальные профили и доска не трогаются."""
        atomic_stop = lambda cfg, board, card_id, **kw: (self.stopped.append((card_id, kw)),
                                                         {"ok": True})[1]
        note = lambda cfg, room, root_id, text: self.notes.append((root_id, text))
        for module, attr, value in ((watchdog, "_mods", None),):
            pass
        # `_mods` отдаёт (models, atomic, wl): atomic.stop и wl.activity подменяем точечно
        models, atomic, wl = watchdog._mods()        # noqa: SLF001 - проверяем сам проход
        for module, attr, value in ((atomic, "stop", atomic_stop), (watchdog, "_note", note)):
            self._patches.append((module, attr, getattr(module, attr)))
            setattr(module, attr, value)
        self.addCleanup(lambda: None)

    def test_hung_card_is_stopped_after_twenty_minutes(self):
        make_board(self.cfg, [{"id": "t_card", "title": "Сверка сроков", "status": "running",
                               "assignee": "designer", "sid": "s1"}])
        make_session(self.cfg, "designer", "s1", [("assistant", "", "[]", "", NOW - 21 * 60)])
        stats = watchdog.tick(self.cfg, {"id": "studio", "board": "team", "tenant": "room:studio",
                                         "orchestrator": "lead", "title": "Сайт студии"},
                              now=NOW)
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(len(stats["stopped"]), 1, "21 минута — остановка")
        self.assertEqual(self.stopped[0][0], "t_card")
        self.assertTrue(any("Остановил" in text for _, text in self.notes))
        self.assertEqual(watchdog.stalled(self.cfg, {"id": "studio"}, now=NOW)[0]["state"], "hung")

    def test_card_with_fresh_activity_is_not_stopped(self):
        make_board(self.cfg, [{"id": "t_card", "title": "Сверка сроков", "status": "running",
                               "assignee": "designer", "sid": "s2"}])
        make_session(self.cfg, "designer", "s2", [("tool", "готово", "[]", "terminal", NOW - 60)])
        stats = watchdog.tick(self.cfg, {"id": "studio", "board": "team", "tenant": "room:studio"},
                              now=NOW)
        self.assertEqual(stats["stopped"], [])
        self.assertEqual(stats["warned"], [])

    def test_eleven_minutes_only_warns(self):
        make_board(self.cfg, [{"id": "t_card", "title": "Сверка сроков", "status": "running",
                               "assignee": "designer", "sid": "s3"}])
        make_session(self.cfg, "designer", "s3", [("assistant", "", "[]", "", NOW - 11 * 60)])
        stats = watchdog.tick(self.cfg, {"id": "studio", "board": "team", "tenant": "room:studio",
                                         "orchestrator": "lead"}, now=NOW)
        self.assertEqual(len(stats["warned"]), 1)
        self.assertEqual(stats["stopped"], [], "на 10 минутах ничего не останавливаем")
        self.assertIn("не отвечает 11 мин", stats["warned"][0]["text"])

    def test_live_tool_call_protects_the_card(self):
        """E5: в сессии висит вызов инструмента у живого воркера — не зависание."""
        make_board(self.cfg, [{"id": "t_card", "title": "Долгая сборка", "status": "running",
                               "assignee": "designer", "sid": "s4"}])
        make_session(self.cfg, "designer", "s4",
                     [("assistant", "", '[{"function": {"name": "terminal", "arguments": "{\\"command\\": \\"сборка\\"}"}}]', "", NOW - 25 * 60)])
        stats = watchdog.tick(self.cfg, {"id": "studio", "board": "team", "tenant": "room:studio"},
                              now=NOW)
        self.assertEqual(stats["alive_tools"], 1)
        self.assertEqual(stats["stopped"], [], "живой инструмент не считается зависанием")
        self.assertEqual(stats["warned"], [])

    def test_second_pass_does_not_stop_twice(self):
        make_board(self.cfg, [{"id": "t_card", "title": "Сверка сроков", "status": "running",
                               "assignee": "designer", "sid": "s5"}])
        make_session(self.cfg, "designer", "s5", [("assistant", "", "[]", "", NOW - 30 * 60)])
        room = {"id": "studio", "board": "team", "tenant": "room:studio", "orchestrator": "lead"}
        watchdog.acquire(self.cfg, "studio", "t_card", now=NOW)      # действие уже идёт
        stats = watchdog.tick(self.cfg, room, now=NOW)
        self.assertEqual(stats["stopped"], [], "замок не пускает второй проход")
        self.assertEqual(len(stats["warned"]), 1)

    def test_hard_stop_off_never_calls_engine(self):
        make_board(self.cfg, [{"id": "t_card", "title": "Сверка сроков", "status": "running",
                               "assignee": "designer", "sid": "s6"}])
        make_session(self.cfg, "designer", "s6", [("assistant", "", "[]", "", NOW - 40 * 60)])
        room = {"id": "studio", "board": "team", "tenant": "room:studio",
                "card_watchdog": {"hard_stop": False}}
        stats = watchdog.tick(self.cfg, room, now=NOW)
        self.assertEqual(stats["stopped"], [])
        self.assertEqual(len(stats["warned"]), 1)
        self.assertEqual(self.stopped, [])

    def test_demo_and_foreign_tenant_are_skipped(self):
        self.cfg.demo = True
        stats = watchdog.tick(self.cfg, {"id": "studio", "board": "team"}, now=NOW)
        self.assertEqual(stats["skipped_reason"], "demo")
        self.cfg.demo = False
        stats = watchdog.tick(self.cfg, {"id": "studio", "board": "team", "tenant": "other:x"},
                              now=NOW)
        self.assertEqual(stats["skipped_reason"], "not_room_tenant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
