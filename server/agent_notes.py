"""«Особенности для этой комнаты» — добавка к SOUL.md ТОЛЬКО для этой комнаты.

История: WAVE9 §9 (простое поле «text»), WAVE14 пакет I / пункт 13 замечаний Михаила.
Файлы профилей (SOUL.md, config.yaml, skills) НЕ меняются никогда: записи живут в реестре
комнат (`data/rooms.json`) и доходят до агента двумя каналами:

1. блок «Особые указания участникам этой комнаты» в теле карточки
   (`context_pack.build_context_block`, общий лимит раздела 3 000 знаков);
2. автокомментарий `[Указание комнаты · <hash8>] <текст>` на карточки дерева
   запроса (`ensure_comments`, вызывается из watcher лениво и идемпотентно).

Структура (W14-I, пункт 13): у участника не сплошной текст, а СПИСОК записей

    entries = [{id, text, source(user|leader|auto), origin(task/комментарий),
                created_at, used_count, pinned}]

`text` рядом — производное (склейка записей): его читают старые потребители
(CONTEXT.md, Vault, комментарии, тесты). Ручные записи Михаила — `pinned`: руководитель
их не удаляет. Рост ограничен (`MAX_ENTRIES`, `MAX_TEXT`), каждые `REVIEW_EVERY` записей
и при превышении лимита идёт переоценка (`review`): дубли сливаются, устаревшее и не
использованное убирается. Каждое изменение — событие журнала комнаты (`agent_notes_log`)
с возможностью откатить конкретное изменение (`rollback`).

Публичные функции::

    validate(value, members=None) -> (notes, warnings)     # строгая проверка (API)
    normalize(value) -> notes                              # мягкое чтение реестра
    notes_of(room) -> {профиль: {text, entries, updated_at, review}}
    entries_of(item) -> [запись]                           # нормализованный список
    participants(room) -> [(профиль, роль по-русски)]
    get(cfg, room_id, profile) -> {profile, role, entries, text, limits, review}
    set(cfg, room_id, profile, text) -> {...}              # полный текст (старый PUT)
    add(cfg, room_id, profile, text, source=..., origin=..., why=...) -> {...}
    update_entry(cfg, room_id, profile, entry_id, text, why=...) -> {...}
    remove_entry(cfg, room_id, profile, entry_id, why=..., force=False) -> {...}
    set_pinned(cfg, room_id, profile, entry_id, pinned) -> {...}
    review(cfg, room_id, profile, apply=True) -> {...}     # переоценка (слияние/удаление)
    touch_used(cfg, room_id, profile, entry_ids=None) -> int
    journal(cfg, room_id, limit=50) -> [событие]
    rollback(cfg, room_id, event_id) -> {...}
    render_block(cfg, room, limit=3000) -> str             # раздел для карточки
    comment_text(profile, text, *, updated=False) -> str   # одна строка с маркером
    ensure_comments(cfg, room, root_id) -> {written, skipped, errors}

Свойства: ничего не пишем в файлы профилей; комментарии — только через
`engine_write` (CLI `hermes kanban`), только для карточек дерева запроса
комнаты; ошибки никогда не роняют watcher.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

try:                                    # import server.agent_notes
    from . import statefiles as sf      # noqa: F401  (единый стиль с соседями)
except ImportError:                      # python server/app.py (плоский импорт)
    import statefiles as sf              # type: ignore  # noqa: F401

#: публичная функция `set` (договорённость §9) занимает имя — конструктор множеств прячем
_new_set = set

MAX_TEXT = 8000                 # знаков в указаниях одного участника (все записи вместе)
SECTION_LIMIT = 3000            # знаков на раздел в теле карточки (WAVE9 §9)
MAX_SECTION_ENTRY = 700         # знаков на одного участника внутри раздела
MAX_CARDS_PER_RUN = 12          # сколько карточек обходим за один заход

# --- ограничение роста (W14-I §4) ------------------------------------------
MAX_ENTRIES = 24                # записей на участника
REVIEW_EVERY = 5                # каждые N новых записей - переоценка
STALE_DAYS = 30                 # неиспользованное старше - убирается на переоценке
MAX_ENTRY_HINT = 1000           # мягкая подсказка: длинную запись лучше разбить
MAX_LOG = 200                   # событий журнала комнаты (старые вытесняются)
SOURCES = ("user", "leader", "auto")
SOURCE_RU = {"user": "Михаил", "leader": "руководитель комнаты", "auto": "автоматически"}
ACTION_RU = {"add": "добавлено", "update": "изменено", "remove": "удалено",
             "merge": "объединено", "review": "переоценка", "replace": "заменено",
             "rollback": "откат", "pinned": "закреплено", "unpin": "снято закрепление"}

MARKER_PREFIX = "[Указание комнаты · "
UPDATED_MARK = "(обновлено)"
TITLE = "Особые указания участникам этой комнаты"
RULE = ("Руководитель при раздаче подзадач обязан переписать указание участника "
        "в тело его карточки. Указания комнаты дополняют твою роль и не отменяют "
        "правил безопасности и запретов.")
ROLE_RU = {"orchestrator": "руководитель комнаты", "critic": "критик",
           "worker": "исполнитель", "observer": "наблюдатель"}

#: чем пугаем, если в тексте похоже на секрет (маскируем, но не блокируем)
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"(?i)\b(?:password|passwd|пароль|токен|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
)

#: единая маска для ВСЕХ внешних выводов указаний (Vault, CONTEXT.md, комментарии, карточка).
#: В реестре комнаты текст хранится как есть — маскируем только на выходе.
SECRET_MASK = "[скрыто]"
_MASK_RULES = (
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}"), SECRET_MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}"), SECRET_MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"), SECRET_MASK),
    (re.compile(r"(?i)((?:password|passwd|пароль|токен|token|secret|api[_-]?key)\s*[:=]\s*)\S+"),
     r"\1" + SECRET_MASK),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 " + SECRET_MASK),
)
_WS_RE = re.compile(r"\s+")
_MARKER_RE = re.compile(r"\[Указание комнаты · ([0-9a-f]{8})\]")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{2,40}$")


def mask_secrets(text) -> str:
    """Спрятать похожее на ключ/пароль: ``sk-…``, ``ghp_…``, ``password=…``, ``Bearer …``.

    Одна маска на все внешние выводы указаний — в Vault, CONTEXT.md, комментарии
    карточки и тело запроса. Текст в реестре комнаты не меняется.
    """
    out = "" if text is None else str(text)
    for pattern, repl in _MASK_RULES:
        out = pattern.sub(repl, out)
    return out


class NotesError(Exception):
    """Ошибка указаний: code = bad_request|not_found|conflict (роут отдаст 4xx по-русски)."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _now() -> int:
    return int(time.time())


