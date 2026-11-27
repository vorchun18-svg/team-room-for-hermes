"""Паспорта принятых задач комнаты (W30-D1): «Принята» = полная заморозка + память.

Что здесь живёт
---------------
* **Состояние принятия** — файл ``data/state/<room>/accepted.json`` в форме
  контракта §3: ``{root_id: {at, by, passport}}`` (плюс служебные поля
  ``stopped``/``note`` — добавлены, существующие не менялись). Один источник
  правды для «Принята»: доска про принятие не знает и не переписывается.
* **Паспорт** — итог задачи в память комнаты: ``{title, outcome, decisions,
  files, at}``. Собирается БЕЗ вызова модели: из свода (последний текст),
  решений карточки и путей артефактов. Это и есть «итог в память комнаты»,
  который подмешивается в контекст других задач (``context_pack``) и в поиск
  по памяти (``memory_search``).
* **Заморозка** — признак ``is_accepted`` для всех петель работы: сторож,
  автосвод, цикл критика, напоминания, уведомления, «Итог дня», отчёт
  диспетчера. Везде, где исключается ``archived``, теперь исключается и
  принятая задача: ноль карточек и ноль вызовов моделей после принятия.

Возврат («Вернуть в работу») просто снимает отметку: карточки на доске не
переписываются, ничего не запускается само — задача возвращается в «Готово
(по мнению системы)», и человек решает, что делать дальше.

Модуль читает/пишет только файлы состояния комнаты, в сеть не ходит и моделей
не зовёт. Кэш отметок — 1 секунда (и сбрасывается записью), чтобы тик сторожа
не читал десятки маленьких файлов на каждом запросе.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт (тесты из корня проекта)
    from . import statefiles as _sf
except ImportError:  # pragma: no cover
    import statefiles as _sf        # type: ignore

#: Имя файла принятых задач внутри ``data/state/<room>``.
ACCEPTED_FILE = "accepted.json"
#: Потолки паспорта (контракт §3).
OUTCOME_MAX = 600
TITLE_MAX = 160
DECISIONS_MAX = 5
FILES_MAX = 8
#: Сводка «Принятые задачи комнаты» для контекста других задач (контракт §3).
ROOM_SUMMARY_MAX = 1200
ROOM_SUMMARY_ITEMS = 6
ROOM_SUMMARY_LINE = 170
#: Причина остановки карточек, которые ещё шли в момент принятия. Ею же
#: пользуется отчёт диспетчера, чтобы не считать их «зависшими».
ACCEPT_STOP_REASON = "задача принята — работа остановлена"
#: Сколько секунд живёт кэш отметок (запись сбрасывает его сразу).
CACHE_TTL_S = 1.0

_CACHE_LOCK = threading.RLock()
_CACHE: dict = {"at": 0.0, "roots": {}}


# ------------------------------------------------------------------ пути/чтение
def accepted_path(cfg, room_id) -> Path:
    """Путь файла принятых задач комнаты: ``data/state/<room>/accepted.json``."""
    return Path(getattr(cfg, "state_dir", Path(getattr(cfg, "data_dir", ".")) / "state")) \
        / str(room_id) / ACCEPTED_FILE


def _room_id(room) -> str:
    """id комнаты из словаря комнаты или строки."""
    if isinstance(room, dict):
        return str(room.get("id") or "")
    return str(room or "")


def _norm(raw) -> dict:
    """Привести файл к ``{root_id: запись}`` (терпимо к обёртке ``{"tasks": …}``)."""
    data = raw if isinstance(raw, dict) else {}
    tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else data
    out: dict = {}
    for key, value in (tasks or {}).items():
        root_id = str(key or "")
        if not root_id or not isinstance(value, dict):
            continue
        out[root_id] = dict(value)
    return out


def load(cfg, room) -> dict:
    """Все принятые задачи комнаты: ``{root_id: {at, by, passport, stopped}}``."""
    room_id = _room_id(room)
    if not room_id:
        return {}
    data, _info = _sf.read_json_doc(accepted_path(cfg, room_id), None)
    return _norm(data)


def save(cfg, room, tasks: dict) -> None:
    """Записать файл принятых задач комнаты (атомарно) и сбросить кэш."""
    room_id = _room_id(room)
    if not room_id:
        return
    _sf.write_json_atomic(accepted_path(cfg, room_id), _norm(tasks))
    invalidate()


def invalidate() -> None:
    """Сбросить кэш отметок приёма (после записи файла)."""
    with _CACHE_LOCK:
        _CACHE["at"] = 0.0
        _CACHE["roots"] = {}


def frozen_roots(cfg) -> dict:
    """``{root_id: room_id}`` по ВСЕМ комнатам — для чтения статусов пачкой.

    Нужно там, где известен только id корня (``engine_read.today_states``,
    ``request_states``, отчёт диспетчера). Кэш — 1 секунда, запись его сбрасывает.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE["at"] and now - float(_CACHE["at"]) < CACHE_TTL_S:
            return dict(_CACHE["roots"])
    roots: dict = {}
    try:
        base = Path(getattr(cfg, "state_dir", Path(getattr(cfg, "data_dir", ".")) / "state"))
        folders = sorted(p for p in base.iterdir() if p.is_dir())
    except (OSError, AttributeError):
        folders = []
    for folder in folders:
        data, _info = _sf.read_json_doc(folder / ACCEPTED_FILE, None)
        for root_id in _norm(data):
            roots[root_id] = folder.name
    with _CACHE_LOCK:
        _CACHE["at"] = now
        _CACHE["roots"] = dict(roots)
    return roots


