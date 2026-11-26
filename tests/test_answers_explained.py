# -*- coding: utf-8 -*-
"""W30-A §5: «пояснено — выберите ответ» (состояние ``explained``).

Что проверяется (docs/WAVE30-PACKAGES.md §1, пакет A п.5):

* пояснение руководителя ``[Руководитель · пояснение по вопросу <id>] <текст>``
  поднимается в поле вопроса ``explanation`` (и ``explained_at``/``explained_by``);
* пока ответа нет, ``answer_state = explained``, подпись «пояснено — выберите
  ответ», кнопки активны (``needs_answer`` true) — работа не встаёт;
* устаревшая подсказка «вопрос ждёт, пока руководитель не объяснит» исчезает:
  ``needs_explanation`` false, ``answer_hint`` говорит о выборе ответа;
* ответ на пояснённый вопрос идёт обычным путём (``sent``), пояснение остаётся;
* «Ещё пояснить» на пояснённом вопросе снова даёт ``waiting_explanation``;
* дубль-вариант метки ``[Руководитель · пояснение]`` тоже понимается.

Данные вымышленные (химчистка). Песочница — `tests.helpers.Sandbox`;
CLI Hermes подменён shim'ом.

    python -m unittest tests.test_answers_explained -v
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.answers as answers_mod     # noqa: E402
import server.questions as q_mod          # noqa: E402
import server.stale as stale_mod          # noqa: E402
from tests.test_questions import QuestionsApiTests  # noqa: E402

QID = "q_cafe1234"
Q_TEXT = "Стираем деликатные вещи сами или отдаём подрядчику?"
EXPLAINED_TEXT = ("Подрядчик — это отдельная прачечная, которая берёт наши заказы. "
                  "Они дешевле на 12 за килограмм, но везут два дня.")


def _thread(text, at=100, author="team-director", kind="agent"):
    return {"id": "c_%d" % at, "author": author, "author_kind": kind, "at": at, "text": text}


class ExplainedStateTests(unittest.TestCase):
    """Состояние и подписи — чистые функции."""

    def test_state_is_declared_with_russian_labels(self):
        self.assertIn("explained", answers_mod.STATES)
        self.assertEqual(answers_mod.STATE_RU["explained"], "пояснено — выберите ответ")
        self.assertIn("выберите ответ", answers_mod.STATE_HINT["explained"])
        self.assertNotIn("пока руководитель", answers_mod.STATE_HINT["explained"])

    def test_apply_question_sets_explained(self):
        question = {"id": QID, "text": Q_TEXT, "for": "mikhail"}
        answers_mod.apply_question(question, {}, None,
                                   explanation={"text": EXPLAINED_TEXT, "at": 100,
                                                "author": "team-director"})
        self.assertEqual(question["answer_state"], "explained")
        self.assertEqual(question["explanation"], EXPLAINED_TEXT)
        self.assertEqual(question["explained_at"], 100)
        self.assertFalse(question["answered"])
        self.assertTrue(question["needs_answer"], "пояснённый вопрос всё ещё ждёт ответа")
        self.assertFalse(question["needs_explanation"])
        self.assertIn("выберите", question["answer_hint"])
        self.assertEqual(stale_mod.question_state(question), "open", "пункт не считается закрытым")
        self.assertTrue(stale_mod.needs_you("question", question))

    def test_answer_keeps_the_explanation(self):
        question = {"id": QID, "text": Q_TEXT, "for": "mikhail"}
        record = {"text": "Отдаём подрядчику", "at": 200, "answered": True, "status": "sent"}
        answers_mod.apply_question(question, record, None,
                                   explanation={"text": EXPLAINED_TEXT, "at": 100})
        self.assertEqual(question["answer_state"], "sent")
        self.assertEqual(question["explanation"], EXPLAINED_TEXT)
        self.assertTrue(question["answered"])

    def test_without_explanation_nothing_changes(self):
        question = {"id": QID, "text": Q_TEXT, "for": "mikhail"}
        answers_mod.apply_question(question, {}, None)
        self.assertEqual(question["answer_state"], "open")
        self.assertIsNone(question["explanation"])

    def test_plain_text_question_stays_open_until_explained(self):
        question = {"text": Q_TEXT, "for": "mikhail"}
        answers_mod.apply_question(question, {}, None)
        self.assertEqual(question["answer_label"], "ждёт вашего ответа")


class AliasTests(unittest.TestCase):
    """Варианты метки пояснения и мусор в ленте."""

    def test_short_mark_without_question_word(self):
        thread = [_thread("[Руководитель · пояснение] Подрядчик — отдельная прачечная.")]
        # короткая метка не несёт id вопроса: привязать не к чему, но и падать нельзя
        self.assertEqual(answers_mod.explanations_from_thread(thread), {})

    def test_mark_inside_longer_text_is_ignored(self):
        thread = [_thread("Итог: [Руководитель · пояснение по вопросу %s] не метка в начале" % QID)]
        self.assertEqual(answers_mod.explanations_from_thread(thread), {})

    def test_help_message_from_human_is_ignored(self):
        thread = [_thread("Поясни по вопросу %s, пожалуйста" % QID, author="mikhail", kind="user")]
        self.assertEqual(answers_mod.explanations_from_thread(thread), {})

    def test_explain_comment_builder_is_round_trip(self):
        mark = answers_mod.explain_comment(QID, Q_TEXT, EXPLAINED_TEXT)
        found = answers_mod.explanations_from_thread([_thread(mark)])
        self.assertEqual(found[QID]["text"], EXPLAINED_TEXT)


class ExplainedApiTests(QuestionsApiTests):
    """Через маршруты: пояснение приходит из ленты, ответ затем идёт обычным путём."""

    def _explained_detail(self):
        rid = self._request([self._ask()])
        room, rec = self.app._find_record(rid)
        detail = self.app._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                                  rec.get("answers"))
        qid = detail["open_questions"][0]["id"]
        detail["thread"] = [_thread(answers_mod.explain_comment(qid, detail["open_questions"][0]["text"],
                                                               EXPLAINED_TEXT),
                                    at=int(time.time()))]
        self.app._answers_view(room, rec, detail)
        return rid, qid, detail

    def test_explained_question_keeps_buttons_alive(self):
        _rid, _qid, detail = self._explained_detail()
        question = detail["open_questions"][0]
        self.assertEqual(question["answer_state"], "explained")
        self.assertTrue(question["needs_answer"])
        self.assertEqual(question["answer_label"], "пояснено — выберите ответ")
        self.assertEqual(detail["questions_summary"]["explained"], 1)
        self.assertEqual(question["options"][0]["id"], "o1")

    def test_answer_after_explanation_is_regular_answer(self):
        rid, qid, _detail = self._explained_detail()
        status, payload, _ = self.actions(rid, action="answer_option", question_id=qid, option_id="o1")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["question"]["answered"])
        self.assertEqual(payload["answer"]["state"], "sent")
        self.assertEqual(self.detail(rid)["questions_summary"]["open"], 0)

    def test_explain_more_after_explanation(self):
        rid, qid, _detail = self._explained_detail()
        status, payload, _ = self.actions(rid, action="explain_more", question_id=qid,
                                          text="Всё равно не понял, что такое подрядчик")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answer"]["state"], "waiting_explanation")
        question = self.detail(rid)["open_questions"][0]
        self.assertEqual(question["answer_state"], "waiting_explanation")
        self.assertTrue(question["needs_explanation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
