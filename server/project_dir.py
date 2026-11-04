"""Папка проекта комнаты: проверка пути, создание, содержание (README.md).

Зачем (Д2 WAVE8-RULES): у комнаты есть своя папка, куда команда складывает ВСЕ
итоговые материалы по проекту — отчёты, таблицы, документы. В Obsidian пишется
суть, в папку — результаты.

Публичный интерфейс (по нему пишут роуты и диалог комнаты):

    check_dir(cfg, path) -> {normalized, exists, writable, will_create,
                             warnings: [str], allowed: bool, reason: str|None}
    ensure_dir(cfg, path) -> Path            # mkdir -p, только если allowed
    request_subdir(title, created_ts) -> str  # «YYYY-MM-DD название»
    update_index(cfg, room) -> bool          # README.md, блок «Содержание»

Правила:
  * путь — абсолютный путь Windows (`C:\\...`); UNC `\\\\server\\share`, длинные
    `\\\\?\\`-пути, корень диска, `..`, зарезервированные имена (con, prn, nul,
    com1..9, lpt1..9) и недопустимые символы отклоняются;
  * запрещены системные каталоги (Windows, Program Files, ProgramData), папка
    данных приложения (`<app>/data`), каталоги программы (`server/`, `web/`) и
    весь `%LOCALAPPDATA%\\hermes`;
  * Vault не запрещён, но даёт предупреждение: результаты лучше держать отдельно;
  * `check_dir` НИЧЕГО не создаёт — права проверяются у ближайшего существующего
    родителя; единственная запись на диске — `ensure_dir` под `project_dir`;
  * `update_index` правит только служебный блок между маркерами
    `<!-- teamroom:begin index -->…<!-- teamroom:end -->`, ручной текст вне
    маркеров сохраняется.
"""
from __future__ import annotations

import datetime as _dt
import ntpath
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:                                    # import server.project_dir
    from . import proc
    from . import safety
    from . import statefiles as sf
except ImportError:                      # python server/app.py (плоский импорт)
    import proc                          # type: ignore
    import safety                        # type: ignore
    import statefiles as sf              # type: ignore

__all__ = ["DirError", "check_dir", "ensure_dir", "open_dir", "open_dir_report",
           "request_subdir", "update_index", "BEGIN_INDEX", "END_INDEX", "INDEX_FILE",
           "MAX_PATH_LEN", "EXPLORER_CLASSES"]

INDEX_FILE = "README.md"
BEGIN_INDEX = "<!-- teamroom:begin index -->"
END_INDEX = "<!-- teamroom:end -->"

#: Длиннее — Windows начинает капризничать с длинными путями (без `\\\\?\\`).
MAX_PATH_LEN = 180
#: Имя папки запроса — до 60 знаков: дата + название, читаемо в проводнике.
MAX_SEGMENT_LEN = 60

#: Имена, которые Windows не даёт создать ни в каком виде (в т.ч. с расширением).
RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul", "clock$"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)

#: Системные каталоги: там наши отчёты точно не нужны.
SYSTEM_ROOTS = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
)

#: Сообщения — короткие и по-русски: их читает Михаил.
MSG_HERMES = "Папка внутри служебных каталогов Hermes — выберите другую"
MSG_APP = "Это папка программы «Комната команды» — выберите другую"
MSG_DATA = "Это папка данных приложения — выберите другую"
MSG_VAULT = ("Папка внутри Vault: там уже лежат заметки. Для результатов команды "
             "лучше отдельная папка — например D:\\Проекты\\<название>.")


