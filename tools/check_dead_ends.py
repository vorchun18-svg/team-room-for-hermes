#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Сторожевой обход «ждёт вас»: тупиков быть не должно (W26-D1, правило Михаила).

Зачем
-----
Михаил сформулировал правило (Requirements-Mikhail.md): всё, что приложение показывает
как «ждёт вашего решения», обязано иметь действие, которое реально что-то меняет.
Надпись «ждёт вашего решения» без единой кнопки — это тупик, а не вопрос. Второй
тупик — блокировка, которая висит `pending` у уже завершённой карточки: ждать её
некому.

Что проверяет
-------------
1. **у каждого пункта есть действие**: `approvals[]` со состоянием `pending` /
   `workaround` / `info` обязаны нести непустой список `actions[]` (кнопка, которая
   меняет состояние); пункты других видов с явным `actions: []` — тоже тупик;
2. **нет `pending` у завершённых карточек**: `card_state` в
   `done/archived/failed/cancelled` не может быть «ждёт решения»;
3. **счётчики честные**: `approvals_pending` не считает строки без действия,
   недоговорные блокировки и жёсткие запреты;
4. **надпись «ждёт вашего решения»** не стоит у строки без кнопок.

Режимы
------
    python tools/check_dead_ends.py --base http://127.0.0.1:8851
    python tools/check_dead_ends.py --base http://127.0.0.1:8851 --json out.json --quiet
    python tools/check_dead_ends.py --items dead_ends.json     # офлайн: массив approvals
    python tools/check_dead_ends.py --selftest                 # проверка самой проверки

Код возврата: 0 — тупиков нет, 1 — есть, 2 — не смог прочитать сервер/файл.

Публично: ``check_approval``, ``check_items``, ``check_detail``, ``walk``,
``selftest``, ``main``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

#: Статусы карточки, в которых ждать блокировку некому (как в ``server/approvals.py``).
CLOSED_CARDS = ("done", "archived", "failed", "cancelled")
#: Состояния строки разрешения, которые ещё не решены человеком.
UNDECIDED = ("pending", "workaround", "info")
#: Сколько запросов обходим по умолчанию (страховка от огромной комнаты).
MAX_REQUESTS = 120
#: Строки, которые обязаны иметь действие.
WAITING_TEXT = "ждёт вашего решения"


#: Состояния пункта по контракту W27 §1 (вопрос / решение / шаг).
ITEM_STATES = ("open", "answered", "accepted", "rejected", "dismissed", "done")
#: Вопрос дополнительно знает `postponed` — «Отложить» (W30-A §1): человек увидел
#: вопрос и просит вернуться позже. Это НЕ ответ и НЕ снятие: пункт не входит в
#: счётчики сейчас, но остаётся открытым и обязан иметь кнопку «Вернуть»
#: (`restore`) — иначе «Отложить» превращается в чёрную дыру.
QUESTION_STATES = ITEM_STATES + ("postponed",)
#: Состояния разрешения (§1). ``workaround``/``hint_agent`` — старые сборки W26.
APPROVAL_STATES = ("pending", "granted_once", "granted_always", "denied", "expired",
                   "closed", "dismissed", "workaround", "info", "hinted", "trusted")
#: Шаг дополнительно знает ``cancelled`` («снят по вашему ответу»).
STEP_STATES = ITEM_STATES + ("cancelled",)
#: Разделы запроса с пунктами «ждёт вас»: (ключ, вид, допустимые состояния).
ITEM_SECTIONS = (("open_questions", "question", QUESTION_STATES), ("questions", "question", QUESTION_STATES),
                 ("decisions", "decision", ITEM_STATES), ("next_steps", "step", STEP_STATES))
#: Ключи серверного счётчика запроса (§1).
COUNTER_KEYS = ("questions", "decisions", "approvals", "steps", "total")


def _actions(item: dict) -> list:
    """Кнопки пункта: ``actions[]`` / ``action`` / ``options[]`` — что найдётся."""
    for key in ("actions", "action", "buttons", "options"):
        value = item.get(key)
        if isinstance(value, list) and [v for v in value if v]:
            return value
        if isinstance(value, dict) and value:
            return [value]
        if isinstance(value, str) and value.strip():
            return [value]
    return []


def _action_ids(item: dict) -> list:
    """Имена кнопок пункта (``actions[]``/``options[]``) — то, что нажимает человек."""
    return [str((a or {}).get("id") or "") if isinstance(a, dict) else str(a or "")
            for a in _actions(item or {})]


def check_approval(item: dict) -> list[str]:
    """Проблемы одной строки ``approvals[]`` (пустой список — тупика нет)."""
    if not isinstance(item, dict):
        return []
    state = str(item.get("state") or "")
    mode = str(item.get("mode") or "")
    card = str(item.get("card_state") or "")
    actions = _actions(item)
    out: list[str] = []
    if not mode:
        # старая сборка без `mode`: считаем по `grantable`/`floor`
        mode = "floor" if item.get("floor") else ("grant" if item.get("grantable") else "workaround")
    if state == "pending" and not actions:
        out.append("тупик: состояние «ждёт вашего решения», а действий нет")
    if state == "pending" and card in CLOSED_CARDS:
        out.append("pending у завершённой карточки «%s»: ждать блокировку некому" % (card,))
    if mode == "floor" and state == "pending":
        out.append("жёсткий запрет Hermes показан как «ждёт вашего решения»")
    if mode == "workaround" and state == "pending":
        out.append("недоговорная блокировка показана как «ждёт вашего решения»")
    if state in UNDECIDED and mode in ("workaround", "floor") and not actions:
        out.append("нет действия: «%s» без единой кнопки" % (mode,))
    note = str(item.get("note") or "")
    if WAITING_TEXT in note and not actions:
        out.append("надпись «%s» у строки без кнопок" % WAITING_TEXT)
    return out


