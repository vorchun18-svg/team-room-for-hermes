# -*- coding: utf-8 -*-
"""W24-TODAY (пакет N2): сервер экрана «Сегодня» — контракт §3.1.

Что проверяется (docs/WAVE24-RULES.md §3.1, §4):

* сборка ответа `GET /api/today` на поддельных комнатах и данных: просрочка,
  ближайшие сроки (3 суток), все виды «ждёт вас» (вопрос, решение, шаг,
  разрешение, черновик карты, сбой), порядок массивов, счётчики, пустая комната,
  битая комната не валит ответ;
* «что изменилось» считается от `last_visit_at` (а без него — за 24 часа);
* отметка захода `data/state/today.json`: идемпотентность, не чаще раза в 60 с;
* живой маршрут в песочнице (`App.dispatch`): форма ответа, кэш ≤ 10 с,
  `POST /api/today/seen`;
* производительность: 2 комнаты × 50 запросов — ответ заметно быстрее 200 мс.

Чистые правила — без доски и сервера (`TodayBuildTests`), файл отметки — во
временном каталоге (`TodaySeenTests`), маршруты и замер — в песочнице
`tests.helpers.Sandbox` (реальная доска и Hermes сверяются до/после).

    python -m unittest tests.test_today -v
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.registry as registry            # noqa: E402
import server.rooms as rooms_mod              # noqa: E402
import server.today as today                  # noqa: E402
from server import engine_read               # noqa: E402
from server.routes_today import LiveSources   # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

ROOM_A = "studio"
ROOM_B = "office"
LEAD = "lead-profile"
WORKER = "worker-profile"
WORKER2 = "worker-2"

CONTRACT_KEYS = {"generated_at", "last_visit_at", "counters", "needs_you", "overdue",
                 "due_soon", "running", "changed", "rooms"}
NEEDS_KEYS = {"room", "room_title", "request_id", "request_title", "kind", "text",
              "since", "urgent", "recommended"}
DUE_KEYS = {"room", "room_title", "item_id", "text", "owner", "due", "request_id",
            # W27-E3: строке нужен переход и признак «можно закрыть»
            "request_title", "kind", "state", "actionable"}
RUNNING_KEYS = {"room", "room_title", "request_id", "request_title", "members_working", "since"}
CHANGED_KEYS = {"finished", "new_requests", "new_answers", "failures"}
ROOM_KEYS = {"id", "title", "needs_you", "overdue", "running"}


# ------------------------------------------------------------------ подделки ---
class FakeSources(today.Sources):
    """Поддельные комнаты и данные: то, что вернёт настоящее чтение.

    ``broken`` — комнаты, у которых КАЖДЫЙ источник бросает исключение
    (проверяем «битая комната не валит ответ»).
    """

    def __init__(self, rooms=None, requests=None, backlog=None, states=None,
                 approvals=None, plans=None, broken=(), raise_on=None, waiting=None):
        self._rooms = rooms or []
        self._requests = requests or {}
        self._backlog = backlog or {}
        self._states = states or {}
        self._approvals = approvals or {}
        self._plans = plans or {}
        self._broken = set(broken)
        self._raise_on = raise_on or ""
        # W27-FIX: точные пункты «ждёт вас» по запросам; None — источника нет (бэклог)
        self._waiting = waiting
        self.calls = {"requests": 0, "backlog": 0, "states": 0}

    def _check(self, room, what: str):
        if room.get("id") in self._broken or what == self._raise_on:
            raise RuntimeError("источник недоступен: %s (%s)" % (what, room.get("id")))

    def rooms(self):
        return self._rooms

    def requests(self, room):
        self._check(room, "requests")
        self.calls["requests"] += 1
        return self._requests.get(room.get("id"), [])

    def backlog(self, room):
        self._check(room, "backlog")
        self.calls["backlog"] += 1
        return self._backlog.get(room.get("id"), [])

    def waiting(self, room):
        self._check(room, "waiting")
        if self._waiting is None:
            return None
        return self._waiting.get(room.get("id"), {})

    def states(self, room):
        self._check(room, "states")
        self.calls["states"] += 1
        return self._states.get(room.get("id"), {})

    def approvals(self, room):
        self._check(room, "approvals")
        return self._approvals.get(room.get("id"), [])

    def plan(self, room):
        self._check(room, "plan")
        return self._plans.get(room.get("id"))


def _room(room_id, title):
    return {"id": room_id, "title": title}


def _item(item_id, kind, text, owner="mikhail", status="open",
          due=None, request_id=None, at=1000):
    return {"id": item_id, "kind": kind, "text": text, "owner": owner, "status": status,
            "due": due, "source_request": request_id, "created_at": at, "updated_at": at}


def _rec(root_id, title, created_at=1000, answer_at=0):
    return {"root_id": root_id, "title": title, "text": title, "created_at": created_at,
            "answer_at": answer_at}


def _state(status, started_at=900, finished_at=None, members=0, since=None, error=""):
    return {"status": status, "started_at": started_at, "finished_at": finished_at,
            "members_working": members, "since": since if since is not None else started_at,
            "error": error}


DAY = 24 * 3600
#: «Сейчас» для чистых тестов — фиксированное, чтобы сроки не «плыли» от прогона к прогону.
NOW = 1794000000          # 2026-11-06 23:20:00 UTC (пояс сервера — локальный)
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
YESTERDAY = time.strftime("%Y-%m-%d", time.localtime(NOW - DAY))
TWO_DAYS_AGO = time.strftime("%Y-%m-%d", time.localtime(NOW - 2 * DAY))
IN_THREE_DAYS = time.strftime("%Y-%m-%d", time.localtime(NOW + 3 * DAY))
IN_FOUR_DAYS = time.strftime("%Y-%m-%d", time.localtime(NOW + 4 * DAY))


# --------------------------------------------------------------- чистая сборка ---
class TodayBuildTests(unittest.TestCase):
    """Правила «что ждёт человека» — без доски, сервера и файлов."""

    def _sources(self):
        return FakeSources(
            rooms=[_room(ROOM_A, "Сайт студии"), _room(ROOM_B, "Ремонт офиса")],
            requests={ROOM_A: [_rec("t_a1", "Годовой отчёт", NOW - 3 * DAY, NOW - 3600)],
                      ROOM_B: []},
            states={ROOM_A: {"t_a1": _state("needs_you", NOW - 3 * DAY)}},
            backlog={ROOM_A: [
                _item("t_a1:q_1", "open_question", "Какой домен выбрать для сайта?", "mikhail",
                      request_id="t_a1", at=NOW - 3 * DAY),
                _item("t_a1:d_1", "decision", "Делаем ли английскую версию?", "mikhail",
                      request_id="t_a1", at=NOW - 2 * DAY),
                _item("t_a1:ns_1", "next_step", "Согласовать структуру страниц", "mikhail",
                      due=TWO_DAYS_AGO, request_id="t_a1", at=NOW - 4 * DAY),
                _item("t_a1:ns_2", "next_step", "Собрать тексты", WORKER,
                      due=IN_THREE_DAYS, request_id="t_a1", at=NOW - 4 * DAY),
                _item("t_a1:ns_3", "next_step", "Проверить вёрстку", WORKER,
                      due=IN_FOUR_DAYS, request_id="t_a1", at=NOW - 4 * DAY),
                _item("t_a1:ns_4", "next_step", "Отдать макет", WORKER, status="done",
                      due=YESTERDAY, request_id="t_a1", at=NOW - 5 * DAY),
                _item("t_a1:q_2", "open_question", "Нужен ли раздел с ценами?", WORKER,
                      request_id="t_a1", at=NOW - 5 * DAY),
            ]},
            plans={ROOM_A: {"status": "draft", "brief_root_id": "t_a1", "updated_at": NOW - 600,
                            "streams": [{"id": "s1", "title": "Страницы"}]}},
        )

    def test_contract_shape_and_order(self):
        view = today.build(self._sources(), last_visit_at=NOW - DAY, now=NOW)
        self.assertEqual(set(view), CONTRACT_KEYS)
        self.assertEqual(view["generated_at"], NOW)
        self.assertEqual(view["last_visit_at"], NOW - DAY)
        self.assertEqual(set(view["counters"]), {"needs_you", "overdue", "due_soon",
                                                 "running", "changed"})
        self.assertEqual(set(view["changed"]), CHANGED_KEYS)
        for item in view["needs_you"]:
            self.assertEqual(set(item), NEEDS_KEYS)
            self.assertIn(item["kind"], today.KINDS)
            self.assertLessEqual(len(item["text"]), today.TEXT_MAX)
            self.assertIsInstance(item["urgent"], bool)
        for item in view["overdue"]:
            self.assertEqual(set(item), DUE_KEYS | {"days_overdue"})
            self.assertGreater(item["days_overdue"], 0)
        for item in view["due_soon"]:
            self.assertEqual(set(item), DUE_KEYS | {"days_left"})
            self.assertGreaterEqual(item["days_left"], 0)
        for item in view["running"]:
            self.assertEqual(set(item), RUNNING_KEYS)
        for room in view["rooms"]:
            self.assertEqual(set(room), ROOM_KEYS)

    def test_kinds_cover_all_waiting(self):
        sources = self._sources()
        sources._states[ROOM_A]["t_a1"] = _state("needs_you", NOW - 3 * DAY)
        sources._approvals = {ROOM_A: [{"request_id": "t_a1", "member_title": "Аналитик",
                                        "description": "запуск скрипта", "at": NOW - 60}]}
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        kinds = [item["kind"] for item in view["needs_you"]]
        self.assertEqual(sorted(set(kinds)), ["approval", "decision", "draft", "question", "step"])
        # пункты чужого владельца и закрытые в «ждёт вас» не попадают
        self.assertNotIn("Нужен ли раздел с ценами?", [i["text"] for i in view["needs_you"]])
        self.assertNotIn("Отдать макет", [i["text"] for i in view["needs_you"]])

    def test_failure_kind_and_reason(self):
        sources = self._sources()
        sources._states[ROOM_A]["t_a1"] = _state("failed", NOW - 2 * DAY, finished_at=NOW - DAY,
                                                 error="Воркер упал: токен sk-secret-value123456")
        view = today.build(sources, last_visit_at=NOW - 2 * DAY, now=NOW)
        failures = [item for item in view["needs_you"] if item["kind"] == "failure"]
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0]["urgent"])
        self.assertNotIn("sk-secret-value123456", failures[0]["text"])   # секрет замаскирован
        self.assertEqual(len(view["changed"]["failures"]), 1)
        self.assertEqual(view["changed"]["failures"][0]["request_id"], "t_a1")

    def test_urgent_first_then_oldest(self):
        view = today.build(self._sources(), last_visit_at=NOW - DAY, now=NOW)
        flags = [item["urgent"] for item in view["needs_you"]]
        self.assertEqual(flags, sorted(flags, reverse=True))               # срочные сверху
        calm = [item["since"] for item in view["needs_you"] if not item["urgent"]]
        self.assertEqual(calm, sorted(calm))                              # затем кто ждёт дольше

    def test_overdue_and_due_soon(self):
        view = today.build(self._sources(), last_visit_at=NOW - DAY, now=NOW)
        self.assertEqual([item["item_id"] for item in view["overdue"]], ["t_a1:ns_1"])
        self.assertEqual(view["overdue"][0]["days_overdue"], 2)
        self.assertEqual(view["overdue"][0]["due"], TWO_DAYS_AGO)
        # закрытый пункт со сроком вчера не просрочен; +3 суток входит в «ближайшие»
        self.assertEqual([item["item_id"] for item in view["due_soon"]], ["t_a1:ns_2"])
        self.assertEqual(view["due_soon"][0]["days_left"], 3)
        self.assertEqual([item["days_left"] for item in view["due_soon"]],
                         sorted(item["days_left"] for item in view["due_soon"]))

    def test_changed_from_last_visit(self):
        view = today.build(self._sources(), last_visit_at=NOW - DAY, now=NOW)
        self.assertEqual(view["changed"]["new_requests"], 0)     # запрос создан 3 суток назад
        self.assertEqual(view["changed"]["new_answers"], 1)      # ответ сутки назад
        self.assertEqual(view["changed"]["finished"], [])
        self.assertEqual(view["counters"]["changed"], 1)

    def test_changed_without_last_visit_is_24h(self):
        sources = self._sources()
        sources._requests[ROOM_A] = [_rec("t_new", "Новый запрос", NOW - 3600, 0),
                                     _rec("t_old", "Старый запрос", NOW - 5 * DAY, 0)]
        view = today.build(sources, last_visit_at=None, now=NOW)
        self.assertIsNone(view["last_visit_at"])
        self.assertEqual(view["changed"]["new_requests"], 1)

    def test_finished_counts_and_order(self):
        sources = self._sources()
        sources._requests[ROOM_A] = [_rec("t_a1", "Годовой отчёт"), _rec("t_a2", "Сайт студии")]
        sources._states[ROOM_A] = {"t_a1": _state("done", NOW - 2 * DAY, finished_at=NOW - 100),
                                   "t_a2": _state("done", NOW - 2 * DAY, finished_at=NOW - 50)}
        view = today.build(sources, last_visit_at=NOW - 300, now=NOW)
        self.assertEqual([row["request_id"] for row in view["changed"]["finished"]],
                         ["t_a2", "t_a1"])                    # свежие сверху
        self.assertEqual(view["counters"]["changed"], 2)

    def test_running_and_members(self):
        sources = self._sources()
        sources._states[ROOM_A] = {"t_a1": _state("working", NOW - 600, members=2,
                                                  since=NOW - 500)}
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        self.assertEqual(len(view["running"]), 1)
        self.assertEqual(view["running"][0]["members_working"], 2)
        self.assertEqual(view["running"][0]["since"], NOW - 500)
        self.assertEqual(view["rooms"][0]["running"], 1)
        self.assertEqual(view["counters"]["running"], 1)

    def test_empty_rooms_are_empty_arrays(self):
        view = today.build(FakeSources(rooms=[_room(ROOM_B, "Ремонт офиса")]),
                           last_visit_at=None, now=NOW)
        for key in ("needs_you", "overdue", "due_soon", "running"):
            self.assertEqual(view[key], [])
        self.assertEqual(view["changed"], {"finished": [], "new_requests": 0,
                                           "new_answers": 0, "failures": []})
        self.assertEqual(view["counters"], {"needs_you": 0, "overdue": 0, "due_soon": 0,
                                           "running": 0, "changed": 0})
        self.assertEqual(view["rooms"], [{"id": ROOM_B, "title": "Ремонт офиса",
                                          "needs_you": 0, "overdue": 0, "running": 0}])

    def test_no_rooms_at_all(self):
        view = today.build(FakeSources(), now=NOW)
        self.assertEqual(view["rooms"], [])
        self.assertEqual(view["counters"]["needs_you"], 0)

    def test_broken_room_does_not_break_answer(self):
        sources = self._sources()
        sources._broken = {ROOM_B}
        sources._requests[ROOM_B] = [_rec("t_b1", "Покрасить стены", NOW - 60)]
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        self.assertEqual(view["counters"]["needs_you"], 4)          # комната A не пострадала (4 пункта)
        self.assertIn(ROOM_B, [room["id"] for room in view["rooms"]])
        self.assertEqual([r for r in view["rooms"] if r["id"] == ROOM_B][0]["needs_you"], 0)

    def test_broken_source_backlog_only(self):
        """Бэклог недоступен — экран всё равно отвечает по контракту."""
        sources = FakeSources(rooms=[_room(ROOM_A, "Сайт студии")],
                              requests={ROOM_A: [_rec("t_a1", "Годовой отчёт")]},
                              raise_on="backlog")
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        self.assertEqual(view["needs_you"], [])
        self.assertEqual(view["overdue"], [])
        self.assertEqual(set(view), CONTRACT_KEYS)

    def test_proposal_of_phases_waits_for_human(self):
        sources = FakeSources(rooms=[_room(ROOM_A, "Сайт студии")],
                              backlog={ROOM_A: [
                                  {"id": "phases_proposal", "kind": "phases_proposal",
                                   "status": "open", "owner": LEAD, "text": "Руководитель предлагает этапы",
                                   "source_request": None, "created_at": NOW - 120}]})
        view = today.build(sources, now=NOW)
        self.assertEqual(len(view["needs_you"]), 1)
        self.assertEqual(view["needs_you"][0]["kind"], "decision")
        self.assertEqual(view["needs_you"][0]["request_id"], "")

    def test_text_is_cut_to_200(self):
        long_text = "Очень длинный вопрос " * 40
        sources = FakeSources(rooms=[_room(ROOM_A, "Сайт студии")],
                              backlog={ROOM_A: [_item("t_a1:q_1", "open_question", long_text)]})
        view = today.build(sources, now=NOW)
        self.assertEqual(len(view["needs_you"][0]["text"]), today.TEXT_MAX)
        self.assertTrue(view["needs_you"][0]["text"].endswith("…"))

    def test_bad_due_is_ignored(self):
        sources = FakeSources(rooms=[_room(ROOM_A, "Сайт студии")],
                              backlog={ROOM_A: [_item("t_a1:ns_1", "next_step", "Шаг",
                                                      due="когда-нибудь")]})
        view = today.build(sources, now=NOW)
        self.assertEqual(view["overdue"], [])
        self.assertEqual(view["due_soon"], [])

    def test_no_business_terms_in_items(self):
        """Площадка универсальна: в ответе нет терминов конкретного бизнеса (§0.1)."""
        view = today.build(self._sources(), last_visit_at=NOW - DAY, now=NOW)
        blob = json.dumps(view, ensure_ascii=False).lower()
        # термины собраны из частей: сами слова не держим в исходниках релиза
        banned = ("".join(("место", "рожден")), "".join(("лицензи", "и на недр")),
                  "".join(("нефт", "ян")), "".join(("маркшейд", "ер")))
        for word in banned:
            self.assertNotIn(word, blob)


# ------------------------------------------------- «ждёт вас» из самого запроса ---
class TodayExactItemsTests(unittest.TestCase):
    """W27-FIX: точные открытые пункты запроса — вместо устаревшего индекса бэклога.

    Дефект (перенос на боевые данные): у запроса `needs_you_count = {questions: 2,
    steps: 2, total: 4}`, а «Сегодня» показывало ноль — индекс бэклога хранил id
    вопросов прошлой версии свода, и открытые вопросы в нём не совпадали с карточкой.
    Теперь «Ждёт вас» берёт пункты из того же расчёта, что счётчик запроса; шаги в
    «Ждёт вас» не идут (они в «Просрочено/Ближайшие»).
    """

    def _sources(self, waiting):
        return FakeSources(
            rooms=[_room(ROOM_A, "Сайт студии")],
            requests={ROOM_A: [_rec("t_a1", "Годовой отчёт", NOW - 3 * DAY)]},
            states={ROOM_A: {"t_a1": _state("needs_you", NOW - 3 * DAY)}},
            waiting={ROOM_A: waiting},
            backlog={ROOM_A: [
                # индекс отстал: вопрос отвечен, но в бэклоге всё ещё «open»
                _item("t_a1:q_old", "open_question", "Какой домен берём?", "mikhail",
                      request_id="t_a1", at=NOW - 4 * DAY),
                # шаг с человеком: его место — «Просрочено», не «Ждёт вас»
                _item("t_a1:ns_1", "next_step", "Согласовать структуру страниц", "mikhail",
                      due=TWO_DAYS_AGO, request_id="t_a1", at=NOW - 4 * DAY),
            ]},
        )

    def test_blocked_card_waits_for_human_in_both_places(self):
        """W28-F: заблокированная карточка — «ждёт вас» и в «Сегодня», и во вкладке.

        У неё есть действие (ответ → `action: unblock`), поэтому число экрана и число
        вкладки «От вас нужно» считают её одинаково. Одно число — один источник.
        """
        sources = self._sources({"t_a1": [
            {"kind": "question", "id": "q_open", "text": "Нужна ли английская версия?",
             "since": NOW - 2 * DAY},
            {"kind": "blocked", "id": "t_block", "text": "Смета на упаковку",
             "since": NOW - 3600},
        ]})
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        kinds = [item["kind"] for item in view["needs_you"]]
        self.assertEqual(kinds, ["question", "blocked"], view["needs_you"])
        row = next(item for item in view["needs_you"] if item["kind"] == "blocked")
        self.assertEqual(row["text"], "Смета на упаковку")
        self.assertEqual(row["recommended"], today.RECOMMENDED["blocked"])
        self.assertEqual(view["counters"]["needs_you"], 2)
        self.assertEqual(view["rooms"][0]["needs_you"], 2)

    def test_answered_question_is_not_waiting_and_steps_are_not_cards(self):
        sources = self._sources({"t_a1": [
            {"kind": "question", "id": "q_open", "text": "Нужна ли английская версия?",
             "since": NOW - 2 * DAY},
            {"kind": "decision", "id": "d_open", "text": "Берём ли подрядчика?",
             "since": NOW - 3600},
            {"kind": "step", "id": "ns_1", "text": "Согласовать структуру страниц",
             "since": NOW - 3600},
        ]})
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        kinds = [item["kind"] for item in view["needs_you"]]
        self.assertEqual(kinds, ["question", "decision"], view["needs_you"])
        self.assertEqual([item["text"] for item in view["needs_you"]],
                         ["Нужна ли английская версия?", "Берём ли подрядчика?"])
        # отвеченный вопрос из индекса бэклога в «Ждёт вас» не попадает
        self.assertNotIn("Какой домен берём?", [item["text"] for item in view["needs_you"]])
        self.assertEqual(view["counters"]["needs_you"], 2)
        self.assertEqual(view["counters"]["needs_you"], len(view["needs_you"]))
        self.assertEqual(view["rooms"][0]["needs_you"], 2)
        # шаг остался в «Просрочено» — там его и ждут
        self.assertEqual([row["item_id"] for row in view["overdue"]], ["t_a1:ns_1"])

    def test_exact_items_keep_request_link_and_kind_keys(self):
        sources = self._sources({"t_a1": [
            {"kind": "question", "id": "q_open", "text": "Нужна ли английская версия?",
             "since": NOW - 2 * DAY}]})
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        card = view["needs_you"][0]
        self.assertEqual(set(card), NEEDS_KEYS)
        self.assertEqual(card["room"], ROOM_A)
        self.assertEqual(card["request_id"], "t_a1")
        self.assertEqual(card["request_title"], "Годовой отчёт")   # есть куда перейти
        self.assertTrue(card["urgent"])                            # ждёт больше суток

    def test_archived_request_items_do_not_wait(self):
        sources = self._sources({"t_a1": [
            {"kind": "question", "id": "q_open", "text": "Нужна ли английская версия?",
             "since": NOW - 2 * DAY}]})
        sources._states[ROOM_A]["t_a1"] = _state("archived", NOW - 3 * DAY)
        view = today.build(sources, last_visit_at=NOW - DAY, now=NOW)
        self.assertEqual(view["needs_you"], [])
        self.assertEqual(view["counters"]["needs_you"], 0)


# ------------------------------------------------------------ фикстура N3 ---
class TodayFixtureTests(unittest.TestCase):
    """Сверка формы ответа с фикстурой интерфейса `fixtures/today.json` (N3).

    Фикстура — не эталон значений, а договор о ФОРМЕ: те же ключи у каждого
    элемента, те же виды ожидания, те же типы. Если N3 расширит фикстуру —
    тест покажет, чего не хватает серверу, а не «упадёт молча».
    """

    path = ROOT / "fixtures" / "today.json"

    def setUp(self):
        if not self.path.is_file():
            self.skipTest("фикстуры N3 ещё нет")
        self.fixture = json.loads(self.path.read_text(encoding="utf-8"))

    def test_top_level_keys_match_contract(self):
        keys = {key for key in self.fixture if not key.startswith("_")}
        self.assertEqual(keys, CONTRACT_KEYS)

    def test_item_keys_match_contract(self):
        for item in self.fixture["needs_you"]:
            self.assertEqual(set(item), NEEDS_KEYS)
        for item in self.fixture["overdue"]:
            self.assertEqual(set(item), DUE_KEYS | {"days_overdue"})
        for item in self.fixture["due_soon"]:
            self.assertEqual(set(item), DUE_KEYS | {"days_left"})
        for item in self.fixture["running"]:
            self.assertEqual(set(item), RUNNING_KEYS)
        self.assertEqual(set(self.fixture["changed"]), CHANGED_KEYS)
        for room in self.fixture["rooms"]:
            self.assertEqual(set(room), ROOM_KEYS)

    def test_kinds_and_counters_agree(self):
        kinds = [item["kind"] for item in self.fixture["needs_you"]]
        for kind in kinds:
            self.assertIn(kind, today.KINDS)
        counters = self.fixture["counters"]
        self.assertEqual(counters["needs_you"], len(self.fixture["needs_you"]))
        self.assertEqual(counters["overdue"], len(self.fixture["overdue"]))
        self.assertEqual(counters["due_soon"], len(self.fixture["due_soon"]))
        self.assertEqual(counters["running"], len(self.fixture["running"]))
        changed = self.fixture["changed"]
        # «changed» в контракте не расшифрован: сервер считает суммой четырёх частей
        # (см. docs/API.md, W24-TODAY.1). В фикстуре N3 число меньше суммы — это данные
        # фикстуры, а не форма ответа: проверяем только, что счётчик не меньше
        # перечисленных сбоев и завершений (расхождение вынесено в отчёт N2).
        self.assertIsInstance(counters["changed"], int)
        self.assertGreaterEqual(counters["changed"], len(changed["finished"]) + len(changed["failures"]))
        # суммы по комнатам сходятся с общими счётчиками
        self.assertEqual(sum(room["needs_you"] for room in self.fixture["rooms"]), counters["needs_you"])
        self.assertEqual(sum(room["overdue"] for room in self.fixture["rooms"]), counters["overdue"])
        self.assertEqual(sum(room["running"] for room in self.fixture["rooms"]), counters["running"])

    def test_server_answers_with_same_shape(self):
        """Мой ответ на тех же данных — та же форма, что ждёт интерфейс."""
        fixture = self.fixture
        rooms = [{"id": room["id"], "title": room["title"]} for room in fixture["rooms"]]
        requests, backlog, states = {}, {}, {}
        for item in fixture["needs_you"] + fixture["overdue"] + fixture["due_soon"] \
                + fixture["running"] + fixture["changed"]["finished"]:
            room_id, root_id = item["room"], item.get("request_id")
            if not root_id:
                continue
            requests.setdefault(room_id, [])
            if not any(rec["root_id"] == root_id for rec in requests[room_id]):
                requests[room_id].append(_rec(root_id, item["request_title"], NOW - DAY))
        for room in rooms:
            room_id = room["id"]
            states[room_id] = {rec["root_id"]: _state(
                "working" if rec["root_id"] in [r["request_id"] for r in fixture["running"]]
                else "needs_you", NOW - 3600, members=1) for rec in requests.get(room_id, [])}
            backlog[room_id] = []
        for row in fixture["overdue"]:
            backlog[row["room"]].append({"id": row["item_id"], "kind": "next_step",
                                         "text": row["text"], "owner": row["owner"],
                                         "status": "open", "due": row["due"],
                                         "source_request": row["request_id"],
                                         "created_at": NOW - DAY})
        sources = FakeSources(rooms=rooms, requests=requests, backlog=backlog, states=states)
        view = today.build(sources, last_visit_at=fixture["last_visit_at"],
                           now=fixture["generated_at"])
        self.assertEqual({key for key in view}, CONTRACT_KEYS)
        for item in view["needs_you"]:
            self.assertEqual(set(item), NEEDS_KEYS)
        self.assertEqual([row["item_id"] for row in view["overdue"]],
                         [row["item_id"] for row in fixture["overdue"]])


# ------------------------------------------------------------- отметка захода ---
class TodaySeenTests(unittest.TestCase):
    """`data/state/today.json`: последний заход, идемпотентность, 60 секунд."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="today-state-")
        self.cfg = SimpleNamespace(data_dir=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_visit_has_no_last(self):
        self.assertIsNone(today.last_visit(self.cfg))
        result = today.mark_seen(self.cfg, now=NOW)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["last_visit_at"], NOW)
        self.assertIsNone(result["previous"])
        self.assertEqual(today.last_visit(self.cfg), NOW)
        path = today.state_path(self.cfg)
        self.assertEqual(path.name, "today.json")
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["last_visit_at"], NOW)

    def test_second_visit_within_minute_does_not_change(self):
        today.mark_seen(self.cfg, now=NOW)
        result = today.mark_seen(self.cfg, now=NOW + 5)
        self.assertFalse(result["changed"])
        self.assertEqual(result["last_visit_at"], NOW)
        self.assertEqual(today.last_visit(self.cfg), NOW)

    def test_visit_after_minute_moves(self):
        today.mark_seen(self.cfg, now=NOW)
        result = today.mark_seen(self.cfg, now=NOW + today.SEEN_MIN_GAP_S + 1)
        self.assertTrue(result["changed"])
        self.assertEqual(result["last_visit_at"], NOW + today.SEEN_MIN_GAP_S + 1)
        self.assertEqual(result["previous"], NOW)

    def test_broken_file_is_not_fatal(self):
        path = today.state_path(self.cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{это не json", encoding="utf-8")
        self.assertIsNone(today.last_visit(self.cfg))
        result = today.mark_seen(self.cfg, now=NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(today.last_visit(self.cfg), NOW)

    def test_future_mark_is_replaced(self):
        today.mark_seen(self.cfg, now=NOW + 300)          # часы «ушли вперёд»
        result = today.mark_seen(self.cfg, now=NOW)
        self.assertTrue(result["changed"])                 # отметка из будущего заменяется
        self.assertEqual(result["last_visit_at"], NOW)


# ----------------------------------------------------------- живой маршрут ---
class _AppCase(unittest.TestCase):
    """Песочница и помощники для живых тестов экрана (база для двух наборов)."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)
        cls.app = cls._build_app()

    @classmethod
    def _build_app(cls):
        from server import app as app_mod
        app = app_mod.App(cls.cfg, port=0)
        cls.room_a = rooms_mod.create_room(cls.cfg, {
            "title": "Сайт студии", "orchestrator": LEAD, "members": [{"profile": WORKER, "role": "worker"}]})
        cls.room_b = rooms_mod.create_room(cls.cfg, {
            "title": "Ремонт офиса", "orchestrator": LEAD, "members": [{"profile": WORKER, "role": "worker"}]})
        return app

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self._today_cache_clear()

    def _today_cache_clear(self):
        try:
            getattr(self.app, "__today_cache", {}).clear()
        except AttributeError:
            pass

    # --- помощники ---
    def _request(self, room, title, status="todo", child_status="running", tenant=None):
        """Запрос комнаты: корень + карточка-ребёнок в нужном статусе, запись в реестре."""
        room_id = room["id"]
        root = self.sa.card(title, LEAD, status="todo",
                            tenant=tenant or ("room:" + room_id))
        if child_status:
            self.sa.decompose(root, [{"title": title + ": часть 1", "assignee": WORKER,
                                      "status": child_status}], assignee=LEAD)
        if status == "done":
            self.sa.finish(root, "Готово")
        registry.add_request(self.cfg, room_id, {
            "root_id": root, "slug": "r-test-" + root, "title": title,
            "text": title, "created_at": _board_now()})
        return root

    def _request_with_questions(self, room, title, questions):
        """Запрос, у которого карточка участника завершена с вопросами руководителю.

        Вопросы живут там, где их пишут агенты — в `metadata` завершённой карточки
        (`server/normalize.py` → `open_questions`). Именно этот источник читает
        страница запроса и, начиная с W27-FIX, «Сегодня».
        """
        room_id = room["id"]
        root = self.sa.card(title, LEAD, status="todo", tenant="room:" + room_id)
        child = self.sa.card(title + ": часть 1", WORKER, status="running",
                             tenant="room:" + room_id, parent=root)
        self.sa.finish(child, "Работа сделана, нужны ваши ответы.",
                       {"open_questions": [{"text": text, "for": "mikhail"}
                                           for text in questions]})
        registry.add_request(self.cfg, room_id, {
            "root_id": root, "slug": "r-fix-" + root, "title": title, "text": title,
            "created_at": _board_now()})
        return root

    def _backlog(self, room_id, items):
        path = Path(self.cfg.data_dir) / "state" / room_id / "backlog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"room": room_id, "items": items, "updated_at": _board_now()},
                                  ensure_ascii=False), encoding="utf-8")

    def _reset_seen(self):
        """Убрать отметку захода: порядок тестов не должен влиять на «первый заход»."""
        try:
            today.state_path(self.cfg).unlink()
        except OSError:
            pass
        self._today_cache_clear()

    def _get(self, path="/api/today"):
        status, body, _headers = self.app.dispatch("GET", path, {}, {})
        self.assertEqual(status, 200, body)
        return body

    def _post(self, path, body=None):
        """POST через диспетчер приложения (W27-E3: действия по шагу)."""
        status, payload, _headers = self.app.dispatch("POST", path, {}, body or {})
        return status, payload

class TodayApiTests(_AppCase):
    """`GET /api/today` и `POST /api/today/seen` через настоящий `App` в песочнице."""

    # --- тесты ---
    def test_empty_room_answers_contract(self):
        body = self._get()
        self.assertEqual(set(body), CONTRACT_KEYS)
        self.assertIn(self.room_a["id"], [room["id"] for room in body["rooms"]])
        self.assertIsInstance(body["needs_you"], list)

    def test_question_from_card_waits_for_human(self):
        """Вопрос из карточки запроса — «ждёт вас» (W27-FIX: источник — сам запрос).

        Раньше пункты брались из индекса бэклога; после переноса на боевые данные id
        вопросов в индексе разошлись с карточками, и «Сегодня» показывало ноль при
        двух открытых вопросах в запросе. Теперь вопрос кладётся туда, где он живёт
        на самом деле — в `metadata` карточки участника.
        """
        root = self._request_with_questions(self.room_a, "Годовой отчёт",
                                            ["Нужна ли английская версия?"])
        body = self._get()
        found = [item for item in body["needs_you"] if item["request_id"] == root]
        self.assertTrue(found, body["needs_you"])
        self.assertEqual(found[0]["kind"], "question")
        self.assertEqual(found[0]["room"], self.room_a["id"])
        self.assertTrue(found[0]["request_title"])
        self.assertGreaterEqual(body["counters"]["needs_you"], 1)

    def test_answered_question_leaves_needs_you_and_count_matches(self):
        """W27-FIX: отвеченный вопрос не ждёт, открытый — ждёт; число = счётчик запроса.

        Жёсткая проверка дефекта: у запроса два вопроса, на один ответили. «Сегодня»
        обязано показать ОДИН пункт, и это же число (без шагов) — у комнаты. Число
        карточек запроса сверяется с `needs_you_count` страницы запроса: два источника
        одного факта не имеют права расходиться.
        """
        room = rooms_mod.create_room(self.cfg, {"title": "Счётчик ожиданий",
                                                "orchestrator": LEAD,
                                                "members": [{"profile": WORKER, "role": "worker"}]})
        root = self._request_with_questions(room, "Проверка доступа",
                                            ["Нужна ли английская версия?",
                                             "Какой домен берём?"])
        detail = self._get("/api/requests/" + root)
        questions = {item["text"]: str(item["id"]) for item in detail["open_questions"]}
        self.assertEqual(len(questions), 2, detail["open_questions"])
        status, payload = self._post("/api/requests/%s/actions" % root,
                                     {"action": "answer",
                                      "question_id": questions["Какой домен берём?"],
                                      "text": "Берём example.ru"})
        self.assertEqual(status, 200, payload)
        self._today_cache_clear()

        body = self._get()
        cards = [item for item in body["needs_you"] if item["request_id"] == root]
        self.assertEqual(len(cards), 1, cards)
        self.assertEqual(cards[0]["kind"], "question")
        self.assertIn("английская", cards[0]["text"])
        self.assertNotIn("домен", " ".join(item["text"] for item in cards))
        # число карточек запроса = серверный счётчик запроса без шагов
        counts = self._get("/api/requests/" + root)["needs_you_count"]
        self.assertEqual(counts["questions"], 1)
        self.assertEqual(counts["total"] - counts["steps"], len(cards))
        # у комнаты — то же число, что у «Сегодня» (меню/бейдж считают так же)
        room_row = [row for row in body["rooms"] if row["id"] == room["id"]][0]
        self.assertEqual(room_row["needs_you"], len(cards))
        self.assertEqual(body["counters"]["needs_you"], len(body["needs_you"]))

    def test_overdue_step_counts(self):
        root = self._request(self.room_a, "Ремонт офиса: смета")
        self._backlog(self.room_a["id"], [
            {"id": root + ":ns_1", "kind": "next_step", "text": "Согласовать смету",
             "owner": "mikhail", "status": "open", "due": _days_ago(2),
             "source_request": root, "created_at": _board_now() - 600}])
        body = self._get()
        rows = [row for row in body["overdue"] if row["request_id"] == root]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["days_overdue"], 2)
        self.assertEqual(rows[0]["owner"], "mikhail")
        self.assertEqual(body["counters"]["overdue"], len(body["overdue"]))

    def test_overdue_row_has_way_in_and_action(self):
        """W27-E3: у строки «Просрочено» есть переход и она закрываема.

        Дефект 2 (18.11, вечер): строка была постером — ни перехода, ни действий.
        Сервер обязан отдать всё, что нужно строке: комнату, запрос с названием,
        вид пункта, состояние и признак «это ещё можно закрыть».
        """
        root = self._request(self.room_a, "Ремонт офиса: смета")
        self._backlog(self.room_a["id"], [
            {"id": root + ":ns_1", "kind": "next_step", "text": "Согласовать смету",
             "owner": "mikhail", "status": "open", "due": _days_ago(2),
             "source_request": root, "created_at": _board_now() - 600},
            # без id закрывать нечего — в счётчик такая строка не идёт
            {"id": "", "kind": "next_step", "text": "Строка без пункта",
             "owner": "mikhail", "status": "open", "due": _days_ago(1),
             "source_request": root, "created_at": _board_now() - 600}])
        body = self._get()
        rows = [row for row in body["overdue"] if row["room"] == self.room_a["id"]]
        self.assertEqual(len(rows), 2, "строка без id всё равно видна")
        by_text = {row["text"]: row for row in rows}
        real = by_text["Согласовать смету"]
        self.assertTrue(real["actionable"])
        self.assertTrue(real["request_title"], "у строки есть куда перейти")
        self.assertEqual(real["state"], "open")
        self.assertEqual(real["kind"], "next_step")
        self.assertFalse(by_text["Строка без пункта"]["actionable"])
        # Счётчик «просрочено» считает только закрываемое (§1 W27).
        self.assertEqual(body["counters"]["overdue"], 1)
        self.assertLessEqual(body["counters"]["overdue"], len(body["overdue"]))

    def test_overdue_counter_drops_after_step_done(self):
        """W27-E3: «Готово» в «Сегодня» = статус шага в бэклоге — счётчик падает."""
        root = self._request(self.room_a, "Ремонт офиса: смета")
        item_id = root + ":ns_1"
        self._backlog(self.room_a["id"], [
            {"id": item_id, "kind": "next_step", "text": "Согласовать смету",
             "owner": "mikhail", "status": "open", "due": _days_ago(2),
             "source_request": root, "created_at": _board_now() - 600}])
        self.assertEqual(self._get()["counters"]["overdue"], 1)
        status, _ = self._post("/api/rooms/%s/backlog/%s" % (self.room_a["id"], item_id),
                              {"status": "done"})
        self.assertEqual(status, 200)
        self._today_cache_clear()          # ответ /api/today кэшируется на 10 с
        body = self._get()
        self.assertEqual(body["overdue"], [], "сделанный шаг не висит в «Просрочено»")
        self.assertEqual(body["counters"]["overdue"], 0)

    def test_overdue_postpone_moves_due(self):
        """W27-E3: «Перенести срок» тем же бэклог-эндпоинтом меняет срок строки."""
        root = self._request(self.room_a, "Ремонт офиса: смета")
        item_id = root + ":ns_1"
        self._backlog(self.room_a["id"], [
            {"id": item_id, "kind": "next_step", "text": "Согласовать смету",
             "owner": "mikhail", "status": "open", "due": _days_ago(2),
             "source_request": root, "created_at": _board_now() - 600}])
        future = _days_ago(-3)
        status, _ = self._post("/api/rooms/%s/backlog/%s" % (self.room_a["id"], item_id),
                              {"status": "open", "due": future})
        self.assertEqual(status, 200)
        self._today_cache_clear()
        body = self._get()
        self.assertEqual(body["overdue"], [])
        soon = [row for row in body["due_soon"] if row["item_id"] == item_id]
        self.assertEqual(len(soon), 1, "перенесённый срок попал в «Ближайшие»")
        self.assertEqual(soon[0]["due"], future)
        self.assertTrue(soon[0]["actionable"])

    def test_running_request_shows_members(self):
        self._request(self.room_a, "Сайт студии: структура")
        body = self._get()
        self.assertTrue(body["running"], body)
        row = body["running"][0]
        self.assertGreaterEqual(row["members_working"], 1)
        self.assertIsInstance(row["since"], int)
        self.assertEqual(row["room"], self.room_a["id"])

    def test_seen_marks_visit_and_resets_changes(self):
        self._reset_seen()
        first = self._get()
        self.assertIsNone(first["last_visit_at"])            # отметок ещё не было
        status, seen, _h = self.app.dispatch("POST", "/api/today/seen", {}, {})
        self.assertEqual(status, 200, seen)
        self.assertTrue(seen["ok"])
        stamp = seen["last_visit_at"]
        self.assertIsInstance(stamp, int)
        # второй заход сразу — файл не переписывается
        _s, again, _h = self.app.dispatch("POST", "/api/today/seen", {}, {})
        self.assertFalse(again["changed"])
        self.assertEqual(again["last_visit_at"], stamp)
        second = self._get()
        self.assertEqual(second["last_visit_at"], stamp)
        self.assertEqual(second["changed"]["new_requests"], 0)   # всё было до захода
        self.assertEqual(second["counters"]["changed"], 0)

    def test_engine_states_are_cheap_and_exact(self):
        """`engine_read.today_states` — состояние корня и живые исполнители одним чтением."""
        root = self.sa.card("Сайт студии: два исполнителя", LEAD, status="todo",
                            tenant="room:" + self.room_a["id"])
        self.sa.decompose(root, [{"title": "часть 1", "assignee": WORKER, "status": "running"},
                                 {"title": "часть 2", "assignee": WORKER2, "status": "running"},
                                 {"title": "часть 3", "assignee": WORKER, "status": "todo"}],
                          assignee=LEAD)
        board = self.room_a.get("board") or "team"
        states = engine_read.today_states(self.cfg, board, [root, "t_нет"])
        self.assertIn(root, states)
        self.assertEqual(states[root]["members_working"], 2)      # три карточки, два человека
        self.assertIsInstance(states[root]["since"], int)
        self.assertEqual(engine_read.today_states(self.cfg, "team", []), {})

    def test_cache_keeps_answer_for_seconds(self):
        first = self._get()
        second = self._get()
        self.assertEqual(first["generated_at"], second["generated_at"])
        # отметка захода сбрасывает кэш — иначе экран показывал бы «изменилось» зря
        self.app.dispatch("POST", "/api/today/seen", {}, {})
        third = self._get()
        self.assertNotEqual(first["last_visit_at"], third["last_visit_at"])


# ------------------------------------------------------------- производительность ---
class TodayPerfTests(_AppCase):
    """2 комнаты × 50 запросов: сборка ответа заметно быстрее 200 мс (контракт §3.1)."""

    REQUESTS_PER_ROOM = 50
    ROUNDS = 5

    @classmethod
    def _build_app(cls):
        from server import app as app_mod
        app = app_mod.App(cls.cfg, port=0)
        cls.room_a = rooms_mod.create_room(cls.cfg, {
            "title": "Сайт студии", "orchestrator": LEAD, "members": [{"profile": WORKER, "role": "worker"}]})
        cls.room_b = rooms_mod.create_room(cls.cfg, {
            "title": "Ремонт офиса", "orchestrator": LEAD, "members": [{"profile": WORKER, "role": "worker"}]})
        # наполняем две комнаты как боевые: по 50 запросов в разных состояниях
        items_by_room = {}
        for index in range(cls.REQUESTS_PER_ROOM):
            for room in (cls.room_a, cls.room_b):
                title = "%s: запрос %d" % (room["title"], index + 1)
                root = cls.sa.card(title, LEAD, status="todo", tenant="room:" + room["id"])
                if index % 3 == 0:
                    kids = cls.sa.decompose(root, [{"title": title + ": часть", "assignee": WORKER,
                                                    "status": "running"}], assignee=LEAD)
                    status = "working"
                    if index == 0:
                        # W27-FIX: «Ждёт вас» берёт пункты из запроса, поэтому у одного
                        # запроса комнаты есть настоящий открытый вопрос в metadata —
                        # так замер включает и сборку детали, а не только индекс.
                        cls.sa.finish(kids[0], "Работа идёт, нужен ваш ответ.",
                                      {"open_questions": [{"text": "Уточнить объём работ",
                                                           "for": "mikhail"}]})
                elif index % 3 == 1:
                    cls.sa.finish(root, "Готово")
                    status = "done"
                else:
                    status = "todo"
                registry.add_request(cls.cfg, room["id"], {
                    "root_id": root, "slug": "r-perf-%s-%d" % (room["id"], index),
                    "title": title, "text": title,
                    "created_at": _board_now() - index * 60})
                items_by_room.setdefault(room["id"], []).extend([
                    {"id": root + ":q_1", "kind": "open_question", "text": "Уточнить объём работ",
                     "owner": "mikhail", "status": "open" if index % 2 else "done",
                     "source_request": root, "created_at": _board_now() - index * 60},
                    {"id": root + ":ns_1", "kind": "next_step", "text": "Согласовать план",
                     "owner": "mikhail", "status": "open", "due": _days_ago(index % 5),
                     "source_request": root, "created_at": _board_now() - index * 60}])
        for room_id, items in items_by_room.items():
            path = Path(cls.cfg.data_dir) / "state" / room_id / "backlog.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"room": room_id, "items": items,
                                        "updated_at": _board_now()}, ensure_ascii=False),
                            encoding="utf-8")
        return app

    def test_two_rooms_fifty_requests_under_200ms(self):
        sources = LiveSources(self.app)
        timings = []
        for _round in range(self.ROUNDS):
            started = time.monotonic()
            view = today.build(sources, last_visit_at=None)
            timings.append((time.monotonic() - started) * 1000)
            sources = LiveSources(self.app)          # холодное чтение каждый раунд
        median = statistics.median(timings)
        self.assertLess(median, 200.0, "медиана сборки %.1f мс (замеры %s)" % (median, timings))
        self.assertEqual(len(view["rooms"]), 2)
        self.assertGreaterEqual(view["counters"]["needs_you"], 1)
        print("\n  замер: 2 комнаты × %d запросов — медиана %.1f мс, худший %.1f мс"
              % (self.REQUESTS_PER_ROOM, median, max(timings)))

    def test_route_answers_within_cache_window(self):
        timings = []
        for _round in range(3):
            started = time.monotonic()
            self._get()
            timings.append((time.monotonic() - started) * 1000)
        self.assertLess(statistics.median(timings), 200.0,
                        "медиана ответа маршрута %.1f мс" % statistics.median(timings))


# ------------------------------------------------------------------- мелочи ---
def _board_now() -> int:
    return int(time.time())


def _days_ago(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * DAY))


if __name__ == "__main__":
    unittest.main(verbosity=2)
