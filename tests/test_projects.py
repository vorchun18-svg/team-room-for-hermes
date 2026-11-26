# -*- coding: utf-8 -*-
"""Предложение задач и проект «задача → подзадачи» (W30-B, контракт §2).

Проверяем на ПЕСОЧНИЦЕ (своя доска, поддельный дом Hermes, реальные файлы Hermes
только читаются):

* разбор комментария руководителя `[Руководитель · оценка задачи]` — разовая и
  объёмная задача, ветки, устойчивый id, идемпотентность повторного чтения;
* решения человека по ветке/разовой задаче (в т.ч. отказы с причиной по-русски);
* применение: ОТДЕЛЬНЫЕ задачи-корни (без связи доски с родителем), связь в
  `projects.json`, критик наследуется, тело «Подзадача проекта <родитель>: <цель>»;
* повторное «применить» не создаёт вторую задачу;
* отказ `board_guard` в песочнице с реальной доской;
* `proposal`/`project` в деталях, «Итоги подзадач» для свода родителя,
  действие «Разбить на задачи» (карточка руководителю, идемпотентно);
* маршруты подключены без дублей и текст правила попал в устав руководителя.

    python -m unittest tests.test_projects -v

Данные вымышленные (химчистка, кофейня). Модели не запускаются, на боевую доску
ничего не пишется (`board_guard`).
"""
from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                     # noqa: E402
import server.contract as contract_mod           # noqa: E402
import server.contract_project as rule_mod       # noqa: E402
import server.projects as projects               # noqa: E402
import server.registry as registry_mod           # noqa: E402
from tests.helpers import Sandbox, SimAgents     # noqa: E402

ROOM = {
    "id": "laundry",
    "title": "Химчистка на углу",
    "board": "team",
    "tenant": "room:laundry",
    "orchestrator": "team-director",
    "critic": "team-critic",
    "critic_pass": True,
    "members": [
        {"profile": "team-director", "role": "orchestrator", "note": "Руководитель комнаты"},
        {"profile": "team-ops", "role": "worker", "note": "Оператор: помещение и оборудование"},
        {"profile": "team-cfo", "role": "worker", "note": "Финансист: цена и окупаемость"},
        {"profile": "team-lawyer", "role": "worker", "note": "Юрист: договоры и персонал"},
        {"profile": "team-critic", "role": "critic", "note": "Критик"},
    ],
    "agent_models": {
        "team-ops": {"provider": "ollama-cloud", "model": "model-ops"},
        "team-cfo": {"provider": "ollama-cloud", "model": "model-cfo"},
        "team-lawyer": {"provider": "ollama-cloud", "model": "model-lawyer"},
        "team-director": {"provider": "ollama-cloud", "model": "model-lead"},
    },
}

BRANCHES = [
    {"id": "b1", "title": "Помещение", "goal": "Найти помещение под второй пункт приёма",
     "assignee": "team-ops", "questions": ["Какая площадь нужна?"], "order": 1},
    {"id": "b2", "title": "Оборудование", "goal": "Подобрать машины и посчитать цену",
     "assignee": "team-cfo", "questions": [], "order": 2},
    {"id": "b3", "title": "Персонал", "goal": "Кто работает на точке и на каких условиях",
     "assignee": "team-lawyer", "questions": ["Нужен ли второй сменщик?"], "order": 3},
]

BIG_COMMENT = ("[Руководитель · оценка задачи]\n"
               "Задача объёмная: открываем второй пункт приёма, направления разные.\n"
               "```json\n%s\n```" % json.dumps({"task_kind": "big",
                                               "summary": "Второй пункт приёма: три направления.",
                                               "branches": BRANCHES}, ensure_ascii=False))
SINGLE_COMMENT = ('[Руководитель · оценка задачи]\n'
                  'Задача разовая — сделаем одной задачей.\n'
                  '{"task_kind":"single","summary":"Проверить один договор аренды."}')


class ProjectsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        cls.sim = SimAgents(cls.sb)
        cls.real_before = cls.sb.snapshot_real()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls._stack.close()

    # --- помощники -------------------------------------------------------
    def _root(self, title: str = "Открыть второй пункт приёма", critic: bool = True) -> str:
        root_id = self.sim.card(title, "team-director", status="todo", tenant="room:laundry",
                                body="Открыть второй пункт приёма химчистки")
        registry_mod.add_request(self.cfg, "laundry", {
            "root_id": root_id, "slug": "r-test-%s" % root_id[2:], "title": title,
            "text": "Открыть второй пункт приёма химчистки", "created_at": 1,
            "critic": bool(critic), "critic_mode": "rounds", "critic_round_cap": 2,
            "mode": "auto", "extra_cards": [],
        })
        return root_id

    def _rec(self, root_id: str) -> dict:
        rec = registry_mod.get(self.cfg, "laundry", root_id)
        self.assertIsNotNone(rec)
        return rec

    def _board_rows(self, sql: str, params: tuple = ()) -> list:
        return self.sb.rows(sql, params)

    def _children_on_board(self, root_id: str) -> list:
        """Карточки, связанные с родителем ЧЕРЕЗ ДОСКУ (их быть не должно)."""
        return self._board_rows("SELECT child_id FROM task_links WHERE parent_id=?", (root_id,))

    def _task(self, task_id: str):
        return self.sb.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    # --- разбор комментария ---------------------------------------------
    def test_01_parse_big_comment(self):
        draft = projects.parse_comment(BIG_COMMENT)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["kind"], "big")
        self.assertEqual(len(draft["branches"]), 3)
        self.assertIn("второй пункт", draft["summary"].lower())

    def test_02_parse_single_comment_and_foreign_text(self):
        single = projects.parse_comment(SINGLE_COMMENT)
        self.assertEqual(single["kind"], "single")
        self.assertEqual(single["branches"], [])
        self.assertEqual(single["summary"], "Проверить один договор аренды.")
        self.assertIsNone(projects.parse_comment("Михаил, посмотри договор аренды."))
        self.assertIsNone(projects.parse_comment(""))

    def test_03_sync_is_idempotent_and_keeps_decisions(self):
        root_id = self._root("Синхронизация предложения")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        first = projects.sync(self.cfg, ROOM, root_id)
        self.assertIsNotNone(first)
        self.assertEqual(first["kind"], "big")
        self.assertTrue(projects.ready(first))
        path = projects.proposal_path(self.cfg, "laundry", root_id)
        self.assertTrue(path.is_file())
        self.assertTrue(first["id"].startswith("pr_"))

        decided = projects.decide(self.cfg, ROOM, root_id, "b2", "yes")
        self.assertEqual(decided["proposal"]["branches"][1]["decision"], "yes")

        # повторное чтение того же комментария: тот же id, решение человека цело
        again = projects.sync(self.cfg, ROOM, root_id)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(again["branches"]), 3)
        self.assertEqual(again["branches"][1]["decision"], "yes")
        stored = json.loads(path.read_text("utf-8"))
        self.assertEqual(stored["branches"][1]["decision"], "yes")

    # --- решения человека ------------------------------------------------
    def test_04_single_decision_states(self):
        root_id = self._root("Разовая задача")
        self.sim.comment(root_id, "team-director", SINGLE_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.decide(self.cfg, ROOM, root_id, None, "custom", "")
        self.assertEqual(ctx.exception.http, 400)
        self.assertIn("напишите", ctx.exception.message.lower())
        agreed = projects.decide(self.cfg, ROOM, root_id, None, "agree", "")
        self.assertEqual(agreed["proposal"]["state"], "approved")
        self.assertEqual(agreed["proposal"]["single"]["decision"], "agree")

    def test_05_branch_decision_refusals_are_russian(self):
        root_id = self._root("Проверка отказов")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.decide(self.cfg, ROOM, root_id, None, "yes")
        self.assertEqual(ctx.exception.code, "branch_required")
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.decide(self.cfg, ROOM, root_id, "b9", "yes")
        self.assertEqual(ctx.exception.code, "branch_not_found")
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.decide(self.cfg, ROOM, root_id, "b1", "custom", "")
        self.assertEqual(ctx.exception.code, "text_required")
        no_proposal = self._root("Без предложения")
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.resolve(self.cfg, ROOM, self._rec(no_proposal), no_proposal, "apply")
        self.assertEqual(ctx.exception.code, "no_proposal")
        self.assertEqual(ctx.exception.http, 409)

    def test_06_apply_without_approved_branches_is_refused(self):
        root_id = self._root("Нечего применять")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        self.assertEqual(ctx.exception.code, "nothing_to_apply")
        self.assertIn("Да", ctx.exception.message)

    # --- применение: отдельные задачи-корни -------------------------------
    def test_07_apply_creates_separate_root_tasks(self):
        root_id = self._root("Второй пункт приёма (применение)")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        accepted = projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "accept_all")
        self.assertEqual(accepted["proposal"]["state"], "approved")
        self.assertEqual([b["decision"] for b in accepted["proposal"]["branches"]],
                         ["yes", "yes", "yes"])

        applied = projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        self.assertEqual(len(applied["created"]), 3, applied)
        proposal = applied["proposal"]
        self.assertEqual(proposal["state"], "applied")
        self.assertEqual(proposal["resolution"]["action"], "accept_all")
        tasks = []
        for branch in proposal["branches"]:
            self.assertTrue(branch["task_id"], branch)
            row = self._task(branch["task_id"])
            self.assertIsNotNone(row, "задача ветки не создана: %s" % branch["task_id"])
            tasks.append(row)
            self.assertEqual(row["tenant"], "room:laundry")
            self.assertEqual(row["assignee"], branch["assignee"])
            self.assertIn("Подзадача проекта", row["body"])
            self.assertIn(branch["goal"], row["body"])
            self.assertNotEqual(row["status"], "triage")
        # связь «родитель→подзадачи» — в projects.json, НЕ в связях доски
        self.assertEqual(self._children_on_board(root_id), [],
                         "подзадача не должна быть дочерней карточкой родителя")
        data = projects.load_projects(self.cfg, "laundry")
        self.assertEqual(len(data[root_id]["children"]), 3)
        for branch in proposal["branches"]:
            self.assertEqual(data[branch["task_id"]]["parent"], root_id)
        # критик наследуется, запись реестра есть у каждой подзадачи
        for branch in proposal["branches"]:
            rec = registry_mod.get(self.cfg, "laundry", branch["task_id"])
            self.assertIsNotNone(rec, "подзадача не записана в реестр комнаты")
            self.assertTrue(rec["critic"])
            self.assertEqual(rec["critic_round_cap"], 2)
            self.assertEqual(rec["kind"], "subtask")

    def test_08_second_apply_does_not_duplicate(self):
        root_id = self._root("Повторное применение")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "accept_all")
        first = projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        before = len(self._board_rows("SELECT id FROM tasks"))
        second = projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        after = len(self._board_rows("SELECT id FROM tasks"))
        self.assertTrue(second["duplicate"])
        self.assertEqual(before, after, "повторное применение создало карточки")
        # повтор отдаёт ссылки на уже созданные задачи, а не «создано заново»
        self.assertEqual(sorted(b["task_id"] for b in first["created"]),
                         sorted(b["task_id"] for b in second["created"]))
        self.assertTrue(all(b["task_id"] for b in second["proposal"]["branches"]))

    def test_09_custom_branch_text_goes_into_task_body(self):
        root_id = self._root("Свой вариант ветки")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        projects.decide(self.cfg, ROOM, root_id, "b1", "custom", "Искать только первый этаж")
        projects.decide(self.cfg, ROOM, root_id, "b2", "no")
        applied = projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        self.assertEqual(len(applied["created"]), 1, applied)
        self.assertEqual(applied["proposal"]["resolution"]["action"], "partial")
        child = self._task(applied["created"][0]["task_id"])
        self.assertIn("Уточнение Михаила: Искать только первый этаж", child["body"])
        skipped = [b for b in applied["proposal"]["branches"] if b["decision"] == "no"]
        self.assertIsNone(skipped[0]["task_id"])

    def test_10_board_guard_blocks_real_board(self):
        root_id = self._root("Песочница и реальная доска")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "accept_all")
        from server import board_guard
        original = board_guard.check
        board_guard.check = lambda cfg=None, board=None, db=None: {
            "code": "sandbox_real_board", "message": "Запись в доску отклонена: песочница.",
            "detail": {"reason": "sandbox_real_board"}}
        try:
            with self.assertRaises(projects.ProjectError) as ctx:
                projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        finally:
            board_guard.check = original
        self.assertEqual(ctx.exception.code, "sandbox_real_board")
        self.assertEqual(ctx.exception.http, 409)
        self.assertIn("песочниц", ctx.exception.message)
        # ни одной задачи по веткам не появилось
        self.assertTrue(all(not b["task_id"] for b in
                            projects.get(self.cfg, ROOM, root_id)["branches"]))

    # --- чтение для страницы и свода -------------------------------------
    def test_11_view_and_attach(self):
        root_id = self._root("Вид проекта")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "accept_all")
        applied = projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        view = projects.view(self.cfg, ROOM, root_id)
        self.assertIsNone(view["parent_id"])
        self.assertEqual(len(view["children"]), 3)
        child_id = applied["created"][0]["task_id"]
        self.assertEqual([c["id"] for c in view["children"]][0], child_id)
        self.assertTrue(all(c["stage"] in ("new", "working", "needs_you", "review",
                                           "done_system", "archived") for c in view["children"]))
        child_view = projects.view(self.cfg, ROOM, child_id)
        self.assertEqual(child_view["parent_id"], root_id)

        detail = {"id": root_id, "thread": []}
        projects.attach(self.cfg, ROOM, self._rec(root_id), detail)
        self.assertEqual(detail["proposal"]["state"], "applied")
        self.assertEqual(len(detail["project"]["children"]), 3)

    def test_12_child_results_for_parent_svod(self):
        root_id = self._root("Итоги подзадач")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)
        projects.sync(self.cfg, ROOM, root_id)
        projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "accept_all")
        applied = projects.resolve(self.cfg, ROOM, self._rec(root_id), root_id, "apply")
        child_id = applied["created"][0]["task_id"]
        self.sim.start(child_id)
        self.sim.finish(child_id, "Помещение найдено: 40 м², аренда 60 тыс. в месяц.",
                        {"svod": {"short": "Нашли помещение 40 м² за 60 тыс."}})
        text = projects.child_results_text(self.cfg, ROOM, root_id)
        self.assertIn("ИТОГИ ПОДЗАДАЧ", text)
        self.assertIn("Помещение", text)
        self.assertIn("готово", text)
        self.assertLessEqual(len(text), 1200)

    # --- «Разбить на задачи» ---------------------------------------------
    def test_13_split_creates_lead_card_once(self):
        root_id = self._root("Старая большая задача")
        first = projects.split(self.cfg, ROOM, self._rec(root_id), root_id)
        self.assertTrue(first["created"])
        card = self._task(first["task_id"])
        self.assertIsNotNone(card)
        self.assertEqual(card["assignee"], "team-director")
        self.assertIn("оценка задачи", card["body"])
        second = projects.split(self.cfg, ROOM, self._rec(root_id), root_id)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["task_id"], first["task_id"])
        same = self._board_rows("SELECT id FROM tasks WHERE title=?", (rule_mod.split_card_title(),))
        self.assertEqual(len(same), 1, "карточка «разбить» создана дважды")

    # --- устав руководителя и маршруты -----------------------------------
    def test_13a_split_without_profile_in_roster_is_409_not_5xx(self):
        """W30-FIX2: «профиля нет в ростере» — настройка комнаты, а не сбой движка.

        На песочнице без песочного ростера `team-*` действие отвечало 502 и человек
        видел «ошибка сервера» вместо причины. Теперь — 409 с профилем и подсказкой.
        """
        root_id = self._root("Старая большая задача с чужим профилем")
        class _RosterError(Exception):
            code = "bad_request"
            message_ru = "Профиля нет в ростере Hermes."

        class _Engine:
            @staticmethod
            def create_child_card(*args, **kwargs):
                raise _RosterError()

        with mock.patch.object(projects, "_engine", lambda: _Engine()):
            with self.assertRaises(projects.ProjectError) as ctx:
                projects.split(self.cfg, ROOM, self._rec(root_id), root_id)
        self.assertEqual(ctx.exception.http, 409, "настройка комнаты не 5xx")
        self.assertIn("ростере", str(ctx.exception))
        self.assertIn("team-director", str(ctx.exception))
        self.assertEqual(ctx.exception.detail.get("profile"), "team-director")

    def test_14_contract_rule_reaches_lead_card(self):
        block = contract_mod.build_contract_block(dict(ROOM), "request", self.cfg)
        self.assertIn("Оценка задачи", block)
        self.assertIn("[Руководитель · оценка задачи]", block)
        self.assertIn("Подзадачи создаёт ПРИЛОЖЕНИЕ", block)
        self.assertLessEqual(len(rule_mod.card_rule(ROOM)), 1200)

    def test_15_routes_registered_without_duplicates(self):
        pairs = [(method, pattern.pattern) for method, pattern, _handler in app_mod.ROUTES]
        self.assertEqual(len(pairs), len(set(pairs)), "дубли маршрутов")
        for path in ("/api/requests/(?P<rid>t_[A-Za-z0-9_]+)/proposal",
                     "/api/requests/(?P<rid>t_[A-Za-z0-9_]+)/proposal/decision",
                     "/api/requests/(?P<rid>t_[A-Za-z0-9_]+)/proposal/resolve"):
            self.assertIn(("GET", path) if path.endswith("/proposal") else ("POST", path), pairs)
        from server.app_common import ACTION_ALLOWED
        self.assertIn("split_to_tasks", ACTION_ALLOWED)
        self.assertTrue(hasattr(app_mod.App, "_project_view"))
        self.assertTrue(hasattr(app_mod.App, "_split_action"))


