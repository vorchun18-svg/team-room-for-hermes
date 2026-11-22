# -*- coding: utf-8 -*-
"""Демо-хранилище для мок-сервера: `GET /api/storage` и безопасная уборка.

Отвечает той же формой, что и живой бэкенд (`server/storage.py`), чтобы экран
«Хранилище» проверялся на моке без боевых данных. Пути — вымышленные
(`C:/TeamRoom/...`), ничего на диске не читается и не удаляется: сухой прогон и
«удаление» живут только в памяти процесса.

Подключение: `tools/mock_server.py` отдаёт сюда все пути `/api/storage*`.
"""
from __future__ import annotations

import time

POLICY = {
    "voice_hours": 72, "voice_max": 30, "logs_days": 30,
    "backups_keep": 5, "backups_per_release": 1, "build_cache_days": 7,
    "tmp_hours": 24, "accept_immediately": 1, "copy_max_bytes": 2 * 1024 * 1024,
}

LIMITS = {
    "voice_hours": [0, 8760], "voice_max": [0, 5000], "logs_days": [0, 3650],
    "backups_keep": [1, 200], "backups_per_release": [0, 10], "build_cache_days": [0, 365],
    "tmp_hours": [0, 8760], "accept_immediately": [0, 1],
    "copy_max_bytes": [65536, 67108864],
}

_CATEGORIES = [
    {"id": "voice", "title": "Голосовые диктовки", "path": "C:/TeamRoom/data/stt-recordings",
     "exists": True, "bytes": 18_400_000, "size_text": "17,5 МБ", "files": 6, "foreign": False,
     "cleanable": True, "policy": "старше 72 ч и сверх 30 записей",
     "why": "Части длинных записей на диске. Расшифровка уже сохранена в запросе — аудио нужно "
            "только для повторной расшифровки.",
     "will_remove": {"files": 2, "bytes": 12_600_000, "size_text": "12 МБ", "count": 2,
                     "preview": [{"name": "rec-9f2c11", "size": 6_300_000, "size_text": "6 МБ",
                                  "path": "C:/TeamRoom/data/stt-recordings/rec-9f2c11"},
                                 {"name": "rec-7a10de", "size": 6_300_000, "size_text": "6 МБ",
                                  "path": "C:/TeamRoom/data/stt-recordings/rec-7a10de"}]},
     "truncated": False},
    {"id": "logs", "title": "Логи приложения", "path": "C:/TeamRoom/data/logs",
     "exists": True, "bytes": 3_100_000, "size_text": "3 МБ", "files": 9, "foreign": False,
     "cleanable": True, "policy": "старше 30 дн.",
     "why": "Журнал app.log и его ротации. Нужны для разбора недавних сбоев.",
     "will_remove": {"files": 1, "bytes": 700_000, "size_text": "684 КБ", "count": 1,
                     "preview": [{"name": "app.log.3", "size": 700_000, "size_text": "684 КБ",
                                  "path": "C:/TeamRoom/data/logs/app.log.3"}]},
     "truncated": False},
    {"id": "backups", "title": "Бэкапы Team Room", "path": "C:/TeamRoom/backups",
     "exists": True, "bytes": 141_000_000, "size_text": "134,5 МБ", "files": 9, "foreign": False,
     "cleanable": True, "policy": "последние 5 + по одному на релиз",
     "why": "Копии перед обновлением. Свежие нужны для отката, старые — нет.",
     "will_remove": {"files": 1, "bytes": 15_000_000, "size_text": "14,3 МБ", "count": 1,
                     "preview": [{"name": "team-room-1.4.2-02.zip", "size": 15_000_000,
                                  "size_text": "14,3 МБ",
                                  "path": "C:/TeamRoom/backups/team-room-1.4.2-02.zip"}]},
     "truncated": False},
    {"id": "tmp", "title": "Временные копии приложения", "path": "C:/TeamRoom/data/tmp",
     "exists": True, "bytes": 4_200_000, "size_text": "4 МБ", "files": 12, "foreign": False,
     "cleanable": True, "policy": "старше 24 ч",
     "why": "Промежуточные файлы экспорта и сборки отчётов.",
     "will_remove": {"files": 1, "bytes": 700_000, "size_text": "684 КБ", "count": 1,
                     "preview": [{"name": "export-old.csv", "size": 700_000, "size_text": "684 КБ",
                                  "path": "C:/TeamRoom/data/tmp/export-old.csv"}]},
     "truncated": False},
    {"id": "builds", "title": "Кэш сборок релиза", "path": "C:/TeamRoom/data/builds",
     "exists": True, "bytes": 96_000_000, "size_text": "91,6 МБ", "files": 240, "foreign": False,
     "cleanable": True, "policy": "старше 7 дн.",
     "why": "Наши сборки релиза. Нужны только на время сборки и проверки.",
     "will_remove": {"files": 1, "bytes": 48_000_000, "size_text": "45,8 МБ", "count": 1,
                     "preview": [{"name": "w28-release", "size": 48_000_000,
                                  "size_text": "45,8 МБ",
                                  "path": "C:/TeamRoom/data/builds/w28-release"}]},
     "truncated": False},
    {"id": "vault", "title": "Vault (Obsidian)", "path": "C:/TeamRoom/Vault",
     "exists": True, "bytes": 352_000_000, "size_text": "335,7 МБ", "files": 100, "foreign": True,
     "cleanable": False, "policy": "не удаляется",
     "why": "Заметки и материалы комнат. Ваши документы — их убирает только человек.",
     "will_remove": {"files": 0, "bytes": 0, "size_text": "0 Б", "count": 0, "preview": []},
     "proposal": {"files": 12, "bytes": 320_000_000, "available": True,
                  "sample": [{"name": "Меню.pdf", "size": 151_000_000, "size_text": "144 МБ"}]},
     "truncated": False},
    {"id": "hermes_cache", "title": "Кэш Hermes (scratch)", "path": "C:/TeamRoom/hermes/cache/scratch",
     "exists": True, "bytes": 443_000_000, "size_text": "422,5 МБ", "files": 16_234,
     "foreign": True, "cleanable": False, "policy": "только показ",
     "why": "Рабочие файлы Hermes и сборок. Чистит сам Hermes, мы только показываем размер.",
     "will_remove": {"files": 0, "bytes": 0, "size_text": "0 Б", "count": 0, "preview": []},
     "truncated": False},
    {"id": "hermes_logs", "title": "Логи Hermes", "path": "C:/TeamRoom/hermes/logs",
     "exists": True, "bytes": 51_000_000, "size_text": "48,6 МБ", "files": 189, "foreign": True,
     "cleanable": False, "policy": "только показ",
     "why": "Журналы Hermes Desktop. Нужны для разбора сбоев моделей.",
     "will_remove": {"files": 0, "bytes": 0, "size_text": "0 Б", "count": 0, "preview": []},
     "truncated": False},
]

