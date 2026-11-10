# -*- coding: utf-8 -*-
"""W14-I: «Особенности для этой комнаты» — записи, лимиты, журнал, откат (пункт 13).

Что проверяется:
  * запись = структура `{id, text, source, origin, created_at, used_count, pinned}`;
  * руководитель дописывает записи участнику И СЕБЕ без согласований (API + модуль);
  * дубль по смыслу не плодится; рост ограничен (лимит записей/знаков, переоценка);
  * переоценка: слияние дублей, удаление устаревшего неиспользованного, сжатие по лимиту;
    закреплённые записи Михаила не трогаются никогда;
  * журнал комнаты: каждое добавление/изменение/удаление/слияние с автором, причиной
    и источником; откат конкретного изменения; повторный откат — 409;
  * блок в теле карточки не превышает лимит; секреты маскируются (`mask_secrets`).

Правила волны: только песочница `tests.helpers.Sandbox`, CLI Hermes подменён shim'ом,
файлы профилей (SOUL.md) сверяются по хэшам до/после.

    python -m unittest tests.test_agent_notes_entries -v
"""
from __future__ import annotations

import hashlib
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_notes as notes_mod          # noqa: E402
import server.app as app_mod                    # noqa: E402
import server.context_pack as context_pack      # noqa: E402
import server.rooms as rooms_mod                # noqa: E402
import server.vault_mirror as mirror            # noqa: E402
from server.http_util import ApiError           # noqa: E402
from tests.helpers import Sandbox               # noqa: E402

ROOM = "cafe"


def soul_hashes() -> dict:
    """Хэши SOUL.md реальных профилей (файлы профилей менять нельзя никогда)."""
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
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
            assert soul_hashes() == cls.soul_before, "файлы профилей (SOUL.md) изменились!"
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_notes": {}, "agent_notes_log": []}), {}))

    # --- помощники ---------------------------------------------------------
    def room(self) -> dict:
        return self.app.call("rooms", "get_room", ((self.cfg, ROOM), {}))

    def entries(self, profile: str = "biz-lawyer") -> list:
        return notes_mod.entries_of(notes_mod.notes_of(self.room()).get(profile) or {})

    def add(self, profile: str, text: str, **kwargs):
        return notes_mod.add(self.cfg, ROOM, profile, text, **kwargs)

    def dispatch(self, method: str, path: str, body=None, query=None):
        try:
            return self.app.dispatch(method, path, query or {}, body or {})
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def notes_path(self, profile: str = "biz-lawyer") -> str:
        return "/api/rooms/%s/agents/%s/notes" % (ROOM, profile)


class EntriesTests(_Base):
    def test_add_entry_structure_and_journal(self):
        res = self.add("biz-lawyer", "Проверяй договор по закону X",
                       source="leader", origin="задача t_1", why="ошибался в аренде",
                       actor="biz-director")
        entry = res["entry"]
        for key in ("id", "text", "source", "origin", "created_at", "used_count", "pinned"):
            self.assertIn(key, entry)
        self.assertEqual(entry["source"], "leader")
        self.assertFalse(entry["pinned"])
        self.assertEqual(entry["origin"], "задача t_1")
        self.assertGreater(entry["created_at"], 0)

        events = notes_mod.journal(self.cfg, ROOM)
        self.assertTrue(events, "добавление должно попасть в журнал")
        first = events[0]
        self.assertEqual(first["action"], "add")
        self.assertEqual(first["profile"], "biz-lawyer")
        self.assertIn("закону X", first["text"])
        self.assertIn("аренде", first["why"])
        self.assertIn("t_1", first["origin"])

    def test_leader_writes_to_himself(self):
        lead = self.room().get("orchestrator")
        res = self.add(lead, "Ornith — слабая модель, но локальная: простые задачи ей")
        self.assertEqual(res["profile"], lead)
        self.assertTrue(notes_mod.get(self.cfg, ROOM, lead)["entries"])

    def test_duplicate_is_not_duplicated(self):
        self.add("biz-lawyer", "Отвечай коротко и цифрами")
        before = len(self.entries())
        res = self.add("biz-lawyer", "  отвечай КОРОТКО и цифрами ")
        self.assertTrue(res.get("duplicate"))
        self.assertEqual(len(self.entries()), before)

    def test_profile_must_be_in_room(self):
        with self.assertRaises(notes_mod.NotesError) as ctx:
            self.add("coder", "чужой профиль")
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_pinned_manual_entry_cannot_be_removed_by_leader(self):
        res = self.add("biz-lawyer", "Ручная запись Михаила", source="user")
        entry_id = res["entry"]["id"]
        self.assertTrue(res["entry"]["pinned"])
        with self.assertRaises(notes_mod.NotesError) as ctx:
            notes_mod.remove_entry(self.cfg, ROOM, "biz-lawyer", entry_id,
                                   actor="biz-director")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(len(self.entries()), 1, "запись Михаила осталась")
        # сам Михаил (интерфейс) убирает её с force=True
        notes_mod.remove_entry(self.cfg, ROOM, "biz-lawyer", entry_id, actor="Михаил", force=True)
        self.assertEqual(self.entries(), [])

    def test_used_count(self):
        self.add("biz-lawyer", "Держи в курсе по срокам")
        notes_mod.touch_used(self.cfg, ROOM, "biz-lawyer")
        self.assertEqual(self.entries()[0]["used_count"], 1)
        notes_mod.touch_used(self.cfg, ROOM, "biz-lawyer")
        self.assertEqual(self.entries()[0]["used_count"], 2)


