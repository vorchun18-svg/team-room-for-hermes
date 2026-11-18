# -*- coding: utf-8 -*-
"""W20-W1: «Ход работы» — чтение сеансов, человеческий текст, курсор, маскирование.

Что проверяется (красное на старом коде — модуля `server.worklog` не было):

* настройки комнаты `worklog_mode` (full/brief/off) и `digest_every_min` (0/1/2/5):
  умолчания, проверка значений, старые комнаты без полей;
* лента: рассуждения → «думает: …», вызовы инструментов → «читает файл …» /
  «ищет в сети …» / «ставит задачу …», результаты — «готово» / «ошибка: …»;
  записи `compacted`/`observed` не показываются, длинное рассуждение обрезается;
* курсор: постранично ничего не теряется и не дублируется, пустой опрос пуст,
  «показать раньше» (`before`) отдаёт ровно предыдущую страницу, новые записи видны;
* маскирование: `sk-…`, `ghp_…`, `password=…`, `Bearer …`, base64 и длинные hex —
  в тексте их нет; полные пути укорачиваются;
* режимы: `brief` — без рассуждений и без текстов реплик; `off` — не читает БД вовсе
  (в тесте `state.db` — мусорный файл, и это не ошибка);
* отсутствие `state.db`/`kanban.db` — пустая лента и 200, а не падение;
* сторож: дайджест раз в N минут, без дублей, после завершения убирается; «возможно
  завис» — карточка без активности дольше 15 мин;
* файлы БД после чтения не изменились (только чтение).

    python -m unittest tests.test_worklog -v
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config as config_mod            # noqa: E402
from server import rooms as rooms_mod              # noqa: E402
from server import worklog as wl                   # noqa: E402
from tools.worklog_fixture_db import (             # noqa: E402
    LEADER, ROOT_CARD, WORKER, WORKER_CARD, build, create_state_db)


def _tmp_base() -> Path:
    """Каталог песочницы: `TMPDIR` (у Hermes — его scratch), иначе системный temp."""
    base = os.environ.get("TMPDIR") or os.environ.get("TEAMROOM_TEST_TMP") or tempfile.gettempdir()
    return Path(base)


class Fixture(unittest.TestCase):
    """Общая песочница: вымышленная `state.db` + мини-доска во временной папке."""

    stuck = False

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="worklog-", dir=str(_tmp_base())))
        made = build(cls.root, stuck=cls.stuck)
        cfg = config_mod.load()
        cls.cfg = dataclasses.replace(cfg, hermes_home=Path(made["hermes_home"]),
                                      kanban_home=Path(made["kanban_home"]),
                                      data_dir=cls.root / "data")
        cls.cfg.ensure_dirs()
        cls.room = {"id": "demo", "board": "demo", "orchestrator": LEADER, "critic": None,
                    "members": [{"profile": WORKER, "label": "Анна — юрист"}],
                    "role_labels": {LEADER: "Лев — руководитель"}}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def full(self, **kwargs):
        return wl.worklog(self.cfg, self.room, ROOT_CARD, limit=200, **kwargs)


class TestRoomSettings(unittest.TestCase):
    """Настройки комнаты: умолчания, проверка, старые записи."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="worklog-rooms-", dir=str(_tmp_base())))
        cfg = config_mod.load()
        self.cfg = dataclasses.replace(cfg, data_dir=self.root / "data",
                                       hermes_home=self.root / "hermes",
                                       kanban_home=self.root / "kanban")
        self.cfg.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _new_room(self, **extra):
        data = {"title": "Кафе «Лето»", "orchestrator": "lev",
                "members": [{"profile": "anna"}]}
        data.update(extra)
        return rooms_mod.create_room(self.cfg, data)

    def test_defaults_full_and_two_minutes(self):
        room = self._new_room()
        self.assertEqual(room["worklog_mode"], "full")
        self.assertEqual(room["digest_every_min"], 2)
        fresh = rooms_mod.get_room(self.cfg, room["id"])
        self.assertEqual(fresh["worklog"]["mode_label"], "Подробно (с рассуждениями)")
        self.assertEqual(fresh["worklog"]["digest_label"], "Каждые 2 минуты")
        self.assertTrue(fresh["worklog"]["on"])

    def test_old_room_without_fields_reads_as_defaults(self):
        room = self._new_room()
        # имитируем запись 1.0.4: полей нет
        path = Path(self.cfg.data_dir) / "rooms.json"
        import json
        data = json.loads(path.read_text("utf-8"))
        for item in data["rooms"]:
            item.pop("worklog_mode", None)
            item.pop("digest_every_min", None)
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        old = rooms_mod.get_room(self.cfg, room["id"])
        self.assertEqual(old["worklog_mode"], "full")
        self.assertEqual(old["digest_every_min"], 2)

    def test_patch_accepts_valid_values(self):
        room = self._new_room()
        patched = rooms_mod.update_room(self.cfg, room["id"],
                                        {"worklog_mode": "brief", "digest_every_min": 5})
        self.assertEqual(patched["worklog_mode"], "brief")
        self.assertEqual(patched["digest_every_min"], 5)
        off = rooms_mod.update_room(self.cfg, room["id"],
                                    {"worklog_mode": "off", "digest_every_min": 0})
        self.assertEqual(off["worklog"]["mode_label"], "Выключено")
        self.assertFalse(off["worklog"]["on"])
        self.assertEqual(off["worklog"]["digest_label"], "Выключено")

    def test_patch_rejects_unknown_values(self):
        room = self._new_room()
        for bad in ({"worklog_mode": "verbose"}, {"digest_every_min": 3},
                    {"digest_every_min": "2"}, {"digest_every_min": True}):
            with self.assertRaises(rooms_mod.RoomsError) as ctx:
                rooms_mod.update_room(self.cfg, room["id"], bad)
            self.assertEqual(ctx.exception.code, "bad_request")

    def test_room_settings_level(self):
        full = wl.room_settings({"worklog_mode": "full", "digest_every_min": 2})
        self.assertEqual((full["mode"], full["level"], full["digest_every_min"]),
                         ("full", "thoughts", 2))
        brief = wl.room_settings({"worklog_mode": "brief"})
        self.assertEqual((brief["mode"], brief["level"]), ("brief", "steps"))
        garbage = wl.room_settings({"worklog_mode": "?", "digest_every_min": 99})
        self.assertEqual((garbage["mode"], garbage["digest_every_min"]), ("full", 2))


