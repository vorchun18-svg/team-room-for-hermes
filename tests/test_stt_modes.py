"""Диктовка «как в Hermes»: три режима и никаких ключей в ответах.

Проверяем то, о чём просил Михаил:
  * есть faster-whisper → режим ``local`` (как раньше);
  * нет локального движка, но в Hermes выбран облачный STT и есть ключ → режим
    ``provider``: аудио уходит в API провайдера (в тесте — свой HTTP-заглушка);
  * ничего не настроено → ``mode="none"`` с понятным текстом и подсказкой;
  * ключи не попадают ни в ответы, ни в журнал.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import stt, stt_cloud, stt_worker                    # noqa: E402

FAKE_KEY = "sk-test-NEVER-LOG-123"


class StubProvider:
    """Заглушка STT-сервиса: принимает multipart и отвечает {"text": …}."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.requests: list = []
        self.status = status
        self.payload = payload if payload is not None else {"text": "привет"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):                                  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "body": body})
                raw = json.dumps(outer.payload).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):                        # тишина в тестах
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d/v1" % self.port

    def __enter__(self) -> "StubProvider":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.server.shutdown()
        self.server.server_close()
        return False


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="stt-mode-"))
        self.home = self.dir / "hermes"
        self.home.mkdir(parents=True, exist_ok=True)
        self.data = self.dir / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.cfg = SimpleNamespace(hermes_home=str(self.home), data_dir=self.data,
                                   tmp_dir=str(self.dir / "tmp"), sandbox=True)
        self._env = {}
        for key in ("TEAMROOM_STT_CONFIG", "OPENAI_API_KEY", "GROQ_API_KEY", "STT_OPENAI_BASE_URL",
                    "OPENAI_STT_BASE_URL", "HERMES_PROFILE", "TEAMROOM_STT_PROFILE"):
            self._env[key] = os.environ.pop(key, None)
        stt.reset_worker()
        stt._MODE_LOGGED.clear()

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        stt.reset_worker()
        stt._MODE_LOGGED.clear()

    def write_config(self, text: str) -> None:
        (self.home / "config.yaml").write_text(text, encoding="utf-8")

    def log_text(self) -> str:
        path = self.data / "logs" / "stt.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""