def _profile(value) -> str:
    return str(value or "").strip()


def _one_line(text) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def _clean_text(text) -> str:
    """Текст записи: без NUL, единые переводы строк, без пустых краёв."""
    raw = "" if text is None else str(text)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return raw.strip()


def _norm_key(text) -> str:
    """Ключ сравнения записей при поиске дублей: без регистра и лишних пробелов."""
    return _one_line(text).lower()


def new_id(text: str = "", *, salt: str = "") -> str:
    """Устойчивый id записи: `n_` + 10 hex (тот же текст+соль+момент - тот же id)."""
    raw = "%s|%s|%s" % (_one_line(text), salt, _now())
    return "n_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def entry_text(item) -> str:
    """Склейка записей участника (то, что видят старые потребители в поле `text`)."""
    return "\n".join(entry["text"] for entry in entries_of(item))


# ---------------------------------------------------------------- проверка
def secret_warnings(text) -> list[str]:
    """Предупреждения о похожем на секрет (маска в тексте, не блокировка)."""
    raw = str(text or "")
    found: list[str] = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.findall(raw):
            head = str(match).split(":", 1)[0].split("=", 1)[0].strip()
            masked = ("%s…" % head[:6]) if head and head.lower() not in ("password", "passwd",
                                                                      "пароль", "токен", "token",
                                                                      "secret", "api_key", "apikey") \
                else "%s=…" % (head or "секрет")
            if masked not in found:
                found.append(masked)
    if not found:
        return []
    return ["Похоже на ключ или пароль (%s). В указаниях лучше не хранить секреты — "
            "они попадут в карточки и в Obsidian." % ", ".join(found[:4])]


def _clean_id(value, text: str = "", salt: str = "") -> str:
    raw = str(value or "").strip()
    if _ID_RE.match(raw):
        return raw
    return new_id(text, salt=salt)


def _entry(raw, *, stamp: int = 0, index: int = 0) -> dict | None:
    """Одна запись из реестра: строка/объект - канонический вид (мусор - None)."""
    if isinstance(raw, str):
        text = _clean_text(raw)
        if not text:
            return None
        return {"id": _clean_id("", text, "legacy"), "text": text, "source": "user",
                "origin": "", "created_at": int(stamp or 0), "used_count": 0, "pinned": True}
    if not isinstance(raw, dict):
        return None
    text = _clean_text(raw.get("text"))
    if not text:
        return None
    source = str(raw.get("source") or "").strip().lower()
    if source not in SOURCES:
        source = "auto" if raw.get("pinned") is False else "user"
    created = 0
    for key in ("created_at", "updated_at"):
        try:
            created = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            created = 0
        if created:
            break
    try:
        used = max(0, int(raw.get("used_count") or 0))
    except (TypeError, ValueError):
        used = 0
    pinned = raw.get("pinned")
    if pinned is None:
        pinned = source == "user"
    return {"id": _clean_id(raw.get("id"), text, "%s|%d" % (source, index)),
            "text": text, "source": source,
            "origin": _one_line(raw.get("origin"))[:200], "created_at": created,
            "used_count": used, "pinned": bool(pinned)}


def entries_of(item) -> list:
    """Список записей участника в каноническом виде (старое поле `text` - одной записью)."""
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        return []
    raw = item.get("entries")
    out: list = []
    if isinstance(raw, list):
        for index, one in enumerate(raw):
            entry = _entry(one, stamp=item.get("updated_at") or 0, index=index)
            if entry:
                out.append(entry)
    if not out:
        legacy = _clean_text(item.get("text"))
        if legacy:
            one = _entry({"text": legacy, "source": "user", "pinned": True,
                          "created_at": item.get("updated_at") or 0})
            if one:
                out.append(one)
    # дубли id внутри одного участника недопустимы: второй получает новый id
    seen: set = _new_set()
    for entry in out:
        if entry["id"] in seen:
            entry["id"] = _clean_id("", entry["text"], entry["id"] + entry["source"])
        seen.add(entry["id"])
    return out[:MAX_ENTRIES * 4]           # страховка от мусора в реестре


def _review_of(item) -> dict:
    """Состояние переоценки: принимает и запись участника, и само состояние."""
    raw = item if isinstance(item, dict) else {}
    if isinstance(raw.get("review"), dict):
        raw = raw["review"]
    try:
        count = max(0, int(raw.get("count_at_review") or 0))
    except (TypeError, ValueError):
        count = 0
    try:
        stamp = max(0, int(raw.get("reviewed_at") or 0))
    except (TypeError, ValueError):
        stamp = 0
    return {"count_at_review": count, "reviewed_at": stamp, "due": bool(raw.get("due"))}


def _item(entries: list, *, updated_at: int = 0, review: dict | None = None) -> dict:
    """Каноническая запись участника: `text` (склейка) + `entries` + `review`."""
    entries = [one for one in (entries or []) if one and _clean_text(one.get("text"))]
    out = {"text": "\n".join(one["text"] for one in entries), "updated_at": int(updated_at or 0),
           "entries": entries, "review": _review_of(review)}
    if out["review"].get("due"):
        out["review_due"] = True
    return out


def _from_value(raw_value) -> list:
    """Значение из реестра/API - список записей (старый формат тоже понимаем)."""
    if isinstance(raw_value, str):
        return entries_of({"text": raw_value})
    if isinstance(raw_value, dict):
        return entries_of(raw_value)
    return []


def _clamp(entries: list) -> list:
    """Страховка чтения реестра: суммарный текст не больше MAX_TEXT (лишнее — «…»)."""
    out: list = []
    left = MAX_TEXT
    for entry in entries:
        text = str(entry.get("text") or "")
        if len(text) > left:
            entry = dict(entry, text=(text[: max(0, left - 1)].rstrip() + "…") if left > 1 else "")
            if entry["text"]:
                out.append(entry)
            break
        out.append(entry)
        left -= len(text) + 1
    return out


