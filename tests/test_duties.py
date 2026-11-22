"""Дежурства (WAVE24 §3.2, N4): чтение Hermes только на чтение и привязка к комнате.

Всё на ВЫМЫШЛЕННЫХ данных: поддельный домашний каталог Hermes во временной папке
вне реального `%LOCALAPPDATA%\\hermes` (правило WAVE22 §0). Настоящий Hermes нужен
только для живой проверки в отчёте, тесты от него не зависят.

    python -m unittest tests.test_duties -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import duties as duties_mod          # noqa: E402
from tests.helpers import _outside_hermes_base   # noqa: E402

NOW = int(time.time())      # «сейчас» для расчётов: окна пропусков считаются от реального времени
DAY = 86400
# В воскресенье в неделе нет будущих дней — тесты «будущий день/плановые запуски» краснели
# бы раз в неделю по календарю, а не по вине продукта. Воскресенье сдвигаем на пятницу.
if time.localtime(NOW).tm_wday == 6:
    NOW -= 2 * DAY


def _iso(stamp: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(int(stamp)))


def _file_stamp(stamp: int) -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime(int(stamp))) + ".md"


EXECUTIONS_SQL = """
CREATE TABLE executions (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, process_id TEXT NOT NULL,
  pid INTEGER NOT NULL, process_started_at INTEGER,
  status TEXT NOT NULL CHECK(status IN
    ('claimed','running','completed','failed','unknown','cancelled','missed')),
  claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT,
  handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL, delivery_outcome TEXT,
  scheduled_instant TEXT, progress_at TEXT)
