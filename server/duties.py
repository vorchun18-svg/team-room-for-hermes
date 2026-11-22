"""Дежурства: задания Hermes по расписанию — **только чтение**.

Источник — домашний каталог Hermes (`HERMES_HOME`, в тестах — поддельный):

* `cron/jobs.json` — список заданий (расписание, включено/выключено, последний запуск);
* `cron/executions.db` — ведомость запусков (SQLite открывается `mode=ro`);
* `cron/output/<job_id>/*.md` — выводы запусков, блок «## Response».

Приложение **ничего не пишет** в Hermes и не запускает задания: «Запустить сейчас» и
правка расписания — не в этой волне. Связь «дежурство ↔ комната» хранится в реестре
комнат (`data/rooms.json`, поле `duties`) и меняется только маршрутом
`PUT /api/rooms/{id}/duties`.

Контракт ответа — `docs/WAVE24-RULES.md` §3.2, описание — `docs/API.md` §W24-DUTIES.

    python -m unittest tests.test_duties -v
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

try:                                     # import server.duties
    from . import rooms as rooms_mod
    from . import worklog as worklog_mod
    from . import proc as proc_mod
    from .http_util import mask_secrets
except ImportError:                      # python server/app.py (плоский импорт)
    import rooms as rooms_mod            # type: ignore
    import worklog as worklog_mod        # type: ignore
    import proc as proc_mod              # type: ignore
    from http_util import mask_secrets   # type: ignore

#: Сколько знаков вывода показываем как «краткий итог».
EXCERPT_MAX = 600
#: Сколько последних запусков отдаём в `recent`.
RECENT_MAX = 5
#: Окно «пропусков» и окно поиска ошибок доступа в выводах.
MISSED_WINDOW_S = 7 * 86400
TOKEN_SCAN_DAYS = 30
#: Просрочка запуска, после которой считаем, что дежурство «отстало».
STALE_GRACE_S = 900
#: Сколько дежурств можно привязать к одной комнате.
MAX_BOUND = 40
#: id задания Hermes — 12 шестнадцатеричных знаков, но формат не гарантирован.
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
#: Признаки того, что доступ к календарю отозван/просрочен (как их пишут агенты).
TOKEN_MARKS = ("invalid_grant", "REFRESH_FAILED", "invalid_request: Token has been expired")

STATUS_MAP = {"claimed": "running", "running": "running", "completed": "completed",
              "failed": "failed", "missed": "missed", "cancelled": "unknown",
              "unknown": "unknown"}

#: Дни недели в cron: 0 — воскресенье (так же, как в `jobs.json`).
_WEEKDAYS = ("воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота")
_WEEKDAYS_PLURAL = ("воскресеньям", "понедельникам", "вторникам", "средам", "четвергам",
                    "пятницам", "субботам")


class DutiesError(Exception):
    """Ошибка привязки/чтения дежурств: HTTP-статус + русский текст + машинный detail."""

    def __init__(self, message: str, http: int = 400, code: str = "bad_request", detail=None):
        super().__init__(message)
        self.message = str(message)
        self.http = int(http)
        self.code = str(code)
        self.detail = dict(detail or {})


# ------------------------------------------------------------------ время

def _ts(value) -> int | None:
    """ISO-время из данных Hermes → unix-секунды (``None``, если не разобрать)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 минута / 2 минуты / 5 минут» — русское число с нужной формой слова."""
    num = abs(int(count or 0))
    if num % 10 == 1 and num % 100 != 11:
        word = one
    elif num % 10 in (2, 3, 4) and num % 100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return "%d %s" % (num, word)


def _clock(hour: int, minute: int) -> str:
    return "%02d:%02d" % (int(hour) % 24, int(minute))


def _minutes_text(minutes: int) -> str:
    """«каждые 30 минут», «каждые 2 часа», «каждые 6 часов»."""
    minutes = int(minutes)
    if minutes % 60 == 0 and minutes >= 60:
        return "каждые " + plural(minutes // 60, "час", "часа", "часов")
    return "каждые " + plural(minutes, "минуту", "минуты", "минут")


# -------------------------------------------------------- расписание по-русски

_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.I)
_EVERY_DAY_RE = re.compile(r"^every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)
_IN_RE = re.compile(r"^in\s+(\d+)\s*(s|m|h|d)$", re.I)


def _unit_plural(unit: str, count: int) -> str:
    unit = (unit or "m").lower()
    if unit.startswith("s"):
        return plural(count, "секунду", "секунды", "секунд")
    if unit.startswith("h"):
        return plural(count, "час", "часа", "часов")
    if unit.startswith("d"):
        return plural(count, "день", "дня", "дней")
    return plural(count, "минуту", "минуты", "минут")


def _cron_parts(expr: str) -> list[str] | None:
    parts = str(expr or "").split()
    return parts if len(parts) == 5 else None


def _day_names(field: str) -> list[int]:
    """Поле дней недели cron (`1-5`, `0,6`, `1`) → список номеров (0 — воскресенье)."""
    out: list[int] = []
    for chunk in str(field or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        found = re.fullmatch(r"(\d)-(\d)", chunk)
        if found:
            start, end = int(found.group(1)), int(found.group(2))
            if start <= end:
                out += list(range(start, end + 1))
            else:                                   # «5-1» через воскресенье
                out += list(range(start, 7)) + list(range(0, end + 1))
            continue
        if chunk.isdigit() and 0 <= int(chunk) <= 7:
            out.append(int(chunk) % 7)
        else:
            return []
    return sorted(set(out))


def _cron_human(expr: str) -> str:
    """Cron-выражение → «каждый день в 08:00», «по будням в 09:00», «каждые 30 минут»."""
    parts = _cron_parts(expr)
    if not parts:
        return ""
    minute, hour, dom, month, dow = parts
    step_min = re.fullmatch(r"\*/(\d+)", minute)
    if step_min and hour == "*" and dom == "*" and month == "*" and dow == "*":
        step = int(step_min.group(1))
        return "каждую минуту" if step == 1 else "каждые " + plural(step, "минуту", "минуты", "минут")
    step_hour = re.fullmatch(r"\*/(\d+)", hour)
    if step_hour and minute.isdigit() and dom == "*" and month == "*" and dow == "*":
        step = int(step_hour.group(1))
        if step == 1:
            return "каждый час в :%02d" % int(minute) if int(minute) else "каждый час"
        return "каждые " + plural(step, "час", "часа", "часов") + \
               (" в :%02d" % int(minute) if int(minute) else "")
    if not (minute.isdigit() and hour.isdigit()):
        return ""
    at = _clock(int(hour), int(minute))
    if dom != "*" and dow == "*":
        if dom.isdigit():
            return "%d-го числа каждого месяца в %s" % (int(dom), at)
        return ""
    if dom == "*" and dow != "*":
        days = _day_names(dow)
        if not days:
            return ""
        if days == [1, 2, 3, 4, 5]:
            return "по будням в " + at
        if days == [0, 6]:
            return "по выходным в " + at
        names = [_WEEKDAYS_PLURAL[day] for day in days]
        if len(names) == 1:
            return "по " + names[0] + " в " + at
        return ", ".join("по " + name for name in names[:-1]) + " и по " + names[-1] + " в " + at
    if dom == "*" and dow == "*" and month == "*":
        return "каждый день в " + at
    return ""


def schedule_human(display="", schedule=None) -> str:
    """Расписание Hermes → по-русски («каждый день в 08:00», «каждые 30 минут»).

    Принимает и словарь `schedule` из `jobs.json` (`{kind, expr|minutes}`), и строку
    `schedule_display` («every 30m», «in 2h», «0 8 * * *»). Непонятное расписание
    не выдумываем: отдаём как есть с пометкой «по расписанию».
    """
    doc = schedule if isinstance(schedule, dict) else {}
    text = str(display or doc.get("display") or "").strip()
    kind = str(doc.get("kind") or "").strip().lower()
    expr = str(doc.get("expr") or "").strip()

    if kind == "cron" and expr:
        human = _cron_human(expr)
        if human:
            return human
    if kind == "interval":
        minutes = doc.get("minutes")
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 0
        if minutes > 0:
            return _minutes_text(minutes)

    found = _EVERY_DAY_RE.match(text)
    if found:
        hour = int(found.group(1))
        minute = int(found.group(2) or 0)
        meridiem = (found.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        return "каждый день в " + _clock(hour, minute)

    found = _INTERVAL_RE.match(text)
    if found:
        count, unit = int(found.group(1)), found.group(2).lower()
        if unit.startswith("s"):
            if count >= 60 and count % 60 == 0:
                return _minutes_text(count // 60)
            return "каждую секунду" if count == 1 else \
                "каждые " + plural(count, "секунду", "секунды", "секунд")
        if unit.startswith("h"):
            return "каждый час" if count == 1 else "каждые " + plural(count, "час", "часа", "часов")
        if unit.startswith("d"):
            return "каждый день" if count == 1 else "каждые " + plural(count, "день", "дня", "дней")
        if count == 1:
            return "каждую минуту"
        if count >= 60 and count % 60 == 0:
            return "каждые " + plural(count // 60, "час", "часа", "часов")
        return _minutes_text(count)

    found = _IN_RE.match(text)
    if found:
        count, unit = int(found.group(1)), found.group(2)
        return "один раз через " + _unit_plural(unit, count)

    human = _cron_human(text)
    if human:
        return human
    return ("по расписанию " + text) if text else "расписание не указано"


def interval_seconds(schedule=None, display="") -> int | None:
    """Ожидаемый шаг расписания в секундах (``None`` — шаг не определить)."""
    doc = schedule if isinstance(schedule, dict) else {}
    kind = str(doc.get("kind") or "").lower()
    if kind == "interval":
        try:
            return max(1, int(doc.get("minutes"))) * 60
        except (TypeError, ValueError):
            return None
    expr = str(doc.get("expr") or display or "").strip()
    parts = _cron_parts(expr)
    if not parts:
        return None
    minute, hour = parts[0], parts[1]
    step_min = re.fullmatch(r"\*/(\d+)", minute)
    if step_min and hour == "*":
        return max(1, int(step_min.group(1))) * 60
    step_hour = re.fullmatch(r"\*/(\d+)", hour)
    if step_hour:
        return max(1, int(step_hour.group(1))) * 3600
    if minute.isdigit() and hour.isdigit():
        dom, month, dow = parts[2], parts[3], parts[4]
        if dom != "*":
            return 30 * 86400            # раз в месяц
        if dow != "*":
            return 7 * 86400             # раз в неделю
        return 86400                     # раз в сутки
    return None


# ------------------------------------------------------------------ чтение

def home_of(cfg=None) -> Path:
    """Домашний каталог Hermes (только чтение). В тестах и песочнице — поддельный.

    В песочнице (`TEAMROOM_HERMES_HOME`) читаем её домашний каталог, а не реальный:
    иначе «Запустить сейчас» из песочницы целилось бы в живые задания Hermes.
    """
    raw = getattr(cfg, "hermes_home", None)
    if getattr(cfg, "sandbox", False) and getattr(cfg, "kanban_home", None):
        raw = getattr(cfg, "kanban_home")
    return Path(str(raw)) if raw else Path(".")


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh, strict=False)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _jobs_doc(cfg) -> dict:
    return _read_json(home_of(cfg) / "cron" / "jobs.json")


def jobs(cfg) -> list[dict]:
    """Все задания Hermes (сырые записи `jobs.json`, порядок файла сохраняется)."""
    doc = _jobs_doc(cfg)
    raw = doc.get("jobs")
    if not isinstance(raw, list):
        return []
    return [job for job in raw if isinstance(job, dict) and job.get("id")]


def job_ids(cfg) -> set:
    return {str(job.get("id")) for job in jobs(cfg)}


def _connect(path: Path) -> sqlite3.Connection | None:
    """Чужую БД открываем только на чтение (`mode=ro`), без создания файла."""
    try:
        return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True, timeout=3)
    except sqlite3.Error:
        return None


def executions(cfg, limit: int = 40) -> dict:
    """Последние запуски по заданиям: ``{job_id: [{status, started_at, ...}, …]}``.

    Берём ``limit`` последних запусков **каждого** задания (окно по `job_id`), а не
    общий хвост: у частого задания тысячи записей, и общий срез вытесняет редкие —
    тогда «пропуски» и «последний запуск» у них теряются.
    """
    db = home_of(cfg) / "cron" / "executions.db"
    con = _connect(db)
    if con is None:
        return {}
    out: dict = {}
    try:
        rows = con.execute(
            "SELECT job_id, status, claimed_at, started_at, finished_at, scheduled_instant,"
            " error, delivery_outcome,"
            " ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY claimed_at DESC) AS rn"
            " FROM executions").fetchall()
        missed = con.execute(
            "SELECT job_id, claimed_at, scheduled_instant FROM executions"
            " WHERE status = 'missed'").fetchall()
    except sqlite3.Error:
        rows, missed = [], []
    finally:
        con.close()
    cutoff = int(time.time()) - MISSED_WINDOW_S
    for row in rows:
        job_id, status, claimed_at, started_at, finished_at, scheduled, error, delivery, rn = row
        if rn is not None and int(rn) > max(1, int(limit)):
            continue
        item = {
            "status": STATUS_MAP.get(str(status or "").lower(), "unknown"),
            "raw_status": str(status or ""),
            "claimed_at": _ts(claimed_at),
            "started_at": _ts(started_at),
            "finished_at": _ts(finished_at),
            "scheduled_for": _ts(scheduled),
            "delivered": _delivered(delivery),
            "error": (mask(str(error))[:300] or None) if error else None,
        }
        out.setdefault(str(job_id), []).append(item)
    for job_id, claim, scheduled in missed:
        stamp = _ts(claim) or _ts(scheduled)
        if stamp is None or stamp < cutoff:
            continue
        out.setdefault("__missed__", {}).setdefault(str(job_id), 0)
        out["__missed__"][str(job_id)] += 1
    for rows_list in out.values():
        if isinstance(rows_list, list):
            rows_list.sort(key=lambda row: (row["claimed_at"] or row["scheduled_for"] or 0),
                           reverse=True)
    return out


def _delivered(value) -> bool | None:
    """`delivery_outcome` Hermes → «доставлено ли» (``None`` — не знаем)."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in ("delivered", "sent", "ok"):
        return True
    if text in ("suppressed", "failed", "error", "unverified"):
        return False
    return None


