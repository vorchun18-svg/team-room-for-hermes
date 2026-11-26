"""Уведомления Windows о событиях комнат (W29B-Q6).

Зачем
-----
Человеку не нужно сидеть в приложении: «ждёт ваш ответ», «запрос завершён»,
«ошибка запуска», «дежурство нашло изменения» приходят системным уведомлением,
а клик открывает нужный запрос в браузере.

Что важно по заданию
--------------------
* **По умолчанию ВЫКЛЮЧЕНО.** Пока человек явно не включил уведомления в
  Настройках (кнопка «Включить»), ничего не показывается; первый проход только
  запоминает состояние, чтобы после включения не прилетела пачка старых событий.
* **Тихие часы**: интервал ``ЧЧ:ММ`` (по местному времени), в том числе через
  полночь. Пока тихо — события не теряются, а ждут в «отложенных» (до 6 часов) и
  уходят после окончания тихих часов.
* **Не чаще одного раза в 5 минут на вид** (``min_interval_s``, не меньше 60 с).
* **Дедупликация**: одно и то же событие (вид + комната + карточка + подпись)
  показывается один раз в 7 дней.
* **Канал**: по умолчанию — значок в области уведомлений (``NotifyIcon``) в скрытом
  процессе PowerShell: без мигающей консоли, клик по уведомлению и двойной клик по
  значку открывают ссылку ``http://127.0.0.1:<порт>/#/…``. Текст уведомления идёт
  НЕ в командную строку, а в переменные окружения, скрипт постоянный и передаётся
  готовой base64-строкой (``-EncodedCommand``) — подставить в него код нельзя.
* **Песочница**: ``TEAMROOM_NOTIFY_FAKE=<путь>`` — уведомления пишутся строками
  JSONL в файл, PowerShell не запускается. В песочнице (``cfg.sandbox``) без
  этой переменной настоящие уведомления тоже не показываются.

Хранилище (в данных приложения, не в профилях Hermes)
-----------------------------------------------------
* ``<data>/state/notify.json`` — настройки (правит человек);
* ``<data>/state/notify-state.json`` — состояние: где мы видели запросы, что уже
  показано, отложенные, журнал последних уведомлений.

Чистая логика (``plan``) отделена от канала: тесты проверяют правила без
Windows, а живой канал — только через ``TEAMROOM_NOTIFY_FAKE``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:                                      # пакетный и «плоский» импорт
    from . import proc as proc_mod
    from . import statefiles as sf
except ImportError:                       # pragma: no cover
    import proc as proc_mod               # type: ignore
    import statefiles as sf               # type: ignore

IS_WINDOWS = os.name == "nt"

#: Виды уведомлений (по заданию пакета).
KINDS = ("waiting", "finished", "error", "duty")
KIND_TITLES = {
    "waiting": "Ждёт ваш ответ",
    "finished": "Задача завершена",
    "error": "Ошибка запуска",
    "duty": "Дежурство нашло изменения",
    "test": "Проверка уведомлений",
}
KIND_LABELS = {
    "waiting": "«Ждёт ваш ответ»: появились новые пункты",
    "finished": "«Задача завершена»",
    "error": "«Ошибка запуска»",
    "duty": "«Дежурство нашло изменения»",
}

DEFAULT_MIN_INTERVAL_S = 300          # не чаще 1 раза в 5 минут на вид
MIN_INTERVAL_FLOOR = 60
MAX_INTERVAL = 24 * 3600
SEEN_TTL = 7 * 86400                  # дедупликация — неделя
BACKLOG_TTL = 6 * 3600                # отложенные тихими часами ждут ≤ 6 часов
BACKLOG_MAX = 20
HISTORY_MAX = 50
ROOTS_MAX = 500
TEXT_MAX = 160

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")

#: Скрипт канала. ПОСТОЯННЫЙ: ни одного значения от человека внутри — только
#: чтение переменных окружения. Клик по уведомлению и по значку открывает ссылку.
NOTIFYICON_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$title = [string]$env:TEAMROOM_NOTIFY_TITLE
$body = [string]$env:TEAMROOM_NOTIFY_BODY
$url = [string]$env:TEAMROOM_NOTIFY_URL
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.Text = 'Комната команды'
$icon.Visible = $true
$open = {
  if ($url) { try { Start-Process -FilePath $url } catch { } }
}
$icon.add_BalloonTipClicked($open)
$icon.add_MouseDoubleClick($open)
$icon.BalloonTipTitle = $title
$icon.BalloonTipText = $body
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Information
$icon.ShowBalloonTip(10000)
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 200
}
$icon.Visible = $false
$icon.Dispose()
"""


