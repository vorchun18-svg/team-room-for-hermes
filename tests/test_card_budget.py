# -*- coding: utf-8 -*-
"""Бюджеты тела карточки (WAVE25 §M2, R4 §4.3): потолки, выжимка, подгонка.

Проверяем то, что можно проверить без доски и сети:
  * ни один тип карточки не превышает свой потолок на реалистичных данных;
  * тело доработки не растёт с номером версии (20 версий — тот же потолок);
  * прошлый свод приходит выжимкой + ссылкой на карточку версии, а не целиком;
  * контракт комнаты не дублируется целиком: в доработке — ядро и указатель;
  * выжимка детерминирована и не длиннее потолка.

Данные вымышленные (правило W25 §0/24 §0.1): «Сайт студии», «Ремонт офиса».

Запуск:
    python -m unittest tests.test_card_budget -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import card_budget, contract  # noqa: E402


class FakeCfg:
    def __init__(self, base: Path):
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"


def make_room(**over):
    room = {
        "id": "studio", "title": "Сайт студии", "board": "team", "tenant": "room:studio",
        "orchestrator": "lead", "critic": "critic",
        "members": [{"profile": "designer", "role": "worker", "note": "собирает макеты"}],
        "context_files": [], "critic_pass": True,
    }
    room.update(over)
    return room


LONG_SVOD = ("## Коротко\nСвод по сайту готов.\n\n## Что сделано\n"
             + "\n".join("- пункт %d: проверили раздел и поправили текст" % i for i in range(300))
             + "\n\n## Что осталось\n- согласовать сроки\n")
DIGEST = {"version": 7, "count": 4,
          "text": "Ответы Михаила на вопросы — 4:\n"
                  + "\n".join("%d. Вопрос: вопрос %d про сроки и объём работ\n   Ответ: да, так и делаем"
                              % (i, i) for i in range(1, 5))}


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-budget-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.room = make_room()

    def tearDown(self):
        self.tmp.cleanup()

    # --- цифры ---------------------------------------------------------
    def test_budgets_are_the_canon_values(self):
        self.assertEqual(card_budget.BUDGETS, {"leader": 6000, "worker": 2500, "critic": 8000,
                                               "svod": 8000, "service": 2000})
        self.assertEqual(card_budget.limit("revision"), 8000, "slug доработки → свод")
        self.assertEqual(card_budget.limit("request"), 6000, "slug запроса → руководитель")
        self.assertEqual(card_budget.limit("неизвестно"), 2000, "чужое — служебное")

    def test_kind_of_recognises_app_titles(self):
        self.assertEqual(card_budget.kind_of("revision"), "svod")
        self.assertEqual(card_budget.kind_of("", "Обновление свода (авто)"), "svod")
        self.assertEqual(card_budget.kind_of("", "Направление: Главная"), "worker")
        self.assertEqual(card_budget.kind_of("", "Проверка критиком"), "critic")

    # --- тела карточек -------------------------------------------------
    def test_leader_body_fits_budget(self):
        text = ("Проверь сайт студии и скажи, что делать по шагам. " * 15)[:900]
        body = contract.build_request_body(self.room, text, "request", self.cfg)
        self.assertLessEqual(len(body), card_budget.limit("leader"),
                             "тело руководителя ≤ 6 000: %d" % len(body))
        self.assertIn(text, body, "текст Михаила не режется")

    def test_revision_body_fits_budget_and_keeps_digest(self):
        body = contract.build_revision_body(self.room, "Проверь сайт. " * 40, LONG_SVOD, None,
                                            "сделай короче", self.cfg, digest=DIGEST,
                                            prev_card="t_prev123")
        self.assertLessEqual(len(body), card_budget.limit("svod"),
                             "тело доработки ≤ 8 000: %d" % len(body))
        self.assertIn("ПОСЛЕ ВЕРСИИ 7", body)
        self.assertIn("t_prev123", body, "ссылка на карточку прошлой версии")

    def test_prev_answer_is_a_summary_not_the_whole_text(self):
        body = contract.build_revision_body(self.room, "запрос", LONG_SVOD, None, None,
                                            self.cfg, prev_card="t_prev")
        self.assertNotIn(LONG_SVOD, body, "прошлый свод не вставляется целиком")
        self.assertIn("ПРЕДЫДУЩИЙ ОТВЕТ", body)
        self.assertIn("карточка `t_prev`", body)
        self.assertLess(len(body), len(LONG_SVOD) / 2 + 3000)

    def test_body_does_not_grow_with_version_number(self):
        sizes = []
        for version in (1, 5, 12, 20):
            digest = {"version": version, "text": DIGEST["text"], "count": 4}
            body = contract.build_revision_body(self.room, "Проверь сайт. " * 20, LONG_SVOD,
                                                None, None, self.cfg, digest=digest,
                                                prev_card="t_v%d" % version)
            sizes.append(len(body))
        self.assertLessEqual(max(sizes), card_budget.limit("svod"))
        self.assertLessEqual(max(sizes) - min(sizes), 400,
                             "размер тела почти не зависит от номера версии: %s" % sizes)

    def test_contract_is_not_duplicated_in_revision(self):
        core = contract.build_contract_block(self.room, "revision", self.cfg)
        full = contract.build_contract_block(self.room, "request", self.cfg)
        self.assertLess(len(core), len(full) / 3, "ядро короче полного устава втрое")
        self.assertIn("корневой карточке", core)
        body = contract.build_revision_body(self.room, "запрос", "ответ", None, None, self.cfg)
        # перечень полей metadata живёт в корневой карточке, в доработку не копируется
        self.assertNotIn("phases_proposal:", body)
        self.assertIn("changes", body)

    def test_critic_body_fits_budget(self):
        body = contract.build_critic_body(self.room, "запрос " * 100, "ответ " * 2000, self.cfg)
        self.assertLessEqual(len(body), card_budget.limit("critic"))

    def test_service_card_budget_is_two_thousand(self):
        self.assertEqual(card_budget.limit("service"), 2000)

    # --- выжимка -------------------------------------------------------
    def test_summarize_is_bounded_deterministic_and_keeps_short_text(self):
        short = "## Коротко\nВсё готово."
        self.assertEqual(card_budget.summarize(short, 1000), short)
        cut = card_budget.summarize(LONG_SVOD, 600)
        self.assertLessEqual(len(cut), 600)
        self.assertEqual(cut, card_budget.summarize(LONG_SVOD, 600), "детерминированно")
        self.assertIn("Коротко", cut, "заголовок раздела сохраняется")

    def test_allowance_shrinks_prev_before_digest(self):
        room = card_budget.allowance("svod", 2000)
        self.assertLessEqual(room["prev"] + room["digest"], card_budget.limit("svod") - 2000)
        self.assertGreaterEqual(room["prev"], card_budget.MIN_PREV)
        tight = card_budget.allowance("svod", 7600)
        self.assertLess(tight["prev"], room["prev"], "места мало — выжимка короче")

    def test_audit_counts_over_budget(self):
        report = card_budget.audit([{"kind": "svod", "chars": 26000},
                                    {"kind": "svod", "chars": 3000},
                                    {"kind": "critic", "chars": 1200}])
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["over"], 1)
        self.assertEqual(report["kinds"]["svod"]["max"], 26000)
        self.assertEqual(report["kinds"]["svod"]["limit"], 8000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
