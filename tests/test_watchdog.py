# -*- coding: utf-8 -*-
"""Сторож зависаний (WAVE25 §M2): 10 минут — предупреждение, 20 — остановка.

Проверяем ПРАВИЛО и ПРОХОД на подставных данных:
  * время подменяется аргументом — реальные минуты не ждём;
  * 11 минут молчания → предупреждение, 19 → ещё предупреждение, 21 → остановка;
  * карточка с живым выводом инструментов не считается зависшей (E5);
  * остановку можно выключить настройкой комнаты;
  * повторный проход не останавливает ту же карточку второй раз (замок);
  * «Сегодня» получает новый вид дела `stalled`, старые виды не меняются.

Данные вымышленные: «Сайт студии», «Ремонт офиса», профили `lead`/`designer`.

Запуск:
    python -m unittest tests.test_watchdog -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import today, watchdog  # noqa: E402

NOW = 1_800_000_000


class FakeCfg:
    def __init__(self, base: Path):
        self.data_dir = base / "data"
        self.board = "team"
        self.mock = False
        self.demo = False

    def kanban_db(self, board=""):
        return self.data_dir / "kanban" / ("%s.db" % (board or "team"))


def room(**over):
    data = {"id": "studio", "title": "Сайт студии", "board": "team", "tenant": "room:studio",
            "orchestrator": "lead"}
    data.update(over)
    return data


class ClassifyTest(unittest.TestCase):
    """Правило: чистая функция, без доски и файлов."""

    def setUp(self):
        self.conf = watchdog.normalize(None)

    def check(self, minutes, **kw):
        return watchdog.classify(status=kw.pop("status", "running"),
                                 last_at=NOW, started_at=NOW - 3600,
                                 now=NOW + minutes * 60, conf=self.conf, **kw)

    def test_defaults_are_ten_and_twenty(self):
        self.assertEqual(self.conf, {"warn_min": 10, "stop_min": 20, "hard_stop": True})

    def test_working_before_ten_minutes(self):
        self.assertEqual(self.check(9)["state"], "working")

    def test_warns_at_ten_minutes(self):
        verdict = self.check(11)
        self.assertEqual(verdict["state"], "warn")
        self.assertEqual(verdict["action"], "warn")
        self.assertIn("не отвечает 11 мин", verdict["text"])

    def test_stops_at_twenty_minutes(self):
        verdict = self.check(21)
        self.assertEqual(verdict["state"], "hung")
        self.assertEqual(verdict["action"], "stop")

    def test_live_tool_output_is_not_a_hang(self):
        self.assertEqual(self.check(45, tool_alive=True)["state"], "working")

    def test_finished_card_is_ignored(self):
        self.assertEqual(self.check(60, status="done")["state"], "idle")
        self.assertEqual(self.check(60, status="blocked")["state"], "idle")

    def test_hard_stop_can_be_switched_off(self):
        conf = watchdog.normalize({"hard_stop": False})
        verdict = watchdog.classify(status="running", last_at=NOW, started_at=0,
                                    now=NOW + 40 * 60, conf=conf)
        self.assertEqual(verdict["state"], "hung")
        self.assertEqual(verdict["action"], "warn", "остановка выключена — только предупреждаем")

    def test_room_thresholds_are_used(self):
        conf = watchdog.normalize({"warn_min": 3, "stop_min": 7})
        early = watchdog.classify(status="running", last_at=NOW, started_at=0,
                                  now=NOW + 4 * 60, conf=conf)
        self.assertEqual(early["state"], "warn")
        late = watchdog.classify(status="running", last_at=NOW, started_at=0,
                                 now=NOW + 8 * 60, conf=conf)
        self.assertEqual(late["state"], "hung")

    def test_broken_settings_fall_back_to_defaults(self):
        conf = watchdog.normalize({"warn_min": "мусор", "stop_min": -5})
        self.assertEqual(conf["warn_min"], watchdog.WARN_MIN)
        self.assertGreater(conf["stop_min"], conf["warn_min"], "стоп всегда позже предупреждения")

    def test_started_at_is_the_fallback_when_no_activity(self):
        verdict = watchdog.classify(status="running", last_at=0, started_at=NOW,
                                    now=NOW + 21 * 60, conf=self.conf)
        self.assertEqual(verdict["state"], "hung")


class StateAndLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-watchdog-")
        self.cfg = FakeCfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_is_empty_and_safe_when_missing(self):
        self.assertEqual(watchdog.stalled(self.cfg, room(), now=NOW), [])
        self.assertEqual(watchdog.read_state(self.cfg, "studio")["cards"], {})

    def test_one_active_run_per_card(self):
        self.assertTrue(watchdog.acquire(self.cfg, "studio", "t_1", now=NOW))
        self.assertFalse(watchdog.acquire(self.cfg, "studio", "t_1", now=NOW + 10),
                         "второй проход не входит в ту же карточку")
        watchdog.release(self.cfg, "studio", "t_1")
        self.assertTrue(watchdog.acquire(self.cfg, "studio", "t_1", now=NOW + 20))

    def test_stale_lock_expires(self):
        self.assertTrue(watchdog.acquire(self.cfg, "studio", "t_2", now=NOW, ttl=60))
        self.assertTrue(watchdog.acquire(self.cfg, "studio", "t_2", now=NOW + 120, ttl=60),
                        "брошенный замок не держит карточку вечно")

    def test_stalled_lists_only_recent_warnings(self):
        def change(state):
            state["cards"]["t_1"] = {"card_id": "t_1", "state": "warn", "idle_min": 12,
                                     "at": NOW, "label": "designer"}
            state["cards"]["t_2"] = {"card_id": "t_2", "state": "warn", "idle_min": 11,
                                     "at": NOW - 10_000, "label": "designer"}
            state["cards"]["t_3"] = {"card_id": "t_3", "state": "working", "idle_min": 2,
                                     "at": NOW}

        watchdog._mutate(self.cfg, "studio", change)      # noqa: SLF001 - состояние комнаты
        rows = watchdog.stalled(self.cfg, room(), now=NOW)
        self.assertEqual([row["card_id"] for row in rows], ["t_1"])
        report = watchdog.report(self.cfg, room())
        self.assertEqual(report["warns"], 0)
        self.assertEqual(report["cards"], 2, "в сводке — предупреждение и старая запись")


class FakeSources(today.Sources):
    """Комната с одной зависшей карточкой и одним вопросом — для «Сегодня»."""

    def rooms(self):
        return [room()]

    def requests(self, r):
        return [{"root_id": "t_root", "title": "Сайт студии", "created_at": NOW - 600}]

    def backlog(self, r):
        return [{"id": "i1", "kind": "open_question", "owner": today.HUMAN, "status": "open",
                 "text": "Какой срок?", "source_request": "t_root", "created_at": NOW - 60}]

    def stalled(self, r):
        return [{"card_id": "t_card", "root_id": "t_root", "label": "designer",
                 "idle_min": 12, "state": "warn", "at": NOW - 120},
                {"card_id": "t_card2", "root_id": "t_root", "label": "lead",
                 "idle_min": 22, "state": "hung", "at": NOW - 60, "hard_stop": True}]


class TodayStalledTest(unittest.TestCase):
    def test_stalled_is_a_new_needs_you_kind(self):
        data = today.build(FakeSources(), last_visit_at=None, now=NOW)
        kinds = [item["kind"] for item in data["needs_you"]]
        self.assertIn("stalled", kinds)
        self.assertIn("question", kinds, "старые виды не пострадали")
        stalled = [item for item in data["needs_you"] if item["kind"] == "stalled"]
        self.assertEqual(len(stalled), 2)
        self.assertTrue(all(len(item["text"]) <= today.TEXT_MAX for item in stalled))
        warn = [item for item in stalled if "12 мин" in item["text"]][0]
        hung = [item for item in stalled if "22 мин" in item["text"]][0]
        self.assertIn("не отвечает 12 мин", warn["text"])
        self.assertIn("нужно внимание", hung["text"], "остановленная карточка")
        self.assertTrue(hung["urgent"], "20+ минут — срочно")
        self.assertFalse(warn["urgent"])
        self.assertEqual(warn["recommended"], today.RECOMMENDED["stalled"])
        self.assertEqual(stalled[0], hung, "срочные — первыми (порядок контракта)")
        self.assertEqual(data["counters"]["needs_you"], len(data["needs_you"]))

    def test_stalled_does_not_break_empty_source(self):
        class Empty(today.Sources):
            def rooms(self):
                return [room()]

        data = today.build(Empty(), last_visit_at=NOW - 60, now=NOW)
        self.assertEqual([i for i in data["needs_you"] if i["kind"] == "stalled"], [])

    def test_hard_stop_off_is_said_in_words(self):
        class Off(FakeSources):
            def stalled(self, r):
                return [{"card_id": "t_card2", "root_id": "t_root", "label": "lead",
                         "idle_min": 25, "state": "hung", "at": NOW, "hard_stop": False}]

        data = today.build(Off(), last_visit_at=None, now=NOW)
        item = [i for i in data["needs_you"] if i["kind"] == "stalled"][0]
        self.assertIn("остановка выключена", item["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