def is_accepted(cfg, room, root_id) -> bool:
    """Принята ли задача (заморожена). Дешёвая проверка для всех петель работы."""
    root_id = str(root_id or "")
    if not root_id:
        return False
    return root_id in frozen_roots(cfg)


def accepted_ids(cfg, room) -> set:
    """id принятых задач одной комнаты."""
    return set(load(cfg, room))


def get(cfg, room, root_id) -> dict:
    """Запись принятия одной задачи (или пустой словарь)."""
    record = load(cfg, room).get(str(root_id or ""))
    return record if isinstance(record, dict) else {}


def passport_of(cfg, room, root_id) -> dict:
    """Паспорт принятой задачи (или пустой словарь)."""
    record = get(cfg, room, root_id)
    passport = record.get("passport")
    return passport if isinstance(passport, dict) else {}


def frozen_card_ids(cfg) -> set:
    """id карточек, остановленных при принятии (для отчёта диспетчера)."""
    out: set = set()
    for room_id in set(frozen_roots(cfg).values()):
        for record in load(cfg, {"id": room_id}).values():
            for card_id in record.get("stopped") or []:
                if card_id:
                    out.add(str(card_id))
    return out


# ------------------------------------------------------------------- паспорт
def _one(value, limit: int) -> str:
    """Одна строка: схлопнуть пробелы и обрезать по длине."""
    text = " ".join(str(value or "").split())
    if limit and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _outcome(detail: dict, record: dict) -> str:
    """Итог паспорта: текст свода, иначе короткая подпись состояния/текст задачи.

    Никаких моделей: берём то, что уже лежит в карточках (свод, критик, реестр).
    """
    synth = detail.get("synthesis") if isinstance(detail.get("synthesis"), dict) else {}
    for key in ("text_md", "text", "summary"):
        text = _one(synth.get(key), OUTCOME_MAX)
        if text:
            return text
    critic = detail.get("critic") if isinstance(detail.get("critic"), dict) else {}
    for key in ("summary", "verdict_text", "reason"):
        text = _one(critic.get(key), OUTCOME_MAX)
        if text:
            return text
    for key in ("summary", "status_reason", "preview"):
        text = _one(detail.get(key), OUTCOME_MAX)
        if text:
            return text
    for key in ("summary", "text", "title"):
        text = _one(record.get(key), OUTCOME_MAX)
        if text:
            return text
    return ""


def _decisions(detail: dict) -> list:
    """Решения задачи: принятые/обсуждённые — текстом, без внутренних кодов."""
    out: list = []
    for item in detail.get("decisions") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") in ("dismissed", "superseded", "rejected"):
            continue
        text = _one(item.get("text"), TITLE_MAX)
        if text and text not in out:
            out.append(text)
        if len(out) >= DECISIONS_MAX:
            break
    return out