#: Что «удалили» в демо: имена, чтобы повторный прогон был честным.
_removed: set = set()


def overview() -> dict:
    categories = []
    total = 0
    for row in _CATEGORIES:
        item = dict(row)
        item["will_remove"] = dict(row["will_remove"])
        preview = [p for p in item["will_remove"].get("preview", []) if p["name"] not in _removed]
        item["will_remove"]["preview"] = preview
        if item["cleanable"] and _removed:
            item["will_remove"]["count"] = max(0, item["will_remove"]["count"]
                                              - len(_removed & {p["name"] for p in row["will_remove"].get("preview", [])}))
        total += int(item["bytes"])
        categories.append(item)
    return {"ok": True, "generated_at": int(time.time()), "total_bytes": total,
            "total_text": "1,1 ГБ", "free_bytes": 389_000_000_000, "free_text": "362 ГБ",
            "categories": categories, "policy": dict(POLICY), "warning": None,
            "root": "C:/TeamRoom/data"}


def clean(payload: dict) -> dict:
    dry_run = payload.get("dry_run")
    dry_run = True if dry_run is None else bool(dry_run)
    confirm = bool(payload.get("confirm"))
    wanted = payload.get("categories") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    if not dry_run and not confirm:
        raise ValueError("confirm_required")
    items = []
    for row in _CATEGORIES:
        if not row["cleanable"]:
            continue
        if wanted and row["id"] not in wanted:
            continue
        for item in row["will_remove"].get("preview", []):
            if item["name"] in _removed:
                continue
            items.append({"path": item["path"], "bytes": item["size"], "files": 1,
                          "size_text": item["size_text"]})
    result = {"dry_run": dry_run, "removed": [], "would_remove": [], "refused": [],
              "errors": [], "bytes": 0, "files": 0,
              "categories": sorted({row["id"] for row in _CATEGORIES if row["cleanable"]}),
              "generated_at": int(time.time()), "policy": dict(POLICY)}
    if dry_run:
        result["would_remove"] = items
        result["bytes"] = sum(int(i["bytes"]) for i in items)
        result["files"] = len(items)
        result["message"] = ("Сухой прогон: ничего не удалено. К удалению — %d объект(ов), %s."
                             % (len(items), "12 МБ"))
    else:
        for item in items:
            _removed.add(item["path"].rsplit("/", 1)[-1])
        result["removed"] = items
        result["bytes"] = sum(int(i["bytes"]) for i in items)
        result["files"] = len(items)
        result["message"] = ("Удалено %d объект(ов), освобождено 12 МБ. Не удалось: 0."
                             % len(items))
    return result


