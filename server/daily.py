"""«Итог дня» — что сделано за сутки, что ждёт, сколько потрачено (W29B-Q8).

Экран «Сегодня» отвечает на вопрос «что сейчас»; этот модуль — на вопрос «как
прошёл день»: утром — «что было вчера», вечером — «что сделано сегодня». Считается
**без модели** по уже существующим данным (реестр запросов, состояние доски,
реестр решений, журнал, архив, расход Hermes). Кнопка «Короче» — единственный
платный шаг: дешёвая модель сжимает готовую сводку до пяти строк, стоимость
показывается до нажатия.

Источники (всё только чтение):

* ``registry.list_requests`` — запросы комнат (через ``Sources.requests``);
* ``engine_read.today_states`` — состояния корней (завершён, сбой);
* ``today``-расчёт «ждёт вас» (``routes_today.waiting_items``) — пункты с действием;
* ``decision_registry`` — принятые/отклонённые решения за сутки;
* ``archive.archive_info`` — что ушло в архив;
* ``reports``/``state`` комнаты — файлы, появившиеся за сутки;
* ``limits.panel`` — расход Hermes за сутки (только чтение ``state.db``).

Границы дня: сутки начинаются в :data:`DAY_START_HOUR` (04:00): ночная работа
относится к прошлому дню — так человек и считает «вчерашний день».

Публичный интерфейс::

    build(sources, now=None, room=None) -> dict
    digest_text(payload) -> str            # вход для «Короче»
    estimate(payload, price=None) -> dict  # токены и стоимость ДО запуска
    shorten(cfg, payload, room, runner=None) -> dict
    last_short(cfg, room_id) / save_short(...)
"""
from __future__ import annotations

import json
import os
import statistics
import time
from datetime import date, datetime
from pathlib import Path

try:                                     # пакетный и «плоский» импорт
    from . import statefiles as sf
except ImportError:                      # pragma: no cover
    import statefiles as sf              # type: ignore

#: Час, с которого начинаются «сутки» итога (ночь относится к прошлому дню).
DAY_START_HOUR = 4
#: Когда человек видит «утро» и «вечер»: подпись и порядок разделов.
MORNING_HOUR = 10
EVENING_HOUR = 17
#: Сколько знаков сводки уходит в «Короче» (дешевле — короче).
SHORT_MAX_INPUT = 4500
#: Сколько знаков ответа модели показываем (дальше — обрезка с многоточием).
SHORT_MAX_OUTPUT = 1200
#: Сколько токенов просим у модели на ответ (потолок, а не обещание).
SHORT_OUTPUT_TOKENS = 400
#: Потолок времени одного вызова дешёвой модели.
SHORT_TIMEOUT_S = 120
#: Сколько «Короче» живёт в файле, прежде чем считается устаревшим.
SHORT_TTL_S = 6 * 3600
#: Песочница и тесты: подставной ответ вместо вызова модели.
SHORT_FAKE_ENV = "TEAMROOM_DAILY_FAKE"
#: Файл последнего «Короче» внутри ``data/state/<room>/daily``.
SHORT_FILE = "short.json"
#: Сколько запросов/решений/файлов показываем списком (остальное — счётчиком).
LIST_MAX = 40
#: Потолок «ждёт вас» в итоге: это сводка, а не второй экран «Сегодня».
WAITING_MAX = 12
#: Подпись периода.
PERIOD_TITLES = {"morning": "Утро", "evening": "Вечер", "day": "День"}

_FAKE_RUNNER = None


# ------------------------------------------------------------------ источники
class Sources:
    """Что итогу дня нужно от комнат (всё только чтение).

    Настоящие источники — ``routes_daily.DailySources``; в тестах — подделки.
    Любой метод может бросить исключение: сборка продолжает работу.
    """

    def rooms(self) -> list[dict]:
        """Комнаты без архивных: ``[{id, title}]``."""
        return []

    def requests(self, room: dict) -> list[dict]:
        """Запросы комнаты из реестра (``root_id``, ``title``, ``created_at``, …)."""
        return []

    def states(self, room: dict) -> dict:
        """``{root_id: {status, finished_at, started_at, error}}`` с доски."""
        return {}

    def waiting(self, room: dict) -> list[dict]:
        """Пункты «ждёт вас» с действием: ``[{kind, text, since, request_id}]``."""
        return []

    def decisions(self, room: dict) -> list[dict]:
        """Строки реестра решений: ``[{id, at, text, status, source_request}]``."""
        return []

    def files(self, room: dict) -> list[dict]:
        """Файлы комнаты, появившиеся за сутки: ``[{name, at}]`` (без путей)."""
        return []

    def archived(self, room: dict) -> list[dict]:
        """Запросы, ушедшие в архив: ``[{root_id, title, at}]``."""
        return []

    def usage(self) -> dict:
        """Расход Hermes за сутки: ``{calls, tokens, cost_usd, source}``."""
        return {}


