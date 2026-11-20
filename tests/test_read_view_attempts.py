"""W28-C2: попытки карточки — «ошибки», которых не было (server/read_view.py + engine_read).

Что проверяем (дефект 5, интерфейс):

* убитая контролем моделей попытка (`reclaimed`, причина «контроль моделей…») —
  НЕ ошибка: плашки нет, в счётчик «ошибок» она не попадает, карточка остаётся живой;
* красное — только по ИТОГУ карточки (`crashed/failed/timed_out/gave_up` без
  последующего `completed`);
* `attempts[]` — у каждой попытки свой итог, причина, модель, провайдер и число вызовов;
  модель берётся из сессии ЭТОЙ попытки, а не из карточки (там уже исправленный выбор);
* данные приёмки — форма реальной доски (юрист: glm-5.3-flash → deepseek; исследователь:
  anthropic · claude-sonnet-5-5, 3 вызова).

Запуск: ``python -m unittest tests.test_read_view_attempts -v`` из корня проекта.
Песочница теста — своя временная папка доски и поддельный дом Hermes (только чтение
файлов); реальная доска и реальный ``%LOCALAPPDATA%\\hermes`` не трогаются.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import config as config_mod   # noqa: E402
from server import read_view as rv        # noqa: E402
from server import safety                 # noqa: E402

#: `engine_read` импортируется лениво, внутри интеграционных тестов: он тянет
#: `server/contract.py`, который в общей копии правят параллельно, и синтаксическая
#: ошибка коллеги не должна ронять чистые тесты попыток.

GUARD_ERROR = "manual_reclaim: контроль моделей: неверная модель"


def run(rid, status, outcome, *, error=None, started=100, ended=200, profile="worker"):
    """Попытка в форме `task_runs` (метаданные — как у боевой доски)."""
    return {"id": rid, "profile": profile, "status": status, "outcome": outcome,
            "started_at": started, "ended_at": ended, "error": error, "metadata": "{}"}


def guard_run(rid, *, started=100, ended=117, profile="worker"):
    """Убитая сторожем попытка: `reclaimed` + причина «контроль моделей: неверная модель»."""
    return run(rid, "reclaimed", "reclaimed", error=GUARD_ERROR, started=started,
               ended=ended, profile=profile)


def ok_run(rid, *, started=200, ended=400, profile="worker"):
    return run(rid, "done", "completed", started=started, ended=ended, profile=profile)


class AttemptsPureTest(unittest.TestCase):
    """Чистые функции read_view: без БД, без диска."""

    def test_guard_reclaim_is_not_an_error(self):
        node = {"id": "t_1", "status": "done", "runs": [guard_run(152), ok_run(154)]}
        rv.apply_attempts([node])
        self.assertIsNone(node["error"], "убитая контролем попытка — не ошибка карточки")
        self.assertEqual([a["n"] for a in node["attempts"]], [1, 2])
        self.assertEqual([a["outcome"] for a in node["attempts"]], ["reclaimed", "completed"])
        self.assertTrue(node["attempts"][0]["guard"])
        self.assertEqual(node["attempts"][0]["reason"],
                         "остановлена контролем моделей: была не та модель")
        self.assertEqual(node["guard_stopped"], [1], "номер убитой попытки — для строки в интерфейсе")
        self.assertTrue(node["attempts"][-1]["final"], "итог — последняя попытка")

    def test_three_killed_attempts_do_not_make_the_request_failed(self):
        """Приёмка по реальному запросу: три убитых запуска и успешные итоговые."""
        nodes = [
            {"id": "t_lawyer", "status": "done", "runs": [guard_run(152, ended=117), ok_run(154)]},
            {"id": "t_surveyor", "status": "done", "runs": [guard_run(153, ended=120), ok_run(155)]},
            {"id": "t_critic", "status": "done", "runs": [guard_run(159, ended=117), ok_run(160)]},
        ]
        rv.apply_attempts(nodes)
        failed = [n for n in nodes if n["error"]]
        self.assertEqual(failed, [], "красных плашек нет — реальных сбоев не было")
        self.assertEqual(sum(len(n["guard_stopped"]) for n in nodes), 3, "три убитых попытки")

    def test_real_failure_after_guard_is_an_error(self):
        node = {"id": "t_2", "status": "failed",
                "runs": [guard_run(10), run(11, "failed", "failed", error="worker exited with code 1")]}
        rv.apply_attempts([node])
        self.assertIsNotNone(node["error"], "после убитой попытки карточка реально упала")
        self.assertEqual(node["error"]["code"], "failed")
        self.assertIn("worker exited", node["error"]["message"])

    def test_completed_after_failure_closes_the_error(self):
        node = {"id": "t_3", "status": "done",
                "runs": [run(20, "crashed", "crashed", error="EOFError"), ok_run(21)]}
        rv.apply_attempts([node])
        self.assertIsNone(node["error"], "успешный повтор снимает красное: у карточки есть итог")

    def test_failure_without_retry_stays_red(self):
        for outcome in ("crashed", "failed", "timed_out", "gave_up"):
            node = {"id": "t_4", "status": "failed",
                    "runs": [run(30, outcome, outcome, error="boom")]}
            rv.apply_attempts([node])
            self.assertIsNotNone(node["error"], f"итог {outcome} — это сбой")
            self.assertEqual(rv.final_result(node["runs"])[0], "error")

    def test_last_guard_attempt_leaves_card_without_error(self):
        """Карточку убили и вернули в очередь: не красное, а спокойная строка."""
        node = {"id": "t_5", "status": "ready", "runs": [guard_run(40, ended=115)]}
        rv.apply_attempts([node])
        self.assertIsNone(node["error"])
        self.assertEqual(node["guard_stopped"], [1])
        self.assertEqual(rv.final_result(node["runs"])[0], "open")

    def test_manual_reclaim_without_guard_words_is_not_an_error_but_not_a_guard_line(self):
        node = {"id": "t_6", "status": "ready",
                "runs": [run(50, "reclaimed", "reclaimed", error="manual_reclaim: пользователь")]}
        rv.apply_attempts([node])
        self.assertIsNone(node["error"])
        self.assertEqual(node["attempts"][0]["reason"], "остановлена и возвращена в очередь")
        self.assertFalse(node["attempts"][0]["guard"])
        self.assertNotIn("guard_stopped", node)

    def test_attempts_carry_facts_from_the_resolver(self):
        """Модель/провайдер/вызовы — у КАЖДОЙ попытки своё (это читает engine_read)."""
        facts = {
            152: {"model": "glm-5.3-flash", "provider": "ollama-cloud", "calls": 1},
            154: {"model": "deepseek-v4.1-flash:1017", "provider": "ollama-cloud", "calls": 35},
        }
        runs = [guard_run(152), ok_run(154)]
        nodes = [{"id": "t_7", "runs": runs}]
        rv.apply_attempts(nodes, lambda node, item: facts.get(item.get("id"), {}))
        first, second = nodes[0]["attempts"]
        self.assertEqual((first["model"], first["provider"], first["calls"]),
                         ("glm-5.3-flash", "ollama-cloud", 1))
        self.assertEqual((second["model"], second["provider"], second["calls"]),
                         ("deepseek-v4.1-flash:1017", "ollama-cloud", 35))
        self.assertEqual(first["duration_s"], 17)

    def test_zero_calls_is_a_fact_not_unknown(self):
        """Критика убили до первого запроса: «вызовов 0», а не «неизвестно»."""
        node = {"id": "t_8", "runs": [guard_run(159)]}
        rv.apply_attempts([node], lambda n, r: {"calls": 0})
        self.assertEqual(node["attempts"][0]["calls"], 0)

    def test_broken_resolver_does_not_break_reading(self):
        def boom(_node, _run):
            raise RuntimeError("сессии нет")
        node = {"id": "t_9", "runs": [ok_run(60)]}
        rv.apply_attempts([node], boom)            # не должно бросить
        self.assertEqual(node["attempts"][0]["model"], "")
        self.assertIsNone(node["error"])

    def test_attempt_outcome_words(self):
        self.assertEqual(rv.attempt_outcome({"status": "done", "outcome": "completed"}), "completed")
        self.assertEqual(rv.attempt_outcome({"status": "running"}), "running")
        self.assertEqual(rv.attempt_outcome({"status": "reclaimed", "outcome": "reclaimed"}), "reclaimed")
        self.assertEqual(rv.attempt_outcome({"outcome": "timeout"}), "timed_out")
        self.assertEqual(rv.attempt_outcome({}), "")
        self.assertTrue(rv.is_guard_reclaim({"outcome": "reclaimed", "error": GUARD_ERROR}))
        self.assertFalse(rv.is_guard_reclaim({"outcome": "reclaimed", "error": "manual: таймаут"}))

    def test_nodes_without_runs_stay_calm(self):
        node = {"id": "t_10", "status": "todo", "runs": []}
        rv.apply_attempts([node])
        self.assertEqual(node["attempts"], [])
        self.assertIsNone(node["error"])


# ---------------------------------------------------------------- интеграция ---

BOARD = "team"


class BoardSandbox:
    """Временная доска канбана и поддельный дом Hermes (сессии воркеров).

    Ничего не пишет в реальный ``%LOCALAPPDATA%\\hermes``: всё внутри ``tempfile``,
    который удаляется в ``stop()``.
    """

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="w28c2-"))
        self.hermes_home = self.root / "hermes"
        self.kanban_home = self.root / "kanban_home"
        self.db = self.kanban_home / "kanban" / "boards" / BOARD / "kanban.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        # W28-QA1: каталоги профилей — через safety.guarded_mkdir: скан tests/test_safety.py
        # справедливо запрещает сырой mkdir() по пути от hermes_home (правило «никаких
        # записей в реальный дом»). Здесь путь всё равно временный, но правило одно для всех.
        safety.guarded_mkdir(self.hermes_home / "profiles" / "lawyer", parents=True, exist_ok=True)
        safety.guarded_mkdir(self.hermes_home / "profiles" / "surveyor", parents=True, exist_ok=True)
        self._schema()
        self.cfg = config_mod.Cfg(
            hermes_home=self.hermes_home, kanban_home=self.kanban_home,
            data_dir=self.root / "data", vault_dir=self.root / "vault",
            sandbox=True, allowed_roots=[self.root],
            app_dir=PROJECT_ROOT, vault_enabled=False)

    def _schema(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.executescript(
                """
                CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
                    status TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER,
                    tenant TEXT, model_override TEXT, provider_override TEXT, reasoning_effort TEXT);
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
                    status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER,
                    summary TEXT, metadata TEXT, error TEXT);
                CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT,
                    body TEXT, created_at INTEGER);
                CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER,
                    kind TEXT, payload TEXT, created_at INTEGER);
                """
            )
            con.commit()
        finally:
            con.close()

    def session(self, profile: str, sid: str, *, model: str, provider: str | None, calls: int) -> None:
        path = self.hermes_home / "profiles" / profile / "state.db"
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, model TEXT, "
                        "billing_provider TEXT, estimated_cost_usd REAL, api_call_count INTEGER)")
            con.execute("INSERT INTO sessions (id, model, billing_provider, estimated_cost_usd, "
                        "api_call_count) VALUES (?,?,?,?,?)",
                        (sid, model, provider, 0.0, calls))
            con.commit()
        finally:
            con.close()

    def card(self, tid: str, *, title: str, status: str, assignee: str,
             model: str | None = None, provider: str | None = None,
             parent: str | None = None, tenant: str = "room:fn") -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute("INSERT INTO tasks (id, title, assignee, status, created_at, started_at, "
                        "completed_at, tenant, model_override, provider_override) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (tid, title, assignee, status, 1700000000, 1700000100, 1700000300,
                         tenant, model, provider))
            if parent:
                con.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)", (parent, tid))
            con.commit()
        finally:
            con.close()

    def attempt(self, rid: int, tid: str, profile: str, *, status: str, outcome: str,
                started: int, ended: int, error: str | None = None,
                session_id: str | None = None, meta: dict | None = None) -> None:
        con = sqlite3.connect(self.db)
        try:
            payload = meta if meta is not None else ({"worker_session_id": session_id} if session_id else {})
            con.execute("INSERT INTO task_runs (id, task_id, profile, status, outcome, started_at, "
                        "ended_at, summary, metadata, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, tid, profile, status, outcome, started, ended, "",
                         json.dumps(payload), error))
            if session_id:
                con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (tid, rid, "owner_admitted", json.dumps({"session_id": session_id}), started))
            con.commit()
        finally:
            con.close()

    def stop(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class EngineIntegrationTest(unittest.TestCase):
    """`engine_read.request_detail` на боевой форме: у убитых попыток своя модель."""

    def setUp(self) -> None:
        self.box = BoardSandbox()
        b = self.box
        b.card("t_root", title="Запрос", status="done", assignee="default")
        b.card("t_lawyer", title="Юридический разбор", status="done", assignee="lawyer",
               model="deepseek-v4.1-flash:1017", provider="ollama-cloud", parent="t_root")
        b.card("t_surveyor", title="Проверить факты", status="failed", assignee="surveyor",
               model="deepseek-v4.1-flash:1017", provider="ollama-cloud", parent="t_root")
        # юрист: убит на glm-5.3-flash, повтор на deepseek — карточка завершена
        b.session("lawyer", "s-lawyer-1", model="glm-5.3-flash", provider="ollama-cloud", calls=1)
        b.session("lawyer", "s-lawyer-2", model="deepseek-v4.1-flash:1017",
                  provider="ollama-cloud", calls=35)
        # исследователь: убит на anthropic (без id сессии в metadata — только событие),
        # повтор упал по-настоящему
        b.session("surveyor", "s-surveyor-1", model="claude-sonnet-5-5",
                  provider="anthropic", calls=3)
        b.session("surveyor", "s-surveyor-2", model="deepseek-v4.1-flash:1017",
                  provider="ollama-cloud", calls=9)
        b.attempt(152, "t_lawyer", "lawyer", status="reclaimed", outcome="reclaimed",
                  started=1000, ended=1017, error=GUARD_ERROR, meta={"prev_pid": 4321})
        b.attempt(154, "t_lawyer", "lawyer", status="done", outcome="completed",
                  started=1060, ended=1406, session_id="s-lawyer-2")
        b.attempt(153, "t_surveyor", "surveyor", status="reclaimed", outcome="reclaimed",
                  started=1000, ended=1020, error=GUARD_ERROR, meta={"prev_pid": 777})
        b.attempt(155, "t_surveyor", "surveyor", status="failed", outcome="failed",
                  started=1060, ended=1120, session_id="s-surveyor-2",
                  error="worker exited with code 1: KeyError('cost_packaging')")
        # убитые попытки: id сессии — только в событии допуска (как на боевой доске)
        con = sqlite3.connect(b.db)
        try:
            con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        ("t_lawyer", 152, "owner_admitted", json.dumps({"session_id": "s-lawyer-1"}), 1000))
            con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        ("t_surveyor", 153, "owner_admitted",
                         json.dumps({"session_id": "s-surveyor-1"}), 1000))
            con.commit()
        finally:
            con.close()

    def tearDown(self) -> None:
        self.box.stop()

    def _detail(self) -> dict:
        from server import engine_read as er
        return er.request_detail(self.box.cfg, BOARD, "t_root")

    def test_killed_attempt_is_calm_and_shows_its_own_model(self):
        detail = self._detail()
        nodes = {n["id"]: n for n in detail["tree"]}
        lawyer = nodes["t_lawyer"]
        self.assertIsNone(lawyer["error"], "убитая попытка не делает карточку ошибочной")
        first = lawyer["attempts"][0]
        self.assertEqual(first["outcome"], "reclaimed")
        self.assertTrue(first["guard"])
        self.assertEqual(first["model"], "glm-5.3-flash",
                         "модель ПОПЫТКИ, а не исправленный выбор карточки")
        self.assertEqual(first["provider"], "ollama-cloud")
        self.assertEqual(first["calls"], 1)
        self.assertEqual(lawyer["attempts"][1]["model"], "deepseek-v4.1-flash:1017")
        self.assertEqual(lawyer["attempts"][1]["calls"], 35)

    def test_label_uses_the_last_attempt(self):
        detail = self._detail()
        nodes = {n["id"]: n for n in detail["tree"]}
        sent = nodes["t_lawyer"].get("model_sent") or {}
        self.assertIn("deepseek-v4.1-flash:1017", str(sent.get("text") or ""),
                      "подпись «ушло на» — из последней попытки")

    def test_no_card_failed_warning_for_killed_attempts(self):
        detail = self._detail()
        failed = [w for w in detail.get("warnings") or [] if w.get("code") == "card_failed"]
        # у исследователя второй запуск упал по-настоящему — он в красном списке,
        # а карточка юриста (убитая попытка + успешный повтор) — нет
        ids = [i for w in failed for i in (w.get("detail") or {}).get("task_ids") or []]
        self.assertNotIn("t_lawyer", ids, "красного баннера по убитой попытке нет")
        self.assertIn("t_surveyor", ids, "настоящий сбой остаётся видимым")

    def test_real_failure_is_red_and_keeps_attempts(self):
        detail = self._detail()
        nodes = {n["id"]: n for n in detail["tree"]}
        surveyor = nodes["t_surveyor"]
        self.assertIsNotNone(surveyor["error"], "второй запуск упал по-настоящему")
        self.assertEqual(surveyor["error"]["code"], "failed")
        self.assertIn("cost_packaging", surveyor["error"]["message"])
        killed = surveyor["attempts"][0]
        self.assertEqual((killed["model"], killed["provider"], killed["calls"]),
                         ("claude-sonnet-5-5", "anthropic", 3))
        self.assertIn("card_failed", [w["code"] for w in detail.get("warnings") or []])

    def test_attempts_field_is_in_the_card_contract(self):
        detail = self._detail()
        for node in detail["tree"]:
            self.assertIn("attempts", node)
            for item in node["attempts"]:
                for key in ("n", "outcome", "reason", "model", "provider", "calls"):
                    self.assertIn(key, item, f"поле {key} в attempts[]")

    def test_real_board_is_read_only_and_untouched(self):
        """Тест-снимок: приёмка шла на копии — реальная БД этим тестом не меняется."""
        before = self.box.db.stat().st_mtime_ns
        self._detail()
        self.assertEqual(before, self.box.db.stat().st_mtime_ns, "чтение не пишет в доску")


if __name__ == "__main__":
    unittest.main()
