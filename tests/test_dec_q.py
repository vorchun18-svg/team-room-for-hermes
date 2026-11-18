# -*- coding: utf-8 -*-
"""W20-D1 DEC/Q-BE: решения и вопросы по контракту §1.2 (дефекты B-37, B-38, B-41).

Что проверяется (docs/WAVE20-RULES.md, пакет D1 §1.2; дефекты из
``Defects-2026-11-10-third-round.md``):

* **B-37** — у решения есть «Снять — неактуально» (статус ``dismissed``) и «Вернуть»:
  снятое уходит из «Нужен ваш ответ» и из счётчиков, но остаётся в списке «Решения»
  серым (``status_muted``); отзыв руководителем (``metadata.decisions_retracted``)
  и отсутствие решения в новой версии свода дают ``superseded`` + ``superseded_by``;
* **B-38** — вопросы руководителя из его комментариев: метки
  ``[Руководитель · вопрос] …`` и кандидаты под заголовками «Что решить…» попадают
  в ``open_questions`` с ``source=comment|candidate``, у сообщения в ленте появляется
  ``asks:[id]``, а «Это не вопрос» (``question_dismiss``) даёт ``answer_state=dismissed``;
* **B-41** — если реакция руководителя есть, но записана не по формату, вопрос
  получает ``answer_state=replied_text`` («посмотрите в ленте»), а не вечное
  «ответ отправлен»; реакция по id вопроса распознаётся надёжно;
* счётчики и «Нужен ваш ответ» учитывают новые статусы, старые записи читаются как
  раньше (обратная совместимость).

Только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён shim'ом; реальная
доска и конфиги Hermes сверяются до/после. Данные — вымышленные.

    python -m unittest tests.test_dec_q -v

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
from server.http_util import ApiError         # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

ROOM = "cafe"
LEAD = "team-director"

QUESTION = "Кто отвечает за поставку упаковки для кафе «Лето»?"
ANSWER_NO = "Нет, отдельного поставщика не берём — работаем с прежним"

DECISION_A = "Берём упаковку у поставщика «Ромашка» на 6 месяцев"
DECISION_B = "Считаем стоимость от объёма 650 тонн по тарифу завода"
DECISION_C = "Отчёт по упаковке готовит Марина"

COMMENT_WITH_QS = """Проверил расчёты, всё сходится.

Что решить тебе:

1) Ставим ли срок до 15.12 по упаковке?
2) Кто подписывает договор с «Ромашкой»?
3) Нужен ли отдельный отчёт для Марины?

Остальное разложил сам."""


class CliShim:
    """subprocess-подмена: пишем argv, ничего не запускаем (как в tests/test_decisions.py)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                      # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out = json.dumps({"id": "t_0000cafe", "status": "ready"}) if "create" in argv else ""
        return real_subprocess.CompletedProcess(argv, 0, out, "")

    def comment_texts(self) -> list[str]:
        out = []
        for argv in self.calls:
            if "comment" in argv and "--" in argv:
                out.append(argv[argv.index("--") + 1])
        return out


def _record(root_id: str, **extra) -> dict:
    rec = {"root_id": root_id, "slug": "r-20261111-0930-decq", "title": "Упаковка для кафе",
           "text": "Собери план упаковки для кафе «Лето»", "created_at": int(time.time()),
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

    # --- помощники ---------------------------------------------------------
    def rid(self, decisions=None, questions=None) -> str:
        """Запрос с предложенными решениями (и, если нужно, вопросами)."""
        root = self.sa.card("Собери план упаковки для кафе", LEAD, status="running")
        metadata = {"decisions": decisions if decisions is not None else [
            {"text": DECISION_A, "why": "поставщик рядом, сроки короткие", "status": "proposed"},
            {"text": DECISION_B, "why": "так считает плановый отдел", "status": "proposed"},
        ]}
        if questions:
            metadata["open_questions"] = [{"text": q, "for": "mikhail"} for q in questions]
        self.sa.finish(root, "План готов, нужно ваше слово по решениям.", metadata)
        registry.add_request(self.cfg, ROOM, _record(root))
        return root

    def actions(self, rid: str, **body):
        try:
            return self.app.dispatch("POST", "/api/requests/%s/actions" % rid, {}, body)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def detail(self, rid: str) -> dict:
        status, payload, _ = self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})
        self.assertEqual(status, 200, payload)
        return payload

    def decision_ids(self, rid: str) -> list:
        return [str(d.get("id")) for d in self.detail(rid).get("decisions") or []]

    def decision_of(self, rid: str, did: str) -> dict:
        for item in self.detail(rid).get("decisions") or []:
            if str(item.get("id")) == did:
                return item
        raise AssertionError("решение не найдено: %s" % did)

    def leader_card(self, rid: str, metadata: dict, summary: str = "Учёл ответ Михаила.") -> str:
        card = self.sa.card("Уточнение", LEAD, status="running", parent=rid)
        self.sa.finish(card, summary, metadata)
        return card

    def backlog(self) -> dict:
        status, payload, _ = self.app.dispatch("GET", "/api/rooms/%s/backlog" % ROOM, {}, {})
        self.assertEqual(status, 200, payload)
        return payload

    def waiting_decisions(self, rid: str) -> list:
        return [d for d in self.detail(rid).get("decisions") or [] if d.get("open")]

    def comment_questions(self, rid: str) -> list:
        return [q for q in self.detail(rid).get("open_questions") or []
                if str(q.get("source") or "") in ("comment", "candidate")]


