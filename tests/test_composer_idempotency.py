# -*- coding: utf-8 -*-
"""W20-C1 (§1.3, B-39): идемпотентность сообщений и «накопленных ответов».

Что проверяется:
  * ``POST /api/requests/{rid}/messages`` с тем же ``client_id`` второй раз НЕ
    создаёт сообщение: ответ 200, ``duplicate: true``, тот же id записи, лента и
    карточка не меняются (``201`` был один);
  * повтор того же текста от того же автора в окне 120 с — тоже дубль, даже без
    ``client_id`` (диктовка уходила дважды из двух окон вкладки); текст сравнивается
    без различий в пробелах;
  * другой текст (или текст после смены набора ответов) — не дубль;
  * окно живёт 120 с: после истечения тот же текст создаёт новое сообщение;
  * ``client_id`` проверяется: не строка, пустой, длиннее 120 знаков, с NUL → 400;
  * ``POST actions {action:"answers_resend"}`` вторым нажатием не создаёт вторую
    карточку — ``duplicate: true`` и прежний ответ.

Правила волны: только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён
shim'ом; реальная доска сверяется до/после.

    python -m unittest tests.test_composer_idempotency -v

# SHIM-OK: CLI Hermes подменён CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import subprocess as real_subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                 # noqa: E402
import server.engine_cli as engine_cli       # noqa: E402
import server.engine_write as ew             # noqa: E402
import server.idempotency as idem_mod        # noqa: E402
import server.registry as registry           # noqa: E402
from server.http_util import ApiError        # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

ROOM = "cafe"
LEAD = "team-director"
Q1 = "Какой срок подачи заявки на бренд?"
Q2 = "Какой минимальный тираж коробки?"
A1 = "Срок — до 30 ноября, дальше не успеваем"
A2 = "Минимальный тираж — 500 штук"


class CliShim:
    """Подмена subprocess: записываем argv и НИЧЕГО не запускаем."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                      # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out = '{"id": "t_0000cafe", "status": "ready"}'
        return real_subprocess.CompletedProcess(argv, 0, out, "")

    def creates(self) -> list[list[str]]:
        return [argv for argv in self.calls if "create" in argv]


def _record(root_id: str, **extra) -> dict:
    rec = {"root_id": root_id, "slug": "r-20261111-0900-w20c1", "title": "Запрос про диалог",
           "text": "Соберите короткий ответ по бренду и коробке", "created_at": int(time.time()),
           "critic": False, "mode": "auto", "adopted": False, "extra_cards": [],
           "ingested": {}, "retargeted": False}
    rec.update(extra)
    return rec


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)
        cls.shim = CliShim()
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))

    @classmethod
    def tearDownClass(cls):
        engine_cli.subprocess = cls._real_sub
        try:
            del ew.subprocess
        except AttributeError:
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    def setUp(self):
        self.shim.calls.clear()
        idem_mod.reset()

    # --- помощники ---------------------------------------------------------
    def rid(self, meta: dict | None = None) -> str:
        """Запрос в работе; с ``meta`` — с открытыми вопросами на него."""
        root = self.sa.card("Соберите короткий ответ по бренду и коробке", LEAD, status="running")
        self.sa.finish(root, "Готово: бренд и коробка.", meta or {})
        registry.add_request(self.cfg, ROOM, _record(root))
        return root

    def rid_with_questions(self) -> str:
        return self.rid({"open_questions": [{"text": Q1, "for": "mikhail"},
                                            {"text": Q2, "for": "mikhail"}]})

    def post(self, rid: str, text: str, **body):
        payload = {"text": text}
        payload.update(body)
        try:
            return self.app.dispatch("POST", "/api/requests/%s/messages" % rid, {}, payload)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def actions(self, rid: str, **body):
        try:
            return self.app.dispatch("POST", "/api/requests/%s/actions" % rid, {}, body)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def detail(self, rid: str):
        status, payload, _headers = self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})
        return status, payload

    def answer(self, rid: str, question_id: str, text: str):
        return self.actions(rid, action="answer", question_id=question_id, text=text)


