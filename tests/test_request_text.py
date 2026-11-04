"""Тексты запросов: мягкий предел, единые значения в API и чистый `text` (D1).

Проверяется:
* `app_common.MAX_TEXT` = 1 000 000 (текст запроса), `MAX_NOTE` = 200 000;
  `engine_write.MAX_TEXT` — тот же предел для заметок, не меньше 200 000;
* `POST /api/rooms/{id}/requests` на 1 000 001 знак → 400 с русским текстом
  без технических слов; `/api/config` отдаёт новые пределы;
* `request_detail.text` — только текст пользователя, `body_full` — полное тело
  карточки (контракт `КОМНАТА-КОНТРАКТ` в `text` не попадает, старые карточки
  режутся на чтении; слово «КОМНАТА-КОНТРАКТ» без маркера не обрезается);
* большой запрос (300 000 знаков: кириллица, переносы, эмодзи, `--`, кавычки
  Windows) реально создаётся через CLI в песочнице, тело карточки совпадает
  с отправленным побайтно и читается обратно.
"""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import app_common, contract, engine_read  # noqa: E402
from server import app as app_mod  # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

BIG_LEN = 300_000


def _big_text(length: int = BIG_LEN) -> str:
    """Кириллица, переносы строк, эмодзи, `--`, кавычки и метасимволы Windows."""
    seed = (
        "- Пункт с дефисами -- и длинным тире — проверить «кавычки», 'одинарные', "
        "двойные \"кавычки\", знаки & | ^ % < > $ ` и эмодзи 🙂 🚀 ✅.\n"
        "Вторая строка: юнит-экономика набора из 6 моти, маржа 46 %.\n"
        "\tТретья строка с табуляцией и обратным слэшем C:\\TeamRoom\\Vault\\x.md\n"
    )
    head = "Запрос на 300 000 знаков: диктовка целиком\n"
    body = (head + (seed * ((length - len(head)) // len(seed) + 1)))[:length]
    assert len(body) == length
    return body


class LimitsTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(app_common.MAX_TEXT, 1_000_000)
        self.assertEqual(app_common.MAX_NOTE, 200_000)
        self.assertGreaterEqual(app_common.MAX_NOTE, 200_000)
        from server import engine_write
        self.assertGreaterEqual(engine_write.MAX_TEXT, 200_000)

    def test_message_is_russian_and_readable(self):
        message = app_common.text_too_long("Текст запроса", 1_000_001, app_common.MAX_TEXT)
        self.assertIn("1 000 001", message)
        self.assertIn("1 000 000", message)
        self.assertIn("Сократите", message)
        for technical in ("max", "len", "limit", "text", "symbol"):
            self.assertNotIn(technical, message.lower())


class VisibleTextTest(unittest.TestCase):
    """D1: контракт не показывается, пользовательский текст не режется."""

    USER = "Нужен ответ по цене набора из 6 моти.\nВторая строка — с дефисом."

    def _body(self, team: bool = True) -> str:
        parts = [self.USER]
        if team:
            parts.append("Команда комнаты (назначай подзадачи только этим профилям): biz-director — директор комнаты")
        parts.append(contract.build_contract_block({"id": "cafe", "title": "Лето"}, "r-1", self._cfg()))
        return "\n\n".join(parts)

    @staticmethod
    def _cfg():
        class Cfg:
            app_dir = PROJECT_ROOT
            data_dir = PROJECT_ROOT / "data"
            vault_dir = PROJECT_ROOT / "data" / "vault"
            reports_dir = PROJECT_ROOT / "data" / "reports"
            reportkit_dir = PROJECT_ROOT / "reportkit"
        return Cfg()

    def test_contract_is_cut_on_read(self):
        body = self._body()
        self.assertIn(contract.CONTRACT_OPEN, body)
        text = contract.visible_text(body)
        self.assertEqual(text, self.USER)
        self.assertNotIn("КОМНАТА-КОНТРАКТ", text)

    def test_user_written_word_without_marker_is_kept(self):
        body = "Проверь слово КОМНАТА-КОНТРАКТ в тексте — это часть запроса, не маркер."
        self.assertEqual(contract.visible_text(body), body)

    def test_empty(self):
        self.assertEqual(contract.visible_text(""), "")
        self.assertEqual(contract.visible_text(None), "")

    def test_detail_text_and_body_full(self):
        with Sandbox() as sb:
            body = self._body()
            agents = SimAgents(sb)
            rid = agents.card("Запрос с контрактом", "biz-director", body=body, tenant="room:cafe")
            detail = engine_read.request_detail(sb.cfg, "team", rid)
            self.assertEqual(detail["text"], self.USER)
            self.assertNotIn("КОМНАТА-КОНТРАКТ", detail["text"])
            self.assertEqual(detail["body_full"], body)
            self.assertIn(contract.CONTRACT_OPEN, detail["body_full"])
            self.assertEqual(detail["preview"][:20], self.USER[:20])


class LimitsApiTest(unittest.TestCase):
    """HTTP: новые пределы в /api/config и 400 на 1 000 001 знак."""

    def setUp(self):
        self.sb = Sandbox().__enter__()
        self._saved_threads = app_mod.TeamRoomServer.max_threads
        app_mod.TeamRoomServer.max_threads = 8
        self.server = app_mod.create_app(self.sb.cfg, start_watcher=False, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        app_mod.TeamRoomServer.max_threads = self._saved_threads
        self.sb.__exit__(None, None, None)

    def _call(self, method: str, path: str, body=None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json"}
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw.decode("utf-8"))

    def test_config_limits(self):
        from server import http_util
        status, data = self._call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data["limits"]["request_text_max"], 1_000_000)
        self.assertEqual(data["limits"]["note_text_max"], 200_000)
        # общий предел тела POST — 8 МиБ (STT-BE поднял http_util.MAX_BODY)
        self.assertEqual(http_util.MAX_BODY, 8 * 1024 * 1024)
        self.assertEqual(data["limits"]["body_bytes_max"], http_util.MAX_BODY)

    def test_over_limit_is_400_with_human_message(self):
        text = "я" * 1_000_001
        status, data = self._call("POST", "/api/rooms/cafe/requests", {"text": text})
        self.assertEqual(status, 400, data)
        message = data["error"]["message"]
        self.assertIn("1 000 001", message)
        self.assertIn("1 000 000", message)
        self.assertIn("Сократите", message)
        self.assertEqual(data["error"]["detail"].get("limit"), 1_000_000)


@unittest.skipUnless(shutil.which("hermes"), "нет CLI hermes — реальный прогон невозможен")
class BigRequestRoundTripTest(unittest.TestCase):
    """Реальный прогон: 300 000 знаков создаются, лежат в теле и читаются обратно."""

    def test_300k_round_trip(self):
        text = _big_text()
        with Sandbox() as sb:
            server = app_mod.create_app(sb.cfg, start_watcher=False, port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                             daemon=True).start()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
            try:
                payload = json.dumps({"text": text, "critic": False}, ensure_ascii=False).encode("utf-8")
                conn.request("POST", "/api/rooms/cafe/requests", body=payload,
                             headers={"Host": "127.0.0.1:%d" % port,
                                      "Content-Type": "application/json"})
                resp = conn.getresponse()
                raw = resp.read()
                self.assertEqual(resp.status, 201, raw[:400])
                created = json.loads(raw.decode("utf-8"))
                rid = created["request"]["id"]

                conn.request("GET", "/api/requests/" + rid,
                             headers={"Host": "127.0.0.1:%d" % port})
                resp = conn.getresponse()
                raw = resp.read()
                self.assertEqual(resp.status, 200, raw[:400])
                detail = json.loads(raw.decode("utf-8"))
            finally:
                conn.close()
                server.shutdown()
                server.server_close()

            # 1) API отдаёт ровно отправленный текст (байт в байт)
            self.assertEqual(len(detail["text"]), len(text))
            self.assertEqual(detail["text"].encode("utf-8"), text.encode("utf-8"))
            # 2) в теле карточки текст лежит дословно и следом идёт контракт
            row = sb.one("SELECT body FROM tasks WHERE id = ?", (rid,))
            stored = row["body"]
            self.assertEqual(stored.encode("utf-8")[:len(text.encode("utf-8"))],
                             text.encode("utf-8"))
            self.assertIn(contract.CONTRACT_OPEN, stored)
            # 3) полное тело отдаётся отдельно и совпадает с БД байт в байт
            self.assertEqual(detail["body_full"].encode("utf-8"), stored.encode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
