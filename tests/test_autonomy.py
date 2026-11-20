"""Уровни автономии комнаты (W27 §2, пакет E5).

Всё на ВЫМЫШЛЕННЫХ данных: поддельный домашний каталог Hermes, профили
``anna-lead`` / ``ivan-petrov``, комната «Заявки кафе «Лето»». Реальный
``%LOCALAPPDATA%\\hermes`` не трогается: он нужен только чтобы найти дерево Hermes
(только чтение, и то не обязательно).

Запись в профили в тестах — ПОДСТАВНАЯ (``autonomy.set_writer``): она делает ровно
то, что сделал бы Hermes (список разрешений, ``approvals.deny``,
``approvals.single_query_mode``), но без запуска процессов. Отдельный тест проверяет
сам протокол записи ``server/approval_grant.py::op_apply`` с подставным
``hermes_cli.config``: правку делает наш код, а не выдуманный ответ.

    python -m unittest tests.test_autonomy -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import approval_grant as ag          # noqa: E402
from server import approvals as ap               # noqa: E402
from server import autonomy as au                # noqa: E402
from server import contract                      # noqa: E402
from server import rooms as rooms_mod            # noqa: E402
from tests.helpers import _outside_hermes_base   # noqa: E402

ROOM_ID = "r_leto"
LEAD = "anna-lead"
PROFILE = "ivan-petrov"
BOARD = "team"

CONFIG_BEFORE = """\
# профиль участника комнаты (вымышленный)
approvals:
  mode: smart
  single_query_mode: deny
  deny: []
agent:
  max_turns: 40
