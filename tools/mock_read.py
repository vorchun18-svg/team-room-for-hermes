# -*- coding: utf-8 -*-
"""GET-обработчики мок-сервера (чтение фикстур и живых запросов)."""
from __future__ import annotations

import copy
import mimetypes
import os
import re
import time
from urllib.parse import quote, unquote

from mock_core import (ALLOWED_EXT, DENY_NAME_PARTS, FILE_ROOTS, MIME, MOCK, MOCK_PRIVATE, OFFICE_EXT, PHASES,
                       ApiError, Raw, backlog_counts, child_id, export_html, export_markdown,
                       ics_escape, inside, now)
import mock_demo as demo
import mock_stt

class MockRead:
    def get(self, path: str, query: dict) -> tuple:
        if path == "/api/health":
            return 200, self.health()
        if path == "/api/stt/status":
            return 200, mock_stt.status()
        if path == "/api/stt/dictionary":
            return 200, mock_stt.get_dictionary()
        if path == "/api/config":
            return 200, self.fixture("config.json")
        if path == "/api/models":
            return 200, self.catalog()               # каталог моделей (WAVE10 §19)
        if path == "/api/profiles":
            return 200, self.fixture("profiles.json")
        if path == "/api/rooms":
            include = query.get("include_archived", ["0"])[0] in ("1", "true", "yes")
            rooms = [self.room_summary(rid) for rid in self.room_details if include or not self.room(rid)["archived"]]
            return 200, {"rooms": rooms, "engine": {"ok": True, "board": "team", "warning": None},
                         "updated_at": now()}
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/plan", path)
        if m:
            return 200, self.get_plan(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})", path)
        if m:
            summary = self.room_summary(m.group(1))
            return 200, {**self.with_models(self.room(m.group(1))), "counters": summary["counters"],
                         "phase": summary["phase"],
                         "dispatcher": {"ok": True, "state": "running", "warning": None}}
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/requests", path)
        if m:
            return 200, self.list_requests(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/candidates", path)
        if m:
            return 200, self.candidates_for(m.group(1))
        m = re.fullmatch(r"/api/requests/(t_[0-9a-f]{8})", path)
        if m:
            return 200, self.detail(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/team", path)
        if m:
            return 200, self.team(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/roadmap", path)
        if m:
            return 200, self.roadmap(m.group(1))
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/backlog", path)
        if m:
            return 200, self.backlog_for(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/journal", path)
        if m:
            return 200, self.journal(m.group(1), query)
        m = re.fullmatch(r"/api/rooms/([a-z0-9-]{2,32})/reports", path)
        if m:
            room = self.room(m.group(1))
            items = copy.deepcopy(self.fixture("reports_cafe.json")["items"]) if room["id"] == "cafe" else []
            for rid, req in self.live.items():
                if req.get("room") != room["id"]:
                    continue
                detail = self.live_detail(rid)
                if (detail.get("synthesis") or {}).get("state") != "ready":
                    continue
                ref = detail.get("report") or {}
                items.insert(0, {"key": ref.get("key") or demo.report_key(rid),
                                 "title": ref.get("title") or "Разбор запроса",
                                 "subtitle": "Разбор живого запроса · демо",
                                 "kind": "spec", "path": None, "request_id": rid,
                                 "blocks": 9, "created_at": detail["created_at"],
                                 "updated_at": detail["created_at"] + 35, "exists": True})
            return 200, {"room": room["id"], "items": items}
        m = re.fullmatch(r"/api/reports/([a-z0-9-]{2,32})/([a-z0-9-]{2,64})", path)
        if m:
            room, key = m.group(1), m.group(2)
            self.room(room)
            if key == "unit-economics":
                return 200, self.fixture("report_unit_economics.json")
            if key.startswith("demo-"):
                rid = "t_" + key[len("demo-"):]
                if re.fullmatch(r"t_[0-9a-f]{8}", rid) and rid in self.live:
                    detail = self.detail(rid)
                    if (detail.get("synthesis") or {}).get("state") != "ready":
                        raise ApiError(409, "conflict", "Ответ ещё не готов — отчёт появится после свода",
                                       {"reason": "synthesis_pending", "rid": rid})
                    return 200, demo.report_spec(detail, detail.get("text") or "")
            raise ApiError(404, "not_found", "Отчёт не найден", {"room": room, "key": key})
        if path == "/api/search":
            return 200, self.search(query)
        if path == "/api/ics":
            return self.ics(query)
        m = re.fullmatch(r"/api/export/request/(t_[0-9a-f]{8})\.(md|html)", path)
        if m:
            return self.export(m.group(1), m.group(2))
        if path == "/api/file":
            return self.file_response(query)
        raise ApiError(404, "not_found", "Неизвестный эндпоинт", {"path": path})

    def health(self) -> dict:
        data = self.fixture("health.json")
        data["app"]["port"] = self.port
        data["app"]["started_at"] = self.started_at
        data["app"]["uptime_s"] = now() - self.started_at
        data.setdefault("engine", {})["mock"] = MOCK
        data["data"]["rooms"] = len(self.rooms)
        data["data"]["requests"] = len(self.requests) + len(self.live)
        return data

    def list_requests(self, room_id: str, query: dict) -> dict:
        self.room(room_id)
        items = self.summaries(room_id)
        statuses = [s for s in query.get("status", [""])[0].split(",") if s]
        known = {"queued", "working", "reviewing", "synthesizing", "done", "needs_you", "failed"}
        bad = [s for s in statuses if s not in known]
        if bad:
            raise ApiError(400, "bad_request", "Неизвестный статус запроса",
                           {"status": bad[0], "allowed": sorted(known)})
        if statuses:
            items = [s for s in items if s["status"] in statuses]
        q = query.get("q", [""])[0].strip().casefold()
        if q:
            items = [s for s in items if q in s["title"].casefold() or q in s["preview"].casefold()]
        try:
            limit = int(query.get("limit", ["20"])[0])
        except ValueError:
            raise ApiError(400, "bad_request", "limit должен быть числом", {"field": "limit"})
        if not 1 <= limit <= 100:
            raise ApiError(400, "bad_request", "limit вне диапазона 1..100", {"limit": limit})
        return {"items": items[:limit], "next_cursor": None, "has_more": len(items) > limit}

    def candidates_for(self, room_id: str) -> dict:
        """Кандидаты на привязку: карточки с decomposed вне реестра запросов комнаты."""
        self.room(room_id)
        data = self.candidates.get(room_id)
        if not data:
            return {"room": room_id, "generated_at": now(), "count": 0, "items": []}
        items = copy.deepcopy(data["items"])
        return {"room": room_id, "generated_at": data.get("generated_at") or now(),
                "count": len(items), "items": items}

    def team(self, room_id: str) -> dict:
        room = self.room(room_id)
        if room["id"] not in ("cafe", "fn"):
            members = [{"profile": m["profile"], "title": m["profile"], "role": m["role"], "note": m["note"],
                        "pinned": m["pinned"], "state": "idle", "current": None, "last": None,
                        "counts": {"done": 0, "blocked": 0, "active": 0}} for m in room["members"]]
            return {"room": room["id"], "generated_at": now(),
                    "dispatcher": {"ok": True, "state": "running", "pid": 9608, "warning": None},
                    "members": members}
        data = self.fixture("team_%s.json" % room["id"])
        data["generated_at"] = now()
        return data

    def roadmap(self, room_id: str) -> dict:
        room = self.room(room_id)
        if room["id"] == "cafe":
            return self.fixture("roadmap_cafe.json")
        ids = ["idea", "legal", "brand", "packaging", "production", "sales"]
        titles = ["Идея и стратегия", "Легализация", "Бренд и товарный знак", "Упаковка", "Производство", "Продажи"]
        statuses = {"legal": "active", "production": "blocked"} if room["id"] == "fn" else {}
        return {"room": room["id"], "updated_at": now(),
                "current": {"id": "legal", "title": "Легализация", "status": "active"} if room["id"] == "fn" else None,
                "phases": [{"id": i, "title": t, "status": statuses.get(i, "next"), "note": ""}
                           for i, t in zip(ids, titles)],
                "decisions": []}

    def backlog_for(self, room_id: str, query: dict) -> dict:
        self.room(room_id)
        data = self.backlog_data(room_id)          # + недостающие шаги из всех запросов комнаты
        items = copy.deepcopy(data["items"])
        statuses = [s for s in query.get("status", [""])[0].split(",") if s]
        kinds = [s for s in query.get("kind", [""])[0].split(",") if s]
        if statuses:
            items = [i for i in items if i["status"] in statuses]
        if kinds:
            items = [i for i in items if i["kind"] in kinds]
        return {"room": room_id, "updated_at": data["updated_at"], "counts": backlog_counts(items), "items": items}

    def journal(self, room_id: str, query: dict) -> dict:
        self.room(room_id)
        data = self.fixture("journal_cafe.json") if room_id == "cafe" else {
            "room": room_id, "next_cursor": None, "has_more": False, "items": []}
        q = query.get("q", [""])[0].strip().casefold()
        events = self.models_journal.get(room_id, []) + data["items"]   # «модель изменена» — сверху
        items = [e for e in events if not q or q in e["text"].casefold()]
        try:
            limit = int(query.get("limit", ["50"])[0])
        except ValueError:
            raise ApiError(400, "bad_request", "limit должен быть числом", {"field": "limit"})
        if not 1 <= limit <= 200:
            raise ApiError(400, "bad_request", "limit вне диапазона 1..200", {"limit": limit})
        return {"room": room_id, "items": items[:limit], "next_cursor": None, "has_more": len(items) > limit}

    def search(self, query: dict) -> dict:
        q = query.get("q", [""])[0].strip()
        if len(q) < 2:
            raise ApiError(400, "bad_request", "Поисковый запрос короче 2 символов", {"field": "q"})
        room = query.get("room", [""])[0] or None
        if room and room not in {r["id"] for r in self.rooms}:
            raise ApiError(404, "not_found", "Комната не найдена", {"room": room})
        data = self.fixture("search.json")
        items = [h for h in data["items"] if (not room or h["room"] == room)
                 and (q.casefold() in h["title"].casefold() or q.casefold() in h["snippet"].casefold())]
        return {"q": q, "room": room, "total": len(items), "items": items}

    def ics(self, query: dict) -> tuple:
        room_id = query.get("room", [""])[0]
        if not room_id:
            raise ApiError(400, "bad_request", "Нужен параметр room", {"field": "room"})
        self.room(room_id)
        raw = query.get("items", [""])[0]
        ids = [unquote(x) for x in raw.split(",") if x]
        all_flag = query.get("all", ["0"])[0] in ("1", "true", "yes")
        if not ids and not all_flag:
            raise ApiError(400, "bad_request", "Укажите items=<id,...> или all=1", {"field": "items"})
        items = self.backlog.get(room_id, {"items": []})["items"]
        chosen = [i for i in items if (i["id"] in ids or (all_flag and i["status"] == "open" and i["due"]))]
        if ids and not chosen:
            raise ApiError(404, "not_found", "Элементы бэклога не найдены", {"items": ids})
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Team Room//Mock 0.1//RU",
                 "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
        for it in chosen:
            day = it["due"].replace("-", "")
            lines += ["BEGIN:VEVENT", "UID:%s-%s@team-room.local" % (room_id, re.sub(r"[^\w:-]", "_", it["id"])),
                      "DTSTAMP:%sZ" % time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
                      "DTSTART;VALUE=DATE:%s" % day, "SUMMARY:%s" % ics_escape(it["text"][:200]),
                      "DESCRIPTION:%s" % ics_escape("Владелец: %s. Источник: %s" % (it.get("owner") or "—", it["source_title"])),
                      "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return 200, Raw("\r\n".join(lines) + "\r\n", "text/calendar; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="%s.ics"' % room_id})

    def export(self, rid: str, fmt: str) -> tuple:
        detail = self.detail(rid)
        if detail.get("synthesis", {}).get("state") != "ready":
            raise ApiError(409, "conflict", "Ответ ещё не готов — экспортировать нечего",
                           {"reason": "synthesis_pending", "rid": rid})
        md = export_markdown(detail)
        filename = "%s.%s" % (rid, fmt)
        if fmt == "md":
            return 200, Raw(md, "text/markdown; charset=utf-8",
                            {"Content-Disposition": 'attachment; filename="%s"' % filename})
        page = export_html(detail, md)
        return 200, Raw(page, "text/html; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="%s"' % filename})

    def resolve_shared_path(self, raw: str) -> str:
        """Путь из белого списка API §2: 5 корней + расширение + чёрный список имён."""
        path = os.path.realpath(raw)
        roots = list(FILE_ROOTS) + list(getattr(self, "extra_roots", []) or [])
        if any(inside(private, path) for private in MOCK_PRIVATE):
            raise ApiError(403, "forbidden", "Исходники и фикстуры мока не отдаются",
                           {"reason": "outside_roots", "path": raw})
        if not any(inside(root, path) for root in roots if root):
            raise ApiError(403, "forbidden", "Файл вне разрешённых корней",
                           {"reason": "outside_roots", "path": raw})
        low = os.path.basename(path).lower()
        parts = [p.lower() for p in path.split(os.sep)[-4:]]
        if any(bad in part for part in parts for bad in DENY_NAME_PARTS) or \
                any(bad in low for bad in DENY_NAME_PARTS):
            raise ApiError(403, "forbidden", "Файл в чёрном списке", {"reason": "blacklisted", "path": raw})
        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_EXT:
            raise ApiError(403, "forbidden", "Расширение файла не разрешено", {"reason": "outside_roots",
                                                                              "path": raw, "ext": ext})
        if not os.path.isfile(path):
            raise ApiError(404, "not_found", "Файл не найден", {"path": raw})
        return path

    def file_response(self, query: dict) -> tuple:
        raw = query.get("path", [""])[0]
        if not raw:
            raise ApiError(400, "bad_request", "Нужен параметр path", {"field": "path"})
        path = self.resolve_shared_path(raw)
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext, mimetypes.guess_type(path)[0] or "application/octet-stream")
        name = os.path.basename(path)
        if ext in OFFICE_EXT:
            # RFC 5987: кириллица в имени не влезает в latin-1 заголовок
            disp = ('attachment; filename="%s"' % name if name.isascii() else
                    "attachment; filename=\"file%s\"; filename*=UTF-8''%s" % (ext, quote(name)))
        else:
            disp = "inline"
        headers = {"Content-Disposition": disp}
        if ext == ".svg":
            headers["Content-Security-Policy"] = "sandbox"   # API §6.6 / §2
        with open(path, "rb") as fh:
            body = fh.read()
        return 200, Raw(body, ctype, headers)

    # --- POST -------------------------------------------------------------