def check_items(items, request_id: str = "", card_state: str = "") -> list[dict]:
    """Проблемы массива ``approvals[]`` (офлайн-режим и тесты)."""
    problems: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for text in check_approval(item):
            problems.append({"request_id": str(request_id or item.get("root_id") or ""),
                             "kind": "approval", "id": str(item.get("id") or ""),
                             "card_state": str(item.get("card_state") or card_state or ""),
                             "why": text,
                             "text": str(item.get("description") or item.get("command") or "")[:160]})
    return problems


def _generic(kind: str, items, request_id: str) -> list[dict]:
    """Пункты других видов: если список кнопок задан явно и пуст — это тупик."""
    problems: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "actions" not in item:
            continue                      # кнопки рисует интерфейс по типу пункта
        if _actions(item):
            continue
        problems.append({"request_id": request_id, "kind": kind,
                         "id": str(item.get("id") or ""), "card_state": "",
                         "why": "пункт ждёт человека, а действий у него нет",
                         "text": str(item.get("text") or "")[:160]})
    return problems


def check_detail(detail: dict, request_id: str = "") -> list[dict]:
    """Проблемы одного запроса: ``approvals[]``, счётчики, вопросы/решения/шаги."""
    if not isinstance(detail, dict):
        return []
    rid = str(request_id or detail.get("id") or "")
    problems = check_items(detail.get("approvals") or [], rid)
    for kind, key in (("question", "questions"), ("question", "open_questions"),
                      ("decision", "decisions"), ("step", "next_steps")):
        problems += _generic(kind, detail.get(key) or [], rid)
    items = [item for item in (detail.get("approvals") or []) if isinstance(item, dict)]
    waiting = [item for item in items
               if item.get("state") == "pending" and _actions(item)]
    counted = detail.get("approvals_pending")
    if isinstance(counted, int) and counted > len(waiting):
        problems.append({"request_id": rid, "kind": "counter", "id": "",
                         "card_state": "", "why": "счётчик «%d» больше числа строк с действием "
                                                  "(%d) — считает недоговорное" % (counted, len(waiting)),
                         "text": ""})
    if detail.get("needs_you") and not waiting and not _generic_any(detail):
        problems.append({"request_id": rid, "kind": "counter", "id": "", "card_state": "",
                         "why": "карточка «нужен ваш ответ», а решать нечего", "text": ""})
    problems += _state_problems(detail, rid)
    problems += _counter_problems(detail, rid)
    # W29-P2: заблокированная карточка — такой же пункт «ждёт вас»: кнопка обязана быть.
    problems += check_blocked_cards(detail, rid)
    return problems


def _state_problems(detail: dict, rid: str) -> list[dict]:
    """Пункт с состоянием не по словарю §1 — интерфейс нарисует его как «ждёт»."""
    problems: list[dict] = []
    seen: set = set()
    for key, kind, allowed in ITEM_SECTIONS:
        for item in detail.get(key) or []:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "")
            if ident and (kind, ident) in seen:
                continue
            if ident:
                seen.add((kind, ident))
            state = item.get("state")
            if state is None:
                continue                      # старая сборка: состояние ещё не размечено
            if str(state) not in allowed:
                problems.append({"request_id": rid, "kind": "state", "id": ident,
                                 "card_state": "",
                                 "why": "состояние «%s» вне словаря §1 (%s)"
                                        % (state, ", ".join(allowed)),
                                 "text": str(item.get("text") or "")[:160]})
            elif str(state) == "open" and item.get("needs_you") and not _actions(item):
                problems.append({"request_id": rid, "kind": "dead_end", "id": ident,
                                 "card_state": "",
                                 "why": "открытый пункт «ждёт вас», а действия нет",
                                 "text": str(item.get("text") or "")[:160]})
            elif str(state) == "postponed" and "restore" not in _action_ids(item):
                # «Отложить» без «Вернуть» — чёрная дыра: пункт ушёл из счётчиков,
                # и вернуть его человеку нечем (правило «без тупиков»).
                problems.append({"request_id": rid, "kind": "dead_end", "id": ident,
                                 "card_state": "",
                                 "why": "отложенный вопрос без кнопки «Вернуть»: "
                                        "пункт нигде не ждёт и вернуться не может",
                                 "text": str(item.get("text") or "")[:160]})
    return problems


