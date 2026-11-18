# -*- coding: utf-8 -*-
"""W21-Q1 ANSWERS: «Пояснить» без текста (B-44) и подписи ответов (B-45).

Что проверяется (docs/WAVE21-RULES.md §2, пакет Q1; замечания B-44, B-45):
  * кнопка «Пояснить» работает БЕЗ текста: ``need_explanation:true`` без ``text``
    → 200, статус ``waiting_explanation``, вопрос остаётся открытым, в ветку идёт
    стандартная просьба ``DEFAULT_EXPLAIN_TEXT``, руководитель получает карточку
    «Уточнение» с контрактом реакции;
  * с текстом — прежнее поведение («поясни вот это: …»);
  * «Отправить» с пустым полем — по-прежнему отказ (400 «Нужен текст»);
  * повторное нажатие «Пояснить» в окне 120 с не создаёт вторую просьбу
    (тот же ключ идемпотентности, что у остальных действий);
  * ``summary()`` отдаёт спокойную подпись ``resend_hint`` без слов «без реакции»,
    а ``can_resend`` оставлен как есть (совместимость; видимость кнопки решает клиент).

Песочница — только ``tests.helpers.Sandbox`` (см. ``tests/test_answers.py``); CLI
Hermes подменён shim'ом, реальные доска и конфиг сверяются до/после.

    python -m unittest tests.test_answers_explain -v

# SHIM-OK: CLI Hermes подменён CliShim; модели не запускаются.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.answers as answers_mod     # noqa: E402
import server.idempotency as idem_mod    # noqa: E402
from tests.test_answers import Q_ASK, QUESTION, _Base   # noqa: E402

DEFAULT = answers_mod.DEFAULT_EXPLAIN_TEXT


class EmptyExplanationTests(_Base):
    """B-44: «Пояснить» с пустым полем — просьба уходит, ввода от человека нет."""

    def setUp(self):
        super().setUp()
        # Окно идемпотентности — общее на процесс: чистим, чтобы тесты не влияли.
        idem_mod.reset()

    def test_empty_explanation_is_accepted_and_question_stays_open(self):
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                          need_explanation=True)
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["question"]["answered"], "просьба — не ответ")
        self.assertEqual(payload["question"]["answer_state"], "waiting_explanation")
        self.assertEqual(payload["answer"]["state"], "waiting_explanation")
        self.assertTrue(payload["answer"]["needs_explanation"])
        # Вопрос по-прежнему в «Нужен ваш ответ», ответа у него нет.
        question = self.question_of(rid)
        self.assertFalse(question["answered"])
        self.assertTrue(question["needs_explanation"])
        self.assertEqual(question["answer_state"], "waiting_explanation")

    def test_empty_explanation_uses_standard_request(self):
        """Пустой текст → стандартная просьба, а не пустая строка."""
        self.assertTrue(DEFAULT)
        self.assertIn("простыми словами", DEFAULT)
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        self.actions(rid, action="answer", question_id=qid, need_explanation=True)
        stored = self.registry_answers(rid)[qid]
        self.assertEqual(stored["text"], DEFAULT)
        self.assertEqual(stored["status"], "waiting_explanation")
        self.assertFalse(stored["answered"])
        self.assertEqual(stored["question_text"], Q_ASK)
        # Ветка: метка просьбы + стандартная формулировка.
        texts = self.shim.comment_texts()
        marks = [t for t in texts if t.startswith(answers_mod.EXPLAIN_MARK)]
        self.assertEqual(len(marks), 1, texts)
        self.assertIn(qid, marks[0])
        self.assertIn(DEFAULT, marks[0])

    def test_empty_explanation_creates_leader_card(self):
        """Руководителю — та же карточка «Уточнение», что у пояснения с текстом."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        self.actions(rid, action="answer", question_id=qid, need_explanation=True)
        creates = self.shim.creates()
        self.assertEqual(len(creates), 1, creates)
        body = self.shim.bodies[0]
        self.assertIn("ПРОСИТ ПОЯСНЕНИЕ", body)
        self.assertIn("ОСТАЁТСЯ ОТКРЫТЫМ", body)
        self.assertIn(qid, body)
        self.assertIn(DEFAULT, body)

    def test_explanation_with_text_keeps_own_words(self):
        """С текстом — как раньше: просьба в рамках написанного уточнения."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        own = "уточни, что значит «вручную» — свой менеджер или сам через форму"
        status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                          text=own, need_explanation=True)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.registry_answers(rid)[qid]["text"], own)
        marks = [t for t in self.shim.comment_texts() if t.startswith(answers_mod.EXPLAIN_MARK)]
        self.assertTrue(marks and marks[0].endswith(own), marks)
        self.assertNotIn(DEFAULT, marks[0])

    def test_send_with_empty_text_is_still_refused(self):
        """«Отправить» с пустым полем — отказ, никаких карточек и комментариев."""
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        status, payload, _ = self.actions(rid, action="answer", question_id=qid)
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.shim.creates(), [], "руководителю ничего не ушло")
        self.assertEqual(self.shim.comment_texts(), [], "в ветку ничего не ушло")
        self.assertNotIn(qid, self.registry_answers(rid))

    def test_second_press_within_window_does_not_repeat_request(self):
        """B-44: двойное нажатие — одна просьба (ключ идемпотентности, 120 с)."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        first_status, first, _ = self.actions(rid, action="answer", question_id=qid,
                                              need_explanation=True)
        second_status, second, _ = self.actions(rid, action="answer", question_id=qid,
                                                need_explanation=True)
        self.assertEqual(first_status, 200, first)
        self.assertEqual(second_status, 200, second)
        self.assertTrue(second.get("duplicate"), second)
        self.assertFalse(first.get("duplicate"))
        self.assertEqual(len(self.shim.creates()), 1, "вторая карточка не создана")
        self.assertEqual(len([t for t in self.shim.comment_texts()
                              if t.startswith(answers_mod.EXPLAIN_MARK)]), 1,
                         "второго комментария в ветке нет")

    def test_different_words_are_a_new_request(self):
        """Другой текст — другая просьба (ключ считается по тексту)."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        self.actions(rid, action="answer", question_id=qid, need_explanation=True)
        status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                          text="и ещё: сколько это стоит?", need_explanation=True)
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload.get("duplicate"))
        self.assertEqual(len(self.shim.creates()), 2, payload)

    def test_explanation_is_not_counted_as_answer_to_resend(self):
        """Просьба о пояснении — не накопленный ответ: досылать нечего."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        self.actions(rid, action="answer", question_id=qid, need_explanation=True)
        status, payload, _ = self.actions(rid, action="answers_resend")
        self.assertEqual(status, 409, payload)
        detail = ((payload.get("error") or {}).get("detail") if isinstance(payload.get("error"), dict)
                  else None) or payload
        self.assertEqual(detail.get("reason"), "nothing_to_resend", payload)

    def test_can_answer_after_empty_explanation(self):
        """После пояснения вопрос по-прежнему можно закрыть ответом."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        self.actions(rid, action="answer", question_id=qid, need_explanation=True)
        status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                          text="Вручную: менеджер сам заводит заказ")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["question"]["answered"])
        self.assertEqual(payload["question"]["answer_state"], "sent")

    # --- помощник ---------------------------------------------------------
    def registry_answers(self, rid: str) -> dict:
        from server import registry
        return registry.get(self.cfg, "cafe", rid).get("answers") or {}


class ExplanationGuardsTests(_Base):
    """W21-REV2: мусорные типы, разные вопросы и доставка состояния до клиента."""

    def setUp(self):
        super().setUp()
        idem_mod.reset()

    def registry_answers(self, rid: str) -> dict:
        from server import registry
        return registry.get(self.cfg, "cafe", rid).get("answers") or {}

    def test_garbage_text_type_is_refused(self):
        """`text: 123` + «Пояснить» — 400, а не молчаливая стандартная просьба."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        for junk in (123, 0, ["поясни"], {"a": 1}, True):
            status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                              text=junk, need_explanation=True)
            self.assertEqual(status, 400, "text=%r принят молча: %s" % (junk, payload))
            self.assertEqual(((payload.get("error") or {}).get("detail") or {}).get("field"),
                             "text", payload)
        self.assertEqual(self.shim.creates(), [], "карточка руководителю не создана")
        self.assertNotIn(qid, self.registry_answers(rid), "в реестр ничего не записано")

    def test_garbage_need_explanation_type_is_refused(self):
        """`need_explanation: "true"` — не булево: 400 (иначе «Пояснить» стал бы ответом)."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        for junk in ("true", 1, "да", [True]):
            status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                              text="поясни", need_explanation=junk)
            self.assertEqual(status, 400, "need_explanation=%r принят: %s" % (junk, payload))
        self.assertNotIn(qid, self.registry_answers(rid))

    def test_two_different_questions_are_not_mixed(self):
        """Просьба о пояснении у двух разных вопросов — ДВЕ просьбы, не «дубль»."""
        rid = self.rid(questions=[Q_ASK, QUESTION])
        questions = self.detail(rid)["open_questions"]
        self.assertGreaterEqual(len(questions), 2, questions)
        first, second = questions[0]["id"], questions[1]["id"]
        s1, p1, _ = self.actions(rid, action="answer", question_id=first, need_explanation=True)
        s2, p2, _ = self.actions(rid, action="answer", question_id=second, need_explanation=True)
        self.assertEqual((s1, s2), (200, 200), (p1, p2))
        self.assertFalse(p1.get("duplicate"))
        self.assertFalse(p2.get("duplicate"), "второй вопрос получил ответ первого")
        stored = self.registry_answers(rid)
        self.assertEqual(set(stored), {first, second})
        self.assertEqual(len(self.shim.creates()), 2, "по карточке на каждый вопрос")

    def test_waiting_explanation_reaches_page_and_counters(self):
        """Состояние `waiting_explanation` доходит до страницы и до счётчиков."""
        rid = self.rid(question=Q_ASK)
        qid = self.question_of(rid)["id"]
        self.actions(rid, action="answer", question_id=qid, need_explanation=True)
        detail = self.detail(rid)
        question = [q for q in detail["open_questions"] if q["id"] == qid][0]
        self.assertEqual(question["answer_state"], "waiting_explanation")
        self.assertTrue(question["needs_explanation"])
        self.assertFalse(question["answered"])
        self.assertTrue(question["needs_answer"], "вопрос остаётся в «Нужен ваш ответ»")
        summary = detail["answers"]["summary"]
        self.assertEqual(summary["waiting_explanation"], 1)
        self.assertEqual(summary["states"].get("waiting_explanation"), 1)
        self.assertIn("пояснения", detail["answers"]["waiting_label"])
        item = [i for i in detail["answers"]["items"] if i["question_id"] == qid][0]
        self.assertTrue(item["needs_explanation"])
        self.assertEqual(item["state"], "waiting_explanation")


class ExplanationTextTests(unittest.TestCase):
    """Чистые функции текста: одна формулировка на сервер, ветку и демо."""

    def test_default_request_is_used_when_text_is_empty(self):
        mark = answers_mod.explanation_comment("q_1", Q_ASK, "")
        self.assertTrue(mark.startswith(answers_mod.EXPLAIN_MARK))
        self.assertIn(DEFAULT, mark)
        self.assertEqual(mark, answers_mod.explanation_comment("q_1", Q_ASK, None))

    def test_default_request_is_not_added_to_a_real_text(self):
        mark = answers_mod.explanation_comment("q_1", Q_ASK, "поясни сроки")
        self.assertTrue(mark.endswith("поясни сроки"), mark)
        self.assertNotIn(DEFAULT, mark)

    def test_answer_mark_is_untouched(self):
        mark = answers_mod.answer_comment("q_1", QUESTION, "Да, добавляем")
        self.assertTrue(mark.startswith(answers_mod.ANSWER_MARK))
        self.assertTrue(mark.endswith("Да, добавляем"))


class SummaryTextTests(unittest.TestCase):
    """B-45: в подписях нет слов «без реакции»; `can_resend` совместим."""

    def test_summary_has_calm_hint_without_bez_reakcii(self):
        summary = answers_mod.summary([
            {"id": "q_1", "answer_state": "sent", "answered": True},
            {"id": "q_2", "answer_state": "waiting_explanation"},
            {"id": "q_3", "answer_state": "open"},
        ], [])
        self.assertTrue(summary["can_resend"], "совместимость: как раньше")
        self.assertEqual(summary["resend_pending"], 1,
                         "просьба о пояснении не считается накопленным ответом")
        self.assertEqual(summary["waiting_explanation"], 1)
        self.assertIn("ближайшем обновлении свода", summary["resend_hint"])
        for key in ("resend_hint", "resend_hint_soon", "resend_label"):
            self.assertNotIn("без реакции", summary[key], key)

    def test_summary_without_pending_hides_resend(self):
        summary = answers_mod.summary([{"id": "q_1", "answer_state": "applied"}], [])
        self.assertFalse(summary["can_resend"])
        self.assertEqual(summary["resend_pending"], 0)


if __name__ == "__main__":
    unittest.main()
