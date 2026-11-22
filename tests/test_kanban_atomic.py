# -*- coding: utf-8 -*-
"""W23-AT1 — атомарная «Остановить»: воркер убит И карточка удержана, без окна гонки.

Проблема (реальный опыт оркестратора 17.11): `hermes kanban reclaim <id>` убивает воркер, но
возвращает карточку в `ready` — диспетчер подхватывает её через 30 с (а если слот освободился,
то и раньше); `block` на работающей карточке удерживает её, но воркер не убивает. Два вызова
CLI по 1–2 с оставляют окно, в котором человек видит «остановлено», а работа идёт.

Проверяется здесь (поддельная доска ВО ВРЕМЕННОЙ папке, собственный интерпретатор Hermes,
настоящий процесс-«воркер» — обычный `sleep`, который правда убивается):

1. `stop` завершает воркер и оставляет карточку в `blocked` (одна операция, один процесс);
2. `stop` для карточки в очереди удерживает её, не убивая ничего;
3. «остановил → продолжил → остановил» трижды не уводит карточку в `triage`
   (счётчик петель Hermes `BLOCK_RECURRENCE_LIMIT` не должен срабатывать на человека);
4. чужой claim (другой хост) не сигналится: честный ответ `ok: false`, `worker_alive: true`;
5. `resume` возвращает в очередь (и ставит модель в том же процессе);
6. `change_model_and_resume` снимает идущую работу и ставит новую модель одним процессом;
7. `restart` (сторож) снимает работу и возвращает карточку в очередь БЕЗ паузы и без override;
8. отказы: нет карточки — `not_found`, завершённая — `conflict`;
9. запасной путь (`engine_write`): последовательность CLI `reclaim → block` с проверкой и
   повтором, «продолжить» и «сменить модель» — тоже (без интерпретатора Hermes/модуля);
10. сторож моделей после `card_stop` два тика подряд карточку не поднимает и движок не зовёт.

Без интерпретатора Hermes (чистая машина, CI) тесты 1–8 честно пропускаются: там нечем
вызвать штатные функции канбана. Тесты 9–10 идут всегда (CLI и сторож подменены).

    python -m unittest tests.test_kanban_atomic -v
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.engine_cli as engine_cli             # noqa: E402
import server.engine_write as ew                   # noqa: E402
import server.kanban_atomic as atomic              # noqa: E402
import server.model_guard as guard                 # noqa: E402
from server import config as config_mod            # noqa: E402
from tests.helpers import Sandbox, SimAgents, _outside_hermes_base  # noqa: E402

ROOM = "cafe"
TENANT = "room:cafe"
WORKER_PROFILE = "ornik"          # вымышленный локальный исполнитель
CARD = "t_0123abcd"
#: Какую карточку мы сами поднимаем в «боевой» доске теста.
CHEAP = ("llamacpp", "qwen36v")
EXPENSIVE = ("anthropic", "claude-sonnet-5-5")

#: Колонки, которые тест имеет право выставлять карточке (никаких чужих имён в SQL).
ALLOWED_FIELDS = {"status", "worker_pid", "claim_lock", "worker_started_at", "started_at",
                  "block_kind", "model_override", "provider_override"}

#: Программа на интерпретаторе Hermes: схема + карточка + состояние (как в бою, но на песке).
PREP = r'''
import json, os, sys
data = json.loads(sys.argv[1])
for name in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
             "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACES_ROOT",
             "HERMES_KANBAN_ATTACHMENTS_ROOT"):
    os.environ.pop(name, None)
os.environ["HERMES_KANBAN_HOME"] = data["home"]
sys.path.insert(0, data["tree"])
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
kb.init_db()
conn = kbc.connect()
task_id = data.get("task_id") or ""
if not task_id:
    task_id = kb.create_task(conn, title=data.get("title") or "Карточка песочницы",
                             body="Тело карточки (вымышленные данные)",
                             assignee=data.get("assignee") or "ornik",
                             tenant="room:sbx", created_by="Песочница")
fields = data.get("fields") or {}
if fields:
    keys = ", ".join("%s = ?" % name for name in fields)
    conn.execute("UPDATE tasks SET %s WHERE id = ?" % keys, (*fields.values(), task_id))
    conn.commit()
row = conn.execute(
    "SELECT status, worker_pid, claim_lock, block_kind, block_recurrences,"
    " model_override, provider_override FROM tasks WHERE id = ?", (task_id,)).fetchone()
conn.close()
print(json.dumps({"db": str(kb.kanban_db_path()), "task_id": task_id,
                  "status": row["status"], "worker_pid": row["worker_pid"],
                  "claim_lock": row["claim_lock"], "block_kind": row["block_kind"],
                  "block_recurrences": row["block_recurrences"],
                  "model": row["model_override"], "provider": row["provider_override"]},
                 ensure_ascii=False))
'''


RACER = r'''
import json, os, sys, time
from pathlib import Path
data = json.loads(sys.argv[1])
for name in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
             "HERMES_KANBAN_TASK"):
    os.environ.pop(name, None)
os.environ["HERMES_KANBAN_HOME"] = data["home"]
sys.path.insert(0, data["tree"])
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
conn = kbc.connect(db_path=Path(data["db"]))
deadline = time.time() + float(data.get("seconds") or 10)
while time.time() < deadline:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (data["task"],)).fetchone()
    if row is None or row["status"] in ("blocked", "done", "archived", "triage"):
        break
    if kb.claim_task(conn, data["task"]):
        print(json.dumps({"claimed": True, "at": time.time()}), flush=True)
        break
    time.sleep(0.015)
'''


class Cfg:
    """Заглушка конфигурации: только то, что нужно действию над карточкой."""
    def __init__(self, root: Path, home: Path, db: Path, hermes_home: Path):
        self.hermes_home = Path(hermes_home)
        self.kanban_home = Path(home)
        self.app_dir = ROOT
        self.log_dir = Path(root) / "logs"
        self.tmp_dir = Path(root) / "tmp"
        self.data_dir = Path(root) / "data"
        self.state_dir = Path(root) / "data" / "state"
        self.sandbox = False
        self.mock = False
        self.demo = False
        self.board = "sbx"
        self.hermes_cli = "hermes"
        self._db = Path(db)

    def kanban_db(self, board: str = "") -> Path:
        return self._db


class _Recorder:
    """Подмена CLI Hermes: запоминает вызовы и ничего не делает."""

    def __init__(self) -> None:
        self.calls: list = []

    def run(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess([str(a) for a in argv], 0, "", "")


# ---------------------------------------------------------------- атомарные действия

class AtomicTests(unittest.TestCase):
    """Главное: воркер убит, карточка удержана — одной операцией, проверено по доске."""

    @classmethod
    def setUpClass(cls):
        cls.hermes_home = Path(config_mod.real_hermes_home())
        cls.root = Path(tempfile.mkdtemp(prefix="kanban-atomic-",
                                         dir=str(_outside_hermes_base())))
        cls.home = cls.root / "home"
        cls.home.mkdir()
        cls.cfg = Cfg(cls.root, cls.home, cls.home / "kanban.db", cls.hermes_home)
        if not atomic.available(cls.cfg):
            if os.environ.get("TEAMROOM_KANBAN_ATOMIC_TESTS") == "1":
                raise AssertionError("нужен интерпретатор Hermes: TEAMROOM_KANBAN_ATOMIC_TESTS=1")
            raise unittest.SkipTest(
                "Нет интерпретатора Hermes с hermes_cli.kanban_db — атомарное действие "
                "проверить нечем (это честный пропуск, а не пройденный тест)")
        cls.python = atomic.find_python(cls.cfg, refresh=True)
        cls.tree = atomic.hermes_tree(cls.cfg)
        cls.db = cls.home / "kanban.db"

    def setUp(self):
        self._procs: list = []

    def tearDown(self):
        for proc in self._procs:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass

    # --- поддельная доска ---------------------------------------------------
    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in atomic.CLEAN_ENV_KEYS}
        env["HERMES_KANBAN_HOME"] = str(self.home)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def prep(self, *, task_id: str = "", fields: dict | None = None, **kw) -> dict:
        """Создать/поправить карточку штатными функциями Hermes на поддельной доске."""
        fields = dict(fields or {})
        unknown = set(fields) - ALLOWED_FIELDS
        self.assertFalse(unknown, "тест правит неразрешённые колонки: %s" % sorted(unknown))
        payload = {"tree": str(self.tree), "home": str(self.home), "task_id": task_id,
                   "title": kw.get("title") or "Карточка песочницы",
                   "assignee": kw.get("assignee") or WORKER_PROFILE, "fields": fields}
        done = subprocess.run([self.python, "-c", PREP, json.dumps(payload, ensure_ascii=False)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180, env=self._env())
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        info = json.loads(done.stdout.strip().splitlines()[-1])
        self.db = Path(info["db"])
        self.cfg._db = self.db
        return info

    def worker(self) -> subprocess.Popen:
        """Настоящий процесс-«воркер»: обычный `sleep`, который reclaim должен убить."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        return proc

    def local_lock(self) -> str:
        return "%s:%s" % (socket.gethostname(), os.getpid())

    def running_card(self, *, model: str = "", provider: str = "", lock: str = "",
                     worker: bool = True) -> tuple:
        """Карточка «идёт»: статус running, живой pid воркера, claim этого хоста."""
        proc = self.worker() if worker else None
        fields = {"status": "running", "started_at": int(time.time())}
        if proc is not None:
            fields["worker_pid"] = proc.pid
            fields["claim_lock"] = lock or self.local_lock()
        if model:
            fields["model_override"] = model
            fields["provider_override"] = provider or None
        info = self.prep(fields=fields)
        return info["task_id"], proc

    def row(self, task_id: str) -> dict:
        con = sqlite3.connect(str(self.db), timeout=10)
        try:
            con.row_factory = sqlite3.Row
            found = con.execute("SELECT status, worker_pid, claim_lock, block_kind,"
                                " block_recurrences, model_override, provider_override"
                                " FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(found) if found else {}
        finally:
            con.close()

    def events(self, task_id: str) -> list:
        con = sqlite3.connect(str(self.db), timeout=10)
        try:
            return [str(r[0]) for r in con.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))]
        finally:
            con.close()

    def stopped(self, proc) -> bool:
        """Процесс правда мёртв (а не «мы так думаем»): спрашиваем у ОС."""
        try:
            return proc.wait(timeout=5) is not None
        except subprocess.TimeoutExpired:
            return False

    # --- 1. остановка ------------------------------------------------------
    def test_stop_kills_worker_and_holds_the_card(self):
        """Одна операция: воркер завершён И карточка в `blocked` (а не «в очереди»)."""
        card, proc = self.running_card()
        res = atomic.stop(self.cfg, "sbx", card, reason="остановлено пользователем")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(res["terminated"], res)
        self.assertFalse(res["worker_alive"], res)
        self.assertEqual(res["attempts"], 1, res)
        self.assertIn("reclaim", res["steps"])
        self.assertIn("block", res["steps"])
        self.assertTrue(self.stopped(proc), "процесс воркера жив — остановка не сработала")
        row = self.row(card)
        self.assertEqual(row["status"], "blocked")
        self.assertIsNone(row["worker_pid"], "worker_pid остался — движок не снял claim")
        self.assertIsNone(row["claim_lock"])
        self.assertIn("blocked", self.events(card), "в доске нет события блокировки")

    def test_stop_holds_a_queued_card_without_killing_anything(self):
        """Карточка в очереди: удерживаем, воркера нет — `terminated` честно False."""
        card = self.prep(fields={"status": "ready"})["task_id"]
        res = atomic.stop(self.cfg, "sbx", card)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "blocked")
        self.assertFalse(res["terminated"])
        self.assertFalse(res["worker_alive"])
        self.assertEqual(self.row(card)["status"], "blocked")

    def test_stop_resume_cycles_do_not_triage_the_card(self):
        """Три «остановить/продолжить» подряд — карточка не улетает в `triage`.

        `block_task` Hermes считает повторы одного и того же `block_kind` и на втором уводит
        карточку в `triage` («петля»). Для решения человека это ложное срабатывание: работа
        должна оставаться на паузе, а не исчезать из очереди.
        """
        card = self.prep(fields={"status": "ready"})["task_id"]
        for step in range(3):
            stopped = atomic.stop(self.cfg, "sbx", card)
            self.assertTrue(stopped["ok"], stopped)
            self.assertEqual(stopped["status"], "blocked", "шаг %d: %s" % (step, stopped))
            resumed = atomic.resume(self.cfg, "sbx", card)
            self.assertTrue(resumed["ok"], resumed)
            self.assertEqual(resumed["status"], "ready", resumed)
        row = self.row(card)
        self.assertEqual(row["status"], "ready")
        self.assertLess(int(row["block_recurrences"] or 0), 2,
                        "счётчик петель Hermes вырос — третья остановка увела бы в triage")

    def test_foreign_claim_is_reported_honestly(self):
        """Claim другого хоста: pid не наш — не сигналим и честно говорим «воркер жив»."""
        card, _proc = self.running_card(lock="other-host:4242")
        res = atomic.stop(self.cfg, "sbx", card)
        self.assertFalse(res["ok"], res)
        self.assertTrue(res["worker_alive"], res)
        self.assertFalse(res["terminated"], res)
        self.assertEqual(res["attempts"], 3, "повторов должно быть не больше трёх")
        self.assertEqual(self.row(card)["status"], "blocked")

    def test_unknown_and_finished_cards_are_refused(self):
        """Чужая/несуществующая карточка — `not_found`; завершённая — `conflict`."""
        with self.assertRaises(atomic.AtomicError) as ctx:
            atomic.stop(self.cfg, "sbx", "t_deadbeef")
        self.assertEqual(ctx.exception.code, "not_found")
        card = self.prep(fields={"status": "done"})["task_id"]
        with self.assertRaises(atomic.AtomicError) as ctx2:
            atomic.stop(self.cfg, "sbx", card)
        self.assertEqual(ctx2.exception.code, "conflict")

    # --- 2. продолжение ----------------------------------------------------
    def test_resume_returns_card_to_queue_and_sets_model(self):
        """`resume` с моделью: `set_model` → `unblock` → `ready` в одном процессе."""
        card = self.prep(fields={"status": "blocked", "block_kind": "needs_input"})["task_id"]
        res = atomic.resume(self.cfg, "sbx", card, model=CHEAP[1], provider=CHEAP[0])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "ready")
        row = self.row(card)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["model_override"], CHEAP[1])
        self.assertEqual(row["provider_override"], CHEAP[0])
        self.assertIn("set_model", res["steps"])
        self.assertIn("unblock", res["steps"])
        # снятие модели: пустая строка в запросе = «модель профиля»
        again = atomic.stop(self.cfg, "sbx", card)
        self.assertTrue(again["ok"], again)
        cleared = atomic.resume(self.cfg, "sbx", card, model="")
        self.assertTrue(cleared["ok"], cleared)
        self.assertIsNone(self.row(card)["model_override"])

    def test_resume_of_a_running_card_is_a_conflict(self):
        card, _proc = self.running_card()
        with self.assertRaises(atomic.AtomicError) as ctx:
            atomic.resume(self.cfg, "sbx", card)
        self.assertEqual(ctx.exception.code, "conflict")

    def test_change_model_and_resume_kills_the_worker_and_requeues(self):
        """«Сменить модель и продолжить»: работа снята, модель записана, карточка в очереди."""
        card, proc = self.running_card(model=EXPENSIVE[1], provider=EXPENSIVE[0])
        res = atomic.change_model_and_resume(self.cfg, "sbx", card, model=CHEAP[1],
                                             provider=CHEAP[0])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "ready")
        self.assertTrue(res["terminated"], res)
        self.assertTrue(self.stopped(proc), "старый воркер жив — перезапуск не сработал")
        row = self.row(card)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["model_override"], CHEAP[1], "модель не записалась до снятия работы")
        self.assertEqual(row["provider_override"], CHEAP[0])

    def test_restart_on_model_clears_override_and_requeues_without_pause(self):
        """Сторож: снять работу на чужой модели и вернуть в очередь БЕЗ паузы и без override."""
        card, proc = self.running_card(model=EXPENSIVE[1], provider=EXPENSIVE[0])
        res = atomic.restart_on_model(self.cfg, "sbx", card, model=None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "ready", "сторож не должен парковать карточку")
        self.assertTrue(res["terminated"], res)
        self.assertTrue(self.stopped(proc))
        row = self.row(card)
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["model_override"], "override не снят — работа пойдёт опять на дорогой")
        self.assertIsNone(row["provider_override"])

    def test_restart_of_a_blocked_card_is_a_conflict(self):
        card = self.prep(fields={"status": "blocked"})["task_id"]
        with self.assertRaises(atomic.AtomicError) as ctx:
            atomic.restart_on_model(self.cfg, "sbx", card, model=CHEAP[1])
        self.assertEqual(ctx.exception.code, "conflict")

    def test_a_dispatcher_claim_in_the_gap_is_taken_back(self):
        """Диспетчер успел взять карточку — итог всё равно «остановлено», воркер мёртв.

        Настоящий второй процесс (как `hermes kanban` диспетчера) в цикле пробует
        `claim_task` — то есть ровно то, что диспетчер делает каждые 30 с (и сразу, если
        освободился слот). Один захват он делает максимум: если он попал в окно между
        `reclaim` и `block`, проверка это видит и повтор снимает захват. Итог — `blocked` и
        мёртвый воркер, а не «остановлено» над работающей карточкой.
        """
        card, proc = self.running_card()
        racer = subprocess.Popen([self.python, "-c", RACER,
                                  json.dumps({"tree": str(self.tree), "home": str(self.home),
                                              "db": str(self.db), "task": card,
                                              "seconds": 10}, ensure_ascii=False)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 encoding="utf-8", errors="replace", env=self._env())
        self._procs.append(racer)
        res = atomic.stop(self.cfg, "sbx", card, reason="остановлено пользователем")
        try:
            out, _err = racer.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            racer.kill()
            out, _err = racer.communicate()
        claimed = "claimed" in (out or "")
        self.assertTrue(res["ok"], "захват в окне не снят: %s (захват был: %s)" % (res, claimed))
        self.assertEqual(res["status"], "blocked", res)
        self.assertFalse(res["worker_alive"], res)
        self.assertTrue(self.stopped(proc), "воркер жив — остановка не сработала")
        row = self.row(card)
        self.assertEqual(row["status"], "blocked", row)
        self.assertIsNone(row["claim_lock"], "захват остался на карточке")

    # --- 3. сквозь engine_write (как кнопка «Остановить») -------------------
    def test_engine_write_uses_the_atomic_path_end_to_end(self):
        """Кнопка «Остановить» идёт через `ew.card_stop` — один процесс, карточка на паузе."""
        card, proc = self.running_card()
        res = ew.card_stop(self.cfg, "sbx", card, reason="остановлено пользователем")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["path"], "atomic", "действие ушло старым путём CLI")
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(res["held"])
        self.assertTrue(res["terminated"], res)
        self.assertTrue(self.stopped(proc), "воркер жив — остановка не сработала")
        self.assertEqual(self.row(card)["status"], "blocked")
        self.assertGreater(res["cmd_ms"], 0, "время команды не замерено")
        self.assertTrue((Path(self.cfg.log_dir) / "engine.log").is_file(),
                        "атомарное действие не попало в engine.log")

    def test_engine_write_resume_and_restart_go_the_atomic_way(self):
        """«Продолжить» и перезапуск сторожа — тоже одним процессом (path: atomic)."""
        card, _proc = self.running_card(model=EXPENSIVE[1], provider=EXPENSIVE[0])
        restarted = ew.card_restart_on_model(self.cfg, "sbx", card, model=None)
        self.assertTrue(restarted["ok"], restarted)
        self.assertEqual(restarted["path"], "atomic")
        self.assertEqual(restarted["status"], "ready")
        self.assertIsNone(self.row(card)["model_override"])
        stopped = ew.card_stop(self.cfg, "sbx", card)
        self.assertTrue(stopped["ok"], stopped)
        resumed = ew.card_resume(self.cfg, "sbx", card, model=CHEAP[1], provider=CHEAP[0])
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["path"], "atomic")
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(self.row(card)["model_override"], CHEAP[1])


