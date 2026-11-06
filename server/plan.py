"""Карта проекта комнаты: направления, проверка, очередь и запуск (WAVE 9).

Что здесь (§1–§3 ТЗ волны 9):
* модель ``Plan``/``Stream``/``LogItem`` и файл ``data/state/<room>/plan.json``
  (запись атомарная, все изменения — под блокировкой файла);
* ``validate_plan`` — проверка блока ``plan`` от руководителя с русскими
  предупреждениями (не исключение): лимит 8 направлений, исполнители только из
  состава комнаты, зависимости без циклов, заголовок 3–120, корректный срок;
* ``ingest_plan`` — принять карту из ответа руководителя (черновик);
* ``approve`` — единственный путь к запуску: направления встают в очередь;
* ``tick`` — синхронизация статусов, сторож лимита (A8) и запуск того, что можно
  запустить (``depends_on`` готовы, ``running`` меньше предела комнаты);
* ``apply_patch`` — правки карты по правилу A6 (что автоматически, что только с
  подтверждением) с записью в журнал и причиной.

Границы: под-запросы создаются ТОЛЬКО здесь и ТОЛЬКО через
``engine_write.create_request`` (он же контекст-блок комнаты). Реальный
диспетчер не вызывается никогда — в тестах CLI подменяется.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

try:  # пакетный импорт (server.plan)
    from . import statefiles as sf
except ImportError:  # pragma: no cover - плоский импорт (python server/app.py)
    import statefiles as sf  # type: ignore

PLAN_FILE = "plan.json"
PLAN_VERSION = 1

MAX_STREAMS = 8              # A4: больше восьми направлений карта нечитаема
TITLE_MIN, TITLE_MAX = 3, 120
GOAL_MAX = 600
MEMBERS_MAX = 8
DUE_MIN_YEAR = 2000
DUE_MAX_YEAR = 2100

MAX_PARALLEL_DEFAULT = 2     # A2
MAX_PARALLEL_LIMIT = 4

STREAM_STATUSES = ("proposed", "queued", "running", "waiting_quota", "review", "done", "dropped")
STREAM_STATUS_RU = {"proposed": "предложено", "queued": "в очереди", "running": "идёт",
                    "waiting_quota": "ждёт лимит", "review": "на проверке",
                    "done": "готово", "dropped": "отложено"}
LOG_KINDS = ("created", "changed", "added", "launched", "dropped", "quota")
LOG_LIMIT = 200
LOG_TEXT_MAX = 400

#: Признаки лимита провайдера в исходе детей (A8). Сравнение без учёта регистра.
QUOTA_MARKERS = ("429", "usage limit", "session usage limit", "rate limit", "too many requests")


class PlanError(Exception):
    """Доменная ошибка карты: код API.md §1 + русский текст (4xx, не 5xx)."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.message_ru = str(message)
        self.message = str(message)
        self.code = code
        self.detail = detail if isinstance(detail, dict) else {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message_ru, "detail": self.detail}


# --------------------------------------------------------------- служебное
def _now() -> int:
    return int(time.time())


def plan_path(cfg, room_id) -> Path:
    return Path(cfg.state_dir) / str(room_id) / PLAN_FILE


def empty_plan(brief_root_id=None) -> dict:
    return {"version": PLAN_VERSION, "brief_root_id": brief_root_id or None,
            "updated_at": _now(), "status": "draft", "streams": [], "log": []}


def _modules() -> dict:
    """Соседние модули — лениво (плоский и пакетный импорт, чужие файлы могут меняться)."""
    out = {}
    for name in ("engine_read", "engine_write", "registry", "rooms"):
        try:
            out[name] = __import__("server." + name, fromlist=["*"])
        except ImportError:  # pragma: no cover
            try:
                out[name] = __import__(name)
            except ImportError:
                out[name] = None
    return out


def _get_room(cfg, room_id) -> dict:
    rooms_mod = _modules().get("rooms")
    room = None
    if rooms_mod is not None:
        try:
            room = rooms_mod.get_room(cfg, room_id)
        except Exception:  # noqa: BLE001 - реестр комнат не должен ронять карту
            room = None
    return room if isinstance(room, dict) else {}


# ------------------------------------------------------- настройки комнаты
def project_mode(room: dict) -> bool:
    """Включён ли проектный режим у комнаты (у старых комнат по умолчанию нет)."""
    return bool((room or {}).get("project_mode", False))


def max_parallel(room: dict) -> int:
    """Сколько направлений может идти одновременно (A2, 1–4, по умолчанию 2)."""
    try:
        value = int((room or {}).get("max_parallel_streams", MAX_PARALLEL_DEFAULT))
    except (TypeError, ValueError):
        return MAX_PARALLEL_DEFAULT
    return value if 1 <= value <= MAX_PARALLEL_LIMIT else MAX_PARALLEL_DEFAULT


def launch_mode(room: dict) -> str:
    """Режим запуска комнаты: «план → Запустить» (по умолчанию) или сразу."""
    mode = str((room or {}).get("launch_mode") or "plan_first")
    return mode if mode in ("plan_first", "auto") else "plan_first"


def room_profiles(room: dict) -> set:
    """Состав комнаты: участники + руководитель + критик."""
    out = set()
    for member in (room or {}).get("members") or []:
        if isinstance(member, dict) and member.get("profile"):
            out.add(str(member["profile"]))
        elif isinstance(member, str) and member:
            out.add(member)
    for key in ("orchestrator", "critic"):
        value = (room or {}).get(key)
        if value:
            out.add(str(value))
    return out


#: роль по-русски для профиля, когда в комнате нет его описания (технический id наружу не отдаём)
PROFILE_ROLE_RU = {"director": "руководитель комнаты", "ops": "операции", "cfo": "экономика",
                   "marketing": "маркетинг", "strategy": "стратегия", "lawyer": "юрист",
                   "critic": "критик", "runner": "сбор фактов", "coder": "программист",
                   "surveyor": "исследователь", "analyst": "аналитик", "default": "руководитель комнаты"}
OWNER_LABEL_MAX = 32
_SLUG_RE = re.compile(r"^(?:biz|room)-")
_NOTE_HEAD_RE = re.compile(r"[:.,;]|\s[—–]\s")


