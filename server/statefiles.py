"""Файловое хранилище «Комнаты команды»: JSON/JSONL с атомарной записью.

Все операции сериализуются по пути файла (глобальный RLock на путь), поэтому
два потока не теряют данные при read-modify-write циклах в registry/ingest.
Только stdlib, UTF-8 (BOM допускается при чтении), LF, ensure_ascii=False.

Надёжность (D4/D5/D14):
* ``read_json_doc`` различает «файла нет» (``missing``), «битый JSON» (``corrupt``)
  и «прочитать нельзя» (``unreadable``: занят другим процессом, нет прав).
  Вызывающий код сам решает, можно ли писать поверх, — молчаливой подмены
  значениями по умолчанию больше нет.
* Битый файл перед перезаписью копируется в ``<файл>.corrupt-<unixtime>``
  (``quarantine``); факт запоминается и уходит в ``/api/health`` (``problems``).
* ``PermissionError``/``EACCES`` при чтении повторяется 3 раза с паузой,
  ``os.replace`` при записи — 5 раз с backoff 20 мс (Windows держит файл
  открытым у читателя, и ``os.replace`` падает с WinError 5/32).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

# --- блокировки по пути -----------------------------------------------------

_GUARD = threading.RLock()
_LOCKS: dict[str, threading.RLock] = {}

REPLACE_ATTEMPTS = 5          # D14: os.replace под открытым читателем на Windows
REPLACE_PAUSE = 0.02          # 20 мс, линейный backoff: 20/40/60/80 мс
READ_ATTEMPTS = 3             # D4/D5: PermissionError при чтении — 3 попытки
READ_PAUSE = 0.05

_TRANSIENT_ERRNOS = (errno.EACCES, errno.EPERM, errno.EBUSY, errno.EAGAIN)
_TRANSIENT_WINERRORS = (5, 32, 33)      # ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION


class StateUnavailable(Exception):
    """Файл состояния существует, но прочитать его нельзя (занят/нет прав).

    Это НЕ «данных нет»: писать поверх нельзя, поэтому вызывающий код пробрасывает
    ошибку до API (503 ``engine_error`` с русским сообщением), а не подставляет
    значения по умолчанию.
    """

    code = "engine_error"

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = dict(detail or {})

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def lock_for(path: str | os.PathLike) -> threading.RLock:
    """RLock, общий для всех операций с этим путём (ключ — нормализованный абсолютный путь)."""
    key = os.path.normcase(os.path.abspath(str(path)))
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


# --- проблемы чтения (для /api/health) --------------------------------------

_PROBLEMS_LOCK = threading.RLock()
_PROBLEMS: dict[str, dict] = {}
_QUARANTINED: dict[str, str] = {}


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
            return True
        return exc.errno in _TRANSIENT_ERRNOS
    return False


def _describe(info: dict) -> str:
    """Русское сообщение для health: что случилось и где копия."""
    path = info.get("path") or ""
    if info.get("state") == "corrupt":
        text = "Файл «%s» повреждён: не читается как JSON." % path
        if info.get("backup"):
            text += " Данные не потеряны — копия: %s." % info["backup"]
        else:
            text += " Копия будет сохранена перед перезаписью."
        return text
    if info.get("state") == "unreadable":
        return ("Файл «%s» сейчас недоступен (%s). Данные не заменяются значениями "
                "по умолчанию — повторите попытку." % (path, info.get("error") or "занят"))
    return ""


def _remember(info: dict) -> dict:
    info = dict(info)
    info.setdefault("code", "state_corrupt" if info.get("state") == "corrupt" else "state_unreadable")
    info["message"] = _describe(info)
    with _PROBLEMS_LOCK:
        _PROBLEMS[str(info.get("path") or "")] = info
    return info


def problems() -> list[dict]:
    """Все известные проблемы чтения файлов состояния (для warning-ов health)."""
    with _PROBLEMS_LOCK:
        return [dict(item) for item in _PROBLEMS.values()]


def clear_problem(path: str | os.PathLike) -> None:
    with _PROBLEMS_LOCK:
        _PROBLEMS.pop(str(path), None)


def quarantine(path: str | os.PathLike) -> str:
    """Скопировать битый файл в ``<имя>.corrupt-<unixtime>``; вернуть путь копии.

    Копия делается один раз на версию файла (mtime+size): повторные чтения не
    плодят бэкапы. Исходный файл не трогаем — его заменит ближайшая запись.
    """
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return ""
    key = "%s|%d|%d" % (os.path.normcase(str(target)), stat.st_mtime_ns, stat.st_size)
    with _PROBLEMS_LOCK:
        cached = _QUARANTINED.get(key)
    if cached:
        return cached
    stamp = int(time.time())
    backup = target.with_name("%s.corrupt-%d" % (target.name, stamp))
    counter = 1
    while backup.exists():
        backup = target.with_name("%s.corrupt-%d-%d" % (target.name, stamp, counter))
        counter += 1
    try:
        shutil.copy2(target, backup)
    except OSError:
        return ""
    with _PROBLEMS_LOCK:
        _QUARANTINED[key] = str(backup)
        current = _PROBLEMS.get(str(target))
    if current is not None:
        current["backup"] = str(backup)
        _remember(current)
    return str(backup)


# --- чтение ----------------------------------------------------------------
def _read_text(path) -> tuple[str | None, object]:
    """(текст, None) либо (None, "missing") либо (None, исключение).

    BOM читается как utf-8-sig; PermissionError/«файл занят» повторяется.
    """
    last: BaseException | None = None
    for attempt in range(READ_ATTEMPTS):
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                return fh.read(), None
        except FileNotFoundError:
            return None, "missing"
        except OSError as exc:
            last = exc
            if not _is_transient(exc) or attempt == READ_ATTEMPTS - 1:
                return None, exc
            time.sleep(READ_PAUSE * (attempt + 1))
    return None, last


def read_json_doc(path: str | os.PathLike, default=None) -> tuple[object, dict]:
    """Прочитать JSON и вернуть ``(значение, info)``.

    ``info["state"]``: ``ok`` | ``missing`` | ``corrupt`` | ``unreadable``.
    Проблемы запоминаются и попадают в ``/api/health`` (``problems()``).
    """
    target = str(path)
    text, error = _read_text(target)
    if error == "missing":
        return default, {"path": target, "state": "missing", "error": "", "backup": ""}
    if error is not None:
        return default, _remember({"path": target, "state": "unreadable",
                                   "error": "%s: %s" % (type(error).__name__, error), "backup": ""})
    try:
        value = json.loads(text or "")
    except (ValueError, UnicodeDecodeError) as exc:
        return default, _remember({"path": target, "state": "corrupt",
                                   "error": str(exc)[:200], "backup": ""})
    with _PROBLEMS_LOCK:
        previous = _PROBLEMS.get(target)
        if previous is not None and not previous.get("backup"):
            _PROBLEMS.pop(target, None)   # прочитался и копии не было — тревога снята
    return value, {"path": target, "state": "ok", "error": "", "backup": ""}


def read_json(path: str | os.PathLike, default=None):
    """Прочитать JSON. Нет файла/битый JSON/нет прав → default (исключений не бросаем)."""
    value, _info = read_json_doc(path, default)
    return value


# --- запись ----------------------------------------------------------------
def _replace_with_retry(src: str, dst: str) -> None:
    """``os.replace`` с повторами: Windows отдаёт PermissionError, пока файл открыт."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_PAUSE * (attempt + 1))


