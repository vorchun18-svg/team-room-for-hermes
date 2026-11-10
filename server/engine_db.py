"""Чтение канбана: константы, схема доски и низкоуровневые SQLite-помощники (только `mode=ro`).

Листовой модуль: `engine_tree`, `engine_health` и `engine_read` берут отсюда соединение,
проверку схемы и выборки строк задач/запусков/комментариев.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ACTIVE = ("ready", "running", "review")
CLOSED = ("done", "archived")
FAILED_STATUS = ("failed", "crashed")
MAX_NODES, MAX_DEPTH = 400, 6
# Версия раскладки доски, которую умеет читать приложение (tasks/links/runs/comments/events
# + tenant/current_run_id/block_kind). В самой БД номера нет (`PRAGMA user_version = 0`),
# поэтому наружу уходит версия поддерживаемой схемы: fixtures/health.json ждёт int,
# docs/API.md §3.1 допускает null — выбран int (см. docs/status/conformance.md).
SCHEMA_VERSION = 3
REQUIRED_TABLES = ("tasks", "task_runs", "task_comments", "task_events", "task_links")
REQUIRED_COLUMNS = {
    "tasks": ("id", "title", "status", "assignee", "created_at"),
    "task_runs": ("task_id", "status", "outcome", "summary"),
    "task_comments": ("task_id", "author", "body", "created_at"),
    "task_events": ("task_id", "kind", "payload", "created_at"),
    "task_links": ("parent_id", "child_id"),
}
OPTIONAL_COLUMNS = {
    "tasks": ("body", "tenant", "created_by", "started_at", "completed_at", "idempotency_key",
              "last_failure_error", "consecutive_failures", "max_retries", "workspace_path",
              "block_kind", "priority", "current_run_id"),
    "task_runs": ("id", "profile", "started_at", "ended_at", "metadata", "error"),
}
TASK_FIELDS = ("id", "title", "body", "assignee", "status", "priority", "created_at", "started_at",
               "completed_at", "tenant", "created_by", "idempotency_key", "last_failure_error",
               "consecutive_failures", "max_retries", "workspace_path", "block_kind",
               # WAVE10: модель/провайдер/мышление карточки — для подписи model_label в дереве
               "model_override", "provider_override", "reasoning_effort")


RUN_FIELDS = ("id", "task_id", "profile", "status", "outcome", "started_at", "ended_at",
              "summary", "metadata", "error")


EVENT_TEXT = {
    "created": "Карточка создана", "decomposed": "Разложено на подзадачи", "promoted": "Готова к работе",
    "claimed": "Взята в работу", "started": "Работа началась", "commented": "Комментарий",
    "completed": "Завершена", "blocked": "Заблокирована", "unblocked": "Разблокирована",
    "failed": "Ошибка", "archived": "В архив", "spawned": "Запущен воркер",
    "reclaimed": "Перехвачена", "review_requested": "Отправлена на проверку",
    "scheduled": "Отложена", "heartbeat": "Признак жизни",
}


class EngineError(Exception):
    """Ошибка чтения движка: ``code`` машинный, ``message`` — по-русски."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


# ---------------------------------------------------------------- соединение ---
BUSY_TIMEOUT_MS = 3000        # D6: не ждём блокировку дольше 3 с

_DEADLINE = threading.local()
_BUSY_WORDS = ("locked", "busy", "lock", "блокиров")


@contextmanager
def deadline(seconds: float):
    """Бюджет времени на весь блок чтения (health, тик watcher): D6.

    Все соединения внутри блока берут ``busy_timeout`` не больше остатка бюджета,
    поэтому блокировка БД не растягивает ответ на десятки секунд.
    """
    previous = getattr(_DEADLINE, "ts", None)
    _DEADLINE.ts = time.monotonic() + float(seconds)
    try:
        yield
    finally:
        _DEADLINE.ts = previous


def remaining_ms(default_ms: int = BUSY_TIMEOUT_MS) -> int:
    """Сколько ещё можно ждать в текущем бюджете (0 — бюджет исчерпан)."""
    ts = getattr(_DEADLINE, "ts", None)
    if ts is None:
        return int(default_ms)
    left = int((ts - time.monotonic()) * 1000)
    return max(0, min(int(default_ms), left))


def is_busy(exc: BaseException) -> bool:
    """Похоже ли на «база занята» (а не на «схема изменилась»)."""
    text = str(exc).lower()
    return any(word in text for word in _BUSY_WORDS)