def owner_label(room: dict, profile, limit: int = OWNER_LABEL_MAX) -> str:
    """Русская подпись исполнителя для интерфейса: «Операции», «Юрист», «руководитель комнаты».

    Порядок источников: описание участника в комнате («Юрист: ИП, СЭС» → «Юрист»),
    роль в составе, словарь профессий по слагу профиля. Техническое имя (`biz-ops`)
    наружу не выводим — оно остаётся в поле ``owner``.
    """
    raw = _text(profile, 64)
    if not raw:
        return ""
    if raw == "mikhail":
        return "Михаил"
    room = room if isinstance(room, dict) else {}
    member = None
    for item in room.get("members") or []:
        if isinstance(item, dict) and str(item.get("profile") or "") == raw:
            member = item
            break
    if member is not None:
        head = _NOTE_HEAD_RE.split(_text(member.get("note"), 120))[0].strip(" .—-")
        if head and 1 < len(head) <= limit:
            return head
        role = str(member.get("role") or "")
        if role in ("critic", "observer"):
            return "критик" if role == "critic" else "наблюдатель"
    if raw == str(room.get("orchestrator") or ""):
        return "руководитель комнаты"
    if raw == str(room.get("critic") or ""):
        return "критик"
    slug = _SLUG_RE.sub("", raw)
    label = PROFILE_ROLE_RU.get(slug, slug)[:limit]
    return label[:1].upper() + label[1:] if label else label


def fill_labels(plan: dict | None, room: dict) -> dict | None:
    """Проставить ``owner_label`` направлениям для интерфейса (обратно совместимо)."""
    for stream in (plan or {}).get("streams") or []:
        if not isinstance(stream, dict):
            continue
        label = owner_label(room, stream.get("owner"))
        if label and stream.get("owner_label") != label:
            stream["owner_label"] = label
    return plan


# ------------------------------------------------------------------- модель
def _text(value, limit: int = 0) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit] if limit else text


def _str_list(value) -> list:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = _text(item, 64)
        if text and text not in out:
            out.append(text)
    return out


def _norm_stream(stream: dict) -> dict:
    """Канонический Stream; поля вне схемы отбрасываются."""
    stream = stream if isinstance(stream, dict) else {}
    status = str(stream.get("status") or "proposed")
    if status not in STREAM_STATUSES:
        status = "proposed"
    created_by = str(stream.get("created_by") or "manager")
    if created_by not in ("manager", "mikhail"):
        created_by = "manager"
    request_id = stream.get("request_id")
    return {
        "id": _text(stream.get("id"), 16) or "",
        "title": _text(stream.get("title"), TITLE_MAX),
        "goal": _text(stream.get("goal"), GOAL_MAX),
        "owner": _text(stream.get("owner"), 64),
        "owner_label": _text(stream.get("owner_label"), OWNER_LABEL_MAX),
        "members": _str_list(stream.get("members"))[:MEMBERS_MAX],
        "depends_on": _str_list(stream.get("depends_on")),
        "due": _text(stream.get("due"), 10) or None,
        "status": status,
        "request_id": _text(request_id, 64) or None,
        "reason": _text(stream.get("reason"), 300),
        "created_by": created_by,
        # W14-D §4: из какого вопроса владельца возникло направление (связка вопрос↔шаг)
        "from_question": _text(stream.get("from_question"), 40) or None,
    }


def _norm_log(item: dict) -> dict:
    item = item if isinstance(item, dict) else {}
    kind = str(item.get("kind") or "changed")
    if kind not in LOG_KINDS:
        kind = "changed"
    who = str(item.get("who") or "system")
    if who not in ("manager", "mikhail", "system"):
        who = "system"
    return {"at": int(item.get("at") or _now()), "who": who, "kind": kind,
            "text": _text(item.get("text"), LOG_TEXT_MAX),
            "stream_id": _text(item.get("stream_id"), 16) or None}


def _norm_plan(plan: dict) -> dict:
    plan = plan if isinstance(plan, dict) else {}
    status = str(plan.get("status") or "draft")
    if status not in ("draft", "approved", "running", "done"):
        status = "draft"
    streams, seen = [], set()
    for raw in plan.get("streams") or []:
        stream = _norm_stream(raw)
        if not stream["id"] or stream["id"] in seen:
            continue
        seen.add(stream["id"])
        streams.append(stream)
    log = [_norm_log(item) for item in (plan.get("log") or []) if isinstance(item, dict)]
    return {"version": PLAN_VERSION, "brief_root_id": _text(plan.get("brief_root_id"), 64) or None,
            "updated_at": int(plan.get("updated_at") or _now()), "status": status,
            "streams": streams, "log": log[-LOG_LIMIT:]}


def load(cfg, room_id) -> dict | None:
    """Карта комнаты или None. Битый файл копируется рядом и считается отсутствующим."""
    path = plan_path(cfg, room_id)
    data, info = sf.read_json_doc(path, None)
    if info["state"] == "unreadable":
        raise sf.StateUnavailable(
            "Не удалось прочитать карту проекта комнаты «%s»: %s. Данные не изменены."
            % (room_id, info.get("error") or "файл занят"),
            {"room": str(room_id), "path": str(path)},
        )
    if info["state"] == "corrupt":
        sf.quarantine(path)
        return None
    if not isinstance(data, dict):
        return None
    return _norm_plan(data)


def save(cfg, room_id, plan: dict) -> dict:
    """Атомарно записать карту (под блокировкой файла) и вернуть её канон."""
    path = plan_path(cfg, room_id)
    with sf.lock_for(path):
        clean = _norm_plan(dict(plan, updated_at=_now()))
        sf.write_json_atomic(path, clean)
    return clean


def _update(cfg, room_id, mutate):
    """Read-modify-write под блокировкой: ``mutate(plan) -> plan``."""
    path = plan_path(cfg, room_id)
    with sf.lock_for(path):
        plan = load(cfg, room_id) or empty_plan()
        result = mutate(plan)
        clean = _norm_plan(dict(result if isinstance(result, dict) else plan, updated_at=_now()))
        sf.write_json_atomic(path, clean)
    return clean


def _log(plan: dict, kind: str, text: str, who: str = "system", stream_id=None) -> None:
    plan.setdefault("log", []).append({"at": _now(), "who": who, "kind": kind,
                                       "text": _text(text, LOG_TEXT_MAX),
                                       "stream_id": stream_id})
    if len(plan["log"]) > LOG_LIMIT:
        del plan["log"][:len(plan["log"]) - LOG_LIMIT]


def _find(plan: dict, stream_id) -> dict | None:
    for stream in (plan or {}).get("streams") or []:
        if stream.get("id") == stream_id:
            return stream
    return None


def _next_id(plan: dict) -> str:
    used = {str(s.get("id") or "") for s in (plan or {}).get("streams") or []}
    for index in range(1, MAX_STREAMS + 1):
        candidate = "s%d" % index
        if candidate not in used:
            return candidate
    return "s%d" % (len(used) + 1)


# -------------------------------------------------------------- проверка
def _valid_due(value) -> str | None:
    text = _text(value, 10)
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return text if DUE_MIN_YEAR <= parsed.year <= DUE_MAX_YEAR else None


