# -*- coding: utf-8 -*-
"""W20-S1 (B-36) — «живой свод»: версии свода, «тишина», идемпотентность, доставка.

Проверяем по контракту §1.1 ТЗ волны 20:
* `synthesis.live` — состояние `fresh|pending|updating|stale|off`, `behind`, строка по-русски;
* «тишина» (`auto_svod_quiet_s`) и потолок частоты (`auto_svod_min_gap_s`);
* карточку обновления ставит одно место, тем же путём, что кнопка «Доработать»;
* идемпотентность: один запуск на набор событий, карточки свода событием не считаются;
* `delivery` у сообщений Михаила;
* итоговая карточка руководителя с `metadata.svod` — новая версия (`history[].kind="leader"`),
  автоматическая — `auto: true`;
* в контракте руководителя есть запрет «переоткрыть корень».

Данные в тестах ВЫМЫШЛЕННЫЕ (кафе «Лето», поставщик «Ромашка», Анна/Лев/Марина).

Запуск из корня проекта:
    python -m unittest tests.test_svod_live -v
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import app_common  # noqa: E402
from server import answers as answers_mod  # noqa: E402
from server import contract  # noqa: E402
from server import read_view  # noqa: E402
from server import registry  # noqa: E402
from server import engine_tree  # noqa: E402
from server import rooms  # noqa: E402
from server import svod_live  # noqa: E402
from server import thread as thread_mod  # noqa: E402

NOW = 1_760_000_000            # «сейчас» в тестах: 16.11.2025, 01:53 по Москве
V1_AT = NOW - 7200             # версия 1 выпущена два часа назад
V2_AT = NOW - 3600             # версия 2 — час назад
QUIET = 180                    # «тишина» по умолчанию, сек
GAP = 600                      # потолок частоты по умолчанию, сек


class FakeCfg:
    """Минимальные пути контракта: тела карточек только форматируются, файлы не пишутся."""

    def __init__(self, base: Path):
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"


CFG = FakeCfg(ROOT / "tests" / "_svod_tmp")


def make_room(**over) -> dict:
    """Комната кафе «Лето»: руководитель, два исполнителя, критик."""
    room = {"id": "cafe-leto", "title": "Кафе «Лето»", "board": "team",
            "tenant": "room:cafe-leto", "orchestrator": "lead-anna", "critic": "critic-lev",
            "members": [{"profile": "worker-marina", "role": "worker", "note": "закупки"},
                        {"profile": "worker-petr", "role": "worker", "note": "склад"}],
            "auto_svod": True, "auto_svod_quiet_s": QUIET, "auto_svod_min_gap_s": GAP}
    room.update(over)
    return room


def make_detail(**over) -> dict:
    """Деталь запроса «Меню на осень» в вымышленном кафе «Лето»."""
    detail = {
        "id": "t_1000aaaa",
        "status": "done",
        "text": "Собрать меню на осень и посчитать закупки у поставщика «Ромашка».",
        "synthesis": {
            "state": "ready", "version": 2, "at": V2_AT, "source_task": "t_2000bbbb",
            "author": "lead-anna", "text_md": "Версия 2: меню на осень собрано.",
            "history": [
                {"version": 1, "kind": "initial", "task_id": "t_1000aaaa", "at": V1_AT,
                 "author": "lead-anna", "note": "", "changes": []},
                {"version": 2, "kind": "revision", "task_id": "t_2000bbbb", "at": V2_AT,
                 "author": "lead-anna", "note": "Доработка после замечаний",
                 "changes": ["учли сроки поставки"], "auto": False},
            ],
        },
        "tree": [
            {"id": "t_1000aaaa", "role": "root", "status": "done", "title": "Меню на осень",
             "assignee": "lead-anna", "finished_at": V1_AT, "summary_md": "Версия 1",
             "metadata": {}, "runs": []},
            {"id": "t_2000bbbb", "role": "revision", "status": "done", "title": "Доработка ответа",
             "assignee": "lead-anna", "finished_at": V2_AT, "summary_md": "Версия 2",
             "metadata": {"changes": ["учли сроки поставки"]}, "runs": []},
            {"id": "t_3000cccc", "role": "worker", "status": "done", "title": "Закупки у «Ромашки»",
             "assignee": "worker-marina", "finished_at": V2_AT - 600, "summary_md": "Список",
             "metadata": {}, "runs": []},
        ],
        "open_questions": [
            {"id": "q_aaaa1111", "text": "Сколько порций десерта закладывать?",
             "for": "mikhail", "answered": True, "answer": "По 40 порций",
             "answered_at": V2_AT - 900},
        ],
        "decisions": [
            {"id": "d_aaaa2222", "text": "Закупать у «Ромашки»", "status": "accepted",
             "at": V2_AT - 1200},
        ],
        "next_steps": [],
        "thread": [
            {"id": "c1", "at": V2_AT - 950, "author": "mikhail", "author_kind": "mikhail",
             "kind": "message", "text": "Десерт — по 40 порций"},
            {"id": "c2", "at": V2_AT + 600, "author": "mikhail", "author_kind": "mikhail",
             "kind": "message", "text": "Ещё добавьте сезонный суп"},
            {"id": "c3", "at": V2_AT + 900, "author": "lead-anna", "author_kind": "agent",
             "kind": "comment", "text": "Принял, передал Марине"},
        ],
        "critic": {"enabled": True, "state": "approved", "round": 1, "task_id": None},
        "answers": {"items": []},
    }
    detail.update(over)
    return detail


def make_rec(**over) -> dict:
    rec = {"root_id": "t_1000aaaa", "text": "Собрать меню на осень.",
           "extra_cards": [{"id": "t_2000bbbb", "kind": "revision", "round": 1, "at": V2_AT,
                            "note": "Доработка после замечаний", "reason": "note"}],
           "answers": {}, "svod_event_key": "", "svod_launched_at": 0}
    rec.update(over)
    return rec


class FakeEngine:
    """Подмена engine_write: запоминаем карточку, никаких CLI и моделей."""

    def __init__(self):
        self.created = []

    def create_child_card(self, cfg, room, board, parent_id, assignee, title, body, kind,
                          idem_key=None):
        self.created.append({"room": room, "board": board, "parent": parent_id,
                             "assignee": assignee, "title": title, "body": body,
                             "kind": kind, "idem_key": idem_key})
        return "t_a000000%d" % len(self.created)


class FakeRegistry:
    def __init__(self):
        self.cards = []
        self.patches = []

    def add_extra_card(self, cfg, room_id, root_id, card):
        self.cards.append({"room": room_id, "root": root_id, "card": card})
        return {"root_id": root_id}

    def update(self, cfg, room_id, root_id, patch):
        self.patches.append({"room": room_id, "root": root_id, "patch": patch})
        return {"root_id": root_id}


class SvodBase(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.registry = FakeRegistry()
        self._engine, self._registry = svod_live._engine, svod_live._registry
        svod_live._engine = self.engine
        svod_live._registry = lambda: self.registry

    def tearDown(self):
        svod_live._engine, svod_live._registry = self._engine, self._registry


# ------------------------------------------------------------------ настройки ---
class SettingsTest(unittest.TestCase):
    def test_defaults_when_room_has_no_fields(self):
        conf = svod_live.settings({"id": "cafe-leto"})
        self.assertTrue(conf["auto_svod"])
        self.assertEqual(conf["quiet_s"], 180)
        self.assertEqual(conf["min_gap_s"], 600)

    def test_values_are_clamped_not_broken(self):
        conf = svod_live.settings({"auto_svod_quiet_s": 5, "auto_svod_min_gap_s": 10 ** 6})
        self.assertEqual(conf["quiet_s"], 30, "5 секунд — меньше минимума, берём нижнюю границу")
        self.assertEqual(conf["min_gap_s"], 86400, "сутки — верхняя граница")
        broken = svod_live.settings({"auto_svod_quiet_s": "скоро", "auto_svod_min_gap_s": None})
        self.assertEqual((broken["quiet_s"], broken["min_gap_s"]), (180, 600))

    def test_off_is_respected(self):
        self.assertFalse(svod_live.settings({"auto_svod": False})["auto_svod"])

    def test_room_card_has_svod_fields(self):
        room = rooms._norm_room(make_room())
        self.assertTrue(room["auto_svod"])
        self.assertEqual(room["auto_svod_quiet_s"], QUIET)
        self.assertEqual(room["auto_svod_min_gap_s"], GAP)
        self.assertIn("Включено", room["svod"]["auto_svod_label"])

    def test_patch_validation_bounds(self):
        self.assertEqual(rooms._check_svod_quiet(30), 30)
        self.assertEqual(rooms._check_svod_gap(86400), 86400)
        with self.assertRaises(rooms.RoomsError):
            rooms._check_svod_quiet(10)
        with self.assertRaises(rooms.RoomsError):
            rooms._check_svod_gap(30)
        with self.assertRaises(rooms.RoomsError):
            rooms._check_auto_svod("да")

    def test_fields_are_accepted_by_patch(self):
        for field in ("auto_svod", "auto_svod_quiet_s", "auto_svod_min_gap_s"):
            self.assertIn(field, rooms.UPDATABLE)
            self.assertIn(field, app_common.PATCH_FIELDS)


# ------------------------------------------------------------------ состояния ---
class ComputeStateTest(unittest.TestCase):
    def test_off_without_svod(self):
        detail = make_detail(synthesis={"state": "pending", "version": 0, "at": None,
                                        "text_md": "", "history": []})
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "off")
        self.assertEqual(live["since_version"], 0)
        self.assertIn("ещё не выпущен", live["text"])

    def test_fresh_when_nothing_happened_after_version(self):
        detail = make_detail()
        # события (сообщение и решение) — ДО версии 2: свод актуален
        detail["thread"] = [{"id": "c1", "at": V2_AT - 950, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message", "text": "Десерт"}]
        detail["tree"] = detail["tree"][:2]
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}])
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "fresh")
        self.assertEqual(live["behind"], {"answers": 0, "notes": 0, "decisions": 0, "cards": 0})
        self.assertIsNone(live["next_update_at"])
        self.assertIn("Актуально на", live["text"])
        self.assertIn("версия 2", live["text"])
        self.assertIn("учтено: 1 ответ", live["text"])

    def test_pending_waits_for_silence(self):
        detail = make_detail()
        detail["thread"] = [{"id": "c2", "at": NOW - 60, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message",
                             "text": "Ещё добавьте сезонный суп"}]
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending")
        self.assertEqual(live["behind"], {"answers": 0, "notes": 1, "decisions": 0, "cards": 0})
        self.assertEqual(live["last_event_at"], NOW - 60)
        self.assertEqual(live["next_update_at"], NOW - 60 + QUIET,
                         "ждём «тишину» 3 минуты после последнего события")
        self.assertIn("Отстаёт: 1 сообщение не учтено", live["text"])
        self.assertIn("обновится в", live["text"])

    def test_pending_respects_min_gap_even_after_silence(self):
        detail = make_detail()
        # версия выпущена 2 минуты назад, событие «отстоялось» (тишина 30 секунд),
        # но потолок частоты (10 минут) ещё не прошёл
        detail["synthesis"]["history"][-1]["at"] = NOW - 120
        detail["synthesis"]["at"] = NOW - 120
        detail["thread"] = [{"id": "c9", "at": NOW - 60, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message", "text": "Правка"}]
        live = svod_live.compute(detail, make_room(auto_svod_quiet_s=30), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending")
        self.assertEqual(live["next_update_at"], NOW - 120 + GAP,
                         "потолок частоты: не чаще раза в 10 минут")

    def test_stale_when_auto_off(self):
        live = svod_live.compute(make_detail(), make_room(auto_svod=False), make_rec(), now=NOW)
        self.assertEqual(live["state"], "stale")
        self.assertFalse(live["auto"])
        self.assertIn("автообновление свода выключено", live["text"])

    def test_stale_without_leader(self):
        live = svod_live.compute(make_detail(), make_room(orchestrator=""), make_rec(), now=NOW)
        self.assertEqual(live["state"], "stale")
        self.assertIn("не выбран руководитель", live["text"])

    def test_updating_while_card_is_running(self):
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT},
                                    {"id": "t_4000dddd", "kind": "revision", "at": NOW - 30,
                                     "auto": True, "svod": True}])
        detail = make_detail()
        detail["tree"].append({"id": "t_4000dddd", "role": "revision", "status": "running",
                               "title": "Обновление свода (авто)", "assignee": "lead-anna",
                               "finished_at": None, "summary_md": "", "metadata": {}, "runs": []})
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "updating")
        self.assertIn("Обновляется", live["text"])
        self.assertIn("версия 3", live["text"])

    def test_counts_answers_decisions_and_cards(self):
        detail = make_detail()
        detail["open_questions"].append({"id": "q_bbbb3333", "text": "Сроки?", "for": "mikhail",
                                         "answered": True, "answer": "До пятницы",
                                         "answered_at": V2_AT + 120})
        detail["decisions"].append({"id": "d_bbbb4444", "text": "Взять «Ромашку» на сезон",
                                    "status": "proposed", "at": V2_AT + 300})
        detail["tree"].append({"id": "t_5000eeee", "role": "worker", "status": "done",
                               "title": "Сезонный суп", "assignee": "worker-petr",
                               "finished_at": V2_AT + 400, "summary_md": "Готово",
                               "metadata": {}, "runs": []})
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["behind"], {"answers": 1, "notes": 1, "decisions": 1, "cards": 1})
        self.assertIn("1 ответ", live["text"])
        self.assertIn("1 сообщение", live["text"])
        self.assertIn("1 решение", live["text"])
        self.assertIn("1 карточка", live["text"])

    def test_update_cards_do_not_make_it_update_again(self):
        """Защита от зацикливания: карточка свода и «Уточнение» событием не считаются."""
        detail = make_detail()
        detail["thread"] = []
        detail["tree"] = [
            {"id": "t_1000aaaa", "role": "root", "status": "done", "title": "Меню",
             "finished_at": V1_AT, "summary_md": "Версия 1", "metadata": {}, "runs": []},
            {"id": "t_2000bbbb", "role": "revision", "status": "done", "title": "Доработка",
             "finished_at": V2_AT, "summary_md": "Версия 2", "metadata": {}, "runs": []},
            {"id": "t_6000ffff", "role": "revision", "status": "done",
             "title": "Обновление свода (авто)", "finished_at": V2_AT + 100,
             "summary_md": "Версия 3", "metadata": {}, "runs": []},
            {"id": "t_7000abcd", "role": "worker", "status": "done",
             "title": "Уточнение от Михаила", "finished_at": V2_AT + 200,
             "summary_md": "Принял", "metadata": {}, "runs": []},
        ]
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}])
        ev = svod_live.events(detail, make_room(), rec)
        self.assertEqual(ev["total"], 0, "карточки свода и сообщений — не события")
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "fresh")


# ---------------------------------------------------------------------- план ---
class PlanTest(SvodBase):
    def test_plan_waits_during_quiet(self):
        detail = make_detail()
        detail["thread"] = [{"id": "c2", "at": NOW - 60, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message", "text": "Суп"}]
        decision = svod_live.plan(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["reason"], "quiet")
        self.assertEqual(decision["next_update_at"], NOW - 60 + QUIET)

    def test_plan_ready_after_quiet(self):
        now = V2_AT + 600 + QUIET + 1
        decision = svod_live.plan(make_detail(), make_room(), make_rec(), now=now)
        self.assertEqual(decision["action"], "create")
        self.assertTrue(decision["key"])

    def test_plan_respects_min_gap_after_launch(self):
        now = V2_AT + 600 + QUIET + 1
        rec = make_rec(svod_launched_at=now - 10)
        decision = svod_live.plan(make_detail(), make_room(), rec, now=now)
        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["reason"], "min_gap")

    def test_plan_same_events_do_not_repeat(self):
        now = V2_AT + 600 + QUIET + 1
        first = svod_live.plan(make_detail(), make_room(), make_rec(), now=now)
        rec = make_rec(svod_event_key=first["key"], svod_launched_at=now - GAP - 1)
        second = svod_live.plan(make_detail(), make_room(), rec, now=now)
        self.assertEqual(second["reason"], "same_events")
        self.assertNotEqual(second["action"], "create")

    def test_plan_does_nothing_when_auto_off(self):
        now = V2_AT + 600 + QUIET + 1
        decision = svod_live.plan(make_detail(), make_room(auto_svod=False), make_rec(), now=now)
        self.assertEqual(decision["action"], "none")
        self.assertEqual(decision["reason"], "auto_off")


# ---------------------------------------------------------------- карточка ---
class UpdateCardTest(SvodBase):
    def test_tick_creates_card_with_events_and_digest(self):
        now = V2_AT + 600 + QUIET + 1
        # since=V1_AT: автообновление включено ДО событий (обычная работа функции);
        # без него события считаются «старыми» — см. GraceStartTest
        res = svod_live.tick(CFG, make_room(), make_rec(), make_detail(), now=now, since=V1_AT)
        self.assertEqual(res["action"], "created")
        self.assertEqual(len(self.engine.created), 1)
        card = self.engine.created[0]
        self.assertEqual(card["kind"], "revision")
        self.assertEqual(card["assignee"], "lead-anna", "карточку берёт руководитель комнаты")
        self.assertEqual(card["title"], "Обновление свода (авто)")
        self.assertIn("Автообновление:", card["body"])
        self.assertIn("Уточнения Михаила в диалоге — 1", card["body"],
                      "в тело идёт тот же дайджест, что у кнопки «Доработать»")
        self.assertIn("Ещё добавьте сезонный суп", card["body"])
        self.assertTrue(card["idem_key"].startswith("svod-t_1000aaaa-"))
        # реестр: пометки «своя» и «свод» + ключ набора событий
        entry = self.registry.cards[0]["card"]
        self.assertTrue(entry["auto"])
        self.assertTrue(entry["svod"])
        self.assertEqual(entry["kind"], "revision")
        self.assertEqual(entry["events"]["notes"], 1)
        self.assertTrue(self.registry.patches, "ключ набора событий записан в реестр")
        self.assertEqual(self.registry.patches[0]["patch"]["svod_event_key"], res["key"])

    def test_second_tick_does_not_create_second_card(self):
        now = V2_AT + 600 + QUIET + 1
        svod_live.tick(CFG, make_room(), make_rec(), make_detail(), now=now, since=V1_AT)
        patch = self.registry.patches[0]["patch"]
        rec = make_rec(svod_event_key=patch["svod_event_key"],
                       svod_launched_at=patch["svod_launched_at"])
        again = svod_live.tick(CFG, make_room(), rec, make_detail(), now=now + GAP + 1,
                               since=V1_AT)
        self.assertEqual(len(self.engine.created), 1, "один запуск на набор событий")
        self.assertEqual(again["action"], "none")
        self.assertEqual(again["reason"], "same_events")

    def test_refresh_now_ignores_quiet_but_returns_conflict_while_busy(self):
        rec = make_rec()
        res = svod_live.refresh_now(CFG, make_room(), rec, make_detail(), now=NOW)
        self.assertTrue(res["card_id"])
        self.assertEqual(self.engine.created[0]["title"], "Обновление свода")
        busy = make_rec(extra_cards=[{"id": "t_8888dddd", "kind": "revision", "at": NOW - 20,
                                      "auto": True, "svod": True}])
        detail = make_detail()
        detail["tree"].append({"id": "t_8888dddd", "role": "revision", "status": "running",
                               "title": "Обновление свода (авто)", "finished_at": None,
                               "summary_md": "", "metadata": {}, "runs": []})
        with self.assertRaises(svod_live.SvodError) as ctx:
            svod_live.refresh_now(CFG, make_room(), busy, detail, now=NOW)
        self.assertEqual(ctx.exception.code, "conflict")

    def test_refresh_without_svod_is_conflict(self):
        detail = make_detail(synthesis={"state": "pending", "version": 0, "text_md": "",
                                        "history": []})
        with self.assertRaises(svod_live.SvodError) as ctx:
            svod_live.refresh_now(CFG, make_room(), make_rec(), detail, now=NOW)
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(self.engine.created, [])

    def test_restart_before_registry_write_uses_the_same_idem_key(self):
        """W21-REV2: рестарт между постановкой карточки и записью реестра.

        Если процесс упал после `create_child_card` и до `_remember`, реестр не
        знает ни ключа набора событий, ни карточки. Второй тик попробует поставить
        карточку ещё раз, но с ТЕМ ЖЕ ключом идемпотентности — движок (`hermes
        kanban create --idempotency-key`) вернёт существующую карточку, дубля не
        будет. Тест фиксирует именно ключ: он и есть защита.
        """
        now = V2_AT + 600 + QUIET + 1
        first = svod_live.tick(CFG, make_room(), make_rec(), make_detail(), now=now, since=V1_AT)
        self.assertEqual(first["action"], "created")
        key = self.engine.created[0]["idem_key"]
        # реестр «пуст»: запись не успела лечь, дерево карточку ещё не показывает
        again = svod_live.tick(CFG, make_room(), make_rec(), make_detail(), now=now + 1,
                               since=V1_AT)
        self.assertEqual(again["action"], "created", "план не знает про карточку")
        self.assertEqual(self.engine.created[1]["idem_key"], key,
                         "ключ идемпотентности тот же — движок не создаст вторую карточку")


# ------------------------------------- «льготный старт» и лимит карточек ---
class StateCfg:
    """Настройки с каталогом состояния: файл «льготного старта» пишется на диск."""

    def __init__(self, base: Path):
        self.state_dir = base / "state"
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"


class GraceStartTest(SvodBase):
    """После переноса в боевое дерево старые запросы не должны ставить доработки.

    Проверяем два предохранителя из ТЗ волны 20 (§1.1): «льготный старт» (только
    события после включения автообновления) и лимит карточек в час на комнату.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.cfg = StateCfg(self.base)

    def test_first_run_ignores_old_events_and_reacts_to_new_ones(self):
        now = V2_AT + 3600                      # первый запуск новой версии
        decision = svod_live.tick(self.cfg, make_room(), make_rec(), make_detail(), now=now)
        self.assertEqual(decision["action"], "none")
        self.assertEqual(self.engine.created, [],
                         "старые ответы и сообщения не поднимают доработку при первом запуске")
        self.assertEqual(svod_live.start_at(self.cfg, now), now,
                         "момент включения запомнен, а не пересчитывается каждый тик")

        live = svod_live.compute(make_detail(), make_room(), make_rec(), now=now,
                                 since=svod_live.start_at(self.cfg, now))
        self.assertEqual(live["state"], "fresh")
        self.assertEqual(sum(live["behind"].values()), 0)
        self.assertNotIn("Отстаёт", live["text"],
                         "старый запрос не показывается как «отстаёт» — иначе ждёт вечно")

        detail = make_detail()
        detail["thread"].append({"id": "c9", "at": now + 30, "author": "mikhail",
                                 "author_kind": "mikhail", "kind": "message",
                                 "text": "Добавьте вегетарианское меню."})
        again = svod_live.tick(self.cfg, make_room(), make_rec(), detail,
                               now=now + 30 + QUIET + 1)
        self.assertEqual(again["action"], "created")
        self.assertEqual(len(self.engine.created), 1, "новое событие — обычная работа свода")

    def test_marker_survives_restart(self):
        now = V2_AT + 3600
        svod_live.tick(self.cfg, make_room(), make_rec(), make_detail(), now=now)
        after_restart = StateCfg(self.base)     # тот же каталог состояния
        self.assertEqual(svod_live.start_at(after_restart, now + 7200), now,
                         "перезапуск сервера не сдвигает порог и не открывает старые запросы")

    def test_hour_limit_stops_automatic_cards(self):
        now = V2_AT + 3600
        path = svod_live._start_path(self.cfg)
        svod_live._save_start(path, {"started_at": V1_AT,
                                     "launches": {"cafe-leto": [now - 60] * svod_live.MAX_CARDS_PER_HOUR}})
        decision = svod_live.tick(self.cfg, make_room(), make_rec(), make_detail(), now=now)
        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["reason"], "hour_limit")
        self.assertEqual(self.engine.created, [], "больше N карточек в час не ставим")

    def test_hour_limit_counts_only_this_room_and_last_hour(self):
        now = V2_AT + 3600
        path = svod_live._start_path(self.cfg)
        svod_live._save_start(path, {"started_at": V1_AT, "launches": {
            "cafe-leto": [now - svod_live.HOUR_S - 5] * (svod_live.MAX_CARDS_PER_HOUR + 3),
            "other-room": [now - 60] * 2}})
        self.assertEqual(svod_live.launches_last_hour(self.cfg, make_room(), now), 0,
                         "карточки старше часа не считаются")
        self.assertFalse(svod_live.hour_limit_reached(self.cfg, make_room(), now))
        self.assertEqual(svod_live.launches_last_hour(self.cfg, {"id": "other-room"}, now), 2)

    def test_created_card_counts_toward_the_limit(self):
        now = V2_AT + 3600
        svod_live.tick(self.cfg, make_room(), make_rec(), make_detail(),
                       now=now + 600 + QUIET + 1, since=V1_AT)
        self.assertEqual(len(self.engine.created), 1)
        self.assertEqual(svod_live.launches_last_hour(self.cfg, make_room(), now + 600 + QUIET + 1), 1)

    def test_manual_refresh_ignores_grace_start_and_limit(self):
        now = V2_AT + 3600
        path = svod_live._start_path(self.cfg)
        svod_live._save_start(path, {"started_at": now + 3600,     # порог в будущем: старые события
                                     "launches": {"cafe-leto": [now - 60] * svod_live.MAX_CARDS_PER_HOUR}})
        res = svod_live.refresh_now(self.cfg, make_room(), make_rec(), make_detail(), now=now)
        self.assertTrue(res["card_id"], "кнопка «Обновить сейчас» работает всегда")
        self.assertEqual(len(self.engine.created), 1)


