"""«Без тупиков»: недоговорные блокировки, автозакрытие, действия (W26-D1).

Проверяем три дефекта из разбора 18.11 (Vault: Prep-…-dead-ends-rail-duties.md §1):

* **T1** — недоговорная блокировка (`grantable:false`, `execute_code`) и жёсткий запрет
  (`floor`) НЕ бывают `pending`, не попадают в счётчики «нужно от вас» и всегда имеют
  действие;
* **T2** — блокировка завершённой/архивной карточки закрывается сама (ждать некому);
* **T3** — у недоговорной блокировки есть путь решения: `hint_agent` (подсказка в ветку
  карточки), `trust_profile` (доверие профилю на срок, с отзывом), `dismiss`.

Плюс сторожевой обход `tools/check_dead_ends.py`: у каждого пункта «ждёт вас» есть
действие, у завершённых карточек нет `pending`.

Всё — на ВЫМЫШЛЕННЫХ данных: поддельный домашний каталог Hermes (профиль ``ivan-petrov``,
кафе «Лето»), поддельные ``state.db`` и ``kanban.db``. Реальный
``%LOCALAPPDATA%\\hermes`` только читается (интерпретатор Hermes), записи — в песочницу.

    python -m unittest tests.test_dead_ends -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import approvals as ap            # noqa: E402
from server import today as today_mod         # noqa: E402
from tests.helpers import _outside_hermes_base  # noqa: E402

if str(PROJECT_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import check_dead_ends as cde                 # noqa: E402

PROFILE = "ivan-petrov"
LEAD = "anna-lead"
ROOM_ID = "r_leto"
BOARD = "team"
CARD = "t_1a2b3c4d"

CONFIG_BEFORE = """\
# профиль участника комнаты (вымышленный)
approvals:
  mode: smart
  single_query_mode: deny
agent:
  max_turns: 40
stt:
  language: ru
