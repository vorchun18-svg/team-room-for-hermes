"""GET-хендлеры «Комнаты команды» — миксин для ``App`` (server/app.py).

Здесь только чтение: комнаты, запросы, команда, дорожная карта, бэклог, отчёты,
журнал, поиск, файлы, календарь и экспорт. Никаких записей в домен.
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

try:  # пакетный и «плоский» импорт
    from . import config
    from . import http_util
    from . import statefiles as sf
    from .app_common import (ACTION_ALLOWED, ACTION_REQUIRED, ACTION_TEXT, BACKLOG_KINDS,
                             BACKLOG_STATUSES, MAX_ATTACH, MAX_NOTE, MAX_TEXT, PATCH_FIELDS,
                             ROOM_STATUSES, decode_cursor, derive_title, encode_cursor, make_slug)
    from .http_util import ApiError, Raw, err
except ImportError:  # pragma: no cover
    import config  # type: ignore
    import http_util  # type: ignore
    import statefiles as sf  # type: ignore
    from app_common import (ACTION_ALLOWED, ACTION_REQUIRED, ACTION_TEXT, BACKLOG_KINDS,  # type: ignore
                            BACKLOG_STATUSES, MAX_ATTACH, MAX_NOTE, MAX_TEXT, PATCH_FIELDS,
                            ROOM_STATUSES, decode_cursor, derive_title, encode_cursor, make_slug)
    from http_util import ApiError, Raw, err  # type: ignore


def _stt_flag(cfg) -> bool:
    """Флаг `features.stt` для `/api/config`: доступна ли диктовка.

    Импорт ленивый и в try/except: модуль STT может отсутствовать (демо/старый
    стенд), и тогда конфиг обязан отвечать, а не падать. Ошибка внутри status()
    трактуется как «недоступно», без текста в ответе.
    """
    try:
        try:
            from . import stt as stt_mod
        except ImportError:  # «плоский» импорт (тесты, CLI)
            import stt as stt_mod  # type: ignore
        return bool((stt_mod.status(cfg) or {}).get("available"))
    except Exception:  # noqa: BLE001 — конфиг важнее диагностики
        return False


def _stt_mode(cfg) -> str:
    """Режим диктовки как возможность: local / provider / none (см. stt.status)."""
    try:
        try:
            from . import stt as stt_mod
        except ImportError:  # «плоский» импорт (тесты, CLI)
            import stt as stt_mod  # type: ignore
        return str((stt_mod.status(cfg) or {}).get("mode") or "none")
    except Exception:  # noqa: BLE001 — конфиг важнее диагностики
        return "none"


class ReadRoutes:
    """Обработчики GET-эндпоинтов (``self`` — экземпляр ``server.app.App``)."""

    def h_health(self, query, body):
        started = time.monotonic()
        engine = self._engine_health()
        if time.monotonic() - started < 2.5:      # D6: health не висит за движком
            dispatcher = self._dispatcher()
        else:
            dispatcher = {"ok": False, "state": "unknown", "pid": None, "updated_at": None,
                          "warning": "Диспетчер не проверялся: движок отвечает медленно"}
        data = {"dir": str(self.cfg.data_dir), "writable": True,
                "rooms": 0, "requests": 0}
        # D15: своя проба на каждый вызов — общий .write-probe давал ложное writable=false
        probe = Path(self.cfg.data_dir) / (".write-probe-%d-%d-%s"
                                           % (os.getpid(), threading.get_ident(), uuid.uuid4().hex[:8]))
        try:
            probe.write_text("ok", encoding="utf-8")
        except OSError:
            data["writable"] = False
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        try:
            rows = self._rooms()
            data["rooms"] = len(rows)
            data["requests"] = sum(len(self._records(r.get("id"), quiet=True)) for r in rows)
        except Exception:  # noqa: BLE001 - health не должен падать
            pass
        vault_enabled = bool(getattr(self.cfg, "vault_enabled", True))
        vault_ok = True
        try:
            vault_ok = Path(self.cfg.vault_dir).is_dir()
        except OSError:
            vault_ok = False

        cli = engine.get("cli") or {}
        warnings = []
        if not engine.get("ok"):
            warnings.append({"code": "schema_drift" if engine.get("schema_ok") is False else "cli_missing",
                             "message": str(engine.get("warning") or "Движок канбана недоступен"),
                             "detail": {"board": engine.get("board")}})
        if not cli.get("ok"):
            warnings.append({"code": "cli_missing",
                             "message": "Hermes CLI не найден (%s) — комната работает в режиме "
                                        "просмотра: читать можно, записи в канбан недоступны. "
                                        "Установите Hermes Desktop или укажите TEAMROOM_HERMES_CLI."
                                        % (cli.get("path") or self.cfg.hermes_cli or "hermes"),
                             "detail": {"path": cli.get("path") or str(self.cfg.hermes_cli or ""),
                                        "mode": "read-only"}})
        if not dispatcher.get("ok"):
            warnings.append({"code": "dispatcher_down",
                             "message": str(dispatcher.get("warning") or "Диспетчер не запущен"),
                             "detail": {"state": dispatcher.get("state")}})
        if vault_enabled and not vault_ok:
            warnings.append({"code": "vault_missing", "message": "Каталог Vault недоступен",
                             "detail": {"dir": str(self.cfg.vault_dir)}})
        elif not vault_enabled:
            warnings.append({"code": "vault_disabled",
                             "message": config.VAULT_HINT_RU,
                             "detail": {"hint_env": "TEAMROOM_VAULT_DIR"}})
        # D4/D5: битый/занятый файл состояния — предупреждение с путём копии
        warnings.extend(self._state_warnings())
        watcher_state = self.watcher.status() if self.watcher else {}
        if watcher_state.get("last_error"):
            warnings.append({"code": "watcher_error", "message": "Watcher сообщил об ошибке",
                             "detail": {"error": watcher_state["last_error"]}})
        payload = {"ok": bool(engine.get("ok")) and bool(cli.get("ok")) and (vault_ok or not vault_enabled),
                   "app": self._app_info(), "engine": engine, "dispatcher": dispatcher,
                   "data": data,
                   "vault": {"dir": str(self.cfg.vault_dir), "ok": vault_ok,
                             "enabled": vault_enabled,
                             "hint": "" if vault_enabled else config.VAULT_HINT_RU},
                   # возможности: что есть на этой машине, а что необязательно (см. docs/INSTALL.md)
                   "features": {"kanban_read": bool(engine.get("db_exists")),
                                "kanban_write": bool(cli.get("ok")),
                                "vault_mirror": vault_enabled,
                                "dictation": _stt_mode(self.cfg)},
                   "watcher": watcher_state, "warnings": warnings}
        return 200, payload, {}

    @staticmethod
    def _state_warnings() -> list:
        """Проблемы чтения data/*.json → warnings health (код, русский текст, копия)."""
        try:
            problems = sf.problems()
        except Exception:  # noqa: BLE001 - health не должен падать из-за диагностики
            return []
        out = []
        for problem in problems:
            out.append({"code": problem.get("code") or "state_corrupt",
                        "message": problem.get("message") or "Файл состояния повреждён или недоступен",
                        "detail": {"path": problem.get("path"), "backup": problem.get("backup") or ""}})
        return out

    def h_config(self, query, body):
        payload = {
            "app": self._app_info(),
            "paths": {"data": str(self.cfg.data_dir), "vault": str(self.cfg.vault_dir),
                      "project_dir": str(self.cfg.app_dir), "reports": str(self.cfg.reports_dir)},
            "allowed_roots": [str(path) for path in self.cfg.allowed_roots],
            "limits": {"request_text_max": MAX_TEXT, "note_text_max": MAX_NOTE,
                       "body_bytes_max": int(getattr(http_util, "MAX_BODY", 0) or 0),
                       "attach_max": MAX_ATTACH,
                       "attach_bytes_max": 25 * 1024 * 1024, "page_size_max": 100,
                       "journal_page_max": 200},
            "features": {"reports": True, "ics": True, "export_html": True, "adopt": True,
                         "stt": bool(_stt_flag(self.cfg)),
                         # как именно работает диктовка: local / provider / none
                         "dictation": _stt_mode(self.cfg),
                         "kanban_write": bool(self._engine_health().get("cli", {}).get("ok")),
                         "vault_mirror": bool(getattr(self.cfg, "vault_enabled", True)),},
        }
        return 200, payload, {}

    def h_profiles(self, query, body):
        roster = self.call("engine_write", "list_profiles", ((self.cfg,), {}))
        if isinstance(roster, dict):
            roster = roster.get("items") or []
        items = [self._profile_card(item) for item in roster or [] if isinstance(item, dict)]
        return 200, {"items": items, "count": len(items), "dispatcher": self._dispatcher_short()}, {}

    @staticmethod
    def _profile_card(item: dict) -> dict:
        """Ростер движка (`name/model/provider`) → `Profile` (docs/API.md §3.5)."""
        name = str(item.get("name") or item.get("profile") or "").strip()
        tags = item.get("tags")
        if not isinstance(tags, list):
            tags = []
        kind = str(item.get("kind") or "").strip()
        if kind not in ("director", "worker", "critic", "utility"):
            low = name.lower()
            if "critic" in low:
                kind = "critic"
            elif "director" in low or low in ("default", "orchestrator"):
                kind = "director" if low != "default" else "utility"
            elif low in ("runner", "assistant", "utility", "decomposer"):
                kind = "utility"
            else:
                kind = "worker"
        return {"profile": name,
                "title": str(item.get("title") or name.replace("-", " ") or name),
                "description": str(item.get("description") or ""),
                "tags": [str(tag) for tag in tags if str(tag).strip()],
                "kind": kind,
                "available": bool(item.get("available", True))}

    def h_rooms(self, query, body):
        include = (query.get("include_archived") or ["0"])[0].strip().lower() in ("1", "true", "yes")
        rooms = self._rooms()
        items = [self._room_summary(room) for room in rooms if include or not room.get("archived")]
        return 200, {"rooms": items, "engine": self._engine_brief(),
                     "updated_at": int(time.time())}, {}

    def h_room_get(self, query, body, room_id):
        detail = self._room_detail(self._room(room_id))
        return 200, detail, {"ETag": http_util.etag_of(http_util.json_bytes(detail)),
                             "Cache-Control": "no-cache"}

    def h_requests(self, query, body, room_id):
        room = self._room(room_id)
        limit = self._limit(query, default=20, high=100)
        statuses = self._csv(query, "status")
        bad = [s for s in statuses if s not in ROOM_STATUSES]
        if bad:
            raise err(400, "Неизвестный статус", statuses=bad, allowed=list(ROOM_STATUSES))
        needle = (query.get("q") or [""])[0].strip().lower()
        offset = decode_cursor((query.get("cursor") or [""])[0].strip()) if (query.get("cursor") or [""])[0].strip() else 0
        records = sorted(self._records(room_id),
                         key=lambda rec: (rec.get("created_at") or 0, rec.get("root_id") or ""),
                         reverse=True)
        items = []
        for rec in records:
            root_id = rec.get("root_id")
            if not root_id:
                continue
            try:
                summary = self._summary(room, root_id, rec.get("extra_cards"), rec.get("critic"),
                                        rec.get("answers"))
            except ApiError as exc:
                if exc.status == 501:
                    raise
                self.log.error("request_summary %s" % root_id, exc)
                summary = self._local_summary(room, root_id, rec.get("text") or "",
                                              rec.get("title") or "", bool(rec.get("critic")),
                                              rec.get("mode") or "auto")
            if statuses and summary.get("status") not in statuses:
                continue
            if needle:
                haystack = ("%s %s" % (summary.get("title") or "", rec.get("text") or "")).lower()
                if needle not in haystack:
                    continue
            items.append(summary)
        page = items[offset:offset + limit]
        has_more = offset + limit < len(items)
        return 200, {"items": page, "next_cursor": encode_cursor(offset + limit) if has_more else None,
                     "has_more": has_more}, {}

    def h_candidates(self, query, body, room_id):
        room = self._room(room_id)
        board = room.get("board") or self.board
        try:
            ids = self.call("registry", "all_registered_ids", ((self.cfg, board), {}))
        except ApiError as exc:
            if exc.status != 501:
                raise
            ids = set()
        if isinstance(ids, dict):
            ids = list(ids.keys())
        ids = list(ids or [])
        items = self.call("engine_read", "list_candidates", ((self.cfg, board, set(ids)), {}))
        if isinstance(items, dict):
            items = items.get("items") or []
        items = items or []
        return 200, {"room": room_id, "generated_at": int(time.time()),
                     "items": items, "count": len(items)}, {}

    def h_request_get(self, query, body, rid):
        room, rec = self._find_record(rid)
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"), rec.get("answers"))
        self._ensure_ingested(room, detail)
        detail["thread"] = self._thread(room, rid)
        return 200, detail, {"ETag": http_util.etag_of(http_util.json_bytes(detail)),
                             "Cache-Control": "no-cache"}

    def _ensure_ingested(self, room: dict, detail: dict) -> None:
        """Готовый запрос, открытый до приёма watcher'ом, — принять сейчас.

        Иначе шаги запроса так и не попадают в бэклог комнаты и их нельзя
        отметить (WAVE8, Блок B). Приём идемпотентен по версии ответа, поэтому
        повторные открытия ничего не дублируют; ошибки только пишутся в лог.
        """
        if not isinstance(detail, dict) or str(detail.get("status") or "") != "done":
            return
        try:
            ingest_request = getattr(self.mod("ingest"), "ingest_request", None)
            if callable(ingest_request):
                ingest_request(self.cfg, room, detail)
        except Exception as exc:  # noqa: BLE001 - страница запроса важнее приёма
            self.log.warning("ensure ingest %s: %s" % (detail.get("id"), exc))

    def _thread(self, room: dict, rid: str) -> list:
        """Лента диалога (server/thread.py); сбой чтения не роняет страницу запроса."""
        try:
            items = self.call("thread", "get_thread",
                              ((self.cfg, rid), {"room": room,
                                                 "board": room.get("board") or self.board}),
                              ((self.cfg, rid), {}))
        except ApiError as exc:
            self.log.warning("thread %s: %s" % (rid, exc.message))
            return []
        except Exception as exc:  # noqa: BLE001 - диалог вторичен по отношению к ответу
            self.log.warning("thread %s: %s" % (rid, exc))
            return []
        return items if isinstance(items, list) else []

    def h_team(self, query, body, room_id):
        room = self._room(room_id)
        roster: list = []
        orchestrator = str(room.get("orchestrator") or "").strip()
        if orchestrator:
            # директор первым: он ведёт запросы комнаты
            roster.append({"profile": orchestrator, "role": "orchestrator",
                           "note": "Разбирает запрос, раздаёт работу, сводит ответ", "pinned": True})
        for member in room.get("members") or []:
            profile = (member or {}).get("profile")
            if profile and profile != orchestrator:
                roster.append(dict(member))
        critic = str(room.get("critic") or "").strip()
        if critic and all(m.get("profile") != critic for m in roster):
            roster.append({"profile": critic, "role": "critic", "note": "", "pinned": False})
        members = self.call("engine_read", "team_state",
                            ((self.cfg, room.get("board") or self.board, roster, room.get("tenant")), {}),
                            ((self.cfg, room.get("board") or self.board, roster), {}))
        if isinstance(members, dict):
            members = members.get("members") or []
        members = self._with_models(room, members or [])
        return 200, {"room": room_id, "generated_at": int(time.time()),
                     "dispatcher": self._dispatcher(), "members": members or []}, {}

    def _with_models(self, room, members):
        """Дополнить строки команды моделью: замена комнаты поверх настроек агента.

        Руководитель и критик приходят в `members` не из `room["members"]`, поэтому
        их модель надо посчитать отдельно — иначе в интерфейсе видно «как у агента».
        """
        try:
            from agent_models import model_settings, agent_default  # noqa: PLC0415 - лениво
        except ImportError:
            try:
                from .agent_models import model_settings, agent_default  # type: ignore
            except Exception:  # модуль моделей ещё не поднят — отдаём как есть
                return members
        out = []
        for member in members:
            if not isinstance(member, dict):
                out.append(member)
                continue
            item = dict(member)
            profile = str(item.get("profile") or "").strip()
            if profile and not item.get("model_settings"):
                try:
                    item["model_settings"] = model_settings(self.cfg, room, profile)
                except Exception as exc:  # noqa: BLE001 - подпись модели вторична
                    self.log.warning("модель %s: %s" % (profile, exc))
            if profile and not item.get("agent_default"):
                try:
                    item["agent_default"] = agent_default(self.cfg, profile)
                except Exception:  # noqa: BLE001
                    pass
            out.append(item)
        return out

    def h_roadmap(self, query, body, room_id):
        self._room(room_id)
        with self.engine():
            view = self.call("ingest", "roadmap_view", ((self.cfg, room_id), {}))
        if not isinstance(view, dict):
            view = {"phases": view or []}
        view.setdefault("room", room_id)
        return 200, view, {}

    def h_backlog(self, query, body, room_id):
        self._room(room_id)
        statuses = self._csv(query, "status")
        bad = [s for s in statuses if s not in BACKLOG_STATUSES]
        if bad:
            raise err(400, "Неизвестный статус", statuses=bad, allowed=list(BACKLOG_STATUSES))
        kinds = self._csv(query, "kind")
        bad_kinds = [k for k in kinds if k not in BACKLOG_KINDS]
        if bad_kinds:
            raise err(400, "Неизвестный вид элемента", kinds=bad_kinds, allowed=list(BACKLOG_KINDS))
        limit = self._limit(query, default=200, high=500)
        view = self._backlog(room_id)
        items = view["items"]
        if statuses:
            items = [i for i in items if (i or {}).get("status") in statuses]
        if kinds:
            items = [i for i in items if (i or {}).get("kind") in kinds]
        view["items"] = items[:limit]
        return 200, view, {}

    def h_reports(self, query, body, room_id):
        self._room(room_id)
        with self.engine():
            items = self.call("reports", "list_reports", ((self.cfg, room_id), {}))
        if isinstance(items, dict):
            items = items.get("items") or []
        return 200, {"room": room_id, "items": items or []}, {}

    def h_report_get(self, query, body, room_id, key):
        self._room(room_id)
        with self.engine():
            rep = self.call("reports", "find_report", ((self.cfg, room_id, key), {}))
        if not rep:
            raise err(404, "Отчёт не найден", room=room_id, key=key)
        if isinstance(rep, str):
            rep = {"path": rep, "kind": "spec"}
        path = rep.get("path")
        with self.engine():
            loaded = self.call("reports", "load_report", ((self.cfg, path), {}))
        rep_kind = rep.get("kind") or ("html" if str(path).lower().endswith((".html", ".htm")) else "spec")
        if (isinstance(loaded, dict) and loaded.get("kind") == "html") or rep_kind == "html":
            payload = {"kind": "html",
                       "url": (loaded.get("url") if isinstance(loaded, dict) else None)
                       or "/api/file?path=" + quote(str(path), safe=""),
                       "title": (loaded.get("title") if isinstance(loaded, dict) else None)
                       or rep.get("title") or key}
            return 200, payload, {"ETag": http_util.etag_of(http_util.json_bytes(payload)),
                                  "Cache-Control": "no-cache"}
        loaded = loaded if isinstance(loaded, dict) else {}
        errors = loaded.get("errors") or []
        if errors:
            raise ApiError(502, "Отчёт не прошёл проверку",
                           detail={"errors": errors[:50], "room": room_id, "key": key,
                                   "warnings": loaded.get("warnings") or []})
        spec = loaded.get("spec") if "spec" in loaded else loaded
        if not isinstance(spec, dict) or not spec:
            raise ApiError(502, "Отчёт пуст или не читается",
                           detail={"errors": errors or ["пустой report.json"], "path": str(path)})
        return 200, spec, {"ETag": http_util.etag_of(http_util.json_bytes(spec)),
                           "Cache-Control": "no-cache"}

    def h_report_file(self, query, body, room_id, key, rel=""):
        """Статика папки отчёта: /reports/<room>/<key>/<файл> (URL из reports.py)."""
        with self.engine():
            res = self.call("reports", "serve_report_file", ((self.cfg, room_id, key, rel), {}))
        if not (isinstance(res, tuple) and len(res) == 3):
            raise ApiError(500, "Модуль reports вернул неверный ответ", detail={"module": "reports"})
        status, headers, payload = res
        status = int(status or 200)
        if status != 200:
            raise self._passthrough_error(status, payload, "%s/%s/%s" % (room_id, key, rel))
        headers = dict(headers or {})
        ctype = headers.pop("Content-Type", None) or "application/octet-stream"
        headers.pop("Content-Length", None)
        if isinstance(payload, Path):
            try:
                payload = payload.read_bytes()
            except OSError as exc:
                raise err(404, "Файл отчёта не найден", path=str(payload)) from exc
        headers.setdefault("Cache-Control", "no-cache")
        return int(status or 200), Raw(payload or b"", str(ctype), headers), {}

    def h_journal(self, query, body, room_id):
        self._room(room_id)
        limit = self._limit(query, default=50, high=200)
        needle = (query.get("q") or [""])[0].strip().lower()
        view = self.call("ingest", "journal_view",
                         ((self.cfg, room_id), {"limit": limit, "q": needle}),
                         ((self.cfg, room_id), {}))
        if isinstance(view, list):
            view = {"items": view}
        if not isinstance(view, dict):
            view = {"items": []}
        items = view.get("items") or []
        if needle:
            items = [item for item in items
                     if needle in str((item or {}).get("text") or "").lower()
                     or needle in str((item or {}).get("kind") or "").lower()]
        payload = {"room": room_id, "items": items[:limit],
                   "next_cursor": view.get("next_cursor"),
                   "has_more": bool(view.get("has_more"))}
        return 200, payload, {}

    def h_search(self, query, body):
        needle = (query.get("q") or [""])[0].strip()
        if len(needle) < 2:
            raise err(400, "Поисковый запрос короче 2 символов", field="q", length=len(needle))
        if len(needle) > 200:
            raise err(400, "Поисковый запрос длиннее 200 символов", field="q", length=len(needle))
        room_id = (query.get("room") or [""])[0].strip() or None
        if room_id:
            self._room(room_id)
        limit = self._limit(query, default=20, high=50)
        items = self.call("search", "query",
                          ((self.cfg, needle, room_id, limit), {}),
                          ((self.cfg, needle), {"room": room_id, "limit": limit}))
        if isinstance(items, dict):
            items = items.get("items") or []
        items = items or []
        total = len(items)
        try:
            counted = self.call("search", "total", ((self.cfg, needle, room_id), {}),
                                ((self.cfg, needle), {"room": room_id}))
            if isinstance(counted, int) and counted >= 0:
                total = counted
        except ApiError:
            pass
        except Exception as exc:  # noqa: BLE001 - total не должен ломать поиск
            self.log.warning("search.total: %s" % exc)
        return 200, {"q": needle, "room": room_id, "total": total, "items": items[:limit]}, {}

    # --- файлы, календарь, экспорт ---
    def h_file(self, query, body):
        path = (query.get("path") or [""])[0].strip()
        if not path:
            raise err(400, "Нужен параметр path", field="path")
        self._deny_log_file(path)          # D13: журналы — только через /api/logs (с маскировкой)
        self._file_guard(path)
        try:
            res = self.call("files", "open_file", ((self.cfg, path), {}))
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.error("open_file %s" % path, exc)
            raise ApiError(500, "Не удалось отдать файл", detail={"path": path}) from exc
        if not (isinstance(res, tuple) and len(res) == 3):
            raise ApiError(500, "Модуль files вернул неверный ответ", detail={"module": "files"})
        status, headers, payload = res
        status = int(status or 200)
        if status != 200:
            raise self._passthrough_error(status, payload, path)
        headers = dict(headers or {})
        ctype = headers.pop("Content-Type", None) or headers.pop("content-type", None) \
            or "application/octet-stream"
        if isinstance(payload, Path):
            try:
                payload = payload.read_bytes()
            except OSError as exc:
                raise err(404, "Файл не найден", path=path) from exc
        if str(ctype).startswith("text/html"):
            headers.setdefault("Content-Security-Policy", self._report_csp())
        headers.setdefault("Cache-Control", "no-cache")
        return 200, Raw(payload or b"", str(ctype), headers), {}

    # --- журналы -----------------------------------------------------------
    def h_logs(self, query, body):
        """GET /api/logs?name=engine.log&tail=200 — хвост журнала с маскировкой секретов (D13)."""
        name = ((query.get("name") or query.get("file") or ["engine.log"])[0] or "").strip() or "engine.log"
        if ".." in name or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise err(400, "name: имя файла журнала (латиница, цифры, . _ -)", field="name", value=name)
        raw_tail = (query.get("tail") or ["200"])[0].strip() or "200"
        try:
            tail = int(raw_tail)
        except ValueError:
            raise err(400, "tail: число строк", field="tail", value=raw_tail) from None
        if tail < 1 or tail > 1000:
            raise err(400, "tail: от 1 до 1000 строк", field="tail", value=tail)
        log_dir = Path(self.cfg.log_dir).resolve()
        try:
            target = (log_dir / name).resolve()
            target.relative_to(log_dir)
        except (OSError, ValueError):
            raise err(403, "Журнал вне data/logs", path=name) from None
        if not target.is_file():
            raise err(404, "Журнал не найден", name=name, dir=str(log_dir))
        lines = sf.read_text(target, "").splitlines()[-tail:]
        text = "\n".join(http_util.mask_secrets(line) for line in lines)
        if text:
            text += "\n"
        return 200, Raw(text, "text/plain; charset=utf-8",
                        {"Cache-Control": "no-store"}), {}

    def _deny_log_file(self, path: str) -> None:
        """Журналы не отдаём как обычный файл: в них бывают секреты из argv/stderr."""
        low = str(path).replace("\\", "/").lower()
        log_dir = Path(self.cfg.log_dir)
        inside = False
        try:
            inside = Path(path).resolve(strict=False).is_relative_to(log_dir.resolve())
        except (OSError, ValueError):
            inside = False
        if low.endswith(".log") or low.endswith(".log.1") or inside:
            raise err(403, "Журналы не отдаются через /api/file — используйте GET /api/logs",
                      reason="blacklisted", path=path)

    def h_ics(self, query, body):
        room_id = (query.get("room") or [""])[0].strip()
        if not room_id:
            raise err(400, "Нужен параметр room", field="room")
        room = self._room(room_id)
        ids = [part.strip() for part in (query.get("items") or [""])[0].split(",") if part.strip()]
        all_flag = (query.get("all") or ["0"])[0].strip().lower() in ("1", "true", "yes")
        if not ids and not all_flag:
            raise err(400, "Нужны items или all=1", field="items")
        with self.engine():
            meta: dict = {}
            text = self.call("ics", "ics_for_items",
                             ((self.cfg, room_id, ids), {"all_open": all_flag, "meta": meta}),
                             ((self.cfg, room_id, ids), {"all_open": all_flag}))
        if ids or all_flag:
            if not isinstance(text, str) or not text.strip() or not meta.get("count", 1):
                raise err(404, "Нет элементов с датой", room=room_id,
                          missing=meta.get("missing") or [], items=ids or None)
        headers = {"Content-Disposition": 'attachment; filename="%s.ics"' % room_id,
                   "Cache-Control": "no-store"}
        return 200, Raw(text or "", "text/calendar; charset=utf-8", headers), {}

    def h_export_md(self, query, body, rid):
        detail, rec = self._export_detail(rid)
        text = self.call("export", "export_request_md", ((detail,), {}))
        name = (rec.get("slug") or rid) + ".md"
        return 200, Raw(text or "", "text/markdown; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="%s"' % name,
                         "Cache-Control": "no-store"}), {}

    def h_export_html(self, query, body, rid):
        detail, rec = self._export_detail(rid)
        try:
            html_text = self.call("export", "export_request_html", ((self.cfg, detail), {}),
                                  ((detail,), {}))
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - экспорт может упасть на рендере
            self.log.error("export_request_html %s" % rid, exc)
            raise ApiError(502, "Не удалось собрать HTML-экспорт",
                           detail={"stderr": str(exc)[-1000:]}) from exc
        name = (rec.get("slug") or rid) + ".html"
        return 200, Raw(html_text or "", "text/html; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="%s"' % name,
                         "Content-Security-Policy": self._report_csp(),
                         "Cache-Control": "no-store"}), {}

    # --- обслуживание комнаты ---
    def _export_detail(self, rid: str) -> tuple:
        room, rec = self._find_record(rid)
        detail = self._detail(room, rid, rec.get("extra_cards"), rec.get("critic"), rec.get("answers"))
        state = (detail.get("synthesis") or {}).get("state")
        if state != "ready":
            raise err(409, "Ответ ещё не готов", reason="synthesis_pending", rid=rid)
        return detail, rec

