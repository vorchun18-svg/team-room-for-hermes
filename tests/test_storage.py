# -*- coding: utf-8 -*-
"""W29-P8 — хранилище и Obsidian без дублей.

Что проверяем (по пункту P8 волны 29):

* сухой прогон ничего не удаляет и возвращает тот же список, что удалил бы;
* удаление — только внутри перечисленных корней (диктовки, логи, бэкапы,
  временные папки), путь снаружи отклоняется с причиной;
* Vault (Obsidian) и папка Hermes не удаляются никогда — даже явным запросом
  категории;
* реальное удаление — только с подтверждением;
* материалы запросов: крупный/нетекстовый файл — заметка-ссылка с путём,
  размером и хэшем; маленький текстовый — копия, как раньше;
* ссылка «Материалы» (junction) в Vault создаётся и снимается без потери данных;
* маршруты хранилища подключены и не дублируют существующие.

Данные вымышленные: комната «cafe», файлы «Меню.pdf», «заметка.md».
Реальный `%LOCALAPPDATA%\\hermes`, Vault автора и порт 8777 не затрагиваются.

Запуск:
    python -m unittest tests.test_storage -v
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import storage as storage_mod          # noqa: E402
from tests.helpers import Sandbox                  # noqa: E402

MB = 1024 * 1024


def _write(path: Path, size: int = 10, age_hours: float = 0.0) -> Path:
    """Создать файл нужного размера и «возраста» (mtime в прошлом)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_hours:
        stamp = time.time() - age_hours * 3600.0
        os.utime(path, (stamp, stamp))
    return path


