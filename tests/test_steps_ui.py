# -*- coding: utf-8 -*-
"""W14-H STEPS-UI — «Следующие шаги»: план руководителя и реакция пользователя (п. 12).

Что проверяется (``WAVE14-RULES.md`` пакет H §1–§4):
  * шаг — не галочка пользователя: статус ведёт руководитель, а пользователь
    ВЛИЯЕТ на план действиями ``step_agree`` / ``step_comment`` / ``step_move`` /
    ``step_drop``;
  * каждое действие уходит руководителю КАК ОТВЕТ НА ЭТОТ ШАГ (общий механизм
    §5/§6): метка ``[Михаил · … шаг ns_x «…»]`` в ветке + запись в реестре;
  * видимая реакция: «принято / учтено / срок изменён / порядок изменён / шаг убран»,
    а до реакции — «руководитель ещё не ответил»;
  * «Переместить» меняет срок/исполнителя/порядок и в шаге, и в бэклоге комнаты;
  * у шага есть «зачем» (вопрос/ответ-источник), история и метка
    «противоречит вашему ответу» (из W14-D §4).

Только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён shim'ом; реальная
доска и реальный Hermes сверяются до/после.

    python -m unittest tests.test_steps_ui -v

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
LEAD = "biz-director"

QUESTION = "Добавляем ли биз-финансиста (biz-cfo) и геолога в команду?"
STEP_TEXT = "Решить по составу команды: добавить biz-cfo и/или геолога"
STEP_OTHER = "Проверить состав и аллергены на этикетке по СанПиН до первой партии"


class CliShim:
    """subprocess-подмена: пишем argv, ничего не запускаем (см. tests/test_answers.py)."""

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
    rec = {"root_id": root_id, "slug": "r-20261105-1200-stp1", "title": "Состав команды",
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

    # --- помощники ---------------------------------------------------------
    def rid(self) -> str:
        """Свой запрос: корень с завершённым run'ом, вопрос и два шага в metadata."""
        root = self.sa.card("Собери команду под Россию", LEAD, status="running")
        metadata = {
            "open_questions": [{"text": QUESTION, "for": "mikhail"}],
            "next_steps": [{"text": STEP_TEXT, "owner": "default", "due": "2026-11-07"},
                           {"text": STEP_OTHER, "owner": "biz-lawyer"}],
        }
        self.sa.finish(root, "Команда собрана, нужен ответ по составу.", metadata)
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

    def step(self, rid: str, index: int = 0) -> dict:
        return self.detail(rid)["next_steps"][index]

    def backlog_item(self, room: str, rid: str, step_id: str) -> dict:
        status, payload, _ = self.app.dispatch(
            "GET", "/api/rooms/%s/backlog" % room, {}, {})
        self.assertEqual(status, 200, payload)
        item_id = "%s:%s" % (rid, step_id)
        item = next((i for i in payload["items"] if i.get("id") == item_id), None)
        self.assertIsNotNone(item, [i.get("id") for i in payload["items"]])
        return item


class StepAgreeTests(_Base):
    """§12: «Согласен» — это ответ на шаг, а не отметка выполнения."""

    def test_agree_goes_to_leader_as_answer_to_step(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_agree", step_id=step["id"])
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "step_agree")
        self.assertEqual(payload["step_id"], step["id"])
        self.assertEqual(payload["step"]["state"], "open", "статус шага ведёт руководитель")
        self.assertEqual(payload["step"]["user_action"]["kind"], "agree")
        self.assertEqual(payload["step"]["user_action_label"], "согласовано вами")
        self.assertEqual(payload["step"]["reaction_label"], answers_mod.PENDING_RU)
        self.assertTrue(payload["step"]["reaction_pending"])

        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.STEP_AGREE_MARK) and step["id"] in t
                            for t in texts), texts)
        self.assertTrue(any("«" + STEP_TEXT[:40] in t for t in texts), texts)
        stored = registry.get(self.cfg, ROOM, rid)
        self.assertEqual(stored["step_actions"][step["id"]]["kind"], "agree")
        self.assertEqual(stored["steps_log"][-1]["step_id"], step["id"])

    def test_agree_is_visible_on_read(self):
        rid = self.rid()
        step = self.step(rid)
        self.actions(rid, action="step_agree", step_id=step["id"])
        fresh = self.step(rid)
        self.assertEqual(fresh["user_action"]["kind"], "agree")
        self.assertEqual(fresh["user_action_label"], "согласовано вами")
        self.assertTrue(fresh["reaction_pending"])
        self.assertEqual(fresh["history"][-1]["kind"], "agree")

    def test_leader_reaction_marks_step_received(self):
        rid = self.rid()
        step = self.step(rid)
        self.actions(rid, action="step_agree", step_id=step["id"])
        self.sa.comment(rid, LEAD, "%s %s] Принято, шаг оставляем"
                        % (answers_mod.STEP_REACTION_MARK, step["id"]))
        fresh = self.step(rid)
        self.assertFalse(fresh["reaction_pending"])
        self.assertEqual(fresh["reaction_status"], "received")
        self.assertEqual(fresh["reaction_label"], "принято")
        self.assertEqual(fresh["reaction"]["text"], "Принято, шаг оставляем")
        self.assertEqual(fresh["history"][-1]["kind"], "reaction")

    def test_reaction_applied_and_no_change(self):
        rid = self.rid()
        step = self.step(rid)
        self.actions(rid, action="step_agree", step_id=step["id"])
        self.sa.comment(rid, LEAD, "%s %s] Учтено: карточки t_e9c6c2b2 пересмотрены"
                        % (answers_mod.STEP_REACTION_MARK, step["id"]))
        fresh = self.step(rid)
        self.assertEqual(fresh["reaction_status"], "applied")
        self.assertEqual(fresh["reaction_label"], "учтено: работа пересмотрена")
        self.assertEqual(fresh["reaction"]["cards"], ["t_e9c6c2b2"])

    def test_agree_on_unknown_step_is_404(self):
        rid = self.rid()
        status, payload, _ = self.actions(rid, action="step_agree", step_id="ns_99")
        self.assertEqual(status, 404, payload)

    def test_agree_requires_step_id(self):
        rid = self.rid()
        status, payload, _ = self.actions(rid, action="step_agree")
        self.assertEqual(status, 400, payload)
        self.assertIn("step_id", payload["error"]["detail"]["missing"])


