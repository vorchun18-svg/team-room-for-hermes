"""Тесты канонической нормализации metadata (`server/normalize.py`).

Legacy-образец — реальный завершённый run корня `t_605613e0` (доска `team`),
прочитанный один раз в режиме `mode=ro`; здесь он зафиксирован как константа,
чтобы тесты не зависели от живой БД.
"""
from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.normalize import (  # noqa: E402
    artifact_kind,
    canonical_phase_id,
    canonical_stage,
    empty_metadata,
    looks_legacy,
    normalize_metadata,
    parse_due,
    stable_id,
)

CANON_KEYS = {"next_steps", "open_questions", "decisions", "stage", "future_questions",
              "artifacts", "report", "verdict", "issues", "extra"}
ID_RE = re.compile(r"^(?:ns|q|d)_[0-9a-f]{8}(?:_\d+)?$")

# --- реальный legacy-формат t_605613e0 (структура ключей 1:1, текст сокращён) ---
LEGACY = {
    "artifacts_doc": r"C:\TeamRoom\Vault\Projects\Team\00-Director\Tasks\svod-leto-2026-10-27.md",
    "contradictions_flagged": [
        "VISION 300/нед ≠ 150/нед (18.09) ≠ Rev.2 (60–90)",
        "триггер R7 в Decisions.md устарел против Rev.2",
        "D19 «закрыт запуском» 24.09 ≠ blocked 32 дня",
    ],
    "next_steps": [
        "Ш1 Михаил (1–2 мин, 0 R$): снять блокер канала исходящих TG/WA — V1 бот @BotFather → "
        "токен в gateway / V2 QR Telegram Web / V3 отправка вручную; → Марина стартует поле D19",
        "Ш2 Антон (0 R$, без Михаила): ратификация TRADEMARK-GUIDE.md по LUKA-METHOD + "
        "чек-лист «ИП за один вечер» (ОКВЭД 10.71, НЕ добавлять 56.10)",
        "Ш3 Михаил (по чек-листу Ш2, 0 R$, ~1 ч): фактическая регистрация ИП + запросы "
        "свидетельство о регистрации и заключение о размещении по ALVARA-GUIDE",
        "Ш4 Марина+Михаил (0 R$): первый контакт с СЭС по 4 готовым PT-запросам",
        "Ш5 Илья → Глеб, Атлас — диф: «Лист решений Михаила» одним документом",
    ],
    "open_questions": [
        "Q1 Канал для D19: V1 (реком. Марины) / V2 / V3 + новый дедлайн (старый 05.10 истёк) — Михаил",
        "Q2 Подача в Роспатент «Лето»: кл.30 (440 R$) или 30+43 (880 R$ со скидкой для ИП)? — Михаил",
        "Q3 Фактический статус ИП / свидетельство о регистрации / заключение о размещении",
        "Q4 Утвердить kill-критерии D8 (<50 наборов/мес на 60-й день → ревизия)",
        "Q5 Бренд-пак: ник IG/TikTok, домен, финал лейаута, слоган — Михаил",
        "Q6 Шесть решений дайджеста 26.10 за Михаилом",
        "Q7 Единица мощности моти vs наборы (разброс 4× — пересчёт у Анны)",
    ],
    "spend": "0 R$, внешних сервисов нет",
    "stage": {
        "idea": "готов: VISION/брифы утверждены 14–16.09, карта решений D1–D20 отработана",
        "legal": "в работе → ждёт действия/решения Михаила: нормы/гайды готовы, факт ИП — "
                 "нет данных; 4 вопроса в СЭС PMF открыты",
        "brand": "в работе → ждёт решения: «Лето» утверждён 24.09, проверка в Роспатенте: свободно",
        "ops": "исследования готовы (рынок/каналы/B2B/GEO), поле D19 не начато",
        "packaging": "в работе: исследование готово (10 произв. городе), котировки не отправлены",
        "production": "не начат: кухня есть, замер смены 150/нед",
        "sales": "не начат: 0 продаж/заказов, гейт G1 не закрыт, валидация D19 = 0 разговоров",
    },
    "artifacts": [r"C:\TeamRoom\Vault\Projects\Team\00-Director\Tasks\svod-leto-2026-10-27.md"],
    "worker_session_id": "local-a47197cae3dec58245e58413adfd805306a3ed2eebc7f360834aac8ba824b97b",
}

