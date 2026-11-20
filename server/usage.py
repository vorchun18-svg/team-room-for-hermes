# -*- coding: utf-8 -*-
"""Расход токенов запроса (W28-E, п.6 замеров 19.11: «видимость и стоп-кран»).

``GET /api/requests/{rid}/usage`` — сколько стоил запрос: вызовы модели, токены
(вход, кэш-чтение, кэш-запись, выход), оценка стоимости и одна строка для интерфейса
«Расход: N вызовов, M тыс. токенов (база / тело / инструменты)».

Откуда цифры:

* карточки запроса — из реестра (``rec.extra_cards``) и детали (``detail.tree``);
* сессии карточек — из ``task_events`` доски канбана (``kind = owner_admitted``):
  Hermes пишет там, какой ``session_id`` и в каком ``state.db`` профиля он завёл
  (так работают и профили участников комнаты);
* токены и стоимость — из ``state.db`` (``sessions``: ``input_tokens``,
  ``cache_read_tokens``, ``cache_write_tokens``, ``output_tokens``,
  ``estimated_cost_usd``). База данных открывается ТОЛЬКО на чтение (``mode=ro``):
  конфиги и профили Hermes не меняются.

Разделение «база / тело / инструменты»:

* ``база`` — первый вызов сессии (системный промпт + память + каталог навыков);
  берётся из замеров ``state.db`` (медиана ``cache_write + input`` коротких сессий),
  на сессию не больше этой величины;
* ``инструменты`` — доля схем инструментов в базе (по записям ``system_prompts``:
  в них лежит JSON с ``tools``);
* ``тело`` — всё остальное: текст карточки, история сессии, результаты инструментов.

Песочница и демо: если ``state.db``/доски нет (или карточка без сессий), отдаём данные
из фикстуры ``fixtures/request-usage.json`` — интерфейс не должен пустовать, а врать
цифрами нельзя, поэтому в ответе всегда есть ``source`` (``state.db`` / ``fixtures`` /
``none``) и ``warnings``.

Модуль ничего не пишет и не запускает: только чтение.
"""
from __future__ import annotations

import json
import re as _re
import sqlite3
import statistics
import time

try:                                    # пакетный и «плоский» импорт
    from .http_util import ApiError
except ImportError:                     # pragma: no cover
    from http_util import ApiError      # type: ignore

RID_RE = r"(?P<rid>t_[A-Za-z0-9_]+)"

ROUTES = [
    ("GET", r"/api/requests/" + RID_RE + r"/usage", "h_request_usage"),
]

#: Оценка «первого вызова» по умолчанию — из замеров 19.11.2026 (≈30–31 тыс. токенов:
#: промпт + схемы инструментов + память + каталог навыков). Используется, когда
#: state.db недоступен.
BASE_TOKENS_DEFAULT = 31_000
#: Доля схем инструментов в базе (замер: 29 тыс. знаков промпта против 59 тыс. знаков
#: JSON со схемами → ≈2/3 базы). Уточняется по state.db, когда он есть.
TOOLS_SHARE_DEFAULT = 0.66
#: Сколько секунд держим замер базы в памяти (state.db читается редко и целиком).
BASE_CACHE_S = 600
#: Статусы сессии, которые считаем неудачной попыткой «не на той модели».
RECLAIMED_KINDS = ("reclaimed", "model_override_set")
STATE_KINDS = ("owner_admitted",)

_FIXTURE_NAME = "request-usage.json"
_BASE_CACHE: dict = {}


# ---------------------------------------------------------------- доступ к данным
def _ro(path) -> sqlite3.Connection:
    """Открыть SQLite только на чтение (чужой state.db не меняется никогда)."""
    uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
    return sqlite3.connect(uri, uri=True, timeout=2.0)


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


def card_ids(detail, rec) -> list:
    """Карточки запроса: корень, дерево и записи реестра — без повторов, по порядку."""
    out: list = []
    for value in [str((rec or {}).get("root_id") or (detail or {}).get("id") or "")]:
        if value and value not in out:
            out.append(value)
    for node in (detail or {}).get("tree") or []:
        if isinstance(node, dict):
            task_id = str(node.get("id") or "")
            if task_id and task_id not in out:
                out.append(task_id)
    for card in (rec or {}).get("extra_cards") or []:
        if isinstance(card, dict):
            task_id = str(card.get("id") or "")
            if task_id and task_id not in out:
                out.append(task_id)
    return [i for i in out if i]


