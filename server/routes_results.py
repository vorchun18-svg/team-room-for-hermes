"""HTTP «Результаты» комнаты (WAVE29 §P6).

Подключение — одним блоком в ``server/app.py``::

    from server.routes_results import install_results_routes
    install_results_routes(App, ROUTES)

Маршруты:

* ``GET  /api/rooms/{id}/results`` — итоги завершённых запросов комнаты +
  интерактивные отчёты + «запросы без дела» (один ответ на две вкладки);
* ``POST /api/rooms/{id}/delos/from-request`` — «Сделать делом» из запроса
  (``request_id`` или ``request_ids: [...]`` для массового);
* ``POST /api/rooms/{id}/phases/from-requests`` — предложить этапы карты по
  выполненным запросам (эвристика, без запуска модели);
* ``POST /api/rooms/{id}/results/offer`` — решение по пункту «Предлагаю собрать
  карту проекта»: ``{action:"later"}`` (отложить) или ``{action:"build"}``.

Ничего наружу не отправляется, агенты не запускаются: чтение — поверх реестра,
доски и файлов комнаты; запись — только файл дела комнаты и поле предложения
этапов комнаты (штатным ``rooms.set_phases_state``).
"""
from __future__ import annotations

import re as _re

try:                                     # пакетный и «плоский» импорт
    from . import results as results_mod
    from .app_common import err
    from .http_util import ApiError
except ImportError:                      # pragma: no cover
    import results as results_mod        # type: ignore
    from app_common import err           # type: ignore
    from http_util import ApiError       # type: ignore

ROOM = r"(?P<room_id>[a-z][a-z0-9_-]{1,31})"

ROUTES = [
    ("GET", r"/api/rooms/" + ROOM + r"/results", "h_room_results"),
    ("POST", r"/api/rooms/" + ROOM + r"/delos/from-request", "h_room_delos_from_request"),
    ("POST", r"/api/rooms/" + ROOM + r"/phases/from-requests", "h_phases_from_requests"),
    ("POST", r"/api/rooms/" + ROOM + r"/results/offer", "h_results_offer"),
]

#: Сколько запросов разом можно превратить в дела (защита от «нажал — 200 дел»).
BULK_MAX = 30


