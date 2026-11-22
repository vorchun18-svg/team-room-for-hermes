"""Рецепты комнаты — повторяющийся процесс одной кнопкой (W29B, пакет Q1).

Рецепт — это заготовка запроса: имя, описание, поля ввода (текст, файл, срок)
и заранее заданный граф шагов «роль → шаг → критик → моё да». Модель шага берётся
из выбора комнаты (``model_guard.override_for_card`` → тот же путь, что и у всех
карточек), поэтому карточки создаются СРАЗУ с моделью и провайдером, без
переразбора руководителем.

Что здесь:

* хранение: ``data/state/<room>/recipes.json`` (рецепты, корзина) и
  ``data/state/<room>/recipe-runs.json`` (история запусков); запись атомарная
  через ``statefiles``;
* три встроенных нейтральных рецепта (химчистка, грузоперевозки, кофейня) —
  сеются в комнату при первом чтении, правятся и удаляются как обычные;
* ``launch`` — запрос + карточки первого «слоя» графа; шаги, зависящие от
  предыдущих, создаются в ``tick`` (сторож), когда предшественники готовы;
* ``from_request`` — «сохранить запрос как рецепт» по завершённому запросу.

Границы: карточки создаются ТОЛЬКО через ``engine_write.create_request`` и
``engine_write.create_child_card`` (там же модель/провайдер участника).
Реальный CLI Hermes в тестах подменяется песочницей (tests.helpers.Sandbox).
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

try:                                     # пакетный и «плоский» импорт
    from . import statefiles as sf
except ImportError:                      # pragma: no cover
    import statefiles as sf              # type: ignore

RECIPES_FILE = "recipes.json"
RUNS_FILE = "recipe-runs.json"
RECIPES_VERSION = 1

MAX_RECIPES = 60          # больше не помещается на один экран без поиска
MAX_FIELDS = 8
MAX_STEPS = 10
MAX_RUNS = 50
NAME_MIN, NAME_MAX = 3, 120
DESC_MAX = 600
TEXT_MAX = 4000
INSTRUCTION_MAX = 4000
FIELD_KINDS = ("text", "file", "due")
FIELD_KIND_RU = {"text": "текст", "file": "файл или папка", "due": "срок"}
RUN_ACTIVE = ("launching", "running")

#: Ключевые слова темы комнаты → какой встроенный рецепт ей ближе.
TOPICS = {
    "laundry": ("прачечн", "химчист", "стирк", "laundry"),
    "freight": ("груз", "перевоз", "логист", "freight", "доставк"),
    "cafe": ("кофе", "кафе", "cafe", "кухн", "меню"),
}


class RecipeError(Exception):
    """Доменная ошибка рецепта: код API + русский текст (4xx, не 5xx)."""

    def __init__(self, message: str, code: str = "bad_request", http: int = 400,
                 detail: dict | None = None, **extra):
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.http = int(http)
        self.detail = dict(detail or {})
        for key, value in extra.items():
            self.detail.setdefault(key, value)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


# --------------------------------------------------------------- встроенные

def _step(sid: str, role: str, title: str, instruction: str, *, depends=(), critic=False) -> dict:
    return {"id": sid, "role": role, "assignee": "", "title": title,
            "instruction": instruction, "critic": bool(critic),
            "depends_on": list(depends)}


BUILTIN = [
    {
        "id": "builtin-laundry-claim",
        "name": "Ответ клиенту на претензию",
        "topic": "laundry",
        "description": "Разобрать претензию по заказу: факты, позиция, ответ клиенту с решением.",
        "fields": [
            {"id": "client", "label": "Клиент", "kind": "text", "required": True,
             "hint": "как обращаться и по какому заказу"},
            {"id": "claim", "label": "Суть претензии", "kind": "text", "required": True,
             "hint": "что именно не устроило и чего просит клиент"},
            {"id": "due", "label": "Ответить до", "kind": "due", "required": False, "hint": ""},
        ],
        "steps": [
            _step("s1", "юрист", "Проверить требования и сроки",
                  "Проверь, что клиент требует и в какие сроки мы обязаны ответить. "
                  "Назови применимые правила и риски."),
            _step("s2", "оператор", "Собрать факты по заказу",
                  "Собери факты: что было заказано, что выполнено, чеки, переписку, "
                  "кто из сотрудников что делал.", depends=("s1",)),
            _step("s3", "руководитель", "Ответ клиенту и что меняем",
                  "Сведи ответ клиенту: что признаём, что нет, что делаем дальше. "
                  "Коротко, без канцелярита.", depends=("s1", "s2"), critic=True),
        ],
    },
    {
        "id": "builtin-freight-reconcile",
        "name": "Сверка накладных",
        "topic": "freight",
        "description": "Сверить накладные с реестром рейсов и собрать расхождения с решением.",
        "fields": [
            {"id": "folder", "label": "Где накладные", "kind": "file", "required": True,
             "hint": "папка или файл с накладными"},
            {"id": "period", "label": "Период", "kind": "due", "required": False,
             "hint": "с какой даты сверяем"},
        ],
        "steps": [
            _step("s1", "бухгалтер", "Сверить накладные с реестром",
                  "Сверь накладные с реестром рейсов: номер, дата, сумма, подпись. "
                  "Отдельно — чего не хватает."),
            _step("s2", "логист", "Проверить расхождения по рейсам",
                  "Пройди по расхождениям: что реально было, где ошибка в документе, "
                  "где потерялся рейс.", depends=("s1",)),
            _step("s3", "руководитель", "Решение по расхождениям",
                  "Свод расхождений и что делать: исправить документ, доплатить, "
                  "запросить у перевозчика.", depends=("s1", "s2"), critic=True),
        ],
    },
    {
        "id": "builtin-cafe-menu",
        "name": "Меню на неделю",
        "topic": "cafe",
        "description": "Собрать меню на неделю, посчитать закупку и проверить, что всё сходится.",
        "fields": [
            {"id": "season", "label": "Сезон и повод", "kind": "text", "required": False,
             "hint": "например: ноябрь, будни, без праздников"},
            {"id": "limits", "label": "Ограничения", "kind": "text", "required": False,
             "hint": "что не готовим, сколько позиций, бюджет"},
        ],
        "steps": [
            _step("s1", "технолог", "Собрать меню на неделю",
                  "Собери меню на семь дней: блюда, повторы не чаще раза в неделю, "
                  "сезонные продукты."),
            _step("s2", "закупщик", "Посчитать закупку",
                  "Посчитай закупку по меню: позиции, количество, примерная цена. "
                  "Отметь, что заканчивается на складе.", depends=("s1",)),
            _step("s3", "руководитель", "Утвердить меню",
                  "Сверь меню и закупку, скажи, что меняем, и дай короткий план на неделю.",
                  depends=("s1", "s2"), critic=True),
        ],
    },
]


def builtin_for(room: dict) -> list[dict]:
    """Встроенные рецепты для комнаты: по теме комнаты, иначе все три.

    Тему узнаём по id/названию комнаты («химчистка», «грузоперевозки», «кофейня»).
    Комнате без узнаваемой темы показываем все примеры: пустой экран хуже
    лишнего примера, а выбор из трёх — это не шум.
    """
    text = " ".join([str((room or {}).get("id") or ""), str((room or {}).get("title") or ""),
                     str((room or {}).get("description") or "")]).lower()
    matched = [topic for topic, keys in TOPICS.items() if any(k in text for k in keys)]
    out = []
    for item in BUILTIN:
        if not matched or item["topic"] in matched:
            out.append(_copy(item))
    return out


def _copy(value):
    import copy as _copy_mod
    return _copy_mod.deepcopy(value)


# --------------------------------------------------------------- хранилище

def _now() -> int:
    return int(time.time())


def recipes_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / RECIPES_FILE


def runs_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / RUNS_FILE


def _room_id(room) -> str:
    rid = str((room or {}).get("id") or "").strip()
    if not rid:
        raise RecipeError("Не передана комната рецепта.", "bad_request")
    return rid


def _load_doc(cfg, room) -> dict:
    """Прочитать рецепты комнаты (с первым заполнением встроенными)."""
    path = recipes_path(cfg, _room_id(room))
    doc, info = sf.read_json_doc(str(path), default=None)
    state = str((info or {}).get("state") or "")
    if not isinstance(doc, dict) or state != "ok":
        doc = {"version": RECIPES_VERSION, "items": [], "trash": [], "seeded_at": _now()}
        # Нет файла или он битый (statefiles уже отложил копию): показываем
        # встроенные примеры и записываем их — пустой экран хуже лишнего примера.
        doc["items"] = builtin_for(room)
        for item in doc["items"]:
            item.update({"builtin": True, "source": "builtin",
                         "created_at": doc["seeded_at"], "updated_at": doc["seeded_at"]})
        try:
            _save_doc(cfg, room, doc)
        except Exception:                # noqa: BLE001 - чтение не должно падать из-за записи
            pass
    doc.setdefault("items", [])
    doc.setdefault("trash", [])
    return doc


def _save_doc(cfg, room, doc) -> None:
    doc["version"] = RECIPES_VERSION
    sf.write_json_atomic(str(recipes_path(cfg, _room_id(room))), doc)


def _load_runs(cfg, room) -> dict:
    doc, info = sf.read_json_doc(str(runs_path(cfg, _room_id(room))), default=None)
    if not isinstance(doc, dict):
        doc = {"version": RECIPES_VERSION, "items": []}
    doc.setdefault("items", [])
    return doc


def _save_runs(cfg, room, doc) -> None:
    items = doc.get("items") or []
    doc["items"] = items[-MAX_RUNS:]
    doc["version"] = RECIPES_VERSION
    sf.write_json_atomic(str(runs_path(cfg, _room_id(room))), doc)


# --------------------------------------------------------------- проверки

def _one_line(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_fields(raw) -> list[dict]:
    fields = []
    seen = set()
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        item = item if isinstance(item, dict) else {}
        label = _one_line(item.get("label") or item.get("name"), 80)
        if not label:
            raise RecipeError("У поля ввода нет подписи (поле %d)." % (index + 1),
                              field="fields")
        kind = str(item.get("kind") or "text").strip()
        if kind not in FIELD_KINDS:
            raise RecipeError("Неизвестный вид поля «%s» у «%s». Допустимо: %s."
                              % (kind, label, ", ".join(FIELD_KIND_RU.values())),
                              field="fields")
        fid = str(item.get("id") or "").strip() or ("f%d" % (index + 1))
        if fid in seen:
            raise RecipeError("Поле «%s» повторяется." % label, field="fields")
        seen.add(fid)
        fields.append({"id": fid, "label": label, "kind": kind,
                       "required": bool(item.get("required")),
                       "hint": _one_line(item.get("hint"), 160)})
    if len(fields) > MAX_FIELDS:
        raise RecipeError("Полей ввода больше %d — заполнять станет долго." % MAX_FIELDS,
                          field="fields")
    return fields


def _clean_steps(raw, members: list[str]) -> list[dict]:
    steps = []
    seen = set()
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        item = item if isinstance(item, dict) else {}
        title = _one_line(item.get("title"), NAME_MAX)
        if len(title) < 3:
            raise RecipeError("У шага %d нет названия." % (index + 1), field="steps")
        sid = str(item.get("id") or "").strip() or ("s%d" % (index + 1))
        if sid in seen:
            raise RecipeError("Шаг «%s» повторяется." % title, field="steps")
        seen.add(sid)
        role = _one_line(item.get("role"), 80)
        assignee = str(item.get("assignee") or "").strip()
        if assignee and members and assignee not in members:
            raise RecipeError("Исполнитель шага «%s» не участвует в комнате: %s."
                              % (title, assignee), field="steps")
        if not assignee and not role:
            raise RecipeError("У шага «%s» не указана роль или исполнитель." % title,
                              field="steps")
        steps.append({"id": sid, "role": role, "assignee": assignee, "title": title,
                      "instruction": str(item.get("instruction") or "")[:INSTRUCTION_MAX],
                      "critic": bool(item.get("critic")),
                      "depends_on": [str(d).strip() for d in (item.get("depends_on") or [])
                                     if str(d).strip()]})
    if not steps:
        raise RecipeError("В рецепте нет ни одного шага.", field="steps")
    if len(steps) > MAX_STEPS:
        raise RecipeError("Шагов больше %d — рецепт перестаёт быть рецептом." % MAX_STEPS,
                          field="steps")
    ids = [s["id"] for s in steps]
    for step in steps:
        for dep in step["depends_on"]:
            if dep not in ids:
                raise RecipeError("Шаг «%s» ждёт несуществующий шаг «%s»."
                                  % (step["title"], dep), field="steps")
            if dep == step["id"]:
                raise RecipeError("Шаг «%s» ждёт сам себя." % step["title"], field="steps")
    _check_cycles(steps)
    return steps


def _check_cycles(steps: list[dict]) -> None:
    graph = {s["id"]: list(s["depends_on"]) for s in steps}
    state: dict[str, int] = {}

    def walk(node: str) -> None:
        if state.get(node) == 1:
            raise RecipeError("Шаги ссылаются друг на друга по кругу.", field="steps")
        if state.get(node) == 2:
            return
        state[node] = 1
        for dep in graph.get(node, []):
            walk(dep)
        state[node] = 2

    for sid in graph:
        walk(sid)


def _members(room) -> list[str]:
    out = []
    for member in (room or {}).get("members") or []:
        if isinstance(member, dict) and member.get("profile"):
            out.append(str(member["profile"]))
    return out


def _check_recipe(data: dict, room) -> dict:
    """Проверить рецепт из интерфейса и вернуть нормализованную форму."""
    data = data if isinstance(data, dict) else {}
    name = _one_line(data.get("name") or data.get("title"), NAME_MAX)
    if len(name) < NAME_MIN:
        raise RecipeError("Название рецепта — от %d знаков." % NAME_MIN, field="name")
    return {"name": name,
            "description": _one_line(data.get("description"), DESC_MAX),
            "fields": _clean_fields(data.get("fields")),
            "steps": _clean_steps(data.get("steps"), _members(room)),
            "topic": str(data.get("topic") or "any")[:24]}


def _new_id(name: str) -> str:
    digest = hashlib.sha1(("%s|%s|%s" % (name, _now(), id(name))).encode("utf-8")).hexdigest()
    return "rc-" + digest[:8]


def _find(doc: dict, recipe_id: str) -> tuple[dict | None, str]:
    rid = str(recipe_id or "").strip()
    for bucket in ("items", "trash"):
        for item in doc.get(bucket) or []:
            if str(item.get("id") or "") == rid:
                return item, bucket
    return None, ""


# --------------------------------------------------------------- роли и модель

def _role_words(room) -> list[dict]:
    """Участники комнаты с подписью-ролью (для сопоставления «юрист» → профиль)."""
    out = []
    for member in (room or {}).get("members") or []:
        if not isinstance(member, dict) or not member.get("profile"):
            continue
        note = str(member.get("note") or member.get("label") or "")
        out.append({"profile": str(member["profile"]),
                    "role": str(member.get("role") or "worker"),
                    "words": (note + " " + str(member.get("label") or "")).lower()})
    return out


def resolve_role(room, role: str) -> str:
    """Профиль комнаты под роль шага («юрист», «руководитель»).

    Сопоставление — по подписи участника (там написано «Юрист: …»), затем по
    роли карточки. Если совпадения нет — первый работник комнаты с пояснением
    в предупреждении запуска. Пустая комната — ошибка, а не молчаливый запуск.
    """
    role_text = _one_line(role, 80).lower()
    people = _role_words(room)
    if not people:
        raise RecipeError("В комнате нет участников — рецепту некого назначить шаг.",
                          "conflict", 409)
    lead = str((room or {}).get("orchestrator") or "").strip()
    if role_text in ("руководитель", "директор", "leader", "lead", "оркестратор"):
        if lead and any(p["profile"] == lead for p in people):
            return lead
        for person in people:
            if person["role"] in ("orchestrator", "leader"):
                return person["profile"]
    for person in people:
        if role_text and role_text in person["words"]:
            return person["profile"]
    for person in people:
        if person["role"] == "worker":
            return person["profile"]
    return people[0]["profile"]


def room_roles(room) -> list[dict]:
    """Короткий список ролей комнаты для подсказки в редакторе рецепта."""
    out = []
    for person in _role_words(room):
        out.append({"profile": person["profile"], "role": person["role"],
                    "label": person["words"].strip()[:80] or person["role"]})
    return out


# --------------------------------------------------------------- чтение

def _public(item: dict, runs: list[dict]) -> dict:
    rid = str(item.get("id") or "")
    mine = [r for r in runs if str(r.get("recipe_id") or "") == rid]
    return {"id": rid, "name": item.get("name") or "", "description": item.get("description") or "",
            "fields": _copy(item.get("fields") or []), "steps": _copy(item.get("steps") or []),
            "topic": item.get("topic") or "any", "builtin": bool(item.get("builtin")),
            "source": item.get("source") or "user",
            "created_at": item.get("created_at") or 0, "updated_at": item.get("updated_at") or 0,
            "runs": len(mine), "last_run": max((r.get("at") or 0 for r in mine), default=0)}


def _run_public(run: dict) -> dict:
    steps = run.get("steps") or []
    return {"id": run.get("id") or "", "recipe_id": run.get("recipe_id") or "",
            "recipe_name": run.get("recipe_name") or "", "request_id": run.get("request_id") or "",
            "at": run.get("at") or 0, "state": run.get("state") or "launching",
            "values": _copy(run.get("values") or {}),
            "steps": [{"id": s.get("step_id") or "", "title": s.get("title") or "",
                       "assignee": s.get("assignee") or "", "role": s.get("role") or "",
                       "card_id": s.get("card_id") or "", "status": s.get("status") or "pending",
                       "depends_on": list(s.get("depends_on") or []),
                       "error": s.get("error") or ""} for s in steps]}


def listing(cfg, room) -> dict:
    """Все рецепты комнаты, корзина, история запусков и роли — одним ответом."""
    doc = _load_doc(cfg, room)
    runs = _load_runs(cfg, room).get("items") or []
    items = [_public(item, runs) for item in doc.get("items") or []]
    items.sort(key=lambda x: (-int(x["builtin"]), -int(x["last_run"] or 0), x["name"].lower()))
    return {"items": items,
            "trash": [_public(item, runs) for item in doc.get("trash") or []],
            "runs": [_run_public(r) for r in sorted(runs, key=lambda r: -(r.get("at") or 0))[:10]],
            "roles": room_roles(room),
            "field_kinds": [{"value": k, "label": FIELD_KIND_RU[k]} for k in FIELD_KINDS],
            "max_fields": MAX_FIELDS, "max_steps": MAX_STEPS}


def get(cfg, room, recipe_id: str) -> dict:
    doc = _load_doc(cfg, room)
    item, _bucket = _find(doc, recipe_id)
    if item is None:
        raise RecipeError("Рецепт не найден.", "not_found", 404,
                          {"recipe_id": str(recipe_id or "")})
    return _public(item, _load_runs(cfg, room).get("items") or [])


# --------------------------------------------------------------- правка

def save(cfg, room, data: dict, recipe_id: str | None = None, actor: str = "mikhail") -> dict:
    """Создать рецепт или изменить свой; встроенный пример при правке становится копией."""
    clean = _check_recipe(data, room)
    doc = _load_doc(cfg, room)
    now = _now()
    if recipe_id:
        item, bucket = _find(doc, recipe_id)
        if item is None or bucket != "items":
            raise RecipeError("Рецепт не найден — возможно, он уже в корзине.",
                              "not_found", 404, {"recipe_id": str(recipe_id)})
        item.update(clean)
        item.update({"updated_at": now, "builtin": False,
                     "source": item.get("source") if item.get("source") in ("user", "request")
                     else "user"})
        saved = item
    else:
        if len(doc.get("items") or []) >= MAX_RECIPES:
            raise RecipeError("Рецептов уже %d — сначала уберите лишние." % MAX_RECIPES,
                              "conflict", 409)
        saved = dict(clean)
        saved.update({"id": _new_id(clean["name"]), "builtin": False, "source": "user",
                      "created_at": now, "updated_at": now})
        doc.setdefault("items", []).append(saved)
    _save_doc(cfg, room, doc)
    return _public(saved, _load_runs(cfg, room).get("items") or [])


def remove(cfg, room, recipe_id: str) -> dict:
    """В корзину (без удаления): рецепт можно вернуть."""
    doc = _load_doc(cfg, room)
    item, bucket = _find(doc, recipe_id)
    if item is None or bucket != "items":
        raise RecipeError("Рецепт не найден.", "not_found", 404,
                          {"recipe_id": str(recipe_id)})
    doc["items"] = [i for i in doc["items"] if i is not item]
    item = dict(item)
    item["removed_at"] = _now()
    doc.setdefault("trash", []).insert(0, item)
    doc["trash"] = doc["trash"][:MAX_RECIPES]
    _save_doc(cfg, room, doc)
    return {"ok": True, "id": item.get("id"), "message": "Рецепт в корзине — его можно вернуть."}


def restore(cfg, room, recipe_id: str) -> dict:
    doc = _load_doc(cfg, room)
    item, bucket = _find(doc, recipe_id)
    if item is None or bucket != "trash":
        raise RecipeError("В корзине такого рецепта нет.", "not_found", 404,
                          {"recipe_id": str(recipe_id)})
    doc["trash"] = [i for i in doc["trash"] if i is not item]
    item = dict(item)
    item.pop("removed_at", None)
    item["updated_at"] = _now()
    doc.setdefault("items", []).append(item)
    _save_doc(cfg, room, doc)
    return _public(item, _load_runs(cfg, room).get("items") or [])


# --------------------------------------------------------------- запуск

def _validate_values(recipe: dict, values) -> dict:
    """Проверить заполненные поля; вернуть {id: строка} без пустых."""
    values = values if isinstance(values, dict) else {}
    out = {}
    for field_item in recipe.get("fields") or []:
        fid = str(field_item.get("id") or "")
        raw = values.get(fid)
        text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        text = text.strip()
        if len(text) > TEXT_MAX:
            raise RecipeError("Поле «%s» длиннее %d знаков." % (field_item.get("label"), TEXT_MAX),
                              field=fid)
        if field_item.get("required") and not text:
            raise RecipeError("Заполните поле «%s»." % field_item.get("label"),
                              "bad_request", 400, {"field": fid})
        if text and field_item.get("kind") == "due":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                raise RecipeError("Срок «%s» — в виде ГГГГ-ММ-ДД." % field_item.get("label"),
                                  field=fid)
        if text and field_item.get("kind") == "file":
            if not Path(text).exists():
                raise RecipeError("Файл или папка «%s» не найдены: %s"
                                  % (field_item.get("label"), text[:200]),
                                  "bad_request", 400, {"field": fid, "path": text[:400]})
        out[fid] = text
    return out


def _plan_body(recipe: dict, values: dict, steps: list[dict]) -> str:
    """Тело запроса: поля человека + готовый граф шагов (разбирать не надо)."""
    lines = ["Рецепт: %s" % recipe.get("name")]
    if recipe.get("description"):
        lines += ["", str(recipe["description"]).strip()]
    fields = recipe.get("fields") or []
    if fields:
        lines += ["", "ЧТО УКАЗАЛ ЧЕЛОВЕК"]
        for field_item in fields:
            value = values.get(str(field_item.get("id") or "")) or "—"
            lines.append("• %s: %s" % (field_item.get("label"), value))
    lines += ["", "ПЛАН ГОТОВ — РАЗБИРАТЬ НЕ НУЖНО",
              "Шаги ниже уже созданы отдельными карточками. Не переразбивай запрос, "
              "не заводи новых подзадач: работай по шагам и опирайся на их результат.",
              ""]
    order = {s["id"]: i + 1 for i, s in enumerate(steps)}
    for step in steps:
        deps = ", ".join("шаг %d" % order[d] for d in step.get("depends_on") or [] if d in order)
        lines.append("%d. %s — %s%s%s" % (
            order[step["id"]], (step.get("role") or step.get("assignee") or "участник"),
            step.get("title"), (" (зависит: %s)" % deps) if deps else "",
            " · проверяет критик" if step.get("critic") else ""))
        if step.get("instruction"):
            lines.append("   Что сделать: %s" % _one_line(step["instruction"], 400))
    lines += ["", "Граф шагов: %s" % " → ".join(
        "%s%s" % (s.get("title"), (" [жди %s]" % ", ".join(s.get("depends_on") or []))
                  if s.get("depends_on") else "") for s in steps),
        "", "Правило комнаты: каждый шаг делает только свой исполнитель; "
            "критик проверяет итог перед ответом человеку."]
    return "\n".join(lines).strip() + "\n"


def _engine():
    try:
        from . import engine_write as mod
    except ImportError:                  # pragma: no cover - плоский импорт
        import engine_write as mod        # type: ignore
    return mod


def _db():
    try:
        from . import engine_db as mod
    except ImportError:                  # pragma: no cover
        import engine_db as mod          # type: ignore
    return mod


def _card_status(cfg, board: str, card_id: str) -> str:
    if not card_id:
        return ""
    try:
        dbmod = _db()
        with dbmod._conn(cfg, board) as con:
            rows = dbmod._task_rows(con, [card_id])
        return str((rows.get(card_id) or {}).get("status") or "")
    except Exception:                    # noqa: BLE001 - статус нужен для подсказки, не для отказа
        return ""


def _create_step_card(cfg, room, board: str, root_id: str, run_id: str, step: dict,
                      assignee: str) -> str:
    """Создать карточку шага общим путём (модель/провайдер комнаты внутри)."""
    title = "Шаг: %s" % step.get("title")
    body_lines = ["Шаг рецепта «%s»." % step.get("title")]
    if step.get("instruction"):
        body_lines += ["", str(step["instruction"]).strip()]
    if step.get("depends_on"):
        body_lines += ["", "Опирайся на результат предыдущих шагов этого запроса "
                           "(они уже в работе или готовы)."]
    body_lines += ["", "Не переразбивай запрос и не создавай новых подзадач: "
                       "сделай только этот шаг и оставь итог в ответе."]
    return _engine().create_child_card(
        cfg, room, board, root_id, assignee, title, "\n".join(body_lines),
        "recipe_step", idem_key="recipe:%s:%s" % (run_id, step.get("id") or ""))


def _resolved_steps(recipe: dict, room) -> list[dict]:
    steps = []
    warnings = []
    for step in recipe.get("steps") or []:
        assignee = str(step.get("assignee") or "").strip()
        if not assignee:
            assignee = resolve_role(room, step.get("role") or "")
            matched = False
            role_text = str(step.get("role") or "").lower()
            for person in _role_words(room):
                if role_text and role_text in person["words"]:
                    matched = True
                    break
            if not matched:
                warnings.append("Роль «%s» не нашлась в комнате — шаг «%s» отдан %s."
                                % (step.get("role") or "—", step.get("title"), assignee))
        item = dict(step)
        item["assignee"] = assignee
        steps.append(item)
    return steps, warnings


def _ready_now(steps: list[dict], done: set) -> list[dict]:
    return [s for s in steps
            if str(s.get("id") or "") not in done
            and all(dep in done for dep in (s.get("depends_on") or []))]


def launch(cfg, room, recipe_id: str, values, actor: str = "mikhail") -> dict:
    """Запустить рецепт: запрос + карточки первого слоя; остальное — в ``tick``."""
    doc = _load_doc(cfg, room)
    recipe, bucket = _find(doc, recipe_id)
    if recipe is None or bucket != "items":
        raise RecipeError("Рецепт не найден.", "not_found", 404,
                          {"recipe_id": str(recipe_id)})
    board = str((room or {}).get("board") or "").strip()
    if not board:
        raise RecipeError("У комнаты не указана доска канбана.", "conflict", 409)
    clean_values = _validate_values(recipe, values)
    steps, warnings = _resolved_steps(recipe, room)
    root_title = "%s: %s" % (recipe.get("name"),
                             ", ".join(v for v in list(clean_values.values())[:2] if v))
    body = _plan_body(recipe, clean_values, steps)
    run_id = "run-%s-%s" % (recipe.get("id") or "rc", hashlib.sha1(
        ("%s|%s" % (recipe.get("id"), _now())).encode("utf-8")).hexdigest()[:6])
    slug = "recipe-%s-%s" % (recipe.get("id") or "rc", run_id[-6:])
    root = _engine().create_request(cfg, room, slug, root_title.strip()[:NAME_MAX], body)
    root_id = str(root.get("root_id") or "")
    if not root_id:
        raise RecipeError("Канбан не создал запрос — рецепт не запущен.", "engine_error", 500)

    run = {"id": run_id, "recipe_id": recipe.get("id"), "recipe_name": recipe.get("name"),
           "room_id": (room or {}).get("id"), "request_id": root_id, "at": _now(),
           "actor": actor, "state": "running", "values": clean_values,
           "warnings": warnings,
           "steps": [{"step_id": s.get("id"), "title": s.get("title"),
                      "role": s.get("role"), "assignee": s.get("assignee"), "card_id": "",
                      "status": "pending", "depends_on": list(s.get("depends_on") or []),
                      "error": ""} for s in steps]}
    created = _create_wave(cfg, room, board, root_id, run, steps, done=set())
    if not created:
        run["state"] = "failed"
        _remember_run(cfg, room, run)
        raise RecipeError("Запрос создан, но ни одна карточка шага не создалась. "
                          "Проверьте доску и профили участников.",
                          "engine_error", 502, {"request_id": root_id})
    _remember_run(cfg, room, run)
    return {"ok": True, "run": _run_public(run), "request_id": root_id,
            "created": created, "warnings": warnings,
            "message": "Запущено: запрос и %d карточк(и) шагов." % len(created)}


def _create_wave(cfg, room, board: str, root_id: str, run: dict, steps: list[dict],
                 done: set) -> list[str]:
    """Создать карточки шагов, чьи зависимости готовы; отметить статусы в запуске."""
    by_id = {s.get("step_id"): s for s in run.get("steps") or []}
    created = []
    for step in _ready_now(steps, done):
        row = by_id.get(step.get("id"))
        if row is None or row.get("card_id"):
            continue
        try:
            card_id = _create_step_card(cfg, room, board, root_id, run["id"], step,
                                        str(step.get("assignee") or ""))
        except Exception as exc:          # noqa: BLE001 - один шаг не должен терять остальные
            row["status"] = "failed"
            row["error"] = _one_line(exc, 300)
            continue
        row["card_id"] = card_id
        row["status"] = "created"
        created.append({"step_id": step.get("id"), "title": step.get("title"),
                        "assignee": step.get("assignee"), "card_id": card_id})
    return created


def _remember_run(cfg, room, run: dict) -> None:
    doc = _load_runs(cfg, room)
    items = [r for r in doc.get("items") or [] if r.get("id") != run.get("id")]
    items.append(run)
    doc["items"] = items[-MAX_RUNS:]
    _save_runs(cfg, room, doc)


def tick(cfg, room) -> dict:
    """Продвинуть незавершённые запуски: создать шаги, чьи предшественники готовы.

    Вызывается сторожем на каждом проходе (как ``plan.tick``). Ничего не делает,
    если нет запусков в работе; любая ошибка одного запуска не ломает остальные.
    """
    doc = _load_runs(cfg, room)
    items = doc.get("items") or []
    if not any(r.get("state") in RUN_ACTIVE for r in items):
        return {"runs": 0, "created": 0}
    board = str((room or {}).get("board") or "").strip()
    created = 0
    dirty = False
    for run in items:
        if run.get("state") not in RUN_ACTIVE or not run.get("request_id"):
            continue
        root_status = _card_status(cfg, board, str(run.get("request_id") or ""))
        if root_status == "archived":
            run["state"] = "archived"
            dirty = True
            continue
        statuses = {}
        for row in run.get("steps") or []:
            if row.get("card_id"):
                status = _card_status(cfg, board, row["card_id"])
                if status:
                    statuses[row["step_id"]] = status
                    before = row.get("status")
                    if status == "done":
                        row["status"] = "done"
                    elif status == "blocked":
                        row["status"] = "blocked"
                    elif status in ("running", "review"):
                        row["status"] = "running"
                    if row.get("status") != before:
                        dirty = True
        done = {sid for sid, status in statuses.items() if status in ("done", "archived")}
        fresh = _create_wave(cfg, room, board, str(run.get("request_id") or ""),
                             run, _run_steps(run), done)
        created += len(fresh)
        if fresh:
            run["state"] = "running"
            dirty = True
        steps = run.get("steps") or []
        if steps and all(r.get("status") in ("done", "failed") for r in steps):
            new_state = "done" if any(r.get("status") == "done" for r in steps) else "failed"
            if run.get("state") != new_state:
                run["state"] = new_state
                dirty = True
    if dirty:
        _save_runs(cfg, room, {"version": RECIPES_VERSION, "items": items})
    return {"runs": len([r for r in items if r.get("state") in RUN_ACTIVE]),
            "created": created, "changed": dirty}


def _run_steps(run: dict) -> list[dict]:
    """Шаги запуска в форме, которую понимает ``_create_wave``."""
    return [{"id": s.get("step_id"), "title": s.get("title"), "role": s.get("role"),
             "assignee": s.get("assignee"), "depends_on": list(s.get("depends_on") or [])}
            for s in run.get("steps") or []]


# --------------------------------------------------------------- из запроса

def from_request(cfg, room, root_id: str, name: str = "", actor: str = "mikhail") -> dict:
    """Сохранить завершённый запрос как рецепт: шаги = карточки запроса по порядку."""
    root_id = str(root_id or "").strip()
    if not root_id:
        raise RecipeError("Не указан запрос.", field="request_id")
    board = str((room or {}).get("board") or "").strip()
    dbmod = _db()
    try:
        with dbmod._conn(cfg, board) as con:
            rows = dbmod._task_rows(con, [root_id])
            if root_id not in rows:
                raise RecipeError("Запрос не найден на доске комнаты.", "not_found", 404,
                                  {"request_id": root_id})
            links = con.execute("SELECT child_id FROM task_links WHERE parent_id = ?",
                                (root_id,)).fetchall()
            child_ids = [str(r[0]) for r in links]
            children = dbmod._task_rows(con, child_ids)
    except RecipeError:
        raise
    except Exception as exc:             # noqa: BLE001
        raise RecipeError("Не удалось прочитать запрос: %s" % _one_line(exc, 200),
                          "engine_error", 503) from exc
    root = rows.get(root_id) or {}
    ordered = sorted(((children.get(cid) or {}) for cid in child_ids),
                     key=lambda c: (c.get("created_at") or 0, str(c.get("id") or "")))
    role_of = {p["profile"]: (p["words"].strip()[:60] or p["role"]) for p in _role_words(room)}
    steps = []
    for index, card in enumerate([c for c in ordered if c.get("id") and not c.get("parent_missing")]):
        if not card.get("title"):
            continue
        assignee = str(card.get("assignee") or "").strip()
        steps.append({"id": "s%d" % (index + 1), "role": role_of.get(assignee, ""),
                      "assignee": assignee, "title": _one_line(card.get("title"), NAME_MAX),
                      "instruction": _one_line(card.get("body"), INSTRUCTION_MAX),
                      "critic": bool(assignee) and assignee == str((room or {}).get("critic") or ""),
                      "depends_on": ["s%d" % index] if index else []})
    if not steps:
        raise RecipeError("В запросе нет карточек-шагов — сохранять нечего. "
                          "Сначала запрос должен быть разобран на шаги.", "conflict", 409,
                          {"request_id": root_id})
    data = {"name": _one_line(name, NAME_MAX) or _one_line(root.get("title"), NAME_MAX),
            "description": "Из запроса: %s" % _one_line(root.get("title"), 200),
            "fields": [{"id": "what", "label": "Что нужно", "kind": "text", "required": True,
                        "hint": "как в прошлый раз, но по новому случаю"}],
            "steps": steps, "topic": "any"}
    saved = save(cfg, room, data, actor=actor)
    saved["source"] = "request"
    doc = _load_doc(cfg, room)
    for item in doc.get("items") or []:
        if item.get("id") == saved["id"]:
            item["source"] = "request"
            item["from_request"] = root_id
    _save_doc(cfg, room, doc)
    return saved
