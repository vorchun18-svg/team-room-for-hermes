# -*- coding: utf-8 -*-
"""W27-E1 STALE-BE: единые состояния пунктов, счётчик, «Не актуально»/«Вернуть».

Что проверяется (docs/WAVE27-RULES.md §1, дефект 1 из
``Defects-2026-11-18-evening-stale-needs-you.md``):

* состояния пунктов читаются по ОДНОМУ словарю (`server/stale.py`): вопрос,
  решение, разрешение и шаг знают `open/answered/accepted/rejected/dismissed/done`
  (разрешение — свои `pending…closed/dismissed`), и «ждёт вас» бывает только `open`
  (разрешение — `pending`);
* `needs_you_count = {questions, decisions, approvals, steps, total}` — серверный
  счётчик запроса; закрытые и решённые пункты в него не входят;
* `POST /api/requests/{id}/actions {action:"dismiss"|"restore", target:{kind,id}}`
  работает для вопроса, решения, шага (и разрешения — через реестр разрешений);
  повторное нажатие даёт `409 already_decided`, а не 500;
* `POST /api/rooms/{room}/dismiss_stale {scope:"finished_requests"}` закрывает
  пункты завершённых запросов и возвращает `{closed:{questions,decisions,approvals,steps}}`;
* завершённый запрос закрывает свои пункты САМ (`closed_reason: request_done`) —
  «залипнуть» в счётчике они не могут (дефект 1: 27 при нуле решаемого);
* сторож `tools/check_dead_ends.py` понимает новые состояния и счётчик.

Только песочница `tests.helpers.Sandbox`: реальная доска, `%LOCALAPPDATA%\\hermes`
и живой сервер 8777 не трогаются, CLI Hermes подменён shim'ом. Данные вымышленные.

    python -m unittest tests.test_stale -v
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

import server.app as app_mod                  # noqa: E402
import server.engine_cli as engine_cli        # noqa: E402
import server.engine_write as ew              # noqa: E402
import server.registry as registry            # noqa: E402
import server.rooms as rooms_mod             # noqa: E402
import server.stale as stale                  # noqa: E402
from server.http_util import ApiError         # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402
from tools import check_dead_ends             # noqa: E402

ROOM = "demo-room"
LEAD = "lead-profile"
QUESTION = "Кто отвечает за поставку упаковки?"
DECISION = "Берём упаковку у поставщика «Контрагент А» на 6 месяцев"
STEP = "Согласовать договор с «Контрагент А»"
DUE = "2026-11-20"


class CliShim:
    """subprocess-подмена: пишем argv, ничего не запускаем."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                      # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out = json.dumps({"id": "t_0000demo", "status": "ready"}) if "create" in argv else ""
        return real_subprocess.CompletedProcess(argv, 0, out, "")


def _record(root_id: str, **extra) -> dict:
    rec = {"root_id": root_id, "slug": "r-20261118-2200-stale", "title": "Проверка доступа",
           "text": "Проверь доступ к почтам и контуру", "created_at": int(time.time()),
           "critic": False, "mode": "auto", "adopted": False, "extra_cards": [],
           "ingested": {}, "retargeted": False}
    rec.update(extra)
    return rec


