"""Тесты папки проекта комнаты (Д2 WAVE8-RULES): валидация путей, создание, README.

Железное правило: ни одна проверка не создаёт папок вне временного каталога
песочницы. `C:\\TeamRoom\\Projects` на реальном диске не создаётся — тесты работают
только с путями внутри `tempfile.TemporaryDirectory`.
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, project_dir, safety  # noqa: E402

B = chr(92)                      # обратная косая черта, без экранирования в литералах
HERMES_HOME = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes"


class Base(unittest.TestCase):
    """Песочница: data/vault/hermes — во временном каталоге, всё остальное запрещено."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-pdir-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault" / "rooms"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
            "TEAMROOM_NO_SHELL": "1",
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self._before = sorted(str(p) for p in self.tmp.rglob("*"))

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def new_path(self, *parts) -> str:
        """Путь внутри песочницы (создаётся только тем, что мы сами вызвали)."""
        return str(self.tmp.joinpath(*parts))

    def assert_nothing_new(self):
        after = sorted(str(p) for p in self.tmp.rglob("*"))
        self.assertEqual(after, self._before, "появились файлы вне ожидаемых")


class TestReject(Base):
    """Что нельзя: traversal, UNC, зарезервированные имена, системные корни, Hermes, data/."""

    def check(self, path, fragment):
        result = project_dir.check_dir(self.cfg, path)
        self.assertFalse(result["allowed"], "%r должен быть запрещён" % (path,))
        self.assertIn(fragment, result["reason"] or "")
        self.assertEqual(result["warnings"], [])
        return result

    def test_empty_and_relative(self):
        self.check("", "Пустой путь")
        self.check("   ", "Пустой путь")
        self.check("Projects" + B + "Мой проект", "полный путь")

    def test_traversal(self):
        self.check("C:" + B + "Hermes" + B + "Projects" + B + ".." + B + ".." + B + "Windows",
                   "«..»")
        self.check("C:" + B + "Hermes" + B + "Projects" + B + "a" + B + ".." + B + "b", "«..»")
        self.check("C:" + B + "Hermes" + B + "Projects" + B + "." + B + "x", "«..»")

    def test_unc_and_device_paths(self):
        self.check(B + B + "server" + B + "share" + B + "проект", "Сетевая папка")
        self.check(B + B + "?" + B + "C:" + B + "проект", "не поддерживаются")
        self.check(B + B + "." + B + "C:" + B + "проект", "не поддерживаются")
        self.check("//server/share", "Сетевая папка")

    def test_reserved_names(self):
        for name in ("con", "PRN", "aux", "nul", "com1", "LPT9", "con.txt", "nul.log"):
            self.check("C:" + B + "Hermes" + B + "Projects" + B + name, "зарезервировано")

    def test_system_roots(self):
        self.check("C:" + B + "Windows" + B + "System32", "Системный каталог")
        self.check("C:" + B + "Windows", "Системный каталог")
        self.check("C:" + B + "Program Files" + B + "App", "Системный каталог")
        self.check("C:" + B + "Program Files (x86)" + B + "App", "Системный каталог")
        self.check("C:" + B + "ProgramData" + B + "x", "Системный каталог")

    def test_drive_root(self):
        self.check("C:" + B, "весь диск")
        self.check("C:/", "весь диск")

    def test_hermes_home_anything_inside(self):
        for path in (str(HERMES_HOME), str(HERMES_HOME / "projects" / "мой"),
                     str(HERMES_HOME / "cache" / "scratch" / "x")):
            result = self.check(path, "служебных каталогов Hermes")
            self.assertEqual(result["reason"], "Папка внутри служебных каталогов Hermes — выберите другую")

    def test_app_data_and_program_dirs(self):
        self.check(str(Path(self.cfg.data_dir) / "projects" / "x"), "данных приложения")
        self.check(str(Path(self.cfg.app_dir) / "server" / "x"), "программы")
        self.check(str(Path(self.cfg.app_dir) / "web" / "x"), "программы")
        self.check(str(Path(self.cfg.app_dir) / "data" / "x"), "программы")

    def test_bad_characters_and_long_path(self):
        self.check("C:" + B + "Hermes" + B + "Projects" + B + "a<b", "запрещённые в Windows")
        self.check("C:" + B + "Hermes" + B + "Projects" + B + "a|b", "запрещённые в Windows")
        self.check("C:" + B + "Hermes" + B + "Projects" + B + "x" + B + ("д" * 200),
                   "Слишком длинный")
        self.check("C:" + B + "Hermes" + B + "Projects" + B + "хвост.", "точкой или пробелом")

    def test_nothing_created_by_checks(self):
        for path in ("C:" + B + "Windows" + B + "no", "C:" + B + "Hermes" + B + "Projects" + B + "нет",
                     self.new_path("projects", "новый")):
            project_dir.check_dir(self.cfg, path)
        self.assertFalse((self.tmp / "projects" / "новый").exists())
        self.assertFalse(Path("C:" + B + "Hermes" + B + "Projects" + B + "нет").exists())