"""


class FakeHermes:
    """Поддельный домашний каталог Hermes: jobs.json, executions.db, выводы."""

    def __init__(self, root: Path):
        self.home = Path(root) / "hermes"
        (self.home / "cron").mkdir(parents=True, exist_ok=True)
        self.jobs: list = []
        self.rows: list = []

    # --- наполнение -----------------------------------------------------
    def job(self, job_id: str, name: str, schedule: dict, *, enabled=True,
            next_run_at=None, last_run_at=None, last_status=None, last_error=None):
        self.jobs.append({
            "id": job_id, "name": name, "prompt": "вымышленное задание",
            "schedule": schedule, "schedule_display": schedule.get("display") or "",
            "enabled": enabled, "state": "scheduled",
            "next_run_at": _iso(next_run_at) if next_run_at else None,
            "last_run_at": _iso(last_run_at) if last_run_at else None,
            "last_status": last_status, "last_error": last_error,
            "last_dispatch": ({"scheduled_at": _iso(last_run_at - 12),
                               "lateness_seconds": 12.0} if last_run_at else {}),
        })
        return self

    def run(self, job_id: str, status: str, *, claimed_at: int, started_at=None,
            finished_at=None, scheduled_for=None, error=None, delivery=None):
        self.rows.append({
            "id": "%s-%d" % (job_id, len(self.rows)), "job_id": job_id, "status": status,
            "claimed_at": _iso(claimed_at),
            "started_at": _iso(started_at) if started_at else None,
            "finished_at": _iso(finished_at) if finished_at else None,
            "scheduled_for": _iso(scheduled_for) if scheduled_for else None,
            "error": error, "delivery": delivery,
        })
        return self

    def output(self, job_id: str, stamp: int, body: str, *, heading="## Response"):
        folder = self.home / "cron" / "output" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        text = "# Cron Job: вымышленное задание\n\n**Job ID:** %s\n\n" % job_id
        text += "## Prompt\n\nвымышленный текст задания\n\n" + heading + "\n\n" + body + "\n"
        (folder / _file_stamp(stamp)).write_text(text, encoding="utf-8")
        return self

    def write(self) -> "FakeHermes":
        (self.home / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": self.jobs, "updated_at": _iso(NOW)}, ensure_ascii=False),
            encoding="utf-8")
        db = self.home / "cron" / "executions.db"
        con = sqlite3.connect(str(db))
        try:
            con.execute(EXECUTIONS_SQL)
            for idx, row in enumerate(self.rows):
                con.execute(
                    "INSERT INTO executions (id, job_id, source, process_id, pid, status,"
                    " claimed_at, started_at, finished_at, error, delivery_outcome,"
                    " scheduled_instant) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["job_id"], "builtin", "p%d" % idx, 1000 + idx,
                     row["status"], row["claimed_at"], row["started_at"], row["finished_at"],
                     row["error"], row["delivery"], row["scheduled_for"]))
            con.commit()
        finally:
            con.close()
        return self

    def cfg(self, data_dir: Path):
        return type("Cfg", (), {"hermes_home": self.home, "data_dir": Path(data_dir),
                                "sandbox": True})()


def sample_home(root: Path, *, next_run_offset: int = 0) -> FakeHermes:
    """Три дежурства: живые, с ошибкой и с пропусками — без привязки к бизнесу."""
    fake = FakeHermes(root)
    fake.job("j_daily", "Утренняя сводка по комнатам",
             {"kind": "cron", "expr": "0 8 * * *", "display": "0 8 * * *"},
             next_run_at=NOW + DAY, last_run_at=NOW - 3600, last_status="ok")
    fake.job("j_watch", "Проверка сроков",
             {"kind": "interval", "minutes": 30, "display": "every 30m"},
             next_run_at=NOW + 900, last_run_at=NOW - 200, last_status="error",
             last_error="не удалось открыть список (файл занят)")
    fake.job("j_old", "Резервная копия",
             {"kind": "cron", "expr": "0 2 * * *", "display": "0 2 * * *"},
             enabled=False, next_run_at=None, last_run_at=NOW - 3 * DAY, last_status="ok")
    fake.run("j_daily", "completed", claimed_at=NOW - 3600, started_at=NOW - 3590,
             finished_at=NOW - 3500, scheduled_for=NOW - 3600, delivery="delivered")
    fake.run("j_watch", "failed", claimed_at=NOW - 200, started_at=NOW - 190,
             finished_at=NOW - 180, scheduled_for=NOW - 260,
             error="Не удалось открыть список: файл занят", delivery="failed")
    fake.run("j_watch", "missed", claimed_at=NOW - 2000, scheduled_for=NOW - 2000)
    fake.run("j_watch", "missed", claimed_at=NOW - 4000, scheduled_for=NOW - 4000)
    fake.run("j_watch", "completed", claimed_at=NOW - 6000, started_at=NOW - 5990,
             finished_at=NOW - 5900, scheduled_for=NOW - 6000, delivery="suppressed")
    fake.output("j_daily", NOW - 3600,
                "Собрано 4 запроса: 2 готовы, 1 в работе, 1 ждёт ответа. "
                "Токен доступа обновлён, пароль password=hunter2 не показываем.")
    return fake


class DutiesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w24-n4-", dir=str(_outside_hermes_base())))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "rooms.json").write_text(
            json.dumps({"rooms": [{"id": "studio", "title": "Сайт студии",
                                   "orchestrator": "anna-lead",
                                   "members": [{"profile": "ivan-petrov"}],
                                   "created_at": 1}],
                        "updated_at": NOW}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def make(self, **kwargs) -> FakeHermes:
        fake = sample_home(self.tmp, **kwargs)
        fake.write()
        return fake

    def rooms_now(self) -> dict:
        return json.loads((self.data / "rooms.json").read_text(encoding="utf-8"))


class ScheduleHumanTests(unittest.TestCase):
    """Расписание по-русски: cron, интервалы и разовый запуск."""

    def test_cron_every_day(self):
        self.assertEqual(duties_mod.schedule_human("0 8 * * *"), "каждый день в 08:00")
        self.assertEqual(duties_mod.schedule_human("30 19 * * *"), "каждый день в 19:30")

    def test_cron_step_minutes(self):
        self.assertEqual(duties_mod.schedule_human("*/30 * * * *"), "каждые 30 минут")
        self.assertEqual(duties_mod.schedule_human("*/15 * * * *"), "каждые 15 минут")

    def test_cron_monthly_and_weekdays(self):
        self.assertEqual(duties_mod.schedule_human("0 9 1 * *"),
                         "1-го числа каждого месяца в 09:00")
        self.assertEqual(duties_mod.schedule_human("0 9 * * 1"), "по понедельникам в 09:00")
        self.assertEqual(duties_mod.schedule_human("0 10 * * 0"), "по воскресеньям в 10:00")
        self.assertEqual(duties_mod.schedule_human("30 7 * * 1-5"), "по будням в 07:30")
        self.assertEqual(duties_mod.schedule_human("0 9 * * 0,6"), "по выходным в 09:00")
        self.assertEqual(duties_mod.schedule_human("0 9 * * 1,3"),
                         "по понедельникам и по средам в 09:00")

    def test_interval_forms(self):
        self.assertEqual(duties_mod.schedule_human("every 30m"), "каждые 30 минут")
        self.assertEqual(duties_mod.schedule_human("every 2m"), "каждые 2 минуты")
        self.assertEqual(duties_mod.schedule_human("every 1m"), "каждую минуту")
        self.assertEqual(duties_mod.schedule_human("every 360m"), "каждые 6 часов")
        self.assertEqual(duties_mod.schedule_human("every 2h"), "каждые 2 часа")
        self.assertEqual(duties_mod.schedule_human("every day at 9am"), "каждый день в 09:00")
        self.assertEqual(duties_mod.schedule_human("every day at 8pm"), "каждый день в 20:00")
        self.assertEqual(duties_mod.schedule_human("in 2h"), "один раз через 2 часа")

    def test_schedule_dict_wins(self):
        self.assertEqual(
            duties_mod.schedule_human("0 9 * * 1", {"kind": "cron", "expr": "0 9 * * 1"}),
            "по понедельникам в 09:00")
        self.assertEqual(
            duties_mod.schedule_human("", {"kind": "interval", "minutes": 45}),
            "каждые 45 минут")

    def test_unknown_is_honest(self):
        self.assertEqual(duties_mod.schedule_human("непонятно"), "по расписанию непонятно")
        self.assertEqual(duties_mod.schedule_human(""), "расписание не указано")

    def test_interval_seconds(self):
        self.assertEqual(duties_mod.interval_seconds({"kind": "interval", "minutes": 30}), 1800)
        self.assertEqual(duties_mod.interval_seconds({"kind": "cron", "expr": "0 8 * * *"}), DAY)
        self.assertEqual(duties_mod.interval_seconds({"kind": "cron", "expr": "0 9 * * 1"}), 7 * DAY)
        self.assertEqual(duties_mod.interval_seconds({"kind": "cron", "expr": "0 9 1 * *"}), 30 * DAY)


class DutiesReadTests(DutiesBase):
    """Чтение дежурств: только чтение, форма ответа по контракту §3.2."""

    def test_shape_of_items(self):
        cfg = self.make().cfg(self.data)
        payload = duties_mod.items(cfg, now=NOW)
        self.assertEqual(payload["generated_at"], NOW)
        self.assertEqual(len(payload["items"]), 3)
        keys = {"job_id", "name", "enabled", "schedule", "schedule_human", "bound_rooms",
                "last_run", "next_run_at", "missed_recent", "recent", "excerpt", "problem",
                # W29-P5: чьё это дежурство и куда придёт результат (контракт расширен).
                "scope", "deliver", "model", "provider", "workdir", "skills", "script",
                "monitor_url"}
        for item in payload["items"]:
            self.assertEqual(set(item), keys)
        self.assertIsInstance(payload["items"][0]["bound_rooms"], list)

    def test_last_run_and_missed(self):
        cfg = self.make().cfg(self.data)
        by_id = {item["job_id"]: item for item in duties_mod.items(cfg, now=NOW)["items"]}
        watch = by_id["j_watch"]
        self.assertEqual(watch["last_run"]["status"], "failed")
        self.assertEqual(watch["last_run"]["delivered"], False)
        self.assertEqual(watch["missed_recent"], 2)
        self.assertLessEqual(len(watch["recent"]), 5)
        self.assertEqual(by_id["j_daily"]["last_run"]["status"], "completed")
        self.assertEqual(by_id["j_daily"]["last_run"]["delivered"], True)

    def test_problems_kinds(self):
        cfg = self.make().cfg(self.data)
        by_id = {item["job_id"]: item for item in duties_mod.items(cfg, now=NOW)["items"]}
        self.assertEqual(by_id["j_watch"]["problem"]["kind"], "failed")
        self.assertIn("ошибкой", by_id["j_watch"]["problem"]["text"])
        self.assertEqual(by_id["j_old"]["problem"]["kind"], "disabled_but_due")
        self.assertIsNone(by_id["j_daily"]["problem"])

    def test_never_ran(self):
        fake = FakeHermes(self.tmp)
        fake.job("j_new", "Годовой отчёт", {"kind": "cron", "expr": "0 18 * * 5",
                                            "display": "0 18 * * 5"},
                 next_run_at=NOW + DAY)
        fake.write()
        item = duties_mod.items(fake.cfg(self.data), now=NOW)["items"][0]
        self.assertIsNone(item["last_run"])
        self.assertEqual(item["problem"]["kind"], "never_ran")

    def test_stale_when_schedule_missed(self):
        fake = FakeHermes(self.tmp)
        fake.job("j_late", "Проверка писем",
                 {"kind": "interval", "minutes": 30, "display": "every 30m"},
                 next_run_at=NOW - 7200, last_run_at=NOW - 7200, last_status="ok")
        fake.run("j_late", "completed", claimed_at=NOW - 7200, started_at=NOW - 7190,
                 finished_at=NOW - 7100, scheduled_for=NOW - 7200)
        fake.write()
        item = duties_mod.items(fake.cfg(self.data), now=NOW)["items"][0]
        self.assertEqual(item["problem"]["kind"], "stale")
        self.assertIn("должно было запуститься", item["problem"]["text"])

    def test_excerpt_masked_and_short(self):
        cfg = self.make().cfg(self.data)
        by_id = {item["job_id"]: item for item in duties_mod.items(cfg, now=NOW)["items"]}
        text = by_id["j_daily"]["excerpt"]
        self.assertIn("Собрано 4 запроса", text)
        self.assertNotIn("hunter2", text)
        self.assertLessEqual(len(text), duties_mod.EXCERPT_MAX)

    def test_excerpt_without_response_block_is_empty(self):
        fake = FakeHermes(self.tmp)
        fake.job("j_silent", "Тихая проверка", {"kind": "interval", "minutes": 2})
        fake.write()
        fake.output("j_silent", NOW - 60, "ничего нового", heading="## Status")
        cfg = fake.cfg(self.data)
        self.assertEqual(duties_mod.items(cfg, now=NOW)["items"][0]["excerpt"], "")

    def test_room_filter(self):
        fake = self.make()
        cfg = fake.cfg(self.data)
        data = self.rooms_now()
        data["rooms"][0]["duties"] = ["j_daily", "j_watch"]
        (self.data / "rooms.json").write_text(json.dumps(data, ensure_ascii=False),
                                              encoding="utf-8")
        payload = duties_mod.items(cfg, room="studio", now=NOW)
        self.assertEqual({item["job_id"] for item in payload["items"]}, {"j_daily", "j_watch"})
        for item in payload["items"]:
            self.assertEqual(item["bound_rooms"], ["studio"])
        self.assertEqual(len(duties_mod.items(cfg, room="all", now=NOW)["items"]), 3)

    def test_read_only_no_writes(self):
        fake = self.make()
        before = {path.name: (path.stat().st_mtime, path.stat().st_size)
                  for path in (fake.home / "cron").rglob("*") if path.is_file()}
        duties_mod.items(fake.cfg(self.data), now=NOW)
        duties_mod.problems(fake.cfg(self.data), now=NOW)
        duties_mod.token_hits(fake.cfg(self.data))
        after = {path.name: (path.stat().st_mtime, path.stat().st_size)
                 for path in (fake.home / "cron").rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_missing_home_is_not_a_crash(self):
        empty = self.tmp / "пусто"
        empty.mkdir()
        cfg = type("Cfg", (), {"hermes_home": empty, "data_dir": self.data})()
        payload = duties_mod.items(cfg, now=NOW)
        self.assertEqual(payload["items"], [])

    def test_broken_jobs_json(self):
        fake = self.make()
        (fake.home / "cron" / "jobs.json").write_text("{битый json", encoding="utf-8")
        payload = duties_mod.items(fake.cfg(self.data), now=NOW)
        self.assertEqual(payload["items"], [])


class TokenHitsTests(DutiesBase):
    """Ошибка доступа к календарю ищется в итогах дежурств, а не в памятках."""

    def test_invalid_grant_found_in_response(self):
        fake = self.make()
        fake.output("j_daily", NOW - 600, "Календарь: `invalid_grant` — нужна переавторизация.")
        cfg = fake.cfg(self.data)
        hits = duties_mod.token_hits(cfg, days=30)
        self.assertTrue(hits)
        self.assertTrue(any(hit["job_id"] == "j_daily" for hit in hits))

    def test_mention_in_prompt_is_not_a_hit(self):
        fake = self.make()
        folder = fake.home / "cron" / "output" / "j_daily"
        (folder / _file_stamp(NOW - 300)).write_text(
            "## Prompt\n\nВ памятке написано: REFRESH_FAILED — это пример.\n\n"
            "## Response\n\nВсё в порядке, календарь обновлён.\n", encoding="utf-8")
        cfg = fake.cfg(self.data)
        hits = [hit for hit in duties_mod.token_hits(cfg, days=30)
                if hit["job_id"] == "j_daily"]
        self.assertEqual(hits, [])


class BindDutiesTests(DutiesBase):
    """Привязка дежурств к комнате: реестр комнат, Hermes не меняется."""

    def setUp(self):
        super().setUp()
        self.fake = self.make()
        self.cfg = self.fake.cfg(self.data)
        self.room = {"id": "studio", "title": "Сайт студии"}

    def test_bind_and_store(self):
        bound = duties_mod.set_room_duties(self.cfg, self.room, ["j_daily", "j_watch"])
        self.assertEqual(bound, ["j_daily", "j_watch"])
        self.assertEqual(self.rooms_now()["rooms"][0]["duties"], ["j_daily", "j_watch"])
        self.assertEqual(duties_mod.bound_rooms(self.cfg, "j_watch"), ["studio"])

    def test_dedupe_and_clean(self):
        bound = duties_mod.set_room_duties(self.cfg, self.room,
                                           ["j_daily", "j_daily", "", None, "j_watch"])
        self.assertEqual(bound, ["j_daily", "j_watch"])

    def test_unknown_job_rejected(self):
        with self.assertRaises(duties_mod.DutiesError) as ctx:
            duties_mod.set_room_duties(self.cfg, self.room, ["j_daily", "nosuchjob01"])
        self.assertEqual(ctx.exception.http, 422)
        self.assertEqual(ctx.exception.detail.get("unknown"), ["nosuchjob01"])
        self.assertEqual(self.rooms_now()["rooms"][0].get("duties", []), [])

    def test_bad_body_rejected(self):
        with self.assertRaises(duties_mod.DutiesError):
            duties_mod.set_room_duties(self.cfg, self.room, "j_daily")
        with self.assertRaises(duties_mod.DutiesError):
            duties_mod.check_duties(["плохой id!"])

    def test_too_many(self):
        many = ["j%d" % idx for idx in range(duties_mod.MAX_BOUND + 1)]
        with self.assertRaises(duties_mod.DutiesError):
            duties_mod.check_duties(many)

    def test_unbind(self):
        duties_mod.set_room_duties(self.cfg, self.room, ["j_daily"])
        self.assertEqual(duties_mod.set_room_duties(self.cfg, self.room, []), [])
        self.assertEqual(self.rooms_now()["rooms"][0]["duties"], [])

    def test_hermes_untouched(self):
        before = {path.name: path.stat().st_mtime
                  for path in (self.fake.home / "cron").rglob("*") if path.is_file()}
        duties_mod.set_room_duties(self.cfg, self.room, ["j_daily"])
        after = {path.name: path.stat().st_mtime
                 for path in (self.fake.home / "cron").rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_unknown_room(self):
        with self.assertRaises(duties_mod.DutiesError):
            duties_mod.set_room_duties(self.cfg, {"id": ""}, ["j_daily"])


class RoomsFieldTests(DutiesBase):
    """Поле `duties` в реестре комнат: чтение старых записей и проверка значений."""

    def test_old_room_reads_as_empty(self):
        from server import rooms as rooms_mod
        cfg = self.make().cfg(self.data)
        room = rooms_mod.get_room(cfg, "studio")
        self.assertEqual(room["duties"], [])

    def test_broken_value_reads_as_empty(self):
        from server import rooms as rooms_mod
        cfg = self.make().cfg(self.data)
        data = self.rooms_now()
        data["rooms"][0]["duties"] = "мусор"
        (self.data / "rooms.json").write_text(json.dumps(data, ensure_ascii=False),
                                              encoding="utf-8")
        self.assertEqual(rooms_mod.get_room(cfg, "studio")["duties"], [])

    def test_patch_field_allowed(self):
        from server import rooms as rooms_mod
        cfg = self.make().cfg(self.data)
        updated = rooms_mod.update_room(cfg, "studio", {"duties": ["j_daily"]})
        self.assertEqual(updated["duties"], ["j_daily"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
