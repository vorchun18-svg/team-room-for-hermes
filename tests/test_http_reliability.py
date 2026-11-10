"""HTTP-надёжность: строгая валидация (D7), идемпотентность (D8), лимиты (D11),
секреты в журналах (D13), health (D15) и warning о битом файле (D5)."""
from __future__ import annotations

import dataclasses
import json
import socket
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import http.client  # noqa: E402

from server import app as app_mod                  # noqa: E402
from server import engine_db, engine_read, engine_write, http_util, registry, rooms  # noqa: E402
from server import statefiles as sf                # noqa: E402
from tests.helpers import Sandbox                  # noqa: E402

ROOT_ID = "t_dd0c0001"


class AppCase(unittest.TestCase):
    """Реальный App в песочнице; пишущая функция CLI заменена заглушкой (никаких агентов)."""

    sandbox: Sandbox

    @classmethod
    def setUpClass(cls):
        cls.sandbox = Sandbox()
        cls.sb = cls.sandbox.__enter__()
        cls.cfg = cls.sb.cfg
        rooms.seed_defaults(cls.cfg)
        cls.created: list = []
        cls._orig_create = engine_write.create_request

        def fake_create(cfg, room, slug, title, body):
            cls.created.append({"room": (room or {}).get("id"), "slug": slug, "title": title})
            return {"root_id": ROOT_ID, "warning": None}

        engine_write.create_request = fake_create
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        engine_write.create_request = cls._orig_create
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.sandbox.__exit__(None, None, None)

    # --- клиент ---
    def call(self, method, path, body=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=20)
        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        info = {"status": resp.status, "headers": dict(resp.getheaders()), "raw": raw}
        ctype = resp.getheader("Content-Type") or ""
        if "json" in ctype and raw:
            info["json"] = json.loads(raw.decode("utf-8"))
        conn.close()
        return info

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)

    def patch(self, path, body, **kw):
        return self.call("PATCH", path, body, **kw)


class TestValidation(AppCase):
    def test_critic_must_be_real_bool(self):
        """D7: `critic: "false"` раньше включал критика (bool(str) == True)."""
        before = len(self.created)
        res = self.post("/api/rooms/cafe/requests", {"text": "проверка", "critic": "false"})
        self.assertEqual(res["status"], 400, res.get("json"))
        self.assertEqual(res["json"]["error"]["detail"]["field"], "critic")
        for bad in ("no", 0, 1, []):
            res = self.post("/api/rooms/cafe/requests", {"text": "проверка", "critic": bad})
            self.assertEqual(res["status"], 400, bad)
        self.assertEqual(len(self.created), before)

    def test_real_bool_is_accepted(self):
        res = self.post("/api/rooms/cafe/requests",
                        {"text": "идемпотентность %s" % uuid.uuid4().hex, "critic": False})
        self.assertEqual(res["status"], 201, res.get("json"))
        rec = registry.get(self.cfg, "cafe", ROOT_ID)
        self.assertIsNotNone(rec)
        self.assertFalse(rec["critic"])

    def test_room_fields_are_validated(self):
        """D7: board/icon/context_files/max_rounds — не произвольные значения."""
        base = {"title": "Комната", "orchestrator": "biz-director",
                "members": [{"profile": "biz-lawyer"}]}
        for patch in ({"board": "../../escape-board"}, {"icon": [1, 2]},
                      {"context_files": "abc"}, {"critic_pass": "no"},
                      {"max_rounds": 99}, {"max_rounds": "abc"}):
            body = dict(base, **patch)
            res = self.post("/api/rooms", body)
            self.assertEqual(res["status"], 400, (patch, res.get("json")))

    def test_patch_rejects_bad_fields_and_unknown_profile(self):
        profiles = self.get("/api/profiles")
        roster = {item.get("profile") for item in (profiles.get("json") or {}).get("items", [])}
        if not roster:
            self.skipTest("нет профилей Hermes — ростер пуст")
        for patch in ({"icon": [1, 2]}, {"context_files": "abc"}, {"critic_pass": "no"},
                      {"max_rounds": "abc"}, {"max_rounds": 99}):
            res = self.patch("/api/rooms/cafe", patch)
            self.assertEqual(res["status"], 400, (patch, res.get("json")))
        res = self.patch("/api/rooms/cafe", {"orchestrator": "zzz-nonexistent"})
        self.assertEqual(res["status"], 409, res.get("json"))
        res = self.patch("/api/rooms/cafe", {"critic": "zzz-nonexistent"})
        self.assertEqual(res["status"], 409, res.get("json"))

    def test_create_rejects_unknown_profile(self):
        profiles = self.get("/api/profiles")
        roster = {item.get("profile") for item in (profiles.get("json") or {}).get("items", [])}
        if not roster:
            self.skipTest("нет профилей Hermes — ростер пуст")
        res = self.post("/api/rooms", {"title": "X", "orchestrator": "zzz-nonexistent",
                                       "members": [{"profile": "zzz-nonexistent"}]})
        self.assertEqual(res["status"], 400, res.get("json"))
        self.assertEqual(res["json"]["error"]["detail"]["field"], "orchestrator")
        self.assertTrue(res["json"]["error"]["detail"]["available"])


