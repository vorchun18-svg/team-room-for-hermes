# -*- coding: utf-8 -*-
"""W8-THREAD-BE: диалог по запросу — лента и сообщение РУКОВОДИТЕЛЮ (Блок A, Д1).

Что проверяется:
  * ``thread.get_thread`` — комментарии дерева запроса по времени, события
    («уточнение передано», «версия готова»), итоги карточек «Уточнение» как сообщения
    команды; роли по-русски (никаких `biz-director` в подписях);
  * ``POST /api/requests/{rid}/messages`` — ровно ОДНА карточка «Уточнение от Михаила»
    на запрос, пакетирование вторым сообщением (``merged``), ветки done/failed;
  * сообщения НЕ рассылаются по дочерним карточкам исполнителей (Д1 отменяет рассылку);
  * инъекции (`--rm=…`, `--help`, чужой task_id) → 4xx и ни одного вызова CLI.

Правила волны 7/8: только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён
shim'ом (ни агентов, ни диспетчера); реальная доска сверяется до/после.

    python -m unittest tests.test_thread -v

# SHIM-OK: CLI Hermes подменён CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import dataclasses
import json
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
import server.registry as registry           # noqa: E402
import server.thread as thread_mod           # noqa: E402
from server.config import path_allowed       # noqa: E402
from server.http_util import ApiError        # noqa: E402
from tests.helpers import Sandbox, SimAgents  # noqa: E402

ROOM = "cafe"
MAXIM_MARK = "[Михаил · уточнение]"


class CliShim:
    """Подмена subprocess: записываем argv и НИЧЕГО не запускаем."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, **kwargs):                      # noqa: D401 - duck-typing
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for token in argv:                              # тело читаем сразу: его удаляют
            if token.startswith("--body-file="):
                try:
                    self.bodies.append(Path(token.split("=", 1)[1]).read_text(encoding="utf-8"))
                except OSError:
                    self.bodies.append("")
        verb = ""
        try:
            start = argv.index("kanban") + 1
        except ValueError:
            start = 0
        skip = False
        for token in argv[start:]:
            if skip:
                skip = False
                continue
            if token in ("--board", "--home"):
                skip = True
                continue
            if token.startswith("-"):
                continue
            verb = token
            break
        out = json.dumps({"id": "t_0000cafe", "status": "ready"}) if verb == "create" else ""
        return real_subprocess.CompletedProcess(argv, 0, out, "")

    # --- разбор вызовов -----------------------------------------------------
    def verbs(self) -> list[str]:
        out = []
        for argv in self.calls:
            body = argv[argv.index("kanban") + 1:] if "kanban" in argv else argv
            words = [t for t in body if not t.startswith("-")]
            out.append(words[1] if len(words) > 1 else "")     # после board
        return out

    def comment_targets(self) -> list[str]:
        out = []
        for argv in self.calls:
            if "comment" not in argv:
                continue
            index = argv.index("comment")
            if index + 1 < len(argv):
                out.append(argv[index + 1])
        return out

    def creates(self) -> list[list[str]]:
        return [argv for argv in self.calls if "create" in argv]


def _record(root_id: str, **extra) -> dict:
    rec = {"root_id": root_id, "slug": "r-20261028-1400-thr1", "title": "Запрос про диалог",
           "text": "Текст запроса", "created_at": int(time.time()), "critic": False,
           "mode": "auto", "adopted": False, "extra_cards": [], "ingested": {},
           "retargeted": False}
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
        self.shim.bodies.clear()

    # --- помощники ---------------------------------------------------------
    def rid(self, tree: str = "done_modern") -> str:
        root = self.sb.state(tree)["root_id"]
        registry.add_request(self.cfg, ROOM, _record(root))
        return root

    def post(self, rid: str, text: str, **body):
        payload = {"text": text}
        payload.update(body)
        try:
            return self.app.dispatch("POST", "/api/requests/%s/messages" % rid, {}, payload)
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def detail(self, rid: str):
        try:
            return self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def room(self) -> dict:
        return self.app._room(ROOM)


