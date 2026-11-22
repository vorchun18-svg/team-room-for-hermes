# -*- coding: utf-8 -*-
"""Расход и лимиты по провайдерам (W29-P9).

Что делает панель «Расход и лимиты»:

* читает ``state.db`` профилей Hermes ТОЛЬКО на чтение (``mode=ro``) и складывает
  сессии за последние 5 часов, сутки и неделю: вызовы, токены (ввод, чтение из
  кэша, запись в кэш, вывод), оценку стоимости — по провайдерам и моделям;
* хранит ПОЛЬЗОВАТЕЛЬСКИЕ лимиты (в ``data/state/limits.json``): для провайдера —
  токены или вызовы за 5 часов и за неделю; по умолчанию лимитов нет;
* считает прогресс к лимиту и предупреждения на 70 % и 90 %;
* даёт прогноз «хватит ли» по среднему расходу последних запусков;
* показывает топ дорогих запросов со ссылкой на запрос и строку «потрачено зря»
  (запуски на «не той модели», которые пришлось перезапускать).

Сеть не используется: только локальные файлы и локальные базы. Чужие базы и
конфиги не меняются — соединение открывается строкой ``file:…?mode=ro``.

Единицы: токены — целые, стоимость — доллары США (как её пишет Hermes в
``sessions.estimated_cost_usd``). Если Hermes стоимость не знает (подписка,
нет прайса), это честно попадает в ``warnings``, а не превращается в ноль.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import time

try:                                    # пакетный и «плоский» импорт
    from . import statefiles as sf
    from .http_util import ApiError
except ImportError:                     # pragma: no cover
    import statefiles as sf             # type: ignore
    from http_util import ApiError      # type: ignore

#: Окна панели: ключ, длина в секундах, подпись для человека.
WINDOWS = (
    ("5h", 5 * 3600, "за 5 часов"),
    ("day", 24 * 3600, "за сутки"),
    ("week", 7 * 86400, "за неделю"),
)
#: Окна, для которых человек задаёт лимит (как в задаче пакета).
LIMIT_WINDOWS = ("5h", "week")
#: Порог предупреждения и порог «почти всё»: проценты от лимита.
WARN_PCT = 70.0
ALERT_PCT = 90.0
#: Предел значения лимита (защита от опечатки в 10 нулей).
LIMIT_MAX = 1_000_000_000_000
#: Записи доски, которые означают «запуск отменён и перезапущен на другой модели».
RECLAIMED_KINDS = ("reclaimed", "model_override_set")
#: Как называть провайдеров человеку. Неизвестный — как в ``state.db``.
PROVIDER_TITLES = {
    "anthropic": "Anthropic",
    "ollama-cloud": "Ollama Cloud",
    "ollamacloop": "Ollama Cloud",
    "llamacpp": "локально (llama.cpp)",
    "openai": "OpenAI",
    "google": "Google",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "xai": "xAI",
}
NO_PROVIDER = "не указан"
_FIXTURE_NAME = "limits.json"
_TOKEN_KEYS = ("input", "cache_read", "cache_write", "output")

ROUTES = [
    ("GET", r"/api/limits", "h_limits"),
    ("PUT", r"/api/limits/settings", "h_limits_settings_put"),
]


# ------------------------------------------------------------------ мелочи
def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ro(path) -> sqlite3.Connection:
    """Открыть SQLite только на чтение: чужая ``state.db`` не меняется никогда."""
    uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def provider_title(provider) -> str:
    """Подпись провайдера для человека: ``ollama-cloud`` → «Ollama Cloud»."""
    key = str(provider or "").strip()
    if not key:
        return NO_PROVIDER
    return PROVIDER_TITLES.get(key.lower(), key)


def short_number(value) -> str:
    """Короткое число для одной строки: 1 234 567 → «1,2 млн», 640 → «640»."""
    n = float(value or 0)
    if abs(n) >= 1e9:
        return ("%.1f млрд" % (n / 1e9)).replace(".", ",").replace(",0 ", " ")
    if abs(n) >= 1e6:
        return ("%.1f млн" % (n / 1e6)).replace(".", ",").replace(",0 ", " ")
    if abs(n) >= 1e3:
        return "%.0f тыс." % (n / 1e3)
    return str(int(round(n)))


def money(value) -> str:
    """Сумма в долларах с двумя знаками: 12.9639 → «$12,96»."""
    n = float(value or 0.0)
    if n and n < 0.01:
        return "$%.3f" % n
    return "$%.2f" % n


def human_duration(seconds) -> str:
    """«2 ч 15 мин», «3 дн 4 ч», «меньше минуты» — без секунд в сводке."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return "0 мин"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return ("%d дн %d ч" % (days, hours)) if hours else ("%d дн" % days)
    if hours:
        return "%d ч %02d мин" % (hours, minutes)
    if minutes:
        return "%d мин" % minutes
    return "меньше минуты"


