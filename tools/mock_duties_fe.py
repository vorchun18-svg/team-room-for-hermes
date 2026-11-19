# -*- coding: utf-8 -*-
"""W27-E8: демо-сервер «дежурств комнаты» (мастер, правила, корзина, история).

Мок нужен интерфейсу, пока E7 не отдал серверную часть: контракт —
`docs/WAVE27-RULES.md` §3. Здесь только ДЕМО-состояние в памяти процесса:
ни одного реального крона, ни одного вызова `hermes`. Имена, письма, папки и
дела — вымышленные (публичный релиз: без терминов бизнеса и реальных путей).

Подключение (свои блоки):
  * `tools/mock_read.py`  → `GET /api/duty-templates`, `GET /api/duties/{id}/history`;
  * `tools/mock_write.py` → `POST /api/rooms/{id}/duties`, `PATCH|DELETE /api/duties/{id}`,
    `/restore`, `/dry_run`, `/actions`, `POST /api/duties/proposals/{id}/actions`.
"""
from __future__ import annotations

import copy
import hashlib
import re
import time

from mock_core import ApiError, now

#: Шаблоны дежурств (универсальные, без терминов конкретного бизнеса).
TEMPLATES = [
    {"id": "tpl_mail", "title": "Проверять ящик на новое",
     "for": "Почта или общий ящик: чтобы важное письмо не ждало неделю.",
     "kind": "watch", "schedule": "30m", "when_human": "каждые 30 минут",
     "task": "C:/Work/Rooms/demo/inbox",
     "check": "Новые письма в ящике с прошлого запуска.",
     "change": "появилось новое письмо", "on_change": "inbox",
     "action_human": "во «Входящие» комнаты, предложить", "level": "propose",
     "notify": ["today", "badge"]},
    {"id": "tpl_folder", "title": "Следить за папкой",
     "for": "Выгрузки, сканы, входящие файлы: знать, что появилось.",
     "kind": "watch", "schedule": "every 2h", "when_human": "каждые 2 часа",
     "task": "C:/Work/Rooms/demo/exports",
     "check": "Новые файлы в папке комнаты.",
     "change": "появились новые файлы", "on_change": "log",
     "action_human": "тихая запись в историю", "level": "self",
     "notify": ["today"]},
    {"id": "tpl_morning", "title": "Утренний обзор комнаты",
     "for": "Один короткий обзор на утро: что ждёт, что просрочено.",
     "kind": "model", "schedule": "0 9 * * 1-5", "when_human": "по будням в 09:00",
     "check": "Дела и шаги комнаты: что ждёт вас, что просрочено, что готово.",
     "change": "модель сравнивает с прошлым обзором", "on_change": "inbox",
     "action_human": "во «Входящие» комнаты, предложить", "level": "propose",
     "notify": ["today"]},
    {"id": "tpl_page", "title": "Следить за страницей и ценой",
     "for": "Чужая страница, тариф или цена поставщика.",
     "kind": "watch", "schedule": "0 10 * * *", "when_human": "каждый день в 10:00",
     "task": "https://example.com/tariff",
     "check": "Страница целиком: цена, условия, наличие.",
     "change": "текст отличается", "on_change": "inbox",
     "action_human": "во «Входящие» комнаты, предложить", "level": "propose",
     "notify": ["today"]},
    {"id": "tpl_deadlines", "title": "Напоминать о сроках",
     "for": "Сроки по делам комнаты — заранее, а не в день сдачи.",
     "kind": "script", "schedule": "0 9 * * 1-5", "when_human": "по будням в 09:00",
     "check": "Дела, у которых срок ближе двух дней и нет шага «проверить».",
     "change": "стало больше порога", "on_change": "question",
     "action_human": "спросить меня", "level": "ask",
     "notify": ["today", "badge"]},
    {"id": "tpl_weekly", "title": "Еженедельный отчёт",
     "for": "Итог недели одной страницей: что сделано, что застряло.",
     "kind": "model", "schedule": "0 10 * * 1", "when_human": "по понедельникам в 10:00",
     "check": "Готовые и застрявшие дела комнаты за неделю.",
     "change": "модель сравнивает с прошлым отчётом", "on_change": "inbox",
     "action_human": "во «Входящие» комнаты, предложить", "level": "propose",
     "notify": ["today"]},
    {"id": "tpl_steps", "title": "Проверять выполнение шагов",
     "for": "Шаги, которые висят без движения дольше недели.",
     "kind": "script", "schedule": "0 9 * * 1-5", "when_human": "по будням в 09:00",
     "check": "Открытые шаги без изменений больше семи дней.",
     "change": "стало больше порога", "on_change": "step",
     "action_human": "предложить шаг в деле", "level": "propose",
     "notify": ["today"]},
]

