"""Тесты системной защиты `server/safety.py` (инцидент 28.10.2026).

# SHIM-OK: этот файл проверяет перехват `hermes ... dispatch|decompose|swarm|kick`:
# исключение бросается ДО запуска процесса, реальный CLI не вызывается.

Что проверяется:
* рантайм — запись в реальные `<hermes_home>/config.yaml`,
  `<hermes_home>/profiles/<x>/profile.yaml` и `<Vault>/…` под
  `forbid_real_writes()` даёт `SafetyError`, файлы не создаются/не меняются;
  временный каталог и `<hermes_home>/cache/scratch` писать можно;
* `Sandbox` включает оба запрета на входе и снимает на выходе;
* `forbid_real_subprocess()` не даёт запустить агентские глаголы `hermes`,
  но пропускает безопасные (`kanban list`) и снимается после блока;
* статика — в `tools/*.py` и `tests/*.py` нет записи по пути от `hermes_home`
  в обход `safety.*`, а `dispatch|decompose|swarm|kick` в `subprocess` есть
  только в файлах с маркером `# SHIM-OK`.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import safety  # noqa: E402
from tests.helpers import Sandbox  # noqa: E402

TOOLS_DIR = PROJECT_ROOT / "tools"
TESTS_DIR = PROJECT_ROOT / "tests"
#: функции/методы, которые пишут на диск
WRITE_METHODS = {"write_text", "write_bytes", "mkdir", "unlink", "rename",
                 "replace", "touch", "open"}
SHUTIL_WRITES = {"copy", "copy2", "copyfile", "copytree", "move", "rmtree"}
OS_WRITES = {"replace", "rename", "remove", "unlink", "rmdir"}
#: признаки того, что путь происходит от реального Hermes/Vault
PATH_HINTS = ("hermes_home", "real_vault")
#: глаголы, запускающие реальных агентов
AGENT_VERBS = {"dispatch", "decompose", "swarm", "kick"}
SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_output", "check_call", "system"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mode_is_write(node: ast.Call) -> bool:
    mode = None
    if len(node.args) > 1:
        mode = node.args[1]
    else:
        mode = next((k.value for k in node.keywords if k.arg == "mode"), None)
    return (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
            and any(ch in mode.value for ch in "wax+"))


def _write_call(node: ast.Call):
    """(имя, [выражения-пути]) для опасных вызовов записи, иначе None."""
    func = node.func
    if isinstance(func, ast.Attribute):
        name = func.attr
        recv = ast.unparse(func.value)
        args = [ast.unparse(a) for a in node.args]
        if name in WRITE_METHODS - {"open"}:
            return name, [recv] + args
        if name == "open" and _mode_is_write(node):
            return name, [recv] + args
        if name in SHUTIL_WRITES or name in OS_WRITES:
            return name, args
    elif isinstance(func, ast.Name) and func.id == "open" and _mode_is_write(node):
        return "open", [ast.unparse(a) for a in node.args]
    return None


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _strings_in(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


class RuntimeWriteGuardTest(unittest.TestCase):
    """Рантайм: реальный Hermes/Vault защищены, песочница и temp — нет."""

    def setUp(self) -> None:
        self.real = safety.real_hermes_home()
        self.probe_dir = self.real / "profiles" / ("_safety-probe-" + uuid.uuid4().hex[:12])
        self.probe_file = self.probe_dir / "profile.yaml"

    def tearDown(self) -> None:
        # если защита когда-нибудь сломается, тест не оставит мусор в реальном Hermes
        if self.probe_dir.exists():
            shutil.rmtree(self.probe_dir, ignore_errors=True)

    def test_config_yaml_write_blocked_and_unchanged(self):
        cfg_path = self.real / "config.yaml"
        if not cfg_path.is_file():
            self.skipTest("реального config.yaml нет — проверять нечего")
        before = _sha256(cfg_path)
        text = cfg_path.read_text(encoding="utf-8")
        with safety.forbid_real_writes():
            with self.assertRaises(safety.SafetyError):
                safety.guarded_write_text(cfg_path, text)      # тот же текст: даже при сбое не портим
            with self.assertRaises(safety.SafetyError):
                cfg_path.write_text(text, encoding="utf-8")
            with self.assertRaises(safety.SafetyError):
                cfg_path.open("a", encoding="utf-8")
        self.assertEqual(_sha256(cfg_path), before, "реальный config.yaml изменился!")

    def test_profile_yaml_blocked_before_creation(self):
        self.assertFalse(self.probe_file.exists())
        with safety.forbid_real_writes():
            for write in (lambda: safety.guarded_write_text(self.probe_file, "title: x\n"),
                          lambda: self.probe_file.write_text("title: x\n", encoding="utf-8"),
                          lambda: self.probe_file.open("w", encoding="utf-8"),
                          lambda: open(self.probe_file, "w", encoding="utf-8")):
                with self.assertRaises(safety.SafetyError):
                    write()
        self.assertFalse(self.probe_file.exists(), "файл в реальном профиле создан!")
        self.assertFalse(self.probe_dir.exists(), "каталог в реальных профилях создан!")

    def test_vault_write_blocked(self):
        target = safety.real_vault() / ("_safety-probe-%s.md" % uuid.uuid4().hex[:12])
        try:
            with safety.forbid_real_writes():
                with self.assertRaises(safety.SafetyError) as ctx:
                    safety.guarded_write_text(target, "# probe\n")
            self.assertIn("Vault", str(ctx.exception))
            self.assertFalse(target.exists())
        finally:
            if target.exists():
                target.unlink()

    def test_destructive_ops_blocked(self):
        ghost = self.real / "profiles" / ("_safety-probe-%s" % uuid.uuid4().hex[:12])
        with safety.forbid_real_writes():
            with self.assertRaises(safety.SafetyError):
                shutil.rmtree(ghost)
            with self.assertRaises(safety.SafetyError):
                ghost.mkdir(parents=True)

    def test_temp_and_scratch_allowed(self):
        with safety.forbid_real_writes():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "note.txt"
                path.write_text("ok", encoding="utf-8")
                self.assertEqual(path.read_text(encoding="utf-8"), "ok")
                image = Path(tmp) / "shot.png"
                image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)   # данные — не путь
                self.assertEqual(image.read_bytes()[:4], b"\x89PNG")
            scratch = self.real / "cache" / "scratch" / ("probe-%s.txt" % uuid.uuid4().hex[:8])
            safety.assert_not_real(scratch)           # исключение не бросается
            self.assertFalse(scratch.exists())

    def test_guard_restores_after_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "after.txt"
            with safety.forbid_real_writes():
                with self.assertRaises(safety.SafetyError):
                    (self.real / "config.yaml").write_text("x")
            path.write_text("ok", encoding="utf-8")   # вне блока запись снова работает
            self.assertTrue(path.is_file())


class WriteSemanticsTest(unittest.TestCase):
    """D20: перехват проверяет ТОЛЬКО путь, данные (байты/текст) — не путь.

    Регрессия из docs/critic/wave5-ui.md: `Path.write_bytes` падал `SafetyError`,
    потому что охранник принимал PNG-байты за путь.
    """

    #: данные, которые выглядят как запрещённый путь — их нельзя путать с путём
    PATH_LIKE = (b"C:\\Users\\User\\AppData\\Local\\hermes\\config.yaml\x00\x01PNG")

    def test_data_never_checked_as_path(self):
        with safety.forbid_real_writes(), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sb"
            root.mkdir()
            # write_bytes с байтами, похожими на путь
            image = root / "shot.png"
            image.write_bytes(self.PATH_LIKE)
            self.assertEqual(image.read_bytes(), self.PATH_LIKE)
            # write_text, где текст — тоже «путь»
            note = root / "note.txt"
            note.write_text("C:\\TeamRoom\\Vault\\x.md", encoding="utf-8")
            self.assertEqual(note.read_text(encoding="utf-8"), "C:\\TeamRoom\\Vault\\x.md")
            # open('wb') в песочнице
            with (root / "raw.bin").open("wb") as fh:
                fh.write(self.PATH_LIKE)
            # копирование и замена внутри песочницы
            shutil.copyfile(image, root / "copy.png")
            os.replace(root / "copy.png", root / "moved.png")
            self.assertTrue((root / "moved.png").is_file())

    def test_scratch_is_writable(self):
        scratch = safety.real_hermes_home() / "cache" / "scratch"
        target = scratch / ("guard-%s.png" % uuid.uuid4().hex[:10])
        try:
            with safety.forbid_real_writes():
                scratch.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.PATH_LIKE)
            self.assertEqual(target.read_bytes(), self.PATH_LIKE)
        finally:
            try:
                target.unlink()
            except OSError:
                pass

    def test_real_home_and_vault_still_blocked(self):
        home_target = safety.real_hermes_home() / "profiles" / ("_safety-probe-%s" % uuid.uuid4().hex[:8])
        vault_target = safety.real_vault() / ("_safety-probe-%s.png" % uuid.uuid4().hex[:8])
        with safety.forbid_real_writes(), tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.bin"
            src.write_bytes(b"x")
            cases = (
                lambda: (home_target / "x.png").write_bytes(self.PATH_LIKE),
                lambda: (home_target / "x.txt").write_text("x", encoding="utf-8"),
                lambda: (home_target / "x.bin").open("wb"),
                lambda: vault_target.write_bytes(self.PATH_LIKE),
                lambda: shutil.copyfile(src, home_target / "x.png"),
                lambda: os.replace(src, vault_target),
            )
            for index, case in enumerate(cases):
                with self.subTest(case=index):
                    with self.assertRaises(safety.SafetyError):
                        case()
        self.assertFalse(home_target.exists(), "каталог в реальном Hermes создан!")
        self.assertFalse(vault_target.exists(), "файл в реальном Vault создан!")


class SandboxAutoGuardTest(unittest.TestCase):
    """`Sandbox` включает запреты сам — даже если тест забыл про safety."""

    def test_sandbox_enables_and_restores_guards(self):
        with Sandbox() as sb:
            probe = self._probe_path()
            with self.assertRaises(safety.SafetyError):
                probe.write_text("x", encoding="utf-8")
            # песочная запись работает
            inner = Path(sb.root) / "guard-probe.txt"
            inner.write_text("ok", encoding="utf-8")
            self.assertEqual(inner.read_text(encoding="utf-8"), "ok")
            with self.assertRaises(safety.SafetyError):
                subprocess.run(["hermes", "kanban", "dispatch"])
        self.assertFalse(probe.exists())
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "free.txt").write_text("ok", encoding="utf-8")
        self.assertFalse(self._probe_parent().exists())

    @staticmethod
    def _probe_path() -> Path:
        return safety.real_hermes_home() / "profiles" / "_safety-probe-sandbox" / "profile.yaml"

    @staticmethod
    def _probe_parent() -> Path:
        return safety.real_hermes_home() / "profiles" / "_safety-probe-sandbox"


class SubprocessGuardTest(unittest.TestCase):
    """`forbid_real_subprocess`: агентские глаголы запрещены, чтение — можно."""

    def test_agent_verbs_blocked(self):
        cases = (["hermes", "kanban", "dispatch"],
                 ["hermes.exe", "kanban", "--board", "team", "decompose"],
                 [r"C:\tools\hermes.cmd", "profile", "add", "x"],
                 "hermes kanban swarm --json")
        with safety.forbid_real_subprocess():
            for argv in cases:
                with self.subTest(argv=argv):
                    with self.assertRaises(safety.SafetyError):
                        subprocess.run(argv)
                    with self.assertRaises(safety.SafetyError):
                        subprocess.Popen(argv)

    def test_safe_verb_passes_to_popen(self):
        calls: list = []
        real_popen = subprocess.Popen

        class FakePopen:
            def __init__(self, *args, **kwargs):
                calls.append(args)

        subprocess.Popen = FakePopen
        try:
            with safety.forbid_real_subprocess():
                subprocess.Popen(["hermes", "kanban", "list"])
            self.assertEqual(calls, [(["hermes", "kanban", "list"],)])
        finally:
            subprocess.Popen = real_popen

    def test_guard_restores(self):
        real_popen = subprocess.Popen
        with safety.forbid_real_subprocess():
            self.assertIsNot(subprocess.Popen, real_popen)
        self.assertIs(subprocess.Popen, real_popen)


class StaticWriteScanTest(unittest.TestCase):
    """AST-скан: запись по пути от hermes_home/real_vault — только через safety.*."""

    def test_no_raw_writes_to_real_home(self):
        bad: list[str] = []
        for path in sorted(TOOLS_DIR.glob("*.py")) + sorted(TESTS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            lines = source.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                found = _write_call(node)
                if not found:
                    continue
                name, exprs = found
                if not any(hint in expr.lower() for expr in exprs for hint in PATH_HINTS):
                    continue
                if "safety." in ast.unparse(node.func):
                    continue
                window = lines[max(0, node.lineno - 2):node.end_lineno]
                if any("SAFETY-OK" in line for line in window):
                    continue
                bad.append("%s:%d: %s() с путём от hermes_home без safety.*"
                           % (path.relative_to(PROJECT_ROOT), node.lineno, name))
        self.assertEqual(bad, [], "небезопасная запись:\n" + "\n".join(bad))


class StaticSubprocessScanTest(unittest.TestCase):
    """AST-скан: dispatch/decompose/swarm/kick в tests/ — только с маркером # SHIM-OK."""

    def test_agent_verbs_only_with_shim_marker(self):
        bad: list[str] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "# SHIM-OK" in source:
                continue
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _call_name(node) not in SUBPROCESS_FUNCS:
                    continue
                words = set(re.findall(r"[a-z]+", " ".join(_strings_in(node)).lower()))
                if words & AGENT_VERBS:
                    bad.append("%s:%d: %s()" % (path.relative_to(PROJECT_ROOT),
                                               node.lineno, _call_name(node)))
        self.assertEqual(bad, [], "subprocess с агентскими глаголами без # SHIM-OK:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
