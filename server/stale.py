"""Единые состояния пунктов «ждёт вас» (W27-E1, контракт `WAVE27-RULES.md` §1).

Зачем отдельный модуль
----------------------
Дефект 18.11.2026 (вечер): сервер отдавал `approvals[].state = "closed"`, а
интерфейс знал только `pending` — двадцать закрытых пунктов рисовались как
«ждёт вашего решения», счётчик не уменьшался, нажатие давало 409. Причина —
состояния жили в двух местах и сверялись «на глаз».

Здесь ОДИН источник правды:

* состояния пункта — :data:`ITEM_STATES` (вопрос / решение / шаг) и
  :data:`APPROVAL_STATES` (разрешение);
* ждёт человека **только** `open` (у решения — `proposed`, у разрешения — `pending`);
  всё остальное не считается и не подписывается «ждёт»;
* у каждого ожидающего пункта есть действие («Не актуально»), у снятого — «Вернуть»;
* `needs_you_count = {questions, decisions, approvals, steps, total}` — серверный
  счётчик запроса: клиент показывает это число, а не пересчитывает список;
* пункты завершённого запроса (``done``/``archived``) закрываются сами
  (`closed_reason: request_done`) — ждать их некому.

Модуль чистый: ничего не пишет и не читает диск. Запись снятия/возврата —
``routes_write`` (одно действие на все виды), массовое закрытие — `stale`-маршрут
в ``routes_approvals``.
"""
from __future__ import annotations

#: Состояния вопроса, решения и шага (контракт §1).
ITEM_STATES = ("open", "answered", "accepted", "rejected", "dismissed", "done")
#: Состояния разрешения (контракт §1).
APPROVAL_STATES = ("pending", "granted_once", "granted_always", "denied", "expired",
                   "closed", "dismissed")
#: Ещё состояния разрешения — из словаря ``approvals.STATES`` (W26-D1, W28-B):
#: «нельзя штатно» (`workaround`), жёсткий запрет (`info`), подсказка участнику
#: (`hinted`) и доверие профилю (`trusted`). Человеку они НЕ «ждут решения», поэтому
#: в счётчик не входят. Раньше эта дверь их не знала и молча превращала `state`
#: в `pending`: недоговорная блокировка попадала в «нужно от вас» и рисовалась как
#: вопрос с кнопками разрешения — то есть кнопки дефекта 6 не работали и по счётчику.
APPROVAL_EXTRA_STATES = ("workaround", "info", "hinted", "trusted")
#: Почему пункт закрыт. Один словарь на сервер, интерфейс и тексты.
CLOSED_REASONS = ("card_finished", "request_done", "request_archived", "dismissed", "superseded")
REASON_RU = {
    "card_finished": "работа завершена",
    "request_done": "запрос завершён",
    # W29-P4: запрос в архиве — пункт не ждёт человека; после «Вернуть из архива»
    # он снова открывается сам (закрытие вычисляется из флага, а не пишется в карточку).
    "request_archived": "запрос в архиве",
    "dismissed": "закрыто как неактуальное",
    "superseded": "устарело: работа пошла иначе",
}
#: Единственное состояние, которое правда ждёт человека (у решения — `proposed`).
WAITING_ITEM_STATES = ("open",)


def waits(kind: str, state: str) -> bool:
    """Ждёт ли состояние человека: у разрешения это `pending`, у остальных `open`."""
    return str(state or "") in (("pending",) if kind == "approval" else WAITING_ITEM_STATES)


#: «Снят» у шага: `dismissed` — человек убрал, `cancelled` — снят его ответом.
STEP_CLOSED_STATES = ("dismissed", "cancelled")
#: Статус запроса, при котором пункты считаются неактуальными (контракт §1).
FINISHED_REQUESTS = ("done", "archived")
#: А вот САМИ закрываются только у убранного запроса (`archived`). У `done` бывает
#: открытый вопрос — это и есть «ждёт вашего слова», а не мусор: руководитель
#: завершает карточку ИМЕННО затем, чтобы спросить. Поэтому для `done` закрытие —
#: только явное действие человека («Не актуально» / «Закрыть все неактуальные»).
AUTO_CLOSE_REQUESTS = ("archived",)
#: Виды пунктов, к которым применимо «Не актуально» / «Вернуть».
KINDS = ("question", "decision", "approval", "step")
#: Подписи действий — одни на сервер, интерфейс и подсказки.
ACTION_LABELS = {"dismiss": "Не актуально", "restore": "Вернуть"}
#: Что значит снятие/возврат — простыми словами (для подсказки у кнопки).
ACTION_HINTS = {
    "dismiss": "Пункт уйдёт из «Нужен ваш ответ» и из счётчиков. Решение при этом "
               "не принимается — пункт можно вернуть.",
    "restore": "Пункт снова появится в «Нужен ваш ответ» и в счётчиках.",
}
#: Разделы запроса, где лежат пункты: (ключ детали, вид, поле id).
SECTIONS = (("open_questions", "question", "id"), ("questions", "question", "id"),
            ("decisions", "decision", "id"), ("next_steps", "step", "id"))
