"""Нормализация этапов проекта (`stage`): новый формат и legacy-словарь «область → статус».

Часть `server/normalize.py`; зависит только от `normalize_base`.
"""
from __future__ import annotations

from typing import Any

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .normalize_base import PHASE_STATUSES, _text, phase_id
except ImportError:  # pragma: no cover
    from normalize_base import PHASE_STATUSES, _text, phase_id  # type: ignore

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

def canonical_phase_id(area: Any) -> str | None:
    """«область» → id этапа: стабильный слаг из её названия (W17-R B-8).

    Своего словаря этапов в коде нет: id выводится из того, что пришло в
    metadata («legal», «Согласование», «Юридическая часть») или из настроек
    комнаты. Пустое значение — этапа нет.
    """
    key = _text(area)
    if not key:
        return None
    return phase_id(key)


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
    if raw in PHASE_STATUSES:
        return raw
    return _status_of(raw) or "next"


def canonical_stage(stage: Any) -> dict:
    """`stage` любого вида → `{current, phases[]}` (API §3.4).

    W17-R §1 (B-8): этапы берутся ТОЛЬКО из metadata — normalize не дорисовывает
    чужих этапов и не знает названий ни одного проекта. Нет `stage` — пустой
    список; какие этапы показывать, решает комната (`server/phase_book.py`).
    """
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
                merged.setdefault("title", k)
                provided.append(merged)
                continue
            note = _text(value)
            pid = canonical_phase_id(k)
            if pid is None:
                continue
            if not note:
                unmapped[k] = note
                continue
            provided.append({"id": pid, "title": k, "status": _legacy_status(note), "note": note})
    elif isinstance(stage, str) and stage.strip():
        # «legal: в работе» — одна строка, без области
        head, _, tail = stage.partition(":")
        pid = canonical_phase_id(head)
        note = (tail or stage).strip()
        if pid:
            provided.append({"id": pid, "title": head.strip(), "status": _legacy_status(note), "note": note})

    phases: list[dict] = []
    index: dict[str, int] = {}
    for item in provided:
        pid = canonical_phase_id(item.get("id")) or canonical_phase_id(item.get("title"))
        if pid is None:
            continue
        title = _text(item.get("title")) or _text(item.get("id")) or pid
        note = _text(item.get("note"))
        status = (_phase_status(item.get("status")) if item.get("status") is not None
                  else (_legacy_status(note) if note else "next"))
        if pid in index:
            # тот же этап пришёл дважды (id и заголовок) — обновляем, не плодим
            phases[index[pid]] = {"id": pid, "title": title or phases[index[pid]]["title"],
                                  "status": status, "note": note or phases[index[pid]]["note"]}
            continue
        index[pid] = len(phases)
        phases.append({"id": pid, "title": title, "status": status, "note": note})

    current = canonical_phase_id(current_raw) if current_raw is not None else None
    if current not in index:
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
