# -*- coding: utf-8 -*-
"""Колонки задачи, метка «Ждёт вас» и кэш доски (W31-A1, контракт W31).

Проверяем поведение, а не текст:

* колонок РОВНО четыре — «Новая», «В работе», «На проверке», «Готово»; старые
  стадии сводятся к ним (``needs_you`` → «В работе» с меткой, ``done_system`` →
  «На проверке», ``accepted`` → «Готово»);
* строка доски содержит ``column``, ``waiting_you``, ``waiting_count``; метка
  «Ждёт вас» стоит только на карточке в работе, у принятой — ноль;
* метка «возвращена в работу» держит карточку в «В работе», пока её не приняли;
* счётчики колонок равны числу карточек в них;
* кэш ответа ручки — по отпечатку доски: второй запрос без изменений отдаёт
  прежний ответ, а изменение доски (статус карточки, комментарий) сбрасывает
  кэш и не отдаёт устаревшее.

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

from server import read_cache as cache_mod       # noqa: E402
from server import tasks_board as board_mod     # noqa: E402
from tests.helpers import Sandbox, SimAgents    # noqa: E402

ROOM_ID = "cafe"
ROOM = {"id": ROOM_ID, "board": "team", "orchestrator": "team-director"}
#: Поля строки доски — контракт W31 (старое `needs_you` оставлено для клиентов).
ROW_FIELDS = {"id", "title", "column", "stage", "status", "assignee", "steps_done",
              "steps_total", "waiting_you", "waiting_count", "waiting_blocking",
              "needs_you", "accepted", "parent_id", "children_count", "updated_at",
              "summary_short"}


class StageTest(unittest.TestCase):
    """Колонка — из статуса, принятия и блокирующих пунктов."""

    def test_status_mapping(self):
        self.assertEqual(board_mod.stage_of("queued"), "new")
        self.assertEqual(board_mod.stage_of("triage"), "new")
        self.assertEqual(board_mod.stage_of("todo"), "new")
        self.assertEqual(board_mod.stage_of("running"), "working")
        self.assertEqual(board_mod.stage_of("needs_you"), "working")
        self.assertEqual(board_mod.stage_of("blocked"), "working")
        self.assertEqual(board_mod.stage_of("failed"), "working")
        self.assertEqual(board_mod.stage_of("reviewing"), "review")
        self.assertEqual(board_mod.stage_of("synthesizing"), "review")
        self.assertEqual(board_mod.stage_of("review"), "review")
        self.assertEqual(board_mod.stage_of("done"), "review")
        self.assertEqual(board_mod.stage_of("archived"), "archived")
        self.assertEqual(board_mod.stage_of(""), "new")
        self.assertEqual(board_mod.stage_of(None), "new")

    def test_accept_and_archive_freeze_win(self):
        self.assertEqual(board_mod.stage_of("done", accepted=True), "done")
        self.assertEqual(board_mod.stage_of("running", accepted=True), "done")
        self.assertEqual(board_mod.stage_of("done", archived=True), "archived")
        self.assertEqual(board_mod.stage_of("archived", accepted=True), "archived")

    def test_blocking_question_keeps_card_in_work(self):
        # «Ждёт вас» — не колонка: карточка остаётся там, где работа
        self.assertEqual(board_mod.stage_of("done", blocking=2), "review")
        self.assertEqual(board_mod.stage_of("queued", blocking=1), "working")
        self.assertEqual(board_mod.stage_of("running", blocking=1), "working")

    def test_columns_are_four(self):
        self.assertEqual(board_mod.COLUMNS, ["new", "working", "review", "done"])
        self.assertEqual(set(board_mod.STAGE_RU), set(board_mod.COLUMNS) | {"archived"})
        self.assertNotIn("needs_you", board_mod.COLUMNS)
        self.assertNotIn("accepted", board_mod.COLUMNS)
        self.assertNotIn("done_system", board_mod.COLUMNS)

    def test_allowed_moves(self):
        self.assertEqual(board_mod.allowed_moves("review"), ["done", "working"])
        self.assertEqual(board_mod.allowed_moves("done"), ["working"])
        self.assertEqual(board_mod.allowed_moves("working"), ["review"])
        self.assertEqual(board_mod.allowed_moves("new"), [])
        self.assertEqual(board_mod.allowed_moves("archived"), [])


class RowTest(unittest.TestCase):
    """Строка и счётчики: поля контракта, число = число карточек."""

    def _rec(self, root_id, title="Химчистка: приём белья"):
        return {"root_id": root_id, "title": title, "created_at": 1793000000}

    def test_row_has_contract_fields(self):
        summary = {"id": "t_0000aaaa", "title": "Химчистка: приём белья",
                   "title_short": "Приём белья", "status": "running",
                   "progress": {"done": 2, "total": 3, "percent": 66},
                   "assignee": "team-ops", "status_reason": "Работа идёт",
                   "finished_at": 1793000500}
        row = board_mod.row(self._rec("t_0000aaaa"), summary, status="running",
                            waiting=[{"kind": "question", "blocking": True},
                                     {"kind": "decision", "blocking": False}])
        self.assertEqual(set(row), ROW_FIELDS)
        self.assertEqual(row["column"], "working")
        self.assertEqual(row["stage"], "working")
        self.assertTrue(row["waiting_you"])
        self.assertEqual(row["waiting_count"], 2)
        self.assertEqual(row["waiting_blocking"], 1)
        self.assertEqual(row["needs_you"], {"count": 2, "blocking": 1})
        self.assertEqual((row["steps_done"], row["steps_total"]), (2, 3))
        self.assertEqual(row["updated_at"], 1793000500)
        self.assertTrue(row["summary_short"])

    def test_waiting_flag_only_in_working(self):
        """Метка «Ждёт вас» — на карточке в работе; в «На проверке» её нет."""
        summary = {"id": "t_0000bbbb", "title": "Грузоперевозки: слоты", "status": "done"}
        review = board_mod.row(self._rec("t_0000bbbb", "Грузоперевозки: слоты"), summary,
                               status="done", waiting=[{"kind": "question", "blocking": False}])
        self.assertEqual(review["column"], "review")
        self.assertFalse(review["waiting_you"])
        self.assertEqual(review["waiting_count"], 1)   # пункт есть, но это не метка «в работе»

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
        # статус ЗАДАЧИ важнее статуса корневой карточки: корень `todo`, работа идёт
        self.assertEqual(by_id["t_0000bbbb"]["column"], "working")
        self.assertEqual(by_id["t_0000aaaa"]["column"], "review")

    def test_counts_equal_cards_in_columns(self):
        summaries = {"t_0000aaaa": {"title": "Химчистка", "status": "done"},
                     "t_0000bbbb": {"title": "Грузоперевозки", "status": "working"},
                     "t_0000cccc": {"title": "Кофейня", "status": "done"}}
        rows = board_mod.build_rows(
            [self._rec("t_0000aaaa"), self._rec("t_0000bbbb"), self._rec("t_0000cccc")],
            states={"t_0000aaaa": {"status": "done"},
                    "t_0000bbbb": {"status": "running"},
                    "t_0000cccc": {"status": "done"}},
            accepted={"t_0000cccc"},
            summary_of=lambda rec: summaries.get(rec["root_id"]) or {})
        counts = board_mod.counts(rows)
        self.assertEqual(counts["review"], 1)
        self.assertEqual(counts["working"], 1)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["total"], len(rows))
        for column in board_mod.COLUMNS:
            self.assertEqual(counts[column],
                             sum(1 for row in rows if row["column"] == column), column)

    def test_accepted_task_reports_no_waiting_items(self):
        summary = {"id": "t_0000dddd", "title": "Кофейня: витрина", "status": "done",
                   "progress": {"done": 3, "total": 3}}
        row = board_mod.row(self._rec("t_0000dddd", "Кофейня: витрина"), summary,
                            status="done", accepted=True,
                            waiting=[{"kind": "question", "blocking": False},
                                     {"kind": "question", "blocking": True}])
        self.assertEqual(row["column"], "done")
        self.assertFalse(row["waiting_you"])
        self.assertEqual(row["waiting_count"], 0)
        self.assertEqual(row["needs_you"], {"count": 0, "blocking": 0})
        back = board_mod.row(self._rec("t_0000dddd", "Кофейня: витрина"), summary,
                             status="done",
                             waiting=[{"kind": "question", "blocking": True}])
        self.assertEqual(back["needs_you"], {"count": 1, "blocking": 1})

    def test_work_mark_keeps_card_in_working(self):
        """Принятую задачу вернули в работу — карточка стоит в «В работе»."""
        summary = {"id": "t_0000eeee", "title": "Химчистка: выдача", "status": "done"}
        self.assertEqual(board_mod.stage_of("done"), "review")
        row = board_mod.row(self._rec("t_0000eeee", "Химчистка: выдача"), summary,
                            status="done", work_mark={"at": 1793000700})
        self.assertEqual(row["column"], "working")
        # приняли снова — метка не спорит с заморозкой
        again = board_mod.row(self._rec("t_0000eeee", "Химчистка: выдача"), summary,
                              status="done", accepted=True, work_mark={"at": 1})
        self.assertEqual(again["column"], "done")

    def test_payload_shape(self):
        body = board_mod.payload(ROOM_ID, [])
        self.assertEqual(body["columns"], board_mod.COLUMNS)
        self.assertEqual(body["room"], ROOM_ID)
        self.assertEqual(body["counts"]["total"], 0)
        self.assertEqual(len(body["labels"]), 5)


class CacheTest(unittest.TestCase):
    """Отпечаток доски и хранилище ответов (без сервера)."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        cls.agents = SimAgents(cls.sb)
        cls.ids = cls.sb.state("done_modern")
        cls.records = [{"root_id": cls.ids["root_id"]}]
        cls.room = dict(ROOM)

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def setUp(self):
        cache_mod.invalidate()

    def _mark(self):
        return cache_mod.fingerprint(self.cfg, self.room, self.records, "team")

    def _set_status(self, status):
        con = self.agents.connect()
        try:
            con.execute("UPDATE tasks SET status=? WHERE id=?", (status, self.ids["root_id"]))
            con.commit()
        finally:
            con.close()

    def test_fingerprint_is_stable_without_changes(self):
        self.assertEqual(self._mark(), self._mark())

    def test_fingerprint_changes_on_new_comment(self):
        before = self._mark()
        self.agents.comment(self.ids["root_id"], "team-ops", "Проверка: комментарий")
        self.assertNotEqual(before, self._mark(), "комментарий должен менять отпечаток")

    def test_fingerprint_changes_on_status_change(self):
        self._set_status("blocked")
        before = self._mark()
        self._set_status("running")
        self.assertNotEqual(before, self._mark(), "смена статуса должна менять отпечаток")

    def test_fingerprint_changes_on_state_file_write(self):
        before = self._mark()
        board_mod.mark_working(self.cfg, ROOM_ID, self.ids["root_id"], note="вернули")
        try:
            self.assertNotEqual(before, self._mark(), "файл комнаты должен менять отпечаток")
        finally:
            board_mod.clear_working(self.cfg, ROOM_ID, self.ids["root_id"])

    def test_lookup_and_invalidate(self):
        mark = self._mark()
        payload = {"items": [{"id": "t_0000aaaa"}], "has_more": False}
        cache_mod.store("room_requests", ROOM_ID, mark, payload)
        self.assertEqual(cache_mod.lookup("room_requests", ROOM_ID, mark), payload)
        # другой отпечаток — промах (устаревшее не отдаём)
        self.assertIsNone(cache_mod.lookup("room_requests", ROOM_ID, mark + "x"))
        self.assertEqual(cache_mod.invalidate(ROOM_ID), 1)
        self.assertIsNone(cache_mod.lookup("room_requests", ROOM_ID, mark))


