# -*- coding: utf-8 -*-
"""Установка на macOS и Linux: разбор install.sh и настоящий прогон в песочнице.

Два уровня проверки (как в test_install_script.py для Windows):

1. Разбор текста — bash -n, ключевые шаги (сканирование, скачивание релиза,
   плагин, автозапуск launchd/systemd/профиль, удаление), отсутствие жёстких
   `C:\\` и личных путей автора.
2. Настоящий прогон в git-bash: `install.sh --source-dir <папка> --home <temp>
   --no-autostart --port <свободный>` — файлы ставятся, плагин появляется в
   подменённом HERMES_HOME, сервер отвечает, повторный запуск ничего не
   дублирует, `bin/team-room-stop.sh` останавливает, `--uninstall` убирает.

Реальные `~/.hermes`, `~/.local/share/TeamRoom` и порт 8777 тест не трогает:
HOME, HERMES_HOME и порт подменяются на временные. Порт берётся случайный из
8890–8899 (кроме служебных 8891/8892), проверяется занятостью bind и тем, что
на нём никто не отвечает. Параллельный прогон другого набора тестов может
занять тот же порт — тогда сервер на нём чужой: тест это замечает по
`data.dir` в /api/health и повторяет сценарий на другом порту, а не падает
на гонке.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
BIN = ROOT / "bin"
PLUGIN_TOOL = ROOT / "tools" / "install_desktop_plugin.py"

BASH = shutil.which("bash") or ""
IS_WINDOWS = os.name == "nt"

# Служебные порты: боевой/демо и ручные проверки установщика (WAVE7-RULES §1).
RESERVED_PORTS = (8777, 8778, 8891, 8892)
PORT_RANGE = tuple(port for port in range(8890, 8900) if port not in RESERVED_PORTS)

TAKEN_PORTS: set[int] = set()


def bash_path(path) -> str:
    """Путь, который поймёт bash: на Windows — C:/..., без обратных слэшей."""
    return str(path).replace("\\", "/")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def free_port() -> int:
    """Случайный свободный порт: bind + никто не отвечает по HTTP."""
    candidates = [port for port in PORT_RANGE if port not in TAKEN_PORTS]
    random.shuffle(candidates)          # параллельные прогоны не бьются за один порт
    for port in candidates:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        if health(port) == 0:
            TAKEN_PORTS.add(port)
            return port
    raise unittest.SkipTest("нет свободного порта в 8890–8899 (кроме 8891/8892)")


def health_json(port: int):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/api/health" % port, timeout=3) as response:
            return json.loads(response.read(200000).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — недоступный сервер и есть «не отвечает»
        return None


def health(port: int) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/api/health" % port, timeout=3) as response:
            return response.getcode()
    except Exception:  # noqa: BLE001 — недоступный сервер и есть «не отвечает»
        return 0


def data_dir_from_health(port: int) -> str:
    """Каталог данных сервера, который ответил, — так видно, наш это сервер или чужой."""
    payload = health_json(port)
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("dir") or "").replace("\\", "/").rstrip("/").lower()


class PortTaken(Exception):
    """На выбранном порту отвечает посторонний сервер (гонка параллельных прогонов)."""


class InstallCase(unittest.TestCase):
    """Общая песочница: свой HOME, свой HERMES_HOME, свой порт."""

    def setUp(self):
        if not BASH:
            self.skipTest("нет bash — прогон установщика невозможен")
        if not INSTALL.is_file():
            self.fail("нет install.sh в корне проекта")
        self.tmp = Path(tempfile.mkdtemp(prefix="team-room-sh-"))
        self.app = self.tmp / "app"
        self.home = self.tmp / "home"
        self.hermes = self.tmp / "hermes"
        self.home.mkdir(parents=True, exist_ok=True)
        self.hermes.mkdir(parents=True, exist_ok=True)
        self.port = free_port()

    def tearDown(self):
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra) -> dict:
        merged = {k: v for k, v in os.environ.items()
                  if k not in ("TEAMROOM_HOME", "TEAMROOM_DATA", "TEAMROOM_PORT",
                               "TEAMROOM_PYTHON", "TEAMROOM_VAULT_DIR")}
        merged.update({
            "HOME": bash_path(self.home),
            "HERMES_HOME": bash_path(self.hermes),
            "TEAMROOM_NO_SHELL": "1",       # не открывать окна и браузер
            "PYTHONIOENCODING": "utf-8",
            "LANG": "en_US.UTF-8",
        })
        if IS_WINDOWS:
            merged["USERPROFILE"] = bash_path(self.home)
            merged["LOCALAPPDATA"] = bash_path(self.tmp / "localappdata")
        merged.update({k: bash_path(v) for k, v in extra.items()})
        return merged

    def run_install(self, *args, timeout=240, env=None):
        command = ["bash", bash_path(INSTALL), "--home", bash_path(self.app),
                   "--port", str(self.port)] + list(args)
        return subprocess.run(command, cwd=bash_path(ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env or self.env())

    def run_dir(self) -> Path:
        return self.app / "data" / "run"

    def pid_files(self) -> list:
        directory = self.run_dir()
        return sorted(directory.glob("server-*.pid")) if directory.is_dir() else []

    def wait_pid_files(self, count=1, timeout=5.0) -> list:
        """pid-файл пишет лаунчер после старта: даём ему до 5 с, но не больше нужного."""
        deadline = time.time() + timeout
        files = self.pid_files()
        while len(files) < count and time.time() < deadline:
            time.sleep(0.1)
            files = self.pid_files()
        return files

    def pid_alive(self, pid: int) -> bool:
        result = subprocess.run(["bash", "-c", 'kill -0 "$1" 2>/dev/null', "bash", str(pid)],
                                capture_output=True, text=True, timeout=30)
        return result.returncode == 0

    def is_foreign_server(self) -> bool:
        """На нашем порту отвечает не наш сервер (чужой прогон занял порт)."""
        return (health(self.port) == 200
                and data_dir_from_health(self.port) != bash_path(self.app / "data").rstrip("/").lower())

    def assert_our_server(self, where: str) -> None:
        """Сервер на нашем порту — именно наш: health 200 и наш каталог данных."""
        self.assertEqual(health(self.port), 200, "сервер не ответил %s" % where)
        answered = data_dir_from_health(self.port)
        expected = bash_path(self.app / "data").rstrip("/").lower()
        if answered != expected:
            raise PortTaken("на порту %d отвечает чужой сервер %s (data.dir=%r)"
                            % (self.port, where, answered))

    def stop(self):
        script = self.app / "bin" / "team-room-stop.sh"
        if script.is_file():
            try:
                subprocess.run(["bash", bash_path(script), "--home", bash_path(self.app)],
                               capture_output=True, text=True, timeout=60, env=self.env())
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.kill_stragglers()

    def kill_stragglers(self):
        """Последняя страховка: снять любой python-процесс с нашим app.py и его порт."""
        pattern = bash_path(self.app) + "/server/app.py"
        if not IS_WINDOWS:
            return
        try:
            listing = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "@(Get-CimInstance Win32_Process "
                 "| Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf('%s', "
                 "[System.StringComparison]::OrdinalIgnoreCase) -ge 0 } "
                 "| Select-Object -ExpandProperty ProcessId) -join ','" % pattern],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return
        for part in (listing.stdout or "").strip().strip('"').split(","):
            if part.strip().isdigit():
                subprocess.run(["taskkill", "/PID", part.strip(), "/T", "/F"],
                               capture_output=True, timeout=30)
        # Порт мог остаться за процессом, чья командная строка не содержит наш путь
        # (например, python запущен из копии) — добиваем только слушателя нашего порта.
        try:
            net = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=30).stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            return
        marker = (":%d " % self.port)
        for line in net.splitlines():
            if marker not in line or "LISTEN" not in line.upper():
                continue
            pid = line.split()[-1]
            if not pid.isdigit():
                continue
            try:
                cmd = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout or ""
            except (OSError, subprocess.TimeoutExpired):
                continue
            if pattern.lower() in cmd.replace("\\", "/").lower():
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, timeout=30)


class ShapeTests(unittest.TestCase):
    """Разбор install.sh и POSIX-лаунчеров без запуска."""

    def setUp(self):
        if not INSTALL.is_file():
            self.fail("нет install.sh в корне проекта")
        self.text = read(INSTALL)

    def test_bash_syntax(self):
        result = subprocess.run(["bash", "-n", bash_path(INSTALL)],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launchers_syntax_and_exist(self):
        for name in ("team-room-server.sh", "team-room-stop.sh", "team-room.sh"):
            script = BIN / name
            self.assertTrue(script.is_file(), "нет bin/" + name)
            result = subprocess.run(["bash", "-n", bash_path(script)],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=60)
            self.assertEqual(result.returncode, 0, "%s: %s" % (name, result.stderr))

    def test_flags_present(self):
        for flag in ("--version", "--home", "--port", "--source-dir", "--no-autostart",
                     "--no-browser", "--install-deps", "--dry-run", "--uninstall",
                     "--remove-data", "--wait", "--yes"):
            self.assertIn(flag, self.text, "в install.sh нет ключа " + flag)

    def test_scan_steps_present(self):
        for needle in ("Python 3.11+", "Hermes Desktop", "hermes CLI", "faster-whisper",
                       "ffmpeg", "Интернет", "Порт", "Место на диске", "Чего не хватает"):
            self.assertIn(needle, self.text, "в отчёте установщика нет пункта " + needle)

    def test_autostart_variants(self):
        self.assertIn("LaunchAgents", self.text)
        self.assertIn("com.teamroom.server", self.text)
        self.assertIn("systemd", self.text)
        self.assertIn('SERVICE_NAME="teamroom"', self.text)
        self.assertIn(".profile", self.text)          # запасной способ
        self.assertIn("RunAtLoad", self.text)
        self.assertIn("KeepAlive", self.text)
        self.assertIn("loginctl enable-linger", self.text)

    def test_hidden_start_and_health(self):
        self.assertIn("team-room-server.sh", self.text)          # запуск в фоне
        self.assertIn("nohup", read(BIN / "team-room-server.sh"))  # без окна
        self.assertIn("$DATA_DIR/logs", self.text)
        self.assertIn("server-", self.text)            # pid-файл
        self.assertIn("/api/health", self.text)

    def test_layout_paths(self):
        self.assertIn("XDG_DATA_HOME", self.text)
        self.assertIn(".hermes", self.text)
        self.assertIn("desktop-plugins", self.text)
        self.assertIn("install.json", self.text)

    def test_no_windows_hardcoding(self):
        for text, name in ((self.text, "install.sh"),
                           (read(BIN / "team-room-server.sh"), "team-room-server.sh"),
                           (read(BIN / "team-room-stop.sh"), "team-room-stop.sh"),
                           (read(PLUGIN_TOOL), "install_desktop_plugin.py")):
            self.assertNotIn("C:\\", text, "%s: жёсткий путь C:\\" % name)
            self.assertNotIn("C:/Users", text, "%s: жёсткий путь C:/Users" % name)
            self.assertNotIn("User", text, "%s: личный путь автора" % name)
        for text, name in ((self.text, "install.sh"),
                           (read(BIN / "team-room-server.sh"), "team-room-server.sh"),
                           (read(BIN / "team-room-stop.sh"), "team-room-stop.sh")):
            self.assertNotIn("%LOCALAPPDATA%", text, "%s: Windows-литерал" % name)

    def test_proc_is_portable(self):
        source = read(ROOT / "server" / "proc.py")
        self.assertIn("IS_WINDOWS", source)
        self.assertIn("IS_MACOS", source)
        self.assertIn("xdg-open", source)
        self.assertIn('["open", target]', source)
        self.assertIn("if flags or IS_WINDOWS", source)

    def test_plugin_tool_knows_macos(self):
        source = read(PLUGIN_TOOL)
        self.assertIn("Application Support", source)
        self.assertIn("HERMES_HOME", source)


class DryRunTests(InstallCase):
    def test_dry_run_changes_nothing(self):
        result = self.run_install("--dry-run", "--no-autostart", "--source-dir", bash_path(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("только проверка", result.stdout)
        self.assertFalse(self.app.exists(), "пробный запуск создал папку программы")
        self.assertFalse((self.hermes / "desktop-plugins").exists(),
                         "пробный запуск поставил плагин")
        self.assertEqual(list(self.tmp.glob("team-room-install-*")), [],
                         "пробный запуск оставил временную папку")


class UninstallTests(InstallCase):
    def make_artifacts(self):
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "systemd" / "user").mkdir(parents=True, exist_ok=True)
        (self.home / "Library" / "LaunchAgents" / "com.teamroom.server.plist").write_text(
            "<plist/>", encoding="utf-8")
        (self.home / ".config" / "systemd" / "user" / "teamroom.service").write_text(
            "[Unit]", encoding="utf-8")
        (self.home / ".profile").write_text(
            "KEEP=1\n# >>> team-room >>>\necho start\n# <<< team-room <<<\n", encoding="utf-8")
        (self.app / "bin").mkdir(parents=True, exist_ok=True)
        (self.app / "server").mkdir(parents=True, exist_ok=True)
        (self.app / "server" / "app.py").write_text("# stub\n", encoding="utf-8")
        (self.app / "data").mkdir(parents=True, exist_ok=True)
        (self.app / "data" / "rooms.json").write_text("{}", encoding="utf-8")
        (self.hermes / "desktop-plugins" / "team-room").mkdir(parents=True, exist_ok=True)
        (self.hermes / "desktop-plugins" / "team-room" / "plugin.js").write_text(
            "// stub", encoding="utf-8")

    def test_uninstall_removes_autostart_and_files_keeps_data(self):
        self.make_artifacts()
        result = self.run_install("--uninstall", "--no-browser")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.teamroom.server.plist").exists(),
                         "plist остался")
        self.assertFalse((self.home / ".config" / "systemd" / "user" / "teamroom.service").exists(),
                         "unit остался")
        self.assertNotIn("team-room", (self.home / ".profile").read_text(encoding="utf-8"),
                         "строка автозапуска осталась в профиле")
        self.assertIn("KEEP=1", (self.home / ".profile").read_text(encoding="utf-8"))
        self.assertFalse((self.hermes / "desktop-plugins" / "team-room").exists(), "плагин остался")
        self.assertFalse((self.app / "server").exists(), "файлы программы остались")
        self.assertTrue((self.app / "data" / "rooms.json").is_file(), "данные удалены без --remove-data")

    def test_uninstall_remove_data(self):
        self.make_artifacts()
        result = self.run_install("--uninstall", "--remove-data", "--no-browser")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.app.exists(), "папка программы с данными осталась")


class RealRunTests(InstallCase):
    """Настоящая установка из локальной папки: сервер, плагин, повтор, остановка."""

    def test_install_health_idempotent_stop(self):
        # Параллельный прогон другого набора тестов может занять тот же порт —
        # тогда повторяем сценарий на другом порту (гонка среды, не дефект продукта).
        for attempt in (1, 2):
            try:
                self.run_scenario()
                return
            except PortTaken as exc:
                if attempt == 2:
                    self.fail("порт так и занят посторонним сервером: %s" % exc)
                self.stop()
                self.port = free_port()

    def run_scenario(self):
        result = self.run_install("--no-autostart", "--no-browser",
                                  "--source-dir", bash_path(ROOT), timeout=300)
        if result.returncode != 0 and self.is_foreign_server():
            raise PortTaken("порт %d занял другой сервер" % self.port)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_our_server("после установки")

        plugin = self.hermes / "desktop-plugins" / "team-room" / "plugin.js"
        self.assertTrue(plugin.is_file(), "плагин не установлен")
        self.assertIn("127.0.0.1:%d/" % self.port, plugin.read_text(encoding="utf-8"),
                      "в плагин не подставлен порт")

        manifest = json.loads((self.app / "install.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["port"], self.port)
        self.assertEqual(manifest["home"].replace("\\", "/").lower(),
                         bash_path(self.app).lower())
        self.assertTrue((self.app / "bin" / "team-room-server.sh").is_file())

        # Первый запуск обязан оставить РОВНО один pid-файл на живой процесс:
        # без него остановка и повторная установка не найдут сервер.
        first_pids = self.wait_pid_files(1, timeout=5)
        self.assertEqual(len(first_pids), 1,
                         "после установки нет pid-файла сервера: %s" % first_pids)
        first_pid = int(first_pids[0].read_text(encoding="utf-8").strip())
        self.assertTrue(self.pid_alive(first_pid),
                        "pid-файл %s указывает на неживой процесс %d"
                        % (first_pids[0].name, first_pid))

        # Идемпотентность: второй запуск не плодит серверы и не теряет данные.
        marker = self.app / "data" / "rooms.json"
        marker.write_text('{"rooms": []}', encoding="utf-8")
        again = self.run_install("--no-autostart", "--no-browser",
                                 "--source-dir", bash_path(ROOT), timeout=300)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assert_our_server("после повторной установки")
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"rooms": []}',
                         "повторная установка затёрла данные")
        pid_files = self.wait_pid_files(1, timeout=5)
        self.assertEqual(len(pid_files), 1, "осталось несколько pid-файлов: %s" % pid_files)
        self.assertEqual(pid_files[0].name, "server-%d.pid" % self.port,
                         "pid-файл назван не по нашему порту: %s" % pid_files[0].name)
        pid = int(pid_files[0].read_text(encoding="utf-8").strip())
        self.assertTrue(self.pid_alive(pid),
                        "после повторной установки pid-файл указывает на неживой процесс %d" % pid)

        # Остановка по pid-файлу.
        stop = subprocess.run(["bash", bash_path(self.app / "bin" / "team-room-stop.sh"),
                               "--home", bash_path(self.app)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, env=self.env())
        self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
        self.assertEqual(health(self.port), 0, "сервер не остановился")
        self.assertEqual(list(self.run_dir().glob("*.pid")), [],
                         "pid-файл остался после остановки")
        self.assertFalse(self.pid_alive(pid), "процесс сервера жив после остановки")

        # Удаление.
        removed = self.run_install("--uninstall", "--remove-data", "--no-browser")
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(self.app.exists(), "папка программы осталась после --uninstall --remove-data")
        self.assertFalse((self.hermes / "desktop-plugins" / "team-room").exists(),
                         "плагин остался после удаления")

    def test_plugin_defaults_to_hermes_home_env(self):
        result = subprocess.run(
            [sys.executable, bash_path(PLUGIN_TOOL), "--port", str(self.port)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env=self.env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.hermes / "desktop-plugins" / "team-room" / "plugin.js").is_file(),
                        "инструмент не взял HERMES_HOME из окружения")


if __name__ == "__main__":
    unittest.main()
