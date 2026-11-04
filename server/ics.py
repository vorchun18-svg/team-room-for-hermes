"""Экспорт «Дальше» в календарь (.ics, RFC 5545).

Публичный интерфейс:
    make_ics(items, room_title) -> str
    ics_for_items(cfg, room_id, ids=None, all_open=False, meta=None) -> str

Правила:
  * CRLF в каждой строке, фолдинг по 75 октетов UTF-8 без разрыва символа;
  * события на весь день: ``DTSTART;VALUE=DATE`` (+ ``DTEND`` на следующий день);
  * элемент без ``due`` не попадает в календарь, счётчик — в ``X-WR-CALDESC``;
  * UID стабилен: ``<комната>-<id элемента>@team-room.local``;
  * SUMMARY/DESCRIPTION экранируются (``;``, ``,``, ``\\``, перевод строки).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:  # import server.ics
    from .config import Cfg
except ImportError:  # python server/app.py
    from config import Cfg

PRODID = "-//Team Room//reportkit 1.0//RU"
UID_DOMAIN = "team-room.local"
SUMMARY_MAX = 200
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
STATUS_RU = {"open": "открыт", "asked": "спрошен", "done": "готово", "dismissed": "снят"}
KIND_RU = {"next_step": "шаг", "future_question": "будущий вопрос",
           "open_question": "вопрос", "decision": "решение"}


# --- текст -------------------------------------------------------------------
def escape_text(value) -> str:
    """RFC 5545 TEXT: обратный слэш, ; , и переводы строк."""
    s = "" if value is None else str(value)
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return s


def unescape_text(value: str) -> str:
    """Обратное к escape_text (нужно тестам и отладке)."""
    out, i, s = [], 0, value or ""
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt in ("\\", ";", ","):
                out.append(nxt)
            else:
                out.append(ch + nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def fold_line(line: str, limit: int = 75) -> list:
    """Разбить строку на физические строки ≤ limit октетов (UTF-8 безопасно)."""
    if len(line.encode("utf-8")) <= limit:
        return [line]
    out, cur, size, budget = [], [], 0, limit
    for ch in line:
        b = len(ch.encode("utf-8"))
        if size + b > budget:
            out.append("".join(cur))
            cur, size, budget = [ch], b, limit - 1  # продолжение начинается с пробела
        else:
            cur.append(ch)
            size += b
    if cur:
        out.append("".join(cur))
    return [out[0]] + [" " + part for part in out[1:]]


def _one_line(value) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _clip(value, limit: int = SUMMARY_MAX) -> str:
    s = _one_line(value)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _parse_date(value):
    """'YYYY-MM-DD' (или ISO с временем) -> date; None, если нет/кривая."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    m = DATE_RE.match(value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _slug(value) -> str:
    s = re.sub(r"[^0-9A-Za-z_.-]+", "-", _one_line(value)).strip("-.")
    return s or "item"


def _uid(item: dict) -> str:
    if item.get("uid"):
        return _slug(item["uid"])
    base = _slug(item.get("id") or hashlib.sha1(
        _one_line(item.get("text") or "").encode("utf-8")).hexdigest()[:8])
    room = item.get("room")
    return ("%s-%s@%s" % (_slug(room), base, UID_DOMAIN)) if room else ("%s@%s" % (base, UID_DOMAIN))


def _description(item: dict) -> str:
    parts = []
    owner = item.get("owner")
    if owner:
        parts.append("Кто: " + ("Михаил" if str(owner) == "mikhail" else str(owner)))
    status = STATUS_RU.get(str(item.get("status") or ""), str(item.get("status") or ""))
    if status:
        parts.append("Статус: " + status)
    kind = KIND_RU.get(str(item.get("kind") or ""), "")
    if kind:
        parts.append("Тип: " + kind)
    if item.get("source_title"):
        parts.append("Источник: " + str(item["source_title"]))
    if item.get("source_request"):
        parts.append("Запрос: " + str(item["source_request"]))
    return escape_text(" · ".join(parts))


def _event(item: dict, day: date, stamp: str) -> list:
    summary = escape_text(_clip(item.get("text") or item.get("title") or "Шаг"))
    lines = ["BEGIN:VEVENT",
             "UID:" + _uid(item),
             "DTSTAMP:" + stamp,
             "DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"),
             "DTEND;VALUE=DATE:" + (day + timedelta(days=1)).strftime("%Y%m%d"),
             "SUMMARY:" + summary]
    desc = _description(item)
    if desc:
        lines.append("DESCRIPTION:" + desc)
    lines += ["STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"]
    return lines