def _artifact_paths(value) -> list:
    """Пути из ``metadata.artifacts`` карточки (строки и объекты ``{path}``)."""
    out: list = []
    for item in value if isinstance(value, (list, tuple)) else []:
        path = item.get("path") if isinstance(item, dict) else item
        path = str(path or "").strip()
        if path and path not in out:
            out.append(path)
    return out


def _files(detail: dict) -> list:
    """Файлы агентов: ``[{agent, path, what}]`` из дерева и блока артефактов.

    Агент известен там, где путь пришёл из ``metadata.artifacts`` его карточки;
    файлы, найденные только сканированием папки, получают ``agent: ""`` — это
    честно, а не «приписано первому попавшемуся».
    """
    out: list = []
    seen: set = set()

    def add(agent, path, what):
        path = str(path or "").strip()
        if not path or path in seen or len(out) >= FILES_MAX:
            return
        seen.add(path)
        out.append({"agent": str(agent or ""), "path": path,
                    "what": _one(what or Path(path).name, 120)})

    for node in detail.get("tree") or []:
        if not isinstance(node, dict):
            continue
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        canon = meta.get("canon") if isinstance(meta.get("canon"), dict) else meta
        for path in _artifact_paths(canon.get("artifacts")):
            add(node.get("assignee") or "", path,
                next((a.get("title") for a in (canon.get("artifacts") or [])
                      if isinstance(a, dict) and str(a.get("path") or "") == path), ""))
    for item in detail.get("artifacts") or []:
        if isinstance(item, dict) and item.get("path"):
            add("", item.get("path"), item.get("title"))
    return out