class TestHumanText(unittest.TestCase):
    """Перевод событий в человеческий русский — без id, дампов и секретов."""

    def test_tool_phrases(self):
        self.assertEqual(wl.human_tool("read_file", {"path": "C:\\Дела\\Лето\\Договор.md"}),
                         "читает файл «…\\Лето\\Договор.md»")
        self.assertEqual(wl.human_tool("web_search", {"query": "сроки поставки"}),
                         "ищет в сети «сроки поставки»")
        self.assertEqual(wl.human_tool("terminal", {"command": "type secret.txt"}), "запускает команду")
        self.assertEqual(wl.human_tool("kanban_create", {"title": "Проверить договор"}),
                         "ставит задачу: «Проверить договор»")
        self.assertIn("ищет в файлах", wl.human_tool("search_files", {"pattern": "неустойка"}))
        self.assertTrue(wl.human_tool("неведомый_инструмент", {}).startswith("работает"))

    def test_result_ok_and_error(self):
        text, ok = wl.result_text("Договор поставки: срок 14 дней.\nНеустойка 0,1%.")
        self.assertTrue(ok)
        self.assertTrue(text.startswith("готово"))
        text, ok = wl.result_text("Error: файл config.yaml не найден")
        self.assertFalse(ok)
        self.assertEqual(text, "ошибка: файл config.yaml не найден")
        self.assertEqual(wl.result_text("")[0], "готово")

    def test_masking(self):
        for secret in ("sk-test0000000000000",
                       "ghp_" + "0" * 40,
                       "password=hunter2",
                       "Bearer abcdefghijklmnop",
                       "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+P0A="):
            out = wl.mask("доступ " + secret + " конец")
            self.assertNotIn(secret, out, msg=secret[:24])
            self.assertIn("[скрыто]", out)
        self.assertEqual(wl.mask("C:\\Дела\\Лето\\Договор.md"), "…\\Лето\\Договор.md")

    def test_masking_catches_keys_from_agent_reasoning(self):
        """Рассуждения агента содержат не только `sk-…`: ключи AWS, пароль в адресе БД."""
        aws = "AKIA" + "IOSFODNN7EXAMPLE"
        dsn = "postgres://user:" + "s3cr3tPass" + "@db.local:5432/app"
        bot = "1234567890:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
        for secret, text in ((aws, "ключ " + aws), (dsn, "строка " + dsn), (bot, "бот " + bot)):
            out = wl.mask(text)
            self.assertNotIn(secret, out, msg=secret[:12])
            self.assertIn("[скрыто]", out, msg=secret[:12])

    def test_trim_marks_more(self):
        long = "слово " * 200
        out = wl.trim(long, 100)
        self.assertLessEqual(len(out), 110)
        self.assertTrue(out.endswith("… ещё"))

    def test_tool_calls_formats(self):
        rows = wl.tool_calls('[{"function": {"name": "read_file", "arguments": "{\\"path\\": \\"a.md\\"}"}}]')
        self.assertEqual(rows[0][0], "read_file")
        self.assertEqual(rows[0][1]["path"], "a.md")
        self.assertEqual(wl.tool_calls("не json"), [])
        self.assertEqual(wl.tool_calls(None), [])


