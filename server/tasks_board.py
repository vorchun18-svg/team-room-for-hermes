"""Канбан стадий задачи комнаты (W30-D1): «Новая → … → Принята».

Контракт §3 (``docs/WAVE30-PACKAGES.md``)::

    GET /api/rooms/{room}/tasks
    → {"tasks":[{id,title,stage,status,assignee,steps_done,steps_total,
                 needs_you:{count,blocking},accepted,parent_id,children_count,
                 updated_at,summary_short}],
       "columns":["new","working","needs_you","review","done_system","accepted"],
       "counts":{…}}

Стадия считается из того, что уже есть: статус корня на доске, флаг принятия
(``server/passports.py``), открытые пункты «ждёт вас» (тот же расчёт, что на
странице запроса) и связи «задача → подзадачи» из ``data/state/<room>/projects.json``.
Моделей не зовёт и ничего не пишет: это чистый слой чтения, поэтому его можно
тестировать на словарях.

Почему ``failed`` — это «Ждёт вас»: упавшая задача не идёт сама, у неё есть
кнопка «Повторить». Отдельной колонки «Ошибка» в контракте нет, и «без тупиков»
важнее косметики: карточка попадает туда, где человек её увидит и нажмёт.
"""
from __future__ import annotations

import time
from pathlib import Path

try:  # пакетный и «плоский» импорт
    from . import statefiles as _sf
except ImportError:  # pragma: no cover
    import statefiles as _sf        # type: ignore

#: Стадии в порядке колонок доски (контракт §3). ``archived`` — вне колонок.
STAGES = ("new", "working", "needs_you", "review", "done_system", "accepted")
COLUMNS = list(STAGES)
ARCHIVED = "archived"
#: Русские подписи (клиент их и рисует; сервер отдаёт id — один словарь на всех).
STAGE_RU = {"new": "Новая", "working": "В работе", "needs_you": "Ждёт вас",
            "review": "На проверке", "done_system": "Готово (по мнению системы)",
            "accepted": "Принята", "archived": "В архиве"}
#: Что значит стадия — для пустых колонок («зачем экран + что делать»).
STAGE_HINTS = {
    "new": "Задача поставлена и ждёт, пока руководитель её разложит.",
    "working": "Агенты работают: идут карточки, свод собирается.",
    "needs_you": "Работа стоит или спрашивает — от вас нужно слово или кнопка.",
    "review": "Критик или руководитель проверяет итог.",
    "done_system": "Система считает работу законченной — проверьте и примите.",
    "accepted": "Принято вами: заморожено, итог в памяти комнаты.",
    "archived": "Убрано как неактуальное (раздел «Архив»).",
}
#: Статусы доски → стадия (до поправок на принятие и открытые пункты).
STATUS_STAGE = {
    "triage": "new", "queued": "new", "todo": "new", "ready": "new",
    "running": "working", "working": "working", "scheduled": "new",
    "review": "review", "reviewing": "review", "synthesizing": "review",
    "done": "done_system",
    "needs_you": "needs_you", "blocked": "needs_you", "failed": "needs_you",
    "archived": ARCHIVED,
}
#: Файл связей «задача → подзадачи» (пишет пакет B, читаем терпимо).
PROJECTS_FILE = "projects.json"
SUMMARY_SHORT_MAX = 200


def stage_of(status, *, accepted: bool = False, archived: bool = False,
             blocking: int = 0) -> str:
    """Стадия задачи по статусу, принятию и числу БЛОКИРУЮЩИХ пунктов.

    Порядок важен: архив и принятие — полная заморозка, они выше любых статусов;
    блокирующий пункт — выше «Готово» (работа правда ждёт человека); «Ждёт вас»
    без блокирующих пунктов ставится только по статусу доски.
    """
    if archived or str(status or "").strip().lower() == "archived":
        return ARCHIVED
    if accepted:
        return "accepted"
    if int(blocking or 0) > 0:
        return "needs_you"
    return STATUS_STAGE.get(str(status or "").strip().lower(), "new")


def links(cfg, room_id) -> dict:
    """``{root_id: {parent, children[]}}`` из ``data/state/<room>/projects.json``.

    Файл пишет пакет «проект → подзадачи» (B). Читаем терпимо: нет файла, битый
    JSON, чужая форма — «связей нет», а не ошибка страницы.
    """
    room_id = str(room_id or "")
    if not room_id:
        return {}
    base = Path(getattr(cfg, "state_dir", Path(getattr(cfg, "data_dir", ".")) / "state"))
    data, _info = _sf.read_json_doc(base / room_id / PROJECTS_FILE, None)
    raw = data if isinstance(data, dict) else {}
    if isinstance(raw.get("projects"), dict):
        raw = raw["projects"]
    out: dict = {}
    for root_id, value in (raw or {}).items():
        if not isinstance(value, dict):
            continue
        children = [str(c) for c in (value.get("children") or []) if c]
        parent = str(value.get("parent") or "") or None
        out[str(root_id)] = {"parent": parent, "children": children}
    return out


def _one(value, limit: int = SUMMARY_SHORT_MAX) -> str:
    text = " ".join(str(value or "").split())
    if limit and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def summary_short(summary: dict, rec: dict) -> str:
    """Короткая строка для карточки: чем закончилось / на чём стоит."""
    summary = summary if isinstance(summary, dict) else {}
    rec = rec if isinstance(rec, dict) else {}
    for value in (summary.get("status_reason"), summary.get("preview"),
                  rec.get("summary"), rec.get("text")):
        text = _one(value)
        if text:
            return text
    return ""


