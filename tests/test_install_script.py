# -*- coding: utf-8 -*-
"""Установка «из коробки»: разбор install.ps1 и настоящий прогон в песочнице.

Два уровня проверки:

1. Разбор текста (`TestInstallScriptShape`) — без запуска. Стережёт, чтобы в
   установщике остались все ключевые шаги (скачивание архива, плагин, автозапуск,
   проверка окружения, удаление) и чтобы в него не заползли личные пути автора.
2. Настоящий прогон (`TestInstallRun`) — `powershell -File install.ps1` на
   временной папке с `-SourceDir` (сеть в тестах запрещена): программа ставится,
   плагин появляется в подменённом `HERMES_HOME`, сервер отвечает, повторный
   запуск ничего не дублирует, `-Uninstall` убирает за собой, данные остаются.

Реальные `%LOCALAPPDATA%\\hermes` и порт 8777 тест не трогает: `HERMES_HOME` и
`TEAMROOM_VAULT_DIR` подменяются на temp, порт берётся из 8890–8899.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.ps1"
BIN = ROOT / "bin"

IS_WINDOWS = os.name == "nt"
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe") or ""
PY_LAUNCHER = shutil.which("py") or shutil.which("py.exe") or ""

PS = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]


def read_script() -> str:
    return INSTALL.read_text(encoding="utf-8-sig")


def free_port() -> int:
    for port in range(8899, 8889, -1):
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise unittest.SkipTest("нет свободного порта в 8890–8899")


def health(port: int) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/api/health" % port, timeout=3) as response:
            return response.getcode()
    except Exception:  # noqa: BLE001 - недоступный сервер и есть «не отвечает»
        return 0


def health_json(port: int):
    """Тело /api/health (или None) — нужно, чтобы сверять версию и возможности."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/api/health" % port, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"_error": "%s: %s" % (type(exc).__name__, exc)}


def run_ps(args, env=None, timeout=180):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(PS + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=merged)


def processes_with(fragment: str) -> list[int]:
    """PID процессов python/pythonw, у которых в командной строке есть fragment."""
    script = (
        "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
        "| Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf('%s', "
        "[System.StringComparison]::OrdinalIgnoreCase) -ge 0 } "
        "| Select-Object -ExpandProperty ProcessId) -join ','" % fragment
    )
    result = run_ps(["-Command", script], timeout=60)
    text = (result.stdout or "").strip().strip('"')
    return [int(part) for part in text.split(",") if part.strip().isdigit()]


def kill(pids) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=30)


def has(text: str, needle: str, why: str) -> bool:
    return needle.lower() in text.lower()


