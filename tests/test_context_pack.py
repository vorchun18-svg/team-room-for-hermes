"""Блок «КОНТЕКСТ КОМНАТЫ» в карточке запроса (WAVE 8, Блок D) — состав, обрезка, чтение.

Проверяется:
* состав блока: название/описание комнаты, выжимка проекта (CONTEXT.md или STATE.md),
  пути `context_files`, похожие прошлые запросы, инструкция «Obsidian — источник
  правды», правило папки проекта (Д2) и его отсутствие без `project_dir`;
* обрезка: блок никогда не длиннее 4 000 знаков, инструкция и конец блока выживают;
* секреты: токены/пароли и значения переменных окружения в блок не попадают;
* пустая комната: аккуратный блок без падений и без «None»;
* `similar_requests`: кириллица и падежи (цена/цены/ценами), порядок, лимит, путь
  к заметке в Vault;
* детерминированность: два вызова на одних данных дают одинаковый текст;
* интеграция: `engine_write.create_request` кладёт блок в тело карточки, а
  `RequestDetail.text` (через `contract.visible_text`) остаётся текстом Михаила.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import context_pack as cp  # noqa: E402
from server import contract, engine_read, engine_write, registry, rooms  # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

TEXT = "Посчитай цену набора из 6 моти и сравни с прошлым расчётом"
PROJECT_DIR = r"C:\TeamRoom\Projects\Лето"

STATE_MD = """# Лето — состояние проекта

_Файл генерируется приложением._

## Где мы сейчас

- **Фаза:** Валидация спроса — идёт
- **Что происходит:** собираем предзаказы на набор из 6 моти

## Решения

**Принято**
- Берём поставщика упаковки №2 — почему: дешевле на 12 %

## Ждёт Михаила

