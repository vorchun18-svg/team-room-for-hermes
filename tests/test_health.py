"""Проверки здоровья интеграций (WAVE24 §3.3, N4) — на поддельном Hermes.

Живые файлы реального `%LOCALAPPDATA%\\hermes` не читаются: каждое правило проверяется
на ВЫМЫШЛЕННОМ домашнем каталоге во временной папке (правило WAVE22 §0). Сеть не
трогается: «сосед» — это локальный сокет внутри теста.

    python -m unittest tests.test_health -v
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import health as health_mod          # noqa: E402
from tests.helpers import _outside_hermes_base   # noqa: E402

NOW = int(time.time())
DAY = 86400
SHAPE = {"id", "title", "status", "detail", "since", "hint"}


def _iso(stamp: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(int(stamp)))


def _file_stamp(stamp: int) -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime(int(stamp))) + ".md"


class HealthBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w24-n4h-", dir=str(_outside_hermes_base())))
        self.home = self.tmp / "hermes"
        (self.home / "cron" / "output").mkdir(parents=True, exist_ok=True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "rooms.json").write_text(
            json.dumps({"rooms": [{"id": "studio", "title": "Сайт студии",
                                   "orchestrator": "anna-lead",
                                   "members": [{"profile": "ivan-petrov"}]}],
                        "updated_at": NOW}, ensure_ascii=False), encoding="utf-8")
        self.cfg = type("Cfg", (), {"hermes_home": self.home, "data_dir": self.data,
                                    "sandbox": True})()

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # --- помощники ------------------------------------------------------
    def gateway_state(self, **kwargs):
        doc = {"pid": os.getpid(), "kind": "hermes-gateway", "gateway_state": "running",
               "exit_reason": None, "updated_at": _iso(int(time.time())),   # свежая запись на момент вызова,
               "active_agents": 0}                                 # а не на момент импорта модуля
        doc.update(kwargs)
        (self.home / "gateway_state.json").write_text(json.dumps(doc), encoding="utf-8")
        return self

    def token(self, **kwargs):
        doc = {"account": "вымышленный", "token": "вымышленный-токен",
               "refresh_token": "вымышленный-refresh", "expiry": "2099-01-01T00:00:00Z"}
        doc.update(kwargs)
        (self.home / "google_token.json").write_text(json.dumps(doc), encoding="utf-8")
        return self

    def jobs(self, jobs: list):
        (self.home / "cron").mkdir(parents=True, exist_ok=True)
        (self.home / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": jobs, "updated_at": _iso(NOW)}, ensure_ascii=False),
            encoding="utf-8")
        return self

    def output(self, job_id: str, body: str, *, stamp: int | None = None):
        folder = self.home / "cron" / "output" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _file_stamp(stamp or NOW - 600)).write_text(
            "# Cron Job: вымышленное задание\n\n## Prompt\n\nвымышленный текст\n\n"
            "## Response\n\n" + body + "\n", encoding="utf-8")
        return self

    def peer_config(self, url: str):
        (self.home / "config.yaml").write_text(
            "bot_peers:\n  сосед:\n    url: %s\n" % url, encoding="utf-8")
        return self


class ContractTests(HealthBase):
    """Форма ответа по контракту §3.3."""

    def test_shape_and_statuses(self):
        self.gateway_state()
        payload = health_mod.run(self.cfg)
        self.assertEqual(set(payload), {"generated_at", "overall", "checks"})
        self.assertIn(payload["overall"], ("ok", "warn", "fail"))
        self.assertTrue(payload["checks"])
        for item in payload["checks"]:
            self.assertEqual(set(item), SHAPE)
            self.assertIn(item["status"], health_mod.STATUSES)
            self.assertLessEqual(len(item["detail"]), 300)
            if item["hint"] is not None:
                self.assertTrue(item["hint"])

    def test_order_is_registration_order(self):
        payload = health_mod.run(self.cfg)
        names = [item["id"] for item in payload["checks"]]
        self.assertEqual(names[:4], ["gateway", "dispatcher", "peer_link", "calendar_token"])
        self.assertIn("duties", names)
        self.assertIn("backups", names)
        self.assertIn("disk", names)

    def test_new_check_joins_by_registration(self):
        calls = []

        @health_mod.register
        def вымышленная_проверка(cfg):
            calls.append(1)
            return health_mod.check("вымышленная", "Вымышленная проверка", "ok", "работает")

        try:
            payload = health_mod.run(self.cfg)
            ids = [item["id"] for item in payload["checks"]]
            self.assertIn("вымышленная", ids)
            self.assertEqual(calls, [1])
        finally:
            health_mod.CHECKS.remove(вымышленная_проверка)
        self.assertNotIn("вымышленная", [item["id"] for item in health_mod.run(self.cfg)["checks"]])

    def test_failing_check_is_unknown_not_crash(self):
        def падает(cfg):
            raise RuntimeError("вымышленная поломка")

        падает.__name__ = "broken"
        health_mod.register(падает)
        try:
            payload = health_mod.run(self.cfg)
        finally:
            health_mod.CHECKS.remove(падает)
        item = [one for one in payload["checks"] if one["id"] == "broken"][0]
        self.assertEqual(item["status"], "unknown")
        self.assertIn("не удалась", item["detail"])

    def test_overall_precedence(self):
        self.assertEqual(health_mod.overall([{"status": "ok"}, {"status": "unknown"}]), "ok")
        self.assertEqual(health_mod.overall([{"status": "ok"}, {"status": "warn"}]), "warn")
        self.assertEqual(health_mod.overall([{"status": "warn"}, {"status": "fail"}]), "fail")
        self.assertEqual(health_mod.overall([]), "ok")


class GatewayTests(HealthBase):
    def test_running(self):
        self.gateway_state()
        item = health_mod.gateway(self.cfg)
        self.assertEqual(item["status"], "ok")
        self.assertIn("работает", item["detail"])

    def test_stale_record(self):
        self.gateway_state(updated_at=_iso(NOW - 3600))
        item = health_mod.gateway(self.cfg)
        self.assertEqual(item["status"], "warn")
        self.assertIn("не обновлялась", item["detail"])

    def test_not_running(self):
        self.gateway_state(gateway_state="stopped", exit_reason="выход по ошибке")
        item = health_mod.gateway(self.cfg)
        self.assertEqual(item["status"], "fail")
        self.assertIn("выход по ошибке", item["detail"])

    def test_no_file(self):
        item = health_mod.gateway(self.cfg)
        self.assertEqual(item["status"], "unknown")
        self.assertIsNone(item["since"])


class DispatcherTests(HealthBase):
    def test_running_pid(self):
        self.gateway_state()
        (self.home / "gateway.pid").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self.assertEqual(health_mod.dispatcher(self.cfg)["status"], "ok")

    def test_stopped(self):
        item = health_mod.dispatcher(self.cfg)
        self.assertEqual(item["status"], "fail")


class PeerLinkTests(HealthBase):
    def test_not_configured_is_unknown(self):
        item = health_mod.peer_link(self.cfg)
        self.assertEqual(item["status"], "unknown")

    def test_answering_peer(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.peer_config("http://127.0.0.1:%d" % port)
            item = health_mod.peer_link(self.cfg)
        finally:
            server.close()
        self.assertEqual(item["status"], "ok")
        self.assertIn("отвечает", item["detail"])

    def test_silent_peer(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()                       # порт свободен — там никто не слушает
        self.peer_config("http://127.0.0.1:%d" % port)
        item = health_mod.peer_link(self.cfg)
        self.assertEqual(item["status"], "fail")
        self.assertIn("не отвечает", item["detail"])


class CalendarTokenTests(HealthBase):
    def test_expired_token_fails(self):
        self.token(expiry="2026-10-26T23:11:28Z")
        item = health_mod.calendar_token(self.cfg)
        self.assertEqual(item["status"], "fail")
        self.assertIn("истёк", item["detail"])
        self.assertIsNotNone(item["since"])
        self.assertIn("авторизаци", item["hint"])

    def test_invalid_grant_in_duties_fails(self):
        self.token(expiry="2099-01-01T00:00:00Z")
        self.jobs([{"id": "j_daily", "name": "Сводка",
                    "schedule": {"kind": "cron", "expr": "0 8 * * *"},
                    "schedule_display": "0 8 * * *", "enabled": True}])
        self.output("j_daily", "Календарь не обновляется: `invalid_grant`.")
        item = health_mod.calendar_token(self.cfg)
        self.assertEqual(item["status"], "fail")
        self.assertIn("отказ", item["detail"])

    def test_old_mention_outside_window_is_ignored(self):
        self.token(expiry="2099-01-01T00:00:00Z")
        self.jobs([{"id": "j_daily", "name": "Сводка",
                    "schedule": {"kind": "cron", "expr": "0 8 * * *"},
                    "schedule_display": "0 8 * * *", "enabled": True}])
        self.output("j_daily", "Календарь: `invalid_grant`.", stamp=NOW - 10 * DAY)
        item = health_mod.calendar_token(self.cfg)
        self.assertEqual(item["status"], "ok")

    def test_no_token_file(self):
        item = health_mod.calendar_token(self.cfg)
        self.assertEqual(item["status"], "unknown")

    def test_healthy_token(self):
        self.token()
        item = health_mod.calendar_token(self.cfg)
        self.assertEqual(item["status"], "ok")
        self.assertIn("действует", item["detail"])


class DutiesCheckTests(HealthBase):
    def test_no_jobs_file(self):
        self.assertEqual(health_mod.duties(self.cfg)["status"], "unknown")

    def test_all_fine(self):
        self.jobs([{"id": "j_ok", "name": "Сводка", "enabled": True,
                    "schedule": {"kind": "cron", "expr": "0 8 * * *"},
                    "schedule_display": "0 8 * * *",
                    "next_run_at": _iso(NOW + DAY), "last_run_at": _iso(NOW - 3600),
                    "last_status": "ok"}])
        item = health_mod.duties(self.cfg)
        self.assertEqual(item["status"], "ok")

    def test_problem_warns(self):
        self.jobs([{"id": "j_bad", "name": "Проверка сроков", "enabled": True,
                    "schedule": {"kind": "interval", "minutes": 30},
                    "schedule_display": "every 30m",
                    "next_run_at": _iso(NOW + 900), "last_run_at": _iso(NOW - 200),
                    "last_status": "error", "last_error": "файл занят"}])
        item = health_mod.duties(self.cfg)
        self.assertEqual(item["status"], "warn")
        self.assertIn("замечаниями", item["detail"])


class BackupsTests(HealthBase):
    def setUp(self):
        super().setUp()
        self.backups = self.tmp / "backups"
        self.backups.mkdir()
        self.env = mock.patch.dict(os.environ, {"TEAMROOM_BACKUPS": str(self.backups)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _folder(self, name: str, age_days: float):
        path = self.backups / name
        path.mkdir()
        stamp = NOW - int(age_days * DAY)
        os.utime(str(path), (stamp, stamp))

    def test_fresh_backup(self):
        self._folder("2026-11-17 копия", 1)
        self.assertEqual(health_mod.backups(self.cfg)["status"], "ok")

    def test_old_backup_warns(self):
        self._folder("2026-10-20 копия", 21)
        item = health_mod.backups(self.cfg)
        self.assertEqual(item["status"], "warn")
        self.assertIn("21", item["detail"])

    def test_empty_folder_warns(self):
        self.assertEqual(health_mod.backups(self.cfg)["status"], "warn")

    def test_missing_folder_unknown(self):
        with mock.patch.dict(os.environ, {"TEAMROOM_BACKUPS": str(self.tmp / "нет-папки")}):
            self.assertEqual(health_mod.backups(self.cfg)["status"], "unknown")


class DiskTests(HealthBase):
    def _usage(self, free_share: float):
        total = 1000 * 1024 ** 3
        usage = type("Usage", (), {"total": total, "free": int(total * free_share),
                                   "used": total - int(total * free_share)})
        with mock.patch.object(health_mod.shutil, "disk_usage", return_value=usage):
            return health_mod.disk(self.cfg)

    def test_plenty(self):
        item = self._usage(0.40)
        self.assertEqual(item["status"], "ok")
        self.assertIn("40", item["detail"])

    def test_low(self):
        self.assertEqual(self._usage(0.07)["status"], "warn")

    def test_critical(self):
        self.assertEqual(self._usage(0.01)["status"], "fail")


class SttAndGuardTests(HealthBase):
    def test_stt_none_warns(self):
        with mock.patch("server.diagnostics._stt_status", return_value={"mode": "none"}):
            self.assertEqual(health_mod.stt(self.cfg)["status"], "warn")

    def test_stt_local_ok(self):
        with mock.patch("server.diagnostics._stt_status", return_value={"mode": "local"}):
            item = health_mod.stt(self.cfg)
        self.assertEqual(item["status"], "ok")
        self.assertIn("локальный", item["detail"])

    def test_model_guard_warn(self):
        with mock.patch("server.model_guard.diag_item",
                        return_value={"id": "model_guard", "status": "warn",
                                      "value": "есть расхождения", "hint": "проверьте"}):
            item = health_mod.model_guard(self.cfg)
        self.assertEqual(item["status"], "warn")

    def test_model_guard_without_rooms(self):
        (self.data / "rooms.json").write_text(json.dumps({"rooms": []}), encoding="utf-8")
        self.assertEqual(health_mod.model_guard(self.cfg)["status"], "unknown")


class CatalogLineTests(unittest.TestCase):
    """W29-P1 §4: строка «Каталог моделей: …» — откуда список и почему он неполный."""

    def setUp(self):
        from server import agent_models as models_mod
        self.models = models_mod
        self._real_catalog = models_mod.catalog

    def tearDown(self):
        self.models.catalog = self._real_catalog

    def test_hermes_catalog_line(self):
        self.models.catalog = lambda cfg, force=False: {
            "providers": [{"id": "a", "label": "A", "configured": True,
                           "models": [{"id": "m1"}, {"id": "m2"}]},
                          {"id": "b", "label": "B", "configured": True,
                           "models": [{"id": "m3"}]}],
            "source": "hermes:inventory", "warnings": []}
        line = self.models.catalog_line(object())
        self.assertTrue(line["ok"])
        self.assertEqual(line["providers"], 2)
        self.assertEqual(line["models"], 3)
        self.assertIn("Каталог моделей: Hermes (2 провайдера, 3 модели)", line["text"])

    def test_profiles_catalog_line_names_reason(self):
        self.models.catalog = lambda cfg, force=False: {
            "providers": [{"id": "a", "label": "A", "configured": True, "models": [{"id": "m1"}]}],
            "source": "profiles",
            "warnings": ["No module named 'ruamel'"]}
        line = self.models.catalog_line(object())
        self.assertFalse(line["ok"])
        self.assertIn("из профилей", line["text"])
        self.assertIn("ruamel", line["text"])


class ReadOnlyTests(HealthBase):
    def test_health_does_not_write_to_hermes(self):
        self.gateway_state()
        self.token(expiry="2026-10-26T23:11:28Z")
        self.jobs([{"id": "j_daily", "name": "Сводка", "enabled": True,
                    "schedule": {"kind": "cron", "expr": "0 8 * * *"},
                    "schedule_display": "0 8 * * *",
                    "next_run_at": _iso(NOW + DAY), "last_run_at": _iso(NOW - 3600),
                    "last_status": "ok"}])
        self.output("j_daily", "Всё в порядке.")
        before = {str(path): (path.stat().st_mtime, path.stat().st_size)
                  for path in self.home.rglob("*") if path.is_file()}
        health_mod.run(self.cfg)
        after = {str(path): (path.stat().st_mtime, path.stat().st_size)
                 for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