# ---------------------------------------------------------------- доставка ---
class DeliveryTest(unittest.TestCase):
    def _items(self, detail):
        svod_live.attach(detail, make_room(), make_rec(), now=NOW)
        return {item["id"]: item for item in detail["thread"]}

    def test_message_before_version_is_applied(self):
        detail = make_detail()
        items = self._items(detail)
        delivery = items["c1"]["delivery"]
        self.assertEqual(delivery["state"], "applied")
        self.assertEqual(delivery["version"], 2)
        self.assertEqual(delivery["text"], "Учтено в версии 2")
        self.assertEqual(delivery["cards"], ["t_2000bbbb"])

    def test_message_after_version_waits_for_update(self):
        detail = make_detail()
        items = self._items(detail)
        delivery = items["c2"]["delivery"]
        self.assertEqual(delivery["state"], "waiting")
        self.assertIn("Ждёт обновления свода", delivery["text"])
        self.assertIn("в ", delivery["text"])

    def test_message_in_work_when_update_card_runs(self):
        detail = make_detail()
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT},
                                    {"id": "t_9999eeee", "kind": "revision", "at": NOW - 20,
                                     "auto": True, "svod": True}])
        detail["tree"].append({"id": "t_9999eeee", "role": "revision", "status": "running",
                               "title": "Обновление свода (авто)", "finished_at": None,
                               "summary_md": "", "metadata": {}, "runs": []})
        svod_live.attach(detail, make_room(), rec, now=NOW)
        delivery = {item["id"]: item for item in detail["thread"]}["c2"]["delivery"]
        self.assertEqual(delivery["state"], "working")
        self.assertEqual(delivery["cards"], ["t_9999eeee"])
        self.assertEqual(delivery["text"], "В работе: 1 карточка")

    def test_message_declined_when_auto_off(self):
        detail = make_detail()
        svod_live.attach(detail, make_room(auto_svod=False), make_rec(), now=NOW)
        delivery = {item["id"]: item for item in detail["thread"]}["c2"]["delivery"]
        self.assertEqual(delivery["state"], "declined")
        self.assertEqual(delivery["reason"], "auto_off")
        self.assertIn("Не учтено", delivery["text"])

    def test_only_messages_of_human_get_delivery(self):
        detail = make_detail()
        items = self._items(detail)
        self.assertNotIn("delivery", items["c3"], "комментарий руководителя — не сообщение Михаила")

    def test_live_is_inside_synthesis(self):
        detail = make_detail()
        svod_live.attach(detail, make_room(), make_rec(), now=NOW)
        live = detail["synthesis"]["live"]
        for key in ("state", "since_version", "behind", "last_event_at", "next_update_at",
                    "auto", "text"):
            self.assertIn(key, live)
        self.assertEqual(set(live["behind"]), {"answers", "notes", "decisions", "cards"})


