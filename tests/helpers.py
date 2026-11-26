"""Песочница для backend-тестов и защита реального канбана.

`Sandbox` — контекстный менеджер: делает временный каталог ВНЕ реального
`%LOCALAPPDATA%\\hermes` (иначе `kanban_home()` всё равно вернёт настоящий корень
и изоляции не будет — ENGINE-FACTS §8.1), наполняет его `tools.seed_sandbox.py`,
выставляет `TEAMROOM_HERMES_HOME` / `TEAMROOM_DATA` / `TEAMROOM_VAULT_DIR`,
отдаёт `server.config.load()` и убирает за собой.

    from tests.helpers import Sandbox
    with Sandbox() as sb:
        before = sb.snapshot_real()
        ...
        sb.assert_real_untouched(before)

`snapshot_real()` больше НЕ сравнивает содержимое живой `kanban.db`: в ней
одновременно работает сам пользователь и его приложение (диспетчер, агенты
руководителя, новые запросы), поэтому карточки, комментарии, размер и mtime
меняются законно и вне нашего процесса. Раньше сравнение «наш след» давало
ложные падения (W20-I1 п.23). Что остаётся защитой:

  * конфиги Hermes (`config.yaml`, `profiles/*/config.yaml`) — снимаем хэши и
    требуем совпадения: тесты их только читают;
  * живая доска не должна исчезнуть/появиться (факт наличия файла);
  * запись в реальный Hermes по-прежнему запрещена сторожем
    `safety.forbid_real_writes()` (включается в `Sandbox`), а `_assert_writable_db`
    не даёт открыть живую БД на запись.

Статистика по доске (число карточек, «наши» карточки, размер) остаётся в
слепке как справка для отчёта, но решения об ошибке по ней не принимаются.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import config as config_mod            # noqa: E402  (после настройки sys.path)
from server import safety                         # noqa: E402
from tools.seed_sandbox import (                  # noqa: E402
    BOARD,
    _assert_writable_db,
    _insert,
    _is_under,
    _real_hermes_home,
    build_sandbox,
)

SANDBOX_ENV_KEYS = ("TEAMROOM_HERMES_HOME", "TEAMROOM_DATA",
                    "TEAMROOM_VAULT_DIR", "HERMES_KANBAN_HOME")
REAL_BOARD = "team"
#: `created_by`, которым наше приложение помечает СВОИ карточки и комментарии
#: (`server/engine_write.py: CREATED_BY`). По нему и узнаём «наш след» в доске.
OUR_CREATORS = ("Комната команды",)


def _outside_hermes_base() -> Path:
    """Каталог для временных песочниц, гарантированно вне реального Hermes."""
    real = _real_hermes_home()
    candidates: list[Path] = []
    if os.environ.get("TEAMROOM_TEST_TMP"):
        candidates.append(Path(os.environ["TEAMROOM_TEST_TMP"]))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Temp")
    candidates.append(Path(tempfile.gettempdir()))
    candidates.append(PROJECT_ROOT / "data" / "_tests")
    for base in candidates:
        try:
            if _is_under(base, real):
                continue
            base.mkdir(parents=True, exist_ok=True)
            probe = tempfile.mkdtemp(prefix=".probe-", dir=str(base))
            os.rmdir(probe)
            return base
        except OSError:
            continue
    raise RuntimeError(f"не нашёл каталог вне реального Hermes {real} для песочницы тестов")


def real_hermes_home() -> Path:
    """Реальный `%HERMES_HOME%` — корень, который тесты не имеют права менять."""
    return _real_hermes_home()


#: Ростер песочного дома: те же имена, что в фикстурах и `tools/check_conformance.py`
#: (`_write_profiles`). Без него тесты ищут `team-director` в ЖИВОМ Hermes, где живут
#: `исполнители`, и падают «Профиля нет в ростере» — это состояние стенда, а не регресс.
FAKE_ROSTER = {
    "team-director": ("Директор Лето", "director", "Разбирает задачу и сводит ответ.", ["декомпозиция"]),
    "team-critic": ("Критик", "critic", "Проверяет свод перед ответом.", ["проверка"]),
    "coder": ("Программист", "worker", "Пишет и проверяет код.", ["python"]),
    "team-lawyer": ("Юрист проекта", "worker", "Проверяет договоры и риски (юрист комнаты).", ["право"]),
    "team-cfo": ("Финансист Лето", "worker", "Считает юнит-экономику.", ["финансы"]),
    "team-ops": ("Операции", "worker", "Ведёт поставщиков и упаковку.", ["операции"]),
    "team-marketing": ("Маркетолог", "worker", "Готовит витрину и тексты.", ["маркетинг"]),
    "team-strategy": ("Стратег", "worker", "Сравнивает рынок и цены.", ["стратегия"]),
    "team-runner": ("Гонец Лето", "worker", "Забирает и проверяет файлы.", ["сбор"]),
}

#: Конфиг профиля песочницы: ровно то, что читают модули (провайдер, модель).
#: Провайдер тот же, что у боевых профилей стенда, — тесты сверяют ростер по нему.
FAKE_PROFILE_CONFIG = "model:\n  provider: ollama-cloud\n  default: test-model\n"


def seed_fake_hermes_home(home: Path) -> Path:
    """Создать ПОДДЕЛЬНЫЙ дом Hermes с ростером профилей внутри песочницы.

    `cfg.hermes_home` указывает на реальный `%LOCALAPPDATA%\\hermes` (его конфиги только
    читаются), поэтому тестам нужен свой дом: иначе они ищут `team-director` в живом
    ростере и падают. Путь обязан быть вне реального Hermes — проверяется сторожем.

        home = seed_fake_hermes_home(sb.root / "hermes")
        cfg = dataclasses.replace(cfg, hermes_home=home)
    """
    home = Path(home)
    real = safety.real_hermes_home().resolve()
    target = home.resolve()
    if target == real or real in target.parents or target in real.parents:
        raise RuntimeError("отказ: ростер профилей в реальном каталоге Hermes: %s" % target)
    safety.assert_not_real(home, "ростер профилей песочницы")
    safety.guarded_mkdir(home, parents=True, exist_ok=True)
    safety.guarded_write_text(home / "config.yaml", FAKE_PROFILE_CONFIG)
    # Состояние движка (pid диспетчера) читается из gateway_state.json — копия живого, только чтение.
    gateway = real / "gateway_state.json"
    if gateway.is_file():
        try:
            safety.guarded_copy2(gateway, home / "gateway_state.json")
        except Exception:                      # noqa: BLE001 - состояние движка вторично
            pass
    for name, (title, kind, description, tags) in FAKE_ROSTER.items():
        folder = home / "profiles" / name
        safety.guarded_mkdir(folder, parents=True, exist_ok=True)
        safety.guarded_write_text(folder / "config.yaml", FAKE_PROFILE_CONFIG)
        safety.guarded_write_text(
            folder / "profile.yaml",
            "title: %s\nkind: %s\ndescription: %s\ntags: [%s]\n"
            % (title, kind, description, ", ".join(tags)))
    return home


def real_db_path(cfg: Any = None) -> Path:
    """Путь к РЕАЛЬНОЙ БД доски `team` (только чтение, никогда не песочная).

    В песочнице `cfg.hermes_home` — поддельный дом (`<data>/hermes-home`, W28-F), поэтому
    «реальную» доску всегда считаем от настоящего `HERMES_HOME`: иначе проверка «песочная
    БД не совпала с живой» сравнивала бы песочницу с песочницей.
    """
    if cfg is not None and not getattr(cfg, "sandbox", False):
        home = Path(cfg.hermes_home)
    else:
        home = _real_hermes_home()
    return Path(home) / "kanban" / "boards" / REAL_BOARD / "kanban.db"


def config_hashes(home: Any = None) -> dict:
    """Хэши конфигов Hermes (`config.yaml`, `profiles/*/config.yaml`) — только чтение.

    Это единственная часть «реальных файлов», которую тесты обязаны не менять:
    конфиги не трогает ни пользователь, ни его приложение во время прогона.
    """
    root = Path(home) if home is not None else _real_hermes_home()
    out: dict = {}
    candidates = [root / "config.yaml"]
    try:
        candidates += sorted((root / "profiles").glob("*/config.yaml"))
    except OSError:
        pass
    for path in candidates:
        try:
            out[config_label(path, root)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def config_label(path: Path, root: Path | None = None) -> str:
    """Подпись конфига для сообщения об ошибке: без полного пути пользователя."""
    root = Path(root) if root is not None else _real_hermes_home()
    try:
        rel = Path(path).relative_to(root).as_posix()
    except (ValueError, OSError):
        return Path(path).name
    return rel


def snapshot_real_db(cfg: Any = None) -> dict:
    """Слепок реального Hermes: конфиги (хэши), наличие живой доски и справка.

    Читаем живую `kanban.db` только в режиме `mode=ro` и ТОЛЬКО как справку:
    содержимое живой доски меняется законно и вне нашего процесса (пользователь
    работает в приложении, Hermes крутит диспетчера и агентов руководителя),
    поэтому сравнивать её построчно нельзя — это давало ложные падения.
    """
    home = Path(getattr(cfg, "hermes_home", "") or _real_hermes_home()) if cfg is not None \
        else _real_hermes_home()
    db = real_db_path(cfg)
    info = {"exists": False, "tasks_count": 0, "ours": frozenset(), "our_comments": 0,
            "board_bytes": 0, "board_mtime": 0, "db": str(db)}
    if not db.exists():
        info["config"] = config_hashes(home)
        return info
    try:
        stat = db.stat()
    except OSError:
        info["config"] = config_hashes(home)
        return info
    tasks_count = 0
    ours: frozenset = frozenset()
    our_comments = 0
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            tasks_count = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
            marks = ", ".join("?" * len(OUR_CREATORS))
            ours = frozenset(row[0] for row in con.execute(
                "SELECT id FROM tasks WHERE COALESCE(created_by,'') IN (%s)" % marks,
                OUR_CREATORS))
            try:
                our_comments = con.execute(
                    "SELECT count(*) FROM task_comments WHERE COALESCE(author,'') IN (%s)" % marks,
                    OUR_CREATORS).fetchone()[0]
            except sqlite3.Error:              # старая/чужая схема — не падаем
                our_comments = 0
        finally:
            con.close()
    except sqlite3.Error:                      # доска занята/битая — это не наша ошибка
        pass
    info.update({"exists": True, "tasks_count": int(tasks_count), "ours": ours,
                 "our_comments": int(our_comments),
                 "board_bytes": int(stat.st_size), "board_mtime": int(stat.st_mtime),
                 "config": config_hashes(home)})
    return info


def assert_real_untouched(before: dict, cfg: Any = None) -> dict:
    """Упасть, если тест изменил то, что менять нельзя: конфиги Hermes и доску.

    Содержимое живой `kanban.db` намеренно не сравнивается (см. `snapshot_real_db`):
    она меняется законно. Проверяем ровно две вещи, которые от прогона тестов
    зависеть не должны:

    * хэши `config.yaml` / `profiles/*/config.yaml` — тесты их только читают;
    * файл живой доски не исчез и не появился.

    Запись в реальный Hermes при этом физически запрещена: `Sandbox` включает
    `safety.forbid_real_writes()`, а `_assert_writable_db()` не пускает на запись
    в живую канбан-БД (эти защиты проверяют test_safety и test_seed).
    """
    after = snapshot_real_db(cfg)
    problems: list = []
    if bool(after["exists"]) != bool(before.get("exists")):
        problems.append("файл живой доски появился/исчез: %s → %s"
                        % (before.get("exists"), after["exists"]))
    was = dict(before.get("config") or {})
    now = dict(after.get("config") or {})
    for label, digest in sorted(was.items()):
        if now.get(label) not in (None, digest):
            problems.append("изменён конфиг Hermes: %s" % label)
    if problems:
        raise AssertionError("реальные файлы Hermes: " + "; ".join(problems))
    return after


class SimAgents:
    """Имитация движка и агентов прямой записью в ПЕСОЧНУЮ kanban.db.

    Зачем: CLI не умеет ставить ``running``/``review`` и не «играет» за агентов,
    а сквозной сценарий требует настоящих состояний движка (decompose, run'ы,
    ``kanban_complete`` с metadata/вердиктом). Поэтому тест сам пишет карточки —
    но ТОЛЬКО в БД внутри песочницы: перед каждым соединением
    ``_assert_writable_db`` проверяет, что файл не в реальном Hermes.

        sa = SimAgents(sb)
        kids = sa.decompose(root_id, [{"title": "Срез", "assignee": "team-cfo"}])
        sa.finish(kids[0], "Готово", {"next_steps": [...]})
    """

    def __init__(self, sandbox: "Sandbox") -> None:
        self.sb = sandbox
        self.db = Path(sandbox.db)
        self.root = Path(sandbox.root)
        self._seq = 0

    # --- служебное ---------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Соединение на запись — только песочная БД (иначе AssertionError)."""
        _assert_writable_db(self.db, self.root)
        con = sqlite3.connect(str(self.db), timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def _next_id(self, key: str) -> str:
        self._seq += 1
        raw = "%s:%s:%s" % (key, self._seq, time.time_ns())
        return "t_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]

    def _row(self, con, task_id: str):
        return con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def _event(self, con, task_id: str, kind: str, payload: dict | None = None, at: int | None = None):
        _insert(con, "task_events", {"task_id": task_id, "kind": kind,
                                     "payload": json.dumps(payload or {}, ensure_ascii=False),
                                     "created_at": at or int(time.time())})

    # --- карточки ----------------------------------------------------------
    def card(self, title: str, assignee: str, status: str = "todo", body: str = "",
             tenant: str | None = "room:cafe", parent: str | None = None,
             created_by: str = "Михаил", card_id: str | None = None) -> str:
        """Создать карточку (по умолчанию ``todo``) и, если нужно, связь с родителем."""
        cid = card_id or self._next_id(title)
        now = int(time.time())
        con = self.connect()
        try:
            _insert(con, "tasks", {"id": cid, "title": title, "body": body or title, "assignee": assignee,
                                   "status": status, "created_by": created_by, "created_at": now,
                                   "tenant": tenant, "priority": 1, "consecutive_failures": 0})
            self._event(con, cid, "created", {"status": status}, now)
            if parent:
                _insert(con, "task_links", {"parent_id": parent, "child_id": cid})
            con.commit()
        finally:
            con.close()
        return cid

    def decompose(self, root_id: str, children: list, assignee: str = "auto-decomposer") -> list:
        """Разложить корень: дети ``running`` + связи child_id=root + событие decomposed.

        ``children`` — список dict'ов ``{title, assignee, body?, status?}``.
        Корень остаётся ``todo``: движок ждёт детей; ``assignee`` — как в реальном
        событии ``decomposed`` (у комнаты другой директор → watcher переназначит).
        """
        con = self.connect()
        ids: list = []
        try:
            row = self._row(con, root_id)
            if row is None:
                raise AssertionError("SimAgents.decompose: нет карточки %s" % root_id)
            tenant = row["tenant"]
            now = int(time.time())
            for spec in children:
                cid = spec.get("id") or self._next_id(str(spec.get("title")))
                status = spec.get("status") or "running"
                _insert(con, "tasks", {"id": cid, "title": spec.get("title") or "Срез",
                                       "body": spec.get("body") or spec.get("title") or "Срез",
                                       "assignee": spec.get("assignee"), "status": status,
                                       "created_by": "auto-decomposer", "created_at": now,
                                       "started_at": now if status == "running" else None,
                                       "tenant": tenant, "priority": 1, "consecutive_failures": 0})
                self._event(con, cid, "created", {"status": status}, now)
                if status == "running":
                    con.execute(
                        "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                        (cid, spec.get("assignee"), "running", now))
                    con.execute("UPDATE tasks SET current_run_id=(SELECT max(id) FROM task_runs "
                                "WHERE task_id=?), last_heartbeat_at=? WHERE id=?", (cid, now, cid))
                    self._event(con, cid, "claimed", {"profile": spec.get("assignee")}, now)
                _insert(con, "task_links", {"parent_id": cid, "child_id": root_id})
                ids.append(cid)
            con.execute("UPDATE tasks SET assignee = ?, status = 'todo' WHERE id = ?", (assignee, root_id))
            self._event(con, root_id, "decomposed",
                        {"child_ids": ids, "root_assignee": assignee}, now)
            con.commit()
        finally:
            con.close()
        return ids

    def start(self, task_id: str) -> None:
        """Взять карточку в работу: running + открытый run."""
        now = int(time.time())
        con = self.connect()
        try:
            row = self._row(con, task_id)
            con.execute("UPDATE tasks SET status='running', started_at=coalesce(started_at, ?) "
                        "WHERE id=?", (now, task_id))
            con.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                        (task_id, row["assignee"], "running", now))
            con.execute("UPDATE tasks SET current_run_id=(SELECT max(id) FROM task_runs "
                        "WHERE task_id=?), last_heartbeat_at=? WHERE id=?", (task_id, now, task_id))
            self._event(con, task_id, "claimed", {"profile": row["assignee"]}, now)
            con.commit()
        finally:
            con.close()

    def finish(self, task_id: str, summary: str, metadata: dict | None = None,
               verdict: str | None = None, issues: list | None = None) -> None:
        """Завершить карточку как агент: run done/completed + summary/metadata."""
        meta = dict(metadata or {})
        if verdict is not None:
            meta["verdict"] = verdict
        if issues is not None:
            meta["issues"] = issues
        now = int(time.time())
        con = self.connect()
        try:
            row = self._row(con, task_id)
            if row is None:
                raise AssertionError("SimAgents.finish: нет карточки %s" % task_id)
            open_run = con.execute("SELECT id FROM task_runs WHERE task_id=? AND status='running' "
                                   "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            payload = json.dumps(meta, ensure_ascii=False) if meta else None
            if open_run:
                con.execute("UPDATE task_runs SET status='done', outcome='completed', summary=?, "
                            "metadata=?, ended_at=? WHERE id=?",
                            (summary, payload, now, open_run["id"]))
                run_id = open_run["id"]
            else:
                cur = con.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, summary, metadata, "
                    "started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
                    (task_id, row["assignee"], "done", "completed", summary, payload, now, now))
                run_id = cur.lastrowid
            con.execute("UPDATE tasks SET status='done', completed_at=?, current_run_id=?, "
                        "claim_lock=NULL, last_failure_error=NULL WHERE id=?",
                        (now, run_id, task_id))
            self._event(con, task_id, "completed", {"run_id": run_id, "summary": summary[:200]}, now)
            con.commit()
        finally:
            con.close()

    def comment(self, task_id: str, author: str, text: str) -> None:
        con = self.connect()
        try:
            _insert(con, "task_comments", {"task_id": task_id, "author": author, "body": text,
                                           "created_at": int(time.time())})
            con.commit()
        finally:
            con.close()

    def card_status(self, task_id: str) -> str:
        con = self.connect()
        try:
            row = self._row(con, task_id)
            return str(row["status"]) if row else ""
        finally:
            con.close()


class Sandbox:
    """Временная песочница комнаты: дерево карточек во всех состояниях."""

    def __init__(self) -> None:
        self._base = _outside_hermes_base()
        self.root = Path(tempfile.mkdtemp(prefix="teamroom-", dir=str(self._base)))
        self._saved_env: dict[str, str | None] = {}
        self._entered = False
        self._guards: list = []
        self.ids: dict = {}
        self.cfg: Any = None
        #: Конфиг с РЕАЛЬНЫМ домом Hermes — только для слепков «не трогали живое».
        self.cfg_real: Any = None
        #: Поддельный дом Hermes песочницы с ростером `team-*` (cfg.hermes_home).
        self.hermes_home: Path | None = None

    # --- жизненный цикл -----------------------------------------------------
    def _enter_guards(self) -> None:
        """Включить запрет записи в реальный Hermes/Vault и агентского CLI.

        С этого момента любой код внутри песочницы (и тесты, и сервер, и
        инструменты) получает `SafetyError` при попытке писать в реальный
        `%LOCALAPPDATA%\\hermes` или `C:\\TeamRoom\\Vault` — см. docs/SAFETY-RULES.md.
        """
        self._guards = []
        for cm in (safety.forbid_real_writes(), safety.forbid_real_subprocess()):
            cm.__enter__()
            self._guards.append(cm)

    def _exit_guards(self) -> None:
        while self._guards:
            cm = self._guards.pop()
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - снятие патчей не должно ронять уборку
                pass

    def __enter__(self) -> "Sandbox":
        self._enter_guards()
        try:
            self.ids = build_sandbox(self.root)
            for key, value in self.ids["paths"]["env"].items():
                self._saved_env[key] = os.environ.get(key)
                os.environ[key] = str(value)
            self.cfg = config_mod.load()
            self.cfg_real = self.cfg
            # Ростер профилей — в ПОДДЕЛЬНОМ доме внутри песочницы (W28-F): иначе тесты
            # ищут `team-*` в живом Hermes, где живут `исполнители`, и падают «Профиля нет
            # в ростере». Тот же приём, что у `tools/check_conformance.py::_write_profiles`.
            self.hermes_home = seed_fake_hermes_home(Path(self.cfg.data_dir) / "hermes-home")
            self.cfg = dataclasses.replace(self.cfg, hermes_home=self.hermes_home)
            self._entered = True
            db = Path(self.cfg.kanban_db(BOARD))
            if not _is_under(db, self.root):
                raise RuntimeError(f"cfg.kanban_db вне песочницы: {db}")
            if not _is_under(Path(self.cfg.hermes_home), self.root):
                raise RuntimeError(f"cfg.hermes_home вне песочницы: {self.cfg.hermes_home}")
            if not self.cfg.sandbox:
                raise RuntimeError("cfg.sandbox=False: TEAMROOM_HERMES_HOME не применился")
            return self
        except Exception:
            self._exit_guards()
            self._restore_env()
            self._rmtree()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._exit_guards()
        self._restore_env()
        self._rmtree()
        return False

    def _restore_env(self) -> None:
        for key, old in self._saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._saved_env.clear()

    def _rmtree(self) -> None:
        if not self.root.exists():
            return

        def _onerror(func, path, _exc):
            try:  # read-only файлы/каталоги Windows
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass

        for _attempt in range(3):
            shutil.rmtree(self.root, onerror=_onerror)
            if not self.root.exists():
                return
            time.sleep(0.1)  # дать отпустить WAL/SHM после закрытия соединений
        shutil.rmtree(self.root, ignore_errors=True)

    # --- доступ к песочнице -------------------------------------------------
    @property
    def paths(self) -> dict:
        return self.ids.get("paths", {})

    @property
    def db(self) -> Path:
        """БД песочной доски `team`."""
        if self.cfg is not None:
            return Path(self.cfg.kanban_db(BOARD))
        return Path(self.ids["paths"]["db"])

    @property
    def data_dir(self) -> Path:
        return Path(self.ids["paths"]["data"])

    @property
    def vault_dir(self) -> Path:
        return Path(self.ids["paths"]["vault"])

    @property
    def kanban_home(self) -> Path:
        return Path(self.ids["paths"]["kanban_home"])

    def state(self, name: str) -> dict:
        """Описание одного дерева: `queued`, `working`, `done_modern`, …"""
        return self.ids[name]

    def connect(self) -> sqlite3.Connection:
        """Соединение с песочной БД (read-only) — только для проверок в тестах."""
        return sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True, timeout=10)

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        con = self.connect()
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def assert_writable_db_guard(self) -> None:
        """Проверка защиты: запись в реальную БД и за пределы песочницы запрещена."""
        _assert_writable_db(self.db, self.root)
        outside = self.root.parent / ".outside-sandbox" / "kanban.db"
        for bad in (real_db_path(self.cfg_real or self.cfg), outside):
            try:
                _assert_writable_db(bad, self.root)
            except AssertionError:
                continue
            raise AssertionError(f"защита не сработала для {bad}")

    # --- защита реальной доски ---------------------------------------------
    def snapshot_real(self, cfg: Any = None) -> dict:
        """Слепок РЕАЛЬНОЙ доски и конфигов Hermes.

        Берём `cfg_real` (дом Hermes настоящий): `self.cfg.hermes_home` — поддельный дом
        песочницы, и слепок по нему ничего не сказал бы о живых конфигах.
        """
        return snapshot_real_db(cfg or self.cfg_real or self.cfg)

    def assert_real_untouched(self, before: dict) -> dict:
        """Упасть, если реальная доска изменилась после `before`."""
        return assert_real_untouched(before, self.cfg_real or self.cfg)
