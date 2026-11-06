# -*- coding: utf-8 -*-
"""W17-B BACKEND-DATA: контракт данных страницы запроса и бокового меню.

Что проверяется (WAVE17-RULES.md §1, пакет B §1–§6):

* ``GET /api/rooms/summary`` — счётчики и последние запросы ВСЕХ комнат одним
  ответом (боковое меню больше не шлёт пачку запросов);
* ``GET /api/dispatcher/preview`` — состояние доски ЧТЕНИЕМ, без CLI и без POST;
* ``GET /api/requests/{rid}``: ``timeline`` без ``heartbeat`` по умолчанию
  (+ поле ``heartbeats: {count, last_at}``), ``?include=heartbeat``, ``?fields=``
  и один источник вопросов (дубль ``questions`` убран);
* ``title_short`` (≤ 60 знаков) в сводке, списке и детали — из ``metadata.title``
  руководителя либо из первого предложения без вводных «Так,…»;
* ``POST actions {action:"answers_resend"}`` — накопленные ответы ОДНОЙ карточкой,
  только по клику (никакой автопересылки, запускающей настоящего агента);
* ``B5``: встречный вопрос — только явная кнопка ``need_explanation``; старые
  записи реестра считаются ответами.

Только песочница ``tests.helpers.Sandbox``; CLI Hermes подменён shim'ом; реальная
доска и реальный Hermes сверяются до/после.

    python -m unittest tests.test_api_wave17 -v

# SHIM-OK: CLI Hermes подменён CliShim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.answers as answers_mod          # noqa: E402
import server.registry as registry            # noqa: E402
import server.title_short as title_mod        # noqa: E402
from server.http_util import ApiError         # noqa: E402
from tests.test_answers import LEAD, ROOM, _Base, _record  # noqa: E402

LONG_TITLE = ("Так, начнём работу в этой комнате. Работа будет построена следующим образом. "
              "Для начала ты проверь доступ ко всем подключениям и инструментам, которые нам нужны.")
Q1 = "Передаём папку «гранит 2026» юристу или в другую комнату?"
Q2 = "Добавляем ли биз-финансиста (biz-cfo) в команду?"
A1 = "Эта папка не относится к юристу — передайте в другую комнату."
A2 = "Нет, финансиста не добавляем: команда заточена на Россию."


class TitleShortTests(unittest.TestCase):
    """Чистые правила названия (без сервера): §4, аудит B9."""

    def test_drops_dictation_fillers(self):
        self.assertEqual(title_mod.short_title(LONG_TITLE), "Начнём работу в этой комнате")

    def test_limit_and_ellipsis(self):
        value = title_mod.short_title("Проверь доступ ко всем подключениям (почта компании, Google, 1С) "
                                      "и напиши коротко, что не работает")
        self.assertLessEqual(len(value), title_mod.TITLE_LIMIT)
        self.assertTrue(value.endswith("…"), value)

    def test_junk_lead_is_skipped(self):
        value = title_mod.short_title("Так, теперь. В общем, эта папка не относится к юристу, "
                                      "её надо передать в другую комнату и не считать юридической")
        self.assertTrue(value.startswith("Эта папка"), value)

    def test_empty_text(self):
        self.assertEqual(title_mod.short_title(""), "Запрос")
        self.assertEqual(title_mod.short_title("   "), "Запрос")

    def test_markdown_is_stripped(self):
        value = title_mod.short_title("**Заголовок** с _курсивом_ и [ссылкой](http://x)")
        self.assertNotIn("*", value)
        self.assertNotIn("[", value)

    def test_leader_title_wins(self):
        raw = {"title": "Сдать отчёт по НДС за 4 квартал"}
        self.assertEqual(title_mod.title_from_metadata(raw), "Сдать отчёт по НДС за 4 квартал")
        # каноническая форма normalize кладёт незнакомые ключи в extra
        self.assertEqual(title_mod.title_from_metadata({"extra": {"title": "Из extra"}}), "Из extra")
        self.assertEqual(title_mod.title_from_metadata({}), "")
        self.assertLessEqual(len(title_mod.title_from_metadata({"title": "я" * 200})), title_mod.TITLE_LIMIT)


class DetailViewTests(_Base):
    """Деталь запроса: heartbeat, один источник вопросов, `?fields=`, `title_short`."""

    def detail(self, rid: str, query: dict | None = None):
        # query-значения — списки строк через запятую, как в HTTP (`?fields=a,b`)
        params = {key: [",".join(value) if isinstance(value, list) else str(value)]
                  for key, value in (query or {}).items()}
        status, payload, headers = self.app.dispatch("GET", "/api/requests/%s" % rid,
                                                     params, {})
        self.assertEqual(status, 200, payload)
        return payload, headers

    def _beat(self, rid: str, count: int = 3, base: int | None = None) -> int:
        base = base or int(time.time()) - 60
        con = self.sa.connect()
        try:
            for index in range(count):
                self.sa._event(con, rid, "heartbeat", {"profile": LEAD}, at=base + index)
            con.commit()
        finally:
            con.close()
        return base

    def test_timeline_without_heartbeat(self):
        rid = self.rid()
        base = self._beat(rid, 3)
        detail, _headers = self.detail(rid)
        kinds = [event["kind"] for event in detail["timeline"]]
        self.assertNotIn("heartbeat", kinds, kinds)
        self.assertEqual(detail["heartbeats"]["count"], 3)
        self.assertEqual(detail["heartbeats"]["last_at"], base + 2)

    def test_include_heartbeat_returns_them(self):
        rid = self.rid()
        self._beat(rid, 2)
        detail, _headers = self.detail(rid, {"include": ["heartbeat"]})
        kinds = [event["kind"] for event in detail["timeline"]]
        self.assertEqual(kinds.count("heartbeat"), 2, kinds)
        self.assertEqual(detail["heartbeats"]["count"], 2)

    def test_unknown_include_is_400(self):
        rid = self.rid()
        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("GET", "/api/requests/%s" % rid, {"include": ["everything"]}, {})
        self.assertEqual(ctx.exception.status, 400)

    def test_questions_duplicate_is_gone(self):
        rid = self.rid()
        detail, _headers = self.detail(rid)
        self.assertNotIn("questions", detail)
        self.assertTrue(detail["open_questions"], "вопросы отдаются в open_questions")
        # `?fields=questions` — это блок open_questions, а не второй ключ
        again, _headers = self.detail(rid, {"fields": ["questions"]})
        self.assertTrue(again["open_questions"])
        self.assertNotIn("questions", again)

    def test_fields_limits_blocks(self):
        rid = self.rid()
        detail, _headers = self.detail(rid, {"fields": ["answers", "steps", "questions"]})
        self.assertIn("open_questions", detail)
        self.assertNotIn("questions", detail)
        self.assertIn("answers", detail)
        self.assertIn("next_steps", detail)
        for key in ("tree", "timeline", "thread", "synthesis", "text", "body_full"):
            self.assertNotIn(key, detail, key)
        self.assertEqual(detail["id"], rid, "ядро RequestSummary остаётся всегда")
        self.assertIn("title_short", detail)

    def test_unknown_field_is_400(self):
        rid = self.rid()
        with self.assertRaises(ApiError) as ctx:
            self.app.dispatch("GET", "/api/requests/%s" % rid, {"fields": ["nonsense"]}, {})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("allowed", ctx.exception.detail or {})

    def test_title_short_from_dictation(self):
        root = self.sa.card(LONG_TITLE, LEAD, status="running")
        metadata = {"open_questions": [{"text": Q1, "for": "mikhail"}]}
        self.sa.finish(root, "Готово.", metadata)
        registry.add_request(self.cfg, ROOM, _record(root, title=LONG_TITLE))
        detail, _headers = self.detail(root)
        self.assertLessEqual(len(detail["title_short"]), 60)
        self.assertEqual(detail["title_short"], "Начнём работу в этой комнате")
        self.assertEqual(detail["title"], LONG_TITLE, "полный заголовок не меняется")

    def test_title_short_from_leader_metadata(self):
        root = self.sa.card(LONG_TITLE, LEAD, status="running")
        metadata = {"title": "Разбор папки «гранит 2026»",
                    "open_questions": [{"text": Q1, "for": "mikhail"}]}
        self.sa.finish(root, "Готово.", metadata)
        registry.add_request(self.cfg, ROOM, _record(root, title=LONG_TITLE))
        detail, _headers = self.detail(root)
        self.assertEqual(detail["title_short"], "Разбор папки «гранит 2026»")

    def test_summary_and_list_have_title_short(self):
        rid = self.rid()
        summary = self.app._summary(self.app._room(ROOM), rid)
        self.assertIn("title_short", summary)
        self.assertLessEqual(len(summary["title_short"]), 60)
        status, payload, _headers = self.app.dispatch("GET", "/api/rooms/%s/requests" % ROOM, {}, {})
        self.assertEqual(status, 200, payload)
        found = [item for item in payload["items"] if item.get("id") == rid]
        self.assertTrue(found, payload.get("items"))
        self.assertIn("title_short", found[0])

    def test_etag_does_not_drift_without_real_events(self):
        rid = self.rid()
        self._beat(rid, 2)
        _detail, first = self.detail(rid)
        _detail2, second = self.detail(rid)
        self.assertEqual(first.get("ETag"), second.get("ETag"),
                         "без настоящих событий ETag не должен меняться")

    def test_tree_runs_have_no_duplicate_payload(self):
        rid = self.rid()
        detail, _headers = self.detail(rid)
        for node in detail["tree"]:
            for run in node.get("runs") or []:
                if node.get("metadata"):
                    self.assertNotEqual(run.get("metadata"), node["metadata"],
                                        "metadata попытки дублирует metadata узла")
                if node.get("summary_md"):
                    self.assertNotEqual(run.get("summary"), node.get("summary_md"),
                                        "summary попытки дублирует summary_md узла")


class RoomsSummaryTests(_Base):
    """`GET /api/rooms/summary` — одно чтение на всё боковое меню."""

    def get(self, **query):
        params = {key: [str(value)] for key, value in query.items()}
        try:
            return self.app.dispatch("GET", "/api/rooms/summary", params, {})
        except ApiError as exc:
            return exc.status, exc.payload(), {}

    def test_all_rooms_in_one_response(self):
        rid = self.rid()
        status, payload, _headers = self.get()
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertIn("engine", payload)
        rooms = {room["id"]: room for room in payload["rooms"]}
        self.assertIn(ROOM, rooms)
        room = rooms[ROOM]
        for key in ("counters", "title", "requests_count", "last", "phase", "critic_mode"):
            self.assertIn(key, room, key)
        self.assertEqual(payload["count"], len(payload["rooms"]))
        ids = [item["id"] for item in room["last"]]
        self.assertIn(rid, ids)
        item = next(entry for entry in room["last"] if entry["id"] == rid)
        self.assertIn("title_short", item)
        self.assertEqual(item["room"], ROOM)
        self.assertIn("status", item)

    def test_requests_zero(self):
        self.rid()
        status, payload, _headers = self.get(requests=0)
        self.assertEqual(status, 200, payload)
        self.assertTrue(all(not room["last"] for room in payload["rooms"]))

    def test_one_room_only(self):
        status, payload, _headers = self.get(room=ROOM)
        self.assertEqual(status, 200, payload)
        self.assertEqual([room["id"] for room in payload["rooms"]], [ROOM])

    def test_unknown_room_is_404(self):
        status, payload, _headers = self.get(room="nosuchroom")
        self.assertEqual(status, 404, payload)

    def test_bad_requests_param(self):
        self.assertEqual(self.get(requests="много")[0], 400)
        self.assertEqual(self.get(requests=99)[0], 400)


class DispatcherPreviewTests(_Base):
    """`GET /api/dispatcher/preview` — чтение доски вместо POST kick {preview}."""

    def test_preview_is_get_and_writes_nothing(self):
        status, payload, _headers = self.app.dispatch("GET", "/api/dispatcher/preview", {}, {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["preview"])
        self.assertIn("report", payload)
        self.assertEqual(self.shim.calls, [], "проверка доски не должна звать CLI")

    def test_route_is_not_room_id(self):
        """Маршрут `/api/rooms/summary` не должен уходить в `h_room_get`."""
        status, payload, _headers = self.app.dispatch("GET", "/api/rooms/summary", {}, {})
        self.assertEqual(status, 200, payload)
        self.assertIn("rooms", payload)
        self.assertNotIn("members", payload)


class AnswersResendTests(_Base):
    """`answers_resend`: накопленные ответы одной карточкой и только по клику (§3, B6)."""

    def rid_two(self) -> str:
        root = self.sa.card("Собери команду под Россию", LEAD, status="running")
        self.sa.finish(root, "Команда собрана.", {
            "open_questions": [{"text": Q1, "for": "mikhail"}, {"text": Q2, "for": "mikhail"}],
            "next_steps": [{"text": "Передать папку в другую комнату", "owner": "default"}]})
        registry.add_request(self.cfg, ROOM, _record(root))
        return root

    def questions(self, rid: str) -> list:
        status, payload, _headers = self.app.dispatch("GET", "/api/requests/%s" % rid, {}, {})
        self.assertEqual(status, 200, payload)
        return payload["open_questions"]

    def resend(self, rid: str):
        return self.actions(rid, action="answers_resend")

    def test_nothing_to_resend_is_409(self):
        rid = self.rid()
        status, payload, _headers = self.resend(rid)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["detail"]["reason"], "nothing_to_resend")

    def test_one_card_with_all_answers(self):
        rid = self.rid_two()
        q1, q2 = self.questions(rid)
        self.answer(rid, q1["id"], A1)
        self.answer(rid, q2["id"], A2)
        self.shim.calls.clear()
        self.shim.bodies.clear()
        status, payload, _headers = self.resend(rid)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["action"], "answers_resend")
        self.assertEqual(payload["sent"], 2, payload)
        self.assertEqual(payload["question_ids"], [q1["id"], q2["id"]], "порядок вопросов ответа")
        texts = self.shim.comment_texts()
        digest = [text for text in texts if text.startswith(answers_mod.DIGEST_MARK)]
        self.assertEqual(len(digest), 1, texts)
        self.assertIn(q1["id"], digest[0])
        self.assertIn(q2["id"], digest[0])
        self.assertIn(A1, digest[0])
        self.assertIn(A2, digest[0])
        self.assertEqual(len(self.shim.creates()), 1, "одна карточка руководителю")
        body = self.shim.bodies[-1]
        self.assertIn("НАКОПЛЕННЫЕ ОТВЕТЫ", body)
        self.assertIn(A2, body)
        stored = registry.get(self.cfg, ROOM, rid)["answers"]
        for qid in (q1["id"], q2["id"]):
            self.assertTrue(stored[qid].get("resent_at"), qid)
            self.assertEqual(stored[qid].get("resend_count"), 1)

    def test_reacted_answer_is_not_resent(self):
        rid = self.rid_two()
        q1, q2 = self.questions(rid)
        self.answer(rid, q1["id"], A1)
        self.answer(rid, q2["id"], A2)
        self.sa.comment(rid, LEAD, "%s %s] Принято: папку передали" % (answers_mod.REACTION_MARK, q1["id"]))
        status, payload, _headers = self.resend(rid)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["question_ids"], [q2["id"]])

    def test_all_reacted_is_409(self):
        rid = self.rid_two()
        q1, q2 = self.questions(rid)
        self.answer(rid, q1["id"], A1)
        self.answer(rid, q2["id"], A2)
        for qid in (q1["id"], q2["id"]):
            self.sa.comment(rid, LEAD, "%s %s] Принято: учтено" % (answers_mod.REACTION_MARK, qid))
        status, payload, _headers = self.resend(rid)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["detail"]["reason"], "nothing_to_resend")

    def test_summary_flags_button(self):
        rid = self.rid_two()
        q1, _q2 = self.questions(rid)
        self.answer(rid, q1["id"], A1)
        detail = self.detail(rid)
        summary = detail["answers"]["summary"]
        self.assertTrue(summary["can_resend"])
        self.assertEqual(summary["resend_pending"], 1)
        self.assertIn("накопленные ответы", summary["resend_label"])


class BacklogQuestionTruthTests(_Base):
    """B-7: «Дальше» знает об ответе, близнецы схлопнуты, служебные вопросы скрыты.

    Образцы — настоящие данные комнаты `fn` (запрос `t_1b0aa253`), сохранённые в
    `fixtures/questions_live_fn.json`: q_e7b16109 (отвечен, а в бэклоге был `open`),
    q_22b45cc3/q_18b309bf (один вопрос с разными id), q_6f877b08 (раунды критика).
    """

    live = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = ROOT / "fixtures" / "questions_live_fn.json"
        cls.live = json.loads(path.read_text(encoding="utf-8"))

    def seed_backlog(self, items: list, request: str = "t_1b0aa253") -> None:
        directory = Path(self.cfg.state_dir) / ROOM
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"room": ROOM, "updated_at": int(time.time()), "items": items}
        (directory / "backlog.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        record = registry.get(self.cfg, ROOM, request) or _record(request)
        record["answers"] = dict(self.live["answers_registry"])
        registry.add_request(self.cfg, ROOM, record)

    def live_items(self) -> list:
        return [{"id": item["id"], "kind": item["kind"], "text": item["text"],
                 "status": "open", "created_at": int(time.time()) - 3600,
                 "updated_at": int(time.time()) - 3600, "request_id": "t_1b0aa253",
                 "source": "t_1b0aa253"} for item in self.live["backlog_questions"]]

    def backlog(self, query: dict | None = None) -> dict:
        status, payload, _headers = self.app.dispatch("GET", "/api/rooms/%s/backlog" % ROOM,
                                                      query or {}, {})
        self.assertEqual(status, 200, payload)
        return payload

    def test_answered_question_leaves_inbox(self):
        """B7-1: ответ в реестре закрывает вопрос в «Дальше» (было status=open)."""
        self.seed_backlog(self.live_items())
        before = self.backlog()
        item = next(i for i in before["items"] if i["id"] == "t_1b0aa253:q_e7b16109")
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["answer_state"], "sent")
        self.assertEqual(item["state_label"], "отвечен")
        self.assertNotIn(item["id"], {i["id"] for i in before["sections"]["inbox"]})
        self.assertGreater(before["counts"]["done"], 0)

    def test_waiting_explanation_is_labelled(self):
        """Просьба пояснить: вопрос остаётся, но с подписью «ждёт пояснения» (B5 + B7)."""
        self.seed_backlog(self.live_items())
        view = self.backlog()
        item = next(i for i in view["items"] if i["id"] == "t_1b0aa253:q_7985e685")
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["answer_state"], "waiting_explanation")
        self.assertEqual(item["state_label"], "ждёт пояснения")

    def test_twins_collapse_and_counter_is_unique(self):
        """B7-2: один вопрос с разными id — один элемент, счётчики по уникальным."""
        self.seed_backlog(self.live_items())
        view = self.backlog()
        twins = [i for i in view["items"]
                 if "маркшейдер" in (i.get("text") or "").lower()
                 and i.get("kind") == "open_question"]
        self.assertEqual(len(twins), 1, [i["id"] for i in twins])
        self.assertEqual(len(twins[0].get("aliases") or []), 1, twins[0])
        # счётчики считают уникальные: total = len(items)
        self.assertEqual(view["counts"]["total"], len(view["items"]))

    def test_answer_on_twin_id_closes_backlog_item(self):
        """B7-2: ответ дан на q_18b309bf (деталь), а в бэклоге живёт q_22b45cc3."""
        self.seed_backlog(self.live_items())
        item = next(i for i in self.backlog()["items"] if i["id"] == "t_1b0aa253:q_22b45cc3")
        self.assertEqual(item["status"], "done", item)

    def test_service_question_hidden_from_backlog(self):
        """B7-3: «раунды критика» не висят в «Дальше»."""
        self.seed_backlog(self.live_items())
        view = self.backlog()
        texts = [str(i.get("text") or "").lower() for i in view["items"]]
        self.assertFalse([t for t in texts if "порог обязательного прогона через критика" in t], texts)
        self.assertEqual(view["counts"]["hidden_service"], 2)

    def test_detail_hides_service_and_dedupes(self):
        """Деталь: служебный вопрос скрыт, близнецы — один элемент с aliases."""
        # боевые близнецы: «(маркшейдер, TaskFlow)» и «(маркшейдер)» — один вопрос
        twins = [item["text"] for item in self.live["backlog_questions"]
                 if "маркшейдер" in item["text"]]
        self.assertEqual(len(twins), 2, twins)
        root = self.sa.card("Проверка вопросов", LEAD, status="running")
        self.sa.finish(root, "Готово.", {
            "open_questions": [{"text": twins[0], "for": "mikhail"},
                               {"text": twins[1], "for": "mikhail"},
                               {"text": self.live["backlog_questions"][1]["text"], "for": "mikhail"}]})
        registry.add_request(self.cfg, ROOM, _record(root))
        status, detail, _headers = self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})
        self.assertEqual(status, 200, detail)
        self.assertEqual(len(detail["open_questions"]), 1, detail["open_questions"])
        merged = detail["open_questions"][0]
        self.assertEqual(len(merged.get("aliases") or []), 1, merged)
        self.assertNotIn("порог обязательного прогона", " ".join(
            str(q.get("text") or "").lower() for q in detail["open_questions"]))
        self.assertNotIn("hidden_questions", detail)

    def test_answer_counters_agree_between_pages(self):
        """Счётчики вкладки «Дальше» и вопроса в детали сходятся после ответа."""
        root = self.sa.card("Согласование", LEAD, status="running")
        text = "Нужен ли отдельный профиль агента для TaskFlow?"
        self.sa.finish(root, "Готово.", {"open_questions": [{"text": text, "for": "mikhail"}]})
        registry.add_request(self.cfg, ROOM, _record(root))
        self.seed_backlog([{"id": "%s:q_1" % root, "kind": "open_question", "text": text,
                            "status": "open", "created_at": int(time.time())}], request=root)
        status, detail, _headers = self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})
        self.assertEqual(status, 200, detail)
        qid = detail["open_questions"][0]["id"]
        before = self.backlog()
        self.assertEqual(len(before["sections"]["inbox"]), 1)
        self.assertEqual(self.actions(root, action="answer", question_id=qid,
                                      text="Да, отдельный профиль нужен.")[0], 200)
        after = self.backlog()
        self.assertEqual(after["sections"]["inbox"], [])
        self.assertEqual(after["counts"]["open"], 0)
        status, detail, _headers = self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})
        self.assertTrue(detail["open_questions"][0]["answered"])
        self.assertFalse(detail["needs_you"])


if __name__ == "__main__":
    unittest.main()