class DirError(Exception):
    """Путь к папке проекта не подходит или её не удалось создать."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


# ─────────────────────────────────────────────────────────────── пути

def _normcase(text: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(text)))


def _under(child: str, parent: str) -> bool:
    """`child` == `parent` или лежит внутри (регистр и разделители не важны)."""
    if not parent:
        return False
    c, p = _normcase(child), _normcase(parent).rstrip("\\/")
    if not p:
        return False
    return c == p or c.startswith(p + "\\")


def _result(allowed: bool, reason: str | None = None, normalized: str = "",
            exists: bool = False, writable: bool = False, will_create: bool = False,
            warnings: list | None = None) -> dict:
    return {"normalized": normalized, "exists": bool(exists), "writable": bool(writable),
            "will_create": bool(will_create), "warnings": list(warnings or []),
            "allowed": bool(allowed), "reason": None if allowed else (reason or "Путь не подходит")}


def _segments(norm: str) -> list:
    drive, rest = ntpath.splitdrive(norm)
    return [part for part in re.split(r"[\\/]+", rest or "") if part]


def _forbidden_roots(cfg) -> list:
    """Пары (путь, сообщение): куда писать нельзя и что об этом сказать."""
    items = [(root, "Системный каталог — выберите другую папку") for root in SYSTEM_ROOTS]
    for attr, message in (("hermes_home", MSG_HERMES), ("app_dir", MSG_APP),
                          ("data_dir", MSG_DATA)):
        try:
            value = getattr(cfg, attr, None)
        except Exception:                      # noqa: BLE001 - конфиг может быть неполным
            value = None
        if value:
            items.append((str(value), message))
    return items


def _vault_roots(cfg) -> list:
    roots = []
    try:
        if getattr(cfg, "vault_dir", None):
            roots.append(str(cfg.vault_dir))
    except Exception:                          # noqa: BLE001
        pass
    try:
        roots.append(str(safety.real_vault()))
    except Exception:                          # noqa: BLE001
        pass
    return [root for root in roots if root]


def _writable_of(path: Path) -> bool:
    """Права на запись БЕЗ создания: у самой папки или у ближайшего родителя."""
    probe = path
    for _ in range(64):                        # защита от бесконечного подъёма
        try:
            if probe.exists():
                return probe.is_dir() and os.access(str(probe), os.W_OK)
        except OSError:
            return False
        parent = probe.parent
        if parent == probe:
            return False
        probe = parent
    return False


def check_dir(cfg, path) -> dict:
    """Проверить путь к папке проекта. Ничего не создаёт и не меняет на диске."""
    raw = "" if path is None else str(path)
    text = raw.strip().strip('"').strip()
    if not text:
        return _result(False, "Пустой путь: укажите папку, например D:\\Проекты\\Мой проект")

    if "\x00" in text or any(ord(ch) < 32 for ch in text):
        return _result(False, "В пути есть непечатаемые символы — проверьте путь")

    unc = text.replace("/", "\\")
    if unc.startswith("\\\\?\\") or unc.startswith("\\\\.\\"):
        return _result(False, "Длинные и служебные пути (\\\\?\\) не поддерживаются — выберите обычную папку")
    if unc.startswith("\\\\"):
        return _result(False, "Сетевая папка (\\\\сервер\\папка) не подходит — выберите папку на этом компьютере")

    if any(seg in ("..", ".") for seg in re.split(r"[\\/]+", text)):
        return _result(False, "В пути есть «..» — так нельзя, укажите папку целиком")

    norm = ntpath.normpath(text)
    drive, rest = ntpath.splitdrive(norm)
    if not drive or not re.fullmatch(r"[A-Za-z]:", drive):
        return _result(False, "Нужен полный путь, например D:\\Проекты\\Мой проект")
    if not (rest or "").strip("\\/"):
        return _result(False, "Нельзя выбрать весь диск (C:\\) — создайте папку внутри")

    if re.search(r'[<>:"|?*]', rest):
        return _result(False, "В пути есть символы, запрещённые в Windows: < > : \" | ? *")

    segments = _segments(norm)
    if not segments:
        return _result(False, "Нельзя выбрать весь диск (C:\\) — создайте папку внутри")
    for segment in segments:
        if segment != segment.rstrip(" ."):
            return _result(False, "Имя папки не может заканчиваться точкой или пробелом")
        stem = segment.split(".")[0].strip().lower()
        if stem in RESERVED_NAMES:
            return _result(False, "Имя «%s» зарезервировано в Windows — выберите другое" % segment)

    if len(norm) > MAX_PATH_LEN:
        return _result(False, "Слишком длинный путь — до %d знаков" % MAX_PATH_LEN)

    for root, message in _forbidden_roots(cfg):
        if _under(norm, root):
            return _result(False, message, normalized=norm)

    warnings = []
    if any(_under(norm, root) for root in _vault_roots(cfg)):
        warnings.append(MSG_VAULT)

    target = Path(norm)
    try:
        exists = target.is_dir()
    except OSError:
        return _result(False, "Не удалось проверить папку — проверьте путь", normalized=norm)
    if not exists:
        try:
            if target.exists() and not target.is_dir():
                return _result(False, "По этому пути лежит файл, а не папка", normalized=norm)
        except OSError:
            return _result(False, "Не удалось проверить папку — проверьте путь", normalized=norm)

    writable = _writable_of(target)
    will_create = not exists
    if not writable:
        return _result(False, "Нет прав на запись в эту папку — выберите другую",
                       normalized=norm, exists=exists, warnings=warnings)
    return _result(True, None, normalized=norm, exists=exists, writable=writable,
                   will_create=will_create, warnings=warnings)


def ensure_dir(cfg, path) -> Path:
    """Создать папку (`mkdir -p`) — только если `check_dir` разрешил."""
    result = check_dir(cfg, path)
    if not result["allowed"]:
        raise DirError(result["reason"] or "Путь не подходит",
                       "bad_request", {"path": result.get("normalized") or str(path)})
    target = Path(result["normalized"])
    try:
        if target.is_dir():
            return target
    except OSError as exc:
        raise DirError("Не удалось открыть папку «%s»: %s" % (target, exc), detail={"path": str(target)}) from exc
    if safety.is_under_real(target):
        # реальный Vault/Hermes: правило §1 WAVE7-RULES — папки там не создаём
        return target
    try:
        safety.guarded_mkdir(target, parents=True, exist_ok=True)
    except safety.SafetyError:
        return target
    except OSError as exc:
        raise DirError("Не удалось создать папку «%s»: %s" % (target, exc),
                       detail={"path": str(target), "error": str(exc)[:200]}) from exc
    return target


# ───────────────────────────────────── открытие папки в проводнике Windows
#
# Дефект с живой системы (W10): сервер запущен скрыто (pythonw/DETACHED_PROCESS),
# поэтому запущенное им окно проводника открывалось «в фоне» — за другими окнами.
# Правильный путь: запустить проводник в сессии пользователя (ShellExecute, без
# оболочки cmd) и САМОМУ вывести окно проводника вперёд: найти его через
# EnumWindows (класс CabinetWClass), затем Alt-трюк + AttachThreadInput +
# SetForegroundWindow/ShowWindow(SW_RESTORE) — иначе Windows не даёт фоновому
# процессу захватить фокус.

#: Классы окон проводника: папка (Win7+) и старое окно «Проводник».
EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")
#: Сколько ждать появления окна проводника, сек.
FOREGROUND_WAIT_S = 2.5
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002


def _shell_blocked() -> bool:
    """Тесты и headless-прогоны не запускают проводник (`TEAMROOM_NO_SHELL=1`)."""
    return (os.environ.get("TEAMROOM_NO_SHELL") or "").strip().lower() in ("1", "true", "yes")


def _launch_explorer(target: str) -> bool:
    """Открыть папку в сессии пользователя. Без оболочки cmd — путь не идёт в неё."""
    if hasattr(os, "startfile"):                    # Windows: ShellExecute
        try:
            os.startfile(target)                    # type: ignore[attr-defined]
            return True
        except (OSError, ValueError):
            pass
    try:
        if proc.IS_WINDOWS:
            # Запасной путь на Windows. gui=True: окно проводника видно, а
            # консольного окна у него нет.
            proc.popen(["explorer.exe", target], gui=True)
        elif not proc.open_path(target):            # macOS — open, Linux — xdg-open
            return False
    except (OSError, ValueError, safety.SafetyError):
        return False
    return True


def _find_explorer_window(folder_name: str) -> int:
    """HWND видимого окна проводника с этой папкой в заголовке (0 — не нашли)."""
    if sys.platform != "win32" or not folder_name:
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except (ImportError, AttributeError, OSError):
        return 0
    needle = str(folder_name).casefold()
    hits: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _lparam):                      # noqa: ANN001 - колбэк Win32
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = int(user32.GetWindowTextLengthW(hwnd))
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if needle not in str(title.value or "").casefold():
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if str(cls.value or "") in EXPLORER_CLASSES:
                hits.append(int(hwnd))
                return False                        # нашли первое — хватит
        except Exception:                           # noqa: BLE001 - окно могло закрыться
            return True
        return True

    try:
        user32.EnumWindows(_visit, 0)
    except (OSError, AttributeError):
        return 0
    return hits[0] if hits else 0


def _force_foreground(hwnd: int) -> bool:
    """Вывести окно вперёд и убедиться, что оно переднее. Alt-трюк + AttachThreadInput."""
    if not hwnd or sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
    except (ImportError, AttributeError, OSError):
        return False
    fg = int(user32.GetForegroundWindow() or 0)
    if fg == hwnd:
        return True
    attached: list = []
    try:
        # Нажатый Alt снимает «замок фокуса» (SystemParametersInfo foreground lock).
        user32.keybd_event(VK_MENU, 0, 0, 0)
        tid_me = int(ctypes.windll.kernel32.GetCurrentThreadId())
        for tid in {int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0,
                    int(user32.GetWindowThreadProcessId(hwnd, None))}:
            if tid and tid != tid_me and user32.AttachThreadInput(tid_me, tid, True):
                attached.append(tid)
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
    except (OSError, AttributeError):
        return False
    finally:
        for tid in attached:
            try:
                user32.AttachThreadInput(ctypes.windll.kernel32.GetCurrentThreadId(), tid, False)
            except (OSError, AttributeError):
                pass
        try:
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except (OSError, AttributeError):
            pass
    try:
        return int(user32.GetForegroundWindow() or 0) == hwnd
    except (OSError, AttributeError):
        return False


def _raise_explorer(folder_name: str, wait_s: float = FOREGROUND_WAIT_S) -> dict:
    """Дождаться окна проводника с папкой и вывести его вперёд.

    `{found, foreground}`. Чужие окна не трогаем: ищем только окно проводника,
    в заголовке которого есть имя нашей папки.
    """
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while True:
        hwnd = _find_explorer_window(folder_name)
        if hwnd:
            if _force_foreground(hwnd):
                return {"found": True, "foreground": True}
            if time.monotonic() >= deadline:
                return {"found": True, "foreground": False}
        elif time.monotonic() >= deadline:
            return {"found": False, "foreground": False}
        time.sleep(0.08)


def open_dir_report(cfg, path, *, create: bool = False) -> dict:
    """Открыть папку проекта: `{ok, path, foreground, reason, can_create}`.

    Только для разрешённых путей (см. `check_dir`): системные каталоги, Hermes и
    `data/` приложения отклоняются там же — наугад открывать ничего нельзя.
    `create=True` — сначала создать папку (`ensure_dir`), это единственная запись.
    """
    report = {"ok": False, "path": "", "foreground": False,
              "reason": "", "can_create": False}
    if _shell_blocked():
        report["reason"] = "Открытие папки отключено в тестовом режиме"
        return report
    result = check_dir(cfg, path)
    report["path"] = result.get("normalized") or ""
    if not result["allowed"]:
        report["reason"] = result.get("reason") or "Путь не подходит"
        return report
    target = result["normalized"]
    report["path"] = target
    if not result["exists"]:
        if not create:
            report["reason"] = "Папки нет на диске — её можно создать"
            report["can_create"] = True
            return report
        try:
            ensure_dir(cfg, target)
        except DirError as exc:
            report["reason"] = exc.message
            return report
        try:
            if not Path(target).is_dir():
                report["reason"] = "Папку не удалось создать — проверьте путь"
                return report
        except OSError:
            report["reason"] = "Папку не удалось создать — проверьте путь"
            return report
    if sys.platform != "win32":
        report["reason"] = "Открыть папку можно только в Windows"
        return report
    if not _launch_explorer(target):
        report["reason"] = "Не удалось открыть папку: проводник не запустился"
        return report
    raised = _raise_explorer(ntpath.basename(target))
    report["ok"] = True
    report["foreground"] = bool(raised["foreground"])
    if not raised["foreground"]:
        report["reason"] = ("Папка открылась, но окно проводника не вышло вперёд — "
                            "найдите его на панели задач")
    return report


def open_dir(cfg, path) -> bool:
    """«Открыть папку проекта» → True, если папка открылась в проводнике."""
    return bool(open_dir_report(cfg, path).get("ok"))


# ────────────────────────────────────────────── имена и подпапки запросов

def safe_segment(text, fallback: str = "Запрос") -> str:
    """Имя папки из названия: без запрещённых символов, точек на конце, ≤60 знаков."""
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(text or ""))
    raw = re.sub(r"\s+", " ", raw).strip(" .-")
    raw = raw[:MAX_SEGMENT_LEN].strip(" .")
    if not raw:
        return fallback
    if raw.split(".")[0].strip().lower() in RESERVED_NAMES:
        raw = ("%s %s" % (fallback, raw))[:MAX_SEGMENT_LEN].strip(" .")
    return raw or fallback


def request_subdir(title: str, created_ts: int) -> str:
    """Подпапка запроса в папке проекта: «YYYY-MM-DD название» (локальное время)."""
    try:
        stamp = int(created_ts or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        stamp = int(time.time())
    try:
        day = _dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        day = _dt.datetime.now().strftime("%Y-%m-%d")
    return day + " " + safe_segment(title)


# ────────────────────────────────────────────────── содержание README.md

def _count_files(folder: Path, limit: int = 99) -> int:
    total = 0
    try:
        for item in folder.iterdir():
            if item.is_file():
                total += 1
                if total >= limit:
                    break
    except OSError:
        return total
    return total


def _form_files(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "%d файл" % count
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "%d файла" % count
    return "%d файлов" % count


def index_block(room: dict, folders: list) -> str:
    """Служебный блок «Содержание»: заголовок + список подпапок (свежие сверху)."""
    title = str((room or {}).get("title") or (room or {}).get("id") or "").strip()
    lines = [BEGIN_INDEX, "## Содержание", ""]
    if title:
        lines.append("Папка проекта комнаты «%s». Список обновляется автоматически; "
                     "всё, что вы напишете ниже, сохранится." % title)
    else:
        lines.append("Список обновляется автоматически; всё, что вы напишете ниже, сохранится.")
    lines.append("")
    if folders:
        lines.append("Последние папки запросов:")
        lines.append("")
        for name, count in folders:
            lines.append("- %s — %s" % (name, _form_files(count)))
    else:
        lines.append("Папок запросов пока нет: они появятся, когда команда начнёт работу.")
    lines.append(END_INDEX)
    return "\n".join(lines)


def merge_index(existing: str, block: str) -> str:
    """Заменить только блок между маркерами; ручной текст вне маркеров не трогаем."""
    text = existing or ""
    start = text.find(BEGIN_INDEX)
    if start >= 0:
        stop = text.find(END_INDEX, start)
        if stop >= 0:
            return text[:start] + block + text[stop + len(END_INDEX):]
    head = text.rstrip("\n")
    if head:
        return head + "\n\n" + block + "\n"
    return block + "\n"


def _folders_of(root: Path, limit: int = 20) -> list:
    items = []
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(".") or name.startswith("$"):
                continue
            items.append((name, _count_files(child)))
    except OSError:
        return []
    items.sort(key=lambda pair: pair[0], reverse=True)
    return items[:limit]


def update_index(cfg, room) -> bool:
    """Обновить «Содержание» в `<project_dir>\\README.md`. True — файл изменился."""
    if isinstance(room, str):
        room = {"project_dir": room}
    project_dir = str((room or {}).get("project_dir") or "").strip()
    if not project_dir:
        return False
    result = check_dir(cfg, project_dir)
    if not result["allowed"]:
        return False
    root = Path(result["normalized"])
    try:
        if not root.is_dir():
            return False               # папку создаёт ensure_dir, не «Содержание»
    except OSError:
        return False
    if safety.is_under_real(root):
        return False                   # реальный Vault/Hermes — только чтение (§1 WAVE7)
    readme = root / INDEX_FILE
    try:
        existing = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    except OSError:
        existing = ""
    text = merge_index(existing, index_block(room or {}, _folders_of(root)))
    if text == existing:
        return False
    try:
        return sf.write_text_if_changed(readme, text, newline="\n")
    except OSError:
        return False
