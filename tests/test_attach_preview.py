"""Тесты предпросмотра вложений (W29B-Q3): текст, таблица, Office, голос, OCR, пути.

Фикстуры создаются здесь же, нейтральные (химчистка, кофейня), и только внутри
песочницы: настоящая доска и реальные профили не трогаются (`Sandbox`).
"""
from __future__ import annotations

import os
import sys
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import attach_preview as ap          # noqa: E402
from tests.helpers import Sandbox                # noqa: E402

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def make_docx(path: Path, paragraphs: list) -> None:
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % text for text in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="%s"><w:body>%s'
           '</w:body></w:document>' % (W_NS, body))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


def make_xlsx(path: Path, rows: list) -> None:
    def cell(ref, value):
        if isinstance(value, (int, float)):
            return '<c r="%s"><v>%s</v></c>' % (ref, value)
        return ('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, value))

    sheet_rows = []
    for index, row in enumerate(rows, start=1):
        cells = ""
        for col, value in enumerate(row):
            cells += cell("%s%d" % (chr(65 + col), index), value)
        sheet_rows.append('<row r="%d">%s</row>' % (index, cells))
    sheet_xml = ('<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%s</sheetData>'
                 '</worksheet>' % (S_NS, "".join(sheet_rows)))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def make_pptx(path: Path, slides: list) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for index, lines in enumerate(slides, start=1):
            paras = "".join("<a:p><a:r><a:t>%s</a:t></a:r></a:p>" % line for line in lines)
            xml = ('<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/'
                   'presentationml/2006/main" xmlns:a="%s"><a:txBody>%s</a:txBody></p:sld>'
                   % (A_NS, paras))
            zf.writestr("ppt/slides/slide%d.xml" % index, xml)


class AttachPreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._sb = Sandbox()
        cls.sb = cls._sb.__enter__()
        cls.cfg = cls.sb.cfg
        cls.folder = Path(cls.sb.data_dir) / "attach-fixtures"
        cls.folder.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._sb.__exit__(None, None, None)

    def setUp(self) -> None:
        for key in (ap.STT_FAKE_ENV, ap.OCR_FAKE_ENV):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key in (ap.STT_FAKE_ENV, ap.OCR_FAKE_ENV):
            os.environ.pop(key, None)

    def file(self, name: str, data: bytes | str) -> Path:
        path = self.folder / name
        if isinstance(data, str):
            data = data.encode("utf-8")
        path.write_bytes(data)
        return path

    # --- вид и текст -----------------------------------------------------
    def test_kind_of(self) -> None:
        self.assertEqual(ap.kind_of("a.PNG"), "image")
        self.assertEqual(ap.kind_of("a.pdf"), "pdf")
        self.assertEqual(ap.kind_of("a.csv"), "table")
        self.assertEqual(ap.kind_of("a.docx"), "docx")
        self.assertEqual(ap.kind_of("a.ogg"), "audio")
        self.assertEqual(ap.kind_of("a.md"), "text")
        self.assertEqual(ap.kind_of("a.zip"), "other")

    def test_text_preview_utf8_and_cp1251(self) -> None:
        md = self.file("план.md", "# План\n\nПостирать пледы к пятнице.\n")
        out = ap.preview(self.cfg, str(md))
        self.assertEqual(out["kind"], "text")
        self.assertIn("Пледы", out["text"].replace("пледы", "Пледы"))
        self.assertEqual(out["lines"], 3)
        cp = self.folder / "старое.txt"
        cp.write_bytes("Прайс химчистки; сдача в 9:00".encode("cp1251"))
        out2 = ap.preview(self.cfg, str(cp))
        self.assertEqual(out2["encoding"], "cp1251")
        self.assertIn("химчистки", out2["text"])

    def test_csv_table(self) -> None:
        csv_path = self.file("заказы.csv", "услуга;цена\nПлед;400\nПальто;900\n")
        out = ap.preview(self.cfg, str(csv_path))
        self.assertEqual(out["kind"], "table")
        self.assertEqual(out["table"]["delimiter"], ";")
        self.assertEqual(out["table"]["columns"], ["услуга", "цена"])
        self.assertEqual(out["table"]["rows"][1], ["Пальто", "900"])
        self.assertFalse(out["table"]["truncated"])

    def test_txt_window_is_capped(self) -> None:
        big = self.file("лог.txt", "строка\n" * (ap.MAX_TEXT_CHARS // 4))
        out = ap.preview(self.cfg, str(big))
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["text"]), ap.MAX_TEXT_CHARS)

    # --- Office ----------------------------------------------------------
    def test_docx_text(self) -> None:
        path = self.folder / "акт.docx"
        make_docx(path, ["Акт приёмки", "Пальто, 2 шт.", "Срок — пятница"])
        out = ap.preview(self.cfg, str(path))
        self.assertEqual(out["kind"], "docx")
        self.assertEqual(out["paragraphs"], 3)
        self.assertIn("Пальто", out["text"])

    def test_xlsx_table(self) -> None:
        path = self.folder / "смена.xlsx"
        make_xlsx(path, [["день", "выручка"], [1, 2], [2, 3]])
        out = ap.preview(self.cfg, str(path))
        self.assertEqual(out["kind"], "xlsx")
        self.assertEqual(out["table"]["rows"][0], ["1", "2"])
        self.assertEqual(len(out["table"]["rows"]), 2)

    def test_pptx_text(self) -> None:
        path = self.folder / "меню.pptx"
        make_pptx(path, [["Меню на неделю"], ["Понедельник: латте"]])
        out = ap.preview(self.cfg, str(path))
        self.assertEqual(out["kind"], "pptx")
        self.assertIn("Слайд 2", out["text"])
        self.assertIn("латте", out["text"])

    def test_broken_office_is_explained(self) -> None:
        path = self.file("битый.docx", b"not a zip")
        with self.assertRaises(ap.AttachError) as err:
            ap.preview(self.cfg, str(path))
        self.assertEqual(err.exception.code, "bad_office")
        self.assertTrue(err.exception.message.strip())

    # --- картинки, PDF, голос --------------------------------------------
    def test_image_and_pdf_give_url_without_reading(self) -> None:
        png = self.file("чек.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        out = ap.preview(self.cfg, str(png))
        self.assertEqual(out["kind"], "image")
        self.assertTrue(out["url"].startswith("/api/attach/file?path="))
        self.assertIn("ocr", out)
        pdf = self.file("договор.pdf", b"%PDF-1.4 ...")
        out2 = ap.preview(self.cfg, str(pdf))
        self.assertEqual(out2["kind"], "pdf")
        self.assertTrue(out2["url"].startswith("/api/attach/file"))

    def test_audio_transcribe_fake(self) -> None:
        ogg = self.file("голос.ogg", b"OggS" + b"\x00" * 4096)
        os.environ[ap.STT_FAKE_ENV] = "1"
        job = ap.start_transcribe(self.cfg, str(ogg))
        self.assertEqual(job["state"], "running")
        text = ""
        for _ in range(100):
            state = ap.transcribe_job(job["job_id"])
            if state["state"] != "running":
                text = state.get("text", "")
                break
            time.sleep(0.05)
        self.assertEqual(state["state"], "done")
        self.assertEqual(text, ap.FAKE_TRANSCRIPT)
        self.assertEqual(state["progress"], 1.0)

    def test_transcribe_rejects_not_audio(self) -> None:
        txt = self.file("заметка.txt", "просто текст")
        with self.assertRaises(ap.AttachError) as err:
            ap.start_transcribe(self.cfg, str(txt))
        self.assertEqual(err.exception.code, "not_audio")

    def test_transcribe_unknown_job(self) -> None:
        with self.assertRaises(ap.AttachError) as err:
            ap.transcribe_job("deadbeef")
        self.assertEqual(err.exception.code, "job_not_found")

    # --- OCR -------------------------------------------------------------
    def test_ocr_unavailable_without_tesseract(self) -> None:
        png = self.file("скан.png", b"\x89PNG\r\n\x1a\n")
        with mock.patch.object(ap.shutil, "which", return_value=None):
            ap._OCR_CACHE.update({"at": 0.0, "path": None})
            self.assertFalse(ap.capabilities()["ocr"]["available"])
            with self.assertRaises(ap.AttachError) as err:
                ap.run_ocr(png)
        self.assertEqual(err.exception.code, "ocr_unavailable")
        self.assertIn("tesseract", err.exception.message)

    def test_ocr_fake_and_bad_lang(self) -> None:
        png = self.file("скан2.png", b"\x89PNG\r\n\x1a\n")
        text_file = self.file("ocr.txt", "Итого: 1 300 руб.\n")
        os.environ[ap.OCR_FAKE_ENV] = str(text_file)
        out = ap.run_ocr(png, "rus+eng")
        self.assertIn("1 300", out["text"])
        with self.assertRaises(ap.AttachError) as err:
            ap.run_ocr(png, "--rm -rf /")
        self.assertEqual(err.exception.code, "bad_lang")

    # --- безопасность путей ---------------------------------------------
    def test_bad_paths_rejected(self) -> None:
        cases = {
            "": "path_required",
            "C:/нет/такого/файла.txt": "not_found",
            "a\x00b.txt": "bad_path",
            "C:/x/y.txt:stream": "bad_path",
        }
        for raw, code in cases.items():
            with self.assertRaises(ap.AttachError) as ctx:
                ap.resolve_file(self.cfg, raw)
            self.assertEqual(ctx.exception.code, code, raw)

    def test_secret_and_service_dirs_rejected(self) -> None:
        secret = self.file(".env", "TOKEN=1")
        with self.assertRaises(ap.AttachError) as err:
            ap.resolve_file(self.cfg, str(secret))
        self.assertEqual(err.exception.code, "secret")
        key = self.file("ключ.pem", "-----BEGIN-----")
        with self.assertRaises(ap.AttachError) as err2:
            ap.resolve_file(self.cfg, str(key))
        self.assertEqual(err2.exception.code, "secret")
        # Каталог приложения — служебный: предпросмотр не показываем.
        app_file = Path(self.cfg.app_dir) / "docs" / "API.md"
        if app_file.is_file():
            with self.assertRaises(ap.AttachError) as err3:
                ap.resolve_file(self.cfg, str(app_file))
            self.assertEqual(err3.exception.code, "forbidden_dir")

    def test_symlink_escape_not_followed(self) -> None:
        root = self.folder / "root"
        root.mkdir(exist_ok=True)
        outside = self.folder / "снаружи.txt"
        outside.write_text("секрет", encoding="utf-8")
        link = root / "ссылка.txt"
        try:
            if link.exists():
                link.unlink()
            os.symlink(outside, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("символические ссылки недоступны без прав")
        with self.assertRaises(ap.AttachError) as err:
            ap.resolve_file(self.cfg, str(link), roots=[root])
        self.assertEqual(err.exception.code, "symlink_escape")

    def test_size_limit(self) -> None:
        with mock.patch.object(ap, "MAX_PREVIEW_BYTES", 10):
            png = self.file("большой.png", b"\x89PNG" + b"0" * 100)
            out = ap.preview(self.cfg, str(png))
            self.assertEqual(out["unavailable"], "too_big")
            with self.assertRaises(ap.AttachError) as err:
                ap.file_bytes(self.cfg, str(png))
        self.assertEqual(err.exception.code, "too_big")


class AttachRoutesTest(unittest.TestCase):
    """Маршруты подключены к приложению и не дублируют существующие."""

    def test_routes_registered_once(self) -> None:
        from server import app as app_mod
        patterns = [(method, pattern.pattern) for method, pattern, _handler in app_mod.ROUTES]
        self.assertEqual(len(patterns), len(set(patterns)), "дубли маршрутов запрещены")
        for expected in ("/api/attach/preview", "/api/attach/file", "/api/attach/transcribe",
                         "/api/attach/ocr"):
            self.assertIn(expected, [p for _m, p in patterns], expected)
        self.assertTrue(hasattr(app_mod.App, "h_attach_preview"))
        self.assertTrue(hasattr(app_mod.App, "h_attach_ocr"))


if __name__ == "__main__":
    unittest.main()
