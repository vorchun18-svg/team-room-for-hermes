"""«Ход работы» запроса — поток событий агентов (WAVE20 §1.6, WAVE18 пакет W).

Задача (06.11): «как в Hermes Agent — видно процесс работы и рассуждения, можно
включать/отключать в настройках комнаты; плюс сторож — промежуточный итог раз в пару
минут, даже когда рассуждения выключены».

Что делает модуль
-----------------
* читает сеансы участников запроса **только на чтение** (`sqlite mode=ro`): рассуждения,
  вызовы инструментов, результаты, реплики агентов (`state.db` профилей);
* берёт карточки дерева запроса из `kanban.db` (тоже `mode=ro`) — кто принял, кому что
  передали, что готово;
* переводит всё в человеческий русский текст (никаких id и дампов на экране);
* отдаёт ленту курсором: `items` после `since` не теряются и не повторяются;
* маскирует секреты единой `agent_notes.mask_secrets()` — на КАЖДЫЙ показываемый текст;
* считает промежуточный итог («дайджест») и находит возможно зависшие карточки — оба
  без модели, 0 токенов.

Публичные функции
-----------------
    room_settings(room) -> {mode, digest_every_min, level, on, ...}
    worklog(cfg, room, root_id, since="", level="", limit=50, extra=None) -> dict
    collect(cfg, room, root_id, since="", level="", limit=50, extra=None) -> dict
    live(cfg, room, root_id, extra=None) -> list[dict]
    stuck_cards(cfg, room, root_id, extra=None) -> list[dict]
    digest_step(cfg, room, record, detail) -> dict        # шаг сторожа из watcher.py
    digest_for(cfg, room_id, root_id) -> dict | None
    mask(text) -> str
    human_tool(name, args) -> str
    encode_cursor(state) / decode_cursor(text) -> dict

Настройки комнаты (`rooms.worklog_mode`, `rooms.digest_every_min`) — источник истины;
неизвестное значение в реестре читается как «подробно» и «раз в 2 минуты».

Ничего не пишет ни в `state.db`, ни в `kanban.db`: единственная запись — файл дайджеста
в СВОЁМ каталоге данных (`data/state/<комната>/worklog_digest.json`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

try:                                        # пакетный импорт
    from . import agent_models as am
    from . import agent_notes as an
    from . import rooms as rooms_mod
    from . import statefiles as sf
except ImportError:                          # python server/app.py (плоский импорт)
    import agent_models as am             # type: ignore
    import agent_notes as an              # type: ignore
    import rooms as rooms_mod             # type: ignore
    import statefiles as sf               # type: ignore

# ---------------------------------------------------------------- константы

LEVELS = ("thoughts", "steps")
DEFAULT_LEVEL = "thoughts"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: сколько строк одного сеанса читаем за раз (страховка от сеанса на 20 000 сообщений)
MAX_ROWS_PER_SESSION = 400
#: сколько знаков показываем от рассуждения / результата / реплики
MAX_THOUGHT = 300
MAX_RESULT = 200
MAX_MESSAGE = 400
#: карточка без активности дольше этого — «возможно завис» (WAVE18 п.5)
STUCK_MIN = 15
#: статусы, в которых карточка «в работе»
RUNNING = ("running", "review")
#: статусы, в которых запрос ещё идёт (дайджест нужен только тут)
WORK_STATUSES = ("queued", "working", "reviewing", "synthesizing", "needs_you")
#: сколько записей дайджеста храним на комнату
DIGEST_KEEP = 80

ROLE_RU = {"orchestrator": "Руководитель", "critic": "Критик",
           "worker": "Исполнитель", "observer": "Наблюдатель"}
#: как выглядит карточка в статусе — короткая человеческая строка
STATUS_RU = {
    "triage": "разбирает",
    "todo": "в очереди",
    "ready": "готова к запуску",
    "queued": "в очереди",
    "blocked": "ждёт вашего ответа",
    "running": "работает",
    "review": "на проверке у критика",
    "done": "готово",
    "archived": "в архиве",
    "failed": "не удалось",
}


class WorklogError(Exception):
    """Ошибка запроса хода работы: code = bad_request|not_found (роут отдаст 4xx)."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _now() -> int:
    return int(time.time())


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ms(value) -> int:
    """Секунды (float из `state.db`) → миллисекунды: в одной секунде бывает 5 записей."""
    try:
        return int(round(float(value or 0) * 1000))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- текст

#: длинные «плотные» строки — почти наверняка ключ, подпись или base64
_DENSE_RE = re.compile(r"[A-Za-z0-9+/_=\-]{80,}")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{48,}\b")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
#: пути: `C:\...\файл.md`, `\\сервер\...`, `/home/...`
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s\"'«»<>|]+")
#: Ключи доступа AWS (20 знаков, цифры и заглавные буквы): `agent_notes` их не знает,
#: а в рассуждениях агента они встречаются («aws configure»).
_AWS_RE = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{12,20}\b")
#: Пароль внутри адреса подключения: `postgres://user:пароль@хост/…`, `mysql://…`
_DSN_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)")
#: Токен бота телеграма: `1234567890:AAH…`
_BOT_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b")
#: Пароль в коротком флаге клиента: `mysql -pSuperSecret123`, `sshpass -p 'Tr0ub4dor'`.
#: Порты (`-p 22`, `-p8080`) и обычные флаги (`-project`) не трогаем.
_SHORT_P_RE = re.compile(r"""(?<![A-Za-z0-9_])-p\s?("[^"]{3,}"|'[^']{3,}'|[^\s"'-]{4,})""")
#: Секрет в переменной окружения: `AWS_SECRET_ACCESS_KEY=…`, `OPENAI_API_KEY=…`.
_ENV_SECRET_RE = re.compile(
    r"""\b([A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY|PRIVATE_KEY)"""
    r"""[A-Za-z0-9_]*\s*=\s*)("[^"]*"|'[^']*'|[^\s]+)""")


def short_path(path: str, keep: int = 2) -> str:
    """Путь → последние `keep` частей («…\\Отчёты\\свод.md»): полный путь на экране не нужен."""
    text = str(path or "").strip().strip('"').strip("'")
    if not text:
        return ""
    parts = re.split(r"[\\/]+", text)
    parts = [p for p in parts if p]
    if len(parts) <= keep:
        return text
    return "…\\" + "\\".join(parts[-keep:])


