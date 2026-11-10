"""Примитивы нормализации metadata: фазы, стабильные id, даты, приведение типов.

Нижний слой `server/normalize.py` (публичный фасад). Здесь только то, что не зависит
от остальных частей: канонические фазы, `stable_id`, `parse_due`, `artifact_kind`,
безопасные приведения (`_text`, `_as_dict`, `_items`, …).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any

# Этапы проекта — свойство КОМНАТЫ (WAVE17 §R, B-8): канонического списка этапов в
# коде больше нет. Здесь остаётся только общая грамматика: статусы этапа и
# приведение заголовка к id. Названия этапов приходят из metadata ответа и из
# настроек комнаты (`server/phase_book.py`).
PHASE_STATUSES = ("done", "active", "next", "blocked")

STATES = ("open", "asked", "done", "dismissed")

_PHASE_ID_RE = re.compile(r"[^a-zа-я0-9]+")


def phase_id(text: Any, fallback: str = "phase") -> str:
    """Заголовок этапа → id: «Согласование и ИП» → «согласование-и-ип»."""
    raw = _text(text).lower()
    slug = _PHASE_ID_RE.sub("-", raw).strip("-")
    return slug or fallback

# ---------------------------------------------------------------- утилиты


def stable_id(prefix: str, text: str) -> str:
    """`<prefix>_<sha1(нормализованный текст)[:8]>` — стабильный id элемента."""
    norm = re.sub(r"\s+", " ", (text or "").strip())
    digest = hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def _text(value: Any) -> str:
    """Строка из чего угодно: None → ''; числа/объекты приводим аккуратно."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_DATE_RU = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$")


def parse_due(value: Any) -> str | None:
    """Дата в ISO `YYYY-MM-DD` или `None`.

    Принимается ISO-дата (и ISO-datetime — берётся дата) и `DD.MM.YYYY`.
    Всё остальное (`«к пятнице»`, `«10 ноября»`, мусор, несуществующая дата)
    → `None`: исходный текст остаётся в `text` шага, дату не выдумываем.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None
    raw = _text(value)
    if not raw:
        return None
    m = _DATE_ISO.match(raw) or None
    if m is None:
        m = _DATE_RU.match(raw)
    if m is None:
        return None
    parts = m.groups()
    if len(parts) == 3 and len(parts[0]) == 4:      # YYYY-MM-DD
        y, mo, d = (int(x) for x in parts)
    else:                                          # DD.MM.YYYY
        d, mo, y = (int(x) for x in parts)
    try:
        date(y, mo, d)
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


_KIND_BY_EXT = {
    "md": "md", "markdown": "md", "txt": "other", "json": "json",
    "html": "html", "htm": "html", "csv": "csv",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
    "webp": "image", "svg": "image", "pdf": "pdf",
    "docx": "docx", "xlsx": "xlsx", "pptx": "pptx",
}


def artifact_kind(path: Any) -> str:
    """Тип артефакта по расширению (API §3.4), неизвестное → `other`."""
    name = _text(path).replace("\\", "/").rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _KIND_BY_EXT.get(ext, "other")


def _as_dict(meta: Any) -> dict:
    """metadata → dict. JSON-строка разбирается, всё остальное → {}."""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, (bytes, bytearray)):
        try:
            meta = meta.decode("utf-8", "replace")
        except Exception:
            return {}
    if isinstance(meta, str):
        raw = meta.strip()
        if not raw:
            return {}
        try:
            obj = json.loads(raw, strict=False)
        except (ValueError, TypeError):
            return {}
        return obj if isinstance(obj, dict) else {}
    return {}


def _clean_keys(meta: dict) -> dict:
    """Убрать пробелы вокруг ключей (реальный баг: ' contradictions_flagged')."""
    out: dict = {}
    for key, value in meta.items():
        if not isinstance(key, str):
            continue
        k = key.strip()
        if not k:
            continue
        if k not in out:  # первый вариант важнее дубля после strip
            out[k] = value
    return out


def _items(value: Any) -> list:
    """Значение → список элементов; одиночная строка/объект тоже допустимы."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (list, tuple)):
        flat: list = []
        for v in value:
            if isinstance(v, (list, tuple)):
                flat.extend(v)
            else:
                flat.append(v)
        return flat
    if isinstance(value, (str, dict)):
        return [value]
    return []


def _dedupe_ids(prefix: str, texts: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for text in texts:
        base = stable_id(prefix, text)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base}_{n + 1}")
    return out
