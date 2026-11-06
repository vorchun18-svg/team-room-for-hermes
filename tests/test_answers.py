# -*- coding: utf-8 -*-
"""W14-D ANSWERS: ответ = ответ на вопрос, реакция руководителя, шаги (п. 5, 6, 10).

Что проверяется (WAVE14-RULES.md, пакет D §1–§4):
  * ``POST /api/requests/{rid}/actions {action:"answer"}`` сохраняет связку
    ``question_id ↔ answer`` в реестре, пишет в ветку метку
    ``[Михаил · ответ на вопрос q_x «текст вопроса»]`` и отдаёт запись типа
    ``answer`` (со ссылкой на вопрос), а не обычное сообщение;
  * руководитель получает ответ РАБОЧЕЙ карточкой с контрактом реакции, а его
    реакция в ветке поднимает статус ``sent → received → applied/no_change``
    и показывается у вопроса; без реакции — «руководитель ещё не ответил»
    и «Напомнить руководителю»;
  * встречный вопрос («поясни, что это») вопрос НЕ закрывает: ``answered=False``,
    статус «ждёт вашего решения после пояснения», руководителю — просьба пояснить;
  * ответ «нет/не нужно» снимает связанные шаги («снят по вашему ответу»), а шаг,
    появившийся ПОСЛЕ ответа, помечается «противоречит вашему ответу» + «Убрать».

Только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён shim'ом; реальная
доска и реальный Hermes сверяются до/после.

    python -m unittest tests.test_answers -v

# SHIM-OK: CLI Hermes подменён CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import json
import subprocess as real_subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.answers as answers_mod          # noqa: E402
import server.app as app_mod                  # noqa: E402
import server.engine_cli as engine_cli        # noqa: E402
import server.engine_write as ew              # noqa: E402
import server.registry as registry            # noqa: E402
import server.thread as thread_mod            # noqa: E402
from server.http_util import ApiError         # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

ROOM = "cafe"
LEAD = "biz-director"

QUESTION = "Добавляем ли биз-финансиста (biz-cfo) и геолога в команду?"
Q_ASK = "Какой канал продаж выбрать: бот, QR или вручную?"
STEP_TEXT = "Решить по составу команды: добавить biz-cfo и/или геолога"
STEP_OTHER = "Проверить состав и аллергены на этикетке по СанПиН до первой партии"
ANSWER_NO = "Нет, никакого финансиста не добавляем — команда заточена на Россию."


class CliShim:
    """subprocess-подмена: пишем argv, ничего не запускаем (см. tests/test_thread.py)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                      # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for token in argv:
            if token.startswith("--body-file="):
                try:
                    self.bodies.append(Path(token.split("=", 1)[1]).read_text(encoding="utf-8"))
                except OSError:
                    self.bodies.append("")
        out = json.dumps({"id": "t_0000cafe", "status": "ready"}) if "create" in argv else ""
        return real_subprocess.CompletedProcess(argv, 0, out, "")

    def comment_texts(self) -> list[str]:
        """Тексты комментариев: всё после ``--``."""
        out = []
        for argv in self.calls:
            if "comment" in argv and "--" in argv:
                out.append(argv[argv.index("--") + 1])
        return out

    def creates(self) -> list[list[str]]:
        return [argv for argv in self.calls if "create" in argv]


