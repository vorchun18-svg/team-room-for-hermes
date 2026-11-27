# -*- coding: utf-8 -*-
"""Демо «Рецепты» для мок-сервера (W29B-Q1).

Форма ответов — как у боевого сервера (`server/recipes.py`): список рецептов,
корзина, история запусков, роли комнаты, виды полей. Состояние меняется в
памяти демо-сервера (правка, корзина, возврат, запуск) и живёт до перезапуска;
в файл `fixtures/recipes_cafe.json` ничего не пишется.

    python tools/mock_server.py --port 8864
    # → вкладка «Рецепты» комнаты cafe
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "recipes_cafe.json"
MAX_FIELDS = 8
MAX_STEPS = 10
FIELD_KINDS = [{"value": "text", "label": "текст"},
               {"value": "file", "label": "файл или папка"},
               {"value": "due", "label": "срок"}]

_STATE: dict | None = None


def _load() -> dict:
    global _STATE
    if _STATE is None:
        try:
            _STATE = json.loads(FIXTURE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STATE = {"items": [], "trash": [], "runs": [], "roles": []}
    return _STATE


def _listing() -> dict:
    state = _load()
    items = copy.deepcopy(state.get("items") or [])
    for item in items:
        item.setdefault("sample", bool(item.get("builtin")))
        item.setdefault("sample_note", "образец чужой сферы" if item.get("builtin") else "")
    return {"items": items,
            "samples": copy.deepcopy(state.get("samples") or []),
            "trash": copy.deepcopy(state.get("trash") or []),
            "runs": sorted(copy.deepcopy(state.get("runs") or []),
                           key=lambda r: -(r.get("at") or 0))[:10],
            "roles": copy.deepcopy(state.get("roles") or []),
            "field_kinds": FIELD_KINDS,
            "max_fields": MAX_FIELDS, "max_steps": MAX_STEPS,
            "dialog": copy.deepcopy(_DIALOG.get("item")),
            "analysis": copy.deepcopy(_ANALYSIS)}


def listing() -> dict:
    return _listing()


#: Разговор о рецепте и лёгкий разбор в демо живут в памяти (W31-C). Руководителя
#: здесь играет сам мок: вопросов он не задаёт, а сразу присылает алгоритм.
_DIALOG: dict = {"item": None}


def _analysis_state() -> dict:
    return {"settings": {"enabled": False, "every_days": 7, "min_tasks": 3, "only_propose": True,
                         "model": {}, "lead_model": {"profile": "team-director", "provider": "ollama-cloud",
                                                     "model": "demo-model", "label": "ollama-cloud · demo-model"},
                         "model_label": "ollama-cloud · demo-model", "models": [], "warning": "",
                         "last_run_at": 0, "next_run_at": 0},
            "estimate": {"input_tokens": 4200, "output_tokens": 900, "chars": 900, "base_tokens": 0,
                         "tasks": 0, "source": "оценка",
                         "text": "≈ 4,2 тыс. токенов на входе и 900 на выходе — один лёгкий проход."},
            "candidates": {"count": 0, "items": []},
            # W31-FIX3: плашка «пора разобрать задачи» — только пометка сторожа,
            # карточку ставит человек кнопкой «Запустить». В демо пометки нет.
            "prompt": {"show": False, "text": "", "count": 0, "tokens": 0, "output_tokens": 0,
                       "due_at": 0, "snoozed_until": 0},
            "offers": [], "dismissed_count": 0, "last_run_at": 0, "waiting": False}


_ANALYSIS = _analysis_state()


def dialog_start(body: dict) -> dict:
    text = str((body or {}).get("text") or "").strip()
    if len(text) < 5:
        raise ValueError("Опишите словами, какое дело повторяется.")
    item = {"id": "dlg-mock01", "text": text, "at": int(time.time()), "updated_at": int(time.time()),
            "stage": "draft", "stage_ru": "алгоритм готов — утвердите", "waiting": False,
            "card_id": "t_mockdlg", "request_id": "", "source": "text", "questions": [],
            "answers": "", "recipe_id": "", "problem": "",
            "model": {"profile": "team-director", "provider": "ollama-cloud", "model": "demo-model",
                      "label": "ollama-cloud · demo-model"},
            "messages": [{"role": "human", "text": text, "at": int(time.time())},
                         {"role": "draft", "text": "Готовый алгоритм (демо)", "at": int(time.time())}],
            "draft": _clean({"name": (text.split(":")[0] or "Повторяющееся дело")[:70],
                             "description": "Шаблон: " + text[:120],
                             "fields": [{"id": "f1", "label": "Что за случай", "kind": "text",
                                         "required": True, "hint": ""}],
                             "steps": [{"id": "s1", "role": "руководитель", "assignee": "",
                                        "title": "Разобрать случай", "instruction": "", "critic": False,
                                        "depends_on": []},
                                       {"id": "s2", "role": "руководитель", "assignee": "",
                                        "title": "Свести итог", "instruction": "", "critic": True,
                                        "depends_on": ["s1"]}]})}
    _DIALOG["item"] = item
    return {"ok": True, "dialog": copy.deepcopy(item)}


def dialog_state() -> dict:
    return {"ok": True, "dialog": copy.deepcopy(_DIALOG.get("item"))}


def dialog_accept() -> dict:
    item = _DIALOG.get("item")
    if not item:
        raise ValueError("Разговор не начат — начните с описания словами.")
    recipe = create(dict(item["draft"], description=item["draft"].get("description") or ""))
    recipe["source"] = "dialog"
    item.update({"stage": "accepted", "stage_ru": "рецепт сохранён", "recipe_id": recipe["id"]})
    return {"ok": True, "recipe": recipe, "dialog": copy.deepcopy(item)}


def dialog_cancel() -> dict:
    item = _DIALOG.get("item")
    if item:
        item.update({"stage": "closed", "stage_ru": "закрыт"})
    return {"ok": True, "dialog": copy.deepcopy(item)}


def analysis_view() -> dict:
    return copy.deepcopy(_ANALYSIS)


def analysis_run() -> dict:
    return {"ok": True, "card_id": "t_mockanalysis", "tasks": 0,
            "message": "Демо: разбор не ходит к модели."}


def analysis_later(days=None) -> dict:
    """«Позже» в демо: только откладывает напоминание, карточку не ставит."""
    num = int(days or _ANALYSIS["settings"].get("every_days") or 7)
    num = max(1, min(90, num))
    _ANALYSIS["prompt"] = {"show": False, "text": "", "count": 0, "tokens": 0,
                           "output_tokens": 0, "due_at": 0,
                           "snoozed_until": int(time.time()) + num * 86400}
    return {"ok": True, "days": num, "snoozed_until": _ANALYSIS["prompt"]["snoozed_until"],
            "message": "Отложили на %d дней — напомним позже." % num}


def analysis_sync() -> dict:
    return {"ok": True, "added": 0, "suppressed": [], "offers": copy.deepcopy(_ANALYSIS["offers"])}


def analysis_settings(body: dict) -> dict:
    body = body if isinstance(body, dict) else {}
    if body.get("every_days") is not None:
        days = int(body["every_days"])
        if not 1 <= days <= 90:
            raise ValueError("«Раз в N дней» — от 1 до 90.")
        _ANALYSIS["settings"]["every_days"] = days
    if body.get("enabled") is not None:
        _ANALYSIS["settings"]["enabled"] = bool(body["enabled"])
    return {"ok": True, "settings": copy.deepcopy(_ANALYSIS["settings"])}


def _find(rid: str) -> tuple[dict | None, str]:
    state = _load()
    for bucket in ("items", "trash"):
        for item in state.get(bucket) or []:
            if str(item.get("id")) == str(rid):
                return item, bucket
    return None, ""


def _clean(body: dict) -> dict:
    body = body if isinstance(body, dict) else {}
    return {"name": str(body.get("name") or "").strip()[:120] or "Без названия",
            "description": str(body.get("description") or "").strip()[:600],
            "fields": copy.deepcopy(body.get("fields") or []),
            "steps": copy.deepcopy(body.get("steps") or []),
            "topic": "any"}


def create(body: dict) -> dict:
    state = _load()
    item = _clean(body)
    item.update({"id": "rc-mock%02d" % (len(state.get("items") or []) + 1),
                 "builtin": False, "source": "user",
                 "created_at": int(time.time()), "updated_at": int(time.time()),
                 "runs": 0, "last_run": 0})
    state.setdefault("items", []).append(item)
    return copy.deepcopy(item)


def patch(rid: str, body: dict) -> dict:
    item, bucket = _find(rid)
    if item is None or bucket != "items":
        raise ValueError("рецепт не найден")
    item.update(_clean(body))
    item.update({"builtin": False, "updated_at": int(time.time())})
    return copy.deepcopy(item)


def remove(rid: str) -> dict:
    state = _load()
    item, bucket = _find(rid)
    if item is None or bucket != "items":
        raise ValueError("рецепт не найден")
    state["items"] = [i for i in state.get("items") or [] if i is not item]
    trash = dict(item)
    trash["removed_at"] = int(time.time())
    state.setdefault("trash", []).insert(0, trash)
    return {"ok": True, "id": rid, "message": "Рецепт в корзине — его можно вернуть."}


def restore(rid: str) -> dict:
    state = _load()
    item, bucket = _find(rid)
    if item is None or bucket != "trash":
        raise ValueError("в корзине такого рецепта нет")
    state["trash"] = [i for i in state.get("trash") or [] if i is not item]
    back = dict(item)
    back.pop("removed_at", None)
    state.setdefault("items", []).append(back)
    return copy.deepcopy(back)


def launch(rid: str, body: dict) -> dict:
    state = _load()
    item, bucket = _find(rid)
    if item is None or bucket != "items":
        raise ValueError("рецепт не найден")
    values = (body or {}).get("values") if isinstance(body, dict) else {}
    values = values if isinstance(values, dict) else {}
    for field_item in item.get("fields") or []:
        if field_item.get("required") and not str(values.get(field_item.get("id")) or "").strip():
            raise ValueError("Заполните поле «%s»." % field_item.get("label"))
    run_id = "run-mock-%d" % (len(state.get("runs") or []) + 1)
    root = "t_mock%04d" % (len(state.get("runs") or []) + 1)
    steps = []
    for index, step in enumerate(item.get("steps") or []):
        steps.append({"id": step.get("id"), "title": step.get("title"),
                      "role": step.get("role"), "assignee": step.get("assignee") or "team-ops",
                      "card_id": "" if index else ("t_mockc%03d" % (len(steps) + 1)),
                      "status": "pending" if index else "created",
                      "depends_on": list(step.get("depends_on") or []), "error": ""})
    run = {"id": run_id, "recipe_id": rid, "recipe_name": item.get("name"),
           "request_id": root, "at": int(time.time()), "state": "running",
           "values": values, "steps": steps}
    state.setdefault("runs", []).insert(0, run)
    item["runs"] = int(item.get("runs") or 0) + 1
    item["last_run"] = run["at"]
    return {"ok": True, "run": copy.deepcopy(run), "request_id": root,
            "created": [s for s in steps if s["card_id"]],
            "warnings": [], "message": "Запущено: запрос и %d карточк(и) шагов."
                                       % len([s for s in steps if s["card_id"]])}


def from_request(body: dict) -> dict:
    state = _load()
    root = str((body or {}).get("request_id") or "").strip()
    if not root:
        raise ValueError("Не указан запрос: нужен request_id.")
    item = _clean({"name": (body or {}).get("name") or "Рецепт из запроса",
                   "description": "Из запроса %s" % root,
                   "fields": [{"id": "what", "label": "Что нужно", "kind": "text",
                               "required": True, "hint": "как в прошлый раз"}],
                   "steps": [{"id": "s1", "role": "руководитель", "assignee": "",
                              "title": "Разобрать случай", "instruction": "",
                              "critic": False, "depends_on": []}]})
    item.update({"id": "rc-mock%02d" % (len(state.get("items") or []) + 1),
                 "builtin": False, "source": "request", "from_request": root,
                 "created_at": int(time.time()), "updated_at": int(time.time()),
                 "runs": 0, "last_run": 0})
    state.setdefault("items", []).append(item)
    return copy.deepcopy(item)
