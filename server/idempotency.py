# -*- coding: utf-8 -*-
"""Повторные нажатия не создают дублей (B-25).

Кнопка «Повторить» отправляет вместе с действием ``idempotency_key`` — случайный ключ
одного нажатия. Если тот же ключ пришёл второй раз (двойной клик, повторная доставка,
два окна), сервер не запускает действие заново, а отдаёт сохранённый ответ с
``duplicate: true``.

Почему в памяти, а не в файле: окно, в котором имеет смысл дедупликация, — секунды
(пока кнопка заблокирована). Сервер один; после перезапуска карточка уже в очереди, и
повторное нажатие безопасно само по себе (`retry` на `ready`-карточке отвечает
«уже в очереди» и ничего не запускает). Ключи старше TTL выбрасываются.

Модуль без зависимостей от приложения: его можно проверить отдельно.

    guard = Idempotency(ttl=120)
    hit = guard.replay("t_x:t_1:retry-1-abc", scope="retry")
    if hit is None: ... выполнить ...; guard.remember(key, payload)

Использование в ``routes_write.h_request_actions`` — только для ``retry``.
"""
from __future__ import annotations

import re as _re
import threading
import time
from typing import Any

#: Сколько секунд помним ответ (кнопка держит блокировку 90 с — берём с запасом).
DEFAULT_TTL_S = 120.0

#: Сколько ключей максимум держим (защита от роста памяти).
MAX_ITEMS = 512

#: Допустимый ключ: буквы, цифры и «.-_:», 6…120 знаков.
_KEY_RE = _re.compile(r"^[A-Za-z0-9._:\-]{6,120}$")


class IdempotencyError(Exception):
    """Ключ идемпотентности не годится (ошибка клиента, а не сервера)."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


def valid_key(value: Any) -> bool:
    """Ключ идемпотентности пригоден? (str, 6…120 знаков, без пробелов и NUL)."""
    if not isinstance(value, str):
        return False
    return bool(_KEY_RE.match(value))


def ensure_key(value: Any) -> str:
    """Проверить ключ или понятно отказать (400)."""
    if not valid_key(value):
        raise IdempotencyError(
            "idempotency_key: 6–120 знаков, буквы, цифры и «.-_:»",
            detail={"field": "idempotency_key", "value": str(value)[:120]})
    return str(value)


class Idempotency:
    """Короткая память «этот запрос уже выполнялся» (потокобезопасная)."""

    def __init__(self, ttl: float = DEFAULT_TTL_S, clock=None, max_items: int = MAX_ITEMS):
        self.ttl = float(ttl)
        self.max_items = int(max_items)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    # --- внутреннее ------------------------------------------------------
    def _evict(self, now: float) -> None:
        dead = [key for key, item in self._items.items() if now - item["at"] > self.ttl]
        for key in dead:
            del self._items[key]
        if len(self._items) <= self.max_items:
            return
        # Переполнение: выбрасываем самые старые.
        ordered = sorted(self._items.items(), key=lambda kv: kv[1]["at"])
        for key, _item in ordered[:len(self._items) - self.max_items]:
            del self._items[key]

    def _slot(self, scope: str, key: str) -> str:
        return "%s|%s" % (str(scope or "action"), str(key))

    # --- API -------------------------------------------------------------
    def replay(self, key: str, scope: str = "action") -> dict | None:
        """Сохранённый ответ для того же ключа в пределах TTL (иначе None)."""
        now = float(self._clock())
        slot = self._slot(scope, key)
        with self._lock:
            self._evict(now)
            item = self._items.get(slot)
            if not item:
                return None
            if now - item["at"] > self.ttl:
                del self._items[slot]
                return None
            payload = item.get("payload")
            return dict(payload) if isinstance(payload, dict) else None

    def remember(self, key: str, payload: dict | None, scope: str = "action",
                 status: int = 200) -> None:
        """Запомнить ответ действия, чтобы повтор вернул его же."""
        now = float(self._clock())
        slot = self._slot(scope, key)
        with self._lock:
            self._evict(now)
            self._items[slot] = {"at": now, "status": int(status),
                                 "payload": dict(payload) if isinstance(payload, dict) else payload}
            self._evict(now)          # предел памяти держим и после вставки

    def forget(self, key: str, scope: str = "action") -> None:
        """Освободить ключ (действие не выполнено — пусть повтор попробует снова)."""
        with self._lock:
            self._items.pop(self._slot(scope, key), None)

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


#: Общая память процесса: обработчики берут её, тесты могут очистить.
GUARD = Idempotency()


def reset() -> None:
    """Очистить общую память (тесты, диагностика)."""
    GUARD.clear()


__all__ = ["DEFAULT_TTL_S", "Idempotency", "IdempotencyError", "GUARD", "reset",
           "valid_key", "ensure_key"]
