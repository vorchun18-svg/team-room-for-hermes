# -*- coding: utf-8 -*-
"""W29-P9 — расход и лимиты по провайдерам.

Проверяем по заданию пакета:
* окна 5 часов / сутки / неделя считаются отдельно, по провайдерам и моделям;
* токены раскладываются на ввод, кэш-чтение, кэш-запись и вывод, стоимость — как
  её пишет Hermes (``sessions.estimated_cost_usd``);
* ПОЛЬЗОВАТЕЛЬСКИЕ лимиты: по умолчанию пусто, сохраняются в данных приложения
  (``data/state/limits.json``), проверяются (отрицательные и мусор не проходят);
* прогресс-полосы предупреждают на 70 % и 90 %, «исчерпан» — на 100 %;
* прогноз «хватит ли» считается по среднему расходу прошедшей части окна;
* топ дорогих запросов сопоставляет сессии с карточками, «зря потрачено» —
  перезапуски на другой модели;
* чужие ``state.db`` открываются ТОЛЬКО на чтение: после замера файлы не меняются.

Данные вымышленные: комнаты «Кофейня» и «Грузоперевозки», провайдеры demo-cloud
и example-ai, запрос «Свести план закупок».

Запуск:
    python -m unittest tests.test_limits -v
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import limits as lim  # noqa: E402
from tests.helpers import _outside_hermes_base  # noqa: E402

NOW = 1_800_000_000
HOUR = 3600


def make_state_db(path: Path, rows) -> None:
    """Мини-``state.db``: только таблица ``sessions`` (остальное расходу не нужно)."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, billing_provider TEXT, model TEXT, "
        "started_at REAL, api_call_count INTEGER, input_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, output_tokens INTEGER, "
        "reasoning_tokens INTEGER, estimated_cost_usd REAL, cost_status TEXT)")
    con.executemany(
        "INSERT INTO sessions (id, billing_provider, model, started_at, api_call_count, "
        "input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens, "
        "estimated_cost_usd, cost_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def make_board(path: Path, events) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "task_id TEXT, kind TEXT, payload TEXT)")
    con.executemany("INSERT INTO task_events (task_id, kind, payload) VALUES (?,?,?)",
                    [(task_id, kind, json.dumps(payload)) for task_id, kind, payload in events])
    con.commit()
    con.close()


class LimitsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w29-p9-", dir=str(_outside_hermes_base())))
        self.home = self.tmp / "hermes"
        self.home.mkdir(parents=True)
        self.data = self.tmp / "data"
        (self.data / "state" / "cafe").mkdir(parents=True)
        self.board = self.tmp / "kanban.db"
        self.cfg = SimpleNamespace(
            state_dir=self.data / "state",
            data_dir=self.data,
            fixtures_dir=self.tmp / "no-fixtures",
            kanban_db=lambda board: self.board,
        )
        (self.data / "rooms.json").write_text(json.dumps({"rooms": [
            {"id": "cafe", "title": "Кофейня", "board": "default"},
        ]}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_registry(self, items):
        path = self.data / "state" / "cafe" / "requests.json"
        path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


class WindowTests(LimitsBase):
    def test_windows_group_by_provider_and_model(self):
        make_state_db(self.home / "state.db", [
            # 2 часа назад — попадает во все окна
            ("s_1", "demo-cloud", "model-a", NOW - 2 * HOUR, 10, 1000, 5000, 2000, 400, 0, 0.5, "estimated"),
            # 20 часов назад — сутки и неделя, но не 5 часов
            ("s_2", "example-ai", "model-b", NOW - 20 * HOUR, 4, 300, 800, 100, 90, 10, 0.2, "estimated"),
            # 8 суток назад — никуда
            ("s_3", "demo-cloud", "model-a", NOW - 8 * 86400, 99, 1, 1, 1, 1, 0, 9.0, "estimated"),
        ])
        payload = lim.panel(self.cfg, state_dir=self.home, now=NOW)
        five = payload["windows"]["5h"]
        self.assertEqual(five["calls"], 10)
        self.assertEqual(five["tokens"]["input"], 1000)
        self.assertEqual(five["tokens"]["cache_read"], 5000)
        self.assertEqual(five["tokens"]["cache_write"], 2000)
        self.assertEqual(five["tokens"]["output"], 400)
        self.assertEqual(five["tokens"]["total"], 8400)
        self.assertAlmostEqual(five["cost_usd"], 0.5, places=6)
        self.assertEqual([p["provider"] for p in five["providers"]], ["demo-cloud"])
        self.assertEqual(five["providers"][0]["models"][0]["model"], "model-a")

        day = payload["windows"]["day"]
        self.assertEqual(day["calls"], 14)
        self.assertEqual(len(day["providers"]), 2)
        week = payload["windows"]["week"]
        self.assertEqual(week["calls"], 14, "старая сессия вне недели не считается")
        self.assertEqual(payload["source"], "state.db")

    def test_profile_state_dbs_are_read_too(self):
        (self.home / "profiles" / "worker").mkdir(parents=True)
        make_state_db(self.home / "profiles" / "worker" / "state.db", [
            ("s_9", "demo-cloud", "model-a", NOW - HOUR, 2, 10, 20, 30, 40, 0, 0.0, "unknown"),
        ])
        payload = lim.panel(self.cfg, state_dir=self.home, now=NOW)
        self.assertEqual(payload["windows"]["5h"]["calls"], 2)
        self.assertTrue(any("стоимость" in w for w in payload["warnings"]))

    def test_empty_state_db_is_honest_not_zero(self):
        payload = lim.panel(self.cfg, state_dir=self.home, now=NOW)
        self.assertEqual(payload["source"], "none")
        self.assertEqual(payload["windows"]["5h"]["calls"], 0)
        self.assertEqual(payload["top_requests"], [])
        self.assertTrue(payload["chip"].startswith("5 ч"))


class ProgressTests(unittest.TestCase):
    def test_thresholds_70_and_90(self):
        self.assertEqual(lim.progress(699, {"kind": "tokens", "value": 1000})["status"], "ok")
        self.assertEqual(lim.progress(700, {"kind": "tokens", "value": 1000})["status"], "warn")
        self.assertEqual(lim.progress(900, {"kind": "tokens", "value": 1000})["status"], "alert")
        self.assertEqual(lim.progress(1000, {"kind": "tokens", "value": 1000})["status"], "over")
        self.assertIsNone(lim.progress(10, None))
        self.assertIsNone(lim.progress(10, {"kind": "tokens", "value": 0}))

    def test_progress_keeps_units_of_limit(self):
        item = lim.progress(40, {"kind": "calls", "value": 50})
        self.assertEqual(item["kind"], "calls")
        self.assertEqual(item["used"], 40)
        self.assertEqual(item["pct"], 80.0)

    def test_forecast_uses_rate_of_elapsed_window(self):
        item = lim.forecast(500, {"kind": "tokens", "value": 1000}, 5 * HOUR, 9000, 2, NOW)
        self.assertAlmostEqual(item["hours_left"], 2.5, places=1)
        self.assertEqual(item["runs_left"], 2)
        self.assertIn("хватит на", item["text"])

    def test_forecast_spent_limit_says_so(self):
        item = lim.forecast(1200, {"kind": "tokens", "value": 1000}, 5 * HOUR, 9000, 4, NOW)
        self.assertEqual(item["hours_left"], 0)
        self.assertIn("израсходован", item["text"])
        self.assertIsNone(lim.forecast(5, None, HOUR, HOUR, 1, NOW))


class LimitsFileTests(LimitsBase):
    def test_default_is_empty(self):
        limits = lim.load_limits(self.cfg)
        self.assertEqual(limits["providers"], {})

    def test_save_load_roundtrip_and_validation(self):
        saved = lim.save_limits(self.cfg, {"providers": {
            "demo-cloud": {"5h": {"kind": "tokens", "value": "150000"},
                           "week": {"kind": "вызовы", "value": 300}},
            "example-ai": {"5h": {"kind": "calls", "value": -5}},
            "": {"5h": {"kind": "tokens", "value": 1}},
        }})
        self.assertEqual(saved["providers"]["demo-cloud"]["5h"], {"kind": "tokens", "value": 150000})
        self.assertEqual(saved["providers"]["demo-cloud"]["week"]["kind"], "tokens")
        self.assertEqual(saved["providers"]["demo-cloud"]["week"]["value"], 300)
        self.assertNotIn("example-ai", saved["providers"], "отрицательный лимит не сохраняется")
        self.assertNotIn("", saved["providers"])
        again = lim.load_limits(self.cfg)
        self.assertEqual(again["providers"], saved["providers"])
        self.assertTrue((self.data / "state" / "limits.json").is_file())

    def test_limits_file_lives_in_app_data_not_hermes_profiles(self):
        lim.save_limits(self.cfg, {"providers": {"demo-cloud": {"5h": {"kind": "calls", "value": 5}}}})
        self.assertTrue(str(lim.limits_path(self.cfg)).startswith(str(self.data)))
        self.assertFalse(list(self.home.rglob("limits.json")), "в профилях Hermes ничего не пишем")

    def test_limit_appears_for_provider_without_runs(self):
        lim.save_limits(self.cfg, {"providers": {"demo-cloud": {"5h": {"kind": "calls", "value": 10}}}})
        payload = lim.panel(self.cfg, state_dir=self.home, now=NOW)
        rows = payload["windows"]["5h"]["providers"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 0)
        self.assertEqual(rows[0]["limit"]["pct"], 0.0)


class RequestsTests(LimitsBase):
    def test_top_requests_and_wasted_runs(self):
        self.write_registry([
            {"root_id": "t_aaaa1111", "title": "Свести план закупок",
             "extra_cards": [{"id": "t_bbbb2222"}]},
        ])
        make_board(self.board, [
            ("t_aaaa1111", "owner_admitted", {"session_id": "s_1", "db": ""}),
            ("t_bbbb2222", "owner_admitted", {"session_id": "s_2", "db": ""}),
            ("t_bbbb2222", "reclaimed", {"session_id": "s_3", "db": ""}),
        ])
        make_state_db(self.home / "state.db", [
            ("s_1", "demo-cloud", "model-a", NOW - HOUR, 5, 100, 900, 50, 60, 0, 0.4, "estimated"),
            ("s_2", "demo-cloud", "model-a", NOW - HOUR, 3, 50, 400, 20, 30, 0, 0.2, "estimated"),
            ("s_3", "example-ai", "model-b", NOW - HOUR, 7, 70, 700, 30, 40, 0, 0.9, "estimated"),
        ])
        payload = lim.panel(self.cfg, state_dir=self.home, now=NOW)
        self.assertEqual(len(payload["top_requests"]), 1)
        item = payload["top_requests"][0]
        self.assertEqual(item["rid"], "t_aaaa1111")
        self.assertEqual(item["room"], "cafe")
        self.assertEqual(item["calls"], 15)          # обе карточки одного запроса
        self.assertAlmostEqual(item["cost_usd"], 1.5, places=6)
        self.assertEqual(payload["wasted"]["attempts"], 1)
        self.assertAlmostEqual(payload["wasted"]["cost_usd"], 0.9, places=6)
        self.assertIn("Потрачено зря", payload["wasted"]["text"])

    def test_state_db_is_not_modified(self):
        make_state_db(self.home / "state.db", [
            ("s_1", "demo-cloud", "model-a", NOW - HOUR, 1, 1, 2, 3, 4, 0, 0.1, "estimated"),
        ])
        path = self.home / "state.db"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        lim.panel(self.cfg, state_dir=self.home, now=NOW)
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, after, "state.db открывается только на чтение")


class FormatTests(unittest.TestCase):
    def test_short_numbers_and_money(self):
        self.assertEqual(lim.short_number(645000), "645 тыс.")
        self.assertEqual(lim.short_number(1_200_000), "1,2 млн")
        self.assertEqual(lim.short_number(640), "640")
        self.assertEqual(lim.money(1.284), "$1.28")
        self.assertEqual(lim.money(0.0031), "$0.003")

    def test_line_and_chip_readable(self):
        payload = {
            "windows": {
                "5h": {"tokens": {"total": 645000}, "cost_usd": 1.284,
                       "summary": {"worst_limit": {"pct": 83.5, "status": "warn",
                                                    "provider": "demo-cloud"}}},
                "week": {"tokens": {"total": 16874000}, "cost_usd": 21.37,
                         "summary": {"worst_limit": {"pct": 73.5, "status": "warn",
                                                      "provider": "demo-cloud",
                                                      "provider_title": "Demo Cloud"}}},
            },
        }
        line = lim.line_of(payload)
        self.assertIn("Расход за 5 ч", line)
        self.assertIn("645 тыс.", line)
        self.assertIn("74", line)
        chip = lim.chip_of(payload)
        self.assertIn("5 ч: 645 тыс.", chip)
        self.assertIn("%", chip)

    def test_human_duration(self):
        self.assertEqual(lim.human_duration(9000), "2 ч 30 мин")
        self.assertEqual(lim.human_duration(2 * 86400 + 4 * 3600), "2 дн 4 ч")
        self.assertEqual(lim.human_duration(30), "меньше минуты")

    def test_routes_are_distinct(self):
        pairs = [(method, pattern) for method, pattern, _handler in lim.ROUTES]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertIn(("GET", "/api/limits"), pairs)
        self.assertIn(("PUT", "/api/limits/settings"), pairs)


if __name__ == "__main__":
    unittest.main()
