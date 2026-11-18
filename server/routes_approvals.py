"""HTTP «Разрешения агентам» (WAVE22 §2, docs/API.md §W22-APPROVALS).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_approvals import install_approvals_routes
    install_approvals_routes(App, ROUTES)

Что здесь:

* ``GET /api/approvals/grants?room=`` — постоянные и временные разрешения профилей
  комнаты (чтение ``command_allowlist``, ничего не пишется);
* ``POST /api/approvals/revoke {profile, pattern_key, room?}`` — отзыв штатным путём
  Hermes;
* ``_approvals_view(detail)`` — поле ``approvals[]`` и счётчик ``approvals_pending``
  в деталях запроса (зовётся из ``routes_read.h_request_get``);
* ``_approval_action(room, rec, rid, body)`` — действие ``{action:"approval"}` внутри
  ``POST /api/requests/{id}/actions`` (зовётся из ``routes_write.h_request_actions``).

Никаких решений «за пользователя»: сервер только ищет блокировки и исполняет нажатие.
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

ROUTES = [
    ("GET", r"/api/approvals/grants", "h_approvals_grants"),
    ("POST", r"/api/approvals/revoke", "h_approvals_revoke"),
    ("GET", r"/api/approvals/trusts", "h_approvals_trusts"),
    ("POST", r"/api/approvals/untrust", "h_approvals_untrust"),
    ("POST", r"/api/approvals/tidy", "h_approvals_tidy"),
]

#: Сколько знаков заметки пользователя принимаем (подпись к решению).
MAX_NOTE = 1000


def _first(query: dict, name: str) -> str:
    """Первое значение параметра запроса (``?room=…``) как строка."""
    try:
        values = (query or {}).get(name) or []
    except AttributeError:                   # pragma: no cover - query уже строка
        return ""
    if isinstance(values, str):
        return values
    for value in values:
        return "" if value is None else str(value)
    return ""


class ApprovalRoutes:
    """Обработчики разрешений (``self`` — экземпляр ``server.app.App``)."""

    def _approvals_mod(self):
        return self.mod("approvals")

    # --- чтение: поле approvals[] в деталях запроса (W22 §2.1) --------------
    def _approvals_view(self, room: dict, rec: dict, detail: dict) -> None:
        """``approvals[]`` и ``approvals_pending`` у запроса + пересчёт ``needs_you``.

        Сбой чтения не роняет страницу: разрешения — дополнение к ответу, а не сам ответ.
        W26-D1: ``approvals_pending`` и ``needs_you`` считают только строки с действием —
        недоговорная блокировка и жёсткий запрет не «ждёт вашего решения».
        """
        if not isinstance(detail, dict) or not self._has_mod("approvals"):
            return
        root_id = str(detail.get("id") or (rec or {}).get("root_id") or "")
        if not root_id:
            return
        try:
            mod = self.mod("approvals")
            items = mod.collect(self.cfg, room, root_id, (rec or {}).get("extra_cards"))
        except Exception as exc:            # noqa: BLE001 - страница важнее разрешений
            self.log.warning("разрешения %s: %s" % (root_id, exc))
            detail.setdefault("approvals", [])
            detail.setdefault("approvals_pending", 0)
            return
        detail["approvals"] = items
        waiting = mod.pending(items)        # W26-D1: только «можно разрешить» + действие
        detail["approvals_pending"] = len(waiting)
        detail["approvals_actionable"] = len(mod.actionable(items))
        if waiting:
            detail["needs_you"] = True

    # --- действие «разрешить/запретить» (W22 §2.2, W26-D1) ------------------
    def _approval_action(self, room: dict, rec: dict, rid: str, body: dict) -> tuple:
        """``{action:"approval", approval_id, decision, note?, ttl?, ttl_min?}``.

        ``decision``: ``once`` · ``always`` · ``deny`` — разрешение; ``hint_agent`` ·
        ``trust_profile`` · ``dismiss`` — недоговорные действия (W26-D1): путь решения
        есть у каждой блокировки, тупиков не остаётся.
        """
        mod = self._approvals_mod()
        body = body if isinstance(body, dict) else {}
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise err(400, "Поле note должно быть строкой", field="note")
        if isinstance(note, str) and len(note) > MAX_NOTE:
            raise err(400, "Заметка слишком длинная", field="note", limit=MAX_NOTE)

        def act(action: str, **kw) -> dict:
            """Побочные шаги — штатными действиями движка (комментарий, перезапуск)."""
            with self.engine():
                return self.call("engine_write", "act",
                                 ((self.cfg, room.get("board") or self.board, rid, action),
                                  dict(kw))) or {}

        try:
            payload = mod.decide(self.cfg, room, rid, body.get("approval_id"),
                                 body.get("decision"), note=note or "",
                                 extra=(rec or {}).get("extra_cards"), act=act,
                                 ttl=body.get("ttl"), ttl_min=body.get("ttl_min"))
        except mod.ApprovalError as exc:
            raise ApiError(int(getattr(exc, "http", 400) or 400),
                           getattr(exc, "message", None) or str(exc) or
                           "Не удалось выполнить решение по разрешению",
                           code=str(getattr(exc, "code", "bad_request")),
                           detail=getattr(exc, "detail", None) or {}) from exc
        payload["action"] = "approval"
        payload.setdefault("decided_at", int(_time.time()))
        return 200, payload, {"Cache-Control": "no-store"}

    # --- список и отзыв (W22 §2.2) -----------------------------------------
    def h_approvals_grants(self, query, body):
        """``GET /api/approvals/grants?room=`` — что разрешено участникам комнаты."""
        mod = self._approvals_mod()
        room_id = _first(query, "room").strip()
        try:
            if room_id:
                payload = mod.grants(self.cfg, self._room(room_id))
            else:
                payload = mod.grants(self.cfg, rooms=self._rooms())
        except ApiError:
            raise
        except Exception as exc:            # noqa: BLE001 - список не должен давать 500
            self.log.error("разрешения: список: %s" % exc)
            raise err(503, "Список разрешений сейчас недоступен — попробуйте ещё раз",
                      reason="approvals_unavailable") from exc
        payload.setdefault("items", [])
        payload.setdefault("warnings", [])
        payload.setdefault("at", int(_time.time()))
        return 200, payload, {"Cache-Control": "no-store"}

    def h_approvals_revoke(self, query, body):
        """``POST /api/approvals/revoke {profile, pattern_key, room?}`` — отозвать запись."""
        mod = self._approvals_mod()
        body = body if isinstance(body, dict) else {}
        profile = body.get("profile")
        pattern_key = body.get("pattern_key")
        if not isinstance(profile, str) or not profile.strip():
            raise err(400, "Нужны поля: profile", missing=["profile"])
        if not isinstance(pattern_key, str) or not pattern_key.strip():
            raise err(400, "Нужны поля: pattern_key", missing=["pattern_key"])
        room = None
        room_id = body.get("room") or _first(query, "room")
        if isinstance(room_id, str) and room_id.strip():
            room = self._room(room_id.strip())
        try:
            payload = mod.revoke(self.cfg, profile.strip(), pattern_key.strip(), room=room)
        except mod.ApprovalError as exc:
            raise ApiError(int(getattr(exc, "http", 400) or 400),
                           getattr(exc, "message", None) or str(exc) or
                           "Не удалось отозвать разрешение",
                           code=str(getattr(exc, "code", "bad_request")),
                           detail=getattr(exc, "detail", None) or {}) from exc
        # после отзыва — свежий список (пользователь видит результат сразу)
        try:
            listing = (mod.grants(self.cfg, room) if room is not None
                       else mod.grants(self.cfg, rooms=self._rooms()))
            payload["items"] = listing.get("items") or []
        except Exception as exc:            # noqa: BLE001 - отзыв уже сделан
            self.log.warning("разрешения: список после отзыва: %s" % exc)
        return 200, payload, {"Cache-Control": "no-store"}

    # --- доверие профилю запуску кода (W26-D1, T3) --------------------------
    def h_approvals_trusts(self, query, body):
        """``GET /api/approvals/trusts?room=`` — кому доверен запуск кода и на сколько."""
        mod = self._approvals_mod()
        body = body if isinstance(body, dict) else {}
        room_id = _first(query, "room").strip() or str(body.get("room") or "").strip()
        if not room_id:
            raise err(400, "Нужен параметр room", missing=["room"])
        payload = mod.trusts(self.cfg, self._room(room_id))
        return 200, payload, {"Cache-Control": "no-store"}

    def h_approvals_untrust(self, query, body):
        """``POST /api/approvals/untrust {profile, room, approval_id?}`` — отозвать доверие."""
        mod = self._approvals_mod()
        body = body if isinstance(body, dict) else {}
        profile = body.get("profile")
        if not isinstance(profile, str) or not profile.strip():
            raise err(400, "Нужны поля: profile", missing=["profile"])
        room_id = body.get("room") or _first(query, "room")
        if not isinstance(room_id, str) or not room_id.strip():
            raise err(400, "Нужны поля: room", missing=["room"])
        room = self._room(room_id.strip())
        try:
            payload = mod.revoke_trust(self.cfg, profile.strip(), room=room,
                                       item_id=str(body.get("approval_id") or ""))
        except mod.ApprovalError as exc:
            raise ApiError(int(getattr(exc, "http", 400) or 400),
                           getattr(exc, "message", None) or str(exc) or
                           "Не удалось отозвать доверие",
                           code=str(getattr(exc, "code", "bad_request")),
                           detail=getattr(exc, "detail", None) or {}) from exc
        return 200, payload, {"Cache-Control": "no-store"}

    # --- уборка «ждёт вас»: подсказка участнику (способ A) ------------------
    def h_approvals_tidy(self, query, body):
        """``POST /api/approvals/tidy {room, request_id, delay?}`` — подсказать участнику.

        Способ A без человека: недоговорным блокировкам запроса пишется подсказка
        «сделай через файл и терминал» (``approvals.auto_hint``). ``delay`` — сколько
        секунд блокировка должна провисеть (интерфейс зовёт с 120). Разрешений
        не выдаёт, в чужие профили не пишет.
        """
        mod = self._approvals_mod()
        body = body if isinstance(body, dict) else {}
        room_id = str(body.get("room") or _first(query, "room") or "").strip()
        if not room_id:
            raise err(400, "Нужны поля: room", missing=["room"])
        request_id = str(body.get("request_id") or "").strip()
        if not request_id:
            raise err(400, "Нужны поля: request_id", missing=["request_id"])
        room = self._room(room_id)
        try:
            delay = int(body.get("delay") or 0)
        except (TypeError, ValueError):
            raise err(400, "Поле delay должно быть числом секунд", field="delay") from None

        def act(action: str, **kw) -> dict:
            with self.engine():
                return self.call("engine_write", "act",
                                 ((self.cfg, room.get("board") or self.board, request_id, action),
                                  dict(kw))) or {}

        try:
            payload = mod.auto_hint(self.cfg, room, request_id, act, delay=max(0, delay))
        except mod.ApprovalError as exc:
            raise ApiError(int(getattr(exc, "http", 400) or 400),
                           getattr(exc, "message", None) or str(exc) or
                           "Не удалось подсказать участнику",
                           code=str(getattr(exc, "code", "bad_request")),
                           detail=getattr(exc, "detail", None) or {}) from exc
        payload["request_id"] = request_id
        payload["message"] = ("Подсказано участникам: %d." % int(payload.get("hinted") or 0))
        return 200, payload, {"Cache-Control": "no-store"}


def install_approvals_routes(app_cls, routes: list) -> None:
    """Подключить разрешения к ``App``: методы миксина + строки таблицы маршрутов."""
    for name, value in vars(ApprovalRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
