"""Общее для ingest: константы, пути состояния комнаты, мелкие помощники, `IngestError`.

Листовой модуль (ничего не импортирует из ingest*); остальные части раскладки —
`ingest_extract`, `ingest_merge`, `vault_mirror` — берут отсюда константы и пути.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path

# Этапы проекта — свойство КОМНАТЫ (WAVE17 §R, B-8): списка этапов в коде нет,
# заголовки приходят из настроек комнаты и из metadata ответа.
PHASE_STATUSES = ("done", "active", "next", "blocked")
STATUSES = ("open", "asked", "done", "dismissed")
KINDS = ("next_step", "future_question", "open_question", "decision")
STATUS_RU = {"done": "готово", "active": "в работе", "next": "впереди", "blocked": "ждёт решения"}
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]


MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


STATUS_ORDER = {"open": 0, "asked": 1, "done": 2, "dismissed": 3}


class IngestError(Exception):
    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _room_id(room) -> str:
    return str(room.get("id")) if isinstance(room, dict) else str(room)


def _state_dir(cfg, room_id: str) -> Path:
    return Path(cfg.state_dir) / str(room_id)


def _roadmap_path(cfg, room_id): return _state_dir(cfg, room_id) / "roadmap.json"


def _decisions_path(cfg, room_id): return _state_dir(cfg, room_id) / "decisions.json"


def _backlog_path(cfg, room_id): return _state_dir(cfg, room_id) / "backlog.json"


def _journal_path(cfg, room_id): return _state_dir(cfg, room_id) / "journal.jsonl"


def _reports_path(cfg, room_id): return _state_dir(cfg, room_id) / "reports" / "index.json"


def _now() -> int:
    return int(_dt.datetime.now().timestamp())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text) -> str:
    """Однострочный текст для таблиц/журнала: без переводов строк и лишних пробелов."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _phase_template(phase_id: str, title: str | None = None) -> dict:
    """Заготовка этапа комнаты: заголовок только из данных, никаких значений по умолчанию."""
    return {"id": phase_id, "title": title or phase_id, "status": "next",
            "note": "", "updated_at": None, "source_request": None, "source_at": 0}


def _current_phase(phases: list[dict]) -> dict | None:
    """API §3.4: первая по порядку фаза active|blocked, иначе последняя done, иначе первая next."""
    for p in phases:
        if p.get("status") in ("active", "blocked"):
            return {"id": p["id"], "title": p["title"], "status": p["status"]}
    done = [p for p in phases if p.get("status") == "done"]
    if done:
        p = done[-1]
        return {"id": p["id"], "title": p["title"], "status": p["status"]}
    for p in phases:
        if p.get("status") == "next":
            return {"id": p["id"], "title": p["title"], "status": p["status"]}
    return None