- [ ] Согласовать цену набора
"""


class FakeCfg:
    """Мини-конфиг для чистых проверок: только каталоги, всё во временной папке."""

    def __init__(self, root: Path):
        self.app_dir = PROJECT_ROOT
        self.data_dir = root / "data"
        self.vault_dir = root / "vault"
        self.reportkit_dir = PROJECT_ROOT / "reportkit"
        for path in (self.data_dir, self.vault_dir / "cafe"):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"


def _room(**extra) -> dict:
    room = {
        "id": "cafe",
        "title": "Лето",
        "board": "team",
        "tenant": "room:cafe",
        "orchestrator": "biz-director",
        "description": "Производство десертов: моти, наборы, доставка по Заозёрью",
        "members": [{"profile": "biz-cfo", "role": "worker"}],
        "context_files": [r"C:\TeamRoom\Vault\Projects\Team\Decisions.md",
                          r"C:\TeamRoom\Vault\Projects\Лето\BRIEF.md"],
    }
    room.update(extra)
    return room


class BlockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cp-")
        self.root = Path(self._tmp)
        self.cfg = FakeCfg(self.root)
        (self.cfg.vault_dir / "cafe" / "STATE.md").write_text(STATE_MD, encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # --- состав --------------------------------------------------------------
    def test_block_has_room_summary_files_and_rules(self):
        block = cp.build_context_block(self.cfg, _room(project_dir=PROJECT_DIR), TEXT)
        self.assertTrue(block.startswith(cp.CONTEXT_OPEN))
        self.assertTrue(block.rstrip().endswith(cp.CONTEXT_CLOSE))
        self.assertIn("Комната: «Лето» (cafe)", block)
        self.assertIn("Производство десертов", block)
        # выжимка проекта: модуля vault_mirror.context_summary может ещё не быть → STATE.md
        self.assertIn("CONTEXT.md", block)
        self.assertIn("Выжимка проекта", block)
        # файлы контекста комнаты
        self.assertIn("Decisions.md", block)
        self.assertIn("BRIEF.md", block)
        # обязательная инструкция (дословно)
        self.assertIn("Источник правды — Obsidian", block)
        self.assertIn("Недостающие данные ищи в Vault и в архиве запросов комнаты", block)
        self.assertIn("не спрашивай Михаила, пока ответа нет в Vault", block)
        self.assertIn("Пользуйся своими обычными навыками и инструментами Hermes", block)
        # правило папки проекта (Д2)
        self.assertIn(f"Папка проекта: {PROJECT_DIR}", block)
        self.assertIn("и перечисли пути в artifacts", block)
        self.assertIn("Дублируй суть в Obsidian", block)
        self.assertIn("Исключение — только если Михаил явно указал другое место", block)
        # WAVE19 A2 (B-27): зеркало комнаты названо вторым разрешённым местом —
        # и добавлено жёсткое правило мест сохранения
        self.assertIn("Правило мест сохранения файлов", block)
        self.assertIn("в другие места — лишь с прямого разрешения Михаила", block)

    def test_without_project_dir_points_to_vault_materials(self):
        block = cp.build_context_block(self.cfg, _room(project_dir=""), TEXT)
        self.assertIn("Папка проекта не задана", block)
        self.assertIn("итоговые файлы сохраняй в Vault, в папку комнаты Материалы", block)
        self.assertNotIn("artifacts", block)

    def test_summary_falls_back_to_state_md(self):
        """Если `vault_mirror.context_summary` недоступен/падает — берём STATE.md комнаты."""
        from unittest import mock
        from server import vault_mirror
        room = _room(project_dir="")
        with mock.patch.object(vault_mirror, "context_summary", side_effect=RuntimeError("нет")):
            block = cp.build_context_block(self.cfg, room, TEXT)
        self.assertIn("Валидация спроса", block)
        self.assertIn("поставщика упаковки", block)
        self.assertIn("Согласовать цену набора", block)
        self.assertLessEqual(len(block), cp.MAX_BLOCK_CHARS)

    def test_subdir_is_dated_and_safe(self):
        room = _room(project_dir=PROJECT_DIR)
        block = cp.build_context_block(self.cfg, room, "Отчёт: смета на 6 моти <>?*")
        subdir = cp._request_subdir("Отчёт: смета на 6 моти <>?*")
        self.assertIn(subdir, block)
        self.assertRegex(subdir, r"^\d{4}-\d{2}-\d{2} ")
        for bad in '<>:"/\\|?*':
            self.assertNotIn(bad, subdir)

    def test_empty_room_is_quiet(self):
        block = cp.build_context_block(self.cfg, {}, "")
        self.assertIn(cp.CONTEXT_OPEN, block)
        self.assertIn("инструментами Hermes", block)
        self.assertNotIn("None", block)
        self.assertEqual(cp.similar_requests(self.cfg, "no-such-room", "текст"), [])
        self.assertLessEqual(len(block), cp.MAX_BLOCK_CHARS)

    # --- длина и секреты ------------------------------------------------------
    def test_block_is_capped(self):
        (self.cfg.vault_dir / "cafe" / "STATE.md").write_text(
            STATE_MD + "\n## Решения\n" + "- решение номер раз с длинным пояснением\n" * 400,
            encoding="utf-8")
        room = _room(project_dir=PROJECT_DIR,
                     context_files=[r"C:\Vault\file-%02d.md" % i for i in range(20)])
        for i in range(12):
            registry.add_request(self.cfg, "cafe", {
                "root_id": "t_%08d" % i, "title": "Цена набора из 6 моти %d" % i,
                "text": TEXT + " версия %d" % i, "created_at": 1_700_000_000 + i,
                "summary": "Итог по набору моти " * 30,
            })
        block = cp.build_context_block(self.cfg, room, TEXT)
        self.assertLessEqual(len(block), cp.MAX_BLOCK_CHARS)
        self.assertIn("инструментами Hermes", block)     # инструкция выживает
        self.assertIn(cp.CONTEXT_CLOSE, block)

    def test_no_secrets_or_env_values(self):
        import os
        secret = "ENV-SECRET-VALUE-9876"
        os.environ["TEAMROOM_TEST_SECRET"] = secret
        try:
            room = _room(project_dir=PROJECT_DIR,
                         description="Ключ sk-ABCDEFGHIJKLMNOP1234 и password=hunter2hunter2")
            registry.add_request(self.cfg, "cafe", {
                "root_id": "t_11112222", "title": "Доступ к API", "text": "token: abcdef123456",
                "created_at": 1_700_000_000, "summary": "Bearer abcdefghijklmnop123 ключ",
            })
            block = cp.build_context_block(self.cfg, room, TEXT + " " + secret)
            for bad in ("sk-ABCDEFGHIJKLMNOP1234", "hunter2hunter2", "abcdef123456",
                        "abcdefghijklmnop123", secret):
                self.assertNotIn(bad, block)
            self.assertIn("[скрыто]", block)
        finally:
            os.environ.pop("TEAMROOM_TEST_SECRET", None)

    # --- похожие запросы -----------------------------------------------------
    def test_similar_requests_cyrillic_cases_and_order(self):
        registry.add_request(self.cfg, "cafe", {
            "root_id": "t_aaaa1111", "title": "Цена набора из 6 моти",
            "text": "Считали цену набора моти для Заозёрья", "created_at": 1_700_000_100,
        })
        registry.add_request(self.cfg, "cafe", {
            "root_id": "t_bbbb2222", "title": "Калькуляция моти и маржа",
            "text": "Посчитай калькуляцию моти: мука рисовая, маржа 46 %", "created_at": 1_700_000_200,
        })
        registry.add_request(self.cfg, "cafe", {
            "root_id": "t_cccc3333", "title": "Нанять кондитера", "text": "Найм кондитера в Сан-Паулу",
            "created_at": 1_700_000_300,
        })
        # падежи: «ценами», «наборов», «мотями» — те же основы, что «цена набора моти»
        found = cp.similar_requests(self.cfg, "cafe", "с ценами наборов моти и маржой", limit=5)
        ids = [item["root_id"] for item in found]
        self.assertIn("t_aaaa1111", ids)
        self.assertIn("t_bbbb2222", ids)
        self.assertNotIn("t_cccc3333", ids)          # нет общих основ — не похож
        self.assertTrue(ids[0].startswith("t_bbbb"), ids)   # больше совпадений — выше
        self.assertEqual(set(found[0]), {"root_id", "title", "summary_line", "note_path"})
        self.assertLessEqual(len(cp.similar_requests(self.cfg, "cafe", TEXT, limit=1)), 1)

    def test_note_path_and_answer_line(self):
        registry.add_request(self.cfg, "cafe", {
            "root_id": "t_ab12cd34", "title": "Цена набора из 6 моти",
            "text": "Посчитай цену набора моти", "created_at": 1_700_000_000,
        })
        notes = self.cfg.vault_dir / "cafe" / "Запросы"
        notes.mkdir(parents=True, exist_ok=True)
        note = notes / "2026-10-27 Цена набора из 6 моти (ab12cd34).md"
        note.write_text("# Цена набора\n\n## Ответ\nНабор стоит 46 R$ за 6 штук.\n", encoding="utf-8")
        found = cp.similar_requests(self.cfg, "cafe", "цена набора моти", limit=1)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["note_path"], str(note))
        self.assertTrue(found[0]["summary_line"].startswith("Итог: "))
        self.assertIn("46 R$", found[0]["summary_line"])

    def test_deterministic(self):
        registry.add_request(self.cfg, "cafe", {
            "root_id": "t_dddd4444", "title": "Цена набора из 6 моти", "text": TEXT,
            "created_at": 1_700_000_000, "summary": "Набор — 46 R$",
        })
        room = _room(project_dir=PROJECT_DIR)
        first = cp.build_context_block(self.cfg, room, TEXT)
        second = cp.build_context_block(self.cfg, dict(room), TEXT)
        self.assertEqual(first, second)
        self.assertEqual(cp.similar_requests(self.cfg, "cafe", TEXT),
                         cp.similar_requests(self.cfg, "cafe", TEXT))


class InsertIntoBodyTest(unittest.TestCase):
    """Блок не должен ломать чтение: `text` — только то, что написал Михаил."""

    @staticmethod
    def _cfg():
        class Cfg:
            app_dir = PROJECT_ROOT
            data_dir = PROJECT_ROOT / "data" / "_cp_probe"
            vault_dir = PROJECT_ROOT / "data" / "_cp_probe" / "vault"
            reports_dir = PROJECT_ROOT / "data" / "_cp_probe" / "reports"
            reportkit_dir = PROJECT_ROOT / "reportkit"
        return Cfg()

    def test_block_goes_after_text_and_contract_is_cut_on_read(self):
        cfg = self._cfg()
        room = _room(project_dir="")
        body = contract.build_request_body(room, TEXT, "r-slug", cfg)
        block = cp.build_context_block(cfg, room, TEXT)
        new = cp.insert_into_body(body, block)
        self.assertIn(cp.CONTEXT_OPEN, new)
        self.assertIn(contract.CONTRACT_OPEN, new)
        self.assertLess(new.index(TEXT), new.index(cp.CONTEXT_OPEN))
        self.assertLess(new.index(cp.CONTEXT_OPEN), new.index("metadata (все поля необязательны"))
        text = contract.visible_text(new)
        self.assertEqual(text, TEXT)
        self.assertNotIn("КОНТЕКСТ КОМНАТЫ", text)
        self.assertEqual(cp.insert_into_body(new, block), new)     # без дублей

    def test_body_without_contract_still_gets_block(self):
        block = cp.build_context_block(self._cfg(), {"id": "cafe"}, TEXT)
        new = cp.insert_into_body(TEXT, block)
        self.assertTrue(new.startswith(TEXT))
        self.assertIn(cp.CONTEXT_OPEN, new)


class EngineIntegrationTest(unittest.TestCase):
    """`create_request` на песочнице: блок в теле карточки, но не в `text` для интерфейса."""

    def test_create_request_puts_block_into_card(self):
        with Sandbox() as sb:
            before = sb.snapshot_real()
            room = rooms.get_room(sb.cfg, "cafe") or rooms.get_room(sb.cfg, "fn")
            if room is None:   # песочница без rooms.json — берём комнату из фикстуры
                room = _room(project_dir="")
            tenant = room.get("tenant") or "room:%s" % room["id"]
            captured = {}
            agents = SimAgents(sb)

            def fake_run_json(cfg, board, args):
                flag = next(a for a in args if str(a).startswith("--body-file="))
                path = flag.split("=", 1)[1]
                captured["body"] = Path(path).read_text(encoding="utf-8")
                title = args[1]
                card_id = agents.card(title, "biz-director", body=captured["body"], tenant=tenant)
                return {"id": card_id}, 7

            original = engine_write._run_json
            engine_write._run_json = fake_run_json
            try:
                body = contract.build_request_body(room, TEXT, "cp-20261028-0001", sb.cfg)
                result = engine_write.create_request(sb.cfg, room, "cp-20261028-0001",
                                                     "Цена набора из 6 моти", body)
            finally:
                engine_write._run_json = original

            root_id = result["root_id"]
            stored = sb.one("SELECT body FROM tasks WHERE id=?", (root_id,))["body"]
            self.assertEqual(stored, captured["body"])
            self.assertTrue(stored.startswith(TEXT))
            self.assertIn(cp.CONTEXT_OPEN, stored)
            self.assertIn("Источник правды — Obsidian", stored)
            self.assertIn(contract.CONTRACT_OPEN, stored)

            detail = engine_read.request_detail(sb.cfg, "team", root_id)
            self.assertEqual(detail["text"], TEXT)
            self.assertNotIn("КОНТЕКСТ КОМНАТЫ", detail["text"])
            self.assertIn(cp.CONTEXT_OPEN, detail["body_full"])
            sb.assert_real_untouched(before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
