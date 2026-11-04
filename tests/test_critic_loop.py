# -*- coding: utf-8 -*-
"""Тесты цикла критика (server/critic_loop.py): таблица состояний plan() и run_step().

plan() — чистая функция, проверяется на синтетических detail/record (без БД).
run_step() — на подменённом engine_write/registry (без CLI) и один интеграционный
прогон в песочнице с настоящими карточками.

Запуск из корня проекта:
    python -m unittest tests.test_critic_loop -v
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import contract, critic_loop, engine_write  # noqa: E402
from tests.helpers import Sandbox                        # noqa: E402

ROOT_ID = "t_root001"
CRITIC_ID = "t_crit001"
REVISION_ID = "t_rev0001"
REQUEST_TEXT = "Проверь «Алвара» — что делать?\r\nВторой абзац."
ANSWER_MD = "## Ответ\n\nМаржа 46%, точка безубыточности 71 набор."


def make_room(**over):
    room = {"id": "cafe", "title": "Лето", "board": "team", "tenant": "room:cafe",
            "orchestrator": "biz-director", "critic": "biz-critic",
            "members": [{"profile": "biz-lawyer", "role": "worker", "note": ""}],
            "context_files": [], "critic_pass": True}
    room.update(over)
    return room


def make_record(**over):
    record = {"root_id": ROOT_ID, "slug": "r-1", "text": REQUEST_TEXT, "critic": True,
              "extra_cards": [], "retargeted": False}
    record.update(over)
    return record


def node(task_id, status, *, assignee=None, role="worker", metadata=None, summary="", parents=()):
    return {"id": task_id, "status": status, "assignee": assignee, "role": role,
            "metadata": metadata or {}, "summary_md": summary, "parent_ids": list(parents)}


def make_detail(root_status="done", *, assignee="biz-director", children=(), answer=ANSWER_MD,
                state="ready", critic_info=None, timeline=None, text=REQUEST_TEXT):
    tree = [node(ROOT_ID, root_status, assignee=assignee, role="root")]
    tree.extend(children)
    return {"status": root_status, "text": text, "tree": tree, "critic": critic_info,
            "timeline": [{"kind": "decomposed"}] if timeline is None else timeline,
            "synthesis": {"state": state, "text_md": answer if state == "ready" else ""}}


def kinds(actions):
    return [a["kind"] for a in actions]


class PlanTest(unittest.TestCase):
    """Таблица состояний: что plan() делает в каждой ситуации."""

    def plan(self, room=None, record=None, detail=None, now=1793171000):
        return critic_loop.plan(room or make_room(), record or make_record(),
                                detail if detail is not None else make_detail(), now)

    # --- создание критика ----------------------------------------------------
    def test_root_done_creates_critic(self):
        actions = self.plan()
        self.assertEqual(kinds(actions), ["create_critic"])
        action = actions[0]
        self.assertEqual(action["assignee"], "biz-critic")
        self.assertEqual(action["parent_id"], ROOT_ID)
        self.assertEqual(action["round"], 1)

    def test_root_not_done_no_critic(self):
        self.assertEqual(self.plan(detail=make_detail("todo", assignee="biz-director")), [])

    def test_synthesis_pending_no_critic(self):
        self.assertEqual(self.plan(detail=make_detail(state="pending")), [])

    def test_critic_disabled_does_nothing(self):
        self.assertEqual(self.plan(record=make_record(critic=False)), [])
        self.assertEqual(self.plan(record=make_record(critic=False),
                                   detail=make_detail(state="pending")), [])

    def test_no_second_critic_while_first_works(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[node(CRITIC_ID, "todo", assignee="biz-critic")])
        self.assertEqual(self.plan(record=record, detail=detail), [])
        detail_run = make_detail(children=[node(CRITIC_ID, "running", assignee="biz-critic")])
        self.assertEqual(self.plan(record=record, detail=detail_run), [])

    # --- вердикты ------------------------------------------------------------
    def test_approve_does_nothing(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[node(CRITIC_ID, "done", assignee="biz-critic",
                                            metadata={"verdict": "approve"})])
        self.assertEqual(self.plan(record=record, detail=detail), [])

    def test_revise_creates_one_revision(self):
        issues = [{"where": "Срез 02-CFO", "problem": "нет роста какао", "fix": "пересчитать"}]
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[node(CRITIC_ID, "done", assignee="biz-critic",
                                            metadata={"verdict": "revise", "issues": issues})])
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_revision"])
        self.assertEqual(actions[0]["parent_id"], CRITIC_ID)
        self.assertEqual(actions[0]["round"], 1)
        self.assertEqual(actions[0]["issues"], issues)

    def test_revision_pending_no_duplicate(self):
        record = make_record(extra_cards=[
            {"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5},
            {"id": REVISION_ID, "kind": "revision", "round": 1, "at": 6}])
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise"}),
            node(REVISION_ID, "todo", assignee="biz-director")])
        self.assertEqual(self.plan(record=record, detail=detail), [])

    def test_revision_done_starts_next_round(self):
        record = make_record(max_rounds=2, extra_cards=[
            {"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5},
            {"id": REVISION_ID, "kind": "revision", "round": 1, "at": 6}])
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise"}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")])
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_critic"])
        self.assertEqual(actions[0]["round"], 2)

    def test_max_rounds_stops_cycle(self):
        record = make_record(max_rounds=1, extra_cards=[
            {"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5},
            {"id": REVISION_ID, "kind": "revision", "round": 1, "at": 6}])
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise"}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")])
        self.assertEqual(kinds(self.plan(record=record, detail=detail)), ["mark_ingest"])

    def test_max_rounds_is_clamped_to_two(self):
        record = make_record(max_rounds=9, extra_cards=[
            {"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5},
            {"id": REVISION_ID, "kind": "revision", "round": 1, "at": 6}])
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise"}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")])
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_critic"])
        self.assertEqual(actions[0]["round"], 2)

    def test_verdict_from_detail_critic(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        info = {"task_id": CRITIC_ID, "verdict": "revise", "round": 1,
                "issues": [{"where": "шаг 2", "problem": "нет срока", "fix": "указать"}]}
        detail = make_detail(children=[node(CRITIC_ID, "done", assignee="biz-critic")],
                             critic_info=info)
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_revision"])
        self.assertEqual(actions[0]["issues"][0]["fix"], "указать")

    def test_verdict_from_detail_when_node_missing(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[], critic_info={
            "enabled": True, "round": 1, "max_rounds": 1, "state": "revised",
            "verdict": "revise", "issues": [{"where": "шаг", "problem": "нет", "fix": "есть"}],
            "task_id": CRITIC_ID})
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_revision"])
        self.assertEqual(actions[0]["issues"][0]["fix"], "есть")

    def test_verdict_from_summary_fallback(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[node(CRITIC_ID, "done", assignee="biz-critic",
                                            summary="Вердикт: revise. Пересчитать маржу.")])
        self.assertEqual(kinds(self.plan(record=record, detail=detail)), ["create_revision"])

    def test_unknown_verdict_waits(self):
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[node(CRITIC_ID, "done", metadata={"verdict": "?"})])
        self.assertEqual(self.plan(record=record, detail=detail), [])

    def test_max_rounds_from_detail_critic_block(self):
        record = make_record(extra_cards=[
            {"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5},
            {"id": REVISION_ID, "kind": "revision", "round": 1, "at": 6}])
        detail = make_detail(children=[
            node(CRITIC_ID, "done", metadata={"verdict": "revise"}),
            node(REVISION_ID, "done", assignee="biz-director", summary="Версия 2")],
            critic_info={"enabled": True, "round": 1, "max_rounds": 2, "verdict": None,
                         "issues": [], "task_id": None})
        actions = self.plan(record=record, detail=detail)
        self.assertEqual(kinds(actions), ["create_critic"])
        self.assertEqual(actions[0]["round"], 2)

    def test_critic_without_profile_marks_ingest(self):
        actions = self.plan(room=make_room(critic=None))
        self.assertEqual(kinds(actions), ["mark_ingest"])

    # --- директор комнаты ----------------------------------------------------
    def test_retarget_root_once(self):
        room = make_room(orchestrator="default")
        detail = make_detail("todo", assignee="biz-director",
                             children=[node("t_child1", "ready", assignee="biz-lawyer")])
        actions = self.plan(room=room, detail=detail)
        self.assertEqual(kinds(actions), ["retarget_root"])
        self.assertEqual(actions[0]["profile"], "default")
        again = self.plan(room=room, record=make_record(retargeted=True), detail=detail)
        self.assertEqual(again, [])

    def test_retarget_skips_correct_assignee(self):
        room = make_room(orchestrator="biz-director")
        detail = make_detail("todo", assignee="biz-director",
                             children=[node("t_child1", "ready")])
        self.assertEqual(self.plan(room=room, detail=detail), [])

    def test_retarget_not_before_decomposition(self):
        room = make_room(orchestrator="default")
        detail = make_detail("todo", assignee="biz-director", children=[], timeline=[])
        self.assertEqual(self.plan(room=room, detail=detail), [])

    def test_retarget_ignores_done_root(self):
        room = make_room(orchestrator="default")
        detail = make_detail("done", assignee="biz-director",
                             children=[node(CRITIC_ID, "done", metadata={"verdict": "approve"})],
                             state="pending")
        self.assertEqual(self.plan(room=room, detail=detail), [])

    def test_plan_is_repeatable(self):
        room, record, detail = make_room(), make_record(), make_detail()
        first = self.plan(room=room, record=record, detail=detail)
        second = self.plan(room=room, record=record, detail=detail)
        self.assertEqual(first, second)
        self.assertEqual(kinds(first), ["create_critic"])


class FakeRegistry:
    def __init__(self):
        self.updated: list[dict] = []
        self.cards: list[dict] = []

    def update(self, cfg, room_id, root_id, patch):
        self.updated.append(patch)

    def add_extra_card(self, cfg, room_id, root_id, card):
        self.cards.append(card)


class FakeEngine:
    def __init__(self, fail: engine_write.EngineWriteError | None = None):
        self.calls: list[dict] = []
        self.fail = fail

    def create_child_card(self, cfg, room, board, parent_id, assignee, title, body, kind,
                          idem_key=None):
        self.calls.append({"fn": "create_child_card", "parent_id": parent_id,
                           "assignee": assignee, "title": title, "body": body,
                           "kind": kind, "idem_key": idem_key})
        if self.fail:
            raise self.fail
        return CRITIC_ID

    def retarget_root(self, cfg, board, root_id, profile):
        self.calls.append({"fn": "retarget_root", "root_id": root_id, "profile": profile})
        if self.fail:
            raise self.fail
        return {"ok": True, "detail": f"назначен {profile}", "cmd_ms": 7, "changed": True}


class RunStepTest(unittest.TestCase):
    def setUp(self):
        self._orig_engine = critic_loop._engine
        self._orig_registry = critic_loop._REG
        self.engine = FakeEngine()
        self.registry = FakeRegistry()
        critic_loop._engine = self.engine
        critic_loop.set_registry(self.registry)
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-critic-")
        base = Path(self._tmp.name)
        self.cfg = SimpleNamespace(vault_dir=base / "vault", reports_dir=base / "reports",
                                   reportkit_dir=ROOT / "reportkit", tmp_dir=base / "tmp",
                                   log_dir=base / "logs")

    def tearDown(self):
        critic_loop._engine = self._orig_engine
        critic_loop.set_registry(self._orig_registry)
        self._tmp.cleanup()

    def test_create_critic_card_and_record(self):
        results = critic_loop.run_step(self.cfg, make_room(), make_record(), make_detail())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["kind"], "create_critic")
        self.assertEqual(results[0]["task_id"], CRITIC_ID)
        self.assertTrue(results[0]["registry"])
        call = self.engine.calls[0]
        self.assertEqual(call["kind"], "critic")
        self.assertEqual(call["parent_id"], ROOT_ID)
        self.assertEqual(call["assignee"], "biz-critic")
        self.assertIn(REQUEST_TEXT, call["body"])
        self.assertIn(ANSWER_MD, call["body"])
        self.assertIn("verdict", call["body"])
        card = self.registry.cards[0]
        self.assertEqual((card["id"], card["kind"], card["round"]), (CRITIC_ID, "critic", 1))

    def test_create_revision_with_issues(self):
        issues = [{"where": "Срез CFO", "problem": "старая себестоимость", "fix": "пересчитать"}]
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[node(CRITIC_ID, "done", assignee="biz-critic",
                                            metadata={"verdict": "revise", "issues": issues})])
        results = critic_loop.run_step(self.cfg, make_room(), record, detail)
        self.assertEqual([r["kind"] for r in results], ["create_revision"])
        self.assertTrue(results[0]["ok"])
        call = self.engine.calls[0]
        self.assertEqual(call["kind"], "revision")
        self.assertEqual(call["parent_id"], CRITIC_ID)
        self.assertEqual(call["assignee"], "biz-director")
        self.assertIn("ЗАМЕЧАНИЯ КРИТИКА", call["body"])
        self.assertIn("пересчитать", call["body"])
        self.assertEqual(self.registry.cards[0]["kind"], "revision")

    def test_manual_critique_creates_card(self):
        """Ручной «Проверить ответ»: карточка критика + запись в реестр."""
        res = critic_loop.manual_critique(self.cfg, make_room(), make_record(), make_detail())
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "critique")
        self.assertEqual(res["task_id"], CRITIC_ID)
        self.assertEqual(self.engine.calls[0]["kind"], "critic")
        self.assertEqual(self.registry.cards[0]["kind"], "critic")

    def test_manual_revision_with_user_note(self):
        """Ручная «На доработку»: замечание Михаила в теле, доработка в реестре."""
        record = make_record(extra_cards=[{"id": CRITIC_ID, "kind": "critic", "round": 1, "at": 5}])
        detail = make_detail(children=[node(CRITIC_ID, "done", assignee="biz-critic",
                                            metadata={"verdict": "revise", "issues": []})],
                              critic_info={"enabled": True, "round": 1, "max_rounds": 1,
                                           "state": "revised", "verdict": "revise", "issues": [],
                                           "task_id": CRITIC_ID})
        res = critic_loop.manual_revision(self.cfg, make_room(), record, detail,
                                          text="Пересчитай маржу с доставкой")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "revise")
        self.assertEqual(self.engine.calls[0]["kind"], "revision")
        self.assertEqual(self.engine.calls[0]["parent_id"], CRITIC_ID)
        self.assertIn("Пересчитай маржу с доставкой", self.engine.calls[0]["body"])
        self.assertEqual(self.registry.cards[0]["kind"], "revision")

    def test_manual_actions_need_ready_synthesis(self):
        pending = make_detail(answer="", state="pending")
        with self.assertRaises(engine_write.EngineWriteError) as ctx:
            critic_loop.manual_critique(self.cfg, make_room(), make_record(), pending)
        self.assertEqual(ctx.exception.code, "conflict")

    def test_retarget_writes_flag(self):
        room = make_room(orchestrator="default")
        detail = make_detail("todo", assignee="biz-director",
                             children=[node("t_child1", "ready")])
        results = critic_loop.run_step(self.cfg, room, make_record(), detail)
        self.assertEqual([r["kind"] for r in results], ["retarget_root"])
        self.assertEqual(self.engine.calls[0]["profile"], "default")
        self.assertEqual(self.registry.updated, [{"retargeted": True}])

    def test_engine_error_is_reported_and_stops(self):
        self.engine.fail = engine_write.EngineWriteError("conflict", "нельзя сейчас", "stderr-tail")
        results = critic_loop.run_step(self.cfg, make_room(), make_record(), make_detail())
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"]["code"], "conflict")
        self.assertEqual(results[0]["error"]["message"], "нельзя сейчас")
        self.assertEqual(self.registry.cards, [])

    def test_without_registry_still_creates_card(self):
        critic_loop.set_registry(SimpleNamespace())   # модуль без update/add_extra_card
        results = critic_loop.run_step(self.cfg, make_room(), make_record(), make_detail())
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[0]["registry"])

    def test_no_actions_no_calls(self):
        self.assertEqual(critic_loop.run_step(self.cfg, make_room(),
                                              make_record(critic=False), make_detail()), [])
        self.assertEqual(self.engine.calls, [])


class SandboxIntegrationTest(unittest.TestCase):
    """Один настоящий прогон: критик и доработка создаются CLI в песочнице."""

    @classmethod
    def setUpClass(cls):
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

    def _detail(self, critic_node=None, answer=ANSWER_MD):
        children = [critic_node] if critic_node else []
        return make_detail(children=children, answer=answer)

    def test_critic_then_revision_cards_created(self):
        tree = self.sb.state("done_modern")
        root_id = tree["root_id"]
        room = make_room(orchestrator="biz-director", critic="biz-critic")
        record = {"root_id": root_id, "slug": tree["slug"], "text": REQUEST_TEXT,
                  "critic": True, "extra_cards": [], "retargeted": True, "max_rounds": 1}
        registry = FakeRegistry()
        critic_loop.set_registry(registry)
        try:
            detail = {"status": "done", "text": REQUEST_TEXT,
                      "tree": [node(root_id, "done", assignee="biz-director", role="root")],
                      "critic": None, "timeline": [{"kind": "decomposed"}],
                      "synthesis": {"state": "ready", "text_md": ANSWER_MD}}
            first = critic_loop.run_step(self.cfg, room, record, detail)
            self.assertEqual([r["kind"] for r in first], ["create_critic"])
            self.assertTrue(first[0]["ok"], first)
            critic_id = first[0]["task_id"]
            row = self.sb.one("SELECT status, assignee, tenant, body FROM tasks WHERE id=?",
                              (critic_id,))
            self.assertEqual(row["assignee"], "biz-critic")
            self.assertEqual(row["tenant"], "room:cafe")
            self.assertIn(row["status"], ("todo", "ready"))  # родитель done — карточка ждёт диспетчера
            self.assertIn(REQUEST_TEXT.replace("\r\n", "\n"), row["body"])
            self.assertIn('"verdict":"approve|revise"', row["body"])
            link = self.sb.one("SELECT parent_id FROM task_links WHERE child_id=?", (critic_id,))
            self.assertEqual(link["parent_id"], root_id)

            # критик завершился с вердиктом revise — движок кладёт metadata в run
            self._cli("complete", critic_id, "--summary", "Вердикт: revise, пересчитать маржу.",
                      "--metadata", json.dumps({"verdict": "revise",
                                                "issues": [{"where": "CFO",
                                                            "problem": "старая себестоимость",
                                                            "fix": "пересчитать с какао"}]},
                                               ensure_ascii=False))
            record["extra_cards"] = registry.cards[:]
            detail2 = self._detail(critic_node=node(
                critic_id, "done", assignee="biz-critic",
                metadata={"verdict": "revise",
                          "issues": [{"where": "CFO", "problem": "старая себестоимость",
                                      "fix": "пересчитать с какао"}]}))
            second = critic_loop.run_step(self.cfg, room, record, detail2)
            self.assertEqual([r["kind"] for r in second], ["create_revision"])
            self.assertTrue(second[0]["ok"], second)
            revision_id = second[0]["task_id"]
            revision = self.sb.one("SELECT assignee, tenant, body FROM tasks WHERE id=?",
                                   (revision_id,))
            self.assertEqual(revision["assignee"], "biz-director")
            self.assertEqual(revision["tenant"], "room:cafe")
            self.assertIn("пересчитать с какао", revision["body"])
            self.assertIn(contract.CONTRACT_OPEN, revision["body"])
            link2 = self.sb.one("SELECT parent_id FROM task_links WHERE child_id=?", (revision_id,))
            self.assertEqual(link2["parent_id"], critic_id)
        finally:
            critic_loop.set_registry(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
