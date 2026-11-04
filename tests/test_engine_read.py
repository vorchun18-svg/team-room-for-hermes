"""Тесты чтения канбана (server/engine_read.py) на песочнице + реальная доска только на чтение.

Запуск: ``python -m unittest tests.test_engine_read -v`` из корня проекта.

Песочница — ``tests.helpers.Sandbox`` (наполняет ``tools.seed_sandbox.py``): 7 деревьев
Лето во всех состояниях запроса + карточка-кандидат вне реестра. Реальная доска
``team`` читается только для приёмки C (``t_605613e0``); ``assert_real_untouched``
доказывает, что тесты её не меняют.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import config as config_mod          # noqa: E402
from server import engine_read as er             # noqa: E402
from tests.helpers import Sandbox, real_db_path, snapshot_real_db  # noqa: E402
from tests.helpers import assert_real_untouched  # noqa: E402

BOARD = "team"
STATES = ("queued", "working", "reviewing", "needs_you", "done_modern", "done_legacy", "failed")
EXPECTED_STATUS = {"queued": "queued", "working": "working", "reviewing": "reviewing",
                   "needs_you": "needs_you", "done_modern": "done", "done_legacy": "done",
                   "failed": "failed"}
ENV_KEYS = ("TEAMROOM_HERMES_HOME", "TEAMROOM_DATA", "TEAMROOM_VAULT_DIR", "HERMES_KANBAN_HOME")


class EngineReadSandboxTest(unittest.TestCase):
    """Все состояния запроса на песочнице; каждый тест читает те же данные."""

    @classmethod
    def setUpClass(cls):
        cls._sbx = Sandbox()
        cls.sb = cls._sbx.__enter__()
        cls.cfg = cls.sb.cfg
        cls.ids = cls.sb.ids

    @classmethod
    def tearDownClass(cls):
        cls._sbx.__exit__(None, None, None)

    # ---------------------------------------------------------------- helpers
    def extra(self, state: str) -> list:
        """extra_cards реестра: критик/доработка конкретного дерева."""
        cards = self.ids[state].get("extra_cards") or {}
        return [{"id": tid, "kind": kind, "round": 1, "at": 0}
                for kind, tid in cards.items()
                if kind in ("critic", "revision") and isinstance(tid, str) and tid.startswith("t_")]

    def root(self, state: str) -> str:
        return self.ids[state]["root_id"]

    def detail(self, state: str, room=None) -> dict:
        return er.request_detail(self.cfg, BOARD, self.root(state), room=room, extra_cards=self.extra(state))

    def summary(self, state: str) -> dict:
        return er.request_summary(self.cfg, BOARD, self.root(state), extra_cards=self.extra(state))

    # ------------------------------------------------------------------ тесты
    def test_01_health_ok(self):
        h = er.health(self.cfg, BOARD)
        self.assertTrue(h["ok"], h)
        self.assertEqual(h["version"], config_mod.VERSION)
        self.assertIsInstance(h["time"], int)
        eng = h["engine"]
        self.assertTrue(eng["db_exists"])
        self.assertTrue(eng["schema_ok"])
        self.assertEqual(eng["schema_missing"], [])
        self.assertEqual(eng["board"], BOARD)
        self.assertTrue(eng["board_db"].lower().endswith("kanban.db"))
        self.assertGreater(eng["tasks"]["total"], 20)
        self.assertEqual(set(eng["gateway"]), {"running", "pid", "state", "age_s"})
        self.assertIsInstance(eng["dispatcher_ok"], bool)

    def test_02_health_missing_board_never_raises(self):
        h = er.health(self.cfg, "no-such-board")
        self.assertFalse(h["ok"])
        self.assertFalse(h["engine"]["db_exists"])
        self.assertIn("Нет файла БД", h["engine"]["error"])
        self.assertEqual(h["engine"]["schema_ok"], False)

    def test_03_statuses_all_states(self):
        for state in STATES:
            with self.subTest(state=state):
                got = self.summary(state)["status"]
                self.assertEqual(got, EXPECTED_STATUS[state])
                self.assertEqual(self.detail(state)["status"], EXPECTED_STATUS[state])

    def test_04_queued_shape(self):
        d = self.detail("queued")
        self.assertEqual(d["synthesis"]["state"], "pending")
        self.assertEqual(d["synthesis"]["version"], 0)
        self.assertEqual(d["synthesis"]["text_md"], "")
        self.assertEqual(len(d["tree"]), 1)
        self.assertEqual(d["tree"][0]["role"], "root")
        self.assertEqual(d["progress"]["total"], 0)
        self.assertEqual(d["progress"]["percent"], 0)
        self.assertIsNone(d["duration_s"])
        self.assertTrue(d["text"].startswith("Разбери, стоит ли Лето"))  # текст запроса дословно

    def test_05_working_progress_and_tree(self):
        d = self.detail("working")
        self.assertEqual(d["progress"], {"done": 1, "total": 4, "percent": 25,
                                         "running": ["biz-marketing", "biz-ops", "biz-strategy"]})
        self.assertEqual(d["tree"][0]["id"], self.root("working"))
        self.assertEqual({n["role"] for n in d["tree"]}, {"root", "worker"})
        roles = {n["assignee"]: n for n in d["tree"] if n["role"] == "worker"}
        self.assertEqual(roles["biz-ops"]["status"], "running")
        self.assertEqual(roles["biz-lawyer"]["status"], "done")
        self.assertTrue(roles["biz-lawyer"]["summary_md"].startswith("Срез 01-Legal"))
        self.assertIsNone(d["status_reason"])

    def test_06_reviewing_uses_critic_card(self):
        d = self.detail("reviewing")
        self.assertEqual(d["status"], "reviewing")
        self.assertIn("Критик", d["status_reason"])
        self.assertTrue(d["critic"]["enabled"])
        self.assertEqual(d["critic"]["state"], "reviewing")
        self.assertEqual(d["critic"]["task_id"], self.ids["reviewing"]["extra_cards"]["critic"])
        critic = [n for n in d["tree"] if n["role"] == "critic"]
        self.assertEqual(len(critic), 1)
        self.assertEqual(critic[0]["status"], "running")
        self.assertEqual(critic[0]["round"], 1)
        self.assertEqual(d["synthesis"]["state"], "ready")  # ответ уже есть, критик его проверяет
        self.assertEqual(d["progress"]["running"], ["biz-critic"])

    def test_07_needs_you_question_from_child(self):
        d = self.detail("needs_you")
        self.assertEqual(d["status"], "needs_you")
        self.assertTrue(d["needs_you"])
        self.assertIn("Ждёт решения Михаила", d["status_reason"])
        self.assertEqual(len(d["open_questions"]), 1)
        q = d["open_questions"][0]
        self.assertEqual(q["for"], "mikhail")
        self.assertFalse(q["answered"])
        self.assertIn("Какой канал", q["text"])
        blocked = [n for n in d["tree"] if n["status"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["assignee"], "biz-runner")
        self.assertIn("Остановился", blocked[0]["summary_md"])
        # вопрос лежит в metadata карточки-ребёнка, а не только в ответе корня
        self.assertTrue(blocked[0]["needs_human"] is False)
        human = er.needs_human(self.cfg, BOARD, [self.root("needs_you")])
        self.assertEqual([h["kind"] for h in human], ["question"])
        self.assertEqual(human[0]["question_id"], q["id"])

    def test_08_done_modern_versions_and_critic(self):
        d = self.detail("done_modern")
        self.assertEqual(d["status"], "done")
        synth = d["synthesis"]
        self.assertEqual(synth["state"], "ready")
        self.assertEqual(synth["version"], 2)
        self.assertTrue(synth["text_md"].startswith("Пересчитал"))
        self.assertFalse(synth["legacy"])
        self.assertEqual([(h["version"], h["kind"], h["verdict"]) for h in synth["history"]],
                         [(1, "initial", "revise"), (2, "revision", None)])
        self.assertEqual(synth["history"][0]["task_id"], self.root("done_modern"))
        self.assertEqual(synth["history"][1]["task_id"], self.ids["done_modern"]["extra_cards"]["revision"])
        roles = {n["role"] for n in d["tree"]}
        self.assertTrue({"root", "worker", "synthesizer", "critic", "revision"} <= roles)
        self.assertTrue(d["critic"]["enabled"])
        self.assertEqual(d["critic"]["state"], "revised")
        self.assertEqual(d["critic"]["verdict"], "revise")
        self.assertEqual(len(d["critic"]["issues"]), 2)
        self.assertEqual(d["stage"]["current"], "packaging")
        self.assertEqual([p["status"] for p in d["stage"]["phases"]],
                         ["done", "done", "done", "active", "next", "next"])
        self.assertEqual(len(d["next_steps"]), 5)
        self.assertTrue(all(s["id"] and s["state"] in ("open", "asked", "done", "dismissed") for s in d["next_steps"]))
        self.assertTrue(d["report"]["available"])
        self.assertEqual(d["report"]["kind"], "spec")
        self.assertTrue(all(a["exists"] for a in d["artifacts"]), d["artifacts"])
        self.assertEqual(d["progress"]["percent"], 100)
        self.assertEqual(d["duration_s"], 56 * 60)

    def test_09_done_legacy_normalized(self):
        d = self.detail("done_legacy")
        synth = d["synthesis"]
        self.assertTrue(synth["legacy"])
        self.assertEqual(synth["version"], 1)
        self.assertTrue(synth["text_md"].startswith("Картина по «Лету»"))
        self.assertIn("legacy_metadata", [w["code"] for w in d["warnings"]])
        self.assertEqual(len(d["next_steps"]), 5)
        self.assertIsNone(d["next_steps"][0]["owner"])
        self.assertEqual(len(d["open_questions"]), 5)
        self.assertTrue(all(q["for"] == "mikhail" and not q["answered"] for q in d["open_questions"]))
        self.assertEqual(len(d["stage"]["phases"]), 6)
        self.assertEqual(d["stage"]["current"], "legal")
        self.assertEqual([p["status"] for p in d["stage"]["phases"]],
                         ["done", "blocked", "blocked", "active", "next", "next"])
        self.assertTrue(d["stage"]["phases"][0]["note"])
        bad = [a for a in d["artifacts"] if not a["exists"]]
        if bad:  # недоступный путь не роняет ответ, а даёт предупреждение
            self.assertIn("missing_artifact", [w["code"] for w in d["warnings"]])

    def test_10_failed(self):
        d = self.detail("failed")
        self.assertEqual(d["status"], "failed")
        self.assertIn("упал", d["status_reason"])
        self.assertIn("card_failed", [w["code"] for w in d["warnings"]])
        broken = [n for n in d["tree"] if n["error"]]
        self.assertTrue(broken)
        self.assertTrue(broken[0]["error"]["message"])
        self.assertEqual(broken[0]["error"]["code"], "crashed")
        self.assertEqual(d["progress"]["done"], 0)

    def test_11_candidates(self):
        registered = {self.root(s) for s in STATES}
        cands = er.list_candidates(self.cfg, BOARD, registered)
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand["task_id"], self.root("candidate"))
        self.assertEqual(cand["children"], 2)
        self.assertIn("поставщик заморозки", cand["title"])
        self.assertIsNone(cand["tenant"])
        self.assertEqual(er.list_candidates(self.cfg, BOARD, registered | {cand["task_id"]}), [])

    def test_12_list_roots_tenant_only(self):
        roots = er.list_roots(self.cfg, BOARD, tenant="room:cafe")
        self.assertEqual(len(roots), len(STATES))
        self.assertEqual({r["id"] for r in roots}, {self.root(s) for s in STATES})
        self.assertTrue(all(r["tenant"] == "room:cafe" for r in roots))
        stamps = [r["created_at"] for r in roots]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(len(er.list_roots(self.cfg, BOARD, tenant="room:cafe", limit=3)), 3)
        self.assertEqual(er.list_roots(self.cfg, BOARD, tenant="room:other"), [])
        self.assertEqual({r["id"] for r in er.list_roots(self.cfg, BOARD)}, {self.root(s) for s in STATES})

    def test_13_task_events_and_decomposed(self):
        events = er.task_events(self.cfg, BOARD, self.root("working"))
        kinds = [e["kind"] for e in events]
        self.assertIn("decomposed", kinds)
        dec = [e for e in events if e["kind"] == "decomposed"][0]
        self.assertTrue(dec["payload"]["child_ids"])
        self.assertEqual([e["id"] for e in events], sorted(e["id"] for e in events))
        if events:
            later = er.task_events(self.cfg, BOARD, self.root("working"), since_id=events[0]["id"])
            self.assertTrue(all(e["id"] > events[0]["id"] for e in later))
        self.assertTrue(er.is_decomposed(self.cfg, BOARD, self.root("working")))
        self.assertFalse(er.is_decomposed(self.cfg, BOARD, self.root("queued")))
        self.assertEqual(er.root_assignee_status(self.cfg, BOARD, self.root("working")),
                         {"assignee": "biz-director", "status": "todo", "running": False})
        self.assertEqual(er.root_assignee_status(self.cfg, BOARD, self.root("reviewing"))["status"], "done")

    def test_14_team_state(self):
        members = [{"profile": "biz-ops", "title": "Операции", "role": "worker"},
                   {"profile": "biz-lawyer", "role": "worker"},
                   {"profile": "biz-critic", "role": "critic", "pinned": True},
                   {"profile": "biz-nobody", "role": "observer"}]
        team = {m["profile"]: m for m in er.team_state(self.cfg, BOARD, members, tenant="room:cafe")}
        self.assertEqual(set(team), {m["profile"] for m in members})
        ops = team["biz-ops"]
        self.assertEqual(ops["state"], "working")
        self.assertEqual(ops["current"]["task_id"], er.team_state(
            self.cfg, BOARD, [{"profile": "biz-ops"}], tenant="room:cafe")[0]["current"]["task_id"])
        self.assertIn("Срез 03-Ops", ops["current"]["title"])
        self.assertIsInstance(ops["current"]["since"], int)
        self.assertGreaterEqual(ops["load"], 1)
        self.assertEqual(ops["counts"]["active"], 1)
        lawyer = team["biz-lawyer"]
        self.assertEqual(lawyer["state"], "idle")
        self.assertIsNotNone(lawyer["last"])
        self.assertTrue(lawyer["last"]["summary"])
        self.assertGreater(lawyer["counts"]["done"], 0)
        self.assertEqual(team["biz-critic"]["role"], "critic")
        self.assertTrue(team["biz-critic"]["pinned"])
        runner = er.team_state(self.cfg, BOARD, [{"profile": "biz-runner"}], tenant="room:cafe")[0]
        self.assertEqual(runner["state"], "waiting")
        self.assertEqual(er.team_state(self.cfg, BOARD, [], tenant="room:cafe"), [])

    def test_15_outside_room_warning(self):
        room = {"members": [{"profile": "biz-lawyer"}, {"profile": "biz-ops"},
                            {"profile": "biz-marketing"}, {"profile": "biz-strategy"}],
                "orchestrator": "biz-director", "critic": "biz-critic"}
        d = self.detail("done_modern", room=room)
        outside = [n for n in d["tree"] if n["outside_room"]]
        self.assertEqual({n["assignee"] for n in outside}, {"biz-cfo"})
        codes = [w["code"] for w in d["warnings"]]
        self.assertIn("outside_room", codes)
        warning = [w for w in d["warnings"] if w["code"] == "outside_room"][0]
        self.assertIn("biz-cfo", warning["message"])
        self.assertTrue(all(not n["outside_room"] for n in self.detail("done_modern")["tree"]))

    def test_16b_accepts_room_id_and_profile_strings(self):
        d = er.request_detail(self.cfg, BOARD, self.root("done_modern"), room="cafe",
                              extra_cards=self.extra("done_modern"))
        self.assertEqual(d["status"], "done")
        self.assertIsInstance(d["warnings"], list)
        team = er.team_state(self.cfg, BOARD, ["biz-ops", "biz-lawyer"], tenant="room:cafe")
        self.assertEqual([m["profile"] for m in team], ["biz-ops", "biz-lawyer"])
        self.assertEqual(team[0]["role"], "worker")
        self.assertEqual(team[0]["state"], "working")

    def test_16_dispatcher_status_shape(self):
        disp = er.dispatcher_status(self.cfg)
        self.assertEqual({"ok", "running", "pid", "state", "age_s", "warning"} - set(disp), set())
        self.assertIn(disp["state"], ("running", "stopped", "stale", "unknown"))
        self.assertIsInstance(disp["running"], bool)
        if not disp["running"]:
            self.assertTrue(disp["warning"])

    def test_17_summary_matches_detail(self):
        for state in STATES:
            with self.subTest(state=state):
                s, d = self.summary(state), self.detail(state)
                for key in ("id", "title", "status", "status_reason", "mode", "created_at", "started_at",
                            "finished_at", "duration_s", "progress", "assignee", "needs_you", "preview",
                            "archived", "room"):
                    self.assertEqual(s[key], d[key], key)
                self.assertEqual(s["mode"], "auto")
                # summary.critic — краткий объект (решение архитектора), detail.critic — полный (§12.5)
                self.assertIsInstance(s["critic"], dict)
                self.assertEqual(set(s["critic"]), {"enabled", "state", "verdict", "round"})
                self.assertIn(s["critic"]["state"],
                              ("off", "waiting", "reviewing", "approved", "revised", "failed"))
                self.assertIsInstance(d["critic"], dict)
                self.assertEqual(set(d["critic"]),
                                 {"enabled", "round", "max_rounds", "state", "verdict", "issues", "task_id"})
                self.assertTrue(s["preview"])
                self.assertLessEqual(len(s["preview"]), 200)
                self.assertNotIn("#", s["preview"])

    def test_18_no_db_raises_engine_error(self):
        for call in (lambda: er.request_detail(self.cfg, "nope", "t_x"),
                     lambda: er.request_summary(self.cfg, "nope", "t_x"),
                     lambda: er.list_roots(self.cfg, "nope"),
                     lambda: er.list_candidates(self.cfg, "nope", set()),
                     lambda: er.task_events(self.cfg, "nope", "t_x"),
                     lambda: er.is_decomposed(self.cfg, "nope", "t_x"),
                     lambda: er.team_state(self.cfg, "nope", [{"profile": "biz-ops"}])):
            with self.subTest(call=call):
                with self.assertRaises(er.EngineError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "no_db")
                self.assertIn("не найдена", ctx.exception.message)
                self.assertEqual(ctx.exception.as_dict()["code"], "no_db")

    def test_19_not_found_card(self):
        with self.assertRaises(er.EngineError) as ctx:
            er.request_detail(self.cfg, BOARD, "t_deadbeef")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_20_reads_do_not_write(self):
        db = Path(self.cfg.kanban_db(BOARD))
        before = (db.stat().st_size, db.stat().st_mtime_ns, self._counts())
        for state in STATES:
            self.detail(state)
        er.list_roots(self.cfg, BOARD)
        er.list_candidates(self.cfg, BOARD, set())
        er.needs_human(self.cfg, BOARD, [self.root(s) for s in STATES])
        er.team_state(self.cfg, BOARD, [{"profile": "biz-ops"}], tenant="room:cafe")
        after = (db.stat().st_size, db.stat().st_mtime_ns, self._counts())
        self.assertEqual(before, after)

    def _counts(self) -> tuple:
        con = self.sb.connect()
        try:
            return tuple(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                         for t in ("tasks", "task_runs", "task_comments", "task_events", "task_links"))
        finally:
            con.close()

    def test_21_detail_performance(self):
        times = []
        for _ in range(10):
            t0 = time.perf_counter()
            self.detail("done_modern")  # 8 карточек в дереве
            times.append((time.perf_counter() - t0) * 1000)
        median = sorted(times)[len(times) // 2]
        self.assertLess(median, 50, f"request_detail медиана {median:.1f} мс > 50 мс ({times})")


class EngineReadSchemaDriftTest(unittest.TestCase):
    """Дрейф схемы: нет таблицы / нет колонки — диагностика вместо падения."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teamroom-drift-", dir=str(Path(tempfile.gettempdir()))))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _cfg(self, board: str = BOARD):
        home = self.tmp / "kanban_home" / "kanban" / "boards" / board
        home.mkdir(parents=True, exist_ok=True)
        return config_mod.Cfg(hermes_home=self.tmp / "hermes", kanban_home=self.tmp / "kanban_home",
                              data_dir=self.tmp / "data", vault_dir=self.tmp / "vault", sandbox=True)

    def _db(self, *, with_events: bool, with_tenant: bool) -> Path:
        db = self.tmp / "kanban_home" / "kanban" / "boards" / BOARD / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        try:
            tenant_col = ", tenant TEXT" if with_tenant else ""
            con.executescript(f"""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
                    status TEXT, priority INTEGER DEFAULT 0, created_at INTEGER, started_at INTEGER,
                    completed_at INTEGER, created_by TEXT, idempotency_key TEXT,
                    consecutive_failures INTEGER DEFAULT 0, last_failure_error TEXT, max_retries INTEGER,
                    workspace_kind TEXT, workspace_path TEXT, block_kind TEXT{tenant_col});
                CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT,
                    status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT,
                    metadata TEXT, error TEXT);
                CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT,
                    body TEXT, created_at INTEGER);
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY (parent_id, child_id));
            """)
            if with_events:
                con.executescript("""
                    CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
                        kind TEXT, payload TEXT, created_at INTEGER);
                    INSERT INTO task_events (task_id, kind, payload, created_at)
                        VALUES ('t_00000001', 'decomposed', '{"child_ids": []}', 100);
                """)
            con.execute("INSERT INTO tasks (id, title, body, assignee, status, created_at, started_at, "
                        "completed_at) VALUES ('t_00000001', 'Дрейф схемы', 'Текст', 'biz-director', 'done', 10, 20, 30)")
            con.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, summary, "
                        "metadata) VALUES ('t_00000001', 'biz-director', 'done', 'completed', 20, 30, "
                        "'Ответ есть', '{\"next_steps\": [\"Шаг\"]}')")
            con.commit()
        finally:
            con.close()
        return db

    def test_30_missing_table(self):
        cfg = self._cfg()
        self._db(with_events=False, with_tenant=True)
        h = er.health(cfg, BOARD)
        self.assertFalse(h["ok"])
        self.assertFalse(h["engine"]["schema_ok"])
        self.assertIn("table:task_events", h["engine"]["schema_missing"])
        self.assertTrue(h["engine"]["db_exists"])
        with self.assertRaises(er.EngineError) as ctx:
            er.is_decomposed(cfg, BOARD, "t_00000001")
        self.assertEqual(ctx.exception.code, "schema_drift")
        with self.assertRaises(er.EngineError):
            er.list_candidates(cfg, BOARD, set())
        d = er.request_detail(cfg, BOARD, "t_00000001")  # без событий отвечает, не падает
        self.assertEqual(d["status"], "done")
        self.assertEqual(d["synthesis"]["text_md"], "Ответ есть")
        self.assertEqual(d["timeline"], [])

    def test_31_missing_column(self):
        cfg = self._cfg()
        self._db(with_events=True, with_tenant=False)
        h = er.health(cfg, BOARD)
        self.assertFalse(h["ok"])
        self.assertIn("column:tasks.tenant", h["engine"]["schema_missing"])
        self.assertEqual(er.list_roots(cfg, BOARD), [])          # фильтр по tenant невозможен — пусто
        self.assertEqual(er.list_roots(cfg, BOARD, tenant="room:cafe"), [])
        d = er.request_detail(cfg, BOARD, "t_00000001")          # остальное читается
        self.assertEqual(d["status"], "done")
        self.assertIsNone(d["room"])
        self.assertTrue(er.is_decomposed(cfg, BOARD, "t_00000001"))

    def test_32_no_db_dir(self):
        cfg = self._cfg("empty")
        h = er.health(cfg, "empty")
        self.assertFalse(h["ok"])
        self.assertIn("Нет файла БД", h["engine"]["error"])
        with self.assertRaises(er.EngineError):
            er.request_detail(cfg, "empty", "t_00000001")


