# -*- coding: utf-8 -*-
"""W20-W1: маршрут `GET /api/requests/{id}/worklog` (HTTP-слой).

Проверяется подключение в `server/app.py`, коды и заголовки, параметры запроса и то, что
ошибки уходят 4xx по-русски, а не 500. Данные — из вымышленной песочницы
(`tools/worklog_fixture_db.py`), живые базы Hermes не участвуют.

    python -m unittest tests.test_worklog_routes -v
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                       # noqa: E402
import server.rooms as rooms_mod                   # noqa: E402
from server import config as config_mod            # noqa: E402
from server.http_util import ApiError              # noqa: E402
from tools.worklog_fixture_db import LEADER, ROOT_CARD, WORKER, build   # noqa: E402


def _tmp_base() -> Path:
    base = os.environ.get("TMPDIR") or os.environ.get("TEAMROOM_TEST_TMP") or tempfile.gettempdir()
    return Path(base)


class TestWorklogRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="worklog-route-", dir=str(_tmp_base())))
        made = build(cls.root)
        cfg = config_mod.load()
        cls.cfg = dataclasses.replace(cfg, hermes_home=Path(made["hermes_home"]),
                                      kanban_home=Path(made["kanban_home"]),
                                      data_dir=cls.root / "data")
        cls.cfg.ensure_dirs()
        cls.room = {"id": "demo", "board": "demo", "orchestrator": LEADER, "critic": None,
                    "members": [{"profile": WORKER, "label": "Анна — юрист"}],
                    "role_labels": {LEADER: "Лев — руководитель"}}
        cls.app = app_mod.App(cls.cfg, 0)
        cls.record = {"root_id": ROOT_CARD, "extra_cards": [], "room": "demo"}
        cls.app._find_record = lambda rid: (cls.room, cls.record)   # без реестра

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def call(self, **query):
        return self.app.dispatch("GET", "/api/requests/%s/worklog" % ROOT_CARD,
                                 {k: [v] for k, v in query.items()}, None)

    def test_route_registered(self):
        patterns = [(method, pattern.pattern) for method, pattern, _handler in app_mod.ROUTES]
        self.assertIn(("GET", r"/api/requests/(?P<rid>t_[A-Za-z0-9_]+)/worklog"), patterns)

    def test_ok_payload(self):
        status, payload, headers = self.call(limit="30")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(payload["request"], ROOT_CARD)
        self.assertEqual(payload["room"], "demo")
        self.assertEqual(payload["mode"], "full")
        self.assertTrue(payload["items"])
        self.assertIn("cursor", payload)
        self.assertIsInstance(payload["live"], list)
        for item in payload["items"]:
            for key in ("id", "ts", "card_id", "profile", "label", "kind", "text", "tool"):
                self.assertIn(key, item)
            self.assertIn(item["kind"], ("thought", "action", "result", "message",
                                         "status", "handoff", "digest"))

    def test_cursor_is_opaque_and_works(self):
        _status, first, _headers = self.call(limit="5")
        _status, second, _headers = self.call(limit="5")
        self.assertEqual(first["cursor"], second["cursor"])
        _status, empty, _headers = self.call(since=first["cursor"])
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["more"])

    def test_level_steps(self):
        _status, payload, _headers = self.call(level="steps", limit="30")
        self.assertEqual(payload["level"], "steps")
        self.assertNotIn("thought", {item["kind"] for item in payload["items"]})

    def test_bad_parameters_are_400(self):
        for query in ({"level": "all"}, {"limit": "0"}, {"limit": "500"}, {"limit": "много"}):
            with self.assertRaises(ApiError) as ctx:
                self.call(**query)
            self.assertEqual(ctx.exception.status, 400, query)

    def test_unknown_request_is_404(self):
        app = app_mod.App(self.cfg, 0)          # настоящий _find_record, реестр пуст
        with self.assertRaises(ApiError) as ctx:
            app.dispatch("GET", "/api/requests/t_00000000/worklog", {}, None)
        self.assertEqual(ctx.exception.status, 404)

    def test_wrong_method_is_405(self):
        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("POST", "/api/requests/%s/worklog" % ROOT_CARD, {}, {})
        self.assertEqual(ctx.exception.status, 405)

    def test_patch_room_accepts_worklog_settings(self):
        """`PATCH /api/rooms/{id}` реально принимает поля (иначе настройка недоступна)."""
        room = rooms_mod.create_room(self.cfg, {"title": "Кафе «Лето»", "orchestrator": "lev",
                                                "members": [{"profile": "anna"}]})
        status, body, _headers = self.app.dispatch(
            "PATCH", "/api/rooms/" + room["id"],
            {}, {"worklog_mode": "brief", "digest_every_min": 5})
        self.assertEqual(status, 200)
        self.assertEqual(body["worklog_mode"], "brief")
        self.assertEqual(body["digest_every_min"], 5)
        self.assertEqual(body["worklog"]["mode_label"], "Кратко (только действия)")
        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("PATCH", "/api/rooms/" + room["id"], {},
                              {"worklog_mode": "verbose"})
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
