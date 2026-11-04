# -*- coding: utf-8 -*-
"""Тесты reportkit: валидатор, нормализация, лимиты, экспорт в автономный HTML.

Запуск из корня проекта:
    python -m unittest tests/test_reportkit.py -v
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RK = ROOT / "reportkit"
sys.path.insert(0, str(RK))

import export_html as X          # noqa: E402
import validate as V             # noqa: E402

EXAMPLES = sorted((RK / "examples").glob("*.report.json"))
EXTERNAL_REF = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.I)


def spec(*blocks, **kw):
    base = {"version": 1, "title": "Тестовый отчёт", "blocks": list(blocks)}
    base.update(kw)
    return base


def text_block(md="абзац"):
    return {"type": "text", "md": md}


class TestExamples(unittest.TestCase):
    """Четыре демо-отчёта обязаны проходить валидатор без ошибок."""

    def test_examples_exist(self):
        self.assertEqual(len(EXAMPLES), 4, f"ожидалось 4 примера, найдено {len(EXAMPLES)}")

    def test_examples_valid(self):
        for path in EXAMPLES:
            with self.subTest(example=path.name):
                norm, errors, warnings = V.load_file(path)
                self.assertEqual(errors, [], f"{path.name}: {errors}")
                self.assertEqual(warnings, [], f"{path.name}: {warnings}")
                self.assertGreater(len(norm["blocks"]), 0)

    def test_examples_have_demo_mark_and_no_remote_links(self):
        for path in EXAMPLES:
            with self.subTest(example=path.name):
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                self.assertIn("демо", (data.get("subtitle") or "").lower(),
                              "в подзаголовке примера должно быть помечено «демо»")
                self.assertNotIn("http://", raw)
                self.assertNotIn("https://", raw)

    def test_kitchen_sink_covers_all_types(self):
        norm, errors, _ = V.load_file(RK / "examples" / "kitchen-sink.report.json")
        self.assertEqual(errors, [])
        seen = set()

        def walk(blocks):
            for b in blocks:
                seen.add(b["type"])
                if b["type"] == "tabs":
                    for tb in b["tabs"]:
                        walk(tb["blocks"])
                elif b["type"] == "accordion":
                    for it in b["items"]:
                        walk(it["blocks"])
                elif b["type"] == "columns":
                    for col in b["cols"]:
                        walk(col)

        walk(norm["blocks"])
        self.assertEqual(seen, set(V.BLOCK_TYPES), f"не покрыты: {set(V.BLOCK_TYPES) - seen}")


class TestValidBasics(unittest.TestCase):
    def test_minimal(self):
        norm, errors, warnings = V.validate(spec(text_block()))
        self.assertEqual(errors, [])
        self.assertEqual(norm["title"], "Тестовый отчёт")
        self.assertEqual(norm["version"], 1)
        self.assertEqual(warnings, [])

    def test_root_not_object(self):
        _, errors, _ = V.validate([1, 2])
        self.assertTrue(errors)
        self.assertIn("объект", errors[0])

    def test_missing_title(self):
        _, errors, _ = V.validate({"version": 1, "blocks": [text_block()]})
        self.assertTrue(any("title" in e for e in errors), errors)

    def test_empty_blocks_is_error(self):
        """R2: пустой отчёт — ошибка, а не «OK с предупреждением»."""
        _, errors, _ = V.validate({"version": 1, "title": "x", "blocks": []})
        self.assertTrue(errors)
        self.assertTrue(any("пуст" in e for e in errors), errors)

    def test_missing_blocks(self):
        _, errors, _ = V.validate({"version": 1, "title": "x"})
        self.assertTrue(any("blocks" in e for e in errors), errors)

    def test_bad_version(self):
        _, errors, _ = V.validate({"version": 2, "title": "x", "blocks": [text_block()]})
        self.assertTrue(any("version" in e for e in errors), errors)

    def test_broken_json_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text('{"title": "x",,}', encoding="utf-8")
            _, errors, _ = V.load_file(p)
            self.assertTrue(errors)
            self.assertIn("JSON", errors[0])

    def test_missing_file(self):
        _, errors, _ = V.load_file(Path(tempfile.gettempdir()) / "нет-такого-файла-12345.json")
        self.assertTrue(errors)
        self.assertIn("не удалось прочитать", errors[0])


class TestBlockErrors(unittest.TestCase):
    def test_unknown_type_with_suggestion(self):
        _, errors, _ = V.validate(spec({"type": "tabls"}))
        self.assertEqual(len(errors), 1)
        self.assertIn("неизвестный тип", errors[0])
        self.assertIn("'table'", errors[0], "должна быть подсказка ближайшего типа")

    def test_type_path_in_error(self):
        _, errors, _ = V.validate(spec({"type": "heading", "text": "ок"}))
        self.assertFalse(errors)

    def test_error_path_format(self):
        bad = {"type": "chart", "kind": "bar", "series": [{"name": "s", "data": ["нет"]}]}
        _, errors, _ = V.validate(spec(bad))
        self.assertEqual(len(errors), 1)
        self.assertIn("blocks[0].series[0].data", errors[0])
        self.assertRegex(errors[0], r"^blocks\[0\]\.series\[0\]\.data\[0\]:")

    def test_bad_enum(self):
        _, errors, _ = V.validate(spec({"type": "chart", "kind": "pie3d", "series": [{"name": "a", "data": [1]}]}))
        self.assertTrue(any("недопустимое значение" in e for e in errors), errors)

    def test_heading_level_range(self):
        _, errors, _ = V.validate(spec({"type": "heading", "text": "x", "level": 5}))
        self.assertTrue(errors)

    def test_table_rows_not_list(self):
        _, errors, _ = V.validate(spec({"type": "table", "columns": [{"key": "a", "label": "A"}], "rows": {"a": 1}}))
        self.assertTrue(any("ожидался список" in e for e in errors), errors)

    def test_table_duplicate_key(self):
        cols = [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}]
        _, errors, _ = V.validate(spec({"type": "table", "columns": cols, "rows": []}))
        self.assertTrue(any("уже есть" in e for e in errors), errors)

    def test_table_totals_unknown_column(self):
        cols = [{"key": "a", "label": "A", "type": "money"}]
        _, errors, _ = V.validate(spec({"type": "table", "columns": cols, "rows": [], "totals": {"b": "sum"}}))
        self.assertTrue(any("нет в columns" in e for e in errors), errors)

    def test_graph_edge_unknown_node(self):
        g = {"type": "graph", "nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "zzz"}]}
        _, errors, _ = V.validate(spec(g))
        self.assertTrue(any("нет в nodes" in e for e in errors), errors)

    def test_graph_duplicate_id(self):
        g = {"type": "graph", "nodes": [{"id": "a"}, {"id": "a"}], "edges": []}
        _, errors, _ = V.validate(spec(g))
        self.assertTrue(any("уже занят" in e for e in errors), errors)

    def test_links_needs_path_or_url(self):
        _, errors, _ = V.validate(spec({"type": "links", "items": [{"title": "ничего"}]}))
        self.assertTrue(any("path" in e and "url" in e for e in errors), errors)

    def test_callout_bad_tone(self):
        _, errors, _ = V.validate(spec({"type": "callout", "tone": "warning", "md": "x"}))
        self.assertTrue(any("недопустимое значение" in e for e in errors), errors)

    def test_cell_object_allowed_only_for_link(self):
        cols = [{"key": "doc", "label": "Документ", "type": "link"}]
        rows = [{"doc": {"title": "Смета", "path": "C:/x/смета.xlsx"}}]
        norm, errors, _ = V.validate(spec({"type": "table", "columns": cols, "rows": rows}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["rows"][0]["doc"]["title"], "Смета")

    def test_cell_object_rejected_for_text(self):
        cols = [{"key": "t", "label": "Текст"}]
        _, errors, _ = V.validate(spec({"type": "table", "columns": cols, "rows": [{"t": {"a": 1}}]}))
        self.assertTrue(any("скаляром" in e for e in errors), errors)


class TestLimits(unittest.TestCase):
    def test_blocks_limit(self):
        blocks = [text_block() for _ in range(V.MAX_BLOCKS + 1)]
        _, errors, _ = V.validate(spec(*blocks))
        self.assertTrue(any("лимита" in e for e in errors), errors[:5])

    def test_nesting_depth(self):
        deep = {"type": "tabs", "tabs": [{"title": "1", "blocks": [
            {"type": "tabs", "tabs": [{"title": "2", "blocks": [
                {"type": "tabs", "tabs": [{"title": "3", "blocks": [
                    {"type": "tabs", "tabs": [{"title": "4", "blocks": [text_block()]}]}
                ]}]}
            ]}]}
        ]}]}
        _, errors, _ = V.validate(spec(deep))
        self.assertTrue(any("глубок" in e for e in errors), errors)

    def test_nesting_three_levels_ok(self):
        ok = {"type": "tabs", "tabs": [{"title": "1", "blocks": [
            {"type": "columns", "cols": [[{"type": "accordion", "items": [
                {"title": "x", "blocks": [text_block()]}]}]]}
        ]}]}
        _, errors, _ = V.validate(spec(ok))
        self.assertEqual(errors, [])

    def test_table_rows_limit(self):
        cols = [{"key": "a", "label": "A", "type": "number"}]
        rows = [{"a": i} for i in range(V.MAX_TABLE_ROWS + 1)]
        _, errors, _ = V.validate(spec({"type": "table", "columns": cols, "rows": rows}))
        self.assertTrue(any("больше лимита" in e for e in errors), errors[:3])

    def test_graph_nodes_limit(self):
        nodes = [{"id": f"n{i}"} for i in range(V.MAX_GRAPH_NODES + 1)]
        _, errors, _ = V.validate(spec({"type": "graph", "nodes": nodes, "edges": []}))
        self.assertTrue(any("больше лимита" in e for e in errors), errors[:3])

    def test_stats_limit(self):
        items = [{"label": f"п{i}", "value": i} for i in range(V.MAX_STATS + 1)]
        _, errors, _ = V.validate(spec({"type": "stats", "items": items}))
        self.assertTrue(any("показателей" in e for e in errors), errors[:3])


class TestNormalization(unittest.TestCase):
    def test_string_level(self):
        norm, errors, warnings = V.validate(spec({"type": "heading", "text": "x", "level": "2"}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["level"], 2)
        self.assertTrue(any("level" in w for w in warnings), warnings)

    def test_done_string(self):
        norm, errors, _ = V.validate(spec({"type": "checklist", "items": [{"text": "x", "done": "done"}]}))
        self.assertEqual(errors, [])
        self.assertIs(norm["blocks"][0]["items"][0]["done"], True)

    def test_done_no_string(self):
        norm, _, _ = V.validate(spec({"type": "checklist", "items": [{"text": "x", "done": "нет"}]}))
        self.assertIs(norm["blocks"][0]["items"][0]["done"], False)

    def test_alias_kpi(self):
        norm, errors, warnings = V.validate(spec({"type": "kpi", "items": [{"label": "a", "value": 1}]}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["type"], "stats")
        self.assertTrue(any("синоним" in w for w in warnings), warnings)

    def test_text_field_renamed_to_md(self):
        norm, errors, _ = V.validate(spec({"type": "text", "text": "привет"}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["md"], "привет")

    def test_stats_string_value_kept(self):
        norm, errors, _ = V.validate(spec({"type": "stats", "items": [{"label": "a", "value": "48,8 %"}]}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["items"][0]["value"], "48,8 %")

    def test_chart_numbers_as_strings(self):
        norm, errors, warnings = V.validate(
            spec({"type": "chart", "kind": "bar", "series": [{"name": "a", "data": ["1", "2.5"]}]}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["series"][0]["data"], [1.0, 2.5])
        self.assertEqual(len(warnings), 2)

    def test_column_defaults(self):
        cols = [{"key": "m", "label": "Деньги", "type": "money"}]
        norm, _, _ = V.validate(spec({"type": "table", "columns": cols, "rows": []}))
        col = norm["blocks"][0]["columns"][0]
        self.assertEqual(col["align"], "right")
        self.assertEqual(col["decimals"], 2)
        self.assertEqual(col["currency"], "R$")

    def test_mixed_currency_warns_not_errors(self):
        cols = [{"key": "m", "label": "Сумма, ₽", "type": "money", "currency": "₽"}]
        norm, errors, warnings = V.validate(spec(
            {"type": "table", "columns": cols, "rows": [{"m": 38.0}]},
            {"type": "chart", "kind": "bar", "unit": "R$", "x": ["a"],
             "series": [{"name": "Маржа, R$", "data": [19.85]}]}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["columns"][0]["currency"], "₽")
        self.assertTrue(any("смешаны валюты" in w and "R$" in w and "₽" in w for w in warnings), warnings)

    def test_single_currency_no_warning(self):
        cols = [{"key": "m", "label": "Сумма, R$", "type": "money"}]
        _, errors, warnings = V.validate(spec(
            {"type": "table", "columns": cols, "rows": [{"m": 38.0}]},
            {"type": "chart", "kind": "bar", "unit": "R$", "x": ["a"],
             "series": [{"name": "Маржа, R$", "data": [19.85]}]}))
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(V.currency_families({"a": "цена R$ 42, скидка $ 2"}), ["BRL", "USD"])

    def test_percent_default_decimals(self):
        cols = [{"key": "p", "label": "Доля", "type": "percent"},
                {"key": "n", "label": "Штук", "type": "number"}]
        norm, _, _ = V.validate(spec({"type": "table", "columns": cols, "rows": []}))
        self.assertEqual(norm["blocks"][0]["columns"][0]["decimals"], 1)
        self.assertEqual(norm["blocks"][0]["columns"][1]["decimals"], 0)

    def test_filter_default_by_rows(self):
        cols = [{"key": "a", "label": "A", "type": "number"}]
        small = [{"a": i} for i in range(3)]
        big = [{"a": i} for i in range(30)]
        n1, _, _ = V.validate(spec({"type": "table", "columns": cols, "rows": small}))
        n2, _, _ = V.validate(spec({"type": "table", "columns": cols, "rows": big}))
        self.assertFalse(n1["blocks"][0]["filter"])
        self.assertTrue(n2["blocks"][0]["filter"])

    def test_long_text_truncated(self):
        norm, errors, warnings = V.validate(spec(text_block("я" * 25000)))
        self.assertEqual(errors, [])
        self.assertLessEqual(len(norm["blocks"][0]["md"]), V.MAX_TEXT + 1)
        self.assertTrue(any("обрезан" in w for w in warnings), warnings)

    def test_unknown_field_dropped(self):
        norm, errors, warnings = V.validate(spec({"type": "text", "md": "x", "color": "red"}))
        self.assertEqual(errors, [])
        self.assertNotIn("color", norm["blocks"][0])
        self.assertTrue(any("не используется" in w for w in warnings), warnings)

    def test_sources_and_summary(self):
        norm, _, _ = V.validate(spec({"type": "sources", "items": [{"title": "t", "ref": "C:/x"}]},
                                     summary="итог", subtitle="подзаголовок"))
        self.assertEqual(norm["summary"], "итог")
        self.assertEqual(norm["subtitle"], "подзаголовок")

    def test_scatter_points(self):
        ch = {"type": "chart", "kind": "scatter",
              "series": [{"name": "s", "data": [{"x": 1, "y": 2}, {"x": "окт", "y": 3}]}]}
        norm, errors, _ = V.validate(spec(ch))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["series"][0]["data"][1], {"x": "окт", "y": 3.0})

    def test_swot_requires_four_lists(self):
        _, errors, _ = V.validate(spec({"type": "matrix", "kind": "swot", "swot": {"strengths": ["a"]}}))
        self.assertEqual(len(errors), 3, errors)


class TestSummarize(unittest.TestCase):
    def test_counts(self):
        norm, _, _ = V.validate(spec(
            {"type": "table", "columns": [{"key": "a", "label": "A"}], "rows": [{"a": 1}, {"a": 2}]},
            {"type": "graph", "nodes": [{"id": "a"}, {"id": "b"}], "edges": []},
            {"type": "tabs", "tabs": [{"title": "t", "blocks": [text_block()]}]},
        ))
        st = V.summarize(norm)
        self.assertEqual(st["blocks"], 4)
        self.assertEqual(st["rows"], 2)
        self.assertEqual(st["nodes"], 2)


class TestStrictness(unittest.TestCase):
    """R1/R3/R4/R5/R6: валидатор ловит то, что обещает SPEC."""

    def test_non_numeric_in_numeric_column_is_error(self):
        norm, errors, _ = V.validate(spec({
            "type": "table",
            "columns": [{"key": "price", "label": "Цена", "type": "money"},
                        {"key": "name", "label": "Название"}],
            "rows": [{"price": "не число", "name": "A"},
                     {"price": "12 тыс", "name": "B"},
                     {"price": True, "name": "C"},
                     {"price": 190, "name": "D"}],
        }))
        self.assertEqual(len(errors), 3, errors)
        self.assertIn("blocks[0].rows[0].price", errors[0])
        self.assertIn("число", errors[0])
        self.assertEqual(norm["blocks"][0]["rows"][3]["price"], 190)

    def test_numeric_string_is_coerced_with_warning(self):
        norm, errors, warnings = V.validate(spec({
            "type": "table", "columns": [{"key": "n", "label": "N", "type": "number"}],
            "rows": [{"n": "1 234,5"}],
        }))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["rows"][0]["n"], 1234.5)
        self.assertTrue(any("приведено" in w for w in warnings), warnings)

    def test_missing_columns_single_error(self):
        _, errors, _ = V.validate(spec({"type": "table", "rows": []}))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("колонка", errors[0])

    def test_cyrillic_column_key_warns(self):
        _, errors, warnings = V.validate(spec({
            "type": "table", "columns": [{"key": "цена", "label": "Цена"}], "rows": [{"цена": 1}],
        }))
        self.assertEqual(errors, [])
        self.assertTrue(any("латиниц" in w for w in warnings), warnings)

    def test_graph_node_without_label_warns(self):
        norm, errors, warnings = V.validate(spec({"type": "graph", "nodes": [{"id": "a"}], "edges": []}))
        self.assertEqual(errors, [])
        self.assertEqual(norm["blocks"][0]["nodes"][0]["label"], "a")
        self.assertTrue(any("нет label" in w for w in warnings), warnings)

    def test_chart_without_x_warns_for_bars(self):
        for kind in ("stacked", "area", "hbar", "pie"):
            _, errors, warnings = V.validate(spec({"type": "chart", "kind": kind,
                                                   "series": [{"data": [1, 2]}]}))
            self.assertEqual(errors, [], kind)
            self.assertTrue(any("нужен x" in w for w in warnings), (kind, warnings))


class TestCli(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(RK / "validate.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", cwd=str(RK), timeout=120)

    def test_valid_exit_0(self):
        r = self._run(str(EXAMPLES[0]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Итог: OK", r.stdout)

    def test_invalid_exit_1(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.report.json"
            p.write_text(json.dumps(spec({"type": "nonsense"})), encoding="utf-8")
            r = self._run(str(p))
            self.assertEqual(r.returncode, 1)
            self.assertIn("ОШИБКИ", r.stdout)
            self.assertIn("blocks[0].type", r.stdout)

    def test_write_normalized(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "norm.json"
            r = self._run(str(EXAMPLES[0]), "--write", str(out))
            self.assertEqual(r.returncode, 0)
            self.assertTrue(out.is_file())
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["version"], 1)

    def test_json_mode(self):
        r = self._run(str(EXAMPLES[0]), "--json")
        data = json.loads(r.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["errors"], [])
        self.assertGreater(data["stats"]["blocks"], 0)


# --- экспорт ---------------------------------------------------------------
STUB_CSS = "/* tokens */ :root{--background:#fff}\n"
STUB_UTIL = "export function h(tag){ return document.createElement(tag); }\n"
STUB_BLOCKS = "import { h } from '../util.js';\nexport const BLOCKS = { text: () => h('p') };\n"
STUB_MD = "export function renderMarkdown(s){ return String(s); }\n"
STUB_ENTRY = (
    "import { h } from '../util.js';\n"
    "import { BLOCKS } from './blocks.js';\n"
    "export function renderReport(el, spec){ el.textContent = spec.title; return el; }\n"
    "export default renderReport;\n"
)
STUB_DEP = "export const X = 1;\n"


def make_web(root: Path):
    """Мини-дерево web/ с заглушками модулей (как у настоящего рендерера)."""
    (root / "css").mkdir(parents=True, exist_ok=True)
    (root / "js" / "report").mkdir(parents=True, exist_ok=True)
    (root / "css" / "tokens.css").write_text(STUB_CSS, encoding="utf-8")
    (root / "css" / "report.css").write_text("/* report */\n", encoding="utf-8")
    (root / "js" / "util.js").write_text(STUB_UTIL, encoding="utf-8")
    (root / "js" / "md.js").write_text(STUB_MD, encoding="utf-8")
    (root / "js" / "report" / "blocks.js").write_text(STUB_BLOCKS, encoding="utf-8")
    (root / "js" / "report" / "extra.js").write_text(STUB_DEP, encoding="utf-8")
    (root / "js" / "report" / "index.js").write_text(STUB_ENTRY, encoding="utf-8")


class TestExport(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.web = self.dir / "web"
        make_web(self.web)
        self.report = self.dir / "report.json"
        data = json.loads((RK / "examples" / "unit-economics.report.json").read_text(encoding="utf-8"))
        self.report.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.out = self.dir / "out.html"

    def tearDown(self):
        self.td.cleanup()

    def test_creates_file(self):
        ok, errors, warns, info = X.export(self.report, self.out, "auto", self.web)
        self.assertTrue(ok, errors)
        self.assertTrue(self.out.is_file())
        self.assertGreater(info["size"], 5000)

    def test_contains_modules_and_spec(self):
        X.export(self.report, self.out, "auto", self.web)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn('data-module="/js/report/index.js"', html)
        self.assertIn('data-module="/js/util.js"', html)
        self.assertIn('id="rk-spec"', html)
        self.assertIn("renderReport", html)
        # точка входа идёт последней: зависимости уже собраны
        order = json.loads(re.search(r'id="rk-order">(.*?)</script>', html, re.S).group(1))
        # зависимости обязаны идти раньше тех, кто их импортирует
        self.assertLess(order.index("/js/util.js"), order.index("/js/report/index.js"))
        self.assertLess(order.index("/js/report/blocks.js"), order.index("/js/report/index.js"))

    def test_rewrite_helper_present(self):
        X.export(self.report, self.out, "auto", self.web)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn("URL.createObjectURL", html)
        self.assertIn("new Blob", html)
        self.assertIn("__rkDyn", html)

    def test_no_external_resources(self):
        X.export(self.report, self.out, "auto", self.web)
        html = self.out.read_text(encoding="utf-8")
        self.assertIsNone(EXTERNAL_REF.search(html), "в HTML не должно быть внешних src/href http")
        self.assertNotIn("@import url(http", html)
        self.assertIsNone(re.search(r"<link[^>]+href=[\"']http", html, re.I))

    def test_theme_switch(self):
        X.export(self.report, self.out, "dark", self.web)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn('data-theme="dark"', html)
        X.export(self.report, self.out, "light", self.web)
        self.assertIn('data-theme="light"', self.out.read_text(encoding="utf-8"))
        X.export(self.report, self.out, "auto", self.web)
        self.assertIn('data-theme="auto"', self.out.read_text(encoding="utf-8"))

    def test_invalid_report_refused(self):
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps(spec({"type": "nonsense"})), encoding="utf-8")
        out = self.dir / "never.html"
        ok, errors, _, _ = X.export(bad, out, "auto", self.web)
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertFalse(out.exists())

    def test_missing_web_falls_back(self):
        empty = self.dir / "empty-web"
        empty.mkdir()
        ok, errors, warns, info = X.export(self.report, self.out, "auto", empty)
        self.assertTrue(ok, errors)
        self.assertFalse(info["has_entry"])
        self.assertTrue(any("рендерера" in w for w in warns), warns)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn("__reportkitFallback", html)

    def test_cli_prints_size(self):
        r = subprocess.run([sys.executable, str(RK / "export_html.py"), str(self.report),
                            str(self.out), "--web", str(self.web)],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"Файл: .+ — [\d.,]+ КБ \(\d+ байт\)")

    def test_inline_image(self):
        png = (RK / "examples" / "assets" / "demo-chart.png")
        self.assertTrue(png.is_file(), "нужна демо-картинка рядом с примерами")
        # берём пример на месте: картинка лежит рядом с ним (examples/assets)
        rep = RK / "examples" / "kitchen-sink.report.json"
        ok, errors, _, info = X.export(rep, self.out, "auto", self.web)
        self.assertTrue(ok, errors)
        self.assertEqual(info["images"], 1)
        self.assertIn("data:image/png;base64,", self.out.read_text(encoding="utf-8"))

    def test_script_close_is_escaped(self):
        (self.web / "js" / "report" / "evil.js").write_text(
            "export const s = '</script><b>x</b>';\n", encoding="utf-8")
        (self.web / "js" / "report" / "index.js").write_text(
            STUB_ENTRY + "import './evil.js';\n", encoding="utf-8")
        X.export(self.report, self.out, "auto", self.web)
        html = self.out.read_text(encoding="utf-8")
        self.assertNotIn("</script><b>", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
