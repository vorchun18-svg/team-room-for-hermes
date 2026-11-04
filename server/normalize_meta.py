"""Нормализация полей ответа (шаги, вопросы, решения, артефакты, замечания) и markdown-фолбэк.

Часть `server/normalize.py`; зависит только от `normalize_base`.
"""
from __future__ import annotations

import re
from typing import Any

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .normalize_base import STATES, _dedupe_ids, _items, _text, artifact_kind, parse_due
except ImportError:  # pragma: no cover
    from normalize_base import STATES, _dedupe_ids, _items, _text, artifact_kind, parse_due  # type: ignore

# ---------------------------------------------------------------- поля ответа


def _next_steps(raw: Any) -> list[dict]:
    out: list[dict] = []
    for item in _items(raw):
        owner: Any = None
        due: Any = None
        state = "open"
        if isinstance(item, dict):
            text = _text(item.get("text") or item.get("step") or item.get("title") or item.get("what"))
            owner = item.get("owner") or item.get("assignee") or item.get("who")
            due = item.get("due") if item.get("due") is not None else item.get("deadline")
            raw_state = _text(item.get("state")).lower()
            if raw_state in STATES:
                state = raw_state
        elif isinstance(item, str):
            text = _text(item)
        else:
            continue
        if not text:
            continue
        out.append({"text": text, "owner": _text(owner) or None,
                    "due": parse_due(due), "state": state})
    ids = _dedupe_ids("ns", [s["text"] for s in out])
    return [{"id": i, **s} for i, s in zip(ids, out)]


def _open_questions(raw: Any) -> list[dict]:
    out: list[dict] = []
    for item in _items(raw):
        if isinstance(item, dict):
            text = _text(item.get("text") or item.get("question") or item.get("q"))
            target = _text(item.get("for") or item.get("for_") or item.get("to")
                           or item.get("addressee") or item.get("assignee")) or "mikhail"
            answered = bool(item.get("answered"))
        elif isinstance(item, str):
            text, target, answered = _text(item), "mikhail", False
        else:
            continue
        if not text:
            continue
        out.append({"text": text, "for": target, "answered": answered})
    ids = _dedupe_ids("q", [q["text"] for q in out])
    return [{"id": i, **q} for i, q in zip(ids, out)]


def _decisions(raw: Any) -> list[dict]:
    out: list[dict] = []
    for item in _items(raw):
        if isinstance(item, dict):
            text = _text(item.get("text") or item.get("decision") or item.get("title"))
            why = _text(item.get("why") or item.get("reason") or item.get("rationale")) or None
            status = _text(item.get("status")).lower()
            if status in ("accepted", "accept", "approved", "approve", "принято", "утверждено"):
                status = "accepted"
            elif status in ("rejected", "reject", "отклонено"):
                status = "rejected"
            else:
                status = "proposed"
        elif isinstance(item, str):
            text, why, status = _text(item), None, "proposed"
        else:
            continue
        if not text:
            continue
        out.append({"text": text, "why": why, "status": status})
    ids = _dedupe_ids("d", [d["text"] for d in out])
    return [{"id": i, **d} for i, d in zip(ids, out)]


def _future_questions(raw: Any) -> list[str]:
    out: list[str] = []
    for item in _items(raw):
        if isinstance(item, dict):
            text = _text(item.get("text"))
        elif isinstance(item, str):
            text = _text(item)
        else:
            continue
        if text:
            out.append(text)
    return out


def _artifacts(raw: Any) -> list[dict]:
    out: list[dict] = []
    for item in _items(raw):
        if isinstance(item, dict):
            path = _text(item.get("path") or item.get("file") or item.get("url"))
            title = _text(item.get("title") or item.get("name"))
            kind = _text(item.get("kind")).lower()
        elif isinstance(item, str):
            path = _text(item)
            title, kind = "", ""
        else:
            continue
        if not path:
            continue
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        out.append({"path": path,
                    "title": title or name or path,
                    "kind": kind or artifact_kind(path)})
    return out


def _issues(raw: Any) -> list[dict]:
    out: list[dict] = []
    for item in _items(raw):
        if isinstance(item, dict):
            problem = _text(item.get("problem") or item.get("issue") or item.get("text"))
            where = _text(item.get("where") or item.get("task") or item.get("location"))
            fix = _text(item.get("fix") or item.get("how") or item.get("solution"))
        elif isinstance(item, str):
            problem, where, fix = _text(item), "", ""
        else:
            continue
        if not problem and not where:
            continue
        out.append({"where": where, "problem": problem, "fix": fix})
    return out


def _verdict(raw: Any) -> str | None:
    low = _text(raw).lower()
    if low in ("approve", "approved", "accept", "accepted", "ok", "lgtm", "утвержд", "принято"):
        return "approve"
    if low in ("revise", "revised", "revision", "changes", "changes_requested",
               "request_changes", "reject", "rejected", "переделать", "доработка"):
        return "revise"
    return None


def _report(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("path") or raw.get("report") or raw.get("file") or raw.get("url")
    text = _text(raw)
    return text or None


# ---------------------------------------------------------------- markdown-фолбэк

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_BOLD_RE = re.compile(r"^\s{0,3}\*\*(.{1,120}?)\*\*:?\s*$")
_ITEM_RE = re.compile(r"^\s*(?:[-*•+—]|\d{1,2}[.)])\s+(.*\S)\s*$")
_SECTION_PATTERNS = (
    ("next_steps", ("след", "дальше", "next step")),
    ("open_questions", ("вопрос",)),
    ("decisions", ("решени",)),
)


def _section_kind(heading: str) -> str | None:
    low = heading.lower()
    for kind, needles in _SECTION_PATTERNS:
        if any(n in low for n in needles):
            return kind
    return None


def _strip_md(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*|__(.+?)__|`(.+?)`",
                  lambda m: m.group(1) or m.group(2) or m.group(3) or "", text).strip()


def _parse_markdown(summary_md: Any) -> dict:
    """Разделы summary → черновые списки. Только для отсутствующих ключей."""
    if not isinstance(summary_md, str) or not summary_md.strip():
        return {}
    found: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in summary_md.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        heading = None
        m = _HEADING_RE.match(line)
        if m:
            heading = m.group(2)
        else:
            mb = _BOLD_RE.match(line)
            if mb:
                heading = mb.group(1)
        if heading is not None:
            current = _section_kind(heading)
            if current:
                found.setdefault(current, [])
            continue
        if current is None:
            continue
        mi = _ITEM_RE.match(line)
        if mi:
            text = _strip_md(mi.group(1))
            if text:
                found[current].append(text)
        elif line.strip() and found[current]:
            # продолжение пункта на следующей строке
            found[current][-1] = f"{found[current][-1]} {_strip_md(line)}".strip()
    return found
