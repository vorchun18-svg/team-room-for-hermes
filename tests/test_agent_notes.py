# -*- coding: utf-8 -*-
"""W9-AGENT-NOTES: особые указания участникам комнаты (docs/WAVE9-RULES.md §9).

Что проверяется:
  * ``agent_notes.validate`` — профиль из состава, ≤ 8 000 знаков, пустой текст
    = очистка, секреты в тексте → предупреждение (маска), а не блокировка;
  * поле комнаты ``agent_notes`` в реестре, ``GET /api/rooms/{id}``,
    ``PUT /api/rooms/{id}/agents/{profile}/notes`` (400 по-русски на чужой профиль),
    ``POST /api/rooms`` с указаниями;
  * раздел «Особые указания участникам этой комнаты» в ``context_pack.build_context_block``
    (≤ 3 000 знаков на раздел, правило про руководителя, полный текст — в Vault);
  * ``vault_mirror.sync_room``: раздел в CONTEXT.md и файл «Указания участникам.md»;
  * автокомментарии ``[Указание комнаты · hash8]``: ровно по одной строке на карточку
    дерева, идемпотентно, при смене текста — «(обновлено)»;
  * файлы профилей не тронуты: хэши SOUL.md до/после совпадают.

Правила волны 7/9: только песочница ``tests.helpers.Sandbox``, CLI Hermes подменён
shim'ом (он же имитирует запись комментария в песочную БД), реальная доска
сверяется до/после.

    python -m unittest tests.test_agent_notes -v

# SHIM-OK: CLI Hermes подменён CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import hashlib
import json
import subprocess as real_subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_notes as notes_mod          # noqa: E402
import server.app as app_mod                   # noqa: E402
import server.context_pack as context_pack     # noqa: E402
import server.engine_cli as engine_cli         # noqa: E402
import server.engine_write as ew               # noqa: E402
import server.rooms as rooms_mod               # noqa: E402
import server.vault_mirror as mirror           # noqa: E402
from server.http_util import ApiError          # noqa: E402
from tests.helpers import Sandbox, SimAgents   # noqa: E402

ROOM = "cafe"
MARKER = notes_mod.MARKER_PREFIX


class CliShim:
    """Подмена subprocess: ничего не запускаем, но `comment` пишем в песочную БД.

    Так проверяется настоящая идемпотентность: приложение читает маркер из БД,
    а не из памяти теста.
    """

    def __init__(self, sa: SimAgents) -> None:
        self.calls: list[list[str]] = []
        self.sa = sa
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                     # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if "comment" in argv:
            index = argv.index("comment")
            task_id = argv[index + 1] if index + 1 < len(argv) else ""
            author = "mikhail"
            if "--author" in argv:
                author = argv[argv.index("--author") + 1]
            text = ""
            if "--" in argv:
                text = argv[argv.index("--") + 1]
            if task_id.startswith("t_"):
                self.sa.comment(task_id, author, text)
        verb = ""
        try:
            start = argv.index("kanban") + 1
        except ValueError:
            start = 0
        skip = False
        for token in argv[start:]:
            if skip:
                skip = False
                continue
            if token in ("--board", "--home"):
                skip = True
                continue
            if token.startswith("-"):
                continue
            verb = token
            break
        out = json.dumps({"id": "t_0000cafe", "status": "ready"}) if verb == "create" else ""
        return real_subprocess.CompletedProcess(argv, 0, out, "")

    # --- разбор ------------------------------------------------------------
    def comments(self) -> list[str]:
        return [c[1] for c in (argv[argv.index("comment") + 1:] for argv in self.calls
                               if "comment" in argv) if c]


def soul_hashes() -> dict:
    """Хэши SOUL.md реальных профилей (чтение; менять их нельзя никогда)."""
    home = Path.home() / "AppData" / "Local" / "hermes"
    paths = [home / "SOUL.md"] + sorted((home / "profiles").glob("*/SOUL.md"))
    out = {}
    for path in paths:
        try:
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.soul_before = soul_hashes()
        cls.sa = SimAgents(cls.sb)
        cls.shim = CliShim(cls.sa)
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))

    @classmethod
    def tearDownClass(cls):
        engine_cli.subprocess = cls._real_sub
        try:
            del ew.subprocess
        except AttributeError:
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
            assert soul_hashes() == cls.soul_before, "файлы профилей (SOUL.md) изменились!"
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.shim.calls.clear()

    # --- помощники ---------------------------------------------------------
    def put(self, profile: str, text: str):
        path = "/api/rooms/%s/agents/%s/notes" % (ROOM, profile)
        try:
            return self.app.dispatch("PUT", path, {}, {"text": text})
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def room(self) -> dict:
        return self.app.call("rooms", "get_room", ((self.cfg, ROOM), {}))

    def room_of_api(self) -> dict:
        status, body, _h = self.app.dispatch("GET", "/api/rooms/%s" % ROOM, {}, {})
        self.assertEqual(status, 200)
        return body

    def clear_notes(self) -> None:
        self.app.call("rooms", "update_room", ((self.cfg, ROOM, {"agent_notes": {}}), {}))


class ValidateTests(_Base):
    def test_profile_must_be_in_room(self):
        with self.assertRaises(notes_mod.NotesError) as ctx:
            notes_mod.validate({"coder": "делай так"}, members=["biz-lawyer"])
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertIn("в составе", str(ctx.exception))

    def test_limit_8000(self):
        notes, warnings = notes_mod.validate({"biz-lawyer": "а" * 8000}, members=["biz-lawyer"])
        self.assertEqual(len(notes["biz-lawyer"]["text"]), 8000)
        self.assertEqual(warnings, [])
        with self.assertRaises(notes_mod.NotesError):
            notes_mod.validate({"biz-lawyer": "а" * 8001}, members=["biz-lawyer"])

    def test_empty_text_clears(self):
        notes, _w = notes_mod.validate({"biz-lawyer": "   "}, members=["biz-lawyer"])
        self.assertEqual(notes, {})

    def test_secret_is_warning_not_block(self):
        notes, warnings = notes_mod.validate(
            {"biz-lawyer": "ключ sk-abcdefghijklmnop1234 и password=hunter2"},
            members=["biz-lawyer"],
        )
        self.assertIn("biz-lawyer", notes, "указание должно сохраниться")
        self.assertTrue(warnings, "про секрет нужно предупредить")
        self.assertTrue(any("ключ" in w.lower() or "пароль" in w.lower() for w in warnings))
        self.assertNotIn("hunter2", " ".join(warnings), "секрет в предупреждении маскируется")

    def test_render_block_limit(self):
        room = {"id": "cafe", "title": "Лето", "orchestrator": "biz-director",
                "members": [{"profile": "biz-lawyer", "role": "worker"},
                            {"profile": "biz-cfo", "role": "worker"}],
                "agent_notes": {"biz-lawyer": {"text": "я" * 4000, "updated_at": 1},
                                "biz-cfo": {"text": "Отвечай цифрами", "updated_at": 1}}}
        block = notes_mod.render_block(self.cfg, room, 3000)
        self.assertLessEqual(len(block), 3000)
        self.assertIn("Особые указания участникам этой комнаты", block)
        self.assertIn("biz-cfo", block)
        self.assertIn("Руководитель при раздаче подзадач", block)
        self.assertIn("…", block)


class ApiTests(_Base):
    def setUp(self):
        super().setUp()
        self.clear_notes()

    def test_room_has_field_and_put_roundtrip(self):
        body = self.room_of_api()
        self.assertIn("agent_notes", body)
        self.assertEqual(body["agent_notes"], {})

        status, payload, _h = self.put("biz-lawyer", "Всегда проверяй по закону X")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["text"], "Всегда проверяй по закону X")
        self.assertIn("biz-lawyer", payload["agent_notes"])

        body = self.room_of_api()
        self.assertEqual(body["agent_notes"]["biz-lawyer"]["text"], "Всегда проверяй по закону X")
        self.assertGreater(body["agent_notes"]["biz-lawyer"]["updated_at"], 0)

    def test_put_foreign_profile_is_400(self):
        status, payload, _h = self.put("coder", "делай так")
        self.assertEqual(status, 400, payload)
        self.assertIn("состав", json.dumps(payload, ensure_ascii=False))

    def test_put_too_long_is_400(self):
        status, payload, _h = self.put("biz-lawyer", "а" * 8001)
        self.assertEqual(status, 400, payload)

    def test_put_empty_clears(self):
        self.put("biz-lawyer", "Коротко и цифрами")
        status, payload, _h = self.put("biz-lawyer", "")
        self.assertEqual(status, 200)
        self.assertTrue(payload["cleared"])
        self.assertEqual(self.room_of_api()["agent_notes"], {})

    def test_put_secret_returns_warning(self):
        status, payload, _h = self.put("biz-lawyer", "токен: sk-abcdefghijklmnop1234")
        self.assertEqual(status, 200)
        self.assertTrue(payload["warnings"])
        self.assertEqual(self.room_of_api()["agent_notes"]["biz-lawyer"]["text"],
                         "токен: sk-abcdefghijklmnop1234")

    def test_create_room_with_notes(self):
        payload = {
            "title": "Комната с указаниями", "id": "notes-room", "board": "team",
            "orchestrator": "biz-director",
            "members": [{"profile": "biz-lawyer", "role": "worker"}],
            "agent_notes": {"biz-lawyer": "Всегда проверяй по закону X"},
        }
        status, body, _h = self.app.dispatch("POST", "/api/rooms", {}, payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["agent_notes"]["biz-lawyer"]["text"], "Всегда проверяй по закону X")

        bad = dict(payload, id="notes-room-2", agent_notes={"coder": "чужой"})
        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("POST", "/api/rooms", {}, bad)
        self.assertEqual(ctx.exception.status, 400)

    def test_patch_accepts_notes(self):
        try:
            status, body, _h = self.app.dispatch(
                "PATCH", "/api/rooms/%s" % ROOM, {},
                {"agent_notes": {"biz-cfo": {"text": "Только цифры"}}})
        except ApiError as exc:
            self.fail("PATCH с указаниями не должен падать: %s" % exc)
        self.assertEqual(status, 200)
        self.assertEqual(body["agent_notes"]["biz-cfo"]["text"], "Только цифры")


class ContextPackTests(_Base):
    def setUp(self):
        super().setUp()
        self.clear_notes()

    def test_section_appears_and_respects_limits(self):
        self.put("biz-lawyer", "Всегда проверяй по закону X")
        self.put("biz-cfo", "Отвечай коротко и только цифрами")
        room = self.room()
        block = context_pack.build_context_block(self.cfg, room, "Проверь договор аренды")
        self.assertIn("Особые указания участникам этой комнаты", block)
        self.assertIn("исполнитель (biz-lawyer): Всегда проверяй по закону X", block)
        self.assertIn("Указания комнаты дополняют твою роль", block)
        self.assertLessEqual(len(block), context_pack.MAX_BLOCK_CHARS)

    def test_without_notes_section_absent(self):
        room = self.room()
        block = context_pack.build_context_block(self.cfg, room, "Проверь договор аренды")
        self.assertNotIn("Особые указания участникам этой комнаты", block)

    def test_long_note_truncated_with_hint(self):
        self.put("biz-lawyer", "правило " * 800)           # 6 400 знаков: обрезка с пометкой
        room = self.room()
        block = context_pack.build_context_block(self.cfg, room, "Договор")
        self.assertIn("полный текст", block)
        self.assertIn("Указания участникам.md", block)
        self.assertLessEqual(len(block), context_pack.MAX_BLOCK_CHARS)


class SecretsMaskTests(_Base):
    """Единая маска: Vault, CONTEXT.md, комментарии и карточка не выдают секрет (волна 9)."""

    # Поддельные «секреты» собираются из кусков, чтобы сканеры не принимали тест за утечку.
    _PW = "Hunter2" + "SuperSecret"
    _SK = "sk-" + "live" + "A1b2C3d4E5f6G7h8"
    _GH = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"
    _BR = "abcdefghij" + "klmnop123456"
    SECRET = f"Смотри: password={_PW}, ключ {_SK}, {_GH} и Bearer {_BR}"
    RAW = (_PW, _SK, _GH, _BR)

    def setUp(self):
        super().setUp()
        self.clear_notes()

    def assert_masked(self, text: str):
        for raw in self.RAW:
            self.assertNotIn(raw, text, "секрет ушёл открытым текстом")
        self.assertIn("[скрыто]", text)

    def test_mask_rules_cover_main_shapes(self):
        masked = notes_mod.mask_secrets(self.SECRET)
        self.assert_masked(masked)
        self.assertIn("password=[скрыто]", masked)
        self.assertIn("Bearer [скрыто]", masked)

    def test_render_block_masks(self):
        self.put("biz-lawyer", self.SECRET)
        block = notes_mod.render_block(self.cfg, self.room(), 3000)
        self.assert_masked(block)

    def test_context_pack_masks(self):
        self.put("biz-lawyer", self.SECRET)
        block = context_pack.build_context_block(self.cfg, self.room(), "Проверь договор")
        self.assert_masked(block)

    def test_vault_files_mask_but_registry_keeps_text(self):
        self.put("biz-lawyer", self.SECRET)
        room = self.room()
        self.assertEqual(room["agent_notes"]["biz-lawyer"]["text"], self.SECRET,
                         "в реестре комнаты текст хранится как есть")
        res = mirror.sync_room(self.cfg, ROOM)
        notes_text = Path(res["notes_path"]).read_text(encoding="utf-8")
        context_text = Path(res["context_path"]).read_text(encoding="utf-8")
        self.assert_masked(notes_text)
        self.assert_masked(context_text)

    def test_comment_is_masked(self):
        self.put("biz-lawyer", self.SECRET)
        root = self.sa.card("Корень с секретом", "biz-director", status="todo", tenant="room:cafe")
        self.sa.decompose(root, [{"title": "Срез", "assignee": "biz-lawyer"}])
        res = notes_mod.ensure_comments(self.cfg, self.room(), root)
        self.assertTrue(res["written"], res)
        bodies = [b for b in
                  (str(r["body"]) for r in self.sb.rows(
                      "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id",
                      (res["written"][0],)))
                  if MARKER in b]
        self.assertEqual(len(bodies), 1, bodies)
        self.assert_masked(bodies[0])


class VaultMirrorTests(_Base):
    def setUp(self):
        super().setUp()
        self.clear_notes()

    def test_context_md_and_notes_file(self):
        self.put("biz-lawyer", "Всегда проверяй по закону X")
        res = mirror.sync_room(self.cfg, ROOM)
        context_path = Path(res["context_path"])
        text = context_path.read_text(encoding="utf-8")
        self.assertIn("## Особые указания", text)
        self.assertIn("biz-lawyer", text)
        notes_path = Path(res["notes_path"])
        self.assertTrue(notes_path.is_file(), notes_path)
        notes_text = notes_path.read_text(encoding="utf-8")
        self.assertIn("Всегда проверяй по закону X", notes_text)
        self.assertIn("teamroom:begin agent_notes", notes_text)
        # внутри песочницы, не в настоящем Vault
        self.assertTrue(str(notes_path).startswith(str(self.sb.vault_dir)),
                        notes_path)

    def test_manual_text_outside_markers_survives(self):
        self.put("biz-cfo", "Отвечай цифрами")
        res = mirror.sync_room(self.cfg, ROOM)
        path = Path(res["notes_path"])
        path.write_text(path.read_text(encoding="utf-8") + "\nМоя заметка руками.\n", encoding="utf-8")
        mirror.sync_room(self.cfg, ROOM)
        self.assertIn("Моя заметка руками.", path.read_text(encoding="utf-8"))


class CommentsTests(_Base):
    """Автокомментарии: по одной строке на карточку, идемпотентно, «обновлено»."""

    def setUp(self):
        super().setUp()
        self.clear_notes()

    def _tree(self, assignees: list[str]) -> str:
        """Своё дерево на тест: комментарии соседних тестов не мешают счёту."""
        root = self.sa.card("Корень запроса", "biz-director", status="todo", tenant="room:cafe")
        self.sa.decompose(root, [{"title": "Срез %d" % i, "assignee": who}
                                 for i, who in enumerate(assignees)])
        return root

    def _bodies(self, task_id: str) -> list[str]:
        rows = self.sb.rows("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,))
        return [str(r["body"]) for r in rows]

    def test_one_line_per_card_and_idempotent(self):
        root = self._tree(["biz-lawyer", "biz-lawyer", "biz-cfo"])
        self.put("biz-lawyer", "Всегда проверяй по закону X")

        first = notes_mod.ensure_comments(self.cfg, self.room(), root)
        self.assertGreaterEqual(len(first["written"]), 2)
        self.assertEqual(first["errors"], [])
        calls_after_first = len(self.shim.calls)

        # ровно по одному комментарию с маркером на карточку исполнителя
        for card_id in first["written"]:
            bodies = [b for b in self._bodies(card_id) if MARKER in b]
            self.assertEqual(len(bodies), 1, bodies)
            self.assertNotIn("\n", bodies[0], "указание — одной строкой")
            self.assertIn("Всегда проверяй по закону X", bodies[0])
            self.assertIn(notes_mod.comment_hash("biz-lawyer", "Всегда проверяй по закону X"),
                          bodies[0])

        second = notes_mod.ensure_comments(self.cfg, self.room(), root)
        self.assertEqual(second["written"], [])
        self.assertEqual(len(self.shim.calls), calls_after_first,
                         "повторный проход не должен звать CLI")

    def test_changed_text_writes_updated_marker(self):
        root = self._tree(["biz-lawyer"])
        self.put("biz-lawyer", "Отвечай коротко")
        first = notes_mod.ensure_comments(self.cfg, self.room(), root)
        self.assertTrue(first["written"])

        self.put("biz-lawyer", "Отвечай коротко и только цифрами")
        second = notes_mod.ensure_comments(self.cfg, self.room(), root)
        self.assertTrue(second["written"], "смена текста — новый комментарий")

        card = second["written"][0]
        bodies = [b for b in self._bodies(card) if MARKER in b]
        self.assertEqual(len(bodies), 2, bodies)
        latest = bodies[-1]
        self.assertIn("(обновлено)", latest)
        self.assertIn("только цифрами", latest)

    def test_other_assignees_untouched(self):
        root = self._tree(["biz-cfo"])
        self.put("biz-lawyer", "Всегда проверяй по закону X")
        res = notes_mod.ensure_comments(self.cfg, self.room(), root)
        self.assertEqual(res["written"], [])
        self.assertEqual(self.shim.calls, [])

    def test_no_notes_no_cli(self):
        root = self._tree(["biz-lawyer"])
        res = notes_mod.ensure_comments(self.cfg, self.room(), root)
        self.assertEqual(res, {"written": [], "skipped": 0, "errors": []})
        self.assertEqual(self.shim.calls, [])


if __name__ == "__main__":
    unittest.main()