def _cycles(ids: list, deps: dict) -> set:
    """Направления, участвующие в цикле зависимостей (DFS по серому/чёрному)."""
    bad: set = set()
    state: dict = {}

    def walk(node: str, stack: list) -> None:
        state[node] = 1
        stack.append(node)
        for dep in deps.get(node, []):
            if state.get(dep) == 1:
                bad.update(stack[stack.index(dep):])
            elif state.get(dep) is None:
                walk(dep, stack)
        stack.pop()
        state[node] = 2

    for node in ids:
        if state.get(node) is None:
            walk(node, [])
    return bad


def validate_plan(room: dict, streams, plan: dict | None = None) -> dict:
    """Проверить блок ``plan`` от руководителя.

    Возвращает ``{"streams": [...], "warnings": [...]}``: годные направления (id
    присвоены приложением, статус ``proposed``) и русские предупреждения.
    Исключений нет — непригодное направление отбрасывается с объяснением.
    """
    warnings: list = []
    if streams is None or streams == {}:
        return {"streams": [], "warnings": []}
    if not isinstance(streams, list):
        return {"streams": [], "warnings": ["Блок «plan» должен быть списком частей работы — карта не изменилась."]}
    if not streams:
        return {"streams": [], "warnings": []}

    profiles = room_profiles(room)
    known = {str(s.get("id")) for s in (plan or {}).get("streams") or []}
    accepted: list = []
    titles: set = set()
    for raw in streams:
        if not isinstance(raw, dict):
            warnings.append("Направление без описания пропущено.")
            continue
        title = _text(raw.get("title") or raw.get("name"), TITLE_MAX + 40)
        if len(title) < TITLE_MIN:
            warnings.append("Название направления «%s» короче %d знаков — пропущено."
                            % (title or "без названия", TITLE_MIN))
            continue
        if len(title) > TITLE_MAX:
            warnings.append("Название «%s…» длиннее %d знаков — сокращено." % (title[:40], TITLE_MAX))
            title = title[:TITLE_MAX]
        if title.lower() in titles:
            warnings.append("Часть работы «%s» повторяется — вторая пропущена." % title)
            continue
        owner = _text(raw.get("owner"), 64)
        if profiles and owner and owner not in profiles:
            warnings.append("Исполнитель «%s» не в составе комнаты — направление «%s» пропущено."
                            % (owner, title))
            continue
        members = _str_list(raw.get("members"))
        if profiles:
            foreign = [m for m in members if m not in profiles]
            members = [m for m in members if m in profiles]
            if foreign:
                warnings.append("В направлении «%s» убраны исполнители не из комнаты: %s."
                                % (title, ", ".join(foreign[:4])))
        if not owner and members:
            owner = members[0]
        if not owner and profiles:
            owner = (room.get("orchestrator") or sorted(profiles)[0])
        due = raw.get("due")
        due_text = _valid_due(due)
        if _text(due) and not due_text:
            warnings.append("Срок «%s» у направления «%s» не похож на дату — оставлен пустым."
                            % (_text(due, 20), title))
        titles.add(title.lower())
        accepted.append({"id": _text(raw.get("id"), 16) or "",
                         "title": title,
                         "goal": _text(raw.get("goal") or raw.get("text"), GOAL_MAX),
                         "owner": owner or "",
                         "owner_label": owner_label(room, owner),
                         "members": members[:MEMBERS_MAX],
                         "depends_on": _str_list(raw.get("depends_on")),
                         "due": due_text,
                         "reason": _text(raw.get("reason"), 300),
                         "status": "proposed", "request_id": None,
                         "created_by": "manager"})
    if not accepted:
        return {"streams": [], "warnings": warnings}
    if len(accepted) > MAX_STREAMS:
        warnings.append("Направлений больше восьми (%d) — лишние отброшены." % len(accepted))
        accepted = accepted[:MAX_STREAMS]

    # id присваивает приложение: чужие/повторные id заменяем на свободные
    used: set = set(known)
    order: list = []
    for stream in accepted:
        sid = stream["id"]
        if not sid or sid in used:
            sid = _next_id({"streams": [{"id": i} for i in used] + [{"id": o} for o in order]})
            stream["id"] = sid
        used.add(sid)
        order.append(sid)
    index = {s["id"] for s in accepted} | known

    # зависимости: только существующие и без циклов
    deps: dict = {}
    for stream in accepted:
        clean = []
        for dep in stream["depends_on"]:
            if dep == stream["id"]:
                warnings.append("Часть работы «%s» не может зависеть от себя — связь убрана." % stream["title"])
                continue
            if dep not in index:
                warnings.append("Зависимость «%s» у направления «%s» не найдена — связь убрана."
                                % (dep, stream["title"]))
                continue
            clean.append(dep)
        stream["depends_on"] = clean
        deps[stream["id"]] = clean
    for stream in accepted:
        deps.setdefault(stream["id"], stream["depends_on"])
    bad = _cycles([s["id"] for s in accepted], deps)
    if bad:
        for stream in accepted:
            if stream["id"] in bad:
                stream["depends_on"] = [d for d in stream["depends_on"] if d not in bad]
        warnings.append("В зависимостях направлений был цикл (%s) — связи, замыкавшие круг, убраны."
                        % ", ".join(sorted(bad)))
    return {"streams": [ _norm_stream(s) for s in accepted ], "warnings": warnings}


# --------------------------------------------------------------- бриф/карта
def streams_summary(cfg, room_id) -> dict | None:
    """Короткая сводка для страницы брифа: всего/идёт/готово и статус карты."""
    try:
        plan = load(cfg, room_id)
    except Exception:  # noqa: BLE001 - сводка необязательна
        return None
    if not plan:
        return None
    streams = plan.get("streams") or []
    return {"total": len(streams),
            "running": sum(1 for s in streams if s.get("status") in ("running", "waiting_quota")),
            "done": sum(1 for s in streams if s.get("status") == "done"),
            "status": plan.get("status")}


def link_for(cfg, room_id, root_id) -> dict:
    """Связь под-запроса с картой: ``plan_link`` (или None) + ``streams_summary`` брифа."""
    out = {"plan_link": None, "streams_summary": None}
    try:
        plan = load(cfg, room_id)
    except Exception:  # noqa: BLE001 - страница запроса важнее карты
        return out
    if not plan:
        return out
    for stream in plan.get("streams") or []:
        if root_id and stream.get("request_id") == str(root_id):
            out["plan_link"] = {"stream_id": stream.get("id"),
                                "title": stream.get("title") or "",
                                "status": stream.get("status")}
            break
    if plan.get("brief_root_id") and str(plan.get("brief_root_id")) == str(root_id):
        out["streams_summary"] = streams_summary(cfg, room_id)
    return out


