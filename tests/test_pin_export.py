# -*- coding: utf-8 -*-
"""W20-U3 (P13/P14): закрепление запросов и экспорт свода одним действием.

Что проверяем (всё на старом коде КРАСНОЕ — полей и маршрута ещё нет):

  * `PATCH /api/requests/{id} {pinned}` ставит и снимает закрепление в реестре
    комнаты, не трогая карточку, текст и название; без `title` и без `pinned` —
    400 по-русски.
  * Закрепление видно во всех трёх представлениях: деталь запроса, список
    комнаты (`/api/rooms/{id}/requests`) и строки «Недавнего» в
    `/api/rooms/summary`; в списках закреплённый запрос идёт ПЕРВЫМ.
  * `POST /api/requests/{id}/export`:
      - `{format:"obsidian"}` кладёт заметку в зеркальную папку комнаты
        (в песочнице — `TEAMROOM_VAULT_DIR`) и возвращает путь, папку и имя;
      - `{format:"html"}` отдаёт автономный файл вложением (`Raw`);
      - `{format:"print"}` подтверждает формат (печать собирает браузер);
      - неизвестный формат — 400 со списком допустимых.
  * Печатный стиль существует: `web/css/request-export.css` содержит
    `@media print` и прячет боковую колонку, ленту и кнопки.

Данные — только вымышленные. Только песочница `tests.helpers.Sandbox`.

    python -m unittest tests.test_pin_export -v
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                    # noqa: E402
import server.export as export_mod              # noqa: E402
import server.registry as registry              # noqa: E402
from server.http_util import ApiError, Raw      # noqa: E402
from tests.helpers import Sandbox, SimAgents       # noqa: E402

ROOM = "cafe"
RID = "t_pin00001"
RID_OLD = "t_pin00002"

#: вымышленный запрос: кафе «Лето», поставщик «Ромашка» (структура как у настоящих)
DETAIL_TEXT = ("Посчитай, сколько нужно муки на 120 порций моти с клубникой для кафе «Лето», "
               "если поставщик «Ромашка» даёт муку мешками по 25 кг и сроки поставки две недели.")


def _detail(rid: str, title: str, at: int) -> dict:
    return {
        "id": rid, "room": ROOM, "title": title, "title_short": title,
        "status": "done", "created_at": at, "duration_s": 3600,
        "text": DETAIL_TEXT,
        "synthesis": {"state": "ready", "version": 2, "at": at + 600,
                      "text_md": "## Вывод\n\nНа 120 порций нужно 18 кг муки — берите два мешка.\n",
                      "author": "team-director", "legacy": False},
        "next_steps": [{"id": "ns_1", "text": "Заказать два мешка муки у «Ромашки»",
                        "owner": "team-cfo", "due": "2026-12-01", "state": "open"}],
        "open_questions": [{"id": "q_1", "text": "Берём клубнику у «Ромашки» или на рынке?",
                            "for": "mikhail", "answered": False}],
        "decisions": [{"id": "d_1", "text": "Заказывать муку раз в месяц",
                       "why": "дешевле доставка", "status": "accepted"}],
        "tree": [{"id": rid, "parent_ids": [], "title": title, "assignee": "team-director",
                  "role": "root", "status": "done", "duration_s": 3600}],
        "progress": {"done": 1, "total": 1, "percent": 100},
        "report": {"available": False, "kind": None, "path": None, "title": None},
        "warnings": [],
    }


class PinExportTests(unittest.TestCase):
    """Маршруты закрепления и экспорта на песочнице."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))
        cls.now = int(time.time())
        cls.sim = SimAgents(cls.sb)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self._reset_registry()
        self._reset_cards()

    def tearDown(self):
        # Подмена «детали запроса» живёт только внутри одного теста.
        self.app.__dict__.pop("_export_detail", None)

    # --- помощники ---------------------------------------------------------
    def _reset_cards(self) -> None:
        """Корневые карточки на ПЕСОЧНОЙ доске: без них список и «Недавнее» пусты.

        ``SimAgents.connect`` проверяет, что БД внутри песочницы, поэтому удалять
        и создавать карточки можно только там (живая доска недоступна физически).
        """
        con = self.sim.connect()
        try:
            con.execute("DELETE FROM task_links WHERE child_id IN (?, ?) OR parent_id IN (?, ?)",
                        (RID, RID_OLD, RID, RID_OLD))
            con.execute("DELETE FROM task_events WHERE task_id IN (?, ?)", (RID, RID_OLD))
            con.execute("DELETE FROM tasks WHERE id IN (?, ?)", (RID, RID_OLD))
            con.commit()
        finally:
            con.close()
        for rid, at in ((RID, self.now), (RID_OLD, self.now - 3600)):
            self.sim.card("Мука для моти" if rid == RID else "Клубника для моти",
                          "team-director", status="done", tenant="room:" + ROOM, card_id=rid)
            con = self.sim.connect()
            try:
                con.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (at, rid))
                con.commit()
            finally:
                con.close()

    def _reset_registry(self) -> None:
        path = registry.registry_path(self.cfg, ROOM)
        data = {"room": ROOM, "updated_at": 0, "requests": [
            {"root_id": RID, "slug": RID, "title": "Мука для моти", "text": DETAIL_TEXT,
             "created_at": self.now, "pinned": False, "pinned_at": 0},
            {"root_id": RID_OLD, "slug": RID_OLD, "title": "Клубника для моти",
             "text": DETAIL_TEXT + " Подскажи, где брать клубнику в декабре.",
             "created_at": self.now - 3600, "pinned": False, "pinned_at": 0},
        ]}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def dispatch(self, method: str, path: str, body=None, query=None):
        try:
            return self.app.dispatch(method, path, query or {}, body or {})
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def patch(self, body: dict, rid: str = RID):
        return self.dispatch("PATCH", "/api/requests/" + rid, body)

    def rec(self, rid: str = RID) -> dict:
        return registry.get(self.cfg, ROOM, rid) or {}

    # --- закрепление -------------------------------------------------------
    def test_pin_sets_and_clears_flag(self):
        status, body, _ = self.patch({"pinned": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["pinned"])
        self.assertEqual(body["action"], "pin")
        self.assertTrue(self.rec()["pinned"], "флаг в реестре комнаты")
        self.assertGreater(self.rec()["pinned_at"], 0, "время закрепления записано")

        status, body, _ = self.patch({"pinned": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["pinned"])
        self.assertEqual(body["action"], "unpin")
        self.assertFalse(self.rec()["pinned"])
        self.assertEqual(self.rec()["pinned_at"], 0)

    def test_pin_keeps_text_and_title(self):
        before = dict(self.rec())
        self.patch({"pinned": True})
        after = self.rec()
        self.assertEqual(after["text"], before["text"], "текст запроса не меняется")
        self.assertEqual(after["title"], before["title"], "название не меняется")
        self.assertEqual(after["created_at"], before["created_at"])

    def test_pin_needs_a_field(self):
        status, body, _ = self.patch({})
        self.assertEqual(status, 400)
        self.assertIn("pinned", str(body))

    def test_pin_rejects_wrong_type(self):
        status, body, _ = self.patch({"pinned": {"x": 1}})
        self.assertEqual(status, 400)
        self.assertIn("pinned", str(body))

    def test_rename_contract_still_works(self):
        """Старый контракт PATCH {title} не сломан закреплением."""
        status, body, _ = self.patch({"title": "Мука для моти — итог"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "rename")
        self.assertEqual(body["request"]["title_short"], "Мука для моти — итог")

    def test_pin_visible_in_detail(self):
        self.patch({"pinned": True})
        status, body, _ = self.dispatch("GET", "/api/requests/" + RID)
        self.assertEqual(status, 200, str(body)[:300])
        self.assertTrue(body.get("pinned"), "закрепление видно на странице запроса")
        self.assertTrue(body.get("pinned_at"))

    def test_pinned_goes_first_in_room_list(self):
        self.patch({"pinned": True}, RID_OLD)          # старый по времени — вверх
        status, body, _ = self.dispatch("GET", "/api/rooms/%s/requests" % ROOM,
                                        query={"limit": ["10"]})
        self.assertEqual(status, 200, str(body)[:300])
        ids = [item.get("id") for item in body.get("items") or []]
        self.assertEqual(ids[0], RID_OLD, "закреплённый запрос — первым в списке")
        self.assertTrue((body["items"][0] or {}).get("pinned"))
        self.assertIn(RID, ids)

    def test_pinned_goes_first_in_recent(self):
        self.patch({"pinned": True}, RID_OLD)
        status, body, _ = self.dispatch("GET", "/api/rooms/summary",
                                        query={"requests": ["10"]})
        self.assertEqual(status, 200, str(body)[:300])
        rooms = body.get("rooms") or []
        room = next((r for r in rooms if str(r.get("id")) == ROOM), None)
        self.assertIsNotNone(room, "комната в сводке")
        rows = [row for row in (room.get("last") or []) if str(row.get("id")) in (RID, RID_OLD)]
        self.assertTrue(rows, "строки запросов в «Недавнем»")
        self.assertEqual(str(rows[0].get("id")), RID_OLD, "закреплённый — сверху")
        self.assertTrue(rows[0].get("pinned"))
        self.assertFalse(next(r for r in rows if str(r.get("id")) == RID).get("pinned"))

    # --- экспорт -----------------------------------------------------------
    def test_export_needs_format(self):
        status, body, _ = self.dispatch("POST", "/api/requests/%s/export" % RID, {})
        self.assertEqual(status, 400)
        self.assertIn("format", str(body))

    def test_export_unknown_format(self):
        status, body, _ = self.dispatch("POST", "/api/requests/%s/export" % RID,
                                        {"format": "pdf-ish"})
        self.assertEqual(status, 400)
        self.assertIn("obsidian", str(body), "в ошибке перечислены допустимые форматы")

    def test_export_print_is_accepted(self):
        status, body, _ = self.dispatch("POST", "/api/requests/%s/export" % RID, {"format": "print"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("hint"), "print_css")

    def test_export_html_is_a_download(self):
        detail = _detail(RID, "Мука для моти", self.now)
        self.app._export_detail = lambda rid: (detail, self.rec() or {"slug": rid})  # noqa: SLF001
        status, payload, headers = self.dispatch("POST", "/api/requests/%s/export" % RID,
                                                 {"format": "html"})
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, Raw)
        self.assertIn("text/html", payload.ctype)
        self.assertIn("attachment", payload.headers.get("Content-Disposition", ""))
        html_text = payload.body.decode("utf-8")
        self.assertIn("Мука для моти", html_text)
        self.assertIn("18 кг муки", html_text, "в файле есть текст свода")
        self.assertNotRegex(html_text, r'src="https?://', "никаких внешних ссылок в файле")

    def test_export_obsidian_writes_mirror_note(self):
        detail = _detail(RID, "Мука для моти", self.now)
        res = export_mod.export_request_obsidian(self.cfg, detail)
        self.assertTrue(res["ok"], res)
        path = Path(res["path"])
        self.assertTrue(path.is_file(), "файл заметки создан")
        vault = Path(self.cfg.vault_dir).resolve()
        self.assertTrue(str(path.resolve()).startswith(str(vault)), "только внутри хранилища")
        self.assertIn("Запросы", str(path.parent))
        text = path.read_text(encoding="utf-8")
        self.assertIn("Мука для моти", text)
        self.assertIn(RID, text)
        # повторный экспорт обновляет ту же заметку, а не плодит новую
        again = export_mod.export_request_obsidian(self.cfg, detail)
        self.assertEqual(again["path"], res["path"])
        same = sorted(p.name for p in path.parent.glob("*.md"))
        self.assertEqual(same.count(path.name), 1)

    def test_export_obsidian_route_returns_path(self):
        detail = _detail(RID, "Мука для моти", self.now)
        self.app._export_detail = lambda rid: (detail, self.rec() or {"slug": rid})  # noqa: SLF001
        status, body, _ = self.dispatch("POST", "/api/requests/%s/export" % RID,
                                        {"format": "obsidian"})
        self.assertEqual(status, 200, str(body)[:400])
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(body.get("format"), "obsidian")
        self.assertTrue(Path(body["path"]).is_file())
        self.assertTrue(body.get("name", "").endswith(".md"))


class PrintStyleTests(unittest.TestCase):
    """Печатный стиль: браузер печатает свод по `@media print`."""

    def setUp(self):
        self.css = (ROOT / "web" / "css" / "request-export.css").read_text(encoding="utf-8")

    def test_media_print_present(self):
        self.assertIn("@media print", self.css)

    def test_prints_only_the_summary(self):
        block = self.css.split("@media print", 1)[1]
        for selector in (".req__aside", ".req-thread", ".req-worklog", ".req-menu"):
            self.assertIn(selector, block, "в печати скрыт " + selector)
        self.assertIn("req-answer__foot", block)


if __name__ == "__main__":
    unittest.main()
