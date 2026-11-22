"""Поиск по памяти комнаты (W29B-Q8): «почему мы так решили».

Проверяем то, ради чего поиск существует:

* решение находится первым и приходит с цитатой, датой, ссылкой и подписью
  источника — человек может проверить, а не поверить;
* свод, ответ, журнал и заметка Vault тоже находятся, у каждого свой вид;
* пустой/короткий запрос — честный пустой ответ (никаких «всё подряд»);
* битая строка реестра не валит поиск; фильтр по комнатам и видам работает;
* путей с диска в выдаче нет — только названия и ссылки приложения.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import server.memory_search as ms


class Cfg:
    """Минимальная настройка: только каталоги данных (песочница теста)."""

    def __init__(self, root: Path):
        self.data_dir = root
        self.state_dir = root / "state"
        self.reports_dir = root / "reports"
        self.vault_dir = root / "Vault" / "Projects" / "Rooms"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _registry_rows(rows) -> str:
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


class MemoryFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Cfg(self.root)
        cafe = self.cfg.state_dir / "cafe"
        _write(cafe / "memory" / "registry.jsonl", _registry_rows([
            {"id": "t_cafe1:d_1", "seq": 1, "at": 1_700_000_000,
             "text": "Доставка цветов только по вторникам и четвергам",
             "why": "у флориста в другие дни нет свободной машины",
             "status": "accepted", "source_request": "t_cafe1"},
            {"id": "t_cafe1:d_2", "seq": 2, "at": 1_700_000_100,
             "text": "Букеты собираем сами, доставку отдаём подрядчику",
             "why": "", "status": "accepted", "source_request": "t_cafe1"},
            {"id": "t_cafe2:d_1", "seq": 3, "at": 1_700_000_200,
             "text": "Упаковку закупаем раз в месяц", "why": "",
             "status": "rejected", "source_request": "t_cafe2"},
        ]))
        _write(cafe / "state.json", json.dumps({
            "answers": [{"id": "a_1", "at": 1_700_000_300,
                         "text": "Доставка цветов: клиент выбирает интервал сам",
                         "source_request": "t_cafe1"}],
            "journal": {"items": [{"id": "ev_1", "at": 1_700_000_400,
                                   "text": "Курьер подтвердил доставку цветов",
                                   "kind": "note", "actor": "cafe"}]},
        }, ensure_ascii=False), )
        _write(self.cfg.reports_dir / "cafe" / "svod-1.md",
               "# Свод по доставке\n\nДоставка цветов идёт по вторникам.\n")
        _write(self.cfg.vault_dir / "cafe" / "Доставка.md",
               "# Заметка\n\nКлиенты спрашивают про доставку цветов на праздники.\n")
        # вторая комната — чтобы проверить фильтр
        _write(self.cfg.state_dir / "laundry" / "memory" / "registry.jsonl", _registry_rows([
            {"id": "t_laundry1:d_1", "seq": 1, "at": 1_700_000_500,
             "text": "Химчистка пуховиков — только по предоплате",
             "status": "accepted", "source_request": "t_laundry1"},
        ]))

    def tearDown(self):
        self._tmp.cleanup()


class SearchTest(MemoryFixture):
    def test_decision_found_first_with_citation(self):
        res = ms.search(self.cfg, "доставка цветов", room="cafe")
        self.assertGreaterEqual(res["total"], 1)
        first = res["items"][0]
        self.assertEqual(first["kind"], "decision")
        self.assertIn("доставка", first["snippet"].lower())
        self.assertTrue(first["citation"].startswith("Решение"))
        self.assertEqual(first["route"], "#/room/cafe/request/t_cafe1")
        self.assertTrue(first["ranges"])
        self.assertEqual(first["request_id"], "t_cafe1")

    def test_all_kinds_present(self):
        kinds = {item["kind"] for item in ms.search(self.cfg, "доставка", room="cafe",
                                                    limit=50)["items"]}
        self.assertTrue({"decision", "svod", "answer", "journal", "note"} <= kinds, kinds)

    def test_why_is_searchable(self):
        res = ms.search(self.cfg, "флориста", room="cafe")
        self.assertTrue(res["items"])
        self.assertEqual(res["items"][0]["kind"], "decision")

    def test_empty_query_is_empty_answer(self):
        for q in ("", "   ", " я "):
            res = ms.search(self.cfg, q, room="cafe")
            self.assertEqual(res["items"], [])
            self.assertTrue(res["note"])

    def test_limit_and_counts(self):
        res = ms.search(self.cfg, "доставка", room="cafe", limit=2)
        self.assertEqual(len(res["items"]), 2)
        self.assertGreaterEqual(res["total"], 2)
        self.assertEqual(sum(res["by_kind"].values()), res["total"])

    def test_room_filter_and_all_rooms(self):
        only = ms.search(self.cfg, "химчистка", room="cafe")
        self.assertEqual(only["items"], [])
        everywhere = ms.search(self.cfg, "химчистка", room=["cafe", "laundry"])
        self.assertTrue(everywhere["items"])
        self.assertEqual(everywhere["items"][0]["room"], "laundry")

    def test_kinds_filter(self):
        res = ms.search(self.cfg, "доставка", room="cafe", kinds=["note"])
        self.assertTrue(res["items"])
        self.assertEqual({item["kind"] for item in res["items"]}, {"note"})

    def test_no_filesystem_paths_in_answer(self):
        text = json.dumps(ms.search(self.cfg, "доставка", room="cafe", limit=50),
                          ensure_ascii=False)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("C:\\\\", text)

    def test_broken_registry_line_does_not_break(self):
        path = self.cfg.state_dir / "cafe" / "memory" / "registry.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "{битая строка\n",
                        encoding="utf-8")
        res = ms.search(self.cfg, "доставка", room="cafe")
        self.assertTrue(res["items"])

    def test_unknown_room_is_empty_not_error(self):
        res = ms.search(self.cfg, "доставка", room="нет-такой")
        self.assertEqual(res["items"], [])
        self.assertTrue(res["note"])

    def test_citation_has_date_and_source(self):
        for item in ms.search(self.cfg, "доставка", room="cafe", limit=50)["items"]:
            self.assertTrue(item["citation"])
            self.assertTrue(item["source"])
            self.assertIn("·", item["citation"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
