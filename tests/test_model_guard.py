# -*- coding: utf-8 -*-
"""W23-MG1 MODEL-GUARD-BE: контроль моделей (docs/WAVE23-RULES.md §2).

Что проверяется (все — на песочнице, поддельные `kanban.db` и `state.db`):

1. корневая карточка запроса создаётся БЕЗ `model_override` (исполнитель ещё неизвестен) —
   именно модель руководителя в корне и сожгла $2,45 у локального `team-local`;
2. сторож приводит переопределение к ожидаемому ДО старта (запущенная карточка не портится);
3. запущенная на чужой модели карточка останавливается, перезапускается, пишет стоимость;
4. не более 2 автоперезапусков: дальше карточка парковывается с текстом «нужно решение»;
5. профиль без модели — НЕ убиваем (только предупреждение «неизвестно»);
6. чужой `tenant` не трогаем никогда;
7. нормализация имён: регистр и `провайдер/модель` — то же, а `qwen36` ≠ `qwen36v`;
8. режим `warn` ничего не останавливает и не правит;
9. тик идемпотентен: десять проходов — одно исправление;
10. регрессия на всех путях создания карточек (запрос, критик/доработка, повтор);
11. `GET /api/rooms/{id}/model-check`, `model_guard[]` в деталях запроса, пункт диагностики;
12. `card_stop` / `card_resume` / `card_model` — остановить без потери работы, продолжить,
    сменить модель и продолжить; возобновление сообщением пользователя.

Правила волны: только `tests.helpers.Sandbox` (свои `HERMES_KANBAN_HOME`, `TEAMROOM_DATA`,
`TEAMROOM_VAULT_DIR`), CLI `hermes kanban` подменён `Shim`, реальные профили Hermes и
`state.db` читаются только на чтение (а модель профиля берётся из поддельных конфигов).

    python -m unittest tests.test_model_guard -v

# SHIM-OK: CLI Hermes подменён Shim; dispatch/decompose не выполняются никогда.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess as real_subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.agent_models as models_mod          # noqa: E402
import server.app as app_mod                      # noqa: E402
import server.engine_cli as engine_cli            # noqa: E402
import server.engine_write as ew                  # noqa: E402
import server.model_guard as guard                # noqa: E402
from server.http_util import ApiError             # noqa: E402
from tests.helpers import Sandbox, SimAgents      # noqa: E402

ROOM = "cafe"
TENANT = "room:cafe"
LEAD = "team-director"          # руководитель демо-комнаты
WORKER = "team-ops"             # исполнитель с локальной моделью
ORPHAN = "team-runner"          # профиль без модели в конфиге
CHEAP = ("llamacpp", "qwen36v")
EXPENSIVE = ("anthropic", "claude-sonnet-5-5")

#: поддельные конфиги профилей: ровно то, что сторож должен считать «моделью агента»
CONFIGS = {
    LEAD: "model:\n  provider: anthropic\n  default: claude-sonnet-5-5\nagent:\n  reasoning_effort: high\n",
    WORKER: ("model:\n  provider: llamacpp\n  default: qwen36v\n"
             "  base_url: http://127.0.0.1:8090/v1\nagent:\n  reasoning_effort: ''\n"),
    "team-cfo": "model:\n  provider: llamacpp\n  default: qwen36v\n",
}
NO_MODEL = ("team-runner", "team-marketing", "team-strategy", "team-lawyer", "team-critic")


class Shim:
    """CLI `hermes kanban` без агентов: глаголы пишут в ПЕСОЧНУЮ доску.

    Реализованы ровно те, что нужны пакету: `create`, `reassign`, `reclaim`, `block`,
    `unblock`, `promote`, `set-model`, `comment`. Ничего не запускает и агентов не зовёт.
    """

    def __init__(self, sa: SimAgents) -> None:
        self.calls: list[list[str]] = []
        self.sa = sa
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def _flag(argv, name: str) -> str:
        for token in argv:
            if token.startswith("--%s=" % name):
                return token.split("=", 1)[1]
        return ""

    def _verb(self, argv) -> tuple:
        try:
            tail = list(argv[argv.index("kanban") + 1:])
        except ValueError:
            tail = list(argv)
        skip = False
        for index, token in enumerate(tail):
            if skip:
                skip = False
                continue
            if token == "--board":
                skip = True
                continue
            if token.startswith("-"):
                continue
            return token, tail[index + 1:]
        return "", tail

    def _task_id(self, rest) -> str:
        return next((tok for tok in rest if tok.startswith("t_")), "")

    def _set(self, task_id: str, **fields) -> None:
        if not task_id or not fields:
            return
        con = self.sa.connect()
        try:
            keys = ", ".join("%s = ?" % name for name in fields)
            con.execute("UPDATE tasks SET %s WHERE id = ?" % keys,
                        (*fields.values(), task_id))
            con.commit()
        finally:
            con.close()

    def run(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb, rest = self._verb(argv)
        if verb == "create":
            title = rest[0] if rest and not rest[0].startswith("-") else "Карточка"
            assignee = self._flag(argv, "assignee")
            status = "triage" if "--triage" in argv else "todo"
            body = ""
            body_file = self._flag(argv, "body-file")
            if body_file:
                try:
                    body = Path(body_file).read_text(encoding="utf-8")
                except OSError:
                    body = ""
            parent = self._flag(argv, "parent")
            card_id = self.sa.card(title=title[:120], assignee=assignee, status=status,
                                   body=body, created_by="Комната команды", parent=parent or None)
            model, provider = self._flag(argv, "model"), self._flag(argv, "provider")
            if model:                                  # движок пишет override из флагов create
                self._set(card_id, model_override=model, provider_override=provider or None)
            return real_subprocess.CompletedProcess(
                argv, 0, json.dumps({"id": card_id, "status": status}), "")
        if verb == "reassign":
            self._set(self._task_id(rest), status="ready", current_run_id=None)
            return real_subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "reclaim":
            self._set(self._task_id(rest), status="ready", claim_lock=None, worker_pid=None)
            return real_subprocess.CompletedProcess(argv, 0, "Reclaimed", "")
        if verb == "block":
            self._set(self._task_id(rest), status="blocked")
            return real_subprocess.CompletedProcess(argv, 0, "Blocked", "")
        if verb == "unblock":
            self._set(self._task_id(rest), status="ready")
            return real_subprocess.CompletedProcess(argv, 0, "Unblocked", "")
        if verb == "promote":
            self._set(self._task_id(rest), status="ready")
            return real_subprocess.CompletedProcess(argv, 0, "Promoted", "")
        if verb == "set-model":
            task_id = self._task_id(rest)
            model = next((tok for tok in rest if not tok.startswith("t_")
                          and not tok.startswith("-")), "")
            model = "" if model.lower() in ("none", "null", "-") else model
            provider = self._flag(argv, "provider")
            self._set(task_id, model_override=model or None,
                      provider_override=(provider or None) if model else None)
            return real_subprocess.CompletedProcess(argv, 0, "Set", "")
        if verb == "comment":
            text = ""
            if "--" in rest:
                text = " ".join(rest[rest.index("--") + 1:])
            self.sa.comment(self._task_id(rest), self._flag(argv, "author") or "mikhail",
                            text or "комментарий")
            return real_subprocess.CompletedProcess(argv, 0, "", "")
        return real_subprocess.CompletedProcess(argv, 0, "", "")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox().__enter__()
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)
        cls.shim = Shim(cls.sa)
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.shim
        ew.subprocess = cls.shim
        cls.app = app_mod.App(cls.cfg, port=0)
        cls.app.call("rooms", "seed_defaults", ((cls.cfg,), {}))
        # поддельные конфиги профилей: реальные `config.yaml` в тестах не читаем
        cls.configs = Path(tempfile.mkdtemp(prefix="mg-configs-", dir=str(cls.sb.root)))
        for name, text in CONFIGS.items():
            path = cls.configs / ("%s.yaml" % name)
            path.write_text(text, encoding="utf-8")
        cls._real_config_path = models_mod.config_path
        cls._real_root = models_mod._root
        models_mod.config_path = cls._config_path
        models_mod._root = lambda cfg: cls.configs      # в этой папке нет config.yaml
        cls._real_state_path = models_mod.state_path
        cls._session_dbs: dict = {}
        cls._state_seq = 0
        models_mod.state_path = cls._state_path

    @classmethod
    def tearDownClass(cls):
        models_mod.config_path = cls._real_config_path
        models_mod._root = cls._real_root
        models_mod.state_path = cls._real_state_path
        models_mod._SESSION_CACHE.clear()
        engine_cli.subprocess = cls._real_sub
        try:
            del ew.subprocess
        except AttributeError:
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    # --- поддельные конфиги/сессии ---------------------------------------
    @classmethod
    def _config_path(cls, cfg, profile) -> Path:
        name = str(profile or "default")
        if name not in CONFIGS:
            return cls.configs / ("%s.yaml" % name)      # файла нет → модель не задана
        return cls.configs / ("%s.yaml" % name)

    @classmethod
    def _state_path(cls, cfg, profile) -> Path:
        name = str(profile or "default")
        path = cls._session_dbs.get(name)
        if path is None:
            cls._state_seq += 1
            path = Path(cls.sb.root) / ("sessions-%s-%d.db"
                                        % (name.replace("/", "_"), cls._state_seq))
            con = sqlite3.connect(str(path))
            try:
                con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, "
                            "model TEXT, billing_provider TEXT, estimated_cost_usd REAL)")
                con.commit()
            finally:
                con.close()
            cls._session_dbs[name] = path
        return path

    def setUp(self):
        self.shim.calls.clear()
        guard._SESSIONS.clear()
        models_mod._SESSION_CACHE.clear()
        # комната — «как у пользователя»: строгий контроль, выборов моделей нет
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_models": {}, "model_guard": "enforce"}), {}))
        # всё, что могло остаться от прошлых тестов, гасим: иначе сторож видит чужие запуски
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='done' WHERE tenant = ? AND status='running'",
                        (TENANT,))
            # прогоны прошлых тестов не должны быть «последним запуском» участника
            con.execute("DELETE FROM task_runs WHERE task_id IN "
                        "(SELECT id FROM tasks WHERE tenant = ?)", (TENANT,))
            con.commit()
        finally:
            con.close()
        for path in dict.fromkeys(self._session_dbs.values()):
            try:
                con = sqlite3.connect(str(path))
                try:
                    con.execute("DELETE FROM sessions")
                    con.commit()
                finally:
                    con.close()
            except sqlite3.Error:
                continue
        for name in ("model_guard.json", "model_pending.json"):
            try:
                (Path(self.cfg.state_dir) / ROOM / name).unlink()
            except OSError:
                pass

    # --- помощники -------------------------------------------------------
    def room(self) -> dict:
        return self.app.call("rooms", "get_room", ((self.cfg, ROOM), {}))

    def select_worker_model(self, provider: str = CHEAP[0], model: str = CHEAP[1]) -> None:
        """Выбор комнаты для исполнителя (W29-I1: пин правится только по выбору комнаты)."""
        status, body, _h = self.app.dispatch(
            "PUT", "/api/rooms/%s/agents/%s/model" % (ROOM, WORKER), {},
            {"provider": provider, "model": model})
        self.assertEqual(status, 200, body)

    def new_root(self, title: str = "Запрос") -> str:
        root = self.sa.card(title=title, assignee="", status="triage", tenant=TENANT)
        self.app.call("registry", "add_request",
                      ((self.cfg, ROOM, {"root_id": root, "slug": "s-" + root,
                                         "title": title, "created_at": int(time.time())}), {}))
        return root

    def card(self, title: str, assignee: str, status: str = "ready", *, parent: str = "",
             tenant: str = TENANT, model: str = "", provider: str = "") -> str:
        card_id = self.sa.card(title=title, assignee=assignee, status=status, tenant=tenant,
                               parent=parent or None)
        if model or provider:
            con = self.sa.connect()
            try:
                con.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                            (model or None, provider or None, card_id))
                con.commit()
            finally:
                con.close()
        return card_id

    def row(self, card_id: str) -> dict:
        found = self.sb.one("SELECT id, status, assignee, model_override, provider_override, "
                            "reasoning_effort FROM tasks WHERE id = ?", (card_id,))
        return dict(found) if found else {}

    def status(self, card_id: str) -> str:
        return str(self.row(card_id).get("status") or "")

    def session(self, profile: str, session_id: str, model: str, provider: str,
                cost: float | None = None) -> None:
        con = sqlite3.connect(str(self._state_path(self.cfg, profile)))
        try:
            con.execute("INSERT OR REPLACE INTO sessions (id, model, billing_provider, "
                        "estimated_cost_usd) VALUES (?,?,?,?)",
                        (session_id, model, provider, cost))
            con.commit()
        finally:
            con.close()

    def running(self, card_id: str, profile: str, session_id: str, model: str,
                provider: str, *, cost: float | None = None, age: int = 300) -> None:
        """Карточка «идёт» с сессией воркера (как пишет движок) и фактом модели."""
        started = int(time.time()) - max(1, age)
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='running', started_at=?, assignee=? WHERE id=?",
                        (started, profile, card_id))
            con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                        "VALUES (?,?,?,?,?)",
                        (card_id, profile, "running", started,
                         json.dumps({"worker_session_id": session_id})))
            run_id = con.execute("SELECT max(id) FROM task_runs WHERE task_id = ?",
                                 (card_id,)).fetchone()[0]
            con.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, card_id))
            con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (card_id, run_id, "owner_admitted",
                         json.dumps({"session_id": session_id}), started + 1))
            con.commit()
        finally:
            con.close()
        self.session(profile, session_id, model, provider, cost)

    def tick(self) -> dict:
        return guard.tick(self.cfg, self.room())

    def events(self, room_id: str = ROOM) -> list:
        return guard.read_state(self.cfg, room_id).get("events") or []

    def kinds(self, card_id: str = "") -> list:
        items = self.events() if not card_id else [e for e in self.events()
                                                   if str(e.get("card_id")) == card_id]
        return [str(e.get("kind")) for e in items]

    def action(self, rid: str, payload: dict):
        try:
            return self.app.dispatch("POST", "/api/requests/%s/actions" % rid, {}, payload)
        except ApiError as exc:
            return exc.status, exc.payload(), {}


# ---------------------------------------------------------------- создание карточек

class CreationTests(Base):
    def test_root_card_has_no_model_override(self):
        """Корневая карточка (triage, исполнителя нет) — БЕЗ переопределения модели."""
        # руководителю в комнате явно выбрана дорогая модель: именно она раньше попадала в корень
        status, _body, _h = self.app.dispatch(
            "PUT", "/api/rooms/%s/agents/%s/model" % (ROOM, LEAD),
            {}, {"provider": EXPENSIVE[0], "model": EXPENSIVE[1], "reasoning": "high"})
        self.assertEqual(status, 200)
        status, body, _h = self.app.dispatch(
            "POST", "/api/rooms/%s/requests" % ROOM, {},
            {"text": "Проверь поставщика ромашки и сроки", "title": "Проверка поставщика"})
        self.assertIn(status, (200, 201), body)
        root = body.get("root_id") or (body.get("request") or {}).get("root_id") or ""
        self.assertTrue(root.startswith("t_"), body)
        row = self.row(root)
        self.assertEqual(row.get("status"), "triage")
        self.assertFalse(row.get("model_override"),
                         "в корень записали модель руководителя — это и есть баг $2,45")
        self.assertFalse(row.get("provider_override"))
        create = [a for a in self.shim.calls if "create" in a][-1]
        self.assertFalse([t for t in create if t.startswith("--model")], create)

    def test_child_card_gets_only_room_selection(self):
        """Карточка-ребёнок: без выбора комнаты — без флагов; с выбором — ровно он."""
        root = self.new_root("Дочерние карточки")
        kw = dict(room=self.room(), board="team", parent_id=root, assignee=WORKER,
                  title="Собери факты", body="Собери факты по поставщику", kind="task",
                  idem_key="mg-child-1")
        card_id = ew.create_child_card(self.cfg, **kw)
        self.assertFalse(self.row(card_id).get("model_override"))
        create = [a for a in self.shim.calls if "create" in a][-1]
        self.assertFalse([t for t in create if t.startswith("--model")], create)
        # теперь пользователь выбрал модель комнаты для этого исполнителя
        status, _b, _h = self.app.dispatch(
            "PUT", "/api/rooms/%s/agents/%s/model" % (ROOM, WORKER), {},
            {"provider": CHEAP[0], "model": CHEAP[1]})
        self.assertEqual(status, 200)
        card2 = ew.create_child_card(self.cfg, **dict(kw, room=self.room(),
                                                      idem_key="mg-child-2"))
        row = self.row(card2)
        self.assertEqual(row.get("model_override"), CHEAP[1])
        self.assertEqual(row.get("provider_override"), CHEAP[0])


# ---------------------------------------------------------------- сторож

class GuardTests(Base):
    def test_fixes_override_before_start(self):
        """Карточка с чужой моделью исправляется ДО запуска — по выбору КОМНАТЫ.

        W29-I1: раньше сторож «приводил карточку к модели профиля» и просто СНИМАЛ
        переопределение. После инцидента 21.11 (снятые пины у восьми боевых карточек)
        пин без выбора комнаты не трогается вообще: единственный законный источник —
        `agent_models` комнаты. Поэтому тест выбирает модель комнате, а проверка та же:
        чужая модель исправлена ДО старта, карточку не остановили, событие одно.
        """
        self.select_worker_model()
        root = self.new_root("До запуска")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root,
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        res = self.tick()
        self.assertIn(card_id, res["fixed"], res)
        row = self.row(card_id)
        self.assertEqual(row.get("model_override"), CHEAP[1], row)
        self.assertEqual(row.get("provider_override"), CHEAP[0], row)
        self.assertEqual(row.get("status"), "ready", "до запуска карточку не останавливаем")
        kinds = self.kinds(card_id)
        self.assertEqual(kinds.count("fixed"), 1, kinds)
        event = [e for e in self.events() if e.get("kind") == "fixed"][-1]
        self.assertIn(EXPENSIVE[1], event["text"])
        self.assertIn(CHEAP[1], event["text"])
        journal = (Path(self.cfg.state_dir) / ROOM / "journal.jsonl").read_text("utf-8")
        self.assertIn("исправлена до запуска", journal)

    def test_idempotent_tick_ten_times(self):
        """Десять проходов подряд — одно исправление (нет петли и перезаписи)."""
        self.select_worker_model()          # W29-I1: пин правится только по выбору комнаты
        root = self.new_root("Идемпотентность")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root,
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        for _ in range(10):
            self.tick()
        fixed = [e for e in self.events()
                 if e.get("kind") == "fixed" and str(e.get("card_id")) == card_id]
        self.assertEqual(len(fixed), 1, [e["text"] for e in fixed])

    def test_stops_and_restarts_wrong_model(self):
        """Запущенная на чужой модели карточка останавливается, перезапускается, с ценой."""
        root = self.new_root("Чужой запуск")
        card_id = self.card("Отчёт по поставщику", WORKER, "triage", parent=root)
        self.running(card_id, WORKER, "sess-wrong-1", EXPENSIVE[1], EXPENSIVE[0], cost=2.45)
        res = self.tick()
        self.assertIn(card_id, res["stopped"], res)
        row = self.row(card_id)
        self.assertEqual(row.get("status"), "ready", "карточка должна вернуться в очередь")
        self.assertFalse(row.get("model_override"), "переопределение должно быть снято")
        self.assertTrue(any("reclaim" in argv for argv in self.shim.calls), self.shim.calls)
        event = [e for e in self.events() if e.get("kind") == "stopped"][-1]
        self.assertEqual(event["cost_usd"], 2.45)
        self.assertEqual(event["actual"]["model"], EXPENSIVE[1])
        self.assertEqual(event["expected"]["model"], CHEAP[1])
        self.assertEqual(event["root_id"], root)
        state = guard.read_state(self.cfg, ROOM)
        self.assertEqual(state["restarts"][card_id]["count"], 1)
        # повторный тик уже не находит расхождения (сессия новая, карточка в очереди)
        self.tick()
        self.assertEqual(self.kinds(card_id).count("stopped"), 1, self.kinds(card_id))

    def test_no_more_than_two_restarts(self):
        """После двух перезапусков карточка парковывается с вопросом человеку."""
        root = self.new_root("Предел перезапусков")
        card_id = self.card("Отчёт", WORKER, "triage", parent=root)
        for attempt in range(1, 4):
            # каждый раз карточку снова берут, и снова не на той модели
            self.running(card_id, WORKER, "sess-wrong-%d" % attempt, EXPENSIVE[1],
                         EXPENSIVE[0], cost=0.5)
            self.tick()
        row = self.row(card_id)
        self.assertEqual(row.get("status"), "blocked", row)
        self.assertEqual(guard.read_state(self.cfg, ROOM)["restarts"][card_id]["count"], 2)
        stopped = [e for e in self.events()
                   if e.get("kind") == "stopped" and str(e.get("card_id")) == card_id]
        self.assertEqual(len(stopped), 3, [e["text"] for e in stopped])
        self.assertIn("нужно решение", stopped[-1]["text"])
        comments = self.sb.rows("SELECT body FROM task_comments WHERE task_id = ?", (root,))
        self.assertTrue(any("[Руководитель · вопрос]" in str(c["body"]) for c in comments),
                        [dict(c) for c in comments])
        # сторож больше не поднимает остановленную им же карточку
        self.tick()
        self.assertEqual(self.row(card_id).get("status"), "blocked")

    def test_unknown_model_is_not_killed(self):
        """Профиль без модели: работу не трогаем, пишем предупреждение один раз."""
        root = self.new_root("Неизвестная модель")
        card_id = self.card("Собери факты", ORPHAN, "triage", parent=root)
        self.running(card_id, ORPHAN, "sess-unknown", "some-model", "some-provider")
        res = self.tick()
        self.assertIn("unknown", self.kinds(card_id), self.kinds(card_id))
        self.assertEqual(self.status(card_id), "running", "неизвестно — не убиваем")
        self.assertFalse(any("reclaim" in argv for argv in self.shim.calls))
        before = self.kinds(card_id).count("unknown")
        self.tick()
        self.assertEqual(self.kinds(card_id).count("unknown"), before,
                         "предупреждение — раз в сутки")

    def test_foreign_tenant_untouched(self):
        """Карточки другого tenant (чужая работа) сторож не трогает никогда."""
        foreign = self.card("Чужая работа", WORKER, "running", tenant="room:other",
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        self.tick()
        row = self.row(foreign)
        self.assertEqual(row.get("model_override"), EXPENSIVE[1])
        self.assertEqual(self.status(foreign), "running")
        self.assertFalse(any("reclaim" in argv for argv in self.shim.calls))

    def test_warn_mode_does_nothing(self):
        """Режим «только предупреждать»: ни остановок, ни перезапусков."""
        self.app.call("rooms", "update_room", ((self.cfg, ROOM, {"model_guard": "warn"}), {}))
        root = self.new_root("Режим предупреждения")
        pending = self.card("Собери факты", WORKER, "ready", parent=root,
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        running = self.card("Отчёт", WORKER, "triage", parent=root)
        self.running(running, WORKER, "sess-warn", EXPENSIVE[1], EXPENSIVE[0], cost=1.0)
        res = self.tick()
        self.assertEqual(res["fixed"], [])
        self.assertEqual(res["stopped"], [])
        self.assertEqual(self.status(running), "running")
        self.assertEqual(self.row(pending).get("model_override"), EXPENSIVE[1])
        self.assertEqual(len([e for e in self.events() if e.get("kind") == "warned"
                              and str(e.get("card_id")) in (pending, running)]), 2,
                         self.kinds())
        self.assertFalse(any("reclaim" in argv for argv in self.shim.calls))

    def test_room_selection_is_expected_model(self):
        """Переопределение комнаты — ожидаемая модель: карточку ведут к ней, а не к профилю."""
        self.app.call("rooms", "update_room", ((self.cfg, ROOM, {"agent_models": {
            WORKER: {"provider": "anthropic", "model": EXPENSIVE[1], "reasoning": "high"}}}), {}))
        root = self.new_root("Выбор комнаты")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root)
        self.tick()
        row = self.row(card_id)
        self.assertEqual(row.get("model_override"), EXPENSIVE[1])
        self.assertEqual(row.get("provider_override"), EXPENSIVE[0])
        self.assertEqual(str(row.get("reasoning_effort") or ""), "high")
        self.assertEqual(self.kinds(card_id).count("fixed"), 1, self.kinds())


# ---------------------------------------------------------------- нормализация

class NormalizationTests(unittest.TestCase):
    def test_same_model_true(self):
        self.assertTrue(guard.same_model({"provider": "llamacpp", "model": "qwen36v"},
                                          {"provider": "llamacpp", "model": "Qwen36V"}))
        self.assertTrue(guard.same_model({"provider": "llamacpp", "model": "qwen36v"},
                                          {"provider": "", "model": "qwen36v"}))
        self.assertTrue(guard.same_model({"provider": "llamacpp", "model": "qwen36v"},
                                          {"provider": "", "model": "llamacpp/qwen36v"}))
        self.assertTrue(guard.same_model({"provider": "llamacpp", "model": "qwen36v:latest"},
                                          {"provider": "llamacpp", "model": "qwen36v"}))

    def test_same_model_false(self):
        self.assertFalse(guard.same_model({"model": "qwen36v"}, {"model": "qwen36"}))
        self.assertFalse(guard.same_model({"model": "glm-5.3"}, {"model": "glm-5.3-flash"}))
        self.assertFalse(guard.same_model({"model": "deepseek-v4-pro"},
                                           {"model": "deepseek-v4-pro:0813"}))
        self.assertFalse(guard.same_model({"provider": "llamacpp", "model": "qwen36v"},
                                           {"provider": "anthropic", "model": "qwen36v"}))
        self.assertFalse(guard.same_model({"model": "qwen36v"}, {"model": ""}))
        self.assertFalse(guard.same_model({"model": ""}, {"model": "qwen36v"}))

    def test_aliases_are_same(self):
        self.assertEqual(guard.canon_model("Claude-Sonnet-5.5"), "claude-sonnet-5-5")


# ---------------------------------------------------------------- API

class ApiTests(Base):
    def test_model_check(self):
        """«Куда собирается уйти запрос»: ожидание, карточка, факт запуска, проблема."""
        root = self.new_root("Проверка моделей")
        card_id = self.card("Отчёт", WORKER, "triage", parent=root,
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        self.running(card_id, WORKER, "sess-check", EXPENSIVE[1], EXPENSIVE[0], cost=2.45)
        status, body, _h = self.app.dispatch("GET", "/api/rooms/%s/model-check" % ROOM, {}, {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["room"], ROOM)
        member = next(m for m in body["members"] if m["profile"] == WORKER)
        self.assertEqual(member["expected"]["model"], CHEAP[1])
        self.assertEqual(member["expected"]["source"], "profile")
        self.assertEqual(member["expected"]["base_url"], "http://127.0.0.1:8090/v1")
        self.assertEqual(member["card_model"]["model"], EXPENSIVE[1])
        self.assertEqual(member["last_run"]["model"], EXPENSIVE[1])
        self.assertEqual(member["last_run"]["cost_usd"], 2.45)
        self.assertFalse(member["ok"])
        self.assertIn("qwen36v", member["problem"])
        self.assertIn("claude-sonnet-5-5", member["problem"])
        self.assertEqual(body["guard"]["mode"], "enforce")
        self.assertIn("text", body["guard"])

    def test_request_details_show_guard(self):
        """Лента `model_guard[]` в деталях запроса: готовый русский текст."""
        self.select_worker_model()          # W29-I1: пин правится только по выбору комнаты
        root = self.new_root("Лента контроля")
        card_id = self.card("Собери факты", WORKER, "ready", parent=root,
                            model=EXPENSIVE[1], provider=EXPENSIVE[0])
        self.tick()
        status, body, _h = self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})
        self.assertEqual(status, 200, body)
        self.assertIn("model_guard", body)
        self.assertTrue(body["model_guard"], "лента пустая — событие не доехало")
        first = body["model_guard"][0]
        self.assertEqual(first["kind"], "fixed")
        self.assertEqual(first["card_id"], card_id)
        self.assertIn("исправлена до запуска", first["text"])

    def test_room_setting(self):
        """Настройка комнаты: строго по умолчанию, переключается, мусор — 400."""
        room = self.room()
        self.assertEqual(room.get("model_guard"), "enforce")
        self.assertIn("Строго", str(room.get("model_guard_label")))
        status, body, _h = self.app.dispatch("PATCH", "/api/rooms/%s" % ROOM, {},
                                             {"model_guard": "warn"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("model_guard"), "warn")
        self.assertIn("предупреждать", str(body.get("model_guard_label")).lower())
        try:
            status, body, _h = self.app.dispatch("PATCH", "/api/rooms/%s" % ROOM, {},
                                                 {"model_guard": "как-нибудь"})
        except ApiError as exc:
            status, body = exc.status, exc.payload()
        self.assertEqual(status, 400, body)

    def test_diagnostics_item(self):
        """Пункт «Контроль моделей» в «Проверке системы»."""
        root = self.new_root("Диагностика")
        card_id = self.card("Отчёт", WORKER, "triage", parent=root)
        self.running(card_id, WORKER, "sess-diag", EXPENSIVE[1], EXPENSIVE[0], cost=0.1)
        status, body, _h = self.app.dispatch("GET", "/api/diagnostics", {"room": [ROOM]}, {})
        self.assertEqual(status, 200, body)
        item = next(c for c in body["checks"] if c["id"] == "model_guard")
        self.assertEqual(item["title"], "Контроль моделей")
        self.assertIn(item["status"], ("ok", "warn", "fail", "off"))
        self.assertIn("контроль", item["value"].lower())


# ---------------------------------------------------------------- действия с карточкой

class CardActionTests(Base):
    def test_card_stop_keeps_progress(self):
        """Остановка: воркер снят (reclaim), карточка на паузе, прогресс не тронут."""
        root = self.new_root("Остановка")
        card_id = self.card("Долгая работа", WORKER, "triage", parent=root)
        self.running(card_id, WORKER, "sess-stop", CHEAP[1], CHEAP[0])
        status, body, _h = self.action(root, {"action": "card_stop", "task_id": card_id,
                                             "note": "сначала уточню задачу"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["task_id"], card_id)
        self.assertEqual(body["status"], "blocked")
        self.assertTrue(body["detail"]["paused"])
        self.assertIn("сохранены", body["message"])
        verbs = [argv[argv.index("kanban") + 1:] for argv in self.shim.calls if "kanban" in argv]
        self.assertTrue(any("reclaim" in v for v in verbs), verbs)
        self.assertTrue(any("block" in v for v in verbs), verbs)
        self.assertEqual(guard.stopped_by(self.cfg, ROOM, card_id), "user")
        self.assertIn("stopped_by_user", self.kinds())
        # сторож не поднимает остановленную человеком карточку
        self.tick()
        self.assertEqual(self.status(card_id), "blocked")

    def test_stopped_card_is_marked_in_tree(self):
        """W23-ST: в дереве у остановленной карточки есть `stopped` (для строки MG2).

        Интерфейсу нужно «Остановлено вами в 10:41 · модель: …» и кнопка «Продолжить»:
        статус `blocked` сам по себе не говорит, что карточку остановил человек (та же
        `blocked` бывает у карточки, ждущей зависимостей), поэтому метка берётся из
        реестра сторожа и приходит прямо в узел дерева.
        """
        root = self.new_root("Метка остановки")
        card_id = self.card("Долгая работа", WORKER, "triage", parent=root)
        self.running(card_id, WORKER, "sess-mark", CHEAP[1], CHEAP[0], cost=0.37)
        status, body, _h = self.action(root, {"action": "card_stop", "task_id": card_id,
                                             "note": "сначала уточню задачу"})
        self.assertEqual(status, 200, body)
        status, detail, _h = self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})
        self.assertEqual(status, 200)
        node = next(n for n in detail["tree"] if n["id"] == card_id)
        stopped = node.get("stopped")
        self.assertIsInstance(stopped, dict, node)
        self.assertEqual(stopped["by"], "user")
        self.assertGreater(stopped["at"], 0)
        self.assertEqual(stopped["model"], CHEAP[1], "модель, на которой шла работа")
        self.assertEqual(stopped["provider"], CHEAP[0])
        self.assertAlmostEqual(float(stopped["cost_usd"]), 0.37, places=4)
        # «Продолжить» — метка снимается вместе с остановкой
        status, body, _h = self.action(root, {"action": "card_resume", "task_id": card_id})
        self.assertEqual(status, 200, body)
        status, detail, _h = self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})
        self.assertEqual(status, 200)
        node = next(n for n in detail["tree"] if n["id"] == card_id)
        self.assertNotIn("stopped", node, node)

    def test_guard_parked_card_is_not_marked_as_stopped_by_user(self):
        """Остановку сторожа (`by: guard`) в дерево НЕ кладём: «Остановлено вами» — ложь."""
        root = self.new_root("Чужая остановка")
        card_id = self.card("Работа", WORKER, "triage", parent=root)
        self.running(card_id, WORKER, "sess-guard", EXPENSIVE[1], EXPENSIVE[0], cost=0.5)
        self.tick()                      # сторож исправляет/останавливает сам
        status, detail, _h = self.app.dispatch("GET", "/api/requests/%s" % root, {}, {})
        self.assertEqual(status, 200)
        node = next(n for n in detail["tree"] if n["id"] == card_id)
        self.assertNotIn("stopped", node, node)

    def test_card_resume_and_message(self):
        """Продолжение кнопкой и сообщением «продолжай» в чат."""
        root = self.new_root("Продолжение")
        card_id = self.card("Долгая работа", WORKER, "triage", parent=root)
        self.running(card_id, WORKER, "sess-resume", CHEAP[1], CHEAP[0])
        self.action(root, {"action": "card_stop", "task_id": card_id})
        status, body, _h = self.action(root, {"action": "card_resume", "task_id": card_id})
        self.assertEqual(status, 200, body)
        self.assertIn(body["status"], ("ready", "todo"), body)
        self.assertEqual(guard.stopped_by(self.cfg, ROOM, card_id), "")
        self.assertIn("resumed", self.kinds())
        # остановили снова и продолжили обычным сообщением в чат
        self.running(card_id, WORKER, "sess-resume-2", CHEAP[1], CHEAP[0])
        self.action(root, {"action": "card_stop", "task_id": card_id})
        status, body, _h = self.app.dispatch(
            "POST", "/api/requests/%s/messages" % root, {},
            {"text": "Продолжай, я уточнил вводные"})
        self.assertEqual(status, 201, body)
        self.assertIn(card_id, body.get("resumed") or [], body)
        self.assertIn(self.status(card_id), ("ready", "todo"), body)
        self.assertEqual(guard.stopped_by(self.cfg, ROOM, card_id), "")

    def test_card_model_change_and_clear(self):
        """«Сменить и продолжить»: set-model + остановка; `none` — модель профиля."""
        root = self.new_root("Смена модели")
        card_id = self.card("Работа", WORKER, "triage", parent=root)
        self.running(card_id, WORKER, "sess-model", EXPENSIVE[1], EXPENSIVE[0], cost=0.2)
        status, body, _h = self.action(root, {"action": "card_model", "task_id": card_id,
                                              "model": CHEAP[1], "provider": CHEAP[0]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["model"], {"provider": CHEAP[0], "model": CHEAP[1],
                                         "source": "user"})
        row = self.row(card_id)
        self.assertEqual(row.get("model_override"), CHEAP[1])
        self.assertEqual(row.get("provider_override"), CHEAP[0])
        self.assertIn("reclaim", " ".join(" ".join(a) for a in self.shim.calls))
        # выбор человека сильнее настроек комнаты: сторож его не переписывает
        self.tick()
        self.assertEqual(self.row(card_id).get("model_override"), CHEAP[1])
        # «none» — вернуть к модели профиля
        status, body, _h = self.action(root, {"action": "card_model", "task_id": card_id,
                                              "model": "none"})
        self.assertEqual(status, 200, body)
        self.assertFalse(self.row(card_id).get("model_override"))
        self.assertEqual(body["model"]["source"], "profile")
        self.assertEqual(guard.user_models(self.cfg, ROOM).get(card_id), None)

    def test_card_action_errors(self):
        """Ошибки по-русски: чужая карточка 404, завершённая 409, модель не из каталога 422."""
        root = self.new_root("Ошибки")
        foreign = self.card("Чужая", WORKER, "ready", tenant="room:other")
        status, body, _h = self.action(root, {"action": "card_stop", "task_id": foreign})
        self.assertEqual(status, 404, body)
        done = self.card("Готово", WORKER, "done", parent=root)
        status, body, _h = self.action(root, {"action": "card_resume", "task_id": done})
        self.assertEqual(status, 409, body)
        card = self.card("Работа", WORKER, "ready", parent=root)
        status, body, _h = self.action(root, {"action": "card_resume", "task_id": card})
        self.assertEqual(status, 200, body)          # ready → просто в очереди
        status, body, _h = self.action(root, {"action": "card_stop", "task_id": "t_zzz"})
        self.assertEqual(status, 400, body)
        status, body, _h = self.action(root, {"action": "card_model", "task_id": card,
                                              "model": "нет-такой-модели",
                                              "provider": "llamacpp"})
        self.assertEqual(status, 422, body)


if __name__ == "__main__":
    unittest.main()
