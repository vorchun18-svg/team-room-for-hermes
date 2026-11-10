# -*- coding: utf-8 -*-
"""«Особые указания для этой комнаты» в мок-сервере (WAVE9 §9, AGENT-NOTES).

Поле комнаты `agent_notes = {profile: {text, updated_at}}` — добавка к SOUL.md
участника, действующая только в этой комнате. Файлы профилей мок не трогает:
состояние живёт в памяти, как остальные демо-данные.

Правила (те же, что у `server/agent_notes.py`):
* профиль обязан быть в составе комнаты (руководитель, участники, критик) — иначе `400` по-русски;
* текст ≤ 8 000 знаков; пустой текст (или одни пробелы) — очистка указания;
* секреты не блокируют сохранение, но возвращаются предупреждением (`warnings`);
* `GET /api/rooms/{id}` отдаёт только непустые указания.
"""
from __future__ import annotations

import hashlib
import re

from mock_core import MAX_AGENT_NOTE, ApiError, now

SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "Ключ вида sk-…"),
    (re.compile(r"(?i)\bpassword\s*[=:]\s*\S+"), "Пароль в тексте (password=…)"),
    (re.compile(r"(?i)\btoken\s*[=:]\s*\S+"), "Токен в тексте (token=…)"),
)


def mask(text: str) -> str:
    """Маска для предупреждения: сам секрет не повторяем."""
    out = text
    for pattern, _label in SECRET_PATTERNS:
        out = pattern.sub("sk-…" if pattern.pattern.startswith("\\bsk-") else "…", out)
    return out


def warnings_for(text: str) -> list:
    """Предупреждения о секретах — не блокировка (WAVE9 §9)."""
    return [{"code": "secret_in_notes", "message": "%s — уберите из указаний, агент не должен видеть ключи"
             % label} for pattern, label in SECRET_PATTERNS if pattern.search(text)]


def roster(room: dict) -> list:
    """Состав комнаты: руководитель, участники, критик — кому можно задать указания."""
    out = []
    for profile in [room.get("orchestrator")] + [m.get("profile") for m in room.get("members") or []]:
        if profile and profile not in out:
            out.append(profile)
    if room.get("critic") and room["critic"] not in out:
        out.append(room["critic"])
    return out


def role_of(room: dict, profile: str) -> str:
    if profile == room.get("orchestrator"):
        return "руководитель комнаты"
    if profile == room.get("critic"):
        return "критик"
    member = next((m for m in room.get("members") or [] if m.get("profile") == profile), None)
    return (member or {}).get("role") or "worker"


def validate(room: dict, profile: str, text) -> tuple:
    """Проверка профиля и текста → (чистый текст, warnings). Ошибки — `400` по-русски."""
    people = roster(room)
    if profile not in people:
        raise ApiError(400, "bad_request",
                       "Профиль «%s» не участвует в комнате «%s» — указания задают только её участникам"
                       % (profile, room.get("title") or room.get("id")),
                       {"field": "profile", "profile": profile, "available": people})
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ApiError(400, "bad_request", "Указания — текст: поле «text» должно быть строкой",
                       {"field": "text", "got": type(text).__name__})
    if "\x00" in text:
        raise ApiError(400, "bad_request", "В указаниях недопустимый символ", {"field": "text"})
    clean = text.strip()
    if len(clean) > MAX_AGENT_NOTE:
        raise ApiError(400, "bad_request",
                       "Указания длиннее %d знаков — сократите до главного" % MAX_AGENT_NOTE,
                       {"field": "text", "length": len(clean), "max": MAX_AGENT_NOTE})
    return clean, warnings_for(clean)


def public(notes) -> dict:
    """Только непустые указания: `{profile: {text, updated_at}}`."""
    out = {}
    for profile, item in (notes or {}).items():
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            out[profile] = {"text": str(item["text"]), "updated_at": int(item.get("updated_at") or 0)}
    return out


def apply_map(room: dict, value) -> list:
    """`POST/PATCH /api/rooms` с `agent_notes` — та же проверка, что у PUT."""
    if value is None:
        return []
    if not isinstance(value, dict):
        raise ApiError(400, "bad_request", "agent_notes — объект «профиль: {text}»",
                       {"field": "agent_notes", "got": type(value).__name__})
    notes, warnings = {}, []
    for profile, item in value.items():
        text = item.get("text") if isinstance(item, dict) else item
        clean, warns = validate(room, str(profile), text)
        warnings.extend(warns)
        if clean:
            notes[str(profile)] = {"text": clean, "updated_at": now()}
    room["agent_notes"] = notes
    return warnings


