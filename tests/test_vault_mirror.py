"""Тесты зеркала комнаты в Obsidian (Блок C волны 8) — только временный Vault.

Проверяем: заметка на запрос, обновление, сохранность ручных правок вне маркеров,
версии ответа, безопасные имена (кириллица/длина/запрещённые символы), защиту от
выхода за ``vault_dir``, идемпотентность, CONTEXT.md ≤ 6 000 знаков, выжимку для
контракта, диалог из ``server.thread`` и отсутствие записи в реальный Vault.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, rooms, registry, safety, vault_mirror  # noqa: E402

AT = 1793165000  # 28.10.2026
BAD_CHARS = set('<>:"/\\|?*')


def snapshot_tree(path: Path) -> dict:
    """Снимок каталога: относительный путь → (is_dir, размер, mtime)."""
    out: dict = {}
    if not path.exists():
        return out
    for item in sorted(path.rglob("*")):
        try:
            stat = item.stat()
            rel = str(item.relative_to(path))
        except OSError:
            continue
        out[rel] = (item.is_dir(), 0 if item.is_dir() else stat.st_size, int(stat.st_mtime))
    return out


def make_detail(root_id: str = "t_a1", title: str = "Дорожная карта проекта", **over) -> dict:
    detail = {
        "id": root_id,
        "title": title,
        "text": "Исходный текст запроса\nвторая строка — дословно",
        "created_at": AT - 4000,
        "status": "done",
        "synthesis": {"state": "ready", "text_md": "Готовый ответ.", "author": "biz-director",
                      "at": AT, "version": 1},
        "next_steps": [{"id": "ns_1", "text": "Подать заявление на ИП", "owner": "mikhail",
                        "due": "2026-11-01", "state": "open"}],
        "open_questions": [{"id": "q_1", "text": "Класс 30 или 30+43?", "for": "mikhail",
                           "answered": False, "answer": None}],
        "decisions": [{"id": "d_1", "text": "Старт с набора 6 моти", "why": "дешевле",
                      "status": "accepted"}],
        "future_questions": [],
        "artifacts": [],
        "tree": [{"id": "t_a1", "title": "Корень", "assignee": "biz-director", "status": "done",
                  "started_at": AT - 100, "finished_at": AT}],
        "report": None,
    }
    detail.update(over)
    return detail


class VaultMirrorBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-vm-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
            "TEAMROOM_HERMES_HOME": str(self.tmp / "hermes"),
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        rooms.seed_defaults(self.cfg)
        self.vault = Path(self.cfg.vault_dir).resolve()
        self.notes = self.vault / "cafe" / vault_mirror.NOTE_DIR
        self.real_projects = safety.real_vault() / "Projects"
        self.real_before = snapshot_tree(self.real_projects)
        registry.add_request(self.cfg, "cafe", {
            "root_id": "t_a1", "title": "Дорожная карта проекта", "text": "Исходный текст",
            "created_at": AT - 4000,
        })

    def tearDown(self):
        after = snapshot_tree(self.real_projects)
        self.assertEqual(after, self.real_before, "тест изменил реальный Vault C:/TeamRoom/Vault/Projects")
        self._env.stop()
        self._tmp.cleanup()

    # --- помощники ---
    def sync(self, detail=None, room_id="cafe", root_id="t_a1"):
        return vault_mirror.sync_request(self.cfg, room_id, root_id, detail or make_detail())

    def note_path(self, detail=None) -> Path:
        return Path(self.sync(detail)["path"])


class RequestNoteTests(VaultMirrorBase):
    def test_creates_note_with_frontmatter_and_sections(self):
        path = self.note_path()
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.notes)
        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2} .+ \(t_a1\)\.md$")
        raw = path.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith("---\n"))
        for key in ("tags:", "room:", "request_id:", "status:", "created:", "versions:"):
            self.assertIn(key, text)
        for heading in ("## Запрос", "## Диалог", "## Ответ", "## Дальше",
                        "## Вопросы к Михаилу", "## Решения", "## Артефакты",
                        "## Кто что делал", "## См. также"):
            self.assertIn(heading, text)
        self.assertIn("Исходный текст запроса", text)
        self.assertIn("Готовый ответ.", text)
        self.assertIn("[[CONTEXT|", text)
        self.assertIn("<!-- teamroom:begin answer -->", text)
        self.assertIn("<!-- teamroom:end -->", text)

    def test_idempotent_second_sync_does_not_rewrite(self):
        first = self.sync()
        path = Path(first["path"])
        before = path.read_bytes()
        second = self.sync()
        self.assertFalse(second["changed"])
        self.assertEqual(path.read_bytes(), before)

    def test_manual_edits_outside_markers_survive(self):
        path = self.note_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n\nМоя заметка руками — не терять.\n")
        detail = make_detail(status="failed", synthesis={
            "state": "ready", "text_md": "Новый ответ.", "version": 2,
            "history": [{"version": 1, "text_md": "Готовый ответ.", "kind": "initial", "at": AT - 10}],
        })
        self.assertTrue(self.sync(detail)["changed"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("Моя заметка руками — не терять.", text)
        self.assertIn("**Статус:** ошибка", text)
        self.assertIn("Новый ответ.", text)

    def test_new_version_and_old_under_version_heading(self):
        detail = make_detail(synthesis={
            "state": "ready", "text_md": "Ответ версии 3.", "author": "biz-director",
            "at": AT, "version": 3,
            "history": [
                {"version": 1, "text_md": "Первый ответ.", "kind": "initial", "at": AT - 20,
                 "note": "Первый свод"},
                {"version": 2, "text_md": "Второй ответ.", "kind": "revision", "at": AT - 10},
            ],
        })
        text = self.note_path(detail).read_text(encoding="utf-8")
        self.assertIn("Ответ версии 3.", text)
        self.assertIn("### Версия 2", text)
        self.assertIn("### Версия 1", text)
        self.assertIn("Второй ответ.", text)
        self.assertIn("Первый ответ.", text)
        self.assertLess(text.index("Ответ версии 3."), text.index("### Версия 2"))

    def test_windows_safe_names_for_long_and_exotic_titles(self):
        titles = [
            "Ж" * 300,
            "Отчёт: заказ/смета <>|?* и точка. в конце...",
            "CON",
            "   ...   ",
            "😀 Десерт — Лето 🍰",
        ]
        for number, title in enumerate(titles):
            root_id = "t_name%d" % number
            result = self.sync(make_detail(root_id=root_id, title=title), root_id=root_id)
            path = Path(result["path"])
            self.assertEqual(path.parent, self.notes)
            self.assertTrue(path.is_file(), path)
            self.assertLessEqual(len(path.name), 170)
            self.assertFalse(BAD_CHARS & set(path.name), path.name)
            self.assertFalse(path.name.startswith(" "), path.name)
            self.assertNotIn(path.stem.lower(), {"con", "prn", "aux", "nul"})
            self.assertTrue(path.resolve().is_relative_to(self.vault))

    def test_traversal_stays_inside_vault(self):
        cases = [("..", "../evil", "../../evil"),
                 ("cafe", "../../../evil", "../../evil"),
                 ("a/b", "a\\b", "a/b")]
        base = self.vault
        for room_id, root_id, title in cases:
            result = self.sync(make_detail(root_id=root_id, title=title),
                               room_id=room_id, root_id=root_id)
            path = Path(result["path"])
            self.assertTrue(path.resolve().is_relative_to(base), path)
        for item in self.tmp.rglob("*.md"):
            self.assertTrue(item.resolve().is_relative_to(base), item)

    def test_mock_mode_writes_nothing(self):
        with mock.patch.dict(os.environ, {"TEAMROOM_MOCK": "1"}):
            result = self.sync()
        self.assertFalse(result["changed"])
        self.assertEqual(result.get("skipped"), "mock")
        self.assertFalse((self.vault / "cafe" / vault_mirror.NOTE_DIR).exists())

    def test_no_write_outside_vault_dir(self):
        self.sync()
        vault_mirror.sync_room(self.cfg, "cafe")
        for item in self.tmp.rglob("*"):
            if item.is_file() and item.suffix in (".md", ".tmp"):
                self.assertTrue(item.resolve().is_relative_to(self.vault), item)


class DialogAndSummaryTests(VaultMirrorBase):
    def test_thread_items_rendered_in_order(self):
        items = [
            {"id": "c1", "at": AT - 20, "author_kind": "mikhail", "author": "mikhail",
             "author_label": "Михаил", "text": "Уточнение: не забудь про Роспатент",
             "task_id": "t_a1", "kind": "message"},
            {"id": "c2", "at": AT - 10, "author_kind": "agent", "author": "biz-director",
             "author_label": "Глеб · руководитель комнаты", "text": "Принял, передал",
             "task_id": "t_a1", "kind": "comment"},
        ]
        try:
            import server.thread  # noqa: F401
        except Exception:  # pragma: no cover - модуль соседа может быть не готов
            self.skipTest("server.thread недоступен")
        with mock.patch("server.thread.get_thread", return_value=items):
            text = self.note_path().read_text(encoding="utf-8")
        self.assertIn("Михаил", text)
        self.assertIn("Уточнение: не забудь про Роспатент", text)
        self.assertIn("Глеб · руководитель комнаты", text)
        self.assertLess(text.index("Уточнение: не забудь"), text.index("Принял, передал"))

    def test_thread_failure_leaves_empty_dialog(self):
        try:
            import server.thread  # noqa: F401
        except Exception:  # pragma: no cover
            self.skipTest("server.thread недоступен")
        with mock.patch("server.thread.get_thread", side_effect=RuntimeError("нет доски")):
            text = self.note_path().read_text(encoding="utf-8")
        self.assertIn("## Диалог", text)
        self.assertIn("Пока сообщений нет.", text)

    def test_context_md_limit_content_and_glossary(self):
        dict_dir = Path(self.cfg.data_dir) / "dictation"
        dict_dir.mkdir(parents=True, exist_ok=True)
        (dict_dir / "dictionary.json").write_text(
            '{"terms": ["Лето", "Роспатент", "свидетельство"], "replacements": []}', encoding="utf-8")
        for number in range(40):
            registry.add_request(self.cfg, "cafe", {
                "root_id": "t_%03d" % number, "title": "Очень длинный запрос номер %d " % number * 2,
                "text": "текст " * 60, "created_at": AT - number * 100,
                "summary": "одна строка ответа " * 10,
            })
        result = vault_mirror.sync_room(self.cfg, "cafe")
        path = Path(result["context_path"])
        self.assertEqual(path, self.vault / "cafe" / "CONTEXT.md")
        text = path.read_text(encoding="utf-8")
        self.assertLessEqual(len(text), vault_mirror.CONTEXT_LIMIT)
        for heading in ("## О комнате", "## Текущий этап", "## Решения", "## Открытые вопросы",
                        "## Ближайшие шаги", "## Последние 10 запросов", "## Глоссарий",
                        "## Где искать"):
            self.assertIn(heading, text)
        self.assertIn("свидетельство", text)
        self.assertIn("[[", text)
        self.assertTrue((self.vault / "cafe" / "STATE.md").is_file())
        self.assertTrue((self.vault / "cafe" / "Журнал.md").is_file())

    def test_context_manual_edit_survives_and_is_idempotent(self):
        first = vault_mirror.sync_room(self.cfg, "cafe")
        path = Path(first["context_path"])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n\nЗаметка Михаила про комнату.\n")
        registry.add_request(self.cfg, "cafe", {"root_id": "t_new", "title": "Новый запрос",
                                               "text": "текст", "created_at": AT})
        self.assertTrue(vault_mirror.sync_room(self.cfg, "cafe")["changed"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("Заметка Михаила про комнату.", text)
        self.assertIn("Новый запрос", text)
        self.assertFalse(vault_mirror.sync_room(self.cfg, "cafe")["changed"])

    def test_context_summary_is_short(self):
        text = vault_mirror.context_summary(self.cfg, "cafe", max_chars=300)
        self.assertTrue(text)
        self.assertLessEqual(len(text), 300)
        self.assertIn("Текущий этап", text)


class WatcherIntegrationTests(VaultMirrorBase):
    """Тик watcher'а пишет заметку запроса с изменениями и CONTEXT.md комнаты."""

    def test_tick_syncs_request_note_once(self):
        from server import watcher as watcher_mod
        room = rooms.get_room(self.cfg, "cafe")
        detail = make_detail()
        record = {"root_id": "t_a1", "title": "Дорожная карта проекта", "created_at": AT - 4000,
                  "ingested": {}, "version": 1}
        calls = {"request": 0, "room": 0}

        def sync_request(cfg, room_id, root_id, detail=None):
            calls["request"] += 1
            return vault_mirror.sync_request(cfg, room_id, root_id, detail)

        def sync_room(cfg, room_id):
            calls["room"] += 1
            return vault_mirror.sync_room(cfg, room_id)

        modules = {
            "rooms": SimpleNamespace(list_rooms=lambda cfg, include_archived=False: [dict(room)]),
            "registry": SimpleNamespace(list_requests=lambda cfg, room_id, limit=50: [dict(record)]),
            "engine_read": SimpleNamespace(
                request_detail=lambda cfg, board, root, rm, extra, **kw: dict(detail)),
            "critic_loop": SimpleNamespace(run_step=lambda cfg, rm, rec, det: []),
            "ingest": SimpleNamespace(
                ingest_request=lambda cfg, rm, det: {"changed": True, "skipped": None},
                version_key=lambda det: "v1",
                write_vault_mirror=lambda cfg, room_id: False,
            ),
            "vault_mirror": SimpleNamespace(sync_request=sync_request, sync_room=sync_room),
        }

        class FakeLog:
            def error(self, *args, **kwargs):
                pass

        class FakeApp:
            def __init__(self, cfg):
                self.cfg = cfg
                self.board = "team"
                self.log = FakeLog()

            def fn(self, name, attr):
                return getattr(modules[name], attr)

            def call(self, name, attr, *attempts):
                args, kwargs = attempts[0]
                return self.fn(name, attr)(*args, **kwargs)

        watch = watcher_mod.Watcher(FakeApp(self.cfg))
        stats = watch.tick_once()
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(calls["request"], 1)
        self.assertEqual(calls["room"], 1)
        notes = list(self.notes.glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertTrue((self.vault / "cafe" / "CONTEXT.md").is_file())
        # повторный тик без изменений — заметку не переписываем
        watch.tick_once()
        self.assertEqual(calls["request"], 1)


if __name__ == "__main__":
    unittest.main()
