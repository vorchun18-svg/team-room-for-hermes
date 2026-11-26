"""Раздел «Архив» комнаты: список, массовый архив и возврат (W29-P4).

Здесь только HTTP-слой. Правила («что значит архив», какие статусы возвращаются)
живут в ``server/archive.py``; чтение — существующие ``engine_read``/реестр.

Маршруты::

    GET  /api/rooms/{room}/archive            — архивные запросы комнаты (поиск, даты)
    POST /api/rooms/{room}/archive            — массово в архив {root_ids: [...]}
    POST /api/rooms/{room}/archive/restore    — массово из архива {root_ids: [...]}

Одиночные действия идут через страницу запроса:
``POST /api/requests/{rid}/actions {action: "archive"|"unarchive"}`` — тот же
модуль, один источник правды.

Подключение — одним блоком в ``server/app.py``::

    from server.routes_archive import install_archive_routes
    install_archive_routes(App, ROUTES)
"""
from __future__ import annotations

import re as _re
import time

try:  # пакетный и «плоский» импорт
    from . import archive as archive_mod
    from . import engine_write as engine_write_mod
    from .app_common import err
except ImportError:  # pragma: no cover
    import archive as archive_mod        # type: ignore
    import engine_write as engine_write_mod  # type: ignore
    from app_common import err           # type: ignore

#: Сколько записей отдавать за раз и сколько запросов можно убрать одним нажатием.
PAGE_LIMIT = 200
MASS_LIMIT = 100

ROUTES = [
    ("GET", r"/api/rooms/(?P<room>[^/]+?)/archive", "h_archive_list"),
    # Массовый уход в архив: путь со `/requests`, потому что `/api/rooms/{id}/archive`
    # уже занят архивацией КОМНАТЫ (server/app.py → h_room_archive). Дубли путей запрещены.
    ("POST", r"/api/rooms/(?P<room>[^/]+?)/archive/requests", "h_archive_mass"),
    ("POST", r"/api/rooms/(?P<room>[^/]+?)/archive/restore", "h_archive_restore"),
]


