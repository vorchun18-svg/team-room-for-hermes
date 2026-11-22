# -*- coding: utf-8 -*-
"""Мок ручек партии B (W29-AC2): совместимость, второе мнение, итог дня,
поиск по памяти и настройки уведомлений.

Мод `mock_read`/`mock_write` спрашивает этот модуль ПЕРВЫМ: ``handle(app, method,
path, query, body)`` возвращает ``(status, payload)`` или ``None``, если путь не
наш. Так демо отвечает той же формой, что боевой сервер (сверка — по
``docs/API.md`` и тестам пакетов Q2/Q4/Q6/Q8).

Ничего не читает с диска и не пишет: только память демо-сервера.
"""
from __future__ import annotations

import copy
import re
import time

#: Виды уведомлений — как ``server/notify.py:KINDS``.
KINDS = (("waiting", "Ждёт ваш ответ"), ("finished", "Запрос завершён"),
         ("error", "Ошибка запуска"), ("duty", "Дежурство нашло изменения"))

#: Метки ответа второго мнения — как ``server/second_opinion.py``.
_MARKERS = (("НЕ СОГЛАСНА", "disagree"), ("НЕ ХВАТАЕТ", "missing"), ("СОГЛАСНА", "agree"))
VERDICT_RU = {"agree": "согласна", "disagree": "не согласна", "missing": "не хватает данных"}
TITLE_PREFIX = "Второе мнение"


def _now() -> int:
    return int(time.time())


def state(app) -> dict:
    """Память демо для этих ручек (создаётся при первом обращении)."""
    box = getattr(app, "batchb", None)
    if isinstance(box, dict):
        return box
    box = {"notify": {
        "enabled": False,
        "kinds": {kind: True for kind, _title in KINDS},
        "quiet_hours": {"enabled": True, "from": "22:00", "to": "08:00"},
        "min_interval_s": 300,
    }, "history": []}
    app.batchb = box
    return box


# ── совместимость (W29B-Q2) ────────────────────────────────────────────────

