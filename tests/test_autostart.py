"""Автозапуск сервера: кнопка «Включить автозапуск» (W20-I1, п.24).

Все проверки — на ПОДДЕЛЬНЫХ путях: временный «домашний» каталог, вымышленный
каталог программы, вымышленный запуск процессов (`runner`). На реальной машине
ни задача планировщика, ни ярлык, ни служба при прогоне этих тестов не
создаются — это главное требование пункта 24.

Что проверяем:
  * Windows — задача планировщика `TeamRoomServer` (ONLOGON), при отказе ярлык
    в «Автозагрузке»; ни чего не пишем на диск сами;
  * macOS — `~/Library/LaunchAgents/com.teamroom.server.plist` + launchctl;
  * Linux — `~/.config/systemd/user/teamroom.service`, при отказе systemd строка
    в `~/.profile` (и она не дублируется);
  * нет файла запуска — понятная русская причина, а не тихая «удача»;
  * пункт «Автозапуск» в проверке системы получает действие `autostart.enable`,
    а маршрут отдаёт итог без полных путей пользователя.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock as umock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import autostart  # noqa: E402


class FakeRunner:
    """Поддельный запуск системных команд: записывает вызовы, отвечает по коду."""

    def __init__(self, codes=None, default_code=0, touch=None):
        self.calls: list = []
        self.codes = dict(codes or {})
        self.default_code = default_code
        self.touch = touch or {}

    def __call__(self, argv, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        key = Path(argv[0]).name.lower()
        code = self.codes.get(key, self.default_code)
        target = self.touch.get(key)
        if target and code == 0:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text("подделка", encoding="utf-8")

        class Result:
            returncode = code
            stdout = ""
            stderr = "" if code == 0 else "отказано"
        return Result()

    def names(self) -> list:
        return [Path(call[0]).name.lower() for call in self.calls]


class AutostartBase(unittest.TestCase):
    """Общий стенд: поддельные каталоги программы и пользователя."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="w20i1-auto-")
        self.base = Path(self._tmp.name)
        self.app = self.base / "app"
        self.home = self.base / "user"
        (self.app / "bin").mkdir(parents=True)
        (self.home).mkdir(parents=True)
        self.data = self.base / "appdata"
        (self.data / "logs").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def make_launcher(self, os_kind="windows"):
        name = "team-room-server.cmd" if os_kind == "windows" else "team-room-server.sh"
        path = self.app / "bin" / name
        path.write_text("@echo off\r\nrem поддельный запуск\r\n", encoding="utf-8")
        return path

    def env(self, **extra):
        base = {"USERPROFILE": str(self.home), "HOME": str(self.home),
                "APPDATA": str(self.home / "AppData" / "Roaming"),
                "TEAMROOM_DATA": str(self.data)}
        base.update(extra)
        return base


class WindowsPlanTest(AutostartBase):
    def test_plan_uses_scheduler_and_startup_shortcut(self):
        launcher = self.make_launcher("windows")
        plan = autostart.plan(app_dir=self.app, home=self.home, env=self.env(),
                             os_name="windows")
        self.assertEqual(plan["os"], "windows")
        self.assertEqual(Path(plan["launcher"]), launcher)
        argvs = [action.get("argv") for action in plan["actions"] if action["kind"] == "command"]
        self.assertTrue(argvs, plan["actions"])
        create = argvs[0]
        self.assertEqual(Path(create[0]).name.lower(), "schtasks")
        self.assertIn("TeamRoomServer", create)
        self.assertIn("ONLOGON", create)
        self.assertIn('"%s"' % launcher, create)
        # запасной путь — ярлык в «Автозагрузке», путь строится от поддельного APPDATA
        fallback = [a for a in plan["actions"] if a.get("fallback")]
        self.assertEqual(len(fallback), 1)
        script = " ".join(fallback[0]["argv"])
        self.assertIn("WScript.Shell", script)
        self.assertIn(str(self.home), script)          # поддельный профиль, не реальный
        self.assertNotIn(str(self.app.parent / "appdata"), script)


