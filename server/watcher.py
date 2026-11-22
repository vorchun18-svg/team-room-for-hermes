"""Фоновый наблюдатель «Комнаты команды».

Раз в 5 с (15 с, если ничего не идёт) по каждой неархивной комнате:
* берёт последние запросы из реестра;
* читает ``engine_read.request_detail`` для незавершённых и недавно завершённых;
* зовёт ``critic_loop.run_step`` (retarget / критик / доработка);
* закрепляет выбор модели комнаты на ВСЕХ её карточках по tenant и сверяет фактическую
  модель идущих запусков (``agent_models.apply_to_room`` + ``verify_started``) — до того,
  как движок успеет взять карточку на модель профиля;
* для завершённых — ``ingest.ingest_request`` + ``ingest.write_vault_mirror`` (идемпотентно);
* кладёт в «Ход работы» промежуточный итог сторожа (раз в ``digest_every_min`` минут, без
  модели) и находит карточки без активности дольше 15 минут (W20 §1.6);
* обновляет кэш сводок для ``GET /api/rooms``.

Ошибка одного запроса не валит цикл: она уходит в ``data/logs/app.log`` и в
``/api/health.watcher.last_error``.
"""
from __future__ import annotations

import contextlib
import threading
import time

TICK_ACTIVE = 5          # секунд, когда есть незавершённая работа
TICK_IDLE = 15           # секунд простоя
#: W23 §2.3: свой быстрый цикл контроля моделей — отдельно от общего тика наблюдателя.
#: Он должен успевать ДО того, как диспетчер возьмёт карточку на чужую модель, поэтому
#: 3 секунды, а не 5–15. Один проход — по одному дешёвому чтению tenant на комнату.
GUARD_TICK = 3.0
#: W28-C1: свой цикл «модель до старта». Главный дефект 5 — карточки без `model_override`
#: диспетчер брал раньше, чем сторож ставил модель (окно ~15 с, платили токенами). Этот
#: цикл 1 с: сначала модель, потом «отдать диспетчеру».
PREFLIGHT_TICK = 1.0
PREFLIGHT_ROOM_BUDGET = 4.0    # секунд на комнату
PREFLIGHT_PASS_BUDGET = 12.0   # секунд на весь проход
#: W25-M2: свой цикл сторожа зависаний — пороги в минутах, спешить незачем
WATCHDOG_TICK = 30.0
GUARD_ROOM_BUDGET = 8.0  # секунд на комнату: медленная доска не задерживает остальные
GUARD_PASS_BUDGET = 20.0  # секунд на весь проход сторожа (дальше комнаты уходят на следующий)
MAX_REQUESTS = 6         # сколько последних запросов комнаты смотреть за тик
TICK_BUDGET = 20.0       # D6: тик не висит дольше этого бюджета (секунды)
REQUEST_BUDGET = 5.0     # бюджет одного чтения запроса внутри тика
#: B-35: сколько секунд после завершения не напоминаем — даём руководителю
#: дописать записи в «Особенности» и отметку уже после kanban_complete.
DUTY_GRACE_S = 180
#: B-35: запросы, завершённые ДО запуска приложения (и раньше, чем за 15 минут до него),
#: не трогаем: иначе первый же тик после обновления разослал бы напоминания по всей
#: истории комнаты. Обязанность касается новых завершений.
DUTY_START_LOOKBACK_S = 900


def _module_missing(exc: BaseException) -> bool:
    """Модуль ещё не готов (нет в сборке/в подмене) — это не ошибка тика.

    Нужно, чтобы вызов карты проекта не портил счётчик ошибок там, где модуля
    ``plan`` нет вовсе (старые сборки, тестовые ``FakeApp``).
    """
    return isinstance(exc, (KeyError, AttributeError)) or getattr(exc, "status", None) == 501


def board_block_reason(cfg) -> str:
    """W29-I1: почему проходы сторожа не должны писать в доску (пусто — можно).

    Песочница с реальной доской Hermes — это не «прочитать и поправить», это чужие
    карточки: 21.11.2026 такой проход снял пины и паузы у восьми боевых карточек.
    """
    try:
        try:
            from . import board_guard as guard
        except ImportError:                               # pragma: no cover
            import board_guard as guard                   # type: ignore
        found = guard.check(cfg, getattr(cfg, "board", None))
    except Exception:                                     # noqa: BLE001 - защита вторична
        return ""
    return str((found or {}).get("message") or "")


def local_midnight(ts: int | None = None) -> int:
    """Начало текущих суток по поясу сервера (для счётчика done_today)."""
    lt = time.localtime(ts or time.time())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def counters_from_statuses(statuses, finished_at, today0: int) -> dict:
    """Счётчики комнаты: working|reviewing|synthesizing → working, needs_you, done_today."""
    counters = {"working": 0, "needs_you": 0, "done_today": 0}
    for status, finished in zip(statuses, finished_at):
        if status in ("working", "reviewing", "synthesizing"):
            counters["working"] += 1
        elif status == "needs_you":
            counters["needs_you"] += 1
        elif status == "done" and (finished or 0) >= today0:
            counters["done_today"] += 1
    return counters


def phase_of(detail: dict):
    """Текущая фаза проекта из stage запроса: {id, title, status} или None."""
    stage = (detail or {}).get("stage") or {}
    current = stage.get("current")
    if not current:
        return None
    for phase in stage.get("phases") or []:
        if isinstance(phase, dict) and phase.get("id") == current:
            return {"id": current, "title": phase.get("title") or current,
                    "status": phase.get("status") or "active"}
    return {"id": current, "title": current, "status": "active"}