def _record(root_id: str, **extra) -> dict:
    rec = {"root_id": root_id, "slug": "r-20261105-1200-ans1", "title": "Состав команды",
           "text": "Собери команду под Россию", "created_at": int(time.time()),
           "critic": False, "mode": "auto", "adopted": False, "extra_cards": [],
           "ingested": {}, "retargeted": False}
    rec.update(extra)
    return rec


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)
        cls.shim = CliShim()
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))

    @classmethod
    def tearDownClass(cls):
        engine_cli.subprocess = cls._real_sub
        try:
            del ew.subprocess
        except AttributeError:
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.shim.calls.clear()
        self.shim.bodies.clear()

    # --- помощники ---------------------------------------------------------
    def rid(self, question: str = QUESTION, answer_style: str = "modern") -> str:
        """Свой запрос: корень с завершённым run'ом, вопрос и два шага в metadata."""
        root = self.sa.card("Собери команду под Россию", LEAD, status="running")
        metadata = {"open_questions": [{"text": question, "for": "mikhail"}],
                    "next_steps": [{"text": STEP_OTHER, "owner": "biz-lawyer"},
                                   {"text": STEP_TEXT, "owner": "default", "due": "2026-11-07"}],
                    "stage": {"current": "sales", "phases": [{"id": "sales", "title": "Продажи",
                                                              "status": "active", "note": ""}]}}
        if answer_style == "conflict":
            metadata["next_steps"].append({"text": STEP_TEXT + " (ещё раз)", "owner": "default"})
        self.sa.finish(root, "Команда собрана, нужен ответ по составу.", metadata)
        registry.add_request(self.cfg, ROOM, _record(root))
        return root

    def actions(self, rid: str, **body):
        try:
            return self.app.dispatch("POST", "/api/requests/%s/actions" % rid, {}, body)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def answer(self, rid: str, qid: str, text: str, **extra):
        body = {"action": "answer", "question_id": qid, "text": text}
        body.update(extra)
        return self.actions(rid, **body)

    def detail(self, rid: str) -> dict:
        status, payload, _ = self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})
        self.assertEqual(status, 200, payload)
        return payload

    def question_of(self, rid: str) -> dict:
        detail = self.detail(rid)
        self.assertTrue(detail["open_questions"], "в запросе должен быть вопрос")
        return detail["open_questions"][0]

    def step_by_text(self, detail: dict, text: str) -> dict:
        for step in detail["next_steps"]:
            if step["text"] == text:
                return step
        for step in detail["next_steps"]:
            if step["text"].startswith(text[:30]):
                return step
        raise AssertionError("шаг не найден: %s" % [s["text"] for s in detail["next_steps"]])


