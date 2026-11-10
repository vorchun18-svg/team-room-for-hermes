"""HTTP-слой «Комнаты команды»: ошибки API, JSON/ETag, Host/Origin, статика, журнал.

Модуль ничего не знает о домене: обработчик запросов отдаёт ``App.dispatch()``
из ``server/app.py``. Здесь — транспорт, заголовки безопасности и сериализация.
"""
from __future__ import annotations

import errno
import hashlib
import json
import mimetypes
import posixpath
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

MAX_BODY = 8 * 1024 * 1024           # предел тела JSON-запроса (8 МиБ)
BODY_POLICY = {"limit": MAX_BODY, "binary": False, "code": None, "message": None}
WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
APP_PREFIXES = ("/api/", "/reports/")   # пути приложения, не статика
JSON_CT = "application/json; charset=utf-8"

# «Клиент ушёл» — не ошибка сервера: закрываем соединение молча.
# Windows отдаёт ConnectionAbortedError (WinError 10053) вместо ConnectionResetError.
CONNECTION_ERRORS = (ConnectionError, ConnectionAbortedError, ConnectionResetError,
                     BrokenPipeError, TimeoutError)
# Те же обрывы, но как «голые» OSError (разные ОС): WSAECONNABORTED/RESET/ESHUTDOWN
DISCONNECT_WINERRORS = (10053, 10054, 10058)
DISCONNECT_ERRNOS = (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ESHUTDOWN)


def is_client_disconnect(exc: BaseException | None) -> bool:
    """True, если клиент (браузер) оборвал соединение: сброс/обрыв/закрытый канал."""
    if exc is None:
        return False
    if isinstance(exc, CONNECTION_ERRORS):
        return True
    return isinstance(exc, OSError) and (getattr(exc, "winerror", None) in DISCONNECT_WINERRORS
                                         or exc.errno in DISCONNECT_ERRNOS)


def has_nul(value, depth: int = 0) -> bool:
    """NUL-байт в строке/ключе тела запроса (D12: иначе ValueError из subprocess → 500)."""
    if depth > 40:
        return False
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(has_nul(key, depth + 1) or has_nul(item, depth + 1)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_nul(item, depth + 1) for item in value)
    return False


# CSP страниц приложения (web/index.html и вкладки SPA)
APP_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
           "frame-src 'self'; connect-src 'self'")
# CSP пользовательского/агентского HTML (sandbox-iframe, отчёты)
HTML_CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'")

MIME = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff",
    ".ttf": "font/ttf", ".otf": "font/otf", ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".pdf": "application/pdf", ".csv": "text/csv; charset=utf-8", ".ics": "text/calendar; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

STATUS_CODE = {400: "bad_request", 403: "forbidden", 404: "not_found", 405: "bad_request",
               409: "conflict", 411: "bad_request", 413: "bad_request", 415: "bad_request",
               500: "engine_error", 501: "engine_error", 502: "engine_error", 503: "engine_busy"}

# --- секреты в журналах (D13) ----------------------------------------------
# Маскируем и то, что печатает приложение, и то, что попало в лог из argv/ошибок CLI.
_SECRET_PAIR = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|api[_-]?secret|access[_-]?key)\b"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&|]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{8,})")


def mask_secrets(text) -> str:
    """Скрыть значения секретов в строке лога: ``password=hunter2`` → ``password=***``.

    Обрабатывает ``password|passwd|pwd|token|secret|api_key|api-key|api secret|access_key``
    (через ``=`` или ``:``), а также ``Bearer <токен>``.
    """
    if not text:
        return text if text is not None else ""
    out = _BEARER.sub(lambda m: m.group(1) + " ***", str(text))
    return _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + "***", out)


class ApiError(Exception):
    """Ошибка API: HTTP-статус + русское сообщение + машинный detail."""

    def __init__(self, status: int, message: str, code: str | None = None, detail: dict | None = None):
        super().__init__(message)
        self.status = int(status)
        self.code = code or STATUS_CODE.get(self.status, "engine_error")
        self.message = message
        self.detail = dict(detail or {})

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


def err(status: int, message: str, code: str | None = None, **detail) -> ApiError:
    """Короткий конструктор ApiError: ``raise err(404, "Нет комнаты", room=rid)``."""
    return ApiError(status, message, code=code, detail=detail)