class StorageCase(unittest.TestCase):
    """Песочница с «мусором» всех категорий и один живой Vault."""

    def setUp(self) -> None:
        self.sb = Sandbox().__enter__()
        self.addCleanup(self.sb.__exit__, None, None, None)
        self.cfg = self.sb.cfg
        self.data = Path(self.cfg.data_dir)
        self.vault = Path(self.cfg.vault_dir)
        self.vault.mkdir(parents=True, exist_ok=True)
        # Голосовые диктовки: одна старая, одна свежая.
        self.old_voice = _write(storage_mod.voice_root(self.cfg) / "rec-old" / "recording.json",
                                size=2048, age_hours=24 * 10)
        self.new_voice = _write(storage_mod.voice_root(self.cfg) / "rec-new" / "recording.json",
                                size=1024, age_hours=1)
        # Возраст записи — это возраст её папки (по ней и решает уборка stt).
        for folder, hours in ((self.old_voice.parent, 24 * 10), (self.new_voice.parent, 1)):
            stamp = time.time() - hours * 3600.0
            os.utime(folder, (stamp, stamp))
        # Логи: старый и свежий.
        self.old_log = _write(storage_mod.logs_root(self.cfg) / "app.log.1", size=4096,
                              age_hours=24 * 40)
        self.new_log = _write(storage_mod.logs_root(self.cfg) / "app.log", size=512)
        # Бэкапы: 8 копий одного релиза + одна старая версия. Правило — «последние 5
        # + по одному на релиз»: остаются 5 свежих, по копии на каждый релиз, лишние уходят.
        self.backups = []
        for index in range(8):
            self.backups.append(_write(
                storage_mod.backups_root(self.cfg) / ("team-room-1.5.0-%02d.zip" % index),
                size=1000 + index, age_hours=48 - index))
        self.old_release = _write(
            storage_mod.backups_root(self.cfg) / "team-room-1.4.0-01.zip", size=900,
            age_hours=24 * 30)
        # Временные копии приложения: старая уходит, свежая остаётся.
        self.old_tmp = _write(storage_mod.tmp_root(self.cfg) / "export-old.csv", size=700,
                              age_hours=48)
        self.new_tmp = _write(storage_mod.tmp_root(self.cfg) / "export-new.csv", size=700)
        # Vault: заметка и «материал», который трогать нельзя.
        self.vault_note = _write(self.vault / "cafe" / "STATE.md", size=200)
        self.vault_material = _write(self.vault / "cafe" / "Запросы" / "Меню.pdf", size=3 * MB)
        # Чужой файл вне корней (домашний каталог песочницы).
        self.outside = _write(Path(self.sb.root) / "outside" / "чужое.txt", size=64)

    # --- сухой прогон -------------------------------------------------------

    def test_dry_run_deletes_nothing(self):
        result = storage_mod.clean(self.cfg, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["removed"], [])
        self.assertGreater(len(result["would_remove"]), 0, result)
        for path in (self.old_voice, self.old_log, self.old_tmp, self.backups[0]):
            self.assertTrue(path.exists(), "сухой прогон удалил %s" % path)
        self.assertIn("ничего не удалено", result["message"])

    def test_dry_run_and_real_run_see_the_same_list(self):
        dry = storage_mod.clean(self.cfg, dry_run=True)
        real = storage_mod.clean(self.cfg, dry_run=False, confirm=True)
        dry_paths = sorted(item["path"] for item in dry["would_remove"])
        real_paths = sorted(item["path"] for item in real["removed"])
        self.assertEqual(dry_paths, real_paths)

    # --- реальное удаление --------------------------------------------------

    def test_real_clean_removes_candidates_only(self):
        result = storage_mod.clean(self.cfg, dry_run=False, confirm=True)
        self.assertEqual(result["errors"], [], result)
        self.assertFalse(self.old_voice.exists())
        self.assertFalse(self.old_log.exists())
        self.assertFalse(self.old_tmp.exists())
        self.assertFalse(self.backups[0].exists())
        # Свежее — на месте.
        self.assertTrue(self.new_voice.exists())
        self.assertTrue(self.new_log.exists())
        self.assertTrue(self.new_tmp.exists())
        # Бэкапы: пять свежих + по одной копии на релиз, лишние ушли.
        self.assertEqual(sum(1 for item in self.backups if item.exists()), 6)
        self.assertTrue(self.old_release.exists(), "по одной копии на релиз остаётся")

    def test_real_clean_requires_confirmation(self):
        with self.assertRaises(storage_mod.StorageError) as ctx:
            storage_mod.clean(self.cfg, dry_run=False, confirm=False)
        self.assertEqual(ctx.exception.code, "confirm_required")
        self.assertTrue(self.old_log.exists())

    # --- границы корней -----------------------------------------------------

    def test_path_outside_roots_refused(self):
        result = storage_mod.delete_paths(self.cfg, [str(self.outside)], dry_run=False)
        self.assertEqual(result["removed"], [])
        self.assertEqual(len(result["refused"]), 1)
        self.assertIn("вне разрешённых", result["refused"][0]["reason"])
        self.assertTrue(self.outside.exists())

    def test_root_itself_is_not_deleted(self):
        result = storage_mod.delete_paths(self.cfg, [str(storage_mod.logs_root(self.cfg))],
                                          dry_run=False)
        self.assertEqual(result["removed"], [])
        self.assertTrue(storage_mod.logs_root(self.cfg).is_dir())

    def test_vault_is_never_deleted(self):
        result = storage_mod.delete_paths(self.cfg, [str(self.vault_material), str(self.vault_note)],
                                          dry_run=False)
        self.assertEqual(result["removed"], [])
        self.assertEqual(len(result["refused"]), 2)
        self.assertTrue(self.vault_material.exists())
        self.assertTrue(self.vault_note.exists())
        with self.assertRaises(storage_mod.StorageError) as ctx:
            storage_mod.clean(self.cfg, categories=["vault"], dry_run=False, confirm=True)
        self.assertIn("не удаляются", ctx.exception.message)
        self.assertTrue(self.vault_material.exists())

    def test_hermes_cache_is_show_only(self):
        cache = Path(self.cfg.hermes_home) / "cache" / "scratch" / "old.bin"
        _write(cache, size=128, age_hours=24 * 30)
        result = storage_mod.delete_paths(self.cfg, [str(cache)], dry_run=False)
        self.assertEqual(result["removed"], [])
        self.assertTrue(cache.exists())
        rows = {row["id"]: row for row in storage_mod.overview(self.cfg)["categories"]}
        self.assertFalse(rows["hermes_cache"]["cleanable"])
        self.assertTrue(rows["hermes_cache"]["foreign"])
        self.assertGreater(rows["hermes_cache"]["bytes"], 0)

    # --- экран «Хранилище» --------------------------------------------------

    def test_overview_has_all_categories(self):
        data = storage_mod.overview(self.cfg)
        rows = {row["id"]: row for row in data["categories"]}
        for key in ("voice", "logs", "backups", "tmp", "vault", "hermes_cache", "hermes_logs"):
            self.assertIn(key, rows)
        self.assertTrue(rows["logs"]["cleanable"])
        self.assertFalse(rows["vault"]["cleanable"])
        self.assertGreater(rows["logs"]["will_remove"]["count"], 0)
        self.assertFalse(rows["backups"]["will_remove"]["count"] == 0)
        self.assertTrue(data["total_bytes"] > 0)
        # Vault — «можно заменить ссылками»: материал 3 МБ больше порога копии.
        self.assertGreater(rows["vault"]["proposal"]["bytes"], 0)

    def test_policy_saved_and_bounded(self):
        saved = storage_mod.save_policy(self.cfg, {"logs_days": 7})
        self.assertEqual(saved["logs_days"], 7)
        self.assertEqual(storage_mod.load_policy(self.cfg)["logs_days"], 7)
        with self.assertRaises(storage_mod.StorageError):
            storage_mod.save_policy(self.cfg, {"logs_days": 0 - 1})
        with self.assertRaises(storage_mod.StorageError):
            storage_mod.save_policy(self.cfg, {"нет_такой": 1})
        # Настройка меняет и список к удалению: 7 дней — старый лог всё ещё старый.
        self.assertEqual(storage_mod.load_policy(self.cfg)["logs_days"], 7)

    # --- материалы: копия или ссылка ---------------------------------------

    def test_material_policy_copy_small_text(self):
        small = _write(Path(self.sb.root) / "materials" / "заметка.md", size=2048)
        plan = storage_mod.material_plan(self.cfg, small)
        self.assertTrue(plan["copy"])
        self.assertFalse(plan["link"])

    def test_material_policy_link_big_and_binary(self):
        big = _write(Path(self.sb.root) / "materials" / "Меню.pdf", size=3 * MB)
        plan = storage_mod.material_reference(self.cfg, big)
        self.assertFalse(plan["copy"])
        self.assertTrue(plan["link"])
        self.assertEqual(plan["kind"], "PDF-документ")
        self.assertEqual(len(plan["sha256"]), 64)
        lines = storage_mod.material_note_lines(self.cfg, big)
        text = "\n".join(lines)
        self.assertIn("PDF-документ", text)
        self.assertIn("sha256:", text)
        self.assertIn("копии в Vault нет", text)

    # --- ссылка «Материалы» в Vault ---------------------------------------

    def test_materials_junction_create_and_remove_without_data_loss(self):
        target = Path(self.sb.root) / "проект"
        payload = _write(target / "Отчёт.md", size=32)
        room = {"id": "cafe", "title": "Кофейня", "project_dir": str(target)}
        created = storage_mod.create_materials_link(self.cfg, room)
        self.assertTrue(created["created"], created)
        link = Path(created["path"])
        self.assertTrue(link.is_dir())
        self.assertTrue((link / "Отчёт.md").is_file())
        self.assertTrue((link.parent / "Проект.md").is_file())
        state = storage_mod.link_state(self.cfg, room)
        self.assertTrue(state["is_link"])
        removed = storage_mod.remove_materials_link(self.cfg, room)
        self.assertTrue(removed["removed"], removed)
        self.assertFalse(link.exists())
        self.assertTrue(payload.is_file(), "снятие ссылки потеряло данные")
        # Повторное снятие — не ошибка, а «снимать нечего».
        self.assertFalse(storage_mod.remove_materials_link(self.cfg, room)["removed"])

    def test_materials_link_refuses_plain_folder(self):
        folder = self.vault / "cafe" / storage_mod.MATERIALS_DIRNAME
        _write(folder / "человек-принёс.md", size=10)
        room = {"id": "cafe", "project_dir": str(Path(self.sb.root) / "проект")}
        (Path(self.sb.root) / "проект").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(storage_mod.StorageError):
            storage_mod.create_materials_link(self.cfg, room)
        with self.assertRaises(storage_mod.StorageError):
            storage_mod.remove_materials_link(self.cfg, room)
        self.assertTrue((folder / "человек-принёс.md").is_file())

    def test_home_proposal_points_to_vault(self):
        room = {"id": "cafe", "project_dir": str(Path(self.sb.root) / "проект")}
        proposal = storage_mod.home_proposal(self.cfg, room)
        self.assertTrue(proposal["suggest"])
        self.assertFalse(proposal["same"])
        inside = {"id": "cafe", "project_dir": proposal["default"]}
        self.assertTrue(storage_mod.home_proposal(self.cfg, inside)["same"])


class RoutesTestCase(unittest.TestCase):
    """Маршруты хранилища подключены и не дублируют существующие."""

    def test_routes_installed_once(self):
        import server.app as app_mod                               # noqa: WPS433
        pairs = [(method, pattern.pattern) for method, pattern, _handler in app_mod.ROUTES]
        self.assertEqual(len(pairs), len(set(pairs)))
        for method, pattern, handler in __import__("server.routes_storage", fromlist=["ROUTES"]).ROUTES:
            self.assertEqual(pairs.count((method, pattern)), 1, "маршрута нет или он задвоен: %s" % pattern)
            self.assertTrue(hasattr(app_mod.App, handler), "нет обработчика %s" % handler)

    def test_handler_names_and_methods(self):
        from server import routes_storage                           # noqa: WPS433
        self.assertEqual({method for method, _p, _h in routes_storage.ROUTES},
                         {"GET", "POST", "PUT"})
        for method, pattern, _handler in routes_storage.ROUTES:
            self.assertTrue(pattern.startswith("/api/"), pattern)


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
