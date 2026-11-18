# -*- coding: utf-8 -*-
"""W24-N1: формат итога в контракте агентов.

Проверяем три вещи:
  * блок «Как оформлять итог» есть и он короткий (не раздувает карточку);
  * он попадает в карточку руководителя (запрос, доработка) и в карточку
    исполнителя (тело направления из карты проекта);
  * он не дублируется и не ломает уже существующие части контракта.

Данные — вымышленные и нейтральные (правило W24 §0.1): никакого конкретного
бизнеса, только «Сайт студии» и пути вида `C:/Работа/...`.

Запуск:
    python -m unittest tests.test_svod_format_contract -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import contract, plan  # noqa: E402

MAX_ADDED = 900          # сколько знаков блок имеет право добавить в тело карточки


class FakeCfg:
    """Минимальный cfg: контракту нужны только три каталога."""

    def __init__(self, base: Path):
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"
        self.web_dir = ROOT / "web"
        self.data_dir = base / "data"


def make_room(**over):
    room = {
        "id": "studio",
        "title": "Сайт студии",
        "board": "team",
        "orchestrator": "lead",
        "critic": "critic",
        "members": [
            {"profile": "designer", "role": "worker", "note": "собирает макеты"},
            {"profile": "critic", "role": "critic", "note": ""},
        ],
        "context_files": [],
        "critic_pass": False,
    }
    room.update(over)
    return room


class SummaryFormatRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-svod-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.room = make_room()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rule_is_short_and_says_the_important_things(self):
        rule = contract.summary_format_rule()
        self.assertTrue(rule)
        self.assertLessEqual(len(rule), MAX_ADDED, "блок не должен раздувать карточку")
        self.assertIn("Коротко", rule)
        self.assertIn("## Что сделано", rule)
        self.assertIn("`C:/папка/файл.md`", rule)
        self.assertIn("списками", rule)
        # никакой вёрстки: агент не пишет HTML
        self.assertIn("не писать HTML", rule)

    def test_leader_card_contains_the_block_once(self):
        body = contract.build_request_body(self.room, "Собери план на квартал.", "r-slug", self.cfg)
        rule = contract.summary_format_rule()
        self.assertEqual(body.count(rule), 1, "блок ровно один раз")
        self.assertIn("КОМНАТА-КОНТРАКТ", body)
        # порядок: блок формата стоит после правил комнаты, а не вместо них
        self.assertLess(body.index("Свод команды обновляет ПРИЛОЖЕНИЕ"), body.index(rule))

    def test_revision_card_contains_the_block_once(self):
        body = contract.build_revision_body(self.room, "Собери план на квартал.", "# Свод\n\nтекст",
                                            cfg=self.cfg)
        rule = contract.summary_format_rule()
        self.assertEqual(body.count(rule), 1)
        self.assertIn("ПРЕДЫДУЩИЙ ОТВЕТ", body)

    def test_brief_card_contains_the_block_once(self):
        body = contract.build_brief_body(self.room, "Что входит в квартал?", cfg=self.cfg)
        self.assertEqual(body.count(contract.summary_format_rule()), 1)

    def test_worker_card_contains_the_block_once(self):
        """Карточку исполнителя собирает приложение (направление из карты проекта)."""
        streams = [
            {"id": "s1", "title": "Главная страница", "goal": "макет и текст", "owner": "designer",
             "status": "approved", "members": [], "depends_on": []},
            {"id": "s2", "title": "Внутренние страницы", "goal": "шаблоны", "owner": "designer",
             "status": "planned", "members": [], "depends_on": ["s1"]},
        ]
        fake_plan = {"streams": streams, "brief_root_id": None}
        body = plan._stream_body(self.cfg, fake_plan, streams[0], self.room)
        rule = contract.summary_format_rule()
        self.assertEqual(body.count(rule), 1)
        self.assertIn("Направление: Главная страница", body)
        self.assertIn("итог верни как обычно", body)

    def test_block_does_not_break_card_size_budget(self):
        """Прирост тела карточки от блока — ровно длина блока, без «эха»."""
        base = contract.build_contract_block(self.room, "r-slug", self.cfg)
        rule = contract.summary_format_rule()
        self.assertIn(rule, base)
        self.assertLessEqual(len(rule), MAX_ADDED)
        self.assertLessEqual(base.count(rule), 1)

    def test_guide_has_the_same_block(self):
        """Документ для людей и контракт в коде не должны разъезжаться."""
        guide = (ROOT / "docs" / "AGENT-GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("### Как оформлять итог", guide)
        for line in ("Коротко", "## Что сделано", "## Чего не нашли", "## Что не проверено",
                     "`C:/папка/файл.md`"):
            self.assertIn(line, guide)


if __name__ == "__main__":
    unittest.main()