def trim(text: str, limit: int, tail: str = "… ещё") -> str:
    """Обрезать по границе слова и честно сказать, что дальше есть ещё."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—–-") + " " + tail


def _mask_short_password(match) -> str:
    """`-pSecret` и `-p'Secret'` → `-p[скрыто]`; `-p8080`, `-project` оставляем как есть."""
    value = match.group(1) or ""
    core = value.strip("'\"")
    looks_secret = (len(core) >= 6 and any(ch.isdigit() for ch in core)
                    and any(ch.isalpha() for ch in core))
    if value[:1] in ("'", "\"") and len(core) >= 3:
        looks_secret = True
    return "-p" + an.SECRET_MASK if looks_secret else match.group(0)


def mask(text) -> str:
    """Спрятать ключи и «плотные» строки. Одна маска на всё, что видит пользователь."""
    out = "" if text is None else str(text)
    out = an.mask_secrets(out)                 # sk-…, ghp_…, password=…, Bearer …
    out = _ENV_SECRET_RE.sub(lambda m: m.group(1) + an.SECRET_MASK, out)
    out = _SHORT_P_RE.sub(_mask_short_password, out)
    out = _AWS_RE.sub(an.SECRET_MASK, out)
    out = _BOT_RE.sub(an.SECRET_MASK, out)
    out = _DSN_RE.sub(lambda m: m.group(1) + an.SECRET_MASK + m.group(3), out)
    out = _HEX_RE.sub(an.SECRET_MASK, out)
    out = _DENSE_RE.sub(an.SECRET_MASK, out)
    out = _CTRL_RE.sub(" ", out)
    out = _PATH_RE.sub(lambda m: short_path(m.group(0)), out)
    return _WS_RE.sub(" ", out).strip()


def one_line(text, limit: int = 0) -> str:
    out = mask(text)
    return trim(out, limit) if limit else out


# ---------------------------------------------------------------- инструменты

#: глагол для строки «что делает» — без технического имени инструмента
TOOL_VERBS = {
    "read_file": ("читает файл", "path"),
    "view": ("смотрит файл", "path"),
    "write_file": ("записывает файл", "path"),
    "create_file": ("создаёт файл", "path"),
    "patch": ("правит файл", "path"),
    "edit_file": ("правит файл", "path"),
    "apply_patch": ("правит файл", "path"),
    "search_files": ("ищет в файлах", "pattern"),
    "grep": ("ищет в файлах", "pattern"),
    "glob": ("ищет файлы", "pattern"),
    "terminal": ("запускает команду", "command"),
    "shell": ("запускает команду", "command"),
    "bash": ("запускает команду", "command"),
    "execute_code": ("выполняет код", ""),
    "python": ("выполняет код", ""),
    "web_search": ("ищет в сети", "query"),
    "web_extract": ("открывает страницу", "url"),
    "web_fetch": ("открывает страницу", "url"),
    "browser_exec": ("работает в браузере", ""),
    "browser": ("работает в браузере", ""),
    "kanban_create": ("ставит задачу", "title"),
    "kanban_update": ("правит карточку", "task_id"),
    "kanban_comment": ("пишет в карточку", "text"),
    "kanban_claim": ("принимает карточку", "task_id"),
    "kanban_complete": ("закрывает карточку", "task_id"),
    "skill_view": ("смотрит навык", "name"),
    "skill_manage": ("правит навык", "name"),
    "delegate_task": ("передаёт работу", ""),
    "todo_write": ("обновляет план", ""),
    "todo_list": ("смотрит план", ""),
    "vision_analyze": ("смотрит изображение", "image_url"),
    "session_search": ("ищет в прошлых беседах", "query"),
    "agent_models": ("смотрит модель участника", ""),
}
#: ключи аргументов, в которых лежит «объект действия» (по порядку важности)
_OBJ_KEYS = ("path", "file_path", "filename", "file", "pattern", "query", "url",
             "command", "title", "name", "task_id", "text")


def _json_args(raw):
    """Аргументы вызова: dict, JSON-строка или мусор → dict (никогда не падаем)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError:
            return {"_raw": text}
        return value if isinstance(value, dict) else {}
    return {}


