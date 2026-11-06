# -*- coding: utf-8 -*-
"""Тесты мок-сервера «Комнаты команды» (unittest, только stdlib).

Запуск: python tests/test_mock_server.py
Сервер поднимается на свободном порту со своим временным web/ и ускоренной
эволюцией запросов (phase_scale), чтобы тест не ждал реальные 35 секунд.
Контракт v2: версии ответа, критик, кандидаты на привязку, режим только auto.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from unittest import mock as umock
from urllib.parse import quote

warnings.filterwarnings("ignore", category=ResourceWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mock_server as mock  # noqa: E402
import mock_stt  # noqa: E402


class MockServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = tempfile.mkdtemp(prefix="teamroom-web-")
        with open(os.path.join(cls.web, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<!doctype html><title>mock-index</title><h1>Комната команды</h1>")
        os.makedirs(os.path.join(cls.web, "js"), exist_ok=True)
        with open(os.path.join(cls.web, "js", "app.js"), "w", encoding="utf-8") as fh:
            fh.write("export const ok = true;\n")
        cls.fixtures = os.path.join(ROOT, "fixtures")
        cls.files = tempfile.mkdtemp(prefix="teamroom-files-")
        with open(os.path.join(cls.files, "health.json"), "w", encoding="utf-8") as fh:
            json.dump({"app": {"name": "team-room"}}, fh)
        with open(os.path.join(cls.files, "pic.svg"), "w", encoding="utf-8") as fh:
            fh.write('<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>')
        with open(os.path.join(cls.files, "смета.docx"), "wb") as fh:
            fh.write(b"PK\x03\x04demo")

    def setUp(self):
        mock_stt.reset()
        self.addCleanup(mock_stt.reset)
        self.server = mock.create_server(0, self.web, self.fixtures, phase_scale=0.03,
                                         extra_roots=[self.files])
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # --- STAB-3: занятый порт не должен «тихо двоиться» --------------------
    def test_busy_port_is_refused(self):
        """Второй мок на занятом порту — понятная ошибка, а не двойная привязка.

        На Windows `allow_reuse_address` разрешает второму сокету встать на тот же
        порт: оба слушают и Windows отдаёт соединение любому из них. В e2e это
        давало «Комната с таким id уже есть» в чистом прогоне (браузер попадал в
        прежний мок). Теперь create_server проверяет порт и падает.
        """
        with self.assertRaises(RuntimeError) as ctx:
            mock.create_server(self.port, self.web, self.fixtures, phase_scale=0.03,
                               extra_roots=[self.files])
        message = str(ctx.exception)
        self.assertIn("уже занят", message)
        self.assertIn(str(self.port), message)
        self.assertTrue(mock.port_in_use("127.0.0.1", self.port))
        self.assertFalse(mock.port_in_use("127.0.0.1", 0))

    # --- помощники -------------------------------------------------------
    def call(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, path, method="GET", body=None, headers=None):
        status, head, raw = self.call(path, method, body, headers)
        return status, head, json.loads(raw) if raw else None

    def raw_http(self, method, path, headers):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        lines = ["%s %s HTTP/1.1" % (method, path)] + ["%s: %s" % (k, v) for k, v in headers.items()]
        conn.sendall(("\r\n".join(lines) + "\r\nConnection: close\r\n\r\n").encode("utf-8"))
        chunks = []
        while True:
            part = conn.recv(8192)
            if not part:
                break
            chunks.append(part)
        conn.close()
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), head, body

    def detail(self, rid):
        status, _, data = self.json("/api/requests/" + rid)
        self.assertEqual(status, 200)
        return data

    def wait_status(self, rid, timeout=20.0, step=0.02):
        """Дождаться done, попутно собрав последовательность статусов."""
        seen, last, deadline = [], None, time.time() + timeout
        while time.time() < deadline:
            data = self.detail(rid)
            if data["status"] != last:
                seen.append(data["status"])
                last = data["status"]
            if data["status"] == "done":
                return seen, data
            time.sleep(step)
        self.fail("запрос не завершился: %s" % seen)

    # --- чтение ----------------------------------------------------------
    def test_health_and_config(self):
        status, _, data = self.json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["app"]["name"], "team-room")
        self.assertEqual(data["app"]["port"], self.port)
        self.assertTrue(data["engine"]["schema_ok"])
        self.assertIs(data["engine"]["mock"], True)      # фронт показывает плашку «ДЕМО»
        status, _, cfg = self.json("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("allowed_roots", cfg)
        self.assertEqual(cfg["limits"]["request_text_max"], 1000000)
        self.assertEqual(cfg["limits"]["body_bytes_max"], 8388608)
        self.assertEqual(cfg["limits"]["note_text_max"], 200000)

    def test_rooms_and_room_detail(self):
        status, _, data = self.json("/api/rooms")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in data["rooms"]], ["cafe", "fn"])
        cafe = data["rooms"][0]
        self.assertEqual(cafe["critic"], "biz-critic")
        self.assertEqual(cafe["max_rounds"], 1)
        self.assertEqual(cafe["phase"]["id"], "legal")
        fn = data["rooms"][1]
        self.assertEqual(fn["orchestrator"], "default")
        self.assertIsNone(fn["critic"])
        self.assertEqual(fn["members_count"], 3)
        status, _, room = self.json("/api/rooms/cafe")
        self.assertEqual(status, 200)
        self.assertEqual(room["tenant"], "room:cafe")
        self.assertEqual(len(room["members"]), 7)
        self.assertTrue(room["seeded"])
        self.assertEqual(room["max_rounds"], 1)
        status, _, fn_room = self.json("/api/rooms/fn")
        self.assertEqual([m["profile"] for m in fn_room["members"]], ["lawyer", "coder", "surveyor"])

    def test_requests_list(self):
        status, _, data = self.json("/api/rooms/cafe/requests?limit=3")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 3)
        self.assertTrue(data["has_more"])
        self.assertEqual([i["created_at"] for i in data["items"]],
                         sorted([i["created_at"] for i in data["items"]], reverse=True))
        status, _, filtered = self.json("/api/rooms/cafe/requests?status=needs_you")
        # W17-S: в демо-данных появился ещё один запрос, ждущий ответа (свод с вопросами),
        # поэтому проверяем сам фильтр, а не число запросов в демо.
        self.assertTrue(filtered["items"])
        self.assertTrue(all(i["status"] == "needs_you" for i in filtered["items"]))
        status, _, bad = self.json("/api/rooms/cafe/requests?status=nope")
        self.assertEqual(status, 400)
        self.assertEqual(bad["error"]["code"], "bad_request")
        self.assertTrue(all(i["mode"] == "auto" for i in data["items"]))

    def test_fn_room_endpoints(self):
        status, _, data = self.json("/api/rooms/fn/requests")
        self.assertEqual(status, 200)
        self.assertEqual([i["id"] for i in data["items"]], ["t_4e91c7b2"])
        status, _, team = self.json("/api/rooms/fn/team")
        self.assertEqual(status, 200)
        self.assertEqual([m["profile"] for m in team["members"]], ["default", "lawyer", "coder", "surveyor"])
        self.assertEqual(team["members"][0]["role"], "orchestrator")
        status, _, cand = self.json("/api/rooms/fn/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(cand["count"], 0)
        # W17-R §1 (B-8): у комнаты без своих этапов полосы этапов нет —
        # в том числе в демо (раньше демо выдумывало шесть чужих этапов).
        status, _, roadmap = self.json("/api/rooms/fn/roadmap")
        self.assertEqual(roadmap["phases"], [])
        self.assertIsNone(roadmap["current"])
        status, _, cafe_map = self.json("/api/rooms/cafe/roadmap")
        self.assertEqual(len(cafe_map["phases"]), 6, "у Лето свои этапы")
        self.assertEqual(cafe_map["current"]["id"], "legal")

    def test_done_request_v2_contract(self):
        d = self.detail("t_a1b2c3d4")
        self.assertEqual(d["status"], "done")
        syn = d["synthesis"]
        self.assertEqual(syn["state"], "ready")
        self.assertEqual(syn["version"], 2)
        self.assertEqual([h["kind"] for h in syn["history"]], ["initial", "revision"])
        self.assertEqual([h["verdict"] for h in syn["history"]], ["revise", None])
        self.assertEqual(syn["history"][0]["task_id"], "t_a1b2c3d4")
        self.assertEqual(syn["history"][1]["task_id"], "t_1000a006")
        self.assertIn("| Этап | Статус |", syn["text_md"])
        self.assertNotIn("MOQ не подтверждены", syn["text_md"])

        critic = d["critic"]
        self.assertTrue(critic["enabled"])
        self.assertEqual(critic["state"], "revised")
        self.assertEqual(critic["verdict"], "revise")
        self.assertEqual((critic["round"], critic["max_rounds"]), (1, 1))
        self.assertEqual(critic["task_id"], "t_1000a005")
        self.assertGreaterEqual(len(critic["issues"]), 2)
        self.assertTrue(all({"where", "problem", "fix"} <= set(i) for i in critic["issues"]))

        roles = [n["role"] for n in d["tree"]]
        self.assertEqual(roles.count("critic"), 1)
        self.assertEqual(roles.count("revision"), 1)
        self.assertEqual(d["tree"][0]["role"], "root")
        self.assertFalse(any(n["outside_room"] for n in d["tree"]))
        self.assertEqual(d["progress"], {"done": 6, "total": 6, "percent": 100})
        self.assertEqual(d["duration_s"], d["finished_at"] - d["started_at"])
        self.assertEqual(d["duration_s"], 495)
        self.assertEqual(len(d["next_steps"]), 5)
        self.assertEqual(len(d["stage"]["phases"]), 6)

    def test_critic_approved_single_version(self):
        d = self.detail("t_c4d2e118")
        self.assertEqual(d["status"], "done")
        self.assertEqual(d["synthesis"]["version"], 1)
        self.assertEqual(len(d["synthesis"]["history"]), 1)
        self.assertEqual(d["synthesis"]["history"][0]["verdict"], "approve")
        self.assertEqual(d["critic"]["state"], "approved")
        self.assertEqual(d["critic"]["verdict"], "approve")
        self.assertEqual(d["critic"]["issues"], [])
        self.assertEqual([n["role"] for n in d["tree"]].count("revision"), 0)

    def test_reviewing_has_ready_v1(self):
        d = self.detail("t_7d21b8c4")
        self.assertEqual(d["status"], "reviewing")
        self.assertEqual(d["synthesis"]["state"], "ready")
        self.assertEqual(d["synthesis"]["version"], 1)
        self.assertEqual(d["synthesis"]["history"][0]["verdict"], None)
        self.assertEqual(d["critic"]["state"], "reviewing")
        self.assertIsNone(d["critic"]["verdict"])
        self.assertEqual(d["tree"][-1]["role"], "critic")
        self.assertEqual(d["tree"][-1]["status"], "running")
        self.assertIsNone(d["finished_at"])

    def test_legacy_request_normalized(self):
        d = self.detail("t_605613e0")
        self.assertTrue(d["synthesis"]["legacy"])
        self.assertIn("legacy_metadata", [w["code"] for w in d["warnings"]])
        self.assertEqual(d["synthesis"]["version"], 1)
        self.assertEqual(len(d["synthesis"]["history"]), 1)
        self.assertFalse(d["critic"]["enabled"])
        self.assertEqual(d["critic"]["state"], "off")
        self.assertEqual(len(d["next_steps"]), 5)
        self.assertTrue(all(s["owner"] is None and s["state"] == "open" for s in d["next_steps"]))
        self.assertEqual([p["status"] for p in d["stage"]["phases"]],
                         ["done", "blocked", "blocked", "active", "next", "next"])
        self.assertEqual(d["stage"]["current"], "legal")
        self.assertEqual(d["report"], {"available": False, "kind": None, "path": None, "title": None})
        self.assertEqual([n["role"] for n in d["tree"]].count("synthesizer"), 1)
        self.assertTrue(all("outside_room" in n for n in d["tree"]))

    def test_outside_room_warning(self):
        d = self.detail("t_4e91c7b2")
        self.assertEqual(d["room"], "fn")
        self.assertEqual(d["critic"]["state"], "off")
        self.assertFalse(d["critic"]["enabled"])
        codes = [w["code"] for w in d["warnings"]]
        self.assertIn("outside_room", codes)
        warning = next(w for w in d["warnings"] if w["code"] == "outside_room")
        self.assertEqual(warning["detail"]["task_id"], "t_4e91c7b4")
        node = next(n for n in d["tree"] if n["id"] == "t_4e91c7b4")
        self.assertTrue(node["outside_room"])
        self.assertEqual(node["assignee"], "analyst")
        self.assertEqual(d["synthesis"]["version"], 1)
        self.assertEqual(len(d["synthesis"]["history"]), 1)

    def test_candidates_and_adopt(self):
        status, _, data = self.json("/api/rooms/cafe/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        self.assertEqual([c["id"] for c in data["items"]], ["t_71af0c5d", "t_63d0be21"])
        self.assertTrue(all({"id", "title", "status", "decomposed_at", "children"} <= set(c) for c in data["items"]))
        status, _, adopted = self.json("/api/adopt", "POST", {"room": "cafe", "task_id": "t_71af0c5d"})
        self.assertEqual(status, 200)
        self.assertEqual(adopted["request"]["id"], "t_71af0c5d")
        self.assertTrue(adopted["adopted"])
        self.assertFalse(adopted["already"])
        self.assertEqual(adopted["warning"]["code"], "legacy_metadata")
        status, _, again = self.json("/api/adopt", "POST", {"room": "cafe", "task_id": "t_71af0c5d"})
        self.assertEqual(status, 200)
        self.assertTrue(again["already"])
        status, _, after = self.json("/api/rooms/cafe/candidates")
        self.assertEqual(after["count"], 1)
        status, _, err = self.json("/api/adopt", "POST", {"room": "cafe", "task_id": "t_63d0be21"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["detail"]["tenant"], "room:fn")

    def test_etag_returns_304(self):
        status, head, _ = self.call("/api/requests/t_a1b2c3d4")
        self.assertEqual(status, 200)
        etag = head["ETag"]
        self.assertTrue(etag.startswith('W/"'))
        status, head2, body = self.call("/api/requests/t_a1b2c3d4", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(head2["ETag"], etag)

    def test_static_and_fallback(self):
        status, head, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"mock-index", body)
        status, head, body = self.call("/rooms/cafe/request/t_a1b2c3d4")
        self.assertEqual(status, 200)
        self.assertIn(b"mock-index", body)
        status, head, body = self.call("/js/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", head["Content-Type"])
        status, _, body = self.call("/missing.css")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    # --- создание и эволюция --------------------------------------------
    def test_create_request_evolves_with_critic(self):
        status, head, created = self.json("/api/rooms/cafe/requests", "POST",
                                          {"text": "Проверь упаковку моти", "critic": True})
        self.assertEqual(status, 201)
        self.assertIn("Location", head)
        rid = created["root_id"]
        self.assertRegex(rid, r"^t_[0-9a-f]{8}$")
        self.assertEqual(created["request"]["status"], "queued")
        self.assertEqual(created["request"]["mode"], "auto")
        self.assertEqual(self.detail(rid)["text"], "Проверь упаковку моти")

        seen, d = self.wait_status(rid)
        self.assertEqual(seen, ["queued", "working", "reviewing", "synthesizing", "done"])
        self.assertEqual(d["synthesis"]["state"], "ready")
        self.assertEqual(d["synthesis"]["version"], 1)
        self.assertEqual([h["kind"] for h in d["synthesis"]["history"]], ["initial"])
        self.assertEqual(d["synthesis"]["history"][0]["verdict"], "approve")
        self.assertEqual(d["critic"]["state"], "approved")
        self.assertEqual(d["critic"]["verdict"], "approve")
        self.assertEqual([n["role"] for n in d["tree"]].count("revision"), 0)
        self.assertEqual([n["role"] for n in d["tree"]].count("critic"), 1)
        self.assertEqual(len([n for n in d["tree"] if n["role"] == "worker"]), 4)
        self.assertTrue(d["needs_you"])                 # в конце остаётся вопрос Михаилу
        self.assertEqual(len(d["open_questions"]), 1)
        self.assertEqual(d["progress"]["percent"], 100)
        self.assertEqual(len(d["tree"]), d["progress"]["total"] + 1)
        self.assertTrue(d["report"]["available"])
        self.assertTrue(d["report"]["key"].startswith("demo-"))
        listed = self.json("/api/rooms/cafe/requests")[2]
        self.assertEqual(listed["items"][0]["id"], rid)

    def test_create_request_without_critic(self):
        status, _, created = self.json("/api/rooms/cafe/requests", "POST",
                                        {"text": "Без критика: только факты", "critic": False})
        self.assertEqual(status, 201)
        rid = created["root_id"]
        seen, d = self.wait_status(rid)
        self.assertEqual(seen, ["queued", "working", "synthesizing", "done"])
        self.assertEqual(d["synthesis"]["version"], 1)
        self.assertFalse(d["critic"]["enabled"])
        self.assertEqual(d["critic"]["state"], "off")
        self.assertFalse(any(n["role"] in ("critic", "revision") for n in d["tree"]))

    def test_create_request_validation(self):
        status, _, data = self.json("/api/rooms/cafe/requests", "POST", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["detail"]["field"], "text")
        status, _, data = self.json("/api/rooms/cafe/requests", "POST", {"text": "x", "mode": "swarm"})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "bad_request")
        self.assertIn("не поддерживается", data["error"]["message"])
        self.assertEqual(data["error"]["detail"]["allowed"], ["auto"])
        status, _, data = self.json("/api/rooms/cafe/requests", "POST", {"text": "x", "mode": "fast"})
        self.assertEqual(status, 400)
        status, _, data = self.json("/api/rooms/cafe/requests", "POST", {"text": "x", "critic": "yes"})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["detail"]["field"], "critic")
        status, _, data = self.json("/api/rooms/nope/requests", "POST", {"text": "x"})
        self.assertEqual(status, 404)

    # --- действия ---------------------------------------------------------
    def test_comment_action_persists(self):
        status, _, data = self.json("/api/requests/t_a1b2c3d4/actions", "POST",
                                    {"action": "comment", "text": "Нужен ещё один вариант упаковки"})
        self.assertEqual(status, 200)
        self.assertEqual(data["comment"]["author"], "mikhail")
        d = self.detail("t_a1b2c3d4")
        self.assertEqual(d["tree"][0]["comments"][-1]["text"], "Нужен ещё один вариант упаковки")
        self.assertIn("Нужен ещё один вариант", d["timeline"][-1]["text"])

    def test_comment_has_int_id(self):
        """API §6.3: comment = {id:int, author, text, at}; id растёт (как task_comments.id)."""
        ids = []
        for text in ("первый", "второй"):
            status, _, data = self.json("/api/requests/t_a1b2c3d4/actions", "POST",
                                        {"action": "comment", "text": text})
            self.assertEqual(status, 200)
            self.assertIsInstance(data["comment"]["id"], int)
            ids.append(data["comment"]["id"])
        self.assertLess(ids[0], ids[1])
        self.assertEqual(sorted(data["comment"]), ["at", "author", "id", "text"])

    def test_done_with_open_question_is_done_plus_needs_you(self):
        """Решение «done + needs_you»: статус done, флаг needs_you, счётчик комнаты считает такой запрос."""
        status, _, feed = self.json("/api/rooms/cafe/requests?limit=100")
        by_id = {i["id"]: i for i in feed["items"]}
        for rid, waiting in (("t_a1b2c3d4", True), ("t_605613e0", True), ("t_c4d2e118", False)):
            self.assertEqual(by_id[rid]["status"], "done", rid)
            self.assertEqual(by_id[rid]["needs_you"], waiting, rid)
            self.assertEqual(self.detail(rid)["needs_you"], waiting, rid)
        status, _, rooms = self.json("/api/rooms")
        cafe = next(r for r in rooms["rooms"] if r["id"] == "cafe")
        # W17-S: +1 запрос с вопросами (свод с тремя версиями) в демо-данных.
        self.assertEqual(cafe["counters"]["needs_you"], 5)       # 5 запросов с вопросами в демо (W17)
        status, _, data = self.json("/api/requests/t_a1b2c3d4/actions", "POST",
                                    {"action": "answer", "question_id": "q_1", "text": "Да"})
        self.assertEqual(status, 200)
        d = self.detail("t_a1b2c3d4")
        self.assertEqual((d["status"], d["needs_you"]), ("done", False))   # статус не меняется
        status, _, rooms = self.json("/api/rooms")
        self.assertEqual(next(r for r in rooms["rooms"] if r["id"] == "cafe")["counters"]["needs_you"], 4)

    def test_answer_unblocks_and_is_single_use(self):
        status, _, data = self.json("/api/requests/t_5c8e4a02/actions", "POST",
                                    {"action": "answer", "question_id": "q_1", "text": "Подаём 30+43"})
        self.assertEqual(status, 200)
        self.assertTrue(data["question"]["answered"])
        self.assertIn("t_5c8e4a03", data["unblocked"])
        d = self.detail("t_5c8e4a02")
        self.assertEqual(d["status"], "working")
        self.assertEqual(d["open_questions"][0]["answer"], "Подаём 30+43")
        status, _, err = self.json("/api/requests/t_5c8e4a02/actions", "POST",
                                   {"action": "answer", "question_id": "q_1", "text": "ещё раз"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "conflict")

    def test_unknown_action_and_card(self):
        status, _, data = self.json("/api/requests/t_a1b2c3d4/actions", "POST", {"action": "explode"})
        self.assertEqual(status, 400)
        self.assertIn("decompose", data["error"]["detail"]["allowed"])
        self.assertIn("revise", data["error"]["detail"]["allowed"])
        status, _, data = self.json("/api/requests/t_deadbeef/actions", "POST", {"action": "comment", "text": "x"})
        self.assertEqual(status, 404)

    def test_reassign_rework_retry(self):
        status, _, data = self.json("/api/requests/t_9f3c2a10/actions", "POST",
                                    {"action": "reassign", "task_id": "t_9f3c2a13", "profile": "biz-lawyer"})
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["assignee"], "biz-lawyer")
        self.assertFalse(data["task"]["outside_room"])
        status, _, err = self.json("/api/requests/t_9f3c2a10/actions", "POST",
                                   {"action": "reassign", "task_id": "t_9f3c2a13", "profile": "nobody"})
        self.assertEqual(status, 400)
        self.assertIn("biz-lawyer", err["error"]["detail"]["available"])
        status, _, data = self.json("/api/requests/t_3b7e9d11/actions", "POST", {"action": "retry"})
        self.assertEqual(status, 200)
        self.assertEqual(data["retried"], ["t_3b7e9d13"])
        self.assertEqual(self.detail("t_3b7e9d11")["status"], "working")

    def test_decompose_action(self):
        status, _, created = self.json("/api/rooms/cafe/requests", "POST", {"text": "Разложи и выполни"})
        self.assertEqual(status, 201)
        rid = created["root_id"]
        status, _, data = self.json("/api/requests/%s/actions" % rid, "POST", {"action": "decompose"})
        self.assertEqual(status, 200)
        self.assertTrue(data["decomposed"])
        self.assertEqual(data["action"], "decompose")
        self.assertEqual(data["request"]["status"], "working")
        self.assertIn("dispatcher", data)
        status, _, err = self.json("/api/requests/%s/actions" % rid, "POST", {"action": "decompose"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["detail"]["reason"], "already_decomposed")

    def test_revise_action(self):
        status, _, err = self.json("/api/requests/t_2a6f5c93/actions", "POST",
                                   {"action": "revise", "text": "пока рано"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["detail"]["reason"], "synthesis_pending")
        status, _, err = self.json("/api/requests/t_a1b2c3d4/actions", "POST", {"action": "revise", "text": "  "})
        self.assertEqual(status, 400)
        status, _, data = self.json("/api/requests/t_c4d2e118/actions", "POST",
                                    {"action": "revise", "text": "Добавь сроки поставки"})
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["previous_version"], 1)
        self.assertRegex(data["revision_task_id"], r"^t_[0-9a-f]{8}$")
        d = self.detail("t_c4d2e118")
        self.assertEqual(d["status"], "synthesizing")
        self.assertEqual(d["synthesis"]["version"], 2)
        self.assertEqual(d["synthesis"]["history"][-1]["note"], "Добавь сроки поставки")
        self.assertEqual(d["tree"][-1]["role"], "revision")
        self.assertEqual(d["tree"][-1]["status"], "running")

    def test_critique_action(self):
        status, _, data = self.json("/api/requests/t_a1b2c3d4/actions", "POST", {"action": "critique"})
        self.assertEqual(status, 200)
        self.assertRegex(data["critic_task_id"], r"^t_[0-9a-f]{8}$")
        self.assertEqual(data["request"]["status"], "reviewing")
        self.assertEqual(data["state"], "reviewing")
        self.assertEqual(data["round"], 2)
        d = self.detail("t_a1b2c3d4")
        self.assertEqual(d["status"], "reviewing")
        self.assertEqual(d["critic"]["state"], "reviewing")
        self.assertEqual([n["role"] for n in d["tree"]].count("critic"), 2)
        status, _, err = self.json("/api/requests/t_4e91c7b2/actions", "POST", {"action": "critique"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["detail"]["reason"], "no_critic")

    def test_live_revise_after_done(self):
        status, _, created = self.json("/api/rooms/cafe/requests", "POST", {"text": "Доведи до версии 2"})
        rid = created["root_id"]
        _, d = self.wait_status(rid)
        self.assertEqual(d["synthesis"]["version"], 1)
        status, _, data = self.json("/api/requests/%s/actions" % rid, "POST",
                                    {"action": "revise", "text": "Уточни сроки"})
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["previous_version"], 1)
        _, final = self.wait_status(rid)
        self.assertEqual(final["synthesis"]["version"], 2)
        self.assertEqual(len(final["synthesis"]["history"]), 2)
        self.assertEqual(final["synthesis"]["history"][-1]["note"], "Уточни сроки")
        self.assertIn("Что изменилось в версии 2", final["synthesis"]["text_md"])
        self.assertEqual([n["role"] for n in final["tree"]].count("revision"), 1)

    def test_live_demo_report_and_answer(self):
        """Новый запрос — это сценарий: свои карточки, отчёт (таблица/графики/граф) и вопрос Михаилу."""
        text = ("Сравни «Едок» и «Развоз» для первых 100 наборов моти: комиссии, охват, "
                "скорость выплат. Нужна рекомендация.")
        status, _, created = self.json("/api/rooms/cafe/requests", "POST", {"text": text, "critic": True})
        self.assertEqual(status, 201)
        rid = created["root_id"]
        _, d = self.wait_status(rid)
        self.assertTrue(d["title"].startswith("Сравни «Едок» и «Развоз»"))
        self.assertLessEqual(len(d["title"]), 80)
        self.assertEqual(d["tree"][0]["title"], d["title"])
        titles = " ".join(n["title"] for n in d["tree"])
        self.assertIn("Едок", titles)
        self.assertNotIn("Дорожная карта", titles)
        self.assertEqual(len([n for n in d["tree"] if n["role"] == "worker"]), 4)
        self.assertIn("| Канал |", d["synthesis"]["text_md"])
        self.assertIn("Telegram", d["synthesis"]["text_md"])
        self.assertEqual([q["for"] for q in d["open_questions"]], ["mikhail"])
        self.assertIn("Развоз", d["open_questions"][0]["text"])
        self.assertTrue(d["needs_you"])
        counters = next(r for r in self.json("/api/rooms")[2]["rooms"] if r["id"] == "cafe")["counters"]
        self.assertGreaterEqual(counters["needs_you"], 4)

        key = d["report"]["key"]
        status, _, spec = self.json("/api/reports/cafe/" + key)
        self.assertEqual(status, 200)
        kinds = [b["type"] for b in spec["blocks"]]
        self.assertIn("table", kinds)
        self.assertGreaterEqual(kinds.count("chart"), 2)
        self.assertIn("graph", kinds)
        self.assertIn("checklist", kinds)
        self.assertTrue(any(b.get("kind") == "hbar" for b in spec["blocks"] if b["type"] == "chart"))
        report_path = os.path.join(self.files, "live-report.json")
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False)
        result = subprocess.run([sys.executable, os.path.join(ROOT, "reportkit", "validate.py"), report_path],
                                capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
        index = self.json("/api/rooms/cafe/reports")[2]
        self.assertEqual(index["items"][0]["key"], key)
        self.assertEqual(index["items"][0]["request_id"], rid)
        self.assertEqual(index["items"][0]["path"], None)

        status, _, answered = self.json("/api/requests/%s/actions" % rid, "POST",
                                        {"action": "answer", "question_id": "q_1",
                                         "text": "Стартуем с Telegram и двух кофеен"})
        self.assertEqual(status, 200)
        self.assertEqual(answered["request"]["status"], "done")
        self.assertFalse(answered["request"]["needs_you"])
        fresh = self.detail(rid)
        self.assertFalse(fresh["needs_you"])
        self.assertTrue(fresh["open_questions"][0]["answered"])
        self.assertEqual(fresh["open_questions"][0]["answer"], "Стартуем с Telegram и двух кофеен")
        self.assertIn("Ответ Михаила", fresh["timeline"][-1]["text"])
        spec = self.json("/api/reports/cafe/" + key)[2]
        stats = next(b for b in spec["blocks"] if b["type"] == "stats")
        self.assertIn("0", [i["value"] for i in stats["items"] if i["label"] == "Вопросов к Михаилу"])

    def test_live_report_before_synthesis(self):
        status, _, created = self.json("/api/rooms/cafe/requests", "POST", {"text": "Отчёт до готовности"})
        rid = created["root_id"]
        key = "demo-" + rid[2:]
        status, _, data = self.json("/api/reports/cafe/" + key)
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["detail"]["reason"], "synthesis_pending")
        self.assertEqual(self.json("/api/rooms/cafe/reports")[2]["items"][0]["key"] != key, True)

    # --- бэклог -----------------------------------------------------------
    def test_backlog_status_and_ask(self):
        status, _, data = self.json("/api/rooms/cafe/backlog/t_a1b2c3d4:ns_1", "POST", {"status": "done"})
        self.assertEqual(status, 200)
        self.assertEqual(data["item"]["status"], "done")
        self.assertGreaterEqual(data["counts"]["done"], 2)
        status, _, asked = self.json("/api/rooms/cafe/backlog/t_a1b2c3d4:fq_1/ask", "POST", {})
        self.assertEqual(status, 201)
        self.assertEqual(asked["item"]["status"], "asked")
        self.assertRegex(asked["root_id"], r"^t_[0-9a-f]{8}$")
        again = self.json("/api/rooms/cafe/backlog/t_a1b2c3d4:fq_1/ask", "POST", {})[0]
        self.assertEqual(again, 409)
        bad = self.json("/api/rooms/cafe/backlog/t_a1b2c3d4:ns_2", "POST", {"status": "later"})[0]
        self.assertEqual(bad, 400)

    # --- файлы, календарь, экспорт ---------------------------------------
    def test_ics_and_export(self):
        status, head, body = self.call("/api/ics?room=cafe&all=1")
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", head["Content-Type"])
        text = body.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("DTSTART;VALUE=DATE:20261030", text)
        self.assertIn("UID:cafe-", text)
        self.assertEqual(self.call("/api/ics?room=cafe")[0], 400)
        title = self.detail("t_a1b2c3d4")["title"]
        status, head, body = self.call("/api/export/request/t_a1b2c3d4.md")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", head["Content-Type"])
        self.assertIn("# " + title, body.decode("utf-8"))
        status, head, body = self.call("/api/export/request/t_a1b2c3d4.html")
        self.assertIn("<!doctype html>", body.decode("utf-8"))
        self.assertEqual(self.call("/api/export/request/t_2a6f5c93.md")[0], 409)

    def test_file_whitelist(self):
        target = os.path.join(self.files, "health.json")
        status, head, body = self.call("/api/file?path=" + quote(target))
        self.assertEqual(status, 200)
        self.assertIn("application/json", head["Content-Type"])
        self.assertEqual(json.loads(body)["app"]["name"], "team-room")
        self.assertEqual(head["Content-Disposition"], "inline")
        # SVG — с CSP sandbox; office — attachment (API §6.6)
        status, head, _ = self.call("/api/file?path=" + quote(os.path.join(self.files, "pic.svg")))
        self.assertEqual(status, 200)
        self.assertEqual(head["Content-Type"], "image/svg+xml")
        self.assertEqual(head.get("Content-Security-Policy"), "sandbox")
        status, head, _ = self.call("/api/file?path=" + quote(os.path.join(self.files, "смета.docx")))
        self.assertEqual(status, 200)
        self.assertIn("attachment", head["Content-Disposition"])
        # вне белых корней
        status, _, body = self.call("/api/file?path=" + quote("C:/Windows/win.ini"))
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["detail"]["reason"], "outside_roots")
        # исходники/фикстуры мока и .py не отдаются
        for private in (os.path.join(ROOT, "fixtures", "health.json"), os.path.join(ROOT, "server", "config.py")):
            status, _, body = self.call("/api/file?path=" + quote(private))
            self.assertEqual(status, 403, private)
            self.assertEqual(json.loads(body)["error"]["detail"]["reason"], "outside_roots")
        status, _, body = self.call("/api/file?path=" + quote(os.path.join(self.files, "nope.json")))
        self.assertEqual(status, 404)

    # --- безопасность -----------------------------------------------------
    def test_bad_host_rejected(self):
        status, head, body = self.raw_http("GET", "/api/health", {"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn(b"bad_host", body)
        status, _, _ = self.raw_http("GET", "/api/health", {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)

    def test_bad_origin_rejected_on_post(self):
        status, _, data = self.json("/api/rooms/cafe/requests", "POST", {"text": "тест"},
                                    {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["detail"]["reason"], "bad_origin")
        status, _, data = self.json("/api/rooms/cafe/requests", "POST", {"text": "тест с origin"},
                                    {"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 201)

    def test_error_shape(self):
        status, _, data = self.json("/api/requests/t_deadbeef")
        self.assertEqual(status, 404)
        self.assertEqual(set(data["error"]), {"code", "message", "detail"})
        self.assertEqual(data["error"]["code"], "not_found")
        self.assertIsInstance(data["error"]["message"], str)
        status, _, data = self.json("/api/search?q=x")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "bad_request")

    # --- контрактные мелочи (M1, M4, M2, M3, F1) ---------------------------
    def test_critic_shape_same_everywhere(self):
        """F1: critic — объект в деталях и в лентах, один набор ключей."""
        for name in sorted(os.listdir(self.fixtures)):
            if not name.startswith("request_") or not name.endswith(".json"):
                continue
            with open(os.path.join(self.fixtures, name), encoding="utf-8") as fh:
                detail = json.load(fh)
            block = detail.get("critic")
            self.assertIsInstance(block, dict, name)
            self.assertTrue({"enabled", "round", "max_rounds", "state", "verdict", "issues", "task_id"} <= set(block), name)
            self.assertIn(block["state"], mock.CRITIC_STATES, name)
        for name in ("requests_cafe.json", "requests_fn.json"):
            with open(os.path.join(self.fixtures, name), encoding="utf-8") as fh:
                feed = json.load(fh)
            for item in feed["items"]:
                self.assertIsInstance(item["critic"], dict, item["id"])
                self.assertEqual(set(item["critic"]), {"enabled", "state", "verdict", "round"}, item["id"])
                self.assertIn(item["critic"]["state"], mock.CRITIC_STATES, item["id"])
        created = self.json("/api/rooms/cafe/requests", "POST", {"text": "Форма критика в сводке"})[2]
        self.assertEqual(set(created["request"]["critic"]), {"enabled", "state", "verdict", "round"})

    def test_bad_body_types_rejected(self):
        cases = [
            ("/api/rooms", "POST", {"title": {"a": 1}, "orchestrator": "default"}, "title"),
            ("/api/rooms", "POST", {"title": "Тестовая комната", "orchestrator": ["x"]}, "orchestrator"),
            ("/api/rooms/cafe", "PATCH", {"title": [1, 2]}, "title"),
            ("/api/rooms/cafe", "PATCH", {"max_rounds": "2"}, "max_rounds"),
            ("/api/rooms/cafe/requests", "POST", {"text": 123}, "text"),
            ("/api/rooms/cafe/requests", "POST", {"text": "x", "attach": "C:/a.md"}, "attach"),
            ("/api/rooms/cafe/members", "POST", {"profile": "coder", "note": {"a": 1}}, "note"),
            ("/api/rooms/cafe/members", "POST", {"profile": "lawyer", "role": "boss"}, "role"),
        ]
        for path, method, body, field in cases:
            status, _, data = self.json(path, method, body)
            self.assertEqual(status, 400, (path, body))
            self.assertEqual(data["error"]["code"], "bad_request")
            self.assertEqual(data["error"]["detail"]["field"], field, (path, body))
        status, _, data = self.json("/api/rooms", "POST", {"title": "Без директора в ростере", "orchestrator": "nobody"})
        self.assertEqual(status, 400)
        self.assertIn("biz-director", data["error"]["detail"]["available"])

    def test_journal_limit_range(self):
        self.assertEqual(self.json("/api/rooms/cafe/journal?limit=500")[0], 400)
        self.assertEqual(self.json("/api/rooms/cafe/journal?limit=0")[0], 400)
        self.assertEqual(self.json("/api/rooms/cafe/journal?limit=abc")[0], 400)
        status, _, data = self.json("/api/rooms/cafe/journal?limit=200")
        self.assertEqual(status, 200)
        self.assertIn("items", data)

    def test_method_not_allowed(self):
        status, head, data = self.call("/api/health", "POST", {})
        self.assertEqual(status, 405)
        self.assertIn("GET", head.get("Allow", ""))
        self.assertEqual(json.loads(data)["error"]["code"], "bad_request")
        status, head, _ = self.call("/api/requests/t_a1b2c3d4", "DELETE")
        self.assertEqual(status, 405)
        self.assertIn("GET", head.get("Allow", ""))
        status, head, _ = self.call("/api/adopt")
        self.assertEqual(status, 405)
        self.assertIn("POST", head.get("Allow", ""))
        self.assertEqual(self.call("/api/nope")[0], 404)

    def test_body_too_large_is_413(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        headers = ("POST /api/rooms/cafe/requests HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                   % (self.port, mock.MAX_BODY + 1))
        conn.sendall(headers.encode("ascii") + b"{}")
        raw = b""
        while True:
            part = conn.recv(8192)
            if not part:
                break
            raw += part
        conn.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertEqual(int(head.split(b" ")[1]), 413)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_request")

    def test_ask_returns_request_id(self):
        status, _, asked = self.json("/api/rooms/cafe/backlog/t_a1b2c3d4:ns_1/ask", "POST", {})
        self.assertEqual(status, 201)
        self.assertEqual(asked["request_id"], asked["root_id"])
        self.assertEqual(asked["request"]["id"], asked["root_id"])

    def test_adopt_appears_in_feed(self):
        status, _, adopted = self.json("/api/adopt", "POST", {"room": "cafe", "task_id": "t_71af0c5d"})
        self.assertEqual(status, 200)
        feed = self.json("/api/rooms/cafe/requests?limit=100")[2]
        ids = [i["id"] for i in feed["items"]]
        self.assertIn(adopted["request"]["id"], ids)

    # --- диктовка (server/stt.py) -----------------------------------------
    class FakeStt:
        def __init__(self):
            self.seen = {}

        def status(self, cfg):
            self.seen["status_cfg"] = cfg
            return {"available": True, "loaded": False, "loading": False,
                    "model": "fake-large", "device": "cpu", "reason": None}

        def transcribe(self, cfg, audio, content_type="audio/webm", *, language=None):
            self.seen["transcribe"] = {"cfg": cfg, "audio": audio, "ctype": content_type, "language": language}
            return {"text": "проверка", "raw": "проверка", "duration_s": 1.0, "elapsed_s": 0.1,
                    "language": language or "ru", "model": "fake-large", "device": "cpu", "corrections": []}

        def get_dictionary(self, cfg):
            self.seen["get_cfg"] = cfg
            return {"terms": ["моти"], "replacements": [], "updated_at": 1}

        def put_dictionary(self, cfg, obj):
            self.seen["put"] = (cfg, obj)
            return {"terms": obj.get("terms") or [], "replacements": obj.get("replacements") or [],
                    "updated_at": 2}

        def learn(self, cfg, raw, final, *, apply=False):
            self.seen["learn"] = {"cfg": cfg, "raw": raw, "final": final, "apply": apply}
            return {"suggestions": [{"from": "моти", "to": "моти"}], "saved": []}

    def test_stt_unavailable_is_honest_503(self):
        mock_stt.FORCE_UNAVAILABLE = True
        for path, method, body in (
                ("/api/stt/status", "GET", None),
                ("/api/stt/dictionary", "GET", None),
                ("/api/stt/dictionary", "PUT", {"terms": ["моти"]}),
                ("/api/stt/learn", "POST", {"raw": "моти", "final": "моти"})):
            status, _, data = self.json(path, method, body)
            self.assertEqual(status, 503, path)
            self.assertEqual(data["error"]["code"], "stt_unavailable", path)
            self.assertEqual(data["error"]["message"], mock_stt.UNAVAILABLE_MESSAGE)
            self.assertNotIn("text", data)
        req = urllib.request.Request(self.base + "/api/stt/transcribe", data=b"\x1aE\xdf\xa3", method="POST")
        req.add_header("Content-Type", "audio/webm")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 503)
        payload = json.loads(ctx.exception.read())
        self.assertEqual(payload["error"]["code"], "stt_unavailable")
        self.assertNotIn("text", payload)

    def test_stt_delegates_and_keeps_demo_dictionary_path(self):
        fake = self.FakeStt()
        mock_stt.MODULE_OVERRIDE = fake
        status, _, data = self.json("/api/stt/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["model"], "fake-large")
        cfg = fake.seen["status_cfg"]
        self.assertEqual(os.path.basename(str(cfg.data_dir)), "dictation-demo")
        self.assertEqual(str(cfg.dictionary_path),
                         os.path.join(str(cfg.data_dir), "dictation", "dictionary.json"))
        self.assertTrue(str(cfg.dictionary_path).startswith(str(cfg.data_dir)))

        status, _, data = self.json("/api/stt/dictionary")
        self.assertEqual((status, data["terms"]), (200, ["моти"]))
        status, _, data = self.json("/api/stt/dictionary", "PUT", {"terms": ["моти"], "replacements": []})
        self.assertEqual(status, 200)
        self.assertEqual(fake.seen["put"][1]["terms"], ["моти"])
        status, _, data = self.json("/api/stt/learn", "POST", {"raw": "моти", "final": "моти", "apply": True})
        self.assertEqual(status, 200)
        self.assertEqual(fake.seen["learn"]["apply"], True)
        self.assertEqual(fake.seen["learn"]["raw"], "моти")
        self.assertEqual(self.json("/api/stt/learn", "POST", {"raw": 5, "final": "x"})[0], 400)

        req = urllib.request.Request(self.base + "/api/stt/transcribe?language=ru",
                                     data=b"\x1aE\xdf\xa3audio", method="POST")
        req.add_header("Content-Type", "audio/webm; codecs=opus")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            got = json.loads(resp.read())
        self.assertEqual(got["text"], "проверка")
        self.assertEqual(fake.seen["transcribe"]["audio"], b"\x1aE\xdf\xa3audio")
        self.assertEqual(fake.seen["transcribe"]["ctype"], "audio/webm")
        self.assertEqual(fake.seen["transcribe"]["language"], "ru")

    def test_stt_dictionary_path_honours_env(self):
        fake = self.FakeStt()
        mock_stt.MODULE_OVERRIDE = fake
        tmp = tempfile.mkdtemp(prefix="teamroom-dict-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        with umock.patch.dict(os.environ, {"TEAMROOM_DATA_DICT": tmp}):
            status, _, _ = self.json("/api/stt/dictionary", "PUT", {"terms": ["моти"]})
        self.assertEqual(status, 200)
        cfg = fake.seen["put"][0]
        self.assertEqual(str(cfg.data_dir), tmp)
        self.assertEqual(str(cfg.dictionary_path), os.path.join(tmp, "dictation", "dictionary.json"))

    def test_stt_transcribe_validates_audio(self):
        mock_stt.MODULE_OVERRIDE = self.FakeStt()
        req = urllib.request.Request(self.base + "/api/stt/transcribe", data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(json.loads(ctx.exception.read())["error"]["code"], "bad_request")
        req = urllib.request.Request(self.base + "/api/stt/transcribe", data=b"", method="POST")
        req.add_header("Content-Type", "audio/webm")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(json.loads(ctx.exception.read())["error"]["code"], "audio_empty")
        status, _, body = self.raw_http("POST", "/api/stt/transcribe",
                                        {"Host": "127.0.0.1:%d" % self.port,
                                         "Content-Type": "audio/webm",
                                         "Content-Length": str(64 * 1024 * 1024 + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "audio_too_big")

    def test_stt_methods_and_health(self):
        status, head, data = self.json("/api/stt/status", "POST", {})
        self.assertEqual(status, 405)
        self.assertIn("GET", head.get("Allow", ""))
        self.assertEqual(self.json("/api/stt/transcribe")[0], 405)
        status, _, health = self.json("/api/health")
        self.assertIs(health["engine"]["mock"], True)

    # --- обслуживание -----------------------------------------------------
    def test_rooms_crud_and_members(self):
        status, _, room = self.json("/api/rooms", "POST",
                                    {"title": "Новая комната", "orchestrator": "default", "critic": None})
        self.assertEqual(status, 201)
        rid = room["id"]
        self.assertEqual(room["tenant"], "room:" + rid)
        self.assertEqual(room["max_rounds"], 1)
        self.assertFalse(room["seeded"])
        self.assertEqual(self.json("/api/rooms", "POST", {"title": "Новая комната", "orchestrator": "default"})[0], 409)
        status, _, err = self.json("/api/rooms", "POST",
                                   {"title": "Рой", "orchestrator": "default", "mode_default": "swarm"})
        self.assertEqual(status, 400)
        self.assertIn("не поддерживается", err["error"]["message"])
        status, _, patched = self.json("/api/rooms/" + rid, "PATCH", {"title": "Переименована", "max_rounds": 2})
        self.assertEqual(status, 200)
        self.assertEqual(patched["title"], "Переименована")
        self.assertEqual(patched["max_rounds"], 2)
        self.assertEqual(self.json("/api/rooms/" + rid, "PATCH", {"mode_default": "swarm"})[0], 400)
        status, _, added = self.json("/api/rooms/" + rid + "/members", "POST", {"profile": "coder", "note": "код"})
        self.assertEqual(status, 201)
        self.assertEqual(added["member"]["profile"], "coder")
        self.assertEqual(self.json("/api/rooms/" + rid + "/members", "POST", {"profile": "coder"})[0], 409)
        self.assertEqual(self.call("/api/rooms/%s/members/coder" % rid, "DELETE")[0], 200)
        self.assertEqual(self.call("/api/rooms/%s/members/default" % rid, "DELETE")[0], 409)
        status, _, archived = self.json("/api/rooms/%s/archive" % rid, "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(archived["room"]["archived"])
        self.assertFalse(any(r["id"] == rid for r in self.json("/api/rooms")[2]["rooms"]))

    def test_consolidate_creates_request(self):
        status, _, data = self.json("/api/rooms/cafe/consolidate", "POST", {"note": "после решений Михаила"})
        self.assertEqual(status, 201)
        self.assertIn("STATE.md", data["request"]["title"])
        self.assertIn("после решений Михаила", self.detail(data["root_id"])["text"])

    def test_report_spec_and_404(self):
        status, _, spec = self.json("/api/reports/cafe/unit-economics")
        self.assertEqual(status, 200)
        self.assertEqual(spec["version"], 1)
        self.assertGreaterEqual(len(spec["blocks"]), 8)
        self.assertIn("Юнит-экономика", spec["title"])
        self.assertEqual(self.json("/api/reports/cafe/nope")[0], 404)

    def test_schedule_defaults(self):
        self.assertEqual([n for _, n in mock.PHASES], ["queued", "working", "reviewing", "synthesizing", "done"])
        self.assertEqual(mock.PHASES[-1][0], 35.0)
        self.assertEqual([n for _, n in mock.PHASES_PLAIN], ["queued", "working", "synthesizing", "done"])
        self.assertEqual(mock.PHASES_PLAIN[-1][0], 30.0)

    # --- волна 8: диалог по запросу, папка проекта, бэклог -----------------
    THREAD_KEYS = {"id", "at", "author_kind", "author", "author_label", "text", "task_id", "kind"}

    def test_thread_of_working_request(self):
        data = self.detail("t_9f3c2a10")
        items = data["thread"]
        self.assertGreaterEqual(len(items), 5)
        self.assertLessEqual(len(items), 6)
        for item in items:
            self.assertEqual(set(item), self.THREAD_KEYS, item)
            self.assertIn(item["author_kind"], ("mikhail", "agent", "system"))
            self.assertIn(item["kind"], ("message", "comment", "event"))
            self.assertIsInstance(item["at"], int)
            self.assertTrue(item["text"].strip())
        self.assertEqual([i["at"] for i in items], sorted(i["at"] for i in items))
        self.assertEqual(items[0]["author_kind"], "mikhail")
        labels = [i["author_label"] for i in items]
        self.assertIn("Михаил", labels)
        self.assertIn("Глеб · руководитель комнаты", labels)     # не «директор»
        self.assertIn("system", [i["author_kind"] for i in items])
        self.assertTrue(any("Принял; передал" in i["text"] for i in items))

    def test_thread_of_done_request_has_versions_and_revision(self):
        data = self.detail("t_a1b2c3d4")
        texts = [i["text"] for i in data["thread"]]
        self.assertGreaterEqual(data["synthesis"]["version"], 2)
        self.assertTrue(any(t.startswith("Версия 1") for t in texts))
        self.assertTrue(any(t.startswith("Версия 2") for t in texts))
        self.assertTrue(any(i["author_kind"] == "mikhail" and i["id"].startswith("c") for i in data["thread"]))
        self.assertTrue(any(i["kind"] == "event" and "Версия" in i["text"] for i in data["thread"]))
        ids = [i["id"] for i in data["thread"]]
        self.assertEqual(len(ids), len(set(ids)))
        tree_ids = {n["id"] for n in data["tree"]}
        for item in data["thread"]:
            if item["task_id"]:
                self.assertIn(item["task_id"], tree_ids)

    def test_post_message_appends_and_gets_reply(self):
        status, _, res = self.json("/api/requests/t_9f3c2a10/messages", "POST",
                                   {"text": "Уточнение: нужен слой PE под холод, проверь у A"})
        self.assertEqual(status, 201)
        for key in ("message", "thread", "routed_to", "merged"):
            self.assertIn(key, res)
        self.assertFalse(res["merged"])
        self.assertEqual(res["routed_to"], "biz-director")
        self.assertEqual(res["message"]["author_kind"], "mikhail")
        self.assertEqual(res["message"]["kind"], "message")
        self.assertEqual(res["thread"][-1]["id"], res["message"]["id"])
        comment = self.detail("t_9f3c2a10")["tree"][0]["comments"][-1]
        self.assertTrue(comment["text"].startswith("[Михаил · уточнение]"))

        reply, data = None, None
        deadline = time.time() + 6.0
        while time.time() < deadline:
            data = self.detail("t_9f3c2a10")
            reply = next((i for i in data["thread"] if i["author_kind"] == "agent" and "Передал" in i["text"]), None)
            if reply:
                break
            time.sleep(0.05)
        self.assertIsNotNone(reply, "руководитель комнаты не ответил за 6 с")
        self.assertTrue(reply["text"].startswith("Принял"), reply["text"])
        self.assertIn("·", reply["author_label"])
        self.assertEqual(reply["task_id"], "t_9f3c2a10")
        self.assertTrue(any(i["kind"] == "event" and "уточнени" in i["text"].lower() for i in data["thread"]))
        self.assertEqual([i["at"] for i in data["thread"]], sorted(i["at"] for i in data["thread"]))

    def test_post_message_merges_while_reply_pending(self):
        status, _, first = self.json("/api/requests/t_7d21b8c4/messages", "POST",
                                     {"text": "Добавь срок подачи в Роспатент"})
        self.assertEqual(status, 201)
        self.assertFalse(first["merged"])
        status, _, res = self.json("/api/requests/t_7d21b8c4/messages", "POST",
                                   {"text": "И проверь СанПиН в ответе"})
        self.assertEqual(status, 201)
        self.assertTrue(res["merged"], "второе сообщение должно пакетироваться, пока ответ не пришёл")

    def test_post_message_to_done_creates_new_version(self):
        before = self.detail("t_a1b2c3d4")
        version = int(before["synthesis"]["version"])
        status, _, res = self.json("/api/requests/t_a1b2c3d4/messages", "POST",
                                   {"text": "Добавь срок подачи в Роспатент в шаги"})
        self.assertEqual(status, 201)
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["revision"]["version"], version + 1)
        data, deadline = before, time.time() + 6.0
        while time.time() < deadline:
            data = self.detail("t_a1b2c3d4")
            if data["synthesis"]["version"] == version + 1 and data["status"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(data["synthesis"]["version"], version + 1)
        self.assertEqual(data["synthesis"]["history"][-1]["version"], version + 1)
        self.assertEqual(data["status"], "done")
        self.assertTrue(any(i["text"].startswith("Версия %d" % (version + 1)) for i in data["thread"]))

    def test_post_message_validation(self):
        path = "/api/requests/t_9f3c2a10/messages"
        status, _, err = self.json(path, "POST", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertIn("Пустое сообщение", err["error"]["message"])
        self.assertEqual(self.json(path, "POST", {})[0], 400)
        self.assertEqual(self.json(path, "POST", {"text": 5})[0], 400)
        self.assertEqual(self.json(path, "POST", {"text": "a\x00b"})[0], 400)
        self.assertEqual(self.json(path, "POST", {"text": "x", "urgent": "да"})[0], 400)
        self.assertEqual(self.json(path, "POST", {"text": "x", "attach": "файл"})[0], 400)
        status, _, err = self.json(path, "POST", {"text": "x", "attach": [r"C:\Windows\win.ini"]})
        self.assertEqual(status, 403)
        self.assertEqual(self.json("/api/requests/t_ffffffff/messages", "POST", {"text": "x"})[0], 404)
        self.assertEqual(self.call(path, "GET")[0], 405)

    def test_check_dir_is_virtual(self):
        def check(path):
            status, _, data = self.json("/api/rooms/check-dir", "POST", {"path": path})
            self.assertEqual(status, 200, data)
            return data

        ok = check(r"D:\Проекты\Новая комната")
        self.assertTrue(ok["allowed"])
        self.assertFalse(ok["exists"])
        self.assertTrue(ok["will_create"])
        self.assertEqual(ok["normalized"], r"D:\Проекты\Новая комната")
        self.assertEqual(ok["warnings"], [])
        self.assertFalse(os.path.exists(ok["normalized"]), "мок не создаёт папки на диске")

        local = (os.environ.get("LOCALAPPDATA") or r"C:\Users\User\AppData\Local") + r"\hermes\skills"
        for bad, reason in (("C:\\", "system_root"), (r"C:\Windows\System32", "system_root"),
                            (r"C:\Program Files\Лето", "system_root"), (local, "app_data"),
                            ("Отчёты", "relative"), (r"\\server\share\dir", "network"),
                            (r"D:\Проекты\..\Windows", "traversal"),
                            (r"D:\Проекты\CON", "reserved")):
            data = check(bad)
            self.assertFalse(data["allowed"], bad)
            self.assertEqual(data["reason"], reason, bad)
            self.assertTrue(data["message"])
            self.assertFalse(data["will_create"])

        vault = check(r"D:\Проекты\Vault\Комната")
        self.assertTrue(vault["allowed"])
        self.assertTrue(vault["warnings"], "папка внутри Vault — предупреждение")
        self.assertEqual(self.json("/api/rooms/check-dir", "POST", {})[0], 400)
        self.assertEqual(self.json("/api/rooms/check-dir", "POST", {"path": 5})[0], 400)

    def test_room_project_dir_saved_but_not_created(self):
        _, _, room = self.json("/api/rooms/cafe")
        self.assertEqual(room["project_dir"], r"D:\Проекты\Лето")
        target = r"C:\TeamRoom\Projects\Лето-демо"
        status, _, patched = self.json("/api/rooms/cafe", "PATCH", {"project_dir": target})
        self.assertEqual(status, 200)
        self.assertEqual(patched["project_dir"], target)
        self.assertFalse(os.path.exists(target), "мок не создаёт папку проекта")
        status, _, err = self.json("/api/rooms/cafe", "PATCH", {"project_dir": r"C:\Windows\Temp"})
        self.assertEqual(status, 400)
        self.assertIn("системная", err["error"]["message"])
        status, _, created = self.json("/api/rooms", "POST",
                                       {"title": "Демо-папка", "orchestrator": "default",
                                        "project_dir": r"D:\Проекты\Демо-папка"})
        self.assertEqual(status, 201)
        self.assertEqual(created["project_dir"], r"D:\Проекты\Демо-папка")
        self.assertEqual(self.json("/api/rooms", "POST",
                                   {"title": "Плохая папка", "orchestrator": "default",
                                    "project_dir": "Отчёты"})[0], 400)

    def test_backlog_covers_every_request_of_both_rooms(self):
        for room in ("cafe", "fn"):
            rids = [i["id"] for i in self.json("/api/rooms/%s/requests?limit=100" % room)[2]["items"]]
            self.assertTrue(rids, room)
            ids = {i["id"] for i in self.json("/api/rooms/%s/backlog" % room)[2]["items"]}
            for rid in rids:
                self.assertIn(rid + ":ns_1", ids, "%s: у запроса %s нет шага в бэклоге" % (room, rid))
                status, _, res = self.json("/api/rooms/%s/backlog/%s" % (room, quote(rid + ":ns_1", safe="")),
                                           "POST", {"status": "done"})
                self.assertEqual(status, 200, "%s: %s" % (rid, res))
                self.assertEqual(res["item"]["status"], "done")
                self.assertEqual(res["item"]["id"], rid + ":ns_1")

    def test_backlog_has_questions_and_decisions(self):
        data = self.json("/api/rooms/cafe/backlog")[2]
        ids = {i["id"] for i in data["items"]}
        self.assertIn("t_5c8e4a02:oq_1", ids)      # вопрос Михаилу
        self.assertIn("t_a1b2c3d4:d_1", ids)      # решение
        self.assertIn("t_a1b2c3d4:d_2", ids)
        fn_ids = {i["id"] for i in self.json("/api/rooms/fn/backlog")[2]["items"]}
        self.assertIn("t_4e91c7b2:fq_1", fn_ids)  # вопрос «на будущее» комнаты FN
        self.assertEqual(data["counts"]["total"], len(data["items"]))
        by_id = {i["id"]: i for i in data["items"]}
        self.assertEqual(by_id["t_5c8e4a02:oq_1"]["status"], "open")
        self.assertEqual(by_id["t_a1b2c3d4:d_1"]["status"], "done")
        status, _, res = self.json("/api/rooms/cafe/backlog/%s" % quote("t_5c8e4a02:oq_1", safe=""),
                                   "POST", {"status": "dismissed"})
        self.assertEqual(status, 200)
        self.assertEqual(res["item"]["status"], "dismissed")

    def test_backlog_unknown_id_is_404(self):
        status, _, err = self.json("/api/rooms/cafe/backlog/t_a1b2c3d4%3Ans_99", "POST", {"status": "done"})
        self.assertEqual(status, 404)
        self.assertIn("не найден", err["error"]["message"])
        self.assertEqual(self.json("/api/rooms/fn/backlog/t_a1b2c3d4%3Ans_1", "POST", {"status": "done"})[0], 404)

    # --- волна 9: карта проекта, бриф, особые указания ---------------------
    def plan(self, room="cafe"):
        status, _, data = self.json("/api/rooms/%s/plan" % room)
        self.assertEqual(status, 200)
        return data

    def wait_plan(self, room, predicate, timeout=8.0, step=0.05):
        """Дождаться состояния карты (очередь тикает на каждом GET)."""
        deadline, data = time.time() + timeout, None
        while time.time() < deadline:
            data = self.plan(room)
            if predicate(data["plan"]):
                return data["plan"]
            time.sleep(step)
        self.fail("карта не дошла до нужного состояния: %s" % json.dumps(data["plan"], ensure_ascii=False)[:400])

    def test_plan_fixtures_of_both_rooms(self):
        cafe = self.plan("cafe")
        plan = cafe["plan"]
        self.assertEqual(plan["status"], "running")
        self.assertEqual(plan["brief_root_id"], "t_605613e0")
        self.assertEqual(len(plan["streams"]), 5)
        self.assertEqual({s["status"] for s in plan["streams"]},
                         {"done", "running", "queued", "proposed", "waiting_quota"})
        self.assertEqual(cafe["summary"]["total"], 5)
        self.assertTrue(plan["log"])
        for entry in plan["log"]:
            self.assertIn(entry["who"], ("manager", "mikhail", "system"))
            self.assertIn(entry["kind"], ("created", "changed", "added", "launched", "dropped", "quota"))
            self.assertTrue(entry["text"].strip())

        fn = self.plan("fn")["plan"]
        self.assertEqual(fn["status"], "draft")
        self.assertEqual(len(fn["streams"]), 3)
        self.assertEqual({s["status"] for s in fn["streams"]}, {"proposed"})
        self.assertEqual(fn["streams"][2]["depends_on"], ["s1", "s2"])

    def test_brief_and_subrequest_links(self):
        brief = self.detail("t_605613e0")
        self.assertIs(brief["brief"], True)
        self.assertEqual(brief["streams_summary"],
                         {"total": 5, "running": 2, "done": 1, "status": "running"})
        self.assertIsNone(brief["plan_link"])

        sub = self.detail("t_9f3c2a10")
        self.assertIs(sub["brief"], False)
        self.assertEqual(sub["plan_link"], {"stream_id": "s2", "title": "Упаковка и бренд", "status": "running"})
        self.assertIsNone(sub["streams_summary"])

        quota = self.detail("t_7d21b8c4")
        self.assertEqual(quota["plan_link"]["stream_id"], "s5")
        self.assertEqual(quota["plan_link"]["status"], "waiting_quota")

    def test_approve_respects_dependencies_and_parallel_limit(self):
        room = self.json("/api/rooms/fn")[2]
        self.assertEqual(room["max_parallel_streams"], 1)
        status, _, res = self.json("/api/rooms/fn/plan/approve", "POST", {"streams": "all"})
        self.assertEqual(status, 200)
        self.assertEqual(res["started"], ["s1"])              # параллельно только одно направление
        by_id = {s["id"]: s["status"] for s in res["plan"]["streams"]}
        self.assertEqual(by_id["s1"], "running")
        self.assertEqual(by_id["s2"], "queued")
        self.assertEqual(by_id["s3"], "queued")               # ждёт s1 и s2

        # под-запрос направления появился в ленте комнаты и имеет plan_link
        rid = next(s["request_id"] for s in res["plan"]["streams"] if s["id"] == "s1")
        self.assertIn(rid, [i["id"] for i in self.json("/api/rooms/fn/requests?limit=100")[2]["items"]])
        self.assertEqual(self.detail(rid)["plan_link"]["stream_id"], "s1")

        plan = self.wait_plan("fn", lambda p: all(s["status"] == "done" for s in p["streams"]), timeout=20.0)
        self.assertTrue(all(s["request_id"] for s in plan["streams"]))
        self.assertEqual(len({s["request_id"] for s in plan["streams"]}), 3, "под-запросы не должны повторяться")

    def test_approve_is_idempotent(self):
        status, _, first = self.json("/api/rooms/fn/plan/approve", "POST", {"streams": ["s1"]})
        self.assertEqual(status, 200)
        self.assertEqual(first["started"], ["s1"])
        status, _, again = self.json("/api/rooms/fn/plan/approve", "POST", {"streams": ["s1"]})
        self.assertEqual(status, 200)
        self.assertEqual(again["started"], [], "повторное «Запустить» не создаёт второй запрос")
        rids = [s["request_id"] for s in again["plan"]["streams"] if s["request_id"]]
        self.assertEqual(len(rids), len(set(rids)))

    def test_approve_validation(self):
        self.assertEqual(self.json("/api/rooms/fn/plan/approve", "POST", {"streams": []})[0], 400)
        self.assertEqual(self.json("/api/rooms/fn/plan/approve", "POST", {"streams": "s1"})[0], 400)
        status, _, err = self.json("/api/rooms/fn/plan/approve", "POST", {"streams": ["s99"]})
        self.assertEqual(status, 404)
        self.assertIn("нет в карте", err["error"]["message"])
        self.assertEqual(self.json("/api/rooms/cafe/plan/approve", "POST", {"streams": ["s4"]})[0], 200)

        status, _, patched = self.json("/api/rooms/fn", "PATCH", {"project_mode": False})
        self.assertIs(patched["project_mode"], False)
        status, _, err = self.json("/api/rooms/fn/plan/approve", "POST", {"streams": "all"})
        self.assertEqual(status, 409)
        self.assertIn("Проектный режим", err["error"]["message"])

    def test_stream_edit_add_and_drop(self):
        status, _, res = self.json("/api/rooms/cafe/plan/streams/s4", "PATCH",
                                   {"title": "Продажи, доставка и ярмарка", "due": "2026-12-10"})
        self.assertEqual(status, 200)
        stream = next(s for s in res["plan"]["streams"] if s["id"] == "s4")
        self.assertEqual(stream["title"], "Продажи, доставка и ярмарка")
        self.assertEqual(stream["due"], "2026-12-10")
        self.assertTrue(any("Михаил изменил" in e["text"] for e in res["plan"]["log"]))

        status, _, added = self.json("/api/rooms/cafe/plan/streams", "POST",
                                     {"title": "Сертификация Роспотребнадзор", "owner": "biz-lawyer",
                                      "depends_on": ["s1"]})
        self.assertEqual(status, 201)
        new = added["stream"]
        self.assertEqual(new["created_by"], "mikhail")
        self.assertEqual(new["status"], "proposed")
        self.assertTrue(any(e["kind"] == "added" and "Сертификация" in e["text"] for e in added["plan"]["log"]))

        status, _, dropped = self.json("/api/rooms/cafe/plan/streams/%s" % new["id"], "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(next(s for s in dropped["plan"]["streams"] if s["id"] == new["id"])["status"], "dropped")
        self.assertTrue(any(e["kind"] == "dropped" for e in dropped["plan"]["log"]))
        # отложенное направление не запускается
        self.assertEqual(self.json("/api/rooms/cafe/plan/approve", "POST", {"streams": [new["id"]]})[0], 409)

    def test_stream_validation_in_russian(self):
        path = "/api/rooms/cafe/plan/streams"
        status, _, err = self.json(path, "POST", {"title": "Чужая тема", "owner": "coder"})
        self.assertEqual(status, 400)
        self.assertIn("не участвует в комнате", err["error"]["message"])
        status, _, err = self.json("/api/rooms/cafe/plan/streams", "POST",
                                   {"title": "Цикл", "owner": "biz-ops", "depends_on": ["s99"]})
        self.assertEqual(status, 400)
        self.assertIn("такого направления нет", err["error"]["message"])
        # s3 уже зависит от s1 — обратная зависимость даёт цикл
        status, _, err = self.json("/api/rooms/cafe/plan/streams/s1", "PATCH", {"depends_on": ["s3"]})
        self.assertEqual(status, 400)
        self.assertIn("Цикл зависимостей", err["error"]["message"])
        self.assertEqual(self.json(path, "POST", {"title": "  ", "owner": "biz-ops"})[0], 400)
        status, _, err = self.json("/api/rooms/cafe/plan/streams/s2", "PATCH", {"status": "running"})
        self.assertEqual(status, 400)
        self.assertIn("Запустить", err["error"]["message"])
        self.assertEqual(self.json("/api/rooms/cafe/plan/streams/s99", "PATCH", {"title": "Нет такого"})[0], 404)

        plan = self.plan("cafe")["plan"]
        while len(plan["streams"]) < 8:
            status, _, res = self.json(path, "POST", {"title": "Направление %d" % len(plan["streams"]),
                                                      "owner": "biz-ops"})
            self.assertEqual(status, 201, res)
            plan = res["plan"]
        status, _, err = self.json(path, "POST", {"title": "Девятое", "owner": "biz-ops"})
        self.assertEqual(status, 400)
        self.assertIn("не больше 8", err["error"]["message"])

    def test_brief_request_gets_draft_plan_in_demo_tempo(self):
        status, _, res = self.json("/api/rooms/fn/requests", "POST",
                                   {"text": "Хочу продавать наборы моти с доставкой", "brief": True})
        self.assertEqual(status, 201)
        rid = res["root_id"]
        self.assertIs(res["request"]["brief"], True)
        # руководитель «думает» 5–8 с демо-темпа: сразу карты ещё нет
        plan = self.wait_plan("fn", lambda p: bool(p) and p.get("brief_root_id") == rid)
        self.assertEqual(plan["status"], "draft")
        self.assertEqual({s["status"] for s in plan["streams"]}, {"proposed"})
        self.assertTrue(any(e["kind"] == "created" for e in plan["log"]))

        detail = self.detail(rid)
        self.assertIs(detail["brief"], True)
        self.assertEqual(detail["streams_summary"]["total"], len(plan["streams"]))
        texts = [i["text"] for i in detail["thread"] if i["author_kind"] == "agent"]
        self.assertTrue(any("Предлагаю карту проекта" in t for t in texts), texts)

    def test_rebuild_creates_map_request(self):
        status, _, res = self.json("/api/rooms/cafe/plan/rebuild", "POST", {})
        self.assertEqual(status, 201)
        self.assertTrue(res["root_id"])
        self.assertIn("карт", res["request"]["title"].lower())
        plan = self.wait_plan("cafe", lambda p: p.get("brief_root_id") == res["root_id"])
        self.assertEqual(plan["status"], "draft")
        ids = [s["id"] for s in plan["streams"]]
        self.assertEqual(len(ids), len(set(ids)), "id направлений не должны повторяться")
        self.assertTrue(any("собирает карту" in e["text"] for e in plan["log"]))

    def test_message_to_manager_adds_direction(self):
        plan = self.plan("cafe")["plan"]
        before = len(plan["streams"])
        status, _, res = self.json("/api/requests/t_605613e0/messages", "POST",
                                   {"text": "Ещё нужна сертификация Роспотребнадзор и маркировка упаковки"})
        self.assertEqual(status, 201)
        reply = None
        deadline = time.time() + 6.0
        while time.time() < deadline:
            items = self.detail("t_605613e0")["thread"]
            reply = next((i for i in items if i["author_kind"] == "agent" and "Добавил в карту" in i["text"]), None)
            if reply:
                break
            time.sleep(0.05)
        self.assertIsNotNone(reply, "руководитель не добавил направление")
        self.assertIn("Сертификация и маркировка", reply["text"])
        plan = self.plan("cafe")["plan"]
        self.assertEqual(len(plan["streams"]), before + 1)
        added = plan["streams"][-1]
        self.assertEqual(added["status"], "proposed")
        self.assertIn("Добавил", plan["log"][-1]["text"])
        self.assertEqual(plan["log"][-1]["kind"], "added")

    def test_agent_notes_round_trip(self):
        room = self.json("/api/rooms/cafe")[2]
        self.assertEqual(set(room["agent_notes"]), {"biz-ops", "biz-cfo"})
        self.assertTrue(room["agent_notes"]["biz-ops"]["text"].startswith("Отвечай коротко"))
        self.assertIsInstance(room["agent_notes"]["biz-ops"]["updated_at"], int)

        path = "/api/rooms/cafe/agents/biz-runner/notes"
        status, _, res = self.json(path, "PUT", {"text": "Всегда давай ссылку на источник."})
        self.assertEqual(status, 200)
        self.assertEqual(res["text"], "Всегда давай ссылку на источник.")
        self.assertEqual(res["agent_notes"]["biz-runner"]["text"], "Всегда давай ссылку на источник.")
        self.assertEqual(self.json("/api/rooms/cafe")[2]["agent_notes"]["biz-runner"]["text"],
                         "Всегда давай ссылку на источник.")

        status, _, cleared = self.json(path, "PUT", {"text": "   "})
        self.assertEqual(status, 200)
        self.assertTrue(cleared["cleared"])
        self.assertNotIn("biz-runner", self.json("/api/rooms/cafe")[2]["agent_notes"])

    def test_agent_notes_validation_in_russian(self):
        self.assertEqual(self.json("/api/rooms/cafe/agents/nobody/notes", "PUT", {"text": "x"})[0], 400)
        status, _, err = self.json("/api/rooms/cafe/agents/nobody/notes", "PUT", {"text": "x"})
        self.assertIn("не участвует в комнате", err["error"]["message"])
        status, _, err = self.json("/api/rooms/cafe/agents/biz-ops/notes", "PUT", {"text": "а" * 8001})
        self.assertEqual(status, 400)
        self.assertIn("8000", err["error"]["message"])
        self.assertEqual(self.json("/api/rooms/cafe/agents/biz-ops/notes", "PUT", {})[0], 400)
        self.assertEqual(self.json("/api/rooms/cafe/agents/biz-ops/notes", "PUT", {"text": 5})[0], 400)
        status, _, res = self.json("/api/rooms/cafe/agents/biz-ops/notes", "PUT",
                                   {"text": "Ключ sk-abc123def не показывай, пароль password=hunter2"})
        self.assertEqual(status, 200)
        self.assertTrue(res["warnings"], "секрет в указаниях — предупреждение, не блокировка")
        self.assertEqual(res["warnings"][0]["code"], "secret_in_notes")

    def test_room_settings_and_notes_in_create_and_patch(self):
        status, _, created = self.json("/api/rooms", "POST",
                                       {"title": "Демо-проект", "orchestrator": "biz-director",
                                        "members": [{"profile": "biz-ops", "role": "worker"}],
                                        "launch_mode": "auto", "max_parallel_streams": 3,
                                        "project_mode": True,
                                        "agent_notes": {"biz-ops": {"text": "Отвечай цифрами."}}})
        self.assertEqual(status, 201, created)
        self.assertEqual(created["launch_mode"], "auto")
        self.assertEqual(created["max_parallel_streams"], 3)
        self.assertIs(created["project_mode"], True)
        self.assertEqual(created["agent_notes"]["biz-ops"]["text"], "Отвечай цифрами.")
        rid = created["id"]
        self.assertEqual(self.json("/api/rooms/%s" % rid)[2]["agent_notes"]["biz-ops"]["text"], "Отвечай цифрами.")

        status, _, patched = self.json("/api/rooms/%s" % rid, "PATCH",
                                       {"max_parallel_streams": 4, "launch_mode": "plan_first"})
        self.assertEqual(status, 200)
        self.assertEqual(patched["max_parallel_streams"], 4)
        self.assertEqual(patched["launch_mode"], "plan_first")
        status, _, err = self.json("/api/rooms/%s" % rid, "PATCH", {"max_parallel_streams": 9})
        self.assertEqual(status, 400)
        self.assertIn("вне диапазона", err["error"]["message"])
        status, _, err = self.json("/api/rooms/%s" % rid, "PATCH", {"launch_mode": "later"})
        self.assertEqual(status, 400)
        status, _, err = self.json("/api/rooms/%s" % rid, "PATCH",
                                   {"agent_notes": {"coder": {"text": "x"}}})
        self.assertEqual(status, 400)
        self.assertIn("не участвует", err["error"]["message"])
        self.assertEqual(self.json("/api/rooms/%s" % rid, "PATCH", {"agent_notes": "строка"})[0], 400)

    def test_plan_methods_and_limits(self):
        self.assertEqual(self.call("/api/rooms/cafe/plan", "POST", {})[0], 405)
        self.assertEqual(self.call("/api/rooms/cafe/plan/streams/s1", "POST", {})[0], 405)
        status, _, plan_room = self.json("/api/rooms/fn/plan")
        self.assertIn("summary", plan_room)
        # текст запроса: лимит 1 000 000 (не 20 000)
        status, _, res = self.json("/api/rooms/fn/requests", "POST", {"text": "а" * 25000})
        self.assertEqual(status, 201, res)
        self.assertEqual(self.json("/api/rooms/fn/requests", "POST", {"text": "а" * 1000001})[0], 400)

    # --- модели участников (волна 10) --------------------------------------

    def test_models_catalog_is_fictional(self):
        status, _, cat = self.json("/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(cat), ["cached", "efforts", "providers", "source", "updated_at", "warnings"])
        self.assertEqual(len(cat["providers"]), 4)
        self.assertEqual([e["id"] for e in cat["efforts"]],
                         ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
        self.assertTrue(all(isinstance(e["label"], str) and e["label"] for e in cat["efforts"]))
        configured = [p for p in cat["providers"] if p["configured"]]
        self.assertEqual(len(configured), 3, "один провайдер должен быть ненастроенным — для фильтра")
        for provider in cat["providers"]:
            self.assertIsInstance(provider["id"], str)
            self.assertIsInstance(provider["label"], str)
            self.assertTrue(3 <= len(provider["models"]) <= 6, provider)
        names = " ".join(p["label"] + " " + m["label"] for p in cat["providers"] for m in p["models"]).lower()
        for real in ("anthropic", "openai", "ollama", "gemini", "claude", "gpt", "deepseek", "qwen"):
            self.assertNotIn(real, names, "в каталоге демо не должно быть реальных провайдеров")

    def test_members_have_model_settings_and_agent_default(self):
        cafe = self.json("/api/rooms/cafe")[2]
        for member in cafe["members"]:
            settings = member["model_settings"]
            self.assertEqual(sorted(settings), ["model", "provider", "reasoning", "source", "updated_at"])
            self.assertEqual(sorted(member["agent_default"]), ["model", "provider", "reasoning"])
        by_profile = {m["profile"]: m for m in cafe["members"]}
        ops = by_profile["biz-ops"]
        self.assertEqual(ops["model_settings"]["source"], "room", "у biz-ops модель изменена для комнаты")
        self.assertEqual(ops["model_settings"]["model"], "dilizhans-ultra")
        self.assertNotEqual(ops["model_settings"]["model"], ops["agent_default"]["model"])
        cfo = by_profile["biz-cfo"]
        self.assertEqual(cfo["model_settings"]["source"], "agent")
        self.assertEqual({k: cfo["model_settings"][k] for k in ("provider", "model", "reasoning")},
                         cfo["agent_default"])
        self.assertIn("biz-ops", cafe["agent_models"])
        fn = self.json("/api/rooms/fn")[2]
        layers = {m["profile"]: m["model_settings"]["source"] for m in fn["members"]}
        self.assertEqual(layers, {"lawyer": "room", "coder": "agent", "surveyor": "agent"})

    def test_put_model_changes_and_resets(self):
        path = "/api/rooms/cafe/agents/biz-cfo/model"
        status, _, res = self.json(path, "PUT", {"provider": "oblako-b", "model": "vektor-2", "reasoning": "low"})
        self.assertEqual(status, 200, res)
        self.assertEqual(sorted(res), ["agent_models", "applied", "cleared", "member", "ok",
                                       "profile", "room", "warnings"])
        self.assertEqual(sorted(res["applied"]), ["cards_failed", "cards_updated", "cleared",
                                                  "pending_running", "saved", "settings"])
        self.assertEqual(res["applied"]["saved"]["model"], "vektor-2",
                         "applied.saved — ровно запись реестра (без «ближайших» моделей)")
        self.assertEqual(res["applied"]["settings"]["source"], "room")
        self.assertEqual({k: res["member"]["model_settings"][k]
                          for k in ("provider", "model", "reasoning", "source")},
                         {"provider": "oblako-b", "model": "vektor-2", "reasoning": "low", "source": "room"})
        self.assertIsInstance(res["member"]["model_settings"]["updated_at"], int)
        self.assertEqual(res["agent_models"]["biz-cfo"]["model"], "vektor-2")
        self.assertEqual(res["applied"]["cards_updated"], 2,
                         "у biz-cfo две карточки, которым сторож закрепит модель: ready и blocked")
        self.assertEqual(res["applied"]["pending_running"], 0)
        room = self.json("/api/rooms/cafe")[2]
        cfo = next(m for m in room["members"] if m["profile"] == "biz-cfo")
        self.assertEqual(cfo["model_settings"]["model"], "vektor-2")
        self.assertEqual(cfo["model_settings"]["source"], "room")
        self.assertEqual(room["agent_models"]["biz-cfo"]["reasoning"], "low")

        journal = self.json("/api/rooms/cafe/journal")[2]["items"]
        self.assertEqual(journal[0]["kind"], "model_changed")
        self.assertIn("Модель участника «biz-cfo» изменена", journal[0]["text"])
        self.assertIn("Облако-Б · Вектор 2 · низкие", journal[0]["text"])

        # сторож комнаты: у карточек не в работе подпись меняется сразу, у done — нет
        tree = {n["id"]: n for n in self.detail("t_9f3c2a10")["tree"]}
        self.assertEqual(tree["t_9f3c2a14"]["model_label"], "Облако-Б · Вектор 2 · низкие")
        self.assertEqual(tree["t_9f3c2a14"]["status"], "ready")
        done_tree = {n["id"]: n for n in self.detail("t_c4d2e118")["tree"]}
        self.assertNotEqual(done_tree["t_c4d2e11c"].get("model_label"), "Облако-Б · Вектор 2 · низкие",
                            "завершённые карточки сторож не трогает")

        status, _, cleared = self.json(path, "PUT", {})
        self.assertEqual(status, 200)
        self.assertEqual(cleared["member"]["model_settings"]["source"], "agent")
        self.assertEqual({k: cleared["member"]["model_settings"][k] for k in ("provider", "model", "reasoning")},
                         cleared["member"]["agent_default"])
        back = {n["id"]: n for n in self.detail("t_9f3c2a10")["tree"]}
        self.assertEqual(back["t_9f3c2a14"]["model_label"],
                         "Облако-А · Дилижанс ультра · очень высокие")
        self.assertNotIn("biz-cfo", self.json("/api/rooms/cafe")[2]["agent_models"])

    def test_put_model_validation_in_russian(self):
        path = "/api/rooms/cafe/agents/%s/model"
        status, _, err = self.json(path % "nobody", "PUT", {"provider": "oblako-a", "model": "dilizhans-mini"})
        self.assertEqual(status, 400)
        self.assertIn("не участвует в комнате", err["error"]["message"])
        status, _, err = self.json(path % "biz-ops", "PUT", {"provider": "kontur", "model": "kontur-1"})
        self.assertEqual(status, 400)
        self.assertIn("не настроен", err["error"]["message"])
        status, _, err = self.json(path % "biz-ops", "PUT", {"provider": "oblako-a", "model": "нет-такой"})
        self.assertEqual(status, 400)
        self.assertIn("нет модели", err["error"]["message"])
        status, _, err = self.json(path % "biz-ops", "PUT",
                                   {"provider": "oblako-a", "model": "dilizhans-mini", "reasoning": "turbo"})
        self.assertEqual(status, 400)
        self.assertIn("Усилия", err["error"]["message"])
        status, _, err = self.json(path % "biz-ops", "PUT", {"provider": "oblako-a"})
        self.assertEqual(status, 400)
        self.assertIn("выбрать модель", err["error"]["message"])
        status, _, err = self.json(path % "biz-ops", "PUT", {"model": "dilizhans-mini"})
        self.assertEqual(status, 400)
        self.assertIn("без провайдера", err["error"]["message"])
        self.assertEqual(self.json(path % "biz-ops", "PUT", {"provider": 5})[0], 400)
        self.assertEqual(self.call(path % "biz-ops", "GET")[0], 405)

    def test_pending_running_and_restart(self):
        path = "/api/rooms/cafe/agents/biz-ops/model"
        status, _, res = self.json(path, "PUT", {"provider": "lokal", "model": "dilizhans-home-2",
                                                 "reasoning": "high"})
        self.assertEqual(status, 200)
        self.assertEqual(res["applied"]["pending_running"], 1, "у biz-ops есть карточка в работе")
        self.assertTrue(res["warnings"], "нужно предупреждение про перезапуск")
        self.assertIn("Перезапустить на новой модели", res["warnings"][0])

        status, _, restarted = self.json("/api/rooms/cafe/agents/biz-ops/restart", "POST", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(restarted["restarted"], 1)
        self.assertEqual(len(restarted["cards"]), restarted["restarted"])
        self.assertTrue(all(str(c).startswith("t_") for c in restarted["cards"]))
        journal = self.json("/api/rooms/cafe/journal")[2]["items"]
        self.assertEqual(journal[0]["kind"], "model_restarted")
        status, _, err = self.json("/api/rooms/cafe/agents/nobody/restart", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("не участвует", err["error"]["message"])
        self.assertEqual(self.call("/api/rooms/cafe/agents/biz-ops/restart", "GET")[0], 405)
        _, _, quiet = self.json("/api/rooms/cafe/agents/biz-cfo/restart", "POST", {})
        self.assertEqual(quiet["restarted"], 0)

    def test_agent_models_in_create_and_patch(self):
        status, _, created = self.json("/api/rooms", "POST",
                                       {"title": "Проверка моделей", "orchestrator": "biz-director",
                                        "members": [{"profile": "biz-cfo"}, {"profile": "biz-runner"}],
                                        "agent_models": {"biz-cfo": {"provider": "oblako-a",
                                                                     "model": "dilizhans-pro",
                                                                     "reasoning": "high"}}})
        self.assertEqual(status, 201, created)
        rid = created["id"]
        layers = {m["profile"]: m["model_settings"]["source"] for m in created["members"]}
        self.assertEqual(layers, {"biz-cfo": "room", "biz-runner": "agent"})

        status, _, patched = self.json("/api/rooms/%s" % rid, "PATCH",
                                       {"agent_models": {"biz-runner": {"provider": "oblako-b",
                                                                        "model": "vektor-1",
                                                                        "reasoning": "minimal"}}})
        self.assertEqual(status, 200)
        self.assertEqual([m["model_settings"]["source"] for m in patched["members"]], ["agent", "room"])
        room = self.json("/api/rooms/%s" % rid)[2]
        self.assertEqual(room["agent_models"]["biz-runner"]["model"], "vektor-1")

        status, _, err = self.json("/api/rooms/%s" % rid, "PATCH",
                                   {"agent_models": {"coder": {"provider": "oblako-a",
                                                               "model": "dilizhans-mini"}}})
        self.assertEqual(status, 400)
        self.assertIn("не участвует", err["error"]["message"])
        status, _, err = self.json("/api/rooms/%s" % rid, "PATCH",
                                   {"agent_models": {"biz-runner": {"provider": "kontur",
                                                                    "model": "kontur-1"}}})
        self.assertEqual(status, 400)
        self.assertIn("не настроен", err["error"]["message"])
        self.assertEqual(self.json("/api/rooms/%s" % rid, "PATCH", {"agent_models": "строка"})[0], 400)
        _, _, cleared = self.json("/api/rooms/%s" % rid, "PATCH", {"agent_models": {}})
        self.assertEqual(cleared["agent_models"], {})
        self.assertEqual([m["model_settings"]["source"] for m in cleared["members"]], ["agent", "agent"])

    def test_model_label_in_trees(self):
        detail = self.detail("t_9f3c2a10")
        by_id = {n["id"]: n for n in detail["tree"]}
        self.assertEqual(by_id["t_9f3c2a13"]["model_label"], "Облако-А · Дилижанс ультра · высокие")
        self.assertTrue(by_id["t_9f3c2a14"]["model_label"].endswith("· очень высокие"))
        self.assertNotIn("model_label", by_id["t_9f3c2a10"], "у корня подписи нет")
        status, _, res = self.json("/api/rooms/cafe/requests", "POST",
                                   {"text": "Проверь подписи моделей в дереве."})
        self.assertEqual(status, 201, res)
        rid = res["root_id"]
        self.assertEqual(self.json("/api/requests/%s/actions" % rid, "POST",
                                   {"action": "decompose"})[0], 200)
        fresh = self.detail(rid)
        workers = [n for n in fresh["tree"] if n.get("assignee") and n["role"] != "root"]
        self.assertTrue(workers)
        for node in workers:
            self.assertIn(" · ", node["model_label"])
            self.assertEqual(len(node["model_label"].split(" · ")), 3)

    def test_models_endpoint_methods(self):
        self.assertEqual(self.call("/api/models", "POST", {})[0], 405)
        status, head, _raw = self.call("/api/models", "POST", {})
        self.assertIn("GET", head.get("Allow", ""))
        self.assertEqual(self.call("/api/rooms/cafe/agents/biz-ops/model", "POST", {})[0], 405)
        self.assertEqual(self.call("/api/rooms/cafe/agents/biz-ops/restart", "POST", {})[0], 200)

    # --- фикстуры ---------------------------------------------------------
    def test_fixtures_valid_json_and_report_spec(self):
        names = sorted(n for n in os.listdir(self.fixtures) if n.endswith(".json"))
        self.assertGreaterEqual(len(names), 25)
        for name in names:
            with open(os.path.join(self.fixtures, name), encoding="utf-8") as fh:
                json.load(fh)
        result = subprocess.run([sys.executable, os.path.join(ROOT, "reportkit", "validate.py"),
                                 os.path.join(self.fixtures, "report_unit_economics.json")],
                                capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
