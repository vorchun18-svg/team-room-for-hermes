# -*- coding: utf-8 -*-
"""W6-BE-A: регрессия безопасности записи (D1, D2, D3, D9, D10, D12).

Проверки критика `docs/critic/wave4-backend.md` §2.1–2.3, §2.9, §2.10, §2.12.

Правила:
  * только песочница `tests.helpers.Sandbox` (никаких записей в реальный Hermes/Vault);
  * CLI Hermes подменяется **shim'ом**: он записывает argv и НИЧЕГО не выполняет —
    ни один тест не может запустить агентов, диспетчер или декомпозицию;
  * для проверки «не песочница» каталог Hermes подменяется
    `dataclasses.replace(cfg, sandbox=False)` (как требует задание) — CLI всё равно shim.

Запуск:
    python -m unittest tests.test_write_security -v

# SHIM-OK: CLI Hermes подменяется CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import os
import subprocess as real_subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                 # noqa: E402
import server.engine_cli as engine_cli       # noqa: E402
import server.engine_write as ew             # noqa: E402
import server.files as files_mod             # noqa: E402
import server.http_util as http_util         # noqa: E402
import server.registry as registry           # noqa: E402
import server.reports as reports             # noqa: E402
from server.http_util import ApiError        # noqa: E402
from tests.helpers import Sandbox            # noqa: E402

ROOM = "cafe"


class CliShim:
    """Подмена subprocess: пишем argv, ничего не запускаем (ни агентов, ни досок)."""

    def __init__(self, dispatch_json: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.dispatch_json = dispatch_json or {"spawned": [], "promoted": 0, "reclaimed": 0}
        self._real = real_subprocess

    # прозрачность для остального API модуля subprocess
    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                      # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb = ""
        try:
            start = argv.index("kanban") + 1
        except ValueError:
            start = 0
        skip = False
        for token in argv[start:]:
            if skip:
                skip = False
                continue
            if token in ("--board", "--home"):
                skip = True
                continue
            if token.startswith("-"):
                continue
            verb = token
            break
        if verb == "dispatch":
            out = json.dumps(self.dispatch_json)
        elif verb == "create":
            out = json.dumps({"id": "t_0000cafe", "status": "triage"})
        else:
            out = ""
        return real_subprocess.CompletedProcess(argv, 0, out, "")

    def verbs(self) -> list[str]:
        out = []
        for argv in self.calls:
            tokens = [t for t in argv if not t.startswith("-")]
            out.append(tokens[4] if len(tokens) > 4 else "")
        return out


def _record(root_id: str, **extra) -> dict:
    rec = {"root_id": root_id, "slug": "r-20261028-1200-abcd", "title": "Запрос",
           "text": "Текст запроса", "created_at": 1793000000, "critic": False,
           "mode": "auto", "adopted": False, "extra_cards": [], "ingested": {},
           "retargeted": False}
    rec.update(extra)
    return rec


class _Base(unittest.TestCase):
    """Песочница + shim на весь класс."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.shim = CliShim()
        cls._had_ew_sub = hasattr(ew, "subprocess")
        cls._real_mods = (getattr(ew, "subprocess", None), engine_cli.subprocess)
        ew.subprocess = cls.shim                 # на случай прямого вызова из engine_write
        engine_cli.subprocess = cls.shim

    @classmethod
    def tearDownClass(cls):
        engine_cli.subprocess = cls._real_mods[1]
        if cls._had_ew_sub:
            ew.subprocess = cls._real_mods[0]
        else:
            try:
                del ew.subprocess
            except AttributeError:
                pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.shim.calls.clear()
        getattr(ew, "reset_dispatch_guard", lambda: None)()

    # --- помощники ---------------------------------------------------------
    def app(self, cfg=None):
        app = app_mod.App(cfg or self.cfg, port=0)
        app.call("rooms", "seed_defaults", ((self.cfg,), {}))
        return app

    def request_id(self, tree: str = "done_modern", room: str = ROOM) -> str:
        """Зарегистрировать дерево песочницы как запрос комнаты и вернуть rid."""
        root = self.sb.state(tree)["root_id"]
        registry.add_request(self.cfg, room, _record(root))
        return root

    def call(self, app, method: str, path: str, query=None, body=None):
        """Вызов роутера как HTTP-запрос: ApiError → (status, payload, headers)."""
        try:
            return app.dispatch(method, path, dict(query or {}), dict(body or {}))
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def act(self, rid: str, body: dict, app=None):
        app = app or self.app()
        return self.call(app, "POST", "/api/requests/%s/actions" % rid, {}, body)

    def assert_no_cli(self, msg: str = "CLI не должен вызываться"):
        self.assertEqual(self.shim.calls, [], "%s: %r" % (msg, self.shim.calls))