def _counter_problems(detail: dict, rid: str) -> list[dict]:
    """`needs_you_count` должен совпадать с числом пунктов, которые правда ждут (§1).

    W31-FIX4: в наборе — и заблокированные карточки (`blocked`), тот же список, что
    в правой колонке и на доске. Раньше они считались в статусе «нужен ваш ответ» и
    в «Сегодня», но не в счётчике задачи: человек видел «ждёт вас» и ноль в блоке.
    """
    counter = detail.get("needs_you_count")
    if not isinstance(counter, dict):
        return []
    problems: list[dict] = []
    missing = [key for key in COUNTER_KEYS if key not in counter]
    if missing:
        problems.append({"request_id": rid, "kind": "counter", "id": "", "card_state": "",
                         "why": "в счётчике нет полей: %s" % ", ".join(missing), "text": ""})
        return problems
    actual = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0, "blocked": 0}
    seen: set = set()
    for key, kind, _allowed in ITEM_SECTIONS:
        group = {"question": "questions", "decision": "decisions", "step": "steps"}[kind]
        for item in detail.get(key) or []:
            if not isinstance(item, dict) or not item.get("needs_you"):
                continue
            ident = str(item.get("id") or "")
            if ident and (kind, ident) in seen:
                continue
            if ident:
                seen.add((kind, ident))
            actual[group] += 1
    for item in detail.get("approvals") or []:
        if isinstance(item, dict) and item.get("needs_you"):
            actual["approvals"] += 1
    # Заблокированные карточки: ждут человека все, кроме системных пауз сторожа,
    # и только те, у которых есть кнопка (иначе это скажет check_blocked_cards).
    actual["blocked"] = len([node for node in _blocked_cards(detail)
                             if not is_system_pause(node) and _actions(node)])
    for key in ("questions", "decisions", "approvals", "steps"):
        if counter.get(key) != actual[key]:
            problems.append({"request_id": rid, "kind": "counter", "id": "", "card_state": "",
                             "why": "счётчик «%s»=%s, а пунктов с действием %d"
                                    % (key, counter.get(key), actual[key]), "text": ""})
    if "blocked" in counter and counter.get("blocked") != actual["blocked"]:
        problems.append({"request_id": rid, "kind": "counter", "id": "", "card_state": "",
                         "why": "счётчик «blocked»=%s, а заблокированных карточек с действием %d"
                                % (counter.get("blocked"), actual["blocked"]), "text": ""})
    total = sum(actual.values())
    if counter.get("total") != total:
        problems.append({"request_id": rid, "kind": "counter", "id": "", "card_state": "",
                         "why": "итог счётчика «%s» не равен числу пунктов с действием (%d)"
                                % (counter.get("total"), total), "text": ""})
    return problems


def _blocked_cards(detail: dict) -> list[dict]:
    """Узлы дерева, которые стоят в `blocked` (все — и системные паузы тоже)."""
    out = []
    for node in detail.get("tree") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("status") or "") != "blocked":
            continue
        out.append(node)
    return out


def is_system_pause(node: dict) -> bool:
    """Системная пауза сторожа моделей (`hold.by = guard`) — снимается сама."""
    hold = (node or {}).get("hold")
    if not isinstance(hold, dict):
        return False
    return hold.get("by") == "guard" or hold.get("auto") is True


def check_blocked_cards(detail: dict, request_id: str = "") -> list[dict]:
    """W29-P2: каждая `blocked`-карточка обязана иметь кнопку, а число — совпадать.

    Два правила (их сформулировал руководитель): «пункт, ждущий человека, всегда
    имеет рабочую кнопку» и «число в счётчике = число пунктов в списке». Заблокированная
    карточка — такой же пункт: раньше она считалась в «1 задача ждёт вашего решения»,
    но не показывалась нигде, а в правой колонке была ссылка на пустую вкладку.

    Что проверяется:

    * у узла `status == "blocked"` есть непустой `actions[]`;
    * у обычной карточки среди кнопок есть «дать указание» (`instruct`) — без неё
      человек не может сказать, что делать дальше;
    * у системной паузы сторожа есть «снять сейчас» (`resume`);
    * `needs_you_blocked` (серверное число) равно числу карточек без системной паузы;
    * `blocked_waiting[]`, если он есть, совпадает с этими карточками по id.
    """
    rid = str(request_id or (detail or {}).get("id") or "")
    problems: list[dict] = []
    cards = _blocked_cards(detail if isinstance(detail, dict) else {})
    waiting = [node for node in cards if not is_system_pause(node)]
    for node in cards:
        ident = str(node.get("id") or "")
        actions = [str((a or {}).get("id") or "") for a in (node.get("actions") or [])
                   if isinstance(a, dict)]
        actions = [a for a in actions if a]
        system = is_system_pause(node)
        if not actions:
            problems.append({"request_id": rid, "kind": "blocked", "id": ident, "card_state": "blocked",
                             "why": "заблокированная карточка без единой кнопки: тупик",
                             "text": str(node.get("title") or "")[:160]})
            continue
        if system and "resume" not in actions:
            problems.append({"request_id": rid, "kind": "blocked", "id": ident, "card_state": "blocked",
                             "why": "системная пауза сторожа без кнопки «Снять паузу сейчас»",
                             "text": str(node.get("title") or "")[:160]})
        if not system and "instruct" not in actions:
            problems.append({"request_id": rid, "kind": "blocked", "id": ident, "card_state": "blocked",
                             "why": "нет кнопки «Дать указание / ответить» — карточку нечем двинуть",
                             "text": str(node.get("title") or "")[:160]})
    counted = detail.get("needs_you_blocked") if isinstance(detail, dict) else None
    if isinstance(counted, int) and counted != len(waiting):
        problems.append({"request_id": rid, "kind": "blocked-counter", "id": "", "card_state": "",
                         "why": "число заблокированных карточек «%d», а в списке %d"
                                % (counted, len(waiting)), "text": ""})
    listed = detail.get("blocked_waiting") if isinstance(detail, dict) else None
    if isinstance(listed, list):
        ids_list = [str((c or {}).get("id") or "") for c in listed if isinstance(c, dict)]
        ids_tree = [str(n.get("id") or "") for n in waiting]
        if ids_list != ids_tree:
            problems.append({"request_id": rid, "kind": "blocked-counter", "id": "", "card_state": "",
                             "why": "список «ждёт решения» (%d) не совпадает с деревом (%d)"
                                    % (len(ids_list), len(ids_tree)), "text": ""})
    return problems