class TestModes(Base):
    def test_local_when_faster_whisper_present(self):
        self.write_config("stt:\n  provider: local\n  language: ru\n")
        with mock.patch.object(stt_worker, "find_python", return_value=sys.executable):
            status = stt.status(self.cfg)
        self.assertEqual(status["mode"], "local")
        self.assertEqual(status["source"], "local")
        self.assertTrue(status["available"])
        self.assertIsNone(status["reason"])

    def test_provider_when_hermes_has_cloud_stt(self):
        self.write_config("stt:\n  provider: openai\n  language: ru\n")
        os.environ["OPENAI_API_KEY"] = FAKE_KEY
        with StubProvider() as stub:
            (self.home / "config.yaml").write_text(
                "stt:\n  provider: openai\n  language: ru\n  openai:\n"
                "    base_url: %s\n" % stub.base, encoding="utf-8")
            with mock.patch.object(stt_worker, "find_python", return_value=None):
                status = stt.status(self.cfg)
                self.assertEqual(status["mode"], "provider")
                self.assertEqual(status["provider"], "openai")
                self.assertTrue(status["available"])
                self.assertIsNone(status["reason"])
                self.assertNotIn(FAKE_KEY, json.dumps(status, ensure_ascii=False))
                result = stt.transcribe(self.cfg, b"\x1aE\xdf\xa3" + b"\x00" * 64)
            self.assertEqual(result["text"], "привет")
            self.assertEqual(result["device"], "cloud:openai")
            self.assertEqual(len(stub.requests), 1)
            request = stub.requests[0]
            self.assertEqual(request["path"], "/v1/audio/transcriptions")
            self.assertEqual(request["headers"].get("Authorization"), "Bearer " + FAKE_KEY)
            self.assertIn(b'name="file"', request["body"])
            self.assertIn(b'name="model"', request["body"])
        self.assertNotIn(FAKE_KEY, self.log_text())
        self.assertIn("режим=provider", self.log_text())

    def test_none_when_nothing_configured(self):
        self.write_config("stt:\n  provider: local\n")
        with mock.patch.object(stt_worker, "find_python", return_value=None):
            status = stt.status(self.cfg)
            self.assertEqual(status["mode"], "none")
            self.assertFalse(status["available"])
            self.assertIn("не настроено", status["reason"])
            self.assertIn("faster-whisper", status["reason"])
            with self.assertRaises(stt.SttError) as ctx:
                stt.transcribe(self.cfg, b"\x00" * 32)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("stt_unavailable", 503))
        self.assertIn("режим=none", self.log_text())

    def test_provider_without_key_is_none(self):
        self.write_config("stt:\n  provider: groq\n")
        with mock.patch.object(stt_worker, "find_python", return_value=None):
            status = stt.status(self.cfg)
        self.assertEqual(status["mode"], "none")
        self.assertFalse(status["available"])
        self.assertIn("ключ", status["reason"])
        self.assertNotIn("GROQ_API_KEY", status["reason"])

    def test_provider_error_is_readable(self):
        os.environ["OPENAI_API_KEY"] = FAKE_KEY
        with StubProvider(status=401, payload={"error": {"message": "bad key"}}) as stub:
            self.write_config("stt:\n  provider: openai\n  openai:\n    base_url: %s\n" % stub.base)
            with mock.patch.object(stt_worker, "find_python", return_value=None):
                with self.assertRaises(stt.SttError) as ctx:
                    stt.transcribe(self.cfg, b"\x00" * 64)
        self.assertEqual(ctx.exception.code, "stt_failed")
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("401", ctx.exception.message_ru)
        self.assertNotIn(FAKE_KEY, json.dumps(ctx.exception.detail, ensure_ascii=False))
        self.assertNotIn(FAKE_KEY, self.log_text())

    def test_profile_config_overrides_root(self):
        (self.home / "profiles" / "work").mkdir(parents=True)
        (self.home / "profiles" / "work" / "config.yaml").write_text(
            "stt:\n  provider: openai\n", encoding="utf-8")
        self.write_config("stt:\n  provider: local\n")
        os.environ["OPENAI_API_KEY"] = FAKE_KEY
        os.environ["HERMES_PROFILE"] = "work"
        info = stt_cloud.resolve(self.cfg)
        self.assertEqual(info["provider"], "openai")
        self.assertEqual(info["mode"], "provider")

    def test_env_key_and_dotenv(self):
        self.write_config("stt:\n  provider: elevenlabs\n")
        (self.home / ".env").write_text("ELEVENLABS_API_KEY=%s\n" % FAKE_KEY, encoding="utf-8")
        info = stt_cloud.resolve(self.cfg)
        self.assertEqual(info["mode"], "provider")
        self.assertEqual(info["provider"], "elevenlabs")
        self.assertEqual(stt_cloud.PROVIDERS["elevenlabs"]["auth"], "xi")


class TestParsing(unittest.TestCase):
    def test_yaml_subset(self):
        text = ("stt:\n  provider: groq\n  language: 'ru'\n  groq:\n    model: whisper-large-v3\n"
                "providers:\n  groq:\n    api_key: k-1\n")
        parsed = stt_cloud.parse_yaml_subset(text)
        self.assertEqual(parsed["stt"]["provider"], "groq")
        self.assertEqual(parsed["stt"]["groq"]["model"], "whisper-large-v3")
        self.assertEqual(parsed["providers"]["groq"]["api_key"], "k-1")

    def test_multipart_body(self):
        body, ctype = stt_cloud._multipart({"model": "whisper-1"}, "a.webm", b"data")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="model"', body)
        self.assertIn(b"whisper-1", body)
        self.assertIn(b"data", body)


if __name__ == "__main__":
    unittest.main()
