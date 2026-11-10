#!/usr/bin/env python3
"""Тесты сторожа «Командных комнат» (tools/team_room_guard.py).

Герметичность (правило волны 8):
* всё — на ВРЕМЕННОЙ копии «папки Hermes» и временного проекта; настоящие
  %LOCALAPPDATA%\\hermes и C:\\TeamRoom\\Team\\team-room не пишутся;
* порт выбирается СВОБОДНЫЙ из 8890–8899 в setUp и передаётся сторожу
  (`--port`), поэтому тесты проходят и когда боевой 8777/демо 8778 работают,
  и когда они лежат. Боевые порты не опрашиваются и не занимаются;
* `guard.json` (состояние и лимит запусков) живёт в `data/state` временного
  проекта: лимит не «накопится» от прошлых прогонов.

    python -m unittest tests.test_guard -v
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GUARD = PROJECT_ROOT / "tools" / "team_room_guard.py"
PLUGIN_SOURCE = PROJECT_ROOT / "desktop-plugin" / "plugin.js"
CHECK = PROJECT_ROOT / "tools" / "check_desktop_module.py"

# Диапазон свободных портов для своих тестов (WAVE7-RULES §1: 8851–8899).
PORT_RANGE = range(8890, 8900)
# Боевые порты, к которым тесты не обращаются вовсе.
REAL_PORTS = (8777, 8778)

BUNDLE_REL = Path(
    "hermes-agent/apps/desktop/release/win-unpacked/resources/app.asar.unpacked/dist/assets"
)

# Мини-сервер вместо настоящего server/app.py: тот же контракт (--port + /api/health),
# но без Канбана, вотчера и записи в реальные папки.
STUB_SERVER = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = 8777
if "--port" in sys.argv:
    port = int(sys.argv[sys.argv.index("--port") + 1])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "app": {"name": "team-room", "version": "stub", "uptime_s": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

CRASHING_SERVER = """
import sys
print("не смог запуститься", file=sys.stderr)
sys.exit(3)
"""

# Фрагменты взяты из скилла hermes-voice-config: именно так выглядят две
# константы 120 в собранном бандле приложения.
BUNDLE_120 = (
    'var $bt=120;function ext(e){return typeof e==`number`&&Number.isFinite(e)&&e>0?e:120}'
    'function ChatBar(e){let maxRecordingSeconds:o=120,profile:e.profile}'
)

CONFIG_DEFAULTS_120 = (
    "DEFAULTS = {\n"
    '        "voice": {\n'
    '            "max_recording_seconds": 120,\n'
    '        },\n'
    "}\n"
)

VOICE_MIXIN_120 = (
    "        _max_rec = _numeric_or(voice_cfg.get(\"max_recording_seconds\"), None)\n"
    "        rec._max_recording_seconds = _max_rec if _max_rec is not None else 120.0\n"
)


def free_port(exclude: tuple[int, ...] = REAL_PORTS) -> int:
    """Первый свободный порт из PORT_RANGE (боевые 8777/8778 не берём никогда)."""
    for port in PORT_RANGE:
        if port in exclude:
            continue
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            sock.close()
        return port
    raise unittest.SkipTest("нет свободного порта в 8890–8899: заняты чужими процессами")


def health_ok(port: int, timeout: float = 2.0) -> bool:
    """Отвечает ли на порту ИМЕННО наш мини-сервер, а не чужой процесс.

    Наивная проверка «HTTP 200» ловила посторонний сервер (соседскую песочницу)
    и роняла тесты; теперь смотрим имя приложения и версию stub.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(65536).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return False
    app = payload.get("app") if isinstance(payload, dict) else None
    return (isinstance(app, dict)
            and str(app.get("name") or "") == "team-room"
            and str(app.get("version") or "") == "stub")


def port_busy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


