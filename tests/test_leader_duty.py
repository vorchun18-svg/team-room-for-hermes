# -*- coding: utf-8 -*-
"""W19-L: обязанность руководителя оценить работу и «Особенности» (B-35, B-34).

Что проверяется:
  * контракт руководителя: блок «Правила комнаты» создаётся приложением и содержит
    жёсткое правило мест сохранения файлов (B-27/B-34) и шаг «оцени работу участников,
    реши про «Особенности», оставь отметку» (B-35); в интерфейс этот блок не попадает;
  * отметка ``metadata.leader_review`` разбирается в любом виде (объект, строка,
    ``extra`` после нормализации), мусор не роняет разбор;
  * отметка пишется в журнал комнаты один раз (повторный проход — то же событие),
    формат события фиксирован: ``action=leader_review``, ``root_id``, ``outcome``,
    ``outcome_ru``, ``profiles``, ``added``, ``count``, ключ по комнате и корню;
  * без отметки приложение ставит РОВНО одно напоминание комментарием на корень
    (автор — руководитель комнаты, метка ``[Комната команды · проверка работы]``),
    без карточек «Уточнение», без вопросов Михаилу и без запуска моделей;
  * повторный тик/проход напоминание не дублирует; появившаяся отметка снимает
    обязанность и напоминаний больше не будет; ручные записи Михаила не трогаются.

Правила волны 19: только песочница ``tests.helpers.Sandbox``, CLI Hermes подменён
shim'ом (он пишет комментарий в песочную БД), реальная доска сверяется до/после,
файлы профилей — по хэшам.

    python -m unittest tests.test_leader_duty -v

# SHIM-OK: CLI Hermes подменён CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import hashlib
import subprocess as real_subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_notes as notes_mod          # noqa: E402
import server.app as app_mod                    # noqa: E402
import server.contract as contract              # noqa: E402
import server.engine_cli as engine_cli          # noqa: E402
import server.engine_write as ew                # noqa: E402
import server.rooms as rooms_mod                # noqa: E402
import server.watcher as watcher_mod            # noqa: E402
from tests.helpers import Sandbox, SimAgents     # noqa: E402

ROOM = "cafe"
NOW = int(time.time())


def soul_hashes() -> dict:
    """Хэши SOUL.md реальных профилей (файлы профилей менять нельзя никогда)."""
    home = Path.home() / "AppData" / "Local" / "hermes"
    paths = [home / "SOUL.md"] + sorted((home / "profiles").glob("*/SOUL.md"))
    out = {}
    for path in paths:
        try:
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


class CliShim:
    """Подмена subprocess: ничего не запускаем, `comment` пишем в песочную БД."""

    def __init__(self, sa: SimAgents) -> None:
        self.calls: list[list[str]] = []
        self.sa = sa
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                     # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if "comment" in argv:
            index = argv.index("comment")
            task_id = argv[index + 1] if index + 1 < len(argv) else ""
            author = "mikhail"
            for token in argv:
                if token.startswith("--author="):
                    author = token.split("=", 1)[1]
            if "--author" in argv:
                author = argv[argv.index("--author") + 1]
            text = ""
            if "--" in argv:
                text = argv[argv.index("--") + 1]
            if task_id.startswith("t_"):
                self.sa.comment(task_id, author, text)
        return real_subprocess.CompletedProcess(argv, 0, "", "")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.soul_before = soul_hashes()
        cls.sa = SimAgents(cls.sb)
        cls.shim = CliShim(cls.sa)
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))

    @classmethod
    def tearDownClass(cls):
        engine_cli.subprocess = cls._real_sub
        try:
            del ew.subprocess
        except AttributeError:
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
            assert soul_hashes() == cls.soul_before, "файлы профилей (SOUL.md) изменились!"
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        # «Сейчас» берём в момент теста, а не при импорте модуля: полный прогон идёт минуты,
        # и отметки «NOW - 5 с» иначе становятся «старыми» (льготный срок 180 с).
        global NOW
        NOW = int(time.time())
        self.shim.calls.clear()
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_notes_log": [], "agent_notes": {}}), {}))

    # --- помощники ---------------------------------------------------------
    def room(self) -> dict:
        return self.app.call("rooms", "get_room", ((self.cfg, ROOM), {}))

    def log(self) -> list:
        return list(self.room().get("agent_notes_log") or [])

    def events(self, action: str) -> list:
        return [event for event in self.log() if event.get("action") == action]

    def comments(self, root: str) -> list:
        rows = self.sb.rows("SELECT author, body FROM task_comments WHERE task_id = ?", (root,))
        return [dict(row) for row in rows]

    def new_root(self, status: str = "done") -> str:
        """Карточка корня запроса комнаты cafe (в песочной БД)."""
        self.seq = int(getattr(self, "seq", 0)) + 1
        card_id = self.sa._next_id("leader-%d" % self.seq)
        return self.sa.card("Оценка работы %d" % self.seq, "default", status=status,
                            tenant="room:" + ROOM, card_id=card_id)


# ---------------------------------------------------------------- контракт

class ContractTests(_Base):
    """B-35/B-34: шаг и правило мест сохранения — в правилах комнаты контракта."""

    def block(self, slug: str = "request") -> str:
        return contract.build_contract_block(self.room(), slug, self.cfg)

    def test_rules_block_has_files_rule_and_duty(self):
        text = self.block()
        self.assertIn(contract.ROOM_RULES_OPEN, text)
        self.assertIn(contract.FILES_RULE_OPEN, text)
        self.assertIn(contract.LEADER_DUTY_OPEN, text)
        # правило файлов — ВНУТРИ правил комнаты, до шага оценки
        self.assertGreater(text.index(contract.ROOM_RULES_OPEN), text.index(contract.CONTRACT_OPEN))
        self.assertGreater(text.index(contract.FILES_RULE_OPEN), text.index(contract.ROOM_RULES_OPEN))
        self.assertGreater(text.index(contract.LEADER_DUTY_OPEN), text.index(contract.FILES_RULE_OPEN))

    def test_duty_names_limits_self_write_and_mark(self):
        text = self.block()
        self.assertIn("24 записи", text)
        self.assertIn("8 000 знаков", text)
        self.assertIn("без согласования с Михаилом", text)
        self.assertIn("записи Михаила не удаляешь", text)
        self.assertIn("leader_review", text)
        self.assertIn("оценил, записей не нужно", text)
        self.assertIn("один раз напомнит", text)

    def test_metadata_has_leader_review_field(self):
        text = self.block()
        self.assertIn("leader_review:", text)
        self.assertIn('"outcome":"no_change|note_added"', text)

    def test_rules_in_request_brief_and_revision_bodies(self):
        room = self.room()
        request = contract.build_request_body(room, "Текст запроса " * 30, "request", self.cfg)
        brief = contract.build_brief_body(room, "Бриф " * 20, "brief", self.cfg)
        revision = contract.build_revision_body(room, "Запрос " * 20, "Свод " * 20,
                                                 cfg=self.cfg, user_note="замечание")
        for body in (request, brief, revision):
            self.assertIn(contract.ROOM_RULES_OPEN, body)
            self.assertIn(contract.LEADER_DUTY_OPEN, body)

    def test_contract_block_hidden_from_user_text(self):
        body = contract.build_request_body(self.room(), "Текст Михаила " * 40, "request", self.cfg)
        visible = contract.visible_text(body)
        self.assertIn("Текст Михаила", visible)
        self.assertNotIn("Правила комнаты", visible)
        self.assertNotIn("leader_review", visible)
        self.assertNotIn("Правило мест сохранения", visible)


# ---------------------------------------------------------------- разбор отметки

class ParseTests(_Base):
    """`parse_leader_review`: любой вид отметки, мусор не роняет."""

    def test_no_block_is_not_an_error(self):
        self.assertEqual(notes_mod.parse_leader_review({}), (None, []))
        self.assertEqual(notes_mod.parse_leader_review(None), (None, []))
        self.assertEqual(notes_mod.parse_leader_review({"extra": {}}), (None, []))

    def test_no_change_mark(self):
        mark, warnings = notes_mod.parse_leader_review(
            {"leader_review": {"outcome": "no_change", "summary": "оценил, записей не нужно"}})
        self.assertEqual(warnings, [])
        self.assertEqual(mark["outcome"], "no_change")
        self.assertEqual(mark["count"], 0)
        self.assertEqual(mark["outcome_ru"], "оценил, записей не нужно")
        self.assertIn("записей в «Особенности» не нужно", mark["text"])

    def test_note_added_mark_counts_and_profiles(self):
        mark, _w = notes_mod.parse_leader_review({"leader_review": {
            "outcome": "note_added", "profiles": ["biz-lawyer"],
            "added": [{"profile": "biz-lawyer", "text": "проверять сроки подачи"}],
            "summary": "юрист отработал хорошо"}})
        self.assertEqual(mark["outcome"], "note_added")
        self.assertEqual(mark["count"], 1)
        self.assertEqual(mark["profiles"], ["biz-lawyer"])
        self.assertEqual(mark["added"][0]["text"], "проверять сроки подачи")
        self.assertEqual(mark["outcome_ru"], "добавил 1 запись")
        self.assertIn("biz-lawyer", mark["text"])
        self.assertIn("юрист отработал хорошо", mark["text"])

    def test_plural_forms(self):
        def make(count):
            added = [{"profile": "default", "text": "запись %d" % i} for i in range(count)]
            mark, _w = notes_mod.parse_leader_review(
                {"leader_review": {"outcome": "note_added", "added": added}})
            return mark["outcome_ru"]
        self.assertIn("2 записи", make(2))
        self.assertIn("5 записей", make(5))
        self.assertIn("11 записей", make(11))

    def test_string_mark_and_extra_nesting(self):
        mark, _w = notes_mod.parse_leader_review({"leader_review": "оценил, записей не нужно"})
        self.assertEqual(mark["outcome"], "no_change")
        self.assertEqual(mark["summary"], "")      # фраза не дублируется в тексте
        mark, _w = notes_mod.parse_leader_review({"extra": {"leader_review": "добавил запись про критика"}})
        self.assertEqual(mark["outcome"], "note_added")

    def test_unknown_outcome_warns_but_keeps_mark(self):
        mark, warnings = notes_mod.parse_leader_review(
            {"leader_review": {"outcome": "сделано", "summary": "ок"}})
        self.assertEqual(mark["outcome"], "no_change")
        self.assertTrue(warnings)
        self.assertIn("неизвестен", warnings[0])

    def test_garbage_does_not_raise(self):
        mark, warnings = notes_mod.parse_leader_review({"leader_review": 42})
        self.assertIsNone(mark)
        self.assertTrue(warnings)


# ---------------------------------------------------------------- журнал

class JournalTests(_Base):
    """Отметка и напоминание в журнале комнаты: формат и идемпотентность."""

    def mark(self, outcome="no_change", **extra) -> dict:
        data = {"outcome": outcome, "summary": "оценил работу"}
        data.update(extra)
        mark, _w = notes_mod.parse_leader_review({"leader_review": data})
        return mark

    def test_mark_written_once_with_format(self):
        root = self.new_root()
        event = notes_mod.log_leader_review(self.cfg, ROOM, root, self.mark(), actor="default")
        self.assertTrue(event["id"].startswith("j_"))
        self.assertEqual(event["action"], "leader_review")
        self.assertEqual(event["root_id"], root)
        self.assertEqual(event["outcome"], "no_change")
        self.assertEqual(event["outcome_ru"], "оценил, записей не нужно")
        self.assertEqual(event["source"], "leader")
        self.assertEqual(event["profile"], "default")
        self.assertIsInstance(event["at"], int)
        self.assertEqual(event["key"], notes_mod.leader_review_key(ROOM, root))
        self.assertIn("оценил работу", event["text"])

        again = notes_mod.log_leader_review(self.cfg, ROOM, root, self.mark(), actor="default")
        self.assertEqual(again["id"], event["id"])
        self.assertEqual(len(self.events("leader_review")), 1)

        journal = notes_mod.journal(self.cfg, ROOM)
        self.assertEqual(journal[0]["action_ru"], "оценка работы руководителем")
        self.assertTrue(notes_mod.leader_review_status(self.cfg, ROOM, root)["done"])

    def test_mark_from_card_metadata(self):
        root = self.new_root()
        detail = {"status": "done", "finished_at": NOW - 3600, "tree": [
            {"id": root, "metadata": {"leader_review": {
                "outcome": "note_added", "profiles": ["biz-lawyer"],
                "added": [{"profile": "biz-lawyer", "text": "проверять сроки"}],
                "summary": "учтено"}}, "completed_at": NOW - 3600}]}
        event = notes_mod.record_leader_review(self.cfg, ROOM, root, detail)
        self.assertEqual(event["action"], "leader_review")
        self.assertEqual(event["outcome"], "note_added")
        self.assertEqual(event["count"], 1)
        self.assertEqual(event["profiles"], ["biz-lawyer"])
        self.assertEqual(self.events("leader_review")[0]["id"], event["id"])

    def test_mark_in_normalized_extra(self):
        root = self.new_root()
        detail = {"tree": [{"id": root, "metadata": {
            "extra": {"leader_review": {"outcome": "no_change"}}}}]}
        event = notes_mod.record_leader_review(self.cfg, ROOM, root, detail)
        self.assertEqual(event["outcome"], "no_change")

    def test_no_mark_no_event(self):
        root = self.new_root()
        detail = {"tree": [{"id": root, "metadata": {"next_steps": []}}]}
        self.assertIsNone(notes_mod.record_leader_review(self.cfg, ROOM, root, detail))
        self.assertEqual(self.events("leader_review"), [])
        self.assertFalse(notes_mod.leader_review_status(self.cfg, ROOM, root)["done"])

    def test_reminder_logged_once(self):
        root = self.new_root()
        text = notes_mod.reminder_text("Лето")
        event, created = notes_mod.log_leader_reminder(self.cfg, ROOM, root, version=1, text=text)
        self.assertTrue(created)
        self.assertEqual(event["action"], "leader_reminder")
        self.assertEqual(event["source"], "auto")
        self.assertEqual(event["key"], notes_mod.leader_reminder_key(ROOM, root))
        self.assertEqual(event["version"], 1)
        again, created_again = notes_mod.log_leader_reminder(self.cfg, ROOM, root, version=1)
        self.assertFalse(created_again)
        self.assertEqual(again["id"], event["id"])
        self.assertEqual(len(self.events("leader_reminder")), 1)
        state = notes_mod.leader_review_status(self.cfg, ROOM, root)
        self.assertFalse(state["done"])
        self.assertTrue(state["reminder_at"])

    def test_reminder_text_is_for_leader_not_for_pavel(self):
        text = notes_mod.reminder_text("Комната Лето")
        self.assertTrue(text.startswith(notes_mod.REMINDER_MARK))
        self.assertIn("Комната Лето", text)
        self.assertIn("нет отметки", text)
        self.assertIn("leader_review", text)
        self.assertIn("«Особенности»", text)
        self.assertIn("Отвечать Михаилу не нужно", text)
        self.assertNotIn("[Михаил", text)
        self.assertNotIn("[Руководитель", text)

    def test_user_notes_survive_the_mark(self):
        root = self.new_root()
        notes_mod.add(self.cfg, ROOM, "biz-lawyer", "Ручная запись Михаила",
                      source="user", origin="проверка")
        notes_mod.log_leader_review(self.cfg, ROOM, root, self.mark(), actor="default")
        item = (notes_mod.notes_of(self.room()) or {}).get("biz-lawyer") or {}
        texts = [entry["text"] for entry in notes_mod.entries_of(item)]
        self.assertIn("Ручная запись Михаила", texts)

    def test_mark_cannot_be_rolled_back_and_never_wipes_notes(self):
        """Откат отметки не должен «вернуть» пустой список и снести записи."""
        root = self.new_root()
        notes_mod.add(self.cfg, ROOM, "biz-lawyer", "Запись руководителя о сроках",
                      source="leader", origin="оценка")
        event = notes_mod.log_leader_review(self.cfg, ROOM, root, self.mark(), actor="default")
        with self.assertRaises(notes_mod.NotesError) as ctx:
            notes_mod.rollback(self.cfg, ROOM, event["id"])
        self.assertEqual(ctx.exception.code, "conflict")
        item = (notes_mod.notes_of(self.room()) or {}).get("biz-lawyer") or {}
        texts = [entry["text"] for entry in notes_mod.entries_of(item)]
        self.assertIn("Запись руководителя о сроках", texts)
        self.assertNotIn("leader_review", notes_mod.ROLLBACK_ACTIONS)


# ---------------------------------------------------------------- напоминание в watcher

class ReminderTests(_Base):
    """Тик: нет отметки → ровно одно напоминание; есть → только запись в журнал."""

    def detail(self, root: str, **over) -> dict:
        data = {"status": "done", "finished_at": NOW - 600, "synthesis": {"version": 1},
                "tree": [{"id": root, "metadata": {}, "completed_at": NOW - 600}]}
        data.update(over)
        return data

    def test_missing_mark_gets_exactly_one_reminder(self):
        root = self.new_root()
        stats = {}
        self.app.watcher._leader_duty(self.room(), root, self.detail(root), stats)
        self.assertEqual(stats.get("leader_reminders"), 1)
        comments = self.comments(root)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], self.room().get("orchestrator"))
        self.assertTrue(comments[0]["body"].startswith(notes_mod.REMINDER_MARK))
        self.assertIn("leader_review", comments[0]["body"])
        self.assertEqual(len(self.events("leader_reminder")), 1)

        # повторный проход (и «потерянный» журнал) — второго комментария нет
        self.app.call("rooms", "update_room", ((self.cfg, ROOM, {"agent_notes_log": []}), {}))
        second = {}
        self.app.watcher._leader_duty(self.room(), root, self.detail(root), second)
        self.assertNotIn("leader_reminders", second)
        self.assertEqual(len(self.comments(root)), 1)
        self.assertEqual(self.events("leader_reminder"), [])   # журнал не тронут
        self.assertTrue(notes_mod.reminder_present(self.cfg, self.room(), root))

    def test_mark_present_writes_journal_and_sends_nothing(self):
        root = self.new_root()
        detail = self.detail(root)
        detail["tree"][0]["metadata"] = {"leader_review": {"outcome": "no_change",
                                                           "summary": "оценил, записей не нужно"}}
        stats = {}
        self.app.watcher._leader_duty(self.room(), root, detail, stats)
        self.assertEqual(stats.get("leader_marks"), 1)
        self.assertNotIn("leader_reminders", stats)
        self.assertEqual(self.comments(root), [])
        self.assertEqual(len(self.events("leader_review")), 1)
        self.assertEqual(self.events("leader_reminder"), [])

    def test_repeat_pass_with_mark_is_idempotent(self):
        root = self.new_root()
        detail = self.detail(root)
        detail["tree"][0]["metadata"] = {"leader_review": {"outcome": "note_added",
                                                           "added": [{"profile": "default",
                                                                      "text": "распределение"}]}}
        for _ in range(3):
            self.app.watcher._leader_duty(self.room(), root, detail, {})
        self.assertEqual(len(self.events("leader_review")), 1)
        self.assertEqual(self.comments(root), [])

    def test_grace_period_and_unfinished_requests(self):
        root = self.new_root()
        fresh = self.detail(root, finished_at=NOW - 5)
        self.app.watcher._leader_duty(self.room(), root, fresh, {})
        self.assertEqual(self.comments(root), [])
        self.app.watcher._leader_duty(self.room(), root, self.detail(root, status="working"), {})
        self.assertEqual(self.comments(root), [])
        # время завершения неизвестно — не рискуем рассылкой по старым карточкам
        self.app.watcher._leader_duty(self.room(), root, self.detail(root, finished_at=0), {})
        self.assertEqual(self.comments(root), [])
        self.assertEqual(self.log(), [])

    def test_old_completions_are_not_reminded(self):
        """Запросы, завершённые до запуска приложения, без ретро-рассылки."""
        root = self.new_root()
        old = self.detail(root, finished_at=NOW - 3 * 3600)
        stats = {}
        self.app.watcher._leader_duty(self.room(), root, old, stats)
        self.assertEqual(self.comments(root), [])
        self.assertEqual(self.log(), [])

    def test_mark_arrives_later_and_cancels_reminder(self):
        root = self.new_root()
        detail = self.detail(root)
        self.app.watcher._leader_duty(self.room(), root, detail, {})
        self.assertEqual(len(self.comments(root)), 1)
        detail["tree"][0]["metadata"] = {"leader_review": {"outcome": "no_change"}}
        stats = {}
        self.app.watcher._leader_duty(self.room(), root, detail, stats)
        self.assertEqual(stats.get("leader_marks"), 1)
        self.assertEqual(len(self.comments(root)), 1)          # второго нет
        self.assertEqual(len(self.events("leader_review")), 1)


class TickWiringTests(_Base):
    """`Watcher.tick_once` действительно доходит до шага (без CLI и моделей)."""

    def test_tick_counts_one_reminder_and_second_tick_is_silent(self):
        root = self.new_root()
        room = dict(self.room())
        detail = {"status": "done", "finished_at": NOW - 600, "needs_you": False,
                  "synthesis": {"state": "ready", "version": 1, "at": NOW - 600},
                  "tree": [{"id": root, "metadata": {}, "completed_at": NOW - 600}],
                  "thread": [], "next_steps": [], "open_questions": [], "decisions": [],
                  "timeline": []}
        posted: list = []
        cfg = self.cfg

        class FakeLog:
            def error(self, *args, **kwargs):
                pass

        modules = {
            "rooms": type("M", (), {"list_rooms": staticmethod(
                lambda cfg, include_archived=False: [dict(room)])})(),
            "registry": type("M", (), {"list_requests": staticmethod(
                lambda cfg, room_id, limit=50: [{"root_id": root, "created_at": NOW - 3600}])})(),
            "engine_read": type("M", (), {"request_detail": staticmethod(
                lambda *a, **k: dict(detail))})(),
            "critic_loop": type("M", (), {"run_step": staticmethod(lambda *a, **k: [])})(),
            "ingest": type("M", (), {
                "ingest_request": staticmethod(lambda *a, **k: {"changed": False, "skipped": True}),
                "version_key": staticmethod(lambda det: "v1"),
                "write_vault_mirror": staticmethod(lambda *a, **k: False)})(),
            "titles": type("M", (), {"maybe_refresh": staticmethod(lambda *a, **k: None)})(),
            "agent_notes": notes_mod,
            "engine_write": type("M", (), {"act": staticmethod(
                lambda cfg, board, rid, action, **kw: posted.append((rid, action, kw)) or {"ok": True})})(),
        }

        class FakeApp:
            def __init__(self):
                self.cfg = cfg
                self.board = "team"
                self.log = FakeLog()
                self._mods = {}

            def fn(self, name, attr):
                return getattr(modules[name], attr)

            def call(self, name, attr, *attempts):
                args, kwargs = attempts[0]
                return self.fn(name, attr)(*args, **kwargs)

        watch = watcher_mod.Watcher(FakeApp())
        stats = watch.tick_once()
        self.assertEqual(stats["errors"], 0, watch.last_error)
        self.assertEqual(stats.get("leader_reminders"), 1)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][1], "comment")
        self.assertEqual(posted[0][2]["author"], room.get("orchestrator"))
        self.assertEqual(len(self.events("leader_reminder")), 1)
        second = watch.tick_once()
        self.assertNotIn("leader_reminders", second)
        self.assertEqual(len(posted), 1)


if __name__ == "__main__":
    unittest.main()