# ----------------------------------------------------------------- настройки

def _state_dir(cfg) -> Path:
    root = getattr(cfg, "state_dir", None) or Path(str(getattr(cfg, "data_dir", "."))) / "state"
    return Path(str(root))


def settings_path(cfg) -> Path:
    return _state_dir(cfg) / "notify.json"


def state_path(cfg) -> Path:
    return _state_dir(cfg) / "notify-state.json"


def default_settings() -> dict:
    return {"enabled": False,
            "kinds": {kind: True for kind in KINDS},
            "quiet_hours": {"enabled": False, "from": "22:00", "to": "08:00"},
            "min_interval_s": DEFAULT_MIN_INTERVAL_S,
            "updated_at": 0}


def parse_hhmm(value) -> int | None:
    """``"22:30"`` → минут от полуночи; мусор → ``None``."""
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _as_bool(value, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    return fallback


def normalize_settings(raw, base: dict | None = None) -> tuple[dict, list[dict]]:
    """Проверить настройки и вернуть ``(настройки, замечания)``.

    Неизвестные поля отбрасываются, некорректные значения не проходят (остаётся
    прежнее) с понятной причиной — Настройки показывают её человеку.
    """
    out = json.loads(json.dumps(base or default_settings()))
    warnings: list[dict] = []
    data = raw if isinstance(raw, dict) else {}
    if "enabled" in data:
        out["enabled"] = _as_bool(data.get("enabled"), bool(out.get("enabled")))
    kinds = data.get("kinds")
    if isinstance(kinds, dict):
        for kind in KINDS:
            if kind in kinds:
                out["kinds"][kind] = _as_bool(kinds.get(kind), bool(out["kinds"].get(kind, True)))
        unknown = sorted(set(kinds) - set(KINDS))
        if unknown:
            warnings.append({"field": "kinds", "message": "Неизвестные виды пропущены: %s"
                             % ", ".join(unknown)})
    quiet = data.get("quiet_hours")
    if isinstance(quiet, dict):
        current = dict(out.get("quiet_hours") or {})
        if "enabled" in quiet:
            current["enabled"] = _as_bool(quiet.get("enabled"), bool(current.get("enabled")))
        for key, fallback in (("from", "22:00"), ("to", "08:00")):
            if key not in quiet:
                continue
            if parse_hhmm(quiet.get(key)) is None:
                warnings.append({"field": "quiet_hours." + key,
                                 "message": "Время нужно в виде ЧЧ:ММ (например 22:30)"})
                current.setdefault(key, fallback)
            else:
                current[key] = str(quiet.get(key)).strip()
        out["quiet_hours"] = {"enabled": bool(current.get("enabled")),
                              "from": current.get("from") or "22:00",
                              "to": current.get("to") or "08:00"}
    if "min_interval_s" in data:
        try:
            value = int(data.get("min_interval_s"))
        except (TypeError, ValueError):
            warnings.append({"field": "min_interval_s", "message": "Интервал задаётся числом секунд"})
        else:
            if value < MIN_INTERVAL_FLOOR:
                warnings.append({"field": "min_interval_s",
                                 "message": "Не чаще раза в минуту — поставили %d с"
                                            % MIN_INTERVAL_FLOOR})
            out["min_interval_s"] = max(MIN_INTERVAL_FLOOR, min(MAX_INTERVAL, value))
    return out, warnings


def load_settings(cfg) -> tuple[dict, dict]:
    """Настройки с диска; файла нет — значения по умолчанию (уведомления выключены)."""
    data, info = sf.read_json_doc(settings_path(cfg), None)
    settings, warnings = normalize_settings(data)
    return settings, {"info": info, "warnings": warnings,
                      "path": str(settings_path(cfg))}


def save_settings(cfg, patch) -> tuple[dict, list[dict]]:
    """Слить правку с текущими настройками, проверить и записать атомарно."""
    current, _ = load_settings(cfg)
    merged_input = dict(current)
    data = patch if isinstance(patch, dict) else {}
    for key in ("enabled", "min_interval_s"):
        if key in data:
            merged_input[key] = data[key]
    if isinstance(data.get("kinds"), dict):
        merged_input["kinds"] = {**current["kinds"], **{k: v for k, v in data["kinds"].items()
                                                        if k in KINDS}}
    if isinstance(data.get("quiet_hours"), dict):
        merged_input["quiet_hours"] = {**current.get("quiet_hours", {}), **data["quiet_hours"]}
    settings, warnings = normalize_settings(merged_input)
    settings["updated_at"] = int(time.time())
    sf.write_json_atomic(settings_path(cfg), settings)
    return settings, warnings


# --------------------------------------------------------------- тихие часы

def in_quiet_hours(settings, now=None) -> bool:
    """Сейчас тихо? Границы включительно-исключительно: с ``from`` до ``to``.

    ``from == to`` — «тихо весь день» (человек явно попросил молчания).
    """
    quiet = (settings or {}).get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return False
    start, end = parse_hhmm(quiet.get("from")), parse_hhmm(quiet.get("to"))
    if start is None or end is None:
        return False
    stamp = time.localtime(time.time() if now is None else int(now))
    current = stamp.tm_hour * 60 + stamp.tm_min
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


# ------------------------------------------------------------ тексты и ссылки

def _clean(value, limit: int = TEXT_MAX) -> str:
    text = _CTRL_RE.sub(" ", str(value or ""))
    text = " ".join(text.split())
    return text[:limit]


def _safe_id(value) -> str:
    return _SAFE_ID_RE.sub("", str(value or ""))[:64]


def _short_hash(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:10]


def request_url(port, room_id: str, root_id: str = "") -> str:
    """Ссылка на страницу запроса/комнаты для клика по уведомлению."""
    host = "http://127.0.0.1:%s" % int(port or 8777)
    room = _safe_id(room_id)
    root = _safe_id(root_id)
    if room and root:
        return "%s/#/room/%s/request/%s" % (host, room, root)
    if room:
        return "%s/#/room/%s" % (host, room)
    return "%s/#/today" % host


def _candidate(kind: str, room: str, room_name: str, root: str, title: str,
               body: str, url: str, key: str, now: int) -> dict:
    return {"kind": kind, "room": room, "room_name": room_name, "root": root,
            "title": _clean(title or KIND_TITLES.get(kind, "Комната команды"), 80),
            "body": _clean(body, TEXT_MAX), "url": url, "key": key, "at": int(now)}


def _is_done(status) -> bool:
    return str(status or "") in ("done", "archived", "cancelled")


# ----------------------------------------------------------------- состояние

def _norm_state(raw) -> dict:
    data = raw if isinstance(raw, dict) else {}
    out = {"roots": {}, "waiting": {}, "seen": {}, "sent": {},
           "backlog": [], "history": [], "updated_at": 0}
    for key in ("roots", "waiting", "seen", "sent"):
        block = data.get(key)
        if isinstance(block, dict):
            out[key] = {str(k): v for k, v in block.items() if isinstance(k, (dict, int, float, str))}
    if isinstance(data.get("backlog"), list):
        out["backlog"] = [item for item in data["backlog"] if isinstance(item, dict)][:BACKLOG_MAX]
    if isinstance(data.get("history"), list):
        out["history"] = [item for item in data["history"] if isinstance(item, dict)][:HISTORY_MAX]
    try:
        out["updated_at"] = int(data.get("updated_at") or 0)
    except (TypeError, ValueError):
        out["updated_at"] = 0
    return out


def load_state(cfg) -> dict:
    data, _info = sf.read_json_doc(state_path(cfg), None)
    return _norm_state(data)


def save_state(cfg, state: dict) -> None:
    state["updated_at"] = int(time.time())
    sf.write_json_atomic(state_path(cfg), state)


# ------------------------------------------------------- события из фактов

def _candidates(facts, state: dict, now: int, port=None) -> list[dict]:
    """Сравнить факты прохода сторожа с тем, что уже видели, и собрать события.

    Факты (их собирает сторож одним проходом):
    ``{"kind":"root", room, room_name, root, status, error, title, needs_you}``,
    ``{"kind":"waiting", room, room_name, count, roots[]}``,
    ``{"kind":"duty", room, room_name, job, run, title, count}``.
    """
    out: list[dict] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        # W30-D1: принятая задача заморожена — о ней не уведомляем вообще
        # (сторож её и не отдаёт; проверка страхует другие источники фактов).
        if fact.get("accepted"):
            continue
        kind = str(fact.get("kind") or "")
        room = _safe_id(fact.get("room"))
        room_name = _clean(fact.get("room_name"), 60) or room
        if not room:
            continue
        if kind == "root":
            root = _safe_id(fact.get("root"))
            if not root:
                continue
            key = room + ":" + root
            prev = state["roots"].get(key)
            prev = prev if isinstance(prev, dict) else {}
            status = str(fact.get("status") or "")
            error = _clean(fact.get("error"), 200)
            title = _clean(fact.get("title"), 120) or "Задача"
            first_seen = not prev
            if not first_seen:
                if _is_done(status) and not _is_done(prev.get("status")):
                    out.append(_candidate(
                        "finished", room, room_name, root, KIND_TITLES["finished"],
                        "%s · %s" % (room_name, title),
                        request_url(port, room, root),
                        "finished:%s:%s" % (room, root), now))
                if error and error != _clean(prev.get("error"), 200):
                    out.append(_candidate(
                        "error", room, room_name, root, KIND_TITLES["error"],
                        "%s · %s: %s" % (room_name, title, error),
                        request_url(port, room, root),
                        "error:%s:%s:%s" % (room, root, _short_hash(error)), now))
            state["roots"][key] = {"status": status, "error": error,
                                   "needs_you": bool(fact.get("needs_you")), "at": now}
        elif kind == "waiting":
            try:
                count = max(0, int(fact.get("count") or 0))
            except (TypeError, ValueError):
                count = 0
            roots = sorted({_safe_id(one) for one in (fact.get("roots") or []) if _safe_id(one)})
            signature = "%d|%s" % (count, ",".join(roots))
            previous = str(state["waiting"].get(room) or "")
            if count > 0 and signature != previous:
                single = roots[0] if len(roots) == 1 else ""
                out.append(_candidate(
                    "waiting", room, room_name, single,
                    "%s: %d" % (KIND_TITLES["waiting"], count),
                    "%s · %s" % (room_name, _clean(fact.get("title"), 120) or "новые пункты"),
                    request_url(port, room, single),
                    "waiting:%s:%s" % (room, _short_hash(signature)), now))
            state["waiting"][room] = signature if count > 0 else ""
        elif kind == "duty":
            job = _safe_id(fact.get("job")) or "duty"
            run = _safe_id(fact.get("run")) or str(now // 900)
            out.append(_candidate(
                "duty", room, room_name, "", KIND_TITLES["duty"],
                "%s · %s" % (room_name, _clean(fact.get("title"), 120) or "изменения"),
                request_url(port, room, ""),
                "duty:%s:%s:%s" % (room, job, run), now))
    return out


# -------------------------------------------------------------------- правила

def plan(settings, state, facts, now=None, port=None) -> dict:
    """Правила уведомлений: что показать сейчас, что отложить, что пропустить.

    Возвращает ``{"send": [...], "skipped": [...], "state": новый_стейт}``.
    Никакого ввода-вывода: так правила проверяются без Windows.
    """
    stamp = int(time.time() if now is None else now)
    settings = normalize_settings(settings)[0]
    next_state = _norm_state(state)
    candidates = _candidates(facts, next_state, stamp, port=port)

    send: list[dict] = []
    skipped: list[dict] = []
    backlog: list[dict] = []
    seen = next_state["seen"]
    quiet = in_quiet_hours(settings, stamp)

    pending = [item for item in next_state.get("backlog") or []
               if stamp - int(item.get("at") or 0) <= BACKLOG_TTL]
    pool: list[dict] = []
    known: set = set()
    for item in pending + candidates:
        key = str(item.get("key") or "")
        if not key or key in known:
            continue
        known.add(key)
        pool.append(item)

    for item in pool:
        key = str(item["key"])
        last_seen = seen.get(key)
        if isinstance(last_seen, (int, float)) and stamp - int(last_seen) < SEEN_TTL:
            skipped.append({**item, "reason": "duplicate"})
            continue
        if not settings["enabled"]:
            # Запоминаем как «видели»: включение не должно вылить старые события.
            seen[key] = stamp
            skipped.append({**item, "reason": "disabled"})
            continue
        if not settings["kinds"].get(item["kind"], False):
            seen[key] = stamp
            skipped.append({**item, "reason": "kind_off"})
            continue
        if quiet:
            backlog.append(item)
            skipped.append({**item, "reason": "quiet"})
            continue
        last_sent = next_state["sent"].get(item["kind"])
        if last_sent and stamp - int(last_sent) < int(settings["min_interval_s"]):
            backlog.append(item)
            skipped.append({**item, "reason": "rate"})
            continue
        seen[key] = stamp
        next_state["sent"][item["kind"]] = stamp
        send.append(item)

    # отложенные: не больше BACKLOG_MAX, свежие первыми
    backlog.sort(key=lambda one: int(one.get("at") or 0), reverse=True)
    next_state["backlog"] = [{**one} for one in backlog[:BACKLOG_MAX]]

    # чистка «видели» и «где были запросы» — файл не растёт бесконечно
    next_state["seen"] = {key: value for key, value in seen.items()
                          if stamp - int(value or 0) < SEEN_TTL}
    roots = next_state["roots"]
    if len(roots) > ROOTS_MAX:
        ordered = sorted(roots.items(), key=lambda pair: int((pair[1] or {}).get("at") or 0),
                         reverse=True)[:ROOTS_MAX]
        next_state["roots"] = dict(ordered)
    next_state["updated_at"] = stamp
    return {"send": send, "skipped": skipped, "state": next_state,
            "touched": bool(facts or candidates or send or skipped),
            "quiet": quiet, "enabled": bool(settings["enabled"])}


# ---------------------------------------------------------------------- канал

def fake_path(cfg) -> str:
    """Путь файла песочного канала (пусто — настоящий канал)."""
    raw = os.environ.get("TEAMROOM_NOTIFY_FAKE") or ""
    raw = str(raw).strip()
    if not raw or raw.lower() in ("0", "false", "off"):
        return ""
    if raw == "1":
        return str(_state_dir(cfg) / "notify-fake.jsonl")
    return raw


def powershell_exe() -> str:
    found = shutil.which("powershell") or shutil.which("powershell.exe")
    if found:
        return found
    candidate = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate)


def ps_command() -> str:
    """Готовая команда канала: постоянный скрипт в base64, без данных человека."""
    encoded = base64.b64encode(NOTIFYICON_SCRIPT.encode("utf-16-le")).decode("ascii")
    return "%s -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand %s" % (
        powershell_exe(), encoded)


def channel(cfg) -> str:
    if fake_path(cfg):
        return "fake"
    if getattr(cfg, "sandbox", False):
        return "sandbox"
    if not IS_WINDOWS:
        return "none"
    return "windows" if Path(powershell_exe()).exists() else "none"


def channel_info(cfg) -> dict:
    name = channel(cfg)
    reasons = {"fake": "Песочный канал: уведомления пишутся в файл, окна не показываются",
               "sandbox": "Песочница: настоящие уведомления выключены (нет TEAMROOM_NOTIFY_FAKE)",
               "windows": "Уведомления Windows (значок в области уведомлений)",
               "none": "Нет доступного канала уведомлений в этой системе"}
    return {"name": name, "available": name in ("fake", "windows"),
            "reason": reasons.get(name, reasons["none"])}


def _deliver_fake(path: str, item: dict) -> dict:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({**item, "channel": "fake", "sent_at": int(time.time())},
                          ensure_ascii=False)
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        return {"ok": False, "channel": "fake", "reason": "не удалось записать %s: %s" % (path, exc)}
    return {"ok": True, "channel": "fake", "reason": ""}


def _deliver_windows(item: dict) -> dict:
    exe = powershell_exe()
    if not Path(exe).exists():
        return {"ok": False, "channel": "windows", "reason": "не найден PowerShell"}
    env = os.environ.copy()
    env["TEAMROOM_NOTIFY_TITLE"] = _clean(item.get("title"), 80)
    env["TEAMROOM_NOTIFY_BODY"] = _clean(item.get("body"), TEXT_MAX)
    url = str(item.get("url") or "")
    env["TEAMROOM_NOTIFY_URL"] = url if url.startswith("http://127.0.0.1:") else ""
    encoded = base64.b64encode(NOTIFYICON_SCRIPT.encode("utf-16-le")).decode("ascii")
    argv = [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-EncodedCommand", encoded]
    try:
        proc_mod.popen(argv, gui=True, env=env, cwd=str(Path.home()))
    except Exception as exc:                       # noqa: BLE001 - канал не критичен
        return {"ok": False, "channel": "windows", "reason": "не удалось показать: %s" % exc}
    return {"ok": True, "channel": "windows", "reason": ""}


def deliver(cfg, item: dict) -> dict:
    """Показать одно уведомление. Никогда не бросает: возвращает ``{ok, channel, reason}``."""
    name = channel(cfg)
    if name == "fake":
        return _deliver_fake(fake_path(cfg), item)
    if name == "windows":
        return _deliver_windows(item)
    return {"ok": False, "channel": name,
            "reason": "нет канала уведомлений" if name == "none"
                      else "песочница: настоящие уведомления не показываем"}


# ------------------------------------------------------------------ проход

def tick(cfg, facts, port=None, log=None) -> dict:
    """Один проход уведомлений из тика сторожа: правила → канал → состояние.

    Ошибка канала не валит тик: она попадает в ``history`` и в ответ.
    """
    settings, meta = load_settings(cfg)
    state = load_state(cfg)
    result = plan(settings, state, facts, port=port if port is not None else getattr(cfg, "port", None))
    shown: list[dict] = []
    for item in result["send"]:
        outcome = deliver(cfg, item)
        entry = {"at": int(time.time()), "kind": item["kind"], "room": item["room"],
                 "root": item.get("root") or "", "title": item["title"],
                 "ok": bool(outcome.get("ok")), "channel": outcome.get("channel") or "",
                 "reason": _clean(outcome.get("reason"), 200)}
        shown.append(entry)
    state = result["state"]
    if shown:
        state["history"] = (shown[::-1] + list(state.get("history") or []))[:HISTORY_MAX]
    if result["send"] or result["skipped"] or result.get("touched"):
        save_state(cfg, state)
    skipped: dict = {}
    for item in result["skipped"]:
        skipped[item["reason"]] = int(skipped.get(item["reason"]) or 0) + 1
    if log is not None and (result["send"] or skipped):
        try:
            log.info("уведомления: показано %d, пропущено %s" % (len(shown), skipped or "—"))
        except Exception:                          # noqa: BLE001 - журнал вторичен
            pass
    return {"enabled": bool(result["enabled"]), "quiet": bool(result["quiet"]),
            "channel": channel(cfg), "sent": len(shown), "shown": shown,
            "skipped": skipped, "warnings": meta.get("warnings") or []}


def test_notification(cfg, port=None, log=None) -> dict:
    """Кнопка «Проверить»: одно уведомление в обход тихих часов и выключенности."""
    info = channel_info(cfg)
    item = _candidate("test", "", "Комната команды", "",
                      KIND_TITLES["test"],
                      "Уведомления работают. Клик откроет «Сегодня».",
                      request_url(port if port is not None else getattr(cfg, "port", None), ""),
                      "test:%d" % int(time.time()), int(time.time()))
    if not info["available"]:
        return {"ok": False, "channel": info["name"], "reason": info["reason"], "sent": 0}
    outcome = deliver(cfg, item)
    entry = {"at": int(time.time()), "kind": "test", "room": "", "root": "",
             "title": item["title"], "ok": bool(outcome.get("ok")),
             "channel": outcome.get("channel") or "", "reason": _clean(outcome.get("reason"), 200)}
    state = load_state(cfg)
    state["history"] = ([entry] + list(state.get("history") or []))[:HISTORY_MAX]
    save_state(cfg, state)
    if log is not None:
        try:
            log.info("уведомления: проверка канала %s → %s" % (info["name"], outcome.get("ok")))
        except Exception:                          # noqa: BLE001
            pass
    return {"ok": bool(outcome.get("ok")), "channel": outcome.get("channel") or info["name"],
            "reason": outcome.get("reason") or info["reason"], "sent": 1 if outcome.get("ok") else 0}


def status(cfg, port=None, now=None) -> dict:
    """Что показывает раздел «Уведомления» в Настройках."""
    settings, meta = load_settings(cfg)
    state = load_state(cfg)
    stamp = int(time.time() if now is None else now)
    today = time.localtime(stamp)[:3]
    sent_today = 0
    for entry in state.get("history") or []:
        try:
            if time.localtime(int(entry.get("at") or 0))[:3] == today:
                sent_today += 1
        except (TypeError, ValueError):
            continue
    return {"settings": settings, "warnings": meta.get("warnings") or [],
            "channel": channel_info(cfg), "quiet_now": in_quiet_hours(settings, stamp),
            "sent_today": sent_today,
            "pending": len(state.get("backlog") or []),
            "history": (state.get("history") or [])[:20],
            "kinds": [{"id": kind, "title": KIND_LABELS[kind]} for kind in KINDS],
            "platform": sys.platform,
            "generated_at": stamp}