class ThreadReadTests(_Base):
    """Лента: комментарии дерева, события, роли по-русски, сортировка по времени."""

    def test_thread_shape_and_sorting(self):
        rid = self.rid("working")
        items = thread_mod.get_thread(self.cfg, rid, room=self.room())
        self.assertTrue(items, "лента не должна быть пустой у запроса в работе")
        keys = {"id", "at", "author_kind", "author", "author_label", "text", "task_id", "kind"}
        for item in items:
            self.assertEqual(set(item), keys, item)
            self.assertIsInstance(item["at"], int)
            self.assertIn(item["author_kind"], ("mikhail", "agent", "system"))
            self.assertIn(item["kind"], ("message", "comment", "event"))
        stamps = [item["at"] for item in items]
        self.assertEqual(stamps, sorted(stamps), "лента должна идти по времени")

    def test_labels_are_russian_roles(self):
        rid = self.rid("done_modern")
        items = thread_mod.get_thread(self.cfg, rid, room=self.room())
        for item in items:
            self.assertNotIn("biz-", item["author_label"], item)
            self.assertNotIn("biz-", item["text"] if item["kind"] == "event" else "")
        lead_items = [i for i in items if i["author"] == "biz-director"]
        if lead_items:
            self.assertIn("руководитель комнаты", lead_items[0]["author_label"])

    def test_pavel_comment_is_message(self):
        rid = self.rid("working")
        self.sa.comment(rid, "mikhail", MAXIM_MARK + " добавь сроки")
        items = thread_mod.get_thread(self.cfg, rid, room=self.room())
        mikhail = [i for i in items if i["author_kind"] == "mikhail"]
        self.assertTrue(mikhail, items)
        self.assertEqual(mikhail[-1]["kind"], "message")
        self.assertEqual(mikhail[-1]["author_label"], "Михаил")
        self.assertIn("добавь сроки", mikhail[-1]["text"])

    def test_children_comments_are_in_thread(self):
        rid = self.rid("working")
        child = self.sb.state("working")["child_ids"][0]
        self.sa.comment(child, "biz-ops", "Проверил поставщика — берём второго")
        items = thread_mod.get_thread(self.cfg, rid, room=self.room())
        found = [i for i in items if i["task_id"] == child and "поставщика" in i["text"]]
        self.assertTrue(found, "комментарий дочерней карточки обязан попасть в ленту")
        self.assertEqual(found[0]["author_kind"], "agent")
        self.assertIn("·", found[0]["author_label"])

    def test_versions_and_note_events(self):
        rid = self.rid("done_modern")
        # карточка «Уточнение» с итогом руководителя (как её создаёт движок)
        note = self.sa.card(thread_mod.NOTE_TITLE, "biz-director", status="running",
                            body="Запрос: %s «…»" % rid, tenant="room:cafe")
        self.sa.finish(note, "Принял; передал: юристу сроки, операциям упаковку. "
                             "Учтено из уточнений: сроки поставки.")
        items = thread_mod.get_thread(self.cfg, rid, room=self.room())
        texts = [i["text"] for i in items]
        self.assertTrue(any(t.startswith("Версия 1 готова") for t in texts), texts)
        self.assertIn("Уточнение передано руководителю комнаты.", texts)
        ack = [i for i in items if "Учтено из уточнений" in i["text"]]
        self.assertTrue(ack, "итог карточки «Уточнение» — сообщение команды")
        self.assertEqual(ack[0]["author_kind"], "agent")
        self.assertEqual(ack[0]["author"], "biz-director")
        self.assertIn("руководитель комнаты", ack[0]["author_label"])
        self.assertEqual(ack[0]["kind"], "comment")

    def test_request_detail_has_thread_field(self):
        rid = self.rid("working")
        status, payload, _ = self.detail(rid)
        self.assertEqual(status, 200, payload)
        self.assertIn("thread", payload)
        self.assertIsInstance(payload["thread"], list)
        self.assertTrue(payload["thread"])