def _empty_tokens() -> dict:
    return {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "reasoning": 0, "total": 0}


def _add_tokens(target: dict, source: dict) -> None:
    for key in ("input", "cache_read", "cache_write", "output", "reasoning"):
        target[key] = _int(target.get(key)) + _int(source.get(key))
    target["total"] = sum(_int(target.get(key)) for key in _TOKEN_KEYS)


# ------------------------------------------------------------------ базы
def state_db_paths(state_dir) -> list:
    """``[(подпись, путь)]``: корневая ``state.db`` и ``profiles/*/state.db``."""
    out: list = []
    if not state_dir:
        return out
    root = pathlib.Path(state_dir)
    if (root / "state.db").is_file():
        out.append(("", root / "state.db"))
    try:
        for path in sorted((root / "profiles").glob("*/state.db")):
            if path.is_file():
                out.append((path.parent.name, path))
    except OSError:
        pass
    return out


def _window_totals(con, since: float) -> list:
    """Строки ``(провайдер, модель, вызовы, ввод, кэш-чт., кэш-зап., вывод, стоимость)``."""
    try:
        return con.execute(
            "SELECT billing_provider, model, SUM(api_call_count), SUM(input_tokens), "
            "SUM(cache_read_tokens), SUM(cache_write_tokens), SUM(output_tokens), "
            "SUM(reasoning_tokens), SUM(estimated_cost_usd), COUNT(*) "
            "FROM sessions WHERE started_at >= ? AND api_call_count > 0 "
            "GROUP BY billing_provider, model", (since,)).fetchall()
    except sqlite3.Error:
        return []


def _session_rows(con, ids, since: float) -> list:
    """Сессии по списку id — для топ-запросов и «зря потрачено»."""
    clean = [str(i) for i in ids if i]
    if not clean:
        return []
    marks = ",".join("?" for _ in clean)
    try:
        return con.execute(
            "SELECT id, billing_provider, model, api_call_count, input_tokens, "
            "cache_read_tokens, cache_write_tokens, output_tokens, estimated_cost_usd "
            "FROM sessions WHERE id IN (%s) AND started_at >= ?" % marks,
            clean + [since]).fetchall()
    except sqlite3.Error:
        return []


# ------------------------------------------------------------------ лимиты
def limits_path(cfg):
    """Файл пользовательских лимитов — в данных приложения, не в профилях Hermes."""
    return pathlib.Path(cfg.state_dir) / "limits.json"


def _normalize_limit(item):
    """``{"kind":"tokens"|"calls","value":N}`` или ``None`` (лимита нет)."""
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "tokens").strip().lower()
    if kind not in ("tokens", "calls"):
        kind = "tokens"
    value = _int(item.get("value"), 0)
    if value <= 0:
        return None
    return {"kind": kind, "value": min(value, LIMIT_MAX)}


def normalize_limits(payload) -> dict:
    """Проверить и привести лимиты к одному виду (ничего лишнего не сохраняем)."""
    src = payload.get("providers") if isinstance(payload, dict) else None
    providers: dict = {}
    for raw_key, raw in (src or {}).items():
        key = str(raw_key or "").strip()[:64]
        if not key or not isinstance(raw, dict):
            continue
        item: dict = {}
        for window in LIMIT_WINDOWS:
            limit = _normalize_limit((raw or {}).get(window))
            if limit:
                item[window] = limit
        if item:
            providers[key] = item
    return {"providers": providers, "updated_at": int(time.time())}


def load_limits(cfg) -> dict:
    """Прочитанные лимиты; файла нет — «лимитов не задавали» (пустой набор)."""
    data, info = sf.read_json_doc(limits_path(cfg), None)
    if info.get("state") == "unreadable":
        raise sf.StateUnavailable(
            "Не удалось прочитать лимиты: %s. Показан только расход." % (info.get("error") or "файл занят"),
            {"path": str(limits_path(cfg))})
    if info.get("state") == "corrupt":
        sf.quarantine(limits_path(cfg))
        data = None
    return normalize_limits(data if isinstance(data, dict) else {})