PROPOSALS = [
    {"id": "dp_deadlines", "title": "Проверять сроки по делам комнаты",
     "why": "За две недели трижды переносили срок: о нём узнавали в последний день.",
     "when": "0 9 * * 1-5", "when_human": "по будням в 09:00",
     "check": "Дела комнаты: у кого срок ближе двух дней и нет шага «проверить».",
     "on_change": "inbox", "on_change_label": "во «Входящие» комнаты",
     "kind": "script", "source": "manager",
     "limit_text": "Руководитель предлагает не больше двух дежурств в неделю — это второе."},
    {"id": "dp_folder", "title": "Следить за папкой выгрузок",
     "why": "Вы сказали: «это будем делать постоянно» — оформил в дежурство.",
     "when": "every 2h", "when_human": "каждые 2 часа",
     "check": "Новые файлы в папке комнаты с прошлого запуска.",
     "on_change": "log", "on_change_label": "записать в историю",
     "kind": "watch", "source": "dictation",
     "limit_text": "Предложение из диктовки — можно поправить на любом шаге."},
]

#: Дополнительные поля дежурств (демо): тип, правило, действие, уровень, итог.
ITEM_EXTRAS = {
    "j_daily_brief": {
        "kind": "model", "task": "Собрать короткий обзор комнаты: что готово, что ждёт ответа.",
        "workdir": "C:/Work/Rooms/demo", "catch_up": True,
        "change": {"rule": "model_compares", "threshold": 0},
        "on_change": "inbox", "level": "propose", "notify": ["today"],
        "source": "manager",
        "outcome": {"changed": "да", "found": "2 дела без движения", "action": "предложено Дело"},
    },
    "j_overdue_watch": {
        "kind": "script", "task": "C:/Work/Rooms/demo/tools/check_overdue.py",
        "workdir": "C:/Work/Rooms/demo", "catch_up": False,
        "change": {"rule": "count_over", "threshold": 3},
        "on_change": "question", "level": "ask", "notify": ["today", "badge"],
        "source": "manual",
        "outcome": {"changed": "нет"},
    },
    "j_backup": {
        "kind": "script", "task": "C:/Work/Rooms/demo/tools/copy_notes.py",
        "workdir": "C:/Work/Rooms/demo", "catch_up": True,
        "change": {"rule": "new_items", "threshold": 0},
        "on_change": "log", "level": "self", "notify": [],
        "source": "template", "template": "tpl_folder",
        "outcome": {"changed": "нет"},
    },
    "j_report_digest": {
        "kind": "model", "task": "Собрать итог недели: что сделано, что застряло.",
        "workdir": "C:/Work/Rooms/demo", "catch_up": False,
        "change": {"rule": "model_compares", "threshold": 0},
        "on_change": "delo", "level": "propose", "notify": ["today"],
        "source": "delo",
        "outcome": {"changed": "да", "found": "отчёт за неделю собран", "action": "предложено Дело"},
    },
}

HISTORY_TEXTS = [
    {"changed": "нет", "done": "проверено 12 пунктов", "found": ""},
    {"changed": "да", "done": "собран обзор", "found": "2 новых письма",
     "proposals": [{"what": "Ответить на письмо поставщика", "why": "срок ответа — 2 дня",
                    "action": "создать Дело"}], "created": ["Дело «Ответ на письмо»"]},
    {"changed": "нет", "done": "проверено 12 пунктов", "found": ""},
]

TRASH_SEED = [
    {"job_id": "j_notes_copy", "title": "Копировать заметки комнаты",
     "trashed_at": None, "days_ago": 3},
]


def _state(app) -> dict:
    """Состояние демо: один словарь на процесс мок-сервера."""
    st = getattr(app, "duty_fe", None)
    if st is None:
        st = {"created": [], "patched": {}, "trash": [], "proposals": copy.deepcopy(PROPOSALS),
              "never": [], "accepted": [], "trashed_seed": False, "deleted": {}}
        app.duty_fe = st
    return st


