"""Тесты «тихого» запуска процессов: один помощник, всегда без консольного окна.

Проверяем три вещи:
  * `server/proc.py` действительно ставит `CREATE_NO_WINDOW` (и не ставит его на
    POSIX, где `creationflags` не принимается), а для фоновых процессов —
    `CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS`; для GUI-программ (проводник)
    флага нет, а потоки уведены в DEVNULL;
  * ни один модуль `server/` не порождает процессы мимо помощника — иначе окно
    вернётся в одном забытом месте (grep-тест по исходникам);
  * ошибки запуска обрабатываются по-прежнему: `FileNotFoundError` и
    `TimeoutExpired` превращаются в те же русские ошибки, что и раньше.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import proc                                        # noqa: E402

SERVER_DIR = ROOT / "server"

#: Прямые вызовы запуска процессов — разрешены только в `proc.py` (и `safety.py`,
#: который сам подменяет `subprocess` для тестов). Это строки-образцы для поиска,
#: а не вызовы: ничего здесь не исполняется.
SPAWN_PATTERNS = (
    "subprocess.run(", "subprocess.Popen(", "subprocess.call(", "subprocess.check_output(",
    "subprocess.check_call(", "subprocess.getoutput(", "subprocess.getstatusoutput(",
    "os.system(", "os.popen(", "os.spawn", "shell=True",
)


class FakeModule:
    """Подмена модуля subprocess: запоминает argv и kwargs, ничего не запускает."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list = []
        self.error = error

    def _record(self, argv, kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append(([str(a) for a in argv], dict(kwargs)))
        return "result"

    def run(self, argv, **kwargs):
        return self._record(argv, kwargs)

    def Popen(self, argv, **kwargs):        # noqa: N802 - имя как у subprocess
        return self._record(argv, kwargs)


class TestFlags(unittest.TestCase):
    def test_no_window_value(self):
        if proc.IS_WINDOWS:
            self.assertEqual(proc.no_window(), 0x08000000)
        else:
            self.assertEqual(proc.no_window(), 0)

    def test_detached_flags_value(self):
        if proc.IS_WINDOWS:
            self.assertEqual(proc.detached_flags(), 0x08000000 | 0x00000200)
            # DETACHED_PROCESS сам открывает окно Windows Terminal — не ставим
            self.assertFalse(proc.detached_flags() & 0x00000008)
        else:
            self.assertEqual(proc.detached_flags(), 0)

    def test_run_passes_flags(self):
        fake = FakeModule()
        result = proc.run(["hermes", "kanban", "--json"], module=fake,
                          capture_output=True, text=True, timeout=5)
        self.assertEqual(result, "result")
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv, ["hermes", "kanban", "--json"])
        self.assertEqual(kwargs.get("creationflags", 0), proc.no_window())
        self.assertEqual(kwargs["timeout"], 5)
        self.assertTrue(kwargs["capture_output"])

    def test_run_merges_explicit_flags(self):
        fake = FakeModule()
        proc.run(["x"], module=fake, creationflags=0x00000010)
        _, kwargs = fake.calls[0]
        self.assertEqual(kwargs["creationflags"], proc.no_window() | 0x00000010)

    def test_posix_does_not_pass_creationflags(self):
        fake = FakeModule()
        with mock.patch.object(proc, "IS_WINDOWS", False):
            proc.run(["x"], module=fake)
            proc.popen(["y"], module=fake)
        for _, kwargs in fake.calls:
            self.assertNotIn("creationflags", kwargs)

    def test_popen_passes_flags_and_close_fds(self):
        fake = FakeModule()
        proc.popen(["python", "server/app.py"], module=fake, env={})
        _, kwargs = fake.calls[0]
        self.assertEqual(kwargs.get("creationflags", 0), proc.no_window())
        self.assertTrue(kwargs["close_fds"])

    def test_detached_popen_is_background(self):
        fake = FakeModule()
        with mock.patch.object(proc, "IS_WINDOWS", True):
            proc.popen(["python", "server/app.py"], module=fake, detached=True)
        _, kwargs = fake.calls[0]
        self.assertTrue(kwargs["creationflags"] & 0x00000200)      # CREATE_NEW_PROCESS_GROUP
        self.assertTrue(kwargs["creationflags"] & 0x08000000)      # CREATE_NO_WINDOW
        self.assertFalse(kwargs["creationflags"] & 0x00000008)     # без DETACHED

    def test_gui_popen_has_no_console_flag(self):
        fake = FakeModule()
        with mock.patch.object(proc, "IS_WINDOWS", True):
            proc.popen(["explorer", "/select,C:\\tmp\\a.md"], module=fake, gui=True)
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv[0], "explorer")
        self.assertEqual(kwargs.get("creationflags", 0), 0)         # окно проводника видно
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_shell_true_rejected(self):
        for call in (proc.run, proc.popen):
            with self.assertRaises(ValueError):
                call("dir C:\\ /s", module=FakeModule(), shell=True)