def detail_meta(cfg, room_id, root_id) -> dict:
    """Необязательные поля RequestDetail (WAVE 9 §4): ``brief``, ``plan_link``, сводка.

    Никогда не мешает открыть запрос: любая беда с картой — просто пустые поля.
    """
    meta = {"brief": False, "plan_link": None, "streams_summary": None}
    try:
        meta.update(link_for(cfg, room_id, root_id))
        registry = _modules().get("registry")
        if registry is not None and callable(getattr(registry, "get", None)):
            rec = registry.get(cfg, str(room_id), str(root_id)) or {}
            meta["brief"] = bool(rec.get("brief") or str(rec.get("kind") or "") in
                                 ("brief", "map_from_existing"))
    except Exception:  # noqa: BLE001 - страница запроса важнее карты
        pass
    return meta


# ---------------------------------------------------------------- ingest
def ingest_plan(cfg, room_id, root_id, metadata, room=None) -> dict:
    """Принять блок ``plan`` из ответа руководителя как черновик карты.

    Пустой/битый блок — не ошибка: карта не меняется, предупреждение возвращается
    вызывающему (``watcher``/``ingest``). Уже запущенную карту не перезаписываем.
    """
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        return {"ok": True, "changed": False, "skipped": "project_mode_off", "warnings": []}
    block = metadata.get("plan") if isinstance(metadata, dict) else None
    if isinstance(block, dict) and "streams" in block and not isinstance(block.get("streams"), list):
        return {"ok": True, "changed": False, "skipped": "broken_block",
                "warnings": ["Блок «plan» должен быть списком частей работы — карта не изменилась."]}
    if not isinstance(block, dict) or not isinstance(block.get("streams"), list):
        return {"ok": True, "changed": False, "skipped": "no_block", "warnings": []}
    current = load(cfg, room_id)
    if current and current.get("status") != "draft":
        return {"ok": True, "changed": False, "skipped": "plan_started",
                "warnings": ["Карта уже запущена — новые предложения применяйте как правку карты."],
                "plan": current}
    result = validate_plan(room, block.get("streams"), None)
    if not result["streams"]:
        return {"ok": True, "changed": False, "skipped": "nothing_valid",
                "warnings": result["warnings"], "plan": current}

    holder: dict = {"skipped": None, "warnings": list(result["warnings"])}

    def mutate(plan: dict) -> dict:
        # состояние перечитываем под блокировкой: пока шла проверка, карту могли запустить
        if plan.get("status") != "draft":
            holder["skipped"] = "plan_started"
            return plan
        streams = list(result["streams"])
        if len(streams) > MAX_STREAMS:          # страховка: больше восьми в карту не пускаем
            holder["warnings"].append("Направлений больше восьми (%d) — лишние отброшены."
                                      % len(streams))
            streams = streams[:MAX_STREAMS]
        plan["brief_root_id"] = str(root_id or plan.get("brief_root_id") or "") or None
        plan["streams"] = streams
        plan["status"] = "draft"
        _log(plan, "created", "Руководитель предложил карту: частей работы — %d." % len(streams),
             who="manager")
        return plan

    plan = _update(cfg, room_id, mutate)
    if holder["skipped"]:
        return {"ok": True, "changed": False, "skipped": "plan_started",
                "warnings": ["Карта уже запущена — новые предложения применяйте как правку карты."],
                "plan": plan}
    out = {"ok": True, "changed": True, "plan": plan, "warnings": holder["warnings"]}
    if launch_mode(room) == "auto":
        # режим комнаты «сразу»: направления идут в очередь без кнопки «Запустить» (WAVE 9 §6)
        started = _auto_start(cfg, room_id, room)
        out.update({"auto": True, "plan": started.get("plan") or plan,
                    "started": started.get("started") or [],
                    "queued": started.get("queued") or []})
    return out


# ------------------------------------------------------------------ запуск
def _engine_write():
    return _modules().get("engine_write")


def _summary(cfg, room: dict, rid: str) -> dict:
    reader = _modules().get("engine_read")
    if reader is None:
        return {}
    board = room.get("board") or "team"
    try:
        res = reader.request_summary(cfg, board, rid, room=room)
    except Exception:  # noqa: BLE001 - карточку мог ещё не увидеть движок
        return {}
    return res if isinstance(res, dict) else {}


def _detail(cfg, room: dict, rid: str) -> dict:
    reader = _modules().get("engine_read")
    if reader is None:
        return {}
    board = room.get("board") or "team"
    try:
        res = reader.request_detail(cfg, board, rid, room=room)
    except Exception:  # noqa: BLE001
        return {}
    return res if isinstance(res, dict) else {}


def _quota_strings(detail: dict) -> list:
    """Тексты исходов детей, в которых может быть причина лимита."""
    out: list = []
    for node in (detail or {}).get("tree") or []:
        if not isinstance(node, dict):
            continue
        error = node.get("error")
        if isinstance(error, dict):
            out.append(str(error.get("message") or ""))
            out.append(str(error.get("code") or ""))
        out.append(str(node.get("summary_md") or ""))
        for run in node.get("runs") or []:
            if not isinstance(run, dict):
                continue
            out.append(str(run.get("error") or ""))
            out.append(str(run.get("summary") or ""))
            meta = run.get("metadata")
            if isinstance(meta, dict):
                out.append(json.dumps(meta, ensure_ascii=False))
    return [text.lower() for text in out if text]


def quota_limited(cfg, detail) -> bool:
    """Провайдер упёрся в лимит (429 / usage limit) — направление ждёт лимит (A8).

    Читает только исходы детей из уже прочитанного дерева запроса; никаких
    сетевых вызовов и запусков. В тестах функция подменяется.
    """
    if not isinstance(detail, dict):
        return False
    return any(marker in text for text in _quota_strings(detail) for marker in QUOTA_MARKERS)


def _brief_title(cfg, room: dict, brief) -> str:
    """Название брифа для тела под-запроса (id остаётся только в служебной части)."""
    rid = _text(brief, 64)
    if not rid:
        return ""
    registry = _modules().get("registry")
    if registry is not None and callable(getattr(registry, "get", None)):
        try:
            rec = registry.get(cfg, str((room or {}).get("id") or ""), rid) or {}
            title = _text(rec.get("title") if isinstance(rec, dict) else "", TITLE_MAX)
            if title:
                return title
        except Exception:  # noqa: BLE001 - реестр вторичен, ниже попробуем сводку
            pass
    summary = _summary(cfg, room if isinstance(room, dict) else {}, rid)
    return _text((summary or {}).get("title"), TITLE_MAX)


