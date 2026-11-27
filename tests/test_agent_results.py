# -*- coding: utf-8 -*-
"""W30-E — «Результаты агентов»: сборка из дерева карточек и ручка API.

Что проверяем и почему
----------------------
Дефект 16: в задаче не видно, кто из агентов что изучил/сделал, какой вывод, где
лежит файл и что осталось — всё тонуло в ленте. Блок собирается БЕЗ вызовов
моделей, только из дерева и метаданных карточек, поэтому тест держит именно
правила сборки:

* вывод 2–3 строки из `summary_md` — без markdown, но с сохранёнными именами
  файлов (`02_legal.md` не должно превращаться в «02legal.md»);
* «что осталось» — из явных ключей метаданных (`not_done`, `next_steps`, …),
  первый непустой, не больше двух пунктов + «и ещё N»;
* файлы — с полным путём и проверкой существования (только чтение), зеркала
  одного файла схлопываются, похожие на секреты не показываются;
* роль и статус — по-русски; порядок — как в дереве;
* пустая/битая карточка не рушит блок; потолок агентов считается;
* ключ `agent_results` кладётся в деталь задачи, а маршрут
  `GET /api/requests/{id}/agent-results` объявлен (и `?fields=agent_results`
  допустим в детали).

Данные вымышленные (химчистка, кофейня). Модели не вызываются, доска не
трогается.

    python -m unittest tests.test_agent_results -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import agent_results as ar  # noqa: E402


def node(**kwargs) -> dict:
    base = {"id": "t_00000001", "assignee": "team-a", "role": "worker", "status": "done",
            "title": "Проверить договор аренды", "summary_md": "", "metadata": {}}
    base.update(kwargs)
    return base


class TestVerdict(unittest.TestCase):
    """Вывод карточки — 2–3 строки без разметки."""

    def test_markdown_removed_and_filename_kept(self):
        text = ar.plain("**Коротко.** Файл `02_legal.md` собран. См. [отчёт](C:/x/y.md).")
        self.assertIn("02_legal.md", text)          # имя файла не разобрано как курсив
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertIn("отчёт", text)                # ссылка → текст
        self.assertNotIn("C:/x/y.md", text)

    def test_verdict_is_short_but_not_a_stub(self):
        # одно длинное предложение без точек — обрезаем по пределу, с «…»
        long = " ".join(["слово"] * 200)
        got = ar.verdict_of(long)
        self.assertLessEqual(len(got), ar.VERDICT_CHARS + 1)
        self.assertTrue(got.endswith("…"), got[-20:])
        # 2–3 предложения — не больше: четвёртое уже не берём
        many = "Первое. Второе. Третье. Четвёртое. Пятое."
        self.assertEqual(ar.verdict_of(many), "Первое. Второе. Третье.")

    def test_verdict_empty_when_no_summary(self):
        self.assertEqual(ar.verdict_of(""), "")
        self.assertEqual(ar.verdict_of(None), "")


class TestRemaining(unittest.TestCase):
    """«Что осталось» — из метаданных, по приоритету, без простыни."""

    def test_string_list_with_limit(self):
        got = ar.remaining_of({"next_steps": ["Первое", "Второе", "Третье", "Четвёртое"]})
        self.assertIn("Первое", got)
        self.assertIn("Второе", got)
        self.assertIn("и ещё 2", got)
        self.assertNotIn("Четвёртое", got)

    def test_priority_not_done_first(self):
        got = ar.remaining_of({"not_done": ["Котировки не отправлены"],
                               "next_steps": ["Выбрать канал"]})
        self.assertIn("Котировки не отправлены", got)
        self.assertNotIn("Выбрать канал", got)

    def test_dict_items_and_counts_ignored(self):
        got = ar.remaining_of({"open_questions": [{"text": "Кому писать?"}],
                               "gaps": 3})
        self.assertIn("Кому писать?", got)
        # счётчик-число не превращается в пункт
        self.assertEqual(ar.remaining_of({"gaps": 3, "todo": 0}), "")

    def test_nothing_left_is_empty(self):
        self.assertEqual(ar.remaining_of({}), "")
        self.assertEqual(ar.remaining_of(None), "")

    def test_machine_questions_are_rewritten(self):
        """W31-A2: «что осталось» из open_questions — те же правила, что у задачи."""
        got = ar.remaining_of({"open_questions": [
            "Q3. Доставка: А) платная; Б) доплачиваем. Что выбираем?",
            "D19 — отправляете 5–10 сообщений вручную или готовым текстом"]})
        self.assertNotIn("Q3", got)
        self.assertNotIn("D19", got)
        self.assertIn("Доставка", got)
        self.assertIn("Отправляете 5–10 сообщений вручную", got)
        # «что осталось» из шагов — как было, без переписывания
        self.assertEqual(ar.remaining_of({"next_steps": ["Подписать договор"]}), "Подписать договор")


class TestFiles(unittest.TestCase):
    """Файлы результата: путь, существование, зеркала, секреты."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.real = os.path.join(self.dir, "01-legal.md")
        with open(self.real, "w", encoding="utf-8") as handle:
            handle.write("# вымышленный файл\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exists_flag_is_honest(self):
        got = ar.files_of({"artifacts": [self.real, os.path.join(self.dir, "нет.md")]})
        by_name = {item["name"]: item for item in got["files"]}
        self.assertTrue(by_name["01-legal.md"]["exists"])
        self.assertFalse(by_name["нет.md"]["exists"])
        self.assertEqual(got["total"], 2)

    def test_mirrors_collapsed(self):
        mirror = os.path.join(self.dir, "mirror")
        os.makedirs(mirror, exist_ok=True)
        got = ar.files_of({"artifacts": [self.real, os.path.join(mirror, "01-legal.md")]})
        self.assertEqual(len(got["files"]), 1, "один файл — одна строка")
        self.assertEqual(got["mirrors"], 1)
        # остаётся самый короткий путь (папка задачи), а не зеркало
        self.assertEqual(got["files"][0]["path"], self.real)

    def test_secrets_hidden(self):
        got = ar.files_of({"artifacts": [self.real, os.path.join(self.dir, ".env"),
                                         os.path.join(self.dir, "google_token.json")]})
        names = [item["name"] for item in got["files"]]
        self.assertEqual(names, ["01-legal.md"])
        self.assertEqual(got["hidden"], 2, "секреты считаются, но не показываются")

    def test_dict_artifacts_and_cap(self):
        items = [{"path": os.path.join(self.dir, "f%d.md" % i), "title": "t"} for i in range(15)]
        got = ar.files_of({"artifacts": items})
        self.assertEqual(len(got["files"]), ar.MAX_FILES)
        self.assertEqual(got["total"], 15)

    def test_no_artifacts(self):
        self.assertEqual(ar.files_of({})["files"], [])


class TestAgentAndBuild(unittest.TestCase):
    """Строка агента и сборка всего блока."""

    def test_agent_fields(self):
        row = ar.agent_of(node(summary_md="Проверил договор: два пункта правятся.",
                               metadata={"verdict": "revise",
                                         "next_steps": ["Подписать"]}),
                          room={"role_labels": {"team-a": "юрист"}})
        for key in ("agent", "role", "title", "status", "verdict", "files", "remaining", "card_id"):
            self.assertIn(key, row)
        self.assertEqual(row["role"], "юрист", "подпись роли из комнаты")
        self.assertEqual(row["status_label"], "готово")
        self.assertEqual(row["verdict_tag"], "revise")
        self.assertEqual(row["card_id"], "t_00000001")
        self.assertIn("Подписать", row["remaining"])

    def test_role_fallback_russian(self):
        self.assertEqual(ar.agent_of(node(role="root"))["role"], "руководитель")
        self.assertEqual(ar.agent_of(node(role="critic"))["role"], "критик")
        self.assertEqual(ar.agent_of(node(role="synthesizer"))["role"], "свод")

    def test_build_keeps_tree_order_and_counts(self):
        nodes = [node(id="t_00000001", summary_md="Свод готов.", role="root"),
                 node(id="t_00000002", assignee="team-b", summary_md="Файл собран.",
                      metadata={"artifacts": ["C:/нет/файла.md"]}),
                 {}, None, "мусор"]
        got = ar.build(nodes)
        self.assertEqual([row["card_id"] for row in got["agents"]], ["t_00000001", "t_00000002"])
        self.assertEqual(got["counts"]["agents"], 2)
        self.assertEqual(got["counts"]["agents_with_files"], 1)
        self.assertEqual(got["counts"]["remaining"], 0)

    def test_agent_cap(self):
        nodes = [node(id="t_%08d" % i) for i in range(ar.MAX_AGENTS + 5)]
        got = ar.build(nodes)
        self.assertEqual(len(got["agents"]), ar.MAX_AGENTS)
        self.assertEqual(got["counts"]["hidden_agents"], 5)

    def test_broken_card_does_not_break_block(self):
        class Weird(dict):
            pass

        bad = Weird({"id": "t_00000009", "assignee": "team-x", "metadata": {"artifacts": 42}})
        got = ar.build([node(id="t_00000001", summary_md="Ок."), bad])
        self.assertEqual(len(got["agents"]), 2)
        self.assertEqual(got["agents"][1]["files"], [])

    def test_attach_puts_same_payload(self):
        detail = {"id": "t_00000001", "tree": [node(summary_md="Готово.")]}
        payload = ar.attach(detail)
        self.assertIs(detail["agent_results"], payload)
        self.assertEqual(payload["agents"][0]["verdict"], "Готово.")

    def test_attach_without_tree(self):
        detail = {"id": "t_00000001"}
        self.assertEqual(ar.attach(detail)["agents"], [])


class TestAgentName(unittest.TestCase):
    """Дефект 31: в «Результатах агентов» видно, КТО работал, а не «исполнитель-воркер»."""

    ROOM = {
        "orchestrator": "team-lead",
        "critic": "team-critic",
        "members": [{"profile": "team-a", "role": "worker", "label": "Анна — юрист"}],
        "role_labels": {"team-lead": "Вера — руководитель"},
    }

    def test_name_comes_from_team_tab_label(self):
        row = ar.agent_of(node(assignee="team-a"), room=self.ROOM)
        self.assertEqual(row["agent_name"], "Анна", "имя из подписи участника, без должности")
        self.assertEqual(row["agent_role"], "исполнитель")

    def test_lead_and_critic_have_their_own_role(self):
        lead = ar.agent_of(node(assignee="team-lead", role="root"), room=self.ROOM)
        critic = ar.agent_of(node(assignee="team-critic", role="critic"), room=self.ROOM)
        self.assertEqual((lead["agent_name"], lead["agent_role"]), ("Вера", "руководитель"))
        self.assertEqual((critic["agent_name"], critic["agent_role"]), ("team-critic", "критик"))

    def test_name_falls_back_to_profile_description(self):
        row = ar.agent_of(node(assignee="team-b"), room=self.ROOM,
                          descriptions={"team-b": "Борис — исследователь"})
        self.assertEqual(row["agent_name"], "Борис")
        self.assertEqual(row["agent_role"], "исполнитель")

    def test_name_never_empty(self):
        row = ar.agent_of(node(assignee="team-z"), room={})
        self.assertEqual(row["agent_name"], "team-z", "техническое имя лучше пустой строки")
        self.assertEqual(ar.agent_of(node(assignee=""))["agent_name"], "Свод задачи")

    def test_role_uses_card_when_room_is_silent(self):
        self.assertEqual(ar.agent_of(node(assignee="x", role="critic"), room={})["agent_role"],
                         "критик")
        self.assertEqual(ar.agent_of(node(assignee="x", role="worker"), room={})["agent_role"],
                         "исполнитель")

    def test_counts_list_people_for_the_filter(self):
        nodes = [node(id="t_00000001", assignee="team-a"), node(id="t_00000002", assignee="team-a"),
                 node(id="t_00000003", assignee="team-b")]
        got = ar.build(nodes, room=self.ROOM, descriptions={"team-b": "Борис — исследователь"})
        self.assertEqual(got["counts"]["people"], ["Анна", "Борис"])

    def test_handler_passes_descriptions(self):
        from server import routes_agent_results as mod

        class Stub(mod.AgentResultsRoutes):
            def __init__(self):
                self.log = type("L", (), {"error": lambda *a: None, "warning": lambda *a: None})()

            def _find_record(self, rid):
                return {"id": "clean", "members": []}, {"root_id": rid}

            def _detail(self, room, rid, extra=None, critic=None, answers=None):
                return {"id": rid, "tree": [node(id="t_aa000001", assignee="team-b")]}

            def _profile_descriptions(self):
                return {"team-b": "Борис — исследователь"}

            def _has_mod(self, name):
                return True

            def mod(self, name):
                return ar

        status, payload, _ = Stub().h_agent_results({}, None, "t_aa000001")
        self.assertEqual(status, 200)
        self.assertEqual(payload["agents"][0]["agent_name"], "Борис")


class TestRoute(unittest.TestCase):
    """Маршрут объявлен, шаблон ловит только id задачи, поля детали допускают блок."""

    def test_route_declared(self):
        from server import routes_agent_results as mod
        names = [handler for _method, _pattern, handler in mod.ROUTES]
        self.assertIn("h_agent_results", names)
        pattern = mod.ROUTES[0][1]
        import re
        rx = re.compile(pattern)
        self.assertTrue(rx.fullmatch("/api/requests/t_bbd46401/agent-results"))
        self.assertFalse(rx.fullmatch("/api/requests/t_bbd4640/agent-results"))
        self.assertFalse(rx.fullmatch("/api/requests/t_bbd46401/other"))

    def test_install_is_idempotent(self):
        from server import routes_agent_results as mod
        routes = []
        mod.install_agent_results_routes(type("App", (), {}), routes)
        mod.install_agent_results_routes(type("App", (), {}), routes)
        self.assertEqual(len(routes), len(mod.ROUTES), "маршрут не дублируется")

    def test_detail_fields_allow_block(self):
        from server import routes_read
        self.assertEqual(routes_read.DETAIL_FIELDS.get("agent_results"), "agent_results")

    def test_handler_returns_payload(self):
        """Хендлер: 200, `{agents, counts}`, заголовок no-cache — без движка и сети."""
        from server import routes_agent_results as mod

        class Stub(mod.AgentResultsRoutes):
            def __init__(self):
                self.log = type("L", (), {"error": lambda *a: None, "warning": lambda *a: None})()

            def _find_record(self, rid):
                return {"id": "clean"}, {"root_id": rid}

            def _detail(self, room, rid, extra=None, critic=None, answers=None):
                return {"id": rid, "room": "clean",
                        "tree": [node(id="t_aa000001", summary_md="Файл собран.",
                                      metadata={"artifacts": ["C:/нет/файла.md"]})]}

            def _has_mod(self, name):
                return True

            def mod(self, name):
                return ar

        status, payload, headers = Stub().h_agent_results({}, None, "t_aa000001")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        self.assertEqual(payload["agents"][0]["card_id"], "t_aa000001")
        self.assertEqual(payload["agents"][0]["files"][0]["exists"], False)


if __name__ == "__main__":
    unittest.main()
