"""Длинная диктовка (WAVE13 §2): лимит из настроек Hermes и части записи на диск.

Что проверяется:

* лимит записи берётся из ``voice.max_recording_seconds`` config.yaml Hermes
  (только чтение), ниже 10 минут не опускается, без значения — 600 с;
* части длинной записи сохраняются на диск по одной (обрыв браузера не уносит
  наговоренное), читаются обратно, перечисляются и удаляются;
* HTTP-маршруты ``/api/stt/recordings/*`` отвечают по контракту docs/API.md;
* каждая часть распознаётся отдельно — текст собирается по порядку, а сбой одной
  части не мешает остальным (проверка на заглушке `stt.transcribe`);
* настоящее распознавание нескольких частей — ``TestLongRecordingIntegration``
  (пропускается, если в окружении нет faster_whisper).

    python -m unittest tests.test_stt_long -v
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import app as app_mod                      # noqa: E402
from server import routes_stt, stt, stt_worker          # noqa: E402
from tests.helpers import Sandbox                       # noqa: E402

SCRATCH = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or tempfile.gettempdir())
FFMPEG = stt_worker.ffmpeg_path()
WAV = ROOT / "fixtures" / "dictation-ru.wav"

CONFIG_TEMPLATE = """\
stt:
  language: 'ru'
  local:
    model: large-v3-turbo
    device: cuda
    compute_type: float16
voice:
  max_recording_seconds: %s