def _stream_body(cfg, plan: dict, stream: dict, room: dict) -> str:
    """Тело под-запроса: цель направления, ссылка на бриф, карта, участники."""
    lines = ["# Направление: %s" % (stream.get("title") or "Без названия"), "",
             "Цель: %s" % (stream.get("goal") or stream.get("title") or "—")]
    brief = (plan or {}).get("brief_root_id")
    if brief:
        title = _brief_title(cfg, room, brief)
        if title:
            lines += ["", "Это направление из карты проекта. Главный запрос (бриф) — «%s»." % title]
        else:
            lines += ["", "Это направление из карты проекта. Главный запрос (бриф) — в комнате."]
    map_lines = []
    for item in (plan or {}).get("streams") or []:
        mark = "→ " if item.get("id") == stream.get("id") else "- "
        map_lines.append("%s%s — %s — %s" % (mark, item.get("title") or "—",
                                            item.get("owner") or "—",
                                            STREAM_STATUS_RU.get(item.get("status"), "")))
    if map_lines:
        lines += ["", "Карта проекта (кратко):"] + map_lines
    people = []
    for profile in [stream.get("owner")] + list(stream.get("members") or []):
        if profile and profile not in people:
            people.append(profile)
    if people:
        lines += ["", "Участники: %s." % ", ".join(people)]
    if stream.get("due"):
        lines.append("Срок: %s." % stream["due"])
    deps = []
    for dep_id in stream.get("depends_on") or []:
        dep = _find(plan, dep_id)
        deps.append(dep.get("title") if dep else dep_id)
    if deps:
        lines.append("Работа опирается на направления: %s." % ", ".join(str(d) for d in deps))
    lines += ["", "Работай в рамках этого направления; итог верни как обычно (summary, stage, next_steps).",
              "Если делишь работу на подзадачи — в тело каждой карточки перенеси суть, решения и "
              "ограничения направления: служебный блок контекста комнаты исполнитель подзадачи не видит."]
    service = []
    if brief:
        service.append("бриф %s" % brief)
    if stream.get("id"):
        service.append("направление %s" % stream.get("id"))
    if service:
        lines += ["", "Служебное (для поиска, в ответ не переносить): %s." % ", ".join(service)]
    return "\n".join(lines)


def _create_request(cfg, room: dict, stream: dict, plan: dict) -> dict:
    """Создать под-запрос направления (единственный путь запуска)."""
    writer = _engine_write()
    if writer is None or not callable(getattr(writer, "create_request", None)):
        raise PlanError("Движок записи недоступен — направление не запущено.", "conflict",
                        {"module": "engine_write"})
    room_id = str(room.get("id") or "")
    slug = "plan-%s-%s" % (room_id, stream.get("id"))
    key = "plan:%s:%s" % (room_id, stream.get("id"))
    body = _stream_body(cfg, plan, stream, room)
    title = (stream.get("title") or "Направление")[:120]
    try:
        return writer.create_request(cfg, room, slug, title, body, brief=False, idem_key=key,
                                     model_profile=stream.get("owner") or None) or {}
    except TypeError:  # соседняя версия create_request без brief/idem_key/model_profile
        try:
            return writer.create_request(cfg, room, slug, title, body, brief=False, idem_key=key) or {}
        except TypeError:
            return writer.create_request(cfg, room, slug, title, body) or {}


def _register(cfg, room: dict, stream: dict, root_id: str, slug: str, text: str) -> None:
    registry = _modules().get("registry")
    if registry is None or not callable(getattr(registry, "add_request", None)):
        return
    try:
        registry.add_request(cfg, str(room.get("id") or ""), {
            "root_id": root_id, "slug": slug, "title": stream.get("title") or "",
            "text": text, "created_at": _now(), "critic": False, "mode": "auto",
            "adopted": False, "extra_cards": [], "ingested": {}, "retargeted": False,
            "kind": "stream", "plan_link": {"stream_id": stream.get("id"),
                                            "title": stream.get("title") or ""}})
    except Exception:  # noqa: BLE001 - карточка уже создана, реестр вторичен
        pass


def _launch(cfg, room: dict, plan: dict, stream: dict) -> bool:
    """Запустить направление; False — не вышло (причина в журнале, очередь стоит)."""
    try:
        res = _create_request(cfg, room, stream, plan)
    except Exception as exc:  # noqa: BLE001 - одна карточка не должна ронять очередь
        message = getattr(exc, "message_ru", None) or getattr(exc, "message", None) or str(exc)
        stream["status"] = "queued"
        stream["reason"] = _text(message, 300)
        _log(plan, "changed", "Не удалось запустить «%s»: %s" % (stream.get("title"), _text(message, 200)),
             stream_id=stream.get("id"))
        return False
    root_id = str((res or {}).get("root_id") or (res or {}).get("id") or "")
    if not root_id:
        stream["status"] = "queued"
        stream["reason"] = "Движок не вернул id карточки."
        _log(plan, "changed", "Не удалось запустить «%s»: движок не вернул id." % stream.get("title"),
             stream_id=stream.get("id"))
        return False
    stream["request_id"] = root_id
    stream["status"] = "running"
    stream["reason"] = ""
    _log(plan, "launched", "Часть работы «%s» запущена (карточка %s)." % (stream.get("title"), root_id),
         stream_id=stream.get("id"))
    _register(cfg, room, stream, root_id, "plan-%s-%s" % (room.get("id"), stream.get("id")),
              stream.get("goal") or stream.get("title") or "")
    return True


def _sync_stream(cfg, room: dict, plan: dict, stream: dict) -> bool:
    """Статус направления по статусу под-запроса; True — были изменения."""
    rid = stream.get("request_id")
    if not rid:
        return False
    summary = _summary(cfg, room, rid)
    status = str((summary or {}).get("status") or "")
    mapping = {"done": "done", "reviewing": "review", "needs_you": "review",
               "working": "running", "synthesizing": "running", "queued": "running",
               "failed": "review"}
    fresh = mapping.get(status)
    changed = False
    if fresh and fresh != stream.get("status"):
        stream["status"] = fresh
        changed = True
    if fresh == "review" and status == "failed":
        reason = _text((summary or {}).get("status_reason"), 300)
        if reason and reason != stream.get("reason"):
            stream["reason"] = reason
            changed = True
    if fresh in ("running", "review"):
        detail = _detail(cfg, room, rid)
        if detail and quota_limited(cfg, detail):
            if stream.get("status") != "waiting_quota":
                stream["status"] = "waiting_quota"
                _log(plan, "quota", "Часть работы «%s» ждёт лимит провайдера."
                     % stream.get("title"), stream_id=stream.get("id"))
                return True
    elif stream.get("status") == "waiting_quota":
        stream["status"] = fresh if fresh in ("done", "review") else "running"
        _log(plan, "quota", "Лимит снят — часть работы «%s» продолжает работу."
             % stream.get("title"), stream_id=stream.get("id"))
        return True
    return changed


