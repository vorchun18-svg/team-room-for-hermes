# -*- coding: utf-8 -*-
"""Архив запросов (W29-P4): остановка работы, раздел «Архив», возврат.

Проверяем поведение, а не текст:
* архив останавливает всё — сторож не зовёт ни критика, ни названия, ни свод,
  ни ingest по архивному запросу (счётчик вызовов = 0);
* возврат восстанавливает статусы карточек (завершённые — `done`, незавершённые —
  `blocked`, то есть «продолжить кнопкой», без запуска моделей);
* висящие пункты у архивного запроса закрыты с причиной `request_archived` и
  снова открываются после возврата — без записи в карточки;
* ручки «Архива»: список, массово в архив, массово из архива.

Песочница — `tests.helpers.Sandbox` (настоящий CLI канбана по временной доске,
реальный Hermes и Vault не трогаются). Никаких моделей: доска и реестр только.

    python -m unittest tests.test_archive -v
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import archive as archive_mod      # noqa: E402
from server import engine_read                 # noqa: E402
from server import registry                    # noqa: E402
from server import stale                       # noqa: E402
from server import watcher as watcher_mod      # noqa: E402
from tests.helpers import Sandbox              # noqa: E402

ROOM_ID = "cafe"
ROOM = {"id": ROOM_ID, "board": "team", "orchestrator": "team-director"}


def _detail(status="done", archived=False, question_open=True):
    """Деталь запроса ровно в том виде, в каком её читает `stale.decorate`."""
    question = {"id": "q_1", "text": "Какой канал берём первым?", "for": "mikhail",
                "answered": not question_open, "dismissed": False}
    return {"id": "t_root0001", "status": status, "archived": archived,
            "open_questions": [question], "questions": [question],
            "decisions": [], "next_steps": [], "approvals": [],
            "tree": [{"id": "t_root0001", "role": "root", "status": "archived" if archived else status}]}


class RegistryTest(unittest.TestCase):
    """Реестр запроса нужен и «Архиву», и возврату: прежние статусы карточек."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def _register(self, state_name):
        ids = self.sb.state(state_name)
        registry.add_request(self.cfg, ROOM_ID, {
            "root_id": ids["root_id"], "title": "Архив: " + state_name,
            "text": "запрос песочницы", "created_at": 1793000000,
        })
        return ids

    def _statuses(self, ids):
        states = engine_read.request_states(self.cfg, "team",
                                            [ids["root_id"], *ids["child_ids"]])
        return {tid: (states.get(tid) or {}).get("status") for tid in states}

    def test_01_archive_stops_work_and_keeps_prev(self):
        ids = self._register("done_modern")
        record = registry.get(self.cfg, ROOM_ID, ids["root_id"])
        res = archive_mod.archive_request(self.cfg, ROOM, record, ids["root_id"], by="mikhail")
        self.assertFalse(res["already"])
        self.assertEqual(set(res["archived"]), {ids["root_id"], *ids["child_ids"]})
        self.assertEqual(set(self._statuses(ids).values()), {"archived"})
        summary = engine_read.request_summary(self.cfg, "team", ids["root_id"], [], ROOM)
        self.assertTrue(summary["archived"])
        self.assertEqual(len(res["prev"]), len(res["archived"]))
        saved = registry.get(self.cfg, ROOM_ID, ids["root_id"])
        self.assertEqual(saved["archive_prev"], res["prev"])
        self.assertGreater(saved["archived_at"], 0)
        # повторный архив — честное «уже в архиве», без второй работы
        again = archive_mod.archive_request(self.cfg, ROOM, saved, ids["root_id"])
        self.assertTrue(again["already"])
        self.assertEqual(again["archived"], [])

    def test_02_restore_returns_state_as_before(self):
        ids = self._register("done_modern")
        record = registry.get(self.cfg, ROOM_ID, ids["root_id"])
        archive_mod.archive_request(self.cfg, ROOM, record, ids["root_id"])
        record = registry.get(self.cfg, ROOM_ID, ids["root_id"])
        before = {tid: status for tid, status in record["archive_prev"].items()}
        res = archive_mod.restore_request(self.cfg, ROOM, record, ids["root_id"])
        self.assertEqual(res["count"], len(before))
        self.assertEqual(res["paused"], [])
        after = self._statuses(ids)
        for tid, status in before.items():
            want = "done" if status == "done" else "blocked"
            self.assertEqual(after[tid], want, tid)
        summary = engine_read.request_summary(self.cfg, "team", ids["root_id"], [], ROOM)
        self.assertFalse(summary["archived"])
        self.assertEqual(summary["status"], "done")
        saved = registry.get(self.cfg, ROOM_ID, ids["root_id"])
        self.assertEqual(saved["archive_prev"], {})
        self.assertGreater(saved["restored_at"], 0)

    def test_03_unfinished_work_is_paused_not_started(self):
        ids = self._register("working")
        record = registry.get(self.cfg, ROOM_ID, ids["root_id"])
        archive_mod.archive_request(self.cfg, ROOM, record, ids["root_id"])
        res = archive_mod.restore_request(self.cfg, ROOM, record, ids["root_id"])
        self.assertTrue(res["paused"])            # работа не завершилась — пауза
        after = self._statuses(ids)
        self.assertEqual(after[ids["root_id"]], "blocked")
        for tid in res["paused"]:
            self.assertEqual(after[tid], "blocked")
        detail = engine_read.request_detail(self.cfg, "team", ids["root_id"], ROOM, [])
        self.assertEqual(detail["status"], "needs_you")   # «Продолжить», а не «в очереди»
        self.assertTrue((detail.get("status_reason") or "").strip())

    def test_04_list_only_archived(self):
        from server import app as app_mod
        ids = self._register("done_modern")
        app = app_mod.App(self.cfg, port=0)
        app.call("rooms", "seed_defaults", ((self.cfg,), {}))
        status, payload, _ = app.h_archive_list({}, {}, ROOM_ID)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 0)     # ещё не в архиве
        archive_mod.archive_request(self.cfg, ROOM, registry.get(self.cfg, ROOM_ID, ids["root_id"]),
                                    ids["root_id"])
        status, payload, _ = app.h_archive_list({"q": ["архив"]}, {}, ROOM_ID)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        self.assertTrue(item["archived"])
        self.assertGreater(item["archived_at"], 0)
        # поиск, который ничего не находит, честно пуст
        _status, empty, _ = app.h_archive_list({"q": ["нет-такого"]}, {}, ROOM_ID)
        self.assertEqual(empty["total"], 0)

    def test_05_mass_archive_and_restore_endpoints(self):
        from server import app as app_mod
        ids = self._register("done_modern")
        app = app_mod.App(self.cfg, port=0)
        app.call("rooms", "seed_defaults", ((self.cfg,), {}))
        status, payload, _ = app.h_archive_mass({}, {"root_ids": [ids["root_id"]]}, ROOM_ID)
        self.assertEqual(status, 200)
        self.assertEqual(payload["done"], 1)
        self.assertEqual(self._statuses(ids)[ids["root_id"]], "archived")
        status, payload, _ = app.h_archive_restore({}, {"root_ids": [ids["root_id"]]}, ROOM_ID)
        self.assertEqual(status, 200)
        self.assertEqual(payload["done"], 1)
        self.assertEqual(self._statuses(ids)[ids["root_id"]], "done")
        # мусор в теле — отказ с причиной, а не тихий успех
        with self.assertRaises(Exception):
            app.h_archive_mass({}, {"root_ids": []}, ROOM_ID)


