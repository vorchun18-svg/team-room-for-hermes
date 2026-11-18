"""«Дело» — долгоживущий объект комнаты: хранение, этапы, ожидания, результат, исходящее.

Контракт формы — `docs/WAVE26-RULES.md` §1 (общий с пакетом D6), описание —
`docs/API.md` §W26-DELO.

Где живут данные: ``data/state/<room>/delos.json`` — один файл на комнату, запись
атомарная (``statefiles.write_json_atomic``) и под замком файла. Никакой второй
базы: этапы, ожидания, части результата, файлы, исходящее и лента «Ход дела» —
в этом же документе.

Что здесь есть:

* ``create`` — дело из запроса (``request_id``) и вручную;
* ``list_delos`` / ``get`` / ``find`` — чтение; ``needs_you`` собирается ПРИ ЧТЕНИИ
  из существующих модулей через ``Sources`` (вопросы/решения/шаги — бэклог
  комнаты, разрешения — ``approvals``), поэтому своих копий правил здесь нет;
* ``act`` — единственный путь записи, кроме создания: 15 действий контракта §1;
* ``Delo.view`` — вид дела для API: с пересчитанными ``needs_you`` и статусом.

Исходящее (``outbox``) живёт по правилу «наружу — только по вашему да»:
``draft → approved → sent``. Никаких писем и записей во внешние системы модуль не
делает: ``mark_sent`` — ручная отметка (или отметка канала, когда он появится),
``sent_at``/``error`` пишутся в дело и в ленту.

    python -m unittest tests.test_delo -v
"""
from __future__ import annotations

import copy
import functools
import re
import secrets
import threading
import time
from datetime import date
from pathlib import Path

try:                                     # пакетный и «плоский» импорт
    from . import statefiles as sf
except ImportError:                      # pragma: no cover
    import statefiles as sf              # type: ignore

#: Файл дела внутри `data/state/<room>`.
STATE_FILE = "delos.json"
#: Версия формы файла: пригодится, если форма дела поменяется.
STORE_VERSION = 1

#: Цепочка этапов по умолчанию (§1): можно править действием `set_stage`.
STAGE_TITLES = ("Сбор", "Черновик", "Согласование", "Исполнение", "Готово")

STATUSES = ("open", "waiting", "paused", "done", "cancelled")
STAGE_STATES = ("done", "now", "todo")
PART_STATES = ("agreed", "discussing", "todo")
OUTBOX_KINDS = ("email", "file", "message")
OUTBOX_STATES = ("draft", "approved", "sent", "failed", "cancelled")
FILE_ROLES = ("final", "source", "draft")
NEEDS_KINDS = ("approve", "question", "permission")

#: Действия маршрута `POST /api/delos/{id}/actions` (§1).
ACTIONS = (
    "approve", "reject", "answer",
    "set_stage", "add_part", "set_part",
    "approve_outbox", "edit_outbox", "cancel_outbox", "mark_sent",
    "pause", "resume", "done",
    "add_waiting", "clear_waiting",
)
#: Действия по пункту «ждёт вашего решения»: уходят в существующий механизм запроса.
POINT_ACTIONS = ("approve", "reject", "answer")

#: Кто пишет в ленту «Ход дела».
WHO_MANAGER = "manager"
WHO_YOU = "you"
WHO_SYSTEM = "system"

TITLE_MAX = 200
TEXT_MAX = 2000
BODY_MAX = 20000
WHO_MAX = 120
#: Сколько строк ленты держим в деле (старое обрезается — файл не растёт вечно).
TIMELINE_MAX = 300
#: Сколько дел принимаем в одну комнату (защита от мусора в файле).
MAX_DELOS = 500

DELO_ID_RE = re.compile(r"^dl_[A-Za-z0-9]{2,24}$")
REQUEST_ID_RE = re.compile(r"^t_[A-Za-z0-9_\-]{1,40}$")
PATH_MAX = 1000

#: Подсказка «что с этим делать» для каждого вида пункта (не приказ, а следующий шаг).
RECOMMENDED = {
    "question": "Ответить на вопрос",
    "approve": "Принять решение",
    "permission": "Разрешить или запретить",
}


class DeloError(Exception):
    """Ошибка дела: HTTP-статус + русский текст + машинный ``detail``.

    Лишние именованные аргументы (``field=``, ``value=``, ``allowed=``, ``missing=``)
    попадают в ``detail`` — так же, как у ``http_util.err``.
    """

    def __init__(self, message: str, http: int = 400, code: str = "bad_request", detail=None,
                 **extra):
        super().__init__(message)
        self.message = str(message)
        self.http = int(http)
        self.code = str(code)
        payload = dict(detail or {})
        payload.update(extra)
        self.detail = payload


# --------------------------------------------------------------- источники «ждёт вас»
class Sources:
    """Откуда берётся «ждёт вашего решения»: вопросы, решения, шаги, разрешения.

    Правила «что ждёт человека» живут в существующих модулях (``ingest.backlog_view``
    и ``approvals``). Дело их не переписывает, а спрашивает: ``needs`` возвращает
    список пунктов запроса в общем виде:

        {id, kind: "open_question|decision|next_step|approval", text, at,
         recommended?, options?, question_id?|decision_id?|step_id?|approval_id?}

    Пустой источник (``NullSources``) — законное состояние: дело без запроса или
    модуль недоступен. Тогда «ждёт вашего решения» пусто, а не выдумано.
    """

    def needs(self, room_id: str, request_id: str) -> list[dict]:
        return []

    def request(self, room_id: str, request_id: str) -> dict | None:
        """Запрос комнаты (``{id, title}``) или ``None``: нужен заголовок и проверка связи."""
        return None


