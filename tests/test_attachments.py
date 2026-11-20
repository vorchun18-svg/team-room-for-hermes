"""Тесты вложений человека (W28-A, дефект 4).

Что проверяем и почему (это красный тест на 1.4.0):

* файл из «Загрузок» и из произвольной папки **прикрепляется** — раньше
  `config.path_allowed` отвечал 403 «вложение вне разрешённых путей», и человек
  копировал путь в текст запроса;
* `.zip`, `.doc`, `.dwg` прикрепляются — раньше белый список `ALLOWED_EXT`
  запрещал их вовсе;
* кириллица и пробелы в имени — норма;
* 12 файлов (и предел 30) — норма; 31 — понятная ошибка с действием;
* `.env` — не запрет, а вопрос: `needs_confirm` → «Всё равно прикрепить»
  (`confirm_secret`) → принято;
* несуществующий файл и папка вместо файла — понятная причина, а не «вне путей»;
* диалог выбора подменяется `TEAMROOM_PICK_FAKE` (песочница/тесты), на не-Windows
  честно отвечает «вставьте путь в поле»;
* «Открыть»/«Открыть в папке» у чипа проверяют только существование файла и НЕ
  запускают ничего в тестах (подмена `proc`).

Реальные данные, живой 8777 и рабочий стол не трогаются: всё в tempfile.

    python -m unittest tests.test_attachments -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import attachments                                   # noqa: E402
from server import routes_files_pick as pick_routes               # noqa: E402
from server.http_util import ApiError                             # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tr-attach-"))
        # «Загрузки» — папка, которой нет ни в одном белом списке корней.
        self.downloads = self.tmp / "Загрузки"
        self.project = self.tmp / "Проект Альфа" / "исходники"
        for folder in (self.downloads, self.project):
            folder.mkdir(parents=True, exist_ok=True)
        self._cleanup = []

    def tearDown(self):
        for path in self._cleanup:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name, content=b"data", folder=None):
        path = Path(folder or self.downloads) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        self._cleanup.append(path)
        return path


class CheckTest(Base):
    """«Файл есть, это файл, читается» — и ничего больше."""

    def test_downloads_folder_is_accepted(self):
        path = self.make("смета.xlsx", b"PK")
        info = attachments.check(path)
        self.assertEqual(info["name"], "смета.xlsx")
        self.assertEqual(info["size"], 2)
        self.assertEqual(info["path"], str(path.resolve()))
        self.assertFalse(info["secret"])

    def test_arbitrary_folder_is_accepted(self):
        path = self.make("отчёт.md", folder=self.project)
        info = attachments.check(str(path))
        self.assertEqual(Path(info["path"]), path.resolve())

    def test_archive_and_legacy_office_are_accepted(self):
        # Раньше эти расширения не входили в ALLOWED_EXT и прикрепить их было нельзя.
        for name in ("архив.zip", "договор.doc", "план.dwg", "данные.xls", "видео.mp4"):
            with self.subTest(name=name):
                info = attachments.check(self.make(name))
                self.assertTrue(info["path"].endswith(name))

    def test_cyrillic_and_spaces(self):
        path = self.make("Смета Альфа — версия 2 (итог).docx")
        info = attachments.check(path)
        self.assertEqual(info["name"], "Смета Альфа — версия 2 (итог).docx")

    def test_twelve_files(self):
        paths = [self.make("файл %02d.txt" % i) for i in range(12)]
        checked = attachments.check_all(paths)
        self.assertEqual(len(checked), 12)
        self.assertEqual(checked[0], str(paths[0].resolve()))

    def test_limit_is_thirty(self):
        self.assertEqual(attachments.MAX_ATTACH, 30)
        paths = []
        for i in range(31):
            path = self.tmp / ("many-%02d.txt" % i)
            path.write_text("x", encoding="utf-8")
            paths.append(path)
        with self.assertRaises(attachments.AttachmentError) as ctx:
            attachments.check_all(paths)
        self.assertEqual(ctx.exception.code, "too_many")
        self.assertIn("не больше 30", ctx.exception.message)
        self.assertEqual(len(attachments.check_all(paths[:30])), 30)

    def test_missing_file_says_why(self):
        ghost = self.tmp / "нет-такого-файла.pdf"
        with self.assertRaises(attachments.AttachmentError) as ctx:
            attachments.check(ghost)
        exc = ctx.exception
        self.assertEqual(exc.code, "not_found")
        self.assertEqual(exc.status, 400)
        self.assertIn("файла нет", exc.message)
        self.assertIn("выберите файл заново", exc.message.lower())
        self.assertEqual(exc.detail["reason"], "not_found")
        self.assertIn(str(ghost), exc.detail["path"])

    def test_folder_is_not_a_file(self):
        folder = self.downloads / "папка"
        folder.mkdir()
        with self.assertRaises(attachments.AttachmentError) as ctx:
            attachments.check(folder)
        self.assertEqual(ctx.exception.code, "not_file")
        self.assertIn("это папка", ctx.exception.message)

    def test_empty_path(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(attachments.AttachmentError) as ctx:
                    attachments.check(value)
                self.assertEqual(ctx.exception.code, "empty_path")

    def test_duplicates_are_dropped(self):
        path = self.make("дубль.txt")
        checked = attachments.check_all([str(path)] * 3)
        self.assertEqual(len(checked), 1)


class SecretTest(Base):
    """Секреты — вопрос, а не запрет."""

    def test_env_requires_confirmation(self):
        path = self.make(".env", "API_KEY=1")
        with self.assertRaises(attachments.AttachmentError) as ctx:
            attachments.check(path)
        exc = ctx.exception
        self.assertEqual(exc.code, "needs_confirm")
        self.assertEqual(exc.status, 409)
        self.assertIn("Всё равно прикрепить", exc.message)

    def test_env_accepted_after_confirm(self):
        path = self.make(".env", "API_KEY=1")
        info = attachments.check(path, confirm_secret=True)
        self.assertTrue(info["secret"])
        self.assertTrue(info["path"].endswith(".env"))

    def test_common_secret_names(self):
        for name in ("google_token.json", "id_rsa", "server.key", "credentials.json",
                     ".env.local", "client_secret.txt"):
            with self.subTest(name=name):
                self.assertTrue(attachments.is_secret(name), name)
        for name in ("смета.xlsx", "отчёт.md", "план.dwg", "архив.zip"):
            with self.subTest(name=name):
                self.assertFalse(attachments.is_secret(name), name)

    def test_check_all_with_secret_needs_flag(self):
        env = self.make(".env", "A=1")
        plain = self.make("отчёт.md", "# привет")
        with self.assertRaises(attachments.AttachmentError):
            attachments.check_all([str(plain), str(env)])
        both = attachments.check_all([str(plain), str(env)], True)
        self.assertEqual(len(both), 2)


class PickTest(Base):
    """Окно выбора файлов: подмена в песочнице и честный отказ на не-Windows."""

    def test_parse_fake_lines_and_json(self):
        self.assertEqual(pick_routes.parse_fake("C:/a/b.txt\nC:/c d/e.txt"),
                         ["C:/a/b.txt", "C:/c d/e.txt"])
        self.assertEqual(pick_routes.parse_fake('["C:/x/1.zip", "C:/x/2.doc"]'),
                         ["C:/x/1.zip", "C:/x/2.doc"])
        self.assertEqual(pick_routes.parse_fake(""), [])
        self.assertEqual(pick_routes.parse_fake(None), [])

    def test_fake_env_returns_paths(self):
        one = self.make("архив.zip")
        two = self.make("договор.doc")
        fake = "%s\n%s" % (one, two)
        with mock.patch.dict(os.environ, {pick_routes.FAKE_ENV: fake}):
            self.assertEqual(pick_routes.pick_files("Прикрепить файлы"),
                             [str(one), str(two)])

    def test_empty_fake_env_means_cancelled(self):
        with mock.patch.dict(os.environ, {pick_routes.FAKE_ENV: ""}):
            self.assertEqual(pick_routes.pick_files(), [])

    def test_non_windows_falls_back_to_path_field(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(pick_routes.FAKE_ENV, None)
            with mock.patch.object(pick_routes.proc, "IS_WINDOWS", False):
                with self.assertRaises(pick_routes.PickError) as ctx:
                    pick_routes.pick_files()
        self.assertEqual(ctx.exception.code, "pick_unavailable")
        self.assertEqual(ctx.exception.status, 501)
        self.assertIn("поле", ctx.exception.message.lower())

    def test_fake_never_runs_powershell(self):
        """В песочнице не должно быть ни одного запуска процесса."""
        with mock.patch.dict(os.environ, {pick_routes.FAKE_ENV: "C:/a.txt"}):
            with mock.patch.object(pick_routes.proc, "run") as run:
                paths = pick_routes.pick_files()
        run.assert_not_called()
        self.assertEqual(paths, ["C:/a.txt"])


class Stub(pick_routes.PickRoutes):
    """Экземпляр App без сервера: хендлерам нужен только cfg."""

    def __init__(self, tmp):
        self.cfg = mock.Mock(tmp_dir=tmp)
        self.log = mock.Mock()


class RoutesTest(Base):
    def test_pick_route_with_fake(self):
        one = self.make("Смета Альфа.docx")
        with mock.patch.dict(os.environ, {pick_routes.FAKE_ENV: str(one)}):
            status, payload, _ = Stub(self.tmp).h_files_pick({}, {"title": "Прикрепить файлы"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["paths"], [str(one)])
        self.assertFalse(payload["cancelled"])
        self.assertEqual(payload["max"], 30)

    def test_pick_route_reports_cancel(self):
        with mock.patch.dict(os.environ, {pick_routes.FAKE_ENV: ""}):
            status, payload, _ = Stub(self.tmp).h_files_pick({}, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["paths"], [])
        self.assertTrue(payload["cancelled"])

    def test_pick_route_unavailable_is_501_with_action(self):
        os.environ.pop(pick_routes.FAKE_ENV, None)
        with mock.patch.object(pick_routes.proc, "IS_WINDOWS", False):
            with self.assertRaises(ApiError) as ctx:
                Stub(self.tmp).h_files_pick({}, {})
        self.assertEqual(ctx.exception.status, 501)
        self.assertIn("поле", ctx.exception.message)

    def test_attach_check_items(self):
        good = self.make("архив.zip")
        env = self.make(".env", "A=1")
        ghost = self.tmp / "нет.pdf"
        stub = Stub(self.tmp)
        status, payload, _ = stub.h_files_attach_check(
            {}, {"attach": [str(good), str(env), str(ghost)]})
        self.assertEqual(status, 200)
        codes = [item["code"] for item in payload["items"]]
        self.assertEqual(codes, ["ok", "needs_confirm", "not_found"])
        self.assertTrue(payload["items"][0]["ok"])
        self.assertEqual(payload["items"][0]["size"], 4)
        self.assertEqual(payload["needs_confirm"], [str(env)])
        self.assertEqual(payload["accepted"], [str(good.resolve())])
        self.assertIn("Всё равно прикрепить", payload["items"][1]["message"])

    def test_attach_check_confirm_accepts_secret(self):
        env = self.make(".env", "A=1")
        status, payload, _ = Stub(self.tmp).h_files_attach_check(
            {}, {"attach": [str(env)], "confirm_secret": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["needs_confirm"], [])
        self.assertTrue(payload["items"][0]["ok"])

    def test_attach_check_stops_above_limit(self):
        with self.assertRaises(ApiError) as ctx:
            Stub(self.tmp).h_files_attach_check({}, {"attach": ["x"] * 31})
        self.assertEqual(ctx.exception.status, 400)

    def test_attach_check_rejects_bad_body(self):
        with self.assertRaises(ApiError) as ctx:
            Stub(self.tmp).h_files_attach_check({}, {"attach": "C:/a.md"})
        self.assertEqual(ctx.exception.status, 400)

    def test_attach_open_reveal_uses_proc_not_shell(self):
        path = self.make("доклад.pdf")
        with mock.patch.object(pick_routes.proc, "IS_WINDOWS", False), \
                mock.patch.object(pick_routes.proc, "open_path", return_value=True) as opener:
            status, payload, _ = Stub(self.tmp).h_files_attach_open(
                {}, {"path": str(path), "mode": "reveal"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["revealed"])
        opener.assert_called_once()

    def test_attach_open_missing_file(self):
        with self.assertRaises(ApiError) as ctx:
            Stub(self.tmp).h_files_attach_open({}, {"path": str(self.tmp / "нет.txt")})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("файла нет", ctx.exception.message)

    def test_attach_open_executable_is_revealed_not_launched(self):
        path = self.make("setup.exe", b"MZ")
        with mock.patch.object(pick_routes.proc, "IS_WINDOWS", False), \
                mock.patch.object(pick_routes.proc, "open_path", return_value=True) as opener:
            status, payload, _ = Stub(self.tmp).h_files_attach_open(
                {}, {"path": str(path), "mode": "open"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "reveal")
        self.assertIn("не запускаю", payload["note"])
        # открывали ПАПКУ, а не сам файл
        self.assertEqual(Path(opener.call_args.args[0]), path.resolve().parent)


class ContractTest(unittest.TestCase):
    """Стык «интерфейс ↔ сервер»: имена ручек и полей существуют с обеих сторон."""

    def setUp(self):
        (ROOT / "web" / "js" / "views" / "composer.js").read_text(encoding="utf-8")

    def test_routes_registered_in_app(self):
        from server import app as app_mod
        routes = {(method, pattern.pattern) for method, pattern, _handler in app_mod.ROUTES}
        for method, pattern, _handler in pick_routes.ROUTES:
            with self.subTest(pattern=pattern):
                self.assertIn((method, pattern), routes)
        self.assertTrue(hasattr(app_mod.App, "h_files_pick"))
        self.assertTrue(hasattr(app_mod.App, "h_files_attach_check"))
        self.assertTrue(hasattr(app_mod.App, "h_files_attach_open"))

    def test_ui_and_server_agree_on_fields(self):
        js = (ROOT / "web" / "js" / "views" / "composer.js").read_text(encoding="utf-8")
        # Клиент зовёт ровно те ручки, что объявлены здесь.
        for _method, pattern, _handler in pick_routes.ROUTES:
            url = pattern.replace(r"\\.", ".")
            self.assertIn(url, js, "интерфейс не зовёт " + url)
        for field in ("confirm_secret", "attach", "paths", "mode", "cancelled"):
            self.assertIn(field, js, "поле %s не читается/не шлётся интерфейсом" % field)
        self.assertIn("MAX_ATTACH = 30", js, "предел вложений в интерфейсе — 30")

    def test_allow_list_still_used_for_showing_files(self):
        """Белый список остался там, где нужен: показ файлов в браузере."""
        from server import config
        self.assertTrue(callable(config.path_allowed))
        source = (ROOT / "server" / "routes_read.py").read_text(encoding="utf-8")
        self.assertIn("path_allowed", source)


if __name__ == "__main__":
    unittest.main()