class TaskIdGuardTests(_Base):
    """D1: task_id — строго `^t_[0-9a-f]{8}$` и только из дерева этого запроса."""

    def test_option_injection_in_task_id_is_400_and_no_cli(self):
        rid = self.request_id("done_modern")
        for bad in ("--rm=" + rid, "--help", "-h", "--board=other", "-x"):
            status, payload, _ = self.act(rid, {"action": "archive", "task_id": bad})
            self.assertEqual(status, 400, (bad, payload))
            self.assertEqual(self.shim.calls, [], (bad, self.shim.calls))

    def test_task_id_shapes_rejected(self):
        rid = self.request_id("done_modern")
        for bad in ("../x", "t_ZZZZZZZZ", "t_1234", "t_" + "a" * 9, " ", "t_0000cafe\x00"):
            for action in ("comment", "reassign", "unblock", "retry"):
                body = {"action": action, "task_id": bad, "text": "x", "profile": "biz-ops"}
                status, payload, _ = self.act(rid, body)
                self.assertEqual(status, 400, (action, bad, payload))
            self.assert_no_cli(bad)

    def test_foreign_task_id_is_404_and_no_cli(self):
        rid = self.request_id("done_modern")
        foreign = self.sb.state("queued")["root_id"]        # валиден по форме, но чужой
        for action, extra in (("comment", {"text": "привет"}),
                              ("reassign", {"profile": "biz-ops"}),
                              ("unblock", {}), ("retry", {}), ("rework", {"text": "x"})):
            status, payload, _ = self.act(rid, dict({"action": action, "task_id": foreign}, **extra))
            self.assertEqual(status, 404, (action, payload))
            self.assertEqual(payload["error"]["detail"].get("reason"), "foreign_task", (action, payload))
        self.assert_no_cli()

    def test_own_child_is_allowed_and_argv_is_safe(self):
        rid = self.request_id("working")
        child = self.sb.state("working")["child_ids"][0]
        status, payload, _ = self.act(rid, {"action": "comment", "task_id": child, "text": "- пункт"})
        self.assertEqual(status, 200, payload)
        argv = self.shim.calls[-1]
        self.assertIn("comment", argv)
        self.assertIn(child, argv)
        self.assertIn("--author=mikhail", argv)
        self.assertIn("--", argv)
        self.assertLess(argv.index("--"), argv.index("- пункт"))
        # после `kanban` значения не передаются отдельными флагами (только --board board и `--`)
        tail = argv[argv.index("kanban") + 1:]
        tail = [t for i, t in enumerate(tail)
                if not (i + 1 < len(tail) and tail[i] == "--board") and t != "team"]
        self.assertFalse([t for t in tail if t.startswith("--") and "=" not in t and t != "--"], argv)

    def test_archive_has_no_task_id(self):
        rid = self.request_id("done_modern")
        status, payload, _ = self.act(rid, {"action": "archive", "task_id": "--rm=" + rid})
        self.assertEqual(status, 400, payload)
        self.assert_no_cli()
        status, payload, _ = self.act(rid, {"action": "archive"})
        self.assertEqual(status, 200, payload)
        argv = self.shim.calls[-1]
        self.assertEqual(argv[argv.index("archive") + 1], "--", argv)


