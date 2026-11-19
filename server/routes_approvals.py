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
    from . import stale as stale_mod
    from .app_common import err
    from .http_util import ApiError
except ImportError:  # pragma: no cover
    import stale as stale_mod     # type: ignore
    from app_common import err  # type: ignore
    from http_util import ApiError  # type: ignore

ROUTES = [
    ("GET", r"/api/approvals/grants", "h_approvals_grants"),
    ("POST", r"/api/approvals/revoke", "h_approvals_revoke"),
    ("GET", r"/api/approvals/trusts", "h_approvals_trusts"),
    ("POST", r"/api/approvals/untrust", "h_approvals_untrust"),
    ("POST", r"/api/approvals/tidy", "h_approvals_tidy"),
    # W27-E1 §1: массовое «Закрыть все неактуальные» — только у завершённых запросов.
    ("POST", r"/api/rooms/(?P<room>[^/]+)/dismiss_stale", "h_dismiss_stale"),
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
        if not isinstance(detail, dict):
            return
        root_id = str(detail.get("id") or (rec or {}).get("root_id") or "")
        if self._has_mod("approvals") and root_id:
            try:
                mod = self.mod("approvals")
                items = mod.collect(self.cfg, room, root_id, (rec or {}).get("extra_cards"))
            except Exception as exc:        # noqa: BLE001 - страница важнее разрешений
                self.log.warning("разрешения %s: %s" % (root_id, exc))
                detail.setdefault("approvals", [])
                detail.setdefault("approvals_pending", 0)
            else:
                detail["approvals"] = items
                waiting = mod.pending(items)        # W26-D1: только «можно разрешить» + действие
                detail["approvals_pending"] = len(waiting)
                detail["approvals_actionable"] = len(mod.actionable(items))
                if waiting:
                    detail["needs_you"] = True
        # W27-E1 §1: состояния всех пунктов и серверный счётчик — ОДИН проход в конце,
        # после вопросов, решений, шагов и разрешений (иначе счётчик считался бы по части).
        self._stale_view(detail, rec)

    def _stale_view(self, detail: dict, rec: dict | None = None) -> None:
        """Единые состояния пунктов и `needs_you_count` (W27-E1 §1).

        Ничего не пишет: завершённый запрос закрывает свои пункты на чтении
        (``closed_reason: request_done``), поэтому «залипнуть» в счётчике они не
        могут. Сбой разметки не роняет страницу — состояния вторичны.
        """
        if not isinstance(detail, dict):
            return
        try:
            stale_mod.decorate(detail)
        except Exception as exc:            # noqa: BLE001 - страница важнее разметки
            self.log.warning("состояния пунктов %s: %s" % (detail.get("id"), exc))

    # --- массовое «Закрыть все неактуальные» (W27-E1 §1) ---------------------
    def h_dismiss_stale(self, query, body, room):
        """``POST /api/rooms/{room}/dismiss_stale {scope:"finished_requests"}``.

        Закрывает пункты завершённых запросов комнаты (``done``/``archived``):
        вопрос, решение, разрешение и шаг получают отметку «неактуально» со своей
        причиной. Решённое человеком (ответ, принятое решение, выполненный шаг) не
        трогается: такие пункты и так не ждут. Ответ — сколько закрыто по видам.

        Повторный вызов безопасен: уже закрытое возвращает 409 `already_decided`,
        это не ошибка и в счётчики не попадает.
        """
        body = body if isinstance(body, dict) else {}
        scope = str(body.get("scope") or "finished_requests").strip()
        if scope not in stale_mod.STALE_SCOPES:
            raise err(400, "scope: finished_requests", field="scope",
                      allowed=list(stale_mod.STALE_SCOPES), value=scope[:40])
        # W27-QA2 (§1): предпросмотр — сколько неактуальных пунктов закроется, БЕЗ записи.
        # Нужен кнопке «Закрыть все неактуальные (N)» на «Сегодня»: число берётся у сервера,
        # клиент его не досчитывает по списку.
        preview = bool(body.get("preview"))
        room_dict = self._room(str(room))
        room_id = str(room_dict.get("id") or room)
        closed = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0}
        already = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0}
        errors: list = []
        touched: list = []
        for rec in self._records(room_id, quiet=True) or []:
            rid = str((rec or {}).get("root_id") or "")
            if not rid:
                continue
            try:
                detail = self._detail(room_dict, rid, (rec or {}).get("extra_cards"),
                                      (rec or {}).get("critic"), (rec or {}).get("answers"))
            except ApiError as exc:
                errors.append({"request": rid, "why": str(exc.message)[:160]})
                continue
            if not (stale_mod.request_finished((detail or {}).get("status"))
                    or (detail or {}).get("archived")):
                continue
            # Разметка БЕЗ автозакрытия: так видны пункты, которые ещё лежат в данных
            # открытыми, — их и закрываем. Иначе закрывать было бы нечего.
            stale_mod.decorate(detail, request_status="")
            targets = stale_mod.open_targets(detail)
            if not targets:
                continue
            per = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0}
            for target in targets:
                key = {"question": "questions", "decision": "decisions",
                       "approval": "approvals", "step": "steps"}[target["kind"]]
                if preview:
                    # Уже снятый человеком шаг (`steps_dropped`) запись отвергнет
                    # (`409 already_decided`) — в предпросмотре его тоже не считаем,
                    # иначе кнопка обещает больше, чем закроет.
                    if target["kind"] == "step":
                        dropped = {str(x) for x in ((rec or {}).get("steps_dropped") or [])}
                        bare = str(target["id"]).split(":", 1)[-1]
                        if bare in dropped:
                            continue
                    per[key] += 1
                    continue
                try:
                    status, _payload, _headers = self._stale_action(
                        room_dict, rec or {}, rid,
                        {"action": "dismiss", "reason": "запрос завершён",
                         "target": {"kind": target["kind"], "id": target["id"]}}, "dismiss")
                except ApiError as exc:
                    if exc.status == 409:
                        already[key] += 1        # уже не ждёт — не ошибка
                    else:
                        errors.append({"request": rid, "kind": target["kind"],
                                       "id": target["id"], "why": str(exc.message)[:160]})
                    continue
                if status == 200:
                    per[key] += 1
                    if target["kind"] in ("question", "decision"):
                        # реестр ответов только что переписан — перечитываем запись,
                        # иначе следующее снятие перезапишет предыдущее своим снимком
                        rec = self._reload_record(room_id, rid) or rec
            for key in per:
                closed[key] += per[key]
            if per["questions"] or per["decisions"] or per["approvals"] or per["steps"]:
                touched.append({"request": rid, "closed": dict(per)})
        total = sum(closed.values())
        payload = {"ok": True, "scope": scope, "room": room_id, "closed": closed,
                   "already_closed": already, "requests": touched, "errors": errors,
                   "total": total, "at": int(_time.time()),
                   "message": (("Неактуальных пунктов у завершённых запросов: %d." % total)
                               if preview else
                               ("Закрыто как неактуальное: %d." % total) if total
                               else "Неактуальных пунктов у завершённых запросов нет.")}
        if preview:
            payload["preview"] = True
            payload.pop("requests", None)
            payload.pop("already_closed", None)
        return 200, payload, {"Cache-Control": "no-store"}

    def _reload_record(self, room_id: str, rid: str) -> dict | None:
        """Свежая запись реестра по id запроса (после записи в реестр ответов)."""
        try:
            for row in self._records(room_id, quiet=True) or []:
                if str((row or {}).get("root_id") or "") == str(rid):
                    return row
        except Exception:                   # noqa: BLE001 - уборка вторична
            return None
        return None

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