class EngineReadRealBoardTest(unittest.TestCase):
    """Приёмка C: реальный завершённый запрос t_605613e0, доска только на чтение."""

    def _real_cfg(self):
        saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
        try:
            return config_mod.load()
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_40_real_request_readonly(self):
        cfg = self._real_cfg()
        db = real_db_path(cfg)
        if not db.is_file():
            self.skipTest(f"реальная БД недоступна: {db}")
        if Path(cfg.kanban_db(BOARD)) != db:
            self.skipTest("конфиг указывает не на реальную доску")
        detail = None
        before = snapshot_real_db(cfg)
        try:
            detail = er.request_detail(cfg, BOARD, "t_605613e0")
            after = snapshot_real_db(cfg)
            if after != before:  # живая доска могла измениться от диспетчера — перечитываем
                before = after
                detail = er.request_detail(cfg, BOARD, "t_605613e0")
                assert_real_untouched(before, cfg)
        finally:
            self.assertIsNotNone(detail)
        self.assertEqual(detail["status"], "done")
        self.assertTrue(detail["synthesis"]["text_md"].strip())
        self.assertEqual(detail["synthesis"]["state"], "ready")
        children = [n for n in detail["tree"] if n["id"] != "t_605613e0"]
        self.assertGreaterEqual(len(children), 4, [n["id"] for n in children])
        self.assertTrue(detail["synthesis"]["legacy"])
        self.assertTrue(detail["stage"]["phases"])
        self.assertEqual(len(detail["stage"]["phases"]), 6)
        self.assertIsNotNone(detail["stage"]["current"])
        self.assertTrue(detail["next_steps"])
        self.assertIn("legacy_metadata", [w["code"] for w in detail["warnings"]])
        self.assertEqual(detail["tree"][0]["id"], "t_605613e0")
        self.assertEqual(detail["tree"][0]["role"], "root")
        self.assertTrue(any(n["role"] == "synthesizer" for n in detail["tree"]))
        self.assertTrue(er.is_decomposed(cfg, BOARD, "t_605613e0"))
        health = er.health(cfg, BOARD)
        self.assertTrue(health["engine"]["db_exists"])
        assert_real_untouched(before, cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
