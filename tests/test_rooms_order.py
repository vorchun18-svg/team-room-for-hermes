# -*- coding: utf-8 -*-
"""Порядок комнат в меню: перетаскивание, закрепление, группы (W29-P7).

Проверяется то, что человек увидит после перезапуска: место комнаты, 📌 и
группы живут в реестре комнат (`data/rooms.json`), а не в памяти окна и не в
профилях Hermes. Ошибки раскладки называют причину и ничего не пишут.

    python -m unittest tests.test_rooms_order -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, rooms  # noqa: E402


class LayoutBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-order-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        for room_id, title in (("cafe", "Кофейня"), ("laundry", "Химчистка"),
                               ("freight", "Грузоперевозки")):
            rooms.create_room(self.cfg, {
                "id": room_id, "title": title, "orchestrator": "lead",
                "members": [{"profile": "worker-1"}],
            })

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def ids(self):
        return [r["id"] for r in rooms.list_rooms(self.cfg)]

    def doc(self) -> dict:
        return json.loads((self.tmp / "data" / "rooms.json").read_text(encoding="utf-8"))

    def room(self, room_id: str) -> dict:
        return next(r for r in self.doc()["rooms"] if r["id"] == room_id)


class TestOrder(LayoutBase):
    def test_new_rooms_go_to_the_end_of_the_menu(self):
        self.assertEqual(self.ids(), ["cafe", "laundry", "freight"])
        self.assertEqual([self.room(i)["order"] for i in ("cafe", "laundry", "freight")], [1, 2, 3])

    def test_order_survives_restart(self):
        result = rooms.apply_layout(self.cfg, {"order": ["freight", "cafe", "laundry"]})
        self.assertTrue(result["changed"])
        self.assertEqual([r["id"] for r in result["rooms"]], ["freight", "cafe", "laundry"])
        # «Перезапуск»: новый конфиг читает тот же файл, порядок прежний.
        fresh = config.load()
        self.assertEqual([r["id"] for r in rooms.list_rooms(fresh)],
                         ["freight", "cafe", "laundry"])
        self.assertEqual([self.room(i)["order"] for i in ("freight", "cafe", "laundry")], [1, 2, 3])

    def test_partial_order_keeps_the_rest_in_place(self):
        rooms.apply_layout(self.cfg, {"order": ["laundry"]})
        self.assertEqual(self.ids(), ["laundry", "cafe", "freight"])
        # Прислали одного — остальные не потерялись и не перемешались.
        self.assertEqual([self.room(i)["order"] for i in ("laundry", "cafe", "freight")], [1, 2, 3])

    def test_pinned_rooms_stand_above_the_order(self):
        rooms.apply_layout(self.cfg, {"rooms": {"freight": {"pinned": True}}})
        self.assertEqual(self.ids(), ["freight", "cafe", "laundry"])
        rooms.apply_layout(self.cfg, {"rooms": {"freight": {"pinned": False}}})
        self.assertEqual(self.ids(), ["cafe", "laundry", "freight"],
                         "сняли 📌 — комната вернулась на своё место")

    def test_new_room_after_layout_still_goes_last(self):
        rooms.apply_layout(self.cfg, {"order": ["freight", "laundry", "cafe"]})
        rooms.create_room(self.cfg, {"id": "flowers", "title": "Доставка цветов",
                                     "orchestrator": "lead",
                                     "members": [{"profile": "worker-2"}]})
        self.assertEqual(self.ids(), ["freight", "laundry", "cafe", "flowers"])

    def test_order_error_names_the_room_and_writes_nothing(self):
        before = (self.tmp / "data" / "rooms.json").read_text(encoding="utf-8")
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.apply_layout(self.cfg, {"order": ["cafe", "nope"]})
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertIn("nope", ctx.exception.message)
        with self.assertRaises(rooms.RoomsError):
            rooms.apply_layout(self.cfg, {"order": ["cafe", "cafe"]})
        self.assertEqual((self.tmp / "data" / "rooms.json").read_text(encoding="utf-8"), before,
                         "ошибка не должна менять файл комнат")

    def test_pin_accepts_only_true_false(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.apply_layout(self.cfg, {"rooms": {"cafe": {"pinned": "true"}}})
        self.assertIn("pinned", ctx.exception.message + json.dumps(ctx.exception.detail))

    def test_unknown_room_field_in_layout_is_rejected(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.apply_layout(self.cfg, {"rooms": {"cafe": {"title": "Другое"}}})
        self.assertIn("закрепление", ctx.exception.message)


class TestGroups(LayoutBase):
    def test_group_create_rename_collapse_survives_restart(self):
        rooms.apply_layout(self.cfg, {"groups": [{"id": "work", "title": "Работа"}]})
        rooms.apply_layout(self.cfg, {"groups": [{"id": "work", "title": "Рабочие комнаты",
                                                 "collapsed": True}]})
        fresh = config.load()
        groups = rooms.list_groups(fresh)
        self.assertEqual(groups, [{"id": "work", "title": "Рабочие комнаты", "collapsed": True}])

    def test_room_moves_into_group_and_out_of_it(self):
        rooms.apply_layout(self.cfg, {"groups": [{"id": "work", "title": "Работа"}]})
        rooms.apply_layout(self.cfg, {"rooms": {"cafe": {"group": "work"}}})
        self.assertEqual(self.room("cafe")["group"], "work")
        rooms.apply_layout(self.cfg, {"rooms": {"cafe": {"group": ""}}})
        self.assertEqual(self.room("cafe")["group"], "")

    def test_unknown_group_is_rejected_with_reason(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.apply_layout(self.cfg, {"rooms": {"cafe": {"group": "nope"}}})
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertIn("nope", ctx.exception.message)

    def test_deleting_group_keeps_rooms_in_the_menu(self):
        rooms.apply_layout(self.cfg, {"groups": [{"id": "work", "title": "Работа"}]})
        rooms.apply_layout(self.cfg, {"rooms": {"cafe": {"group": "work"}}})
        rooms.apply_layout(self.cfg, {"groups": []})
        self.assertEqual(self.room("cafe")["group"], "", "комната не пропала — просто без группы")
        self.assertEqual(self.ids(), ["cafe", "laundry", "freight"])

    def test_bad_group_id_and_empty_title_are_rejected(self):
        for bad in ({"id": "Плохой", "title": "Нельзя"}, {"id": "work", "title": "  "},
                    {"id": "work", "title": "x" * 61}):
            with self.assertRaises(rooms.RoomsError):
                rooms.apply_layout(self.cfg, {"groups": [bad]})
        self.assertEqual(rooms.list_groups(self.cfg), [], "неверная группа не записалась")

    def test_duplicate_group_id_is_rejected(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.apply_layout(self.cfg, {"groups": [{"id": "work", "title": "Работа"},
                                                     {"id": "work", "title": "Ещё"}]})
        self.assertIn("уже есть", ctx.exception.message)

    def test_room_fields_come_back_from_list_rooms(self):
        rooms.apply_layout(self.cfg, {"groups": [{"id": "work", "title": "Работа"}],
                                      "rooms": {"cafe": {"pinned": True, "group": "work"}}})
        cafe = next(r for r in rooms.list_rooms(self.cfg) if r["id"] == "cafe")
        self.assertEqual((cafe["pinned"], cafe["group"], cafe["order"]), (True, "work", 1))


class TestContract(unittest.TestCase):
    """Клиент и сервер говорят об одном маршруте и одних полях."""

    def test_route_is_registered_and_handler_exists(self):
        import server.app as app_mod
        pairs = [(method, pattern.pattern) for method, pattern, _ in app_mod.ROUTES]
        matches = [p for m, p in pairs if m == "PUT" and p == r"/api/rooms/order"]
        self.assertEqual(len(matches), 1, "маршрут объявлен ровно один раз")
        self.assertTrue(hasattr(app_mod.App, "h_rooms_order_put"), "обработчик есть")

    def test_client_uses_the_same_endpoint(self):
        client = (ROOT / "web" / "js" / "rooms-order.js").read_text(encoding="utf-8")
        self.assertIn("export const ORDER_ENDPOINT = '/api/rooms/order';", client)
        sidebar = (ROOT / "web" / "js" / "shell-sidebar.js").read_text(encoding="utf-8")
        self.assertIn("api.put(ORDER_ENDPOINT", sidebar)

    def test_mock_understands_the_same_body(self):
        core = (ROOT / "tools" / "mock_core.py").read_text(encoding="utf-8")
        self.assertIn(r'(r"^/api/rooms/order$", ("PUT",))', core,
                      "мок принимает PUT /api/rooms/order — иначе живая проверка врёт")


if __name__ == "__main__":
    unittest.main()
