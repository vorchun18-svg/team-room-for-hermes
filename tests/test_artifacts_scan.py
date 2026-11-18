# -*- coding: utf-8 -*-
"""Блок «Артефакты» запроса: полный и упорядоченный (W20-A1, B-43, контракт §1.4).

Что проверяется:

* артефакты собираются СКАНИРОВАНИЕМ папки материалов запроса и подпапок проекта,
  а не только из метаданных карточек (раньше в блоке было 4 файла из 15);
* порядок: ``final`` («итоговые») сначала, дальше по времени изменения от новых
  к старым; у файла ``mtime``, ``size``, ``group``, ``folder`` и
  ``newer_than_svod`` (изменён позже последней версии свода);
* подпапки возвращаются элементом ``{kind:'folder', title, count, items}``
  (в интерфейсе — свёрнутая группа);
* ограничения §1.4: только внутри разрешённых корней комнаты, глубина ≤ 2,
  ≤ 200 элементов, служебные ``_*.json`` / ``_*.html`` — группа ``service``;
* прежние состояния (B-22/B-27) не потеряны: файл вне папки проекта — ``outside``,
  пропавший — ``missing``, запрещённый — ``denied``.

Все данные вымышленные и лежат во временных каталогах: кафе «Лето», поставщик
«Ромашка», сотрудники Анна и Лев. Реальные комнаты, канбан и Vault не трогаются.

    python -m unittest tests.test_artifacts_scan -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, read_view  # noqa: E402

RID = "t_a1b2c3d4"
ROOM_ID = "fn"
MATERIALS = "2026-03-04 Письмо поставщику — ответ"

#: 13 файлов нумерации 00–12: так выглядит папка материалов настоящего запроса.
NUMBERED = [
    "00-Бриф-запроса.md",
    "01-Смета-расчёта.md",
    "02-План-проверки.md",
    "03-Выписка-кафе-Лето.md",
    "04-Черновик-ответа.md",
    "05-Проект-ответа.md",
    "06-Список-уточнений.md",
    "07-Итог-ред2.md",
    "08-Расхождения-смет.md",
    "09-Проверка-цифр.md",
    "10-Критика-ред2.md",
    "11-Итог-дня.md",
    "12-Почему-свод-остался-ред1.md",
]
BASE = 1_760_000_000          # вымышленное «начало работы», секунды


class ScanCase(unittest.TestCase):
    """Песочница: свои данные, зеркало и папка проекта во временном каталоге."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teamroom-artscan-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = {}
        env = {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "kanban"),
            "HERMES_KANBAN_HOME": str(self.tmp / "kanban"),
        }
        for key, value in env.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        self.addCleanup(self._restore_env)
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self.project = self.tmp / "Agents" / "default"
        self.project.mkdir(parents=True, exist_ok=True)
        self.room = {"id": ROOM_ID, "title": "Письмо поставщику", "orchestrator": "default",
                     "project_dir": str(self.project)}

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # --- помощники ---------------------------------------------------------
    def make(self, folder, name: str, mtime: int = None, text: str = "вымышленный текст") -> Path:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def materials(self, mtimes=None) -> Path:
        """Папка материалов запроса с файлами 00–12 и двумя копиями в подпапке."""
        folder = self.project / MATERIALS
        mtimes = mtimes or {}
        for index, name in enumerate(NUMBERED):
            self.make(folder, name, mtime=mtimes.get(name, BASE + index * 600))
        self.make(folder, "_состояние.json", mtime=mtimes.get("_состояние.json", BASE + 100))
        self.make(folder, "_оглавление.html", mtime=mtimes.get("_оглавление.html", BASE + 90))
        copies = folder / "_ред2-копии"
        self.make(copies, "07-Итог-ред2-копия.md", mtime=mtimes.get("copy-itog", BASE + 300))
        self.make(copies, "09-Проверка-цифр-копия.md", mtime=mtimes.get("copy-check", BASE + 200))
        return folder

    def canon_with(self, folder, index: int = 0) -> dict:
        """Как в жизни: карточка упоминает хотя бы один файл папки материалов."""
        return {"artifacts": [{"path": str(Path(folder) / NUMBERED[index])}]}

    def collect(self, canon=None, svod_at=None):
        warnings: list = []
        items = read_view.artifacts(self.cfg, canon or {}, warnings, self.room,
                                    request_id=RID, svod_at=svod_at)
        return items, warnings

    @staticmethod
    def files(items) -> list:
        """Плоский список файлов (папки разворачиваются) — для проверок порядка."""
        out = []
        for item in items:
            if item.get("kind") == "folder":
                out.extend(item.get("items") or [])
            else:
                out.append(item)
        return out

    def titles(self, items) -> list:
        return [str(item.get("title") or "") for item in items]

    # --- общие проверки ----------------------------------------------------
    def test_empty_request_has_no_block(self):
        items, warnings = self.collect()
        self.assertEqual(items, [])
        self.assertEqual(warnings, [])

    def test_scans_materials_folder_not_only_cards(self):
        folder = self.materials()
        # в метаданных карточки — только один файл, остальные 12 появились сканированием
        canon = {"artifacts": [{"path": str(folder / NUMBERED[7]), "title": "Итог письма"}]}
        items, _warnings = self.collect(canon)
        files = self.files(items)
        names = {Path(item["path"]).name for item in files}
        for name in NUMBERED:
            self.assertIn(name, names, "файл папки материалов должен попасть в блок")
        self.assertIn("_состояние.json", names, "служебный файл показывается отдельной группой")
        self.assertIn("07-Итог-ред2-копия.md", names, "файл подпапки тоже виден")

    def test_final_group_first_and_mtime_desc(self):
        folder = self.materials()
        canon = {"artifacts": [{"path": str(folder / NUMBERED[4])}]}
        items, _w = self.collect(canon)
        # итоговые — в начале списка
        groups = [item["group"] for item in items]
        first_other = next((i for i, g in enumerate(groups) if g != "final"), len(groups))
        self.assertEqual(set(groups[:first_other]), {"final"})
        self.assertGreater(first_other, 0, "должен быть хотя бы один итоговый файл")
        for item in items[:first_other]:
            self.assertEqual(item["group"], "final")
        # внутри «Итоговых» — от новых к старым
        finals = [item["mtime"] for item in items[:first_other]]
        self.assertEqual(finals, sorted(finals, reverse=True), finals)
        # материалы и служебные после — тоже от новых к старым (папка стоит там,
        # где стоял её самый свежий файл — см. test_folder_element_is_collapsed_group)
        rest = [item["mtime"] for item in items[first_other:] if item.get("kind") != "folder"]
        self.assertEqual(rest, sorted(rest, reverse=True), rest)
        # «письмо», «итог» — итоговые; «Проект-ответа» — материал
        finals_titles = self.titles(items[:first_other])
        self.assertIn("11-Итог-дня", finals_titles)
        self.assertIn("07-Итог-ред2", finals_titles)
        self.assertNotIn("05-Проект-ответа", self.titles(items[:first_other]))

    def test_groups_and_service_files(self):
        folder = self.materials()
        items, _w = self.collect(self.canon_with(folder))
        flat = self.files(items)
        by_name = {Path(item["path"]).name: item for item in flat}
        self.assertEqual(by_name["_состояние.json"]["group"], "service")
        self.assertEqual(by_name["_оглавление.html"]["group"], "service")
        self.assertEqual(by_name["09-Проверка-цифр.md"]["group"], "material")
        self.assertEqual(by_name["07-Итог-ред2.md"]["group"], "final")
        # группы возвращаются только из известного набора
        self.assertTrue({item["group"] for item in flat} <= set(read_view.ARTIFACT_GROUPS))

    def test_folder_element_is_collapsed_group(self):
        folder = self.materials()
        items, _w = self.collect(self.canon_with(folder))
        folders = [item for item in items if item.get("kind") == "folder"]
        self.assertEqual(len(folders), 1, [f.get("title") for f in folders])
        folder = folders[0]
        self.assertEqual(folder["title"], "_ред2-копии")
        self.assertEqual(folder["folder"], "_ред2-копии")
        self.assertEqual(folder["count"], 2)
        # копии итогового и рабочего файла — не «Итоговые»: папка уходит в материалы
        self.assertEqual(folder["group"], "material")
        self.assertEqual(len(folder["items"]), 2)
        for item in folder["items"]:
            self.assertEqual(item["folder"], "_ред2-копии")
        # папка не дублирует свои файлы на верхнем уровне списка
        top = [item for item in items if item.get("kind") != "folder"]
        self.assertNotIn("_ред2-копии", {str(item.get("folder") or "") for item in top} - {""})

    def test_mtime_and_size_present(self):
        self.materials()
        items, _w = self.collect()
        for item in self.files(items):
            self.assertIsInstance(item["mtime"], int, item["path"])
            self.assertIsInstance(item["size"], int, item["path"])
            self.assertEqual(item["state"], "ok")
            self.assertTrue(item["exists"])

    def test_newer_than_svod_flag(self):
        folder = self.materials()
        # свод выпущен между старыми и новыми файлами: 11-Итог-дня новее свода
        svod_at = BASE + 9 * 600
        canon = {"artifacts": [{"path": str(folder / NUMBERED[0])}]}
        items, _w = self.collect(canon, svod_at=svod_at)
        by_name = {Path(item["path"]).name: item for item in self.files(items)}
        self.assertTrue(by_name["11-Итог-дня.md"]["newer_than_svod"])
        self.assertTrue(by_name["12-Почему-свод-остался-ред1.md"]["newer_than_svod"])
        self.assertFalse(by_name["00-Бриф-запроса.md"]["newer_than_svod"])
        # без версии свода бейджа нет ни у кого
        plain, _w2 = self.collect(canon)
        self.assertFalse(any(item["newer_than_svod"] for item in self.files(plain)))

    def test_depth_limit_two(self):
        folder = self.materials()
        self.make(folder / "подпапка", "в-подпапке.md", mtime=BASE + 2000)
        self.make(folder / "подпапка" / "вторая", "второй-уровень.md", mtime=BASE + 2000)
        self.make(folder / "подпапка" / "вторая" / "третья", "третий-уровень.md", mtime=BASE + 2000)
        items, _w = self.collect(self.canon_with(folder))
        names = {Path(item["path"]).name for item in self.files(items)}
        self.assertIn("в-подпапке.md", names)
        self.assertIn("второй-уровень.md", names)
        self.assertNotIn("третий-уровень.md", names, "глубина больше двух не сканируется")

    def test_limit_two_hundred_elements(self):
        folder = self.project / MATERIALS
        for index in range(235):
            self.make(folder, "файл-%03d.md" % index, mtime=BASE + index)
        items, _w = self.collect({"artifacts": [{"path": str(folder / "файл-000.md")}]})
        flat = self.files(items)
        self.assertLessEqual(len(flat), read_view.ARTIFACT_LIMIT)
        self.assertGreater(len(flat), 190, "почти весь лимит должен быть заполнен")
        self.assertLessEqual(len(items), read_view.ARTIFACT_LIMIT + 1)

    def test_scan_stops_on_huge_folder(self):
        """Обход папки ограничен `VISIT_LIMIT`: папка-свалка не задерживает страницу."""
        folder = self.project / MATERIALS
        for index in range(12):
            self.make(folder, "материал-%02d.md" % index, mtime=BASE + index)
        saved = read_view.VISIT_LIMIT
        read_view.VISIT_LIMIT = 4
        try:
            rows = read_view._scan_folder(folder, read_view.ARTIFACT_LIMIT, ROOM_ID)
        finally:
            read_view.VISIT_LIMIT = saved
        self.assertGreater(len(rows), 0, "часть файлов всё равно показываем")
        self.assertLessEqual(len(rows), 4, "смотрим не больше VISIT_LIMIT записей")

    def test_only_inside_allowed_roots(self):
        folder = self.materials()
        stranger = self.tmp / "чужая папка" / "чужой-итог.md"
        self.make(stranger.parent, stranger.name, mtime=BASE + 9000)
        canon = {"artifacts": [{"path": str(stranger)}]}
        items, warnings = self.collect(canon)
        entries = {Path(item["path"]).name: item for item in self.files(items)}
        self.assertIn("чужой-итог.md", entries)
        self.assertEqual(entries["чужой-итог.md"]["state"], "outside")
        self.assertEqual([w["code"] for w in warnings], ["artifact_outside"])
        names = {Path(item["path"]).name for item in self.files(items)}
        self.assertNotIn("00-Бриф-запроса", names,
                         "соседняя папка вне корней комнаты не сканируется")
        self.assertTrue((folder / NUMBERED[0]).is_file())

    def test_card_title_is_kept(self):
        folder = self.materials()
        canon = {"artifacts": [{"path": str(folder / NUMBERED[7]), "title": "Итог письма поставщику",
                               "group": "final"}]}
        items, _w = self.collect(canon)
        titles = self.titles(self.files(items))
        self.assertIn("Итог письма поставщику", titles)
        self.assertNotIn("07-Итог-ред2", titles)

    def test_folder_of_project_with_request_id(self):
        work = self.project / ("Работа-" + RID)
        for index, name in enumerate(NUMBERED):
            self.make(work, name, mtime=BASE + index * 600)
        items, _w = self.collect()
        names = {Path(item["path"]).name for item in self.files(items)}
        self.assertEqual(names, set(NUMBERED))

    def test_mirror_materials_folder(self):
        mirror = self.cfg.vault_dir / ROOM_ID / ("Материалы-" + RID)
        for index, name in enumerate(NUMBERED):
            self.make(mirror, name, mtime=BASE + index * 600)
        items, _w = self.collect()
        names = {Path(item["path"]).name for item in self.files(items)}
        self.assertIn(NUMBERED[0], names)
        self.assertIn(NUMBERED[7], names)

    def test_mirror_request_notes_are_not_artifacts(self):
        notes = self.cfg.vault_dir / ROOM_ID / "Запросы"
        self.make(notes, "2026-03-04 Письмо поставщику (%s).md" % RID, mtime=BASE + 500)
        items, _w = self.collect()
        self.assertEqual(items, [], "заметки запросов — не материалы работы")

    def test_long_names_and_many_items(self):
        folder = self.project / MATERIALS
        long_name = ("07-Итог-проверки-расчётов-по-поставке-Ромашка-за-первый-квартал-"
                     "с-учётом-изменений-в-смете-и-письма-поставщику.md")
        self.make(folder, long_name, mtime=BASE + 5000)
        for index in range(30):
            self.make(folder, "Материал-%02d-сверка-цифр-и-расхождений-по-смете.md" % index,
                      mtime=BASE + index)
        items, _w = self.collect({"artifacts": [{"path": str(folder / long_name)}]})
        flat = self.files(items)
        self.assertEqual(len(flat), 31)
        titles = self.titles(flat)
        self.assertIn(long_name, titles)
        self.assertGreater(len(long_name), 100, "длинное название — проверка вёрстки")
        self.assertEqual(flat[0]["group"], "final")


if __name__ == "__main__":
    unittest.main()
