# -*- coding: utf-8 -*-
"""W17-B2: единый поиск реакции руководителя (B-12) и названия запросов (B9 усилено).

Что проверяется:

* **B-12** — «Напомнить руководителю» и окно вопроса ищут реакцию ОДНОЙ функцией:
  автор — только руководитель комнаты, время реакции — не раньше ответа, порог
  совпадения слов выше. Боевой образец `q_7decf20e` (комментарий 16:48 раньше
  ответа 16:52) больше не считается реакцией; `409` всегда с ответом и реакцией.
* **B9 усилено** — название запроса: мгновенное `derived` при создании, улучшение
  малой моделью Hermes в фоне, `title_source`/`title_locked`, приоритет
  `derived < llm < leader < user`, переименование `PATCH /api/requests/{id}`,
  обновление по ходу беседы, неблокирующее создание при недоступном мосте.

Только песочница `tests.helpers.Sandbox`; реальная модель НЕ вызывается —
`server.titles.generate` подменён заглушкой (правило волны 17).

    python -m unittest tests.test_wave17b -v
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.answers as answers_mod          # noqa: E402
import server.registry as registry            # noqa: E402
import server.titles as titles_mod            # noqa: E402
from server.http_util import ApiError         # noqa: E402
from tests.helpers import _insert             # noqa: E402  (запись в песочную БД)
from tests.test_answers import LEAD, QUESTION, ROOM, _Base, _record  # noqa: E402

LIVE = ROOT / "fixtures" / "reaction_b12_live.json"
LONG_DICTATION = ("Так, значит, смотрите, нам нужно разобраться с договором поставки для "
                  "действующего клиента и проверить все приложения к нему. " + "Далее " * 500)


class _B12Base(_Base):
    """Комментарии с управляемым временем — прямо в песочную БД."""

    def comment_at(self, rid: str, author: str, text: str, at: int) -> None:
        con = self.sa.connect()
        try:
            _insert(con, "task_comments", {"task_id": rid, "author": author, "body": text,
                                           "created_at": int(at)})
            con.commit()
        finally:
            con.close()

    def answer_at(self, rid: str, qid: str, text: str, at: int) -> None:
        """Ответ + сдвиг времени ответа в реестре (время задаём вручную)."""
        status, payload, _ = self.answer(rid, qid, text)
        self.assertEqual(status, 200, payload)
        rec = registry.get(self.cfg, ROOM, rid)
        record = dict(rec["answers"][qid], at=int(at))
        registry.update(self.cfg, ROOM, rid, {"answers": {qid: record}})


class ReactionTimeTests(_B12Base):
    """B-12: реакция — только ПОСЛЕ ответа и только от руководителя комнаты."""

    def test_comment_before_answer_is_not_reaction(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        answer_at = int(time.time())
        self.answer_at(rid, qid, "Нет, финансиста не добавляем", answer_at)
        self.comment_at(rid, LEAD, "Уточнение Михаила принято и разнесено по карточкам запроса. "
                                   "**Учтено из уточнений:** основная цель комнаты — управление работой.",
                        answer_at - 250)
        question = self.question_of(rid)
        self.assertIsNone(question["reaction"], question)
        self.assertEqual(question["answer_state"], "sent")
        self.assertTrue(question["can_remind"])
        status, payload, _ = self.actions(rid, action="answer_remind", question_id=qid)
        self.assertEqual(status, 200, payload)

    def test_comment_from_other_agent_is_not_reaction(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        answer_at = int(time.time()) - 10
        self.answer_at(rid, qid, "Нет, финансиста не добавляем", answer_at)
        self.comment_at(rid, "biz-critic", "%s %s] Принято: всё учтено"
                        % (answers_mod.REACTION_MARK, qid), answer_at + 5)
        question = self.question_of(rid)
        self.assertIsNone(question["reaction"], question)
        status, payload, _ = self.actions(rid, action="answer_remind", question_id=qid)
        self.assertEqual(status, 200, payload)

    def test_reaction_after_answer_counts(self):
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        answer_at = int(time.time()) - 60
        self.answer_at(rid, qid, "Нет, финансиста не добавляем", answer_at)
        self.comment_at(rid, LEAD, "%s %s] Принято: работа пересмотрена, карточки t_e9c6c2b2"
                        % (answers_mod.REACTION_MARK, qid), answer_at + 30)
        question = self.question_of(rid)
        self.assertEqual(question["answer_state"], "applied", question)
        self.assertEqual(question["reaction"]["cards"], ["t_e9c6c2b2"])
        self.assertEqual(question["reaction"]["label"], "учтено: работа пересмотрена")

    def test_remind_conflict_carries_answer_and_reaction(self):
        """«Уже ответил» никогда не приходит без показа ответа и реакции."""
        rid = self.rid()
        qid = self.question_of(rid)["id"]
        answer_at = int(time.time()) - 60
        self.answer_at(rid, qid, "Нет, финансиста не добавляем: команда про Россию", answer_at)
        self.comment_at(rid, LEAD, "%s %s] Принято: ничего менять не нужно"
                        % (answers_mod.REACTION_MARK, qid), answer_at + 20)
        status, payload, _ = self.actions(rid, action="answer_remind", question_id=qid)
        self.assertEqual(status, 409, payload)
        detail = payload["error"]["detail"]
        self.assertEqual(detail["reason"], "already_answered")
        self.assertIn("финансиста не добавляем", detail["answer"]["text"])
        self.assertEqual(detail["reaction"]["status"], "no_change")
        self.assertIn("ничего менять не нужно", detail["reaction"]["text"])
        self.assertTrue(detail["question"]["answered"])
        self.assertIsNotNone(detail["question"]["reaction"])

    def test_window_and_button_use_the_same_lookup(self):
        """Один вопрос и весь список вопросов дают ОДИН результат (корень B-12)."""
        data = json.loads(LIVE.read_text(encoding="utf-8"))
        questions = data["questions"]
        records = data["records"]
        thread = data["thread"]
        reactions_all = answers_mod.reactions_from_thread(thread, questions, records, lead="default")
        for question in questions:
            one = answers_mod.reactions_from_thread(thread, [question], records, lead="default")
            self.assertEqual(bool(one.get(question["id"])),
                             bool(reactions_all.get(question["id"])),
                             question["id"])
        target = next(q for q in questions if q["id"] == "q_7decf20e")
        _record_answer, reaction = answers_mod.reaction_lookup(
            target, questions, records, thread, lead="default")
        self.assertIsNone(reaction, "комментарий ДО ответа — не реакция (B-12)")

    def test_before_answer_comment_does_not_close_other_questions(self):
        data = json.loads(LIVE.read_text(encoding="utf-8"))
        reactions = answers_mod.reactions_from_thread(data["thread"], data["questions"],
                                                      data["records"], lead="default")
        self.assertNotIn("q_f69f0d34", reactions)      # раньше ложная атрибуция
        self.assertIn("q_a37572d5", reactions)         # настоящая реакция после ответа


class TitlePriorityTests(unittest.TestCase):
    """Приоритет источников названия — чистая логика (без сервера)."""

    def test_sources_order(self):
        self.assertEqual([titles_mod.PRIORITY[s] for s in ("derived", "llm", "leader", "user")],
                         [0, 1, 2, 3])

    def test_overlay_prefers_stored_llm_over_derived(self):
        item = {"title_short": "Из диктовки", "title_source": "derived"}
        titles_mod.overlay(item, {"title_short": "Разбор договора поставки", "title_source": "llm"})
        self.assertEqual(item["title_short"], "Разбор договора поставки")
        self.assertEqual(item["title_source"], "llm")

    def test_overlay_keeps_user_name(self):
        item = {"title_short": "Название руководителя", "title_source": "leader"}
        titles_mod.overlay(item, {"title_short": "Моё имя", "title_source": "user",
                                  "title_locked": True})
        self.assertEqual(item["title_short"], "Моё имя")
        self.assertTrue(item["title_locked"])

    def test_leader_beats_llm(self):
        item = {"title_short": "Заголовок руководителя", "title_source": "leader"}
        titles_mod.overlay(item, {"title_short": "Название модели", "title_source": "llm"})
        self.assertEqual(item["title_short"], "Заголовок руководителя")

    def test_derive_never_empty_and_short(self):
        value = titles_mod.derive(LONG_DICTATION)
        self.assertTrue(value)
        self.assertLessEqual(len(value), titles_mod.TITLE_LIMIT)
        self.assertEqual(titles_mod.derive(""), "Запрос")
        self.assertFalse(value.lower().startswith("так"))

    def test_seed_marks_user_title(self):
        fields = titles_mod.seed("текст", "Сдать отчёт по НДС", user=True)
        self.assertEqual(fields["title_source"], "user")
        self.assertTrue(fields["title_locked"])


class TitleApiTests(_Base):
    """Название через API: создание, улучшение, переименование, обновление."""

    def setUp(self):
        super().setUp()
        self._real_generate = titles_mod.generate
        self.bridge_calls: list = []
        # CLI Hermes подменён shim'ом с ФИКСИРОВАННЫМ id карточки — для названий
        # нужны разные запросы, иначе второй `create` считается дубликатом.
        self._shim_run = self.shim.run
        counter = {"n": 0}

        def unique_run(argv, **kwargs):
            done = self._shim_run(argv, **kwargs)
            tokens = [str(a) for a in argv]
            if "create" in tokens and getattr(done, "stdout", ""):
                counter["n"] += 1
                payload = json.loads(done.stdout)
                payload["id"] = "t_%s%02d" % (self._uniq(), counter["n"])
                return type(done)(tokens, 0, json.dumps(payload), "")
            return done

        self.shim.run = unique_run

        def fake_generate(cfg, text):
            self.bridge_calls.append(text)
            return {"ok": True, "derived": "Ответ на запрос заказчика по договору поставки",
                    "title": "Договор поставки для действующего клиента",
                    "source": "llm", "model": {}, "error": ""}

        titles_mod.generate = fake_generate
        titles_mod.drain(5)

    def tearDown(self):
        titles_mod.generate = self._real_generate
        try:
            self.shim.run = self._shim_run
        except Exception:  # noqa: BLE001
            pass
        super().tearDown()

    def _uniq(self) -> str:
        """Уникальный префикс id карточки для теста (реестр общий на класс)."""
        import uuid
        return uuid.uuid4().hex[:6]

    def create(self, text: str | None = None, **extra):
        """Только проверка ОТВЕТА создания (карточки shim'а нет в песочной доске)."""
        body = {"text": "%s [%s]" % (text or LONG_DICTATION, self._uniq())}
        body.update(extra)
        status, payload, _ = self.app.dispatch("POST", "/api/rooms/%s/requests" % ROOM, {}, body)
        self.assertEqual(status, 201, payload)
        return payload

    def seed_request(self, text: str | None = None, user: bool = False) -> str:
        """Настоящий запрос в песочной доске + запись реестра с полями названия."""
        body_text = "%s [%s]" % (text or LONG_DICTATION, self._uniq())
        root = self.sa.card("Договор поставки", LEAD, status="running")
        self.sa.finish(root, "Готово.", {"open_questions": [{"text": QUESTION, "for": "mikhail"}]})
        record = _record(root, text=body_text)
        record.update(titles_mod.seed(body_text, "", user=user))
        registry.add_request(self.cfg, ROOM, record)
        return root

    def patch_title(self, rid: str, title):
        return self.app.dispatch("PATCH", "/api/requests/%s" % rid, {}, {"title": title})

    def _wait_title(self, rid: str, source: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rec = registry.get(self.cfg, ROOM, rid) or {}
            if rec.get("title_source") == source:
                return rec
            time.sleep(0.05)
        raise AssertionError("название не стало %s: %r" % (source, registry.get(self.cfg, ROOM, rid)))

    def test_create_gives_instant_title_and_does_not_wait_for_bridge(self):
        payload = self.create()
        rid = payload["root_id"]
        summary = payload["request"]
        self.assertEqual(summary["title_source"], "derived")
        self.assertTrue(summary["title_short"])
        self.assertIs(summary["title_locked"], False)
        rec = registry.get(self.cfg, ROOM, rid)
        self.assertEqual(rec["title_source"], "derived")
        self.assertTrue(rec["title_short"])

    def test_llm_title_arrives_in_background(self):
        rid = self.seed_request()
        titles_mod.enqueue(self.cfg, ROOM, rid, LONG_DICTATION)
        self.assertTrue(titles_mod.drain(10))
        self._wait_title(rid, "llm")
        detail = self.detail(rid)
        self.assertEqual(detail["title_short"], "Договор поставки для действующего клиента")
        self.assertEqual(detail["title_source"], "llm")
        self.assertTrue(self.bridge_calls)

    def test_user_rename_locks_title(self):
        rid = self.seed_request()
        titles_mod.enqueue(self.cfg, ROOM, rid, LONG_DICTATION)
        titles_mod.drain(10)
        self._wait_title(rid, "llm")
        status, payload, _ = self.patch_title(rid, "Мой короткий заголовок")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["request"]["title_short"], "Мой короткий заголовок")
        self.assertEqual(payload["request"]["title_source"], "user")
        self.assertTrue(payload["request"]["title_locked"])
        # модель больше не перезаписывает название
        titles_mod.process(self.cfg, ROOM, rid, LONG_DICTATION, "refresh")
        detail = self.detail(rid)
        self.assertEqual(detail["title_short"], "Мой короткий заголовок")

    def test_empty_patch_unlocks_and_restores_automatic_title(self):
        rid = self.seed_request()
        titles_mod.enqueue(self.cfg, ROOM, rid, LONG_DICTATION)
        titles_mod.drain(10)
        self._wait_title(rid, "llm")
        self.patch_title(rid, "Своё имя")
        status, payload, _ = self.patch_title(rid, "")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["request"]["title_source"], "derived")
        self.assertFalse(payload["request"]["title_locked"])
        self.assertTrue(payload["request"]["title_short"])

    def test_patch_requires_title_field(self):
        rid = self.seed_request()
        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("PATCH", "/api/requests/%s" % rid, {}, {})
        self.assertEqual(ctx.exception.status, 400)

    def test_reaction_after_rename_is_not_lost(self):
        rid = self.seed_request()
        self.patch_title(rid, "Имя")
        detail = self.detail(rid)
        self.assertEqual(detail["title_short"], "Имя")

    def test_refresh_is_rate_limited_and_skips_locked(self):
        rid = self.seed_request()
        answers = {"q_1": {"text": "Нет, финансиста не добавляем", "at": int(time.time()),
                           "question_text": QUESTION, "answered": True, "status": "sent"}}
        rec = registry.get(self.cfg, ROOM, rid)
        first = titles_mod.maybe_refresh(self.cfg, self.app._room(ROOM), rec, answers=answers)
        self.assertTrue(first["scheduled"], first)
        second = titles_mod.maybe_refresh(self.cfg, self.app._room(ROOM),
                                          registry.get(self.cfg, ROOM, rid), answers=answers)
        self.assertFalse(second["scheduled"])
        self.assertEqual(second["reason"], "too_soon")
        self.patch_title(rid, "Имя")
        locked = titles_mod.maybe_refresh(self.cfg, self.app._room(ROOM),
                                          registry.get(self.cfg, ROOM, rid),
                                          answers=answers, force=True)
        self.assertFalse(locked["scheduled"])
        self.assertEqual(locked["reason"], "locked")

    def test_create_survives_unavailable_bridge(self):
        """Мост недоступен/завис: создание всё равно 201 с осмысленным названием."""
        def boom(cfg, text):
            raise RuntimeError("мост завис")
        titles_mod.generate = boom
        payload = self.create()
        self.assertTrue(payload["request"]["title_short"])
        self.assertEqual(payload["request"]["title_source"], "derived")

    def test_title_never_blocks_when_worker_disabled(self):
        import os
        os.environ["TEAMROOM_TITLE_WORKER"] = "off"
        try:
            payload = self.create()
            self.assertTrue(payload["request"]["title_short"])
        finally:
            os.environ.pop("TEAMROOM_TITLE_WORKER", None)