def make_ics(items, room_title: str) -> str:
    """Собрать .ics: события на весь день, без срока — пропуск (счётчик в шапке)."""
    title = _clip(room_title or "Комната команды", 200)
    rows, skipped = [], 0
    for item in items or []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        day = _parse_date(item.get("due"))
        if day is None:
            skipped += 1
            continue
        rows.append((day, _one_line(item.get("text") or ""), item))
    rows.sort(key=lambda r: (r[0], r[1]))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    desc = "Экспорт «Комнаты команды», %s. Событий: %d." % (title, len(rows))
    if skipped:
        desc += " Пропущено без срока: %d." % skipped
    lines = ["BEGIN:VCALENDAR",
             "VERSION:2.0",
             "PRODID:" + PRODID,
             "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH",
             "X-WR-CALNAME:" + escape_text(title),
             "X-WR-CALDESC:" + escape_text(desc)]
    for day, _text, item in rows:
        lines.extend(_event(item, day, stamp))
    lines.append("END:VCALENDAR")

    out = []
    for line in lines:
        out.extend(fold_line(line))
    return "\r\n".join(out) + "\r\n"


# --- источники ---------------------------------------------------------------
def _load_state(cfg: Cfg, room_id: str) -> dict:
    try:
        from . import ingest
    except Exception:
        ingest = None
    if ingest is not None:
        try:
            state = ingest.load_state(cfg, room_id)
            if isinstance(state, dict):
                return state
        except Exception:
            pass
    p = Path(cfg.data_dir) / "state" / room_id / "state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_backlog(cfg: Cfg, room_id: str) -> list:
    """Элементы «Дальше» комнаты (ingest.load_state -> backlog)."""
    state = _load_state(cfg, room_id)
    items = state.get("backlog")
    if isinstance(items, dict):
        items = items.get("items")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    p = Path(cfg.data_dir) / "state" / room_id / "backlog.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("items")
    return [x for x in data or [] if isinstance(x, dict)]


def _room_title(cfg: Cfg, room_id: str) -> str:
    try:
        from . import rooms
    except Exception:
        rooms = None
    if rooms is not None:
        try:
            room = rooms.get_room(cfg, room_id)
            if isinstance(room, dict) and room.get("title"):
                return str(room["title"])
            if room is not None and getattr(room, "title", None):
                return str(room.title)
        except Exception:
            pass
    p = Path(cfg.data_dir) / "rooms.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return room_id
    for room in data.get("rooms") or []:
        if isinstance(room, dict) and room.get("id") == room_id and room.get("title"):
            return str(room["title"])
    return room_id


def _matches(item: dict, wanted: set) -> bool:
    ident = str(item.get("id") or "")
    return ident in wanted or (":" in ident and ident.split(":", 1)[1] in wanted)


def ics_for_items(cfg: Cfg, room_id: str, ids=None, all_open=False, meta=None) -> str:
    """Календарь по элементам бэклога комнаты.

    ``ids`` — id элементов (``<root>:ns_1`` или короткие ``ns_1``);
    ``all_open`` — все открытые элементы со сроком. ``meta`` (dict) заполняется
    счётчиками ``count``/``skipped``/``title``/``missing`` — для HTTP-слоя.
    """
    items = _load_backlog(cfg, room_id)
    title = _room_title(cfg, room_id)
    wanted = {str(x).strip() for x in (ids or []) if str(x).strip()}
    missing = []

    if wanted:
        selected = []
        for ident in wanted:
            hit = None
            for item in items:
                if _matches(item, {ident}):
                    hit = item
                    break
            if hit is None:
                missing.append(ident)
            elif hit not in selected:
                selected.append(hit)
    elif all_open:
        selected = [it for it in items if str(it.get("status") or "open") in ("open", "asked")]
    else:
        selected = []

    prepared = []
    for item in selected:
        row = dict(item)
        row.setdefault("room", room_id)
        prepared.append(row)

    text = make_ics(prepared, title)
    if isinstance(meta, dict):
        with_due = [it for it in prepared if _parse_date(it.get("due"))]
        meta.update({"room": room_id, "title": title, "count": len(with_due),
                     "skipped": len(prepared) - len(with_due), "missing": missing,
                     "selected": len(prepared)})
    return text


__all__ = ["make_ics", "ics_for_items", "fold_line", "escape_text", "unescape_text"]
