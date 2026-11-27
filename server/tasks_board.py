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

#: Колонки доски в порядке показа (контракт W31). ``archived`` — вне колонок.
COLUMNS = ["new", "working", "review", "done"]
#: Прежнее имя (пакеты волны 30 ещё импортируют STAGES).
STAGES = tuple(COLUMNS)
ARCHIVED = "archived"
#: Русские подписи (клиент их и рисует; сервер отдаёт id — один словарь на всех).
STAGE_RU = {"new": "Новая", "working": "В работе", "review": "На проверке",
            "done": "Готово", "archived": "В архиве"}
#: Что значит колонка — для пустых колонок («зачем экран + что делать»).
STAGE_HINTS = {
    "new": "Задача поставлена и ждёт, пока руководитель её разложит.",
    "working": "Агенты работают. Метка «Ждёт вас» — есть открытые вопросы.",
    "review": "Работа закончена или проверяется — посмотрите и примите.",
    "done": "Принято вами: заморожено, итог в памяти комнаты.",
    "archived": "Убрано как неактуальное (раздел «Архив»).",
}
#: Статусы доски → колонка (до поправок на принятие и открытые пункты).
STATUS_COLUMN = {
    "triage": "new", "queued": "new", "todo": "new", "ready": "new", "scheduled": "new",
    "running": "working", "working": "working",
    "needs_you": "working", "blocked": "working", "failed": "working",
    "review": "review", "reviewing": "review", "synthesizing": "review",
    "done": "review",
    "archived": ARCHIVED,
}
#: Прежнее имя того же словаря (его зовут пакеты волны 30 и их тесты).
STATUS_STAGE = STATUS_COLUMN
#: Файл связей «задача → подзадачи» (пишет пакет B, читаем терпимо).
PROJECTS_FILE = "projects.json"
#: Файл «возвращена в работу» (W31-A1): принятую задачу человек вернул в работу,
#: а на доске корень всё ещё `done` (канбан не умеет переоткрыть готовую
#: карточку). Пока метка жива, карточка стоит в «В работе» — это намерение
#: человека; метка снимается, когда задачу принимают снова.
WORK_FILE = "board_work.json"
SUMMARY_SHORT_MAX = 200
#: Переходы действия `move` (контракт W31). Остальное — ошибка человека.
MOVES = {"new": (), "working": ("review",), "review": ("done", "working"),
         "done": ("working",)}


def stage_of(status, *, accepted: bool = False, archived: bool = False,
             blocking: int = 0) -> str:
    """Колонка задачи по статусу, принятию и числу БЛОКИРУЮЩИХ пунктов.

    Порядок важен: архив и принятие — полная заморозка, они выше любых статусов.
    Блокирующий пункт больше НЕ выносит карточку в отдельную колонку (колонки
    «Ждёт вас» нет с W31): работа стоит на месте, а карточка остаётся «В работе»
    с меткой — иначе задача прыгала бы между колонками на каждом вопросе.
    Параметр ``blocking`` оставлен: им пользуются вызовы, которым нужно знать,
    блокирует ли пункт работу, и тесты волны 30.
    """
    if archived or str(status or "").strip().lower() == "archived":
        return ARCHIVED
    if accepted:
        return "done"
    column = STATUS_COLUMN.get(str(status or "").strip().lower(), "new")
    if column == "new" and int(blocking or 0) > 0:
        return "working"          # ждёт человека — это уже работа, а не очередь
    return column


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


# --------------------------------------------- «вернули в работу» (W31-A1)
def _state_base(cfg) -> Path:
    return Path(getattr(cfg, "state_dir", Path(getattr(cfg, "data_dir", ".")) / "state"))