command_allowlist: []
custom_note: кафе «Лето»
"""

#: Вердикты подставного `hermes approvals test` по классам (как у настоящего Hermes).
STUB_ASK = ("python -c \"print(1)\"",)
STUB_ALLOW = ("python файл.py",)
STUB_DENY = ("rm -rf /", "taskkill /F /IM node.exe")


def _yaml():
    import yaml                                             # noqa: PLC0415
    return yaml


class FakeCfg:
    """Мини-конфиг приложения: только то, что читает модуль уровней."""

    def __init__(self, root: Path, data: Path, sandbox: bool = True) -> None:
        self.hermes_home = root
        self.kanban_home = root
        self.data_dir = data
        self.vault_dir = data / "Vault"
        self.port = 0
        self.host = "127.0.0.1"
        self.hermes_cli = ""
        self.hermes_bin = ""
        self.sandbox = sandbox
        self.app_dir = PROJECT_ROOT
        self.allowed_roots: list = [data]
        self.vault_enabled = False
        self.board = BOARD

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def kanban_db(self, board: str = "") -> Path:
        return self.kanban_home / "kanban" / "boards" / (board or BOARD) / "kanban.db"


def _write_config(home: Path, allowlist=None, deny=None, mode="deny") -> None:
    """Написать конфиг профиля (как Hermes: approvals + command_allowlist + прочее)."""
    yaml = _yaml()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(CONFIG_BEFORE, encoding="utf-8")
    path = home / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["command_allowlist"] = list(allowlist or [])
    approvals = dict(data.get("approvals") or {})
    approvals["deny"] = list(deny or [])
    approvals["single_query_mode"] = mode
    data["approvals"] = approvals
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _read_config(home: Path) -> dict:
    return _yaml().safe_load((home / "config.yaml").read_text(encoding="utf-8-sig")) or {}


class AutonomyCase(unittest.TestCase):
    """Общая песочница: две комнаты, три профиля, подставная запись."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(_outside_hermes_base())
        cls.sandbox = Path(tempfile.mkdtemp(prefix="autonomy-", dir=str(cls.base)))
        # Поддельный HERMES_HOME песочницы: `approvals.hermes_root` при sandbox=True
        # берёт `<data>/hermes-home`, поэтому профили пишем именно туда.
        cls.cfg = FakeCfg(cls.sandbox / "data" / "hermes-home", cls.sandbox / "data")
        cls.root = Path(cls.cfg.hermes_home)

    @classmethod
    def tearDownClass(cls) -> None:
        au.reset_writer()
        au.reset_cli_runner()
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def setUp(self) -> None:
        au.reset_writer()
        au.reset_cli_runner()
        self.calls: list = []
        au.set_writer(self._fake_write)
        for folder in ("state", "backups"):
            path = Path(self.cfg.data_dir) / folder
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        self.lead_home = self.root / "profiles" / LEAD
        self.home = self.root / "profiles" / PROFILE
        _write_config(self.lead_home)
        _write_config(self.home)
        self.room = {"id": ROOM_ID, "title": "Заявки кафе «Лето»", "board": BOARD,
                     "orchestrator": LEAD,
                     "members": [{"profile": PROFILE, "role": "worker",
                                  "label": "Исполнитель по заявкам"}]}
        self._save_rooms()

    def _save_rooms(self) -> None:
        data_dir = Path(self.cfg.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        rooms = [{"id": ROOM_ID, "title": "Заявки кафе «Лето»", "board": BOARD,
                  "orchestrator": LEAD, "members": self.room["members"],
                  "created_at": 1700000000}]
        (data_dir / "rooms.json").write_text(json.dumps({"version": 1, "rooms": rooms},
                                                        ensure_ascii=False), encoding="utf-8")

    def room_now(self) -> dict:
        return rooms_mod.get_room(self.cfg, ROOM_ID)

    def _fake_write(self, cfg, request: dict) -> dict:
        """Подставная запись: ровно то, что делает Hermes (без процессов)."""
        self.calls.append(dict(request))
        yaml = _yaml()
        home = Path(request["home"])
        path = home / "config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        allow = [str(k) for k in (data.get("command_allowlist") or [])]
        for key in request.get("allowlist_remove") or []:
            allow = [item for item in allow if item != key]
        for key in request.get("allowlist_add") or []:
            if key not in allow:
                allow.append(key)
        data["command_allowlist"] = allow
        approvals = dict(data.get("approvals") or {})
        if request.get("deny") is not None:
            approvals["deny"] = list(request["deny"])
        if request.get("single_query_mode") is not None:
            approvals["single_query_mode"] = request["single_query_mode"]
        data["approvals"] = approvals
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        return {"ok": True, "op": "apply", "home": str(home), "allowlist": allow,
                "single_query_mode": approvals.get("single_query_mode") or "",
                "deny": approvals.get("deny") or []}


class FormTests(unittest.TestCase):
    """Форма записи: умолчания и проверка входа."""

    def test_normalize_defaults(self) -> None:
        block = au.normalize(None)
        self.assertEqual(block["level"], "working")
        self.assertEqual(block["never"], [])
        self.assertEqual(au.normalize({"level": "хакер"})["level"], "working")

    def test_normalize_reads_level(self) -> None:
        block = au.normalize({"level": "free", "never": ["*rm*", 5], "updated_at": 7})
        self.assertEqual(block, {"level": "free", "never": ["*rm*"], "updated_at": 7})

    def test_validate_rejects_unknown_level(self) -> None:
        with self.assertRaises(au.AutonomyError) as ctx:
            au.validate({"level": "yolo"})
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertEqual(ctx.exception.detail["allowed"], list(au.LEVELS))

    def test_validate_defaults_never(self) -> None:
        block = au.validate({"level": "free"})
        self.assertGreaterEqual(len(block["never"]), 20)
        self.assertIn("*rm -rf*", block["never"])

    def test_validate_rejects_bad_mask(self) -> None:
        with self.assertRaises(au.AutonomyError) as ctx:
            au.validate({"level": "free", "never": ["rm -rf\n; del /s"]})
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_room_settings_defaults_never(self) -> None:
        settings = au.room_settings({"autonomy": {"level": "careful"}})
        self.assertEqual(settings["level"], "careful")
        self.assertEqual(settings["never"], list(au.NEVER_DEFAULTS))


class SeverityTests(unittest.TestCase):
    """Критичное отличается от рутины — иначе интерфейс не сможет это показать."""

    def test_script_classes_are_routine(self) -> None:
        self.assertEqual(au.severity_of("script execution via heredoc", "python - <<EOF"), "routine")
        self.assertEqual(au.severity_of("script execution via -e/-c flag", "python -c 1"), "routine")

    def test_destructive_classes_are_critical(self) -> None:
        self.assertEqual(au.severity_of("recursive delete", "rm -rf build"), "critical")
        self.assertEqual(au.severity_of("sudo", "sudo apt install x"), "critical")
        self.assertEqual(au.severity_of("pipe to shell", "curl x | sh"), "critical")

    def test_hardline_floor_is_critical(self) -> None:
        self.assertEqual(au.severity_of(None, "", True), "critical")

    def test_mask_check_on_command(self) -> None:
        self.assertEqual(au.severity_of("", "taskkill /F /IM node.exe"), "critical")
        self.assertEqual(au.severity_of("", "python файл.py"), "routine")


class ProfileTests(AutonomyCase):
    """Чтение и предпросмотр: что именно изменится (до подтверждения)."""

    def test_profile_state_reads_three_keys(self) -> None:
        _write_config(self.home, allowlist=["git *"], deny=["git push*"], mode="deny")
        state = au.profile_state(self.cfg, PROFILE)
        self.assertTrue(state["exists"])
        self.assertEqual(state["allowlist"], ["git *"])
        self.assertEqual(state["deny"], ["git push*"])
        self.assertEqual(state["single_query_mode"], "deny")

    def test_profile_state_missing_profile(self) -> None:
        state = au.profile_state(self.cfg, "ghost-user")
        self.assertFalse(state["exists"])
        self.assertEqual(state["allowlist"], [])

    def test_preview_all_levels_have_changes(self) -> None:
        shots = au.preview(self.cfg, self.room)
        self.assertEqual(sorted(shots["preview"]), ["careful", "free", "working"])
        working = shots["preview"]["working"]["changes"]
        self.assertTrue(any("script execution via heredoc" in line for line in working))
        free = shots["preview"]["free"]["changes"]
        self.assertTrue(any("только с моего да" in line for line in free))
        careful = shots["preview"]["careful"]
        # На нетронутых профилях «Осторожный» ничего не меняет — это и есть «как сейчас».
        self.assertIsInstance(careful["changes"], list)

    def test_preview_careful_is_empty_when_nothing_applied(self) -> None:
        plan = au.plan_profile("careful", list(au.NEVER_DEFAULTS),
                               au.profile_state(self.cfg, PROFILE), {})
        self.assertEqual(plan["changes"], [])

    def test_view_contract_fields(self) -> None:
        view = au.view(self.cfg, self.room)
        for key in ("level", "never", "profiles", "defaults", "preview"):
            self.assertIn(key, view)
        self.assertEqual(view["level"], "working")
        self.assertEqual([p["profile"] for p in view["profiles"]], [LEAD, PROFILE])
        self.assertIn("careful", view["preview"])
        self.assertEqual(view["defaults"]["working_rules"], list(au.WORKING_RULES))


class ApplyTests(AutonomyCase):
    """Запись: подтверждение, профили, копия, учёт, отзыв."""

    def test_without_confirm_is_409_with_preview(self) -> None:
        with self.assertRaises(au.AutonomyError) as ctx:
            au.apply_level(self.cfg, self.room, "working")
        self.assertEqual(ctx.exception.http, 409)
        self.assertEqual(ctx.exception.code, "confirm_required")
        self.assertIn("working", ctx.exception.detail["preview"])
        self.assertEqual(self.calls, [])             # ни одной записи

    def test_working_adds_script_rules(self) -> None:
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["level"], "working")
        self.assertEqual(res["warnings"], [])
        for home in (self.home, self.lead_home):
            data = _read_config(home)
            self.assertEqual(sorted(data["command_allowlist"]), sorted(au.WORKING_RULES))
            self.assertEqual(data["approvals"]["single_query_mode"], "deny")
            self.assertEqual(data["custom_note"], "кафе «Лето»")     # чужие ключи целы
        self.assertEqual(len(self.calls), 2)
        rooms = self.room_now()
        self.assertEqual(rooms["autonomy"]["level"], "working")
        applied = json.loads((Path(self.cfg.data_dir) / "state" / "autonomy-applied.json")
                             .read_text(encoding="utf-8"))
        record = applied["rooms"][ROOM_ID]["profiles"][PROFILE]
        self.assertEqual(sorted(record["allowlist_added"]), sorted(au.WORKING_RULES))
        backups = list((Path(self.cfg.data_dir) / "backups" / "autonomy").glob("*.yaml"))
        self.assertEqual(len(backups), 2)
        self.assertIn("command_allowlist: []", backups[0].read_text(encoding="utf-8"))

    def test_working_is_idempotent(self) -> None:
        au.apply_level(self.cfg, self.room, "working", confirm=True)
        calls = len(self.calls)
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        self.assertEqual(len(self.calls), calls)     # второй раз писать нечего
        self.assertTrue(all(row.get("unchanged") for row in res["applied"]))

    def test_free_sets_approve_and_never(self) -> None:
        res = au.apply_level(self.cfg, self.room, "free", confirm=True)
        self.assertEqual(res["level"], "free")
        data = _read_config(self.home)
        self.assertEqual(data["approvals"]["single_query_mode"], "approve")
        self.assertEqual(sorted(data["approvals"]["deny"]), sorted(au.NEVER_DEFAULTS))
        self.assertEqual(sorted(data["command_allowlist"]), sorted(au.WORKING_RULES))

    def test_free_with_own_never_list(self) -> None:
        au.apply_level(self.cfg, self.room, "free", never=["*rm -rf*", "*git push*"],
                       confirm=True)
        data = _read_config(self.home)
        self.assertEqual(data["approvals"]["deny"], ["*rm -rf*", "*git push*"])
        self.assertEqual(self.room_now()["autonomy"]["never"], ["*rm -rf*", "*git push*"])

    def test_careful_removes_our_records(self) -> None:
        au.apply_level(self.cfg, self.room, "free", confirm=True)
        au.apply_level(self.cfg, self.room, "careful", confirm=True)
        data = _read_config(self.home)
        self.assertEqual(data["command_allowlist"], [])            # наши записи сняты
        self.assertEqual(data["approvals"]["deny"], [])            # «только с моего да» снят
        self.assertEqual(data["approvals"]["single_query_mode"], "deny")

    def test_careful_keeps_foreign_allowlist(self) -> None:
        _write_config(self.home, allowlist=["git *"], deny=["*format*"], mode="deny")
        au.apply_level(self.cfg, self.room, "free", confirm=True)
        data = _read_config(self.home)
        self.assertIn("git *", data["command_allowlist"])           # чужое не трогаем
        self.assertIn("*format*", data["approvals"]["deny"])
        au.apply_level(self.cfg, self.room, "careful", confirm=True)
        data = _read_config(self.home)
        self.assertEqual(data["command_allowlist"], ["git *"])
        self.assertEqual(data["approvals"]["deny"], ["*format*"])

    def test_missing_profile_is_skipped_honestly(self) -> None:
        self.room["members"] = self.room["members"] + [
            {"profile": "ghost-user", "role": "worker"}]
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        skipped = [row for row in res["applied"] if row["profile"] == "ghost-user"]
        self.assertTrue(skipped and skipped[0]["ok"] is False)
        self.assertTrue(any("ghost-user" in text for text in res["warnings"]))

    def test_write_error_does_not_lie_about_level(self) -> None:
        def broken(cfg, request):
            raise ap.ApprovalError("Hermes не нашёл интерпретатор", 422, "no_hermes")
        au.set_writer(broken)
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        self.assertTrue(all(row["ok"] is False for row in res["applied"]))
        self.assertTrue(res["warnings"])
        # профили не изменились, но уровень записан как выбранный (для повтора)
        self.assertEqual(_read_config(self.home)["command_allowlist"], [])

    def test_journal_gets_level_line(self) -> None:
        au.apply_level(self.cfg, self.room, "working", confirm=True)
        doc = au.journal(self.cfg, self.room)
        self.assertEqual(doc["count"], 1)
        self.assertEqual(doc["items"][0]["kind"], "level")
        self.assertEqual(doc["items"][0]["level"], "working")

    def test_unknown_room(self) -> None:
        with self.assertRaises(au.AutonomyError):
            au.apply_level(self.cfg, {"id": ""}, "working", confirm=True)