class Watcher:
    """Поток наблюдателя. ``tick_once()`` вызывается и тестами (без потока)."""

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.cache: dict = {}          # room_id -> {counters, phase, updated_at}
        self.last_run_at: int | None = None
        self.last_error: str | None = None
        self.last_stats: dict | None = None
        self.running = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mirrored: set = set()
        self._synced: dict = {}        # (room_id, root_id) -> подпись заметки Obsidian
        #: W23 §2.3: быстрый сторож моделей — свой поток и своя статистика
        self._guard_thread: threading.Thread | None = None
        self._guard_stop = threading.Event()
        self.guard_last_run_at: int | None = None
        self.guard_last_error: str | None = None
        self.guard_stats: dict | None = None
        self.guard_running = False
        #: W28-C1: цикл «модель до старта» — свой поток, свой интервал 1 с
        self._preflight_thread: threading.Thread | None = None
        self._preflight_stop = threading.Event()
        self.preflight_last_run_at: int | None = None
        self.preflight_last_error: str | None = None
        self.preflight_stats: dict | None = None
        self.preflight_running = False
        #: W25-M2: сторож зависаний — тоже своим потоком (пороги в минутах)
        self._wd_thread: threading.Thread | None = None
        self._wd_stop = threading.Event()
        self.wd_last_run_at: int | None = None
        self.wd_last_error: str | None = None
        self.wd_stats: dict | None = None
        self.wd_running = False
        #: B-35: с какого момента напоминаем об оценке работы (без ретро-рассылки)
        self._duty_since = int(time.time())
        #: W29B-Q6: итог последнего прохода уведомлений (канал/показано/пропущено)
        self.notify_stats: dict | None = None

    # --- публичное ---
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="team-room-watcher", daemon=True)
        self._thread.start()
        # W23 §2.3: быстрый сторож моделей — своим потоком, чтобы не ждать общего тика
        if not (self._guard_thread and self._guard_thread.is_alive()):
            self._guard_stop.clear()
            self._guard_thread = threading.Thread(target=self._guard_loop,
                                                  name="team-room-model-guard", daemon=True)
            self._guard_thread.start()
        # W28-C1: модель до старта — свой поток с интервалом 1 с (закрывает гонку дефекта 5)
        if not (self._preflight_thread and self._preflight_thread.is_alive()):
            self._preflight_stop.clear()
            self._preflight_thread = threading.Thread(target=self._preflight_loop,
                                                      name="team-room-model-preflight", daemon=True)
            self._preflight_thread.start()
        # W25-M2: сторож зависаний (10 мин — предупреждение, 20 — остановка)
        if not (self._wd_thread and self._wd_thread.is_alive()):
            self._wd_stop.clear()
            self._wd_thread = threading.Thread(target=self._watchdog_loop,
                                               name="team-room-watchdog", daemon=True)
            self._wd_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._guard_stop.set()
        self._preflight_stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        guard, self._guard_thread = self._guard_thread, None
        if guard and guard.is_alive():
            guard.join(timeout=3.0)
        pre, self._preflight_thread = self._preflight_thread, None
        if pre and pre.is_alive():
            pre.join(timeout=3.0)
        wd, self._wd_thread = self._wd_thread, None
        if wd and wd.is_alive():
            wd.join(timeout=3.0)
        self.running = False
        self.guard_running = False
        self.preflight_running = False
        self.wd_running = False

    def status(self) -> dict:
        return {"running": bool(self.running), "last_run_at": self.last_run_at,
                "last_error": self.last_error,
                # W23-MG1 §2.3: сводка быстрого сторожа моделей (для /api/health.guard)
                "guard": self.guard_status(),
                # W28-C1: сводка цикла «модель до старта» (для /api/health.preflight)
                "preflight": self.preflight_status(),
                # W25-M2: сводка сторожа зависаний (для /api/health.watchdog)
                "watchdog": self.watchdog_status()}

    def guard_status(self) -> dict:
        """Состояние быстрого сторожа моделей: идёт ли, когда был тик, что сделал."""
        thread = self._guard_thread
        alive = bool(thread is not None and thread.is_alive())
        return {"running": bool(self.guard_running or alive), "tick_s": GUARD_TICK,
                "last_run_at": self.guard_last_run_at, "last_error": self.guard_last_error,
                "stats": dict(self.guard_stats or {})}

    def preflight_status(self) -> dict:
        """Состояние цикла «модель до старта»: идёт ли, когда был проход, что сделал."""
        thread = self._preflight_thread
        alive = bool(thread is not None and thread.is_alive())
        return {"running": bool(self.preflight_running or alive), "tick_s": PREFLIGHT_TICK,
                "last_run_at": self.preflight_last_run_at,
                "last_error": self.preflight_last_error,
                "stats": dict(self.preflight_stats or {})}

    def watchdog_status(self) -> dict:
        """Состояние сторожа зависаний: идёт ли, когда был проход, что сделал."""
        thread = self._wd_thread
        alive = bool(thread is not None and thread.is_alive())
        return {"running": bool(self.wd_running or alive), "tick_s": WATCHDOG_TICK,
                "last_run_at": self.wd_last_run_at, "last_error": self.wd_last_error,
                "stats": dict(self.wd_stats or {})}

    def summary_for(self, room_id):
        return self.cache.get(room_id)

    def summaries(self) -> dict:
        return dict(self.cache)

    # --- цикл ---
    def _loop(self) -> None:
        self.running = True
        try:
            while not self._stop.is_set():
                interval = TICK_IDLE
                try:
                    stats = self.tick_once()
                    interval = TICK_ACTIVE if stats.get("active") else TICK_IDLE
                except Exception as exc:  # noqa: BLE001 - поток не должен умирать
                    self.app.log.error("watcher: тик не удался", exc)
                    self.last_error = ("тик: %s" % (exc or exc.__class__.__name__))[:400]
                self._stop.wait(interval)
        finally:
            self.running = False

    # --- быстрый сторож моделей (W23 §2.3) ---
    def _guard_loop(self) -> None:
        """Свой цикл: раз в `GUARD_TICK` секунд, не дожидаясь общего тика.

        Пауза считается от НАЧАЛА прохода (а не от его конца): медленная доска иначе
        растягивала бы интервал до «проход + 3 с» и сторож переставал быть быстрым.
        """
        self.guard_running = True
        try:
            while not self._guard_stop.is_set():
                started = time.monotonic()
                try:
                    self.guard_tick_once()
                except Exception as exc:  # noqa: BLE001 - поток не должен умирать
                    self.app.log.error("model_guard: тик не удался", exc)
                    self.guard_last_error = ("тик: %s" % (exc or exc.__class__.__name__))[:400]
                pause = GUARD_TICK - (time.monotonic() - started)
                self._guard_stop.wait(max(0.2, pause))
        finally:
            self.guard_running = False

    def _watchdog_loop(self) -> None:
        """Свой цикл сторожа зависаний: пауза от начала прохода, не от конца."""
        self.wd_running = True
        try:
            while not self._wd_stop.is_set():
                started = time.monotonic()
                try:
                    self.watchdog_tick_once()
                except Exception as exc:         # noqa: BLE001 - поток не должен умирать
                    self.app.log.error("watchdog: тик не удался", exc)
                    self.wd_last_error = ("тик: %s" % (exc or exc.__class__.__name__))[:400]
                pause = WATCHDOG_TICK - (time.monotonic() - started)
                self._wd_stop.wait(max(0.5, pause))
        finally:
            self.wd_running = False

    def _preflight_loop(self) -> None:
        """Свой цикл «модель до старта» (W28-C1): пауза от НАЧАЛА прохода, шаг 1 с.

        Медленная доска не должна растягивать интервал: иначе окно «карточка без модели, а
        диспетчер уже смотрит» снова станет секундами — из него и уходили токены на модель
        профиля.
        """
        self.preflight_running = True
        try:
            while not self._preflight_stop.is_set():
                started = time.monotonic()
                try:
                    self.preflight_tick_once()
                except Exception as exc:  # noqa: BLE001 - поток не должен умирать
                    self.app.log.error("model_preflight: тик не удался", exc)
                    self.preflight_last_error = ("тик: %s" % (exc or exc.__class__.__name__))[:400]
                pause = PREFLIGHT_TICK - (time.monotonic() - started)
                self._preflight_stop.wait(max(0.1, pause))
        finally:
            self.preflight_running = False

    def preflight_tick_once(self) -> dict:
        """Один проход «модель до старта» по комнатам (W28-C1).

        Логика — в `model_guard.preflight`: дельта `task_events` + проход по карточкам
        комнаты в `triage/todo/ready`, удержание непроставленных, одна остановка чужого
        запуска. Ошибка комнаты проход не валит: она в `preflight_last_error` и в логе.
        """
        stamp = int(time.time())
        stats = {"rooms": 0, "checked": 0, "pinned": 0, "reclaimed": 0, "held": 0,
                 "released": 0, "errors": 0, "at": stamp}
        self.preflight_last_error = None
        blocked = board_block_reason(self.cfg)
        if blocked:
            stats["skipped_reason"] = "sandbox_real_board"
            stats["skipped_text"] = blocked
            self.preflight_stats = stats
            self.preflight_last_run_at = stamp
            return stats
        try:
            rooms = self._rooms()
        except Exception as exc:                 # noqa: BLE001 - реестр может быть занят
            stats["errors"] += 1
            self.preflight_last_error = ("список комнат: %s" % str(exc))[:400]
            self.preflight_stats = stats
            self.preflight_last_run_at = stamp
            return stats
        try:
            func = self.app.fn("model_guard", "preflight")
        except Exception as exc:                 # noqa: BLE001 - старая сборка: модуля нет
            if not _module_missing(exc):
                stats["errors"] += 1
                self.preflight_last_error = ("модуль контроля: %s" % str(exc))[:400]
            self.preflight_stats = stats
            self.preflight_last_run_at = stamp
            return stats
        budget_end = time.monotonic() + PREFLIGHT_PASS_BUDGET
        for room in rooms:
            if not isinstance(room, dict) or room.get("archived"):
                continue
            if time.monotonic() > budget_end:
                stats["skipped_rooms"] = int(stats.get("skipped_rooms") or 0) + 1
                continue
            stats["rooms"] += 1
            started = time.monotonic()
            try:
                res = func(self.cfg, room, now=stamp) or {}
            except Exception as exc:             # noqa: BLE001 - комната проход не валит
                stats["errors"] += 1
                self.preflight_last_error = ("комната %s: %s" % (room.get("id"), str(exc)))[:400]
                self.app.log.error("model_preflight: комната %s" % room.get("id"), exc)
                continue
            stats["checked"] += int(res.get("checked") or 0)
            stats["pinned"] += len(res.get("pinned") or [])
            stats["reclaimed"] += len(res.get("reclaimed") or [])
            stats["held"] += len(res.get("held") or [])
            stats["released"] += len(res.get("released") or [])
            if res.get("errors"):
                stats["errors"] += 1
                self.preflight_last_error = str((res.get("errors") or [""])[0])[:400]
            took = time.monotonic() - started
            if took > PREFLIGHT_ROOM_BUDGET:
                stats["slow_rooms"] = int(stats.get("slow_rooms") or 0) + 1
                stats["slowest_s"] = round(max(float(stats.get("slowest_s") or 0), took), 2)
        self.preflight_stats = stats
        self.preflight_last_run_at = stamp
        return stats

    def watchdog_tick_once(self) -> dict:
        """Один проход сторожа зависаний по комнатам (W25-M2).

        Логика — в `server/watchdog.tick`: предупредить на 10 мин, остановить
        штатным механизмом W23 на 20 мин. Ошибка комнаты не валит проход.
        """
        stamp = int(time.time())
        stats = {"rooms": 0, "checked": 0, "warned": 0, "stopped": 0, "alive_tools": 0,
                 "errors": 0, "at": stamp}
        self.wd_last_error = None
        try:
            rooms = self._rooms()
        except Exception as exc:                 # noqa: BLE001 - реестр может быть занят
            stats["errors"] += 1
            self.wd_last_error = ("список комнат: %s" % str(exc))[:400]
            self.wd_stats = stats
            self.wd_last_run_at = stamp
            return stats
        try:
            func = self.app.fn("watchdog", "tick")
        except Exception as exc:                 # noqa: BLE001 - старая сборка
            if not _module_missing(exc):
                stats["errors"] += 1
                self.wd_last_error = ("модуль сторожа: %s" % str(exc))[:400]
            self.wd_stats = stats
            self.wd_last_run_at = stamp
            return stats
        for room in rooms:
            if not isinstance(room, dict) or room.get("archived"):
                continue
            stats["rooms"] += 1
            try:
                res = func(self.cfg, room, now=stamp) or {}
            except Exception as exc:             # noqa: BLE001 - комната не валит проход
                stats["errors"] += 1
                self.wd_last_error = ("комната %s: %s" % (room.get("id"), str(exc)))[:400]
                self.app.log.error("watchdog: комната %s" % room.get("id"), exc)
                continue
            stats["checked"] += int(res.get("checked") or 0)
            stats["warned"] += len(res.get("warned") or [])
            stats["stopped"] += len(res.get("stopped") or [])
            stats["alive_tools"] += int(res.get("alive_tools") or 0)
        self.wd_stats = stats
        self.wd_last_run_at = stamp
        return stats

    def guard_tick_once(self) -> dict:
        """Один проход контроля моделей: исправить до запуска, остановить чужой запуск.

        По каждой неархивной комнате зовётся `model_guard.tick` (там вся логика и все
        правила «не трогать чужое/неизвестное»). Ошибка одной комнаты не валит проход:
        она остаётся в `guard_last_error` и в `data/logs/app.log`.
        """
        stamp = int(time.time())
        stats = {"rooms": 0, "fixed": 0, "stopped": 0, "warned": 0, "unknown": 0,
                 "errors": 0, "at": stamp}
        self.guard_last_error = None
        try:
            rooms = self._rooms()
        except Exception as exc:  # noqa: BLE001 - реестр комнат может быть занят
            stats["errors"] += 1
            self.guard_last_error = ("список комнат: %s" % str(exc))[:400]
            self.guard_stats = stats
            self.guard_last_run_at = stamp
            return stats
        budget_end = time.monotonic() + GUARD_PASS_BUDGET
        for room in rooms:
            if not isinstance(room, dict) or room.get("archived"):
                continue
            if time.monotonic() > budget_end:
                # проход длиннее бюджета: не копим долг, остальные комнаты — следующий проход
                stats["skipped_rooms"] = int(stats.get("skipped_rooms") or 0) + 1
                continue
            stats["rooms"] += 1
            room_started = time.monotonic()
            try:
                func = self.app.fn("model_guard", "tick")
            except Exception as exc:  # noqa: BLE001 - модуль ещё не готов (старая сборка)
                if not _module_missing(exc):
                    stats["errors"] += 1
                    self.guard_last_error = ("модуль контроля: %s" % str(exc))[:400]
                break
            try:
                res = func(self.cfg, room, now=stamp) or {}
            except Exception as exc:  # noqa: BLE001 - одна комната не валит проход
                stats["errors"] += 1
                self.guard_last_error = ("комната %s: %s" % (room.get("id"), str(exc)))[:400]
                self.app.log.error("model_guard: комната %s" % room.get("id"), exc)
                continue
            stats["fixed"] += len(res.get("fixed") or [])
            stats["stopped"] += len(res.get("stopped") or [])
            stats["warned"] += len(res.get("warned") or [])
            stats["unknown"] += int(res.get("unknown") or 0)
            if res.get("errors"):
                stats["errors"] += 1
                self.guard_last_error = str((res.get("errors") or [""])[0])[:400]
            took = time.monotonic() - room_started
            if took > GUARD_ROOM_BUDGET:
                # комната не уложилась в бюджет: видно в /api/health.guard, а не «тихо тормозит»
                stats["slow_rooms"] = int(stats.get("slow_rooms") or 0) + 1
                stats["slowest_s"] = round(max(float(stats.get("slowest_s") or 0), took), 2)
        self.guard_stats = stats
        self.guard_last_run_at = stamp
        return stats

    def tick_once(self) -> dict:
        """Один проход. Никогда не бросает исключений наружу (кроме остановки).

        D6: у тика есть бюджет времени (``TICK_BUDGET``) и бюджет на каждое чтение
        (``REQUEST_BUDGET``) — блокировка БД не превращает тик в 33-секундное зависание.
        """
        now = int(time.time())
        today0 = local_midnight(now)
        tick_deadline = time.monotonic() + TICK_BUDGET
        stats = {"rooms": 0, "requests": 0, "ingested": 0, "steps": 0, "errors": 0,
                 "active": False, "at": now}
        blocked = board_block_reason(self.cfg)
        if blocked:
            # W29-I1: песочница с реальной доской — ни карточек, ни моделей, ни записей.
            stats["skipped_reason"] = "sandbox_real_board"
            stats["skipped_text"] = blocked
            self.last_stats = stats
            self.last_run_at = now
            return stats
        # W29B-Q6: факты для уведомлений собираем по ходу прохода и отдаём в
        # ``notify.tick`` ОДНИМ вызовом в конце тика (правила и канал — там).
        facts: list = []
        self.last_stats = stats
        self.last_error = None
        try:
            rooms = self._rooms()
        except Exception as exc:  # noqa: BLE001 - модуль комнат может быть не готов
            self._fail("список комнат", exc)
            stats["errors"] += 1
            self.last_run_at = now
            return stats
        for room in rooms:
            if not isinstance(room, dict) or room.get("archived"):
                continue
            stats["rooms"] += 1
            room_id = room.get("id")
            counters: dict = {"working": 0, "needs_you": 0, "done_today": 0}
            need_roots: list = []          # W29B-Q6: какие запросы комнаты ждут человека
            phase = None
            changed = False
            # 0) модель участников комнаты (WAVE10/13): ДО чтения запросов и до диспетчера.
            #    Руководитель создаёт карточки сам (`hermes kanban create` без --model),
            #    а движок берёт переопределение только в момент допуска запуска, поэтому
            #    закрепляем выбор комнаты на всех её карточках как можно раньше — и сразу
            #    сверяем факт с журналом запусков (`verify_started`).
            self._agent_models_room(room, stats)
            try:
                records = self._records(room_id)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                self._fail("реестр комнаты %s" % room_id, exc)
                continue
            records = sorted(records, key=lambda rec: (rec.get("created_at") or 0), reverse=True)
            for rec in records[:MAX_REQUESTS]:
                root_id = rec.get("root_id")
                if not root_id:
                    continue
                if time.monotonic() >= tick_deadline:
                    stats["truncated"] = True
                    stats["errors"] += 1
                    self._fail("тик", RuntimeError("бюджет времени тика исчерпан (%.0f с)" % TICK_BUDGET))
                    break
                stats["requests"] += 1
                try:
                    with self._deadline(REQUEST_BUDGET):
                        detail = self._detail(room, root_id, rec)
                    status = detail.get("status") or "queued"
                    needs_you = bool(detail.get("needs_you"))
                    # W29-P4: архивный запрос — ПОЛНАЯ остановка. Ни цикла критика,
                    # ни свода, ни названий, ни «Хода работы», ни ingest: ни одного
                    # вызова модели и никаких новых карточек. Счётчики его не считают.
                    # Проверка именно по флагу: статус архивного корня вычисляется
                    # отдельно и не обязан быть `archived`.
                    if detail.get("archived") or status == "archived":
                        stats["archived"] = int(stats.get("archived") or 0) + 1
                        continue
                    if status in ("working", "reviewing", "synthesizing"):
                        counters["working"] += 1
                        stats["active"] = True
                    elif status == "needs_you":
                        counters["needs_you"] += 1
                    elif status == "done":
                        # завершённый запрос с неотвечённым вопросом Михаилу тоже «ждёт тебя»
                        if needs_you:
                            counters["needs_you"] += 1
                        if int(detail.get("finished_at") or 0) >= today0:
                            counters["done_today"] += 1
                    if phase is None:
                        phase = phase_of(detail)
                    # W29B-Q6: факт для уведомлений — состояние корня на этом проходе.
                    # Новые события (завершён, ошибка) вычисляет `notify` сравнением
                    # с прошлым проходом: здесь только снимок, никакой логики.
                    facts.append({"kind": "root", "room": room_id, "room_name": room.get("name") or "",
                                  "root": root_id, "status": status,
                                  "error": detail.get("error") or rec.get("last_error") or "",
                                  "title": detail.get("title_short") or detail.get("title") or "",
                                  "needs_you": needs_you})
                    if needs_you:
                        need_roots.append(str(root_id))
                    # 1) цикл критика — ДЛЯ ВСЕХ запросов (иначе пост-синтезный критик
                    #    не стартует на завершённом корне); ошибки не валят тик
                    try:
                        steps = self.app.fn("critic_loop", "run_step")(self.cfg, room, rec, detail)
                        steps = steps if isinstance(steps, list) else []
                        stats["steps"] += len(steps)
                        if steps:
                            stats["active"] = True
                    except Exception as exc:  # noqa: BLE001 - цикл не должен ронять ingest
                        stats["errors"] += 1
                        self._fail("цикл критика %s (%s)" % (root_id, room_id), exc)
                    # 2) ingest версии ответа (ровно один раз на run_id последней версии)
                    #    в режиме «до полного одобрения критиком» версия не публикуется,
                    #    пока критик не сказал approve (W13-E §9): спрашиваем critic_loop
                    if not self._delivery_hold(room, rec, detail) and self._ingest(room, root_id, rec, detail):
                        stats["ingested"] += 1
                        changed = True
                    # 3) заметка запроса в Obsidian — при изменении состояния/диалога
                    key = (str(room_id), str(root_id))
                    signature = self._signature(detail, rec)
                    if self._synced.get(key) != signature:
                        try:
                            self._sync_request(room_id, root_id, detail)
                            self._synced[key] = signature
                            changed = True
                        except Exception as exc:  # noqa: BLE001 - заметка вторична
                            stats["errors"] += 1
                            self._fail("заметка Obsidian %s (%s)" % (root_id, room_id), exc)
                    # 4) особые указания участникам комнаты — автокомментарии (WAVE9 §9)
                    self._agent_notes(room, root_id, stats)
                    # 5) название запроса по ходу беседы (W17-B §4, B9 усилено):
                    #    после выпуска свода/уточнений, не чаще REFRESH_MIN_S,
                    #    user-название не трогается. Ошибка не валит тик.
                    try:
                        self.app.fn("titles", "maybe_refresh")(self.cfg, room, rec, detail)
                    except Exception as exc:  # noqa: BLE001 - название вторично
                        self._fail("название %s (%s)" % (root_id, room_id), exc)
                    # 6) B-35: отметка «оценил работу» после завершения, иначе ОДНО
                    #    напоминание комментарием в ветке. Модели не запускаются.
                    self._leader_duty(room, root_id, detail, stats)
                    # 7) W20-S1 §1.1 (B-36): живой свод — после «тишины» и не чаще
                    #    минимального интервала ставим карточку обновления. Модели
                    #    приложение не запускает: карточку берёт обычный движок.
                    try:
                        tick = self.app.fn("svod_live", "tick")
                        svod = tick(self.cfg, room, rec, detail, log=self.app.log)
                        if isinstance(svod, dict) and svod.get("card_id"):
                            stats["svod_cards"] = int(stats.get("svod_cards") or 0) + 1
                            stats["active"] = True
                    except Exception as exc:  # noqa: BLE001 - свод не должен ронять тик
                        if not _module_missing(exc):
                            stats["errors"] += 1
                            self._fail("живой свод %s (%s)" % (root_id, room_id), exc)
                    # 8) W20 §1.6: промежуточный итог «Хода работы» — сторож без модели
                    #    (0 токенов), раз в digest_every_min минут, и пометки «возможно завис».
                    self._worklog_digest(room, rec, detail, stats)
                except Exception as exc:  # noqa: BLE001 - один запрос не валит цикл
                    stats["errors"] += 1
                    self._fail("запрос %s (%s)" % (root_id, room_id), exc)
                    continue
            if changed or room_id not in self._mirrored:
                try:
                    self._sync_room(room_id)
                    self._mirrored.add(room_id)
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    self._fail("зеркало Vault %s" % room_id, exc)
            # 3.5) W22-AP1 §2.2: снять временные разрешения («один раз»), которые
            #      истекли или чья карточка завершилась. Модели не запускаются,
            #      подпроцесс Hermes — только когда срок действительно вышел.
            self._approvals_sweep(room, stats)
            # 3.6) W27-E7 §3: результаты дежурств комнаты — тихая строка в журнале или
            #      карточка «Что выполнено / Что обнаружено / Предлагаю» (лимит ≤ 5 за тик,
            #      идемпотентность по run_id, наружу ничего не уходит).
            duty_res = self._duties_tick(room, stats)
            if isinstance(duty_res, dict) and int(duty_res.get("cards") or 0):
                # W29B-Q6: дежурство нашло изменения (создана карточка-результат)
                facts.append({"kind": "duty", "room": room_id, "room_name": room.get("name") or "",
                              "job": duty_res.get("job_id") or "", "run": duty_res.get("run_id") or "",
                              "title": "найдены изменения", "count": int(duty_res.get("cards") or 0)})
            # W29B-Q6: «ждёт ваш ответ: N» — одним фактом на комнату (счётчик из прохода)
            if counters["needs_you"]:
                facts.append({"kind": "waiting", "room": room_id, "room_name": room.get("name") or "",
                              "count": int(counters["needs_you"]), "roots": need_roots,
                              "title": ""})
            # 4) карта проекта (WAVE 9): очередь направлений — одна строка, ошибки не валят тик
            try:
                self.app.call("plan", "tick", ((self.cfg, room_id), {"room": room}))
            except Exception as exc:  # noqa: BLE001 - модуль может быть не готов
                if not _module_missing(exc):
                    stats["errors"] += 1
                    self._fail("карта проекта %s" % room_id, exc)
            # 4.5) W29B-Q1: рецепты — шаги, которые ждали предшественника, создаются,
            #      когда тот готов. Без запусков в работе ничего не делает.
            try:
                stats["recipe_cards"] = int(stats.get("recipe_cards") or 0) + int(
                    (self.app.fn("recipes", "tick")(self.cfg, room) or {}).get("created") or 0)
            except Exception as exc:  # noqa: BLE001 - рецепты не должны ронять тик
                if not _module_missing(exc):
                    stats["errors"] += 1
                    self._fail("рецепты %s" % room_id, exc)
            self.cache[room_id] = {"counters": counters, "phase": phase, "updated_at": now}
        # W29B-Q6: ОДИН вызов уведомлений за проход (правила, тихие часы, дедупликация,
        # канал — server/notify.py). Ошибка канала не валит тик и не мешает счётчикам.
        self._notify_tick(facts, stats)
        self.last_run_at = now
        return stats

    def _notify_tick(self, facts: list, stats: dict) -> None:
        """Отдать факты прохода в ``server/notify.tick`` (W29B-Q6).

        Модуля нет — молча пропускаем: уведомления необязательны и не должны
        ломать основной проход. В песочнице канал — ``TEAMROOM_NOTIFY_FAKE``.
        """
        try:
            func = self.app.fn("notify", "tick")
        except Exception:                       # noqa: BLE001 - модуль может быть не готов
            return
        try:
            res = func(self.cfg, facts, port=getattr(self.app, "port", None),
                       log=getattr(self.app, "log", None))
        except Exception as exc:                # noqa: BLE001 - уведомления вторичны
            stats["errors"] += 1
            self._fail("уведомления", exc)
            return
        if isinstance(res, dict):
            if res.get("sent"):
                stats["notified"] = int(stats.get("notified") or 0) + int(res["sent"])
            self.notify_stats = {"at": int(time.time()), "channel": res.get("channel"),
                                 "sent": int(res.get("sent") or 0),
                                 "skipped": dict(res.get("skipped") or {})}

    # --- шаги ---
    def _deadline(self, seconds: float):
        """Бюджет времени на одно чтение движка (D6): есть у App, у заглушек — нет."""
        factory = getattr(self.app, "_engine_deadline", None)
        if callable(factory):
            return factory(seconds)
        return contextlib.nullcontext()

    def _rooms(self) -> list:
        rows = self.app.call("rooms", "list_rooms", ((self.cfg,), {}))
        if isinstance(rows, dict):
            rows = rows.get("rooms") or rows.get("items") or []
        return [room for room in rows or [] if isinstance(room, dict)]

    def _records(self, room_id) -> list:
        rows = self.app.call("registry", "list_requests",
                             ((self.cfg, room_id), {}), ((self.cfg,), {"room_id": room_id}),
                             ((self.cfg,), {}))
        if isinstance(rows, dict):
            rows = rows.get("requests") or rows.get("items") or []
        out = []
        for rec in rows or []:
            if not isinstance(rec, dict):
                continue
            if rec.get("room") and rec.get("room") != room_id:
                continue
            out.append(rec)
        return out

    def _detail(self, room: dict, root_id: str, rec: dict) -> dict:
        board = room.get("board") or self.app.board
        extra = list(rec.get("extra_cards") or [])
        answers = rec.get("answers") if isinstance(rec.get("answers"), dict) else {}
        detail = self.app.call("engine_read", "request_detail",
                               ((self.cfg, board, root_id, room, extra),
                                {"answers": answers}),
                               ((self.cfg, board, root_id, room, extra), {}),
                               ((self.cfg, board, root_id, room.get("id"), extra), {}),
                               ((self.cfg, board, root_id, room.get("id")), {}))
        if not isinstance(detail, dict) or not detail:
            raise RuntimeError("engine_read.request_detail вернул пустой ответ")
        # W22-AP1 §2.1: «Нужен ваш ответ» и счётчики комнаты учитывают и ожидающие
        # разрешения — иначе бейдж вкладки не загорится, пока участник стоит.
        view = getattr(self.app, "_approvals_view", None)
        if callable(view):
            try:
                view(room, rec, detail)
            except Exception as exc:                 # noqa: BLE001 - разрешения вторичны
                self._fail("разрешения %s (%s)" % (root_id, room.get("id")), exc)
        return detail

    def _delivery_hold(self, room: dict, rec: dict, detail: dict) -> bool:
        """Версию ответа пока нельзя публиковать (режим «до полного одобрения»).

        Решение принимает critic_loop; модуля нет или он не умеет — False (прежнее
        поведение: ответ публикуется как только готов).
        """
        try:
            func = self.app.fn("critic_loop", "delivery_hold")
        except Exception:  # noqa: BLE001 - модуль ещё не готов
            return False
        try:
            return bool(func(self.cfg, room, rec, detail))
        except Exception:  # noqa: BLE001 - сбой проверки не должен блокировать ingest
            return False

    def _ingest(self, room: dict, root_id: str, rec: dict, detail: dict) -> bool:
        """Записать версию ответа в здание проекта ровно один раз (ключ — run_id версии)."""
        syn = detail.get("synthesis") or {}
        if not isinstance(syn, dict) or syn.get("state") != "ready":
            return False
        key = self._version_key(detail)
        if not key:
            return False
        if key in (rec.get("ingested") or {}):
            return False
        res = self.app.fn("ingest", "ingest_request")(self.cfg, room, detail)
        # отметку ставит только ingest (единый писатель) — watcher её лишь перечитывает
        return bool((res or {}).get("changed")) or not (res or {}).get("skipped")

    # --- зеркало Obsidian (Блок C волны 8) ---
    def _sync_request(self, room_id, root_id, detail) -> bool:
        """Заметка запроса; модуля нет (старый сервер/заглушка) — тихо пропускаем."""
        try:
            func = self.app.fn("vault_mirror", "sync_request")
        except Exception:  # noqa: BLE001 - модуль ещё не готов
            return False
        func(self.cfg, room_id, root_id, detail)
        return True

    def _sync_room(self, room_id) -> None:
        """CONTEXT.md + STATE.md/Журнал.md; без vault_mirror — прежний ingest."""
        try:
            func = self.app.fn("vault_mirror", "sync_room")
        except Exception:  # noqa: BLE001 - модуль ещё не готов
            func = None
        if func is not None:
            func(self.cfg, room_id)
            return
        self.app.fn("ingest", "write_vault_mirror")(self.cfg, room_id)

    def _agent_notes(self, room: dict, root_id: str, stats: dict) -> None:
        """Автокомментарии `[Указание комнаты · hash8]` на карточки дерева (WAVE9 §9).

        Лениво и идемпотентно: модуля нет, указаний нет, ошибка CLI — тихо
        пропускаем; всё уже стоящее помечено маркером и повторно не пишется.
        """
        try:
            func = self.app.fn("agent_notes", "ensure_comments")
        except Exception:  # noqa: BLE001 - модуль ещё не готов
            return
        try:
            res = func(self.cfg, room, root_id) or {}
        except Exception as exc:  # noqa: BLE001 - указания не роняют тик
            stats["errors"] += 1
            self._fail("указания комнаты %s (%s)" % (root_id, room.get("id")), exc)
            return
        if res.get("written"):
            stats["notes_written"] = int(stats.get("notes_written") or 0) + len(res["written"])

    def _leader_duty(self, room: dict, root_id: str, detail: dict, stats: dict) -> None:
        """B-35: «оценил работу» после завершения, иначе ОДНО напоминание руководителю.

        После завершения запроса смотрим metadata карточек: отметку ``leader_review``
        ставит руководитель в своей карточке (корень или доработка). Есть — пишем
        событие в журнал комнаты. Нет — один комментарий-напоминание на корень (тот же
        механизм комментариев, что у «Напомнить руководителю»), без карточек, без
        вопросов Михаилу и без запуска моделей: комментарий ничего не диспатчит.
        Ошибки не роняют тик.
        """
        if str(detail.get("status") or "") != "done":
            return
        finished = int(detail.get("finished_at") or 0)
        if not finished:
            return                     # время завершения неизвестно — не рискуем рассылкой
        if int(time.time()) - finished < DUTY_GRACE_S:
            return
        since = int(getattr(self, "_duty_since", 0) or 0)
        if since and finished < since - DUTY_START_LOOKBACK_S:
            return                     # завершено до запуска приложения — не напоминаем
        try:
            record = self.app.fn("agent_notes", "record_leader_review")
            status_of = self.app.fn("agent_notes", "leader_review_status")
        except Exception as exc:  # noqa: BLE001 - модуль ещё не готов
            if not _module_missing(exc):
                stats["errors"] += 1
                self._fail("отметка об оценке (%s)" % root_id, exc)
            return
        room_id = str(room.get("id") or "")
        try:
            mark = record(self.cfg, room_id, root_id, detail)
        except Exception as exc:  # noqa: BLE001 - журнал вторичен для тика
            stats["errors"] += 1
            self._fail("отметка об оценке (%s)" % root_id, exc)
            return
        if mark:
            stats["leader_marks"] = int(stats.get("leader_marks") or 0) + 1
            return
        try:
            if status_of(self.cfg, room_id, root_id).get("done"):
                return
            present = self.app.fn("agent_notes", "reminder_present")
            text_of = self.app.fn("agent_notes", "reminder_text")
            log_reminder = self.app.fn("agent_notes", "log_leader_reminder")
        except Exception as exc:  # noqa: BLE001 - модуль ещё не готов
            if not _module_missing(exc):
                stats["errors"] += 1
                self._fail("напоминание руководителю (%s)" % root_id, exc)
            return
        try:
            if present(self.cfg, room, root_id):
                return
        except Exception:  # noqa: BLE001 - проверка вторична, попробуем поставить
            pass
        lead = str(room.get("orchestrator") or "").strip()
        if not lead:
            return
        board = room.get("board") or getattr(self.app, "board", "team")
        text = text_of(str(room.get("title") or room_id or ""))
        try:
            self.app.fn("engine_write", "act")(self.cfg, board, root_id, "comment",
                                               text=text, task_id=root_id, author=lead)
        except Exception as exc:  # noqa: BLE001 - без комментария напоминания нет
            stats["errors"] += 1
            self._fail("напоминание руководителю (%s)" % root_id, exc)
            return
        version = 0
        synthesis = detail.get("synthesis")
        if isinstance(synthesis, dict):
            try:
                version = int(synthesis.get("version") or 0)
            except (TypeError, ValueError):
                version = 0
        try:
            log_reminder(self.cfg, room_id, root_id, version=version, text=text)
        except Exception as exc:  # noqa: BLE001 - журнал вторичен, комментарий уже есть
            stats["errors"] += 1
            self._fail("журнал напоминания (%s)" % root_id, exc)
        stats["leader_reminders"] = int(stats.get("leader_reminders") or 0) + 1

    def _worklog_digest(self, room: dict, rec: dict, detail: dict, stats: dict) -> None:
        """Сторож-дайджест «Хода работы» (W20 §1.6): промежуточный итог без модели.

        Пока запрос в работе, раз в ``digest_every_min`` минут (настройка комнаты) в ленту
        кладётся системная запись «Прошло 6 мин. Работают: … Готово 2 из 5.» — она собирается
        из событий и счётчиков, моделей не запускает и агентам не отправляется. Тот же шаг
        находит карточки без активности дольше 15 минут («возможно завис»).

        Настройка «Выключено» (``worklog_mode=off``) гасит экран, но не этот итог:
        частоту задаёт отдельное поле ``digest_every_min`` (0 — выключить совсем).
        Ошибка шага не валит тик.
        """
        try:
            step = self.app.fn("worklog", "digest_step")
        except Exception as exc:  # noqa: BLE001 - модуль ещё не готов (старая сборка)
            if not _module_missing(exc):
                stats["errors"] += 1
                self._fail("ход работы (%s)" % (rec or {}).get("root_id"), exc)
            return
        try:
            res = step(self.cfg, room, rec, detail) or {}
        except Exception as exc:  # noqa: BLE001 - итог вторичен для тика
            stats["errors"] += 1
            self._fail("дайджест хода работы (%s)" % (rec or {}).get("root_id"), exc)
            return
        if res.get("digest"):
            stats["worklog_digest"] = int(stats.get("worklog_digest") or 0) + 1
        if res.get("stuck"):
            stats["worklog_stuck"] = int(stats.get("worklog_stuck") or 0) + len(res["stuck"])

    def _approvals_sweep(self, room: dict, stats: dict) -> None:
        """Снять истёкшие «разовые» разрешения (W22-AP1 §2.2, server/approvals.sweep).

        Разрешение «один раз» живёт до конца карточки или 30 минут — иначе временная
        запись осталась бы в профиле навсегда. Ошибка шага не валит тик.
        """
        try:
            func = self.app.fn("approvals", "sweep")
        except Exception as exc:                        # noqa: BLE001 - старaя сборка
            if not _module_missing(exc):
                stats["errors"] += 1
                self._fail("разрешения комнаты %s" % (room or {}).get("id"), exc)
            return
        try:
            res = func(self.cfg, room) or {}
        except Exception as exc:                        # noqa: BLE001 - разрешения вторичны
            stats["errors"] += 1
            self._fail("снятие разрешений %s" % (room or {}).get("id"), exc)
            return
        if int(res.get("revoked") or 0):
            stats["approvals_revoked"] = int(stats.get("approvals_revoked") or 0) + int(res["revoked"])

    def _duties_tick(self, room: dict, stats: dict) -> dict:
        """Результаты дежурств комнаты → журнал / «Входящие» / Дело (W27-E7 §3).

        Дешёвая операция без моделей: читаем свежие выводы привязанных заданий,
        разбираем блок `результат` и по правилу комнаты кладём карточку. Лимит
        и идемпотентность — внутри `duty_rules.process_runs`; ошибка шага тик не валит.
        Возвращает сводку прохода (``cards`` — дежурство нашло изменения; W29B-Q6).
        """
        try:
            func = self.app.fn("duty_rules", "process_runs")
        except Exception as exc:                        # noqa: BLE001 - старая сборка
            if not _module_missing(exc):
                stats["errors"] += 1
                self._fail("дежурства комнаты %s" % (room or {}).get("id"), exc)
            return {}
        try:
            res = func(self.cfg, room) or {}
        except Exception as exc:                        # noqa: BLE001 - дежурства вторичны
            stats["errors"] += 1
            self._fail("результаты дежурств %s" % (room or {}).get("id"), exc)
            return {}
        for key, name in (("processed", "duty_runs"), ("cards", "duty_cards"),
                          ("self", "duty_self")):
            if int(res.get(key) or 0):
                stats[name] = int(stats.get(name) or 0) + int(res[key])
        return res

    def _agent_models_room(self, room: dict, stats: dict) -> None:
        """Модель комнаты на ВСЕ её карточки + сверка факта (WAVE10, WAVE13 пакет A).
        Один проход по tenant комнаты за тик (а не по деревьям запросов): карточки,
        которые руководитель создаёт сам, в деревьях реестра не видны — из-за этого
        работа уходила на модель профиля. Затем `verify_started` сверяет модель
        реальных запусков с выбором комнаты и возвращает «не ту» карточку на нужную.
        Модуля нет, замен нет, ошибка моста — тихо пропускаем, тик не рушим.
        """
        try:
            apply_room = self.app.fn("agent_models", "apply_to_room")
        except Exception:  # noqa: BLE001 - модуль ещё не готов
            return
        try:
            res = apply_room(self.cfg, room.get("id")) or {}
            if res.get("updated"):
                stats["models_applied"] = int(stats.get("models_applied") or 0) + len(res["updated"])
        except Exception as exc:  # noqa: BLE001 - модель вторична для тика
            stats["errors"] += 1
            self._fail("модель комнаты (%s)" % room.get("id"), exc)
        try:
            verify = self.app.fn("agent_models", "verify_started")
        except Exception:  # noqa: BLE001 - модуль ещё не готов
            return
        try:
            check = verify(self.cfg, room.get("id")) or {}
            if check.get("mismatched"):
                stats["models_mismatched"] = int(stats.get("models_mismatched") or 0) + \
                    len(check["mismatched"])
            if check.get("fixed"):
                stats["models_fixed"] = int(stats.get("models_fixed") or 0) + len(check["fixed"])
        except Exception as exc:  # noqa: BLE001 - сверка модели не валит тик
            stats["errors"] += 1
            self._fail("проверка модели (%s)" % room.get("id"), exc)

    @staticmethod
    def _signature(detail: dict, rec: dict) -> tuple:
        """Что меняет заметку: статус, версия ответа, шаги/вопросы, дерево, лента."""
        syn = detail.get("synthesis") or {}
        thread = detail.get("thread")
        return (
            str(detail.get("status") or ""),
            str(syn.get("state") or ""),
            int(syn.get("version") or 0),
            int(syn.get("at") or 0),
            int(detail.get("finished_at") or 0),
            len(detail.get("next_steps") or []),
            len(detail.get("open_questions") or []),
            len(detail.get("decisions") or []),
            len(detail.get("tree") or []),
            len(thread) if isinstance(thread, list) else -1,
            int((rec or {}).get("version") or 0),
        )

    def _version_key(self, detail: dict) -> str:
        """Ключ версии ответа: ingest.version_key, иначе — реестровый фолбэк."""
        try:
            func = self.app.fn("ingest", "version_key")
            return str(func(detail) or "")
        except Exception:  # noqa: BLE001 - модуль может быть старой версии
            syn = detail.get("synthesis") or {}
            return str(syn.get("at") or detail.get("finished_at") or "")

    def _fail(self, label: str, exc: BaseException) -> None:
        message = "%s: %s" % (label, (str(exc) or exc.__class__.__name__))[:300]
        self.last_error = message[:400]
        self.app.log.error("watcher: " + message, exc)
