"""Фоновый наблюдатель «Комнаты команды».

Раз в 5 с (15 с, если ничего не идёт) по каждой неархивной комнате:
* берёт последние запросы из реестра;
* читает ``engine_read.request_detail`` для незавершённых и недавно завершённых;
* зовёт ``critic_loop.run_step`` (retarget / критик / доработка);
* закрепляет выбор модели комнаты на ВСЕХ её карточках по tenant и сверяет фактическую
  модель идущих запусков (``agent_models.apply_to_room`` + ``verify_started``) — до того,
  как движок успеет взять карточку на модель профиля;
* для завершённых — ``ingest.ingest_request`` + ``ingest.write_vault_mirror`` (идемпотентно);
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
        #: B-35: с какого момента напоминаем об оценке работы (без ретро-рассылки)
        self._duty_since = int(time.time())

    # --- публичное ---
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="team-room-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self.running = False

    def status(self) -> dict:
        return {"running": bool(self.running), "last_run_at": self.last_run_at,
                "last_error": self.last_error}

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
            # 4) карта проекта (WAVE 9): очередь направлений — одна строка, ошибки не валят тик
            try:
                self.app.call("plan", "tick", ((self.cfg, room_id), {"room": room}))
            except Exception as exc:  # noqa: BLE001 - модуль может быть не готов
                if not _module_missing(exc):
                    stats["errors"] += 1
                    self._fail("карта проекта %s" % room_id, exc)
            self.cache[room_id] = {"counters": counters, "phase": phase, "updated_at": now}
        self.last_run_at = now
        return stats

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