def save_limits(cfg, payload) -> dict:
    """Сохранить лимиты атомарно (проверенные значения; пустые — удаляются)."""
    value = normalize_limits(payload if isinstance(payload, dict) else {})
    path = limits_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write_json_atomic(path, value)
    return value


# ------------------------------------------------------------------ прогресс
def progress(used, limit) -> dict:
    """Прогресс к лимиту: ``{kind, value, used, pct, status}`` либо ``None``."""
    if not isinstance(limit, dict) or not limit.get("value"):
        return None
    value = _int(limit.get("value"))
    if value <= 0:
        return None
    pct = round(100.0 * float(used or 0) / value, 1)
    if pct >= 100.0:
        status = "over"
    elif pct >= ALERT_PCT:
        status = "alert"
    elif pct >= WARN_PCT:
        status = "warn"
    else:
        status = "ok"
    return {"kind": str(limit.get("kind") or "tokens"), "value": value,
            "used": _int(used), "pct": pct, "status": status}


def forecast(used, limit, window_seconds: int, elapsed: int, sessions: int, now: int) -> dict | None:
    """Хватит ли лимита: по среднему расходу за прошедшую часть окна.

    Честно оговариваем, что это линейный прогноз по последнему окну, а не
    гарантия: ``basis`` так и пишет, что взято за основу.
    """
    if not isinstance(limit, dict) or not limit.get("value"):
        return None
    value = _int(limit.get("value"))
    used_now = float(used or 0)
    span = max(1, min(int(window_seconds), int(elapsed or window_seconds)))
    rate = used_now / span
    left = max(0.0, value - used_now)
    seconds_left = None
    if used_now >= value:
        seconds_left = 0
    elif rate > 0:
        seconds_left = left / rate
    unit = "токенов" if str(limit.get("kind")) == "tokens" else "вызовов"
    per_run = (used_now / sessions) if sessions else 0.0
    runs_left = int(left / per_run) if per_run > 0 else None
    return {
        "kind": str(limit.get("kind") or "tokens"),
        "limit": value,
        "used": int(round(used_now)),
        "left": int(round(left)),
        "hours_left": round(seconds_left / 3600.0, 1) if seconds_left is not None else None,
        "runs_left": runs_left,
        "avg_per_run": int(round(per_run)),
        "basis": "средний расход за прошедшую часть окна",
        "unit": unit,
        "text": _forecast_text(seconds_left, left, unit, runs_left, used_now >= value),
    }


def _runs_word(count: int) -> str:
    """«1 запуск», «2 запуска», «5 запусков» — для прогноза, а не «запуск(ов)»."""
    n = abs(int(count or 0)) % 100
    if 11 <= n <= 14:
        return "запусков"
    n %= 10
    if n == 1:
        return "запуск"
    if 2 <= n <= 4:
        return "запуска"
    return "запусков"


def _forecast_text(seconds_left, left, unit, runs_left, spent) -> str:
    if spent:
        return "Лимит уже израсходован — новые запуски упрутся в потолок"
    if seconds_left is None:
        return "Расход в этом окне нулевой — прогноз появится после первых запусков"
    tail = ""
    if runs_left is not None:
        tail = " (≈ %d %s)" % (runs_left, _runs_word(runs_left))
    return "При нынешнем темпе хватит на %s%s" % (human_duration(seconds_left), tail)


# ------------------------------------------------------------------ карточки
def _rooms_of(cfg) -> list:
    data, info = sf.read_json_doc(pathlib.Path(cfg.data_dir) / "rooms.json", None)
    if info.get("state") == "unreadable" or not isinstance(data, dict):
        return []
    rooms = data.get("rooms") if isinstance(data.get("rooms"), list) else []
    return [r for r in rooms if isinstance(r, dict) and r.get("id")]