class AnswerLinkTests(_Base):
    """§1: ответ = ответ на конкретный вопрос (связка + отдельная запись в ветке)."""

    def test_answer_is_linked_to_question(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        status, payload, _ = self.answer(rid, qid, ANSWER_NO)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answer"]["question_id"], qid)
        self.assertEqual(payload["answer"]["state"], "sent")
        self.assertTrue(payload["question"]["answered"])
        self.assertEqual(payload["question"]["answer"], ANSWER_NO)
        self.assertEqual(payload["question"]["answer_state"], "sent")
        stored = registry.get(self.cfg, ROOM, rid)["answers"][qid]
        self.assertEqual(stored["text"], ANSWER_NO)
        self.assertEqual(stored["question_text"], QUESTION)
        self.assertIn("steps_at_answer", stored)

    def test_thread_has_answer_record_with_question_link(self):
        """Чтение ленты: метка ответа → запись ``kind="answer"`` со ссылкой на вопрос.

        Комментарий кладём прямо в песочную БД (``SimAgents.comment``): в тестах CLI
        Hermes подменён shim'ом, поэтому запись проверяется по argv (см. тест выше),
        а чтение — по фактическим строкам ``task_comments``.
        """
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        mark = answers_mod.answer_comment(qid, QUESTION, ANSWER_NO)
        self.sa.comment(rid, "mikhail", mark)
        items = thread_mod.get_thread(self.cfg, rid, room=self.app._room(ROOM))
        found = [i for i in items if i["kind"] == "answer"]
        self.assertTrue(found, [i["kind"] for i in items])
        item = found[-1]
        self.assertEqual(item["question_id"], qid)
        self.assertIn("биз-финансиста", item["question_text"])
        self.assertEqual(item["answer_kind"], "answer")
        self.assertEqual(item["text"], ANSWER_NO, "текст записи — сам ответ, без метки")
        self.assertEqual(item["author_label"], "Михаил")
        self.assertEqual(item["text_mark"], mark)
        marks = [i for i in items if i["kind"] == "message" and "ответ на вопрос" in i["text"]]
        self.assertEqual(marks, [], "ответ не должен лежать обычным сообщением")

    def test_write_path_puts_mark_in_comment(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.ANSWER_MARK) and qid in t for t in texts), texts)
        self.assertTrue(any(t.endswith(ANSWER_NO) for t in texts), texts)

    def test_comment_mark_helps_leader_see_the_question(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.ANSWER_MARK) and qid in t for t in texts), texts)
        self.assertTrue(any("«Добавляем ли биз-финансиста" in t for t in texts), texts)

    def test_leader_gets_working_card_with_reaction_contract(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        status, payload, _ = self.answer(rid, qid, ANSWER_NO)
        self.assertEqual(status, 200, payload)
        creates = self.shim.creates()
        self.assertEqual(len(creates), 1, self.shim.calls)
        argv = creates[0]
        self.assertIn("--assignee=" + LEAD, argv)
        body = self.shim.bodies[0]
        self.assertIn(qid, body)
        self.assertIn("ОТВЕТ МАКСИМА НА ВОПРОС", body)
        self.assertIn(answers_mod.REACTION_MARK, body, "контракт реакции — в брифе")
        self.assertIn("Работа пересмотрена: карточки", body)
        self.assertIn("ничего менять не нужно", body)
        self.assertEqual(payload["note"]["task_id"], "t_0000cafe")

    def test_answer_to_unknown_question_is_404(self):
        rid = self.rid()
        status, payload, _ = self.answer(rid, "q_нет_такого", "Нет")
        self.assertEqual(status, 404, payload)


class ReactionTests(_Base):
    """§2: статус ответа и видимая реакция руководителя."""

    def test_pending_until_leader_reacts(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        question = self.question_of(rid)
        self.assertEqual(question["answer_state"], "sent")
        self.assertTrue(question["leader_pending"])
        self.assertTrue(question["can_remind"])
        self.assertIn("руководитель ещё не ответил", question["answer_label"])
        self.assertIn("reaction_overdue", question)
        self.assertIsNotNone(question["reaction_waited_s"])
        detail = self.detail(rid)
        self.assertEqual(detail["answers"]["summary"]["pending"], 1)
        self.assertEqual(detail["answers"]["pending_label"], "руководитель ещё не ответил")

    def test_reaction_applied_is_visible_on_question(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        self.sa.comment(rid, LEAD, "%s %s] Принято: биз-финансиста не добавляем; "
                                   "работа пересмотрена: карточки t_e9c6c2b2"
                        % (answers_mod.REACTION_MARK, qid))
        question = self.question_of(rid)
        self.assertEqual(question["answer_state"], "applied")
        self.assertEqual(question["answer_label"], "учтено: работа пересмотрена")
        self.assertFalse(question["leader_pending"])
        self.assertFalse(question["can_remind"])
        self.assertEqual(question["reaction"]["status"], "applied")
        self.assertEqual(question["reaction"]["cards"], ["t_e9c6c2b2"])
        self.assertTrue(question["answer_changes_done"])
        self.assertIn("биз-финансиста не добавляем", question["reaction"]["text"])

    def test_reaction_no_change(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, "Да, добавляем геолога")
        self.sa.comment(rid, LEAD, "%s %s] Принято: геолога добавляем; ничего менять не нужно"
                        % (answers_mod.REACTION_MARK, qid))
        question = self.question_of(rid)
        self.assertEqual(question["answer_state"], "no_change")
        self.assertEqual(question["answer_label"], "принято: менять ничего не нужно")
        self.assertFalse(question["answer_changes_done"])

    def test_plain_leader_comment_with_question_id_counts_as_reaction(self):
        """Запасной путь: руководитель не написал метку, но назвал вопрос."""
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        self.sa.comment(rid, LEAD, "По %s принято, ничего менять не нужно" % qid)
        question = self.question_of(rid)
        self.assertEqual(question["answer_state"], "no_change", question)
        self.assertTrue(question["reaction"]["implicit"])

    def test_remind_sends_signal_and_marks_registry(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        self.shim.calls.clear()
        status, payload, _ = self.actions(rid, action="answer_remind", question_id=qid)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["reminders"], 1)
        self.assertIsNotNone(payload["reminded_at"])
        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.REMIND_MARK) for t in texts), texts)
        self.assertTrue(any(qid in t and "Жду реакции" in t for t in texts), texts)
        self.assertIsNotNone(self.question_of(rid)["reminded_at"])

    def test_remind_after_reaction_is_conflict(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        self.sa.comment(rid, LEAD, "%s %s] Принято: всё учтено" % (answers_mod.REACTION_MARK, qid))
        status, payload, _ = self.actions(rid, action="answer_remind", question_id=qid)
        self.assertEqual(status, 409, payload)


class CounterQuestionTests(_Base):
    """§3: встречный вопрос не закрывает вопрос (п. 10 — «поясни, что это»)."""

    def test_explanation_request_keeps_question_open(self):
        rid = self.rid(question="Какой канал продаж выбрать: бот, QR или вручную?")
        qid = self.question_of(rid)["id"]
        status, payload, _ = self.answer(rid, qid, "поясни, что ты имеешь в виду под QR")
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["question"]["answered"], "встречный вопрос — не ответ")
        self.assertEqual(payload["question"]["answer_state"], "waiting_explanation")
        self.assertEqual(payload["question"]["answer_label"],
                         "ждёт вашего решения после пояснения")
        self.assertIsNone(payload["question"]["answered_at"])
        question = self.question_of(rid)
        self.assertFalse(question["answered"])
        self.assertTrue(question["needs_explanation"])
        self.assertEqual(question["answer_state"], "waiting_explanation")
        self.assertIn("ждёт вашего решения", question["answer_label"])
        self.assertNotIn(qid, {i.get("question_id") for i in
                               self.detail(rid)["answers"]["items"]
                               if i.get("answer") and not i.get("needs_explanation")})

    def test_explanation_request_marks_thread_and_brief(self):
        rid = self.rid(question="Какой канал продаж выбрать: бот, QR или вручную?")
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, "поясни, что это значит")
        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.EXPLAIN_MARK) for t in texts), texts)
        body = self.shim.bodies[0]
        self.assertIn("ПРОСИТ ПОЯСНЕНИЕ", body)
        self.assertIn("ОСТАЁТСЯ ОТКРЫТЫМ", body)
        self.assertIn("пояснение по вопросу %s" % qid, body)
        items = thread_mod.get_thread(self.cfg, rid, room=self.app._room(ROOM))
        self.sa.comment(rid, "mikhail", answers_mod.explanation_comment(qid, Q_ASK, "поясни, что это значит"))
        items = thread_mod.get_thread(self.cfg, rid, room=self.app._room(ROOM))
        found = [i for i in items if i["kind"] == "answer"]
        self.assertTrue(found and found[-1]["answer_kind"] == "explanation_request", found)
        self.assertEqual(found[-1]["question_id"], qid)

    def test_explicit_button_beats_heuristic(self):
        """«Мне нужно пояснение» — даже если в тексте есть решение."""
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        status, payload, _ = self.answer(rid, qid, "Нет, не добавляем, но уточни сроки",
                                        need_explanation=True)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["question"]["answer_state"], "waiting_explanation")
        self.assertFalse(payload["question"]["answered"])

    def test_can_answer_again_after_explanation(self):
        rid = self.rid(question="Какой канал продаж выбрать: бот, QR или вручную?")
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, "поясни, что это значит")
        status, payload, _ = self.answer(rid, qid, "Бот, остальное не нужно")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["question"]["answered"])
        self.assertEqual(payload["question"]["answer_state"], "sent")