# --------------------------------------------------------------- чистые правила ---
class StaleContractTests(unittest.TestCase):
    """Состояния и счётчик — без доски, сервера и файлов."""

    def test_states_are_the_contract_vocabulary(self):
        self.assertEqual(stale.ITEM_STATES,
                         ("open", "answered", "accepted", "rejected", "dismissed", "done"))
        self.assertEqual(stale.APPROVAL_STATES,
                         ("pending", "granted_once", "granted_always", "denied",
                          "expired", "closed", "dismissed"))
        self.assertEqual(stale.CLOSED_REASONS,
                         ("card_finished", "request_done", "request_archived",
                          "dismissed", "superseded"))

    def test_only_open_waits_for_human(self):
        self.assertEqual(stale.question_state({"answer_state": "open"}), "open")
        self.assertEqual(stale.question_state({"answer_state": "waiting_explanation"}), "open")
        self.assertEqual(stale.question_state({"answer_state": "sent"}), "answered")
        self.assertEqual(stale.question_state({"dismissed": True}), "dismissed")
        self.assertEqual(stale.decision_state({"status": "proposed"}), "open")
        self.assertEqual(stale.decision_state({"status": "accept"}), "accepted")
        self.assertEqual(stale.decision_state({"status": "rejected"}), "rejected")
        self.assertEqual(stale.decision_state({"status": "dismissed"}), "dismissed")
        self.assertEqual(stale.step_state({"state": "open"}), "open")
        self.assertEqual(stale.step_state({"state": "done"}), "done")
        self.assertEqual(stale.step_state({"state": "cancelled"}), "cancelled")
        self.assertEqual(stale.item_state("approval", {"state": "closed"}), "closed")
        for state in ("answered", "sent", "dismissed", "replied_text"):
            self.assertFalse(stale.needs_you("question", {"answer_state": state}), state)
        self.assertTrue(stale.needs_you("decision", {"status": "proposed"}))
        self.assertFalse(stale.needs_you("decision", {"status": "accepted"}))

    def test_approval_needs_you_only_pending_with_action(self):
        self.assertTrue(stale.needs_you("approval", {"state": "pending", "actionable": True,
                                                     "actions": [{"id": "once"}]}))
        self.assertFalse(stale.needs_you("approval", {"state": "closed", "actions": []}))
        self.assertFalse(stale.needs_you("approval", {"state": "pending", "actions": []}))
        self.assertFalse(stale.needs_you("approval", {"state": "pending", "actionable": False,
                                                      "actions": [{"id": "dismiss"}]}))

    def _detail(self, status="working"):
        return {
            "id": "t_0000demo", "status": status,
            "open_questions": [
                {"id": "q_1", "text": QUESTION, "answer_state": "open", "needs_answer": True},
                {"id": "q_2", "text": QUESTION, "answer_state": "sent", "answered": True},
                {"id": "q_3", "text": QUESTION, "answer_state": "dismissed", "dismissed": True},
            ],
            "decisions": [
                {"id": "d_1", "text": DECISION, "status": "proposed", "open": True},
                {"id": "d_2", "text": DECISION, "status": "accepted", "open": False},
            ],
            "next_steps": [
                {"id": "ns_1", "text": STEP, "state": "open"},
                {"id": "ns_2", "text": STEP, "state": "done"},
            ],
            "approvals": [
                {"id": "ap_1", "state": "pending", "actionable": True,
                 "actions": [{"id": "once"}, {"id": "deny"}]},
                {"id": "ap_2", "state": "closed", "closed_reason": "card_finished",
                 "card_state": "done", "actions": []},
            ],
        }

    def test_counter_counts_only_items_with_action(self):
        detail = stale.decorate(self._detail())
        self.assertEqual(detail["needs_you_count"],
                         {"questions": 1, "decisions": 1, "approvals": 1, "steps": 1, "total": 4})
        self.assertGreater(detail["needs_you_count"]["total"], 0)
        # 6 закрытых пунктов не ждут человека, но и счётчик их не считает
        closed = [item for key in ("open_questions", "decisions", "next_steps", "approvals")
                  for item in detail[key] if not item["needs_you"]]
        self.assertEqual(len(closed), 5)

    def test_every_waiting_item_has_action_and_closed_ones_have_reason(self):
        detail = stale.decorate(self._detail())
        for key in ("open_questions", "decisions", "next_steps", "approvals"):
            for item in detail[key]:
                if item["needs_you"]:
                    ids = [action["id"] for action in item.get("actions") or []]
                    self.assertTrue(ids, "%s: «ждёт вас» без действия" % item["id"])
                elif item["state"] in ("dismissed",):
                    ids = [action["id"] for action in item.get("actions") or []]
                    self.assertIn("restore", ids, "%s: снятый пункт без «Вернуть»" % item["id"])
        self.assertEqual(detail["next_steps"][0]["closed_reason"] if
                         detail["next_steps"][0]["closed"] else None, None)

    def test_done_request_keeps_open_items(self):
        """`done` + открытый вопрос — рабочее состояние, автозакрытия нет."""
        detail = stale.decorate(self._detail(status="done"))
        self.assertEqual(detail["stale"]["auto_closed"], 0)
        self.assertGreater(detail["needs_you_count"]["total"], 0)

    def test_finished_request_closes_all_kinds_itself(self):
        detail = stale.decorate(self._detail(status="archived"))
        self.assertEqual(detail["needs_you_count"]["total"], 0)
        self.assertTrue(detail["stale"]["request_finished"])
        self.assertEqual(detail["stale"]["auto_closed"], 4)
        for key in ("open_questions", "decisions", "next_steps", "approvals"):
            for item in detail[key]:
                if item.get("auto_closed"):
                    self.assertEqual(item["state"], "dismissed")
                    self.assertEqual(item["closed_reason"], "request_done")
                    self.assertFalse(item["needs_you"])

    def test_open_targets_for_mass_close(self):
        detail = stale.decorate(self._detail(status="done"), request_status="")
        targets = stale.open_targets(detail)
        self.assertEqual(sorted(t["kind"] for t in targets),
                         ["approval", "decision", "question", "step"])

    def test_cancelled_step_is_superseded(self):
        item = {"id": "ns_1", "text": STEP, "state": "cancelled"}
        stale.decorate({"id": "t_1", "status": "working", "next_steps": [item]})
        # подпись «снят по вашему ответу» интерфейс берёт из state — значение не меняем
        self.assertEqual(item["state"], "cancelled")
        self.assertEqual(item["closed_reason"], "superseded")
        self.assertTrue(item["closed"])
        self.assertFalse(item["needs_you"])

    def test_reason_labels_are_russian(self):
        for reason in stale.CLOSED_REASONS:
            self.assertTrue(stale.REASON_RU.get(reason))
            self.assertFalse(any(ch.isascii() and ch.isalpha()
                                 for ch in stale.REASON_RU[reason]),
                             "подпись причины не должна быть латиницей: %s" % reason)
        self.assertEqual(stale.ACTION_LABELS["dismiss"], "Не актуально")
        self.assertEqual(stale.ACTION_LABELS["restore"], "Вернуть")