class TestIdempotency(AppCase):
    def test_same_text_twice_creates_one_card(self):
        """D8: два одинаковых POST давали 2 карточки и 2 декомпозиции."""
        before = len(self.created)
        text = "двойная отправка %s" % uuid.uuid4().hex
        first = self.post("/api/rooms/cafe/requests", {"text": text})
        second = self.post("/api/rooms/cafe/requests", {"text": text})
        self.assertEqual(first["status"], 201, first.get("json"))
        self.assertEqual(second["status"], 200, second.get("json"))
        self.assertTrue(second["json"]["duplicate"])
        self.assertFalse(first["json"].get("duplicate", False))
        self.assertEqual(first["json"]["root_id"], second["json"]["root_id"])
        self.assertEqual(len(self.created) - before, 1)

    def test_client_id_dedup_even_with_different_text(self):
        before = len(self.created)
        client_id = str(uuid.uuid4())
        first = self.post("/api/rooms/cafe/requests",
                          {"text": "первый текст %s" % uuid.uuid4().hex, "client_id": client_id})
        second = self.post("/api/rooms/cafe/requests",
                           {"text": "второй текст %s" % uuid.uuid4().hex, "client_id": client_id})
        self.assertEqual(first["status"], 201, first.get("json"))
        self.assertEqual(second["status"], 200, second.get("json"))
        self.assertTrue(second["json"]["duplicate"])
        self.assertEqual(len(self.created) - before, 1)

    def test_bad_client_id_is_400(self):
        for bad in ([], {"x": 1}, "a" * 200, "со пробелом"):
            res = self.post("/api/rooms/cafe/requests",
                            {"text": "текст", "client_id": bad})
            self.assertEqual(res["status"], 400, (bad, res.get("json")))

    def test_201_with_degraded_summary_when_read_fails(self):
        """D8: сбой чтения ПОСЛЕ create давал 500 и провоцировал повтор."""
        before = len(self.created)
        orig = engine_read.request_summary

        def boom(*args, **kwargs):
            raise engine_db.EngineError("db_busy", "База канбана занята другим процессом.")

        engine_read.request_summary = boom
        try:
            res = self.post("/api/rooms/cafe/requests",
                            {"text": "сбой чтения %s" % uuid.uuid4().hex})
        finally:
            engine_read.request_summary = orig
        self.assertEqual(res["status"], 201, res.get("json"))
        body = res["json"]
        self.assertTrue(body["request"]["degraded"])
        self.assertEqual(body["warning"]["code"], "summary_unavailable")
        self.assertEqual(len(self.created) - before, 1)      # карточка уже создана