def tool_calls(raw) -> list[tuple[str, dict]]:
    """Разобрать `messages.tool_calls` в список `(имя, аргументы)`.

    Формы в БД разные: `[{"function": {"name", "arguments"}}]`, `[{"name", "arguments"}]`,
    иногда одна запись объектом. Всё непонятное пропускаем — лента не должна падать.
    """
    if raw is None:
        return []
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except ValueError:
            return []
    if isinstance(data, dict):
        data = data.get("tool_calls") if "tool_calls" in data else [data]
    if not isinstance(data, list):
        return []
    out: list[tuple[str, dict]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        func = item.get("function") if isinstance(item.get("function"), dict) else item
        name = str(func.get("name") or item.get("name") or item.get("tool") or "").strip()
        if not name:
            continue
        args = _json_args(func.get("arguments") if "arguments" in func else
                          func.get("input") if "input" in func else func.get("args"))
        out.append((name, args))
    return out


def human_tool(name: str, args=None) -> str:
    """«читает файл «договор.md»», «ищет в сети: сроки поставки», «запускает команду»."""
    tool = str(name or "").strip()
    data = args if isinstance(args, dict) else {}
    verb, key = TOOL_VERBS.get(tool, ("", ""))
    if not verb:
        short = tool.split("__")[-1].replace("_", " ").strip()
        verb = "работает: " + short if short else "действует"
    subject = ""
    if key and data.get(key):
        subject = str(data.get(key) or "")
    else:
        for other in _OBJ_KEYS:
            if data.get(other):
                subject = str(data.get(other) or "")
                break
    subject = one_line(subject, 90)
    if not subject:
        return verb
    if "\\" in subject or "/" in subject:
        subject = short_path(subject)
    if tool in ("terminal", "shell", "bash"):
        return verb                      # команду не показываем: в ней бывают ключи
    if tool in ("kanban_create", "kanban_comment"):
        return verb + ": «%s»" % subject
    return "%s «%s»" % (verb, subject)


def _looks_like_error(text: str) -> bool:
    low = str(text or "")[:400].lower()
    if not low:
        return False
    return (low.lstrip().startswith(("error", "traceback", "ошибка", "не удалось", "failed",
                                     "exception"))
            or '"ok": false' in low or '"error"' in low or "\nerror:" in low)


def result_text(payload) -> tuple[str, bool]:
    """Результат инструмента одной строкой: `("готово · 12 строк", True)` / ошибка."""
    raw = "" if payload is None else str(payload)
    ok = not _looks_like_error(raw)
    clean = one_line(raw, MAX_RESULT)
    if ok:
        lines = len([ln for ln in raw.splitlines() if ln.strip()])
        return ("готово" + (" · %d строк" % lines if lines > 1 else "")), True
    if not clean:
        return "ошибка", False
    # «Ошибка: Ошибка: …» — не повторяем слово дважды
    low = clean.lower()
    for head in ("ошибка:", "ошибка ", "error:", "error ", "exception:"):
        if low.startswith(head):
            clean = clean[len(head):].lstrip(" :-")
            break
    return ("ошибка: " + clean.rstrip(" .")) if clean else "ошибка", False


# ---------------------------------------------------------------- курсор

#: Самая длинная разумная строка курсора: реальный курсор — сотни знаков, всё
#: длиннее — мусор (W21-REV2: не декодируем мегабайты base64 из query-строки).
MAX_CURSOR_CHARS = 4096


def encode_cursor(state: dict) -> str:
    """Курсор в строку: ключ последней записи + отметки строк по сеансам → `w1.…`."""
    if not isinstance(state, dict):
        return ""
    payload = {"v": 1, "t": _int(state.get("t")), "s": str(state.get("s") or ""),
               "r": _int(state.get("r")), "i": str(state.get("i") or ""), "m": {}}
    for key, value in (state.get("m") or {}).items():
        sid = str(key or "")
        if sid:
            payload["m"][sid] = _int(value)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "w1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(text) -> dict:
    """Курсор из строки; мусор и чужой формат — пустой курсор (покажем последнее)."""
    raw = str(text or "").strip()
    if not raw.startswith("w1.") or len(raw) > MAX_CURSOR_CHARS:
        return {"t": 0, "s": "", "r": 0, "i": "", "m": {}}
    body = raw[3:]
    body += "=" * (-len(body) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
    except Exception:                       # noqa: BLE001 - битый курсор = «с начала»
        return {"t": 0, "s": "", "r": 0, "i": "", "m": {}}
    if not isinstance(data, dict):
        return {"t": 0, "s": "", "r": 0, "i": "", "m": {}}
    marks = {}
    for key, value in (data.get("m") or {}).items():
        sid = str(key or "")
        if sid:
            marks[sid] = _int(value)
    return {"t": _int(data.get("t")), "s": str(data.get("s") or ""),
            "r": _int(data.get("r")), "i": str(data.get("i") or ""), "m": marks}


def page_cursor(text) -> dict | None:
    """Курсор «показать раньше»; мусор/пустое → ``None`` (показываем свежее).

    W21-REV2: раньше любая непустая строка в `?before=` считалась курсором. Мусор
    (`before=abc`, обрезанный адрес, мегабайт base64) декодировался в «нулевой»
    курсор, и клиент получал ПУСТУЮ страницу с `more:false` — «Показать раньше»
    молча выключалось. Теперь мусор означает «страницы нет», как будто параметра
    не передали.
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > MAX_CURSOR_CHARS:
        return None
    page = decode_cursor(raw)
    if not (_int(page.get("t")) or page.get("i") or page.get("m")):
        return None
    return page


# ---------------------------------------------------------------- настройки комнаты

def room_settings(room) -> dict:
    """Действующие настройки «Хода работы» комнаты (старое поле = умолчания)."""
    room = room if isinstance(room, dict) else {}
    mode = rooms_mod._norm_worklog_mode(room.get("worklog_mode"))
    minutes = rooms_mod._norm_digest_min(room.get("digest_every_min"))
    block = rooms_mod.worklog_block(mode, minutes)
    block["level"] = "steps" if mode == "brief" else "thoughts"
    return block


def _level(asked: str, mode: str) -> str:
    """Фактический уровень ленты: `brief` не показывает рассуждения в любом случае."""
    name = str(asked or "").strip().lower()
    if name and name not in LEVELS:
        raise WorklogError("Уровень хода работы: «thoughts» или «steps»", "bad_request",
                           {"field": "level", "value": asked, "allowed": list(LEVELS)})
    if mode in ("brief", "off"):
        return "steps"
    return name or DEFAULT_LEVEL


def _limit(value) -> int:
    if value in (None, ""):
        return DEFAULT_LIMIT
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise WorklogError("limit: целое число 1…%d" % MAX_LIMIT, "bad_request",
                           {"field": "limit", "value": value})
    if number < 1 or number > MAX_LIMIT:
        raise WorklogError("limit: от 1 до %d" % MAX_LIMIT, "bad_request",
                           {"field": "limit", "value": value, "max": MAX_LIMIT})
    return number


# ---------------------------------------------------------------- подписи участников

def _role_of(room: dict, profile: str) -> str:
    try:
        for item, role in am.participants(room):
            if item == profile:
                return str(role or "")
    except Exception:                        # noqa: BLE001 - подпись вторична
        pass
    return ""


def _label_of(room: dict, profile: str) -> str:
    """Подпись участника из 1.0.3 («Анна — юрист»), иначе роль по-русски."""
    room = room if isinstance(room, dict) else {}
    name = str(profile or "")
    custom = ""
    for member in room.get("members") or []:
        if isinstance(member, dict) and str(member.get("profile") or "") == name:
            custom = str(member.get("label") or "")
            break
    if not custom:
        custom = str((room.get("role_labels") or {}).get(name) or "")
    if custom:
        return one_line(custom, 60)
    role = _role_of(room, name)
    return ROLE_RU.get(role, name or "участник")


# ---------------------------------------------------------------- доступ к БД

def _connect(path) -> sqlite3.Connection | None:
    """Чтение чужой БД: `mode=ro`, без создания файла, с коротким таймаутом."""
    db = Path(str(path or ""))
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=3)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _card_rows(cfg, board: str, ids: list) -> dict:
    """Карточки дерева: кто, что, в каком статусе (только чтение `kanban.db`)."""
    if not ids:
        return {}
    con = _connect(cfg.kanban_db(board))
    if con is None:
        return {}
    out: dict = {}
    try:
        marks = ",".join("?" * len(ids))
        rows = con.execute(
            "SELECT id, assignee, status, title, created_at, started_at FROM tasks "
            "WHERE id IN (%s)" % marks, tuple(ids)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for row in rows:
        out[str(row["id"])] = {"id": str(row["id"]),
                               "assignee": str(row["assignee"] or ""),
                               "status": str(row["status"] or ""),
                               "title": one_line(row["title"], 80),
                               "created_at": _int(row["created_at"]),
                               "started_at": _int(row["started_at"])}
    return out


def _links(cfg, board: str, ids: list) -> list[tuple[str, str]]:
    """Связи «родитель → ребёнок» дерева: по ним видно, кому что передали."""
    if not ids:
        return []
    con = _connect(cfg.kanban_db(board))
    if con is None:
        return []
    try:
        marks = ",".join("?" * len(ids))
        rows = con.execute(
            "SELECT parent_id, child_id FROM task_links WHERE parent_id IN (%s) "
            "OR child_id IN (%s)" % (marks, marks), tuple(ids) * 2).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [(str(r["parent_id"]), str(r["child_id"])) for r in rows]


def _session_meta(cfg, profile: str, sid: str) -> dict:
    """Строка `sessions` профиля: когда начался/кончился, сколько сообщений и вызовов."""
    con = _connect(am.state_path(cfg, profile))
    if con is None:
        return {}
    try:
        row = con.execute(
            "SELECT id, started_at, ended_at, message_count, tool_call_count, "
            "last_activity_at, profile_name, source FROM sessions WHERE id = ?",
            (sid,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    if row is None:
        return {}
    return {"started_at": _int(row["started_at"]),
            "ended_at": _int(row["ended_at"]),
            "messages": _int(row["message_count"]),
            "tools": _int(row["tool_call_count"]),
            "last_activity_at": _int(row["last_activity_at"]),
            "profile_name": str(row["profile_name"] or ""),
            "source": str(row["source"] or "")}


def _recent_sessions(cfg, profile: str, *, after: int, limit: int = 8) -> list[dict]:
    """Последние сеансы профиля — запасной путь поиска сеанса карточки."""
    con = _connect(am.state_path(cfg, profile))
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT id, started_at, last_activity_at FROM sessions "
            "WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
            (_int(after), max(1, int(limit)))).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [{"id": str(r["id"]), "started_at": _int(r["started_at"]),
             "last_activity_at": _int(r["last_activity_at"])} for r in rows]


def _first_user_text(cfg, profile: str, sid: str) -> str:
    """Первая реплика пользователя в сеансе — в ней движок пишет id карточки."""
    con = _connect(am.state_path(cfg, profile))
    if con is None:
        return ""
    try:
        row = con.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                          "ORDER BY id LIMIT 1", (sid,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    return str(row["content"] or "") if row is not None else ""


def _sessions(cfg, board: str, cards: dict) -> dict:
    """Сеансы участников: `{session_id: {profile, card_id, source, run_id, …}}`.

    Основной путь — то же, что у «ушло на» (`agent_models._run_session`): метаданные
    прогона карточки (`worker_session_id`) и событие допуска `owner_admitted`.
    Запасной — первый запрос пользователя сеанса, в котором есть id карточки: так
    находятся сеансы, у которых прогон уже убран из журнала.
    """
    out: dict = {}
    if not cards:
        return out
    con = _connect(cfg.kanban_db(board))
    try:
        for cid, card in sorted(cards.items()):
            run, sid = ({}, "")
            if con is not None:
                try:
                    run, sid = am._run_session(con, cid)
                except Exception:            # noqa: BLE001 - старая схема журнала
                    run, sid = {}, ""
            profile = str((run or {}).get("profile") or card.get("assignee") or "")
            if sid and profile:
                out[sid] = {"session_id": sid, "profile": profile, "card_id": cid,
                            "source": "run", "run_id": _int((run or {}).get("id")) or None,
                            "started_at": _int((run or {}).get("started_at"))}
    finally:
        if con is not None:
            con.close()
    have = {item["card_id"] for item in out.values()}
    for cid, card in sorted(cards.items()):
        if cid in have:
            continue
        profile = str(card.get("assignee") or "")
        if not profile:
            continue
        base = _int(card.get("started_at")) or _int(card.get("created_at"))
        for sess in _recent_sessions(cfg, profile, after=max(0, base - 900)):
            sid = sess["id"]
            if sid in out:
                continue
            if cid and cid in _first_user_text(cfg, profile, sid):
                out[sid] = {"session_id": sid, "profile": profile, "card_id": cid,
                            "source": "message", "run_id": None,
                            "started_at": sess.get("started_at")}
                have.add(cid)
                break
    for sid, item in out.items():
        item.update({k: v for k, v in _session_meta(cfg, item["profile"], sid).items()
                     if k not in item or not item.get(k)})
    return out


def _context(cfg, room: dict, root_id: str, extra=None) -> dict:
    """Всё, что нужно ленте: карточки дерева, связи, сеансы, подписи."""
    room = room if isinstance(room, dict) else {}
    board = str(room.get("board") or getattr(cfg, "board", "") or "default")
    try:
        from .engine_write_guard import tree_ids
    except ImportError:                      # pragma: no cover
        from engine_write_guard import tree_ids  # type: ignore
    ids = {str(root_id)} if root_id else set()
    try:
        ids |= {str(i) for i in tree_ids(cfg, board, str(root_id)) if i}
    except Exception:                        # noqa: BLE001 - доска недоступна
        pass
    for item in extra or []:
        if str(item or "").strip():
            ids.add(str(item).strip())
    cards = _card_rows(cfg, board, sorted(ids))
    return {"room": room, "board": board, "root": str(root_id),
            "ids": sorted(ids), "cards": cards,
            "links": _links(cfg, board, sorted(ids)),
            "sessions": _sessions(cfg, board, cards)}


def _labels(context: dict) -> dict:
    room = context.get("room") or {}
    out = {}
    for cid, card in (context.get("cards") or {}).items():
        profile = card.get("assignee") or ""
        out[profile] = {"label": _label_of(room, profile), "role": _role_of(room, profile)}
    return out


# ---------------------------------------------------------------- лента из сеансов

_ITEM_FIELDS = ("id", "ts", "card_id", "profile", "label", "role", "kind", "text",
                "tool", "ok")


def _item(*, key: str, ts_ms: int, sid: str, row_id: int, card_id: str, profile: str,
          who: dict, kind: str, text: str, tool: str = "", ok=None, sub: int = 0) -> dict:
    item = {"id": key, "ts": int(ts_ms // 1000), "card_id": card_id or None,
            "profile": profile, "label": who.get("label") or profile,
            "role": who.get("role") or "", "kind": kind, "text": text,
            "tool": tool or "", "ok": ok}
    # составной ключ: строка сеанса × номер события в строке — иначе из одной строки
    # (рассуждение + два вызова + ответ) на границе страницы терялись бы записи
    item["_t"], item["_s"], item["_r"] = int(ts_ms), sid or "", int(row_id) * 10 + int(sub)
    return item


def _message_rows(con, sid: str, after_row: int, want: int) -> list:
    """Строки сеанса после курсора: без сжатых, наблюдённых и выключенных."""
    sql = ("SELECT id, role, content, tool_calls, tool_name, timestamp, reasoning, "
           "reasoning_content FROM messages WHERE session_id = ? AND id > ? "
           "AND COALESCE(active, 1) = 1 AND COALESCE(compacted, 0) = 0 "
           "AND COALESCE(observed, 0) = 0 ORDER BY id LIMIT ?")
    try:
        return con.execute(sql, (sid, int(after_row), max(1, int(want)))).fetchall()
    except sqlite3.Error:
        return []


#: «бесконечная» граница строк, когда отметки сеанса в курсоре нет
_ROW_ANY = 2 ** 62


def _message_rows_before(con, sid: str, before_row: int | None, want: int) -> list:
    """Строки сеанса СТАРШЕ отметки — для «Показать раньше» и первой страницы.

    W21-L1: раньше на любой запрос читались ПЕРВЫЕ строки сеанса (``id > 0``), и у
    длинного сеанса «первый ответ» показывал начало работы, а «Показать раньше»
    не могло догрузить историю — окно уже было занято началом. Теперь читаем от
    отметки НАЗАД (по убыванию id), то есть ближайшие к курсору строки; наружу
    отдаём как обычно — по возрастанию.
    ``before_row=None`` — самые последние строки сеанса.
    """
    sql = ("SELECT id, role, content, tool_calls, tool_name, timestamp, reasoning, "
           "reasoning_content FROM messages WHERE session_id = ? AND id <= ? "
           "AND COALESCE(active, 1) = 1 AND COALESCE(compacted, 0) = 0 "
           "AND COALESCE(observed, 0) = 0 ORDER BY id DESC LIMIT ?")
    upper = int(before_row) if before_row else _ROW_ANY
    try:
        rows = con.execute(sql, (sid, int(upper), max(1, int(want)))).fetchall()
    except sqlite3.Error:
        return []
    rows.reverse()
    return rows


def _items_of_rows(rows, *, sid: str, card_id: str, profile: str, who: dict,
                   level: str) -> list[dict]:
    """Перевод строк сеанса в человеческие события: рассуждение, действие, результат."""
    out: list[dict] = []
    for row in rows:
        role = str(row["role"] or "")
        rid = _int(row["id"])
        stamp = _ms(row["timestamp"])
        key = "m_%s_%d" % (hashlib.sha1(sid.encode("utf-8")).hexdigest()[:6], rid)
        thought = ""
        if role in ("assistant", ""):
            thought = str(row["reasoning"] or row["reasoning_content"] or "")
        if thought.strip() and level == "thoughts":
            text = one_line(thought, MAX_THOUGHT)
            if text:
                out.append(_item(key=key + "_r", ts_ms=stamp, sid=sid, row_id=rid,
                                 card_id=card_id, profile=profile, who=who,
                                 kind="thought", text="думает: " + text, sub=0))
        if role == "tool":
            text, ok = result_text(row["content"])
            out.append(_item(key=key + "_t", ts_ms=stamp, sid=sid, row_id=rid,
                             card_id=card_id, profile=profile, who=who, kind="result",
                             text=text, tool=str(row["tool_name"] or ""), ok=ok))
            continue
        if role != "assistant":
            continue                       # задание/систему не показываем: это не «ход работы»
        calls = tool_calls(row["tool_calls"])
        for index, (name, args) in enumerate(calls):
            out.append(_item(key="%s_c%d" % (key, index), ts_ms=stamp, sid=sid, row_id=rid,
                             card_id=card_id, profile=profile, who=who, kind="action",
                             text=human_tool(name, args), tool=name, ok=None,
                             sub=2 + index))
        content = str(row["content"] or "").strip()
        if content and level == "thoughts":
            text = one_line(content, MAX_MESSAGE)
            if text:
                out.append(_item(key=key + "_m", ts_ms=stamp, sid=sid, row_id=rid,
                                 card_id=card_id, profile=profile, who=who,
                                 kind="message", text=text, sub=1))
    return out


# ---------------------------------------------------------------- события карточек

def _card_items(context: dict, labels: dict) -> list[dict]:
    """«Принял карточку», «передал», «готово» — из карточек и связей дерева (не из логов)."""
    out: list[dict] = []
    cards = context.get("cards") or {}
    root = context.get("root") or ""
    for cid, card in sorted(cards.items()):
        profile = card.get("assignee") or ""
        who = labels.get(profile) or {"label": profile, "role": _role_of(context.get("room") or {}, profile)}
        status = str(card.get("status") or "")
        stamp = _ms(_int(card.get("started_at")) or _int(card.get("created_at")))
        title = card.get("title") or ""
        kind = "status"
        if status in RUNNING:
            text = "взял в работу «%s»" % title if title else "взял в работу"
        elif status == "blocked":
            text = "заблокировано: ждёт вашего ответа"
        elif status == "done":
            text = "готово: «%s»" % title if title else "готово"
        elif status == "failed":
            text = "не удалось закончить: «%s»" % title if title else "не удалось закончить"
        elif status in ("queued", "todo", "ready"):
            text = "в очереди: «%s»" % title if title else "в очереди"
        elif status == "triage":
            text = "разбирает «%s»" % title if title else "разбирает"
        else:
            continue
        out.append(_item(key="k_%s_%s" % (cid, status), ts_ms=stamp,
                         sid="", row_id=0, card_id=cid, profile=profile, who=who,
                         kind=kind, text=text))
    # передача работы: «Руководитель → Юрист: «Проверить договор»»
    for parent, child in context.get("links") or []:
        if child == root or child not in cards or parent not in cards:
            continue
        author = cards[parent].get("assignee") or ""
        target = cards[child].get("assignee") or ""
        if not target or author == target:
            continue
        who = labels.get(target) or {"label": target, "role": _role_of(context.get("room") or {}, target)}
        from_who = labels.get(author) or {"label": author, "role": ""}
        title = cards[child].get("title") or ""
        text = "%s → %s: «%s»" % (from_who["label"], who["label"], title) if title \
            else "%s → %s" % (from_who["label"], who["label"])
        out.append(_item(key="h_%s" % child, ts_ms=_ms(_int(cards[child].get("created_at"))),
                         sid="", row_id=0, card_id=child, profile=target, who=who,
                         kind="handoff", text=text))
    return out


# ---------------------------------------------------------------- дайджест (хранилище)

def _digest_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / "worklog_digest.json"


def _digest_store(cfg, room_id) -> dict:
    try:
        value = sf.read_json(_digest_path(cfg, room_id), {})
    except Exception:                        # noqa: BLE001 - файл вторичен
        return {}
    return value if isinstance(value, dict) else {}


def _digest_save(cfg, room_id, root_id: str, entry: dict | None) -> None:
    """Записать/убрать итог запроса. Ошибка записи не ломает тик сторожа."""
    try:
        path = _digest_path(cfg, room_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sf.lock_for(path):
            data = _digest_store(cfg, room_id)
            if entry is None:
                data.pop(str(root_id), None)
            else:
                data[str(root_id)] = entry
            if len(data) > DIGEST_KEEP:
                keep = sorted(data.items(), key=lambda kv: _int((kv[1] or {}).get("at")),
                              reverse=True)[:DIGEST_KEEP]
                data = dict(keep)
            sf.write_json_atomic(path, data)
    except Exception:                        # noqa: BLE001 - журнал итогов вторичен
        pass


def digest_for(cfg, room_id, root_id) -> dict | None:
    """Последний промежуточный итог запроса (для API и сторожа)."""
    entry = _digest_store(cfg, room_id).get(str(root_id))
    return dict(entry) if isinstance(entry, dict) else None


# ---------------------------------------------------------------- живое состояние

def _last_activity(cfg, profile: str, sid: str) -> tuple[int, str, str]:
    """Последнее действие сеанса: `(время, «ищет в договорах», инструмент)`."""
    con = _connect(am.state_path(cfg, profile))
    if con is None:
        return 0, "", ""
    out: tuple[int, str, str] = (0, "", "")
    try:
        rows = con.execute(
            "SELECT role, content, tool_calls, tool_name, timestamp FROM messages "
            "WHERE session_id = ? AND COALESCE(active, 1) = 1 AND COALESCE(compacted, 0) = 0 "
            "ORDER BY id DESC LIMIT 8", (sid,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for row in rows:
        stamp = _int(row["timestamp"])
        role = str(row["role"] or "")
        if role == "assistant":
            calls = tool_calls(row["tool_calls"])
            if calls:
                name, args = calls[-1]
                return stamp, human_tool(name, args), name
            content = one_line(row["content"], 90)
            if content:
                return stamp, "пишет ответ", ""
            continue
        if role == "tool":
            text, ok = result_text(row["content"])
            return stamp, ("получил результат" if ok else text), str(row["tool_name"] or "")
    return out


def activity(cfg, profile: str, sid: str) -> dict:
    """Признаки жизни сеанса карточки: когда была активность и не ждёт ли инструмент.

    Нужно сторожу зависаний (`server/watchdog.py`, W25-M2). Отличие от
    `_last_activity`: возвращает не подпись, а факты — время последнего сообщения
    любого вида, висит ли незавершённый вызов инструмента (последняя строка —
    ассистент с `tool_calls`, результата после неё нет) и имя инструмента.
    Карточка с живым выводом инструментов зависшей НЕ считается (E5).

    Если незавершённый вызов есть, но строки ассистента нет (результат не записан),
    отдаём время вызова: таймер сторожа считается от него, а не от старта карточки.
    """
    out = {"last_at": 0, "pending_tool": False, "tool": "", "role": ""}
    con = _connect(am.state_path(cfg, profile))
    if con is None:
        return out
    try:
        rows = con.execute(
            "SELECT role, tool_name, tool_calls, timestamp FROM messages "
            "WHERE session_id = ? AND COALESCE(active, 1) = 1 AND COALESCE(compacted, 0) = 0 "
            "ORDER BY id DESC LIMIT 6", (sid,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for index, row in enumerate(rows):
        stamp = _int(row["timestamp"])
        role = str(row["role"] or "")
        if not out["last_at"] or stamp > out["last_at"]:
            out["last_at"] = stamp
        if index == 0:
            out["role"] = role
            if role == "assistant":
                calls = tool_calls(row["tool_calls"])
                if calls:
                    out["pending_tool"] = True
                    out["tool"] = calls[-1][0]
            elif role == "tool":
                out["tool"] = str(row["tool_name"] or "")
        if out["last_at"] and index >= 2:
            break
    return out


def live(cfg, room: dict, root_id: str, extra=None, *, context=None,
         now: int | None = None) -> list[dict]:
    """Кто работает прямо сейчас: подпись, состояние, последнее действие, зависание."""
    context = context or _context(cfg, room, root_id, extra)
    labels = _labels(context)
    cards = context.get("cards") or {}
    now = _now() if now is None else int(now)
    out: list[dict] = []
    for card_id, card in sorted(cards.items(), key=lambda kv: kv[0]):
        profile = card.get("assignee") or ""
        status = str(card.get("status") or "")
        sessions = [item for item in context["sessions"].values()
                    if item.get("card_id") == card_id]
        last_at, action, tool = 0, "", ""
        tools = 0
        for item in sessions:
            stamp, text, name = _last_activity(cfg, item["profile"], item["session_id"])
            if stamp >= last_at:
                last_at, action, tool = stamp, text or action, name or tool
            tools += _int(item.get("tools"))
            item["last_at"] = stamp
        since = last_at or _int(card.get("started_at")) or _int(card.get("created_at"))
        state = "idle"
        if status in RUNNING:
            state = "working"
            if not action:
                action = STATUS_RU.get(status, status)
            if since and now - since > STUCK_MIN * 60:
                state = "stuck"
        elif status == "blocked":
            state = "waiting"
            action = action or "ждёт вашего ответа"
        elif status in ("queued", "todo", "ready", "triage"):
            state = "idle"
            action = action or STATUS_RU.get(status, status)
        elif status in ("done", "archived"):
            state = "idle"
            action = action or STATUS_RU.get(status, "готово")
        who = labels.get(profile) or {"label": profile or "участник", "role": ""}
        minutes = int((now - since) / 60) if since and now > since else 0
        out.append({"profile": profile, "label": who["label"], "role": who["role"],
                    "card_id": card_id, "state": state, "since": since or None,
                    "last_action": one_line(action, 120), "tool": tool,
                    "tools": tools, "messages": sum(_int(i.get("messages")) for i in sessions),
                    "stuck": state == "stuck",
                    "stuck_text": ("возможно завис: нет активности %d мин. Проверьте доску"
                                   % minutes) if state == "stuck" else ""})
    out.sort(key=lambda item: (item["state"] != "stuck", item["state"] == "idle",
                               item["label"]))
    return out


def stuck_cards(cfg, room: dict, root_id: str, extra=None, *, context=None,
                now: int | None = None) -> list[dict]:
    """Карточки в работе без активности дольше `STUCK_MIN` минут (только пометка)."""
    rows = live(cfg, room, root_id, extra, context=context, now=now)
    out = []
    for item in rows:
        if not item.get("stuck"):
            continue
        since = _int(item.get("since")) or _now()
        out.append({"card_id": item["card_id"], "profile": item["profile"],
                    "label": item["label"], "minutes": max(0, int((_now() - since) / 60)),
                    "text": item["stuck_text"]})
    return out


# ---------------------------------------------------------------- сборка ленты

def _key(item: dict) -> tuple:
    """Полный ключ порядка записи: время, сеанс, номер строки, id (без повторов)."""
    return (_int(item.get("_t")), item.get("_s") or "", _int(item.get("_r")),
            str(item.get("id") or ""))


def _cursor_key(cursor: dict) -> tuple:
    return (_int(cursor.get("t")), cursor.get("s") or "", _int(cursor.get("r")),
            str(cursor.get("i") or ""))


def _passes(item: dict, cursor: dict) -> bool:
    """Запись строго новее курсора — сравнение по полному ключу, без потерь и дублей."""
    return _key(item) > _cursor_key(cursor)


def _advance(cursor: dict, items: list[dict]) -> dict:
    """Новый курсор: ключ последней отданной записи + отметки строк по сеансам."""
    best = _cursor_key(cursor)
    marks = dict(cursor.get("m") or {})
    for item in items:
        key = _key(item)
        if key > best:
            best = key
        if item.get("_s"):
            sid = item["_s"]
            marks[sid] = max(_int(marks.get(sid)), _int(item.get("_r")))
    return {"t": best[0], "s": best[1], "r": best[2], "i": best[3], "m": marks}


def _rewind(items: list[dict]) -> dict:
    """Курсор «раньше»: ключ САМОЙ СТАРОЙ записи страницы + отметки строк по сеансам.

    Отметка сеанса — самая старая отданная строка этого сеанса: следующий запрос с
    этим курсором читает строки до неё включительно, а совпадения отсекает строгое
    сравнение ключей. Поэтому страницы стыкуются без повторов и без пропусков.
    """
    if not items:
        return {"t": 0, "s": "", "r": 0, "i": "", "m": {}}
    oldest = min(items, key=_key)
    marks: dict = {}
    for item in items:
        sid = item.get("_s")
        if not sid:
            continue
        row = _int(item.get("_r"))
        marks[sid] = min(row, marks.get(sid, row))
    key = _key(oldest)
    return {"t": key[0], "s": key[1], "r": key[2], "i": key[3], "m": marks}


def _digest_items(cfg, room: dict, root_id: str) -> list[dict]:
    entry = digest_for(cfg, room.get("id"), root_id)
    if not entry:
        return []
    stamp = _ms(_int(entry.get("at")))
    who = {"label": "Сторож", "role": ""}
    return [_item(key=str(entry.get("id") or "d_1"), ts_ms=stamp, sid="", row_id=0,
                  card_id=root_id or "", profile="", who=who, kind="digest",
                  text=one_line(entry.get("text"), MAX_MESSAGE + 200))]


def collect(cfg, room: dict, root_id: str, *, since: str = "", before: str = "",
            level: str = "", limit: int = DEFAULT_LIMIT, extra=None,
            now: int | None = None) -> dict:
    """Лента хода работы: записи после курсора, живое состояние, итоги, зависания.

    ``since`` — «что нового» (опрос), ``before`` — «показать раньше» (страница назад):
    записи строго старше указанного курсора, ближайшие к нему. Оба курсора непрозрачны.
    """
    settings = room_settings(room)
    mode = settings["mode"]
    use_level = _level(level, mode)
    want = _limit(limit)
    base = {"request": str(root_id or ""), "room": str((room or {}).get("id") or ""),
            "mode": mode, "level": use_level, "items": [], "cursor": "",
            "before": "", "more": False, "live": [], "digest": None, "stuck": [],
            "generated_at": _now(), "warnings": []}
    if mode == "off":
        # «Выключено» — никакого чтения: ни state.db, ни kanban.db.
        base["cursor"] = encode_cursor({"t": 0, "m": {}})
        return base
    cursor = decode_cursor(since)
    page = page_cursor(before) if before else None
    # Окно чтения: «раньше» и первая страница читаются назад, поэтому запас строк
    # больше запрошенного (на уровне «Кратко» строки без действий не дают событий).
    margin = min(MAX_ROWS_PER_SESSION, want * 2 + 60)
    context = _context(cfg, room, root_id, extra)
    labels = _labels(context)
    if not context["cards"]:
        base["warnings"].append("Карточки запроса не найдены — работа ещё не начиналась.")
    pool: list[dict] = []
    fetched = 0
    cons: dict = {}
    try:
        for sid, session in sorted(context["sessions"].items()):
            profile = session.get("profile") or ""
            if not profile:
                continue
            if profile not in cons:
                cons[profile] = _connect(am.state_path(cfg, profile))
            con = cons[profile]
            if con is None:
                continue
            if page is not None:
                # «Показать раньше»: строки СТАРШЕ отметки этого сеанса, от отметки назад.
                mark = _int((page.get("m") or {}).get(sid))
                rows = _message_rows_before(con, sid, (mark // 10) if mark else None, margin)
            elif since:
                after = _int((cursor.get("m") or {}).get(sid))
                # строку, на которой остановился курсор, перечитываем всегда: в ней могло
                # остаться ещё одно событие (рассуждение + вызов + ответ — одна строка)
                after_row = max(0, (after // 10) - 1)
                rows = _message_rows(con, sid, after_row, min(MAX_ROWS_PER_SESSION, want + 50))
            else:
                # Первый ответ — ПОСЛЕДНИЕ записи: лента открывается на свежем, а не на
                # начале работы (W21-L1; раньше читались первые строки сеанса).
                rows = _message_rows_before(con, sid, None, margin)
            fetched += len(rows)
            card_id = str(session.get("card_id") or "")
            who = labels.get(profile) or {"label": profile, "role": ""}
            pool.extend(_items_of_rows(rows, sid=sid, card_id=card_id, profile=profile,
                                       who=who, level=use_level))
    finally:
        for con in cons.values():
            if con is not None:
                con.close()
    pool.extend(_card_items(context, labels))
    pool.extend(_digest_items(cfg, room, root_id))
    pool = [item for item in pool if _passes(item, cursor)]
    pool.sort(key=_key)
    if page is not None:
        older = [item for item in pool if _key(item) < _cursor_key(page)]
        chosen = older[-want:]                  # ближайшие к курсору «раньше»
        base["more"] = len(older) > len(chosen)
        base["cursor"] = encode_cursor(page)    # опрос «что нового» не сдвигаем
    else:
        if since and len(pool) > want:
            chosen = pool[:want]                # инкремент: отдаём самые старые из новых
        else:
            chosen = pool[-want:]
        base["more"] = len(pool) > len(chosen)
        base["cursor"] = encode_cursor(_advance(cursor, chosen)) if chosen \
            else encode_cursor(cursor)
    # Курсор следующей страницы «раньше»: ключ самой старой отданной записи.
    # Клиент передаёт его в `?before=`; пусто — история кончилась.
    base["before"] = encode_cursor(_rewind(chosen)) if (chosen and base["more"]) else ""
    base["items"] = [{key: item.get(key) for key in _ITEM_FIELDS} for item in chosen]
    rows = live(cfg, room, root_id, extra, context=context, now=now)
    base["live"] = rows
    base["stuck"] = [{"card_id": row["card_id"], "profile": row["profile"],
                      "label": row["label"],
                      "minutes": max(0, int((_now() - (_int(row["since"]) or _now())) / 60)),
                      "text": row["stuck_text"]} for row in rows if row.get("stuck")]
    entry = digest_for(cfg, room.get("id"), root_id)
    if isinstance(entry, dict):
        base["digest"] = {key: entry.get(key) for key in
                          ("id", "at", "text", "minutes", "workers", "done", "total")
                          if entry.get(key) is not None}
    return base


def worklog(cfg, room: dict, root_id: str, *, since: str = "", before: str = "",
            level: str = "", limit: int = DEFAULT_LIMIT, extra=None) -> dict:
    """Публичный вход маршрута: те же поля, что в `docs/API.md` §W20.2."""
    return collect(cfg, room, root_id, since=since, before=before, level=level,
                   limit=limit, extra=extra)


# ---------------------------------------------------------------- сторож: дайджест

def _clock(stamp: int) -> str:
    """Время по-человечески: «17:40» (по поясу сервера)."""
    try:
        return time.strftime("%H:%M", time.localtime(int(stamp)))
    except (OSError, ValueError, OverflowError):
        return ""


def digest_text(rows: list[dict], cards: dict, *, started: int, last_event: int,
                now: int) -> tuple[str, dict]:
    """Строка промежуточного итога и её данные: «Прошло 6 мин. Работают: … Готово 2 из 5.»"""
    working = [row for row in rows if row["state"] in ("working", "stuck")]
    waiting = [row for row in rows if row["state"] == "waiting"]
    done = sum(1 for card in cards.values() if str(card.get("status")) in ("done", "archived"))
    total = len(cards)
    minutes = max(1, int((now - (started or now)) / 60))
    parts: list[str] = ["Прошло %d мин." % minutes]
    workers = []
    if working:
        chunks = []
        for row in working[:4]:
            action = row.get("last_action") or "работает"
            chunks.append("%s — %s" % (row["label"], action))
            workers.append({"profile": row["profile"], "label": row["label"],
                            "last_action": action, "waiting": False})
        parts.append("Работают: " + "; ".join(chunks) + ".")
    if waiting:
        names = [row["label"] for row in waiting[:4]]
        parts.append("%s ждёт вашего ответа." % ", ".join(names))
        for row in waiting[:4]:
            workers.append({"profile": row["profile"], "label": row["label"],
                            "last_action": row.get("last_action") or "ждёт вашего ответа",
                            "waiting": True})
    if not working and not waiting:
        parts.append("Активной работы нет.")
    if total:
        parts.append("Готово %d из %d." % (min(done, total), total))
    if last_event:
        parts.append("Последнее событие — %s." % _clock(last_event))
    text = one_line(" ".join(parts), MAX_MESSAGE + 200)
    return text, {"minutes": minutes, "workers": workers, "done": done, "total": total}


def digest_step(cfg, room: dict, record: dict, detail: dict, *,
                now: int | None = None) -> dict:
    """Шаг сторожа для одного запроса: положить итог, если пора. Модели не запускаются.

    Зовётся из `watcher.py` на каждом тике (5–15 с). Внутри — только чтение и один
    маленький файл на комнату. Возвращает `{digest: bool, text, stuck: [id], skipped}`.
    """
    room = room if isinstance(room, dict) else {}
    settings = room_settings(room)
    minutes = int(settings["digest_every_min"])
    root_id = str((record or {}).get("root_id") or "")
    result = {"digest": False, "text": "", "stuck": [], "skipped": ""}
    if not root_id or not room.get("id"):
        result["skipped"] = "нет карточки"
        return result
    status = str((detail or {}).get("status") or "")
    if status not in WORK_STATUSES:
        # запрос завершён: итог больше не нужен, отметку убираем (не дублируется)
        if digest_for(cfg, room.get("id"), root_id):
            _digest_save(cfg, room.get("id"), root_id, None)
        result["skipped"] = "не в работе"
        return result
    if minutes <= 0:
        result["skipped"] = "выключено"
        return result
    now = _now() if now is None else int(now)
    # Частота проверяется ДО чтения баз: иначе каждый тик сторожа (5–15 с) открывал бы
    # `state.db` всех участников и `kanban.db` ради подписи «рано». Чтение — самая
    # дорогая часть шага, а итог всё равно не чаще `digest_every_min` минут.
    previous = digest_for(cfg, room.get("id"), root_id) or {}
    if previous and now - _int(previous.get("at")) < minutes * 60:
        result["skipped"] = "рано"
        return result
    entries = [c for c in ((detail or {}).get("tree") or []) if isinstance(c, dict)]
    cards = {str(c.get("id")): {"id": str(c.get("id")), "assignee": str(c.get("assignee") or ""),
                                "status": str(c.get("status") or ""), "title": str(c.get("title") or "")}
             for c in entries if c.get("id")}
    try:
        rows = live(cfg, room, root_id, now=now)
    except Exception:                        # noqa: BLE001 - сторож не должен падать
        rows = []
    result["stuck"] = [row["card_id"] for row in rows if row.get("stuck")]
    starts = [_int(card.get("started_at")) for card in cards.values()
              if _int(card.get("started_at"))]
    started = min(starts + [_int((detail or {}).get("created_at")) or now])
    stamps = [_int(row.get("since")) for row in rows if _int(row.get("since"))]
    last_event = max(stamps) if stamps else started
    text, data = digest_text(rows, cards, started=started, last_event=last_event, now=now)
    entry = {"id": "d_%s_%d" % (hashlib.sha1(root_id.encode("utf-8")).hexdigest()[:6], now),
             "at": int(now), "text": text, "minutes": data["minutes"],
             "workers": data["workers"], "done": data["done"], "total": data["total"],
             "status": status, "root_id": root_id}
    _digest_save(cfg, room.get("id"), root_id, entry)
    result.update({"digest": True, "text": text, "entry": entry})
    return result


__all__ = ["LEVELS", "MAX_LIMIT", "STUCK_MIN", "WorklogError", "room_settings",
           "worklog", "collect", "live", "stuck_cards", "activity", "digest_step", "digest_for",
           "digest_text", "mask", "human_tool", "tool_calls", "result_text",
           "encode_cursor", "decode_cursor", "page_cursor", "MAX_CURSOR_CHARS",
           "short_path", "trim"]