class DismissTests(_Base):
    """B-37: «Снять — неактуально» вместо вынужденного «Принять»."""

    def test_proposed_decision_can_be_dismissed_and_leaves_the_inbox(self):
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        status, payload, _ = self.actions(rid, action="decision", decision_id=first,
                                          decision="dismiss", text="Это не наш вопрос")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["decision"]["status"], "dismissed")
        self.assertEqual(payload["action_label"], "Снять — неактуально")
        self.assertFalse(payload["decision"]["open"])
        self.assertTrue(payload["decision"]["can_restore"])
        self.assertFalse(payload["decision"]["can_accept"])
        # в списке «Решения» остаётся, но серым
        decision = self.decision_of(rid, first)
        self.assertEqual(decision["status"], "dismissed")
        self.assertTrue(decision["status_muted"])
        self.assertIn("снято", decision["status_label"])
        # из «Нужен ваш ответ» и счётчиков — ушло
        self.assertEqual(len(self.waiting_decisions(rid)), 1)
        # реестр: история действий, чтобы «Вернуть» вернуло именно то, что было
        stored = registry.get(self.cfg, ROOM, rid)["answers"][first]
        self.assertEqual(stored["action"], "dismiss")
        self.assertEqual(stored["history"][-1]["action"], "dismiss")
        # руководителю не карточка-запуск, а метка в ветке: он не должен просыпаться
        texts = self.shim.comment_texts()
        self.assertTrue(any(answers_mod.DECISION_MARK in text and "Снять" in text
                            for text in texts), texts)
        self.assertFalse([argv for argv in self.shim.calls if "create" in argv])

    def test_restore_returns_decision_to_work(self):
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        self.actions(rid, action="decision", decision_id=first, decision="dismiss")
        status, payload, _ = self.actions(rid, action="decision", decision_id=first,
                                          decision="restore")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["decision"]["status"], "proposed")
        self.assertTrue(payload["decision"]["open"])
        self.assertEqual(payload["action_label"], "Вернуть")
        self.assertFalse(payload["decision"]["can_restore"])
        self.assertTrue(payload["decision"]["can_dismiss"])
        self.assertEqual(len(self.waiting_decisions(rid)), 2)
        stored = registry.get(self.cfg, ROOM, rid)["answers"][first]
        self.assertEqual([item["action"] for item in stored["history"]], ["dismiss", "restore"])

    def test_accept_after_dismiss_wins(self):
        """Последнее действие пользователя сильнее: снял → принял = принято."""
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        self.actions(rid, action="decision", decision_id=first, decision="dismiss")
        status, payload, _ = self.actions(rid, action="decision", decision_id=first,
                                          decision="accept", text="Передумал, берём")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["decision"]["status"], "accepted")
        self.assertFalse(payload["decision"]["open"])

    def test_dismissed_decision_is_not_in_backlog_inbox(self):
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})   # приём в бэклог
        self.backlog()
        self.actions(rid, action="decision", decision_id=first, decision="dismiss")
        after = self.backlog()
        mine = [item for item in after["items"]
                if str(item.get("id") or "").startswith(rid + ":")]
        target = [item for item in mine if str(item.get("id") or "").endswith(first)]
        self.assertTrue(target, mine)
        self.assertEqual(target[0].get("status"), "dismissed")
        self.assertEqual(target[0].get("decision_status"), "dismissed")
        self.assertNotIn(target[0]["id"], [item.get("id") for item in after["sections"]["inbox"]])