def work_marks(cfg, room_id) -> dict:
    """``{root_id: {at, note}}`` — задачи, которые человек вернул в работу.

    Только чтение. Пишет их действие `move` (``mark_working``), снимает
    ``clear_working`` при повторном принятии. Ошибки чтения — «меток нет».
    """
    room_id = str(room_id or "")
    if not room_id:
        return {}
    data, _info = _sf.read_json_doc(_state_base(cfg) / room_id / WORK_FILE, None)
    raw = data if isinstance(data, dict) else {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def mark_working(cfg, room_id, root_id, note: str = "") -> dict:
    """Отметить «возвращена в работу»: колонка `working`, пока доска не догнала."""
    room_id, root_id = str(room_id or ""), str(root_id or "")
    if not room_id or not root_id:
        raise ValueError("Не передана комната или задача")
    marks = work_marks(cfg, room_id)
    marks[root_id] = {"at": int(time.time()), "note": " ".join(str(note or "").split())[:400]}
    _sf.write_json_atomic(_state_base(cfg) / room_id / WORK_FILE, marks)
    return marks[root_id]


def clear_working(cfg, room_id, root_id) -> bool:
    """Снять метку «возвращена в работу» (задачу приняли снова)."""
    room_id, root_id = str(room_id or ""), str(root_id or "")
    marks = work_marks(cfg, room_id)
    if root_id not in marks:
        return False
    marks.pop(root_id, None)
    _sf.write_json_atomic(_state_base(cfg) / room_id / WORK_FILE, marks)
    return True


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
        updated_at=None, work_mark=None) -> dict:
    """Одна строка доски для карточки задачи (контракт W31).

    ``waiting`` — пункты «ждёт вас» (как на странице задачи). У принятой задачи их
    нет по определению: принятие — заморозка, висящих вопросов у неё быть не
    может (иначе карточка в «Готово» спорит сама с собой).
    """
    rec = rec if isinstance(rec, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    items = [item for item in (waiting or []) if isinstance(item, dict)]
    blocking = sum(1 for item in items if item.get("blocking"))
    # W30-QA1: принятая задача — полная заморозка, вопросов у неё больше нет.
    if accepted:
        items, blocking = [], 0
    root_id = str(rec.get("root_id") or summary.get("id") or "")
    progress = summary.get("progress") if isinstance(summary.get("progress"), dict) else {}
    status = str(status or summary.get("status") or "")
    kids = [str(c) for c in (children or []) if c]
    if updated_at is None:
        updated_at = (summary.get("finished_at") or summary.get("started_at")
                      or rec.get("created_at") or 0)
    column = stage_of(status, accepted=accepted, archived=archived, blocking=blocking)
    # Метка «возвращена в работу»: доска ещё показывает `done`, а человек уже
    # вернул задачу в работу. Принятая и архивная карточка метку не слушает.
    if work_mark and not accepted and column in ("new", "review"):
        column = "working"
    waiting_count = len(items)
    return {
        "id": root_id,
        "title": _one(summary.get("title") or rec.get("title") or rec.get("text"), 200),
        "column": column,
        # `stage` — то же значение для старых клиентов и тестов волны 30.
        "stage": column,
        "status": status,
        "assignee": summary.get("assignee") or rec.get("assignee"),
        "steps_done": int(progress.get("done") or 0),
        "steps_total": int(progress.get("total") or 0),
        # Метка «Ждёт вас · N вопросов» — на карточке В РАБОТЕ.
        "waiting_you": bool(waiting_count) and column == "working",
        "waiting_count": waiting_count,
        "waiting_blocking": blocking,
        "needs_you": {"count": waiting_count, "blocking": blocking},
        "accepted": bool(accepted),
        "parent_id": parent_id or None,
        "children_count": len(kids),
        "updated_at": int(updated_at or 0),
        "summary_short": summary_short(summary, rec),
    }


def build_rows(records, *, states=None, accepted=None, waiting=None, links_map=None,
               summary_of=None, work_marks_map=None) -> list:
    """Строки доски по записям реестра комнаты.

    ``states`` — ``{root_id: {status}}`` с доски; ``accepted`` — набор id принятых
    задач; ``waiting`` — ``{root_id: [{kind, id, text, blocking}]}`` (тот же
    расчёт, что на странице задачи); ``links_map`` — ``links()``;
    ``work_marks_map`` — ``work_marks()``; ``summary_of(rec)`` — сводка задачи
    (может бросить: тогда карточка остаётся с локальными данными).
    """
    states = states if isinstance(states, dict) else {}
    accepted = {str(i) for i in (accepted or set())}
    waiting = waiting if isinstance(waiting, dict) else {}
    links_map = links_map if isinstance(links_map, dict) else {}
    work_marks_map = work_marks_map if isinstance(work_marks_map, dict) else {}
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
                        parent_id=link.get("parent"), children=link.get("children"),
                        work_mark=work_marks_map.get(root_id)))
    rows.sort(key=lambda item: (-int(item.get("updated_at") or 0),
                                ARCHIVED == item.get("column"), str(item.get("id") or "")))
    return rows


def counts(rows) -> dict:
    """Счётчики по колонкам + архив. Число в колонке = числу карточек в ней."""
    out = {stage: 0 for stage in COLUMNS}
    out[ARCHIVED] = 0
    out["total"] = 0
    for item in rows or []:
        column = str((item or {}).get("column") or "")
        if column in out:
            out[column] += 1
        out["total"] += 1          # всего задач, включая неизвестную колонку
    return out


def payload(room_id: str, rows, *, now=None) -> dict:
    """Тело ответа ``GET /api/rooms/{room}/tasks`` (контракт W31)."""
    return {"tasks": list(rows or []), "columns": list(COLUMNS),
            "counts": counts(rows), "room": str(room_id or ""),
            "labels": dict(STAGE_RU), "hints": dict(STAGE_HINTS),
            "at": int(now or time.time())}


def allowed_moves(column: str) -> list:
    """Допустимые ``to`` для действия `move` из этой колонки (контракт W31)."""
    return list(MOVES.get(str(column or ""), ()))


def column_for(cfg, room, root_id, *, status="", accepted=None, archived: bool = False,
               blocking: int = 0) -> str:
    """Колонка ОДНОЙ задачи — для ``board_column`` в ответе страницы задачи."""
    root_id = str(root_id or "")
    room_id = str((room or {}).get("id") or "") if isinstance(room, dict) else str(room or "")
    if accepted is None:
        try:
            try:
                from . import passports as _passports
            except ImportError:  # pragma: no cover
                import passports as _passports       # type: ignore
            accepted = bool(_passports.is_accepted(cfg, room, root_id))
        except Exception:  # noqa: BLE001 - отметка вторична
            accepted = False
    column = stage_of(status, accepted=bool(accepted), archived=archived, blocking=blocking)
    if not accepted and column in ("new", "review"):
        try:
            if root_id in work_marks(cfg, room_id):
                return "working"
        except Exception:  # noqa: BLE001
            pass
    return column


__all__ = ["COLUMNS", "STAGES", "ARCHIVED", "STAGE_RU", "STAGE_HINTS", "STATUS_COLUMN",
           "STATUS_STAGE", "PROJECTS_FILE", "WORK_FILE", "MOVES", "stage_of", "links",
           "work_marks", "mark_working", "clear_working", "summary_short", "row",
           "build_rows", "counts", "payload", "allowed_moves", "column_for"]
