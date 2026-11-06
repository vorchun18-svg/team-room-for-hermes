"""Этапы проекта — свойство КОМНАТЫ, а не список в коде (WAVE17 §R, B-8).

Раньше «Карта проекта» показывала всем комнатам один и тот же список из шести
этапов (константа `PHASES`), потому что он был зашит в трёх местах. Теперь у
комнаты есть необязательное поле `phases: [{id, title}]`:

* новая комната и комната без этапов — полосы этапов нет (никаких заглушек);
* этапы добавляет, переименовывает, переставляет и удаляет сам пользователь в
  настройках комнаты;
* переименование и порядок не ломают привязку данных: всё связывается по `id`.

Модуль держит три вещи в одном месте, чтобы не было второго мнения о этапах:

1. `check` / `norm` — запись и чтение списка этапов комнаты;
2. `stage` / `current` — «этап × статус из metadata ответа» для страницы запроса;
3. `migrate` — миграция ЧТЕНИЕМ: у комнат, которые уже вели карту (в
   `state/<комната>/roadmap.json` есть реальные статусы/заметки), этапы
   подхватываются из их собственного файла. Данные не переписываются, у
   комнаты без следов карты этапов не появляется.

Ни одного названия этапа какого-либо проекта в коде нет: заголовки приходят
только из данных комнаты (или из её файла карты при миграции).
"""
from __future__ import annotations

import re

try:  # пакетный и «плоский» импорт (python server/app.py)
    from . import statefiles as sf
except ImportError:  # pragma: no cover
    import statefiles as sf  # type: ignore

MAX_PHASES = 24
MAX_TITLE = 80
MAX_ID = 40
PHASE_STATUSES = ("done", "active", "next", "blocked")
# Статусы, которые считаются «в этой комнате карту действительно вели»:
# миграция чтением не должна принимать за данные пустую заготовку.
REAL_STATUSES = ("done", "active", "blocked")

_ID_RE = re.compile(r"[^a-zа-я0-9_-]+")


class PhaseError(Exception):
    """Ошибка списка этапов: сообщение по-русски для 400 от роута."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


def _slug(text) -> str:
    """Заголовок → стабильный id этапа (латиница/кириллица, цифры, `-`, `_`)."""
    raw = str(text or "").strip().lower()
    slug = _ID_RE.sub("-", raw).strip("-_")
    return slug[:MAX_ID].strip("-_") or "phase"


def _title(value) -> str:
    return " ".join(str(value or "").split())[:MAX_TITLE]


def norm(value) -> list[dict]:
    """Прочитать список этапов терпимо: мусор не роняет карту комнаты.

    Принимается список `{id?, title}`; id выводится из заголовка, если его нет.
    Повторы id и пустые заголовки отбрасываются — порядок сохраняется.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = _title(item.get("title") or item.get("name"))
        pid = _slug(item.get("id")) if str(item.get("id") or "").strip() else _slug(title)
        if not title or not pid or pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "title": title})
        if len(out) >= MAX_PHASES:
            break
    return out