class TestFeed(Fixture):
    """Лента: перевод сообщений, режимы, исключения, отсутствие БД."""

    def test_items_are_human_and_ordered(self):
        data = self.full()
        self.assertEqual(data["mode"], "full")
        self.assertEqual(data["level"], "thoughts")
        self.assertTrue(data["items"])
        kinds = {item["kind"] for item in data["items"]}
        self.assertTrue({"thought", "action", "result", "message", "status", "handoff"} <= kinds)
        stamps = [item["ts"] for item in data["items"]]
        self.assertEqual(stamps, sorted(stamps))
        joined = " ".join(item["text"] for item in data["items"])
        self.assertIn("думает:", joined)
        self.assertIn("читает файл", joined)
        self.assertIn("запускает команду", joined)
        self.assertIn("Лев — руководитель → Анна — юрист", joined)
        self.assertIn("ошибка: файл config.yaml не найден", joined)
        # технику не показываем
        for item in data["items"]:
            self.assertNotIn("s_0000", item["text"])
            self.assertNotIn("t_1a2b", item["text"])
            self.assertNotIn("sk-", item["text"])

    def test_thought_is_trimmed(self):
        long = [i for i in self.full()["items"] if i["kind"] == "thought"
                and len(i["text"]) > 250]
        self.assertTrue(long, "длинное рассуждение должно быть в фикстуре")
        self.assertTrue(all(len(i["text"]) <= wl.MAX_THOUGHT + 40 for i in long))

    def test_compacted_and_observed_excluded(self):
        joined = " ".join(i["text"] for i in self.full()["items"])
        self.assertNotIn("СЖАТАЯ ЗАПИСЬ", joined)
        self.assertNotIn("наблюдено", joined)

    def test_result_error_flagged(self):
        errors = [i for i in self.full()["items"] if i["kind"] == "result" and i["ok"] is False]
        self.assertEqual(len(errors), 1)
        self.assertIn("config.yaml", errors[0]["text"])

    def test_cursor_pages_without_loss_or_duplicates(self):
        everything = [item["id"] for item in self.full()["items"]]
        cursor = wl.encode_cursor({"t": 0, "s": "", "r": 0, "i": ""})
        seen: list = []
        for _page in range(50):
            page = wl.worklog(self.cfg, self.room, ROOT_CARD, since=cursor, limit=3)
            seen += [item["id"] for item in page["items"]]
            if not page["items"] or page["cursor"] == cursor:
                break
            cursor = page["cursor"]
            if not page["more"]:
                break
        self.assertEqual(set(seen), set(everything))
        self.assertEqual(len(seen), len(set(seen)), "дубли на границах страниц")

    def test_idle_poll_is_empty(self):
        first = self.full()
        idle = wl.worklog(self.cfg, self.room, ROOT_CARD, since=first["cursor"])
        self.assertEqual(idle["items"], [])
        self.assertFalse(idle["more"])
        self.assertEqual(idle["cursor"], first["cursor"])

    def test_before_returns_previous_page(self):
        first = self.full()
        back = wl.worklog(self.cfg, self.room, ROOT_CARD, before=first["cursor"], limit=4)
        self.assertEqual([i["id"] for i in back["items"]],
                         [i["id"] for i in first["items"]][-5:-1])

    def test_first_page_is_the_latest_events(self):
        """W21-L1: первый ответ — ПОСЛЕДНИЕ N записей (лента открывается на свежем)."""
        data = wl.worklog(self.cfg, self.room, ROOT_CARD, limit=5)
        everything = [item["id"] for item in self.full()["items"]]
        self.assertEqual([item["id"] for item in data["items"]], everything[-5:])
        self.assertTrue(data["more"])
        self.assertTrue(data["before"], "курсор «раньше» нужен, пока есть история")

    def test_before_chain_walks_back_without_loss_or_duplicates(self):
        """Курсор `before` доводит до начала: без повторов, без пропусков, с концом."""
        everything = [item["id"] for item in self.full()["items"]]
        page = wl.worklog(self.cfg, self.room, ROOT_CARD, limit=6)
        seen = [item["id"] for item in page["items"]]
        oldest_ts = page["items"][0]["ts"] if page["items"] else 0
        cursor = page["before"]
        pages = 1
        while cursor and pages < 80:
            page = wl.worklog(self.cfg, self.room, ROOT_CARD, before=cursor, limit=6)
            ids = [item["id"] for item in page["items"]]
            self.assertTrue(ids, "страница «раньше» пуста, хотя курсор ещё есть")
            # страница идёт СТРОГО раньше показанного — позиция чтения не прыгает
            self.assertLessEqual(page["items"][-1]["ts"], oldest_ts)
            oldest_ts = page["items"][0]["ts"]
            seen = ids + seen
            cursor = page["before"]
            pages += 1
        self.assertEqual(len(seen), len(set(seen)), "дубли на стыке страниц")
        self.assertEqual(seen, everything, "история должна дойти до начала")

    def test_before_reports_end_of_history(self):
        everything = [item["id"] for item in self.full()["items"]]
        whole = wl.worklog(self.cfg, self.room, ROOT_CARD, limit=len(everything))
        self.assertEqual([item["id"] for item in whole["items"]], everything)
        self.assertFalse(whole["more"])
        self.assertEqual(whole["before"], "", "история кончилась — курсора нет")

    # ---- W21-REV2: мусор в `before` не должен молча выключать «Показать раньше» ---
    def _latest(self):
        return wl.worklog(self.cfg, self.room, ROOT_CARD, limit=5)

    def test_garbage_before_falls_back_to_the_latest_page(self):
        normal = self._latest()
        for junk in ("abc", "w1.", "w1.!!!", "w1." + "A" * 40, "  ", "\x00w1.e30"):
            page = wl.worklog(self.cfg, self.room, ROOT_CARD, before=junk, limit=5)
            self.assertEqual([i["id"] for i in page["items"]],
                             [i["id"] for i in normal["items"]],
                             "мусор в before: показываем свежее, а не пустую страницу (%r)" % junk)
            self.assertTrue(page["more"], "история есть — «Показать раньше» должно работать")
            self.assertTrue(page["before"], "курсор следующей страницы обязан остаться")

    def test_huge_before_is_ignored_not_decoded(self):
        huge = "w1." + "A" * (wl.MAX_CURSOR_CHARS + 10)
        page = wl.worklog(self.cfg, self.room, ROOT_CARD, before=huge, limit=5)
        self.assertEqual([i["id"] for i in page["items"]], [i["id"] for i in self._latest()["items"]])
        self.assertEqual(wl.page_cursor(None), None)
        self.assertEqual(wl.page_cursor("abc"), None)

    def test_valid_before_still_pages_back(self):
        first = self._latest()
        page = wl.worklog(self.cfg, self.room, ROOT_CARD, before=first["before"], limit=5)
        self.assertTrue(page["items"])
        self.assertEqual(wl.page_cursor(first["before"])["t"], wl.decode_cursor(first["before"])["t"])

    def test_brief_hides_thoughts_and_messages(self):
        room = dict(self.room, worklog_mode="brief")
        data = wl.worklog(self.cfg, room, ROOT_CARD, level="thoughts", limit=200)
        self.assertEqual(data["mode"], "brief")
        self.assertEqual(data["level"], "steps", "«Кратко» не показывает рассуждения")
        kinds = {item["kind"] for item in data["items"]}
        self.assertNotIn("thought", kinds)
        self.assertNotIn("message", kinds)
        self.assertIn("action", kinds)
        self.assertIn("status", kinds)

    def test_steps_level_hides_thoughts(self):
        data = wl.worklog(self.cfg, self.room, ROOT_CARD, level="steps", limit=200)
        self.assertEqual(data["level"], "steps")
        self.assertNotIn("thought", {i["kind"] for i in data["items"]})
        self.assertIn("action", {i["kind"] for i in data["items"]})

    def test_bad_level_and_limit(self):
        with self.assertRaises(wl.WorklogError) as ctx:
            wl.worklog(self.cfg, self.room, ROOT_CARD, level="all")
        self.assertEqual(ctx.exception.code, "bad_request")
        for bad in (0, 201, -5, "abc"):
            with self.assertRaises(wl.WorklogError):
                wl.worklog(self.cfg, self.room, ROOT_CARD, limit=bad)

    def test_off_mode_reads_nothing(self):
        broken = Path(tempfile.mkdtemp(prefix="worklog-broken-", dir=str(_tmp_base())))
        try:
            home = broken / "hermes"
            state = home / "profiles" / WORKER / "state.db"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("это не база данных, а мусор", "utf-8")   # открыть нельзя
            cfg = dataclasses.replace(self.cfg, hermes_home=home,
                                      data_dir=broken / "data")
            cfg.ensure_dirs()
            room = dict(self.room, worklog_mode="off")
            data = wl.worklog(cfg, room, ROOT_CARD)
            self.assertEqual(data["mode"], "off")
            self.assertEqual(data["items"], [])
            self.assertEqual(data["live"], [])
            self.assertIsNone(data["digest"])
        finally:
            shutil.rmtree(broken, ignore_errors=True)

    def test_missing_databases_is_not_error(self):
        empty = Path(tempfile.mkdtemp(prefix="worklog-empty-", dir=str(_tmp_base())))
        try:
            cfg = dataclasses.replace(self.cfg, hermes_home=empty / "hermes",
                                      kanban_home=empty / "kanban",
                                      data_dir=empty / "data")
            cfg.ensure_dirs()
            data = wl.worklog(cfg, self.room, ROOT_CARD)
            self.assertEqual(data["items"], [])
            self.assertEqual(data["live"], [])
            self.assertTrue(data["warnings"])
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_read_only(self):
        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        state = Path(self.cfg.hermes_home) / "profiles" / WORKER / "state.db"
        board = Path(self.cfg.kanban_db("demo"))
        before = (digest(state), digest(board), state.stat().st_mtime_ns, board.stat().st_mtime_ns)
        self.full()
        wl.live(self.cfg, self.room, ROOT_CARD)
        after = (digest(state), digest(board), state.stat().st_mtime_ns, board.stat().st_mtime_ns)
        self.assertEqual(before, after, "чтение хода работы не должно менять БД")

    def test_new_message_appears_incrementally(self):
        first = self.full()
        state = Path(self.cfg.hermes_home) / "profiles" / WORKER / "state.db"
        con = sqlite3.connect(str(state))
        try:
            con.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                        "VALUES (?,?,?,?)", ("s_0000bb22", "assistant",
                                             "Уточнение: второй поставщик дороже на 4%.", time.time() + 9))
            con.commit()
        finally:
            con.close()
        fresh = wl.worklog(self.cfg, self.room, ROOT_CARD, since=first["cursor"])
        texts = [i["text"] for i in fresh["items"]]
        self.assertTrue(any("второй поставщик дороже" in text for text in texts), texts)


