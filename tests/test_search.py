"""Тесты поиска (server/search.py): ё/е, префиксы, сниппеты, ранжирование, кэш.

Данные — фейки (registry.list_requests / ingest.load_state подменяются),
реальные state/*.json и канбан не читаются.
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

from server import config, search  # noqa: E402

REQUESTS = {
    "cafe": [
        {"root_id": "t_a1b2c3d4", "slug": "r-20261028-0931-ab12", "title": "Легализация и ИНН",
         "text": "Как открыть компанию в России. Ёмкость рынка моти и цены.",
         "created_at": 1761700000},
        {"root_id": "t_beef0001", "slug": "r-20261028-1000-cd34", "title": "Упаковка и бренд",
         "text": "Дизайн коробки, Роспатент, пробная партия.", "created_at": 1761710000},
    ],
    "fn": [
        {"root_id": "t_fn000001", "slug": "r-20261028-1100-ef56", "title": "Регламент работы",
         "text": "Как оформлять решения и журнал комнаты.", "created_at": 1761720000},
    ],
}

STATE = {
    "cafe": {
        "roadmap": {"phases": [
            {"id": "legal", "title": "Легализация", "status": "active", "note": "ждём ИНН",
             "updated_at": 1761700100},
            {"id": "brand", "title": "Бренд", "status": "next", "note": "", "updated_at": None}]},
        "decisions": [
            {"id": "d_1", "text": "Регистрировать LTDA, а не ИП", "why": "два собственника",
             "status": "accepted", "source_request": "t_a1b2c3d4", "at": 1761700200},
            {"id": "d_2", "text": "Цена 220 ₽ за моти", "why": "маржа 48,8 %",
             "status": "proposed", "source_request": "t_a1b2c3d4"},
        ],
        "backlog": [
            {"id": "t_a1b2c3d4:ns_1", "text": "Согласовать цену 220 ₽", "owner": "mikhail",
             "due": "2026-11-05", "status": "open", "source_request": "t_a1b2c3d4",
             "source_title": "Легализация и ИНН", "created_at": 1761700300},
            {"id": "t_a1b2c3d4:ns_2", "text": "Проверить ёмкость рынка моти", "owner": "biz-cfo",
             "due": None, "status": "open", "source_request": "t_a1b2c3d4"},
        ],
        "journal": [
            {"id": "ev1", "at": 1761700400, "kind": "request_done", "text": "Запрос завершён",
             "detail": {"summary": "Юрист подтвердил легализацию и прислал список документов."}},
            {"id": "ev2", "at": 1761700500, "kind": "report_added", "text": "Добавлен отчёт",
             "detail": {"path": "data/reports/cafe/unit/report.json"}},
        ],
    },
    "fn": {
        "roadmap": {"phases": [{"id": "process", "title": "Процессы", "status": "active"}]},
        "decisions": [], "backlog": [], "journal": [],
    },
}


def fake_requests(cfg, room_id, limit=500):
    return list(REQUESTS.get(room_id, []))[:limit]


def fake_state(cfg, room_id):
    return STATE.get(room_id, {})


class SearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="tr-search-"))
        cls._env = mock.patch.dict(os.environ, {
            "TEAMROOM_DATA": str(cls.tmp / "data"),
            "TEAMROOM_VAULT_DIR": str(cls.tmp / "vault"),
        })
        cls._env.start()
        cls.cfg = config.load()
        cls.cfg.ensure_dirs()
        (cls.cfg.data_dir / "rooms.json").write_text(
            '{"rooms": [{"id": "cafe", "title": "Лето"}, {"id": "fn", "title": "FN"}]}',
            encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def setUp(self):
        search.clear_cache()
        self._p1 = mock.patch.object(search, "_list_requests", side_effect=fake_requests)
        self._p2 = mock.patch.object(search, "_load_state", side_effect=fake_state)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    # --- индекс ---
    def test_build_index_counts(self):
        info = search.build_index(self.cfg, "cafe")
        self.assertEqual(info["room"], "cafe")
        # 2 запроса + 2 решения + 2 бэклога + 2 журнала + 2 фазы
        self.assertEqual(info["docs"], 10)
        self.assertEqual(info["by_type"]["request"], 2)

    def test_all_types_present(self):
        info = search.build_index(self.cfg, "cafe")
        for kind in ("request", "decision", "backlog", "journal", "phase"):
            self.assertGreaterEqual(info["by_type"].get(kind, 0), 1, kind)

    def test_docs_have_required_fields(self):
        search.build_index(self.cfg, "cafe")
        hits = search.query(self.cfg, "моти")
        self.assertTrue(hits)
        for h in hits:
            for key in ("type", "room", "id", "title", "snippet", "score", "ranges"):
                self.assertIn(key, h)
            self.assertIsInstance(h["ranges"], list)

    # --- нормализация ё/е ---
    def test_yo_matches_e(self):
        for q in ("ёмкость", "емкость", "ЁМКОСТЬ"):
            hits = search.query(self.cfg, q, room="cafe")
            self.assertTrue(hits, q)
            self.assertTrue(any("мкость" in h["title"] or "мкость" in h["snippet"] for h in hits), q)

    def test_e_matches_yo_in_document(self):
        hits = search.query(self.cfg, "емкость рынка", room="cafe")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["type"], "backlog")

    # --- префиксная морфология ---
    def test_prefix_finds_inflection(self):
        hits = search.query(self.cfg, "легализац", room="cafe")
        self.assertTrue(hits)
        self.assertTrue(any("легализац" in h["title"].lower() or
                            "легализац" in h["snippet"].lower() for h in hits))
        # «цену» находится по «цена»
        self.assertTrue(search.query(self.cfg, "цена", room="cafe"))

    def test_short_query_not_noisy(self):
        self.assertEqual(search.query(self.cfg, "", room="cafe"), [])
        self.assertEqual(search.query(self.cfg, "   ", room="cafe"), [])
        self.assertEqual(search.query(self.cfg, "ъъъъ", room="cafe"), [])

    # --- сниппет и подсветка ---
    def test_snippet_and_ranges(self):
        hits = search.query(self.cfg, "легализац", room="cafe")
        hit = next(h for h in hits if h["type"] == "journal")
        self.assertTrue(hit["snippet"])
        self.assertEqual(len(hit["ranges"]), 1)
        start, end = hit["ranges"][0]
        fragment = hit["snippet"][start:end]
        self.assertTrue(fragment.lower().startswith("легализац"), fragment)
        self.assertIn("документ", hit["snippet"])

    def test_snippet_has_ellipsis_for_long_text(self):
        long_text = ("Вступление. " * 30) + "уникальноеслово" + (" хвост." * 30)
        with mock.patch.object(search, "_list_requests",
                               side_effect=lambda cfg, room, limit=500: [
                                   {"root_id": "t_long", "title": "Длинный", "text": long_text,
                                    "created_at": 1}] if room == "cafe" else []):
            search.clear_cache()
            hits = search.query(self.cfg, "уникальноеслово", room="cafe")
        self.assertEqual(len(hits), 1)
        snip = hits[0]["snippet"]
        self.assertIn("уникальноеслово", snip)
        self.assertTrue(snip.startswith("…"))
        self.assertTrue(snip.endswith("…"))
        self.assertLess(len(snip), 200)
        r0, r1 = hits[0]["ranges"][0]
        self.assertEqual(snip[r0:r1], "уникальноеслово")

    # --- ранжирование ---
    def test_request_beats_journal_for_same_word(self):
        hits = search.query(self.cfg, "легализация", room="cafe")
        types = [h["type"] for h in hits]
        self.assertIn("request", types)
        self.assertLess(types.index("request"), types.index("journal"))

    def test_title_match_beats_body_only(self):
        # «Упаковка» — в заголовке запроса; в бэклоге/журнале этого слова нет
        hits = search.query(self.cfg, "упаковка", room="cafe")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["type"], "request")
        self.assertEqual(hits[0]["id"], "t_beef0001")

    def test_decisions_found_and_ranked(self):
        hits = search.query(self.cfg, "LTDA", room="cafe")
        self.assertEqual(hits[0]["type"], "decision")
        self.assertEqual(hits[0]["id"], "d_1")
        # текст ответа из журнала тоже ищется
        self.assertTrue(any(h["type"] == "journal" for h in
                            search.query(self.cfg, "документов", room="cafe")))

    def test_scores_descending(self):
        hits = search.query(self.cfg, "комнаты", room="cafe")
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    # --- фильтры и лимиты ---
    def test_room_filter(self):
        self.assertTrue(search.query(self.cfg, "регламент", room="fn"))
        self.assertEqual(search.query(self.cfg, "регламент", room="cafe"), [])
        self.assertEqual(search.query(self.cfg, "регламент", room="nope"), [])

    def test_global_search_covers_rooms(self):
        rooms = {h["room"] for h in search.query(self.cfg, "комната")}
        self.assertIn("cafe", rooms | {"cafe"})
        hits = search.query(self.cfg, "журнал")
        self.assertTrue(any(h["room"] == "fn" for h in hits))

    def test_limit(self):
        hits = search.query(self.cfg, "комната", limit=1)
        self.assertLessEqual(len(hits), 1)
        self.assertLessEqual(len(search.query(self.cfg, "комната", limit=0)), 1)
        self.assertLessEqual(len(search.query(self.cfg, "комната", limit=500)), 500)

    def test_route(self):
        hits = search.query(self.cfg, "ИНН", room="cafe")
        hit = hits[0]
        self.assertEqual(hit["route"], "#/room/cafe/request/t_a1b2c3d4")
        phase = search.query(self.cfg, "бренд", room="cafe")
        self.assertTrue(any(h["route"].endswith("/roadmap") for h in phase if h["type"] == "phase"))

    # --- кэш ---
    def test_cache_ttl_and_clear(self):
        search.build_index(self.cfg, "cafe")
        self.assertTrue(search.query(self.cfg, "моти", room="cafe"))
        with mock.patch.object(search, "_list_requests",
                               side_effect=lambda cfg, room, limit=500: []), \
                mock.patch.object(search, "_load_state", side_effect=lambda cfg, room: {}):
            # в пределах TTL данные берутся из кэша
            self.assertTrue(search.query(self.cfg, "моти", room="cafe"))
            search.clear_cache()
            self.assertEqual(search.query(self.cfg, "моти", room="cafe"), [])

    def test_expired_cache_rebuilds(self):
        search.build_index(self.cfg, "cafe")
        with mock.patch.object(search, "_list_requests",
                               side_effect=lambda cfg, room, limit=500: []), \
                mock.patch.object(search, "_load_state", side_effect=lambda cfg, room: {}), \
                mock.patch.object(search.time, "monotonic", return_value=10 ** 9):
            self.assertEqual(search.query(self.cfg, "моти", room="cafe"), [])

    # --- устойчивость ---
    def test_broken_state_does_not_crash(self):
        with mock.patch.object(search, "_load_state",
                               side_effect=RuntimeError("битый state")), \
                mock.patch.object(search, "_list_requests",
                                  side_effect=lambda cfg, room, limit=500: REQUESTS["cafe"]):
            search.clear_cache()
            hits = search.query(self.cfg, "легализация", room="cafe")
        self.assertTrue(hits)  # запросы всё равно нашлись

    def test_fold_and_tokens(self):
        self.assertEqual(search.fold("ЁЖИК Ёлка"), "ежик елка")
        self.assertEqual(len(search.fold("ЁЖИК")), len("ЁЖИК"))
        self.assertEqual([t[0] for t in search.tokens(search.fold("Цена 220 ₽"))], ["цена", "220"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
