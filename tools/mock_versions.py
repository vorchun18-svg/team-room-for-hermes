# -*- coding: utf-8 -*-
"""Расписание фаз и форма v2 для мок-сервера (ARCH §12.4–12.5).

Чистые функции без состояния: расписание эволюции живого запроса, нормализация
RequestDetail до формы v2 (для legacy-фикстур) и словари допустимых значений.
"""
from __future__ import annotations

import time

# W20-FA §1.2 (B-38): вопросы руководителя из ленты демо разбирает ТОТ ЖЕ модуль,
# что у сервера — второго мнения о том, что считать вопросом, быть не должно.
import sys as _sys
from pathlib import Path as _Path

_SERVER_DIR = _Path(__file__).resolve().parent.parent / "server"
if str(_SERVER_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SERVER_DIR))
import answers as _answers  # noqa: E402  (server/answers.py, чистые функции)

# Фазы: (секунды от создания, имя). С критиком цикл длиннее на один шаг.
PHASES_CRITIC = ((0.0, "queued"), (4.0, "working"), (12.0, "reviewing"), (20.0, "synthesizing"), (35.0, "done"))
PHASES_PLAIN = ((0.0, "queued"), (4.0, "working"), (22.0, "synthesizing"), (30.0, "done"))
PHASE_ORDER = ("queued", "working", "reviewing", "synthesizing", "done")
SYNTHESIS_STATES = ("pending", "ready", "none")
CRITIC_STATES = ("off", "waiting", "reviewing", "approved", "revised", "failed")
TREE_ROLES = ("worker", "critic", "synthesizer", "root", "revision")
#: «Тишина» перед автообновлением свода — как `auto_svod_quiet_s` в комнате (демо).
SVOD_QUIET_S = 180
#: W21-S2 (B-45): после ответа на вопрос — минута, и потолок частоты не держит.
ANSWERS_QUIET_S = 60
#: W21-S2 (B-46): карточки, из-за которых свод ждёт команду (демо идёт ~35 с, и это видно).
WAIT_STATUSES = ("triage", "todo", "ready", "running", "review", "scheduled")
WAITING_CARDS_TEXT = "Обновится после завершения работы команды"
#: W21-S2: до какого момента сообщения демо считаются «написанными раньше
#: автообновления» (подпись `grace_start`). 0 — выключено; демо-сервер включает это
#: при `TEAMROOM_DEMO_GRACE=1` (см. tools/mock_server.py).
GRACE_START_AT = 0


def _hhmm(stamp) -> str:
    try:
        value = int(stamp or 0)
    except (TypeError, ValueError):
        return ""
    return time.strftime("%H:%M", time.localtime(value)) if value else ""


def _ru(number: int, one: str, few: str, many: str) -> str:
    number = abs(int(number))
    tail, last = number % 100, number % 10
    if 11 <= tail <= 14:
        word = many
    elif last == 1:
        word = one
    elif 2 <= last <= 4:
        word = few
    else:
        word = many
    return "%d %s" % (number, word)


def _counts_ru(behind: dict) -> str:
    parts = []
    if behind.get("answers"):
        parts.append(_ru(behind["answers"], "ответ", "ответа", "ответов"))
    if behind.get("notes"):
        parts.append(_ru(behind["notes"], "сообщение", "сообщения", "сообщений"))
    if behind.get("decisions"):
        parts.append(_ru(behind["decisions"], "решение", "решения", "решений"))
    if behind.get("cards"):
        parts.append(_ru(behind["cards"], "карточка", "карточки", "карточек"))
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " и " + parts[-1]


def _busy_cards(detail: dict) -> list:
    """Карточки демо, из-за которых свод ещё не собирается (W21-S2, B-46)."""
    out = []
    for node in detail.get("tree") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("role") or "") == "root":
            continue                      # корень — сам запрос
        if str(node.get("title") or "").startswith("Обновление свода"):
            continue                      # своя карточка свода свод не держит
        if node.get("needs_human"):
            continue                      # ждёт человека — не держим
        if str(node.get("status") or "") in WAIT_STATUSES:
            out.append(node)
    return out


