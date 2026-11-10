"""PATCH-маршрут «Закрепить в составе / Открепить» (волна 19, B-28).

Закрепление — флаг `pinned` участника в реестре комнаты (`data/rooms.json`):
участник остаётся в составе, даже если задачи давно не приходят. Раньше этот
флаг показывался на лице карточки значком скрепки без подписи и не нажимался;
теперь это обычное действие меню «⋯» карточки.

Руководитель комнаты и критик закреплены всегда — они задаются настройками
комнаты, поэтому для них операция отклоняется (`409`, `detail.reason="mandatory_role"`).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_members import install_member_routes
    install_member_routes(App, ROUTES)

Маршрут:

    PATCH /api/rooms/{id}/members/{profile}   {pinned: bool}
        → 200 {ok, profile, pinned, members: Member[]}
"""
from __future__ import annotations

import re as _re

try:  # пакетный и «плоский» импорт
    from .app_common import err
except ImportError:  # pragma: no cover
    from app_common import err  # type: ignore

ROOM_RE = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"
PROFILE_RE = r"(?P<profile>[A-Za-z0-9_.@+\-]{1,64})"
BASE = r"/api/rooms/" + ROOM_RE

ROUTES = [
    ("PATCH", BASE + r"/members/" + PROFILE_RE, "h_member_pin"),
]


def _as_bool(value) -> bool:
    """`true/false`, `"true"/"false"`, `1/0` — как у остальных флагов API."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class MemberRoutes:
    """Обработчики состава комнаты (``self`` — экземпляр ``server.app.App``)."""

    def h_member_pin(self, query, body, room_id, profile):
        """``PATCH /api/rooms/{id}/members/{profile}`` — закрепить или открепить участника.

        Тело: `{pinned: true|false}`. Меняется только реестр комнаты; профиль Hermes
        (`profile.yaml`, `SOUL.md`, `config.yaml`) не трогается.
        """
        self._room(room_id)
        data = body if isinstance(body, dict) else {}
        if "pinned" not in data:
            raise err(400, "Нужно поле pinned: true или false", field="pinned")
        value = data.get("pinned")
        if value is None or isinstance(value, (dict, list)):
            raise err(400, "pinned: ожидается true или false", field="pinned", value=value)
        pinned = _as_bool(value)
        with self.engine():
            res = self.call("rooms", "set_member_pinned",
                            ((self.cfg, room_id, profile, pinned), {}))
        members = res.get("members") if isinstance(res, dict) else None
        if members is None:
            members = self._room(room_id).get("members") or []
        return 200, {"ok": True, "profile": profile, "pinned": pinned,
                     "members": members}, {}


def install_member_routes(app_cls, routes: list) -> None:
    """Подключить закрепление участника к ``App``: метод + строка таблицы маршрутов."""
    for name, value in vars(MemberRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
