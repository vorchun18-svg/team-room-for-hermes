"""Артефакты карточки запроса: папка проекта комнаты и контроль места сохранения.

WAVE19, пакет A2 (B-22, B-27):

  * артефакт внутри `project_dir` комнаты виден и открывается («Открыть / Копировать
    путь / В проводнике»), хотя его путь не входит в общие `allowed_roots`;
  * выход за пределы (`..`, симлинк), запрещённые имена/типы и системные каталоги
    по-прежнему отклоняются;
  * файл вне папки проекта и вне зеркала показывается с предупреждением
    «Файл сохранён вне папки проекта» и одним событием журнала комнаты;
  * отсутствующий файл говорит «Файл удалён или перемещён», а не «недоступен».

Все данные — во временных каталогах: реальные комнаты, канбан и Vault не трогаются.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                       # noqa: E402
from server import agent_notes, config, contract, context_pack, files, read_view, rooms  # noqa: E402
from server.http_util import ApiError              # noqa: E402

#: Тот же набор, что в документации: путь с «—» и пробелами (боевой случай B-22).
FOLDER_NAME = "2026-11-06 Письмо Лебедев — ответ"
FILE_NAME = "Письмо Лебедев — ответ.docx"


class ArtifactCase(unittest.TestCase):
    """Песочница: свои данные, зеркало и папка проекта во временном каталоге."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teamroom-artifacts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved_env: dict = {}
        env = {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "kanban"),
            "HERMES_KANBAN_HOME": str(self.tmp / "kanban"),
        }
        for key, value in env.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.addCleanup(self._restore_env)
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self.project = self.tmp / "Agents" / "default"
        self.project.mkdir(parents=True, exist_ok=True)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # --- помощники ---------------------------------------------------------
    def make_room(self, **extra) -> dict:
        """Настоящая комната в реестре песочницы (журнал пишется туда же)."""
        data = {"id": "fn", "title": "Рабочее пространство", "orchestrator": "default",
                "members": [{"profile": "lawyer"}], "project_dir": str(self.project)}
        data.update(extra)
        return rooms.create_room(self.cfg, data)

    def make_file(self, folder: Path, name: str = FILE_NAME, text: str = "текст отчёта") -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def artifact_states(self, room, paths) -> tuple:
        warnings: list = []
        items = [{"path": str(path), "title": Path(path).name} for path in paths]
        arts = read_view.artifacts(self.cfg, {"artifacts": items}, warnings, room)
        return arts, warnings


