"""Надёжность файлов состояния (D4/D5/D14): BOM, битый/занятый файл, повторы записи."""
from __future__ import annotations

import codecs
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import statefiles as sf  # noqa: E402


class StateFilesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-state-")
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "rooms.json"

    def tearDown(self):
        self._tmp.cleanup()


class TestReadJsonDoc(StateFilesBase):
    def test_bom_is_read_as_utf8_sig(self):
        """D4: BOM — не повод считать файл битым (иначе seed затирал комнаты)."""
        payload = {"version": 1, "rooms": [{"id": "mine"}]}
        self.path.write_bytes(codecs.BOM_UTF8 + json.dumps(payload).encode("utf-8"))
        value, info = sf.read_json_doc(self.path, None)
        self.assertEqual(info["state"], "ok")
        self.assertEqual(value["rooms"][0]["id"], "mine")

    def test_missing_file_is_missing_not_corrupt(self):
        value, info = sf.read_json_doc(self.path, {"default": True})
        self.assertEqual(info["state"], "missing")
        self.assertTrue(value["default"])

    def test_truncated_and_empty_are_corrupt(self):
        for raw in ('{"rooms": [', "", "   "):
            self.path.write_text(raw, encoding="utf-8")
            value, info = sf.read_json_doc(self.path, None)
            self.assertEqual(info["state"], "corrupt", raw)
            self.assertIsNone(value)
            self.assertTrue(info["message"])

    def test_quarantine_keeps_bytes_and_reports_backup(self):
        """D4/D5: копия `<файл>.corrupt-<ts>` появляется ДО перезаписи."""
        broken = '{"version": 1, "rooms": ['
        self.path.write_text(broken, encoding="utf-8")
        _value, info = sf.read_json_doc(self.path, None)
        self.assertEqual(info["state"], "corrupt")
        backup = sf.quarantine(self.path)
        self.assertTrue(backup)
        self.assertTrue(Path(backup).is_file())
        self.assertEqual(Path(backup).read_text(encoding="utf-8"), broken)
        self.assertIn(".corrupt-", Path(backup).name)
        self.assertTrue(self.path.is_file())          # исходник не удалён
        problems = [p for p in sf.problems() if p["path"] == str(self.path)]
        self.assertTrue(problems)
        self.assertEqual(problems[-1]["backup"], backup)
        self.assertIn("копия", problems[-1]["message"])
        self.assertEqual(sf.quarantine(self.path), backup)   # второй вызов не плодит копии

    def test_permission_error_retries_and_is_not_default(self):
        """D4: PermissionError — 3 попытки, «нечитаем», а не «данных нет»."""
        self.path.write_text('{"rooms": []}', encoding="utf-8")
        calls = []
        real_open = open

        def fake_open(file, *args, **kwargs):
            if str(file) == str(self.path):
                calls.append(1)
                raise PermissionError(13, "файл занят", str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", fake_open), \
                mock.patch.object(sf, "READ_PAUSE", 0.0):
            value, info = sf.read_json_doc(self.path, {"fallback": True})
        self.assertEqual(info["state"], "unreadable")
        self.assertEqual(value, {"fallback": True})
        self.assertEqual(len(calls), sf.READ_ATTEMPTS)
        problem = [p for p in sf.problems() if p["path"] == str(self.path)][-1]
        self.assertEqual(problem["code"], "state_unreadable")
        self.assertIn("не заменяются", problem["message"])


class TestWrites(StateFilesBase):
    def test_replace_retries_permission_error(self):
        """D14: os.replace под открытым читателем — 5 попыток по 20 мс."""
        real_replace = os.replace
        attempts = []

        def flaky(src, dst):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(13, "файл занят", str(dst))
            return real_replace(src, dst)

        with mock.patch.object(sf.os, "replace", flaky), mock.patch.object(sf, "REPLACE_PAUSE", 0.0):
            sf.write_json_atomic(self.path, {"ok": True})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["ok"], True)

    def test_replace_gives_up_after_limit_and_cleans_tmp(self):
        def always_busy(src, dst):
            raise PermissionError(13, "файл занят", str(dst))

        with mock.patch.object(sf.os, "replace", always_busy), mock.patch.object(sf, "REPLACE_PAUSE", 0.0):
            with self.assertRaises(PermissionError):
                sf.write_json_atomic(self.path, {"ok": False})
        self.assertEqual(sf.REPLACE_ATTEMPTS, 5)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_write_text_if_changed_retries_read(self):
        """D14: чтение для сравнения тоже повторяется при PermissionError."""
        self.path.write_text("старое", encoding="utf-8")
        real_read = Path.read_bytes
        calls = []

        def flaky(path_self):
            calls.append(path_self)
            if len(calls) == 1:
                raise PermissionError(13, "занят", str(path_self))
            return real_read(path_self)

        with mock.patch.object(Path, "read_bytes", flaky), mock.patch.object(sf, "READ_PAUSE", 0.0):
            changed = sf.write_text_if_changed(self.path, "новое")
        self.assertTrue(changed)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "новое")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
