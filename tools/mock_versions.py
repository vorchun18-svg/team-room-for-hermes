# -*- coding: utf-8 -*-
"""Расписание фаз и форма v2 для мок-сервера (ARCH §12.4–12.5).

Чистые функции без состояния: расписание эволюции живого запроса, нормализация
RequestDetail до формы v2 (для legacy-фикстур) и словари допустимых значений.
"""
from __future__ import annotations

# Фазы: (секунды от создания, имя). С критиком цикл длиннее на один шаг.
PHASES_CRITIC = ((0.0, "queued"), (4.0, "working"), (12.0, "reviewing"), (20.0, "synthesizing"), (35.0, "done"))
PHASES_PLAIN = ((0.0, "queued"), (4.0, "working"), (22.0, "synthesizing"), (30.0, "done"))
PHASE_ORDER = ("queued", "working", "reviewing", "synthesizing", "done")
SYNTHESIS_STATES = ("pending", "ready", "none")
CRITIC_STATES = ("off", "waiting", "reviewing", "approved", "revised", "failed")
TREE_ROLES = ("worker", "critic", "synthesizer", "root", "revision")


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