class MessageIdempotencyTests(_Base):
    """Повтор сообщения не создаёт дубль (§1.3, B-39)."""

    def test_default_window_is_120_seconds(self):
        self.assertEqual(idem_mod.DEFAULT_TTL_S, 120.0)
        self.assertEqual(float(idem_mod.GUARD.ttl), 120.0)

    def test_same_client_id_is_duplicate(self):
        rid = self.rid()
        status, first, _h = self.post(rid, "Уточнение по срокам поставки", client_id="msg-abc123")
        self.assertEqual(status, 201, first)
        self.assertFalse(first["duplicate"])
        creates_before = len(self.shim.creates())
        thread_before = len(self.detail(rid)[1]["thread"])

        status, second, _h = self.post(rid, "Уточнение по срокам поставки", client_id="msg-abc123")
        self.assertEqual(status, 200, second)
        self.assertTrue(second["duplicate"], "второй раз — уже отправлено")
        self.assertEqual(second["message"]["id"], first["message"]["id"], "та же запись ленты")
        self.assertEqual(len(self.shim.creates()), creates_before, "новая карточка не создана")
        self.assertEqual(len(self.detail(rid)[1]["thread"]), thread_before, "лента не выросла")

    def test_same_text_without_client_id_is_duplicate(self):
        rid = self.rid()
        first = self.post(rid, "Сдвинь срок на 30 ноября")
        self.assertEqual(first[0], 201, first[1])
        # Тот же текст, другие лишние пробелы: это то же сообщение.
        second = self.post(rid, "  Сдвинь   срок на 30 ноября   ")
        self.assertEqual(second[0], 200, second[1])
        self.assertTrue(second[1]["duplicate"])
        self.assertEqual(second[1]["message"]["id"], first[1]["message"]["id"])

    def test_different_text_is_new_message(self):
        rid = self.rid()
        self.assertEqual(self.post(rid, "Первое уточнение про срок")[0], 201)
        status, payload, _h = self.post(rid, "Второе уточнение про тираж")
        self.assertEqual(status, 201, payload)
        self.assertFalse(payload["duplicate"])

    def test_different_client_id_same_text_is_still_duplicate(self):
        rid = self.rid()
        self.assertEqual(self.post(rid, "Одинаковый текст", client_id="msg-one-1")[0], 201)
        status, payload, _h = self.post(rid, "Одинаковый текст", client_id="msg-two-2")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["duplicate"], "текст тоже совпал — дубль по тексту")

    def test_window_expires(self):
        rid = self.rid()
        guard = idem_mod.GUARD
        old_ttl = guard.ttl
        try:
            guard.ttl = 0.05
            first = self.post(rid, "Текст, который повторят позже")
            self.assertEqual(first[0], 201, first[1])
            time.sleep(0.15)                      # окно истекло
            second = self.post(rid, "Текст, который повторят позже")
            self.assertEqual(second[0], 201, "после окна это новое сообщение")
            self.assertFalse(second[1]["duplicate"])
        finally:
            guard.ttl = old_ttl

    def test_bad_client_id_is_400(self):
        rid = self.rid()
        for bad in ("", "   ", "x" * 121, "a\x00b", 42):
            with self.subTest(bad=str(bad)[:12]):
                status, payload, _h = self.post(rid, "Текст сообщения", client_id=bad)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error"]["detail"]["field"], "client_id")

    def test_client_id_accepted_in_response(self):
        rid = self.rid()
        status, payload, _h = self.post(rid, "С ключом нажатия", client_id="msg-xyz-987")
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["client_id"], "msg-xyz-987")

    def test_same_text_in_another_request_is_not_a_duplicate(self):
        """Одинаковый текст в РАЗНЫХ запросах — два разных сообщения (ключ включает запрос)."""
        first, second = self.rid(), self.rid()
        text = "Добавьте сроки поставки в свод."
        s1, p1, _h = self.post(first, text)
        s2, p2, _h = self.post(second, text)
        self.assertEqual((s1, s2), (201, 201), (p1, p2))
        self.assertFalse(p1.get("duplicate"))
        self.assertFalse(p2.get("duplicate"))
        self.assertEqual(p1["message"]["task_id"], first, "сообщение ушло в свой запрос")
        self.assertEqual(p2["message"]["task_id"], second, "и во второй — тоже своё")
        self.assertIn(text, p1["message"]["text"])
        self.assertIn(text, p2["message"]["text"])


class IdempotencyMemoryTests(unittest.TestCase):
    """Память повторов не растёт бесконечно (утечка исключена потолком ключей)."""

    def test_memory_is_capped(self):
        guard = idem_mod.Idempotency()
        for index in range(idem_mod.MAX_ITEMS + 50):
            guard.remember("key-%04d" % index, {"n": index})
        self.assertLessEqual(guard.size(), idem_mod.MAX_ITEMS)

    def test_threaded_replay_is_safe(self):
        """Два потока с одним ключом не получают исключений и не портят словарь."""
        import threading
        guard = idem_mod.Idempotency()
        errors = []

        def worker():
            try:
                for index in range(50):
                    guard.remember("k%d" % index, {"n": index})
                    guard.replay("k%d" % index, scope="messages")
            except Exception as exc:                      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


class AnswersResendIdempotencyTests(_Base):
    """Повторное нажатие «Отправить накопленные ответы» не создаёт вторую карточку."""

    def test_second_click_is_duplicate(self):
        rid = self.rid_with_questions()
        questions = self.detail(rid)[1]["open_questions"]
        self.assertEqual(len(questions), 2, questions)
        q1, q2 = questions[0]["id"], questions[1]["id"]
        self.answer(rid, q1, A1)
        self.answer(rid, q2, A2)
        self.shim.calls.clear()

        status, first, _h = self.actions(rid, action="answers_resend")
        self.assertEqual(status, 200, first)
        self.assertEqual(first["sent"], 2, first)
        self.assertFalse(first["duplicate"])
        creates = len(self.shim.creates())
        self.assertEqual(creates, 1, "одна карточка с ответами")

        status, second, _h = self.actions(rid, action="answers_resend")
        self.assertEqual(status, 200, second)
        self.assertTrue(second["duplicate"], "второе нажатие — уже отправлено")
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(len(self.shim.creates()), creates, "вторая карточка не создана")

    def test_new_answer_after_resend_is_not_duplicate(self):
        rid = self.rid_with_questions()
        q1, q2 = [q["id"] for q in self.detail(rid)[1]["open_questions"]]
        self.answer(rid, q1, A1)
        self.shim.calls.clear()
        self.assertEqual(self.actions(rid, action="answers_resend")[0], 200)

        self.answer(rid, q2, A2)              # набор ответов изменился
        status, payload, _h = self.actions(rid, action="answers_resend")
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["duplicate"], "новый ответ отправляется заново")
        self.assertIn(q2, payload["question_ids"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
