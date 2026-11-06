# -*- coding: utf-8 -*-
"""W13-F (§8): «Проверить доску сейчас» — итог прохода и состояние доски.

Что проверяем:
  * `server/kick_report.py` — счётчики и причины «почему работа не идёт»
    (готово к запуску / идёт / зависла / ждёт ответа / ждёт других задач);
  * `POST /api/dispatcher/kick {preview:true}` — предпросмотр доски: работает
    и в песочнице, НЕ зовёт CLI и не меняет файл доски;
  * `POST /api/dispatcher/kick` — итог прохода (`report`) в ответе: что запущено,
    кого вернули, чего ждать; ошибка итога не ломает сам проход.

Правила: только песочница (`tests.helpers.Sandbox`), CLI Hermes подменён shim'ом
(argv пишется, ничего не выполняется), реальная доска/Hermes не трогаются.

    python -m unittest tests.test_kick_report -v

# SHIM-OK: CLI Hermes подменяется CliShim; dispatch не выполняется никогда.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess as real_subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod              # noqa: E402
import server.engine_cli as engine_cli    # noqa: E402
import server.engine_write as ew          # noqa: E402
import server.kick_report as kr           # noqa: E402
from server.http_util import ApiError     # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

BOARD = "team"


class CliShim:
    """Подмена subprocess: записываем argv и НИЧЕГО не выполняем."""

    def __init__(self, dispatch_json: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.dispatch_json = dispatch_json or {"spawned": [], "promoted": 0, "reclaimed": 0}

    def __getattr__(self, name):
        return getattr(real_subprocess, name)

    def run(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb = ""
        try:
            start = argv.index("kanban") + 1
        except ValueError:
            start = 0
        skip = False
        for token in argv[start:]:
            if skip:
                skip = False
                continue
            if token in ("--board", "--home"):
                skip = True
                continue
            if token.startswith("-"):
                continue
            verb = token
            break
        out = json.dumps(self.dispatch_json) if verb == "dispatch" else ""
        return real_subprocess.CompletedProcess(argv, 0, out, "")

    def verbs(self) -> list[str]:
        out = []
        for argv in self.calls:
            tokens = [t for t in argv if not t.startswith("-")]
            out.append(tokens[4] if len(tokens) > 4 else "")
        return out


class _Base(unittest.TestCase):
    """Песочница + shim на класс; чистая доска на каждый тест — новый тенант."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.shim = CliShim()
        cls._real_cli_sub = engine_cli.subprocess
        cls._had_ew_sub = hasattr(ew, "subprocess")
        cls._real_ew_sub = getattr(ew, "subprocess", None)
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim

    @classmethod
    def tearDownClass(cls):
        engine_cli.subprocess = cls._real_cli_sub
        if cls._had_ew_sub:
            ew.subprocess = cls._real_ew_sub
        else:
            try:
                del ew.subprocess
            except AttributeError:
                pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.shim.calls.clear()
        ew.reset_dispatch_guard()
        self.agents = SimAgents(self.sb)

    # --- помощники ---------------------------------------------------------
    def card(self, title: str, status: str = "todo", assignee: str = "biz-ops",
             tenant: str = "room:cafe") -> str:
        return self.agents.card(title, assignee, status=status, tenant=tenant)

    def set_block(self, task_id: str, kind: str | None, **extra) -> None:
        """Выставить block_kind/heartbeat прямо в песочной БД (как это делает движок)."""
        con = self.agents.connect()
        try:
            con.execute("UPDATE tasks SET block_kind = ? WHERE id = ?", (kind, task_id))
            for key, value in extra.items():
                con.execute("UPDATE tasks SET %s = ? WHERE id = ?" % key, (value, task_id))
            con.commit()
        finally:
            con.close()

    def app(self, cfg=None):
        app = app_mod.App(cfg or self.cfg, port=0)
        app.call("rooms", "seed_defaults", ((cfg or self.cfg,), {}))
        return app

    def call(self, app, method: str, path: str, query=None, body=None):
        try:
            return app.dispatch(method, path, dict(query or {}), dict(body or {}))
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def db_hash(self) -> str:
        data = Path(self.cfg.kanban_db(BOARD)).read_bytes()
        return hashlib.sha256(data).hexdigest()

    def counts_now(self) -> dict:
        return dict(kr.board_state(self.cfg, BOARD)["counts"])

    def room_cards(self, state: dict | None = None) -> dict:
        state = state or kr.board_state(self.cfg, BOARD)
        return {card["id"]: card for card in state["cards"]}, state