def _generic_any(detail: dict) -> bool:
    """Есть ли у запроса другие пункты, которые правда ждут человека.

    W31-FIX4: заблокированная карточка — такой же пункт «ждёт вас» («Продолжить» /
    «Дать указание»), как вопрос или разрешение: без неё проверка объявляла тупиком
    задачу, у которой все вопросы отвечены, а корень стоит заблокированным, хотя
    человеку есть что нажать. Системная пауза сторожа (``hold``) ждёт не человека —
    её не считаем.
    """
    for key in ("questions", "open_questions", "decisions", "next_steps"):
        for item in detail.get(key) or []:
            if not isinstance(item, dict):
                continue
            if key in ("questions", "open_questions"):
                if (item.get("for") or "mikhail") == "mikhail" and item.get("needs_answer"):
                    return True
            elif key == "decisions":
                if item.get("open") is not False:
                    return True
            elif not item.get("done") and not item.get("cancelled_by_answer"):
                return True
    for node in detail.get("tree") or []:
        if not isinstance(node, dict) or str(node.get("status") or "") != "blocked":
            continue
        if is_system_pause(node):
            continue
        if _actions(node):
            return True
    return False


# ---------------------------------------------------------------- HTTP-обход

def _get(base: str, path: str, timeout: float = 20.0) -> dict:
    url = base.rstrip("/") + path
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310 - локальный сервер
        payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    return payload if isinstance(payload, dict) else {}


def _post(base: str, path: str, body: dict, timeout: float = 20.0) -> tuple:
    """POST на живой сервер: ``(код, тело)``. Ошибка — кодом и телом, не исключением.

    Нужен кнопкам «без тупиков» (W28-B): нажатие обязано либо изменить состояние
    (200), либо честно сказать, почему нет (4xx с причиной).
    """
    url = base.rstrip("/") + path
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - локальный сервер
            raw = response.read().decode("utf-8", "replace") or "{}"
            payload = json.loads(raw)
            return response.status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") or "{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw[:400]}
        return exc.code, payload if isinstance(payload, dict) else {}


def walk(base: str, room: str = "", limit: int = MAX_REQUESTS, timeout: float = 20.0,
         verbose: bool = False, log=None) -> dict:
    """Обойти «ждёт вас» на живом сервере и вернуть отчёт ``{problems, checked, …}``."""
    log = log or (lambda text: None)
    started = time.time()
    rooms = _get(base, "/api/rooms", timeout).get("rooms") or []
    rooms = [r for r in rooms if isinstance(r, dict) and r.get("id")]
    if room:
        rooms = [r for r in rooms if str(r.get("id")) == room]
    report = {"base": base, "rooms": [], "checked": 0, "approvals": 0,
              "problems": [], "at": int(started), "ms": 0, "errors": []}
    for row in rooms:
        room_id = str(row.get("id"))
        entry = {"room": room_id, "request_id": str(row.get("title") or room_id),
                 "requests": 0, "approvals": 0, "problems": 0}
        try:
            items = _get(base, "/api/rooms/%s/requests?limit=100" % room_id, timeout).get("items") or []
        except (urllib.error.URLError, OSError, ValueError) as exc:
            entry["error"] = str(exc)[:160]
            report["errors"].append({"room": room_id, "why": str(exc)[:160]})
            report["rooms"].append(entry)
            continue
        for summary in items:
            if not isinstance(summary, dict) or not summary.get("id"):
                continue
            if report["checked"] >= max(1, int(limit)):
                break
            if not summary.get("needs_you"):
                continue                    # не «ждёт вас» — тупику неоткуда взяться
            request_id = str(summary.get("id"))
            try:
                detail = _get(base, "/api/requests/%s" % request_id, timeout)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                report["errors"].append({"request": request_id, "why": str(exc)[:160]})
                continue
            report["checked"] += 1
            entry["requests"] += 1
            found = check_detail(detail, request_id)
            count = len([a for a in (detail.get("approvals") or []) if isinstance(a, dict)])
            entry["approvals"] += count
            report["approvals"] += count
            entry["problems"] += len(found)
            for item in found:
                item["room"] = room_id
                item["request_title"] = str(summary.get("title") or "")
                report["problems"].append(item)
                log("  ! %s · %s — %s" % (request_id, item["kind"], item["why"]))
            if verbose:
                log("  · %s: блокировок %d, проблем %d" % (request_id, count, len(found)))
        report["rooms"].append(entry)
        if report["checked"] >= max(1, int(limit)):
            break
    report["ms"] = int((time.time() - started) * 1000)
    return report


# ---------------------------------------------------------------- W28-B: нажатия ---
#: Сколько нажатий за один обход (страховка «не разорить живую комнату»).
MAX_PRESS = 40
#: Действия, у которых тело своё (§W28-B: контракт карточки разрешения).
#: ``ack`` — только вид (серверу не шлётся), ``restore`` — возврат снятого пункта.
LOCAL_ONLY_ACTIONS = ("ack",)
#: Действия-решения: сервер ждёт `{action:"approval", decision:…}`.
DECISION_ACTIONS = ("once", "always", "deny")


def press_body(item: dict, action: str) -> dict:
    """Тело нажатия по контракту ручки ``POST /api/requests/{id}/actions``.

    Один контракт на все кнопки (W28-B): ``{action, approval_id, ttl?}`` для
    ``hint_agent``/``trust_profile``, ``{action:"approval", decision}`` для разрешения,
    ``{action:"dismiss"|"restore", target:{kind,id}}`` для снятия и возврата.
    """
    ident = str(item.get("id") or "")
    if action in ("dismiss", "restore"):
        return {"action": action, "target": {"kind": "approval", "id": ident}}
    if action in DECISION_ACTIONS:
        return {"action": "approval", "approval_id": ident, "decision": action}
    body = {"action": str(action), "approval_id": ident}
    if action == "trust_profile":
        # Срок обязателен: берём самый безопасный — «до конца карточки».
        body["ttl"] = "card"
    return body


