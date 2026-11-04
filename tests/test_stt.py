"""Диктовка: словарь, замены, обучение, подсказка, поиск интерпретатора, HTTP.

Быстрые тесты не поднимают модель: маршруты проверяются с подменой
``server.stt.transcribe``. Настоящее распознавание (Windows SAPI → wav → webm/opus
→ POST /api/stt/transcribe) — в ``TestSttIntegration``; он пропускается, если в
окружении нет faster_whisper.

    python -m unittest tests.test_stt -v
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import app as app_mod                      # noqa: E402
from server import http_util, stt, stt_worker, routes_stt   # noqa: E402
from tests.helpers import Sandbox                      # noqa: E402

SAPI_TEXT = "Привет, это проверка распознавания речи. Гранит и лицензия на добычу."
SCRATCH = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or ".")
FFMPEG = stt_worker.ffmpeg_path()


def _fake_cfg(data_dir: Path, hermes_home: Path | None = None):
    """Минимальный cfg для чистых функций (без песочницы и канбана)."""
    class Cfg:
        pass
    cfg = Cfg()
    cfg.data_dir = Path(data_dir)
    cfg.tmp_dir = Path(data_dir) / "tmp"
    cfg.hermes_home = Path(hermes_home or ROOT / "no-hermes")
    return cfg


class TestSettings(unittest.TestCase):
    """Конфиг модели: PyYAML и запасной разбор блока ``stt:``."""

    def test_fallback_matches_yaml_on_real_config(self):
        try:
            import yaml                                          # noqa: PLC0415
        except ImportError:                                      # pragma: no cover
            self.skipTest("PyYAML недоступен")
        path = Path(r"C:/Users/User/AppData/Local/hermes/config.yaml")
        if not path.is_file():
            self.skipTest("нет реального config.yaml")
        text = path.read_text(encoding="utf-8")
        expected = (yaml.safe_load(text) or {}).get("stt") or {}
        self.assertEqual(stt.parse_stt_block(text), expected)

    def test_fallback_parses_quoted_and_bool(self):
        text = ("model:\n  default: x\nstt:\n  language: 'ru'\n  local:\n"
                "    model: large-v3-turbo\n    device: cuda\n    compute_type: float16\n"
                "    vad: false\n    initial_prompt: \"Гранит, TaskFlow\"\nvoice:\n  auto_tts: true\n")
        block = stt.parse_stt_block(text)
        self.assertEqual(block["local"]["model"], "large-v3-turbo")
        self.assertEqual(block["local"]["initial_prompt"], "Гранит, TaskFlow")
        self.assertIs(block["local"]["vad"], False)
        self.assertEqual(block["language"], "ru")

    def test_missing_config_gives_defaults(self):
        with self.subTest("нет файла"):
            settings = stt.read_settings(_fake_cfg(SCRATCH, ROOT / "nope"))
            self.assertEqual(settings["model"], "large-v3-turbo")
            self.assertEqual(settings["device"], "cuda")
            self.assertEqual(settings["language"], "ru")
            self.assertEqual(settings["initial_prompt"], "")


class TestDictionary(unittest.TestCase):
    """Валидация, схлопывание дублей и атомарная запись словаря."""

    def setUp(self):
        self.dir = Path(SCRATCH) / ("stt-dict-" + str(time.time_ns()))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = _fake_cfg(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_empty_when_file_missing(self):
        self.assertEqual(stt.get_dictionary(self.cfg),
                         {"terms": [], "replacements": [], "updated_at": 0})

    def test_put_get_roundtrip_and_dedupe(self):
        doc = stt.put_dictionary(self.cfg, {
            "terms": ["Гранит", "гранит", " TaskFlow ", ""],
            "replacements": [{"from": "Тескфло", "to": "TaskFlow"},
                             {"from": "тескфло", "to": "taskflow"}]})
        self.assertEqual(doc["terms"], ["Гранит", "TaskFlow"])
        self.assertEqual(len(doc["replacements"]), 1)
        self.assertEqual(doc["replacements"][0]["count"], 2)
        self.assertGreater(doc["updated_at"], 0)
        self.assertEqual(stt.get_dictionary(self.cfg)["replacements"], doc["replacements"])

    def test_duplicate_count_is_summed(self):
        doc = stt.put_dictionary(self.cfg, {"terms": [], "replacements": [
            {"from": "а", "to": "б", "count": 3}, {"from": "А", "to": "Б", "count": 4}]})
        self.assertEqual(len(doc["replacements"]), 1)
        self.assertEqual(doc["replacements"][0]["count"], 7)

    def test_validation_limits(self):
        cases = [
            ({"terms": ["я" * 61]}, "terms"),
            ({"terms": ["т%d" % i for i in range(301)]}, "terms"),
            ({"replacements": [{"from": "а", "to": "А"}]}, "replacements"),
            ({"replacements": [{"from": "", "to": "б"}]}, "replacements"),
            ({"replacements": [{"from": "а", "to": ""}]}, "replacements"),
            ({"replacements": [{"from": "а%d" % i, "to": "б%d" % i} for i in range(201)]},
             "replacements"),
            ({"terms": "не список"}, "terms"),
            ({"replacements": ["строка"]}, "replacements"),
        ]
        for payload, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(stt.SttError) as ctx:
                    stt.put_dictionary(self.cfg, payload)
                self.assertEqual(ctx.exception.code, "bad_request")
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.detail.get("field"), field)

    def test_partial_put_keeps_other_half(self):
        first = stt.put_dictionary(self.cfg, {"terms": ["Гранит"],
                                              "replacements": [{"from": "а", "to": "б"}]})
        second = stt.put_dictionary(self.cfg, {"terms": ["Гранит", "TaskFlow"]})
        self.assertEqual(second["terms"], ["Гранит", "TaskFlow"])
        self.assertEqual(len(second["replacements"]), 1)
        self.assertEqual(first["replacements"], second["replacements"])

    def test_corrupt_file_does_not_break(self):
        path = stt.dict_path(self.cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"terms": [битый json', encoding="utf-8")
        self.assertEqual(stt.get_dictionary(self.cfg)["terms"], [])
        doc = stt.put_dictionary(self.cfg, {"terms": ["Гранит"]})
        self.assertEqual(doc["terms"], ["Гранит"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["terms"], ["Гранит"])

    def test_write_is_atomic_and_leaves_no_tmp(self):
        stt.put_dictionary(self.cfg, {"terms": ["Гранит"]})
        leftovers = [p.name for p in stt.dict_path(self.cfg).parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_replace_on_windows_retries_permission_error(self):
        target = self.dir / "target.json"
        target.write_text("{}", encoding="utf-8")
        tmp = self.dir / "tmp.json"
        tmp.write_text("{}", encoding="utf-8")
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("файл занят читателем")
            return real(src, dst)

        with mock.patch("server.stt.os.replace", side_effect=flaky):
            stt._replace_with_retry(tmp, target, attempts=3)
        self.assertEqual(calls["n"], 2)


class TestReplacements(unittest.TestCase):
    """Замены после распознавания: регистр, границы слов, длинные фразы."""

    def test_case_of_first_letter_is_kept(self):
        """Заглавная в начале фразы и КАПС сохраняются; строчное — как в словаре."""
        rules = [{"from": "тескфло", "to": "TaskFlow"}, {"from": "гранит", "to": "гранит"}]
        self.assertEqual(stt.apply_replacements("Тескфло готов", rules[:1])[0], "TaskFlow готов")
        self.assertEqual(stt.apply_replacements("тескфло готов", rules[:1])[0], "TaskFlow готов")
        self.assertEqual(stt.apply_replacements("ТЕСКФЛО готов", rules[:1])[0], "TASKFLOW готов")
        self.assertEqual(stt.apply_replacements("Гранит и гранит", rules)[0], "Гранит и гранит")

    def test_word_boundaries(self):
        rules = [{"from": "гранит", "to": "Гранит"}]
        text = "гранит, недропользование и поднедровье"
        self.assertEqual(stt.apply_replacements(text, rules)[0],
                         "Гранит, недропользование и поднедровье")

    def test_multiword_rule_and_longest_first(self):
        rules = [{"from": "гранит", "to": "N1"}, {"from": "гранит управление", "to": "Гранит-управление"}]
        text, applied = stt.apply_replacements("гранит управление и гранит", rules)
        self.assertEqual(text, "Гранит-управление и N1")
        self.assertEqual([item["from"] for item in applied], ["гранит управление", "гранит"])

    def test_no_match_returns_same_text(self):
        text = "привет мир"
        self.assertEqual(stt.apply_replacements(text, [{"from": "гранит", "to": "x"}]), (text, []))

    def test_disabled_or_empty_rules_are_ignored(self):
        self.assertEqual(stt.apply_replacements("текст", [{"from": "", "to": "x"}]), ("текст", []))
        self.assertEqual(stt.apply_replacements("текст", None), ("текст", []))


class TestPrompt(unittest.TestCase):
    """Подсказка распознаванию: базовая строка + термины, не длиннее 800."""

    def setUp(self):
        self.dir = Path(SCRATCH) / ("stt-prompt-" + str(time.time_ns()))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = _fake_cfg(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_terms_are_added(self):
        stt.put_dictionary(self.cfg, {"terms": ["Гранит", "TaskFlow"]})
        prompt = stt.build_prompt(self.cfg)
        self.assertIn("Гранит", prompt)
        self.assertIn("TaskFlow", prompt)

    def test_prompt_is_truncated_and_new_terms_win(self):
        long_base = "Очень длинная подсказка. " * 60          # ~1400 символов
        with mock.patch.object(stt, "read_settings",
                               return_value={"initial_prompt": long_base, "model": "m",
                                             "device": "d", "compute_type": "c",
                                             "vad": True, "language": "ru"}):
            terms = ["Старый%d" % i for i in range(30)] + ["Самый новый"]
            prompt = stt.build_prompt(self.cfg, terms=terms)
        self.assertLessEqual(len(prompt), stt.PROMPT_LIMIT)
        self.assertIn("Самый новый", prompt)

    def test_no_terms_returns_base(self):
        with mock.patch.object(stt, "read_settings",
                               return_value={"initial_prompt": "База", "model": "m", "device": "d",
                                             "compute_type": "c", "vad": True, "language": "ru"}):
            self.assertEqual(stt.build_prompt(self.cfg, terms=[]), "База")


class TestLearn(unittest.TestCase):
    """Обучение на правке: только замены 1–3 слов, пунктуация не в счёт."""

    def setUp(self):
        self.dir = Path(SCRATCH) / ("stt-learn-" + str(time.time_ns()))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = _fake_cfg(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_single_word_replacement(self):
        result = stt.learn(self.cfg, "задача в тескфло готова", "задача в TaskFlow готова")
        self.assertEqual(result["suggestions"], [{"from": "тескфло", "to": "TaskFlow"}])
        self.assertEqual(result["saved"], [])

    def test_punctuation_and_case_are_not_suggested(self):
        result = stt.learn(self.cfg, "Привет мир как дела", "привет, мир — как дела!")
        self.assertEqual(result["suggestions"], [])

    def test_long_phrase_edits_are_ignored(self):
        raw = "первое второе третье четвёртое пятое"
        final = "совсем другой текст"
        self.assertEqual(stt.learn(self.cfg, raw, final)["suggestions"], [])

    def test_two_word_replacement(self):
        result = stt.learn(self.cfg, "открыть недро пользование", "открыть недропользование")
        self.assertEqual(result["suggestions"], [{"from": "недро пользование", "to": "недропользование"}])

    def test_apply_saves_replacement_and_term(self):
        result = stt.learn(self.cfg, "в тескфло всё готово", "в TaskFlow всё готово", apply=True)
        self.assertEqual(result["saved"], [{"from": "тескфло", "to": "TaskFlow"}])
        doc = stt.get_dictionary(self.cfg)
        self.assertIn("TaskFlow", doc["terms"])
        self.assertEqual(doc["replacements"][0]["from"], "тескфло")
        self.assertEqual(doc["replacements"][0]["to"], "TaskFlow")
        # повторное обучение не плодит запись, а увеличивает счётчик
        stt.learn(self.cfg, "в тескфло всё готово", "в TaskFlow всё готово", apply=True)
        doc = stt.get_dictionary(self.cfg)
        self.assertEqual(len(doc["replacements"]), 1)
        self.assertEqual(doc["replacements"][0]["count"], 2)

    def test_bad_arguments_raise(self):
        with self.assertRaises(stt.SttError):
            stt.learn(self.cfg, None, "текст")
        with self.assertRaises(stt.SttError):
            stt.learn(self.cfg, "а" * (stt.MAX_LEARN_TEXT + 1), "б")


class TestDiscovery(unittest.TestCase):
    """Поиск интерпретатора с faster_whisper и окружение воркера."""

    def test_returns_first_candidate_that_passes_probe(self):
        probed = []

        def probe(path):
            probed.append(path)
            return path.endswith("good.exe")

        found = stt_worker.find_python(candidates=["bad.exe", "good.exe", "later.exe"], probe=probe)
        self.assertEqual(found, "good.exe")
        self.assertEqual(probed, ["bad.exe", "good.exe"])

    def test_none_when_nothing_has_package(self):
        self.assertIsNone(stt_worker.find_python(candidates=["a", "b"], probe=lambda _p: False))

    def test_result_is_cached_in_memory(self):
        calls = []

        def probe(path):
            calls.append(path)
            return True

        stt_worker.find_python(candidates=None, probe=probe, refresh=True)
        first = len(calls)
        stt_worker.find_python()
        self.assertEqual(len(calls), first, "повторный поиск не должен запускать проверки")
        stt_worker.reset_lookup()

    def test_candidates_start_with_current_interpreter(self):
        candidates = stt_worker.python_candidates()
        self.assertEqual(candidates[0], sys.executable)

    def test_real_environment_has_python(self):
        found = stt_worker.find_python(refresh=True)
        self.assertTrue(found and Path(found).is_file(), "нет интерпретатора с faster-whisper")
        self.assertTrue(stt_worker.has_faster_whisper(found))

    def test_worker_env_adds_hermes_bin(self):
        env = stt_worker.worker_env({"PATH": "C:/Windows", "KEEP": "1"})
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(stt_worker.bin_dir()))
        self.assertIn("C:/Windows", env["PATH"])
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")


class TestStatusAndErrors(unittest.TestCase):
    """status() и коды SttError без запуска модели."""

    def test_transport_constants_match_module(self):
        """Маршрут и модуль не должны разойтись в лимите и типах аудио."""
        self.assertEqual(routes_stt.STT_MAX_BODY, stt.MAX_AUDIO)
        self.assertEqual(routes_stt.AUDIO_TYPES, stt.AUDIO_TYPES)

    def setUp(self):
        self.dir = Path(SCRATCH) / ("stt-status-" + str(time.time_ns()))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = _fake_cfg(self.dir)

    def tearDown(self):
        stt.reset_worker()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_status_shape(self):
        status = stt.status(self.cfg)
        self.assertEqual(set(status), {"available", "loaded", "loading", "model", "device",
                                     "mode", "source", "provider", "reason"})
        self.assertIsInstance(status["available"], bool)
        self.assertFalse(status["loaded"])
        self.assertEqual(status["model"], "large-v3-turbo")

    def test_status_unavailable_without_python(self):
        with mock.patch.object(stt_worker, "find_python", return_value=None):
            status = stt.status(self.cfg)
        self.assertFalse(status["available"])
        self.assertIn("faster-whisper", status["reason"])

    def test_empty_audio(self):
        with self.assertRaises(stt.SttError) as ctx:
            stt.transcribe(self.cfg, b"")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("audio_empty", 400))
        self.assertIn("Пустое аудио", ctx.exception.message_ru)

    def test_audio_too_big_before_worker(self):
        with self.assertRaises(stt.SttError) as ctx:
            stt.transcribe(self.cfg, b"x" * (stt.MAX_AUDIO + 1))
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("audio_too_big", 413))

    def test_unavailable_without_interpreter(self):
        with mock.patch.object(stt_worker, "find_python", return_value=None):
            with self.assertRaises(stt.SttError) as ctx:
                stt.transcribe(self.cfg, b"\x1aE\xdf\xa3" + b"0" * 100, "audio/webm")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("stt_unavailable", 503))
        self.assertIn("Распознавание речи не настроено", ctx.exception.message_ru)

    def test_timeout_formula(self):
        self.assertEqual(stt._timeout_for(0, ".webm"), 60.0)            # не меньше минуты
        self.assertEqual(stt._timeout_for(30000, ".webm"), 60.0)          # 10 с аудио
        self.assertEqual(stt._timeout_for(16000 * 2 * 100, ".wav"), 300.0)  # 100 с аудио
        self.assertGreater(stt._timeout_for(30000, ".webm", cold=True), 60.0)


class SttHttpCase(unittest.TestCase):
    """Песочница + живой сервер; модель не поднимается (transcribe подменён)."""

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
            cls.calls.append({"size": len(audio), "content_type": content_type,
                              "language": language})
            return {"text": "готово", "raw": "готово", "duration_s": 1.0, "elapsed_s": 0.1,
                    "language": language or "ru", "model": "fake", "device": "cpu",
                    "corrections": []}

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

    # --- помощники
    def call(self, method, path, body=None, ctype="application/json", headers=None,
             raw=False, timeout=60):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        head = {"Host": "127.0.0.1:%d" % self.port}
        payload = None
        if body is not None:
            head["Content-Type"] = ctype
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            head["Content-Length"] = str(len(payload))
        head.update(headers or {})
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

    def setUp(self):
        self.calls.clear()

    # --- статус
    def test_status_ok(self):
        status, payload, headers = self.call("GET", "/api/stt/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"available", "loaded", "loading", "model", "device",
                                        "mode", "source", "provider", "reason"})
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    # --- распознавание
    def test_transcribe_accepts_binary_body(self):
        audio = b"\x1aE\xdf\xa3" + bytes(range(256)) * 4      # не JSON — и не должен разбираться
        status, payload, _ = self.call("POST", "/api/stt/transcribe?language=ru", audio,
                                       ctype="audio/webm")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["text"], "готово")
        self.assertEqual(self.calls[-1]["size"], len(audio))
        self.assertEqual(self.calls[-1]["content_type"], "audio/webm")
        self.assertEqual(self.calls[-1]["language"], "ru")

    def test_transcribe_without_language(self):
        status, _payload, _ = self.call("POST", "/api/stt/transcribe", b"abc", ctype="audio/ogg")
        self.assertEqual(status, 200)
        self.assertIsNone(self.calls[-1]["language"])

    def test_transcribe_rejects_non_audio(self):
        status, payload, _ = self.call("POST", "/api/stt/transcribe", b"{}", ctype="application/json")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "bad_request")
        self.assertEqual(self.calls, [])

    def test_transcribe_rejects_bad_language(self):
        status, payload, _ = self.call("POST", "/api/stt/transcribe?language=../../x", b"abc",
                                       ctype="audio/webm")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["detail"]["field"], "language")

    def test_transcribe_empty_body(self):
        status, payload, _ = self.call("POST", "/api/stt/transcribe", b"", ctype="audio/webm")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "audio_empty")
        self.assertIn("Пустое аудио", payload["error"]["message"])

    def test_transcribe_body_limit_64_mib(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest("POST", "/api/stt/transcribe", skip_accept_encoding=True)
        conn.putheader("Host", "127.0.0.1:%d" % self.port)
        conn.putheader("Content-Type", "audio/webm")
        conn.putheader("Content-Length", str(70 * 1024 * 1024))
        conn.endheaders()
        conn.send(b"x" * 64)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 413)
        self.assertEqual(payload["error"]["code"], "audio_too_big")
        self.assertEqual(payload["error"]["detail"]["limit"], stt.MAX_AUDIO)
        self.assertIn("64 МиБ", payload["error"]["message"])

    def test_json_routes_keep_8_mib_limit(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest("POST", "/api/open", skip_accept_encoding=True)
        conn.putheader("Host", "127.0.0.1:%d" % self.port)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(http_util.MAX_BODY + 10))
        conn.endheaders()
        conn.send(b"{")
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 413)
        self.assertEqual(payload["error"]["code"], "bad_request")
        self.assertEqual(payload["error"]["detail"]["limit"], 8 * 1024 * 1024)

    def test_transcribe_wrong_method(self):
        status, payload, _headers = self.call("GET", "/api/stt/transcribe")
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "bad_request")
        self.assertIn("Метод GET", payload["error"]["message"])

    def test_transcribe_error_from_module(self):
        def broken(cfg, audio, content_type="audio/webm", *, language=None):
            raise stt.SttError("stt_failed", 500, "Распознавание речи упало: тест")

        with mock.patch.object(stt, "transcribe", broken):
            status, payload, _ = self.call("POST", "/api/stt/transcribe", b"abc", ctype="audio/webm")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "stt_failed")
        self.assertEqual(payload["error"]["message"], "Распознавание речи упало: тест")

    # --- словарь
    def test_dictionary_roundtrip(self):
        status, payload, _ = self.call("PUT", "/api/stt/dictionary",
                                       {"terms": ["Гранит"], "replacements": []})
        self.assertEqual(status, 200)
        self.assertEqual(payload["terms"], ["Гранит"])
        status, payload, _ = self.call("GET", "/api/stt/dictionary")
        self.assertEqual(status, 200)
        self.assertEqual(payload["terms"], ["Гранит"])
        self.assertIn("updated_at", payload)

    def test_dictionary_rejects_bad_payload(self):
        for body in ({"terms": ["я" * 61]},
                     {"replacements": [{"from": "а", "to": "а"}]},
                     ["not", "object"]):
            with self.subTest(body=str(body)[:30]):
                status, payload, _ = self.call("PUT", "/api/stt/dictionary", body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "bad_request")

    # --- обучение
    def test_learn_endpoint(self):
        status, payload, _ = self.call("POST", "/api/stt/learn",
                                       {"raw": "в тескфло всё", "final": "в TaskFlow всё"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["suggestions"], [{"from": "тескфло", "to": "TaskFlow"}])
        self.assertEqual(payload["saved"], [])

    def test_learn_validation(self):
        cases = [({"final": "текст"}, "raw"),
                 ({"raw": "текст"}, "final"),
                 ({"raw": "а", "final": "б", "apply": "да"}, "apply")]
        for body, field in cases:
            with self.subTest(field=field):
                status, payload, _ = self.call("POST", "/api/stt/learn", body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["detail"]["field"], field)

    # --- безопасность транспорта
    def test_host_and_origin_checks(self):
        status, payload, _ = self.call("GET", "/api/stt/status",
                                       headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["detail"]["reason"], "bad_host")
        status, payload, _ = self.call("POST", "/api/stt/learn", {"raw": "а", "final": "б"},
                                       headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["detail"]["reason"], "bad_origin")
        status, _payload, _ = self.call("POST", "/api/stt/learn", {"raw": "а", "final": "б"},
                                        headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)


@unittest.skipUnless(Path(stt_worker.find_python() or "").is_file() and not stt_worker.IMPORT_ERROR,
                     "нет интерпретатора с faster-whisper")
class TestSttIntegration(unittest.TestCase):
    """Настоящее распознавание: SAPI → wav → webm/opus → POST /api/stt/transcribe."""

    @classmethod
    def setUpClass(cls):
        cls.work = Path(SCRATCH) / ("stt-real-" + str(time.time_ns()))
        cls.work.mkdir(parents=True, exist_ok=True)
        cls.wav = cls.work / "speech.wav"
        if not cls._sapi(cls.wav):
            raise unittest.SkipTest("Windows SAPI недоступен")
        cls.webm = cls.work / "speech.webm"
        cls.silence = cls.work / "silence.webm"
        if FFMPEG:
            cls._ffmpeg("-i", str(cls.wav), "-c:a", "libopus", "-b:a", "24k", str(cls.webm))
            cls._ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "3",
                        str(cls.work / "silence.wav"))
            cls._ffmpeg("-i", str(cls.work / "silence.wav"), "-c:a", "libopus",
                        "-b:a", "24k", str(cls.silence))
        cls.sandbox = Sandbox()
        cls.sb = cls.sandbox.__enter__()
        cls.cfg = cls.sb.cfg
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)
        finally:
            stt.shutdown()
            cls.sandbox.__exit__(None, None, None)
            shutil.rmtree(cls.work, ignore_errors=True)

    @staticmethod
    def _ps(script: str) -> bool:
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        if not powershell:
            return False
        path = Path(SCRATCH) / ("stt-sapi-%s.ps1" % time.time_ns())
        path.write_text(script, encoding="utf-8-sig")     # BOM обязателен для кириллицы
        try:
            res = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-File", str(path)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=60)
            return res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            path.unlink(missing_ok=True)

    @classmethod
    def _sapi(cls, target: Path) -> bool:
        script = ("Add-Type -AssemblyName System.Speech\n"
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                  "$s.SelectVoice(\"Microsoft Irina Desktop\")\n"
                  "$s.SetOutputToWaveFile(\"%s\")\n"
                  "$s.Speak(\"%s\")\n$s.Dispose()\n" % (target, SAPI_TEXT))
        return cls._ps(script) and target.is_file() and target.stat().st_size > 10000

    @staticmethod
    def _ffmpeg(*args) -> bool:
        try:
            res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            return res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def call(self, path, body, ctype="audio/webm", timeout=180):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("POST", path, body=body,
                     headers={"Host": "127.0.0.1:%d" % self.port,
                              "Content-Type": ctype, "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, json.loads(data.decode("utf-8") or "{}")

    def test_01_webm_speech_is_recognized(self):
        started = time.time()
        status, payload = self.call("/api/stt/transcribe?language=ru", self.webm.read_bytes())
        elapsed = time.time() - started
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["text"], "пустой текст вместо речи")
        normalized = payload["text"].lower().replace("ё", "е")
        self.assertIn("проверка распознавания речи", normalized)
        self.assertIn("гранит", normalized)
        self.assertEqual(payload["language"], "ru")
        self.assertGreater(payload["duration_s"], 3.0)
        self.assertIn("large", payload["model"])
        print("\n  первое распознавание (модель + запрос): %.1f с" % elapsed)
        self.assertLess(elapsed, 120.0, "первый запрос дольше двух минут")

    def test_02_wav_is_recognized_faster(self):
        started = time.time()
        status, payload = self.call("/api/stt/transcribe", self.wav.read_bytes(), "audio/wav")
        elapsed = time.time() - started
        self.assertEqual(status, 200, payload)
        self.assertIn("распознавания речи", payload["text"].lower())
        print("  второе распознавание (модель в памяти): %.2f с" % elapsed)

    def test_03_silence_gives_empty_text(self):
        if not self.silence.is_file():
            self.skipTest("нет ffmpeg для тишины")
        status, payload = self.call("/api/stt/transcribe", self.silence.read_bytes())
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["text"], "")

    def test_04_junk_is_400_and_server_survives(self):
        status, payload = self.call("/api/stt/transcribe", b"not audio at all" * 200)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "audio_empty")
        status, _ = self.call("/api/stt/transcribe", self.wav.read_bytes(), "audio/wav")
        self.assertEqual(status, 200, "сервер не должен падать на мусорном файле")

    def test_05_two_requests_at_once(self):
        results: dict = {}

        def run(key, data, ctype):
            results[key] = self.call("/api/stt/transcribe", data, ctype)

        threads = [threading.Thread(target=run, args=(1, self.webm.read_bytes(), "audio/webm")),
                   threading.Thread(target=run, args=(2, self.wav.read_bytes(), "audio/wav"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([results[1][0], results[2][0]], [200, 200])
        self.assertTrue(results[1][1]["text"] and results[2][1]["text"])

    def test_06_corrections_are_applied(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = json.dumps({"terms": [], "replacements": [{"from": "проверка", "to": "тест"}]},
                          ensure_ascii=False).encode("utf-8")
        conn.request("PUT", "/api/stt/dictionary", body=body,
                     headers={"Host": "127.0.0.1:%d" % self.port,
                              "Content-Type": "application/json", "Content-Length": str(len(body))})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        status, payload = self.call("/api/stt/transcribe", self.wav.read_bytes(), "audio/wav")
        self.assertEqual(status, 200)
        self.assertTrue(payload["corrections"], "замена из словаря не сработала")
        self.assertIn("тест", payload["text"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
