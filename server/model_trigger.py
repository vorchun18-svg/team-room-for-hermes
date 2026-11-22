"""Триггер БД: пин модели ставится в ТОЙ ЖЕ транзакции, что и создание карточки (W29-P10).

Первопричина (Vault `RootCause-2026-11-20-model-selection.md`): выбор модели комнаты живёт
в реестре Team Room, который Hermes не читает. Пока пин «догоняет» карточку из Python,
между `INSERT` и `claim` диспетчера нет ни одной гарантированной секунды — гонка по
построению. Здесь закрыт корень: служебная таблица `teamroom_room_models` + триггеры
`AFTER INSERT ON tasks` и `AFTER UPDATE OF assignee, status ON tasks`. При пустом
`model_override` у карточки комнаты (`tenant LIKE 'room:%'`) триггер заполняет
`model_override/provider_override/reasoning_effort` из таблицы — до того, как диспетчер
увидит строку. Работает при ЛЮБОМ создателе: декомпозитор Hermes, руководитель
(`kanban_create`), CLI, кроны, приложение.

Границы (важно):

* приложение по-прежнему НЕ пишет карточки напрямую: карточки создаёт штатный
  `hermes_cli.kanban_db.create_task` (через CLI `hermes kanban create`);
* служебная таблица и триггеры — ДОПОЛНЕНИЕ к схеме Hermes, а не изменение: колонки
  `tasks` не трогаются, триггер срабатывает только когда пин пуст;
* установка — ТОЛЬКО по явному действию человека (`tools/install_model_trigger.py install`
  или кнопка «Установить защиту выбора модели»). Ни старт сервера, ни тик сторожа
  ничего не устанавливают: они только ЧИТАЮТ состояние и показывают тревогу в «Проверке»;
* перед установкой — резервная копия доски через `sqlite3` backup API и проверка схемы
  (`tasks` содержит нужные колонки). Установка идемпотентна (повтор не плодит объектов);
* самолечение: обновление Hermes может пересоздать `tasks` и снести триггеры. `status()`
  и `check_room()` это видят, «Проверка» говорит об этом человеку, переустановка — по
  действию;
* удаление одной командой: `tools/install_model_trigger.py uninstall --db <путь>`
  (снимает триггеры; `--purge` удаляет и служебную таблицу).

Синхронизация таблицы: `rooms.update_room` (любая правка `agent_models`) и
`agent_models.set/clear` вызывают `sync_room()` — идемпотентно, только если защита уже
установлена. Ошибка синхронизации не мешает сохранить выбор в реестре комнаты.

Публично::

    status(db) -> {...}                     # только чтение: установлено/здорово/проблемы
    install(db, rows=None, ...) -> {...}    # явная установка (+ резервная копия)
    uninstall(db, drop_table=False) -> {...}
    sync(db, tenant, rows) -> {...}         # идемпотентная запись таблицы
    rows_of_room(room) -> {профиль: {...}}  # выбор комнаты → строки таблицы
    sync_room(cfg, room, board=None) -> {...}
    check_room(cfg, room=None) -> {...}     # тревога/самолечение для «Проверки»
    diag_item(cfg, room=None) -> {...}      # пункт отчёта диагностики
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

try:                                     # пакетный импорт
    from . import agent_models as models_mod
except ImportError:                      # плоский импорт (python server/app.py)
    import agent_models as models_mod    # type: ignore

#: версия описания объектов (меняется при правке SQL — видно в status)
VERSION = "w29.1"
TABLE = "teamroom_room_models"
META = "teamroom_model_trigger"
TRIGGER_INSERT = "trg_teamroom_model_pin_insert"
TRIGGER_UPDATE = "trg_teamroom_model_pin_update"
TRIGGERS = (TRIGGER_INSERT, TRIGGER_UPDATE)
#: колонки `tasks`, без которых защита бессмысленна (проверяем до установки)
REQUIRED_COLUMNS = ("id", "title", "assignee", "status", "tenant",
                    "model_override", "provider_override", "reasoning_effort")
#: статусы, в которых пин ещё «сработает» при следующем допуске к запуску
PIN_STATUSES = ("triage", "todo", "ready", "running")
TENANT_PREFIX = "room:"
MAX_PROVIDER = 200
MAX_MODEL = 200
MAX_REASONING = 40

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS %(table)s (
    tenant     TEXT NOT NULL,
    assignee   TEXT NOT NULL,
    provider   TEXT,
    model      TEXT NOT NULL,
    reasoning  TEXT,
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, assignee)
)
""" % {"table": TABLE}