class ProfileAuthorGuardTests(_Base):
    """D1: profile — из состава комнаты (или none), author — белый список."""

    def test_unknown_profile_is_400_and_no_cli(self):
        rid = self.request_id("working")
        child = self.sb.state("working")["child_ids"][0]
        for bad in ("zzz-nonexistent", "--help", "--board=team", "biz-critic --reclaim"):
            status, payload, _ = self.act(rid, {"action": "reassign", "task_id": child, "profile": bad})
            self.assertEqual(status, 400, (bad, payload))
            self.assertTrue(payload["error"]["detail"].get("available"), payload)
            self.assert_no_cli(bad)

    def test_profile_outside_room_is_400(self):
        rid = self.request_id("working")
        child = self.sb.state("working")["child_ids"][0]
        # профиль есть в ростере Hermes, но в комнате cafe его нет
        status, payload, _ = self.act(rid, {"action": "reassign", "task_id": child, "profile": "coder"})
        self.assertEqual(status, 400, payload)
        self.assert_no_cli()

    def test_member_profile_ok_with_dashdash(self):
        rid = self.request_id("working")
        child = self.sb.state("working")["child_ids"][0]
        status, payload, _ = self.act(rid, {"action": "reassign", "task_id": child, "profile": "biz-ops"})
        self.assertEqual(status, 200, payload)
        argv = self.shim.calls[-1]
        self.assertEqual(argv[argv.index("assign") + 1:argv.index("assign") + 4],
                         ["--", child, "biz-ops"], argv)

    def test_unassign_none_allowed(self):
        rid = self.request_id("working")
        child = self.sb.state("working")["child_ids"][0]
        status, payload, _ = self.act(rid, {"action": "reassign", "task_id": child, "profile": "none"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.shim.calls[-1][-3:], ["--", child, "none"])

    def test_author_whitelist(self):
        rid = self.request_id("done_modern")
        status, payload, _ = self.act(rid, {"action": "comment", "text": "x", "author": "evil"})
        self.assertEqual(status, 400, payload)
        self.assert_no_cli()
        status, payload, _ = self.act(rid, {"action": "comment", "text": "x", "author": "--help"})
        self.assertEqual(status, 400, payload)
        self.assert_no_cli()
        status, payload, _ = self.act(rid, {"action": "comment", "text": "x", "author": "biz-lawyer"})
        self.assertEqual(status, 200, payload)
        self.assertIn("--author=biz-lawyer", self.shim.calls[-1])

    def test_nul_in_text_is_400_and_no_cli(self):
        rid = self.request_id("done_modern")
        for body in ({"action": "comment", "text": "a\x00b"},
                     {"action": "comment", "text": "ok", "author": "max\x00im"},
                     {"action": "revise", "text": "a\x00b"}):
            status, payload, _ = self.act(rid, body)
            self.assertEqual(status, 400, (body, payload))
        self.assert_no_cli()


class DecomposeGuardTests(_Base):
    """D9: decompose — только для корня в queued/triage."""

    def test_decompose_done_request_is_409(self):
        rid = self.request_id("done_modern")
        status, payload, _ = self.act(rid, {"action": "decompose"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["detail"].get("reason"), "already_decomposed")
        self.assert_no_cli()

    def test_decompose_queued_request_runs(self):
        rid = self.request_id("queued")
        status, payload, _ = self.act(rid, {"action": "decompose"})
        self.assertEqual(status, 200, payload)
        argv = self.shim.calls[-1]
        self.assertEqual(argv[argv.index("decompose") + 1], "--", argv)

    def test_decompose_foreign_task_id_is_404(self):
        rid = self.request_id("queued")
        status, payload, _ = self.act(rid, {"action": "decompose",
                                            "task_id": self.sb.state("working")["root_id"]})
        self.assertEqual(status, 404, payload)
        self.assert_no_cli()


class DispatchGuardTests(_Base):
    """D2: dispatch/kick — отказ в песочнице, --max, dry-run, чужой tenant, частота."""

    def _app_outside_sandbox(self):
        cfg = dataclasses.replace(self.cfg, sandbox=False, hermes_home=Path(self.sb.root) / "fake_hermes")
        cfg.hermes_home.mkdir(parents=True, exist_ok=True)  # SAFETY-OK: fake_hermes внутри песочницы sb.root
        return self.app(cfg)

    def kick(self, app, body=None):
        return self.call(app, "POST", "/api/dispatcher/kick", {}, body or {})

    def test_kick_refused_in_sandbox(self):
        app = self.app()
        status, payload, _ = self.kick(app)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "sandbox_no_dispatch")
        self.assert_no_cli()

    def test_dispatch_action_refused_in_sandbox(self):
        rid = self.request_id("done_modern")
        status, payload, _ = self.act(rid, {"action": "dispatch"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "sandbox_no_dispatch")
        self.assert_no_cli()

    def test_dry_run_then_confirm_with_max(self):
        self.shim.dispatch_json = {"spawned": [{"id": "t_0000beef", "assignee": "biz-ops"}],
                                   "promoted": 1, "reclaimed": 0}
        app = self._app_outside_sandbox()
        status, payload, _ = self.kick(app)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["detail"].get("reason"), "foreign_cards")
        self.assertEqual(len(self.shim.calls), 1, self.shim.calls)          # только dry-run
        argv = self.shim.calls[0]
        self.assertIn("--dry-run", argv)
        self.assertIn("--max=3", argv)
        self.assertTrue([c["id"] for c in payload["error"]["detail"]["cards"]])

        ew.reset_dispatch_guard()
        status, payload, _ = self.kick(app, {"confirm": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual([c["id"] for c in payload.get("planned") or []], ["t_0000beef"])
        real_calls = [c for c in self.shim.calls if "--dry-run" not in c]
        self.assertEqual(len(real_calls), 1, self.shim.calls)
        self.assertIn("--max=3", real_calls[0])
        self.assertEqual(real_calls[0][:real_calls[0].index("dispatch") + 1][-1], "dispatch")

    def test_rate_limit_429(self):
        self.shim.dispatch_json = {"spawned": [], "promoted": 0, "reclaimed": 0}
        app = self._app_outside_sandbox()
        first = self.kick(app, {"confirm": True})
        self.assertEqual(first[0], 200, first[1])
        calls = len(self.shim.calls)
        second = self.kick(app, {"confirm": True})
        self.assertEqual(second[0], 429, second[1])
        self.assertEqual(second[1]["error"]["code"], "rate_limited")
        self.assertTrue(second[1]["error"]["detail"].get("retry_after") >= 1)
        self.assertEqual(len(self.shim.calls), calls)

    def test_max_is_configurable_and_clamped(self):
        self.shim.dispatch_json = {"spawned": [], "promoted": 0, "reclaimed": 0}
        app = self._app_outside_sandbox()
        for env_value, expected in (("5", "--max=5"), ("9", "--max=5"), ("0", "--max=1"),
                                    ("abc", "--max=3"), (None, "--max=3")):
            ew.reset_dispatch_guard()
            if env_value is None:
                os.environ.pop("TEAMROOM_DISPATCH_MAX", None)
            else:
                os.environ["TEAMROOM_DISPATCH_MAX"] = env_value
            self.shim.calls.clear()
            for body in ({"confirm": True},):
                status, payload, _ = self.kick(app, body)
                self.assertEqual(status, 200, (env_value, payload))
            used = [t for c in self.shim.calls for t in c if t.startswith("--max=")]
            self.assertEqual(set(used), {expected}, (env_value, used))
        os.environ.pop("TEAMROOM_DISPATCH_MAX", None)


class ReportPathGuardTests(_Base):
    """D3: /api/reports/{room}/{key} — только слаг, только белые корни."""

    def setUp(self):
        super().setUp()
        self.outside = Path(self.sb.root) / "outside"
        self.outside.mkdir(parents=True, exist_ok=True)
        self.leak = self.outside / "leak.json"
        self.leak.write_text(json.dumps({"version": 1, "title": "OUTSIDE-SECRET",
                                         "blocks": [{"type": "text", "md": "x"}]}),
                             encoding="utf-8")
        self.denied = Path(self.cfg.data_dir) / "auth.json"
        self.denied.parent.mkdir(parents=True, exist_ok=True)
        self.denied.write_text(json.dumps({"version": 1, "title": "AUTHSECRET",
                                           "blocks": [{"type": "text", "md": "x"}]}),
                               encoding="utf-8")
        self.app_ = self.app()

    def _get(self, key: str):
        return self.call(self.app_, "GET", "/api/reports/%s/%s" % (ROOM, key))

    def test_absolute_and_relative_paths_are_rejected(self):
        for key in (str(self.leak), "C:\\Windows\\win.ini", "../../leak.json",
                    "..%2F..%2Fleak.json", "reports/cafe/x/report.json", "..", "."):
            status, payload, _ = self._get(key)
            # ключ со слешами не попадает в маршрут (404), без слешей — 400 из find_report;
            # главное — ни чтения, ни содержимого чужого файла
            self.assertIn(status, (400, 404), (key, payload))
            self.assertNotIn("OUTSIDE-SECRET", json.dumps(payload, ensure_ascii=False), key)

    def test_denied_names_not_served(self):
        status, payload, _ = self._get("auth.json")
        self.assertIn(status, (400, 404), payload)
        self.assertNotIn("AUTHSECRET", json.dumps(payload, ensure_ascii=False))

    def test_valid_slug_still_works(self):
        status, payload, _ = self._get(self.sb.state("done_modern")["slug"])
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("version"), 1)
        self.assertTrue(payload.get("blocks"))

    def test_load_report_refuses_outside_roots(self):
        got = reports.load_report(self.cfg, self.leak)
        self.assertTrue(got.get("errors"), got)
        self.assertNotIn("OUTSIDE-SECRET", json.dumps(got, ensure_ascii=False))
        got = reports.load_report(self.cfg, self.denied)
        self.assertTrue(got.get("errors"), got)

    def test_serve_report_file_stays_inside(self):
        slug = self.sb.state("done_modern")["slug"]
        for rel in ("../../auth.json", "..%2F..%2Fauth.json", str(self.leak), "/etc/passwd"):
            status, _, body = reports.serve_report_file(self.cfg, ROOM, slug, rel)
            self.assertIn(status, (403, 404), (rel, status))
            self.assertNotIn(b"OUTSIDE-SECRET", body if isinstance(body, bytes) else b"")

    def test_file_endpoint_blocks_scripts(self):
        for name in ("run.exe", "run.bat", "run.lnk", "run.ps1", "run.cmd", "run.msi",
                     "app.js", "run.vbs"):
            target = Path(self.cfg.data_dir) / name
            target.write_text("x", encoding="utf-8")
            status, payload, headers = self.call(self.app_, "GET", "/api/file",
                                                 {"path": [str(target)]})
            self.assertEqual(status, 403, (name, status, payload))

    def test_open_endpoint_blocks_scripts(self):
        calls = []
        original = files_mod.reveal
        files_mod.reveal = lambda cfg, path: calls.append(path) or True
        try:
            for name in ("run.exe", "run.bat", "run.lnk", "run.ps1", "run.cmd", "run.msi",
                         "app.js", "run.vbs"):
                target = Path(self.cfg.data_dir) / name
                target.write_text("x", encoding="utf-8")
                status, payload, _ = self.call(self.app_, "POST", "/api/open", {},
                                               {"path": str(target)})
                self.assertEqual(status, 403, (name, status, payload))
            self.assertEqual(calls, [])
        finally:
            files_mod.reveal = original


class TransportGuardTests(_Base):
    """D10/D12 на живом сервере: 403 вместо 500 и 400 вместо 500."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.1},
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        finally:
            super().tearDownClass()

    def _post(self, path: str, raw: bytes, headers: dict | None = None, method: str = "POST"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            hdrs = {"Host": "127.0.0.1:%d" % self.port,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw))}
            hdrs.update(headers or {})
            conn.request(method, path, body=raw, headers=hdrs)
            res = conn.getresponse()
            payload = res.read()
            return res.status, payload
        finally:
            conn.close()

    def test_bad_origin_port_is_403_not_500(self):
        self.assertFalse(http_util.origin_ok("http://127.0.0.1:99999", self.port))
        self.assertFalse(http_util.origin_ok("http://127.0.0.1:%d.evil.com" % self.port, self.port))
        for origin in ("http://127.0.0.1:99999", "http://127.0.0.1:%d.evil.com" % self.port):
            status, body = self._post("/api/open", b'{"path":"x"}', {"Origin": origin})
            self.assertEqual(status, 403, (origin, body[:200]))
            self.assertIn(b"bad_origin", body)

    def test_null_and_deep_json_are_400(self):
        cases = [b'{"text":"a\\u0000b"}',
                 b"[" * 100000 + b"]" * 100000,
                 b'{"title":' + b"9" * 5000 + b"}"]
        for raw in cases:
            status, body = self._post("/api/rooms/" + ROOM + "/requests", raw)
            self.assertEqual(status, 400, (raw[:40], status, body[:300]))
            self.assertNotIn(b"engine_error", body)


if __name__ == "__main__":
    unittest.main()
