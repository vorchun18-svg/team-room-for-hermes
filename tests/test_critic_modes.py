# -*- coding: utf-8 -*-
"""Режим критика «до полного одобрения» и его страховки (волна 13, пакет E, §9).

Проверяется:
* поле комнаты `critic_mode` / `critic_round_cap` (значения и валидация);
* план цикла в режиме «до одобрения»: раунд за раундом, пока не approve;
* страховки: нет прогресса (3 одинаковых раунда), мягкий потолок, пауза при
  лимите провайдера (429) с повтором карточки;
* гейт публикации ответа `delivery_hold` (ответ не отдаётся до одобрения);
* ход цикла `progress` («было / закрыто / осталось»);
* вопрос руководителя «сколько раундов критика» не показывается (фильтр в чтении);
* регрессия режимов 1 и 2 раунда.

Запуск из корня проекта:
    python -m unittest tests.test_critic_modes -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import critic_loop, engine_write, rooms as rooms_mod  # noqa: E402
from server.routes_read import _is_critic_rounds_question              # noqa: E402
from tests.helpers import Sandbox                                     # noqa: E402

ROOT_ID = "t_root001"
CRITIC_ID = "t_crit001"
REVISION_ID = "t_rev0001"
REQUEST_TEXT = "Проверь «Алвара» — что делать?"
ANSWER_MD = "## Ответ\n\nМаржа 46%, точка безубыточности 71 набор."

ISSUE_A = {"where": "Срез CFO", "problem": "старая себестоимость", "fix": "пересчитать"}
ISSUE_B = {"where": "Шаги", "problem": "нет сроков", "fix": "проставить даты"}


def make_room(**over):
    room = {"id": "cafe", "title": "Лето", "board": "team", "tenant": "room:cafe",
            "orchestrator": "biz-director", "critic": "biz-critic",
            "members": [{"profile": "biz-lawyer", "role": "worker", "note": ""}],
            "context_files": [], "critic_pass": True, "critic_mode": "until_approved",
            "max_rounds": 1, "critic_round_cap": 12}
    room.update(over)
    return room


def make_record(**over):
    record = {"root_id": ROOT_ID, "slug": "r-1", "text": REQUEST_TEXT, "critic": True,
              "extra_cards": [], "retargeted": True, "critic_mode": "until_approved"}
    record.update(over)
    return record


def node(task_id, status, *, assignee=None, role="worker", metadata=None, summary="",
         parents=(), error=None):
    return {"id": task_id, "status": status, "assignee": assignee, "role": role,
            "metadata": metadata or {}, "summary_md": summary, "parent_ids": list(parents),
            "error": error}


def make_detail(root_status="done", *, assignee="biz-director", children=(), answer=ANSWER_MD,
                state="ready", critic_info=None, timeline=None, text=REQUEST_TEXT):
    tree = [node(ROOT_ID, root_status, assignee=assignee, role="root")]
    tree.extend(children)
    return {"status": root_status, "text": text, "tree": tree, "critic": critic_info,
            "timeline": [{"kind": "decomposed"}] if timeline is None else timeline,
            "synthesis": {"state": state, "text_md": answer if state == "ready" else ""}}


def kinds(actions):
    return [a["kind"] for a in actions]


def round_entry(number, verdict, issues, at=None):
    return {"round": number, "verdict": verdict, "at": at or (100 + number),
            "issues": [critic_loop._issue_key(item) for item in issues]}


class RoomFieldTest(unittest.TestCase):
    """Поле комнаты: режим и потолок раундов (W13-E §9)."""

    def test_norm_defaults_for_old_rooms(self):
        room = rooms_mod._norm_room({"id": "old", "max_rounds": 2})
        self.assertEqual(room["critic_mode"], "rounds")
        self.assertEqual(room["critic_round_cap"], rooms_mod.DEFAULT_CRITIC_ROUND_CAP)

    def test_norm_keeps_valid_values(self):
        room = rooms_mod._norm_room({"id": "x", "critic_mode": "until_approved",
                                     "critic_round_cap": 7})
        self.assertEqual((room["critic_mode"], room["critic_round_cap"]), ("until_approved", 7))

    def test_norm_replaces_broken_values(self):
        room = rooms_mod._norm_room({"id": "x", "critic_mode": "как-нибудь",
                                     "critic_round_cap": 999})
        self.assertEqual((room["critic_mode"], room["critic_round_cap"]),
                         ("rounds", rooms_mod.DEFAULT_CRITIC_ROUND_CAP))

    def test_check_mode_rejects_unknown(self):
        with self.assertRaises(rooms_mod.RoomsError) as ctx:
            rooms_mod._check_critic_mode("swarm")
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertEqual(ctx.exception.detail["allowed"],
                         ["rounds", "until_approved"])

    def test_check_mode_empty_means_rounds(self):
        self.assertEqual(rooms_mod._check_critic_mode(""), "rounds")
        self.assertEqual(rooms_mod._check_critic_mode(None), "rounds")

    def test_check_cap_limits(self):
        self.assertEqual(rooms_mod._check_critic_cap(5), 5)
        for bad in (1, 31, 0, -3, "12", True):
            with self.assertRaises(rooms_mod.RoomsError):
                rooms_mod._check_critic_cap(bad)

    def test_updatable_contains_critic_mode(self):
        self.assertIn("critic_mode", rooms_mod.UPDATABLE)
        self.assertIn("critic_round_cap", rooms_mod.UPDATABLE)

    def test_contract_line_mentions_room_value(self):
        from server import contract
        line = contract.critic_settings_line(make_room(critic_mode="until_approved",
                                                       critic_round_cap=9))
        self.assertIn("9 раундов", line)
        self.assertIn("Не спрашивай Михаила", line)
        line_rounds = contract.critic_settings_line(make_room(critic_mode="rounds", max_rounds=2))
        self.assertIn("2 раунд", line_rounds)
        self.assertEqual(contract.critic_settings_line(make_room(critic=None)), "")


class ModePlanTest(unittest.TestCase):
    """Таблица состояний для режима «до полного одобрения»."""

    def plan(self, room=None, record=None, detail=None, now=1793171000):
        return critic_loop.plan(room or make_room(), record or make_record(),
                                detail if detail is not None else make_detail(), now)

    def critic_round(self, number, verdict, issues=(), *, status="done"):
        return node(CRITIC_ID, status, assignee="biz-critic",
                    metadata={"verdict": verdict, "issues": list(issues)})

    def cards(self, *rounds):
        out = []
        for number, verdict in rounds:
            cid = CRITIC_ID if number == 1 else "t_crit00%d" % number
            out.append({"id": cid, "kind": "critic", "round": number, "at": 10 * number})
            out.append({"id": REVISION_ID if number == 1 else "t_rev000%d" % number,
                        "kind": "revision", "round": number, "at": 10 * number + 1})
        return out

    # --- основной цикл -------------------------------------------------------
    def test_first_round_created_with_mode(self):
        actions = self.plan()
        self.assertEqual(kinds(actions), ["create_critic"])
        self.assertEqual(actions[0]["round"], 1)
        self.assertEqual(actions[0]["mode"], "until_approved")

    def test_revise_creates_revision(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[self.critic_round(1, "revise", [ISSUE_A])])
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_revision"])

    def test_revision_done_starts_next_round_despite_max_rounds(self):
        """В режиме «до одобрения» max_rounds=1 не останавливает цикл."""
        record = make_record(max_rounds=1, critic_rounds=[round_entry(1, "revise", [ISSUE_A])],
                             extra_cards=self.cards((1, "revise")))
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")])
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_critic"])
        self.assertEqual(actions[0]["round"], 2)

    def test_approve_stops_cycle(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}],
                             critic_rounds=[round_entry(1, "approve", [])])
        detail = make_detail(children=[self.critic_round(1, "approve")])
        self.assertEqual(self.plan(record=record, detail=detail), [])

    def test_stop_is_sticky(self):
        record = make_record(critic_stop={"kind": "no_progress", "round": 3})
        self.assertEqual(self.plan(record=record), [])

    # --- страховка «нет прогресса» ------------------------------------------
    def test_no_progress_stops_after_three_same_rounds(self):
        history = [round_entry(n, "revise", [ISSUE_A, ISSUE_B]) for n in (1, 2, 3)]
        record = make_record(critic_rounds=history, extra_cards=self.cards(
            (1, "revise"), (2, "revise"), (3, "revise")))
        detail = make_detail(children=[
            node("t_crit003", "done", metadata={"verdict": "revise",
                                                "issues": [ISSUE_A, ISSUE_B]}),
            node("t_rev0003", "done", assignee="biz-director", summary="Версия 4")])
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["stop_no_progress"])
        self.assertEqual(actions[0]["stop"]["kind"], "no_progress")
        self.assertEqual(actions[0]["stop"]["round"], 3)

    def test_progress_prevents_stop(self):
        """Замечаний стало меньше — третий раунд не считается зацикливанием."""
        history = [round_entry(1, "revise", [ISSUE_A, ISSUE_B]),
                   round_entry(2, "revise", [ISSUE_A, ISSUE_B]),
                   round_entry(3, "revise", [ISSUE_A])]
        record = make_record(critic_rounds=history, extra_cards=self.cards(
            (1, "revise"), (2, "revise"), (3, "revise")))
        detail = make_detail(children=[
            node("t_crit003", "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node("t_rev0003", "done", assignee="biz-director", summary="Версия 4")])
        self.assertEqual(kinds(self.plan(record=record, detail=detail)), ["create_critic"])

    def test_different_issues_do_not_stall(self):
        history = [round_entry(1, "revise", [ISSUE_A]),
                   round_entry(2, "revise", [ISSUE_B]),
                   round_entry(3, "revise", [ISSUE_A])]
        record = make_record(critic_rounds=history, extra_cards=self.cards(
            (1, "revise"), (2, "revise"), (3, "revise")))
        detail = make_detail(children=[
            node("t_crit003", "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node("t_rev0003", "done", assignee="biz-director", summary="Версия 4")])
        self.assertEqual(kinds(self.plan(record=record, detail=detail)), ["create_critic"])

    def test_two_rounds_are_not_enough_for_stall(self):
        history = [round_entry(1, "revise", [ISSUE_A]), round_entry(2, "revise", [ISSUE_A])]
        record = make_record(critic_rounds=history, extra_cards=self.cards(
            (1, "revise"), (2, "revise")))
        detail = make_detail(children=[
            node("t_crit002", "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node("t_rev0002", "done", assignee="biz-director", summary="Версия 3")])
        self.assertEqual(kinds(self.plan(record=record, detail=detail)), ["create_critic"])

    # --- страховка «мягкий потолок» -----------------------------------------
    def test_soft_cap_stops_with_report(self):
        room = make_room(critic_round_cap=4)
        history = [round_entry(n, "revise", [ISSUE_A, ISSUE_B][: 2 - (n % 2)])
                   for n in (1, 2, 3, 4)]
        record = make_record(critic_rounds=history, extra_cards=self.cards(
            (1, "revise"), (2, "revise"), (3, "revise"), (4, "revise")))
        detail = make_detail(children=[
            node("t_crit004", "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node("t_rev0004", "done", assignee="biz-director", summary="Версия 5")])
        actions = self.plan(room=room, record=record, detail=detail)
        self.assertEqual(kinds(actions), ["stop_cap"])
        self.assertEqual(actions[0]["stop"]["cap"], 4)

    def test_cap_does_not_fire_early(self):
        room = make_room(critic_round_cap=4)
        record = make_record(critic_rounds=[round_entry(1, "revise", [ISSUE_A])],
                             extra_cards=self.cards((1, "revise")))
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")])
        self.assertEqual(kinds(self.plan(room=room, record=record, detail=detail)),
                         ["create_critic"])

    # --- страховка «лимит провайдера» ---------------------------------------
    def test_rate_limit_pauses_then_retries(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        failed = node(CRITIC_ID, "failed", assignee="biz-critic",
                      error={"code": "failed", "message": "HTTP 429 Too Many Requests"})
        detail = make_detail(children=[failed])
        first = self.plan(record=record, detail=detail, now=1000)
        self.assertEqual(kinds(first), ["pause_rate_limit"])
        self.assertEqual(first[0]["until"], 1000 + critic_loop.RATE_LIMIT_PAUSE)

        paused = dict(record, critic_pause={"kind": "critic", "round": 1, "until": 1600,
                                            "attempt": 0})
        self.assertEqual(self.plan(record=paused, detail=detail, now=1200), [])

        after = self.plan(record=paused, detail=detail, now=1700)
        self.assertEqual(kinds(after), ["retry_card"])
        self.assertEqual(after[0]["attempt"], 1)

    def test_usage_limit_text_is_recognised(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        failed = node(CRITIC_ID, "failed", error={"code": "failed",
                                                  "message": "usage limit reached for today"})
        self.assertEqual(kinds(self.plan(record=record, detail=make_detail(children=[failed]))),
                         ["pause_rate_limit"])

    def test_other_failure_waits(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        failed = node(CRITIC_ID, "failed", error={"code": "failed", "message": "нет доступа"})
        self.assertEqual(self.plan(record=record, detail=make_detail(children=[failed])), [])

    def test_rate_limit_on_revision(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5},
                                          {"id": REVISION_ID, "kind": "revision", "round": 1, "at": 6}])
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node(REVISION_ID, "failed", error={"code": "failed", "message": "429 rate limit"})])
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["pause_rate_limit"])
        self.assertEqual(actions[0]["card_kind"], "revision")

    # --- регрессия режимов 1 и 2 раунда -------------------------------------
    def test_rounds_mode_still_stops_after_limit(self):
        room = make_room(critic_mode="rounds", max_rounds=1)
        record = make_record(critic_mode="rounds", max_rounds=1, extra_cards=self.cards((1, "revise")))
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")])
        self.assertEqual(kinds(self.plan(room=room, record=record, detail=detail)),
                         ["mark_ingest"])

    def test_rounds_mode_two_rounds(self):
        room = make_room(critic_mode="rounds", max_rounds=2)
        record = make_record(critic_mode="rounds", max_rounds=2,
                             extra_cards=self.cards((1, "revise")))
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")])
        self.assertEqual(kinds(self.plan(room=room, record=record, detail=detail)),
                         ["create_critic"])


class DeliveryHoldTest(unittest.TestCase):
    """Гейт: в режиме «до одобрения» ответ не публикуется до approve."""

    def test_hold_until_approved(self):
        record = make_record(critic_rounds=[round_entry(1, "revise", [ISSUE_A])])
        self.assertTrue(critic_loop.delivery_hold(make_room(), record, make_detail()))

    def test_no_hold_after_approve(self):
        record = make_record(critic_rounds=[round_entry(1, "approve", [])])
        self.assertFalse(critic_loop.delivery_hold(make_room(), record, make_detail()))

    def test_no_hold_after_stop(self):
        record = make_record(critic_rounds=[round_entry(1, "revise", [ISSUE_A])],
                             critic_stop={"kind": "cap", "round": 12})
        self.assertFalse(critic_loop.delivery_hold(make_room(), record, make_detail()))

    def test_no_hold_in_rounds_mode(self):
        record = make_record(critic_mode="rounds",
                             critic_rounds=[round_entry(1, "revise", [ISSUE_A])])
        self.assertFalse(critic_loop.delivery_hold(make_room(critic_mode="rounds"),
                                                   record, make_detail()))

    def test_no_hold_without_critic(self):
        record = make_record(critic=False, critic_rounds=[round_entry(1, "revise", [ISSUE_A])])
        self.assertFalse(critic_loop.delivery_hold(make_room(), record, make_detail()))

    def test_no_hold_without_synthesis(self):
        detail = make_detail(state="pending", answer="")
        record = make_record(critic_rounds=[round_entry(1, "revise", [ISSUE_A])])
        self.assertFalse(critic_loop.delivery_hold(make_room(), record, detail))


class ProgressTest(unittest.TestCase):
    def test_counts_closed_and_open(self):
        history = [round_entry(1, "revise", [ISSUE_A, ISSUE_B]),
                   round_entry(2, "revise", [ISSUE_B]),
                   round_entry(3, "revise", [ISSUE_B, ISSUE_A])]
        record = make_record(critic_rounds=history)
        got = critic_loop.progress(record, make_detail())
        self.assertEqual(got["round"], 3)
        self.assertEqual(got["rounds"], 3)
        self.assertEqual(got["issues_before"], 2)
        self.assertEqual(got["open"], 2)
        self.assertEqual(got["closed"], 0)

    def test_reports_closed_items(self):
        history = [round_entry(1, "revise", [ISSUE_A, ISSUE_B]),
                   round_entry(2, "revise", [ISSUE_B])]
        got = critic_loop.progress(make_record(critic_rounds=history), make_detail())
        self.assertEqual(got["closed"], 1)
        self.assertEqual(got["open"], 1)
        self.assertTrue(got["closed_items"])
        self.assertIn("себестоимость", got["closed_items"][0])

    def test_empty_history(self):
        got = critic_loop.progress(make_record(), make_detail())
        self.assertEqual((got["round"], got["issues_before"], got["open"]), (0, 0, 0))


class FakeRegistry:
    def __init__(self):
        self.updated: list[dict] = []
        self.cards: list[dict] = []

    def update(self, cfg, room_id, root_id, patch):
        self.updated.append(patch)

    def add_extra_card(self, cfg, room_id, root_id, card):
        self.cards.append(card)


class FakeEngine:
    def __init__(self):
        self.calls: list[dict] = []
        self.counter = 0

    def create_child_card(self, cfg, room, board, parent_id, assignee, title, body, kind,
                          idem_key=None):
        self.counter += 1
        card_id = "t_auto%03d" % self.counter
        self.calls.append({"fn": "create_child_card", "kind": kind, "assignee": assignee,
                           "title": title, "idem_key": idem_key, "task_id": card_id,
                           "parent_id": parent_id})
        return card_id

    def retarget_root(self, cfg, board, root_id, profile):
        self.calls.append({"fn": "retarget_root", "profile": profile})
        return {"ok": True, "detail": "назначен", "cmd_ms": 1, "changed": True}


class RunStepModeTest(unittest.TestCase):
    def setUp(self):
        self._orig_engine = critic_loop._engine
        self._orig_registry = critic_loop._REG
        self.engine = FakeEngine()
        self.registry = FakeRegistry()
        critic_loop._engine = self.engine
        critic_loop.set_registry(self.registry)
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-critic-mode-")
        base = Path(self._tmp.name)
        (base / "state").mkdir(parents=True, exist_ok=True)
        self.cfg = SimpleNamespace(state_dir=base / "state", vault_dir=base / "vault",
                                   reports_dir=base / "reports", reportkit_dir=ROOT / "reportkit",
                                   tmp_dir=base / "tmp", log_dir=base / "logs")

    def tearDown(self):
        critic_loop._engine = self._orig_engine
        critic_loop.set_registry(self._orig_registry)
        self._tmp.cleanup()

    def journal(self):
        path = Path(self.cfg.state_dir) / "cafe" / "journal.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_stop_writes_registry_and_journal(self):
        history = [round_entry(n, "revise", [ISSUE_A]) for n in (1, 2, 3)]
        record = make_record(critic_rounds=history, extra_cards=[
            {"id": "t_crit003", "kind": "critic", "round": 3, "at": 30},
            {"id": "t_rev0003", "kind": "revision", "round": 3, "at": 31}])
        detail = make_detail(children=[
            node("t_crit003", "done", metadata={"verdict": "revise", "issues": [ISSUE_A]}),
            node("t_rev0003", "done", assignee="biz-director", summary="Версия 4")])
        results = critic_loop.run_step(self.cfg, make_room(), record, detail)
        self.assertEqual([r["kind"] for r in results], ["stop_no_progress"])
        self.assertTrue(results[0]["ok"])
        stop_patch = [u for u in self.registry.updated if "critic_stop" in u]
        self.assertTrue(stop_patch)
        self.assertEqual(stop_patch[0]["critic_stop"]["kind"], "no_progress")
        self.assertTrue(stop_patch[0]["ingest_pending"])
        self.assertTrue(any("остановлен" in e["text"] for e in self.journal()))

    def test_pause_and_retry_flow(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        failed = node(CRITIC_ID, "failed", error={"code": "failed", "message": "429"})
        detail = make_detail(children=[failed])
        first = critic_loop.run_step(self.cfg, make_room(), record, detail)
        self.assertEqual([r["kind"] for r in first], ["pause_rate_limit"])
        self.assertTrue(any("critic_pause" in u for u in self.registry.updated))

        paused = dict(record, critic_pause={"kind": "critic", "round": 1,
                                            "until": 1, "attempt": 0})
        second = critic_loop.run_step(self.cfg, make_room(), paused, detail)
        self.assertEqual([r["kind"] for r in second], ["retry_card"])
        call = self.engine.calls[-1]
        self.assertEqual(call["kind"], "critic")
        self.assertEqual(call["idem_key"], "critic-%s-r1-a1" % ROOT_ID)

    def test_history_is_written_to_registry(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise", "issues": [ISSUE_A]})])
        critic_loop.run_step(self.cfg, make_room(), record, detail)
        written = [u["critic_rounds"] for u in self.registry.updated if "critic_rounds" in u]
        self.assertTrue(written)
        self.assertEqual(written[0][0]["verdict"], "revise")
        self.assertEqual(written[0][0]["round"], 1)


class QuestionFilterTest(unittest.TestCase):
    """§7: вопрос «сколько раундов/прогонов критика» не показывается."""

    def test_matches_rounds_question(self):
        for text in ("Сколько прогонов через критика делать?",
                     "Сколько раундов критика нужно?",
                     "Уточните: сколько раз проверять критиком?",
                     "Какой лимит раундов критика?"):
            self.assertTrue(_is_critic_rounds_question(text), text)

    def test_ignores_other_questions(self):
        for text in ("Добавляем ли биз-финансиста?",
                     "Порог маржи какой?",
                     "Сколько стоит доставка?",
                     "",
                     None):
            self.assertFalse(_is_critic_rounds_question(text), text)


class SandboxCycleTest(unittest.TestCase):
    """Настоящий цикл в песочнице: карточки критика/доработки, подставные вердикты."""

    @classmethod
    def setUpClass(cls):
        import contextlib
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls._stack.close()

    def _cli(self, *args: str) -> str:
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB",
                            "HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD")}
        env["HERMES_KANBAN_HOME"] = str(self.sb.kanban_home)
        proc = subprocess.run([self.cfg.hermes_cli, "kanban", "--board", "team", *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return proc.stdout

    def test_two_rounds_until_approve(self):
        tree = self.sb.state("done_modern")
        root_id = tree["root_id"]
        room = make_room()
        record = {"root_id": root_id, "slug": tree["slug"], "text": REQUEST_TEXT,
                  "critic": True, "extra_cards": [], "retargeted": True,
                  "critic_mode": "until_approved", "critic_round_cap": 12, "max_rounds": 1}
        registry = FakeRegistry()
        critic_loop.set_registry(registry)
        try:
            detail = {"status": "done", "text": REQUEST_TEXT,
                      "tree": [node(root_id, "done", assignee="biz-director", role="root")],
                      "critic": None, "timeline": [{"kind": "decomposed"}],
                      "synthesis": {"state": "ready", "text_md": ANSWER_MD}}
            self.assertTrue(critic_loop.delivery_hold(room, record, detail))

            first = critic_loop.run_step(self.cfg, room, record, detail)
            self.assertEqual([r["kind"] for r in first], ["create_critic"])
            critic1 = first[0]["task_id"]
            self._cli("complete", critic1, "--summary", "Вердикт: revise, пересчитать маржу.",
                      "--metadata", json.dumps({"verdict": "revise", "issues": [ISSUE_A]},
                                               ensure_ascii=False))
            record["extra_cards"] = registry.cards[:]
            detail2 = {"status": "done", "text": REQUEST_TEXT,
                       "tree": [node(root_id, "done", assignee="biz-director", role="root"),
                                node(critic1, "done", assignee="biz-critic",
                                     metadata={"verdict": "revise", "issues": [ISSUE_A]})],
                       "critic": None, "timeline": [{"kind": "decomposed"}],
                       "synthesis": {"state": "ready", "text_md": ANSWER_MD}}
            second = critic_loop.run_step(self.cfg, room, record, detail2)
            self.assertEqual([r["kind"] for r in second], ["create_revision"])
            revision1 = second[0]["task_id"]
            self._cli("complete", revision1, "--summary", "Версия 2: маржа пересчитана.")
            record["extra_cards"] = registry.cards[:]
            # история раундов уже в реестре после второго шага
            record["critic_rounds"] = [e for e in
                                       [u.get("critic_rounds") for u in registry.updated]
                                       if e][-1]

            detail3 = {"status": "done", "text": REQUEST_TEXT,
                       "tree": [node(root_id, "done", assignee="biz-director", role="root"),
                                node(critic1, "done", assignee="biz-critic",
                                     metadata={"verdict": "revise", "issues": [ISSUE_A]}),
                                node(revision1, "done", assignee="biz-director",
                                     summary="Версия 2: маржа пересчитана.")],
                       "critic": None, "timeline": [{"kind": "decomposed"}],
                       "synthesis": {"state": "ready", "text_md": ANSWER_MD}}
            third = critic_loop.run_step(self.cfg, room, record, detail3)
            self.assertEqual([r["kind"] for r in third], ["create_critic"])
            self.assertEqual(third[0]["round"], 2)
            critic2 = third[0]["task_id"]
            self.assertNotEqual(critic2, critic1)
            row = self.sb.one("SELECT title, assignee FROM tasks WHERE id=?", (critic2,))
            self.assertEqual(row["assignee"], "biz-critic")
            self.assertIn("раунд 2", row["title"])

            self._cli("complete", critic2, "--summary", "Вердикт: approve.",
                      "--metadata", json.dumps({"verdict": "approve", "issues": []},
                                               ensure_ascii=False))
            record["extra_cards"] = registry.cards[:]
            detail4 = {"status": "done", "text": REQUEST_TEXT,
                       "tree": [node(root_id, "done", assignee="biz-director", role="root"),
                                node(critic1, "done", assignee="biz-critic",
                                     metadata={"verdict": "revise", "issues": [ISSUE_A]}),
                                node(revision1, "done", assignee="biz-director",
                                     summary="Версия 2: маржа пересчитана."),
                                node(critic2, "done", assignee="biz-critic",
                                     metadata={"verdict": "approve", "issues": []})],
                       "critic": None, "timeline": [{"kind": "decomposed"}],
                       "synthesis": {"state": "ready", "text_md": ANSWER_MD}}
            self.assertEqual(critic_loop.run_step(self.cfg, room, record, detail4), [])
            # история раундов записана в реестр: последний раунд — approve
            record["critic_rounds"] = [e for e in
                                       [u.get("critic_rounds") for u in registry.updated]
                                       if e][-1]
            self.assertEqual([e["verdict"] for e in record["critic_rounds"]],
                             ["revise", "approve"])
            self.assertFalse(critic_loop.delivery_hold(room, record, detail4))
        finally:
            critic_loop.set_registry(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