class JournalTests(AutonomyCase):
    """Журнал уровня: «что сделано само» без счётчика «нужно от вас»."""

    def test_note_decision_marks_auto_for_routine(self) -> None:
        item = {"profile": PROFILE, "pattern_key": "script execution via heredoc",
                "command": "python - <<EOF", "description": "запуск скрипта через heredoc",
                "card_title": "Собрать отчёт", "task_id": "t_1"}
        au.note_decision(self.cfg, self.room, item, "once")
        doc = au.journal(self.cfg, self.room)
        self.assertEqual(doc["count"], 1)
        row = doc["items"][0]
        self.assertEqual(row["profile"], PROFILE)
        self.assertEqual(row["severity"], "routine")
        self.assertIn("уровень пропустил бы сам", row["text"])
        self.assertEqual(row["card"], "Собрать отчёт")

    def test_note_decision_marks_human_for_critical(self) -> None:
        item = {"profile": PROFILE, "pattern_key": "recursive delete",
                "command": "rm -rf build", "description": "рекурсивное удаление"}
        au.note_decision(self.cfg, self.room, item, "deny")
        row = au.journal(self.cfg, self.room)["items"][0]
        self.assertEqual(row["severity"], "critical")
        self.assertIn("решено человеком", row["text"])

    def test_journal_limit_and_order(self) -> None:
        for index in range(5):
            au.journal_add(self.cfg, ROOM_ID, {"at": 1000 + index, "kind": "auto",
                                               "profile": PROFILE, "text": "строка %d" % index})
        doc = au.journal(self.cfg, self.room, limit=3)
        self.assertEqual(doc["count"], 3)
        self.assertEqual(doc["total"], 5)
        self.assertEqual(doc["items"][0]["text"], "строка 4")

    def test_journal_of_new_room_is_empty(self) -> None:
        doc = au.journal(self.cfg, {"id": "r_other"})
        self.assertEqual(doc["items"], [])
        self.assertEqual(doc["note"].startswith("Что агенты сделали сами"), True)


