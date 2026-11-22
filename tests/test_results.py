"""Результаты комнаты (WAVE29 §P6): итоги запросов, «запросы без дела», этапы карты.

Всё на временном каталоге вне реального Hermes: модуль читает реестр комнаты и
необязательно доску, поэтому подставные ``states``/``shorts`` дают тест без БД.

    python -m unittest tests.test_results -v

Что проверяется (это же приёмка пакета):

* три завершённых запроса комнаты → «Результаты» показывают 3, у каждого «Коротко»;
* запросы без дела → 3 «запроса без дела», «Сделать делом» создаёт дело (и не
  создаёт дубль на повторное нажатие);
* «Собрать карту по выполненным запросам» → предложение этапов комнаты: оно ждёт
  решения человека и попадает пунктом в «От вас нужно»;
* пункт «Предлагаю собрать карту проекта» показывается один раз и откладывается.
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import phase_book                       # noqa: E402
from server import registry                        # noqa: E402
from server import results as results_mod          # noqa: E402

ROOM = "cafe"
R1, R2, R3, R4 = "t_aaa111", "t_bbb222", "t_ccc333", "t_ddd444"


def make_cfg(root: Path):
    state = Path(root) / "state"
    state.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(data_dir=Path(root), state_dir=state,
                                 hermes_home=Path(root) / "hermes", sandbox=True)


class ResultsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self._seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        """Три завершённых запроса и один в работе — как в живой комнате."""
        for root_id, title, created in ((R1, "Согласовать поставки для кофейни", 1762700000),
                                        (R2, "Посчитать смены на неделю", 1762710000),
                                        (R3, "Подготовить меню на осень", 1762720000),
                                        (R4, "Выбрать поставщика молока", 1762730000)):
            registry.add_request(self.cfg, ROOM, {"root_id": root_id,
                                                  "slug": "r-20261121-%s" % root_id[-4:],
                                                  "title": title, "created_at": created})
        self.states = {
            R1: {"status": "done", "finished_at": 1762705000},
            R2: {"status": "done", "finished_at": 1762715000},
            R3: {"status": "done", "finished_at": 1762725000},
            R4: {"status": "running", "finished_at": None},
        }
        self.shorts = {
            R1: "Договорились о двух поставках в неделю: вторник и пятница.",
            R2: "График собран: три смены закрыты, одна требует подмены.",
            R3: "Меню готово: пять сезонных позиций, смета посчитана.",
        }

    def payload(self, **kw):
        kw.setdefault("states", self.states)
        kw.setdefault("shorts", self.shorts)
        kw.setdefault("report_index", [])
        kw.setdefault("delos", [])
        return results_mod.list_results(self.cfg, ROOM, **kw)


class ResultsList(ResultsBase):
    def test_three_finished_requests_are_results(self):
        data = self.payload()
        self.assertEqual(data["counts"]["results"], 3)
        self.assertEqual(data["counts"]["requests"], 4)
        self.assertEqual(data["counts"]["pending"], 1)
        ids = [item["id"] for item in data["items"]]
        self.assertEqual(ids, [R3, R2, R1], "свежие сверху")
        first = data["items"][0]
        self.assertEqual(first["title"], "Подготовить меню на осень")
        self.assertTrue(first["short"].startswith("Меню готово"), first["short"])
        self.assertEqual(first["finished_at"], 1762725000)

    def test_pending_requests_are_listed_but_not_results(self):
        data = self.payload()
        self.assertEqual([item["id"] for item in data["pending"]], [R4])
        self.assertNotIn(R4, [item["id"] for item in data["items"]])

    def test_reports_are_grouped_by_request(self):
        reports = [{"key": "r-20261121-1200-abcd", "title": "Свод по поставкам",
                    "kind": "spec", "request_id": R1, "blocks": 4, "updated_at": 1762706000},
                   {"key": "r-20261121-1300-ef01", "title": "График смен",
                    "kind": "html", "request_id": R2, "blocks": 0, "updated_at": 1762716000}]
        data = self.payload(report_index=reports)
        by_id = {item["id"]: item for item in data["items"]}
        self.assertEqual([r["key"] for r in by_id[R1]["reports"]], ["r-20261121-1200-abcd"])
        self.assertEqual(by_id[R2]["reports"][0]["kind"], "html")
        self.assertEqual(by_id[R3]["reports"], [])
        self.assertEqual(data["counts"]["reports"], 2)

    def test_without_delo_lists_every_request_once(self):
        data = self.payload()
        self.assertEqual(data["counts"]["without_delo"], 4)
        delos = [{"id": "dl_abc", "title": "Смены", "request_id": R2}]
        data = self.payload(delos=delos)
        self.assertEqual([item["id"] for item in data["without_delo"]], [R4, R3, R1])
        self.assertEqual(self.payload(delos=delos)["items"][1]["delo"]["id"], "dl_abc")

    def test_empty_room_is_honest(self):
        empty = make_cfg(Path(self.tmp.name) / "empty")
        data = results_mod.list_results(empty, "laundry", states={}, shorts={},
                                        report_index=[], delos=[])
        self.assertEqual(data["counts"], {"requests": 0, "results": 0, "pending": 0,
                                          "reports": 0, "without_delo": 0})
        self.assertEqual(data["items"], [])

    def test_short_text_is_one_sentence_without_markup(self):
        text = "## Итог\n\n**Готово**: смены закрыты.\n\n- пункт один\n- пункт два\n" + "хвост " * 80
        short = results_mod.short_text(text)
        self.assertNotIn("#", short)
        self.assertNotIn("**", short)
        self.assertLessEqual(len(short), results_mod.SHORT_MAX + 1)
        self.assertTrue(short.startswith("Итог"))


class DeloFromRequest(ResultsBase):
    """«Сделать делом»: одно дело на запрос, повтор — не дубль."""

    def make_delo(self, room, request_id, title):
        from server import delo as delo_mod
        return delo_mod.create(self.cfg, room, {
            "request_id": request_id, "title": title, "status": "open",
            "stages": [{"title": "Запрос выполнен", "state": "done"},
                       {"title": "Итог и следующий шаг", "state": "now"}],
        })

    def test_created_once_and_repeat_is_conflict(self):
        from server import delo as delo_mod
        room = {"id": ROOM, "title": "Кофейня"}
        made = self.make_delo(room, R1, "Согласовать поставки")
        self.assertEqual(made["request_id"], R1)
        self.assertEqual(made["title"], "Согласовать поставки")
        with self.assertRaises(delo_mod.DeloError) as ctx:
            self.make_delo(room, R1, "Согласовать поставки")
        self.assertEqual(ctx.exception.http, 409)
        # и запрос больше не попадает в «без дела»
        data = self.payload(delos=[made])
        self.assertNotIn(R1, [item["id"] for item in data["without_delo"]])


class PhasesFromRequests(ResultsBase):
    def room(self):
        return {"id": ROOM, "title": "Кофейня", "orchestrator": "lead", "phases": []}

    def test_proposal_waits_for_human_and_lands_in_backlog(self):
        data = self.payload()
        room = self.room()
        got = results_mod.phases_from_requests(self.cfg, room, data["items"])
        self.assertTrue(got["ok"], got)
        items = [item["title"] for item in got["proposal"]["items"]]
        self.assertEqual(items[0], "Подготовить меню на осень")
        self.assertEqual(items[-1], "Итоги и следующий шаг")
        self.assertGreaterEqual(len(items), 3)
        self.assertEqual(got["proposal"]["state"], "proposed")
        # предложение — в комнате, а не в файле бэклога: его решает человек
        room["phases_proposal"] = got["proposal"]
        self.assertEqual(phase_book.proposal_of(room)["state"], "proposed")
        self.assertEqual(room.get("phases"), [], "этапы человека не меняются")

    def test_repeat_proposal_is_unchanged(self):
        data = self.payload()
        room = self.room()
        first = results_mod.phases_from_requests(self.cfg, room, data["items"])
        room["phases_proposal"] = first["proposal"]
        again = results_mod.phases_from_requests(self.cfg, room, data["items"])
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "unchanged")

    def test_no_results_means_nothing_to_build(self):
        empty = make_cfg(Path(self.tmp.name) / "empty2")
        got = results_mod.phases_from_requests(empty, {"id": "freight"}, [])
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"], "no_results")

    def test_request_phases_do_not_touch_human_phases(self):
        data = self.payload()
        room = self.room()
        room["phases"] = [{"id": "idea", "title": "Идея", "state": "done"}]
        got = results_mod.phases_from_requests(self.cfg, room, data["items"])
        # «предложение» не применяется само: этапы человека остаются как были
        self.assertTrue(got["ok"])
        self.assertEqual(room["phases"], [{"id": "idea", "title": "Идея", "state": "done"}])


class RoadmapOffer(ResultsBase):
    def test_offer_appears_when_map_not_built(self):
        item = results_mod.roadmap_offer(self.cfg, {"id": ROOM, "title": "Кофейня"},
                                         states=self.states)
        self.assertIsNotNone(item)
        self.assertEqual(item["kind"], "roadmap_offer")
        self.assertIn("карту проекта", item["text"])
        self.assertEqual([a["id"] for a in item["actions"]], ["build", "later"])

    def test_offer_hidden_when_map_exists_or_proposal_pending(self):
        self.assertIsNone(results_mod.roadmap_offer(
            self.cfg, {"id": ROOM, "phases": [{"id": "idea", "title": "Идея"}]},
            states=self.states))
        pending = {"state": "proposed", "items": [{"id": "a", "title": "А"}]}
        self.assertIsNone(results_mod.roadmap_offer(
            self.cfg, {"id": ROOM, "phases_proposal": pending}, states=self.states))

    def test_offer_shown_once_and_dismissed_for_two_weeks(self):
        room = {"id": ROOM}
        self.assertIsNotNone(results_mod.roadmap_offer(self.cfg, room, states=self.states))
        out = results_mod.dismiss_offer(self.cfg, ROOM, at=1762740000)
        self.assertTrue(out["ok"])
        self.assertIsNone(results_mod.roadmap_offer(self.cfg, room, states=self.states,
                                                    now=1762740001))
        # через две недели — снова можно предложить
        self.assertIsNotNone(results_mod.roadmap_offer(self.cfg, room, states=self.states,
                                                       now=1762740000 + results_mod.OFFER_COOLDOWN + 1))

    def test_offer_absent_without_finished_requests(self):
        self.assertIsNone(results_mod.roadmap_offer(
            self.cfg, {"id": ROOM}, states={R1: {"status": "running"}}))


if __name__ == "__main__":
    unittest.main()
