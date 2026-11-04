"""Тесты экспорта запроса (server/export.py): Markdown и автономный HTML."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, export  # noqa: E402

SPEC = {
    "version": 1,
    "title": "Юнит-экономика моти",
    "summary": "Маржа 48,8 %.",
    "blocks": [{"type": "stats", "items": [{"label": "Маржа", "value": "48,8 %"}]},
               {"type": "text", "md": "Цена 220 ₽ даёт 48,8 % маржи."},
               {"type": "gallery", "images": [{"src": "chart.png", "caption": "Маржа"}]}],
}


def make_detail(report_path=None, answer="## Вывод\n\nМаржа **48,8 %** при цене 220 ₽."):
    return {
        "id": "t_a1b2c3d4", "room": "cafe", "title": "Себестоимость моти", "status": "done",
        "created_at": 1761700000, "duration_s": 5400, "text": "Посчитай себестоимость моти",
        "synthesis": {"state": "ready", "text_md": answer, "author": "biz-director",
                      "at": 1761701000, "version": 1, "legacy": False},
        "next_steps": [
            {"id": "ns_1", "text": "Согласовать цену 220 ₽", "owner": "biz-cfo",
             "due": "2026-11-05", "state": "open"},
            {"id": "ns_2", "text": "Запросить КП на муку", "owner": None, "due": None,
             "state": "open"},
        ],
        "open_questions": [
            {"id": "q_1", "text": "Какой канал для D19?", "for": "mikhail", "answered": False},
            {"id": "q_2", "text": "Подтвердить состав?", "for": "mikhail", "answered": True,
             "answer": "Да, подтверждаю"},
        ],
        "decisions": [
            {"id": "d_1", "text": "Цена 220 ₽", "why": "маржа 48,8 %", "status": "accepted"},
            {"id": "d_2", "text": "Вторая кухня", "why": None, "status": "proposed"},
        ],
        "stage": {"current": "legal", "phases": [
            {"id": "idea", "title": "Идея", "status": "done", "note": ""},
            {"id": "legal", "title": "Легализация", "status": "active", "note": "ИНН"},
            {"id": "brand", "title": "Бренд", "status": "blocked", "note": "ждём Роспатент"},
        ]},
        "artifacts": [{"path": "C:/TeamRoom/Vault/Projects/Лето/смета.xlsx", "title": "Смета",
                       "exists": True},
                      {"path": "C:/TeamRoom/Vault/Projects/Лето/нет.md", "title": "Нет файла",
                       "exists": False}],
        "tree": [
            {"id": "t_a1b2c3d4", "parent_ids": [], "title": "Себестоимость моти",
             "assignee": "biz-director", "role": "root", "status": "done", "duration_s": 5400},
            {"id": "t_child0001", "parent_ids": ["t_a1b2c3d4"], "title": "Юнит-экономика",
             "assignee": "biz-cfo", "role": "worker", "status": "done",
             "summary_md": "Маржа 48,8 %.\nПодробности в отчёте."},
            {"id": "t_child0002", "parent_ids": ["t_child0001"], "title": "Проверка цифр",
             "assignee": "biz-critic", "role": "critic", "status": "done"},
        ],
        "progress": {"done": 2, "total": 2, "percent": 100},
        "report": ({"available": True, "kind": "spec", "path": str(report_path),
                    "title": "Юнит-экономика моти"} if report_path else
                   {"available": False, "kind": None, "path": None, "title": None}),
        "warnings": [{"code": "legacy_metadata", "message": "Metadata старого формата"}],
    }


class MarkdownTest(unittest.TestCase):
    def setUp(self):
        self.md = export.export_request_md(make_detail())

    def test_header_and_meta(self):
        self.assertTrue(self.md.startswith("# Себестоимость моти"))
        self.assertIn("**Статус:** готово", self.md)
        self.assertIn("**Комната:** cafe", self.md)
        self.assertIn("**Длительность:** 1 ч 30 мин", self.md)
        self.assertIn("**Прогресс:** 2 из 2 шагов (100%)", self.md)

    def test_answer_present(self):
        self.assertIn("## Ответ", self.md)
        self.assertIn("Маржа **48,8 %** при цене 220 ₽.", self.md)
        self.assertIn("автор: biz-director", self.md)

    def test_steps_table(self):
        self.assertIn("| Шаг | Кто | Срок | Статус |", self.md)
        self.assertIn("| Согласовать цену 220 ₽ | biz-cfo | 2026-11-05 | open |", self.md)
        self.assertIn("| Запросить КП на муку | — | — | open |", self.md)

    def test_questions(self):
        self.assertIn("## Вопросы к тебе", self.md)
        self.assertIn("**Михаил** — Какой канал для D19?", self.md)
        self.assertIn("✓ **Михаил** — Подтвердить состав?", self.md)
        self.assertIn("ответ: Да, подтверждаю", self.md)

    def test_decisions(self):
        self.assertIn("## Решения", self.md)
        self.assertIn("Цена 220 ₽ _(принято)_", self.md)
        self.assertIn("почему: маржа 48,8 %", self.md)
        self.assertIn("Вторая кухня _(предложено)_", self.md)

    def test_phases(self):
        self.assertIn("## Этапы проекта", self.md)
        self.assertIn("✓ **Идея** — готово", self.md)
        self.assertIn("▶ **Легализация** — в работе (ИНН)", self.md)
        self.assertIn("⚠ **Бренд** — ждёт (ждём Роспатент)", self.md)

    def test_artifacts_and_tree(self):
        self.assertIn("## Артефакты", self.md)
        self.assertIn("`C:/TeamRoom/Vault/Projects/Лето/смета.xlsx`", self.md)
        self.assertIn("(не найден)", self.md)
        self.assertIn("## Дерево работ", self.md)
        self.assertIn("- **Себестоимость моти** (biz-director, корень, done", self.md)
        self.assertIn("  - **Юнит-экономика** (biz-cfo, исполнитель, done", self.md)
        self.assertIn("    - **Проверка цифр** (biz-critic, критик, done)", self.md)
        self.assertIn("Маржа 48,8 %.", self.md)

    def test_warnings_and_footer(self):
        self.assertIn("## Предупреждения", self.md)
        self.assertIn("Metadata старого формата", self.md)
        self.assertIn("_Экспорт «Комнаты команды»", self.md)

    def test_pending_answer(self):
        detail = make_detail()
        detail["synthesis"] = {"state": "pending", "text_md": ""}
        md = export.export_request_md(detail)
        self.assertIn("_Ответ ещё готовится._", md)

    def test_minimal_detail(self):
        md = export.export_request_md({})
        self.assertTrue(md.startswith("# Запрос"))
        md = export.export_request_md(None)
        self.assertIn("# Запрос", md)

    def test_no_python_none_leaks(self):
        detail = make_detail()
        detail["next_steps"] = ["просто строка"]  # legacy/мусор не должен ломать
        detail["open_questions"] = ["Строковый вопрос?"]
        detail["decisions"] = ["Строковое решение"]
        detail["artifacts"] = ["C:/TeamRoom/Vault/Projects/Лето/x.md"]
        detail["tree"] = []
        md = export.export_request_md(detail)
        self.assertNotIn("None", md)
        self.assertIn("Статус:", md)
        self.assertIn("| просто строка | — | — | open |", md)
        self.assertIn("**Михаил** — Строковый вопрос?", md)
        self.assertIn("Строковое решение", md)
        self.assertIn("`C:/TeamRoom/Vault/Projects/Лето/x.md`", md)


class HtmlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="tr-export-"))
        cls._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(cls.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(cls.tmp / "vault"),
        })
        cls._env.start()
        cls.cfg = config.load()
        cls.cfg.ensure_dirs()
        cls.report_dir = Path(cls.cfg.data_dir) / "reports" / "cafe" / "unit"
        cls.report_dir.mkdir(parents=True)
        cls.spec_path = cls.report_dir / "report.json"
        cls.spec_path.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
        cls._img = cls.report_dir / "chart.png"
        cls._img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def html(self, detail=None):
        return export.export_request_html(self.cfg, detail or make_detail())

    def test_autonomous(self):
        html = self.html(make_detail(self.spec_path))
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn('<html lang="ru"', html)
        self.assertIn('data-theme="auto"', html)
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn("<style>", html)
        # теги ищем в разметке: внутри инлайн-скриптов слово «<link>» встречается
        # в комментариях движка отчётов — это не внешняя ссылка
        markup = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
        self.assertTrue("<link" not in markup, "внешний <link>")
        self.assertTrue("<script src=" not in html, "внешний script")
        self.assertTrue('src="http' not in html, "src http")
        self.assertTrue('href="http' not in html, "href http")
        self.assertTrue("url(http" not in html, "css url(http)")
        self.assertTrue("@import" not in html, "css @import")

    def test_no_external_references(self):
        html = self.html(make_detail(self.spec_path))
        self.assertIsNone(re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.I))
        # единственный http:// — XML-namespace SVG в util.js, не сетевой запрос
        self.assertLessEqual(html.count("http://"), 1)
        self.assertIn("http://www.w3.org/2000/svg", html)

    def test_inline_css_tokens(self):
        html = self.html()
        self.assertIn("--background", html)
        self.assertIn("tokens.css", html)
        self.assertIn("report.css", html)
        self.assertIn(".tr-export", html)

    def test_markdown_rendered_by_md_module(self):
        html = self.html()
        self.assertIn('data-module="/js/md.js"', html)
        self.assertIn('data-module="/js/md-inline.js"', html)
        self.assertIn('data-module="/js/util.js"', html)
        self.assertIn('data-module="/js/tr-export.js"', html)
        self.assertIn("renderMarkdown", html)
        self.assertIn("__rkDyn", html)  # тот же Blob-загрузчик reportkit

    def test_answer_in_payload(self):
        html = self.html()
        self.assertIn("Маржа **48,8 %** при цене 220 ₽.", html)
        self.assertIn('id="rk-spec"', html)
        payload = json.loads(re.search(r'<script type="application/json" id="rk-spec">(.*?)</script>',
                                       html, re.S).group(1))
        self.assertEqual(payload["title"], "Себестоимость моти")
        self.assertIn("комната cafe", payload["subtitle"])
        self.assertIn("## Ответ", payload["md"])

    def test_report_spec_embedded(self):
        html = self.html(make_detail(self.spec_path))
        self.assertIn('data-module="/js/report/index.js"', html)
        self.assertIn("Юнит-экономика моти", html)
        payload = json.loads(re.search(r'<script type="application/json" id="rk-spec">(.*?)</script>',
                                       html, re.S).group(1))
        self.assertIsNotNone(payload["report"])
        self.assertEqual(payload["report"]["spec"]["title"], "Юнит-экономика моти")
        self.assertEqual(len(payload["report"]["spec"]["blocks"]), 3)
        self.assertIn("data:image/png;base64", payload["report"]["spec"]["blocks"][2]["images"][0]["src"])

    def test_no_report(self):
        payload = json.loads(re.search(
            r'<script type="application/json" id="rk-spec">(.*?)</script>',
            self.html(), re.S).group(1))
        self.assertIsNone(payload["report"])

    def test_broken_report_is_not_embedded(self):
        broken = self.report_dir / "broken"
        broken.mkdir(exist_ok=True)
        p = broken / "report.json"
        p.write_text("{ битый", encoding="utf-8")
        html = self.html(make_detail(p))
        payload = json.loads(re.search(r'<script type="application/json" id="rk-spec">(.*?)</script>',
                                       html, re.S).group(1))
        self.assertIsNone(payload["report"])
        self.assertIn("Отчёт не встроен", payload["note"])

    def test_xss_in_answer_is_escaped_in_payload(self):
        detail = make_detail(answer='<script>alert(1)</script> <img src=x onerror=alert(2)>')
        html = self.html(detail)
        self.assertTrue("<script>alert(1)</script>" not in html, "сырой script")
        self.assertTrue("<img src=x onerror" not in html, "сырой img")
        self.assertIn("\\u003cscript\\u003e", html)

    def test_xss_in_title(self):
        detail = make_detail()
        detail["title"] = '</title><script>alert(1)</script>'
        html = self.html(detail)
        self.assertEqual(html.count("<title>"), 1)
        self.assertTrue("</title><script>" not in html, "разрыв title")
        self.assertIn("&lt;/title&gt;", html)

    def test_size_is_reasonable(self):
        html = self.html(make_detail(self.spec_path))
        self.assertLess(len(html.encode("utf-8")), 1_500_000)
        self.assertGreater(len(html.encode("utf-8")), 50_000)  # стили и модули на месте

    def test_works_without_web_dir(self):
        import types
        stub = types.SimpleNamespace(web_dir=Path(self.tmp) / "нет-веба",
                                     data_dir=self.cfg.data_dir)
        html = export.export_request_html(stub, make_detail())
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("Маржа **48,8 %**", html)
        self.assertTrue('data-module="/js/md.js"' not in html, "рендерера нет — не врём")


if __name__ == "__main__":
    unittest.main(verbosity=2)