def _board_db(cfg, board: str):
    """Путь к базе доски канбана (``cfg.kanban_db``) или ``None``."""
    maker = getattr(cfg, "kanban_db", None)
    if callable(maker):
        try:
            return maker(board)
        except Exception:               # noqa: BLE001 - путь — не повод для 500
            return None
    return None


def sessions_of(db_path, task_ids) -> list:
    """Сессии карточек из ``task_events``: ``[{task_id, session_id, db, kind}]``."""
    ids = [i for i in (task_ids or []) if i]
    if not ids or not db_path:
        return []
    try:
        con = _ro(db_path)
    except sqlite3.Error:
        return []
    out: list = []
    try:
        marks = ",".join("?" for _ in ids)
        rows = con.execute(
            "SELECT task_id, kind, payload FROM task_events "
            "WHERE kind IN ('owner_admitted','reclaimed') AND task_id IN (%s) "
            "ORDER BY id" % marks, ids).fetchall()
    except sqlite3.Error:
        con.close()
        return []
    con.close()
    for task_id, kind, payload in rows:
        try:
            data = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        out.append({"task_id": str(task_id or ""), "kind": str(kind or ""),
                    "session_id": str(data.get("session_id") or ""),
                    "db": str(data.get("db") or "")})
    return out


def session_usage(db_path, session_id) -> dict | None:
    """Строка ``sessions`` по id сессии: токены, модель, стоимость (только чтение)."""
    if not db_path or not session_id:
        return None
    try:
        con = _ro(db_path)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT model, api_call_count, input_tokens, cache_read_tokens, cache_write_tokens, "
            "output_tokens, reasoning_tokens, estimated_cost_usd, cost_status "
            "FROM sessions WHERE id = ?", (str(session_id),)).fetchone()
    except sqlite3.Error:
        row = None
    con.close()
    if not row:
        return None
    return {"model": str(row[0] or ""), "calls": _int(row[1]), "input": _int(row[2]),
            "cache_read": _int(row[3]), "cache_write": _int(row[4]), "output": _int(row[5]),
            "reasoning": _int(row[6]), "cost_usd": round(_num(row[7]), 6),
            "cost_status": str(row[8] or "")}


def base_estimate(state_dir) -> dict:
    """Замер базы запроса по ``state.db`` (кэшируется на ``BASE_CACHE_S``).

    ``{base_tokens, tools_share, prompt_chars, tools_chars, source}``. База — медиана
    ``cache_write + input`` коротких сессий (1–2 вызова): на первом вызове кэш пишется
    целиком. Доля инструментов — из записей ``system_prompts`` (JSON с ``tools``).
    """
    key = str(state_dir or "")
    now = int(time.time())
    cached = _BASE_CACHE.get(key)
    if cached and now - cached["at"] < BASE_CACHE_S:
        return cached["value"]
    value = {"base_tokens": BASE_TOKENS_DEFAULT, "tools_share": TOOLS_SHARE_DEFAULT,
             "prompt_chars": 0, "tools_chars": 0, "source": "default"}
    bases: list = []
    prompts: list = []
    tools: list = []
    try:
        paths = []
        if state_dir:
            root = __import__("pathlib").Path(state_dir)
            if (root / "state.db").is_file():
                paths.append(root / "state.db")
            try:
                paths.extend(p for p in sorted((root / "profiles").glob("*/state.db")) if p.is_file())
            except OSError:
                pass
        for path in paths:
            try:
                con = _ro(path)
            except sqlite3.Error:
                continue
            try:
                rows = con.execute(
                    "SELECT api_call_count, input_tokens, cache_write_tokens "
                    "FROM sessions WHERE api_call_count > 0 AND cache_write_tokens > 0").fetchall()
                proms = con.execute("SELECT prompt FROM system_prompts").fetchall()
            except sqlite3.Error:
                rows, proms = [], []
            con.close()
            for calls, inp, cache_write in rows:
                if _int(calls) <= 2:
                    bases.append(_int(cache_write) + _int(inp))
            for (prompt,) in proms:
                text = str(prompt or "")
                if not text.lstrip().startswith("{"):
                    if text:
                        prompts.append(len(text))
                    continue
                try:
                    data = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("tools"), list):
                    try:
                        tools.append(len(json.dumps(data["tools"], ensure_ascii=False)))
                    except (TypeError, ValueError):
                        pass
    except Exception:                  # noqa: BLE001 - замер не должен ронять запрос
        bases, prompts, tools = [], [], []
    if bases:
        value["base_tokens"] = int(statistics.median(bases))
        value["source"] = "state.db"
    if tools and prompts:
        prompt_chars = int(statistics.median(prompts))
        tools_chars = int(statistics.median(tools))
        total = prompt_chars + tools_chars
        if total > 0:
            value["tools_share"] = round(tools_chars / float(total), 3)
        value["prompt_chars"] = prompt_chars
        value["tools_chars"] = tools_chars
    _BASE_CACHE[key] = {"at": now, "value": value}
    return value