class TestAllow(Base):
    """Разрешённые пути внутри песочницы: exists / will_create / writable."""

    def test_existing_dir_is_allowed(self):
        target = self.tmp / "проекты" / "Мой проект"
        target.mkdir(parents=True)
        result = project_dir.check_dir(self.cfg, str(target))
        self.assertTrue(result["allowed"])
        self.assertTrue(result["exists"])
        self.assertFalse(result["will_create"])
        self.assertTrue(result["writable"])
        self.assertEqual(Path(result["normalized"]), target)

    def test_missing_dir_will_be_created(self):
        result = project_dir.check_dir(self.cfg, self.new_path("проекты", "Новый"))
        self.assertTrue(result["allowed"])
        self.assertFalse(result["exists"])
        self.assertTrue(result["will_create"])
        self.assertTrue(result["writable"])
        self.assertFalse((self.tmp / "проекты").exists(), "check_dir не должен создавать папки")

    def test_normalization(self):
        result = project_dir.check_dir(self.cfg, "  " + self.new_path("a", "b") + B + "  ")
        self.assertTrue(result["allowed"])
        self.assertTrue(result["normalized"].endswith(B + "b"))
        self.assertNotIn("\\\\", result["normalized"])          # двойных разделителей нет

    def test_vault_is_allowed_with_warning(self):
        result = project_dir.check_dir(self.cfg, str(Path(self.cfg.vault_dir) / "cafe" / "Материалы"))
        self.assertTrue(result["allowed"])
        self.assertTrue(result["warnings"], "внутри Vault должно быть предупреждение")
        self.assertIn("Vault", result["warnings"][0])

    def test_file_instead_of_dir(self):
        target = self.tmp / "файл.txt"
        target.write_text("x", encoding="utf-8")
        result = project_dir.check_dir(self.cfg, str(target))
        self.assertFalse(result["allowed"])
        self.assertIn("файл", result["reason"])


class TestEnsureDir(Base):
    def test_creates_only_allowed_path(self):
        target = self.tmp / "проекты" / "Мой проект"
        made = project_dir.ensure_dir(self.cfg, str(target))
        self.assertEqual(Path(made), target)
        self.assertTrue(target.is_dir())
        self.assertTrue(target.parent.is_dir())

    def test_refuses_forbidden(self):
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        with self.assertRaises(project_dir.DirError) as ctx:
            project_dir.ensure_dir(self.cfg, str(Path(self.cfg.data_dir) / "projects"))
        self.assertIn("данных приложения", str(ctx.exception))
        with self.assertRaises(project_dir.DirError):
            project_dir.ensure_dir(self.cfg, "C:" + B + "Windows" + B + "no-way")
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*")), before)

    def test_traversal_cannot_escape(self):
        with self.assertRaises(project_dir.DirError):
            project_dir.ensure_dir(self.cfg, self.new_path("a") + B + ".." + B + ".." + B + "z")
        self.assertFalse((self.tmp.parent / "z").exists())

    def test_open_dir_never_spawns_without_flag(self):
        target = self.tmp / "проекты"
        target.mkdir()
        self.assertFalse(project_dir.open_dir(self.cfg, str(target)))   # TEAMROOM_NO_SHELL=1
        self.assertFalse(project_dir.open_dir(self.cfg, "C:" + B + "Windows"))