class CliTests(AutonomyCase):
    """Подсказки и проба — через ПОДСТАВНОЙ CLI (настоящий hermes не запускаем)."""

    def test_suggest_parses_json(self) -> None:
        def runner(args):
            self.assertIn("suggest", args)
            return {"code": 0, "err": "", "out": json.dumps({
                "proposals": [{"n": 1, "pattern": "git *", "kind": "glob", "count": 4,
                               "classes": ["git"], "example": "git status"},
                              {"n": 2, "pattern": "recursive delete", "kind": "class",
                               "count": 2, "classes": ["recursive delete"],
                               "example": "rm -rf build"}]})}
        au.set_cli_runner(runner)
        doc = au.suggest(self.cfg, self.room)
        self.assertTrue(doc["available"])
        self.assertEqual(doc["count"], 2)
        self.assertEqual(doc["items"][0]["pattern"], "git *")
        self.assertEqual(doc["items"][1]["severity"], "critical")
        self.assertFalse(doc["items"][0]["already"])

    def test_suggest_reports_missing_cli(self) -> None:
        au.set_cli_runner(lambda args: {"code": 127, "out": "", "err": "команда hermes не найдена"})
        doc = au.suggest(self.cfg, self.room)
        self.assertFalse(doc["available"])
        self.assertEqual(doc["items"], [])
        self.assertIn("не найдена", doc["message"])

    def test_probe_verdicts(self) -> None:
        def runner(args):
            command = args[-1]
            verdict = "allow" if command in STUB_ALLOW else (
                "ask" if command in STUB_ASK else "deny")
            codes = {"allow": 0, "ask": 2, "deny": 3}
            return {"code": codes[verdict], "err": "",
                    "out": json.dumps({"verdict": verdict, "exit_code": codes[verdict],
                                       "rule": "script execution via -e/-c flag"
                                       if verdict != "allow" else ""})}
        au.set_cli_runner(runner)
        doc = au.probe(self.cfg, self.room, STUB_ALLOW + STUB_ASK + STUB_DENY)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["summary"], {"allow": 1, "ask": 1, "deny": 2, "error": 0})
        self.assertEqual(doc["commands"][3]["severity"], "critical")

    def test_probe_survives_broken_cli(self) -> None:
        au.set_cli_runner(lambda args: {"code": 0, "out": "не json", "err": ""})
        doc = au.probe(self.cfg, self.room, ["python файл.py"])
        self.assertEqual(doc["commands"][0]["verdict"], "allow")   # по коду возврата


