# -*- coding: utf-8 -*-
"""W30-A: вопросы человеку — язык, форма, темы, дедупликация (контракт §1, §4).

Что проверяется (docs/WAVE30-PACKAGES.md, пакет A):

* :mod:`server.questions` — валидатор «понятно неспециалисту» (голые коды
  ``D19``/``ISIC``/``56.10``, отсутствие «зачем»/«откуда взялось», объём),
  устойчивый ``topic_key``, схлопывание смысловых дублей и потолок блокирующих;
* реестр закрытых тем комнаты (``data/state/<room>/question_topics.json``):
  «Не актуально» → тема записана, строка «не спрашивать об этом» уходит в
  контракт руководителя, новый вопрос той же темы скрывается как ``closed_topic``;
* ``GET /api/requests/{id}`` отдаёт ``why/context/options/blocking/plain_ok/
  plain_issues/topic_key``, ``questions_summary`` и ``stage_hint``; дубли не в
  основном списке, а в ``hidden_duplicate_questions``;
* действия вопроса: ``answer_option`` (подпись варианта + последствие уходят в
  ветку), ``answer_custom``, ``explain_more``, ``postpone``;
* пояснение руководителя ``[Руководитель · пояснение по вопросу <id>]`` —
  ``explanation`` под вопросом и состояние ``explained``;
* нейтральный аналог живой задачи (52 вопроса) схлопывается по темам без ложных
  склеек — `fixtures/w30_questions_laundry.json`.

Данные вымышленные (химчистка, грузоперевозки). Песочница — `tests.helpers.Sandbox`,
CLI Hermes подменён shim'ом; реальная доска и `%LOCALAPPDATA%\\hermes` не трогаются.

    python -m unittest tests.test_questions -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.answers as answers_mod      # noqa: E402
import server.contract as contract_mod    # noqa: E402
import server.questions as q_mod          # noqa: E402
import server.registry as registry        # noqa: E402
from tests.test_answers import _Base, _record   # noqa: E402

ROOM = "cafe"      # комната песочницы (`rooms.seed_defaults` создаёт cafe/fn)
FIXTURE = ROOT / "fixtures" / "w30_questions_laundry.json"

GOOD_WHY = "От этого зависит, сколько людей ставим на первую неделю."
GOOD_CONTEXT = ("Курьер один, район небольшой. На прошлой неделе 18 заказов, "
                "из них 12 — до обеда. Значит, вопрос про смену, а не про число людей.")
Q_DELIVERY = "Доставка на старте: только самовывоз или сразу курьер по городу?"
Q_COURIER = "Смена курьера: один человек на весь день или два по половине?"
Q_CODE = "D19: отправляем 5–10 сообщений вручную по готовым текстам?"


class _FakeCfg:
    """Минимальный cfg для реестра тем: нужен только ``state_dir``."""

    def __init__(self, base: Path):
        self.state_dir = base / "state"


# --------------------------------------------------------------- чистые функции ---

class PlainLanguageTests(unittest.TestCase):
    """§1a: «понятно человеку» — валидатор без вызова моделей."""

    def test_bare_code_is_flagged(self):
        ok, issues = q_mod.plain_check(Q_CODE, GOOD_WHY, GOOD_CONTEXT)
        self.assertFalse(ok, "вопрос с кодом D19 не может быть plain_ok")
        self.assertTrue(any("D19" in item for item in issues), issues)

    def test_latin_abbreviation_without_expansion_is_flagged(self):
        ok, issues = q_mod.plain_check("Оформляем ISIC до первой партии?", GOOD_WHY, GOOD_CONTEXT)
        self.assertFalse(ok)
        self.assertTrue(any("ISIC" in item for item in issues), issues)

    def test_abbreviation_with_expansion_in_brackets_is_fine(self):
        text = "Оформляем микропредприятие (VAT) до первой партии или после?"
        ok, issues = q_mod.plain_check(text, GOOD_WHY, GOOD_CONTEXT)
        self.assertTrue(ok, issues)

    def test_classifier_number_is_flagged(self):
        ok, issues = q_mod.plain_check("Тариф 9601-0/02 подходит?", GOOD_WHY, GOOD_CONTEXT)
        self.assertFalse(ok)
        self.assertTrue(any("9601-0/02" in item for item in issues), issues)

    def test_missing_why_and_context_is_flagged(self):
        ok, issues = q_mod.plain_check("Ставим цену 210 за килограмм?", None, None)
        self.assertFalse(ok)
        self.assertTrue(any("зачем" in item for item in issues), issues)
        self.assertTrue(any("откуда" in item for item in issues), issues)

    def test_good_question_passes(self):
        text = "Ставим цену 210 за килограмм обычной стирки или 240?"
        ok, issues = q_mod.plain_check(text, GOOD_WHY, GOOD_CONTEXT)
        self.assertTrue(ok, issues)

    def test_long_question_is_flagged(self):
        ok, issues = q_mod.plain_check("Цена? " + "очень длинное объяснение " * 30, GOOD_WHY, GOOD_CONTEXT)
        self.assertFalse(ok)
        self.assertTrue(any("Слишком длинный" in item for item in issues), issues)


class TopicTests(unittest.TestCase):
    """§4: устойчивая тема и смысловые дубли (без моделей)."""

    def test_topic_key_comes_from_the_head(self):
        self.assertEqual(q_mod.topic_key(Q_DELIVERY), "доста")
        self.assertEqual(q_mod.topic_key("Упаковка заказов: пакет или бокс?"), "упако")

    def test_same_nick_is_the_same_topic(self):
        left = "Кто создал @laundry24 и есть ли доступ к аккаунту?"
        right = "Кто владелец @laundry24 и есть ли доступ с двухфакторной защитой?"
        self.assertTrue(q_mod.is_same_topic(left, right))

    def test_rewording_of_one_question_is_a_duplicate(self):
        left = "Цена за килограмм: 210 или 240 за обычную стирку?"
        right = "Цена за килограмм: 210 рублей или 240?"
        self.assertTrue(q_mod.is_same_topic(left, right))

    def test_different_topics_are_not_duplicates(self):
        self.assertFalse(q_mod.is_same_topic(Q_DELIVERY, Q_COURIER))
        self.assertFalse(q_mod.is_same_topic(
            "Сколько стоит химчистка пуховика?",
            "Кто ведёт реестр клиентов — администратор или я сам?"))

    def test_shared_abbreviation_alone_is_not_a_duplicate(self):
        """W30-FIX2: общее сокращение сферы (VAT) — не имя темы, а термин.

        На живой доске склейка по слову «VAT» спрятала от человека живой вопрос
        (статус регистрации) за другим (выбор класса подачи): отвечая на один,
        руководитель не получал ответа на другой. Тот же класс дефекта проверяем
        здесь нейтрально: разные вопросы про микропредприятие (VAT) не дубли.
        """
        left = ("Фактический статус регистрации микропредприятия (VAT) и справка об "
                "отсутствии долгов — данных в папке нет")
        right = "Подача заявки на знак: класс 30 или 30+43 (скидка для микропредприятия (VAT))?"
        self.assertFalse(q_mod.is_same_topic(left, right))
        # а переформулировка ОДНОГО вопроса про VAT по-прежнему дубль (по словам)
        again = "Есть ли справка об отсутствии долгов у микропредприятия (VAT)?"
        self.assertTrue(q_mod.is_same_topic(left, again))

    def test_dedupe_keeps_order_and_marks_duplicates(self):
        questions = [
            {"id": "q_a", "text": Q_DELIVERY},
            {"id": "q_b", "text": "Как доставляем на старте — самовывоз или курьер по городу?"},
            {"id": "q_c", "text": Q_COURIER},
        ]
        visible, hidden = q_mod.dedupe([dict(item) for item in questions])
        self.assertEqual([item["id"] for item in visible], ["q_a", "q_c"])
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0]["id"], "q_b")
        self.assertEqual(hidden[0]["duplicate_of"], "q_a")
        self.assertEqual(hidden[0]["topic_key"], visible[0]["topic_key"])

    def test_blocking_cap_keeps_three(self):
        questions = [{"id": "q_%d" % i, "text": "Вопрос номер %d про прайс?" % i,
                      "blocking": True} for i in range(1, 6)]
        kept = q_mod.cap_blocking(questions)
        self.assertEqual(kept, q_mod.MAX_BLOCKING)
        self.assertEqual(sum(1 for item in questions if item["blocking"]), 3)
        rest = [item for item in questions if not item["blocking"]]
        self.assertEqual(len(rest), 2)
        self.assertTrue(all(item.get("decision_package") for item in rest))

    def test_summary_counts(self):
        visible = [{"id": "q_1", "blocking": True, "answer_state": "open"},
                   {"id": "q_2", "blocking": False, "answer_state": "postponed"},
                   {"id": "q_3", "answer_state": "explained"}]
        result = q_mod.summary(visible, [{"id": "q_4", "duplicate_of": "q_1"}], {"доста": {}})
        self.assertEqual(result["open"], 2)
        self.assertEqual(result["blocking"], 1)
        self.assertEqual(result["hidden_duplicates"], 1)
        self.assertEqual(result["closed_topics"], 1)
        self.assertEqual(result["explained"], 1)
        self.assertEqual(result["postponed"], 1)


class ClosedTopicsTests(unittest.TestCase):
    """§4: «Не актуально» → закрытая тема комнаты и строка «не спрашивать»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-w30a-topics-")
        self.cfg = _FakeCfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_close_and_reopen_roundtrip(self):
        question = {"id": "q_1", "text": Q_DELIVERY}
        entry = q_mod.close_topic(self.cfg, ROOM, question, reason="Решили позже")
        self.assertEqual(entry["topic_key"], q_mod.topic_key(Q_DELIVERY))
        stored = q_mod.read_topics(self.cfg, ROOM)
        self.assertIn(entry["topic_key"], stored)
        self.assertIsNotNone(q_mod.closed_line(stored))
        self.assertTrue(q_mod.reopen_topic(self.cfg, ROOM, question))
        self.assertEqual(q_mod.read_topics(self.cfg, ROOM), {})

    def test_new_question_of_closed_topic_is_hidden(self):
        q_mod.close_topic(self.cfg, ROOM, {"id": "q_old", "text": Q_DELIVERY})
        fresh = {"id": "q_new", "text": "Доставка на старте: самовывоз или курьер по городу, как лучше?"}
        visible, hidden = q_mod.dedupe([fresh], q_mod.read_topics(self.cfg, ROOM))
        self.assertEqual(visible, [])
        self.assertEqual(hidden[0]["duplicate_of"], "q_old")
        self.assertEqual(hidden[0]["duplicate_reason"], "closed_topic")

    def test_closed_line_is_limited(self):
        for index in range(30):
            q_mod.close_topic(self.cfg, ROOM, {"id": "q_%d" % index,
                                               "text": "Вопрос про упаковку заказов номер %d?" % index})
        line = q_mod.closed_line(q_mod.read_topics(self.cfg, ROOM))
        self.assertTrue(line.startswith("Об этом Михаил уже сказал"))
        self.assertLessEqual(len(line), q_mod.CLOSED_LINE_MAX)