class SandboxBoardTest(unittest.TestCase):
    """Живая песочная доска: ручка отдаёт колонки, метки и кэширует ответ."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        cls.agents = SimAgents(cls.sb)
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

    def setUp(self):
        cache_mod.invalidate()

    def test_board_endpoint_returns_columns(self):
        status, payload, _headers = self.app.h_room_tasks({}, {}, ROOM_ID)
        self.assertEqual(status, 200)
        self.assertEqual(payload["columns"], board_mod.COLUMNS)
        self.assertGreaterEqual(payload["counts"]["total"], 4)
        columns = {row["column"] for row in payload["tasks"]}
        self.assertIn("working", columns)
        self.assertIn("review", columns)
        self.assertIn("new", columns)
        for row in payload["tasks"]:
            self.assertEqual(set(row), ROW_FIELDS, row)
            self.assertIn(row["column"], board_mod.COLUMNS + [board_mod.ARCHIVED])
            self.assertEqual(row["stage"], row["column"])
        self.assertEqual(payload["counts"]["total"], len(payload["tasks"]))
        for column in board_mod.COLUMNS:
            self.assertEqual(payload["counts"][column],
                             sum(1 for row in payload["tasks"] if row["column"] == column), column)

    def test_cache_returns_same_answer_and_drops_on_change(self):
        cache_mod.invalidate()
        _status, first, _headers = self.app.h_room_tasks({}, {}, ROOM_ID)
        hits_before = cache_mod.stats()["hits"]
        _status, second, _headers = self.app.h_room_tasks({}, {}, ROOM_ID)
        self.assertEqual(first, second, "без изменений ответ берётся из кэша")
        self.assertGreater(cache_mod.stats()["hits"], hits_before,
                           "второй запрос должен попасть в кэш")
        before = {row["id"]: row["status"] for row in first["tasks"]}
        target = self.sb.state("reviewing")["root_id"]
        self.assertIn(target, before)
        # меняем статус карточки на доске — кэш обязан сброситься
        con = self.agents.connect()
        try:
            con.execute("UPDATE tasks SET status='blocked' WHERE id=?", (target,))
            con.commit()
        finally:
            con.close()
        _status, third, _headers = self.app.h_room_tasks({}, {}, ROOM_ID)
        after = {row["id"]: row["status"] for row in third["tasks"]}
        self.assertNotEqual(before.get(target), after.get(target),
                            "кэш не должен отдавать прежний статус карточки")

    def test_projects_json_links_are_read(self):
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


class MoveActionTest(unittest.TestCase):
    """Действие `move` (контракт W31): принять, вернуть в работу, отказы."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        from server import app as app_mod
        from server import registry
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))
        cls.ids = {}
        for name in ("queued", "done_modern"):
            ids = cls.sb.state(name)
            cls.ids[name] = ids["root_id"]
            registry.add_request(cls.cfg, ROOM_ID, {
                "root_id": ids["root_id"], "title": "Задача: " + name,
                "text": "нейтральный пример: химчистка", "created_at": 1793000000,
            })

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def setUp(self):
        from server import passports
        cache_mod.invalidate()
        path = passports.accepted_path(self.cfg, ROOM_ID)
        if path.exists():
            path.unlink()
        passports.invalidate()
        board_mod.clear_working(self.cfg, ROOM_ID, self.ids["done_modern"])
        board_mod.clear_working(self.cfg, ROOM_ID, self.ids["queued"])

    def _move(self, rid, to, **body):
        return self.app.h_request_actions({}, dict(body, action="move", to=to), rid)

    def test_review_to_done_asks_confirm_then_accepts(self):
        """Открытые вопросы: сначала «принять и закрыть?», потом принятие."""
        from server.http_util import ApiError
        with self.assertRaises(ApiError) as ctx:
            self._move(self.ids["done_modern"], "done")
        self.assertEqual(ctx.exception.status, 409)
        body = ctx.exception.payload()
        dump = json.dumps(body, ensure_ascii=False)
        self.assertIn("needs_confirm", dump)
        self.assertIn("open_questions", dump)
        self.assertRegex(dump, r'"open_questions"[^0-9]{0,8}[1-9]')
        status, payload, _headers = self._move(self.ids["done_modern"], "done", confirm=True)
        self.assertGreaterEqual(int(payload.get("closed_questions") or 0), 1,
                                "принятие закрывает вопросы этой задачи")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["to"], "done")
        self.assertEqual(payload["from_column"], "review")
        self.assertEqual(payload["board_column"], "done")
        self.assertEqual(payload["allowed_moves"], ["working"])
        self.assertTrue(payload["accepted"])
        from server import passports
        self.assertTrue(passports.is_accepted(self.cfg, ROOM, self.ids["done_modern"]))
        # у принятой задачи открытых пунктов нет — страница задачи это подтверждает
        _status, detail, _headers = self.app.h_request_get({}, {}, self.ids["done_modern"])
        self.assertEqual(detail["board_column"], "done")
        self.assertEqual(detail["allowed_moves"], ["working"])
        self.assertEqual(detail["needs_you_count"]["total"], 0)

    def test_done_to_working_returns_to_work(self):
        self._move(self.ids["done_modern"], "done", confirm=True)
        status, payload, _headers = self._move(self.ids["done_modern"], "working")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["board_column"], "working")
        self.assertFalse(payload["accepted"])
        from server import passports
        self.assertFalse(passports.is_accepted(self.cfg, ROOM, self.ids["done_modern"]))
        # карточка правда стоит в «В работе» — и на доске, и в странице задачи
        _status, board, _headers = self.app.h_room_tasks({}, {}, ROOM_ID)
        by_id = {row["id"]: row for row in board["tasks"]}
        self.assertEqual(by_id[self.ids["done_modern"]]["column"], "working")
        _status, detail, _headers = self.app.h_request_get({}, {}, self.ids["done_modern"])
        self.assertEqual(detail["board_column"], "working")
        self.assertIn("review", detail["allowed_moves"])

    def test_bad_move_is_human_error(self):
        from server.http_util import ApiError
        with self.assertRaises(ApiError) as ctx:
            self._move(self.ids["queued"], "working")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("bad_move", json.dumps(ctx.exception.payload(), ensure_ascii=False))
        with self.assertRaises(ApiError) as ctx:
            self._move(self.ids["done_modern"], "new")
        self.assertEqual(ctx.exception.status, 409)