class BoardStateTests(_Base):
    """Причины «почему не идёт» и признак «кнопка нужна»."""

    def test_ready_card_makes_button_useful(self):
        card = self.card("Готовая работа", status="ready")
        cards, state = self.room_cards()
        self.assertTrue(state["ok"], state)
        self.assertEqual(cards[card]["reason_code"], "ready")
        self.assertEqual(cards[card]["reason"], "готова к запуску")
        self.assertGreaterEqual(state["counts"]["ready"], 1)
        self.assertTrue(state["useful"])

    def test_blocked_cards_tell_who_waits(self):
        waiting_answer = self.card("Заблокирована вопросом", status="blocked")
        self.set_block(waiting_answer, "needs_input")
        waiting_decision = self.card("Заблокирована решением", status="blocked")
        self.set_block(waiting_decision, None)
        cards, state = self.room_cards()
        self.assertEqual(cards[waiting_answer]["reason_code"], "input")
        self.assertEqual(cards[waiting_answer]["reason"], "ждёт вашего ответа")
        self.assertEqual(cards[waiting_decision]["reason_code"], "blocked")
        self.assertEqual(cards[waiting_decision]["reason"], "ждёт решения")

    def test_dependencies_and_stuck_runs(self):
        deps = self.card("Ждёт других задач", status="todo")
        fresh = self.card("Идёт нормально", status="running")
        self.agents.start(fresh)
        stale = self.card("Зависла", status="running")
        self.agents.start(stale)
        self.set_block(stale, None, last_heartbeat_at=int(time.time()) - 3600)
        cards, state = self.room_cards()
        self.assertEqual(cards[deps]["reason_code"], "deps")
        self.assertEqual(cards[fresh]["reason_code"], "running")
        self.assertEqual(cards[stale]["reason_code"], "stuck")
        self.assertGreaterEqual(state["counts"]["stuck"], 1)

    def test_only_running_and_done_cards_are_not_reason_to_kick(self):
        before = self.counts_now()
        running = self.card("Идёт", status="running")
        self.agents.start(running)
        done = self.card("Готово", status="done")
        cards, state = self.room_cards()
        self.assertEqual(cards[running]["reason_code"], "running")
        self.assertNotIn(done, cards, "завершённые карточки не попадают в список ожидания")
        after = state["counts"]
        self.assertEqual(after["ready"] - before["ready"], 0)
        self.assertEqual(after["stuck"] - before["stuck"], 0)
        self.assertEqual(after["running"] - before["running"], 1)
        # признак «кнопка нужна» — ровно «есть готовые или зависшие»
        self.assertEqual(state["useful"], (after["ready"] + after["stuck"]) > 0)

    def test_other_tenants_are_ignored(self):
        before = self.counts_now()
        card = self.card("Чужая доска", status="ready", tenant=None)
        cards, state = self.room_cards()
        self.assertNotIn(card, cards)
        self.assertEqual(state["counts"]["ready"] - before["ready"], 0)

    def test_missing_board_degrades_without_raising(self):
        cfg = dataclasses.replace(self.cfg, kanban_home=Path(self.sb.root) / "no-board")
        state = kr.board_state(cfg, BOARD)
        self.assertFalse(state["ok"])
        self.assertTrue(state["error"])
        self.assertEqual(state["counts"]["ready"], 0)


