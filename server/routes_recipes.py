"""HTTP «Рецепты» комнаты (W29B, пакет Q1, docs/API.md §W29-RECIPES).

Подключение одним блоком в ``server/app.py``::

    from server.routes_recipes import install_recipes_routes
    install_recipes_routes(App, ROUTES)

Маршруты (все — про комнату, рецепты живут в реестре приложения):

* ``GET    /api/rooms/{id}/recipes`` — список, корзина, история запусков, роли;
* ``POST   /api/rooms/{id}/recipes`` — создать рецепт;
* ``POST   /api/rooms/{id}/recipes/from-request`` — сохранить запрос как рецепт;
* ``PATCH  /api/rooms/{id}/recipes/{rid}`` — изменить;
* ``DELETE /api/rooms/{id}/recipes/{rid}`` — в корзину;
* ``POST   /api/rooms/{id}/recipes/{rid}/restore`` — вернуть из корзины;
* ``POST   /api/rooms/{id}/recipes/{rid}/launch`` — запустить (запрос + карточки).

Запись на доску идёт только через ``server.recipes`` → ``engine_write`` (там же
модель и провайдер участника). В песочнице CLI Hermes подставной.
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
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes", "h_recipes"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes", "h_recipe_create"),
    # ВАЖНО: раньше маршрута с {rid} — иначе «from-request» станет id рецепта.
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/from-request",
     "h_recipe_from_request"),
    ("PATCH", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/(?P<recipe_id>[A-Za-z0-9_-]{1,64})",
     "h_recipe_patch"),
    ("DELETE", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/(?P<recipe_id>[A-Za-z0-9_-]{1,64})",
     "h_recipe_delete"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/(?P<recipe_id>[A-Za-z0-9_-]{1,64})/launch",
     "h_recipe_launch"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/(?P<recipe_id>[A-Za-z0-9_-]{1,64})/restore",
     "h_recipe_restore"),
]


class RecipeRoutes:
    """Обработчики рецептов (``self`` — экземпляр ``server.app.App``)."""

    def _recipes_mod(self):
        return self.mod("recipes")

    def _recipe_call(self, method: str, room_id: str, *args, **kw):
        """Вызвать ``server.recipes`` и перевести доменную ошибку в ApiError."""
        room = self._room(room_id)
        mod = self._recipes_mod()
        try:
            return getattr(mod, method)(self.cfg, room, *args, **kw)
        except mod.RecipeError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc

    # --- чтение ---------------------------------------------------------
    def h_recipes(self, query, body, room_id):
        """``GET /api/rooms/{id}/recipes`` — всё для экрана одним ответом."""
        return 200, self._recipe_call("listing", room_id), {"Cache-Control": "no-store"}

    # --- правка ---------------------------------------------------------
    def h_recipe_create(self, query, body, room_id):
        """``POST /api/rooms/{id}/recipes`` — новый рецепт или копия примера."""
        data = body if isinstance(body, dict) else {}
        recipe = self._recipe_call("save", room_id, data, None)
        return 201, recipe, {"Cache-Control": "no-store"}

    def h_recipe_patch(self, query, body, room_id, recipe_id):
        """``PATCH /api/rooms/{id}/recipes/{id}`` — правка (пример становится копией)."""
        data = body if isinstance(body, dict) else {}
        recipe = self._recipe_call("save", room_id, data, recipe_id)
        return 200, recipe, {"Cache-Control": "no-store"}

    def h_recipe_delete(self, query, body, room_id, recipe_id):
        """``DELETE /api/rooms/{id}/recipes/{id}`` — в корзину (можно вернуть)."""
        return 200, self._recipe_call("remove", room_id, recipe_id), {"Cache-Control": "no-store"}

    def h_recipe_restore(self, query, body, room_id, recipe_id):
        """``POST …/recipes/{id}/restore`` — вернуть из корзины."""
        return 200, self._recipe_call("restore", room_id, recipe_id), {"Cache-Control": "no-store"}

    # --- запуск ---------------------------------------------------------
    def h_recipe_launch(self, query, body, room_id, recipe_id):
        """``POST …/recipes/{id}/launch`` — запрос и карточки шагов сразу с моделью."""
        data = body if isinstance(body, dict) else {}
        values = data.get("values") if isinstance(data.get("values"), dict) else data
        result = self._recipe_call("launch", room_id, recipe_id, values)
        return 201, result, {"Cache-Control": "no-store"}

    def h_recipe_from_request(self, query, body, room_id):
        """``POST …/recipes/from-request {request_id, name}`` — рецепт из запроса."""
        data = body if isinstance(body, dict) else {}
        root_id = str(data.get("request_id") or data.get("id") or "").strip()
        if not root_id:
            raise err(400, "Не указан запрос: нужен request_id.", field="request_id")
        recipe = self._recipe_call("from_request", room_id, root_id,
                                   str(data.get("name") or ""))
        return 201, recipe, {"Cache-Control": "no-store"}


def install_recipes_routes(app_cls, routes: list) -> None:
    """Добавить методы в ``App``, а маршруты — в общий ``ROUTES`` (без дублей)."""
    for name in dir(RecipeRoutes):
        if name.startswith(("h_", "_recipe")):
            setattr(app_cls, name, getattr(RecipeRoutes, name))
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