def _refresh_status(plan: dict) -> None:
    """Статус карты: черновик не трогаем, запущенная живёт по направлениям."""
    if plan.get("status") == "draft":
        return
    streams = plan.get("streams") or []
    if not streams:
        return
    statuses = {s.get("status") for s in streams}
    if statuses & {"running", "waiting_quota"}:
        plan["status"] = "running"
    elif "queued" in statuses:
        plan["status"] = "running"
    elif all(s in ("done", "dropped") for s in statuses):
        plan["status"] = "done"
    else:
        plan["status"] = "approved"


def _tick_plan(cfg, room: dict, plan: dict) -> list:
    """Один проход очереди над уже прочитанной картой; возвращает запущенные id."""
    started: list = []
    has_streams = bool(plan.get("streams"))
    if not has_streams:
        return started
    for stream in plan["streams"]:
        if stream.get("request_id") and stream.get("status") not in ("done", "dropped"):
            _sync_stream(cfg, room, plan, stream)
    # сторож лимита: очередь стоит, пока хоть одно направление ждёт лимит
    if any(s.get("status") == "waiting_quota" for s in plan["streams"]):
        _refresh_status(plan)
        return started
    limit = max_parallel(room)
    by_id = {s.get("id"): s for s in plan["streams"]}

    def ready(stream: dict) -> bool:
        deps = [by_id.get(dep) for dep in stream.get("depends_on") or []]
        return all(dep is not None and dep.get("status") == "done" for dep in deps)

    for stream in plan["streams"]:
        if stream.get("status") != "queued":
            continue
        if sum(1 for s in plan["streams"] if s.get("status") == "running") >= limit:
            break
        if not ready(stream):
            continue
        if _launch(cfg, room, plan, stream):
            started.append(stream.get("id"))
    _refresh_status(plan)
    return started


def _noop(room: dict) -> dict:
    return {"started": [], "plan": None, "skipped": "project_mode_off" if not project_mode(room) else "no_plan"}


def tick(cfg, room_id, room=None) -> dict:
    """Шаг очереди: синхронизировать статусы и запустить готовые направления.

    Вызывается из ``watcher`` (раз на комнату за тик) и после ``approve``.
    Никогда не бросает исключение наружу: очередь не должна ронять тик.
    """
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        return _noop(room)
    try:
        plan = load(cfg, room_id)
    except Exception:  # noqa: BLE001 - карту не прочитать: очередь просто стоит
        return {"started": [], "plan": None, "skipped": "unreadable"}
    if not plan:
        return {"started": [], "plan": plan, "skipped": "no_plan"}
    auto = launch_mode(room) == "auto"
    if plan.get("status") == "draft" and not auto:
        return {"started": [], "plan": plan, "skipped": "draft"}
    if plan.get("status") == "draft" and not any(s.get("status") == "proposed"
                                                 for s in plan.get("streams") or []):
        return {"started": [], "plan": plan, "skipped": "draft"}
    holder: dict = {"started": []}

    def mutate(current: dict) -> dict:
        if current.get("status") == "draft" and auto:
            # режим комнаты «сразу»: ждать «Запустить» не нужно (WAVE 9 §6)
            chosen = [s.get("id") for s in current.get("streams") or []
                      if s.get("status") == "proposed"]
            if chosen:
                for stream in current["streams"]:
                    if stream.get("id") in chosen:
                        stream["status"] = "queued"
                current["status"] = "approved"
                _log(current, "changed", AUTO_NOTE % len(chosen), who="system")
        holder["started"] = _tick_plan(cfg, room, current)
        return current

    try:
        plan = _update(cfg, room_id, mutate)
    except Exception as exc:  # noqa: BLE001 - ошибка записи: очередь останется на месте
        return {"started": [], "plan": plan, "error": str(exc)[:200]}
    return {"started": holder["started"], "plan": plan}


# ----------------------------------------------------------------- approve
def _selection(plan: dict, stream_ids) -> list:
    streams = plan.get("streams") or []
    if stream_ids in (None, "", "all", ["all"]):
        return [s for s in streams if s.get("status") in ("proposed", "queued")]
    if isinstance(stream_ids, str):
        stream_ids = [stream_ids]
    if not isinstance(stream_ids, (list, tuple)) or not stream_ids:
        raise PlanError("Нужно указать направления: список id или «all».", "bad_request",
                        {"allowed": ["s1", "all"]})
    known = {s.get("id") for s in streams}
    unknown = [str(sid) for sid in stream_ids if str(sid) not in known]
    if unknown:
        raise PlanError("В карте нет направлений: %s." % ", ".join(unknown), "not_found",
                        {"unknown": unknown, "known": sorted(x for x in known if x)})
    wanted = {str(sid) for sid in stream_ids}
    return [s for s in streams if s.get("id") in wanted and s.get("status") in ("proposed", "queued")]


def _queue_chosen(cfg, room_id, chosen_ids, room, note, who="mikhail") -> dict:
    """Взять выбранные направления в очередь, записать причину и запустить ``tick``."""
    chosen = [str(sid) for sid in chosen_ids or []]

    def mutate(current: dict) -> dict:
        for stream in current.get("streams") or []:
            if stream.get("id") in chosen and stream.get("status") == "proposed":
                stream["status"] = "queued"
        if current.get("status") == "draft":
            current["status"] = "approved"
        _log(current, "changed", note, who=who)
        return current

    plan = _update(cfg, room_id, mutate)
    result = tick(cfg, room_id, room=room)
    return {"plan": result.get("plan") or plan, "started": result.get("started") or [],
            "queued": chosen}


#: журнал запуска без подтверждения (``launch_mode: auto``, WAVE 9 §6)
AUTO_NOTE = "Запуск без подтверждения — режим комнаты «сразу»: частей работы — %d."


def _auto_start(cfg, room_id, room) -> dict:
    """``launch_mode: auto`` — направления сами встают в очередь сразу после приёма карты."""
    plan = load(cfg, room_id)
    if not plan:
        return {"plan": plan, "started": [], "queued": []}
    chosen = [s.get("id") for s in plan.get("streams") or [] if s.get("status") == "proposed"]
    if not chosen:
        return {"plan": plan, "started": [], "queued": []}
    return _queue_chosen(cfg, room_id, chosen, room, AUTO_NOTE % len(chosen), who="system")


def approve(cfg, room_id, stream_ids="all", room=None) -> dict:
    """«Запустить»: направления из очереди → под-запросы (ровно один раз на id)."""
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        raise PlanError("Проектный режим выключен — включите его в настройках комнаты.",
                        "conflict", {"room": str(room_id)})
    plan = load(cfg, room_id)
    if not plan:
        raise PlanError("Карты проекта пока нет — сначала напишите главный запрос.",
                        "not_found", {"room": str(room_id)})
    chosen = _selection(plan, stream_ids)
    if not chosen:
        return {"plan": plan, "started": [], "queued": []}
    chosen_ids = [s.get("id") for s in chosen]
    return _queue_chosen(cfg, room_id, chosen_ids, room,
                         "Запуск: направлений — %d (%s)." % (len(chosen_ids), ", ".join(chosen_ids)))


