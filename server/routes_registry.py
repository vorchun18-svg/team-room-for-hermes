"""HTTP-маршрут реестра решений комнаты (W25 M1, секция W25-REGISTRY в docs/API.md).

Только чтение: запись в реестр идёт исключительно существующими действиями
принятия/отклонения решения (``POST /api/requests/{id}/actions``) и приёмом
ответа (``ingest``). Отдельного «POST в реестр» нет намеренно: единственный
писатель — приложение, и у него один путь (E1 П5).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_registry import install_registry_routes
    install_registry_routes(App, ROUTES)
"""
from __future__ import annotations

import re as _re
import time

try:  # пакетный и «плоский» импорт
    from . import decision_registry as dec_reg
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    import decision_registry as dec_reg  # type: ignore
    from http_util import ApiError  # type: ignore

ROUTES = [
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/registry", "h_room_registry"),
]


def public_row(row: dict, pins=()) -> dict:
    """Строка реестра для API: только нужные поля, без хешей."""
    return {"id": str(row.get("id") or ""), "at": int(row.get("at") or 0),
            "text": str(row.get("text") or ""), "why": row.get("why") or None,
            "status": str(row.get("status") or "proposed"),
            "prev_status": row.get("prev_status") or None,
            "by": str(row.get("by") or "app"),
            "source_request": row.get("source_request") or None,
            "due": row.get("due") or None,
            "seq": int(row.get("seq") or 0),
            "pinned": str(row.get("id") or "") in set(pins)}


class RegistryRoutes:
    """Обработчики реестра решений (``self`` — экземпляр ``server.app.App``)."""

    def h_room_registry(self, query, body, room_id):
        """``GET /api/rooms/{id}/registry`` — действующие решения и состояние цепочки.

        Параметры: ``history=1`` — все строки журнала (а не свёртка по id);
        ``status=accepted,dismissed`` — только эти статусы; ``limit`` ≤ 500.
        """
        room = self._room(room_id)
        room_id = str(room.get("id") or room_id)
        query = query or {}
        pins = dec_reg.pinned_ids(self.cfg, room_id)
        state = dec_reg.read_state(self.cfg, room_id)
        def one(name: str, default: str = "") -> str:
            """Параметр запроса: ``parse_qs`` отдаёт список значений."""
            value = query.get(name)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            return str(value if value is not None else default).strip()

        wanted = {part.strip() for part in one("status").split(",") if part.strip()}
        unknown = sorted(wanted - set(dec_reg.STATUSES))
        if unknown:
            raise ApiError(400, "Неизвестный статус решения", detail={"allowed": list(dec_reg.STATUSES),
                                                                     "unknown": unknown})
        try:
            limit = int(one("limit") or 200)
        except (TypeError, ValueError):
            raise ApiError(400, "limit — число")
        limit = max(1, min(500, limit))
        history = one("history").lower() in ("1", "true", "да", "yes")
        source = state["rows"] if history else list(state["current"].values())
        items = [row for row in source if not wanted or str(row.get("status")) in wanted]
        items.sort(key=lambda row: (-int(row.get("at") or 0), str(row.get("id") or "")))
        counts = {status: 0 for status in dec_reg.STATUSES}
        for row in state["current"].values():
            counts[str(row.get("status") or "proposed")] = \
                counts.get(str(row.get("status") or "proposed"), 0) + 1
        chain = dec_reg.verify(self.cfg, room_id)
        pinned = dec_reg.pin_state(self.cfg, room_id)
        warnings = []
        if not chain.get("ok"):
            warnings.append({"code": "registry_chain_broken",
                             "message": "Цепочка реестра решений повреждена: %s" % chain.get("reason"),
                             "detail": {"broken_at": chain.get("broken_at")}})
        if pinned.get("over_limit"):
            warnings.append({"code": "pin_limit",
                             "message": pinned.get("warning") or "Достигнут потолок закрепления",
                             "detail": {"count": pinned.get("count"), "limit": pinned.get("limit")}})
        return 200, {
            "room": room_id,
            "generated_at": int(time.time()),
            "registry": {"path": "memory/registry.jsonl", "rows": state["count"],
                         "decisions": len(state["current"]), "append_only": True},
            "chain": {"ok": bool(chain.get("ok")), "rows": chain.get("rows") or 0,
                      "broken_at": chain.get("broken_at"), "reason": chain.get("reason") or ""},
            "counts": counts,
            "pinned": {"ids": pins, "count": pinned.get("count") or 0,
                       "limit": pinned.get("limit") or dec_reg.PIN_LIMIT,
                       "over_limit": bool(pinned.get("over_limit"))},
            "items": [public_row(row, pins) for row in items[:limit]],
            "total": len(items),
            "limit": limit,
            "history": history,
            "warnings": warnings,
        }, {}


def install_registry_routes(app_cls, routes: list) -> None:
    """Подключить реестр решений к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(RegistryRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
