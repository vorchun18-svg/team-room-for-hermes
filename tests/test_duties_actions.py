"""Дежурства: действия по подтверждению и недельная сетка (WAVE26 §2, D8).

Проверяем то, что человек нажимает и что при этом происходит на самом деле:

* без подтверждения (`confirm`) ничего не делается;
* «Запустить сейчас / Пауза / Возобновить» — ровно `hermes cron <run|pause|resume> <id>`,
  в тестах — подставной исполнитель, настоящий Hermes и реальные кроны не трогаются;
* «Привязать / Отвязать» меняют только реестр комнат;
* «Изменить расписание» — только предложение, Hermes не меняется;
* недельная сетка: 7 дней с понедельника, запуски по дням, «+N ещё», план.

    python -m unittest tests.test_duties_actions -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import duties as duties_mod          # noqa: E402
from tests.helpers import _outside_hermes_base   # noqa: E402
from tests.test_duties import NOW, DAY, DutiesBase, FakeHermes, sample_home  # noqa: E402


class FakeCli:
    """Подставной `hermes`: записывает вызовы, отвечает заданным кодом."""

    def __init__(self, code: int = 0, out: str = "ok", err: str = ""):
        self.calls: list[list[str]] = []
        self.code = code
        self.out = out
        self.err = err

    def __call__(self, args):
        self.calls.append(list(args))
        return {"code": self.code, "out": self.out, "err": self.err}


def _snapshot(folder: Path) -> list[tuple]:
    """Слепок файлов каталога (имя, размер, mtime) — «в Hermes не писали»."""
    out = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            info = path.stat()
            out.append((str(path.relative_to(folder)), int(info.st_size), int(info.st_mtime)))
    return out


class ActionsBase(DutiesBase):
    def setUp(self):
        super().setUp()
        self.fake = sample_home(self.tmp)
        self.fake.write()
        self.cfg = self.fake.cfg(self.data)
        self.cli = FakeCli()
        duties_mod.set_cli_runner(self.cli)
        self._dry = os.environ.pop("TEAMROOM_DUTIES_DRY", None)

    def tearDown(self):
        duties_mod.reset_cli_runner()
        if self._dry is not None:
            os.environ["TEAMROOM_DUTIES_DRY"] = self._dry
        super().tearDown()

    def room(self) -> dict:
        return {"id": "studio", "title": "Сайт студии", "duties": []}

    def cron_snapshot(self):
        return _snapshot(self.fake.home / "cron")


class ConfirmTests(ActionsBase):
    """Без подтверждения человека не делаем ничего видимого снаружи."""

    def test_requires_confirm(self):
        for action in ("run", "pause", "resume", "bind", "unbind", "schedule"):
            with self.assertRaises(duties_mod.DutiesError) as ctx:
                duties_mod.act(self.cfg, self.room(), "j_daily", action,
                               confirm=False, schedule="every 2h")
            self.assertEqual(ctx.exception.http, 409, action)
            self.assertEqual(ctx.exception.code, "confirm_required", action)
        self.assertEqual(self.cli.calls, [], "без подтверждения CLI не вызывали")

    def test_unknown_action(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.act(self.cfg, None, "j_daily", "delete", confirm=True)
        self.assertEqual(ctx.exception.http, 400)

    def test_unknown_job(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.act(self.cfg, None, "nosuchjob01", "run", confirm=True)
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.code, "unknown_job")
        self.assertEqual(self.cli.calls, [])


class CliActionTests(ActionsBase):
    """Запуск/пауза/возобновление — штатный CLI, ровно три слова."""

    def test_run_pause_resume_call_exact_cli(self):
        for action in ("run", "pause", "resume"):
            result = duties_mod.act(self.cfg, None, "j_daily", action, confirm=True)
            self.assertTrue(result["applied"])
            self.assertFalse(result["dry"])
            self.assertEqual(result["action"], action)
            self.assertEqual(result["command"], "hermes cron %s j_daily" % action)
            self.assertTrue(result["text"].strip())
        self.assertEqual(self.cli.calls, [["cron", "run", "j_daily"],
                                          ["cron", "pause", "j_daily"],
                                          ["cron", "resume", "j_daily"]])

    def test_cli_failure_is_reported_honestly(self):
        duties_mod.set_cli_runner(FakeCli(code=1, err="нет такого задания"))
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.act(self.cfg, None, "j_daily", "run", confirm=True)
        self.assertEqual(ctx.exception.http, 502)
        self.assertEqual(ctx.exception.code, "cli_failed")
        self.assertIn("нет такого задания", ctx.exception.detail.get("err", ""))

    def test_action_does_not_write_into_hermes(self):
        before = self.cron_snapshot()
        duties_mod.act(self.cfg, None, "j_daily", "run", confirm=True)
        duties_mod.act(self.cfg, None, "j_watch", "pause", confirm=True)
        self.assertEqual(before, self.cron_snapshot())

    def test_dry_run_does_not_call_cli(self):
        os.environ["TEAMROOM_DUTIES_DRY"] = "1"
        try:
            result = duties_mod.act(self.cfg, None, "j_daily", "run", confirm=True)
        finally:
            os.environ.pop("TEAMROOM_DUTIES_DRY", None)
        self.assertTrue(result["dry"])
        self.assertFalse(result["applied"])
        self.assertIn("не запускалась", result["text"])
        self.assertEqual(self.cli.calls, [])

    def test_real_runner_is_only_hermes_cron_with_fake_home(self):
        """Настоящий путь: argv без свободы, HOME — поддельный, shell не используется."""
        seen = {}

        class Proc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return Proc()

        real_run = duties_mod.subprocess.run
        duties_mod.subprocess.run = fake_run
        try:
            duties_mod.reset_cli_runner()
            duties_mod.act(self.cfg, None, "j_watch", "pause", confirm=True)
        finally:
            duties_mod.subprocess.run = real_run
        self.assertEqual(seen["argv"], ["hermes", "cron", "pause", "j_watch"])
        self.assertFalse(seen["kwargs"].get("shell", False))
        self.assertEqual(seen["kwargs"]["env"]["HERMES_HOME"], str(self.fake.home))
        self.assertIn("timeout", seen["kwargs"])


class BindTests(ActionsBase):
    """Привязка меняет только реестр комнат."""

    def test_bind_and_unbind_touch_only_rooms(self):
        before = self.cron_snapshot()
        result = duties_mod.act(self.cfg, self.room(), "j_watch", "bind", confirm=True)
        self.assertTrue(result["applied"])
        self.assertEqual(result["duties"], ["j_watch"])
        self.assertEqual(result["bound_rooms"], ["studio"])
        rooms = json.loads((self.data / "rooms.json").read_text(encoding="utf-8"))
        self.assertEqual(rooms["rooms"][0]["duties"], ["j_watch"])

        room = dict(rooms["rooms"][0])
        result = duties_mod.act(self.cfg, room, "j_watch", "unbind", confirm=True)
        self.assertEqual(result["duties"], [])
        rooms = json.loads((self.data / "rooms.json").read_text(encoding="utf-8"))
        self.assertEqual(rooms["rooms"][0]["duties"], [])
        self.assertEqual(before, self.cron_snapshot())
        self.assertEqual(self.cli.calls, [])

    def test_bind_needs_room(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.act(self.cfg, None, "j_watch", "bind", confirm=True)
        self.assertEqual(ctx.exception.code, "room_required")


class ScheduleProposalTests(ActionsBase):
    """Правка расписания — только предложение, Hermes не меняется."""

    def test_proposal_saved_and_hermes_untouched(self):
        before = self.cron_snapshot()
        jobs_before = (self.fake.home / "cron" / "jobs.json").read_text(encoding="utf-8")
        result = duties_mod.act(self.cfg, self.room(), "j_watch", "schedule",
                                confirm=True, schedule="every 2h")
        self.assertFalse(result["applied"])
        self.assertEqual(result["proposal"]["to"], "every 2h")
        self.assertEqual(result["proposal"]["from"], "каждые 30 минут")
        self.assertIn("не изменено", result["text"])
        self.assertEqual(before, self.cron_snapshot())
        self.assertEqual(jobs_before, (self.fake.home / "cron" / "jobs.json").read_text(encoding="utf-8"))
        self.assertEqual(self.cli.calls, [])
        stored = duties_mod.proposals(self.cfg, "j_watch")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["state"], "proposed")

    def test_schedule_empty(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.act(self.cfg, None, "j_watch", "schedule", confirm=True, schedule="  ")
        self.assertEqual(ctx.exception.code, "schedule_required")


class WeekTests(ActionsBase):
    """Недельная сетка: дни, запуски, «+N ещё», итог и план."""

    def _seed_week(self, extra_jobs=(), extra_runs=()):
        """Свежий поддельный Hermes: запуски на разных днях недели (ошибка и пропуск)."""
        shutil.rmtree(self.fake.home / "cron", ignore_errors=True)
        fake = FakeHermes(self.tmp)
        fake.job("j_daily", "Утренняя сводка по комнатам",
                 {"kind": "cron", "expr": "0 8 * * *", "display": "0 8 * * *"},
                 next_run_at=NOW + DAY, last_run_at=NOW - 3600, last_status="ok")
        fake.job("j_watch", "Проверка сроков",
                 {"kind": "interval", "minutes": 30, "display": "every 30m"},
                 next_run_at=NOW + 900, last_run_at=NOW - 200, last_status="error")
        fake.job("j_old", "Резервная копия",
                 {"kind": "cron", "expr": "0 2 * * *", "display": "0 2 * * *"},
                 enabled=False, next_run_at=None, last_run_at=NOW - 3 * DAY, last_status="ok")
        for job in extra_jobs:
            fake.job(*job[0], **job[1])
        monday = datetime.fromtimestamp(NOW).date() - timedelta(
            days=datetime.fromtimestamp(NOW).weekday())
        base = int(datetime(monday.year, monday.month, monday.day, 9, 0).timestamp())
        fake.run("j_daily", "completed", claimed_at=base, started_at=base)
        fake.run("j_watch", "completed", claimed_at=base + 60, started_at=base + 60)
        fake.run("j_watch", "failed", claimed_at=base + 2 * DAY, started_at=base + 2 * DAY,
                 error="файл занят")
        fake.run("j_watch", "missed", claimed_at=base + 2 * DAY + 60)
        fake.run("j_old", "completed", claimed_at=base + 2 * DAY + 120)
        for spec in extra_runs:
            fake.run(*spec[0], **spec[1])
        fake.write()
        self.fake = fake
        self.cfg = fake.cfg(self.data)
        return monday

    def test_week_shape(self):
        monday = self._seed_week()
        week = duties_mod.week(self.cfg, now=NOW)
        self.assertEqual(week["start"], monday.isoformat())
        self.assertEqual(len(week["days"]), 7)
        self.assertEqual(week["days"][0]["label"], "пн")
        self.assertEqual(week["days"][6]["label"], "вс")
        self.assertEqual([day["weekday"] for day in week["days"]], [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(sum(1 for day in week["days"] if day["today"]), 1)

    def test_week_counts_runs_and_failures(self):
        monday = self._seed_week()
        week = duties_mod.week(self.cfg, now=NOW)
        by_date = {day["date"]: day for day in week["days"]}
        mon = by_date[monday.isoformat()]
        wed = by_date[(monday + timedelta(days=2)).isoformat()]
        self.assertEqual(mon["runs"], 2)
        self.assertEqual(mon["failed"], 0)
        self.assertEqual(wed["runs"], 3)
        self.assertEqual(wed["failed"], 1)
        self.assertEqual(wed["missed"], 1)
        self.assertEqual(week["runs"], mon["runs"] + wed["runs"])
        self.assertEqual(week["failed"], 1)
        self.assertEqual(week["missed"], 1)

    def test_week_more_and_planned(self):
        # Четвёртое задание — чтобы в среду было больше трёх разных и появилось «+N ещё».
        today = datetime.fromtimestamp(NOW)
        monday_date = today.date() - timedelta(days=today.weekday())
        wed = int(datetime(monday_date.year, monday_date.month, monday_date.day,
                           12, 0).timestamp()) + 2 * DAY
        monday = self._seed_week(
            extra_jobs=[(("j_extra", "Выгрузка отчёта",
                          {"kind": "cron", "expr": "0 12 * * *", "display": "0 12 * * *"}), {}),
                        (("j_more", "Сверка справочника",
                          {"kind": "cron", "expr": "0 13 * * *", "display": "0 13 * * *"}), {})],
            extra_runs=[(("j_extra", "completed"), {"claimed_at": wed, "started_at": wed}),
                        (("j_more", "completed"), {"claimed_at": wed + 3600,
                                                   "started_at": wed + 3600})])
        week = duties_mod.week(self.cfg, now=NOW)
        by_date = {day["date"]: day for day in week["days"]}
        wednesday = by_date[(monday + timedelta(days=2)).isoformat()]
        self.assertEqual(wednesday["runs"], 5)                     # 3 + 2 добавленных
        self.assertEqual(len(wednesday["jobs"]), duties_mod.WEEK_DAY_JOBS)
        self.assertEqual(wednesday["more"], 1)                     # «+1 ещё»
        # План: у «каждые 30 минут» 48 запусков в сутки, у дневных — по одному,
        # у выключенного — ноль.
        self.assertGreaterEqual(wednesday["planned"], 48)
        future = [day for day in week["days"] if day["future"]]
        self.assertTrue(future)
        self.assertEqual(future[0]["runs"], 0)          # будущее честно пустое
        self.assertGreater(future[0]["planned"], 0)     # но план есть

    def test_week_filters_by_room(self):
        monday = self._seed_week()
        duties_mod.set_room_duties(self.cfg, self.room(), ["j_daily"])
        week = duties_mod.week(self.cfg, room="studio", now=NOW)
        self.assertEqual(week["runs"], 1)               # только j_daily в понедельник
        by_date = {day["date"]: day for day in week["days"]}
        self.assertEqual(by_date[monday.isoformat()]["runs"], 1)
        self.assertEqual(len(by_date[monday.isoformat()]["jobs"]), 1)
        self.assertEqual(by_date[monday.isoformat()]["jobs"][0]["job_id"], "j_daily")

    def test_items_stay_list_and_week_is_separate(self):
        payload = duties_mod.items(self.cfg, now=NOW)
        # `items()` остаётся списком дежурств; неделю считает `week()` (маршрут склеивает).
        self.assertIsInstance(payload["items"], list)
        self.assertEqual(payload["generated_at"], NOW)


if __name__ == "__main__":
    unittest.main()
