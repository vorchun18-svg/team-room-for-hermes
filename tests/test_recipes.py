# -*- coding: utf-8 -*-
"""Рецепты комнаты (server/recipes.py, W29B-Q1) — только в песочнице.

Проверяем: встроенные примеры, правку/корзину/возврат, проверки рецепта,
запуск (запрос + карточки первого слоя с моделью комнаты), граф зависимостей
(следующий слой создаётся ``tick``, когда предшественник готов) и сохранение
запроса как рецепта.

    python -m unittest tests.test_recipes -v
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

from server import recipes  # noqa: E402
from tests.helpers import Sandbox  # noqa: E402

ROOM = {
    "id": "cafe",
    "title": "Кофейня у площади",
    "board": "team",
    "tenant": "room:cafe",
    "orchestrator": "team-director",
    "critic": "team-critic",
    "members": [
        {"profile": "team-director", "role": "orchestrator", "note": "Руководитель комнаты"},
        {"profile": "team-lawyer", "role": "worker", "note": "Юрист: договоры и претензии"},
        {"profile": "team-ops", "role": "worker", "note": "Оператор: заказы и факты"},
        {"profile": "team-critic", "role": "critic", "note": "Критик: проверяет свод"},
    ],
    "agent_models": {
        "team-lawyer": {"provider": "ollama-cloud", "model": "model-lawyer"},
        "team-ops": {"provider": "ollama-cloud", "model": "model-ops"},
        "team-director": {"provider": "ollama-cloud", "model": "model-lead"},
    },
}


class RecipesTest(unittest.TestCase):
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

    # --- помощники -------------------------------------------------------
    def _row(self, task_id: str) -> sqlite3.Row | None:
        return self.sb.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def _children(self, root_id: str) -> list[sqlite3.Row]:
        return self.sb.rows(
            "SELECT t.* FROM tasks t JOIN task_links l ON l.child_id = t.id "
            "WHERE l.parent_id = ? ORDER BY t.created_at", (root_id,))

    def _exec(self, sql: str, params: tuple = ()) -> None:
        """Запись в ПЕСОЧНУЮ доску (только для подготовки состояния в тесте)."""
        con = sqlite3.connect(str(self.sb.db), timeout=10)
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def _recipe(self, **over):
        data = {"name": "Сверка заказа", "description": "Проверить заказ по фактам",
                "fields": [{"id": "client", "label": "Клиент", "kind": "text", "required": True}],
                "steps": [
                    {"id": "s1", "role": "юрист", "title": "Проверить условия",
                     "instruction": "Проверь условия заказа."},
                    {"id": "s2", "role": "оператор", "title": "Собрать факты",
                     "instruction": "Собери факты по заказу.", "depends_on": ["s1"]},
                    {"id": "s3", "role": "руководитель", "title": "Ответ клиенту",
                     "instruction": "Сведи ответ.", "depends_on": ["s1", "s2"],
                     "critic": True},
                ]}
        data.update(over)
        return data

    # --- встроенные примеры ----------------------------------------------
    def test_01_listing_seeds_examples_for_room_topic(self):
        payload = recipes.listing(self.cfg, ROOM)
        names = [item["name"] for item in payload["items"]]
        self.assertIn("Меню на неделю", names, names)
        self.assertTrue(all(item["builtin"] for item in payload["items"]))
        self.assertEqual(payload["trash"], [])
        self.assertEqual(payload["runs"], [])
        self.assertTrue(payload["roles"])
        self.assertTrue(Path(recipes.recipes_path(self.cfg, "cafe")).is_file())
        # тема комнаты не «грузоперевозки» — чужих примеров в комнате нет
        self.assertNotIn("Сверка накладных", names)

        other = {"id": "laundry", "title": "Химчистка", "members": []}
        laundry = [i["name"] for i in recipes.listing(self.cfg, other)["items"]]
        self.assertIn("Ответ клиенту на претензию", laundry)
        self.assertNotIn("Меню на неделю", laundry)

    def test_02_resolve_role_by_member_note(self):
        self.assertEqual(recipes.resolve_role(ROOM, "юрист"), "team-lawyer")
        self.assertEqual(recipes.resolve_role(ROOM, "оператор"), "team-ops")
        self.assertEqual(recipes.resolve_role(ROOM, "руководитель"), "team-director")
        self.assertEqual(recipes.resolve_role(ROOM, "нет-такой"), "team-lawyer")
        empty = {"members": []}
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.resolve_role(empty, "юрист")
        self.assertEqual(ctx.exception.http, 409)

    # --- правка и корзина -------------------------------------------------
    def test_03_save_edit_trash_restore(self):
        saved = recipes.save(self.cfg, ROOM, self._recipe())
        self.assertTrue(saved["id"].startswith("rc-"))
        self.assertFalse(saved["builtin"])
        self.assertEqual([s["assignee"] for s in saved["steps"]], ["", "", ""])

        changed = recipes.save(self.cfg, ROOM, self._recipe(name="Сверка заказа v2"),
                               saved["id"])
        self.assertEqual(changed["name"], "Сверка заказа v2")
        self.assertEqual(len(recipes.listing(self.cfg, ROOM)["items"]),
                         len(recipes.builtin_for(ROOM)) + 1)

        removed = recipes.remove(self.cfg, ROOM, saved["id"])
        self.assertTrue(removed["ok"])
        payload = recipes.listing(self.cfg, ROOM)
        self.assertNotIn(saved["id"], [i["id"] for i in payload["items"]])
        self.assertIn(saved["id"], [i["id"] for i in payload["trash"]])

        back = recipes.restore(self.cfg, ROOM, saved["id"])
        self.assertEqual(back["id"], saved["id"])
        self.assertIn(saved["id"], [i["id"] for i in recipes.listing(self.cfg, ROOM)["items"]])

    def test_04_validation_names_the_problem(self):
        cases = [
            (self._recipe(name=""), "Название"),
            (self._recipe(steps=[]), "ни одного шага"),
            (self._recipe(steps=[{"id": "s1", "title": "Шаг"}]),
             "не указана роль или исполнитель"),
            (self._recipe(steps=[{"id": "s1", "role": "юрист", "title": "Раз",
                                  "depends_on": ["s9"]}]), "несуществующий"),
            (self._recipe(steps=[
                {"id": "s1", "role": "юрист", "title": "Раз", "depends_on": ["s2"]},
                {"id": "s2", "role": "оператор", "title": "Два", "depends_on": ["s1"]}]),
             "по кругу"),
            (self._recipe(fields=[{"id": "f1", "label": "Почта", "kind": "sms"}]),
             "Неизвестный вид поля"),
        ]
        for data, needle in cases:
            with self.assertRaises(recipes.RecipeError) as ctx:
                recipes.save(self.cfg, ROOM, data)
            self.assertIn(needle, ctx.exception.message)
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.save(self.cfg, ROOM, self._recipe(steps=[
                {"id": "s1", "assignee": "coder", "title": "Чужой шаг"}]))
        self.assertIn("не участвует в комнате", ctx.exception.message)

    def test_05_builtin_can_be_edited_into_copy(self):
        item = recipes.listing(self.cfg, ROOM)["items"][0]
        data = {"name": item["name"] + " (свой)", "description": item["description"],
                "fields": item["fields"], "steps": item["steps"]}
        if item["builtin"]:
            edited = recipes.save(self.cfg, ROOM, data, item["id"])
            self.assertFalse(edited["builtin"], "правка примера — это уже свой рецепт")

    # --- запуск -----------------------------------------------------------
    def test_06_launch_creates_request_and_first_wave_with_models(self):
        saved = recipes.save(self.cfg, ROOM, self._recipe(name="Запуск по заказу"))
        result = recipes.launch(self.cfg, ROOM, saved["id"], {"client": "Кофейня «Пар»"})
        self.assertTrue(result["ok"])
        root_id = result["request_id"]
        root = self._row(root_id)
        self.assertIsNotNone(root)
        self.assertEqual(root["status"], "triage")
        self.assertEqual(root["tenant"], "room:cafe")
        self.assertIn("ПЛАН ГОТОВ", root["body"])
        self.assertIn("Кофейня «Пар»", root["body"])
        self.assertIn("зависит: шаг 1", root["body"])

        cards = self._children(root_id)
        # первый слой графа — только s1: s2/s3 ждут предшественников
        self.assertEqual(len(cards), 1, [c["title"] for c in cards])
        self.assertEqual(cards[0]["assignee"], "team-lawyer")
        self.assertEqual(cards[0]["model_override"], "model-lawyer")
        self.assertEqual(cards[0]["provider_override"], "ollama-cloud")
        self.assertIn(cards[0]["status"], ("ready", "todo", "running", "queued"),
                      "карточка шага создана и ждёт диспетчера, а не в triage")
        self.assertNotEqual(cards[0]["status"], "triage")
        # модель шага — из выбора комнаты (второй участник — своя)
        stored = recipes.listing(self.cfg, ROOM)["runs"][0]
        self.assertEqual(stored["request_id"], root_id)
        self.assertEqual([s["status"] for s in stored["steps"]],
                         ["created", "pending", "pending"])

    def test_07_tick_creates_dependent_step_when_predecessor_done(self):
        saved = recipes.save(self.cfg, ROOM, self._recipe(name="Граф по шагам"))
        result = recipes.launch(self.cfg, ROOM, saved["id"], {"client": "«Пар»"})
        root_id = result["request_id"]
        first = self._children(root_id)[0]
        self.assertTrue(first["id"])

        # предшественник ещё не готов — второй шаг не создаётся
        self.assertEqual(recipes.tick(self.cfg, ROOM)["created"], 0)
        self.assertEqual(len(self._children(root_id)), 1)

        self._exec("UPDATE tasks SET status='done' WHERE id=?", (first["id"],))
        stats = recipes.tick(self.cfg, ROOM)
        self.assertEqual(stats["created"], 1)
        cards = self._children(root_id)
        self.assertEqual(len(cards), 2)
        second = [c for c in cards if c["id"] != first["id"]][0]
        self.assertEqual(second["assignee"], "team-ops")
        self.assertEqual(second["model_override"], "model-ops")

        self._exec("UPDATE tasks SET status='done' WHERE id=?", (second["id"],))
        self.assertEqual(recipes.tick(self.cfg, ROOM)["created"], 1)
        third = [c for c in self._children(root_id) if c["id"] not in (first["id"], second["id"])][0]
        self.assertEqual(third["assignee"], "team-director")
        self.assertEqual(third["model_override"], "model-lead")

    def test_08_launch_checks_required_fields(self):
        saved = recipes.save(self.cfg, ROOM, self._recipe(name="Поле обязательное"))
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.launch(self.cfg, ROOM, saved["id"], {})
        self.assertEqual(ctx.exception.http, 400)
        self.assertIn("Клиент", ctx.exception.message)

    def test_09_from_request_saves_steps_and_marks_source(self):
        saved = recipes.save(self.cfg, ROOM, self._recipe(name="Из запроса — основа"))
        result = recipes.launch(self.cfg, ROOM, saved["id"], {"client": "«Пар»"})
        root_id = result["request_id"]
        self._exec("UPDATE tasks SET body=? WHERE id=?", ("Сделай шаг и оставь итог", root_id))
        first = self._children(root_id)[0]
        self._exec("UPDATE tasks SET body='Пошаговая работа' WHERE id=?", (first["id"],))

        made = recipes.from_request(self.cfg, ROOM, root_id, name="Повтор претензии")
        self.assertEqual(made["name"], "Повтор претензии")
        self.assertEqual(made["source"], "request")
        self.assertTrue(made["steps"])
        self.assertEqual(made["steps"][0]["assignee"], "team-lawyer")
        self.assertTrue(made["fields"][0]["required"])
        stored = recipes.get(self.cfg, ROOM, made["id"])
        self.assertEqual(stored["source"], "request")
        self.assertEqual(stored["steps"][0]["depends_on"], [])

    def test_10_missing_recipe_is_404(self):
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.get(self.cfg, ROOM, "rc-nope")
        self.assertEqual(ctx.exception.http, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
