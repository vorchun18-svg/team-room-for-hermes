"""HTTP «Дежурства» и «Здоровье интеграций» (WAVE24 §3.2–3.3, docs/API.md §W24-DUTIES).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_duties import install_duties_routes
    install_duties_routes(App, ROUTES)

Что здесь:

* ``GET /api/duties?room=<id|all>`` — задания Hermes по расписанию (только чтение);
* ``PUT /api/rooms/{id}/duties {job_ids:[...]}`` — привязка дежурств к комнате
  (меняется только реестр комнат приложения, Hermes не меняется);
* ``POST /api/duties/{id}/actions`` — «Запустить сейчас / Пауза / Возобновить»
  (штатный CLI `hermes cron …`, только по подтверждению человека), «Привязать /
  Отвязать» (реестр комнат) и «Изменить расписание» — **предложение**, Hermes не
  меняется (W26-D8);
* ``GET /api/health/integrations`` — короткий список проверок здоровья.

Чтения — только чтение. Запись в Hermes бывает ровно одним путём: ``act`` →
`hermes cron run|pause|resume <id>` по подтверждению; в тестах и песочнице CLI
подставной (`duties.set_cli_runner`, `TEAMROOM_HERMES_BIN`, `TEAMROOM_DUTIES_DRY`).
"""
from __future__ import annotations

import re as _re

try:                                     # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError
except ImportError:                      # pragma: no cover
    from app_common import err           # type: ignore
    from http_util import ApiError       # type: ignore

