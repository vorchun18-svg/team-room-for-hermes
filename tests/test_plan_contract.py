# -*- coding: utf-8 -*-
"""Контракт руководителя волны 9 (PLAN-CONTRACT): бриф, `plan`, `plan_patch`, карта.

Проверяем ровно то, что обещано в WAVE9 §2, §3 и решениях A4–A7:
  * режим БРИФ: в тело карточки попадают понимание задачи, вопросы, блок `plan`
    (3–8 направлений) и `stage`, а список исполнителей — ТОЛЬКО состав комнаты
    с русскими ролями;
  * блок `plan`/`plan_patch` разбирается толерантно: нет блока — не ошибка,
    битый/пустой — предупреждение по-русски и ничего не применяется, лишние поля
    и чужие профили отбрасываются;
  * канонические ключи нормализатора не тронуты, карта лежит отдельно (`extra`);
  * служебные блоки (бриф, КОНТЕКСТ КОМНАТЫ, контракт) НЕ попадают в текст
    запроса для интерфейса (`visible_text` в RequestDetail.text);
  * цикл брифа на песочнице (`tests.helpers.Sandbox` + `SimAgents`): ответ
    руководителя → `plan.ingest_plan` (черновик), ответ на «Уточнение от
    Михаила» с `plan_patch` → `plan.apply_patch(source="manager")`;
  * обратная совместимость: старый контракт без `plan` работает как раньше.

Запуск:  python -m unittest tests.test_plan_contract -v
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

from server import contract, context_pack, normalize  # noqa: E402

REQUEST = "Собери карту проекта: запускаем десерты Лето в России."


class FakeCfg:
    """Минимальный cfg: контракту нужны только три каталога."""

    def __init__(self, base: Path):
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"


def make_room(**over) -> dict:
    room = {
        "id": "cafe",
        "title": "Лето",
        "board": "team",
        "tenant": "room:cafe",
        "orchestrator": "biz-director",
        "critic": "biz-critic",
        "project_mode": True,
        "members": [
            {"profile": "biz-lawyer", "role": "worker", "note": "юрист по праву России"},
            {"profile": "biz-ops", "role": "worker", "note": ""},
            {"profile": "biz-cfo", "role": "worker", "note": "деньги"},
        ],
        "context_files": [],
        "critic_pass": True,
        # W17-R §3: у новой комнаты режим «сразу»; здесь проверяется ручной
        # («сначала показать план»), поэтому задаём его явно.
        "launch_mode": "plan_first",
    }
    room.update(over)
    return room


STREAMS = [
    {"title": "Легализация и ИП", "goal": "Оформить ИП и разрешения", "owner": "biz-lawyer",
     "members": ["biz-ops"], "depends_on": [], "due": "2026-11-10",
     "reason": "без ИП нельзя продавать"},
    {"title": "Экономика набора", "goal": "Себестоимость и цена", "owner": "biz-cfo",
     "members": ["biz-lawyer"], "depends_on": ["s1"], "due": "2026-11-20",
     "reason": "цена зависит от легализации"},
    {"title": "Упаковка и бренд", "goal": "Название, этикетка", "owner": "biz-ops",
     "members": [], "depends_on": [], "due": None, "reason": "идёт параллельно"},
]


class BriefBodyTests(unittest.TestCase):
    """Режим БРИФ (§2): что приложение дописывает в тело главного запроса."""

    def setUp(self):
        self.cfg = FakeCfg(Path("C:/tmp/tr-plan-contract"))
        self.room = make_room()

    def test_brief_body_has_four_parts_and_plan_contract(self):
        body = contract.build_brief_body(self.room, REQUEST, "r-1", self.cfg)
        self.assertTrue(body.startswith(REQUEST), "текст Михаила идёт первым, дословно")
        self.assertIn(contract.BRIEF_OPEN, body)
        self.assertIn(contract.BRIEF_CLOSE, body)
        for needle in ("Как я понял задачу", "Вопросы к Михаилу", "Карта проекта", "plan:",
                       "stage:", "3–8", "depends_on", "reason", "due"):
            self.assertIn(needle, body, "в брифе нет части %r" % needle)
        self.assertIn(contract.CONTRACT_OPEN, body, "обычный контракт ответа тоже нужен")

    def test_brief_lists_only_room_profiles_with_russian_roles(self):
        body = contract.build_brief_body(self.room, REQUEST, "r-1", self.cfg)
        self.assertIn("biz-lawyer — исполнитель", body)
        self.assertIn("biz-director — руководитель комнаты", body)
        self.assertIn("biz-critic — критик", body)
        for alien in ("coder", "ornith", "biz-strategy"):
            self.assertNotIn(alien, body, "чужой профиль не должен попасть в список")

    def test_brief_without_roster_says_so(self):
        room = make_room(members=[], orchestrator="", critic="")
        body = contract.build_brief_body(room, REQUEST, "r-1", self.cfg)
        self.assertIn("состав комнаты не задан", body)

    def test_flag_brief_switches_contract_body(self):
        plain = contract.build_request_body(self.room, REQUEST, "r-1", self.cfg)
        brief = contract.build_request_body(self.room, REQUEST, "r-1", self.cfg, brief=True)
        self.assertNotIn(contract.BRIEF_OPEN, plain, "без флага брифа быть не должно")
        self.assertIn(contract.BRIEF_OPEN, brief)

    def test_visible_text_has_only_pavel_words(self):
        body = contract.build_brief_body(self.room, REQUEST, "r-1", self.cfg)
        self.assertEqual(contract.visible_text(body), REQUEST)

    def test_visible_text_survives_context_pack_and_brief_together(self):
        body = contract.build_brief_body(self.room, REQUEST, "r-1", self.cfg)
        block = (context_pack.CONTEXT_OPEN + "\nКомната: Лето. CONTEXT.md: C:/Vault/CONTEXT.md\n"
                 + context_pack.CONTEXT_CLOSE)
        with_context = context_pack.insert_into_body(body, block)
        self.assertIn(context_pack.CONTEXT_OPEN, with_context, "блок остался в теле для агента")
        self.assertEqual(contract.visible_text(with_context), REQUEST)

    def test_visible_text_cuts_leading_service_blocks(self):
        """Если служебный блок оказался выше текста Михаила — текст не теряется."""
        body = contract.build_brief_body(self.room, REQUEST, "r-1", self.cfg)
        shifted = body[body.find(contract.BRIEF_OPEN):] + "\n\n" + REQUEST
        self.assertEqual(contract.visible_text(shifted), REQUEST)

    def test_engine_write_order(self):
        """`engine_write.brief_body` зовёт `build_brief_body(cfg, room, body)` — так тоже работает."""
        body = contract.build_brief_body(self.cfg, self.room, REQUEST)
        self.assertIn(contract.BRIEF_OPEN, body)
        self.assertIn("biz-lawyer — исполнитель", body)
        self.assertEqual(contract.visible_text(body), REQUEST)

    def test_engine_write_brief_body_keeps_brief_mode(self):
        """Сквозь `engine_write` (A5) бриф не теряется и не протекает в интерфейс."""
        from server import engine_write as ew
        briefed = ew.brief_body(self.cfg, self.room, REQUEST)
        self.assertIn(contract.BRIEF_OPEN, briefed, "engine_write должен сохранить режим брифа")
        packed = ew.with_context_pack(self.cfg, self.room, briefed)
        self.assertNotIn(context_pack.CONTEXT_OPEN, contract.visible_text(packed),
                         "блок контекста комнаты не должен попадать в текст для интерфейса")
        self.assertEqual(contract.visible_text(packed), REQUEST)


class MapBodyTests(unittest.TestCase):
    """A7: «Собрать карту из запросов комнаты» — kind=map_from_existing."""

    def setUp(self):
        self.cfg = FakeCfg(Path("C:/tmp/tr-plan-contract"))
        self.room = make_room()

    EXISTING = [
        {"title": "Легализация ИП", "status": "done", "summary": "ИП оформлен, ИНН получен",
         "finished_at": 1793165000},
        {"title": "Поставщики упаковки", "status": "failed", "summary": "Три отказа, нужен новый поиск"},
    ]

    def test_map_body_lists_existing_requests(self):
        body = contract.build_map_body(self.room, self.EXISTING, slug="map", cfg=self.cfg)
        self.assertIn("Собери карту проекта", body)
        self.assertIn("Существующие запросы комнаты (2)", body)
        self.assertIn("Легализация ИП", body)
        self.assertIn("готов", body)
        self.assertIn("ошибка", body)
        self.assertIn("ИП оформлен", body)
        self.assertIn(contract.BRIEF_OPEN, body)
        self.assertIn("Не предлагай заново то, что уже сделано", body)

    def test_map_body_argument_order_is_forgiving(self):
        a = contract.build_map_body(self.room, self.EXISTING, cfg=self.cfg)
        b = contract.build_map_body(self.EXISTING, self.room, cfg=self.cfg)
        self.assertEqual(a, b)

    def test_map_body_without_requests(self):
        body = contract.build_map_body(self.room, [], cfg=self.cfg)
        self.assertIn("Существующих запросов в комнате пока нет", body)
        self.assertIn(contract.BRIEF_OPEN, body)

    def test_map_body_visible_text_is_prompt(self):
        body = contract.build_map_body(self.room, self.EXISTING, cfg=self.cfg)
        text = contract.visible_text(body)
        self.assertIn("Собери карту проекта", text)
        self.assertNotIn(contract.BRIEF_OPEN, text)
        self.assertNotIn("plan:", text)


class ParsePlanTests(unittest.TestCase):
    """Толерантный парсер блока `plan` (§2)."""

    def setUp(self):
        self.room = make_room()

    def test_no_block_is_not_an_error(self):
        for meta in ({}, {"next_steps": []}, None, "", "{не json", {"extra": {}}):
            plan, warnings = contract.parse_plan(meta, self.room)
            self.assertIsNone(plan)
            self.assertEqual(warnings, [], "отсутствие блока — не ошибка: %r" % (meta,))

    def test_valid_plan_and_extra_fields_dropped(self):
        raw = json.loads(json.dumps(STREAMS))
        raw[0]["color"] = "red"
        raw[0]["priority"] = 3
        plan, warnings = contract.parse_plan({"plan": {"streams": raw}}, self.room)
        self.assertEqual(warnings, [])
        self.assertEqual(len(plan["streams"]), 3)
        first = plan["streams"][0]
        self.assertEqual(set(first), set(contract.STREAM_FIELDS))
        self.assertEqual(first["due"], "2026-11-10")
        self.assertEqual(first["members"], ["biz-ops"])
        self.assertEqual(plan["streams"][1]["depends_on"], ["s1"])
        self.assertEqual(plan["streams"][1]["owner"], "biz-cfo")

    def test_plan_accepts_json_string_and_bare_list(self):
        plan, warnings = contract.parse_plan(json.dumps({"plan": {"streams": STREAMS}}), self.room)
        self.assertEqual(len(plan["streams"]), 3, warnings)
        plan2, warnings2 = contract.parse_plan({"plan": STREAMS}, self.room)
        self.assertEqual(len(plan2["streams"]), 3, warnings2)

    def test_block_inside_extra_is_found(self):
        plan, _ = contract.parse_plan({"extra": {"plan": {"streams": STREAMS}}}, self.room)
        self.assertEqual(len(plan["streams"]), 3)

    def test_aliens_dropped_with_warning(self):
        streams = [{"title": "Право", "goal": "g", "owner": "coder",
                    "members": ["coder", "biz-lawyer"], "due": "2026-11-01"}]
        plan, warnings = contract.parse_plan({"plan": {"streams": streams}}, self.room)
        stream = plan["streams"][0]
        self.assertIsNone(stream["owner"], "чужой исполнитель не назначается")
        self.assertEqual(stream["members"], ["biz-lawyer"], "чужие участники убраны")
        self.assertEqual(len(warnings), 2)
        self.assertIn("не из состава комнаты", warnings[0])
        self.assertIn("coder", warnings[1])

    def test_broken_and_empty_block_warn_and_apply_nothing(self):
        plan, warnings = contract.parse_plan({"plan": "мусор"}, self.room)
        self.assertIsNone(plan)
        self.assertTrue(warnings and "не разобран" in warnings[0])
        plan, warnings = contract.parse_plan({"plan": {"streams": "нет"}}, self.room)
        self.assertIsNone(plan)
        self.assertTrue(any("streams должен быть списком" in w for w in warnings))
        plan, warnings = contract.parse_plan({"plan": {"streams": []}}, self.room)
        self.assertIsNone(plan)
        self.assertTrue(any("пуст" in w for w in warnings))
        plan, warnings = contract.parse_plan({"plan": {"streams": [{"goal": "без названия"}]}}, self.room)
        self.assertIsNone(plan)
        self.assertTrue(any("нет пригодных направлений" in w for w in warnings))

    def test_short_title_skipped_long_one_cut(self):
        long_title = "Право и лицензии " + "x" * 300
        plan, warnings = contract.parse_plan(
            {"plan": {"streams": [{"title": "ok"}, {"title": long_title, "goal": "g"}]}},
            self.room)
        self.assertEqual(len(plan["streams"]), 1, warnings)
        self.assertEqual(plan["streams"][0]["title"], long_title[: contract.MAX_TITLE])
        self.assertTrue(any("нет названия" in w for w in warnings))

    def test_more_than_eight_truncated(self):
        streams = [{"title": "Направление %d" % i, "goal": "g"} for i in range(10)]
        plan, warnings = contract.parse_plan({"plan": {"streams": streams}}, self.room)
        self.assertEqual(len(plan["streams"]), contract.MAX_STREAMS)
        self.assertTrue(any("взяты первые" in w for w in warnings))

    def test_bad_due_becomes_null(self):
        plan, _ = contract.parse_plan(
            {"plan": {"streams": [{"title": "Срок", "due": "когда-нибудь", "goal": "g"}]}}, self.room)
        self.assertIsNone(plan["streams"][0]["due"])

    def test_unknown_status_not_set(self):
        plan, warnings = contract.parse_plan(
            {"plan": {"streams": [{"title": "Статус", "goal": "g", "status": "летит"}]}}, self.room)
        self.assertNotIn("status", plan["streams"][0])
        self.assertTrue(any("неизвестен" in w for w in warnings))


class ParsePlanPatchTests(unittest.TestCase):
    """Правки карты из ответа на «Уточнение от Михаила» (§2, A6)."""

    def setUp(self):
        self.room = make_room()

    def test_no_block(self):
        patch, warnings = contract.parse_plan_patch({"plan": {"streams": STREAMS}}, self.room)
        self.assertIsNone(patch)
        self.assertEqual(warnings, [])

    def test_full_patch(self):
        raw = {
            "add": [{"title": "Новое направление", "goal": "g", "owner": "biz-ops", "color": "red"}],
            "change": [{"id": "s1", "fields": {"due": "2026-12-01", "owner": "biz-lawyer"}}],
            "drop": [{"id": "s2", "reason": "не нужно"}, "s3"],
            "note": "перенос сроков",
        }
        patch, warnings = contract.parse_plan_patch({"plan_patch": raw}, self.room)
        self.assertEqual(warnings, [])
        self.assertEqual(len(patch["add"]), 1)
        self.assertEqual(set(patch["add"][0]), set(contract.STREAM_FIELDS), "лишние поля отброшены")
        self.assertEqual(patch["change"][0]["fields"], {"due": "2026-12-01", "owner": "biz-lawyer"})
        self.assertEqual(patch["drop"], [{"id": "s2", "reason": "не нужно"}, {"id": "s3", "reason": ""}])
        self.assertEqual(patch["note"], "перенос сроков")

    def test_change_without_id_skipped(self):
        patch, warnings = contract.parse_plan_patch(
            {"plan_patch": {"change": [{"fields": {"due": "2026-12-01"}}],
                            "drop": [{"id": "s2"}]}}, self.room)
        self.assertEqual(patch["change"], [])
        self.assertTrue(any("нет id" in w for w in warnings))

    def test_alien_owner_in_change_not_applied(self):
        patch, warnings = contract.parse_plan_patch(
            {"plan_patch": {"change": [{"id": "s1", "fields": {"owner": "coder"}}],
                            "note": "x"}}, self.room)
        self.assertIsNone(patch, "правка без пригодных полей не применяется")
        self.assertTrue(any("не из состава" in w for w in warnings) or
                        any("пуста" in w for w in warnings))

    def test_alien_owner_dropped_but_other_fields_kept(self):
        patch, warnings = contract.parse_plan_patch(
            {"plan_patch": {"change": [{"id": "s1", "fields": {"owner": "coder", "due": "2026-12-01"}}]}},
            self.room)
        self.assertEqual(patch["change"][0]["fields"], {"due": "2026-12-01"})
        self.assertTrue(any("не из состава" in w for w in warnings))

    def test_broken_and_empty_patch(self):
        patch, warnings = contract.parse_plan_patch({"plan_patch": [1, 2]}, self.room)
        self.assertIsNone(patch)
        self.assertTrue(any("нужен объект" in w for w in warnings))
        patch, warnings = contract.parse_plan_patch({"plan_patch": {}}, self.room)
        self.assertIsNone(patch)
        self.assertTrue(any("нет пригодных правок" in w for w in warnings))
        patch, warnings = contract.parse_plan_patch({"plan_patch": {"drop": [{"reason": "без id"}]}},
                                                    self.room)
        self.assertIsNone(patch)
        self.assertTrue(any("удаление без id" in w for w in warnings))

    def test_both_blocks_together(self):
        got = contract.parse_plan_metadata(
            {"plan": {"streams": STREAMS}, "plan_patch": {"drop": [{"id": "s1", "reason": "r"}]}},
            self.room)
        self.assertEqual(len(got["plan"]["streams"]), 3)
        self.assertEqual(got["plan_patch"]["drop"][0]["id"], "s1")
        self.assertEqual(got["warnings"], [])


class CanonicalKeysTests(unittest.TestCase):
    """Карта живёт ОТДЕЛЬНО от 10 канонических ключей нормализатора (§2, §5 волны 1)."""

    CANONICAL = {"next_steps", "open_questions", "decisions", "stage", "future_questions",
                 "artifacts", "report", "verdict", "issues", "extra"}

    def test_plan_does_not_add_canonical_keys(self):
        out = normalize.normalize_metadata({"plan": {"streams": STREAMS},
                                            "plan_patch": {"drop": [{"id": "s1"}]}}, "Ответ")
        self.assertEqual(set(out), self.CANONICAL)
        self.assertIn("plan", out["extra"], "сырой блок сохраняется в extra")
        self.assertIn("plan_patch", out["extra"])

    def test_old_answer_without_plan_unchanged(self):
        out = normalize.normalize_metadata({"next_steps": [{"text": "шаг", "owner": "mikhail",
                                                            "due": "2026-11-01"}]}, "Ответ")
        self.assertEqual(set(out), self.CANONICAL)
        self.assertEqual(out["extra"], {}, "без блока карта ничего не добавляет")
        self.assertEqual(out["next_steps"][0]["text"], "шаг")

    def test_normalize_plan_blocks_helper(self):
        got = normalize.normalize_plan_blocks({"plan": {"streams": STREAMS}}, make_room())
        self.assertEqual(len(got["plan"]["streams"]), 3)
        self.assertIsNone(got["plan_patch"])
        broken = normalize.normalize_plan_blocks({"plan": 42}, make_room())
        self.assertIsNone(broken["plan"])
        self.assertTrue(broken["warnings"])


class BriefCycleSandboxTests(unittest.TestCase):
    """Сквозной цикл брифа на песочнице: SimAgents → контракт → plan.py."""

    @classmethod
    def setUpClass(cls):
        from tests.helpers import Sandbox, SimAgents
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    # --- помощники ---------------------------------------------------------
    def _room(self) -> dict:
        from server import rooms
        self.__class__._room_seq = getattr(self.__class__, "_room_seq", 0) + 1
        seq = self.__class__._room_seq
        return rooms.create_room(self.cfg, {
            "id": "planroom%s" % seq, "title": "Планы Лето %s" % seq,
            "orchestrator": "biz-director", "critic": "biz-critic",
            "members": [{"profile": "biz-lawyer", "role": "worker"},
                        {"profile": "biz-ops", "role": "worker"},
                        {"profile": "biz-cfo", "role": "worker"}],
            # W17-R §3: у новых комнат режим «сразу», а эти тесты проверяют
            # ЧЕРНОВИК карты — поэтому здесь явно «сначала план».
            "launch_mode": "plan_first",
        })

    def _detail(self, task_id: str, rec_meta: dict, title: str, brief: bool = False) -> dict:
        """detail для ingest: дерево из песочной БД (CLI чтения заменён прямым SELECT)."""
        con = self.sa.connect()
        try:
            row = con.execute(
                "SELECT r.metadata AS meta, r.summary AS summary, t.status AS status, "
                "t.completed_at AS completed_at FROM tasks t LEFT JOIN task_runs r "
                "ON r.id = t.current_run_id WHERE t.id = ?", (task_id,)).fetchone()
        finally:
            con.close()
        metadata = json.loads(row["meta"]) if row and row["meta"] else {}
        metadata.update(rec_meta)
        now = int(time.time())
        detail = {
            "id": task_id, "title": title, "text": REQUEST, "status": "done",
            "created_at": now - 100, "finished_at": int(row["completed_at"] or now) if row else now,
            "synthesis": {"state": "ready", "text_md": row["summary"] if row else "Ответ",
                          "author": "biz-director", "at": now, "version": 1,
                          "source_task": task_id},
            "next_steps": [{"id": task_id + ":ns_1", "text": "Запустить карту", "owner": "mikhail",
                            "due": None, "state": "open"}],
            "tree": [{"id": task_id, "status": "done", "finished_at": now,
                      "metadata": metadata}],
        }
        if brief:
            detail["brief"] = True
        return detail

    def _record(self, room: dict, root_id: str, **extra) -> dict:
        from server import registry
        rec = {"root_id": root_id, "slug": "r-20261029-1200-map1", "title": "Главный запрос",
               "text": REQUEST, "created_at": int(time.time()) - 100, "critic": False,
               "mode": "auto", "adopted": False, "extra_cards": [], "ingested": {},
               "retargeted": False, "brief": True}
        rec.update(extra)
        registry.add_request(self.cfg, room["id"], rec)
        return rec

    # --- тесты -------------------------------------------------------------
    def test_brief_answer_creates_draft_plan(self):
        from server import ingest, plan as plan_mod
        room = self._room()
        root = self.sa.card("Главный запрос: карта Лето", room["orchestrator"],
                            tenant="room:" + room["id"])
        self.sa.start(root)
        self.sa.finish(root, "Как я понял задачу…", {
            "plan": {"streams": STREAMS + [{"title": "Чужое", "goal": "g", "owner": "coder"}]},
            "next_steps": [{"text": "Запустить", "owner": "mikhail", "due": "2026-11-01"}],
            "stage": {"current": "legal", "phases": []},
        })
        self._record(room, root)
        detail = self._detail(root, {"plan": {"streams": STREAMS + [
            {"title": "Чужое", "goal": "g", "owner": "coder"}]}}, "Главный запрос", brief=True)
        res = ingest.ingest_request(self.cfg, room, detail)
        self.assertTrue(res.get("changed"), res)

        data = plan_mod.load(self.cfg, room["id"])
        self.assertIsNotNone(data, "ответ брифа должен создать черновик карты")
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["brief_root_id"], root)
        self.assertGreaterEqual(len(data["streams"]), 3)
        for stream in data["streams"]:
            self.assertEqual(stream["status"], "proposed", "черновик ничего не запускает")
            self.assertIn(stream["owner"], (None, "biz-lawyer", "biz-ops", "biz-cfo"),
                          "чужой исполнитель не должен попасть в карту")
        self.assertTrue(any("Руководитель предложил карту" in str(item.get("text"))
                            for item in data.get("log") or []), "журнал карты: предложение")
        path = plan_mod.plan_path(self.cfg, room["id"])
        self.assertTrue(str(path).startswith(str(self.sb.root)), "карта пишется только в песочницу")

    def test_note_answer_with_patch_updates_plan(self):
        from server import ingest, plan as plan_mod, thread
        room = self._room()
        root = self.sa.card("Главный запрос: карта Лето 2", room["orchestrator"],
                            tenant="room:" + room["id"])
        self.sa.start(root)
        self.sa.finish(root, "Карта предложена", {"plan": {"streams": STREAMS}})
        self._record(room, root)
        ingest.ingest_request(self.cfg, room, self._detail(
            root, {"plan": {"streams": STREAMS}}, "Главный запрос", brief=True))
        before = plan_mod.load(self.cfg, room["id"])
        self.assertTrue(before and before["streams"])

        # карточка «Уточнение от Михаила»: руководитель приложил plan_patch (§A6)
        note = self.sa.card(thread.NOTE_TITLE, room["orchestrator"], tenant="room:" + room["id"])
        self.sa.start(note)
        patch = {"add": [{"title": "Логистика", "goal": "доставка", "owner": "biz-ops"}],
                 "change": [{"id": "s1", "fields": {"due": "2026-12-31"}}],
                 "note": "Михаил сдвинул сроки"}
        self.sa.finish(note, "Принял; передал: biz-ops", {"plan_patch": patch})
        applied = ingest.apply_plan_from_detail(
            self.cfg, room, self._detail(note, {"plan_patch": patch}, thread.NOTE_TITLE))
        self.assertEqual(applied["applied"], "plan_patch", applied)

        after = plan_mod.load(self.cfg, room["id"])
        ids = {s["id"]: s for s in after["streams"]}
        self.assertEqual(ids["s1"]["due"], "2026-12-31", "срок применён автоматически")
        added = [s for s in after["streams"] if s["title"] == "Логистика"]
        self.assertTrue(added, "новое направление добавлено")
        self.assertEqual(added[0]["status"], "proposed", "новое ждёт «Запустить»")
        self.assertTrue(any("Михаил сдвинул сроки" in str(item.get("text"))
                            for item in after.get("log") or []), "причина правки — в журнал")
        # W17-R §3: у комнаты по умолчанию («сразу, без подтверждения») карта
        # стартует сама, поэтому статус карты может быть уже running. Проверяем
        # главное: правка ничего НЕ запускает — новый пункт остался «предложено».
        self.assertIn(after["status"], ("draft", "running"))

    def test_broken_plan_does_not_break_request_ingest(self):
        from server import ingest, plan as plan_mod
        room = self._room()
        root = self.sa.card("Главный запрос: битый блок", room["orchestrator"],
                            tenant="room:" + room["id"])
        self.sa.start(root)
        self.sa.finish(root, "Ответ с битым планом", {})
        self._record(room, root)
        detail = self._detail(root, {"plan": "мусор"}, "Битый блок", brief=True)
        res = ingest.ingest_request(self.cfg, room, detail)
        self.assertTrue(res.get("changed"), "приём ответа не должен падать из-за карты")
        self.assertIsNone(plan_mod.load(self.cfg, room["id"]), "битый блок не создаёт карту")


class LegacyContractTests(unittest.TestCase):
    """Обратная совместимость: старые карточки и старые ответы агентов."""

    def setUp(self):
        self.cfg = FakeCfg(Path("C:/tmp/tr-plan-contract"))
        self.room = make_room()

    def test_old_request_body_still_works(self):
        body = contract.build_request_body(self.room, REQUEST, "r-1", self.cfg)
        self.assertIn(contract.CONTRACT_OPEN, body)
        self.assertIn(contract.CONTRACT_CLOSE, body)
        for key in ("next_steps", "open_questions", "decisions", "stage", "future_questions",
                    "artifacts", "report"):
            self.assertIn(key, body)
        self.assertEqual(contract.visible_text(body), REQUEST)

    def test_old_answer_metadata_gives_no_plan(self):
        got = contract.parse_plan_metadata({"next_steps": [], "stage": {"current": "legal"}},
                                          self.room)
        self.assertIsNone(got["plan"])
        self.assertIsNone(got["plan_patch"])
        self.assertEqual(got["warnings"], [])


if __name__ == "__main__":
    unittest.main()