class TestTransportLimits(AppCase):
    def test_handler_timeout_is_set(self):
        """D11: Handler.timeout=None держал поток на медленном клиенте бесконечно."""
        self.assertEqual(http_util.Handler.timeout, 30)

    def test_chunked_body_is_rejected_and_connection_closed(self):
        """D11: chunked игнорировался, и остаток тела становился «следующим запросом»."""
        payload = (b"POST /api/rooms/cafe/requests HTTP/1.1\r\n"
                   b"Host: 127.0.0.1:%d\r\n"
                   b"Transfer-Encoding: chunked\r\n"
                   b"Content-Type: application/json\r\n\r\n"
                   b"1e\r\n{\"text\": \"chunked\"}\r\n0\r\n\r\n"
                   b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % (self.port, self.port))
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(payload)
            chunks = []
            while True:
                try:
                    part = sock.recv(4096)
                except socket.timeout:
                    break
                if not part:
                    break
                chunks.append(part)
        finally:
            sock.close()
        raw = b"".join(chunks)
        self.assertEqual(raw.count(b"HTTP/1.1"), 1, raw[:400])
        self.assertIn(b" 411 ", raw.split(b"\r\n")[0] + b" ")
        self.assertIn("Transfer-Encoding".encode("ascii"), raw)

    def test_thread_cap_returns_503(self):
        """D11: 400 соединений → 402 потока; теперь сверх лимита сразу 503."""
        saved = app_mod.TeamRoomServer.max_threads
        app_mod.TeamRoomServer.max_threads = 1
        server = None
        holder = None
        try:
            server = app_mod.create_app(self.cfg, start_watcher=False, port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            holder = socket.create_connection(("127.0.0.1", port), timeout=10)
            holder.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % port)  # без пустой строки
            time.sleep(0.2)                       # поток занят ожиданием заголовков
            busy = self.get("/api/health", port=port)
            self.assertEqual(busy["status"], 503, busy.get("raw", b"")[:200])
            self.assertEqual(busy["json"]["error"]["code"], "server_busy")
        finally:
            if holder is not None:
                holder.close()
            if server is not None:
                server.shutdown()
                server.server_close()
            app_mod.TeamRoomServer.max_threads = saved

    def test_deep_json_is_400(self):
        deep = ("[" * 5000) + ("]" * 5000)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        body = ('{"text": %s}' % deep).encode("utf-8")
        conn.request("POST", "/api/rooms/cafe/requests", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        self.assertEqual(resp.status, 400, raw[:200])


class TestSecrets(AppCase):
    def test_mask_secrets_covers_patterns(self):
        """D13: password/token/secret/api-key/Bearer больше не попадают в журнал."""
        cases = {
            "create LOG password=hunter2": "hunter2",
            "token=abc123xyz": "abc123xyz",
            "secret: supersecret": "supersecret",
            "api_key=key-123": "key-123",
            "api-key=key-456": "key-456",
            "Authorization: Bearer sk-ant-0123456789": "sk-ant-0123456789",
        }
        for text, secret in cases.items():
            masked = http_util.mask_secrets(text)
            self.assertNotIn(secret, masked, text)
            self.assertIn("***", masked, text)
        self.assertEqual(http_util.mask_secrets("обычная строка"), "обычная строка")

    def test_logs_endpoint_masks_and_file_denies_logs(self):
        log = Path(self.cfg.log_dir) / "engine.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        line = ("2026-10-28 08:00:00 | 5ms | code=0 | kanban create LOG password=hunter2 "
                "token=abc123 Bearer sk-ant-0123456789\n")
        log.write_text(line, encoding="utf-8")

        res = self.get("/api/logs?name=engine.log&tail=10")
        self.assertEqual(res["status"], 200, res.get("json"))
        text = res["raw"].decode("utf-8")
        self.assertIn("text/plain", res["headers"].get("Content-Type", ""))
        self.assertIn("password=***", text)
        self.assertIn("Bearer ***", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc123", text)

        denied = self.get("/api/file?path=" + str(log).replace("\\", "/"))
        self.assertEqual(denied["status"], 403, denied.get("json"))
        self.assertEqual(denied["json"]["error"]["detail"]["reason"], "blacklisted")

        other = Path(self.cfg.data_dir) / "notes.log"
        other.write_text("password=hunter2\n", encoding="utf-8")
        denied = self.get("/api/file?path=" + str(other).replace("\\", "/"))
        self.assertEqual(denied["status"], 403, denied.get("json"))

    def test_logs_endpoint_validates_name(self):
        for qs in ("name=../rooms.json", "name=..%2Frooms.json", "name=" + "x" * 80):
            res = self.get("/api/logs?" + qs)
            self.assertIn(res["status"], (400, 403), (qs, res.get("json")))
        res = self.get("/api/logs?name=ghost.log")
        self.assertEqual(res["status"], 404)
        res = self.get("/api/logs?tail=abc")
        self.assertEqual(res["status"], 400)


class TestHealth(AppCase):
    def test_missing_cli_is_reported(self):
        """D15: cli.ok был true при несуществующем hermes."""
        cfg = dataclasses.replace(self.cfg, hermes_cli="C:/nope/hermes.exe")
        server = app_mod.create_app(cfg, start_watcher=False, port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            res = self.get("/api/health", port=port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        body = res["json"]
        self.assertEqual(res["status"], 200)
        self.assertFalse(body["engine"]["cli"]["ok"], body["engine"]["cli"])
        codes = [w["code"] for w in body["warnings"]]
        self.assertIn("cli_missing", codes)
        self.assertFalse(body["ok"])

    def test_write_probe_is_unique_and_data_stays_writable(self):
        """D15: общий .write-probe давал ложное data.writable=false при параллельных опросах."""
        results = []

        def poll():
            results.append(self.get("/api/health"))

        threads = [threading.Thread(target=poll) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 8)
        for res in results:
            self.assertEqual(res["status"], 200)
            self.assertTrue(res["json"]["data"]["writable"], res.get("json"))
        leftovers = list(Path(self.cfg.data_dir).glob(".write-probe*"))
        self.assertEqual(leftovers, [])


class TestCorruptStateWarning(unittest.TestCase):
    """D5: битый requests.json → копия .corrupt-<ts> и warning в /api/health."""

    def test_health_reports_backup(self):
        with Sandbox() as sb:
            rooms.seed_defaults(sb.cfg)
            reg = Path(sb.cfg.state_dir) / "cafe" / "requests.json"
            reg.parent.mkdir(parents=True, exist_ok=True)
            broken = '{"room": "cafe", "requests": ['
            reg.write_text(broken, encoding="utf-8")
            server = app_mod.create_app(sb.cfg, start_watcher=False, port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                conn.request("GET", "/api/health")
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            warnings = [w for w in body["warnings"] if w["code"] == "state_corrupt"]
            self.assertTrue(warnings, body["warnings"])
            warning = warnings[0]
            self.assertIn("копия", warning["message"])
            backup = warning["detail"]["backup"]
            self.assertTrue(backup)
            self.assertTrue(Path(backup).is_file())
            self.assertEqual(Path(backup).read_text(encoding="utf-8"), broken)
            self.assertTrue(any(p["backup"] == backup for p in sf.problems()))


if __name__ == "__main__":
    unittest.main()