def _epoch(value):
    """Дата из фильтра: unix-секунды или ``YYYY-MM-DD`` (местные сутки). None — нет."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(time.mktime(time.strptime(text, "%Y-%m-%d")))
    except ValueError:
        return None


def _first(query, name: str) -> str:
    value = (query or {}).get(name) or [""]
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


class ArchiveRoutes:
    """Методы ``App`` для раздела «Архив»."""

    def h_archive_list(self, query, body, room=None):
        room_id = str(room or "") or _first(query, "room")
        if not room_id:
            raise err(400, "Не передана комната", field="room")
        room = self._room(room_id)
        needle = _first(query, "q").lower()
        since = _epoch(_first(query, "from"))
        until = _epoch(_first(query, "to"))
        if until is not None:
            until += 86399                 # «по 30 ноября» включает этот день целиком
        records = [rec for rec in self._records(room_id) if rec.get("root_id")]
        records.sort(key=lambda rec: (int(rec.get("archived_at") or 0),
                                      int(rec.get("created_at") or 0),
                                      str(rec.get("root_id") or "")), reverse=True)
        ids = [str(rec["root_id"]) for rec in records]
        states = {}
        if ids:
            states = self.call("engine_read", "request_states",
                               ((self.cfg, room.get("board") or self.board, ids), {})) or {}
        items = []
        for rec in records:
            root_id = str(rec.get("root_id") or "")
            state = states.get(root_id) or {}
            if str(state.get("status") or "") != "archived":
                continue                   # в разделе — только то, что правда в архиве
            text = str(rec.get("text") or "")
            title = str(rec.get("title") or "")
            if needle and needle not in (title + " " + text).lower():
                continue
            created = int(rec.get("created_at") or 0)
            if since is not None and created < since:
                continue
            if until is not None and created > until:
                continue
            try:
                summary = self._summary(room, root_id, rec.get("extra_cards"),
                                        rec.get("critic"), rec.get("answers"))
            except Exception:  # noqa: BLE001 — одна битая запись не прячет остальные
                summary = self._local_summary(room, root_id, text, title,
                                              bool(rec.get("critic")),
                                              rec.get("mode") or "auto")
            self._title_view(summary, rec)
            info = archive_mod.archive_info(rec)
            summary["archived_at"] = info["archived_at"] or int(state.get("finished_at") or 0)
            summary["archived"] = True
            summary["archive_prev"] = info["prev"]
            items.append(summary)
        page = items[:PAGE_LIMIT]
        try:                               # W29-P4: «пора в архив» — мягкая подсказка
            suggest = archive_mod.suggestions(records, states)
        except Exception:                  # noqa: BLE001 - подсказка не валит список
            suggest = {"items": [], "total": 0}
        return 200, {"items": page, "total": len(items),
                     "has_more": len(items) > len(page),
                     "suggest": suggest, "auto_archive": archive_mod.auto_enabled(room),
                     "room": room_id, "at": int(time.time())}, {"Cache-Control": "no-store"}

    def h_archive_mass(self, query, body, room=None):
        """Массово в архив: `POST /api/rooms/{room}/archive/requests {root_ids}`."""
        return self._archive_many("archive", query, body, room)

    def h_archive_restore(self, query, body, room=None):
        """Массово из архива: `POST /api/rooms/{room}/archive/restore {root_ids}`."""
        return self._archive_many("restore", query, body, room)

    def _archive_many(self, action: str, query, body, room_id=None):
        """Массовое действие. Частичный сбой не отменяет остальные."""
        body = body if isinstance(body, dict) else {}
        room_id = room_id or _first(query, "room") or str(body.get("room") or "")
        if not room_id:
            raise err(400, "Не передана комната", field="room")
        room = self._room(room_id)
        raw = body.get("root_ids") if isinstance(body.get("root_ids"), list) else []
        ids = [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]
        if not ids:
            raise err(400, "Выберите хотя бы одну задачу", field="root_ids")
        if len(ids) > MASS_LIMIT:
            raise err(400, "Слишком много задач за раз: не больше %d" % MASS_LIMIT,
                      field="root_ids", limit=MASS_LIMIT, count=len(ids))
        records = {str(rec.get("root_id")): rec for rec in self._records(room_id)}
        results = []
        done = failed = 0
        for root_id in ids:
            rec = records.get(root_id)
            entry = {"root_id": root_id, "ok": False}
            if not rec:
                entry["message"] = "Задача не найдена в этой комнате"
                entry["code"] = "not_found"
                failed += 1
                results.append(entry)
                continue
            try:
                if action == "archive":
                    res = archive_mod.archive_request(self.cfg, room, rec, root_id,
                                                      by="mikhail")
                else:
                    res = archive_mod.restore_request(self.cfg, room, rec, root_id,
                                                      by="mikhail")
            except engine_write_mod.EngineWriteError as exc:
                entry["message"] = exc.message_ru
                entry["code"] = exc.code
                failed += 1
                results.append(entry)
                continue
            entry.update({"ok": True, "count": int(res.get("count") or 0),
                          "message": res.get("message") or (
                              "Запрос в архиве" if action == "archive" else "Задача возвращена")})
            if res.get("already") or (action == "restore" and not res.get("count")):
                entry["note"] = entry["message"]
            done += 1
            results.append(entry)
        return 200, {"ok": failed == 0, "action": action, "results": results,
                     "done": done, "failed": failed, "room": room_id,
                     "at": int(time.time())}, {"Cache-Control": "no-store"}


def install_archive_routes(app_cls, routes: list) -> None:
    """Подключить раздел «Архив» к ``App``: методы + строки таблицы маршрутов."""
    for name, value in vars(ArchiveRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
