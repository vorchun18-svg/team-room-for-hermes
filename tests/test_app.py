"""Тесты оболочки «Комнаты команды»: транспорт, роутинг, ETag, watcher.

Модули домена подменяются фейками (sys.modules), поэтому набор проходит
независимо от готовности engine_read/engine_write/rooms/ingest/… .
Реальный сервер на свободном порту проверяется отдельным классом.
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import http.client
import io
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from types import ModuleType
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod          # noqa: E402
import server.config as config        # noqa: E402
import server.http_util as http_util  # noqa: E402

#: Запреты на весь модуль: тест дергает `POST /api/dispatcher/kick` на фейках домена,
#: но если подмена не установится, запрос не должен дойти до настоящего CLI/агентов.
_GUARDS: list = []


def setUpModule():
    from server import safety

    _GUARDS.extend([safety.forbid_real_writes(), safety.forbid_real_subprocess()])
    for cm in _GUARDS:
        cm.__enter__()


def tearDownModule():
    while _GUARDS:
        cm = _GUARDS.pop()
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


FAKE_MODULES = ("rooms", "registry", "engine_read", "engine_write", "contract", "critic_loop",
                "ingest", "files", "reports", "ics", "search", "export")
RID = "t_aaaa1111"
# момент завершения фиксируется один раз на процесс: тело детали (а значит ETag) не должно зависеть от часов
DONE_AT = int(time.time())


# --- фейки домена ----------------------------------------------------------
class FakeEngineError(RuntimeError):
    """Копия EngineWriteError(code, message_ru, stderr_tail)."""

    def __init__(self, code, message, stderr=""):
        super().__init__(message)
        self.code = code
        self.message_ru = message
        self.stderr_tail = stderr


class State:
    """Состояние фейкового мира комнат и запросов."""

    def __init__(self):
        self.calls: list = []
        self.dispatcher_ok = True
        self.fail_detail = set()          # root_id, на которых request_detail падает
        self.rooms = [
            {"id": "cafe", "title": "Лето", "icon": "cake", "board": "team", "tenant": "room:cafe",
             "orchestrator": "biz-director", "critic": "biz-critic",
             "members": [{"profile": "biz-lawyer", "role": "worker", "note": "", "pinned": False,
                          "added_at": 1}],
             "context_files": [], "project_dir": "", "mode_default": "auto", "critic_pass": False,
             "created_at": 1793000000, "archived": False},
            {"id": "old", "title": "Архив", "icon": "folder", "board": "team", "tenant": "room:old",
             "orchestrator": "default", "critic": None, "members": [], "context_files": [],
             "project_dir": "", "mode_default": "auto", "critic_pass": False,
             "created_at": 1793000001, "archived": True},
        ]
        self.records = {
            "cafe": [
                {"root_id": RID, "slug": "r-20261028-0931-ab12", "title": "Тестовый запрос",
                 "text": "Проверь цены", "created_at": 1793000100, "critic": False, "mode": "auto",
                 "adopted": False, "extra_cards": [], "ingested": {}, "retargeted": False},
                {"root_id": "t_bbbb2222", "slug": "r-20261028-0932-cd34", "title": "В работе",
                 "text": "Сделай отчёт", "created_at": 1793000200, "critic": True, "mode": "auto",
                 "adopted": False, "extra_cards": [], "ingested": {}, "retargeted": False},
            ],
        }
        self.backlog = [
            {"id": RID + ":ns_1", "kind": "next_step", "text": "Снять блокер", "owner": "mikhail",
             "due": "2026-11-01", "status": "open", "source_request": RID, "source_title": "Тестовый запрос",
             "asked_request": None, "created_at": 1, "updated_at": 1},
            {"id": RID + ":q_1", "kind": "open_question", "text": "V1 или V2?", "owner": "mikhail",
             "due": None, "status": "asked", "source_request": RID, "source_title": "Тестовый запрос",
             "asked_request": None, "created_at": 1, "updated_at": 1},
        ]
        self.journal = [
            {"id": "e1", "at": 3, "kind": "request_done", "room": "cafe", "request_id": RID,
             "task_id": RID, "actor": "system", "text": "Запрос завершён", "detail": {}},
        ]
        self.reports = [
            {"key": "unit-economics", "title": "Юнит-экономика", "kind": "spec",
             "path": "C:/reports/cafe/unit-economics/report.json", "request_id": RID, "exists": True},
            {"key": "freeform", "title": "Свободный HTML", "kind": "html",
             "path": "C:/reports/cafe/freeform/report.html", "request_id": RID, "exists": True},
        ]
        self.status = {RID: "done", "t_bbbb2222": "working"}
        self.ingest_calls: list = []
        self.critic_cards: list = []
        self.step_calls: list = []
        self.mirror_calls: list = []
        self.created_requests: list = []
        self.acted: list = []


def _summary(state: State, root_id: str) -> dict:
    status = state.status.get(root_id, "queued")
    rec = next((r for r in state.records.get("cafe", []) if r["root_id"] == root_id), {})
    return {"id": root_id, "room": "cafe", "title": rec.get("title") or "Запрос", "status": status,
            "status_reason": None, "mode": "auto",
            "critic": {"enabled": bool(rec.get("critic")), "state": "off", "verdict": None, "round": 0},
            "created_at": rec.get("created_at") or 1, "started_at": 1 if status != "queued" else None,
            "finished_at": (DONE_AT if status == "done" else None),
            "duration_s": 1 if status == "done" else None,
            "progress": {"done": 1, "total": 1, "percent": 100 if status == "done" else 50},
            "assignee": "biz-director", "needs_you": status == "needs_you",
            "preview": (rec.get("text") or "")[:200], "archived": False}


def _detail(state: State, root_id: str, room_id="cafe") -> dict:
    if root_id in state.fail_detail:
        raise FakeEngineError("engine_error", "БД не читается")
    summary = _summary(state, root_id)
    rec = next((r for r in state.records.get(room_id, []) if r["root_id"] == root_id), {})
    summary.update({
        "text": rec.get("text") or "Проверь цены",
        "dispatcher": {"ok": True, "warning": None},
        "synthesis": ({"state": "ready", "text_md": "# Ответ\n\nГотово", "author": "biz-director",
                       "at": 2, "source_task": root_id, "legacy": False, "version": 1, "history": []}
                      if summary["status"] == "done" else
                      {"state": "pending", "text_md": "", "author": None, "at": None,
                       "source_task": None, "legacy": False, "version": 0, "history": []}),
        "next_steps": [{"id": "ns_1", "text": "Шаг", "owner": None, "due": None, "state": "open"}],
        "open_questions": [], "decisions": [], "future_questions": [], "artifacts": [],
        "stage": {"current": "idea", "phases": [
            {"id": "idea", "title": "Идея", "status": "done", "note": ""},
            {"id": "legal", "title": "Легализация", "status": "active", "note": ""}]},
        "tree": [{"id": root_id, "parent_ids": [], "title": rec.get("title") or "Запрос",
                  "assignee": "biz-director", "role": "root", "status": summary["status"],
                  "started_at": 1, "finished_at": 2, "summary_md": "# Ответ", "metadata": {},
                  "comments": [], "runs": [], "needs_human": False, "error": None,
                  "outside_room": False}],
        "timeline": [], "warnings": [],
        "critic": {"enabled": bool(rec.get("critic")), "round": 0, "max_rounds": 1, "state": "off",
                   "verdict": None, "issues": [], "task_id": None},
    })
    return summary


def build_fakes(state: State) -> dict:
    """Собрать модули-фейки с сигнатурами реальных модулей."""
    mods = {name: ModuleType("server." + name) for name in FAKE_MODULES}

    def log(*args):
        state.calls.append(args)

    rooms = mods["rooms"]
    rooms.list_rooms = lambda cfg, include_archived=True: [
        dict(r) for r in state.rooms if include_archived or not r.get("archived")]
    rooms.get_room = lambda cfg, room_id: next(
        (dict(r) for r in state.rooms if r["id"] == room_id), None)
    rooms.seed_defaults = lambda cfg: state.calls.append(("seed_defaults",))

    def create_room(cfg, data):
        log("create_room", data.get("title"))
        room = {"id": data.get("id") or "newroom", "title": data.get("title"), "icon": "folder",
                "board": "team", "tenant": "room:newroom", "orchestrator": data.get("orchestrator"),
                "critic": data.get("critic"), "members": data.get("members") or [],
                "context_files": [], "project_dir": "", "mode_default": "auto", "critic_pass": False,
                "created_at": int(time.time()), "archived": False}
        state.rooms.append(room)
        return room

    def update_room(cfg, room_id, patch):
        room = next(r for r in state.rooms if r["id"] == room_id)
        room.update(patch)
        return dict(room)

    def add_member(cfg, room_id, member):
        room = next(r for r in state.rooms if r["id"] == room_id)
        new = {"profile": member["profile"], "role": member.get("role") or "worker",
               "note": member.get("note") or "", "pinned": bool(member.get("pinned")), "added_at": 1}
        room["members"].append(new)
        return {"member": new, "members": room["members"]}

    def remove_member(cfg, room_id, profile):
        room = next(r for r in state.rooms if r["id"] == room_id)
        room["members"] = [m for m in room["members"] if m["profile"] != profile]
        return {"ok": True, "members": room["members"]}

    def archive_room(cfg, room_id):
        room = next(r for r in state.rooms if r["id"] == room_id)
        changed = not room.get("archived")
        room["archived"] = True
        return {"room": dict(room), "changed": changed}

    rooms.create_room = create_room
    rooms.update_room = update_room
    rooms.add_member = add_member
    rooms.remove_member = remove_member
    rooms.archive_room = archive_room

    reg = mods["registry"]
    def add_request(cfg, room_id, rec):
        rows = state.records.setdefault(room_id, [])
        for idx, existing in enumerate(rows):          # как настоящий реестр: upsert по root_id
            if existing.get("root_id") == rec.get("root_id"):
                rows[idx] = {**existing, **{k: v for k, v in rec.items() if v not in ("", None, [], {})}}
                state.calls.append(("add_request", rec.get("root_id")))
                return rows[idx]
        rows.append(dict(rec))
        state.calls.append(("add_request", rec.get("root_id")))
        return rows[-1]

    reg.add_request = add_request
    reg.list_requests = lambda cfg, room_id, limit=50: [dict(r) for r in state.records.get(room_id, [])][:limit]
    reg.get = lambda cfg, room_id, root_id: next(
        (r for r in state.records.get(room_id, []) if r["root_id"] == root_id), None)
    reg.all_registered_ids = lambda cfg, board=None: {r["root_id"] for recs in state.records.values()
                                                      for r in recs}
    reg.add_extra_card = lambda cfg, room_id, root_id, card: state.calls.append(("add_extra_card", root_id))

    def mark_ingested(cfg, room_id, root_id, run_id):
        for rec in state.records.get(room_id, []):
            if rec["root_id"] == root_id:
                rec.setdefault("ingested", {})[run_id] = True
                return True
        return False

    reg.mark_ingested = mark_ingested

    er = mods["engine_read"]
    er.health = lambda cfg, board="team": {"ok": True, "warning": None, "board": board,
                                          "db": "kanban.db", "schema_ok": True, "schema_version": 3,
                                          "tasks": {"total": 61, "done": 54, "blocked": 2, "active": 0},
                                          "cli": {"ok": True, "path": "hermes"}}
    er.dispatcher_status = lambda cfg: ({"ok": True, "running": True, "pid": 9608, "state": "running",
                                         "age_s": 5, "updated_at": 1, "warning": None}
                                        if state.dispatcher_ok else
                                        {"ok": False, "running": False, "pid": None, "state": "stopped",
                                         "age_s": None, "updated_at": None,
                                         "warning": "Диспетчер не запущен"})
    er.request_summary = lambda cfg, board, root_id, extra_cards=None, room=None: _summary(state, root_id)
    er.request_detail = lambda cfg, board, root_id, room=None, extra_cards=None: _detail(state, root_id, room)
    er.team_state = lambda cfg, board, members, tenant=None: [
        {"profile": p, "title": p, "role": "worker", "note": "", "pinned": False, "state": "idle",
         "current": None, "last": None, "counts": {"done": 0, "blocked": 0, "active": 0}} for p in members]
    er.list_candidates = lambda cfg, board, registered_ids: []

    ew = mods["engine_write"]
    ew.list_profiles = lambda cfg: [{"profile": "biz-director", "title": "Директор", "tags": [],
                                     "kind": "director", "available": True},
                                    {"profile": "biz-lawyer", "title": "Юрист", "tags": [],
                                     "kind": "worker", "available": True}]
    ew.create_request = lambda cfg, room, slug, title, body: (
        state.created_requests.append({"slug": slug, "title": title, "body": body, "room": room["id"]}),
        {"root_id": "t_cccc3333", "warning": None})[1]
    KNOWN = {"dispatch", "comment", "answer", "unblock", "reassign", "rework", "retry", "archive",
             "decompose", "create_card"}

    def act(cfg, board, root_id, action, **kw):
        if action not in KNOWN:
            raise FakeEngineError("bad_request", "Неизвестное действие: %s" % action)
        state.acted.append({"root_id": root_id, "action": action, "kw": kw})
        payload = {"ok": True, "detail": "сделано", "cmd_ms": 1}
        if action == "decompose":
            payload["task_id"] = root_id
        return payload

    ew.act = act
    ew.adopt = lambda cfg, room, task_id: {"ok": True}

    mods["contract"].build_request_body = lambda room, text, slug, cfg, attach=None: "BODY:" + text
    mods["contract"].build_critic_body = lambda room, text, answer, cfg: "CRITIC"
    mods["contract"].build_revision_body = lambda room, text, answer, issues=None, user_note=None, cfg=None: "REVISION"

    mods["critic_loop"].run_step = lambda cfg, room, record, detail: state.step_calls.append(
        record.get("root_id"))
    mods["critic_loop"].plan = lambda room, record, detail, now: []

    def manual_critique(cfg, room, record, detail, text=None):
        state.critic_cards.append((record.get("root_id"), "critic"))
        return {"ok": True, "task_id": "t_crit0001", "detail": "Создана карточка критика."}

    def manual_revision(cfg, room, record, detail, text=None):
        state.critic_cards.append((record.get("root_id"), "revision"))
        return {"ok": True, "task_id": "t_rev00001", "detail": "Создана карточка доработки."}

    mods["critic_loop"].manual_critique = manual_critique
    mods["critic_loop"].manual_revision = manual_revision

    ing = mods["ingest"]
    def _version_key(detail):
        syn = (detail or {}).get("synthesis") or {}
        return "v%s@%s" % (syn.get("version") or 0, syn.get("at") or "")
    ing.version_key = _version_key
    def _ingest_request(cfg, room, detail):
        state.ingest_calls.append(detail.get("id"))
        reg.mark_ingested(cfg, room.get("id"), detail.get("id"), _version_key(detail))
        return {"changed": True, "added": {}, "skipped": None}
    ing.ingest_request = _ingest_request
    ing.write_vault_mirror = lambda cfg, room_id: state.mirror_calls.append(room_id)
    ing.roadmap_view = lambda cfg, room_id: {"room": room_id, "updated_at": 1, "current": None,
                                             "phases": [], "decisions": []}
    ing.backlog_view = lambda cfg, room_id: {"room": room_id, "updated_at": 1,
                                            "counts": {"open": 1, "asked": 1, "done": 0, "dismissed": 0,
                                                       "total": 2},
                                            "items": [dict(i) for i in state.backlog]}
    ing.journal_view = lambda cfg, room_id, limit=50, q=None: {"room": room_id, "items": state.journal,
                                                              "next_cursor": None, "has_more": False}

    def set_backlog_status(cfg, room_id, item_id, status, due=None):
        item = next((i for i in state.backlog if i["id"] == item_id), None)
        if item is None:
            raise FakeEngineError("not_found", "Элемент бэклога не найден")
        item.update({"status": status, "due": due})
        return item

    ing.set_backlog_status = set_backlog_status

    def ask_item(cfg, room_id, item_id, text=None):
        item = next((i for i in state.backlog if i["id"] == item_id), None)
        if item is None:
            raise FakeEngineError("not_found", "Элемент бэклога не найден")
        return "%s\n\nИсточник: «%s»." % ((text or item["text"]), item.get("source_title") or "")

    def mark_asked(cfg, room_id, item_id, root_id=None):
        item = next((i for i in state.backlog if i["id"] == item_id), None)
        if item is None:
            raise FakeEngineError("not_found", "Элемент бэклога не найден")
        item.update({"status": "asked", "asked_request": root_id})
        return dict(item)

    ing.ask_item = ask_item
    ing.mark_asked = mark_asked

    files = mods["files"]
    files.open_file = lambda cfg, path: (200, {"Content-Type": "text/plain; charset=utf-8"}, b"hello")
    files.reveal = lambda cfg, path: True

    rep = mods["reports"]
    rep.list_reports = lambda cfg, room_id: [dict(r) for r in state.reports]
    rep.find_report = lambda cfg, room_id, key: next(
        (dict(r) for r in state.reports if r["key"] == key), None)
    rep.load_report = lambda cfg, path: (
        {"kind": "html", "url": "/reports/cafe/freeform/report.html", "title": "Свободный HTML",
         "path": str(path), "errors": [], "warnings": []}
        if str(path).endswith("report.html")
        else {"kind": "spec", "spec": {"version": 1, "title": "Юнит-экономика", "blocks": []},
              "errors": [], "warnings": [], "path": str(path)})
    rep.html_headers = lambda: {"Content-Type": "text/html; charset=utf-8",
                                "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'"}
    rep.serve_report_file = lambda cfg, room, key, rel: (
        200, {"Content-Type": "text/html; charset=utf-8",
              "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'"}, b"<html>ok</html>")

    mods["ics"].ics_for_items = lambda cfg, room_id, ids=None, all_open=False, meta=None: (
        "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    mods["ics"].make_ics = lambda items, room_title: "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    mods["search"].query = lambda cfg, q, room=None, limit=30: [
        {"kind": "request", "id": RID, "room": "cafe", "title": "Тестовый запрос", "snippet": "…",
         "score": 1.0, "at": 2, "route": "#/room/cafe/request/" + RID}]

    mods["export"].export_request_md = lambda detail: "# Экспорт\n\n" + detail.get("title", "")
    mods["export"].export_request_html = lambda cfg, detail: "<html>export</html>"
    return mods


def make_cfg(base: Path):
    cfg = config.Cfg(hermes_home=base / "hermes", kanban_home=base / "kanban",
                     data_dir=base / "data", vault_dir=base / "vault", port=0,
                     hermes_cli="hermes", sandbox=True,
                     allowed_roots=[base / "data", base / "vault"])
    cfg.ensure_dirs()
    (base / "vault").mkdir(parents=True, exist_ok=True)
    return cfg


class HttpBase(unittest.TestCase):
    """Поднимает реальный ThreadingHTTPServer с фейковыми модулями домена."""

    state: State

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="tr-test-")
        cls.base = Path(cls.tmp.name)
        cls.cfg = make_cfg(cls.base)
        cls.state = State()
        cls.fakes = build_fakes(cls.state)
        cls._saved = {}
        for name, mod in cls.fakes.items():
            key = "server." + name
            cls._saved[key] = sys.modules.get(key)
            sys.modules[key] = mod
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
        for key, value in cls._saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        cls.tmp.cleanup()

    # --- HTTP-клиент ---
    def call(self, method, path, body=None, headers=None, raw_body=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        hdrs = dict(headers or {})
        payload = None
        if raw_body is not None:
            payload = raw_body
            if content_type:
                hdrs.setdefault("Content-Type", content_type)
        elif body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", content_type)
        conn.request(method, quote(path, safe="/?=&%:,+"), body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        info = {"status": resp.status, "headers": dict(resp.getheaders()), "raw": raw}
        ctype = resp.getheader("Content-Type") or ""
        if "json" in ctype and raw:
            info["json"] = json.loads(raw.decode("utf-8"))
        conn.close()
        return info

    def get(self, path, headers=None):
        return self.call("GET", path, headers=headers)

    def post(self, path, body=None, headers=None):
        return self.call("POST", path, body=body if body is not None else {}, headers=headers)

    def patch(self, path, body):
        return self.call("PATCH", path, body=body)

    def delete(self, path):
        return self.call("DELETE", path)


# --- безопасность и транспорт ---------------------------------------------
class SecurityTests(HttpBase):
    def test_health_ok_and_security_headers(self):
        res = self.get("/api/health")
        self.assertEqual(res["status"], 200)
        body = res["json"]
        self.assertTrue(body["ok"])
        self.assertEqual(body["app"]["name"], "team-room")
        self.assertEqual(body["engine"]["board"], "team")
        self.assertIn("watcher", body)
        self.assertEqual(res["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertEqual(res["headers"]["Referrer-Policy"], "no-referrer")
        self.assertEqual(res["headers"]["Cache-Control"], "no-store")

    def test_bad_host_rejected(self):
        res = self.get("/api/health", headers={"Host": "evil.example"})
        self.assertEqual(res["status"], 403)
        self.assertEqual(res["json"]["error"]["code"], "forbidden")
        self.assertEqual(res["json"]["error"]["detail"]["reason"], "bad_host")
        self.assertNotIn("\n", res["json"]["error"]["message"])

    def test_bad_origin_and_referer_rejected(self):
        res = self.post("/api/open", {"path": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(res["status"], 403)
        self.assertEqual(res["json"]["error"]["detail"]["reason"], "bad_origin")
        res = self.post("/api/open", {"path": "x"},
                        headers={"Referer": "https://evil.example/x"})
        self.assertEqual(res["status"], 403)
        self.assertEqual(res["json"]["error"]["detail"]["reason"], "bad_origin")

    def test_our_origin_allowed(self):
        res = self.post("/api/open", {"path": "x"},
                        headers={"Origin": "http://127.0.0.1:%d" % self.port})
        # путь «x» вне белых корней → 403 forbidden уже по существу, но не bad_origin
        self.assertEqual(res["status"], 403)
        self.assertNotEqual(res["json"]["error"]["detail"].get("reason"), "bad_origin")

    def test_static_and_spa(self):
        index = self.get("/")
        self.assertEqual(index["status"], 200)
        self.assertIn("text/html", index["headers"]["Content-Type"])
        self.assertIn("default-src 'self'", index["headers"]["Content-Security-Policy"])
        spa = self.get("/room/cafe/request/t_aaaa1111")
        self.assertEqual(spa["status"], 200)
        self.assertIn(b"<html", spa["raw"].lower())
        self.assertEqual(self.get("/_kit.html")["status"], 200)

    def test_static_traversal_forbidden(self):
        for path in ("/%2e%2e/server/config.py", "/..%2fserver%2fapp.py", "/../server/app.py"):
            res = self.get(path)
            self.assertEqual(res["status"], 403, path)
            self.assertEqual(res["json"]["error"]["code"], "forbidden")

    def test_body_too_large(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.putrequest("POST", "/api/open", skip_accept_encoding=True)
        conn.putheader("Host", "127.0.0.1:%d" % self.port)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(http_util.MAX_BODY + 10))
        conn.endheaders()
        conn.send(b"{" + b" " * 100)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        self.assertEqual(resp.status, 413)
        self.assertEqual(json.loads(raw.decode())["error"]["code"], "bad_request")

    def test_bad_json_and_wrong_type(self):
        res = self.call("POST", "/api/open", raw_body=b"{not json")
        self.assertEqual(res["status"], 400)
        self.assertEqual(res["json"]["error"]["code"], "bad_request")
        res = self.call("POST", "/api/open", raw_body=b"[1,2,3]")
        self.assertEqual(res["status"], 400)
        res = self.call("POST", "/api/open", raw_body=b"x=1", content_type="application/x-www-form-urlencoded")
        self.assertEqual(res["status"], 415)

    def test_unknown_endpoint_and_methods(self):
        self.assertEqual(self.get("/api/nope")["status"], 404)
        self.assertEqual(self.get("/api/rooms/cafe/requests")["status"], 200)
        res = self.call("DELETE", "/api/health")
        self.assertEqual(res["status"], 405)
        res = self.call("OPTIONS", "/api/health")
        self.assertEqual(res["status"], 405)
        self.assertEqual(res["json"]["error"]["code"], "bad_request")

    def test_unhandled_exception_is_json_500_and_logged(self):
        rooms_mod = self.fakes["rooms"]
        original = rooms_mod.list_rooms

        def boom(cfg, include_archived=True):
            raise RuntimeError("тестовый сбой модуля")

        rooms_mod.list_rooms = boom
        try:
            res = self.get("/api/rooms")
        finally:
            rooms_mod.list_rooms = original
        self.assertEqual(res["status"], 500)
        self.assertEqual(res["json"]["error"]["code"], "engine_error")
        self.assertIn("Внутренняя ошибка", res["json"]["error"]["message"])
        self.assertNotIn("Traceback", res["raw"].decode("utf-8"))
        log = (self.cfg.log_dir / "app.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("тестовый сбой модуля", log)
        self.assertIn("Traceback", log)


# --- роутинг всех эндпоинтов ----------------------------------------------
class RoutingTests(HttpBase):
    def test_get_endpoints(self):
        cases = [
            ("/api/health", ("ok", "app", "engine", "dispatcher", "data", "vault", "watcher")),
            ("/api/config", ("app", "paths", "allowed_roots", "limits", "features")),
            ("/api/profiles", ("items", "count", "dispatcher")),
            ("/api/rooms", ("rooms", "engine", "updated_at")),
            ("/api/rooms/cafe", ("id", "counters", "phase", "dispatcher")),
            ("/api/rooms/cafe/team", ("room", "members", "dispatcher")),
            ("/api/rooms/cafe/roadmap", ("room", "phases", "decisions")),
            ("/api/rooms/cafe/backlog", ("room", "items", "counts")),
            ("/api/rooms/cafe/journal", ("room", "items", "has_more")),
            ("/api/rooms/cafe/reports", ("room", "items")),
            ("/api/rooms/cafe/candidates", ("room", "items", "count")),
            ("/api/rooms/cafe/requests", ("items", "next_cursor", "has_more")),
            ("/api/search?q=цены", ("q", "room", "total", "items")),
        ]
        for path, keys in cases:
            res = self.get(path)
            self.assertEqual(res["status"], 200, path)
            for key in keys:
                self.assertIn(key, res["json"], "%s → %s" % (path, key))

    def test_rooms_include_archived(self):
        default = self.get("/api/rooms")["json"]["rooms"]
        self.assertNotIn("old", [room["id"] for room in default])
        everything = self.get("/api/rooms?include_archived=1")["json"]["rooms"]
        self.assertIn("old", [room["id"] for room in everything])
        archived = next(room for room in everything if room["id"] == "old")
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["counters"], {"working": 0, "needs_you": 0, "done_today": 0})

    def test_room_crud_and_members(self):
        created = self.post("/api/rooms", {"title": "Новая", "orchestrator": "biz-director"})
        self.assertEqual(created["status"], 201)
        self.assertEqual(created["headers"]["Location"], "/api/rooms/newroom")
        patch = self.patch("/api/rooms/newroom", {"title": "Переименована"})
        self.assertEqual(patch["status"], 200)
        self.assertEqual(patch["json"]["title"], "Переименована")
        bad = self.patch("/api/rooms/newroom", {"board": "other"})
        self.assertEqual(bad["status"], 400)
        self.assertIn("board", bad["json"]["error"]["detail"]["fields"])
        # max_rounds правится через PATCH (лимит раундов критика 1..2)
        rounds = self.patch("/api/rooms/newroom", {"max_rounds": 2, "critic_pass": True})
        self.assertEqual(rounds["status"], 200)
        self.assertEqual(rounds["json"]["max_rounds"], 2)
        self.assertTrue(rounds["json"]["critic_pass"])
        # режим только auto: swarm отклоняется
        self.assertEqual(self.patch("/api/rooms/newroom", {"mode_default": "swarm"})["status"], 400)
        member = self.post("/api/rooms/newroom/members", {"profile": "biz-lawyer"})
        self.assertEqual(member["status"], 201)
        self.assertEqual(member["json"]["member"]["profile"], "biz-lawyer")
        dup = self.post("/api/rooms/newroom/members", {"profile": "biz-lawyer"})
        self.assertEqual(dup["status"], 409)
        unknown = self.post("/api/rooms/newroom/members", {"profile": "nobody"})
        self.assertEqual(unknown["status"], 400)
        self.assertIn("available", unknown["json"]["error"]["detail"])
        lead = self.delete("/api/rooms/newroom/members/biz-director")
        self.assertEqual(lead["status"], 409)
        self.assertEqual(lead["json"]["error"]["detail"]["reason"], "room_lead")
        removed = self.delete("/api/rooms/newroom/members/biz-lawyer")
        self.assertEqual(removed["status"], 200)
        archived = self.post("/api/rooms/newroom/archive", {})
        self.assertEqual(archived["status"], 200)
        self.assertTrue(archived["json"]["changed"])
        again = self.post("/api/rooms/newroom/archive", {})
        self.assertFalse(again["json"]["changed"])

    def test_room_detail_etag(self):
        first = self.get("/api/rooms/cafe")
        self.assertEqual(first["status"], 200)
        etag = first["headers"].get("ETag")
        self.assertTrue(etag and etag.startswith('W/"'))
        second = self.get("/api/rooms/cafe", headers={"If-None-Match": etag})
        self.assertEqual(second["status"], 304)
        self.assertEqual(second["raw"], b"")

    def test_request_detail_etag_304(self):
        first = self.get("/api/requests/" + RID)
        self.assertEqual(first["status"], 200)
        self.assertEqual(first["json"]["id"], RID)
        self.assertEqual(first["json"]["synthesis"]["state"], "ready")
        etag = first["headers"]["ETag"]
        self.assertTrue(etag.startswith('W/"'))
        self.assertEqual(first["headers"]["Cache-Control"], "no-cache")
        second = self.get("/api/requests/" + RID, headers={"If-None-Match": etag})
        self.assertEqual(second["status"], 304)
        self.assertEqual(second["raw"], b"")
        self.assertNotIn("Content-Type", second["headers"])
        self.assertEqual(second["headers"]["ETag"], etag)
        star = self.get("/api/requests/" + RID, headers={"If-None-Match": "*"})
        self.assertEqual(star["status"], 304)
        weak = self.get("/api/requests/" + RID, headers={"If-None-Match": '"other", ' + etag})
        self.assertEqual(weak["status"], 304)
        other = self.get("/api/requests/" + RID, headers={"If-None-Match": 'W/"deadbeefdeadbeef"'})
        self.assertEqual(other["status"], 200)

    def test_etag_does_not_depend_on_clock(self):
        """ETag считается по содержательным данным: сдвиг часов не меняет ни ETag, ни 304."""
        paths = ["/api/requests/" + RID, "/api/rooms/cafe"]
        before = {p: self.get(p)["headers"]["ETag"] for p in paths}
        real_time = time.time
        with mock.patch("time.time", side_effect=lambda: real_time() + 7200):
            for path in paths:
                again = self.get(path, headers={"If-None-Match": before[path]})
                self.assertEqual(again["status"], 304, path)
                self.assertEqual(again["headers"]["ETag"], before[path], path)

    def test_unknown_request_404(self):
        res = self.get("/api/requests/t_ffff9999")
        self.assertEqual(res["status"], 404)
        self.assertEqual(res["json"]["error"]["code"], "not_found")

    def test_requests_list_filters_and_paging(self):
        full = self.get("/api/rooms/cafe/requests?limit=100")["json"]
        total = len(full["items"])
        self.assertGreaterEqual(total, 2)
        res = self.get("/api/rooms/cafe/requests?limit=1")
        self.assertEqual(res["status"], 200)
        self.assertEqual(len(res["json"]["items"]), 1)
        self.assertEqual(res["json"]["has_more"], total > 1)
        cursor = res["json"]["next_cursor"]
        self.assertTrue(cursor)
        page2 = self.get("/api/rooms/cafe/requests?limit=1&cursor=" + cursor)
        self.assertEqual(len(page2["json"]["items"]), 1)
        self.assertEqual(page2["json"]["has_more"], total > 2)
        self.assertNotEqual(page2["json"]["items"][0]["id"], res["json"]["items"][0]["id"])
        bad = self.get("/api/rooms/cafe/requests?status=bogus")
        self.assertEqual(bad["status"], 400)
        self.assertIn("allowed", bad["json"]["error"]["detail"])
        bad_limit = self.get("/api/rooms/cafe/requests?limit=999")
        self.assertEqual(bad_limit["status"], 400)
        bad_cursor = self.get("/api/rooms/cafe/requests?cursor=%%%")
        self.assertEqual(bad_cursor["status"], 400)
        filtered = self.get("/api/rooms/cafe/requests?status=done")
        self.assertEqual(filtered["status"], 200)
        self.assertTrue(all(item["status"] == "done" for item in filtered["json"]["items"]))

    def test_create_request_validation_and_success(self):
        empty = self.post("/api/rooms/cafe/requests", {"text": "   "})
        self.assertEqual(empty["status"], 400)
        self.assertEqual(empty["json"]["error"]["detail"]["field"], "text")
        long_text = self.post("/api/rooms/cafe/requests", {"text": "я" * 1_000_001})
        self.assertEqual(long_text["status"], 400)
        self.assertEqual(long_text["json"]["error"]["detail"]["limit"], 1_000_000)
        swarm = self.post("/api/rooms/cafe/requests", {"text": "привет", "mode": "swarm"})
        self.assertEqual(swarm["status"], 400)
        outside = self.post("/api/rooms/cafe/requests", {"text": "привет", "attach": ["C:/Windows/win.ini"]})
        self.assertEqual(outside["status"], 403)
        self.assertIn("path", outside["json"]["error"]["detail"])
        ok = self.post("/api/rooms/cafe/requests", {"text": "Проверь цены на десерты"})
        self.assertEqual(ok["status"], 201)
        self.assertEqual(ok["json"]["root_id"], "t_cccc3333")
        self.assertEqual(ok["headers"]["Location"], "/api/requests/t_cccc3333")
        self.assertIn("request", ok["json"])
        self.assertIn("dispatcher", ok["json"])
        created = self.state.created_requests[-1]
        self.assertRegex(created["slug"], r"^r-\d{8}-\d{4}-[0-9a-f]{4}$")
        self.assertTrue(created["body"].startswith("BODY:"))
        self.assertTrue(any(call[0] == "add_request" for call in self.state.calls))

    def test_create_request_warns_when_dispatcher_down(self):
        self.state.dispatcher_ok = False
        try:
            res = self.post("/api/rooms/cafe/requests", {"text": "Ещё запрос"})
            self.assertEqual(res["status"], 201)
            warning = res["json"]["warning"]
            self.assertIsInstance(warning, dict)
            self.assertEqual(warning["code"], "dispatcher_down")
        finally:
            self.state.dispatcher_ok = True

    def test_actions(self):
        bad = self.post("/api/requests/%s/actions" % RID, {"action": "teleport"})
        self.assertEqual(bad["status"], 400)
        self.assertIn("allowed", bad["json"]["error"]["detail"])
        no_text = self.post("/api/requests/%s/actions" % RID, {"action": "comment"})
        self.assertEqual(no_text["status"], 400)
        comment = self.post("/api/requests/%s/actions" % RID, {"action": "comment", "text": "Молодцы"})
        self.assertEqual(comment["status"], 200)
        self.assertEqual(comment["json"]["action"], "comment")
        self.assertIn("request", comment["json"])
        self.assertEqual(self.state.acted[-1]["kw"]["text"], "Молодцы")
        missing = self.post("/api/requests/%s/actions" % RID, {"action": "reassign", "task_id": RID})
        self.assertEqual(missing["status"], 400)
        # D9: decompose только для queued/triage; завершённый запрос — 409
        decompose = self.post("/api/requests/%s/actions" % RID, {"action": "decompose"})
        self.assertEqual(decompose["status"], 409)
        self.assertEqual(decompose["json"]["error"]["detail"]["reason"], "already_decomposed")
        self.state.status[RID] = "queued"
        try:
            decompose = self.post("/api/requests/%s/actions" % RID, {"action": "decompose"})
            self.assertEqual(decompose["status"], 200)
        finally:
            self.state.status[RID] = "done"
        unknown = self.post("/api/requests/t_ffff9999/actions", {"action": "comment", "text": "x"})
        self.assertEqual(unknown["status"], 404)

    def test_clone_creates_new_request(self):
        before = len(self.state.created_requests)
        res = self.post("/api/requests/%s/actions" % RID, {"action": "clone"})
        self.assertEqual(res["status"], 201)
        self.assertEqual(res["json"]["root_id"], "t_cccc3333")
        self.assertEqual(res["headers"]["Location"], "/api/requests/t_cccc3333")
        self.assertEqual(len(self.state.created_requests), before + 1)

    def test_critique_and_revise_create_cards(self):
        critique = self.post("/api/requests/%s/actions" % RID, {"action": "critique"})
        self.assertEqual(critique["status"], 200)
        self.assertEqual(critique["json"]["task_id"], "t_crit0001")
        self.assertEqual(self.state.critic_cards[-1], (RID, "critic"))
        revise = self.post("/api/requests/%s/actions" % RID, {"action": "revise", "text": "Переделай"})
        self.assertEqual(revise["status"], 200)
        self.assertEqual(self.state.critic_cards[-1], (RID, "revision"))

    def test_backlog_set_and_ask(self):
        bad = self.post("/api/rooms/cafe/backlog/%s:ns_1" % RID, {"status": "bogus"})
        self.assertEqual(bad["status"], 400)
        ok = self.post("/api/rooms/cafe/backlog/%s:ns_1" % RID, {"status": "done", "due": "2026-11-05"})
        self.assertEqual(ok["status"], 200)
        self.assertEqual(ok["json"]["item"]["status"], "done")
        self.assertIn("counts", ok["json"])
        bad_due = self.post("/api/rooms/cafe/backlog/%s:ns_1" % RID, {"status": "open", "due": "05.11.2026"})
        self.assertEqual(bad_due["status"], 400)
        missing = self.post("/api/rooms/cafe/backlog/nope:ns_9", {"status": "done"})
        self.assertEqual(missing["status"], 404)
        asked = self.post("/api/rooms/cafe/backlog/%s:q_1/ask" % RID, {})
        self.assertEqual(asked["status"], 201)
        self.assertEqual(asked["json"]["request_id"], "t_cccc3333")
        self.assertEqual(asked["json"]["root_id"], "t_cccc3333")
        self.assertEqual(asked["json"]["item"]["status"], "asked")
        self.assertEqual(asked["json"]["item"]["asked_request"], "t_cccc3333")
        self.assertIn("request", asked["json"])

    def test_reports(self):
        listing = self.get("/api/rooms/cafe/reports")
        self.assertEqual(listing["status"], 200)
        self.assertTrue(listing["json"]["items"])
        spec = self.get("/api/reports/cafe/unit-economics")
        self.assertEqual(spec["status"], 200)
        self.assertEqual(spec["json"]["title"], "Юнит-экономика")
        etag = spec["headers"]["ETag"]
        self.assertEqual(self.get("/api/reports/cafe/unit-economics",
                                 headers={"If-None-Match": etag})["status"], 304)
        html = self.get("/api/reports/cafe/freeform")
        self.assertEqual(html["status"], 200)
        self.assertEqual(html["json"]["kind"], "html")
        self.assertTrue(html["json"]["url"].startswith("/reports/"))
        served = self.get("/reports/cafe/freeform/report.html")
        self.assertEqual(served["status"], 200)
        self.assertIn("sandbox", served["headers"]["Content-Security-Policy"])
        missing = self.get("/api/reports/cafe/nope")
        self.assertEqual(missing["status"], 404)

    def test_files_and_open(self):
        outside = self.get("/api/file?path=C:/Windows/win.ini")
        self.assertEqual(outside["status"], 403)
        self.assertEqual(outside["json"]["error"]["detail"]["reason"], "outside_roots")
        no_path = self.get("/api/file")
        self.assertEqual(no_path["status"], 400)
        missing = self.get("/api/file?path=" + str(self.base / "data" / "nope.md"))
        self.assertEqual(missing["status"], 404)
        target = self.base / "data" / "note.md"
        target.write_text("# привет", encoding="utf-8")
        ok = self.get("/api/file?path=" + str(target))
        self.assertEqual(ok["status"], 200)
        self.assertEqual(ok["raw"], b"hello")
        denied = self.base / "data" / "auth.json"
        denied.write_text("{}", encoding="utf-8")
        res = self.get("/api/file?path=" + str(denied))
        self.assertEqual(res["status"], 403)
        self.assertEqual(res["json"]["error"]["detail"]["reason"], "blacklisted")
        opened = self.post("/api/open", {"path": str(target)})
        self.assertEqual(opened["status"], 200)
        self.assertTrue(opened["json"]["revealed"])
        self.assertEqual(self.post("/api/open", {})["status"], 400)

    def test_search_validation(self):
        self.assertEqual(self.get("/api/search?q=x")["status"], 400)
        ok = self.get("/api/search?q=цены&limit=5")
        self.assertEqual(ok["status"], 200)
        self.assertEqual(ok["json"]["total"], 1)
        self.assertEqual(self.get("/api/search?q=цены&room=nope")["status"], 404)

    def test_ics(self):
        no_room = self.get("/api/ics")
        self.assertEqual(no_room["status"], 400)
        no_items = self.get("/api/ics?room=cafe")
        self.assertEqual(no_items["status"], 400)
        ok = self.get("/api/ics?room=cafe&all=1")
        self.assertEqual(ok["status"], 200)
        self.assertIn("text/calendar", ok["headers"]["Content-Type"])
        self.assertIn("attachment", ok["headers"]["Content-Disposition"])
        self.assertIn(b"BEGIN:VCALENDAR", ok["raw"])

    def test_export(self):
        md = self.get("/api/export/request/%s.md" % RID)
        self.assertEqual(md["status"], 200)
        self.assertIn("text/markdown", md["headers"]["Content-Type"])
        self.assertIn("attachment", md["headers"]["Content-Disposition"])
        html = self.get("/api/export/request/%s.html" % RID)
        self.assertEqual(html["status"], 200)
        self.assertIn("text/html", html["headers"]["Content-Type"])
        self.assertEqual(self.get("/api/export/request/t_ffff9999.md")["status"], 404)

    def test_consolidate(self):
        res = self.post("/api/rooms/cafe/consolidate", {"note": "и свежие решения"})
        self.assertEqual(res["status"], 201)
        self.assertTrue(res["json"]["root_id"])
        self.assertIn("STATE.md", self.state.created_requests[-1]["title"])

    def test_adopt(self):
        res = self.post("/api/adopt", {"room": "cafe", "task_id": "t_eeff5566"})
        self.assertEqual(res["status"], 200)
        self.assertTrue(res["json"]["ok"])
        bad = self.post("/api/adopt", {"room": "cafe", "task_id": "not-an-id"})
        self.assertEqual(bad["status"], 400)

    def test_profiles_and_kick(self):
        profiles = self.get("/api/profiles")
        self.assertEqual(profiles["json"]["count"], 2)
        # D2: в песочнице проход диспетчера запрещён — CLI не вызывается вовсе
        before = len(self.state.acted)
        kick = self.post("/api/dispatcher/kick", {})
        self.assertEqual(kick["status"], 409)
        self.assertEqual(kick["json"]["error"]["code"], "sandbox_no_dispatch")
        self.assertEqual(len(self.state.acted), before)
        real_cfg = self.server.http_app.cfg
        self.server.http_app.cfg = dataclasses.replace(real_cfg, sandbox=False)
        try:
            kick = self.post("/api/dispatcher/kick", {})
            self.assertEqual(kick["status"], 200)
            self.assertIn("spawned", kick["json"])
            self.assertEqual(self.state.acted[-1]["action"], "dispatch")
        finally:
            self.server.http_app.cfg = real_cfg


# --- watcher ---------------------------------------------------------------
class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-watch-")
        self.base = Path(self.tmp.name)
        self.cfg = make_cfg(self.base)
        self.state = State()
        self.fakes = build_fakes(self.state)
        self._saved = {}
        for name, mod in self.fakes.items():
            key = "server." + name
            self._saved[key] = sys.modules.get(key)
            sys.modules[key] = mod
        self.app = app_mod.App(self.cfg, port=0)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        self.tmp.cleanup()

    def test_tick_once_order_and_idempotency(self):
        stats = self.app.watcher.tick_once()
        self.assertEqual(stats["rooms"], 1)              # архивная комната пропущена
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(stats["errors"], 0)
        # цикл критика вызывается для ВСЕХ запросов (и для завершённого — ingest после него)
        self.assertEqual(self.state.step_calls, ["t_bbbb2222", RID])
        self.assertEqual(self.state.ingest_calls, [RID])
        self.assertEqual(self.state.mirror_calls, ["cafe"])
        self.assertTrue(stats["active"])
        cache = self.app.watcher.summary_for("cafe")
        self.assertEqual(cache["counters"], {"working": 1, "needs_you": 0, "done_today": 1})
        self.assertEqual(cache["phase"]["id"], "idea")
        # повторный тик не дублирует ingest
        second = self.app.watcher.tick_once()
        self.assertEqual(second["ingested"], 0)
        self.assertEqual(self.state.ingest_calls, [RID])
        self.assertEqual(self.state.mirror_calls, ["cafe"])
        self.assertIsNotNone(self.app.watcher.last_run_at)
        self.assertIsNone(self.app.watcher.last_error)

    def test_error_in_one_request_does_not_break_tick(self):
        self.state.fail_detail = {"t_bbbb2222"}
        stats = self.app.watcher.tick_once()
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.state.ingest_calls, [RID])      # второй запрос обработан
        self.assertIn("t_bbbb2222", self.app.watcher.last_error or "")
        self.assertTrue((self.cfg.log_dir / "app.log").exists())
        log_text = (self.cfg.log_dir / "app.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("watcher", log_text)

    def test_tick_once_survives_missing_rooms_module(self):
        sys.modules["server.rooms"] = None  # имитируем «модуль ещё не готов»
        try:
            self.app._mods.pop("rooms", None)
            stats = self.app.watcher.tick_once()
            self.assertEqual(stats["errors"], 1)
            self.assertIsNotNone(self.app.watcher.last_error)
        finally:
            sys.modules["server.rooms"] = self.fakes["rooms"]


# --- отсутствующие модули и реальный сервер -------------------------------
class MissingModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tr-missing-")
        self.cfg = make_cfg(Path(self.tmp.name))
        self.app = app_mod.App(self.cfg, port=0)
        self.app._load = self._missing

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _missing(name):
        raise ModuleNotFoundError("No module named '%s'" % name, name="server." + name.split(".")[-1])

    def test_501_engine_error(self):
        with self.assertRaises(http_util.ApiError) as ctx:
            self.app.dispatch("GET", "/api/rooms", {}, {})
        self.assertEqual(ctx.exception.status, 501)
        self.assertEqual(ctx.exception.code, "engine_error")
        self.assertIn("ещё не готов", ctx.exception.message)

    def test_health_still_answers(self):
        status, payload, _ = self.app.dispatch("GET", "/api/health", {}, {})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["engine"]["schema_ok"], False)
        self.assertTrue(payload["warnings"])

    def test_config_and_static_work_without_modules(self):
        status, payload, _ = self.app.dispatch("GET", "/api/config", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["app"]["name"], "team-room")
        code, body, ctype, headers = http_util.serve_static(self.cfg.web_dir, "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)


class RealServerTests(unittest.TestCase):
    """Настоящий сервер на свободном порту: статика, health, шапки (без фейков)."""

    def test_real_server_boots_and_serves(self):
        tmp = tempfile.TemporaryDirectory(prefix="tr-real-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        cfg = make_cfg(base)
        cfg.kanban_home = base / "kanban"          # пустая песочница канбана
        server = app_mod.create_app(cfg, start_watcher=False, port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertEqual(body["app"]["port"], port)
            self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
            self.assertIn("warnings", body)
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", "/")
            resp = conn.getresponse()
            html = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertIn(b"<html", html.lower())
            self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy") or "")
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("GET", "/api/rooms")
            resp = conn.getresponse()
            rooms = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertIn("rooms", rooms)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _serve(self, base: Path):
        cfg = make_cfg(base)
        cfg.kanban_home = base / "kanban"
        server = app_mod.create_app(cfg, start_watcher=False, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        return cfg, server

    def test_client_disconnects_are_silent(self):
        """Обрыв соединения клиентом (RST в разные моменты) не пишет трейсбеков в stderr."""
        tmp = tempfile.TemporaryDirectory(prefix="tr-rst-")
        self.addCleanup(tmp.cleanup)
        cfg, server = self._serve(Path(tmp.name))
        port = server.server_address[1]

        def reset(sock):                       # закрытие с RST — как закрытая вкладка браузера
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()

        host = ("Host: 127.0.0.1:%d\r\n" % port).encode()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            sock = socket.create_connection(("127.0.0.1", port))      # ничего не отправил
            reset(sock)
            sock = socket.create_connection(("127.0.0.1", port))      # keep-alive: ответ получен, ждём следующий
            sock.sendall(b"GET /api/health HTTP/1.1\r\n" + host + b"\r\n")
            sock.recv(65536)
            reset(sock)
            sock = socket.create_connection(("127.0.0.1", port))      # тело оборвано на полуслове
            sock.sendall(b"POST /api/rooms HTTP/1.1\r\n" + host + b"Content-Length: 500\r\n\r\n{")
            reset(sock)
            sock = socket.create_connection(("127.0.0.1", port))      # запрос ушёл, ответ не читаем
            sock.sendall(b"GET /api/rooms HTTP/1.1\r\n" + host + b"\r\n")
            reset(sock)
            sock = socket.create_connection(("127.0.0.1", port))      # штатное закрытие без данных
            sock.close()
            time.sleep(0.6)                                           # дать потокам обработчиков отработать
        self.assertEqual(stderr.getvalue(), "")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)  # сервер жив
        conn.request("GET", "/api/health")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        log = Path(cfg.log_dir) / "app.log"
        self.assertNotIn("Connection", log.read_text(encoding="utf-8") if log.exists() else "")

    def test_unexpected_handler_error_goes_to_app_log(self):
        """Не-обрывы (баг в обработчике) не шумят в stderr, а попадают в data/logs/app.log."""
        tmp = tempfile.TemporaryDirectory(prefix="tr-err-")
        self.addCleanup(tmp.cleanup)
        cfg, server = self._serve(Path(tmp.name))
        port = server.server_address[1]
        log = Path(cfg.log_dir) / "app.log"
        stderr = io.StringIO()
        with mock.patch.object(http_util.Handler, "handle", side_effect=RuntimeError("boom-test")), \
                contextlib.redirect_stderr(stderr):
            sock = socket.create_connection(("127.0.0.1", port))
            sock.close()
            deadline = time.time() + 5
            while time.time() < deadline and not (log.exists() and "boom-test" in log.read_text(encoding="utf-8")):
                time.sleep(0.05)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("boom-test", log.read_text(encoding="utf-8"))
        self.assertIn("RuntimeError", log.read_text(encoding="utf-8"))

    def test_is_client_disconnect(self):
        for exc in (ConnectionResetError(), ConnectionAbortedError(), BrokenPipeError(),
                    OSError(errno.ECONNRESET, "reset")):
            self.assertTrue(http_util.is_client_disconnect(exc), repr(exc))
        for exc in (RuntimeError("x"), ValueError("y"), OSError(errno.ENOENT, "no file"), None):
            self.assertFalse(http_util.is_client_disconnect(exc), repr(exc))


try:  # песочницу пишет другой исполнитель — тесты не должны падать без неё
    from tests.helpers import Sandbox as SandboxHelper      # noqa: E402
except Exception:                                            # noqa: BLE001
    SandboxHelper = None


@unittest.skipUnless(SandboxHelper is not None, "tests/helpers.py (Sandbox) ещё не готов")
class SandboxIntegrationTests(unittest.TestCase):
    """Реальные модули в изолированной песочнице: чтение без записи в живую доску."""

    def test_real_stack_on_sandbox_board(self):
        with SandboxHelper() as sb:
            server = app_mod.create_app(sb.cfg, start_watcher=False, port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()

            def get(path, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                conn.request("GET", path, headers=headers or {})
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp, raw

            try:
                resp, raw = get("/api/health")
                health = json.loads(raw.decode("utf-8"))
                self.assertEqual(resp.status, 200)
                self.assertTrue(health["engine"]["schema_ok"], health["engine"].get("warning"))
                self.assertTrue(health["engine"]["ok"])
                self.assertGreater(health["engine"]["tasks"]["total"], 0)
                self.assertEqual(health["app"]["sandbox"], True)

                resp, raw = get("/api/rooms")
                rooms = json.loads(raw.decode("utf-8"))
                self.assertEqual(resp.status, 200)
                ids = [room["id"] for room in rooms["rooms"]]
                self.assertIn("cafe", ids)

                resp, raw = get("/api/rooms/cafe/team")
                team = json.loads(raw.decode("utf-8"))
                self.assertEqual(resp.status, 200)
                self.assertTrue(team["members"])
                self.assertIn("profile", team["members"][0])

                resp, raw = get("/api/rooms/cafe/roadmap")
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8"))["room"], "cafe")

                resp, raw = get("/api/rooms/cafe/backlog")
                self.assertEqual(resp.status, 200)
                self.assertIn("items", json.loads(raw.decode("utf-8")))

                resp, raw = get("/api/rooms/cafe/candidates")
                self.assertEqual(resp.status, 200)
                self.assertIn("items", json.loads(raw.decode("utf-8")))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


    def test_real_etags_ignore_clock(self):
        """Реальный стек: ETag комнат/запросов/отчётов не меняется при сдвиге часов на +3 ч."""
        with SandboxHelper() as sb:
            server = app_mod.create_app(sb.cfg, start_watcher=False, port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()

            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                conn.request("GET", path)
                resp = conn.getresponse()
                raw = resp.read()
                etag = resp.getheader("ETag")
                conn.close()
                return resp.status, raw, etag

            try:
                from server import registry
                for name, info in sb.ids.items():          # посеянные деревья → реестр комнаты cafe
                    if not isinstance(info, dict) or "root_id" not in info or name == "candidate":
                        continue
                    row = sb.one("SELECT * FROM tasks WHERE id = ?", (info["root_id"],))
                    if row is not None:
                        registry.add_request(sb.cfg, "cafe", {
                            "root_id": info["root_id"], "slug": info["slug"], "title": row["title"],
                            "text": row["body"], "created_at": row["created_at"], "critic": False,
                            "mode": "auto", "adopted": False, "extra_cards": [], "retargeted": True,
                            "ingested": {}})
                paths = ["/api/rooms/cafe", "/api/rooms/fn"]
                items = json.loads(get("/api/rooms/cafe/requests?limit=100")[1].decode("utf-8"))["items"]
                paths += ["/api/requests/" + it["id"] for it in items]
                self.assertGreaterEqual(len(paths), 4)
                first = {p: get(p) for p in paths}
                real_time = time.time
                with mock.patch("time.time", side_effect=lambda: real_time() + 3 * 3600):
                    for path in paths:
                        status, _raw, etag = get(path)
                        self.assertEqual(status, 200, path)
                        self.assertTrue(first[path][2], path)
                        self.assertEqual(etag, first[path][2], "ETag зависит от времени: " + path)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