# ---------------------------------------------------------------- записи (WAVE14 §13)
# Демо держит записи в памяти: список `{id, text, source, origin, created_at,
# used_count, pinned}` на участника + журнал комнаты с откатом. Файлы профилей
# не трогаются, как и в настоящем приложении.

MAX_ENTRIES = 24
MAX_TEXT = 8000
SOURCE_RU = {"user": "Михаил", "leader": "руководитель комнаты", "auto": "автоматически"}


def _entries(room: dict, profile: str) -> list:
    return room.setdefault("agent_notes_entries", {}).setdefault(profile, [])


def _sync(room: dict) -> None:
    """Пересобрать старое поле `agent_notes` (текст) из записей — его читают другие ответы.

    Форма — как у настоящего реестра (`_item`): `{text, updated_at, entries, review}`.
    Без `entries` карточка участника и ссылка «Особенности (N)» в демо показывали 0.
    """
    notes = {}
    for profile, entries in (room.get("agent_notes_entries") or {}).items():
        kept = [dict(one) for one in entries if str(one.get("text") or "").strip()]
        texts = [one["text"] for one in kept]
        if texts:
            notes[profile] = {"text": "\n".join(texts),
                              "updated_at": kept[-1].get("created_at") or now(),
                              "entries": kept,
                              "review": {"count_at_review": len(kept), "reviewed_at": now(),
                                         "due": False}}
    room["agent_notes"] = notes


def _log(room: dict) -> list:
    return room.setdefault("agent_notes_log", [])


def _event(room: dict, action: str, profile: str, text: str, *, why: str = "", origin: str = "",
           source: str = "user", note: str = "", before=None, after=None) -> dict:
    event = {"id": "j_%06d" % (len(_log(room)) + 1), "at": now(), "action": action,
             "profile": profile, "entry_id": "", "text": text, "why": why, "origin": origin,
             "actor": SOURCE_RU.get(source, "Михаил"), "source": source, "note": note,
             "rolled_back": False, "rolls_back": ""}
    if before is not None:
        event["before"] = [dict(one) for one in before]
    if after is not None:
        event["after"] = [dict(one) for one in after]
    _log(room).insert(0, event)
    del _log(room)[200:]
    return event


def _payload(room: dict, profile: str, **extra) -> dict:
    entries = _entries(room, profile)
    out = {"ok": True, "profile": profile, "role": role_of(room, profile),
           "entries": [dict(one) for one in entries],
           "text": "\n".join(one["text"] for one in entries),
           "updated_at": (entries[-1]["created_at"] if entries else 0),
           "review": {"count_at_review": len(entries), "reviewed_at": now(), "due": False},
           "limits": {"max_entries": MAX_ENTRIES, "max_text": MAX_TEXT, "review_every": 5,
                      "stale_days": 30, "entry_hint": 1000},
           "agent_notes": dict(room.get("agent_notes") or {})}
    out.update(extra)
    return out


def check_entry_text(room: dict, profile: str, text) -> str:
    """Проверка новой записи: участник комнаты, непустой текст, общий лимит знаков."""
    if profile not in roster(room):
        raise ApiError(400, "bad_request",
                       "Профиль «%s» не участвует в комнате — особенности задают только её участникам"
                       % profile, {"field": "profile", "profile": profile})
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "bad_request", "Нужен текст записи", {"field": "text"})
    if "\x00" in text:
        raise ApiError(400, "bad_request", "В записи недопустимый символ", {"field": "text"})
    clean = text.strip()
    total = sum(len(one["text"]) for one in _entries(room, profile))
    if total + len(clean) > MAX_TEXT:
        raise ApiError(400, "bad_request",
                       "Указания длиннее %d знаков — сократите или уберите лишнее" % MAX_TEXT,
                       {"field": "text", "limit": MAX_TEXT})
    return clean