def _job_id(title: str) -> str:
    digest = hashlib.sha1(("%s|%d" % (title, time.time_ns())).encode("utf-8")).hexdigest()
    return "j_" + digest[:10]


def _human(schedule: str) -> str:
    """Русское расписание — та же логика, что в интерфейсе (duty-model.js)."""
    raw = str(schedule or "").strip()
    presets = {"30m": "каждые 30 минут", "0 * * * *": "каждый час", "every 2h": "каждые 2 часа",
               "0 9 * * 1-5": "по будням в 09:00", "0 9 * * *": "каждый день в 09:00",
               "0 10 * * 1": "по понедельникам в 10:00", "0 10 * * *": "каждый день в 10:00"}
    if raw in presets:
        return presets[raw]
    if re.fullmatch(r"\d+m", raw):
        return "каждые %s минут" % raw[:-1]
    match = re.fullmatch(r"\*/(\d+) \* \* \* \*", raw)
    if match:
        return "каждые %s минут" % match.group(1)
    return raw or "расписание не указано"


def _command(body: dict, job_id: str = "") -> list:
    """Точная команда, которую выполнит сервер (для предпросмотра)."""
    argv = ["hermes", "cron", "edit", job_id] if job_id else \
        ["hermes", "cron", "create", str(body.get("title") or "дежурство")]
    if body.get("schedule"):
        argv += ["--schedule", str(body["schedule"])]
    kind = str(body.get("kind") or "model")
    task = str(body.get("task") or "")
    if kind == "script":
        argv += ["--script", task]
    elif kind == "watch":
        argv += ["--monitor-url", task]
    else:
        argv += ["--prompt", task]
    if body.get("workdir"):
        argv += ["--workdir", str(body["workdir"])]
    if body.get("catch_up"):
        argv.append("--catch-up")
    if body.get("started_paused") or body.get("paused"):
        argv.append("--paused")
    if body.get("room"):
        argv += ["--deliver", "room:%s" % body["room"]]
    return argv


def _quote(argv: list) -> str:
    out = []
    for part in argv:
        text = str(part)
        out.append('"%s"' % text.replace('"', '\\"') if " " in text else text)
    return " ".join(out)


def _changes(body: dict, job_id: str = "") -> list:
    kind = str(body.get("kind") or "model")
    kind_text = {"script": "скрипт без модели", "watch": "слежение Hermes",
                 "model": "модель комнаты"}.get(kind, "модель комнаты")
    change = body.get("change") or {}
    labels = {"new_items": "появилось новое", "count_over": "стало больше порога",
              "text_differs": "текст отличается", "model_compares": "сравнение с прошлым разом"}
    actions = {"log": "тихая запись в историю", "inbox": "карточка во «Входящих»",
               "delo": "предложение Дела", "step": "предложение шага",
               "question": "вопрос вам"}
    levels = {"self": "сделает сам (внутри комнаты)", "propose": "предложит",
              "ask": "спросит"}
    out = [("Расписание станет: " if job_id else "Расписание: ") + _human(body.get("schedule"))]
    out.append("Задание выполнит " + kind_text)
    out.append("Изменением считается: " + labels.get(str(change.get("rule")), "не задано")
               + (" (порог %s)" % change.get("threshold") if change.get("rule") == "count_over" else ""))
    out.append("При изменении: " + actions.get(str(body.get("on_change")), "не задано")
               + ", " + levels.get(str(body.get("level")), "предложит"))
    return out


def _history_for(job_id: str, count: int = 3) -> list:
    """Демо-история: у каждого задания свой набор строк (без выдуманных чисел в отчётах)."""
    base = now() - 6 * 3600
    out = []
    for index in range(count):
        sample = HISTORY_TEXTS[index % len(HISTORY_TEXTS)]
        out.append({
            "run_id": "run_%s_%d" % (job_id[-4:], index + 1),
            "job_id": job_id,
            "at": base - index * 86400 // 3,
            "changed": sample["changed"],
            "done": sample["done"],
            "found": sample["found"],
            "proposals": copy.deepcopy(sample.get("proposals") or []),
            "created": list(sample.get("created") or []),
        })
    return out