class SupersededTests(_Base):
    """B-37 п.2: руководитель отозвал решение — приложение помечает «устарело»."""

    def test_retracted_in_metadata_marks_decision_superseded(self):
        rid = self.rid()
        second = self.decision_ids(rid)[1]
        # Руководитель отзывает решение в ЛЮБОЙ своей карточке — данные не переписываем
        self.leader_card(rid, {"decisions_retracted": [second],
                               "svod": {"version": 2, "changes": ["убрали расчёт от 650 тонн"]}})
        decision = self.decision_of(rid, second)
        self.assertEqual(decision["status"], "superseded")
        self.assertEqual(decision["status_label"], "устарело")
        self.assertFalse(decision["open"])
        self.assertFalse(decision["needs_answer"])
        self.assertEqual(decision["superseded_by"], 2)
        self.assertEqual(decision["superseded_why"], "retracted")
        self.assertTrue(decision["status_muted"])
        # первое решение не тронуто, в «Нужен ваш ответ» только оно
        self.assertEqual(len(self.waiting_decisions(rid)), 1)
        self.assertEqual(self.waiting_decisions(rid)[0]["id"], self.decision_ids(rid)[0])

    def test_absence_in_new_version_marks_decision_superseded(self):
        rid = self.rid()
        ids = self.decision_ids(rid)
        self.assertTrue(len(ids) >= 2, ids)
        # Новая версия свода содержит только первое решение, и руководитель заявил, что
        # список ПОЛНЫЙ (`decisions_full`) — второе стало устаревшим (W21-S5, B-50)
        self.leader_card(rid, {"svod": {"version": 2, "decisions_full": True},
                               "decisions": [{"text": DECISION_A, "why": "оставляем",
                                              "status": "proposed"}]})
        decision = self.decision_of(rid, ids[1])
        self.assertEqual(decision["status"], "superseded")
        self.assertEqual(decision["superseded_why"], "absent")
        self.assertIsNotNone(decision["superseded_by"])
        self.assertEqual([d["id"] for d in self.waiting_decisions(rid)], [ids[0]])

    def test_superseded_decision_is_shown_even_if_dropped_from_list(self):
        """Решение, которого нет в новой ПОЛНОЙ версии, видно серым — а не исчезает."""
        root = self.sa.card("Собери план упаковки для кафе", LEAD, status="running")
        self.sa.finish(root, "Первый вариант.", {"decisions": [{"text": DECISION_C}]})
        registry.add_request(self.cfg, ROOM, _record(root))
        first = self.decision_ids(root)[0]
        self.leader_card(root, {"svod": {"version": 2, "decisions_full": True},
                                "decisions": [{"text": DECISION_A}]})
        items = {str(d.get("id")): d for d in self.detail(root).get("decisions") or []}
        self.assertIn(first, items)
        self.assertEqual(items[first]["status"], "superseded")
        self.assertIn("устарело", items[first]["status_label"])


DECISION_NEW = "Заказываем десерты у кондитерской «Лето»"


class DecisionsFullTests(_Base):
    """W21-S5 (B-50): «устарело» — только если руководитель перечислил ПОЛНЫЙ список.

    На боевых данных после переноса 1.2.0 карточка обновления с `metadata.decisions`
    из одного элемента объявила устаревшими все прежние решения (два принятых посерели):
    руководитель перечисляет в карточке только открытое/новое. Теперь `absent` работает
    только при `metadata.svod.decisions_full = true`, а обычный список — дополнение.
    """

    def test_partial_list_only_adds_decisions(self):
        rid = self.rid()
        ids = self.decision_ids(rid)
        # карточка руководителя должна быть ПОЗЖЕ текущей версии: при равных секундах
        # порядок версий решается по id карточки
        time.sleep(1.05)
        # руководитель перечислил ОДНО новое решение и не заявил, что список полный
        self.leader_card(rid, {"svod": {"version": 2},
                               "decisions": [{"text": DECISION_NEW, "status": "proposed"}]})
        items = {str(d.get("id")): d for d in self.detail(rid).get("decisions") or []}
        for did in ids:
            self.assertIn(did, items, "прежнее решение не пропало из списка")
            self.assertEqual(items[did]["status"], "proposed", "и не посерело")
            self.assertFalse(items[did].get("superseded"))
            self.assertTrue(items[did].get("carried"), "видно, что оно из прежней версии")
        self.assertTrue(any(DECISION_NEW in str(d.get("text") or "") for d in items.values()),
                        "новое решение добавлено")
        self.assertEqual(len(self.waiting_decisions(rid)), 3)
        self.assertEqual(answers_mod.decision_summary(self.detail(rid)["decisions"])["superseded"], 0)

    def test_partial_list_keeps_accepted_decision_accepted(self):
        """Принятое человеком решение не сереет от того, что его не повторили."""
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        self.actions(rid, action="decision", decision_id=first, decision="accept",
                     text="Передумал, берём")
        self.leader_card(rid, {"svod": {"version": 2},
                               "decisions": [{"text": DECISION_NEW, "status": "proposed"}]})
        decision = self.decision_of(rid, first)
        self.assertEqual(decision["status"], "accepted")
        self.assertFalse(decision["superseded"])
        self.assertTrue(decision["answered"])

    def test_full_list_supersedes_only_missing(self):
        rid = self.rid()
        ids = self.decision_ids(rid)
        self.leader_card(rid, {"svod": {"version": 2, "decisions_full": True},
                               "decisions": [{"text": DECISION_A, "status": "proposed"}]})
        items = {str(d.get("id")): d for d in self.detail(rid).get("decisions") or []}
        self.assertEqual(items[ids[0]]["status"], "proposed", "перечисленное — действует")
        self.assertFalse(items[ids[0]].get("carried"))
        self.assertEqual(items[ids[1]]["status"], "superseded")
        self.assertEqual(items[ids[1]]["superseded_why"], "absent")
        self.assertEqual([d["id"] for d in self.waiting_decisions(rid)], [ids[0]])

    def test_dismissed_decision_is_not_resurrected_by_the_leader_list(self):
        """(C) Последнее действие ЧЕЛОВЕКА сильнее статуса в карточке руководителя."""
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        self.actions(rid, action="decision", decision_id=first, decision="dismiss",
                     text="Это не наш вопрос")
        # руководитель снова перечисляет снятое решение — и сразу как «принятое»
        self.leader_card(rid, {"svod": {"version": 2},
                               "decisions": [{"text": DECISION_A, "status": "accepted"},
                                             {"text": DECISION_B, "status": "proposed"}]})
        decision = self.decision_of(rid, first)
        self.assertEqual(decision["status"], "dismissed", "решение не воскресло")
        self.assertTrue(decision["status_muted"])
        self.assertFalse(decision["open"])
        self.assertEqual(decision["status_source"], "user")
        others = [d["id"] for d in self.waiting_decisions(rid)]
        self.assertEqual(others, [self.decision_ids(rid)[1]])


