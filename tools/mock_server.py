#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Мок-сервер «Комнаты команды»: статика web/ + фикстуры + живое поведение в памяти.

    python tools/mock_server.py            # http://127.0.0.1:8778
    python tools/mock_server.py --port 8799

GET отдаёт fixtures/*.json по таблице путей; POST меняет состояние только в памяти.
Новый запрос эволюционирует queued → working → reviewing (критик) → synthesizing
(доработка) → done за ~35 секунд; без критика шаг reviewing пропускается (30 с).
Ответ после критика приходит двумя версиями (initial + revision) — см. mock_versions.
Действия decompose/revise/critique перезапускают нужную фазу.
Проверки Host/Origin — как в docs/ARCHITECTURE.md §1.7.

Публично: create_server(port=0, web_dir=None, fixtures_dir=None, phase_scale=1.0).
phase_scale < 1 ускоряет эволюцию — используется только тестами.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from mock_app import App
from mock_core import (ALLOWED_HOSTS, CRITIC_STATES, FIX_DIR, MAX_BODY, MIME, PHASES, PHASES_PLAIN,  # noqa: F401
                       WEB_DIR, ApiError, Raw, allowed_methods, inside)
import mock_stt


# --- HTTP ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TeamRoomMock/0.1"

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._send_json(405, {"error": {"code": "bad_request", "message": "OPTIONS не поддерживается", "detail": {}}},
                        {"Allow": "GET, POST, PATCH, DELETE"})

    def _dispatch(self, method: str):
        app = self.server.app
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            self._check_host()
            if method in ("POST", "PATCH", "PUT", "DELETE"):
                self._check_origin()
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            allowed = allowed_methods(path) if path.startswith("/api/") else ()
            if allowed and method not in allowed:
                raise ApiError(405, "bad_request", "Метод %s для этого пути не поддерживается" % method,
                               {"method": method, "allowed": list(allowed)})
            if method == "POST" and path == "/api/stt/transcribe":
                return self._respond(self._stt_transcribe(query))
            m = re.fullmatch(r"/api/stt/recordings/([a-z0-9][a-z0-9_-]{5,40})/parts/(\d{1,3})", path)
            if m and method in ("PUT", "POST"):
                return self._respond(self._stt_part_save(m.group(1), int(m.group(2))))
            if method == "GET":
                body = app.get(path, query)
                return self._respond(body)
            payload = self._read_json() if method in ("POST", "PATCH", "PUT") else {}
            if method == "POST":
                result = app.post(path, query, payload)
            elif method == "PUT":
                result = app.put(path, query, payload)
            elif method == "PATCH":
                result = app.patch(path, query, payload)
            else:
                result = app.delete(path, query, payload)
            return self._respond(result)
        except ApiError as exc:
            self.close_connection = True
            headers = {"Allow": ", ".join(exc.detail["allowed"])} if exc.status == 405 and exc.detail.get("allowed") else None
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message,
                                                   "detail": exc.detail or {}}}, headers)
        except Exception as exc:  # noqa: BLE001 — любой сбой должен дать JSON, а не оборванный ответ
            self.close_connection = True
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "engine_error", "message": "Внутренняя ошибка мока",
                                           "detail": {"error": str(exc)[:200]}}})

    def _check_host(self):
        host = self.headers.get("Host", "")
        name, _, port = host.rpartition(":")
        if not name:
            name, port = host, ""
        if name.lower() not in ALLOWED_HOSTS or (port and port != str(self.server.server_port)):
            raise ApiError(403, "forbidden", "Неверный заголовок Host", {"reason": "bad_host", "host": host})

    def _check_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return
        part = urlsplit(origin)
        if part.scheme != "http" or (part.hostname or "").lower() not in ALLOWED_HOSTS \
                or (part.port or 80) != self.server.server_port:
            raise ApiError(403, "forbidden", "Запрос с чужого Origin", {"reason": "bad_origin", "origin": origin})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "Некорректный Content-Length", {})
        if length > MAX_BODY:
            raise ApiError(413, "bad_request", "Тело запроса больше 8 МиБ", {"length": length, "max": MAX_BODY})
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "bad_request", "Тело не является JSON", {"error": str(exc)[:200]})
        if not isinstance(data, dict):
            if data is None:          # `null` в теле = пустой объект (PUT …/model — сброс)
                return {}
            raise ApiError(400, "bad_request", "Тело должно быть JSON-объектом", {})
        return data

    def _stt_transcribe(self, query: dict) -> tuple:
        """Тело — сырые байты аудио (лимит 64 МиБ только для этого маршрута)."""
        mock_stt.ensure_available()          # нет движка → 503 до чтения тела
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in mock_stt.ALLOWED_AUDIO:
            raise ApiError(400, "bad_request", "Формат аудио не поддерживается",
                           {"content_type": ctype, "allowed": list(mock_stt.ALLOWED_AUDIO)})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "Некорректный Content-Length", {})
        if length <= 0:
            raise ApiError(400, "audio_empty", "Аудио не получено", {})
        if length > mock_stt.STT_MAX_BODY:
            raise ApiError(413, "audio_too_big", "Аудио больше 64 МиБ — сократите запись",
                           {"length": length, "max": mock_stt.STT_MAX_BODY})
        audio = self.rfile.read(length)
        if len(audio) != length:
            raise ApiError(400, "audio_empty", "Аудио не получено", {"got": len(audio), "length": length})
        language = (query.get("language", [""])[0] or "").strip() or None
        return 200, mock_stt.transcribe(audio, ctype, language)

    def _stt_part_save(self, rid: str, part: int) -> tuple:
        """PUT/POST /api/stt/recordings/{rid}/parts/{n}: тело — байты части.

        Демо хранит части в памяти: диск не трогаем (как и весь мок).
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in mock_stt.ALLOWED_AUDIO:
            raise ApiError(400, "bad_request", "Формат аудио не поддерживается",
                           {"content_type": ctype, "allowed": list(mock_stt.ALLOWED_AUDIO)})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "Некорректный Content-Length", {})
        if length <= 0:
            raise ApiError(400, "audio_empty", "Пустая часть записи", {})
        if length > mock_stt.STT_MAX_BODY:
            raise ApiError(413, "audio_too_big", "Часть записи больше 64 МиБ",
                           {"length": length, "max": mock_stt.STT_MAX_BODY})
        audio = self.rfile.read(length)
        return 200, mock_stt.save_part(rid, part, audio, ctype)

    def _respond(self, result):
        status, payload = result
        if isinstance(payload, Raw):
            return self._send_payload(status, payload.body, payload.ctype, payload.headers)
        headers = {}
        if status == 201 and isinstance(payload, dict):
            if payload.get("root_id"):
                headers["Location"] = "/api/requests/" + payload["root_id"]
            elif payload.get("id"):
                headers["Location"] = "/api/rooms/" + str(payload["id"])
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._send_payload(status, body, "application/json; charset=utf-8", headers)

    def _send_json(self, status, obj, headers=None):
        return self._send_payload(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8", headers or {})

    def _send_payload(self, status, payload, ctype, headers=None):
        headers = dict(headers or {})
        etag = 'W/"%s"' % hashlib.sha1(payload).hexdigest()[:16]
        if self.command == "GET" and ctype.startswith("application/json") and status == 200:
            inm = self.headers.get("If-None-Match", "")
            tags = [re.sub(r"^W/", "", t.strip()).strip('"') for t in inm.split(",") if t.strip()]
            if "*" in tags or re.sub(r"^W/", "", etag).strip('"') in tags:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            headers.setdefault("ETag", etag)
            headers.setdefault("Cache-Control", "no-cache")
        elif ctype.startswith("application/json"):
            headers.setdefault("Cache-Control", "no-store")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _static(self, path: str):
        rel = posixpath.normpath(path).lstrip("/")
        if rel in ("", "."):
            rel = "index.html"
        target = os.path.realpath(os.path.join(self.server.app.web_dir, rel))
        web = os.path.realpath(self.server.app.web_dir)
        if not inside(web, target):
            raise ApiError(403, "forbidden", "Путь вне web/", {"path": path})
        if not os.path.isfile(target) and "." not in os.path.basename(rel):
            target = os.path.join(web, "index.html")
        if not os.path.isfile(target):
            if not os.path.isfile(os.path.join(web, "index.html")):
                return self._placeholder()
            raise ApiError(404, "not_found", "Файл не найден", {"path": path})
        with open(target, "rb") as fh:
            payload = fh.read()
        ctype = MIME.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        self._send_payload(200, payload, ctype, {"Cache-Control": "no-cache"})

    def _placeholder(self):
        body = ("<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\"><title>Комната команды — мок</title>"
                "<style>body{font:15px/1.6 system-ui,sans-serif;max-width:720px;margin:60px auto;padding:0 20px}"
                "code{background:#f2f2f2;padding:2px 6px;border-radius:4px}</style>"
                "<h1>Мок-сервер «Комнаты команды»</h1>"
                "<p>Каталог <code>web/</code> ещё не собран — интерфейс появится, когда фронтенд положит "
                "<code>web/index.html</code>. API уже живое.</p>"
                "<ul><li><a href=\"/api/health\">/api/health</a></li>"
                "<li><a href=\"/api/rooms\">/api/rooms</a></li>"
                "<li><a href=\"/api/rooms/cafe/requests\">/api/rooms/cafe/requests</a></li>"
                "<li><a href=\"/api/rooms/cafe/candidates\">/api/rooms/cafe/candidates</a></li>"
                "<li><a href=\"/api/requests/t_a1b2c3d4\">/api/requests/t_a1b2c3d4</a></li>"
                "<li><a href=\"/api/requests/t_c4d2e118\">/api/requests/t_c4d2e118</a></li>"
                "<li><a href=\"/api/rooms/fn/requests\">/api/rooms/fn/requests</a></li>"
                "<li><a href=\"/api/rooms/cafe/backlog\">/api/rooms/cafe/backlog</a></li>"
                "<li><a href=\"/api/reports/cafe/unit-economics\">/api/reports/cafe/unit-economics</a></li></ul>"
                "<p>Фикстуры: <code>fixtures/*.json</code>. Порт по умолчанию — 8778.</p></body></html>")
        self._send_payload(200, body.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[mock] %s %s\n" % (self.address_string(), fmt % args))


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def port_in_use(host: str, port: int) -> bool:
    """Занят ли уже порт (STAB-3: на Windows SO_REUSEADDR даёт тихую «двойную привязку»).

    `allow_reuse_address=True` на Windows означает НЕ «переиспользовать TIME_WAIT»,
    а разрешение второму сокету встать на тот же порт: оба слушают, Windows отдаёт
    соединение любому из них. Из-за этого прогон, у которого на порту остался
    прежний/чужой мок, отвечал то «своим», то «чужим» состоянием (наблюдение:
    «Комната с таким id уже есть» при создании комнаты в чистом прогоне).
    Поэтому проверяем порт заранее и падаем с понятным текстом, а не тихо врём.
    """
    if not port:
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.35)
    try:
        return probe.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        probe.close()


def create_server(port: int = 0, web_dir: str | None = None, fixtures_dir: str | None = None,
                  phase_scale: float = 1.0, verbose: bool = False, host: str = "127.0.0.1",
                  extra_roots: list | None = None) -> MockServer:
    """Поднять мок на порту (0 — свободный). Возвращает сервер с .app.

    host — только loopback (main() проверяет то же); extra_roots — дополнительные
    разрешённые корни для /api/file (нужно тестам, чтобы не писать в репозиторий).
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("мок слушает только loopback")
    if port_in_use(host, port):
        # Иначе на Windows второй сокет тихо встанет на тот же порт и браузер будет
        # попадать то в «наш» мок, то в чужой — прогон станет неповторяемым.
        raise RuntimeError(
            "порт %d уже занят — его слушает другой сервер (мок, приложение или "
            "остаток прежнего прогона). Возьмите свободный порт: --port 0 или другой номер."
            % port)
    app = App(web_dir or WEB_DIR, fixtures_dir or FIX_DIR, phase_scale=phase_scale, port=port,
              extra_roots=extra_roots)
    server = MockServer((host, port), Handler)
    server.app = app
    server.verbose = verbose
    app.port = server.server_address[1]
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Мок-сервер «Комнаты команды»")
    ap.add_argument("--port", type=int, default=8778, help="порт (по умолчанию 8778)")
    ap.add_argument("--host", default="127.0.0.1", help="адрес (только loopback)")
    ap.add_argument("--web", default=WEB_DIR, help="каталог статики")
    ap.add_argument("--fixtures", default=FIX_DIR, help="каталог фикстур")
    ap.add_argument("--phase-scale", type=float, default=1.0, help="ускорение эволюции (тесты)")
    ap.add_argument("--pid-file", default=None, help="записать PID сервера в файл (для stop-team-room.cmd)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Отказ: мок слушает только loopback", file=sys.stderr)
        return 2
    if args.pid_file:
        pid_path = os.path.abspath(args.pid_file)
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        with open(pid_path, "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))

        def _remove_pid() -> None:
            try:
                with open(pid_path, encoding="ascii") as fh:
                    same = fh.read().strip() == str(os.getpid())
                if same:
                    os.remove(pid_path)
            except OSError:
                pass

        atexit.register(_remove_pid)
    try:
        server = create_server(args.port, args.web, args.fixtures, args.phase_scale, args.verbose, args.host)
    except RuntimeError as exc:            # STAB-3: занятый порт — без трейсбека
        print("Отказ: %s" % exc, file=sys.stderr)
        return 2
    url = "http://127.0.0.1:%d" % server.server_address[1]
    print("Мок «Комнаты команды»: %s  (Ctrl+C — остановить)" % url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
