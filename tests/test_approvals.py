"""Разрешения агентам (WAVE22 §2, AP1): поиск блокировок, выдача, отзыв, снятие.

Всё — на ВЫМЫШЛЕННЫХ данных: поддельный домашний каталог Hermes (профиль
``ivan-petrov``, кафе «Лето»), поддельные ``state.db`` и ``kanban.db``. Реальный
``%LOCALAPPDATA%\\hermes`` не трогается: он нужен только чтобы найти интерпретатор
Hermes и его дерево (только чтение). Если их нет (чистая машина/CI) — тесты, которым
нужен Hermes, честно пропускаются.

    python -m unittest tests.test_approvals -v
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
from tests.helpers import _outside_hermes_base  # noqa: E402

PROFILE = "ivan-petrov"
LEAD = "anna-lead"
ROOM_ID = "r_leto"
BOARD = "team"
CARD = "t_1a2b3c4d"

#: Конфиг профиля с посторонними ключами: проверяем, что они не меняются.
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

#: Тексты блокировок — как их пишет Hermes в `messages.content` (role=tool).
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
    con = sqlite3.connect(str(path))
    return con


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


class ApprovalCase(unittest.TestCase):
    """Общая песочница: поддельный Hermes-дом, один запрос, одна карточка."""

    sandbox: Path
    cfg: FakeCfg
    room: dict

    @classmethod
    def setUpClass(cls) -> None:
        base = _outside_hermes_base()
        cls.sandbox = Path(base) / ("approvals-%d-%d" % (os.getpid(), int(time.time() * 1000)))
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

    # --- сборка поддельных данных ---------------------------------------
    def setUp(self) -> None:
        ap.reset_python_cache()
        self.home = self.root / "profiles" / PROFILE
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.yaml").write_text(CONFIG_BEFORE, encoding="utf-8")
        (self.root / "profiles" / LEAD / "config.yaml").write_text("approvals:\n  mode: smart\n",
                                                                   encoding="utf-8")
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
        for name in ("state",):
            path = self.cfg.data_dir / name / ROOM_ID
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
        # первая реплика пользователя — в ней движок пишет id карточки
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

    def read_config(self) -> dict:
        import yaml
        return yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8")) or {}

    def allowlist(self) -> list:
        return [str(v) for v in (self.read_config().get("command_allowlist") or [])]

    def hermes_ready(self) -> bool:
        return bool(ap.find_python(self.cfg))

    def skip_unless_hermes(self) -> None:
        if not self.hermes_ready():
            self.skipTest("нет интерпретатора Hermes на этой машине — сверка пропущена")


class FindTests(ApprovalCase):
    """Поиск блокировок: факты, идемпотентность, поля контракта."""

    def test_finds_blocked_terminal(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["state"], "pending")
        self.assertEqual(item["kind"], "terminal")
        self.assertEqual(item["pattern_key"], "script execution via heredoc")
        self.assertEqual(item["description"], "запуск скрипта через heredoc")
        self.assertIn("print('отчёт по заявкам')", item["command"])
        self.assertEqual(item["profile"], PROFILE)
        self.assertEqual(item["member_title"], "Исполнитель по заявкам")
        self.assertTrue(item["grantable"])
        self.assertFalse(item["floor"])
        self.assertTrue(item["id"].startswith("ap_"))
        self.assertEqual(len(item["id"]), 11)
        self.assertEqual(item["card_state"], "running")
        self.assertEqual(item["card_title"], "Собрать отчёт по заявкам")

    def test_same_block_twice_is_one_request_with_count(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 2)

    def test_same_reason_commands_are_one_row_with_counter(self) -> None:
        """W22-ST: разные команды ОДНОГО правила — одна строка со счётчиком и списком.

        Разрешение «на вид» выдаётся на класс команд (`command_allowlist` хранит
        `pattern_key`), поэтому десять `python -c …` — один вопрос. Последняя команда
        показывается в строке, остальные лежат в `commands[]` (раскрываемый список).
        """
        other = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
            {"command": "python -c \"print(2)\""})}}])
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("terminal", other, BLOCK_HEREDOC)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1, "одинаковая причина — одна строка")
        item = items[0]
        self.assertTrue(str(item.get("group") or "").startswith("gr_"))
        self.assertEqual(len(item["approval_ids"]), 2)
        self.assertEqual(len(item["commands"]), 2)
        self.assertEqual(item["count"], 2)
        self.assertEqual(item["command"], item["commands"][0]["command"],
                         "в строке — последняя команда")
        self.assertIn("print(2)", item["command"])
        self.assertIn("print('отчёт по заявкам')", item["commands"][1]["command"])
        self.assertEqual(item["state"], "pending")
        self.assertEqual(item["id"], item["approval_ids"][0],
                         "id строки — самый свежий запрос группы")

    def test_same_command_twice_is_counter_not_group(self) -> None:
        """Одна и та же команда дважды — счётчик «×2» без группы (`commands` не нужен)."""
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1)
        self.assertNotIn("group", items[0])
        self.assertNotIn("approval_ids", items[0])
        self.assertEqual(items[0]["count"], 2)

    def test_group_key_is_reason_profile_and_card(self) -> None:
        """Группа = правило + участник + карточка: остальное (команда) внутри строки."""
        key = ap.group_id(CARD, PROFILE, "script execution via heredoc")
        self.assertTrue(key.startswith("gr_"))
        self.assertEqual(key, ap.group_id(CARD, PROFILE, "script execution via heredoc"))
        self.assertNotEqual(key, ap.group_id(CARD, LEAD, "script execution via heredoc"))
        self.assertNotEqual(key, ap.group_id("t_9f9f9f9f", PROFILE,
                                             "script execution via heredoc"))
        self.assertNotEqual(key, ap.group_id(CARD, PROFILE, "script execution via -e/-c flag"))

    def test_execute_code_is_not_grantable(self) -> None:
        self.session([("execute_code", CALL_CODE, BLOCK_CODE)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "execute_code")
        self.assertEqual(items[0]["pattern_key"], "execute_code")
        self.assertEqual(items[0]["description"], "execute_code — произвольный код")
        self.assertFalse(items[0]["grantable"])
        self.assertFalse(items[0]["floor"])
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, items[0]["id"], "once")
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "not_grantable")
        self.assertEqual(self.allowlist(), [])

    def test_hardline_is_floor(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_FLOOR)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["floor"])
        self.assertFalse(items[0]["grantable"])
        self.assertIn("разрешить нельзя", items[0]["note"])
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, items[0]["id"], "always")
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "floor")
        self.assertEqual(self.allowlist(), [])

    def test_secrets_are_masked_in_command(self) -> None:
        """Пользователь видит команду целиком, но без секретов (одна маска на всё)."""
        command = ("python - <<'EOF'\n"
                   "import os\n"
                   "db = 'postgres://leto:секрет-в-адресе@localhost/orders'\n"
                   "print('свод')\n"
                   "EOF")
        call = json.dumps([{"function": {"name": "terminal",
                                         "arguments": json.dumps({"command": command})}}])
        self.session([("terminal", call, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        self.assertNotIn("секрет-в-адресе", item["command"])
        self.assertIn("import os", item["command"])           # остальной текст не съеден
        self.assertIn("print", item["command"])

    def test_needs_you_pending_running_card(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        self.assertTrue(ap.needs_you(self.cfg, self.room, CARD))

    def test_needs_you_finished_card_only_when_agent_says_blocked(self) -> None:
        """W26-D1 (T2): у завершённой карточки блокировка закрыта сама — жать нечего.

        Раньше строка «участник написал BLOCKED» держала `needs_you` вечно: разрешение
        на завершённой карточке ничего не запускает. Теперь пункт закрывается с
        состоянием `closed`, а человеку путь — «Доработать» (текст подписи).
        """
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)],
                     final="Отчёт готов.", status="done")
        self.assertFalse(ap.needs_you(self.cfg, self.room, CARD))
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)],
                     final="Не смог: Hermes BLOCKED команду, нужна ваша разрешение.",
                     status="done")
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertTrue(all(item["state"] == "closed" for item in items))
        self.assertIn("Доработать", items[0]["note"])
        self.assertFalse(ap.needs_you(self.cfg, self.room, CARD))

    def test_nothing_found_without_block(self) -> None:
        self.session([("terminal", CALL_TERMINAL,
                       json.dumps({"ok": True, "output": "готово"}))])
        self.assertEqual(ap.collect(self.cfg, self.room, CARD), [])


class DecideTests(ApprovalCase):
    """Выдача разрешения: запись в профиль, состояния, повторные нажатия."""

    def test_deny_needs_no_hermes_and_writes_nothing(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        before = (self.home / "config.yaml").read_text(encoding="utf-8")
        payload = ap.decide(self.cfg, self.room, CARD, item["id"], "deny", note="не нужно")
        self.assertEqual(payload["approval"]["state"], "denied")
        self.assertNotIn("grant", payload)
        self.assertEqual((self.home / "config.yaml").read_text(encoding="utf-8"), before)
        # повторное решение — 409, а не вторая запись
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, item["id"], "once")
        self.assertEqual(ctx.exception.http, 409)

    def test_unknown_approval_404(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, "ap_00000000", "once")
        self.assertEqual(ctx.exception.http, 404)

    def test_bad_decision_400(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, item["id"], "может быть")
        self.assertEqual(ctx.exception.http, 400)

    def test_no_interpreter_422_and_nothing_written(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        before = (self.home / "config.yaml").read_text(encoding="utf-8")
        old = os.environ.get("TEAMROOM_HERMES_PYTHON")
        os.environ["TEAMROOM_HERMES_PYTHON"] = ""       # «интерпретатора нет»
        ap.reset_python_cache()
        try:
            with self.assertRaises(ap.ApprovalError) as ctx:
                ap.decide(self.cfg, self.room, CARD, item["id"], "always")
        finally:
            if old is None:
                os.environ.pop("TEAMROOM_HERMES_PYTHON", None)
            else:
                os.environ["TEAMROOM_HERMES_PYTHON"] = old
            ap.reset_python_cache()
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "no_hermes")
        self.assertEqual((self.home / "config.yaml").read_text(encoding="utf-8"), before)

    def test_once_writes_allowlist_and_keeps_other_keys(self) -> None:
        self.skip_unless_hermes()
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        payload = ap.decide(self.cfg, self.room, CARD, item["id"], "once")
        self.assertEqual(payload["approval"]["state"], "granted_once")
        self.assertIn("script execution via heredoc", self.allowlist())
        self.assertEqual(payload["grant"]["scope"], "once")
        self.assertTrue(payload["grant"]["expires_at"])
        # посторонние ключи конфига не изменились
        after = self.read_config()
        before = {key: value for key, value in (__import__("yaml").safe_load(CONFIG_BEFORE) or {}).items()
                  if key != "command_allowlist"}
        kept = {key: value for key, value in after.items()
                if key not in ("command_allowlist", "_config_version")}
        self.assertEqual(kept, before)
        # состояние сохраняется в реестре комнаты
        reg = json.loads(ap.registry_path(self.cfg, ROOM_ID).read_text(encoding="utf-8"))
        self.assertIn(item["id"], reg["approvals"])
        grants = ap.grants(self.cfg, self.room)["items"]
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["source"], "room")
        self.assertEqual(grants[0]["scope"], "once")

    def test_always_stays_and_shows_in_settings(self) -> None:
        self.skip_unless_hermes()
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        payload = ap.decide(self.cfg, self.room, CARD, item["id"], "always")
        self.assertEqual(payload["approval"]["state"], "granted_always")
        self.assertEqual(payload["grant"]["scope"], "always")
        self.assertIsNone(payload["grant"]["expires_at"])
        # после завершения карточки постоянное разрешение остаётся
        self.set_card_status("done")
        ap.sweep(self.cfg, self.room)
        self.assertIn("script execution via heredoc", self.allowlist())
        grants = ap.grants(self.cfg, self.room)["items"]
        self.assertEqual([g["scope"] for g in grants], ["always"])

    def test_manual_key_shows_as_manual(self) -> None:
        lead_cfg = self.root / "profiles" / LEAD / "config.yaml"
        lead_cfg.write_text("approvals:\n  mode: smart\ncommand_allowlist:\n"
                            "  - recursive delete\n", encoding="utf-8")
        items = ap.grants(self.cfg, self.room)["items"]
        manual = [i for i in items if i["source"] == "manual"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["profile"], LEAD)
        self.assertEqual(manual[0]["pattern_key"], "recursive delete")
        self.assertEqual(manual[0]["description"], "рекурсивное удаление файлов")

    def test_comment_and_restart_via_engine_action(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        calls: list = []

        class Act:
            def __call__(self, action, **kw):
                calls.append((action, kw))
                return {"ok": True}

        ap.decide(self.cfg, self.room, CARD, item["id"], "deny", act=Act())
        self.assertEqual(calls[0][0], "comment")
        self.assertIn("запрещено", calls[0][1]["text"])
        self.assertIn("найди другой способ", calls[0][1]["text"])
        self.assertFalse([c for c in calls if c[0] == "retry"])

    def test_restart_only_for_stopped_cards(self) -> None:
        self.skip_unless_hermes()
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)], status="blocked")
        item = ap.collect(self.cfg, self.room, CARD)[0]
        calls: list = []

        def act(action, **kw):
            calls.append((action, kw))
            return {}

        payload = ap.decide(self.cfg, self.room, CARD, item["id"], "always", act=act)
        self.assertEqual([c[0] for c in calls], ["comment", "retry"])
        self.assertTrue(payload["restarted"])

    def test_finished_card_no_restart(self) -> None:
        """W26-D1 (T2): у завершённой карточки блокировка закрыта — решения по ней нет.

        Раньше разрешение записывалось «на будущее» и просило «Доработать»; теперь
        пункт просто закрыт автозакрытием, и путь один — «Доработать» (подпись строки),
        никакой записи в профиль без работающей карточки не происходит.
        """
        self.skip_unless_hermes()
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)], status="done")
        item = ap.collect(self.cfg, self.room, CARD)[0]
        self.assertEqual(item["state"], "closed")
        calls: list = []
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, item["id"], "always",
                      act=lambda action, **kw: calls.append((action, kw)) or {})
        self.assertEqual(ctx.exception.http, 409)
        self.assertEqual(ctx.exception.code, "already_decided")
        self.assertEqual([c[0] for c in calls], [], "ничего не пишем и не перезапускаем")
        self.assertNotIn("script execution via heredoc", self.allowlist())

    def test_group_decision_applies_to_all_and_is_atomic(self) -> None:
        """W22-ST: решение по строке-группе закрывает ВСЕ её запросы, а не первый."""
        other = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
            {"command": "python -c \"print(2)\""})}}])
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("terminal", other, BLOCK_HEREDOC)])
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1)
        group = items[0]
        calls: list = []

        def act(action, **kw):
            calls.append((action, kw))
            return {}

        payload = ap.decide(self.cfg, self.room, CARD, group["id"], "deny", act=act)
        self.assertEqual(payload["approval"]["state"], "denied")
        self.assertEqual(sorted(payload.get("applied") or []), sorted(group["approval_ids"]))
        # один комментарий и никакого перезапуска при запрете — на всю группу
        self.assertEqual([c[0] for c in calls], ["comment"])
        self.assertIn("всего команд: 2", calls[0][1]["text"])
        # ни один запрос группы не остался «ждущим»
        after = ap.collect(self.cfg, self.room, CARD)
        self.assertFalse([row for row in after if row["state"] == "pending"], after)
        self.assertEqual({row["state"] for row in after}, {"denied"})
        # реестр помнит решение по КАЖДОМУ запросу группы
        reg = ap._registry(self.cfg, ROOM_ID)  # noqa: SLF001 - проверка реестра
        for ident in group["approval_ids"]:
            self.assertIn(ident, reg["approvals"])
            self.assertEqual(reg["approvals"][ident]["state"], "denied")

    def test_group_decision_writes_one_grant(self) -> None:
        """«Разрешать всегда» по группе — одна запись правила, а не две."""
        self.skip_unless_hermes()
        other = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
            {"command": "python -c \"print(2)\""})}}])
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("terminal", other, BLOCK_HEREDOC)])
        group = ap.collect(self.cfg, self.room, CARD)[0]
        payload = ap.decide(self.cfg, self.room, CARD, group["id"], "always")
        self.assertEqual(payload["grant"]["scope"], "always")
        self.assertEqual(self.allowlist(), ["script execution via heredoc"])
        grants = ap.grants(self.cfg, self.room)["items"]
        self.assertEqual(len(grants), 1, grants)
        after = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual({row["state"] for row in after}, {"granted_always"})
        self.assertEqual(len(after), 2, "обе команды группы закрыты одним решением")

    def test_group_decision_closes_second_request_too(self) -> None:
        """После решения по группе второго вопроса не остаётся: повтор по нему — 409."""
        other = json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(
            {"command": "python -c \"print(2)\""})}}])
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC),
                      ("terminal", other, BLOCK_HEREDOC)])
        group = ap.collect(self.cfg, self.room, CARD)[0]
        first, second = group["approval_ids"][0], group["approval_ids"][1]
        ap.decide(self.cfg, self.room, CARD, first, "deny")
        before = (self.home / "config.yaml").read_text(encoding="utf-8")
        with self.assertRaises(ap.ApprovalError) as ctx:
            ap.decide(self.cfg, self.room, CARD, second, "once")
        self.assertEqual(ctx.exception.http, 409, ctx.exception.code)
        self.assertEqual((self.home / "config.yaml").read_text(encoding="utf-8"), before,
                         "отказ по уже решённому запросу не пишет в профиль")

    def test_live_hermes_approves_and_revoke_removes(self) -> None:
        """Живая сверка: Hermes сам подтверждает, что разрешение сняло блокировку."""
        self.skip_unless_hermes()
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]

        def verdict(kind: str = "terminal") -> bool:
            res = ap.run_worker(self.cfg, {"op": "check", "kind": kind,
                                           "home": str(self.home), "command": COMMAND})
            return bool(res.get("approved"))

        self.assertFalse(verdict("terminal"), "до разрешения команда должна быть закрыта")
        ap.decide(self.cfg, self.room, CARD, item["id"], "always")
        self.assertTrue(verdict("terminal"), "после «разрешать всегда» Hermes должен разрешить")
        # отзыв — снова закрыто
        ap.revoke(self.cfg, PROFILE, "script execution via heredoc", room=self.room)
        self.assertNotIn("script execution via heredoc", self.allowlist())
        self.assertFalse(verdict("terminal"), "после отзыва команда снова закрыта")
        self.assertEqual(ap.grants(self.cfg, self.room)["items"], [])

    def test_execute_code_cannot_be_allowed_by_hermes(self) -> None:
        """Документируем факт Hermes: список разрешений не снимает блок execute_code."""
        self.skip_unless_hermes()
        res = ap.run_worker(self.cfg, {"op": "add", "home": str(self.home),
                                       "pattern_key": "execute_code"})
        self.assertTrue(res.get("ok"))
        verdict = ap.run_worker(self.cfg, {"op": "check", "kind": "execute_code",
                                           "home": str(self.home),
                                           "command": "print('отчёт')"})
        self.assertFalse(verdict.get("approved"))


class SweepTests(ApprovalCase):
    """Снятие «разовых» разрешений: по завершении карточки и по сроку."""

    def _grant(self, decision: str = "once") -> dict:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        return ap.decide(self.cfg, self.room, CARD, item["id"], decision)

    def test_card_finished_removes_once(self) -> None:
        self.skip_unless_hermes()
        self._grant("once")
        self.assertIn("script execution via heredoc", self.allowlist())
        self.set_card_status("done")
        res = ap.sweep(self.cfg, self.room)
        self.assertEqual(res["revoked"], 1)
        self.assertNotIn("script execution via heredoc", self.allowlist())
        self.assertEqual(ap.grants(self.cfg, self.room)["items"], [])

    def test_ttl_expires_once(self) -> None:
        self.skip_unless_hermes()
        self._grant("once")
        # сдвигаем срок в прошлое, как будто прошло 30 минут
        reg = json.loads(ap.registry_path(self.cfg, ROOM_ID).read_text(encoding="utf-8"))
        for grant in reg["grants"]:
            grant["expires_at"] = int(time.time()) - 5
        ap.registry_path(self.cfg, ROOM_ID).write_text(json.dumps(reg), encoding="utf-8")
        self.assertEqual(ap.sweep(self.cfg, self.room)["revoked"], 1)
        self.assertNotIn("script execution via heredoc", self.allowlist())

    def test_preexisting_entry_is_kept(self) -> None:
        self.skip_unless_hermes()
        # ключ стоял в конфиге ДО нас (правил человек) — снимать его нельзя
        (self.home / "config.yaml").write_text(
            CONFIG_BEFORE.replace("command_allowlist: []",
                                  "command_allowlist:\n  - script execution via heredoc"),
            encoding="utf-8")
        self._grant("once")
        self.set_card_status("done")
        ap.sweep(self.cfg, self.room)
        self.assertIn("script execution via heredoc", self.allowlist(),
                      "чужую запись приложение не снимает")

    def test_always_is_not_swept(self) -> None:
        self.skip_unless_hermes()
        self._grant("always")
        self.set_card_status("done")
        self.assertEqual(ap.sweep(self.cfg, self.room)["revoked"], 0)
        self.assertIn("script execution via heredoc", self.allowlist())

    def test_swept_once_shows_expired_and_reopens_on_new_block(self) -> None:
        """После снятия «разового» разрешения вопрос не «уже решён», а снова открыт."""
        self.skip_unless_hermes()
        self._grant("once")
        self.set_card_status("done")
        ap.sweep(self.cfg, self.room)
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(items[0]["state"], "expired")
        # W26-D1 (T2): карточку вернули в работу («Доработать») — иначе блокировка была
        # бы закрыта автозакрытием, и «снова открыт» проверить нечем
        self.set_card_status("running")
        # участник повторил команду и снова упёрся — вопрос открыт заново
        sid = "s_retry_%d" % int(time.time() * 1000)
        con = sqlite3.connect(str(self.home / "state.db"))
        con.execute("INSERT INTO sessions (id, started_at, last_activity_at, message_count, "
                    "tool_call_count, profile_name, source) VALUES (?,?,?,?,?,?,'cli')",
                    (sid, int(time.time()) - 30, int(time.time()), 4, 2, PROFILE))
        con.commit()
        _add(con, sid, "user", "Карточка %s: повтори" % CARD, at=time.time() - 20)
        _add(con, sid, "assistant", "", call=CALL_TERMINAL, at=time.time() + 5)
        _add(con, sid, "tool", BLOCK_HEREDOC, tool="terminal", at=time.time() + 6)
        con.close()
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(items[0]["state"], "pending")
        self.assertEqual(items[0]["count"], 2)
        payload = ap.decide(self.cfg, self.room, CARD, items[0]["id"], "always")
        self.assertEqual(payload["approval"]["state"], "granted_always")


class RegistryTests(ApprovalCase):
    """Мелочи реестра и подписей."""

    def test_decision_survives_vanished_session(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        ap.decide(self.cfg, self.room, CARD, item["id"], "deny")
        state = self.home / "state.db"
        con = sqlite3.connect(str(state))
        con.execute("DELETE FROM messages")
        con.commit()
        con.close()
        items = ap.collect(self.cfg, self.room, CARD)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "denied")
        self.assertEqual(items[0]["id"], item["id"])

    def test_journal_records_decision(self) -> None:
        self.session([("terminal", CALL_TERMINAL, BLOCK_HEREDOC)])
        item = ap.collect(self.cfg, self.room, CARD)[0]
        ap.decide(self.cfg, self.room, CARD, item["id"], "deny")
        path = self.cfg.state_dir / ROOM_ID / "journal.jsonl"
        text = path.read_text(encoding="utf-8")
        self.assertIn("запрещено", text)
        self.assertIn("Исполнитель по заявкам", text)

    def test_describe_is_russian(self) -> None:
        self.assertEqual(ap.describe("script execution via -e/-c flag"),
                         "выполнение кода python -c")
        self.assertEqual(ap.describe("nonsense rule"), ap.GENERIC_DESCRIPTION)
        self.assertEqual(ap.describe(None, "execute_code"), "execute_code — произвольный код")

    def test_profile_paths(self) -> None:
        self.assertEqual(ap.profile_home(self.cfg, "default"), self.root)
        self.assertEqual(ap.profile_home(self.cfg, PROFILE), self.root / "profiles" / PROFILE)
        with self.assertRaises(ap.ApprovalError):
            ap._profile_ok("../evil")

    def test_sandbox_never_writes_real_profiles(self) -> None:
        sandbox_cfg = FakeCfg(self.root, self.cfg.data_dir, sandbox=True)
        home = ap.profile_home(sandbox_cfg, PROFILE)
        self.assertIn("hermes-home", str(home))
        self.assertNotEqual(home, self.root / "profiles" / PROFILE)
        self.assertEqual(ap.hermes_root(sandbox_cfg), Path(self.cfg.data_dir) / "hermes-home")


if __name__ == "__main__":
    unittest.main()