class TestInstallScriptShape(unittest.TestCase):
    """Разбор install.ps1 без запуска."""

    def setUp(self) -> None:
        self.assertTrue(INSTALL.is_file(), "нет install.ps1 в корне проекта")
        self.text = read_script()

    def check(self, *needles: str) -> None:
        missing = [word for word in needles if not has(self.text, word, "")]
        self.assertEqual(missing, [], "в install.ps1 нет: %s" % ", ".join(missing))

    def test_params_documented(self):
        for pattern, why in (
            (r"\[string\]\$Version", "параметр Version"),
            (r"\[Alias\('Home'\)\]\s*\n\s*\[string\]\$InstallHome", "параметр Home (алиас: $HOME только для чтения)"),
            (r"\[int\]\$Port", "параметр Port"),
            (r"\[string\]\$SourceDir", "параметр SourceDir"),
            (r"\[switch\]\$NoAutostart", "параметр NoAutostart"),
            (r"\[switch\]\$NoBrowser", "параметр NoBrowser"),
            (r"\[switch\]\$Uninstall", "параметр Uninstall"),
            (r"\[switch\]\$RemoveData", "параметр RemoveData"),
            (r"\[switch\]\$DryRun", "параметр DryRun"),
            (r"\[switch\]\$InstallDeps", "параметр InstallDeps"),
            (r"\[int\]\$WaitSeconds", "параметр WaitSeconds"),
        ):
            self.assertRegex(self.text, pattern, "нет параметра: " + why)
        self.assertRegex(self.text, r"\[CmdletBinding\(\)\]")

    def test_help_block_readable(self):
        self.assertTrue(self.text.lstrip("\ufeff").startswith("<#"), "справка должна быть в начале файла")
        for token in (".SYNOPSIS", ".DESCRIPTION", ".PARAMETER", ".EXAMPLE", ".NOTES"):
            self.assertTrue(has(self.text, token, ""), "нет раздела справки %s" % token)

    def test_download_step(self):
        self.check("Expand-Archive", "Invoke-WebRequest", "archive/refs/tags/",
                   "archive/refs/heads/", "github.com/", "Invoke-RestMethod")

    def test_environment_scan(self):
        self.check("Проверяю окружение", "Python 3.11+", "Hermes Desktop", "Место на диске",
                   "faster-whisper", "ffmpeg", "hermes CLI", "Чего не хватает",
                   "winget install --id Python.Python.3.12",
                   "python -m pip install --user faster-whisper",
                   "обязательно", "необязательно")

    def test_plugin_and_autostart(self):
        self.check("install_desktop_plugin.py", "desktop-plugins", "schtasks.exe",
                   "/городе ONLOGON", "TeamRoomServer", "WScript.Shell", "Startup", "pythonw")

    def test_health_and_stop(self):
        self.check("/api/health", "Wait-Health", "Stop-TeamRoom", "Stop-Process", "app\.py")

    def test_idempotence_guards(self):
        # задача перезаписывается по имени, а удаляется только своя
        self.check("/Create /F", "/Delete /TN", "ведёт в другую папку", "уже работал")

    def test_no_absolute_author_paths(self):
        for pattern in (r"[A-Za-z]:\\+Users\\+[^%\\\s]", r"[A-Za-z]:/+Users/+[^%\s]",
                        r"[A-Za-z]:\\+Hermes", r"[A-Za-z]:/+Hermes", r"\bMarks\b"):
            self.assertIsNone(re.search(pattern, self.text, re.IGNORECASE),
                              "личный путь автора: " + pattern)

    def test_bom_and_line_endings(self):
        raw = INSTALL.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"),
                        "без BOM PowerShell 5.1 читает файл как ANSI и ломает русский текст")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "переводы строк должны быть CRLF")

    def test_data_is_never_overwritten(self):
        self.check("$item.Name -eq 'data'", "$removeData")
        self.assertNotIn("'data', 'server'", self.text, "папку data нельзя копировать из поставки")


