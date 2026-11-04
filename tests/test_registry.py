"""Тесты реестра запросов: data/state/<room>/requests.json."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, registry, rooms  # noqa: E402


class RegistryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-reg-")
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

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def add(self, root_id="t_a1", slug="r-1", title="Запрос", **extra):
        rec = {"root_id": root_id, "slug": slug, "title": title, "text": "текст",
               "created_at": 1793160000, "critic": False, "mode": "auto"}
        rec.update(extra)
        return registry.add_request(self.cfg, "cafe", rec)


class TestRegistry(RegistryBase):
    def test_add_get_and_find_by_slug(self):
        rec = self.add()
        self.assertEqual(rec["root_id"], "t_a1")
        self.assertEqual(registry.get(self.cfg, "cafe", "t_a1")["slug"], "r-1")
        self.assertEqual(registry.find_by_slug(self.cfg, "cafe", "r-1")["root_id"], "t_a1")
        self.assertIsNone(registry.get(self.cfg, "cafe", "t_missing"))
        self.assertIsNone(registry.find_by_slug(self.cfg, "cafe", "nope"))

    def test_add_does_not_duplicate_root(self):
        self.add(title="первый")
        self.add(title="второй")
        rows = registry.list_requests(self.cfg, "cafe")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "второй")

    def test_list_requests_newest_first_and_limit(self):
        self.add(root_id="t_old", created_at=100)
        self.add(root_id="t_new", created_at=300)
        self.add(root_id="t_mid", created_at=200)
        rows = registry.list_requests(self.cfg, "cafe", 2)
        self.assertEqual([r["root_id"] for r in rows], ["t_new", "t_mid"])

    def test_mark_ingested_idempotent(self):
        self.add()
        self.assertTrue(registry.mark_ingested(self.cfg, "cafe", "t_a1", "run-1"))
        self.assertFalse(registry.mark_ingested(self.cfg, "cafe", "t_a1", "run-1"))
        self.assertTrue(registry.mark_ingested(self.cfg, "cafe", "t_a1", "run-2"))
        self.assertEqual(registry.get(self.cfg, "cafe", "t_a1")["ingested"],
                         {"run-1": True, "run-2": True})

    def test_mark_ingested_unknown_root_returns_false(self):
        self.assertFalse(registry.mark_ingested(self.cfg, "cafe", "t_nope", "run"))

    def test_update_merges_and_keeps_fields(self):
        self.add()
        rec = registry.update(self.cfg, "cafe", "t_a1", {"title": "Новый", "critic": True})
        self.assertEqual(rec["title"], "Новый")
        self.assertTrue(rec["critic"])
        self.assertEqual(rec["text"], "текст")
        self.assertIsNone(registry.update(self.cfg, "cafe", "t_nope", {"title": "x"}))

    def test_update_ingested_merges_not_replaces(self):
        self.add()
        registry.mark_ingested(self.cfg, "cafe", "t_a1", "r1")
        rec = registry.update(self.cfg, "cafe", "t_a1", {"ingested": {"r2": True}})
        self.assertEqual(rec["ingested"], {"r1": True, "r2": True})

    def test_update_rejects_bad_types(self):
        self.add()
        with self.assertRaises(ValueError):
            registry.update(self.cfg, "cafe", "t_a1", {"ingested": []})
        with self.assertRaises(ValueError):
            registry.update(self.cfg, "cafe", "t_a1", "не словарь")

    def test_add_extra_card_is_idempotent(self):
        self.add()
        card = {"id": "t_c1", "kind": "critic", "round": 1, "at": 1793160100}
        registry.add_extra_card(self.cfg, "cafe", "t_a1", card)
        registry.add_extra_card(self.cfg, "cafe", "t_a1", card)
        rec = registry.get(self.cfg, "cafe", "t_a1")
        self.assertEqual(len(rec["extra_cards"]), 1)
        self.assertEqual(rec["extra_cards"][0]["kind"], "critic")

    def test_all_registered_ids_and_board_filter(self):
        self.add(root_id="t_cafe")
        registry.add_request(self.cfg, "fn", {"root_id": "t_fn", "slug": "r-2", "created_at": 1})
        self.assertEqual(registry.all_registered_ids(self.cfg), {"t_cafe", "t_fn"})
        self.assertEqual(registry.all_registered_ids(self.cfg, board="team"), {"t_cafe", "t_fn"})
        rooms.create_room(self.cfg, {"id": "dev", "title": "Разработка", "orchestrator": "default",
                                     "board": "devboard", "members": [{"profile": "coder"}]})
        registry.add_request(self.cfg, "dev", {"root_id": "t_dev", "slug": "r-3", "created_at": 2})
        self.assertEqual(registry.all_registered_ids(self.cfg, board="team"), {"t_cafe", "t_fn"})
        self.assertEqual(registry.all_registered_ids(self.cfg, board="devboard"), {"t_dev"})

    def test_concurrent_updates_do_not_lose_data(self):
        self.add()
        errors: list[BaseException] = []

        def worker(prefix: str):
            try:
                for i in range(25):
                    registry.update(self.cfg, "cafe", "t_a1", {f"{prefix}_{i}": i})
            except BaseException as exc:  # noqa: BLE001 - тест должен показать причину
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(p,)) for p in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        rec = registry.get(self.cfg, "cafe", "t_a1")
        self.assertEqual(len([k for k in rec if k.startswith("a_")]), 25)
        self.assertEqual(len([k for k in rec if k.startswith("b_")]), 25)

    def test_cyrillic_stored_raw(self):
        self.add(title="Лето — где мы сейчас", text="Проверка кириллицы: ёжик, «кавычки»")
        raw = (self.tmp / "data" / "state" / "cafe" / "requests.json").read_text(encoding="utf-8")
        self.assertIn("Лето", raw)
        self.assertIn("ёжик", raw)
        self.assertNotIn("\\u", raw)
        self.assertEqual(json.loads(raw)["requests"][0]["text"], "Проверка кириллицы: ёжик, «кавычки»")

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            registry.add_request(self.cfg, "cafe", {"slug": "no-root"})
        with self.assertRaises(ValueError):
            registry.mark_ingested(self.cfg, "cafe", "t_a1", "")
        with self.assertRaises(ValueError):
            registry.add_extra_card(self.cfg, "cafe", "t_a1", {"kind": "critic"})


class TestStatefiles(RegistryBase):
    def test_read_json_default_on_missing_and_broken(self):
        from server import statefiles as sf
        path = self.tmp / "x.json"
        self.assertEqual(sf.read_json(path, {"a": 1}), {"a": 1})
        path.write_text("{битый", encoding="utf-8")
        self.assertIsNone(sf.read_json(path))
        self.assertEqual(sf.read_json(path, []), [])

    def test_atomic_write_leaves_no_tmp_and_keeps_utf8(self):
        from server import statefiles as sf
        path = self.tmp / "deep" / "dir" / "obj.json"
        sf.write_json_atomic(path, {"текст": "ёжик", "n": 1})
        self.assertEqual(sf.read_json(path), {"текст": "ёжик", "n": 1})
        self.assertEqual([p.name for p in path.parent.iterdir()], ["obj.json"])
        self.assertNotIn("\\u", path.read_text(encoding="utf-8"))

    def test_jsonl_append_read_limit_and_broken_lines(self):
        from server import statefiles as sf
        path = self.tmp / "j.jsonl"
        for i in range(5):
            sf.append_jsonl(path, {"i": i, "text": "строка"})
        path.write_text(path.read_text(encoding="utf-8") + "не json\n", encoding="utf-8")
        rows = sf.read_jsonl(path)
        self.assertEqual([r["i"] for r in rows], [0, 1, 2, 3, 4])
        self.assertEqual([r["i"] for r in sf.read_jsonl(path, limit=2)], [3, 4])
        self.assertEqual(sf.read_jsonl(self.tmp / "none.jsonl"), [])

    def test_write_text_if_changed(self):
        from server import statefiles as sf
        path = self.tmp / "STATE.md"
        self.assertTrue(sf.write_text_if_changed(path, "привет\n"))
        self.assertFalse(sf.write_text_if_changed(path, "привет\n"))
        self.assertTrue(sf.write_text_if_changed(path, "привет 2\n"))
        self.assertEqual(path.read_bytes(), "привет 2\n".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
