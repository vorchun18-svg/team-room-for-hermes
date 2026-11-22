"""Результаты комнаты (WAVE29 §P6): итоги завершённых запросов и «запросы без дела».

Зачем модуль
------------
Вкладка «Результаты» (бывшая «Отчёты») и вкладка «Дела» раньше были пусты у
комнаты с готовыми запросами: отчёты появлялись только если команда собирала
интерактивную страницу, а дела — только если человек заводил их руками.
Здесь один источник правды поверх уже существующих данных:

* реестр запросов комнаты (``registry.list_requests``) — названия и время;
* доски Hermes — статус и время завершения (``engine_read.today_states``, один
  запрос на все корни) и последний непустой ``summary`` корня — «Коротко»;
* индекс отчётов (``reports.list_reports``) — интерактивные страницы;
* файл дел комнаты (``delo.list_delos``) — какие запросы уже стали делами.

Ничего не пишется, кроме одного служебного файла «предложение собрать карту»
(``state/<комната>/roadmap_offer.json``) — он нужен, чтобы пункт «Предлагаю
собрать карту проекта» показывался один раз, а не висел вечно.

Публичный интерфейс:
    list_results(cfg, room_id, board=None, …) -> dict
    without_delo(cfg, room_id, delo_items, …) -> list[dict]
    phases_from_requests(cfg, room, items) -> dict
    roadmap_offer(cfg, room, board=None) -> dict|None
    dismiss_offer(cfg, room_id, …) -> dict
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

try:                                     # пакетный и «плоский» импорт
    from . import registry
    from . import reports as reports_mod
    from . import statefiles as sf
except ImportError:                      # pragma: no cover
    import registry                      # type: ignore
    import reports as reports_mod        # type: ignore
    import statefiles as sf              # type: ignore

DONE_STATUSES = ("done",)
SHORT_MAX = 240
REQUEST_LIMIT = 200
PHASE_LIMIT = 6
OFFER_FILE = "roadmap_offer.json"
#: Сколько дней «Не сейчас» на пункт «Предлагаю собрать карту проекта».
OFFER_COOLDOWN = 14 * 86400


# ------------------------------------------------------------------ «Коротко» --
_MD_LINK = re.compile(r"\[([^\]]{1,120})\]\([^)]*\)")
_WS = re.compile(r"\s+")


def short_text(text, limit: int = SHORT_MAX) -> str:
    """Короткий итог из текста свода: без разметки, по границе предложения."""
    raw = str(text or "")
    if not raw.strip():
        return ""
    raw = re.sub(r"```.*?```", " ", raw, flags=re.S)
    raw = _MD_LINK.sub(r"\1", raw)
    raw = re.sub(r"^\s{0,3}#{1,6}\s*", "", raw, flags=re.M)
    raw = re.sub(r"^\s*[-*•]\s+", "", raw, flags=re.M)
    raw = re.sub(r"[*_`>#|]+", " ", raw)
    raw = _WS.sub(" ", raw).strip()
    if len(raw) <= limit:
        return raw
    cut = raw[:limit]
    for sep in (". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos >= int(limit * 0.6):
            return cut[:pos + 1].strip()
    pos = cut.rfind(" ")
    head = cut[:pos] if pos > 0 else cut
    return head.strip().rstrip(" ,;:—–-") + "…"


# ---------------------------------------------------------- внешние источники --
def _engine():
    try:
        from . import engine_read as eng
    except ImportError:                  # pragma: no cover
        import engine_read as eng        # type: ignore
    return eng


def states_of(cfg, board, ids) -> dict:
    """``{root_id: {status, finished_at}}`` одним запросом; сбой — пусто."""
    keys = [str(i) for i in ids or [] if i]
    if not keys:
        return {}
    try:
        return _engine().today_states(cfg, board, keys) or {}
    except Exception:                    # noqa: BLE001 — статусы вторичны
        return {}


def shorts_of(cfg, board, ids) -> dict:
    """``{root_id: «Коротко»}`` — последний непустой summary карточки-корня."""
    keys = [str(i) for i in ids or [] if i]
    if not keys:
        return {}
    eng = _engine()
    out: dict = {}
    try:
        with eng._conn(cfg, board) as con:
            rows = eng._run_rows(con, keys)
    except Exception:                    # noqa: BLE001 — «Коротко» не обязательно
        return {}
    for root_id, runs in (rows or {}).items():
        for run in reversed(list(runs or [])):
            text = str((run or {}).get("summary") or "").strip()
            if text:
                out[str(root_id)] = short_text(text)
                break
    return out


def report_items(cfg, room_id) -> list:
    """Индекс отчётов комнаты в плоском виде (без путей — они не нужны экрану)."""
    try:
        items = reports_mod.list_reports(cfg, room_id) or []
    except Exception:                    # noqa: BLE001 — битый отчёт не роняет список
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        out.append({"key": key,
                    "title": str(item.get("title") or key),
                    "subtitle": str(item.get("subtitle") or ""),
                    "kind": "html" if item.get("kind") == "html" else "spec",
                    "request_id": str(item.get("request_id") or "") or None,
                    "blocks": int(item.get("blocks") or 0),
                    "updated_at": int(item.get("updated_at") or 0)})
    out.sort(key=lambda it: it.get("updated_at") or 0, reverse=True)
    return out


def delo_items(cfg, room_id, sources=None) -> list:
    """Дела комнаты (только чтение). Сбой читается как «дел нет»."""
    try:
        from . import delo as delo_mod
    except ImportError:                  # pragma: no cover
        import delo as delo_mod          # type: ignore
    try:
        payload = delo_mod.list_delos(cfg, room_id, sources=sources)
        return (payload or {}).get("items") or []
    except Exception:                    # noqa: BLE001
        return []


def _records(cfg, room_id) -> list:
    try:
        return registry.list_requests(cfg, room_id, REQUEST_LIMIT) or []
    except Exception:                    # noqa: BLE001 — реестр вторичен для экрана
        return []


# ------------------------------------------------------------------- список ---
def list_results(cfg, room_id, board=None, states=None, shorts=None,
                 report_index=None, delos=None, limit: int = REQUEST_LIMIT) -> dict:
    """Итоги ВСЕХ завершённых запросов комнаты + блок интерактивных отчётов.

    ``items``      — завершённые запросы (свежие сверху), у каждого «Коротко»,
                     отчёты этого запроса и ссылка на страницу запроса;
    ``pending``    — запросы ещё в работе (чтобы пустой экран не врал «ничего нет»);
    ``reports``    — интерактивные страницы-отчёты комнаты;
    ``without_delo`` — запросы, из которых ещё не сделали дело (для вкладки «Дела»);
    ``counts``     — числа для шапки (один источник для подписей и счётчиков).
    """
    records = _records(cfg, room_id)[:max(0, int(limit or REQUEST_LIMIT))]
    ids = [str(rec.get("root_id") or "") for rec in records if rec.get("root_id")]
    st = states if states is not None else states_of(cfg, board, ids)
    sh = shorts if shorts is not None else shorts_of(cfg, board, ids)
    reps = report_items(cfg, room_id) if report_index is None else list(report_index)
    delo_list = delo_items(cfg, room_id) if delos is None else list(delos)

    by_request: dict = {}
    for item in reps:
        rid = str(item.get("request_id") or "")
        if rid:
            by_request.setdefault(rid, []).append(
                {"key": item.get("key"), "title": item.get("title") or item.get("key"),
                 "kind": item.get("kind") or "spec"})
    delo_by_request = {}
    for item in delo_list:
        rid = str((item or {}).get("request_id") or "")
        if rid:
            delo_by_request[rid] = {"id": item.get("id"), "title": item.get("title") or ""}

    items, pending = [], []
    for rec in records:
        rid = str(rec.get("root_id") or "")
        if not rid:
            continue
        state = st.get(rid) if isinstance(st, dict) else None
        state = state if isinstance(state, dict) else {}
        status = str(state.get("status") or "")
        finished = int(state.get("finished_at") or 0)
        title = str(rec.get("title") or "")
        entry = {"id": rid,
                 "slug": str(rec.get("slug") or ""),
                 "title": title,
                 "title_short": short_text(title, 90) or title or rid,
                 "status": status,
                 "created_at": int(rec.get("created_at") or 0),
                 "finished_at": finished,
                 "short": str(sh.get(rid) or ""),
                 "reports": by_request.get(rid) or [],
                 "delo": delo_by_request.get(rid)}
        if status in DONE_STATUSES:
            items.append(entry)
        else:
            pending.append({k: entry[k] for k in
                            ("id", "title", "title_short", "status", "created_at", "delo")})
    items.sort(key=lambda it: (it.get("finished_at") or 0, it.get("created_at") or 0), reverse=True)
    pending.sort(key=lambda it: it.get("created_at") or 0, reverse=True)

    without = [dict(item) for item in pending + items if not item.get("delo")]
    without.sort(key=lambda it: it.get("finished_at") or it.get("created_at") or 0, reverse=True)
    return {"room": str(room_id),
            "generated_at": int(time.time()),
            "items": items,
            "pending": pending,
            "reports": reps,
            "without_delo": without,
            "counts": {"requests": len(records),
                       "results": len(items),
                       "pending": len(pending),
                       "reports": len(reps),
                       "without_delo": len(without)}}


def without_delo(cfg, room_id, delo_list, board=None, states=None,
                 limit: int = REQUEST_LIMIT) -> list:
    """Запросы комнаты, из которых ещё не сделали дело (свежие сверху)."""
    have = {str((item or {}).get("request_id") or "") for item in (delo_list or [])}
    records = _records(cfg, room_id)[:max(0, int(limit or REQUEST_LIMIT))]
    ids = [str(rec.get("root_id") or "") for rec in records if rec.get("root_id")]
    st = states if states is not None else states_of(cfg, board, ids)
    out = []
    for rec in records:
        rid = str(rec.get("root_id") or "")
        if not rid or rid in have:
            continue
        state = (st or {}).get(rid) if isinstance(st, dict) else None
        state = state if isinstance(state, dict) else {}
        out.append({"id": rid, "title": str(rec.get("title") or ""),
                    "status": str(state.get("status") or ""),
                    "created_at": int(rec.get("created_at") or 0),
                    "finished_at": int(state.get("finished_at") or 0)})
    out.sort(key=lambda it: it.get("finished_at") or it.get("created_at") or 0, reverse=True)
    return out


# --------------------------------------------------- предложение этапов карты --
def phases_from_requests(cfg, room, items, limit: int = PHASE_LIMIT) -> dict:
    """Эвристика: этапы карты проекта по ВЫПОЛНЕННЫМ запросам (без запуска модели).

    Один дешёвый проход по названиям и «Коротко» готовых запросов: каждый
    завершённый запрос — этап, который уже пройден. Результат кладётся как
    ОБЫЧНОЕ предложение этапов комнаты (`phases_proposal`), поэтому человек
    решает его штатно: принять все / выбранные / поправить / отклонить, и оно
    же появляется пунктом в «От вас нужно».
    """
    try:
        from . import phase_book
    except ImportError:                  # pragma: no cover
        import phase_book                # type: ignore
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "").strip()
    if not room_id:
        return {"ok": False, "reason": "no_room",
                "message": "Не передана комната — этапы предлагать некуда"}
    phases = []
    seen = set()
    for req in items or []:
        if not isinstance(req, dict):
            continue
        title = " ".join(str(req.get("title_short") or req.get("title") or "").split())
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        phases.append({"title": title[:80],
                       "description": str(req.get("short") or "")[:200]})
        if len(phases) >= max(1, int(limit or PHASE_LIMIT)):
            break
    if not phases:
        return {"ok": False, "reason": "no_results",
                "message": "Нет завершённых запросов — этапы пока не из чего собрать"}
    phases.append({"title": "Итоги и следующий шаг",
                   "description": "Что делаем дальше по проекту: приоритет и следующий запрос."})
    got = phase_book.capture(room, phases, by="по выполненным запросам", source="requests")
    if not got.get("applied"):
        reason = got.get("reason") or "unchanged"
        text = {"empty": "Предложение пустое — этапы не собраны",
                "unchanged": "Такое предложение уже ждёт вашего решения",
                "same_as_phases": "Этапы комнаты уже такие — предлагать нечего"}.get(
                    reason, "Предложение этапов не изменилось")
        return {"ok": False, "reason": reason, "message": text,
                "proposal": got.get("proposal")}
    return {"ok": True, "reason": "proposed", "proposal": got["proposal"],
            "message": "Предложены этапы: %d. Принять, поправить или отклонить — на карте проекта"
                       % len(phases)}


# --------------------------------------------- «предлагаю собрать карту» ------
def _offer_path(cfg, room_id) -> Path:
    base = getattr(cfg, "state_dir", None) or (Path(cfg.data_dir) / "state")
    return Path(base) / str(room_id) / OFFER_FILE


def offer_state(cfg, room_id) -> dict:
    path = _offer_path(cfg, room_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dismiss_offer(cfg, room_id, at=None) -> dict:
    """«Не сейчас»: пункт «Предлагаю собрать карту» уходит на OFFER_COOLDOWN."""
    stamp = int(at or time.time())
    path = _offer_path(cfg, room_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write_json_atomic(path, {"room": str(room_id), "dismissed_at": stamp})
    except Exception as exc:             # noqa: BLE001
        return {"ok": False, "message": "Не удалось отложить предложение: %s" % exc}
    return {"ok": True, "dismissed_at": stamp}


def roadmap_offer(cfg, room, board=None, now=None, states=None) -> dict:
    """Пункт «Предлагаю собрать карту проекта» для «От вас нужно» (или None).

    Условия: у комнаты НЕТ своих этапов, нет ждущего предложения этапов, есть
    хотя бы один завершённый запрос и человек не сказал «Не сейчас» (14 дней).
    Пункт живёт в ответе, файл бэклога не меняется — как и строка предложения
    этапов (`ingest._proposal_item`).
    """
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "").strip()
    if not room_id:
        return None
    try:
        from . import phase_book
    except ImportError:                  # pragma: no cover
        import phase_book                # type: ignore
    if phase_book.norm(room.get("phases")):
        return None                       # карта уже собрана
    if phase_book.proposal_of(room) is not None:
        return None                       # предложение уже ждёт решения
    # Доска — из комнаты: без неё `states_of` вернёт пусто и «завершённых» не будет.
    board = (room.get("board") or board or "team")
    stamp = int(now or time.time())
    seen = offer_state(cfg, room_id)
    if int(seen.get("dismissed_at") or 0) and stamp - int(seen["dismissed_at"]) < OFFER_COOLDOWN:
        return None
    records = _records(cfg, room_id)
    ids = [str(rec.get("root_id") or "") for rec in records if rec.get("root_id")]
    if not ids:
        return None
    st = states if states is not None else states_of(cfg, board, ids)
    done = [rec for rec in records
            if str((st.get(str(rec.get("root_id"))) or {}).get("status") or "") in DONE_STATUSES]
    if not done:
        return None
    titles = [str(rec.get("title") or "") for rec in done[:3]]
    return {"id": "roadmap_offer",
            "kind": "roadmap_offer",
            "status": "open",
            "text": "Предлагаю собрать карту проекта: завершённых запросов — %d" % len(done),
            "why": "Этапы соберу по выполненным запросам («%s»). Принять, поправить или "
                   "отклонить — ваше решение; этапы комнаты не меняются."
                   % "», «".join(t for t in titles if t),
            "source_title": str(room.get("title") or room_id),
            "source_request": None,
            "owner": room.get("orchestrator"),
            "due": None,
            "created_at": stamp,
            "updated_at": stamp,
            "count": len(done),
            "titles": [t for t in titles if t],
            "actions": [
                {"id": "build", "label": "Собрать карту"},
                {"id": "later", "label": "Не сейчас"},
            ]}
