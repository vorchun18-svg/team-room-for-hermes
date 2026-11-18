"""W20-U4 (P9): лёгкий счётчик «новых реакций» для бейджа вкладки.

Проверяем `server/reaction_notice.py` на вымышленной доске: реакция руководителя
на ответ/шаг/решение находится по метке в комментарии или в `metadata.reaction`,
считается по корню запроса и стоит ровно столько, сколько её есть.

Красный на старом коде: функции `room_reactions` и полей `reactions_unseen` /
`last_reaction_at` в сводке комнат не существовало — уведомление о реакции жило
только на странице запроса.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config as config_mod          # noqa: E402
from server import reaction_notice as rn         # noqa: E402

BOARD = "team"

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
    status TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER,
    metadata TEXT);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY (parent_id, child_id));
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT,
    body TEXT, created_at INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
    payload TEXT, created_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT,
    status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT,
    metadata TEXT, error TEXT);
"""

ROOM = {"id": "kafe-leto", "title": "Кафе «Лето»", "orchestrator": "anna", "board": BOARD,
        "critic": "lev"}


class ReactionNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teamroom-react-"))
        self.board_dir = self.tmp / "kanban_home" / "kanban" / "boards" / BOARD
        self.board_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = config_mod.Cfg(hermes_home=self.tmp / "hermes",
                                  kanban_home=self.tmp / "kanban_home",
                                  data_dir=self.tmp / "data", vault_dir=self.tmp / "vault",
                                  sandbox=True)
        self.db = Path(self.cfg.kanban_db(BOARD))
        self._write({"req_alpha": 1000, "req_beta": 2000}, links=[("req_alpha", "ns_alpha1"),
                                                                  ("ns_alpha1", "ns_alpha2"),
                                                                  ("req_beta", "ns_beta1")])
        self.records = [
            {"root_id": "req_alpha", "created_at": 1000,
             "answers": {"q_1": {"text": "Свести заявки", "at": 1100}}},
            {"root_id": "req_beta", "created_at": 2000,
             "answers": {"q_2": {"text": "Проверить сроки", "at": 2100}}},
            # Без ответов человека реагировать не на что — такой запрос не читаем.
            {"root_id": "req_gamma", "created_at": 3000},
        ]

    # --- песочница ---------------------------------------------------------

    def _write(self, roots: dict, links=()) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.executescript(SCHEMA)
            for tid, created in roots.items():
                con.execute("INSERT INTO tasks (id, title, assignee, status, created_at) "
                            "VALUES (?, ?, ?, ?, ?)", (tid, "Запрос «Лето»", "anna", "done", created))
            for parent, child in links:
                con.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                            (parent, child))
                con.execute("INSERT OR IGNORE INTO tasks (id, title, assignee, status, created_at) "
                            "VALUES (?, ?, ?, ?, ?)", (child, "Шаг плана", "marina", "done", 1500))
            con.commit()
        finally:
            con.close()

    def _comment(self, task_id: str, author: str, body: str, at: int) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.execute("INSERT INTO task_comments (task_id, author, body, created_at) "
                        "VALUES (?, ?, ?, ?)", (task_id, author, body, at))
            con.commit()
        finally:
            con.close()

    def _meta(self, task_id: str, payload: dict, at: int) -> None:
        con = sqlite3.connect(str(self.db))
        try:
            con.execute("UPDATE tasks SET metadata = ?, completed_at = ? WHERE id = ?",
                        (json.dumps(payload, ensure_ascii=False), at, task_id))
            con.commit()
        finally:
            con.close()

    # --- счёт --------------------------------------------------------------

    def test_reaction_from_leader_comment_counts_for_root(self):
        self._comment("req_alpha", "anna",
                      "[Руководитель · реакция на ответ q_1] Учтено: изменено 2 карточки", 1200)
        info = rn.room_reactions(self.cfg, BOARD, ROOM, self.records)
        self.assertEqual(info["count"], 1)
        self.assertEqual(info["last_at"], 1200)
        self.assertEqual(list(info["requests"].keys()), ["req_alpha"])
        self.assertEqual(info["requests"]["req_alpha"], {"count": 1, "last_at": 1200})
        self.assertEqual(info["items"][0]["kind"], "answer")
        self.assertIn("изменено 2 карточки", info["items"][0]["text"])

    def test_reaction_on_child_and_grandchild_rolls_up_to_request(self):
        self._comment("ns_alpha1", "anna", "[Руководитель · реакция на шаг ns_alpha1] срок изменён", 1300)
        self._comment("ns_alpha2", "anna", "[Руководитель · реакция на шаг ns_alpha2] принято", 1400)
        info = rn.room_reactions(self.cfg, BOARD, ROOM, self.records)
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["requests"]["req_alpha"]["count"], 2)
        self.assertEqual(info["requests"]["req_alpha"]["last_at"], 1400)
        self.assertEqual({i["kind"] for i in info["items"]}, {"step"})

    def test_comments_from_workers_are_not_reactions(self):
        self._comment("req_alpha", "marina", "[Руководитель · реакция на ответ q_1] Учтено", 1200)
        self._comment("req_alpha", "anna", "Просто заметка без метки реакции", 1300)
        info = rn.room_reactions(self.cfg, BOARD, ROOM, self.records)
        self.assertEqual(info["count"], 0)
        self.assertIsNone(info["last_at"])

    def test_requests_without_answers_are_skipped(self):
        self._comment("req_gamma", "anna", "[Руководитель · реакция на ответ q_9] Учтено", 3100)
        info = rn.room_reactions(self.cfg, BOARD, ROOM, self.records)
        self.assertEqual(info["count"], 0, "по запросу без ответов реакций не ищем")

    def test_metadata_reaction_is_counted(self):
        self._meta("ns_beta1", {"reaction": {"question_id": "q_2", "outcome": "applied",
                                             "cards": ["t_a", "t_b"]}}, 2200)
        info = rn.room_reactions(self.cfg, BOARD, ROOM, self.records)
        self.assertEqual(info["count"], 1)
        self.assertEqual(info["requests"]["req_beta"]["last_at"], 2200)
        self.assertEqual(info["items"][0].get("question_id"), "q_2")

    def test_two_requests_are_counted_separately(self):
        self._comment("req_alpha", "anna", "[Руководитель · реакция на ответ q_1] Учтено", 1200)
        self._comment("req_beta", "anna", "[Руководитель · реакция на ответ q_2] принято", 2200)
        info = rn.room_reactions(self.cfg, BOARD, ROOM, self.records)
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["last_at"], 2200)
        self.assertEqual(sorted(info["requests"].keys()), ["req_alpha", "req_beta"])

    # --- страховки ---------------------------------------------------------

    def test_no_orchestrator_no_count(self):
        info = rn.room_reactions(self.cfg, BOARD, {"id": "kafe-leto"}, self.records)
        self.assertEqual(info["count"], 0)
        self.assertEqual(info["requests"], {})
        self.assertEqual(info["items"], [])

    def test_missing_board_returns_empty(self):
        info = rn.room_reactions(self.cfg, "net-takoy-doski", ROOM, self.records)
        self.assertEqual(info, {"count": 0, "last_at": None, "requests": {}, "items": []})

    def test_no_records_returns_empty(self):
        self.assertEqual(rn.room_reactions(self.cfg, BOARD, ROOM, [])["count"], 0)

    def test_database_is_opened_read_only(self):
        """Наш счёт не должен писать в доску: файл и размер не меняются."""
        before = (self.db.stat().st_size, self.db.stat().st_mtime_ns)
        self._comment("req_alpha", "anna", "[Руководитель · реакция на ответ q_1] Учтено", 1200)
        before = (self.db.stat().st_size, self.db.stat().st_mtime_ns)
        rn.room_reactions(self.cfg, BOARD, ROOM, self.records)
        self.assertEqual((self.db.stat().st_size, self.db.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