class TestErrors(unittest.TestCase):
    def test_timeout_propagates(self):
        fake = FakeModule(subprocess.TimeoutExpired(["x"], 1))
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.run(["x"], module=fake)

    def test_missing_exe_propagates(self):
        fake = FakeModule(FileNotFoundError("нет такого файла"))
        with self.assertRaises(FileNotFoundError):
            proc.run(["hermes"], module=fake)

    def test_engine_cli_missing_exe_is_engine_error(self):
        from server import engine_cli

        tmp = Path(tempfile.mkdtemp(prefix="proc-test-"))
        cfg = SimpleNamespace(hermes_cli=str(tmp / "нет-такого-hermes.exe"),
                              kanban_home=tmp, log_dir=tmp, app_dir="")
        with self.assertRaises(engine_cli.EngineWriteError) as ctx:
            engine_cli._run(cfg, None, ["list"])
        self.assertEqual(ctx.exception.code, "engine_error")
        self.assertIn("Не найден исполняемый файл", ctx.exception.message_ru)

    def test_engine_cli_timeout_is_engine_error(self):
        from server import engine_cli

        tmp = Path(tempfile.mkdtemp(prefix="proc-test-"))
        cfg = SimpleNamespace(hermes_cli="hermes", kanban_home=tmp, log_dir=tmp, app_dir="")
        with mock.patch.object(engine_cli.proc, "run",
                               side_effect=subprocess.TimeoutExpired(["hermes"], 30)):
            with self.assertRaises(engine_cli.EngineWriteError) as ctx:
                engine_cli._run(cfg, None, ["list"], timeout=30)
        self.assertEqual(ctx.exception.code, "engine_error")
        self.assertIn("не ответил", ctx.exception.message_ru)

    def test_agent_bridge_timeout_is_model_error(self):
        from server import agent_models

        cfg = SimpleNamespace(app_dir=str(ROOT), hermes_home=str(ROOT))
        with mock.patch.object(agent_models, "bridge_path", return_value=Path(__file__)), \
                mock.patch.object(agent_models, "_python", return_value=sys.executable), \
                mock.patch.object(agent_models, "_env", return_value={}), \
                mock.patch.object(agent_models.proc, "run",
                                  side_effect=subprocess.TimeoutExpired(["python"], 40)):
            with self.assertRaises(agent_models.ModelError) as ctx:
                agent_models._bridge(cfg, ["catalog"])
        self.assertEqual(ctx.exception.code, "unavailable")

    def test_agent_bridge_missing_python_is_model_error(self):
        from server import agent_models

        cfg = SimpleNamespace(app_dir=str(ROOT), hermes_home=str(ROOT))
        with mock.patch.object(agent_models, "bridge_path", return_value=Path(__file__)), \
                mock.patch.object(agent_models, "_python", return_value=sys.executable), \
                mock.patch.object(agent_models, "_env", return_value={}), \
                mock.patch.object(agent_models.proc, "run",
                                  side_effect=OSError("не запускается")):
            with self.assertRaises(agent_models.ModelError) as ctx:
                agent_models._bridge(cfg, ["catalog"])
        self.assertEqual(ctx.exception.code, "unavailable")


class TestNoDirectSpawn(unittest.TestCase):
    """grep-тест: в `server/` процессы запускаются только через `server/proc.py`."""

    ALLOWED = {"proc.py", "safety.py"}

    def test_server_modules_use_proc(self):
        problems: list[str] = []
        for path in sorted(SERVER_DIR.glob("*.py")):
            if path.name in self.ALLOWED:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in SPAWN_PATTERNS:
                    if pattern in line:
                        problems.append("%s:%d: %s" % (path.name, lineno, stripped[:110]))
        self.assertEqual(problems, [], "запуск процесса мимо server/proc.py:\n" + "\n".join(problems))

    def test_proc_is_the_only_place_with_flags(self):
        text = (SERVER_DIR / "proc.py").read_text(encoding="utf-8")
        self.assertIn("CREATE_NO_WINDOW", text)
        self.assertIn("DETACHED_PROCESS", text)
        self.assertIn("CREATE_NEW_PROCESS_GROUP", text)

    def test_stt_worker_reuses_proc(self):
        from server import stt_worker

        self.assertEqual(stt_worker.no_window(), proc.no_window())


if __name__ == "__main__":
    unittest.main()