#: Действия, которые снимает массовое закрытие (только для завершённых запросов).
STALE_SCOPES = ("finished_requests",)

#: W29-P2 п.2: маркер системной паузы. Карточку остановил сторож моделей, а не
#: человек: сторож снимет её сам (≤ 60 с), кнопка «Снять паузу сейчас» — на случай,
#: если ждать не хочется. Такая пауза НЕ входит в число «ждёт вашего решения».
SYSTEM_PAUSE_LABEL = "Снимаю автоматически"
SYSTEM_PAUSE_DEFAULT_REASON = "модель не подтверждена — жду простановки модели"
#: Кнопки, которые обязана иметь заблокированная карточка (одно место правды для
#: сервера, интерфейса и `tools/check_dead_ends.py`). Снятие паузы есть у всех:
#: без него пункт «ждёт вашего решения» — тупик.
BLOCKED_ACTIONS = ("resume", "instruct", "model", "reassign", "dismiss")
#: У системной паузы сторожа — только «снять сейчас»: остальное сделает он сам.
SYSTEM_PAUSE_ACTIONS = ("resume",)

#: Состояния ответа на вопрос, которые уже не ждут человека (W20-D1).
_ANSWER_CLOSED = ("answered", "sent", "received", "applied", "no_change",
                  "replied_text", "dismissed")


def request_finished(status) -> bool:
    """Запрос завершён (``done``/``archived``): пункты ждать некому."""
    return str(status or "").strip().lower() in FINISHED_REQUESTS


def auto_closeable(status) -> bool:
    """Пункты такого запроса закрываются сами, без действия человека."""
    return str(status or "").strip().lower() in AUTO_CLOSE_REQUESTS


def question_state(item: dict) -> str:
    """Состояние вопроса: ``open | answered | dismissed`` (контракт §1).

    ``waiting_explanation`` («ждёт пояснения») — вопрос всё ещё открыт: человек
    попросил объяснить, а не ответил.
    """
    if not isinstance(item, dict):
        return "open"
    if item.get("dismissed"):
        return "dismissed"
    answer = str(item.get("answer_state") or item.get("status") or "").strip().lower()
    if answer == "dismissed":
        return "dismissed"
    if answer in _ANSWER_CLOSED:
        return "answered"
    if answer in ("open", "waiting_explanation", ""):
        return "open" if not item.get("answered") else "answered"
    return "answered" if item.get("answered") else "open"


def decision_state(item: dict) -> str:
    """Состояние решения: ``open (proposed) | answered (discuss) | accepted | rejected | dismissed``."""
    if not isinstance(item, dict):
        return "open"
    status = str(item.get("status") or "").strip().lower()
    if not status:
        status = "proposed" if item.get("open") is not False else "accepted"
    if status == "proposed":
        return "open"
    if status == "accept" or status == "accepted":
        return "accepted"
    if status == "reject" or status == "rejected":
        return "rejected"
    if status in ("dismissed", "superseded"):
        return "dismissed"
    if status == "done":
        return "done"
    return "open" if item.get("open") is True else "answered"


def step_state(item: dict) -> str:
    """Состояние шага: ``open | done | dismissed``.

    ``cancelled`` («снят по вашему ответу») — это тоже не «ждёт вас»: он попадает
    в `dismissed` с причиной `superseded`, а прежнее значение остаётся в
    ``legacy_state`` для подписи («снят по вашему ответу»).
    """
    if not isinstance(item, dict):
        return "open"
    state = str(item.get("state") or "open").strip().lower()
    if state in ("done",) + STEP_CLOSED_STATES:
        # `cancelled` («снят по вашему ответу») оставляем как есть: это подпись
        # интерфейса и значение, которым уже пользуются чтение и правка шагов.
        return state
    if item.get("done") is True:
        return "done"
    return "open"


