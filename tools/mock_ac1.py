# -*- coding: utf-8 -*-
"""Демо-ручки W29-AC1: «Результаты», «Входящие» и расход запроса.

Зачем отдельный модуль: ``mock_read.py`` держат несколько пакетов, поэтому
новые пути демо подключаются одной строкой (``mock_ac1.handle(...)``), а всё
содержимое живёт здесь.

Что отдаём:

* ``GET  /api/rooms/{id}/results`` — итоги завершённых запросов комнаты
  (без него вкладка «Результаты» показывает только страницы-отчёты);
* ``GET  /api/rooms/{id}/inbox`` — записи «Входящих» (демо-набор в памяти);
* ``POST /api/rooms/{id}/inbox/{item}/actions`` — разбор записи;
* ``GET  /api/requests/{rid}/usage`` — строка расхода запроса.

Только чтение фикстур и памяти мока: диск, профили и доска не трогаются.
"""
from __future__ import annotations

import re
import time

from mock_core import ApiError

ROOM_RE = r"[a-z][a-z0-9_-]{1,31}"
RID_RE = r"t_[0-9a-f]{8}"

#: Демо-записи «Входящих»: одна ждёт разбора, одна уже у руководителя,
#: одна разобрана — чтобы экран показывал все состояния и у каждой строки
#: была кнопка (правило «без тупиков»).
_INBOX = {
    "cafe": [
        {"id": "in_a1b2c3d4", "text": "Позвонить в химчистку: уточнить, берут ли скатерти из льна и сколько стоит срочная чистка к пятнице.",
         "source": "dictation", "status": "new", "kind": None, "note": ""},
        {"id": "in_b2c3d4e5", "text": "Дежурство «Следить за папкой выгрузок»: появились два новых файла — прайс и накладная.",
         "source": "duty", "status": "processing", "kind": None,
         "proposal": {"state": "waiting", "kind": "delo",
                      "title": "Разобрать новые файлы в папке выгрузок",
                      "text": "Предлагаю оформить так: дело «Разобрать новые файлы в папке выгрузок» — "
                              "сверить прайс с накладной и записать расхождение."}},
        {"id": "in_c3d4e5f6", "text": "Идея: добавить в меню кофейни сезонный напиток и посчитать его себестоимость.",
         "source": "manual", "status": "processed", "kind": "delo", "note": ""},
    ],
}

_ACTIONS_NEW = [
    {"id": "to_delo", "label": "В дело"},
    {"id": "to_step", "label": "В шаг"},
    {"id": "to_question", "label": "Вопрос"},
    {"id": "to_duty", "label": "Дежурство"},
    {"id": "to_note", "label": "Справка"},
    {"id": "propose", "label": "Разобрать руководителем"},
    {"id": "delete", "label": "Удалить"},
]
_ACTIONS_PROCESSED = [
    {"id": "restore", "label": "Вернуть в работу"},
    {"id": "delete", "label": "Удалить"},
]
_KIND_TO_ACTION = {"delo": "to_delo", "step": "to_step", "question": "to_question",
                   "duty": "to_duty", "note": "to_note"}


def _now() -> int:
    return int(time.time())


def _items(app) -> dict:
    """Демо-записи комнаты: копия, чтобы действия меняли только её."""
    store = getattr(app, "ac1_inbox", None)
    if store is None:
        store = {}
        for room, rows in _INBOX.items():
            store[room] = [dict(row, room=room, created_at="") for row in rows]
        app.ac1_inbox = store
    return store


def _public(app, item: dict) -> dict:
    status = str(item.get("status") or "new")
    out = dict(item)
    out.setdefault("source_title", {"dictation": "диктовка", "manual": "вручную",
                                    "duty": "дежурство"}.get(str(item.get("source")), "вручную"))
    out.setdefault("created_at", "")
    out.setdefault("created_hhmm", time.strftime("%H:%M", time.localtime(item.get("created_ts") or _now())))
    if status in ("new", "processing"):
        out["actions"] = list(_ACTIONS_NEW)
    elif status == "deleted":
        out["actions"] = [{"id": "restore", "label": "Вернуть"}]
    else:
        out["actions"] = list(_ACTIONS_PROCESSED)
    return out


