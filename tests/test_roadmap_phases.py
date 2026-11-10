"""W17-R (B-8, B-9, B-10): этапы проекта — свойство комнаты, а не константа.

Проверяется то, что видел пользователь как баг:

* в «Карте проекта» любой комнаты висели шесть этапов чужого проекта (константа
  `PHASES` в трёх файлах) — теперь у комнаты свой необязательный список этапов;
* у комнаты без этапов нет ни полосы этапов, ни чипа «этап»;
* этапы, которые комната уже вела, подхватываются миграцией ЧТЕНИЕМ из её
  `roadmap.json` (данные не переписываются), пустая заготовка данных не даёт;
* переименование и порядок этапов не ломают привязку — связка по `id`,
  а старые ответы с названиями этапов сопоставляются по названию;
* новые комнаты запускают части работы сразу (решение Михаила), настройки
  существующих комнат не меняются;
* в коде нет названий этапов какого-либо проекта (портативность).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, ingest, phase_book, read_view, rooms  # noqa: E402


class PhaseBookTest(unittest.TestCase):
    """Чистые функции: чтение, проверка и показ этапов комнаты."""

    def test_norm_is_tolerant(self):
        self.assertEqual(phase_book.norm(None), [])
        self.assertEqual(phase_book.norm("этап"), [])
        got = phase_book.norm([
            {"title": "Идея"},
            {"id": "legal", "title": "  Согласование  "},
            {"id": "legal", "title": "Дубль"},
            {"title": ""},
            "Производство",
            {"title": "X" * 200},
        ])
        self.assertEqual([p["id"] for p in got],
                         ["идея", "legal", "производство", "x" * phase_book.MAX_ID])
        self.assertEqual(got[1]["title"], "Согласование")
        self.assertEqual(len(got[3]["title"]), phase_book.MAX_TITLE, "длинный заголовок обрезается")

    def test_check_rejects_junk(self):
        self.assertEqual(phase_book.check(None), [])
        self.assertEqual(phase_book.check([]), [])
        with self.assertRaises(phase_book.PhaseError):
            phase_book.check("этап")
        with self.assertRaises(phase_book.PhaseError):
            phase_book.check([{"title": "  "}])
        with self.assertRaises(phase_book.PhaseError):
            phase_book.check([{"id": "a", "title": "A"}, {"id": "a", "title": "B"}])
        with self.assertRaises(phase_book.PhaseError):
            phase_book.check([{"title": "X" * 81}])
        too_many = [{"title": "Этап %d" % i} for i in range(phase_book.MAX_PHASES + 1)]
        with self.assertRaises(phase_book.PhaseError):
            phase_book.check(too_many)

    def test_stage_maps_only_room_phases(self):
        phases = [{"id": "idea", "title": "Идея"}, {"id": "legal", "title": "Согласование"}]
        canon = {"stage": {"current": "legal", "phases": [
            {"id": "idea", "status": "done", "note": "утвердили"},
            {"id": "legal", "status": "active", "note": "в процессе"},
            {"id": "sales", "status": "blocked", "note": "чужой этап"},
        ]}}
        out = phase_book.stage(phases, canon)
        self.assertEqual([p["id"] for p in out["phases"]], ["idea", "legal"])
        self.assertEqual(out["current"], "legal")
        self.assertEqual(out["phases"][0]["status"], "done")
        self.assertEqual(out["phases"][0]["note"], "утвердили")

    def test_stage_matches_by_title_when_ids_differ(self):
        # Старые ответы называли этапы словами: «Легализация» вместо id.
        phases = [{"id": "legal", "title": "Легализация"}]
        canon = {"stage": {"current": "Легализация", "phases": [
            {"id": "Легализация", "title": "Легализация", "status": "active", "note": "ИП подан"}]}}
        out = phase_book.stage(phases, canon)
        self.assertEqual(out["current"], "legal")
        self.assertEqual(out["phases"][0]["status"], "active")
        self.assertEqual(out["phases"][0]["title"], "Легализация")

    def test_stage_rename_keeps_binding(self):
        before = phase_book.stage([{"id": "legal", "title": "Легализация"}],
                                  {"stage": {"phases": [{"id": "legal", "status": "done"}]}})
        after = phase_book.stage([{"id": "legal", "title": "Юридическая часть"}],
                                 {"stage": {"phases": [{"id": "legal", "status": "done"}]}})
        self.assertEqual(before["phases"][0]["status"], after["phases"][0]["status"])
        self.assertEqual(after["phases"][0]["title"], "Юридическая часть")

    def test_no_phases_no_stage(self):
        self.assertEqual(phase_book.stage([], {"stage": {"current": "legal", "phases": []}}),
                         {"current": None, "phases": []})
        self.assertEqual(read_view.PHASES, [], "константы этапов в коде больше нет")
        self.assertEqual(read_view.stage({"stage": {"current": "legal"}}),
                         {"current": None, "phases": []})


class RoomPhaseSettingsTest(unittest.TestCase):
    """Поле `phases` в реестре комнат: создание, правка, чтение."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-phases-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
            "TEAMROOM_SEED": "1",
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        rooms.seed_defaults(self.cfg)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _create(self, **over):
        data = {
            "title": "Новая комната", "orchestrator": "default",
            "members": [{"profile": "lawyer", "role": "worker"}],
        }
        data.update(over)
        return rooms.create_room(self.cfg, data)

    def test_new_room_has_no_phases(self):
        room = self._create()
        self.assertEqual(room["phases"], [], "новая комната — без этапов")

    def test_new_room_launches_parts_at_once(self):
        room = self._create()
        self.assertEqual(room["launch_mode"], "auto", "новые комнаты — сразу, без подтверждения")
        self.assertEqual(room["max_parallel_streams"], 2, "одновременно 2 части")

    def test_existing_room_settings_untouched(self):
        # Комната без полей (как живые cafe/fn) читается по-прежнему: «сначала план».
        rooms.update_room(self.cfg, "fn", {"title": "Рабочее пространство"})
        path = Path(self.cfg.data_dir) / "rooms.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for rec in data["rooms"]:
            for key in ("launch_mode", "max_parallel_streams", "project_mode", "phases"):
                rec.pop(key, None)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        fn = rooms.get_room(self.cfg, "fn")
        self.assertEqual(fn["launch_mode"], "plan_first")
        self.assertEqual(fn["max_parallel_streams"], 2)
        self.assertIsNone(fn["phases"], "нет поля — значит «взять из карты комнаты»")

    def test_edit_add_rename_reorder_delete(self):
        room = self._create(phases=[{"title": "Идея"}, {"title": "Согласование"}])
        self.assertEqual([p["title"] for p in room["phases"]], ["Идея", "Согласование"])
        first_id = room["phases"][0]["id"]

        updated = rooms.update_room(self.cfg, room["id"], {"phases": [
            {"id": first_id, "title": "Замысел"},                       # переименование
            {"title": "Согласование"},                                  # на месте
            {"title": "Запуск"},                                        # добавлен
        ]})
        self.assertEqual([p["title"] for p in updated["phases"]], ["Замысел", "Согласование", "Запуск"])
        self.assertEqual(updated["phases"][0]["id"], first_id, "id не меняется при переименовании")

        reordered = rooms.update_room(self.cfg, room["id"],
                                      {"phases": list(reversed(updated["phases"]))})
        self.assertEqual([p["title"] for p in reordered["phases"]], ["Запуск", "Согласование", "Замысел"])

        emptied = rooms.update_room(self.cfg, room["id"], {"phases": []})
        self.assertEqual(emptied["phases"], [], "этапы можно убрать все")

    def test_bad_phases_are_rejected(self):
        room = self._create()
        with self.assertRaises(rooms.RoomsError) as ctx:
            rooms.update_room(self.cfg, room["id"], {"phases": "не список"})
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertIn("список этапов", ctx.exception.message)