class StepLinkTests(_Base):
    """§4: связка вопрос ↔ шаг; «нет» снимает шаг; конфликт — «противоречит ответу»."""

    def test_link_by_text_finds_step(self):
        step = {"id": "ns_2", "text": STEP_TEXT}
        question = {"id": "q_7decf20e", "text": QUESTION}
        found, how = answers_mod.link_question(step, [question])
        self.assertEqual(how, "text")
        self.assertEqual(found["id"], "q_7decf20e")

    def test_explicit_from_question_wins(self):
        step = {"id": "ns_2", "text": "совсем другой текст", "from_question": "q_1"}
        found, how = answers_mod.link_question(step, [{"id": "q_1", "text": "вопрос"}])
        self.assertEqual(how, "explicit")
        self.assertEqual(found["id"], "q_1")

    def test_no_answer_cancels_related_step(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        step_id = self.step_by_text(self.detail(rid), STEP_TEXT)["id"]
        status, payload, _ = self.answer(rid, qid, ANSWER_NO)
        self.assertEqual(status, 200, payload)
        self.assertIn(step_id, payload["steps"]["cancelled"], payload)
        detail = self.detail(rid)
        step = self.step_by_text(detail, STEP_TEXT)
        self.assertEqual(step["state"], "cancelled")
        self.assertEqual(step["state_label"], "снят по вашему ответу")
        self.assertTrue(step["cancelled_by_answer"])
        self.assertEqual(step["from_question"], qid)
        other = self.step_by_text(detail, STEP_OTHER)
        self.assertEqual(other["state_label"], "предстоит", "чужие шаги не трогаем")
        self.assertFalse(other["cancelled_by_answer"])

    def test_cancelled_step_is_dismissed_in_room_backlog(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        step_id = self.step_by_text(self.detail(rid), STEP_TEXT)["id"]
        self.answer(rid, qid, ANSWER_NO)
        backlog = self.app.call("ingest", "backlog_view", ((self.cfg, ROOM), {}))
        items = {item["id"]: item for item in backlog["items"]}
        item_id = "%s:%s" % (rid, step_id)
        item = items.get(item_id)
        self.assertIsNotNone(item, list(items))
        self.assertEqual(item["status"], "dismissed")
        self.assertNotIn(item_id,
                         [i["id"] for i in backlog["sections"]["next_steps"]],
                         "снятый шаг не должен висеть в «Дальше»")

    def test_step_created_after_answer_is_conflict(self):
        """Шаг появился ПОСЛЕ ответа «нет» — метка «противоречит вашему ответу»."""
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        self.answer(rid, qid, ANSWER_NO)
        # руководитель создал новую версию ответа уже после ответа Михаила:
        # шаг с НОВЫМ текстом (а значит и id), которого не было на момент ответа
        self.sa.finish(rid, "Обновил план: шаг по составу команды остаётся.",
                       {"open_questions": [{"text": QUESTION, "for": "mikhail"}],
                        "next_steps": [{"text": STEP_TEXT + " (по-новому)", "owner": "default",
                                        "due": "2026-11-08"}],
                        "stage": {"current": "sales",
                                  "phases": [{"id": "sales", "title": "Продажи",
                                              "status": "active", "note": ""}]}})
        detail = self.detail(rid)
        step = self.step_by_text(detail, STEP_TEXT + " (по-новому)")
        self.assertTrue(step["conflicts_with_answer"], step)
        self.assertEqual(step["state_label"], "противоречит вашему ответу")
        self.assertIn(step["id"], detail["answers"]["summary"]["conflicts"])
        self.assertTrue(step["can_remove"], "у конфликтного шага есть кнопка «Убрать»")

    def test_drop_step_removes_it_and_tells_leader(self):
        rid = self.rid()
        detail = self.detail(rid)
        step = self.step_by_text(detail, STEP_OTHER)
        self.shim.calls.clear()
        status, payload, _ = self.actions(rid, action="step_drop", step_id=step["id"])
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["step"]["state"], "dismissed")
        self.assertIn(step["id"], payload["dropped"])
        detail = self.detail(rid)
        self.assertEqual(self.step_by_text(detail, STEP_OTHER)["state"], "dismissed")
        texts = self.shim.comment_texts()
        self.assertTrue(any("убрать шаг %s" % step["id"] in t for t in texts), texts)
        backlog = self.app.call("ingest", "backlog_view", ((self.cfg, ROOM), {}))
        item = {i["id"]: i for i in backlog["items"]}.get("%s:%s" % (rid, step["id"]))
        self.assertEqual((item or {}).get("status"), "dismissed")

    def test_drop_unknown_step_is_404(self):
        rid = self.rid()
        status, payload, _ = self.actions(rid, action="step_drop", step_id="ns_99")
        self.assertEqual(status, 404, payload)

    def test_answer_no_does_not_cancel_without_link(self):
        rid = self.rid(question="Какой цвет упаковки выбрать для набора из 6 моти?")
        qid = self.question_of(rid)["id"]
        status, payload, _ = self.answer(rid, qid, "Нет, не нужно")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["steps"]["cancelled"], [])
        detail = self.detail(rid)
        self.assertFalse(any(s["cancelled_by_answer"] for s in detail["next_steps"]))


