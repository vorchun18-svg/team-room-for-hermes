# -*- coding: utf-8 -*-
"""W19-D2 DECISIONS: действия над решением «предложено» и понятная цепочка статусов.

Что проверяется (docs/WAVE19-RULES.md, пакет D2 §1–§2; дефекты B-20, B-21):

  * B-20: у решения в статусе «предложено» есть действия «Принять», «Отклонить»,
    «Обсудить» (с текстом/диктовкой). Каждое действие уходит руководителю ТЕМ ЖЕ
    механизмом, что ответ на вопрос и действие по шагу:
    ``POST /api/requests/{rid}/actions {action:"decision", decision_id, decision}``
    → метка ``[Михаил · решение d_x «текст»] <действие>`` в ветке, запись в реестре
    (``answers[d_x]``, ``kind="decision"``), карточка «Уточнение» руководителю;
  * принятое/отклонённое решение СРАЗУ уходит из «Нужен ваш ответ» и из счётчиков —
    по данным API (``status``/``open`` в детали запроса, ``status`` пункта бэклога),
    без перезагрузки и без правки файлов данных;
  * B-21: цепочка «ответ отправлен → руководитель прочитал → учтено»; исход задаёт
    руководитель в ``metadata.reaction.outcome`` (``read|applied|no_change``), а
    разбор по словам остаётся запасным вариантом для старых записей;
  * подписи одинаковы во всём интерфейсе (серверный словарь ``STATE_RU``) и у каждой
    есть подсказка «что значит» (``STATE_HINT``/``CHAIN_HINT``).

Только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён shim'ом; реальная
доска и реальный Hermes сверяются до/после.

    python -m unittest tests.test_decisions -v

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
import server.contract as contract_mod        # noqa: E402
import server.engine_cli as engine_cli        # noqa: E402
import server.engine_write as ew              # noqa: E402
import server.registry as registry            # noqa: E402
import server.thread as thread_mod            # noqa: E402
from server.http_util import ApiError         # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

ROOM = "cafe"
LEAD = "biz-director"

QUESTION = "Добавляем ли биз-финансиста в команду под Россию?"
DECISION = "Стартовать с набора из 6 месяцев, а не из 12"
DECISION_WHY = "набор дешевле на входе, спрос ещё не подтверждён"
DECISION_2 = "Брать упаковку у местного поставщика, а не везти из Китая"


class CliShim:
    """subprocess-подмена: пишем argv, ничего не запускаем (см. tests/test_answers.py)."""

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
        out = []
        for argv in self.calls:
            if "comment" in argv and "--" in argv:
                out.append(argv[argv.index("--") + 1])
        return out

    def creates(self) -> list[list[str]]:
        return [argv for argv in self.calls if "create" in argv]


def _record(root_id: str, **extra) -> dict:
    rec = {"root_id": root_id, "slug": "r-20261106-1200-dec1", "title": "Упаковка и состав",
           "text": "Собери план под Россию", "created_at": int(time.time()),
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
    def rid(self, with_question: bool = False) -> str:
        """Запрос с двумя предложенными решениями (и, если нужно, вопросом)."""
        root = self.sa.card("Собери план под Россию", LEAD, status="running")
        metadata = {
            "decisions": [
                {"text": DECISION, "why": DECISION_WHY, "status": "proposed"},
                {"text": DECISION_2, "why": "логистика длинная", "status": "proposed"},
            ],
            "next_steps": [{"text": "Заказать упаковку на 6 месяцев", "owner": "default"}],
        }
        if with_question:
            metadata["open_questions"] = [{"text": QUESTION, "for": "mikhail"}]
        self.sa.finish(root, "План готов, нужно ваше слово по двум решениям.", metadata)
        registry.add_request(self.cfg, ROOM, _record(root))
        return root

    def actions(self, rid: str, **body):
        try:
            return self.app.dispatch("POST", "/api/requests/%s/actions" % rid, {}, body)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def decide(self, rid: str, did: str, outcome: str, text: str = ""):
        body = {"action": "decision", "decision_id": did, "decision": outcome}
        if text:
            body["text"] = text
        return self.actions(rid, **body)

    def detail(self, rid: str) -> dict:
        status, payload, _ = self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})
        self.assertEqual(status, 200, payload)
        return payload

    def decision_ids(self, rid: str) -> list:
        """id решений в порядке ответа (id считаются по тексту — не «d_1»)."""
        return [str(d.get("id")) for d in self.detail(rid).get("decisions") or []]

    def decision_of(self, rid: str, did: str = "") -> dict:
        items = self.detail(rid).get("decisions") or []
        if not did:
            self.assertTrue(items, "в запросе должно быть решение")
            return items[0]
        for item in items:
            if str(item.get("id")) == did:
                return item
        raise AssertionError("решение не найдено: %s" % [d.get("id") for d in items])

    def note_card(self, rid: str, metadata: dict) -> str:
        """Карточка руководителя в дереве запроса: завершена с метаданными реакции."""
        card = self.sa.card("Уточнение", LEAD, status="running", parent=rid)
        self.sa.finish(card, "Учёл ответ Михаила.", metadata)
        return card


class DecisionActionsTests(_Base):
    """B-20: «предложено» можно принять, отклонить и обсудить."""

    def test_proposed_decision_has_three_actions_and_no_buttons_before(self):
        rid = self.rid()
        decision = self.decision_of(rid)
        self.assertEqual(decision["status"], "proposed")
        self.assertTrue(decision["open"])
        self.assertEqual([a["action"] for a in decision["actions"]], ["accept", "reject", "discuss"])
        self.assertFalse(decision["answered"])
        self.assertIsNone(decision["answer_label"])
        # «Нужен ваш ответ» = решения в статусе proposed/discuss
        detail = self.detail(rid)
        waiting = [d for d in detail["decisions"] if d["open"]]
        self.assertEqual(len(waiting), 2)

    def test_accept_is_sent_to_leader_and_leaves_the_inbox(self):
        rid = self.rid()
        first, second = self.decision_ids(rid)
        status, payload, _ = self.decide(rid, first, "accept", "Согласен, набор на 6 месяцев")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["decision"]["status"], "accepted")
        self.assertEqual(payload["decision"]["answer_state"], "sent")
        self.assertEqual(payload["decision"]["answer_label"], "ответ отправлен")
        self.assertFalse(payload["decision"]["open"])
        self.assertTrue(payload["decision"]["leader_pending"])
        self.assertEqual(payload["action_label"], "Принять")
        # реестр: запись решения рядом с ответами на вопросы
        stored = registry.get(self.cfg, ROOM, rid)["answers"][first]
        self.assertEqual(stored["kind"], "decision")
        self.assertEqual(stored["action"], "accept")
        self.assertEqual(stored["outcome"], "accepted")
        self.assertIn("набор на 6 месяцев", stored["text"])
        # ветка: метка «[Михаил · решение d_1 «…»] Принять: …»
        marks = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.DECISION_MARK) and first in t for t in marks), marks)
        self.assertTrue(any(t.endswith("Согласен, набор на 6 месяцев") for t in marks), marks)
        # руководитель получил карточку «Уточнение» с контрактом реакции
        self.assertTrue(self.shim.creates(), self.shim.calls)
        self.assertIn("metadata={\"reaction\"", self.shim.bodies[0])
        # деталь запроса: решение принято и больше НЕ ждёт Михаила
        after = self.detail(rid)
        accepted = [d for d in after["decisions"] if d["id"] == first][0]
        self.assertEqual(accepted["status"], "accepted")
        self.assertFalse(accepted["open"])
        self.assertEqual([d["id"] for d in after["decisions"] if d["open"]], [second])
        self.assertEqual(payload["request"]["id"], rid)

    def test_reject_and_discuss(self):
        rid = self.rid()
        first, second = self.decision_ids(rid)
        status, payload, _ = self.decide(rid, first, "reject", "Не согласен — нужен год")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["decision"]["status"], "rejected")
        self.assertFalse(payload["decision"]["open"])
        self.assertIn("Отклонить", self.shim.comment_texts()[-1])

        status, payload, _ = self.decide(rid, second, "discuss", "Почему местный поставщик дешевле?")
        self.assertEqual(status, 200, payload)
        # «Обсудить» решение не закрывает: слово по-прежнему за Михаилом
        self.assertEqual(payload["decision"]["status"], "discuss")
        self.assertTrue(payload["decision"]["open"])
        self.assertEqual(payload["decision"]["answer_text"], "Почему местный поставщик дешевле?")

    def test_discuss_needs_a_text(self):
        rid = self.rid()
        status, payload, _ = self.decide(rid, self.decision_ids(rid)[0], "discuss")
        self.assertEqual(status, 400, payload)
        self.assertIn("надиктуйте", json.dumps(payload, ensure_ascii=False))

    def test_unknown_decision_and_bad_action(self):
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        status, payload, _ = self.decide(rid, "d_nonexistent", "accept")
        self.assertEqual(status, 404, payload)
        status, payload, _ = self.decide(rid, first, "approve")
        self.assertEqual(status, 400, payload)

    def test_prefixed_decision_id_from_backlog_is_accepted(self):
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        status, payload, _ = self.decide(rid, "%s:%s" % (rid, first), "accept")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["decision_id"], first)

    def test_leader_accepted_decision_is_not_attributed_to_pavel(self):
        """Решение закрыл сам руководитель — подпись честная, цепочки ответа нет."""
        rid = self.sa.card("Собери план под Россию", LEAD, status="running")
        self.sa.finish(rid, "Готово", {"decisions": [{"text": DECISION, "why": DECISION_WHY,
                                                     "status": "accepted"}]})
        registry.add_request(self.cfg, ROOM, _record(rid))
        decision = self.decision_of(rid)
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["status_source"], "leader")
        self.assertEqual(decision["status_label"], "принято руководителем")
        self.assertIsNone(decision["answer_label"])
        self.assertFalse(decision["open"])

    def test_backlog_item_closes_without_touching_data(self):
        """Принятое/отклонённое уходит из «Дальше» по данным реестра (overlay чтения)."""
        rid = self.rid()
        first, second = self.decision_ids(rid)
        self.decide(rid, first, "accept")
        record = registry.get(self.cfg, ROOM, rid)
        view = {"items": [
            {"id": "%s:%s" % (rid, first), "kind": "decision", "text": DECISION, "status": "open"},
            {"id": "%s:%s" % (rid, second), "kind": "decision", "text": DECISION_2, "status": "open"},
        ]}
        out = answers_mod.backlog_overlay(view, [record])
        by_id = {item["id"]: item for item in out["items"]}
        self.assertEqual(by_id["%s:%s" % (rid, first)]["status"], "done")
        self.assertEqual(by_id["%s:%s" % (rid, first)]["decision_status"], "accepted")
        self.assertEqual(by_id["%s:%s" % (rid, second)]["status"], "open")
        self.assertEqual(out["counts"]["done"], 1)
        self.assertEqual([i["id"] for i in out["sections"]["inbox"]], ["%s:%s" % (rid, second)])


class OutcomeChainTests(_Base):
    """B-21: исход реакции — из metadata.reaction.outcome, слова — запасной вариант."""

    def test_labels_are_one_chain_everywhere(self):
        self.assertEqual(answers_mod.STATE_RU["sent"], "ответ отправлен")
        self.assertEqual(answers_mod.STATE_RU["received"], "руководитель прочитал")
        self.assertEqual(answers_mod.STATE_RU["no_change"], "учтено: менять ничего не нужно")
        self.assertEqual(answers_mod.state_label("applied", ["t_a", "t_b"]), "учтено: изменено 2 карточки")
        self.assertEqual(answers_mod.state_label("applied", [], None), "учтено: работа изменена")
        self.assertEqual(answers_mod.state_label("applied", [], 5), "учтено: изменено 5 карточек")
        self.assertTrue(answers_mod.state_hint("received"))
        self.assertIn("руководитель прочитал", answers_mod.CHAIN_HINT)

    def test_explicit_outcome_beats_words(self):
        # слова говорят «принято», но исход указан явно — верим исходу
        text = "Принято: учёл, ничего менять не нужно. outcome=applied"
        self.assertEqual(answers_mod.outcome_of(text), "applied")
        self.assertEqual(answers_mod.reaction_state(text), "applied")
        self.assertEqual(answers_mod.reaction_state("Работа пересмотрена: карточки t_1"), "applied")
        self.assertEqual(answers_mod.reaction_state("Принято: ничего менять не нужно"), "no_change")
        self.assertEqual(answers_mod.reaction_state("Хорошо, посмотрел"), "received")
        self.assertEqual(answers_mod.outcome_of("Хорошо, посмотрел"), "")

    def test_leader_metadata_outcome_closes_the_decision_chain(self):
        rid = self.rid()
        first = self.decision_ids(rid)[0]
        self.decide(rid, first, "accept", "Согласен")
        self.assertEqual(self.decision_of(rid, first)["answer_state"], "sent")
        self.note_card(rid, {"reaction": {"decision_id": first, "outcome": "applied",
                                          "cards": ["t_abcd1234", "t_abcd5678"], "changes": 2}})
        decision = self.decision_of(rid, first)
        self.assertEqual(decision["answer_state"], "applied")
        self.assertEqual(decision["answer_label"], "учтено: изменено 2 карточки")
        self.assertFalse(decision["leader_pending"])
        self.assertEqual(decision["reaction"]["cards"], ["t_abcd1234", "t_abcd5678"])
        self.assertEqual(decision["reaction"]["source"], "metadata")

    def test_question_reaction_from_metadata_no_change(self):
        rid = self.rid(with_question=True)
        qid = self.detail(rid)["open_questions"][0]["id"]
        status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                          text="Нет, биз-финансиста не добавляем.")
        self.assertEqual(status, 200, payload)
        self.note_card(rid, {"reaction": {"question_id": qid, "outcome": "no_change"}})
        question = self.detail(rid)["open_questions"][0]
        self.assertEqual(question["answer_state"], "no_change")
        self.assertEqual(question["answer_label"], "учтено: менять ничего не нужно")
        self.assertTrue(question["answer_hint"])
        self.assertFalse(question["leader_pending"])

    def test_old_reaction_without_outcome_still_works(self):
        rid = self.rid(with_question=True)
        qid = self.detail(rid)["open_questions"][0]["id"]
        self.actions(rid, action="answer", question_id=qid, text="Да, добавляем геолога.")
        self.sa.comment(rid, LEAD, "%s %s] Работа пересмотрена: карточки t_e9c6c2b2"
                        % (answers_mod.REACTION_MARK, qid))
        question = self.detail(rid)["open_questions"][0]
        self.assertEqual(question["answer_state"], "applied")
        self.assertEqual(question["answer_label"], "учтено: изменено 1 карточка")
        self.assertTrue(question["reaction"]["guessed"], "старая запись — исход выведен по словам")

    def test_leader_reaction_on_decision_shows_outcome(self):
        rid = self.rid()
        second = self.decision_ids(rid)[1]
        self.decide(rid, second, "reject", "Не подходит")
        self.sa.comment(rid, LEAD, answers_mod.decision_reaction_comment(
            second, "Учтено: менять ничего не нужно — берём прежний вариант. outcome=no_change"))
        decision = self.decision_of(rid, second)
        self.assertEqual(decision["answer_state"], "no_change")
        self.assertEqual(decision["answer_label"], "учтено: менять ничего не нужно")
        self.assertFalse(decision["reaction"]["guessed"])


class ContractTests(unittest.TestCase):
    """Контракт руководителя: исход реакции описан явно (B-21)."""

    class FakeCfg:
        """Контракту нужны только каталоги (как в tests/test_contract.py)."""

        def __init__(self, base: Path) -> None:
            self.vault_dir = base / "Vault" / "Rooms"
            self.reports_dir = base / "data" / "reports"
            self.reportkit_dir = base / "reportkit"

    def test_contract_mentions_reaction_outcome(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="tr-dec-contract-") as tmp:
            cfg = self.FakeCfg(Path(tmp))
            room = {"id": "cafe", "title": "Лето", "orchestrator": "biz-director", "members": []}
            body = contract_mod.build_contract_block(room, "request", cfg)
        self.assertIn("reaction:", body)
        self.assertIn("outcome", body)
        self.assertIn("read|applied|no_change", body)
        self.assertIn("реакции на ответ", body)

    def test_decision_comment_round_trip(self):
        mark = answers_mod.decision_comment("accept", "d_7", DECISION, "Берём 6 месяцев")
        self.assertTrue(answers_mod.decision_kind_of(mark))
        self.assertEqual(answers_mod.decision_id_of(mark), "d_7")
        self.assertEqual(answers_mod.decision_action_of(mark), "accept")
        self.assertIn("Принять", mark)
        self.assertIn("Берём 6 месяцев", mark)
        plain = answers_mod.decision_comment("discuss", "d_7", DECISION)
        self.assertEqual(answers_mod.decision_action_of(plain), "discuss")
        self.assertFalse(plain.endswith(":"))


if __name__ == "__main__":
    unittest.main()
