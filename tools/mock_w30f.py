# -*- coding: utf-8 -*-
"""Мок пакета W30-F: скрепка (окно выбора файлов) и «Запросы без дела».

Зачем
-----
1. Композер и «Диалог по запросу» теперь зовут ОДИН компонент
   (`web/js/attach-picker.js`), который ходит в три серверные ручки:
   ``POST /api/files/pick``, ``POST /api/files/attach-check``,
   ``POST /api/files/attach-open``. Демо-сервер их не знал.
   Окно Windows здесь не открывается: список путей берётся из
   ``TEAMROOM_PICK_FAKE`` (как в боевом ``server/routes_files_pick.py``), пустая
   строка = «окно закрыли».

2. ``GET /api/rooms/{id}/results`` отдаёт «Запросы без дела» в форме боевого
   ответа: ``without_delo[]`` с РАЗНЫМ набором полей у завершённых и незавершённых
   записей (у ``pending`` нет ``slug/finished_at/short/reports``) и служебными
   статусами ``done`` / ``archived`` / ``failed`` — на них проверяется подпись
   по-русски (латиница в интерфейсе не показывается).

Нейтральные данные: кофейня и грузоперевозки, ничего реального.

    python tools/mock_server.py --port 8862
    # TEAMROOM_PICK_FAKE="C:/Загрузки/смета.xlsx;C:/Загрузки/договор.docx"
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "w30f_results.json"
ROOM_RE = r"[a-z][a-z0-9_-]{1,31}"
FAKE_ENV = "TEAMROOM_PICK_FAKE"
MAX_ATTACH = 30
#: Имена, которые считаются секретом (как у боевого `attachments`).
SECRET_PARTS = (".env", "id_rsa", ".pem", ".key", "token", "secret", "credentials")


def _fixture() -> dict:
    try:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fake_paths() -> list:
    """Пути из ``TEAMROOM_PICK_FAKE``: JSON-массив или по одному на строку."""
    raw = str(os.environ.get(FAKE_ENV) or "").replace("\r\n", "\n").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    items = []
    for chunk in raw.replace(";", "\n").split("\n"):
        text = chunk.strip()
        if text and text not in items:
            items.append(text)
    return items


def _is_secret(path: str) -> bool:
    name = Path(str(path)).name.lower()
    return any(part in name for part in SECRET_PARTS)


def file_info(path: str, confirm_secret: bool = False) -> dict:
    """Одна запись проверки: имя, размер, «похоже на секрет», причина отказа."""
    target = Path(str(path))
    secret = _is_secret(path)
    if secret and not confirm_secret:
        return {"path": str(path), "ok": False, "code": "needs_confirm", "secret": True,
                "name": target.name, "size": None, "ext": target.suffix,
                "message": "«%s» похож на секрет. Если он правда нужен команде — "
                           "нажмите «Всё равно прикрепить»." % target.name}
    exists = target.exists()
    size = target.stat().st_size if exists else None
    if not exists:
        return {"path": str(path), "ok": False, "code": "not_found", "secret": False,
                "name": target.name, "size": None, "ext": target.suffix,
                "message": "Файла нет по пути «%s». Выберите файл заново." % str(path)}
    return {"path": str(path), "ok": True, "code": "ok", "secret": secret,
            "name": target.name, "size": size, "ext": target.suffix, "message": ""}


def pick(body: dict) -> tuple:
    paths = _fake_paths()
    if not paths:
        return 200, {"ok": True, "paths": [], "cancelled": True, "count": 0,
                     "total": 0, "dropped": 0, "max": MAX_ATTACH}
    kept = paths[:MAX_ATTACH]
    return 200, {"ok": True, "paths": kept, "cancelled": not kept, "count": len(kept),
                 "total": len(paths), "dropped": len(paths) - len(kept), "max": MAX_ATTACH}


def attach_check(body: dict) -> tuple:
    raw = body.get("attach")
    if raw is None:
        raw = body.get("paths")
    if raw in (None, ""):
        return 200, {"ok": True, "items": [], "needs_confirm": [], "accepted": [],
                     "max": MAX_ATTACH}
    if isinstance(raw, str) or not isinstance(raw, list):
        return 400, {"error": {"code": "bad_request", "message": "attach: список путей",
                               "detail": {}}}
    if len(raw) > MAX_ATTACH:
        return 400, {"error": {"code": "bad_request",
                               "message": "attach: не больше %d путей" % MAX_ATTACH,
                               "detail": {"limit": MAX_ATTACH}}}
    confirm = body.get("confirm_secret") is True
    rows = [file_info(str(item), confirm) for item in raw]
    return 200, {"ok": True, "items": rows,
                 "needs_confirm": [r["path"] for r in rows if r["code"] == "needs_confirm"],
                 "accepted": [r["path"] for r in rows if r["ok"]],
                 "max": MAX_ATTACH}


def _made_delos(room_id: str) -> set:
    """Запросы, из которых в демо уже сделали дело (память мока «Дела»).

    Пока пакет F отдавал «Запросы без дела» своей фикстурой, кнопка «Сделать
    делом» перестала убирать строку: состояние живёт в ``mock_ac1._MADE``, а
    фикстура о нём не знала. Читаем то же состояние — один источник, как на
    боевом сервере (там ``delo`` приходит из дел комнаты).
    """
    try:
        import mock_ac1                     # соседний мок «Дел» (плоский импорт в tools/)
        return set((mock_ac1._MADE or {}).get(room_id) or ())
    except Exception:                      # noqa: BLE001 — без мока дел фикстура как есть
        return set()


def results(app, room_id: str) -> tuple:
    """Форма боевого ответа: разные поля у done/pending, служебные статусы."""
    room = app.room(room_id) or {}
    data = _fixture()
    items = [dict(row) for row in (data.get("items") or [])]
    pending = [dict(row) for row in (data.get("pending") or [])]
    made = _made_delos(room_id)
    for row in items + pending:
        rid = str(row.get("id") or "")
        if rid in made:
            row["delo"] = {"id": "d_" + rid, "title": str(row.get("title") or "")}
    without = [row for row in items + pending if not row.get("delo")]
    return 200, {"ok": True, "room": room_id,
                 "room_title": str(room.get("title") or ""),
                 "generated_at": int(data.get("generated_at") or 0),
                 "items": items, "pending": pending, "without_delo": without,
                 "reports": [],
                 "counts": {"requests": len(items) + len(pending), "results": len(items),
                            "pending": len(pending), "reports": 0,
                            "without_delo": len(without)}}


def handle(app, method: str, path: str, query: dict, body: dict):
    """Вернуть ``(status, payload)`` для ручек пакета F или ``None``."""
    if method == "GET":
        m = re.fullmatch(r"/api/rooms/(%s)/results" % ROOM_RE, path)
        if m:
            return results(app, m.group(1))
        return None
    if method == "POST":
        if path == "/api/files/pick":
            return pick(body or {})
        if path == "/api/files/attach-check":
            return attach_check(body or {})
        if path == "/api/files/attach-open":
            data = body or {}
            if not str(data.get("path") or "").strip():
                return 400, {"error": {"code": "bad_request", "message": "Нужен path",
                                       "detail": {}}}
            return 200, {"ok": True, "path": str(data.get("path")), "opened": True,
                         "mode": str(data.get("mode") or "open"),
                         "revealed": str(data.get("mode") or "open") == "reveal",
                         "note": "Демо: окно программы не открывается."}
    return None


__all__ = ["FAKE_ENV", "FILE", "attach_check", "file_info", "handle", "pick", "results"]
FILE = FIXTURE
