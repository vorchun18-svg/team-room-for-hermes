"""Тесты отдачи файлов и «открыть в проводнике» (server/files.py).

Песочница: TEAMROOM_DATA/TEAMROOM_VAULT_DIR — временные каталоги.
Реальные данные не трогаются, explorer не запускается (subprocess мок).
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

from server import config, files  # noqa: E402


class FilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ)
        cls._env.start()

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tr-files-"))
        self.data = self.tmp / "data"
        self.vault = self.tmp / "vault"
        self.outside = self.tmp / "outside"
        for d in (self.data / "uploads", self.vault, self.outside):
            d.mkdir(parents=True, exist_ok=True)
        os.environ["TEAMROOM_DATA"] = str(self.data)
        os.environ["TEAMROOM_VAULT_DIR"] = str(self.vault)
        os.environ.pop("TEAMROOM_EXTRA_ROOTS", None)
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self.files = {}

    def tearDown(self):
        for key, path in list(self.files.items()):
            try:
                Path(path).unlink(missing_ok=True)
            except (OSError, TypeError):
                pass

    # --- помощники ---
    def make(self, name, content=b"hello", base=None):
        p = Path(base or (self.data / "uploads")) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        self.files[name] = p
        return p

    def open(self, path):
        return files.open_file(self.cfg, str(path))

    # --- успешная отдача ---
    def test_text_file_inline(self):
        p = self.make("note.md", "# привет")
        status, headers, body = self.open(p)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(headers["Content-Length"], str(p.stat().st_size))
        self.assertIsInstance(body, Path)
        self.assertEqual(body.read_bytes(), "# привет".encode("utf-8"))

    def test_docx_attachment(self):
        p = self.make("смета.docx", b"PK\x03\x04")
        status, headers, _ = self.open(p)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        self.assertIn("wordprocessingml", headers["Content-Type"])

    def test_xlsx_pptx_attachment(self):
        for name in ("таблица.xlsx", "слайды.pptx"):
            status, headers, _ = self.open(self.make(name, b"PK\x03\x04"))
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Disposition"].startswith("attachment"))

    def test_pdf_and_images_inline(self):
        for name, ctype in (("a.pdf", "application/pdf"), ("b.png", "image/png"),
                            ("c.jpg", "image/jpeg"), ("d.webp", "image/webp"),
                            ("e.gif", "image/gif")):
            status, headers, _ = self.open(self.make(name, b"x"))
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], ctype)
            self.assertTrue(headers["Content-Disposition"].startswith("inline"))

    def test_html_and_svg_are_plain_text(self):
        html = self.make("page.html", "<script>alert(1)</script>")
        svg = self.make("pic.svg", "<svg onload=alert(1)></svg>")
        for p in (html, svg):
            status, headers, _ = self.open(p)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))

    def test_json_and_csv(self):
        status, headers, _ = self.open(self.make("a.json", "{}"))
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        status, headers, _ = self.open(self.make("b.csv", "a,b\n1,2\n"))
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")

    # --- запреты ---
    def test_env_blacklisted(self):
        for name in (".env", ".ENV", "prod.Env"):
            p = self.make(name, "SECRET=1")
            status, headers, body = self.open(p)
            self.assertEqual(status, 403, name)
            self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
            self.assertIn("запрещ", body.decode("utf-8").lower())

    def test_secret_names_blacklisted(self):
        for name in ("auth.json", "google_token.json", "google_credentials.json",
                     "token.txt", "client_secret.json", "session.session", ".env.local"):
            status, _, _ = self.open(self.make(name, "x"))
            self.assertEqual(status, 403, name)

    def test_extension_not_allowed(self):
        for name in ("run.exe", "lib.dll", "data.bin", "script.js", "archive.zip"):
            status, _, body = self.open(self.make(name, b"MZ"))
            self.assertEqual(status, 403, name)
            self.assertIn("запрещ", body.decode("utf-8").lower())

    def test_outside_roots_forbidden(self):
        p = Path(self.outside) / "secret.md"
        p.write_text("тайна", encoding="utf-8")
        status, _, body = self.open(p)
        self.assertEqual(status, 403)
        self.assertIn("разрешённых", body.decode("utf-8"))

    def test_traversal_dotdot(self):
        secret = Path(self.outside) / "secret.md"
        secret.write_text("тайна", encoding="utf-8")
        # абсолютный путь, собранный через .. — всегда 403
        for raw in (str(self.data / "uploads" / ".." / ".." / "outside" / "secret.md"),
                    str(self.data / "uploads" / ".." / ".." / ".." / ".." / "outside" / "secret.md"),
                    str(self.data / "uploads" / ".." / ".." / "outside" / "секрет.md")):
            status, _, _ = files.open_file(self.cfg, raw)
            self.assertEqual(status, 403, raw)
        # относительный ../.. — не 200 ни при каких условиях
        for raw in ("..\\..\\outside\\secret.md", "../outside/secret.md", "..\\..\\..\\secret.md"):
            status, _, _ = files.open_file(self.cfg, raw)
            self.assertIn(status, (403, 404), raw)

    def test_absolute_path_outside(self):
        status, _, body = self.open(Path(self.tmp) / "nope.md")
        self.assertEqual(status, 403)  # не подтверждаем существование
        self.assertIn("разрешённых", body.decode("utf-8"))

    def test_missing_inside_root_is_404(self):
        status, _, body = self.open(self.data / "uploads" / "нет.md")
        self.assertEqual(status, 404)
        self.assertIn("не найден", body.decode("utf-8"))

    def test_no_path_is_400(self):
        for raw in (None, "", "   "):
            status, _, body = files.open_file(self.cfg, raw)
            self.assertEqual(status, 400)
        self.assertIn("путь", files.open_file(self.cfg, None)[2].decode("utf-8").lower())

    def test_ads_suffix_rejected(self):
        p = self.make("note.md", "# x")
        for raw in (str(p) + "::$DATA", str(p) + ":stream", str(p).replace("note.md", "note.md::$DATA")):
            status, _, body = self.open(raw)
            self.assertEqual(status, 403, raw)
            self.assertIn("недопустим", body.decode("utf-8"))

    def test_backslashes_and_case(self):
        p = self.make("note.md", "# x")
        alt = str(p).replace("/", "\\")
        status, headers, _ = files.open_file(self.cfg, alt)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
        # имя в другом регистре на Windows — тот же файл, но чёрный список
        # срабатывает независимо от регистра
        env = self.make(".Env", "x")
        self.assertEqual(self.open(str(env).replace(".Env", ".eNv"))[0], 403)

    def test_xss_filename_is_harmless(self):
        # «» и кавычки в имени на Windows создать нельзя — проверяем заголовок напрямую
        evil = Path('отчёт "><img src=x onerror=alert(1)>.md')
        header = files._disposition(evil, True)
        self.assertIn("inline", header)
        self.assertNotIn("<", header)
        self.assertNotIn(">", header)
        self.assertEqual(header.count('"'), 2)  # только обрамляющие кавычки
        crlf = files._disposition(Path("a\r\nSet-Cookie: x=1.md"), True)
        self.assertNotIn("\r", crlf)
        self.assertNotIn("\n", crlf)
        # имя с юникодом и пробелами отдаётся корректно и не ломает заголовок
        p = self.make("Итоги за октябрь.md", "# x")
        status, headers, _ = self.open(p)
        self.assertEqual(status, 200)
        self.assertIn("filename*=UTF-8''%D0%98", headers["Content-Disposition"])

    def test_symlink_escape(self):
        secret = Path(self.outside) / "secret.md"
        secret.write_text("тайна", encoding="utf-8")
        link = self.data / "uploads" / "link.md"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("симлинки недоступны без прав разработчика")
        status, _, _ = self.open(link)
        self.assertEqual(status, 403)

    def test_size_limit(self):
        p = self.make("big.txt", b"x" * 32)
        with mock.patch.object(files, "MAX_BYTES", 16):
            status, _, body = self.open(p)
        self.assertEqual(status, 413)
        self.assertIn("50 МБ", body.decode("utf-8"))

    # --- проводник ---
    def test_reveal_allowed_path(self):
        p = self.make("note.md", "# x")
        with mock.patch.object(files.subprocess, "Popen") as popen:
            self.assertTrue(files.reveal(self.cfg, str(p)))
        popen.assert_called_once()
        args = popen.call_args[0][0]
        self.assertEqual(args[0], "explorer")
        self.assertTrue(args[1].startswith("/select,"))
        self.assertTrue(args[1].endswith(str(p.resolve())))
        self.assertTrue(popen.call_args[1].get("close_fds"))

    def test_reveal_forbidden_path(self):
        p = Path(self.outside) / "secret.md"
        p.write_text("x", encoding="utf-8")
        with mock.patch.object(files.subprocess, "Popen") as popen:
            self.assertFalse(files.reveal(self.cfg, str(p)))
            self.assertFalse(files.reveal(self.cfg, str(p) + "::$DATA"))
            self.assertFalse(files.reveal(self.cfg, ""))
        popen.assert_not_called()

    def test_reveal_swallows_popen_errors(self):
        p = self.make("note.md", "# x")
        with mock.patch.object(files.subprocess, "Popen", side_effect=OSError("boom")):
            self.assertFalse(files.reveal(self.cfg, str(p)))

    # --- вспомогательное ---
    def test_resolve_file_alias(self):
        p = self.make("note.md", "# x")
        self.assertEqual(files.resolve_file(self.cfg, str(p)), p.resolve())
        self.assertIsNone(files.resolve_file(self.cfg, str(p) + "::$DATA"))

    def test_info(self):
        p = self.make("note.md", "# x")
        got = files.info(self.cfg, str(p))
        self.assertTrue(got["ok"])
        self.assertEqual(got["ext"], ".md")
        self.assertEqual(got["size"], 3)
        self.assertFalse(files.info(self.cfg, str(Path(self.outside) / "x.md"))["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