class NullSources(Sources):
    """Ничего не знает: дело живёт на своих данных (вручную созданное/без запроса)."""


# ------------------------------------------------------------------------- утилиты
def _now(now=None) -> int:
    return int(time.time()) if now is None else int(now)


#: Замок записи: чтение-изменение-запись файла дела идут по очереди в процессе,
#: а сама запись атомарна ещё и по файлу (``statefiles.write_json_atomic``).
_WRITE_LOCK = threading.RLock()


def _serialized(func):
    """Провести запись дела под замком процесса: два действия не теряют друг друга."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _WRITE_LOCK:
            return func(*args, **kwargs)

    return wrapper


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _one_line(value, limit: int = 0) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if limit and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _need_text(value, field: str, limit: int, required: bool = True) -> str:
    """Строка с проверкой: пустая обязательная — 400, длинная — 400 с лимитом."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise DeloError("Поле %s должно быть строкой" % field, field=field, value=str(value)[:80])
    text = value.strip()
    if required and not text:
        raise DeloError("Нужно поле %s" % field, field=field, missing=[field])
    if len(text) > limit:
        raise DeloError("Поле %s длиннее %d знаков" % (field, limit), field=field,
                        length=len(text), limit=limit)
    if "\x00" in text:
        raise DeloError("В поле %s есть NUL-байт" % field, field=field)
    return text


def _need_choice(value, field: str, allowed, default: str | None = None) -> str:
    text = str(value if value is not None else default or "").strip().lower()
    if not text or text not in allowed:
        raise DeloError("Поле %s: одно из %s" % (field, ", ".join(allowed)), field=field,
                        value=str(value)[:40], allowed=list(allowed))
    return text