_META_SQL = """
CREATE TABLE IF NOT EXISTS %(meta)s (
    key   TEXT PRIMARY KEY,
    value TEXT
)
""" % {"meta": META}

#: Подзапрос «что выбрано этому исполнителю в этой комнате» (модель непустая).
_PICK = ("(SELECT m.%(col)s FROM %(table)s m WHERE m.tenant = NEW.tenant"
         " AND m.assignee = LOWER(TRIM(NEW.assignee)))")

_INSERT_SQL = """
CREATE TRIGGER %(name)s
AFTER INSERT ON tasks
WHEN NEW.model_override IS NULL
 AND NEW.status IN ('triage', 'todo', 'ready', 'running')
 AND NEW.assignee IS NOT NULL AND TRIM(NEW.assignee) <> ''
 AND NEW.tenant LIKE 'room:%%'
 AND EXISTS (SELECT 1 FROM %(table)s m
              WHERE m.tenant = NEW.tenant
                AND m.assignee = LOWER(TRIM(NEW.assignee))
                AND m.model IS NOT NULL AND TRIM(m.model) <> '')
BEGIN
    UPDATE tasks SET
        model_override = %(model)s,
        provider_override = NULLIF(%(provider)s, ''),
        reasoning_effort = CASE
            WHEN NEW.reasoning_effort IS NULL OR TRIM(NEW.reasoning_effort) = ''
            THEN NULLIF(%(reasoning)s, '')
            ELSE NEW.reasoning_effort END
    WHERE id = NEW.id AND model_override IS NULL;
END
"""

_UPDATE_SQL = """
CREATE TRIGGER %(name)s
AFTER UPDATE OF assignee, status ON tasks
WHEN NEW.model_override IS NULL
 AND NEW.status IN ('triage', 'todo', 'ready', 'running')
 AND NEW.assignee IS NOT NULL AND TRIM(NEW.assignee) <> ''
 AND NEW.tenant LIKE 'room:%%'
 AND EXISTS (SELECT 1 FROM %(table)s m
              WHERE m.tenant = NEW.tenant
                AND m.assignee = LOWER(TRIM(NEW.assignee))
                AND m.model IS NOT NULL AND TRIM(m.model) <> '')
BEGIN
    UPDATE tasks SET
        model_override = %(model)s,
        provider_override = NULLIF(%(provider)s, ''),
        reasoning_effort = CASE
            WHEN NEW.reasoning_effort IS NULL OR TRIM(NEW.reasoning_effort) = ''
            THEN NULLIF(%(reasoning)s, '')
            ELSE NEW.reasoning_effort END
    WHERE id = NEW.id AND model_override IS NULL;
END
"""


def _trigger_sql(template: str, name: str) -> str:
    return template % {
        "name": name, "table": TABLE,
        "model": _PICK % {"col": "model", "table": TABLE},
        "provider": _PICK % {"col": "provider", "table": TABLE},
        "reasoning": _PICK % {"col": "reasoning", "table": TABLE},
    }


