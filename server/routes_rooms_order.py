"""Порядок комнат в левом меню: перетаскивание, закрепление, группы (W29-P7).

Один маршрут `PUT /api/rooms/order` — одно движение сохраняет всё устройство меню:
порядок, закрепление и группы. Порядок живёт в реестре комнат (`data/rooms.json`),
профили Hermes не меняются ни при каком раскладе.

Формат тела (все ключи необязательны):
    {"order": ["cafe", "laundry", …],
     "groups": [{"id": "work", "title": "Работа", "collapsed": false}],
     "rooms": {"cafe": {"pinned": true, "group": "work"}}}

Ответ: `{ok, changed, rooms: [...], groups: [...]}` — комнаты уже в порядке меню,
чтобы интерфейс не пересортировывал список по-своему.
"""
from __future__ import annotations

from server.http_util import ApiError


class RoomOrderRoutes:
    """Обработчик раскладки меню; доменная проверка и запись — `server/rooms.py`."""

    def h_rooms_order_put(self, query, body):
        payload = body if isinstance(body, dict) else {}
        with self.engine():
            result = self.call("rooms", "apply_layout", ((self.cfg, payload), {}))
        if not isinstance(result, dict):
            raise ApiError(502, "Порядок комнат не сохранён: пустой ответ", detail={})
        return 200, {"ok": True, "changed": bool(result.get("changed")),
                     "rooms": result.get("rooms") or [], "groups": result.get("groups") or []}, {}
