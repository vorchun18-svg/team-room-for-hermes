"""Реестр запросов комнаты: data/state/<room>/requests.json (ARCH §12.10).

Хранит корневые карточки, которые принадлежат комнате, отметки ingest-а
(`ingested: {run_id: true}`), дополнительные карточки критика/доработки.
Все read-modify-write циклы идут под блокировкой файла — параллельные
обновления не теряют данные.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import statefiles as sf

REQUESTS_FILE = "requests.json"
DEFAULT_FIELDS = {
    "root_id": "",
    "slug": "",
    "title": "",
    "text": "",
    "created_at": 0,
    "critic": False,
    "mode": "auto",
    "adopted": False,
    "extra_cards": [],
    "ingested": {},
    "retargeted": False,
}


def _now() -> int:
    return int(time.time())


def registry_path(cfg, room_id: str) -> Path:
    return Path(cfg.state_dir) / str(room_id) / REQUESTS_FILE


def _load(cfg, room_id: str) -> dict:
    """Реестр комнаты. Битый файл копируется в ``.corrupt-<ts>`` до перезаписи (D5).

    «Файла нет» — пустой реестр (норма). «Прочитать нельзя» (занят/нет прав) —
    ошибка ``StateUnavailable``: писать поверх нельзя, иначе потеряются adopted/
    ingested/answers.
    """
    path = registry_path(cfg, room_id)
    data, info = sf.read_json_doc(path, None)
    if info["state"] == "unreadable":
        raise sf.StateUnavailable(
            "Не удалось прочитать реестр запросов комнаты «%s»: %s. Данные не изменены."
            % (room_id, info.get("error") or "файл занят"),
            {"room": str(room_id), "path": str(path)},
        )
    if info["state"] == "corrupt":
        sf.quarantine(path)             # копия ДО любой перезаписи
        data = None
    if not isinstance(data, dict):
        data = {"room": str(room_id), "updated_at": 0, "requests": []}
    if not isinstance(data.get("requests"), list):
        data["requests"] = []
    return data


def _norm_rec(rec: dict) -> dict:
    """Привести запись к канонической форме, не теряя незнакомые поля."""
    out = {k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in DEFAULT_FIELDS.items()}
    for key, value in (rec or {}).items():
        out[key] = value
    out["root_id"] = str(out.get("root_id") or "")
    out["slug"] = str(out.get("slug") or "")
    out["title"] = str(out.get("title") or "")
    out["text"] = str(out.get("text") or "")
    try:
        out["created_at"] = int(out.get("created_at") or 0)
    except (TypeError, ValueError):
        out["created_at"] = 0
    out["critic"] = bool(out.get("critic"))
    out["adopted"] = bool(out.get("adopted"))
    out["retargeted"] = bool(out.get("retargeted"))
    out["mode"] = str(out.get("mode") or "auto")
    if not isinstance(out.get("extra_cards"), list):
        out["extra_cards"] = []
    if not isinstance(out.get("ingested"), dict):
        out["ingested"] = {}
    return out


def add_request(cfg, room_id: str, rec: dict) -> dict:
    """Добавить запись реестра. Повторный root_id не дублируется: поля сливаются."""
    entry = _norm_rec(rec)
    if not entry["root_id"]:
        raise ValueError("add_request: root_id обязателен")
    path = registry_path(cfg, room_id)
    with sf.lock_for(path):
        data = _load(cfg, room_id)
        for idx, existing in enumerate(data["requests"]):
            if existing.get("root_id") == entry["root_id"]:
                merged = _norm_rec({**existing, **{k: v for k, v in entry.items() if v not in ("", None, [], {})}})
                merged["ingested"] = {**existing.get("ingested", {}), **entry.get("ingested", {})}
                data["requests"][idx] = merged
                data["updated_at"] = _now()
                sf.write_json_atomic(path, data)
                return merged
        data["requests"].append(entry)
        data["room"] = str(room_id)
        data["updated_at"] = _now()
        sf.write_json_atomic(path, data)
    return entry


def get(cfg, room_id: str, root_id: str) -> dict | None:
    """Запись по root_id или None."""
    for rec in _load(cfg, room_id)["requests"]:
        if rec.get("root_id") == root_id:
            return rec
    return None


def find_by_slug(cfg, room_id: str, slug: str) -> dict | None:
    for rec in _load(cfg, room_id)["requests"]:
        if rec.get("slug") == slug:
            return rec
    return None


def list_requests(cfg, room_id: str, limit: int = 50) -> list[dict]:
    """Запросы комнаты, свежие первыми."""
    items = list(_load(cfg, room_id)["requests"])
    items.sort(key=lambda r: (int(r.get("created_at") or 0), str(r.get("root_id") or "")), reverse=True)
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def update(cfg, room_id: str, root_id: str, patch: dict) -> dict | None:
    """Частично обновить запись; None — такой записи нет. Безопасно при гонках."""
    if not isinstance(patch, dict):
        raise ValueError("update: patch должен быть словарём")
    path = registry_path(cfg, room_id)
    with sf.lock_for(path):
        data = _load(cfg, room_id)
        for idx, existing in enumerate(data["requests"]):
            if existing.get("root_id") != root_id:
                continue
            merged = {**existing}
            for key, value in patch.items():
                if key == "ingested":
                    if not isinstance(value, dict):
                        raise ValueError("update: ingested должен быть словарём")
                    merged["ingested"] = {**merged.get("ingested", {}), **value}
                elif key == "extra_cards":
                    if not isinstance(value, list):
                        raise ValueError("update: extra_cards должен быть списком")
                    merged["extra_cards"] = value
                elif key == "root_id":
                    continue  # идентификатор неизменяем
                else:
                    merged[key] = value
            merged = _norm_rec(merged)
            data["requests"][idx] = merged
            data["updated_at"] = _now()
            sf.write_json_atomic(path, data)
            return merged
    return None


def add_extra_card(cfg, room_id: str, root_id: str, card: dict) -> dict | None:
    """Добавить карточку критика/доработки (идемпотентно по card['id'])."""
    if not isinstance(card, dict) or not card.get("id"):
        raise ValueError("add_extra_card: нужен dict с id")
    path = registry_path(cfg, room_id)
    with sf.lock_for(path):
        data = _load(cfg, room_id)
        for idx, existing in enumerate(data["requests"]):
            if existing.get("root_id") != root_id:
                continue
            cards = list(existing.get("extra_cards") or [])
            if not any(c.get("id") == card["id"] for c in cards):
                cards.append(dict(card))
                existing["extra_cards"] = cards
                data["requests"][idx] = existing
                data["updated_at"] = _now()
                sf.write_json_atomic(path, data)
            return existing
    return None


def mark_ingested(cfg, room_id: str, root_id: str, run_id: str) -> bool:
    """Отметить run как разобранный. True — он был новым, False — уже был."""
    if not run_id:
        raise ValueError("mark_ingested: run_id обязателен")
    path = registry_path(cfg, room_id)
    with sf.lock_for(path):
        data = _load(cfg, room_id)
        for idx, existing in enumerate(data["requests"]):
            if existing.get("root_id") != root_id:
                continue
            ingested = dict(existing.get("ingested") or {})
            if run_id in ingested:
                return False
            ingested[run_id] = True
            existing["ingested"] = ingested
            data["requests"][idx] = existing
            data["updated_at"] = _now()
            sf.write_json_atomic(path, data)
            return True
    return False


def all_registered_ids(cfg, board: str | None = None) -> set[str]:
    """Все root_id реестров; board ограничивает комнаты одной доской."""
    rooms = None
    if board:
        data = sf.read_json(Path(cfg.data_dir) / "rooms.json", {}) or {}
        rooms = {
            str(r.get("id")) for r in data.get("rooms", [])
            if isinstance(r, dict) and str(r.get("board") or "team") == board
        }
    ids: set[str] = set()
    state_dir = Path(cfg.state_dir)
    if not state_dir.is_dir():
        return ids
    for sub in state_dir.iterdir():
        if not sub.is_dir():
            continue
        if rooms is not None and sub.name not in rooms:
            continue
        for rec in _load(cfg, sub.name)["requests"]:
            root_id = rec.get("root_id")
            if root_id:
                ids.add(str(root_id))
    return ids