class TriggerError(Exception):
    """Отказ с кодом для вызывающей стороны (``bad_request``|``unavailable``|``bad_schema``)."""

    def __init__(self, message: str, code: str = "failed", detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = dict(detail or {})


# ---------------------------------------------------------------- соединение и схема

def _path(db) -> Path:
    text = str(db or "").strip().strip('"').strip("'")
    if not text:
        raise TriggerError("Не передан путь к доске канбана", "bad_request", {"field": "db"})
    path = Path(text)
    if not path.is_absolute():
        raise TriggerError("Путь к доске должен быть абсолютным", "bad_request", {"field": "db"})
    return path


def _connect(db: Path, readonly: bool = False, timeout: float = 15.0) -> sqlite3.Connection:
    """Соединение с доской; ``readonly=True`` — только чтение (URI ``mode=ro``)."""
    try:
        if readonly:
            uri = "file:%s?mode=ro" % str(db).replace("\\", "/")
            con = sqlite3.connect(uri, uri=True, timeout=timeout)
        else:
            con = sqlite3.connect(str(db), timeout=timeout)
    except sqlite3.Error as exc:
        raise TriggerError("Доска не открывается: %s" % (str(exc)[:200] or "ошибка"),
                           "unavailable", {"db": str(db)}) from exc
    con.row_factory = sqlite3.Row
    return con


def _objects(con) -> dict:
    """Имена таблиц и триггеров доски."""
    rows = con.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')")
    out: dict = {"table": set(), "trigger": set()}
    for row in rows:
        kind = str(row["type"])
        if kind in out:
            out[kind].add(str(row["name"]))
    return out


def task_schema(con) -> dict:
    """Схема ``tasks``: есть ли нужные колонки (проверка ДО установки)."""
    try:
        columns = [str(row[1]) for row in con.execute("PRAGMA table_info(tasks)")]
    except sqlite3.Error as exc:
        return {"ok": False, "columns": 0, "names": [], "missing": list(REQUIRED_COLUMNS),
                "error": str(exc)[:200]}
    if not columns:
        return {"ok": False, "columns": 0, "names": [], "missing": list(REQUIRED_COLUMNS),
                "error": "таблицы tasks нет"}
    missing = [name for name in REQUIRED_COLUMNS if name not in columns]
    return {"ok": not missing, "columns": len(columns), "names": columns, "missing": missing,
            "error": ""}


def _signature(columns: list) -> str:
    return hashlib.sha1(",".join(sorted(columns)).encode("utf-8")).hexdigest()[:12]


def _meta(con) -> dict:
    try:
        rows = con.execute("SELECT key, value FROM %s" % META).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["key"]): str(row["value"] or "") for row in rows}


# ---------------------------------------------------------------- состояние (только чтение)

def status(db) -> dict:
    """Что установлено в доске: таблица, триггеры, версия, проблемы, число участников.

    Ничего не пишет и не создаёт. Отсутствие доски — ``exists: false``, а не исключение.
    """
    path = _path(db)
    out = {"db": str(path), "exists": False, "installed": False, "healthy": False,
           "table": False, "meta": False, "rows": 0, "triggers": {name: False for name in TRIGGERS},
           "schema": {"ok": False, "columns": 0, "missing": list(REQUIRED_COLUMNS)},
           "version": "", "installed_at": 0, "app_version": "", "signature": "",
           "problems": [], "text": "Доска не найдена — защита недоступна"}
    if not path.is_file():
        out["problems"] = ["Доска не найдена: " + path.name]
        return out
    out["exists"] = True
    con = _connect(path, readonly=True)
    try:
        objects = _objects(con)
        out["table"] = TABLE in objects["table"]
        out["meta"] = META in objects["table"]
        out["schema"] = task_schema(con)
        present = {name: (name in objects["trigger"]) for name in TRIGGERS}
        out["triggers"] = present
        if out["table"]:
            try:
                row = con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()
                out["rows"] = int(row[0] or 0)
            except sqlite3.Error:
                out["rows"] = 0
        meta = _meta(con) if out["meta"] else {}
        out["version"] = str(meta.get("version") or "")
        out["app_version"] = str(meta.get("app_version") or "")
        out["signature"] = str(meta.get("schema_signature") or "")
        try:
            out["installed_at"] = int(meta.get("installed_at") or 0)
        except (TypeError, ValueError):
            out["installed_at"] = 0
    finally:
        con.close()

    problems = []
    if not out["schema"]["ok"]:
        problems.append("Схема доски не та: нет колонок " + ", ".join(out["schema"]["missing"]))
    if not out["table"]:
        problems.append("Служебной таблицы выбора модели нет")
    if not out["meta"]:
        problems.append("Отметки об установке нет")
    missing = [name for name, ok in out["triggers"].items() if not ok]
    if out["table"] and missing:
        problems.append("Триггеры сняты (%.0f из %d): %s"
                        % (len(TRIGGERS) - len(missing), len(TRIGGERS), ", ".join(missing)))
    if out["version"] and out["version"] != VERSION:
        problems.append("Версия описания %s, ожидается %s — переустановите"
                        % (out["version"], VERSION))
    if out["signature"] and out["schema"]["ok"]:
        columns = list(out["schema"].get("names") or [])
        if columns and _signature(columns) != out["signature"]:
            problems.append("Таблица задач пересоздана (обновление Hermes?) — защиту надо "
                            "поставить заново")
    out["installed"] = bool(out["table"] and all(out["triggers"].values()))
    #: следы установки (таблица/отметка/хотя бы один триггер) — «защита была, но сломалась»
    out["present_any"] = bool(out["table"] or out["meta"] or any(out["triggers"].values()))
    out["healthy"] = bool(out["installed"] and out["schema"]["ok"] and not problems)
    out["problems"] = problems
    if out["healthy"]:
        out["text"] = ("Защита выбора модели: установлена · триггеров %d из %d · участников %d"
                       % (len(TRIGGERS), len(TRIGGERS), out["rows"]))
    elif out["installed"]:
        out["text"] = "Защита выбора модели: есть, но с замечаниями"
    else:
        out["text"] = "Защита выбора модели не установлена"
    return out