def _clean(text, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def _skill_totals(profile: str) -> dict:
    own = 4 + (len(profile) % 5)
    shared = 12
    return {"own": own, "shared": shared, "total": max(own, shared)}


def parity(app, room_id: str) -> tuple:
    room = app.room(room_id)
    members = []
    for member in room.get("members") or []:
        profile = str(member.get("profile") or "")
        chosen = member.get("model_settings") or {}
        default = member.get("agent_default") or {}
        source = "room" if str(chosen.get("source") or "") == "room" else "profile"
        model = {"model": chosen.get("model") or default.get("model") or "",
                 "provider": chosen.get("provider") or default.get("provider") or "",
                 "reasoning": chosen.get("reasoning") or default.get("reasoning") or "",
                 "source": source}
        diff_model = (source == "room"
                      and (model["model"] != default.get("model")
                           or model["provider"] != default.get("provider")))
        diffs = []
        status = "ok"
        if diff_model:
            status = "fail"
            diffs.append({
                "code": "model_differs", "severity": "fail",
                "text": "Комната запускает «%s» на %s · %s, а профиль Hermes — на %s · %s."
                        % (member.get("note") or profile, model["provider"], model["model"],
                           default.get("provider") or "—", default.get("model") or "—"),
                "fix": {"kind": "reset_room_model", "label": "Вернуть модель профиля"}})
        elif not default.get("model"):
            status = "warn"
            diffs.append({
                "code": "model_unknown", "severity": "warn",
                "text": "В профиле «%s» модель не задана — действует общая настройка Hermes." % profile,
                "fix": {"kind": "open_models", "label": "Открыть «Команду»"}})
        skills = _skill_totals(profile)
        members.append({
            "profile": profile,
            "role": str(member.get("role") or "worker"),
            "label": _clean(member.get("note") or profile, 80).split(":")[0],
            "description": _clean(member.get("note"), 160),
            "profile_dir": "~/.hermes/profiles/" + profile,
            "read_by": "profile",
            "status": status,
            "model": model,
            "profile_model": {"model": default.get("model") or "", "provider": default.get("provider") or "",
                              "reasoning": default.get("reasoning") or ""},
            "skills": skills,
            "tools": {"count": 3, "names": ["файлы", "терминал", "веб"]},
            "mcp": {"count": 1, "names": ["kb"]},
            "permissions": {"mode": "ask", "level": "working", "level_label": "Работает сам",
                            "single_query_mode": "deny", "allowlist_count": 2},
            "differences": ["Запуск в комнате получает контекст комнаты и правило папки проекта."],
            "diffs": diffs,
        })
    summary = {key: sum(1 for m in members if m["status"] == key) for key in ("ok", "warn", "fail", "off")}
    summary["total"] = len(members)
    return 200, {"room": room_id, "room_title": room.get("title"), "level": "working",
                 "level_label": "Работает сам", "members": members, "summary": summary,
                 "fixable": [d["fix"] for m in members for d in m["diffs"]
                             if (d.get("fix") or {}).get("kind") == "reset_room_model"],
                 "notes": ["Экран только читает: профили Hermes не меняются."],
                 "at": _now()}


def parity_fix(app, room_id: str, body: dict) -> tuple:
    profile = str((body or {}).get("profile") or "")
    if str((body or {}).get("action") or "") != "reset_model" or not profile:
        return 400, {"error": {"code": "bad_request", "message": "Нужно действие reset_model и участник",
                               "detail": {"field": "action"}}}
    room = app.room(room_id)
    for member in room.get("members") or []:
        if str(member.get("profile")) == profile:
            default = member.get("agent_default") or {}
            chosen = member.setdefault("model_settings", {})
            chosen.update({"provider": default.get("provider") or "", "model": default.get("model") or "",
                           "reasoning": default.get("reasoning") or "", "source": "agent"})
            break
    else:
        return 404, {"error": {"code": "not_found", "message": "В комнате нет такого участника",
                               "detail": {"profile": profile}}}
    return parity(app, room_id)


# ── второе мнение (W29B-Q4) ────────────────────────────────────────────────

def _catalog_models(app) -> list:
    out = []
    for prov in (app.catalog().get("providers") or []):
        pid = str(prov.get("id") or prov.get("name") or "")
        plabel = str(prov.get("label") or pid)
        for item in prov.get("models") or []:
            model = item.get("id") if isinstance(item, dict) else str(item)
            label = (item.get("label") or item.get("name") or model) if isinstance(item, dict) else model
            if model:
                out.append({"provider": pid, "provider_label": plabel, "model": str(model),
                            "label": str(label)})
    return out


def _svod_of(detail: dict) -> dict:
    svod = detail.get("synthesis") if isinstance(detail, dict) else None
    svod = svod if isinstance(svod, dict) else {}
    text = str(svod.get("text_md") or "")
    return {"text": text, "version": int(svod.get("version") or 0)}


def _parse_check(text: str) -> dict:
    items, counts = [], {"agree": 0, "disagree": 0, "missing": 0}
    for line in str(text or "").splitlines():
        clean = line.strip().lstrip("-*• ").strip()
        for marker, verdict in _MARKERS:
            if clean.startswith(marker):
                rest = clean[len(marker):].lstrip(": —-").strip()
                quote, _, why = rest.partition("—")
                quote = quote.strip().strip("«»\" ").strip()
                if not quote:
                    continue
                items.append({"verdict": verdict, "label": VERDICT_RU[verdict],
                              "quote": quote, "text": why.strip() or quote})
                counts[verdict] += 1
                break
    return {"items": items, "counts": counts,
            "source": "format" if items else "raw", "summary": ""}


def _checks(detail: dict) -> list:
    out = []
    for node in detail.get("tree") or []:
        if not isinstance(node, dict) or not str(node.get("title") or "").startswith(TITLE_PREFIX):
            continue
        summary = str(node.get("summary_md") or "")
        parsed = _parse_check(summary) if summary else {"items": [], "counts": {k: 0 for k in VERDICT_RU},
                                                        "source": "empty", "summary": ""}
        status = str(node.get("status") or "")
        out.append({"card_id": str(node.get("id") or ""), "title": str(node.get("title") or ""),
                    "at": int(node.get("created_at") or 0), "finished_at": int(node.get("finished_at") or 0),
                    "status": status,
                    "running": status in ("todo", "ready", "running", "review", "blocked"),
                    "failed": status in ("failed", "cancelled") or bool(node.get("error")),
                    "assignee": str(node.get("assignee") or ""), "model": str(node.get("model_label") or ""),
                    "items": parsed["items"], "counts": parsed["counts"],
                    "counts_text": "", "text": summary, "source": parsed["source"],
                    "error": str(node.get("error") or "")})
    out.sort(key=lambda c: (c["at"], c["card_id"]))
    return out


def second_opinion(app, rid: str) -> tuple:
    detail = app.detail(rid)
    svod = _svod_of(detail)
    models = _catalog_models(app)
    author = {"model": "", "provider": ""}
    tree = [n for n in detail.get("tree") or [] if n.get("role") == "root"]
    if tree:
        label = str(tree[0].get("model_label") or "")
        parts = [p.strip() for p in label.split("·")]
        if len(parts) >= 2:
            author = {"provider": parts[0], "model": parts[1]}
    default = next((m for m in models if m["model"] != author.get("model")), (models[0] if models else None))
    checks = _checks(detail)
    running = next((c for c in checks if c["running"]), None)
    can_run = bool(svod["text"]) and bool(models) and not running
    why_not = ""
    if not svod["text"]:
        why_not = "Свода ещё нет — проверять нечего."
    elif not models:
        why_not = "Список моделей пуст: выбрать модель для проверки нельзя."
    elif running:
        why_not = "Проверка уже идёт — дождитесь её ответа."
    text = svod["text"]
    return 200, {"ok": True, "version": svod["version"], "author": author, "models": models,
                 "warning": "", "default": default,
                 "estimate": {"input_tokens": 12000 + len(text) // 4, "output_tokens": 700,
                              "cost_usd": None},
                 "checks": checks, "can_run": can_run, "why_not": why_not,
                 "running": bool(running)}


def second_opinion_run(app, rid: str, body: dict) -> tuple:
    """Действие ``second_opinion``: карточка-проверка в дереве демо (без модели)."""
    detail = app.detail(rid)
    provider = str((body or {}).get("provider") or "")
    model = str((body or {}).get("model") or "")
    if not model:
        return 400, {"error": {"code": "bad_request", "message": "Выберите модель для проверки",
                               "detail": {"field": "model"}}}
    if any(c["running"] for c in _checks(detail)):
        return 409, {"error": {"code": "conflict", "message": "Проверка уже идёт — дождитесь ответа",
                               "detail": {}}}
    tree = detail.get("tree") or []
    index = 1000 + len(tree)
    card_id = "t_ac2so%03d" % index
    now = _now()
    svod_text = _svod_of(detail)["text"]
    quote = " ".join(svod_text.split())[:90] or "свод"
    summary = ("СОГЛАСНА: «%s» — вывод подтверждается сроками и суммами из свода.\n"
               "НЕ ХВАТАЕТ: «%s» — не указан источник цифры по доставке.\n"
               "СОГЛАСНА: «итог по этапам совпадает» — расхождений по составу нет."
               % (quote[:60], quote[60:90] or quote[:30]))
    node = {"id": card_id, "role": "critic", "title": TITLE_PREFIX + ": " + (model),
            "assignee": str((body or {}).get("assignee") or "team-critic"), "status": "done",
            "created_at": now, "finished_at": now + 2, "parent_ids": [rid],
            "model_label": (provider + " · " + model) if provider else model,
            "summary_md": summary, "outside_room": False}
    if rid in getattr(app, "requests", {}):
        app.requests[rid].setdefault("tree", []).append(copy.deepcopy(node))
    elif rid in getattr(app, "live", {}):
        app.live[rid].setdefault("node_patch", {}).setdefault(card_id, {}).update(node)
    return 200, {"ok": True, "action": "second_opinion", "card_id": card_id,
                 "title": node["title"]}


# ── итог дня (W29B-Q8) ─────────────────────────────────────────────────────

def _room_title(app, room_id: str) -> str:
    try:
        return str(app.room(room_id).get("title") or room_id)
    except Exception:                       # noqa: BLE001
        return room_id


def daily(app, query: dict) -> tuple:
    room_filter = str((query.get("room") or [""])[0] or "")
    day = time.strftime("%Y-%m-%d", time.localtime())
    midnight = int(time.mktime(time.strptime(day + " 04:00", "%Y-%m-%d %H:%M")))
    done, decisions, waiting, files, stopped = [], [], [], [], []
    rooms = [room_filter] if room_filter else list(app.room_details)
    for room_id in rooms:
        if room_id not in app.room_details:
            continue
        title = _room_title(app, room_id)
        for summary in app.summaries(room_id):
            rid = str(summary.get("id") or "")
            finished = int(summary.get("finished_at") or 0)
            if summary.get("status") == "done" and finished >= midnight:
                done.append({"room": room_id, "room_title": title, "request_id": rid,
                             "request_title": str(summary.get("title") or "Запрос"), "at": finished})
            if summary.get("status") == "failed":
                stopped.append({"room": room_id, "room_title": title, "request_id": rid,
                                "request_title": str(summary.get("title") or "Запрос"),
                                "reason": str(summary.get("status_reason") or "Запуск не удался"),
                                "kind": "failed", "at": int(summary.get("created_at") or 0)})
            if summary.get("status") == "needs_you" or (summary.get("status") == "done" and summary.get("needs_you")):
                waiting.append({"room": room_id, "room_title": title, "request_id": rid,
                                "kind": "question", "text": "Нужен ответ по «%s»" % str(summary.get("title") or "запросу"),
                                "recommended": "Открыть запрос и ответить",
                                "since": int(summary.get("created_at") or 0)})
        try:
            decisions.append({"room": room_id, "room_title": title,
                              "request_id": "t_a1b2c3d4", "title": "Сдвинуть срок приёмки на 3 дня",
                              "status": "accepted", "at": _now() - 3600})
        except Exception:                   # noqa: BLE001
            pass
        files.append({"room": room_id, "room_title": title, "name": "svod-%s.md" % room_id,
                      "at": _now() - 1800})
    counters = {"done": len(done), "decisions": len(decisions), "waiting": len(waiting),
                "files": len(files), "stopped": len(stopped)}
    counters["all"] = sum(counters.values())
    hour = time.localtime().tm_hour
    period = "Утро" if hour < 12 else ("День" if hour < 18 else "Вечер")
    return 200, {"day": day, "period_title": period, "room": room_filter or None,
                 "counters": counters, "done": done[:10], "decisions": decisions[:10],
                 "waiting": waiting[:10], "files": files[:10], "stopped": stopped[:10],
                 "spent": {"calls": 42, "tokens": 310000, "cost_usd": 1.87},
                 "estimate": {"input_tokens": 1200, "output_tokens": 300, "cost_usd": None},
                 "short": None, "generated_at": _now()}


def daily_shorten(app, body: dict) -> tuple:
    return 200, {"ok": True, "fake": True, "saved": True, "room": str((body or {}).get("room") or ""),
                 "text": "1. Свод по доставке готов, ждёт вашего ответа по сроку.\n"
                         "2. Решение по подрядчику принято — 3 дня сдвига.\n"
                         "3. Один запуск упал на пустой модели — проверьте профиль.\n"
                         "4. Файл отчёта обновлён.\n"
                         "5. Расход за сутки ниже обычного.",
                 "estimate": {"input_tokens": 1200, "output_tokens": 300, "cost_usd": None}}


# ── поиск по памяти (W29B-Q8) ──────────────────────────────────────────────

_MEMORY = [
    {"kind": "decision", "title": "Срок приёмки сдвинут на 3 дня", "room": "cafe",
     "request_id": "t_a1b2c3d4", "citation": "Решение · 21.11.2026 · accepted",
     "snippet": "Приняли: приёмка партии переносится на 3 дня, потому что поставщик не успевает."},
    {"kind": "svod", "title": "Свод: доставка цветов по району", "room": "cafe",
     "request_id": "t_a1b2c3d4", "citation": "Свод · 20.11.2026 · v3",
     "snippet": "Доставка своими силами дешевле на 12 % при объёме от 40 заказов в день."},
    {"kind": "answer", "title": "Ответ владельца про кофейню", "room": "cafe",
     "request_id": "t_c4d2e118", "citation": "Ответ · 19.11.2026",
     "snippet": "Решили начать с одной точки у метро, вторую открывать после окупаемости."},
    {"kind": "note", "title": "Заметка: почему выбрали подрядчика", "room": "laundry",
     "request_id": "", "citation": "Заметка · 18.11.2026",
     "snippet": "Выбрали второго подрядчика: сроки короче на неделю, цена та же."},
    {"kind": "journal", "title": "Журнал комнаты: смена модели", "room": "cafe",
     "request_id": "t_a1b2c3d4", "citation": "Журнал · 17.11.2026",
     "snippet": "Рутинное сведение переведено на лёгкую модель — расход упал вдвое."},
]


def memory_search(app, query: dict) -> tuple:
    text = str((query.get("q") or [""])[0] or "").strip().lower()
    room = str((query.get("room") or [""])[0] or "")
    try:
        limit = max(1, min(50, int((query.get("limit") or ["20"])[0])))
    except (TypeError, ValueError):
        limit = 20
    if not text:
        return 200, {"items": [], "total": 0, "by_kind": {}, "query": "",
                     "note": "Начните вводить слово — ищем в решениях, сводах, ответах и журнале."}
    words = [w for w in re.split(r"\W+", text) if len(w) > 1]
    found = []
    for row in _MEMORY:
        haystack = (row["title"] + " " + row["snippet"]).lower()
        if room and row["room"] != room:
            continue
        if words and not any(w in haystack for w in words):
            continue
        item = dict(row)
        item["at"] = _now() - 3600
        item["route"] = (("#/room/%s/request/%s" % (row["room"], row["request_id"]))
                         if row["request_id"] else "#/room/%s" % row["room"])
        found.append(item)
    by_kind = {}
    for item in found:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return 200, {"items": found[:limit], "total": len(found), "by_kind": by_kind, "query": text}


# ── уведомления (W29B-Q6) ──────────────────────────────────────────────────

def notify_status(app) -> dict:
    box = state(app)
    settings = box["notify"]
    return {"settings": copy.deepcopy(settings),
            "warnings": [], "channel": {"name": "fake", "available": True,
                                        "reason": "Демо: уведомления пишутся в журнал, окна не показываются."},
            "quiet_now": False, "sent_today": sum(1 for h in box["history"] if h.get("ok")),
            "pending": 0, "history": copy.deepcopy(box["history"][:20]),
            "kinds": [{"id": kind, "title": title} for kind, title in KINDS],
            "platform": "win32", "generated_at": _now()}


def notify_put(app, body: dict) -> tuple:
    box = state(app)
    data = body or {}
    settings = box["notify"]
    if "enabled" in data:
        settings["enabled"] = bool(data.get("enabled"))
    if isinstance(data.get("kinds"), dict):
        for key, value in data["kinds"].items():
            if key in settings["kinds"]:
                settings["kinds"][key] = bool(value)
    if isinstance(data.get("quiet_hours"), dict):
        quiet = settings["quiet_hours"]
        for key in ("enabled", "from", "to"):
            if key in data["quiet_hours"]:
                quiet[key] = data["quiet_hours"][key] if key == "enabled" else str(data["quiet_hours"][key])
    if "min_interval_s" in data:
        try:
            settings["min_interval_s"] = max(60, int(data["min_interval_s"]))
        except (TypeError, ValueError):
            return 400, {"error": {"code": "bad_request", "message": "min_interval_s: целое число секунд",
                                   "detail": {"field": "min_interval_s"}}}
    return 200, notify_status(app)


def notify_test(app) -> tuple:
    box = state(app)
    box["history"].insert(0, {"at": _now(), "title": "Проверка уведомлений",
                              "body": "Так выглядит уведомление комнаты.", "ok": True, "reason": ""})
    del box["history"][20:]
    return 200, {"ok": True, "channel": "fake", "reason": "", "sent": 1}


# ── диспетчер ──────────────────────────────────────────────────────────────

ROOM_RE = r"([a-z][a-z0-9_-]{1,31})"
RID_RE = r"(t_[0-9a-f]{8})"


def handle(app, method: str, path: str, query: dict, body: dict):
    """Вернуть ``(status, payload)`` для ручек партии B или ``None``."""
    if method == "GET":
        m = re.fullmatch(r"/api/rooms/" + ROOM_RE + r"/parity", path)
        if m:
            return parity(app, m.group(1))
        m = re.fullmatch(r"/api/requests/" + RID_RE + r"/second-opinion", path)
        if m:
            return second_opinion(app, m.group(1))
        if path == "/api/daily":
            return daily(app, query)
        if path == "/api/memory/search":
            return memory_search(app, query)
        if path == "/api/notify":
            return 200, notify_status(app)
        return None
    if method == "POST":
        m = re.fullmatch(r"/api/rooms/" + ROOM_RE + r"/parity/fix", path)
        if m:
            return parity_fix(app, m.group(1), body)
        if path == "/api/daily/shorten":
            return daily_shorten(app, body)
        if path == "/api/notify/test":
            return notify_test(app)
        return None
    if method == "PUT" and path == "/api/notify/settings":
        return notify_put(app, body)
    return None