command_allowlist: []
custom_note: кафе «Лето»
"""

BLOCK_HEREDOC = json.dumps({
    "error": ("BLOCKED: Command flagged as dangerous (script execution via heredoc) but "
              "single-query mode (-q) runs without a user present to approve it. Find an "
              "alternative approach that avoids this command."),
    "status": "blocked", "exit_code": -1}, ensure_ascii=False)
BLOCK_CODE = json.dumps({
    "status": "error",
    "error": ("BLOCKED: execute_code runs arbitrary local Python (including subprocess calls "
              "that bypass shell-string approval checks). Single-query mode (-q) runs without a "
              "user present to approve it. Use normal tools instead.")}, ensure_ascii=False)
BLOCK_FLOOR = json.dumps({
    "error": ("BLOCKED (hardline): delete in root path. This is never allowed, even with "
              "approval."), "status": "blocked", "exit_code": -1}, ensure_ascii=False)

COMMAND = "python - <<'EOF'\nprint('отчёт по заявкам')\nEOF"
CALL_TERMINAL = json.dumps([{"function": {"name": "terminal",
                                          "arguments": json.dumps({"command": COMMAND})}}])
CALL_CODE = json.dumps([{"function": {"name": "execute_code",
                                      "arguments": json.dumps({"code": "print('отчёт')"})}}])


class FakeCfg:
    """Мини-конфиг приложения: только то, что читают разрешения."""

    def __init__(self, root: Path, data: Path, sandbox: bool = False) -> None:
        self.hermes_home = root
        self.kanban_home = root
        self.data_dir = data
        self.vault_dir = data / "Vault"
        self.port = 0
        self.host = "127.0.0.1"
        self.hermes_cli = ""
        self.sandbox = sandbox
        self.app_dir = PROJECT_ROOT
        self.allowed_roots: list = [data]
        self.vault_enabled = False
        self.board = BOARD

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def kanban_db(self, board: str = "") -> Path:
        if board in ("", "default"):
            return self.kanban_home / "kanban.db"
        return self.kanban_home / "kanban" / "boards" / board / "kanban.db"


def _db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path))


def _messages_schema(con) -> None:
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, "
                "timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at INTEGER, "
                "ended_at INTEGER, message_count INTEGER, tool_call_count INTEGER, "
                "last_activity_at INTEGER, profile_name TEXT, source TEXT)")


def _tasks_schema(con) -> None:
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT, "
                "title TEXT, created_at INTEGER, started_at INTEGER, body TEXT)")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")


def _add(con, sid: str, role: str, payload: str, *, call: str = "", tool: str = "",
         at: float = 0) -> None:
    con.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, "
                "timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, role, payload, call, tool, at or time.time()))
    con.commit()


class Case(unittest.TestCase):
    """Песочница: поддельный Hermes-дом, один запрос, одна карточка."""

    @classmethod
    def setUpClass(cls) -> None:
        base = _outside_hermes_base()
        cls.sandbox = Path(base) / ("dead-ends-%d-%d" % (os.getpid(), int(time.time() * 1000)))
        (cls.sandbox / "hermes" / "profiles" / PROFILE).mkdir(parents=True, exist_ok=True)
        (cls.sandbox / "hermes" / "profiles" / LEAD).mkdir(parents=True, exist_ok=True)
        (cls.sandbox / "data").mkdir(parents=True, exist_ok=True)
        cls.root = cls.sandbox / "hermes"
        cls.cfg = FakeCfg(cls.root, cls.sandbox / "data")
        cls.room = {"id": ROOM_ID, "title": "Заявки кафе «Лето»", "board": BOARD,
                    "orchestrator": LEAD,
                    "members": [{"profile": PROFILE, "role": "worker",
                                 "label": "Исполнитель по заявкам"}]}

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def setUp(self) -> None:
        ap.reset_python_cache()
        self.home = self.root / "profiles" / PROFILE
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.yaml").write_text(CONFIG_BEFORE, encoding="utf-8")
        (self.root / "profiles" / LEAD / "config.yaml").write_text(
            "approvals:\n  mode: smart\n", encoding="utf-8")
        state = self.home / "state.db"
        if state.exists():
            state.unlink()
        con = _db(state)
        _messages_schema(con)
        con.close()
        board_db = self.cfg.kanban_db(BOARD)
        if board_db.exists():
            board_db.unlink()
        con = _db(board_db)
        _tasks_schema(con)
        con.execute("INSERT INTO tasks (id, assignee, status, title, created_at, started_at) "
                    "VALUES (?, ?, 'running', ?, ?, ?)",
                    (CARD, PROFILE, "Собрать отчёт по заявкам", int(time.time()) - 600,
                     int(time.time()) - 540))
        con.commit()
        con.close()
        path = self.cfg.data_dir / "state" / ROOM_ID
        if path.exists():
            import shutil
            shutil.rmtree(path, ignore_errors=True)

    # --- помощники -------------------------------------------------------
    def session(self, blocks: list, *, final: str = "Готово: отчёт собран.",
                status: str = "running") -> str:
        """Сделать сессию карточки: вызовы ассистента + результаты блокировок."""
        sid = "s_%d" % (int(time.time() * 1000) % 10 ** 8)
        con = sqlite3.connect(str(self.home / "state.db"))
        now = time.time()
        con.execute("INSERT INTO sessions (id, started_at, last_activity_at, message_count, "
                    "tool_call_count, profile_name, source) VALUES (?, ?, ?, ?, ?, ?, 'cli')",
                    (sid, int(now) - 500, int(now), 4, 2, PROFILE))
        con.commit()
        _add(con, sid, "user", "Карточка %s: собери отчёт по заявкам за неделю" % CARD,
             at=now - 480)
        for kind, call, text in blocks:
            _add(con, sid, "assistant", "", call=call, at=now - 400)
            _add(con, sid, "tool", text, tool=kind, at=now - 399)
        _add(con, sid, "assistant", final, at=now - 300)
        con.close()
        self.set_card_status(status)
        return sid

    def set_card_status(self, status: str) -> None:
        con = sqlite3.connect(str(self.cfg.kanban_db(BOARD)))
        con.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, CARD))
        con.commit()
        con.close()

    def item(self, blocks: list, *, status: str = "running", final: str = "Готово: отчёт собран."):
        self.session(blocks, status=status, final=final)
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1, "ожидали одну строку блокировки")
        return items[0]


class T1NotPendingTests(Case):
    """T1: недоговорное и жёсткий запрет — не «ждёт вашего решения»."""

    def test_execute_code_is_not_pending(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        self.assertEqual(item["kind"], "execute_code")
        self.assertFalse(item["grantable"])
        self.assertEqual(item["mode"], "workaround")
        self.assertNotEqual(item["state"], "pending")
        self.assertEqual(item["state"], "workaround")
        self.assertNotIn("ждёт вашего решения", item["note"])
        self.assertIn("терминал", item["note"])
        self.assertTrue(item["actions"], "у недоговорной блокировки обязаны быть действия")
        self.assertEqual([a["id"] for a in item["actions"]],
                         ["hint_agent", "trust_profile", "dismiss"])
        trust = next(a for a in item["actions"] if a["id"] == "trust_profile")
        self.assertTrue(trust["ttl_required"], "срок доверия обязателен")
        self.assertEqual([o["id"] for o in trust["ttl_options"]], ["30", "card", "forever"])
        # счётчики: это НЕ «нужно от вас»
        self.assertEqual(ap.pending([item]), [])
        self.assertFalse(ap.needs_you(self.cfg, self.room, CARD, items=[item]))

    def test_floor_is_information_not_question(self) -> None:
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_FLOOR)])
        self.assertTrue(item["floor"])
        self.assertEqual(item["mode"], "floor")
        self.assertEqual(item["state"], "info")
        self.assertIn("жёсткий запрет", item["note"])
        self.assertNotIn("ждёт вашего решения", item["note"])
        self.assertEqual([a["id"] for a in item["actions"]], ["hint_agent", "dismiss"])
        self.assertEqual(ap.pending([item]), [])

    def test_grantable_still_pending_with_three_actions(self) -> None:
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        self.assertEqual(item["mode"], "grant")
        self.assertEqual(item["state"], "pending")
        self.assertEqual(item["note"], ap.PENDING_NOTE)
        self.assertEqual([a["id"] for a in item["actions"]], ["once", "always", "deny"])
        self.assertEqual(len(ap.pending([item])), 1)
        self.assertTrue(ap.needs_you(self.cfg, self.room, CARD, items=[item]))

    def test_mixed_request_counts_only_actionable(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("execute_code", CALL_CODE, BLOCK_CODE),
                      ("terminal", CALL_TERMINAL, BLOCK_FLOOR)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 3)
        self.assertEqual(len(ap.pending(items)), 1)
        self.assertEqual(len(ap.actionable(items)), 3, "действие есть у всех трёх строк")


class T2AutoCloseTests(Case):
    """T2: автозакрытие блокировок завершённых и архивных карточек."""

    def test_done_card_closes_pending(self) -> None:
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)], status="done")
        self.assertEqual(item["state"], "closed")
        self.assertEqual(item["closed_reason"], "card_finished")
        self.assertIn("завершена", item["note"])
        self.assertEqual(item["actions"], [], "история не «ждёт вас»")
        self.assertEqual(ap.pending([item]), [])
        self.assertFalse(ap.needs_you(self.cfg, self.room, CARD))

    def test_archived_and_failed_cards_close_too(self) -> None:
        for status in ("archived", "failed", "cancelled"):
            with self.subTest(status=status):
                item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)], status=status)
                self.assertEqual(item["state"], "closed")
                self.assertEqual(item["closed_reason"], "card_finished")

    def test_finished_card_with_explicit_block_closes_too(self) -> None:
        """Участник написал «BLOCKED» в итоге — всё равно закрыто: карточка не работает."""
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)], status="done",
                         final="BLOCKED: команда не выполнилась, нужен другой способ.")
        self.assertTrue(item["stopped"])
        self.assertEqual(item["state"], "closed")
        self.assertEqual(ap.pending([item]), [])

    def test_decision_is_not_overwritten_by_autoclose(self) -> None:
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        ap.decide(self.cfg, self.room, CARD, item["id"], "deny")
        self.set_card_status("done")
        again = ap.collect(self.cfg, self.room, CARD)[0]
        self.assertEqual(again["state"], "denied", "решение человека сильнее автозакрытия")

    def test_running_card_keeps_pending(self) -> None:
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)], status="running")
        self.assertEqual(item["state"], "pending")


class T3ActionsTests(Case):
    """T3: у недоговорной блокировки есть действие — и оно правда работает."""

    def test_hint_agent_writes_comment(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        calls: list = []

        def act(action, **kw):
            calls.append((action, kw))
            return {}

        payload = ap.decide(self.cfg, self.room, CARD, item["id"], "hint_agent", act=act)
        self.assertTrue(payload["ok"])
        self.assertEqual([c[0] for c in calls], ["comment"])
        text = calls[0][1]["text"]
        self.assertIn("execute_code недоступен в фоновой работе: сделай через файл и терминал",
                      text)
        self.assertIn("python файл.py", text)
        self.assertEqual(payload["approval"]["state"], "hinted")
        self.assertEqual(payload["approval"]["actions"], [])
        self.assertNotIn("ждёт вашего решения", payload["approval"]["note"])
        again = ap.collect(self.cfg, self.room, CARD)[0]
        self.assertEqual(again["state"], "hinted")
        self.assertFalse(ap.needs_you(self.cfg, self.room, CARD))
        journal = (self.cfg.state_dir / ROOM_ID / "journal.jsonl").read_text(encoding="utf-8")
        self.assertIn("подсказка участнику", journal)

    def test_hint_agent_without_channel_is_honest_422(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, item["id"], "hint_agent")
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "no_channel")
        self.assertEqual(ap.collect(self.cfg, self.room, CARD)[0]["state"], "workaround")

    def test_auto_hint_hints_workaround_only(self) -> None:
        self.session([("execute_code", CALL_CODE, BLOCK_CODE),
                      ("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        calls: list = []
        result = ap.auto_hint(self.cfg, self.room, CARD,
                              lambda action, **kw: calls.append((action, kw)) or {})
        self.assertEqual(result["hinted"], 1)
        self.assertEqual(len(calls), 1)
        items = {item["state"]: item for item in ap.collect(self.cfg, self.room, CARD)}
        self.assertIn("hinted", items)
        self.assertIn("pending", items, "разрешаемая блокировка ждёт человека, её не трогаем")

    def test_dismiss_closes_item(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        payload = ap.decide(self.cfg, self.room, CARD, item["id"], "dismiss")
        self.assertEqual(payload["approval"]["state"], "dismissed")
        self.assertIn("неактуальное", payload["approval"]["note"])
        again = ap.collect(self.cfg, self.room, CARD)[0]
        self.assertEqual(again["state"], "dismissed")
        self.assertEqual(ap.pending([again]), [])
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, item["id"], "dismiss")
        self.assertEqual(ctx.exception.http, 409)
        self.assertEqual(ctx.exception.code, "already_decided")

    # --- доверие профилю -------------------------------------------------
    def _stub_worker(self) -> list:
        """Подменить подпроцесс Hermes: пишем «как будто записали» и запоминаем запрос."""
        seen: list = []
        original = ap.run_worker

        def fake(cfg, request, timeout=0):
            seen.append(dict(request))
            if request.get("op") == "trust":
                return {"ok": True, "op": "trust", "previous": "deny", "value": "approve"}
            if request.get("op") == "untrust":
                return {"ok": True, "op": "untrust", "previous": "approve", "value": "deny"}
            return {"ok": False, "code": "failed", "message": "неожиданная операция"}

        ap.run_worker = fake
        self.addCleanup(setattr, ap, "run_worker", original)
        return seen

    def test_trust_requires_ttl(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, item["id"], "trust_profile")
        # W28-B (дефект 6): срок — не «кривое тело», а невыполнимое действие, поэтому
        # ответ 422 с причиной (было 400): интерфейс рисует причину в карточке наравне
        # с «жёсткий запрет»/«нельзя штатно». Код `ttl_required` не изменился.
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "ttl_required")
        self.assertEqual(ap.collect(self.cfg, self.room, CARD)[0]["state"], "workaround")

    def test_trust_writes_only_that_profile_on_request(self) -> None:
        seen = self._stub_worker()
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        payload = ap.decide(self.cfg, self.room, CARD, item["id"], "trust_profile",
                            ttl_min=30)
        self.assertEqual([r["op"] for r in seen], ["trust"])
        self.assertEqual(Path(seen[0]["home"]), self.home, "пишем только в профиль участника")
        self.assertEqual(payload["trust"]["profile"], PROFILE)
        self.assertEqual(payload["trust"]["scope"], "minutes")
        self.assertEqual(payload["trust"]["ttl_min"], 30)
        self.assertEqual(payload["approval"]["state"], "trusted")
        self.assertFalse(ap.needs_you(self.cfg, self.room, CARD))
        listed = ap.trusts(self.cfg, self.room)["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["profile"], PROFILE)
        self.assertEqual(listed[0]["label"], "30 мин.")
        # запись реестра позволяет отозвать запись в профиле
        out = ap.revoke_trust(self.cfg, PROFILE, room=self.room)
        self.assertTrue(out["ok"])
        self.assertEqual([r["op"] for r in seen], ["trust", "untrust"])
        self.assertEqual(seen[-1]["previous"], "deny")
        self.assertEqual(ap.trusts(self.cfg, self.room)["items"], [])

    def test_trust_card_scope_revoked_when_card_finishes(self) -> None:
        seen = self._stub_worker()
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        ap.decide(self.cfg, self.room, CARD, item["id"], "trust_profile", ttl="card")
        self.assertEqual(ap.trusts(self.cfg, self.room)["items"][0]["label"],
                         "до конца карточки")
        self.set_card_status("done")
        result = ap.sweep(self.cfg, self.room)
        self.assertEqual(result["trusts_revoked"], 1)
        self.assertIn("untrust", [r["op"] for r in seen])
        self.assertEqual(ap.trusts(self.cfg, self.room)["items"], [])

    def test_trust_expires_by_ttl(self) -> None:
        seen = self._stub_worker()
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        ap.decide(self.cfg, self.room, CARD, item["id"], "trust_profile", ttl_min=30)
        later = int(time.time()) + 31 * 60
        self.assertEqual(ap.sweep(self.cfg, self.room, now=later)["trusts_revoked"], 1)
        self.assertEqual(ap.trusts(self.cfg, self.room)["items"], [])
        self.assertIn("untrust", [r["op"] for r in seen])

    def test_trust_refused_for_floor_and_grantable(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_FLOOR),
                      ("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        items = ap.collect(self.cfg, self.room, CARD)
        floor = next(item for item in items if item["mode"] == "floor")
        grant = next(item for item in items if item["mode"] == "grant")
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, floor["id"], "trust_profile", ttl="forever")
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "floor")
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, grant["id"], "trust_profile", ttl="forever")
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "grantable")

    def test_ttl_is_validated(self) -> None:
        with self.assertRaises(ap.ApprovalError):
            ap.parse_ttl("когда-нибудь")
        with self.assertRaises(ap.ApprovalError):
            ap.parse_ttl(ttl_min=0)
        with self.assertRaises(ap.ApprovalError):
            ap.parse_ttl(ttl_min=10 ** 6)
        self.assertEqual(ap.parse_ttl("forever"), {"scope": "forever", "ttl_min": None})
        self.assertEqual(ap.parse_ttl(ttl_min=15), {"scope": "minutes", "ttl_min": 15})

    # --- регресс: обычное разрешение недоговорное не снимает ---------------
    def test_once_on_execute_code_still_422_and_names_actions(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, item["id"], "once")
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "not_grantable")
        self.assertIn("hint_agent", ctx.exception.detail.get("actions") or [])
        self.assertIn("Подсказать", ctx.exception.message)


class DeadEndsToolTests(Case):
    """Сторожевой обход: самопроверка и проверка настоящих строк из `collect`."""

    def test_selftest_passes(self) -> None:
        self.assertEqual(cde.selftest(), [])

    def test_collected_items_have_no_dead_ends(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("execute_code", CALL_CODE, BLOCK_CODE),
                      ("terminal", CALL_TERMINAL, BLOCK_FLOOR)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(cde.check_items(items, CARD), [])

    def test_closed_items_have_no_dead_ends(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)], status="done")
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(cde.check_items(items, CARD), [])

    def test_tool_catches_old_style_dead_end(self) -> None:
        """Как было ДО правки: pending без действий у завершённой карточки."""
        broken = {"id": "ap_x", "state": "pending", "grantable": False, "floor": False,
                  "card_state": "done", "description": "execute_code — произвольный код",
                  "note": "ждёт вашего решения", "actions": []}
        found = cde.check_items([broken], CARD)
        self.assertGreaterEqual(len(found), 3)
        reasons = " ".join(item["why"] for item in found)
        self.assertIn("действий нет", reasons)
        self.assertIn("завершённой карточки", reasons)
        self.assertIn("ждёт вашего решения", reasons)

    def test_detail_counter_checked(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        detail = {"id": CARD, "approvals": [item], "approvals_pending": 1, "needs_you": True}
        found = cde.check_detail(detail, CARD)
        self.assertTrue(found, "счётчик не должен считать недоговорную строку")
        self.assertIn("считает недоговорное", found[0]["why"])


class TodayCounterTests(Case):
    """Счётчик «Сегодня»: недоговорное в «нужно от вас» не попадает (W26-D1)."""

    class _Sources(today_mod.Sources):
        def __init__(self, approvals):
            self._approvals = approvals

        def rooms(self):
            return [{"id": ROOM_ID, "title": "Заявки кафе «Лето»"}]

        def requests(self, room):
            return [{"root_id": CARD, "title": "Отчёт по заявкам"}]

        def states(self, room):
            return {CARD: {"status": "needs_you"}}

        def approvals(self, room):
            return self._approvals

    def test_not_actionable_approval_is_not_needs_you(self) -> None:
        item = self.item([("execute_code", CALL_CODE, BLOCK_CODE)])
        row = {"request_id": CARD, "profile": PROFILE, "member_title": "Исполнитель",
               "description": item["description"], "at": item["at"],
               "state": item["state"], "mode": item["mode"],
               "actionable": bool(item["actions"]),
               "closed": bool(item.get("closed_reason"))}
        view = today_mod.build(self._Sources([row]), last_visit_at=None, now=int(time.time()))
        self.assertEqual([n for n in view["needs_you"] if n["kind"] == "approval"], [])
        self.assertEqual(view["counters"]["needs_you"], 0)

    def test_actionable_approval_is_counted(self) -> None:
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        row = {"request_id": CARD, "profile": PROFILE, "member_title": "Исполнитель",
               "description": item["description"], "at": item["at"],
               "state": item["state"], "mode": item["mode"],
               "actionable": True, "closed": False}
        view = today_mod.build(self._Sources([row]), last_visit_at=None, now=int(time.time()))
        self.assertEqual(len([n for n in view["needs_you"] if n["kind"] == "approval"]), 1)

    def test_closed_approval_is_not_counted(self) -> None:
        row = {"request_id": CARD, "profile": PROFILE, "member_title": "Исполнитель",
               "description": "запуск скрипта через heredoc", "at": int(time.time()),
               "state": "closed", "mode": "grant", "actionable": False, "closed": True}
        view = today_mod.build(self._Sources([row]), last_visit_at=None, now=int(time.time()))
        self.assertEqual([n for n in view["needs_you"] if n["kind"] == "approval"], [])


class RegistryShapeTests(Case):
    """Реестр остаётся совместимым: approvals/grants/trusts, старые записи живы."""

    def test_registry_roundtrip(self) -> None:
        item = self.item([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        ap.decide(self.cfg, self.room, CARD, item["id"], "deny")
        reg = ap._registry(self.cfg, ROOM_ID)
        self.assertEqual(sorted(reg), ["approvals", "grants", "trusts"])
        self.assertIn(item["id"], reg["approvals"])
        self.assertEqual(reg["trusts"], [])


class PostponedQuestionTests(unittest.TestCase):
    """W31-FIX4 №1: «Отложить» — законное состояние вопроса, но не чёрная дыра.

    На боевых данных проверка ругалась на `state: postponed` у вопроса задачи про
    платформу: состояние добавлено в волне 30 («Отложить», `server/stale.py`), а
    словарь §1 в проверке и в `docs/API.md` его не знал. Здесь — оба конца правила:
    отложенный вопрос с «Вернуть» проходит, без «Вернуть» — тупик.
    """

    def detail(self, actions):
        return {"id": CARD, "status": "working", "needs_you": False,
                "needs_you_count": {"questions": 0, "decisions": 0, "approvals": 0,
                                    "steps": 0, "blocked": 0, "total": 0},
                "open_questions": [{"id": "q_1", "state": "postponed", "needs_you": False,
                                    "blocking": False, "actions": actions,
                                    "text": "Прайс на кофе с собой: 150 или 180 ₽?"}]}

    def test_postponed_is_known_state(self) -> None:
        self.assertIn("postponed", cde.QUESTION_STATES)
        self.assertEqual(cde.check_detail(self.detail([{"id": "restore"}]), CARD), [])

    def test_postponed_without_restore_is_dead_end(self) -> None:
        found = cde.check_detail(self.detail([]), CARD)
        # Два сообщения об одном: «пункт без действия» и «отложенный без Вернуть».
        self.assertTrue([item for item in found if item["kind"] == "dead_end"], found)
        self.assertIn("Вернуть", " ".join(item["why"] for item in found))

    def test_postponed_is_not_allowed_for_decisions(self) -> None:
        """`postponed` — только у вопросов: у решения такой кнопки нет."""
        detail = {"id": CARD, "decisions": [{"id": "d_1", "state": "postponed",
                                             "actions": [{"id": "restore"}]}]}
        found = cde.check_detail(detail, CARD)
        self.assertTrue([item for item in found if item["kind"] == "state"], found)


class BlockedCardCounterTests(unittest.TestCase):
    """W31-FIX4 №2: «нужен ваш ответ», счётчик и кнопки — один набор пунктов.

    Боевой случай: у задачи про платформу все восемь вопросов отвечены, корневая
    карточка стоит заблокированной. Статус говорил «нужен ваш ответ», «Сегодня» и
    доска показывали один пункт, а `needs_you_count` — ноль; проверка объявляла
    тупик, хотя у карточки есть рабочие кнопки «Продолжить» и «Дать указание».
    """

    def detail(self, counter, blocked=1):
        card = {"id": CARD, "status": "blocked", "title": "Собрать меню на неделю",
                "assignee": "ivan-petrov",
                "actions": [{"id": "resume"}, {"id": "instruct"}, {"id": "dismiss"}]}
        return {"id": CARD, "status": "needs_you", "needs_you": True,
                "open_questions": [{"id": "q_1", "state": "answered", "needs_you": False,
                                    "answer_state": "applied"},
                                   {"id": "q_2", "state": "answered", "needs_you": False,
                                    "answer_state": "replied_text"}],
                "approvals": [{"id": "ap_1", "state": "closed", "card_state": "done",
                               "closed_reason": "card_finished", "actions": []}],
                "tree": [card],
                "needs_you_count": counter,
                "needs_you_blocked": blocked,
                "blocked_waiting": ([{"id": CARD, "title": "Собрать меню на неделю",
                                      "actions": [{"id": "resume"}]}] if blocked else [])}

    def test_blocked_card_makes_status_honest(self) -> None:
        counter = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0,
                   "blocked": 1, "total": 1}
        self.assertEqual(cde.check_detail(self.detail(counter), CARD), [])

    def test_old_counter_that_skips_blocked_card_is_caught(self) -> None:
        """Так отдавала сборка 1.8.0: статус «ждёт вас», счётчик 0."""
        counter = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0, "total": 0}
        found = cde.check_detail(self.detail(counter), CARD)
        self.assertTrue([item for item in found if item["kind"] == "counter"], found)
        self.assertIn("0", " ".join(item["why"] for item in found))

    def test_status_without_any_point_is_still_a_dead_end(self) -> None:
        counter = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0,
                   "blocked": 0, "total": 0}
        detail = self.detail(counter, blocked=0)
        detail["tree"] = [{"id": CARD, "status": "done"}]
        found = cde.check_detail(detail, CARD)
        self.assertTrue([item for item in found if "решать нечего" in item["why"]], found)

    def test_guarded_pause_is_not_a_point_for_human(self) -> None:
        counter = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0, "total": 0}
        detail = self.detail(counter, blocked=0)
        detail["tree"] = [{"id": CARD, "status": "blocked", "hold": {"by": "guard", "auto": True},
                           "actions": [{"id": "resume"}]}]
        found = cde.check_detail(detail, CARD)
        self.assertTrue([item for item in found if "решать нечего" in item["why"]], found)


if __name__ == "__main__":
    unittest.main()