def card_index(cfg) -> tuple:
    """``(session_id → карточка, task_id → карточка, предупреждения)``.

    Карточка — ``{room, room_title, rid, title}``. Сессии берутся из событий доски
    (``owner_admitted`` и перезапуски), карточки — из реестра комнаты
    (``requests.json``). Только чтение.
    """
    by_session: dict = {}
    by_task: dict = {}
    warnings: list = []
    for room in _rooms_of(cfg):
        room_id = str(room.get("id") or "")
        room_title = str(room.get("title") or room_id)
        reg, info = sf.read_json_doc(pathlib.Path(cfg.state_dir) / room_id / "requests.json", None)
        if info.get("state") == "unreadable":
            warnings.append("реестр комнаты «%s» занят — топ запросов может быть неполным" % room_title)
            continue
        items = (reg or {}).get("requests") if isinstance(reg, dict) else None
        if not isinstance(items, list):
            items = (reg or {}).get("items") if isinstance(reg, dict) else None
        if not isinstance(items, list):
            continue
        tasks: dict = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("root_id") or "")
            if not rid:
                continue
            card = {"room": room_id, "room_title": room_title, "rid": rid,
                    "title": str(item.get("title") or item.get("text") or "")[:120]}
            by_task[rid] = card
            for extra in item.get("extra_cards") or []:
                if isinstance(extra, dict) and extra.get("id"):
                    by_task[str(extra["id"])] = card
                    tasks[str(extra["id"])] = card      # доска: события и доп. карточек тоже
            tasks[rid] = card
        board = _board_db(cfg, str(room.get("board") or "team"))
        if not board or not tasks:
            continue
        try:
            con = _ro(board)
        except sqlite3.Error:
            continue
        marks = ",".join("?" for _ in tasks)
        try:
            rows = con.execute(
                "SELECT task_id, kind, payload FROM task_events "
                "WHERE task_id IN (%s) AND kind IN ('owner_admitted','reclaimed','model_override_set') "
                "AND id >= 0" % marks, list(tasks)).fetchall()
        except sqlite3.Error:
            rows = []
        con.close()
        for task_id, kind, payload in rows:
            try:
                data = json.loads(payload) if payload else {}
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                continue
            session_id = str(data.get("session_id") or "")
            card = tasks.get(str(task_id))
            if session_id and card:
                if str(kind) in RECLAIMED_KINDS:
                    by_session.setdefault("!" + session_id, card)   # «зря» — отдельной меткой
                else:
                    by_session.setdefault(session_id, card)
    return by_session, by_task, warnings


def _board_db(cfg, board: str):
    maker = getattr(cfg, "kanban_db", None)
    if not callable(maker):
        return None
    try:
        path = maker(board)
    except Exception:                  # noqa: BLE001 - путь — не повод для 500
        return None
    return path if path and pathlib.Path(path).is_file() else None


# ------------------------------------------------------------------ сборка
def _empty_window(key: str, seconds: int, title: str, now: int) -> dict:
    return {"key": key, "title": title, "seconds": seconds, "from": now - seconds,
            "calls": 0, "sessions": 0, "cost_usd": 0.0, "tokens": _empty_tokens(),
            "providers": []}


def _provider_row(key: str, title: str) -> dict:
    return {"provider": key, "title": title, "calls": 0, "sessions": 0,
            "cost_usd": 0.0, "tokens": _empty_tokens(), "models": [], "limits": {}}