def _counts(rows: list) -> dict:
    by = {"new": 0, "processing": 0, "processed": 0, "deleted": 0}
    for it in rows:
        status = str(it.get("status") or "new")
        by[status if status in by else "new"] += 1
    waiting = sum(1 for it in rows if isinstance(it.get("proposal"), dict)
                  and it["proposal"].get("state") == "waiting")
    return {"new": by["new"], "processing": by["processing"], "processed": by["processed"],
            "deleted": by["deleted"], "total": len(rows),
            "active": by["new"] + by["processing"], "proposals": waiting}


def _short_of(app, rid: str) -> str:
    """«Коротко» запроса: первая фраза свода (демо собирает из ответа)."""
    try:
        detail = app.detail(rid)
    except Exception:                        # noqa: BLE001 — без свода строка пустая
        return ""
    synth = detail.get("synthesis") if isinstance(detail.get("synthesis"), dict) else {}
    text = str(synth.get("short") or synth.get("text_md") or detail.get("text") or "")
    text = " ".join(text.replace("#", " ").replace("*", " ").split())
    if not text:
        return ""
    cut = text[:280]
    dot = cut.rfind(". ")
    return cut[:dot + 1] if dot > 80 else cut


#: Запросы, из которых в демо уже сделали дело (память, без доски).
_MADE: dict = {}