class LimitsTests(_Base):
    def _seed(self, count: int, prefix: str = "правило", *, source: str = "leader") -> list:
        """Заполнить реестр записями напрямую (без автопереоценки на каждом шаге)."""
        entries = [{"id": "n_%02d" % i, "text": "%s %d" % (prefix, i),
                    "source": source, "created_at": int(time.time()), "used_count": 0,
                    "pinned": False} for i in range(count)]
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_notes": {"biz-lawyer": {"entries": entries}}}), {}))
        return entries

    def test_review_runs_after_every_five_entries(self):
        for index in range(5):
            self.add("biz-lawyer", "правило номер %d" % index)
        # пятое добавление запускает переоценку: пять разных записей остаются, но
        # отметка «проверено» снимает порог (иначе переоценка шла бы на каждой записи)
        review = ((self.room().get("agent_notes") or {}).get("biz-lawyer") or {}).get("review") or {}
        self.assertFalse(review.get("due"))
        self.assertEqual(review.get("count_at_review"), 5)
        self.assertEqual(len(self.entries()), 5)

    def test_entries_limit_is_enforced(self):
        for index in range(notes_mod.MAX_ENTRIES + 1):
            self.add("biz-lawyer", "правило номер %d" % index, origin="проверка лимита")
        self.assertLessEqual(len(self.entries()), notes_mod.MAX_ENTRIES,
                             "записей больше лимита быть не должно")
        actions = [e["action"] for e in notes_mod.journal(self.cfg, ROOM)]
        self.assertIn("review", actions, "переоценка должна быть в журнале")
        self.assertIn("remove", actions, "лишнее убрано на переоценке")

    def test_review_merges_duplicates_and_drops_stale_unused(self):
        old = int(time.time()) - (notes_mod.STALE_DAYS + 5) * 86400
        entries = [
            {"id": "n_old1", "text": "Устаревшее правило", "source": "leader",
             "created_at": old, "used_count": 0, "pinned": False},
            {"id": "n_dup1", "text": "Отвечай цифрами", "source": "leader",
             "created_at": old + 10, "used_count": 1, "pinned": False},
            {"id": "n_dup2", "text": "отвечай  цифрами", "source": "leader",
             "created_at": old + 20, "used_count": 2, "pinned": False},
            {"id": "n_pin1", "text": "Ручная запись Михаила", "source": "user",
             "created_at": old, "used_count": 0, "pinned": True},
        ]
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_notes": {"biz-lawyer": {"entries": entries}}}), {}))
        preview = notes_mod.review(self.cfg, ROOM, "biz-lawyer", apply=False)
        self.assertEqual(preview["before"], 4)
        self.assertEqual(len(preview["merges"]), 1)
        self.assertEqual(len(preview["drops"]), 1)

        res = notes_mod.review(self.cfg, ROOM, "biz-lawyer", actor="Михаил", source="user",
                               why="сократить перед стартом")
        self.assertTrue(res["applied"])
        texts = [one["text"] for one in self.entries()]
        self.assertEqual(len(texts), 2, texts)
        self.assertIn("Ручная запись Михаила", texts, "закреплённая запись не тронута")
        merged = next(one for one in self.entries() if "цифрами" in one["text"])
        self.assertEqual(merged["used_count"], 3, "счётчик использования суммируется")
        self.assertNotIn("Устаревшее правило", texts)

        actions = [e["action"] for e in notes_mod.journal(self.cfg, ROOM)]
        for action in ("merge", "remove", "review"):
            self.assertIn(action, actions)

    def test_review_keeps_everything_when_nothing_to_do(self):
        self.add("biz-lawyer", "Единственное правило")
        res = notes_mod.review(self.cfg, ROOM, "biz-lawyer")
        self.assertFalse(res["applied"])
        self.assertEqual(len(self.entries()), 1)

    def test_render_block_stays_within_limit(self):
        for index in range(12):
            self.add("biz-lawyer", ("длинное правило %d " % index) * 20)
        block = notes_mod.render_block(self.cfg, self.room(), 3000)
        self.assertLessEqual(len(block), 3000)
        self.assertIn("Особые указания участникам этой комнаты", block)