def panel(cfg, state_dir=None, now=None) -> dict:
    """Полный ответ панели «Расход и лимиты». Никогда не бросает на чтении баз."""
    now = int(now or time.time())
    state_dir = state_dir or _default_state_dir()
    paths = state_db_paths(state_dir)
    limits = load_limits(cfg)
    windows = {key: _empty_window(key, secs, title, now) for key, secs, title in WINDOWS}
    warnings: list = []
    unknown_cost = 0
    zero_cost_calls = 0
    db_problems: list = []

    for label, path in paths:
        try:
            con = _ro(path)
        except sqlite3.Error as exc:
            db_problems.append("%s: %s" % (label or "основная база", exc))
            continue
        for key, seconds, _title in WINDOWS:
            rows = _window_totals(con, now - seconds)
            window = windows[key]
            for provider, model, calls, inp, cache_read, cache_write, output, reasoning, cost, count in rows:
                calls = _int(calls)
                tokens = {"input": _int(inp), "cache_read": _int(cache_read),
                          "cache_write": _int(cache_write), "output": _int(output),
                          "reasoning": _int(reasoning)}
                tokens["total"] = sum(tokens[k] for k in _TOKEN_KEYS)
                window["calls"] += calls
                window["sessions"] += _int(count)
                window["cost_usd"] += _num(cost)
                _add_tokens(window["tokens"], tokens)
                if not cost:
                    zero_cost_calls += calls
                pkey = str(provider or "")
                row = next((p for p in window["providers"] if p["provider"] == pkey), None)
                if row is None:
                    row = _provider_row(pkey, provider_title(pkey))
                    window["providers"].append(row)
                row["calls"] += calls
                row["sessions"] += _int(count)
                row["cost_usd"] += _num(cost)
                _add_tokens(row["tokens"], tokens)
                mitem = next((m for m in row["models"] if m["model"] == str(model or "")), None)
                if mitem is None:
                    mitem = {"model": str(model or "—"), "calls": 0, "cost_usd": 0.0,
                             "tokens": _empty_tokens()}
                    row["models"].append(mitem)
                mitem["calls"] += calls
                mitem["cost_usd"] += _num(cost)
                _add_tokens(mitem["tokens"], tokens)
        try:
            status = con.execute(
                "SELECT cost_status, COUNT(*) FROM sessions WHERE started_at >= ? "
                "AND api_call_count > 0 GROUP BY cost_status", (now - WINDOWS[-1][1],)).fetchall()
        except sqlite3.Error:
            status = []
        unknown_cost += sum(_int(n) for s, n in status if not s or str(s) == "unknown")
        con.close()

    # Провайдеры, для которых лимит задан, а запусков не было: строка с нулём —
    # иначе человек не увидит, что лимит вообще есть, и не поймёт прочерк.
    for key, _seconds, _title in WINDOWS:
        window = windows[key]
        have = {p["provider"] for p in window["providers"]}
        for pkey, item in (limits.get("providers") or {}).items():
            if key in item and pkey not in have:
                window["providers"].append(_provider_row(pkey, provider_title(pkey)))

    source = "state.db" if any(w["calls"] for w in windows.values()) else "none"
    if db_problems:
        warnings.append("часть баз недоступна: " + "; ".join(db_problems[:3]))
    if unknown_cost:
        warnings.append("у %d сессий провайдер не сообщил стоимость — цифры в долларах неполные"
                        % unknown_cost)
    if zero_cost_calls and source == "state.db":
        warnings.append("часть запусков идёт по подписке или без прайса — их вызовы учтены, стоимость $0")

    # Лимиты и прогресс — по провайдерам, которые встретились в данных.
    for key, _seconds, _title in WINDOWS:
        window = windows[key]
        window["cost_usd"] = round(window["cost_usd"], 6)
        for row in window["providers"]:
            row["cost_usd"] = round(row["cost_usd"], 6)
            row["models"].sort(key=lambda m: (-m["tokens"]["total"], m["model"]))
            limit = (limits.get("providers", {}).get(row["provider"]) or {}).get(key)
            used = row["tokens"]["total"] if (limit or {}).get("kind") == "tokens" else row["calls"]
            row["limits"][key] = progress(used, limit)
            row["limit"] = row["limits"][key]
        window["providers"].sort(key=lambda p: (-p["tokens"]["total"], p["title"]))
    totals = {key: _totals_line(windows[key], limits) for key in windows}
    for key, _s, _t in WINDOWS:
        windows[key]["summary"] = totals[key]

    # Топ дорогих запросов и «зря потрачено» — по сессиям из событий доски.
    index, by_task, index_warnings = card_index(cfg)
    warnings.extend(index_warnings)
    top: list = []
    wasted = {"attempts": 0, "cost_usd": 0.0, "tokens": 0, "requests": []}
    if index:
        wanted = {sid.lstrip("!"): None for sid in index}
        per_request: dict = {}
        for label, path in paths:
            try:
                con = _ro(path)
            except sqlite3.Error:
                continue
            rows = _session_rows(con, list(wanted), now - WINDOWS[-1][1])
            con.close()
            for sid, provider, model, calls, inp, cr, cw, output, cost in rows:
                card = index.get(str(sid)) or index.get("!" + str(sid))
                if not card:
                    continue
                tokens = _int(inp) + _int(cr) + _int(cw) + _int(output)
                key = card["rid"]
                item = per_request.get(key)
                if item is None:
                    item = {"room": card["room"], "room_title": card["room_title"],
                            "rid": key, "title": card["title"], "calls": 0,
                            "tokens": 0, "cost_usd": 0.0, "models": []}
                    per_request[key] = item
                item["calls"] += _int(calls)
                item["tokens"] += tokens
                item["cost_usd"] += _num(cost)
                model_name = str(model or "—")
                if model_name not in item["models"]:
                    item["models"].append(model_name)
                if index.get("!" + str(sid)):
                    wasted["cost_usd"] += _num(cost)
                    wasted["tokens"] += tokens
                    if key not in wasted["requests"]:
                        wasted["requests"].append(key)
        top = sorted(per_request.values(),
                     key=lambda r: (-r["tokens"], -r["cost_usd"]))[:5]
        for item in top:
            item["cost_usd"] = round(item["cost_usd"], 6)

    if source == "none":
        fixture = fixture_panel(cfg, now)
        if fixture:
            fixture["limits"] = limits
            fixture.setdefault("warnings", []).append(
                "в state.db нет запусков за неделю — показаны данные фикстуры")
            fixture["warnings"] = warnings + fixture["warnings"]
            fixture["source"] = "fixtures"
            return fixture
    payload = {
        "ok": True,
        "source": source,
        "generated_at": now,
        "windows": windows,
        "limits": limits,
        "forecast": {key: _window_forecast(windows[key], limits, now) for key, _s, _t in WINDOWS},
        "top_requests": top,
        "wasted": {"attempts": len(wasted["requests"]), "cost_usd": round(wasted["cost_usd"], 6),
                   "tokens": wasted["tokens"], "requests": wasted["requests"],
                   "text": _wasted_text(wasted)},
        "warnings": warnings,
        "line": "",
        "chip": "",
    }
    payload["line"] = line_of(payload)
    payload["chip"] = chip_of(payload)
    return payload


