# -*- coding: utf-8 -*-
"""W20-P1 PHASES-BE: этапы проекта предлагаются автоматически (F-2, контракт §1.7).

Что проверяется:

* руководитель присылает предложение этапов в `metadata.phases_proposal` ЛЮБОЙ своей
  карточки — приложение кладёт его в поле комнаты `phases_proposal` и **не трогает**
  уже заданные этапы пользователя (порядок, переименования, удаления, добавления);
* `POST /api/rooms/{id}/phases/proposal` — `accept` / `accept_selected` / `reject`:
  принятые этапы дописываются в конец, совпавшие по id или названию пропускаются,
  правки пользователя не затираются;
* повторное чтение той же карточки не сбрасывает решение (отклонённое не воскресает);
* `POST /api/rooms/{id}/phases/propose` — карточка руководителю «предложи этапы»
  (пока предложение ждёт решения — 409, без спама карточками);
* интеграция: карта проекта (`GET …/roadmap`) показывает этапы комнаты сразу после
  решения и само предложение, «От вас нужно» (`GET …/backlog`) — строку «Руководитель
  предлагает этапы», сводка комнаты — короткую подпись состояния.

Только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён shim'ом; реальная
доска и реальный Hermes сверяются до/после. Все имена, названия и тексты — вымышленные.

    python -m unittest tests.test_phases_proposal -v

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

import server.app as app_mod                  # noqa: E402
import server.engine_cli as engine_cli        # noqa: E402
import server.engine_write as ew              # noqa: E402
import server.ingest as ingest_mod            # noqa: E402
import server.phase_book as phases            # noqa: E402
import server.registry as registry            # noqa: E402
import server.rooms as rooms_mod              # noqa: E402
from server.http_util import ApiError         # noqa: E402
from tests.helpers import Sandbox             # noqa: E402

LEAD = "team-director"
WORKER = "team-cfo"
TITLE = "Кафе «Лето» — запуск"
USER_IDEA = {"id": "ideya", "title": "Идея и вывеска"}
USER_CHECK = {"id": "proverka", "title": "Проверка спроса и цен"}
PROPOSAL = [
    {"title": "Идея и вывеска", "description": "уже есть у человека"},
    {"title": "Проверка спроса", "description": "мини-опрос у стойки, 20 человек"},
    {"title": "Подготовка документов", "description": "договор аренды и меню"},
]


class CliShim:
    """subprocess-подмена: пишем argv в файл-тело, ничего не запускаем."""

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

    def creates(self) -> list[list[str]]:
        return [argv for argv in self.calls if "create" in argv]


class _Base(unittest.TestCase):
    """Песочница + приложение; комнаты создаются по одной на тест (id уникальный)."""

    _seq = 0

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.shim = CliShim()
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim
        cls.app = app_mod.App(cls.cfg, port=0)

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
    def room(self, phases=None, proposal=None, archived=False) -> dict:
        """Своя комната (вымышленная) с этапами и/или предложением."""
        type(self)._seq += 1
        room_id = "leto%d" % type(self)._seq
        room = rooms_mod.create_room(self.cfg, {
            "id": room_id, "title": TITLE, "orchestrator": LEAD,
            "members": [{"profile": WORKER}], "project_dir": "",
        })
        patch: dict = {}
        if phases is not None:
            patch["phases"] = phases
        if proposal is not None:
            patch["phases_proposal"] = proposal
        if patch:
            rooms_mod.set_phases_state(self.cfg, room_id, **patch)
        if archived:
            rooms_mod.archive_room(self.cfg, room_id)
        return rooms_mod.get_room(self.cfg, room_id) or room

    def detail(self, room_id: str) -> dict:
        status, payload, _ = self.app.dispatch("GET", "/api/rooms/%s" % room_id, {}, {})
        self.assertEqual(status, 200, payload)
        return payload

    def decide(self, room_id: str, **body):
        return self.app.dispatch("POST", "/api/rooms/%s/phases/proposal" % room_id, {}, body)

    def propose(self, room_id: str, **body):
        return self.app.dispatch("POST", "/api/rooms/%s/phases/propose" % room_id, {}, body)

    def capture(self, room: dict, items, at: int = 1770000000, by: str = LEAD,
                source: str = "t_1111aaaa") -> dict:
        """Приём metadata так, как это делает ingest (свежая комната под замком)."""
        info: dict = {}

        def compute(snapshot):
            got = phases.capture(snapshot, items, by=by, source=source, at=at)
            info.update(got)
            return {"phases_proposal": got["proposal"]} if got.get("applied") else {}

        rooms_mod.set_phases_state(self.cfg, room["id"], compute=compute)
        return info

    def meta_detail(self, items, at: int = 1770000000, assignee: str = LEAD,
                    root_id: str = "t_1111aaaa") -> dict:
        """detail запроса с metadata.phases_proposal на карточке руководителя."""
        return {"id": root_id, "title": "Этапы кафе", "status": "done",
                "synthesis": {"state": "pending"},
                "tree": [{"id": root_id, "status": "done", "assignee": assignee,
                          "finished_at": at,
                          "metadata": {"phases_proposal": items}}]}


# --------------------------------------------------------------- чистые правила
class NormTests(unittest.TestCase):
    """Форма предложения и подписи (без песочницы и сервера)."""

    def test_parse_tolerates_junk_and_makes_ids_from_titles(self):
        items, warnings = phases.parse_meta_proposal([
            {"title": "  Проверка   спроса ", "description": " опрос "},
            "Вывеска",
            {"title": ""},
            {"description": "без названия"},
            42,
        ])
        self.assertEqual([i["title"] for i in items], ["Проверка спроса", "Вывеска"])
        self.assertEqual(items[0]["id"], "проверка-спроса")
        self.assertEqual(items[0]["description"], "опрос")
        self.assertTrue(warnings)          # пустые пункты — в предупреждения, не в этапы

    def test_parse_accepts_prepared_proposal_shape(self):
        items, warnings = phases.parse_meta_proposal(
            {"state": "proposed", "items": [{"id": "a", "title": "Первый"}]})
        self.assertEqual([i["id"] for i in items], ["a"])
        self.assertEqual(warnings, [])

    def test_signature_changes_only_with_ids_and_titles(self):
        one = phases.norm_items(PROPOSAL)
        same = phases.norm_items(list(reversed(list(reversed(PROPOSAL)))))
        self.assertEqual(phases.signature(one), phases.signature(same))
        renamed = phases.norm_items([dict(PROPOSAL[0], title="Другое"), PROPOSAL[1]])
        self.assertNotEqual(phases.signature(one), phases.signature(renamed))

    def test_state_text_reads_humanly(self):
        proposal = {"state": "proposed", "items": [{"id": "a", "title": "Первый"}]}
        self.assertEqual(phases.state_text(proposal), "Руководитель предлагает 1 этап: Первый")
        many = {"state": "proposed", "items": [{"id": str(i), "title": "Этап %d" % i}
                                               for i in range(4)]}
        self.assertTrue(phases.state_text(many).endswith("…"))
        self.assertEqual(phases.state_text({"state": "rejected", "items": many["items"]}),
                         "Предложение этапов отклонено (4 этапа)")

    def test_decide_keeps_user_order_and_titles(self):
        user = [USER_IDEA, USER_CHECK]
        proposal = {"state": "proposed", "items": phases.norm_items(PROPOSAL)}
        got = phases.decide(user, proposal, "accept")
        self.assertEqual([p["id"] for p in got["phases"]],
                         ["ideya", "proverka", "подготовка-документов"])
        # название, заданное человеком, не затёрто названием из предложения
        self.assertEqual(got["phases"][1]["title"], "Проверка спроса и цен")
        self.assertEqual(got["added"], ["подготовка-документов"])
        self.assertEqual(got["proposal"]["state"], "accepted")

    def test_decide_rejects_unknown_and_empty_selection(self):
        proposal = {"state": "proposed", "items": phases.norm_items(PROPOSAL)}
        with self.assertRaises(phases.PhaseError):
            phases.decide([], proposal, "accept_selected", [])
        with self.assertRaises(phases.PhaseError):
            phases.decide([], proposal, "accept_selected", ["нет-такого"])

    def test_reconcile_marks_edited_phases_as_accepted(self):
        proposal = {"state": "proposed", "items": phases.norm_items(PROPOSAL[:2])}
        edited = [{"id": "ideya", "title": "Идея и вывеска"},
                  {"id": "проверка-спроса", "title": "Проверка спроса"}]
        self.assertEqual(phases.reconcile(edited, proposal)["state"], "accepted")
        other = [{"id": "ideya", "title": "Идея и вывеска"}]
        self.assertEqual(phases.reconcile(other, proposal)["state"], "proposed")


# ------------------------------------------------------------------- приём метаданных
class CaptureTests(_Base):
    """§1.7: metadata.phases_proposal принимается, этапы человека не затираются."""

    def test_metadata_from_any_card_lands_in_room_without_touching_phases(self):
        room = self.room(phases=[USER_IDEA, USER_CHECK])
        res = ingest_mod.ingest_request(self.cfg, room, self.meta_detail(PROPOSAL))
        self.assertEqual(res["phases"]["reason"], "proposed", res)
        fresh = rooms_mod.get_room(self.cfg, room["id"])
        self.assertEqual([p["id"] for p in fresh["phases"]], ["ideya", "proverka"])
        self.assertEqual([p["title"] for p in fresh["phases"]],
                         ["Идея и вывеска", "Проверка спроса и цен"])
        proposal = fresh["phases_proposal"]
        self.assertEqual(proposal["state"], "proposed")
        self.assertEqual(proposal["by"], LEAD)
        self.assertEqual(proposal["source"], "t_1111aaaa")
        self.assertEqual([i["title"] for i in proposal["items"]],
                         ["Идея и вывеска", "Проверка спроса", "Подготовка документов"])
        self.assertEqual(proposal["items"][1]["description"], "мини-опрос у стойки, 20 человек")

    def test_same_card_twice_is_unchanged(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        again = self.capture(room, PROPOSAL)
        self.assertFalse(again.get("applied"))
        self.assertEqual(again.get("reason"), "unchanged")

    def test_rejected_proposal_is_not_resurrected_by_rereading_card(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        status, payload, _ = self.decide(room["id"], action="reject")
        self.assertEqual(status, 200, payload)
        again = self.capture(room, PROPOSAL)      # карточка прочитана снова
        self.assertFalse(again.get("applied"))
        self.assertEqual(again.get("reason"), "unchanged")
        fresh = rooms_mod.get_room(self.cfg, room["id"])
        self.assertEqual(fresh["phases_proposal"]["state"], "rejected")
        self.assertEqual(fresh["phases"], [])

    def test_proposal_equal_to_user_phases_is_not_shown(self):
        items = [{"title": "Идея и вывеска"}, {"title": "Проверка спроса и цен"}]
        room = self.room(phases=[USER_IDEA, USER_CHECK])
        got = self.capture(room, items)
        self.assertEqual(got.get("reason"), "same_as_phases")
        self.assertIsNone(rooms_mod.get_room(self.cfg, room["id"])["phases_proposal"])

    def test_journal_has_proposal_event(self):
        room = self.room()
        ingest_mod.ingest_request(self.cfg, room, self.meta_detail(PROPOSAL))
        events = [e for e in self._journal(room["id"]) if e.get("kind") == "phases_proposed"]
        self.assertTrue(events, "в журнале комнаты должно быть событие предложения")
        self.assertIn("Подготовка документов", events[0]["text"])

    def _journal(self, room_id: str) -> list:
        path = Path(self.cfg.state_dir) / room_id / "journal.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------- решения
class DecisionTests(_Base):
    """§1.7: accept / accept_selected / reject и сохранение правок человека."""

    def _pending(self, phases_list=None, items=None) -> dict:
        room = self.room(phases=phases_list or [])
        self.capture(room, items or PROPOSAL)
        return rooms_mod.get_room(self.cfg, room["id"])

    def test_accept_appends_new_phases_in_proposal_order(self):
        room = self._pending([USER_IDEA, USER_CHECK])
        status, payload, _ = self.decide(room["id"], action="accept")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["state"], "accepted")
        self.assertEqual([p["id"] for p in payload["phases"]],
                         ["ideya", "proverka", "подготовка-документов"])
        self.assertEqual(payload["phases"][1]["title"], "Проверка спроса и цен")
        self.assertEqual(payload["added"], ["подготовка-документов"])
        self.assertEqual(payload["room"]["phases_proposal"]["state"], "accepted")

    def test_accept_selected_takes_only_chosen(self):
        room = self._pending([])
        items = {i["title"]: i["id"] for i in room["phases_proposal"]["items"]}
        chosen = [items["Проверка спроса"], items["Подготовка документов"]]
        status, payload, _ = self.decide(room["id"], action="accept_selected", ids=chosen)
        self.assertEqual(status, 200, payload)
        self.assertEqual([p["id"] for p in payload["phases"]], chosen)
        self.assertEqual(payload["proposal"]["accepted_ids"], chosen)

    def test_reject_keeps_phases(self):
        room = self._pending([USER_IDEA])
        status, payload, _ = self.decide(room["id"], action="reject")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["state"], "rejected")
        self.assertEqual([p["id"] for p in payload["phases"]], ["ideya"])
        self.assertEqual(payload["added"], [])
        self.assertTrue(payload["proposal"]["decided_at"])

    def test_repeat_action_is_not_a_second_change(self):
        room = self._pending([])
        self.decide(room["id"], action="accept")
        status, payload, _ = self.decide(room["id"], action="accept")
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["changed"])
        self.assertEqual(len(payload["phases"]), len(room["phases_proposal"]["items"]))

    def test_conflicting_decision_is_refused(self):
        room = self._pending([])
        self.decide(room["id"], action="reject")
        with self.assertRaises(ApiError) as ctx:
            self.decide(room["id"], action="accept")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.detail.get("reason"), "already_decided")

    def test_no_proposal_is_conflict(self):
        room = self.room()
        with self.assertRaises(ApiError) as ctx:
            self.decide(room["id"], action="accept")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.detail.get("reason"), "no_proposal")

    def test_bad_request_cases(self):
        room = self._pending([])
        with self.assertRaises(ApiError) as ctx:
            self.decide(room["id"], action="принять")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.decide(room["id"], action="accept_selected", ids=[])
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.decide(room["id"], action="accept_selected", ids=["нет-такого"])
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.detail.get("unknown"), ["нет-такого"])

    def test_archived_room_refuses_decisions(self):
        room = self._pending([])
        rooms_mod.archive_room(self.cfg, room["id"])
        with self.assertRaises(ApiError) as ctx:
            self.decide(room["id"], action="accept")
        self.assertEqual(ctx.exception.status, 409)

    def test_user_edit_that_matches_proposal_accepts_it(self):
        room = self._pending([])
        titles = [{"id": item["id"], "title": item["title"]}
                  for item in room["phases_proposal"]["items"]]
        status, payload, _ = self.app.dispatch(
            "PATCH", "/api/rooms/%s" % room["id"], {}, {"phases": titles})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["phases_proposal"]["state"], "accepted")
        self.assertEqual([p["id"] for p in payload["phases"]],
                         [item["id"] for item in room["phases_proposal"]["items"]])


# ------------------------------------------------------- карточка «предложи этапы»
class ProposeTests(_Base):
    """§1.7: `POST …/phases/propose` ставит карточку руководителю."""

    def test_propose_creates_card_for_leader_with_format(self):
        room = self.room(phases=[USER_IDEA])
        status, payload, _ = self.propose(room["id"], note="Учесть сезонность")
        self.assertEqual(status, 201, payload)
        root_id = payload["root_id"]
        self.assertTrue(root_id)
        self.assertEqual(payload["title"], phases.PROPOSE_TITLE)
        rec = registry.get(self.cfg, room["id"], root_id)
        self.assertIsNotNone(rec, "карточка должна попасть в реестр комнаты")
        self.assertEqual(rec.get("kind"), "phases_proposal")
        body = "\n".join(self.shim.bodies)
        self.assertIn("phases_proposal", body)
        self.assertIn("Учесть сезонность", body)
        self.assertIn(USER_IDEA["title"], body)      # существующие этапы — в контексте
        self.assertTrue(self.shim.creates(), "карточка создаётся через движок")

    def test_second_propose_while_pending_is_conflict(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        with self.assertRaises(ApiError) as ctx:
            self.propose(room["id"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.detail.get("reason"), "proposal_pending")

    def test_propose_allowed_after_reject(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        self.decide(room["id"], action="reject")
        status, payload, _ = self.propose(room["id"])
        self.assertEqual(status, 201, payload)

    def test_propose_bad_note(self):
        room = self.room()
        with self.assertRaises(ApiError) as ctx:
            self.propose(room["id"], note=5)
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.propose(room["id"], note="а" * 2001)
        self.assertEqual(ctx.exception.status, 400)

    def test_archived_room_refuses_propose(self):
        room = self.room(archived=True)
        with self.assertRaises(ApiError) as ctx:
            self.propose(room["id"])
        self.assertEqual(ctx.exception.status, 409)


# ------------------------------------------------------------------- интеграция
class IntegrationTests(_Base):
    """Карта проекта, «От вас нужно» и сводка видят этапы и предложение."""

    def test_room_card_has_proposal_and_human_text(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        detail = self.detail(room["id"])
        self.assertEqual(detail["phases_proposal"]["state"], "proposed")
        self.assertIn("Руководитель предлагает 3 этапа", detail["phases_proposal_text"])
        self.assertIn("Проверка спроса", detail["phases_proposal_text"])

    def test_roadmap_shows_accepted_phases_immediately(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        status, payload, _ = self.decide(room["id"], action="accept")
        self.assertEqual(status, 200, payload)
        status, view, _ = self.app.dispatch("GET", "/api/rooms/%s/roadmap" % room["id"], {}, {})
        self.assertEqual(status, 200, view)
        self.assertEqual([p["id"] for p in view["phases"]],
                         [item["id"] for item in payload["proposal"]["items"]])
        self.assertEqual(view["phases"][0]["title"], "Идея и вывеска")
        self.assertEqual(view["phases_proposal"]["state"], "accepted")

    def test_backlog_shows_pending_proposal_once(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        status, view, _ = self.app.dispatch("GET", "/api/rooms/%s/backlog" % room["id"], {}, {})
        self.assertEqual(status, 200, view)
        item = view.get("proposal_item")
        self.assertIsNotNone(item, view)
        self.assertEqual(item["id"], "phases_proposal")
        self.assertIn("Проверка спроса", item["text"])
        self.assertEqual(item["count"], 3)
        rows = [i for i in view["items"] if i.get("kind") == "phases_proposal"]
        self.assertEqual(len(rows), 1, view["items"])
        self.assertEqual(view["items"][0]["id"], "phases_proposal")

    def test_backlog_has_no_proposal_after_decision(self):
        room = self.room()
        self.capture(room, PROPOSAL)
        self.decide(room["id"], action="reject")
        status, view, _ = self.app.dispatch("GET", "/api/rooms/%s/backlog" % room["id"], {}, {})
        self.assertEqual(status, 200, view)
        self.assertIsNone(view.get("proposal_item"))
        self.assertFalse([i for i in view["items"] if i.get("kind") == "phases_proposal"])

    def test_user_phases_survive_round_trip(self):
        """Порядок и переименования человека не меняются ни приёмом, ни решением."""
        renamed = [{"id": "proverka", "title": "Проверка спроса, цен и меню"},
                   {"id": "ideya", "title": "Идея и вывеска"}]
        room = self.room(phases=renamed)
        self.capture(room, PROPOSAL)
        fresh = rooms_mod.get_room(self.cfg, room["id"])
        self.assertEqual([p["title"] for p in fresh["phases"]],
                         ["Проверка спроса, цен и меню", "Идея и вывеска"])
        self.decide(room["id"], action="accept")
        final = rooms_mod.get_room(self.cfg, room["id"])
        self.assertEqual([p["id"] for p in final["phases"]],
                         ["proverka", "ideya", "подготовка-документов"])
        self.assertEqual(final["phases"][0]["title"], "Проверка спроса, цен и меню")


if __name__ == "__main__":
    unittest.main(verbosity=2)
