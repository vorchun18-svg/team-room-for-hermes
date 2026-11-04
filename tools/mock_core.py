# -*- coding: utf-8 -*-
"""Общие константы, ошибки и чистые помощники мок-сервера."""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import time

from mock_versions import (CRITIC_STATES, PHASES_CRITIC, PHASES_PLAIN, PHASE_ORDER,  # noqa: F401
                           SYNTHESIS_STATES, TREE_ROLES)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
FIX_DIR = os.path.join(ROOT, "fixtures")
DATA_DIR = os.path.join(ROOT, "data")
PHASES = PHASES_CRITIC  # обратная совместимость: расписание с критиком
STATUS_ORDER = PHASE_ORDER
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MAX_BODY = 8 * 1024 * 1024          # как server/http_util.MAX_BODY (API §0)
MAX_TEXT = 1_000_000                # текст запроса/сообщения (server/app_common.MAX_TEXT)
MAX_NOTE = 200_000                  # комментарий/заметка/ответ (server/app_common.MAX_NOTE)
MAX_AGENT_NOTE = 8000               # «Особые указания участнику» (WAVE9 §9)
MOCK = True                         # health.engine.mock: фронт показывает плашку «ДЕМО»
# Белый список /api/file — те же пять корней, что в API §2 / server/config.py.
FILE_ROOTS = [DATA_DIR, r"D:\Проекты\Vault", r"D:\Проекты\Комната", r"D:\Проекты\Код"]
_boards = os.path.join(os.environ.get("LOCALAPPDATA") or "", "hermes", "kanban", "boards")
if os.path.isdir(_boards):
    FILE_ROOTS += [os.path.join(b, "workspaces") for b in os.listdir(_boards)
                   if os.path.isdir(os.path.join(_boards, b, "workspaces"))]
# Мок не отдаёт собственные исходники и фикстуры (отличие мока, см. API §7).
MOCK_PRIVATE = [os.path.join(ROOT, name) for name in ("server", "tools", "tests", "reportkit", "fixtures", "docs")]
ALLOWED_EXT = {".md", ".txt", ".json", ".csv", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".webp",
               ".svg", ".pdf", ".docx", ".xlsx", ".pptx", ".ics", ".log"}
DENY_NAME_PARTS = (".env", "auth.json", "token", "secret", ".session", "google_", "credentials",
                   "password", "id_rsa")
OFFICE_EXT = {".docx", ".xlsx", ".pptx"}

FIXTURE_BY_RID = {
    "t_a1b2c3d4": "request_done.json",
    "t_c4d2e118": "request_critic_approved.json",
    "t_605613e0": "request_done_legacy.json",
    "t_9f3c2a10": "request_working.json",
    "t_7d21b8c4": "request_reviewing.json",
    "t_5c8e4a02": "request_needs_you.json",
    "t_3b7e9d11": "request_failed.json",
    "t_2a6f5c93": "request_queued.json",
    "t_4e91c7b2": "request_outside_room.json",
}
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
        ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
        ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
        ".pdf": "application/pdf"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail or {}


class Raw:
    """Не-JSON ответ: тело, тип и дополнительные заголовки."""

    def __init__(self, body, ctype: str, headers: dict | None = None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.ctype = ctype
        self.headers = headers or {}


def now() -> int:
    return int(time.time())


def local_midnight(ts: int | None = None) -> int:
    """Начало текущих суток по поясу сервера (для счётчика done_today)."""
    lt = time.localtime(ts or now())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def inside(root: str, path: str) -> bool:
    """path лежит внутри root (с учётом регистра Windows), без исключений на разных дисках."""
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)),
                                   os.path.normcase(os.path.realpath(root))]) == os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False