class StepCommentTests(_Base):
    """§12: «Прокомментировать» — текст уходит руководителю вместе с шагом."""

    def test_comment_text_reaches_leader(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_comment", step_id=step["id"],
                                          text="Сначала уточните страну у Михаила")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["step"]["user_action"]["kind"], "comment")
        self.assertEqual(payload["step"]["user_action"]["text"], "Сначала уточните страну у Михаила")
        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.STEP_COMMENT_MARK)
                            and "Сначала уточните страну" in t for t in texts), texts)
        fresh = self.step(rid)
        self.assertEqual(fresh["user_action"]["text"], "Сначала уточните страну у Михаила")
        self.assertEqual(fresh["reaction_label"], answers_mod.PENDING_RU)

    def test_comment_needs_text(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_comment", step_id=step["id"])
        self.assertEqual(status, 400, payload)


class StepMoveTests(_Base):
    """§12: «Переместить» — срок, исполнитель и порядок; видно «срок изменён»."""

    def test_move_changes_due_everywhere(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_move", step_id=step["id"],
                                          due="2026-12-01")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["step"]["due"], "2026-12-01")
        self.assertTrue(payload["step"]["due_changed_by_user"])
        self.assertEqual(payload["step"]["reaction_label"], answers_mod.PENDING_RU)
        fresh = self.step(rid)
        self.assertEqual(fresh["due"], "2026-12-01", "срок из реестра виден на чтении")
        item = self.backlog_item(ROOM, rid, step["id"])
        self.assertEqual(item["due"], "2026-12-01", "срок обновлён и в бэклоге комнаты")
        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.STEP_MOVE_MARK) and "2026-12-01" in t
                            for t in texts), texts)

    def test_move_reaction_says_due_changed(self):
        rid = self.rid()
        step = self.step(rid)
        self.actions(rid, action="step_move", step_id=step["id"], due="2026-12-01")
        self.sa.comment(rid, LEAD, "%s %s] Срок изменён на 01.12"
                        % (answers_mod.STEP_REACTION_MARK, step["id"]))
        fresh = self.step(rid)
        self.assertEqual(fresh["reaction_status"], "moved")
        self.assertEqual(fresh["reaction_label"], "срок изменён")

    def test_move_changes_owner(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_move", step_id=step["id"],
                                          owner="biz-lawyer")
        self.assertEqual(status, 200, payload)
        fresh = self.step(rid)
        self.assertEqual(fresh["owner"], "biz-lawyer")
        self.assertTrue(fresh["owner_changed_by_user"])

    def test_move_reorders_steps(self):
        rid = self.rid()
        first = self.detail(rid)["next_steps"][0]
        status, payload, _ = self.actions(rid, action="step_move", step_id=first["id"],
                                          direction="down")
        self.assertEqual(status, 200, payload)
        fresh = self.detail(rid)["next_steps"]
        self.assertEqual(fresh[0]["text"], STEP_OTHER, "шаг ушёл вниз")
        self.assertEqual(fresh[1]["id"], first["id"])
        self.assertEqual([s["order"] for s in fresh], [0, 1])

    def test_move_up_first_step_is_noop_but_saved(self):
        rid = self.rid()
        first = self.detail(rid)["next_steps"][0]
        status, payload, _ = self.actions(rid, action="step_move", step_id=first["id"],
                                          direction="up")
        self.assertEqual(status, 200, payload)
        fresh = self.detail(rid)["next_steps"]
        self.assertEqual(fresh[0]["id"], first["id"])

    def test_move_without_changes_is_400(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_move", step_id=step["id"])
        self.assertEqual(status, 400, payload)

    def test_move_bad_due_is_400(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_move", step_id=step["id"],
                                          due="01.12.2026")
        self.assertEqual(status, 400, payload)

    def test_move_bad_direction_is_400(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_move", step_id=step["id"],
                                          direction="left")
        self.assertEqual(status, 400, payload)


class StepDropTests(_Base):
    """§12: «Не согласен — убрать» — шаг убран, руководитель извещён."""

    def test_drop_removes_step_and_notifies_leader(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_drop", step_id=step["id"])
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["step"]["state"], "dismissed")
        self.assertEqual(payload["step"]["reaction_label"], "шаг убран")
        self.assertIn(step["id"], payload["dropped"])
        fresh = self.step(rid)
        self.assertEqual(fresh["state"], "dismissed")
        self.assertEqual(fresh["state_label"], "убран")
        item = self.backlog_item(ROOM, rid, step["id"])
        self.assertEqual(item["status"], "dismissed")
        texts = self.shim.comment_texts()
        self.assertTrue(any(t.startswith(answers_mod.STEP_DROP_MARK) for t in texts), texts)

    def test_drop_accepts_backlog_style_id(self):
        rid = self.rid()
        step = self.step(rid)
        status, payload, _ = self.actions(rid, action="step_drop",
                                          step_id="%s:%s" % (rid, step["id"]))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["step_id"], step["id"])


