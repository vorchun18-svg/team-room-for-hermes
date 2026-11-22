# -*- coding: utf-8 -*-
"""Мок ручек партии B (W29-AC2): формы совпадают с боевым сервером.

Проверяем не «мок отвечает 200», а то, что демо-экран показывает правду:
совместимость различает «модель задана комнатой» и «из профиля», второе мнение
отдаёт выбор моделей и разбирает ответ по меткам, итог дня считает счётчики как
длину списков, поиск по памяти ищет и возвращает маршрут, настройки уведомлений
сохраняются и «Проверить» появляется в журнале.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import mock_server          # noqa: E402


class BatchBMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mock_server.create_server(port=0, phase_scale=0.01)
        cls.app = cls.server.app
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # Порядок тестов не должен влиять: «Исправить» в одном тесте меняет реестр
        # комнаты — возвращаем демо-участнику выбор комнаты, как в фикстуре.
        for member in self.app.room("cafe").get("members") or []:
            if member.get("profile") == "team-ops":
                member["model_settings"].update({"provider": "oblako-a", "model": "dilizhans-ultra",
                                                 "reasoning": "high", "source": "room"})

    def get(self, path, query=None):
        status, payload = self.app.get(path, query or {})
        self.assertEqual(status, 200)
        return payload

    def test_parity_lists_members_and_flags_room_model(self):
        data = self.get("/api/rooms/cafe/parity")
        self.assertEqual(data["room"], "cafe")
        self.assertTrue(data["members"])
        self.assertEqual(data["summary"]["total"], len(data["members"]))
        fails = [m for m in data["members"] if m["status"] == "fail"]
        ok = [m for m in data["members"] if m["status"] == "ok"]
        self.assertTrue(fails and ok, "демо должно показывать и совпадение, и расхождение")
        fail = fails[0]
        self.assertEqual(fail["model"]["source"], "room")
        kinds = [d["fix"]["kind"] for d in fail["diffs"]]
        self.assertIn("reset_room_model", kinds)
        for member in data["members"]:
            for key in ("model", "skills", "tools", "mcp", "permissions"):
                self.assertIn(key, member)

    def test_parity_fix_returns_profile_model(self):
        status, payload = self.app.post("/api/rooms/cafe/parity/fix", {},
                                        {"action": "reset_model", "profile": "team-ops"})
        self.assertEqual(status, 200)
        member = next(m for m in payload["members"] if m["profile"] == "team-ops")
        self.assertEqual(member["model"]["source"], "profile")
        self.assertFalse([d for d in member["diffs"] if d["code"] == "model_differs"])

    def test_second_opinion_view_and_run(self):
        view = self.get("/api/requests/t_a1b2c3d4/second-opinion")
        self.assertTrue(view["models"])
        self.assertTrue(view["can_run"])
        self.assertTrue(view["default"])
        self.assertTrue(view["version"])
        status, payload = self.app.post("/api/requests/t_a1b2c3d4/actions", {},
                                        {"action": "second_opinion", "provider": view["default"]["provider"],
                                         "model": view["default"]["model"]})
        self.assertEqual(status, 200)
        after = self.get("/api/requests/t_a1b2c3d4/second-opinion")
        self.assertEqual(len(after["checks"]), len(view["checks"]) + 1)
        check = after["checks"][-1]
        self.assertTrue(check["model"])
        self.assertTrue(check["items"], "ответ по меткам должен разбираться")
        self.assertGreaterEqual(sum(check["counts"].values()), 1)

    def test_daily_counters_equal_lists(self):
        data = self.get("/api/daily")
        self.assertTrue(data["day"])
        self.assertTrue(data["period_title"])
        for key in ("done", "decisions", "waiting", "files", "stopped"):
            self.assertEqual(data["counters"][key], len(data[key]))
        self.assertEqual(data["counters"]["all"], sum(data["counters"][k] for k in
                                                      ("done", "decisions", "waiting", "files", "stopped")))
        status, short = self.app.post("/api/daily/shorten", {}, {"room": "cafe"})
        self.assertEqual(status, 200)
        self.assertTrue(short["ok"] and short["text"])

    def test_memory_search_finds_and_routes(self):
        data = self.get("/api/memory/search", {"q": ["подрядчик"]})
        self.assertTrue(data["total"] >= 1)
        self.assertEqual(sum(data["by_kind"].values()), data["total"])
        self.assertTrue(all(item["route"].startswith("#/") for item in data["items"]))
        empty = self.get("/api/memory/search", {"q": [""]})
        self.assertEqual(empty["total"], 0)
        self.assertTrue(empty["note"])

    def test_notify_settings_saved_and_test_logged(self):
        data = self.get("/api/notify")
        self.assertFalse(data["settings"]["enabled"])
        self.assertEqual(len(data["kinds"]), 4)
        status, saved = self.app.put("/api/notify/settings", {}, {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(saved["settings"]["enabled"])
        status, tested = self.app.post("/api/notify/test", {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(tested["ok"])
        after = self.get("/api/notify")
        self.assertEqual(after["sent_today"], 1)
        self.assertEqual(after["history"][0]["title"], "Проверка уведомлений")


if __name__ == "__main__":
    unittest.main()