def build_passport(cfg, room, record: dict, detail: dict | None = None) -> dict:
    """Собрать паспорт принятой задачи БЕЗ вызова модели.

    ``detail`` — уже прочитанная деталь запроса (если её нет, паспорт соберётся
    из записи реестра: название, текст, итог — что есть). Ошибки чтения не
    бросаются: паспорт — память, а не блокировка принятия.
    """
    record = record if isinstance(record, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    title = _one(detail.get("title_short") or detail.get("title") or record.get("title")
                 or record.get("text"), TITLE_MAX)
    passport = {
        "title": title,
        "outcome": _outcome(detail, record),
        "decisions": _decisions(detail),
        "files": _files(detail),
        "at": int(time.time()),
    }
    return passport


# --------------------------------------------------------------- принятие
def accept(cfg, room, record: dict, root_id: str, *, by: str = "", note: str = "",
           passport: dict | None = None, stopped=None, at=None) -> dict:
    """Записать принятие задачи (идемпотентно).

    Возвращает ``{accepted, already, at, passport}``: повторное нажатие не
    плодит записей и не меняет паспорт (паспорт — итог, а не журнал).
    """
    room_id = _room_id(room)
    root_id = str(root_id or "")
    if not room_id or not root_id:
        raise ValueError("Не передана комната или задача")
    tasks = load(cfg, room)
    existing = tasks.get(root_id)
    if isinstance(existing, dict) and existing.get("at"):
        return {"accepted": True, "already": True, "at": int(existing.get("at") or 0),
                "passport": existing.get("passport") if isinstance(existing.get("passport"), dict) else {},
                "stopped": list(existing.get("stopped") or [])}
    stamp = int(at or time.time())
    entry = {"at": stamp, "by": str(by or ""),
             "passport": dict(passport or build_passport(cfg, room, record)),
             "stopped": [str(i) for i in (stopped or []) if i],
             "note": _one(note, 400)}
    tasks[root_id] = entry
    save(cfg, room, tasks)
    return {"accepted": True, "already": False, "at": stamp,
            "passport": dict(entry["passport"]), "stopped": list(entry["stopped"])}


def reopen(cfg, room, root_id: str, *, by: str = "") -> dict:
    """«Вернуть в работу»: снять отметку принятия. Карточки не переписываются."""
    room_id = _room_id(room)
    root_id = str(root_id or "")
    if not room_id or not root_id:
        raise ValueError("Не передана комната или задача")
    tasks = load(cfg, room)
    if root_id not in tasks:
        return {"reopened": False, "at": 0, "by": str(by or ""),
                "message": "Задача не была принята"}
    tasks.pop(root_id, None)
    save(cfg, room, tasks)
    return {"reopened": True, "at": int(time.time()), "by": str(by or ""),
            "message": "Задача возвращена в работу — она снова в «Готово (по мнению системы)»"}


# ------------------------------------------------------------- память комнаты
def summary_text(cfg, room, limit: int = ROOM_SUMMARY_MAX) -> str:
    """Сводка «Принятые задачи комнаты» для контекста других задач (≤ ``limit``).

    Последние по дате принятия, по одной строке: название — итог. Пустая строка,
    если принятых задач нет (тогда и раздел в контексте не рисуется).
    """
    tasks = load(cfg, room)
    if not tasks:
        return ""
    rows = []
    for root_id, entry in tasks.items():
        passport = entry.get("passport") if isinstance(entry.get("passport"), dict) else {}
        title = _one(passport.get("title") or root_id, 70)
        outcome = _one(passport.get("outcome"), ROOM_SUMMARY_LINE)
        rows.append((int(entry.get("at") or 0), root_id, title, outcome))
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    lines = ["Принятые задачи комнаты (итог уже утверждён человеком):"]
    for _at, root_id, title, outcome in rows[:ROOM_SUMMARY_ITEMS]:
        line = "- «%s» — %s" % (title, outcome) if outcome else "- «%s»" % title
        if len("\n".join(lines + [line])) > limit:
            break
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text if len(lines) > 1 else ""


def decorate_detail(cfg, room, detail: dict) -> dict:
    """Проставить детали запроса поля принятия (один вызов на страницу запроса).

    ``accepted`` / ``accepted_at`` / ``accepted_by`` / ``passport`` / ``board_stage``
    (стадия доски; поле ``stage`` занято этапами проекта — его не трогаем).
    Ошибки чтения файла не бросаются: страница важнее отметки.
    """
    if not isinstance(detail, dict):
        return detail
    root_id = str(detail.get("id") or "")
    try:
        record = get(cfg, room, root_id)
    except Exception:  # noqa: BLE001 - отметка вторична для страницы
        record = {}
    detail["accepted"] = bool(record)
    detail["accepted_at"] = int(record.get("at") or 0) if record else 0
    detail["accepted_by"] = str(record.get("by") or "") if record else ""
    detail["passport"] = record.get("passport") if isinstance(record.get("passport"), dict) else None
    try:
        from . import tasks_board as board_mod
    except ImportError:  # pragma: no cover
        import tasks_board as board_mod  # type: ignore
    try:
        detail["board_stage"] = board_mod.stage_of(
            detail.get("status"), accepted=detail["accepted"],
            archived=bool(detail.get("archived")))
    except Exception:  # noqa: BLE001 - стадия вторична
        detail["board_stage"] = None
    # W31-A1: колонка доски и допустимые перемещения — клиент строит кнопки
    # «Принять / Вернуть на доработку / Вернуть в работу» по ним, без догадок.
    try:
        detail["board_column"] = board_mod.column_for(
            cfg, room, root_id, status=detail.get("status"),
            accepted=bool(detail["accepted"]), archived=bool(detail.get("archived")))
    except Exception:  # noqa: BLE001 - колонка вторична
        detail["board_column"] = detail.get("board_stage")
    try:
        detail["allowed_moves"] = board_mod.allowed_moves(detail.get("board_column"))
    except Exception:  # noqa: BLE001
        detail["allowed_moves"] = []
    return detail


__all__ = ["ACCEPTED_FILE", "ACCEPT_STOP_REASON", "OUTCOME_MAX", "ROOM_SUMMARY_MAX",
           "ROOM_SUMMARY_ITEMS", "accepted_path", "load", "save", "invalidate",
           "frozen_roots", "is_accepted", "accepted_ids", "get", "passport_of",
           "frozen_card_ids", "build_passport", "accept", "reopen", "summary_text",
           "decorate_detail"]
