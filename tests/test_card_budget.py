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
        # W28-E: полный устав теперь живёт файлом (в карточке — короткая версия), поэтому
        # «втрое короче» сравниваем с полным текстом файла: смысл правила (в доработке нет
        # устава целиком) сохранён, а обещание «ядро втрое короче устава» — стало строже.
        detailed = contract.full_contract_text(self.room, "request", self.cfg)
        self.assertLess(len(core), len(detailed) / 3, "ядро короче полного устава втрое")
        self.assertLess(len(full), len(detailed), "в карточке — короткая версия, файл длиннее")
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


class W28BodyTest(unittest.TestCase):
    """W28-E: урезанное тело карточки, «последние N без дублей», резюме родителей.

    Замеры 19.11.2026: устав комнаты (4 722 знака) повторялся в каждой корневой
    карточке, прошлый свод и ленты — тоже. Здесь проверяется, что потолки стали
    правилом: короткий устав + файл полного устава, резюме родителей и ленты по N.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-budget-w28-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.room = make_room()

    def tearDown(self):
        self.tmp.cleanup()

    def test_w28_constants_are_declared(self):
        # Потолок короткого устава: замер 19.11 дал 4 722 знака в карточке; после
        # правки — 3 882, потолок 4 400 (запас на длинные пути). Больше 4 400 —
        # значит устав снова расползся.
        self.assertLessEqual(card_budget.CONTRACT_SHORT_MAX, 4400,
                             "короткий устав в карточке не больше 4 400 знаков")
        self.assertEqual(card_budget.RECENT_MAX, 3, "«Recent work» — последние 3")
        self.assertEqual(card_budget.COMMENTS_MAX, 5, "«Comment thread» — последние 5")
        self.assertEqual(card_budget.PARENT_RESULT_MAX, 1200)
        self.assertGreaterEqual(card_budget.PREV_PREFIX_MAX, 400)

    def test_leader_card_has_short_contract_and_points_to_file(self):
        project = Path(self.tmp.name) / "Проект"
        project.mkdir(parents=True, exist_ok=True)
        room = make_room(project_dir=str(project))
        body = contract.build_request_body(room, "Проверь сайт студии. " * 10, "request", self.cfg)
        block = contract.build_contract_block(room, "request", self.cfg)
        self.assertLessEqual(len(block), card_budget.CONTRACT_SHORT_MAX,
                             "короткий устав в бюджете: %d" % len(block))
        path = contract.contract_file_path(room, self.cfg)
        self.assertTrue(Path(path).is_file(), "полный устав лёг файлом в папку проекта")
        self.assertIn(path, body, "карточка ссылается на файл устава")
        self.assertGreater(len(contract.full_contract_text(room, "request", self.cfg)), len(block),
                           "файл подробнее карточки")

    def test_contract_file_is_not_written_without_project_folder(self):
        room = make_room()                      # project_dir не задан
        self.assertEqual(contract.ensure_contract_file(room, self.cfg), "",
                         "без папки проекта ничего не пишем")
        body = contract.build_request_body(room, "Проверь сайт.", "request", self.cfg)
        self.assertIn(contract.CONTRACT_OPEN, body)

    def test_recent_unique_keeps_last_n_without_duplicates(self):
        items = ["сделали раздел 1", "сделали раздел 2", "сделали раздел 2",
                 "согласовали сроки", "проверили макеты"]
        got = card_budget.recent_unique(items, 3)
        self.assertEqual(got, ["сделали раздел 2", "согласовали сроки", "проверили макеты"],
                         "последние три по порядку, дубль выброшен")
        self.assertEqual(got[-1], "проверили макеты", "последняя запись — самая новая")
        self.assertEqual(len(set(got)), len(got), "без повторов")
        self.assertEqual(card_budget.recent_unique([], 3), [])
        self.assertEqual(card_budget.recent_unique(items, 0), [])

    def test_recent_unique_drops_repeated_tail(self):
        items = [{"text": "одно и то же"}, {"text": "одно и то же"}]
        got = card_budget.recent_unique(items, 5, key=lambda i: i["text"])
        self.assertEqual(len(got), 1, "дубль не занимает место в теле")

    def test_parent_digest_caps_each_parent_and_keeps_path(self):
        results = [{"id": "t_p1", "summary": "абзац. " * 900, "path": "C:/Projects/a/отчёт.md"},
                   {"id": "t_p2", "summary": "короткий итог"},
                   {"id": "t_p3", "summary": ""}]
        got = card_budget.parent_digest(results, cap=300)
        self.assertEqual(len(got), 2, "пустой результат пропущен")
        self.assertLessEqual(len(got[0]["text"]), 300, "резюме в потолке")
        self.assertTrue(got[0]["truncated"], "видно, что текст сокращён")
        self.assertEqual(got[0]["path"], "C:/Projects/a/отчёт.md", "путь к полному тексту рядом")
        self.assertFalse(got[1]["truncated"])

    def test_digest_does_not_repeat_the_same_message(self):
        detail = {"id": "t_1", "synthesis": {"state": "ready", "version": 2, "at": 1_700_000_000,
                                             "history": [{"version": 2, "at": 1_700_000_000}]},
                  "thread": [{"id": "c%d" % i, "at": 1_700_000_100 + i, "author": "mikhail",
                              "author_kind": "mikhail", "kind": "message",
                              "text": "Добавь сроки" if i % 2 else "Добавь сроки"}
                             for i in range(6)]}
        digest = contract.revision_digest(detail, make_room(), {}, None)
        self.assertEqual(digest["text"].count("Добавь сроки"), 1,
                         "шесть одинаковых сообщений — одна строка в дайджесте")


if __name__ == "__main__":
    unittest.main(verbosity=2)