class StaleReasonTest(unittest.TestCase):
    """Причина закрытия пунктов: архив — `request_archived`, возврат — снова открыт."""

    def test_archived_closes_with_own_reason(self):
        detail = stale.decorate(_detail(archived=True))
        q = detail["open_questions"][0]
        self.assertFalse(q["needs_you"])
        self.assertEqual(q["closed_reason"], "request_archived")
        self.assertEqual(detail["needs_you_count"]["total"], 0)

    def test_restore_opens_items_again_without_writes(self):
        detail = stale.decorate(_detail(archived=False, status="done"))
        q = detail["open_questions"][0]
        self.assertTrue(q["needs_you"])          # у `done` вопрос — рабочее «ждёт вас»
        self.assertNotEqual(q.get("closed_reason"), "request_archived")
        self.assertEqual(detail["needs_you_count"]["questions"], 1)


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

    def __init__(self, detail):
        import types
        self.detail = detail
        self.calls = []
        self.log = _Log()
        self.cfg = types.SimpleNamespace(board="team", sandbox=True, demo=False, mock=False,
                                         data_dir=Path("."), log_dir=Path("."))

    def call(self, name, action, *args, **kwargs):
        self.calls.append(("%s.%s" % (name, action), args))
        if (name, action) == ("rooms", "list_rooms"):
            return [dict(ROOM)]
        if (name, action) == ("registry", "list_requests"):
            return [{"root_id": "t_root0001", "title": "Архивный", "created_at": 1}]
        if (name, action) == ("engine_read", "request_detail"):
            return dict(self.detail)
        return {}

    def fn(self, name, action):
        self.calls.append(("%s.%s" % (name, action), ()))
        return lambda *a, **k: {}


