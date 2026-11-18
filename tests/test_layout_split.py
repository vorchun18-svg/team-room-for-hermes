#!/usr/bin/env python3
"""Раскладка страницы запроса на узких окнах — живой замер в Chromium (W21-R1).

Жалоба 11.11: «на пол-экрана центр сжимается, боковые колонки остаются прежними».
Тест поднимает мок-сервер на своём порту, открывает страницу запроса в настоящем
Chromium (свой временный профиль) и требует, чтобы на 1440…560 px выполнялись
правила из docs/WAVE21-RULES.md §1 пп. 1–3:

  * основной блок ≥ 640 px в двух колонках (W25-L1 / R5 §9 п. 1) и ≥ 320 px в любой;
  * правая колонка ≥ 280 px, когда колонок две;
  * две колонки только при ширине вида ≥ 1100 px (и обязательно при ней);
  * строка текста в своде ≥ 45 знаков;
  * нет горизонтальной прокрутки;
  * в одноколоночном режиме «От вас нужно» — нижний лист с ручкой
    «От вас нужно · N» (W25-L1; прежде была плашка «Нужен ваш ответ · N»).

На старом коде проверка красная: при окне 1100 px основной блок был 428 px.
Подробный прогон (все маршруты × 15 ширин × три набора данных) — вручную:
`tools/probe_layout_split.py`. Без Chromium тест честно пропускается.

    python -m unittest tests.test_layout_split
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import mock_server as mock          # noqa: E402

# W21-REV2: `tools/cdp.py` и `tools/probe_layout_split.py` — рабочие инструменты
# разработчика, в релиз они НЕ идут (`tools/build_release.py::COPY_TOOLS`,
# `probe_*.py` намеренно исключены). В собранном пакете этого теста быть не должно
# красным: честно пропускаем, а не падаем на импорте (иначе `--run-tests` сборки
# релиза ломался бы ImportError'ом).
try:
    from tools import cdp as cdp_tool             # noqa: E402
    from tools import probe_layout_split as probe  # noqa: E402
    MISSING_TOOLS = ""
except ImportError as _exc:                        # noqa: PERF203 - один раз при импорте
    cdp_tool = None
    probe = None
    MISSING_TOOLS = str(_exc)


class Args:
    """То, что probe.judge ждёт от argparse (пороги — канон R5 §9)."""
    min_main = 640
    min_aside = 280


class LayoutSplitTest(unittest.TestCase):
    WIDTHS = (1440, 1280, 1100, 1024, 960, 900, 640, 560)

    @classmethod
    def setUpClass(cls):
        if MISSING_TOOLS:
            raise unittest.SkipTest("живой замер вёрстки доступен только в рабочем дереве "
                                    "(tools/cdp.py, tools/probe_layout_split.py): %s" % MISSING_TOOLS)
        try:
            cdp_tool.find_chrome()
        except RuntimeError as err:  # на чистом раннере Chromium нет — проверять нечего
            raise unittest.SkipTest("нет Chromium/Chrome: живой замер вёрстки недоступен (%s)" % err)
        cls.server = mock.create_server(0, os.path.join(ROOT, "web"), os.path.join(ROOT, "fixtures"),
                                        phase_scale=0.03)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request_id(self) -> str:
        items = probe.room_requests(self.base, "cafe")
        pick = probe.pick_request(items)
        self.assertIsNotNone(pick, "в моке нет запросов — проверять нечего")
        return pick["id"]

    def test_request_page_columns_and_min_widths(self):
        rid = self._request_id()
        url = f"{self.base}/?theme=light#/room/cafe/request/{rid}"
        problems = []
        with cdp_tool.Chrome.launch(width=1280, height=900) as ch:
            for width in self.WIDTHS:
                row = probe.measure(ch, url, width, pause=1.7)
                got = probe.judge("request", row, Args)
                if got:
                    problems.append((width, got, row.get("main"), row.get("aside")))
                # W25-L1: в одной колонке «От вас нужно» — нижний лист с ручкой;
                # в двух колонках листа быть не должно.
                sheet = row.get("sheet")
                two = (row.get("view") or 0) >= probe.TWO_COL_MIN
                if two and sheet:
                    problems.append((width, ["лист виден в двух колонках"], row.get("main"), row.get("aside")))
                if not two and not sheet:
                    problems.append((width, ["нет листа «От вас нужно» в одной колонке"],
                                     row.get("main"), row.get("aside")))
        self.assertEqual(problems, [], "раскладка страницы запроса: " + json.dumps(problems, ensure_ascii=False))

    def test_all_routes_have_no_horizontal_scroll(self):
        """Остальные экраны комнаты: полоса меню не должна ломать раскладку.

        Полный обход (9 маршрутов × 15 ширин) — вручную через
        `tools/probe_layout_split.py`; здесь — короткий сторож на двух ширинах.
        """
        rid = self._request_id()
        routes = [r for r in probe.ROUTES if r[0] in ("request", "backlog", "team", "reports")]
        problems = []
        with cdp_tool.Chrome.launch(width=1280, height=900) as ch:
            for name, tpl in routes:
                hash_ = tpl.format(room="cafe", rid=rid)
                url = self.base + "/?theme=light" + hash_
                for width in (1100, 900):
                    row = probe.measure(ch, url, width, pause=1.5)
                    got = probe.judge(name, row, Args)
                    if got:
                        problems.append((name, width, got))
        self.assertEqual(problems, [], "маршруты комнаты: " + json.dumps(problems, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