class TestBinScripts(unittest.TestCase):
    """Лаунчеры: скрытый запуск, остановка, открытие страницы."""

    def test_files_present(self):
        for name in ("team-room-server.cmd", "team-room.cmd", "team-room-stop.cmd", "team-room-stop.ps1"):
            self.assertTrue((BIN / name).is_file(), "нет bin/%s" % name)

    def test_server_launcher_is_thin_and_hidden(self):
        text = (BIN / "team-room-server.cmd").read_text(encoding="utf-8", errors="replace")
        self.assertIn("launch-server.ps1", text)
        self.assertIn("-WindowStyle Hidden", text)
        low = text.lower()
        self.assertNotIn("/k", low)  # никаких «оставь окно открытым»

    def test_launch_server_ps1(self):
        text = (BIN / "launch-server.ps1").read_text(encoding="utf-8-sig")
        low = text.lower()
        self.assertIn("win32_process", low)
        self.assertIn("/api/health", low)
        self.assertIn("pythonw", low)
        self.assertIn("already running", low)
        self.assertTrue((BIN / "launch-server.ps1").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_stop_script(self):
        text = (BIN / "team-room-stop.ps1").read_text(encoding="utf-8-sig")
        low = text.lower()
        self.assertIn("stop-process", low)
        self.assertIn("app\\.py", low)
        self.assertIn("server-", low)  # pid-файл
        self.assertIn("не трогаю", low)

    def test_open_script(self):
        text = (BIN / "team-room.cmd").read_text(encoding="utf-8", errors="replace")
        self.assertIn("team-room-server.cmd", text)
        self.assertIn("OpenBrowser", text)
        hidden = (BIN / "launch-server.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("http://127.0.0.1:", hidden)


@unittest.skipUnless(IS_WINDOWS and POWERSHELL, "нужен Windows и PowerShell")
class TestInstallRun(unittest.TestCase):
    """Настоящий прогон: установка → повтор → остановка → удаление."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="team-room-install-"))
        cls.home = cls.root / "home"
        cls.hermes = cls.root / "hermes"
        cls.vault = cls.root / "vault"
        cls.hermes.mkdir(parents=True)
        cls.port = free_port()
        cls.env = {
            "HERMES_HOME": str(cls.hermes),
            "TEAMROOM_VAULT_DIR": str(cls.vault),
            "TEAMROOM_HOME": str(cls.home),
        }
        cls.results = {}

    @classmethod
    def tearDownClass(cls):
        try:
            kill(processes_with(str(cls.home)))
        except Exception:  # noqa: BLE001 - уборка не должна ломать прогон
            pass
        shutil.rmtree(cls.root, ignore_errors=True)

    def install(self, *extra):
        args = ["-File", str(INSTALL), "-Home", str(self.home), "-Port", str(self.port),
                "-NoAutostart", "-NoBrowser", "-SourceDir", str(ROOT), "-WaitSeconds", "25"]
        args.extend(extra)
        return run_ps(args, self.env)

    def test_01_first_install(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Проверяю окружение", result.stdout)
        self.assertIn("Python 3.11+", result.stdout)
        self.assertIn("Командные комнаты", result.stdout)

        self.assertTrue((self.home / "server" / "app.py").is_file())
        self.assertTrue((self.home / "install.json").is_file())
        self.assertTrue((self.home / "bin" / "team-room-server.cmd").is_file())

        plugin = self.hermes / "desktop-plugins" / "team-room" / "plugin.js"
        self.assertTrue(plugin.is_file(), "плагин не зарегистрирован")
        text = plugin.read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:%d/" % self.port, text, "порт не подставлен в копию плагина")

        self.assertEqual(health(self.port), 200, "сервер не отвечает на /api/health")
        self.assertEqual(len(processes_with(str(self.home))), 1)

        # Версия — из файла VERSION поставки, а не заглушка «0.0.0+unknown»:
        # значит установщик обязан скопировать VERSION в папку программы.
        version_file = self.home / "VERSION"
        self.assertTrue(version_file.is_file(), "установщик не скопировал VERSION")
        expected = version_file.read_text(encoding="utf-8").strip()
        self.assertEqual((self.home / "CHANGELOG.md").exists(), True, "нет CHANGELOG.md")
        live = health_json(self.port) or {}
        got = str(((live.get("app") or {}).get("version")) or "")
        self.assertEqual(got, expected, "сервер сообщает версию %r вместо %r" % (got, expected))
        type(self).results["first"] = result.stdout

    def test_02_reinstall_keeps_data(self):
        data = self.home / "data"
        data.mkdir(parents=True, exist_ok=True)
        marker = data / "KEEP.txt"
        marker.write_text("keep", encoding="utf-8")

        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.is_file(), "данные пользователя затёрлись при обновлении")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(health(self.port), 200)
        self.assertEqual(len(processes_with(str(self.home))), 1,
                         "после повторной установки работают два сервера")

    def test_03_dry_run_changes_nothing(self):
        before = sorted(p.name for p in self.home.iterdir())
        result = self.install("-DryRun")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("только проверка", result.stdout.lower())
        self.assertEqual(before, sorted(p.name for p in self.home.iterdir()))

    def test_04_stop_and_start_via_bin(self):
        stop = run_ps(["-File", str(self.home / "bin" / "team-room-stop.ps1")], timeout=90)
        self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
        self.assertNotEqual(health(self.port), 200, "сервер не остановился")

        start = run_ps(["-File", str(self.home / "bin" / "launch-server.ps1")],
                       self.env, timeout=120)
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        self.assertEqual(health(self.port), 200)

        again = run_ps(["-File", str(self.home / "bin" / "launch-server.ps1")],
                       self.env, timeout=120)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(len(processes_with(str(self.home))), 1, "второй экземпляр сервера")

    def test_05_uninstall(self):
        result = run_ps(["-File", str(INSTALL), "-Home", str(self.home),
                         "-Port", str(self.port), "-Uninstall"], self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Данные сохранены", result.stdout)

        self.assertFalse((self.hermes / "desktop-plugins" / "team-room").exists(),
                         "плагин остался в Hermes")
        self.assertFalse((self.home / "server").exists(), "файлы программы остались")
        self.assertTrue((self.home / "data" / "KEEP.txt").is_file(), "данные должны остаться")
        self.assertNotEqual(health(self.port), 200, "сервер не остановлен при удалении")
        self.assertEqual(processes_with(str(self.home)), [])


if __name__ == "__main__":
    unittest.main()
