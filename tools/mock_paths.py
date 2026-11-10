# -*- coding: utf-8 -*-
"""Проверка пути «Папка проекта» в моке — БЕЗ обращения к диску.

`POST /api/rooms/check-dir {path}` → `{normalized, exists, writable, will_create,
warnings[], allowed, reason, message}` (Д2 WAVE8; контракт `server/project_dir.py`).

Демо ничего не создаёт и ничего не читает: `exists` берётся из таблицы
демо-каталогов, `will_create` — из неё же. Это честное ограничение мока и
написано в docs/DEMO.md.
"""
from __future__ import annotations

import ntpath
import os
import re

from mock_core import ROOT

VAULT_DIR = r"D:\Проекты\Vault"
HERMES_HOME_FALLBACK = r"C:\Users\User\AppData\Local\hermes"
SYSTEM_ROOTS = (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)", r"C:\ProgramData",
                 r"C:\PerfLogs", r"C:\Recovery", r"C:\System Volume Information", r"C:\$Recycle.Bin")
# Демо-каталоги, которые «уже есть» на стенде (реальный диск не спрашиваем).
DEMO_EXISTING = (r"D:\Проекты", r"D:\Проекты\Vault", r"D:\Проекты\Комната",
                 r"D:\Проекты\Архив", r"C:\Users", r"C:\Users\User")
# Деревья, у которых все подпапки считаются существующими (Vault и личная папка).
DEMO_TREES = (r"D:\Проекты\Vault", r"C:\Users\User")
RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}
MAX_PATH = 400

MESSAGES = {
    "empty": "Путь пустой — выберите папку проекта",
    "relative": "Нужен полный путь, например D:\\Проекты\\Комната",
    "network": "Сетевая папка (\\\\сервер\\папка) не подойдёт — выберите папку на этом компьютере",
    "traversal": "В пути есть «..» — укажите полный путь без сокращений",
    "reserved": "Имя папки зарезервировано Windows (CON, PRN, AUX, NUL, COM1…LPT9) — выберите другое",
    "system_root": "Это системная папка Windows — в неё писать нельзя. Выберите папку проекта",
    "app_data": "Это служебная папка приложения Hermes — выберите отдельную папку проекта",
    "too_long": "Путь длиннее %d символов — выберите папку ближе к корню диска" % MAX_PATH,
}


def hermes_home() -> str:
    """`%LOCALAPPDATA%\\hermes` — только из переменной окружения, без чтения диска."""
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return ntpath.join(root, "hermes") if root else HERMES_HOME_FALLBACK


def _norm(path: str) -> str:
    return ntpath.normpath(str(path).replace("/", "\\"))


def _is_inside(path: str, root: str) -> bool:
    low, base = path.casefold().rstrip("\\"), root.casefold().rstrip("\\")
    return low == base or low.startswith(base + "\\")


def _reserved(parts: list) -> str | None:
    for part in parts:
        stem = part.split(".")[0].strip().casefold()
        if stem in RESERVED:
            return part
    return None


def check_dir(path: str, *, app_dir: str | None = None) -> dict:
    """Результат проверки пути. `allowed: false` — не сохранение, а объяснение почему."""
    raw = str(path or "").strip()
    out = {"normalized": raw, "exists": False, "writable": False, "will_create": False,
           "warnings": [], "allowed": False, "reason": None, "message": None}

    def deny(reason: str, normalized: str | None = None) -> dict:
        out.update({"allowed": False, "reason": reason, "message": MESSAGES[reason],
                    "writable": False, "will_create": False})
        if normalized is not None:
            out["normalized"] = normalized
        return out

    if not raw:
        return deny("empty")
    if len(raw) > MAX_PATH:
        return deny("too_long")
    if raw.startswith("\\\\") or raw.startswith("//"):
        return deny("network", _norm(raw))
    if not re.match(r"^[A-Za-z]:(\\|/|$)", raw):
        return deny("relative", raw)
    raw_parts = [p for p in raw.replace("/", "\\").split("\\") if p]
    if ".." in raw_parts or "." == (raw_parts[-1] if raw_parts else ""):
        return deny("traversal", _norm(raw))
    normalized = _norm(raw)
    parts = [p for p in normalized.split("\\") if p]
    if len(parts) == 1:                      # «C:\» — корень диска
        return deny("system_root", normalized)
    if _reserved(parts):
        return deny("reserved", normalized)
    out["normalized"] = normalized

    homes = [hermes_home()]
    if app_dir:
        homes.append(str(app_dir))
    for home in homes:
        if home and _is_inside(normalized, home):
            return deny("app_data", normalized)
    for root in SYSTEM_ROOTS:
        if _is_inside(normalized, root):
            return deny("system_root", normalized)
    if _is_inside(normalized, VAULT_DIR):
        out["warnings"].append("Папка внутри Obsidian Vault: материалы будут и в заметках. "
                              "Обычно лучше выбрать папку вне Vault, например D:\\Проекты\\Комната")

    exists = (any(normalized.casefold() == demo.casefold() for demo in DEMO_EXISTING)
              or any(_is_inside(normalized, tree) for tree in DEMO_TREES))
    out.update({"exists": exists, "writable": True, "will_create": not exists, "allowed": True,
                "reason": None, "message": None})
    return out


def room_path(path, *, app_dir: str | None = None) -> dict:
    """Путь для сохранения комнаты: нормализованный и проверенный (или ошибка для 400)."""
    info = check_dir(path, app_dir=app_dir or ROOT)
    if info["allowed"]:
        return info
    return info
