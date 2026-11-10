"""«Проверка системы»: что настроено, что нет и что сделать (WAVE 12 §12.2).

Один вызов ``report(cfg)`` собирает список пунктов проверки. Каждый пункт —
это ``{id, title, status, value, hint, fix}``, где ``status`` один из
``ok | warn | fail | off``, а ``fix`` — точное действие, которое снимет
проблему (скопировать команду, открыть документацию, перейти в настройки).

Правила, зашитые в модуль:
  * ничего не предполагаем про машину пользователя — всё читается и честно
    показывается (WAVE 11 §12);
  * реальный Hermes и доска только читаются: файлы не пишутся, CLI не
    запускается в песочнице, запись в БД не пробуется;
  * наружу не уходят ни секреты, ни полные пути: только ``~``-стиль и
    относительные пути, журналы — с маскировкой и обрезкой до 200 символов;
  * проверка не падает: любая ошибка чтения превращается в статус, а не в 500.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import sqlite3
import sys
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from . import config
    from . import http_util
except ImportError:  # pragma: no cover
    import config  # type: ignore
    import http_util  # type: ignore

STATUSES = ("ok", "warn", "fail", "off")

MAX_LOG_LINE = 200          # символов на строку журнала в отчёте
LOG_TAIL_BYTES = 64 * 1024  # читаем хвост журнала, а не весь файл
LOG_FILES = 5
CLI_CACHE_TTL = 300.0       # сек: версия CLI не меняется на каждом обновлении экрана

ERROR_MARK = re.compile(r"(?i)\b(error|critical|traceback|exception)\b|ошибк")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
CLI_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+){0,2})")
PLUGIN_VERSION_RE = re.compile(r"PLUGIN_VERSION\s*=\s*['\"]([^'\"]{1,32})['\"]")
WIN_ABS_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|,;)]*")
USER_ABS_RE = re.compile(r"/(?:Users|home)/[^\s\"'<>|,;)]*")

_CLI_VERSION: dict = {}


# --------------------------------------------------------------- приватность

def home_dir() -> Path:
    """Домашний каталог пользователя (на Windows — USERPROFILE)."""
    return Path(os.environ.get("USERPROFILE") or Path.home()).expanduser()


def safe_path(value, base=None) -> str:
    """Путь для показа: ``~/…``, относительный от ``base`` или ``<путь>``.

    Полные пути пользователя наружу не отдаём — только то, что помогает
    узнать место, но не раскрывает имя учётной записи и раскладку диска.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text)
    except (TypeError, ValueError):
        return "<путь>"
    try:
        rel = path.relative_to(home_dir())
        tail = rel.as_posix()
        return "~" if tail in ("", ".") else "~/" + tail
    except (ValueError, OSError):
        pass
    if base:
        try:
            rel = path.relative_to(Path(base))
            tail = rel.as_posix()
            return "." if tail in ("", ".") else tail
        except (ValueError, OSError):
            pass
    return "<путь>"


def redact(text) -> str:
    """Строка журнала без секретов, путей пользователя и переводов строк (≤200)."""
    line = str(text or "").replace("\r", " ").replace("\n", " ")
    line = http_util.mask_secrets(line)
    home = str(home_dir())
    if home:
        for variant in (home, home.replace("\\", "/")):
            line = line.replace(variant, "~")
    line = WIN_ABS_RE.sub("<путь>", line)
    line = USER_ABS_RE.sub("<путь>", line)
    line = re.sub(r"\s{2,}", " ", line).strip()
    return line[:MAX_LOG_LINE]


# ------------------------------------------------------------------- версия