def validate(value, members=None) -> tuple[dict, list[str]]:
    """Строгая проверка поля `agent_notes` (API): профили — из состава комнаты.

    Возвращает `(notes, warnings)`; `notes = {профиль: {text, entries, updated_at, review}}`.
    Принимает и старое `{профиль: текст}`, и новое `{профиль: {entries: [...]}}`.
    Пустой текст участника — указаний нет (запись удаляется). Ошибки — `NotesError`
    с русским сообщением и кодом `bad_request` (роут отдаст 400, не 500).
    """
    if value in (None, ""):
        return {}, []
    if not isinstance(value, dict):
        raise NotesError("Особые указания: нужен объект «профиль → записи»", "bad_request",
                         {"field": "agent_notes", "type": type(value).__name__})
    allowed = None
    if members is not None:
        allowed = {_profile(p) for p in members if _profile(p)}
    notes: dict[str, dict] = {}
    warnings: list[str] = []
    for raw_profile, raw_value in value.items():
        profile = _profile(raw_profile)
        if not profile:
            raise NotesError("Особые указания: пустое имя профиля", "bad_request",
                             {"field": "agent_notes"})
        if allowed is not None and profile not in allowed:
            raise NotesError(
                "Указания можно задать только участнику комнаты: «%s» в составе нет" % profile,
                "bad_request",
                {"field": "agent_notes", "profile": profile, "members": sorted(allowed)},
            )
        if raw_value is not None and not isinstance(raw_value, (dict, str)):
            raise NotesError("Особые указания «%s»: нужен текст строкой или список записей"
                             % profile, "bad_request",
                             {"field": "agent_notes", "profile": profile})
        if isinstance(raw_value, dict) and raw_value.get("entries") not in (None, []) \
                and not isinstance(raw_value.get("entries"), list):
            raise NotesError("Особые указания «%s»: «entries» — список записей" % profile,
                             "bad_request", {"field": "agent_notes", "profile": profile})
        raw_list = (raw_value.get("entries") if isinstance(raw_value, dict) else None) or []
        for one in raw_list:
            if isinstance(one, str):
                continue
            if not isinstance(one, dict):
                raise NotesError("Особые указания «%s»: запись — объект с полем «text»" % profile,
                                 "bad_request", {"field": "agent_notes", "profile": profile})
            if "\x00" in str(one.get("text") or ""):
                raise NotesError("В указаниях «%s» есть непечатаемый символ" % profile,
                                 "bad_request", {"field": "agent_notes", "profile": profile})
        if isinstance(raw_value, (str, dict)):
            raw_text = raw_value if isinstance(raw_value, str) else raw_value.get("text")
            if "\x00" in str(raw_text or ""):
                raise NotesError("В указаниях «%s» есть непечатаемый символ" % profile,
                                 "bad_request", {"field": "agent_notes", "profile": profile})
        entries = _from_value(raw_value)
        if len(entries) > MAX_ENTRIES:
            raise NotesError(
                "Указания «%s»: %d записей, а можно не больше %d. Слейте дубли и уберите "
                "устаревшее." % (profile, len(entries), MAX_ENTRIES), "bad_request",
                {"field": "agent_notes", "profile": profile, "entries": len(entries),
                 "limit": MAX_ENTRIES})
        text = "\n".join(one["text"] for one in entries)
        if not text.strip():
            continue
        if len(text) > MAX_TEXT:
            raise NotesError(
                "Указания «%s»: %d знаков, а можно не больше %d. Сократите текст."
                % (profile, len(text), MAX_TEXT), "bad_request",
                {"field": "agent_notes", "profile": profile, "length": len(text),
                 "limit": MAX_TEXT})
        try:
            stamp = int((raw_value or {}).get("updated_at") or 0) if isinstance(raw_value, dict) else 0
        except (TypeError, ValueError):
            stamp = 0
        notes[profile] = _item(
            entries, updated_at=stamp or _now(),
            review=(raw_value or {}).get("review") if isinstance(raw_value, dict) else None)
        warnings.extend(secret_warnings(text))
    return notes, warnings