LEGACY_SUMMARY = """# Картина по проекту «Лето»

Коротко: бренд и легализация почти закрыты, упаковка в работе.

## Следующие шаги
1. Это НЕ должно попасть в next_steps: в metadata они уже есть.
2. Второй пункт из прозы.

## Вопросы к тебе
- Вопрос из прозы, который не должен подмениться.
"""


class LegacyMetadataTest(unittest.TestCase):
    def setUp(self):
        self.meta = copy.deepcopy(LEGACY)
        self.out = normalize_metadata(LEGACY, LEGACY_SUMMARY)

    def test_shape_is_canonical(self):
        self.assertEqual(set(self.out), CANON_KEYS)
        self.assertEqual(set(self.out["stage"]), {"current", "phases"})

    def test_next_steps_from_strings(self):
        steps = self.out["next_steps"]
        self.assertEqual(len(steps), 5)
        self.assertEqual([s["text"] for s in steps], LEGACY["next_steps"])
        for step in steps:
            self.assertRegex(step["id"], ID_RE)
            self.assertIsNone(step["owner"])
            self.assertIsNone(step["due"])
            self.assertEqual(step["state"], "open")
        self.assertEqual(len({s["id"] for s in steps}), 5, "id шагов должны быть уникальны")

    def test_summary_fallback_not_used_when_key_present(self):
        texts = " ".join(s["text"] for s in self.out["next_steps"])
        self.assertNotIn("НЕ должно попасть", texts)

    def test_open_questions_from_strings(self):
        questions = self.out["open_questions"]
        self.assertEqual(len(questions), 7)
        self.assertEqual(questions[0]["text"], LEGACY["open_questions"][0])
        for q in questions:
            self.assertRegex(q["id"], ID_RE)
            self.assertEqual(q["for"], "mikhail")
            self.assertFalse(q["answered"])
        self.assertEqual(len({q["id"] for q in questions}), 7)

    def test_stage_legacy_map(self):
        stage = self.out["stage"]
        # W17-R §1 (B-8): своих этапов код не знает — id и порядок берутся из
        # metadata как есть («ops» больше не подменяется на «production»),
        # человеческие названия даёт комната (server/phase_book.py).
        self.assertEqual([p["id"] for p in stage["phases"]],
                         ["idea", "legal", "brand", "ops", "packaging", "production", "sales"])
        self.assertEqual([p["status"] for p in stage["phases"]],
                         ["done", "blocked", "blocked", "done", "active", "next", "next"])
        self.assertEqual(stage["current"], "legal")
        self.assertTrue(stage["phases"][1]["note"].startswith("в работе → ждёт действия"))
        self.assertEqual(stage["phases"][0]["title"], "idea")
        self.assertEqual(stage["phases"][5]["note"], LEGACY["stage"]["production"])

    def test_artifacts_from_paths(self):
        arts = self.out["artifacts"]
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["path"], LEGACY["artifacts"][0])
        self.assertEqual(arts[0]["kind"], "md")
        self.assertEqual(arts[0]["title"], "svod-leto-2026-10-27.md")

    def test_unknown_keys_go_to_extra(self):
        self.assertEqual(set(self.out["extra"]),
                         {"artifacts_doc", "contradictions_flagged", "spend", "worker_session_id"})
        self.assertEqual(self.out["extra"]["spend"], LEGACY["spend"])
        self.assertEqual(len(self.out["extra"]["contradictions_flagged"]), 3)

    def test_empty_sections(self):
        self.assertEqual(self.out["decisions"], [])
        self.assertEqual(self.out["future_questions"], [])
        self.assertIsNone(self.out["report"])
        self.assertIsNone(self.out["verdict"])
        self.assertEqual(self.out["issues"], [])

    def test_looks_legacy(self):
        self.assertTrue(looks_legacy(LEGACY))
        self.assertFalse(looks_legacy({}))

    def test_normalization_is_idempotent(self):
        once = normalize_metadata(LEGACY, LEGACY_SUMMARY)
        twice = normalize_metadata(once, LEGACY_SUMMARY)
        self.assertEqual(twice, once)
        self.assertFalse(looks_legacy(once))

    def test_input_not_mutated(self):
        self.assertEqual(LEGACY, self.meta)

    def test_ids_stable_between_representations(self):
        again = normalize_metadata(json.dumps(LEGACY, ensure_ascii=False))
        self.assertEqual([s["id"] for s in again["next_steps"]],
                         [s["id"] for s in self.out["next_steps"]])
        self.assertEqual([q["id"] for q in again["open_questions"]],
                         [q["id"] for q in self.out["open_questions"]])
        self.assertEqual(again["stage"], self.out["stage"])