class WindowsEnableTest(AutostartBase):
    def test_scheduler_accepted_nothing_written_to_disk(self):
        self.make_launcher("windows")
        runner = FakeRunner(default_code=0)
        result = autostart.enable(app_dir=self.app, home=self.home, env=self.env(),
                                  os_name="windows", runner=runner)
        self.assertTrue(result["ok"], result)
        self.assertIn("TeamRoomServer", result["how"])
        self.assertEqual(runner.names(), ["schtasks", "schtasks"])   # create + query
        self.assertEqual(result["written"], [], "на Windows мы ничего не пишем сами")

    def test_falls_back_to_startup_shortcut(self):
        self.make_launcher("windows")
        runner = FakeRunner(codes={"schtasks": 1}, default_code=0)
        result = autostart.enable(app_dir=self.app, home=self.home, env=self.env(),
                                  os_name="windows", runner=runner)
        self.assertTrue(result["ok"], result)
        self.assertIn("ярлык", result["how"])
        self.assertEqual(runner.names(), ["schtasks", "schtasks", "powershell"])
        script = " ".join(runner.calls[-1])
        self.assertIn("Start", script)                 # папка «Автозагрузка»
        self.assertIn(str(self.home), script)

    def test_both_fail_reports_honest_reason(self):
        self.make_launcher("windows")
        runner = FakeRunner(codes={"schtasks": 1, "powershell": 1}, default_code=1)
        result = autostart.enable(app_dir=self.app, home=self.home, env=self.env(),
                                  os_name="windows", runner=runner)
        self.assertFalse(result["ok"], result)
        self.assertIn("не удалось", result["message"])
        self.assertTrue(result["failures"])


class MacPlanTest(AutostartBase):
    def test_plist_written_into_fake_home(self):
        launcher = self.make_launcher("darwin")
        runner = FakeRunner(default_code=0)
        result = autostart.enable(app_dir=self.app, home=self.home, env=self.env(),
                                  os_name="darwin", runner=runner)
        plist = self.home / "Library" / "LaunchAgents" / (autostart.LAUNCHD_LABEL + ".plist")
        self.assertTrue(plist.is_file(), "plist должен появиться в поддельном профиле")
        text = plist.read_text(encoding="utf-8")
        self.assertIn(autostart.LAUNCHD_LABEL, text)
        self.assertIn(str(launcher), text)
        self.assertIn("RunAtLoad", text)
        self.assertTrue(result["ok"], result)
        self.assertIn("launchctl", runner.names())
        self.assertEqual(result["os"], "macos")

    def test_launchctl_refused_but_file_is_enough(self):
        self.make_launcher("darwin")
        runner = FakeRunner(codes={"launchctl": 1}, default_code=1)
        result = autostart.enable(app_dir=self.app, home=self.home, env=self.env(),
                                  os_name="darwin", runner=runner)
        self.assertTrue(result["ok"], result)
        self.assertIn("LaunchAgent", result["how"])


class LinuxPlanTest(AutostartBase):
    def test_systemd_unit_and_no_profile_line_on_success(self):
        launcher = self.make_launcher("linux")
        runner = FakeRunner(default_code=0)
        result = autostart.enable(app_dir=self.app, home=self.home, env=self.env(),
                                  os_name="linux", runner=runner)
        unit = self.home / ".config" / "systemd" / "user" / "teamroom.service"
        self.assertTrue(unit.is_file())
        text = unit.read_text(encoding="utf-8")
        self.assertIn("[Unit]", text)
        self.assertIn(str(launcher), text)
        self.assertIn("systemctl", runner.names())
        self.assertTrue(result["ok"], result)
        self.assertIn("systemd", result["how"])
        profile = self.home / ".profile"
        self.assertFalse(profile.is_file() and autostart.PROFILE_BEGIN in
                         profile.read_text(encoding="utf-8"),
                         "при рабочем systemd строка в профиль не пишется")

    def test_profile_fallback_is_single(self):
        self.make_launcher("linux")
        runner = FakeRunner(codes={"systemctl": 1}, default_code=1)
        profile = self.home / ".profile"
        profile.write_text("# мой профиль\n", encoding="utf-8")
        for _ in range(2):
            result = autostart.enable(app_dir=self.app, home=self.home, env=self.env(),
                                      os_name="linux", runner=runner)
            self.assertTrue(result["ok"], result)
        text = profile.read_text(encoding="utf-8")
        self.assertEqual(text.count(autostart.PROFILE_BEGIN), 1, text)
        self.assertIn("# мой профиль", text, "чужие строки профиля не трогаем")
        self.assertIn(autostart.PROFILE_END, text)