def press_item(base: str, rid: str, item: dict, post=None, timeout: float = 20.0) -> dict:
    """Нажать первую кнопку пункта и проверить, что состояние правда изменилось.

    Кнопки пункта взаимоисключающие (одно решение на пункт), поэтому нажимаем ОДНУ —
    первую, которую показывает интерфейс (сервер отдаёт их в порядке важности).
    Остальные имена проверяются тем, что сервер их знает (`ACTION_ALLOWED`) — иначе
    они бы отвечали 400, а не меняли состояние.
    """
    post = post or _post
    ident = str(item.get("id") or "")
    before = str(item.get("state") or "")
    actions = [str((a or {}).get("id") or "") for a in _actions(item) if isinstance(a, dict)]
    actions = [a for a in actions if a and a not in LOCAL_ONLY_ACTIONS]
    out = {"request_id": rid, "id": ident, "from": before, "action": "", "to": "", "status": 0,
           "why": ""}
    if not actions:
        out["why"] = "у пункта нет кнопки, которая меняет состояние"
        return out
    out["action"] = actions[0]
    body = press_body(item, actions[0])
    try:
        status, payload = post(base, "/api/requests/%s/actions" % rid, body, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["why"] = "сервер не ответил на нажатие: %s" % str(exc)[:160]
        return out
    out["status"] = int(status)
    after = str(((payload or {}).get("approval") or {}).get("state") or "")
    out["to"] = after
    if status == 409:
        # Пункт уже решён (второй клик/параллельная вкладка) — состояние уже не «ждёт».
        out["already"] = True
        return out
    if status != 200:
        error = (payload or {}).get("error") or {}
        out["why"] = ("нажатие «%s» не изменило состояние: %s %s"
                      % (out["action"], status,
                         str(error.get("message") or error.get("code") or "")[:160]))
        return out
    if not after or after == before:
        out["why"] = ("нажатие «%s» вернуло 200, но состояние осталось «%s»"
                      % (out["action"], before or "?"))
    return out


def walk_press(base: str, room: str = "", limit: int = MAX_REQUESTS, timeout: float = 20.0,
               post=None, max_press: int = MAX_PRESS) -> dict:
    """Обойти «ждёт вас» и НАЖАТЬ кнопку каждого ждущего пункта (W28-B).

    Возвращает ``{checked, pressed, already, problems, presses}``. Проблема — кнопка,
    которая не меняет состояние: это и есть «мёртвая кнопка» из дефекта 6.
    """
    post = post or _post
    started = time.time()
    report = {"base": base, "room": room, "checked": 0, "pressed": 0, "already": 0,
              "problems": [], "presses": [], "errors": [], "ms": 0, "at": int(started)}
    try:
        rooms = _get(base, "/api/rooms", timeout).get("rooms") or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        report["errors"].append({"why": str(exc)[:160]})
        return report
    rooms = [r for r in rooms if isinstance(r, dict) and r.get("id")]
    if room:
        rooms = [r for r in rooms if str(r.get("id")) == room]
    for row in rooms:
        room_id = str(row.get("id"))
        try:
            items = _get(base, "/api/rooms/%s/requests?limit=100" % room_id, timeout)
            requests = items.get("items") or []
        except (urllib.error.URLError, OSError, ValueError) as exc:
            report["errors"].append({"room": room_id, "why": str(exc)[:160]})
            continue
        for summary in requests:
            if not isinstance(summary, dict) or not summary.get("id"):
                continue
            # Недоговорные пункты (`workaround`/`info`) не делают запрос «нужен ваш
            # ответ», но кнопка у них есть — поэтому смотрим ВСЕ запросы комнаты,
            # а решает фильтр по состоянию пункта ниже.
            if report["pressed"] + report["already"] >= max(1, int(max_press)):
                break
            rid = str(summary["id"])
            try:
                detail = _get(base, "/api/requests/%s" % rid, timeout)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                report["errors"].append({"request": rid, "why": str(exc)[:160]})
                continue
            report["checked"] += 1
            for item in detail.get("approvals") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("state") or "") not in UNDECIDED:
                    continue                        # решённое не нажимаем
                if not _actions(item):
                    report["problems"].append({"request_id": rid, "room": room_id,
                                               "id": str(item.get("id") or ""),
                                               "why": "у ждущего пункта нет кнопки"})
                    continue
                if report["pressed"] + report["already"] >= max(1, int(max_press)):
                    break
                result = press_item(base, rid, item, post=post, timeout=timeout)
                report["presses"].append(result)
                if result.get("already"):
                    report["already"] += 1
                elif result.get("why"):
                    report["problems"].append(dict(result, room=room_id))
                else:
                    report["pressed"] += 1
    report["ms"] = int((time.time() - started) * 1000)
    return report


# ------------------------------------------------------- W27-E3 «Сегодня» ---

#: Разделы «Сегодня», где строка — дело со сроком (дефект 2: «постер, не список»).
TODAY_LISTS = ("overdue", "due_soon")


def check_today_row(row: dict, kind: str = "") -> list[str]:
    """Проблемы одной строки «Просрочено»/«Ближайшие».

    У строки обязаны быть ДВА свойства, иначе это тупик (правило «без тупиков»):

    * **переход** — `room` (страница запроса с `?step=` или вкладка «От вас нужно»
      комнаты; адрес строится интерфейсом из этих полей);
    * **действие** — `item_id` пункта бэклога (по нему работают «Готово»,
      «Перенести срок», «Не актуально», «Поручить»).
    """
    if not isinstance(row, dict):
        return []
    out: list[str] = []
    if not str(row.get("room") or ""):
        out.append("строка без комнаты: переходить некуда")
    # Явный отказ сервера (`actionable: false`) или отсутствие id пункта —
    # значит «Готово» нажать не на что.
    if row.get("actionable") is False or not str(row.get("item_id") or ""):
        out.append("тупик: строку нельзя закрыть — нет действия")
    return out


