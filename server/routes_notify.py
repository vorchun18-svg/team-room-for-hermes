"""HTTP раздела «Уведомления Windows» (W29B-Q6).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_notify import install_notify_routes
    install_notify_routes(App, ROUTES)

Маршруты:

* ``GET  /api/notify`` — настройки, канал, тихие часы, журнал последних отправок;
* ``PUT  /api/notify/settings {enabled?, kinds?{…}, quiet_hours?{enabled,from,to}, min_interval_s?}``
  — сохранить правку (проверка внутри ``notify.normalize_settings``);
* ``POST /api/notify/test`` — «Проверить»: одно уведомление в обход выключенности
  и тихих часов. В песочнице пишет в ``TEAMROOM_NOTIFY_FAKE``, окна не показывает.

Ничего не запускает и не читает профили Hermes: только файлы данных приложения.
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
    ("GET", r"/api/notify", "h_notify_get"),
    ("PUT", r"/api/notify/settings", "h_notify_put"),
    ("POST", r"/api/notify/test", "h_notify_test"),
]


class NotifyRoutes:
    """Обработчики уведомлений (``self`` — экземпляр ``server.app.App``)."""

    def _notify_mod(self):
        return self.mod("notify")

    # --- GET /api/notify -------------------------------------------------
    def h_notify_get(self, query, body):
        mod = self._notify_mod()
        try:
            payload = mod.status(self.cfg, port=self.port)
        except Exception as exc:          # noqa: BLE001 — чтение не роняет сервер
            self.log.error("уведомления: чтение настроек", exc)
            raise err(500, "Не удалось прочитать настройки уведомлений") from exc
        return 200, payload, {"Cache-Control": "no-store"}

    # --- PUT /api/notify/settings ---------------------------------------
    def h_notify_put(self, query, body):
        mod = self._notify_mod()
        data = body if isinstance(body, dict) else {}
        if not data:
            raise err(400, "Нужны поля настроек уведомлений", missing=["enabled"])
        try:
            _settings, warnings = mod.save_settings(self.cfg, data)
            payload = mod.status(self.cfg, port=self.port)
        except Exception as exc:          # noqa: BLE001
            self.log.error("уведомления: сохранение настроек", exc)
            raise err(500, "Не удалось сохранить настройки уведомлений") from exc
        payload["warnings"] = warnings
        return 200, payload, {"Cache-Control": "no-store"}

    # --- POST /api/notify/test ------------------------------------------
    def h_notify_test(self, query, body):
        mod = self._notify_mod()
        try:
            result = mod.test_notification(self.cfg, port=self.port, log=self.log)
        except Exception as exc:          # noqa: BLE001
            self.log.error("уведомления: проверка канала", exc)
            raise ApiError(500, "Не удалось показать пробное уведомление",
                           code="notify_test_failed",
                           detail={"error": str(exc)[:300]}) from exc
        if not result.get("ok"):
            # 503 + причина: кнопка «Проверить» обязана объяснить, что не так
            raise ApiError(503, result.get("reason") or "Канал уведомлений недоступен",
                           code="notify_unavailable",
                           detail={"channel": result.get("channel") or ""})
        return 200, {"ok": True, "channel": result.get("channel"),
                     "sent": result.get("sent") or 1,
                     "reason": result.get("reason") or ""}, {"Cache-Control": "no-store"}


def install_notify_routes(app_cls, routes: list) -> None:
    """Подключить уведомления к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(NotifyRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
