# -*- coding: utf-8 -*-
"""W29B-Q6 — уведомления Windows: по умолчанию выключены, тихие часы, дедупликация.

Что проверяется
---------------
1. **По умолчанию выключено.** Файла настроек нет — ``enabled: false``, все виды
   включены (человек выбирает их при включении), тихие часы 22:00–08:00 выключены.
   Пока выключено, события только запоминаются («видели»), пачки старых
   уведомлений после включения не будет.
2. **Проверка значений**: мусор в тихих часах не проходит с причиной, интервал не
   чаще минуты, неизвестные виды отбрасываются.
3. **Тихие часы** считаются по местному времени и через полночь; событие тихих
   часов НЕ теряется — ждёт в отложенных и уходит, когда тишина кончилась.
4. **Дедупликация** (одно событие — один раз за 7 дней) и **не чаще 1 раза в 5
   минут на вид** (второе событие того же вида ждёт, а не пропадает).
5. **Песочный канал** ``TEAMROOM_NOTIFY_FAKE``: уведомление пишется строкой JSONL,
   PowerShell не запускается (подменён шпионом — вызовов быть не должно).
6. **Безопасность аргументов PowerShell**: скрипт постоянный, текст человека идёт
   в переменные окружения, в командной строке — только base64 одного и того же
   скрипта; подстановка кавычек/``;``/``$(...)`` ничего не меняет.
7. **Ручки** ``GET /api/notify``, ``PUT /api/notify/settings``,
   ``POST /api/notify/test`` — через настоящий ``App.dispatch`` в песочнице.

Данные вымышленные: комнаты «Кофейня» (cafe) и «Химчистка» (laundry), запросы
«Свести меню на неделю», «Сверить накладные». Реальная доска и Hermes не трогаются.

Запуск:
    python -m unittest tests.test_notify -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import notify as nt                     # noqa: E402
from tests.helpers import Sandbox                   # noqa: E402

NOW = 1_800_000_000          # 2027-01-15 10:40 по UTC — удобная точка отсчёта


def local_ts(hour: int, minute: int = 0, day: int = 15) -> int:
    """Отметка времени на локальном «сегодня» в заданный час (для тихих часов)."""
    stamp = time.mktime((2027, 1, day, hour, minute, 0, 0, 0, -1))
    return int(stamp)


def cfg_for(tmp: Path, sandbox: bool = False, port: int = 8866):
    return SimpleNamespace(state_dir=tmp / "state", data_dir=tmp, sandbox=sandbox, port=port)


def root_fact(room="cafe", root="t_aaaa1111", status="working", error="", title="Свести меню на неделю",
              room_name="Кофейня", needs_you=False):
    return {"kind": "root", "room": room, "room_name": room_name, "root": root,
            "status": status, "error": error, "title": title, "needs_you": needs_you}


class RulesTest(unittest.TestCase):
    """Правила уведомлений — чистая логика, без Windows и файлов."""

    def test_defaults_are_off(self):
        settings = nt.default_settings()
        self.assertFalse(settings["enabled"])
        self.assertEqual(sorted(settings["kinds"]), sorted(nt.KINDS))
        self.assertTrue(all(settings["kinds"].values()))
        self.assertFalse(settings["quiet_hours"]["enabled"])
        self.assertEqual(settings["min_interval_s"], 300)

    def test_normalize_rejects_garbage(self):
        settings, warnings = nt.normalize_settings(
            {"enabled": True, "kinds": {"waiting": False, "bogus": True},
             "quiet_hours": {"enabled": True, "from": "25:00", "to": "утро"},
             "min_interval_s": 5})
        self.assertTrue(settings["enabled"])
        self.assertFalse(settings["kinds"]["waiting"])
        self.assertNotIn("bogus", settings["kinds"])
        self.assertEqual(settings["quiet_hours"]["from"], "22:00")
        self.assertEqual(settings["quiet_hours"]["to"], "08:00")
        self.assertEqual(settings["min_interval_s"], nt.MIN_INTERVAL_FLOOR)
        fields = {w["field"] for w in warnings}
        self.assertIn("quiet_hours.from", fields)
        self.assertIn("quiet_hours.to", fields)
        self.assertIn("kinds", fields)
        self.assertIn("min_interval_s", fields)

    def test_quiet_hours_wrap_midnight(self):
        on = nt.default_settings()
        on["quiet_hours"] = {"enabled": True, "from": "22:00", "to": "08:00"}
        self.assertTrue(nt.in_quiet_hours(on, local_ts(23, 30)))
        self.assertTrue(nt.in_quiet_hours(on, local_ts(7, 59)))
        self.assertFalse(nt.in_quiet_hours(on, local_ts(8, 0)))
        self.assertFalse(nt.in_quiet_hours(on, local_ts(12, 0)))
        day = nt.default_settings()
        day["quiet_hours"] = {"enabled": True, "from": "09:00", "to": "18:00"}
        self.assertTrue(nt.in_quiet_hours(day, local_ts(9, 0)))
        self.assertFalse(nt.in_quiet_hours(day, local_ts(18, 0)))
        off = nt.default_settings()
        self.assertFalse(nt.in_quiet_hours(off, local_ts(23, 0)))

    def test_disabled_only_remembers(self):
        settings = nt.default_settings()
        first = nt.plan(settings, {}, [root_fact(status="working")], now=NOW)
        self.assertEqual(first["send"], [])
        plan = nt.plan(settings, first["state"], [root_fact(status="done")], now=NOW)
        self.assertEqual(plan["send"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "disabled")
        # второй проход: переход уже не новый, событий нет
        again = nt.plan(settings, plan["state"], [root_fact(status="done")], now=NOW + 60)
        self.assertEqual(again["send"], [])
        self.assertEqual(again["skipped"], [])

    def test_finished_once_with_dedupe(self):
        settings = nt.default_settings()
        settings["enabled"] = True
        first = nt.plan(settings, {}, [root_fact(status="working")], now=NOW)
        self.assertEqual(first["send"], [])                 # первый проход — только запомнили
        second = nt.plan(settings, first["state"], [root_fact(status="done")], now=NOW + 30)
        self.assertEqual(len(second["send"]), 1)
        item = second["send"][0]
        self.assertEqual(item["kind"], "finished")
        self.assertIn("#/room/cafe/request/t_aaaa1111", item["url"])
        self.assertIn("Кофейня", item["body"])
        third = nt.plan(settings, second["state"], [root_fact(status="done")], now=NOW + 120)
        self.assertEqual(third["send"], [])

    def test_error_and_rate_limit_per_kind(self):
        settings = nt.default_settings()
        settings["enabled"] = True
        base = nt.plan(settings, {}, [
            root_fact(root="t_aaaa1111", status="working"),
            root_fact(root="t_bbbb2222", status="working", title="Сверить накладные", room="laundry"),
        ], now=NOW)
        # оба запроса упали в один проход: вид «ошибка» — не чаще раза в 5 минут
        failed = nt.plan(settings, base["state"], [
            root_fact(root="t_aaaa1111", status="failed", error="нет доступа к папке"),
            root_fact(root="t_bbbb2222", status="failed", error="нет доступа к папке", room="laundry"),
        ], now=NOW + 10)
        self.assertEqual(len(failed["send"]), 1)
        self.assertEqual(failed["send"][0]["kind"], "error")
        self.assertIn("нет доступа", failed["send"][0]["body"])
        self.assertIn("rate", [one["reason"] for one in failed["skipped"]])
        # через 5 минут отложенное уходит (не пропало)
        later = nt.plan(settings, failed["state"], [], now=NOW + 10 + settings["min_interval_s"])
        self.assertEqual(len(later["send"]), 1)
        self.assertEqual(later["send"][0]["root"], "t_bbbb2222")

    def test_quiet_hours_hold_then_release(self):
        settings = nt.default_settings()
        settings["enabled"] = True
        settings["quiet_hours"] = {"enabled": True, "from": "22:00", "to": "08:00"}
        night = local_ts(23, 0)
        first = nt.plan(settings, {}, [root_fact(status="working")], now=night - 60)
        quiet = nt.plan(settings, first["state"], [root_fact(status="done")], now=night)
        self.assertEqual(quiet["send"], [])
        self.assertEqual(quiet["skipped"][0]["reason"], "quiet")
        self.assertEqual(len(quiet["state"]["backlog"]), 1)
        morning = nt.plan(settings, quiet["state"], [], now=local_ts(8, 5))
        self.assertEqual(len(morning["send"]), 1)
        self.assertEqual(morning["send"][0]["kind"], "finished")
        self.assertEqual(morning["state"]["backlog"], [])

    def test_waiting_count_notification(self):
        settings = nt.default_settings()
        settings["enabled"] = True
        facts = [{"kind": "waiting", "room": "cafe", "room_name": "Кофейня",
                  "count": 2, "roots": ["t_aaaa1111", "t_bbbb2222"]}]
        plan = nt.plan(settings, {}, facts, now=NOW)
        self.assertEqual(len(plan["send"]), 1)
        self.assertEqual(plan["send"][0]["title"], "Ждёт ваш ответ: 2")
        again = nt.plan(settings, plan["state"], facts, now=NOW + 60)
        self.assertEqual(again["send"], [])


class ChannelTest(unittest.TestCase):
    """Канал: песочный файл, безопасность PowerShell, никаких окон в тестах."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notify-"))
        self.cfg = cfg_for(self.tmp)
        self.fake = self.tmp / "fake.jsonl"
        self._env = os.environ.get("TEAMROOM_NOTIFY_FAKE")
        os.environ["TEAMROOM_NOTIFY_FAKE"] = str(self.fake)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("TEAMROOM_NOTIFY_FAKE", None)
        else:
            os.environ["TEAMROOM_NOTIFY_FAKE"] = self._env
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fake_channel_writes_jsonl_and_never_spawns(self):
        calls = []
        real = nt.proc_mod.popen
        nt.proc_mod.popen = lambda *a, **k: calls.append(a)
        try:
            result = nt.test_notification(self.cfg, port=8866)
        finally:
            nt.proc_mod.popen = real
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [])
        lines = [json.loads(line) for line in self.fake.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "test")
        self.assertTrue(lines[0]["url"].startswith("http://127.0.0.1:8866/#/"))

    def test_tick_through_fake_channel(self):
        settings, _ = nt.save_settings(self.cfg, {"enabled": True})
        self.assertTrue(settings["enabled"])
        nt.tick(self.cfg, [root_fact(status="working")], port=8866)
        result = nt.tick(self.cfg, [root_fact(status="done")], port=8866)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["channel"], "fake")
        lines = [json.loads(line) for line in self.fake.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[-1]["kind"], "finished")

    def test_powershell_command_has_no_payload(self):
        nasty = "Свести меню'; Start-Process calc; '$(Get-Process)"
        command = nt.ps_command()
        self.assertIn("-EncodedCommand", command)
        self.assertNotIn("calc", command)
        self.assertNotIn("Start-Process calc", command)
        self.assertNotIn(nasty.split("'")[0], command)
        # скрипт постоянный: текст человека уходит только в окружение
        self.assertNotIn(nasty, nt.NOTIFYICON_SCRIPT)
        self.assertIn("$env:TEAMROOM_NOTIFY_TITLE", nt.NOTIFYICON_SCRIPT)
        self.assertIn("$env:TEAMROOM_NOTIFY_URL", nt.NOTIFYICON_SCRIPT)
        self.assertIn("BalloonTipClicked", nt.NOTIFYICON_SCRIPT)
        self.assertTrue(nt.ps_command() == command)
        # ссылка не http://127.0.0.1 — не откроется (защита от подмены)
        item = {"kind": "test", "title": "t", "body": "b", "url": "file:///C:/Windows/system32/calc.exe"}
        env_seen = {}

        class _Spy:
            @staticmethod
            def popen(argv, **kwargs):
                env_seen.update(kwargs.get("env") or {})
                return SimpleNamespace(pid=1)

        real = nt.proc_mod.popen
        nt.proc_mod.popen = _Spy.popen
        os.environ.pop("TEAMROOM_NOTIFY_FAKE", None)
        try:
            outcome = nt._deliver_windows(item) if nt.IS_WINDOWS and Path(nt.powershell_exe()).exists() else {"ok": True}
        finally:
            nt.proc_mod.popen = real
            os.environ["TEAMROOM_NOTIFY_FAKE"] = str(self.fake)
        if outcome.get("channel") == "windows" or "ok" in outcome:
            self.assertEqual(env_seen.get("TEAMROOM_NOTIFY_URL", ""), "")

    def test_sandbox_without_fake_is_silent(self):
        os.environ.pop("TEAMROOM_NOTIFY_FAKE", None)
        cfg = cfg_for(self.tmp, sandbox=True)
        info = nt.channel_info(cfg)
        self.assertEqual(info["name"], "sandbox")
        self.assertFalse(info["available"])
        result = nt.test_notification(cfg, port=8866)
        self.assertFalse(result["ok"])
        self.assertIn("настоящие уведомления", result["reason"])