class CommentQuestionTests(_Base):
    """B-38: вопросы руководителя из его сообщений попадают в «Нужен ваш ответ»."""

    def test_marked_question_is_registered_with_source_comment(self):
        rid = self.rid()
        self.sa.comment(rid, LEAD, "%s Подтверждаем состав команды без финансиста?"
                        % answers_mod.LEAD_QUESTION_MARK)
        questions = self.comment_questions(rid)
        self.assertEqual(len(questions), 1, questions)
        question = questions[0]
        self.assertEqual(question["source"], "comment")
        self.assertTrue(question["id"].startswith("cq_"))
        self.assertEqual(len(question["id"]), 11)      # cq_ + 8 hex
        self.assertTrue(question["comment_id"])
        self.assertIsNotNone(question["at"])
        self.assertTrue(question["needs_answer"])
        self.assertFalse(question["answered"])
        self.assertTrue(self.detail(rid)["needs_you"])

    def test_numbered_items_under_heading_become_candidates(self):
        rid = self.rid()
        self.sa.comment(rid, LEAD, COMMENT_WITH_QS)
        questions = self.comment_questions(rid)
        self.assertEqual(len(questions), 3, [q.get("text") for q in questions])
        self.assertTrue(all(q["source"] == "candidate" for q in questions))
        self.assertIn("Ставим ли срок до 15.12", questions[0]["text"])
        self.assertIn("подписывает договор", questions[1]["text"])
        # проза до и после списка в вопросы не попадает
        self.assertFalse(any("Остальное разложил" in q["text"] for q in questions))

    def test_leader_message_gets_asks_ids(self):
        rid = self.rid()
        self.sa.comment(rid, LEAD, COMMENT_WITH_QS)
        questions = self.comment_questions(rid)
        detail = self.detail(rid)
        item = next((i for i in detail["thread"] if i.get("author") == LEAD), None)
        self.assertIsNotNone(item, detail["thread"])
        self.assertEqual(item.get("asks"), [q["id"] for q in questions])

    def test_question_dismiss_removes_candidate(self):
        rid = self.rid()
        self.sa.comment(rid, LEAD, COMMENT_WITH_QS)
        questions = self.comment_questions(rid)
        target = questions[1]["id"]
        status, payload, _ = self.actions(rid, action="question_dismiss", question_id=target)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answer_state"], "dismissed")
        self.assertEqual(payload["answer_label"], "это не вопрос")
        self.assertFalse(payload["needs_answer"])
        # из «Нужен ваш ответ» ушёл: остальные два пункта по-прежнему ждут ответа
        waiting = [q for q in self.detail(rid)["open_questions"]
                   if (q.get("for") or "mikhail") == "mikhail" and q.get("needs_answer")]
        self.assertEqual([q["id"] for q in waiting], [questions[0]["id"], questions[2]["id"]])
        dismissed = next(q for q in self.detail(rid)["open_questions"] if q["id"] == target)
        self.assertTrue(dismissed["dismissed"])
        self.assertFalse(dismissed["needs_answer"])
        # в ленте сообщение ждёт ответа уже по двум пунктам, а не по трём
        detail = self.detail(rid)
        item = next(i for i in detail["thread"] if i.get("author") == LEAD)
        self.assertNotIn(target, item.get("asks") or [])
        self.assertEqual(len(item.get("asks") or []), 2)

    def test_question_dismiss_works_for_card_question(self):
        rid = self.rid(questions=[QUESTION])
        qid = self.detail(rid)["open_questions"][0]["id"]
        self.backlog()                              # приём в бэклог комнаты
        status, payload, _ = self.actions(rid, action="question_dismiss", question_id=qid)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answer_state"], "dismissed")
        items = [i for i in self.backlog()["items"] if str(i.get("id") or "") == "%s:%s" % (rid, qid)]
        self.assertTrue(items, "вопрос из карточки должен быть в бэклоге")
        self.assertEqual(items[0].get("status"), "dismissed")
        self.assertEqual(items[0].get("answer_state"), "dismissed")

    def test_comment_question_can_be_answered_from_inbox(self):
        """Вопрос из сообщения руководителя отвечается как обычный (из «Нужен ваш ответ»)."""
        rid = self.rid()
        self.sa.comment(rid, LEAD, "%s Подтверждаем состав команды без финансиста?"
                        % answers_mod.LEAD_QUESTION_MARK)
        qid = self.comment_questions(rid)[0]["id"]
        status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                          text="Да, состав подтверждаем без финансиста")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answer"]["question_id"], qid)
        stored = registry.get(self.cfg, ROOM, rid)["answers"][qid]
        self.assertEqual(stored["status"], "sent")
        question = next(q for q in self.detail(rid)["open_questions"] if q["id"] == qid)
        self.assertTrue(question["answered"])
        self.assertEqual(question["answer_state"], "sent")
        # руководитель ответил по делу без метки — «ответил текстом», а не «реакции нет»
        self.sa.comment(rid, LEAD, "Принято: финансиста не берём, состав подтверждён.")
        question = next(q for q in self.detail(rid)["open_questions"] if q["id"] == qid)
        self.assertEqual(question["answer_state"], "replied_text")

    def test_question_dismiss_requires_known_question(self):
        rid = self.rid()
        status, payload, _ = self.actions(rid, action="question_dismiss", question_id="cq_00000000")
        self.assertEqual(status, 404, payload)

    # --- W21-L1: кто имеет право «задавать вопрос» из комментария -----------
    def _without_leader(self) -> list:
        """Убрать руководителя у комнаты (допустимое состояние) — и вернуть как было."""
        import server.rooms as rooms_mod
        data = rooms_mod._load(self.cfg)
        saved = [dict(room) for room in data["rooms"]]
        for room in data["rooms"]:
            if str(room.get("id")) == ROOM:
                room["orchestrator"] = ""
        rooms_mod._save(self.cfg, data)
        return saved

    def _restore_rooms(self, saved: list) -> None:
        import server.rooms as rooms_mod
        data = rooms_mod._load(self.cfg)
        data["rooms"] = saved
        rooms_mod._save(self.cfg, data)

    def test_free_text_of_worker_is_not_a_question_even_with_leader(self):
        """Кандидаты под «Что решить…» берутся только из сообщений руководителя."""
        rid = self.rid()
        self.sa.comment(rid, "team-analyst", COMMENT_WITH_QS)
        self.sa.comment(rid, "team-critic", COMMENT_WITH_QS)
        self.assertEqual(self.comment_questions(rid), [])
        self.assertEqual(self.detail(rid)["thread"][0].get("asks"), None,
                         "у чужого сообщения не должно быть «ждёт ваших ответов»")

    def test_room_without_leader_free_text_gives_no_questions(self):
        """Комната без руководителя: «?» и пункты исполнителя/критика вопросов НЕ дают."""
        rid = self.rid()
        self.sa.comment(rid, "team-analyst",
                        "Свёл расчёт по упаковке. Вопросы есть?\n\n"
                        "Что решить:\n1) Ставим ли срок до 15.12?\n2) Нужен ли отчёт для Марины?")
        self.sa.comment(rid, "team-critic",
                        "Замечаний мало.\n\nЧто нужно решить:\n1) Кто подписывает договор?\n"
                        "2) Брать ли вторую половину набора?")
        saved = self._without_leader()
        try:
            self.assertEqual(self.comment_questions(rid), [])
            detail = self.detail(rid)
            self.assertFalse([q for q in detail["open_questions"]
                              if str(q.get("source") or "") in ("comment", "candidate")])
            for item in detail["thread"]:
                self.assertFalse(item.get("asks") or [], item)
        finally:
            self._restore_rooms(saved)

    def test_room_without_leader_explicit_mark_works_from_any_author(self):
        """Явная строка «[Руководитель · вопрос] …» — от любого автора и без руководителя."""
        rid = self.rid()
        self.sa.comment(rid, "team-critic",
                        "%s Кто подписывает договор с «Ромашкой»?" % answers_mod.LEAD_QUESTION_MARK)
        self.sa.comment(rid, "team-analyst", "Что решить:\n1) Ставим ли срок до 15.12?")
        saved = self._without_leader()
        try:
            questions = self.comment_questions(rid)
            self.assertEqual(len(questions), 1, [q.get("text") for q in questions])
            self.assertEqual(questions[0]["source"], "comment")
            self.assertIn("подписывает договор", questions[0]["text"])
            self.assertTrue(questions[0]["needs_answer"])
        finally:
            self._restore_rooms(saved)


