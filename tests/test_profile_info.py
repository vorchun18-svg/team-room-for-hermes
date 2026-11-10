# -*- coding: utf-8 -*-
"""Подписи участников: разбор описания профиля и кэш `hermes profile describe` (WAVE13 §11).

Что проверяем:

* разбор описания — тире разных видов, без должности, без описания, пустое;
* приоритет источников подписи (ручная подпись участника → describe → profile.yaml → роль);
* кэш: второй запрос не запускает CLI, фоновое обновление не ждёт ответа;
* вызов CLI идёт через `server/proc.py` (argv без оболочки, флаг «без окна»);
* в песочнице (`cfg.sandbox`) реальные профили Hermes не опрашиваются;
* живой сервер: `/api/rooms/{id}/team` и `PUT …/agents/{profile}/label` (без записи в
  реальный Hermes и в боевую доску).
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import profile_info                                  # noqa: E402
from server.config import Cfg                                    # noqa: E402


def _cfg(sandbox: bool = False, cli: str = "hermes") -> Cfg:
    """Своя конфигурация: только чтение файлов, ничего вне временного каталога."""
    tmp = Path(tempfile.mkdtemp(prefix="teamroom-labels-"))
    return Cfg(hermes_home=tmp, kanban_home=tmp, data_dir=tmp, vault_dir=tmp / "vault",
               hermes_cli=cli, sandbox=sandbox, vault_enabled=False)


class _Subprocess:
    """Подмена `subprocess`-модуля внутри `proc.run`: запоминаем argv и аргументы."""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))

        class Result:
            pass

        result = Result()
        result.stdout = self.stdout
        result.stderr = ""
        result.returncode = self.returncode
        return result


class _Proc:
    """Подмена `profile_info.proc`: пишем в журнал, а флаги окна считает настоящий proc."""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.sub = _Subprocess(stdout, returncode)
        self.run = profile_info.proc.run          # настоящая реализация (флаги, shell)

    def __call__(self, argv, **kwargs):
        return self.run(argv, module=self.sub, **kwargs)

    @property
    def calls(self):
        return self.sub.calls


class ParseDescriptionTests(unittest.TestCase):
    """«Анна — финансы и экономика (CFO)» → имя и должность."""

    def test_dash_variants(self):
        for dash in ("—", "–", "―", "‒", "−", "-", "--"):
            with self.subTest(dash=dash):
                got = profile_info.parse_description("Анна %s финансы и экономика (CFO)" % dash)
                self.assertEqual(got, {"name": "Анна", "job": "финансы и экономика (CFO)"})

    def test_tight_dash_without_spaces(self):
        self.assertEqual(profile_info.parse_description("Илья—стратегия, партнёр"),
                         {"name": "Илья", "job": "стратегия, партнёр"})

    def test_without_job(self):
        self.assertEqual(profile_info.parse_description("Анна —"), {"name": "Анна", "job": ""})
        self.assertEqual(profile_info.parse_description("  Анна  "), {"name": "", "job": "Анна"})

    def test_without_dash_is_description_not_name(self):
        # Имя не выдумываем: без тире текст считается должностью (описанием).
        self.assertEqual(profile_info.parse_description("Юридические вопросы"),
                         {"name": "", "job": "Юридические вопросы"})

    def test_long_left_part_is_not_a_name(self):
        text = "Свод по «Лету» собран из четырёх срезов — проверено критиком"
        self.assertEqual(profile_info.parse_description(text), {"name": "", "job": text})

    def test_hyphen_inside_word_survives(self):
        self.assertEqual(profile_info.parse_description("юрист-ИП — договоры"),
                         {"name": "юрист-ИП", "job": "договоры"})

    def test_empty_and_no_description(self):
        for value in ("", "   ", None, "(no description set for 'default')",
                      "no description set", "Описание не задано"):
            with self.subTest(value=value):
                self.assertEqual(profile_info.parse_description(value), {"name": "", "job": ""})

    def test_multiline_description_is_one_line(self):
        got = profile_info.parse_description("Ornith — локальный помощник\n(вторая строка)")
        self.assertEqual(got["name"], "Ornith")
        self.assertEqual(got["job"], "локальный помощник (вторая строка)")


class LabelTextTests(unittest.TestCase):

    def test_parts(self):
        self.assertEqual(profile_info.label_text("Анна", "финансы (CFO)"), "Анна — финансы (CFO)")
        self.assertEqual(profile_info.label_text("Анна", ""), "Анна")
        self.assertEqual(profile_info.label_text("", "финансы"), "финансы")
        self.assertEqual(profile_info.label_text("", ""), "")

    def test_decorate_priority(self):
        # 1. ручная подпись участника
        got = profile_info.decorate("Анна — финансы", {"name": "Марина", "job": "исследования"}, "Ира — бухгалтер")
        self.assertEqual((got["name"], got["job"], got["label_source"]), ("Ира", "бухгалтер", "member"))
        self.assertEqual(got["custom_label"], "Ира — бухгалтер")
        # 2. ответ describe
        got = profile_info.decorate("Анна — финансы", {"name": "Марина", "job": "исследования"})
        self.assertEqual((got["name"], got["label_source"]), ("Марина", "describe"))
        # 3. описание из profile.yaml
        got = profile_info.decorate("Анна — финансы и экономика (CFO)")
        self.assertEqual((got["label"], got["label_source"]),
                         ("Анна — финансы и экономика (CFO)", "profile"))
        # 4. нет ничего — подпись соберёт интерфейс из роли
        got = profile_info.decorate("")
        self.assertEqual((got["label"], got["label_source"]), ("", "role"))


class CliCallTests(unittest.TestCase):
    """CLI запускается через proc (argv, таймаут, без shell)."""

    def setUp(self):
        self._real_run = profile_info.proc.run

    def tearDown(self):
        profile_info.proc.run = self._real_run
        profile_info.reset()

    def test_argv_and_flags(self):
        fake = _Proc("Анна — финансы и экономика (CFO)\n")
        profile_info.proc.run = fake
        cfg = _cfg(sandbox=False, cli="hermes")
        text = profile_info.read_description(cfg, "biz-cfo")
        self.assertEqual(text, "Анна — финансы и экономика (CFO)")
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv, ["hermes", "profile", "describe", "biz-cfo"])
        self.assertTrue(kwargs.get("capture_output"))
        self.assertNotIn("shell", kwargs)                  # оболочки нет — argv списком
        self.assertEqual(kwargs.get("timeout"), profile_info.TIMEOUT)
        if os.name == "nt":
            self.assertEqual(kwargs.get("creationflags"), profile_info.proc.no_window())

    def test_bad_profile_name_never_runs_cli(self):
        fake = _Proc("x")
        profile_info.proc.run = fake
        cfg = _cfg(sandbox=False, cli="hermes")
        for name in ("", "  ", "-rf", "../../etc", "a" * 80):
            with self.subTest(name=name):
                self.assertEqual(profile_info.read_description(cfg, name), "")
        self.assertEqual(fake.calls, [])

    def test_error_exit_without_stdout_is_empty(self):
        profile_info.proc.run = _Proc("", returncode=1)
        self.assertEqual(profile_info.read_description(_cfg(sandbox=False), "realtor"), "")

    def test_sandbox_never_calls_cli(self):
        fake = _Proc("Анна — финансы")
        profile_info.proc.run = fake
        got = profile_info.labels(_cfg(sandbox=True), ["biz-cfo"])
        self.assertEqual(got["biz-cfo"]["label"], "")
        self.assertFalse(got["biz-cfo"]["pending"])
        self.assertEqual(fake.calls, [])


class CacheTests(unittest.TestCase):
    """Кэш и фоновое обновление: ответ API не ждёт CLI."""

    def setUp(self):
        profile_info.reset()
        self.calls: list = []

    def tearDown(self):
        profile_info.wait(5)
        profile_info.reset()

    def _reader(self, cfg, profile):
        self.calls.append(profile)
        return {"biz-cfo": "Анна — финансы и экономика (CFO)",
                "biz-ops": "Марина — исследования, поставщики, доставка",
                "default": "(no description set for 'default')"}.get(profile, "")

    def test_background_fill_and_cache(self):
        profile_info.set_reader(self._reader)
        cfg = _cfg(sandbox=False)
        first = profile_info.labels(cfg, ["biz-cfo", "default"])
        # ответ мгновенный: подписи ещё нет, но запрос уже в фоне
        self.assertEqual(first["biz-cfo"]["label"], "")
        self.assertTrue(first["biz-cfo"]["pending"])
        self.assertTrue(profile_info.wait(5))
        second = profile_info.labels(cfg, ["biz-cfo", "default"])
        self.assertEqual(second["biz-cfo"]["name"], "Анна")
        self.assertEqual(second["biz-cfo"]["job"], "финансы и экономика (CFO)")
        self.assertEqual(second["biz-cfo"]["label"], "Анна — финансы и экономика (CFO)")
        self.assertEqual(second["biz-cfo"]["source"], "describe")
        self.assertFalse(second["biz-cfo"]["pending"])
        # профиль без описания — не ошибка и не «pending»
        self.assertEqual(second["default"]["label"], "")
        self.assertEqual(sorted(self.calls), ["biz-cfo", "default"])

        # свежий кэш: повторные вызовы CLI не запускают
        third = profile_info.labels(cfg, ["biz-cfo"])
        self.assertEqual(third["biz-cfo"]["label"], "Анна — финансы и экономика (CFO)")
        self.assertEqual(sorted(self.calls), ["biz-cfo", "default"])

    def test_stale_value_is_given_at_once(self):
        profile_info.set_reader(self._reader)
        cfg = _cfg(sandbox=False)
        profile_info.labels(cfg, ["biz-cfo"])
        profile_info.wait(5)
        later = time.time() + profile_info.TTL + 1
        got = profile_info.labels(cfg, ["biz-cfo"], now=later)
        self.assertEqual(got["biz-cfo"]["label"], "Анна — финансы и экономика (CFO)")
        self.assertTrue(got["biz-cfo"]["stale"])           # обновление пошло в фон
        self.assertTrue(profile_info.wait(5))

    def test_two_requests_do_not_duplicate_work(self):
        profile_info.set_reader(self._reader)
        cfg = _cfg(sandbox=False)
        profile_info.labels(cfg, ["biz-cfo"])
        profile_info.labels(cfg, ["biz-cfo"])
        profile_info.wait(5)
        self.assertEqual(self.calls.count("biz-cfo"), 1)

    def test_reader_exception_does_not_break_labels(self):
        def boom(cfg, profile):
            raise RuntimeError("CLI упал")

        profile_info.set_reader(boom)
        cfg = _cfg(sandbox=False)
        profile_info.labels(cfg, ["biz-cfo"])
        self.assertTrue(profile_info.wait(5))
        self.assertEqual(profile_info.labels(cfg, ["biz-cfo"])["biz-cfo"]["label"], "")


try:                                        # песочница пишется другим исполнителем
    from tests import helpers as _helpers
    Sandbox = _helpers.Sandbox
except Exception:                           # noqa: BLE001
    Sandbox = None


@unittest.skipUnless(Sandbox is not None, "tests/helpers.py (Sandbox) ещё не готов")
class ServerTeamLabelTests(unittest.TestCase):
    """Живой сервер в песочнице: поля подписи в /team и правка подписи участника."""

    ROOM = {
        "id": "lab", "title": "Лаборатория подписей", "icon": "folder", "board": "team",
        "tenant": "room:lab", "orchestrator": "default", "critic": None,
        "members": [{"profile": "biz-cfo", "role": "worker", "note": "Финансы", "pinned": False,
                     "added_at": 1793000000, "label": ""}],
        "context_files": [], "project_dir": "", "mode_default": "auto", "critic_pass": False,
        "max_rounds": 1, "created_at": 1793000000, "archived": False,
    }

    def setUp(self):
        profile_info.reset()
        self.sb = Sandbox().__enter__()
        # свои профили Hermes: песочница не должна читать настоящие
        self.home = Path(tempfile.mkdtemp(prefix="teamroom-profiles-"))
        for name, description in (("biz-cfo", "Анна — финансы и экономика (CFO)"),
                                  ("no-desc", "")):
            folder = self.home / "profiles" / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "config.yaml").write_text("model:\n  default: dilizhans-midi\n", encoding="utf-8")
            (folder / "profile.yaml").write_text("description: %s\n" % description, encoding="utf-8")
        self.sb.cfg.hermes_home = self.home
        from server import statefiles as sf
        sf.write_json_atomic(Path(self.sb.cfg.data_dir) / "rooms.json",
                             {"version": 1, "rooms": [dict(self.ROOM)], "updated_at": 1})
        from server import app as app_mod
        self.server = app_mod.create_app(self.sb.cfg, start_watcher=False, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:                   # noqa: BLE001
            pass
        profile_info.reset()
        self.sb.__exit__(None, None, None)

    def _call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})

    def _member(self, profile):
        status, team = self._call("GET", "/api/rooms/lab/team")
        self.assertEqual(status, 200, team)
        hit = [m for m in team["members"] if m.get("profile") == profile]
        self.assertTrue(hit, team)
        return hit[0]

    def test_team_member_has_label_from_profile(self):
        status, team = self._call("GET", "/api/rooms/lab/team")
        self.assertEqual(status, 200, team)
        member = self._member("biz-cfo")
        self.assertEqual(member["name"], "Анна")
        self.assertEqual(member["job"], "финансы и экономика (CFO)")
        self.assertEqual(member["label"], "Анна — финансы и экономика (CFO)")
        self.assertEqual(member["label_source"], "profile")
        self.assertEqual(member["custom_label"], "")
        self.assertFalse(member["label_pending"])
        # руководитель комнаты без описания профиля — подпись из роли (интерфейс)
        director = self._member("default")
        self.assertEqual(director["label"], "")
        self.assertEqual(director["label_source"], "role")

    def test_manual_label_wins_and_clears(self):
        status, payload = self._call("PUT", "/api/rooms/lab/agents/biz-cfo/label",
                                     {"label": "Ира — бухгалтер проекта"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["label"], "Ира — бухгалтер проекта")
        member = self._member("biz-cfo")
        self.assertEqual(member["label"], "Ира — бухгалтер проекта")
        self.assertEqual(member["label_source"], "member")
        self.assertEqual(member["custom_label"], "Ира — бухгалтер проекта")
        # подпись живёт в реестре комнаты
        rooms = json.loads((Path(self.sb.cfg.data_dir) / "rooms.json").read_text(encoding="utf-8"))
        stored = [m for m in rooms["rooms"][0]["members"] if m["profile"] == "biz-cfo"][0]
        self.assertEqual(stored["label"], "Ира — бухгалтер проекта")
        # пустая подпись — снова описание профиля
        status, _ = self._call("PUT", "/api/rooms/lab/agents/biz-cfo/label", {"label": ""})
        self.assertEqual(status, 200)
        member = self._member("biz-cfo")
        self.assertEqual(member["label"], "Анна — финансы и экономика (CFO)")
        self.assertEqual(member["label_source"], "profile")

    def test_label_of_lead_is_saved_like_anyone(self):
        # WAVE17 B-17: руководитель комнаты подписывается той же операцией, что исполнитель;
        # подпись хранится в реестре комнаты (`role_labels`), а не в профиле Hermes.
        status, payload = self._call("PUT", "/api/rooms/lab/agents/default/label", {"label": "Михаил — руководитель"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["label"], "Михаил — руководитель")
        self.assertEqual(payload["role_labels"].get("default"), "Михаил — руководитель")
        director = self._member("default")
        self.assertEqual(director["label"], "Михаил — руководитель")
        self.assertEqual(director["label_source"], "member")
        stored = json.loads((Path(self.sb.cfg.data_dir) / "rooms.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["rooms"][0]["role_labels"]["default"], "Михаил — руководитель")
        # пустая строка — сброс к роли/описанию профиля
        status, payload = self._call("PUT", "/api/rooms/lab/agents/default/label", {"label": ""})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self._member("default")["label"], "")
        self.assertNotIn("default", payload.get("role_labels") or {})

    def test_label_of_stranger_is_not_found(self):
        status, payload = self._call("PUT", "/api/rooms/lab/agents/lawyer/label", {"label": "Лев"})
        self.assertEqual(status, 404, payload)

    def test_label_must_be_text(self):
        status, payload = self._call("PUT", "/api/rooms/lab/agents/biz-cfo/label", {"label": 42})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["detail"].get("field"), "label")

    def test_profiles_roster_has_labels(self):
        status, payload = self._call("GET", "/api/profiles")
        self.assertEqual(status, 200, payload)
        items = {item["profile"]: item for item in payload["items"]}
        self.assertIn("biz-cfo", items)
        self.assertEqual(items["biz-cfo"]["label"], "Анна — финансы и экономика (CFO)")
        self.assertEqual(items["biz-cfo"]["label_source"], "profile")


if __name__ == "__main__":
    unittest.main()