# ------------------------------------------------------- версии и контракт ---
def _node(task_id, role, status="done", at=V2_AT, summary="Текст", metadata=None, assignee="lead-anna"):
    return {"id": task_id, "role": role, "status": status, "title": "Карточка", "assignee": assignee,
            "summary_md": summary, "metadata": metadata or {}, "finished_at": at,
            "runs": ([{"ended_at": at}] if at else [])}


def _synthesis(nodes, extra_cards):
    ctx = {"root_id": nodes[0]["id"]}
    return engine_tree._synthesis(ctx, nodes, extra_cards)


class VersionsTest(unittest.TestCase):
    def test_leader_card_with_metadata_svod_is_new_version(self):
        nodes = [_node("t_1000aaaa", "root", at=V1_AT, summary="Версия 1"),
                 _node("t_b0001111", "worker", at=V2_AT,
                       metadata={"svod": {"note": "учли замечание про сроки",
                                          "changes": ["сроки поставки", "состав меню"]}})]
        synth, _canon, _warn = _synthesis(nodes, [])
        self.assertEqual(synth["version"], 2)
        self.assertEqual(synth["source_task"], "t_b0001111")
        self.assertEqual(synth["history"][-1]["kind"], "leader")
        self.assertEqual(synth["history"][-1]["source_task"], "t_b0001111")
        self.assertEqual(synth["history"][-1]["note"], "учли замечание про сроки")
        self.assertEqual(synth["history"][-1]["changes"], ["сроки поставки", "состав меню"])

    def test_app_update_card_is_marked_auto(self):
        nodes = [_node("t_1000aaaa", "root", at=V1_AT, summary="Версия 1"),
                 _node("t_c0002222", "revision", at=V2_AT, summary="Версия 2")]
        extra = [{"id": "t_c0002222", "kind": "revision", "at": V2_AT, "auto": True,
                  "svod": True, "note": "Автообновление: учтён 1 ответ"}]
        synth, _canon, _warn = _synthesis(nodes, extra)
        entry = synth["history"][-1]
        self.assertTrue(entry.get("auto"))
        self.assertEqual(entry["kind"], "revision")
        self.assertEqual(entry["note"], "Автообновление: учтён 1 ответ")

    def test_manual_revision_is_not_auto(self):
        nodes = [_node("t_1000aaaa", "root", at=V1_AT, summary="Версия 1"),
                 _node("t_d0003333", "revision", at=V2_AT, summary="Версия 2")]
        extra = [{"id": "t_d0003333", "kind": "revision", "at": V2_AT, "note": "по замечанию"}]
        synth, _canon, _warn = _synthesis(nodes, extra)
        self.assertFalse(synth["history"][-1].get("auto"))
        self.assertEqual(synth["history"][-1]["kind"], "revision")


