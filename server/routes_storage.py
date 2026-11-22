"""HTTP-маршруты хранилища (W29-P8, docs/API.md § W29-STORAGE).

Экран «Хранилище» и безопасная авто-чистка:

* ``GET  /api/storage``               — размеры по категориям, свободное место,
  что попадёт под чистку (сухой прогон в ответе).
* ``POST /api/storage/clean``         — ``{dry_run?:true, confirm?:false, categories?:[]}``.
  Без ``dry_run:false`` ничего не удаляет; удаление требует ``confirm:true``.
* ``GET/PUT /api/storage/policy``     — настройки хранения (сколько дней/штук).
* ``GET  /api/storage/materials-policy?path=`` — копировать файл в Vault или
  описать ссылкой (путь, размер, хэш, «что это»).
* ``GET/POST /api/rooms/{id}/materials-link`` — ссылка «Материалы» в Vault на
  внешнюю папку проекта (создать/снять; данные не копируются и не удаляются).

Ничего не пишется вне ``data/state/storage.json`` и разрешённых корней
(``server/storage.py``: ``delete_roots``), и только по явному действию человека.
"""
from __future__ import annotations

import re as _re

try:  # пакетный и «плоский» импорт
    from . import storage as storage_mod
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    import storage as storage_mod          # type: ignore
    from http_util import ApiError         # type: ignore

ROUTES = [
    ("GET", r"/api/storage", "h_storage"),
    ("POST", r"/api/storage/clean", "h_storage_clean"),
    ("GET", r"/api/storage/policy", "h_storage_policy_get"),
    ("PUT", r"/api/storage/policy", "h_storage_policy_put"),
    ("GET", r"/api/storage/materials-policy", "h_storage_material_policy"),
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/materials-link", "h_materials_link_get"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/materials-link", "h_materials_link_set"),
]


def _wrap(exc: storage_mod.StorageError) -> ApiError:
    return ApiError(int(exc.status), exc.message, code=exc.code, detail=dict(exc.detail or {}))


def _flag(value, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


def _first(query: dict, name: str, default=None):
    """Параметр запроса: ``parse_qs`` отдаёт список значений."""
    value = (query or {}).get(name)
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return default if value is None else value


class StorageRoutes:
    """Обработчики хранилища (``self`` — экземпляр ``server.app.App``)."""

    def h_storage(self, query, body):
        """``GET /api/storage`` — где что лежит и что попадёт под чистку."""
        data = storage_mod.overview(self.cfg)
        return data

    def h_storage_clean(self, query, body):
        """``POST /api/storage/clean`` — сухой прогон по умолчанию.

        Тело: ``{dry_run?: bool (по умолчанию true), confirm?: bool, categories?: [id]}``.
        Удаление — только при ``dry_run:false`` И ``confirm:true``.
        """
        payload = body if isinstance(body, dict) else {}
        dry_run = _flag(payload.get("dry_run"), True)
        confirm = _flag(payload.get("confirm"), False)
        categories = payload.get("categories")
        if isinstance(categories, str):
            categories = [categories]
        if not isinstance(categories, (list, tuple)):
            categories = []
        try:
            return storage_mod.clean(self.cfg, categories=list(categories),
                                     dry_run=dry_run, confirm=confirm)
        except storage_mod.StorageError as exc:
            raise _wrap(exc)

    def h_storage_policy_get(self, query, body):
        """``GET /api/storage/policy`` — настройки хранения и их границы."""
        return {"ok": True, "policy": storage_mod.load_policy(self.cfg),
                "limits": {key: list(value) for key, value in storage_mod.POLICY_LIMITS.items()},
                "defaults": dict(storage_mod.DEFAULT_POLICY)}

    def h_storage_policy_put(self, query, body):
        """``PUT /api/storage/policy`` — сохранить настройки (только известные ключи)."""
        payload = body if isinstance(body, dict) else {}
        fields = payload.get("policy") if isinstance(payload.get("policy"), dict) else payload
        try:
            saved = storage_mod.save_policy(self.cfg, fields or {})
        except storage_mod.StorageError as exc:
            raise _wrap(exc)
        return {"ok": True, "policy": saved,
                "message": "Настройки хранения сохранены."}

    def h_storage_material_policy(self, query, body):
        """``GET /api/storage/materials-policy?path=`` — копия или ссылка."""
        path = str(_first(query, "path", "") or "").strip()
        if not path:
            raise ApiError(400, "Не указан путь файла (path)", code="bad_request",
                           detail={"field": "path"})
        return {"ok": True, "material": storage_mod.material_reference(self.cfg, path)}

    def h_materials_link_get(self, query, body, room_id):
        """``GET /api/rooms/{id}/materials-link`` — стоит ли ссылка «Материалы»."""
        room = self._room(room_id)
        state = storage_mod.link_state(self.cfg, room)
        state["proposal"] = storage_mod.home_proposal(self.cfg, room)
        return state

    def h_materials_link_set(self, query, body, room_id):
        """``POST /api/rooms/{id}/materials-link`` — ``{action:"create"|"remove"}``."""
        room = self._room(room_id)
        payload = body if isinstance(body, dict) else {}
        action = str(payload.get("action") or "").strip()
        if action not in ("create", "remove"):
            raise ApiError(400, "Действие со ссылкой: «create» или «remove»",
                           code="bad_request", detail={"field": "action", "value": action[:20]})
        try:
            if action == "create":
                result = storage_mod.create_materials_link(
                    self.cfg, room, payload.get("target"), note=_flag(payload.get("note"), True))
            else:
                result = storage_mod.remove_materials_link(self.cfg, room)
        except storage_mod.StorageError as exc:
            raise _wrap(exc)
        result["proposal"] = storage_mod.home_proposal(self.cfg, room)
        return result


def install_storage_routes(app_cls, routes: list) -> None:
    """Подключить хранилище к ``App``: методы миксина + строки таблицы маршрутов."""
    import functools

    def _as_response(func):
        """Обработчики миксина возвращают данные; диспетчер ждёт (код, тело, заголовки)."""
        @functools.wraps(func)
        def inner(self, *args, **kwargs):
            result = func(self, *args, **kwargs)
            if isinstance(result, tuple) and len(result) == 3:
                return result
            return 200, result, {"Cache-Control": "no-store"}
        return inner

    for name, value in vars(StorageRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, _as_response(value) if name.startswith("h_") else value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, _re.compile(pattern).pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))


__all__ = ["StorageRoutes", "ROUTES", "install_storage_routes"]