class Raw:
    """Не-JSON ответ (файл, .ics, экспорт): байты + тип + дополнительные заголовки."""

    def __init__(self, body, ctype: str, headers: dict | None = None, status: int | None = None):
        self.body = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
        self.ctype = ctype
        self.headers = dict(headers or {})
        self.status = status


class RawBody:
    """Тело бинарного маршрута: байты + Content-Type, без разбора JSON.

    Так приходит аудио диктовки (``POST /api/stt/transcribe``): 64 МиБ opus —
    это не JSON, и ``json.loads`` на нём дал бы 400 вместо распознавания.
    """

    __slots__ = ("data", "content_type")

    def __init__(self, data: bytes, content_type: str = ""):
        self.data = bytes(data or b"")
        self.content_type = str(content_type or "")

    def __len__(self) -> int:
        return len(self.data)

    def __repr__(self) -> str:
        return "RawBody(%d байт, %s)" % (len(self.data), self.content_type or "?")


def body_policy(path: str | None = None) -> dict:
    """Политика тела по умолчанию: JSON до 8 МиБ, не бинарный.

    Маршрут может переопределить её через ``App.body_policy(path)`` — так
    ``/api/stt/transcribe`` получает свои 64 МиБ и код ``audio_too_big``.
    """
    return dict(BODY_POLICY)


def human_size(size: int) -> str:
    """``8388608`` → ``8 МиБ`` (для русских сообщений об ошибке)."""
    mib = int(size) / (1024 * 1024)
    text = ("%.1f" % mib).rstrip("0").rstrip(".")
    return "%s МиБ" % (text or "0")


# --- журнал ---------------------------------------------------------------
class AppLog:
    """Потокобезопасный журнал в data/logs/app.log (с ротацией по 5 МиБ)."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._rotate()

    def _rotate(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > 5 * 1024 * 1024:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
        except OSError:
            pass

    def write(self, level: str, message: str, exc: BaseException | None = None) -> None:
        line = "%s %-7s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, str(message).replace("\n", " "))
        if exc is not None:
            line += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        line = mask_secrets(line)          # D13: значения секретов в журнал не попадают
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", errors="replace") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def info(self, message: str) -> None:
        self.write("INFO", message)

    def warning(self, message: str, exc: BaseException | None = None) -> None:
        self.write("WARN", message, exc)

    def error(self, message: str, exc: BaseException | None = None) -> None:
        self.write("ERROR", message, exc)


# --- помощники ------------------------------------------------------------
def json_bytes(obj) -> bytes:
    """JSON UTF-8 без BOM и без ASCII-экранирования."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def etag_of(payload: bytes) -> str:
    """Слабый ETag ``W/"<16 hex>"`` по байтам тела."""
    return 'W/"%s"' % hashlib.sha1(payload).hexdigest()[:16]


def _norm_tag(tag: str) -> str:
    tag = tag.strip()
    if tag[:2] in ("W/", "w/"):
        tag = tag[2:].strip()
    return tag.strip('"').strip("'")


def inm_matches(header: str | None, etag: str) -> bool:
    """If-None-Match: точное значение, список через запятую или ``*`` (W/ игнорируется)."""
    if not header:
        return False
    mine = _norm_tag(etag)
    for part in header.split(","):
        if part.strip() == "*":
            return True
        if _norm_tag(part) == mine:
            return True
    return False


def host_ok(host: str | None, port: int) -> bool:
    """Host обязан быть 127.0.0.1:PORT, localhost:PORT или [::1]:PORT."""
    if not host:
        return False
    name, sep, tail = str(host).strip().rpartition(":")
    if not sep:
        name, tail = str(host).strip(), ""
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name.strip().lower() not in LOOPBACK:
        return False
    return tail.strip() == str(port)


def origin_ok(value: str | None, port: int) -> bool:
    """Origin/Referer должен быть http://127.0.0.1:PORT | http://localhost:PORT."""
    if not value:
        return True
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme != "http":
        return False
    if (parts.hostname or "").lower() not in LOOPBACK:
        return False
    try:
        origin_port = parts.port      # D10: порт «99999» → ValueError, а не 500
    except ValueError:
        return False
    return (origin_port or 80) == int(port)


def security_headers() -> dict:
    return {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "SAMEORIGIN"}