class DeadEndsCounterTests(unittest.TestCase):
    """Сторож «без тупиков» понимает новые состояния и счётчик."""

    def _detail(self, counter, items):
        return {"id": "t_1", "approvals": items, "approvals_pending": 1,
                "needs_you": True, "needs_you_count": counter}

    def test_counter_mismatch_is_a_problem(self):
        good = {"id": "ap_1", "state": "pending", "mode": "grant", "grantable": True,
                "card_state": "running", "description": "запуск скрипта",
                "note": "ждёт вашего решения", "actions": [{"id": "once"}]}
        detail = self._detail({"questions": 0, "decisions": 0, "approvals": 2,
                               "steps": 0, "total": 2}, [good])
        problems = check_dead_ends.check_detail(detail, "t_1")
        self.assertTrue([p for p in problems if p["kind"] == "counter"], problems)

    def test_unknown_item_state_is_a_problem(self):
        detail = {"id": "t_1", "open_questions": [
            {"id": "q_1", "text": QUESTION, "state": "waiting_forever",
             "needs_you": True, "actions": [{"id": "dismiss"}]}]}
        problems = check_dead_ends.check_detail(detail, "t_1")
        self.assertTrue([p for p in problems if p["kind"] == "state"], problems)

    def test_healthy_detail_is_silent(self):
        detail = {
            "id": "t_1", "approvals": [], "approvals_pending": 0, "needs_you": False,
            "needs_you_count": {"questions": 1, "decisions": 0, "approvals": 0,
                                "steps": 0, "total": 1},
            "open_questions": [{"id": "q_1", "text": QUESTION, "state": "open",
                                "needs_you": True, "needs_answer": True,
                                "actions": [{"id": "dismiss"}]}],
            "decisions": [{"id": "d_1", "text": DECISION, "state": "accepted",
                           "needs_you": False, "closed_reason": None}],
        }
        self.assertEqual(check_dead_ends.check_detail(detail, "t_1"), [])


# ------------------------------------------------------------------ песочница ---
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
        cls.room = rooms_mod.create_room(cls.cfg, {
            "title": "Комната демо", "orchestrator": LEAD,
            "members": [{"profile": "worker-profile", "role": "worker"}]})

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

    @property
    def room_id(self) -> str:
        return str(self.room.get("id") or ROOM)

    def rid(self) -> str:
        """Запрос с вопросом, решением и шагом — как у боевого «Проверь доступ».

        Корень остаётся в работе (``todo``), а участник завершает СВОЮ карточку с
        вопросами руководителю — так это и происходит в жизни: запрос после этого
        «ждёт вашего слова», а не «завершён».
        """
        root = self.sa.card("Проверь доступ к почтам", LEAD, status="todo",
                            tenant="room:" + self.room_id)
        child = self.sa.card("Проверка доступа", "worker-profile", status="running",
                             tenant="room:" + self.room_id, parent=root)
        metadata = {"open_questions": [{"text": QUESTION, "for": "mikhail"}],
                    "decisions": [{"text": DECISION, "why": "так короче", "status": "proposed"}],
                    "next_steps": [{"text": STEP, "owner": "mikhail", "due": DUE}]}
        self.sa.finish(child, "Проверил: нужен ваш ответ.", metadata)
        registry.add_request(self.cfg, self.room_id, _record(root))
        self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})    # приём в бэклог
        return root

    def set_root_status(self, rid: str, status: str) -> None:
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status=? WHERE id=?", (status, rid))
            con.commit()
        finally:
            con.close()

    def actions(self, rid: str, **body):
        try:
            return self.app.dispatch("POST", "/api/requests/%s/actions" % rid, {}, body)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def detail(self, rid: str) -> dict:
        status, payload, _ = self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})
        self.assertEqual(status, 200, payload)
        return payload

    def counter(self, rid: str) -> dict:
        return self.detail(rid).get("needs_you_count") or {}

    def item(self, rid: str, key: str, ident: str) -> dict:
        for item in self.detail(rid).get(key) or []:
            if str(item.get("id")) == ident:
                return item
        raise AssertionError("пункт %s не найден в %s" % (ident, key))


