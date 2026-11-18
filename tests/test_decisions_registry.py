"""Реестр решений комнаты (W25 M1): гонки, цепочка, миграция, «воскрешение».

Тесты без сети и без живого Hermes: только временные каталоги. Проверяют
ровно то, что просила приёмка пакета:

* параллельные записи потоками и процессами не теряются, цепочка цела;
* подмена/удаление строки ломает цепочку (`decision_registry.verify`);
* миграция из `decisions.json` идемпотентна и делает резервную копию;
* решение, снятое/устаревшее в запросе, НЕ попадает в «принятые» паспорта
  комнаты (реальный сценарий: `decisions.json` = accepted, в запросе dismissed);
* закрепление ограничено потолком; 7 инвариантов `check_room_memory`
  отрабатывают на пустой и наполненной комнате.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import decision_registry as dec_reg      # noqa: E402
from server import ingest_merge                       # noqa: E402
from server import statefiles as sf                   # noqa: E402
from server import vault_mirror                       # noqa: E402


def make_cfg(root: Path):
    """Минимальный Cfg: только то, что читает реестр и зеркало."""
    return SimpleNamespace(state_dir=root / "state", vault_dir=root / "Vault" / "Rooms",
                           data_dir=root / "data", log_dir=root / "logs",
                           reports_dir=root / "reports", tmp_dir=root / "tmp")


WORKER = r'''
import sys
from pathlib import Path
sys.path.insert(0, %(root)r)
from server import decision_registry as reg

class Cfg:
    def __init__(self, root):
        self.state_dir = Path(root) / "state"
        self.vault_dir = Path(root) / "Vault" / "Rooms"

n = sys.argv[1]
cfg = Cfg(sys.argv[2])
for i in range(20):
    reg.append(cfg, "proc", [{"id": "t_7:d_%%s_%%s" %% (n, i), "text": "пункт %%s" %% i,
                              "status": "proposed", "source_request": "t_7"}],
               at=2000 + i)
'''


class RegistryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w25-registry-"))
        self.cfg = make_cfg(self.tmp)
        self.room = "fn"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAppendAndChain(RegistryBase):
    def test_first_row_has_genesis_prev_hash(self):
        written = dec_reg.append(self.cfg, self.room, [
            {"id": "t_1:d_1", "text": "Согласовать формат свода", "why": "иначе не читается",
             "status": "accepted", "source_request": "t_1"}], at=100)
        self.assertEqual(len(written), 1)
        row = written[0]
        self.assertEqual(row["seq"], 1)
        self.assertEqual(row["prev_hash"], dec_reg.GENESIS)
        self.assertEqual(row["hash"], dec_reg.row_hash(row))
        self.assertTrue(dec_reg.verify(self.cfg, self.room)["ok"])

    def test_status_change_is_new_row_with_link(self):
        dec_reg.append(self.cfg, self.room, [{"id": "t_1:d_1", "text": "Решение",
                                              "status": "accepted", "source_request": "t_1"}], at=100)
        written = dec_reg.set_status(self.cfg, self.room, "t_1:d_1", "dismissed", by="user",
                                     why="неактуально: тему сняли", at=200)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["status"], "dismissed")
        self.assertEqual(written[0]["prev_status"], "accepted")
        self.assertEqual(written[0]["supersedes"], 1)
        self.assertEqual(len(dec_reg.rows(self.cfg, self.room)), 2, "прежняя строка не переписана")
        self.assertEqual(dec_reg.active(self.cfg, self.room), [])
        self.assertEqual(dec_reg.current(self.cfg, self.room)["t_1:d_1"]["status"], "dismissed")

    def test_same_status_is_idempotent(self):
        dec_reg.set_status(self.cfg, self.room, "t_1:d_1", "accepted", at=100)
        self.assertEqual(dec_reg.set_status(self.cfg, self.room, "t_1:d_1", "accepted", at=150), [])
        self.assertEqual(len(dec_reg.rows(self.cfg, self.room)), 1)

    def test_unknown_status_rejected(self):
        with self.assertRaises(ValueError):
            dec_reg.append(self.cfg, self.room, [{"id": "t_1:d_1", "text": "x",
                                                  "status": "maybe", "source_request": "t_1"}])

    def test_scoped_ids_from_request(self):
        dec_reg.sync_request(self.cfg, self.room, "t_9", [{"id": "d_1", "text": "Раз",
                                                           "status": "accepted"}], at=10)
        self.assertIn("t_9:d_1", dec_reg.current(self.cfg, self.room))


class TestConcurrency(RegistryBase):
    def test_threads_do_not_lose_rows(self):
        def worker(number):
            for index in range(25):
                dec_reg.append(self.cfg, "thr", [
                    {"id": "t_1:d_%d_%d" % (number, index), "text": "пункт %d" % index,
                     "status": "proposed", "source_request": "t_1"}], at=1000 + index)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state = dec_reg.read_state(self.cfg, "thr")
        self.assertEqual(state["count"], 100)
        self.assertEqual(len(state["current"]), 100)
        self.assertTrue(dec_reg.verify(self.cfg, "thr")["ok"])

    def test_processes_do_not_lose_rows(self):
        worker = self.tmp / "worker.py"
        worker.write_text(WORKER % {"root": str(ROOT)}, encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, str(worker), str(n), str(self.tmp)])
                 for n in range(3)]
        for proc in procs:
            proc.wait(timeout=120)
        state = dec_reg.read_state(self.cfg, "proc")
        self.assertEqual(state["count"], 60, "потеря строк при записи из процессов")
        self.assertEqual(len(state["current"]), 60)
        verify = dec_reg.verify(self.cfg, "proc")
        self.assertTrue(verify["ok"], verify)


class TestTamperDetection(RegistryBase):
    def _fill(self, count=4):
        for index in range(count):
            dec_reg.append(self.cfg, self.room, [
                {"id": "t_1:d_%d" % index, "text": "Решение %d" % index,
                 "status": "accepted", "source_request": "t_1"}], at=100 + index)

    def _rewrite(self, rows):
        path = dec_reg.registry_path(self.cfg, self.room)
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True)
                                  for row in rows) + "\n", encoding="utf-8", newline="\n")

    def test_edited_row_detected(self):
        self._fill()
        path = dec_reg.registry_path(self.cfg, self.room)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[2]["text"] = "Подменённый текст"
        self._rewrite(rows)
        verify = dec_reg.verify(self.cfg, self.room)
        self.assertFalse(verify["ok"])
        self.assertEqual(verify["broken_at"], 3)
        self.assertIn("изменена", verify["reason"])

    def test_deleted_row_detected(self):
        self._fill()
        path = dec_reg.registry_path(self.cfg, self.room)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self._rewrite([rows[0]] + rows[2:])
        verify = dec_reg.verify(self.cfg, self.room)
        self.assertFalse(verify["ok"])
        self.assertEqual(verify["broken_at"], 2)

    def test_forged_row_with_recomputed_hash_detected(self):
        self._fill(3)
        path = dec_reg.registry_path(self.cfg, self.room)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["status"] = "dismissed"
        rows[0]["hash"] = dec_reg.row_hash(rows[0])       # аккуратная подделка
        self._rewrite(rows)
        verify = dec_reg.verify(self.cfg, self.room)
        self.assertFalse(verify["ok"])
        self.assertEqual(verify["broken_at"], 2, "разрыв должен находиться на следующей строке")

    def test_broken_json_detected(self):
        self._fill(2)
        path = dec_reg.registry_path(self.cfg, self.room)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "t_1:d_x", \n')
        self.assertFalse(dec_reg.verify(self.cfg, self.room)["ok"])


class TestMigration(RegistryBase):
    def _decisions(self, status="accepted"):
        path = Path(self.cfg.state_dir) / self.room / "decisions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"room": self.room, "updated_at": 500, "decisions": [
            {"id": "t_9:d_1", "text": "Решение А", "why": None, "status": status,
             "source_request": "t_9", "at": 500},
            {"id": "t_9:d_2", "text": "Решение Б", "why": "нужен срок", "status": "proposed",
             "source_request": "t_9", "at": 501}]}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_migration_writes_rows_and_backup(self):
        self._decisions()
        result = dec_reg.migrate_from_decisions(self.cfg, self.room)
        self.assertEqual(result["source"], 2)
        self.assertEqual(result["written"], 2)
        self.assertTrue(result["backup"] and Path(result["backup"]).exists())
        self.assertTrue(dec_reg.verify(self.cfg, self.room)["ok"])
        self.assertEqual(len(dec_reg.active(self.cfg, self.room)), 1)

    def test_migration_is_idempotent(self):
        self._decisions()
        first = dec_reg.migrate_from_decisions(self.cfg, self.room)
        second = dec_reg.migrate_from_decisions(self.cfg, self.room)
        self.assertEqual(first["written"], 2)
        self.assertEqual(second["written"], 0, "повторный запуск не должен дублировать строки")
        self.assertEqual(len(dec_reg.rows(self.cfg, self.room)), 2)

    def test_migration_projects_status_back(self):
        path = self._decisions(status="accepted")
        dec_reg.migrate_from_decisions(self.cfg, self.room)
        dec_reg.set_status(self.cfg, self.room, "t_9:d_1", "dismissed", by="user", at=600)
        dec_reg.project_decisions(self.cfg, self.room)
        data = json.loads(path.read_text(encoding="utf-8"))
        statuses = {item["id"]: item["status"] for item in data["decisions"]}
        self.assertEqual(statuses["t_9:d_1"], "dismissed")
        self.assertEqual(data["projection_of"], dec_reg.REGISTRY_FILE)


class TestRevival(RegistryBase):
    """Решение, отменённое в запросе, не должно «воскресать» как принятое."""

    def _legacy_accepted(self):
        path = Path(self.cfg.state_dir) / self.room / "decisions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"room": self.room, "updated_at": 400, "decisions": [
            {"id": "t_825ada4c:d_37aefd88", "text": "Согласовывать цену сделки в письме",
             "why": None, "status": "accepted", "owner": "mikhail", "at": 400,
             "updated_at": 400, "source_request": "t_825ada4c"}]}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def test_merge_downgrades_accepted_to_dismissed(self):
        data = self._legacy_accepted()
        added, changed = ingest_merge._merge_decisions(
            self.cfg, self.room, "t_825ada4c", 900,
            [{"id": "d_37aefd88", "text": "Согласовывать цену сделки в письме", "why": None,
              "status": "dismissed", "owner": "mikhail"}])
        self.assertEqual(added, [])
        stored = json.loads((Path(self.cfg.state_dir) / self.room / "decisions.json")
                            .read_text(encoding="utf-8"))
        by_id = {item["id"]: item["status"] for item in stored["decisions"]}
        self.assertEqual(by_id["t_825ada4c:d_37aefd88"], "dismissed",
                         "снятое решение осталось accepted — «воскрешение» вернулось")
        self.assertIn("t_825ada4c:d_37aefd88", changed)

    def test_passport_does_not_list_dismissed_as_accepted(self):
        self._legacy_accepted()
        # руководитель отозвал решение: в detail статус уже superseded/dismissed
        dec_reg.migrate_from_decisions(self.cfg, self.room)
        dec_reg.set_status(self.cfg, self.room, "t_825ada4c:d_37aefd88", "dismissed",
                           by="user", why="неактуально: цену в письме не обсуждаем", at=1000)
        state = {"roadmap": {}, "backlog": {}, "decisions": [
            {"id": "t_825ada4c:d_37aefd88", "text": "Согласовывать цену сделки в письме",
             "status": "accepted", "source_request": "t_825ada4c"}]}
        text = vault_mirror._state_md(self.cfg, self.room, "Комната fn", state)
        self.assertNotIn("Согласовывать цену сделки в письме", text,
                         "снятое решение попало в «Принято» паспорта")
        self.assertIn("Снято, отклонено или устарело (1)", text)
        blocks = vault_mirror._context_blocks(self.cfg, {"id": self.room, "title": "fn"},
                                              state, [])
        decisions_block = dict(blocks)["decisions"]
        self.assertNotIn("Согласовывать цену сделки в письме", decisions_block)
        self.assertIn("Снято/устарело: 1", decisions_block)

    def test_passport_keeps_accepted_with_id(self):
        dec_reg.set_status(self.cfg, self.room, "t_1:d_1", "accepted", text="Формат свода",
                           source_request="t_1", at=100)
        state = {"roadmap": {}, "backlog": {}, "decisions": []}
        text = vault_mirror._state_md(self.cfg, self.room, "Комната fn", state)
        self.assertIn("t_1:d_1", text)
        self.assertIn("Формат свода", text)


class TestPinned(RegistryBase):
    def test_pin_limit_is_enforced(self):
        for index in range(dec_reg.PIN_LIMIT):
            self.assertTrue(dec_reg.pin(self.cfg, self.room, "t_1:d_%d" % index)["added"])
        extra = dec_reg.pin(self.cfg, self.room, "t_1:d_extra")
        self.assertFalse(extra["added"])
        self.assertEqual(extra["reason"], "pin_limit")
        state = dec_reg.pin_state(self.cfg, self.room)
        self.assertTrue(state["over_limit"])
        self.assertIn("30", state["warning"])
        self.assertEqual(dec_reg.unpin(self.cfg, self.room, "t_1:d_0")["removed"], True)
        self.assertFalse(dec_reg.pin_state(self.cfg, self.room)["over_limit"])

    def test_snapshot_tracks_missing_ids(self):
        dec_reg.set_status(self.cfg, self.room, "t_1:d_1", "accepted", at=1)
        dec_reg.write_snapshot(self.cfg, self.room)
        self.assertEqual(dec_reg.snapshot_missing(self.cfg, self.room), [])


class TestApiRow(unittest.TestCase):
    def test_public_row_hides_hashes(self):
        from server.routes_registry import public_row
        row = {"id": "t_1:d_1", "at": 5, "text": "x", "status": "accepted",
               "seq": 3, "prev_hash": "a", "hash": "b", "by": "user"}
        out = public_row(row, ["t_1:d_1"])
        self.assertTrue(out["pinned"])
        self.assertNotIn("hash", out)
        self.assertNotIn("prev_hash", out)


class TestCheckRoomMemory(RegistryBase):
    def test_empty_room_is_green_with_skips(self):
        from tools import check_room_memory
        report = check_room_memory.check(self.cfg, self.room)
        self.assertTrue(report["ok"])
        statuses = {item["n"]: item["status"] for item in report["results"]}
        self.assertEqual(statuses[1], check_room_memory.SKIP)
        self.assertEqual(statuses[7], check_room_memory.SKIP)

    def test_passport_mismatch_is_blocking(self):
        from tools import check_room_memory
        dec_reg.set_status(self.cfg, self.room, "t_1:d_1", "accepted", text="Формат свода",
                           source_request="t_1", at=100)
        vault = Path(self.cfg.vault_dir) / self.room
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "CONTEXT.md").write_text(
            "## Решения\n- `t_1:d_1` — Формат свода\n- `t_1:d_404` — Отменённое\n",
            encoding="utf-8")
        report = check_room_memory.check(self.cfg, self.room, only=[1])
        self.assertFalse(report["ok"])
        self.assertIn("t_1:d_404", report["results"][0]["examples"])

    def test_chain_break_is_blocking(self):
        from tools import check_room_memory
        dec_reg.set_status(self.cfg, self.room, "t_1:d_1", "accepted", at=100)
        path = dec_reg.registry_path(self.cfg, self.room)
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        row["text"] = "подмена"
        path.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        report = check_room_memory.check(self.cfg, self.room, only=[])
        self.assertFalse(report["ok"])
        self.assertEqual(report["results"][0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
