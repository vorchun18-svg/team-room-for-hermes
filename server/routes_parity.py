# -*- coding: utf-8 -*-
"""HTTP-маршруты экрана «Совместимость с Hermes» (W29B-Q2).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_parity import install_parity_routes
    install_parity_routes(App, ROUTES)

* ``GET /api/rooms/{id}/parity`` — по каждому участнику: профиль, модель (и
  источник), наборы инструментов, навыки, MCP-серверы, политика разрешений и
  расхождения с обычным запуском агента в Hermes (``server/parity.py``,
  только чтение);
* ``POST /api/rooms/{id}/parity/fix {profile, action:"reset_model"}`` — единственное
  безопасное исправление: вернуть участнику модель профиля (замена комнаты
  снимается штатным ``agent_models.clear``; профили Hermes не меняются).

Ничего не пишется в профили Hermes, CLI не запускается.
"""
from __future__ import annotations

import re as _re

try:                                    # пакетный и «плоский» импорт
    from .http_util import err
except ImportError:                     # pragma: no cover
    from http_util import err           # type: ignore

ROOM_RE = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"
PROFILE_RE = r"(?P<profile>[A-Za-z0-9_.@+\-]{1,64})"

ROUTES = [
    ("GET", r"/api/rooms/" + ROOM_RE + r"/parity", "h_parity_get"),
    ("POST", r"/api/rooms/" + ROOM_RE + r"/parity/fix", "h_parity_fix"),
]

#: Какие исправления экран имеет право прислать (остальное — ошибка, не «тихо мимо»).
FIX_ACTIONS = ("reset_model",)

_STATUS = {"bad_request": 400, "not_found": 404, "conflict": 409}


class ParityRoutes:
    """Обработчики совместимости (``self`` — экземпляр ``server.app.App``)."""

    def _parity_mod(self):
        return self.mod("parity")

    def _parity_room(self, room_id: str) -> dict:
        with self.engine():                     # ошибка реестра → 404/503, не 500
            return self._room(room_id)

    def _parity_error(self, exc: BaseException):
        code = str(getattr(exc, "code", "bad_request"))
        message = getattr(exc, "message", None) or str(exc) or "Не удалось проверить совместимость"
        return err(_STATUS.get(code, 400), message)

    def h_parity_get(self, query, body, room_id):
        """``GET /api/rooms/{id}/parity`` — совместимость участников (только чтение)."""
        room = self._parity_room(room_id)
        mod = self._parity_mod()
        try:
            payload = mod.report(self.cfg, room)
        except mod.ParityError as exc:
            raise self._parity_error(exc) from exc
        return 200, payload, {"Cache-Control": "no-store"}

    def h_parity_fix(self, query, body, room_id):
        """``POST /api/rooms/{id}/parity/fix`` — «Исправить»: модель профиля участнику."""
        data = body if isinstance(body, dict) else {}
        action = str(data.get("action") or "").strip().lower()
        if action not in FIX_ACTIONS:
            raise err(400, "Нужно явное действие: {action: \"reset_model\"}",
                      field="action", allowed=list(FIX_ACTIONS))
        profile = str(data.get("profile") or "").strip()
        if not profile:
            raise err(400, "Укажите участника, которому вернуть модель профиля",
                      field="profile")
        room = self._parity_room(room_id)
        if room.get("archived"):
            raise err(409, "Комната в архиве — модель не меняется", room=room_id)
        mod = self._parity_mod()
        try:
            payload = mod.reset_model(self.cfg, room, profile)
        except mod.ParityError as exc:
            raise self._parity_error(exc) from exc
        return 200, payload, {"Cache-Control": "no-store"}


def install_parity_routes(app_cls, routes: list) -> None:
    """Подключить совместимость к ``App``: методы миксина + строки таблицы маршрутов."""
    for name, value in vars(ParityRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