ROUTES = [
    ("GET", r"/api/duties", "h_duties"),
    ("POST", r"/api/duties/(?P<job_id>[A-Za-z0-9_.-]{1,64})/actions", "h_duty_action"),
    ("PUT", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/duties", "h_room_duties_put"),
    ("GET", r"/api/health/integrations", "h_health_integrations"),
    # --- W27-E7: мастер, правка, корзина, история, пробный прогон -------------
    ("GET", r"/api/duty-templates", "h_duty_templates"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/duties", "h_room_duty_create"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/duties/process", "h_duty_process"),
    ("PATCH", r"/api/duties/(?P<job_id>[A-Za-z0-9_.-]{1,64})", "h_duty_patch"),
    ("DELETE", r"/api/duties/(?P<job_id>[A-Za-z0-9_.-]{1,64})", "h_duty_delete"),
    ("POST", r"/api/duties/(?P<job_id>[A-Za-z0-9_.-]{1,64})/restore", "h_duty_restore"),
    ("GET", r"/api/duties/(?P<job_id>[A-Za-z0-9_.-]{1,64})/history", "h_duty_history"),
    ("POST", r"/api/duties/(?P<job_id>[A-Za-z0-9_.-]{1,64})/dry_run", "h_duty_dry_run"),
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/duty-proposals", "h_duty_proposals"),
    ("POST", r"/api/duty-proposals/(?P<proposal_id>[A-Za-z0-9_.-]{1,64})", "h_duty_proposal_act"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/duty-cards/(?P<item_id>[A-Za-z0-9_.-]{1,64})",
     "h_duty_card_act"),
    # --- W29-P5: выбор папки/файла окном Windows для мастера дежурств ----------
    ("POST", r"/api/folders/pick", "h_folder_pick"),
]

#: Сколько знаков отдаём в «кратком итоге» и как зовём общий режим в ответе.
ROOM_ALL = "all"
#: Комната по умолчанию: пользователь чаще смотрит «все дежурства», а не пустую комнату.
ROOM_DEFAULT = ROOM_ALL


def _first(query: dict, name: str) -> str:
    """Первое значение параметра запроса (``?room=…``) как строка."""
    try:
        values = (query or {}).get(name) or []
    except AttributeError:               # pragma: no cover - query уже строка
        return ""
    if isinstance(values, str):
        return values
    for value in values:
        return "" if value is None else str(value)
    return ""


class DutiesRoutes:
    """Обработчики дежурств и здоровья (``self`` — экземпляр ``server.app.App``)."""

    def _duties_mod(self):
        return self.mod("duties")

    def _health_mod(self):
        return self.mod("health")

    # --- GET /api/duties -------------------------------------------------
    def h_duties(self, query, body):
        room = _first(query, "room").strip() or ROOM_DEFAULT
        if room != ROOM_ALL:
            self._room(room)              # неизвестная комната → 404 по-русски
        mod = self._duties_mod()
        try:
            payload = mod.items(self.cfg, None if room == ROOM_ALL else room)
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001 — чтение не должно ронять сервер
            self.log.error("дежурства: чтение", exc)
            raise err(500, "Не удалось прочитать дежурства") from exc
        # W26-D8: недельная сетка в том же ответе — интерфейсу не нужен второй запрос.
        # W29-P5: `?start=YYYY-MM-DD` — листание недель стрелками ‹ › и «Сегодня».
        start = _first(query, "start").strip()
        try:
            payload["week"] = mod.week(self.cfg, None if room == ROOM_ALL else room,
                                       start=start or None)
        except ValueError as exc:
            raise err(400, str(exc), code="bad_request",
                      field="start", value=start[:10]) from exc
        except Exception as exc:          # noqa: BLE001 — сетка не должна ломать список
            self.log.error("дежурства: неделя", exc)
            payload["week"] = {"start": None, "end": None, "days": [], "runs": 0,
                               "failed": 0, "missed": 0, "error": "не удалось посчитать неделю"}
        # W27-E7: правила и корзина комнаты — в том же ответе (один запрос на экран).
        payload["rules"] = {}
        payload["trash"] = []
        if room != ROOM_ALL:
            try:
                rules_mod = self._rules_mod()
                for item in payload.get("items") or []:
                    job_id = str(item.get("job_id") or "")
                    found = rules_mod.public_rule(rules_mod.rule(self.cfg, room, job_id))
                    if found:
                        item["rule"] = found
                        payload["rules"][job_id] = found
                payload["trash"] = rules_mod.trash(self.cfg, room)["items"]
            except Exception as exc:      # noqa: BLE001 — правила не должны ломать список
                self.log.error("дежурства: правила", exc)
                payload["rules_error"] = "не удалось прочитать правила"
        return 200, payload, {"Cache-Control": "no-store"}

    # --- POST /api/duties/{id}/actions (W26-D8) --------------------------
    def h_duty_action(self, query, body, job_id):
        """Действие над дежурством: только по подтверждению (`confirm: true`)."""
        data = body if isinstance(body, dict) else {}
        action = str(data.get("action") or "").strip().lower()
        if not action:
            raise err(400, "Нужно действие: action", missing=["action"])
        room = None
        room_id = str(data.get("room") or _first(query, "room") or "").strip()
        if room_id and room_id != ROOM_ALL:
            room = self._room(room_id)
        mod = self._duties_mod()
        try:
            result = mod.act(self.cfg, room, job_id, action,
                             confirm=bool(data.get("confirm")),
                             schedule=data.get("schedule") or "")
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: действие", exc)
            raise err(500, "Не удалось выполнить действие с дежурством") from exc
        return 200, result, {"Cache-Control": "no-store"}

    # --- PUT /api/rooms/{id}/duties --------------------------------------
    def h_room_duties_put(self, query, body, room_id):
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        if "job_ids" not in data:
            raise err(400, "Нужен список дежурств: job_ids", missing=["job_ids"])
        mod = self._duties_mod()
        try:
            bound = mod.set_room_duties(self.cfg, room, data.get("job_ids"))
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: привязка", exc)
            raise err(500, "Не удалось сохранить привязку дежурств") from exc
        # После записи отдаём свежие данные дежурств этой комнаты: интерфейсу не нужен
        # второй запрос, а пользователь сразу видит результат.
        try:
            items = mod.items(self.cfg, None if not bound else str(room.get("id")))
        except Exception:                 # noqa: BLE001 — привязка уже сохранена
            items = {"generated_at": None, "items": []}
        return 200, {"ok": True, "room": str(room.get("id")),
                     "duties": [str(x) for x in (bound or [])],
                     "generated_at": items.get("generated_at"),
                     "items": items.get("items") or []}, {"Cache-Control": "no-store"}

    # --- GET /api/health/integrations ------------------------------------
    def h_health_integrations(self, query, body):
        mod = self._health_mod()
        try:
            payload = mod.run(self.cfg)
        except Exception as exc:          # noqa: BLE001 — отчёт обязан быть
            self.log.error("здоровье: проверки", exc)
            raise err(500, "Не удалось проверить интеграции") from exc
        return 200, payload, {"Cache-Control": "no-store"}

    # --- W27-E7: шаблоны, мастер, правка, корзина, история ----------------
    def _rules_mod(self):
        return self.mod("duty_rules")

    def _flag(self, value) -> bool:
        """``"true"``/``"1"``/``True`` → True (флаги приходят и строкой запроса)."""
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in ("1", "true", "yes", "on", "да")

    def _room_for_job(self, job_id, data: dict, query):
        """Комната дежурства: из запроса, иначе — та, к которой оно привязано."""
        room_id = str((data or {}).get("room") or _first(query, "room") or "").strip()
        if room_id and room_id != ROOM_ALL:
            return self._room(room_id)
        try:
            for room in self.mod("rooms").list_rooms(self.cfg, include_archived=True):
                if str(job_id) in [str(one) for one in (room.get("duties") or [])]:
                    return room
        except Exception:                 # noqa: BLE001 — реестр может быть занят
            pass
        return {"id": room_id or "", "duties": []}

    def h_duty_templates(self, query, body):
        """``GET /api/duty-templates`` — универсальные шаблоны для мастера."""
        mod = self.mod("duty_templates")
        return 200, mod.list_templates(), {"Cache-Control": "no-store"}

    def h_room_duty_create(self, query, body, room_id):
        """``POST /api/rooms/{id}/duties`` — предпросмотр или создание (по «да»)."""
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        mod = self._duties_mod()
        want_preview = self._flag(data.get("preview"))
        try:
            result = mod.create(self.cfg, room, data, preview_only=want_preview,
                                confirm=self._flag(data.get("confirm")))
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: создание", exc)
            raise err(500, "Не удалось создать дежурство") from exc
        return (200 if want_preview else 201), result, {"Cache-Control": "no-store"}

    def h_duty_process(self, query, body, room_id):
        """``POST /api/rooms/{id}/duties/process`` — обработать свежие результаты.

        Обычно это делает тик сторожа; маршрут — для кнопки «Обновить» и проверок.
        Лимит и идемпотентность те же (`duty_rules.process_runs`).
        """
        room = self._room(room_id)
        mod = self._rules_mod()
        try:
            mod.sync_inbox_drafts(self.cfg, room)
        except Exception:                 # noqa: BLE001 — диктовка вторична
            pass
        try:
            stats = mod.process_runs(self.cfg, room)
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: обработка результатов", exc)
            raise err(500, "Не удалось обработать результаты дежурств") from exc
        return 200, {"ok": True, **stats}, {"Cache-Control": "no-store"}

    def h_duty_patch(self, query, body, job_id):
        """``PATCH /api/duties/{job_id}`` — расписание, задание, правило, пауза."""
        data = body if isinstance(body, dict) else {}
        room = self._room_for_job(job_id, data, query)
        mod = self._duties_mod()
        try:
            result = mod.patch(self.cfg, job_id, room, data,
                               preview_only=self._flag(data.get("preview")),
                               confirm=self._flag(data.get("confirm")))
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: правка", exc)
            raise err(500, "Не удалось изменить дежурство") from exc
        return 200, result, {"Cache-Control": "no-store"}

    def h_duty_delete(self, query, body, job_id):
        """``DELETE /api/duties/{job_id}?confirm=true`` — в корзину на 30 дней."""
        data = body if isinstance(body, dict) else {}
        room = self._room_for_job(job_id, data, query)
        confirm = self._flag(data.get("confirm")) or self._flag(_first(query, "confirm"))
        mod = self._duties_mod()
        try:
            result = mod.remove(self.cfg, job_id, room, confirm=confirm)
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: удаление", exc)
            raise err(500, "Не удалось удалить дежурство") from exc
        return 200, result, {"Cache-Control": "no-store"}

    def h_duty_restore(self, query, body, job_id):
        """``POST /api/duties/{job_id}/restore`` — вернуть из корзины (создать заново)."""
        data = body if isinstance(body, dict) else {}
        room = self._room_for_job(job_id, data, query)
        mod = self._duties_mod()
        try:
            result = mod.restore(self.cfg, room, job_id,
                                 confirm=self._flag(data.get("confirm")))
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: возврат из корзины", exc)
            raise err(500, "Не удалось вернуть дежурство") from exc
        return 201 if result.get("applied") else 200, result, {"Cache-Control": "no-store"}

    def h_duty_history(self, query, body, job_id):
        """``GET /api/duties/{job_id}/history`` — что нашло и что создало."""
        room = self._room_for_job(job_id, {}, query)
        mod = self._rules_mod()
        try:
            rows = mod.history(self.cfg, room, job_id, limit=40)
            rule = mod.rule(self.cfg, room, job_id)
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: история", exc)
            raise err(500, "Не удалось прочитать историю дежурства") from exc
        return 200, {"ok": True, "job_id": str(job_id), "room": room.get("id"),
                     "rule": mod.public_rule(rule), "items": rows}, {"Cache-Control": "no-store"}

    def h_duty_dry_run(self, query, body, job_id):
        """``POST /api/duties/{job_id}/dry_run`` — «что бы нашла», без запуска."""
        mod = self._duties_mod()
        try:
            result = mod.dry_run(self.cfg, job_id)
        except mod.DutiesError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: пробный прогон", exc)
            raise err(500, "Не удалось показать будущий результат") from exc
        return 200, result, {"Cache-Control": "no-store"}

    def h_duty_proposals(self, query, body, room_id):
        """``GET /api/rooms/{id}/duty-proposals`` — «Предлагаю дежурство»."""
        room = self._room(room_id)
        mod = self._rules_mod()
        state = _first(query, "state").strip() or "proposed"
        try:
            mod.sync_inbox_drafts(self.cfg, room)
            payload = mod.proposals(self.cfg, room, state)
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: предложения", exc)
            raise err(500, "Не удалось прочитать предложения дежурств") from exc
        payload["room_title"] = room.get("title")
        return 200, payload, {"Cache-Control": "no-store"}

    def h_duty_proposal_act(self, query, body, proposal_id):
        """``POST /api/duty-proposals/{id} {action}`` — создать/изменить/не надо."""
        data = body if isinstance(body, dict) else {}
        room_id = str(data.get("room") or _first(query, "room") or "").strip()
        if not room_id:
            raise err(400, "Нужна комната: room", missing=["room"])
        room = self._room(room_id)
        mod = self._rules_mod()
        try:
            result = mod.act_proposal(self.cfg, room, proposal_id,
                                      str(data.get("action") or "create"), data)
        except mod.DutyRuleError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: решение по предложению", exc)
            raise err(500, "Не удалось разобрать предложение дежурства") from exc
        return 200, result, {"Cache-Control": "no-store"}

    def h_duty_card_act(self, query, body, room_id, item_id):
        """``POST /api/rooms/{id}/duty-cards/{item_id}`` — решение по результату дежурства."""
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        mod = self._rules_mod()
        try:
            result = mod.decide_card(self.cfg, room, item_id,
                                     str(data.get("action") or ""), data)
        except mod.DutyRuleError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc
        except Exception as exc:          # noqa: BLE001
            self.log.error("дежурства: решение по карточке", exc)
            raise err(500, "Не удалось применить решение по карточке дежурства") from exc
        return 200, result, {"Cache-Control": "no-store"}

    # --- POST /api/folders/pick (W29-P5) ----------------------------------
    def h_folder_pick(self, query, body):
        """Окно Windows «Выбрать папку» (или файл) для мастера дежурств.

        Тело: ``{title?, mode?: "folder"|"file"}``. Ответ:
        ``{ok, path, paths, cancelled, kind, exists, message}``.
        Окно закрыли — это не ошибка: ``cancelled:true``, человек вписывает путь сам.
        """
        data = body if isinstance(body, dict) else {}
        mod = self.mod("folder_pick")
        title = data.get("title")
        if title is not None and not isinstance(title, str):
            raise err(400, "title: строка", field="title")
        mode = mod.normalize_mode(data.get("mode"))
        try:
            tmp_dir = getattr(self.cfg, "tmp_dir", None)
        except (AttributeError, TypeError):
            tmp_dir = None
        try:
            path = mod.pick_folder(title or "", mode=mode, tmp_dir=tmp_dir)
        except mod.PickError as exc:
            raise ApiError(int(exc.status), exc.message, code=exc.code,
                           detail=exc.detail) from exc
        info = mod.check_path(path) if path else {"ok": False, "path": "", "kind": "",
                                                  "exists": None, "message": ""}
        return 200, {"ok": True, "path": str(path or ""), "paths": [path] if path else [],
                     "cancelled": not path, "mode": mode, "kind": info.get("kind") or "",
                     "exists": info.get("exists"), "message": info.get("message") or ""}, \
            {"Cache-Control": "no-store"}


def install_duties_routes(app_cls, routes: list) -> None:
    """Подключить дежурства и здоровье к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(DutiesRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