class ProjectDirRootsTest(ArtifactCase):
    """B-22: папка проекта ЭТОЙ комнаты — в белом списке, остальное — как раньше."""

    def test_project_dir_file_is_allowed_only_with_room(self):
        path = self.make_file(self.project / FOLDER_NAME)
        self.assertIsNone(config.path_allowed(self.cfg, str(path)),
                          "без комнаты файл вне общих корней — как раньше")
        room = self.make_room()
        roots = config.room_roots(self.cfg, room)
        self.assertIsNotNone(config.path_allowed(self.cfg, str(path), roots=roots))
        self.assertIn(self.project.resolve(), [Path(root) for root in roots])

    def test_artifacts_ok_inside_project_dir(self):
        path = self.make_file(self.project / FOLDER_NAME)
        room = self.make_room()
        arts, warnings = self.artifact_states(room, [path])
        self.assertEqual(len(arts), 1)
        item = arts[0]
        self.assertEqual(item["state"], "ok")
        self.assertTrue(item["exists"])
        self.assertFalse(item["outside_project"])
        self.assertIsInstance(item["size"], int)
        self.assertEqual(item["title"], FILE_NAME)
        self.assertEqual([w["code"] for w in warnings], [])

    def test_artifacts_ok_inside_room_mirror(self):
        mirror_file = self.make_file(self.cfg.vault_dir / "fn" / "Материалы", "свод.md")
        room = self.make_room()
        arts, warnings = self.artifact_states(room, [mirror_file])
        self.assertEqual(arts[0]["state"], "ok")
        self.assertTrue(arts[0]["exists"])
        self.assertEqual(warnings, [])

    def test_dotdot_out_of_project_dir_is_refused(self):
        secret = self.make_file(self.tmp / "чужая папка", "секрет.md")
        room = self.make_room()
        roots = config.room_roots(self.cfg, room)
        escape = str(self.project / ".." / "чужая папка" / "секрет.md")
        self.assertTrue(Path(secret).is_file())
        self.assertIsNone(config.path_allowed(self.cfg, escape, roots=roots))

    def test_symlink_outside_project_dir_is_refused(self):
        target = self.make_file(self.tmp / "снаружи", "секрет.md")
        link = self.project / "ссылка.md"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest("симлинки недоступны: %s" % exc)
        room = self.make_room()
        roots = config.room_roots(self.cfg, room)
        self.assertIsNone(config.path_allowed(self.cfg, str(link), roots=roots))

    def test_project_dir_in_forbidden_place_is_not_added(self):
        # системный каталог и служебный каталог Hermes не становятся корнями комнаты
        candidates = [r"C:\Windows\System32", str(self.cfg.hermes_home / "workspace")]
        for bad in candidates:
            room = {"id": "fn", "project_dir": bad}
            self.assertIsNone(config.project_root_of(self.cfg, room), bad)
            try:
                resolved = Path(bad).resolve()
            except OSError:
                continue
            self.assertNotIn(resolved, [Path(root) for root in config.room_roots(self.cfg, room)], bad)

    def test_app_data_dir_is_not_a_room_project_dir(self):
        # папка данных приложения — запрещена для папки проекта, но это общий корень
        # приложения: файлы оттуда по-прежнему читаются как раньше
        bad_dir = self.cfg.data_dir / "проект"
        bad_file = self.make_file(bad_dir, "отчёт.md")
        fake_room = {"id": "fn", "title": "FN", "project_dir": str(bad_dir)}
        self.assertIsNone(config.project_root_of(self.cfg, fake_room))
        self.assertNotIn(bad_dir.resolve(), [Path(root) for root in config.room_roots(self.cfg, fake_room)])
        arts, warnings = self.artifact_states(fake_room, [bad_file])
        self.assertEqual(arts[0]["state"], "ok")
        self.assertEqual(warnings, [])

    def test_denied_name_inside_project_dir(self):
        secret = self.make_file(self.project, "auth.json", "{}")
        room = self.make_room()
        arts, warnings = self.artifact_states(room, [secret])
        self.assertEqual(arts[0]["state"], "denied")
        self.assertFalse(arts[0]["exists"])
        self.assertIn("artifact_denied", [w["code"] for w in warnings])