class MissingLauncherTest(AutostartBase):
    def test_clear_reason_without_launcher(self):
        for os_kind in ("windows", "darwin", "linux"):
            with self.assertRaises(autostart.AutostartError) as ctx:
                autostart.plan(app_dir=self.app, home=self.home, env=self.env(),
                               os_name=os_kind)
            message = str(ctx.exception)
            self.assertIn("bin/", message)
            self.assertIn("установщик", message)


class DiagnosticsButtonTest(AutostartBase):
    """Пункт «Автозапуск» в проверке системы и маршрут кнопки."""

    def test_check_offers_action_when_launcher_exists(self):
        from server import diagnostics
        self.make_launcher("windows")
        with umock.patch.object(diagnostics.config, "APP_DIR", self.app), \
                umock.patch.object(diagnostics, "_autostart_entry", return_value=""):
            item = diagnostics._autostart_check()
        self.assertEqual(item["id"], "autostart")
        self.assertEqual(item["status"], "off")
        self.assertEqual(item["fix"]["kind"], "action")
        self.assertEqual(item["fix"]["label"], "Включить автозапуск")
        self.assertEqual(item["fix"]["value"], "autostart.enable")
        self.assertTrue(item["fix"]["confirm"], "подтверждение обязательно")
        self.assertIn("вход", item["fix"]["confirm"])

    def test_check_without_launcher_stays_documentation(self):
        from server import diagnostics
        with umock.patch.object(diagnostics.config, "APP_DIR", self.base / "no-app"), \
                umock.patch.object(diagnostics, "_autostart_entry", return_value=""):
            item = diagnostics._autostart_check()
        self.assertEqual(item["status"], "off")
        self.assertEqual(item["fix"]["kind"], "doc")

    def test_route_masks_paths_and_passes_action(self):
        from server import routes_diag

        class FakeApp:
            cfg = type("Cfg", (), {"app_dir": str(PROJECT_ROOT)})()

            def mod(self, name):
                if name == "autostart":
                    return autostart
                from server import diagnostics
                return diagnostics

        fake_result = {"ok": True, "how": "LaunchAgent com.teamroom.server", "os": "macos",
                       "message": "Автозапуск включён.", "notes": [],
                       "written": [str(Path.home() / "Library" / "LaunchAgents"
                                       / "com.teamroom.server.plist")]}
        with umock.patch.object(autostart, "enable", return_value=fake_result):
            status, payload, headers = routes_diag.DiagRoutes.h_diagnostics_autostart(
                FakeApp(), {}, {"action": "enable"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(payload["written"])
        for path in payload["written"]:
            self.assertTrue(path.startswith("~"), path)   # полные пути наружу не отдаём

    def test_route_rejects_unknown_action(self):
        from server import routes_diag
        from server.http_util import ApiError

        class FakeApp:
            cfg = type("Cfg", (), {"app_dir": str(PROJECT_ROOT)})()

            def mod(self, name):
                return autostart

        with self.assertRaises(ApiError) as ctx:
            routes_diag.DiagRoutes.h_diagnostics_autostart(FakeApp(), {}, {"action": "rm -rf"})
        self.assertEqual(getattr(ctx.exception, "status", 400), 400)

    def test_route_needs_explicit_action(self):
        """Пустое тело не включает автозапуск: только явное {action:"enable"}."""
        from server import routes_diag
        from server.http_util import ApiError

        class FakeApp:
            cfg = type("Cfg", (), {"app_dir": str(PROJECT_ROOT)})()

            def mod(self, name):
                raise AssertionError("без явного действия модули не трогаем")

        for body in ({}, {"action": ""}, None):
            with self.subTest(body=body):
                with self.assertRaises(ApiError) as ctx:
                    routes_diag.DiagRoutes.h_diagnostics_autostart(FakeApp(), {}, body)
                self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
