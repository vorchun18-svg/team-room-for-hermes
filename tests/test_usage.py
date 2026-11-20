# -*- coding: utf-8 -*-
"""W28-E — расход токенов запроса: карточки → сессии → токены, стоимость, строка.

Проверяем по пункту 4 задания (и по замерам 19.11.2026):
* сессии карточек находятся по ``task_events.owner_admitted`` (там Hermes пишет
  ``session_id`` и путь к ``state.db`` профиля);
* токены и стоимость складываются из ``sessions`` — как в ``sessions.estimated_cost_usd``;
* «не та модель» считается отдельно (попытки ``reclaimed`` и смена модели);
* строка для интерфейса — «Расход: N выз. / M тыс. токенов (база / тело / инструменты)»;
* чужие базы открываются ТОЛЬКО на чтение (``mode=ro``): после замера файлы не меняются;
* демо/песочница без данных отдаёт фикстуру и честный ``source``.

Данные вымышленные: комната «Студия», профили analyst/writer, запрос «Сайт студии».

Запуск:
    python -m unittest tests.test_usage -v
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import usage as usage_mod  # noqa: E402

RID = "t_1000aaaa"


def make_board(path: Path, events) -> None:
    """Доска канбана: только ``task_events`` — больше расходу ничего не нужно."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
                "run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)")
    for task_id, kind, payload in events:
        con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, 1, ?, ?, 0)", (task_id, kind, json.dumps(payload)))
    con.commit()
    con.close()


