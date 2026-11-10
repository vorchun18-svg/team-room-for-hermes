"""Комнаты: сохранность rooms.json (D4), id (D16) и валидация полей (D7)."""
from __future__ import annotations

import codecs
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import app as app_mod          # noqa: E402
from server import config, rooms           # noqa: E402
from server import statefiles as sf        # noqa: E402


class RoomsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-rooms2-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self.path = self.tmp / "data" / "rooms.json"

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class TestRoomsSurvive(RoomsBase):
    def test_bom_file_with_rooms_is_not_overwritten(self):
        """D4: BOM читается как utf-8-sig — комнаты пользователя остаются."""
        payload = {"version": 1, "rooms": [{"id": "mine", "title": "Моя", "board": "team",
                                            "tenant": "room:mine", "orchestrator": "biz-director",
                                            "members": [], "context_files": [], "created_at": 5}]}
        self.path.write_bytes(codecs.BOM_UTF8 + json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertFalse(rooms.seed_defaults(self.cfg))
        self.assertIsNotNone(rooms.get_room(self.cfg, "mine"))
        self.assertFalse(any(p.name.startswith("rooms.json.corrupt-") for p in self.path.parent.iterdir()))

    def test_truncated_file_gets_corrupt_copy_before_seed(self):
        broken = '{"version": 1, "rooms": [{"id": "mine"'
        self.path.write_text(broken, encoding="utf-8")
        self.assertTrue(rooms.seed_defaults(self.cfg))
        backups = [p for p in self.path.parent.iterdir() if p.name.startswith("rooms.json.corrupt-")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), broken)
        self.assertEqual({r["id"] for r in rooms.list_rooms(self.cfg)}, {"cafe", "fn"})
        problems = [p for p in sf.problems() if p["path"] == str(self.path)]
        self.assertTrue(problems)
        self.assertIn("копия", problems[-1]["message"])
        self.assertEqual(problems[-1]["backup"], str(backups[0]))

    def test_permission_error_is_not_replaced_by_defaults(self):
        """D4: занятый файл — ошибка, а не молчаливый сид поверх данных."""
        original = '{"version": 1, "rooms": [{"id": "mine"}]}'
        self.path.write_text(original, encoding="utf-8")
        real_open = open

        def fake_open(file, *args, **kwargs):
            if str(file) == str(self.path):
                raise PermissionError(13, "файл занят", str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", fake_open), mock.patch.object(sf, "READ_PAUSE", 0.0):
            with self.assertRaises(sf.StateUnavailable):
                rooms.seed_defaults(self.cfg)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_listing_broken_registry_makes_quarantine(self):
        """D5 (реестр читается rooms-независимо): битый requests.json → копия, не «пусто»."""
        from server import registry
        reg = self.tmp / "data" / "state" / "cafe" / "requests.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text('{"room": "cafe", "requests": [', encoding="utf-8")
        self.assertEqual(registry.list_requests(self.cfg, "cafe"), [])
        backups = [p for p in reg.parent.iterdir() if p.name.startswith("requests.json.corrupt-")]
        self.assertEqual(len(backups), 1)


class TestRoomIds(RoomsBase):
    def _create(self, room_id):
        return rooms.create_room(self.cfg, {"id": room_id, "title": "Т", "orchestrator": "biz-director",
                                            "members": [{"profile": "biz-lawyer"}]})

    def test_reserved_windows_names_rejected(self):
        """D16: nul/con/com1 ломали watcher — такой id теперь 400."""
        for bad in ("nul", "NUL", "con", "prn", "aux", "com1", "com9", "lpt1", "lpt9"):
            with self.assertRaises(rooms.RoomsError, msg=bad) as ctx:
                self._create(bad.lower())
            self.assertEqual(ctx.exception.code, "bad_request")
            self.assertEqual(ctx.exception.detail.get("reason"), "reserved", bad)

    def test_single_char_ids_rejected(self):
        for bad in ("a", "z"):
            with self.assertRaises(rooms.RoomsError, msg=bad):
                self._create(bad)

    def test_underscore_and_two_char_ids_accepted_and_routable(self):
        room = self._create("my_room")
        self.assertEqual(room["id"], "my_room")
        self.assertIsNotNone(self._create("ab"))
        routes = {handler: pattern for _method, pattern, handler in app_mod.ROUTES}
        self.assertTrue(routes["h_room_get"].fullmatch("/api/rooms/my_room"))
        self.assertTrue(routes["h_request_create"].fullmatch("/api/rooms/my_room/requests"))
        self.assertFalse(routes["h_room_get"].fullmatch("/api/rooms/a"))

    def test_slug_regex_and_route_agree(self):
        for room_id in ("cafe", "fn", "my_room", "room-2", "a1"):
            self.assertTrue(rooms._SLUG_RE.match(room_id), room_id)
            self.assertEqual(re.search(app_mod._ROOM, room_id).group(0), room_id, room_id)
        for room_id in ("a", "1room", "UPPER"):
            self.assertFalse(rooms._SLUG_RE.match(room_id), room_id)
        self.assertEqual(rooms._SLUG_RE.pattern, "^[a-z][a-z0-9_-]{1,31}$")
        self.assertEqual(app_mod._ROOM, r"(?P<room_id>[a-z][a-z0-9_-]{1,31})")


class TestRoomFields(RoomsBase):
    def _create(self, **extra):
        body = {"title": "Комната", "orchestrator": "biz-director",
                "members": [{"profile": "biz-lawyer"}]}
        body.update(extra)
        return rooms.create_room(self.cfg, body)

    def test_bad_board_icon_paths_rejected(self):
        """D7: board/icon/context_files не проходят как произвольные значения."""
        for extra in ({"board": "../../escape-board"}, {"icon": [1, 2]},
                      {"icon": "cake!"}, {"context_files": "abc"}, {"context_files": [1, 2]}):
            with self.assertRaises(rooms.RoomsError, msg=str(extra)) as ctx:
                self._create(**extra)
            self.assertEqual(ctx.exception.code, "bad_request", extra)

    def test_critic_pass_and_rounds_are_strict(self):
        for extra in ({"critic_pass": "no"}, {"critic_pass": 1}, {"max_rounds": "abc"},
                      {"max_rounds": 99}, {"max_rounds": 6}, {"max_rounds": True}):
            with self.assertRaises(rooms.RoomsError, msg=str(extra)) as ctx:
                self._create(**extra)
            self.assertEqual(ctx.exception.code, "bad_request", extra)
        room = self._create(max_rounds=5, critic_pass=True)
        self.assertEqual(room["max_rounds"], 5)
        self.assertTrue(room["critic_pass"])

    def test_critic_cannot_equal_orchestrator(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            self._create(critic="biz-director")
        self.assertEqual(ctx.exception.detail["field"], "critic")

    def test_update_validates_fields(self):
        rooms.seed_defaults(self.cfg)
        for patch in ({"max_rounds": "abc"}, {"max_rounds": 9}, {"critic_pass": "no"},
                      {"icon": [1, 2]}, {"context_files": "abc"}, {"critic": "biz-director"}):
            with self.assertRaises(rooms.RoomsError, msg=str(patch)) as ctx:
                rooms.update_room(self.cfg, "cafe", patch)
            self.assertEqual(ctx.exception.code, "bad_request", patch)
        ok = rooms.update_room(self.cfg, "cafe", {"max_rounds": 3})
        self.assertEqual(ok["max_rounds"], 3)


if __name__ == "__main__":
    unittest.main()