# ---------------------------------------------------------------- живой HTTP
PORT = 8854


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) != 0


class LiveProjectsHttpTest(unittest.TestCase):
    """Живой сервер песочницы (порт 8854): ручки предложения и ключи в деталях."""

    @classmethod
    def setUpClass(cls):
        if not _port_free(PORT):
            raise unittest.SkipTest("порт %d занят — живая проба не запускается" % PORT)
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.cfg.port = PORT
        cls.real_before = cls.sb.snapshot_real()
        cls.sim = SimAgents(cls.sb)
        import server.rooms as rooms_mod
        cls.room = rooms_mod.create_room(cls.cfg, {
            "title": "Химчистка на углу",
            "orchestrator": "team-director",
            "members": [{"profile": "team-ops", "role": "worker", "label": "Оператор"},
                        {"profile": "team-cfo", "role": "worker", "label": "Финансист"},
                        {"profile": "team-lawyer", "role": "worker", "label": "Юрист"}]})
        cls.room_id = str(cls.room.get("id"))
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=PORT)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                cls.http("GET", "/api/health")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:                     # noqa: BLE001
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    @classmethod
    def http(cls, method: str, path: str, payload=None) -> tuple:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                                         method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw}

    def _root(self, title: str) -> str:
        root_id = self.sim.card(title, "team-director", status="todo",
                                tenant="room:%s" % self.room_id,
                                body="Открыть второй пункт приёма химчистки")
        registry_mod.add_request(self.cfg, self.room_id, {
            "root_id": root_id, "slug": "r-live-%s" % root_id[2:], "title": title,
            "text": "Открыть второй пункт приёма химчистки", "created_at": 1,
            "critic": True, "critic_mode": "rounds", "critic_round_cap": 2, "mode": "auto",
            "extra_cards": [],
        })
        return root_id

    def test_01_proposal_routes_end_to_end(self):
        root_id = self._root("Живая проба предложения")
        self.sim.comment(root_id, "team-director", BIG_COMMENT)

        status, detail = self.http("GET", "/api/requests/%s" % root_id)
        self.assertEqual(status, 200, detail)
        self.assertIn("proposal", detail, "в деталях нет ключа proposal")
        self.assertIn("project", detail, "в деталях нет ключа project")
        self.assertEqual(detail["proposal"]["kind"], "big")
        self.assertEqual(len(detail["proposal"]["branches"]), 3)

        status, payload = self.http("GET", "/api/requests/%s/proposal" % root_id)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["proposal"]["kind"], "big")

        status, payload = self.http("POST", "/api/requests/%s/proposal/decision" % root_id,
                                    {"branch_id": "b3", "decision": "no"})
        self.assertEqual(status, 200, payload)
        status, payload = self.http("POST", "/api/requests/%s/proposal/decision" % root_id,
                                    {"branch_id": "zz", "decision": "yes"})
        self.assertEqual(status, 404, payload)
        self.assertIn("ветки", payload["error"]["message"].lower())

        status, payload = self.http("POST", "/api/requests/%s/proposal/resolve" % root_id,
                                    {"action": "apply"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "nothing_to_apply")

        status, payload = self.http("POST", "/api/requests/%s/proposal/resolve" % root_id,
                                    {"action": "accept_all"})
        self.assertEqual(status, 200, payload)
        # b3 человек уже отклонил — «принять всё» ставит да всем
        self.assertEqual([b["decision"] for b in payload["proposal"]["branches"]],
                         ["yes", "yes", "yes"])
        status, payload = self.http("POST", "/api/requests/%s/proposal/resolve" % root_id,
                                    {"action": "apply"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(payload["created"]), 3, payload)
        self.assertEqual(payload["proposal"]["state"], "applied")

        status, detail = self.http("GET", "/api/requests/%s" % root_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["project"]["children"]), 3)
        child = detail["project"]["children"][0]
        self.assertTrue(child["id"].startswith("t_"))
        self.assertIn(child["stage"], ("new", "working", "needs_you", "review",
                                       "done_system", "archived"))

        status, child_detail = self.http("GET", "/api/requests/%s" % child["id"])
        self.assertEqual(status, 200)
        self.assertEqual(child_detail["project"]["parent_id"], root_id)

    def test_02_split_action_over_http(self):
        root_id = self._root("Старая задача без предложения")
        status, payload = self.http("POST", "/api/requests/%s/actions" % root_id,
                                    {"action": "split_to_tasks"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["created"])
        again_status, again = self.http("POST", "/api/requests/%s/actions" % root_id,
                                        {"action": "split_to_tasks"})
        self.assertEqual(again_status, 200, again)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["task_id"], payload["task_id"])


if __name__ == "__main__":
    unittest.main()