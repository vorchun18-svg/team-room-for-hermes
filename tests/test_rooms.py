"""Тесты комнат: seed, валидация, участники, архив."""
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


class RoomsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-rooms-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def rooms_json(self) -> dict:
        return json.loads((self.tmp / "data" / "rooms.json").read_text(encoding="utf-8"))


class TestSeed(RoomsBase):
    def _make_vault(self) -> Path:
        """Готовит Vault во временной папке: сид не должен зависеть от машины автора."""
        base = (self.tmp / "vault" / "Projects" / "Business areas"
                / "Лето")
        base.mkdir(parents=True, exist_ok=True)
        for name in ("BRIEF.md", "ROADMAP.md", "VISION.md"):
            (base / name).write_text("# %s\n\nДемо-файл для теста.\n" % name, encoding="utf-8")
        team = self.tmp / "vault" / "Projects" / "Team"
        team.mkdir(parents=True, exist_ok=True)
        (team / "Decisions.md").write_text("# Решения\n\n- Демо-решение.\n", encoding="utf-8")
        return base

    def test_seed_creates_cafe_and_fn(self):
        self._make_vault()
        self.assertTrue(rooms.seed_defaults(self.cfg))
        items = {r["id"]: r for r in rooms.list_rooms(self.cfg)}
        self.assertEqual(set(items), {"cafe", "fn"})

        cafe = items["cafe"]
        self.assertEqual(cafe["board"], "team")
        self.assertEqual(cafe["tenant"], "room:cafe")
        self.assertEqual(cafe["orchestrator"], "biz-director")
        self.assertEqual(cafe["critic"], "biz-critic")
        self.assertTrue(cafe["critic_pass"])
        self.assertTrue(cafe["seeded"])
        self.assertEqual(cafe["max_rounds"], 1)
        self.assertEqual(len(cafe["members"]), 7)
        self.assertEqual({m["profile"] for m in cafe["members"]},
                         {"biz-lawyer", "biz-ops", "biz-strategy", "biz-marketing",
                          "biz-cfo", "biz-runner", "biz-critic"})
        self.assertTrue(all(m["note"] for m in cafe["members"]))
        self.assertTrue(all(isinstance(p, str) and Path(p).is_absolute() for p in cafe["context_files"]))
        self.assertTrue(all(Path(p).is_file() for p in cafe["context_files"]))
        self.assertTrue(not cafe["project_dir"] or Path(cafe["project_dir"]).is_dir())
        self.assertGreaterEqual(len(cafe["context_files"]), 1)

        fn = items["fn"]
        self.assertEqual(fn["tenant"], "room:fn")
        self.assertEqual(fn["orchestrator"], "default")
        self.assertIsNone(fn["critic"])
        self.assertEqual({m["profile"] for m in fn["members"]}, {"lawyer", "coder", "surveyor"})
        self.assertTrue(all(m["note"] for m in fn["members"]))

    def test_seed_is_idempotent_and_never_overwrites(self):
        self.assertTrue(rooms.seed_defaults(self.cfg))
        rooms.update_room(self.cfg, "cafe", {"title": "Моя комната"})
        self.assertFalse(rooms.seed_defaults(self.cfg))
        self.assertEqual(rooms.get_room(self.cfg, "cafe")["title"], "Моя комната")
        self.assertEqual(len(rooms.list_rooms(self.cfg, include_archived=True)), 2)

    def test_seed_does_not_touch_existing_empty_registry(self):
        (self.tmp / "data" / "rooms.json").write_text('{"version": 1, "rooms": []}', encoding="utf-8")
        self.assertFalse(rooms.seed_defaults(self.cfg))
        self.assertEqual(rooms.list_rooms(self.cfg), [])

    def test_seed_repairs_broken_json(self):
        (self.tmp / "data" / "rooms.json").write_text("{битый", encoding="utf-8")
        self.assertTrue(rooms.seed_defaults(self.cfg))
        self.assertEqual({r["id"] for r in rooms.list_rooms(self.cfg)}, {"cafe", "fn"})