class WatcherStopTest(unittest.TestCase):
    """Архивный запрос не даёт сторожу ни одного вызова «работы»."""

    def _tick(self, archived):
        detail = _detail(status="done", archived=archived)
        app = _FakeApp(detail)
        if archived:
            detail["status"] = "queued"      # как у архивного корня до W29
        stats = watcher_mod.Watcher(app).tick_once()
        return app, stats

    def test_archived_request_is_not_touched(self):
        app, stats = self._tick(archived=True)
        names = " ".join(name for name, _ in app.calls)
        for banned in ("critic_loop.run_step", "titles.maybe_refresh",
                       "svod_live.tick", "ingest.ingest_request"):
            self.assertNotIn(banned, names)
        self.assertEqual(stats.get("archived"), 1)
        self.assertEqual(stats.get("requests"), 1)

    def test_live_request_still_works(self):
        app, _stats = self._tick(archived=False)
        names = " ".join(name for name, _ in app.calls)
        self.assertIn("critic_loop.run_step", names)
        self.assertIn("titles.maybe_refresh", names)


class ContractTest(unittest.TestCase):
    def test_actions_include_unarchive(self):
        from server.app_common import ACTION_ALLOWED
        self.assertIn("archive", ACTION_ALLOWED)
        self.assertIn("unarchive", ACTION_ALLOWED)

    def test_suggestions_after_14_days_without_auto_archive(self):
        """«Пора в архив»: завершён ≥ 14 дней — подсказка; автоархив выключен."""
        import time
        now = int(time.time())
        old = now - 15 * 86400
        records = [{"root_id": "t_0000aaaa", "title": "Старый запрос"},
                   {"root_id": "t_0000bbbb", "title": "Свежий запрос"},
                   {"root_id": "t_0000cccc", "title": "Уже в архиве", "archived_at": now - 86400}]
        states = {"t_0000aaaa": {"status": "done", "finished_at": old},
                  "t_0000bbbb": {"status": "done", "finished_at": now - 2 * 86400},
                  "t_0000cccc": {"status": "archived", "finished_at": old}}
        found = archive_mod.suggestions(records, states, now=now)
        self.assertEqual(["t_0000aaaa"], [i["root_id"] for i in found["items"]], found)
        self.assertEqual(1, found["total"])
        self.assertGreaterEqual(found["items"][0]["days"], 14)
        self.assertEqual(14, found["after_days"])
        self.assertFalse(found["auto"], "автоархив по умолчанию должен быть выключен")
        self.assertFalse(archive_mod.auto_enabled({"id": ROOM_ID}))
        self.assertTrue(archive_mod.auto_enabled({"archive_auto": True}))

    def test_suggestions_ignore_unfinished_and_empty(self):
        import time
        now = int(time.time())
        records = [{"root_id": "t_0000dddd", "title": "Ещё идёт"}]
        states = {"t_0000dddd": {"status": "running", "finished_at": now - 30 * 86400}}
        self.assertEqual(0, archive_mod.suggestions(records, states, now=now)["total"])
        self.assertEqual(0, archive_mod.suggestions([], {}, now=now)["total"])


if __name__ == "__main__":
    unittest.main()