def svod_live(detail: dict, since: int = 0) -> dict:
    """Демо-версия `synthesis.live` (W20-S1): то же, что считает server/svod_live.py.

    Упрощено намеренно: события — сообщения Михаила, ответы на вопросы и решения
    после выпуска версии; «тишина» — 3 минуты (после ответа — минута, W21-S2 B-45).
    Мок не запускает работу, поэтому `updating` появляется только после нажатия
    «Обновить сейчас». Пока идёт цепочка карточек команды — `waiting_cards` (B-46).
    """
    syn = detail.get("synthesis") if isinstance(detail.get("synthesis"), dict) else {}
    history = [h for h in (syn.get("history") or []) if isinstance(h, dict)]
    version = int(syn.get("version") or 0)
    at = int((history[-1].get("at") if history else syn.get("at")) or 0)
    floor = int(since or GRACE_START_AT or 0)
    behind = {"answers": 0, "notes": 0, "decisions": 0, "cards": 0}
    last, last_kind = 0, ""
    for item in detail.get("thread") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("author") or "") != "mikhail":
            continue
        if str(item.get("kind") or "") in ("event", "system"):
            continue
        if item.get("svod_event") is False:
            continue                      # B-47: снятие/возврат решения — не событие
        stamp = int(item.get("at") or 0)
        if stamp <= at or (floor and stamp < floor):
            continue
        behind["notes"] += 1
        last, last_kind = max(last, stamp), "note"
    for question in detail.get("open_questions") or []:
        if not isinstance(question, dict) or not question.get("answer"):
            continue
        stamp = int(question.get("answered_at") or 0)
        if stamp > at and not (floor and stamp < floor):
            behind["answers"] += 1
            last, last_kind = max(last, stamp), "answer"
    for decision in detail.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        stamp = int(decision.get("at") or 0)
        if stamp > at and not (floor and stamp < floor):
            behind["decisions"] += 1
            last, last_kind = max(last, stamp), "decision"
    total = sum(behind.values())
    live = {"state": "fresh", "since_version": version, "behind": behind,
            "last_event_at": last or None, "next_update_at": None, "auto": True, "text": ""}
    if not version:
        live.update(state="off",
                    text="Свод ещё не выпущен — появится после первого ответа команды.")
        return live
    if not total:
        live["text"] = "Актуально%s · версия %d" % (
            " на " + _hhmm(at) if at else "", version)
        return live
    if _busy_cards(detail):
        live.update(state="waiting_cards", text=WAITING_CARDS_TEXT)
        return live
    quiet = ANSWERS_QUIET_S if last_kind == "answer" else SVOD_QUIET_S
    next_at = last + quiet
    live.update(state="pending", next_update_at=next_at,
                text="Отстаёт: %s не учтены — обновится в %s" % (_counts_ru(behind), _hhmm(next_at)))
    return live


def _delivery(item: dict, live: dict, cutoff: int, since: int = 0) -> dict:
    """Подпись доставки сообщения Михаила — та же, что отдаёт API (`delivery`)."""
    at = int(item.get("at") or 0)
    state = str(live.get("state") or "")
    version = int(live.get("since_version") or 0)
    if state == "off":
        return {"state": "waiting", "version": None, "cards": [], "reason": "no_svod",
                "text": "Ждёт выпуска свода"}
    if at and cutoff and at <= cutoff:
        return {"state": "applied", "version": version, "cards": [], "reason": "",
                "text": "Учтено в версии %d" % version if version else "Учтено в своде"}
    if since and at and at < since:
        # W21-S2: тот же случай, что на сервере — написано до включения автообновления
        return {"state": "declined", "version": None, "cards": [], "reason": "grace_start",
                "text": "Не учтено — написано до включения автообновления; "
                        "«Обновить сейчас» учтёт вручную"}
    if state == "stale":
        return {"state": "declined", "version": None, "cards": [], "reason": "auto_off",
                "text": "Не учтено — автообновление свода выключено"}
    if state == "waiting_cards":
        return {"state": "waiting", "version": None, "cards": [], "reason": "waiting_cards",
                "text": "Ждёт завершения работы команды"}
    next_at = int(live.get("next_update_at") or 0)
    return {"state": "waiting", "version": None, "cards": [], "reason": "",
            "text": "Ждёт обновления свода" + (" — в %s" % _hhmm(next_at) if next_at else "")}


def apply_message_questions(detail: dict, dismissed=None) -> list:
    """Вопросы руководителя из ленты демо и метка ``asks`` (W20-FA §1.2, B-38).

    Разбор — общий с сервером (``answers.questions_from_thread``/``mark_asks``):
    метки ``[Руководитель · вопрос]`` и пункты под заголовком «Что решить» дают
    вопросы с ``source=comment|candidate``, а сообщение руководителя получает
    ``asks:[id]`` — по нему интерфейс рисует «ждёт ваших ответов: N → справа».
    """
    thread = detail.get("thread")
    if not isinstance(thread, list) or not thread:
        return []
    found = _answers.questions_from_thread(thread, None, dismissed if isinstance(dismissed, dict) else {})
    if not found:
        return []
    existing = detail.get("open_questions")
    if not isinstance(existing, list):
        existing = detail["open_questions"] = []
    seen_ids = {str(q.get("id")) for q in existing if isinstance(q, dict)}
    seen_text = {_answers.text_key(q.get("text")) for q in existing if isinstance(q, dict)}
    for question in found:
        key = _answers.text_key(question.get("text"))
        if str(question.get("id")) in seen_ids or (key and key in seen_text):
            continue
        existing.append(question)
        seen_ids.add(str(question.get("id")))
        if key:
            seen_text.add(key)
    questions = [q for q in existing
                 if isinstance(q, dict) and str(q.get("kind") or "question") == "question"]
    _answers.mark_asks(thread, questions)
    return questions


