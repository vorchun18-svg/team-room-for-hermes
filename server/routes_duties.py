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
        try:
            payload["week"] = mod.week(self.cfg, None if room == ROOM_ALL else room)
        except Exception as exc:          # noqa: BLE001 — сетка не должна ломать список
            self.log.error("дежурства: неделя", exc)
            payload["week"] = {"start": None, "end": None, "days": [], "runs": 0,
                               "failed": 0, "missed": 0, "error": "не удалось посчитать неделю"}
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