def policy_get() -> dict:
    return {"ok": True, "policy": dict(POLICY), "limits": dict(LIMITS),
            "defaults": dict(POLICY)}


def policy_put(payload: dict) -> dict:
    fields = payload.get("policy") if isinstance(payload.get("policy"), dict) else payload
    for key, value in (fields or {}).items():
        if key not in POLICY:
            raise ValueError("unknown:%s" % key)
        low, high = LIMITS[key]
        number = int(value)
        if number < low or number > high:
            raise ValueError("range:%s" % key)
        POLICY[key] = number
    return {"ok": True, "policy": dict(POLICY), "message": "Настройки хранения сохранены."}


def material_policy(path: str) -> dict:
    name = path.rsplit("/", 1)[-1] or "файл"
    low = name.lower()
    text = low.endswith((".md", ".txt", ".csv", ".json", ".log"))
    size = 1_500_000 if not text else 4096
    copy = bool(text and size <= POLICY["copy_max_bytes"])
    return {"ok": True, "material": {
        "path": path, "name": name, "exists": True, "size": size,
        "size_text": "1,4 МБ" if not text else "4 КБ", "text": text, "copy": copy,
        "link": not copy, "limit": POLICY["copy_max_bytes"], "limit_text": "2 МБ",
        "kind": "PDF-документ" if low.endswith(".pdf") else ("текст" if text else "файл"),
        "reason": ("Небольшой текстовый файл — копия в Vault, как раньше." if copy else
                   "Нетекстовый файл — в Vault только ссылка, оригинал остаётся на месте."),
        "sha256": "0" * 64, "sha256_short": "0" * 16,
        "note": "" if copy else "В Vault копия не создаётся: файл остаётся на месте."}}


def materials_link() -> dict:
    return {"ok": True, "path": "C:/TeamRoom/Vault/cafe/Материалы",
            "target": "C:/Проекты/Кофейня", "exists": False, "is_link": False,
            "proposal": {"default": "C:/TeamRoom/Vault/cafe", "current": "C:/Проекты/Кофейня",
                         "same": False, "suggest": True,
                         "hint": "Папка проекта лежит вне Vault — можно поставить ссылку "
                                 "«Материалы» в Obsidian, ничего не копируя."}}


def materials_link_set(payload: dict) -> dict:
    action = str(payload.get("action") or "")
    if action == "create":
        return {"ok": True, "created": True, "path": "C:/TeamRoom/Vault/cafe/Материалы",
                "target": "C:/Проекты/Кофейня",
                "message": "Ссылка «Материалы» создана — Obsidian видит папку проекта.",
                "proposal": materials_link()["proposal"]}
    return {"ok": True, "removed": True, "path": "C:/TeamRoom/Vault/cafe/Материалы",
            "message": "Ссылка снята. Файлы в папке проекта на месте.",
            "proposal": materials_link()["proposal"]}


__all__ = ["overview", "clean", "policy_get", "policy_put", "material_policy",
           "materials_link", "materials_link_set"]