class ContractTest(unittest.TestCase):
    def test_leader_is_forbidden_to_advise_reopening_root(self):
        rule = contract.svod_rule()
        self.assertIn("обновляет ПРИЛОЖЕНИЕ", rule)
        self.assertIn("НИКОГДА не советуй Михаилу «переоткрыть»", rule)
        self.assertIn("metadata.svod", rule)

    def test_contract_block_contains_the_rule_and_svod_metadata(self):
        cfg = FakeCfg(ROOT / "tests" / "_svod_tmp")
        block = contract.build_contract_block(make_room(), "answer", cfg)
        self.assertIn("Свод команды обновляет ПРИЛОЖЕНИЕ", block)
        self.assertIn("переоткрыть", block)
        self.assertIn("svod:         {", block)

    def test_revision_body_keeps_the_rule(self):
        cfg = FakeCfg(ROOT / "tests" / "_svod_tmp")
        body = contract.build_revision_body(make_room(), "Запрос", "Ответ", [], "Заметка", cfg,
                                            digest={"version": 2, "text": "Уточнения — 1"})
        self.assertIn("Свод команды обновляет ПРИЛОЖЕНИЕ", body)
        self.assertIn("Уточнения — 1", body)


class ActionTest(unittest.TestCase):
    def test_svod_refresh_action_is_known(self):
        self.assertIn("svod_refresh", app_common.ACTION_ALLOWED)
        self.assertNotIn("svod_refresh", app_common.ACTION_REQUIRED)


# ------------------------------------- ответы как приоритетное событие (B-45) ---
def _detail_with_answer(at, note_at=None, version_at=None):
    """Деталь, где после версии есть ответ на вопрос (и, может быть, сообщение)."""
    detail = make_detail()
    detail["thread"] = []
    if version_at is not None:
        detail["synthesis"]["at"] = version_at
        detail["synthesis"]["history"][-1]["at"] = version_at
    detail["open_questions"].append({"id": "q_cccc5555", "text": "Кто везёт десерт?",
                                     "for": "mikhail", "answered": True, "answer": "Марина",
                                     "answered_at": at})
    if note_at:
        detail["thread"].append({"id": "c7", "at": note_at, "author": "mikhail",
                                 "author_kind": "mikhail", "kind": "message",
                                 "text": "И добавьте сезонный суп"})
    return detail


