# -*- coding: utf-8 -*-
"""Стадии задачи и доска комнаты (W30-D1, контракт §3).

Проверяем поведение, а не текст:
* стадия считается из статуса доски, принятия и БЛОКИРУЮЩИХ пунктов;
* строка доски содержит ровно поля контракта, а счётчики колонок равны числу
  карточек в них (правило «число = число пунктов»);
* связи «задача → подзадачи» читаются из ``projects.json`` (файл пишет пакет B);
* ручка ``GET /api/rooms/{room}/tasks`` отвечает по контракту на живой песочной
  доске — стадии, колонки, счётчики, подписи.

    python -m unittest tests.test_tasks_board -v
"""
from __future__ import annotations

import contextlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import tasks_board as board_mod   # noqa: E402
from tests.helpers import Sandbox             # noqa: E402

ROOM_ID = "cafe"
ROOM = {"id": ROOM_ID, "board": "team", "orchestrator": "team-director"}
#: Поля строки доски — буква в букву контракт §3.
ROW_FIELDS = {"id", "title", "stage", "status", "assignee", "steps_done", "steps_total",
              "needs_you", "accepted", "parent_id", "children_count", "updated_at",
              "summary_short"}


class StageTest(unittest.TestCase):
    """Стадия — из статуса, принятия и блокирующих пунктов."""

    def test_status_mapping(self):
        self.assertEqual(board_mod.stage_of("queued"), "new")
        self.assertEqual(board_mod.stage_of("triage"), "new")
        self.assertEqual(board_mod.stage_of("running"), "working")
        self.assertEqual(board_mod.stage_of("reviewing"), "review")
        self.assertEqual(board_mod.stage_of("synthesizing"), "review")
        self.assertEqual(board_mod.stage_of("needs_you"), "needs_you")
        self.assertEqual(board_mod.stage_of("done"), "done_system")
        self.assertEqual(board_mod.stage_of("archived"), "archived")
        # упавшая задача не идёт сама: у неё есть кнопка «Повторить»
        self.assertEqual(board_mod.stage_of("failed"), "needs_you")
        # неизвестный/пустой статус — «Новая», а не падение
        self.assertEqual(board_mod.stage_of(""), "new")
        self.assertEqual(board_mod.stage_of(None), "new")

    def test_accept_and_archive_freeze_win(self):
        self.assertEqual(board_mod.stage_of("done", accepted=True), "accepted")
        self.assertEqual(board_mod.stage_of("running", accepted=True), "accepted")
        self.assertEqual(board_mod.stage_of("done", archived=True), "archived")
        # архив сильнее принятия: карточка правда в архиве
        self.assertEqual(board_mod.stage_of("archived", accepted=True), "archived")

    def test_blocking_question_moves_to_needs_you(self):
        self.assertEqual(board_mod.stage_of("done", blocking=2), "needs_you")
        # неблокирующие вопросы стадию не двигают: это «пакет решений»
        self.assertEqual(board_mod.stage_of("done", blocking=0), "done_system")

    def test_columns_are_fixed(self):
        self.assertEqual(board_mod.COLUMNS,
                         ["new", "working", "needs_you", "review", "done_system", "accepted"])
        self.assertEqual(set(board_mod.STAGE_RU), set(board_mod.COLUMNS) | {"archived"})