class LeaderDefaultProfileTest(_Base):
    """Регрессия W21-REV2: руководитель комнаты — профиль `default` (боевой случай).

    В боевой комнате `orchestrator` — это id профиля Hermes (`default`), а не
    «team-director» из тестов. Право задавать вопросы считается сравнением
    `thread[].author` с `room.orchestrator` по строке, поэтому проверяем именно
    боевую форму данных: и обычный комментарий руководителя, и итог его карточки
    «Уточнение от Михаила» (там автор — `assignee` карточки).
    """

    def _set_lead(self, profile: str) -> list:
        import server.rooms as rooms_mod
        data = rooms_mod._load(self.cfg)
        saved = [dict(room) for room in data["rooms"]]
        for room in data["rooms"]:
            if str(room.get("id")) == ROOM:
                room["orchestrator"] = profile
        rooms_mod._save(self.cfg, data)
        return saved

    def _restore(self, saved: list) -> None:
        import server.rooms as rooms_mod
        data = rooms_mod._load(self.cfg)
        data["rooms"] = saved
        rooms_mod._save(self.cfg, data)

    LIST = "Что решить:\n1) Ставим ли срок до 15.12?\n2) Нужен ли отчёт для Марины?"

    def test_comment_of_default_leader_makes_questions(self):
        rid = self.rid()
        saved = self._set_lead("default")
        try:
            self.sa.comment(rid, "default", self.LIST)
            questions = self.comment_questions(rid)
            self.assertEqual(len(questions), 2, [q.get("text") for q in questions])
            self.assertTrue(all(q["source"] == "candidate" for q in questions))
            item = next(i for i in self.detail(rid)["thread"] if i.get("author") == "default")
            self.assertTrue(item.get("asks"), "у сообщения руководителя есть «ждёт ответов»")
        finally:
            self._restore(saved)

    def test_card_summary_of_default_leader_makes_questions(self):
        """Руководитель ответил ИТОГОМ карточки — вопросы тоже его."""
        rid = self.rid()
        saved = self._set_lead("default")
        try:
            # настоящая карточка-уточнение: в теле есть id запроса (иначе `_note_cards`
            # её не признает своей), автор итога — assignee = «default»
            card = self.sa.card("Уточнение от Михаила", "default", status="running",
                                parent=rid, body="Уточнение по запросу %s" % rid)
            self.sa.finish(card, "Разобрал уточнение.\n\n" + self.LIST, {})
            questions = self.comment_questions(rid)
            self.assertEqual(len(questions), 2, [q.get("text") for q in questions])
        finally:
            self._restore(saved)

    def test_executor_named_default_elsewhere_is_not_the_leader(self):
        """В комнате с руководителем «team-director» комментарий «default» — чужой."""
        rid = self.rid()
        self.sa.comment(rid, "default", self.LIST)
        self.assertEqual(self.comment_questions(rid), [],
                         "исполнитель с тем же именем не должен задавать вопросы Михаилу")

    def test_default_leader_and_same_named_worker_are_told_apart(self):
        """Руководитель «default» + исполнитель «default-worker»: вопросы только у первого."""
        rid = self.rid()
        saved = self._set_lead("default")
        try:
            self.sa.comment(rid, "default-worker", self.LIST)
            self.assertEqual(self.comment_questions(rid), [])
            self.sa.comment(rid, "default", "Что решить:\n1) Кто подписывает договор?")
            questions = self.comment_questions(rid)
            self.assertEqual(len(questions), 1, [q.get("text") for q in questions])
            self.assertIn("подписывает", questions[0]["text"])
        finally:
            self._restore(saved)