class Sandbox:
    """Временный проект + временная папка Hermes на своём порту."""

    def __init__(self, base: Path, server: str = STUB_SERVER, port: int = 0):
        self.base = base
        self.port = int(port or free_port())
        self.project = base / "project"
        self.hermes = base / "hermes"
        (self.project / "desktop-plugin").mkdir(parents=True)
        (self.project / "server").mkdir(parents=True)
        shutil.copyfile(PLUGIN_SOURCE, self.project / "desktop-plugin" / "plugin.js")
        (self.project / "server" / "app.py").write_text(server, encoding="utf-8")
        self.assets = self.hermes / BUNDLE_REL
        self.assets.mkdir(parents=True)
        self.bundle = self.assets / "index-TESTBUILD.js"
        self.bundle.write_text(BUNDLE_120, encoding="utf-8")
        self.config_defaults = self.hermes / "hermes-agent" / "hermes_cli" / "config_defaults.py"
        self.config_defaults.parent.mkdir(parents=True)
        self.config_defaults.write_text(CONFIG_DEFAULTS_120, encoding="utf-8")
        self.voice_mixin = self.config_defaults.parent / "cli_voice_mixin.py"
        self.voice_mixin.write_text(VOICE_MIXIN_120, encoding="utf-8")
        self.plugin_target = self.hermes / "desktop-plugins" / "team-room" / "plugin.js"
        self.state = self.project / "data" / "state" / "guard.json"
        self.log = self.project / "data" / "logs" / "guard.log"
        self.pid_file = self.project / "data" / "run" / ("app-%d.pid" % self.port)

    def run_guard(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(GUARD),
                "--project-root",
                str(self.project),
                "--hermes-home",
                str(self.hermes),
                "--port",
                str(self.port),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace",
        )

    def settle(self) -> None:
        """Сделать окружение «полностью в порядке»: плагин, бандл, дефолты."""
        self.plugin_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.project / "desktop-plugin" / "plugin.js", self.plugin_target)
        self.bundle.write_text(BUNDLE_120.replace("=120", "=600"), encoding="utf-8")
        self.config_defaults.write_text(CONFIG_DEFAULTS_120.replace("120", "600"), encoding="utf-8")
        self.voice_mixin.write_text(VOICE_MIXIN_120.replace("120.0", "600.0"), encoding="utf-8")

    def kill_server(self) -> None:
        """Убить только СВОЙ сервер из pid-файла (не по имени процесса)."""
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return
        if pid <= 0:
            return
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


