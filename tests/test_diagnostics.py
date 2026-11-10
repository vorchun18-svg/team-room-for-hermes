"""Проверка системы: структура, песочницы и приватность (WAVE 12 §12.2).

Что доказываем:
  * ``GET /api/diagnostics`` отдаёт пункты ``{id, title, status, value, hint, fix}``
    со статусами ``ok | warn | fail | off`` и честной сводкой;
  * в песочнице без CLI, без диктовки и без Vault пункты становятся ``warn`` /
    ``off`` с подсказкой, а не падают и не врут про «всё хорошо»;
  * в ответе нет ни ключей, ни полных путей пользователя;
  * ошибки из журнала отдаются текстом ≤200 символов, без путей и токенов;
  * ``GET /api/docs`` показывает документацию приложению и не выпускает за
    пределы ``docs/``.
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import diagnostics                                   # noqa: E402
from tests.helpers import Sandbox                                # noqa: E402

STATUSES = {"ok", "warn", "fail", "off"}
REQUIRED = {"id", "title", "status", "value", "hint", "fix"}
SECRET = "sk-test-abcdef123456"


def _blob(data) -> str:
    return json.dumps(data, ensure_ascii=False)


class StructureTests(unittest.TestCase):
    """Форма ответа и сводка — на живой песочнице."""

    @classmethod
    def setUpClass(cls):
        cls._sandbox = Sandbox()
        cls.sb = cls._sandbox.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._sandbox.__exit__(None, None, None)

    def test_report_structure(self):
        data = diagnostics.report(self.sb.cfg, port=8896, board="team")
        self.assertIsInstance(data, dict)
        for key in ("ok", "app", "summary", "checks", "generated_at"):
            self.assertIn(key, data)
        self.assertIsInstance(data["checks"], list)
        self.assertGreaterEqual(len(data["checks"]), 10)
        ids = [item["id"] for item in data["checks"]]
        self.assertEqual(len(ids), len(set(ids)), "id пунктов должны быть уникальны")
        for item in data["checks"]:
            self.assertTrue(REQUIRED.issubset(item), "у пункта не все поля: %s" % item.get("id"))
            self.assertIn(item["status"], STATUSES, item.get("id"))
            self.assertTrue(str(item["title"]).strip())
            self.assertIsInstance(item["details"], list)
            fix = item["fix"]
            if fix is not None:
                self.assertIn(fix.get("kind"), {"command", "doc", "route", "link"})
                self.assertTrue(str(fix.get("label") or "").strip())
        # ключевые пункты на месте
        for expected in ("version", "system", "hermes_cli", "board", "dictation",
                         "vault", "port", "autostart", "plugin", "logs", "disk"):
            self.assertIn(expected, ids)

    def test_summary_matches_checks(self):
        data = diagnostics.report(self.sb.cfg, port=8896, board="team")
        summary = data["summary"]
        self.assertEqual(summary["total"], len(data["checks"]))
        for status in STATUSES:
            self.assertEqual(summary[status],
                             sum(1 for item in data["checks"] if item["status"] == status))
        self.assertEqual(summary["total"], sum(summary[s] for s in STATUSES))
        self.assertEqual(data["ok"], summary["fail"] == 0)

    def test_no_secrets_and_no_user_paths(self):
        data = diagnostics.report(self.sb.cfg, port=8896, board="team")
        blob = _blob(data)
        self.assertNotIn("sk-", blob)
        self.assertNotIn(str(diagnostics.home_dir()), blob)
        self.assertNotIn(str(Path.home()), blob)
        for item in data["checks"]:
            self.assertNotIn("\\\\Users\\\\", _blob(item), item["id"])
        # каждый путь — только ~-стиль или заглушка
        for item in data["checks"]:
            value = str(item["value"])
            if ":" in value and "\\" in value:
                self.fail("полный путь в значении пункта %s: %s" % (item["id"], value))

    def test_project_folder_item_when_room_given(self):
        room = {"id": "cafe", "title": "AMI", "project_dir": ""}
        data = diagnostics.report(self.sb.cfg, port=8896, board="team", room=room)
        project = next(item for item in data["checks"] if item["id"] == "project")
        self.assertEqual(project["status"], "warn")
        self.assertIn("AMI", project["hint"])
        self.assertEqual(project["fix"]["kind"], "route")

    def test_room_with_existing_folder_is_ok(self):
        room = {"id": "cafe", "title": "AMI", "project_dir": str(self.sb.data_dir)}
        data = diagnostics.report(self.sb.cfg, port=8896, board="team", room=room)
        project = next(item for item in data["checks"] if item["id"] == "project")
        self.assertEqual(project["status"], "ok")


class SandboxWithoutExtrasTests(unittest.TestCase):
    """Ни CLI, ни диктовки, ни Vault — «не настроено», а не «всё хорошо»."""

    def setUp(self):
        self._sandbox = Sandbox()
        self.sb = self._sandbox.__enter__()
        self.cfg = dataclasses.replace(self.sb.cfg, hermes_cli="", vault_enabled=False)

    def tearDown(self):
        self._sandbox.__exit__(None, None, None)

    def _report(self):
        with mock.patch.object(diagnostics, "_stt_status", return_value={}):
            return diagnostics.report(self.cfg, port=8896, board="team")

    def test_statuses_without_cli_dictation_and_vault(self):
        data = self._report()
        by_id = {item["id"]: item for item in data["checks"]}
        self.assertEqual(by_id["hermes_cli"]["status"], "warn")
        self.assertIn("не найдена", by_id["hermes_cli"]["value"])
        self.assertIn("docs/INSTALL.md", by_id["hermes_cli"]["fix"]["value"])
        self.assertEqual(by_id["board"]["status"], "warn")
        self.assertIn("только чтение", by_id["board"]["value"])
        self.assertEqual(by_id["dictation"]["status"], "off")
        self.assertIn("не настроена", by_id["dictation"]["value"])
        self.assertEqual(by_id["vault"]["status"], "off")
        self.assertEqual(by_id["vault"]["fix"]["kind"], "doc")
        # доска читается даже без CLI: карточки посчитаны, а не «0 по умолчанию»
        self.assertRegex(by_id["board"]["value"], r"^\d+ (карточка|карточки|карточек)")

    def test_no_keys_even_with_none_mode(self):
        data = self._report()
        blob = _blob(data)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("sk-", blob)
        self.assertNotIn("api_key", blob.lower())


class LogsTests(unittest.TestCase):
    """Последние 5 ошибок: текст, без путей и токенов, ≤200 символов."""

    def setUp(self):
        self._sandbox = Sandbox()
        self.sb = self._sandbox.__enter__()
        self.log = Path(self.sb.data_dir) / "logs" / "app.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        home = str(diagnostics.home_dir())
        lines = [
            "2026-11-03 09:00:00 INFO старт сервера",
            "2026-11-03 09:01:00 ERROR open %s\\secret\\plan.md failed token=%s" % (home, SECRET),
            "2026-11-03 09:02:00 WARNING пусто",
            "2026-11-03 09:03:00 CRITICAL " + ("очень длинная строка " * 40),
            "2026-11-03 09:04:00 ERROR не удалось записать отчёт",
        ]
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tearDown(self):
        self._sandbox.__exit__(None, None, None)

    def test_last_five_errors_are_text_only(self):
        errors = diagnostics.read_log_errors(self.log.parent, limit=5)
        self.assertGreaterEqual(len(errors), 2)
        self.assertLessEqual(len(errors), 5)
        blob = "\n".join(errors)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("User", blob)
        self.assertNotIn(str(diagnostics.home_dir()), blob)
        for line in errors:
            self.assertLessEqual(len(line), diagnostics.MAX_LOG_LINE)
            self.assertNotIn("\n", line)
        # предупреждения и информационные строки в «ошибки» не попадают
        self.assertFalse(any("INFO" in line or "WARNING" in line for line in errors))
        # последняя ошибка журнала — в конце списка
        self.assertIn("не удалось записать отчёт", errors[-1])

    def test_logs_item_shows_errors_without_paths(self):
        data = diagnostics.report(self.sb.cfg, port=8896, board="team")
        item = next(entry for entry in data["checks"] if entry["id"] == "logs")
        self.assertEqual(item["status"], "warn")
        self.assertTrue(item["details"])
        self.assertNotIn("User", _blob(item))
        self.assertNotIn(SECRET, _blob(item))
        self.assertEqual(item["fix"]["kind"], "link")

    def test_limit_is_respected(self):
        errors = diagnostics.read_log_errors(self.log.parent, limit=1)
        self.assertEqual(len(errors), 1)


class HelpersTests(unittest.TestCase):
    """Чистые помощники: версия, парсеры, пути, порт."""

    def test_app_version_falls_back_to_config(self):
        version, source = diagnostics.app_version(app_dir=ROOT / "нет-такой-папки")
        self.assertEqual(version, diagnostics.config.VERSION)
        self.assertEqual(source, "config")

    def test_app_version_reads_version_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            version, source = diagnostics.app_version(app_dir=tmp)
        self.assertEqual((version, source), ("1.2.3", "VERSION"))

    def test_parse_cli_version(self):
        self.assertEqual(diagnostics.parse_cli_version("hermes 1.4.2\nbuild 7"), "1.4.2")
        self.assertEqual(diagnostics.parse_cli_version("Hermes Desktop v0.9"), "0.9")
        self.assertEqual(diagnostics.parse_cli_version(""), "")

    def test_parse_plugin_version(self):
        text = "export const PLUGIN_VERSION = '1.0.0'\n"
        self.assertEqual(diagnostics.parse_plugin_version(text), "1.0.0")
        self.assertEqual(diagnostics.parse_plugin_version("const x = 1"), "")

    def test_safe_path_hides_home(self):
        home = diagnostics.home_dir()
        shown = diagnostics.safe_path(home / "AppData" / "Local" / "hermes")
        self.assertTrue(shown.startswith("~/"), shown)
        self.assertNotIn(str(home), shown)
        self.assertEqual(diagnostics.safe_path(""), "")
        self.assertEqual(diagnostics.safe_path("/opt/чужое/место"), "<путь>")

    def test_redact_truncates_and_masks(self):
        line = "ERROR token=%s %s" % (SECRET, "x" * 400)
        cleaned = diagnostics.redact(line)
        self.assertNotIn(SECRET, cleaned)
        self.assertLessEqual(len(cleaned), diagnostics.MAX_LOG_LINE)

    def test_port_check_reports_busy_next_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        try:
            cfg = mock.Mock(port=busy - 1)
            item = diagnostics._port_check(cfg, busy - 1)
            self.assertEqual(item["status"], "warn")
            self.assertIn("занят", item["value"])
        finally:
            sock.close()


class HttpTests(unittest.TestCase):
    """Эндпоинт через настоящий сервер: JSON, no-store, документация."""

    @classmethod
    def setUpClass(cls):
        import server.app as app_mod
        cls._sandbox = Sandbox()
        cls.sb = cls._sandbox.__enter__()
        cls.server = app_mod.create_app(cls.sb.cfg, start_watcher=False, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._sandbox.__exit__(None, None, None)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("GET", path)
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    def test_diagnostics_endpoint(self):
        status, headers, raw = self._get("/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertIn("json", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        data = json.loads(raw.decode("utf-8"))
        self.assertIn("checks", data)
        self.assertIn("summary", data)
        self.assertNotIn("User", raw.decode("utf-8"))

    def test_diagnostics_with_room(self):
        status, _headers, raw = self._get("/api/diagnostics?room=cafe")
        self.assertEqual(status, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertIn("project", [item["id"] for item in data["checks"]])

    def test_diagnostics_unknown_room_is_404(self):
        status, _headers, raw = self._get("/api/diagnostics?room=no-such-room")
        self.assertEqual(status, 404)
        self.assertIn("не найдена", json.loads(raw.decode("utf-8"))["error"]["message"])

    def test_docs_endpoint(self):
        status, headers, raw = self._get("/api/docs?name=INSTALL.md")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers.get("Content-Type", ""))
        self.assertIn("Установка", raw.decode("utf-8")[:400])

    def test_docs_rejects_foreign_paths(self):
        for path in ("/api/docs?name=../server/app.py", "/api/docs?name=app.py"):
            status, _headers, raw = self._get(path)
            self.assertEqual(status, 400, path)
            self.assertIn("недоступен", json.loads(raw.decode("utf-8"))["error"]["message"])


if __name__ == "__main__":
    unittest.main()
