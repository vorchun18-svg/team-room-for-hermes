#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team-room-submit: отправить запрос в «Комнату команды» из чата Hermes.

Навык Hermes (``docs/hermes-skill/team-room-submit/SKILL.md``). Скрипт делает
ровно два обращения к локальному приложению:

    GET  /api/rooms                     — проверить, что комната есть
    POST /api/rooms/{id}/requests       — создать запрос

Ничего не пишет в профили Hermes, не читает личные файлы, не ходит в сеть:
только ``http://127.0.0.1:<порт>``.

    python submit_request.py --room cafe --text "Сверстать меню на неделю"

Ответ — одна строка JSON:
    {"ok": true, "room": "cafe", "request_id": "t_…", "title": "…",
     "url": "http://127.0.0.1:8777/#/room/cafe/request/t_…", "duplicate": false}
    {"ok": false, "error": "…", "reason": "…", "how": "…"}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_PORT = 8777
TIMEOUT = 20
MAX_TEXT = 20000
MAX_TITLE = 120
#: Команда запуска приложения — её и подсказываем, если оно не отвечает.
HOW = "Запустите «Комнату команды» (start-team-room.cmd) и повторите."


def port_of(value=None) -> int:
    """Порт приложения: аргумент → ``TEAMROOM_PORT`` → 8777."""
    raw = value if value not in (None, "") else os.environ.get("TEAMROOM_PORT") or ""
    try:
        port = int(str(raw).strip() or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def base_url(port: int) -> str:
    return "http://127.0.0.1:%d" % int(port)


def _request(url: str, *, method: str = "GET", payload=None) -> tuple:
    """HTTP-вызов к локальному приложению: ``(статус, разобранный JSON)``."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:   # noqa: S310 - только 127.0.0.1
            body = resp.read().decode("utf-8", "replace")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:                            # 4xx/5xx с телом
        body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        status = int(getattr(exc, "code", 500) or 500)
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": "app_unavailable", "reason": str(getattr(exc, "reason", exc))[:200]}
    try:
        return status, json.loads(body or "{}")
    except ValueError:
        return status, {"error": "bad_response", "reason": (body or "")[:200]}


def rooms(port: int) -> list:
    """Список комнат (только чтение): ``[{id, title, archived}]``."""
    status, payload = _request(base_url(port) + "/api/rooms")
    if status == 0:
        raise _Fail("app_unavailable", payload.get("reason") or "приложение не отвечает")
    if status >= 400:
        raise _Fail(str(payload.get("error") or "rooms_failed"),
                    str(payload.get("message") or payload.get("reason") or status))
    items = payload.get("rooms") if isinstance(payload, dict) else payload
    return [item for item in (items or []) if isinstance(item, dict)]


def client_id_of(room: str, text: str) -> str:
    """Ключ идемпотентности: одинаковый текст в ту же комнату не создаст дубль."""
    raw = ("%s\n%s" % (room, text)).encode("utf-8")
    return "hermes-" + hashlib.sha1(raw).hexdigest()[:32]


class _Fail(Exception):
    """Отказ с машинным кодом и причиной (для строки ответа)."""

    def __init__(self, code: str, reason: str = ""):
        super().__init__(reason or code)
        self.code = code
        self.reason = reason


def submit(room: str, text: str, *, port: int = DEFAULT_PORT, title: str = "",
           client_id: str = "") -> dict:
    """Создать запрос в комнате комнаты ``room`` и вернуть результат для человека."""
    room = str(room or "").strip()
    text = str(text or "").strip()
    if not room:
        raise _Fail("bad_request", "Не указана комната: «в комнату <id>: …»")
    if not text:
        raise _Fail("bad_request", "Пустой текст запроса")
    if len(text) > MAX_TEXT:
        raise _Fail("bad_request", "Текст длиннее %d знаков" % MAX_TEXT)
    known = {str(item.get("id") or "") for item in rooms(port)}
    if room not in known:
        raise _Fail("unknown_room",
                    "комнаты «%s» нет; есть: %s" % (room, ", ".join(sorted(known)) or "ни одной"))
    payload = {"text": text, "mode": "auto",
               "client_id": client_id or client_id_of(room, text)}
    if title:
        payload["title"] = str(title).strip()[:MAX_TITLE]
    status, body = _request(base_url(port) + "/api/rooms/%s/requests" % room,
                            method="POST", payload=payload)
    if status == 0:
        raise _Fail("app_unavailable", body.get("reason") or "приложение не отвечает")
    if status >= 400:
        raise _Fail(str(body.get("error") or "request_failed"),
                    str(body.get("message") or body.get("reason") or status))
    root = str(body.get("root_id") or (body.get("request") or {}).get("id") or "")
    summary = body.get("request") if isinstance(body.get("request"), dict) else {}
    return {"ok": True, "room": room, "request_id": root,
            "title": str(summary.get("title") or title or ""),
            "url": "%s/#/room/%s/request/%s" % (base_url(port), room, root),
            "duplicate": bool(body.get("duplicate"))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Отправить запрос в «Комнату команды» (локальное приложение Hermes).")
    parser.add_argument("--room", required=True, help="id комнаты (cafe, laundry, freight, …)")
    parser.add_argument("--text", required=True, help="дословный текст запроса")
    parser.add_argument("--title", default="", help="заголовок (необязательно)")
    parser.add_argument("--port", default="", help="порт приложения (по умолчанию 8777)")
    parser.add_argument("--client-id", default="", help="ключ идемпотентности")
    args = parser.parse_args(argv)
    port = port_of(args.port)
    try:
        result = submit(args.room, args.text, port=port, title=args.title,
                        client_id=args.client_id)
    except _Fail as exc:
        print(json.dumps({"ok": False, "error": exc.code, "reason": exc.reason,
                          "how": HOW}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