class MockNotesEntries:
    """Записи и журнал в демо-режиме (WAVE14 §13): часть `MockNotes`."""

    # --- чтение -----------------------------------------------------------
    def room_notes(self, room_id: str) -> dict:
        room = self.room(room_id)
        rows = []
        for profile in roster(room):
            entries = _entries(room, profile)
            rows.append({"profile": profile, "role": role_of(room, profile),
                         "entries": [dict(one) for one in entries],
                         "text": "\n".join(one["text"] for one in entries),
                         "updated_at": (entries[-1]["created_at"] if entries else 0),
                         "review": {"count_at_review": len(entries), "reviewed_at": now(),
                                    "due": False}})
        return {"ok": True, "room": room_id, "participants": rows,
                "count": sum(len(r["entries"]) for r in rows),
                "limits": {"max_entries": MAX_ENTRIES, "max_text": MAX_TEXT, "review_every": 5,
                           "stale_days": 30, "entry_hint": 1000}}

    def get_notes_entries(self, room_id: str, profile: str) -> dict:
        room = self.room(room_id)
        return _payload(room, profile, journal=self.notes_journal(room_id, 20)["events"])

    def notes_journal(self, room_id: str, limit: int = 50) -> dict:
        room = self.room(room_id)
        events = [dict(one) for one in _log(room)[:max(1, min(200, int(limit or 50)))]]
        for event in events:
            event["action_ru"] = {"add": "добавлено", "update": "изменено", "remove": "удалено",
                                  "merge": "объединено", "review": "переоценка",
                                  "replace": "заменено", "rollback": "откат"}.get(
                                      event["action"], "")
            event["source_ru"] = SOURCE_RU.get(event["source"], "Михаил")
        return {"ok": True, "room": room_id, "events": events, "count": len(events)}

    # --- запись -----------------------------------------------------------
    def post_note_entry(self, room_id: str, profile: str, body: dict) -> dict:
        room = self.room(room_id)
        text = check_entry_text(room, profile, (body or {}).get("text"))
        source = str((body or {}).get("source") or "user")
        if source not in SOURCE_RU:
            source = "user"
        entries = _entries(room, profile)
        for one in entries:
            if one["text"].strip().lower() == text.lower():
                return _payload(room, profile, duplicate=True, entry=dict(one))
        raw = "%s|%s|%s" % (profile, text, now())
        entry = {"id": "n_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10],
                 "text": text, "source": source,
                 "origin": str((body or {}).get("origin") or ""), "created_at": now(),
                 "used_count": 0, "pinned": source == "user"}
        before = [dict(one) for one in entries]
        entries.append(entry)
        _event(room, "add", profile, text, why=str((body or {}).get("why") or ""),
               origin=entry["origin"], source=entry["source"], before=before, after=entries)
        while len(entries) > MAX_ENTRIES:
            victim = next((one for one in entries if not one["pinned"]), None)
            if victim is None:
                break
            entries.remove(victim)
            _event(room, "review", profile, victim["text"], why="лимит записей",
                   source="auto", note="убрано: лимит %d записей" % MAX_ENTRIES)
        _sync(room)
        return _payload(room, profile, entry=entry, duplicate=False, review=None)

    def patch_note_entry(self, room_id: str, profile: str, entry_id: str, body: dict) -> dict:
        room = self.room(room_id)
        text = check_entry_text(room, profile, (body or {}).get("text"))
        entries = _entries(room, profile)
        target = next((one for one in entries if one["id"] == entry_id), None)
        if target is None:
            raise ApiError(404, "not_found", "Запись не найдена", {"entry_id": entry_id})
        before = [dict(one) for one in entries]
        target["text"] = text
        _event(room, "update", profile, text, why=str((body or {}).get("why") or ""),
               source="user", before=before, after=entries)
        _sync(room)
        return _payload(room, profile, entry=target)

    def delete_note_entry(self, room_id: str, profile: str, entry_id: str, body: dict) -> dict:
        room = self.room(room_id)
        entries = _entries(room, profile)
        target = next((one for one in entries if one["id"] == entry_id), None)
        if target is None:
            raise ApiError(404, "not_found", "Запись не найдена", {"entry_id": entry_id})
        if target.get("pinned") and not (body or {}).get("force"):
            raise ApiError(409, "conflict", "Запись закреплена — её может убрать только Михаил",
                           {"entry_id": entry_id, "reason": "pinned"})
        before = [dict(one) for one in entries]
        entries.remove(target)
        _event(room, "remove", profile, target["text"], why=str((body or {}).get("why") or ""),
               source="user", before=before, after=entries)
        _sync(room)
        return _payload(room, profile, removed=target, cleared=not entries)

    def pin_note_entry(self, room_id: str, profile: str, entry_id: str, body: dict) -> dict:
        """Закрепить/снять закрепление записи (WAVE17): как `agent_notes.set_pinned`."""
        room = self.room(room_id)
        entries = _entries(room, profile)
        target = next((one for one in entries if one["id"] == entry_id), None)
        if target is None:
            raise ApiError(404, "not_found", "Запись не найдена", {"entry_id": entry_id})
        pinned = (body or {}).get("pinned")
        if pinned is None:
            raise ApiError(400, "bad_request", "Нужно поле pinned: true или false",
                           {"field": "pinned"})
        if isinstance(pinned, str):
            pinned = pinned.strip().lower() in ("1", "true", "yes", "on")
        before = [dict(one) for one in entries]
        target["pinned"] = bool(pinned)
        _event(room, "pinned" if target["pinned"] else "unpin", profile, target["text"],
               why="", source="user", before=before, after=[dict(one) for one in entries])
        _sync(room)
        return _payload(room, profile, entry=dict(target))

    def review_notes(self, room_id: str, profile: str, body: dict) -> dict:
        room = self.room(room_id)
        entries = _entries(room, profile)
        seen, dups = {}, []
        for one in entries:
            key = " ".join(one["text"].lower().split())
            if key in seen:
                dups.append(one)
            else:
                seen[key] = one
        drops = dups
        preview = {"ok": True, "profile": profile, "before": len(entries),
                   "after": len(entries) - len(drops),
                   "merges": [{"from": one["id"], "into": "", "text": one["text"]} for one in dups],
                   "drops": [{"id": one["id"], "text": one["text"], "reason": "дубль"}
                             for one in drops],
                   "chars_before": sum(len(one["text"]) for one in entries),
                   "chars_after": sum(len(one["text"]) for one in entries if one not in drops)}
        if not (body or {}).get("apply"):
            return dict(preview, applied=False)
        before = [dict(one) for one in entries]
        for one in drops:
            entries.remove(one)
            _event(room, "merge", profile, one["text"], why="переоценка: дубль",
                   source="user", before=before, after=entries)
        if drops:
            _event(room, "review", profile, "", why="переоценка записей", source="user",
                   note="записей %d → %d" % (len(before), len(entries)),
                   before=before, after=entries)
        _sync(room)
        return dict(preview, applied=bool(drops), entries=[dict(one) for one in entries],
                    journal=self.notes_journal(room_id, 20)["events"])

    def rollback_note_event(self, room_id: str, event_id: str, body: dict) -> dict:
        room = self.room(room_id)
        event = next((one for one in _log(room) if one["id"] == event_id), None)
        if event is None:
            raise ApiError(404, "not_found", "Изменение не найдено в журнале",
                           {"event_id": event_id})
        if event.get("rolled_back"):
            raise ApiError(409, "conflict", "Это изменение уже откачено",
                           {"event_id": event_id})
        profile = event.get("profile") or ""
        entries = _entries(room, profile)
        restore = [dict(one) for one in (event.get("before") or [])]
        known = {one["id"] for one in restore}
        for one in entries:
            if one.get("pinned") and one["id"] not in known:
                restore.append(one)
        event["rolled_back"] = True
        _event(room, "rollback", profile, event.get("text") or "", why="откат изменения",
               source="user", note="откат " + event_id, before=entries, after=restore)
        room.setdefault("agent_notes_entries", {})[profile] = restore
        _sync(room)
        return _payload(room, profile, rolled_back=event_id, restored=len(restore))