class JournalRollbackTests(_Base):
    def test_rollback_add_removes_entry(self):
        res = self.add("biz-lawyer", "Правило, которое передумали писать")
        event = notes_mod.journal(self.cfg, ROOM)[0]
        self.assertEqual(event["action"], "add")
        back = notes_mod.rollback(self.cfg, ROOM, event["id"], actor="Михаил")
        self.assertEqual(back["entries"], [])
        actions = [e["action"] for e in notes_mod.journal(self.cfg, ROOM)]
        self.assertIn("rollback", actions)
        self.assertTrue(next(e for e in notes_mod.journal(self.cfg, ROOM)
                             if e["id"] == event["id"]).get("rolled_back"))
        self.assertTrue(res["entry"]["id"])

    def test_rollback_remove_restores_entry(self):
        res = self.add("biz-lawyer", "Правило, которое убрали по ошибке")
        entry_id = res["entry"]["id"]
        notes_mod.remove_entry(self.cfg, ROOM, "biz-lawyer", entry_id, why="передумали")
        self.assertEqual(self.entries(), [])
        event = notes_mod.journal(self.cfg, ROOM)[0]
        self.assertEqual(event["action"], "remove")
        notes_mod.rollback(self.cfg, ROOM, event["id"], actor="Михаил")
        self.assertEqual([one["id"] for one in self.entries()], [entry_id])

    def test_rollback_twice_is_conflict(self):
        self.add("biz-lawyer", "Правило")
        event = notes_mod.journal(self.cfg, ROOM)[0]
        notes_mod.rollback(self.cfg, ROOM, event["id"], actor="Михаил")
        with self.assertRaises(notes_mod.NotesError) as ctx:
            notes_mod.rollback(self.cfg, ROOM, event["id"], actor="Михаил")
        self.assertEqual(ctx.exception.code, "conflict")

    def test_rollback_keeps_pinned_added_after(self):
        self.add("biz-lawyer", "Первое правило", source="leader")
        first = notes_mod.journal(self.cfg, ROOM)[0]
        self.add("biz-lawyer", "Ручная запись Михаила", source="user")
        notes_mod.rollback(self.cfg, ROOM, first["id"], actor="Михаил")
        texts = [one["text"] for one in self.entries()]
        self.assertIn("Ручная запись Михаила", texts, "закреплённое не теряется при откате")
        self.assertNotIn("Первое правило", texts)

    def test_journal_masks_secrets(self):
        secret = "password=" + "Hunter2" + "SuperSecret"
        self.add("biz-lawyer", "Ключ: " + secret)
        event = notes_mod.journal(self.cfg, ROOM)[0]
        self.assertNotIn("Hunter2SuperSecret", event["text"])
        self.assertIn("[скрыто]", event["text"])