class NewContractTest(unittest.TestCase):
    def setUp(self):
        self.meta = {
            "next_steps": [
                {"text": "Согласовать спеку упаковки", "owner": "biz-ops", "due": "2026-10-31"},
                {"text": "Запросить котировки", "owner": "biz-ops", "due": "10.11.2026"},
                {"text": "Решить по цене", "owner": "", "due": "к пятнице", "state": "done"},
            ],
            "open_questions": [
                {"text": "Держим цену R$ 42?", "for": "mikhail", "answered": False},
                {"text": "Кто пишет поставщикам?", "for": "biz-director", "answered": True},
                {"text": "Вопрос без адресата"},
            ],
            "decisions": [
                {"text": "Старт с набора из 6 моти", "why": "упаковка дешевле", "status": "accepted"},
                {"text": "Маржа не ниже 45%", "why": "доставка", "status": "proposed"},
                {"text": "Не поднимать цену выше 45", "why": "тест", "status": "rejected"},
            ],
            "stage": {"current": "packaging", "phases": [
                {"id": "idea", "title": "Идея и стратегия", "status": "done", "note": "утверждено"},
                {"id": "legal", "title": "Легализация", "status": "active", "note": "ИП подан"},
            ]},
            "future_questions": ["Стоит ли выходить в B2B?", {"text": "Нужен ли бренд без сахара?"}, ""],
            "artifacts": [
                {"path": r"C:\Vault\Лето\unit-economics.md", "title": "Юнит-экономика", "kind": "md"},
                r"C:\Vault\Лето\report.json",
                {"path": "", "title": "пустой"},
            ],
            "report": {"path": r"C:\TeamRoom\Team\team-room\data\reports\cafe\r-1\report.json"},
            "verdict": "revise",
            "issues": [
                {"where": "Срез 02-CFO", "problem": "Себестоимость без какао", "fix": "Пересчитать"},
                "Замечание строкой",
                42,
            ],
            "spend": "0 R$",
            "worker_session_id": "local-abc",
            " contradictions_flagged": ["ключ с ведущим пробелом"],
        }

    def setUp_out(self):
        return normalize_metadata(self.meta)

    def test_next_steps_objects(self):
        out = self.setUp_out()
        steps = out["next_steps"]
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["due"], "2026-10-31")
        self.assertEqual(steps[1]["due"], "2026-11-10")
        self.assertIsNone(steps[2]["due"])
        self.assertEqual(steps[2]["owner"], None)
        self.assertEqual(steps[2]["state"], "done")
        self.assertEqual(steps[0]["state"], "open")

    def test_open_questions_objects(self):
        questions = self.setUp_out()["open_questions"]
        self.assertEqual([q["for"] for q in questions], ["mikhail", "biz-director", "mikhail"])
        self.assertEqual([q["answered"] for q in questions], [False, True, False])

    def test_decisions(self):
        decisions = self.setUp_out()["decisions"]
        self.assertEqual([d["status"] for d in decisions], ["accepted", "proposed", "rejected"])
        self.assertEqual(decisions[0]["why"], "упаковка дешевле")
        for d in decisions:
            self.assertRegex(d["id"], ID_RE)

    def test_stage_keeps_only_declared_phases(self):
        # Список этапов пришёл в metadata — код его не дополняет до шести.
        stage = self.setUp_out()["stage"]
        self.assertEqual([p["id"] for p in stage["phases"]], ["idea", "legal"])
        self.assertEqual([p["title"] for p in stage["phases"]], ["Идея и стратегия", "Легализация"])
        self.assertEqual(stage["phases"][0]["status"], "done")
        self.assertEqual(stage["phases"][1]["status"], "active")
        self.assertEqual(stage["current"], "legal", "current вне списка — берём из статусов")

    def test_future_questions_and_artifacts(self):
        out = self.setUp_out()
        self.assertEqual(out["future_questions"],
                         ["Стоит ли выходить в B2B?", "Нужен ли бренд без сахара?"])
        self.assertEqual(len(out["artifacts"]), 2)
        self.assertEqual(out["artifacts"][0]["kind"], "md")
        self.assertEqual(out["artifacts"][1]["kind"], "json")
        self.assertEqual(out["artifacts"][1]["title"], "report.json")

    def test_report_verdict_issues(self):
        out = self.setUp_out()
        self.assertTrue(out["report"].endswith("report.json"))
        self.assertEqual(out["verdict"], "revise")
        self.assertEqual(len(out["issues"]), 2)
        self.assertEqual(out["issues"][0]["where"], "Срез 02-CFO")
        self.assertEqual(out["issues"][1], {"where": "", "problem": "Замечание строкой", "fix": ""})

    def test_extra_keys_stripped(self):
        out = self.setUp_out()
        self.assertEqual(set(out["extra"]), {"spend", "worker_session_id", "contradictions_flagged"})
        self.assertEqual(out["extra"]["contradictions_flagged"], ["ключ с ведущим пробелом"])
        self.assertFalse(any(k != k.strip() for k in out["extra"]))
        self.assertFalse(looks_legacy(self.meta))