# ---------------------------------------------------------------- запасной путь (engine_write)

class _Cli:
    """Подмена CLI: меняет «доску» и записывает глаголы — проверяем последовательность."""

    def __init__(self, state: dict, *, block_plan=(True,)) -> None:
        self.state = state
        self.block_plan = list(block_plan)
        self.calls: list = []

    def __call__(self, cfg, board, args, timeout=None):
        verb = str(args[0])
        self.calls.append([str(a) for a in args])
        if verb == "reclaim":
            self.state["status"] = "ready"
        elif verb == "block":
            if self.block_plan:
                ok = self.block_plan.pop(0)
            else:
                ok = True
            if ok:
                self.state["status"] = "blocked"
        elif verb in ("unblock", "promote"):
            self.state["status"] = "ready"
        elif verb == "set-model":
            self.state["model"] = args[2]
        return "", 7

    def verbs(self) -> list:
        return [call[0] for call in self.calls]


class FallbackTests(unittest.TestCase):
    """Без интерпретатора Hermes: прежняя последовательность CLI + проверка + повтор."""

    def setUp(self):
        self.cfg = Cfg(Path(tempfile.gettempdir()), Path(tempfile.gettempdir()),
                       Path(tempfile.gettempdir()) / "kanban.db", Path(tempfile.gettempdir()))
        self.state = {"status": "running", "worker_pid": None, "claim_lock": "h:1"}

    def _run(self, mode, *, block_plan=(True,), model=ew.KEEP_MODEL):
        cli = _Cli(self.state, block_plan=block_plan)
        with mock.patch.object(ew, "_atomic_call", return_value=None), \
                mock.patch.object(ew, "_run", cli), \
                mock.patch.object(ew, "_card_worker", lambda *a, **k: dict(self.state)):
            if mode == "stop":
                return ew.card_stop(self.cfg, "sbx", CARD), cli
            if mode == "resume":
                return ew.card_resume(self.cfg, "sbx", CARD, model=model), cli
            if mode == "model":
                return ew.card_model_and_resume(self.cfg, "sbx", CARD, model=CHEAP[1],
                                                provider=CHEAP[0]), cli
            return ew.card_restart_on_model(self.cfg, "sbx", CARD, model=None), cli

    def test_stop_sequence_is_reclaim_then_block(self):
        """Остановка без Hermes: `reclaim` → `block`, ответ честно помечен `path: cli`."""
        res, cli = self._run("stop")
        self.assertEqual(cli.verbs(), ["reclaim", "block"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["path"], "cli")
        self.assertEqual(res["attempts"], 1)
        # W29-P1: у `block` причина — ПОЗИЦИОННЫЙ аргумент (после `--`), а не `--reason=…`;
        # флаг раньше попадал в доску как сама причина. W29-I1: проверка приведена к
        # фактическому контракту CLI (тест устарел вместе с дефектом, не ослаблен).
        block_call = cli.calls[1]
        self.assertIn("--", block_call)
        self.assertFalse([t for t in block_call if t.startswith("--reason")],
                         "причина ушла флагом — CLI запишет флаг в доску как причину")
        self.assertTrue(block_call[block_call.index("--") + 1:],
                        "причины после `--` нет")

    def test_stop_repeats_when_the_card_comes_back(self):
        """Первый `block` не удержал — повтор (до 3 раз), карточка всё равно на паузе."""
        res, cli = self._run("stop", block_plan=(False, True))
        self.assertEqual(cli.verbs().count("block"), 2)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["attempts"], 2)

    def test_stop_is_honest_when_the_card_stays_in_the_queue(self):
        res, cli = self._run("stop", block_plan=(False, False, False))
        self.assertEqual(cli.verbs().count("block"), 3)
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["status"], "ready")
        self.assertIn("снова в очереди", res["detail"])

    def test_resume_and_model_change_sequences(self):
        """Продолжение: `unblock` (+`promote`); смена модели: `set-model` → `unblock`."""
        self.state["status"] = "blocked"
        res, cli = self._run("resume")
        self.assertEqual(cli.verbs(), ["unblock"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "ready")

        self.state = {"status": "blocked", "worker_pid": None, "claim_lock": None}
        res2, cli2 = self._run("model")
        self.assertEqual(cli2.verbs(), ["set-model", "unblock"])
        self.assertEqual(cli2.calls[0][2], CHEAP[1])
        self.assertTrue(res2["ok"], res2)

    def test_model_change_on_a_running_card_reclaims(self):
        res, cli = self._run("model")
        self.assertEqual(cli.verbs(), ["set-model", "reclaim"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "ready")

    def test_atomic_switch_defaults(self):
        """В песочнице и в демо — прежний путь CLI; env `TEAMROOM_KANBAN_ATOMIC` сильнее."""
        sandbox = Cfg(Path(tempfile.gettempdir()), Path(tempfile.gettempdir()),
                      Path(tempfile.gettempdir()) / "kanban.db", Path(tempfile.gettempdir()))
        sandbox.sandbox = True
        with mock.patch.dict(os.environ, {"TEAMROOM_KANBAN_ATOMIC": ""}):
            self.assertFalse(ew.atomic_enabled(sandbox))
            self.assertTrue(ew.atomic_enabled(self.cfg))
        with mock.patch.dict(os.environ, {"TEAMROOM_KANBAN_ATOMIC": "1"}):
            self.assertTrue(ew.atomic_enabled(sandbox))
        with mock.patch.dict(os.environ, {"TEAMROOM_KANBAN_ATOMIC": "0"}):
            self.assertFalse(ew.atomic_enabled(self.cfg))

    def test_atomic_result_is_used_when_available(self):
        """Есть Hermes — берём результат одного процесса, CLI не зовём вовсе."""
        class _Mod:
            def __init__(self):
                self.calls = []

            def available(self, cfg):
                return True

            def stop(self, cfg, board, task_id, **kw):
                self.calls.append((board, task_id, kw))
                return {"ok": True, "status": "blocked", "terminated": True,
                        "worker_alive": False, "attempts": 1, "ms": 96,
                        "steps": ["reclaim", "block"], "path": "atomic"}

        mod = _Mod()
        cli = _Cli(self.state)
        with mock.patch.object(ew, "_atomic", lambda: mod), \
                mock.patch.object(ew, "_run", cli), \
                mock.patch.dict(os.environ, {"TEAMROOM_KANBAN_ATOMIC": "1"}):
            res = ew.card_stop(self.cfg, "sbx", CARD, reason="остановлено пользователем")
        self.assertEqual(cli.calls, [], "атомарный путь не должен звать CLI")
        self.assertEqual(len(mod.calls), 1)
        self.assertEqual(mod.calls[0][2]["reason"], "остановлено пользователем")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["path"], "atomic")
        self.assertEqual(res["cmd_ms"], 96)
        self.assertIn("паузе", res["detail"])


# ---------------------------------------------------------------- сторож не поднимает

class GuardKeepsStoppedTests(unittest.TestCase):
    """После «Остановить» сторож моделей карточку не «чинит» и не возобновляет (два тика)."""

    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls.sb = cls._stack.enter_context(Sandbox())
        cls.cfg = cls.sb.cfg
        cls.real_before = cls.sb.snapshot_real()
        cls.sa = SimAgents(cls.sb)
        cls.recorder = _Recorder()
        cls._real_sub = engine_cli.subprocess
        engine_cli.subprocess = cls.recorder
        ew.subprocess = cls.recorder

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
            cls._stack.close()

    def setUp(self):
        self.recorder.calls.clear()
        con = self.sa.connect()
        try:
            con.execute("UPDATE tasks SET status='done' WHERE tenant = ?", (TENANT,))
            con.commit()
        finally:
            con.close()

    def room(self) -> dict:
        return {"id": ROOM, "title": "Песочная комната", "board": "team", "tenant": TENANT,
                "orchestrator": "team-director", "critic": "team-critic",
                "members": [{"profile": WORKER_PROFILE, "role": "worker"},
                            {"profile": "team-director", "role": "orchestrator"}],
                "agent_models": {}, "model_guard": "enforce", "context_files": []}

    def status(self, card_id: str) -> str:
        found = self.sb.one("SELECT status FROM tasks WHERE id = ?", (card_id,))
        return str(found["status"]) if found else ""

    def test_two_ticks_do_not_resurrect_a_card_stopped_by_the_user(self):
        card = self.sa.card("Долгая работа", assignee=WORKER_PROFILE, status="blocked")
        guard.mark_stopped(self.cfg, ROOM, card, by="user", reason="остановлено пользователем")
        self.assertEqual(guard.stopped_by(self.cfg, ROOM, card), "user")
        for _ in range(2):
            out = guard.tick(self.cfg, self.room())
            self.assertNotIn(card, out["stopped"], out)
            self.assertNotIn(card, out["fixed"], out)
        self.assertEqual(self.status(card), "blocked", "сторож поднял остановленную карточку")
        touched = [call for call in self.recorder.calls if any(card in part for part in call)]
        self.assertEqual(touched, [], "сторож позвал движок для карточки, остановленной человеком")
        self.assertEqual(guard.stopped_by(self.cfg, ROOM, card), "user",
                         "метка «остановлено вами» потерялась")


if __name__ == "__main__":
    unittest.main(verbosity=2)
