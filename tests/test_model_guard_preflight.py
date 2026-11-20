# -*- coding: utf-8 -*-
"""W28-C1 «МОДЕЛЬ ДО СТАРТА»: ни одного токена на модели профиля (дефект 5).

Что проверяется (всё — в песочнице `tests.helpers.Sandbox`, поддельные доска и `state.db`;
реальные профили Hermes и `state.db` не пишутся — только чтение):

1. **гонка**: карточка `auto-decomposer` без `model_override` не стартует ни одной секунды
   на модели профиля — 200 итераций с замером (зависимые карточки: окно «родитель ещё
   работает»); наихудший порядок («диспетчер посмотрел раньше сторожа») — остановка ОДИН
   раз, без единого вызова на чужой модели, попытка закрыта `guard_reclaimed`;
2. дельта `task_events` (`kind='created'`) и опережающая проходка по `triage/todo/ready/
   blocked`: переопределение ставится до старта, курсор не разбирает одно и то же дважды;
3. приложение ставит модель сразу при назначении исполнителя (`agent_models.pin_cards`,
   путь `retarget_root` в `critic_loop`);
4. взятая на чужой модели карточка останавливается РОВНО один раз; `ended_at` /
   `end_reason=guard_reclaimed` / `outcome=guard_reclaimed` — в журнале комнаты и в
   состоянии сторожа (в доску и в реальный `state.db` приложение не пишет);
5. непроставленную карточку в `ready` сторож не отдаёт диспетчеру: удерживает штатной
   паузой и снимает её, когда модель встала;
6. `wrong_model_spend` — счётчик «ушло на не ту модель» (вызовы/токены/деньги) по запросу;
7. `room_model_table` — готовая строка для контракта руководителя;
8. чужие tenant и режим «только предупреждать» сторож не трогает.

    python -m unittest tests.test_model_guard_preflight -v

# SHIM-OK: CLI Hermes подменён Shim; агенты и реальный `hermes` не запускаются.
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
from tests.helpers import Sandbox, SimAgents      # noqa: E402

ROOM = "cafe"
TENANT = "room:cafe"
LEAD = "team-director"
WORKER = "team-ops"
CHEAP = ("llamacpp", "qwen36v")                   # модель профиля исполнителя
EXPECTED = ("ollama-cloud", "deepseek-v4.1-flash:1017")   # что выбрано в комнате
ITERATIONS = 200

CONFIGS = {
    LEAD: "model:\n  provider: anthropic\n  default: claude-sonnet-5-5\n",
    WORKER: "model:\n  provider: llamacpp\n  default: qwen36v\n",
}


class Shim:
    """CLI `hermes kanban`: только глаголы, нужные пакету; агентов не запускает."""

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
            con.execute("UPDATE tasks SET %s WHERE id = ?"
                        % ", ".join("%s = ?" % name for name in fields),
                        (*fields.values(), task_id))
            con.commit()
        finally:
            con.close()

    def run(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb, rest = self._verb(argv)
        if verb == "block":
            self._set(self._task_id(rest), status="blocked")
            return real_subprocess.CompletedProcess(argv, 0, "Blocked", "")
        if verb == "unblock":
            self._set(self._task_id(rest), status="ready")
            return real_subprocess.CompletedProcess(argv, 0, "Unblocked", "")
        if verb == "reclaim":
            self._set(self._task_id(rest), status="ready", claim_lock=None, worker_pid=None)
            return real_subprocess.CompletedProcess(argv, 0, "Reclaimed", "")
        if verb == "promote":
            self._set(self._task_id(rest), status="ready")
            return real_subprocess.CompletedProcess(argv, 0, "Promoted", "")
        if verb == "set-model":
            task_id = self._task_id(rest)
            model = next((tok for tok in rest if not tok.startswith("t_") and not tok.startswith("-")), "")
            model = "" if model.lower() in ("none", "null", "-") else model
            provider = self._flag(argv, "provider")
            self._set(task_id, model_override=model or None,
                      provider_override=(provider or None) if model else None)
            return real_subprocess.CompletedProcess(argv, 0, "Set", "")
        if verb == "comment":
            text = " ".join(rest[rest.index("--") + 1:]) if "--" in rest else "комментарий"
            self.sa.comment(self._task_id(rest), self._flag(argv, "author") or "mikhail", text)
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
        cls.configs = Path(tempfile.mkdtemp(prefix="w28c1-configs-", dir=str(cls.sb.root)))
        for name, text in CONFIGS.items():
            (cls.configs / ("%s.yaml" % name)).write_text(text, encoding="utf-8")
        cls._real_config_path = models_mod.config_path
        cls._real_root = models_mod._root
        cls._real_state_path = models_mod.state_path
        models_mod.config_path = cls._config_path
        models_mod._root = lambda cfg: cls.configs
        cls._session_dbs: dict = {}
        models_mod.state_path = cls._state_path

    @classmethod
    def tearDownClass(cls):
        models_mod.config_path = cls._real_config_path
        models_mod._root = cls._real_root
        models_mod.state_path = cls._real_state_path
        models_mod._SESSION_CACHE.clear()
        guard._SESSIONS.clear()
        guard._SPEND_CACHE.clear()
        engine_cli.subprocess = cls._real_sub
        try:
            del ew.subprocess
        except AttributeError:
            pass
        try:
            cls.sb.assert_real_untouched(cls.real_before)
        finally:
            cls.sb.__exit__(None, None, None)

    # --- поддельные конфиги и state.db -----------------------------------
    @classmethod
    def _config_path(cls, cfg, profile) -> Path:
        return cls.configs / ("%s.yaml" % str(profile or "default"))

    @classmethod
    def _state_path(cls, cfg, profile) -> Path:
        name = str(profile or "default")
        path = cls._session_dbs.get(name)
        if path is None:
            path = Path(cls.sb.root) / ("w28c1-sessions-%s.db" % name.replace("/", "_"))
            con = sqlite3.connect(str(path))
            try:
                con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, model TEXT, "
                            "billing_provider TEXT, estimated_cost_usd REAL, api_call_count INTEGER, "
                            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
                            "cache_write_tokens INTEGER, ended_at INTEGER, end_reason TEXT)")
                con.commit()
            finally:
                con.close()
            cls._session_dbs[name] = path
        return path

    def setUp(self):
        self.shim.calls.clear()
        self.app.call("rooms", "update_room",
                      ((self.cfg, ROOM, {"agent_models": {}, "model_guard": "enforce"}), {}))
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='done' WHERE tenant = ? AND status IN "
                        "('running','ready','todo','blocked')", (TENANT,))
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
        guard._SESSIONS.clear()
        guard._SPEND_CACHE.clear()
        models_mod._SESSION_CACHE.clear()

    # --- помощники -------------------------------------------------------
    def room(self) -> dict:
        return self.app.call("rooms", "get_room", ((self.cfg, ROOM), {}))

    def select_room_model(self, model=None, provider=None) -> None:
        model = EXPECTED[1] if model is None else model
        provider = EXPECTED[0] if provider is None else provider
        status, _b, _h = self.app.dispatch(
            "PUT", "/api/rooms/%s/agents/%s/model" % (ROOM, WORKER), {},
            {"provider": provider, "model": model})
        self.assertEqual(status, 200, _b)

    def card(self, assignee: str = WORKER, status: str = "todo", *, parent: str = "",
             tenant: str = TENANT, model: str = "", provider: str = "") -> str:
        cid = self.sa.card(title="Собери факты", assignee=assignee, status=status, tenant=tenant,
                           parent=parent or None)
        if model or provider:
            con = self.sa.connect()
            try:
                con.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                            (model or None, provider or None, cid))
                con.commit()
            finally:
                con.close()
        return cid

    def row(self, card_id: str) -> dict:
        found = self.sb.one("SELECT id, status, assignee, model_override, provider_override, "
                            "claim_lock, reasoning_effort FROM tasks WHERE id = ?", (card_id,))
        return dict(found) if found else {}

    def status(self, card_id: str) -> str:
        return str(self.row(card_id).get("status") or "")

    def session(self, profile: str, session_id: str, model: str, provider: str, *,
                calls: int = 0, tokens_in: int = 0, tokens_out: int = 0, cost=None) -> None:
        con = sqlite3.connect(str(self._state_path(self.cfg, profile)))
        try:
            con.execute("INSERT OR REPLACE INTO sessions (id, model, billing_provider, "
                        "estimated_cost_usd, api_call_count, input_tokens, output_tokens) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (session_id, model, provider, cost, calls, tokens_in, tokens_out))
            con.commit()
        finally:
            con.close()

    def states(self) -> dict:
        return guard.read_state(self.cfg, ROOM)

    def journal(self) -> list:
        path = Path(self.cfg.state_dir) / ROOM / "journal.jsonl"
        try:
            return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
        except OSError:
            return []

    def fast_push(self, want_map=None):
        """Быстрая запись переопределения вместо моста (гонка — про МОМЕНТ записи).

        Сам мост (`tools/kb_bridge.py` → `kanban_db.set_model_override`) проверяется
        отдельным тестом `test_bridge_writes_override`; здесь 200 итераций не должны
        поднимать 200 процессов интерпретатора Hermes.
        """
        calls: list = []

        def push(cfg, board, cards, want):
            calls.append(([c["id"] for c in cards], dict(want)))
            con = self.sa.connect()
            done, failed = [], []
            try:
                for card in cards:
                    model = want.get("model") or None
                    try:
                        con.execute("UPDATE tasks SET model_override=?, provider_override=?, "
                                    "reasoning_effort=? WHERE id=?",
                                    (model, (want.get("provider") or None) if model else None,
                                     want.get("reasoning") or None, card["id"]))
                        done.append(card["id"])
                    except sqlite3.Error as exc:
                        failed.append({"id": card["id"], "reason": str(exc)})
                con.commit()
            finally:
                con.close()
            return done, failed

        return push, calls

    def fake_dispatcher(self, wrong_calls: list):
        """Диспетчер Hermes: берёт карточку из `ready`, движок выбирает модель карточки."""

        def tick(only=None) -> list:
            rows = self.sb.rows("SELECT id, assignee, status, model_override, provider_override "
                                "FROM tasks WHERE tenant = ? AND status = 'ready' "
                                "AND claim_lock IS NULL", (TENANT,))
            taken = []
            for row in rows:
                cid = str(row["id"])
                if only and cid != only:
                    continue
                model = str(row["model_override"] or CHEAP[1])
                provider = str(row["provider_override"] or CHEAP[0])
                sid = "sess-%s" % cid
                started = int(time.time())
                con = self.sa.connect()
                try:
                    con.execute("UPDATE tasks SET status='running', claim_lock='w1', worker_pid=4242, "
                                "started_at=? WHERE id=?", (started, cid))
                    con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                                "VALUES (?,?,?,?,?)",
                                (cid, str(row["assignee"] or ""), "running", started,
                                 json.dumps({"worker_session_id": sid})))
                    run_id = con.execute("SELECT max(id) FROM task_runs WHERE task_id = ?",
                                         (cid,)).fetchone()[0]
                    con.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, cid))
                    con.commit()
                finally:
                    con.close()
                # воркер записал сессию: на какой модели он поднялся
                self.session(str(row["assignee"] or ""), sid, model, provider, calls=0)
                taken.append({"card_id": cid, "model": model, "provider": provider,
                              "expected": str(row["model_override"] or "") == EXPECTED[1]})
                if model != EXPECTED[1]:
                    wrong_calls.append(taken[-1])
            return taken

        return tick

    def worker_step(self, card_id: str) -> None:
        """Воркер сделал первый вызов модели: считаем его в сессии (как Hermes)."""
        got = self.sb.one("SELECT assignee FROM tasks WHERE id = ?", (card_id,))
        con = sqlite3.connect(str(self._state_path(self.cfg, str((got or {})["assignee"] or WORKER))))
        try:
            con.execute("UPDATE sessions SET api_call_count = api_call_count + 1, "
                        "input_tokens = input_tokens + 1000 WHERE id = ?", ("sess-%s" % card_id,))
            con.commit()
        finally:
            con.close()

    def preflight(self) -> dict:
        return guard.preflight(self.cfg, self.room())


# ---------------------------------------------------------------- гонка

class RaceTests(Base):
    """200 итераций: карточка без override не должна уйти на модель профиля."""

    def test_race_200_no_second_on_profile_model(self):
        """Зависимые карточки `auto-decomposer`: модель ставится ДО того, как придёт запуск.

        Модель поведения как у дефекта: карточка создаётся в `todo` (родитель ещё
        работает), диспетчер берёт её только после завершения родителя. Замер: сколько
        карточек стартовало без переопределения и сколько осталось работать на модели
        профиля. Требование — ноль и ноль.
        """
        self.select_room_model()
        wrong: list = []
        dispatcher = self.fake_dispatcher(wrong)
        push, calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        created = pinned_before_ready = 0
        started = 0
        try:
            for index in range(ITERATIONS):
                parent = self.card(assignee=WORKER, status="running")
                card_id = self.card(assignee=WORKER, status="todo", parent=parent)
                created += 1
                # окно «родитель ещё работает»: сторож успевает (в жизни — минуты)
                self.preflight()
                if self.row(card_id).get("model_override") == EXPECTED[1]:
                    pinned_before_ready += 1
                # родитель завершился → карточка готова к запуску
                self.sa.finish(parent, "готово", {"verdict": "ok"})
                con = self.sa.connect()
                try:
                    con.execute("UPDATE tasks SET status='ready' WHERE id=? AND status='todo'",
                                (card_id,))
                    con.commit()
                finally:
                    con.close()
                # сторож проходит ещё раз ПЕРЕД диспетчером (как в жизни: интервал 1 с)
                self.preflight()
                taken = dispatcher()
                started += len(taken)
                for item in taken:               # воркер доделал первый вызов
                    self.worker_step(item["card_id"])
                    self.sa.finish(item["card_id"], "готово", {"verdict": "ok"})
            self.assertEqual(started, ITERATIONS, "диспетчер должен был взять все карточки")
            self.assertEqual(pinned_before_ready, ITERATIONS,
                             "карточка получила модель не до запуска (гонка не закрыта)")
            self.assertEqual(wrong, [], "карточки стартовали на модели профиля")
            self.assertEqual(len(calls), ITERATIONS, "лишние записи модели: %d" % len(calls))
        finally:
            models_mod.push_model = real_push
        self.assertEqual(wrong, [])

    def test_race_worst_order_stop_once_without_calls(self):
        """Наихудший порядок: диспетчер посмотрел РАНЬШЕ сторожа — 200 итераций.

        Карточка создаётся сразу в `ready` без переопределения и тут же взята. Сторож
        обязан остановить её РОВНО один раз, закрыть попытку `guard_reclaimed` и не
        допустить ни одного вызова на чужой модели (воркер делает первый вызов позже
        прохода сторожа — 1 с). Второй раз ту же карточку сторож не убивает.
        """
        self.select_room_model()
        claims: list = []            # здесь «без переопределения» — по условию теста
        dispatcher = self.fake_dispatcher(claims)
        push, _calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        stopped_total = 0
        try:
            for _index in range(ITERATIONS):
                card_id = self.card(assignee=WORKER, status="ready")
                taken = dispatcher(only=card_id)
                self.assertEqual(len(taken), 1, "диспетчер должен был взять карточку")
                res = self.preflight()           # сторож увидел чужой запуск
                self.assertIn(card_id, res["reclaimed"], res)
                stopped_total += 1
                # попытка закрыта в журнале комнаты, а не в доске
                state = self.states()
                attempt = (state.get("attempts") or {}).get("sess-%s" % card_id) or {}
                self.assertEqual(attempt.get("outcome"), "guard_reclaimed", state.get("attempts"))
                self.assertEqual(attempt.get("end_reason"), "guard_reclaimed")
                self.assertTrue(attempt.get("ended_at"))
                self.assertEqual(attempt.get("card_id"), card_id)
                # работа не «пошла»: вызовов на чужой модели нет — карточка снята
                for _ in range(5):               # ещё пять проходов — второй остановки нет
                    self.preflight()
                row = self.row(card_id)
                self.assertNotEqual(row.get("status"), "running", row)
                self.assertTrue(row.get("model_override") == EXPECTED[1], row)
                # воркер к этому моменту не успел сделать вызов (снят до первого запроса)
                con = sqlite3.connect(str(self._state_path(self.cfg, WORKER)))
                try:
                    calls_made = con.execute("SELECT api_call_count FROM sessions WHERE id = ?",
                                             ("sess-%s" % card_id,)).fetchone()[0]
                finally:
                    con.close()
                self.assertEqual(int(calls_made or 0), 0,
                                 "на модели профиля был вызов — токены ушли впустую")
                con = self.sa.connect()
                try:
                    con.execute("UPDATE tasks SET status='done' WHERE id=?", (card_id,))
                    con.commit()
                finally:
                    con.close()
        finally:
            models_mod.push_model = real_push
        # все 200 взятий были «не по правилам» — и все 200 закрыты сторожем
        self.assertEqual(len(claims), ITERATIONS)
        self.assertEqual(stopped_total, ITERATIONS)

    def test_race_threaded_interleaving(self):
        """Настоящая гонка: сторож в СВОЁМ потоке, диспетчер — в главном, 200 итераций.

        Самый неудобный порядок: карточка создаётся сразу в `ready`, диспетчер берёт её в
        том же мгновении (сторож ещё не смотрел). Замер: сколько секунд карточка остаётся
        на модели профиля и сколько вызовов на ней успевает уйти. Ожидание: снята меньше
        чем за шаг сторожа (`PREFLIGHT_TICK`), вызовов — ноль.
        """
        self.select_room_model()
        import threading
        claims: list = []
        dispatcher = self.fake_dispatcher(claims)
        push, _calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        stop = threading.Event()
        latencies: list = []
        calls_on_wrong: list = []

        def loop():
            while not stop.is_set():
                try:
                    self.preflight()
                except Exception:            # noqa: BLE001 - поток не должен умирать
                    pass
                stop.wait(0.01)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        try:
            for _index in range(ITERATIONS):
                card_id = self.card(assignee=WORKER, status="ready")
                taken = dispatcher(only=card_id)
                self.assertEqual(len(taken), 1)
                claimed_at = time.monotonic()
                deadline = claimed_at + 3.0
                while time.monotonic() < deadline and self.status(card_id) == "running":
                    time.sleep(0.005)
                latencies.append(time.monotonic() - claimed_at)
                self.assertNotEqual(self.status(card_id), "running",
                                    "карточка осталась на модели профиля")
                con = sqlite3.connect(str(self._state_path(self.cfg, WORKER)))
                try:
                    calls_made = con.execute("SELECT api_call_count FROM sessions WHERE id = ?",
                                             ("sess-%s" % card_id,)).fetchone()[0]
                finally:
                    con.close()
                calls_on_wrong.append(int(calls_made or 0))
                con = self.sa.connect()
                try:
                    con.execute("UPDATE tasks SET status='done' WHERE id=?", (card_id,))
                    con.commit()
                finally:
                    con.close()
        finally:
            stop.set()
            thread.join(timeout=3.0)
            models_mod.push_model = real_push
        self.assertEqual(len(claims), ITERATIONS)
        self.assertEqual(sum(calls_on_wrong), 0,
                         "на модели профиля были вызовы: %s" % sum(calls_on_wrong))
        self.assertLess(max(latencies), 3.0 * guard.PREFLIGHT_TICK,
                        "карточку снимали слишком долго: %.2f с" % max(latencies))


# ---------------------------------------------------------------- простановка модели

class PreflightTests(Base):
    def test_delta_pins_new_card(self):
        """Дельта `task_events` (kind='created'): новая карточка получает модель сразу."""
        self.select_room_model()
        push, calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            card_id = self.card(assignee=WORKER, status="todo")
            res = self.preflight()
            self.assertIn(card_id, res["pinned"], res)
            self.assertGreaterEqual(res["delta"], 1, res)
            row = self.row(card_id)
            self.assertEqual(row.get("model_override"), EXPECTED[1])
            self.assertEqual(row.get("provider_override"), EXPECTED[0])
            # курсор сдвинулся: второй проход то же событие не разбирает
            state = self.states()
            self.assertGreater(int(state.get("pin_cursor") or 0), 0)
            second = self.preflight()
            self.assertEqual(second["pinned"], [], "повторная запись модели")
            self.assertEqual(len(calls), 1, calls)
        finally:
            models_mod.push_model = real_push

    def test_bridge_writes_override(self):
        """Настоящий мост (`kb_bridge` → `kanban_db.set_model_override`), не подмена."""
        self.select_room_model()
        card_id = self.card(assignee=WORKER, status="todo")
        res = self.preflight()
        if res["errors"]:
            self.skipTest("мост недоступен в этом окружении: %s" % res["errors"][0])
        if card_id not in res["pinned"]:
            self.skipTest("мост недоступен: Hermes CLI не установлен")
        row = self.row(card_id)
        self.assertEqual(row.get("model_override"), EXPECTED[1])
        self.assertEqual(row.get("provider_override"), EXPECTED[0])

    def test_all_pending_statuses_pinned(self):
        """Опережающая проходка: triage, todo, ready, blocked — все получают модель."""
        self.select_room_model()
        push, _calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            ids = {status: self.card(assignee=WORKER, status=status)
                   for status in ("triage", "todo", "ready", "blocked")}
            self.preflight()
            for status, cid in ids.items():
                self.assertEqual(self.row(cid).get("model_override"), EXPECTED[1],
                                 "статус %s не получил модель" % status)
        finally:
            models_mod.push_model = real_push

    def test_hold_when_pin_fails_then_release(self):
        """Не смогли поставить модель — карточку в `ready` не отдаём диспетчеру."""
        self.select_room_model()
        calls = {"n": 0}

        def push(cfg, board, cards, want):
            calls["n"] += 1
            if calls["n"] == 1:
                return [], [{"id": c["id"], "reason": "мост недоступен"} for c in cards]
            con = self.sa.connect()
            try:
                for card in cards:
                    con.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                                (want.get("model"), want.get("provider") or None, card["id"]))
                con.commit()
            finally:
                con.close()
            return [c["id"] for c in cards], []

        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            card_id = self.card(assignee=WORKER, status="ready")
            res = self.preflight()
            self.assertIn(card_id, res["held"], res)
            self.assertEqual(self.status(card_id), "blocked",
                             "карточка осталась в ready без модели — диспетчер её возьмёт")
            self.assertIn(card_id, list(self.states().get("holds") or {}))
            # следующий проход: модель встала — пауза снята, карточка снова в очереди
            res2 = self.preflight()
            self.assertIn(card_id, res2["released"], res2)
            self.assertEqual(self.status(card_id), "ready")
            self.assertEqual(self.row(card_id).get("model_override"), EXPECTED[1])
            self.assertNotIn(card_id, list(self.states().get("holds") or {}))
        finally:
            models_mod.push_model = real_push

    def test_claimed_wrong_stopped_once_and_attempt_closed(self):
        """Взяли на чужой модели: остановить один раз, попытку закрыть `guard_reclaimed`."""
        self.select_room_model()
        push, _calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            card_id = self.card(assignee=WORKER, status="running")
            run = self.sb.one("SELECT max(id) AS id FROM task_runs WHERE task_id = ?", (card_id,))
            sid = "sess-wrong"
            con = self.sa.connect()
            try:
                con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                            "VALUES (?,?,?,?,?)",
                            (card_id, WORKER, "running", int(time.time()) - 5,
                             json.dumps({"worker_session_id": sid})))
                con.execute("UPDATE tasks SET current_run_id=(SELECT max(id) FROM task_runs "
                            "WHERE task_id=?) WHERE id=?", (card_id, card_id))
                con.commit()
            finally:
                con.close()
            self.session(WORKER, sid, CHEAP[1], CHEAP[0], calls=1, tokens_in=26487, cost=0.17)
            res = self.preflight()
            self.assertIn(card_id, res["reclaimed"], res)
            attempt = (self.states().get("attempts") or {}).get(sid) or {}
            self.assertEqual(attempt.get("outcome"), "guard_reclaimed", attempt)
            self.assertEqual(attempt.get("end_reason"), "guard_reclaimed")
            self.assertEqual(attempt.get("model"), CHEAP[1])
            self.assertTrue(attempt.get("ended_at"))
            journal = self.journal()
            self.assertTrue(any(str(e.get("kind")) == "guard_reclaimed" for e in journal),
                            [e.get("kind") for e in journal])
            self.assertIn("ended_at", json.dumps(journal, ensure_ascii=False))
            # повторные проходы не убивают её снова
            for _ in range(4):
                self.preflight()
            self.assertEqual((self.states().get("reclaimed") or {}).get(card_id, {}).get("count"), 1)
            reclaimed = [e for e in guard.events(self.cfg, ROOM) if e.get("kind") == "stopped"
                         and e.get("card_id") == card_id]
            self.assertEqual(len(reclaimed), 1, reclaimed)
        finally:
            models_mod.push_model = real_push

    def test_guard_tick_does_not_kill_again_after_reclaim(self):
        """Общий сторож (3 с) после остановки быстрым циклом — только пауза, без второго убийства."""
        self.select_room_model()
        push, _calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            card_id = self.card(assignee=WORKER, status="running")
            sid = "sess-wrong-2"
            con = self.sa.connect()
            try:
                con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                            "VALUES (?,?,?,?,?)",
                            (card_id, WORKER, "running", int(time.time()) - 60,
                             json.dumps({"worker_session_id": sid})))
                con.execute("UPDATE tasks SET current_run_id=(SELECT max(id) FROM task_runs "
                            "WHERE task_id=?) WHERE id=?", (card_id, card_id))
                con.commit()
            finally:
                con.close()
            self.session(WORKER, sid, CHEAP[1], CHEAP[0], calls=1)
            self.preflight()
            # карточку снова взяли на чужой модели (движок проигнорировал переопределение)
            con = self.sa.connect()
            try:
                con.execute("UPDATE tasks SET status='running', started_at=?, model_override=NULL, "
                            "provider_override=NULL WHERE id=?", (int(time.time()) - 60, card_id))
                con.commit()
            finally:
                con.close()
            self.shim.calls.clear()
            guard.tick(self.cfg, self.room())
            # Второго «остановить и перезапустить» быть не должно: только парковка
            # (`reclaim` + `block` с причиной «нужно решение»), без смены модели.
            kills = [argv for argv in self.shim.calls if "reclaim" in argv]
            self.assertEqual(len(kills), 1, "лишние остановки: %s" % kills)
            self.assertTrue(any("нужно решение" in " ".join(argv) for argv in kills), kills)
            self.assertEqual([argv for argv in self.shim.calls if "set-model" in argv], [],
                             "после одной остановки модель больше не переписываем")
            self.assertEqual(self.status(card_id), "blocked", "карточка должна быть припаркована")
        finally:
            models_mod.push_model = real_push

    def test_provider_alias_not_killed(self):
        """Та же модель, другой ярлык провайдера — не повод убивать запуск."""
        self.select_room_model()
        push, _calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            card_id = self.card(assignee=WORKER, status="running")
            sid = "sess-alias"
            con = self.sa.connect()
            try:
                con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                            "VALUES (?,?,?,?,?)",
                            (card_id, WORKER, "running", int(time.time()) - 30,
                             json.dumps({"worker_session_id": sid})))
                con.execute("UPDATE tasks SET current_run_id=(SELECT max(id) FROM task_runs "
                            "WHERE task_id=?) WHERE id=?", (card_id, card_id))
                con.commit()
            finally:
                con.close()
            self.session(WORKER, sid, EXPECTED[1], "local", calls=1)
            res = self.preflight()
            self.assertEqual(res["reclaimed"], [], res)
            self.assertEqual(self.status(card_id), "running")
        finally:
            models_mod.push_model = real_push

    def test_foreign_tenant_untouched(self):
        """Чужая комната (другой tenant) не трогается никогда."""
        self.select_room_model()
        push, _calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            foreign = self.card(assignee=WORKER, status="todo", tenant="room:other")
            self.preflight()
            row = self.row(foreign)
            self.assertFalse(row.get("model_override"))
            self.assertEqual(row.get("status"), "todo")
        finally:
            models_mod.push_model = real_push

    def test_warn_mode_does_nothing(self):
        """Режим «только предупреждать»: ни модели, ни паузы, ни остановок."""
        self.select_room_model()
        self.app.call("rooms", "update_room", ((self.cfg, ROOM, {"model_guard": "warn"}), {}))
        push, calls = self.fast_push()
        real_push = models_mod.push_model
        models_mod.push_model = push
        try:
            card_id = self.card(assignee=WORKER, status="ready")
            self.preflight()
            self.assertEqual(calls, [])
            self.assertEqual(self.status(card_id), "ready")
            self.assertFalse(self.row(card_id).get("model_override"))
        finally:
            models_mod.push_model = real_push


# ---------------------------------------------------------------- контракт и счётчик

class ContractTests(Base):
    def test_room_model_table(self):
        """Готовая строка для контракта руководителя: только выбор комнаты."""
        self.assertEqual(guard.room_model_table(self.room()), "")
        self.select_room_model()
        table = guard.room_model_table(self.room())
        self.assertIn(WORKER, table)
        self.assertIn(EXPECTED[1], table)
        self.assertIn("--model", table)
        self.assertIn("--provider", table)
        self.assertNotIn(CHEAP[1], table, "в контракт попала модель профиля")
        self.assertLess(len(table), 400, "строка слишком длинная для контракта: %d" % len(table))

    def test_wrong_model_spend(self):
        """Счётчик «ушло на не ту модель»: вызовы, токены и деньги из `state.db` (ro)."""
        self.select_room_model()
        root = self.sa.card(title="Запрос", assignee="", status="triage", tenant=TENANT)
        good = self.card(assignee=WORKER, status="done", parent=root)
        bad = self.card(assignee=WORKER, status="done", parent=root)
        con = self.sa.connect()
        try:
            for cid, sid in ((good, "sess-ok"), (bad, "sess-bad")):
                con.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                            "VALUES (?,?,?,?,?)",
                            (cid, WORKER, "done", int(time.time()), json.dumps({"worker_session_id": sid})))
            con.commit()
        finally:
            con.close()
        self.session(WORKER, "sess-ok", EXPECTED[1], EXPECTED[0], calls=16, tokens_in=312000,
                     tokens_out=4000, cost=0.0)
        self.session(WORKER, "sess-bad", CHEAP[1], CHEAP[0], calls=1, tokens_in=26487,
                     tokens_out=309, cost=0.17)
        spend = guard.wrong_model_spend(self.cfg, self.room(), root_id=root)
        self.assertEqual(spend["calls"], 1, spend)
        self.assertEqual(spend["input_tokens"], 26487)
        self.assertEqual(spend["output_tokens"], 309)
        self.assertAlmostEqual(spend["cost_usd"], 0.17, places=2)
        self.assertEqual([c["card_id"] for c in spend["cards"]], [bad])
        self.assertIn("ошибочных запусках", spend["text"])
        self.assertEqual(guard.wrong_model_spend(self.cfg, self.room())["calls"], 1)

    def test_wrong_spend_zero_text(self):
        """Нет ошибок — честная строка «не было», а не «0 токенов»."""
        self.select_room_model()
        spend = guard.wrong_model_spend(self.cfg, self.room())
        self.assertEqual(spend["calls"], 0)
        self.assertEqual(spend["text"], "Ошибочных запусков не было")


if __name__ == "__main__":
    unittest.main()
