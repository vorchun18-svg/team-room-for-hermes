# -*- coding: utf-8 -*-
"""W20-MODELS: модель участника по умолчанию — из настроек профиля Hermes (§1.5).

Что проверяется (всё на ВЫМЫШЛЕННЫХ конфигах во временной папке, реальный Hermes
только читается и никогда не изменяется):

  * ``default_info``: профиль со своей моделью → ``source: "profile"``; профиль без
    модели → ОБЩАЯ настройка Hermes ЦЕЛОЙ парой и ``source: "global"`` с пометкой;
    ничего нигде → ``source: "none"``; пара из разных источников не смешивается;
  * ``agent_default`` сохраняет прежнюю форму ровно из трёх полей (старые клиенты);
  * ``model_settings``/``effective`` честно говорят, откуда взято «по умолчанию»,
    а замена комнаты всегда важнее;
  * ``GET /api/models`` отдаёт ``agent_defaults`` с ``source`` по каждому профилю
    (и добавляет участников комнаты из ``?room=``, даже если профиля нет на диске);
  * при СОЗДАНИИ комнаты переопределений нет: пусто — пусто, «эхо» настроек агента
    не сохраняется, а явный другой выбор пользователя сохраняется ровно как выбран;
  * ``tools/reset_room_models.py``: по умолчанию только показывает (файл не меняется),
    с ``--apply`` делает копию ``rooms.json`` и убирает ``agent_models`` комнаты,
    другие комнаты не трогает; нет комнаты — код 2.

    python -m unittest tests.test_agent_models_defaults -v
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import logging
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_models as models_mod          # noqa: E402
import server.rooms as rooms_mod                  # noqa: E402
import server.routes_models as routes_models      # noqa: E402
import tools.reset_room_models as reset_mod       # noqa: E402

#: вымышленный каталог: только настроенные провайдеры (как отдаёт Hermes)
CATALOG = {
    "providers": [
        {"id": "oblako-a", "label": "Облако-А", "configured": True,
         "models": [{"id": "dilizhans-pro"}, {"id": "dilizhans-midi"}]},
        {"id": "lokal", "label": "Локальный сервер", "configured": True,
         "models": [{"id": "dilizhans-home"}, {"id": "dilizhans-kompakt"}]},
    ]
}

GLOBAL_CONFIG = """\
model:
  provider: oblako-a
  default: dilizhans-pro
agent:
  max_turns: 150
  reasoning_effort: high
"""

PROFILE_FULL = """\
model:
  provider: lokal
  default: dilizhans-home
  aliases:
    dom: dilizhans-home
agent:
  reasoning_effort: medium
"""

PROFILE_NO_MODEL = """\
agent:
  max_turns: 80
  reasoning_effort: low
"""


class _Base(unittest.TestCase):
    """Временный «Hermes» и временные данные: ни одного касания настоящих папок."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="teamroom-models-default-")
        self.root = Path(self._tmp.name)
        self.home = self.root / "hermes"
        (self.home / "profiles").mkdir(parents=True)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        (self.home / "config.yaml").write_text(GLOBAL_CONFIG, "utf-8")
        self.write_profile("team-ops", PROFILE_FULL)
        self.write_profile("team-legacy", PROFILE_NO_MODEL)
        self.cfg = types.SimpleNamespace(
            hermes_home=str(self.home), app_dir=str(ROOT), data_dir=str(self.data_dir),
            board="team", mock=False, demo=False,
            model_catalog_override=CATALOG)

    def tearDown(self):
        self._tmp.cleanup()

    def write_profile(self, name: str, text: str) -> None:
        folder = self.home / "profiles" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.yaml").write_text(text, "utf-8")


# ---------------------------------------------------------------- «по умолчанию»