def enrich(app, payload: dict) -> dict:
    """Дополнить ответ `GET /api/duties`: поля W27, корзина, предложения, неделя."""
    st = _state(app)
    if not st["trashed_seed"]:
        for seed in TRASH_SEED:
            item = {"job_id": seed["job_id"], "title": seed["title"],
                    "trashed_at": now() - seed["days_ago"] * 86400}
            item["expires_at"] = item["trashed_at"] + 30 * 86400
            st["trash"].append(item)
        st["trashed_seed"] = True

    items = list(payload.get("items") or [])
    for item in items:
        job_id = str(item.get("job_id") or "")
        extra = ITEM_EXTRAS.get(job_id) or {}
        for key, value in extra.items():
            item.setdefault(key, copy.deepcopy(value))
        if job_id in st["patched"]:
            item.update(st["patched"][job_id])
        item.setdefault("kind", "model")
        item.setdefault("change", {"rule": "new_items", "threshold": 0})
        item.setdefault("on_change", "inbox")
        item.setdefault("level", "propose")
        item.setdefault("notify", ["today"])
        item.setdefault("source", "manual")
        item.setdefault("history", _history_for(job_id, 3))
    items.extend(copy.deepcopy(st["created"]))
    payload["items"] = items
    payload["trash"] = copy.deepcopy(st["trash"])
    payload["proposals"] = copy.deepcopy(st["proposals"])
    if not payload.get("week") or not payload["week"].get("days"):
        payload["week"] = demo_week(items)
    return payload


def demo_week(items: list) -> dict:
    """Неделя для демо: понедельник — сегодня, плашки дежурств и «план»."""
    days = []
    labels = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    full = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    today = time.localtime()
    monday = today
    shift = (today.tm_wday + 7) % 7
    monday_ts = time.mktime(today) - shift * 86400
    runs_total = failed_total = missed_total = 0
    for index in range(7):
        moment = time.localtime(monday_ts + index * 86400)
        planned = 8 if index < 5 else 2
        past = index <= shift
        runs = planned if past else 0
        failed = 1 if index == 1 else 0
        missed = 1 if index == 2 else 0
        runs_total += runs
        failed_total += failed
        missed_total += missed
        jobs = []
        if past:
            for item in items[:3]:
                jobs.append({"job_id": item.get("job_id"), "name": item.get("name"),
                             "runs": 2, "state": "failed" if failed and item is items[0] else "ok"})
        days.append({
            "date": time.strftime("%Y-%m-%d", moment),
            "weekday": moment.tm_wday, "label": labels[index], "full": full[index],
            "day": moment.tm_mday, "month": moment.tm_mon,
            "today": index == shift, "future": index > shift,
            "runs": runs, "failed": failed, "missed": missed,
            "planned": planned, "more": max(0, len(items) - 3), "jobs": jobs,
        })
    return {"start": days[0]["date"], "end": days[-1]["date"], "days": days,
            "runs": runs_total, "failed": failed_total, "missed": missed_total, "error": ""}


def templates() -> dict:
    return {"items": copy.deepcopy(TEMPLATES), "generated_at": now()}


def _find(app, job_id: str) -> dict:
    st = _state(app)
    for item in st["created"]:
        if str(item.get("job_id")) == job_id:
            return item
    raise ApiError(404, "unknown_job", "Такого дежурства нет: возможно, его уже удалили",
                   {"job_id": job_id})