def check(value) -> list[dict]:
    """Проверить список этапов для записи (PATCH комнаты).

    Отличия от `norm`: пустой список допустим (значит «этапов нет»), а всё
    сомнительное — ошибка с понятным текстом, а не молчаливая потеря.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise PhaseError("Этапы проекта: нужен список этапов", {"field": "phases"})
    if len(value) > MAX_PHASES:
        raise PhaseError("Этапы проекта: не больше %d этапов" % MAX_PHASES,
                         {"field": "phases", "max": MAX_PHASES})
    out: list[dict] = []
    seen: set[str] = set()
    for pos, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            raise PhaseError("Этап %d: нужен объект с заголовком" % pos,
                             {"field": "phases", "index": pos})
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            raise PhaseError("Этап %d: нужен заголовок" % pos, {"field": "phases", "index": pos})
        if len(title) > MAX_TITLE:
            raise PhaseError("Этап %d: заголовок не длиннее %d знаков (сейчас %d)"
                             % (pos, MAX_TITLE, len(title)),
                             {"field": "phases", "index": pos, "max": MAX_TITLE})
        pid = _slug(item.get("id")) if str(item.get("id") or "").strip() else _slug(title)
        if pid in seen:
            raise PhaseError("Этап %d: повторяется id «%s»" % (pos, pid),
                             {"field": "phases", "index": pos, "id": pid})
        seen.add(pid)
        out.append({"id": pid, "title": title})
    return out


def ids(phases) -> list[str]:
    return [str(p.get("id")) for p in (phases or []) if isinstance(p, dict) and p.get("id")]


def by_id(phases) -> dict:
    return {str(p["id"]): p for p in (phases or []) if isinstance(p, dict) and p.get("id")}


def _derive_current(phases: list[dict]) -> str | None:
    """Текущий этап: первый active|blocked, иначе последний done, иначе первый next."""
    for phase in phases:
        if phase.get("status") in ("active", "blocked"):
            return phase["id"]
    done = [p["id"] for p in phases if p.get("status") == "done"]
    if done:
        return done[-1]
    for phase in phases:
        if phase.get("status") == "next":
            return phase["id"]
    return None


def statuses_of(canon: dict) -> dict:
    """Статусы этапов из metadata ответа: `{id: {status, note}}` (чужие id не берём)."""
    stage = canon.get("stage") if isinstance(canon, dict) else None
    if not isinstance(stage, dict):
        return {}
    out: dict = {}
    for item in stage.get("phases") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        pid = str(item["id"])
        status = item.get("status") if item.get("status") in PHASE_STATUSES else "next"
        out[pid] = {"status": status, "note": str(item.get("note") or "")}
    return out


def _key(text) -> str:
    """Ключ сравнения названий: регистр, лишние пробелы, ё/е не важны."""
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _meta_index(canon: dict) -> tuple[dict, dict]:
    """Metadata ответа → (по id, по названию). Второй — для старых ключей-названий."""
    by_ident: dict = {}
    by_title: dict = {}
    stage = canon.get("stage") if isinstance(canon, dict) else None
    if not isinstance(stage, dict):
        return by_ident, by_title
    for item in stage.get("phases") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        status = item.get("status") if item.get("status") in PHASE_STATUSES else "next"
        pid = str(item["id"])
        got = {"id": pid, "status": status, "note": str(item.get("note") or "")}
        by_ident[pid] = got
        by_title.setdefault(_key(item.get("title")), got)
        by_title.setdefault(_key(pid), got)
    return by_ident, by_title


def stage(phases, canon: dict | None = None) -> dict:
    """API §3.4: `{current, phases[]}` ТОЛЬКО по этапам комнаты.

    Порядок и заголовки — из комнаты (переименование не ломает связку по id),
    статус и заметка — из metadata ответа. Этапы, которых комната не объявила,
    в ответ не попадают: карта комнаты не растёт сама по себе.

    Связка «этап комнаты ↔ этап из metadata» сначала по `id`, затем по
    названию: старые ответы называли этапы словами, а комната может называть
    их иначе — статус всё равно не теряется.
    """
    declared = norm(phases)
    if not declared:
        return {"current": None, "phases": []}
    by_ident, by_title = _meta_index(canon or {})
    out = []
    source_of: dict = {}
    for item in declared:
        got = by_ident.get(item["id"]) or by_title.get(_key(item["title"])) or {}
        source_of[item["id"]] = got.get("id")
        out.append({"id": item["id"], "title": item["title"],
                    "status": got.get("status") or "next", "note": got.get("note") or ""})
    raw_current = (canon or {}).get("stage")
    current = None
    if isinstance(raw_current, dict):
        raw_current = str(raw_current.get("current") or "")
        if raw_current in by_id(out):
            current = raw_current
        else:
            hit = by_ident.get(raw_current) or by_title.get(_key(raw_current))
            if hit and hit.get("id") in by_id(out):
                current = hit["id"]
            elif hit:
                current = next((pid for pid, src in source_of.items() if src == hit.get("id")), None)
    if current is None:
        current = _derive_current(out)
    return {"current": current, "phases": out}


def _roadmap_path(cfg, room_id: str):
    from pathlib import Path
    return Path(cfg.state_dir) / str(room_id) / "roadmap.json"


def migrate(cfg, room_id: str) -> list[dict]:
    """Миграция ЧТЕНИЕМ: этапы комнаты из её собственного файла карты.

    Берём файл `state/<комната>/roadmap.json` — это то, что комната уже вела
    (его пишет ingest из metadata ответов). Принимаем список только если в нём
    есть настоящие следы работы: статус кроме «next», заметка или запрос-источник.
    Пустая заготовка (шесть строк «впереди», как её когда-то создавала константа)
    данных не даёт: у такой комнаты этапов нет.

    Ничего не записываем: файл комнаты и `rooms.json` остаются как были.
    """
    rid = str(room_id or "").strip()
    if not rid:
        return []
    try:
        data = sf.read_json(_roadmap_path(cfg, rid), None)
    except Exception:  # noqa: BLE001 — миграция вторична, карта не должна падать
        return []
    if not isinstance(data, dict):
        return []
    stored = [p for p in (data.get("phases") or []) if isinstance(p, dict)]
    evidence = any(
        str(p.get("status") or "") in REAL_STATUSES
        or str(p.get("note") or "").strip()
        or str(p.get("source_request") or "").strip()
        for p in stored
    )
    if not evidence:
        return []
    return norm([{"id": p.get("id"), "title": p.get("title")} for p in stored])


def phases_of(cfg, room) -> list[dict]:
    """Этапы комнаты: явное поле `phases` или миграция чтением из её карты."""
    if isinstance(room, dict):
        if isinstance(room.get("phases"), list):
            return norm(room.get("phases"))
        return migrate(cfg, str(room.get("id") or ""))
    if isinstance(room, str) and room:
        return migrate(cfg, room)
    return []


def current_of(cfg, room, phases=None) -> dict | None:
    """Текущий этап комнаты по её файлу карты: `{id,title,status}` или None."""
    rid = str(room.get("id") if isinstance(room, dict) else room or "").strip()
    declared = phases if isinstance(phases, list) else phases_of(cfg, room)
    if not rid or not declared:
        return None
    try:
        data = sf.read_json(_roadmap_path(cfg, rid), None)
    except Exception:  # noqa: BLE001
        data = None
    stored = {str(p.get("id")): p for p in
              ((data.get("phases") or []) if isinstance(data, dict) else []) if isinstance(p, dict)}
    items = []
    for item in declared:
        got = stored.get(item["id"]) or {}
        status = got.get("status") if got.get("status") in PHASE_STATUSES else "next"
        items.append({"id": item["id"], "title": item["title"], "status": status})
    raw_current = data.get("current") if isinstance(data, dict) else None
    if isinstance(raw_current, dict):
        raw_current = raw_current.get("id")
    current_id = str(raw_current or "")
    if current_id not in by_id(items):
        current_id = _derive_current(items) or ""
    found = by_id(items).get(current_id)
    return dict(found) if found else None


__all__ = ["MAX_PHASES", "MAX_TITLE", "PHASE_STATUSES", "PhaseError", "norm", "check", "ids",
           "by_id", "statuses_of", "stage", "migrate", "phases_of", "current_of"]