class TestDigest(Fixture):
    """Сторож: промежуточный итог без модели и пометка «возможно завис»."""

    def _detail(self, status="working"):
        return {"status": status, "created_at": int(time.time()) - 600,
                "tree": [{"id": ROOT_CARD, "assignee": LEADER, "status": "running",
                          "title": "Сравнить поставщиков для кафе «Лето»"},
                         {"id": WORKER_CARD, "assignee": WORKER, "status": "running",
                          "title": "Проверить договор с «Ромашкой»"}]}

    def test_digest_written_once_per_interval(self):
        record = {"root_id": ROOT_CARD}
        first = wl.digest_step(self.cfg, self.room, record, self._detail())
        self.assertTrue(first["digest"], first.get("skipped"))
        text = first["text"]
        self.assertIn("Прошло", text)
        self.assertIn("Работают:", text)
        self.assertIn("Готово 0 из 2", text)
        # сразу же — не дублируем
        again = wl.digest_step(self.cfg, self.room, record, self._detail())
        self.assertFalse(again["digest"])
        self.assertIn(again["skipped"], ("нечего добавить", "рано"))
        # через интервал — новый итог
        later = wl.digest_step(self.cfg, self.room, record, self._detail(),
                               now=int(time.time()) + 3 * 60)
        self.assertTrue(later["digest"], later.get("skipped"))
        # итог виден в ленте и в поле digest
        data = self.full()
        self.assertIsNotNone(data["digest"])
        self.assertIn("digest", {item["kind"] for item in data["items"]})

    def test_digest_stops_after_finish(self):
        record = {"root_id": ROOT_CARD}
        wl.digest_step(self.cfg, self.room, record, self._detail())
        self.assertIsNotNone(wl.digest_for(self.cfg, "demo", ROOT_CARD))
        done = wl.digest_step(self.cfg, self.room, record, self._detail("done"))
        self.assertFalse(done["digest"])
        self.assertIsNone(wl.digest_for(self.cfg, "demo", ROOT_CARD))

    def test_digest_disabled(self):
        room = dict(self.room, digest_every_min=0)
        res = wl.digest_step(self.cfg, room, {"root_id": ROOT_CARD}, self._detail())
        self.assertFalse(res["digest"])
        self.assertEqual(res["skipped"], "выключено")


class TestStuck(Fixture):
    """Карточка без активности дольше 15 минут — «возможно завис»."""

    stuck = True

    def test_stuck_card_detected(self):
        data = self.full()
        self.assertTrue(data["stuck"], data["live"])
        first = data["stuck"][0]
        self.assertIn("возможно завис", first["text"])
        self.assertTrue(any(row["state"] == "stuck" for row in data["live"]))
        self.assertTrue(wl.stuck_cards(self.cfg, self.room, ROOT_CARD))


if __name__ == "__main__":
    unittest.main(verbosity=2)