class LiveFixtureTests(unittest.TestCase):
    """§8: нейтральный аналог живой задачи — 52 вопроса схлопываются по темам."""

    def test_fifty_two_questions_collapse(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(data["questions"]), 52, "фикстура должна повторять объём живой задачи")
        visible, hidden = q_mod.dedupe([dict(item) for item in data["questions"]])
        self.assertEqual(len(visible) + len(hidden), 52)
        self.assertGreaterEqual(len(hidden), 25,
                                "больше половины 52 вопросов — повторы одной темы: %d" % len(hidden))
        self.assertLessEqual(len(visible), 20, "52 вопроса одной задачи схлопываются по темам")
        self.assertTrue(all(item.get("duplicate_of") for item in hidden))
        # Главное правило: у каждой темы ровно один ЖДУЩИЙ вопрос. Отвеченные копии
        # остаются (это история и ответ на них), но живые не дублируются.
        waiting = [item for item in visible if not item.get("answered") and not item.get("dismissed")]
        texts = [item["text"] for item in waiting]
        for left in texts:
            for right in texts:
                if left is not right and left != right:
                    self.assertFalse(q_mod.is_same_topic(left, right),
                                     "ложная склейка или дубль: %s || %s" % (left, right))
        # и ни одна склейка не «съела» другую тему целиком
        keys = [item["topic_key"] for item in waiting]
        self.assertEqual(len(keys), len(set(keys)), "у двух ждущих вопросов одна тема")

    def test_codes_are_flagged_in_fixture(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        coded = [item for item in data["questions"] if item["topic"].startswith("code_")]
        self.assertTrue(coded)
        for item in coded:
            ok, issues = q_mod.plain_check(item["text"], GOOD_WHY, GOOD_CONTEXT)
            self.assertFalse(ok, item["text"])
            self.assertTrue(issues)


# ------------------------------------------------------------------ через сервер ---

class QuestionsApiTests(_Base):
    """§1, §4, §5, §7 через настоящие маршруты приложения."""

    def setUp(self):
        super().setUp()
        # Комната песочницы общая на весь модуль: реестр закрытых тем чистим, иначе
        # тест «Не актуально» закрывал бы тему для остальных тестов (это и есть
        # правильное поведение продукта — но здесь мешает изоляции).
        try:
            q_mod.topics_path(self.cfg, ROOM).unlink()
        except OSError:
            pass

    def _request(self, questions, title="Химчистка на районе", text="Собери план стирки и доставки"):
        root = self.sa.card(text, "team-director", status="running")
        metadata = {"open_questions": questions, "next_steps": [],
                    "stage": {"current": "start", "phases": [
                        {"id": "start", "title": "Старт", "status": "active", "note": ""}]}}
        self.sa.finish(root, "План собран, нужны ответы по доставке.", metadata)
        registry.add_request(self.cfg, ROOM, _record(root, title=title, text=text))
        return root

    def _ask(self, **extra):
        question = {"text": Q_DELIVERY, "why": GOOD_WHY, "context": GOOD_CONTEXT,
                    "blocking": True,
                    "options": [{"id": "o1", "label": "Самовывоз",
                                 "consequence": "Стартуем сразу, но клиент едет сам"},
                                {"id": "o2", "label": "Курьер по городу",
                                 "consequence": "Плюс 3 000 в неделю на смену"}]}
        question.update(extra)
        return question

    def test_detail_carries_the_contract_fields(self):
        rid = self._request([self._ask(), {"text": Q_CODE, "blocking": True},
                             {"text": Q_COURIER, "blocking": True},
                             {"text": "Кто ведёт реестр клиентов?", "blocking": True},
                             {"text": "Ставим вывеску световую или простую?", "blocking": False}])
        detail = self.detail(rid)
        questions = detail["open_questions"]
        self.assertEqual(len(questions), 5)
        first = next(item for item in questions if item["text"] == Q_DELIVERY)
        self.assertEqual(first["why"], GOOD_WHY)
        self.assertEqual(first["context"], GOOD_CONTEXT)
        self.assertEqual([option["id"] for option in first["options"]], ["o1", "o2"])
        self.assertEqual(first["options"][1]["consequence"], "Плюс 3 000 в неделю на смену")
        self.assertTrue(first["plain_ok"], first["plain_issues"])
        self.assertTrue(first["topic_key"])
        coded = next(item for item in questions if item["text"] == Q_CODE)
        self.assertFalse(coded["plain_ok"])
        self.assertTrue(coded["plain_issues"])

    def test_blocking_is_capped_at_three(self):
        rid = self._request([self._ask(),
                             {"text": Q_COURIER, "blocking": True},
                             {"text": "Кто ведёт реестр клиентов из района?", "blocking": True},
                             {"text": "Груз до тонны: своя газель или наёмный перевозчик?", "blocking": True}])
        detail = self.detail(rid)
        summary = detail["questions_summary"]
        self.assertEqual(summary["blocking"], q_mod.MAX_BLOCKING)
        self.assertEqual(summary["open"], 4)
        extra = [item for item in detail["open_questions"] if item.get("decision_package")]
        self.assertEqual(len(extra), 1, "четвёртый блокирующий уходит в «пакет решений»")
        self.assertFalse(extra[0]["blocking"])

    def test_duplicates_are_hidden_not_lost(self):
        rid = self._request([self._ask(),
                             {"text": "Как доставляем на старте — самовывоз или курьер по городу?"},
                             {"text": Q_COURIER}])
        detail = self.detail(rid)
        self.assertEqual(len(detail["open_questions"]), 2)
        self.assertEqual(detail["questions_summary"]["hidden_duplicates"], 1)
        hidden = detail["hidden_duplicate_questions"][0]
        self.assertEqual(hidden["duplicate_of"], detail["open_questions"][0]["id"])
        self.assertTrue(hidden["id"])

    def test_stage_hint_says_work_is_waiting_on_blocking(self):
        import server.stale as stale_mod
        rid = self._request([self._ask()])
        detail = self.detail(rid)
        self.assertEqual(detail["questions_summary"]["blocking"], 1)
        self.assertTrue(detail["open_questions"][0]["blocking"])
        # Подпись этапа считает `stale.stage_hint` по тем же вопросам: у завершённого
        # запроса она пустая (ждать некого), у идущего — «работа ждёт ответа».
        waiting = stale_mod.stage_hint({"status": "working", "open_questions": detail["open_questions"]})
        self.assertIn("Работа ждёт вашего ответа", waiting)
        # только неблокирующие — работа идёт
        rid2 = self._request([self._ask(blocking=False)], title="Вторая")
        detail2 = self.detail(rid2)
        going = stale_mod.stage_hint({"status": "working", "open_questions": detail2["open_questions"]})
        self.assertIn("Работа идёт", going)
        self.assertEqual(detail2["questions_summary"]["blocking"], 0)

    def test_answer_option_goes_to_thread_as_readable_text(self):
        rid = self._request([self._ask()])
        qid = self.detail(rid)["open_questions"][0]["id"]
        status, payload, _ = self.actions(rid, action="answer_option", question_id=qid, option_id="o2")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "answer_option")
        self.assertEqual(payload["answer"]["text"], "Курьер по городу — Плюс 3 000 в неделю на смену")
        self.assertTrue(payload["question"]["answered"])
        self.assertIn("ответ на вопрос", self.shim.comment_texts()[-1])

    def test_answer_option_unknown_id_is_refused(self):
        rid = self._request([self._ask()])
        qid = self.detail(rid)["open_questions"][0]["id"]
        status, payload, _ = self.actions(rid, action="answer_option", question_id=qid, option_id="o9")
        self.assertEqual(status, 400, payload)
        self.assertIn("вариант", payload["error"]["message"].lower())

    def test_answer_custom_and_explain_more(self):
        rid = self._request([self._ask()])
        qid = self.detail(rid)["open_questions"][0]["id"]
        status, payload, _ = self.actions(rid, action="answer_custom", question_id=qid,
                                          text="Свой вариант: возим сами на своей машине")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answer"]["text"], "Свой вариант: возим сами на своей машине")
        rid2 = self._request([self._ask()], title="Третья")
        qid2 = self.detail(rid2)["open_questions"][0]["id"]
        status, payload, _ = self.actions(rid2, action="explain_more", question_id=qid2)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["answer"]["needs_explanation"])
        self.assertEqual(payload["answer"]["state"], "waiting_explanation")

    def test_postpone_takes_question_out_of_counters(self):
        rid = self._request([self._ask(blocking=False)])
        before = self.detail(rid)
        self.assertEqual(before["needs_you_count"]["questions"], 1)
        qid = before["open_questions"][0]["id"]
        status, payload, _ = self.actions(rid, action="postpone", question_id=qid, hours=6)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["state"], "postponed")
        self.assertIn("отложен до", payload["message"])
        detail = self.detail(rid)
        self.assertEqual(detail["needs_you_count"]["questions"], 0)
        question = detail["open_questions"][0]
        self.assertEqual(question["answer_state"], "postponed")
        self.assertTrue(any(action["id"] == "restore" for action in question["actions"]))

    def test_postpone_validates_hours(self):
        rid = self._request([self._ask()])
        qid = self.detail(rid)["open_questions"][0]["id"]
        status, payload, _ = self.actions(rid, action="postpone", question_id=qid, hours=0)
        self.assertEqual(status, 400, payload)

    def test_dismiss_closes_topic_and_contract_says_do_not_ask(self):
        rid = self._request([self._ask()])
        detail = self.detail(rid)
        qid, text = detail["open_questions"][0]["id"], detail["open_questions"][0]["text"]
        status, payload, _ = self.actions(rid, action="dismiss", target={"kind": "question", "id": qid})
        self.assertEqual(status, 200, payload)
        topics = q_mod.read_topics(self.cfg, ROOM)
        self.assertTrue(topics, "тема должна попасть в реестр закрытых")
        line = contract_mod.closed_topics_rule({"id": ROOM}, self.cfg)
        self.assertIn("не актуально", line)
        self.assertIn(q_mod.clip(text, 60)[:20], line)
        # новый вопрос той же темы в другом запросе — скрыт как дубль закрытой темы
        rid2 = self._request([{"text": "Доставка на старте: самовывоз или курьер по городу — как лучше?"}],
                             title="Новая задача")
        detail2 = self.detail(rid2)
        self.assertEqual(detail2["open_questions"], [])
        self.assertEqual(detail2["hidden_duplicate_questions"][0]["duplicate_reason"], "closed_topic")
        # правило для руководителя: сначала форма вопроса, потом «об этом не спрашивать»
        self.assertIn("blocking", contract_mod.questions_rule())
        self.assertIn("Дубли по смыслу", contract_mod.questions_rule())

    def test_explanation_appears_under_question(self):
        rid = self._request([self._ask()])
        room, rec = self.app._find_record(rid)
        detail = self.app._detail(room, rid, rec.get("extra_cards"), rec.get("critic"),
                                  rec.get("answers"))
        qid = detail["open_questions"][0]["id"]
        detail["thread"] = [{
            "id": "c_1", "author": "team-director", "author_kind": "agent",
            "at": int(time.time()),
            "text": "%s %s] Самовывоз — это когда клиент привозит вещи сам, на нашей точке. "
                    "Курьер — наш человек с машиной." % (answers_mod.EXPLAINED_MARK, qid)}]
        self.app._answers_view(room, rec, detail)
        question = detail["open_questions"][0]
        self.assertIn("клиент привозит вещи сам", question["explanation"])
        self.assertEqual(question["answer_state"], "explained")
        self.assertEqual(question["answer_label"], "пояснено — выберите ответ")
        self.assertFalse(question["needs_explanation"])
        self.assertNotIn("пока руководитель", question["answer_hint"])
        self.assertTrue(question["needs_answer"], "пояснённый вопрос всё ещё ждёт ответа")
        self.assertEqual(detail["questions_summary"]["explained"], 1)