class MockNotes(MockNotesEntries):
    """Особые указания участникам: состояние в памяти мока."""

    def notes_init(self) -> None:
        for room in self.room_details.values():
            # WAVE14 §13: записи из фикстуры (если есть) — в структуру «список записей».
            seeded = {}
            for profile, item in (room.get("agent_notes") or {}).items():
                raw = item.get("entries") if isinstance(item, dict) else None
                if isinstance(raw, list) and raw:
                    seeded[profile] = [dict(one) for one in raw
                                       if isinstance(one, dict) and str(one.get("text") or "").strip()]
            room["agent_notes_entries"] = seeded
            room.setdefault("agent_notes_log", [])
            room["agent_notes"] = public(room.get("agent_notes"))
            _sync(room)

    def put_notes(self, room_id: str, profile: str, body: dict) -> dict:
        room = self.room(room_id)
        if "text" not in body:
            raise ApiError(400, "bad_request", "Нужно поле «text» — текст указаний (пусто — очистить)",
                           {"field": "text"})
        clean, warnings = validate(room, profile, body.get("text"))
        notes = public(room.get("agent_notes"))
        if clean:
            notes[profile] = {"text": clean, "updated_at": now()}
        else:
            notes.pop(profile, None)
        room["agent_notes"] = notes
        # WAVE14 §13: полный текст — это одна закреплённая запись (структура остаётся целой).
        before = [dict(one) for one in _entries(room, profile)]
        room.setdefault("agent_notes_entries", {})[profile] = (
            [] if not clean else [{"id": "n_%s" % hashlib.sha1(
                                       ("%s|%s|%s" % (profile, clean, now())).encode("utf-8")
                                   ).hexdigest()[:10],
                                   "text": clean, "source": "user", "origin": "ручная запись",
                                   "created_at": now(), "used_count": 0, "pinned": True}])
        _event(room, "replace", profile, clean, why="правка Михаила", origin="ручная запись",
               source="user", before=before, after=_entries(room, profile))
        return {"room": room_id, "profile": profile, "text": clean, "updated_at":
                (notes.get(profile) or {}).get("updated_at"), "cleared": not clean,
                "agent_notes": dict(notes), "warnings": warnings,
                "entries": [dict(one) for one in _entries(room, profile)]}