class RepliedTextTests(_Base):
    """B-41: реакция есть, но записана не по формату — не «вечное получено»."""

    def test_text_reply_changes_state_to_replied_text(self):
        rid = self.rid(questions=[QUESTION])
        qid = self.detail(rid)["open_questions"][0]["id"]
        self.actions(rid, action="answer", question_id=qid, text=ANSWER_NO)
        self.assertEqual(self.detail(rid)["open_questions"][0]["answer_state"], "sent")
        # руководитель ответил по делу, но без метки и без исхода
        self.sa.comment(rid, LEAD, "Принято: на письмо от 02.10 отдельно не отвечаем, "
                                  "поставщика оставляем прежнего.")
        question = self.detail(rid)["open_questions"][0]
        self.assertEqual(question["answer_state"], "replied_text", question.get("answer_label"))
        self.assertEqual(question["answer_label"], "руководитель ответил текстом — посмотрите в ленте")
        self.assertTrue(question["answered_text_only"])
        # ответ владельца дан — ждать от него больше нечего, но формальной реакции нет
        self.assertTrue(question["answered"])
        self.assertFalse(question["needs_answer"])
        self.assertFalse(question["leader_pending"])
        self.assertIn("в ленте", question["answer_hint"])

    def test_reaction_by_question_id_is_recognised_exactly(self):
        rid = self.rid(questions=[QUESTION])
        qid = self.detail(rid)["open_questions"][0]["id"]
        self.actions(rid, action="answer", question_id=qid, text=ANSWER_NO)
        self.sa.comment(rid, LEAD, "%s %s] Принято: меняем график поставки; "
                                  "ничего менять не нужно" % (answers_mod.REACTION_MARK, qid))
        question = self.detail(rid)["open_questions"][0]
        self.assertEqual(question["answer_state"], "no_change")
        self.assertFalse(question["answered_text_only"])

    def test_unrelated_leader_message_does_not_fake_a_reaction(self):
        """Посторонняя реплика не делает вид, что реакция есть (аудит B-12)."""
        rid = self.rid(questions=[QUESTION])
        qid = self.detail(rid)["open_questions"][0]["id"]
        self.actions(rid, action="answer", question_id=qid, text=ANSWER_NO)
        self.sa.comment(rid, LEAD, "Разложил работу на четыре карточки, критик проверит.")
        question = self.detail(rid)["open_questions"][0]
        self.assertEqual(question["answer_state"], "sent")
        self.assertTrue(question["leader_pending"])