def create(app, room_id: str, body: dict) -> dict:
    """`POST /api/rooms/{id}/duties`: предпросмотр или создание (только с confirm)."""
    st = _state(app)
    data = dict(body or {})
    data["room"] = room_id
    preview = bool(data.get("preview"))
    confirm = bool(data.get("confirm"))
    if preview and confirm:
        raise ApiError(400, "bad_request",
                       "Предпросмотр и создание — разные запросы: preview:true не создаёт",
                       {"preview": True, "confirm": True})
    title = str(data.get("title") or "").strip()
    schedule = str(data.get("schedule") or "").strip()
    task = str(data.get("task") or "").strip()
    missing = []
    if not title:
        missing.append("title")
    if not schedule:
        missing.append("schedule")
    if not task:
        missing.append("task")
    if missing:
        raise ApiError(422, "missing_fields", "Не хватает полей мастера", {"missing": missing})
    if preview:
        return {"ok": True, "preview": {
            "command": _quote(_command(data)),
            "argv": _command(data),
            "changes": _changes(data),
            "note": "Это предпросмотр. Задание в Hermes не создано — оно появится только после «Создать».",
        }}
    if not confirm:
        raise ApiError(409, "confirm_required",
                       "Создание дежурства — только с подтверждением человека: confirm:true",
                       {"confirm": False, "created": False})
    item = {
        "job_id": _job_id(title), "name": title, "title": title,
        "enabled": not bool(data.get("started_paused")), "schedule": schedule,
        "schedule_human": _human(schedule), "bound_rooms": [room_id],
        "last_run": None, "next_run_at": now() + 3600, "missed_recent": 0, "recent": [],
        "excerpt": "", "problem": None,
        "kind": str(data.get("kind") or "model"), "task": task,
        "workdir": str(data.get("workdir") or ""), "catch_up": bool(data.get("catch_up")),
        "change": copy.deepcopy(data.get("change") or {"rule": "new_items", "threshold": 0}),
        "on_change": str(data.get("on_change") or "inbox"),
        "level": str(data.get("level") or "propose"),
        "notify": list(data.get("notify") or ["today"]),
        "source": str(data.get("source") or "manual"),
        "template": str(data.get("template") or ""),
        "outcome": None, "history": [], "demo": True,
    }
    st["created"].append(item)
    return {"ok": True, "created": True, "item": copy.deepcopy(item),
            "text": "Дежурство создано: «%s», %s. Первый запуск — по расписанию."
                    % (title, _human(schedule))}


def patch(app, job_id: str, body: dict) -> dict:
    """`PATCH /api/duties/{id}`: расписание, задание, правило, действие, пауза."""
    st = _state(app)
    data = dict(body or {})
    saved = {}
    if "schedule" in data:
        saved["schedule"] = str(data.get("schedule") or "")
        saved["schedule_human"] = _human(saved["schedule"])
    if "task" in data:
        saved["task"] = str(data.get("task") or "")
    if "title" in data:
        saved["name"] = str(data.get("title") or "")
        saved["title"] = saved["name"]
    if "change" in data:
        saved["change"] = copy.deepcopy(data.get("change") or {})
    if "on_change" in data:
        saved["on_change"] = str(data.get("on_change") or "inbox")
    if "level" in data:
        saved["level"] = str(data.get("level") or "propose")
    if "notify" in data:
        saved["notify"] = list(data.get("notify") or [])
    if "paused" in data:
        saved["enabled"] = not bool(data.get("paused"))
    if not saved:
        raise ApiError(400, "bad_request", "Нечего менять: передайте хотя бы одно поле",
                       {"allowed": ["schedule", "task", "title", "change", "on_change",
                                    "level", "notify", "paused"]})
    st["patched"].setdefault(job_id, {}).update(saved)
    for item in st["created"]:
        if str(item.get("job_id")) == job_id:
            item.update(saved)
    labels = {"schedule": "расписание", "task": "задание", "title": "название",
              "change": "правило изменения", "on_change": "действие при изменении",
              "level": "уровень", "notify": "уведомления", "enabled": "пауза"}
    changed = [labels.get(key, key) for key in saved]
    return {"ok": True, "job_id": job_id, "changed": changed,
            "command": _quote(_command({**saved, "paused": saved.get("enabled") is False}, job_id)),
            "note": "Изменения применены штатной командой Hermes по вашему подтверждению."}


def delete(app, job_id: str) -> dict:
    """`DELETE /api/duties/{id}` → корзина на 30 дней."""
    st = _state(app)
    found = None
    for item in list(st["created"]):
        if str(item.get("job_id")) == job_id:
            found = item
            st["created"].remove(item)
            break
    if found is None:
        # Удаление дежурства из фикстуры: показываем в корзине, список не трогаем.
        found = st["patched"].get(job_id) or {"job_id": job_id, "name": job_id}
    if found.get("job_id") and found.get("kind"):
        st["deleted"][job_id] = copy.deepcopy(found)      # вернуть из корзины — тот же объект
    record = {"job_id": job_id, "title": found.get("name") or found.get("title") or job_id,
              "trashed_at": now(), "expires_at": now() + 30 * 86400}
    st["trash"].append(record)
    return {"ok": True, "trashed": copy.deepcopy(record),
            "text": "Дежурство убрано в корзину: вернуть можно 30 дней."}


