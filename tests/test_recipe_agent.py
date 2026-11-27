# -*- coding: utf-8 -*-
"""Разговор о рецепте и лёгкий анализ завершённых задач (W31-C) — только в песочнице.

Проверяем ровно то, что просил Михаил (дефекты 28–29):

* «Создать рецепт» — разговор: человек описал дело → руководитель задаёт вопросы
  по-человечески → готовый алгоритм (поля, шаги, зависимости, критик) → «Да»/«Исправить»;
* «Сохранить задачу как рецепт» — то же, но задание уходит внутрь завершённой задачи;
* периодический анализ: раз в N дней, только предлагает, не предлагает дубли
  существующих рецептов, помнит отклонённое, показывает прикидку токенов;
* примеры чужих сфер помечены «образец» и в комнату не сеются.

Модели НЕ вызываются ни разу: руководителя играет тест (комментарий от его профиля),
а подставной руководитель песочницы (``TEAMROOM_RECIPE_FAKE=1``) — детерминированный.

    python -m unittest tests.test_recipe_agent -v
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import engine_write, recipe_agent, recipes  # noqa: E402
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

DRAFT = {
    "name": "Разбор претензии по заказу",
    "description": "Претензия клиента: факты, позиция, ответ",
    "fields": [{"id": "f1", "label": "Клиент и заказ", "kind": "text", "required": True}],
    "steps": [
        {"id": "s1", "role": "юрист", "title": "Проверить требования",
         "instruction": "Что требует клиент и в какие сроки отвечаем.", "depends_on": []},
        {"id": "s2", "role": "оператор", "title": "Собрать факты",
         "instruction": "Заказ, чеки, переписка.", "depends_on": ["s1"]},
        {"id": "s3", "role": "руководитель", "title": "Ответ клиенту",
         "instruction": "Свести ответ и что меняем.", "critic": True, "depends_on": ["s1", "s2"]},
    ],
}


def _reply(stage_payload: dict) -> str:
    return recipe_agent.MARK + "\n```json\n" + json.dumps(stage_payload, ensure_ascii=False) + "\n```"


class RecipeAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls._env = {k: os.environ.get(k) for k in ("TEAMROOM_SANDBOX", "TEAMROOM_RECIPE_FAKE")}
        os.environ["TEAMROOM_SANDBOX"] = "1"
        os.environ.pop("TEAMROOM_RECIPE_FAKE", None)

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls._stack.close()

    # --- помощники -------------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> None:
        con = sqlite3.connect(str(self.sb.db), timeout=10)
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def _card(self, card_id: str) -> sqlite3.Row | None:
        return self.sb.one("SELECT * FROM tasks WHERE id=?", (card_id,))

    def _lead_says(self, card_id: str, payload: dict, *, offers: bool = False) -> None:
        """Руководитель отвечает комментарием — тот же путь, что у живого агента."""
        mark = recipe_agent.OFFER_MARK if offers else recipe_agent.MARK
        text = mark + "\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        engine_write.act(self.cfg, "team", card_id, "comment", text=text, author="team-director")

    def _done_task(self, title: str, body: str = "", days_ago: int = 1) -> str:
        """Завершённая задача комнаты (для анализа) — прямо в песочную доску."""
        card_id = "t_%08x" % (int(time.time() * 1000) % 0xFFFFFFFF)
        at = int(time.time()) - days_ago * 86400
        self._exec("INSERT INTO tasks (id, title, body, assignee, status, created_at, completed_at,"
                   " tenant, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                   (card_id, title, body, "team-ops", "done", at, at, "room:cafe", "Комната команды"))
        return card_id

    # --- 1. разговор: начало --------------------------------------------
    def test_01_start_puts_card_to_lead(self):
        dialog = recipe_agent.start(self.cfg, ROOM, "Каждый день разбираем претензии клиентов "
                                                    "по заказам, делаем это руками")
        self.assertEqual(dialog["stage"], "asked")
        self.assertTrue(dialog["waiting"])
        self.assertTrue(dialog["card_id"].startswith("t_"))
        card = self._card(dialog["card_id"])
        self.assertIsNotNone(card)
        self.assertEqual(card["assignee"], "team-director", "карточка — руководителю комнаты")
        self.assertEqual(card["tenant"], "room:cafe")
        self.assertIn("РЕЦЕПТ", card["body"])
        self.assertIn(recipe_agent.MARK, card["body"], "в теле — формат ответа с меткой")
        self.assertIn("претензии клиентов", card["body"])
        self.assertIn("team-lawyer", card["body"], "руководитель видит роли комнаты")
        # модель — руководителя комнаты (своя у него, общей у комнаты нет)
        self.assertEqual(dialog["model"]["model"], "model-lead")

    def test_02_short_text_is_refused(self):
        with self.assertRaises(recipe_agent.RecipeAgentError) as ctx:
            recipe_agent.start(self.cfg, ROOM, "да")
        self.assertEqual(ctx.exception.http, 400)
        self.assertIn("Опишите", ctx.exception.message)

    # --- 2. вопросы → ответы → алгоритм → «Да» ---------------------------
    def test_03_questions_answers_draft_accept(self):
        dialog = recipe_agent.start(self.cfg, ROOM, "Разбираем претензии клиентов по заказам")
        self._lead_says(dialog["card_id"], {
            "stage": "questions", "summary": "Понял: претензии повторяются",
            "questions": ["Кто разбирает претензию?", "Что считается «готово»?"]})
        state = recipe_agent.get(self.cfg, ROOM, dialog["id"])
        self.assertEqual(state["stage"], "questions")
        self.assertEqual(len(state["questions"]), 2)
        self.assertIn("Кто разбирает", state["questions"][0]["text"])
        self.assertFalse(any("D19" in q["text"] or "Q3" in q["text"] for q in state["questions"]),
                         "вопросы по-человечески, без внутренних кодов")

        after_answer = recipe_agent.answer(self.cfg, ROOM, dialog["id"],
                                           "Юрист проверяет, оператор собирает факты. "
                                           "Готово — когда клиенту отправлен ответ.")
        self.assertEqual(after_answer["stage"], "asked", "после ответов ждём руководителя")
        self._lead_says(dialog["card_id"], {"stage": "draft", "summary": "Собрал алгоритм",
                                           "recipe": DRAFT})
        state = recipe_agent.get(self.cfg, ROOM, dialog["id"])
        self.assertEqual(state["stage"], "draft")
        self.assertEqual(state["problem"], "")
        self.assertEqual(state["draft"]["name"], DRAFT["name"])
        self.assertEqual([s["title"] for s in state["draft"]["steps"]],
                         ["Проверить требования", "Собрать факты", "Ответ клиенту"])
        self.assertTrue(state["draft"]["steps"][-1]["critic"])

        result = recipe_agent.accept(self.cfg, ROOM, dialog["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["dialog"]["stage"], "accepted")
        saved = recipes.get(self.cfg, ROOM, result["recipe"]["id"])
        self.assertEqual(saved["source"], "dialog", "видно, что рецепт из разговора")
        self.assertEqual(len(saved["steps"]), 3)
        self.assertEqual(saved["steps"][1]["depends_on"], ["s1"])

    def test_04_bad_draft_shows_problem_and_does_not_save(self):
        dialog = recipe_agent.start(self.cfg, ROOM, "Ведём учёт поставок молока по пятницам")
        broken = {"name": "Учёт молока", "fields": [],
                  "steps": [{"id": "s1", "role": "юрист", "title": "Шаг", "depends_on": ["s9"]}]}
        self._lead_says(dialog["card_id"], {"stage": "draft", "recipe": broken})
        state = recipe_agent.get(self.cfg, ROOM, dialog["id"])
        self.assertEqual(state["stage"], "draft")
        self.assertIn("несуществующий", state["problem"])
        before = len(recipes.listing(self.cfg, ROOM)["items"])
        with self.assertRaises(recipe_agent.RecipeAgentError) as ctx:
            recipe_agent.accept(self.cfg, ROOM, dialog["id"])
        self.assertEqual(ctx.exception.http, 400)
        self.assertEqual(len(recipes.listing(self.cfg, ROOM)["items"]), before)

    def test_05_fix_sends_correction_to_lead(self):
        dialog = recipe_agent.start(self.cfg, ROOM, "Считаем выручку точки за неделю")
        self._lead_says(dialog["card_id"], {"stage": "draft", "recipe": dict(DRAFT, name="Выручка")})
        state = recipe_agent.get(self.cfg, ROOM, dialog["id"])
        self.assertEqual(state["stage"], "draft")
        after = recipe_agent.fix(self.cfg, ROOM, dialog["id"], "Добавь шаг «сверить с кассой»")
        self.assertEqual(after["stage"], "asked")
        self.assertTrue(any("сверить с кассой" in m["text"] for m in after["messages"]
                            if m["role"] == "human"))
        thread = recipe_agent._thread(self.cfg, ROOM, dialog["card_id"])
        self.assertTrue(any("Поправь алгоритм" in str(e.get("text") or "") for e in thread),
                        "правка ушла руководителю комментарием")

    def test_06_from_request_card_inside_task(self):
        root_id = self._done_task("Разбор жалобы на обслуживание", "Клиент жалуется на ожидание")
        child = "t_%08x" % ((int(time.time() * 1000) + 7) % 0xFFFFFFFF)
        self._exec("INSERT INTO tasks (id, title, body, assignee, status, created_at, tenant,"
                   " created_by) VALUES (?,?,?,?,?,?,?,?)",
                   (child, "Собрать факты по жалобе", "Переписка и чеки", "team-ops", "done",
                    int(time.time()), "room:cafe", "Комната команды"))
        self._exec("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)", (root_id, child))
        dialog = recipe_agent.start(self.cfg, ROOM, "Сохрани эту задачу как рецепт",
                                    request_id=root_id)
        self.assertEqual(dialog["source"], "request")
        self.assertEqual(dialog["request_id"], root_id)
        card = self._card(dialog["card_id"])
        self.assertIn("ЗАВЕРШЁННАЯ ЗАДАЧА", card["body"])
        self.assertIn("Собрать факты по жалобе", card["body"], "руководитель видит шаги задачи")
        # карточка-задание лежит ВНУТРИ задачи, а не отдельной задачей на доске
        link = self.sb.one("SELECT parent_id FROM task_links WHERE child_id=?",
                           (dialog["card_id"],))
        self.assertIsNotNone(link, "карточка рецепта привязана к задаче")
        self.assertEqual(link["parent_id"], root_id)

    def test_07_fake_lead_sandbox_full_cycle(self):
        """Подставной руководитель песочницы: вопросы → алгоритм без единого токена."""
        os.environ["TEAMROOM_RECIPE_FAKE"] = "1"
        try:
            dialog = recipe_agent.start(self.cfg, ROOM, "Каждую пятницу сводим кассу за неделю")
            state = recipe_agent.get(self.cfg, ROOM, dialog["id"])
            self.assertEqual(state["stage"], "questions", "первый ход — уточняющие вопросы")
            self.assertTrue(state["questions"])
            state = recipe_agent.answer(self.cfg, ROOM, dialog["id"], "Кассир, потом директор")
            self.assertEqual(state["stage"], "draft", "второй ход — готовый алгоритм")
            self.assertTrue(state["draft"]["fields"])
            self.assertTrue(state["draft"]["steps"])
            result = recipe_agent.accept(self.cfg, ROOM, dialog["id"])
            self.assertEqual(result["dialog"]["stage"], "accepted")
        finally:
            os.environ.pop("TEAMROOM_RECIPE_FAKE", None)

    # --- 3. примеры чужих сфер -------------------------------------------
    def test_08_foreign_samples_are_marked_and_not_seeded(self):
        other = {"id": "realroom", "title": "Стоматология на Ленина", "board": "team",
                 "tenant": "room:realroom", "members": []}
        payload = recipes.listing(self.cfg, other)
        self.assertEqual(payload["items"], [], "в комнате без темы нет чужих примеров среди рецептов")
        self.assertEqual(len(payload["samples"]), len(recipes.BUILTIN))
        for item in payload["samples"]:
            self.assertTrue(item["sample"])
            self.assertIn("образец", item["sample_note"])
        cafe = recipes.listing(self.cfg, ROOM)
        own = [i for i in cafe["items"] if i["builtin"]]
        self.assertEqual(len(own), 1, "своя сфера — один пример")
        self.assertTrue(own[0]["sample"])
        self.assertNotIn(own[0]["id"], [s["id"] for s in cafe["samples"]])

    # --- 4. анализ завершённых задач -------------------------------------
    def test_09_analysis_settings_validate_and_estimate(self):
        view = recipe_agent.settings_view(self.cfg, ROOM)
        # W31-FIX3: напоминание о разборе по умолчанию ВЫКЛЮЧЕНО (токены без «да» нельзя).
        self.assertFalse(view["enabled"], "по умолчанию разбор выключен")
        self.assertEqual(recipe_agent.DEFAULT_SETTINGS["enabled"], False)
        self.assertEqual(view["every_days"], 7)
        self.assertEqual(view["lead_model"]["model"], "model-lead", "по умолчанию — модель руководителя")
        with self.assertRaises(recipe_agent.RecipeAgentError):
            recipe_agent.set_settings(self.cfg, ROOM, {"every_days": 0})
        with self.assertRaises(recipe_agent.RecipeAgentError):
            recipe_agent.set_settings(self.cfg, ROOM, {"every_days": 365})
        changed = recipe_agent.set_settings(self.cfg, ROOM, {"every_days": 3, "min_tasks": 2})
        self.assertEqual(changed["every_days"], 3)
        self.assertEqual(changed["min_tasks"], 2)
        est = recipe_agent.estimate(self.cfg, ROOM)
        self.assertGreater(est["input_tokens"], 0)
        self.assertGreater(est["output_tokens"], 0)
        self.assertIn("токенов", est["text"])

    def test_10_analysis_run_offers_and_filters_duplicates(self):
        for index in range(3):
            self._done_task("Претензия клиента №%d" % (index + 1),
                            "Клиент недоволен заказом %d, разбирали вручную" % (index + 1))
        started = recipe_agent.run(self.cfg, ROOM)
        self.assertTrue(started["ok"])
        card = self._card(started["card_id"])
        self.assertEqual(card["assignee"], "team-director")
        self.assertIn(recipe_agent.OFFER_MARK, card["body"])
        self.assertIn("УЖЕ ЕСТЬ РЕЦЕПТЫ", card["body"], "дубли отсекаются по названиям и шагам")
        self.assertIn("Претензия клиента №1", card["body"])

        existing_name = [i["name"] for i in recipes.listing(self.cfg, ROOM)["items"] if i["builtin"]][0]
        self._lead_says(started["card_id"], offers=True, payload={"offers": [
            {"name": existing_name, "why": "похоже на уже существующий рецепт",
             "tasks": ["Претензия клиента №1"], "fields": [], "steps": []},
            {"name": "Ответ на претензию клиента", "why": "три задачи одного вида",
             "tasks": ["Претензия клиента №1", "Претензия клиента №2"],
             "fields": [{"id": "f1", "label": "Клиент и заказ", "kind": "text", "required": True}],
             "steps": [{"id": "s1", "role": "юрист", "title": "Принять претензию в работу",
                        "instruction": "", "depends_on": []},
                       {"id": "s2", "role": "руководитель", "title": "Отправить ответ клиенту",
                        "instruction": "", "critic": True, "depends_on": ["s1"]}]}]})
        synced = recipe_agent.sync_offers(self.cfg, ROOM)
        self.assertEqual(synced["added"], 1, synced)
        self.assertTrue(any("уже есть рецепт" in s["reason"] for s in synced["suppressed"]))
        new_offers = [o for o in synced["offers"] if o["state"] == "new"]
        self.assertEqual(len(new_offers), 1)
        self.assertEqual(new_offers[0]["name"], "Ответ на претензию клиента")
        self.assertEqual(len(new_offers[0]["tasks"]), 2)
        # повторный разбор того же комментария ничего не добавляет
        again = recipe_agent.sync_offers(self.cfg, ROOM)
        self.assertEqual(again["added"], 0)

    def test_11_dismiss_is_remembered_and_accept_makes_recipe(self):
        view = recipe_agent.analysis_view(self.cfg, ROOM)
        offers = [o for o in view["offers"] if o["state"] == "new"]
        self.assertTrue(offers)
        dismissed = recipe_agent.dismiss(self.cfg, ROOM, offers[0]["id"])
        self.assertEqual(dismissed["offer"]["state"], "dismissed")
        doc = recipe_agent._load_analysis(self.cfg, ROOM)
        self.assertTrue(doc["dismissed"], "отклонённое запомнено")

        # та же идея приходит снова — не показываем
        started = recipe_agent.run(self.cfg, ROOM, force=True)
        self._lead_says(started["card_id"], offers=True, payload={"offers": [
            {"name": offers[0]["name"], "why": "снова", "tasks": ["Претензия клиента №1"],
             "fields": offers[0]["fields"], "steps": offers[0]["steps"]}]})
        synced = recipe_agent.sync_offers(self.cfg, ROOM)
        self.assertEqual(synced["added"], 0)
        self.assertTrue(any("отклоняли" in s["reason"] for s in synced["suppressed"]))

    def test_12_accept_offer_creates_recipe_with_source(self):
        started = recipe_agent.run(self.cfg, ROOM, force=True)
        self._lead_says(started["card_id"], offers=True, payload={"offers": [
            {"name": "Сверка кассы за неделю", "why": "повторяется каждую пятницу",
             "tasks": ["Претензия клиента №1", "Претензия клиента №2"],
             "fields": [{"id": "f1", "label": "Неделя", "kind": "due", "required": True}],
             "steps": [{"id": "s1", "role": "оператор", "title": "Сойтись с кассой",
                        "instruction": "", "depends_on": []},
                       {"id": "s2", "role": "руководитель", "title": "Свести итог",
                        "instruction": "", "critic": True, "depends_on": ["s1"]}]}]})
        synced = recipe_agent.sync_offers(self.cfg, ROOM)
        offer = [o for o in synced["offers"] if o["name"] == "Сверка кассы за неделю"][0]
        made = recipe_agent.accept_offer(self.cfg, ROOM, offer["id"])
        self.assertEqual(made["recipe"]["source"], "analysis")
        self.assertEqual(len(made["offer"]["steps"]), 2)
        self.assertEqual(made["offer"]["state"], "created")
        with self.assertRaises(recipe_agent.RecipeAgentError) as ctx:
            recipe_agent.accept_offer(self.cfg, ROOM, offer["id"])
        self.assertEqual(ctx.exception.http, 409)

    def test_13_tick_marks_due_and_never_creates_card(self):
        """W31-FIX3: сторож только помечает «пора» — карточку ставит человек («Запустить»).

        Проверяем на подменённом движке: если бы тик создавал карточку, подмена это
        увидела бы. Ни одного вызова — ни ``create_subtask_request``, ни ``_analysis_card``.
        """
        recipe_agent.set_settings(self.cfg, ROOM, {"enabled": True, "every_days": 3, "min_tasks": 2})
        doc = recipe_agent._load_analysis(self.cfg, ROOM)
        doc.update({"last_run_at": int(time.time()), "last_card_id": "", "due_at": 0,
                    "due_count": 0, "due_estimate": {}, "snoozed_until": 0})
        recipe_agent._save_analysis(self.cfg, ROOM, doc)
        before = int(self.sb.one("SELECT count(*) AS n FROM tasks")["n"])

        calls = []
        saved_engine, saved_card = recipe_agent._engine, recipe_agent._analysis_card
        recipe_agent._engine = lambda: types.SimpleNamespace(
            create_subtask_request=lambda *a, **kw: (calls.append(a), {"root_id": "t_never"})[1])
        recipe_agent._analysis_card = lambda *a, **kw: (calls.append(a), "t_never")[1]
        try:
            first = recipe_agent.tick(self.cfg, ROOM)
            self.assertFalse(first["due"], "раз в N дней — не чаще")

            doc = recipe_agent._load_analysis(self.cfg, ROOM)
            doc["last_run_at"] = int(time.time()) - 4 * 86400
            recipe_agent._save_analysis(self.cfg, ROOM, doc)
            result = recipe_agent.tick(self.cfg, ROOM)
            self.assertTrue(result["due"], result)
            self.assertFalse(result["offered"], "сторож ничего не предлагает и не создаёт")
            self.assertGreaterEqual(int(result["tasks"]), 2)
            self.assertIn("Пора провести разбор задач", str(result["text"]))
            self.assertEqual([], calls, "карточка создана без действия человека")
            self.assertEqual(int(self.sb.one("SELECT count(*) AS n FROM tasks")["n"]), before,
                             "тик не создал ни одной карточки")

            # Пометка уже стоит: повторный тик ничего не пересчитывает и не создаёт.
            again = recipe_agent.tick(self.cfg, ROOM)
            self.assertTrue(again["due"])
            self.assertEqual([], calls, "повторный тик создал карточку")
            view = recipe_agent.analysis_view(self.cfg, ROOM)
            self.assertTrue(view["prompt"]["show"], "вкладка видит пометку")
            self.assertEqual(view["prompt"]["count"], int(result["tasks"]))
            self.assertIn("Пора провести разбор задач", view["prompt"]["text"])
            self.assertIn("примерно", view["prompt"]["text"])
            self.assertIn("токенов", view["prompt"]["text"])
        finally:
            recipe_agent._engine, recipe_agent._analysis_card = saved_engine, saved_card

    def test_14_parse_reply_accepts_prose_and_rejects_foreign_marks(self):
        self.assertIsNone(recipe_agent.parse_reply("просто комментарий без метки"))
        prose = recipe_agent.parse_reply(recipe_agent.MARK + " Сделаю рецепт, но вопросов нет")
        self.assertEqual(prose["stage"], "questions")
        self.assertTrue(prose["problems"])
        draft = recipe_agent.parse_reply(_reply({"stage": "draft", "recipe": DRAFT}))
        self.assertEqual(draft["stage"], "draft")
        self.assertEqual(draft["draft"]["name"], DRAFT["name"])

    # --- 5. W31-FIX3: «предложить, а не запустить» -----------------------
    def test_15_default_is_off_and_tick_is_silent(self):
        """Новая комната: напоминание выключено, тик молчит и доски не читает."""
        fresh = {"id": "freshroom", "title": "Новая комната", "board": "team",
                 "tenant": "room:freshroom", "orchestrator": "team-director", "members": []}
        self.assertFalse(recipe_agent.settings_view(self.cfg, fresh)["enabled"],
                         "по умолчанию напоминание о разборе выключено")
        result = recipe_agent.tick(self.cfg, fresh)
        self.assertFalse(result["due"])
        self.assertFalse(result["offered"])
        self.assertEqual(result["why"], "выключено")
        self.assertFalse(recipe_agent.analysis_view(self.cfg, fresh)["prompt"]["show"],
                         "без включения плашки нет")

    def test_16_run_creates_card_only_by_human_and_later_snoozes(self):
        """Карточку ставит только «Запустить» (run); «Позже» откладывает на N дней."""
        recipe_agent.set_settings(self.cfg, ROOM, {"enabled": True, "every_days": 3, "min_tasks": 2})
        now = int(time.time())
        doc = recipe_agent._load_analysis(self.cfg, ROOM)
        doc.update({"due_at": now, "due_count": 4,
                    "due_estimate": {"input": 4200, "output": 900, "tasks": 4},
                    "snoozed_until": 0, "last_card_id": "", "last_run_at": now - 4 * 86400})
        recipe_agent._save_analysis(self.cfg, ROOM, doc)
        view = recipe_agent.analysis_view(self.cfg, ROOM)
        self.assertTrue(view["prompt"]["show"])
        self.assertEqual(view["prompt"]["count"], 4)
        self.assertEqual(view["prompt"]["tokens"], 4200)
        self.assertIn("4,2 тыс.", view["prompt"]["text"])

        started = recipe_agent.run(self.cfg, ROOM, force=True)
        self.assertTrue(started["card_id"].startswith("t_"))
        self.assertFalse(recipe_agent.analysis_view(self.cfg, ROOM)["prompt"]["show"],
                         "пометка снята: человек уже нажал «Запустить»")

        doc = recipe_agent._load_analysis(self.cfg, ROOM)
        doc.update({"due_at": now, "due_count": 2, "due_estimate": {"input": 1000, "output": 900}})
        recipe_agent._save_analysis(self.cfg, ROOM, doc)
        later = recipe_agent.later(self.cfg, ROOM)
        self.assertEqual(later["days"], 3, "«Позже» — на столько же дней, как в настройках")
        self.assertGreater(later["snoozed_until"], now)
        self.assertIn("Отложили", later["message"])
        self.assertFalse(recipe_agent.analysis_view(self.cfg, ROOM)["prompt"]["show"])
        self.assertEqual(recipe_agent.tick(self.cfg, ROOM)["why"], "отложено",
                         "после «Позже» не напоминаем")
        recipe_agent.set_settings(self.cfg, ROOM, {"enabled": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