class MigrationTest(unittest.TestCase):
    """Миграция чтением: этапы берутся из карты самой комнаты, данные не пишутся."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-migrate-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
            "TEAMROOM_SEED": "1",
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        rooms.seed_defaults(self.cfg)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _write_roadmap(self, room_id: str, phases: list[dict], current=None):
        path = Path(self.cfg.state_dir) / room_id / "roadmap.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"room": room_id, "current": current,
                                    "phases": phases, "updated_at": 1793171200},
                                   ensure_ascii=False), encoding="utf-8")
        return path

    def test_room_with_own_map_keeps_its_stages(self):
        # Так выглядит комната, которая уже вела карту (данные из её файла).
        path = self._write_roadmap("cafe", [
            {"id": "one", "title": "Первый этап", "status": "done", "note": "сделано"},
            {"id": "two", "title": "Второй этап", "status": "active", "note": "идёт"},
            {"id": "three", "title": "Третий этап", "status": "next", "note": ""},
        ], current={"id": "two"})
        before = path.read_bytes()

        got = phase_book.migrate(self.cfg, "cafe")
        self.assertEqual([p["id"] for p in got], ["one", "two", "three"])
        self.assertEqual(got[1]["title"], "Второй этап")
        self.assertEqual(path.read_bytes(), before, "файл комнаты не переписывается")

    def test_empty_placeholder_gives_nothing(self):
        # Пустая заготовка (шесть строк «впереди») — не данные: этапов нет.
        self._write_roadmap("fn", [{"id": "a", "title": "Этап А", "status": "next", "note": ""},
                                   {"id": "b", "title": "Этап Б", "status": "next", "note": ""}])
        self.assertEqual(phase_book.migrate(self.cfg, "fn"), [])
        self.assertEqual(ingest.roadmap_view(self.cfg, "fn")["phases"], [])

    def test_explicit_field_wins_over_migration(self):
        self._write_roadmap("cafe", [{"id": "one", "title": "Первый", "status": "done", "note": ""}])
        rooms.update_room(self.cfg, "cafe", {"phases": [{"id": "mine", "title": "Мой этап"}]})
        room = rooms.get_room(self.cfg, "cafe")
        self.assertEqual([p["id"] for p in phase_book.phases_of(self.cfg, room)], ["mine"])

    def test_current_of_uses_room_map(self):
        self._write_roadmap("cafe", [
            {"id": "one", "title": "Первый", "status": "done"},
            {"id": "two", "title": "Второй", "status": "blocked"},
        ], current={"id": "two"})
        phases = phase_book.migrate(self.cfg, "cafe")   # этапы этой же карты
        self.assertEqual(phase_book.current_of(self.cfg, "cafe", phases),
                         {"id": "two", "title": "Второй", "status": "blocked"})


class PortabilityTest(unittest.TestCase):
    """В коде нет названий этапов какого-либо проекта (продукт для любых людей)."""

    #: Названия этапов демо-проекта. Живут только в данных: фикстуры, демо-сид Лето.
    PROJECT_TITLES = ("Идея и стратегия", "Легализация", "Бренд и товарный знак", "Упаковка",
                      "Производство", "Продажи")
    #: Файлы, где этих названий быть не должно вовсе.
    CLEAN_FILES = (
        "server/read_view.py", "server/phase_book.py", "server/normalize.py",
        "server/normalize_base.py", "server/normalize_stage.py", "server/ingest_common.py",
        "server/ingest_extract.py", "server/ingest_merge.py", "server/ingest.py",
        "server/vault_mirror.py", "server/engine_read.py", "server/app.py",
        "web/js/views/roadmap.js", "web/js/views/plan-streams.js", "web/js/views/plan-req.js",
        "web/js/views/room-dialog.js", "web/js/views/team-roadmap-model.js",
    )

    def test_no_project_stage_titles_in_code(self):
        for name in self.CLEAN_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            for title in self.PROJECT_TITLES:
                self.assertNotIn(title, text, "%s: в коде не должно быть «%s»" % (name, title))

    def test_demo_seed_is_the_only_place(self):
        seed = (ROOT / "server" / "rooms.py").read_text(encoding="utf-8")
        self.assertIn("phases", seed)
        # Демо-комната Лето задаёт свои этапы как данные — это разрешено.
        for title in self.PROJECT_TITLES:
            self.assertIn(title, seed)


if __name__ == "__main__":
    unittest.main()