class PostMessageTests(_Base):
    """Д1: сообщение идёт руководителю — одна карточка «Уточнение», без рассылки."""

    def test_one_note_card_and_comment_on_root(self):
        rid = self.rid("working")
        status, payload, _ = self.post(rid, "Сроки поставки сдвинулись на 10 дней")
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["routed_to"], "biz-director")
        self.assertFalse(payload["merged"])
        self.assertTrue(payload["message"]["text"].startswith(MAXIM_MARK))
        self.assertIn("10 дней", payload["message"]["text"])
        creates = self.shim.creates()
        self.assertEqual(len(creates), 1, self.shim.calls)
        argv = creates[0]
        self.assertIn("--assignee=biz-director", argv)
        self.assertIn("--tenant=room:cafe", argv)
        self.assertNotIn("--parent", " ".join(argv), "карточка «Уточнение» НЕ дочерняя корня")
        self.assertTrue(any(a.startswith("--body-file=") for a in argv), argv)
        self.assertEqual(len(self.shim.bodies), 1, self.shim.calls)
        text = self.shim.bodies[0]
        self.assertIn("Сроки поставки сдвинулись", text)
        self.assertIn("[Руководитель → <роль>]", text)
        self.assertIn("kanban_show", text)
        self.assertIn("«Учтено из уточнений»", text)
        self.assertIn(rid, text)
        # комментарий на корень — с автором mikhail и текстом после `--`
        first = [c for c in self.shim.calls if "comment" in c][0]
        self.assertEqual(first[first.index("comment") + 1], rid)
        self.assertIn("--author=mikhail", first)
        self.assertLess(first.index("--"), first.index(first[first.index("--") + 1]))

    def test_second_message_before_work_is_merged(self):
        rid = self.rid("working")
        status, first, _ = self.post(rid, "Первое уточнение")
        self.assertEqual(status, 201, first)
        self.shim.calls.clear()
        # движок создал карточку «Уточнение» — имитируем её появление в песочной БД
        note = self.sa.card(thread_mod.NOTE_TITLE, "biz-director", status="todo",
                            body="Запрос: %s «…»\n\nСООБЩЕНИЕ МАКСИМА: Первое уточнение" % rid,
                            tenant="room:cafe")
        status, second, _ = self.post(rid, "Второе уточнение")
        self.assertEqual(status, 201, second)
        self.assertTrue(second["merged"], second)
        self.assertEqual(self.shim.creates(), [], "вторая карточка не создаётся")
        self.assertEqual(second["task_id"], note)
        self.assertIn(note, self.shim.comment_targets())
        self.assertEqual(self.shim.comment_targets().count(rid), 1)

    def test_no_comments_on_children(self):
        rid = self.rid("working")
        children = set(self.sb.state("working")["child_ids"])
        status, payload, _ = self.post(rid, "Учтите новую цену поставщика")
        self.assertEqual(status, 201, payload)
        touched = set(self.shim.comment_targets()) & children
        self.assertEqual(touched, set(), "Д1: рассылки по карточкам исполнителей нет")
        self.assertEqual(payload["routed_to"], "biz-director")

    def test_done_request_also_goes_to_revision(self):
        rid = self.rid("done_modern")
        status, payload, _ = self.post(rid, "Добавь сравнение с поставщиком из Заозёрья")
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["status"], "done")
        self.assertIn("revision", payload, payload)
        self.assertTrue(payload["revision"].get("ok"), payload["revision"])
        self.assertTrue(self.shim.creates(), "должна появиться карточка «На доработку»")

    def test_failed_request_is_retried(self):
        rid = self.rid("failed")
        status, payload, _ = self.post(rid, "Разбей партию на две по 15 моти")
        self.assertEqual(status, 201, payload)
        self.assertIn("retry", payload, payload)
        self.assertIn("unblock", self.shim.verbs(), self.shim.calls)

    def test_urgent_changes_only_wording(self):
        rid = self.rid("working")
        status, normal, _ = self.post(rid, "Обычное уточнение")
        self.assertEqual(status, 201, normal)
        self.shim.calls.clear()
        status, urgent, _ = self.post(rid, "Обычное уточнение", urgent=True)
        self.assertEqual(status, 201, urgent)
        if not self.shim.bodies:               # вторая карточка пакетирована, новой нет
            self.assertTrue(urgent.get("merged"), urgent)
            return
        self.assertIn("СРОЧНО", self.shim.bodies[0])
        self.assertNotIn(MAXIM_MARK + " СРОЧНО", urgent["message"]["text"])
        self.assertIn(MAXIM_MARK, urgent["message"]["text"])


