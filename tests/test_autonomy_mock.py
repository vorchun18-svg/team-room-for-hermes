# -*- coding: utf-8 -*-
"""W27-E6: мок-двойник «Уровня автономии» — формы ответов контракта §2.

Проверяем то, на чём работает интерфейс (views/autonomy.js), пока серверная часть
(E5) не готова:

* `GET /api/rooms/{id}/autonomy` — уровень, список «только с моего да», участники
  комнаты, предпросмотр всех трёх уровней, значения по умолчанию;
* `POST` без `confirm:true` — 409 с предпросмотром (ничего не меняется);
* `POST` с `confirm:true` — уровень применяется: рабочий включает классы
  «свои скрипты», свободный добавляет запрет по списку, осторожный снимает всё;
* `GET …/autonomy/journal` — строки «сделано само» и остановленные, критичные
  получают `severity: critical`, у жёсткого запрета `floor/grantable`;
* `GET …/autonomy/suggest` — предложения по истории, уже добавленное не повторяется.

Мок поднимается тем же `tools/mock_server.create_server`, что и демо (порт 0).

    python -m unittest tests.test_autonomy_mock -v
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for path in (ROOT, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import mock_server  # noqa: E402  (tools/mock_server.py)

ROOM = "cafe"
LEVELS = {"careful", "working", "free"}
SCRIPT_CLASSES = {"script execution via -e/-c flag", "script execution via heredoc"}


class AutonomyMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mock_server.create_server(port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    # --- помощники ---------------------------------------------------------
    def request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def autonomy(self) -> dict:
        status, data = self.request("GET", "/api/rooms/%s/autonomy" % ROOM)
        self.assertEqual(status, 200)
        return data

    # --- чтение ------------------------------------------------------------
    def test_read_has_contract_shape(self):
        data = self.autonomy()
        for key in ("level", "never", "profiles", "defaults", "preview"):
            self.assertIn(key, data, key)
        self.assertIn(data["level"], LEVELS)
        self.assertTrue(data["never"], "список «только с моего да» не пустой")
        self.assertIn("never", data["defaults"])
        self.assertEqual(set(data["preview"]), LEVELS, "предпросмотр есть у всех трёх уровней")

        for person in data["profiles"]:
            for key in ("profile", "allowlist", "single_query_mode", "deny"):
                self.assertIn(key, person, key)
            self.assertIn(person["single_query_mode"], ("deny", "approve"))

        for level, block in data["preview"].items():
            self.assertTrue(block["changes"], level + ": «что изменится» не пусто")
            for line in block["changes"]:
                self.assertNotIn("allowlist", line, "текст для человека, не для машины")
                self.assertNotIn("single_query_mode", line)

    def test_working_level_allows_scripts_and_free_adds_deny(self):
        data = self.autonomy()
        working = next(p for p in data["profiles"] if p["single_query_mode"] == "deny")
        if data["level"] == "working":
            self.assertTrue(SCRIPT_CLASSES.issubset(set(working["allowlist"])),
                            "рабочий уровень: свои скрипты в списке разрешений")

        status, applied = self.request("POST", "/api/rooms/%s/autonomy" % ROOM,
                                       {"level": "free", "confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(applied["level"], "free")
        self.assertTrue(applied["backup"].startswith("data/backups/autonomy/"))
        for person in applied["applied"]:
            self.assertEqual(person["single_query_mode"], "approve")
            self.assertEqual(person["deny"], applied["never"],
                             "свободный: запрет ровно по списку «только с моего да»")

        status, careful = self.request("POST", "/api/rooms/%s/autonomy" % ROOM,
                                       {"level": "careful", "confirm": True})
        self.assertEqual(status, 200)
        for person in careful["applied"]:
            self.assertEqual(person["allowlist"], [], "осторожный снимает записи")
            self.assertEqual(person["deny"], [])
            self.assertEqual(person["single_query_mode"], "deny")

    # --- запись ------------------------------------------------------------
    def test_without_confirm_is_409_with_preview_and_changes_nothing(self):
        before = self.autonomy()
        status, body = self.request("POST", "/api/rooms/%s/autonomy" % ROOM, {"level": "free"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "confirm_required")
        detail = body["error"]["detail"]
        self.assertEqual(detail["level"], "free")
        self.assertTrue(detail["preview"]["free"]["changes"])
        after = self.autonomy()
        self.assertEqual(before["level"], after["level"], "без подтверждения ничего не меняется")

    def test_bad_level_is_400(self):
        status, body = self.request("POST", "/api/rooms/%s/autonomy" % ROOM,
                                    {"level": "yolo", "confirm": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_never_list_is_saved_normalised(self):
        status, applied = self.request("POST", "/api/rooms/%s/autonomy" % ROOM,
                                       {"level": "working", "confirm": True,
                                        "never": ["  удаление папки целиком ", "удаление папки целиком",
                                                  "отправка писем"]})
        self.assertEqual(status, 200)
        self.assertEqual(applied["never"], ["удаление папки целиком", "отправка писем"])
        self.assertEqual(self.autonomy()["never"], ["удаление папки целиком", "отправка писем"])

        status, body = self.request("POST", "/api/rooms/%s/autonomy" % ROOM,
                                    {"level": "working", "confirm": True, "never": ["x" * 200]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    # --- журнал и предложения ---------------------------------------------
    def test_journal_marks_critical_and_floor(self):
        status, data = self.request("GET", "/api/rooms/%s/autonomy/journal" % ROOM)
        self.assertEqual(status, 200)
        self.assertIn("items", data)
        self.assertIn("counts", data)
        self.assertTrue(all(i["kind"] in ("self", "blocked") for i in data["items"]))
        self.assertTrue(all(i["severity"] in ("critical", "routine") for i in data["items"]))
        for item in data["items"]:
            self.assertIn("at", item)
            if item["kind"] == "blocked":
                self.assertTrue(item.get("request_id"), "у блокировки есть переход в карточку")
                self.assertIn("grantable", item)
        self.assertEqual(data["counts"]["self"],
                         sum(1 for i in data["items"] if i["kind"] == "self"))

    def test_suggest_is_honest_about_destructive(self):
        status, data = self.request("GET", "/api/rooms/%s/autonomy/suggest" % ROOM)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        current = {x.lower() for x in self.autonomy()["never"]}
        for row in data["items"]:
            self.assertTrue(row["key"])
            self.assertFalse(row["destructive"], "разрушительное не предлагаем")
            self.assertNotIn(str(row["label"]).lower(), current, "уже добавленное не предлагаем")


if __name__ == "__main__":
    unittest.main()