class ToleranceTest(unittest.TestCase):
    GARBAGE = [
        None, "", "   ", "not json at all", 42, 3.14, True, ["a", "b"], (),
        {"next_steps": {"a": 1}}, {"next_steps": None}, {"open_questions": 5},
        {"stage": []}, {"stage": "жёстко"}, {"stage": {"финансы": "в работе"}},
        {"decisions": "одно решение"}, {"artifacts": [None, 1, "", {}]},
        {"issues": 2}, {"report": 0}, {"verdict": "?"}, {"future_questions": {"x": "y"}},
        {"next_steps": [{"owner": "biz-ops"}, "строка"]},
        {"stage": {"current": "неизвестная фаза", "phases": "нет"}},
        {"__proto__": {"a": 1}},
    ]

    def test_never_raises(self):
        for bad in self.GARBAGE:
            with self.subTest(bad=repr(bad)[:60]):
                out = normalize_metadata(bad)
                self.assertEqual(set(out), CANON_KEYS)
                for key in ("next_steps", "open_questions", "decisions", "future_questions",
                            "artifacts", "issues"):
                    self.assertIsInstance(out[key], list)
                self.assertIsInstance(out["stage"]["phases"], list)
                self.assertIsInstance(out["extra"], dict)

    def test_json_string_and_bytes(self):
        self.assertEqual(len(normalize_metadata('{"next_steps": ["шаг"]}')["next_steps"]), 1)
        self.assertEqual(len(normalize_metadata(b'{"next_steps": ["step"]}')["next_steps"]), 1)
        self.assertEqual(len(normalize_metadata('{"next_steps": ["шаг"]}'.encode("utf-8"))["next_steps"]), 1)
        self.assertEqual(normalize_metadata("[1,2,3]")["next_steps"], [])
        self.assertEqual(normalize_metadata('{"broken": ')["stage"]["current"], None)

    def test_stage_unknown_area_preserved_in_extra(self):
        # W17-R §1 (B-8): незнакомая область — это этап с её же названием,
        # а не «потерянный» ключ и не дополнение до шести канонических.
        out = normalize_metadata({"stage": {"финансы": "в работе", "legal": "готов"}})
        self.assertEqual([p["id"] for p in out["stage"]["phases"]], ["финансы", "legal"])
        self.assertEqual([p["title"] for p in out["stage"]["phases"]], ["финансы", "legal"])
        self.assertEqual(out["stage"]["phases"][0]["status"], "active")
        self.assertEqual(out["stage"]["phases"][1]["status"], "done")

    def test_duplicate_texts_get_unique_ids(self):
        out = normalize_metadata({"next_steps": ["Позвонить поставщику", "Позвонить поставщику"]})
        ids = [s["id"] for s in out["next_steps"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(ids[1].startswith(ids[0]))

    def test_empty_metadata_helper(self):
        out = empty_metadata()
        self.assertEqual(set(out), CANON_KEYS)
        self.assertEqual(out["stage"]["phases"], [])  # этапов по умолчанию нет
        self.assertEqual(normalize_metadata(None), out)


class MarkdownFallbackTest(unittest.TestCase):
    SUMMARY = """# Ответ

Короткий вводный текст.

## Следующие шаги
1. Снять блокер канала
2. Согласовать упаковку с Асей
   до пятницы, успеть к тиражу
- Проверить морозилку

## Вопросы к тебе
- Какой канал берём?
- Кто пишет поставщикам?

## Решения
- Стартуем с набора из 6 моти
**Будущее**
"""

    def test_sections(self):
        out = normalize_metadata({}, self.SUMMARY)
        self.assertEqual([s["text"] for s in out["next_steps"]],
                         ["Снять блокер канала", "Согласовать упаковку с Асей до пятницы, успеть к тиражу",
                          "Проверить морозилку"])
        self.assertEqual([q["text"] for q in out["open_questions"]],
                         ["Какой канал берём?", "Кто пишет поставщикам?"])
        self.assertEqual([d["text"] for d in out["decisions"]],
                         ["Стартуем с набора из 6 моти"])
        self.assertEqual(out["open_questions"][0]["for"], "mikhail")
        self.assertEqual(out["decisions"][0]["status"], "proposed")

    def test_bold_heading_and_next_step_english(self):
        summary = "**Next steps**\n- Prepare Роспатент filing\n\n**Дальше**\n- Собрать 15 предзаказов\n"
        out = normalize_metadata(None, summary)
        self.assertEqual([s["text"] for s in out["next_steps"]],
                         ["Prepare Роспатент filing", "Собрать 15 предзаказов"])

    def test_no_fallback_when_key_present_even_empty(self):
        out = normalize_metadata({"next_steps": []}, self.SUMMARY)
        self.assertEqual(out["next_steps"], [])
        self.assertEqual(len(out["open_questions"]), 2)  # вопроса в meta нет → фолбэк

    def test_bad_summary_is_ignored(self):
        for bad in (None, 42, b"bytes", ""):
            self.assertEqual(normalize_metadata(None, bad)["next_steps"], [])


class HelpersTest(unittest.TestCase):
    def test_parse_due(self):
        cases = {
            "2026-11-10": "2026-11-10",
            "2026-11-10T12:00:00Z": "2026-11-10",
            "10.11.2026": "2026-11-10",
            "1.2.2027": "2027-02-01",
            "31.02.2026": None,
            "10/11/2026": "2026-11-10",
            "10.11.26": None,
            "к пятнице": None,
            "10 ноября": None,
            "": None,
            None: None,
            1793171460: None,
            True: None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_due(raw), expected)

    def test_artifact_kind(self):
        self.assertEqual(artifact_kind(r"C:\x\a.MD"), "md")
        self.assertEqual(artifact_kind("a/b/report.json"), "json")
        self.assertEqual(artifact_kind("photo.JPEG"), "image")
        self.assertEqual(artifact_kind("archive.zip"), "other")
        self.assertEqual(artifact_kind("noext"), "other")

    def test_stable_id(self):
        self.assertEqual(stable_id("ns", "Шаг  один"), stable_id("ns", "  Шаг один  "))
        self.assertRegex(stable_id("q", "вопрос"), r"^q_[0-9a-f]{8}$")
        self.assertNotEqual(stable_id("ns", "a"), stable_id("q", "a"))

    def test_canonical_phase_id(self):
        # Своих алиасов этапов нет: id — стабильный слаг из того, что пришло.
        self.assertEqual(canonical_phase_id("production"), "production")
        self.assertEqual(canonical_phase_id("ops"), "ops")
        self.assertEqual(canonical_phase_id("Legal"), "legal")
        self.assertEqual(canonical_phase_id("юр-вопросы"), "юр-вопросы")
        self.assertEqual(canonical_phase_id("упаковка и этикетка"), "упаковка-и-этикетка")
        self.assertEqual(canonical_phase_id("неизвестно"), "неизвестно")
        self.assertIsNone(canonical_phase_id("")) 
        self.assertIsNone(canonical_phase_id(None))

    def test_canonical_stage_derives_current(self):
        stage = canonical_stage({"phases": [{"id": "idea", "status": "done"},
                                           {"id": "legal", "status": "blocked"}]})
        self.assertEqual(stage["current"], "legal")
        stage = canonical_stage({"phases": [{"id": "idea", "status": "done"}]})
        self.assertEqual(stage["current"], "idea")
        stage = canonical_stage({"phases": []})
        self.assertIsNone(stage["current"])
        self.assertIsNone(canonical_stage(None)["current"])


if __name__ == "__main__":
    unittest.main()
