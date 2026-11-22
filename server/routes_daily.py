"""HTTP-слой «Итог дня» (W29B-Q8): ``GET /api/daily`` и ``POST /api/daily/shorten``.

Здесь только источники и маршруты; правила «что считать сделанным за сутки» живут
в ``server/daily.py`` и проверяются без доски и сервера.

Источники — существующее чтение (ничего нового не заводим):

* ``App._rooms`` / ``App._records`` — комнаты и запросы;
* ``engine_read.today_states`` — состояния корней (через ``LiveSources`` экрана
  «Сегодня»: тот же расчёт, те же кэши одного запроса);
* ``routes_today.waiting_items`` — пункты «ждёт вас» с действием (число = число
  пунктов, правило «без тупиков»);
* ``decision_registry.rows`` — реестр решений комнаты;
* ``archive.archive_info`` — что ушло в архив (по записи реестра);
* ``limits.panel`` — расход Hermes за сутки (``state.db``, только чтение);
* каталог отчётов комнаты — файлы, появившиеся за сутки (имя и время, без путей).

«Короче» — один вызов дешёвой модели (``hermes -z``), стоимость оценивается до
запуска по медиане ``state.db``; в песочнице ``TEAMROOM_DAILY_FAKE``.
"""
from __future__ import annotations

import re as _re
import time
from pathlib import Path

try:                                    # пакетный и «плоский» импорт
    from . import daily as daily_mod
    from .app_common import err
    from .routes_today import LiveSources, waiting_items
except ImportError:                     # pragma: no cover
    import daily as daily_mod           # type: ignore
    from app_common import err          # type: ignore
    from routes_today import LiveSources, waiting_items  # type: ignore

ROUTES = [
    ("GET", r"/api/daily", "h_daily"),
    ("POST", r"/api/daily/shorten", "h_daily_shorten"),
]

#: Потолок файлов отчётов комнаты, которые смотрим на «появилось за сутки».
FILES_MAX = 300


class DailySources(LiveSources):
    """Настоящие источники итога дня: «Сегодня» + решения, архив, файлы, расход."""

    # --- решения -----------------------------------------------------------
    def decisions(self, room: dict) -> list[dict]:
        room_id = self._room_id(room)

        def build() -> list[dict]:
            try:
                mod = self.app.mod("decision_registry")
                rows = mod.rows(self.cfg, room_id)
            except Exception:           # noqa: BLE001 — реестра может не быть
                return []
            out = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                out.append({"id": str(row.get("id") or ""),
                            "at": int(row.get("at") or 0),
                            "text": str(row.get("text") or "")[:400],
                            "status": str(row.get("status") or ""),
                            "source_request": str(row.get("source_request") or "")})
            return out

        return self._once(("decisions", room_id), build)

    # --- файлы -------------------------------------------------------------
    def files(self, room: dict) -> list[dict]:
        """Файлы отчётов комнаты: имя и время изменения (пути наружу не отдаём)."""
        room_id = self._room_id(room)

        def build() -> list[dict]:
            try:
                base = Path(self.cfg.reports_dir) / room_id
            except Exception:           # noqa: BLE001
                return []
            if not base.is_dir():
                return []
            out: list[dict] = []
            try:
                paths = sorted(base.rglob("*"))
            except OSError:
                return []
            for path in paths[:FILES_MAX]:
                try:
                    if not path.is_file():
                        continue
                    at = int(path.stat().st_mtime)
                except OSError:
                    continue
                out.append({"name": path.name, "at": at, "request_id": ""})
            return out

        return self._once(("files", room_id), build)

    # --- архив -------------------------------------------------------------
    def archived(self, room: dict) -> list[dict]:
        room_id = self._room_id(room)

        def build() -> list[dict]:
            try:
                mod = self.app.mod("archive")
            except Exception:           # noqa: BLE001
                return []
            out: list[dict] = []
            for rec in self.requests(room):
                if not isinstance(rec, dict):
                    continue
                try:
                    info = mod.archive_info(rec)
                except Exception:       # noqa: BLE001 — старая запись без полей архива
                    continue
                if not isinstance(info, dict) or not info.get("archived_at"):
                    continue                # запрос не в архиве (или вернули из архива)
                out.append({"root_id": str(rec.get("root_id") or ""),
                            "title": str(rec.get("title") or ""),
                            "at": int(info.get("archived_at") or 0)})
            return out

        return self._once(("archived", room_id), build)

    # --- «ждёт вас» в форме итога -----------------------------------------
    def waiting(self, room: dict) -> list[dict]:
        """Пункты «ждёт вас» с готовым действием — тем же расчётом, что «Сегодня»."""
        room_id = self._room_id(room)

        def build() -> list[dict]:
            out: list[dict] = []
            for rec in self.requests(room):
                if not isinstance(rec, dict) or not rec.get("root_id"):
                    continue
                try:
                    items = waiting_items(self.app, room, rec)
                except Exception:       # noqa: BLE001 — один запрос не валит итог
                    continue
                root_id = str(rec.get("root_id"))
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    out.append({"kind": str(item.get("kind") or ""),
                                "text": str(item.get("text") or ""),
                                "request_id": root_id,
                                "since": int(item.get("since") or 0),
                                "recommended": str(item.get("recommended") or "")})
            return out

        return self._once(("waiting_daily", room_id), build)

    # --- расход ------------------------------------------------------------
    def usage(self) -> dict:
        """Расход за сутки из ``limits.panel`` (окно «day»): вызовы, токены, стоимость."""
        def build() -> dict:
            try:
                mod = self.app.mod("limits")
                panel = mod.panel(self.cfg, getattr(self.cfg, "hermes_home", None))
            except Exception:           # noqa: BLE001 — расход вторичен для итога
                return {"calls": 0, "tokens": 0, "cost_usd": 0.0, "source": "none",
                        "note": "Расход недоступен: state.db не прочитался."}
            window = (panel.get("windows") or {}).get("day") or {}
            tokens = window.get("tokens") or {}
            total = int(tokens.get("total") or 0)
            warnings = panel.get("warnings") or []
            return {"calls": int(window.get("calls") or 0), "tokens": total,
                    "cost_usd": float(window.get("cost_usd") or 0.0),
                    "source": str(panel.get("source") or "none"),
                    "note": str(warnings[0]) if warnings else ""}

        return self._once(("usage", ""), build)


