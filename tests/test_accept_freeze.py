# -*- coding: utf-8 -*-
"""«Принята» = полная заморозка (W30-D1, контракт §3).

Главное, что проверяем поведением, а не текстом: после принятия задачи НИ ОДНА
петля работы не делает ни карточек, ни вызовов модели. Тест на каждом месте
исключения — рядом с `archived`, где он был:

* сторож (``watcher.tick_once``) — ни критика, ни свода, ни названий, ни ingest;
* живой свод (``svod_live.tick``) — «нечего делать»;
* цикл критика (``critic_loop.run_step``) — «нечего делать»;
* напоминания (``stale.decorate``) — пункты закрыты причиной ``request_accepted``;
* «Сегодня» и «Итог дня» — принятая задача не «ждёт вас»;
* уведомления — о принятой задаче не сообщаем;
* отчёт диспетчера — карточки принятой задачи не «зависли».

Плюс сами действия: «Принять» только из «Готово (по мнению системы)»,
идемпотентность, паспорт в файле комнаты, запрет остальных действий и возврат.

    python -m unittest tests.test_accept_freeze -v
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

from server import critic_loop, daily, notify, passports, stale, svod_live   # noqa: E402
from server import today as today_mod                                        # noqa: E402
from server import watcher as watcher_mod                                    # noqa: E402
from tests.helpers import Sandbox                                            # noqa: E402

ROOM_ID = "cafe"
ROOM = {"id": ROOM_ID, "board": "team", "orchestrator": "team-director"}


def _detail(status="done", accepted=False, question_open=True):
    """Деталь запроса в том виде, в каком её читают `stale`, свод и критик."""
    question = {"id": "q_1", "text": "Какой канал берём первым?", "for": "mikhail",
                "answered": not question_open, "dismissed": False, "blocking": False}
    return {"id": "t_root0001", "status": status, "archived": False, "accepted": accepted,
            "open_questions": [question], "questions": [question],
            "decisions": [], "next_steps": [], "approvals": [], "artifacts": [],
            "synthesis": {"text_md": "Итог: выбрали самовывоз в двух точках."},
            "tree": [{"id": "t_root0001", "role": "root", "status": status}]}


class AcceptTest(unittest.TestCase):
    """Действия «Принять» / «Вернуть в работу» на живой песочной доске."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        from server import app as app_mod
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def setUp(self):
        from server import registry
        self.ids = self.sb.state("done_modern")
        registry.add_request(self.cfg, ROOM_ID, {
            "root_id": self.ids["root_id"], "title": "Химчистка: приём и выдача",
            "text": "нейтральный пример: химчистка", "created_at": 1793000000,
            "critic": False, "mode": "auto"})
        self.rid = self.ids["root_id"]
        path = passports.accepted_path(self.cfg, ROOM_ID)
        if path.exists():
            path.unlink()
        passports.invalidate()

    def _accept(self, **body):
        return self.app.h_request_actions({}, dict(body, action="accept"), self.rid)

    def test_01_accept_from_done_and_passport_in_file(self):
        status, payload, _headers = self._accept()
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["accepted"])
        self.assertFalse(payload["already"])
        self.assertEqual(payload["board_stage"], "done")
        self.assertEqual(payload["board_column"], "done")
        self.assertEqual(payload["allowed_moves"], ["working"])
        passport = payload.get("passport") or {}
        self.assertEqual(set(passport), {"title", "outcome", "decisions", "files", "at"})
        self.assertTrue(passport["title"])
        self.assertLessEqual(len(passport["outcome"]), 600)
        # файл комнаты — в форме контракта §3: {root_id: {at, by, passport}}
        raw = json.loads(passports.accepted_path(self.cfg, ROOM_ID).read_text(encoding="utf-8"))
        self.assertIn(self.rid, raw)
        self.assertGreater(raw[self.rid]["at"], 0)
        self.assertEqual(raw[self.rid]["by"], "mikhail")
        self.assertIn("passport", raw[self.rid])
        # повторное нажатие — «уже принята», без второй записи
        status, again, _ = self._accept()
        self.assertEqual(status, 200)
        self.assertTrue(again["already"])
        self.assertEqual(again["at"], payload["at"])

    def test_02_accept_only_from_done_system(self):
        from server import registry
        ids = self.sb.state("working")
        registry.add_request(self.cfg, ROOM_ID, {
            "root_id": ids["root_id"], "title": "Грузоперевозки: слоты",
            "text": "нейдерланды не при чём: грузоперевозки", "created_at": 1793000100})
        from server.http_util import ApiError
        with self.assertRaises(ApiError) as ctx:
            self.app.h_request_actions({}, {"action": "accept"}, ids["root_id"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("not_done", json.dumps(ctx.exception.payload(), ensure_ascii=False))

    def test_03_frozen_actions_are_refused(self):
        self._accept()
        from server.http_util import ApiError
        for body in ({"action": "rework", "text": "переделать"},
                     {"action": "critique"},
                     {"action": "svod_refresh"}):
            with self.assertRaises(ApiError) as ctx:
                self.app.h_request_actions({}, body, self.rid)
            self.assertEqual(ctx.exception.status, 409, body)
            self.assertIn("request_accepted",
                          json.dumps(ctx.exception.payload(), ensure_ascii=False), body)

    def test_04_reopen_returns_to_work(self):
        self._accept()
        status, payload, _ = self.app.h_request_actions(
            {}, {"action": "reopen_accepted"}, self.rid)
        self.assertEqual(status, 200)
        self.assertFalse(payload["accepted"])
        self.assertTrue(payload["reopened"])
        self.assertFalse(passports.is_accepted(self.cfg, ROOM, self.rid))
        # действие снова разрешено (задача вернулась в «Готово», не в архив)
        self.assertNotIn(self.rid, passports.accepted_ids(self.cfg, ROOM))

    def test_05_request_page_shows_accepted(self):
        self._accept()
        _status, detail, _headers = self.app.h_request_get({}, {}, self.rid)
        self.assertTrue(detail["accepted"])
        self.assertEqual(detail["board_stage"], "done")
        self.assertEqual(detail["board_column"], "done")
        self.assertEqual(detail["allowed_moves"], ["working"])
        self.assertGreater(detail["accepted_at"], 0)
        self.assertIsInstance(detail["passport"], dict)
        # пункты принятой задачи не «ждут вас»
        self.assertEqual(detail["needs_you_count"]["total"], 0)


# --------------------------------------------------------------------- фейки
class _Log:
    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


class _FakeApp:
    """Приложение для прохода сторожа: считаем вызовы модулей по имени."""

    board = "team"
    demo = False
    mock = False
    sandbox = True

    def __init__(self, detail, cfg):
        self.detail = detail
        self.calls = []
        self.log = _Log()
        self.cfg = cfg

    def call(self, name, action, *args, **kwargs):
        self.calls.append(("%s.%s" % (name, action), args))
        if (name, action) == ("rooms", "list_rooms"):
            return [dict(ROOM)]
        if (name, action) == ("registry", "list_requests"):
            return [{"root_id": str(self.detail.get("id")), "title": "Задача", "created_at": 1}]
        if (name, action) == ("engine_read", "request_detail"):
            return dict(self.detail)
        return {}

    def fn(self, name, action):
        self.calls.append(("%s.%s" % (name, action), ()))
        return lambda *a, **k: {}


class FreezeTest(unittest.TestCase):
    """Ни одна петля работы не трогает принятую задачу."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def setUp(self):
        self.ids = self.sb.state("done_modern")
        self.rid = self.ids["root_id"]
        path = passports.accepted_path(self.cfg, ROOM_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({self.rid: {
            "at": 1793000600, "by": "mikhail",
            "passport": {"title": "Химчистка", "outcome": "Готово", "decisions": [],
                         "files": [], "at": 1793000600},
            "stopped": []}}, ensure_ascii=False), encoding="utf-8")
        passports.invalidate()

    def tearDown(self):
        path = passports.accepted_path(self.cfg, ROOM_ID)
        if path.exists():
            path.unlink()
        passports.invalidate()

    # 1) сторож
    def test_watcher_skips_accepted_with_zero_model_calls(self):
        detail = _detail(status="queued")        # статус не «archived» — флаг в файле
        detail["id"] = self.rid                  # запись принятия — именно про него
        app = _FakeApp(detail, self.cfg)
        stats = watcher_mod.Watcher(app).tick_once()
        names = " ".join(name for name, _ in app.calls)
        for banned in ("critic_loop.run_step", "svod_live.tick", "titles.maybe_refresh",
                       "ingest.ingest_request", "worklog."):
            self.assertNotIn(banned, names, banned)
        self.assertEqual(stats.get("accepted"), 1)
        self.assertEqual(stats.get("requests"), 1)

    def test_watcher_skips_accepted_by_detail_flag(self):
        detail = _detail(status="queued")
        detail["accepted"] = True
        app = _FakeApp(detail, self.cfg)
        stats = watcher_mod.Watcher(app).tick_once()
        names = " ".join(name for name, _ in app.calls)
        self.assertNotIn("critic_loop.run_step", names)
        self.assertNotIn("svod_live.tick", names)
        self.assertEqual(stats.get("accepted"), 1)

    def test_live_request_still_works(self):
        """Контроль: без принятия сторож работает как раньше (тест не «ослеп»)."""
        app = _FakeApp(_detail(status="done"), self.cfg)
        watcher_mod.Watcher(app).tick_once()
        names = " ".join(name for name, _ in app.calls)
        self.assertIn("critic_loop.run_step", names)

    # 2) живой свод
    def test_svod_live_does_not_update(self):
        decision = svod_live.tick(self.cfg, ROOM, {"root_id": self.rid}, _detail(),
                                  now=1793000700)
        self.assertEqual(decision.get("action"), "none")
        self.assertEqual(decision.get("reason"), "accepted")

    # 3) цикл критика
    def test_critic_loop_does_not_work(self):
        steps = critic_loop.run_step(self.cfg, ROOM, {"root_id": self.rid}, _detail())
        self.assertEqual(steps, [{"action": "none", "reason": "accepted"}])

    # 4) напоминания
    def test_stale_closes_items_with_own_reason(self):
        detail = stale.decorate(_detail(accepted=True))
        question = detail["open_questions"][0]
        self.assertFalse(question["needs_you"])
        self.assertEqual(question["closed_reason"], "request_accepted")
        self.assertEqual(detail["needs_you_count"]["total"], 0)
        self.assertIn("request_accepted", stale.REASON_RU)

    # 5) «Сегодня»
    def test_today_does_not_count_accepted_as_waiting(self):
        class Sources(today_mod.Sources):
            def rooms(self):
                return [{"id": ROOM_ID, "title": "Кофейня"}]

            def requests(self, room):
                return [{"root_id": "t_root0001", "title": "Химчистка", "created_at": 1793000000}]

            def states(self, room):
                return {"t_root0001": {"status": "done", "accepted": True}}

            def backlog(self, room):
                return []

            def waiting(self, room):
                return {"t_root0001": [{"kind": "question", "id": "q_1",
                                        "text": "Какой канал берём?", "since": 1793000000}]}

        payload = today_mod.build(Sources(), now=1793000900)
        self.assertEqual(payload["needs_you"], [])
        self.assertEqual(payload["counters"]["needs_you"], 0)

    # 6) «Итог дня»
    def test_daily_does_not_wait_on_accepted(self):
        class Sources(daily.Sources):
            def rooms(self):
                return [{"id": ROOM_ID, "title": "Кофейня"}]

            def requests(self, room):
                return [{"root_id": "t_root0001", "title": "Химчистка", "created_at": 1793000000}]

            def states(self, room):
                return {"t_root0001": {"status": "done", "accepted": True}}

            def waiting(self, room):
                return [{"kind": "question", "text": "Какой канал берём?",
                         "request_id": "t_root0001", "since": 1793000000}]

        payload = daily.build(Sources(), now=1793000900)
        self.assertEqual(payload["waiting"], [])
        self.assertEqual(payload["counters"]["waiting"], 0)

    # 7) уведомления
    def test_notify_ignores_accepted_fact(self):
        settings = {"enabled": True, "kinds": ["finished"], "quiet_hours": {"enabled": False}}
        facts = [{"kind": "root", "room": ROOM_ID, "room_name": "Кофейня",
                  "root": "t_root0001", "status": "done", "accepted": True,
                  "title": "Химчистка", "needs_you": False}]
        result = notify.plan(settings, {"roots": {}}, facts, now=1793000900, port=8857)
        self.assertEqual(result["send"], [])

    # 8) отчёт диспетчера
    def test_kick_report_skips_frozen_cards(self):
        from server import kick_report
        blocked = [row for row in kick_report.board_state(self.cfg, "team")["cards"]
                   if row["status"] == "blocked"]
        if not blocked:                                     # стенд без пауз — не о чем
            self.skipTest("на песочной доске нет заблокированных карточек")
        card_id = blocked[0]["id"]
        path = passports.accepted_path(self.cfg, ROOM_ID)
        data = json.loads(path.read_text(encoding="utf-8"))
        data[self.rid]["stopped"] = [card_id]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        passports.invalidate()
        after = kick_report.board_state(self.cfg, "team")["cards"]
        self.assertNotIn(card_id, [row["id"] for row in after])


if __name__ == "__main__":
    unittest.main()