class TestOpenDirReport(Base):
    """W10: отчёт открытия папки — подменяем запуск и вывод окна вперёд.

    Реальный проводник в тестах не запускается никогда (`TEAMROOM_NO_SHELL=1`
    снимаем только вокруг подмененных вызовов).
    """

    def _open(self, path, create=False):
        from unittest import mock
        calls = []
        with mock.patch.dict(os.environ, {"TEAMROOM_NO_SHELL": "0"}), \
                mock.patch.object(project_dir, "_launch_explorer",
                                  lambda target: calls.append(target) or True), \
                mock.patch.object(project_dir, "_raise_explorer",
                                  lambda name, wait_s=2.5: {"found": True, "foreground": True}):
            report = project_dir.open_dir_report(self.cfg, path, create=create)
        return report, calls

    def test_missing_folder_offers_to_create_and_creates_nothing(self):
        target = self.new_path("проекты", "Пока нет")
        report, calls = self._open(target)
        self.assertFalse(report["ok"])
        self.assertTrue(report["can_create"])
        self.assertIn("Папки нет", report["reason"])
        self.assertEqual(calls, [], "проводник не запускаем для несуществующей папки")
        self.assertFalse(Path(target).exists(), "check_dir/open_dir_report ничего не создают")

    def test_create_then_open_makes_folder_and_reports_foreground(self):
        target = self.new_path("проекты", "Новая")
        report, calls = self._open(target, create=True)
        self.assertTrue(report["ok"], report)
        self.assertTrue(Path(target).is_dir())
        self.assertEqual(calls, [str(Path(report["path"]))], "запуск ровно один, путь нормализован")
        self.assertTrue(report["foreground"])

    def test_forbidden_path_is_refused_before_any_launch(self):
        report, calls = self._open("C:" + B + "Windows")
        self.assertFalse(report["ok"])
        self.assertEqual(calls, [])
        self.assertIn("Системный каталог", report["reason"])

    def test_injection_in_path_never_reaches_launcher(self):
        # кавычки и спецсимволы: путь отбрасывает check_dir, оболочке ничего не уходит
        for bad in ('C:' + B + 'tmp' + B + 'a" & calc', 'C:' + B + 'tmp' + B + 'a|b'):
            report, calls = self._open(bad)
            self.assertFalse(report["ok"], bad)
            self.assertEqual(calls, [], bad)
        self.assertNotIn("shell=True", inspect.getsource(project_dir._launch_explorer))

    def test_shell_flag_blocks_everything(self):
        target = self.tmp / "проекты"
        target.mkdir()
        report = project_dir.open_dir_report(self.cfg, str(target))     # NO_SHELL=1 из Base
        self.assertFalse(report["ok"])
        self.assertIn("тестовом режиме", report["reason"])

    def test_launch_failure_has_human_message(self):
        from unittest import mock
        target = self.new_path("проекты")
        Path(target).mkdir()
        with mock.patch.dict(os.environ, {"TEAMROOM_NO_SHELL": "0"}), \
                mock.patch.object(project_dir, "_launch_explorer", lambda _p: False):
            report = project_dir.open_dir_report(self.cfg, target)
        self.assertFalse(report["ok"])
        self.assertTrue(report["reason"].startswith("Не удалось открыть папку:"), report["reason"])

    def test_window_not_raised_tells_about_taskbar(self):
        from unittest import mock
        target = self.new_path("проекты")
        Path(target).mkdir()
        with mock.patch.dict(os.environ, {"TEAMROOM_NO_SHELL": "0"}), \
                mock.patch.object(project_dir, "_launch_explorer", lambda _p: True), \
                mock.patch.object(project_dir, "_raise_explorer",
                                  lambda name, wait_s=2.5: {"found": True, "foreground": False}):
            report = project_dir.open_dir_report(self.cfg, target)
        self.assertTrue(report["ok"], "папка открылась — это не ошибка")
        self.assertFalse(report["foreground"])
        self.assertIn("панели задач", report["reason"])

    def test_open_dir_bool_wraps_report(self):
        from unittest import mock
        target = self.new_path("проекты")
        Path(target).mkdir()
        with mock.patch.dict(os.environ, {"TEAMROOM_NO_SHELL": "0"}), \
                mock.patch.object(project_dir, "_launch_explorer", lambda _p: True), \
                mock.patch.object(project_dir, "_raise_explorer",
                                  lambda name, wait_s=2.5: {"found": True, "foreground": True}):
            self.assertTrue(project_dir.open_dir(self.cfg, target))
        self.assertEqual(project_dir.open_dir(self.cfg, target), False)   # NO_SHELL вернулся

    def test_find_window_is_safe_without_windows(self):
        # на не-Windows и с пустым именем — 0, без исключений и без ctypes-падений
        self.assertEqual(project_dir._find_explorer_window(""), 0)
        self.assertIn("CabinetWClass", project_dir.EXPLORER_CLASSES)


