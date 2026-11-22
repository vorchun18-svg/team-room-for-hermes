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
    return {"items": copy.deepcopy(state.get("items") or []),
            "trash": copy.deepcopy(state.get("trash") or []),
            "runs": sorted(copy.deepcopy(state.get("runs") or []),
                           key=lambda r: -(r.get("at") or 0))[:10],
            "roles": copy.deepcopy(state.get("roles") or []),
            "field_kinds": FIELD_KINDS,
            "max_fields": MAX_FIELDS, "max_steps": MAX_STEPS}


def listing() -> dict:
    return _listing()


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
