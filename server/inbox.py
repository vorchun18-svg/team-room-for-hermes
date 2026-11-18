"""Входящие комнаты (W26, пакет D9): быстрая запись и разбор.

Что здесь:

* хранилище ``data/state/<room>/inbox.json`` (через ``statefiles``, атомарно):
  текст, источник «диктовка|вручную», время, состояние разбора;
* очередь карточек руководителю (``queue``): «разобрать входящее» — карточка
  кладётся штатной функцией ЭТОГО модуля, в песочнице, без запуска агентов;
* шаблон-предложение «Предлагаю оформить так: … [Принять][Изменить][Отклонить]».
  Сам разбор делает руководитель-модель (в этой волне — шаблон и кнопки),
  поэтому предложение всегда собирается из текста записи детерминированно.

Правило «без тупиков»: у каждой записи, которая чего-то ждёт, есть действие.
Запись в состоянии ``processing`` всегда предлагает «Предложить оформление
сейчас», а предложение — три кнопки; удаление и восстановление доступны всегда.

Заготовки разбора (принятые предложения) лежат в самом ``inbox.json``:
``delos_drafts`` (в дело), ``steps_drafts`` (в шаг), ``questions_drafts``
(вопрос), ``duties_drafts`` (дежурство), ``notes`` (справка). Это честная
граница волны: дела (D5) и дежурства (D8) — отдельные пакеты со своими
хранилищами, поэтому здесь остаются заготовки с текстом, а не выдуманные
записи в чужих файлах.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

try:                                     # пакетный и «плоский» импорт
    from . import statefiles
except ImportError:                      # pragma: no cover
    import statefiles                    # type: ignore

#: Виды разбора. ``note`` — «справка»: текст остаётся, дело не создаётся.
KINDS = ("delo", "step", "question", "duty", "note")
KIND_TITLES = {
    "delo": "Дело",
    "step": "Шаг дела",
    "question": "Вопрос руководителю",
    "duty": "Дежурство",
    "note": "Справка",
}
#: Источники записи: диктовка (микрофон) или набрано руками.
SOURCES = {"dictation": "диктовка", "manual": "вручную"}
STATUSES = ("new", "processing", "processed", "deleted")
#: Сколько знаков принимаем в одну запись (защита от вставки мегабайта).
MAX_TEXT = 4000
#: Сколько символов текста идёт в название предложения.
TITLE_LIMIT = 90

ITEM_RE = re.compile(r"^in_[a-z0-9]{4,32}$")

ACTIONS = ("to_delo", "to_step", "to_question", "to_duty", "to_note",
           "propose", "accept", "reject", "edit", "delete", "restore")

PROPOSAL_ACTIONS = [
    {"id": "accept", "label": "Принять"},
    {"id": "edit", "label": "Изменить"},
    {"id": "reject", "label": "Отклонить"},
]


class InboxError(Exception):
    """Ошибка входящих с HTTP-кодом и русским текстом (как в duties/inbox API)."""

    def __init__(self, http: int, message: str, code: str = "bad_request", detail=None):
        super().__init__(message)
        self.http = int(http)
        self.message = message
        self.code = code
        self.detail = dict(detail or {})


# --- время и идентификаторы -------------------------------------------------
def _now_iso() -> str:
    """Время с местным смещением: человек видит свои часы, сверка — смещение."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return "in_" + secrets.token_hex(4)


def _stamp(iso: str) -> str:
    """ISO → «ЧЧ:ММ» локально (для списка); неизвестное время — пустая строка."""
    try:
        return datetime.fromisoformat(str(iso)).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


# --- хранилище --------------------------------------------------------------
def inbox_path(cfg, room: str):
    """``data/state/<room>/inbox.json`` — рядом с остальным состоянием комнаты."""
    return cfg.data_dir / "state" / str(room) / "inbox.json"


def _empty(room: str) -> dict:
    return {"room": str(room), "items": [], "queue": [], "notes": [],
            "delos_drafts": [], "steps_drafts": [], "questions_drafts": [],
            "duties_drafts": [], "updated_at": None}