class RowTest(unittest.TestCase):
    """Строка и счётчики: поля контракта, число = число карточек."""

    def _rec(self, root_id, title="Химчистка: приём белья"):
        return {"root_id": root_id, "title": title, "created_at": 1793000000}

    def test_row_has_contract_fields(self):
        summary = {"id": "t_0000aaaa", "title": "Химчистка: приём белья",
                   "title_short": "Приём белья", "status": "done",
                   "progress": {"done": 2, "total": 3, "percent": 66},
                   "assignee": "team-ops", "status_reason": "Ответ ждёт проверки",
                   "finished_at": 1793000500}
        row = board_mod.row(self._rec("t_0000aaaa"), summary,
                            status="done", waiting=[{"kind": "question", "blocking": False}])
        self.assertEqual(set(row), ROW_FIELDS)
        self.assertEqual(row["stage"], "done_system")
        self.assertEqual((row["steps_done"], row["steps_total"]), (2, 3))
        self.assertEqual(row["needs_you"], {"count": 1, "blocking": 0})
        self.assertEqual(row["updated_at"], 1793000500)
        self.assertTrue(row["summary_short"])

    def test_parent_and_children_from_links(self):
        summaries = {"t_0000aaaa": {"title": "Химчистка: приём белья", "status": "done"},
                     "t_0000bbbb": {"title": "Грузоперевозки: слоты", "status": "working"}}
        rows = board_mod.build_rows(
            [self._rec("t_0000aaaa"), self._rec("t_0000bbbb", "Грузоперевозки: слоты")],
            states={"t_0000aaaa": {"status": "done"}, "t_0000bbbb": {"status": "todo"}},
            links_map={"t_0000aaaa": {"parent": None, "children": ["t_0000bbbb"]},
                       "t_0000bbbb": {"parent": "t_0000aaaa", "children": []}},
            summary_of=lambda rec: summaries.get(rec["root_id"]) or {})
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["t_0000aaaa"]["children_count"], 1)
        self.assertIsNone(by_id["t_0000aaaa"]["parent_id"])
        self.assertEqual(by_id["t_0000bbbb"]["parent_id"], "t_0000aaaa")
        # статус ЗАПРОСА важнее статуса корневой карточки: корень `todo`, работа идёт
        self.assertEqual(by_id["t_0000bbbb"]["stage"], "working")

    def test_counts_equal_cards_in_columns(self):
        summaries = {"t_0000aaaa": {"title": "Химчистка", "status": "done"},
                     "t_0000bbbb": {"title": "Грузоперевозки", "status": "working"},
                     "t_0000cccc": {"title": "Кофейня", "status": "done"}}
        rows = board_mod.build_rows(
            [self._rec("t_0000aaaa"), self._rec("t_0000bbbb"), self._rec("t_0000cccc")],
            states={"t_0000aaaa": {"status": "done"},
                    "t_0000bbbb": {"status": "running"},
                    "t_0000cccc": {"status": "done", "accepted": True}},
            accepted={"t_0000cccc"},
            summary_of=lambda rec: summaries.get(rec["root_id"]) or {})
        counts = board_mod.counts(rows)
        self.assertEqual(counts["done_system"], 1)
        self.assertEqual(counts["working"], 1)
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(counts["total"], len(rows))
        # счётчик каждой колонки = числу карточек этой стадии
        for stage in board_mod.COLUMNS:
            self.assertEqual(counts[stage],
                             sum(1 for row in rows if row["stage"] == stage), stage)

    def test_accepted_task_reports_no_waiting_items(self):
        """W30-QA1: «Принята» = заморозка — карточка не может «ждать вас»."""
        summary = {"id": "t_0000dddd", "title": "Кофейня: витрина", "status": "done",
                   "progress": {"done": 3, "total": 3}}
        row = board_mod.row(self._rec("t_0000dddd", "Кофейня: витрина"), summary,
                            status="done", accepted=True,
                            waiting=[{"kind": "question", "blocking": False},
                                     {"kind": "question", "blocking": True}])
        self.assertEqual(row["stage"], "accepted")
        self.assertEqual(row["needs_you"], {"count": 0, "blocking": 0},
                         "у принятой задачи «ждёт вас» быть не должно")
        # После возврата в работу (accepted=False) те же пункты снова на месте.
        back = board_mod.row(self._rec("t_0000dddd", "Кофейня: витрина"), summary,
                             status="done",
                             waiting=[{"kind": "question", "blocking": True}])
        self.assertEqual(back["needs_you"], {"count": 1, "blocking": 1})

    def test_payload_shape(self):
        body = board_mod.payload(ROOM_ID, [])
        self.assertEqual(body["columns"], board_mod.COLUMNS)
        self.assertEqual(body["room"], ROOM_ID)
        self.assertEqual(body["counts"]["total"], 0)
        self.assertEqual(len(body["labels"]), 7)


class SandboxBoardTest(unittest.TestCase):
    """Живая песочная доска: ручка отдаёт стадии и счётчики по контракту."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        from server import app as app_mod
        from server import registry
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))
        for name in ("queued", "working", "reviewing", "done_modern"):
            ids = cls.sb.state(name)
            registry.add_request(cls.cfg, ROOM_ID, {
                "root_id": ids["root_id"], "title": "Задача: " + name,
                "text": "нейтральный пример: химчистка", "created_at": 1793000000,
            })

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def test_board_endpoint_returns_stages(self):
        status, payload, _headers = self.app.h_room_tasks({}, {}, ROOM_ID)
        self.assertEqual(status, 200)
        self.assertEqual(payload["columns"], board_mod.COLUMNS)
        self.assertGreaterEqual(payload["counts"]["total"], 4)
        stages = {row["stage"] for row in payload["tasks"]}
        # идущая работа, готовая задача и очередь видны каждая в своей колонке
        self.assertIn("working", stages)
        self.assertIn("done_system", stages)
        self.assertIn("new", stages)
        for row in payload["tasks"]:
            self.assertEqual(set(row), ROW_FIELDS, row)
        self.assertEqual(payload["counts"]["total"], len(payload["tasks"]))
        # счётчик каждой колонки = числу карточек этой стадии
        for stage in board_mod.COLUMNS:
            self.assertEqual(payload["counts"][stage],
                             sum(1 for row in payload["tasks"] if row["stage"] == stage), stage)

    def test_projects_json_links_are_read(self):
        """Связи «родитель → подзадачи» из файла пакета B видны на доске."""
        ids = self.sb.state("done_modern")
        path = Path(self.cfg.state_dir) / ROOM_ID / board_mod.PROJECTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            ids["root_id"]: {"children": ["t_0000dead"], "parent": None},
            "t_0000dead": {"children": [], "parent": ids["root_id"]},
        }, ensure_ascii=False), encoding="utf-8")
        try:
            found = board_mod.links(self.cfg, ROOM_ID)
            self.assertEqual(found[ids["root_id"]]["children"], ["t_0000dead"])
            self.assertEqual(found["t_0000dead"]["parent"], ids["root_id"])
        finally:
            path.unlink()

    def test_unknown_room_is_404(self):
        from server.http_util import ApiError
        with self.assertRaises(ApiError) as ctx:
            self.app.h_room_tasks({}, {}, "no-such-room")
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