"""


def _cfg_with_config(tmp: Path, text: str | None):
    """cfg с «домашней папкой Hermes» во временном каталоге (только чтение)."""
    class Cfg:
        pass
    home = Path(tmp) / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (home / "config.yaml").write_text(text, encoding="utf-8")
    cfg = Cfg()
    cfg.data_dir = Path(tmp) / "data"
    cfg.tmp_dir = cfg.data_dir / "tmp"
    cfg.hermes_home = home
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestRecordLimit(unittest.TestCase):
    """Лимит записи — из настроек Hermes, а не из своих констант."""

    def setUp(self):
        self.dir = SCRATCH / ("stt-limit-" + str(time.time_ns()))
        self.dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_hermes_value_is_used(self):
        cfg = _cfg_with_config(self.dir, CONFIG_TEMPLATE % 900)
        limit = stt.record_limit(cfg)
        self.assertEqual(limit["seconds"], 900)
        self.assertEqual(limit["source"], "hermes")
        self.assertEqual(limit["hermes_seconds"], 900)
        self.assertEqual(limit["setting"], "voice.max_recording_seconds")

    def test_short_hermes_limit_is_lifted_to_ten_minutes(self):
        """В Hermes по умолчанию 120 с — поле комнаты короче 10 минут не делаем."""
        cfg = _cfg_with_config(self.dir, CONFIG_TEMPLATE % 120)
        limit = stt.record_limit(cfg)
        self.assertEqual(limit["seconds"], 600)
        self.assertEqual(limit["hermes_seconds"], 120)
        self.assertEqual(limit["source"], "hermes", "источник значения всё равно Hermes")
        self.assertEqual(limit["minimum"], 600)

    def test_missing_value_falls_back_to_ten_minutes(self):
        cfg = _cfg_with_config(self.dir, "stt:\n  language: ru\n")
        limit = stt.record_limit(cfg)
        self.assertEqual(limit["seconds"], 600)
        self.assertEqual(limit["source"], "fallback")
        self.assertIsNone(limit["hermes_seconds"])

    def test_broken_and_absent_config(self):
        for text in (None, "", "voice: [не словарь", "voice:\n  max_recording_seconds: скоро\n"):
            with self.subTest(text=text):
                cfg = _cfg_with_config(self.dir / ("case-%d" % len(text or "")), text)
                limit = stt.record_limit(cfg)
                self.assertEqual(limit["seconds"], 600)
                self.assertEqual(limit["source"], "fallback")

    def test_stt_block_is_the_second_source(self):
        cfg = _cfg_with_config(self.dir / "sttblock",
                              "stt:\n  max_recording_seconds: 720\n")
        limit = stt.record_limit(cfg)
        self.assertEqual(limit["seconds"], 720)
        self.assertEqual(limit["setting"], "stt.max_recording_seconds")

    def test_part_seconds_from_env(self):
        with mock.patch.dict("os.environ", {"TEAMROOM_STT_PART_SECONDS": "45"}):
            self.assertEqual(stt.part_seconds(), 45.0)
            self.assertEqual(stt.record_limit(_cfg_with_config(self.dir, None))["part_seconds"], 45.0)
        with mock.patch.dict("os.environ", {"TEAMROOM_STT_PART_SECONDS": "1"}):
            self.assertEqual(stt.part_seconds(), 10.0, "часть короче 10 с не бывает")
        with mock.patch.dict("os.environ", {"TEAMROOM_STT_PART_SECONDS": "мусор"}):
            self.assertEqual(stt.part_seconds(), float(stt.DEFAULT_PART_SECONDS))

    def test_status_carries_the_limit(self):
        cfg = _cfg_with_config(self.dir / "status", CONFIG_TEMPLATE % 900)
        status = stt.status(cfg)
        self.assertEqual(status["max_recording_s"], 900)
        self.assertEqual(status["max_recording_source"], "hermes")
        self.assertEqual(status["max_recording_min_s"], 600)
        self.assertGreater(float(status["part_seconds"]), 0)


class TestRecordingStorage(unittest.TestCase):
    """Части записи на диске: сохранить, найти, прочитать, убрать."""

    def setUp(self):
        self.dir = SCRATCH / ("stt-parts-" + str(time.time_ns()))
        self.cfg = _cfg_with_config(self.dir, None)
        self.rid = "r_" + str(time.time_ns())[-10:]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_and_read_back(self):
        first = stt.save_part(self.cfg, self.rid, 1, b"a" * 3000, "audio/webm")
        second = stt.save_part(self.cfg, self.rid, 2, b"b" * 6000, "audio/webm")
        self.assertEqual(first["part"], 1)
        self.assertAlmostEqual(first["seconds_est"], 1.0, places=2)
        self.assertEqual(second["parts_total"], 2)

        view = stt.recording_view(self.cfg, self.rid)
        self.assertEqual(view["parts_total"], 2)
        self.assertEqual([row["n"] for row in view["parts"]], [1, 2])
        self.assertEqual(view["bytes"], 9000)
        self.assertGreater(view["seconds_est"], 2.9)

        payload, ctype = stt.read_part(self.cfg, self.rid, 2)
        self.assertEqual(payload, b"b" * 6000)
        self.assertEqual(ctype, "audio/webm")

        self.assertTrue(stt.drop_recording(self.cfg, self.rid))
        self.assertFalse(stt.recordings_root(self.cfg).joinpath(self.rid).exists())
        self.assertFalse(stt.drop_recording(self.cfg, self.rid))

    def test_same_part_is_replaced_not_duplicated(self):
        stt.save_part(self.cfg, self.rid, 1, b"a" * 1000, "audio/webm")
        stt.save_part(self.cfg, self.rid, 1, b"a" * 2000, "audio/webm")
        view = stt.recording_view(self.cfg, self.rid)
        self.assertEqual(view["parts_total"], 1)
        self.assertEqual(view["bytes"], 2000)

    def test_invalid_ids_and_numbers(self):
        for rid in ("", "x", "../hermes", "R_ПРИВЕТ", "a" * 60):
            with self.subTest(rid=rid):
                with self.assertRaises(stt.SttError) as ctx:
                    stt.save_part(self.cfg, rid, 1, b"a" * 10, "audio/webm")
                self.assertEqual(ctx.exception.status, 400)
        for number in (0, -1, 99, "мусор"):
            with self.subTest(part=number):
                with self.assertRaises(stt.SttError) as ctx:
                    stt.save_part(self.cfg, self.rid, number, b"a" * 10, "audio/webm")
                self.assertEqual(ctx.exception.status, 400)

    def test_empty_and_foreign_type_and_limit(self):
        with self.assertRaises(stt.SttError) as ctx:
            stt.save_part(self.cfg, self.rid, 1, b"", "audio/webm")
        self.assertEqual(ctx.exception.code, "audio_empty")
        with self.assertRaises(stt.SttError) as ctx:
            stt.save_part(self.cfg, self.rid, 1, b"a" * 10, "text/plain")
        self.assertEqual(ctx.exception.code, "bad_request")
        with mock.patch.object(stt, "MAX_PART_BYTES", 100):
            with self.assertRaises(stt.SttError) as ctx:
                stt.save_part(self.cfg, self.rid, 1, b"a" * 200, "audio/webm")
            self.assertEqual(ctx.exception.status, 413)

    def test_unknown_part_is_404(self):
        stt.save_part(self.cfg, self.rid, 1, b"a" * 100, "audio/webm")
        with self.assertRaises(stt.SttError) as ctx:
            stt.read_part(self.cfg, self.rid, 2)
        self.assertEqual(ctx.exception.status, 404)

    def test_prune_drops_old_recordings(self):
        old = self.cfg.data_dir / "stt-recordings" / "r_old_000001"
        old.mkdir(parents=True, exist_ok=True)
        (old / "recording.json").write_text("{}", encoding="utf-8")
        stt.save_part(self.cfg, self.rid, 1, b"a" * 100, "audio/webm")
        stamp = time.time() - 100 * 3600
        for path in (old, old / "recording.json"):
            try:
                import os
                os.utime(path, (stamp, stamp))
            except OSError:
                pass
        removed = stt.prune_recordings(self.cfg, force=True)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(stt.recording_dir(self.cfg, self.rid).is_dir())

    def test_list_recordings_newest_first(self):
        for rid in ("r_aaa_000001", "r_bbb_000002"):
            stt.save_part(self.cfg, rid, 1, b"a" * 100, "audio/webm")
            time.sleep(0.01)
        rows = stt.list_recordings(self.cfg, limit=5)
        self.assertEqual([row["rid"] for row in rows][:2], ["r_bbb_000002", "r_aaa_000001"])


class LongRecordingHttpCase(unittest.TestCase):
    """Живой сервер в песочнице: маршруты частей и распознавание по частям."""

    @classmethod
    def setUpClass(cls):
        cls.sandbox = Sandbox()
        cls.sb = cls.sandbox.__enter__()
        cls.cfg = cls.sb.cfg
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.calls: list = []

        def fake_transcribe(cfg, audio, content_type="audio/webm", *, language=None):
            cls.calls.append({"size": len(audio), "content_type": content_type})
            if len(audio) == 7:                      # «битая» часть
                raise stt.SttError("stt_failed", 500, "Распознавание не получилось",
                                   {"reason": "fake"})
            return {"text": "часть %d" % len(audio), "raw": "часть %d" % len(audio),
                    "duration_s": 2.0, "elapsed_s": 0.01, "language": "ru",
                    "model": "fake", "device": "cpu", "corrections": []}

        cls._orig = stt.transcribe
        stt.transcribe = fake_transcribe

    @classmethod
    def tearDownClass(cls):
        stt.transcribe = cls._orig
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        stt.shutdown()
        cls.sandbox.__exit__(None, None, None)

    def setUp(self):
        self.calls.clear()
        self.rid = "r_http_%d" % (int(time.time_ns()) % 10 ** 9)

    def call(self, method, path, body=None, ctype="application/json", raw=False, timeout=60):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        head = {"Host": "127.0.0.1:%d" % self.port}
        payload = None
        if body is not None:
            head["Content-Type"] = ctype
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            head["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=head)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if raw:
            return resp.status, data, dict(resp.getheaders())
        try:
            return resp.status, json.loads(data.decode("utf-8") or "{}"), dict(resp.getheaders())
        except ValueError:
            return resp.status, data, dict(resp.getheaders())

    def test_status_has_limit_for_ui(self):
        code, payload, _ = self.call("GET", "/api/stt/status")
        self.assertEqual(code, 200)
        self.assertIn("max_recording_s", payload)
        self.assertGreaterEqual(int(payload["max_recording_s"]), 600)
        self.assertGreater(float(payload["part_seconds"]), 0)

    def test_parts_roundtrip_and_transcribe(self):
        base = "/api/stt/recordings/" + self.rid
        sizes = [3, 5, 6]
        for number, size in enumerate(sizes, start=1):
            code, payload, _ = self.call("PUT", "%s/parts/%d" % (base, number),
                                         b"a" * size, "audio/webm;codecs=opus")
            self.assertEqual(code, 200, payload)
            self.assertEqual(payload["part"], number)
            self.assertEqual(payload["bytes"], size)

        code, view, _ = self.call("GET", base)
        self.assertEqual(code, 200)
        self.assertEqual(view["parts_total"], 3)
        self.assertEqual([row["n"] for row in view["parts"]], [1, 2, 3])

        code, listing, _ = self.call("GET", "/api/stt/recordings?limit=5")
        self.assertEqual(code, 200)
        self.assertIn(self.rid, [row["rid"] for row in listing["items"]])

        # клиент распознаёт части по порядку — текст собирается, как в интерфейсе
        texts = []
        for number in range(1, 4):
            code, audio, headers = self.call("GET", "%s/parts/%d" % (base, number), raw=True)
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Content-Type"), "audio/webm")
            code, answer, _ = self.call("POST", "/api/stt/transcribe", audio,
                                        "audio/webm;codecs=opus")
            self.assertEqual(code, 200, answer)
            texts.append(answer["text"])
        self.assertEqual(texts, ["часть 3", "часть 5", "часть 6"])

        code, payload, _ = self.call("DELETE", base)
        self.assertEqual(code, 200)
        self.assertTrue(payload["removed"])
        code, view, _ = self.call("GET", base)
        self.assertEqual(view["parts_total"], 0, "после удаления частей нет")

    def test_one_broken_part_does_not_block_the_rest(self):
        base = "/api/stt/recordings/" + self.rid
        for number, size in enumerate((3, 7, 6), start=1):
            self.call("PUT", "%s/parts/%d" % (base, number), b"a" * size, "audio/webm")
        got = []
        for number in range(1, 4):
            code, audio, _ = self.call("GET", "%s/parts/%d" % (base, number), raw=True)
            code, answer, _ = self.call("POST", "/api/stt/transcribe", audio, "audio/webm")
            got.append((code, (answer or {}).get("text") if isinstance(answer, dict) else ""))
        self.assertEqual(got[0][0], 200)
        self.assertEqual(got[1][0], 500, "битая часть — ошибка только этой части")
        self.assertEqual(got[2][0], 200)
        self.assertEqual([text for code, text in got if code == 200], ["часть 3", "часть 6"])

    def test_delete_removes_recording_for_good(self):
        """«Убрать» в интерфейсе = DELETE: записи нет ни в списке, ни в частях.

        Иначе при следующем открытии панели серверная копия (72 часа) снова
        предложила бы «Есть нерасшифрованная запись» — то самое «убрал, а оно
        снова висит». Повторный DELETE не ошибка: строку можно убирать дважды.
        """
        base = "/api/stt/recordings/" + self.rid
        for number, size in enumerate((4, 5), start=1):
            code, _, _ = self.call("PUT", "%s/parts/%d" % (base, number),
                                   b"a" * size, "audio/webm")
            self.assertEqual(code, 200)
        code, listing, _ = self.call("GET", "/api/stt/recordings?limit=50")
        self.assertIn(self.rid, [row["rid"] for row in listing["items"]])

        code, payload, _ = self.call("DELETE", base)
        self.assertEqual(code, 200)
        self.assertTrue(payload["removed"])

        code, listing, _ = self.call("GET", "/api/stt/recordings?limit=50")
        self.assertEqual(code, 200)
        self.assertNotIn(self.rid, [row["rid"] for row in listing["items"]],
                         "удалённой записи в списке нет — всплыть нечему")
        code, view, _ = self.call("GET", base)
        self.assertEqual(view["parts_total"], 0)
        for number in (1, 2):
            code, _, _ = self.call("GET", "%s/parts/%d" % (base, number), raw=True)
            self.assertEqual(code, 404, "частей удалённой записи нет")

        code, again, _ = self.call("DELETE", base)
        self.assertEqual(code, 200)
        self.assertFalse(again["removed"], "повторное «Убрать» — не ошибка")

    def test_bad_requests(self):
        # путь с несуществующим по форме номером — это чужой маршрут, а не аудио
        code, payload, _ = self.call("PUT", "/api/stt/recordings/nope/parts/1",
                                     b"a" * 10, "audio/webm")
        self.assertEqual(code, 404, payload)
        # номер части вне диапазона — понятная ошибка, а не 500
        code, payload, _ = self.call("PUT", "/api/stt/recordings/" + self.rid + "/parts/199",
                                     b"a" * 10, "audio/webm")
        self.assertEqual(code, 400, payload)
        self.assertEqual(payload["error"]["code"], "bad_request")
        code, payload, _ = self.call("GET", "/api/stt/recordings?limit=abc")
        self.assertEqual(code, 400, payload)
        code, payload, _ = self.call("PUT", "/api/stt/recordings/" + self.rid + "/parts/1",
                                     b"", "audio/webm")
        self.assertEqual(code, 400, payload)
        self.assertEqual(payload["error"]["code"], "audio_empty")
        code, payload, _ = self.call("GET", "/api/stt/recordings/" + self.rid + "/parts/199",
                                     raw=True)
        self.assertEqual(code, 404)

    def test_body_policy_is_binary_and_big(self):
        for path in ("/api/stt/transcribe",
                     "/api/stt/recordings/r_abc12345/parts/2"):
            policy = routes_stt.body_policy(path)
            self.assertTrue(policy.get("binary"), path)
            self.assertEqual(policy["limit"], routes_stt.STT_MAX_BODY)
        self.assertEqual(routes_stt.body_policy("/api/stt/status"), {})


class TestLongRecordingIntegration(unittest.TestCase):
    """Настоящее распознавание нескольких частей (нужен faster_whisper и ffmpeg)."""

    @classmethod
    def setUpClass(cls):
        if not FFMPEG:
            raise unittest.SkipTest("нет ffmpeg")
        cls.python = stt_worker.find_python()
        if not cls.python:
            raise unittest.SkipTest("нет интерпретатора с faster_whisper")
        cls.dir = SCRATCH / ("stt-long-" + str(time.time_ns()))
        cls.dir.mkdir(parents=True, exist_ok=True)
        cls.sandbox = Sandbox()
        cls.sb = cls.sandbox.__enter__()
        cls.cfg = cls.sb.cfg
        cls.rid = "r_int_%d" % (int(time.time_ns()) % 10 ** 9)

    @classmethod
    def tearDownClass(cls):
        stt.reset_worker()
        shutil.rmtree(cls.dir, ignore_errors=True)
        cls.sandbox.__exit__(None, None, None)

    def _make_part(self, name: str, seconds: float) -> bytes:
        """Кусок речи нужной длины (повтор фикстуры) в webm/opus."""
        out = self.dir / name
        res = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-stream_loop", "-1",
             "-i", str(WAV), "-t", str(seconds), "-c:a", "libopus", "-b:a", "32k",
             str(out)], capture_output=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr[-300:])
        return out.read_bytes()

    def test_two_parts_are_recognized_in_order(self):
        """Запись из двух частей: каждая распознаётся, текст собирается по порядку."""
        parts = [self._make_part("part1.webm", 6), self._make_part("part2.webm", 6)]
        for number, payload in enumerate(parts, start=1):
            saved = stt.save_part(self.cfg, self.rid, number, payload, "audio/webm")
            self.assertEqual(saved["part"], number)
            self.assertGreater(saved["seconds_est"], 1.0)
        texts = []
        for number in range(1, 3):
            payload, ctype = stt.read_part(self.cfg, self.rid, number)
            answer = stt.transcribe(self.cfg, payload, ctype)
            self.assertTrue(answer["text"].strip(), "часть %d распознана" % number)
            texts.append(answer["text"])
        whole = " ".join(texts)
        self.assertGreater(len(whole), 20)
        self.assertTrue(stt.drop_recording(self.cfg, self.rid))

    def test_six_minute_recording_keeps_all_parts(self):
        """Запись на 6 минут (как у Михаила) целиком на диске, последняя часть читается."""
        total = 0.0
        for number in range(1, 4):
            payload = self._make_part("long%d.webm" % number, 120)
            saved = stt.save_part(self.cfg, self.rid, number, payload, "audio/webm")
            total += saved["seconds_est"]
        self.assertGreater(total, 340.0, "в сумме около шести минут")
        view = stt.recording_view(self.cfg, self.rid)
        self.assertEqual(view["parts_total"], 3)
        self.assertGreater(view["seconds_est"], 340.0)
        payload, ctype = stt.read_part(self.cfg, self.rid, 3)
        answer = stt.transcribe(self.cfg, payload, ctype)
        self.assertGreater(answer["duration_s"], 100.0, "распознан целый двухминутный кусок")
        self.assertTrue(answer["text"].strip())
        stt.drop_recording(self.cfg, self.rid)


if __name__ == "__main__":
    unittest.main()
