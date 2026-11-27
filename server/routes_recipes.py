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
    # ВАЖНО: раньше маршрута с {rid} — иначе «from-request»/«dialog» станет id рецепта.
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/from-request",
     "h_recipe_from_request"),
    # W31-C: разговор о рецепте с руководителем и лёгкий анализ завершённых задач.
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/dialog",
     "h_recipe_dialog_start"),
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/dialog/(?P<dialog_id>[A-Za-z0-9_-]{1,64})",
     "h_recipe_dialog_get"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/dialog/(?P<dialog_id>[A-Za-z0-9_-]{1,64})/answer",
     "h_recipe_dialog_answer"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/dialog/(?P<dialog_id>[A-Za-z0-9_-]{1,64})/fix",
     "h_recipe_dialog_fix"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/dialog/(?P<dialog_id>[A-Za-z0-9_-]{1,64})/accept",
     "h_recipe_dialog_accept"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/dialog/(?P<dialog_id>[A-Za-z0-9_-]{1,64})/cancel",
     "h_recipe_dialog_cancel"),
    ("GET", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/analysis",
     "h_recipe_analysis"),
    ("PUT", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/analysis/settings",
     "h_recipe_analysis_settings"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/analysis/run",
     "h_recipe_analysis_run"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/analysis/later",
     "h_recipe_analysis_later"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/analysis/sync",
     "h_recipe_analysis_sync"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/analysis/(?P<offer_id>[A-Za-z0-9_-]{1,64})/accept",
     "h_recipe_analysis_accept"),
    ("POST", r"/api/rooms/(?P<room_id>[a-z][a-z0-9_-]{1,31})/recipes/analysis/(?P<offer_id>[A-Za-z0-9_-]{1,64})/dismiss",
     "h_recipe_analysis_dismiss"),
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

    def _agent_mod(self):
        """``server.recipe_agent``: разговор о рецепте и лёгкий анализ (W31-C)."""
        return self.mod("recipe_agent")

    def _agent_call(self, method: str, room_id: str, *args, **kw):
        """Вызвать ``server.recipe_agent`` и перевести доменную ошибку в ApiError."""
        room = self._room(room_id)
        mod = self._agent_mod()
        try:
            return getattr(mod, method)(self.cfg, room, *args, **kw)
        except mod.RecipeAgentError as exc:
            raise ApiError(int(exc.http), exc.message, code=exc.code, detail=exc.detail) from exc

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
        payload = self._recipe_call("listing", room_id)
        try:
            payload["dialog"] = self._agent_call("latest", room_id)
        except ApiError:
            payload["dialog"] = None
        try:
            payload["analysis"] = self._agent_call("analysis_view", room_id)
        except ApiError:
            payload["analysis"] = None
        return 200, payload, {"Cache-Control": "no-store"}

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
        """``POST …/recipes/from-request {request_id, name?, text?, plain?}``.

        По умолчанию рецепт собирает руководитель: карточка-задание ставится ВНУТРЬ
        завершённой задачи, он присылает алгоритм, человек утверждает. ``plain: true`` —
        прежний механический путь «шаги = карточки задачи».
        """
        data = body if isinstance(body, dict) else {}
        root_id = str(data.get("request_id") or data.get("id") or "").strip()
        if not root_id:
            raise err(400, "Не указана задача: нужен request_id.", field="request_id")
        if data.get("plain"):
            recipe = self._recipe_call("from_request", room_id, root_id,
                                       str(data.get("name") or ""))
            return 201, {"ok": True, "recipe": recipe, "mode": "plain"}, {"Cache-Control": "no-store"}
        text = str(data.get("text") or "").strip() or "Собери из этой завершённой задачи рецепт."
        dialog = self._agent_call("start", room_id, text, request_id=root_id)
        return 201, {"ok": True, "dialog": dialog, "mode": "agent",
                     "message": "Задача ушла руководителю: он предложит алгоритм, вы утвердите."}, \
            {"Cache-Control": "no-store"}

    # --- разговор о рецепте (W31-C) --------------------------------------
    def h_recipe_dialog_start(self, query, body, room_id):
        """``POST …/recipes/dialog {text}`` — человек описал дело словами (или диктовкой)."""
        data = body if isinstance(body, dict) else {}
        dialog = self._agent_call("start", room_id, str(data.get("text") or ""))
        return 201, {"ok": True, "dialog": dialog,
                     "message": "Руководитель прочитал описание — его ответ появится здесь."}, \
            {"Cache-Control": "no-store"}

    def h_recipe_dialog_get(self, query, body, room_id, dialog_id):
        """``GET …/recipes/dialog/{id}`` — состояние разговора (ответ руководителя разбирается)."""
        return 200, {"ok": True, "dialog": self._agent_call("get", room_id, dialog_id)}, \
            {"Cache-Control": "no-store"}

    def h_recipe_dialog_answer(self, query, body, room_id, dialog_id):
        """``POST …/dialog/{id}/answer {text}`` — ответы на уточняющие вопросы."""
        data = body if isinstance(body, dict) else {}
        return 200, {"ok": True, "dialog": self._agent_call("answer", room_id, dialog_id,
                                                            str(data.get("text") or ""))}, \
            {"Cache-Control": "no-store"}

    def h_recipe_dialog_fix(self, query, body, room_id, dialog_id):
        """``POST …/dialog/{id}/fix {text}`` — «Исправить»: что поправить в алгоритме."""
        data = body if isinstance(body, dict) else {}
        return 200, {"ok": True, "dialog": self._agent_call("fix", room_id, dialog_id,
                                                            str(data.get("text") or ""))}, \
            {"Cache-Control": "no-store"}

    def h_recipe_dialog_accept(self, query, body, room_id, dialog_id):
        """``POST …/dialog/{id}/accept {name?}`` — «Да»: сохранить алгоритм рецептом."""
        data = body if isinstance(body, dict) else {}
        result = self._agent_call("accept", room_id, dialog_id, str(data.get("name") or ""))
        return 201, result, {"Cache-Control": "no-store"}

    def h_recipe_dialog_cancel(self, query, body, room_id, dialog_id):
        """``POST …/dialog/{id}/cancel`` — закрыть разговор без рецепта."""
        return 200, self._agent_call("cancel", room_id, dialog_id), {"Cache-Control": "no-store"}

    # --- лёгкий анализ завершённых задач (W31-C) -------------------------
    def h_recipe_analysis(self, query, body, room_id):
        """``GET …/recipes/analysis`` — настройки, прикидка токенов, предложения."""
        return 200, self._agent_call("analysis_view", room_id), {"Cache-Control": "no-store"}

    def h_recipe_analysis_settings(self, query, body, room_id):
        """``PUT …/analysis/settings {enabled, every_days, min_tasks, model}``."""
        data = body if isinstance(body, dict) else {}
        return 200, {"ok": True, "settings": self._agent_call("set_settings", room_id, data)}, \
            {"Cache-Control": "no-store"}

    def h_recipe_analysis_run(self, query, body, room_id):
        """``POST …/analysis/run`` — «Запустить»: карточка руководителю сейчас (человек)."""
        data = body if isinstance(body, dict) else {}
        return 202, self._agent_call("run", room_id, force=bool(data.get("force"))), \
            {"Cache-Control": "no-store"}

    def h_recipe_analysis_later(self, query, body, room_id):
        """``POST …/analysis/later {days?}`` — «Позже»: отложить напоминание на N дней."""
        data = body if isinstance(body, dict) else {}
        days = data.get("days")
        return 200, self._agent_call("later", room_id, days), {"Cache-Control": "no-store"}

    def h_recipe_analysis_sync(self, query, body, room_id):
        """``POST …/analysis/sync`` — разобрать ответ руководителя (клиент опрашивает)."""
        return 200, self._agent_call("sync_offers", room_id), {"Cache-Control": "no-store"}

    def h_recipe_analysis_accept(self, query, body, room_id, offer_id):
        """``POST …/analysis/{oid}/accept {name?}`` — сделать рецепт из предложения."""
        data = body if isinstance(body, dict) else {}
        result = self._agent_call("accept_offer", room_id, offer_id, str(data.get("name") or ""))
        return 201, result, {"Cache-Control": "no-store"}

    def h_recipe_analysis_dismiss(self, query, body, room_id, offer_id):
        """``POST …/analysis/{oid}/dismiss`` — отклонить (больше не предложится)."""
        return 200, self._agent_call("dismiss", room_id, offer_id), {"Cache-Control": "no-store"}


def install_recipes_routes(app_cls, routes: list) -> None:
    """Добавить методы в ``App``, а маршруты — в общий ``ROUTES`` (без дублей)."""
    for name in dir(RecipeRoutes):
        if name.startswith(("h_", "_recipe", "_agent")):
            setattr(app_cls, name, getattr(RecipeRoutes, name))
    existing = {(method, pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