def _busy_error(exc: BaseException, board: str) -> EngineError:
    return EngineError("db_busy",
                       "База канбана занята другим процессом — данные временно недоступны. "
                       "Повторите через пару секунд.",
                       {"board": board, "error": str(exc)[:300]})


@contextmanager
def _conn(cfg, board: str):
    """Открыть БД доски только на чтение; закрывает соединение всегда."""
    db = Path(cfg.kanban_db(board))
    if not db.is_file():
        raise EngineError("no_db", f"Доска «{board}» не найдена: нет файла {db}. "
                                   "Создайте доску в Hermes или проверьте настройки.",
                          {"board": board, "db": str(db)})
    budget = remaining_ms()
    if budget <= 0:
        raise EngineError("db_busy", "База канбана не ответила вовремя — попробуйте ещё раз.",
                          {"board": board})
    uri = "file:" + db.resolve().as_posix() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=budget / 1000.0)
    except sqlite3.Error as exc:
        if is_busy(exc):
            raise _busy_error(exc, board) from exc
        raise EngineError("db_unreadable", f"Не удалось открыть БД доски «{board}»: {exc}",
                          {"board": board, "db": str(db)}) from exc
    con.row_factory = sqlite3.Row
    try:
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA busy_timeout=%d" % budget)
        except sqlite3.Error:
            pass
        yield con
    finally:
        con.close()


def _cols(con, table: str) -> set:
    """Колонки таблицы. Ошибку SQLite не выдаём за «пустую схему» (D6).

    ``database is locked`` → ``db_busy`` (503), прочие сбои → ``db_error``:
    иначе ``_require_tables`` рапортует ложный ``schema_drift``.
    """
    try:
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as exc:
        if is_busy(exc):
            raise EngineError("db_busy",
                              "База канбана занята другим процессом — схема временно недоступна. "
                              "Повторите через пару секунд.",
                              {"table": table, "error": str(exc)[:300]}) from exc
        raise EngineError("db_error", f"Не удалось прочитать схему БД (таблица «{table}»): {exc}",
                          {"table": table, "error": str(exc)[:300]}) from exc


def _select(fields, have: set) -> str:
    """SELECT-список с NULL-заглушками отсутствующих колонок (дрейф схемы не роняет чтение)."""
    return ", ".join(f if f in have else f"NULL AS {f}" for f in fields)


def _schema_missing(con) -> list:
    missing = []
    for table in REQUIRED_TABLES:
        have = _cols(con, table)
        if not have:
            missing.append(f"table:{table}")
            continue
        for col in REQUIRED_COLUMNS.get(table, ()) + OPTIONAL_COLUMNS.get(table, ()):
            if col not in have:
                missing.append(f"column:{table}.{col}")
    return missing


def _require_tables(con, *tables):
    for table in tables:
        if not _cols(con, table):
            raise EngineError("schema_drift", f"В базе канбана нет таблицы «{table}»: схема движка "
                                              "изменилась. Показываю диагностику вместо данных.",
                              {"missing": f"table:{table}"})


def _chunks(seq, size: int = 400):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _json(text):
    if not text:
        return {}
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(str(text), strict=False)
    except (ValueError, TypeError):
        return {}


def _task_rows(con, ids) -> dict:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    sel = _select(TASK_FIELDS, _cols(con, "tasks"))
    out = {}
    for chunk in _chunks(ids):
        for row in con.execute(f"SELECT {sel} FROM tasks WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out[row["id"]] = dict(row)
    return out


def _run_rows(con, ids) -> dict:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    sel = _select(RUN_FIELDS, _cols(con, "task_runs"))
    out = {}
    for chunk in _chunks(ids):
        q = (f"SELECT {sel} FROM task_runs WHERE task_id IN ({','.join('?' * len(chunk))}) "
             "ORDER BY COALESCE(ended_at, started_at, 0), id")
        for row in con.execute(q, chunk):
            out.setdefault(row["task_id"], []).append(dict(row))
    return out


def _comment_rows(con, ids) -> dict:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    out = {}
    for chunk in _chunks(ids):
        q = (f"SELECT task_id, author, body, created_at FROM task_comments "
             f"WHERE task_id IN ({','.join('?' * len(chunk))}) ORDER BY created_at, id")
        for row in con.execute(q, chunk):
            out.setdefault(row["task_id"], []).append(
                {"author": row["author"] or "system", "text": row["body"] or "", "at": row["created_at"]})
    return out