class ReportTests(_Base):
    """Итог прохода: вид (kind), что запущено, чего ждать."""

    def test_preview_kind_and_useful(self):
        self.card("Готовая работа", status="ready")
        rep = kr.report(self.cfg, BOARD, {"preview": True})
        self.assertEqual(rep["kind"], "preview")
        self.assertTrue(rep["preview"])
        self.assertTrue(rep["useful"])
        self.assertEqual(rep["spawned"], 0)

    def test_started_cards_take_titles_from_board(self):
        card = self.card("Юр-проверка ИП", status="ready")
        rep = kr.report(self.cfg, BOARD, {"spawned": 1, "reclaimed": 0, "promoted": 0,
                                          "planned": [{"id": card, "assignee": "biz-ops"}],
                                          "max": 3})
        self.assertEqual(rep["kind"], "started")
        self.assertEqual(rep["spawned"], 1)
        self.assertEqual(rep["started"], [{"id": card, "assignee": "biz-ops",
                                           "title": "Юр-проверка ИП"}])

    def test_reclaimed_and_promoted_kinds(self):
        self.assertEqual(kr.report(self.cfg, BOARD, {"spawned": 0, "reclaimed": 2})["kind"],
                         "reclaimed")
        self.assertEqual(kr.report(self.cfg, BOARD, {"spawned": 0, "reclaimed": 0,
                                                     "promoted": 1})["kind"], "promoted")
        self.assertEqual(kr.report(self.cfg, BOARD, {})["kind"], "nothing")


class KickPreviewTests(_Base):
    """`POST /api/dispatcher/kick {preview:true}` — только чтение доски."""

    def test_preview_works_in_sandbox_and_does_not_touch_board(self):
        self.card("Готовая работа", status="ready")
        app = self.app()
        before = self.db_hash()
        status, payload, _ = self.call(app, "POST", "/api/dispatcher/kick", {}, {"preview": True})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["preview"])
        self.assertEqual(payload["report"]["kind"], "preview")
        self.assertGreaterEqual(payload["report"]["counts"]["ready"], 1)
        self.assertEqual(self.shim.calls, [], "предпросмотр не имеет права звать CLI")
        self.assertEqual(self.db_hash(), before, "предпросмотр не должен менять доску")

    def test_plain_kick_still_refused_in_sandbox(self):
        app = self.app()
        status, payload, _ = self.call(app, "POST", "/api/dispatcher/kick", {}, {})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "sandbox_no_dispatch")
        self.assertEqual(self.shim.calls, [])


class KickReportResponseTests(_Base):
    """Реальный проход (не песочница, CLI-shim) отдаёт итог в ответе."""

    def _app_outside_sandbox(self):
        cfg = dataclasses.replace(self.cfg, sandbox=False,
                                  hermes_home=Path(self.sb.root) / "fake_hermes")
        cfg.hermes_home.mkdir(parents=True, exist_ok=True)  # SAFETY-OK: внутри песочницы
        return self.app(cfg)

    def test_kick_returns_started_report(self):
        card = self.card("Смета упаковки", status="ready")
        self.shim.dispatch_json = {"spawned": [{"id": card, "assignee": "biz-ops"}],
                                   "promoted": 1, "reclaimed": 0}
        app = self._app_outside_sandbox()
        status, payload, _ = self.call(app, "POST", "/api/dispatcher/kick", {}, {"confirm": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["spawned"], 1)
        report = payload["report"]
        self.assertEqual(report["kind"], "started")
        self.assertEqual(report["promoted"], 1)
        self.assertEqual(report["started"][0]["id"], card)
        self.assertEqual(report["started"][0]["title"], "Смета упаковки")
        self.assertIn("counts", report)

    def test_nothing_started_explains_why(self):
        blocked = self.card("Ждёт ответа", status="blocked")
        self.set_block(blocked, "needs_input")
        self.shim.dispatch_json = {"spawned": [], "promoted": 0, "reclaimed": 0}
        app = self._app_outside_sandbox()
        status, payload, _ = self.call(app, "POST", "/api/dispatcher/kick", {}, {"confirm": True})
        self.assertEqual(status, 200, payload)
        report = payload["report"]
        self.assertEqual(report["kind"], "nothing")
        self.assertGreaterEqual(report["counts"]["input"], 1)
        codes = {card["id"]: card["reason_code"] for card in report["cards"]}
        self.assertEqual(codes.get(blocked), "input")

    def test_report_failure_does_not_break_pass(self):
        self.shim.dispatch_json = {"spawned": [], "promoted": 0, "reclaimed": 0}
        app = self._app_outside_sandbox()
        original = app.mod("kick_report").report
        app.mod("kick_report").report = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            status, payload, _ = self.call(app, "POST", "/api/dispatcher/kick", {},
                                           {"confirm": True})
        finally:
            app.mod("kick_report").report = original
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["report"], {})
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
