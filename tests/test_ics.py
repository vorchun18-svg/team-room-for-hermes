"""Тесты .ics: RFC 5545, фолдинг UTF-8, экранирование, стабильный UID.

В тесте свой мини-парсер (unfold → VEVENT → unescape) — независимая проверка
того, что календарь прочитает обычный клиент.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, ics  # noqa: E402


# --- мини-парсер -------------------------------------------------------------
def unfold(text: str) -> list:
    """Физические строки → логические (CRLF + продолжение с пробела)."""
    assert "\r\n" in text or text == "", "нет CRLF"
    raw = text[:-2].split("\r\n") if text.endswith("\r\n") else text.split("\r\n")
    lines = []
    for line in raw:
        if line.startswith(" ") or line.startswith("\t"):
            if not lines:
                raise AssertionError("продолжение без начала строки")
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_calendar(text: str) -> dict:
    lines = unfold(text)
    if lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise AssertionError("нет обёртки VCALENDAR")
    root = {}
    events = []
    stack = [root]
    for line in lines[1:-1]:
        if line == "BEGIN:VEVENT":
            ev = {}
            events.append(ev)
            stack.append(ev)
            continue
        if line == "END:VEVENT":
            stack.pop()
            continue
        if ":" not in line:
            raise AssertionError("строка без ':' — %r" % line)
        key, value = line.split(":", 1)
        stack[-1].setdefault(key, []).append(value)
    return {"props": root, "events": events}


def one(ev: dict, key: str) -> str:
    values = ev.get(key) or []
    if not values:
        return ""
    return ics.unescape_text(values[0])


class IcsTest(unittest.TestCase):
    ITEMS = [
        {"id": "t_a1b2c3d4:ns_1", "room": "cafe", "text": "Согласовать цену 220 ₽",
         "owner": "mikhail", "due": "2026-11-05", "status": "open", "kind": "next_step",
         "source_request": "t_a1b2c3d4", "source_title": "Юнит-экономика"},
        {"id": "t_a1b2c3d4:ns_2", "room": "cafe", "text": "Без срока", "owner": "biz-cfo",
         "due": None, "status": "open"},
        {"id": "t_a1b2c3d4:ns_3", "room": "cafe", "text": "Кривая дата", "due": "завтра"},
    ]

    def test_crlf_only(self):
        text = ics.make_ics(self.ITEMS, "Лето")
        self.assertTrue(text.endswith("\r\n"))
        for i, ch in enumerate(text):
            if ch == "\n":
                self.assertEqual(text[i - 1], "\r", "найден одиночный LF")
        self.assertNotIn("\r\r", text)

    def test_lines_fit_75_octets(self):
        long_text = "Очень длинный шаг по легализации компании в России " * 4
        items = [{"id": "x:1", "room": "cafe", "text": long_text, "due": "2026-12-01",
                  "owner": "biz-lawyer", "source_title": "Длинный заголовок " * 5}]
        text = ics.make_ics(items, "Рабочее пространство — очень длинное название комнаты, с запятой")
        for line in text[:-2].split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, repr(line))

    def test_fold_does_not_split_multibyte(self):
        line = "SUMMARY:" + "я" * 120
        parts = ics.fold_line(line)
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-8")), 75)
            part.encode("utf-8").decode("utf-8")  # не рвётся на середине символа
        unfolded = parts[0] + "".join(p[1:] for p in parts[1:])
        self.assertEqual(unfolded, line)
        self.assertFalse(parts[0].startswith(" "))
        for part in parts[1:]:
            self.assertTrue(part.startswith(" "))
            self.assertFalse(part.startswith("  "))

    def test_parse_and_escape(self):
        text = ics.make_ics(self.ITEMS, "Лето, demo")
        cal = parse_calendar(text)
        self.assertEqual(len(cal["events"]), 1, "событие без срока не должно попадать")
        ev = cal["events"][0]
        self.assertEqual(one(ev, "SUMMARY"), "Согласовать цену 220 ₽")
        self.assertEqual(one(ev, "DTSTART;VALUE=DATE"), "20261105")
        self.assertEqual(one(ev, "DTEND;VALUE=DATE"), "20261106")
        self.assertIn("Кто: Михаил", one(ev, "DESCRIPTION"))
        self.assertIn("Статус: открыт", one(ev, "DESCRIPTION"))
        self.assertIn("Запрос: t_a1b2c3d4", one(ev, "DESCRIPTION"))

    def test_special_chars_escaped(self):
        item = {"id": "x:9", "room": "cafe", "due": "2026-11-07",
                "text": "Купить; муку, сахар\\соль"}
        text = ics.make_ics([item], "Лето; test, room")
        self.assertIn("SUMMARY:Купить\\; муку\\, сахар\\\\соль", text)
        cal = parse_calendar(text)
        self.assertEqual(one(cal["events"][0], "SUMMARY"), "Купить; муку, сахар\\соль")
        self.assertEqual(one(cal["props"], "X-WR-CALNAME"), "Лето; test, room")
        self.assertEqual(ics.unescape_text("a\\;b\\,c\\\\d\\ne"), "a;b,c\\d\ne")
        # перевод строки в тексте шага не рвёт ICS: SUMMARY остаётся однострочным,
        # а перевод строки в служебном поле экранируется как \n
        multi = ics.make_ics([{"id": "m:1", "due": "2026-11-07", "text": "раз\nдва"}], "Лето")
        self.assertEqual(one(parse_calendar(multi)["events"][0], "SUMMARY"), "раз два")
        self.assertIn("\\n", ics.escape_text("раз\nдва"))
        desc = ics.make_ics([{"id": "m:2", "due": "2026-11-07", "text": "шаг",
                              "source_title": "первая\nвторая"}], "Лето")
        self.assertIn("DESCRIPTION:", desc)
        self.assertEqual(one(parse_calendar(desc)["events"][0], "DESCRIPTION"),
                         "Источник: первая\nвторая")

    def test_no_due_skipped_and_counted(self):
        text = ics.make_ics(self.ITEMS, "Лето")
        cal = parse_calendar(text)
        self.assertEqual(len(cal["events"]), 1)
        desc = one({"X-WR-CALDESC": cal["props"].get("X-WR-CALDESC", [])}, "X-WR-CALDESC")
        self.assertIn("Пропущено без срока: 2", desc)

    def test_calendar_meta(self):
        text = ics.make_ics(self.ITEMS, "Лето (demo)")
        cal = parse_calendar(text)
        props = cal["props"]
        self.assertEqual(props["VERSION"], ["2.0"])
        self.assertTrue(props["PRODID"][0].startswith("-//"))
        self.assertEqual(one(props, "X-WR-CALNAME"), "Лето (demo)")
        self.assertEqual(props["CALSCALE"], ["GREGORIAN"])
        self.assertEqual(props["METHOD"], ["PUBLISH"])

    def test_uid_stable_and_unique(self):
        a = ics.make_ics(self.ITEMS[:1], "Лето")
        b = ics.make_ics(self.ITEMS[:1], "Лето")
        first = [l for l in unfold(a) if l.startswith("UID:")][0]
        second = [l for l in unfold(b) if l.startswith("UID:")][0]
        self.assertEqual(first, second)
        self.assertEqual(first, "UID:cafe-t_a1b2c3d4-ns_1@team-room.local")
        # другой id — другой UID; без room — без префикса
        other = ics.make_ics([{**self.ITEMS[0], "id": "t_deadbeef:ns_1"}], "Лето")
        self.assertNotEqual(first, [l for l in unfold(other) if l.startswith("UID:")][0])
        bare = ics.make_ics([{"id": "ns_1", "text": "x", "due": "2026-11-05"}], "Лето")
        self.assertIn("UID:ns_1@team-room.local", bare)

    def test_empty_calendar_still_valid(self):
        text = ics.make_ics([], "Пустая комната")
        cal = parse_calendar(text)
        self.assertEqual(cal["events"], [])
        self.assertIn("Событий: 0", one(cal["props"], "X-WR-CALDESC"))

    def test_summary_truncated_200(self):
        item = {"id": "x:1", "due": "2026-11-05", "text": "я" * 400}
        cal = parse_calendar(ics.make_ics([item], "Лето"))
        summary = one(cal["events"][0], "SUMMARY")
        self.assertEqual(len(summary), 200)
        self.assertTrue(summary.endswith("…"))

    def test_events_sorted_by_date(self):
        items = [{"id": "b:1", "due": "2026-12-01", "text": "второй"},
                 {"id": "a:1", "due": "2026-11-01", "text": "первый"}]
        text = ics.make_ics(items, "Лето")
        summaries = [l for l in unfold(text) if l.startswith("SUMMARY:")]
        self.assertEqual(summaries[0], "SUMMARY:первый")


class IcsForItemsTest(unittest.TestCase):
    BACKLOG = [
        {"id": "t_1:ns_1", "text": "Шаг со сроком", "due": "2026-11-05", "status": "open",
         "owner": "mikhail", "source_request": "t_1", "source_title": "Запрос"},
        {"id": "t_1:ns_2", "text": "Шаг без срока", "due": None, "status": "open"},
        {"id": "t_1:q_1", "text": "Закрытый вопрос", "due": "2026-11-09", "status": "done"},
    ]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tr-ics-"))
        self._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(self.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(self.tmp / "vault"),
        })
        self._env.start()
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self._backlog = mock.patch.object(ics, "_load_backlog", return_value=self.BACKLOG)
        self._title = mock.patch.object(ics, "_room_title", return_value="Лето")
        self._backlog.start()
        self._title.start()

    def tearDown(self):
        self._backlog.stop()
        self._title.stop()
        self._env.stop()

    def test_all_open(self):
        meta = {}
        text = ics.ics_for_items(self.cfg, "cafe", all_open=True, meta=meta)
        cal = parse_calendar(text)
        self.assertEqual(len(cal["events"]), 1)
        self.assertEqual(meta["count"], 1)
        self.assertEqual(meta["skipped"], 1)  # без срока
        self.assertEqual(meta["title"], "Лето")
        self.assertEqual(meta["selected"], 2)  # done не попал
        self.assertIn("UID:cafe-t_1-ns_1@team-room.local", text)

    def test_by_ids_and_short_ids(self):
        text = ics.ics_for_items(self.cfg, "cafe", ids=["t_1:ns_1"])
        self.assertEqual(len(parse_calendar(text)["events"]), 1)
        text = ics.ics_for_items(self.cfg, "cafe", ids=["ns_1", "q_1"])
        cal = parse_calendar(text)
        self.assertEqual(len(cal["events"]), 2)  # q_1 есть, хоть и done

    def test_missing_ids_reported(self):
        meta = {}
        text = ics.ics_for_items(self.cfg, "cafe", ids=["nope"], meta=meta)
        self.assertEqual(parse_calendar(text)["events"], [])
        self.assertEqual(meta["missing"], ["nope"])
        self.assertEqual(meta["count"], 0)

    def test_no_ids_no_all(self):
        text = ics.ics_for_items(self.cfg, "cafe")
        self.assertEqual(parse_calendar(text)["events"], [])

    def test_bad_dates_skipped(self):
        patch = mock.patch.object(ics, "_load_backlog", return_value=[
            {"id": "a", "text": "x", "due": "2026-13-45"},
            {"id": "b", "text": "y", "due": "2026-02-30"},
            {"id": "c", "text": "z", "due": "2026-11-05T10:00:00"}])
        with patch:
            meta = {}
            text = ics.ics_for_items(self.cfg, "cafe", all_open=True, meta=meta)
        self.assertEqual(len(parse_calendar(text)["events"]), 1)
        self.assertEqual(meta["count"], 1)
        self.assertEqual(meta["skipped"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