def item_state(kind: str, item: dict) -> str:
    """Состояние пункта по его виду — одна дверь для всех вызовов."""
    kind = str(kind or "").strip().lower()
    if kind == "question":
        return question_state(item)
    if kind == "decision":
        return decision_state(item)
    if kind == "step":
        return step_state(item)
    if kind == "approval":
        state = str((item or {}).get("state") or "").strip().lower()
        if state in APPROVAL_STATES or state in APPROVAL_EXTRA_STATES:
            # W28-B: недоговорные/информационные состояния — как есть. Иначе
            # `workaround`/`info`/`hinted`/`trusted` становились `pending` и попадали
            # в «нужно от вас», хотя разрешить их нельзя (правило «без тупиков»).
            return state
        return "pending"
    return "open"


def needs_you(kind: str, item: dict) -> bool:
    """Ждёт ли пункт человека: только `open`/`pending` И есть чем нажать."""
    state = item_state(kind, item)
    if kind == "approval":
        if state != "pending":
            return False
        if (item or {}).get("actionable") is False:
            return False
        return bool((item or {}).get("actions") or [])
    return waits(kind, state)


def closed_reason(kind: str, item: dict, state: str) -> str | None:
    """Почему пункт закрыт (``None`` — он ещё ждёт или закрыт человеком)."""
    item = item if isinstance(item, dict) else {}
    if waits(kind, state):
        return None
    if kind == "approval":
        return str(item.get("closed_reason") or "") or None
    legacy = str(item.get("legacy_state") or item.get("state") or "").strip().lower()
    if legacy == "cancelled":
        return "superseded"
    if state == "dismissed":
        return "dismissed"
    return None


def _actions_for(kind: str, state: str) -> list[dict]:
    """Кнопки пункта: «Не актуально» у ожидающего, «Вернуть» у снятого."""
    if waits(kind, state):
        return [{"id": "dismiss", "label": ACTION_LABELS["dismiss"], "kind": "close"}]
    if state == "dismissed" and kind != "approval":
        return [{"id": "restore", "label": ACTION_LABELS["restore"], "kind": "restore"}]
    return []


def _apply(kind: str, item: dict, close: bool, archived: bool = False) -> bool:
    """Проставить пункту единые поля. True — он ждёт человека."""
    state = item_state(kind, item)
    reason = closed_reason(kind, item, state)
    if waits(kind, state) and close:
        # Автозакрытие (контракт §1): у завершённого запроса пункт не ждёт никого.
        # W29-P4: у архивного запроса — своя причина, чтобы человек видел, почему
        # пункт закрыт, и после «Вернуть из архива» он открылся снова сам.
        state = "dismissed"
        reason = "request_archived" if archived else "request_done"
        item["auto_closed"] = True
    item["state"] = state
    waiting = waits(kind, state) and (kind != "approval" or bool(item.get("actions") or []))
    item["needs_you"] = bool(waiting)
    item["closed"] = not waits(kind, state)
    if reason:
        item["closed_reason"] = reason
        item.setdefault("closed_reason_label", REASON_RU.get(reason, ""))
    if not item.get("actions"):
        actions = _actions_for(kind, state)
        if actions:
            item["actions"] = actions
    return bool(waiting)


def counts(detail: dict) -> dict:
    """Счётчик «нужно от вас» по разделам запроса (``needs_you_count`` контракта §1)."""
    out = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0, "total": 0}
    seen: set = set()
    for key, kind, id_field in SECTIONS:
        for item in (detail or {}).get(key) or []:
            if not isinstance(item, dict):
                continue
            ident = str(item.get(id_field) or item.get("id") or "")
            if ident and (kind, ident) in seen:
                continue          # `questions` и `open_questions` — один источник
            if ident:
                seen.add((kind, ident))
            if item.get("needs_you"):
                out[{"question": "questions", "decision": "decisions",
                     "step": "steps"}.get(kind, "questions")] += 1
    for item in (detail or {}).get("approvals") or []:
        if isinstance(item, dict) and item.get("needs_you"):
            out["approvals"] += 1
    out["total"] = out["questions"] + out["decisions"] + out["approvals"] + out["steps"]
    return out


