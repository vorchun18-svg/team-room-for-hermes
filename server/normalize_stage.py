"""Нормализация этапов проекта (`stage`): новый формат и legacy-словарь «область → статус».

Часть `server/normalize.py`; зависит только от `normalize_base`.
"""
from __future__ import annotations

import re
from typing import Any

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .normalize_base import PHASES, PHASE_IDS, PHASE_TITLES, _text
except ImportError:  # pragma: no cover
    from normalize_base import PHASES, PHASE_IDS, PHASE_TITLES, _text  # type: ignore

# ---------------------------------------------------------------- статусы/фазы

_BLOCKED = (
    "blocked", "заблок", "блок", "ждёт", "ждет", "ждут", "ждём", "ожида",
    "waiting", "awaiting", "on hold", "needs you", "needs decision",
)
_BLOCKED_DECISION = ("жд", "ожида", "треб", "нужн", "pending", "await", "wait")
_NEXT = (
    "не начат", "не запущ", "не готов", "не заверш", "не утвержд",
    "not started", "not begun", "planned", "запланир", "next", "todo",
    "pending", "backlog", "в планах", "в очереди",
)
_ACTIVE = (
    "в работе", "в процессе", "идёт", "идет", "активн", "active",
    "in progress", "in_progress", "progress", "running", "doing", "начат",
)
_DONE = (
    "done", "готов", "заверш", "закрыт", "утвержд", "выполн",
    "completed", "complete", "finished", "shipped", "принят",
)

PHASE_ALIASES: dict[str, tuple[str, ...]] = {
    "idea": ("idea", "vision", "идея", "идеи", "концепция", "стратегия", "strategy"),
    "legal": ("legal", "law", "юр", "легал", "юрид", "закон"),
    "brand": ("brand", "роспатент", "trademark", "бренд", "торговая марка", "нейминг"),
    "packaging": ("packaging", "package", "pack", "упаковка", "упак", "этикетк"),
    "production": ("production", "ops", "производство", "операции", "кухня", "manufactur"),
    "sales": ("sales", "vendas", "продажи", "продаж", "сбыт", "каналы"),
}


def canonical_phase_id(area: Any) -> str | None:
    """«область» → id канонической фазы (точное совпадение приоритетнее алиаса)."""
    key = _text(area).lower()
    if not key:
        return None
    if key in PHASE_IDS:
        return key
    slug = re.sub(r"[^a-zа-я0-9]+", "-", key).strip("-")
    if slug in PHASE_IDS:
        return slug
    for pid, aliases in PHASE_ALIASES.items():
        for alias in aliases:
            if alias in key:
                return pid
    return None


def _status_of(text: str) -> str | None:
    """Статус фазы по тексту значения. Порядок важен: «в работе, но ждёт» → blocked."""
    low = " " + (text or "").lower() + " "
    if any(k in low for k in _BLOCKED):
        return "blocked"
    if "решен" in low and any(k in low for k in _BLOCKED_DECISION):
        return "blocked"
    if any(k in low for k in _NEXT):
        return "next"
    if any(k in low for k in _ACTIVE):
        return "active"
    if any(k in low for k in _DONE):
        return "done"
    return None


def _legacy_status(text: str) -> str:
    """Статус по строке legacy-словаря: сначала ведущая фраза, затем весь текст.

    «в работе: исследование готово» — это active (ведущая фраза), а
    «в работе → ждёт решения Михаила» — blocked (ждёт в первых 40 знаках).
    """
    raw = _text(text)
    head = _status_of(raw[:40])
    if head:
        return head
    tail = _status_of(raw)
    return tail or "next"


def _phase_status(value: Any) -> str:
    """Нормализация статуса уже канонической фазы (терпимо к синонимам)."""
    raw = _text(value).lower()
    if raw in ("done", "active", "next", "blocked"):
        return raw
    return _status_of(raw) or "next"


def canonical_stage(stage: Any) -> dict:
    """`stage` любого вида → `{current, phases[6]}` (API §3.4)."""
    provided: list[dict] = []
    unmapped: dict = {}
    current_raw: Any = None

    if isinstance(stage, list):
        provided = [p for p in stage if isinstance(p, dict)]
    elif isinstance(stage, dict):
        phases_val = stage.get("phases")
        if isinstance(phases_val, list):
            provided = [p for p in phases_val if isinstance(p, dict)]
        current_raw = stage.get("current")
        # legacy-словарь «область → статус-строка» может лежать рядом с current/phases
        for key, value in stage.items():
            k = key.strip() if isinstance(key, str) else ""
            if not k or k in ("current", "phases"):
                continue
            if isinstance(value, dict):
                merged = dict(value)
                merged.setdefault("id", k)
                provided.append(merged)
                continue
            pid = canonical_phase_id(k)
            note = _text(value)
            if pid is None:
                unmapped[k] = note
                continue
            provided.append({"id": pid, "title": "", "status": _legacy_status(note),
                             "note": note, "_exact": k.strip().lower() in PHASE_IDS})
    elif isinstance(stage, str) and stage.strip():
        # «legal: в работе» — единственная строка, без области; в фазу не превращаем
        pid = canonical_phase_id(stage.split(":", 1)[0])
        if pid:
            note = stage.split(":", 1)[1].strip() if ":" in stage else stage.strip()
            provided.append({"id": pid, "status": _legacy_status(note), "note": note})

    by_id: dict[str, dict] = {}
    extra: list[dict] = []
    for item in provided:
        pid = canonical_phase_id(item.get("id")) or canonical_phase_id(item.get("title"))
        if pid is None:
            title = _text(item.get("title")) or _text(item.get("id"))
            if not title:
                continue
            extra.append({
                "id": re.sub(r"[^a-zа-я0-9]+", "-", title.lower()).strip("-") or "phase",
                "title": title,
                "status": _phase_status(item.get("status")),
                "note": _text(item.get("note")),
            })
            continue
        note = _text(item.get("note"))
        title = _text(item.get("title"))
        exact = bool(item.get("_exact")) or _text(item.get("id")).strip().lower() in PHASE_IDS
        prev = by_id.get(pid)
        # Точное совпадение с id фазы приоритетнее алиаса («production» бьёт «ops»).
        if prev is not None and prev.get("_exact") and not exact:
            continue
        by_id[pid] = {
            "id": pid,
            "title": title or PHASE_TITLES[pid],
            "status": _phase_status(item.get("status")) if item.get("status") is not None
            else (_legacy_status(note) if note else "next"),
            "note": note,
            "_exact": exact,
        }

    phases: list[dict] = []
    for pid, title in PHASES:
        found = by_id.get(pid)
        if found:
            phases.append({"id": pid, "title": found["title"] or title,
                           "status": found["status"], "note": found["note"]})
        else:
            phases.append({"id": pid, "title": title, "status": "next", "note": ""})
    phases.extend(extra)

    current = canonical_phase_id(current_raw) if current_raw is not None else None
    if current is None and by_id:
        current = _derive_current(phases)
    result = {"current": current, "phases": phases}
    if unmapped:
        result["unmapped"] = unmapped
    return result


def _derive_current(phases: list[dict]) -> str | None:
    """API §3.4: первая active|blocked, иначе последняя done, иначе первая next."""
    for p in phases:
        if p["status"] in ("active", "blocked"):
            return p["id"]
    done = [p["id"] for p in phases if p["status"] == "done"]
    if done:
        return done[-1]
    for p in phases:
        if p["status"] == "next":
            return p["id"]
    return None