def app_version(app_dir=None) -> tuple:
    """``(версия, источник)``: файл ``VERSION`` в корне программы или config.VERSION.

    Файл может отсутствовать (CIREL ещё не собрал релиз) — тогда честно берём
    номер из программы, а не выдумываем.
    """
    base = Path(app_dir) if app_dir else Path(getattr(config, "APP_DIR", "."))
    for name in ("VERSION", "version.txt"):
        try:
            text = (base / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            value = line.strip().lstrip("\ufeff")
            if value and VERSION_RE.match(value):
                return value, name
    return str(getattr(config, "VERSION", "") or ""), "config"


def parse_cli_version(text) -> str:
    """Первая строка вывода ``hermes --version`` с номером → ``1.2.3`` ('' если нет)."""
    for line in str(text or "").splitlines():
        found = CLI_VERSION_RE.search(line)
        if found:
            return found.group(1)
    return ""


def parse_plugin_version(text) -> str:
    """``PLUGIN_VERSION = '1.0.0'`` из plugin.js → ``1.0.0``."""
    found = PLUGIN_VERSION_RE.search(str(text or ""))
    return found.group(1) if found else ""


# ------------------------------------------------------------------ журналы

def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _tail_text(path: Path) -> str:
    """Последние ``LOG_TAIL_BYTES`` файла (журналы бывают большими)."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - LOG_TAIL_BYTES))
        raw = fh.read()
    return raw.decode("utf-8", "replace")


def read_log_errors(log_dir, limit: int = 5) -> list:
    """Последние ``limit`` строк с ошибками из ``*.log`` — без путей и секретов."""
    folder = Path(str(log_dir or ""))
    try:
        files = [p for p in folder.glob("*.log") if p.is_file()]
    except OSError:
        return []
    files.sort(key=_mtime)                 # старые → новые: «последние» в конце
    found: list = []
    for path in files[-LOG_FILES:]:
        try:
            text = _tail_text(path)
        except OSError:
            continue
        for line in text.splitlines():
            if not ERROR_MARK.search(line):
                continue
            cleaned = redact(line)
            if cleaned and cleaned not in found:
                found.append(cleaned)
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = 5
    return found[-count:]


# ------------------------------------------------------------- помощники

def _is_dir(path) -> bool:
    try:
        return Path(path).is_dir()
    except (OSError, ValueError, TypeError):
        return False


def _resolve_cli(command: str) -> str:
    """Реальный файл ``hermes`` по имени из PATH или абсолютному пути ('' если нет)."""
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        if os.path.isabs(text) or "/" in text or "\\" in text:
            return text if Path(text).is_file() else ""
        return shutil.which(text) or ""
    except (OSError, ValueError):
        return ""


def cli_version(path: str, *, sandbox: bool = False) -> str:
    """Версия CLI из ``hermes --version`` (кэш 5 мин, без окна, в песочнице — никогда)."""
    key = str(path or "")
    if not key or sandbox:
        return ""
    now = time.monotonic()
    cached = _CLI_VERSION.get(key)
    if cached and now - cached[0] < CLI_CACHE_TTL:
        return cached[1]
    version = ""
    try:
        try:
            from . import proc as spawn
        except ImportError:  # «плоский» импорт (тесты, CLI)
            import proc as spawn  # type: ignore
        res = spawn.run([key, "--version"], timeout=5, capture_output=True)
        raw = b""
        for attr in ("stdout", "stderr"):
            raw += getattr(res, attr, b"") or b""
        version = parse_cli_version(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — версия CLI не повод падать проверке
        version = ""
    _CLI_VERSION[key] = (now, version)
    return version


def _count_tasks(db: Path) -> tuple:
    """``(число карточек, текст ошибки)`` — только чтение (``mode=ro``)."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True, timeout=3)
    except sqlite3.Error as exc:
        return 0, str(exc)
    try:
        row = con.execute("SELECT count(*) FROM tasks").fetchone()
        return int(row[0] if row else 0), ""
    except sqlite3.Error as exc:
        return 0, str(exc)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def _cards_ru(count: int) -> str:
    n = int(count or 0)
    if n % 10 == 1 and n % 100 != 11:
        word = "карточка"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "карточки"
    else:
        word = "карточек"
    return "%d %s" % (n, word)


def _port_free(port: int) -> bool:
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.4)
        sock.bind(("127.0.0.1", int(port)))
        return True
    except (OSError, ValueError):
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _mirror_count(root: Path) -> int:
    """Сколько комнат уже отражено в Vault (есть STATE.md)."""
    total = 0
    try:
        for entry in root.iterdir():
            if total >= 500:
                break
            if entry.is_dir() and (entry / "STATE.md").is_file():
                total += 1
    except OSError:
        return 0
    return total