class DetailCounterTests(_Base):
    def test_detail_has_states_and_server_counter(self):
        rid = self.rid()
        detail = self.detail(rid)
        self.assertIn("needs_you_count", detail)
        counter = detail["needs_you_count"]
        self.assertEqual(set(counter), {"questions", "decisions", "approvals", "steps", "total"})
        self.assertEqual(counter["total"],
                         counter["questions"] + counter["decisions"]
                         + counter["approvals"] + counter["steps"])
        self.assertGreaterEqual(counter["questions"], 1)
        self.assertGreaterEqual(counter["decisions"], 1)
        # у каждого пункта есть состояние словаря контракта
        for item in detail.get("open_questions") or []:
            self.assertIn(item.get("state"), stale.ITEM_STATES)
        for item in detail.get("decisions") or []:
            self.assertIn(item.get("state"), stale.ITEM_STATES)
        for item in detail.get("next_steps") or []:
            self.assertIn(item.get("state"), stale.ITEM_STATES + ("cancelled",))
        self.assertFalse(check_dead_ends.check_detail(detail, rid),
                         check_dead_ends.check_detail(detail, rid))


class QuestionStaleTests(_Base):
    def test_question_dismiss_then_restore_moves_counter(self):
        rid = self.rid()
        before = self.counter(rid)
        self.assertGreaterEqual(before["questions"], 1)
        qid = str(self.detail(rid)["open_questions"][0]["id"])
        status, payload, _ = self.actions(rid, action="dismiss",
                                          target={"kind": "question", "id": qid})
        self.assertEqual(status, 200, payload)
        after = self.counter(rid)
        self.assertEqual(after["questions"], before["questions"] - 1)
        self.assertEqual(after["total"], before["total"] - 1)
        self.assertTrue(self.item(rid, "open_questions", qid)["dismissed"])
        # повторное нажатие — 409 already_decided, а не 500
        status, payload, _ = self.actions(rid, action="dismiss",
                                          target={"kind": "question", "id": qid})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload.get("error", {}).get("code"), "already_decided")
        # «Вернуть» возвращает вопрос в счётчик
        status, payload, _ = self.actions(rid, action="restore",
                                          target={"kind": "question", "id": qid})
        self.assertEqual(status, 200, payload)
        back = self.counter(rid)
        self.assertEqual(back["questions"], before["questions"])
        self.assertTrue(self.item(rid, "open_questions", qid)["needs_answer"])
        status, payload, _ = self.actions(rid, action="restore",
                                          target={"kind": "question", "id": qid})
        self.assertEqual(status, 409, payload)

    def test_answered_question_cannot_be_dismissed(self):
        rid = self.rid()
        qid = str(self.detail(rid)["open_questions"][0]["id"])
        status, payload, _ = self.actions(rid, action="answer", question_id=qid,
                                          text="Отвечает Марина")
        self.assertEqual(status, 200, payload)
        status, payload, _ = self.actions(rid, action="dismiss",
                                          target={"kind": "question", "id": qid})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload.get("error", {}).get("code"), "already_decided")


class DecisionStaleTests(_Base):
    def test_decision_dismiss_and_restore_via_target(self):
        rid = self.rid()
        did = str(self.detail(rid)["decisions"][0]["id"])
        before = self.counter(rid)["decisions"]
        status, payload, _ = self.actions(rid, action="dismiss",
                                          target={"kind": "decision", "id": did})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.counter(rid)["decisions"], before - 1)
        self.assertEqual(self.item(rid, "decisions", did)["state"], "dismissed")
        status, payload, _ = self.actions(rid, action="restore",
                                          target={"kind": "decision", "id": did})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.counter(rid)["decisions"], before)
        self.assertEqual(self.item(rid, "decisions", did)["state"], "open")