class CounterTests(_Base):
    """Счётчики и «Нужен ваш ответ» знают новые статусы."""

    def test_decision_summary_counts_muted_separately(self):
        summary = answers_mod.decision_summary([
            {"status": "proposed"}, {"status": "accepted"}, {"status": "dismissed"},
            {"status": "superseded"}, {"status": "discuss"},
        ])
        self.assertEqual(summary["open"], 2)
        self.assertEqual(summary["closed"], 3)
        self.assertEqual(summary["dismissed"], 1)
        self.assertEqual(summary["superseded"], 1)
        self.assertEqual(summary["muted"], 2)
        self.assertEqual(summary["total"], 5)

    def test_needs_you_true_with_comment_question_and_false_after_dismiss(self):
        rid = self.rid()
        self.sa.comment(rid, LEAD, "%s Нужен ли отдельный отчёт?" % answers_mod.LEAD_QUESTION_MARK)
        self.assertTrue(self.detail(rid)["needs_you"])
        qid = self.comment_questions(rid)[0]["id"]
        self.actions(rid, action="question_dismiss", question_id=qid)
        # решения ещё ждут слова, поэтому needs_you остаётся только из-за них
        detail = self.detail(rid)
        waiting = [q for q in detail["open_questions"]
                   if (q.get("for") or "mikhail") == "mikhail" and q.get("needs_answer")]
        self.assertEqual(waiting, [])


