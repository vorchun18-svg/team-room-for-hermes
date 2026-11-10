# -*- coding: utf-8 -*-
"""W17-F: закрепление записи в «Особенностях» — маршрут `…/entries/{e}/pin`.

Окно-«блокнот» (WAVE17 P6) показывает кнопку «Закрепить», а закрепление меняется
только через новый маршрут: без него интерфейсу пришлось бы переписывать запись
целиком (и терять её id/историю). Проверяем:

  * `POST …/agents/{p}/notes/entries/{e}/pin` ставит и снимает закрепление;
  * событие `pinned`/`unpin` попадает в журнал комнаты, откат возвращает состояние;
  * без поля `pinned` — 400 по-русски, неизвестная запись — 404, архив — 409;
  * старый контракт (`PUT …/notes`, `POST …/entries`) не изменился.

Правила волны: только песочница `tests.helpers.Sandbox`, файлы профилей
(SOUL.md) сверяются по хэшам до/после.

    python -m unittest tests.test_notes_pin -v
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_notes as notes_mod          # noqa: E402
import server.app as app_mod                    # noqa: E402
from server.http_util import ApiError           # noqa: E402
from tests.helpers import Sandbox               # noqa: E402

ROOM = "cafe"
PROFILE = "biz-lawyer"


def soul_hashes() -> dict:
    home = Path.home() / "AppData" / "Local" / "hermes"
    paths = [home / "SOUL.md"] + sorted((home / "profiles").glob("*/SOUL.md"))
    out = {}
    for path in paths:
        try:
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


class PinRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.soul_before = soul_hashes()
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
            assert soul_hashes() == cls.soul_before, "файлы профилей (SOUL.md) изменились!"
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_notes": {}, "agent_notes_log": []}), {}))

    # --- помощники ---------------------------------------------------------
    def dispatch(self, method: str, path: str, body=None, query=None):
        try:
            return self.app.dispatch(method, path, query or {}, body or {})
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def add(self, text: str, **kwargs) -> dict:
        res = notes_mod.add(self.cfg, ROOM, PROFILE, text, **kwargs)
        return res["entry"]

    def pin_path(self, entry_id: str) -> str:
        return "/api/rooms/%s/agents/%s/notes/entries/%s/pin" % (ROOM, PROFILE, entry_id)

    def entries(self) -> list:
        return notes_mod.entries_of(notes_mod.notes_of(
            self.app.call("rooms", "get_room", ((self.cfg, ROOM), {}))).get(PROFILE) or {})

    # --- тесты -------------------------------------------------------------
    def test_pin_and_unpin(self):
        entry = self.add("Проверяй договор по закону X", source="leader")
        self.assertFalse(entry["pinned"])

        status, body, _ = self.dispatch("POST", self.pin_path(entry["id"]), {"pinned": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["entry"]["pinned"], "запись закреплена")
        self.assertTrue(self.entries()[0]["pinned"])

        status, body, _ = self.dispatch("POST", self.pin_path(entry["id"]), {"pinned": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["entry"]["pinned"], "закрепление снято")

    def test_pin_is_in_journal_and_can_be_rolled_back(self):
        entry = self.add("Не обсуждай цены ниже Y", source="leader")
        status, body, _ = self.dispatch("POST", self.pin_path(entry["id"]), {"pinned": True})
        self.assertEqual(status, 200)

        events = notes_mod.journal(self.cfg, ROOM)
        self.assertEqual(events[0]["action"], "pinned")
        self.assertEqual(events[0]["profile"], PROFILE)
        self.assertIn("цены ниже", " ".join(str(e.get("text") or "") for e in events[:2]))
        event_id = events[0]["id"]

        status, body, _ = self.dispatch("POST", "/api/rooms/%s/notes/journal/%s/rollback"
                                        % (ROOM, event_id), {"why": "передумал"})
        self.assertEqual(status, 200)
        self.assertFalse(self.entries()[0]["pinned"], "откат вернул прежнее состояние")

    def test_pin_requires_field(self):
        entry = self.add("Отвечай коротко")
        status, body, _ = self.dispatch("POST", self.pin_path(entry["id"]), {})
        self.assertEqual(status, 400)
        self.assertIn("pinned", str(body.get("message") or body.get("error") or body))

    def test_pin_unknown_entry(self):
        status, body, _ = self.dispatch("POST", self.pin_path("n_nosuch"), {"pinned": True})
        self.assertEqual(status, 404)

    def test_old_contract_still_works(self):
        """Старый полный PUT создаёт закреплённую запись — pin его не ломает."""
        status, body, _ = self.dispatch("PUT", "/api/rooms/%s/agents/%s/notes" % (ROOM, PROFILE),
                                        {"text": "Только цифры и сроки"})
        self.assertEqual(status, 200)
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["pinned"])
        status, body, _ = self.dispatch("POST", self.pin_path(entries[0]["id"]), {"pinned": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["entry"]["pinned"])


if __name__ == "__main__":
    unittest.main()
