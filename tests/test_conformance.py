"""Соответствие ЖИВОГО бэкенда контракту: `fixtures/*.json` + `docs/API.md`.

Два уровня:

* `ConformanceReportTest` прогоняет `tools/check_conformance.py` целиком (все 26
  фикстур + POST/PATCH/DELETE) и требует 0 нарушений;
* `ConformanceSemanticsTest` поднимает живой сервер один раз и проверяет решения
  контракта, которые структурной сверкой не ловятся: `needs_you` у завершённого
  запроса с открытым вопросом Михаилу, форма `/api/profiles`, `schema_version`,
  сохранение ответа на вопрос, `waiting` у включённого критика без карточки.

Реальная доска не трогается: всё идёт в песочницу (`tests.helpers.Sandbox`),
`Live` в конце сверяет слепок реальной `kanban.db`.

    python -m unittest tests.test_conformance -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers import Sandbox, assert_real_untouched, snapshot_real_db   # noqa: E402
from tools import check_conformance as conf                                   # noqa: E402


class ConformanceReportTest(unittest.TestCase):
    """Полный отчёт по фикстурам и POST-эндпоинтам: нарушений быть не должно."""

    def test_01_report_is_clean(self):
        code = conf.run(with_posts=True)
        self.assertEqual(code, 0, "check_conformance нашёл расхождения с контрактом")


class ConformanceSemanticsTest(unittest.TestCase):
    """Смысловые требования контракта, которые не выражаются типом поля."""

    @classmethod
    def setUpClass(cls):
        cls.live = conf.Live().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.live.__exit__(None, None, None)

    # --- helper ---
    def get(self, path: str) -> dict:
        status, payload, _headers = self.live.get(path)
        self.assertEqual(status, 200, "%s → %s" % (path, payload))
        return payload

    # --- тесты ---
    def test_01_health_schema_version(self):
        health = self.get("/api/health")
        engine = health["engine"]
        self.assertTrue(engine["schema_ok"])
        self.assertIsInstance(engine["schema_version"], int)
        self.assertGreater(engine["schema_version"], 0)
        self.assertEqual(engine["tasks"]["total"] > 0, True)
        self.assertIn(health["dispatcher"]["state"], ("running", "stopped", "stale", "unknown"))

    def test_02_profiles_are_profile_shape(self):
        payload = self.get("/api/profiles")
        self.assertEqual(payload["count"], len(payload["items"]))
        self.assertTrue(payload["items"], "ростер песочницы пуст")
        # WAVE13 §11: к подписи профиля добавлены name/job/label/label_source (+ custom_label,
        # label_pending — общая форма с TeamMember, см. docs/API.md §3.5).
        for item in payload["items"]:
            self.assertEqual(set(item), {"profile", "title", "description", "tags", "kind", "available",
                                          "name", "job", "label", "label_source", "custom_label",
                                          "label_pending"})
            self.assertIn(item["kind"], ("director", "worker", "critic", "utility"))
            self.assertIsInstance(item["tags"], list)
            self.assertIsInstance(item["available"], bool)
        kinds = {item["profile"]: item["kind"] for item in payload["items"]}
        self.assertEqual(kinds.get("biz-director"), "director")
        self.assertEqual(kinds.get("biz-critic"), "critic")

    def test_03_done_with_open_question_keeps_done_and_flags_needs_you(self):
        rid = self.live.root_of("done_modern")
        detail = self.get("/api/requests/" + rid)
        self.assertEqual(detail["status"], "done")          # вопрос не перебивает done
        self.assertTrue(detail["needs_you"])                 # но флаг «нужен человек» стоит
        questions = [q for q in detail["open_questions"]
                     if q["for"] == "mikhail" and not q["answered"]]
        self.assertTrue(questions, "в ответе нет вопроса Михаилу")
        rooms = self.get("/api/rooms")["rooms"]
        cafe = next(room for room in rooms if room["id"] == "cafe")
        self.assertGreaterEqual(cafe["counters"]["needs_you"], 1)

    def test_04_queued_request_is_led_by_director(self):
        rid = self.live.root_of("queued")
        summary = self.get("/api/requests/" + rid)
        self.assertEqual(summary["status"], "queued")
        self.assertEqual(summary["assignee"], "biz-director")   # ведёт директор комнаты
        self.assertIsNone(summary["started_at"])                # работу ещё не начинали
        self.assertEqual(summary["critic"]["state"], "waiting")  # критик включён, карточки нет
        self.assertFalse(summary["needs_you"])

    def test_05_candidates_carry_preview_and_decomposed_at(self):
        payload = self.get("/api/rooms/cafe/candidates")
        self.assertEqual(payload["count"], len(payload["items"]))
        self.assertIsInstance(payload["generated_at"], int)
        self.assertTrue(payload["items"])
        for item in payload["items"]:
            self.assertEqual(item["task_id"], item["id"])
            self.assertIn("preview", item)
            self.assertIsInstance(item["decomposed_at"], int)
            self.assertIsInstance(item["children"], int)

    def test_06_legacy_request_is_normalized(self):
        rid = self.live.root_of("done_legacy")
        detail = self.get("/api/requests/" + rid)
        self.assertEqual(detail["status"], "done")
        self.assertTrue(detail["synthesis"]["legacy"])
        self.assertEqual(detail["synthesis"]["state"], "ready")
        self.assertIn("legacy_metadata", [w["code"] for w in detail["warnings"]])
        self.assertEqual(detail["stage"]["current"], "legal")
        self.assertEqual([p["status"] for p in detail["stage"]["phases"]],
                         ["done", "blocked", "blocked", "active", "next", "next"])
        self.assertEqual(detail["critic"]["state"], "off")
        self.assertFalse(detail["critic"]["enabled"])

    def test_07_outside_room_child_is_warned(self):
        rid = self.live.root_of("outside_room")
        detail = self.get("/api/requests/" + rid)
        outside = [node for node in detail["tree"] if node["outside_room"]]
        self.assertEqual([node["assignee"] for node in outside], ["analyst"])
        self.assertIn("outside_room", [w["code"] for w in detail["warnings"]])

    def test_08_answer_is_persisted_and_clears_needs_you(self):
        rid = self.live.root_of("done_modern")
        detail = self.get("/api/requests/" + rid)
        question = next(q for q in detail["open_questions"] if q["for"] == "mikhail" and not q["answered"])
        status, payload, _headers = self.live.post(
            "/api/requests/%s/actions" % rid,
            {"action": "answer", "question_id": question["id"], "text": "Держим R$ 42 без доставки."})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["question"]["answered"])
        self.assertEqual(payload["question"]["answer"], "Держим R$ 42 без доставки.")
        self.assertIsInstance(payload["question"]["answered_at"], int)
        fresh = self.get("/api/requests/" + rid)
        same = next(q for q in fresh["open_questions"] if q["id"] == question["id"])
        self.assertTrue(same["answered"])
        self.assertEqual(same["answer"], "Держим R$ 42 без доставки.")
        self.assertEqual(fresh["status"], "done")
        self.assertFalse(fresh["needs_you"])
        # повторный ответ — конфликт, а не тихая перезапись
        status, payload, _headers = self.live.post(
            "/api/requests/%s/actions" % rid,
            {"action": "answer", "question_id": question["id"], "text": "Ещё раз."})
        self.assertEqual(status, 409, payload)

    def test_09_real_board_untouched(self):
        before = snapshot_real_db()
        after = assert_real_untouched(before)
        self.assertEqual(before, after)


class RealLegacyFixtureTest(unittest.TestCase):
    """Реальный `t_605613e0` — только чтение доски: GET после adopt совпадает с фикстурой.

    `TEAMROOM_DATA`/`TEAMROOM_VAULT_DIR` — во временных каталогах, `TEAMROOM_HERMES_HOME`
    снят, чтобы читать настоящий `kanban_home`; в доску не пишем (adopt = запись в реестр
    комнаты), слепок `kanban.db` до/после совпадает.
    """

    RID = "t_605613e0"
    ENV_KEYS = ("TEAMROOM_HERMES_HOME", "HERMES_KANBAN_HOME", "TEAMROOM_DATA", "TEAMROOM_VAULT_DIR")

    def test_real_legacy_detail_matches_fixture(self):
        import json
        import os
        import shutil
        import tempfile
        import threading

        import server.app as app_mod
        import server.config as config_mod

        before = snapshot_real_db()
        if not before.get("exists"):
            self.skipTest("реальная kanban.db не найдена")
        tmp = Path(tempfile.mkdtemp(prefix="tr-conform-real-",
                                    dir=str(Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
                                            / "Temp")))
        saved = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in ("TEAMROOM_HERMES_HOME", "HERMES_KANBAN_HOME"):
            os.environ.pop(key, None)
        os.environ["TEAMROOM_DATA"] = str(tmp / "data")
        os.environ["TEAMROOM_VAULT_DIR"] = str(tmp / "vault")
        server = thread = None
        try:
            cfg = config_mod.load()
            cfg.ensure_dirs()
            self.assertFalse(cfg.sandbox)
            server = app_mod.create_app(cfg, start_watcher=False, port=0)
            base = "http://127.0.0.1:%d" % server.server_address[1]
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()

            import urllib.error as urlerror
            import urllib.request as urlrequest

            def call(method, path, body=None):
                data = None
                headers = {"Accept": "application/json"}
                if body is not None:
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                req = urlrequest.Request(base + path, data=data, headers=headers, method=method)
                try:
                    with urlrequest.urlopen(req, timeout=60) as resp:
                        return resp.status, json.loads(resp.read())
                except urlerror.HTTPError as exc:
                    raw = exc.read()
                    payload = json.loads(raw) if raw[:1] in (b"{", b"[") else raw
                    exc.close()
                    return exc.code, payload

            status, adopted = call("POST", "/api/adopt", {"room": "cafe", "task_id": self.RID})
            self.assertEqual(status, 200, adopted)
            self.assertTrue(adopted["adopted"])
            status, detail = call("GET", "/api/requests/" + self.RID)
            self.assertEqual(status, 200, detail)

            with open(ROOT / "fixtures" / "request_done_legacy.json", encoding="utf-8") as fh:
                fixture = json.load(fh)
            violations = conf.Violations("t_605613e0 ↔ request_done_legacy.json")
            conf.compare(fixture, detail, [], conf.REQUEST_ENUMS, violations, conf.DEFAULT_SHALLOW)
            self.assertFalse(violations.failed, violations.line())

            # смысл legacy-нормализации и решение про needs_you
            self.assertEqual(detail["status"], "done")
            self.assertTrue(detail["needs_you"])          # 7 вопросов Михаилу без ответа
            self.assertEqual(detail["synthesis"]["version"], 1)
            self.assertTrue(detail["synthesis"]["legacy"])
            self.assertFalse(detail["critic"]["enabled"])
            self.assertEqual(detail["stage"]["current"], "legal")
            self.assertEqual(len(detail["next_steps"]), 5)
            self.assertEqual(len(detail["open_questions"]), 7)
            self.assertEqual(detail["room"], "cafe")        # tenant карточки остался NULL
        finally:
            if server is not None:
                try:
                    server.shutdown()
                    server.server_close()
                except Exception:  # noqa: BLE001
                    pass
            if thread is not None:
                thread.join(timeout=5)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp, ignore_errors=True)
            self.assertEqual(assert_real_untouched(before), before)


if __name__ == "__main__":
    unittest.main()