class ForeignServer:
    """Посторонний HTTP-сервер на нашем порту: отвечает 200, но это не «Комната команды»."""

    def __init__(self, port: int):
        self.port = int(port)
        self._httpd = None
        self._thread = None

    def start(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"hello": "not team room"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._httpd = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % self.port, timeout=2) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="teamroom-guard-")
        self.base = Path(self._tmp.name)
        # Свой свободный порт на каждый тест: не зависим от того, живы ли 8777/8778,
        # и не мешаем соседним песочницам.
        self.port = free_port()
        self.assertNotIn(self.port, REAL_PORTS)
        self.sandbox = Sandbox(self.base, port=self.port)

    def tearDown(self) -> None:
        self.sandbox.kill_server()
        self._tmp.cleanup()

    # 0. герметичность: свой порт, состояние — во временном каталоге
    def test_sandbox_is_hermetic(self) -> None:
        self.assertFalse(port_busy(self.port), "порт из setUp должен быть свободен")
        self.assertTrue(str(self.sandbox.state).startswith(str(self.base)))
        self.assertTrue(str(self.sandbox.pid_file).startswith(str(self.base)))
        self.assertFalse(health_ok(self.port))
        for real in REAL_PORTS:
            self.assertNotEqual(self.port, real)

    # 1. сервер не отвечает → запущен и потом остановлен
    def test_starts_server_when_down(self) -> None:
        self.assertFalse(health_ok(self.port))
        result = self.sandbox.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Сервер «Комнаты команды» не отвечал", result.stdout)
        self.assertTrue(health_ok(self.port), "сервер на порту %d не поднялся" % self.port)
        self.assertTrue(self.sandbox.pid_file.is_file())
        pid = int(self.sandbox.pid_file.read_text(encoding="utf-8").split()[0])
        self.assertGreater(pid, 0)
        self.assertTrue((self.sandbox.project / "data" / "logs" / ("server-%d.out" % self.port)).is_file())

    # 2. плагин удалён → восстановлен
    def test_restores_missing_plugin(self) -> None:
        self.sandbox.settle()
        self.sandbox.plugin_target.unlink()
        result = self.sandbox.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Плагин «Командные комнаты» пропал или устарел — восстановил", result.stdout)
        self.assertTrue(self.sandbox.plugin_target.is_file())
        self.assertEqual(
            self.sandbox.plugin_target.read_bytes(),
            (self.sandbox.project / "desktop-plugin" / "plugin.js").read_bytes(),
        )

    # 3. бандл вернулся к 120 → починен, бэкап создан, повтор идемпотентен
    def test_patches_bundle_and_is_idempotent(self) -> None:
        self.sandbox.settle()
        self.sandbox.bundle.write_text(BUNDLE_120, encoding="utf-8")
        result = self.sandbox.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("вернулся лимит диктовки 2 минуты", result.stdout)
        patched = self.sandbox.bundle.read_text(encoding="utf-8")
        self.assertNotIn("maxRecordingSeconds:o=120", patched)
        self.assertIn("maxRecordingSeconds:o=600", patched)
        backup = self.sandbox.bundle.with_name(self.sandbox.bundle.name + ".bak-voicecap")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), BUNDLE_120)
        self.assertIn('"max_recording_seconds": 600', self.sandbox.config_defaults.read_text(encoding="utf-8"))
        self.assertIn("else 600.0", self.sandbox.voice_mixin.read_text(encoding="utf-8"))

        second = self.sandbox.run_guard()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("лимит диктовки", second.stdout)
        self.assertEqual(self.sandbox.bundle.read_text(encoding="utf-8"), patched)
        self.assertEqual(backup.read_text(encoding="utf-8"), BUNDLE_120)

    # 4. всё в порядке → пустой stdout
    def test_silent_when_everything_fine(self) -> None:
        self.sandbox.settle()
        first = self.sandbox.run_guard()  # поднимает сервер — тут вывод не пустой
        self.assertTrue(health_ok(self.port))
        self.assertIn("Сервер «Комнаты команды» не отвечал", first.stdout)
        second = self.sandbox.run_guard()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), "", second.stdout)

    # 5. больше 3 запусков за 10 минут → аларм вместо нового запуска
    def test_start_rate_limit(self) -> None:
        self.sandbox.settle()
        # Отдельный порт и отдельный guard.json: лимит не смешивается с другими тестами.
        crash_port = free_port((*REAL_PORTS, self.port))
        crashing = Sandbox(self.base / "crash", server=CRASHING_SERVER, port=crash_port)
        crashing.settle()
        outputs = []
        for _ in range(4):
            result = crashing.run_guard()
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout)
        self.assertTrue(
            any("упал при запуске" in out for out in outputs),
            "не увидел сообщения о падении: %r" % outputs,
        )
        self.assertIn("Не могу поднять сервис", outputs[-1])
        self.assertIn("попытки за 10 минут", outputs[-1])
        self.assertTrue(crashing.state.is_file(), "состояние — во временном проекте")
        self.assertTrue(str(crashing.state).startswith(str(self.base)))

    # 6. чужой процесс на порту не убивается
    def test_foreign_process_on_port_is_not_killed(self) -> None:
        self.sandbox.settle()
        foreign = ForeignServer(self.port)
        foreign.start()
        try:
            result = self.sandbox.run_guard()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("занят не «Комнатой команды»", result.stdout)
            self.assertIn(str(self.port), result.stdout)
            self.assertFalse(self.sandbox.pid_file.exists(), "не должен был запускать сервер")
            self.assertTrue(foreign.alive())
        finally:
            foreign.stop()

    # 7. пробный запуск ничего не пишет
    def test_dry_run_writes_nothing(self) -> None:
        self.sandbox.plugin_target.parent.mkdir(parents=True, exist_ok=True)
        result = self.sandbox.run_guard("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("восстановил бы", result.stdout)
        self.assertIn("запустил бы заново", result.stdout)
        self.assertIn(str(self.port), result.stdout, "сторож говорит про свой порт")
        self.assertEqual(self.sandbox.bundle.read_text(encoding="utf-8"), BUNDLE_120)
        self.assertFalse(
            self.sandbox.bundle.with_name(self.sandbox.bundle.name + ".bak-voicecap").exists()
        )
        self.assertFalse(self.sandbox.plugin_target.exists())
        self.assertFalse(self.sandbox.state.exists())
        self.assertFalse(self.sandbox.log.exists())
        self.assertFalse(self.sandbox.pid_file.exists())
        self.assertFalse(health_ok(self.port))

    # 8. состояние и смена бандла
    def test_state_file_and_bundle_change_message(self) -> None:
        self.sandbox.settle()
        self.sandbox.run_guard()
        state = json.loads(self.sandbox.state.read_text(encoding="utf-8"))
        self.assertIn(self.sandbox.bundle.name, state["bundle"]["files"])
        self.assertTrue(state["plugin_ok"])

        # Hermes «обновился»: другой файл бандла
        self.sandbox.bundle.unlink()
        new_bundle = self.sandbox.assets / "index-NEWBUILD.js"
        new_bundle.write_text(BUNDLE_120.replace("=120", "=600"), encoding="utf-8")
        result = self.sandbox.run_guard()
        self.assertIn("Hermes обновился — модуль проверен", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
