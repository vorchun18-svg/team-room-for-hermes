"""Регрессия «Элемент бэклога не найден» (WAVE8, Блок B).

Причина (проверена воспроизведением): шаги на странице запроса берутся из
`RequestDetail.next_steps` (id `<root_id>:ns_N`), а отметка идёт в бэклог комнаты,
куда они попадают только после ingest. Пока watcher не принял запрос — 404.

Здесь проверяется, что:
* отметка шага не принятого запроса проходит (пункт upsert'ится из метаданных);
* повторная отметка идемпотентна (одна запись, без дублей);
* состояние видно в `/api/rooms/{room}/backlog` и на странице запроса;
* id с закодированным двоеточием (`%3A`) работает;
* 404 остаётся только если элемента нет и в запросе;
* открытие готового запроса само включает приём (ensure_ingested).
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                       # noqa: E402
from server import engine_read, ingest, registry, rooms  # noqa: E402

try:
    from tests.helpers import Sandbox
except Exception:  # pragma: no cover - helpers.py может ещё писаться
    Sandbox = None


def _register(sb, rid: str) -> None:
    """Запрос из песочной доски → реестр комнаты cafe (без ingest)."""
    row = sb.one("SELECT id, title, body, created_at FROM tasks WHERE id = ?", (rid,))
    assert row is not None, "нет карточки %s в песочнице" % rid
    registry.add_request(sb.cfg, "cafe", {
        "root_id": rid, "slug": "late-" + rid[-4:], "title": row["title"],
        "text": row["body"], "created_at": row["created_at"], "critic": False,
        "mode": "auto", "adopted": False, "extra_cards": [], "retargeted": True,
    })


def _detail(sb, rid: str) -> dict:
    room = rooms.get_room(sb.cfg, "cafe")
    return engine_read.request_detail(sb.cfg, room.get("board") or "team", rid, room)


def _backlog_items(sb, rid: str | None = None) -> list[dict]:
    items = ingest.backlog_view(sb.cfg, "cafe")["items"]
    return [i for i in items if rid is None or i.get("source_request") == rid]


@unittest.skipUnless(Sandbox is not None, "tests/helpers.py (Sandbox) ещё не готов")
class BacklogUpsertTests(unittest.TestCase):
    """Песочница + реальный сервер на свободном порту (не 8777/8778)."""

    def setUp(self):
        self._sb = Sandbox()
        self.sb = self._sb.__enter__()
        self.server = app_mod.create_app(self.sb.cfg, start_watcher=False, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._sb.__exit__(None, None, None)

    # --- HTTP ---------------------------------------------------------------
    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if method != "GET" else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, quote(path, safe="/?=&%:+"), body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        data = None
        if raw and "json" in (resp.getheader("Content-Type") or ""):
            data = json.loads(raw.decode("utf-8"))
        return resp.status, data

    # --- сценарий 1: шаг запроса, не принятый watcher'ом ---------------------
    def test_step_of_unaccepted_request_can_be_marked(self):
        rid = self.sb.ids["done_modern"]["root_id"]
        _register(self.sb, rid)
        detail = _detail(self.sb, rid)          # прямой вызов, без HTTP: ingest не запускается
        self.assertTrue(detail.get("next_steps"), "в песочнице у запроса нет шагов")
        item_id = "%s:%s" % (rid, detail["next_steps"][0]["id"])
        self.assertEqual(_backlog_items(self.sb, rid), [], "предусловие: шагов в бэклоге ещё нет")

        # id с закодированным двоеточием (%3A) — как шлёт фронт
        path = "/api/rooms/cafe/backlog/" + quote(item_id, safe="")
        self.assertTrue(path.endswith("%3A" + quote(item_id.split(":", 1)[1], safe="")))

        status, data = self.call("POST", path, {"status": "done"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["item"]["id"], item_id)
        self.assertEqual(data["item"]["status"], "done")
        self.assertEqual(data["item"]["source_request"], rid)

        # повторная отметка — идемпотентна: одна запись, без дублей
        status, again = self.call("POST", path, {"status": "done"})
        self.assertEqual(status, 200, again)
        same = [i for i in _backlog_items(self.sb, rid) if i["id"] == item_id]
        self.assertEqual(len(same), 1, "пункт задвоился")
        self.assertEqual(same[0]["status"], "done")

        # состояние видно в бэклоге комнаты и в разделе «Выполнено»
        status, view = self.call("GET", "/api/rooms/cafe/backlog")
        self.assertEqual(status, 200)
        got = next((i for i in view["items"] if i["id"] == item_id), None)
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "done")
        self.assertGreaterEqual(view["counts"]["done"], 1)

        # и на странице запроса шаги на месте
        status, page = self.call("GET", "/api/requests/" + quote(rid, safe=""))
        self.assertEqual(status, 200)
        self.assertTrue(page.get("next_steps"))

    def test_due_and_reopen_on_unaccepted_step(self):
        rid = self.sb.ids["done_modern"]["root_id"]
        _register(self.sb, rid)
        detail = _detail(self.sb, rid)
        item_id = "%s:%s" % (rid, detail["next_steps"][0]["id"])
        path = "/api/rooms/cafe/backlog/" + quote(item_id, safe="")
        status, data = self.call("POST", path, {"status": "open", "due": "2026-12-31"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["item"]["due"], "2026-12-31")
        status, data = self.call("POST", path, {"status": "done"})
        self.assertEqual(status, 200, data)
        self.assertEqual(_backlog_items(self.sb, rid)[0]["due"], "2026-12-31")

    def test_unknown_element_is_still_404(self):
        rid = self.sb.ids["done_modern"]["root_id"]
        _register(self.sb, rid)
        status, data = self.call("POST", "/api/rooms/cafe/backlog/" + quote(rid + ":ns_999", safe=""),
                                 {"status": "done"})
        self.assertEqual(status, 404, data)
        self.assertIn("не найден", (data or {}).get("message_ru") or (data or {}).get("error", {}).get("message", "")
                      or json.dumps(data, ensure_ascii=False))
        self.assertEqual(_backlog_items(self.sb, rid), [])

    def test_foreign_root_is_not_touched(self):
        rid = self.sb.ids["done_modern"]["root_id"]
        other = self.sb.ids["done_legacy"]["root_id"]
        _register(self.sb, rid)
        # чужой root_id с локальным id этого шага — создавать нельзя
        status, _data = self.call("POST", "/api/rooms/cafe/backlog/" + quote(other + ":ns_1", safe=""),
                                  {"status": "done"})
        self.assertEqual(status, 404)
        self.assertEqual(_backlog_items(self.sb, other), [])

    def test_ask_from_unaccepted_step_works(self):
        rid = self.sb.ids["done_modern"]["root_id"]
        _register(self.sb, rid)
        detail = _detail(self.sb, rid)
        item_id = "%s:%s" % (rid, detail["next_steps"][1]["id"])
        status, data = self.call("POST", "/api/rooms/cafe/backlog/" + quote(item_id, safe="") + "/ask",
                                 {})
        self.assertEqual(status, 201, data)
        self.assertTrue(data.get("root_id"))
        status, view = self.call("GET", "/api/rooms/cafe/backlog")
        got = next((i for i in view["items"] if i["id"] == item_id), None)
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "asked")
        self.assertEqual(got["asked_request"], data["root_id"])

    # --- сценарий 2: ensure-приём при открытии готового запроса --------------
    def test_opening_done_request_ingests_it(self):
        rid = self.sb.ids["done_legacy"]["root_id"]
        _register(self.sb, rid)
        self.assertEqual(_backlog_items(self.sb, rid), [])

        status, page = self.call("GET", "/api/requests/" + quote(rid, safe=""))
        self.assertEqual(status, 200)
        self.assertTrue(page.get("next_steps"))

        items = _backlog_items(self.sb, rid)
        self.assertTrue(items, "после открытия запроса шаги не попали в бэклог")
        self.assertTrue(all(i["id"].startswith(rid + ":") for i in items))


if __name__ == "__main__":
    unittest.main()