class ArtifactPlaceTest(ArtifactCase):
    """B-27: файл вне папки проекта и вне зеркала — предупреждение и запись в журнал."""

    def test_outside_and_missing_states(self):
        room = self.make_room()
        outside = self.make_file(self.tmp / "рабочая папка агента", "черновик.md")
        missing = self.project / "удалённый файл.md"
        arts, warnings = self.artifact_states(room, [outside, missing])
        states = [item["state"] for item in arts]
        self.assertEqual(states, ["outside", "missing"])
        # файл вне папки проекта существует и открывается — но с предупреждением
        self.assertTrue(arts[0]["exists"])
        self.assertTrue(arts[0]["outside_project"])
        self.assertIsInstance(arts[0]["size"], int)
        self.assertIn("вне папки проекта", arts[0]["reason"])
        # отсутствующий файл — отдельный понятный текст, а не «недоступен»
        self.assertFalse(arts[1]["exists"])
        self.assertIsNone(arts[1]["size"])
        self.assertEqual(arts[1]["reason"], "Файл удалён или перемещён")
        codes = [w["code"] for w in warnings]
        self.assertEqual(codes, ["artifact_outside", "missing_artifact"])
        self.assertIn("Файл сохранён вне папки проекта", warnings[0]["message"])
        self.assertIn(outside.name, warnings[0]["message"])
        self.assertEqual(warnings[1]["message"], "Файл удалён или перемещён: удалённый файл.md")

    def test_journal_record_is_written_once(self):
        room = self.make_room()
        outside = self.make_file(self.tmp / "рабочая папка агента", "черновик.md")
        self.artifact_states(room, [outside])
        self.artifact_states(room, [outside])       # повторный опрос страницы
        events = [e for e in agent_notes.journal(self.cfg, "fn") if e["action"] == "artifact_outside"]
        self.assertEqual(len(events), 1, "одна запись на файл, а не на каждый опрос")
        event = events[0]
        self.assertEqual(event["action_ru"], "файл вне папки проекта")
        self.assertEqual(event["path"], str(outside).replace("\\", "/"))
        self.assertIn("Сохранён вне папки проекта", event["note"])
        self.assertEqual(event["source_ru"], "автоматически")

    def test_no_journal_when_everything_in_place(self):
        room = self.make_room()
        inside = self.make_file(self.project / FOLDER_NAME)
        self.artifact_states(room, [inside])
        events = [e for e in agent_notes.journal(self.cfg, "fn") if e["action"] == "artifact_outside"]
        self.assertEqual(events, [])

    def test_room_without_project_dir_does_not_spam_warnings(self):
        # нет папки проекта — но есть зеркало: файл в зеркале считается «на месте»
        room = self.make_room(project_dir="")
        mirror_file = self.make_file(self.cfg.vault_dir / "fn" / "Материалы", "свод.md")
        arts, warnings = self.artifact_states(room, [mirror_file])
        self.assertEqual(arts[0]["state"], "ok")
        self.assertEqual(warnings, [])


class FilesModuleTest(ArtifactCase):
    """Отдача и «в проводнике» с корнями комнаты и внешними файлами."""

    def test_open_file_with_room_roots(self):
        path = self.make_file(self.project / FOLDER_NAME, text="ответ")
        room = self.make_room()
        roots = config.room_roots(self.cfg, room)
        status, headers, body = files.open_file(self.cfg, str(path), roots=roots)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, Path)
        # без корней комнаты — по-прежнему 403 (совместимость и безопасность)
        self.assertEqual(files.open_file(self.cfg, str(path))[0], 403)

    def test_external_file_only_with_flag(self):
        outside = self.make_file(self.tmp / "рабочая папка агента", "черновик.md")
        self.assertEqual(files.open_file(self.cfg, str(outside))[0], 403)
        status, _headers, _body = files.open_file(self.cfg, str(outside), allow_external=True)
        self.assertEqual(status, 200)
        self.assertEqual(files.reveal(self.cfg, str(outside)), False)

    def test_forbidden_places_stay_forbidden(self):
        # папка данных приложения — внутренний каталог: если её нет в корнях комнаты,
        # «внешняя» лазейка её не открывает
        narrow = dataclasses.replace(self.cfg, allowed_roots=[Path(self.cfg.vault_dir)])
        hidden = self.make_file(self.cfg.data_dir, "заметка.md")
        self.assertEqual(files.open_file(narrow, str(hidden), allow_external=True)[0], 403)
        # исполняемые файлы не отдаём никогда
        exe = self.make_file(self.project / FOLDER_NAME, "собрать.exe", "MZ")
        room = self.make_room()
        roots = config.room_roots(self.cfg, room)
        self.assertEqual(files.open_file(self.cfg, str(exe), roots=roots, allow_external=True)[0], 403)

    def test_missing_file_message(self):
        missing = self.project / "нет такого.md"
        status, _headers, body = files.open_file(self.cfg, str(missing), allow_external=True)
        self.assertEqual(status, 404)
        self.assertIn("Файл удалён или перемещён", bytes(body).decode("utf-8"))

    def test_reveal_uses_explorer_for_project_dir(self):
        path = self.make_file(self.project / FOLDER_NAME)
        room = self.make_room()
        roots = config.room_roots(self.cfg, room)
        calls: list = []

        def fake_popen(args, **kwargs):       # noqa: ANN001 - подмена запуска процесса
            calls.append((args, kwargs))

        with mock.patch.object(files.sys, "platform", "win32"), \
                mock.patch.object(files.proc, "popen", fake_popen):
            self.assertTrue(files.reveal(self.cfg, str(path), roots=roots))
        self.assertTrue(calls, "проводник должен получить путь файла")
        self.assertIn("/select,", calls[0][0][1])


