"""Здоровье интеграций: короткий список проверок для экрана «Сегодня».

Контракт — `docs/WAVE24-RULES.md` §3.3, описание — `docs/API.md` §W24-DUTIES::

    { "generated_at": int, "overall": "ok"|"warn"|"fail",
      "checks": [ {"id", "title", "status", "detail", "since", "hint"} ] }

Правила:

* **Никаких обращений во внешнюю сеть.** Только файлы Hermes (на чтение) и
  localhost-порты, которые уже настроены у пользователя.
* Проверка не имеет права уронить отчёт: любое исключение внутри проверки — это
  `unknown` с честным текстом, остальные проверки всё равно считаются.
* Набор расширяемый: новая проверка = функция с `@register`, она сразу попадает
  в ответ (тест `tests/test_health.py` это стережёт).
* Названия проверок общие — площадка не знает, чем занимается пользователь.

    python -m unittest tests.test_health -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:                                     # import server.health
    from . import duties as duties_mod
    from . import rooms as rooms_mod
    from .http_util import mask_secrets
except ImportError:                      # python server/app.py (плоский импорт)
    import duties as duties_mod          # type: ignore
    import rooms as rooms_mod            # type: ignore
    from http_util import mask_secrets   # type: ignore

STATUSES = ("ok", "warn", "fail", "unknown")
#: Свежесть записи шлюза, после которой состояние считаем устаревшим.
GATEWAY_FRESH_S = 300
#: Порт соседа отвечает/не отвечает — ждём не больше этого.
PEER_TIMEOUT_S = 0.8
#: Возраст самой свежей резервной копии, после которого — предупреждение.
BACKUP_WARN_DAYS = 14
#: Окно поиска ошибок доступа к календарю в выводах дежурств.
TOKEN_ACTIVE_DAYS = 3
#: Порог свободного места на диске (доля).
DISK_WARN = 0.10
DISK_FAIL = 0.03


def backup_candidates() -> list:
    """Где искать резервные копии: `TEAMROOM_BACKUPS`, затем `<данные приложения>/Backups`,
    затем папка `Backups` рядом с найденным Vault (так обычно и лежит). Без путей автора."""
    out = []
    env = os.environ.get("TEAMROOM_BACKUPS")
    if env:
        return [Path(env).expanduser()]
    try:
        from . import config as _config
        out.append(_config.teamroom_home() / "Backups")
        root = _config.detected_vault_root()
        if root is not None:
            out.append(Path(root).parent / "Backups")
    except Exception:  # noqa: BLE001 — проверка здоровья не должна падать из-за настроек
        pass
    return out

#: Реестр проверок — порядок в ответе совпадает с порядком регистрации.
CHECKS: list = []


def register(fn):
    """Зарегистрировать проверку: ``fn(cfg) -> {id, title, status, detail, since, hint}``."""
    if fn not in CHECKS:
        CHECKS.append(fn)
    return fn


def check(check_id: str, title: str, status: str, detail: str = "", since=None, hint=None) -> dict:
    """Пункт проверки в форме контракта (статус вне списка → ``unknown``)."""
    status = str(status or "").strip().lower()
    return {
        "id": str(check_id),
        "title": str(title),
        "status": status if status in STATUSES else "unknown",
        "detail": _one_line(detail, 300),
        "since": int(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else None,
        "hint": (_one_line(hint, 240) or None) if hint else None,
    }


def _one_line(text, limit: int = 300) -> str:
    line = re.sub(r"\s+", " ", str(text or "")).strip()
    line = mask_secrets(line)
    if len(line) <= limit:
        return line
    return line[:limit].rstrip() + "…"


def _home(cfg) -> Path:
    """Домашний каталог Hermes (тот же, что у дежурств) — только чтение."""
    return duties_mod.home_of(cfg)


def _data_dir(cfg) -> Path:
    raw = getattr(cfg, "data_dir", None) or os.environ.get("TEAMROOM_DATA") or "."
    return Path(str(raw))


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh, strict=False)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _age_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "меньше минуты назад"
    if seconds < 3600:
        return "%d %s назад" % (seconds // 60, _plural(seconds // 60, "минуту", "минуты", "минут"))
    if seconds < 86400 * 2:
        return "%d %s назад" % (seconds // 3600, _plural(seconds // 3600, "час", "часа", "часов"))
    return "%d %s назад" % (seconds // 86400, _plural(seconds // 86400, "день", "дня", "дней"))


def _plural(count: int, one: str, few: str, many: str) -> str:
    num = abs(int(count or 0))
    if num % 10 == 1 and num % 100 != 11:
        return one
    if num % 10 in (2, 3, 4) and num % 100 not in (12, 13, 14):
        return few
    return many


def _date_text(stamp) -> str:
    try:
        return datetime.fromtimestamp(int(stamp)).strftime("%d.%m.%Y")
    except (TypeError, ValueError, OSError):
        return ""


# --------------------------------------------------------------- проверки

@register
def gateway(cfg) -> dict:
    """Шлюз Hermes: `gateway_state.json` — состояние, свежесть записи, живой ли pid."""
    doc = _read_json(_home(cfg) / "gateway_state.json")
    if not doc:
        return check("gateway", "Шлюз запуска", "unknown",
                     "состояние шлюза не найдено",
                     hint="Если работа идёт без приложения Hermes, проверка не нужна")
    updated = _ts(doc.get("updated_at"))
    state = str(doc.get("gateway_state") or "unknown")
    now = int(time.time())
    age = (now - updated) if updated else None
    if state != "running":
        reason = str(doc.get("exit_reason") or "").strip()
        return check("gateway", "Шлюз запуска", "fail",
                     "шлюз не работает" + ((": " + reason) if reason else ""),
                     since=updated,
                     hint="Запустите приложение Hermes — без шлюза задания не раздаются")
    if age is not None and age > GATEWAY_FRESH_S:
        return check("gateway", "Шлюз запуска", "warn",
                     "запись состояния шлюза не обновлялась " + _age_text(age),
                     since=updated,
                     hint="Похоже, шлюз завис — проверьте приложение Hermes")
    detail = "работает" + ((", последняя запись " + _age_text(age)) if age is not None else "")
    return check("gateway", "Шлюз запуска", "ok", detail, since=updated)


@register
def dispatcher(cfg) -> dict:
    """Раздача заданий: та же диагностика, что в «Проверке системы»."""
    try:
        from . import engine_health as health_mod
    except ImportError:                  # pragma: no cover
        import engine_health as health_mod  # type: ignore
    data = health_mod.dispatcher_status(cfg) or {}
    state = str(data.get("state") or "unknown")
    warning = str(data.get("warning") or "")
    if data.get("running"):
        return check("dispatcher", "Раздача заданий", "ok",
                     "очередь обрабатывается", since=data.get("updated_at"))
    if state == "stopped":
        return check("dispatcher", "Раздача заданий", "fail",
                     warning or "раздача заданий не работает", since=data.get("updated_at"),
                     hint="Запустите приложение Hermes")
    return check("dispatcher", "Раздача заданий", "warn",
                 warning or "состояние раздачи заданий неясно", since=data.get("updated_at"),
                 hint="Откройте «Проверку системы» и посмотрите подробности")


@register
def peer_link(cfg) -> dict:
    """Связь с соседними приложениями: localhost-порты из настроек Hermes (`bot_peers`)."""
    peers = _peers(cfg)
    if not peers:
        return check("peer_link", "Связь с соседним приложением", "unknown",
                     "адреса не указаны в настройках",
                     hint="Укажите адрес соседнего приложения, если связь нужна")
    dead, alive = [], 0
    for name, url in peers:
        host, port = _split_url(url)
        if not host or not port:
            dead.append("%s: адрес непонятен" % name)
            continue
        if _port_open(host, port):
            alive += 1
        else:
            dead.append("%s не отвечает (%s:%s)" % (name, host, port))
    if not dead:
        return check("peer_link", "Связь с соседним приложением", "ok",
                     "связь есть: отвечает %d из %d" % (alive, len(peers)))
    status = "warn" if alive else "fail"
    return check("peer_link", "Связь с соседним приложением", status,
                 "; ".join(dead[:3]),
                 hint="Проверьте, запущено ли соседнее приложение и его туннель")


def _peers_plain(raw: str) -> list[tuple]:
    """Запасной разбор блока `bot_peers:` без PyYAML (имя: / url: адрес, отступами)."""
    out: list[tuple] = []
    in_block, name = False, None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        if indent == 0:
            in_block = text.startswith("bot_peers:")
            name = None
            continue
        if not in_block:
            continue
        if text.startswith("- "):
            text = text[2:].strip()
        if text.startswith("url:") and name:
            url = text[4:].strip().strip("'\"")
            if url:
                out.append((name, url))
            name = None
        elif text.endswith(":") and not text.startswith("url"):
            name = text[:-1].strip().strip("'\"")
    return out


def _peers(cfg) -> list[tuple]:
    """`bot_peers` из `config.yaml` Hermes: ``[(имя, url)]`` (пусто — не настроено)."""
    path = _home(cfg) / "config.yaml"
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    try:
        import yaml
        doc = yaml.safe_load(raw) or {}
    except ImportError:                  # PyYAML есть не везде: читаем нужный блок сами
        return _peers_plain(raw)
    except Exception:                    # noqa: BLE001 — настройки могут быть нечитаемы
        return []
    peers = doc.get("bot_peers") if isinstance(doc, dict) else None
    out: list[tuple] = []
    if isinstance(peers, dict):
        for name, value in peers.items():
            url = value.get("url") if isinstance(value, dict) else value
            if isinstance(url, str) and url.strip():
                out.append((str(name), url.strip()))
    elif isinstance(peers, list):
        for value in peers:
            if isinstance(value, dict):
                url = value.get("url")
                if isinstance(url, str) and url.strip():
                    out.append((str(value.get("name") or "сосед"), url.strip()))
    return out


def _split_url(url: str):
    try:
        parsed = urlparse(str(url))
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, AttributeError):
        return "", 0
    return host, int(port or 0)


def _port_open(host: str, port: int) -> bool:
    """Порт localhost отвечает? (внешние адреса не проверяем — сети нет)"""
    if host not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=PEER_TIMEOUT_S):
            return True
    except (OSError, ValueError):
        return False


@register
def calendar_token(cfg) -> dict:
    """Доступ к календарю: файл токена (`expiry`) и ошибки обновления в выводах дежурств."""
    path = _home(cfg) / "google_token.json"
    doc = _read_json(path)
    now = int(time.time())
    hits = []
    try:
        hits = [hit for hit in duties_mod.token_hits(cfg, days=duties_mod.TOKEN_SCAN_DAYS)
                if hit.get("at")]
    except Exception:                    # noqa: BLE001 — без выводов проверяем хотя бы файл
        hits = []
    active = [hit for hit in hits if hit["at"] >= now - TOKEN_ACTIVE_DAYS * 86400]
    first_active = active[0]["at"] if active else None
    if not doc:
        if active:
            return check("calendar_token", "Доступ к календарю", "fail",
                         "в выводах дежурств ошибка доступа к календарю с "
                         + (_date_text(first_active) or "недавно"),
                         since=first_active,
                         hint="Нужна повторная авторизация Google")
        return check("calendar_token", "Доступ к календарю", "unknown",
                     "календарь не подключён",
                     hint="Подключите календарь, если он нужен дежурствам")
    expiry = _ts(doc.get("expiry"))
    if active or (expiry is not None and expiry < now):
        if expiry is not None and expiry < now:
            # Срок в самом файле — самый точный ответ «с какого дня»: доступ истёк.
            detail = "доступ истёк " + _date_text(expiry)
            if active:
                detail += " — дежурства сообщают об отказе доступа"
            since = expiry
        else:
            detail = ("дежурства сообщают об отказе доступа к календарю с "
                      + (_date_text(first_active) or "недавно"))
            since = first_active
        return check("calendar_token", "Доступ к календарю", "fail", detail, since=since,
                     hint="Нужна повторная авторизация Google")
    if expiry is None:
        return check("calendar_token", "Доступ к календарю", "warn",
                     "срок действия доступа не указан",
                     hint="Переавторизуйте календарь, чтобы срок был известен")
    left = expiry - now
    if left < 86400:
        return check("calendar_token", "Доступ к календарю", "warn",
                     "доступ истекает " + _date_text(expiry),
                     since=expiry, hint="Обновите авторизацию заранее")
    return check("calendar_token", "Доступ к календарю", "ok",
                 "доступ действует до " + _date_text(expiry), since=expiry)


@register
def duties(cfg) -> dict:
    """Дежурства: есть ли задания с замечаниями (ошибка, отставание, не запускалось)."""
    if not (_home(cfg) / "cron" / "jobs.json").is_file():
        return check("duties", "Дежурства", "unknown", "список дежурств не найден",
                     hint="Дежурства появятся, когда Hermes сохранит хотя бы одно")
    try:
        bad = duties_mod.problems(cfg)
    except Exception as exc:             # noqa: BLE001
        return check("duties", "Дежурства", "unknown",
                     "список дежурств не читается: " + _one_line(exc, 120))
    if not bad:
        return check("duties", "Дежурства", "ok", "все дежурства без замечаний")
    word = _plural(len(bad), "дежурство", "дежурства", "дежурств")
    return check("duties", "Дежурства", "warn",
                 "%d %s с замечаниями" % (len(bad), word),
                 since=None,
                 hint="Откройте «Дежурства» ниже и посмотрите причину")


@register
def backups(cfg) -> dict:
    """Резервные копии: возраст самой свежей папки (см. `backup_candidates`)."""
    entries = []
    for root in backup_candidates():
        try:
            entries = [p for p in root.iterdir() if p.is_dir() or p.is_file()]
            break
        except OSError:
            continue
    else:
        entries = None
    if entries is None:
        return check("backups", "Резервные копии", "unknown",
                     "папка копий не найдена",
                     hint="Если копии делаются в другом месте, укажите его в настройках")
    stamps = []
    for path in entries:
        if path.name.startswith(".") or path.name == "__pycache__":
            continue
        try:
            stamps.append(int(path.stat().st_mtime))
        except OSError:
            continue
    if not stamps:
        return check("backups", "Резервные копии", "warn", "копий не найдено",
                     hint="Проверьте, что копирование включено")
    newest = max(stamps)
    age_days = (int(time.time()) - newest) // 86400
    if age_days > BACKUP_WARN_DAYS:
        return check("backups", "Резервные копии", "warn",
                     "самая свежая копия — %d %s назад"
                     % (age_days, _plural(age_days, "день", "дня", "дней")),
                     since=newest, hint="Проверьте, что копирование включено")
    return check("backups", "Резервные копии", "ok",
                 "самая свежая копия — " + _age_text(int(time.time()) - newest), since=newest)


@register
def disk(cfg) -> dict:
    """Свободное место там, где лежат данные приложения."""
    try:
        usage = shutil.disk_usage(str(_data_dir(cfg)))
    except (OSError, ValueError, TypeError):
        return check("disk", "Место на диске", "unknown", "не удалось узнать свободное место",
                     hint="Проверьте место на диске вручную")
    if not usage.total:
        return check("disk", "Место на диске", "unknown", "размер диска неизвестен")
    share = usage.free / float(usage.total)
    detail = "свободно %d %% (%.1f ГБ из %.1f ГБ)" % (round(share * 100), usage.free / 1024 ** 3,
                                                     usage.total / 1024 ** 3)
    detail = detail.replace(".", ",")
    if share < DISK_FAIL:
        return check("disk", "Место на диске", "fail", detail,
                     hint="Меньше 3 % — ответы и копии могут не записаться, освободите место")
    if share < DISK_WARN:
        return check("disk", "Место на диске", "warn", detail,
                     hint="Меньше 10 % — скоро может не хватить места")
    return check("disk", "Место на диске", "ok", detail)


@register
def stt(cfg) -> dict:
    """Распознавание речи — та же диагностика, что в «Проверке системы»."""
    try:
        from . import diagnostics as diag_mod
    except ImportError:                  # pragma: no cover
        import diagnostics as diag_mod   # type: ignore
    data = diag_mod._stt_status(cfg) or {}
    mode = str(data.get("mode") or "none")
    if mode == "none":
        return check("stt", "Распознавание речи", "warn", "диктовка недоступна",
                     hint="Поставьте локальный движок или настройте облачное распознавание")
    label = {"local": "локальный движок", "provider": "облачное распознавание"}.get(mode, mode)
    return check("stt", "Распознавание речи", "ok", label + " готов")


@register
def model_guard(cfg) -> dict:
    """Контроль моделей — по комнатам (как в «Проверке системы»)."""
    try:
        from . import model_guard as guard_mod
    except ImportError:                  # pragma: no cover
        import model_guard as guard_mod  # type: ignore
    try:
        rooms = rooms_mod.list_rooms(cfg) or []
    except Exception:                    # noqa: BLE001
        rooms = []
    if not rooms:
        return check("model_guard", "Контроль моделей", "unknown", "комнат нет — проверять нечего")
    item = guard_mod.diag_item(cfg, rooms=rooms) or {}
    status = str(item.get("status") or "warn")
    if status == "off":
        status = "unknown"
    return check("model_guard", "Контроль моделей", status,
                 str(item.get("value") or ""),
                 hint=str(item.get("hint") or "") or None)


# ------------------------------------------------------------------ сводка

def _ts(value) -> int | None:
    return duties_mod._ts(value)


def overall(checks) -> str:
    """`fail` важнее `warn`; `unknown` сам по себе общую оценку не портит."""
    statuses = [str(item.get("status") or "") for item in (checks or []) if isinstance(item, dict)]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def run(cfg, *, now: int | None = None, only=None) -> dict:
    """Все проверки по порядку регистрации → ответ контракта §3.3."""
    wanted = set(only) if only else None
    out: list[dict] = []
    for fn in list(CHECKS):
        name = getattr(fn, "__name__", "")
        if wanted is not None and name not in wanted:
            continue
        try:
            item = fn(cfg)
        except Exception as exc:         # noqa: BLE001 — одна проверка не валит отчёт
            item = check(name or "check", name or "Проверка", "unknown",
                         "проверка не удалась: " + _one_line(exc, 140))
        if isinstance(item, dict):
            out.append(item)
    return {"generated_at": int(now if now is not None else time.time()),
            "overall": overall(out), "checks": out}


def report(cfg, **kwargs) -> dict:
    """Псевдоним `run` для привычного имени в остальном сервере."""
    return run(cfg, **kwargs)


__all__ = ["BACKUP_WARN_DAYS", "CHECKS", "DISK_FAIL", "DISK_WARN",
           "GATEWAY_FRESH_S", "STATUSES", "TOKEN_ACTIVE_DAYS", "backups", "calendar_token",
           "check", "dispatcher", "disk", "duties", "gateway", "model_guard", "overall",
           "peer_link", "register", "report", "run", "stt"]