def slugify(text: str) -> str:
    table = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
             "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
             "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
             "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}
    out = "".join(table.get(ch, ch) for ch in text.lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return (out[:32] or "room-" + secrets.token_hex(2)).strip("-")


# --- чистые функции -------------------------------------------------------- --------------------------------------------------------
def child_id(rid: str, index: int) -> str:
    """Дочерний id, не пересекающийся с корнями других живых запросов."""
    return "t_%08x" % (((int(rid[2:], 16) << 4) | (index & 0xF)) & 0xFFFFFFFF)


def compute_needs_you(d: dict) -> bool:
    """Флаг `needs_you` (API §3.3): статус needs_you, ИЛИ неотвеченный вопрос Михаилу
    (в т.ч. у `done`-запроса — статус остаётся `done`), ИЛИ карточка ждёт человека."""
    if d.get("status") == "needs_you":
        return True
    if any((q.get("for") or "mikhail") == "mikhail" and not q.get("answered")
           for q in d.get("open_questions") or []):
        return True
    return any(n.get("needs_human") for n in (d.get("tree") or [])[1:])


def counts_as_waiting(summary: dict) -> bool:
    """Считается ли запрос в `counters.needs_you`: статус needs_you или done с флагом needs_you."""
    status = summary.get("status")
    return status == "needs_you" or (status == "done" and bool(summary.get("needs_you")))


def detail_to_summary(d: dict) -> dict:
    keys = ("id", "room", "title", "status", "status_reason", "mode", "critic", "created_at", "started_at",
            "finished_at", "duration_s", "progress", "assignee", "needs_you", "preview", "archived")
    out = {k: d.get(k) for k in keys}
    out["critic"] = critic_ref(out.get("critic"))
    out["needs_you"] = compute_needs_you(d)
    if out.get("preview") is None:
        out["preview"] = (d.get("text") or "")[:200]
    if out.get("archived") is None:
        out["archived"] = False
    return out


def critic_ref(block) -> dict:
    """CriticBlock (7 ключей) → CriticRef (4 ключа) для RequestSummary, API §3.3."""
    if isinstance(block, bool):  # старая форма: bool в фикстуре
        return {"enabled": block, "state": "waiting" if block else "off", "verdict": None, "round": 0}
    block = block if isinstance(block, dict) else {}
    return {"enabled": bool(block.get("enabled")), "state": block.get("state") or "off",
            "verdict": block.get("verdict"), "round": int(block.get("round") or 0)}


# --- маршруты: для 405 + Allow (API §1) ------------------------------------
_RID = r"t_[0-9a-f]{8}"
_ROOM = r"[a-z0-9-]{2,32}"
ROUTE_METHODS = (
    (r"^/api/health$", ("GET",)),
    (r"^/api/config$", ("GET",)),
    (r"^/api/profiles$", ("GET",)),
    (r"^/api/models$", ("GET",)),
    (r"^/api/dispatcher/kick$", ("POST",)),
    (r"^/api/rooms$", ("GET", "POST")),
    (r"^/api/rooms/check-dir$", ("POST",)),
    (r"^/api/rooms/%s$" % _ROOM, ("GET", "PATCH", "POST")),
    (r"^/api/rooms/%s/requests$" % _ROOM, ("GET", "POST")),
    (r"^/api/rooms/%s/candidates$" % _ROOM, ("GET",)),
    (r"^/api/rooms/%s/team$" % _ROOM, ("GET",)),
    (r"^/api/rooms/%s/roadmap$" % _ROOM, ("GET",)),
    (r"^/api/rooms/%s/backlog$" % _ROOM, ("GET",)),
    (r"^/api/rooms/%s/backlog/.+/ask$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/backlog/.+$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/journal$" % _ROOM, ("GET",)),
    (r"^/api/rooms/%s/reports$" % _ROOM, ("GET",)),
    (r"^/api/rooms/%s/archive$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/members$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/members/[a-z0-9_.-]{2,40}$" % _ROOM, ("DELETE",)),
    (r"^/api/rooms/%s/agents/[a-z0-9_.-]{2,40}/notes$" % _ROOM, ("PUT",)),
    (r"^/api/rooms/%s/agents/[a-z0-9_.-]{2,40}/model$" % _ROOM, ("PUT",)),
    (r"^/api/rooms/%s/agents/[a-z0-9_.-]{2,40}/restart$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/plan$" % _ROOM, ("GET",)),
    (r"^/api/rooms/%s/plan/approve$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/plan/rebuild$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/plan/streams$" % _ROOM, ("POST",)),
    (r"^/api/rooms/%s/plan/streams/[A-Za-z0-9_-]{1,16}$" % _ROOM, ("PATCH", "DELETE")),
    (r"^/api/rooms/%s/consolidate$" % _ROOM, ("POST",)),
    (r"^/api/requests/%s$" % _RID, ("GET",)),
    (r"^/api/requests/%s/actions$" % _RID, ("POST",)),
    (r"^/api/requests/%s/messages$" % _RID, ("POST",)),
    (r"^/api/reports/%s/[a-z0-9-]{2,64}$" % _ROOM, ("GET",)),
    (r"^/api/search$", ("GET",)),
    (r"^/api/ics$", ("GET",)),
    (r"^/api/file$", ("GET",)),
    (r"^/api/open$", ("POST",)),
    (r"^/api/adopt$", ("POST",)),
    (r"^/api/export/request/%s\.(md|html)$" % _RID, ("GET",)),
    (r"^/api/stt/status$", ("GET",)),
    (r"^/api/stt/transcribe$", ("POST",)),
    (r"^/api/stt/dictionary$", ("GET", "PUT")),
    (r"^/api/stt/learn$", ("POST",)),
)


def allowed_methods(path: str) -> tuple:
    """Разрешённые методы для известного пути (пусто — путь неизвестен)."""
    out = []
    for pattern, methods in ROUTE_METHODS:
        if re.fullmatch(pattern, path):
            out.extend(m for m in methods if m not in out)
    return tuple(out)


def backlog_counts(items: list) -> dict:
    out = {"open": 0, "asked": 0, "done": 0, "dismissed": 0, "total": len(items)}
    for it in items:
        out[it["status"]] = out.get(it["status"], 0) + 1
    return out


def ics_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def export_markdown(d: dict) -> str:
    lines = ["# %s" % d["title"], "", "Комната: %s · статус: %s · создан: %s" % (
        d["room"], d["status"], time.strftime("%d.%m.%Y %H:%M", time.localtime(d["created_at"]))), "",
        d.get("synthesis", {}).get("text_md") or "_Ответ ещё не готов._", "", "## Дальше", ""]
    lines += ["- [%s] %s — %s" % ("x" if s["state"] == "done" else " ", s["text"], s.get("owner") or "—")
              for s in d.get("next_steps", [])] or ["—"]
    lines += ["", "## Вопросы", ""]
    lines += ["- %s%s" % (q["text"], " (отвечено)" if q.get("answered") else "") for q in d.get("open_questions", [])] or ["—"]
    lines += ["", "## Дерево работ", ""]
    for n in d.get("tree", []):
        lines.append("- `%s` %s — %s (%s)" % (n["id"], n["title"], n["assignee"], n["status"]))
    return "\n".join(lines) + "\n"


def export_html(d: dict, md: str) -> str:
    esc = html.escape
    rows = "".join("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        esc(n["id"]), esc(n["title"]), esc(str(n["assignee"])), esc(n["status"])) for n in d.get("tree", []))
    return ("<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\"><title>%s</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#111}"
            "pre{white-space:pre-wrap;background:#f6f6f6;padding:16px;border-radius:8px}"
            "table{border-collapse:collapse;width:100%%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}"
            "code{font-size:12px;color:#555}</style>"
            "<h1>%s</h1><p>Комната %s · статус %s · экспорт мок-сервера</p>"
            "<h2>Ответ</h2><pre>%s</pre><h2>Дерево работ</h2>"
            "<table><tr><th>id</th><th>Карточка</th><th>Исполнитель</th><th>Статус</th></tr>%s</table></body></html>") % (
        esc(d["title"]), esc(d["title"]), esc(d["room"]), esc(d["status"]), esc(md), rows)
