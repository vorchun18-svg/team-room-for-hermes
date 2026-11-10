"""Защитные проверки записи в канбан: id/profile/author, дерево запроса, диспетчер.

Вынесено из ``engine_write`` (D1/D2 из docs/critic/wave4-backend.md §2.1–2.2), чтобы
файл записи не разрастался: здесь только проверки значений, которые уходят в argv
CLI, и проход диспетчера (частота, ``--max``, dry-run, чужие карточки).

Импорт ``engine_write`` — ленивый (внутри функций): модуль не участвует в циклах.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from .engine_cli import EngineWriteError, _run_json, opt
except ImportError:  # pragma: no cover
    from engine_cli import EngineWriteError, _run_json, opt  # type: ignore

# D1: единственно допустимая форма id карточки
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
# имя профиля: без ведущего дефиса, без пробелов и служебных символов
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}$")
AUTHOR_MAXIM = "mikhail"
UNASSIGN_VALUES = ("none", "unassign")

# D2: проход диспетчера
DISPATCH_MAX_DEFAULT = 3
DISPATCH_MAX_LIMIT = 5
DISPATCH_COOLDOWN_S = 30
TENANT_PREFIX = "room:"

_DISPATCH_LOCK = threading.Lock()
_DISPATCH_LAST: dict[str, float] = {}


def _ew():
    """Модуль engine_write (ленивый импорт, чтобы не было цикла)."""
    try:
        from . import engine_write as mod
    except ImportError:  # pragma: no cover
        import engine_write as mod  # type: ignore
    return mod


# ---------------------------------------------------------------- значения
def no_nul(value, field: str = "значение") -> str:
    """Строка без NUL: иначе 400 ещё до сборки argv (D12)."""
    text = "" if value is None else str(value)
    if "\x00" in text:
        raise EngineWriteError("bad_request", f"В поле {field} есть NUL-байт.",
                               detail={"field": field})
    return text


def assert_task_id(value, field: str = "task_id") -> str:
    """`^t_[0-9a-f]{8}$` либо bad_request (D1: `--rm=…`, `--help`, `../` — отказ)."""
    text = no_nul(value, field).strip()
    if not TASK_ID_RE.fullmatch(text):
        raise EngineWriteError("bad_request",
                               "Некорректный id карточки: ожидается t_ и 8 hex-символов.",
                               detail={"field": field, "value": text[:80]})
    return text


def tree_ids(cfg, board: str, root_id: str, limit: int = 400) -> set[str]:
    """Все id дерева запроса в обе стороны (parent_id / task_links), BFS по SQL."""
    root = str(root_id or "")
    ids: set[str] = {root} if root else set()
    if not root:
        return ids
    db = Path(cfg.kanban_db(board))
    if not db.exists():
        return ids
    frontier = [root]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return ids
    try:
        for _depth in range(12):
            if not frontier or len(ids) >= limit:
                break
            marks = ",".join("?" * len(frontier))
            # связи в обе стороны: decompose пишет child_id=root, ручные ссылки — parent_id=root;
            # колонки tasks.parent_id в схеме движка НЕТ (только task_links)
            sql = ("SELECT child_id FROM task_links WHERE parent_id IN (%s) "
                   "UNION SELECT parent_id FROM task_links WHERE child_id IN (%s)") % (marks, marks)
            params = tuple(frontier) * 2
            try:
                rows = con.execute(sql, params).fetchall()
            except sqlite3.Error:
                break
            found = {str(r[0]) for r in rows if r and r[0]}
            new = sorted(found - ids)
            ids |= found
            frontier = new[:limit]
    finally:
        con.close()
    ids.discard("")
    return ids


def assert_owned(cfg, board: str, root_id: str, task_id, extra_ids=None) -> str:
    """task_id обязан входить в дерево ИМЕННО этого запроса (иначе not_found)."""
    text = assert_task_id(task_id)
    allowed = tree_ids(cfg, board, root_id)
    for item in extra_ids or []:
        if isinstance(item, dict):
            value = item.get("id") or item.get("task_id")
        else:
            value = item
        if value:
            allowed.add(str(value))
    if text not in allowed:
        raise EngineWriteError("not_found", "Карточка не принадлежит этому запросу.",
                               detail={"reason": "foreign_task", "task_id": text,
                                       "rid": str(root_id)})
    return text


def profile_names(cfg) -> dict:
    """{имя: True} из ростера Hermes (файлы профилей, без subprocess)."""
    try:
        items = _ew().list_profiles(cfg) or []
    except Exception:  # noqa: BLE001 - ростер недоступен → проверяем только форму
        return {}
    out = {}
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = True
    return out


def assert_profile(cfg, profile, *, allow_unassign: bool = True, field: str = "profile") -> str:
    """Профиль — из ростера Hermes; `none`/`unassign` — только как «снять исполнителя»."""
    text = no_nul(profile, field).strip()
    if allow_unassign and text.lower() in UNASSIGN_VALUES:
        return "none"
    if not PROFILE_RE.fullmatch(text):
        raise EngineWriteError("bad_request", "Некорректное имя профиля.",
                               detail={"field": field, "profile": text[:80],
                                       "available": sorted(profile_names(cfg))})
    known = profile_names(cfg)
    if known and text not in known:
        raise EngineWriteError("bad_request", "Профиля нет в ростере Hermes.",
                               detail={"field": field, "profile": text,
                                       "available": sorted(known)})
    return text


def assert_author(cfg, author, extra_names=None) -> str:
    """Автор комментария — только `mikhail` или профиль из ростера/комнаты."""
    text = no_nul(author, "author").strip() or AUTHOR_MAXIM
    allowed = {AUTHOR_MAXIM} | set(profile_names(cfg))
    allowed |= {str(name) for name in (extra_names or []) if name}
    if text not in allowed:
        raise EngineWriteError("bad_request", "Автор не из комнаты.",
                               detail={"field": "author", "author": text[:80],
                                       "available": sorted(allowed)})
    return text


# ---------------------------------------------------------------- диспетчер
def dispatch_max(cfg, override=None) -> int:
    """Предел карточек за проход: cfg.dispatch_max → TEAMROOM_DISPATCH_MAX → 3, обрезка 1..5."""
    raw = override
    if raw in (None, ""):
        raw = getattr(cfg, "dispatch_max", None) or os.environ.get("TEAMROOM_DISPATCH_MAX")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DISPATCH_MAX_DEFAULT
    return max(1, min(DISPATCH_MAX_LIMIT, value))


def dispatch_guard(cfg, board: str, *, now: float | None = None) -> dict | None:
    """Причина отказа или None: песочница (409) и частота (429)."""
    if getattr(cfg, "sandbox", False):
        return {"status": 409, "code": "sandbox_no_dispatch",
                "message": "В песочнице диспетчер не запускается: агенты работают на реальных "
                           "профилях и ключах. Запуск — только на боевом каталоге Hermes.",
                "detail": {"reason": "sandbox_no_dispatch"}}
    stamp = time.time() if now is None else float(now)
    with _DISPATCH_LOCK:
        last = _DISPATCH_LAST.get(str(board))
    if last is not None:
        age = stamp - last
        if age < DISPATCH_COOLDOWN_S:
            wait = max(1, int(math.ceil(DISPATCH_COOLDOWN_S - age)))
            return {"status": 429, "code": "rate_limited",
                    "message": "Диспетчер уже запускали %d с назад — не чаще раза в %d секунд."
                               % (max(0, int(age)), DISPATCH_COOLDOWN_S),
                    "detail": {"reason": "rate_limited", "retry_after": wait,
                               "cooldown_s": DISPATCH_COOLDOWN_S}}
    return None


def reset_dispatch_guard() -> None:
    """Сбросить отметку частоты (тесты и ручной сброс после смены доски)."""
    with _DISPATCH_LOCK:
        _DISPATCH_LAST.clear()


def _mark_dispatch(board: str, now: float | None = None) -> None:
    with _DISPATCH_LOCK:
        _DISPATCH_LAST[str(board)] = time.time() if now is None else float(now)


def _planned(cfg, board: str, data: dict) -> list[dict]:
    """Карточки, которые диспетчер запустит: id/assignee/tenant/foreign."""
    raw = []
    if isinstance(data, dict):
        for key in ("spawned", "spawned_cards", "will_spawn"):
            value = data.get(key)
            if isinstance(value, list):
                raw = value
                break
    row_of = _ew()._task_row
    out = []
    for item in raw:
        if isinstance(item, dict):
            task_id = item.get("id") or item.get("task_id") or ""
            assignee = item.get("assignee") or item.get("profile")
            tenant = item.get("tenant")
        else:
            task_id, assignee, tenant = str(item), None, None
        task_id = str(task_id or "")
        row = row_of(cfg, board, task_id) if task_id else None
        row = row or {}
        tenant = str(tenant or row.get("tenant") or "")
        out.append({"id": task_id, "assignee": assignee or row.get("assignee"),
                    "tenant": tenant, "foreign": not tenant.startswith(TENANT_PREFIX)})
    return out


def dispatch_board(cfg, board: str, *, max_cards=None, confirm: bool = False,
                   now: float | None = None) -> dict:
    """Проход диспетчера: отказ в песочнице, частота, dry-run, `--max`, чужие карточки.

    Dry-run обязателен: без него нельзя ни показать «кого запустим», ни отказаться
    от чужой работы. Реальный проход — только после проверки и подтверждения.
    """
    guard = dispatch_guard(cfg, board, now=now)
    if guard:
        raise EngineWriteError(guard["code"], guard["message"], detail=guard["detail"],
                               http_status=guard["status"])
    limit = dispatch_max(cfg, max_cards)
    started = time.perf_counter()
    dry, _ms = _run_json(cfg, board, ["dispatch", "--dry-run", opt("max", limit), "--json"],
                         timeout=60)
    planned = _planned(cfg, board, dry if isinstance(dry, dict) else {})
    foreign = [card for card in planned if card["foreign"]]
    if foreign and not confirm:
        raise EngineWriteError(
            "conflict",
            "Диспетчер запустит карточки вне комнат приложения — нужен confirm:true.",
            detail={"reason": "foreign_cards", "cards": foreign, "planned": planned,
                    "max": limit}, http_status=409)
    data, ms = _run_json(cfg, board, ["dispatch", opt("max", limit), "--json"], timeout=120)
    _mark_dispatch(board, now)
    data = data if isinstance(data, dict) else {}
    spawned = data.get("spawned", data.get("spawned_count"))
    spawned = len(spawned) if isinstance(spawned, list) else spawned
    total_ms = int((time.perf_counter() - started) * 1000)
    return {"ok": True, "spawned": int(spawned or 0),
            "reclaimed": int(data.get("reclaimed") or 0),
            "promoted": int(data.get("promoted") or 0),
            "planned": planned, "max": limit, "result": data,
            "detail": "Диспетчер: запущено %s, продвинуто %s." % (spawned if spawned is not None else "?",
                                                                   data.get("promoted", "?")),
            "cmd_ms": int(total_ms or ms)}


__all__ = ["TASK_ID_RE", "PROFILE_RE", "AUTHOR_MAXIM", "UNASSIGN_VALUES",
           "DISPATCH_MAX_DEFAULT", "DISPATCH_MAX_LIMIT", "DISPATCH_COOLDOWN_S",
           "no_nul", "assert_task_id", "tree_ids", "assert_owned", "profile_names",
           "assert_profile", "assert_author", "dispatch_max", "dispatch_guard",
           "reset_dispatch_guard", "dispatch_board"]