def _due(value) -> str | None:
    """Срок ``YYYY-MM-DD``; пусто → ``None``; мусор → 400 (молча не выкидываем)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise DeloError("Срок — строка вида 2026-11-21", field="due", value=str(value)[:40])
    text = value.strip()
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise DeloError("Срок — строка вида 2026-11-21", field="due", value=text[:40]) from exc
    return text


def _new_delo_id(taken: set[str]) -> str:
    for _ in range(20):
        ident = "dl_" + secrets.token_hex(4)
        if ident not in taken:
            return ident
    raise DeloError("Не удалось подобрать id дела", http=500, code="engine_error")


def _new_outbox_id(delo: dict) -> str:
    return "ob_%d" % (len(delo.get("outbox") or []) + 1)


def _new_part_id(delo: dict) -> str:
    return "pt_%d" % (len((delo.get("result") or {}).get("parts") or []) + 1)


def _tl(delo: dict, at: int, who: str, text: str, kind: str) -> dict:
    """Дописать строку в «Ход дела» и вернуть её (лента хранится в деле)."""
    entry = {"at": int(at), "who": str(who), "text": _one_line(text, TEXT_MAX), "kind": str(kind)}
    timeline = delo.setdefault("timeline", [])
    timeline.append(entry)
    if len(timeline) > TIMELINE_MAX:
        del timeline[: len(timeline) - TIMELINE_MAX]
    return entry


# --------------------------------------------------------------------------- этапы
def default_stages() -> list[dict]:
    """Цепочка по умолчанию: первый этап «сейчас», остальные «ещё нет»."""
    out = []
    for number, title in enumerate(STAGE_TITLES, 1):
        out.append({"id": "st_%d" % number, "title": title,
                    "state": "now" if number == 1 else "todo"})
    return out


def _normalize_stages(raw) -> list[dict]:
    """Этапы из запроса: список строк или объектов ``{id?, title, state?}``.

    Гарантии: непустая цепочка, уникальные id, ровно один «сейчас» (если дело не
    завершено), состояния из контракта. Пустое/битое → цепочка по умолчанию.
    """
    items: list[dict] = []
    for number, item in enumerate(raw or [], 1):
        if isinstance(item, str):
            title, state = item, ""
        elif isinstance(item, dict):
            title, state = item.get("title"), item.get("state")
        else:
            continue
        title = _one_line(title, TITLE_MAX)
        if not title:
            continue
        state = str(state or "").strip().lower()
        if state not in STAGE_STATES:
            state = "todo"
        ident = _one_line(item.get("id") if isinstance(item, dict) else "", 20) or "st_%d" % number
        if any(stage["id"] == ident for stage in items):
            ident = "st_%d" % number
        items.append({"id": ident, "title": title, "state": state})
    if not items:
        return default_stages()
    if not any(stage["state"] == "now" for stage in items) and \
            not all(stage["state"] == "done" for stage in items):
        for stage in items:
            if stage["state"] != "done":
                stage["state"] = "now"
                break
    return items


def _stage_of(delo: dict, stage_id: str) -> dict:
    stage_id = str(stage_id or "").strip()
    for stage in delo.get("stages") or []:
        if str(stage.get("id")) == stage_id:
            return stage
    raise DeloError("Этап не найден", http=404, code="not_found", detail={"stage_id": stage_id})


def set_stage_state(delo: dict, stage_id: str, state: str) -> dict:
    """Перевести этап: «сейчас» → предыдущие сделаны; «сделано» → следующий стал «сейчас»."""
    stage = _stage_of(delo, stage_id)
    state = _need_choice(state, "state", STAGE_STATES)
    chain = delo.get("stages") or []
    index = chain.index(stage)
    if state == "now":
        for before in chain[:index]:
            if before.get("state") != "done":
                before["state"] = "done"
        stage["state"] = "now"
        for after in chain[index + 1:]:
            if after.get("state") == "now":
                after["state"] = "todo"
    elif state == "done":
        stage["state"] = "done"
        for before in chain[:index]:       # этап не может быть «сделан» раньше предыдущих
            if before.get("state") != "done":
                before["state"] = "done"
        for after in chain[index + 1:]:
            if after.get("state") != "done":
                after["state"] = "now"
                break
    else:                                   # «ещё нет»: этот и все следующие — назад
        stage["state"] = "todo"
        for after in chain[index + 1:]:
            after["state"] = "todo"
        if not any(item.get("state") == "now" for item in chain):
            stage["state"] = "now"
    return stage


def current_stage(delo: dict) -> dict | None:
    for stage in delo.get("stages") or []:
        if stage.get("state") == "now":
            return stage
    return None


# --------------------------------------------------------------------- результат
def _normalize_parts(raw) -> list[dict]:
    items: list[dict] = []
    for number, item in enumerate(raw or [], 1):
        if isinstance(item, str):
            title, state = item, "todo"
        elif isinstance(item, dict):
            title, state = item.get("title"), item.get("state")
        else:
            continue
        title = _one_line(title, TITLE_MAX)
        if not title:
            continue
        state = str(state or "").strip().lower()
        if state not in PART_STATES:
            state = "todo"
        items.append({"id": "pt_%d" % number, "title": title, "state": state})
    return items


def _normalize_files(raw) -> list[dict]:
    items: list[dict] = []
    for item in raw or []:
        if isinstance(item, str):
            path, title, role, note = item, "", "final", ""
        elif isinstance(item, dict):
            path = item.get("path")
            title = item.get("title")
            role = item.get("role")
            note = item.get("note")
        else:
            continue
        path = _one_line(path, PATH_MAX)
        if not path:
            continue
        role = str(role or "final").strip().lower()
        if role not in FILE_ROLES:
            role = "final"
        items.append({"path": path, "title": _one_line(title or Path(path).name, TITLE_MAX),
                      "role": role, "note": _one_line(note, TEXT_MAX)})
    return items


def _part_of(delo: dict, part_id: str) -> dict:
    for part in (delo.get("result") or {}).get("parts") or []:
        if str(part.get("id")) == str(part_id or "").strip():
            return part
    raise DeloError("Часть результата не найдена", http=404, code="not_found",
                    detail={"part_id": str(part_id or "")[:40]})


# ---------------------------------------------------------------------- исходящее
def _normalize_outbox(raw) -> list[dict]:
    """Исходящее из запроса: состояния и id выставляются здесь, а не интерфейсом."""
    items: list[dict] = []
    for number, item in enumerate(raw or [], 1):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "message").strip().lower()
        if kind not in OUTBOX_KINDS:
            continue
        to = _one_line(item.get("to"), TITLE_MAX)
        subject = _one_line(item.get("subject"), TITLE_MAX)
        body = item.get("body")
        if not isinstance(body, str):
            body = "" if body is None else str(body)
        if len(body) > BODY_MAX:
            body = body[:BODY_MAX]
        attachments = [_one_line(path, PATH_MAX) for path in (item.get("attachments") or [])
                       if _one_line(path, PATH_MAX)]
        state = str(item.get("state") or "draft").strip().lower()
        if state not in OUTBOX_STATES:
            state = "draft"
        entry = {"id": "ob_%d" % number, "kind": kind, "to": to, "subject": subject,
                 "body": body, "attachments": attachments, "state": state}
        if entry["state"] in ("sent", "failed"):
            entry["sent_at"] = _int(item.get("sent_at")) or None
        if entry["state"] == "failed" or item.get("error"):
            entry["error"] = _one_line(item.get("error"), TEXT_MAX)
        items.append(entry)
    return items


def _outbox_of(delo: dict, outbox_id: str) -> dict:
    for item in delo.get("outbox") or []:
        if str(item.get("id")) == str(outbox_id or "").strip():
            return item
    raise DeloError("Исходящее не найдено", http=404, code="not_found",
                    detail={"outbox_id": str(outbox_id or "")[:40]})


# --------------------------------------------------------------- «ждёт вашего решения»
def _options(raw) -> list[dict]:
    out: list[dict] = []
    for number, item in enumerate(raw or [], 1):
        if isinstance(item, str):
            label = _one_line(item, TITLE_MAX)
            ident = "o_%d" % number
        elif isinstance(item, dict):
            label = _one_line(item.get("label") or item.get("title"), TITLE_MAX)
            ident = _one_line(item.get("id"), 40) or "o_%d" % number
        else:
            continue
        if label:
            out.append({"id": ident, "label": label})
    return out


def _point_kind(source_kind: str) -> str | None:
    """Вид пункта источника → вид контракта §1 (approve|question|permission)."""
    if source_kind == "open_question":
        return "question"
    if source_kind in ("decision", "next_step"):
        return "approve"
    if source_kind == "approval":
        return "permission"
    return None


def _point_actions(kind: str) -> list[str]:
    if kind == "question":
        return ["answer"]
    return ["approve", "reject"]


def needs_you(delo: dict, sources: Sources | None, now: int | None = None) -> list[dict]:
    """Пункты «ждёт вашего решения» — из существующих источников, без копий.

    Дело без ``request_id`` (создано вручную) или без источника не имеет пунктов:
    пустой список честнее выдуманного «ждёт вас».
    """
    request_id = str(delo.get("request_id") or "")
    if not request_id or sources is None:
        return []
    now = _now(now)
    try:
        raw = sources.needs(str(delo.get("room") or ""), request_id) or []
    except Exception:                        # noqa: BLE001 — источник вторичен для чтения дела
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source_kind = str(item.get("kind") or "")
        kind = _point_kind(source_kind)
        if kind is None:
            continue
        text = _one_line(item.get("text"), TEXT_MAX)
        if not text:
            continue
        source: dict = {"kind": source_kind, "request_id": request_id}
        for field in ("question_id", "decision_id", "step_id", "approval_id"):
            if item.get(field):
                source[field] = str(item.get(field))
        point = {
            "id": "ny_" + (_one_line(item.get("id"), 60) or secrets.token_hex(3)),
            "kind": kind,
            "text": text,
            "since": _int(item.get("at")) or now,
            "actions": _point_actions(kind),
            "source": source,
        }
        recommended = _one_line(item.get("recommended"), TITLE_MAX) or RECOMMENDED[kind]
        point["recommended"] = recommended
        options = _options(item.get("options"))
        if options:
            point["options"] = options
        out.append(point)
    rank = {"permission": 0, "approve": 1, "question": 2}
    out.sort(key=lambda point: (rank.get(point["kind"], 9), point.get("since") or 0))
    return out


def derived_status(delo: dict) -> str:
    """Статус для чтения: явные «пауза/закрыто/отменено» сильнее вычисленного.

    Иначе: есть ожидания (``waiting_on``) → ``waiting``, нет → ``open``.
    """
    status = str(delo.get("status") or "open")
    if status in ("paused", "done", "cancelled"):
        return status
    return "waiting" if delo.get("waiting_on") else "open"


# ------------------------------------------------------------------------ хранилище
def store_path(cfg, room_id) -> Path:
    """``data/state/<room>/delos.json`` — как остальные файлы состояния комнаты."""
    return Path(cfg.state_dir) / str(room_id) / STATE_FILE


def _empty_store() -> dict:
    return {"version": STORE_VERSION, "updated_at": 0, "items": []}


def _load(cfg, room_id) -> dict:
    """Прочитать файл дел: нет файла → пусто; битый → карантин и пусто; занят → 503."""
    path = store_path(cfg, room_id)
    data, info = sf.read_json_doc(path, None)
    state = str((info or {}).get("state") or "ok")
    if state == "unreadable":
        raise DeloError("Файл дел комнаты сейчас недоступен: повторите попытку",
                        http=503, code="state_unavailable", detail={"path": str(path)})
    if state == "corrupt":
        sf.quarantine(path)              # копия битого файла остаётся рядом
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    return {"version": _int(data.get("version"), STORE_VERSION),
            "updated_at": _int(data.get("updated_at")), "items": items}


def _save(cfg, room_id, data: dict) -> None:
    data["version"] = STORE_VERSION
    data["updated_at"] = _now()
    path = store_path(cfg, room_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write_json_atomic(path, data)


def _clean(delo: dict) -> dict:
    """Опорная форма дела: все ключи контракта на месте (файл могли править руками)."""
    out = dict(delo) if isinstance(delo, dict) else {}
    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    out["stages"] = _normalize_stages(out.get("stages"))
    out["waiting_on"] = [item for item in (out.get("waiting_on") or []) if isinstance(item, dict)]
    out["needs_you"] = [item for item in (out.get("needs_you") or []) if isinstance(item, dict)]
    out["result"] = {"parts": _normalize_parts(result.get("parts")),
                     "files": _normalize_files(result.get("files"))}
    out["outbox"] = _normalize_outbox(out.get("outbox"))
    out["found"] = [item for item in (out.get("found") or []) if isinstance(item, (dict, str))]
    out["timeline"] = [item for item in (out.get("timeline") or []) if isinstance(item, dict)]
    out["duties"] = [item for item in (out.get("duties") or []) if isinstance(item, dict)]
    out.setdefault("members", [])
    out.setdefault("owner", WHO_MANAGER)
    out.setdefault("due", None)
    out.setdefault("request_id", None)
    out["status"] = str(out.get("status") or "open")
    return out


def view(delo: dict, sources: Sources | None = None, now: int | None = None) -> dict:
    """Вид дела для API: ``needs_you`` пересчитаны, статус — по правилу чтения."""
    out = copy.deepcopy(_clean(delo))
    out["needs_you"] = needs_you(out, sources, now)
    out["status"] = derived_status(out)
    return out


# --------------------------------------------------------------------------- чтение
def list_delos(cfg, room_id, sources: Sources | None = None, now: int | None = None) -> dict:
    """Все дела комнаты: ``{items:[Delo…], generated_at}`` (контракт §1)."""
    now = _now(now)
    data = _load(cfg, room_id)
    items = [view(item, sources, now) for item in data["items"]]
    items.sort(key=lambda item: (-_int(item.get("updated_at")), str(item.get("id"))))
    return {"items": items, "generated_at": now}


def find(cfg, delo_id: str) -> tuple[str, dict]:
    """Найти дело по id без комнаты: ``(room_id, delo)``; нет — 404 по-русски."""
    delo_id = str(delo_id or "").strip()
    if not DELO_ID_RE.match(delo_id):
        raise DeloError("Некорректный id дела", field="id", value=delo_id[:40])
    state_dir = Path(cfg.state_dir)
    if not state_dir.is_dir():
        raise DeloError("Дело не найдено", http=404, code="not_found", detail={"id": delo_id})
    for folder in sorted(state_dir.iterdir()):
        if not folder.is_dir() or not (folder / STATE_FILE).exists():
            continue
        for item in _load(cfg, folder.name)["items"]:
            if str(item.get("id")) == delo_id:
                return folder.name, _clean(item)
    raise DeloError("Дело не найдено", http=404, code="not_found", detail={"id": delo_id})


def get(cfg, delo_id: str, sources: Sources | None = None, now: int | None = None) -> dict:
    _room_id, delo = find(cfg, delo_id)
    return view(delo, sources, now)


def get_in_room(cfg, room_id, delo_id: str) -> dict:
    """Дело внутри известной комнаты (без обхода каталогов)."""
    delo_id = str(delo_id or "").strip()
    if not DELO_ID_RE.match(delo_id):
        raise DeloError("Некорректный id дела", field="id", value=delo_id[:40])
    for item in _load(cfg, room_id)["items"]:
        if str(item.get("id")) == delo_id:
            return _clean(item)
    raise DeloError("Дело не найдено", http=404, code="not_found", detail={"id": delo_id})


# -------------------------------------------------------------------------- создание
@_serialized
def create(cfg, room: dict, payload: dict, sources: Sources | None = None,
           now: int | None = None) -> dict:
    """Открыть дело: из запроса (``request_id``) или вручную.

    ``room`` — словарь комнаты (нужны ``id`` и, для ленты, ``title``).
    Одно дело на запрос: повторное создание возвращает 409 с id существующего дела
    (иначе на один запрос появится десяток карточек-клонов).
    """
    now = _now(now)
    payload = payload if isinstance(payload, dict) else {}
    room_id = str((room or {}).get("id") or "").strip()
    if not room_id:
        raise DeloError("Не указана комната", field="room")

    request_id = payload.get("request_id")
    if request_id is not None and request_id != "":
        request_id = str(request_id).strip()
        if not REQUEST_ID_RE.match(request_id):
            raise DeloError("Некорректный id запроса", field="request_id", value=request_id[:60])
    else:
        request_id = None

    title = _one_line(payload.get("title"), TITLE_MAX)
    request_title = ""
    if request_id and sources is not None:
        try:
            found = sources.request(room_id, request_id)
        except Exception:                    # noqa: BLE001 — связь с запросом вторична
            found = None
        if isinstance(found, dict):
            request_title = _one_line(found.get("title"), TITLE_MAX)
    if not title:
        title = request_title
    if not title:
        raise DeloError("Нужно название дела", field="title", missing=["title"])

    data = _load(cfg, room_id)
    if len(data["items"]) >= MAX_DELOS:
        raise DeloError("В комнате уже %d дел — закройте лишние" % MAX_DELOS,
                        http=409, code="conflict", detail={"max": MAX_DELOS})
    if request_id:
        for item in data["items"]:
            if str(item.get("request_id") or "") == request_id:
                raise DeloError("Дело по этому запросу уже есть", http=409, code="conflict",
                                detail={"id": item.get("id"), "request_id": request_id})

    owner = _one_line(payload.get("owner"), 60) or WHO_MANAGER
    members = [_one_line(profile, 60) for profile in (payload.get("members") or [])
               if _one_line(profile, 60)]
    status = str(payload.get("status") or "open").strip().lower()
    if status not in STATUSES:
        status = "open"

    result_raw = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    delo = {
        "id": _new_delo_id({str(item.get("id")) for item in data["items"]}),
        "room": room_id,
        "title": title,
        "status": status,
        "due": _due(payload.get("due")),
        "owner": owner,
        "members": members,
        "updated_at": now,
        "request_id": request_id,
        "stages": _normalize_stages(payload.get("stages")),
        "waiting_on": _waiting_entries(payload.get("waiting_on"), now),
        "needs_you": [],                     # собирается при чтении
        "result": {"parts": _normalize_parts(result_raw.get("parts")),
                   "files": _normalize_files(result_raw.get("files"))},
        "outbox": _normalize_outbox(payload.get("outbox")),
        "found": copy.deepcopy(payload.get("found") or []),
        "timeline": [],
        "duties": copy.deepcopy(payload.get("duties") or []),
    }
    where = "из запроса %s" % request_id if request_id else "вручную"
    _tl(delo, now, WHO_MANAGER, "Дело открыто %s: %s" % (where, title), "created")
    if status in ("paused", "done", "cancelled"):
        _tl(delo, now, WHO_YOU, "Дело сразу помечено: %s" % status, "status")

    data["items"].append(delo)
    _save(cfg, room_id, data)
    return view(delo, sources, now)


def _waiting_entries(raw, now: int) -> list[dict]:
    """Ожидания «ждём: кого/чего»: ``{who, what, since}``; пустые строки отбрасываем."""
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        who = _one_line(item.get("who"), WHO_MAX)
        what = _one_line(item.get("what"), TEXT_MAX)
        if not who or not what:
            continue
        out.append({"who": who, "what": what, "since": _int(item.get("since")) or now})
    return out


# ------------------------------------------------------------------------- действия
def _require_item_id(payload: dict) -> str:
    """id пункта «ждёт вас»: ``item_id`` (контракт) или ``need_id`` (так зовёт интерфейс)."""
    for field in ("item_id", "need_id"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return _need_text(value, field, 80)
    raise DeloError("Нужно поле item_id", field="item_id", missing=["item_id"])


@_serialized
def act(cfg, room_id, delo_id: str, payload: dict, sources: Sources | None = None,
        delegate=None, now: int | None = None) -> dict:
    """Выполнить действие над делом и вернуть ``{ok, action, delo}``.

    ``delegate(request_id, body)`` — связь с существующим механизмом запроса
    (вопросы/решения/разрешения). Без него действия по пункту «ждёт вас» честно
    отказывают: 409, а не тихая запись «сделано» в деле.

    Запись одна: сначала действие (внешнее — до изменения файла), потом один
    ``write_json_atomic``; поэтому «сделано в деле, но не в запросе» не бывает.
    """
    now = _now(now)
    payload = payload if isinstance(payload, dict) else {}
    action = str(payload.get("action") or "").strip()
    if action not in ACTIONS:
        raise DeloError("Неизвестное действие", field="action", value=action[:40],
                        allowed=list(ACTIONS))
    room_id = str(room_id or "").strip()
    data = _load(cfg, room_id)
    target = None
    for item in data["items"]:
        if str(item.get("id")) == str(delo_id or "").strip():
            target = item
            break
    if target is None:
        raise DeloError("Дело не найдено", http=404, code="not_found", detail={"id": delo_id})
    delo = _clean(target)
    data["items"] = [delo if str(item.get("id")) == delo["id"] else item for item in data["items"]]

    delegated: dict | None = None
    if action in POINT_ACTIONS:
        delegated = _apply_point(delo, payload, action, sources, delegate, now)
    elif action == "set_stage":
        stage = _stage_of(delo, payload.get("stage_id") or payload.get("id"))
        title = payload.get("title")
        title_given = isinstance(title, str) and bool(title.strip())
        if title_given:
            stage["title"] = _need_text(title, "title", TITLE_MAX)
        state = payload.get("state")
        if state is None and not title_given:
            # Без state — «сделать этот этап текущим»: так подписана кнопка в интерфейсе.
            state = "now"
        if state is not None:
            set_stage_state(delo, stage["id"], state)
        _tl(delo, now, WHO_YOU, "Этап «%s»: %s" % (stage["title"], _stage_word(stage["state"])),
            "stage")
    elif action == "add_part":
        title = _need_text(payload.get("title"), "title", TITLE_MAX)
        state = str(payload.get("state") or "todo").strip().lower()
        if state not in PART_STATES:
            state = "todo"
        part = {"id": _new_part_id(delo), "title": title, "state": state}
        delo["result"]["parts"].append(part)
        _tl(delo, now, WHO_MANAGER, "В результат добавлена часть: %s" % title, "part")
    elif action == "set_part":
        part = _part_of(delo, payload.get("part_id") or payload.get("id"))
        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            part["title"] = _need_text(title, "title", TITLE_MAX)
        if payload.get("state") is not None:
            part["state"] = _need_choice(payload.get("state"), "state", PART_STATES)
            _tl(delo, now, WHO_YOU, "Часть «%s»: %s" % (part["title"], _part_word(part["state"])),
                "part")
        elif not (isinstance(title, str) and title.strip()):
            raise DeloError("Нужно поле state (agreed|discussing|todo)", field="state",
                            missing=["state"])
    elif action == "approve_outbox":
        entry = _outbox_of(delo, payload.get("outbox_id") or payload.get("id"))
        if entry.get("state") not in ("draft", "failed"):
            raise DeloError("Утвердить можно только черновик", http=409, code="conflict",
                            detail={"state": entry.get("state"), "outbox_id": entry["id"]})
        entry["state"] = "approved"
        entry.pop("error", None)
        _tl(delo, now, WHO_YOU, "Исходящее «%s» утверждено — можно отправлять"
            % _outbox_label(entry), "outbox")
    elif action == "edit_outbox":
        entry = _outbox_of(delo, payload.get("outbox_id") or payload.get("id"))
        if entry.get("state") in ("sent", "cancelled"):
            raise DeloError("Отправленное и отменённое не правится", http=409, code="conflict",
                            detail={"state": entry.get("state"), "outbox_id": entry["id"]})
        changed = _edit_outbox(entry, payload)
        if not changed:
            raise DeloError("Нечего править: to, subject, body или attachments", field="body")
        if entry.get("state") == "approved":
            entry["state"] = "draft"          # правка после «да» — снова на утверждение
            _tl(delo, now, WHO_YOU, "Исходящее «%s» изменено — снова черновик, нужно «да»"
                % _outbox_label(entry), "outbox")
        else:
            _tl(delo, now, WHO_MANAGER, "Исходящее «%s» поправлено" % _outbox_label(entry),
                "outbox")
    elif action == "cancel_outbox":
        entry = _outbox_of(delo, payload.get("outbox_id") or payload.get("id"))
        if entry.get("state") == "sent":
            raise DeloError("Отправленное не отменяется", http=409, code="conflict",
                            detail={"state": "sent", "outbox_id": entry["id"]})
        entry["state"] = "cancelled"
        _tl(delo, now, WHO_YOU, "Исходящее «%s» отменено" % _outbox_label(entry), "outbox")
    elif action == "mark_sent":
        entry = _outbox_of(delo, payload.get("outbox_id") or payload.get("id"))
        if entry.get("state") != "approved":
            raise DeloError("Отметить отправленным можно только утверждённое", http=409,
                            code="conflict", detail={"state": entry.get("state"),
                                                    "outbox_id": entry["id"]})
        error = _one_line(payload.get("error"), TEXT_MAX)
        if error:
            entry["state"] = "failed"
            entry["error"] = error
            entry["sent_at"] = now
            _tl(delo, now, WHO_SYSTEM, "Отправка «%s» не удалась: %s"
                % (_outbox_label(entry), error), "outbox")
        else:
            entry["state"] = "sent"
            entry["sent_at"] = now
            entry.pop("error", None)
            _tl(delo, now, WHO_SYSTEM, "Исходящее «%s» отмечено отправленным"
                % _outbox_label(entry), "outbox")
    elif action == "pause":
        _set_status(delo, "paused", now, "Дело поставлено на паузу")
    elif action == "resume":
        if derived_status(delo) != "paused":
            raise DeloError("Дело не на паузе", http=409, code="conflict",
                            detail={"status": derived_status(delo)})
        delo["status"] = "open"
        _tl(delo, now, WHO_YOU, "Дело продолжено", "status")
    elif action == "done":
        _set_status(delo, "done", now, "Дело закрыто")
    elif action == "add_waiting":
        who = _need_text(payload.get("who"), "who", WHO_MAX)
        what = _need_text(payload.get("what"), "what", TEXT_MAX)
        delo["waiting_on"].append({"who": who, "what": what, "since": now})
        _tl(delo, now, WHO_MANAGER, "Ждём «%s»: %s" % (who, what), "waiting")
    elif action == "clear_waiting":
        cleared = _clear_waiting(delo, payload)
        if not cleared:
            raise DeloError("Нечего снимать: нет такого ожидания", http=404, code="not_found")
        _tl(delo, now, WHO_YOU, "Снято ожидание: %s" % "; ".join(cleared), "waiting")
    else:                                    # pragma: no cover — ACTIONS и ветки сверены тестом
        raise DeloError("Действие не реализовано", http=501, code="not_implemented",
                        detail={"action": action})

    delo["updated_at"] = now
    _save(cfg, room_id, data)
    result = {"ok": True, "action": action, "delo": view(delo, sources, now)}
    if delegated is not None:
        result["delegated"] = delegated
    return result


def _stage_word(state: str) -> str:
    return {"done": "сделано", "now": "сейчас", "todo": "ещё нет"}.get(str(state), str(state))


def _part_word(state: str) -> str:
    return {"agreed": "согласовано", "discussing": "обсуждаем", "todo": "не начато"}.get(
        str(state), str(state))


def _outbox_label(entry: dict) -> str:
    return _one_line(entry.get("subject") or entry.get("to") or entry.get("kind"), 80)


def _set_status(delo: dict, status: str, now: int, text: str) -> None:
    if derived_status(delo) == status:
        raise DeloError("Дело уже в этом состоянии", http=409, code="conflict",
                        detail={"status": status})
    delo["status"] = status
    _tl(delo, now, WHO_YOU, text, "status")


def _edit_outbox(entry: dict, payload: dict) -> bool:
    """Правка исходящего; возвращает True, если что-то изменилось."""
    changed = False
    for field, limit in (("to", TITLE_MAX), ("subject", TITLE_MAX)):
        if payload.get(field) is not None:
            value = _need_text(payload.get(field), field, limit, required=False)
            if value != str(entry.get(field) or ""):
                entry[field] = value
                changed = True
    if payload.get("body") is not None:
        body = _need_text(payload.get("body"), "body", BODY_MAX, required=False)
        if body != str(entry.get("body") or ""):
            entry["body"] = body
            changed = True
    if payload.get("attachments") is not None:
        raw = payload.get("attachments")
        if not isinstance(raw, list):
            raise DeloError("attachments — список путей", field="attachments")
        paths = [_one_line(path, PATH_MAX) for path in raw if _one_line(path, PATH_MAX)]
        if paths != list(entry.get("attachments") or []):
            entry["attachments"] = paths
            changed = True
    return changed


def _clear_waiting(delo: dict, payload: dict) -> list[str]:
    """Снять ожидания: ``all:true`` / ``who`` / ``what`` / ``index``. Возвращает что снято."""
    entries = delo.get("waiting_on") or []
    who = _one_line(payload.get("who"), WHO_MAX)
    what = _one_line(payload.get("what"), TEXT_MAX)
    only_index = payload.get("index")
    cleared: list[str] = []
    keep: list[dict] = []
    for number, entry in enumerate(entries):
        match = True
        if only_index is not None and payload.get("all") is not True:
            match = _int(only_index, -1) == number
        else:
            if who:
                match = match and str(entry.get("who")) == who
            if what:
                match = match and str(entry.get("what")) == what
            if payload.get("all") is not True and not who and not what:
                match = False
        if match:
            cleared.append("%s — %s" % (entry.get("who"), entry.get("what")))
        else:
            keep.append(entry)
    if cleared:
        delo["waiting_on"] = keep
    return cleared


def _apply_point(delo: dict, payload: dict, action: str, sources: Sources | None,
                 delegate, now: int) -> dict:
    """Действие по пункту «ждёт вашего решения» — через механизм запроса (делегат).

    Пункт ищется в ПЕРЕСЧИТАННЫХ ``needs_you``: источник — единственная истина, а
    дело только показывает. Потом вызывается ``delegate`` (штатный обработчик
    действия запроса), и лишь затем пишется лента дела.
    """
    request_id = str(delo.get("request_id") or "")
    if not request_id:
        raise DeloError("У дела нет запроса: действие по пункту недоступно", http=409,
                        code="conflict")
    if delegate is None:
        raise DeloError("Нет связи с запросом: действие по пункту недоступно", http=409,
                        code="channel_unavailable")
    item_id = _require_item_id(payload)
    points = needs_you(delo, sources, now)
    point = next((item for item in points if item.get("id") == item_id), None)
    if point is None:
        raise DeloError("Пункт уже не ждёт решения", http=409, code="conflict",
                        detail={"item_id": item_id})
    if action not in (point.get("actions") or []):
        raise DeloError("Этот пункт не поддерживает действие", http=400, code="bad_request",
                        field="action", value=action, allowed=list(point.get("actions") or []))
    source = point.get("source") or {}
    kind = str(source.get("kind") or "")
    text = _need_text(payload.get("text"), "text", TEXT_MAX, required=False)
    if not text and payload.get("option_id") is not None:
        # Интерфейс присылает выбранный вариант кнопкой: берём его подпись как ответ.
        option_id = _need_text(payload.get("option_id"), "option_id", 40, required=False)
        option = next((item for item in point.get("options") or []
                       if str(item.get("id")) == option_id), None)
        if option is None:
            raise DeloError("Такого варианта ответа нет", http=400, code="bad_request",
                            field="option_id", value=option_id, item_id=item_id)
        text = _one_line(option.get("label"), TEXT_MAX)
    body: dict = {}
    if kind == "open_question":
        if not text:
            raise DeloError("Нужен текст ответа", field="text", missing=["text"])
        body = {"action": "answer", "question_id": source.get("question_id"), "text": text}
    elif kind == "decision":
        body = {"action": "decision", "decision_id": source.get("decision_id"),
                "decision": "accept" if action == "approve" else "reject"}
        if text:
            body["text"] = text
    elif kind == "next_step":
        body = {"action": "step_agree" if action == "approve" else "step_drop",
                "step_id": source.get("step_id")}
        if text:
            body["text"] = text
    elif kind == "approval":
        body = {"action": "approval", "approval_id": source.get("approval_id"),
                "decision": "once" if action == "approve" else "deny"}
        if text:
            body["note"] = text
    else:                                    # pragma: no cover — kinds сверены тестом
        raise DeloError("Вид пункта не поддержан", http=409, code="conflict",
                        detail={"kind": kind})
    delegated = delegate(request_id, body)
    word = {"approve": "утверждено", "reject": "отклонено", "answer": "отвечено"}[action]
    _tl(delo, now, WHO_YOU, "«%s» — %s" % (_one_line(point.get("text"), 120), word), "decision")
    return delegated if isinstance(delegated, dict) else {"ok": True}


__all__ = [
    "DeloError", "Sources", "NullSources", "ACTIONS", "POINT_ACTIONS", "STAGE_TITLES",
    "STATUSES", "STAGE_STATES", "PART_STATES", "OUTBOX_KINDS", "OUTBOX_STATES", "NEEDS_KINDS",
    "STATE_FILE", "TIMELINE_MAX", "default_stages", "set_stage_state", "current_stage",
    "needs_you", "derived_status", "store_path", "list_delos", "find", "get", "get_in_room",
    "create", "act", "view",
]