def delo_from_request(app, room_id: str, body: dict) -> tuple:
    """`POST /api/rooms/{id}/delos/from-request` — «Сделать делом» в демо.

    Кнопка в блоке «Запросы без дела» не должна вести в пустоту: запоминаем запросы
    как оформленные, и они пропадают из блока до перезапуска мока.
    """
    app.room(room_id)
    raw = (body or {}).get("request_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    one = (body or {}).get("request_id")
    ids = [str(x) for x in raw if str(x or "").strip()]
    if one and str(one).strip() not in ids:
        ids.append(str(one).strip())
    known = {str(row.get("id")) for row in app.summaries(room_id)}
    made, skipped = [], []
    bucket = _MADE.setdefault(room_id, set())
    for rid in ids:
        if rid in known and rid not in bucket:
            bucket.add(rid)
            made.append(rid)
        else:
            skipped.append(rid)
    return 200, {"ok": True, "room": room_id, "created": made, "skipped": skipped,
                 "message": "" if made else "Дело уже есть — открываю список дел"}


# --- GET -------------------------------------------------------------------
def results(app, room_id: str) -> tuple:
    """Итоги завершённых запросов комнаты: «Коротко», отчёты, ссылка на запрос."""
    app.room(room_id)
    reports = []
    if room_id == "cafe":
        try:
            reports = list(app.fixture("reports_cafe.json").get("items") or [])
        except Exception:                    # noqa: BLE001 — отчёты вторичны
            reports = []
    by_request: dict = {}
    for rep in reports:
        rid = str(rep.get("request_id") or "")
        if rid:
            by_request.setdefault(rid, []).append(
                {"key": rep.get("key"), "title": rep.get("title") or rep.get("key"),
                 "kind": rep.get("kind") or "spec"})
    items, pending = [], []
    for summary in app.summaries(room_id):
        rid = str(summary.get("id") or "")
        title = str(summary.get("title") or rid)
        done = str(summary.get("status") or "") == "done"
        row = {"id": rid, "slug": "", "title": title,
               "title_short": (title[:90] + "…") if len(title) > 90 else title,
               "status": str(summary.get("status") or ""),
               "created_at": int(summary.get("created_at") or 0),
               "finished_at": int(summary.get("finished_at") or 0),
               "short": _short_of(app, rid) if done else "",
               "reports": by_request.get(rid) or [], "delo": None}
        if done:
            items.append(row)
        else:
            pending.append({k: row[k] for k in ("id", "title", "title_short", "status",
                                                "created_at", "delo")})
    # «Сделать делом» в демо: запрос, ставший делом, уходит из «Запросов без дела»
    # и получает ссылку на дело — как на боевом сервере (W29-FIX1 п.3).
    made = _MADE.get(room_id) or set()
    for row in items + pending:
        if str(row.get("id")) in made:
            row["delo"] = {"id": "d_" + str(row.get("id")), "title": str(row.get("title") or "")}
    items.sort(key=lambda it: (it.get("finished_at") or 0, it.get("created_at") or 0), reverse=True)
    pending.sort(key=lambda it: it.get("created_at") or 0, reverse=True)
    without = [row for row in items + pending if not row.get("delo")]
    # `requests` — сколько запросов ВСЕГО: блок «Запросы без дела» говорит по нему
    # «запросов пока нет / каждый запрос — работа». Без поля экран врал: 11 строк
    # и подпись «запросов в комнате пока нет» (W29-FIX1 п.3).
    counts = {"requests": len(items) + len(pending), "done": len(items),
              "results": len(items), "pending": len(pending), "reports": len(reports),
              "without_delo": len(without)}
    return 200, {"ok": True, "room": room_id, "room_title": str(app.room(room_id).get("title") or ""),
                 "items": items, "pending": pending, "without_delo": without,
                 "counts": counts, "reports": reports}


def inbox(app, room_id: str, query: dict) -> tuple:
    """Записи «Входящих»: отбор ``active|new|processing|processed|deleted|all``."""
    app.room(room_id)
    wanted = str((query.get("status") or ["active"])[0] or "active")
    rows = _items(app).setdefault(room_id, []) if room_id in _INBOX else []
    listed = []
    for item in rows:
        status = str(item.get("status") or "new")
        if (wanted == "all" or (wanted == "active" and status in ("new", "processing"))
                or status == wanted):
            listed.append(_public(app, item))
    return 200, {"room": room_id, "filter": wanted, "counts": _counts(rows),
                 "items": listed, "notes": [], "queue": [], "warning": "",
                 "generated_at": str(_now())}


def usage(app, rid: str) -> tuple:
    """Строка расхода запроса: демо-цифры, стабильные для одной карточки."""
    detail = app.detail(rid)
    cards = 1 + len([n for n in (detail.get("tree") or []) if isinstance(n, dict)])
    seed = sum(ord(ch) for ch in rid)
    calls = cards * (3 + seed % 4)
    per_call = 9000 + seed % 5000
    body = calls * per_call
    tokens = {"input": int(body * 0.55), "cache_read": int(body * 0.3),
              "cache_write": int(body * 0.1), "output": int(body * 0.05)}
    total = sum(tokens.values())
    models = [{"model": "Дилижанс миди", "calls": max(1, calls - 2),
               "cost_usd": round(calls * 0.031, 4), "sessions": max(1, cards - 1)},
              {"model": "Вектор 3", "calls": min(2, calls),
               "cost_usd": 0.012, "sessions": 1}]
    cost = round(sum(m["cost_usd"] for m in models), 4)
    return 200, {"rid": rid, "calls": calls, "tokens": tokens, "cost_usd": cost,
                 "models": models, "source": "fixtures",
                 "line": "Расход: %d выз. / %s тыс. токенов" % (calls, round(total / 1000)),
                 "split": {"base": int(total * 0.5), "tools": int(total * 0.15),
                           "body": int(total * 0.35)},
                 "by_card": [], "wasted": {"attempts": 0, "cards": [], "cost_usd": 0.0,
                                           "tokens": 0, "text": ""},
                 "mismatched": {"attempts": 0, "cards": []}, "warnings": [],
                 "generated_at": _now()}


# --- POST ------------------------------------------------------------------
def handle(app, method: str, path: str, query: dict, body: dict):
    """Вернуть ``(status, payload)`` для ручек AC1 или ``None``."""
    if method == "GET":
        m = re.fullmatch(r"/api/rooms/(%s)/results" % ROOM_RE, path)
        if m:
            return results(app, m.group(1))
        m = re.fullmatch(r"/api/rooms/(%s)/inbox" % ROOM_RE, path)
        if m:
            return inbox(app, m.group(1), query)
        m = re.fullmatch(r"/api/requests/(%s)/usage" % RID_RE, path)
        if m:
            return usage(app, m.group(1))
        return None
    if method == "POST":
        m = re.fullmatch(r"/api/rooms/(%s)/delos/from-request" % ROOM_RE, path)
        if m:
            return delo_from_request(app, m.group(1), body or {})
        m = re.fullmatch(r"/api/rooms/(%s)/inbox/(in_[a-z0-9]{4,32})/actions" % ROOM_RE, path)
        if m:
            return inbox_action(app, m.group(1), m.group(2), body or {})
    return None


def inbox_action(app, room_id: str, item_id: str, body: dict) -> tuple:
    """Разбор записи: смена состояния и вида. Неизвестное действие — 400."""
    app.room(room_id)
    action = str((body or {}).get("action") or "")
    rows = _items(app).get(room_id) or []
    item = next((row for row in rows if row.get("id") == item_id), None)
    if item is None:
        raise ApiError(404, "not_found", "Запись не найдена", {"item": item_id})
    known = {a["id"] for a in _ACTIONS_NEW} | {"restore", "accept", "reject", "edit"}
    if action not in known:
        raise ApiError(400, "bad_request", "Неизвестное действие: %s" % action,
                       {"allowed": sorted(known)})
    if action in _KIND_TO_ACTION.values():
        item["kind"] = next(k for k, v in _KIND_TO_ACTION.items() if v == action)
        item["status"] = "processed"
    elif action == "propose":
        item["status"] = "processing"
        item["proposal"] = {"state": "waiting", "kind": "delo",
                            "title": "Разобрать: " + str(item.get("text") or "")[:60],
                            "text": "Предлагаю оформить так: дело «" + str(item.get("text") or "")[:60] + "»."}
    elif action == "accept":
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        item["kind"] = proposal.get("kind") or "delo"
        proposal["state"] = "accepted"
        item["status"] = "processed"
    elif action == "reject":
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        proposal["state"] = "rejected"
        item["status"] = "new"
    elif action == "delete":
        item["status"] = "deleted"
    elif action == "restore":
        item["status"] = "new"
        item["kind"] = None
    fresh = _public(app, item)
    return 200, {"ok": True, "item": fresh, "counts": _counts(rows)}


# --- маршруты демо ----------------------------------------------------------
def _register_routes() -> None:
    """Дописать свои пути в таблицу методов мока (``mock_core.ROUTE_METHODS``).

    Так новый демо-путь не требует правки общего ``mock_core.py``: он принадлежит
    этому пакету и регистрируется при импорте. ``GET`` проверяется таблицей тоже,
    поэтому без регистрации запрос получил бы 405.
    """
    try:
        import mock_core
    except ImportError:                      # pragma: no cover — плоский импорт
        return
    extra = (
        (r"^/api/rooms/%s/results$" % ROOM_RE, ("GET",)),
        (r"^/api/rooms/%s/delos/from-request$" % ROOM_RE, ("POST",)),
        (r"^/api/rooms/%s/inbox$" % ROOM_RE, ("GET", "POST")),
        (r"^/api/rooms/%s/inbox/in_[a-z0-9]{4,32}/actions$" % ROOM_RE, ("POST",)),
        (r"^/api/requests/%s/usage$" % RID_RE, ("GET",)),
    )
    rows = tuple(getattr(mock_core, "ROUTE_METHODS", ()) or ())
    for row in extra:
        if row not in rows:
            rows += (row,)
    mock_core.ROUTE_METHODS = rows


_register_routes()