def stale_open(detail: dict) -> dict:
    """Сколько пунктов ещё можно снять у завершённого запроса (кнопка «Закрыть все»)."""
    out = {"questions": 0, "decisions": 0, "approvals": 0, "steps": 0, "total": 0}
    if not request_finished((detail or {}).get("status")):
        return out
    seen: set = set()
    for key, kind, id_field in SECTIONS:
        for item in (detail or {}).get(key) or []:
            if not isinstance(item, dict) or not item.get("needs_you"):
                continue
            ident = str(item.get(id_field) or item.get("id") or "")
            if ident and (kind, ident) in seen:
                continue
            if ident:
                seen.add((kind, ident))
            out[{"question": "questions", "decision": "decisions",
                 "step": "steps"}.get(kind, "questions")] += 1
    for item in (detail or {}).get("approvals") or []:
        if isinstance(item, dict) and item.get("needs_you"):
            out["approvals"] += 1
    out["total"] = out["questions"] + out["decisions"] + out["approvals"] + out["steps"]
    return out


def decorate(detail: dict, request_status=None, auto_close=None) -> dict:
    """Проставить единые поля всем пунктам запроса и серверный счётчик.

    Меняет ``detail`` на месте (так же, как остальные ``*_view`` приложения) и
    возвращает его. У убранного запроса (``archived``) пункты закрываются сами — с
    причиной ``request_done``, без записи в файлы: пункт не пропадает из данных, но
    и не числится за человеком. У ``done`` закрытие только явное: «руководитель
    завершил карточку, чтобы спросить» — рабочее состояние, а не мусор.
    """
    if not isinstance(detail, dict):
        return detail
    status = request_status if request_status is not None else detail.get("status")
    archived = bool(detail.get("archived"))
    finished = request_finished(status) or archived
    if auto_close is None:
        close = auto_closeable(status) or archived
    else:
        close = bool(auto_close)
    auto_closed = 0
    for key, kind, _id in SECTIONS:
        items = detail.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                _apply(kind, item, close, archived)
                if item.get("auto_closed"):
                    auto_closed += 1
    for item in detail.get("approvals") or []:
        if isinstance(item, dict):
            _apply("approval", item, close, archived)
            if item.get("auto_closed"):
                auto_closed += 1
    counter = counts(detail)
    detail["needs_you_count"] = counter
    # W29-P2 п.5: число и список заблокированных карточек — один источник. В счётчик
    # `needs_you_count` их не кладём (его ключи закреплены контрактом §1), но отдельным
    # полем отдаём и число, и список с кнопками: интерфейс не считает их сам, а
    # `tools/check_dead_ends.py` сверяет число со списком.
    waiting_cards = blocked_waiting(detail)
    detail["needs_you_blocked"] = len(waiting_cards)
    detail["blocked_waiting"] = waiting_cards
    detail["stale"] = {"request_finished": finished, "auto_close": close,
                       "open": stale_open(detail), "auto_closed": auto_closed,
                       "reasons": dict(REASON_RU), "labels": dict(ACTION_LABELS)}
    return detail


def open_targets(detail: dict) -> list[dict]:
    """Пункты, которые ещё ждут человека: ``[{kind, id, text}]`` (для массового закрытия)."""
    out: list[dict] = []
    seen: set = set()
    for key, kind, id_field in SECTIONS:
        for item in (detail or {}).get(key) or []:
            if not isinstance(item, dict) or not item.get("needs_you"):
                continue
            ident = str(item.get(id_field) or item.get("id") or "")
            if not ident or (kind, ident) in seen:
                continue
            seen.add((kind, ident))
            out.append({"kind": kind, "id": ident,
                        "text": str(item.get("text") or item.get("question_text") or "")[:200]})
    for item in (detail or {}).get("approvals") or []:
        if not isinstance(item, dict) or not item.get("needs_you"):
            continue
        ident = str(item.get("id") or "")
        if ident:
            out.append({"kind": "approval", "id": ident,
                        "profile": str(item.get("profile") or ""),
                        "text": str(item.get("description") or "")[:200]})
    return out


# ------------------------------------------------- системная пауза сторожа ---
# W29-P2 п.2. Карточка, остановленная контролем моделей, — это НЕ вопрос человеку:
# сторож поставил паузу сам и снимет её сам. Раньше такая карточка попадала в
# `today.waiting` (вид `blocked`) и человек видел «1 задача ждёт вашего решения»,
# хотя от него ничего не требовалось, а разблокировать её было нечем — тупик.
# Метка живёт только на чтении: `hold` в узле дерева (`engine_read._load`).
# Модель сторожа читается, но не правится (`read_state` — чистая функция чтения).


def is_system_pause(node) -> bool:
    """Узел дерева — системная пауза сторожа (снимается сама, не ждёт человека)."""
    hold = (node or {}).get("hold") if isinstance(node, dict) else None
    if not isinstance(hold, dict):
        return False
    return hold.get("by") == "guard" or hold.get("auto") is True