# ---------------------------------------------------------------- фикстура (демо)
def _fixtures_dir(cfg):
    value = getattr(cfg, "fixtures_dir", None)
    if value:
        return value
    try:
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        return root / "fixtures"
    except Exception:                  # noqa: BLE001
        return None


def fixture_usage(cfg, rid: str) -> dict | None:
    """Расход из фикстуры (песочница/демо): те же ключи, что у живого ответа."""
    folder = _fixtures_dir(cfg)
    if not folder:
        return None
    try:
        import pathlib
        path = pathlib.Path(folder) / _FIXTURE_NAME
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    payload = dict(data)
    payload["rid"] = rid
    payload["source"] = "fixtures"
    payload.setdefault("warnings", [])
    payload["line"] = line_of(payload)
    return payload


# ---------------------------------------------------------------- сборка ответа
def _empty() -> dict:
    return {"calls": 0, "cards": 0, "tokens": {"input": 0, "cache_read": 0, "cache_write": 0,
                                               "output": 0, "reasoning": 0},
            "cost_usd": 0.0, "models": [], "mismatched": {"attempts": 0, "cards": []},
            "split": {"base": 0, "body": 0, "tools": 0}}


def line_of(payload: dict) -> str:
    """Одна строка для интерфейса: «Расход: N вызовов, M тыс. токенов (база/тело/инструменты)»."""
    calls = _int((payload or {}).get("calls"))
    tokens = (payload or {}).get("tokens") or {}
    total = sum(_int(tokens.get(k)) for k in ("input", "cache_read", "cache_write", "output"))
    split = (payload or {}).get("split") or {}
    thousands = int(round(total / 1000.0))
    parts = []
    if _int(split.get("base")):
        parts.append("база %d" % int(round(_int(split.get("base")) / 1000.0)))
    if _int(split.get("body")):
        parts.append("тело %d" % int(round(_int(split.get("body")) / 1000.0)))
    if _int(split.get("tools")):
        parts.append("инструменты %d" % int(round(_int(split.get("tools")) / 1000.0)))
    tail = " (%s тыс.)" % " / ".join(parts) if parts else ""
    return "Расход: %d выз. / %d тыс. токенов%s" % (calls, thousands, tail)