class DailyRoutes:
    """Обработчики «Итога дня» (``self`` — экземпляр ``server.app.App``)."""

    def h_daily(self, query, body):
        """``GET /api/daily?room=<id>`` — счёт сделанного, ожиданий и расхода за сутки."""
        mod = self.mod("daily")
        params = query or {}
        room = str(_first(params, "room") or "").strip()
        if room and not any(str((r or {}).get("id") or "") == room for r in self._rooms()):
            raise err(404, "Комната не найдена", code="room_not_found")
        with self.engine():
            payload = mod.build(DailySources(self), room=room or None)
        if room:
            payload["short"] = mod.last_short(self.cfg, room)
            # Цена «Короче» — на кнопке ДО нажатия: тот же расчёт, что и в
            # обработчике, поэтому человек видит одно число, а не два разных.
            model, _provider = _routine_model(
                next((r for r in self._rooms() if str((r or {}).get("id") or "") == room), {}))
            payload["estimate"] = mod.estimate(
                payload, mod.price_per_token(getattr(self.cfg, "hermes_home", None), model))
        return 200, payload, {"Cache-Control": "no-store"}

    def h_daily_shorten(self, query, body):
        """``POST /api/daily/shorten {room}`` — сжать итог дешёвой моделью (по кнопке)."""
        mod = self.mod("daily")
        data = body if isinstance(body, dict) else {}
        room_id = str(data.get("room") or _first(query or {}, "room") or "").strip()
        if not room_id:
            raise err(400, "Укажите комнату: «Короче» считается по одной комнате.",
                      code="room_required")
        room = next((r for r in self._rooms() if str((r or {}).get("id") or "") == room_id), None)
        if not room:
            raise err(404, "Комната не найдена", code="room_not_found")
        with self.engine():
            payload = mod.build(DailySources(self), room=room_id)
        if not payload.get("counters", {}).get("all"):
            raise err(409, "За сутки в комнате ничего не произошло — сжимать нечего.",
                      code="nothing_to_shorten")
        model, provider = _routine_model(room)
        price = mod.price_per_token(getattr(self.cfg, "hermes_home", None), model)
        est = mod.estimate(payload, price)
        est["model"] = model or None
        result = mod.shorten(self.cfg, payload, room_id, model=model, provider=provider,
                             state_dir=getattr(self.cfg, "hermes_home", None))
        result["estimate"] = est
        if not result.get("ok"):
            raise err(503, result.get("error") or "«Короче» не получился",
                      code="shorten_failed", detail={"estimate": est})
        return 200, result, {"Cache-Control": "no-store"}


def _routine_model(room: dict) -> tuple:
    """Модель для «Короче»: «модель рутины» комнаты, иначе руководитель комнаты.

    Возвращает ``(model, provider)``. Пустая строка — модель по умолчанию профиля
    (Hermes сам выберет), это честнее выдуманного имени.
    """
    room = room if isinstance(room, dict) else {}
    for key in ("routine_model", "svod_model"):
        value = str(room.get(key) or "").strip()
        if value:
            return value, str(room.get("routine_provider") or room.get("svod_provider") or "")
    leader = str(room.get("leader") or room.get("leader_profile") or "")
    for member in room.get("members") or []:
        if not isinstance(member, dict):
            continue
        if leader and str(member.get("profile") or "") != leader:
            continue
        model = str(member.get("model") or "").strip()
        if model:
            return model, str(member.get("provider") or "")
    return "", ""


def _first(params: dict, key: str):
    """Первое значение параметра запроса (``dict`` из http_util)."""
    value = (params or {}).get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def install_daily_routes(app_cls, routes: list) -> None:
    """Подключить «Итог дня» к ``App``: методы + строки таблицы маршрутов."""
    for name, value in vars(DailyRoutes).items():
        if name.startswith("__") or not callable(value):
            continue
        setattr(app_cls, name, value)
    existing = {(method, getattr(pattern, "pattern", pattern)) for method, pattern, _h in routes}
    for method, pattern, handler in ROUTES:
        if (method, pattern) in existing:
            continue
        routes.append((method, _re.compile(pattern), handler))


__all__ = ["ROUTES", "DailySources", "DailyRoutes", "install_daily_routes"]