class AnswersModuleTests(unittest.TestCase):
    """Чистые функции (без сервера): метки, эвристики, статусы."""

    def test_answer_comment_format(self):
        text = answers_mod.answer_comment("q_1", "Нужен ли геолог?", "Нет")
        self.assertEqual(text, "[Михаил · ответ на вопрос q_1 «Нужен ли геолог?»] Нет")
        self.assertEqual(answers_mod.answer_text_of(text), "Нет")
        self.assertEqual(answers_mod.question_id_of(text), "q_1")
        self.assertEqual(answers_mod.question_text_of(text), "Нужен ли геолог?")

    def test_negation(self):
        self.assertTrue(answers_mod.is_negation("Нет, никакого финансиста не добавляем"))
        self.assertTrue(answers_mod.is_negation("Не нужно, спасибо"))
        self.assertFalse(answers_mod.is_negation("Да, добавляем геолога"))
        self.assertFalse(answers_mod.is_negation("Порог прогона — это лимит раундов"))

    def test_counter_question(self):
        self.assertTrue(answers_mod.is_counter_question("поясни, что такое порог прогона"))
        self.assertTrue(answers_mod.is_counter_question("Что это значит?"))
        self.assertFalse(answers_mod.is_counter_question("Нет, не добавляем"))
        self.assertFalse(answers_mod.is_counter_question("Да, но поясни детали по срокам"))

    def test_reaction_state_order(self):
        self.assertEqual(answers_mod.reaction_state("Работа пересмотрена: карточки t_1"), "applied")
        self.assertEqual(answers_mod.reaction_state("Принято, ничего менять не нужно"), "no_change")
        self.assertEqual(answers_mod.reaction_state("Принял, передал юристу"), "received")

    def test_state_of_waiting_explanation(self):
        record = {"text": "поясни", "at": 1, "answered": False, "status": "waiting_explanation"}
        self.assertEqual(answers_mod.state_of(record), "waiting_explanation")
        self.assertEqual(answers_mod.state_of({"text": "да", "at": 1}), "sent")
        self.assertEqual(answers_mod.state_of({"text": "да", "at": 1}, {"status": "applied"}), "applied")

    def test_link_steps_cancel_and_conflict(self):
        questions = [{"id": "q_1", "text": QUESTION, "answer_state": "sent",
                      "answer": ANSWER_NO}]
        answers = {"q_1": {"text": ANSWER_NO, "at": 10, "steps_at_answer": ["ns_1"]}}
        steps = answers_mod.link_steps(
            [{"id": "ns_1", "text": STEP_TEXT}, {"id": "ns_2", "text": STEP_TEXT + " ещё раз"}],
            questions, answers, [])
        self.assertEqual(steps[0]["state"], "cancelled")
        self.assertTrue(steps[1]["conflicts_with_answer"])
        summary = answers_mod.summary(questions, steps)
        self.assertEqual(summary["cancelled"], ["ns_1"])
        self.assertEqual(summary["conflicts"], ["ns_2"])


if __name__ == "__main__":
    unittest.main()