class InjectionTests(_Base):
    """Инъекции: 4xx и ни одного вызова CLI."""

    def test_bad_text_is_400_without_cli(self):
        rid = self.rid("working")
        for bad in ("", "   ", "текст\x00с NUL"):
            status, payload, _ = self.post(rid, bad)
            self.assertEqual(status, 400, (bad, payload))
        self.assertEqual(self.shim.calls, [])

    def test_long_text_is_400(self):
        rid = self.rid("working")
        status, payload, _ = self.post(rid, "я" * 1_000_001)
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.shim.calls, [])

    def test_bad_rid_is_4xx_without_cli(self):
        for bad in ("--help", "--rm=t_0000cafe", "t_ZZZZZZZZ"):
            try:
                thread_mod.post_message(self.cfg, self.room(), bad, "привет")
            except Exception as exc:                       # noqa: BLE001 - ждём доменную ошибку
                self.assertEqual(getattr(exc, "code", ""), "bad_request", (bad, exc))
            else:
                self.fail("инъекция в rid прошла: %s" % bad)
            try:
                thread_mod.get_thread(self.cfg, bad, room=self.room())
            except Exception as exc:                       # noqa: BLE001
                self.assertEqual(getattr(exc, "code", ""), "bad_request", (bad, exc))
            else:
                self.fail("инъекция в rid прошла в get_thread: %s" % bad)
        self.assertEqual(self.shim.calls, [])

    def test_unknown_root_is_404(self):
        try:
            thread_mod.post_message(self.cfg, self.room(), "t_0000dead", "привет")
        except Exception as exc:                           # noqa: BLE001
            self.assertEqual(getattr(exc, "code", ""), "not_found", exc)
        else:
            self.fail("сообщение для несуществующей карточки не должно проходить")
        self.assertEqual(self.shim.calls, [])

    def test_foreign_task_id_never_touched(self):
        """«Уточнение» другого запроса — не наша карточка, комментария к ней нет."""
        rid = self.rid("working")
        foreign_root = self.sb.state("done_modern")["root_id"]
        foreign_note = self.sa.card(thread_mod.NOTE_TITLE, "biz-director", status="todo",
                                    body="Запрос: %s «чужой»" % foreign_root,
                                    tenant="room:cafe")
        status, payload, _ = self.post(rid, "Наше уточнение")
        self.assertEqual(status, 201, payload)
        self.assertFalse(payload["merged"])
        self.assertNotIn(foreign_note, self.shim.comment_targets())

    def test_attach_outside_roots_is_403(self):
        rid = self.rid("working")
        outside = "C:/Windows/System32/drivers/etc/hosts"
        if path_allowed(self.cfg, outside):      # если корень разрешён — берём другой
            outside = "Z:/nope/file.pdf"
        status, payload, _ = self.post(rid, "С файлом", attach=[outside])
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.shim.calls, [])

    def test_attach_inside_sandbox_is_allowed(self):
        rid = self.rid("working")
        path = Path(self.cfg.data_dir) / "attach-thread.txt"
        path.write_text("вложение", encoding="utf-8")
        if not path_allowed(self.cfg, str(path)):
            self.skipTest("каталог данных песочницы вне разрешённых путей")
        status, payload, _ = self.post(rid, "С файлом", attach=[str(path)])
        self.assertEqual(status, 201, payload)
        self.assertIn("attach-thread.txt", payload["message"]["text"])


class ContractRuleTests(unittest.TestCase):
    """Контракт для исполнителей: перечитать комментарии перед финальным ответом."""

    def test_contract_block_mentions_marks(self):
        from server import contract

        class _Cfg:
            vault_dir = ROOT / "data" / "_vault"
            data_dir = ROOT / "data"
            reports_dir = ROOT / "data" / "reports"
            reportkit_dir = ROOT / "reportkit"

        room = {"id": "cafe", "title": "Лето", "orchestrator": "biz-director",
                "members": [{"profile": "biz-ops", "role": "worker"}]}
        block = contract.build_contract_block(room, "r-x", _Cfg())
        self.assertIn("ПЕРЕЧИТАЙ комментарии карточки", block)
        self.assertIn("[Михаил · уточнение]", block)
        self.assertIn("[Руководитель → …]", block)
        self.assertIn("Учтено из уточнений", block)
        self.assertTrue(contract.CONTRACT_OPEN in block and contract.CONTRACT_CLOSE in block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