class TestCreateValidation(RoomsBase):
    def setUp(self):
        super().setUp()
        rooms.seed_defaults(self.cfg)

    def test_create_room_defaults_and_slug(self):
        room = rooms.create_room(self.cfg, {"title": "Лето", "orchestrator": "biz-director",
                                            "members": [{"profile": "biz-lawyer", "note": "Юрист"}]})
        self.assertEqual(room["id"], "leto")
        self.assertEqual(room["board"], "team")
        self.assertEqual(room["tenant"], "room:leto")
        self.assertEqual(room["icon"], "folder")
        self.assertEqual(room["mode_default"], "auto")
        self.assertFalse(room["critic_pass"])
        self.assertEqual(room["members"][0]["added_at"] > 0, True)

    def test_create_requires_title(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.create_room(self.cfg, {"orchestrator": "x", "members": [{"profile": "a"}]})
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertEqual(ctx.exception.detail["field"], "title")

    def test_create_requires_orchestrator(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.create_room(self.cfg, {"title": "Без директора", "members": [{"profile": "a"}]})
        self.assertEqual(ctx.exception.detail["field"], "orchestrator")

    def test_create_requires_member(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.create_room(self.cfg, {"title": "Пустая", "orchestrator": "x", "members": []})
        self.assertEqual(ctx.exception.detail["field"], "members")

    def test_create_rejects_bad_id(self):
        for bad in ("1room", "комната", "UPPER", "a" * 40):
            with self.assertRaises(rooms.RoomsError, msg=bad):
                rooms.create_room(self.cfg, {"id": bad, "title": "Т", "orchestrator": "x",
                                             "members": [{"profile": "a"}]})

    def test_create_rejects_duplicate_and_bad_mode(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.create_room(self.cfg, {"id": "cafe", "title": "Дубль", "orchestrator": "x",
                                         "members": [{"profile": "a"}]})
        self.assertEqual(ctx.exception.code, "conflict")
        with self.assertRaises(rooms.RoomsError):
            rooms.create_room(self.cfg, {"title": "Режим", "orchestrator": "x",
                                         "mode_default": "fast", "members": [{"profile": "a"}]})

    def test_create_rejects_bad_member_role(self):
        with self.assertRaises(rooms.RoomsError):
            rooms.create_room(self.cfg, {"title": "Роль", "orchestrator": "x",
                                         "members": [{"profile": "a", "role": "boss"}]})


class TestUpdateAndMembers(RoomsBase):
    def setUp(self):
        super().setUp()
        rooms.seed_defaults(self.cfg)

    def test_update_allowed_fields(self):
        room = rooms.update_room(self.cfg, "cafe", {"title": "Лето 2", "icon": "cake",
                                                   "critic_pass": False, "max_rounds": 2})
        self.assertEqual(room["title"], "Лето 2")
        self.assertFalse(room["critic_pass"])
        self.assertEqual(room["max_rounds"], 2)

    def test_update_forbids_immutable_and_unknown(self):
        for patch in ({"id": "x"}, {"board": "other"}, {"tenant": "room:x"}, {"что-то": 1}):
            with self.assertRaises(rooms.RoomsError) as ctx:
                rooms.update_room(self.cfg, "cafe", patch)
            self.assertEqual(ctx.exception.code, "bad_request")
            self.assertTrue(ctx.exception.detail["fields"])

    def test_update_not_found_and_empty_title(self):
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.update_room(self.cfg, "nope", {"title": "x"})
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(rooms.RoomsError):
            rooms.update_room(self.cfg, "cafe", {"title": "   "})

    def test_add_member_and_duplicate(self):
        res = rooms.add_member(self.cfg, "cafe", {"profile": "coder", "note": "Код", "role": "worker"})
        self.assertEqual(res["member"]["profile"], "coder")
        self.assertIn("coder", [m["profile"] for m in res["members"]])
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.add_member(self.cfg, "cafe", {"profile": "coder"})
        self.assertEqual(ctx.exception.code, "conflict")

    def test_add_member_bad_role_and_missing_room(self):
        with self.assertRaises(rooms.RoomsError):
            rooms.add_member(self.cfg, "cafe", {"profile": "coder", "role": "chief"})
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.add_member(self.cfg, "nope", {"profile": "coder"})
        self.assertEqual(ctx.exception.code, "not_found")

    def test_remove_member_works_but_protects_room_lead(self):
        res = rooms.remove_member(self.cfg, "cafe", "biz-ops")
        self.assertNotIn("biz-ops", [m["profile"] for m in res["members"]])
        for profile in ("biz-director", "biz-critic"):
            with self.assertRaises(rooms.RoomsError) as ctx:
                rooms.remove_member(self.cfg, "cafe", profile)
            self.assertEqual(ctx.exception.code, "conflict")
            self.assertEqual(ctx.exception.detail["reason"], "room_lead")
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.remove_member(self.cfg, "cafe", "ghost")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_archive(self):
        first = rooms.archive_room(self.cfg, "cafe")
        self.assertTrue(first["changed"])
        self.assertTrue(first["room"]["archived"])
        self.assertEqual([r["id"] for r in rooms.list_rooms(self.cfg)], ["fn"])
        self.assertEqual(len(rooms.list_rooms(self.cfg, include_archived=True)), 2)
        second = rooms.archive_room(self.cfg, "cafe")
        self.assertFalse(second["changed"])
        with self.assertRaises(rooms.RoomsError):
            rooms.archive_room(self.cfg, "nope")

    def test_slugify(self):
        self.assertEqual(rooms.slugify("Лето"), "leto")
        self.assertEqual(rooms.slugify("Рабочее пространство"), "rabochee-prostranstvo")
        self.assertEqual(rooms.slugify("Ёжик  и  Ко!"), "ezhik-i-ko")
        self.assertTrue(rooms.slugify("123").startswith("room-"))


if __name__ == "__main__":
    unittest.main()