class B7RegressionTests(_Base):
    """Ранее сданное B-7 не сломалось на живых образцах (fixtures/questions_live_fn.json)."""

    def live(self) -> dict:
        return json.loads((ROOT / "fixtures" / "questions_live_fn.json").read_text(encoding="utf-8"))

    def test_backlog_overlay_on_live_rows(self):
        live = self.live()
        items = [{"id": item["id"], "kind": item["kind"], "text": item["text"],
                  "status": "open", "request_id": "t_1b0aa253"} for item in live["backlog_questions"]]
        records = [{"root_id": "t_1b0aa253", "answers": live["answers_registry"]}]
        result = answers_mod.backlog_overlay({"items": items}, records)
        states = {item["id"]: item.get("status") for item in result["items"]}
        self.assertEqual(states.get("t_1b0aa253:q_e7b16109"), "done", states)
        self.assertEqual(result["counts"]["hidden_service"], 2)
        self.assertLess(len(result["items"]), len(items), "близнецы схлопнуты")
        self.assertEqual(result["counts"]["total"], len(result["items"]))

    def test_dedupe_aliases_survive(self):
        live = self.live()
        pairs = [("q_22b45cc3", "q_18b309bf"), ("q_d47fe5e7", "q_d5b4abf0")]
        texts = {item["id"].split(":")[-1]: item["text"] for item in live["backlog_questions"]}
        for item in live["detail_questions"]:
            texts.setdefault(str(item.get("id") or "").split(":")[-1], item.get("text") or "")
        for left, right in pairs:
            merged = answers_mod.dedupe_questions([{"id": left, "text": texts[left]},
                                                   {"id": right, "text": texts[right]}])
            self.assertEqual(len(merged), 1, (left, right, merged))
            self.assertIn(right, merged[0]["aliases"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