class TestNames(Base):
    def test_request_subdir_format(self):
        name = project_dir.request_subdir("Отчёт по поставщикам", 1793059000)
        self.assertRegex(name, r"^\d{4}-\d{2}-\d{2} Отчёт по поставщикам$")
        import time
        self.assertTrue(name.startswith(time.strftime("%Y-%m-%d", time.localtime(1793059000))))

    def test_request_subdir_safe_for_windows(self):
        name = project_dir.request_subdir('Отчёт: "касса" / смета? *', 1793059000)
        for bad in '<>:"/\\|?*':
            # дефис вместо разделителя пути допустим, остальные символы запрещены
            if bad in ("/", "\\"):
                self.assertNotIn(bad, name.split(" ", 1)[1])
            else:
                self.assertNotIn(bad, name)
        self.assertEqual(project_dir.request_subdir("", 1793059000).split(" ", 1)[1], "Запрос")
        self.assertEqual(project_dir.request_subdir("con", 1793059000).split(" ", 1)[1][:6], "Запрос")
        self.assertFalse(project_dir.request_subdir("очень длинное " * 30, 1793059000)
                         .split(" ", 1)[1].endswith(" "))


class TestUpdateIndex(Base):
    def room(self, path):
        return {"id": "test", "title": "Лето", "project_dir": path}

    def test_creates_readme_with_markers(self):
        target = self.tmp / "проекты"
        target.mkdir()
        (target / "2026-10-28 Отчёт").mkdir()
        (target / "2026-10-28 Отчёт" / "смета.xlsx").write_text("x", encoding="utf-8")
        self.assertTrue(project_dir.update_index(self.cfg, self.room(str(target))))
        text = (target / project_dir.INDEX_FILE).read_text(encoding="utf-8")
        self.assertIn(project_dir.BEGIN_INDEX, text)
        self.assertIn(project_dir.END_INDEX, text)
        self.assertIn("## Содержание", text)
        self.assertIn("2026-10-28 Отчёт — 1 файл", text)
        self.assertIn("Лето", text)
        # повторный вызов без изменений — False (файл не переписывается)
        self.assertFalse(project_dir.update_index(self.cfg, self.room(str(target))))

    def test_manual_text_survives(self):
        target = self.tmp / "проекты"
        target.mkdir()
        readme = target / project_dir.INDEX_FILE
        readme.write_text("# Мои заметки\n\nНе трогать этот текст.\n", encoding="utf-8")
        self.assertTrue(project_dir.update_index(self.cfg, self.room(str(target))))
        (target / "2026-10-29 Второй").mkdir()
        self.assertTrue(project_dir.update_index(self.cfg, self.room(str(target))))
        text = readme.read_text(encoding="utf-8")
        self.assertIn("# Мои заметки", text)
        self.assertIn("Не трогать этот текст.", text)
        self.assertEqual(text.count(project_dir.BEGIN_INDEX), 1)
        self.assertEqual(text.count("## Содержание"), 1)
        self.assertIn("2026-10-29 Второй", text)

    def test_manual_text_between_markers_is_replaced_only_inside(self):
        target = self.tmp / "проекты"
        target.mkdir()
        readme = target / project_dir.INDEX_FILE
        readme.write_text("Сверху\n\n" + project_dir.BEGIN_INDEX + "\nстарое\n"
                          + project_dir.END_INDEX + "\n\nСнизу\n", encoding="utf-8")
        self.assertTrue(project_dir.update_index(self.cfg, self.room(str(target))))
        text = readme.read_text(encoding="utf-8")
        self.assertIn("Сверху", text)
        self.assertIn("Снизу", text)
        self.assertNotIn("старое", text)
        self.assertIn("## Содержание", text)

    def test_empty_or_missing_folder(self):
        self.assertFalse(project_dir.update_index(self.cfg, self.room("")))
        self.assertFalse(project_dir.update_index(self.cfg, self.room(str(self.tmp / "нет" / "такой"))))
        self.assertFalse((self.tmp / "нет").exists())

    def test_real_vault_is_never_written(self):
        """Реальный Vault — только чтение (§1 WAVE7): «Содержание» там не пишется."""
        fake = self.tmp / "vault"
        fake.mkdir()
        with mock.patch.object(project_dir.safety, "is_under_real", lambda path: True):
            self.assertFalse(project_dir.update_index(self.cfg, self.room(str(fake))))
        self.assertFalse((fake / project_dir.INDEX_FILE).exists())


if __name__ == "__main__":
    unittest.main()
