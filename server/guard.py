"""Страж HTTP-запросов комнаты: идемпотентность POST /requests и строгая валидация.

Миксин стоит в MRO **перед** ``ReadRoutes``/``WriteRoutes`` (``server/app.py``):
он проверяет тело до того, как доменный обработчик начнёт писать, и возвращает
уже готовый ответ при повторной отправке. Доменные модули (``routes_write``)
при этом не меняются — страж только оборачивает их методы.

D7: ``critic`` — только настоящий ``bool`` (строка ``"false"`` → 400); поля комнаты
``board/icon/context_files/critic_pass/max_rounds`` и профили ``orchestrator/critic``
проверяются по ростера (``engine_write.list_profiles``), иначе 400 (создание) или
409 (PATCH) — как обещает docs/API.md §6.2.

D8: повтор POST /requests с тем же ``client_id`` (uuid с фронта) или с тем же
хэшем (комната + текст + вложения) в течение 60 с возвращает ТОТ ЖЕ запрос
с ``duplicate: true`` и кодом 200 — второй карточки и второй декомпозиции нет.
"""
from __future__ import annotations

import copy
import hashlib
import re
import threading
import time

try:  # пакетный и «плоский» импорт
    from .http_util import ApiError, err
except ImportError:  # pragma: no cover
    from http_util import ApiError, err  # type: ignore

DEDUP_TTL = 60.0                 # окно защиты от двойной отправки, секунды
MAX_CLIENT_ID = 64
MAX_ROUNDS = 5

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ICON_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class GuardRoutes:
    """Проверки тела запроса и дедупликация (``self`` — экземпляр ``App``)."""

    # --- POST /requests ----------------------------------------------------
    def h_request_create(self, query, body, room_id):
        self._check_request_body(body)
        key = self._dedup_key(room_id, body)
        state = self._dedup_state()
        now = time.monotonic()
        hit = None
        with state["lock"]:
            for stale in [k for k, v in state["items"].items() if now - v["at"] > DEDUP_TTL]:
                state["items"].pop(stale, None)
            hit = state["items"].get(key) if key else None
            if hit is not None:
                payload = copy.deepcopy(hit["payload"])
                payload["duplicate"] = True
                return 200, payload, dict(hit["headers"])
        status, payload, headers = super().h_request_create(query, body, room_id)
        status = int(status or 0)
        if key and 200 <= status < 300 and isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("duplicate", False)
            request = payload.get("request")
            warning = request.get("warning") if isinstance(request, dict) else None
            if warning and not payload.get("warning"):
                payload["warning"] = warning          # сводка не прочиталась, но карточка есть
            with state["lock"]:
                state["items"][key] = {"at": time.monotonic(), "payload": copy.deepcopy(payload),
                                       "headers": dict(headers or {})}
        return status, payload, headers

    def _dedup_state(self) -> dict:
        state = getattr(self, "_dedup", None)
        if state is None:
            state = {"lock": threading.Lock(), "items": {}}
            self._dedup = state
        return state

    def _dedup_key(self, room_id, body: dict) -> str:
        """Ключ идемпотентности: client_id с фронта либо хэш (комната, текст, вложения)."""
        if not isinstance(body, dict):
            return ""
        client_id = body.get("client_id")
        if client_id is not None:
            if not isinstance(client_id, str) or not _CLIENT_ID_RE.match(client_id.strip()):
                raise err(400, "client_id: строка до %d символов (латиница, цифры, . _ : -)"
                          % MAX_CLIENT_ID, field="client_id", value=str(client_id)[:80])
            return "c:%s:%s" % (room_id, client_id.strip())
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return ""                      # пустой текст отклонит доменный обработчик
        attach = body.get("attach") or []
        parts = sorted(str(p) for p in attach) if isinstance(attach, list) else []
        digest = hashlib.sha256(("%s\x1f%s" % (text, "|".join(parts))).encode("utf-8")).hexdigest()
        return "h:%s:%s" % (room_id, digest)

    def _check_request_body(self, body: dict) -> None:
        if not isinstance(body, dict):
            return
        critic = body.get("critic")
        if critic is not None and not isinstance(critic, bool):
            raise err(400, "critic: ожидается true или false", field="critic",
                      got=type(critic).__name__)

    # --- комнаты -----------------------------------------------------------
    def h_room_create(self, query, body):
        self._check_room_body(body, creating=True)
        return super().h_room_create(query, body)

    def h_room_patch(self, query, body, room_id):
        self._check_room_body(body, creating=False, room_id=room_id)
        return super().h_room_patch(query, body, room_id)

    def _check_room_body(self, body: dict, creating: bool = True, room_id=None) -> None:
        if not isinstance(body, dict):
            return
        if "board" in body:
            value = body.get("board")
            if not isinstance(value, str) or not _BOARD_RE.match(value.strip()):
                raise err(400, "board: слаг доски (латиница, цифры, - и _)", field="board",
                          value=value)
        if "icon" in body:
            value = body.get("icon")
            if value is not None and (not isinstance(value, str) or not _ICON_RE.match(value.strip())):
                raise err(400, "icon: имя иконки (латиница, цифры, - и _)", field="icon", value=value)
        if "context_files" in body:
            value = body.get("context_files")
            if not isinstance(value, list) or any(
                    not isinstance(item, str) or not item.strip() for item in value):
                raise err(400, "context_files: список непустых путей", field="context_files")
        if "critic_pass" in body and not isinstance(body.get("critic_pass"), bool):
            raise err(400, "critic_pass: ожидается true или false", field="critic_pass",
                      got=type(body.get("critic_pass")).__name__)
        if "max_rounds" in body:
            value = body.get("max_rounds")
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ROUNDS:
                raise err(400, "max_rounds: целое от 1 до %d" % MAX_ROUNDS, field="max_rounds",
                          value=value)
        self._check_profiles(body, creating, room_id)

    def _check_profiles(self, body: dict, creating: bool, room_id) -> None:
        fields = [field for field in ("orchestrator", "critic") if field in body]
        if not fields:
            return
        roster = self._roster_names()
        for field in fields:
            value = body.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                if field == "orchestrator":
                    raise err(400, "orchestrator обязателен", field=field)
                continue
            if not isinstance(value, str):
                raise err(400, "%s: имя профиля строкой" % field, field=field,
                          got=type(value).__name__)
            profile = value.strip()
            if roster and profile not in roster:
                # создание — 400 (неизвестный профиль), PATCH — 409 (не из ростера), API §6.2
                status = 400 if creating else 409
                raise err(status, "Профиль «%s» не найден в ростере Hermes" % profile,
                          field=field, profile=profile, available=sorted(roster))
        orchestrator = body.get("orchestrator")
        critic = body.get("critic")
        if isinstance(orchestrator, str) and isinstance(critic, str) \
                and orchestrator.strip() and orchestrator.strip() == critic.strip():
            raise err(400, "critic не может совпадать с orchestrator", field="critic",
                      value=critic.strip())
