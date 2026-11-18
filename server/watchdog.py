"""Сторож зависаний карточки (WAVE25 §M2, основание — R4/E2/E5).

Проблема (найдена на живых данных): карточка в работе может не отвечать десятки
минут — в боевом случае было 55 минут. Модель жива, воркер жив, а результат не
приходит: расход растёт, свод ждёт, человек не знает, что происходит.

Правила (пороги — настройки комнаты, по умолчанию 10/20 минут):

* **10 минут** без событий и вывода инструментов → мягкое предупреждение: строка
  в «Ходе работы» и пункт «Карточка X не отвечает 10 мин» в «Сегодня» / «От вас
  нужно». Ничего не останавливаем — человек решает;
* **20 минут** → принудительная остановка **штатным механизмом W23**
  (``kanban_atomic.stop``: убить воркер и удержать карточку в ``blocked`` одним
  процессом Hermes), затем пометка «Нужно внимание» с вариантами «Продолжить» и
  «Сменить модель и продолжить» (``kanban_atomic.resume`` /
  ``change_model_and_resume``). Жёсткую остановку можно выключить настройкой
  комнаты — тогда сторож только предупреждает;
* **карточка с живым выводом инструментов зависшей НЕ считается** (E5): пока в
  сессии идут сообщения инструментов или в ней висит незавершённый вызов
  инструмента у живого воркера, таймер молчит — иначе длинная команда получала
  бы «принудительную остановку» на ровном месте;
* **один активный запуск на карточку**: повторная остановка/перезапуск той же
  карточки берётся под замок с TTL (``data/state/<room>/watchdog/locks/``), чтобы
  два прохода и два действия не подрались за одну карточку.

Что делает проход `tick`: ничего не пишет в реальные профили Hermes, доску читает
`sqlite mode=ro` (через `agent_models`), останавливает только карточки tenant
комнаты и только штатной функцией движка. Работа/мок и комнаты без доски пропускает.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

try:                                        # import server.watchdog
    from . import statefiles as sf
except ImportError:                          # python server/app.py (плоский импорт)
    import statefiles as sf                  # type: ignore

#: Пороги по умолчанию (минуты). Настройка комнаты — `card_watchdog`.
WARN_MIN = 10
STOP_MIN = 20
#: Границы настройки: 2–120 минут; остановка не раньше предупреждения.
MIN_MIN, MAX_MIN = 2, 120
#: Статусы, в которых карточка «идёт» (остальные сторож не трогает).
RUNNING = ("running",)
#: Файл состояния комнаты и каталог замков внутри `data/state/<room>/watchdog`.
STATE_FILE = "watchdog.json"
LOCK_DIR = "locks"
#: Сколько живёт замок действия по карточке (секунды): дольше — считаем брошенным.
LOCK_TTL = 300
#: Как часто повторять предупреждение по одной карточке (журнал не копим).
WARN_TTL = 1800
#: Приписка к комментарию карточки — по ней видно, что это сторож, а не человек.
NOTE_PREFIX = "[Сторож зависаний]"
#: Что предлагаем человеку после остановки (кнопки интерфейса).
ACTIONS = ({"id": "resume", "label": "Продолжить"},
           {"id": "change_model", "label": "Сменить модель и продолжить"})


# ---------------------------------------------------------------- настройки ---
def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize(raw) -> dict:
    """Настройка комнаты ``card_watchdog`` → ``{warn_min, stop_min, hard_stop}``.

    Битое и неполное значение не роняет проход: берём умолчания по полям.
    ``hard_stop=False`` — сторож только предупреждает (остановку выключили).
    """
    raw = raw if isinstance(raw, dict) else {}
    warn = _int(raw.get("warn_min"), WARN_MIN)
    stop = _int(raw.get("stop_min"), max(STOP_MIN, warn + 1))
    warn = max(MIN_MIN, min(MAX_MIN, warn))
    stop = max(warn + 1, min(MAX_MIN * 2, stop))
    hard = raw.get("hard_stop")
    hard_stop = True if hard is None else bool(hard)
    return {"warn_min": warn, "stop_min": stop, "hard_stop": hard_stop}


def settings(room) -> dict:
    """Действующие пороги комнаты (``room['card_watchdog']`` или умолчания)."""
    room = room if isinstance(room, dict) else {}
    return normalize(room.get("card_watchdog"))


# ---------------------------------------------------------------- состояние ---
def _dir(cfg, room_id) -> Path:
    return Path(cfg.data_dir) / "state" / str(room_id or "") / "watchdog"


def state_path(cfg, room_id) -> Path:
    return _dir(cfg, room_id) / STATE_FILE


def _blank() -> dict:
    return {"cards": {}, "stops": 0, "warns": 0, "updated_at": 0}


def read_state(cfg, room_id) -> dict:
    """Состояние сторожа комнаты; битый файл — как пустое (не роняем проход)."""
    try:
        data = sf.read_json(state_path(cfg, room_id), None)
    except Exception:                        # noqa: BLE001 — состояние вторично
        data = None
    if not isinstance(data, dict):
        return _blank()
    out = _blank()
    out["cards"] = {str(k): v for k, v in (data.get("cards") or {}).items() if isinstance(v, dict)}
    out["stops"] = _int(data.get("stops"))
    out["warns"] = _int(data.get("warns"))
    out["updated_at"] = _int(data.get("updated_at"))
    return out


def _mutate(cfg, room_id, change) -> dict:
    """Прочитать-изменить-записать состояние комнаты под блокировкой файла."""
    path = state_path(cfg, room_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sf.lock_for(path):
            data = read_state(cfg, room_id)
            change(data)
            data["updated_at"] = int(time.time())
            sf.write_json_atomic(path, data)
        return data
    except Exception:                        # noqa: BLE001 — не роняем сторож
        return read_state(cfg, room_id)


# ---------------------------------------------------------------- замки ---
def lock_path(cfg, room_id, card_id) -> Path:
    return _dir(cfg, room_id) / LOCK_DIR / ("%s.json" % str(card_id or "card"))


def acquire(cfg, room_id, card_id, *, now=None, ttl: int = LOCK_TTL) -> bool:
    """Взять замок действия по карточке (один активный запуск на карточку)."""
    path = lock_path(cfg, room_id, card_id)
    stamp = int(now if now is not None else time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            held = sf.read_json(path, None)
            since = _int((held or {}).get("at")) if isinstance(held, dict) else 0
            if since and stamp - since < int(ttl):
                return False                 # уже работает другой проход
        sf.write_json_atomic(path, {"at": stamp, "card_id": str(card_id)})
        return True
    except Exception:                        # noqa: BLE001 — без замка действуем осторожно
        return False


def release(cfg, room_id, card_id) -> None:
    try:
        lock_path(cfg, room_id, card_id).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- правило ---
def classify(*, status, last_at, started_at, now, conf, tool_alive=False) -> dict:
    """Чистое правило сторожа: что делать с карточкой в этот момент.

    Возвращает ``{state, idle_min, text, action}``: ``state`` — ``working`` (всё
    в порядке), ``warn`` (пора предупредить) или ``hung`` (пора останавливать).
    Время приходит аргументом — тесты подменяют его, ничего не ждут.
    """
    conf = conf if isinstance(conf, dict) else normalize(None)
    now = _int(now)
    if str(status or "") not in RUNNING:
        return {"state": "idle", "idle_min": 0, "text": "", "action": "none"}
    base = _int(last_at) or _int(started_at)
    if not base or not now:
        return {"state": "working", "idle_min": 0, "text": "", "action": "none"}
    idle = max(0, now - base)
    minutes = int(idle / 60)
    if tool_alive:
        # живой вывод инструментов: карточка не «зависла», даже если молчит долго
        return {"state": "working", "idle_min": minutes, "text": "", "action": "none"}
    if idle >= _int(conf["stop_min"]) * 60:
        return {"state": "hung", "idle_min": minutes,
                "text": "не отвечает %d мин" % minutes,
                "action": "stop" if conf.get("hard_stop") else "warn"}
    if idle >= _int(conf["warn_min"]) * 60:
        return {"state": "warn", "idle_min": minutes,
                "text": "не отвечает %d мин" % minutes, "action": "warn"}
    return {"state": "working", "idle_min": minutes, "text": "", "action": "none"}


def text_for(item: dict) -> str:
    """Человеческая строка для «Хода работы» и «Сегодня» (≤ 200 знаков)."""
    who = str(item.get("label") or item.get("profile") or "участник")
    title = str(item.get("card_title") or "").strip()
    tail = (" «%s»" % title) if title else ""
    if item.get("state") == "hung":
        if item.get("hard_stop") is False:
            return "Карточка %s%s не отвечает %d мин — остановка выключена в настройках комнаты" % (
                who, tail, _int(item.get("idle_min")))
        return "Карточка %s%s не отвечает %d мин — остановлена, нужно внимание" % (
            who, tail, _int(item.get("idle_min")))
    return "Карточка %s%s не отвечает %d мин" % (who, tail, _int(item.get("idle_min")))


__all__ = ["WARN_MIN", "STOP_MIN", "MIN_MIN", "MAX_MIN", "RUNNING", "STATE_FILE", "LOCK_DIR",
           "LOCK_TTL", "WARN_TTL", "NOTE_PREFIX", "ACTIONS", "normalize", "settings",
           "state_path", "read_state", "lock_path", "acquire", "release", "classify", "text_for"]


# ---------------------------------------------------------------- проход ---
def _mods():
    """Ленивые импорты: модуль сторожа не тянет доску при импорте (циклов нет)."""
    try:
        from . import agent_models as models
        from . import kanban_atomic as atomic
        from . import worklog as wl
    except ImportError:                      # pragma: no cover - плоский импорт
        import agent_models as models        # type: ignore
        import kanban_atomic as atomic       # type: ignore
        import worklog as wl                 # type: ignore
    return models, atomic, wl


def _note(cfg, room, root_id, text) -> None:
    """Служебная строка в ветку запроса (видна в «Ходе работы» и у руководителя)."""
    lead = str((room or {}).get("orchestrator") or "").strip()
    if not lead or not root_id:
        return
    try:
        from . import engine_write as ew
    except ImportError:                      # pragma: no cover
        try:
            import engine_write as ew        # type: ignore
        except ImportError:
            return
    try:
        ew.act(cfg, str((room or {}).get("board") or ""), str(root_id), "comment",
               text="%s %s" % (NOTE_PREFIX, text), task_id=str(root_id), author=lead)
    except Exception:                        # noqa: BLE001 - сообщение вторично
        pass


def _remember(cfg, room_id, card_id, data: dict) -> None:
    def change(state):
        state["cards"][str(card_id)] = data
    _mutate(cfg, room_id, change)


def _should_warn(cfg, room_id, card_id, now, state) -> bool:
    """Предупреждение по карточке не чаще раза в `WARN_TTL` (журнал не копим)."""
    seen = (read_state(cfg, room_id).get("cards") or {}).get(str(card_id)) or {}
    if str(seen.get("state") or "") == str(state) and now - _int(seen.get("at")) < WARN_TTL:
        return False

    def change(data):
        data["warns"] = _int(data.get("warns")) + 1
    _mutate(cfg, room_id, change)
    return True


def _root_of(con, card_id) -> str:
    """Корень дерева карточки (для строки в ветку запроса) — обход `task_links` вверх."""
    current = str(card_id or "")
    seen: set = set()
    while current and current not in seen:
        seen.add(current)
        try:
            row = con.execute("SELECT parent_id FROM task_links WHERE child_id = ? "
                              "ORDER BY parent_id LIMIT 1", (current,)).fetchone()
        except Exception:                    # noqa: BLE001 - старая схема
            break
        parent = str((row["parent_id"] if row is not None else "") or "")
        if not parent:
            break
        current = parent
    return current if current != str(card_id or "") else ""


def tick(cfg, room, *, now=None, limit: int = 60) -> dict:
    """Один проход сторожа по комнате: предупредить на 10 мин, остановить на 20.

    Возвращает счётчики и списки: ``warned``, ``stopped``, ``alive_tools``. Ничего
    не делает в демо/моке, без доски и без карточек tenant комнаты; в реальные
    профили Hermes не пишет (только чтение `state.db` через `worklog`).
    """
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "")
    out = {"room": room_id, "checked": 0, "warned": [], "stopped": [], "alive_tools": 0,
           "skipped_reason": "", "at": _int(now if now is not None else time.time())}
    if not room_id or room.get("archived"):
        out["skipped_reason"] = "no_room"
        return out
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        out["skipped_reason"] = "demo"
        return out
    if not str(room.get("tenant") or ("room:" + room_id)).startswith("room:"):
        out["skipped_reason"] = "not_room_tenant"
        return out
    conf = settings(room)
    models, atomic, wl = _mods()
    board = str(room.get("board") or getattr(cfg, "board", "") or "team")
    try:
        cards = models.card_map(cfg, room) or {}
    except Exception as exc:                 # noqa: BLE001 - чтение доски вторично
        out["skipped_reason"] = "cards_unreadable"
        out["error"] = str(exc)[:160]
        return out
    con = models._connect(cfg, board)        # noqa: SLF001 - общий код чтения доски
    if con is None:
        out["skipped_reason"] = "no_board"
        return out
    stamp = out["at"]
    try:
        for card in list(cards.values())[:max(1, int(limit))]:
            cid = str(card.get("id") or "")
            status = str(card.get("status") or "")
            if not cid or status not in RUNNING:
                continue
            out["checked"] += 1
            profile = str(card.get("assignee") or card.get("profile") or "")
            try:
                run, sid = models._run_session(con, cid)   # noqa: SLF001
            except Exception:                # noqa: BLE001 - старая схема
                run, sid = {}, ""
            act = {"last_at": 0, "pending_tool": False, "tool": ""}
            if sid and profile:
                try:
                    act = wl.activity(cfg, profile, sid)
                except Exception:            # noqa: BLE001 - сессия может быть занята
                    act = {"last_at": 0, "pending_tool": False, "tool": ""}
            worker_open = bool(run) and not _int((run or {}).get("ended_at"))
            tool_alive = bool(act.get("pending_tool")) and worker_open   # E5: живой вывод
            if tool_alive:
                out["alive_tools"] += 1
            verdict = classify(status=status, last_at=act.get("last_at") or 0,
                               started_at=_int(card.get("started_at")) or _int(card.get("created_at")),
                               now=stamp, conf=conf, tool_alive=tool_alive)
            if verdict["state"] == "working":
                continue
            root_id = _root_of(con, cid) or cid
            item = {"card_id": cid, "profile": profile,
                    "label": str(card.get("title") or profile or "участник"),
                    "card_title": str(card.get("title") or ""), "root_id": root_id,
                    "idle_min": verdict["idle_min"], "state": verdict["state"],
                    "at": stamp, "hard_stop": conf["hard_stop"], "tool": act.get("tool") or ""}
            item["text"] = text_for(item)
            if verdict["state"] == "warn" or verdict["action"] == "warn":
                if _should_warn(cfg, room_id, cid, stamp, verdict["state"]):
                    _remember(cfg, room_id, cid, dict(item, state=verdict["state"]))
                    _note(cfg, room, root_id,
                          item["text"] + " — работу пока не останавливаю, проверю ещё раз.")
                    out["warned"].append(item)
                continue
            item["state"] = "hung"
            if not acquire(cfg, room_id, cid, now=stamp):
                out["warned"].append(dict(item, text=item["text"] + " (действие уже идёт)"))
                continue
            try:
                res = atomic.stop(cfg, board, cid,
                                  reason="не отвечает %d мин" % verdict["idle_min"])
                item["stop"] = {"ok": bool((res or {}).get("ok")), "code": (res or {}).get("code")}
            except Exception as exc:         # noqa: BLE001 - отказ движка не роняет проход
                item["stop"] = {"ok": False, "error": str(exc)[:200]}
            finally:
                release(cfg, room_id, cid)
            _remember(cfg, room_id, cid, item)

            def change(state):
                state["stops"] = _int(state.get("stops")) + 1
            _mutate(cfg, room_id, change)
            _note(cfg, room, root_id,
                  "%s. Остановил штатно: «Продолжить» или «Сменить модель и продолжить» — "
                  "кнопки в карточке и на странице запроса." % text_for(item))
            out["stopped"].append(item)
    finally:
        con.close()
    return out


def stalled(cfg, room, *, now=None, ttl: int = 7200) -> list[dict]:
    """Карточки, о которых сторож уже предупредил (для «Сегодня» и «Хода работы»).

    Читает ТОЛЬКО файл состояния (без доски): экран «Сегодня» не должен ждать
    sqlite. Записи старше ``ttl`` не показываются — состояние не вечное.
    """
    room_id = str((room or {}).get("id") or "")
    if not room_id:
        return []
    stamp = _int(now if now is not None else time.time())
    out: list[dict] = []
    for card_id, item in (read_state(cfg, room_id).get("cards") or {}).items():
        if str(item.get("state") or "") not in ("warn", "hung"):
            continue
        if stamp - _int(item.get("at")) > ttl:
            continue
        out.append(dict(item, card_id=str(item.get("card_id") or card_id)))
    out.sort(key=lambda row: (-_int(row.get("idle_min")), str(row.get("card_id"))))
    return out


def report(cfg, room, *, now=None) -> dict:
    """Сводка для «Здоровья» и отчёта: пороги, сколько остановок и предупреждений."""
    conf = settings(room)
    state = read_state(cfg, str((room or {}).get("id") or ""))
    return {"warn_min": conf["warn_min"], "stop_min": conf["stop_min"],
            "hard_stop": conf["hard_stop"], "warns": _int(state.get("warns")),
            "stops": _int(state.get("stops")),
            "cards": len([1 for v in (state.get("cards") or {}).values()
                          if str(v.get("state") or "") in ("warn", "hung")]),
            "updated_at": _int(state.get("updated_at")) or None}