def mask(text) -> str:
    """Секреты в тексте вывода — как в «Ходе работы» (плюс значения `token=…`)."""
    out = mask_secrets(str(text or ""))
    try:
        out = worklog_mod.mask(out)
    except Exception:                    # noqa: BLE001 — маски не должны валить чтение
        pass
    home = str(home_of())
    if home and home not in (".", ""):
        out = out.replace(home, "~").replace(home.replace("\\", "/"), "~")
    return out


def _output_dir(cfg, job_id) -> Path:
    return home_of(cfg) / "cron" / "output" / str(job_id)


def output_files(cfg, job_id) -> list[Path]:
    """Файлы выводов задания, новые в конце (имена — «дата_время.md»)."""
    folder = _output_dir(cfg, job_id)
    try:
        files = [p for p in folder.glob("*.md") if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: (p.name, p.stat().st_mtime if p.exists() else 0))
    return files


def _response_block(text: str) -> str:
    """Блок «## Response» из вывода запуска (до следующего заголовка второго уровня)."""
    lines = str(text or "").splitlines()
    start = None
    for idx, line in enumerate(lines):
        if re.match(r"^##\s+response\b", line.strip(), re.I):
            start = idx + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        if re.match(r"^##\s+\S", line):
            break
        body.append(line)
    return "\n".join(body).strip()


def _trim(text: str, limit: int = EXCERPT_MAX) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit - 80:
        cut = cut[:space]
    return cut.rstrip() + "…"


