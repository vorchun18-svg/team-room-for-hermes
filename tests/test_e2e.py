"""Сквозной сценарий бэкенда «Комнаты команды» на песочнице — без LLM.

Что проверяет (приёмка §B задания интегратора):

1. реальный сервер (``create_app`` + настоящий CLI канбана в песочнице) на
   свободном порту, watcher — вручную через ``tick_once()``;
2. путь запроса: queued → decompose → retarget → working → дети done → root done
   → критик → reviewing → вердикт revise → synthesizing → доработка → ingest →
   done, synthesis.version=2, история из двух версий, бэклог/дорожная карта/
   решения заполнены, зеркало Vault записано по-русски;
3. идемпотентность: повторный тик не создаёт карточек и не дублирует элементы;
4. «Задать» из бэклога создаёт запрос тем же путём, что POST /requests;
5. adopt кандидата регистрирует чужую карточку без записи в доску;
6. экспорт .md/.html и .ics по шагу со сроком, поиск по слову из ответа;
7. реальная kanban.db не изменилась (слепок до/после в setUpClass/tearDownClass).

Тест НЕ трогает реальную доску: все записи идут в песочницу через
``SimAgents`` (прямой sqlite только внутри ``TEAMROOM_HERMES_HOME``).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                       # noqa: E402
import server.search as search_mod                 # noqa: E402
from tests.helpers import Sandbox, SimAgents, assert_real_untouched, snapshot_real_db   # noqa: E402

#: Запреты на весь модуль: часть классов работает на реальной доске (только чтение)
#: и на настоящем сервере — запись в реальный Hermes/Vault и агентский CLI запрещены.
_GUARDS: list = []


def setUpModule():
    from server import safety

    _GUARDS.extend([safety.forbid_real_writes(), safety.forbid_real_subprocess()])
    for cm in _GUARDS:
        cm.__enter__()


def tearDownModule():
    while _GUARDS:
        cm = _GUARDS.pop()
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


FIXTURES = ROOT / "fixtures"

ANSWER_V1 = (
    "# Ответ по каналу D19\n\n"
    "Канал V2 выгоднее: доставка собственным курьером даёт маржу 41% против 28% у V1.\n"
    "Точка безубыточности — 74 набора в месяц, запас прочности две недели.\n\n"
    "## Что сделать\n\n"
    "1. Снять блокер канала и согласовать упаковку.\n"
)
ANSWER_V2 = (
    "# Ответ по каналу D19 (версия 2)\n\n"
    "Канал V2 выгоднее: доставка собственным курьером даёт маржу 44% против 28% у V1.\n"
    "Точка безубыточности — 71 набор в месяц; запас прочности три недели.\n\n"
    "## Что сделать\n\n"
    "1. Снять блокер канала и подписать договор с курьером.\n"
    "2. Согласовать упаковку с поставщиком Embalagens городе.\n"
)
META_V1 = {
    "next_steps": [
        {"text": "Снять блокер канала TG/WA", "owner": "mikhail", "due": "2026-11-15"},
        {"text": "Согласовать упаковку с поставщиком", "owner": "biz-ops", "due": "2026-11-20"},
    ],
    "open_questions": [],
    "decisions": [{"text": "Канал D19 — V2 с собственным курьером", "why": "маржа выше на 13 п.п.",
                   "status": "proposed"}],
    "stage": {"current": "production", "phases": [
        {"id": "idea", "status": "done", "note": "VISION и брифы утверждены"},
        {"id": "legal", "status": "done", "note": "ИП подан"},
        {"id": "packaging", "status": "done", "note": "котировки собраны"},
        {"id": "production", "status": "active", "note": "тестовая кухня 150 моти в неделю"},
    ]},
    "future_questions": ["Когда нанимать второго повара?"],
    "artifacts": [],
}
META_V2 = {
    "next_steps": [
        # первый шаг тот же по смыслу, но переписан; второй — новый
        {"text": "Снять блокер канала и подписать договор с курьером", "owner": "mikhail",
         "due": "2026-11-18"},
        {"text": "Согласовать упаковку с Embalagens городе", "owner": "biz-ops", "due": "2026-11-22"},
    ],
    "open_questions": [],
    "decisions": [{"text": "Канал D19 — V2 с собственным курьером", "why": "маржа выше на 13 п.п.",
                   "status": "accepted"}],
    "stage": {"current": "sales", "phases": [
        {"id": "production", "status": "done", "note": "кухня запущена"},
        {"id": "sales", "status": "active", "note": "первые 12 наборов проданы"},
    ]},
    "future_questions": ["Когда нанимать второго повара?"],
    "artifacts": [],
}
CRITIC_SUMMARY = ("Ответ не готов к выдаче: маржа посчитана без доставки, шаг 2 невыполним "
                  "без поставщика. Нужна доработка с пересчётом.")
CRITIC_ISSUES = [
    {"where": "Раздел «Что сделать», п. 3", "problem": "Маржа 41% без учёта доставки",
     "fix": "Пересчитать маржу с доставкой R$ 9,00"},
    {"where": "Раздел «Что сделать», п. 2", "problem": "Упаковка без поставщика",
     "fix": "Указать Embalagens городе и срок 5 дней"},
]


class Api:
    """Минимальный HTTP-клиент к живому серверу (loopback, без внешней сети)."""

    def __init__(self, port: int) -> None:
        self.port = int(port)

    def request(self, method: str, path: str, body=None, headers: dict | None = None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            ctype = resp.getheader("Content-Type") or ""
            data = None
            if "json" in ctype.lower() and raw:
                data = json.loads(raw.decode("utf-8"))
            elif raw and "text" in ctype.lower() or "calendar" in ctype.lower():
                data = raw.decode("utf-8", "replace")
            return resp.status, dict(resp.getheaders()), data, raw
        finally:
            conn.close()

    def get(self, path: str, headers: dict | None = None):
        return self.request("GET", path, headers=headers)

    def post(self, path: str, body=None):
        return self.request("POST", path, body if body is not None else {})


def _keys(value) -> set:
    return set(value) if isinstance(value, dict) else set()


def _diff_keys(path: str, got, want, out: list) -> None:
    """Сравнить наборы ключей объектов (рекурсивно, до 3 уровней)."""
    if not isinstance(got, dict) or not isinstance(want, dict):
        return
    missing = sorted(_keys(want) - _keys(got))
    extra = sorted(_keys(got) - _keys(want))
    if missing or extra:
        out.append({"path": path, "missing": missing, "extra": extra})
    for key in sorted(_keys(want) & _keys(got)):
        gv, wv = got[key], want[key]
        if isinstance(gv, dict) and isinstance(wv, dict):
            _diff_keys(f"{path}.{key}", gv, wv, out)
        elif isinstance(gv, list) and isinstance(wv, list) and gv and wv:
            if isinstance(gv[0], dict) and isinstance(wv[0], dict):
                _diff_keys(f"{path}[0]", gv[0], wv[0], out)


class E2ETest(unittest.TestCase):
    """Один сервер и одна песочница на весь набор: шаги сценария упорядочены."""

    api: Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.before_real = snapshot_real_db()
        cls._sbx = Sandbox()
        cls.sb = cls._sbx.__enter__()
        cls.cfg = cls.sb.cfg
        cls.sim = SimAgents(cls.sb)
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=0)
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05},
                                      name="team-room-e2e", daemon=True)
        cls.thread.start()
        cls.api = Api(cls.port)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server.shutdown()
            cls.server.server_close()
        finally:
            cls.thread.join(timeout=5)
            try:
                cls.sb.assert_real_untouched(cls.before_real)
            finally:
                cls._sbx.__exit__(None, None, None)

    # --- помощники ---------------------------------------------------------
    def ok(self, method: str, path: str, body=None):
        status, _headers, data, _raw = self.api.request(method, path, body)
        self.assertLess(status, 300, "%s %s → %s: %s" % (method, path, status, data))
        return data

    def detail(self, rid: str) -> dict:
        return self.ok("GET", "/api/requests/" + rid)

    def tick(self) -> dict:
        stats = self.server.http_app.watcher.tick_once()
        self.assertIsNone(self.server.http_app.watcher.last_error, stats)
        return stats

    def backlog(self) -> dict:
        return self.ok("GET", "/api/rooms/cafe/backlog")

    def card_count(self) -> int:
        return int(self.sb.one("SELECT count(*) AS n FROM tasks")["n"])

    # --- сценарий ----------------------------------------------------------
    def test_01_full_cycle(self):
        cards_at_start = self.card_count()
        created = self.ok("POST", "/api/rooms/cafe/requests", {
            "text": "Разбери канал D19: что запускать первым — V1 или V2, и сколько заработаем.",
            "title": "Канал D19: V1 или V2"})
        rid = created["root_id"]
        self.assertTrue(rid.startswith("t_"))
        self.assertEqual(created["request"]["status"], "queued")
        self.assertEqual(self.detail(rid)["status"], "queued")
        self.assertTrue(Path(self.cfg.kanban_db("team")).is_file())

        # --- decompose: дети running, связи child_id=root, событие decomposed
        kids = self.sim.decompose(rid, [
            {"title": "Срез: экономика каналов", "assignee": "biz-cfo"},
            {"title": "Срез: доставка и упаковка", "assignee": "biz-ops"},
        ])
        self.assertEqual(len(kids), 2)
        self.assertEqual(self.card_count(), cards_at_start + 3)   # корень + 2 ребёнка

        # --- тик: корень переназначен на директора комнаты, запрос «в работе»
        stats = self.tick()
        self.assertEqual(stats["errors"], 0, stats)
        detail = self.detail(rid)
        self.assertEqual(detail["status"], "working")
        self.assertEqual(detail["assignee"], "biz-director")
        self.assertEqual(len(detail["tree"]), 3)
        self.assertEqual([n["role"] for n in detail["tree"]], ["root", "worker", "worker"])
        self.assertEqual(detail["synthesis"]["state"], "pending")
        self.assertIn("state", detail["critic"])          # блок критика всегда есть

        # --- дети готовы, корень свёл ответ
        self.sim.finish(kids[0], "Экономика: V2 даёт маржу 41%.", {"next_steps": []})
        self.sim.finish(kids[1], "Доставка: свой курьер дешевле на R$ 4.", {"next_steps": []})
        self.sim.start(rid)
        self.sim.finish(rid, ANSWER_V1, META_V1)
        detail = self.detail(rid)
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["synthesis"]["version"], 1)
        self.assertEqual(detail["synthesis"]["text_md"], ANSWER_V1)
        self.assertEqual(detail["critic"]["state"], "waiting")
        self.assertTrue(detail["critic"]["enabled"])

        # --- тик: критик создан (карточка-ребёнок), версия 1 ушла в ingest
        stats = self.tick()
        self.assertEqual(stats["errors"], 0, stats)
        detail = self.detail(rid)
        self.assertEqual(detail["status"], "reviewing")
        self.assertEqual(detail["critic"]["state"], "reviewing")
        self.assertEqual(detail["critic"]["round"], 1)
        critic_id = detail["critic"]["task_id"]
        self.assertTrue(critic_id and critic_id.startswith("t_"))
        self.assertIn(critic_id, [n["id"] for n in detail["tree"]])
        self.assertIn(self.sb.one("SELECT status FROM tasks WHERE id=?",
                                  (critic_id,))["status"], ("todo", "ready"))
        # ingest версии 1 уже прошёл
        roadmap = self.ok("GET", "/api/rooms/cafe/roadmap")
        self.assertEqual([p["id"] for p in roadmap["phases"]],
                         ["idea", "legal", "brand", "packaging", "production", "sales"])
        self.assertTrue(any(p["status"] != "next" for p in roadmap["phases"]))
        first_items = [i for i in self.backlog()["items"] if i["source_request"] == rid]
        self.assertTrue(first_items)
        self.v1_step_ids = sorted(i["id"] for i in first_items if i["kind"] == "next_step")
        self.assertEqual(len(self.v1_step_ids), 2)

        # --- критик вернул revise → «на доработку», ответ ещё не финальный
        self.sim.finish(critic_id, CRITIC_SUMMARY, verdict="revise", issues=CRITIC_ISSUES)
        detail = self.detail(rid)
        self.assertEqual(detail["critic"]["state"], "revised")
        self.assertEqual(detail["critic"]["verdict"], "revise")
        self.assertEqual(len(detail["critic"]["issues"]), 2)
        self.assertEqual(detail["status"], "synthesizing")   # доработки ещё нет

        # --- тик: создана карточка доработки
        cards_before = self.card_count()
        self.tick()
        self.assertEqual(self.card_count(), cards_before + 1)
        detail = self.detail(rid)
        self.assertEqual(detail["status"], "synthesizing")
        revisions = [n for n in detail["tree"] if n["role"] == "revision"]
        self.assertEqual(len(revisions), 1)
        revision_id = revisions[0]["id"]
        self.assertEqual(detail["synthesis"]["version"], 1)   # доработка ещё не готова

        # --- доработка готова → версия 2
        self.sim.finish(revision_id, ANSWER_V2, META_V2)
        detail = self.detail(rid)
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["synthesis"]["version"], 2)
        self.assertEqual(detail["synthesis"]["text_md"], ANSWER_V2)
        self.assertEqual(len(detail["synthesis"]["history"]), 2)
        self.assertEqual([h["kind"] for h in detail["synthesis"]["history"]], ["initial", "revision"])

        # --- тик: ingest версии 2 (раунды исчерпаны — новых карточек нет)
        cards_before = self.card_count()
        stats = self.tick()
        # Страница запроса уже приняла версию 2 (ensure при открытии done-запроса,
        # WAVE8 Блок B), поэтому тик ничего не добавляет — приём ровно один.
        self.assertIn(stats["ingested"], (0, 1), stats)
        self.assertEqual(self.card_count(), cards_before)     # ни критика, ни доработки
        detail = self.detail(rid)
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["critic"]["state"], "revised")
        self.assertEqual(detail["critic"]["round"], 1)

        # бэклог/решения/фазы заполнены из ответа, без дублей по версиям
        backlog = self.backlog()
        mine = [i for i in backlog["items"] if i["source_request"] == rid]
        self.assertTrue(mine)
        self.assertEqual(len({i["text"] for i in mine}), len(mine))   # тексты не повторяются
        self.assertEqual(len({i["id"] for i in mine}), len(mine))
        self.assertTrue(any(i["kind"] == "next_step" for i in mine))
        self.assertTrue(any(i["kind"] == "decision" and i["status"] == "done" for i in mine))
        roadmap = self.ok("GET", "/api/rooms/cafe/roadmap")
        self.assertEqual((roadmap["current"] or {}).get("id"), "sales")
        self.assertTrue(any(d["text"].startswith("Канал D19") for d in roadmap["decisions"]))
        step_ids = [i["id"] for i in mine if i["kind"] == "next_step"]
        self.assertTrue(step_ids)
        # вторая версия обновила шаги на месте: те же id, новый текст, без копий
        self.assertEqual(sorted(step_ids), self.v1_step_ids)
        self.assertTrue(any("курьер" in i["text"] for i in mine if i["kind"] == "next_step"))

        # зеркало Vault: файл создан и по-русски
        mirror = self.cfg.vault_dir / "cafe" / "STATE.md"
        self.assertTrue(mirror.is_file(), list((self.cfg.vault_dir).rglob("*.md")))
        text = mirror.read_text(encoding="utf-8")
        self.assertRegex(text, r"[А-Яа-яЁё]{4,}")
        self.assertIn("Канал D19", text)

        # --- повторный тик ничего не меняет (идемпотентность)
        snapshot = (self.card_count(), len(self.backlog()["items"]), len(self.extra_cards(rid)))
        second = self.tick()
        self.assertEqual(second["ingested"], 0, second)
        self.assertEqual((self.card_count(), len(self.backlog()["items"]),
                          len(self.extra_cards(rid))), snapshot)
        self.assertEqual(len([n for n in self.detail(rid)["tree"] if n["role"] == "critic"]), 1)

        # --- «Задать» из бэклога: новый запрос тем же путём
        item = next(i for i in mine if i["kind"] == "next_step")
        asked = self.ok("POST", "/api/rooms/cafe/backlog/%s/ask" % quote(item["id"], safe=""), {})
        self.assertEqual(asked["root_id"], asked["request_id"])
        self.assertNotEqual(asked["root_id"], rid)
        self.assertEqual(asked["item"]["status"], "asked")
        self.assertEqual(asked["item"]["asked_request"], asked["root_id"])
        asked_detail = self.detail(asked["root_id"])
        self.assertEqual(asked_detail["status"], "queued")
        self.assertIn("Снять блокер", asked_detail["text"])

        # --- экспорт и календарь по шагу со сроком
        status, headers, _data, raw = self.api.get("/api/export/request/%s.md" % rid)
        self.assertEqual(status, 200)
        self.assertIn("markdown", headers.get("Content-Type", ""))
        self.assertIn("Канал D19", raw.decode("utf-8"))
        status, headers, _data, raw = self.api.get("/api/export/request/%s.html" % rid)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"<!doctype html>", raw.lower())
        status, headers, _data, raw = self.api.get(
            "/api/ics?room=cafe&items=" + quote(step_ids[0], safe=""))
        self.assertEqual(status, 200)
        self.assertIn("calendar", headers.get("Content-Type", ""))
        self.assertIn("BEGIN:VCALENDAR", raw.decode("utf-8"))
        status, _h, _d, raw = self.api.get("/api/ics?room=cafe&items=" + quote(f"{rid}:nope", safe=""))
        self.assertEqual(status, 404)
        status, _h, _d, raw = self.api.get("/api/ics?room=cafe&all=1")
        self.assertEqual(status, 200)

        # --- поиск по слову из ответа находит сам запрос
        search_mod.clear_cache()
        found = self.ok("GET", "/api/search?q=%s&room=cafe" % quote("курьер"))
        self.assertGreaterEqual(found["total"], 1)
        self.assertIn(rid, [h["id"] for h in found["items"] if h["kind"] == "request"])

    def test_04_fn_room_retargets_to_default(self):
        """Комната fn: директор — профиль «default»; тик переназначает корень на него."""
        made = self.ok("POST", "/api/rooms/fn/requests",
                       {"text": "Проверь договор подряда с курьером.", "title": "Договор подряда"})
        rid = made["root_id"]
        self.assertEqual(made["request"]["status"], "queued")
        kids = self.sim.decompose(rid, [{"title": "Юридический срез", "assignee": "lawyer"}])
        self.assertEqual(len(kids), 1)
        stats = self.tick()
        self.assertEqual(stats["errors"], 0, stats)
        detail = self.detail(rid)
        self.assertEqual(detail["assignee"], "default")
        self.assertEqual(detail["status"], "working")
        self.assertFalse(detail["critic"]["enabled"])       # у комнаты fn критика нет

    def extra_cards(self, rid: str) -> list:
        reg = json.loads((Path(self.cfg.state_dir) / "cafe" / "requests.json").read_text(encoding="utf-8"))
        for rec in reg.get("requests") or []:
            if rec.get("root_id") == rid:
                return list(rec.get("extra_cards") or [])
        return []

    def test_02_adopt_candidate(self):
        """Привязка чужой карточки: только реестр, доска и tenant не меняются."""
        cand = self.sb.state("candidate")["root_id"]
        before = self.sb.one("SELECT tenant, status, assignee FROM tasks WHERE id=?", (cand,))
        count_before = self.card_count()
        adopted = self.ok("POST", "/api/adopt", {"room": "cafe", "task_id": cand})
        self.assertTrue(adopted["ok"])
        self.assertTrue(adopted["adopted"])
        self.assertFalse(adopted["already"])
        self.assertEqual(self.card_count(), count_before)          # в доску не писали
        after = self.sb.one("SELECT tenant, status, assignee FROM tasks WHERE id=?", (cand,))
        self.assertEqual(tuple(before), tuple(after))
        detail = self.detail(cand)
        self.assertEqual(detail["id"], cand)
        self.assertFalse(detail["critic"]["enabled"])              # adopted → критик выключен
        # повторный adopt идемпотентен: второй записи нет
        again = self.ok("POST", "/api/adopt", {"room": "cafe", "task_id": cand})
        self.assertTrue(again["already"])
        reg = json.loads((Path(self.cfg.state_dir) / "cafe" / "requests.json").read_text(encoding="utf-8"))
        self.assertEqual(len([r for r in reg["requests"] if r["root_id"] == cand]), 1)
        # adopted-запрос не трогает доску на тике (ни retarget, ни критик)
        stats = self.tick()
        self.assertEqual(stats["steps"], 0, stats)
        self.assertEqual(self.card_count(), count_before)

    def test_03_smoke_get_and_fixture_keys(self):
        """Смоук всех GET-эндпоинтов + сравнение набора ключей с fixtures/."""
        items = self.ok("GET", "/api/rooms/cafe/requests")["items"]
        if not items:                                  # запуск теста в одиночку
            made = self.ok("POST", "/api/rooms/cafe/requests",
                           {"text": "Смоук: проверка форм ответов", "title": "Смоук форм"})
            items = [made["request"]]
        rid = items[0]["id"]
        cases = [
            ("/api/health", "health.json", 200),
            ("/api/config", "config.json", 200),
            ("/api/profiles", "profiles.json", 200),
            ("/api/rooms", "rooms.json", 200),
            ("/api/rooms/cafe", "room_cafe.json", 200),
            ("/api/rooms/fn", "room_fn.json", 200),
            ("/api/rooms/cafe/requests", "requests_cafe.json", 200),
            ("/api/rooms/fn/requests", "requests_fn.json", 200),
            ("/api/rooms/cafe/candidates", "candidates_cafe.json", 200),
            ("/api/rooms/cafe/team", "team_cafe.json", 200),
            ("/api/rooms/fn/team", "team_fn.json", 200),
            ("/api/rooms/cafe/roadmap", "roadmap_cafe.json", 200),
            ("/api/rooms/cafe/backlog", "backlog_cafe.json", 200),
            ("/api/rooms/cafe/reports", "reports_cafe.json", 200),
            ("/api/rooms/cafe/journal", "journal_cafe.json", 200),
            ("/api/search?q=канал&room=cafe", "search.json", 200),
            ("/api/requests/" + rid, "request_done.json", 200),
            ("/api/requests/nope_123", None, 404),
            ("/api/rooms/nope", None, 404),
            ("/api/rooms/cafe/requests?limit=999", None, 400),
            ("/api/search?q=a", None, 400),
        ]
        diffs: list = []
        for path, fixture, expected in cases:
            url = quote(path, safe="/?=&:") if not path.isascii() else path
            status, _headers, data, raw = self.api.get(url)
            self.assertEqual(status, expected, "%s → %s: %s" % (path, status, raw[:300]))
            if fixture and isinstance(data, dict):
                want = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
                _diff_keys(fixture, data, want, diffs)
            if fixture and "search" in fixture:
                self.assertIn("total", data)
                self.assertGreaterEqual(data["total"], len(data["items"]))
        # HEAD отдаёт заголовки без тела, статика тоже
        status, headers, _d, raw = self.api.get("/api/health")
        self.assertEqual(status, 200)
        status, headers, data, raw = self.api.request("HEAD", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertTrue(int(headers.get("Content-Length") or 0) > 0)
        status, headers, data, raw = self.api.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        # «мягкое» закрытие соединения: клиент уходит, сервер не падает
        for _ in range(3):
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/api/health")
            conn.sock.close()
            conn.close()
        self.assertEqual(self.api.get("/api/health")[0], 200)
        if diffs:
            print("\n[backend ↔ contract] расходы ключей с fixtures/:")
            for d in diffs:
                print("  %-42s missing=%s extra=%s" % (d["path"], d["missing"], d["extra"]))


class RealBoardAdoptTest(unittest.TestCase):
    """Реальная доска — ТОЛЬКО ЧТЕНИЕ: adopt legacy-карточки и ingest в временное здание.

    Проверяем на живом ``t_605613e0`` (настоящие данные): adopt работает без записи
    в доску, tenant не меняется, roadmap/backlog заполняются из legacy-metadata,
    watcher не создаёт карточек критика (adopted → critic=false), а слепок
    реальной kanban.db до/после совпадает.
    """

    RID = "t_605613e0"
    ENV_KEYS = ("TEAMROOM_HERMES_HOME", "HERMES_KANBAN_HOME", "TEAMROOM_DATA",
                "TEAMROOM_VAULT_DIR")

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile
        cls.before = snapshot_real_db()
        if not cls.before.get("exists"):
            raise unittest.SkipTest("реальная kanban.db не найдена")
        cls.tmp = Path(tempfile.mkdtemp(prefix="tr-real-", dir=str(_outside_tmp())))
        cls.saved = {key: os.environ.get(key) for key in cls.ENV_KEYS}
        for key in ("TEAMROOM_HERMES_HOME", "HERMES_KANBAN_HOME"):
            os.environ.pop(key, None)                  # читаем РЕАЛЬНЫЙ kanban_home
        os.environ["TEAMROOM_DATA"] = str(cls.tmp / "data")
        os.environ["TEAMROOM_VAULT_DIR"] = str(cls.tmp / "vault")
        import server.config as config_mod
        cls.cfg = config_mod.load()
        cls.cfg.ensure_dirs()
        cls.server = app_mod.create_app(cls.cfg, start_watcher=False, port=0)
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.api = Api(cls.port)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server.shutdown()
            cls.server.server_close()
        finally:
            cls.thread.join(timeout=5)
            for key, value in cls.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            import shutil
            shutil.rmtree(cls.tmp, ignore_errors=True)
            after = assert_real_untouched(cls.before)
            if after["ours"] != cls.before["ours"]:
                raise AssertionError("наш код оставил карточки в живой доске")

    def test_real_legacy_adopt_and_ingest(self):
        self.assertFalse(self.cfg.sandbox)
        self.assertTrue(str(self.cfg.kanban_db("team")).lower().endswith("kanban.db"))
        tasks_before = int(self.before["tasks_count"])

        status, _h, adopted, raw = self.api.post("/api/adopt", {"room": "cafe", "task_id": self.RID})
        self.assertEqual(status, 200, raw[:300])
        self.assertTrue(adopted["adopted"])
        # tenant не тронут: карточка по-прежнему без tenant
        con = self.sb_connect()
        try:
            row = con.execute("SELECT tenant, status, assignee FROM tasks WHERE id=?",
                              (self.RID,)).fetchone()
        finally:
            con.close()
        self.assertIsNone(row["tenant"])
        self.assertEqual(row["status"], "done")

        detail = self.ok("GET", "/api/requests/" + self.RID)
        self.assertIn(detail["status"], ("done", "needs_you"))
        self.assertEqual(detail["synthesis"]["state"], "ready")
        self.assertEqual(detail["synthesis"]["version"], 1)
        self.assertTrue(detail["synthesis"]["legacy"])
        self.assertFalse(detail["critic"]["enabled"])          # adopted → критик выключен
        self.assertTrue(detail["next_steps"])
        self.assertTrue(detail["open_questions"])
        # W17-R §1 (B-8): порядок и названия этапов — данные комнаты, статусы — из
        # metadata ответа. Точный `current` здесь задаёт сам ответ руководителя
        # (его порядок областей), а карта комнаты берёт первый незакрытый этап в
        # порядке комнаты, поэтому проверяем смысл: этап существует и ещё не закрыт.
        self.assertEqual([p["id"] for p in detail["stage"]["phases"]],
                         ["idea", "legal", "brand", "packaging", "production", "sales"])
        current = next(p for p in detail["stage"]["phases"]
                       if p["id"] == detail["stage"]["current"])
        self.assertIn(current["status"], ("active", "blocked"))

        # watcher: adopted-запрос не создаёт карточек, но доносит версию до здания проекта
        stats = self.server.http_app.watcher.tick_once()
        self.assertEqual(stats["errors"], 0, stats)
        self.assertEqual(stats["steps"], 0, stats)
        # adopted-запрос уже принят при открытии страницы (ensure_ingested)
        self.assertIn(stats["ingested"], (0, 1), stats)

        roadmap = self.ok("GET", "/api/rooms/cafe/roadmap")
        self.assertTrue(any(p["status"] != "next" for p in roadmap["phases"]))
        self.assertEqual(roadmap["current"]["id"], "legal")
        backlog = self.ok("GET", "/api/rooms/cafe/backlog")
        mine = [i for i in backlog["items"] if i["source_request"] == self.RID]
        self.assertGreaterEqual(len(mine), 5)
        self.assertTrue(any(i["kind"] == "next_step" for i in mine))
        self.assertTrue(any(i["kind"] == "open_question" for i in mine))
        mirror = Path(self.cfg.vault_dir) / "cafe" / "STATE.md"
        self.assertTrue(mirror.is_file(), list(Path(self.cfg.vault_dir).rglob("*.md")))
        self.assertRegex(mirror.read_text(encoding="utf-8"), r"[А-Яа-яЁё]{4,}")

        # доска не выросла и слепок не изменился
        con = self.sb_connect()
        try:
            tasks_after = int(con.execute("SELECT count(*) FROM tasks").fetchone()[0])
            tenants = [r[0] for r in con.execute("SELECT tenant FROM tasks WHERE id=?",
                                                (self.RID,))]
        finally:
            con.close()
        self.assertEqual(tasks_after, tasks_before)
        self.assertEqual(tenants, [None])

    # --- помощники ---------------------------------------------------------
    def ok(self, method: str, path: str, body=None):
        status, _headers, data, _raw = self.api.request(method, path, body)
        self.assertLess(status, 300, "%s %s → %s: %s" % (method, path, status, data))
        return data

    def sb_connect(self):
        import sqlite3
        from tests.helpers import real_db_path
        db = real_db_path(self.cfg)
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con


def _outside_tmp() -> Path:
    """Временный каталог вне реального Hermes (TMPDIR может смотреть внутрь него)."""
    for raw in (os.environ.get("TEAMROOM_TEST_TMP"), os.environ.get("LOCALAPPDATA"),
                tempfile.gettempdir()):
        if not raw:
            continue
        base = Path(raw)
        if raw == os.environ.get("LOCALAPPDATA"):
            base = base / "Temp"
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = tempfile.mkdtemp(prefix=".probe-", dir=str(base))
            os.rmdir(probe)
            return base
        except OSError:
            continue
    return Path(tempfile.gettempdir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
