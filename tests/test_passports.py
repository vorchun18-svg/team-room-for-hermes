# -*- coding: utf-8 -*-
"""Паспорт принятой задачи и память комнаты (W30-D1, контракт §3).

Проверяем:
* паспорт собирается БЕЗ вызова модели — из свода, решений и путей артефактов;
* форма паспорта — ``{title, outcome ≤ 600, decisions[], files[{agent, path, what}], at}``;
* сводка «Принятые задачи комнаты» ≤ 1200 знаков и попадает в контекст блок новой
  задачи (``context_pack``);
* паспорт находится поиском по памяти комнаты (``memory_search``, вид ``passport``);
* запись принятия идемпотентна, отметки видны пачкой (``frozen_roots``) и
  сбрасываются возвратом в работу.

    python -m unittest tests.test_passports -v
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

from server import context_pack, memory_search, passports   # noqa: E402
from tests.helpers import Sandbox                           # noqa: E402

ROOM_ID = "laundry"
ROOM = {"id": ROOM_ID, "board": "team", "orchestrator": "team-director"}
ROOT_ID = "t_0000aaaa"


def _detail():
    """Деталь задачи: свод, одно принятое решение, два файла агентов."""
    return {
        "id": ROOT_ID, "title": "Химчистка: приём белья и выдача", "status": "done",
        "title_short": "Приём белья", "accepted": False, "artifacts": [],
        "synthesis": {"text_md": "Итог: приём и выдача в двух точках, график 9–20."},
        "decisions": [{"id": "d_1", "text": "Работаем без выходных", "status": "accepted"},
                      {"id": "d_2", "text": "Старый вариант", "status": "dismissed"}],
        "tree": [
            {"id": ROOT_ID, "role": "root", "status": "done", "assignee": "team-director",
             "metadata": {"artifacts": ["C:/rooms/laundry/report.md"]}},
            {"id": "t_0000bbbb", "role": "worker", "status": "done", "assignee": "team-ops",
             "metadata": {"artifacts": [{"path": "C:/rooms/laundry/slots.csv",
                                         "title": "Слоты приёма"}]}},
        ],
        "open_questions": [], "questions": [], "next_steps": [], "approvals": [],
    }


class PassportTest(unittest.TestCase):
    """Паспорт — из данных карточек, без модели."""

    def test_build_passport_shape_and_no_model(self):
        # Модель не вызывается вовсе: у нас нет ни runner'а, ни сети — только словари.
        passport = passports.build_passport(None, ROOM, {"root_id": ROOT_ID}, _detail())
        self.assertEqual(set(passport), {"title", "outcome", "decisions", "files", "at"})
        self.assertEqual(passport["title"], "Приём белья")
        self.assertIn("приём и выдача", passport["outcome"])
        self.assertLessEqual(len(passport["outcome"]), passports.OUTCOME_MAX)
        self.assertEqual(passport["decisions"], ["Работаем без выходных"])
        agents = {item["agent"] for item in passport["files"]}
        self.assertEqual(agents, {"team-director", "team-ops"})
        for item in passport["files"]:
            self.assertEqual(set(item), {"agent", "path", "what"})

    def test_passport_without_detail_falls_back_to_record(self):
        """Деталь не прочиталась — паспорт всё равно есть (название из реестра)."""
        passport = passports.build_passport(None, ROOM, {
            "root_id": ROOT_ID, "title": "Грузоперевозки: слоты", "summary": "Сделано"}, None)
        self.assertEqual(passport["title"], "Грузоперевозки: слоты")
        self.assertEqual(passport["outcome"], "Сделано")
        self.assertEqual(passport["decisions"], [])
        self.assertEqual(passport["files"], [])


class StorageTest(unittest.TestCase):
    """Файл принятых задач комнаты: форма, идемпотентность, отметки пачкой."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def setUp(self):
        path = passports.accepted_path(self.cfg, ROOM_ID)
        if path.exists():
            path.unlink()
        passports.invalidate()

    def test_accept_is_idempotent_and_visible(self):
        passport = passports.build_passport(self.cfg, ROOM, {"root_id": ROOT_ID}, _detail())
        first = passports.accept(self.cfg, ROOM, {"root_id": ROOT_ID}, ROOT_ID,
                                 by="mikhail", passport=passport, stopped=["t_0000cccc"], at=1793000000)
        self.assertTrue(first["accepted"])
        self.assertFalse(first["already"])
        second = passports.accept(self.cfg, ROOM, {"root_id": ROOT_ID}, ROOT_ID, by="mikhail")
        self.assertTrue(second["already"])
        self.assertEqual(second["at"], 1793000000)      # паспорт не перезаписан
        self.assertTrue(passports.is_accepted(self.cfg, ROOM, ROOT_ID))
        self.assertEqual(passports.accepted_ids(self.cfg, ROOM), {ROOT_ID})
        self.assertIn(ROOT_ID, passports.frozen_roots(self.cfg))
        self.assertIn("t_0000cccc", passports.frozen_card_ids(self.cfg))

    def test_reopen_clears_everything(self):
        passports.accept(self.cfg, ROOM, {"root_id": ROOT_ID}, ROOT_ID, by="mikhail",
                         passport={"title": "Химчистка", "outcome": "Готово",
                                   "decisions": [], "files": [], "at": 1793000000})
        res = passports.reopen(self.cfg, ROOM, ROOT_ID, by="mikhail")
        self.assertTrue(res["reopened"])
        self.assertFalse(passports.is_accepted(self.cfg, ROOM, ROOT_ID))
        self.assertEqual(passports.accepted_ids(self.cfg, ROOM), set())
        # повторный возврат — честное «не была принята», без исключения
        self.assertFalse(passports.reopen(self.cfg, ROOM, ROOT_ID)["reopened"])

    def test_room_summary_is_short_and_last_first(self):
        for index in range(9):
            root_id = "t_0000%04d" % index
            passports.accept(self.cfg, ROOM, {"root_id": root_id}, root_id, by="mikhail",
                             passport={"title": "Задача %d" % index,
                                       "outcome": "Итог " + ("длинный текст " * 20),
                                       "decisions": [], "files": [], "at": 1793000000 + index})
        text = passports.summary_text(self.cfg, ROOM)
        self.assertTrue(text.startswith("Принятые задачи комнаты"))
        self.assertLessEqual(len(text), passports.ROOM_SUMMARY_MAX)
        self.assertIn("Задача 8", text)                 # последняя принятая — в сводке
        self.assertNotIn("Задача 0", text)              # в потолок не влезла

    def test_corrupt_file_does_not_break_reading(self):
        path = passports.accepted_path(self.cfg, ROOM_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{это не json", encoding="utf-8")
        passports.invalidate()
        self.assertEqual(passports.accepted_ids(self.cfg, ROOM), set())
        self.assertFalse(passports.is_accepted(self.cfg, ROOM, ROOT_ID))


class MemoryTest(unittest.TestCase):
    """Паспорт — в контекст новой задачи и в поиск по памяти комнаты."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def setUp(self):
        path = passports.accepted_path(self.cfg, ROOM_ID)
        if path.exists():
            path.unlink()
        passports.invalidate()
        passports.accept(self.cfg, ROOM, {"root_id": ROOT_ID}, ROOT_ID, by="mikhail",
                         passport={"title": "Приём белья",
                                   "outcome": "Приём и выдача в двух точках, график 9–20",
                                   "decisions": ["Работаем без выходных"],
                                   "files": [{"agent": "team-ops", "path": "C:/rooms/laundry/a.md",
                                              "what": "Итог"}], "at": 1793000000})

    def test_context_block_has_accepted_section(self):
        section = context_pack.accepted_section(self.cfg, {"id": ROOM_ID})
        self.assertIn("Принятые задачи комнаты", section)
        self.assertIn("Приём белья", section)
        self.assertLessEqual(len(section), 1200)
        block = context_pack.build_context_block(self.cfg, {"id": ROOM_ID, "title": "Химчистка"},
                                                 "Сделай ещё одну точку приёма")
        self.assertIn("Принятые задачи комнаты", block)
        self.assertLessEqual(len(block), context_pack.MAX_BLOCK_CHARS)

    def test_memory_search_finds_passport(self):
        found = memory_search.search(self.cfg, "приём белья", room=[ROOM_ID], limit=10)
        kinds = {item["kind"] for item in found["items"]}
        self.assertIn("passport", kinds, found)
        hit = next(item for item in found["items"] if item["kind"] == "passport")
        self.assertEqual(hit["request_id"], ROOT_ID)
        self.assertIn("Приём белья", hit["title"])
        self.assertIn("passport", memory_search.KINDS)
        self.assertIn("passport", memory_search.KIND_TITLES)


if __name__ == "__main__":
    unittest.main()
