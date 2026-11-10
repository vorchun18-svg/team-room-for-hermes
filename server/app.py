"""HTTP-приложение «Комната команды»: роутинг API, статика, watcher, запуск.

Публично: ``create_app(cfg, start_watcher=True)`` → сервер с ``.serve_forever()``
и ``.shutdown()``. Модули домена (engine_read, engine_write, rooms, ingest, …)
подключаются лениво: пока файла нет, эндпоинт честно отвечает 501 ``engine_error``
«Модуль X ещё не готов», а не падает.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import importlib
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

_SERVER_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SERVER_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import server.config as config                      # noqa: E402
import server.http_util as http_util                # noqa: E402
import server.phase_book as phase_book              # noqa: E402
from server.http_util import ApiError, Raw, err     # noqa: E402
from server.watcher import Watcher                  # noqa: E402
from server.routes_read import ReadRoutes           # noqa: E402
from server.routes_write import WriteRoutes         # noqa: E402
from server.routes_stt import SttRoutes, body_policy as stt_body_policy   # noqa: E402
from server.guard import GuardRoutes                # noqa: E402

from server.app_common import (                    # noqa: E402
    ACTION_ALLOWED, ACTION_REQUIRED, ACTION_TEXT, APP_NAME, BACKLOG_KINDS, BACKLOG_STATUSES,
    MAX_ATTACH, MAX_NOTE, MAX_TEXT, PATCH_FIELDS, ROOM_STATUSES, _backlog_counts, decode_cursor,
    derive_title, encode_cursor, make_slug, preview, try_call,
)
from server.title_short import short_title          # noqa: E402  # W17-B §4

DEFAULT_BOARD = os.environ.get("TEAMROOM_BOARD") or "team"
MAX_THREADS = 64             # D11: предел одновременных соединений (сверх — 503)

# --- реальное наличие CLI (D15) --------------------------------------------
CLI_CACHE_TTL = 60.0         # сек: не дёргаем filesystem на каждый /api/health
_CLI_CACHE: dict[str, tuple[float, bool]] = {}
_CLI_LOCK = threading.Lock()


def cli_available(command: str) -> bool:
    """Есть ли исполняемый файл ``hermes`` на самом деле (кеш 60 с).

    ``shutil.which`` для имени из PATH, ``Path.is_file()`` для абсолютного пути.
    """
    command = str(command or "").strip()
    if not command:
        return False
    now = time.monotonic()
    with _CLI_LOCK:
        cached = _CLI_CACHE.get(command)
        if cached is not None and now - cached[0] < CLI_CACHE_TTL:
            return cached[1]
    try:
        if os.path.isabs(command) or "/" in command or "\\" in command:
            found = Path(command).is_file()
        else:
            found = bool(shutil.which(command))
    except (OSError, ValueError):
        found = False
    with _CLI_LOCK:
        _CLI_CACHE[command] = (now, bool(found))
    return bool(found)


# --- приложение ------------------------------------------------------------
class App(GuardRoutes, ReadRoutes, WriteRoutes, SttRoutes):
    """Доменные обработчики + таблица маршрутов. Один экземпляр на сервер.

    Хендлеры разложены по миксинам: страж — ``guard.GuardRoutes`` (валидация тела
    комнаты/запроса и дедупликация POST /requests), чтение — ``routes_read.ReadRoutes``,
    запись — ``routes_write.WriteRoutes``, диктовка — ``routes_stt.SttRoutes``;
    общие помощники — здесь.
    """

    def __init__(self, cfg, port: int):
        self.cfg = cfg
        self.port = int(port)
        self.board = DEFAULT_BOARD
        self.started_at = int(time.time())
        self.log = http_util.AppLog(cfg.log_dir / "app.log")
        self.watcher = Watcher(self)
        self._mods: dict = {}

    # --- подключение модулей ---
    def _load(self, name: str):
        return importlib.import_module("server." + name)

    def mod(self, name: str):
        cached = self._mods.get(name)
        if cached is not None:
            return cached
        try:
            mod = self._load(name)
        except ModuleNotFoundError as exc:
            if exc.name in ("server." + name, name):
                raise ApiError(501, "Модуль %s ещё не готов" % name,
                               detail={"module": name}) from exc
            self.log.error("модуль %s не загрузился" % name, exc)
            raise ApiError(500, "Модуль %s не загрузился" % name,
                           detail={"module": name, "error": str(exc)[:300]}) from exc
        except Exception as exc:  # noqa: BLE001 - битый модуль не должен ронять сервер
            self.log.error("модуль %s не загрузился" % name, exc)
            raise ApiError(500, "Модуль %s не загрузился" % name,
                           detail={"module": name, "error": str(exc)[:300]}) from exc
        self._mods[name] = mod
        return mod

    def fn(self, name: str, attr: str):
        mod = self.mod(name)
        func = getattr(mod, attr, None)
        if not callable(func):
            raise ApiError(501, "Модуль %s не реализует %s" % (name, attr),
                           detail={"module": name, "function": attr})
        return func

    def _has_mod(self, name: str) -> bool:
        """Есть ли модуль (без исключения, если его ещё пишут)."""
        if name in self._mods:
            return True
        try:
            self._mods[name] = self._load(name)
            return True
        except Exception:  # noqa: BLE001 - модуль может быть не готов
            return False

    def call(self, name: str, attr: str, *attempts):
        func = self.fn(name, attr)
        try:
            return try_call(func, *attempts)
        except TypeError as exc:
            self.log.error("сигнатура %s.%s не совпала" % (name, attr), exc)
            raise ApiError(501, "Модуль %s: не удалось вызвать %s" % (name, attr),
                           detail={"module": name, "function": attr, "error": str(exc)[:300]}) from exc

    @contextlib.contextmanager
    def engine(self):
        """Ошибки движка и доменных модулей → корректные HTTP-коды API.md §1.

        Понимает EngineWriteError(code, message_ru, stderr_tail), EngineError(code,
        message, detail) из engine_db, StateUnavailable из statefiles, RoomsError(message,
        code, detail) и обычные исключения (→ 500 engine_error).
        """
        try:
            yield
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - доменные ошибки движка
            mapped = self.api_error_from(exc)
            if mapped is not None:
                self.log.error("движок: %s" % mapped.message, exc)
                raise mapped from exc
            message = (getattr(exc, "message_ru", None) or getattr(exc, "message", None)
                       or str(exc) or "Ошибка движка канбана")
            code = getattr(exc, "code", None)
            detail = getattr(exc, "detail", None)
            if not isinstance(detail, dict):
                detail = {}
            tail = getattr(exc, "stderr_tail", None)
            if tail:
                detail["stderr"] = str(tail)[-2000:]
            if code in ("bad_request", "forbidden", "not_found", "conflict"):
                status = {"bad_request": 400, "forbidden": 403, "not_found": 404, "conflict": 409}[code]
            else:
                status, code = 500, "engine_error"
            self.log.error("движок: %s" % message, exc)
            raise ApiError(status, message, code=code, detail=detail) from exc

    def api_error_from(self, exc: BaseException) -> ApiError | None:
        """Доменная ошибка → ApiError с кодом из API.md §1; None — не наш тип (→ 500).

        D6: блокировка/сбой SQLite — 503 ``engine_busy``/``engine_error`` с русским
        сообщением вместо «Внутренней ошибки сервера». D4/D5: занятый файл состояния —
        503 ``engine_error`` (данные не подменяются). Схема движка — 502.
        """
        code = getattr(exc, "code", None)
        message = (getattr(exc, "message_ru", None) or getattr(exc, "message", None))
        if not isinstance(code, str) or not message:
            return None
        if not hasattr(exc, "as_dict"):
            # EngineWriteError/RoomsError разбирает engine() по-старому (их коды — доменные)
            return None
        detail = getattr(exc, "detail", None)
        detail = dict(detail) if isinstance(detail, dict) else {}
        tail = getattr(exc, "stderr_tail", None)
        if tail:
            detail.setdefault("stderr", str(tail)[-2000:])
        if code == "db_busy":
            return ApiError(503, message, code="engine_busy", detail=detail)
        if code in ("db_error", "db_unreadable", "no_db", "engine_error"):
            return ApiError(503, message, code="engine_error", detail=detail)
        if code == "schema_drift":
            return ApiError(502, message, code="engine_error", detail=detail)
        if code in ("bad_request", "forbidden", "not_found", "conflict"):
            status = {"bad_request": 400, "forbidden": 403, "not_found": 404, "conflict": 409}[code]
            return ApiError(status, message, code=code, detail=detail)
        return None

    # --- запуск/остановка ---
    def start_watcher(self) -> None:
        self.watcher.start()

    def stop(self) -> None:
        self.watcher.stop()
        try:                      # диктовка: выгрузить модель и погасить рабочий процесс
            self.mod("stt").shutdown()
        except Exception:         # noqa: BLE001 - остановка не должна падать
            pass

    # --- политика тела запроса (лимит и бинарность задаёт маршрут)
    def body_policy(self, path: str | None = None) -> dict:
        """Лимит тела по маршруту: по умолчанию 8 МиБ JSON, у диктовки — 64 МиБ аудио."""
        return stt_body_policy(path or "")

    # --- маршрутизация ---
    def dispatch(self, method: str, path: str, query: dict, body: dict):
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if match:
                params = {key: unquote(value) for key, value in match.groupdict().items() if value is not None}
                return getattr(self, handler)(query, body, **params)
        for _method, pattern, _handler in ROUTES:
            if pattern.fullmatch(path):
                raise err(405, "Метод %s не поддерживается" % method, method=method, path=path)
        raise err(404, "Неизвестный эндпоинт", path=path)

    # --- общие чтения ---
    def _app_info(self) -> dict:
        return {"name": APP_NAME, "version": config.VERSION, "port": self.port,
                "started_at": self.started_at, "uptime_s": max(0, int(time.time()) - self.started_at),
                "sandbox": bool(self.cfg.sandbox), "tz": http_util.local_tz_name()}

    def _engine_health(self) -> dict:
        board = self.board
        try:
            with self._engine_deadline(3.0):
                res = self.call("engine_read", "health",
                                ((self.cfg, board), {}), ((self.cfg,), {"board": board}))
            return self._normalise_engine_health(res, board)
        except ApiError as exc:
            if exc.status != 501:
                self.log.error("engine_read.health", exc)
        except Exception as exc:  # noqa: BLE001 - health не должен падать
            self.log.error("engine_read.health", exc)
        return self._normalise_engine_health({}, board)

    @contextlib.contextmanager
    def _engine_deadline(self, seconds: float):
        """D6: общий бюджет времени на блок чтения (health ≤3 с, тик watcher)."""
        module = None
        try:
            if self._has_mod("engine_db"):
                module = self.mod("engine_db")
        except ApiError:
            module = None
        if module is not None and hasattr(module, "deadline"):
            with module.deadline(seconds):
                yield
        else:
            yield

    def _normalise_engine_health(self, raw, board: str) -> dict:
        """Единая плоская форма API.md §3.1 (+ вложенные tasks/cli/gateway).

        Одна и та же форма и когда модуль готов, и когда его нет: набор ключей
        не меняется, различаются только значения.
        """
        raw = raw if isinstance(raw, dict) else {}
        nested = raw.get("engine") if isinstance(raw.get("engine"), dict) else None
        flat = bool(nested is None and ("db" in raw or "cli" in raw))
        src = raw if flat else (nested or {})
        tasks = src.get("tasks") if isinstance(src.get("tasks"), dict) else {}
        ok = bool(raw.get("ok")) if not flat else bool(src.get("ok", raw.get("ok")))
        warning = src.get("warning", src.get("error"))
        if not ok and not warning:
            warning = "Движок канбана недоступен"
        cli = src.get("cli") if isinstance(src.get("cli"), dict) else {}
        gateway = src.get("gateway") if isinstance(src.get("gateway"), dict) else {}
        db_path = src.get("db") or src.get("board_db") or str(self.cfg.kanban_db(board))
        cli_path = str(cli.get("path") or self.cfg.hermes_cli or "")
        return {"ok": ok, "warning": warning, "board": src.get("board") or board,
                "mock": False,          # живой движок: тот же ключ, что у мока (fixtures/health.json)
                "db": db_path,
                "db_exists": bool(src.get("db_exists", bool(src))),
                "schema_ok": bool(src.get("schema_ok")),
                "schema_version": src.get("schema_version"),
                "schema_missing": src.get("schema_missing") or [],
                "kanban_home": src.get("kanban_home") or str(self.cfg.kanban_home),
                "db_bytes": src.get("db_bytes"),
                "tasks": {"total": int(tasks.get("total") or 0), "done": int(tasks.get("done") or 0),
                          "blocked": int(tasks.get("blocked") or 0),
                          "active": int(tasks.get("active") or 0)},
                "cli": {"ok": cli_available(cli_path), "path": cli_path},
                "gateway": {"running": bool(gateway.get("running")),
                            "pid": gateway.get("pid"), "state": gateway.get("state") or "unknown",
                            "age_s": gateway.get("age_s")}}

    def _dispatcher(self) -> dict:
        try:
            res = self.call("engine_read", "dispatcher_status", ((self.cfg,), {}))
            return res if isinstance(res, dict) else {}
        except ApiError as exc:
            if exc.status != 501:
                self.log.error("dispatcher_status", exc)
        except Exception as exc:  # noqa: BLE001
            self.log.error("dispatcher_status", exc)
        return {"ok": False, "state": "unknown", "pid": None, "updated_at": None,
                "warning": "Состояние диспетчера недоступно"}

    def _dispatcher_short(self) -> dict:
        state = self._dispatcher()
        return {"ok": bool(state.get("ok")), "state": state.get("state") or "unknown",
                "warning": state.get("warning")}

    def _engine_brief(self) -> dict:
        health = self._engine_health()
        return {"ok": bool(health.get("ok")), "board": health.get("board") or self.board,
                "warning": health.get("warning")}

    def _records(self, room_id, quiet: bool = False) -> list:
        if not room_id:
            return []
        try:
            rows = self.call("registry", "list_requests", ((self.cfg, room_id), {"limit": 100}))
        except TypeError:
            rows = []
        except ApiError as exc:
            if quiet or exc.status == 501:
                return []
            self.log.error("реестр %s" % room_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            if quiet:
                return []
            self.log.error("реестр %s" % room_id, exc)
            raise ApiError(502, "Не удалось прочитать реестр запросов", detail={"room": room_id}) from exc
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

    def _rooms(self, include_archived: bool = True) -> list:
        try:
            rows = self.call("rooms", "list_rooms", ((self.cfg,), {"include_archived": include_archived}))
        except ApiError:
            raise
        if isinstance(rows, dict):
            rows = rows.get("rooms") or rows.get("items") or []
        out = [r for r in rows or [] if isinstance(r, dict)]
        if not include_archived:
            out = [r for r in out if not r.get("archived")]
        return out

    def _room(self, room_id: str) -> dict:
        room = self.call("rooms", "get_room", ((self.cfg, room_id), {}), ((self.cfg,), {"room_id": room_id}))
        if not room or not isinstance(room, dict):
            raise err(404, "Комната не найдена", room=room_id)
        return room

    def _summary(self, room: dict, root_id: str, extra=None, critic=None, answers=None) -> dict:
        board = room.get("board") or self.board
        res = self.call("engine_read", "request_summary",
                        ((self.cfg, board, root_id, list(extra or [])),
                         {"room": room, "critic": critic, "answers": answers}),
                        ((self.cfg, board, root_id, list(extra or [])), {"room": room}),
                        ((self.cfg, board, root_id), {"room": room}))
        return res if isinstance(res, dict) else {}

    def _detail(self, room: dict, root_id: str, extra=None, critic=None, answers=None,
                include_heartbeat: bool = False) -> dict:
        board = room.get("board") or self.board
        res = self.call("engine_read", "request_detail",
                        ((self.cfg, board, root_id, room, list(extra or [])),
                         {"critic": critic, "answers": answers,
                          "include_heartbeat": bool(include_heartbeat)}),
                        ((self.cfg, board, root_id, room, list(extra or [])),
                         {"critic": critic, "answers": answers}),
                        ((self.cfg, board, root_id, room, list(extra or [])), {}),
                        ((self.cfg, board, root_id, room.get("id"), list(extra or [])), {}),
                        ((self.cfg, board, root_id), {"room": room}))
        if not isinstance(res, dict):
            raise ApiError(502, "Движок вернул пустой ответ по запросу", detail={"rid": root_id})
        # WAVE 9 (§4): бриф/под-запрос — необязательные связи с картой проекта.
        try:
            res.update(self.call("plan", "detail_meta",
                                 ((self.cfg, room.get("id"), root_id), {})) or {})
        except Exception:  # noqa: BLE001 - карта вторична для страницы запроса
            res.setdefault("brief", False)
            res.setdefault("plan_link", None)
            res.setdefault("streams_summary", None)
        return res

    def _local_summary(self, room: dict, root_id: str, text: str = "", title: str = "",
                       critic: bool = False, mode: str = "auto") -> dict:
        full = title or derive_title(text)
        return {"id": root_id, "room": room.get("id"), "title": full,
                # W17-B §4: короткое название есть даже когда движок недоступен
                "title_short": short_title(full),
                "title_source": "derived", "title_locked": False,
                "status": "queued", "status_reason": None, "mode": mode,
                "critic": {"enabled": bool(critic), "state": "off", "verdict": None, "round": 0},
                "created_at": int(time.time()), "started_at": None, "finished_at": None,
                "duration_s": None, "progress": {"done": 0, "total": 0, "percent": 0},
                "assignee": None, "needs_you": False, "preview": preview(text), "archived": False}

    def _summary_or_local(self, room: dict, root_id: str, text: str = "", title: str = "",
                          critic: bool = False, mode: str = "auto", answers=None) -> dict:
        """Сводка запроса; при любом сбое чтения — локальная (карточка уже есть, D8).

        Раньше ``EngineError``/``ValueError`` из ``request_summary`` давали HTTP 500
        после успешного ``create``: пользователь видел ошибку, повторял отправку и
        получал вторую карточку. Теперь ответ 201 с усечённой сводкой и warning.
        """
        try:
            summary = self._summary(room, root_id, [], critic, answers)
            if summary:
                return summary
        except ApiError as exc:
            self.log.warning("request_summary %s: %s" % (root_id, exc.message))
        except Exception as exc:  # noqa: BLE001 - карточка уже создана, 500 не отдаём
            self.log.warning("request_summary %s: %s" % (root_id, exc))
        summary = self._local_summary(room, root_id, text, title, critic, mode)
        summary["degraded"] = True
        summary["warning"] = {"code": "summary_unavailable",
                              "message": "Запрос создан, но сводку прочитать не удалось — "
                                         "детали появятся, когда движок освободится."}
        return summary

    def _find_record(self, rid: str):
        for room in self._rooms():
            for rec in self._records(room.get("id"), quiet=True):
                if rec.get("root_id") == rid:
                    return room, rec
        raise err(404, "Запрос не найден", reason="not_root", rid=rid)

    def _register(self, room: dict, record: dict) -> None:
        try:
            self.call("registry", "add_request",
                      ((self.cfg, room.get("id"), record), {}),
                      ((self.cfg, dict(record, room=room.get("id"))), {}))
        except Exception as exc:  # noqa: BLE001 - карточка уже создана, реестр вторичен
            self.log.error("реестр: не удалось записать %s" % record.get("root_id"), exc)

    def _room_summary(self, room: dict) -> dict:
        rid = room.get("id")
        cached = self.watcher.summary_for(rid) if self.watcher else None
        cached = cached or {}
        counters = dict(cached.get("counters") or {"working": 0, "needs_you": 0, "done_today": 0})
        # W17-R §1 (B-8): этапы — свойство комнаты. Нет этапов — нет и чипа этапа,
        # даже если в кэше вотчера осталась старая фаза из прежней константы.
        # Нет этапов у комнаты — нет и чипа этапа. Текущий этап даёт наблюдатель
        # (кэш), а не расчёт на каждом запросе: ETag комнаты не должен «дрожать».
        phases = phase_book.phases_of(self.cfg, room)
        phase = cached.get("phase") if phases else None
        return {"id": rid, "title": room.get("title") or rid, "icon": room.get("icon") or "folder",
                "archived": bool(room.get("archived")), "orchestrator": room.get("orchestrator"),
                "critic": room.get("critic"), "members_count": len(room.get("members") or []),
                "project_dir": str(room.get("project_dir") or ""),
                "mode_default": room.get("mode_default") or "auto",
                "critic_pass": bool(room.get("critic_pass")),
                "max_rounds": int(room.get("max_rounds") or 1),
                # Режим критика (W13-E §9): отдаём и в списке — бейдж «до одобрения»
                # не должен требовать отдельного запроса детали комнаты.
                "critic_mode": str(room.get("critic_mode") or "rounds"),
                "critic_round_cap": int(room.get("critic_round_cap") or 12),
                # Проектный режим — настройка самой комнаты (A1/A2), отдаём и в списке,
                # чтобы бейдж «проектный режим» не требовал отдельного запроса.
                "launch_mode": str(room.get("launch_mode") or "plan_first"),
                "max_parallel_streams": int(room.get("max_parallel_streams") or 2),
                "project_mode": bool(room.get("project_mode")),
                # Этапы комнаты — в списке, чтобы карта не делала лишний запрос.
                "phases": phases,
                "counters": counters,
                "phase": phase,
                "updated_at": int(cached.get("updated_at") or room.get("created_at") or 0)}

    def _room_detail(self, room: dict) -> dict:
        summary = self._room_summary(room)
        detail = {key: value for key, value in room.items()
                  if key not in ("counters", "phase", "dispatcher")}
        detail.update({"counters": summary["counters"], "phase": summary["phase"],
                       "phases": summary.get("phases") or [],
                       "dispatcher": self._dispatcher_short()})
        # Модель/провайдер/мышление участников (WAVE10): model_settings + agent_default
        # у каждого участника и общая карта agent_models_meta. Модуль не готов — Room без них.
        try:
            decorate = self.call("agent_models", "decorate_room", ((self.cfg, detail), {}))
        except ApiError as exc:
            self.log.warning("модели участников: %s" % exc.message)
            return detail
        except Exception as exc:  # noqa: BLE001 - комната важнее настроек модели
            self.log.warning("модели участников: %s" % exc)
            return detail
        return decorate if isinstance(decorate, dict) else detail

    def _limit(self, query: dict, default: int = 20, high: int = 100) -> int:
        raw = (query.get("limit") or [""])[0].strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise err(400, "Параметр limit должен быть числом", field="limit") from None
        if value < 1 or value > high:
            raise err(400, "Параметр limit вне диапазона 1..%d" % high, field="limit", limit=value)
        return value

    def _csv(self, query: dict, name: str) -> list:
        out: list = []
        for part in (query.get(name) or [""])[0].split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
        return out

    def _roster_names(self):
        try:
            items = self.call("engine_write", "list_profiles", ((self.cfg,), {}))
        except ApiError:
            return None
        except Exception:  # noqa: BLE001
            return None
        if isinstance(items, dict):
            items = items.get("items") or []
        # engine_write.list_profiles отдаёт "name", фейки/карточки — "profile": понимаем оба
        names = {str(item.get("profile") or item.get("name"))
                 for item in items or []
                 if isinstance(item, dict) and (item.get("profile") or item.get("name"))}
        return names

    # --- служебные ---
    def _backlog(self, room_id: str) -> dict:
        with self.engine():
            view = self.call("ingest", "backlog_view", ((self.cfg, room_id), {}))
        if isinstance(view, list):
            view = {"items": view}
        if not isinstance(view, dict):
            view = {"items": []}
        items = view.get("items") or []
        view["room"] = view.get("room") or room_id
        view["items"] = items
        view["counts"] = view.get("counts") or _backlog_counts(items)
        view.setdefault("updated_at", int(time.time()))
        # W17-B §7 (B7): статус вопроса берётся из реестра ответов (единственный
        # источник истины), близнецы схлопываются, служебные вопросы скрываются.
        # Читается каждый раз заново — поэтому ответ виден в «Дальше» сразу, без
        # ожидания следующего прохода плана. Данные на диске не меняются.
        if self._has_mod("answers"):
            try:
                view = self.mod("answers").backlog_overlay(view, self._records(room_id, quiet=True))
            except Exception as exc:  # noqa: BLE001 - бэклог важнее меток
                self.log.warning("бэклог: наложение ответов %s: %s" % (room_id, exc))
        return view

    def _report_csp(self) -> str:
        try:
            headers = self.call("reports", "html_headers", ((), {}))
            if isinstance(headers, dict):
                return str(headers.get("Content-Security-Policy") or headers.get("csp")
                           or http_util.HTML_CSP)
        except Exception:  # noqa: BLE001 - CSP по умолчанию всегда безопасна
            pass
        return http_util.HTML_CSP

    def _file_guard(self, path: str):
        """Проверка пути до files.py: белые корни, чёрный список имён, 404 внутри корня."""
        resolved = config.path_allowed(self.cfg, path)
        if resolved is not None:
            return resolved
        try:
            candidate = Path(path).resolve(strict=False)
        except (OSError, ValueError):
            candidate = None
        inside = False
        if candidate is not None:
            for root in self.cfg.allowed_roots:
                try:
                    candidate.relative_to(Path(root).resolve())
                    inside = True
                    break
                except (OSError, ValueError, TypeError):
                    continue
        if not inside:
            raise err(403, "Файл вне разрешённых каталогов", reason="outside_roots", path=path)
        if candidate is not None and candidate.is_file():
            raise err(403, "Файл запрещён к показу: имя или тип вне белого списка",
                      reason="blacklisted", path=path)
        raise err(404, "Файл не найден", path=path)

    def _passthrough_error(self, status: int, payload, path: str) -> ApiError:
        message = ""
        if isinstance(payload, (bytes, bytearray)):
            message = bytes(payload).decode("utf-8", "replace")
        message = (message or "").strip()[:300] or "Файл недоступен"
        return ApiError(int(status), message, detail={"path": path})

# --- таблица маршрутов -----------------------------------------------------
_ROOM = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"
_RID = r"(?P<rid>t_[A-Za-z0-9_]+)"
ROUTES = [(method, re.compile(pattern), handler) for method, pattern, handler in [
    ("GET", r"/api/health", "h_health"),
    ("GET", r"/api/stt/status", "h_stt_status"),
    ("POST", r"/api/stt/transcribe", "h_stt_transcribe"),
    ("GET", r"/api/stt/dictionary", "h_stt_dictionary_get"),
    ("PUT", r"/api/stt/dictionary", "h_stt_dictionary_put"),
    ("POST", r"/api/stt/learn", "h_stt_learn"),
    ("GET", r"/api/logs", "h_logs"),
    ("GET", r"/api/config", "h_config"),
    ("GET", r"/api/profiles", "h_profiles"),
    ("GET", r"/api/dispatcher/preview", "h_dispatcher_preview"),
    ("POST", r"/api/dispatcher/kick", "h_kick"),
    ("GET", r"/api/rooms", "h_rooms"),
    # ВАЖНО: раньше маршрута комнаты по id — иначе «summary» матчится как id комнаты.
    ("GET", r"/api/rooms/summary", "h_rooms_summary"),
    ("POST", r"/api/rooms", "h_room_create"),
    ("POST", r"/api/rooms/check-dir", "h_room_check_dir"),
    ("GET", r"/api/rooms/" + _ROOM, "h_room_get"),
    ("PATCH", r"/api/rooms/" + _ROOM, "h_room_patch"),
    ("POST", r"/api/rooms/" + _ROOM + r"/archive", "h_room_archive"),
    ("POST", r"/api/rooms/" + _ROOM + r"/members", "h_member_add"),
    ("DELETE", r"/api/rooms/" + _ROOM + r"/members/(?P<profile>[A-Za-z0-9_.\-]{1,64})", "h_member_remove"),
    # Особые указания участнику этой комнаты (WAVE9 §9): только реестр комнаты.
    ("PUT", r"/api/rooms/" + _ROOM + r"/agents/(?P<profile>[A-Za-z0-9_.@+\-]{1,64})/notes",
     "h_agent_notes_put"),
    # Подпись участника в комнате (WAVE13 §11): «Имя — должность», файлы профиля не меняются.
    ("PUT", r"/api/rooms/" + _ROOM + r"/agents/(?P<profile>[A-Za-z0-9_.@+\-]{1,64})/label",
     "h_agent_label_put"),
    ("GET", r"/api/rooms/" + _ROOM + r"/requests", "h_requests"),
    ("POST", r"/api/rooms/" + _ROOM + r"/requests", "h_request_create"),
    ("GET", r"/api/rooms/" + _ROOM + r"/candidates", "h_candidates"),
    ("GET", r"/api/requests/" + _RID, "h_request_get"),
    # Переименование запроса пользователем (W17-B §4): title_source=user, title_locked.
    ("PATCH", r"/api/requests/" + _RID, "h_request_patch"),
    ("POST", r"/api/requests/" + _RID + r"/actions", "h_request_actions"),
    ("POST", r"/api/requests/" + _RID + r"/messages", "h_request_messages"),
    ("GET", r"/api/rooms/" + _ROOM + r"/team", "h_team"),
    ("GET", r"/api/rooms/" + _ROOM + r"/roadmap", "h_roadmap"),
    ("GET", r"/api/rooms/" + _ROOM + r"/backlog", "h_backlog"),
    ("POST", r"/api/rooms/" + _ROOM + r"/backlog/(?P<item_id>[^/]+?)/ask", "h_backlog_ask"),
    ("POST", r"/api/rooms/" + _ROOM + r"/backlog/(?P<item_id>[^/]+)", "h_backlog_set"),
    ("GET", r"/api/rooms/" + _ROOM + r"/reports", "h_reports"),
    ("GET", r"/api/reports/" + _ROOM + r"/(?P<key>[^/]+)", "h_report_get"),
    ("GET", r"/reports/" + _ROOM + r"/(?P<key>[^/]+)/(?P<rel>.+)", "h_report_file"),
    ("GET", r"/reports/" + _ROOM + r"/(?P<key>[^/]+)", "h_report_file"),
    ("GET", r"/api/rooms/" + _ROOM + r"/journal", "h_journal"),
    ("GET", r"/api/search", "h_search"),
    ("GET", r"/api/file", "h_file"),
    ("POST", r"/api/open", "h_open"),
    # B-22 WAVE19: «Открыть / В проводнике» для артефактов комнаты (папка проекта).
    ("POST", r"/api/artifacts/open", "h_artifact_open"),
    ("GET", r"/api/ics", "h_ics"),
    ("GET", r"/api/export/request/" + _RID + r"\.md", "h_export_md"),
    ("GET", r"/api/export/request/" + _RID + r"\.html", "h_export_html"),
    ("POST", r"/api/rooms/" + _ROOM + r"/consolidate", "h_consolidate"),
    ("POST", r"/api/adopt", "h_adopt"),
]]

# --- карта проекта (WAVE 9) ------------------------------------------------
# Один блок подключения: методы ``PlanRoutes`` на ``App`` + строки в ``ROUTES``.
from server.routes_plan import install_plan_routes                    # noqa: E402

install_plan_routes(App, ROUTES)

# --- модель участника комнаты (WAVE 10) ------------------------------------
# Один блок подключения: методы ``ModelRoutes`` на ``App`` + строки в ``ROUTES``.
from server.routes_models import install_model_routes                    # noqa: E402

install_model_routes(App, ROUTES)

# --- особенности для этой комнаты (WAVE14 пакет I, пункт 13) ----------------
# Записи участникам, журнал изменений и откат: ``server/routes_notes.py``.
from server.routes_notes import install_notes_routes                      # noqa: E402

install_notes_routes(App, ROUTES)

# --- проверка системы (WAVE 12) --------------------------------------------
# Один блок подключения: методы ``DiagRoutes`` на ``App`` + строки в ``ROUTES``.
from server.routes_diag import install_diag_routes                        # noqa: E402

install_diag_routes(App, ROUTES)

# --- длинная диктовка (WAVE 13, пакет B) -----------------------------------
# Части записи на диск: ``/api/stt/recordings/*`` (см. server/routes_stt.py).
from server.routes_stt import install_stt_routes                          # noqa: E402

install_stt_routes(App, ROUTES)

# --- состав комнаты: закрепление участника (волна 19, B-28) ----------------
# ``PATCH /api/rooms/{id}/members/{profile} {pinned}`` — флаг реестра комнаты.
from server.routes_members import install_member_routes                   # noqa: E402

install_member_routes(App, ROUTES)


# --- сервер ----------------------------------------------------------------
class TeamRoomServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + приложение; shutdown останавливает watcher.

    ``allow_reuse_address = False``: на Windows SO_REUSEADDR позволяет двум
    процессам слушать один порт — тогда «порт занят» молча не срабатывает.
    ``max_threads`` (D11) ограничивает число одновременных соединений: сверх лимита
    сразу уходит 503 ``server_busy``, а не растёт стек потоков.
    """

    daemon_threads = True
    allow_reuse_address = False
    max_threads = MAX_THREADS

    def __init__(self, address, app: App):
        self.http_app = app
        self._slots = threading.Semaphore(max(1, int(self.max_threads or MAX_THREADS)))
        super().__init__(address, http_util.Handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self._reject_busy(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _reject_busy(self, request) -> None:
        """503 без разбора запроса: поток не создаётся, сокет закрывается вежливо.

        На Windows `closesocket` при непрочитанном входящем теле шлёт RST, и
        клиент получает `ConnectionAbortedError 10053` вместо нашего 503
        (флейк `test_thread_cap_returns_503`). Поэтому: отдать ответ,
        `shutdown(SHUT_WR)`, коротко (≤0,2 с) дочитать вход и только затем
        закрыть — клиент успевает получить и разобрать ответ.
        """
        error = http_util.ApiError(
            503, "Сервер занят: слишком много одновременных запросов — повторите через секунду",
            code="server_busy", detail={"limit": int(self.max_threads or MAX_THREADS)})
        body = http_util.json_bytes(error.payload())
        head = ("HTTP/1.1 503 Service Unavailable\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                "Content-Length: %d\r\n"
                "X-Content-Type-Options: nosniff\r\n"
                "Referrer-Policy: no-referrer\r\n"
                "X-Frame-Options: SAMEORIGIN\r\n"
                "Connection: close\r\n\r\n" % len(body))
        try:
            request.sendall(head.encode("ascii") + body)
        except OSError:
            pass
        finally:
            try:
                request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            self._drain_input(request, 0.2)
            try:
                request.close()
            except OSError:
                pass

    @staticmethod
    def _drain_input(request, budget: float) -> None:
        """Дочитать входящие байты (не дольше `budget` секунд), чтобы не было RST."""
        deadline = time.monotonic() + max(0.0, float(budget))
        try:
            request.settimeout(0.05)
            while time.monotonic() < deadline:
                if not request.recv(65536):
                    break
        except OSError:
            pass

    def shutdown(self):
        try:
            self.http_app.stop()
        finally:
            super().shutdown()

    def server_close(self):
        try:
            self.http_app.stop()
        finally:
            super().server_close()

    def finish_request(self, request, client_address):
        """Обработать соединение; обрыв клиентом (закрыл вкладку/браузер) — молча."""
        try:
            super().finish_request(request, client_address)
        except Exception as exc:  # noqa: BLE001 - не-обрывы уходят в handle_error
            if not http_util.is_client_disconnect(exc):
                raise

    def handle_error(self, request, client_address):
        """Обрывы соединения игнорируются; прочие сбои — в data/logs/app.log, не в stderr."""
        exc = sys.exception()
        if http_util.is_client_disconnect(exc):
            return
        try:
            self.http_app.log.error("сбой при обработке соединения от %s" % (client_address,), exc)
        except Exception:  # noqa: BLE001 - лог не должен ронять поток обработчика
            pass


def create_app(cfg=None, start_watcher: bool = True, port: int | None = None) -> TeamRoomServer:
    """Создать сервер (bind уже выполнен). ``serve_forever()``/``shutdown()`` — снаружи."""
    cfg = cfg or config.load()
    try:
        cfg.ensure_dirs()
    except OSError as exc:
        print("Не удалось создать каталоги данных: %s" % exc, file=sys.stderr)
    app = App(cfg, port=port if port is not None else cfg.port)
    try:
        app.call("rooms", "seed_defaults", ((cfg,), {}))
    except Exception as exc:  # noqa: BLE001 - старт не должен падать из-за модуля
        app.log.warning("rooms.seed_defaults недоступен: %s" % exc)
    server = TeamRoomServer((cfg.host or "127.0.0.1", port if port is not None else cfg.port), app)
    app.port = server.server_address[1]
    if start_watcher:
        app.start_watcher()
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Комната команды — локальный сервер (только 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="порт (по умолчанию 8777 или TEAMROOM_PORT)")
    parser.add_argument("--root", default=None,
                        help="папка приложения (по умолчанию — папка, где лежит server/)")
    parser.add_argument("--home", default=None,
                        help="папка данных (по умолчанию <root>/data или TEAMROOM_DATA)")
    parser.add_argument("--no-watcher", action="store_true", help="без фонового наблюдателя")
    args = parser.parse_args(argv)
    cfg = config.load()
    # Запуск из любой папки: все пути считаются от самого приложения, а не от cwd.
    if args.root:
        cfg.app_dir = Path(args.root).expanduser().resolve()
    if args.home:
        cfg.data_dir = Path(args.home).expanduser().resolve()
    if args.port:
        cfg.port = int(args.port)
    # Папка данных должна остаться доступной для файлов (её же видят вложения комнат).
    try:
        resolved_data = Path(cfg.data_dir).resolve()
        if not any(Path(root).resolve() == resolved_data for root in cfg.allowed_roots):
            cfg.allowed_roots.append(resolved_data)
    except OSError:
        pass
    try:
        server = create_app(cfg, start_watcher=not args.no_watcher, port=cfg.port)
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        busy = winerror in (10048, 10013) or getattr(exc, "errno", None) in (48, 98, 99, 13)
        if busy:
            print("Порт %d занят — закройте другой экземпляр или укажите --port N." % cfg.port,
                  file=sys.stderr)
        else:
            print("Не удалось запустить сервер на 127.0.0.1:%d — %s" % (cfg.port, exc),
                  file=sys.stderr)
        return 2
    url = "http://127.0.0.1:%d" % server.server_address[1]
    print("Комната команды: %s" % url, flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        try:
            server.http_app.stop()
        except Exception:  # noqa: BLE001
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