class ExplanationParsingTests(unittest.TestCase):
    """§5: метка пояснения руководителя разбирается, мусор не считается."""

    def test_mark_is_parsed(self):
        thread = [{"id": "c_1", "author": "team-director", "author_kind": "agent", "at": 10,
                   "text": "[Руководитель · пояснение по вопросу q_ab12cd34] Это про доставку, "
                           "а не про упаковку."}]
        found = answers_mod.explanations_from_thread(thread)
        self.assertIn("q_ab12cd34", found)
        self.assertTrue(found["q_ab12cd34"]["text"].startswith("Это про доставку"))

    def test_last_explanation_wins(self):
        thread = [
            {"id": "c_1", "author": "team-director", "author_kind": "agent", "at": 10,
             "text": "[Руководитель · пояснение по вопросу q_ab12cd34] Первое."},
            {"id": "c_2", "author": "team-director", "author_kind": "agent", "at": 20,
             "text": "[Руководитель · пояснение по вопросу q_ab12cd34] Второе."},
        ]
        found = answers_mod.explanations_from_thread(thread)
        self.assertEqual(found["q_ab12cd34"]["text"], "Второе.")

    def test_human_message_is_not_an_explanation(self):
        thread = [{"id": "c_1", "author": "mikhail", "author_kind": "user", "at": 10,
                   "text": "Поясни, пожалуйста, что такое самовывоз?"}]
        self.assertEqual(answers_mod.explanations_from_thread(thread), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