class ResultsRoutes:
    """Обработчики результатов (``self`` — экземпляр ``server.app.App``)."""

    # --- общие мелочи -----------------------------------------------------
    def _results_board(self, room) -> str:
        return (room or {}).get("board") or getattr(self, "board", None)

    def _results_delos(self, room_id: str) -> list:
        sources = None
        if hasattr(self, "_delo_sources"):
            try:
                sources = self._delo_sources()
            except Exception:            # noqa: BLE001 — источники вторичны
                sources = None
        return results_mod.delo_items(self.cfg, room_id, sources=sources)

    # --- GET /api/rooms/{id}/results --------------------------------------
    def h_room_results(self, query, body, room_id):
        room = self._room(room_id)
        board = self._results_board(room)
        try:
            payload = results_mod.list_results(self.cfg, str(room.get("id")), board=board,
                                               delos=self._results_delos(str(room.get("id"))))
        except Exception as exc:         # noqa: BLE001 — чтение не должно ронять сервер
            self.log.error("результаты: чтение %s" % room_id, exc)
            raise err(500, "Не удалось прочитать результаты комнаты") from exc
        payload["room_title"] = str(room.get("title") or room.get("id") or "")
        return 200, payload, {"Cache-Control": "no-store"}

    # --- POST /api/rooms/{id}/delos/from-request --------------------------
    def h_room_delos_from_request(self, query, body, room_id):
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        single = str(data.get("request_id") or "").strip()
        many = data.get("request_ids")
        wanted: list = []
        if isinstance(many, list):
            wanted = [str(x).strip() for x in many if str(x or "").strip()]
        elif single:
            wanted = [single]
        if not wanted:
            raise err(400, "Нужен request_id или request_ids: [...]",
                      missing=["request_id"])
        if len(wanted) > BULK_MAX:
            raise err(400, "За раз можно сделать дела из %d задач" % BULK_MAX,
                      max=BULK_MAX, requested=len(wanted))
        delo_mod = self.mod("delo")
        sources = self._delo_sources() if hasattr(self, "_delo_sources") else None
        payload = results_mod.list_results(self.cfg, str(room.get("id")),
                                           board=self._results_board(room),
                                           delos=self._results_delos(str(room.get("id"))))
        by_id = {str(it.get("id")): it for it in
                 (payload.get("pending") or []) + (payload.get("items") or [])}
        done_states = ("done", "archived")

        created, skipped, failed = [], [], []
        for request_id in wanted:
            source = by_id.get(request_id)
            if source is None:
                failed.append({"request_id": request_id,
                               "message": "Такой задачи в комнате нет"})
                continue
            if source.get("delo"):
                skipped.append({"request_id": request_id,
                                "delo_id": (source.get("delo") or {}).get("id"),
                                "message": "Дело по этой задаче уже есть"})
                continue
            finished = str(source.get("status") or "") in done_states
            stages = [{"title": ("Запрос выполнен" if finished else "Задача в работе"),
                       "state": ("done" if finished else "now")},
                      {"title": "Итог и следующий шаг", "state": ("now" if finished else "todo")}]
            if source.get("short"):
                stages[0]["title"] = "Задача выполнена: " + str(source.get("short"))[:60]
            delo_payload = {"request_id": request_id,
                            "title": source.get("title") or source.get("title_short") or request_id,
                            "status": "waiting" if finished else "open",
                            "stages": stages}
            try:
                made = delo_mod.create(self.cfg, room, delo_payload, sources=sources)
            except delo_mod.DeloError as exc:
                code = "exists" if int(exc.http or 0) == 409 else "failed"
                (skipped if code == "exists" else failed).append({
                    "request_id": request_id, "message": exc.message,
                    **(exc.detail or {})})
                continue
            except Exception as exc:     # noqa: BLE001 — одно дело не роняет список
                self.log.error("дела из запроса %s" % request_id, exc)
                failed.append({"request_id": request_id,
                               "message": "Не удалось открыть дело: сервер не ответил"})
                continue
            created.append({"request_id": request_id, "delo": made})
        status = 201 if created and not failed else (200 if created else 409)
        return status, {"ok": bool(created), "created": created, "skipped": skipped,
                        "failed": failed,
                        "message": ("Открыто дел: %d" % len(created)) if created else
                                   (failed[0]["message"] if failed else "Дела уже есть")}, \
            {"Cache-Control": "no-store"}

    # --- POST /api/rooms/{id}/phases/from-requests ------------------------
    def h_phases_from_requests(self, query, body, room_id):
        room = self._room(room_id)
        if room.get("archived"):
            raise err(409, "Комната в архиве — этапы не меняются", room=room_id)
        payload = results_mod.list_results(self.cfg, str(room.get("id")),
                                           board=self._results_board(room),
                                           delos=self._results_delos(str(room.get("id"))))
        outcome = results_mod.phases_from_requests(self.cfg, room, payload.get("items") or [])
        if not outcome.get("ok"):
            raise err(409 if outcome.get("reason") in ("unchanged", "same_as_phases")
                      else 400, outcome.get("message") or "Этапы не собраны",
                      reason=outcome.get("reason"), room=room_id)
        proposal = outcome.get("proposal") or {}
        try:
            res = self.call("rooms", "set_phases_state", ((self.cfg, str(room.get("id"))),
                                                          {"proposal": proposal}))
        except Exception as exc:         # noqa: BLE001
            self.log.error("этапы из запросов %s" % room_id, exc)
            raise err(500, "Этапы собраны, но не сохранились в комнате — попробуйте ещё раз") from exc
        fresh = (res or {}).get("room") if isinstance(res, dict) else None
        fresh = fresh if isinstance(fresh, dict) and fresh.get("id") else self._room(room_id)
        return 200, {"ok": True, "message": outcome.get("message"),
                     "proposal": fresh.get("phases_proposal") or proposal,
                     "count": len((fresh.get("phases_proposal") or {}).get("items") or []),
                     "room": self._room_summary(fresh) if hasattr(self, "_room_summary") else None}, \
            {"Cache-Control": "no-store"}

    # --- POST /api/rooms/{id}/results/offer -------------------------------
    def h_results_offer(self, query, body, room_id):
        room = self._room(room_id)
        data = body if isinstance(body, dict) else {}
        action = str(data.get("action") or "").strip()
        if action not in ("later", "build", "dismiss"):
            raise err(400, "Действие: build (собрать карту) или later (не сейчас)",
                      field="action", value=action, allowed=["build", "later"])
        if action == "build":
            return self.h_phases_from_requests({}, {}, room_id)
        outcome = results_mod.dismiss_offer(self.cfg, str(room.get("id")))
        if not outcome.get("ok"):
            raise err(500, outcome.get("message") or "Не удалось отложить предложение")
        return 200, {"ok": True, "action": "later",
                     "message": "Предложение отложено — вернётся через две недели"}, \
            {"Cache-Control": "no-store"}


def install_results_routes(app_cls, routes: list) -> None:
    """Подключить результаты к ``App``: методы миксина + строки маршрутов."""
    for name, value in vars(ResultsRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, pattern.pattern) for method, pattern, _handler in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))
