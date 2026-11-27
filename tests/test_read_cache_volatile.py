"""Кэш чтения: служебные файлы сторожа не должны сбрасывать отпечаток доски.

Боевой замер 27.11.2026: `model_guard.json` и `worklog_digest.json` переписываются каждый
тик сторожа, и кэш `/api/rooms/<id>/tasks` не срабатывал ни разу (0,5 с вместо 0,01 с).
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import read_cache  # noqa: E402


class VolatileFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w31-rc-")
        self.base = Path(self.tmp)
        room = self.base / "state" / "cafe"
        (room / "watchdog").mkdir(parents=True)
        for name in ("requests.json", "model_guard.json", "worklog_digest.json"):
            (room / name).write_text("{}", encoding="utf-8")
        (room / "watchdog" / "tick.json").write_text("{}", encoding="utf-8")
        (self.base / "rooms.json").write_text("[]", encoding="utf-8")
        self.cfg = SimpleNamespace(data_dir=str(self.base), state_dir=str(self.base / "state"))
        self.room = room

    def _touch(self, path):
        later = time.time() + 5
        os.utime(path, (later, later))

    def test_guard_files_touch_does_not_change_mark(self):
        before = read_cache._files_mark(self.cfg, "cafe")
        for rel in ("model_guard.json", "worklog_digest.json", "watchdog/tick.json"):
            self._touch(self.room / rel)
        self.assertEqual(before, read_cache._files_mark(self.cfg, "cafe"))

    def test_registry_change_changes_mark(self):
        before = read_cache._files_mark(self.cfg, "cafe")
        self._touch(self.room / "requests.json")
        self.assertNotEqual(before, read_cache._files_mark(self.cfg, "cafe"))

    def test_guard_content_size_change_changes_mark(self):
        before = read_cache._files_mark(self.cfg, "cafe")
        (self.room / "model_guard.json").write_text('{"incidents": [1]}', encoding="utf-8")
        self.assertNotEqual(before, read_cache._files_mark(self.cfg, "cafe"))


if __name__ == "__main__":
    unittest.main()
