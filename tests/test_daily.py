"""Итог дня (W29B-Q8): чистые правила ``server/daily.py`` без сервера и доски.

Проверяем ровно то, за что человек отвечает глазами:

* границы суток (ночь относится к прошлому дню) и подпись периода;
* «что сделано» = запросы, завершённые за сутки; сбой и архив — в «остановлено»;
* число в счётчике = число пунктов в списке (правило «без тупиков»);
* сводка для модели не выходит за бюджет знаков и не содержит путей;
* «Короче»: оценка стоимости до запуска, подставной ответ в песочнице, ошибка
  модели не ломает экран, результат сохраняется и читается.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import server.daily as daily
import server.statefiles as sf


class FakeSources(daily.Sources):
    """Нейтральные данные: кофейня и химчистка, никаких настоящих имён и путей."""

    def __init__(self, now: int):
        self.now = now
        self._rooms = [{"id": "cafe", "title": "Кофейня"}, {"id": "laundry", "title": "Химчистка"}]
        self._requests = {
            "cafe": [
                {"root_id": "t_cafe1", "title": "Меню на неделю", "created_at": now - 3 * 3600},
                {"root_id": "t_cafe2", "title": "Заказ зерна", "created_at": now - 30 * 3600},
                {"root_id": "t_cafe3", "title": "Старое меню", "created_at": now - 90 * 3600},
            ],
            "laundry": [
                {"root_id": "t_laundry1", "title": "Прайс на чистку", "created_at": now - 5 * 3600},
            ],
        }
        self._states = {
            "cafe": {
                "t_cafe1": {"status": "done", "finished_at": now - 3600},
                "t_cafe2": {"status": "failed", "finished_at": now - 7200,
                            "error": "Не нашлась папка с прайсом"},
                "t_cafe3": {"status": "done", "finished_at": now - 80 * 3600},
            },
            "laundry": {"t_laundry1": {"status": "working", "started_at": now - 600}},
        }
        self._waiting = {
            "cafe": [{"kind": "question", "text": "Какой объём зерна брать?",
                      "request_id": "t_cafe1", "since": now - 2 * 3600,
                      "recommended": "Ответить на вопрос"}],
            "laundry": [],
        }
        self._decisions = {
            "cafe": [
                {"id": "t_cafe1:d_1", "at": now - 1800, "text": "Берём арабику 1 кг",
                 "status": "accepted", "source_request": "t_cafe1"},
                {"id": "t_cafe3:d_1", "at": now - 80 * 3600, "text": "Старое решение",
                 "status": "accepted", "source_request": "t_cafe3"},
            ],
            "laundry": [],
        }
        self._files = {
            "cafe": [{"name": "меню-на-неделю.md", "at": now - 3600},
                     {"name": "старый-прайс.md", "at": now - 100 * 3600}],
            "laundry": [],
        }
        self._archived = {"cafe": [], "laundry": [{"root_id": "t_laundry0",
                                                   "title": "Прошлый прайс", "at": now - 900}]}

    def rooms(self):
        return list(self._rooms)

    def requests(self, room):
        return list(self._requests.get(room.get("id"), []))

    def states(self, room):
        return dict(self._states.get(room.get("id"), {}))

    def waiting(self, room):
        return list(self._waiting.get(room.get("id"), []))

    def decisions(self, room):
        return list(self._decisions.get(room.get("id"), []))

    def files(self, room):
        return list(self._files.get(room.get("id"), []))

    def archived(self, room):
        return list(self._archived.get(room.get("id"), []))

    def usage(self):
        return {"calls": 42, "tokens": 128_000, "cost_usd": 0.31, "source": "state.db",
                "note": ""}


def _noon() -> int:
    """Полдень локального времени — середина суток итога."""
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    return int(now.timestamp())


class DayBoundsTest(unittest.TestCase):
    def test_night_belongs_to_previous_day(self):
        now = int(datetime.now().replace(hour=2, minute=30, second=0,
                                         microsecond=0).timestamp())
        start = daily.day_start(now)
        self.assertLess(start, now)
        self.assertEqual(datetime.fromtimestamp(start).hour, daily.DAY_START_HOUR)
        self.assertGreaterEqual(now - start, 22 * 3600)

    def test_same_day_after_start_hour(self):
        now = int(datetime.now().replace(hour=9, minute=0, second=0,
                                         microsecond=0).timestamp())
        start = daily.day_start(now)
        self.assertEqual(datetime.fromtimestamp(start).hour, daily.DAY_START_HOUR)
        self.assertEqual(datetime.fromtimestamp(start).date(),
                         datetime.fromtimestamp(now).date())

    def test_period_of(self):
        def at(hour):
            return int(datetime.now().replace(hour=hour, minute=0, second=0,
                                              microsecond=0).timestamp())
        self.assertEqual(daily.period_of(at(6)), "morning")
        self.assertEqual(daily.period_of(at(12)), "day")
        self.assertEqual(daily.period_of(at(20)), "evening")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.now = _noon()
        self.payload = daily.build(FakeSources(self.now), now=self.now)

    def test_done_only_for_today(self):
        titles = [row["request_title"] for row in self.payload["done"]]
        self.assertEqual(titles, ["Меню на неделю"])
        self.assertEqual(self.payload["counters"]["done"], 1)

    def test_failed_and_archived_go_to_stopped(self):
        kinds = {(row["kind"], row["request_title"]) for row in self.payload["stopped"]}
        self.assertIn(("failed", "Заказ зерна"), kinds)
        self.assertIn(("archived", "Прошлый прайс"), kinds)
        self.assertEqual(self.payload["counters"]["stopped"], 2)

    def test_counters_equal_list_lengths(self):
        counters = self.payload["counters"]
        self.assertEqual(counters["done"], len(self.payload["done"]))
        self.assertEqual(counters["decisions"], len(self.payload["decisions"]))
        self.assertEqual(counters["files"], len(self.payload["files"]))
        self.assertEqual(counters["waiting"], len(self.payload["waiting"]))
        self.assertEqual(counters["stopped"], len(self.payload["stopped"]))

    def test_decisions_and_files_only_today(self):
        self.assertEqual([row["title"] for row in self.payload["decisions"]],
                         ["Берём арабику 1 кг"])
        self.assertEqual([row["name"] for row in self.payload["files"]],
                         ["меню-на-неделю.md"])

    def test_waiting_has_action(self):
        self.assertEqual(len(self.payload["waiting"]), 1)
        item = self.payload["waiting"][0]
        self.assertEqual(item["kind"], "question")
        self.assertTrue(item["recommended"])

    def test_spent_passed_through(self):
        self.assertEqual(self.payload["spent"]["calls"], 42)
        self.assertEqual(self.payload["counters"]["spent_tokens"], 128_000)
        self.assertAlmostEqual(self.payload["spent"]["cost_usd"], 0.31, places=6)

    def test_single_room_filter(self):
        payload = daily.build(FakeSources(self.now), now=self.now, room="laundry")
        self.assertEqual([row["id"] for row in payload["rooms"]], ["laundry"])
        self.assertEqual(payload["counters"]["done"], 0)
        self.assertEqual(payload["counters"]["stopped"], 1)

    def test_empty_is_honest(self):
        class Empty(daily.Sources):
            def rooms(self):
                return [{"id": "cafe", "title": "Кофейня"}]

        payload = daily.build(Empty(), now=self.now)
        self.assertTrue(payload["empty"])
        self.assertEqual(payload["counters"]["all"], 0)
        self.assertEqual(payload["waiting"], [])

    def test_broken_source_does_not_break_day(self):
        class Broken(FakeSources):
            def decisions(self, room):
                raise RuntimeError("битый реестр")

            def files(self, room):
                raise RuntimeError("нет каталога")

        payload = daily.build(Broken(self.now), now=self.now)
        self.assertEqual(payload["counters"]["decisions"], 0)
        self.assertEqual(payload["counters"]["done"], 1)


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.now = _noon()
        self.payload = daily.build(FakeSources(self.now), now=self.now)

    def test_digest_is_short_and_factual(self):
        text = daily.digest_text(self.payload)
        self.assertLessEqual(len(text), daily.SHORT_MAX_INPUT)
        self.assertIn("Сделано", text)
        self.assertIn("Решения", text)
        self.assertIn("Ждёт вас", text)
        self.assertIn("Остановлено", text)
        self.assertNotIn("C:\\", text)
        self.assertNotIn("/c/", text)

    def test_prompt_asks_for_five_lines(self):
        prompt = daily.short_prompt(self.payload)
        self.assertIn("пяти коротких строк", prompt)
        self.assertIn("Только факты", prompt)

    def test_estimate_without_price_is_honest(self):
        est = daily.estimate(self.payload)
        self.assertGreater(est["input_tokens"], 0)
        self.assertIsNone(est["cost_usd"])
        self.assertTrue(est["note"])

    def test_estimate_with_price(self):
        est = daily.estimate(self.payload, price_per_token=0.000002)
        self.assertIsNotNone(est["cost_usd"])
        self.assertGreater(est["cost_usd"], 0)
        self.assertEqual(est["output_tokens"], daily.SHORT_OUTPUT_TOKENS)


class ShortenTest(unittest.TestCase):
    def setUp(self):
        self.now = _noon()
        self.payload = daily.build(FakeSources(self.now), now=self.now)
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = type("Cfg", (), {"state_dir": Path(self.tmp.name) / "state",
                                    "hermes_home": "", "hermes_cli": "hermes"})()
        daily.reset_runner()
        os.environ.pop(daily.SHORT_FAKE_ENV, None)

    def tearDown(self):
        daily.reset_runner()
        os.environ.pop(daily.SHORT_FAKE_ENV, None)
        self.tmp.cleanup()

    def test_runner_answer_saved_and_read(self):
        daily.set_runner(lambda prompt: {"ok": True, "text": "Коротко: всё сделано.",
                                         "error": ""})
        result = daily.shorten(self.cfg, self.payload, "cafe", now=self.now)
        self.assertTrue(result["ok"])
        self.assertIn("Коротко", result["text"])
        saved = daily.last_short(self.cfg, "cafe")
        self.assertEqual(saved["text"], result["text"])
        self.assertEqual(saved["day"], self.payload["day"])

    def test_runner_error_keeps_screen(self):
        daily.set_runner(lambda prompt: {"ok": False, "text": "",
                                         "error": "Модель вернула ошибку: таймаут"})
        result = daily.shorten(self.cfg, self.payload, "cafe", now=self.now)
        self.assertFalse(result["ok"])
        self.assertIn("таймаут", result["error"])
        self.assertIn("estimate", result)
        self.assertIsNone(daily.last_short(self.cfg, "cafe"))

    def test_sandbox_fake_answer(self):
        os.environ[daily.SHORT_FAKE_ENV] = "1"
        result = daily.shorten(self.cfg, self.payload, "cafe", now=self.now)
        self.assertTrue(result["ok"])
        self.assertTrue(result["fake"])
        self.assertTrue(result["text"])

    def test_prompt_has_no_paths(self):
        seen = {}

        def runner(prompt):
            seen["prompt"] = prompt
            return {"ok": True, "text": "Готово", "error": ""}

        daily.shorten(self.cfg, self.payload, "cafe", runner=runner, now=self.now)
        self.assertNotIn("C:\\", seen["prompt"])
        self.assertIn("Меню на неделю", seen["prompt"])


class PriceTest(unittest.TestCase):
    def test_price_from_state_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) .joinpath("state.db")
            con = sqlite3.connect(str(db))
            con.execute("CREATE TABLE sessions (model TEXT, input_tokens INTEGER, "
                        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                        "output_tokens INTEGER, estimated_cost_usd REAL)")
            con.execute("INSERT INTO sessions VALUES ('m1', 1000, 0, 0, 1000, 0.002)")
            con.execute("INSERT INTO sessions VALUES ('m1', 2000, 0, 0, 2000, 0.004)")
            con.execute("INSERT INTO sessions VALUES ('m1', 500, 0, 0, 500, 0)")
            con.commit()
            con.close()
            price = daily.price_per_token(tmp, "m1")
            self.assertAlmostEqual(price, 0.000001, places=9)
            self.assertIsNone(daily.price_per_token(tmp, "нет-такой"))

    def test_price_without_state_db(self):
        self.assertIsNone(daily.price_per_token("", "m1"))
        self.assertIsNone(daily.price_per_token(None, "m1"))


class ShortFileTest(unittest.TestCase):
    def test_last_short_tolerates_broken_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = type("Cfg", (), {"state_dir": Path(tmp)})()
            self.assertIsNone(daily.last_short(cfg, "cafe"))
            path = daily.short_path(cfg, "cafe")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{битый", encoding="utf-8")
            self.assertIsNone(daily.last_short(cfg, "cafe"))
            sf.write_json_atomic(path, {"text": "Готово", "at": 1})
            self.assertEqual(daily.last_short(cfg, "cafe")["text"], "Готово")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