class StepContextTests(_Base):
    """§2/§4: зачем нужен шаг (вопрос/ответ-источник) и метка конфликта."""

    def test_step_has_question_link_and_history(self):
        rid = self.rid()
        detail = self.detail(rid)
        step = detail["next_steps"][0]
        self.assertIn("question", step)
        self.assertIn("question_link", step)
        self.assertIn("history", step)
        self.assertIn("can_agree", step)
        self.assertTrue(step["can_agree"])

    def test_conflicting_step_keeps_marker_for_ui(self):
        root = self.sa.card("Собери команду под Россию", LEAD, status="running")
        metadata = {"open_questions": [{"text": QUESTION, "for": "mikhail"}],
                    "next_steps": [{"text": STEP_TEXT, "owner": "default",
                                    "from_question": "q_00000001"}]}
        self.sa.finish(root, "Нужен ответ.", metadata)
        registry.add_request(self.cfg, ROOM, _record(root))
        detail = self.detail(root)
        qid = detail["open_questions"][0]["id"]
        self.actions(root, action="answer", question_id=qid,
                     text="Нет, никакого финансиста не добавляем")
        after = self.detail(root)
        step = after["next_steps"][0]
        self.assertTrue(step["conflicts_with_answer"] or step["cancelled_by_answer"], step)
        self.assertTrue(step["can_remove"])


class StepFormatTests(unittest.TestCase):
    """Чистые функции: метки, статусы, реакция (без сервера)."""

    def test_marks(self):
        self.assertEqual(answers_mod.step_comment("agree", "ns_1", "Шаг"),
                         "[Михаил · согласен с шагом ns_1 «Шаг»]")
        self.assertEqual(answers_mod.step_comment("comment", "ns_2", "Шаг", "Текст"),
                         "[Михаил · комментарий к шагу ns_2 «Шаг»] Текст")
        self.assertIn("срок 2026-12-01",
                      answers_mod.step_comment("move", "ns_3", "Шаг", due="2026-12-01"))
        self.assertIn("выше", answers_mod.step_comment("move", "ns_3", "Шаг", direction="up"))

    def test_step_id_parsing(self):
        self.assertEqual(answers_mod.step_id_of("[Михаил · убрать шаг ns_12 «текст»]"), "ns_12")
        self.assertEqual(answers_mod.step_id_of("[Михаил · ответ на вопрос q_1 «…»]"), "")

    def test_reaction_states(self):
        self.assertEqual(answers_mod.step_reaction_state("Принято, шаг убран"), "removed")
        self.assertEqual(answers_mod.step_reaction_state("Срок изменён"), "moved")
        self.assertEqual(answers_mod.step_reaction_state("Ничего менять не нужно"), "no_change")
        self.assertEqual(answers_mod.step_reaction_state("Принято"), "received")

    def test_labels(self):
        self.assertEqual(answers_mod.step_reaction_label("agree", "received"), "принято")
        self.assertEqual(answers_mod.step_reaction_label("agree", "applied"),
                         "учтено: работа пересмотрена")
        self.assertEqual(answers_mod.step_reaction_label("move", "received", True), "срок изменён")
        self.assertEqual(answers_mod.step_reaction_label("move", "received", False), "порядок изменён")
        self.assertEqual(answers_mod.step_reaction_label("drop", None), "шаг убран")
        self.assertEqual(answers_mod.step_reaction_label("comment", None), answers_mod.PENDING_RU)

    def test_sort_steps(self):
        steps = [{"id": "ns_1"}, {"id": "ns_2"}, {"id": "ns_3"}]
        out = answers_mod.sort_steps(steps, ["ns_3", "ns_1"])
        self.assertEqual([s["id"] for s in out], ["ns_3", "ns_1", "ns_2"])
        self.assertEqual([s["order"] for s in out], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
