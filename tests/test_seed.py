"""Тесты песочницы (`tools/seed_sandbox.py` + `tests/helpers.py`).

Проверяется: все восемь состояний запроса создаются и читаются из SQLite,
реальная `kanban.db` доски `team` не меняется ни на байт, каталог песочницы
удаляется после выхода, защита от записи в реальную БД срабатывает.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock as umock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.helpers import (  # noqa: E402
    SANDBOX_ENV_KEYS,
    Sandbox,
    assert_real_untouched,
    real_db_path,
    real_hermes_home,
    snapshot_real_db,
)
from tools.seed_sandbox import BOARD, TENANT, build_sandbox  # noqa: E402

ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
STATES = ("queued", "working", "reviewing", "needs_you",
          "done_modern", "done_legacy", "failed", "candidate")
ENGINE_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "review",
                   "done", "blocked", "archived"}


class SeedSandboxTest(unittest.TestCase):
    """Одна песочница на весь класс: сборка занимает ~2 с."""

    @classmethod
    def setUpClass(cls):
        cls.real_before = snapshot_real_db()
        cls.sb = Sandbox()
        cls.sb.__enter__()
        cls.ids = cls.sb.ids

    @classmethod
    def tearDownClass(cls):
        cls.sb.__exit__(None, None, None)

    # --- общее --------------------------------------------------------------
    def test_all_states_present(self):
        for name in STATES:
            with self.subTest(state=name):
                state = self.ids[name]
                self.assertRegex(state["root_id"], ID_RE)
                self.assertIsInstance(state["child_ids"], list)
                self.assertIsInstance(state["extra_cards"], dict)
        self.assertEqual(self.ids["board"], BOARD)
        self.assertEqual(self.ids["tenant"], TENANT)

    def test_paths_and_env(self):
        paths = self.ids["paths"]
        for key in ("kanban_home", "data", "vault", "db", "env"):
            self.assertIn(key, paths)
        root = Path(self.sb.root)
        for key in ("kanban_home", "data", "vault"):
            self.assertTrue(Path(paths[key]).is_dir(), key)
            self.assertTrue(Path(paths[key]).resolve().is_relative_to(root), key)
        self.assertEqual(Path(paths["db"]).name, "kanban.db")
        self.assertTrue(Path(paths["db"]).resolve().is_relative_to(root))
        for env_key in ("TEAMROOM_HERMES_HOME", "TEAMROOM_DATA", "TEAMROOM_VAULT_DIR"):
            self.assertEqual(paths["env"][env_key], paths[{"TEAMROOM_HERMES_HOME": "kanban_home",
                                                           "TEAMROOM_DATA": "data",
                                                           "TEAMROOM_VAULT_DIR": "vault"}[env_key]])

    def test_cfg_points_into_sandbox(self):
        cfg = self.sb.cfg
        self.assertTrue(cfg.sandbox)
        self.assertTrue(Path(cfg.kanban_db(BOARD)).resolve().is_relative_to(Path(self.sb.root)))
        self.assertEqual(Path(cfg.data_dir), self.sb.data_dir)
        self.assertEqual(Path(cfg.vault_dir), self.sb.vault_dir)
        self.assertFalse(Path(self.sb.root).resolve().is_relative_to(real_hermes_home()))
        self.assertFalse(Path(cfg.kanban_home).resolve().is_relative_to(real_hermes_home()))

    def test_board_created_by_cli(self):
        board_json = self.sb.kanban_home / "kanban" / "boards" / BOARD / "board.json"
        self.assertTrue(board_json.is_file(), "доска должна создаваться CLI (board.json)")
        meta = json.loads(board_json.read_text(encoding="utf-8"))
        self.assertEqual(meta["slug"], BOARD)

    def test_all_statuses_are_engine_statuses(self):
        rows = self.sb.rows("SELECT DISTINCT status FROM tasks")
        statuses = {r["status"] for r in rows}
        self.assertTrue(statuses <= ENGINE_STATUSES, statuses - ENGINE_STATUSES)
        self.assertIn("running", statuses)
        self.assertIn("done", statuses)
        self.assertIn("triage", statuses)

    def test_tenant_on_room_cards_only(self):
        rows = self.sb.rows("SELECT tenant, count(*) c FROM tasks GROUP BY tenant")
        tenants = {r["tenant"]: r["c"] for r in rows}
        self.assertEqual(tenants.get(TENANT), 30)
        self.assertEqual(tenants.get(None), 3, "кандидат вне реестра — без tenant")

    # --- состояния ----------------------------------------------------------
    def test_queued(self):
        state = self.ids["queued"]
        root = self.sb.one("SELECT * FROM tasks WHERE id=?", (state["root_id"],))
        self.assertEqual(root["status"], "triage")
        self.assertEqual(root["tenant"], TENANT)
        self.assertEqual(state["child_ids"], [])
        self.assertEqual(self._children(state["root_id"]), [])

    def test_working(self):
        state = self.ids["working"]
        root = self.sb.one("SELECT status FROM tasks WHERE id=?", (state["root_id"],))
        self.assertEqual(root["status"], "todo")
        children = {r["id"]: r for r in self._children(state["root_id"])}
        self.assertEqual(len(children), 4)
        self.assertEqual(sorted(r["status"] for r in children.values()),
                         ["done", "ready", "running", "running"])
        # decompose-направление: каждый ребёнок — parent корня
        for child_id in state["child_ids"]:
            link = self.sb.one("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                               (child_id, state["root_id"]))
            self.assertIsNotNone(link, f"нет связи {child_id} → {state['root_id']}")

    def test_reviewing(self):
        state = self.ids["reviewing"]
        critic = state["extra_cards"]["critic"]
        self.assertEqual(self.sb.one("SELECT status FROM tasks WHERE id=?", (critic,))["status"],
                         "running")
        # критик создан как ребёнок корня (ARCH §12.4: create --parent <root>)
        self.assertIsNotNone(self.sb.one(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
            (state["root_id"], critic)))
        children = self._children(state["root_id"])
        self.assertEqual(len(children), 4)
        self.assertEqual(sum(1 for c in children if c["status"] == "done"), 3)

    def test_needs_you(self):
        state = self.ids["needs_you"]
        blocked = state["extra_cards"]["blocked_child"]
        row = self.sb.one("SELECT status FROM tasks WHERE id=?", (blocked,))
        self.assertEqual(row["status"], "blocked")
        meta = self._last_metadata(blocked)
        self.assertIn("open_questions", meta)
        self.assertEqual(meta["open_questions"][0]["for"], "mikhail")
        self.assertIn("канал", meta["open_questions"][0]["text"])
        self.assertEqual(state["extra_cards"]["question"], meta["open_questions"][0]["text"])

    def test_done_modern(self):
        state = self.ids["done_modern"]
        root = self.sb.one("SELECT status FROM tasks WHERE id=?", (state["root_id"],))
        self.assertEqual(root["status"], "done")
        meta = self._last_metadata(state["root_id"])
        for key in ("next_steps", "open_questions", "decisions", "stage",
                    "future_questions", "artifacts", "report"):
            self.assertIn(key, meta)
        self.assertEqual([p["id"] for p in meta["stage"]["phases"]],
                         ["idea", "legal", "brand", "packaging", "production", "sales"])
        self.assertTrue(all(isinstance(s, dict) and s.get("due") for s in meta["next_steps"]))
        # критик и доработка связаны с корнем
        self.assertIsNotNone(self.sb.one(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
            (state["root_id"], state["extra_cards"]["critic"])))
        self.assertIsNotNone(self.sb.one(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
            (state["extra_cards"]["critic"], state["extra_cards"]["revision"])))
        self.assertIsNotNone(self.sb.one(
            "SELECT 1 FROM tasks WHERE id=?", (state["extra_cards"]["synthesizer"],)))

    def test_done_modern_report(self):
        state = self.ids["done_modern"]
        report = Path(state["report_path"])
        self.assertTrue(report.is_file())
        self.assertTrue(report.resolve().is_relative_to(self.sb.data_dir))
        self.assertEqual(report.name, "report.json")
        self.assertIn(f"reports/cafe/{state['slug']}/report.json",
                      str(report).replace("\\", "/"))
        from reportkit.validate import load_file
        spec, errors, _warnings = load_file(str(report))
        self.assertEqual(errors, [])
        self.assertEqual(spec["version"], 1)
        self.assertGreater(len(spec["blocks"]), 3)
        self.assertEqual(self._last_metadata(state["root_id"])["report"], str(report))

    def test_done_modern_artifacts_exist(self):
        meta = self._last_metadata(self.ids["done_modern"]["root_id"])
        self.assertGreaterEqual(len(meta["artifacts"]), 2)
        for art in meta["artifacts"]:
            self.assertTrue(Path(art["path"]).is_file(), art["path"])
            self.assertIn(art["kind"], ("md", "json"))

    def test_done_legacy(self):
        state = self.ids["done_legacy"]
        meta = self._last_metadata(state["root_id"])
        self.assertIsInstance(meta["next_steps"][0], str)
        self.assertIsInstance(meta["open_questions"][0], str)
        self.assertIsInstance(meta["artifacts"][0], str)
        self.assertTrue(Path(meta["artifacts"][0]).is_file(), meta["artifacts"][0])
        self.assertTrue(meta["next_steps"][0].startswith("Ш1"))
        self.assertIn("stage", meta)
        self.assertNotIn("phases", meta["stage"], "legacy-stage — словарь «область → статус»")
        self.assertIn("production", meta["stage"])
        # нормализатор обязан понять этот формат
        from server.normalize import looks_legacy, normalize_metadata
        self.assertTrue(looks_legacy(meta))
        norm = normalize_metadata(meta)
        self.assertEqual(len(norm["next_steps"]), len(meta["next_steps"]))
        self.assertEqual([p["status"] for p in norm["stage"]["phases"]][:2], ["done", "blocked"])
        # W17-R §1: normalize больше не сворачивает алиасы — «ops» остаётся
        # отдельным этапом, поэтому в этом ответе 7 этапов, а не 6.
        self.assertEqual(len(norm["stage"]["phases"]), 7)
        # дерево: 4 среза + синтезатор
        self.assertEqual(len(self._children(state["root_id"])), 5)

    def test_failed(self):
        state = self.ids["failed"]
        child = state["extra_cards"]["failed_child"]
        row = self.sb.one("SELECT * FROM tasks WHERE id=?", (child,))
        self.assertEqual(row["status"], "blocked")
        self.assertGreaterEqual(row["consecutive_failures"], 2)
        self.assertIsNotNone(row["last_failure_error"])
        runs = self.sb.rows("SELECT status, outcome, error FROM task_runs WHERE task_id=?", (child,))
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r["status"] == "crashed" for r in runs))
        root = self.sb.one("SELECT * FROM tasks WHERE id=?", (state["root_id"],))
        self.assertIsNotNone(root["last_failure_error"])

    def test_candidate(self):
        state = self.ids["candidate"]
        self.assertFalse(state["extra_cards"]["in_registry"])
        event = self.sb.one("SELECT payload FROM task_events WHERE task_id=? AND kind='decomposed'",
                            (state["root_id"],))
        self.assertIsNotNone(event, "у кандидата должно быть событие decomposed")
        self.assertEqual(json.loads(event["payload"])["child_ids"], state["child_ids"])
        for task_id in [state["root_id"]] + state["child_ids"]:
            self.assertIsNone(self.sb.one("SELECT tenant FROM tasks WHERE id=?", (task_id,))["tenant"])
        registry = self.sb.data_dir / "state" / "cafe" / "requests.json"
        if registry.exists():
            ids = json.dumps(json.loads(registry.read_text(encoding="utf-8")))
            self.assertNotIn(state["root_id"], ids)

    def test_content_is_russian_cafe(self):
        titles = [r["title"] for r in self.sb.rows("SELECT title FROM tasks")]
        self.assertTrue(any("Лето" in t for t in titles))
        self.assertTrue(any("моти" in t for t in titles))
        self.assertTrue(any("Заозёрье" in t for t in titles))

    # --- защита реальной БД -------------------------------------------------
    def test_real_db_untouched_by_build(self):
        after = assert_real_untouched(self.real_before)
        self.assertTrue(after["exists"])
        self.assertGreater(after["tasks_count"], 0)
        self.assertEqual(set(after), {"exists", "tasks_count", "ours", "our_comments",
                                      "board_bytes", "board_mtime"})
        # наш след не вырос: ни карточек, ни комментариев от нашего приложения
        self.assertEqual(after["ours"], self.real_before["ours"])
        self.assertEqual(after["our_comments"], self.real_before["our_comments"])

    def test_assert_real_untouched_detects_drift(self):
        live = snapshot_real_db()
        # чужая запись Hermes (диспетчер, агент руководителя) — НЕ падение:
        # файл вырос и карточек стало больше, но нашего следа не прибавилось.
        grown = dict(live, tasks_count=live["tasks_count"] + 3,
                     board_bytes=live["board_bytes"] + 8192)
        with umock.patch("tests.helpers.snapshot_real_db", return_value=grown):
            self.assertEqual(assert_real_untouched(live)["tasks_count"], grown["tasks_count"])
        # файл доски исчез
        with umock.patch("tests.helpers.snapshot_real_db",
                         return_value={"exists": False, "tasks_count": 0, "ours": frozenset(),
                                       "our_comments": 0, "board_bytes": 0, "board_mtime": 0}):
            with self.assertRaises(AssertionError):
                assert_real_untouched(live)
        # наш код создал карточку в живой доске
        created = dict(live, tasks_count=live["tasks_count"] + 1,
                       ours=live["ours"] | {"t_0badc0de"})
        with umock.patch("tests.helpers.snapshot_real_db", return_value=created):
            with self.assertRaises(AssertionError):
                assert_real_untouched(live)
        # наш код написал комментарий в живую доску
        commented = dict(live, our_comments=live["our_comments"] + 1)
        with umock.patch("tests.helpers.snapshot_real_db", return_value=commented):
            with self.assertRaises(AssertionError):
                assert_real_untouched(live)

    def test_writable_db_guard(self):
        self.sb.assert_writable_db_guard()
        real = real_db_path(self.sb.cfg)
        self.assertEqual(real.name, "kanban.db")
        self.assertFalse(real.resolve().is_relative_to(Path(self.sb.root)))

    def test_real_db_is_not_sandbox_db(self):
        self.assertNotEqual(real_db_path(self.sb.cfg).resolve(), self.sb.db.resolve())

    # --- идемпотентность и уборка -------------------------------------------
    def test_second_build_returns_same_ids(self):
        again = build_sandbox(self.sb.root)
        self.assertEqual(again, self.ids)

    def test_non_empty_root_refused(self):
        extra = Path(self.sb.root) / "not-a-sandbox"
        extra.mkdir()
        (extra / "file.txt").write_text("мусор", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            build_sandbox(extra)

    def test_sandbox_inside_real_hermes_refused(self):
        import unittest.mock as mock
        fake_real = Path(self.sb.root)
        with mock.patch("tools.seed_sandbox._real_hermes_home", return_value=fake_real):
            with self.assertRaises(RuntimeError):
                build_sandbox(fake_real / "nested-sandbox")
        self.assertFalse((fake_real / "nested-sandbox").exists())

    def test_cleanup_removes_sandbox_and_env(self):
        import os
        before_env = {k: os.environ.get(k) for k in SANDBOX_ENV_KEYS}
        with Sandbox() as tmp:
            root = Path(tmp.root)
            self.assertTrue(root.is_dir())
            self.assertTrue(Path(tmp.ids["paths"]["db"]).is_file())
        self.assertFalse(root.exists(), "каталог песочницы должен удаляться")
        for key, value in before_env.items():
            self.assertEqual(os.environ.get(key), value, key)

    # --- вспомогательное ----------------------------------------------------
    def _children(self, root_id: str) -> list[sqlite3.Row]:
        return self.sb.rows(
            "SELECT id, status, assignee FROM tasks WHERE id IN ("
            " SELECT parent_id FROM task_links WHERE child_id=?"
            " UNION SELECT child_id FROM task_links WHERE parent_id=?)", (root_id, root_id))

    def _last_metadata(self, task_id: str) -> dict:
        row = self.sb.one(
            "SELECT metadata FROM task_runs WHERE task_id=? AND metadata IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 1", (task_id,))
        self.assertIsNotNone(row, f"у {task_id} нет run с metadata")
        return json.loads(row["metadata"])


if __name__ == "__main__":
    unittest.main()