def usage(cfg, room, rid: str, detail=None, rec=None, state_dir=None) -> dict:
    """Расход запроса ``rid``: карточки → сессии → токены, стоимость и разбивка.

    ``state_dir`` — каталог Hermes (по умолчанию из окружения/настроек); читается
    только на чтение. Нет данных — ``source="fixtures"`` (демо) или ``"none"``.
    Никогда не бросает: интерфейс получает строку и честный ``source``.
    """
    room = room if isinstance(room, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    rec = rec if isinstance(rec, dict) else {}
    board = str(room.get("board") or "")
    ids = card_ids(detail, rec)
    payload = _empty()
    payload.update({"rid": rid, "cards": len(ids), "warnings": [], "models": [],
                    "mismatched": {"attempts": 0, "cards": []}})
    base = base_estimate(state_dir or _default_state_dir())
    payload["base_estimate"] = base
    db_path = _board_db(cfg, board) if board else None
    events = sessions_of(db_path, ids) if db_path else []
    seen: set = set()
    totals = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "reasoning": 0}
    for event in events:
        if event["kind"] in RECLAIMED_KINDS:
            payload["mismatched"]["attempts"] += 1
            if event["task_id"] not in payload["mismatched"]["cards"]:
                payload["mismatched"]["cards"].append(event["task_id"])
            continue
        session_id = event["session_id"]
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        row = session_usage(event["db"], session_id)
        if not row:
            payload["warnings"].append("сессия %s не найдена в %s" % (session_id[:12], event["db"]))
            continue
        payload["calls"] += row["calls"]
        for key in totals:
            totals[key] += row[key]
        payload["cost_usd"] += row["cost_usd"]
        model = row["model"] or "—"
        item = next((m for m in payload["models"] if m["model"] == model), None)
        if item is None:
            item = {"model": model, "calls": 0, "cost_usd": 0.0, "sessions": 0}
            payload["models"].append(item)
        item["calls"] += row["calls"]
        item["cost_usd"] = round(item["cost_usd"] + row["cost_usd"], 6)
        item["sessions"] += 1
        # «не та модель»: попытка шла на модели, отличной от первой в запросе
        first = payload["models"][0]["model"] if payload["models"] else model
        if model != first and event["task_id"] not in payload["mismatched"]["cards"]:
            payload["mismatched"]["attempts"] += 1
            payload["mismatched"]["cards"].append(event["task_id"])
    if payload["calls"] or totals["cache_read"] or totals["input"]:
        payload["tokens"] = totals
        payload["cost_usd"] = round(payload["cost_usd"], 6)
        sessions_count = sum(m["sessions"] for m in payload["models"]) or 1
        base_tokens = min(_int(base["base_tokens"]), int(totals["cache_write"] or 0) or
                          _int(base["base_tokens"]))
        base_total = base_tokens * sessions_count
        tools_total = int(base_total * float(base.get("tools_share") or 0))
        grand = sum(totals.values())
        body = max(0, grand - base_total)
        payload["split"] = {"base": int(base_total - tools_total), "tools": int(tools_total),
                            "body": int(body)}
        payload["source"] = "state.db"
        payload["line"] = line_of(payload)
        payload["models"].sort(key=lambda m: (-m["cost_usd"], m["model"]))
        if len({m.get("model") for m in payload["models"]}) > 1 and not payload["mismatched"]["attempts"]:
            payload["warnings"].append("запрос работал на нескольких моделях")
        return payload
    fixture = fixture_usage(cfg, rid)
    if fixture:
        payload["warnings"].append("нет сессий в state.db — показаны данные фикстуры")
        fixture.setdefault("warnings", [])
        fixture["warnings"].extend(payload["warnings"])
        return fixture
    payload["source"] = "none"
    payload["line"] = "Расход: нет данных (карточки ещё не запускались)"
    return payload


def _default_state_dir():
    """Каталог Hermes по умолчанию (`%LOCALAPPDATA%\\hermes`, `~/.local/share/hermes`)."""
    import os
    import pathlib
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


# ---------------------------------------------------------------- HTTP
class UsageRoutes:
    """Обработчик расхода запроса (``self`` — экземпляр ``server.app.App``)."""

    def h_request_usage(self, query, body, rid):
        """``GET /api/requests/{id}/usage`` — вызовы, токены, стоимость, строка расхода."""
        room, rec = self._find_record(rid)
        mod = self.mod("usage")
        detail = {}
        try:
            got = self._detail(room, rid, rec.get("extra_cards") or [])
            detail = got if isinstance(got, dict) else {}
        except Exception as exc:        # noqa: BLE001 - без дерева цифры всё равно есть
            self.log.error("usage detail %s: %s" % (rid, exc))
        state_dir = getattr(self.cfg, "hermes_home", None) or _default_state_dir()
        try:
            payload = mod.usage(self.cfg, room, rid, detail=detail, rec=rec, state_dir=state_dir)
        except Exception as exc:        # noqa: BLE001 - расход не должен ронять страницу
            self.log.error("usage %s: %s" % (rid, exc))
            raise ApiError(503, "Расход сейчас недоступен — попробуйте ещё раз",
                           code="usage_unavailable", detail={"reason": "usage_unavailable"}) from exc
        payload.setdefault("generated_at", int(time.time()))
        payload.setdefault("warnings", [])
        return 200, payload, {"Cache-Control": "no-store"}


def install_usage_routes(app_cls, routes: list) -> None:
    """Подключить расход к ``App``: методы миксина + строки таблицы маршрутов."""
    for name, value in vars(UsageRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
