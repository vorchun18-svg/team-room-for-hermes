"""HTTP «Уровни автономии комнаты» (W27-E5, docs/API.md §W27-AUTONOMY).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_autonomy import install_autonomy_routes
    install_autonomy_routes(App, ROUTES)

Маршруты (контракт WAVE27 §2):

* ``GET  /api/rooms/{id}/autonomy`` — ``{level, never, profiles[], defaults,
  preview:{careful,working,free}}``; чтение конфигов профилей, ничего не пишет;
* ``POST /api/rooms/{id}/autonomy {level, never?, confirm}`` — применить уровень к
  профилям участников комнаты; без ``confirm: true`` — ``409 confirm_required``
  с точным предпросмотром «что изменится» и копией старого конфига при записи;
* ``GET  /api/rooms/{id}/autonomy/journal`` — что агенты сделали сами по уровню
  (строка = профиль, класс, время, карточка), без счётчика «нужно от вас»;
* ``GET  /api/rooms/{id}/autonomy/suggest`` — обёртка ``hermes approvals suggest``
  (только чтение; в песочнице и тестах — подставной CLI).
"""
from __future__ import annotations

import re as _re
import time as _time

try:  # пакетный и «плоский» импорт
    from .app_common import err
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    from app_common import err  # type: ignore
    from http_util import ApiError  # type: ignore

ROOM_RE = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"
BASE = r"/api/rooms/" + ROOM_RE

ROUTES = [
    ("GET", BASE + r"/autonomy", "h_autonomy_get"),
    ("POST", BASE + r"/autonomy", "h_autonomy_set"),
    ("GET", BASE + r"/autonomy/journal", "h_autonomy_journal"),
    ("GET", BASE + r"/autonomy/suggest", "h_autonomy_suggest"),
]


def _first(query: dict, name: str) -> str:
    """Первое значение параметра запроса (``?limit=60``) как строка."""
    try:
        values = (query or {}).get(name) or []
    except AttributeError:                   # pragma: no cover - query уже строка
        return ""
    if isinstance(values, str):
        return values
    for value in values:
        return "" if value is None else str(value)
    return ""


class AutonomyRoutes:
    """Обработчики уровней автономии (``self`` — экземпляр ``server.app.App``)."""

    def _autonomy_mod(self):
        return self.mod("autonomy")

    def _raise(self, exc) -> None:
        """Доменная ошибка уровня → ответ API с кодом (``409`` и т. д.)."""
        raise ApiError(int(getattr(exc, "http", 400) or 400),
                       getattr(exc, "message", None) or str(exc) or
                       "Не удалось изменить уровень автономии",
                       code=str(getattr(exc, "code", "bad_request")),
                       detail=getattr(exc, "detail", None) or {})

    def h_autonomy_get(self, query, body, room_id):
        """``GET /api/rooms/{id}/autonomy`` — уровень, профили, умолчания, предпросмотр."""
        mod = self._autonomy_mod()
        room = self._room(room_id)
        try:
            payload = mod.view(self.cfg, room)
        except Exception as exc:            # noqa: BLE001 - чтение не должно давать 500
            self.log.error("автономия %s: %s" % (room_id, exc))
            raise err(503, "Уровень автономии сейчас недоступен — попробуйте ещё раз",
                      reason="autonomy_unavailable") from exc
        payload["at"] = int(_time.time())
        return 200, payload, {"Cache-Control": "no-store"}

    def h_autonomy_set(self, query, body, room_id):
        """``POST /api/rooms/{id}/autonomy {level, never?, confirm:true}`` — применить.

        Без ``confirm: true`` — ``409 confirm_required`` с предпросмотром (§2).
        Запись — только в профили участников ЭТОЙ комнаты и только по этому действию.
        """
        mod = self._autonomy_mod()
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        level = data.get("level")
        if not isinstance(level, str) or not level.strip():
            raise err(400, "Нужно поле level: careful, working или free",
                      missing=["level"], allowed=list(mod.LEVELS))
        never = data.get("never")
        if never is not None and not isinstance(never, list):
            raise err(400, "Поле never должно быть списком масок", field="never")
        try:
            payload = mod.apply_level(self.cfg, room, level.strip().lower(),
                                      never=never, confirm=bool(data.get("confirm")))
        except mod.AutonomyError as exc:
            self._raise(exc)
        except ApiError:
            raise
        except Exception as exc:            # noqa: BLE001 - понятная ошибка вместо 500
            self.log.error("автономия %s: запись: %s" % (room_id, exc))
            raise err(503, "Не удалось изменить уровень автономии — попробуйте ещё раз",
                      reason="autonomy_unavailable") from exc
        payload["request_id"] = room_id
        return 200, payload, {"Cache-Control": "no-store"}

    def h_autonomy_journal(self, query, body, room_id):
        """``GET /api/rooms/{id}/autonomy/journal?limit=60`` — журнал «что сделано само»."""
        mod = self._autonomy_mod()
        room = self._room(room_id)
        try:
            limit = int(_first(query, "limit") or 60)
        except (TypeError, ValueError):
            raise err(400, "Параметр limit должен быть числом", field="limit") from None
        payload = mod.journal(self.cfg, room, limit=limit)
        return 200, payload, {"Cache-Control": "no-store"}

    def h_autonomy_suggest(self, query, body, room_id):
        """``GET /api/rooms/{id}/autonomy/suggest`` — подсказки из истории разрешений."""
        mod = self._autonomy_mod()
        room = self._room(room_id)
        try:
            days = int(_first(query, "days") or 90)
            limit = int(_first(query, "limit") or 10)
        except (TypeError, ValueError):
            raise err(400, "Параметры days и limit должны быть числами",
                      field="days") from None
        payload = mod.suggest(self.cfg, room, days=max(0, days), limit=limit)
        return 200, payload, {"Cache-Control": "no-store"}


def install_autonomy_routes(app_cls, routes: list) -> None:
    """Подключить уровни автономии к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(AutonomyRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