def excerpt(cfg, job_id) -> str:
    """Краткий итог последнего вывода задания (≤ 600 знаков, секреты скрыты)."""
    files = output_files(cfg, job_id)
    if not files:
        return ""
    try:
        with open(files[-1], "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    return _trim(mask(_response_block(text)))


def token_hits(cfg, days: int = TOKEN_SCAN_DAYS) -> list[dict]:
    """Выводы дежурств, где видна ошибка доступа к календарю (``invalid_grant`` и подобное).

    Возвращает ``[{job_id, at, day}]``, отсортировано по времени. Нужно проверке
    здоровья: «доступ к календарю недействителен с DD.MM» — по факту, а не по догадке.
    """
    cutoff = int(time.time()) - max(1, int(days)) * 86400
    found: list[dict] = []
    for job in jobs(cfg):
        job_id = str(job.get("id"))
        for path in output_files(cfg, job_id):
            stamp = _file_day(path)
            if stamp is not None and stamp < cutoff - 86400:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if not any(mark in text for mark in TOKEN_MARKS):
                continue
            # Ищем только в итоге запуска («## Response»): в тексте задания и в
            # подгруженных памятках те же слова встречаются как цитата, и это дало бы
            # ложное «доступ недействителен» там, где всё в порядке.
            block = _response_block(text)
            if not block or not any(mark in block for mark in TOKEN_MARKS):
                continue
            at = stamp if stamp is not None else int(_mtime(path))
            found.append({"job_id": job_id, "at": at, "day": _day_text(at)})
    found.sort(key=lambda item: item["at"])
    return found


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _file_day(path: Path) -> int | None:
    """Время из имени вывода «2026-11-17_19-01-39.md» → unix (``None`` — не разобрать)."""
    found = re.match(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", path.name)
    if not found:
        return None
    try:
        moment = datetime(int(found.group(1)), int(found.group(2)), int(found.group(3)),
                          int(found.group(4)), int(found.group(5)), int(found.group(6)))
    except ValueError:
        return None
    return int(moment.timestamp())


def _day_text(stamp) -> str:
    try:
        return datetime.fromtimestamp(int(stamp)).strftime("%d.%m.%Y")
    except (TypeError, ValueError, OSError):
        return ""


# ------------------------------------------------------------- состояние

def _last_run(job: dict, rows: list, now: int) -> dict | None:
    """Последний запуск: сначала ведомость (точнее), затем `jobs.json`."""
    if rows:
        row = rows[0]
        dispatch = job.get("last_dispatch") if isinstance(job.get("last_dispatch"), dict) else {}
        late = dispatch.get("lateness_seconds")
        try:
            late = int(float(late)) if late is not None else None
        except (TypeError, ValueError):
            late = None
        if late is None and row["scheduled_for"] and row["started_at"]:
            late = max(0, int(row["started_at"] - row["scheduled_for"]))
        return {
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "scheduled_for": row["scheduled_for"],
            "late_s": late,
            "delivered": row["delivered"],
            "error": row["error"],
        }
    ran_at = _ts(job.get("last_run_at"))
    if ran_at is None:
        return None
    raw = str(job.get("last_status") or "").strip().lower()
    status = {"ok": "completed", "success": "completed", "error": "failed",
              "failed": "failed", "running": "running"}.get(raw, "unknown")
    error = job.get("last_error")
    dispatch = job.get("last_dispatch") if isinstance(job.get("last_dispatch"), dict) else {}
    try:
        late = int(float(dispatch.get("lateness_seconds"))) if dispatch.get("lateness_seconds") is not None else None
    except (TypeError, ValueError):
        late = None
    return {
        "status": status,
        "started_at": ran_at,
        "finished_at": ran_at,
        "scheduled_for": _ts(dispatch.get("scheduled_at")),
        "late_s": late,
        "delivered": None,
        "error": (mask(str(error))[:300] or None) if error else None,
    }


def _problem(job: dict, last: dict | None, rows: list, now: int) -> dict | None:
    """Замечание к дежурству: ``failed|stale|never_ran|disabled_but_due``."""
    enabled = bool(job.get("enabled"))
    if last and last["status"] == "failed":
        tail = (" — " + last["error"][:160]) if last.get("error") else ""
        return {"kind": "failed", "text": "последний запуск завершился ошибкой" + tail}
    if not enabled:
        return {"kind": "disabled_but_due",
                "text": "дежурство выключено — по расписанию не запускается"}
    if last is None and not rows:
        return {"kind": "never_ran", "text": "дежурство ещё ни разу не запускалось"}
    if last and last["status"] == "running":
        return None
    next_run = _ts(job.get("next_run_at"))
    if next_run is not None and next_run < now - STALE_GRACE_S:
        when = datetime.fromtimestamp(next_run).strftime("%d.%m в %H:%M")
        return {"kind": "stale", "text": "должно было запуститься %s, но запуска не было" % when}
    step = interval_seconds(job.get("schedule"), job.get("schedule_display"))
    last_at = None
    if last:
        last_at = last["finished_at"] or last["started_at"]
    if step and last_at and now - last_at > max(step * 2, step + 1800):
        age = now - last_at
        return {"kind": "stale",
                "text": "последний запуск был %s назад при расписании «%s»"
                        % (plural(age // 3600 or 1, "час", "часа", "часов"),
                           schedule_human(job.get("schedule_display"), job.get("schedule")))}
    return None


def _recent(rows: list) -> list[dict]:
    return [{"status": row["status"], "started_at": row["started_at"],
             "scheduled_for": row["scheduled_for"]} for row in rows[:RECENT_MAX]]


def _missed_recent(rows: list, now: int) -> int:
    """Сколько запусков пропущено за окно (по ведомости)."""
    cutoff = now - MISSED_WINDOW_S
    return sum(1 for row in rows
               if row["status"] == "missed"
               and (row["claimed_at"] or row["scheduled_for"] or 0) >= cutoff)

def bound_rooms(cfg, job_id) -> list[str]:
    """Комнаты, к которым привязано дежурство (из `rooms.json`, поле `duties`)."""
    out: list[str] = []
    try:
        rooms = rooms_mod.list_rooms(cfg, include_archived=True)
    except Exception:                    # noqa: BLE001 — без реестра привязок нет
        return out
    for room in rooms:
        duties = room.get("duties")
        if isinstance(duties, list) and str(job_id) in [str(x) for x in duties]:
            out.append(str(room.get("id") or ""))
    return [room_id for room_id in out if room_id]


def deliver_info(job: dict, bound: list | None) -> dict:
    """Куда придёт результат дежурства (W29-P5, пункт «Куда придёт результат»).

    Для дежурства комнаты итог запуска попадает во «Входящие» этой комнаты —
    и только когда что-то изменилось; тихий запуск оставляет строку в истории.
    Непривязанное задание остаётся в журнале Hermes: в комнате его не видно.
    """
    rooms = [str(one) for one in (bound or []) if one]
    target = str(job.get("deliver") or "local")
    if rooms:
        room = rooms[0]
        return {"where": "inbox", "label": "Входящие комнаты", "room": room,
                "href": "#/room/%s/inbox" % room, "target": target,
                "text": "Итог придёт во «Входящие» комнаты — при изменении. "
                        "Без изменений останется тихая строка в истории."}
    return {"where": "local", "label": "Не в комнату", "room": "", "href": "",
            "target": target,
            "text": "Дежурство не привязано к комнате: результат остаётся в журнале "
                    "Hermes. Привяжите его к комнате, чтобы итог приходил во «Входящие»."}


def items(cfg, room=None, now: int | None = None) -> dict:
    """``GET /api/duties`` — все дежурства (или только привязанные к комнате ``room``)."""
    now = int(now if now is not None else time.time())
    rows_by_job = executions(cfg)
    missed_map = rows_by_job.pop("__missed__", {}) or {}
    rooms_map = {}
    try:
        for one in rooms_mod.list_rooms(cfg, include_archived=True):
            rooms_map[str(one.get("id") or "")] = one
    except Exception:                    # noqa: BLE001
        rooms_map = {}
    out: list[dict] = []
    system: list[dict] = []
    for job in jobs(cfg):
        job_id = str(job.get("id"))
        rows = rows_by_job.get(job_id) or []
        bound = [room_id for room_id, one in rooms_map.items()
                 if isinstance(one.get("duties"), list)
                 and job_id in [str(x) for x in one.get("duties") or []]]
        last = _last_run(job, rows, now)
        entry = {
            "job_id": job_id,
            "name": str(job.get("name") or job_id),
            "enabled": bool(job.get("enabled")),
            "schedule": str(job.get("schedule_display") or ""),
            "schedule_human": schedule_human(job.get("schedule_display"), job.get("schedule")),
            "bound_rooms": bound,
            "last_run": last,
            "next_run_at": _ts(job.get("next_run_at")),
            "missed_recent": int(missed_map.get(job_id) or 0),
            "recent": _recent(rows),
            "excerpt": excerpt(cfg, job_id),
            "problem": _problem(job, last, rows, now),
            # W29-P5: чьё это дежурство и куда придёт результат.
            "scope": "room" if bound else "system",
            "deliver": deliver_info(job, bound),
            "model": str(job.get("model") or ""),
            "provider": str(job.get("provider") or ""),
            "workdir": str(job.get("workdir") or ""),
            "skills": [str(x) for x in (job.get("skills") or [])][:6],
            "script": str(job.get("script") or ""),
            "monitor_url": str(job.get("monitor_url") or ""),
        }
        if entry["scope"] == "system":
            system.append(entry)         # видно всегда: «не относятся к комнатам»
        if room and room != "all" and room not in bound:
            continue
        out.append(entry)
    out.sort(key=lambda item: (item["problem"] is None, item["name"].lower()))
    system.sort(key=lambda item: (item["problem"] is None, item["name"].lower()))
    # W29-P5: «дежурства комнат» и «системные задачи Hermes» — разные вещи.
    # `items` остаётся прежним списком (совместимость), а системные отдаём и отдельным
    # ключом: интерфейс показывает их свёрнутым блоком «не относятся к комнатам».
    return {"generated_at": now, "items": out, "system": system,
            "counts": {"room": len([one for one in out if one["scope"] != "system"]),
                       "system": len(system), "total": len(out),
                       "bound_here": len([one for one in out if room and room != "all"
                                          and room in (one.get("bound_rooms") or [])])}}


def problems(cfg, now: int | None = None) -> list[dict]:
    """Дежурства с замечаниями — для проверки здоровья."""
    return [item for item in items(cfg, now=now)["items"] if item.get("problem")]


# ------------------------------------------------------------- привязка

def check_duties(value) -> list[str]:
    """Проверить список id дежурств: список строк, без повторов, разумной длины."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise DutiesError("Нужен список дежурств (job_ids)", 400, "bad_request",
                          {"field": "job_ids"})
    out: list[str] = []
    for raw in value:
        job_id = str(raw or "").strip()
        if not job_id:
            continue
        if not JOB_ID_RE.fullmatch(job_id):
            raise DutiesError("Недопустимый номер дежурства", 400, "bad_request",
                              {"field": "job_ids", "value": job_id[:64]})
        if job_id not in out:
            out.append(job_id)
    if len(out) > MAX_BOUND:
        raise DutiesError("Слишком много дежурств у комнаты (не больше %d)" % MAX_BOUND,
                          400, "bad_request", {"field": "job_ids", "limit": MAX_BOUND})
    return out


def set_room_duties(cfg, room: dict, job_ids) -> list[str]:
    """Привязать дежурства к комнате (меняется только реестр комнат, Hermes не трогаем)."""
    if not isinstance(room, dict) or not room.get("id"):
        raise DutiesError("Комната не найдена", 404, "not_found")
    wanted = check_duties(job_ids)
    known = job_ids_of_hermes(cfg)
    if known:
        unknown = [job_id for job_id in wanted if job_id not in known]
        if unknown:
            raise DutiesError("Таких дежурств нет в Hermes", 422, "unknown_job",
                              {"field": "job_ids", "unknown": unknown[:10]})
    updated: dict = {}
    try:
        updated = rooms_mod.update_room(cfg, str(room["id"]), {"duties": wanted}) or {}
    except Exception as exc:             # noqa: BLE001 — ошибку реестра отдаём как свою
        message = str(getattr(exc, "message", None) or exc or "Не удалось сохранить привязку")
        raise DutiesError(message, int(getattr(exc, "http", 0) or 400),
                          str(getattr(exc, "code", "bad_request") or "bad_request"),
                          getattr(exc, "detail", None) or {}) from exc
    duties = updated.get("duties") if isinstance(updated, dict) else wanted
    return [str(x) for x in (duties or [])]


def job_ids_of_hermes(cfg) -> set:
    """Известные id заданий (пусто, если `jobs.json` не читается — тогда не проверяем)."""
    return job_ids(cfg)


# ------------------------------------------------- недельная сетка (W26-D8)

#: Названия дней недели для сетки: 0 — понедельник (как `date.weekday()`).
WEEK_LABELS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEK_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
#: Сколько плашек задания показываем в дне; остальные — «+N ещё».
WEEK_DAY_JOBS = 3
#: Сколько плановых запусков перечисляем в панели дня (W29-P5).
WEEK_DAY_PLAN = 12


def _runs_between(cfg, start_ts: int, end_ts: int) -> list[tuple]:
    """Запуски ведомости за окно ``[start_ts, end_ts)``: ``[(job_id, status, ts), …]``.

    Читаем окно прямо в SQL (а не хвост на задание): недельная сетка считает все
    запуски дня, и срез в 40 записей у частого задания занизил бы итог.
    """
    db = home_of(cfg) / "cron" / "executions.db"
    con = _connect(db)
    if con is None:
        return []
    left = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(start_ts)))
    right = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(end_ts)))
    try:
        rows = con.execute(
            "SELECT job_id, status, claimed_at, started_at FROM executions"
            " WHERE claimed_at >= ? AND claimed_at < ?", (left, right)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    out: list[tuple] = []
    for job_id, status, claimed, started in rows:
        stamp = _ts(claimed) or _ts(started)
        if stamp is None:
            continue
        out.append((str(job_id), STATUS_MAP.get(str(status or "").lower(), "unknown"), int(stamp)))
    return out


def _planned_on(job: dict, day: date) -> int:
    """Сколько раз по расписанию задание запускается в этот день (0 — не запускается)."""
    if not job.get("enabled"):
        return 0
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    kind = str(schedule.get("kind") or "").lower()
    if kind == "interval":
        try:
            minutes = int(schedule.get("minutes"))
        except (TypeError, ValueError):
            return 0
        if minutes <= 0:
            return 0
        return max(1, 86400 // (minutes * 60))
    expr = str(schedule.get("expr") or job.get("schedule_display") or "").strip()
    parts = _cron_parts(expr)
    if not parts:
        found = _INTERVAL_RE.match(str(job.get("schedule_display") or "").strip())
        if not found:
            return 0
        count, unit = int(found.group(1)), found.group(2).lower()
        step = count * (86400 if unit.startswith("d") else 3600 if unit.startswith("h") else 1)
        return max(1, 86400 // step) if step else 0
    minute, hour, dom, month, dow = parts
    if dow != "*":
        days = _day_names(dow)
        if days and ((day.weekday() + 1) % 7) not in days:   # cron: 0 — воскресенье
            return 0
    if dom != "*":
        return 1 if str(day.day) == dom else 0
    step_min = re.fullmatch(r"\*/(\d+)", minute)
    if step_min and hour == "*":
        return max(1, 1440 // int(step_min.group(1)))
    step_hour = re.fullmatch(r"\*/(\d+)", hour)
    if step_hour:
        return max(1, 24 // int(step_hour.group(1)))
    return 1


def week(cfg, room=None, now: int | None = None, start=None) -> dict:
    """Недельная сетка как в Mission Control: дни, запуски, «+N ещё», итог и план.

    ``start`` — понедельник недели (по умолчанию — текущая неделя по местному времени).
    Считаем **фактические** запуски из ведомости и **план** по расписанию: в будущих днях
    запусков ещё нет, и врать «0 запусков» без слова «план» нельзя.
    """
    now = int(now if now is not None else time.time())
    if isinstance(start, str) and start.strip():
        try:                              # `?start=2026-11-16` — листание недель
            start = datetime.strptime(start.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Не понял дату недели: нужен вид 2026-11-16") from None
    start = start if isinstance(start, date) else datetime.fromtimestamp(now).date()
    start = start - timedelta(days=start.weekday())      # всегда понедельник недели
    days = [start + timedelta(days=idx) for idx in range(7)]
    start_ts = int(datetime.combine(start, dtime.min).timestamp())
    end_ts = start_ts + 7 * 86400
    runs = _runs_between(cfg, start_ts, end_ts)
    all_jobs = jobs(cfg)
    bound: set | None = None
    if room and str(room) != "all":
        try:
            bound = {str(job.get("id")) for job in all_jobs
                     if str(room) in [str(x) for x in bound_rooms(cfg, job.get("id"))]}
        except Exception:                # noqa: BLE001 — без реестра считаем «все»
            bound = None
    names = {str(job.get("id")): str(job.get("name") or job.get("id")) for job in all_jobs}
    today_stamp = datetime.fromtimestamp(now).date()

    per_day: dict = {day: {"runs": 0, "failed": 0, "missed": 0, "jobs": {}} for day in days}
    for job_id, status, stamp in runs:
        if bound is not None and job_id not in bound:
            continue
        day = datetime.fromtimestamp(stamp).date()
        bucket = per_day.get(day)
        if bucket is None:
            continue
        bucket["runs"] += 1
        if status == "failed":
            bucket["failed"] += 1
        elif status == "missed":
            bucket["missed"] += 1
        info = bucket["jobs"].setdefault(job_id, {"job_id": job_id,
                                                  "name": names.get(job_id, job_id),
                                                  "runs": 0, "failed": 0})
        info["runs"] += 1
        if status == "failed":
            info["failed"] += 1

    out_days: list[dict] = []
    for day in days:
        bucket = per_day[day]
        job_list = sorted(bucket["jobs"].values(), key=lambda one: (-one["runs"], one["name"].lower()))
        items = [{"job_id": one["job_id"], "name": one["name"], "runs": int(one["runs"]),
                  "state": "failed" if one["failed"] else "ok"}
                 for one in job_list[:WEEK_DAY_JOBS]]
        planned = 0
        planned_jobs: list = []
        for job in all_jobs:
            if bound is not None and str(job.get("id")) not in bound:
                continue
            count = _planned_on(job, day)
            if not count:
                continue
            planned += count
            planned_jobs.append({"job_id": str(job.get("id")),
                                 "name": names.get(str(job.get("id")), str(job.get("id"))),
                                 "runs": int(count)})
        planned_jobs.sort(key=lambda one: one["name"].lower())
        out_days.append({
            "date": day.isoformat(),
            "weekday": int(day.weekday()),
            "label": WEEK_LABELS[day.weekday()],
            "full": WEEK_FULL[day.weekday()],
            "day": int(day.day),
            "month": int(day.month),
            "today": bool(day == today_stamp),
            "future": bool(day > today_stamp),
            "runs": int(bucket["runs"]),
            "failed": int(bucket["failed"]),
            "missed": int(bucket["missed"]),
            "jobs": items,
            "more": max(0, len(job_list) - WEEK_DAY_JOBS),
            "planned": int(planned),
            # W29-P5: что именно запустится по расписанию — чтобы будущий день
            # открывался списком плановых запусков, а не пустой плашкой.
            "planned_jobs": planned_jobs[:WEEK_DAY_PLAN],
            "planned_more": max(0, len(planned_jobs) - WEEK_DAY_PLAN),
        })
    total_runs = sum(day["runs"] for day in out_days)
    return {"start": days[0].isoformat(), "end": days[-1].isoformat(),
            "prev": (start - timedelta(days=7)).isoformat(),
            "next": (start + timedelta(days=7)).isoformat(),
            "today": today_stamp.isoformat(),
            "days": out_days, "runs": int(total_runs),
            "failed": sum(day["failed"] for day in out_days),
            "missed": sum(day["missed"] for day in out_days)}


# --------------------------------------------------- действия (W26-D8, по клику)

#: Действие → аргумент штатного CLI Hermes (`hermes cron <…>`).
CLI_ACTIONS = {"run": "run", "pause": "pause", "resume": "resume",
               "remove": "remove"}
#: Все действия маршрута.
ACTIONS = ("run", "pause", "resume", "bind", "unbind", "schedule")
ACTION_LABELS = {"run": "Запустить сейчас", "pause": "Пауза", "resume": "Возобновить",
                 "bind": "Привязать", "unbind": "Отвязать", "schedule": "Изменить расписание"}
#: Сколько ждём ответа CLI и сколько предложений расписания храним.
ACTION_TIMEOUT_S = 25
PROPOSALS_MAX = 50

#: Подставной исполнитель CLI (тесты): функция ``args -> {code,out,err}``.
_cli_runner = None


def set_cli_runner(runner) -> None:
    """Подменить исполнителя CLI (тесты, песочница). ``None`` — вернуть настоящий."""
    global _cli_runner
    _cli_runner = runner


def reset_cli_runner() -> None:
    set_cli_runner(None)


def cli_bin(cfg=None) -> str:
    """Путь к штатному CLI Hermes (`hermes`); в песочнице — подставной скрипт."""
    return str(getattr(cfg, "hermes_bin", None) or os.environ.get("TEAMROOM_HERMES_BIN") or "hermes")


def cli_command(action: str, job_id: str) -> list[str]:
    """Аргументы CLI для действия: только `cron run|pause|resume <id>`, без свободы."""
    return ["cron", CLI_ACTIONS[str(action)], str(job_id)]


def cli_display(action: str, job_id: str) -> str:
    return cli_bin() + " " + " ".join(cli_command(action, job_id))


def dry_run_on(cfg=None) -> bool:
    """Пробный режим: команда печатается, но не выполняется (`TEAMROOM_DUTIES_DRY=1`)."""
    raw = getattr(cfg, "duties_dry_run", None)
    if raw is None:
        raw = os.environ.get("TEAMROOM_DUTIES_DRY", "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _default_runner(cfg):
    """Настоящий вызов CLI: ровно `hermes cron <action> <id>`, таймаут, без shell."""
    binary = cli_bin(cfg)
    home = str(home_of(cfg))

    def run(args):
        env = dict(os.environ)
        if home not in ("", "."):
            env["HERMES_HOME"] = home
        try:
            # W26-QA1: запуск только через server/proc.py — он один ставит «без окна»
            # (CREATE_NO_WINDOW) и запрещает shell. Прямой subprocess.run мигал консолью.
            done = proc_mod.run([binary] + list(args), capture_output=True, text=True,
                                timeout=ACTION_TIMEOUT_S, env=env)
        except FileNotFoundError:
            return {"code": 127, "out": "", "err": "команда hermes не найдена"}
        except subprocess.TimeoutExpired:
            return {"code": 124, "out": "", "err": "команда не ответила за %d с" % ACTION_TIMEOUT_S}
        except OSError as exc:
            return {"code": 126, "out": "", "err": str(exc)[:200]}
        return {"code": int(done.returncode), "out": str(done.stdout or "").strip()[:400],
                "err": str(done.stderr or "").strip()[:400]}

    return run


def runner_for(cfg):
    """Исполнитель CLI: подставной, если задан, иначе настоящий Hermes."""
    return _cli_runner or _default_runner(cfg)


def _job_by_id(cfg, job_id) -> dict:
    wanted = str(job_id or "").strip()
    for job in jobs(cfg):
        if str(job.get("id")) == wanted:
            return job
    raise DutiesError("Такого дежурства нет", 422, "unknown_job", {"job_id": wanted[:64]})


def proposals_path(cfg) -> Path | None:
    base = getattr(cfg, "data_dir", None)
    if not base:
        return None
    return Path(str(base)) / "state" / "duties" / "proposals.json"


def _read_proposals(cfg) -> list[dict]:
    path = proposals_path(cfg)
    if path is None:
        return []
    doc = _read_json(path)
    items = doc.get("items")
    return [one for one in items if isinstance(one, dict)] if isinstance(items, list) else []


def _write_proposals(cfg, items: list[dict]) -> None:
    path = proposals_path(cfg)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"items": items[-PROPOSALS_MAX:]}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass                             # предложение — не повод ронять ответ


def proposals(cfg, job_id=None) -> list[dict]:
    """Сохранённые предложения расписания (Hermes они не меняют)."""
    items = _read_proposals(cfg)
    if job_id:
        items = [one for one in items if str(one.get("job_id")) == str(job_id)]
    return items


def _item_of(cfg, job_id):
    """Свежая карточка дежурства для ответа на действие (``None`` — не прочитать)."""
    try:
        for item in items(cfg)["items"]:
            if str(item.get("job_id")) == str(job_id):
                return item
    except Exception:                    # noqa: BLE001 — действие уже выполнено
        return None
    return None


def act(cfg, room, job_id, action, *, confirm=False, schedule="", runner=None,
        now: int | None = None) -> dict:
    """Выполнить действие над дежурством — только по подтверждению человека.

    * `run|pause|resume` — штатный CLI `hermes cron …` (в тестах — подставной);
    * `bind|unbind` — привязка к комнате (меняется только реестр комнат);
    * `schedule` — **предложение** «изменить на…»: Hermes не меняется.

    Без ``confirm=True`` ничего не делаем: это действие видно снаружи.
    """
    now = int(now if now is not None else time.time())
    action = str(action or "").strip().lower()
    job_id = str(job_id or "").strip()
    if action not in ACTIONS:
        raise DutiesError("Неизвестное действие", 400, "bad_request",
                          {"action": action[:32], "allowed": list(ACTIONS)})
    if not confirm:
        raise DutiesError("Это действие меняет дежурство в Hermes — нужно ваше подтверждение",
                          409, "confirm_required", {"action": action, "job_id": job_id})
    job = _job_by_id(cfg, job_id)
    name = str(job.get("name") or job_id)
    out = {"ok": True, "action": action, "label": ACTION_LABELS[action], "job_id": job_id,
           "name": name, "applied": False, "dry": False, "command": None, "text": "",
           "bound_rooms": [], "proposal": None, "generated_at": now}

    if action in CLI_ACTIONS:
        command = cli_display(action, job_id)
        out["command"] = command
        if dry_run_on(cfg):
            out["dry"] = True
            out["text"] = "Пробный режим: «%s» не запускалась." % command
            return out
        result = (runner or runner_for(cfg))(cli_command(action, job_id))
        code = int(result.get("code") or 0)
        if code != 0:
            raise DutiesError(
                "Дежурство не откликнулось: «%s» вернула код %d" % (command, code),
                502, "cli_failed",
                {"command": command, "code": code,
                 "err": str(result.get("err") or "")[:300],
                 "out": str(result.get("out") or "")[:300]})
        out["applied"] = True
        out["text"] = {
            "run": "Запуск поставлен в работу: итог появится в ведомости.",
            "pause": "Дежурство поставлено на паузу — по расписанию не запускается.",
            "resume": "Дежурство снова идёт по расписанию.",
        }[action]
        tail = str(result.get("out") or "").strip()
        if tail:
            out["cli_out"] = mask(tail)[:300]
        out["job"] = _item_of(cfg, job_id)
        return out

    if action in ("bind", "unbind"):
        if not isinstance(room, dict) or not room.get("id"):
            raise DutiesError("Привязать дежурство можно только внутри комнаты", 400,
                              "room_required", {"job_id": job_id})
        current = [str(x) for x in (room.get("duties") or [])]
        if action == "bind":
            if job_id not in current:
                current.append(job_id)
        else:
            current = [one for one in current if one != job_id]
        bound = set_room_duties(cfg, room, current)
        out["applied"] = True
        out["bound_rooms"] = [str(room["id"])] if action == "bind" else []
        out["room"] = str(room["id"])
        out["text"] = ("Дежурство привязано к комнате." if action == "bind"
                       else "Дежурство отвязано от комнаты (само задание в Hermes осталось).")
        out["duties"] = [str(x) for x in bound]
        out["job"] = _item_of(cfg, job_id)
        return out

    # schedule — только предложение
    wanted = str(schedule or "").strip()
    if not wanted:
        raise DutiesError("Нужно новое расписание", 400, "schedule_required",
                          {"field": "schedule", "job_id": job_id})
    if len(wanted) > 80:
        raise DutiesError("Расписание слишком длинное", 400, "bad_request",
                          {"field": "schedule", "limit": 80})
    current_human = schedule_human(job.get("schedule_display"), job.get("schedule"))
    proposal = {
        "id": "dp_%d" % now,
        "job_id": job_id,
        "job_name": name,
        "from": current_human,
        "to": wanted,
        "created_at": now,
        "state": "proposed",
        "text": "Предложение: «%s» вместо «%s»." % (wanted, current_human),
    }
    stored = _read_proposals(cfg)
    stored.append(proposal)
    _write_proposals(cfg, stored)
    out["proposal"] = proposal
    out["text"] = ("Предложение сохранено: «%s» вместо «%s». Расписание в Hermes не "
                   "изменено — правка только по вашему да, вручную."
                   % (wanted, current_human))
    return out


# --------------------------------------- создание, правка, удаление (W27-E7)

#: Шаги мастера: что проверять (``model|script|monitor``), когда, что считать
#: изменением, что делать. Крон создаётся только по «предпросмотр → да».
KIND_TITLES = {"model": "Модель по заданию", "script": "Скрипт без модели",
               "monitor": "Слежение за изменением"}
SCHEDULE_HINTS = ("30m", "every 2h", "0 9 * * 1-5", "*/15 * * * *", "every day at 9am")
PROMPT_MAX = 4000
NAME_MAX = 120
#: Сколько знаков задания показываем в карточке.
PROMPT_SHOW = 400

_CRON_FIELD_RE = re.compile(r"^[0-9*/,\-]+$")
_SHORT_STEP_RE = re.compile(r"^(\d+)\s*(s|m|h|d)$", re.I)
_EVERY_RE = re.compile(r"^every\s+(\d+)\s*(s|m|h|d)$", re.I)
_EVERY_AT_RE = re.compile(r"^every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)
_SCRIPT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


def normalize_schedule(value) -> str:
    """Расписание для `hermes cron`: cron-выражение, «30m», «every 2h», «every day at 9am»."""
    text = " ".join(str(value or "").split())
    if not text:
        raise DutiesError("Нужно расписание — например «0 9 * * 1-5» или «every 2h»",
                          400, "schedule_required",
                          {"field": "schedule", "examples": list(SCHEDULE_HINTS)})
    if len(text) > 80:
        raise DutiesError("Расписание слишком длинное", 400, "bad_request",
                          {"field": "schedule", "limit": 80})
    parts = text.split()
    if len(parts) == 5:
        for part in parts:
            if not _CRON_FIELD_RE.fullmatch(part) or len(part) > 20:
                raise DutiesError("Расписание не разобрать: «%s»" % text, 400, "bad_request",
                                  {"field": "schedule", "examples": list(SCHEDULE_HINTS)})
        return text
    if _SHORT_STEP_RE.fullmatch(text) or _EVERY_RE.fullmatch(text) or _EVERY_AT_RE.fullmatch(text):
        return text
    if text.lower().startswith("every "):
        if _INTERVAL_RE.fullmatch(text.lower()):
            return text
    raise DutiesError("Расписание не разобрать: «%s»" % text, 400, "bad_request",
                      {"field": "schedule", "examples": list(SCHEDULE_HINTS)})


def _kind(payload: dict) -> str:
    """Вид задания по полям мастера (по умолчанию — модель по заданию)."""
    raw = str(payload.get("kind") or "").strip().lower()
    if raw in KIND_TITLES:
        return raw
    if payload.get("monitor_script") or payload.get("monitor_url"):
        return "monitor"
    if payload.get("script") and payload.get("no_agent"):
        return "script"
    return "model"


def _script(value, field: str = "script") -> str:
    """Имя скрипта в каталоге скриптов Hermes: без путей и переходов вверх."""
    name = str(value or "").strip().replace("\\", "/")
    if not name:
        return ""
    if "/" in name or name.startswith(".") or ".." in name:
        raise DutiesError("Скрипт указывается именем файла из каталога скриптов Hermes "
                          "(например room-mail.sh), без путей", 400, "bad_request",
                          {"field": field, "value": name[:80]})
    if not _SCRIPT_RE.fullmatch(name):
        raise DutiesError("Недопустимое имя скрипта", 400, "bad_request",
                          {"field": field, "value": name[:80]})
    return name


def _workdir(cfg, room, value) -> str:
    """Рабочая папка задания: абсолютный путь без переходов вверх, внутри комнаты."""
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    if len(text) > 260:
        raise DutiesError("Путь рабочей папки слишком длинный", 400, "bad_request",
                          {"field": "workdir", "limit": 260})
    if ".." in text.replace("\\", "/").split("/"):
        raise DutiesError("В пути рабочей папки нельзя «..»", 400, "bad_request",
                          {"field": "workdir"})
    if not (re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("/")):
        raise DutiesError("Нужен полный путь рабочей папки", 400, "bad_request",
                          {"field": "workdir", "examples": ["C:/Work/Rooms/demo"]})
    try:                                  # реальная проверка прав — если конфиг настоящий
        import config as cfg_mod          # type: ignore
        if hasattr(cfg_mod, "path_allowed"):
            allowed = cfg_mod.path_allowed(cfg, text)
            if allowed is None:
                raise DutiesError("Рабочая папка вне разрешённых мест комнаты", 422,
                                  "path_denied", {"field": "workdir", "value": text[:200]})
    except DutiesError:
        raise
    except Exception:                     # noqa: BLE001 - проверка прав необязательна
        pass
    return text


def _prompt_with_contract(prompt: str) -> str:
    """Задание + контракт исполнителя (блок `результат`), если его ещё нет."""
    text = str(prompt or "").strip()
    marker = "```результат"
    if marker in text:
        return text
    try:
        from . import contract as contract_mod
    except ImportError:                   # pragma: no cover - плоский импорт
        try:
            import contract as contract_mod   # type: ignore
        except ImportError:
            return text
    block = contract_mod.duty_result_block()
    return (text + "\n\n" + block).strip() if text else block


def wizard_payload(cfg, room, payload: dict) -> dict:
    """Проверить поля мастера и собрать одно описание дежурства (без вызова CLI)."""
    data = payload if isinstance(payload, dict) else {}
    name = " ".join(str(data.get("title") or data.get("name") or "").split())[:NAME_MAX].strip()
    if not name:
        raise DutiesError("Нужно название дежурства", 400, "bad_request",
                          {"missing": ["title"]})
    kind = _kind(data)
    schedule = normalize_schedule(data.get("schedule"))
    prompt = str(data.get("prompt") or data.get("task") or "").strip()
    script = _script(data.get("script"), "script")
    monitor_script = _script(data.get("monitor_script"), "monitor_script")
    monitor_url = str(data.get("monitor_url") or "").strip()
    if monitor_url and not re.match(r"^https?://[^\s]+$", monitor_url):
        raise DutiesError("Нужен адрес вида https://…", 400, "bad_request",
                          {"field": "monitor_url"})
    if kind == "script" and not script:
        raise DutiesError("Для дежурства-скрипта нужен скрипт (например room-mail.sh)",
                          400, "bad_request", {"missing": ["script"]})
    if kind == "monitor" and not (monitor_script or monitor_url):
        raise DutiesError("Для слежения нужен скрипт или адрес страницы", 400, "bad_request",
                          {"missing": ["monitor_script|monitor_url"]})
    if kind in ("model", "monitor") and not prompt:
        raise DutiesError("Нужно задание — что именно проверять", 400, "bad_request",
                          {"missing": ["prompt"]})
    if len(prompt) > PROMPT_MAX:
        raise DutiesError("Задание слишком длинное: до %d знаков" % PROMPT_MAX, 400,
                          "bad_request", {"field": "prompt", "limit": PROMPT_MAX})
    skills = data.get("skills")
    skills = [str(one).strip()[:60] for one in skills if str(one or "").strip()] \
        if isinstance(skills, list) else []
    out = {
        "title": name,
        "kind": kind,
        "schedule": schedule,
        "schedule_human": schedule_human(schedule),
        "prompt": _prompt_with_contract(prompt) if prompt else "",
        "prompt_short": _trim(prompt, PROMPT_SHOW),
        "script": script,
        "monitor_script": monitor_script,
        "monitor_url": monitor_url,
        "workdir": _workdir(cfg, room, data.get("workdir")),
        "skills": skills[:6],
        "model": str(data.get("model") or "").strip()[:80],
        "provider": str(data.get("provider") or "").strip()[:40],
        "catch_up": data.get("catch_up") if isinstance(data.get("catch_up"), bool) else None,
        "continuity": bool(data.get("continuity", True)),
        "paused": bool(data.get("paused")),
        "room": str((room or {}).get("id") or ""),
        "deliver": "room:%s" % str((room or {}).get("id") or ""),
        "changed_rule": " ".join(str(data.get("changed_rule") or "").split())[:300],
        "on_change": str(data.get("on_change") or "inbox").strip().lower(),
        "level": str(data.get("level") or "propose").strip().lower(),
        "notify": list(data.get("notify") or []) if isinstance(data.get("notify"), list) else [],
        "source": str(data.get("source") or "manual").strip().lower(),
        "template": str(data.get("template") or "").strip()[:40],
    }
    if not out["room"]:
        raise DutiesError("Дежурство создаётся внутри комнаты", 400, "room_required")
    return out


def create_argv(ready: dict) -> list[str]:
    """Точная команда `hermes cron create …` для готового описания (без запуска)."""
    argv = ["cron", "create", ready["schedule"]]
    if ready.get("prompt") and ready["kind"] in ("model", "monitor"):
        argv.append(ready["prompt"])
    if ready.get("name") or ready.get("title"):
        argv += ["--name", ready.get("title") or ""]
    if ready.get("deliver"):
        argv += ["--deliver", ready["deliver"]]
    if ready["kind"] == "script":
        argv += ["--script", ready["script"], "--no-agent"]
    if ready.get("monitor_script"):
        argv += ["--monitor-script", ready["monitor_script"]]
    if ready.get("monitor_url"):
        argv += ["--monitor-url", ready["monitor_url"]]
    if ready.get("workdir"):
        argv += ["--workdir", ready["workdir"]]
    for skill in ready.get("skills") or []:
        argv += ["--skill", skill]
    if ready.get("catch_up") is True:
        argv += ["--catch-up", "on"]
    elif ready.get("catch_up") is False:
        argv += ["--catch-up", "off"]
    if ready.get("continuity"):
        argv.append("--continuity")
    if ready.get("model"):
        argv += ["--model", ready["model"]]
    if ready.get("provider"):
        argv += ["--provider", ready["provider"]]
    if ready.get("paused"):
        argv.append("--paused")
    return argv


def edit_argv(job_id: str, ready: dict, changed: set) -> list[str]:
    """`hermes cron edit <id> …` — только те поля, что человек действительно менял."""
    argv = ["cron", "edit", str(job_id)]
    if "schedule" in changed:
        argv += ["--schedule", ready["schedule"]]
    if "prompt" in changed:
        argv += ["--prompt", ready["prompt"]]
    if "title" in changed:
        argv += ["--name", ready["title"]]
    if "workdir" in changed:
        argv += ["--workdir", ready["workdir"]]
    if "monitor_script" in changed:
        argv += ["--monitor-script", ready["monitor_script"]]
    if "monitor_url" in changed:
        argv += ["--monitor-url", ready["monitor_url"]]
    if "model" in changed:                # W29-P5: выбор модели из каталога
        argv += ["--model", ready["model"]]
    if "provider" in changed:
        argv += ["--provider", ready["provider"]]
    return argv


def argv_display(argv) -> str:
    """Команда для человека: с кавычками вокруг аргументов с пробелами."""
    parts = [cli_bin()]
    for arg in argv or []:
        text = str(arg)
        parts.append('"%s"' % text.replace('"', "'") if (" " in text or "\n" in text) else text)
    return " ".join(parts)


def _changes(ready: dict) -> list[str]:
    """Что изменится — списком по-русски (для предпросмотра)."""
    out = ["В Hermes появится задание «%s»." % ready["title"],
           "Расписание: %s (%s)." % (ready["schedule_human"], ready["schedule"]),
           "Вид: %s." % KIND_TITLES.get(ready["kind"], ready["kind"])]
    if ready["kind"] == "script":
        out.append("Запускается скрипт %s без модели (дешево, без токенов)." % ready["script"])
    if ready.get("monitor_script") or ready.get("monitor_url"):
        what = ready.get("monitor_script") or ready.get("monitor_url")
        out.append("Слежение за «%s»: если ничего не изменилось, модель не запускается." % what)
        if ready.get("continuity"):
            out.append("Каждый запуск видит итог прошлого («что изменилось с прошлого раза»).")
    if ready.get("workdir"):
        out.append("Рабочая папка: %s." % ready["workdir"])
    out.append("Итог запуска придёт в комнату (deliver room:%s)." % ready["room"])
    out.append("При изменении: %s." % {"log": "только запись в журнал",
                                      "inbox": "карточка в «Входящие»",
                                      "delo": "предложение создать Дело",
                                      "step": "предложение шага в Деле",
                                      "question": "вопрос вам"}.get(ready["on_change"],
                                                                    ready["on_change"]))
    out.append("Уровень: %s." % {"self": "сам (обратимое внутри комнаты)",
                                 "propose": "предложить", "ask": "спросить"}
               .get(ready["level"], ready["level"]))
    return out


def _warnings(ready: dict) -> list[str]:
    """Честные предупреждения: что человеку стоит знать до «Создать»."""
    out: list[str] = []
    if not ready.get("changed_rule"):
        out.append("Не описано, что считать изменением: дежурство будет опираться на модель.")
    if ready["on_change"] in ("delo", "step") and ready["level"] == "self":
        out.append("Уровень «сам»: Дело создаётся без вопроса — но только внутри комнаты "
                   "и обратимо. Наружу ничего не уходит.")
    if ready["on_change"] in ("delo", "step") and ready["level"] != "self":
        out.append("Дело не создаётся само: сначала карточка «Принять».")
    if ready["kind"] == "model" and not ready.get("continuity"):
        out.append("Сверка с прошлым запуском выключена: возможны повторы.")
    out.append("Кроны запускает Hermes: пока Hermes выключен, запусков не будет.")
    return out


def preview(cfg, room, payload: dict) -> dict:
    """Предпросмотр создания: точная команда, что изменится, чего ждать."""
    ready = wizard_payload(cfg, room, payload)
    argv = create_argv(ready)
    rule_view = None
    try:
        from . import duty_rules as rules_mod
    except ImportError:                   # pragma: no cover
        try:
            import duty_rules as rules_mod   # type: ignore
        except ImportError:
            rules_mod = None
    if rules_mod is not None:
        try:
            rule_view = rules_mod.check_rule(ready)
        except Exception:                 # noqa: BLE001 - правило проверит PATCH
            rule_view = None
    return {"ok": True, "preview": True, "applied": False,
            "command": argv_display(argv), "argv": argv,
            "duty": {key: ready[key] for key in ("title", "kind", "schedule", "schedule_human",
                                                 "prompt_short", "script", "monitor_script",
                                                 "monitor_url", "workdir", "deliver",
                                                 "continuity", "catch_up", "paused")},
            "rule": {"changed_rule": ready["changed_rule"], "on_change": ready["on_change"],
                     "level": ready["level"], "notify": ready["notify"],
                     "source": ready["source"], "template": ready["template"]},
            "changes": _changes(ready), "warnings": _warnings(ready),
            "next": "Чтобы создать, повторите с preview:false и confirm:true."}


def _new_job_id(cfg, before: set, out: str) -> str:
    """Номер созданного задания: из вывода CLI («Created job: …») или по разнице `jobs.json`."""
    found = re.search(r"Created job:\s*([A-Za-z0-9_.-]{1,64})", str(out or ""))
    if found:
        return found.group(1)
    fresh = sorted(job_ids(cfg) - set(before or set()))
    return fresh[0] if len(fresh) == 1 else ""


def _bind(cfg, room, job_id, on: bool = True) -> list[str]:
    """Привязать/отвязать дежурство к комнате (только реестр комнат)."""
    room_dict = room if isinstance(room, dict) else {}
    current = [str(one) for one in (room_dict.get("duties") or [])]
    wanted = list(current)
    if on and job_id not in wanted:
        wanted.append(job_id)
    if not on:
        wanted = [one for one in wanted if one != job_id]
    if not room_dict.get("id"):
        return wanted
    try:
        updated = rooms_mod.update_room(cfg, str(room_dict["id"]), {"duties": wanted}) or {}
    except Exception as exc:              # noqa: BLE001 - ошибку реестра отдаём как свою
        raise DutiesError(str(getattr(exc, "message", None) or exc
                              or "Не удалось сохранить привязку"), 400, "bad_request") from exc
    return [str(one) for one in (updated.get("duties") or wanted)]


def _call_cli_ok(cfg) -> bool:
    """Можно ли звать CLI (``False`` в пробном режиме `TEAMROOM_DUTIES_DRY`)."""
    return not dry_run_on(cfg)


def create(cfg, room, payload: dict, *, preview_only=False, confirm=False, runner=None,
           now: int | None = None) -> dict:
    """``POST /api/rooms/{id}/duties`` — создать дежурство через `hermes cron create`.

    * ``preview_only=True`` — только предпросмотр (ничего не меняется);
    * иначе нужен ``confirm=True``: без него ``409 confirm_required`` с предпросмотром.
    """
    now = int(now if now is not None else time.time())
    view = preview(cfg, room, payload)
    ready = wizard_payload(cfg, room, payload)
    if preview_only:
        return view
    if not confirm:
        raise DutiesError("Дежурство создаётся в Hermes — подтвердите создание",
                          409, "confirm_required",
                          {"action": "create", "preview": view["duty"],
                           "command": view["command"]})
    argv = create_argv(ready)
    out = {"ok": True, "preview": False, "applied": False, "dry": False,
           "command": argv_display(argv), "job_id": None, "room": ready["room"],
           "changes": view["changes"], "warnings": view["warnings"], "generated_at": now}
    before = job_ids(cfg)
    if dry_run_on(cfg):
        out["dry"] = True
        out["text"] = "Пробный режим: «%s» не выполнялась." % out["command"]
        return out
    result = (runner or runner_for(cfg))(argv)
    code = int(result.get("code") or 0)
    if code != 0:
        raise DutiesError("Hermes не создал дежурство: «%s» вернула код %d"
                          % (out["command"], code), 502, "cli_failed",
                          {"command": out["command"], "code": code,
                           "err": str(result.get("err") or "")[:300],
                           "out": str(result.get("out") or "")[:300]})
    job_id = _new_job_id(cfg, before, result.get("out") or "")
    if not job_id:
        out["text"] = ("Hermes ответил, но номер задания не назвал — откройте раздел "
                       "«Дежурства» и привяжите его к комнате вручную.")
        out["warnings"] = list(out["warnings"]) + [out["text"]]
        return out
    out["applied"] = True
    out["job_id"] = job_id
    out["text"] = "Дежурство создано: «%s»." % ready["title"]
    try:
        out["duties"] = _bind(cfg, room, job_id, True)
    except DutiesError as exc:
        out["warnings"] = list(out["warnings"]) + [exc.message]
    try:
        from . import duty_rules as rules_mod
    except ImportError:                   # pragma: no cover
        rules_mod = None          # type: ignore
    if rules_mod is not None:
        try:
            out["rule"] = rules_mod.public_rule(rules_mod.set_rule(
                cfg, ready["room"], job_id,
                {"changed_rule": ready["changed_rule"], "on_change": ready["on_change"],
                 "level": ready["level"], "notify": ready["notify"],
                 "source": ready["source"], "template": ready["template"]}))
        except Exception as exc:          # noqa: BLE001 - правило не должно терять крон
            out["warnings"] = list(out["warnings"]) + [
                "Задание создано, а правило не сохранилось: %s" % str(exc)[:160]]
    out["job"] = _item_of(cfg, job_id)
    return out


def _ready_from_job(cfg, job: dict, room_id: str) -> dict:
    """Описание дежурства из записи `jobs.json` (для правки и для корзины)."""
    schedule = str(job.get("schedule_display") or "")
    if not schedule and isinstance(job.get("schedule"), dict):
        schedule = str(job["schedule"].get("display") or job["schedule"].get("expr") or "")
    prompt = str(job.get("prompt") or "")
    return {
        "title": str(job.get("name") or job.get("id") or ""),
        "kind": ("script" if job.get("no_agent") else
                 "monitor" if (job.get("monitor_script") or job.get("monitor_url")) else "model"),
        "schedule": schedule or "0 9 * * *",
        "schedule_human": schedule_human(schedule),
        "prompt": prompt,
        "prompt_short": _trim(prompt, PROMPT_SHOW),
        "script": str(job.get("script") or ""),
        "monitor_script": str(job.get("monitor_script") or ""),
        "monitor_url": str(job.get("monitor_url") or ""),
        "workdir": str(job.get("workdir") or ""),
        "skills": [str(one) for one in (job.get("skills") or []) if str(one or "").strip()],
        "model": str(job.get("model") or ""),
        "provider": str(job.get("provider") or ""),
        "catch_up": job.get("catch_up") if isinstance(job.get("catch_up"), bool) else None,
        "continuity": bool(job.get("continuity")),
        "paused": not bool(job.get("enabled")),
        "room": str(room_id or ""),
        "deliver": str(job.get("deliver") or ""),
        "changed_rule": "", "on_change": "inbox", "level": "propose", "notify": [],
        "source": "manual", "template": "",
    }


#: Какие поля `PATCH` меняет через Hermes (остальные — только внутреннее правило).
PATCH_CLI_FIELDS = ("schedule", "prompt", "title", "workdir", "monitor_script", "monitor_url",
                    # W29-P5: модель выбирается в мастере — её смена тоже уходит в Hermes.
                    "model", "provider")
#: Какие поля меняют только правило (Hermes не трогаем).
PATCH_RULE_FIELDS = ("changed_rule", "on_change", "level", "notify", "source", "template")


def patch(cfg, job_id, room, payload: dict, *, preview_only=False, confirm=False,
          runner=None, now: int | None = None) -> dict:
    """``PATCH /api/duties/{job_id}`` — расписание/задание/правило/пауза.

    Правка через Hermes (`hermes cron edit|pause|resume`) — только по подтверждению.
    Правка правила — внутреннее состояние: применяется сразу и обратимо.
    """
    now = int(now if now is not None else time.time())
    data = payload if isinstance(payload, dict) else {}
    job = _job_by_id(cfg, job_id)
    room_id = str((room or {}).get("id") or "")
    ready = _ready_from_job(cfg, job, room_id)
    changed: set = set()
    for key in PATCH_CLI_FIELDS:
        if key not in data:
            continue
        value = data.get(key)
        if key == "schedule":
            value = normalize_schedule(value)
        elif key == "title":
            value = " ".join(str(value or "").split())[:NAME_MAX]
        elif key == "prompt":
            value = _prompt_with_contract(str(value or ""))
        elif key == "workdir":
            value = _workdir(cfg, room, value)
        elif key in ("monitor_script",):
            value = _script(value, key)
        elif key in ("model", "provider"):
            value = str(value or "").strip()[:80]
        elif key == "monitor_url":
            value = str(value or "").strip()
            if value and not re.match(r"^https?://[^\s]+$", value):
                raise DutiesError("Нужен адрес вида https://…", 400, "bad_request",
                                  {"field": "monitor_url"})
        if str(value or "") != str(ready.get(key) or ""):
            ready[key] = value
            changed.add(key)
    pause = data.get("pause")
    pause_wanted = None
    if isinstance(pause, bool):
        pause_wanted = pause if pause != bool(ready.get("paused")) else None
    rule_changed = any(key in data for key in PATCH_RULE_FIELDS)
    wants_resume_now = data.get("run_now") is True
    if not changed and pause_wanted is None and not rule_changed and not wants_resume_now:
        return {"ok": True, "applied": False, "job_id": str(job_id), "generated_at": now,
                "text": "Менять нечего: поля те же.", "changes": []}
    plan: list[str] = []
    argv: list[str] = []
    if changed:
        argv = edit_argv(job_id, ready, changed)
        plan.append("Изменится задание в Hermes: %s." % ", ".join(sorted(changed)))
    if pause_wanted is not None:
        action = "pause" if pause_wanted else "resume"
        argv = cli_command(action, job_id) if not argv else argv
        if changed:                       # edit и pause — два вызова, не смешиваем
            plan.append("Отдельно: %s." % ("пауза" if pause_wanted else "возобновление"))
        else:
            plan.append("Задание будет %s." % ("на паузе" if pause_wanted else "снова по расписанию"))
    if wants_resume_now and not changed and pause_wanted is None:
        argv = cli_command("run", job_id)
        plan.append("Задание запустится вне расписания.")
    view = {"ok": True, "applied": False, "preview": bool(preview_only), "job_id": str(job_id),
            "command": argv_display(argv) if argv else None, "argv": argv,
            "changes": plan, "rule_changed": rule_changed,
            "duty": {key: ready.get(key) for key in ("title", "kind", "schedule",
                                                     "schedule_human", "prompt_short",
                                                     "workdir", "continuity", "paused")},
            "warnings": [], "generated_at": now}
    if preview_only:
        view["next"] = "Чтобы применить, повторите с confirm:true."
        return view
    if not argv and not rule_changed:
        view["text"] = "Менять нечего."
        return view
    if argv and not confirm:
        raise DutiesError("Правка дежурства меняет Hermes — подтвердите", 409,
                          "confirm_required",
                          {"action": "patch", "job_id": str(job_id),
                           "command": view["command"], "changes": plan})
    if not argv:                          # только правило: Hermes не трогаем
        view["applied"] = True
    elif not _call_cli_ok(cfg):           # пробный режим: печатаем команду, не выполняем
        view["dry"] = True
        view["text"] = "Пробный режим: «%s» не выполнялась." % view["command"]
        return view
    result = (runner or runner_for(cfg))(argv) if argv else {"code": 0}
    code = int(result.get("code") or 0)
    if code != 0:
        raise DutiesError("Hermes не применил правку: «%s» вернула код %d"
                          % (view["command"], code), 502, "cli_failed",
                          {"command": view["command"], "code": code,
                           "err": str(result.get("err") or "")[:300]})
    if changed and pause_wanted is not None:      # второй вызов отдельно
        call = cli_command("pause" if pause_wanted else "resume", job_id)
        result = (runner or runner_for(cfg))(call)
        if int(result.get("code") or 0) != 0:
            raise DutiesError("Правка применена, а пауза — нет: код %s"
                              % result.get("code"), 502, "cli_failed",
                              {"command": argv_display(call)})
    view["applied"] = bool(argv)
    if rule_changed:
        try:
            from . import duty_rules as rules_mod
        except ImportError:               # pragma: no cover
            rules_mod = None          # type: ignore
        if rules_mod is not None:
            rule_now = rules_mod.rule(cfg, room_id, job_id) or {}
            merged = dict(rule_now)
            merged.update({key: data[key] for key in PATCH_RULE_FIELDS if key in data})
            view["rule"] = rules_mod.public_rule(
                rules_mod.set_rule(cfg, room_id, job_id, merged, base=rule_now or None))
            view["applied"] = True
    view["text"] = "Правка применена." if view["applied"] else "Ничего не изменилось."
    view["job"] = _item_of(cfg, job_id)
    return view


def remove(cfg, job_id, room, *, confirm=False, runner=None, now: int | None = None) -> dict:
    """``DELETE /api/duties/{job_id}`` — убрать крон и положить дежурство в корзину."""
    now = int(now if now is not None else time.time())
    job = _job_by_id(cfg, job_id)
    room_id = str((room or {}).get("id") or "")
    item = _item_of(cfg, job_id) or {"job_id": str(job_id),
                                     "name": str(job.get("name") or job_id)}
    ready = _ready_from_job(cfg, job, room_id)
    command = argv_display(["cron", "remove", str(job_id)])
    if not confirm:
        raise DutiesError("Удаление дежурства останавливает его в Hermes — подтвердите", 409,
                          "confirm_required",
                          {"action": "remove", "job_id": str(job_id), "command": command,
                           "note": "Дежурство уедет в корзину на 30 дней — его можно вернуть."})
    if dry_run_on(cfg):
        return {"ok": True, "applied": False, "dry": True, "job_id": str(job_id),
                "command": command, "text": "Пробный режим: «%s» не выполнялась." % command,
                "generated_at": now}
    result = (runner or runner_for(cfg))(cli_command("remove", job_id))
    code = int(result.get("code") or 0)
    if code != 0:
        raise DutiesError("Hermes не удалил дежурство: код %d" % code, 502, "cli_failed",
                          {"command": command, "code": code,
                           "err": str(result.get("err") or "")[:300]})
    out = {"ok": True, "applied": True, "job_id": str(job_id), "command": command,
           "generated_at": now}
    try:
        out["duties"] = _bind(cfg, room, job_id, False)
    except DutiesError as exc:
        out["warnings"] = [exc.message]
    try:
        from . import duty_rules as rules_mod
    except ImportError:                   # pragma: no cover
        rules_mod = None          # type: ignore
    if rules_mod is not None:
        try:
            row = rules_mod.trash_add(cfg, room_id, job_id, item, ready, now)
            out["trash"] = {"job_id": str(job_id), "delete_after_text": row["delete_after_text"],
                            "days": rules_mod.TRASH_DAYS}
        except Exception as exc:          # noqa: BLE001 - корзина не должна мешать удалению
            out["warnings"] = list(out.get("warnings") or []) + [
                "Задание удалено, а в корзину не попало: %s" % str(exc)[:160]]
    out["text"] = ("Дежурство удалено. Вернуть можно из корзины в течение %d дней."
                   % 30)
    return out


def restore(cfg, room, job_id, *, confirm=False, runner=None, now: int | None = None) -> dict:
    """``POST /api/duties/{job_id}/restore`` — вернуть дежурство из корзины."""
    now = int(now if now is not None else time.time())
    try:
        from . import duty_rules as rules_mod
    except ImportError:                   # pragma: no cover
        rules_mod = None          # type: ignore
    if rules_mod is None:
        raise DutiesError("Корзина недоступна", 503, "unavailable")
    row = rules_mod.trash_take(cfg, room_id_of_room(room), job_id, now)
    if row is None:
        raise DutiesError("В корзине такого дежурства нет", 404, "not_found",
                          {"job_id": str(job_id)[:64]})
    ready = dict(row.get("payload") or {})
    if not ready.get("title"):
        ready = _ready_from_job(cfg, {"id": job_id, "name": row.get("name")}, 
                                room_id_of_room(room))
    ready["room"] = room_id_of_room(room)
    ready["deliver"] = "room:%s" % ready["room"]
    command = argv_display(create_argv(ready))
    if not confirm:
        # вернуть в корзину — чтобы не потерять запись, если человек передумал
        rules_mod.trash_add(cfg, ready["room"], job_id, row.get("item") or {}, ready, now,
                                rule=row.get("rule"))
        raise DutiesError("Возврат создаёт задание в Hermes заново — подтвердите", 409,
                          "confirm_required",
                          {"action": "restore", "job_id": str(job_id), "command": command})
    before = job_ids(cfg)
    if dry_run_on(cfg):
        rules_mod.trash_add(cfg, ready["room"], job_id, row.get("item") or {}, ready, now,
                                rule=row.get("rule"))
        return {"ok": True, "applied": False, "dry": True, "command": command,
                "text": "Пробный режим: «%s» не выполнялась." % command, "generated_at": now}
    result = (runner or runner_for(cfg))(create_argv(ready))
    if int(result.get("code") or 0) != 0:
        rules_mod.trash_add(cfg, ready["room"], job_id, row.get("item") or {}, ready, now,
                                rule=row.get("rule"))
        raise DutiesError("Hermes не вернул дежурство: код %s" % result.get("code"), 502,
                          "cli_failed", {"command": command,
                                         "err": str(result.get("err") or "")[:300]})
    new_id = _new_job_id(cfg, before, result.get("out") or "") or job_id
    out = {"ok": True, "applied": True, "job_id": new_id, "old_job_id": str(job_id),
           "command": command, "generated_at": now,
           "text": "Дежурство возвращено и снова идёт по расписанию."}
    try:
        out["duties"] = _bind(cfg, room, new_id, True)
    except DutiesError as exc:
        out["warnings"] = [exc.message]
    stored = row.get("rule") if isinstance(row.get("rule"), dict) else {}
    if stored:
        try:
            rules_mod.set_rule(cfg, ready["room"], new_id, stored)
        except Exception:                 # noqa: BLE001 - правило восстановим по PATCH
            pass
    out["job"] = _item_of(cfg, new_id)
    return out


def room_id_of_room(room) -> str:
    """id комнаты из словаря или строки."""
    if isinstance(room, dict):
        return str(room.get("id") or "")
    return str(room or "")


def dry_run(cfg, job_id) -> dict:
    """``POST /api/duties/{job_id}/dry_run`` — «покажи, что бы нашла», без запуска.

    Ничего не запускаем и ничего не пишем: у задания могут быть побочные действия
    (файлы, отправка), а «пробный прогон» не должен их делать. Отдаём, что именно
    проверяется, что нашлось в прошлый раз и как запустить по-настоящему, если нужно.
    """
    job = _job_by_id(cfg, job_id)
    last = excerpt(cfg, job_id)
    result = None
    files = output_files(cfg, job_id)
    if files:
        try:
            text = files[-1].read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        try:
            from . import duty_rules as rules_mod
        except ImportError:               # pragma: no cover
            rules_mod = None          # type: ignore
        if rules_mod is not None and text:
            result = rules_mod.parse_result(text)
    return {"ok": True, "job_id": str(job_id), "ran": False,
            "side_effects": "none",
            "name": str(job.get("name") or job_id),
            "schedule_human": schedule_human(job.get("schedule_display"), job.get("schedule")),
            "checks": {"kind": ("script" if job.get("no_agent") else
                                "monitor" if (job.get("monitor_script") or job.get("monitor_url"))
                                else "model"),
                       "prompt": _trim(str(job.get("prompt") or ""), PROMPT_SHOW),
                       "script": str(job.get("script") or ""),
                       "monitor_script": str(job.get("monitor_script") or ""),
                       "monitor_url": str(job.get("monitor_url") or ""),
                       "workdir": str(job.get("workdir") or "")},
            "last_result": result,
            "last_excerpt": last,
            "command": argv_display(cli_command("run", job_id)),
            "text": ("Пробный прогон не запускается: у задания могут быть побочные действия. "
                     "Вот что оно проверяет и что нашло в прошлый раз. Запустить по-настоящему — "
                     "кнопкой «Запустить сейчас».")}


__all__ = ["ACTION_LABELS", "ACTION_TIMEOUT_S", "ACTIONS", "CLI_ACTIONS", "DutiesError",
           "EXCERPT_MAX", "KIND_TITLES", "MAX_BOUND", "MISSED_WINDOW_S", "NAME_MAX",
           "PATCH_CLI_FIELDS", "PATCH_RULE_FIELDS", "PROMPT_MAX", "PROPOSALS_MAX", "RECENT_MAX",
           "SCHEDULE_HINTS", "STALE_GRACE_S", "TOKEN_MARKS", "WEEK_DAY_JOBS", "WEEK_FULL",
           "WEEK_LABELS",
           "act", "argv_display", "bound_rooms", "check_duties", "cli_bin", "cli_command",
           "cli_display", "create", "create_argv", "dry_run", "dry_run_on", "edit_argv",
           "excerpt", "executions", "home_of", "interval_seconds", "items",
           "job_ids", "job_ids_of_hermes", "jobs", "mask", "normalize_schedule", "output_files",
           "patch", "plural", "preview", "problems", "proposals", "proposals_path", "remove",
           "reset_cli_runner", "restore", "room_id_of_room", "runner_for",
           "schedule_human", "set_cli_runner", "set_room_duties", "token_hits", "week",
           "wizard_payload"]
