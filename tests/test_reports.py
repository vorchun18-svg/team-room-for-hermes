"""Тесты отчётов (server/reports.py): индекс, валидация, HTML, статика, ../."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, reports  # noqa: E402
from server.http_util import ApiError  # noqa: E402

GOOD_SPEC = {
    "version": 1,
    "title": "Юнит-экономика моти",
    "subtitle": "комната Лето · 28.10.2026",
    "summary": "Маржа 48,8 % при цене 220 ₽.",
    "blocks": [
        {"type": "stats", "items": [{"label": "Маржа", "value": "48,8 %"}]},
        {"type": "text", "md": "**Вывод.** Цена 220 ₽ даёт 48,8 % маржи."},
        {"type": "table", "columns": [{"key": "price", "label": "Цена", "type": "money"}],
         "rows": [{"price": 190}, {"price": 220}]},
    ],
}


class ReportsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="tr-reports-"))
        cls._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(cls.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(cls.tmp / "vault"),
        })
        cls._env.start()
        cls.cfg = config.load()
        cls.cfg.ensure_dirs()

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def setUp(self):
        root = Path(self.cfg.data_dir) / "reports"
        if root.exists():
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        self.room = "cafe"
        self.base = root / self.room
        self.slug = "r-20261028-0931-ab12"
        self.dir = self.base / self.slug
        self.dir.mkdir(parents=True)
        self._ingest = mock.patch.object(reports, "_ingest_state", return_value={})
        self._registry = mock.patch.object(
            reports, "_request_id_for_slug",
            side_effect=lambda cfg, room, slug: "t_a1b2c3d4" if slug == self.slug else None)
        self._ingest.start()
        self._registry.start()

    def tearDown(self):
        self._ingest.stop()
        self._registry.stop()

    def write_spec(self, data, name="report.json", base=None):
        p = Path(base or self.dir) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def write_html(self, html="<html><head><title>Свой отчёт</title></head><body>x</body></html>",
                   base=None, name="report.html"):
        p = Path(base or self.dir) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        return p

    # --- индекс ---
    def test_list_reports_spec(self):
        self.write_spec(GOOD_SPEC)
        items = reports.list_reports(self.cfg, self.room)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["key"], self.slug)
        self.assertEqual(item["kind"], "spec")
        self.assertEqual(item["title"], "Юнит-экономика моти")
        self.assertEqual(item["blocks"], 3)
        self.assertEqual(item["request_id"], "t_a1b2c3d4")
        self.assertTrue(item["exists"])
        self.assertTrue(Path(item["path"]).is_file())
        self.assertGreater(item["updated_at"], 0)

    def test_list_reports_empty_room(self):
        self.assertEqual(reports.list_reports(self.cfg, "nope"), [])

    def test_list_reports_html(self):
        self.write_html()
        items = reports.list_reports(self.cfg, self.room)
        self.assertEqual(items[0]["kind"], "html")
        self.assertEqual(items[0]["title"], "Свой отчёт")
        self.assertEqual(items[0]["blocks"], 0)

    def test_list_reports_broken_json_still_indexed(self):
        (self.dir / "report.json").write_text("{ битый json", encoding="utf-8")
        items = reports.list_reports(self.cfg, self.room)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["exists"])
        self.assertEqual(items[0]["kind"], "spec")
        self.assertTrue(items[0]["title"])  # ключ как запасное название

    def test_list_reports_ignores_non_report_files(self):
        (self.dir / "notes.md").write_text("x", encoding="utf-8")
        self.assertEqual(reports.list_reports(self.cfg, self.room), [])

    def test_list_reports_from_ingest(self):
        deep = self.base / "extra" / "nested"
        p = self.write_spec(dict(GOOD_SPEC, title="Из журнала"), base=deep)
        with mock.patch.object(reports, "_ingest_state", return_value={
                "journal": [{"kind": "report_added", "detail": {"path": str(p)}}]}):
            items = reports.list_reports(self.cfg, self.room)
        self.assertTrue(any(i["title"] == "Из журнала" for i in items))
        keys = [i["path"] for i in items]
        self.assertEqual(len(keys), len(set(keys)), "дубли не допускаются")

    def test_list_reports_dedupes_same_path(self):
        p = self.write_spec(GOOD_SPEC)
        with mock.patch.object(reports, "_ingest_state", return_value={
                "reports": [{"path": str(p)}]}):
            items = reports.list_reports(self.cfg, self.room)
        self.assertEqual(len(items), 1)

    # --- поиск ---
    def test_find_by_key_and_request(self):
        self.write_spec(GOOD_SPEC)
        self.assertEqual(reports.find_report(self.cfg, self.room, self.slug)["key"], self.slug)
        self.assertEqual(reports.find_report(self.cfg, self.room, "t_a1b2c3d4")["key"], self.slug)

    def test_find_rejects_paths(self):
        """D3: key — только слаг; абсолютный путь и `..` — 400, а не чтение файла."""
        p = self.write_spec(GOOD_SPEC)
        for key in (str(p), self.slug + "/report.json", "../report.json", "..", "a\\b"):
            with self.assertRaises(ApiError) as ctx:
                reports.find_report(self.cfg, self.room, key)
            self.assertEqual(ctx.exception.status, 400, key)

    def test_find_missing(self):
        self.write_spec(GOOD_SPEC)
        for key in ("nope", "", None, "unit-economics"):
            self.assertIsNone(reports.find_report(self.cfg, self.room, key), key)
        self.assertIsNone(reports.find_report(self.cfg, "nope", self.slug))

    # --- чтение и валидация ---
    def test_load_valid_spec(self):
        p = self.write_spec(GOOD_SPEC)
        got = reports.load_report(self.cfg, p)
        self.assertEqual(got["kind"], "spec")
        self.assertEqual(got["errors"], [])
        self.assertEqual(got["spec"]["version"], 1)
        self.assertEqual(got["spec"]["title"], "Юнит-экономика моти")
        self.assertEqual(len(got["spec"]["blocks"]), 3)
        self.assertEqual(got["title"], "Юнит-экономика моти")

    def test_load_broken_json(self):
        p = self.dir / "report.json"
        p.write_text("{ битый", encoding="utf-8")
        got = reports.load_report(self.cfg, p)  # не должно бросать
        self.assertEqual(got["kind"], "spec")
        self.assertTrue(got["errors"])
        self.assertIn("JSON", got["errors"][0])

    def test_load_invalid_spec_keeps_spec_and_errors(self):
        p = self.write_spec({"version": 1, "title": "Плохой",
                             "blocks": [{"type": "неизвестный", "x": 1}]})
        got = reports.load_report(self.cfg, p)
        self.assertEqual(got["kind"], "spec")
        self.assertTrue(got["errors"])
        self.assertTrue(got["spec"])  # нормализованное частично — для баннера в UI

    def test_load_missing_file(self):
        got = reports.load_report(self.cfg, self.dir / "нет.json")
        self.assertEqual(got["kind"], "spec")
        self.assertTrue(got["errors"])

    def test_load_accepts_relative_path(self):
        self.write_spec(GOOD_SPEC)
        rel = "reports/%s/%s/report.json" % (self.room, self.slug)
        got = reports.load_report(self.cfg, rel)
        self.assertEqual(got["errors"], [])
        self.assertEqual(got["spec"]["title"], "Юнит-экономика моти")

    def test_load_html(self):
        p = self.write_html()
        got = reports.load_report(self.cfg, p)
        self.assertEqual(got["kind"], "html")
        self.assertEqual(got["url"], "/reports/cafe/%s/report.html" % self.slug)
        self.assertEqual(got["title"], "Свой отчёт")
        self.assertEqual(got["errors"], [])

    def test_load_html_title_fallback(self):
        p = self.write_html("<html><body><h1>Заголовок H1</h1></body></html>")
        got = reports.load_report(self.cfg, p)
        self.assertEqual(got["title"], "Заголовок H1")

    # --- заголовки и статика ---
    def test_html_headers(self):
        h = reports.html_headers()
        csp = h["Content-Security-Policy"]
        self.assertIn("sandbox allow-scripts", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("img-src data: blob:", csp)
        self.assertIn("style-src 'unsafe-inline'", csp)
        self.assertIn("script-src 'unsafe-inline'", csp)
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")

    def test_serve_html(self):
        self.write_html()
        status, headers, body = reports.serve_report_file(self.cfg, self.room, self.slug, "report.html")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertTrue(str(body).endswith("report.html"))

    def test_serve_nested_asset(self):
        img = self.dir / "assets" / "chart.png"
        img.parent.mkdir()
        img.write_bytes(b"\x89PNG\r\n")
        status, headers, body = reports.serve_report_file(self.cfg, self.room, self.slug,
                                                          "assets/chart.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertNotIn("Content-Security-Policy", headers)
        self.assertEqual(Path(body).name, "chart.png")

    def test_serve_svg_is_image_with_sandbox(self):
        svg = self.dir / "pic.svg"
        svg.write_text("<svg/>", encoding="utf-8")
        status, headers, _ = reports.serve_report_file(self.cfg, self.room, self.slug, "pic.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertIn("sandbox", headers["Content-Security-Policy"])

    def test_serve_traversal(self):
        (Path(self.cfg.data_dir) / "secret.md").write_text("тайна", encoding="utf-8")
        cases = ["../secret.md", "../../secret.md", "..\\..\\secret.md", "/etc/passwd",
                 "C:/Windows/win.ini", "assets/../../report.html", "..%2fsecret.md"]
        for rel in cases:
            status, headers, _ = reports.serve_report_file(self.cfg, self.room, self.slug, rel)
            self.assertIn(status, (403, 404), rel)
            self.assertNotEqual(headers.get("Content-Type"), "text/markdown; charset=utf-8")

    def test_serve_bad_room_or_key(self):
        for room, key in (("../cafe", self.slug), (self.room, ".."), (self.room, "a/b"),
                          ("", self.slug), (self.room, "")):
            status, _, _ = reports.serve_report_file(self.cfg, room, key, "report.json")
            self.assertEqual(status, 403, (room, key))

    def test_serve_missing(self):
        self.assertEqual(reports.serve_report_file(self.cfg, self.room, self.slug, "nope.png")[0], 404)
        self.assertEqual(reports.serve_report_file(self.cfg, self.room, "nope", "report.json")[0], 404)
        # не-ASCII key не является слагом отчёта (D3) — отказ на входе, а не поиск
        self.assertEqual(reports.serve_report_file(self.cfg, self.room, "нет", "report.json")[0], 403)

    def test_serve_disallowed_type(self):
        (self.dir / "run.exe").write_bytes(b"MZ")
        status, _, _ = reports.serve_report_file(self.cfg, self.room, self.slug, "run.exe")
        self.assertEqual(status, 403)

    def test_serve_external_report_by_key(self):
        deep = self.base / "external" / "nested"
        self.write_html(base=deep)
        items = reports.list_reports(self.cfg, self.room)
        key = items[0]["key"]
        status, headers, _ = reports.serve_report_file(self.cfg, self.room, key, "report.html")
        self.assertEqual(status, 200)
        self.assertIn("sandbox", headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