def _totals_line(window: dict, limits: dict) -> dict:
    """Итог окна: сколько израсходовано и (если задан) худший прогресс к лимиту."""
    worst = None
    for row in window.get("providers") or []:
        limit = row.get("limit")
        if limit and (worst is None or limit["pct"] > worst["pct"]):
            worst = dict(limit, provider=row["provider"], provider_title=row["title"])
    return {"calls": _int(window.get("calls")), "sessions": _int(window.get("sessions")),
            "tokens": _int((window.get("tokens") or {}).get("total")),
            "cost_usd": round(_num(window.get("cost_usd")), 6),
            "worst_limit": worst}


def _window_forecast(window: dict, limits: dict, now: int) -> dict:
    """Прогноз по самому «горячему» лимиту окна (по нему и предупреждаем)."""
    out: dict = {"items": []}
    elapsed = max(0, now - _int(window.get("from"), now))
    for row in window.get("providers") or []:
        limit = row.get("limit")
        if not limit:
            continue
        used = row["tokens"]["total"] if limit["kind"] == "tokens" else row["calls"]
        item = forecast(used, limit, _int(window.get("seconds"), 0), elapsed,
                        _int(row.get("sessions")), now)
        if item:
            item.update({"provider": row["provider"], "provider_title": row["title"],
                         "status": limit["status"], "pct": limit["pct"]})
            out["items"].append(item)
    out["items"].sort(key=lambda i: -i["pct"])
    out["text"] = out["items"][0]["text"] if out["items"] else (
        "Лимиты не заданы — прогноз появится, когда вы зададите потолок")
    del limits
    return out


def _wasted_text(wasted: dict) -> str:
    if not wasted.get("requests"):
        return "Потрачено зря: нет — перезапусков на другой модели не было"
    return "Потрачено зря: %d запусков на %s токенов (%s) — модель переключали после старта" % (
        len(wasted["requests"]), short_number(wasted["tokens"]), money(wasted["cost_usd"]))


def line_of(payload: dict) -> str:
    """Одна строка для шапки/панели: «Расход за 5 ч: 1,2 млн токенов · $3,10 …»."""
    windows = (payload or {}).get("windows") or {}
    five = windows.get("5h") or {}
    week = windows.get("week") or {}
    tokens = _int((five.get("tokens") or {}).get("total"))
    parts = ["Расход за 5 ч: %s токенов · %s" % (short_number(tokens), money(five.get("cost_usd")))]
    worst = (week.get("summary") or {}).get("worst_limit")
    if worst:
        parts.append("за неделю %s %% лимита (%s)" % (
            ("%.0f" % worst["pct"]).replace(",", "."),
            worst.get("provider_title") or provider_title(worst.get("provider"))))
    else:
        parts.append("за неделю %s токенов" % short_number((week.get("tokens") or {}).get("total")))
    return "; ".join(parts)


