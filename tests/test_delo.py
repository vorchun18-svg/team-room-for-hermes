"""«Дело» (WAVE26 §1–2, пакет D5): хранение, этапы, ожидания, исходящее, действия.

Три уровня проверки, как у остальных backend-пакетов:

* правила и хранилище — на временном каталоге (`cfg.state_dir`), без сервера;
* маршруты — настоящий `App` в песочнице `tests.helpers.Sandbox` (`app.dispatch`);
* форма — сверка с `fixtures/delo.json` (её пишет пакет D6; пока файла нет, сверка
  пропускается с явной причиной, а не «зелёным» тестом вслепую).

    python -m unittest tests.test_delo -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import delo as delo_mod              # noqa: E402

FIXTURE = PROJECT_ROOT / "fixtures" / "delo.json"
NOW = 1795000000
ROOM = {"id": "studio", "title": "Студия"}

CONTRACT_KEYS = {"id", "room", "title", "status", "due", "owner", "members", "updated_at",
                 "request_id", "stages", "waiting_on", "needs_you", "result", "outbox",
                 "found", "timeline", "duties"}


class FakeSources(delo_mod.Sources):
    """Источники пунктов «ждёт вас»: что положили, то и отдаём (для правил)."""

    def __init__(self, needs=None, request=None):
        self._needs = list(needs or [])
        self._request = request
        self.calls = 0

    def needs(self, room_id, request_id):
        self.calls += 1
        return list(self._needs)

    def request(self, room_id, request_id):
        return self._request


class BrokenSources(delo_mod.Sources):
    def needs(self, room_id, request_id):
        raise RuntimeError("источник упал")

    def request(self, room_id, request_id):
        raise RuntimeError("реестр упал")


def _tmp_cfg() -> SimpleNamespace:
    root = Path(tempfile.mkdtemp(prefix="delo-test-"))
    return SimpleNamespace(state_dir=root / "state", data_dir=root, sandbox=True)


def _point(ident, kind, text, **kw):
    item = {"id": ident, "kind": kind, "text": text, "at": NOW - 60}
    item.update(kw)
    return item


class StageRuleTests(unittest.TestCase):
    """Этапы: цепочка по умолчанию, «сейчас» один, переходы не врут."""

    def test_default_chain(self):
        stages = delo_mod.default_stages()
        self.assertEqual([stage["title"] for stage in stages],
                         ["Сбор", "Черновик", "Согласование", "Исполнение", "Готово"])
        self.assertEqual([stage["id"] for stage in stages],
                         ["st_1", "st_2", "st_3", "st_4", "st_5"])
        self.assertEqual([stage["state"] for stage in stages],
                         ["now", "todo", "todo", "todo", "todo"])

    def test_normalize_from_strings_marks_first_now(self):
        stages = delo_mod._normalize_stages(["Сбор", "Подпись"])
        self.assertEqual([stage["state"] for stage in stages], ["now", "todo"])

    def test_normalize_keeps_explicit_states(self):
        stages = delo_mod._normalize_stages([{"title": "Сбор", "state": "done"},
                                             {"title": "Подпись", "state": "now"}])
        self.assertEqual([stage["state"] for stage in stages], ["done", "now"])

    def test_normalize_bad_state_becomes_todo(self):
        stages = delo_mod._normalize_stages([{"title": "Сбор", "state": "нечто"}])
        self.assertEqual(stages[0]["state"], "now")     # один «сейчас» обязателен

    def test_empty_chain_falls_back_to_default(self):
        self.assertEqual(len(delo_mod._normalize_stages([])), 5)
        self.assertEqual(len(delo_mod._normalize_stages("мусор")), 5)

    def test_mark_done_promotes_next(self):
        delo = {"stages": delo_mod.default_stages()}
        delo_mod.set_stage_state(delo, "st_1", "done")
        self.assertEqual([stage["state"] for stage in delo["stages"]],
                         ["done", "now", "todo", "todo", "todo"])

    def test_mark_now_closes_previous(self):
        delo = {"stages": delo_mod.default_stages()}
        delo_mod.set_stage_state(delo, "st_4", "now")
        self.assertEqual([stage["state"] for stage in delo["stages"]],
                         ["done", "done", "done", "now", "todo"])

    def test_back_to_todo_resets_following(self):
        delo = {"stages": delo_mod._normalize_stages(
            [{"title": "Сбор", "state": "done"}, {"title": "Черновик", "state": "now"},
             {"title": "Готово", "state": "done"}])}
        delo_mod.set_stage_state(delo, "st_2", "todo")
        self.assertEqual([stage["state"] for stage in delo["stages"]],
                         ["done", "now", "todo"])

    def test_unknown_stage_is_404(self):
        delo = {"stages": delo_mod.default_stages()}
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.set_stage_state(delo, "st_99", "done")
        self.assertEqual(ctx.exception.http, 404)

    def test_bad_state_is_400(self):
        delo = {"stages": delo_mod.default_stages()}
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.set_stage_state(delo, "st_1", "готово")
        self.assertEqual(ctx.exception.http, 400)

    def test_current_stage(self):
        delo = {"stages": delo_mod.default_stages()}
        self.assertEqual(delo_mod.current_stage(delo)["id"], "st_1")
        self.assertIsNone(delo_mod.current_stage({"stages": []}))


class NeedsRuleTests(unittest.TestCase):
    """«Ждёт вашего решения»: виды, действия, порядок, честные пустые списки."""

    def _delo(self, request_id="t_abcd1234"):
        return {"id": "dl_1", "room": ROOM["id"], "request_id": request_id,
                "stages": delo_mod.default_stages()}

    def test_without_request_no_points(self):
        sources = FakeSources([_point("q_1", "open_question", "Что-то")])
        self.assertEqual(delo_mod.needs_you(self._delo(None), sources, NOW), [])
        self.assertEqual(sources.calls, 0)

    def test_without_sources_no_points(self):
        self.assertEqual(delo_mod.needs_you(self._delo(), None, NOW), [])

    def test_question_maps_to_question_with_answer(self):
        sources = FakeSources([_point("q_1", "open_question", "Какой срок?",
                                     question_id="q_1")])
        points = delo_mod.needs_you(self._delo(), sources, NOW)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["kind"], "question")
        self.assertEqual(points[0]["actions"], ["answer"])
        self.assertEqual(points[0]["id"], "ny_q_1")
        self.assertEqual(points[0]["source"]["question_id"], "q_1")
        self.assertTrue(points[0]["recommended"])

    def test_decision_maps_to_approve(self):
        sources = FakeSources([_point("d_1", "decision", "Берём предоплату?",
                                     decision_id="d_1")])
        point = delo_mod.needs_you(self._delo(), sources, NOW)[0]
        self.assertEqual(point["kind"], "approve")
        self.assertEqual(point["actions"], ["approve", "reject"])

    def test_step_maps_to_approve(self):
        sources = FakeSources([_point("s_1", "next_step", "Позвонить", step_id="s_1")])
        self.assertEqual(delo_mod.needs_you(self._delo(), sources, NOW)[0]["kind"], "approve")

    def test_approval_maps_to_permission_and_first(self):
        sources = FakeSources([_point("q_1", "open_question", "Вопрос", question_id="q_1"),
                               _point("ap_1", "approval", "Разрешить запуск",
                                      approval_id="ap_1")])
        points = delo_mod.needs_you(self._delo(), sources, NOW)
        self.assertEqual([point["kind"] for point in points], ["permission", "question"])

    def test_unknown_kind_ignored(self):
        sources = FakeSources([_point("x_1", "future_question", "Когда-нибудь")])
        self.assertEqual(delo_mod.needs_you(self._delo(), sources, NOW), [])

    def test_empty_text_and_garbage_ignored(self):
        sources = FakeSources([_point("q_1", "open_question", "   "), "строка", None])
        self.assertEqual(delo_mod.needs_you(self._delo(), sources, NOW), [])

    def test_options_passed_through(self):
        sources = FakeSources([_point("q_1", "open_question", "30 или 50 %?",
                                     options=[{"id": "o1", "label": "30 %"}, "50 %"])])
        options = delo_mod.needs_you(self._delo(), sources, NOW)[0]["options"]
        self.assertEqual([item["label"] for item in options], ["30 %", "50 %"])
        self.assertTrue(all(item["id"] for item in options))

    def test_no_options_key_when_empty(self):
        sources = FakeSources([_point("q_1", "open_question", "Вопрос")])
        self.assertNotIn("options", delo_mod.needs_you(self._delo(), sources, NOW)[0])

    def test_broken_source_does_not_break_reading(self):
        self.assertEqual(delo_mod.needs_you(self._delo(), BrokenSources(), NOW), [])

    def test_newer_points_last_within_kind(self):
        sources = FakeSources([_point("q_2", "open_question", "Новый", at=NOW - 10,
                                     question_id="q_2"),
                               _point("q_1", "open_question", "Старый", at=NOW - 900,
                                      question_id="q_1")])
        points = delo_mod.needs_you(self._delo(), sources, NOW)
        self.assertEqual([point["source"].get("question_id") for point in points], ["q_1", "q_2"])

    def test_derived_status(self):
        base = {"status": "open", "waiting_on": []}
        self.assertEqual(delo_mod.derived_status(base), "open")
        self.assertEqual(delo_mod.derived_status({"status": "open", "waiting_on": [
            {"who": "you", "what": "решение", "since": NOW}]}), "waiting")
        for status in ("paused", "done", "cancelled"):
            self.assertEqual(delo_mod.derived_status({"status": status, "waiting_on": [
                {"who": "you", "what": "решение", "since": NOW}]}), status)


class StoreTests(unittest.TestCase):
    """Создание, чтение, поиск и файл состояния (атомарно, битое — в карантин)."""

    def setUp(self):
        self.cfg = _tmp_cfg()

    def test_create_manual_has_contract_shape(self):
        item = delo_mod.create(self.cfg, ROOM, {"title": "Договор с контрагентом"}, now=NOW)
        self.assertEqual(set(item) >= CONTRACT_KEYS, True, sorted(set(item)))
        self.assertTrue(delo_mod.DELO_ID_RE.match(item["id"]))
        self.assertEqual(item["room"], ROOM["id"])
        self.assertEqual(item["status"], "open")
        self.assertIsNone(item["due"])
        self.assertIsNone(item["request_id"])
        self.assertEqual(item["members"], [])
        self.assertEqual(len(item["stages"]), 5)
        self.assertEqual(item["needs_you"], [])
        self.assertEqual(item["result"], {"parts": [], "files": []})
        self.assertEqual(item["outbox"], [])
        self.assertEqual(item["found"], [])
        self.assertEqual(item["duties"], [])
        self.assertEqual(item["timeline"][0]["kind"], "created")
        self.assertEqual(item["updated_at"], NOW)

    def test_create_from_request_takes_title_from_source(self):
        sources = FakeSources(request={"id": "t_abcd1234", "title": "Согласование договора"})
        item = delo_mod.create(self.cfg, ROOM, {"request_id": "t_abcd1234"},
                               sources=sources, now=NOW)
        self.assertEqual(item["title"], "Согласование договора")
        self.assertEqual(item["request_id"], "t_abcd1234")
        self.assertIn("t_abcd1234", item["timeline"][0]["text"])

    def test_create_requires_title(self):
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.create(self.cfg, ROOM, {}, now=NOW)
        self.assertEqual(ctx.exception.http, 400)

    def test_create_bad_due_is_refused(self):
        with self.assertRaises(delo_mod.DeloError):
            delo_mod.create(self.cfg, ROOM, {"title": "Дело", "due": "21.11.2026"}, now=NOW)

    def test_create_bad_request_id_refused(self):
        with self.assertRaises(delo_mod.DeloError):
            delo_mod.create(self.cfg, ROOM, {"title": "Дело", "request_id": "abc"}, now=NOW)

    def test_one_delo_per_request(self):
        delo_mod.create(self.cfg, ROOM, {"title": "Дело", "request_id": "t_abcd1234"}, now=NOW)
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.create(self.cfg, ROOM, {"title": "Клон", "request_id": "t_abcd1234"},
                            now=NOW + 5)
        self.assertEqual(ctx.exception.http, 409)
        self.assertTrue(ctx.exception.detail.get("id"))

    def test_two_manual_delos_are_different(self):
        first = delo_mod.create(self.cfg, ROOM, {"title": "Первое"}, now=NOW)
        second = delo_mod.create(self.cfg, ROOM, {"title": "Второе"}, now=NOW + 1)
        self.assertNotEqual(first["id"], second["id"])

    def test_list_is_newest_first(self):
        older = delo_mod.create(self.cfg, ROOM, {"title": "Старое"}, now=NOW)
        newer = delo_mod.create(self.cfg, ROOM, {"title": "Новое"}, now=NOW + 10)
        payload = delo_mod.list_delos(self.cfg, ROOM["id"], now=NOW + 20)
        self.assertEqual(set(payload), {"items", "generated_at"})
        self.assertEqual([item["id"] for item in payload["items"]], [newer["id"], older["id"]])
        self.assertEqual(payload["generated_at"], NOW + 20)

    def test_get_and_find(self):
        item = delo_mod.create(self.cfg, ROOM, {"title": "Дело"}, now=NOW)
        self.assertEqual(delo_mod.get(self.cfg, item["id"])["id"], item["id"])
        room_id, found = delo_mod.find(self.cfg, item["id"])
        self.assertEqual(room_id, ROOM["id"])
        self.assertEqual(found["title"], "Дело")

    def test_get_unknown_is_404(self):
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.get(self.cfg, "dl_nothing1")
        self.assertEqual(ctx.exception.http, 404)

    def test_get_bad_id_is_400(self):
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.get(self.cfg, "../../etc/passwd")
        self.assertEqual(ctx.exception.http, 400)

    def test_store_file_name_and_room_folder(self):
        item = delo_mod.create(self.cfg, ROOM, {"title": "Дело"}, now=NOW)
        path = delo_mod.store_path(self.cfg, ROOM["id"])
        self.assertEqual(path.name, "delos.json")
        self.assertEqual(path.parent.name, ROOM["id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["items"][0]["id"], item["id"])

    def test_corrupt_file_is_quarantined_and_empty(self):
        path = delo_mod.store_path(self.cfg, ROOM["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ это не json", encoding="utf-8")
        payload = delo_mod.list_delos(self.cfg, ROOM["id"], now=NOW)
        self.assertEqual(payload["items"], [])
        copies = list(path.parent.glob("delos.json.corrupt-*"))
        self.assertTrue(copies, "битый файл должен остаться копией")

    def test_concurrent_creates_do_not_lose_items(self):
        import threading
        errors: list = []

        def make(index: int) -> None:
            try:
                delo_mod.create(self.cfg, ROOM, {"title": "Дело %d" % index}, now=NOW + index)
            except Exception as exc:         # noqa: BLE001 — тест собирает все сбои
                errors.append(exc)

        threads = [threading.Thread(target=make, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(delo_mod.list_delos(self.cfg, ROOM["id"])["items"]), 8)

    def test_other_rooms_do_not_leak(self):
        mine = delo_mod.create(self.cfg, ROOM, {"title": "Моё"}, now=NOW)
        delo_mod.create(self.cfg, {"id": "other", "title": "Чужая"}, {"title": "Чужое"}, now=NOW)
        self.assertEqual([item["id"] for item in
                          delo_mod.list_delos(self.cfg, ROOM["id"])["items"]], [mine["id"]])
        self.assertEqual(delo_mod.find(self.cfg, mine["id"])[0], ROOM["id"])

    def test_max_delos(self):
        data = {"version": 1, "updated_at": NOW,
                "items": [{"id": "dl_%04x" % number, "room": ROOM["id"], "title": "т"}
                          for number in range(delo_mod.MAX_DELOS)]}
        path = delo_mod.store_path(self.cfg, ROOM["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.create(self.cfg, ROOM, {"title": "Сверх лимита"}, now=NOW)
        self.assertEqual(ctx.exception.http, 409)


class ActionTests(unittest.TestCase):
    """Действия: единственный путь записи, честные коды, лента «Ход дела»."""

    def setUp(self):
        self.cfg = _tmp_cfg()
        self.item = delo_mod.create(self.cfg, ROOM, {
            "title": "Договор", "due": "2026-11-21",
            "result": {"parts": [{"title": "Предмет договора", "state": "agreed"}],
                       "files": [{"path": "C:/Проекты/договор v3.docx",
                                  "title": "Договор v3", "role": "final", "note": "6 стр."}]},
            "outbox": [{"kind": "email", "to": "контрагент", "subject": "Договор",
                        "body": "Добрый день!", "attachments": ["C:/Проекты/договор v3.docx"]}],
            "waiting_on": [{"who": "you", "what": "решение по v3"}],
        }, now=NOW)
        self.delo_id = self.item["id"]

    def _act(self, **payload):
        return delo_mod.act(self.cfg, ROOM["id"], self.delo_id, payload, now=NOW + 60)

    def test_unknown_action_is_400(self):
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self._act(action="телепорт")
        self.assertEqual(ctx.exception.http, 400)
        self.assertIn("approve", ctx.exception.detail.get("allowed") or [])

    def test_unknown_delo_is_404(self):
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], "dl_nothing1", {"action": "pause"}, now=NOW)
        self.assertEqual(ctx.exception.http, 404)

    def test_pause_and_resume(self):
        paused = self._act(action="pause")["delo"]
        self.assertEqual(paused["status"], "paused")
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self._act(action="pause")
        self.assertEqual(ctx.exception.http, 409)
        resumed = self._act(action="resume")["delo"]
        self.assertEqual(resumed["status"], "waiting")     # ожидание «вас» на месте
        self.assertEqual(resumed["waiting_on"][0]["who"], "you")

    def test_done_closes_and_repeats_409(self):
        done = self._act(action="done")["delo"]
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["timeline"][-1]["kind"], "status")
        with self.assertRaises(delo_mod.DeloError):
            self._act(action="done")

    def test_reopen_returns_done_delo_to_work(self):
        """W29B-Q5: карточку с доски из «Закрыто» можно вернуть в «В работе»."""
        self._act(action="done")
        reopened = self._act(action="reopen")["delo"]
        self.assertIn(reopened["status"], ("open", "waiting"))
        self.assertEqual(reopened["timeline"][-1]["kind"], "status")
        self.assertIn("работу", reopened["timeline"][-1]["text"])
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self._act(action="reopen")
        self.assertEqual(ctx.exception.http, 409)

    def test_reopen_refuses_running_delo(self):
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self._act(action="reopen")
        self.assertEqual(ctx.exception.http, 409)
        # дело не закрыто: возвращать в работу нечего (у фикстуры ожидание «вас» → waiting)
        self.assertIn(ctx.exception.detail["status"], ("open", "waiting"))

    def test_set_stage_by_id_and_rename(self):
        result = self._act(action="set_stage", stage_id="st_2", state="done")["delo"]
        self.assertEqual([stage["state"] for stage in result["stages"]],
                         ["done", "done", "now", "todo", "todo"])
        renamed = self._act(action="set_stage", stage_id="st_3", title="Подписание")["delo"]
        self.assertEqual(renamed["stages"][2]["title"], "Подписание")
        self.assertEqual(renamed["stages"][2]["state"], "now")

    def test_set_stage_without_state_makes_current(self):
        result = self._act(action="set_stage", stage_id="st_3")["delo"]
        self.assertEqual([stage["state"] for stage in result["stages"]],
                         ["done", "done", "now", "todo", "todo"])

    def test_add_and_set_part(self):
        added = self._act(action="add_part", title="Приложения")["delo"]
        self.assertEqual([part["title"] for part in added["result"]["parts"]],
                         ["Предмет договора", "Приложения"])
        part_id = added["result"]["parts"][1]["id"]
        updated = self._act(action="set_part", part_id=part_id, state="discussing")["delo"]
        self.assertEqual(updated["result"]["parts"][1]["state"], "discussing")
        with self.assertRaises(delo_mod.DeloError):
            self._act(action="set_part", part_id="pt_99", state="agreed")

    def test_outbox_goes_draft_approved_sent(self):
        outbox_id = self.item["outbox"][0]["id"]
        self.assertEqual(self.item["outbox"][0]["state"], "draft")
        approved = self._act(action="approve_outbox", outbox_id=outbox_id)["delo"]
        self.assertEqual(approved["outbox"][0]["state"], "approved")
        sent = self._act(action="mark_sent", outbox_id=outbox_id)["delo"]
        self.assertEqual(sent["outbox"][0]["state"], "sent")
        self.assertEqual(sent["outbox"][0]["sent_at"], NOW + 60)
        kinds = [entry["kind"] for entry in sent["timeline"]]
        self.assertEqual(kinds.count("outbox"), 2)

    def test_mark_sent_requires_approval(self):
        outbox_id = self.item["outbox"][0]["id"]
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self._act(action="mark_sent", outbox_id=outbox_id)
        self.assertEqual(ctx.exception.http, 409)
        self.assertEqual(ctx.exception.detail.get("state"), "draft")

    def test_mark_sent_with_error_is_failed(self):
        outbox_id = self.item["outbox"][0]["id"]
        self._act(action="approve_outbox", outbox_id=outbox_id)
        failed = self._act(action="mark_sent", outbox_id=outbox_id,
                           error="Сервер почты не принял письмо")["delo"]
        self.assertEqual(failed["outbox"][0]["state"], "failed")
        self.assertIn("не принял", failed["outbox"][0]["error"])
        again = self._act(action="approve_outbox", outbox_id=outbox_id)["delo"]
        self.assertEqual(again["outbox"][0]["state"], "approved")
        self.assertNotIn("error", again["outbox"][0])

    def test_edit_after_approval_returns_to_draft(self):
        outbox_id = self.item["outbox"][0]["id"]
        self._act(action="approve_outbox", outbox_id=outbox_id)
        edited = self._act(action="edit_outbox", outbox_id=outbox_id,
                           body="Добрый день! Направляем редакцию v3.")["delo"]
        self.assertEqual(edited["outbox"][0]["state"], "draft")
        self.assertIn("v3", edited["outbox"][0]["body"])

    def test_edit_sent_is_refused(self):
        outbox_id = self.item["outbox"][0]["id"]
        self._act(action="approve_outbox", outbox_id=outbox_id)
        self._act(action="mark_sent", outbox_id=outbox_id)
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self._act(action="edit_outbox", outbox_id=outbox_id, body="поздно")
        self.assertEqual(ctx.exception.http, 409)

    def test_edit_without_changes_is_400(self):
        outbox_id = self.item["outbox"][0]["id"]
        with self.assertRaises(delo_mod.DeloError):
            self._act(action="edit_outbox", outbox_id=outbox_id)

    def test_cancel_outbox_and_no_cancel_after_sent(self):
        outbox_id = self.item["outbox"][0]["id"]
        cancelled = self._act(action="cancel_outbox", outbox_id=outbox_id)["delo"]
        self.assertEqual(cancelled["outbox"][0]["state"], "cancelled")
        second = delo_mod.create(self.cfg, ROOM, {
            "title": "Второе", "outbox": [{"kind": "email", "to": "x", "body": "y"}]}, now=NOW)
        second_id = second["id"]
        ob = second["outbox"][0]["id"]
        delo_mod.act(self.cfg, ROOM["id"], second_id, {"action": "approve_outbox",
                                                       "outbox_id": ob}, now=NOW + 1)
        delo_mod.act(self.cfg, ROOM["id"], second_id, {"action": "mark_sent",
                                                       "outbox_id": ob}, now=NOW + 2)
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], second_id, {"action": "cancel_outbox",
                                                           "outbox_id": ob}, now=NOW + 3)
        self.assertEqual(ctx.exception.http, 409)

    def test_add_and_clear_waiting(self):
        added = self._act(action="add_waiting", who="контрагент",
                          what="ответ на письмо")["delo"]
        self.assertEqual([entry["who"] for entry in added["waiting_on"]],
                         ["you", "контрагент"])
        cleared = self._act(action="clear_waiting", who="контрагент")["delo"]
        self.assertEqual([entry["who"] for entry in cleared["waiting_on"]], ["you"])
        cleared_all = self._act(action="clear_waiting", all=True)["delo"]
        self.assertEqual(cleared_all["waiting_on"], [])
        self.assertEqual(cleared_all["status"], "open")
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self._act(action="clear_waiting", all=True)
        self.assertEqual(ctx.exception.http, 404)

    def test_clear_waiting_by_index(self):
        self._act(action="add_waiting", who="вы", what="подпись")
        cleared = self._act(action="clear_waiting", index=1)["delo"]
        self.assertEqual([entry["what"] for entry in cleared["waiting_on"]], ["решение по v3"])

    def test_action_updates_updated_at_and_timeline(self):
        result = self._act(action="pause")["delo"]
        self.assertEqual(result["updated_at"], NOW + 60)
        self.assertEqual(result["timeline"][-1]["at"], NOW + 60)

    def test_file_survives_reload(self):
        self._act(action="pause")
        reloaded = delo_mod.get(self.cfg, self.delo_id)
        self.assertEqual(reloaded["status"], "paused")
        self.assertEqual(reloaded["due"], "2026-11-21")


class DelegationTests(unittest.TestCase):
    """approve/reject/answer: уходят в механизм запроса, дело пишет только ленту."""

    def setUp(self):
        self.cfg = _tmp_cfg()
        self.sources = FakeSources([
            _point("q_1", "open_question", "Принять предоплату 30 или 50 %?",
                   question_id="q_1", options=[{"id": "o1", "label": "30 %"}]),
            _point("d_1", "decision", "Утвердить v3", decision_id="d_1"),
            _point("ap_1", "approval", "Разрешить публикацию", approval_id="ap_1"),
        ])
        self.item = delo_mod.create(self.cfg, ROOM, {"title": "Договор",
                                                    "request_id": "t_abcd1234"},
                                    sources=self.sources, now=NOW)
        self.delo_id = self.item["id"]
        self.sent: list = []

    def _delegate(self, request_id, body):
        self.sent.append((request_id, body))
        return {"ok": True, "action": body.get("action")}

    def test_needs_you_present_after_create(self):
        self.assertEqual(len(self.item["needs_you"]), 3)
        self.assertEqual([point["kind"] for point in self.item["needs_you"]],
                         ["permission", "approve", "question"])

    def test_answer_question_goes_to_request(self):
        point = next(point for point in self.item["needs_you"] if point["kind"] == "question")
        result = delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                              {"action": "answer", "item_id": point["id"], "text": "30 %"},
                              sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(self.sent[0][0], "t_abcd1234")
        self.assertEqual(self.sent[0][1]["action"], "answer")
        self.assertEqual(self.sent[0][1]["question_id"], "q_1")
        self.assertEqual(self.sent[0][1]["text"], "30 %")
        self.assertTrue(result.get("delegated"))
        self.assertEqual(result["delo"]["timeline"][-1]["kind"], "decision")

    def test_approve_decision(self):
        point = next(point for point in self.item["needs_you"] if point["kind"] == "approve")
        delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                     {"action": "approve", "item_id": point["id"]},
                     sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(self.sent[0][1]["action"], "decision")
        self.assertEqual(self.sent[0][1]["decision"], "accept")
        self.assertEqual(self.sent[0][1]["decision_id"], "d_1")

    def test_reject_permission(self):
        point = next(point for point in self.item["needs_you"] if point["kind"] == "permission")
        delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                     {"action": "reject", "item_id": point["id"]},
                     sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(self.sent[0][1]["action"], "approval")
        self.assertEqual(self.sent[0][1]["decision"], "deny")

    def test_answer_needs_text(self):
        point = next(point for point in self.item["needs_you"] if point["kind"] == "question")
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                         {"action": "answer", "item_id": point["id"]},
                         sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(ctx.exception.http, 400)

    def test_question_does_not_support_approve(self):
        point = next(point for point in self.item["needs_you"] if point["kind"] == "question")
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                         {"action": "approve", "item_id": point["id"]},
                         sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(ctx.exception.http, 400)
        self.assertEqual(self.sent, [])

    def test_need_id_alias_works(self):
        """Интерфейс зовёт поле `need_id` — принимаем и его, и `item_id` из контракта."""
        point = next(point for point in self.item["needs_you"] if point["kind"] == "approve")
        delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                     {"action": "approve", "need_id": point["id"]},
                     sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(self.sent[0][1]["action"], "decision")

    def test_answer_by_option_id_uses_label(self):
        point = next(point for point in self.item["needs_you"] if point["kind"] == "question")
        delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                     {"action": "answer", "need_id": point["id"], "option_id": "o1"},
                     sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(self.sent[0][1]["text"], "30 %")

    def test_answer_by_unknown_option_is_400(self):
        point = next(point for point in self.item["needs_you"] if point["kind"] == "question")
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                         {"action": "answer", "item_id": point["id"], "option_id": "o_нет"},
                         sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(ctx.exception.http, 400)
        self.assertEqual(self.sent, [])

    def test_unknown_point_is_409(self):
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                         {"action": "approve", "item_id": "ny_gone"},
                         sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(ctx.exception.http, 409)

    def test_without_delegate_is_409_and_nothing_written(self):
        point = self.item["needs_you"][0]
        before = delo_mod.get(self.cfg, self.delo_id, sources=self.sources)
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                         {"action": "approve", "item_id": point["id"]},
                         sources=self.sources, now=NOW + 5)
        self.assertEqual(ctx.exception.http, 409)
        after = delo_mod.get(self.cfg, self.delo_id, sources=self.sources)
        self.assertEqual(len(after["timeline"]), len(before["timeline"]))

    def test_delegate_failure_leaves_delo_untouched(self):
        def broken(request_id, body):
            raise delo_mod.DeloError("Пункт уже не ждёт решения", http=409, code="conflict")

        point = self.item["needs_you"][0]
        before = delo_mod.get(self.cfg, self.delo_id, sources=self.sources)
        with self.assertRaises(delo_mod.DeloError):
            delo_mod.act(self.cfg, ROOM["id"], self.delo_id,
                         {"action": "approve", "item_id": point["id"]},
                         sources=self.sources, delegate=broken, now=NOW + 5)
        after = delo_mod.get(self.cfg, self.delo_id, sources=self.sources)
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(len(after["timeline"]), len(before["timeline"]))

    def test_manual_delo_has_no_points_to_act_on(self):
        manual = delo_mod.create(self.cfg, ROOM, {"title": "Ручное"}, now=NOW)
        with self.assertRaises(delo_mod.DeloError) as ctx:
            delo_mod.act(self.cfg, ROOM["id"], manual["id"],
                         {"action": "approve", "item_id": "ny_q_1"},
                         sources=self.sources, delegate=self._delegate, now=NOW + 5)
        self.assertEqual(ctx.exception.http, 409)


class FixtureTests(unittest.TestCase):
    """Сверка формы с `fixtures/delo.json` (её пишет D6)."""

    def test_fixture_matches_contract_when_present(self):
        if not FIXTURE.exists():
            self.skipTest("fixtures/delo.json ещё не создан пакетом D6")
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        self.assertIsInstance(items, list)
        self.assertGreaterEqual(len(items), 3, "D6 обещает 3 разных дела")
        for raw in items:
            clean = delo_mod.view(raw)
            missing = CONTRACT_KEYS - set(clean)
            self.assertFalse(missing, "нет ключей %s в деле %s" % (missing, raw.get("id")))
            self.assertIn(clean["status"], delo_mod.STATUSES)
            for stage in clean["stages"]:
                self.assertIn(stage["state"], delo_mod.STAGE_STATES)
            for point in raw.get("needs_you") or []:
                self.assertIn(point.get("kind"), delo_mod.NEEDS_KINDS)
                self.assertTrue(point.get("actions"), "у пункта должно быть действие")
            for entry in clean["outbox"]:
                self.assertIn(entry["state"], delo_mod.OUTBOX_STATES)
            for part in clean["result"]["parts"]:
                self.assertIn(part["state"], delo_mod.PART_STATES)


class _AppCase(unittest.TestCase):
    """Настоящий `App` в песочнице: маршруты, коды, форма ответа."""

    @classmethod
    def setUpClass(cls):
        from server import app as app_mod
        from server import rooms as rooms_mod
        from tests.helpers import Sandbox

        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.rooms_mod = rooms_mod

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        """Своя комната на каждый тест: дела одного теста не видны другому."""
        import secrets
        suffix = secrets.token_hex(3)
        self.room = self.rooms_mod.create_room(self.cfg, {
            "title": "Дела %s" % suffix, "orchestrator": "lead",
            "members": [{"profile": "worker", "role": "worker"}]})

    # --- помощники ---
    def _call(self, method, path, body=None, query=None, expect=200):
        from server.http_util import ApiError
        try:
            status, payload, _headers = self.app.dispatch(method, path, query or {}, body or {})
        except ApiError as exc:              # dispatch отдаёт ошибку исключением, как HTTP-слой
            status, payload = int(exc.status), exc.payload()
        self.assertEqual(status, expect, payload)
        return payload

    def _create(self, **payload):
        payload.setdefault("title", "Дело о договоре")
        return self._call("POST", "/api/rooms/%s/delos" % self.room["id"], payload, expect=201)

    def _act(self, delo_id, **payload):
        return self._call("POST", "/api/delos/%s/actions" % delo_id, payload)

    def _get(self, path, query=None):
        return self._call("GET", path, query=query)


class ApiTests(_AppCase):
    """`GET/POST /api/rooms/{id}/delos`, `GET /api/delos/{id}`, действия."""

    def test_empty_room(self):
        payload = self._call("GET", "/api/rooms/%s/delos" % self.room["id"])
        self.assertEqual(payload["items"], [])
        self.assertIsInstance(payload["generated_at"], int)

    def test_create_and_read_back(self):
        created = self._create(due="2026-11-21")
        self.assertEqual(created["room"], self.room["id"])
        listed = self._call("GET", "/api/rooms/%s/delos" % self.room["id"])
        self.assertIn(created["id"], [item["id"] for item in listed["items"]])
        single = self._call("GET", "/api/delos/%s" % created["id"])
        self.assertEqual(single, listed["items"][0])

    def test_all_rooms_list(self):
        created = self._create(title="Сквозное дело")
        payload = self._call("GET", "/api/delos", query={"room": ["all"]})
        self.assertIn(created["id"], [item["id"] for item in payload["items"]])
        only = self._call("GET", "/api/delos", query={"room": [self.room["id"]]})
        self.assertIn(created["id"], [item["id"] for item in only["items"]])

    def test_unknown_room_is_404(self):
        self._call("GET", "/api/rooms/nosuchroom/delos", expect=404)

    def test_unknown_delo_is_404(self):
        self._call("GET", "/api/delos/dl_zzzz1", expect=404)

    def test_create_without_title_is_400(self):
        self._call("POST", "/api/rooms/%s/delos" % self.room["id"], {}, expect=400)

    def test_action_unknown_is_400_with_allowed(self):
        created = self._create()
        payload = self._call("POST", "/api/delos/%s/actions" % created["id"],
                             {"action": "прыгнуть"}, expect=400)
        self.assertIn("approve", payload.get("error", {}).get("detail", {}).get("allowed") or [])

    def test_action_unknown_delo_is_404(self):
        self._call("POST", "/api/delos/dl_zzzz1/actions", {"action": "pause"}, expect=404)

    def test_full_outbox_cycle_over_http(self):
        created = self._create(outbox=[{"kind": "email", "to": "контрагент",
                                        "subject": "Договор", "body": "Добрый день!"}])
        outbox_id = created["outbox"][0]["id"]
        approved = self._act(created["id"], action="approve_outbox", outbox_id=outbox_id)
        self.assertEqual(approved["delo"]["outbox"][0]["state"], "approved")
        sent = self._act(created["id"], action="mark_sent", outbox_id=outbox_id)
        self.assertEqual(sent["delo"]["outbox"][0]["state"], "sent")
        self.assertTrue(sent["delo"]["outbox"][0]["sent_at"])

    def test_no_traffic_outside_is_recorded_as_state_only(self):
        """Отправка — только состояние: никаких писем, только лента и sent_at."""
        created = self._create(outbox=[{"kind": "email", "to": "контрагент", "body": "текст"}])
        outbox_id = created["outbox"][0]["id"]
        self._act(created["id"], action="approve_outbox", outbox_id=outbox_id)
        sent = self._act(created["id"], action="mark_sent", outbox_id=outbox_id)["delo"]
        self.assertEqual([entry["kind"] for entry in sent["timeline"]].count("outbox"), 2)
        self.assertIn("отмечено отправленным", sent["timeline"][-1]["text"])

    def test_points_come_from_backlog(self):
        """Пункт «ждёт вас» приходит из бэклога комнаты, а не хранится в деле."""
        from server import registry
        root = "t_5d0c11a2"
        registry.add_request(self.cfg, self.room["id"], {
            "root_id": root, "slug": "r-test-" + root, "title": "Согласование",
            "text": "Согласование", "created_at": int(time.time())})
        path = Path(self.cfg.data_dir) / "state" / self.room["id"] / "backlog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"room": self.room["id"], "updated_at": int(time.time()),
                                    "items": [{"id": "q_1", "kind": "open_question",
                                               "text": "Какой срок оплаты?", "owner": "mikhail",
                                               "status": "open", "source_request": root,
                                               "created_at": int(time.time())}]},
                                   ensure_ascii=False), encoding="utf-8")
        created = self._create(title="Согласование договора", request_id=root)
        self.assertEqual(created["request_id"], root)
        points = [point for point in created["needs_you"] if point["kind"] == "question"]
        self.assertEqual(len(points), 1, created["needs_you"])
        self.assertEqual(points[0]["actions"], ["answer"])

        calls: list = []

        def fake_actions(query, body, rid):
            calls.append((rid, body))
            return 200, {"ok": True, "action": body.get("action")}, {}

        self.app.h_request_actions = fake_actions
        try:
            result = self._act(created["id"], action="answer", item_id=points[0]["id"],
                               text="30 дней")
        finally:
            del self.app.h_request_actions
        self.assertEqual(calls[0][0], root)
        self.assertEqual(calls[0][1]["question_id"], "q_1")
        self.assertEqual(result["delegated"]["ok"], True)

    def test_duplicate_delo_for_request_is_409(self):
        from server import registry
        root = "t_5d0c11a3"
        registry.add_request(self.cfg, self.room["id"], {
            "root_id": root, "slug": "r-test-" + root, "title": "Второй",
            "text": "Второй", "created_at": int(time.time())})
        self._create(title="Первое", request_id=root)
        self._call("POST", "/api/rooms/%s/delos" % self.room["id"],
                   {"title": "Второе", "request_id": root}, expect=409)

    def test_form_of_every_delo(self):
        created = self._create(stages=["Сбор", "Подпись"])
        for item in self._call("GET", "/api/rooms/%s/delos" % self.room["id"])["items"]:
            self.assertFalse(CONTRACT_KEYS - set(item), CONTRACT_KEYS - set(item))
        self.assertEqual([stage["title"] for stage in created["stages"]], ["Сбор", "Подпись"])


if __name__ == "__main__":
    unittest.main()
