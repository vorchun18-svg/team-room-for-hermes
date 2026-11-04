"""Переносимость: приложение не зависит от путей, Vault, whisper и hermes CLI.

Проверяем то, что нельзя проверить глазами на нашей машине:

* в исходниках (то, что уходит в релиз) нет путей автора — `C:\\TeamRoom`,
  `C:\\Users\\<имя>`, логина, `/c/Users/…`;
* `server/config.py` в чистом окружении не подставляет пути автора: HERMES_HOME
  определяется сам, Vault — по наличию, а не по адресу;
* без Vault зеркало выключено и есть подсказка `TEAMROOM_VAULT_DIR`; без
  faster-whisper диктовка честно говорит «не установлено»; ошибка записи в
  канбан — понятный русский текст без трассировки;
* `tools/portability_check.py` проходит оба сценария: «чистая машина» и копия
  живой структуры Hermes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import config as config_mod   # noqa: E402
from server import engine_cli, rooms, stt  # noqa: E402

#: что уходит в релиз и не должно содержать путей автора
SCAN_DIRS = ("server", "web", "tools", "desktop-plugin", "reportkit", "fixtures")
SCAN_FILES = ("README.md", "LICENSE", "start-demo-room.cmd", "start-team-room.cmd",
              "stop-team-room.cmd")
SCAN_EXT = {".py", ".js", ".mjs", ".css", ".html", ".json", ".cmd", ".ps1", ".md"}
#: файлы, где шаблоны путей — это данные проверки, а не наш адрес
SCAN_SKIP = {"tools/release_sanitize.json", "tools/build_release.py"}

#: логин владельца собираем из частей: сам литерал в исходниках не нужен
OWNER_LOGIN = "vorchun" + "18"

FORBIDDEN = (
    ("путь C:\\TeamRoom", re.compile(r"[A-Za-z]:[\\/]{1,4}Hermes\b")),
    ("имя пользователя Windows", re.compile(r"\bMarks\b")),
    ("логин владельца", re.compile(OWNER_LOGIN + r"(?!-svg/team-room-for-hermes)")),
    ("путь /c/Users/…", re.compile(r"/c/Users/[A-Za-z]")),
    ("профиль C:\\Users\\<имя>",
     re.compile(r"[A-Za-z]:[\\/]{1,4}Users[\\/]{1,4}(?!User\b|Public\b|Default\b)[A-Za-zА-Яа-я]")),
)


def source_files():
    """Текстовые файлы релизной части проекта (без кэшей, отладочных страниц и docs)."""
    out = []
    for name in SCAN_DIRS:
        base = PROJECT_ROOT / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SCAN_EXT:
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if rel in SCAN_SKIP or "__pycache__" in rel:
                continue
            if rel.startswith("web/_"):          # отладочные макеты, в релиз не идут
                continue
            out.append(path)
    for name in SCAN_FILES:
        path = PROJECT_ROOT / name
        if path.is_file():
            out.append(path)
    return out


class SourcePathsTest(unittest.TestCase):
    """grep по исходникам: ни одного адреса автора."""

    def test_no_author_paths_in_sources(self):
        found = []
        for path in source_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for name, pattern in FORBIDDEN:
                    if pattern.search(line):
                        found.append("%s:%d — %s: %s"
                                     % (path.relative_to(PROJECT_ROOT).as_posix(), lineno, name,
                                        line.strip()[:120]))
        self.assertEqual(found, [], "в исходниках остались пути автора:\n" + "\n".join(found[:20]))

    def test_config_has_no_hardcoded_paths(self):
        text = (PROJECT_ROOT / "server" / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("C:\\TeamRoom", text)
        self.assertNotIn("C:/TeamRoom", text)
        self.assertNotIn("Users\\User", text)
        self.assertNotIn("Users/User", text)


class ConfigDefaultsTest(unittest.TestCase):
    """Дефолты считаются от окружения и домашнего каталога, а не от нашей машины."""

    KEYS = ("HERMES_HOME", "HERMES_KANBAN_HOME", "TEAMROOM_HOME", "TEAMROOM_DATA",
            "TEAMROOM_VAULT_DIR", "TEAMROOM_HERMES_CLI", "TEAMROOM_HERMES_HOME",
            "TEAMROOM_EXTRA_ROOTS", "OBSIDIAN_VAULT_PATH", "TEAMROOM_SEED")

    def _clean_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in self.KEYS}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_teamroom_home_is_user_level(self):
        with self._clean_env(LOCALAPPDATA=str(Path.home() / "AppData" / "Local")):
            home = config_mod.teamroom_home()
        self.assertEqual(home.name, "TeamRoom")
        self.assertNotIn("Hermes", str(home))

    def test_hermes_home_detected_without_env(self):
        with self._clean_env():
            home = config_mod.real_hermes_home()
        self.assertIsInstance(home, Path)
        text = str(home)
        self.assertTrue(text.endswith("hermes") or text.endswith(".hermes"), text)
        self.assertNotIn("Team", text)

    def test_cli_missing_is_empty_not_broken(self):
        with self._clean_env(), mock.patch.object(config_mod.shutil, "which", return_value=None), \
                mock.patch.object(config_mod, "_hermes_cli_candidates", return_value=[]):
            self.assertEqual(config_mod.detect_hermes_cli(), "")

    def test_vault_absent_gives_no_mirror_and_hint(self):
        with self._clean_env(TEAMROOM_VAULT_DIR=""), \
                mock.patch.object(config_mod, "detected_vault_root", return_value=None):
            cfg = config_mod.load()
        self.assertFalse(cfg.vault_enabled)
        self.assertFalse(config_mod.vault_root_dir(cfg))
        self.assertIn("TEAMROOM_VAULT_DIR", config_mod.VAULT_HINT_RU)
        # путь-заглушка не выходит за пределы данных приложения
        self.assertIn("Rooms", str(config_mod.room_vault_dir(cfg, "room")))

    def test_vault_off_values(self):
        for value in ("", "off", "none", "0", "false"):
            with self._clean_env(TEAMROOM_VAULT_DIR=value):
                self.assertIsNone(config_mod.detect_vault_dir(), value)

    def test_explicit_vault_dir_is_honoured(self):
        with self._clean_env(TEAMROOM_VAULT_DIR=str(PROJECT_ROOT / "data" / "_portable_vault")):
            self.assertEqual(config_mod.detect_vault_dir(),
                             PROJECT_ROOT / "data" / "_portable_vault")

    def test_demo_rooms_not_seeded_without_vault(self):
        with self._clean_env(TEAMROOM_VAULT_DIR=""), \
                mock.patch.object(config_mod, "detected_vault_root", return_value=None):
            cfg = config_mod.load()
        self.assertFalse(rooms.seed_enabled(cfg))

    def test_demo_rooms_seeded_when_vault_found(self):
        cfg = config_mod.load()
        cfg.vault_enabled = True
        cfg.vault_dir = Path(PROJECT_ROOT / "data" / "_portable_vault")
        self.assertTrue(rooms.seed_enabled(cfg))


class MissingPiecesTest(unittest.TestCase):
    """Нет whisper / нет CLI / нет доски — работаем и объясняем по-русски."""

    def test_stt_without_faster_whisper_says_not_installed(self):
        cfg = config_mod.Cfg(hermes_home=Path.home() / ".hermes",
                             kanban_home=PROJECT_ROOT / "data" / "_none",
                             data_dir=PROJECT_ROOT / "data" / "_portable_tmp",
                             vault_dir=None, vault_enabled=False)
        with mock.patch.object(stt.stt_worker, "find_python", return_value=None):
            status = stt.status(cfg)
        self.assertFalse(status["available"])
        self.assertEqual(status["mode"], "none")
        reason = str(status["reason"] or "")
        self.assertTrue(reason, "без диктовки нужна понятная причина")
        self.assertTrue("faster-whisper" in reason or "диктовк" in reason.lower(), reason)

    def test_stt_config_path_without_cfg_home(self):
        cfg = config_mod.Cfg(hermes_home=None, kanban_home=Path.home(),
                             data_dir=Path.home(), vault_dir=None)
        with mock.patch.dict(os.environ, {"HERMES_HOME": ""}, clear=False):
            path = stt.hermes_config_path(cfg)
        self.assertEqual(path.name, "config.yaml")
        self.assertEqual(path.parent.name, ".hermes")
        self.assertEqual(path.parent.parent, Path.home())

    def test_engine_error_hides_traceback(self):
        noisy = ('Traceback (most recent call last):\n'
                 '  File "C:\\x\\worker.py", line 125, in <module>\n'
                 '    main()\n'
                 '    ~~~~^^\n'
                 'OSError: [Errno 22] Invalid argument\n')
        err = engine_cli.EngineWriteError("engine_error", "Канбан не ответил", noisy)
        detail = err.to_dict()["error"]["detail"]
        self.assertNotIn("Traceback", json.dumps(detail, ensure_ascii=False))
        self.assertNotIn('File "', json.dumps(detail, ensure_ascii=False))
        self.assertIn("OSError", detail["stderr"])

    def test_engine_write_reports_missing_board_without_cli_call(self):
        cfg = config_mod.Cfg(hermes_home=Path.home() / ".hermes",
                             kanban_home=PROJECT_ROOT / "data" / "_no_board_here",
                             data_dir=PROJECT_ROOT / "data" / "_portable_tmp",
                             vault_dir=None, vault_enabled=False,
                             hermes_cli="hermes")
        with mock.patch.object(engine_cli.proc, "run") as run:
            with self.assertRaises(engine_cli.EngineWriteError) as ctx:
                engine_cli._run(cfg, "team", ["task", "list"])
            run.assert_not_called()
        self.assertIn("доска", ctx.exception.message_ru.lower())
        self.assertIn("не создана", ctx.exception.message_ru)


class PortabilityCheckTest(unittest.TestCase):
    """Обе песочницы: чистая машина и копия живой структуры Hermes."""

    def test_both_sandboxes_pass(self):
        tool = PROJECT_ROOT / "tools" / "portability_check.py"
        self.assertTrue(tool.is_file(), "нет tools/portability_check.py")
        env = {k: v for k, v in os.environ.items() if not k.startswith("TEAMROOM_")}
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([sys.executable, str(tool), "--json"], cwd=str(PROJECT_ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600, env=env)
        self.assertEqual(proc.returncode, 0,
                         "portability_check не прошёл:\n%s\n%s"
                         % (proc.stdout[-2000:], proc.stderr[-2000:]))
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"], [c for c in report["checks"] if not c["ok"]])
        failed = [c for c in report["checks"] if not c["ok"]]
        self.assertEqual(failed, [])
        names = [s["name"] for s in report["scenarios"]]
        self.assertEqual(names, ["A-clean", "B-live", "C-nothing"])
        cases = [c["case"] for c in report["scenarios"][2]["cases"]]
        self.assertEqual(cases, ["C1-с доской", "C2-без доски"])
        # возможности описаны в health, а не выдуманы: dictation none, записи нет, зеркала нет
        for case in report["scenarios"][2]["cases"]:
            self.assertIs(case["features"]["kanban_write"], False)
            self.assertIs(case["features"]["vault_mirror"], False)
            self.assertEqual(case["features"]["dictation"], "none")


if __name__ == "__main__":
    unittest.main()