def chip_of(payload: dict) -> str:
    """Компактная строка для шапки: «5 ч: 1,2 млн · 84 %»."""
    windows = (payload or {}).get("windows") or {}
    five = windows.get("5h") or {}
    worst = (five.get("summary") or {}).get("worst_limit") or \
        ((windows.get("week") or {}).get("summary") or {}).get("worst_limit")
    text = "5 ч: %s" % short_number((five.get("tokens") or {}).get("total"))
    if worst:
        text += " · %.0f %%" % worst["pct"]
    return text


def _default_state_dir():
    """Каталог Hermes по умолчанию (как в ``usage.py``), без записи."""
    for env in ("HERMES_HOME", "HERMES_DIR", "TEAMROOM_HERMES_HOME"):
        value = os.environ.get(env)
        if value:
            return value
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return str(pathlib.Path(local) / "hermes")
    home = pathlib.Path.home()
    for candidate in (home / ".local" / "share" / "hermes", home / ".hermes"):
        if candidate.is_dir():
            return str(candidate)
    return str(home / ".hermes")


# ------------------------------------------------------------------ фикстура
def fixture_panel(cfg, now: int) -> dict | None:
    """Демо-данные для песочницы: те же ключи, что у живого ответа."""
    folder = getattr(cfg, "fixtures_dir", None)
    if not folder:
        folder = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
    path = pathlib.Path(folder) / _FIXTURE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    payload = dict(data)
    payload.setdefault("warnings", [])
    payload.setdefault("limits", {"providers": {}, "updated_at": 0})
    payload["generated_at"] = now
    payload["line"] = line_of(payload)
    payload["chip"] = chip_of(payload)
    return payload


# ------------------------------------------------------------------ HTTP
class LimitsRoutes:
    """Обработчики панели лимитов (``self`` — экземпляр ``server.app.App``)."""

    def _limits_state_dir(self):
        return getattr(self.cfg, "hermes_home", None) or _default_state_dir()

    def h_limits(self, query, body, *rest):
        """``GET /api/limits`` — расход по провайдерам, лимиты, прогноз, топ запросов."""
        mod = self.mod("limits")
        try:
            payload = mod.panel(self.cfg, state_dir=self._limits_state_dir())
        except mod.sf.StateUnavailable as exc:
            raise ApiError(503, exc.message, code="limits_unavailable",
                           detail=dict(exc.detail or {})) from exc
        except Exception as exc:      # noqa: BLE001 - панель не должна ронять страницу
            self.log.error("limits: %s" % exc)
            raise ApiError(503, "Расход и лимиты сейчас недоступны — попробуйте ещё раз",
                           code="limits_unavailable", detail={"reason": str(exc)[:200]}) from exc
        payload["limits"] = mod.load_limits(self.cfg)
        return 200, payload, {"Cache-Control": "no-store"}

    def h_limits_settings_put(self, query, body, *rest):
        """``PUT /api/limits/settings`` — сохранить лимиты человека.

        Тело: ``{"providers": {"<провайдер>": {"5h": {"kind":"tokens|calls","value":N},
        "week": {…}}}}``. Пустой набор — «лимитов нет» (так и храним).
        """
        mod = self.mod("limits")
        data = body if isinstance(body, dict) else {}
        if not isinstance(data.get("providers", {}), dict):
            raise ApiError(400, "Лимиты должны быть списком по провайдерам",
                           code="bad_request", detail={"field": "providers"})
        try:
            saved = mod.save_limits(self.cfg, data)
        except mod.sf.StateUnavailable as exc:
            raise ApiError(503, exc.message, code="limits_unavailable",
                           detail=dict(exc.detail or {})) from exc
        except OSError as exc:
            raise ApiError(503, "Не удалось сохранить лимиты: файл занят (%s)" % exc,
                           code="limits_unavailable") from exc
        return 200, {"ok": True, "limits": saved}, {"Cache-Control": "no-store"}


def install_limits_routes(app_cls, routes: list) -> None:
    """Подключить лимиты к ``App``: методы миксина + строки таблицы маршрутов."""
    import re as _re
    for name, value in vars(LimitsRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