def _normalize(doc, room: str) -> dict:
    """Привести прочитанный файл к известной форме, не теряя незнакомые поля."""
    if not isinstance(doc, dict):
        return _empty(room)
    out = dict(doc)
    out["room"] = str(doc.get("room") or room)
    for key in ("items", "queue", "notes", "delos_drafts", "steps_drafts",
                "questions_drafts", "duties_drafts"):
        value = out.get(key)
        out[key] = [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []
    out.setdefault("updated_at", None)
    return out


def load(cfg, room: str) -> tuple[dict, dict]:
    """(документ, info). Битый файл уводим в копию и начинаем с чистого листа."""
    value, info = statefiles.read_json_doc(inbox_path(cfg, room), None)
    state = info.get("state")
    if state == "unreadable":
        raise InboxError(503, "Входящие сейчас недоступны: файл занят. Повторите попытку.",
                         code="state_unreadable", detail={"path": info.get("path")})
    if state == "corrupt":
        statefiles.quarantine(info.get("path") or "")
        return _empty(room), info
    return _normalize(value, room), info


def save(cfg, room: str, doc: dict) -> dict:
    doc = _normalize(doc, room)
    doc["updated_at"] = _now_iso()
    statefiles.write_json_atomic(inbox_path(cfg, room), doc)
    return doc


def counts(doc: dict) -> dict:
    items = doc.get("items") or []
    by = {name: 0 for name in STATUSES}
    for item in items:
        status = str(item.get("status") or "new")
        if status not in by:
            status = "new"
        by[status] += 1
    waiting = sum(1 for item in items
                  if item.get("status") in ("new", "processing")
                  and isinstance(item.get("proposal"), dict)
                  and item["proposal"].get("state") == "waiting")
    return {
        "new": by["new"],
        "processing": by["processing"],
        "processed": by["processed"],
        "deleted": by["deleted"],
        "total": len(items),
        # «Ждут вас»: новые и в разборе — у каждой есть действие (см. ACTIONS).
        "active": by["new"] + by["processing"],
        "proposals": waiting,
    }


def items(cfg, room: str, status: str = "active") -> dict:
    """Список записей. ``status``: ``active`` | ``new`` | ``processing`` | ``processed`` | ``deleted`` | ``all``."""
    doc, info = load(cfg, room)
    wanted = str(status or "active")
    if wanted not in ("active", "new", "processing", "processed", "deleted", "all"):
        raise InboxError(400, "Неизвестный отбор входящих: %s" % wanted,
                         detail={"allowed": ["active", "new", "processing", "processed",
                                             "deleted", "all"]})
    listed = []
    for index, item in enumerate(doc.get("items") or []):
        current = str(item.get("status") or "new")
        if wanted == "all" or (wanted == "active" and current in ("new", "processing")) or current == wanted:
            listed.append((str(item.get("created_at") or ""), index, _public(item)))
    # Новые сверху; при одинаковом времени (запись в одну секунду) — по порядку
    # добавления, позже добавленная выше.
    listed.sort(key=lambda row: (row[0], row[1]), reverse=True)
    listed = [row[2] for row in listed]
    return {
        "room": str(room),
        "generated_at": _now_iso(),
        "filter": wanted,
        "counts": counts(doc),
        "items": listed,
        "notes": list(doc.get("notes") or [])[:50],
        "queue": [card for card in (doc.get("queue") or []) if card.get("state") == "waiting"][:50],
        "updated_at": doc.get("updated_at"),
        "warning": "Файл входящих был повреждён; копия сохранена рядом, список начат заново."
        if info.get("state") == "corrupt" else "",
    }


def _public(item: dict) -> dict:
    """Наружу — только известные поля записи (без служебных хвостов)."""
    out = {
        "id": str(item.get("id") or ""),
        "room": str(item.get("room") or ""),
        "text": str(item.get("text") or ""),
        "source": str(item.get("source") or "manual"),
        "source_title": SOURCES.get(str(item.get("source") or "manual"), "вручную"),
        "created_at": str(item.get("created_at") or ""),
        "created_hhmm": _stamp(item.get("created_at")),
        "status": str(item.get("status") or "new"),
        "kind": item.get("kind") if item.get("kind") in KINDS else None,
        "resolution": item.get("resolution") if isinstance(item.get("resolution"), dict) else None,
        "proposal": item.get("proposal") if isinstance(item.get("proposal"), dict) else None,
        "note": str(item.get("note") or ""),
        "actions": _actions_for(item),
    }
    return out


def _actions_for(item: dict) -> list:
    """Кнопки записи: у любого состояния есть хотя бы одно действие (без тупиков)."""
    status = str(item.get("status") or "new")
    proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else None
    waiting = bool(proposal and proposal.get("state") == "waiting")
    actions = []
    if status == "deleted":
        return [{"id": "restore", "label": "Вернуть"}]
    if waiting:
        return [dict(a) for a in PROPOSAL_ACTIONS] + [{"id": "delete", "label": "Удалить"}]
    if status in ("new", "processing"):
        actions.append({"id": "propose", "label": "Разобрать руководителем"})
        for action, kind in (("to_delo", "delo"), ("to_step", "step"),
                             ("to_question", "question"), ("to_duty", "duty"),
                             ("to_note", "note")):
            actions.append({"id": action, "label": KIND_TITLES[kind]})
        actions.append({"id": "delete", "label": "Удалить"})
        return actions
    # Разобрано: можно удалить или вернуть в работу.
    actions.append({"id": "restore", "label": "Вернуть в работу"})
    actions.append({"id": "delete", "label": "Удалить"})
    return actions


# --- запись ----------------------------------------------------------------
def _clean_text(text) -> str:
    value = str(text if text is not None else "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        raise InboxError(400, "Пустая запись: надиктуйте или наберите текст",
                         code="bad_request", detail={"missing": ["text"]})
    if len(value) > MAX_TEXT:
        raise InboxError(400, "Слишком длинная запись: до %d знаков" % MAX_TEXT,
                         detail={"limit": MAX_TEXT, "got": len(value)})
    return value


def add(cfg, room: str, text, source: str = "manual") -> dict:
    """Добавить запись. Источник: ``dictation`` (микрофон) или ``manual``."""
    value = _clean_text(text)
    kind = str(source or "manual").strip().lower()
    if kind not in SOURCES:
        raise InboxError(400, "Неизвестный источник записи: %s" % kind,
                         detail={"allowed": sorted(SOURCES)})
    doc, _info = load(cfg, room)
    item = {
        "id": new_id(),
        "room": str(room),
        "text": value,
        "source": kind,
        "created_at": _now_iso(),
        "status": "new",
        "kind": None,
        "proposal": None,
        "resolution": None,
        "note": "",
    }
    doc["items"].append(item)
    save(cfg, room, doc)
    return _public(item)


# --- предложение руководителя ----------------------------------------------
def guess_kind(text: str) -> str:
    """Черновая догадка вида разбора по словам. Модель может решить иначе."""
    low = " " + str(text or "").lower().replace("ё", "е") + " "
    if any(word in low for word in (" кажд", " еженед", " ежеднев", " напомина", " крон",
                                    " расписан", " по понедельник", " раз в ")):
        return "duty"
    if "?" in low or any(word in low for word in (" почему ", " как ", " уточн", " спрос")):
        return "question"
    if any(word in low for word in (" шаг ", " этап ", " пункт ", " подзадач")):
        return "step"
    if any(word in low for word in (" справк", " заметк", " запомн", " реквизит", " адрес ")):
        return "note"
    return "delo"


def _title_of(text: str) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= TITLE_LIMIT:
        return flat
    cut = flat[:TITLE_LIMIT]
    space = cut.rfind(" ")
    return (cut[:space] if space > 40 else cut).rstrip(" ,.;:") + "…"


def _proposal_body(kind: str, title: str, text: str) -> str:
    flat = " ".join(str(text or "").split())
    if kind == "delo":
        return ("Дело «%s». Что сделать: %s\nЭтапы по умолчанию: Сбор → Черновик → "
                "Согласование → Исполнение → Готово." % (title, flat))
    if kind == "step":
        return ("Один шаг в текущем деле: «%s». Основание: %s" % (title, flat))
    if kind == "question":
        return "Вопрос руководителю: %s\nОтвет придёт в «От вас нужно» комнаты." % flat
    if kind == "duty":
        return ("Дежурство: «%s». Что делать: %s\nЧастоту подтвержу отдельно — "
                "без вашего «да» ничего не запускается." % (title, flat))
    return "Справка: «%s». Сохраню в справки комнаты, дело не создаётся." % title


def build_proposal(item: dict, kind: str | None = None) -> dict:
    """Шаблон-предложение (детерминированный): вид, название, пояснение, кнопки."""
    text = str(item.get("text") or "")
    chosen = str(kind or "").strip() or guess_kind(text)
    if chosen not in KINDS:
        raise InboxError(400, "Неизвестный вид разбора: %s" % chosen,
                         detail={"allowed": list(KINDS)})
    title = _title_of(text)
    return {
        "id": "pr_" + secrets.token_hex(4),
        "at": _now_iso(),
        "kind": chosen,
        "kind_title": KIND_TITLES[chosen],
        "title": title,
        "body": _proposal_body(chosen, title, text),
        "state": "waiting",
        "by": "шаблон",
        "actions": [dict(a) for a in PROPOSAL_ACTIONS],
    }


def _queue_card(room: str, item: dict, proposal: dict) -> dict:
    """Карточка-задача «разобрать входящее» в очередь руководителя (в песочнице)."""
    return {
        "id": "mq_" + secrets.token_hex(4),
        "room": str(room),
        "item_id": str(item.get("id") or ""),
        "text": "Разобрать входящее: %s\nЧто сделать: разобрать и вернуть предложение "
                "(дело / шаг / вопрос / дежурство / справка)." % _title_of(item.get("text") or ""),
        "kind": proposal.get("kind"),
        "at": _now_iso(),
        "state": "waiting",
        "branch": "руководитель",
    }


# --- действия ---------------------------------------------------------------
def _find(doc: dict, item_id: str) -> dict:
    wanted = str(item_id or "").strip()
    if not ITEM_RE.match(wanted):
        raise InboxError(404, "Запись входящих не найдена: %s" % wanted, code="not_found")
    for item in doc.get("items") or []:
        if str(item.get("id") or "") == wanted:
            return item
    raise InboxError(404, "Запись входящих не найдена: %s" % wanted, code="not_found")


def _draft(store: str, item: dict, proposal: dict) -> dict:
    return {
        "id": "dr_" + secrets.token_hex(4),
        "item_id": str(item.get("id") or ""),
        "title": str(proposal.get("title") or ""),
        "text": str(item.get("text") or ""),
        "at": _now_iso(),
        "state": "draft",
        "source": "входящие",
    }


def act(cfg, room: str, item_id: str, action: str, payload: dict | None = None) -> dict:
    """Одно действие над записью. Возвращает свежий список (как PUT дежурств)."""
    name = str(action or "").strip().lower()
    if name not in ACTIONS:
        raise InboxError(400, "Неизвестное действие: %s" % name,
                         detail={"allowed": list(ACTIONS)})
    data = payload if isinstance(payload, dict) else {}
    doc, _info = load(cfg, room)
    item = _find(doc, item_id)
    kind = str(data.get("kind") or "").strip() or None

    if name in ("to_delo", "to_step", "to_question", "to_duty", "to_note"):
        kind = {"to_delo": "delo", "to_step": "step", "to_question": "question",
                "to_duty": "duty", "to_note": "note"}[name]
        kind = None if kind not in KINDS else kind
    if name == "propose":
        pass

    status = str(item.get("status") or "new")

    if name in ("propose", "to_delo", "to_step", "to_question", "to_duty", "to_note"):
        if status == "deleted":
            raise InboxError(409, "Запись удалена — сначала верните её в работу")
        proposal = build_proposal(item, kind)
        item["proposal"] = proposal
        item["kind"] = proposal["kind"]
        item["status"] = "processing"
        item["note"] = "Ушло руководителю: ждёт вашего «Принять»." if name == "propose" \
            else "Разбор: %s. Проверьте предложение и примите." % KIND_TITLES[proposal["kind"]]
        doc["queue"].append(_queue_card(room, item, proposal))
        doc["queue"] = doc["queue"][-200:]
    elif name == "edit":
        proposal = item.get("proposal")
        if not isinstance(proposal, dict) or proposal.get("state") != "waiting":
            raise InboxError(409, "Нет предложения, которое можно изменить")
        text = str(data.get("text") or "").strip()
        if not text:
            raise InboxError(400, "Нужен текст предложения", detail={"missing": ["text"]})
        proposal["body"] = text[:MAX_TEXT]
        if str(data.get("title") or "").strip():
            proposal["title"] = str(data["title"]).strip()[:TITLE_LIMIT]
        proposal["by"] = "ваша правка"
        proposal["edited_at"] = _now_iso()
        item["proposal"] = proposal
        item["note"] = "Предложение изменено — примите или отклоните."
    elif name == "accept":
        proposal = item.get("proposal")
        if not isinstance(proposal, dict) or proposal.get("state") != "waiting":
            raise InboxError(409, "Нечего принимать: предложения нет")
        proposal["state"] = "accepted"
        proposal["decided_at"] = _now_iso()
        chosen = proposal.get("kind") if proposal.get("kind") in KINDS else "note"
        item["kind"] = chosen
        item["status"] = "processed"
        item["resolution"] = {"action": "accept", "kind": chosen,
                              "kind_title": KIND_TITLES[chosen],
                              "title": proposal.get("title"), "at": _now_iso()}
        store = {"delo": "delos_drafts", "step": "steps_drafts", "question": "questions_drafts",
                 "duty": "duties_drafts", "note": "notes"}[chosen]
        doc[store].append(_draft(store, item, proposal))
        doc[store] = doc[store][-500:]
        item["note"] = "Принято: %s." % KIND_TITLES[chosen]
        for card in doc["queue"]:
            if card.get("item_id") == item.get("id") and card.get("state") == "waiting":
                card["state"] = "done"
                card["done_at"] = _now_iso()
    elif name == "reject":
        proposal = item.get("proposal")
        if not isinstance(proposal, dict) or proposal.get("state") != "waiting":
            raise InboxError(409, "Нечего отклонять: предложения нет")
        proposal["state"] = "rejected"
        proposal["decided_at"] = _now_iso()
        item["status"] = "new"
        item["kind"] = None
        item["note"] = str(data.get("text") or "").strip()[:500] or \
            "Отклонено — разберите иначе или удалите."
        for card in doc["queue"]:
            if card.get("item_id") == item.get("id") and card.get("state") == "waiting":
                card["state"] = "declined"
                card["done_at"] = _now_iso()
    elif name == "delete":
        item["status"] = "deleted"
        item["deleted_at"] = _now_iso()
        item["note"] = "Удалено — можно вернуть."
    elif name == "restore":
        item["status"] = "new" if not item.get("kind") else "processed"
        item["restored_at"] = _now_iso()
        item["note"] = "Возвращено в работу."
    else:                                  # pragma: no cover - ACTIONS закрыт выше
        raise InboxError(400, "Действие не поддержано: %s" % name)

    save(cfg, room, doc)
    fresh = _public(item)
    listing = items(cfg, room, "active")
    return {"ok": True, "item": fresh, "counts": listing["counts"],
            "generated_at": listing["generated_at"]}


def summary(cfg, room: str) -> dict:
    """Короткая сводка для бейджа: сколько записей ждёт разбора."""
    doc, _info = load(cfg, room)
    return counts(doc)