# -------------------------------------------------------------------- мелочи
def _now() -> int:
    return int(time.time())


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _num(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _text(value, limit: int = 160) -> str:
    """Одна строка без шума, длиной ≤ ``limit`` знаков."""
    flat = " ".join(str(value or "").split())
    if limit and len(flat) > limit:
        return flat[: max(0, limit - 1)].rstrip() + "…"
    return flat


def _dicts(value) -> list[dict]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _safe(func, default):
    """Значение источника или ``default``: битая комната не валит итог."""
    try:
        return func()
    except Exception:                    # noqa: BLE001 — источник вторичен, итог важнее
        return default


def _short_title(value) -> str:
    """Короткое название запроса — то же правило, что на остальных экранах."""
    try:
        try:
            from . import title_short as mod
        except ImportError:              # pragma: no cover
            import title_short as mod    # type: ignore
        return str(mod.short_title(str(value or "")))
    except Exception:                    # noqa: BLE001 — название вторично
        return _text(value, 70)


def day_start(now: int) -> int:
    """Начало «суток итога»: последние :data:`DAY_START_HOUR` часов."""
    stamp = datetime.fromtimestamp(int(now)).replace(hour=DAY_START_HOUR, minute=0, second=0,
                                                     microsecond=0)
    start = int(stamp.timestamp())
    return start if start <= int(now) else int(start - 86400)


def period_of(now: int) -> str:
    """Утро / вечер / день — по местному времени (для заголовка и подписи)."""
    hour = datetime.fromtimestamp(int(now)).hour
    if hour < MORNING_HOUR:
        return "morning"
    if hour >= EVENING_HOUR:
        return "evening"
    return "day"


# --------------------------------------------------------------------- сборка
def build(sources, now=None, room=None) -> dict:
    """Собрать «Итог дня» по источникам. ``room`` — один id комнаты или None (все).

    Ничего не пишет и не вызывает модель: это чистая функция над ``Sources``.
    """
    now = _now() if now is None else int(now)
    start = day_start(now)
    today = date.fromtimestamp(now).isoformat()
    only = str(room or "")
    done: list[dict] = []
    decisions: list[dict] = []
    files: list[dict] = []
    waiting: list[dict] = []
    stopped: list[dict] = []
    rooms_out: list[dict] = []

    for entry in _dicts(_safe(sources.rooms, [])):
        room_id = str(entry.get("id") or "")
        if not room_id or (only and room_id != only):
            continue
        room_title = str(entry.get("title") or room_id)
        records = [rec for rec in _dicts(_safe(lambda: sources.requests(entry), []))
                   if rec.get("root_id")]
        states = _safe(lambda: sources.states(entry), {})
        states = states if isinstance(states, dict) else {}
        titles = {}
        for rec in records:
            titles[str(rec.get("root_id"))] = _short_title(rec.get("title") or rec.get("text"))

        def title_of(root_id: str) -> str:
            return titles.get(str(root_id)) or ""

        room_done = room_waiting = room_decisions = 0

        # 1) Что сделано: запросы, завершённые за сутки.
        for rec in records:
            root_id = str(rec.get("root_id"))
            state = states.get(root_id) if isinstance(states.get(root_id), dict) else {}
            status = str((state or {}).get("status") or "").lower()
            finished = _int((state or {}).get("finished_at"))
            if status == "done" and finished >= start:
                done.append({"room": room_id, "room_title": room_title,
                             "request_id": root_id, "request_title": title_of(root_id),
                             "at": finished})
                room_done += 1
            elif status == "failed" and (_int((state or {}).get("finished_at"))
                                         or _int((state or {}).get("started_at"))
                                         or _int(rec.get("created_at"))) >= start:
                at = (_int((state or {}).get("finished_at"))
                      or _int((state or {}).get("started_at")) or now)
                reason = _text((state or {}).get("error") or "Работа остановилась с ошибкой",
                               160) or "Работа остановилась с ошибкой"
                stopped.append({"room": room_id, "room_title": room_title,
                                "request_id": root_id, "request_title": title_of(root_id),
                                "at": at, "reason": reason, "kind": "failed"})
            elif status in ("blocked", "needs_you") and _int(rec.get("created_at")) >= start:
                # Запрос встал и ждёт человека: это не «сделано», но и не «остановлено».
                continue

        # 2) Что решено: строки реестра решений за сутки.
        for row in _dicts(_safe(lambda: sources.decisions(entry), [])):
            at = _int(row.get("at"))
            status = str(row.get("status") or "")
            if at < start or status not in ("accepted", "rejected", "dismissed"):
                continue
            decisions.append({"room": room_id, "room_title": room_title,
                              "id": str(row.get("id") or ""),
                              "title": _text(row.get("text"), 160),
                              "status": status,
                              "request_id": str(row.get("source_request") or ""),
                              "request_title": title_of(str(row.get("source_request") or "")),
                              "at": at})
            room_decisions += 1

        # 3) Что пришло файлами: отчёты и материалы комнаты за сутки.
        for item in _dicts(_safe(lambda: sources.files(entry), [])):
            at = _int(item.get("at"))
            if at < start:
                continue
            files.append({"room": room_id, "room_title": room_title,
                          "name": _text(item.get("name"), 120), "at": at})

        # 4) Что ждёт — с готовым действием (правило «без тупиков»).
        for item in _dicts(_safe(lambda: sources.waiting(entry), [])):
            kind = str(item.get("kind") or "")
            text = _text(item.get("text"), 200)
            if not kind or not text:
                continue
            waiting.append({"room": room_id, "room_title": room_title, "kind": kind,
                            "text": text,
                            "request_id": str(item.get("request_id") or ""),
                            "request_title": title_of(str(item.get("request_id") or "")),
                            "since": _int(item.get("since")) or now,
                            "recommended": str(item.get("recommended") or "")})
            room_waiting += 1

        # 5) Что ушло в архив за сутки.
        for row in _dicts(_safe(lambda: sources.archived(entry), [])):
            at = _int(row.get("at"))
            if at and at < start:
                continue
            stopped.append({"room": room_id, "room_title": room_title,
                            "request_id": str(row.get("root_id") or ""),
                            "request_title": _short_title(row.get("title") or ""),
                            "at": at or now, "reason": "в архиве",
                            "kind": "archived"})

        rooms_out.append({"id": room_id, "title": room_title, "done": room_done,
                          "decisions": room_decisions, "waiting": room_waiting})

    done.sort(key=lambda row: -_int(row.get("at")))
    decisions.sort(key=lambda row: -_int(row.get("at")))
    files.sort(key=lambda row: -_int(row.get("at")))
    waiting.sort(key=lambda row: (0 if _int(row.get("since")) <= start else 1,
                                  _int(row.get("since"))))
    stopped.sort(key=lambda row: -_int(row.get("at")))
    usage = _safe(sources.usage, {})
    usage = usage if isinstance(usage, dict) else {}
    spent = {"calls": _int(usage.get("calls")), "tokens": _int(usage.get("tokens")),
             "cost_usd": round(_num(usage.get("cost_usd")), 6),
             "source": str(usage.get("source") or "none"),
             "note": str(usage.get("note") or "")}

    payload = {
        "generated_at": now,
        "day": today,
        "day_start": start,
        "period": period_of(now),
        "period_title": PERIOD_TITLES.get(period_of(now), "День"),
        "counters": {"done": len(done), "decisions": len(decisions), "files": len(files),
                     "waiting": len(waiting), "stopped": len(stopped),
                     "all": len(done) + len(decisions) + len(files) + len(waiting) + len(stopped),
                     "spent_calls": spent["calls"], "spent_tokens": spent["tokens"],
                     "spent_usd": spent["cost_usd"]},
        "done": done[:LIST_MAX],
        "decisions": decisions[:LIST_MAX],
        "files": files[:LIST_MAX],
        "waiting": waiting[:WAITING_MAX],
        "waiting_total": len(waiting),
        "stopped": stopped[:LIST_MAX],
        "spent": spent,
        "rooms": rooms_out,
        "kinds": ["done", "decisions", "files", "waiting", "stopped"],
        "empty": not (done or decisions or files or waiting or stopped),
        # «Короче» подкладывает HTTP-слой (`routes_daily`): в самой сборке нет
        # ни файлов, ни конфигурации — функция остаётся чистой.
        "short": None,
    }
    return payload


# ------------------------------------------------------------------ «Короче»
def short_path(cfg, room_id) -> Path:
    """Файл последнего «Короче» комнаты: ``data/state/<room>/daily/short.json``."""
    return Path(cfg.state_dir) / str(room_id) / "daily" / SHORT_FILE


def last_short(cfg, room_id) -> dict | None:
    """Последний сжатый итог (или None): свежий ли — решает интерфейс по ``at``."""
    try:
        data = sf.read_json(short_path(cfg, room_id), None)
    except Exception:                    # noqa: BLE001 — файл вторичен
        return None
    if not isinstance(data, dict) or not str(data.get("text") or "").strip():
        return None
    return data


def save_short(cfg, room_id, payload: dict) -> dict:
    """Записать «Короче» (одна строка на комнату; перезапись атомарная)."""
    path = short_path(cfg, room_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sf.lock_for(path):
            sf.write_json_atomic(path, payload)
        return payload
    except Exception as exc:             # noqa: BLE001 — без файла итог всё равно есть
        return dict(payload, warning="Не удалось сохранить «Короче»: %s" % _text(exc, 120))


def digest_text(payload: dict) -> str:
    """Компактная сводка для модели: только факты, без разметки и путей."""
    p = payload or {}
    lines: list[str] = []
    lines.append("Итог дня %s (%s)." % (str(p.get("day") or ""),
                                       PERIOD_TITLES.get(str(p.get("period")), "День")))
    done = _dicts(p.get("done"))
    if done:
        lines.append("Сделано (%d):" % len(done))
        for row in done[:12]:
            lines.append("- «%s» — завершён" % _text(row.get("request_title"), 80))
    decisions = _dicts(p.get("decisions"))
    if decisions:
        lines.append("Решения (%d):" % len(decisions))
        for row in decisions[:12]:
            lines.append("- %s [%s]" % (_text(row.get("title"), 140),
                                        str(row.get("status") or "")))
    files = _dicts(p.get("files"))
    if files:
        lines.append("Файлы (%d): %s" % (len(files), ", ".join(
            _text(row.get("name"), 60) for row in files[:10])))
    waiting = _dicts(p.get("waiting"))
    if waiting:
        lines.append("Ждёт вас (%d):" % int(p.get("waiting_total") or len(waiting)))
        for row in waiting[:10]:
            lines.append("- %s: %s" % (_text(row.get("text"), 120),
                                       _text(row.get("recommended"), 60)))
    stopped = _dicts(p.get("stopped"))
    if stopped:
        lines.append("Остановлено (%d):" % len(stopped))
        for row in stopped[:8]:
            lines.append("- «%s»: %s" % (_text(row.get("request_title"), 60),
                                         _text(row.get("reason"), 80)))
    spent = p.get("spent") or {}
    if _int(spent.get("calls")) or _num(spent.get("cost_usd")):
        lines.append("Расход: %d вызовов, %d тыс. токенов, %.2f усл. ед."
                     % (_int(spent.get("calls")),
                        int(round(_int(spent.get("tokens")) / 1000.0)),
                        _num(spent.get("cost_usd"))))
    text = "\n".join(lines)
    if len(text) > SHORT_MAX_INPUT:
        text = text[: SHORT_MAX_INPUT - 1].rstrip() + "…"
    return text


def short_prompt(payload: dict) -> str:
    """Задание модели: пять строк по-русски, без выдумок и без советов."""
    return ("Ниже — сводка дня команды. Сожми её до пяти коротких строк обычным "
            "русским языком: что сделано, что решено, что ждёт человека, что "
            "остановлено. Только факты из сводки, без вступлений, без списков "
            "задачами, без выдуманных чисел, без разметки. Пиши так, как сказал бы "
            "коллеге вслух.\n\n" + digest_text(payload))


def estimate(payload: dict, price_per_token: float | None = None) -> dict:
    """Сколько будет стоить «Короче» — ДО запуска: токены входа и потолок выхода."""
    text = short_prompt(payload)
    # 4 знака на токен — грубая, но честная оценка для русского текста.
    input_tokens = max(1, int(len(text) / 4))
    output_tokens = SHORT_OUTPUT_TOKENS
    cost = None
    if price_per_token:
        cost = round(float(price_per_token) * (input_tokens + output_tokens), 6)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost, "model": None,
            "note": "" if cost is not None else
                    "Стоимость неизвестна: по этой модели ещё не было сессий в state.db."}


def price_per_token(state_dir, model: str = "") -> float | None:
    """Средняя цена одного токена по ``state.db`` (только чтение) — по модели или всем.

    Читается медиана ``estimated_cost_usd / (все токены)`` по сессиям с известной
    стоимостью: подписки и локальные модели цены не имеют — тогда ``None``.
    """
    if not state_dir:
        return None
    rates: list[float] = []
    try:
        paths = []
        root = Path(state_dir)
        if (root / "state.db").is_file():
            paths.append(root / "state.db")
        try:
            paths.extend(p for p in sorted((root / "profiles").glob("*/state.db")) if p.is_file())
        except OSError:
            pass
        for path in paths:
            uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
            try:
                import sqlite3
                con = sqlite3.connect(uri, uri=True, timeout=2.0)
            except Exception:            # noqa: BLE001 — базы может не быть
                continue
            try:
                sql = ("SELECT model, input_tokens, cache_read_tokens, cache_write_tokens, "
                       "output_tokens, estimated_cost_usd FROM sessions "
                       "WHERE estimated_cost_usd > 0")
                args: tuple = ()
                if model:
                    sql += " AND model = ?"
                    args = (str(model),)
                rows = con.execute(sql, args).fetchall()
            except Exception:            # noqa: BLE001 — схема может отличаться
                rows = []
            con.close()
            for row_model, inp, cache_read, cache_write, out, cost in rows:
                tokens = (_int(inp) + _int(cache_read) + _int(cache_write) + _int(out))
                if tokens > 0 and _num(cost) > 0:
                    rates.append(_num(cost) / float(tokens))
    except Exception:                    # noqa: BLE001 — оценка вторична
        return None
    if not rates:
        return None
    return float(statistics.median(rates))


def set_runner(runner) -> None:
    """Подставить исполнителя «Короче» (тесты, проба)."""
    global _FAKE_RUNNER
    _FAKE_RUNNER = runner


def reset_runner() -> None:
    """Убрать подставного исполнителя."""
    global _FAKE_RUNNER
    _FAKE_RUNNER = None


def _fake_answer() -> str:
    """Ответ песочницы: файл из ``TEAMROOM_DAILY_FAKE`` или короткий пример."""
    raw = str(os.environ.get(SHORT_FAKE_ENV) or "").strip()
    if raw and raw not in ("1", "true", "yes", "on"):
        try:
            text = Path(raw).read_text(encoding="utf-8-sig")
            if text.strip():
                return text.strip()
        except OSError:
            pass
    return ("Сделано: два запроса доведены до конца, решения записаны в реестр. "
            "Ждёт вас: один вопрос по срокам и одна заблокированная карточка. "
            "Остановлено: один запуск с ошибкой — причина в журнале.")


def _default_runner(cfg, model: str = "", provider: str = ""):
    """Один вызов дешёвой модели: штатный `hermes -z` (без правок профилей).

    Флаги — те же, что у обычного запуска Hermes: ``-m`` модель, ``--provider``
    провайдер. Профиль берётся из настроек приложения (``hermes_home``), поэтому
    поведение совпадает с остальной работой комнаты.
    """
    binary = str(getattr(cfg, "hermes_cli", None) or os.environ.get("TEAMROOM_HERMES_BIN")
                 or "hermes")
    home = str(getattr(cfg, "hermes_home", "") or "")

    def run(prompt: str) -> dict:
        argv = [binary, "-z", prompt]
        if model:
            argv += ["-m", str(model)]
        if provider:
            argv += ["--provider", str(provider)]
        env = dict(os.environ)
        if home:
            env["HERMES_HOME"] = home
        env.pop("HERMES_KANBAN_TASK", None)
        try:
            try:
                from . import proc as proc_mod
            except ImportError:          # pragma: no cover
                import proc as proc_mod  # type: ignore
            done = proc_mod.run(argv, module=__import__("subprocess"),
                                capture_output=True, text=True,
                                timeout=SHORT_TIMEOUT_S, env=env)
        except FileNotFoundError:
            return {"ok": False, "code": 127, "text": "",
                    "error": "Команда hermes не найдена — «Короче» недоступен."}
        except Exception as exc:         # noqa: BLE001 — таймаут и прочее
            return {"ok": False, "code": 124, "text": "",
                    "error": "Модель не ответила за %d с: %s" % (SHORT_TIMEOUT_S,
                                                                 _text(exc, 120))}
        code = int(getattr(done, "returncode", 1) or 0)
        out = str(getattr(done, "stdout", "") or "")
        err = str(getattr(done, "stderr", "") or "")
        if code != 0 or not out.strip():
            return {"ok": False, "code": code, "text": "",
                    "error": "Модель вернула ошибку: %s" % _text(err or out or "пустой ответ", 200)}
        return {"ok": True, "code": code, "text": out.strip(), "error": ""}

    return run


def shorten(cfg, payload: dict, room_id: str, model: str = "", provider: str = "",
            runner=None, now=None, state_dir=None) -> dict:
    """«Короче»: сжать готовый итог дешёвой моделью. Один вызов, стоимость известна заранее.

    Песочница и тесты: ``runner`` или ``TEAMROOM_DAILY_FAKE`` — настоящий Hermes не
    вызывается. Ошибка модели не ломает экран: возвращается ``ok:false`` с причиной
    и действием, готовый итог остаётся как был.
    """
    now = _now() if now is None else int(now)
    price = price_per_token(state_dir or getattr(cfg, "hermes_home", None), model)
    est = estimate(payload, price)
    est["model"] = model or None
    run = runner or _FAKE_RUNNER
    fake = False
    if run is None and str(os.environ.get(SHORT_FAKE_ENV) or "").strip():
        fake = True

        def run(prompt: str) -> dict:     # noqa: F811 — подставной исполнитель песочницы
            return {"ok": True, "code": 0, "text": _fake_answer(), "error": ""}
    if run is None:
        run = _default_runner(cfg, model=model, provider=provider)
    try:
        result = run(short_prompt(payload))
    except Exception as exc:             # noqa: BLE001 — сбой модели не валит экран
        result = {"ok": False, "text": "", "error": "Сжатие не удалось: %s" % _text(exc, 160)}
    if not isinstance(result, dict):
        result = {"ok": False, "text": "", "error": "Модель вернула неожидаемый ответ."}
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or "Модель не ответила"),
                "estimate": est, "fake": fake, "at": now}
    text = _text(result.get("text"), SHORT_MAX_OUTPUT)
    saved = save_short(cfg, room_id, {"text": text, "at": now, "model": model or "",
                                      "provider": provider or "", "cost_usd": est.get("cost_usd"),
                                      "day": str(payload.get("day") or ""),
                                      "fake": bool(fake)})
    return {"ok": True, "text": text, "at": now, "estimate": est, "fake": fake,
            "saved": bool(saved and not saved.get("warning")),
            "warning": saved.get("warning") if isinstance(saved, dict) else None}


__all__ = ["DAY_START_HOUR", "MORNING_HOUR", "EVENING_HOUR", "SHORT_MAX_INPUT",
           "SHORT_MAX_OUTPUT", "SHORT_OUTPUT_TOKENS", "SHORT_FAKE_ENV", "SHORT_TTL_S",
           "PERIOD_TITLES", "Sources", "day_start", "period_of", "build", "digest_text",
           "short_prompt", "estimate", "price_per_token", "shorten", "short_path",
           "last_short", "save_short", "set_runner", "reset_runner"]