class ArtifactApiTest(ArtifactCase):
    """HTTP-уровень: страница открывает файл с `?room=…` и новым действием."""

    def setUp(self):
        super().setUp()
        self.room = self.make_room()
        self.app = app_mod.App(self.cfg, port=0)

    def test_file_endpoint_serves_project_dir_with_room(self):
        path = self.make_file(self.project / FOLDER_NAME)
        status, payload, _headers = self.app.h_file({"path": [str(path)], "room": ["fn"]}, {})
        self.assertEqual(status, 200)
        self.assertTrue(bytes(payload.body).startswith(b"\xd1\x82"))     # «текст…» в UTF-8
        with self.assertRaises(ApiError) as caught:
            self.app.h_file({"path": [str(path)]}, {})
        self.assertEqual(caught.exception.status, 403)

    def test_file_endpoint_missing_text(self):
        missing = self.project / "удалён.md"
        with self.assertRaises(ApiError) as caught:
            self.app.h_file({"path": [str(missing)], "room": ["fn"]}, {})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.message, "Файл удалён или перемещён")

    def test_artifact_open_action(self):
        path = self.make_file(self.project / FOLDER_NAME)
        calls: list = []

        def fake_popen(args, **kwargs):       # noqa: ANN001 - подмена запуска процесса
            calls.append((args, kwargs))

        with mock.patch.object(files.sys, "platform", "win32"), \
                mock.patch.object(files.proc, "popen", fake_popen):
            status, payload, _headers = self.app.h_artifact_open(
                {}, {"path": str(path), "room": "fn"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["revealed"])
        self.assertTrue(calls)

    def test_artifact_open_validation(self):
        with self.assertRaises(ApiError) as no_path:
            self.app.h_artifact_open({}, {"room": "fn"})
        self.assertEqual(no_path.exception.status, 400)
        with self.assertRaises(ApiError) as no_room:
            self.app.h_artifact_open({}, {"path": str(self.project / "x.md")})
        self.assertEqual(no_room.exception.status, 400)
        with self.assertRaises(ApiError) as no_such_room:
            self.app.h_artifact_open({}, {"path": str(self.project / "x.md"), "room": "nope"})
        self.assertEqual(no_such_room.exception.status, 404)


class FilesRuleTextTest(ArtifactCase):
    """B-27/B-34: правило мест сохранения — в контракте и в блоке контекста."""

    def test_contract_has_places_rule(self):
        room = self.make_room()
        text = contract.build_contract_block(room, "r-test", self.cfg)
        self.assertIn(contract.FILES_RULE_OPEN, text)
        self.assertIn(str(self.project), text)
        self.assertIn("Файл сохранён вне папки проекта", text)
        self.assertIn("с прямого разрешения Михаила", text)

    def test_context_block_rule_mentions_mirror_and_warning(self):
        room = self.make_room()
        rule = context_pack.files_rule(self.cfg, room, "Письмо Лебедев")
        self.assertIn("Правило мест сохранения файлов", rule)
        self.assertIn(str(self.project), rule)
        self.assertIn(str(self.cfg.vault_dir), rule)

    def test_rule_without_project_dir(self):
        rule = context_pack.files_rule(self.cfg, {"id": "fn", "project_dir": ""}, "запрос")
        self.assertIn("Папка проекта не задана", rule)
        self.assertIn("Правило мест сохранения файлов", rule)


if __name__ == "__main__":
    unittest.main()
