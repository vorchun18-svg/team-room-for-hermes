# -*- coding: utf-8 -*-
"""Тесты записи в канбан (server/engine_write.py) — только в песочнице.

Каждый тест работает с временной БД `tests.helpers.Sandbox` (вне реального
Hermes) и в конце класс доказывает `assert_real_untouched`, что живая доска
`team` не изменилась. Реальные карточки не создаются.

Запуск из корня проекта:
    python -m unittest tests.test_engine_write -v
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import contract, engine_write as ew  # noqa: E402
from tests.helpers import Sandbox              # noqa: E402

REQUEST = ("Проверь, пожалуйста, «Алвара Sanitário» — что делать и в каком порядке?\r\n"
           "Второй абзац: кавычки \"двойные\", тире — и дефис - внутри.\r\n"
           "Третья строка без завершающего перевода.")

ROOM = {
    "id": "sbx",
    "title": "Песочная комната",
    "board": "team",
    "tenant": "room:sbx",
    "orchestrator": "biz-director",
    "critic": "biz-critic",
    "members": [{"profile": "biz-lawyer", "role": "worker", "note": "юрист"}],
    "context_files": [],
    "critic_pass": True,
}


class EngineWriteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls._stack.close()

    # --- помощники -----------------------------------------------------------
    def _row(self, task_id: str) -> sqlite3.Row | None:
        return self.sb.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def _body(self, task_id: str) -> str:
        row = self._row(task_id)
        return (row["body"] or "") if row else ""

    def _create(self, slug: str, text: str = REQUEST, title: str = "Тест записи в канбан") -> dict:
        body = contract.build_request_body(ROOM, text, slug, self.cfg)
        return ew.create_request(self.cfg, ROOM, slug, title, body)

    # --- профили и доска -----------------------------------------------------
    def test_01_list_profiles_from_files(self):
        profiles = ew.list_profiles(self.cfg)
        self.assertTrue(profiles, "ростер профилей пуст")
        self.assertEqual(set(profiles[0]), {"name", "description", "model", "provider", "is_default"})
        self.assertEqual(profiles[0]["name"], "default")
        self.assertTrue(profiles[0]["is_default"])
        by_name = {p["name"]: p for p in profiles}
        self.assertIn("biz-lawyer", by_name)
        self.assertEqual(by_name["biz-lawyer"]["provider"], "ollama-cloud")
        self.assertTrue(by_name["biz-lawyer"]["model"])
        self.assertIn("юрист", by_name["biz-lawyer"]["description"])
        self.assertFalse(any(p["is_default"] for p in profiles[1:]))

    def test_02_ensure_board(self):
        self.assertTrue(ew.ensure_board(self.cfg, "team"))
        self.assertFalse(ew.ensure_board(self.cfg, "no-such-board"))
        self.assertFalse(ew.ensure_board(self.cfg, ""))

    # --- создание запроса ----------------------------------------------------
    def test_03_create_request_writes_card(self):
        res = self._create("wb-20261028-0001")
        root_id = res["root_id"]
        self.assertRegex(root_id, r"^t_[0-9a-f]{8}$")
        self.assertIn(res["warning"], (None, {"code": "dispatcher_down",
                                              "message": "Запрос создан, но диспетчер не запущен — работа не начнётся сама."}))
        row = self._row(root_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "triage")
        self.assertEqual(row["tenant"], "room:sbx")
        self.assertEqual(row["created_by"], "Комната команды")
        self.assertEqual(row["idempotency_key"], "wb-20261028-0001")
        body = row["body"] or ""
        # канбан читает --body-file с универсальными переводами строк: CRLF → LF,
        # поэтому сравниваем с нормализованным телом (кавычки и текст — дословно).
        expected = contract.build_request_body(ROOM, REQUEST, "wb-20261028-0001", self.cfg)
        # WAVE 8, Блок D: create_request дописывает в тело блок «КОНТЕКСТ КОМНАТЫ»
        expected = ew.with_context_pack(self.cfg, ROOM, expected)
        self.assertEqual(body, expected.replace("\r\n", "\n"))
        self.assertIn("КОНТЕКСТ КОМНАТЫ", body)
        self.assertTrue(body.startswith(REQUEST.replace("\r\n", "\n")))
        self.assertIn("«Алвара Sanitário»", body)
        self.assertNotIn("\r", body)
        self.assertNotIn("\ufeff", body)
        self.assertIn("Команда комнаты (назначай подзадачи только этим профилям):", body)
        self.assertIn(contract.CONTRACT_CLOSE, body)
        self.assertIn(str(Path(self.cfg.reports_dir) / "sbx" / "wb-20261028-0001"), body)

    def test_04_create_request_idempotent_by_slug(self):
        first = self._create("wb-20261028-0002")
        second = self._create("wb-20261028-0002")   # тот же slug — та же карточка
        self.assertEqual(first["root_id"], second["root_id"])
        count = self.sb.one("SELECT count(*) AS c FROM tasks WHERE idempotency_key=?",
                            ("wb-20261028-0002",))["c"]
        self.assertEqual(count, 1)

    # --- действия ------------------------------------------------------------
    def test_05_act_comment(self):
        root = self._create("wb-20261028-0003")["root_id"]
        text = "Комментарий Михаила:\r\n- пункт один «кавычки»\r\n- пункт два"
        res = ew.act(self.cfg, "team", root, "comment", text=text, author="mikhail")
        self.assertTrue(res["ok"])
        self.assertIsInstance(res["cmd_ms"], int)
        self.assertIn("Комментарий", res["detail"])
        comment = self.sb.one("SELECT author, body FROM task_comments WHERE task_id=? "
                              "ORDER BY id DESC", (root,))
        self.assertEqual(comment["author"], "mikhail")
        self.assertEqual(comment["body"], text)

    def test_06_act_answer_by_question_unblocks_children(self):
        tree = self.sb.state("needs_you")
        blocked = tree["extra_cards"]["blocked_child"]
        self.assertEqual(self._row(blocked)["status"], "blocked")
        text = "Берём WhatsApp — канал V1"
        # как из UI: id вопроса q_1, а не id карточки
        res = ew.act(self.cfg, "team", tree["root_id"], "answer", question_id="q_1", text=text)
        self.assertTrue(res["ok"])
        self.assertEqual(res["unblocked"], [blocked])
        self.assertEqual(self._row(blocked)["status"], "ready")
        comment = self.sb.one("SELECT author, body FROM task_comments WHERE task_id=? "
                              "AND author='mikhail' ORDER BY id DESC", (tree["root_id"],))
        self.assertEqual(comment["body"], text)

    def test_07_act_unblock(self):
        batch = self.sb.state("failed")["extra_cards"]["failed_child"]
        self.assertEqual(self._row(batch)["status"], "blocked")
        res = ew.act(self.cfg, "team", self.sb.state("failed")["root_id"], "unblock",
                     task_id=batch, text="разблокировать вручную")
        self.assertTrue(res["ok"])
        self.assertEqual(res["unblocked"], [batch])
        self.assertEqual(self._row(batch)["status"], "ready")

    def test_08_act_reassign(self):
        card = self.sb.state("working")["child_ids"][-1]   # срез strategy, ready
        before = self._row(card)["assignee"]
        self.assertNotEqual(before, "biz-lawyer")
        res = ew.act(self.cfg, "team", self.sb.state("working")["root_id"], "reassign",
                     task_id=card, profile="biz-lawyer")
        self.assertTrue(res["ok"])
        self.assertEqual(self._row(card)["assignee"], "biz-lawyer")
        self.assertEqual(res["profile"], "biz-lawyer")

    def test_09_act_archive_root_and_children(self):
        tree = self.sb.state("candidate")
        res = ew.act(self.cfg, "team", tree["root_id"], "archive")
        self.assertTrue(res["ok"])
        self.assertEqual(set(res["archived"]), {tree["root_id"], *tree["child_ids"]})
        for task_id in res["archived"]:
            self.assertEqual(self._row(task_id)["status"], "archived")

    def test_10_errors_are_russian_engine_errors(self):
        with self.assertRaises(ew.EngineWriteError) as ctx:
            ew.act(self.cfg, "team", "t_deadbeef", "comment", text="привет")
        err = ctx.exception
        self.assertEqual(err.code, "not_found")
        self.assertIn("не найдена", err.message_ru)
        self.assertTrue(err.stderr_tail)
        self.assertEqual(err.to_dict()["error"]["code"], "not_found")

        with self.assertRaises(ew.EngineWriteError) as ctx2:
            ew.act(self.cfg, "team", "t_deadbeef", "no_such_action")
        self.assertEqual(ctx2.exception.code, "bad_request")

        with self.assertRaises(ew.EngineWriteError) as ctx3:
            ew.act(self.cfg, "team", "t_deadbeef", "comment", text="   ")
        self.assertEqual(ctx3.exception.code, "bad_request")

    def test_11_create_child_card_links_parent_and_tenant(self):
        parent = self.sb.state("queued")["root_id"]
        card_id = ew.create_child_card(self.cfg, ROOM, "team", parent, "biz-critic",
                                       "Проба: карточка-ребёнок", "Тело дочерней карточки «тест»",
                                       "critic", idem_key="wb-child-0001")
        self.assertRegex(card_id, r"^t_[0-9a-f]{8}$")
        row = self._row(card_id)
        self.assertEqual(row["assignee"], "biz-critic")
        self.assertEqual(row["tenant"], "room:sbx")
        self.assertIn("Тело дочерней карточки «тест»", row["body"])
        link = self.sb.one("SELECT parent_id FROM task_links WHERE child_id=?", (card_id,))
        self.assertIsNotNone(link)
        self.assertEqual(link["parent_id"], parent)
        # повторный вызов с тем же ключом не создаёт дубль
        again = ew.create_child_card(self.cfg, ROOM, "team", parent, "biz-critic",
                                     "Проба: карточка-ребёнок", "другое тело", "critic",
                                     idem_key="wb-child-0001")
        self.assertEqual(again, card_id)

    def test_12_retarget_root(self):
        root = self.sb.state("working")["root_id"]
        self.assertEqual(self._row(root)["assignee"], "biz-director")
        res = ew.retarget_root(self.cfg, "team", root, "default")
        self.assertTrue(res["ok"])
        self.assertTrue(res["changed"])
        self.assertEqual(self._row(root)["assignee"], "default")
        # завершённый корень не трогаем
        done_root = self.sb.state("done_modern")["root_id"]
        before = self._row(done_root)["assignee"]
        res2 = ew.retarget_root(self.cfg, "team", done_root, "default")
        self.assertFalse(res2["changed"])
        self.assertEqual(self._row(done_root)["assignee"], before)

    # --- лог -----------------------------------------------------------------
    def test_13_engine_log_without_bodies(self):
        log_path = Path(self.cfg.log_dir) / "engine.log"
        self.assertTrue(log_path.exists(), "нет data/logs/engine.log")
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("code=0", text)
        self.assertIn("ms |", text)
        self.assertIn("--board team", text)
        self.assertIn("comment", text)
        # тела не логируются: ни текст комментария, ни тело запроса
        self.assertNotIn("пункт один", text)
        self.assertNotIn("Алвара Sanitário", text)
        self.assertIn("<текст", text)   # скрытая длина вместо тела

    def test_14_warning_when_dispatcher_down(self):
        from unittest import mock
        with mock.patch.object(ew, "dispatcher_ok", return_value=False):
            res = self._create("wb-20261028-0004")
        self.assertEqual(res["warning"]["code"], "dispatcher_down")
        self.assertIn("диспетчер не запущен", res["warning"]["message"])
        with mock.patch.object(ew, "dispatcher_ok", return_value=True):
            res2 = self._create("wb-20261028-0005")
        self.assertIsNone(res2["warning"])

    def test_15_answer_on_unblocked_card_keeps_comment(self):
        root = self._create("wb-20261028-0006")["root_id"]
        res = ew.act(self.cfg, "team", root, "answer", task_id=root, text="Ответ без блокировки")
        self.assertTrue(res["ok"])
        self.assertEqual(res["unblocked"], [])
        self.assertIn("не была заблокирована", res["warning"])
        comment = self.sb.one("SELECT body FROM task_comments WHERE task_id=? AND author='mikhail'",
                              (root,))
        self.assertEqual(comment["body"], "Ответ без блокировки")
        self.assertEqual(self._row(root)["status"], "triage")

    def test_16_rework_returns_blocked_card_to_work(self):
        root = self.sb.state("failed")["root_id"]
        self.assertEqual(self._row(root)["status"], "blocked")
        res = ew.act(self.cfg, "team", root, "rework", text="Перезапусти после правок")
        self.assertTrue(res["ok"])
        self.assertIn("На доработку", res["detail"])
        row = self._row(root)
        self.assertIn(row["status"], ("ready", "todo"))
        self.assertEqual(row["assignee"], "biz-director")

    def test_17_retry_ready_card_is_noop(self):
        card = self.sb.state("working")["child_ids"][-1]
        self.assertEqual(self._row(card)["status"], "ready")
        res = ew.act(self.cfg, "team", card, "retry", task_id=card)
        self.assertTrue(res["ok"])
        self.assertEqual(res["cmd_ms"], 0)
        self.assertIn("в очереди", res["detail"])

    def test_18_no_temp_body_files_left(self):
        tmp_dir = Path(self.cfg.tmp_dir)
        leftovers = list(tmp_dir.glob("*.md")) if tmp_dir.exists() else []
        self.assertEqual(leftovers, [], f"временные файлы тел не удалены: {leftovers}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