def check_today(data: dict) -> list[dict]:
    """Проверить ответ ``GET /api/today``: переход, действие, честный счётчик."""
    if not isinstance(data, dict):
        return []
    problems: list[dict] = []
    for key in TODAY_LISTS:
        for row in data.get(key) or []:
            if not isinstance(row, dict):
                continue
            for why in check_today_row(row, key):
                problems.append({"request_id": str(row.get("request_id") or ""), "kind": "today",
                                 "id": str(row.get("item_id") or ""), "card_state": "",
                                 "section": key, "room": str(row.get("room") or ""),
                                 "why": why, "text": str(row.get("text") or "")[:160]})
    counters = data.get("counters") if isinstance(data.get("counters"), dict) else {}
    counted = counters.get("overdue")
    if isinstance(counted, int):
        only_over = sum(1 for row in (data.get("overdue") or [])
                        if isinstance(row, dict) and str(row.get("item_id") or "")
                        and row.get("actionable") is not False)
        if counted > only_over:
            problems.append({"request_id": "", "kind": "today-counter", "id": "", "card_state": "",
                             "section": "overdue", "room": "",
                             "why": "счётчик «просрочено» (%d) больше числа закрываемых строк (%d)"
                                    % (counted, only_over), "text": ""})
    return problems


def walk_today(base: str, timeout: float = 20.0) -> dict:
    """Прочитать ``/api/today`` живого сервера и проверить строки."""
    started = time.time()
    report = {"base": base, "rooms": [], "checked": 0, "approvals": 0,
              "problems": [], "at": int(started), "ms": 0, "errors": [],
              "today": {"overdue": 0, "due_soon": 0}}
    try:
        data = _get(base, "/api/today", timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        report["errors"].append({"request": "/api/today", "why": str(exc)[:160]})
        report["ms"] = int((time.time() - started) * 1000)
        return report
    report["today"]["overdue"] = len(data.get("overdue") or [])
    report["today"]["due_soon"] = len(data.get("due_soon") or [])
    report["checked"] = 1
    for item in check_today(data):
        report["problems"].append(item)
    report["ms"] = int((time.time() - started) * 1000)
    return report


# ---------------------------------------------------------------- самопроверка

_GOOD = {"id": "ap_1", "state": "pending", "mode": "grant", "grantable": True,
         "card_state": "running", "description": "запуск скрипта через heredoc",
         "note": "ждёт вашего решения", "actions": [{"id": "once"}, {"id": "deny"}]}
_BAD_DEAD_END = {"id": "ap_2", "state": "pending", "mode": "grant", "grantable": True,
                 "card_state": "running", "description": "запуск скрипта через heredoc",
                 "note": "ждёт вашего решения", "actions": []}
_BAD_FINISHED = {"id": "ap_3", "state": "pending", "mode": "grant", "grantable": True,
                 "card_state": "done", "description": "запуск скрипта через heredoc",
                 "note": "ждёт вашего решения", "actions": [{"id": "once"}]}
_BAD_NOTGRANT = {"id": "ap_4", "state": "pending", "mode": "workaround", "grantable": False,
                 "card_state": "running", "description": "execute_code — произвольный код",
                 "note": "ждёт вашего решения", "actions": []}
_GOOD_WORKAROUND = {"id": "ap_5", "state": "workaround", "mode": "workaround",
                    "grantable": False, "card_state": "running",
                    "description": "execute_code — произвольный код",
                    "note": "нельзя разрешить штатно", "actions": [{"id": "hint_agent"}]}


def selftest() -> list[str]:
    """Проверка самой проверки: хорошие строки молчат, плохие — ловятся."""
    bad: list[str] = []
    if check_items([_GOOD, _GOOD_WORKAROUND]):
        bad.append("здоровая строка не должна давать проблем")
    if not check_items([_BAD_DEAD_END]):
        bad.append("не поймал строку без действия")
    if not check_items([_BAD_FINISHED]):
        bad.append("не поймал pending у завершённой карточки")
    if not check_items([_BAD_NOTGRANT]):
        bad.append("не поймал недоговорную строку как «ждёт решения»")
    detail = {"id": "t_1", "approvals": [_BAD_DEAD_END], "approvals_pending": 1,
              "needs_you": True}
    if not check_detail(detail, "t_1"):
        bad.append("не поймал тупик в деталях запроса")
    detail_ok = {"id": "t_2", "approvals": [_GOOD], "approvals_pending": 1, "needs_you": True}
    if check_detail(detail_ok, "t_2"):
        bad.append("здоровый запрос дал ложную тревогу: %s" % check_detail(detail_ok, "t_2"))
    # W27-E3: у каждой строки «Сегодня» — переход и действие, счётчик честный.
    today_ok = {"overdue": [{"room": "r1", "item_id": "t_1:ns_1", "request_id": "t_1",
                             "text": "шаг", "actionable": True}],
                "due_soon": [], "counters": {"overdue": 1}}
    if check_today(today_ok):
        bad.append("здоровая строка «Сегодня» дала ложную тревогу: %s" % check_today(today_ok))
    today_bad = {"overdue": [{"room": "r1", "item_id": "", "text": "постер",
                              "actionable": False}],
                 "due_soon": [], "counters": {"overdue": 1}}
    if len(check_today(today_bad)) < 2:
        bad.append("не поймал строку «Сегодня» без действия и нечестный счётчик")
    # W28-B: кнопка обязана менять состояние; «200 без изменений» и отказ — проблема.
    pressable = {"id": "ap_1", "state": "workaround", "actions": [{"id": "hint_agent"}]}

    def _post_ok(base, path, body, timeout=0):
        return 200, {"ok": True, "approval": {"state": "hinted"}}

    def _post_same(base, path, body, timeout=0):
        return 200, {"ok": True, "approval": {"state": "workaround"}}

    def _post_refused(base, path, body, timeout=0):
        return 400, {"error": {"code": "bad_request", "message": "Неизвестное действие"}}

    if press_item("http://x", "t_1", pressable, post=_post_ok).get("why"):
        bad.append("здоровая кнопка дала ложную тревогу")
    if not press_item("http://x", "t_1", pressable, post=_post_same).get("why"):
        bad.append("не поймал кнопку, которая не меняет состояние")
    if not press_item("http://x", "t_1", pressable, post=_post_refused).get("why"):
        bad.append("не поймал кнопку с отказом сервера (400)")
    if press_body(pressable, "trust_profile").get("ttl") != "card":
        bad.append("у «доверять профилю» не подставлен обязательный срок")
    if press_body(pressable, "once").get("decision") != "once":
        bad.append("решение по разрешению уходит не полем decision")
    # W29-P2: заблокированная карточка без кнопки — тупик; число должно совпадать.
    good_card = {"id": "t_1", "status": "blocked", "title": "сверстать прайс",
                 "actions": [{"id": "resume"}, {"id": "instruct"}, {"id": "model"},
                             {"id": "reassign"}, {"id": "dismiss"}]}
    pause_card = {"id": "t_2", "status": "blocked", "title": "проверить склад",
                  "hold": {"by": "guard", "auto": True}, "actions": [{"id": "resume"}]}
    blocked_ok = {"id": "t_ok", "tree": [{"id": "t_0", "status": "done"}, good_card, pause_card],
                  "needs_you_blocked": 1, "blocked_waiting": [{"id": "t_1"}]}
    if check_blocked_cards(blocked_ok, "t_ok"):
        bad.append("здоровое дерево с blocked дало ложную тревогу: %s"
                   % check_blocked_cards(blocked_ok, "t_ok"))
    blocked_bad = {"id": "t_bad", "tree": [dict(good_card, actions=[])], "needs_you_blocked": 1}
    if not check_blocked_cards(blocked_bad, "t_bad"):
        bad.append("не поймал заблокированную карточку без кнопки")
    no_instruct = {"id": "t_b2", "tree": [dict(good_card, actions=[{"id": "resume"}])],
                   "needs_you_blocked": 1}
    if not check_blocked_cards(no_instruct, "t_b2"):
        bad.append("не поймал карточку без «дать указание»")
    wrong_count = {"id": "t_b3", "tree": [good_card, pause_card], "needs_you_blocked": 2}
    if not check_blocked_cards(wrong_count, "t_b3"):
        bad.append("не поймал число заблокированных, не равное списку")
    # W31-FIX4 №1: «Отложить» — законное состояние вопроса, если есть «Вернуть».
    postponed_ok = {"id": "t_p1", "status": "working", "needs_you": False,
                    "open_questions": [{"id": "q_p1", "state": "postponed", "needs_you": False,
                                        "actions": [{"id": "restore"}], "text": "прайс"}]}
    if check_detail(postponed_ok, "t_p1"):
        bad.append("отложенный вопрос с «Вернуть» дал ложную тревогу: %s"
                   % check_detail(postponed_ok, "t_p1"))
    postponed_bad = {"id": "t_p2", "status": "working", "needs_you": False,
                     "open_questions": [{"id": "q_p2", "state": "postponed", "needs_you": False,
                                         "actions": [], "text": "прайс"}]}
    if not [p for p in check_detail(postponed_bad, "t_p2") if p["kind"] == "dead_end"]:
        bad.append("не поймал отложенный вопрос без «Вернуть»")
    # W31-FIX4 №2: заблокированная карточка — пункт «ждёт вас» и в счётчике.
    blocked_point = {"id": "t_b4", "status": "needs_you", "needs_you": True,
                     "open_questions": [{"id": "q_b4", "state": "answered", "needs_you": False}],
                     "tree": [{"id": "t_b4", "status": "blocked", "title": "сверстать прайс",
                               "actions": [{"id": "resume"}, {"id": "instruct"}]}],
                     "needs_you_count": {"questions": 0, "decisions": 0, "approvals": 0,
                                         "steps": 0, "blocked": 1, "total": 1},
                     "needs_you_blocked": 1,
                     "blocked_waiting": [{"id": "t_b4", "actions": [{"id": "resume"}]}]}
    if check_detail(blocked_point, "t_b4"):
        bad.append("заблокированная карточка в счётчике дала ложную тревогу: %s"
                   % check_detail(blocked_point, "t_b4"))
    zero_counter = dict(blocked_point, needs_you_count={"questions": 0, "decisions": 0,
                                                        "approvals": 0, "steps": 0, "total": 0})
    if not [p for p in check_detail(zero_counter, "t_b5") if p["kind"] == "counter"]:
        bad.append("не поймал счётчик, не считающий заблокированную карточку")
    nothing_left = {"id": "t_e1", "status": "needs_you", "needs_you": True,
                    "open_questions": [{"id": "q_e1", "state": "answered", "needs_you": False}],
                    "needs_you_count": {"questions": 0, "decisions": 0, "approvals": 0,
                                        "steps": 0, "blocked": 0, "total": 0}}
    if not [p for p in check_detail(nothing_left, "t_e1") if "решать нечего" in p["why"]]:
        bad.append("не поймал «нужен ваш ответ» без единого пункта")
    return bad


def _print_report(report: dict, quiet: bool = False) -> None:
    rooms = len(report.get("rooms") or [])
    print("Проверка «без тупиков»: %s" % report.get("base") or "(офлайн)")
    print("Комнат: %d · запросов проверено: %d · блокировок: %d · проблем: %d · %d мс"
          % (rooms, report.get("checked") or 0, report.get("approvals") or 0,
             len(report.get("problems") or []), report.get("ms") or 0))
    if report.get("errors"):
        print("Не прочитано (пропущено): %d" % len(report["errors"]))
        if not quiet:
            for item in report["errors"][:10]:
                print("  ? %s — %s" % (item.get("room") or item.get("request"), item.get("why")))
    problems = report.get("problems") or []
    if not problems:
        print("Тупиков нет: у каждого пункта «ждёт вас» есть действие, "
              "у завершённых карточек pending нет.")
        return
    print("ТУПИКИ (%d):" % len(problems))
    for item in problems[:60]:
        print("  [%s] %s · %s%s — %s" % (
            item.get("room") or "-", item.get("request_id") or "-", item.get("kind") or "-",
            ("#" + item["id"]) if item.get("id") else "", item.get("why")))
        if not quiet and item.get("text"):
            print("      «%s»" % item["text"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Обход «ждёт вас»: не должно быть тупиков")
    parser.add_argument("--base", default="", help="адрес сервера, например http://127.0.0.1:8851")
    parser.add_argument("--room", default="", help="только одна комната")
    parser.add_argument("--limit", type=int, default=MAX_REQUESTS, help="сколько запросов обойти")
    parser.add_argument("--items", default="", help="офлайн: JSON-массив approvals[]")
    parser.add_argument("--json", default="", help="записать отчёт в файл")
    parser.add_argument("--quiet", action="store_true", help="только итог")
    parser.add_argument("--verbose", action="store_true", help="печатать каждый запрос")
    parser.add_argument("--selftest", action="store_true", help="проверить саму проверку")
    # W27-E3: отдельный обход «Сегодня» — у каждой строки срок должен быть
    # переход и действие (дефект 2: «постер, а не список»).
    parser.add_argument("--today", action="store_true",
                        help="проверить только строки «Просрочено/Ближайшие» (/api/today)")
    # W28-B: нажать кнопку каждого ждущего пункта и проверить, что состояние меняется.
    parser.add_argument("--press", action="store_true",
                        help="нажать кнопки ждущих пунктов и проверить смену состояния")
    parser.add_argument("--max-press", type=int, default=MAX_PRESS,
                        help="сколько нажатий за обход (по умолчанию %d)" % MAX_PRESS)
    args = parser.parse_args(argv)

    if args.selftest:
        bad = selftest()
        for line in bad:
            print("СБОЙ: %s" % line)
        print("Самопроверка: %s" % ("ок" if not bad else "не прошла"))
        return 0 if not bad else 1

    if args.items:
        try:
            with open(args.items, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            print("Не прочитал %s: %s" % (args.items, exc), file=sys.stderr)
            return 2
        items = data.get("approvals") if isinstance(data, dict) else data
        problems = check_items(items or [], str((data or {}).get("request_id") or "")
                               if isinstance(data, dict) else "")
        report = {"base": args.items, "rooms": [], "checked": 0, "approvals": len(items or []),
                  "problems": problems, "ms": 0, "errors": []}
        _print_report(report, args.quiet)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
        return 0 if not problems else 1

    if not args.base:
        parser.print_help()
        print("\nНужен --base http://127.0.0.1:ПОРТ (или --items ФАЙЛ, или --selftest).")
        return 2
    if args.press:
        try:
            report = walk_press(args.base, room=args.room, limit=args.limit,
                                max_press=args.max_press)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("Сервер не ответил (%s): %s" % (args.base, exc), file=sys.stderr)
            return 2
        print("Нажатия кнопок «ждёт вас»: проверено разделов %d · нажато %d · уже решено %d · "
              "проблем: %d · %d мс" % (report.get("checked") or 0, report.get("pressed") or 0,
                                       report.get("already") or 0,
                                       len(report.get("problems") or []), report.get("ms") or 0))
        if report.get("errors"):
            for item in report["errors"][:5]:
                print("  ? %s — %s" % (item.get("room") or item.get("request"), item.get("why")))
        for item in report.get("problems") or []:
            print("  ТУПИК [%s] %s — %s" % (item.get("action") or "-", item.get("id") or "-",
                                            item.get("why")))
        for item in report.get("presses") or []:
            print("  · %s: «%s» → %s (%s)" % (item.get("id") or "-", item.get("action") or "-",
                                              item.get("to") or "-", item.get("status")))
        if not report.get("problems"):
            print("Мёртвых кнопок нет: каждое нажатие изменило состояние пункта.")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
        return 0 if not report.get("problems") else 1
    if args.today:
        report = walk_today(args.base)
        rows = report.get("today") or {}
        print("Строки «Сегодня»: просрочено %d, ближайших %d · проблем: %d · %d мс"
              % (rows.get("overdue") or 0, rows.get("due_soon") or 0,
                 len(report.get("problems") or []), report.get("ms") or 0))
        if report.get("errors"):
            for item in report["errors"][:5]:
                print("  ? %s — %s" % (item.get("request"), item.get("why")))
        for item in report.get("problems") or []:
            print("  ТУПИК [%s] %s — %s" % (item.get("section") or "-",
                                            item.get("id") or "-", item.get("why")))
        if not report.get("problems"):
            print("Тупиков нет: у каждой строки есть переход и действие.")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
        return 0 if not report.get("problems") else 1
    try:
        report = walk(args.base, room=args.room, limit=args.limit, verbose=args.verbose,
                      log=(None if args.quiet else print))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("Сервер не ответил (%s): %s" % (args.base, exc), file=sys.stderr)
        return 2
    _print_report(report, args.quiet)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