class VerifyAfterWriteTests(AutonomyCase):
    """W28-F: перепроверка читает ТОТ ЖЕ файл, что записан, и не врёт «не записалось»."""

    def test_file_without_pyyaml_is_read_by_raw_parser(self) -> None:
        """Без PyYAML (сервер — stdlib) ключи всё равно читаются из файла."""
        _write_config(self.home, allowlist=["script execution via heredoc"],
                      deny=["*rm -rf*"], mode="approve")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            state = au.profile_state(self.cfg, PROFILE)
        self.assertEqual(state["read_by"], "raw")
        self.assertEqual(state["allowlist"], ["script execution via heredoc"])
        self.assertEqual(state["deny"], ["*rm -rf*"])
        self.assertEqual(state["single_query_mode"], "approve")

    def test_raw_parser_sees_hermes_style_config(self) -> None:
        """Текст конфига Hermes (список и секция approvals) читается без PyYAML."""
        text = ("# профиль\n"
                "command_allowlist:\n"
                "  - script execution via -e/-c flag\n"
                "  - \"script execution via heredoc\"\n"
                "approvals:\n"
                "  mode: smart\n"
                "  deny:\n"
                "    - '*rm -rf*'\n"
                "  single_query_mode: approve\n")
        data = au._parse_keys_text(text)
        self.assertEqual(data["command_allowlist"],
                         ["script execution via -e/-c flag", "script execution via heredoc"])
        self.assertEqual(data["approvals"]["deny"], ["*rm -rf*"])
        self.assertEqual(data["approvals"]["single_query_mode"], "approve")

    def test_no_false_alarm_when_file_has_keys(self) -> None:
        """Запись записала файл, а в ответе нет списка — судим по ФАЙЛУ, а не по ответу."""
        def write_then_short_answer(cfg, request):
            """Пишет как Hermes, но НЕ пересказывает список (как боевой ответ 19.11)."""
            self._fake_write(cfg, request)
            return {"ok": True, "op": "apply", "home": request["home"]}   # без allowlist
        au.set_writer(write_then_short_answer)
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        self.assertEqual(res["warnings"], [], res)
        rows = {row["profile"]: row for row in res["applied"]}
        self.assertTrue(rows[PROFILE]["ok"], rows[PROFILE])
        self.assertGreaterEqual(rows[PROFILE]["allowlist_size"], len(au.WORKING_RULES))
        for home in (self.home, self.lead_home):
            data = _read_config(home)
            for key in au.WORKING_RULES:
                self.assertIn(key, data["command_allowlist"])

    def test_verify_reports_missing_only_when_file_confirms(self) -> None:
        """Если писатель ничего не записал — «не записалось» честное, с числами."""
        au.set_writer(lambda cfg, request: {"ok": True, "op": "apply",
                                           "home": request["home"], "allowlist": []})
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        self.assertTrue(res["warnings"], "пустая запись должна дать замечание")
        rows = {row["profile"]: row for row in res["applied"]}
        self.assertFalse(rows[PROFILE]["ok"])
        self.assertEqual(rows[PROFILE]["allowlist_size"], 0)

    def test_default_profile_lives_in_hermes_root(self) -> None:
        """Профиль `default` — корень дома Hermes, а не `profiles/default`."""
        root_config = self.root / "config.yaml"           # корень поддельного дома
        root_config.write_text(CONFIG_BEFORE, encoding="utf-8")
        self.assertFalse((self.root / "profiles" / "default").exists())
        room = dict(self.room, orchestrator="default")
        res = au.apply_level(self.cfg, room, "working", confirm=True)
        self.assertTrue(res["ok"])
        rows = {row["profile"]: row for row in res["applied"]}
        self.assertTrue(rows["default"]["ok"], rows["default"])
        self.assertEqual(Path(rows["default"]["home"]), self.root)
        data = _read_config(self.root)
        for key in au.WORKING_RULES:
            self.assertIn(key, data["command_allowlist"])
        # у самого `default` путь считается так же, как у записи
        state = au.profile_state(self.cfg, "default")
        self.assertEqual(Path(state["home"]), self.root)
        self.assertIn(au.WORKING_RULES[0], state["allowlist"])

    def test_default_mode_deny_is_not_a_false_alarm(self) -> None:
        """`deny` — умолчание Hermes: его может не быть в файле, и это не «не записалось»."""
        _write_config(self.home, allowlist=list(au.WORKING_RULES), deny=[], mode="")
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        self.assertEqual(res["warnings"], [], res)

    def test_applied_rows_say_how_file_was_read(self) -> None:
        res = au.apply_level(self.cfg, self.room, "working", confirm=True)
        row = next(r for r in res["applied"] if r["profile"] == PROFILE)
        self.assertIn(row["read_by"], ("yaml", "raw"))
        self.assertTrue(str(row["config"]).endswith("config.yaml"))