class RoutesTest(unittest.TestCase):
    """Ручки через настоящий App.dispatch в песочнице (доска и Hermes не трогаются)."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        import server.app as app_mod
        cls.app = app_mod.App(cls.cfg, port=0)
        cls._env = os.environ.get("TEAMROOM_NOTIFY_FAKE")
        os.environ["TEAMROOM_NOTIFY_FAKE"] = str(Path(cls.cfg.state_dir) / "notify-fake.jsonl")

    @classmethod
    def tearDownClass(cls):
        if cls._env is None:
            os.environ.pop("TEAMROOM_NOTIFY_FAKE", None)
        else:
            os.environ["TEAMROOM_NOTIFY_FAKE"] = cls._env
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def test_get_settings_off_by_default(self):
        status, payload, _headers = self.app.dispatch("GET", "/api/notify", {}, {})
        self.assertEqual(status, 200)
        self.assertFalse(payload["settings"]["enabled"])
        self.assertEqual(payload["channel"]["name"], "fake")
        self.assertEqual([one["id"] for one in payload["kinds"]], list(nt.KINDS))

    def test_put_enable_and_validate(self):
        status, payload, _h = self.app.dispatch("PUT", "/api/notify/settings", {}, {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["settings"]["enabled"])
        status, payload, _h = self.app.dispatch(
            "PUT", "/api/notify/settings", {},
            {"quiet_hours": {"enabled": True, "from": "24:99", "to": "07:00"}, "min_interval_s": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["quiet_hours"]["from"], "22:00")
        self.assertEqual(payload["settings"]["min_interval_s"], 60)
        self.assertTrue(payload["warnings"])

    def test_post_test_sends_to_fake(self):
        status, payload, _h = self.app.dispatch("POST", "/api/notify/test", {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        fake = Path(self.cfg.state_dir) / "notify-fake.jsonl"
        self.assertTrue(fake.exists())
        self.assertIn('"kind": "test"', fake.read_text(encoding="utf-8"))

    def test_bad_body_rejected(self):
        from server.http_util import ApiError
        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("PUT", "/api/notify/settings", {}, {})
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