def _scheduled_task(name: str) -> bool:
    """Есть ли задача планировщика (только чтение, без окна)."""
    try:
        try:
            from . import proc as spawn
        except ImportError:
            import proc as spawn  # type: ignore
        res = spawn.run(["schtasks", "/Query", "/TN", name], timeout=4, capture_output=True)
        return int(getattr(res, "returncode", 1) or 0) == 0
    except Exception:  # noqa: BLE001 — нет планировщика/песочница
        return False


def _autostart_entry() -> str:
    """Как именно настроен автозапуск ('' — не настроен)."""
    home = home_dir()
    folders = [home / "Library" / "LaunchAgents", home / ".config" / "systemd" / "user"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        folders.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                name = entry.name.lower()
                if "team-room" not in name and "teamroom" not in name and "team_room" not in name:
                    continue
                if entry.suffix.lower() in (".lnk", ".desktop"):
                    return "ярлык в «Автозагрузке»"
                return entry.name
        except OSError:
            continue
    if os.name == "nt" and _scheduled_task("TeamRoomServer"):
        return "задача планировщика TeamRoomServer"
    return ""


def _stt_status(cfg) -> dict:
    """``stt.status(cfg)`` с защитой: нет модуля/ошибка → пустой словарь."""
    try:
        try:
            from . import stt as stt_mod
        except ImportError:
            import stt as stt_mod  # type: ignore
        res = stt_mod.status(cfg)
        return res if isinstance(res, dict) else {}
    except Exception:  # noqa: BLE001 — диктовка вторична для проверки
        return {}


# ------------------------------------------------------------------- пункты

def _item(item_id: str, title: str, status: str, value: str = "", hint: str = "",
          fix: dict | None = None, details=None) -> dict:
    return {"id": str(item_id), "title": str(title),
            "status": status if status in STATUSES else "warn",
            "value": str(value or ""), "hint": str(hint or ""),
            "fix": fix if isinstance(fix, dict) else None,
            "details": [str(line) for line in (details or [])][:5]}


def _command(label: str, command: str) -> dict:
    return {"kind": "command", "label": str(label), "value": str(command)}


def _doc(label: str = "Открыть документацию", anchor: str = "") -> dict:
    target = "docs/INSTALL.md" + ("#" + anchor if anchor else "")
    return {"kind": "doc", "label": str(label), "value": target}


def _route(label: str, target: str) -> dict:
    return {"kind": "route", "label": str(label), "value": str(target)}


def _link(label: str, url: str) -> dict:
    return {"kind": "link", "label": str(label), "value": str(url)}


def _version_check(version: str, source: str) -> dict:
    if not version:
        return _item("version", "Версия приложения", "warn", "не определена",
                     "Не нашлась ни файловая, ни встроенная версия — обновите установку.", _doc())
    hint = "Единый номер версии — файл VERSION в корне программы." if source != "config" \
        else "Файл VERSION не найден: номер взят из настроек программы (так бывает до релиза)."
    return _item("version", "Версия приложения", "ok", version, hint)


def _system_check() -> dict:
    os_name = ("%s %s" % (platform.system() or "", platform.release() or "")).strip() or "неизвестна"
    python = "%d.%d.%d" % tuple(sys.version_info[:3])
    ok = tuple(sys.version_info[:2]) >= (3, 11)
    return _item("system", "Система", "ok" if ok else "fail",
                 "%s · Python %s" % (os_name, python),
                 "Операционная система и Python, на которых работает приложение." if ok
                 else "Нужен Python 3.11 или новее — поставьте его с python.org и повторите установку.",
                 None if ok else _doc("Открыть документацию", "2-что-нужно-и-что-необязательно"))


def _hermes_home_check(cfg) -> dict:
    path = Path(str(getattr(cfg, "hermes_home", "") or "."))
    shown = safe_path(path)
    if not _is_dir(path):
        return _item("hermes_home", "Hermes Desktop", "warn", "не найден",
                     "Папка Hermes не найдена: комнаты откроются, но профили и настройки агентов брать неоткуда.",
                     _doc())
    looks = any((path / name).exists() for name in ("config.yaml", "profiles", "kanban", "cache"))
    if not looks:
        return _item("hermes_home", "Hermes Desktop", "warn", shown,
                     "Папка есть, но не похожа на Hermes: нет config.yaml, profiles или kanban.", _doc())
    return _item("hermes_home", "Hermes Desktop", "ok", shown,
                 "Настройки и профили читаются только на чтение — приложение их не меняет.")


def _cli_check(cfg) -> dict:
    command = str(getattr(cfg, "hermes_cli", "") or "").strip()
    found = _resolve_cli(command)
    if not found:
        return _item("hermes_cli", "Программа hermes", "warn", "не найдена",
                     "Без неё комната только показывает доску: новые запросы и карточки не создаются.",
                     _doc("Открыть документацию", "2-что-нужно-и-что-необязательно"))
    version = cli_version(found, sandbox=bool(getattr(cfg, "sandbox", False)))
    value = safe_path(found) + (" · версия " + version if version else " · версия не определилась")
    return _item("hermes_cli", "Программа hermes", "ok", value,
                 "Через неё приложение записывает задачи в доску Hermes.")


def _board_check(cfg, board: str) -> dict:
    try:
        db = Path(cfg.kanban_db(board))
    except (OSError, ValueError, TypeError):
        return _item("board", "Доска задач", "fail", "путь к доске не определён",
                     "Комната не знает, где лежит доска Hermes.", _doc())
    if not db.is_file():
        return _item("board", "Доска задач", "fail", "файл доски не найден",
                     "Доски «%s» нет: список задач будет пустым, пока Hermes её не создаст." % board,
                     _doc())
    total, error = _count_tasks(db)
    if error:
        return _item("board", "Доска задач", "fail", "доска не читается",
                     "Файл доски есть, но открыть его не удалось: " + redact(error)[:120], _doc())
    cards = _cards_ru(total)
    try:
        writable = os.access(str(db), os.W_OK)
    except (OSError, ValueError):
        writable = False
    can_write = bool(writable and _resolve_cli(str(getattr(cfg, "hermes_cli", "") or "")))
    if can_write:
        return _item("board", "Доска задач", "ok", cards + " · чтение и запись",
                     "Запись идёт через программу hermes, напрямую в базу приложение не пишет.")
    return _item("board", "Доска задач", "warn", cards + " · только чтение",
                 "Карточки видны, но новые не создать: не найдена программа hermes.",
                 _doc("Открыть документацию", "2-что-нужно-и-что-необязательно"))


def _dictation_check(cfg) -> dict:
    data = _stt_status(cfg)
    mode = str(data.get("mode") or "none")
    model = str(data.get("model") or "")
    device = str(data.get("device") or "")
    provider = str(data.get("provider") or "")
    if mode == "local":
        value = "локально" + (" · модель " + model if model else "")
        if device and device != "cloud":
            value += " · " + device
        return _item("dictation", "Диктовка", "ok", value,
                     "Аудио не покидает компьютер; распознаёт faster-whisper.")
    if mode == "provider":
        value = "через Hermes" + (" · " + provider if provider else "")
        if model:
            value += " · " + model
        return _item("dictation", "Диктовка", "ok", value,
                     "Аудио уходит тому сервису распознавания, который вы выбрали в Hermes.")
    reason = str(data.get("reason") or "Диктовка не настроена: нет ни локального распознавания, "
                                       "ни провайдера в Hermes.").strip()
    return _item("dictation", "Диктовка", "off", "не настроена", reason[:400],
                 _doc("Как включить", "2-что-нужно-и-что-необязательно"))


def _vault_check(cfg) -> dict:
    if not bool(getattr(cfg, "vault_enabled", True)):
        return _item("vault", "Vault и зеркало", "off", "зеркало выключено",
                     str(getattr(config, "VAULT_HINT_RU", "") or "Зеркало в Obsidian выключено."),
                     _doc("Открыть документацию", "2-что-нужно-и-что-необязательно"))
    root = Path(str(getattr(cfg, "vault_dir", "") or "."))
    if not root.exists():
        return _item("vault", "Vault и зеркало", "warn", "папка ещё не создана",
                     "Зеркало появится после первого готового ответа: " + safe_path(root),
                     _doc("Открыть документацию", "2-что-нужно-и-что-необязательно"))
    count = _mirror_count(root)
    return _item("vault", "Vault и зеркало", "ok",
                 "%s · комнат в зеркале: %d" % (safe_path(root), count),
                 "STATE.md и Журнал.md обновляются после каждого готового ответа.")


def _project_check(room: dict) -> dict:
    title = str(room.get("title") or room.get("id") or "комната")
    room_id = str(room.get("id") or "").strip()
    target = "#/room/%s/composer" % room_id if room_id else "#/"
    raw = str(room.get("project_dir") or "").strip()
    if not raw:
        return _item("project", "Папка проекта", "warn", "не задана",
                     "Комната «%s» работает без папки: файлы проекта не читаются." % title,
                     _route("Открыть комнату", target))
    path = Path(raw)
    if _is_dir(path):
        return _item("project", "Папка проекта", "ok", safe_path(path),
                     "Файлы проекта комнаты «%s» доступны." % title)
    return _item("project", "Папка проекта", "warn", safe_path(path),
                 "Папка комнаты «%s» не найдена — проверьте путь в настройках комнаты." % title,
                 _route("Открыть комнату", target))


def _port_check(cfg, port) -> dict:
    try:
        current = int(port if port is not None else getattr(cfg, "port", 0) or 0)
    except (TypeError, ValueError):
        current = 0
    if current <= 0:
        return _item("port", "Порт", "warn", "неизвестен",
                     "Порт задаётся при запуске приложения.", _doc())
    nxt = current + 1 if current < 65535 else current - 1
    free = _port_free(nxt)
    value = "%d · следующий %d %s" % (current, nxt, "свободен" if free else "занят")
    if free:
        return _item("port", "Порт", "ok", value,
                     "Приложение слушает только этот компьютер (127.0.0.1).")
    return _item("port", "Порт", "warn", value,
                 "Второй экземпляр не запустится на %d: закройте лишнее или задайте TEAMROOM_PORT." % nxt,
                 _doc("Открыть документацию", "8-типичные-проблемы"))


def _autostart_check() -> dict:
    entry = _autostart_entry()
    if entry:
        return _item("autostart", "Автозапуск", "ok", entry,
                     "Сервер поднимается сам при входе в систему и пишет журнал в data/logs.")
    return _item("autostart", "Автозапуск", "off", "не настроен",
                 "Приложение запускается вручную (start-team-room.cmd). Автозапуск — по желанию, "
                 "установщик ставит его отдельным шагом.",
                 _doc("Как включить", "3-параметры-установщика"))


def _plugin_check(cfg) -> dict:
    home = Path(str(getattr(cfg, "hermes_home", "") or "."))
    file = home / "desktop-plugins" / "team-room" / "plugin.js"
    if not file.is_file():
        return _item("plugin", "Плагин в Hermes Desktop", "warn", "не установлен",
                     "Комнаты открываются в браузере; внутри Hermes Desktop их не будет, пока "
                     "плагин не установлен.",
                     _command("Скопировать команду", "python tools/install_desktop_plugin.py"))
    version = ""
    try:
        with open(file, "r", encoding="utf-8", errors="replace") as fh:
            version = parse_plugin_version(fh.read(4096))
    except OSError:
        version = ""
    return _item("plugin", "Плагин в Hermes Desktop", "ok",
                 "установлен" + (" · версия " + version if version else ""),
                 "Открывается из левого меню Hermes: «Командные комнаты».")


def _logs_check(cfg) -> dict:
    errors = read_log_errors(getattr(cfg, "log_dir", ""), limit=5)
    if not errors:
        return _item("logs", "Последние ошибки", "ok", "ошибок нет",
                     "Журналы приложения чистые. Пути и ключи в отчёт не попадают.")
    return _item("logs", "Последние ошибки", "warn",
                 "%s %s" % (len(errors), _records_ru(len(errors))),
                 "Пути и ключи скрыты, строки обрезаны до 200 символов. Полный журнал — по кнопке.",
                 _link("Открыть журнал", "/api/logs?name=app.log&tail=200"), details=errors)


def _records_ru(count: int) -> str:
    """«1 запись / 3 записи / 5 записей» для пункта об ошибках в журнале."""
    n = int(count or 0)
    if n % 10 == 1 and n % 100 != 11:
        return "запись"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "записи"
    return "записей"


def _disk_check(cfg) -> dict:
    try:
        usage = shutil.disk_usage(str(getattr(cfg, "data_dir", "") or "."))
    except (OSError, ValueError, TypeError):
        return _item("disk", "Место на диске", "warn", "не удалось узнать",
                     "Проверьте свободное место на диске с данными вручную.", None)
    free_gb = usage.free / float(1024 ** 3)
    total_gb = usage.total / float(1024 ** 3)
    value = "%s ГБ свободно из %s ГБ" % (_num(free_gb), _num(total_gb))
    if free_gb >= 5:
        return _item("disk", "Место на диске", "ok", value,
                     "Здесь лежат данные, отчёты и журналы приложения.")
    if free_gb >= 1:
        return _item("disk", "Место на диске", "warn", value,
                     "Меньше 5 ГБ: большие отчёты и выгрузки могут не сохраниться.",
                     _doc("Открыть документацию", "8-типичные-проблемы"))
    return _item("disk", "Место на диске", "fail", value,
                 "Меньше 1 ГБ: ответы и отчёты могут не записаться — освободите место.",
                 _doc("Открыть документацию", "8-типичные-проблемы"))


def _num(value: float) -> str:
    return ("%.1f" % float(value)).replace(".", ",")


# ------------------------------------------------------------------ сводка

def summarize(checks) -> dict:
    """Сколько пунктов в каждом статусе (для сводки на экране)."""
    counts = {"ok": 0, "warn": 0, "fail": 0, "off": 0}
    items = [item for item in (checks or []) if isinstance(item, dict)]
    for item in items:
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(items)
    return counts


def report(cfg, *, port=None, board: str = "team", room: dict | None = None,
           app_dir=None) -> dict:
    """Собрать проверку системы: ``{ok, app, summary, checks, generated_at}``."""
    started = time.monotonic()
    version, source = app_version(app_dir)
    checks = [
        _version_check(version, source),
        _system_check(),
        _hermes_home_check(cfg),
        _cli_check(cfg),
        _board_check(cfg, board),
        _dictation_check(cfg),
        _vault_check(cfg),
    ]
    if isinstance(room, dict):
        checks.append(_project_check(room))
    checks += [
        _port_check(cfg, port),
        _autostart_check(),
        _plugin_check(cfg),
        _logs_check(cfg),
        _disk_check(cfg),
    ]
    summary = summarize(checks)
    return {
        "ok": summary["fail"] == 0,
        "generated_at": int(time.time()),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "app": {
            "name": "Комната команды",
            "version": version,
            "os": ("%s %s" % (platform.system() or "", platform.release() or "")).strip(),
            "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
            "sandbox": bool(getattr(cfg, "sandbox", False)),
        },
        "summary": summary,
        "checks": checks,
    }
