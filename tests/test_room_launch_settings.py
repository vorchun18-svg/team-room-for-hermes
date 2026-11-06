"""Настройки проектного режима КОНКРЕТНОЙ комнаты (волна 9, A1/A2).

Проверяем ровно то, что важно Михаилу:
  * у новой комнаты проектный режим включён, «сначала план», одновременно 2;
  * у старой записи без полей — выключен, «сначала план», 2 (файл не переписывается);
  * PATCH меняет значения, мусор и выход за 1..4 → 400 по-русски;
  * GET /api/rooms и GET /api/rooms/{id} отдают поля (для бейджа в карточке).
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, rooms  # noqa: E402
import server.app as app_mod      # noqa: E402
from tests.helpers import snapshot_real_db  # noqa: E402


class RoomsUnit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-planroom-")
        self.tmp = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in
                       ("TEAMROOM_DATA", "TEAMROOM_VAULT_DIR", "TEAMROOM_HERMES_HOME")}
        os.environ.update({
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
        })
        self.cfg = config.load()
        self.cfg.ensure_dirs()

    def tearDown(self):
        for key, old in self._saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._tmp.cleanup()

    def make(self, **extra):
        data = {"title": "Проект", "orchestrator": "lead", "members": [{"profile": "worker-a"}]}
        data.update(extra)
        return rooms.create_room(self.cfg, data)

    # --- дефолты ---------------------------------------------------------
    def test_new_room_defaults(self):
        room = self.make()
        # W17-R §3 (решение Михаила 06.11): НОВАЯ комната запускает части сразу.
        self.assertEqual(room["launch_mode"], "auto")
        self.assertEqual(room["max_parallel_streams"], 2)
        self.assertIs(room["project_mode"], True)
        self.assertTrue(rooms.get_room(self.cfg, room["id"])["project_mode"])

    def test_explicit_values_are_kept(self):
        room = self.make(launch_mode="auto", max_parallel_streams=4, project_mode=False)
        self.assertEqual(room["launch_mode"], "auto")
        self.assertEqual(room["max_parallel_streams"], 4)
        self.assertIs(room["project_mode"], False)

    def test_validation_on_create(self):
        cases = [
            ({"max_parallel_streams": 5}, "от 1 до 4"),
            ({"max_parallel_streams": 0}, "от 1 до 4"),
            ({"max_parallel_streams": "2"}, "целое число"),
            ({"max_parallel_streams": True}, "целое число"),
            ({"launch_mode": "later"}, "Режим запуска"),
            ({"project_mode": "true"}, "ожидается true или false"),
        ]
        for extra, needle in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(rooms.RoomsError) as ctx:
                    self.make(**extra)
                self.assertEqual(ctx.exception.code, "bad_request")
                self.assertIn(needle, ctx.exception.message)
                self.assertNotRegex(ctx.exception.message, r"Traceback|ValueError|KeyError")

    # --- правка ----------------------------------------------------------
    def test_patch_values(self):
        room = self.make()
        upd = rooms.update_room(self.cfg, room["id"],
                                {"launch_mode": "auto", "max_parallel_streams": 3, "project_mode": False})
        self.assertEqual((upd["launch_mode"], upd["max_parallel_streams"], upd["project_mode"]),
                         ("auto", 3, False))
        stored = rooms.get_room(self.cfg, room["id"])
        self.assertEqual(stored["max_parallel_streams"], 3)

    def test_patch_validation(self):
        room = self.make()
        for patch, needle in [({"max_parallel_streams": 9}, "от 1 до 4"),
                              ({"max_parallel_streams": 1.5}, "целое число"),
                              ({"launch_mode": ""}, None),
                              ({"launch_mode": "for-all"}, "Режим запуска"),
                              ({"project_mode": 1}, "ожидается true или false")]:
            with self.subTest(patch=patch):
                if needle is None:                     # пустая строка — рекомендуемый режим
                    self.assertEqual(rooms.update_room(self.cfg, room["id"], patch)["launch_mode"],
                                     "plan_first")
                    continue
                with self.assertRaises(rooms.RoomsError) as ctx:
                    rooms.update_room(self.cfg, room["id"], patch)
                self.assertEqual(ctx.exception.code, "bad_request")
                self.assertIn(needle, ctx.exception.message)

    def test_patch_keeps_other_settings(self):
        room = self.make()
        rooms.update_room(self.cfg, room["id"], {"max_parallel_streams": 4})
        fresh = rooms.get_room(self.cfg, room["id"])
        self.assertEqual(fresh["title"], "Проект")
        self.assertEqual(fresh["orchestrator"], "lead")
        self.assertEqual([m["profile"] for m in fresh["members"]], ["worker-a"])
        self.assertEqual(fresh["project_dir"], "")

    # --- обратная совместимость -----------------------------------------
    def test_legacy_record_without_fields(self):
        self.make()
        path = Path(self.cfg.data_dir) / "rooms.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        legacy = {"id": "old", "title": "Старая комната", "orchestrator": "lead",
                  "members": [{"profile": "worker-a", "role": "worker"}], "created_at": 1}
        data["rooms"].append(legacy)
        raw = json.dumps(data, ensure_ascii=False)
        path.write_text(raw, encoding="utf-8")

        room = rooms.get_room(self.cfg, "old")
        # Запись без поля — прежнее поведение: «сначала план» (настройки не меняем).
        self.assertEqual(room["launch_mode"], "plan_first")
        self.assertEqual(room["max_parallel_streams"], 2)
        self.assertIs(room["project_mode"], False)
        # чтение ничего не переписывает: старый файл байт-в-байт
        self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_seeded_rooms_are_off(self):
        rooms.seed_defaults(self.cfg)
        for room in rooms.list_rooms(self.cfg):
            self.assertIs(room["project_mode"], False, room["id"])
            # Существующие (посеянные) комнаты читаются по-старому: «сначала план».
            self.assertEqual(room["launch_mode"], "plan_first", room["id"])
            self.assertEqual(room["max_parallel_streams"], 2, room["id"])

    def test_string_false_in_file_does_not_enable(self):
        """В файле могло оказаться "false" строкой — режим не включается молча."""
        self.make()
        path = Path(self.cfg.data_dir) / "rooms.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["rooms"][0]["project_mode"] = "false"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertIs(rooms.get_room(self.cfg, data["rooms"][0]["id"])["project_mode"], False)

    def test_garbage_in_file_does_not_break_list(self):
        self.make()
        path = Path(self.cfg.data_dir) / "rooms.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["rooms"][0]["max_parallel_streams"] = "много"
        data["rooms"][0]["launch_mode"] = 7
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        room = rooms.list_rooms(self.cfg)[0]
        self.assertEqual(room["max_parallel_streams"], 2)
        # Мусор в файле не включает «сразу»: режим остаётся прежним — «сначала план».
        self.assertEqual(room["launch_mode"], "plan_first")


class RoomsApi(unittest.TestCase):
    ENV_KEYS = ("TEAMROOM_DATA", "TEAMROOM_VAULT_DIR", "TEAMROOM_HERMES_HOME", "TEAMROOM_NO_SHELL")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="tr-planroom-api-")
        cls.base = Path(cls.tmp.name)
        cls._saved = {k: os.environ.get(k) for k in cls.ENV_KEYS}
        os.environ.update({
            "TEAMROOM_DATA": str(cls.base / "data"),
            "TEAMROOM_VAULT_DIR": str(cls.base / "vault" / "rooms"),
            "TEAMROOM_HERMES_HOME": str(cls.base / "hermes"),
            "TEAMROOM_NO_SHELL": "1",
        })
        cls.cfg = config.load()
        cls.cfg.ensure_dirs()
        cls.before_board = snapshot_real_db(cls.cfg)
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        try:
            assert snapshot_real_db(cls.cfg) == cls.before_board, "реальная kanban.db изменилась"
        finally:
            for key, old in cls._saved.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
            cls.tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, quote(path, safe="/?=&%:,+"), body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        info = {"status": resp.status}
        ctype = resp.getheader("Content-Type") or ""
        if "json" in ctype and raw:
            info["json"] = json.loads(raw.decode("utf-8"))
        conn.close()
        return info

    # ростор берётся из реальных профилей Hermes (только чтение) — берём существующие
    BODY = {"title": "Комната настроек", "orchestrator": "biz-director",
            "members": [{"profile": "biz-ops", "role": "worker"}]}

    def test_post_defaults_and_get(self):
        res = self.call("POST", "/api/rooms", dict(self.BODY, id="plan-defaults"))
        self.assertEqual(res["status"], 201, res)
        room = res["json"]
        # W17-R §3 (решение Михаила 06.11): НОВАЯ комната запускает части сразу.
        self.assertEqual(room["launch_mode"], "auto")
        self.assertEqual(room["max_parallel_streams"], 2)
        self.assertIs(room["project_mode"], True)

        listing = self.call("GET", "/api/rooms")["json"]["rooms"]
        item = next(r for r in listing if r["id"] == room["id"])
        for key in ("launch_mode", "max_parallel_streams", "project_mode"):
            self.assertIn(key, item)

        detail = self.call("GET", "/api/rooms/" + room["id"])["json"]
        self.assertIs(detail["project_mode"], True)

    def test_patch_and_400(self):
        res = self.call("POST", "/api/rooms", dict(self.BODY, id="plan-patch"))
        rid = res["json"]["id"]

        ok = self.call("PATCH", "/api/rooms/" + rid,
                       {"launch_mode": "auto", "max_parallel_streams": 4, "project_mode": False})
        self.assertEqual(ok["status"], 200, ok)
        self.assertEqual(ok["json"]["launch_mode"], "auto")
        self.assertEqual(ok["json"]["max_parallel_streams"], 4)
        self.assertIs(ok["json"]["project_mode"], False)

        for body, needle in [({"max_parallel_streams": 5}, "от 1 до 4"),
                             ({"max_parallel_streams": 0}, "от 1 до 4"),
                             ({"max_parallel_streams": "два"}, "целое число"),
                             ({"launch_mode": "все-комнаты"}, "Режим запуска"),
                             ({"project_mode": "да"}, "ожидается true или false")]:
            with self.subTest(body=body):
                bad = self.call("PATCH", "/api/rooms/" + rid, body)
                self.assertEqual(bad["status"], 400, bad)
                text = json.dumps(bad.get("json", {}), ensure_ascii=False)
                self.assertIn(needle, text)
                self.assertNotIn("Traceback", text)

        # отклонённая правка ничего не изменила
        fresh = self.call("GET", "/api/rooms/" + rid)["json"]
        self.assertEqual(fresh["max_parallel_streams"], 4)
        self.assertIs(fresh["project_mode"], False)


if __name__ == "__main__":
    unittest.main()