class SuggestFakeTests(AutonomyCase):
    """W28-F: подсказки в песочнице без вызова настоящего `hermes`."""

    def setUp(self) -> None:
        super().setUp()
        self._saved = os.environ.get(au.SUGGEST_FAKE_ENV)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(au.SUGGEST_FAKE_ENV, None)
        else:
            os.environ[au.SUGGEST_FAKE_ENV] = self._saved
        super().tearDown()

    def _forbid_real(self):
        """Настоящий CLI (и любой подставной runner) не должен вызываться вовсе."""
        def boom(cfg):
            raise AssertionError("настоящий CLI вызван при TEAMROOM_SUGGEST_FAKE")
        self._saved_default = getattr(au, "_default_runner")
        au._default_runner = boom
        self.addCleanup(lambda: setattr(au, "_default_runner", self._saved_default))

    def test_fake_runner_is_used(self) -> None:
        os.environ[au.SUGGEST_FAKE_ENV] = "1"
        self._forbid_real()
        res = au.suggest(self.cfg, self.room)
        self.assertTrue(res["available"], res)
        self.assertEqual(res["source"], "fake")
        self.assertGreaterEqual(res["count"], 3)
        self.assertTrue(all(item["pattern"] for item in res["items"]))

    def test_fake_payload_from_file(self) -> None:
        payload = {"proposals": [{"pattern": "make deploy", "kind": "literal", "count": 5}]}
        path = Path(self.cfg.data_dir) / "suggest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.environ[au.SUGGEST_FAKE_ENV] = str(path)
        self._forbid_real()
        res = au.suggest(self.cfg, self.room)
        self.assertEqual([item["pattern"] for item in res["items"]], ["make deploy"])

    def test_without_env_real_runner_is_used(self) -> None:
        os.environ.pop(au.SUGGEST_FAKE_ENV, None)
        au.set_cli_runner(lambda args: {"code": 0, "out": '{"proposals": []}', "err": ""})
        res = au.suggest(self.cfg, self.room)
        self.assertTrue(res["available"])
        self.assertEqual(res["source"], "hermes")