# ------------------------------------------------------------------- patch
PATCH_FIELDS = ("title", "goal", "due", "members", "status", "depends_on", "from_question")
NO_AUTO_STATUS = ("running", "waiting_quota", "queued")


def _patch_change(plan: dict, room: dict, item: dict, source: str, warnings: list) -> bool:
    stream_id = _text((item or {}).get("id") or (item or {}).get("stream_id"), 16)
    stream = _find(plan, stream_id)
    if stream is None:
        warnings.append("Правка для направления «%s» не применена: такого направления нет." % stream_id)
        return False
    fields = (item or {}).get("fields") if isinstance((item or {}).get("fields"), dict) else {
        k: v for k, v in (item or {}).items() if k in PATCH_FIELDS}
    changed = []
    for key in PATCH_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key == "title":
            title = _text(value, TITLE_MAX + 40)
            if len(title) < TITLE_MIN:
                warnings.append("Название направления «%s» короче трёх знаков — не изменено." % stream_id)
                continue
            stream["title"] = title[:TITLE_MAX]
        elif key == "goal":
            stream["goal"] = _text(value, GOAL_MAX)
        elif key == "due":
            due = _valid_due(value)
            if _text(value) and not due:
                warnings.append("Срок «%s» не похож на дату — у направления «%s» не изменён."
                                % (_text(value, 20), stream.get("title")))
                continue
            stream["due"] = due
        elif key == "members":
            members = _str_list(value)[:MEMBERS_MAX]
            allowed = room_profiles(room)
            stream["members"] = [m for m in members if not allowed or m in allowed]
        elif key == "depends_on":
            known = {s.get("id") for s in plan.get("streams") or []}
            deps = [d for d in _str_list(value) if d in known and d != stream.get("id")]
            stream["depends_on"] = deps
        elif key == "from_question":
            # W14-D §4: «зачем нужен» — ссылка на вопрос владельца
            stream["from_question"] = _text(value, 40) or None
        elif key == "status":
            status = str(value or "")
            if status in NO_AUTO_STATUS:
                stream["status"] = "proposed"
                warnings.append("Перезапуск направления «%s» требует подтверждения — нажмите «Запустить»."
                                % stream.get("title"))
            elif status in STREAM_STATUSES:
                stream["status"] = status
            else:
                warnings.append("Статус «%s» неизвестен — не изменён." % _text(status, 20))
                continue
        changed.append(key)
    if not changed:
        return False
    reason = _text((item or {}).get("reason") or "", 200)
    _log(plan, "changed", "Изменена часть работы «%s» (%s)%s."
         % (stream.get("title"), ", ".join(changed), (" — " + reason) if reason else ""),
         who=source, stream_id=stream.get("id"))
    return True


def apply_patch(cfg, room_id, patch, source="manager", room=None) -> dict:
    """Правки карты от руководителя по A6: без подтверждения — только текст/срок/люди/статус."""
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        return {"ok": False, "skipped": "project_mode_off", "plan": None, "warnings": []}
    if not isinstance(patch, dict):
        return {"ok": True, "changed": False, "plan": load(cfg, room_id), "warnings": []}
    who = "mikhail" if str(source) == "mikhail" else "manager"
    added: list = []
    changed: list = []
    dropped: list = []
    warnings: list = []

    def mutate(plan: dict) -> dict:
        note = _text(patch.get("note"), 200)
        for item in patch.get("change") or []:
            if not isinstance(item, dict):
                continue
            if _patch_change(plan, room, item, who, warnings):
                changed.append(_text(item.get("id") or item.get("stream_id"), 16))
        for raw in patch.get("add") or []:
            result = validate_plan(room, [raw], plan)
            warnings.extend(result["warnings"])
            for stream in result["streams"]:
                if len(plan.get("streams") or []) >= MAX_STREAMS:
                    warnings.append("Направлений уже восемь — «%s» не добавлено." % stream.get("title"))
                    continue
                stream["id"] = _next_id(plan)
                stream["status"] = "proposed"
                stream["created_by"] = who
                stream["reason"] = note or "Предложено руководителем"
                plan.setdefault("streams", []).append(stream)
                _log(plan, "added", "Новая часть работы «%s» — нужно «Запустить»%s."
                     % (stream.get("title"), (" — " + note) if note else ""),
                     who=who, stream_id=stream.get("id"))
                added.append(stream["id"])
        for item in patch.get("drop") or []:
            sid = _text(item.get("id") if isinstance(item, dict) else item, 16)
            stream = _find(plan, sid)
            if stream is None:
                warnings.append("Отложить «%s» не удалось: направления нет." % sid)
                continue
            reason = _text((item or {}).get("reason") if isinstance(item, dict) else "", 200)
            stream["status"] = "dropped"
            _log(plan, "dropped", "Часть работы «%s» отложена%s."
                 % (stream.get("title"), (" — " + reason) if reason else ""),
                 who=who, stream_id=stream.get("id"))
            dropped.append(sid)
        if note and not (added or changed or dropped):
            _log(plan, "changed", "Руководитель: %s" % note, who=who)
        _refresh_status(plan)
        return plan

    plan = _update(cfg, room_id, mutate)
    return {"ok": True, "changed": bool(added or changed or dropped), "plan": plan,
            "applied": {"add": added, "change": changed, "drop": dropped},
            "warnings": warnings}


# ---------------------------------------------------------------- вручную
def add_stream(cfg, room_id, data, source="mikhail", room=None) -> dict:
    """Добавить направление вручную (Михаил): всегда ``proposed``, требует «Запустить»."""
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        raise PlanError("Проектный режим выключен — включите его в настройках комнаты.",
                        "conflict", {"room": str(room_id)})
    plan = load(cfg, room_id)
    if not plan:
        raise PlanError("Карты проекта пока нет — сначала напишите главный запрос.",
                        "not_found", {"room": str(room_id)})
    result = validate_plan(room, [data], plan)
    if not result["streams"]:
        raise PlanError(result["warnings"][0] if result["warnings"]
                        else "Не удалось добавить направление — проверьте название и исполнителей.",
                        "bad_request", {"warnings": result["warnings"]})
    stream = result["streams"][0]

    def mutate(current: dict) -> dict:
        # лимит проверяем под блокировкой: иначе параллельные добавления проскакивают (11 из 8)
        if len(current.get("streams") or []) >= MAX_STREAMS:
            raise PlanError("В карте уже восемь направлений — больше нельзя.", "conflict",
                            {"limit": MAX_STREAMS})
        stream["id"] = _next_id(current)
        stream["status"] = "proposed"
        stream["created_by"] = "mikhail" if str(source) == "mikhail" else "manager"
        current.setdefault("streams", []).append(stream)
        _log(current, "added", "Добавлена часть работы «%s»." % stream.get("title"),
             who=source, stream_id=stream.get("id"))
        return current

    return {"plan": _update(cfg, room_id, mutate), "stream": stream,
            "warnings": result["warnings"]}