def _read_bytes(path: Path) -> bytes | None:
    for attempt in range(READ_ATTEMPTS):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            if not _is_transient(exc) or attempt == READ_ATTEMPTS - 1:
                return None
            time.sleep(READ_PAUSE * (attempt + 1))
    return None


def write_json_atomic(path: str | os.PathLike, obj) -> Path:
    """Атомарно записать JSON: tmp в том же каталоге + os.replace (с повторами).

    Гарантирует, что читатель увидит либо старое, либо новое содержимое целиком.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    with lock_for(target):
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp_name, str(target))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return target


# --- JSONL ------------------------------------------------------------------
def append_jsonl(path: str | os.PathLike, obj) -> Path:
    """Дописать одну JSON-строку (UTF-8, LF). Каталог создаётся при необходимости."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with lock_for(target):
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
    return target


def read_jsonl(path: str | os.PathLike, limit: int | None = None) -> list:
    """Прочитать JSONL; limit — вернуть только последние N записей (порядок сохраняется).

    Битые строки пропускаются: журнал не должен ронять чтение.
    """
    items: list = []
    text, error = _read_text(path)
    if error is not None:
        return []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    if limit is not None and limit >= 0:
        return items[-limit:] if limit else []
    return items


# --- текст (зеркало в Vault) ------------------------------------------------
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_text_if_changed(path: str | os.PathLike, text: str, newline: str = "\n") -> bool:
    """Записать текст только если содержимое отличается. True — файл изменён.

    Сравнение по байтам UTF-8; запись атомарная (tmp+replace), LF.
    """
    target = Path(path)
    data = text.encode("utf-8")
    with lock_for(target):
        current = _read_bytes(target)
        if current is not None and current == data:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp_name, str(target))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return True


def read_text(path: str | os.PathLike, default: str = "") -> str:
    text, error = _read_text(path)
    return default if error is not None else (text or default)
