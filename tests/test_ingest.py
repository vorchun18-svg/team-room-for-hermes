"""Тесты ingest: здание проекта, идемпотентность, legacy, зеркало Vault."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config, ingest, registry, rooms  # noqa: E402

AT = 1793165000  # 27.10.2026, вечер

PHASES = [
    ("idea", "Идея и стратегия", "done", "VISION утверждён"),
    ("legal", "Легализация", "active", "ИП подан"),
    ("brand", "Бренд и товарный знак", "blocked", "Ждёт go/no-go"),
    ("packaging", "Упаковка", "active", "10 поставщиков"),
    ("production", "Производство", "next", "Кухня есть"),
    ("sales", "Продажи", "next", "0 продаж"),
]


def make_detail(root_id: str = "t_a1", at: int = AT, title: str = "Дорожная карта проекта", **over) -> dict:
    detail = {
        "id": root_id,
        "title": title,
        "text": "Исходный текст запроса Михаила",
        "created_at": at - 4000,
        "finished_at": at,
        "mode": "auto",
        "critic": False,
        "synthesis": {"state": "ready", "text_md": "Готовый ответ.", "author": "biz-director",
                      "at": at, "version": 1, "source_task": root_id},
        "next_steps": [
            {"id": "ns_1", "text": "Подать заявление на ИП через Госуслуги", "owner": "mikhail", "due": "2026-11-01", "state": "open"},
            {"id": "ns_2", "text": "Запросить свидетельство", "owner": "biz-ops", "due": None, "state": "open"},
        ],
        "open_questions": [
            {"id": "q_1", "text": "Подача в Роспатент: класс 30 или 30+43?", "for": "mikhail",
             "answered": False, "answer": None, "answered_at": None},
        ],
        "decisions": [
            {"id": "d_1", "text": "Старт с набора из 6 моти", "why": "себестоимость ниже на 18%",
             "status": "proposed"},
        ],
        "future_questions": ["Нужен ли отдельный бренд для B2B?"],
        "stage": {"current": "legal", "phases": [
            {"id": pid, "title": ptitle, "status": status, "note": note} for pid, ptitle, status, note in PHASES
        ]},
        "artifacts": [{"path": "__TMP__/art.md", "title": "Артефакт", "kind": "md"}],
        "report": None,
        "warnings": [],
    }
    detail.update(over)
    return detail


class IngestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr-ingest-")
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
        self.cafe = rooms.get_room(self.cfg, "cafe")
        self.fn = rooms.get_room(self.cfg, "fn")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def detail(self, **over):
        if "artifacts" not in over:
            over["artifacts"] = [{"path": str(self.tmp / "art.md"), "title": "Артефакт", "kind": "md"}]
        return make_detail(**over)

    def state_file(self, name: str, room_id: str = "cafe") -> Path:
        return self.tmp / "data" / "state" / room_id / name

    def snapshot(self, room_id: str = "cafe") -> dict:
        root = self.tmp / "data" / "state" / room_id
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    def items(self, room_id: str = "cafe") -> list[dict]:
        return ingest.backlog_view(self.cfg, room_id)["items"]

    def item_by_kind(self, kind: str, room_id: str = "cafe") -> dict:
        return next(i for i in self.items(room_id) if i["kind"] == kind)


class TestIngestCore(IngestBase):
    def test_builds_project_building(self):
        detail = self.detail()
        res = ingest.ingest_request(self.cfg, self.cafe, detail)
        self.assertTrue(res["changed"])
        self.assertIsNone(res["skipped"])
        self.assertEqual(res["added_counts"]["next_steps"], 2)
        self.assertEqual(res["added_counts"]["open_questions"], 1)
        self.assertEqual(res["added_counts"]["future_questions"], 1)
        self.assertEqual(res["added_counts"]["decisions"], 1)
        self.assertEqual(res["added_counts"]["phases"], 6)  # первый запрос описывает все 6 фаз
        self.assertEqual(len(res["added"]["artifacts"]), 1)

        roadmap = ingest.roadmap_view(self.cfg, "cafe")
        self.assertEqual([p["id"] for p in roadmap["phases"]],
                         ["idea", "legal", "brand", "packaging", "production", "sales"])
        self.assertEqual(roadmap["current"], {"id": "legal", "title": "Легализация", "status": "active"})
        legal = next(p for p in roadmap["phases"] if p["id"] == "legal")
        self.assertEqual(legal["note"], "ИП подан")
        self.assertEqual(legal["source_request"], "t_a1")

        decisions = ingest.load_state(self.cfg, "cafe")["decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["text"], "Старт с набора из 6 моти")
        self.assertEqual(decisions[0]["status"], "proposed")

        items = self.items()
        self.assertEqual(len(items), 5)
        by_id = {i["id"]: i for i in items}
        self.assertIn("t_a1:ns_1", by_id)
        self.assertEqual(by_id["t_a1:ns_1"]["due"], "2026-11-01")
        self.assertEqual(by_id["t_a1:ns_2"]["owner"], "biz-ops")
        self.assertEqual(by_id["t_a1:q_1"]["owner"], "mikhail")
        self.assertEqual(by_id["t_a1:fq_1"]["kind"], "future_question")
        self.assertEqual(by_id["t_a1:d_1"]["kind"], "decision")
        self.assertEqual({i["status"] for i in items}, {"open"})

        journal = ingest.journal_view(self.cfg, "cafe")
        kinds = [e["kind"] for e in journal["items"]]
        self.assertIn("request_created", kinds)
        self.assertIn("request_done", kinds)
        self.assertIn("ingest", kinds)
        self.assertEqual(journal["items"][0]["kind"], "ingest")  # свежее сверху

        rec = registry.get(self.cfg, "cafe", "t_a1")
        self.assertIsNotNone(rec)
        self.assertTrue(rec["ingested"])  # ingest сам завёл запись

    def test_second_ingest_is_noop(self):
        detail = self.detail()
        ingest.ingest_request(self.cfg, self.cafe, detail)
        before = self.snapshot()
        res = ingest.ingest_request(self.cfg, self.cafe, detail)
        self.assertFalse(res["changed"])
        self.assertEqual(res["skipped"], "already_ingested")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.items()), 5)

    def test_pending_request_is_skipped(self):
        res = ingest.ingest_request(self.cfg, self.cafe, {
            "id": "t_pending", "title": "Ещё думает", "synthesis": {"state": "pending"}})
        self.assertFalse(res["changed"])
        self.assertEqual(res["skipped"], "not_finished")
        self.assertEqual(self.items(), [])

    def test_no_resurrect_done_and_dismissed(self):
        detail = self.detail()
        ingest.ingest_request(self.cfg, self.cafe, detail)
        ingest.set_backlog_status(self.cfg, "cafe", "t_a1:ns_1", "done")
        ingest.set_backlog_status(self.cfg, "cafe", "t_a1:fq_1", "dismissed")

        again = self.detail()
        again["synthesis"] = dict(again["synthesis"], text_md="Переработанный ответ, версия 2", version=2)
        ingest.ingest_request(self.cfg, self.cafe, again)

        by_id = {i["id"]: i for i in self.items()}
        self.assertEqual(by_id["t_a1:ns_1"]["status"], "done")
        self.assertEqual(by_id["t_a1:fq_1"]["status"], "dismissed")

    def test_newer_request_wins_phase(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail(root_id="t_old", at=AT, title="Старый"))
        later = self.detail(root_id="t_new", at=AT + 86400, title="Новый")
        later["stage"] = {"current": "brand", "phases": [
            {"id": "idea", "title": "Идея и стратегия", "status": "done", "note": "утверждено"},
            {"id": "legal", "title": "Легализация", "status": "done", "note": "ИП получен"},
            {"id": "brand", "title": "Бренд и товарный знак", "status": "active", "note": "Роспатент подан"},
        ]}
        ingest.ingest_request(self.cfg, self.cafe, later)
        legal = next(p for p in ingest.roadmap_view(self.cfg, "cafe")["phases"] if p["id"] == "legal")
        self.assertEqual((legal["status"], legal["note"]), ("done", "ИП получен"))

        replay = self.detail(root_id="t_old", at=AT, title="Старый")
        replay["synthesis"] = dict(replay["synthesis"], text_md="другой текст")
        replay["stage"] = {"current": "legal", "phases": [
            {"id": "legal", "title": "Легализация", "status": "active", "note": "ИП подан"}]}
        ingest.ingest_request(self.cfg, self.cafe, replay)
        legal = next(p for p in ingest.roadmap_view(self.cfg, "cafe")["phases"] if p["id"] == "legal")
        self.assertEqual((legal["status"], legal["note"]), ("done", "ИП получен"))

    def test_filler_phases_do_not_reset_progress(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        # engine_read всегда отдаёт 6 фаз; неупомянутые — «next» с пустым note (заглушка)
        second = self.detail(root_id="t_b1", at=AT + 7200, title="Второй запрос")
        second["stage"] = {"current": None, "phases": [
            {"id": pid, "title": ptitle, "status": "next", "note": ""} for pid, ptitle, _, _ in PHASES
        ]}
        ingest.ingest_request(self.cfg, self.cafe, second)
        legal = next(p for p in ingest.roadmap_view(self.cfg, "cafe")["phases"] if p["id"] == "legal")
        self.assertEqual((legal["status"], legal["note"], legal["source_request"]),
                         ("active", "ИП подан", "t_a1"))

    def test_decision_status_tightens_forward_only(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        accepted = self.detail()
        accepted["decisions"] = [{"id": "d_1", "text": "Старт с набора из 6 моти",
                                  "why": "себестоимость ниже на 18%", "status": "accepted"}]
        accepted["synthesis"] = dict(accepted["synthesis"], text_md="Ответ v2", version=2)
        ingest.ingest_request(self.cfg, self.cafe, accepted)
        state = ingest.load_state(self.cfg, "cafe")
        self.assertEqual(state["decisions"][0]["status"], "accepted")
        self.assertEqual({i["id"]: i for i in state["backlog"]["items"]}["t_a1:d_1"]["status"], "done")

        back = self.detail()
        back["decisions"] = [{"id": "d_1", "text": "Старт с набора из 6 моти",
                              "why": "себестоимость ниже на 18%", "status": "proposed"}]
        back["synthesis"] = dict(back["synthesis"], text_md="Ответ v3", version=3)
        ingest.ingest_request(self.cfg, self.cafe, back)
        self.assertEqual(ingest.load_state(self.cfg, "cafe")["decisions"][0]["status"], "accepted")

    def test_parallel_ingest_two_requests(self):
        errors: list[BaseException] = []

        def work(root_id: str):
            try:
                ingest.ingest_request(self.cfg, self.cafe, self.detail(root_id=root_id, title=f"Запрос {root_id}"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(rid,)) for rid in ("t_p1", "t_p2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        items = self.items()
        self.assertEqual(len(items), 10)
        self.assertEqual({i["source_request"] for i in items}, {"t_p1", "t_p2"})
        journal = ingest.journal_view(self.cfg, "cafe", limit=200)["items"]
        ids = [e["id"] for e in journal]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cyrillic_raw_utf8(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        raw = self.state_file("backlog.json").read_text(encoding="utf-8")
        self.assertIn("Подать заявление на ИП через Госуслуги", raw)
        self.assertIn("Подача в Роспатент", raw)
        self.assertNotIn("\\u04", raw)


class TestLegacy(IngestBase):
    @unittest.skipUnless(ingest._normalizer() is not None, "server/normalize.py ещё не готов")
    def test_legacy_metadata_is_normalized(self):
        detail = {
            "id": "t_605613e0",
            "title": "Дорожная карта проекта",
            "finished_at": AT,
            "created_at": AT - 3000,
            "synthesis": {"state": "ready", "text_md": "Ответ в старом формате.", "author": "biz-director",
                          "at": AT, "legacy": True},
            "next_steps": ["Ш1 Михаил: снять блокер канала TG/WA"],
            "open_questions": ["Q1 Канал для D19: V1 / V2 / V3?"],
            "stage": {
                "idea": "готов: VISION утверждён",
                "legal": "в работе → ждёт действия Михаила",
                "brand": "в работе → ждёт решения Михаила",
                "packaging": "в работе: исследование готово",
                "production": "не начат: кухня есть",
                "sales": "не начат: 0 продаж",
            },
        }
        res = ingest.ingest_request(self.cfg, self.cafe, detail)
        self.assertTrue(res["changed"])
        items = {i["kind"]: i for i in self.items()}
        self.assertEqual(items["next_step"]["text"], "Ш1 Михаил: снять блокер канала TG/WA")
        self.assertEqual(items["open_question"]["owner"], "mikhail")

        roadmap = ingest.roadmap_view(self.cfg, "cafe")
        statuses = {p["id"]: p["status"] for p in roadmap["phases"]}
        self.assertEqual(statuses, {"idea": "done", "legal": "blocked", "brand": "blocked",
                                    "packaging": "active", "production": "next", "sales": "next"})
        self.assertEqual(roadmap["current"]["id"], "legal")

    def test_legacy_without_normalizer_reports_clearly(self):
        if ingest._normalizer() is not None:
            self.skipTest("normalize.py уже есть — проверяется в предыдущем тесте")
        with self.assertRaises(ingest.IngestError) as ctx:
            ingest.ingest_request(self.cfg, self.cafe, {
                "id": "t_l", "title": "Legacy", "next_steps": ["строка"]})
        self.assertEqual(ctx.exception.code, "normalizer_missing")


class TestViewsAndActions(IngestBase):
    def test_empty_room_views(self):
        roadmap = ingest.roadmap_view(self.cfg, "fn")
        self.assertEqual(len(roadmap["phases"]), 6)
        self.assertEqual({p["status"] for p in roadmap["phases"]}, {"next"})
        self.assertEqual(roadmap["current"]["id"], "idea")
        self.assertEqual(roadmap["decisions"], [])

        backlog = ingest.backlog_view(self.cfg, "fn")
        self.assertEqual(backlog["counts"], {"open": 0, "asked": 0, "done": 0, "dismissed": 0, "total": 0})
        self.assertEqual(backlog["items"], [])
        self.assertEqual(set(backlog["sections"]), {"inbox", "next_steps", "future_questions", "decisions"})

        journal = ingest.journal_view(self.cfg, "fn")
        self.assertEqual((journal["items"], journal["has_more"]), ([], False))

    def test_backlog_sections_and_counts(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        ingest.set_backlog_status(self.cfg, "cafe", "t_a1:ns_1", "done")
        view = ingest.backlog_view(self.cfg, "cafe")
        self.assertEqual(view["counts"]["done"], 1)
        self.assertEqual(view["counts"]["total"], 5)
        self.assertEqual([i["id"] for i in view["sections"]["next_steps"]], ["t_a1:ns_2"])
        self.assertEqual({i["id"] for i in view["sections"]["inbox"]}, {"t_a1:q_1", "t_a1:d_1"})
        self.assertEqual(len(view["sections"]["future_questions"]), 1)

    def test_set_backlog_status_validations(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        item = ingest.set_backlog_status(self.cfg, "cafe", "t_a1:ns_2", "done", due="2026-11-10")
        self.assertEqual((item["status"], item["due"]), ("done", "2026-11-10"))
        with self.assertRaises(ingest.IngestError) as ctx:
            ingest.set_backlog_status(self.cfg, "cafe", "t_a1:ns_2", "closed")
        self.assertEqual(ctx.exception.code, "bad_request")
        with self.assertRaises(ingest.IngestError):
            ingest.set_backlog_status(self.cfg, "cafe", "t_a1:ns_2", "done", due="10.11.2026")
        with self.assertRaises(ingest.IngestError) as ctx:
            ingest.set_backlog_status(self.cfg, "cafe", "t_none", "done")
        self.assertEqual(ctx.exception.code, "not_found")
        kinds = [e["kind"] for e in ingest.journal_view(self.cfg, "cafe")["items"]]
        self.assertIn("step_done", kinds)

    def test_ask_item_returns_request_text(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        text = ingest.ask_item(self.cfg, "cafe", "t_a1:ns_1")
        self.assertIsInstance(text, str)
        self.assertIn("Подать заявление на ИП через Госуслуги", text)
        self.assertIn("t_a1", text)
        self.assertIn("2026-11-01", text)
        custom = ingest.ask_item(self.cfg, "cafe", "t_a1:q_1", text="Свой текст вопроса")
        self.assertTrue(custom.startswith("Вопрос, который ждёт решения: Свой текст вопроса"))
        with self.assertRaises(ingest.IngestError):
            ingest.ask_item(self.cfg, "cafe", "t_none")

    def test_mark_asked_links_request(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        item = ingest.mark_asked(self.cfg, "cafe", "t_a1:fq_1", "t_new_request")
        self.assertEqual((item["status"], item["asked_request"]), ("asked", "t_new_request"))
        self.assertEqual({i["id"]: i for i in self.items()}["t_a1:fq_1"]["asked_request"], "t_new_request")

    def test_journal_view_limit_and_search(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        full = ingest.journal_view(self.cfg, "cafe", limit=200)
        self.assertGreaterEqual(len(full["items"]), 3)
        page = ingest.journal_view(self.cfg, "cafe", limit=2)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        found = ingest.journal_view(self.cfg, "cafe", limit=50, q="дорожная")
        self.assertTrue(found["items"])
        self.assertTrue(all("дорожная" in e["text"].lower() or "дорожная" in e["kind"] for e in found["items"]))
        self.assertEqual(ingest.journal_view(self.cfg, "cafe", q="ничего-такого")["items"], [])

    def test_report_index(self):
        report_path = self.tmp / "report.json"
        report_path.write_text(json.dumps({"version": 1, "title": "Юнит-экономика",
                                           "blocks": [{"type": "text", "md": "a"}, {"type": "text", "md": "b"}]},
                                          ensure_ascii=False), encoding="utf-8")
        detail = self.detail(report={"path": str(report_path), "title": "Юнит-экономика", "kind": "spec"})
        ingest.ingest_request(self.cfg, self.cafe, detail)
        reports = ingest.load_state(self.cfg, "cafe")["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["key"], "yunit-ekonomika")
        self.assertEqual(reports[0]["blocks"], 2)
        self.assertTrue(reports[0]["exists"])
        self.assertEqual(reports[0]["request_id"], "t_a1")

        again = self.detail(report={"path": str(report_path), "title": "Юнит-экономика", "kind": "spec"})
        again["synthesis"] = dict(again["synthesis"], text_md="v2", version=2)
        ingest.ingest_request(self.cfg, self.cafe, again)
        self.assertEqual(len(ingest.load_state(self.cfg, "cafe")["reports"]), 1)


class TestVaultMirror(IngestBase):
    def mirror_path(self, name: str) -> Path:
        return self.tmp / "vault" / "cafe" / name

    def test_mirror_written_changed_and_deterministic(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        self.assertTrue(ingest.write_vault_mirror(self.cfg, "cafe"))
        self.assertTrue(self.mirror_path("STATE.md").is_file())
        self.assertTrue(self.mirror_path("Журнал.md").is_file())
        first = self.mirror_path("STATE.md").read_bytes()
        self.assertFalse(ingest.write_vault_mirror(self.cfg, "cafe"))
        self.assertEqual(self.mirror_path("STATE.md").read_bytes(), first)
        self.assertFalse(ingest.write_vault_mirror(self.cfg, "cafe", dry_run=True))

        new = self.detail(root_id="t_b2", at=AT + 3600, title="Второй запрос")
        ingest.ingest_request(self.cfg, self.cafe, new)
        self.assertTrue(ingest.write_vault_mirror(self.cfg, "cafe", dry_run=True))
        self.assertTrue(ingest.write_vault_mirror(self.cfg, "cafe"))
        second = self.mirror_path("STATE.md").read_text(encoding="utf-8")
        self.assertIn("Второй запрос", second)

    def test_mirror_content_russian_and_lf(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        ingest.write_vault_mirror(self.cfg, "cafe")
        raw = self.mirror_path("STATE.md").read_bytes()
        self.assertNotIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith("# Лето — состояние проекта"))
        self.assertIn("генерируется приложением «Комната команды»", text)
        self.assertIn("правки руками будут перетёрты", text.lower())
        for heading in ("## Где мы сейчас", "## Этапы проекта", "## Решения", "## Ждёт Михаила",
                        "## Следующие шаги", "## Будущие вопросы", "## Последние запросы"):
            self.assertIn(heading, text)
        self.assertIn("01.11.2026", text)  # срок шага в русском формате
        self.assertIn("[t_a1](#/room/cafe/request/t_a1)", text)
        journal = self.mirror_path("Журнал.md").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"## [А-Яа-я]+ 20\d\d", journal), journal[:200])
        self.assertIn("## ", journal)

    def test_mirror_dry_run_does_not_create_files(self):
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        self.assertTrue(ingest.write_vault_mirror(self.cfg, "cafe", dry_run=True))
        self.assertFalse((self.tmp / "vault" / "cafe").exists())

    def test_contract_path_equals_ingest_path(self):
        """Путь STATE.md в карточке агента == файл, который пишет ingest (config.state_md_path)."""
        from server import contract
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        for room_id in ("cafe", "fn"):
            room = rooms.get_room(self.cfg, room_id)
            self.assertTrue(ingest.write_vault_mirror(self.cfg, room_id))
            promised = Path(contract.state_path(room, self.cfg))
            self.assertTrue(promised.is_file(), "агенту обещан несуществующий файл: %s" % promised)
            self.assertEqual(promised.resolve(), config.state_md_path(self.cfg, room_id).resolve())
            self.assertEqual(promised.parent.name, room_id)
            self.assertEqual(promised.parent.parent.resolve(), Path(self.cfg.vault_dir).resolve())
            self.assertTrue(config.journal_md_path(self.cfg, room_id).is_file())
            body = contract.build_request_body(room, "текст", "r-slug", self.cfg)
            self.assertIn(str(promised), body)

    def test_rename_room_keeps_mirror_in_place(self):
        """Каталог зеркала привязан к room.id: переименование комнаты его не перемещает."""
        ingest.ingest_request(self.cfg, self.cafe, self.detail())
        ingest.write_vault_mirror(self.cfg, "cafe")
        rooms.update_room(self.cfg, "cafe", {"title": "Совсем другое имя"})
        self.assertTrue(ingest.write_vault_mirror(self.cfg, "cafe"))   # заголовок обновился на месте
        self.assertEqual(sorted(p.name for p in (self.tmp / "vault").iterdir()), ["cafe"])
        self.assertIn("Совсем другое имя", self.mirror_path("STATE.md").read_text(encoding="utf-8"))

    def test_room_dir_is_safe_for_odd_ids(self):
        """Путь зеркала не выходит из vault_dir, даже если id содержит разделители."""
        base = Path(self.cfg.vault_dir).resolve()
        for bad in ("../x", "a/b", "a\\b", "C:/evil", ""):
            path = config.state_md_path(self.cfg, bad).resolve()
            self.assertEqual(path.parent.parent, base, bad)


if __name__ == "__main__":
    unittest.main()