class AnswersPriorityTest(unittest.TestCase):
    """B-45 (сервер): ответ на вопрос руководителя — приоритетное событие свода."""

    def test_answer_waits_one_minute_not_three(self):
        detail = _detail_with_answer(NOW - 30)
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending")
        self.assertEqual(live["behind"]["answers"], 1)
        self.assertEqual(live["next_update_at"], NOW - 30 + svod_live.ANSWERS_QUIET_S,
                         "после ответа ждём 60 с, а не 180")
        self.assertLess(live["next_update_at"], NOW - 30 + QUIET)
        self.assertIn("обновится в", live["text"])

    def test_room_setting_shorter_than_a_minute_wins(self):
        detail = _detail_with_answer(NOW - 5)
        live = svod_live.compute(detail, make_room(auto_svod_quiet_s=30), make_rec(), now=NOW)
        self.assertEqual(live["next_update_at"], NOW - 5 + 30,
                         "если комната ждёт меньше минуты, берём её настройку")

    def test_min_gap_does_not_hold_the_answer_update(self):
        detail = _detail_with_answer(NOW - 70, version_at=NOW - 120)
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        # B-49: расчётное время уже позади — в тексте «в ближайшие минуты», прошедшее не отдаём
        self.assertIsNone(live["next_update_at"])
        self.assertIn("ближайшие минуты", live["text"])
        decision = svod_live.plan(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(decision["action"], "create", decision)
        self.assertEqual(decision["next_update_at"], NOW - 10,
                         "тишина ответа прошла, а потолок частоты её не задерживает")

    def test_note_still_waits_full_quiet_and_min_gap(self):
        detail = make_detail()
        detail["thread"] = [{"id": "c7", "at": NOW - 70, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message",
                             "text": "Добавьте сезонный суп"}]
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["next_update_at"], NOW - 70 + QUIET,
                         "для обычного сообщения правила прежние")
        decision = svod_live.plan(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual((decision["action"], decision["reason"]), ("wait", "quiet"))

    def test_min_gap_after_launch_does_not_hold_the_answer_either(self):
        detail = _detail_with_answer(NOW - 70, version_at=NOW - 120)
        rec = make_rec(svod_launched_at=NOW - 30)      # меньше min_gap назад
        decision = svod_live.plan(detail, make_room(), rec, now=NOW)
        self.assertEqual(decision["action"], "create", decision)


class AnswersPriorityLimitTest(SvodBase):
    """Лимит карточек в час остаётся и для приоритетных ответов (B-45)."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = StateCfg(Path(self._tmp.name))

    def test_hour_limit_still_applies_to_answer_updates(self):
        path = svod_live._start_path(self.cfg)
        svod_live._save_start(path, {"started_at": V1_AT,
                                     "launches": {"cafe-leto": [NOW - 60] * svod_live.MAX_CARDS_PER_HOUR}})
        detail = _detail_with_answer(NOW - 70, version_at=NOW - 120)
        res = svod_live.tick(self.cfg, make_room(), make_rec(), detail, now=NOW, since=V1_AT)
        self.assertEqual((res["action"], res["reason"]), ("wait", "hour_limit"))
        self.assertEqual(self.engine.created, [], "шестая карточка в час — уже предел")

    def test_answer_priority_does_not_bypass_grace_start(self):
        """W21-REV2: приоритет ответа не отменяет «льготный старт».

        Иначе первый тик после переноса в боевое дерево поднял бы доработку по
        каждому старому ответу — ровно то, от чего «льготный старт» защищает.
        """
        now = V2_AT + 3600
        svod_live.tick(self.cfg, make_room(), make_rec(), make_detail(), now=now)
        floor = svod_live.start_at(self.cfg, now)
        old = _detail_with_answer(now - 3600)               # ответ ЧАС назад, до порога
        live = svod_live.compute(old, make_room(), make_rec(), now=now, since=floor)
        self.assertEqual(live["state"], "fresh")
        self.assertEqual(live["behind"]["answers"], 0)
        decision = svod_live.plan(old, make_room(), make_rec(), now=now, since=floor)
        self.assertEqual(decision["action"], "none",
                         "старый ответ не ставит карточку даже как приоритетный")
        fresh = _detail_with_answer(now + 10)               # ответ после порога
        res = svod_live.tick(self.cfg, make_room(), make_rec(), fresh, now=now + 80,
                             since=floor)
        self.assertEqual(res["action"], "created", "новый ответ — обычная работа свода")


# ------------------------------ ждать завершения карточек команды (B-46) ---
def _chain_node(task_id, title, status, role="worker", assignee="worker-marina", **over):
    node = {"id": task_id, "role": role, "status": status, "title": title, "assignee": assignee,
            "finished_at": None, "summary_md": "", "metadata": {}, "runs": []}
    node.update(over)
    return node


class WaitingCardsTest(SvodBase):
    """B-46: свод не собирается, пока на запросе идёт работа команды."""

    def _detail(self):
        detail = make_detail()
        detail["thread"] = [{"id": "c7", "at": NOW - 30, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message",
                             "text": "Добавьте сезонный суп"}]
        detail["tree"] += [
            _chain_node("t_7101aaaa", "Проверка редакции 12 письма", "running"),
            _chain_node("t_7102bbbb", "Критика редакции 12", "todo", role="critic",
                        assignee="critic-lev"),
            _chain_node("t_7103cccc", "Сбор цен у «Ромашки»", "done", assignee="worker-petr",
                        finished_at=V2_AT - 100, summary_md="Готово"),
        ]
        return detail

    def test_chain_holds_the_svod(self):
        detail = self._detail()
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "waiting_cards")
        self.assertEqual(live["text"], "Обновится после завершения работы команды")
        self.assertIsNone(live["next_update_at"])
        busy = svod_live.busy_cards(detail, make_rec(), NOW)
        self.assertEqual([c["id"] for c in busy], ["t_7101aaaa", "t_7102bbbb"],
                         "готовые карточки свод не держат")

    def test_plan_waits_and_tick_creates_nothing(self):
        detail = self._detail()
        now = NOW + 3600                     # «тишина» и потолок частоты давно прошли
        decision = svod_live.plan(detail, make_room(), make_rec(), now=now)
        self.assertEqual((decision["action"], decision["reason"]), ("wait", "waiting_cards"))
        res = svod_live.tick(CFG, make_room(), make_rec(), detail, now=now, since=V1_AT)
        self.assertEqual(res["reason"], "waiting_cards")
        self.assertEqual(self.engine.created, [], "карточка не ставится, пока идёт цепочка")

    def test_after_chain_finished_svod_updates(self):
        detail = self._detail()
        for node in detail["tree"]:
            if node["id"].startswith("t_71"):
                node["status"] = "done"
                node["finished_at"] = V2_AT - 50
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending")
        res = svod_live.tick(CFG, make_room(), make_rec(), detail, now=NOW + 3600, since=V1_AT)
        self.assertEqual(res["action"], "created")
        self.assertEqual(len(self.engine.created), 1, "после цепочки — обычная «тишина»")

    def test_card_waiting_for_human_does_not_hold(self):
        detail = self._detail()
        detail["tree"] = detail["tree"][:2] + [
            _chain_node("t_7104dddd", "Согласовать поставщика", "blocked",
                        needs_human=True)]
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending", "иначе свод ждал бы вечно")
        res = svod_live.tick(CFG, make_room(), make_rec(), detail, now=NOW + 3600, since=V1_AT)
        self.assertEqual(res["action"], "created")

    def test_message_card_does_not_hold(self):
        detail = self._detail()
        detail["tree"] = [n for n in detail["tree"] if not n["id"].startswith("t_71")]
        detail["tree"].append(_chain_node("t_7105eeee", "Уточнение от Михаила", "ready"))
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending", "её содержимое уже в диалоге")

    def test_delivery_explains_why_it_waits(self):
        detail = self._detail()
        svod_live.attach(detail, make_room(), make_rec(), now=NOW)
        item = [i for i in detail["thread"] if i["id"] == "c7"][0]
        self.assertEqual(item["delivery"]["text"], "Ждёт завершения работы команды")


class WaitingCardsCeilingTest(SvodBase):
    """W21-REV2: брошенная карточка «в работе» не держит свод вечно.

    У карточки в дереве нет сердцебиений — только время старта. Если руководитель
    закрыл приложение и карточка осталась `running` на сутки, старое правило
    `busy_cards` держало бы свод бесконечно («Обновится после завершения работы
    команды» навсегда). Потолок ожидания — два часа, после него свод собирается,
    а причина пишется в `live.text` и в заметку карточки обновления.
    """

    def _detail(self, age_s: int):
        detail = make_detail()
        detail["thread"] = [{"id": "c7", "at": NOW - 30, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message",
                             "text": "Добавьте сезонный суп"}]
        detail["tree"] = [n for n in detail["tree"] if not str(n.get("id")).startswith("t_71")]
        detail["tree"].append(_chain_node("t_7201aaaa", "Зависшая проверка письма", "running",
                                         started_at=NOW - age_s))
        return detail

    def test_ceiling_constant_is_two_hours(self):
        self.assertEqual(svod_live.BUSY_MAX_S, 7200)

    def test_fresh_card_still_holds(self):
        detail = self._detail(age_s=600)
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "waiting_cards")
        self.assertEqual([c["id"] for c in svod_live.busy_cards(detail, make_rec(), NOW)],
                         ["t_7201aaaa"])
        self.assertEqual(svod_live.stalled_cards(detail, make_rec(), NOW), [])

    def test_stuck_card_stops_holding_after_two_hours(self):
        detail = self._detail(age_s=3 * 3600)
        self.assertEqual(svod_live.busy_cards(detail, make_rec(), NOW), [])
        self.assertEqual([c["id"] for c in svod_live.stalled_cards(detail, make_rec(), NOW)],
                         ["t_7201aaaa"], "зависшая карточка видна наверх, но не держит свод")
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending")
        self.assertIn("работа команды идёт дольше двух часов", live["text"])

    def test_stuck_card_does_not_freeze_a_week_later(self):
        detail = self._detail(age_s=7 * 24 * 3600)
        now = NOW + 7 * 24 * 3600
        live = svod_live.compute(detail, make_room(), make_rec(), now=now)
        self.assertNotEqual(live["state"], "waiting_cards")
        decision = svod_live.plan(detail, make_room(), make_rec(), now=now)
        self.assertEqual(decision["action"], "create", "иначе свод не обновился бы никогда")
        self.assertEqual(decision["stalled"], ["t_7201aaaa"])

    def test_note_says_it_did_not_wait(self):
        detail = self._detail(age_s=3 * 3600)
        ev = svod_live.events(detail, make_room(), make_rec(), thread=None)
        note = svod_live.auto_note(ev, svod_live.stalled_cards(detail, make_rec(), NOW))
        self.assertIn("не дожидаясь карточек", note)
        self.assertIn("Зависшая проверка письма", note)

    def test_card_without_time_still_holds(self):
        """Нет времени старта — доказать «зависла» нельзя: ждём (как раньше)."""
        detail = self._detail(age_s=600)
        for node in detail["tree"]:
            if node["id"] == "t_7201aaaa":
                node.pop("started_at", None)
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW + 5 * 3600)
        self.assertEqual(live["state"], "waiting_cards")
        self.assertEqual(svod_live.card_started_at({"id": "t_x"}), 0)


# ------------------- снятие и возврат решения — не событие свода (B-47) ---
class DismissNotEventTest(SvodBase):
    """B-47: строка «Снять — неактуально»/«Вернуть» свод не поднимает."""

    def _detail(self, action, mark=None):
        detail = make_detail()
        text = mark or answers_mod.decision_comment(
            action, "d_aaaa2222", "Закупать у «Ромашки»", "переговоры не сложились")
        detail["thread"] = [
            {"id": "c1", "at": V2_AT - 950, "author": "mikhail", "author_kind": "mikhail",
             "kind": "message", "text": "Десерт — по 40 порций"},
            {"id": "c5", "at": NOW - 60, "author": "mikhail", "author_kind": "mikhail",
             "kind": "message", "text": text},
        ]
        return detail

    def test_dismiss_is_not_an_event(self):
        detail = self._detail("dismiss")
        ev = svod_live.events(detail, make_room(), make_rec())
        self.assertEqual(ev["total"], 0, "уборка списка решений — не событие свода")
        live = svod_live.compute(detail, make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "fresh")
        res = svod_live.tick(CFG, make_room(), make_rec(), detail, now=NOW + 3600, since=V1_AT)
        self.assertNotEqual(res["action"], "created")
        self.assertEqual(self.engine.created, [], "руководителя не будим из-за «Снять»")

    def test_restore_is_not_an_event(self):
        detail = self._detail("restore")
        ev = svod_live.events(detail, make_room(), make_rec())
        self.assertEqual(ev["total"], 0)
        res = svod_live.tick(CFG, make_room(), make_rec(), detail, now=NOW + 3600, since=V1_AT)
        self.assertEqual(self.engine.created, [])

    def test_decision_answer_still_counts(self):
        detail = self._detail("accept")
        ev = svod_live.events(detail, make_room(), make_rec())
        self.assertEqual(ev["behind"]["notes"], 1, "«Принять» — по-прежнему сообщение Михаила")

    def test_delivery_says_nothing_to_change(self):
        detail = self._detail("dismiss")
        svod_live.attach(detail, make_room(), make_rec(), now=NOW)
        item = [i for i in detail["thread"] if i["id"] == "c5"][0]
        self.assertEqual(item["delivery"]["state"], "applied_nochange")
        self.assertEqual(item["delivery"]["text"], "Учтено, менять нечего")
        self.assertEqual(item["delivery"]["reason"], "decision_status")

    def test_thread_marks_the_line(self):
        self.assertEqual(thread_mod._decision_status_action(self._detail("dismiss")["thread"][1]["text"]),
                         "dismiss")
        self.assertEqual(thread_mod._decision_status_action(self._detail("restore")["thread"][1]["text"]),
                         "restore")
        self.assertEqual(thread_mod._decision_status_action(self._detail("accept")["thread"][1]["text"]),
                         "", "«Принять» — не уборка списка")


# ------------------- «остывание» после неудачной карточки (ревью волны 1) ---
class CooldownTest(SvodBase):
    """Неудачная карточка обновления не повторяется на каждом тике."""

    def _case(self, failures, status="failed"):
        """failures — [(id, at)] неудачных карточек обновления (любой порядок)."""
        detail = make_detail()
        detail["thread"] = [{"id": "c7", "at": V2_AT + 100, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message",
                             "text": "Добавьте сезонный суп"}]
        cards = [{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}]
        for card_id, at in failures:
            cards.append({"id": card_id, "kind": "revision", "at": at, "auto": True,
                          "svod": True, "event_key": "k-%s" % card_id})
            detail["tree"].append({"id": card_id, "role": "revision", "status": status,
                                   "title": "Обновление свода (авто)", "assignee": "lead-anna",
                                   "finished_at": at, "summary_md": "", "metadata": {},
                                   "runs": [{"ended_at": at, "status": status}]})
        return detail, make_rec(extra_cards=cards)

    def test_failed_card_does_not_look_like_updating(self):
        detail, rec = self._case([("t_f001aaaa", NOW - 100)])
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertNotEqual(live["state"], "updating")
        self.assertEqual(live["state"], "pending")
        self.assertEqual(live["next_update_at"], NOW - 100 + svod_live.COOLDOWN_BASE_S)
        self.assertIn("Обновление не удалось — повтор в", live["text"])
        self.assertIn("Обновить сейчас", live["text"])

    def test_ten_twenty_forty_minutes(self):
        for attempts, wait in ((1, 600), (2, 1200), (3, 2400)):
            failures = [("t_f%03dbbbb" % i, NOW - 100 - i) for i in range(attempts)]
            detail, rec = self._case(failures)
            found = svod_live.failed_updates(detail, rec)
            self.assertEqual(found["attempts"], attempts)
            self.assertEqual(found["next_at"] - found["at"], wait,
                             "%d неудач подряд → %d мин" % (attempts, wait // 60))

    def test_wait_is_capped_at_two_hours(self):
        failures = [("t_f%03dcccc" % i, NOW - 100 - i) for i in range(7)]
        detail, rec = self._case(failures)
        found = svod_live.failed_updates(detail, rec)
        self.assertEqual(found["next_at"] - found["at"], svod_live.COOLDOWN_MAX_S)

    def test_plan_waits_during_cooldown(self):
        detail, rec = self._case([("t_f002aaaa", NOW - 100)])
        decision = svod_live.plan(detail, make_room(), rec, now=NOW)
        self.assertEqual((decision["action"], decision["reason"]), ("wait", "cooldown"))
        self.assertEqual(decision["next_update_at"], NOW + 500)
        self.assertEqual(self.engine.created, [])

    def test_retry_after_cooldown_even_with_the_same_events(self):
        detail, rec = self._case([("t_f003aaaa", V2_AT + 200)])
        key = svod_live.event_key(svod_live.events(detail, make_room(), rec))
        rec = dict(rec, svod_event_key=key, svod_launched_at=V2_AT + 200)
        detail["tree"][-1].setdefault("metadata", {})
        rec["extra_cards"][-1]["event_key"] = key
        self.assertEqual(svod_live.plan(detail, make_room(), rec, now=V2_AT + 200 + 601)["reason"],
                         "ready")
        self.assertEqual(svod_live.plan(detail, make_room(), rec, now=V2_AT + 200 + 599)["reason"],
                         "cooldown")

    def test_blocked_card_counts_as_failure(self):
        detail, rec = self._case([("t_f004aaaa", NOW - 100)], status="blocked")
        found = svod_live.failed_updates(detail, rec)
        self.assertEqual(found["attempts"], 1)
        self.assertEqual(found["status"], "blocked")

    def test_retry_limit_leaves_the_button_to_the_human(self):
        failures = [("t_f%03ddddd" % i, NOW - 100 - i) for i in range(svod_live.MAX_AUTO_RETRIES)]
        detail, rec = self._case(failures)
        now = NOW + svod_live.COOLDOWN_MAX_S + 10
        decision = svod_live.plan(detail, make_room(), rec, now=now)
        self.assertEqual(decision["reason"], "retry_limit")
        live = svod_live.compute(detail, make_room(), rec, now=now)
        self.assertEqual(live["state"], "stale")
        self.assertIn("Обновить сейчас", live["text"])

    def test_manual_refresh_works_during_cooldown(self):
        detail, rec = self._case([("t_f005aaaa", NOW - 100)])
        res = svod_live.refresh_now(CFG, make_room(), rec, detail, now=NOW)
        self.assertTrue(res["card_id"], "кнопка «Обновить сейчас» работает всегда")


# --------------- «не молчим», если отметку о старте сохранить нельзя ---
class ProblemLog:
    def __init__(self):
        self.lines = []

    def warning(self, fmt, *args):
        self.lines.append(fmt % args if args else str(fmt))


class StateProblemTest(SvodBase):
    def setUp(self):
        super().setUp()
        svod_live._PROBLEM_LOGGED.clear()

    def _detail(self):
        detail = make_detail()
        detail["thread"] = [{"id": "c7", "at": NOW - 30, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message",
                             "text": "Добавьте сезонный суп"}]
        return detail

    def test_live_says_it_cannot_remember_the_start(self):
        live = svod_live.attach(self._detail(), make_room(), make_rec(), now=NOW,
                                cfg=CFG, since=0)
        self.assertEqual(live["state"], "stale")
        self.assertIn("не может сохранить отметку", live["text"])
        self.assertIn("Обновить сейчас", live["text"])

    def test_without_cfg_the_old_behaviour_stays(self):
        live = svod_live.attach(self._detail(), make_room(), make_rec(), now=NOW)
        self.assertEqual(live["state"], "pending")

    def test_readonly_marker_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = StateCfg(Path(tmp))
            self.assertEqual(svod_live.state_problem(cfg, NOW), "", "каталог состояния доступен")
            self.assertEqual(svod_live._problem_of(cfg, {"readonly": True}), "unreadable")
            self.assertEqual(svod_live._problem_of(FakeCfg(Path(tmp)), {}), "no_state_dir")
        self.assertEqual(svod_live.PROBLEM_TEXT["unreadable"], "отметка в папке данных недоступна")

    def test_watcher_logs_once_an_hour(self):
        log = ProblemLog()
        svod_live.tick(CFG, make_room(), make_rec(), self._detail(), now=NOW, since=V1_AT, log=log)
        svod_live.tick(CFG, make_room(), make_rec(), self._detail(), now=NOW + 60,
                       since=V1_AT, log=log)
        self.assertEqual(len(log.lines), 1, "один сбой — одна строка в журнале, а не каждые 10 с")
        svod_live.tick(CFG, make_room(), make_rec(), self._detail(), now=NOW + 3700,
                       since=V1_AT, log=log)
        self.assertEqual(len(log.lines), 2)


# ------------------------------------------- живой сервер в песочнице (shim) ---
# Импорт ниже — ПОСЛЕ вставки ROOT в sys.path: тестовая песочница та же, что у
# остальных серверных наборов (CLI Hermes подменён shim'ом, реальная доска не
# трогается). См. docs/WAVE17-RULES.md раздел 0.
from tests.test_answers import LEAD, ROOM, _Base  # noqa: E402


class LiveServerTest(_Base):
    """Живой свод на живом сервере: деталь запроса и действие «Обновить сейчас»."""

    def test_detail_has_live_and_message_delivery(self):
        root = self.rid()
        detail = self.detail(root)
        live = detail["synthesis"]["live"]
        self.assertEqual(live["since_version"], 1)
        self.assertTrue(live["auto"])
        self.assertEqual(set(live["behind"]), {"answers", "notes", "decisions", "cards"})
        self.assertEqual(live["behind"]["notes"], 0, "событий после версии ещё нет")
        self.assertEqual(live["state"], "fresh")
        self.assertIn("Актуально", live["text"])

        time.sleep(1.1)          # сообщение должно быть ПОЗЖЕ версии (время в секундах)
        self.sa.comment(root, "mikhail", "Добавьте в команду ещё и снабженца — закупки на нём.")
        detail = self.detail(root)
        live = detail["synthesis"]["live"]
        self.assertEqual(live["behind"]["notes"], 1, "сообщение Михаила — событие для свода")
        self.assertEqual(live["state"], "pending")
        self.assertIn("Отстаёт", live["text"])
        self.assertIsInstance(live["next_update_at"], int)
        message = [item for item in detail["thread"] if item["kind"] == "message"][-1]
        self.assertEqual(message["delivery"]["state"], "waiting")
        self.assertIn("Ждёт обновления свода", message["delivery"]["text"])

    def test_svod_refresh_creates_card_and_second_run_is_conflict(self):
        root = self.rid()
        status, payload, _ = self.actions(root, action="svod_refresh")
        self.assertEqual(status, 200, payload)
        card_id = payload["card_id"]
        self.assertTrue(card_id, "карточка обновления создана")
        rec = registry.get(self.cfg, ROOM, root)
        cards = [card for card in rec.get("extra_cards") or [] if card.get("svod")]
        self.assertEqual([card["id"] for card in cards], [card_id])
        self.assertTrue(cards[0]["auto"], "карточку поставило приложение")
        self.assertEqual(cards[0]["kind"], "revision")
        # пока обновление идёт, второй запуск не делаем
        status2, payload2, _ = self.actions(root, action="svod_refresh")
        self.assertEqual(status2, 409, payload2)
        self.assertEqual(payload2["error"]["detail"].get("reason"), "updating")
        detail = self.detail(root)
        self.assertEqual(detail["synthesis"]["live"]["state"], "updating")
        self.assertIn("Обновляется", detail["synthesis"]["live"]["text"])


# ------------------------------------------------ B-49: версия без metadata.svod ---
def _app_card_node(card_id, at, title="Обновление свода (авто)", status="done",
                   outcome="completed", summary="Версия 3: учли сезонный суп",
                   metadata=None, created_at=None):
    """Карточка обновления свода, которую поставило ПРИЛОЖЕНИЕ (заголовок + запуск)."""
    runs = [{"ended_at": at, "outcome": outcome}] if at else []
    return {"id": card_id, "role": "revision", "status": status, "title": title,
            "assignee": "lead-anna", "summary_md": summary, "metadata": metadata or {},
            "created_at": created_at, "finished_at": at, "runs": runs}


def _app_card_entry(card_id, posted_at, key="abc123def456", note="Автообновление: 1 сообщение",
                    manual=False, reason="auto"):
    """Запись реестра запроса: так приложение помнит, что карточку поставило оно."""
    return {"id": card_id, "kind": "revision", "round": 1, "at": posted_at, "posted_at": posted_at,
            "note": note, "reason": reason, "answers_count": 1, "changes": [],
            "auto": True, "svod": True, "manual": manual, "event_key": "svod-t_1000aaaa-" + key,
            "events": {"answers": 0, "notes": 1, "decisions": 0, "cards": 0}}


class AppCardVersionTest(unittest.TestCase):
    """B-49: карточку обновления поставило приложение — значит это версия свода.

    12.11.2026 руководитель-модель три раза не заполнил `metadata.svod`; карточки
    завершились успешно (`outcome=completed`), а приложение версий не увидело: события
    остались «неучтёнными», ключ идемпотентности блокировал новую карточку, три запуска
    агента ушли впустую. Теперь версия регистрируется по самой карточке.
    """

    def _base(self):
        nodes = [_node("t_1000aaaa", "root", at=V1_AT, summary="Версия 1"),
                 _node("t_2000bbbb", "revision", at=V2_AT, summary="Версия 2")]
        return nodes, [{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}]

    def test_card_without_registry_entry_and_without_svod_is_a_version(self):
        nodes, extra = self._base()
        nodes.append(_app_card_node("t_e0001111", NOW - 300))
        synth, _canon, _warn = _synthesis(nodes, extra)
        self.assertEqual(synth["version"], 3, "карточка приложения — новая версия")
        self.assertEqual(synth["source_task"], "t_e0001111")
        self.assertIn("Версия 3", synth["text_md"], "текст берётся из ответа карточки как раньше")
        entry = synth["history"][-1]
        self.assertEqual(entry["kind"], "revision")
        self.assertEqual(entry["reason"], "auto")
        self.assertEqual(entry["task_id"], "t_e0001111")
        self.assertEqual(entry["at"], NOW - 300, "время версии — время завершения карточки")
        self.assertTrue(entry.get("auto"))

    def test_registered_card_without_svod_keeps_note_and_changes(self):
        nodes, extra = self._base()
        nodes.append(_app_card_node("t_e0002222", NOW - 300, created_at=NOW - 900,
                                    metadata={"changes": ["сезонный суп", "сроки поставки"]}))
        extra = extra + [_app_card_entry("t_e0002222", NOW - 900, note="Автообновление: 1 сообщение")]
        synth, _canon, _warn = _synthesis(nodes, extra)
        entry = synth["history"][-1]
        self.assertEqual(synth["version"], 3)
        self.assertEqual(entry["note"], "Автообновление: 1 сообщение", "заметка — из карточки")
        self.assertEqual(entry["changes"], ["сезонный суп", "сроки поставки"],
                         "«что изменилось» — из metadata.changes")
        self.assertEqual(entry["posted_at"], NOW - 900,
                         "граница учтённых событий — когда карточка ушла")

    def test_failed_card_is_not_a_version(self):
        nodes, extra = self._base()
        nodes.append(_app_card_node("t_e0003333", NOW - 300, status="failed", outcome="failed"))
        extra = extra + [_app_card_entry("t_e0003333", NOW - 900)]
        synth, _canon, _warn = _synthesis(nodes, extra)
        self.assertEqual(synth["version"], 2, "упавшая карточка версией не становится")
        self.assertNotIn("t_e0003333", [h.get("task_id") for h in synth["history"]])

    def test_card_with_cancelled_run_is_not_a_version(self):
        nodes, extra = self._base()
        nodes.append(_app_card_node("t_e0004444", NOW - 300, status="done", outcome="cancelled"))
        extra = extra + [_app_card_entry("t_e0004444", NOW - 900)]
        synth, _canon, _warn = _synthesis(nodes, extra)
        self.assertEqual(synth["version"], 2, "отменённый запуск — не версия")

    def test_card_without_text_is_not_a_version(self):
        nodes, extra = self._base()
        nodes.append(_app_card_node("t_e0005555", NOW - 300, summary=""))
        extra = extra + [_app_card_entry("t_e0005555", NOW - 900)]
        synth, _canon, _warn = _synthesis(nodes, extra)
        self.assertEqual(synth["version"], 2, "версии без текста свода нет")

    def test_no_double_version_when_svod_metadata_is_present(self):
        """Обратный путь: карточка и в реестре, и с metadata.svod — версия ОДНА."""
        nodes, extra = self._base()
        nodes.append(_app_card_node("t_e0006666", NOW - 300,
                                    metadata={"svod": {"note": "по ответам",
                                                       "changes": ["сезонный суп"]}}))
        extra = extra + [_app_card_entry("t_e0006666", NOW - 900)]
        synth, _canon, _warn = _synthesis(nodes, extra)
        self.assertEqual([h["version"] for h in synth["history"]], [1, 2, 3])
        self.assertEqual(len(synth["history"]), 3, "двойной версии нет")

    def test_leader_card_with_svod_is_still_one_version(self):
        nodes, extra = self._base()
        nodes.append(_node("t_e0007777", "worker", at=NOW - 300,
                           metadata={"svod": {"note": "по ответам",
                                              "changes": ["сезонный суп"]}}))
        synth, _canon, _warn = _synthesis(nodes, extra)
        self.assertEqual([h["version"] for h in synth["history"]], [1, 2, 3])
        self.assertEqual(synth["history"][-1]["kind"], "leader")


class AppCardLiveTest(SvodBase):
    """B-49: `synthesis.live` после карточки обновления без `metadata.svod`."""

    def _case(self, posted_at, finished_at, note_at=None, summary="Версия 3: учли суп",
              status="done", outcome="completed", created_at=None):
        detail = make_detail()
        detail["thread"] = []
        if note_at:
            detail["thread"].append({"id": "c9", "at": note_at, "author": "mikhail",
                                     "author_kind": "mikhail", "kind": "message",
                                     "text": "И добавьте сезонный суп"})
        node = _app_card_node("t_e0009999", finished_at, status=status, outcome=outcome,
                              summary=summary, created_at=created_at or posted_at,
                              metadata={"changes": ["сезонный суп"]})
        detail["tree"].append(node)
        extra = [{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT},
                 _app_card_entry("t_e0009999", posted_at)]
        synth, _canon, _warn = _synthesis(detail["tree"], extra)
        detail["synthesis"] = synth
        return detail, make_rec(extra_cards=extra)

    def test_card_without_svod_resets_behind(self):
        # событие — ДО постановки карточки: руководитель его видел, оно учтено
        detail, rec = self._case(posted_at=NOW - 900, finished_at=NOW - 300, note_at=NOW - 1200)
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(detail["synthesis"]["version"], 3)
        self.assertEqual(live["behind"], {"answers": 0, "notes": 0, "decisions": 0, "cards": 0},
                         "события, вошедшие в карточку, учтены — behind обнулился")
        self.assertEqual(live["state"], "fresh")
        self.assertIn("версия 3", live["text"])

    def test_event_written_while_the_card_ran_stays_behind(self):
        # событие — ПОСЛЕ постановки карточки (и до её завершения): в карточку не попало
        detail, rec = self._case(posted_at=NOW - 900, finished_at=NOW - 300, note_at=NOW - 600)
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(detail["synthesis"]["version"], 3)
        self.assertEqual(live["behind"]["notes"], 1, "событие после постановки — остаётся behind")
        self.assertEqual(live["state"], "pending")

    def test_failed_card_keeps_behind_and_cools_down(self):
        detail, rec = self._case(posted_at=NOW - 900, finished_at=NOW - 300, note_at=NOW - 1200,
                                 status="failed", outcome="failed")
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(detail["synthesis"]["version"], 2, "версии нет")
        self.assertEqual(live["behind"]["notes"], 1)
        self.assertEqual(live["state"], "pending")
        self.assertIn("Обновление не удалось", live["text"], "остывание W21-S2 работает как раньше")

    def test_planned_time_in_the_past_is_not_promised(self):
        """B-49: «обновится в 11:47» через 18 часов после 11:47 — так было на боевом."""
        detail = make_detail()
        detail["tree"] = detail["tree"][:2]
        detail["synthesis"]["history"][-1]["at"] = NOW - 65_000
        detail["synthesis"]["at"] = NOW - 65_000
        detail["thread"] = [{"id": "c9", "at": NOW - 64_000, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message", "text": "Давнее уточнение"}]
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}])
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "pending")
        self.assertIsNone(live["next_update_at"], "прошедшее время не отдаём")
        self.assertIn("обновится в ближайшие минуты", live["text"])
        self.assertNotIn(svod_live._hhmm(NOW - 64_000 + QUIET), live["text"],
                         "прошлого времени в тексте нет")

    def test_stalled_text_also_says_soon(self):
        detail = make_detail()
        detail["tree"].append({"id": "t_e000aaaa", "role": "worker", "status": "running",
                               "title": "Проверка письма", "assignee": "worker-marina",
                               "started_at": NOW - svod_live.BUSY_MAX_S - 60,
                               "finished_at": None, "summary_md": "", "metadata": {}, "runs": []})
        detail["thread"] = [{"id": "c9", "at": NOW - 3000, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message", "text": "Свежее, но давно"}]
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}])
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "pending")
        self.assertIn(svod_live.BUSY_MAX_TEXT, live["text"])
        self.assertIn("ближайшие минуты", live["text"])
        self.assertIsNone(live["next_update_at"])

    def test_delivery_does_not_promise_the_past_either(self):
        detail = make_detail()
        detail["tree"] = detail["tree"][:2]
        detail["thread"] = [{"id": "c9", "at": NOW - 3000, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message", "text": "Давнее"}]
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}])
        svod_live.attach(detail, make_room(), rec, now=NOW)
        delivery = {i["id"]: i for i in detail["thread"]}["c9"]["delivery"]
        self.assertEqual(delivery["state"], "waiting")
        self.assertIn("ближайшие минуты", delivery["text"])


class LoopGuardTest(SvodBase):
    """B-49: не больше двух автоматических карточек на один набор событий.

    Если после выпущенных карточек события всё ещё «не учтены» (версия не зарегистрировалась —
    например, руководитель вернул пустой ответ), третьей автоматической попытки нет: это
    уже не «тишина», а сбой. Решение остаётся человеку — кнопка «Обновить сейчас».
    """

    def _case(self, cards=1, summary=""):
        detail = make_detail()
        detail["tree"] = detail["tree"][:2]
        detail["thread"] = [{"id": "c9", "at": V2_AT + 600, "author": "mikhail",
                             "author_kind": "mikhail", "kind": "message", "text": "Ещё суп"}]
        rec = make_rec(extra_cards=[{"id": "t_2000bbbb", "kind": "revision", "at": V2_AT}])
        key = svod_live.event_key(svod_live.events(detail, make_room(), rec))
        extra = list(rec["extra_cards"])
        for index in range(cards):
            card_id = "t_e100000%d" % index
            at = NOW - 1200 + index * 120
            detail["tree"].append(_app_card_node(card_id, at, summary=summary))
            extra.append(_app_card_entry(card_id, at - 300, key=key))
        rec = dict(rec, extra_cards=extra, svod_event_key=key)
        return detail, rec, key

    def test_two_attempts_limit(self):
        self.assertEqual(svod_live.MAX_AUTO_TRIES_PER_KEY, 2)
        detail, rec, key = self._case(cards=2)
        tries = svod_live.update_tries(detail, rec, key)
        self.assertEqual(tries["count"], 2)
        decision = svod_live.plan(detail, make_room(), rec, now=NOW)
        self.assertEqual((decision["action"], decision["reason"]), ("none", "loop_guard"))
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "stale")
        self.assertEqual(live["text"], svod_live.LOOP_STALE_TEXT)

    def test_one_useless_success_is_enough_to_stop(self):
        """Набор тот же, руководитель тот же — вторая такая же карточка тоже не даст версии."""
        detail, rec, key = self._case(cards=1)
        self.assertEqual(svod_live.update_tries(detail, rec, key)["count"], 1)
        self.assertEqual(svod_live.plan(detail, make_room(), rec, now=NOW)["reason"], "loop_guard")
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "stale")
        self.assertEqual(live["text"], svod_live.LOOP_STALE_TEXT)
        self.assertEqual(svod_live.tick(CFG, make_room(), rec, detail, now=NOW)["action"], "none")
        self.assertEqual(self.engine.created, [])

    def test_third_card_is_not_created(self):
        detail, rec, _key = self._case(cards=2)
        decision = svod_live.tick(CFG, make_room(), rec, detail, now=NOW)
        self.assertEqual(decision["action"], "none")
        self.assertEqual(self.engine.created, [], "третий запуск агента не тратится")

    def test_manual_button_still_works(self):
        detail, rec, _key = self._case(cards=2)
        res = svod_live.refresh_now(CFG, make_room(), rec, detail, now=NOW)
        self.assertTrue(res["card_id"], "«Обновить сейчас» работает всегда")
        self.assertEqual(len(self.engine.created), 1)

    def test_failed_cards_do_not_use_up_the_limit(self):
        """Неудачи — это «остывание», а не выпущенные версии: их считает failed_updates."""
        detail, rec, key = self._case(cards=0)
        for index in range(3):
            card_id = "t_e200000%d" % index
            at = NOW - 900 + index * 60
            detail["tree"].append(_app_card_node(card_id, at, status="failed", outcome="failed",
                                                 summary=""))
            rec["extra_cards"].append(_app_card_entry(card_id, at - 300, key=key))
        self.assertEqual(svod_live.update_tries(detail, rec, key)["count"], 0)


# --------------------------------------- B-49b: куда крепится карточка свода ---
class UpdateCardParentTest(SvodBase):
    """B-49b: карточка обновления — ребёнок КОРНЯ, а не предыдущей карточки свода.

    Прежняя сборка брала родителем `synthesis.source_task` (последнюю карточку обновления),
    поэтому цепочка «корень → … → обновление → обновление → …» росла в глубину, и карточки
    за `MAX_DEPTH` выпадали из дерева запроса — вместе с версиями свода. Порядок работы
    даёт не связь, а `open_update_cards` (новая карточка не ставится, пока идёт предыдущая).
    """

    def test_new_card_goes_to_the_root(self):
        detail, rec = make_detail(), make_rec()   # source_task = t_2000bbbb (прежняя карточка)
        ev = svod_live.events(detail, make_room(), rec, None, 0)
        svod_live.create_update_card(CFG, make_room(), rec, detail, ev, now=NOW)
        card = self.engine.created[0]
        self.assertEqual(card["parent"], "t_1000aaaa",
                         "родитель — корень запроса, а не последняя карточка свода")
        self.assertEqual(card["title"], "Обновление свода (авто)")
        self.assertTrue(card["idem_key"].startswith("svod-t_1000aaaa-"), card["idem_key"])

    def test_manual_card_goes_to_the_root(self):
        detail, rec = make_detail(), make_rec()
        ev = svod_live.events(detail, make_room(), rec, None, 0)
        svod_live.create_update_card(CFG, make_room(), rec, detail, ev, note="Учтите ответы",
                                     manual=True, now=NOW)
        card = self.engine.created[0]
        self.assertEqual(card["parent"], "t_1000aaaa", "«Доработать с учётом ответов» — тоже к корню")
        self.assertEqual(card["title"], "Обновление свода")
        self.assertIn("-m2-", card["idem_key"], "ключ ручной карточки не изменился")

    def test_the_key_still_holds_idempotency(self):
        detail, rec = make_detail(), make_rec()
        ev = svod_live.events(detail, make_room(), rec, None, 0)
        svod_live.create_update_card(CFG, make_room(), rec, detail, ev, now=NOW)
        svod_live.create_update_card(CFG, make_room(), rec, detail, ev, now=NOW + 5)
        keys = [c["idem_key"] for c in self.engine.created]
        self.assertEqual(keys[0], keys[1], "тот же набор событий — тот же ключ, второй карточки не будет")
        self.assertEqual([c["parent"] for c in self.engine.created], ["t_1000aaaa"] * 2)


class UpdateCardsSeenTest(SvodBase):
    """B-49b: лимит 6 карточек в час и защита от цикла видят ВСЕ карточки запроса.

    Само выпадение карточек из дерева проверяется на настоящей доске
    (`tests/test_engine_read.py`, `TreeDepthTest`). Здесь — обратная сторона: когда в дереве
    карточек обновления много (в том числе восемь), предохранители срабатывают, а не
    «не замечают» их.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = StateCfg(Path(self._tmp.name))

    def _detail_with_eight_cards(self):
        detail = make_detail()
        detail["thread"] = [{"id": "c7", "at": V2_AT + 100, "author": "mikhail", "author_kind": "mikhail",
                             "kind": "message", "text": "Добавьте сезонный суп"}]
        for index in range(8):
            at = V1_AT + 60 * (index + 1)          # история версий — до текущей (V2_AT)
            detail["tree"].append(_app_card_node("t_f%07d" % index, at, summary="Версия %d" % (index + 3)))
        return detail

    def test_hour_limit_holds_when_eight_cards_exist(self):
        now = V2_AT + 3600
        path = svod_live._start_path(self.cfg)
        svod_live._save_start(path, {"started_at": V1_AT,
                                     "launches": {"cafe-leto": [now - 60] * svod_live.MAX_CARDS_PER_HOUR}})
        decision = svod_live.tick(self.cfg, make_room(), make_rec(), self._detail_with_eight_cards(), now=now)
        self.assertEqual((decision["action"], decision["reason"]), ("wait", "hour_limit"))
        self.assertEqual(self.engine.created, [], "больше N карточек в час не ставим")

    def test_loop_guard_counts_a_card_among_many(self):
        detail = self._detail_with_eight_cards()
        rec = make_rec()
        last = detail["tree"][-1]["id"]
        rec["extra_cards"] = list(rec["extra_cards"]) + [_app_card_entry(last, V1_AT + 500)]
        key = svod_live.event_key(svod_live.events(detail, make_room(), rec, None, 0))
        self.assertTrue(key, "событие после версии не найдено — тест ничего не проверяет")
        rec["extra_cards"][-1]["event_key"] = "svod-t_1000aaaa-" + key
        self.assertEqual(svod_live.update_tries(detail, rec, key)["count"], 1)
        res = svod_live.plan(detail, make_room(), rec, now=NOW + 3600)
        self.assertEqual((res["action"], res["reason"]), ("none", "loop_guard"),
                         "повторная карточка на тот же набор событий не ставится")


# ------------------- B-50: результат карточки обновления событием не считается ---
DECISION_PACK = "Закупать упаковку у «Ромашки»"
DECISION_PRICE = "Считать стоимость от объёма 650 тонн"


class OwnCardResultTest(SvodBase):
    """B-50: результат карточки обновления свода не запускает следующую карточку.

    Карточка обновления приложения перечисляет решения (`metadata.decisions`); они
    получают время версии (`at`), и следующий тик считал их новым событием — приложение
    ставило карточку по собственному результату. Цикл запускал Sonnet раз в ~30 минут.
    Правило «собственные карточки автоматики событием не считаются» теперь действует и
    для их решений.
    """

    def _case(self, posted_at=NOW - 900, finished_at=NOW - 300, decisions=(DECISION_PACK,),
              title="Обновление свода (авто)", role="revision", extra_entry=True):
        detail = make_detail()
        detail["thread"] = []
        node = _app_card_node("t_e3009999", finished_at, title=title, created_at=posted_at,
                              metadata={"changes": ["учли сезонный суп"],
                                        "decisions": [{"text": text, "status": "proposed"}
                                                      for text in decisions]})
        node["role"] = role
        detail["tree"] = [_node("t_1000aaaa", "root", at=V1_AT, summary="Версия 1"), node]
        extra = [_app_card_entry("t_e3009999", posted_at)] if extra_entry else []
        synth, canon, _warn = _synthesis(detail["tree"], extra)
        detail["synthesis"] = synth
        # как в чтении: решения действующей версии, время — время версии
        detail["decisions"] = read_view.decisions(canon, "t_1000aaaa", synth.get("at"))
        return detail, make_rec(extra_cards=extra)

    def test_update_card_decisions_are_not_events(self):
        detail, rec = self._case()
        self.assertEqual(detail["synthesis"]["version"], 2)
        self.assertTrue(detail["decisions"], "в версии есть решение — иначе проверять нечего")
        self.assertEqual(detail["synthesis"]["history"][-1].get("source"), "app")
        ev = svod_live.events(detail, make_room(), rec)
        self.assertEqual(ev["behind"]["decisions"], 0, "решения карточки обновления — не событие")
        self.assertEqual(ev["total"], 0)
        live = svod_live.compute(detail, make_room(), rec, now=NOW)
        self.assertEqual(live["state"], "fresh")
        self.assertEqual(svod_live.plan(detail, make_room(), rec, now=NOW)["reason"], "fresh")

    def test_several_ticks_are_stable(self):
        detail, rec = self._case()
        for step in range(4):
            decision = svod_live.tick(CFG, make_room(), rec, detail, now=NOW + step * 700, since=0)
            self.assertEqual(decision["action"], "none", decision)
        self.assertEqual(self.engine.created, [], "ни одной новой карточки — цикла нет")

    def test_a_real_new_decision_is_still_an_event(self):
        """Обратный путь: решение, написанное ПОСЛЕ версии, свод по-прежнему поднимает."""
        detail, rec = self._case()
        detail["decisions"] = list(detail["decisions"]) + [
            {"id": "d_worker01", "text": DECISION_PRICE, "status": "proposed", "at": NOW - 60}]
        self.assertEqual(svod_live.events(detail, make_room(), rec)["behind"]["decisions"], 1)
        self.assertEqual(svod_live.plan(detail, make_room(), rec, now=NOW + 900)["action"], "create")

    def test_finished_update_card_is_not_a_card_event(self):
        """Запись реестра могла потеряться (B-49), а роль в дереве — быть не `revision`."""
        detail, rec = self._case(role="worker", extra_entry=False)
        self.assertEqual(detail["synthesis"]["version"], 2, "версия по карточке приложения есть")
        ev = svod_live.events(detail, make_room(), rec)
        self.assertEqual(ev["behind"]["cards"], 0, "карточка обновления — версия, а не событие")


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