def normalize(value) -> dict:
    """Мягкое чтение реестра: мусор и чужие профили не роняют список комнат."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for raw_profile, raw_value in value.items():
        profile = _profile(raw_profile)
        if not profile:
            continue
        if not isinstance(raw_value, (dict, str)):
            continue
        entries = _from_value(raw_value)
        if not entries:
            continue
        entries = _clamp(entries)
        try:
            stamp = int((raw_value or {}).get("updated_at") or 0) if isinstance(raw_value, dict) else 0
        except (TypeError, ValueError):
            stamp = 0
        out[profile] = _item(
            entries, updated_at=stamp,
            review=(raw_value or {}).get("review") if isinstance(raw_value, dict) else None)
    return out


def notes_of(room) -> dict:
    """Указания комнаты в канонической форме (нет поля — пусто)."""
    return normalize((room or {}).get("agent_notes") if isinstance(room, dict) else {})


def participants(room) -> list[tuple[str, str]]:
    """Состав комнаты по порядку: руководитель, критик, исполнители (без дублей)."""
    room = room if isinstance(room, dict) else {}
    out: list[tuple[str, str]] = []
    seen: set[str] = _new_set()
    lead = _profile(room.get("orchestrator"))
    if lead:
        out.append((lead, ROLE_RU["orchestrator"]))
        seen.add(lead)
    critic = _profile(room.get("critic"))
    if critic and critic not in seen:
        out.append((critic, ROLE_RU["critic"]))
        seen.add(critic)
    for member in room.get("members") or []:
        if not isinstance(member, dict):
            continue
        profile = _profile(member.get("profile"))
        if not profile or profile in seen:
            continue
        seen.add(profile)
        out.append((profile, ROLE_RU.get(str(member.get("role") or "worker"), "участник")))
    return out


def role_ru(room, profile) -> str:
    name = _profile(profile)
    for item, role in participants(room):
        if item == name:
            return role
    return "участник"


# ---------------------------------------------------------------- доступ к реестру
def _rooms():
    try:
        from . import rooms as mod
    except ImportError:  # pragma: no cover - плоский импорт
        import rooms as mod  # type: ignore
    return mod


def _room(cfg, room_id) -> dict:
    room = _rooms().get_room(cfg, str(room_id)) or {}
    if not room:
        raise NotesError("Комната не найдена", "not_found", {"room": str(room_id)})
    return room


def _require_member(room, profile) -> str:
    name = _profile(profile)
    if not name:
        raise NotesError("Не указан профиль участника", "bad_request", {"field": "profile"})
    members = [p for p, _role in participants(room)]
    if name not in members:
        raise NotesError(
            "«%s» не в составе комнаты — указания задаются только участникам" % name,
            "bad_request", {"field": "profile", "profile": name, "members": members})
    return name


def limits() -> dict:
    """Ограничения роста — их показывает интерфейс (не выдумывать свои)."""
    return {"max_entries": MAX_ENTRIES, "max_text": MAX_TEXT, "review_every": REVIEW_EVERY,
            "stale_days": STALE_DAYS, "entry_hint": MAX_ENTRY_HINT}


def get(cfg, room_id, profile) -> dict:
    """Указания одного участника: записи, лимиты, состояние переоценки."""
    room = _room(cfg, room_id)
    name = _profile(profile)
    item = notes_of(room).get(name) or {}
    return {"profile": name, "role": role_ru(room, name), "text": entry_text(item),
            "entries": entries_of(item), "updated_at": int((item or {}).get("updated_at") or 0),
            "review": _review_of(item), "limits": limits(), "agent_notes": notes_of(room)}


# ---------------------------------------------------------------- журнал
def _log_of(room) -> list:
    raw = (room or {}).get("agent_notes_log")
    if not isinstance(raw, list):
        return []
    return [dict(one) for one in raw if isinstance(one, dict)][:MAX_LOG]


def _event(action: str, *, profile: str = "", entry_id: str = "", text: str = "",
           before: list | None = None, after: list | None = None, why: str = "",
           origin: str = "", actor: str = "", source: str = "", note: str = "",
           extra: dict | None = None) -> dict:
    """Событие журнала комнаты: кто, что, зачем, источник — и снимки для отката."""
    stamp = _now()
    raw = "%s|%s|%s|%s|%s" % (action, profile, entry_id, _one_line(text)[:60], stamp)
    event = {"id": "j_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10],
             "at": stamp, "action": action, "profile": _profile(profile),
             "entry_id": str(entry_id or ""), "text": _clean_text(text)[:MAX_TEXT],
             "why": _one_line(why)[:300], "origin": _one_line(origin)[:200],
             "actor": _profile(actor) or SOURCE_RU.get(source or "leader", "руководитель"),
             "source": source if source in SOURCES else "leader",
             "note": _one_line(note)[:300], "rolled_back": False, "rolls_back": ""}
    if before is not None:
        event["before"] = [dict(one) for one in before]
    if after is not None:
        event["after"] = [dict(one) for one in after]
    if extra:
        event.update(extra)
    return event


def journal(cfg, room_id, limit: int = 50) -> list:
    """Журнал комнаты: новые сверху, секреты в тексте замаскированы."""
    room = _room(cfg, room_id)
    try:
        limit = max(1, min(MAX_LOG, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    out = []
    for event in _log_of(room)[:limit]:
        item = dict(event)
        item["text"] = mask_secrets(item.get("text") or "")
        item["action_ru"] = ACTION_RU.get(str(item.get("action")), str(item.get("action") or ""))
        item["source_ru"] = SOURCE_RU.get(str(item.get("source")), "руководитель комнаты")
        out.append(item)
    return out


def _write(cfg, room_id, notes: dict, log: list | None = None) -> dict:
    """Записать указания (и журнал) комнаты одним обновлением реестра."""
    patch = {"agent_notes": notes}
    if log is not None:
        patch["agent_notes_log"] = [dict(one) for one in log[:MAX_LOG]]
    updated = _rooms().update_room(cfg, str(room_id), patch)
    return updated if isinstance(updated, dict) else {}


def _with_event(room, event: dict) -> list:
    """Журнал комнаты + новое событие (новые сверху, старые вытесняются)."""
    return ([event] + _log_of(room))[:MAX_LOG]


def _result(cfg, room_id, profile, *, cleared: bool = False, extra: dict | None = None) -> dict:
    """Ответ API: свежие указания участника + его записи + лимиты."""
    room = _room(cfg, room_id)
    name = _profile(profile)
    item = notes_of(room).get(name) or {}
    entries = entries_of(item)
    out = {"profile": name, "role": role_ru(room, name), "text": entry_text(item),
           "entries": entries, "updated_at": int((item or {}).get("updated_at") or 0),
           "review": _review_of(item), "limits": limits(), "cleared": bool(cleared),
           "agent_notes": notes_of(room),
           "warnings": secret_warnings(entry_text(item))}
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------- изменение записей
def _due(review: dict, count: int) -> bool:
    """Пора переоценивать? (каждые N записей или превышен лимит)"""
    if bool((review or {}).get("due")):
        return True
    if count > MAX_ENTRIES:
        return True
    try:
        at_review = max(0, int((review or {}).get("count_at_review") or 0))
    except (TypeError, ValueError):
        at_review = 0
    return (count - at_review) >= REVIEW_EVERY


def _store(cfg, room_id, room, profile, entries: list, *, updated: int | None = None,
           review: dict | None = None, log: list | None = None) -> dict:
    notes = notes_of(room)
    if not entries:
        notes.pop(profile, None)
    else:
        notes[profile] = _item(entries, updated_at=updated or _now(), review=review)
    return _write(cfg, room_id, notes, log)


def add(cfg, room_id, profile, text, *, source: str = "leader", origin: str = "",
        why: str = "", actor: str = "", pinned: bool = False, auto_review: bool = True) -> dict:
    """Дописать запись участнику (руководитель — сам, без согласований: пункт 13).

    Источник: `user` (Михаил), `leader` (руководитель комнаты), `auto` (переоценка/система).
    Дубль по смыслу не плодится: возвращается существующая запись (`duplicate=True`).
    При достижении порога (каждые 5 записей или превышении лимита) сразу идёт переоценка.
    """
    room = _room(cfg, room_id)
    name = _require_member(room, profile)
    body = _clean_text(text)
    if not body:
        raise NotesError("Пустая запись — нечего сохранять", "bad_request", {"field": "text"})
    if "\x00" in str(text):
        raise NotesError("В записи есть непечатаемый символ", "bad_request", {"field": "text"})
    src = str(source or "leader").strip().lower()
    if src not in SOURCES:
        raise NotesError("Источник записи: user, leader или auto", "bad_request",
                         {"field": "source", "value": src})
    if src != "user":
        pinned = False
    notes = notes_of(room)
    item = notes.get(name) or {}
    entries = entries_of(item)
    key = _norm_key(body)
    for entry in entries:
        if _norm_key(entry["text"]) == key:
            return _result(cfg, room_id, name, extra={"duplicate": True, "entry": entry})
    if len(body) > MAX_TEXT:
        raise NotesError("Запись: %d знаков, а можно не больше %d" % (len(body), MAX_TEXT),
                         "bad_request", {"field": "text", "length": len(body), "limit": MAX_TEXT})
    entry = {"id": new_id(body, salt=name), "text": body, "source": src,
             "origin": _one_line(origin)[:200], "created_at": _now(),
             "used_count": 0, "pinned": bool(pinned or src == "user")}
    entries.append(entry)
    total = sum(len(one["text"]) for one in entries)
    event = _event("add", profile=name, entry_id=entry["id"], text=body, why=why,
                   origin=origin, actor=actor, source=src,
                   before=entries[:-1], after=entries)
    review_state = _review_of(item)
    reviewed = None
    if auto_review and (len(entries) > MAX_ENTRIES or total > MAX_TEXT):
        # Лимит превышен уже этой записью: сжимаем ДО записи в реестр, иначе проверка
        # реестра (validate) отклонит её — а по требованию пункта 13 переоценка должна
        # пройти сама, без участия Михаила. Закреплённые записи не трогаем.
        plan = _plan_review(entries, now=_now())
        log = _with_event(room, event)
        for drop in plan["drops"]:
            log = [_event("remove", profile=name, entry_id=drop["entry"]["id"],
                          text=drop["entry"]["text"],
                          why="переоценка: " + drop["reason"],
                          origin="порог роста записей", actor="система (переоценка)",
                          source="auto", before=entries,
                          after=plan["entries"])] + log
        for merge in plan["merges"]:
            log = [_event("merge", profile=name, entry_id=merge["from"],
                          text=merge["text"], why="переоценка: дубль слит",
                          origin="порог роста записей", actor="система (переоценка)",
                          source="auto", note="вошло в " + merge["into"],
                          before=entries, after=plan["entries"])] + log
        summary = _event("review", profile=name,
                         why="переоценка после %d записей" % len(entries),
                         origin="порог роста записей", actor="система (переоценка)",
                         source="auto",
                         note="записей %d → %d, слито %d, убрано %d"
                              % (len(entries), len(plan["entries"]), len(plan["merges"]),
                                 len(plan["drops"])),
                         before=entries, after=plan["entries"])
        log = ([summary] + log)[:MAX_LOG]
        rev = {"count_at_review": len(plan["entries"]), "reviewed_at": _now(), "due": False}
        _store(cfg, room_id, room, name, plan["entries"], review=rev, log=log)
        reviewed = {"applied": True, "profile": name, "before": len(entries),
                    "after": len(plan["entries"]), "review": rev,
                    "merges": [{"from": m["from"], "into": m["into"]} for m in plan["merges"]],
                    "drops": [{"id": d["entry"]["id"], "reason": d["reason"]}
                              for d in plan["drops"]]}
    else:
        _store(cfg, room_id, room, name, entries, review=review_state,
               log=_with_event(room, event))
    if reviewed is None and auto_review and _due(review_state, len(entries)):
        reviewed = review(cfg, room_id, name, actor="система (переоценка)", source="auto",
                          origin="порог роста записей",
                          why="переоценка после %d записей" % len(entries))
    return _result(cfg, room_id, name,
                   extra={"entry": entry, "duplicate": False, "review": reviewed})


def update_entry(cfg, room_id, profile, entry_id, text, *, why: str = "",
                 actor: str = "") -> dict:
    """Изменить текст записи (id сохраняется — история остаётся связной)."""
    room = _room(cfg, room_id)
    name = _require_member(room, profile)
    item = notes_of(room).get(name) or {}
    entries = entries_of(item)
    target = next((one for one in entries if one["id"] == str(entry_id)), None)
    if target is None:
        raise NotesError("Запись не найдена — возможно, её уже убрали", "not_found",
                         {"field": "entry_id", "entry_id": str(entry_id)})
    body = _clean_text(text)
    if not body:
        raise NotesError("Пустой текст — запись лучше удалить", "bad_request", {"field": "text"})
    before = [dict(one) for one in entries]
    old = target["text"]
    target["text"] = body
    total = sum(len(one["text"]) for one in entries)
    if total > MAX_TEXT:
        raise NotesError("Указания «%s»: вместе %d знаков, а можно не больше %d"
                         % (name, total, MAX_TEXT), "bad_request",
                         {"field": "text", "length": total, "limit": MAX_TEXT})
    event = _event("update", profile=name, entry_id=target["id"], text=body, why=why,
                   actor=actor, before=before, after=entries, note="было: " + old[:200])
    _store(cfg, room_id, room, name, entries, review=_review_of(item),
           log=_with_event(room, event))
    return _result(cfg, room_id, name, extra={"entry": target})


def remove_entry(cfg, room_id, profile, entry_id, *, why: str = "", actor: str = "",
                 force: bool = False) -> dict:
    """Убрать запись. Закреплённую (ручную запись Михаила) — только по его решению."""
    room = _room(cfg, room_id)
    name = _require_member(room, profile)
    item = notes_of(room).get(name) or {}
    entries = entries_of(item)
    target = next((one for one in entries if one["id"] == str(entry_id)), None)
    if target is None:
        raise NotesError("Запись не найдена — возможно, её уже убрали", "not_found",
                         {"field": "entry_id", "entry_id": str(entry_id)})
    if target.get("pinned") and not force:
        raise NotesError("Запись закреплена: её добавил Михаил — руководитель её не удаляет",
                         "conflict", {"field": "entry_id", "entry_id": target["id"],
                                      "reason": "pinned"})
    before = [dict(one) for one in entries]
    left = [one for one in entries if one["id"] != str(entry_id)]
    event = _event("remove", profile=name, entry_id=target["id"], text=target["text"],
                   why=why, actor=actor, before=before, after=left)
    _store(cfg, room_id, room, name, left, review=_review_of(item),
           log=_with_event(room, event))
    return _result(cfg, room_id, name, cleared=not left, extra={"removed": target})


def set_pinned(cfg, room_id, profile, entry_id, pinned: bool = True, *, actor: str = "") -> dict:
    """Закрепить/снять закрепление записи (закреплённые переоценка не трогает)."""
    room = _room(cfg, room_id)
    name = _require_member(room, profile)
    item = notes_of(room).get(name) or {}
    entries = entries_of(item)
    target = next((one for one in entries if one["id"] == str(entry_id)), None)
    if target is None:
        raise NotesError("Запись не найдена", "not_found", {"field": "entry_id"})
    before = [dict(one) for one in entries]
    target["pinned"] = bool(pinned)
    event = _event("pinned" if pinned else "unpin", profile=name, entry_id=target["id"],
                   text=target["text"], actor=actor, source="user",
                   before=before, after=entries)
    _store(cfg, room_id, room, name, entries, review=_review_of(item),
           log=_with_event(room, event))
    return _result(cfg, room_id, name, extra={"entry": target})


def set(cfg, room_id, profile, text, *, why: str = "", actor: str = "") -> dict:  # noqa: A001
    """Записать (или очистить) ПОЛНЫЙ текст указаний участника (старый PUT).

    Ручной ввод Михаила: текст становится одной закреплённой записью («Михаил»),
    прежние записи заменяются — но событие журнала помнит, что было, и откат возможен.
    """
    room = _room(cfg, room_id)
    name = _require_member(room, profile)
    item = notes_of(room).get(name) or {}
    before = entries_of(item)
    body = _clean_text(text)
    if "\x00" in str(text or ""):
        raise NotesError("В указаниях есть непечатаемый символ", "bad_request", {"field": "text"})
    if len(body) > MAX_TEXT:
        raise NotesError("Указания: %d знаков, а можно не больше %d. Сократите текст."
                         % (len(body), MAX_TEXT), "bad_request",
                         {"field": "text", "length": len(body), "limit": MAX_TEXT})
    entries = [] if not body else [{"id": new_id(body, salt=name + "|user"), "text": body,
                                    "source": "user", "origin": "ручная запись",
                                    "created_at": _now(), "used_count": 0, "pinned": True}]
    event = _event("replace", profile=name, entry_id=(entries[0]["id"] if entries else ""),
                   text=body, why=why or "правка Михаила", origin="ручная запись",
                   actor=actor or "Михаил", source="user", before=before, after=entries)
    _store(cfg, room_id, room, name, entries, review=_review_of(item),
           log=_with_event(room, event))
    return _result(cfg, room_id, name, cleared=not entries)


# ---------------------------------------------------------------- переоценка (рост)
def _plan_review(entries: list, *, now: int) -> dict:
    """Что переоценка сделает: слить дубли, убрать устаревшее неиспользованное, сократить."""
    merges: list[dict] = []          # {from, into, text}
    drops: list[dict] = []           # {entry, reason}
    work = [dict(one) for one in entries]
    keep: list[dict] = []
    by_key: dict = {}
    for entry in work:
        key = _norm_key(entry["text"])
        twin = by_key.get(key)
        if twin is None:
            by_key[key] = entry
            keep.append(entry)
            continue
        # дубль: остаётся закреплённая (или первая), счётчик использования суммируем
        if entry.get("pinned") and not twin.get("pinned"):
            keep[keep.index(twin)] = entry
            by_key[key] = entry
            twin, entry = entry, twin
        twin["used_count"] = int(twin.get("used_count") or 0) + int(entry.get("used_count") or 0)
        twin["pinned"] = bool(twin.get("pinned") or entry.get("pinned"))
        merges.append({"from": entry["id"], "into": twin["id"], "text": entry["text"]})
    stale_after = max(0, int(now)) - STALE_DAYS * 86400
    survivors: list[dict] = []
    for entry in keep:
        created = int(entry.get("created_at") or 0)
        if (not entry.get("pinned") and not int(entry.get("used_count") or 0)
                and created and created < stale_after):
            drops.append({"entry": entry, "reason": "не использовалось %d дней" % STALE_DAYS})
            continue
        survivors.append(entry)
    # превышение лимита: сначала неиспользованные, потом самые старые (закреплённые не трогаем)
    while len(survivors) > MAX_ENTRIES or sum(len(x["text"]) for x in survivors) > MAX_TEXT:
        candidates = [x for x in survivors if not x.get("pinned")]
        if not candidates:
            break
        unused = [x for x in candidates if not int(x.get("used_count") or 0)]
        pool = unused or candidates
        victim = sorted(pool, key=lambda x: int(x.get("created_at") or 0))[0]
        drops.append({"entry": victim,
                      "reason": "лимит: %d записей / %d знаков" % (MAX_ENTRIES, MAX_TEXT)})
        survivors = [x for x in survivors if x["id"] != victim["id"]]
    return {"merges": merges, "drops": drops, "entries": survivors}


def review(cfg, room_id, profile, *, apply: bool = True, actor: str = "",
           source: str = "auto", origin: str = "", why: str = "",
           now: int | None = None) -> dict:
    """Переоценка записей: дубли слить, устаревшее неиспользованное убрать, сократить.

    `apply=False` — только показать, что уберётся (предпросмотр для «Сократить»).
    Закреплённые записи Михаила не трогаются никогда. Каждое действие — событие журнала.
    """
    room = _room(cfg, room_id)
    name = _require_member(room, profile)
    item = notes_of(room).get(name) or {}
    entries = entries_of(item)
    stamp = int(now if now is not None else _now())
    plan = _plan_review(entries, now=stamp)
    report = {"merges": [{"from": m["from"], "into": m["into"],
                          "text": mask_secrets(m["text"])[:200]} for m in plan["merges"]],
              "drops": [{"id": d["entry"]["id"], "text": mask_secrets(d["entry"]["text"])[:200],
                         "reason": d["reason"]} for d in plan["drops"]],
              "before": len(entries), "after": len(plan["entries"]),
              "chars_before": sum(len(one["text"]) for one in entries),
              "chars_after": sum(len(one["text"]) for one in plan["entries"])}
    if not apply:
        return dict(report, applied=False, profile=name)
    if not plan["merges"] and not plan["drops"]:
        # переоценивать нечего — только отметка «проверено», чтобы порог не срабатывал вечно
        rev = {"count_at_review": len(entries), "reviewed_at": stamp, "due": False}
        _store(cfg, room_id, room, name, entries, review=rev)
        return dict(report, applied=False, profile=name, review=rev)
    log = _log_of(room)
    for merge in plan["merges"]:
        log = [(_event("merge", profile=name, entry_id=merge["from"], text=merge["text"],
                       why=why or "переоценка: дубль слит", origin=origin, actor=actor,
                       source=source, note="вошло в " + merge["into"],
                       before=entries, after=plan["entries"]))] + log
    for drop in plan["drops"]:
        log = [(_event("remove", profile=name, entry_id=drop["entry"]["id"],
                       text=drop["entry"]["text"], why=why or "переоценка: " + drop["reason"],
                       origin=origin, actor=actor, source=source,
                       before=entries, after=plan["entries"]))] + log
    rev = {"count_at_review": len(plan["entries"]), "reviewed_at": stamp, "due": False}
    summary = _event("review", profile=name, why=why or "переоценка записей",
                     origin=origin, actor=actor, source=source,
                     note="записей %d → %d, слито %d, убрано %d"
                          % (len(entries), len(plan["entries"]), len(plan["merges"]),
                             len(plan["drops"])),
                     before=entries, after=plan["entries"])
    log = ([summary] + log)[:MAX_LOG]
    _store(cfg, room_id, room, name, plan["entries"], review=rev, log=log)
    return dict(report, applied=True, profile=name, review=rev)


def touch_used(cfg, room_id, profile, entry_ids=None) -> int:
    """Отметить, что записи пригодились (счётчик `used_count`), без журнала и правок текста."""
    try:
        room = _rooms().get_room(cfg, str(room_id)) or {}
        name = _profile(profile)
        notes = notes_of(room)
        item = notes.get(name)
        if not item:
            return 0
        entries = entries_of(item)
        wanted = {str(x) for x in entry_ids} if entry_ids else None
        touched = 0
        for entry in entries:
            if wanted is not None and entry["id"] not in wanted:
                continue
            entry["used_count"] = int(entry.get("used_count") or 0) + 1
            touched += 1
        if not touched:
            return 0
        notes[name] = _item(entries, updated_at=int(item.get("updated_at") or 0),
                            review=_review_of(item))
        _write(cfg, str(room_id), notes)
        return touched
    except Exception:  # noqa: BLE001 - счётчик не должен ронять проход watcher
        return 0


def rollback(cfg, room_id, event_id, *, actor: str = "", why: str = "") -> dict:
    """Откатить конкретное изменение из журнала (вернуть прежние записи участника).

    Закреплённые записи, появившиеся после события, сохраняются: их не удаляем.
    Повторный откат того же события — 409 (уже откачено).
    """
    room = _room(cfg, room_id)
    log = _log_of(room)
    event = next((one for one in log if str(one.get("id")) == str(event_id)), None)
    if event is None:
        raise NotesError("Изменение не найдено в журнале", "not_found",
                         {"field": "event_id", "event_id": str(event_id)})
    if event.get("rolled_back"):
        raise NotesError("Это изменение уже откачено", "conflict", {"event_id": str(event_id)})
    target = _profile(event.get("profile"))
    if not target:
        raise NotesError("В событии нет участника — откатывать нечего", "bad_request",
                         {"event_id": str(event_id)})
    if target not in [p for p, _r in participants(room)]:
        raise NotesError("Участника «%s» больше нет в комнате" % target, "conflict",
                         {"profile": target})
    item = notes_of(room).get(target) or {}
    current = entries_of(item)
    restore = [dict(one) for one in (event.get("before") or [])]
    known = {one["id"] for one in restore}
    for entry in current:                       # закреплённое после события не теряем
        if entry.get("pinned") and entry["id"] not in known:
            restore.append(entry)
            known.add(entry["id"])
    event = dict(event, rolled_back=True, rolled_back_at=_now())
    log = [event if str(one.get("id")) == str(event_id) else one for one in log]
    back = _event("rollback", profile=target, entry_id=str(event.get("entry_id") or ""),
                  text=str(event.get("text") or ""), why=why or "откат изменения",
                  actor=actor or "Михаил", source="user", note="откат " + str(event_id),
                  before=current, after=restore,
                  extra={"rolls_back": str(event_id)})
    log = ([back] + log)[:MAX_LOG]
    _store(cfg, room_id, room, target, restore, review=_review_of(item), log=log)
    return _result(cfg, room_id, target, extra={"rolled_back": str(event_id),
                                                "restored": len(restore)})


# ---------------------------------------------------------------- раздел для карточки
def _vault_hint(cfg, room_id) -> str:
    """Путь к полному тексту в Obsidian (для пометки об усечении)."""
    try:
        from . import config as config_mod
        folder = Path(config_mod.room_vault_dir(cfg, room_id))
    except Exception:  # noqa: BLE001 - подсказка не должна ронять блок
        try:
            folder = Path(cfg.vault_dir) / str(room_id)
        except Exception:  # noqa: BLE001
            return ""
    return str(folder / "Указания участникам.md")


def render_block(cfg, room, limit: int = SECTION_LIMIT) -> str:
    """Раздел «Особые указания участникам этой комнаты» (≤ limit знаков) или ''.

    Пусто — если указаний нет вовсе (тогда и раздел в карточке не появляется).
    Записи одного участника идут одной строкой через «; ». Длинный текст обрезается
    «…» с пометкой, где лежит полный (Vault).
    """
    room = room if isinstance(room, dict) else {}
    notes = notes_of(room)
    if not notes:
        return ""
    try:
        limit = max(400, int(limit))
    except (TypeError, ValueError):
        limit = SECTION_LIMIT
    room_id = str(room.get("id") or "")
    hint = _vault_hint(cfg, room_id) if room_id else ""
    tail = RULE
    budget = max(200, limit - len(tail) - 2)
    lines = [TITLE + ":"]
    used = len(lines[0]) + 1
    cut = False
    for profile, role in participants(room):
        item = notes.get(profile)
        if not item:
            continue
        body = "; ".join(_one_line(mask_secrets(one["text"])) for one in entries_of(item))
        if not body:
            continue
        entry = "- %s (%s): %s" % (role, profile, body)
        if len(entry) > MAX_SECTION_ENTRY:
            entry = entry[: MAX_SECTION_ENTRY - 1].rstrip() + "…"
            cut = True
        if used + len(entry) + 1 > budget:
            # Место кончилось: не теряем участника целиком — оставляем начало текста.
            available = budget - used - 1
            cut = True
            if available >= 120:
                entry = entry[: available - 1].rstrip() + "…"
            else:
                continue
        lines.append(entry)
        used += len(entry) + 1
    if cut:
        note = "…полный текст — в Vault"
        if hint:
            note += ": %s" % hint
        if used + len(note) + 2 <= budget:
            lines.append(note)
    lines.append("")
    lines.append(tail)
    text = "\n".join(lines)
    if len(text) > limit:                     # страховка: правило не теряем
        keep = max(0, limit - len(tail) - 2)
        text = text[:keep].rstrip() + "\n" + tail
    return text


# ---------------------------------------------------------------- автокомментарии
def comment_hash(profile, text) -> str:
    """hash8 указания: маркер меняется вместе с текстом (идемпотентность)."""
    raw = "%s|%s" % (_profile(profile), _one_line(text))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def comment_text(profile, text, *, updated: bool = False) -> str:
    """Одна строка комментария: `[Указание комнаты · hash8] <текст>` (секреты маскируются)."""
    marker = "%s%s]" % (MARKER_PREFIX, comment_hash(profile, text))
    body = _one_line(mask_secrets(text))
    if updated:
        return "%s %s %s" % (marker, UPDATED_MARK, body)
    return "%s %s" % (marker, body)


def _board_of(cfg, room) -> str:
    return str((room or {}).get("board") or getattr(cfg, "board", "") or "team")


def _connect(cfg, board: str):
    """Чтение доски: sqlite mode=ro (писать в БД приложение не имеет права)."""
    try:
        db = Path(cfg.kanban_db(board))
    except Exception:  # noqa: BLE001 - доски нет/путь не тот
        return None
    if not db.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=3)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql: str, params) -> list:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _tree_ids(cfg, board: str, root_id: str) -> set:
    try:
        from .engine_write_guard import tree_ids
    except ImportError:  # pragma: no cover
        from engine_write_guard import tree_ids  # type: ignore
    try:
        return _new_set(tree_ids(cfg, board, root_id))
    except Exception:  # noqa: BLE001 - чужая утилита не должна ронять проход
        return {str(root_id)}


def _cards(con, ids: list) -> dict:
    """`{id: {assignee, status}}` по карточкам дерева запроса."""
    if not ids:
        return {}
    rows = _rows(con, "SELECT id, assignee, status FROM tasks WHERE id IN (%s)" % _marks(ids), ids)
    return {str(r["id"]): {"assignee": str(r["assignee"] or ""), "status": str(r["status"] or "")}
            for r in rows}


def _markers(con, ids: list) -> dict:
    """`{task_id: set(hash8)}` — какие указания уже стоят комментариями."""
    if not ids:
        return {}
    rows = _rows(con, "SELECT task_id, body FROM task_comments WHERE task_id IN (%s) "
                      "AND body LIKE ?" % _marks(ids), list(ids) + [MARKER_PREFIX + "%"])
    out: dict[str, set] = {}
    for row in rows:
        found = _new_set(_MARKER_RE.findall(str(row["body"] or "")))
        if found:
            out.setdefault(str(row["task_id"]), _new_set()).update(found)
    return out


def ensure_comments(cfg, room, root_id, *, limit: int = MAX_CARDS_PER_RUN) -> dict:
    """Дописать указания комментариями на карточки дерева запроса (идемпотентно).

    Одна строка на карточку, лениво: только карточки дерева ЭТОГО запроса, чей
    исполнитель есть в составе с указаниями; уже стоящий маркер с тем же hash8
    повторно не пишется; при изменившемся тексте — новый комментарий «(обновлено)».
    После записи счётчик `used_count` записей участника растёт (записи пригодились).
    Ошибки собираются в `errors` и наружу не бросаются.
    """
    result = {"written": [], "skipped": 0, "errors": []}
    room = room if isinstance(room, dict) else {}
    notes = notes_of(room)
    if not notes:
        return result
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return result
    root = str(root_id or "")
    if not root:
        return result
    board = _board_of(cfg, room)
    con = _connect(cfg, board)
    if con is None:
        return result
    try:
        ids = sorted(_tree_ids(cfg, board, root))
        cards = _cards(con, ids)
        markers = _markers(con, ids)
    finally:
        con.close()
    try:
        try:
            from . import engine_write as ew
        except ImportError:  # pragma: no cover
            import engine_write as ew  # type: ignore
    except Exception as exc:  # noqa: BLE001 - модуль записи может быть не готов
        result["errors"].append(str(exc)[:200])
        return result
    written = 0
    used: dict = {}
    for card_id, card in sorted(cards.items()):
        if written >= max(1, int(limit)):
            break
        if card.get("status") == "archived":
            continue
        profile = _profile(card.get("assignee"))
        item = notes.get(profile)
        if not profile or not item:
            continue
        text = _one_line(item.get("text"))
        digest = comment_hash(profile, text)
        seen = markers.get(card_id) or _new_set()
        if digest in seen:
            result["skipped"] += 1
            continue
        body = comment_text(profile, text, updated=bool(seen))
        try:
            ew.act(cfg, board, root, "comment", text=body, task_id=card_id, author=profile)
        except Exception as exc:  # noqa: BLE001 - один комментарий не валит проход
            result["errors"].append("%s: %s" % (card_id, str(exc)[:160]))
            continue
        result["written"].append(card_id)
        written += 1
        used.setdefault(profile, []).extend(one["id"] for one in entries_of(item))
    for profile, ids_used in used.items():       # «сколько раз пригодилась» (пункт 13)
        touch_used(cfg, str(room.get("id") or ""), profile, ids_used)
    return result


__all__ = ["MAX_TEXT", "MAX_ENTRIES", "REVIEW_EVERY", "STALE_DAYS", "MAX_ENTRY_HINT", "MAX_LOG",
           "SECTION_LIMIT", "MARKER_PREFIX", "TITLE", "RULE", "ROLE_RU", "SECRET_MASK",
           "SOURCES", "SOURCE_RU", "ACTION_RU", "limits",
           "NotesError", "validate", "normalize", "notes_of", "entries_of", "entry_text",
           "participants", "role_ru", "secret_warnings", "mask_secrets", "new_id",
           "get", "set", "add", "update_entry", "remove_entry", "set_pinned",
           "review", "touch_used", "journal", "rollback",
           "render_block", "comment_hash", "comment_text", "ensure_comments"]