class DefaultInfoTests(_Base):
    """Источник «по умолчанию»: профиль → общие настройки → ничего."""

    def test_own_profile_settings_are_profile_source(self):
        info = models_mod.default_info(self.cfg, "team-ops")
        self.assertEqual((info["provider"], info["model"], info["reasoning"]),
                         ("lokal", "dilizhans-home", "medium"))
        self.assertEqual(info["source"], "profile")
        self.assertEqual(info["note"], "", "свои настройки — без пометок")

    def test_profile_without_model_uses_global_settings(self):
        info = models_mod.default_info(self.cfg, "team-legacy")
        self.assertEqual((info["provider"], info["model"]), ("oblako-a", "dilizhans-pro"),
                         "модель не задана в профиле — берём общую настройку Hermes")
        self.assertEqual(info["reasoning"], "high", "мышление — из той же общей настройки")
        self.assertEqual(info["source"], "global")
        self.assertIn("общ", info["note"].lower(), "пометка «из общих настроек» обязательна")

    def test_nothing_anywhere_is_none(self):
        (self.home / "config.yaml").unlink()
        info = models_mod.default_info(self.cfg, "team-legacy")
        self.assertNotEqual(info["source"], "profile")
        self.assertEqual(info["source"], "none")
        self.assertFalse(info["provider"] or info["model"], "ничего не выдумываем")
        self.assertTrue(info["note"])

    def test_default_profile_is_its_own_config(self):
        info = models_mod.default_info(self.cfg, "default")
        self.assertEqual((info["source"], info["provider"], info["model"]),
                         ("profile", "oblako-a", "dilizhans-pro"))
        (self.home / "config.yaml").write_text(PROFILE_NO_MODEL, "utf-8")
        self.assertEqual(models_mod.default_info(self.cfg, "default")["source"], "none",
                         "у общего конфига нет «общего над собой»")

    def test_partial_pair_is_not_mixed_between_sources(self):
        """Провайдер профиля + модель общих настроек — запрещённая смесь."""
        self.write_profile("team-half", "model:\n  provider: lokal\n")
        info = models_mod.default_info(self.cfg, "team-half")
        self.assertEqual((info["provider"], info["model"]), ("oblako-a", "dilizhans-pro"),
                         "пара берётся из ОДНОГО источника целиком")
        self.assertEqual(info["source"], "global")

    def test_agent_default_shape_is_still_three_fields(self):
        for name in ("team-ops", "team-legacy", "default", "нет-такого-профиля"):
            with self.subTest(profile=name):
                value = models_mod.agent_default(self.cfg, name)
                self.assertEqual(sorted(value), ["model", "provider", "reasoning"])

    def test_model_settings_reports_where_default_came_from(self):
        room = {"id": "cafe", "orchestrator": "team-ops", "critic": "",
                "members": [{"profile": "team-legacy", "role": "worker"}]}
        own = models_mod.model_settings(self.cfg, room, "team-ops")
        self.assertEqual((own["source"], own["default_source"]), ("agent", "profile"))
        shared = models_mod.model_settings(self.cfg, room, "team-legacy")
        self.assertEqual(shared["source"], "agent")
        self.assertEqual(shared["default_source"], "global")
        self.assertEqual(shared["model"], "dilizhans-pro",
                         "карточка пойдёт на общей настройке Hermes, а не на пустой модели")
        self.assertIn("общ", shared["default_note"].lower())

    def test_room_choice_wins_over_default(self):
        room = {"id": "cafe", "orchestrator": "team-legacy", "critic": "",
                "members": [{"profile": "team-ops", "role": "worker"}],
                "agent_models": {"team-legacy": {"provider": "lokal", "model": "dilizhans-kompakt",
                                                "reasoning": "high", "updated_at": 1}}}
        value = models_mod.model_settings(self.cfg, room, "team-legacy")
        self.assertEqual((value["provider"], value["model"], value["source"]),
                         ("lokal", "dilizhans-kompakt", "room"))
        self.assertEqual(value["default_source"], "global",
                         "источник «по умолчанию» виден даже при выборе комнаты")

    def test_real_profiles_are_only_read(self):
        """Настоящие конфиги Hermes читаются, но их байты не меняются."""
        real = self.root / "real-hermes"
        (real / "profiles" / "anna").mkdir(parents=True)
        (real / "config.yaml").write_text(GLOBAL_CONFIG, "utf-8")
        (real / "profiles" / "anna" / "config.yaml").write_text(PROFILE_FULL, "utf-8")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in real.rglob("config.yaml")}
        cfg = types.SimpleNamespace(hermes_home=str(real), app_dir=str(ROOT))
        models_mod.default_info(cfg, "anna")
        models_mod.default_info(cfg, "нет-такого")
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in real.rglob("config.yaml")}
        self.assertEqual(before, after)


# ---------------------------------------------------------------- GET /api/models

class _StubApp(routes_models.ModelRoutes):
    """Обработчики модели без всего сервера: только то, что им нужно."""

    def __init__(self, cfg, rooms=None):
        self.cfg = cfg
        self.log = logging.getLogger("test.models")
        self.board = "team"
        self.rooms = {str(r.get("id")): r for r in (rooms or [])}

    def mod(self, name):
        return importlib.import_module("server." + name)

    class _Engine:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    def engine(self):
        return self._Engine()

    def _room(self, room_id):
        return self.rooms.get(str(room_id)) or {}