# ---------------------------------------------------------------- установка и удаление

def _backup(path: Path) -> str:
    """Резервная копия доски штатным ``sqlite3`` backup API (WAL читается корректно)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name("%s.bak-model-trigger-%s" % (path.name, stamp))
    try:
        source = _connect(path, readonly=True)
        try:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
    except (sqlite3.Error, TriggerError) as exc:
        raise TriggerError("Резервная копия доски не сделана: %s"
                           % (str(exc)[:200] or "ошибка"), "unavailable",
                           {"db": str(path)}) from exc
    return str(target)


def _clean_rows(rows) -> dict:
    """Строки таблицы: только полная пара «модель + провайдер», всё остальное — мусор."""
    out: dict = {}
    if not isinstance(rows, dict):
        return out
    for raw_assignee, raw in rows.items():
        assignee = str(raw_assignee or "").strip().lower()
        if not assignee or len(assignee) > 64 or not isinstance(raw, dict):
            continue
        model = str(raw.get("model") or "").strip()[:MAX_MODEL]
        provider = str(raw.get("provider") or "").strip()[:MAX_PROVIDER]
        reasoning = str(raw.get("reasoning") or "").strip().lower()[:MAX_REASONING]
        if not model or not provider or "\x00" in model + provider + reasoning:
            continue
        out[assignee] = {"model": model, "provider": provider, "reasoning": reasoning,
                         "updated_at": _int(raw.get("updated_at"))}
    return out


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def install(db, *, rows=None, tenant: str = "", app_version: str = "",
            backup: bool = True) -> dict:
    """Установить таблицу и триггеры (идемпотентно). Только по явному вызову человека.

    ``rows`` — что положить в таблицу сразу (``sync()`` тоже сделает это позже).
    Возвращает ``status()`` + ``backup`` (имя файла копии, без каталога).
    """
    path = _path(db)
    if not path.is_file():
        raise TriggerError("Доска канбана не найдена", "unavailable", {"db": str(path)})
    copy = _backup(path) if backup else ""
    con = _connect(path)
    try:
        con.row_factory = sqlite3.Row
        schema = task_schema(con)
        if not schema["ok"]:
            raise TriggerError("В доске нет нужных колонок задач: " + ", ".join(schema["missing"]),
                               "bad_schema", {"missing": schema["missing"]})
        columns = [str(row[1]) for row in con.execute("PRAGMA table_info(tasks)")]
        created: list = []
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(_TABLE_SQL)
            con.execute(_META_SQL)
            for name, template in ((TRIGGER_INSERT, _INSERT_SQL), (TRIGGER_UPDATE, _UPDATE_SQL)):
                existed = con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                                      "AND name = ?", (name,)).fetchone()
                con.execute("DROP TRIGGER IF EXISTS %s" % name)
                con.execute(_trigger_sql(template, name))
                created.append("replaced:" + name if existed else name)
            now = int(time.time())
            for key, value in (("version", VERSION), ("installed_at", str(now)),
                               ("app_version", str(app_version or "")),
                               ("schema_signature", _signature(columns))):
                con.execute("INSERT INTO %s (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value" % META,
                            (key, value))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        synced = {"synced": 0, "removed": 0}
        if rows:
            synced = _sync_con(con, tenant or "", rows)
    finally:
        con.close()
    out = status(path)
    out.update({"ok": bool(out.get("healthy")), "backup": Path(copy).name if copy else "",
                "backup_path": copy, "created": created, "rows_written": synced.get("synced", 0)})
    return out


def uninstall(db, *, drop_table: bool = False) -> dict:
    """Снять триггеры одной командой. ``drop_table=True`` — удалить и служебную таблицу.

    Пины, уже стоящие на карточках, не снимаются: это выбор человека, а не мусор.
    """
    path = _path(db)
    if not path.is_file():
        raise TriggerError("Доска канбана не найдена", "unavailable", {"db": str(path)})
    con = _connect(path)
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            dropped = []
            for name in TRIGGERS:
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                                     "AND name = ?", (name,)).fetchone()
                con.execute("DROP TRIGGER IF EXISTS %s" % name)
                if exists:
                    dropped.append(name)
            con.execute("DROP TABLE IF EXISTS %s" % META)
            if drop_table:
                con.execute("DROP TABLE IF EXISTS %s" % TABLE)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    out = status(path)
    out.update({"ok": True, "dropped": dropped, "purged": bool(drop_table)})
    return out


# ---------------------------------------------------------------- синхронизация таблицы

def _sync_con(con, tenant: str, rows) -> dict:
    clean = _clean_rows(rows)
    stamp = int(time.time())
    written = 0
    removed = 0
    con.isolation_level = None
    con.execute("BEGIN IMMEDIATE")
    try:
        for assignee, item in clean.items():
            con.execute(
                "INSERT INTO %s (tenant, assignee, provider, model, reasoning, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tenant, assignee) DO UPDATE SET "
                "provider = excluded.provider, model = excluded.model, "
                "reasoning = excluded.reasoning, updated_at = excluded.updated_at" % TABLE,
                (str(tenant or ""), assignee, item["provider"], item["model"],
                 item["reasoning"] or "", stamp if not item["updated_at"] else item["updated_at"]))
            written += 1
        if clean:
            marks = ",".join("?" for _ in clean)
            cursor = con.execute("DELETE FROM %s WHERE tenant = ? AND assignee NOT IN (%s)"
                                 % (TABLE, marks), (str(tenant or ""),) + tuple(clean))
        else:
            cursor = con.execute("DELETE FROM %s WHERE tenant = ?" % TABLE, (str(tenant or ""),))
        removed = int(cursor.rowcount or 0)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"synced": written, "removed": removed}


def sync(db, tenant: str, rows) -> dict:
    """Идемпотентно записать выбор комнаты в таблицу. Защита не установлена — пропуск."""
    path = _path(db)
    state = status(path)
    if not state.get("installed"):
        return {"synced": 0, "removed": 0, "skipped": "not_installed",
                "text": "Защита выбора модели не установлена — таблица не ведётся"}
    con = _connect(path)
    try:
        result = _sync_con(con, tenant, rows)
    finally:
        con.close()
    result["skipped"] = ""
    return result


def rows_of_room(room) -> dict:
    """Выбор модели комнаты (реестр реестров) → строки таблицы: только полные пары."""
    out: dict = {}
    try:
        settings = models_mod.settings_of(room if isinstance(room, dict) else {}) or {}
    except Exception:                                     # noqa: BLE001 - реестр битый
        return out
    for profile, item in settings.items():
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        provider = str(item.get("provider") or "").strip()
        if not model or not provider:
            continue
        out[str(profile).strip().lower()] = {"model": model, "provider": provider,
                                             "reasoning": str(item.get("reasoning") or ""),
                                             "updated_at": _int(item.get("updated_at"))}
    return out


def _tenant_of(room) -> str:
    return str((room or {}).get("tenant") or TENANT_PREFIX + str((room or {}).get("id") or "")).strip()


def _db_of(cfg, room, board=None) -> Path | None:
    name = str(board or (room or {}).get("board") or "").strip()
    try:
        return Path(cfg.kanban_db(name))
    except Exception:                                     # noqa: BLE001 - нет доски — нет синхронизации
        return None


def sync_room(cfg, room, *, board: str | None = None) -> dict:
    """Выбор комнаты → таблица доски. Ошибки не выходят наружу (выбор уже сохранён)."""
    if not isinstance(room, dict):
        return {"synced": 0, "skipped": "no_room"}
    db = _db_of(cfg, room, board)
    if db is None or not db.is_file():
        return {"synced": 0, "skipped": "no_db"}
    try:
        return sync(db, _tenant_of(room), rows_of_room(room))
    except Exception as exc:                              # noqa: BLE001 - вторично
        return {"synced": 0, "skipped": "error", "error": str(exc)[:200]}


# ---------------------------------------------------------------- проверка и самолечение

def check_room(cfg, room=None, *, rooms=None) -> dict:
    """Состояние защиты для комнаты (или по всем комнатам): ``{ok, rooms, problems}``."""
    items: list = []
    if isinstance(room, dict):
        rooms = [room]
    if rooms is None:
        try:
            from . import rooms as rooms_mod       # noqa: PLC0415 - без циклов
        except ImportError:
            import rooms as rooms_mod              # type: ignore
        try:
            rooms = rooms_mod.list_rooms(cfg) or []
        except Exception:                          # noqa: BLE001
            rooms = []
    for item in rooms or []:
        if not isinstance(item, dict):
            continue
        db = _db_of(cfg, item)
        if db is None or not db.is_file():
            continue
        try:
            state = status(db)
        except TriggerError as exc:
            state = {"installed": False, "healthy": False, "text": exc.message,
                     "problems": [exc.message], "rows": 0, "db": str(db)}
        state["room"] = str(item.get("id") or "")
        state["needed"] = bool(rows_of_room(item))
        items.append(state)
    problems = [text for state in items for text in (state.get("problems") or [])]
    return {"ok": all(not state.get("problems") for state in items),
            "installed": any(state.get("installed") for state in items),
            "rooms": items, "problems": problems}


def diag_item(cfg, room=None) -> dict:
    """Пункт «Проверка системы»: защита выбора модели (только чтение, без установки).

    * защита есть и здорова → ``ok``;
    * защита была, но триггеров нет (обновление Hermes) → ``warn`` с действием;
    * защита не стоит → ``off`` и предложение установить, если в комнате есть выбор модели.
    """
    try:
        data = check_room(cfg, room)
    except Exception as exc:                              # noqa: BLE001 - проверка не падает
        return {"id": "model_trigger", "title": "Защита выбора модели", "status": "warn",
                "value": "Проверка недоступна", "hint": "Модуль защиты не загрузился",
                "details": [str(exc)[:200]]}
    rooms = data.get("rooms") or []
    if not rooms:
        return {"id": "model_trigger", "title": "Защита выбора модели", "status": "off",
                "value": "доска не найдена", "hint": "Проверьте доску комнаты.", "details": []}
    installed = [item for item in rooms if item.get("installed")]
    broken = [item for item in rooms
              if item.get("present_any") and (item.get("problems") or not item.get("installed"))]
    needed = [item for item in rooms if item.get("needed")]
    details = ["%s: %s" % (item.get("room") or "комната", item.get("text") or "")
               for item in rooms[:5]]
    if installed and not broken:
        return {"id": "model_trigger", "title": "Защита выбора модели", "status": "ok",
                "value": "установлена · участников %d" % sum(int(item.get("rows") or 0)
                                                             for item in installed),
                "hint": "Пин модели ставится в момент создания карточки, гонки нет.",
                "details": details}
    if broken:
        return {"id": "model_trigger", "title": "Защита выбора модели", "status": "warn",
                "value": "триггеры сняты",
                "hint": "Похоже, обновление Hermes пересоздало доску. Переустановите защиту "
                        "кнопкой «Установить защиту выбора модели» в настройках комнаты.",
                "details": details + list(data.get("problems") or [])[:4]}
    if needed:
        return {"id": "model_trigger", "title": "Защита выбора модели", "status": "off",
                "value": "не установлена",
                "hint": "В комнате выбраны модели участников. Без защиты карточка может уйти "
                        "на модель профиля, пока пин догоняет. Установите её кнопкой в "
                        "настройках комнаты → «Проверка».",
                "details": details}
    return {"id": "model_trigger", "title": "Защита выбора модели", "status": "off",
            "value": "не нужна", "hint": "Модели участников не переопределены — защита не нужна.",
            "details": details}


__all__ = ["TriggerError", "META", "PIN_STATUSES", "REQUIRED_COLUMNS", "TABLE", "TRIGGERS",
           "TRIGGER_INSERT", "TRIGGER_UPDATE", "VERSION", "check_room", "diag_item", "install",
           "rows_of_room", "status", "sync", "sync_room", "task_schema", "uninstall"]
