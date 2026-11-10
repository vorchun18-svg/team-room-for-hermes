"""Отдача файлов из белого списка и «открыть в проводнике».

Публичный интерфейс (сигнатуры фиксированы ARCHITECTURE §7 / задача):
    open_file(cfg, path) -> (status:int, headers:dict, body:bytes|Path)
    reveal(cfg, path) -> bool

Правила:
  * любой путь проходит через ``config.path_allowed`` (realpath + белые корни +
    чёрный список имён + белый список расширений);
  * ``html``/``svg`` отдаём как ``text/plain`` — запускать HTML можно только
    через /reports/… в sandbox-iframe;
  * ``docx/xlsx/pptx`` — только скачивание (``Content-Disposition: attachment``);
  * лимит 50 МБ, Range не поддерживаем;
  * ошибки — русским текстом, без HTML.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

try:  # import server.files
    from . import proc
    from .config import (ALLOWED_EXT, Cfg, external_allowed, is_denied_name, path_allowed)
except ImportError:  # python server/app.py (плоский импорт)
    import proc  # type: ignore
    from config import (ALLOWED_EXT, Cfg, external_allowed, is_denied_name,  # type: ignore
                        path_allowed)

MAX_BYTES = 50 * 1024 * 1024

# расширение -> Content-Type (html/svg намеренно text/plain, см. модульный docstring)
MIME = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".ics": "text/calendar; charset=utf-8",
    ".html": "text/plain; charset=utf-8",
    ".htm": "text/plain; charset=utf-8",
    ".svg": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# что показываем в браузере, а что отдаём на скачивание
ATTACHMENT_EXT = {".docx", ".xlsx", ".pptx"}
# текстовые типы: имя файла отдаём как есть, но как plain text
PLAIN_EXT = {".html", ".htm", ".svg"}
# D3: ничего исполняемого не отдаём и не «открываем» в проводнике
BLOCKED_EXT = {".exe", ".bat", ".cmd", ".com", ".scr", ".lnk", ".ps1", ".msi", ".js",
               ".jse", ".vbs", ".vbe", ".wsf", ".wsh", ".jar", ".reg", ".url", ".pif"}

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def safe_resolve(root, rel):
    """Единственная проверка чтения файла: resolve + внутри корня + чёрный список.

    Возвращает resolved ``Path`` файла внутри ``root`` или ``None``. Отклоняет
    ``..``, абсолютные пути вне корня, NUL, ADS-потоки (``:``), запрещённые имена
    (``auth.json``, ``.env``, ``*secret*``, ``*.pem`` …) в ЛЮБОМ сегменте пути и
    не-файлы. Используется отчётами, ``/api/file`` и ``/api/open``.
    """
    raw = "" if rel is None else str(rel).strip()
    if not raw or "\x00" in raw or "::" in raw:
        return None
    try:
        base = Path(root).resolve(strict=True)
    except (OSError, ValueError):
        return None
    try:
        candidate = Path(raw)
        path = (candidate.resolve(strict=False) if candidate.is_absolute()
                else (base / candidate).resolve(strict=False))
    except (OSError, ValueError):
        return None
    try:
        path.relative_to(base)
    except (ValueError, OSError):
        return None
    if any(is_denied_name(part) or part.lower().endswith(".pem") for part in path.parts):
        return None
    if path.suffix.lower() in BLOCKED_EXT:
        return None
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def root_of(cfg: Cfg, path, roots=None) -> Path | None:
    """Белый корень, внутри которого лежит путь (для ``safe_resolve``)."""
    try:
        target = Path(path).resolve(strict=False)
    except (OSError, ValueError):
        return None
    for root in (roots if roots is not None else cfg.allowed_roots):
        try:
            base = Path(root).resolve()
        except (OSError, ValueError):
            continue
        try:
            target.relative_to(base)
            return base
        except (ValueError, OSError):
            continue
    return None


def _err(status: int, message: str) -> tuple:
    """Ответ-ошибка: русский текст, никакого HTML."""
    body = (message + "\n").encode("utf-8")
    headers = dict(BASE_HEADERS)
    headers["Content-Type"] = "text/plain; charset=utf-8"
    headers["Content-Length"] = str(len(body))
    return status, headers, body


def _suspicious(raw: str) -> bool:
    """Признаки обхода: ADS-потоки, NUL, запрещённые для Windows символы.

    ``C:/x/file.txt::$DATA``, ``file.txt:stream``, ``\\\\?\\`` и прочее —
    отклоняем до обращения к диску.
    """
    if "\x00" in raw:
        return True
    if "::" in raw:
        return True
    rest = raw[2:] if len(raw) > 1 and raw[1] == ":" else raw
    if ":" in rest:
        return True
    return any(ch in raw for ch in '<>"|?*')


def _safe_name(name: str) -> str:
    """Имя для заголовка: только буквы/цифры/._- и пробел, без кавычек и CRLF."""
    out = []
    for ch in str(name):
        if ch.isalnum() or ch in "._- ":
            out.append(ch)
        elif ch in ("\r", "\n", "\t"):
            out.append("_")
        else:
            out.append("_")
    safe = "".join(out).strip(" .") or "file"
    return safe[:120]


def _disposition(path: Path, inline: bool) -> str:
    """Content-Disposition с ASCII-запасным именем и RFC 5987 для кириллицы."""
    name = _safe_name(path.name)
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    if ascii_name.strip("_ .") == "":
        ascii_name = "file"
    suffix = path.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,6}", path.suffix.lower() or "") else ""
    if not ascii_name.lower().endswith(suffix) and suffix:
        ascii_name += suffix
    return ("inline" if inline else "attachment") + '; filename="%s"; filename*=UTF-8\'\'%s' % (
        ascii_name, quote(name, safe=""))


def _in_roots(cfg: Cfg, raw: str, roots=None) -> bool:
    """Лежит ли путь (даже ещё не существующий) внутри белых корней."""
    try:
        p = Path(raw).resolve(strict=False)
    except (OSError, ValueError):
        return False
    for root in (roots if roots is not None else cfg.allowed_roots):
        try:
            p.relative_to(Path(root).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _isfile(raw: str) -> bool:
    try:
        return os.path.isfile(raw)
    except (OSError, ValueError):
        return False


def open_file(cfg: Cfg, path, roots=None, allow_external: bool = False) -> tuple:
    """Отдать файл из белого списка.

    Возвращает ``(status, headers, body)``: при 200 ``body`` — ``Path``
    (сервер сам стримит, не держим 50 МБ в памяти), при ошибке — ``bytes``.

    ``roots`` — корни артефактов комнаты (папка проекта и зеркало, B-22).
    ``allow_external`` — показать файл, который лежит вне этих корней, но прошёл
    отдельную проверку ``config.external_allowed`` (B-27: агент сохранил файл
    в другом месте — показываем, но с предупреждением).
    """
    raw = "" if path is None else str(path).strip()
    if not raw:
        return _err(400, "Не указан путь к файлу.")
    if _suspicious(raw):
        return _err(403, "Путь содержит недопустимые символы.")

    resolved = path_allowed(cfg, raw, roots=roots)
    external = False
    if resolved is None and allow_external:
        resolved = external_allowed(cfg, raw)
        external = resolved is not None
    if resolved is None:
        # вне корней — всегда 403 (не подтверждаем существование файла);
        # внутри корня: запрещённое имя/расширение — 403, отсутствие — 404
        # Вне корней без явного разрешения на внешние файлы — 403 «вне разрешённых» ещё ДО проверки,
        # существует ли файл: наличие постороннего файла не подтверждаем.
        if not allow_external and not _in_roots(cfg, raw, roots=roots):
            return _err(403, "Файл вне разрешённых каталогов или запрещён к показу.")
        if _isfile(raw):
            return _err(403, "Файл запрещён к показу: имя или тип вне белого списка.")
        return _err(404, "Файл удалён или перемещён." if allow_external else "Файл не найден.")
    root = root_of(cfg, resolved, roots=roots)
    # единая проверка чтения (resolve + корень + чёрный список + запрет исполняемого)
    checked = safe_resolve(root, resolved) if root is not None else (resolved if external else None)
    if checked is None:
        return _err(403, "Файл запрещён к показу: имя или тип вне белого списка.")

    try:
        size = resolved.stat().st_size
    except OSError:
        return _err(404, "Файл не найден.")
    if size > MAX_BYTES:
        return _err(413, "Файл больше 50 МБ — откройте его в проводнике.")

    ext = resolved.suffix.lower()
    headers = dict(BASE_HEADERS)
    headers["Content-Type"] = MIME.get(ext, "application/octet-stream")
    headers["Content-Length"] = str(size)
    headers["Content-Disposition"] = _disposition(resolved, inline=ext not in ATTACHMENT_EXT)
    if ext in PLAIN_EXT:
        # исходник страницы как текст: даже при ошибке браузера он не исполнится
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    headers["X-Frame-Options"] = "SAMEORIGIN"
    return 200, headers, resolved


def reveal(cfg: Cfg, path, roots=None, allow_external: bool = False) -> bool:
    """«Открыть в проводнике»: ``explorer /select,<path>`` только для белых путей.

    ``roots``/``allow_external`` — как у ``open_file``: артефакты комнаты (B-22)
    и файлы вне папки проекта (B-27) тоже открываются, с теми же проверками.
    """
    raw = "" if path is None else str(path).strip()
    if not raw or _suspicious(raw):
        return False
    resolved = path_allowed(cfg, raw, roots=roots)
    external = False
    if resolved is None and allow_external:
        resolved = external_allowed(cfg, raw)
        external = resolved is not None
    if resolved is None:
        return False
    if not external:
        root = root_of(cfg, resolved, roots=roots)
        if root is None or safe_resolve(root, resolved) is None:
            return False        # запрещённые имена/типы, включая .exe/.bat/.lnk/.ps1
    if sys.platform != "win32":
        return False
    try:
        # gui=True: окно проводника должно быть видно, но своих потоков у него нет
        # и консоль не создаётся (флаг окна ставит proc).
        proc.popen(["explorer", "/select,%s" % resolved], module=subprocess, gui=True,
                   close_fds=True)
    except (OSError, ValueError):
        return False
    return True


def resolve_file(cfg: Cfg, path) -> Path | None:
    """Совместимость с именем из ARCHITECTURE §4: разрешённый путь или None."""
    raw = "" if path is None else str(path).strip()
    if not raw or _suspicious(raw):
        return None
    try:
        return path_allowed(cfg, raw)
    except (OSError, ValueError):
        return None


def info(cfg: Cfg, path) -> dict:
    """Метаданные файла для UI (без чтения содержимого). None-поля при запрете."""
    raw = "" if path is None else str(path).strip()
    resolved = path_allowed(cfg, raw) if raw and not _suspicious(raw) else None
    if resolved is None:
        return {"ok": False, "path": raw, "name": Path(raw).name if raw else "", "ext": "",
                "size": None, "mime": "", "allowed": False}
    ext = resolved.suffix.lower()
    try:
        size = resolved.stat().st_size
    except OSError:
        size = None
    return {"ok": True, "path": str(resolved), "name": resolved.name, "ext": ext,
            "size": size, "mime": MIME.get(ext, "application/octet-stream"),
            "allowed": ext in ALLOWED_EXT}


__all__ = ["MAX_BYTES", "MIME", "BLOCKED_EXT", "safe_resolve", "root_of", "open_file", "reveal",
           "resolve_file", "info", "is_denied_name"]
