"""HTTP-маршруты выбора модели участника комнаты (WAVE 10, docs/API.md §6.11).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_models import install_model_routes
    install_model_routes(App, ROUTES)

Отдаёт каталог (``GET /api/models``), меняет выбор участника
(``PUT /api/rooms/{r}/agents/{profile}/model``) и перезапускает его идущие карточки
(``POST …/restart``). Все ошибки — по-русски и 4xx: выбор модели не должен отдавать 500.
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

PROFILE_RE = r"(?P<profile>[A-Za-z0-9_.@+\-]{1,64})"
ROOM_RE = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"

ROUTES = [
    ("GET", r"/api/models", "h_models_get"),
    ("PUT", r"/api/rooms/" + ROOM_RE + r"/agents/" + PROFILE_RE + r"/model", "h_agent_model_put"),
    ("POST", r"/api/rooms/" + ROOM_RE + r"/agents/" + PROFILE_RE + r"/restart",
     "h_agent_model_restart"),
    # W13-A: «что реально ушло» и проверка по факту запусков
    ("GET", r"/api/rooms/" + ROOM_RE + r"/models/sent", "h_models_sent"),
    ("POST", r"/api/rooms/" + ROOM_RE + r"/models/check", "h_models_check"),
]

#: что принимаем в теле PUT (всё остальное — ошибка, а не тихо игнорируется)
FIELDS = ("provider", "model", "reasoning")

_STATUS = {"bad_request": 400, "not_found": 404, "unavailable": 503, "conflict": 409}


class ModelRoutes:
    """Обработчики выбора модели (``self`` — экземпляр ``server.app.App``)."""

    # --- помощники ---------------------------------------------------------
    def _models_mod(self):
        return self.mod("agent_models")

    def _models_error(self, exc: BaseException) -> ApiError:
        """Ошибка выбора модели → ApiError с внятным кодом (никогда не 500)."""
        code = getattr(exc, "code", "bad_request")
        message = getattr(exc, "message", None) or str(exc) or "Не удалось применить модель"
        detail = getattr(exc, "detail", None)
        status = _STATUS.get(str(code), 400)
        return ApiError(status, message, code=str(code) if status < 500 else "engine_error",
                        detail=detail if isinstance(detail, dict) else {})

    def _models_room(self, room_id: str) -> dict:
        with self.engine():                       # ошибка реестра → 404/503, не 500
            room = self._room(room_id)
        if room.get("archived"):
            raise err(409, "Комната в архиве — модель не меняется", room=room_id)
        return room

    def _models_call(self, attr: str, args: tuple, kwargs: dict):
        """Вызвать ``agent_models.<attr>``, доменные ошибки — в 4xx по-русски."""
        mod = self._models_mod()
        func = getattr(mod, attr, None)
        if not callable(func):
            raise ApiError(501, "Модуль agent_models не умеет %s" % attr,
                           detail={"module": "agent_models", "function": attr})
        try:
            return func(*args, **kwargs)
        except mod.ModelError as exc:
            raise self._models_error(exc) from exc

    # --- каталог -----------------------------------------------------------
    def h_models_get(self, query, body):
        """``GET /api/models`` — провайдеры с рабочим ключом, их модели и уровни мышления."""
        raw = str((query.get("refresh") or [""])[0] or "").strip().lower()
        force = raw in ("1", "true", "yes", "on")
        mod = self._models_mod()
        try:
            data = mod.catalog(self.cfg, force=force)
        except mod.ModelError as exc:
            raise self._models_error(exc) from exc
        data = dict(data or {})
        data.setdefault("providers", [])
        data.setdefault("efforts", [])
        data.setdefault("warnings", [])
        return 200, data, {"Cache-Control": "no-store"}

    # --- выбор участника ---------------------------------------------------
    def h_agent_model_put(self, query, body, room_id, profile):
        """``PUT /api/rooms/{id}/agents/{profile}/model`` — модель/провайдер/мышление.

        Пустой объект или ``null`` в теле (а также ``model: null`` без провайдера) — «вернуть
        по умолчанию». Файлы профиля не меняются: замена живёт только в реестре комнаты.
        """
        room = self._models_room(room_id)
        data = body if isinstance(body, dict) else {}
        unknown = sorted(set(data) - set(FIELDS))
        if unknown:
            raise err(400, "Эти поля модель не принимает", fields=unknown, allowed=list(FIELDS))
        provider = data.get("provider")
        model = data.get("model")
        reasoning = data.get("reasoning")
        for name, value in (("provider", provider), ("model", model), ("reasoning", reasoning)):
            if value is not None and not isinstance(value, str):
                raise err(400, "Поле «%s» должно быть строкой" % name, field=name,
                          type=type(value).__name__)
        if provider and not str(model or "").strip():
            raise err(400, "Выберите модель для выбранного провайдера", field="model")

        if not str(provider or "").strip() and not str(model or "").strip() \
                and not str(reasoning or "").strip():
            res = self._models_call("clear", (self.cfg, room_id, profile), {})
        else:
            res = self._models_call("set", (self.cfg, room_id, profile),
                                    {"provider": provider or "", "model": model or "",
                                     "reasoning": "" if reasoning is None else reasoning})
        cleared = bool(res.get("cleared"))
        try:                       # сразу применить к не идущим карточкам участника
            applied = self._models_call("apply_profile", (self.cfg, room_id, profile), {})
        except ApiError as exc:    # карточки — вторичное: выбор уже сохранён
            self.log.warning("модель: карточки не обновлены (%s)" % exc.message)
            applied = {"cards_updated": 0, "pending_running": 0, "cards_failed": [],
                       "warning": "Модель сохранена, но карточки обновились не все."}
        warnings = list(res.get("warnings") or [])
        if applied.get("warning"):
            warnings.append(str(applied["warning"]))
        try:
            self._models_call("announce", (self.cfg, room_id, profile), {"cleared": cleared})
        except ApiError:
            pass                    # журнал вторичен — модель уже изменена
        fresh = self._models_room(room_id)          # свежая комната: новая замена уже в ней
        member = self._models_call("member_of", (fresh, self.cfg, profile), {})
        settings = dict((member or {}).get("model_settings") or {})
        payload = {"ok": True, "profile": profile,
                   "member": member,
                   "agent_models": res.get("agent_models") or {},
                   "cleared": cleared,
                   # `applied.settings` — эхо того, что сохранено и пойдёт в следующую карточку:
                   # фронтенд сверяет выбор с ответом, не переспрашивая комнату. Значения —
                   # ровно из реестра (`provider`/`model` — как выбрано, без «ближайших»).
                   "applied": {"cards_updated": int(applied.get("cards_updated") or 0),
                               "pending_running": int(applied.get("pending_running") or 0),
                               "cards_failed": applied.get("cards_failed") or [],
                               "settings": settings,
                               "saved": dict(res.get("room_setting") or {}),
                               "cleared": cleared},
                   "warnings": warnings}
        return 200, payload, {}

    # --- перезапуск --------------------------------------------------------
    def h_agent_model_restart(self, query, body, room_id, profile):
        """``POST /api/rooms/{id}/agents/{profile}/restart`` — «Перезапустить на новой модели»."""
        self._models_room(room_id)
        res = self._models_call("restart_running", (self.cfg, room_id, profile), {})
        return 200, {"ok": True, "restarted": int(res.get("restarted") or 0),
                     "cards": res.get("cards") or [],
                     "warnings": res.get("warnings") or []}, {}


    # --- «ушло на» ---------------------------------------------------------
    def h_models_sent(self, query, body, room_id):
        """``GET /api/rooms/{id}/models/sent`` — выбрано комнатой ↔ реально ушло.

        Для каждого участника: ``model_settings`` (выбор) и ``model_sent`` (факт по его
        последнему запуску из сессии воркера) с пометкой расхождения. Плюс ``cards`` —
        карточки комнаты, где факт уже известен или запись расходится с выбором.
        """
        room = self._models_room(room_id)
        mod = self._models_mod()
        members = []
        try:
            from .agent_models import participants  # noqa: PLC0415 - ленивый импорт
        except ImportError:
            from agent_models import participants  # type: ignore
        for profile, role in participants(room):
            item = {"profile": profile, "role": role,
                    "model_settings": mod.model_settings(self.cfg, room, profile)}
            try:
                item["model_sent"] = mod.member_sent(self.cfg, room, profile)
            except Exception:  # noqa: BLE001 - «ушло на» вторично
                item["model_sent"] = {}
            members.append(item)
        cards = []
        try:
            for card in (mod.card_map(self.cfg, room) or {}).values():
                sent = mod.sent_of(self.cfg, room, card,
                                   board=room.get("board") or self.board)
                if not sent:
                    continue
                if not (sent.get("confirmed") or sent.get("mismatch")):
                    continue
                cards.append({"id": card["id"], "assignee": card["assignee"],
                              "status": card["status"], "model_label": sent["label"],
                              "model_sent": sent})
        except Exception:  # noqa: BLE001 - карточки вторичны для ответа
            cards = []
        cards.sort(key=lambda c: (not c["model_sent"].get("mismatch"), c["id"]))
        return 200, {"room": room_id, "generated_at": int(_time.time()),
                     "members": members, "cards": cards[:50]}, {"Cache-Control": "no-store"}

    def h_models_check(self, query, body, room_id):
        """``POST /api/rooms/{id}/models/check`` — сверить факт запусков и вернуть «не ту».

        ``fix=false`` — только отчёт (ничего не останавливать). Это та же проверка, что
        сторож делает каждый тик; кнопка нужна, чтобы не ждать минуту.
        """
        room = self._models_room(room_id)
        data = body if isinstance(body, dict) else {}
        fix = data.get("fix")
        fix = True if fix is None else bool(fix)
        res = self._models_call("verify_started", (self.cfg, room_id), {"fix": fix})
        return 200, {"ok": True, "room": room_id, "fix": bool(fix),
                     "checked": int(res.get("checked") or 0),
                     "confirmed": int(res.get("confirmed") or 0),
                     "mismatched": res.get("mismatched") or [],
                     "fixed": res.get("fixed") or [],
                     "failed": res.get("failed") or [],
                     "unknown": int(res.get("unknown") or 0)}, {}


def install_model_routes(app_cls, routes: list) -> None:
    """Подключить выбор модели к ``App``: методы миксина + строки таблицы маршрутов."""
    for name, value in vars(ModelRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