def update_stream(cfg, room_id, stream_id, fields, source="mikhail", room=None) -> dict:
    """Ручная правка направления (Михаил): те же правила, что у A6."""
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        raise PlanError("Проектный режим выключен — включите его в настройках комнаты.",
                        "conflict", {"room": str(room_id)})
    if not isinstance(fields, dict) or not fields:
        raise PlanError("Нечего менять: передайте поля направления.", "bad_request")
    plan = load(cfg, room_id)
    if not plan:
        raise PlanError("Карты проекта пока нет — сначала напишите главный запрос.",
                        "not_found", {"room": str(room_id)})
    if _find(plan, stream_id) is None:
        raise PlanError("Направление «%s» не найдено." % stream_id, "not_found",
                        {"stream_id": stream_id})
    warnings: list = []
    patch = {"change": [{"id": stream_id, "fields": fields, "reason": _text(fields.get("reason"), 200)}]}
    result = apply_patch(cfg, room_id, patch, source=source, room=room)
    warnings.extend(result.get("warnings") or [])
    return {"plan": result.get("plan"), "warnings": warnings}


def drop_stream(cfg, room_id, stream_id, reason="", source="mikhail", room=None) -> dict:
    """«Отложить»: статус ``dropped`` (не удаление) + комментарий в карточку, если запущено."""
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        raise PlanError("Проектный режим выключен — включите его в настройках комнаты.",
                        "conflict", {"room": str(room_id)})
    plan = load(cfg, room_id)
    if not plan:
        raise PlanError("Карты проекта пока нет — сначала напишите главный запрос.",
                        "not_found", {"room": str(room_id)})
    stream = _find(plan, stream_id)
    if stream is None:
        raise PlanError("Направление «%s» не найдено." % stream_id, "not_found",
                        {"stream_id": stream_id})
    result = apply_patch(cfg, room_id, {"drop": [{"id": stream_id, "reason": reason}]},
                         source=source, room=room)
    text = "Направление отложено: %s" % (reason or "решение Михаила")
    rid = stream.get("request_id")
    note = None
    if rid and stream.get("status") not in ("done", "proposed", "dropped"):
        writer = _engine_write()
        try:
            board = room.get("board") or "team"
            writer.act(cfg, board, rid, "comment", text=text)
            note = text
        except Exception as exc:  # noqa: BLE001 - карточка могла исчезнуть
            note = "комментарий не записан: %s" % (str(exc)[:120],)
    return {"plan": result.get("plan"), "comment": note, "warnings": result.get("warnings") or []}


# ------------------------------------------------------- карта из запросов
REBUILD_TITLE = "Собрать карту проекта из запросов комнаты"


def rebuild(cfg, room_id, room=None, limit: int = 20) -> dict:
    """A7: запрос-бриф, в тело которого подставлены существующие запросы комнаты."""
    room = room if isinstance(room, dict) and room else _get_room(cfg, room_id)
    if not project_mode(room):
        raise PlanError("Проектный режим выключен — включите его в настройках комнаты.",
                        "conflict", {"room": str(room_id)})
    registry = _modules().get("registry")
    writer = _engine_write()
    if registry is None or writer is None:
        raise PlanError("Реестр или движок записи недоступен — карту собрать нельзя.", "conflict")
    try:
        records = registry.list_requests(cfg, str(room_id), limit=limit) or []
    except Exception as exc:  # noqa: BLE001
        raise PlanError("Не удалось прочитать запросы комнаты: %s." % str(exc)[:160], "conflict") from exc
    lines = ["Собери карту проекта из того, что уже сделано в комнате.",
             "Ниже — существующие запросы: название, итог и статус.", ""]
    for index, rec in enumerate(records, 1):
        rid = rec.get("root_id")
        summary = _summary(cfg, room, rid) if rid else {}
        text = _text((summary or {}).get("preview") or rec.get("text"), 160)
        status = (summary or {}).get("status") or "queued"
        lines.append("%d. %s — %s — %s" % (index, _text(rec.get("title") or "Без названия", 120),
                                          text or "без итога", status))
    if not records:
        lines.append("Запросов пока нет — предложи направления по описанию комнаты.")
    lines += ["", "Выдай блок plan (направления 3–8) и блок stage, как обычно."]
    body = "\n".join(lines)
    slug = "plan-rebuild-%s-%s" % (room_id, _now())
    try:
        res = writer.create_request(cfg, room, slug, REBUILD_TITLE, body, brief=True)
    except TypeError:  # соседняя версия без brief
        res = writer.create_request(cfg, room, slug, REBUILD_TITLE, body)
    except Exception as exc:  # noqa: BLE001
        message = getattr(exc, "message_ru", None) or getattr(exc, "message", None) or str(exc)
        raise PlanError("Не удалось создать запрос «%s»: %s" % (REBUILD_TITLE, message),
                        "conflict") from exc
    root_id = str((res or {}).get("root_id") or (res or {}).get("id") or "")
    if not root_id:
        raise PlanError("Движок не вернул id запроса «карта из запросов».", "conflict")
    try:
        registry.add_request(cfg, str(room_id), {
            "root_id": root_id, "slug": slug, "title": REBUILD_TITLE, "text": body,
            "created_at": _now(), "critic": False, "mode": "auto", "adopted": False,
            "extra_cards": [], "ingested": {}, "retargeted": False, "kind": "map_from_existing",
            "brief": True})
    except Exception:  # noqa: BLE001 - реестр вторичен
        pass
    return {"root_id": root_id, "title": REBUILD_TITLE, "slug": slug,
            "request": _summary(cfg, room, root_id) or {"id": root_id, "title": REBUILD_TITLE}}


__all__ = ["PlanError", "plan_path", "empty_plan", "load", "save", "validate_plan", "ingest_plan",
           "approve", "tick", "apply_patch", "add_stream", "update_stream", "drop_stream",
           "rebuild", "quota_limited", "link_for", "detail_meta", "streams_summary", "project_mode",
           "max_parallel", "launch_mode", "room_profiles", "REBUILD_TITLE", "AUTO_NOTE",
           "owner_label", "fill_labels", "PROFILE_ROLE_RU",
           "MAX_STREAMS", "MAX_PARALLEL_DEFAULT", "STREAM_STATUSES", "STREAM_STATUS_RU"]
