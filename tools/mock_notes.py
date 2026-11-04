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


class MockNotes:
    """Особые указания участникам: состояние в памяти мока."""

    def notes_init(self) -> None:
        for room in self.room_details.values():
            room["agent_notes"] = public(room.get("agent_notes"))

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
        return {"room": room_id, "profile": profile, "text": clean, "updated_at":
                (notes.get(profile) or {}).get("updated_at"), "cleared": not clean,
                "agent_notes": dict(notes), "warnings": warnings}