def make_state(path: Path, sessions) -> None:
    """state.db профиля: ``sessions`` + ``system_prompts`` (как у Hermes)."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, api_call_count INTEGER, "
        "input_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
        "output_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, "
        "cost_status TEXT, system_prompt_hash TEXT)")
    con.execute("CREATE TABLE system_prompts (hash TEXT PRIMARY KEY, prompt TEXT)")
    prompt = "Ты — участник комнаты. " * 200
    con.execute("INSERT INTO system_prompts VALUES ('h1', ?)", (prompt,))
    con.execute("INSERT INTO system_prompts VALUES ('h2', ?)",
                (json.dumps({"tools": [{"function": {"name": "kanban_show"}}] * 50}),))
    for row in sessions:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
    con.commit()
    con.close()


class FakeCfg:
    def __init__(self, base: Path):
        self.base = base
        self.fixtures_dir = base / "fixtures"
        self.kanban = base / "kanban.db"

    def kanban_db(self, board: str):
        return self.kanban


def room():
    return {"id": "studio", "title": "Студия", "board": "team", "orchestrator": "lead"}


def detail():
    return {"id": RID, "tree": [{"id": RID, "role": "root"}, {"id": "t_2000bbbb", "role": "revision"}]}


def rec():
    return {"root_id": RID, "extra_cards": [{"id": "t_3000cccc", "kind": "note"}]}


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-usage-")
        self.base = Path(self.tmp.name)
        self.cfg = FakeCfg(self.base)
        self.state = self.base / "state.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _prepare(self):
        make_board(self.cfg.kanban, [
            ("t_1000aaaa", "owner_admitted", {"db": str(self.state), "session_id": "s1"}),
            ("t_2000bbbb", "owner_admitted", {"db": str(self.state), "session_id": "s2"}),
            ("t_3000cccc", "owner_admitted", {"db": str(self.state), "session_id": "s3"}),
            ("t_3000cccc", "reclaimed", {"db": str(self.state), "session_id": "s9"}),
        ])
        make_state(self.state, [
            ("s1", "model-a", 8, 100, 120000, 31000, 4000, 0, 0.31, "estimated", "h1"),
            ("s2", "model-a", 5, 60, 80000, 32000, 2500, 0, 0.19, "estimated", "h2"),
            ("s3", "model-a", 2, 20, 15000, 30000, 900, 0, 0.05, "estimated", "h1"),
        ])

    def test_sessions_are_found_through_owner_admitted(self):
        self._prepare()
        events = usage_mod.sessions_of(self.cfg.kanban, [RID, "t_2000bbbb"])
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds.count("owner_admitted"), 2, "по карточкам нашлись обе сессии")
        self.assertNotIn("t_3000cccc", [e["task_id"] for e in events], "чужая карточка не попала")

    def test_usage_sums_tokens_cost_and_counts_mismatched(self):
        self._prepare()
        got = usage_mod.usage(self.cfg, room(), RID, detail=detail(), rec=rec(),
                              state_dir=str(self.base))
        self.assertEqual(got["source"], "state.db")
        self.assertEqual(got["calls"], 15, "8 + 5 + 2 вызова")
        self.assertEqual(got["tokens"]["cache_read"], 215000)
        self.assertEqual(got["tokens"]["cache_write"], 93000)
        self.assertAlmostEqual(got["cost_usd"], 0.55, places=6)
        self.assertEqual(got["mismatched"]["attempts"], 1, "попытка reclaimed — «не та модель»")
        self.assertIn("t_3000cccc", got["mismatched"]["cards"])
        self.assertEqual(got["cards"], 3)

    def test_line_is_short_and_has_the_parts(self):
        self._prepare()
        got = usage_mod.usage(self.cfg, room(), RID, detail=detail(), rec=rec(),
                              state_dir=str(self.base))
        line = got["line"]
        self.assertTrue(line.startswith("Расход: "))
        self.assertIn("выз.", line)
        self.assertIn("тыс. токенов", line)
        for part in ("база", "тело", "инструменты"):
            self.assertIn(part, line, "в строке есть разбивка: %s" % part)
        self.assertLess(len(line), 200, "строка влезает в подпись карточки")

    def test_split_adds_up_to_the_total(self):
        self._prepare()
        got = usage_mod.usage(self.cfg, room(), RID, detail=detail(), rec=rec(),
                              state_dir=str(self.base))
        split = got["split"]
        total = sum(got["tokens"].values())
        self.assertLessEqual(split["base"] + split["body"] + split["tools"], total + 1,
                             "разбивка не больше суммы токенов")
        self.assertGreater(split["tools"], 0, "инструменты отделены от базы")

    def test_databases_are_opened_read_only(self):
        self._prepare()
        before = hashlib.sha256(self.state.read_bytes()).hexdigest()
        usage_mod._BASE_CACHE.clear()
        usage_mod.usage(self.cfg, room(), RID, detail=detail(), rec=rec(), state_dir=str(self.base))
        after = hashlib.sha256(self.state.read_bytes()).hexdigest()
        self.assertEqual(before, after, "state.db после замера не изменилась")
        self.assertGreater(usage_mod.base_estimate(str(self.base))["base_tokens"], 0)

    def test_sandbox_without_sessions_uses_fixture(self):
        self.cfg.fixtures_dir.mkdir(parents=True, exist_ok=True)
        fixture = {"cards": 2, "calls": 9, "tokens": {"input": 10, "cache_read": 1000,
                                                     "cache_write": 500, "output": 100},
                   "cost_usd": 0.02, "split": {"base": 400, "body": 300, "tools": 200},
                   "models": [], "mismatched": {"attempts": 0, "cards": []}}
        (self.cfg.fixtures_dir / "request-usage.json").write_text(
            json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        got = usage_mod.usage(self.cfg, room(), RID, detail=detail(), rec=rec(),
                              state_dir=str(self.base))
        self.assertEqual(got["source"], "fixtures")
        self.assertEqual(got["calls"], 9)
        self.assertIn("Расход:", got["line"])
        self.assertTrue(got["warnings"], "интерфейс видит, что данные — из фикстуры")

    def test_no_data_is_honest_not_zero_cost(self):
        got = usage_mod.usage(self.cfg, room(), RID, detail={"id": RID}, rec={"root_id": RID},
                              state_dir=str(self.base))
        self.assertEqual(got["source"], "none")
        self.assertIn("нет данных", got["line"])

    def test_card_ids_are_unique_and_ordered(self):
        ids = usage_mod.card_ids({"id": RID, "tree": [{"id": RID}, {"id": "t_2"}, {}]},
                                {"root_id": RID, "extra_cards": [{"id": "t_3"}, {"id": "t_2"}]})
        self.assertEqual(ids, [RID, "t_2", "t_3"])

    def test_route_is_registered_once(self):
        routes = [(m, p, h) for m, p, h in usage_mod.ROUTES]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0][0], "GET")
        self.assertIn("/api/requests/", routes[0][1])
        self.assertIn("/usage", routes[0][1])


class InstallTest(unittest.TestCase):
    def test_install_adds_method_and_route(self):
        class App:
            pass

        routes: list = []
        usage_mod.install_usage_routes(App, routes)
        self.assertTrue(hasattr(App, "h_request_usage"))
        self.assertEqual(len(routes), 1)
        usage_mod.install_usage_routes(App, routes)
        self.assertEqual(len(routes), 1, "повторная установка не дублирует маршрут")


if __name__ == "__main__":
    unittest.main(verbosity=2)