class ModelsEndpointTests(_Base):
    """``GET /api/models`` → ``agent_defaults`` с источником по каждому профилю."""

    def test_agent_defaults_map_has_source_for_every_profile(self):
        app = _StubApp(self.cfg)
        status, data, headers = app.h_models_get({}, None)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        defaults = data.get("agent_defaults")
        self.assertIsInstance(defaults, dict, "agent_defaults обязателен (W20 §1.5)")
        for name in ("default", "team-ops", "team-legacy"):
            self.assertIn(name, defaults, "профиль %s пропал из ответа" % name)
            item = defaults[name]
            for key in ("provider", "model", "reasoning", "source"):
                self.assertIn(key, item, "%s: нет поля %s" % (name, key))
            self.assertIn(item["source"], ("profile", "global", "none"))
        self.assertEqual(defaults["team-ops"]["source"], "profile")
        self.assertEqual((defaults["team-ops"]["provider"], defaults["team-ops"]["model"]),
                         ("lokal", "dilizhans-home"))
        self.assertEqual(defaults["team-legacy"]["source"], "global")
        self.assertEqual(defaults["team-legacy"]["model"], "dilizhans-pro")
        self.assertIn("общ", defaults["team-legacy"]["note"].lower())

    def test_room_participants_are_included_even_without_config(self):
        room = {"id": "cafe", "orchestrator": "team-ops", "critic": None,
                "members": [{"profile": "anna", "role": "worker"}]}
        app = _StubApp(self.cfg, rooms=[room])
        _status, data, _h = app.h_models_get({"room": ["cafe"]}, None)
        defaults = data["agent_defaults"]
        self.assertIn("anna", defaults, "участник комнаты нужен интерфейсу всегда")
        self.assertEqual(defaults["anna"]["source"], "global",
                         "у профиля без конфига модель берётся из общих настроек")
        self.assertEqual(defaults["team-ops"]["source"], "profile")

    def test_catalog_and_efforts_still_there(self):
        app = _StubApp(self.cfg)
        _status, data, _h = app.h_models_get({}, None)
        self.assertEqual([p["id"] for p in data["providers"]], ["oblako-a", "lokal"])
        self.assertTrue(data["efforts"], "уровни мышления не должны пропасть")


# ---------------------------------------------------------------- создание комнаты

class CreateRoomTests(_Base):
    """При создании комнаты переопределений моделей нет — только явный выбор."""

    def _payload(self, **extra) -> dict:
        payload = {"title": "Лето", "orchestrator": "team-ops",
                   "members": [{"profile": "team-legacy", "role": "worker"}]}
        payload.update(extra)
        return payload

    def test_no_overrides_when_nothing_chosen(self):
        room = rooms_mod.create_room(self.cfg, self._payload())
        self.assertEqual(room.get("agent_models"), {}, "комната создаётся без override'ов")

    def test_echo_of_agent_defaults_is_not_stored(self):
        echo = {"team-ops": {"provider": "lokal", "model": "dilizhans-home",
                            "reasoning": "medium"}}
        room = rooms_mod.create_room(self.cfg, self._payload(agent_models=echo))
        self.assertEqual(room.get("agent_models"), {},
                         "значения из настроек профиля — не выбор пользователя")

    def test_echo_of_global_defaults_is_not_stored(self):
        echo = {"team-legacy": {"provider": "oblako-a", "model": "dilizhans-pro",
                               "reasoning": "high"}}
        room = rooms_mod.create_room(self.cfg, self._payload(agent_models=echo))
        self.assertEqual(room.get("agent_models"), {})

    def test_explicit_choice_is_stored_exactly(self):
        chosen = {"team-legacy": {"provider": "lokal", "model": "dilizhans-kompakt",
                                 "reasoning": "low"}}
        room = rooms_mod.create_room(self.cfg, self._payload(agent_models=chosen))
        stored = room["agent_models"]["team-legacy"]
        self.assertEqual((stored["provider"], stored["model"], stored["reasoning"]),
                         ("lokal", "dilizhans-kompakt", "low"),
                         "выбор пользователя сохраняется ровно как выбран")


# ---------------------------------------------------------------- скрипт сброса