def guard_holds(cfg, room_id) -> dict:
    """``{task_id: {at, reason}}`` — карточки, удержанные сторожем моделей.

    Читает реестр ``data/state/<room>/model_guard.json`` через публичную
    ``model_guard.read_state``. Сбой чтения — пустой словарь: разметка вторична и
    не должна ломать страницу запроса.
    """
    room_id = str(room_id or "")
    if not cfg or not room_id:
        return {}
    try:
        try:
            from . import model_guard as guard          # пакетный импорт
        except ImportError:                             # pragma: no cover
            import model_guard as guard                   # type: ignore
        state = guard.read_state(cfg, room_id)
    except Exception:                                   # noqa: BLE001 - реестр вторичен
        return {}
    holds = state.get("holds") if isinstance(state, dict) else None
    if not isinstance(holds, dict):
        return {}
    return {str(k): v for k, v in holds.items() if isinstance(v, dict)}


def mark_system_pauses(cfg, room_id, nodes) -> int:
    """Проставить ``hold`` узлам, которые сторож остановил и снимет сам.

    Возвращает число размеченных узлов. Только чтение: ни карточка, ни реестр
    сторожа не меняются — это подпись для интерфейса и счётчиков.
    """
    holds = guard_holds(cfg, room_id)
    if not holds:
        return 0
    marked = 0
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("status") or "") != "blocked":
            continue
        item = holds.get(str(node.get("id") or ""))
        if not isinstance(item, dict):
            continue
        # Запись в реестре сторожа бывает устаревшей: карточку потом остановил кто-то другой
        # (сторож зависаний, человек). Системной паузой считается только та, чья ТЕКУЩАЯ
        # причина - наша; иначе пауза ждёт человека и обязана иметь кнопки.
        if "модель не подтверждена" not in str(node.get("summary_md") or "").lower():
            continue
        node["hold"] = {"by": "guard", "auto": True,
                        "at": int(item.get("at") or 0),
                        "reason": str(item.get("reason") or SYSTEM_PAUSE_DEFAULT_REASON),
                        "label": SYSTEM_PAUSE_LABEL}
        marked += 1
    return marked


def mark_blocked_actions(nodes) -> int:
    """Проставить кнопки каждой заблокированной карточке (`node["actions"]`).

    Один список действий на сервер, интерфейс и проверку «без тупиков»: если у
    узла нет кнопок, `tools/check_dead_ends.py` это увидит. Системная пауза
    сторожа получает только «Снять паузу сейчас» — остальное сделает сторож.
    Возвращает число размеченных узлов.
    """
    marked = 0
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("status") or "") != "blocked":
            continue
        if is_system_pause(node):
            node["actions"] = [{"id": "resume", "label": "Снять паузу сейчас", "kind": "system"}]
            node["needs_you"] = False
        else:
            node["actions"] = [{"id": ident} for ident in BLOCKED_ACTIONS]
            node["needs_you"] = True
        marked += 1
    return marked


def blocked_waiting(detail) -> list:
    """Заблокированные карточки, которые правда ждут человека (без пауз сторожа).

    Один источник числа и списка «ждёт вашего решения» по запросу: карточка без
    системной паузы. Возвращает ``[{id, title, assignee, actions}]``.
    """
    out = []
    for node in (detail or {}).get("tree") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("status") or "") != "blocked" or is_system_pause(node):
            continue
        out.append({"id": str(node.get("id") or ""), "title": str(node.get("title") or ""),
                    "assignee": str(node.get("assignee") or ""),
                    "actions": [str((a or {}).get("id") or "") for a in (node.get("actions") or [])
                                if isinstance(a, dict)]})
    return out


__all__ = ["ITEM_STATES", "APPROVAL_STATES", "APPROVAL_EXTRA_STATES", "CLOSED_REASONS", "REASON_RU",
           "WAITING_ITEM_STATES", "FINISHED_REQUESTS", "KINDS", "ACTION_LABELS",
           "ACTION_HINTS", "SECTIONS", "STALE_SCOPES",
           "SYSTEM_PAUSE_LABEL", "SYSTEM_PAUSE_DEFAULT_REASON",
           "BLOCKED_ACTIONS", "SYSTEM_PAUSE_ACTIONS",
           "AUTO_CLOSE_REQUESTS", "request_finished", "auto_closeable", "question_state", "decision_state", "step_state",
           "item_state", "needs_you", "closed_reason", "counts", "stale_open",
           "decorate", "open_targets", "is_system_pause", "guard_holds", "mark_system_pauses",
           "mark_blocked_actions", "blocked_waiting"]
