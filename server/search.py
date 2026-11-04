"""Поиск по комнатам: запросы, решения, бэклог, журнал, этапы.

Публичный интерфейс:
    build_index(cfg, room_id) -> dict            # строит и кэширует индекс
    query(cfg, q, room=None, limit=30) -> list[dict]
    clear_cache() -> None

Особенности:
  * без внешних библиотек и без индекса на диске — всё в памяти;
  * TTL кэша 10 с (комнаты меняются редко, но переиндексация дешёвая);
  * нормализация ``ё→е`` и регистра, токенизация ``\\w+``;
  * префиксный матч по первым 5 символам слова — дешёвая «морфология»
    (``легализац`` находит ``легализация``);
  * сниппет ±80 символов вокруг первого вхождения + диапазоны для подсветки.

Хит: ``{type, kind, room, id, title, snippet, score, ranges, at, route}``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

try:  # import server.search
    from .config import Cfg
except ImportError:  # python server/app.py
    from config import Cfg

TTL = 10.0
PREFIX = 5
SNIPPET_PAD = 80
WEIGHTS = {"request": 1.3, "decision": 1.15, "backlog": 1.0, "journal": 0.9, "phase": 0.8}
TYPE_ORDER = {"request": 0, "decision": 1, "backlog": 2, "journal": 3, "phase": 4}
WORD_RE = re.compile(r"\w+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")

_CACHE = {}  # room_id -> (expires_monotonic, docs)


# --- нормализация ------------------------------------------------------------
def fold(text) -> str:
    """Нижний регистр + ё→е, длина строки сохраняется (индексы совпадают)."""
    out = []
    for ch in "" if text is None else str(text):
        low = ch.lower()
        if len(low) != 1:
            out.append(ch)
            continue
        out.append("е" if low == "ё" else low)
    return "".join(out)


def _flat(text) -> str:
    """Схлопнуть пробелы (переводы строк → пробел), чтобы сниппет был одной строкой."""
    return SPACE_RE.sub(" ", "" if text is None else str(text))


def tokens(text: str) -> list:
    """[(слово, начало, конец)] по нормализованной строке."""
    return [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(text)]


def _tok_score(qt: str, words) -> float:
    """3 — точное слово, 2.6 — префикс, 2.1 — общие первые буквы, 1.1 — вхождение."""
    best = 0.0
    # для коротких слов берём префикс короче, иначе «цена» не находит «цену»
    plen = min(PREFIX, max(3, len(qt) - 1))
    plen = min(plen, len(qt))
    for dt in words:
        if dt == qt:
            return 3.0
        if dt.startswith(qt):
            best = max(best, 2.6)
        elif len(dt) >= plen and len(qt) >= 2 and dt[:plen] == qt[:plen]:
            best = max(best, 2.1)
        elif len(qt) >= 4 and qt in dt:
            best = max(best, 1.1)
    return best


# --- источники ---------------------------------------------------------------
def _rooms(cfg: Cfg) -> list:
    """Список id комнат: rooms.json или каталоги данных."""
    try:
        from . import rooms as rooms_mod
    except Exception:
        rooms_mod = None
    if rooms_mod is not None:
        try:
            got = rooms_mod.list_rooms(cfg)
            ids = [r.get("id") if isinstance(r, dict) else getattr(r, "id", None) for r in got or []]
            ids = [str(x) for x in ids if x]
            if ids:
                return ids
        except Exception:
            pass
    p = Path(cfg.data_dir) / "rooms.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        ids = [str(r["id"]) for r in data.get("rooms") or [] if isinstance(r, dict) and r.get("id")]
        if ids:
            return ids
    except (OSError, ValueError):
        pass
    found = set()
    for sub in ("state", "reports"):
        d = Path(cfg.data_dir) / sub
        if d.is_dir():
            found.update(x.name for x in d.iterdir() if x.is_dir())
    return sorted(found)


def _list_requests(cfg: Cfg, room_id: str, limit: int = 500) -> list:
    try:
        from . import registry
    except Exception:
        registry = None
    if registry is not None:
        try:
            got = registry.list_requests(cfg, room_id, limit)
            if isinstance(got, list):
                return got
        except Exception:
            pass
    p = Path(cfg.data_dir) / "state" / room_id / "requests.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("requests")
    return [x for x in data or [] if isinstance(x, dict)][:limit]


def _load_state(cfg: Cfg, room_id: str) -> dict:
    try:
        from . import ingest
    except Exception:
        ingest = None
    if ingest is not None:
        try:
            got = ingest.load_state(cfg, room_id)
            if isinstance(got, dict):
                return got
        except Exception:
            pass
    p = Path(cfg.data_dir) / "state" / room_id / "state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _route(room: str, kind: str, ident: str) -> str:
    if kind in ("request",):
        return "#/room/%s/request/%s" % (room, ident)
    if kind == "backlog":
        return "#/room/%s/backlog" % room
    if kind == "journal":
        return "#/room/%s/journal" % room
    return "#/room/%s/roadmap" % room


def _doc(kind: str, room: str, ident: str, title: str, body: str, at=None, route=None,
         request_id=None) -> dict:
    title = _flat(title).strip()
    body = _flat(body).strip()
    return {
        "type": kind, "kind": kind, "room": room, "id": str(ident),
        "title": title or body[:80] or kind,
        "body": body,
        "at": int(at) if isinstance(at, (int, float)) else None,
        "request_id": request_id or (str(ident) if kind == "request" else None),
        "route": route or _route(room, kind, str(ident)),
    }


# --- сборка индекса ----------------------------------------------------------
def _collect(cfg: Cfg, room_id: str) -> list:
    docs = []

    try:
        requests = _list_requests(cfg, room_id, 500)
    except Exception:  # реестр не должен ронять поиск
        requests = []
    for req in requests or []:
        if not isinstance(req, dict):
            continue
        root = req.get("root_id") or req.get("id")
        if not root:
            continue
        title = req.get("title") or _flat(req.get("text") or "")[:80]
        body = " ".join(str(x) for x in (req.get("text"), req.get("title"), req.get("slug"),
                                         req.get("summary")) if x)
        docs.append(_doc("request", room_id, root, title, body, req.get("created_at"),
                         request_id=str(root)))

    try:
        state = _load_state(cfg, room_id)
    except Exception:  # битый ingest не должен ронять поиск
        state = {}
    if not isinstance(state, dict):
        state = {}

    for dec in state.get("decisions") or []:
        if not isinstance(dec, dict):
            continue
        ident = dec.get("id") or dec.get("text")
        text = str(dec.get("text") or "")
        body = " ".join(str(x) for x in (text, dec.get("why"), dec.get("owner"),
                                         dec.get("status")) if x)
        docs.append(_doc("decision", room_id, ident, text, body, dec.get("at") or dec.get("updated_at"),
                         request_id=dec.get("source_request")))

    backlog = state.get("backlog")
    if isinstance(backlog, dict):
        backlog = backlog.get("items")
    for item in backlog or []:
        if not isinstance(item, dict):
            continue
        ident = item.get("id") or item.get("text")
        text = str(item.get("text") or "")
        body = " ".join(str(x) for x in (text, item.get("owner"), item.get("due"),
                                         item.get("status"), item.get("source_title")) if x)
        docs.append(_doc("backlog", room_id, ident, text, body,
                         item.get("updated_at") or item.get("created_at"),
                         request_id=item.get("source_request")))

    journal = state.get("journal")
    if isinstance(journal, dict):
        journal = journal.get("items")
    for ev in journal or []:
        if not isinstance(ev, dict):
            continue
        ident = ev.get("id") or "ev-%s" % ev.get("at")
        text = str(ev.get("text") or ev.get("kind") or "")
        detail = ev.get("detail")
        extra = ""
        if isinstance(detail, dict):
            extra = " ".join(str(detail.get(k)) for k in ("summary", "answer", "title", "path", "text")
                             if detail.get(k))
        elif detail:
            extra = str(detail)
        body = " ".join(x for x in (text, ev.get("kind"), ev.get("actor"), extra) if x)
        docs.append(_doc("journal", room_id, ident, text, body, ev.get("at"),
                         request_id=ev.get("request_id")))

    roadmap = state.get("roadmap")
    phases = roadmap.get("phases") if isinstance(roadmap, dict) else roadmap
    for phase in phases or []:
        if not isinstance(phase, dict):
            continue
        ident = "phase:%s" % (phase.get("id") or phase.get("title"))
        title = str(phase.get("title") or phase.get("id") or "")
        body = " ".join(str(x) for x in (title, phase.get("note"), phase.get("status")) if x)
        docs.append(_doc("phase", room_id, ident, title, body,
                         phase.get("updated_at"), request_id=None))

    for d in docs:
        d["title_flat"] = fold(d["title"])
        d["body_flat"] = fold(d["body"])
        d["title_words"] = [t[0] for t in tokens(d["title_flat"])]
        d["body_words"] = [t[0] for t in tokens(d["body_flat"])]
    return docs


def build_index(cfg: Cfg, room_id: str) -> dict:
    """Перестроить индекс комнаты и положить в кэш на TTL секунд."""
    docs = _collect(cfg, room_id)
    _CACHE[str(room_id)] = (time.monotonic() + TTL, docs)
    by_type = {}
    for d in docs:
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1
    return {"room": str(room_id), "docs": len(docs), "by_type": by_type,
            "built_at": int(time.time()), "ttl_s": TTL}


def _docs(cfg: Cfg, room_id: str, rebuild: bool = False) -> list:
    key = str(room_id)
    if not rebuild:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    build_index(cfg, room_id)
    return _CACHE[key][1]


def clear_cache() -> None:
    """Сбросить кэш (тесты, после ingest)."""
    _CACHE.clear()


# --- поиск -------------------------------------------------------------------
def _snippet(doc: dict, qfold: str, qwords) -> tuple:
    """(сниппет, ranges[[s,e]]) по первому вхождению запроса."""
    body = doc["body"]
    flat = doc["body_flat"]
    best = None
    for qt in qwords:
        plen = min(PREFIX, max(3, len(qt) - 1))
        plen = min(plen, len(qt))
        for word, start, _end in tokens(flat):
            if word == qt or word.startswith(qt) or (len(word) >= plen and len(qt) >= 2
                                                     and word[:plen] == qt[:plen]):
                best = (start, start + len(word))
                break
        if best:
            break
    if best is None:
        whole = flat.find(qfold)
        if whole >= 0:
            best = (whole, whole + len(qfold))
    if best is None:
        text = _flat(doc["title"])[:SNIPPET_PAD * 2]
        return text, []

    start = max(0, best[0] - SNIPPET_PAD)
    end = min(len(body), best[1] + SNIPPET_PAD)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    snippet = prefix + body[start:end].strip() + suffix
    shift = len(prefix) - len(body[start:end]) + len(body[start:end].lstrip())
    r0 = max(0, best[0] - start) + shift
    r1 = min(len(snippet), r0 + (best[1] - best[0]))
    return snippet, [[r0, r1]]


def _score(doc: dict, qwords, qfold: str):
    total, matched = 0.0, 0
    for qt in qwords:
        s_body = _tok_score(qt, doc["body_words"])
        s_title = _tok_score(qt, doc["title_words"])
        val = max(s_body, s_title * 1.6)
        if val <= 0:
            continue
        matched += 1
        total += val
    if not matched:
        return 0.0
    coverage = matched / len(qwords)
    score = (total / len(qwords)) * WEIGHTS.get(doc["type"], 1.0) * (0.6 + 0.4 * coverage)
    if qfold and qfold in doc["title_flat"]:
        score += 1.2
    if qfold and qfold in doc["body_flat"]:
        score += 0.8
    if matched < len(qwords):
        score *= 0.55  # неполное совпадение — ниже полного
    return score


def query(cfg: Cfg, q, room=None, limit: int = 30) -> list:
    """Найти ``q`` по комнате(ам). Возвращает список хитов, лучшие первыми."""
    text = _flat(q).strip()
    if len(text) < 1:
        return []
    qfold = fold(text)
    qwords = [w for w, _s, _e in tokens(qfold) if w]
    if not qwords:
        return []
    try:
        limit = max(1, min(int(limit or 30), 500))
    except (TypeError, ValueError):
        limit = 30

    room_ids = [str(room)] if room else _rooms(cfg)
    hits, seen = [], set()
    for rid in room_ids:
        for doc in _docs(cfg, rid):
            score = _score(doc, qwords, qfold)
            if score <= 0:
                continue
            uniq = (doc["type"], doc["room"], doc["id"])
            if uniq in seen:
                continue
            seen.add(uniq)
            snippet, ranges = _snippet(doc, qfold, qwords)
            hits.append({
                "type": doc["type"], "kind": doc["type"], "room": doc["room"],
                "id": doc["id"], "title": doc["title"], "snippet": snippet,
                "score": round(score, 3), "ranges": ranges, "at": doc.get("at"),
                "route": doc.get("route") or _route(doc["room"], doc["type"], doc["id"]),
                "request_id": doc.get("request_id"),
            })

    hits.sort(key=lambda h: (-h["score"], -int(h.get("at") or 0),
                             TYPE_ORDER.get(h["type"], 9), h["id"]))
    return hits[:limit]


def total(cfg: Cfg, q, room=None) -> int:
    """Сколько всего совпадений (для пагинации API; ограничение — 500)."""
    return len(query(cfg, q, room=room, limit=500))


__all__ = ["build_index", "query", "total", "clear_cache", "fold", "tokens", "TTL"]
