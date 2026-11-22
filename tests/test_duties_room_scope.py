"""W29-P5: дежурства комнаты, календарь, «куда придёт результат», выбор папки.

Проверяем то, ради чего пакет:

* список комнаты = только её дежурства, системные задачи Hermes — отдельно и с
  счётчиком, в календарь комнаты не попадают;
* календарь листается (`start`), у будущих дней есть плановые запуски списком;
* «куда придёт результат» — «Входящие» комнаты со ссылкой, у непривязанного — честно
  «не в комнату»; модель и папка видны в пункте (без «дешёвая по умолчанию»);
* окно выбора папки/файла в песочнице берёт путь из `TEAMROOM_PICK_FAKE`, пустая
  строка = «закрыли», путь проверяется (`check_path`).

Всё на вымышленных данных: поддельный домашний каталог Hermes во временной папке
вне реального `%LOCALAPPDATA%\\hermes`, CLI Hermes не вызывается.

    python -m unittest tests.test_duties_room_scope -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import duties as duties_mod          # noqa: E402
from server import folder_pick                  # noqa: E402
from tests.test_duties import NOW, DAY, FakeHermes, DutiesBase   # noqa: E402


class ScopeBase(DutiesBase):
    """Три задания: два привязаны к комнате, одно — общая задача Hermes."""

    def make_scope(self):
        fake = FakeHermes(self.tmp)
        fake.job("j_room_one", "Утренняя сводка", {"kind": "cron", "expr": "0 8 * * *",
                                                   "display": "0 8 * * *"})
        fake.job("j_room_two", "Проверка сроков", {"kind": "interval", "minutes": 30,
                                                    "display": "every 30m"})
        fake.job("j_system", "Обслуживание кэша", {"kind": "interval", "minutes": 2,
                                                    "display": "every 2m"})
        fake.write()
        cfg = fake.cfg(self.data)
        rooms = self.rooms_now()
        rooms["rooms"].append({"id": "cafe", "title": "Кофейня", "created_at": 2,
                               "duties": ["j_room_one", "j_room_two"]})
        (self.data / "rooms.json").write_text(json.dumps(rooms, ensure_ascii=False),
                                              encoding="utf-8")
        return fake, cfg


class RoomScopeTests(ScopeBase):
    def test_room_list_has_only_room_duties(self):
        _, cfg = self.make_scope()
        payload = duties_mod.items(cfg, room="cafe", now=NOW)
        self.assertEqual({one["job_id"] for one in payload["items"]},
                         {"j_room_one", "j_room_two"})
        for one in payload["items"]:
            self.assertEqual("room", one["scope"])
        # системная задача в список комнаты не попала, но видна отдельно
        self.assertEqual([one["job_id"] for one in payload["system"]], ["j_system"])
        self.assertEqual(payload["counts"]["system"], 1)
        self.assertEqual(payload["counts"]["room"], 2)

    def test_system_jobs_are_separate_from_counts(self):
        _, cfg = self.make_scope()
        payload = duties_mod.items(cfg, now=NOW)
        # `items` остаётся полным списком (совместимость), системные помечены.
        self.assertEqual(len(payload["items"]), 3)
        self.assertEqual({one["job_id"] for one in payload["system"]}, {"j_system"})
        self.assertEqual(payload["counts"]["total"], 3)
        self.assertEqual(payload["counts"]["system"], 1)

    def test_system_job_is_not_in_room_week(self):
        _, cfg = self.make_scope()
        week = duties_mod.week(cfg, room="cafe", now=NOW)
        seen = {job["job_id"] for day in week["days"] for job in day["jobs"]}
        planned = {job["job_id"] for day in week["days"] for job in day["planned_jobs"]}
        self.assertNotIn("j_system", seen | planned)


class DeliverTests(ScopeBase):
    def test_bound_duty_delivers_to_room_inbox(self):
        _, cfg = self.make_scope()
        by_id = {one["job_id"]: one for one in duties_mod.items(cfg, room="cafe", now=NOW)["items"]}
        deliver = by_id["j_room_one"]["deliver"]
        self.assertEqual(deliver["where"], "inbox")
        self.assertEqual(deliver["label"], "Входящие комнаты")
        self.assertEqual(deliver["href"], "#/room/cafe/inbox")
        self.assertIn("при изменении", deliver["text"])

    def test_unbound_duty_says_so(self):
        _, cfg = self.make_scope()
        by_id = {one["job_id"]: one for one in duties_mod.items(cfg, now=NOW)["items"]}
        deliver = by_id["j_system"]["deliver"]
        self.assertEqual(deliver["where"], "local")
        self.assertEqual(deliver["href"], "")
        self.assertIn("не привязано", deliver["text"])

    def test_model_and_workdir_are_reported(self):
        fake = FakeHermes(self.tmp)
        job = {"id": "j_model", "name": "Обзор на модели", "prompt": "проверить",
               "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
               "schedule_display": "0 9 * * *", "enabled": True,
               "model": "small-model", "provider": "fictional", "workdir": "C:/Work/Rooms/cafe"}
        fake.jobs.append(job)
        fake.write()
        cfg = fake.cfg(self.data)
        item = duties_mod.items(cfg, now=NOW)["items"][0]
        self.assertEqual(item["model"], "small-model")
        self.assertEqual(item["provider"], "fictional")
        self.assertEqual(item["workdir"], "C:/Work/Rooms/cafe")


class PatchModelTests(ScopeBase):
    """Смена модели в мастере правки действительно уходит в Hermes (не тихо игнорируется)."""

    def test_model_change_is_in_hermes_command(self):
        _, cfg = self.make_scope()
        room = {"id": "cafe", "title": "Кофейня", "duties": ["j_room_one", "j_room_two"]}
        view = duties_mod.patch(cfg, "j_room_one", room,
                                {"model": "small-model", "provider": "fictional"},
                                preview_only=True)
        self.assertTrue(view.get("preview"))
        command = str(view.get("command") or "")
        self.assertIn("--model", command)
        self.assertIn("small-model", command)
        self.assertIn("--provider", command)

    def test_same_model_changes_nothing(self):
        _, cfg = self.make_scope()
        room = {"id": "cafe", "duties": ["j_room_one"]}
        view = duties_mod.patch(cfg, "j_room_one", room, {"model": ""}, preview_only=True)
        self.assertEqual(view.get("changes"), [])
        self.assertEqual(str(view.get("text") or ""), "Менять нечего: поля те же.")


class WeekPagingTests(ScopeBase):
    def test_week_has_prev_next_and_today(self):
        _, cfg = self.make_scope()
        week = duties_mod.week(cfg, now=NOW)
        monday = datetime.fromtimestamp(NOW).date() - timedelta(
            days=datetime.fromtimestamp(NOW).weekday())
        self.assertEqual(week["start"], monday.isoformat())
        self.assertEqual(week["prev"], (monday - timedelta(days=7)).isoformat())
        self.assertEqual(week["next"], (monday + timedelta(days=7)).isoformat())
        self.assertEqual(week["today"], datetime.fromtimestamp(NOW).date().isoformat())

    def test_week_start_moves_window(self):
        _, cfg = self.make_scope()
        monday = datetime.fromtimestamp(NOW).date() - timedelta(
            days=datetime.fromtimestamp(NOW).weekday())
        shift = (monday + timedelta(days=7)).isoformat()
        week = duties_mod.week(cfg, now=NOW, start=shift)
        self.assertEqual(week["start"], shift)
        self.assertFalse(any(day["today"] for day in week["days"]))

    def test_week_start_rejects_garbage(self):
        _, cfg = self.make_scope()
        with self.assertRaises(ValueError):
            duties_mod.week(cfg, now=NOW, start="позавчера")

    def test_future_day_lists_planned_jobs(self):
        _, cfg = self.make_scope()
        week = duties_mod.week(cfg, room="cafe", now=NOW)
        future = [day for day in week["days"] if day["future"]]
        self.assertTrue(future)
        day = future[0]
        self.assertEqual(day["runs"], 0)
        self.assertGreater(day["planned"], 0)
        names = {one["job_id"] for one in day["planned_jobs"]}
        self.assertEqual(names, {"j_room_one", "j_room_two"})
        for one in day["planned_jobs"]:
            self.assertGreater(one["runs"], 0)
            self.assertTrue(one["name"])


class FolderPickTests(unittest.TestCase):
    """Диалог выбора папки: песочница не открывает окно Windows."""

    def setUp(self):
        self._old = os.environ.get(folder_pick.FAKE_ENV)
        self.tmp = Path(self._tmpdir())

    def _tmpdir(self):
        import tempfile
        return tempfile.mkdtemp(prefix="tr-w29p5-")

    def tearDown(self):
        if self._old is None:
            os.environ.pop(folder_pick.FAKE_ENV, None)
        else:
            os.environ[folder_pick.FAKE_ENV] = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_folder_from_fake(self):
        os.environ[folder_pick.FAKE_ENV] = "C:/Work/Rooms/cafe"
        self.assertEqual(folder_pick.pick_folder("Рабочая папка"), "C:/Work/Rooms/cafe")

    def test_file_mode_from_fake_json(self):
        os.environ[folder_pick.FAKE_ENV] = json.dumps(["C:/Work/Rooms/cafe/plan.md"])
        self.assertEqual(folder_pick.pick_folder("Следить за файлом", mode="file"),
                         "C:/Work/Rooms/cafe/plan.md")

    def test_empty_fake_means_cancelled(self):
        os.environ[folder_pick.FAKE_ENV] = "   "
        self.assertEqual(folder_pick.pick_folder("Рабочая папка"), "")

    def test_bad_mode_falls_back_to_folder(self):
        self.assertEqual(folder_pick.normalize_mode("каша"), "folder")
        self.assertEqual(folder_pick.normalize_mode("FILE"), "file")

    def test_check_path_tells_what_is_there(self):
        folder = self.tmp / "папка"
        folder.mkdir()
        file_path = self.tmp / "файл.txt"
        file_path.write_text("вымышленный текст", encoding="utf-8")
        self.assertEqual(folder_pick.check_path(str(folder))["kind"], "folder")
        self.assertEqual(folder_pick.check_path(str(file_path))["kind"], "file")
        missing = folder_pick.check_path(str(self.tmp / "нет-такой"))
        self.assertFalse(missing["ok"])
        self.assertIn("нет на диске", missing["message"])
        self.assertFalse(folder_pick.check_path("")["ok"])


if __name__ == "__main__":
    unittest.main()