class GrantProtocolTests(unittest.TestCase):
    """`server/approval_grant.py::op_apply` — один протокол записи трёх ключей.

    Hermes не запускаем: `hermes_cli.config` подставлен, дерево Hermes — пустая
    заглушка в песочнице. Проверяем нашу часть: что именно попадает в конфиг.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(_outside_hermes_base())
        cls.sandbox = Path(tempfile.mkdtemp(prefix="grant-apply-", dir=str(cls.base)))
        cls.root = cls.sandbox / "hermes-home"
        cls.home = cls.root / "profiles" / "ivan-petrov"
        # `_hermes_tree` ищет дерево рядом с профилем: <root>/profiles/hermes-agent
        tree = cls.root / "profiles" / "hermes-agent"
        (tree / "tools").mkdir(parents=True, exist_ok=True)
        (tree / "tools" / "approval.py").write_text("# заглушка дерева Hermes\n",
                                                    encoding="utf-8")
        cls._saved = {name: os.environ.get(name) for name in ("HERMES_HOME",)}

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("hermes_cli", None)
        sys.modules.pop("hermes_cli.config", None)
        for name, value in (cls._saved or {}).items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def setUp(self) -> None:
        if self.home.exists():
            shutil.rmtree(self.home, ignore_errors=True)
        _write_config(self.home, allowlist=["git *"], deny=["*format*"], mode="deny")
        self._install_fake_hermes()

    def _install_fake_hermes(self) -> None:
        """Подставной `hermes_cli.config`: load_config/save_config на поддельном доме."""
        yaml = _yaml()
        package = types.ModuleType("hermes_cli")
        config_mod = types.ModuleType("hermes_cli.config")

        def load_config():
            path = Path(os.environ.get("HERMES_HOME", "")) / "config.yaml"
            return yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}

        def save_config(data):
            path = Path(os.environ.get("HERMES_HOME", "")) / "config.yaml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")

        config_mod.load_config = load_config
        config_mod.save_config = save_config
        package.config = config_mod
        sys.modules["hermes_cli"] = package
        sys.modules["hermes_cli.config"] = config_mod

    def test_apply_writes_three_keys_and_keeps_others(self) -> None:
        res = ag.run({"op": "apply", "home": str(self.home),
                      "allowlist_add": ["script execution via heredoc"],
                      "allowlist_remove": ["git *"],
                      "deny": ["*rm -rf*"], "single_query_mode": "approve"})
        self.assertTrue(res.get("ok"), res)
        data = _read_config(self.home)
        self.assertEqual(data["command_allowlist"], ["script execution via heredoc"])
        self.assertEqual(data["approvals"]["deny"], ["*rm -rf*"])
        self.assertEqual(data["approvals"]["single_query_mode"], "approve")
        self.assertEqual(data["approvals"]["mode"], "smart")        # чужой ключ цел
        self.assertEqual(data["custom_note"], "кафе «Лето»")
        self.assertEqual(res.get("single_query_mode"), "approve")

    def test_apply_none_means_do_not_touch(self) -> None:
        ag.run({"op": "apply", "home": str(self.home),
                "allowlist_add": [], "allowlist_remove": [],
                "deny": None, "single_query_mode": None})
        data = _read_config(self.home)
        self.assertEqual(data["command_allowlist"], ["git *"])
        self.assertEqual(data["approvals"]["deny"], ["*format*"])
        self.assertEqual(data["approvals"]["single_query_mode"], "deny")

    def test_apply_empty_mode_removes_key(self) -> None:
        ag.run({"op": "apply", "home": str(self.home), "single_query_mode": ""})
        data = _read_config(self.home)
        self.assertNotIn("single_query_mode", data["approvals"])

    def test_apply_reads_file_back_and_refuses_when_empty(self) -> None:
        """W28-F: `save_config`, который ничего не записал, — отказ с кодом (не «ok»)."""
        yaml = _yaml()

        def save_nothing(data):
            return None                                   # «записал», а файла не тронул

        fake = sys.modules["hermes_cli.config"]
        original = fake.save_config
        fake.save_config = save_nothing
        try:
            res = ag.run({"op": "apply", "home": str(self.home),
                          "allowlist_add": ["script execution via heredoc"],
                          "deny": None, "single_query_mode": None})
        finally:
            fake.save_config = original
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("code"), "failed")
        self.assertIn("не записал", res.get("message") or "")

    def test_apply_rejects_bad_masks(self) -> None:
        res = ag.run({"op": "apply", "home": str(self.home), "deny": "rm -rf"})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "bad_request")
        res = ag.run({"op": "apply", "home": str(self.home), "single_query_mode": "yolo"})
        self.assertFalse(res.get("ok"))


class ContractTests(unittest.TestCase):
    """Правило контракта знает уровень комнаты (иначе модель снова обходит блокировку)."""

    def test_working_rule_mentions_scripts(self) -> None:
        # W28-QA1: W28-E сжал правило до сути — упоминание `python -c` ушло, вместо
        # него назван разрешённый путь «файл + терминал» (`write_file` + `python файл.py`)
        # и прямо сказано, что `execute_code` в карточке недоступен. Проверяем смысл
        # правила (скрипты — файлом, не однострочником и не execute_code), а не старую
        # формулировку: правило осталось, текст стал короче.
        text = contract.approval_rule({"autonomy": {"level": "working"}})
        self.assertIn("«Рабочий»", text)
        self.assertIn("write_file", text)
        self.assertIn("python файл.py", text)
        self.assertIn("execute_code", text)
        self.assertNotIn("python -c", text)

    def test_free_rule_mentions_questions(self) -> None:
        text = contract.approval_rule({"autonomy": {"level": "free"}})
        self.assertIn("«Свободный»", text)
        self.assertIn("open_questions", text)

    def test_careful_rule_keeps_base(self) -> None:
        text = contract.approval_rule({"autonomy": {"level": "careful"}})
        self.assertIn("НЕ повод обходить", text)
        self.assertNotIn("«Рабочий»", text)
        self.assertNotIn("«Свободный»", text)


class SeverityIntegrationTests(unittest.TestCase):
    """`approvals` отдаёт вид блокировки, не меняя её состояния."""

    def test_severity_helper(self) -> None:
        self.assertEqual(ap._severity("recursive delete", "rm -rf /", False), "critical")
        self.assertEqual(ap._severity("script execution via heredoc", "python - <<EOF", False),
                         "routine")

    def test_public_item_has_severity(self) -> None:
        raw = {"id": "ap_1", "task_id": "t_1", "profile": PROFILE, "kind": "terminal",
               "command": "rm -rf build", "pattern_key": "recursive delete",
               "at": int(time.time())}
        item = ap._public(raw, None, int(time.time()))
        self.assertEqual(item["severity"], "critical")
        self.assertEqual(item["state"], "pending")     # вид не поменял состояние

    def test_public_routine_item(self) -> None:
        raw = {"id": "ap_2", "task_id": "t_1", "profile": PROFILE, "kind": "terminal",
               "command": "python -c \"print(1)\"",
               "pattern_key": "script execution via -e/-c flag", "at": int(time.time())}
        item = ap._public(raw, None, int(time.time()))
        self.assertEqual(item["severity"], "routine")


class RoomsWiringTests(AutonomyCase):
    """Настройка живёт в реестре комнат и не ломает чтение старых записей."""

    def test_room_keeps_autonomy_block(self) -> None:
        au.apply_level(self.cfg, self.room, "free", confirm=True)
        room = self.room_now()
        self.assertEqual(room["autonomy"]["level"], "free")
        self.assertTrue(room["autonomy"]["updated_at"])

    def test_old_room_reads_as_working_without_writes(self) -> None:
        room = rooms_mod.get_room(self.cfg, ROOM_ID)
        self.assertEqual(room["autonomy"]["level"], "working")
        raw = json.loads((Path(self.cfg.data_dir) / "rooms.json").read_text(encoding="utf-8"))
        self.assertNotIn("autonomy", raw["rooms"][0])   # сами не записали

    def test_bad_autonomy_patch_rejected(self) -> None:
        with self.assertRaises(rooms_mod.RoomsError):
            rooms_mod.update_room(self.cfg, ROOM_ID, {"autonomy": {"level": "yolo"}})


if __name__ == "__main__":
    unittest.main()
