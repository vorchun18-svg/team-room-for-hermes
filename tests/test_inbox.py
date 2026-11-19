"""Входящие комнаты (W26, пакет D9): хранилище, разбор, предложение руководителя.

Всё на временном каталоге вне реального ``%LOCALAPPDATA%\\hermes``: модуль
трогает только ``cfg.data_dir``, Hermes ему не нужен (правило WAVE22 §0).

    python -m unittest tests.test_inbox -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import inbox as inbox_mod          # noqa: E402

ROOM = "rukovodstvo"


def make_cfg(root: Path):
    return types.SimpleNamespace(data_dir=Path(root), hermes_home=Path(root) / "hermes", sandbox=True)


class InboxStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_lives_under_state_room(self):
        path = inbox_mod.inbox_path(self.cfg, ROOM)
        self.assertEqual(path, Path(self.tmp.name) / "state" / ROOM / "inbox.json")
        self.assertFalse(path.exists())

    def test_add_writes_required_fields(self):
        item = inbox_mod.add(self.cfg, ROOM, "  Проверить договор по Альфе  ", "dictation")
        self.assertTrue(item["id"].startswith("in_"))
        self.assertEqual(item["text"], "Проверить договор по Альфе")
        self.assertEqual(item["source"], "dictation")
        self.assertEqual(item["source_title"], "диктовка")
        self.assertEqual(item["status"], "new")
        self.assertTrue(item["created_at"])
        self.assertTrue(item["created_hhmm"])
        self.assertTrue(item["actions"], "у новой записи обязано быть действие")

        raw = json.loads((Path(self.tmp.name) / "state" / ROOM / "inbox.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["room"], ROOM)
        self.assertEqual(len(raw["items"]), 1)
        self.assertEqual(raw["items"][0]["source"], "dictation")

    def test_sources_and_validation(self):
        with self.assertRaises(inbox_mod.InboxError) as empty:
            inbox_mod.add(self.cfg, ROOM, "   ", "manual")
        self.assertEqual(empty.exception.http, 400)
        with self.assertRaises(inbox_mod.InboxError) as too_long:
            inbox_mod.add(self.cfg, ROOM, "а" * (inbox_mod.MAX_TEXT + 1), "manual")
        self.assertEqual(too_long.exception.http, 400)
        with self.assertRaises(inbox_mod.InboxError) as bad_source:
            inbox_mod.add(self.cfg, ROOM, "текст", "телепатия")
        self.assertEqual(bad_source.exception.http, 400)
        # вручную — тоже допустимый источник
        item = inbox_mod.add(self.cfg, ROOM, "набрано руками", "manual")
        self.assertEqual(item["source_title"], "вручную")

    def test_counts_and_filters(self):
        first = inbox_mod.add(self.cfg, ROOM, "первая мысль", "manual")
        second = inbox_mod.add(self.cfg, ROOM, "вторая мысль", "dictation")
        listing = inbox_mod.items(self.cfg, ROOM, "active")
        self.assertEqual(listing["counts"]["new"], 2)
        self.assertEqual(listing["counts"]["active"], 2)
        # новые сверху: последняя запись первая в списке
        self.assertEqual([x["id"] for x in listing["items"]], [second["id"], first["id"]])
        inbox_mod.act(self.cfg, ROOM, first["id"], "delete")
        self.assertEqual(inbox_mod.items(self.cfg, ROOM, "active")["counts"]["new"], 1)
        self.assertEqual(inbox_mod.items(self.cfg, ROOM, "all")["counts"]["deleted"], 1)
        self.assertEqual(inbox_mod.items(self.cfg, ROOM, "deleted")["items"][0]["id"], first["id"])
        with self.assertRaises(inbox_mod.InboxError):
            inbox_mod.items(self.cfg, ROOM, "каша")

    def test_unknown_item_and_id_shape(self):
        with self.assertRaises(inbox_mod.InboxError) as exc:
            inbox_mod.act(self.cfg, ROOM, "in_deadbeef", "delete")
        self.assertEqual(exc.exception.http, 404)
        with self.assertRaises(inbox_mod.InboxError) as bad:
            inbox_mod.act(self.cfg, ROOM, "../../etc/passwd", "delete")
        self.assertEqual(bad.exception.http, 404)

    def test_corrupt_file_is_quarantined_not_fatal(self):
        path = inbox_mod.inbox_path(self.cfg, ROOM)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{это не json", encoding="utf-8")
        listing = inbox_mod.items(self.cfg, ROOM, "active")
        self.assertEqual(listing["items"], [])
        self.assertIn("повреждён", listing["warning"])
        copies = list(path.parent.glob("inbox.json.corrupt-*"))
        self.assertTrue(copies, "битый файл обязан быть скопирован")
        # запись после повреждения работает
        item = inbox_mod.add(self.cfg, ROOM, "после сбоя", "manual")
        self.assertTrue(item["id"])


class InboxProposal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_guess_kind(self):
        self.assertEqual(inbox_mod.guess_kind("каждую пятницу присылать обзор почты"), "duty")
        self.assertEqual(inbox_mod.guess_kind("почему договор не подписан?"), "question")
        self.assertEqual(inbox_mod.guess_kind("второй шаг — согласовать смету"), "step")
        self.assertEqual(inbox_mod.guess_kind("сохранить реквизиты банка"), "note")
        self.assertEqual(inbox_mod.guess_kind("подготовить письмо клиенту"), "delo")

    def test_propose_creates_waiting_proposal_and_manager_card(self):
        item = inbox_mod.add(self.cfg, ROOM, "Подготовить письмо клиенту по договору", "dictation")
        payload = inbox_mod.act(self.cfg, ROOM, item["id"], "propose")
        fresh = payload["item"]
        self.assertEqual(fresh["status"], "processing")
        self.assertEqual(fresh["kind"], "delo")
        self.assertIn("Дело «", fresh["proposal"]["body"])
        self.assertIn("Подготовить письмо клиенту", fresh["proposal"]["body"])
        self.assertEqual(fresh["proposal"]["state"], "waiting")
        self.assertEqual([a["label"] for a in fresh["proposal"]["actions"]],
                         ["Принять", "Изменить", "Отклонить"])
        self.assertEqual([a["label"] for a in fresh["actions"]],
                         ["Принять", "Изменить", "Отклонить", "Удалить"])
        listing = inbox_mod.items(self.cfg, ROOM, "active")
        self.assertEqual(len(listing["queue"]), 1)
        self.assertIn("Разобрать входящее", listing["queue"][0]["text"])
        self.assertEqual(listing["counts"]["proposals"], 1)

    def test_manual_kind_wins_over_guess(self):
        item = inbox_mod.add(self.cfg, ROOM, "каждую пятницу обзор почты", "manual")
        payload = inbox_mod.act(self.cfg, ROOM, item["id"], "to_delo")
        self.assertEqual(payload["item"]["proposal"]["kind"], "delo")
        self.assertEqual(payload["item"]["kind"], "delo")

    def test_accept_moves_to_draft_and_closes_card(self):
        item = inbox_mod.add(self.cfg, ROOM, "каждую пятницу присылать обзор почты", "manual")
        inbox_mod.act(self.cfg, ROOM, item["id"], "to_duty")
        payload = inbox_mod.act(self.cfg, ROOM, item["id"], "accept")
        fresh = payload["item"]
        self.assertEqual(fresh["status"], "processed")
        self.assertEqual(fresh["kind"], "duty")
        self.assertEqual(fresh["proposal"]["state"], "accepted")
        self.assertEqual(fresh["resolution"]["kind_title"], "Дежурство")
        self.assertEqual(payload["counts"]["processed"], 1)
        self.assertEqual(payload["counts"]["active"], 0)
        listing = inbox_mod.items(self.cfg, ROOM, "all")
        self.assertEqual(len(listing["queue"]), 0, "карточка руководителю закрывается при приёме")
        raw = json.loads((Path(self.tmp.name) / "state" / ROOM / "inbox.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["duties_drafts"]), 1)
        self.assertEqual(raw["duties_drafts"][0]["text"], "каждую пятницу присылать обзор почты")

    def test_accept_note_goes_to_notes(self):
        item = inbox_mod.add(self.cfg, ROOM, "сохранить реквизиты банка", "manual")
        inbox_mod.act(self.cfg, ROOM, item["id"], "to_note")
        inbox_mod.act(self.cfg, ROOM, item["id"], "accept")
        raw = json.loads((Path(self.tmp.name) / "state" / ROOM / "inbox.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["notes"]), 1)

    def test_reject_returns_to_work_without_dead_end(self):
        item = inbox_mod.add(self.cfg, ROOM, "подготовить письмо", "manual")
        inbox_mod.act(self.cfg, ROOM, item["id"], "propose")
        payload = inbox_mod.act(self.cfg, ROOM, item["id"], "reject", {"text": "не дело, а справка"})
        fresh = payload["item"]
        self.assertEqual(fresh["status"], "new")
        self.assertIsNone(fresh["kind"])
        self.assertEqual(fresh["proposal"]["state"], "rejected")
        self.assertEqual(fresh["note"], "не дело, а справка")
        self.assertIn("propose", [a["id"] for a in fresh["actions"]])

    def test_edit_changes_body_then_accept(self):
        item = inbox_mod.add(self.cfg, ROOM, "подготовить письмо", "manual")
        inbox_mod.act(self.cfg, ROOM, item["id"], "propose")
        payload = inbox_mod.act(self.cfg, ROOM, item["id"], "edit",
                                {"text": "Дело «Письмо». Что сделать: собрать и отправить.",
                                 "title": "Письмо клиенту"})
        self.assertEqual(payload["item"]["proposal"]["title"], "Письмо клиенту")
        self.assertEqual(payload["item"]["proposal"]["by"], "ваша правка")
        accepted = inbox_mod.act(self.cfg, ROOM, item["id"], "accept")
        self.assertEqual(accepted["item"]["resolution"]["title"], "Письмо клиенту")

    def test_double_accept_is_conflict_not_crash(self):
        item = inbox_mod.add(self.cfg, ROOM, "подготовить письмо", "manual")
        inbox_mod.act(self.cfg, ROOM, item["id"], "propose")
        inbox_mod.act(self.cfg, ROOM, item["id"], "accept")
        with self.assertRaises(inbox_mod.InboxError) as exc:
            inbox_mod.act(self.cfg, ROOM, item["id"], "accept")
        self.assertEqual(exc.exception.http, 409)

    def test_deleted_item_can_be_restored(self):
        item = inbox_mod.add(self.cfg, ROOM, "черновик", "manual")
        inbox_mod.act(self.cfg, ROOM, item["id"], "delete")
        restored = inbox_mod.act(self.cfg, ROOM, item["id"], "restore")
        self.assertEqual(restored["item"]["status"], "new")
        # у удалённой записи действие «Вернуть», а не тупик
        deleted = inbox_mod.act(self.cfg, ROOM, item["id"], "delete")
        self.assertEqual([a["id"] for a in deleted["item"]["actions"]], ["restore"])

    def test_every_waiting_item_has_action(self):
        """Правило «без тупиков»: у любой активной записи минимум одно действие."""
        for source in ("manual", "dictation"):
            inbox_mod.add(self.cfg, ROOM, "мысль %s" % source, source)
        inbox_mod.act(self.cfg, ROOM, inbox_mod.items(self.cfg, ROOM, "active")["items"][0]["id"],
                      "propose")
        for item in inbox_mod.items(self.cfg, ROOM, "all")["items"]:
            self.assertTrue(item["actions"], "тупик: у записи %s нет действий" % item["id"])
            self.assertNotEqual(item["text"], "")

    def test_summary_matches_list_counts(self):
        inbox_mod.add(self.cfg, ROOM, "раз", "manual")
        inbox_mod.add(self.cfg, ROOM, "два", "manual")
        self.assertEqual(inbox_mod.summary(self.cfg, ROOM)["active"], 2)


if __name__ == "__main__":
    unittest.main()