def row(rec: dict, summary: dict, *, status: str = "", accepted: bool = False,
        archived: bool = False, waiting=None, parent_id=None, children=None,
        updated_at=None) -> dict:
    """Одна строка контракта §3 для карточки задачи."""
    rec = rec if isinstance(rec, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    items = [item for item in (waiting or []) if isinstance(item, dict)]
    blocking = sum(1 for item in items if item.get("blocking"))
    # W30-QA1: принятая задача — полная заморозка, вопросов у неё больше нет.
    # Пункты «ждёт вас» из метаданных карточки — след уже закрытой работы; если
    # показать их, карточка в «Принята» спорит с заморозкой («ждёт вас: 3 вопроса»).
    if accepted:
        items, blocking = [], 0
    root_id = str(rec.get("root_id") or summary.get("id") or "")
    progress = summary.get("progress") if isinstance(summary.get("progress"), dict) else {}
    status = str(status or summary.get("status") or "")
    kids = [str(c) for c in (children or []) if c]
    if updated_at is None:
        updated_at = (summary.get("finished_at") or summary.get("started_at")
                      or rec.get("created_at") or 0)
    return {
        "id": root_id,
        "title": _one(summary.get("title") or rec.get("title") or rec.get("text"), 200),
        "stage": stage_of(status, accepted=accepted, archived=archived, blocking=blocking),
        "status": status,
        "assignee": summary.get("assignee") or rec.get("assignee"),
        "steps_done": int(progress.get("done") or 0),
        "steps_total": int(progress.get("total") or 0),
        "needs_you": {"count": len(items), "blocking": blocking},
        "accepted": bool(accepted),
        "parent_id": parent_id or None,
        "children_count": len(kids),
        "updated_at": int(updated_at or 0),
        "summary_short": summary_short(summary, rec),
    }


def build_rows(records, *, states=None, accepted=None, waiting=None, links_map=None,
               summary_of=None) -> list:
    """Строки доски по записям реестра комнаты.

    ``states`` — ``{root_id: {status, accepted?}}`` с доски; ``accepted`` — набор
    id принятых задач; ``waiting`` — ``{root_id: [{kind, id, text, blocking}]}``
    (тот же расчёт, что на странице запроса); ``links_map`` — ``links()``;
    ``summary_of(rec)`` — сводка запроса (может бросить: тогда локальная).
    """
    states = states if isinstance(states, dict) else {}
    accepted = {str(i) for i in (accepted or set())}
    waiting = waiting if isinstance(waiting, dict) else {}
    links_map = links_map if isinstance(links_map, dict) else {}
    rows: list = []
    for rec in records or []:
        if not isinstance(rec, dict) or not rec.get("root_id"):
            continue
        root_id = str(rec["root_id"])
        state = states.get(root_id) if isinstance(states.get(root_id), dict) else {}
        summary: dict = {}
        if callable(summary_of):
            try:
                summary = summary_of(rec) or {}
            except Exception:  # noqa: BLE001 - одна карточка не валит доску
                summary = {}
        if not isinstance(summary, dict):
            summary = {}
        # Стадию задачи даёт СТАТУС ЗАПРОСА (он вычисляется из дерева: идущая
        # работа, критик, свод), а статус корневой карточки — запасной путь.
        # У «в работе» корень бывает `todo`, пока дети `running`: по карточке
        # такая задача выглядела бы «Новой».
        status = str(summary.get("status") or state.get("status") or "")
        archived = (bool(summary.get("archived")) or status == "archived"
                    or str(state.get("status") or "") == "archived")
        link = links_map.get(root_id) if isinstance(links_map.get(root_id), dict) else {}
        rows.append(row(rec, summary, status=status, accepted=root_id in accepted,
                        archived=archived,
                        waiting=waiting.get(root_id) or [],
                        parent_id=link.get("parent"), children=link.get("children")))
    rows.sort(key=lambda item: (-int(item.get("updated_at") or 0),
                                ARCHIVED == item.get("stage"), str(item.get("id") or "")))
    return rows


def counts(rows) -> dict:
    """Счётчики по колонкам + архив. Число в колонке = числу карточек в ней."""
    out = {stage: 0 for stage in COLUMNS}
    out[ARCHIVED] = 0
    out["total"] = 0
    for item in rows or []:
        stage = str((item or {}).get("stage") or "")
        if stage in out:
            out[stage] += 1
        out["total"] += 1          # всего задач, включая неизвестную стадию
    return out


def payload(room_id: str, rows, *, now=None) -> dict:
    """Тело ответа ``GET /api/rooms/{room}/tasks`` (контракт §3, буква в букву)."""
    return {"tasks": list(rows or []), "columns": list(COLUMNS),
            "counts": counts(rows), "room": str(room_id or ""),
            "labels": dict(STAGE_RU), "hints": dict(STAGE_HINTS),
            "at": int(now or time.time())}


__all__ = ["STAGES", "COLUMNS", "ARCHIVED", "STAGE_RU", "STAGE_HINTS", "STATUS_STAGE",
           "PROJECTS_FILE", "stage_of", "links", "summary_short", "row", "build_rows",
           "counts", "payload"]