class ResetRoomModelsTests(_Base):
    """``tools/reset_room_models.py``: показ без изменений, ``--apply`` с копией."""

    def setUp(self):
        super().setUp()
        self.rooms_file = self.data_dir / "rooms.json"
        self.original = {
            "rooms": [
                {"id": "cafe", "title": "Лето «Лето»", "board": "team",
                 "orchestrator": "team-ops",
                 "members": [{"profile": "team-legacy", "role": "worker"}],
                 "agent_models": {
                     "team-ops": {"provider": "oblako-a", "model": "dilizhans-pro",
                                 "reasoning": "high", "updated_at": 1700000000},
                     "team-legacy": {"provider": "lokal", "model": "dilizhans-kompakt",
                                    "reasoning": "", "updated_at": 1700000001}}},
                {"id": "sklad", "title": "Склад «Ромашка»", "board": "team",
                 "orchestrator": "team-ops", "members": [{"profile": "team-legacy"}]},
            ]
        }
        self.rooms_file.write_text(json.dumps(self.original, ensure_ascii=False), "utf-8")

    def _read(self) -> dict:
        return json.loads(self.rooms_file.read_text("utf-8"))

    def test_plan_changes_nothing(self):
        before = self.rooms_file.read_bytes()
        result = reset_mod.plan(self.cfg, "cafe")
        self.assertFalse(result["applied"])
        self.assertEqual(result["room"], "cafe")
        self.assertEqual(result["count"], 2, "две записи переопределений")
        self.assertEqual(sorted(row["profile"] for row in result["overrides"]),
                         ["team-legacy", "team-ops"])
        self.assertEqual(self.rooms_file.read_bytes(), before, "показ ничего не пишет")
        self.assertEqual(list(self.data_dir.glob("rooms.json.bak-*")), [],
                         "копия делается только при --apply")

    def test_plan_reports_defaults_from_agent_settings(self):
        result = reset_mod.plan(self.cfg, "cafe")
        rows = {row["profile"]: row for row in result["overrides"]}
        self.assertEqual(rows["team-ops"]["default_source"], "profile")
        self.assertEqual(rows["team-ops"]["default_label"],
                         "Локальный сервер · dilizhans-home · средние")
        self.assertEqual(rows["team-legacy"]["default_source"], "global")
        self.assertIn("общая настройка hermes", reset_mod.render(result).lower())

    def test_apply_removes_room_models_and_keeps_backup(self):
        before = self._read()
        result = reset_mod.apply(self.cfg, "cafe")
        self.assertTrue(result["applied"])
        self.assertEqual(result["count"], 2)
        backup = Path(result["backup"])
        self.assertTrue(backup.exists(), "копия данных обязательна")
        self.assertEqual(json.loads(backup.read_text("utf-8")), before,
                         "копия — ровно прежнее содержимое")
        after = self._read()
        rooms = {room["id"]: room for room in after["rooms"]}
        self.assertNotIn("agent_models", rooms["cafe"], "записи комнаты убраны целиком")
        self.assertEqual(rooms["sklad"], before["rooms"][1], "другая комната не тронута")

    def test_apply_is_idempotent(self):
        reset_mod.apply(self.cfg, "cafe")
        again = reset_mod.apply(self.cfg, "cafe")
        self.assertFalse(again["applied"])
        self.assertEqual(again["count"], 0)

    def test_cli_default_is_dry_run(self):
        with mock.patch.object(reset_mod, "_cfg", lambda data=None: self.cfg):
            out = io.StringIO()
            with redirect_stdout(out):
                code = reset_mod.main(["--room", "cafe"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("ничего не изменено", text)
        self.assertIn("Лето «Лето»", text)
        self.assertIn("--apply", text)
        self.assertIn("agent_models", self._read()["rooms"][0], "файл не изменён")

    def test_cli_apply_writes_backup_and_clears(self):
        with mock.patch.object(reset_mod, "_cfg", lambda data=None: self.cfg):
            out = io.StringIO()
            with redirect_stdout(out):
                code = reset_mod.main(["--room", "cafe", "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("Копия данных", out.getvalue())
        self.assertNotIn("agent_models", self._read()["rooms"][0])
        self.assertEqual(len(list(self.data_dir.glob("rooms.json.bak-*"))), 1)

    def test_unknown_room_is_exit_code_2(self):
        with mock.patch.object(reset_mod, "_cfg", lambda data=None: self.cfg):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = reset_mod.main(["--room", "нет-такой"])
        self.assertEqual(code, 2)
        self.assertIn("не найдена", err.getvalue())
        self.assertIn("agent_models", self._read()["rooms"][0], "при ошибке файл цел")

    def test_missing_registry_is_exit_code_2(self):
        self.rooms_file.unlink()
        with mock.patch.object(reset_mod, "_cfg", lambda data=None: self.cfg):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = reset_mod.main(["--room", "cafe"])
        self.assertEqual(code, 2)
        self.assertIn("не найден", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