def local_tz_name() -> str:
    """Имя пояса сервера; при отсутствии IANA-имени — смещение вида UTC-03:00."""
    try:
        name = time.strftime("%Z")
        if name and not name.startswith(("+", "-")) and name not in ("UTC", "GMT"):
            return name
    except Exception:  # noqa: BLE001 - имя пояса не критично
        pass
    offset = -time.altzone if time.localtime().tm_isdst else -time.timezone
    sign = "+" if offset >= 0 else "-"
    offset = abs(int(offset))
    return "UTC%s%02d:%02d" % (sign, offset // 3600, (offset % 3600) // 60)


def serve_static(web_dir, raw_path: str):
    """Отдать файл из web/. Возвращает (status, bytes, ctype, headers) или бросает ApiError."""
    rel = unquote(raw_path or "/")
    if any(part == ".." for part in rel.replace("\\", "/").split("/")):
        raise err(403, "Путь вне web/", path=raw_path)
    rel = posixpath.normpath("/" + rel.lstrip("/")).lstrip("/")
    if rel in ("", "."):
        rel = "index.html"
    web = Path(web_dir).resolve()
    target = (web / rel).resolve()
    try:
        target.relative_to(web)
    except ValueError:
        raise err(403, "Путь вне web/", path=raw_path) from None
    if target.is_dir():
        target = (target / "index.html").resolve()
    if not target.is_file() and "." not in Path(rel).name:
        target = web / "index.html"          # SPA: любой «бесточечный» путь отдаёт index.html
    if not target.is_file():
        raise err(404, "Файл не найден", path=raw_path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise err(404, "Файл не найден", path=raw_path) from exc
    ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    headers = {"Cache-Control": "no-cache"}
    if ctype.startswith("text/html"):
        headers["Content-Security-Policy"] = APP_CSP
    return 200, data, ctype, headers


# --- HTTP-обработчик ------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    """Тонкий транспортный слой: проверки безопасности → ``app.dispatch`` → ответ."""

    protocol_version = "HTTP/1.1"
    server_version = "TeamRoom/0.1"
    # D11: медленный клиент не держит поток бесконечно (заголовки + тело + паузы)
    timeout = 30

    # BaseHTTPRequestHandler рисует свою версию; свой лог не нужен
    def log_message(self, fmt, *args):  # noqa: A003 - API базового класса
        return

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def do_OPTIONS(self):
        self.close_connection = True
        self._send_error(ApiError(405, "OPTIONS не поддерживается", detail={"method": "OPTIONS"}))

    # --- маршрутизация одного запроса ---
    def _handle(self, method: str) -> None:
        app = getattr(self.server, "http_app", None)
        try:
            self._check_host()
            if method in WRITE_METHODS:
                self._check_origin()
            parts = urlsplit(self.path)
            raw_path = parts.path or "/"
            query = parse_qs(parts.query, keep_blank_values=True)
            if method in ("GET", "HEAD") and not raw_path.startswith(APP_PREFIXES):
                status, payload, ctype, headers = serve_static(app.cfg.web_dir, raw_path)
                return self._send(status, payload, ctype, headers)
            body = self._read_body(self._policy(app, raw_path)) \
                if method in ("POST", "PATCH", "PUT") else {}
            status, payload, headers = app.dispatch("GET" if method == "HEAD" else method, raw_path, query, body)
            return self._send_result(status, payload, headers)
        except ApiError as exc:
            self.close_connection = True
            return self._send_error(exc)
        except CONNECTION_ERRORS:
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - наружу без трейсбека
            self.close_connection = True
            mapped = None
            if app is not None:
                app.log.error("необработанное исключение %s %s" % (method, self.path), exc)
                convert = getattr(app, "api_error_from", None)
                if callable(convert):
                    mapped = convert(exc)      # D6: EngineError → 503, а не 500
            return self._send_error(mapped or ApiError(500, "Внутренняя ошибка сервера", detail={}))

    def _check_host(self) -> None:
        host = self.headers.get("Host")
        port = self.server.server_address[1]
        if not host_ok(host, port):
            raise ApiError(403, "Неверный заголовок Host", detail={"reason": "bad_host", "host": host or ""})

    def _check_origin(self) -> None:
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if origin:
            if not origin_ok(origin, port):
                raise ApiError(403, "Запрос с чужого Origin", detail={"reason": "bad_origin", "origin": origin})
            return
        referer = self.headers.get("Referer")
        if referer and not origin_ok(referer, port):
            raise ApiError(403, "Запрос с чужого Referer", detail={"reason": "bad_origin", "referer": referer})

    # --- тело запроса: политика маршрута (лимит, бинарность) ---
    @staticmethod
    def _policy(app, path: str) -> dict:
        """Политика тела: по умолчанию 8 МиБ JSON, маршрут может переопределить."""
        policy = body_policy(path)
        hook = getattr(app, "body_policy", None)
        if callable(hook):
            try:
                extra = hook(path) or {}
            except Exception:  # noqa: BLE001 - политика не должна ронять запрос
                extra = {}
            if isinstance(extra, dict):
                policy.update(extra)
        return policy

    def _read_body(self, policy: dict):
        """Прочитать тело по политике: JSON-объект или ``RawBody`` с байтами."""
        limit = int(policy.get("limit") or MAX_BODY)
        binary = bool(policy.get("binary"))
        transfer = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if transfer:
            self.close_connection = True
            raise ApiError(411, "Тело в Transfer-Encoding не поддерживается — нужен Content-Length",
                           detail={"transfer_encoding": transfer})
        raw_type = self.headers.get("Content-Type") or ""
        ctype = raw_type.split(";")[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "Некорректный Content-Length", detail={}) from None
        if length < 0:
            raise ApiError(400, "Некорректный Content-Length", detail={"length": length})
        if length > limit:
            self.close_connection = True
            raise ApiError(413, str(policy.get("message") or "Тело запроса больше %s" % human_size(limit)),
                           code=policy.get("code") or None,
                           detail={"length": length, "limit": limit})
        if binary:
            data = self.rfile.read(length) if length else b""
            return RawBody(data, raw_type)
        if not length:
            return {}
        if ctype not in ("", "application/json", "text/json"):
            self.close_connection = True
            raise ApiError(415, "Ожидается Content-Type: application/json", detail={"content_type": ctype})
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "Тело не является JSON", detail={"error": str(exc)[:200]}) from None
        except (ValueError, RecursionError) as exc:      # глубина/огромные числа: 400, не 500
            raise ApiError(400, "Тело не является корректным JSON", detail={"error": str(exc)[:200]}) from None
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ApiError(400, "Тело должно быть JSON-объектом", detail={"type": type(data).__name__})
        if has_nul(data):
            # D12: NUL в тексте/заголовке иначе доходит до subprocess → 500
            raise ApiError(400, "В теле запроса есть NUL-байт", detail={"field": "body"})
        return data

    # --- ответы ---
    def _send_result(self, status: int, payload, headers: dict | None) -> None:
        headers = dict(headers or {})
        if isinstance(payload, Raw):
            merged = {**headers, **payload.headers}
            return self._send(int(payload.status or status), payload.body, payload.ctype, merged)
        if isinstance(payload, (bytes, bytearray)):
            return self._send(status, bytes(payload), headers.pop("Content-Type", "application/octet-stream"), headers)
        data = json_bytes(payload)
        etag = headers.get("ETag")
        if etag and status == 200 and self.command in ("GET", "HEAD") \
                and inm_matches(self.headers.get("If-None-Match"), etag):
            return self._send_not_modified(etag, headers)
        headers.setdefault("Cache-Control", "no-store")
        return self._send(status, data, JSON_CT, headers)

    def _send_not_modified(self, etag: str, headers: dict) -> None:
        self.send_response(304)
        for key, value in security_headers().items():
            self.send_header(key, value)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", headers.get("Cache-Control") or "no-cache")
        self.end_headers()

    def _send_error(self, exc: ApiError) -> None:
        self._send(exc.status, json_bytes(exc.payload()), JSON_CT,
                   {"Cache-Control": "no-store", "Connection": "close"})

    def _send(self, status: int, payload, ctype: str | None, headers: dict | None) -> None:
        payload = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload or b"")
        try:
            self.send_response(status)
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in security_headers().items():
                self.send_header(key, value)
            for key, value in (headers or {}).items():
                if value is not None:
                    self.send_header(str(key), str(value))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and payload:
                self.wfile.write(payload)
        except CONNECTION_ERRORS:
            # клиент ушёл (закрыл вкладку/оборвал запрос) — это норма, не ошибка сервера
            self.close_connection = True
