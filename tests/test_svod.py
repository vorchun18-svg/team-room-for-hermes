# -*- coding: utf-8 -*-
"""Пакет W17-S (SVOD): версии свода, дайджест «что появилось после версии», тело доработки.

Проверяем ровно то, что видит Михаил и что получает руководитель:
* подпись версии и «Что изменилось» — из реестра/`metadata.changes`, с запасным путём;
* дайджест ответов/уточнений/решений/шагов после выпуска версии — тот же расчёт,
  что использует предпросмотр в интерфейсе (`revisionDigest` в request-answer.js);
* тело карточки доработки содержит этот дайджест и требует `metadata.changes`.

Запуск из корня проекта:
    python -m unittest tests.test_svod -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import contract  # noqa: E402
from server import engine_tree  # noqa: E402

FIXTURE = ROOT / "fixtures" / "request_svod.json"


class FakeCfg:
    """Минимальный cfg: контракту нужны только три каталога (как в test_contract)."""

    def __init__(self, base: Path):
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"


def make_room(**over):
    room = {
        "id": "cafe",
        "title": "Лето",
        "board": "team",
        "tenant": "room:cafe",
        "orchestrator": "biz-director",
        "critic": "biz-critic",
        "members": [{"profile": "biz-lawyer", "role": "worker", "note": "юрист"}],
        "critic_pass": True,
    }
    room.update(over)
    return room


def load_detail() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class RevisionDigestTest(unittest.TestCase):
    """Дайджест после версии 3: 3 ответа, 3 уточнения, 2 решения, 1 убранный шаг."""

    def setUp(self):
        self.detail = load_detail()
        self.cutoff = int(self.detail["synthesis"]["at"])

    def test_counts_answers_and_notes_after_version(self):
        d = contract.revision_digest(self.detail)
        self.assertEqual(d["version"], 3)
        self.assertEqual(d["since_at"], self.cutoff)
        self.assertEqual(len(d["answers"]), 3, "три ответа Михаила после версии 3")
        self.assertEqual(len(d["notes"]), 3, "три уточнения в диалоге после версии 3")
        self.assertEqual(d["count"], 6, "интерфейс показывает то же число K")
        self.assertEqual([a["question_id"] for a in d["answers"]], ["q_1", "q_2", "q_3"])
        self.assertEqual(len(d["decisions"]), 2)
        self.assertEqual(len(d["steps"]), 1)

    def test_answers_before_version_are_not_included(self):
        detail = load_detail()
        # ответ до выпуска версии 3 в дайджест не попадает
        detail["open_questions"][0]["answered_at"] = self.cutoff - 10
        d = contract.revision_digest(detail)
        self.assertEqual(len(d["answers"]), 2)
        self.assertEqual(d["count"], 5)

    def test_text_is_readable_and_has_question_answer_pairs(self):
        text = contract.revision_digest(self.detail)["text"]
        self.assertIn("Ответы Михаила на вопросы — 3", text)
        self.assertIn("Вопрос: Спецификация партий", text)
        self.assertIn("Ответ: Фиксируем в приложении Б и коробы, и штуки", text)
        self.assertIn("Уточнения Михаила в диалоге — 3", text)
        self.assertIn("Решения Михаила — 2", text)
        self.assertIn("Шаги, которые Михаил согласовал, переместил или убрал — 1", text)
        self.assertIn("(принято)", text)
        self.assertIn("(убран)", text)

    def test_long_texts_are_capped_but_not_lost(self):
        detail = load_detail()
        long_text = "Длинное уточнение. " * 200          # ~3800 знаков
        detail["thread"].append({"id": "c_long", "at": self.cutoff + 5000, "author": "mikhail",
                                 "author_kind": "mikhail", "text": long_text, "kind": "message"})
        d = contract.revision_digest(detail)
        self.assertEqual(d["count"], 7)
        note = [n for n in d["notes"] if "Длинное уточнение" in n["text"]][0]
        self.assertLessEqual(len(note["text"]), contract.DIGEST_ITEM_MAX)
        self.assertTrue(note["text"].endswith("…"), "длинное уточнение обрезано с «…», а не пропало")

    def test_empty_digest_when_nothing_new(self):
        detail = load_detail()
        detail["open_questions"] = [q for q in detail["open_questions"] if not q.get("answered")]
        detail["thread"] = [m for m in detail["thread"] if m.get("author") != "mikhail"]
        detail["decisions"] = []
        detail["next_steps"] = [s for s in detail["next_steps"] if s.get("state") == "open"]
        d = contract.revision_digest(detail)
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["text"], "")

    def test_registry_fallback_when_questions_have_no_answer(self):
        """В рабочем пути (critic_loop) деталь приходит без блока `answers`: берём реестр."""
        detail = load_detail()
        for q in detail["open_questions"]:
            q["answered"] = False
            q["answer"] = None
            q["answered_at"] = None
        record = {"answers": {
            "q_1": {"text": "Фиксируем 24 штуки в коробе", "at": self.cutoff + 100, "status": "applied"},
            "q_4": {"text": "Старый ответ до версии", "at": self.cutoff - 100, "status": "applied"},
        }}
        d = contract.revision_digest(detail, record=record)
        self.assertEqual(len(d["answers"]), 1, "только ответ после версии")
        self.assertEqual(d["answers"][0]["answer"], "Фиксируем 24 штуки в коробе")
        self.assertEqual(d["answers"][0]["state_ru"], "учтено руководителем")

    def test_never_raises_on_garbage(self):
        for bad in (None, {}, {"synthesis": "нет"}, {"open_questions": "нет"},
                    {"synthesis": {"history": [None, 3]}, "thread": [None]}):
            d = contract.revision_digest(bad)
            self.assertIn("count", d)
            self.assertEqual(d["count"], 0)


class RevisionBodyTest(unittest.TestCase):
    """Тело карточки доработки = запрос + свод + замечание + дайджест + контракт."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-svod-")
        self.cfg = FakeCfg(Path(self.tmp.name))
        self.room = make_room()
        self.detail = load_detail()
        self.request = self.detail["text"]
        self.prev = self.detail["synthesis"]["text_md"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_body_contains_every_answer_from_digest(self):
        digest = contract.revision_digest(self.detail)
        body = contract.build_revision_body(self.room, self.request, self.prev, None,
                                            "Сократи срок уведомления и учти мои ответы",
                                            self.cfg, digest=digest)
        self.assertIn("<<<ПОСЛЕ ВЕРСИИ 3 (это уже произошло — учти ВСЁ)>>>", body)
        for answer in digest["answers"]:
            self.assertIn(answer["answer"][:60], body, "ответ Михаила попал в карточку")
        for note in digest["notes"]:
            self.assertIn(note["text"][:60], body)
        self.assertIn("<<<ЗАМЕЧАНИЕ МАКСИМА>>>", body)
        self.assertIn("<<<ПРЕДЫДУЩИЙ ОТВЕТ (дорабатывается)>>>", body)
        self.assertIn("metadata.changes", body, "руководителя просят перечислить изменения")
        self.assertIn("version_reason", body)
        self.assertIn("changes:", body, "контракт доработки знает поле changes")

    def test_preview_matches_body_counts(self):
        """Число K из предпросмотра — ровно столько пунктов уходит в карточку."""
        digest = contract.revision_digest(self.detail)
        body = contract.build_revision_body(self.room, self.request, self.prev, None, None,
                                            self.cfg, digest=digest)
        self.assertEqual(digest["count"], 6)
        self.assertEqual(body.count("Вопрос: "), 3)
        self.assertEqual(body.count("Уточнения Михаила в диалоге — 3"), 1)

    def test_body_without_digest_keeps_old_behaviour(self):
        body = contract.build_revision_body(self.room, self.request, self.prev, None, None, self.cfg)
        self.assertIn("Замечаний нет — перепроверь ответ", body)
        self.assertNotIn("ПОСЛЕ ВЕРСИИ", body)

    def test_body_only_digest_no_note(self):
        digest = contract.revision_digest(self.detail)
        body = contract.build_revision_body(self.room, self.request, self.prev, None, None,
                                            self.cfg, digest=digest)
        self.assertNotIn("Замечаний нет — перепроверь ответ", body, "дайджест заменяет «замечаний нет»")
        self.assertIn("Уточнения Михаила в диалоге", body)

    def test_long_note_300_chars_is_kept_whole(self):
        note = ("Сведи штрафы к одному правилу, добавь ответственность за простой линии розлива "
                "и сократи срок уведомления об изменении объёма партии с 90 до 30 дней; отдельно "
                "проверь, что приёмка по качеству не мешает переупаковке брака силами поставщика, "
                "и зафиксируй коэффициент пересчёта коробов в штуки в приложении Б.")
        self.assertGreater(len(note), 300)
        digest = contract.revision_digest(self.detail)
        body = contract.build_revision_body(self.room, self.request, self.prev, None, note,
                                            self.cfg, digest=digest)
        self.assertIn(note, body)

    def test_contract_mentions_changes_only_for_revision(self):
        revision = contract.build_revision_body(self.room, self.request, self.prev, None, "x", self.cfg)
        request_body = contract.build_request_body(self.room, self.request, "request", self.cfg)
        self.assertIn("changes:", revision)
        self.assertNotIn("version_reason", request_body)


class VersionMetaTest(unittest.TestCase):
    """`metadata.changes` → «Что изменилось»: формы значения и ограничения."""

    def test_accepts_list_of_strings_and_objects(self):
        self.assertEqual(engine_tree._changes_of({"changes": ["а", {"text": "б"}, "", 3]}), ["а", "б", "3"])

    def test_accepts_single_string(self):
        self.assertEqual(engine_tree._changes_of({"changes": "одно изменение"}), ["одно изменение"])

    def test_caps_items_and_length(self):
        many = ["пункт %d " % i + "х" * 400 for i in range(20)]
        out = engine_tree._changes_of({"changes": many})
        self.assertEqual(len(out), 10)
        self.assertLessEqual(len(out[0]), 300)

    def test_no_changes_is_empty(self):
        self.assertEqual(engine_tree._changes_of({}), [])
        self.assertEqual(engine_tree._changes_of(None), [])


class DigestWiringTest(unittest.TestCase):
    """Зависимость critic_loop от контракта: старый контракт без digest не ломает доработку."""

    def test_build_revision_body_without_digest_support(self):
        from server import critic_loop
        calls = []

        class Stub:
            @staticmethod
            def build_revision_body(room, text, answer, issues=None, user_note=None, cfg=None):
                calls.append("old")
                return "OLD-BODY"

        original = critic_loop._contract
        critic_loop._contract = Stub
        try:
            body = critic_loop._build_revision_body({}, "t", "a", None, "n", None, {"text": "d"})
        finally:
            critic_loop._contract = original
        self.assertEqual(body, "OLD-BODY")
        self.assertEqual(calls, ["old"])


if __name__ == "__main__":
    unittest.main()
