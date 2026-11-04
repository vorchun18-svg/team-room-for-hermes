"""HTTP-тесты папки проекта комнаты (Д2 WAVE8-RULES) на песочнице.

Проверяем ровно то, что видит человек:
  * `POST /api/rooms/check-dir` отвечает вердиктом и НИЧЕГО не создаёт;
  * `POST /api/rooms` и `PATCH /api/rooms/{id}` принимают `project_dir`,
    отдают его в GET и создают папку (только под разрешённым путём);
  * невалидный путь → 400 с русским сообщением;
  * в папке появляется README.md с блоком «Содержание».

Каталоги создаются только внутри песочницы (`tests.helpers.Sandbox` → временный
каталог). Реальная доска только читается (слепок до/после).
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                      # noqa: E402
from server import config, project_dir            # noqa: E402
from tests.helpers import Sandbox, snapshot_real_db  # noqa: E402

B = chr(92)
HERMES_MSG = "Папка внутри служебных каталогов Hermes — выберите другую"


class RoomFolderApi(unittest.TestCase):
    ENV_KEYS = ("TEAMROOM_DATA", "TEAMROOM_VAULT_DIR", "TEAMROOM_HERMES_HOME", "TEAMROOM_NO_SHELL")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="tr-roomfolder-")
        cls.base = Path(cls.tmp.name)
        cls._saved_env = {key: os.environ.get(key) for key in cls.ENV_KEYS}
        os.environ.update({
            "TEAMROOM_DATA": str(cls.base / "data"),
            "TEAMROOM_VAULT_DIR": str(cls.base / "vault" / "rooms"),
            "TEAMROOM_HERMES_HOME": str(cls.base / "hermes"),
            "TEAMROOM_NO_SHELL": "1",
        })
        cls.cfg = config.load()
        cls.cfg.ensure_dirs()
        cls.before_board = snapshot_real_db(cls.cfg)
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
        try:
            assert snapshot_real_db(cls.cfg) == cls.before_board, "реальная kanban.db изменилась"
        finally:
            for key, old in cls._saved_env.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
            cls.tmp.cleanup()

    # --- клиент -------------------------------------------------------------
    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, quote(path, safe="/?=&%:,+"), body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        info = {"status": resp.status, "raw": raw}
        if "json" in (resp.getheader("Content-Type") or "") and raw:
            info["json"] = json.loads(raw.decode("utf-8"))
        conn.close()
        return info

    def room_body(self, title, project_dir_value=""):
        return {"title": title, "icon": "project", "board": "team",
                "orchestrator": "biz-director", "critic": None,
                "members": [{"profile": "biz-ops", "role": "worker"}],
                "context_files": [], "project_dir": project_dir_value,
                "critic_pass": False, "max_rounds": 1}

    # --- check-dir ----------------------------------------------------------
    def test_check_dir_new_folder_and_nothing_created(self):
        target = self.base / "проекты" / "Новый проект"
        res = self.call("POST", "/api/rooms/check-dir", {"path": str(target)})
        self.assertEqual(res["status"], 200)
        data = res["json"]
        self.assertTrue(data["allowed"])
        self.assertFalse(data["exists"])
        self.assertTrue(data["will_create"])
        self.assertTrue(data["writable"])
        self.assertEqual(data["warnings"], [])
        self.assertIsNone(data["reason"])
        self.assertFalse(target.exists(), "check-dir не должен создавать папку")

    def test_check_dir_existing_folder(self):
        target = self.base / "готово"
        target.mkdir()
        data = self.call("POST", "/api/rooms/check-dir", {"path": str(target)})["json"]
        self.assertTrue(data["allowed"])
        self.assertTrue(data["exists"])
        self.assertFalse(data["will_create"])

    def test_check_dir_hermes_forbidden_russian(self):
        data = self.call("POST", "/api/rooms/check-dir",
                         {"path": str(Path(self.cfg.hermes_home) / "projects" / "x")})["json"]
        self.assertFalse(data["allowed"])
        self.assertEqual(data["reason"], HERMES_MSG)

    def test_check_dir_traversal_and_unc(self):
        for path, fragment in ((B + B + "server" + B + "share", "Сетевая папка"),
                               ("C:" + B + "Windows" + B + "System32", "Системный каталог"),
                               (str(self.base / "a" / ".." / ".." / "z"), "«..»")):
            data = self.call("POST", "/api/rooms/check-dir", {"path": path})["json"]
            self.assertFalse(data["allowed"], path)
            self.assertIn(fragment, data["reason"])

    def test_check_dir_bad_type(self):
        res = self.call("POST", "/api/rooms/check-dir", {"path": 42})
        self.assertEqual(res["status"], 400)

    # --- создание комнаты с папкой -----------------------------------------
    def test_create_room_creates_folder_and_readme(self):
        target = self.base / "проекты" / "Комната А"
        res = self.call("POST", "/api/rooms", self.room_body("Комната с папкой", str(target)))
        self.assertEqual(res["status"], 201, res["raw"][:300])
        room = res["json"]
        self.assertEqual(room["project_dir"], str(target))
        self.assertTrue(target.is_dir(), "папка проекта должна создаться при сохранении")
        readme = target / project_dir.INDEX_FILE
        self.assertTrue(readme.is_file())
        text = readme.read_text(encoding="utf-8")
        self.assertIn(project_dir.BEGIN_INDEX, text)
        self.assertIn("## Содержание", text)
        self.assertIn("Комната с папкой", text)

        listed = self.call("GET", "/api/rooms?include_archived=1")["json"]["rooms"]
        row = next(r for r in listed if r["id"] == room["id"])
        self.assertEqual(row["project_dir"], str(target))
        detail = self.call("GET", "/api/rooms/" + room["id"])["json"]
        self.assertEqual(detail["project_dir"], str(target))

    def test_create_room_without_folder(self):
        res = self.call("POST", "/api/rooms", self.room_body("Без папки"))
        self.assertEqual(res["status"], 201)
        self.assertEqual(res["json"]["project_dir"], "")

    def test_create_room_bad_folder_is_400_russian(self):
        for bad, fragment in ((str(Path(self.cfg.hermes_home) / "x"), HERMES_MSG),
                              (str(Path(self.cfg.data_dir) / "x"), "данных приложения"),
                              ("C:" + B + "Windows" + B + "x", "Системный каталог"),
                              (B + B + "server" + B + "share", "Сетевая папка"),
                              ("relative" + B + "x", "полный путь")):
            res = self.call("POST", "/api/rooms", self.room_body("Плохая папка", bad))
            self.assertEqual(res["status"], 400, "%s → %s" % (bad, res["status"]))
            message = ((res["json"].get("error") or {}).get("message")
                       or res["json"].get("message") or "")
            self.assertIn(fragment, message, message)
        # ни одна «плохая» комната не появилась
        listed = self.call("GET", "/api/rooms?include_archived=1")["json"]["rooms"]
        self.assertFalse([r for r in listed if r["title"] == "Плохая папка"])

    # --- правка комнаты -----------------------------------------------------
    def test_patch_sets_project_dir_and_creates_folder(self):
        created = self.call("POST", "/api/rooms", self.room_body("Правка папки"))["json"]
        rid = created["id"]
        target = self.base / "проекты" / "Позже"
        res = self.call("PATCH", "/api/rooms/" + rid, {"project_dir": str(target)})
        self.assertEqual(res["status"], 200, res["raw"][:300])
        self.assertEqual(res["json"]["project_dir"], str(target))
        self.assertTrue(target.is_dir())
        self.assertTrue((target / project_dir.INDEX_FILE).is_file())

        # очистить папку можно пустой строкой
        res = self.call("PATCH", "/api/rooms/" + rid, {"project_dir": ""})
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["json"]["project_dir"], "")

    def test_patch_bad_folder_is_400_and_keeps_old(self):
        target = self.base / "проекты" / "Держим"
        created = self.call("POST", "/api/rooms", self.room_body("Правка с ошибкой", str(target)))["json"]
        rid = created["id"]
        res = self.call("PATCH", "/api/rooms/" + rid, {"project_dir": str(Path(self.cfg.hermes_home))})
        self.assertEqual(res["status"], 400)
        self.assertIn(HERMES_MSG, ((res["json"].get("error") or {}).get("message") or ""))
        fresh = self.call("GET", "/api/rooms/" + rid)["json"]
        self.assertEqual(fresh["project_dir"], str(target), "старый путь должен сохраниться")

    def test_patch_change_keeps_old_folder_and_its_files(self):
        """W10: смена папки ничего не переносит и не удаляет — старые файлы на месте."""
        first = self.base / "проекты" / "Первая"
        second = self.base / "проекты" / "Вторая"
        created = self.call("POST", "/api/rooms", self.room_body("Смена папки", str(first)))["json"]
        rid = created["id"]
        (first / "отчёт.txt").write_text("итог", encoding="utf-8")
        res = self.call("PATCH", "/api/rooms/" + rid, {"project_dir": str(second)})
        self.assertEqual(res["status"], 200, res["raw"][:200])
        self.assertEqual(res["json"]["project_dir"], str(second))
        self.assertTrue(first.is_dir(), "старая папка не удаляется")
        self.assertTrue((first / "отчёт.txt").is_file(), "старые файлы остаются на месте")
        self.assertTrue(second.is_dir(), "новая папка создана")

    # --- открытие папки (W10) ----------------------------------------------
    def test_open_kind_dir_missing_folder_offers_to_create(self):
        target = self.base / "проекты" / "Ещё нет"
        with mock.patch.dict(os.environ, {"TEAMROOM_NO_SHELL": "0"}):
            res = self.call("POST", "/api/open", {"path": str(target), "kind": "dir"})
        self.assertEqual(res["status"], 200, res["raw"][:300])
        self.assertFalse(res["json"]["ok"])
        self.assertTrue(res["json"]["can_create"], res["json"])
        self.assertIn("Папки нет", res["json"]["reason"])
        self.assertFalse(target.exists(), "открытие не создаёт папку само")

    def test_open_kind_dir_forbidden_path_is_400(self):
        with mock.patch.dict(os.environ, {"TEAMROOM_NO_SHELL": "0"}):
            res = self.call("POST", "/api/open", {"path": "C:" + B + "Windows", "kind": "dir"})
        self.assertEqual(res["status"], 400, res["raw"][:200])
        self.assertIn("Системный каталог", ((res["json"].get("error") or {}).get("message") or ""))

    def test_open_kind_dir_mkdir_creates_and_reports_foreground(self):
        target = self.base / "проекты" / "Создать и открыть"
        with mock.patch.dict(os.environ, {"TEAMROOM_NO_SHELL": "0"}), \
                mock.patch.object(project_dir, "_launch_explorer", lambda _p: True), \
                mock.patch.object(project_dir, "_raise_explorer",
                                  lambda name, wait_s=2.5: {"found": True, "foreground": True}):
            res = self.call("POST", "/api/open",
                            {"path": str(target), "kind": "dir", "mkdir": True})
        self.assertEqual(res["status"], 200, res["raw"][:300])
        self.assertTrue(res["json"]["ok"])
        self.assertTrue(res["json"]["foreground"])
        self.assertTrue(target.is_dir(), "папка создана по кнопке «Создать папку и открыть»")

    def test_open_kind_dir_never_launches_when_shell_is_off(self):
        target = self.base / "проекты"
        target.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(project_dir, "_launch_explorer",
                               side_effect=AssertionError("проводник не должен запускаться")):
            res = self.call("POST", "/api/open", {"path": str(target), "kind": "dir"})
        self.assertEqual(res["status"], 400)
        self.assertIn("тестовом режиме", ((res["json"].get("error") or {}).get("message") or ""))


if __name__ == "__main__":
    unittest.main()