class PureFunctionTests(unittest.TestCase):
    """Чистые функции: обратная совместимость и форматы меток."""

    def test_old_decision_record_still_reads(self):
        """Старые записи без новых полей читаются как раньше."""
        view = answers_mod.apply_decision({"id": "d_1", "text": "Берём 6 месяцев",
                                          "status": "proposed"},
                                         {"kind": "decision", "action": "accept", "text": "Да",
                                          "at": 100, "status": "sent"})
        self.assertEqual(view["status"], "accepted")
        self.assertFalse(view["open"])
        self.assertFalse(view["status_muted"])
        self.assertIsNone(view["superseded_by"])
        self.assertFalse(view["can_restore"])

    def test_dismiss_and_supersede_are_muted(self):
        for status in ("dismissed", "superseded"):
            view = answers_mod.apply_decision({"id": "d_1", "text": "Решение", "status": status})
            self.assertTrue(view["status_muted"], status)
            self.assertFalse(view["open"], status)
            self.assertFalse(view["needs_answer"], status)
            self.assertEqual(view["status_label"],
                             answers_mod.DECISION_STATUS_RU[status])

    def test_question_dismissed_state_has_label(self):
        view = answers_mod.apply_question({"id": "q_1", "text": "Вопрос"},
                                          {"kind": "question", "dismissed": True,
                                           "status": "dismissed", "at": 10})
        self.assertEqual(view["answer_state"], "dismissed")
        self.assertEqual(view["answer_label"], "это не вопрос")
        self.assertFalse(view["needs_answer"])
        self.assertTrue(view["dismissed"])

    def test_candidates_stop_at_prose(self):
        text = ("Нужен ваш ответ:\n\n1) Первый вопрос по упаковке\n2) Второй вопрос по срокам\n\n"
                "Дальше просто рассказываю, что сделал за день: 3) это уже не пункт списка")
        items = answers_mod.candidates_from_comment(text, "c1")
        self.assertEqual(len(items), 2, items)

    def test_mark_text_and_kind(self):
        self.assertEqual(answers_mod.lead_comment_kind("[Руководитель · вопрос] Что делаем?"),
                         "question")
        self.assertEqual(answers_mod.lead_comment_kind("[Руководитель · решение] Берём «Ромашку»"),
                         "decision")
        self.assertEqual(answers_mod.lead_comment_kind("Принял, спасибо"), "")
        self.assertEqual(answers_mod.mark_text_of("[Руководитель · вопрос] Что делаем?"),
                         "Что делаем?")

    def test_comment_id_is_stable(self):
        first = answers_mod.comment_item_id("Ставим ли срок?", "c17", 0)
        second = answers_mod.comment_item_id("Ставим ли срок?", "c17", 0)
        other = answers_mod.comment_item_id("Ставим ли срок?", "c18", 0)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith("cq_"))

    def test_versions_and_superseded_are_pure(self):
        # W21-S5 (B-50): `absent` действует только у версии с полным списком (`full`)
        versions = [
            {"version": 1, "at": 10, "id": "t_aaa", "full": True,
             "decisions": [{"id": "d_1"}, {"id": "d_2"}], "retracted": []},
            {"version": 2, "at": 20, "id": "t_bbb", "full": True,
             "decisions": [{"id": "d_1"}], "retracted": []},
        ]
        marks = answers_mod.superseded_decisions(versions)
        self.assertEqual(marks["d_2"]["by"], 2)
        self.assertEqual(marks["d_2"]["why"], "absent")
        self.assertNotIn("d_1", marks)
        # карточка с одним отзывом не объявляет всё остальное устаревшим
        versions.append({"version": 3, "at": 30, "id": "t_ccc", "decisions": [],
                         "retracted": ["d_1"], "full": True})
        marks = answers_mod.superseded_decisions(versions)
        self.assertEqual(marks["d_1"]["why"], "retracted")
        self.assertNotIn("d_2", {k: v for k, v in marks.items() if v["by"] == 3})

    def test_absent_needs_the_full_flag(self):
        """W21-S5 (B-50): без `decisions_full` список решений — дополнение, не сведение."""
        part = [{"version": 1, "at": 10, "id": "t_a", "retracted": [],
                 "decisions": [{"id": "d_1"}, {"id": "d_2"}]},
                {"version": 2, "at": 20, "id": "t_b", "retracted": [],
                 "decisions": [{"id": "d_9"}]}]
        self.assertEqual(answers_mod.superseded_decisions(part), {},
                         "без флага прежние решения не гаснут")
        carried = answers_mod.carried_decisions(part, ["d_9"])
        self.assertEqual([c["id"] for c in carried], ["d_1", "d_2"])
        self.assertTrue(all(c["carried"] and c["from_version"] == 1 for c in carried))
        # ...а с флагом (в том числе строкой "true" из metadata) — гаснут
        nodes = [{"id": "t_root", "ended_at": 10, "metadata": {},
                  "canon": {"decisions": [{"text": "Решение А"}, {"text": "Решение Б"}]}},
                 {"id": "t_l2", "ended_at": 20,
                  "metadata": {"svod": {"version": 2, "decisions_full": "true"}},
                  "canon": {"decisions": [{"text": "Решение А"}]}}]
        versions = answers_mod.decision_versions(nodes)
        self.assertEqual([v["full"] for v in versions], [False, True])
        marks = answers_mod.superseded_decisions(versions)
        self.assertEqual(sorted((k, v["why"]) for k, v in marks.items()), [("d_2", "absent")])
        self.assertEqual(answers_mod.carried_decisions(versions, ["d_1"]), [])

    def test_decision_returning_to_the_swod_is_live_again(self):
        """Решение, снова попавшее в полный список, перестаёт быть «исчезнувшим»."""
        versions = [{"version": 1, "at": 10, "id": "t_a", "full": True, "retracted": [],
                     "decisions": [{"id": "d_1"}]},
                    {"version": 2, "at": 20, "id": "t_b", "full": True, "retracted": [],
                     "decisions": [{"id": "d_2"}]},
                    {"version": 3, "at": 30, "id": "t_c", "full": True, "retracted": [],
                     "decisions": [{"id": "d_1"}, {"id": "d_2"}]}]
        self.assertEqual(answers_mod.superseded_decisions(versions), {})
        # а явный отзыв возвращением в список не снимается — это осознанное действие
        retracted = [{"version": 1, "at": 10, "id": "t_a", "full": True, "retracted": [],
                      "decisions": [{"id": "d_1"}]},
                     {"version": 2, "at": 20, "id": "t_b", "full": True, "retracted": ["d_1"],
                      "decisions": []},
                     {"version": 3, "at": 30, "id": "t_c", "full": True, "retracted": [],
                      "decisions": [{"id": "d_1"}]}]
        self.assertEqual(answers_mod.superseded_decisions(retracted)["d_1"]["why"], "retracted")

    def test_executor_notes_are_not_a_svod_version(self):
        """Строки в `metadata.decisions` исполнителя — заметки, а не новая версия сведения решений."""
        nodes = [
            {"id": "t_root", "ended_at": 10, "metadata": {},
             "canon": {"decisions": [{"text": "Решение А", "status": "accepted"},
                                      {"text": "Решение Б", "status": "proposed"}]}},
            {"id": "t_note", "ended_at": 20,
             "metadata": {"decisions": ["вырезано по ответу", "оставлена одна фраза"]},
             # канон нормализует строки в словари — различать надо по сырым метаданным
             "canon": {"decisions": [{"text": "вырезано по ответу"}, {"text": "оставлена одна фраза"}]}},
        ]
        versions = answers_mod.decision_versions(nodes)
        self.assertEqual([v["id"] for v in versions], ["t_root"])
        self.assertEqual(answers_mod.superseded_decisions(versions), {})
        # а карточка с метаданными свода (svod) — настоящая версия, даже если решения строками
        nodes[1]["metadata"]["svod"] = {"version": 2}
        versions = answers_mod.decision_versions(nodes)
        self.assertEqual([v["id"] for v in versions], ["t_root", "t_note"])


if __name__ == "__main__":
    unittest.main()
