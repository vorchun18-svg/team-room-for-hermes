"""«Разрешения агентам» — запросы разрешения на «опасные» команды (WAVE22 §2).

Зачем
-----
Карточки комнаты — «конечные» запуски (``hermes chat -q``, ``payload['finite']=True``):
человека за клавиатурой нет, и Hermes при ``approvals.single_query_mode: deny`` сразу
блокирует «опасную» команду (``BLOCKED: Command flagged as dangerous (script execution
via heredoc) but single-query mode (-q) runs without a user present to approve it``).
В обычном чате пользователь видит окно разрешения и нажимает «разрешить»; в комнате
такого окна не было — участник упирался в стену. Здесь то же самое, но кнопкой в
приложении: **разрешить один раз / разрешать всегда / запретить**.

Что делает модуль
-----------------
* **находит** блокировки чтением: сообщения ``role='tool'`` с ``status: blocked`` /
  текстом ``BLOCKED:`` в сессиях карточек комнаты (``state.db`` профилей, ``mode=ro``;
  связь «карточка ↔ сессия» — та же, что у «Хода работы»: метаданные прогона, событие
  ``owner_admitted``, первый запрос пользователя с id карточки). Команда берётся из
  предыдущего сообщения ассистента (``tool_calls``);
* **отдаёт** список запросов разрешения в деталях запроса (``approvals[]``): команда
  целиком, причина по-русски, кому и в какой карточке, состояние, счётчик повторов;
* **выдаёт** разрешение только штатным механизмом Hermes — список ``command_allowlist``
  в ``config.yaml`` профиля (подпроцесс ``server/approval_grant.py``). «Один раз» —
  та же запись плюс срок в реестре комнаты (до конца карточки или 30 минут);
* **показывает и отзывает** постоянные разрешения (``GET /api/approvals/grants``,
  ``POST /api/approvals/revoke``);
* **снимает** истёкшие временные разрешения (шаг сторожа ``sweep``).

Границы
-------
Жёсткие запреты Hermes (``approval_floors``: удаление корня и подобное) не снимаются
никаким разрешением — такие блокировки отдаются с ``floor: true``. Блокировку
``execute_code`` в режиме без человека Hermes не сверяет со списком разрешений вовсе —
она отдаётся с ``grantable: false``.

W26-D1 («без тупиков», правило Михаила): такая блокировка **не** попадает в состояние
``pending`` — у неё нет кнопки «разрешить», значит она не «ждёт вашего решения».
Вместо тупика у каждой строки есть действие: подсказать участнику обойтись файлом и
терминалом (``hint_agent``), доверить профилю запуск кода на срок (``trust_profile``)
или закрыть как неактуальное (``dismiss``). Блокировки завершённых/архивных карточек
закрываются сами (``closed``) — ждать их некому. Реальные профили читаются только на
чтение; запись — только по действию пользователя и только в присланный домашний каталог
профиля (в песочнице — в поддельный, ``<data>/hermes-home``).

Публично: ``collect``, ``decide``, ``grants``, ``revoke``, ``sweep``, ``describe``,
``find_python``, ``profile_home``, ``ApprovalError``.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

try:                                        # пакетный импорт
    from . import agent_models as am
    from . import proc
    from . import statefiles as sf
    from . import worklog as wl
except ImportError:                          # python server/app.py (плоский импорт)
    import agent_models as am               # type: ignore
    import proc                             # type: ignore
    import statefiles as sf                 # type: ignore
    import worklog as wl                    # type: ignore

# ---------------------------------------------------------------- константы

#: Сколько знаков команды отдаём (длиннее — прокрутка в интерфейсе, не в API).
MAX_COMMAND = 4000
#: «Один раз» живёт до конца карточки, но не дольше этого срока.
ONCE_TTL = 30 * 60
#: Сколько запросов разрешения показываем у одного запроса.
MAX_PER_REQUEST = 20
#: Сколько строк одной сессии читаем (страховка от сессии на 20 000 сообщений).
MAX_ROWS = 400
#: Сколько записей держим в кеше чтения `state.db` (страница запроса не перечитывает всё).
SCAN_CACHE_MAX = 4000
#: Статусы, в которых карточка уже не работает.
FINISHED = ("done", "archived", "failed")
#: То же плюс отменённые: у таких карточек ждать блокировку некому (W26-D1, T2).
CLOSED_CARDS = FINISHED + ("cancelled",)
#: Статусы, в которых перезапуск штатным действием движка имеет смысл.
RESTARTABLE = ("todo", "queued", "blocked", "scheduled", "ready", "review")
#: Состояния записи. ``pending`` — ТОЛЬКО «можно разрешить» (W26-D1, T1):
#: у недоговорных и жёстких запретов состояние своё, и в счётчики они не входят.
#: ``workaround`` — нельзя штатно, но есть способ (execute_code); ``hinted`` —
#: участнику подсказали другой способ; ``info`` — жёсткий запрет (информация);
#: ``closed`` — карточка завершилась, ждать некому; ``dismissed`` — закрыто как
#: неактуальное; ``trusted`` — профилю доверен запуск кода на срок.
STATES = ("pending", "granted_once", "granted_always", "denied", "expired",
          "workaround", "hinted", "info", "closed", "dismissed", "trusted")
#: Состояния «ещё не решено человеком»: их и закрывает автозакрытие (T2).
UNDECIDED = ("pending", "workaround", "info")
DECISIONS = {"once": "granted_once", "always": "granted_always", "deny": "denied"}
#: Действия помимо разрешения (W26-D1, T3): подсказать, доверить профилю, закрыть.
EXTRA_DECISIONS = {"hint_agent": "hinted", "dismiss": "dismissed", "trust_profile": "trusted"}
KINDS = ("terminal", "execute_code")
#: Три вида блокировки и что человек с ними может (правило «без тупиков»).
MODES = ("grant", "workaround", "floor")
MODE_LABEL = {"grant": "можно разрешить",
              "workaround": "нельзя разрешить штатно — есть другой способ",
              "floor": "жёсткий запрет Hermes — только информация"}
#: Подписи действий для интерфейса (одно место на сервер и клиент).
ACTION_LABEL = {"once": "Разрешить один раз", "always": "Разрешать всегда",
                "deny": "Отклонить",
                "hint_agent": "Подсказать участнику обойтись терминалом",
                "trust_profile": "Доверять запуску кода участнику…",
                "dismiss": "Закрыть как неактуальное",
                "hide": "Понятно, скрыть"}
#: Срок доверия профилю — обязателен (W26-D1): без срока «доверять» не пишем.
TTL_OPTIONS = ({"id": "30", "label": "30 минут", "ttl_min": 30},
               {"id": "card", "label": "до конца карточки", "ttl_min": None},
               {"id": "forever", "label": "навсегда", "ttl_min": None})
#: Самый долгий мыслимый срок доверия (минуты): сутки.
TRUST_MAX_MIN = 24 * 60
#: Порядок строк в интерфейсе: сначала то, что правда ждёт человека.
_STATE_RANK = {"pending": 0, "workaround": 1, "info": 2, "trusted": 3}
#: Сколько ждём подпроцесс записи разрешения (импорт Hermes не быстрый).
WORKER_TIMEOUT = 90
WORKER = "approval_grant.py"
#: Пометка «разрешено приложением» в журнале комнаты.
ACTOR = "mikhail"

#: Известные правила Hermes по-русски. Ключ — описания из ``tools/approval``.
PATTERN_RU = {
    "script execution via heredoc": "запуск скрипта через heredoc",
    "script execution via -e/-c flag": "выполнение кода python -c",
    "shell command via -c/-lc flag": "выполнение команды оболочки через -c",
    "execute_code": "execute_code — произвольный код",
    "recursive delete": "рекурсивное удаление файлов",
    "recursive delete (long flag)": "рекурсивное удаление файлов",
    "recursive delete (flags after operands)": "рекурсивное удаление файлов",
    "delete in root path": "удаление в корне диска",
    "Windows cmd destructive delete": "удаление файлов командой Windows",
    "Windows PowerShell destructive delete": "удаление файлов в PowerShell",
    "Windows destructive delete (recursive/quiet switch)": "удаление файлов командой Windows",
    "pipe remote content to shell": "скачивание и запуск чужого скрипта",
    "pipe remote content to PowerShell (iwr | iex)": "скачивание и запуск чужого скрипта",
    "execute remote content via Invoke-Expression": "запуск скачанного кода",
    "xargs with rm": "удаление файлов через xargs",
    "find -exec/-execdir rm": "удаление файлов через find",
    "find -delete": "удаление файлов через find",
    "SQL DROP": "удаление таблиц в базе (SQL DROP)",
    "SQL DELETE without WHERE": "удаление всех строк в базе (SQL DELETE)",
    "SQL TRUNCATE": "очистка таблицы в базе (SQL TRUNCATE)",
    "force kill processes": "принудительное завершение процессов",
    "force kill processes (taskkill /F)": "принудительное завершение процессов",
    "kill processes by regex (killall -r)": "завершение процессов по маске",
    "kill all processes": "завершение всех процессов",
    "fork bomb": "лавинообразный запуск процессов",
    "format filesystem": "форматирование диска",
    "format drive (format.com)": "форматирование диска",
    "disk partitioning (diskpart)": "разметка диска",
    "wipe disk (Clear-Disk)": "стирание диска",
    "disk copy": "копирование диска целиком",
    "write to block device": "запись в системный диск напрямую",
    "registry delete (reg delete)": "удаление ветки реестра Windows",
    "reset ACLs recursively (icacls /reset)": "сброс прав доступа к файлам",
    "grant Everyone access (icacls)": "открытие доступа всем пользователям",
    "world/other-writable permissions": "открытие доступа к файлам всем",
    "access to Hermes secrets (Windows path)": "чтение ключей Hermes",
    "access to SSH keys (Windows path)": "чтение ключей доступа SSH",
    "overwrite system config": "замена системного файла настроек",
    "overwrite project env/config via tee": "перезапись файла настроек проекта",
    "overwrite project env/config via redirection": "перезапись файла настроек проекта",
    "overwrite project env/config file": "перезапись файла настроек проекта",
    "stop/restart hermes gateway (kills running agents)": "перезапуск самого Hermes",
    "stop/restart system service": "перезапуск системной службы",
    "force stop service (Stop-Service -Force)": "остановка системной службы",
    "upload a secret or credential file via curl/wget (possible exfiltration)": "отправка файла с ключами наружу",
    "pipe a credential file into a curl/wget upload (possible exfiltration)": "отправка файла с ключами наружу",
    "invisible or bidirectional Unicode control character (possible obfuscation)": "скрытые символы в команде",
    "cloud metadata endpoint access (instance credentials)": "доступ к служебным ключам облака",
    "recursive chown to root": "смена владельца файлов на root",
    "recursive chown to root (long flag)": "смена владельца файлов на root",
    "delete volume shadow copies (vssadmin)": "удаление резервных копий Windows",
    "delete backups (wbadmin)": "удаление резервных копий Windows",
    "modify boot configuration (bcdedit /set)": "изменение загрузки Windows",
    "overwrite system file via tee": "замена системного файла",
    "overwrite system file via redirection": "замена системного файла",
    "copy/move file into system config path": "копирование в системный каталог",
    "docker with remote daemon redirect (-H/--host)": "управление чужим Docker",
    "docker restart/stop/kill (container lifecycle)": "перезапуск контейнеров",
}
#: Описания по-русски, когда правила нет в словаре (показываем смысл, не технику).
GENERIC_DESCRIPTION = "команда, которую Hermes считает опасной"
FLOOR_NOTE = "жёсткий запрет политики Hermes — разрешить нельзя"
NO_GRANT_NOTE = "нельзя разрешить штатно — есть другой способ: файл и терминал"
PENDING_NOTE = "ждёт вашего решения"
ONCE_NOTE = "разрешено до конца этой работы"
ALWAYS_NOTE = "разрешено навсегда — отозвать можно в настройках"
DENIED_NOTE = "запрещено вами"
EXPIRED_NOTE = "разрешение истекло"
HINTED_NOTE = "участнику подсказано обойтись терминалом — ждать нечего"
INFO_NOTE = "Hermes запрещает настрого — участнику передано искать другой способ"
CLOSED_NOTE = ("работа завершена — закрыто автоматически; если блокировка ещё нужна, "
               "повторите работу кнопкой «Доработать»")
DISMISSED_NOTE = "закрыто как неактуальное"
TRUSTED_NOTE = "профилю доверен запуск кода на срок — отозвать можно здесь"
#: Текст подсказки участнику (способ A): точная формулировка — в контракте агентов.
HINT_CODE = ("execute_code недоступен в фоновой работе: сделай через файл и терминал — "
             "запиши скрипт файлом (write_file) и запусти `python файл.py`. "
             "Разрешение «по виду» на терминал уже действует.")

_DANGEROUS_RE = re.compile(r"flagged as dangerous \(([^)]{3,200})\)", re.I)
_MATCHED_RE = re.compile(r"matched '([^']{3,200})'", re.I)
_EXECUTE_CODE_RE = re.compile(r"execute_code", re.I)
#: Признак «окна разрешения не было»: режим без человека (``-q``/cron/платформа).
_UNATTENDED_RE = re.compile(r"single-query|\(-q\)|cron|unattended|no user present", re.I)
#: Жёсткий запрет Hermes (снимать нечем).
_FLOOR_RE = re.compile(r"BLOCKED \(hardline\)", re.I)
#: Что Hermes пишет в блокировке execute_code.
_CODE_BLOCK_RE = re.compile(r"execute_code runs arbitrary local Python", re.I)
#: Явное признание блокировки в итоге работы (по нему считаем «нужен ответ»).
_SAYS_BLOCKED_RE = re.compile(r"BLOCKED|заблокирован|запрещено политикой|снят[оа] разрешени", re.I)

_TOOL_TERMINAL = ("terminal", "shell", "bash", "run_command")
_APPROVALS_FILE = "approvals.json"


class ApprovalError(Exception):
    """Ошибка действия: ``code`` — для API, ``http`` — код ответа (404/409/422)."""

    def __init__(self, message: str, http: int = 400, code: str = "bad_request",
                 detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.http = int(http)
        self.code = code
        self.detail = detail or {}


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- домашний каталог

def hermes_root(cfg) -> Path:
    """Корень Hermes для разрешений: реальный домашний каталог или поддельный в песочнице."""
    if getattr(cfg, "sandbox", False):
        # Демо-сервер не тренируется на живых конфигах: пишем в свой поддельный дом.
        return Path(cfg.data_dir) / "hermes-home"
    return Path(getattr(cfg, "hermes_home", "") or "")


def profile_home(cfg, profile) -> Path:
    """``HERMES_HOME`` профиля: ``default`` — корень, остальные — ``profiles/<имя>``."""
    name = str(profile or "").strip() or "default"
    root = hermes_root(cfg)
    if name == "default":
        return root
    return root / "profiles" / name


def config_path(cfg, profile) -> Path:
    return profile_home(cfg, profile) / "config.yaml"


def state_path(cfg, profile) -> Path:
    """``state.db`` профиля — там же, где его конфиг (в песочнице — в поддельном доме).

    Важно: путь считается от ТОГО ЖЕ корня, что и запись разрешения. Иначе песочница
    читала бы сессии реальных профилей (приватность) и выдавала бы разрешения не туда.
    """
    return profile_home(cfg, profile) / "state.db"


def _connect_state(cfg, profile):
    """Соединение с ``state.db`` профиля только на чтение (``mode=ro``)."""
    return wl._connect(state_path(cfg, profile))


def _recent_sessions(cfg, profile: str, after: int, limit: int = 8) -> list[dict]:
    """Последние сессии профиля — запасной путь поиска сессии карточки."""
    con = _connect_state(cfg, profile)
    if con is None:
        return []
    try:
        rows = con.execute("SELECT id, started_at, last_activity_at FROM sessions "
                           "WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
                           (int(after), max(1, int(limit)))).fetchall()
    except Exception:                        # noqa: BLE001 - старая схема
        rows = []
    finally:
        con.close()
    return [{"id": str(r["id"]), "started_at": wl._int(r["started_at"]),
             "last_activity_at": wl._int(r["last_activity_at"])} for r in rows]


def _first_user_text(cfg, profile: str, sid: str) -> str:
    """Первая реплика пользователя сессии (кеш по свежести файла)."""
    return _cached("first", (str(profile), str(sid), _db_stamp(cfg, profile)),
                   lambda: _read_first_text(cfg, profile, sid))


def _read_first_text(cfg, profile: str, sid: str) -> str:
    """Первая реплика пользователя сессии: в ней движок пишет id карточки."""
    con = _connect_state(cfg, profile)
    if con is None:
        return ""
    try:
        row = con.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                          "ORDER BY id LIMIT 1", (sid,)).fetchone()
    except Exception:                        # noqa: BLE001
        row = None
    finally:
        con.close()
    return str(row["content"] or "") if row is not None else ""


def _session_meta(cfg, profile: str, sid: str) -> dict:
    """Строка ``sessions``: время начала/конца, счётчики."""
    con = _connect_state(cfg, profile)
    if con is None:
        return {}
    try:
        row = con.execute("SELECT started_at, ended_at, message_count, tool_call_count "
                          "FROM sessions WHERE id = ?", (sid,)).fetchone()
    except Exception:                        # noqa: BLE001
        row = None
    finally:
        con.close()
    if row is None:
        return {}
    return {"started_at": wl._int(row["started_at"]), "ended_at": wl._int(row["ended_at"]),
            "messages": wl._int(row["message_count"]), "tools": wl._int(row["tool_call_count"])}


def _sessions(cfg, board: str, cards: dict) -> dict:
    """Сессии карточек: ``{session_id: {profile, card_id, …}}``.

    Основной путь — метаданные прогона карточки (``worker_session_id``) и событие допуска
    ``owner_admitted`` (то же, что у «Хода работы»), запасной — первая реплика
    пользователя сессии с id карточки. Затем добираем ОСТАЛЬНЫЕ сессии профиля, в
    которых упомянута карточка: у перезапущенной карточки их несколько.
    """
    out: dict = {}
    if not cards:
        return out
    con = wl._connect(cfg.kanban_db(board))
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
                            "source": "run", "started_at": wl._int((run or {}).get("started_at"))}
    finally:
        if con is not None:
            con.close()
    by_card: dict = {}
    for sid, item in out.items():
        by_card.setdefault(item["card_id"], set()).add(sid)
    for cid, card in sorted(cards.items()):
        profile = str(card.get("assignee") or "")
        if not profile:
            continue
        base = wl._int(card.get("started_at")) or wl._int(card.get("created_at"))
        try:
            recent = _recent_sessions(cfg, profile, after=max(0, base - 900))
        except Exception:                    # noqa: BLE001 - доска недоступна
            recent = []
        for session in recent:
            sid = str(session.get("id") or "")
            if not sid or sid in out:
                continue
            if cid and cid in _first_user_text(cfg, profile, sid):
                out[sid] = {"session_id": sid, "profile": profile, "card_id": cid,
                            "source": "message", "started_at": session.get("started_at")}
                by_card.setdefault(cid, set()).add(sid)
    for sid, item in out.items():
        item.update({k: v for k, v in _session_meta(cfg, item["profile"], sid).items()
                     if k not in item or not item.get(k)})
    return out


def _profile_ok(profile: str) -> str:
    """Имя профиля: буквы, цифры, точка, дефис, подчёркивание (без путей)."""
    name = str(profile or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}", name):
        raise ApprovalError("Непонятное имя профиля", 400, "bad_request",
                            {"field": "profile", "value": name[:40]})
    return name


def _ensure_home(cfg, profile) -> Path:
    """Создать поддельный дом песочницы, чтобы демо-нажатие не падало.

    В БОЕВОМ режиме ничего не создаём: нет каталога профиля — значит нет и профиля,
    и разрешение туда писать нельзя (``422`` от подпроцесса).
    """
    home = profile_home(cfg, profile)
    if home.is_dir():
        return home
    if not getattr(cfg, "sandbox", False):
        return home
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    if not path.is_file():
        path.write_text("# поддельный профиль песочницы (демо-сервер)\n"
                        "approvals:\n  mode: smart\n  single_query_mode: deny\n"
                        "command_allowlist: []\n", encoding="utf-8")
    return home


# ---------------------------------------------------------------- интерпретатор

_PYTHON_CACHE: dict = {}
#: Сколько ждём проверку «интерпретатор знает Hermes».
PROBE_TIMEOUT = 40


def python_candidates(cfg) -> list[str]:
    """Кандидаты: явный ``TEAMROOM_HERMES_PYTHON``, затем venv'ы установок Hermes.

    Пути автора не зашиты: только ``cfg.hermes_home`` и ``%LOCALAPPDATA%``.
    """
    found: list[str] = []
    explicit = os.environ.get("TEAMROOM_HERMES_PYTHON")
    if explicit is not None:
        return [str(explicit).strip()] if str(explicit).strip() else []
    roots = []
    home = Path(getattr(cfg, "hermes_home", "") or "")
    if str(home):
        roots.append(home)
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if local:
        roots.append(Path(local) / "hermes")
    for path in [sys.executable]:
        if path and path not in found:
            found.append(path)
    for base in roots:
        try:
            pattern = str(base / "installs" / "*" / "environments" / "*" /
                          "venv" / "Scripts" / "python.exe")
            for path in sorted(glob.glob(pattern), reverse=True):
                if path not in found:
                    found.append(path)
        except OSError:
            continue
    return found


def hermes_tree(cfg) -> str:
    """Дерево ``hermes-agent`` (пакет ``tools``) — для ``sys.path`` подпроцесса.

    Порядок: рядом с домашним каталогом Hermes, рядом с профилем, в приложении и, для
    тестов/песочницы с поддельным домом, — в обычном месте установки Hermes
    (``%LOCALAPPDATA%/hermes``). Код Hermes только читается, профиль не трогается.
    """
    home = Path(getattr(cfg, "hermes_home", "") or "")
    candidates = [home / "hermes-agent", home.parent / "hermes-agent",
                  Path(__file__).resolve().parent.parent / "hermes-agent"]
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if local:
        candidates.append(Path(local) / "hermes" / "hermes-agent")
    for candidate in candidates:
        if (candidate / "tools" / "approval.py").is_file():
            return str(candidate)
    return ""


def _has_hermes(python: str, tree: str, timeout: int = PROBE_TIMEOUT) -> bool:
    """Проверить, что интерпретатор видит пакет ``tools`` Hermes."""
    if not python or not Path(python).is_file():
        return False
    code = "import sys; sys.path.insert(0, %r); import tools.approval" % (tree or "")
    try:
        res = proc.run([python, "-c", code], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return getattr(res, "returncode", 1) == 0


def find_python(cfg, refresh: bool = False) -> str | None:
    """Интерпретатор Hermes (умеет ``import tools.approval``); ``None`` — не нашли.

    Результат кешируется в памяти процесса: поиск — это запуск подпроцесса на каждого
    кандидата, а нужен он на каждое нажатие «разрешить». Исчезнувший файл (Hermes
    обновился) кеш не отдаёт — ищем заново, как ``server/stt_worker.find_python``.
    """
    key = str(getattr(cfg, "hermes_home", "")) + "|" + str(bool(getattr(cfg, "sandbox", False)))
    cached = _PYTHON_CACHE.get(key)
    if cached and not refresh and Path(str(cached)).is_file():
        return str(cached)
    tree = hermes_tree(cfg)
    for path in python_candidates(cfg):
        if _has_hermes(path, tree):
            _PYTHON_CACHE[key] = str(path)
            return str(path)
    return None


def reset_python_cache() -> None:
    """Сбросить кеш поиска интерпретатора (тесты)."""
    _PYTHON_CACHE.clear()


# ---------------------------------------------------------------- подпроцесс записи

def worker_path() -> Path:
    return Path(__file__).resolve().parent / WORKER


#: Замки на домашний каталог профиля: две одновременные записи в ОДИН `config.yaml`
#: читают файл, меняют и пишут — без замка вторая запись затирает первую.
_WORKER_LOCKS: dict = {}
_WORKER_GUARD = threading.RLock()


def _worker_lock(home) -> threading.RLock:
    """Замок записи разрешений конкретного дома профиля."""
    key = os.path.normcase(os.path.abspath(str(home or "")))
    with _WORKER_GUARD:
        lock = _WORKER_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WORKER_LOCKS[key] = lock
        return lock


def run_worker(cfg, request: dict, timeout: int = WORKER_TIMEOUT) -> dict:
    """Выполнить операцию записи интерпретатором Hermes. Никогда не бросает наружу OSError.

    Запуск сериализован по домашнему каталогу профиля (``_worker_lock``): «разрешать
    всегда» для двух команд одного участника, нажатое подряд, иначе потеряло бы одну из
    записей — обе операции читают список, дополняют его и пишут файл целиком.
    """
    python = find_python(cfg)
    if not python:
        raise ApprovalError(
            "Не нашёл интерпретатор Hermes — разрешение некуда записать. "
            "Проверьте, что Hermes установлен на этом компьютере.",
            422, "no_hermes")
    payload = dict(request or {})
    payload.setdefault("tree", hermes_tree(cfg))
    try:
        with _worker_lock(payload.get("home") or ""):
            res = proc.run([python, str(worker_path()), json.dumps(payload, ensure_ascii=False)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
    except Exception as exc:                                        # noqa: BLE001
        raise ApprovalError("Не удалось запустить запись разрешения: %s"
                            % (str(exc)[:200] or exc.__class__.__name__), 502, "failed") from exc
    raw = (getattr(res, "stdout", b"") or b"")
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    line = ""
    for candidate in text.splitlines():
        candidate = candidate.strip()
        if candidate.startswith("{"):
            line = candidate
    if not line:
        err = (getattr(res, "stderr", b"") or b"")
        err_text = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err or "")
        raise ApprovalError("Hermes не ответил на запись разрешения: %s"
                            % (" ".join(err_text.split())[-300:] or "нет ответа"), 502, "failed")
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise ApprovalError("Hermes ответил непонятно на запись разрешения", 502,
                            "failed") from exc
    if not isinstance(data, dict):
        raise ApprovalError("Hermes ответил непонятно на запись разрешения", 502, "failed")
    if not data.get("ok"):
        code = str(data.get("code") or "failed")
        http = 422 if code == "no_hermes" else (400 if code == "bad_request" else 502)
        raise ApprovalError(str(data.get("message") or "Не удалось записать разрешение"),
                            http, code, {"worker": code})
    return data


# ---------------------------------------------------------------- текст

def describe(pattern_key, kind: str = "terminal", raw: str = "") -> str:
    """Причина по-русски: «запуск скрипта через heredoc», «execute_code — произвольный код»."""
    if kind == "execute_code":
        return PATTERN_RU["execute_code"]
    key = str(pattern_key or "").strip()
    if key in PATTERN_RU:
        return PATTERN_RU[key]
    low = key.lower()
    for known, text in PATTERN_RU.items():
        if known.lower() == low:
            return text
    if raw:
        return " ".join(str(raw).split())[:120]
    return GENERIC_DESCRIPTION


def _one_line(text, limit: int = 0) -> str:
    out = wl.mask(text)
    return wl.trim(out, limit) if limit else out


# ---------------------------------------------------------------- реестр комнаты

def registry_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / _APPROVALS_FILE


# ---------------------------------------------------------------- кеш чтения сессий

#: Кеш разбора `state.db` по (файл, mtime, размер):
#: ``GET /api/requests/{id}`` не должен перечитывать сессии профилей при каждом открытии.
#: Любая запись в `state.db` меняет mtime/размер → кеш сам становится недействительным,
#: поэтому «устаревших» ответов не бывает: пока файл не менялся, ответ тот же.
_SCAN: dict = {}


def _db_stamp(cfg, profile) -> tuple:
    """Ключ свежести чтения: путь, mtime и размер файла `state.db`."""
    path = state_path(cfg, profile)
    try:
        info = path.stat()
        mtime = int(getattr(info, "st_mtime_ns", 0) or int(info.st_mtime * 1000000000))
        return (str(path), mtime, int(info.st_size))
    except OSError:
        return (str(path), 0, 0)


def _cached(kind: str, key: tuple, build):
    """Взять результат из кеша или посчитать. Сбой кеша не мешает чтению."""
    full = (kind,) + tuple(key)
    try:
        if full in _SCAN:
            return _SCAN[full]
    except TypeError:                       # некхешируемый ключ
        return build()
    value = build()
    if len(_SCAN) > SCAN_CACHE_MAX:
        _SCAN.clear()
    try:
        _SCAN[full] = value
    except TypeError:                       # pragma: no cover - ключ уже проверен
        pass
    return value


def reset_cache() -> None:
    """Сбросить кеш чтения сессий (тесты, ручная перепроверка)."""
    _SCAN.clear()


def _registry(cfg, room_id) -> dict:
    """Реестр комнаты: ``{approvals, grants, trusts}``.

    ``trusts`` — доверие запуску кода профилю (``approvals.single_query_mode:
    approve``), выданное человеком на срок (W26-D1): без записи здесь отозвать
    запись из профиля было бы нечем.
    """
    try:
        data = sf.read_json(registry_path(cfg, room_id), {})
    except Exception:                       # noqa: BLE001 - реестр вторичен
        data = {}
    if not isinstance(data, dict):
        data = {}
    approvals = data.get("approvals")
    grants = data.get("grants")
    trusts = data.get("trusts")
    return {"approvals": approvals if isinstance(approvals, dict) else {},
            "grants": [g for g in (grants or []) if isinstance(g, dict)],
            "trusts": [t for t in (trusts or []) if isinstance(t, dict)]}


def _save_registry(cfg, room_id, data: dict) -> None:
    """Записать реестр атомарно и под замком файла (как остальные файлы состояния)."""
    path = registry_path(cfg, room_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sf.lock_for(path):
        sf.write_json_atomic(path, data)


def _journal(cfg, room_id, text: str, kind: str = "approval") -> None:
    """Строка в журнал комнаты: «разрешено/отозвано, кем, что, когда»."""
    try:
        path = Path(cfg.state_dir) / str(room_id) / "journal.jsonl"
        with sf.lock_for(path):
            events = sf.read_jsonl(path)
            number = 0
            for event in events:
                ident = str((event or {}).get("id") or "")
                if ident.startswith("j_") and ident[2:].isdigit():
                    number = max(number, int(ident[2:]))
            sf.append_jsonl(path, {"id": "j_%04d" % (number + 1), "room": str(room_id),
                                   "at": _now(), "kind": kind, "actor": ACTOR,
                                   "text": _one_line(text, 600)})
    except Exception:                       # noqa: BLE001 - журнал вторичен
        pass


def _journal_level(cfg, room, item: dict, decision: str) -> None:
    """Строка в журнал УРОВНЯ автономии: «что агент сделал сам / что решил человек».

    W27-E5 §2: журнал уровня отвечает на вопрос «что разрешено по уровню», он НЕ
    пополняет «нужно от вас» (у него нет счётчика). Сбой журнала решение не ломает.
    """
    try:
        try:
            from . import autonomy as autonomy_mod
        except ImportError:                              # pragma: no cover
            import autonomy as autonomy_mod              # type: ignore
        autonomy_mod.note_decision(cfg, room, item, decision)
    except Exception:                                    # noqa: BLE001 - журнал вторичен
        pass


# ---------------------------------------------------------------- поиск блокировок

def approval_id(task_id: str, pattern_key: str, command: str, kind: str = "terminal") -> str:
    """``ap_<8hex>``: тот же (карточка, правило, команда) = один и тот же запрос.

    Для ``execute_code`` команда в ключ не входит: блокируется сам инструмент, а код
    каждый раз другой — иначе у одной карточки появлялись бы десятки одинаковых по
    смыслу запросов «разрешить произвольный код».
    """
    body = "\u0001".join([str(task_id or ""), str(pattern_key or ""),
                          "" if kind == "execute_code" else
                          hashlib.sha1(str(command or "").encode("utf-8")).hexdigest()])
    return "ap_" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]


def _payload_text(content) -> str:
    """Текст результата инструмента: JSON Hermes (``error``) или как есть."""
    raw = "" if content is None else str(content)
    text = raw.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
        except ValueError:
            return raw
        if isinstance(data, dict):
            for key in ("error", "message", "output", "detail"):
                if isinstance(data.get(key), str) and data.get(key).strip():
                    return data[key]
    return raw


def _is_blocked(text: str) -> bool:
    """Это отказ по политике (а не обычная ошибка команды)?"""
    raw = str(text or "")
    if "BLOCKED" not in raw:
        return False
    if _FLOOR_RE.search(raw):
        return True
    return bool(_UNATTENDED_RE.search(raw) or _CODE_BLOCK_RE.search(raw))


def _kind_of(tool_name: str, text: str) -> str:
    name = str(tool_name or "").strip().lower()
    if name == "execute_code" or _CODE_BLOCK_RE.search(str(text or "")):
        return "execute_code"
    return "terminal"


def _pattern_of(text: str, kind: str) -> tuple:
    """``(pattern_key, floor)`` из текста блокировки."""
    raw = str(text or "")
    if _FLOOR_RE.search(raw):
        return (None, True)                 # жёсткий запрет: ключа правила в тексте нет
    match = _DANGEROUS_RE.search(raw)
    if match:
        return (" ".join(match.group(1).split()), False)
    if kind == "execute_code":
        return ("execute_code", False)
    match = _MATCHED_RE.search(raw)
    if match:
        return (" ".join(match.group(1).split()), False)
    return (None, False)


def _find_command(calls: list, kind: str) -> tuple:
    """Команда (или код) из аргументов вызова инструмента; ``(текст, имя инструмента)``."""
    for name, args in reversed(calls or []):
        tool = str(name or "").strip().lower()
        data = args if isinstance(args, dict) else {}
        if kind == "execute_code" and tool == "execute_code":
            for key in ("code", "script", "source", "python"):
                if isinstance(data.get(key), str) and data[key].strip():
                    return data[key], name
        if kind == "terminal" and tool in _TOOL_TERMINAL:
            for key in ("command", "cmd", "script"):
                if isinstance(data.get(key), str) and data[key].strip():
                    return data[key], name
    return "", ""


def _session_messages(cfg, profile: str, sid: str, limit: int = MAX_ROWS) -> list:
    """Последние строки сессии (``mode=ro``): только ассистент и результаты инструментов."""
    con = _connect_state(cfg, profile)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT id, role, content, tool_calls, tool_name, timestamp FROM messages "
            "WHERE session_id = ? AND role IN ('assistant', 'tool') "
            "AND COALESCE(active, 1) = 1 AND COALESCE(compacted, 0) = 0 "
            "ORDER BY id DESC LIMIT ?", (sid, max(1, int(limit)))).fetchall()
    except Exception:                       # noqa: BLE001 - старая схема
        rows = []
    finally:
        con.close()
    rows = list(rows)
    rows.reverse()
    return rows


def _final_text(cfg, profile: str, sid: str) -> str:
    """Последняя реплика ассистента (кеш по свежести файла)."""
    return _cached("final", (str(profile), str(sid), _db_stamp(cfg, profile)),
                   lambda: _read_final_text(cfg, profile, sid))


def _read_final_text(cfg, profile: str, sid: str) -> str:
    """Последняя реплика ассистента — по ней видно, признал ли он блокировку."""
    con = _connect_state(cfg, profile)
    if con is None:
        return ""
    try:
        row = con.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' "
                          "AND COALESCE(content, '') <> '' ORDER BY id DESC LIMIT 1",
                          (sid,)).fetchone()
    except Exception:                       # noqa: BLE001
        row = None
    finally:
        con.close()
    return str(row["content"] or "") if row is not None else ""


def _session_blocks(cfg, profile: str, sid: str, task_id: str) -> list[dict]:
    """Блокировки одной сессии для КОНКРЕТНОЙ карточки (разбор сессии — из кеша).

    Разбор сессии не зависит от карточки, поэтому он считается один раз на файл, а
    здесь только проставляется ``task_id``. Без этого у запроса из 19 карточек одна и та
    же сессия разбиралась 19 раз, и страница запроса открывалась ~0,8 с.
    """
    blocks = _cached("blocks", (str(profile), str(sid), _db_stamp(cfg, profile)),
                     lambda: _parse_blocks(cfg, profile, sid))
    out: list[dict] = []
    for block in blocks or []:
        item = dict(block)
        item["task_id"] = str(task_id or "")
        out.append(item)
    return out


def _parse_blocks(cfg, profile: str, sid: str) -> list[dict]:
    """Все блокировки сессии — по порядку, как они случились (без привязки к карточке)."""
    out: list[dict] = []
    calls: list = []
    for row in _session_messages(cfg, profile, sid):
        role = str(row["role"] or "")
        if role == "assistant":
            calls = wl.tool_calls(row["tool_calls"]) or calls
            continue
        text = _payload_text(row["content"])
        if not _is_blocked(text):
            continue
        kind = _kind_of(row["tool_name"], text)
        pattern_key, floor = _pattern_of(text, kind)
        command, tool = _find_command(calls, kind)
        if not command:
            command = " ".join(str(text).split())[:200]
        out.append({"task_id": "", "profile": profile, "kind": kind,
                    "command": command, "tool": str(tool or row["tool_name"] or ""),
                    "pattern_key": pattern_key, "floor": floor,
                    "at": wl._int(row["timestamp"]), "session_id": sid,
                    "text": " ".join(str(text).split())[:400]})
    return out


def _card_state(cfg, room, task_ids: list) -> dict:
    board = str((room or {}).get("board") or getattr(cfg, "board", "") or "default")
    return wl._card_rows(cfg, board, [str(t) for t in task_ids if t])


def _label(room, profile: str) -> str:
    try:
        return wl._label_of(room or {}, profile)
    except Exception:                       # noqa: BLE001 - подпись вторична
        return str(profile or "участник")


def _grantable(kind: str, pattern_key, floor: bool) -> bool:
    """Можно ли разрешить эту блокировку штатно."""
    if floor or not pattern_key:
        return False
    if kind == "execute_code":
        # Hermes в режиме без человека не сверяет список разрешений для execute_code
        # (гейт `check_execute_code_guard` отвечает отказом ДО проверки allowlist).
        return False
    return True


def mode_of(kind: str, pattern_key, floor: bool) -> str:
    """Вид блокировки: ``grant`` (можно разрешить), ``workaround`` (есть способ), ``floor``.

    W26-D1 (T1): вид определяет, «ждёт решения» это или информация. У недоговорной
    блокировки кнопок «разрешить» быть не может, поэтому она и не ``pending``.
    """
    if floor:
        return "floor"
    return "grant" if _grantable(kind, pattern_key, floor) else "workaround"


def actions_for(mode: str, state: str) -> list[dict]:
    """Кнопки строки блокировки — у каждого пункта «ждёт вас» хотя бы одна (T1/T3).

    * ``grant`` — обычное разрешение (один раз / всегда / отклонить);
    * ``workaround`` — нельзя штатно, но есть способ: подсказать участнику, доверить
      профилю запуск кода (со сроком) или закрыть как неактуальное;
    * ``floor`` — информация: сообщить участнику и скрыть.
    Решённые строки — история, кнопок не имеют (это не «ждёт решения»).
    """
    if str(state or "") not in UNDECIDED:
        return []
    if mode == "grant":
        return [{"id": key, "label": ACTION_LABEL[key], "kind": "decision"}
                for key in ("once", "always", "deny")]
    if mode == "workaround":
        return [{"id": "hint_agent", "label": ACTION_LABEL["hint_agent"], "kind": "workaround"},
                {"id": "trust_profile", "label": ACTION_LABEL["trust_profile"],
                 "kind": "workaround", "ttl_required": True,
                 "ttl_options": [dict(option) for option in TTL_OPTIONS]},
                {"id": "dismiss", "label": ACTION_LABEL["dismiss"], "kind": "close"}]
    return [{"id": "hint_agent", "label": ACTION_LABEL["hint_agent"], "kind": "notice"},
            {"id": "dismiss", "label": ACTION_LABEL["hide"], "kind": "close"}]



def find(cfg, room, root_id: str, extra=None) -> list[dict]:
    """Найденные блокировки карточек запроса (без состояния — только факты)."""
    room = room if isinstance(room, dict) else {}
    board = str(room.get("board") or getattr(cfg, "board", "") or "default")
    try:
        from .engine_write_guard import tree_ids
    except ImportError:                                  # pragma: no cover
        from engine_write_guard import tree_ids          # type: ignore
    ids = {str(root_id)} if root_id else set()
    try:
        ids |= {str(i) for i in tree_ids(cfg, board, str(root_id)) if i}
    except Exception:                                    # noqa: BLE001 - доска недоступна
        pass
    for item in extra or []:
        if str(item or "").strip():
            ids.add(str(item).strip())
    cards = _card_state(cfg, room, sorted(ids))
    sessions = _sessions(cfg, board, cards)
    by_card: dict = {}
    for sid, session in sorted(sessions.items()):
        card_id = str(session.get("card_id") or "")
        profile = str(session.get("profile") or "")
        if not card_id or not profile:
            continue
        by_card.setdefault(card_id, []).append((sid, profile))
    # у перезапущенной карточки бывает несколько прогонов (сессий) — `_sessions` уже
    # добирает их по первой реплике пользователя с id карточки
    found: dict = {}
    for card_id, pairs in sorted(by_card.items()):
        card = cards.get(card_id) or {}
        finished = str(card.get("status") or "") in FINISHED
        for sid, profile in pairs:
            # «агент прямо написал о блокировке» — только по последней реплике, не догадка
            stopped = finished and bool(_SAYS_BLOCKED_RE.search(_final_text(cfg, profile, sid)))
            for block in _session_blocks(cfg, profile, sid, card_id):
                key = approval_id(card_id, block["pattern_key"], block["command"],
                                  block.get("kind") or "terminal")
                item = found.get(key)
                if item is None:
                    item = dict(block)
                    item["id"] = key
                    item["count"] = 0
                    item["card"] = card
                    item["member_title"] = _label(room, profile)
                    item["found_at"] = block["at"] or _now()
                    item["stopped"] = stopped
                    found[key] = item
                item["count"] = int(item.get("count") or 0) + 1
                item["stopped"] = bool(item.get("stopped")) or stopped
                stamp = int(block["at"] or _now())
                item["at"] = min(int(item.get("at") or stamp), stamp)
                # последняя блокировка: по ней видно, что участник упёрся СНОВА
                item["last_at"] = max(int(item.get("last_at") or 0), stamp)
    return list(found.values())


def _recompute_state(record: dict, now: int) -> str:
    state = str(record.get("state") or "pending")
    if state == "granted_once":
        expires = int(record.get("expires_at") or 0)
        if expires and expires <= now:
            return "expired"
    return state


def _note_for(item: dict) -> str:
    state = str(item.get("state") or "pending")
    if item.get("floor") and state in UNDECIDED:
        return FLOOR_NOTE
    if state == "pending" and not item.get("grantable"):
        return NO_GRANT_NOTE
    return {"pending": PENDING_NOTE, "granted_once": ONCE_NOTE,
            "granted_always": ALWAYS_NOTE, "denied": DENIED_NOTE,
            "expired": EXPIRED_NOTE, "workaround": NO_GRANT_NOTE,
            "hinted": HINTED_NOTE, "info": INFO_NOTE, "closed": CLOSED_NOTE,
            "dismissed": DISMISSED_NOTE, "trusted": TRUSTED_NOTE}.get(state, PENDING_NOTE)


# ---------------------------------------------------------------- группы (W22-ST)

def group_id(task_id: str, profile: str, pattern_key: str) -> str:
    """``gr_<8hex>``: «одна причина блокировки» = участник + правило + карточка.

    Разрешение «на вид» (``command_allowlist``) выдаётся на КЛАСС команд, поэтому
    десять разных ``python -c …`` одного участника в одной карточке — один вопрос
    («запуск скрипта через -e/-c»), а не десять. Разные команды остаются видны
    списком внутри строки (``commands[]``).
    """
    body = "\u0001".join([str(task_id or ""), str(profile or ""), str(pattern_key or "")])
    return "gr_" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]


def _same_reason(items: list[dict]) -> dict:
    """Схлопнуть одинаковые по причине блокировки в одну строку со счётчиком.

    Группируем ТОЛЬКО ждущие (``pending``) запросы с известным ``pattern_key``:
    решённые — это история (их решение у каждого своё), а записи без правила
    (``floor``) сливать нечестно: разрешить их всё равно нельзя, и каждая команда
    должна быть видна отдельно.

    В получившейся строке:

    * ``id`` — самый свежий запрос группы (на него и «нажимают»);
    * ``group`` — ключ группы, ``approval_ids`` — все запросы группы (свежие первыми),
      решение по строке применяется ко ВСЕМ сразу (`decide`);
    * ``command`` — последняя команда, ``commands`` — все различные команды с числом
      повторов и своей меткой времени (первая — последняя);
    * ``count`` — сколько блокировок всего (как и раньше: повторные — счётчиком).
    """
    groups: dict = {}
    out: list[dict] = []
    for item in items:
        pattern_key = str(item.get("pattern_key") or "")
        if item.get("state") != "pending" or not pattern_key:
            out.append(item)
            continue
        key = group_id(item.get("task_id"), item.get("profile"), pattern_key)
        bucket = groups.get(key)
        if bucket is None:
            groups[key] = [item]
        else:
            bucket.append(item)
    for key, members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        members.sort(key=lambda row: (int(row.get("at") or 0), str(row.get("id"))), reverse=True)
        base = dict(members[0])
        commands: list[dict] = []
        seen_commands = set()
        for row in members:
            command = str(row.get("command") or "")
            mark = command if command else str(row.get("id"))
            if mark in seen_commands:
                for item in commands:
                    if (item["command"] or item["id"]) == mark:
                        item["count"] += max(1, int(row.get("count") or 1))
                        break
                continue
            seen_commands.add(mark)
            commands.append({"id": str(row.get("id") or ""), "command": command,
                             "at": int(row.get("at") or 0),
                             "count": max(1, int(row.get("count") or 1))})
        base["group"] = key
        base["approval_ids"] = [str(row.get("id") or "") for row in members]
        base["commands"] = commands
        base["command"] = commands[0]["command"] if commands else base.get("command")
        base["count"] = sum(max(1, int(row.get("count") or 1)) for row in members)
        base["at"] = min(int(row.get("at") or 0) for row in members) or base.get("at")
        base["last_at"] = max(int(row.get("last_at") or row.get("at") or 0) for row in members)
        base["stopped"] = any(bool(row.get("stopped")) for row in members)
        out.append(base)
    return out


def collect(cfg, room, root_id: str, extra=None, now: int | None = None,
            collapse: bool = True) -> list[dict]:
    """Список запросов разрешения запроса: найденное + решения из реестра.

    Идемпотентно: повторная блокировка той же команды — это тот же запрос со счётчиком
    ``count``. Решения (разрешено/запрещено) берутся из реестра комнаты и не теряются,
    когда сессия карточки уже пропала из ``state.db``.

    ``collapse=True`` (по умолчанию) — одинаковые по причине блокировки сводятся в одну
    строку со счётчиком (W22-ST); ``collapse=False`` — по одному запросу на команду
    (нужно `decide`, чтобы решить ВСЕ запросы группы).
    """
    now = _now() if now is None else int(now)
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    reg = _registry(cfg, room_id) if room_id else {"approvals": {}, "grants": []}
    stored = reg["approvals"]
    live_keys = {(str(grant.get("profile") or ""), str(grant.get("pattern_key") or ""))
                 for grant in reg.get("grants") or []
                 if str(grant.get("scope") or "") == "always"
                 or int(grant.get("expires_at") or 0) > now}
    items: list[dict] = []
    seen = set()
    for raw in find(cfg, room, root_id, extra):
        record = stored.get(raw["id"])
        item = _public(raw, record, now, live_keys=live_keys)
        items.append(item)
        seen.add(raw["id"])
    # запомненные решения, которых больше не видно в сессиях (сессия убрана/сжата)
    for ident, record in stored.items():
        if ident in seen or not isinstance(record, dict):
            continue
        if str(record.get("root_id") or "") != str(root_id or ""):
            continue
        items.append(_public(record, record, now, only_record=True))
    # W22-ST: одинаковые по причине блокировки — одна строка со счётчиком «×N»
    if collapse:
        items = _same_reason(items)
    # W26-D1: первыми — то, что правда ждёт человека, дальше «есть способ», потом инфо.
    items.sort(key=lambda item: (_STATE_RANK.get(str(item.get("state") or ""), 9),
                                 -int(item.get("last_at") or item.get("at") or 0)))
    return items[:MAX_PER_REQUEST]


def _severity(pattern_key, command: str, floor: bool) -> str:
    """Вид блокировки для интерфейса: ``critical`` | ``routine`` (W27-E5 §2).

    Считает модуль уровней автономии (``server/autonomy.py``); его отсутствие или
    сбой вида не ломают выдачу разрешений — тогда всё «рутина».
    """
    try:
        try:
            from . import autonomy as autonomy_mod
        except ImportError:                              # pragma: no cover
            import autonomy as autonomy_mod              # type: ignore
        return autonomy_mod.severity_of(pattern_key, command, bool(floor))
    except Exception:                                    # noqa: BLE001 - вид вторичен
        return "routine"


def _public(raw: dict, record: dict | None, now: int, only_record: bool = False,
            live_keys=None) -> dict:
    """Запрос разрешения наружу (поля контракта W22 §2.1)."""
    record = record if isinstance(record, dict) else {}
    merged = dict(raw)
    for key in ("state", "decided_at", "expires_at", "grant_scope", "decision_note",
                "closed_reason", "closed_at", "ttl_min", "trust_scope"):
        if record.get(key) is not None:
            merged[key] = record[key]
    merged["state"] = _recompute_state(merged, now)
    command = str(raw.get("command") or record.get("command") or "")
    pattern_key = raw.get("pattern_key") or record.get("pattern_key")
    kind = str(raw.get("kind") or record.get("kind") or "terminal")
    floor = bool(raw.get("floor") or record.get("floor"))
    grantable = _grantable(kind, pattern_key, floor)
    mode = mode_of(kind, pattern_key, floor)
    # «Разрешение один раз» живёт до конца работы: если записи в профиле больше нет,
    # а участник СНОВА наткнулся на ту же команду — вопрос снова открыт (это новое
    # нажатие, а не «уже решено»). Иначе — просто истекло.
    if merged["state"] in ("granted_once", "expired") and live_keys is not None:
        profile = str(raw.get("profile") or record.get("profile") or "")
        if (profile, str(pattern_key or "")) not in live_keys:
            fresh = int(raw.get("last_at") or raw.get("at") or 0)
            if fresh > int(record.get("decided_at") or 0):
                merged["state"] = "pending"
                merged["decided_at"] = None
                merged["expires_at"] = None
            elif merged["state"] == "granted_once":
                merged["state"] = "expired"
    card = raw.get("card") if isinstance(raw.get("card"), dict) else {}
    card_state = str(card.get("status") or record.get("card_state") or "")
    # W26-D1 (T1): решение есть только у решённых записей. Недоговорное и жёстко
    # запрещённое — НЕ «ждёт вашего решения»: у них своё состояние и свои действия.
    if not record.get("state"):
        merged["state"] = ("pending" if mode == "grant"
                           else ("info" if mode == "floor" else "workaround"))
    state = str(merged["state"] or "pending")
    # W26-D1 (T2): карточка завершилась — ждать блокировку некому. Закрываем сами,
    # решение человека (разрешено/запрещено/закрыто) остаётся как было.
    closed_reason = merged.get("closed_reason")
    if state in UNDECIDED and card_state in CLOSED_CARDS:
        state = "closed"
        closed_reason = "card_finished"
    actions = actions_for(mode, state)
    item = {
        "id": str(raw.get("id") or record.get("id") or ""),
        "task_id": str(raw.get("task_id") or record.get("task_id") or ""),
        "profile": str(raw.get("profile") or record.get("profile") or ""),
        "member_title": _one_line(raw.get("member_title") or record.get("member_title") or "", 60),
        "kind": kind if kind in KINDS else "terminal",
        "command": _one_line(command, MAX_COMMAND),
        "description": describe(pattern_key, kind),
        # W27-E5 §2: критичные блокировки (разрушительные классы и «наружу») — отдельным
        # заметным видом; поле только для вида, состояние и действия не меняет.
        "severity": _severity(pattern_key, command, floor),
        "pattern_key": pattern_key,
        "floor": floor,
        "grantable": grantable,
        "mode": mode,
        "mode_label": MODE_LABEL.get(mode, ""),
        "state": state,
        "at": int(merged.get("at") or record.get("at") or 0) or now,
        "decided_at": int(merged["decided_at"]) if merged.get("decided_at") else None,
        "count": max(1, int(raw.get("count") or record.get("count") or 1)),
        "card_state": card_state,
        "note": _note_for({"state": state, "floor": floor, "grantable": grantable}),
        "card_title": _one_line(card.get("title") or record.get("card_title") or "", 80),
        "expires_at": int(merged["expires_at"]) if merged.get("expires_at") else None,
        "stopped": bool(raw.get("stopped") or record.get("stopped")),
        "actions": actions,
        "actionable": bool(actions),
    }
    if closed_reason:
        item["closed_reason"] = str(closed_reason)
        item["closed_at"] = int(merged.get("closed_at") or now)
    # W22-ST: строка-группа («одна причина — один вопрос»)
    if raw.get("group"):
        item["group"] = str(raw["group"])
    if isinstance(raw.get("approval_ids"), list):
        item["approval_ids"] = [str(i) for i in raw["approval_ids"] if i]
    if isinstance(raw.get("commands"), list) and raw["commands"]:
        item["commands"] = [{"id": str(row.get("id") or ""),
                             "command": _one_line(row.get("command"), MAX_COMMAND),
                             "at": int(row.get("at") or 0),
                             "count": max(1, int(row.get("count") or 1))}
                            for row in raw["commands"] if isinstance(row, dict)]
    if only_record:
        item["found"] = False
    return item


def pending(items: list[dict]) -> list[dict]:
    """Строки, которые ДЕЙСТВИТЕЛЬНО ждут решения человека: состояние + есть действие.

    W26-D1 (T1): в счётчики «нужно от вас» попадает только то, что человек может
    изменить нажатием. Недоговорная блокировка и жёсткий запрет сюда не входят.
    """
    return [item for item in items
            if item.get("state") == "pending" and (item.get("actions") or [])]


def actionable(items: list[dict]) -> list[dict]:
    """Все строки, которые человек может чем-то закрыть (любое состояние + действия)."""
    return [item for item in items if item.get("actions")]


def needs_you(cfg, room, root_id: str, items: list[dict] | None = None,
              card_status: str = "") -> bool:
    """Нужен ли человек из-за разрешений (для ``needs_you`` и счётчиков).

    W26-D1 (T1): только ``pending`` (значит «можно разрешить») и только с действием.
    Недоговорное, жёстко запрещённое и закрытое автозакрытием — не «ждёт вас».
    """
    items = collect(cfg, room, root_id) if items is None else items
    return bool(pending(items))


# ---------------------------------------------------------------- действия

def _grant_key(profile: str, pattern_key: str) -> str:
    return "%s\u0001%s" % (profile, pattern_key)


def grant_of(reg: dict, profile: str, pattern_key: str) -> dict | None:
    key = _grant_key(profile, pattern_key)
    for grant in reg.get("grants") or []:
        if _grant_key(str(grant.get("profile") or ""), str(grant.get("pattern_key") or "")) == key:
            return grant
    return None


def _comment_text(item: dict, decision: str) -> str:
    """Строка в карточку — от лица Михаила, по-русски, без техники.

    Для строки-группы (W22-ST) честно называем число команд: разрешение (и запрет)
    относится к виду работ, а не к одной строке.
    """
    description = item.get("description") or GENERIC_DESCRIPTION
    count = len([i for i in (item.get("approval_ids") or []) if i])
    more = " (всего команд: %d — относится ко всем такого вида)" % count if count > 1 else ""
    if decision == "deny":
        return ("[Михаил · запрещено: %s] найди другой способ: эту команду выполнять нельзя%s. "
                "Если без неё не обойтись — напиши, что именно нужно, и предложи безопасный "
                "вариант." % (description, more))
    when = "разрешено один раз" if decision == "once" else "разрешено всегда"
    return ("[Михаил · %s: %s] повтори команду — теперь она выполнится.%s"
            % (when, description, more))


def decide(cfg, room, root_id: str, approval_unique: str, decision: str, note: str = "",
           extra=None, act=None, now: int | None = None, ttl=None, ttl_min=None) -> dict:
    """Разрешить один раз / всегда / запретить.

    W26-D1: те же ворота принимают и недоговорные действия — ``hint_agent``
    (подсказать участнику другой способ), ``trust_profile`` (доверить профилю запуск
    кода на срок) и ``dismiss`` (закрыть как неактуальное). Побочные шаги — через
    ``act`` движка.
    """
    now = _now() if now is None else int(now)
    decision = str(decision or "").strip().lower()
    if decision in EXTRA_DECISIONS:
        return act_block(cfg, room, root_id, approval_unique, decision, note=note,
                         extra=extra, act=act, now=now, ttl=ttl, ttl_min=ttl_min)
    if decision not in DECISIONS:
        raise ApprovalError("Непонятное решение: нужно once, always, deny, hint_agent, "
                            "trust_profile или dismiss", 400,
                            "bad_request", {"field": "decision", "value": decision[:20]})
    ident = str(approval_unique or "").strip()
    if not ident:
        raise ApprovalError("Не указан запрос разрешения", 400, "bad_request",
                            {"field": "approval_id"})
    items = collect(cfg, room, root_id, extra, now=now)
    flat = collect(cfg, room, root_id, extra, now=now, collapse=False)
    item = next((row for row in items if row.get("id") == ident), None)
    if item is None:
        raise ApprovalError("Запрос разрешения не найден — возможно, это уже другая работа",
                            404, "not_found", {"approval_id": ident})
    # W26-D1: у недоговорной блокировки и жёсткого запрета нет состояния «ждёт решения»,
    # поэтому сначала честно объясняем, ПОЧЕМУ разрешить нельзя (422), и только потом —
    # «уже решено» (409): иначе человек видел бы «уже решено» там, где решения не было.
    if item.get("state") in UNDECIDED:
        if item.get("floor"):
            raise ApprovalError("Такую команду Hermes запрещает настрого — разрешить нельзя. "
                                "Действия: «Подсказать участнику» или «Понятно, скрыть».", 422,
                                "floor", {"approval_id": ident})
        if not item.get("grantable"):
            raise ApprovalError("Эту блокировку нельзя снять разрешением: Hermes не применяет "
                                "список разрешений к execute_code в работе без человека. "
                                "Действия: «Подсказать участнику обойтись терминалом», "
                                "«Доверять запуску кода участнику…» (со сроком) или "
                                "«Закрыть как неактуальное».", 422,
                                "not_grantable", {"approval_id": ident,
                                                  "actions": ["hint_agent", "trust_profile",
                                                              "dismiss"]})
    if item.get("state") != "pending":
        raise ApprovalError("Этот запрос разрешения уже решён", 409, "already_decided",
                            {"approval_id": ident, "state": item.get("state")})
    # W22-ST: решение по строке-группе применяется ко ВСЕМ её запросам сразу — либо все,
    # либо ни один (иначе человек «разрешил», а половина команд осталась закрытой).
    targets = [item]
    group_ids = [str(i) for i in (item.get("approval_ids") or []) if i]
    if len(group_ids) > 1:
        by_id = {str(row.get("id")): row for row in flat}
        missing = [i for i in group_ids if i not in by_id]
        if missing:
            raise ApprovalError("Часть запросов этой группы уже не актуальна — обновите "
                                "страницу и решите снова", 409, "already_decided",
                                {"approval_id": ident, "missing": missing[:5]})
        targets = [by_id[i] for i in group_ids]
        for row in targets:
            if row.get("state") != "pending":
                raise ApprovalError("Часть запросов этой группы уже решена — обновите "
                                    "страницу", 409, "already_decided",
                                    {"approval_id": str(row.get("id") or ""),
                                     "state": row.get("state")})
            if row.get("floor"):
                raise ApprovalError("В группе есть команда, которую Hermes запрещает настрого "
                                    "— разрешить нельзя.", 422, "floor",
                                    {"approval_id": str(row.get("id") or "")})
            if not row.get("grantable"):
                raise ApprovalError("В группе есть блокировка execute_code: её нельзя снять "
                                    "разрешением — попросите участника сделать то же через "
                                    "терминал.", 422, "not_grantable",
                                    {"approval_id": str(row.get("id") or "")})
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    profile = str(item.get("profile") or "")
    pattern_key = str(item.get("pattern_key") or "")
    if not room_id:
        raise ApprovalError("Непонятно, в какой комнате выдавать разрешение", 400, "bad_request")
    reg = _registry(cfg, room_id)
    grant = None
    allowlist: list[str] = []
    if decision != "deny":
        home = _ensure_home(cfg, profile)
        before = _read_allowlist(cfg, profile)
        previous = grant_of(reg, profile, pattern_key) or {}
        # Замок на дом профиля: две одновременные выдачи («разрешать всегда» для двух команд
        # одного участника) иначе читают список, дополняют и пишут — и одна запись теряется.
        with _worker_lock(home):
            result = run_worker(cfg, {"op": "add", "home": str(home),
                                      "pattern_key": pattern_key})
        allowlist = [str(v) for v in (result.get("allowlist") or [])]
        # «Запись была в конфиге до нас» — по реестру: если разрешение уже выдавалось
        # приложением, оно наше; иначе смотрим, стоял ли ключ в списке ДО записи.
        preexisting = (bool(previous.get("preexisting")) if previous
                       else pattern_key in before)
        grant = {"profile": profile, "member_title": item.get("member_title") or "",
                 "pattern_key": pattern_key, "description": item.get("description") or "",
                 "scope": "always" if decision == "always" else "once",
                 "task_id": item.get("task_id") or "", "root_id": str(root_id or ""),
                 "approval_id": ident, "granted_at": now,
                 "expires_at": None if decision == "always" else now + ONCE_TTL,
                 "card_title": item.get("card_title") or "",
                 "preexisting": bool(preexisting)}
        reg["grants"] = [g for g in reg["grants"]
                         if _grant_key(str(g.get("profile") or ""),
                                       str(g.get("pattern_key") or "")) !=
                         _grant_key(profile, pattern_key)]
        reg["grants"].append(grant)
    state = DECISIONS[decision]
    records: list[dict] = []
    for row in targets:
        records.append({
            "id": str(row.get("id") or ident), "root_id": str(root_id or ""),
            "task_id": row.get("task_id") or item.get("task_id") or "",
            "profile": profile, "kind": row.get("kind") or item.get("kind") or "terminal",
            "pattern_key": pattern_key, "command": str(row.get("command") or "")[:MAX_COMMAND],
            "description": row.get("description") or item.get("description") or "",
            "floor": bool(row.get("floor")),
            "card_title": row.get("card_title") or item.get("card_title") or "",
            "member_title": row.get("member_title") or item.get("member_title") or "",
            "card_state": row.get("card_state") or item.get("card_state") or "",
            "stopped": bool(row.get("stopped")),
            "state": state, "at": int(row.get("at") or item.get("at") or now), "decided_at": now,
            "count": int(row.get("count") or 1),
            "expires_at": (grant or {}).get("expires_at"),
            "group": str(item.get("group") or ""),
            "decision_note": _one_line(note, 300) if note else ""})
    record = records[0]
    for rec in records:
        reg["approvals"][rec["id"]] = rec
    _save_registry(cfg, room_id, reg)
    _journal(cfg, room_id, "%s: %s для %s в карточке «%s» (%s%s)" % (
        "разрешено" if decision != "deny" else "запрещено",
        record["description"] or pattern_key or GENERIC_DESCRIPTION,
        item.get("member_title") or profile, record["card_title"] or record["task_id"],
        "навсегда" if decision == "always" else ("один раз" if decision == "once" else "запрет"),
        ", команд: %d" % len(records) if len(records) > 1 else ""))
    restarted = False
    _journal_level(cfg, room, record, decision)
    if act is not None:
        try:
            act("comment", text=_comment_text(item, decision))
        except Exception as exc:            # noqa: BLE001 - комментарий вторичен
            note = (note or "") + (" " if note else "") + "комментарий не записался: %s" % str(exc)[:120]
        if decision != "deny":
            restarted = _restart(act, record.get("task_id") or "", record.get("card_state") or "")
    message = _message(item, decision, restarted)
    payload = {"ok": True, "approval": _public(record, record, now),
               "restarted": bool(restarted), "message": message}
    if len(records) > 1:
        payload["applied"] = [rec["id"] for rec in records]
        payload["approval"] = dict(payload["approval"], group=str(item.get("group") or ""),
                                   approval_ids=[rec["id"] for rec in records])
    if grant is not None:
        payload["grant"] = {"profile": grant["profile"], "pattern_key": grant["pattern_key"],
                            "description": grant["description"], "scope": grant["scope"],
                            "expires_at": grant["expires_at"]}
    if decision != "deny" and allowlist:
        payload["allowlist_size"] = len(allowlist)
    return payload


def _read_allowlist_before(cfg, profile: str, after: list[str]) -> list[str]:
    """Оставлено для совместимости вызовов: список «до записи» читается явно."""
    return []


def _restart(act, task_id: str, card_state: str) -> bool:
    """Вернуть остановленную карточку в очередь штатным действием движка (``retry``)."""
    if not task_id:
        return False
    if card_state and card_state not in RESTARTABLE:
        return False
    try:
        act("retry", task_id=task_id)
        return True
    except Exception:                       # noqa: BLE001 - перезапуск не обязан удаться
        return False


def _message(item: dict, decision: str, restarted: bool) -> str:
    description = item.get("description") or GENERIC_DESCRIPTION
    if decision == "deny":
        return "Запрещено: %s. Участник получил в карточке просьбу найти другой способ." % description
    if restarted:
        return ("Разрешено%s. Карточка перезапущена — работа продолжится."
                % (" навсегда" if decision == "always" else " один раз"))
    if str(item.get("card_state") or "") in FINISHED:
        return ("Разрешено%s и будет действовать. Карточка уже завершилась — попросите "
                "руководителя повторить работу (кнопка «Доработать»)."
                % (" навсегда" if decision == "always" else " один раз"))
    return ("Разрешено%s. Участник продолжит сам — разрешение подхватится при следующей "
            "попытке." % (" навсегда" if decision == "always" else " один раз"))


# ------------------------------------------- недоговорные действия (W26-D1, T3)

def parse_ttl(ttl=None, ttl_min=None) -> dict:
    """Срок доверия профилю — ОБЯЗАТЕЛЕН: ``ttl_min`` (минуты) либо ``ttl``.

    Доверие снимает защиту со ВСЕХ «опасных» команд профиля в конечных запусках
    (жёсткие запреты Hermes остаются), поэтому «навсегда по умолчанию» здесь нет:
    без ясно названного срока запись в профиль не делается вовсе.
    """
    minutes = None
    if ttl_min not in (None, ""):
        try:
            minutes = int(ttl_min)
        except (TypeError, ValueError):
            raise ApprovalError("Срок доверия должен быть числом минут", 400, "bad_request",
                                {"field": "ttl_min", "value": str(ttl_min)[:20]}) from None
    value = str(ttl or "").strip().lower()
    if minutes is None and value.isdigit():
        minutes = int(value)
    if minutes is not None:
        if not 1 <= minutes <= TRUST_MAX_MIN:
            raise ApprovalError("Срок доверия — от 1 до %d минут" % TRUST_MAX_MIN, 400,
                                "bad_request", {"field": "ttl_min", "value": minutes})
        return {"scope": "minutes", "ttl_min": minutes}
    if value in ("forever", "always", "навсегда"):
        return {"scope": "forever", "ttl_min": None}
    if value in ("card", "until_card", "card_end", "до конца карточки"):
        return {"scope": "card", "ttl_min": None}
    raise ApprovalError("Нужен срок доверия: ttl_min (минуты) либо ttl: card или forever",
                        400, "ttl_required", {"field": "ttl"})


def _hint_text(item: dict) -> str:
    """Текст подсказки участнику (способ A): без техники, с конкретным «как сделать»."""
    if str(item.get("kind") or "") == "execute_code":
        body = HINT_CODE
    elif item.get("floor"):
        body = ("Hermes запрещает настрого: %s. Этот путь не годится — найди другой способ "
                "и продолжи работу." % (item.get("description") or GENERIC_DESCRIPTION))
    else:
        body = ("эту блокировку нельзя снять разрешением. Сделай то же другим способом: "
                "запиши скрипт файлом и запусти через терминал (`python файл.py`).")
    return "[Михаил · подсказка] " + body


def _record_for(row: dict, item: dict, state: str, now: int, **extra) -> dict:
    """Запись решения в реестр комнаты (одна форма для всех действий)."""
    record = {
        "id": str(row.get("id") or item.get("id") or ""), "root_id": str(item.get("root_id") or ""),
        "task_id": row.get("task_id") or item.get("task_id") or "",
        "profile": str(item.get("profile") or ""),
        "kind": row.get("kind") or item.get("kind") or "terminal",
        "pattern_key": item.get("pattern_key"), "command": str(row.get("command") or "")[:MAX_COMMAND],
        "description": row.get("description") or item.get("description") or "",
        "floor": bool(row.get("floor") or item.get("floor")),
        "card_title": row.get("card_title") or item.get("card_title") or "",
        "member_title": row.get("member_title") or item.get("member_title") or "",
        "card_state": row.get("card_state") or item.get("card_state") or "",
        "stopped": bool(row.get("stopped") or item.get("stopped")),
        "state": state, "at": int(row.get("at") or item.get("at") or now), "decided_at": now,
        "count": int(row.get("count") or 1),
    }
    record.update({k: v for k, v in extra.items() if v is not None})
    return record


def act_block(cfg, room, root_id: str, approval_unique: str, action: str, note: str = "",
              extra=None, act=None, now: int | None = None, ttl=None, ttl_min=None) -> dict:
    """Недоговорные действия: ``hint_agent`` · ``trust_profile`` · ``dismiss``.

    * **hint_agent** — кладёт в ветку карточки (штатной функцией записи комментария,
      в песочнице — в поддельную доску) текст «execute_code недоступен в фоновой
      работе: сделай через файл и терминал». Ноль риска: разрешений не выдаёт.
    * **trust_profile** — пишет ``approvals.single_query_mode: approve`` ТОЛЬКО в
      профиль участника и только по этому действию, с обязательным сроком; запись
      реестра позволяет отозвать её (``revoke_trust``/``sweep``).
    * **dismiss** — закрывает пункт как неактуальный (тупика не остаётся).
    """
    now = _now() if now is None else int(now)
    action = str(action or "").strip().lower()
    if action not in EXTRA_DECISIONS:
        raise ApprovalError("Непонятное действие", 400, "bad_request",
                            {"field": "action", "value": action[:20]})
    ident = str(approval_unique or "").strip()
    if not ident:
        raise ApprovalError("Не указан запрос разрешения", 400, "bad_request",
                            {"field": "approval_id"})
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    if not room_id:
        raise ApprovalError("Непонятно, в какой комнате действие", 400, "bad_request")
    items = collect(cfg, room, root_id, extra, now=now)
    flat = collect(cfg, room, root_id, extra, now=now, collapse=False)
    item = next((row for row in items if row.get("id") == ident), None)
    if item is None:
        raise ApprovalError("Запрос разрешения не найден — возможно, это уже другая работа",
                            404, "not_found", {"approval_id": ident})
    if item.get("state") not in UNDECIDED:
        raise ApprovalError("Этот пункт уже закрыт", 409, "already_decided",
                            {"approval_id": ident, "state": item.get("state")})
    mode = str(item.get("mode") or "grant")
    profile = str(item.get("profile") or "")
    targets = [item]
    group_ids = [str(i) for i in (item.get("approval_ids") or []) if i]
    if len(group_ids) > 1:
        by_id = {str(row.get("id")): row for row in flat}
        targets = [by_id[i] for i in group_ids if i in by_id] or [item]
    trust = None
    if action == "trust_profile":
        if mode == "floor":
            raise ApprovalError("Доверие не снимает жёсткий запрет Hermes — тут поможет только "
                                "другой способ работы.", 422, "floor", {"approval_id": ident})
        if mode == "grant":
            raise ApprovalError("Эту блокировку можно разрешить обычным способом — доверие "
                                "профилю тут не нужно.", 422, "grantable",
                                {"approval_id": ident, "actions": ["once", "always", "deny"]})
        plan = parse_ttl(ttl, ttl_min)
        name = _profile_ok(profile)
        members = room_profiles(room)
        if members and name not in members:
            raise ApprovalError("Этот профиль не участвует в комнате", 400, "bad_request",
                                {"field": "profile", "value": name})
        home = _ensure_home(cfg, name)
        result = run_worker(cfg, {"op": "trust", "home": str(home), "ttl": plan["scope"]})
        if not result.get("ok"):
            raise ApprovalError(result.get("message") or "Не удалось записать доверие в профиль",
                                422, str(result.get("code") or "failed"), {"profile": name})
        expires_at = (None if plan["scope"] in ("forever", "card")
                      else now + int(plan["ttl_min"]) * 60)
        trust = {"profile": name, "member_title": item.get("member_title") or name,
                 "scope": plan["scope"], "ttl_min": plan["ttl_min"], "expires_at": expires_at,
                 "card_id": item.get("task_id") or "", "root_id": str(root_id or ""),
                 "granted_at": now, "approval_id": ident,
                 "previous": result.get("previous") or "",
                 "value": "approve", "card_title": item.get("card_title") or ""}
    state = EXTRA_DECISIONS[action]
    records = [_record_for(row, item, state, now,
                           root_id=str(root_id or ""), group=str(item.get("group") or ""),
                           decision_note=_one_line(note, 300) if note else "",
                           ttl_min=(trust or {}).get("ttl_min"),
                           trust_scope=(trust or {}).get("scope"),
                           expires_at=(trust or {}).get("expires_at")) for row in targets]
    commented = False
    if action == "hint_agent":
        if act is None:
            raise ApprovalError("Не с чем передать подсказку участнику: нет канала записи в "
                                "карточку", 422, "no_channel", {"approval_id": ident})
        try:
            act("comment", text=_hint_text(item))
            commented = True
        except Exception as exc:               # noqa: BLE001 - без подсказки не закрываем
            raise ApprovalError("Не удалось записать подсказку в карточку: %s"
                                % str(exc)[:160], 422, "comment_failed",
                                {"approval_id": ident}) from exc
    with _registry_lock(cfg, room_id):         # чтение → изменение → запись под замком
        reg = _registry(cfg, room_id)
        for record in records:
            reg["approvals"][record["id"]] = record
        if trust is not None:
            reg["trusts"] = [t for t in reg["trusts"]
                             if str(t.get("profile") or "") != trust["profile"]]
            reg["trusts"].append(trust)
        _save_registry(cfg, room_id, reg)
    what = {"hint_agent": "подсказка участнику", "trust_profile": "доверие профилю",
            "dismiss": "закрыто как неактуальное"}[action]
    _journal(cfg, room_id, "%s для %s в карточке «%s» (%s)" % (
        what, item.get("member_title") or profile or "участник",
        item.get("card_title") or item.get("task_id") or "", item.get("description") or ""))
    payload = {"ok": True, "action": "approval", "decision": action,
               "approval": _public(dict(item, **_record_fields(records[0])), records[0], now),
               "message": _extra_message(item, action, commented, trust),
               "decided_at": now}
    if len(records) > 1:
        payload["applied"] = [rec["id"] for rec in records]
    if trust is not None:
        payload["trust"] = {"profile": trust["profile"], "scope": trust["scope"],
                            "ttl_min": trust["ttl_min"], "expires_at": trust["expires_at"],
                            "previous": trust["previous"],
                            "label": _ttl_label(trust["scope"], trust["ttl_min"])}
    return payload


def restore(cfg, room, root_id: str, approval_unique: str, extra=None,
            now: int | None = None) -> dict:
    """Вернуть запрос разрешения, закрытый как неактуальное (W27-E1, контракт §1).

    Снятие (`dismiss`) пишет в реестр комнаты строку «закрыто». Возврат —
    обратное действие: строка убирается, и блокировка снова читается как
    `pending`, если она ещё есть в сессиях участников. Карточка не
    перезапускается, разрешение не выдаётся — меняется только учёт пункта.

    Ошибки: 404 — запроса нет; 409 `already_decided` — пункт не был снят
    (клиенту это не ошибка: он тихо перерисовывает состояние).
    """
    now = _now() if now is None else int(now)
    ident = str(approval_unique or "").strip()
    if not ident:
        raise ApprovalError("Не указан запрос разрешения", 400, "bad_request",
                            {"field": "approval_id"})
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    if not room_id:
        raise ApprovalError("Непонятно, в какой комнате действие", 400, "bad_request")
    items = collect(cfg, room, root_id, extra, now=now)
    item = next((row for row in items if row.get("id") == ident), None)
    if item is None:
        raise ApprovalError("Запрос разрешения не найден — возможно, это уже другая работа",
                            404, "not_found", {"approval_id": ident})
    if item.get("state") != "dismissed":
        raise ApprovalError("Этот пункт не закрывали как неактуальное", 409,
                            "already_decided", {"approval_id": ident,
                                                "state": item.get("state")})
    keys = [ident] + [str(i) for i in (item.get("approval_ids") or []) if i and str(i) != ident]
    removed: list = []
    with _registry_lock(cfg, room_id):
        reg = _registry(cfg, room_id)
        for key in keys:
            if reg["approvals"].pop(key, None) is not None:
                removed.append(key)
        _save_registry(cfg, room_id, reg)
    if not removed:                        # строки не было — вернуть нечего, честно говорим
        raise ApprovalError("Снятия в реестре нет — пункт уже открыт заново", 409,
                            "already_decided", {"approval_id": ident, "state": "pending"})
    _journal(cfg, room_id, "пункт снова актуален для %s в карточке «%s» (%s)" % (
        item.get("member_title") or item.get("profile") or "участник",
        item.get("card_title") or item.get("task_id") or "", item.get("description") or ""))
    try:
        fresh = collect(cfg, room, root_id, extra, now=now)
    except Exception:                      # noqa: BLE001 - возврат уже сделан
        fresh = []
    back = next((row for row in fresh if row.get("id") == ident), None)
    payload = {"ok": True, "action": "approval", "decision": "restore",
               "approval_id": ident, "restored": removed, "decided_at": now,
               "message": "Пункт снова ждёт вашего решения."}
    if back is not None:
        payload["approval"] = back
        payload["state"] = back.get("state")
    return payload


def _record_fields(record: dict) -> dict:
    """Поля записи, которые нужны ``_public`` (состояние, сроки, причина закрытия)."""
    return {key: record.get(key) for key in ("state", "decided_at", "expires_at",
                                             "card_state", "closed_reason", "closed_at",
                                             "ttl_min", "trust_scope")
            if record.get(key) is not None}


def _ttl_label(scope: str, minutes) -> str:
    if scope == "minutes":
        return "%d мин." % int(minutes or 0)
    return "до конца карточки" if scope == "card" else "навсегда"


def _extra_message(item: dict, action: str, commented: bool, trust) -> str:
    """Что человеку сказать после недоговорного действия — по-русски и честно."""
    card = item.get("card_title") or ""
    if action == "dismiss":
        return "Закрыто как неактуальное: этот пункт больше не ждёт вашего решения."
    if action == "trust_profile":
        label = _ttl_label((trust or {}).get("scope") or "", (trust or {}).get("ttl_min"))
        return ("Профилю «%s» доверен запуск кода (%s). Запись сделана только в его настройки; "
                "отозвать можно здесь же кнопкой «Отозвать доверие»."
                % ((trust or {}).get("profile") or "участник", label))
    where = "в карточку «%s»" % card if card else "в ветку карточки"
    return ("Подсказка участнику записана %s: обойтись файлом и терминалом.%s"
            % (where, "" if commented else " (комментарий не записался)"))


def auto_hint(cfg, room, root_id: str, act, now: int | None = None,
              delay: int = 0) -> dict:
    """Способ A сам, без человека: подсказать участнику обойтись терминалом.

    Зовётся сторожем/интерфейсом (в этой волне — маршрут ``POST /api/approvals/tidy``):
    ``delay`` — сколько секунд блокировка должна провисеть, прежде чем писать
    (по умолчанию сразу; в интерфейсе — 2 минуты, ``AUTO_HINT_AFTER``).
    """
    now = _now() if now is None else int(now)
    sent: list[str] = []
    failed = 0
    for item in collect(cfg, room, root_id, now=now):
        if item.get("state") != "workaround" or item.get("mode") != "workaround":
            continue
        if delay and now - int(item.get("at") or now) < int(delay):
            continue
        try:
            act_block(cfg, room, root_id, item.get("id"), "hint_agent", act=act, now=now)
        except ApprovalError:
            failed += 1
            continue
        sent.append(str(item.get("id") or ""))
    return {"ok": True, "hinted": len(sent), "ids": sent, "failed": failed}


def trusts(cfg, room=None) -> dict:
    """Действующие доверия профилям комнаты (для панели и отзыва)."""
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    now = _now()
    items: list[dict] = []
    if not room_id:
        return {"items": [], "room": None, "at": now}
    for trust in _registry(cfg, room_id)["trusts"]:
        expires = int(trust.get("expires_at") or 0)
        if expires and expires <= now:
            continue                                # истекшее не показываем
        items.append({"profile": str(trust.get("profile") or ""),
                      "member_title": trust.get("member_title") or _label(room, trust.get("profile")),
                      "scope": str(trust.get("scope") or "minutes"),
                      "ttl_min": trust.get("ttl_min"), "expires_at": trust.get("expires_at"),
                      "granted_at": int(trust.get("granted_at") or 0),
                      "card_title": trust.get("card_title") or "",
                      "label": _ttl_label(str(trust.get("scope") or ""), trust.get("ttl_min")),
                      "room": room_id})
    return {"items": items, "room": room_id, "at": now}


def revoke_trust(cfg, profile: str, room=None, item_id: str = "") -> dict:
    """Отозвать доверие: вернуть прежнее ``single_query_mode`` и снять запись реестра."""
    name = _profile_ok(profile)
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    if not room_id:
        raise ApprovalError("Непонятно, в какой комнате отзывать доверие", 400, "bad_request")
    reg = _registry(cfg, room_id)
    found = [t for t in reg["trusts"] if str(t.get("profile") or "") == name
             and (not item_id or str(t.get("approval_id") or "") == str(item_id))]
    if not found:
        raise ApprovalError("Такого доверия нет — возможно, оно уже отозвано", 404,
                            "not_found", {"profile": name})
    previous = str((found[-1] or {}).get("previous") or "")
    home = _ensure_home(cfg, name)
    result = run_worker(cfg, {"op": "untrust", "home": str(home), "previous": previous})
    if not result.get("ok"):
        raise ApprovalError(result.get("message") or "Не удалось вернуть настройку профиля",
                            422, str(result.get("code") or "failed"), {"profile": name})
    with _registry_lock(cfg, room_id):
        fresh = _registry(cfg, room_id)
        fresh["trusts"] = [t for t in fresh["trusts"]
                           if str(t.get("profile") or "") != name
                           or (item_id and str(t.get("approval_id") or "") != str(item_id))]
        for record in fresh["approvals"].values():
            if not isinstance(record, dict) or str(record.get("profile") or "") != name:
                continue
            if str(record.get("state") or "") == "trusted":
                record["state"] = "dismissed"
                record["decided_at"] = _now()
                record["decision_note"] = "доверие отозвано"
        _save_registry(cfg, room_id, fresh)
    _journal(cfg, room_id, "отозвано доверие запуску кода: %s" % (name,))
    return {"ok": True, "revoked": {"profile": name, "previous": previous},
            "message": "Доверие отозвано: профиль «%s» снова спрашивает разрешения." % name,
            "items": trusts(cfg, room)["items"]}



# ---------------------------------------------------------------- список и отзыв

def _registry_lock(cfg, room_id):
    """Замок реестра комнаты: чтение → изменение → запись под ним (иначе правки теряются)."""
    return sf.lock_for(registry_path(cfg, room_id))


def _read_allowlist(cfg, profile: str) -> list[str]:
    """``command_allowlist`` профиля (только чтение, ``yaml.safe_load``)."""
    path = config_path(cfg, profile)
    if not path.is_file():
        return []
    try:
        import yaml                                        # noqa: PLC0415 - есть не всегда
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:                                       # noqa: BLE001 - конфиг мог быть пуст
        return _raw_allowlist(path)
    raw = data.get("command_allowlist") if isinstance(data, dict) else None
    if isinstance(raw, str):
        try:
            import yaml                                    # noqa: PLC0415
            raw = yaml.safe_load(raw)
        except Exception:                                  # noqa: BLE001
            raw = []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


def _raw_allowlist(path: Path) -> list[str]:
    """Запасной разбор без PyYAML: строки списка ``command_allowlist`` из файла."""
    out: list[str] = []
    inside = False
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^command_allowlist\s*:", line):
                inside = True
                continue
            if inside:
                match = re.match(r"^\s*-\s+(.+?)\s*$", line)
                if match:
                    out.append(match.group(1).strip().strip('"').strip("'"))
                elif line.strip() and not line.startswith((" ", "\t")):
                    inside = False
    except OSError:
        return []
    return out


def room_profiles(room) -> list[str]:
    """Профили участников комнаты (руководитель первым, без повторов)."""
    room = room if isinstance(room, dict) else {}
    out: list[str] = []
    for value in [room.get("orchestrator")] + [ (m or {}).get("profile") for m in room.get("members") or [] ]:
        name = str(value or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def grants(cfg, room=None, rooms=None) -> dict:
    """Постоянные и временные разрешения профилей комнаты (только чтение конфигов)."""
    targets: list[tuple[str, dict]] = []
    if isinstance(room, dict) and room.get("id"):
        targets.append((str(room.get("id")), room))
    else:
        for item in rooms or []:
            if isinstance(item, dict) and item.get("id"):
                targets.append((str(item.get("id")), item))
    items: list[dict] = []
    warnings: list[str] = []
    seen = set()
    for room_id, room_dict in targets:
        reg = _registry(cfg, room_id)
        now = _now()
        for grant in reg["grants"]:
            profile = str(grant.get("profile") or "")
            pattern_key = str(grant.get("pattern_key") or "")
            if not profile or not pattern_key or (profile, pattern_key) in seen:
                continue
            if grant.get("scope") == "once" and int(grant.get("expires_at") or 0) <= now:
                continue                    # истёкшее временное разрешение не показываем
            seen.add((profile, pattern_key))
            items.append({"profile": profile,
                          "member_title": grant.get("member_title") or _label(room_dict, profile),
                          "pattern_key": pattern_key,
                          "description": grant.get("description") or describe(pattern_key),
                          "scope": "always" if grant.get("scope") == "always" else "once",
                          "granted_at": int(grant.get("granted_at") or 0),
                          "expires_at": grant.get("expires_at") or None,
                          "source": "room", "room": room_id,
                          "card_title": grant.get("card_title") or ""})
        for profile in room_profiles(room_dict):
            allow = _read_allowlist(cfg, profile)
            for pattern_key in allow:
                if (profile, pattern_key) in seen:
                    continue
                seen.add((profile, pattern_key))
                items.append({"profile": profile,
                              "member_title": _label(room_dict, profile),
                              "pattern_key": pattern_key,
                              "description": describe(pattern_key),
                              "scope": "always", "granted_at": 0, "expires_at": None,
                              "source": "manual", "room": room_id, "card_title": ""})
    items.sort(key=lambda row: (row["profile"], row["description"]))
    return {"items": items, "room": (targets[0][0] if targets else None),
            "at": _now(), "warnings": warnings}


def revoke(cfg, profile: str, pattern_key: str, room=None) -> dict:
    """Убрать запись из ``command_allowlist`` профиля штатным путём Hermes.

    Профиль приводится к участникам комнаты, если комната указана: отозвать разрешение
    у чужого профиля (или у ``default`` — личного агента пользователя) по телу запроса
    нельзя. Имя профиля проверяется ``_profile_ok`` — никаких путей и ``..``.
    """
    name = _profile_ok(profile)
    if isinstance(room, dict) and room.get("id"):
        members = room_profiles(room)
        if members and name not in members:
            raise ApprovalError("Этот профиль не участвует в комнате — отзывать его "
                                "разрешения здесь нельзя", 400, "bad_request",
                                {"field": "profile", "value": name})
    key = str(pattern_key or "").strip()
    if not key:
        raise ApprovalError("Не указано, какое разрешение отозвать", 400, "bad_request",
                            {"field": "pattern_key"})
    home = profile_home(cfg, name)
    if not home.is_dir():
        raise ApprovalError("Профиль не найден на этом компьютере", 404, "not_found",
                            {"profile": name})
    result = run_worker(cfg, {"op": "remove", "home": str(home), "pattern_key": key})
    changed = bool(result.get("changed"))
    rooms = [room] if isinstance(room, dict) and room.get("id") else []
    for room_dict in rooms:
        room_id = room_dict.get("id")
        with _registry_lock(cfg, room_id):          # чтение → изменение → запись под замком
            reg = _registry(cfg, room_id)
            keep = [g for g in reg["grants"]
                    if _grant_key(str(g.get("profile") or ""), str(g.get("pattern_key") or ""))
                    != _grant_key(name, key)]
            if len(keep) != len(reg["grants"]):
                reg["grants"] = keep
                _save_registry(cfg, room_id, reg)
        _journal(cfg, room_id, "отозвано: %s для %s" % (describe(key), _label(room_dict, name)))
    if not changed and result.get("absent"):
        raise ApprovalError("Такого разрешения у профиля уже нет", 404, "not_found",
                            {"profile": name, "pattern_key": key})
    return {"ok": True, "revoked": {"profile": name, "pattern_key": key,
                                    "description": describe(key)},
            "message": "Разрешение отозвано: %s больше не выполняется без вопроса." % describe(key)}


# ---------------------------------------------------------------- сторож

def _sweep_trusts(cfg, room, now: int) -> tuple:
    """Снять доверие профилю, у которого вышел срок или закончилась карточка (W26-D1).

    Возвращает ``(сколько отозвано, сколько ошибок)``. Ошибка подпроцесса не валит тик:
    запись остаётся в реестре и снимается на следующем проходе.
    """
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    reg = _registry(cfg, room_id)
    if not reg["trusts"]:
        return 0, 0
    board = str(room.get("board") or getattr(cfg, "board", "") or "default")
    task_ids = sorted({str(t.get("card_id") or "") for t in reg["trusts"] if t.get("card_id")})
    cards = wl._card_rows(cfg, board, task_ids) if task_ids else {}
    drop: list[dict] = []
    revoked = 0
    failures = 0
    for trust in reg["trusts"]:
        expires = int(trust.get("expires_at") or 0)
        card_state = str((cards.get(trust.get("card_id") or "") or {}).get("status") or "")
        expired = bool(expires) and expires <= now
        finished = (str(trust.get("scope") or "") == "card" and card_state in CLOSED_CARDS)
        if not (expired or finished):
            continue
        try:
            run_worker(cfg, {"op": "untrust",
                             "home": str(_ensure_home(cfg, trust.get("profile"))),
                             "previous": str(trust.get("previous") or "")})
        except ApprovalError:
            failures += 1
            continue
        drop.append(trust)
        revoked += 1
        _journal(cfg, room_id, "снято доверие запуску кода у %s (%s)" % (
            trust.get("profile") or "участник",
            "карточка завершилась" if finished else "срок вышел"))
    if not drop:
        return 0, failures
    gone = {(str(t.get("profile") or ""), int(t.get("granted_at") or 0)) for t in drop}
    with _registry_lock(cfg, room_id):
        fresh = _registry(cfg, room_id)
        fresh["trusts"] = [t for t in fresh["trusts"]
                           if (str(t.get("profile") or ""),
                               int(t.get("granted_at") or 0)) not in gone]
        for record in fresh["approvals"].values():
            if not isinstance(record, dict) or str(record.get("state") or "") != "trusted":
                continue
            if any(str(record.get("profile") or "") == profile for profile, _ in gone):
                record["state"] = "dismissed"
                record["decision_note"] = "срок доверия вышел"
        _save_registry(cfg, room_id, fresh)
    return revoked, failures


def sweep(cfg, room, records=None, now: int | None = None, act=None) -> dict:
    """Снять истёкшие и «карточка завершилась» временные разрешения.

    Зовётся из ``watcher.py`` на каждом тике. Убираем запись из конфига ТОЛЬКО если её
    поставило приложение (``preexisting: false``) и на неё больше никто не опирается:
    другое живое разрешение того же профиля с тем же правилом или постоянное.

    W26-D1: тот же проход снимает истёкшее/карточное **доверие** профилю
    (``approvals.single_query_mode``). Подсказка участнику (способ A) — отдельно,
    ``auto_hint`` (нужен канал записи в карточку).
    """
    now = _now() if now is None else int(now)
    room = room if isinstance(room, dict) else {}
    room_id = room.get("id")
    result = {"revoked": 0, "kept": 0, "errors": 0, "closed": 0,
              "trusts_revoked": 0, "hinted": 0}
    if not room_id:
        return result
    revoked, failures = _sweep_trusts(cfg, room, now)
    result["trusts_revoked"] = revoked
    result["errors"] += failures
    reg = _registry(cfg, room_id)
    if not reg["grants"]:
        return result
    board = str(room.get("board") or getattr(cfg, "board", "") or "default")
    task_ids = sorted({str(g.get("task_id") or "") for g in reg["grants"] if g.get("task_id")})
    cards = wl._card_rows(cfg, board, task_ids) if task_ids else {}
    changed = False
    keep: list[dict] = []
    states: dict = {}                            # что записать в реестр решений (под замком)
    revoke_keys: list = []                       # какие записи действительно снимаем
    for grant in reg["grants"]:
        if grant.get("scope") != "once":
            keep.append(grant)
            continue
        card_state = str((cards.get(grant.get("task_id") or "") or {}).get("status") or "")
        expired = int(grant.get("expires_at") or 0) <= now
        finished = card_state in FINISHED
        if not expired and not finished:
            keep.append(grant)
            continue
        profile = str(grant.get("profile") or "")
        pattern_key = str(grant.get("pattern_key") or "")
        busy = any(other is not grant
                   and _grant_key(str(other.get("profile") or ""),
                                  str(other.get("pattern_key") or "")) == _grant_key(profile, pattern_key)
                   and (other.get("scope") == "always"
                        or int(other.get("expires_at") or 0) > now)
                   for other in reg["grants"])
        changed = True
        if busy:
            result["kept"] += 1
            continue                            # запись нужна другому разрешению
        if grant.get("preexisting"):
            result["kept"] += 1
            # запись стояла в настройках профиля ДО нас — её не снимаем; для участника
            # разрешение продолжает действовать, поэтому «истекло» не показываем
            record = reg["approvals"].get(str(grant.get("approval_id") or ""))
            if isinstance(record, dict) and record.get("state") == "granted_once":
                states[str(grant.get("approval_id") or "")] = {"state": "granted_always",
                                                                 "kept_reason": "manual_entry"}
            _journal(cfg, room_id, "оставлено разрешение %s: запись была в настройках профиля "
                                   "до нас" % (grant.get("description") or describe(pattern_key)))
            continue
        try:
            run_worker(cfg, {"op": "remove", "home": str(profile_home(cfg, profile)),
                             "pattern_key": pattern_key})
            result["revoked"] += 1
            revoke_keys.append(_grant_key(profile, pattern_key))
            # запрос разрешения больше не «решён»: разрешение снято — показываем «истекло»
            record = reg["approvals"].get(str(grant.get("approval_id") or ""))
            if isinstance(record, dict) and record.get("state") == "granted_once":
                states[str(grant.get("approval_id") or "")] = {
                    "state": "expired", "expired_at": now,
                    "expired_reason": ("card_finished" if finished else "ttl")}
            _journal(cfg, room_id, "снято временное разрешение: %s (%s)"
                     % (grant.get("description") or describe(pattern_key),
                        "карточка завершилась" if finished else "прошло 30 минут"))
        except ApprovalError:
            result["errors"] += 1
            keep.append(grant)                  # не вышло — попробуем на следующем тике
            changed = True
    if changed:
        # пишем под замком и по СНИМКУ-СПИСКУ снятых: разрешение, выданное в это время
        # соседним решением, не теряется; решения обновляем по id, а не перезаписываем файл
        with _registry_lock(cfg, room_id):
            fresh = _registry(cfg, room_id)
            fresh["grants"] = [g for g in fresh["grants"]
                               if _grant_key(str(g.get("profile") or ""),
                                             str(g.get("pattern_key") or "")) not in revoke_keys]
            for ident, patch in states.items():
                row = fresh["approvals"].get(ident)
                if isinstance(row, dict):
                    row.update(patch)
            _save_registry(cfg, room_id, fresh)
    return result


__all__ = ["MAX_COMMAND", "ONCE_TTL", "STATES", "DECISIONS", "KINDS", "PATTERN_RU",
           "MODES", "MODE_LABEL", "ACTION_LABEL", "TTL_OPTIONS", "EXTRA_DECISIONS",
           "HINT_CODE", "ApprovalError", "approval_id", "collect", "decide", "grants",
           "revoke", "sweep", "find", "pending", "actionable", "needs_you", "describe",
           "find_python", "python_candidates", "profile_home", "config_path", "hermes_root",
           "hermes_tree", "room_profiles", "registry_path", "run_worker", "mode_of",
           "actions_for", "act_block", "auto_hint", "parse_ttl", "trusts", "revoke_trust",
           "restore", "reset_python_cache", "grant_of"]