class ApiTests(_Base):
    def test_get_returns_entries_and_limits(self):
        self.add("biz-lawyer", "Отвечай цифрами")
        status, body, _h = self.dispatch("GET", self.notes_path())
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(body["entries"][0]["text"], "Отвечай цифрами")
        self.assertEqual(body["limits"]["max_entries"], notes_mod.MAX_ENTRIES)
        self.assertIn("journal", body)

    def test_post_entry_and_patch_and_delete(self):
        status, body, _h = self.dispatch("POST", self.notes_path() + "/entries",
                                         {"text": "Проверяй сроки", "source": "leader",
                                          "origin": "комментарий Михаила"})
        self.assertEqual(status, 201, body)
        entry_id = body["entry"]["id"]

        status, body, _h = self.dispatch(
            "PATCH", self.notes_path() + "/entries/" + entry_id,
            {"text": "Проверяй сроки и подписантов"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entries"][0]["text"], "Проверяй сроки и подписантов")

        status, body, _h = self.dispatch("DELETE", self.notes_path() + "/entries/" + entry_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entries"], [])

    def test_pinned_delete_needs_force(self):
        status, body, _h = self.dispatch("POST", self.notes_path() + "/entries",
                                         {"text": "Ручная запись", "source": "user"})
        entry_id = body["entry"]["id"]
        status, body, _h = self.dispatch("DELETE", self.notes_path() + "/entries/" + entry_id)
        self.assertEqual(status, 409, body)
        status, body, _h = self.dispatch("DELETE", self.notes_path() + "/entries/" + entry_id,
                                         {"force": True, "why": "убрал сам"})
        self.assertEqual(status, 200, body)

    def test_review_preview_and_apply(self):
        for index in range(3):
            self.dispatch("POST", self.notes_path() + "/entries",
                          {"text": "правило %d" % index, "source": "leader"})
        status, body, _h = self.dispatch("POST", self.notes_path() + "/review", {"apply": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["applied"])
        self.assertEqual(body["before"], 3)

    def test_journal_and_rollback_endpoints(self):
        status, body, _h = self.dispatch("POST", self.notes_path() + "/entries",
                                         {"text": "Правило для отката", "source": "leader"})
        self.assertEqual(status, 201, body)
        status, body, _h = self.dispatch("GET", "/api/rooms/%s/notes/journal" % ROOM)
        self.assertEqual(status, 200, body)
        event_id = body["events"][0]["id"]
        status, body, _h = self.dispatch(
            "POST", "/api/rooms/%s/notes/journal/%s/rollback" % (ROOM, event_id),
            {"actor": "Михаил"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entries"], [])

    def test_room_summary(self):
        self.add("biz-lawyer", "Отвечай цифрами")
        status, body, _h = self.dispatch("GET", "/api/rooms/%s/notes" % ROOM)
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["count"], 1)
        people = {row["profile"] for row in body["participants"]}
        self.assertIn("biz-lawyer", people)


class DeliveryTests(_Base):
    def test_context_block_and_vault_show_entries(self):
        self.add("biz-lawyer", "Всегда проверяй по закону X", source="user")
        self.add("biz-lawyer", "Не согласовывай сроки без меня", source="leader",
                 origin="комментарий Михаила 05.11")
        block = context_pack.build_context_block(self.cfg, self.room(), "Проверь договор")
        self.assertIn("Всегда проверяй по закону X", block)
        self.assertIn("Не согласовывай сроки", block)
        self.assertLessEqual(len(block), context_pack.MAX_BLOCK_CHARS)

        res = mirror.sync_room(self.cfg, ROOM)
        text = Path(res["notes_path"]).read_text(encoding="utf-8")
        self.assertIn("Не согласовывай сроки", text)
        self.assertIn("комментарий Михаила", text)
        self.assertIn("Журнал изменений", text)


if __name__ == "__main__":
    unittest.main()