class StepStaleTests(_Base):
    def test_step_dismiss_and_restore(self):
        rid = self.rid()
        steps = self.detail(rid).get("next_steps") or []
        self.assertTrue(steps, "шаг не появился в запросе")
        sid = str(steps[0]["id"])
        before = self.counter(rid)["steps"]
        status, payload, _ = self.actions(rid, action="dismiss",
                                          target={"kind": "step", "id": sid})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.counter(rid)["steps"], max(0, before - 1))
        state = self.item(rid, "next_steps", sid)
        self.assertEqual(state["state"], "dismissed")
        self.assertFalse(state["needs_you"])
        status, payload, _ = self.actions(rid, action="restore",
                                          target={"kind": "step", "id": sid})
        self.assertEqual(status, 200, payload)
        back = self.item(rid, "next_steps", sid)
        self.assertEqual(back["state"], "open")
        self.assertTrue(back["needs_you"])


class BadTargetTests(_Base):
    def test_bad_target_is_400(self):
        rid = self.rid()
        status, payload, _ = self.actions(rid, action="dismiss")
        self.assertEqual(status, 400, payload)
        status, payload, _ = self.actions(rid, action="dismiss",
                                          target={"kind": "planet", "id": "x"})
        self.assertEqual(status, 400, payload)
        status, payload, _ = self.actions(rid, action="dismiss",
                                          target={"kind": "question", "id": "q_ffffffff"})
        self.assertEqual(status, 404, payload)


class FinishedRequestTests(_Base):
    def test_finished_request_does_not_wait_for_anyone(self):
        rid = self.rid()
        self.assertGreater(self.counter(rid)["total"], 0)
        self.set_root_status(rid, "archived")
        detail = self.detail(rid)
        self.assertEqual(detail["needs_you_count"]["total"], 0, detail["needs_you_count"])
        self.assertTrue(detail["stale"]["request_finished"])
        self.assertGreaterEqual(detail["stale"]["auto_closed"], 1)
        for item in detail.get("open_questions") or []:
            if item.get("auto_closed"):
                # W29-P4: у архивного запроса своя причина — человек видит, почему
                # пункт закрыт, и после «Вернуть из архива» он откроется снова сам.
                self.assertEqual(item["closed_reason"], "request_archived")
                self.assertFalse(item["needs_you"])

    def test_done_request_keeps_question_until_human_closes_it(self):
        """`done` + открытый вопрос — рабочее «ждёт вашего слова», а не мусор."""
        rid = self.rid()
        self.set_root_status(rid, "done")
        detail = self.detail(rid)
        self.assertTrue(detail["stale"]["request_finished"])
        self.assertFalse(detail["stale"]["auto_close"])
        self.assertGreater(detail["needs_you_count"]["total"], 0)
        self.assertEqual(detail["stale"]["auto_closed"], 0)

    def test_dismiss_stale_closes_finished_request(self):
        rid = self.rid()
        self.set_root_status(rid, "done")
        status, payload, _ = self.app.dispatch(
            "POST", "/api/rooms/%s/dismiss_stale" % self.room_id, {},
            {"scope": "finished_requests"})
        self.assertEqual(status, 200, payload)
        closed = payload["closed"]
        self.assertEqual(set(closed), {"questions", "decisions", "approvals", "steps"})
        self.assertGreaterEqual(closed["questions"], 1, payload)
        self.assertGreaterEqual(closed["decisions"], 1, payload)
        # после записи счётчик по-прежнему нулевой — закрытое не возвращается
        self.assertEqual(self.counter(rid)["total"], 0)
        # повторный вызов безопасен: не 500 и ничего не «открывает»
        status, again, _ = self.app.dispatch(
            "POST", "/api/rooms/%s/dismiss_stale" % self.room_id, {},
            {"scope": "finished_requests"})
        self.assertEqual(status, 200, again)
        self.assertEqual(again["closed"]["questions"], 0)

    def test_dismiss_stale_scope_is_checked(self):
        try:
            status, payload, _ = self.app.dispatch(
                "POST", "/api/rooms/%s/dismiss_stale" % self.room_id, {}, {"scope": "everything"})
        except ApiError as exc:
            status, payload = exc.status, exc.payload()
        self.assertEqual(status, 400, payload)


class WorkingRequestStaysTests(_Base):
    def test_dismiss_stale_does_not_touch_working_requests(self):
        rid = self.rid()
        before = self.counter(rid)["total"]
        status, payload, _ = self.app.dispatch(
            "POST", "/api/rooms/%s/dismiss_stale" % self.room_id, {},
            {"scope": "finished_requests"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["closed"],
                         {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0})
        self.assertEqual(self.counter(rid)["total"], before)


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