def restore(app, job_id: str) -> dict:
    st = _state(app)
    found = None
    for record in list(st["trash"]):
        if str(record.get("job_id")) == job_id:
            found = record
            st["trash"].remove(record)
            break
    if found is None:
        raise ApiError(404, "unknown_job", "В корзине такого дежурства нет", {"job_id": job_id})
    back = st["deleted"].pop(job_id, None)
    if back is not None and all(str(one.get("job_id")) != job_id for one in st["created"]):
        st["created"].append(back)                        # вернулось в список, а не только из корзины
    return {"ok": True, "restored": copy.deepcopy(found),
            "text": "Дежурство возвращено: расписание снова работает."}


def history(app, job_id: str) -> dict:
    """`GET /api/duties/{id}/history` — строки запусков (демо)."""
    return {"job_id": job_id, "items": _history_for(job_id, 4), "generated_at": now()}


def dry_run(app, job_id: str) -> dict:
    """`POST /api/duties/{id}/dry_run` — что бы нашло, без побочных действий."""
    item = None
    for one in _state(app)["created"]:
        if str(one.get("job_id")) == job_id:
            item = one
    kind = str((item or {}).get("kind") or "model")
    if kind == "script":
        return {"ok": True, "job_id": job_id, "changed": "нет", "found": [],
                "at": now(), "text": "Изменений бы не нашло: проверено 12 пунктов.",
                "note": "Ничего не записано: это сухой прогон."}
    return {"ok": True, "job_id": job_id, "changed": "да",
            "found": ["2 новых письма в ящике", "1 дело без движения"],
            "at": now(),
            "text": "Нашло бы 2 изменения: 2 новых письма в ящике; 1 дело без движения.",
            "note": "Ничего не записано: это сухой прогон."}


def act(app, job_id: str, body: dict) -> dict:
    """`POST /api/duties/{id}/actions`: run|pause|resume|schedule — только с confirm."""
    data = dict(body or {})
    action = str(data.get("action") or "").strip().lower()
    if action not in ("run", "pause", "resume", "schedule"):
        raise ApiError(400, "bad_request", "Неизвестное действие с дежурством",
                       {"action": action, "allowed": ["run", "pause", "resume", "schedule"]})
    if not data.get("confirm"):
        raise ApiError(409, "confirm_required",
                       "Действие с дежурством требует подтверждения: confirm:true",
                       {"action": action})
    st = _state(app)
    saved = {}
    if action in ("pause", "resume"):
        saved["enabled"] = action == "resume"
        st["patched"].setdefault(job_id, {}).update(saved)
    if action == "schedule":
        schedule = str(data.get("schedule") or "").strip()
        if not schedule:
            raise ApiError(400, "bad_request", "Нужно расписание", {"field": "schedule"})
        saved["schedule"] = schedule
        saved["schedule_human"] = _human(schedule)
        st["patched"].setdefault(job_id, {}).update(saved)
        return {"ok": True, "applied": False,
                "text": "Предложение сохранено: расписание в Hermes не изменено."}
    texts = {"run": "Запуск поставлен в работу — итог появится в истории.",
             "pause": "Дежурство поставлено на паузу.",
             "resume": "Дежурство снова в расписании."}
    for item in st["created"]:
        if str(item.get("job_id")) == job_id:
            item.update(saved)
    return {"ok": True, "applied": True, "action": action,
            "command": _quote(["hermes", "cron", action, job_id]),
            "text": texts[action]}


def proposal_action(app, proposal_id: str, body: dict) -> dict:
    """`POST /api/duties/proposals/{id}/actions`: accept|dismiss|never|edit."""
    st = _state(app)
    action = str((body or {}).get("action") or "").strip().lower()
    if action not in ("accept", "dismiss", "never", "edit"):
        raise ApiError(400, "bad_request", "Неизвестный ответ на предложение",
                       {"action": action, "allowed": ["accept", "dismiss", "never", "edit"]})
    found = None
    for item in list(st["proposals"]):
        if str(item.get("id")) == proposal_id:
            found = item
            break
    if found is None:
        raise ApiError(409, "already_decided",
                       "Это предложение уже закрыто", {"proposal_id": proposal_id})
    if action == "edit":
        return {"ok": True, "draft": copy.deepcopy(found)}
    st["proposals"].remove(found)
    if action == "never":
        st["never"].append(str(found.get("title") or ""))
        return {"ok": True, "text": "Больше не будем предлагать такие дежурства."}
    if action == "accept":
        st["accepted"].append(proposal_id)
        return {"ok": True, "text": "Предложение принято — дежурство создано."}
    return {"ok": True, "text": "Предложение отклонено."}