def apply_svod_live(detail: dict, dismissed=None, since=None) -> dict:
    """Дополнить деталь демо живым сводом: `synthesis.live` и `delivery` у сообщений.

    Вызывается ПОСЛЕ сборки ленты (в `MockApp.detail`): иначе сообщения Михаила ещё
    не известны и «отстаёт» считалось бы без них. Уже посчитанное состояние
    (`updating` после кнопки «Обновить сейчас») не перезаписываем.

    ``since`` — «льготный старт» (W21-S2): сообщения раньше него показываются как
    «написано до включения автообновления». По умолчанию — `GRACE_START_AT`
    (0 — выключено).
    """
    if not isinstance(detail, dict):
        return {}
    syn = detail.get("synthesis")
    if not isinstance(syn, dict):
        return {}
    floor = int(GRACE_START_AT if since is None else (since or 0))
    # Вопросы руководителя из ленты — ДО подсчёта «отстаёт»: снятые вопросы
    # («Это не вопрос») в счётчики не идут.
    apply_message_questions(detail, dismissed)
    live = syn.get("live")
    if not isinstance(live, dict) or not live:
        live = syn["live"] = svod_live(detail, floor)
    history = [h for h in (syn.get("history") or []) if isinstance(h, dict)]
    cutoff = int((history[-1].get("at") if history else 0) or 0)
    thread = detail.get("thread")
    if isinstance(thread, list):
        for item in thread:
            if not isinstance(item, dict):
                continue
            if str(item.get("author") or "") != "mikhail":
                continue
            if str(item.get("kind") or "") in ("event", "system"):
                continue
            item.setdefault("delivery", _delivery(item, live, cutoff, floor))
    return live


def schedule(critic: bool):
    """Шаги эволюции: с критиком (35 с) или без (30 с)."""
    return PHASES_CRITIC if critic else PHASES_PLAIN


def phase_at(elapsed: float, critic: bool) -> str:
    """Фаза по прошедшим секундам (монотонно, последняя подходящая)."""
    phase = "queued"
    for start, name in schedule(critic):
        if elapsed >= start:
            phase = name
    return phase


def phase_start(name: str, critic: bool) -> float:
    """Секунда старта фазы; `reviewing` без критика превращается в `synthesizing`."""
    for start, step in schedule(critic):
        if step == name:
            return start
    if name == "reviewing":
        return phase_start("synthesizing", critic)
    raise KeyError(name)


def total_seconds(critic: bool) -> int:
    return int(schedule(critic)[-1][0])


def normalize_v2(detail: dict) -> dict:
    """Дополнить RequestDetail полями v2 (synthesis.version/history, critic, outside_room).

    Ничего не затирает: старые/legacy-ответы получают version=1 и history из одного run'а.
    """
    syn = detail.get("synthesis")
    syn = dict(syn) if isinstance(syn, dict) else {}
    if syn.get("state") == "missing":  # прежнее имя состояния «завершён без текста»
        syn["state"] = "none"
    syn.setdefault("state", "pending")
    if syn["state"] not in SYNTHESIS_STATES:
        syn["state"] = "none"
    syn.setdefault("version", 1 if syn["state"] == "ready" else 0)
    syn.setdefault("text_md", "")
    syn.setdefault("author", None)
    syn.setdefault("at", None)
    syn.setdefault("source_task", syn.get("source_task"))
    syn.setdefault("legacy", False)
    if not isinstance(syn.get("history"), list):
        syn["history"] = ([{"version": int(syn.get("version") or 1), "kind": "initial",
                            "task_id": syn.get("source_task") or detail.get("id"),
                            "author": syn.get("author"), "at": syn.get("at"),
                            "verdict": None, "note": ""}] if syn["state"] == "ready" else [])
    detail["synthesis"] = syn

    critic = detail.get("critic")
    critic = dict(critic) if isinstance(critic, dict) else {}
    enabled = bool(critic.get("enabled", detail.get("critic")))
    critic.setdefault("enabled", enabled)
    critic.setdefault("round", 0)
    critic.setdefault("max_rounds", 1)
    if "state" not in critic:
        if detail.get("status") == "reviewing":
            critic["state"] = "reviewing"
        elif syn["state"] == "ready":
            # завершённый ответ без карточки критика (legacy) — критик не вёлся приложением
            critic["state"], critic["enabled"] = "off", False
        else:
            critic["state"] = "waiting" if enabled else "off"
    if critic["state"] not in CRITIC_STATES:
        critic["state"] = "off"
    critic.setdefault("verdict", None)
    critic.setdefault("task_id", None)
    if not isinstance(critic.get("issues"), list):
        critic["issues"] = []
    detail["critic"] = critic

    for node in detail.get("tree") or []:
        node.setdefault("role", "worker")
        node.setdefault("outside_room", False)

    return detail
