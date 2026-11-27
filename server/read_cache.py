"""Кэш чтения комнаты по ОТПЕЧАТКУ доски (W31-A1, дефекты 17 и 20).

Зачем: доска задач и список задач комнаты каждый раз заново собирают сводку и
пункты «ждёт вас» по всей задаче — на большой комнате это секунды, и человек
видит серые заглушки при каждом переключении. Правильный ответ — не таймаут, а
кэш, который отдаёт ПРЕЖНИЙ ответ, пока входы не изменились, и сам сбрасывается,
как только доска или файлы состояния комнаты поменялись.

Отпечаток (``fingerprint``) — это свёртка всего, из чего складывается ответ:

* доска: ``MAX(id)`` событий (кроме ``heartbeat``: он ничего не меняет на экране),
  комментариев и прогонов + содержимое карточек комнаты (статус, исполнитель,
  времена, переопределения модели, блокировка);
* файлы состояния комнаты (``data/state/<room>/``) — реестр задач, ответы,
  отметка принятия, связи подзадач — по ``mtime_ns`` и размеру;
* ``data/rooms.json`` — состав комнаты, руководитель, критик (могут меняться).

Чего отпечаток НЕ ловит — медленно меняющиеся источники в профилях агентов
(разрешения из ``state.db``). Для них есть потолок жизни записи
(``TTL_CEILING``): запись старше него не отдаётся, даже если отпечаток совпал.
Это компромисс назван осознанно: разрешение приходит в ту же карточку
комментарием/событием, то есть обычно попадает в отпечаток, а страховка не даёт
залипнуть счётчику «ждёт вас» дольше полуминуты.

Публичные функции::

    fingerprint(cfg, room, records=None, board=None) -> str
    lookup(kind, room_id, mark) -> dict | None
    store(kind, room_id, mark, payload) -> None
    invalidate(room_id=None) -> int
    stats() -> dict
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from . import statefiles as _sf
except ImportError:  # pragma: no cover
    import statefiles as _sf        # type: ignore

#: Потолок жизни записи: страховка от источников, которых нет в отпечатке
#: (разрешения в профилях агентов). Не «кэш на 30 секунд», а предел: отпечаток
#: сбрасывает запись раньше.
TTL_CEILING = 30.0
#: Сколько ответов держим одновременно (комнат на машине единицы, ×2 вида ответа)
MAX_ENTRIES = 24
#: События, которые не меняют ничего из показанного на доске.
QUIET_EVENT_KINDS = ("heartbeat", "worker_bound")
#: Колонки карточки, из которых складывается ответ (статус и модель важнее текста).
CARD_COLUMNS = ("id", "status", "assignee", "started_at", "completed_at",
                "model_override", "provider_override", "reasoning_effort",
                "block_kind", "consecutive_failures", "current_run_id", "session_id")

_STORE: dict = {}
_LOCK = threading.Lock()
_CONNS = threading.local()
_STATS = {"hits": 0, "misses": 0, "stores": 0, "invalidations": 0, "errors": 0}


# ---------------------------------------------------------------- отпечаток
def _board_conn(cfg, board: str):
    """Открытое read-only соединение с доской (на поток); None — базы нет."""
    try:
        db = Path(cfg.kanban_db(board))
    except Exception:                       # noqa: BLE001 - конфиг без доски
        return None
    name = str(db)
    cache = getattr(_CONNS, "by_path", None)
    if not isinstance(cache, dict):
        cache = {}
        _CONNS.by_path = cache
    con = cache.get(name)
    if con is not None:
        return con
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.resolve().as_posix(),
                              uri=True, timeout=2, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.isolation_level = None
        con.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        return None
    if len(cache) >= 8:
        for key in [k for k in cache if k != name][:4]:
            try:
                cache.pop(key).close()
            except Exception:               # noqa: BLE001
                cache.pop(key, None)
    cache[name] = con
    return con


def _quiet_ids_sql(column: str) -> str:
    return ("SELECT COALESCE(MAX(%s), 0) FROM task_events WHERE kind NOT IN (%s)"
            % (column, ", ".join("'%s'" % k for k in QUIET_EVENT_KINDS)))


def board_mark(cfg, board: str, card_ids=None) -> dict:
    """``{events, comments, runs, cards}`` — «дыхание» доски, без содержимого текстов.

    ``events`` — последнее событие, которое МЕНЯЕТ экран (heartbeat и привязка
    воркера пропускаются: они случаются каждые полминуты и не меняют ни статус,
    ни число пунктов). ``cards`` — свёртка карточек комнаты: статус и
    переопределения меняются без нового события (например, `set-model`).
    """
    mark = {"events": 0, "comments": 0, "runs": 0, "cards": ""}
    con = _board_conn(cfg, board)
    if con is None:
        return mark
    try:
        row = con.execute(
            "SELECT (%s), (SELECT COALESCE(MAX(id), 0) FROM task_comments), "
            "(SELECT COALESCE(MAX(id), 0) FROM task_runs)" % _quiet_ids_sql("id")).fetchone()
        mark["events"], mark["comments"], mark["runs"] = (int(row[0] or 0), int(row[1] or 0),
                                                          int(row[2] or 0))
    except sqlite3.Error:
        _STATS["errors"] += 1
        return mark
    ids = sorted({str(c) for c in (card_ids or []) if c})
    rows = []
    for start in range(0, len(ids), 400):           # SQLite: не больше ~999 параметров
        chunk = ids[start:start + 400]
        try:
            rows.extend(con.execute(
                "SELECT %s FROM tasks WHERE id IN (%s)"
                % (", ".join(CARD_COLUMNS), ", ".join("?" for _ in chunk)),
                tuple(chunk)).fetchall())
        except sqlite3.Error:
            _STATS["errors"] += 1
            return mark
    mark["cards"] = hashlib.sha1(
        json.dumps([list(r) for r in rows], ensure_ascii=False, sort_keys=True)
        .encode("utf-8")).hexdigest()[:16]
    return mark


_VOLATILE_FILES = frozenset({"model_guard.json", "worklog_digest.json"})
_VOLATILE_DIRS = frozenset({"watchdog"})


def _files_mark(cfg, room_id: str) -> list:
    """``[(имя, mtime_ns, размер)]`` по файлам состояния комнаты — без чтения тел."""
    out: list = []
    base = Path(getattr(cfg, "data_dir", "") or ".")
    root = Path(getattr(cfg, "state_dir", base / "state")) / str(room_id)
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            # Служебные файлы сторожа переписываются каждый тик (≈3 с) теми же данными:
            # mtime у них не сигнал изменения, иначе кэш на живой доске не срабатывает
            # никогда (замер 27.11: 0,5 с на каждый запрос вместо 0,01 с). Для них — размер.
            if rel in _VOLATILE_FILES or rel.split("/", 1)[0] in _VOLATILE_DIRS:
                out.append((rel, 0, info.st_size))
            else:
                out.append((rel, info.st_mtime_ns, info.st_size))
    except OSError:
        pass
    rooms = base / "rooms.json"
    try:
        info = rooms.stat()
        out.append(("rooms.json", info.st_mtime_ns, info.st_size))
    except OSError:
        pass
    return out


def fingerprint(cfg, room, records=None, board: str | None = None) -> str:
    """Отпечаток входов ответа: доска + файлы состояния комнаты + состав комнаты."""
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    board = board or str(room.get("board") or "")
    card_ids = [str((rec or {}).get("root_id") or "") for rec in (records or [])
                if isinstance(rec, dict)]
    for rec in records or []:
        for extra in (rec or {}).get("extra_cards") or []:
            if isinstance(extra, dict) and extra.get("id"):
                card_ids.append(str(extra["id"]))
    payload = {
        "board": board,
        "mark": board_mark(cfg, board, card_ids),
        "files": _files_mark(cfg, room_id),
        "room": [str(room.get("title") or ""), str(room.get("orchestrator") or ""),
                 str(room.get("critic") or ""), str(room.get("tenant") or ""),
                 len(room.get("members") or [])],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- хранилище
def lookup(kind: str, room_id: str, mark: str):
    """Готовый ответ, если отпечаток совпал и запись не старше ``TTL_CEILING``."""
    key = (str(kind), str(room_id))
    with _LOCK:
        hit = _STORE.get(key)
    if not hit:
        _STATS["misses"] += 1
        return None
    at, seen, payload = hit
    if seen != mark or time.time() - float(at) > TTL_CEILING:
        _STATS["misses"] += 1
        return None
    _STATS["hits"] += 1
    return payload


def store(kind: str, room_id: str, mark: str, payload) -> None:
    """Положить готовый ответ под отпечаток (старые записи вытесняются)."""
    key = (str(kind), str(room_id))
    with _LOCK:
        while len(_STORE) >= MAX_ENTRIES and key not in _STORE:
            try:
                _STORE.pop(next(iter(_STORE)))
            except (StopIteration, KeyError):   # pragma: no cover - гонка потоков
                break
        _STORE[key] = (time.time(), str(mark), payload)
    _STATS["stores"] += 1


def invalidate(room_id: str | None = None) -> int:
    """Сбросить кэш комнаты (или весь). Возвращает число снятых записей."""
    with _LOCK:
        if room_id is None:
            count = len(_STORE)
            _STORE.clear()
        else:
            count = 0
            for key in [k for k in _STORE if k[1] == str(room_id)]:
                _STORE.pop(key, None)
                count += 1
    _STATS["invalidations"] += count
    return count


def stats() -> dict:
    """Счётчики кэша для отчёта и `/api/health`."""
    with _LOCK:
        size = len(_STORE)
    return dict(_STATS, size=size, ttl_ceiling=TTL_CEILING, max_entries=MAX_ENTRIES)


__all__ = ["TTL_CEILING", "MAX_ENTRIES", "QUIET_EVENT_KINDS", "fingerprint", "board_mark",
           "lookup", "store", "invalidate", "stats"]
