"""Экран «Сегодня» — серверная часть (WAVE24 §3.1, пакет N2).

`GET /api/today` отвечает на один вопрос: «что ждёт человека сейчас и что
изменилось с прошлого раза». Сборка ответа — чистая функция `build()` над
интерфейсом `Sources`; настоящие источники (`routes_today.LiveSources`) живут в
HTTP-слое. Поэтому правила «что считать ожиданием» проверяются тестами без
доски, комнат и сервера.

Источники — только существующее чтение, без новых хранилищ:

* реестр запросов комнаты — ``registry.list_requests``;
* «От вас нужно» — ``ingest.backlog_view`` (с наложением ответов ``answers``);
* состояние корней на доске — ``engine_read.today_states`` (лёгкое чтение);
* запросы разрешения — ``approvals.collect`` (только у корней ``needs_you``);
* черновик карты проекта — ``plan.load``.

Единственный новый файл — отметка захода ``data/state/today.json``: по ней
считается «что изменилось с прошлого раза» (``last_visit``/``mark_seen``).

Площадка универсальна: в правилах нет ни одного названия комнаты, запроса или
слова конкретного бизнеса — только виды ожидания и сроки.
"""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

try:  # пакетный и «плоский» импорт (python -m unittest из корня проекта)
    from . import statefiles as sf
except ImportError:  # pragma: no cover
    import statefiles as sf  # type: ignore

#: Кто в данных приложения «человек»: так подписаны владельцы пунктов «От вас
#: нужно» и вопросы руководителя (`read_view.questions`, `ingest_merge`). Это имя
#: профиля, а не термин бизнеса.
HUMAN = "mikhail"
#: Потолок «сути» в карточке ожидания (§3.1: ≤ 200 знаков).
TEXT_MAX = 200
#: «Ближайшие сроки» — сегодня и следующие столько суток (§3.1: 3 суток).
DUE_HORIZON_DAYS = 3
#: Сколько секунд живёт собранный ответ (кэш в HTTP-слое, §3.1: ≤ 10 с).
CACHE_TTL_S = 10.0
#: Отметка захода меняет файл не чаще раза в столько секунд (§3.1: 60 с).
SEEN_MIN_GAP_S = 60
#: Ожидание старше этого возраста считается срочным (выделяется в интерфейсе).
URGENT_AFTER_S = 24 * 3600
#: W25-M2: карточка без признаков жизни дольше этого — срочно (порог остановки 20 мин).
URGENT_STALL_MIN = 20
#: Статусы запроса, при которых работа идёт (для раздела «Идёт сейчас»).
ACTIVE_STATUSES = ("working", "reviewing", "synthesizing")
#: Статусы пунктов, которые ещё ждут действия (как в «От вас нужно»).
WAITING_STATUSES = ("open", "asked")
#: Статусы запроса, у которых пункты не показываются вовсе (W27-E1 §1).
AUTO_CLOSED_REQUESTS = ("archived",)
#: Виды ожидания контракта §3.1 + W25-M2 (карточка не отвечает).
KINDS = ("question", "decision", "approval", "step", "draft", "failure", "stalled")
#: Виды пунктов, которые «Ждёт вас» берёт из самого запроса (W27-FIX). Шаги сюда не
#: входят: у них своё место — «Просрочено/Ближайшие», а в «ждёт вас» они дублировали
#: бы счётчик меню. `blocked` — карточка, которую человек разблокирует ответом
#: (`action: unblock`); она же считается во вкладке «От вас нужно», поэтому числа
#: экрана и меню совпадают (W28-F).
EXACT_KINDS = ("question", "decision", "approval", "blocked")
#: Имя файла отметки захода внутри `data/state`.
STATE_FILE = "today.json"
#: Короткая подсказка «что с этим делать» для каждого вида ожидания.
#: Не приказ, а следующий шаг: интерфейс показывает её рядом с сутью.
RECOMMENDED = {
    "question": "Ответить на вопрос",
    "decision": "Решить и записать решение",
    "approval": "Разрешить или запретить",
    "blocked": "Ответить, что делать дальше",
    "step": "Отметить сделанное или передать дальше",
    "draft": "Утвердить или поправить",
    "failure": "Посмотреть, на чём остановилось",
    # W25-M2: сторож зависаний нашёл карточку без признаков жизни
    "stalled": "Посмотреть карточку: продолжить или сменить модель",
}
#: Сроки в данных — `YYYY-MM-DD` (иногда с временем следом). Другие виды не гадаем.
_DUE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


class Sources:
    """Что экрану нужно от одной комнаты (всё только чтение).

    Настоящие источники — ``routes_today.LiveSources``; в тестах — подделки с
    нужным набором данных. Любой метод может бросить исключение: сборка
    продолжает работу (``_safe``), битая комната не валит ответ.
    """

    def rooms(self) -> list[dict]:
        """Комнаты, которые надо показать (архивные — нет)."""
        return []

    def requests(self, room: dict) -> list[dict]:
        """Запросы комнаты из реестра (``root_id``, ``title``, ``created_at``, …)."""
        return []

    def backlog(self, room: dict) -> list[dict]:
        """Пункты «От вас нужно» комнаты (с наложенными ответами)."""
        return []

    def waiting(self, room: dict) -> dict | None:
        """Точные пункты «ждёт вас» по запросам комнаты (W27-FIX).

        ``{root_id: [{kind, id, text, since}, …]}`` — только вопрос и решение,
        которые правда ждут человека (состояние ``open``/``proposed`` и есть
        действие). Это тот же расчёт, из которого страница запроса берёт
        ``needs_you_count`` (`server/stale.py`), поэтому числа «Сегодня» и запроса
        совпадают. Разрешения экран берёт своим источником (``approvals``) — здесь
        их нет, чтобы не посчитать дважды. ``None`` — источник недоступен: экран
        считает по индексу бэклога, как раньше.
        """
        return None

    def states(self, room: dict) -> dict:
        """``{root_id: {status, finished_at, started_at, members_working, since, error}}``."""
        return {}

    def approvals(self, room: dict) -> list[dict]:
        """Запросы разрешения ``pending``: ``{request_id, profile, member_title, description, at}``."""
        return []

    def plan(self, room: dict) -> dict | None:
        """Карта проекта комнаты (``plan.load``) или None."""
        return None

    def stalled(self, room: dict) -> list[dict]:
        """Карточки, о которых предупредил сторож зависаний (``watchdog.stalled``).

        W25-M2: источник отдаёт только уже записанное состояние сторожа — экран
        «Сегодня» не читает доску и не ждёт sqlite.
        """
        return []


# ------------------------------------------------------------------ мелочи ---
def _now() -> int:
    return int(time.time())


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value, limit: int = TEXT_MAX) -> str:
    """Одна строка без разметки-шума: пробелы сжаты, длина ≤ ``limit``."""
    flat = " ".join(str(value or "").split())
    if limit and len(flat) > limit:
        return flat[: max(0, limit - 1)].rstrip() + "…"
    return flat


def _mask(text: str) -> str:
    """Спрятать секреты в тексте ошибки (маски ``server/worklog.py``, только чтение)."""
    try:
        try:
            from . import worklog as _wl
        except ImportError:  # pragma: no cover
            import worklog as _wl  # type: ignore
        return str(_wl.mask(text) or "")
    except Exception:  # noqa: BLE001 — маска вторична, текст важнее
        return str(text or "")


def _short_title(title: str) -> str:
    """Короткое название запроса — одно правило на приложение (``title_short``)."""
    try:
        try:
            from . import title_short as mod
        except ImportError:  # pragma: no cover
            import title_short as mod  # type: ignore
        return str(mod.short_title(title or ""))
    except Exception:  # noqa: BLE001 — название вторично
        return _text(title, 60)


def _due(value) -> date | None:
    match = _DUE_RE.match(str(value or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _safe(func, default):
    """Значение источника или ``default``: битая комната не валит экран (§4)."""
    try:
        return func()
    except Exception:  # noqa: BLE001 — источник вторичен, ответ важнее
        return default


def _dicts(value) -> list[dict]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _titles(records: list[dict]) -> dict:
    """``{root_id: короткое название}`` для карточек ожидания."""
    out = {}
    for rec in records:
        root_id = str(rec.get("root_id") or "")
        if not root_id:
            continue
        out[root_id] = _short_title(str(rec.get("title") or rec.get("text") or ""))
    return out


def _urgent(kind: str, since: int, now: int) -> bool:
    """Срочность: сбой и блокировка — сразу; остальное — если ждёт больше суток."""
    if kind in ("failure", "approval"):
        return True
    return bool(since) and (now - since) >= URGENT_AFTER_S


# ------------------------------------------------------------------- сборка ---
def build(sources, last_visit_at=None, now=None) -> dict:
    """Собрать ответ `GET /api/today` (контракт §3.1) по источникам.

    ``last_visit_at`` — предыдущий заход из ``data/state/today.json``; ``None``
    означает «первый раз» → «изменилось» считается за последние 24 часа.
    Все массивы упорядочены по контракту, пустые разделы — пустые массивы.
    """
    now = _now() if now is None else int(now)
    last = _as_int(last_visit_at) or None
    since_ts = last if last is not None else now - 24 * 3600
    today = date.fromtimestamp(now)

    needs: list[dict] = []
    overdue: list[dict] = []
    soon: list[dict] = []
    running: list[dict] = []
    finished: list[dict] = []
    failures: list[dict] = []
    new_requests = new_answers = 0
    rooms_out: list[dict] = []

    for room in _dicts(_safe(sources.rooms, [])):
        room_id = str(room.get("id") or "")
        if not room_id:
            continue
        room_title = str(room.get("title") or room_id)
        records = _dicts(_safe(lambda: sources.requests(room), []))
        records = [rec for rec in records if rec.get("root_id")]
        states = _safe(lambda: sources.states(room), {})
        states = states if isinstance(states, dict) else {}
        items = _dicts(_safe(lambda: sources.backlog(room), []))
        approvals = _dicts(_safe(lambda: sources.approvals(room), []))
        plan = _safe(lambda: sources.plan(room), None)
        titles = _titles(records)

        def state_of(root_id: str) -> dict:
            value = states.get(root_id)
            return value if isinstance(value, dict) else {}

        def request_title(root_id: str) -> str:
            return titles.get(root_id) or ""

        def request_finished(root_id: str) -> bool:
            """Запрос убран (`archived`) — его пункты ждать некому (W27-E1 §1).

            Только `archived`: у `done` бывает открытый вопрос, и это рабочее
            состояние «руководитель завершил карточку, чтобы спросить», а не мусор.
            """
            status = str(state_of(str(root_id or "")).get("status") or "").lower()
            return status in AUTO_CLOSED_REQUESTS

        # 1) «Ждёт вас»: вопросы, решения, разрешения. W27-FIX: точные пункты берутся
        #    из самих запросов (тот же расчёт, что `needs_you_count` страницы), а не из
        #    индекса бэклога: в индексе id вопросов расходятся с карточками, и закрытые
        #    вопросы висели как «ждёт вас». Индекс остаётся запасным путём (источник не
        #    отдал точных пунктов) и источником сроков/предложения этапов.
        room_needs = 0
        exact = _safe(lambda: sources.waiting(room), None)
        exact = exact if isinstance(exact, dict) else None
        seen_items: set = set()
        if exact is not None:
            for root_id in sorted(exact):
                if request_finished(root_id):
                    continue                  # W27-E1: запрос убран — ждать некому
                for entry in _dicts(exact.get(root_id)):
                    kind_out = str(entry.get("kind") or "")
                    if kind_out not in EXACT_KINDS:
                        continue              # шаги — в «Просрочено/Ближайшие»
                    ident = str(entry.get("id") or "")
                    key = (root_id, kind_out, ident)
                    if ident and key in seen_items:
                        continue
                    if ident:
                        seen_items.add(key)
                    at = (_as_int(entry.get("since")) or _as_int(state_of(root_id).get("since"))
                          or now)
                    needs.append({"room": room_id, "room_title": room_title,
                                  "request_id": root_id,
                                  "request_title": request_title(root_id),
                                  "kind": kind_out, "text": _text(entry.get("text")),
                                  "since": at, "urgent": _urgent(kind_out, at, now),
                                  "recommended": RECOMMENDED.get(kind_out)})
                    room_needs += 1
            # предложение этапов живёт только в индексе: у него нет запроса (§1)
            for item in items:
                if str(item.get("kind") or "") != "phases_proposal":
                    continue
                if str(item.get("status") or "open") not in WAITING_STATUSES:
                    continue
                at = _as_int(item.get("created_at")) or _as_int(item.get("updated_at")) or now
                needs.append({"room": room_id, "room_title": room_title,
                              "request_id": str(item.get("source_request") or ""),
                              "request_title": request_title(str(item.get("source_request") or "")),
                              "kind": "decision", "text": _text(item.get("text")),
                              "since": at, "urgent": _urgent("decision", at, now),
                              "recommended": RECOMMENDED["decision"]})
                room_needs += 1
        else:
            for item in items:
                status = str(item.get("status") or "open")
                if status not in WAITING_STATUSES:
                    continue                  # закрытое и отклонённое не ждёт
                if request_finished(str(item.get("source_request") or "")):
                    continue                  # W27-E1: запрос завершён — пункт не ждёт никого
                kind = str(item.get("kind") or "")
                owner = str(item.get("owner") or "")
                if kind == "phases_proposal":
                    # предложение этапов руководителем: ждёт решения человека, хотя
                    # владелец в данных — руководитель комнаты (§1 «От вас нужно»)
                    kind_out = "decision"
                elif owner == HUMAN and kind == "open_question":
                    kind_out = "question"
                elif owner == HUMAN and kind == "decision":
                    kind_out = "decision"
                elif owner == HUMAN and kind == "next_step":
                    kind_out = "step"
                else:
                    continue                  # пункт не про человека — не его забота
                request_id = str(item.get("source_request") or "")
                at = _as_int(item.get("created_at")) or _as_int(item.get("updated_at")) or now
                needs.append({"room": room_id, "room_title": room_title,
                              "request_id": request_id,
                              "request_title": request_title(request_id),
                              "kind": kind_out, "text": _text(item.get("text")),
                              "since": at, "urgent": _urgent(kind_out, at, now),
                              "recommended": RECOMMENDED.get(kind_out)})
                room_needs += 1

        # 2) «Ждёт вас»: запросы разрешения (участник упёрся в блокировку)
        for approval in approvals:
            # W26-D1 (T1): «ждёт вас» — только то, что человек может изменить нажатием.
            # Недоговорная блокировка (`workaround`), жёсткий запрет (`floor`), закрытая
            # автозакрытием карточка (`closed`) — статус, а не вопрос: в счётчик «нужно от
            # вас» и в бейджи не идут. Старые источники без этих полей считаются как раньше.
            state = str(approval.get("state") or "")
            mode = str(approval.get("mode") or "")
            if (state and state != "pending") or (mode and mode != "grant"):
                continue
            if approval.get("actionable") is False or approval.get("closed"):
                continue
            if request_finished(str(approval.get("request_id") or approval.get("root_id") or "")):
                continue                      # W27-E1: запрос завершён — блокировка закрыта
            request_id = str(approval.get("request_id") or approval.get("root_id") or "")
            who = _text(approval.get("member_title") or approval.get("profile") or "", 60)
            what = _text(approval.get("description") or "", TEXT_MAX)
            at = _as_int(approval.get("at")) or now
            needs.append({"room": room_id, "room_title": room_title,
                          "request_id": request_id,
                          "request_title": request_title(request_id),
                          "kind": "approval",
                          "text": _text(("%s просит разрешить: %s" % (who, what)).strip(" :")),
                          "since": at, "urgent": True,
                          "recommended": RECOMMENDED["approval"]})
            room_needs += 1

        # 3) «Ждёт вас»: черновик карты проекта (ждёт утверждения)
        if isinstance(plan, dict) and str(plan.get("status") or "") == "draft" and plan.get("streams"):
            request_id = str(plan.get("brief_root_id") or "")
            at = _as_int(plan.get("updated_at")) or now
            needs.append({"room": room_id, "room_title": room_title,
                          "request_id": request_id,
                          "request_title": request_title(request_id),
                          "kind": "draft", "text": _text("Черновик карты проекта ждёт утверждения"),
                          "since": at, "urgent": _urgent("draft", at, now),
                          "recommended": RECOMMENDED["draft"]})
            room_needs += 1

        # 3б) «Ждёт вас»: карточка не отвечает (сторож зависаний, W25-M2)
        for row in _dicts(_safe(lambda: sources.stalled(room), [])):
            card_id = str(row.get("card_id") or "")
            if not card_id:
                continue
            minutes = max(0, _as_int(row.get("idle_min")))
            state = str(row.get("state") or "")
            who = _text(row.get("label") or row.get("profile") or "", 60)
            if state == "hung":
                if row.get("hard_stop") is False:
                    text = ("Карточка %s не отвечает %d мин — остановка выключена в настройках "
                            "комнаты" % (who, minutes))
                else:
                    text = "Карточка %s не отвечает %d мин — остановлена, нужно внимание" % (
                        who, minutes)
            else:
                text = "Карточка %s не отвечает %d мин" % (who, minutes)
            request_id = str(row.get("root_id") or "")
            at = _as_int(row.get("at")) or now
            needs.append({"room": room_id, "room_title": room_title,
                          "request_id": request_id,
                          "request_title": request_title(request_id),
                          "kind": "stalled", "text": _text(text),
                          "since": at, "urgent": minutes >= URGENT_STALL_MIN,
                          "recommended": RECOMMENDED["stalled"]})
            room_needs += 1

        # 4) сроки: просрочено и ближайшие 3 суток — по открытым пунктам со сроком
        room_overdue = room_soon = 0
        for item in items:
            if str(item.get("status") or "open") not in WAITING_STATUSES:
                continue
            if request_finished(str(item.get("source_request") or "")):
                continue                      # W27-E1: у убранного запроса срок не ждёт
            due = _due(item.get("due"))
            if due is None:
                continue
            days = (today - due).days
            request_id = str(item.get("source_request") or "") or None
            # W27-E3: строке «Сегодня» нужны не только срок и текст — ей нужен
            # переход (название запроса) и признак «это ещё можно закрыть».
            base = {"room": room_id, "room_title": room_title,
                    "item_id": str(item.get("id") or ""), "text": _text(item.get("text")),
                    "owner": str(item.get("owner") or ""), "due": due.isoformat(),
                    "request_title": request_title(request_id or ""),
                    "kind": str(item.get("kind") or ""),
                    "state": str(item.get("status") or "open"),
                    # без id пункта закрывать нечего: у строки не будет действия
                    "actionable": bool(item.get("id"))}
            if days > 0:
                overdue.append(dict(base, days_overdue=days, request_id=request_id))
                if base["actionable"]:
                    room_overdue += 1
            elif -days <= DUE_HORIZON_DAYS:
                soon.append(dict(base, days_left=-days, request_id=request_id))
                if base["actionable"]:
                    room_soon += 1

        # 5) «Идёт сейчас» и «что изменилось» — по состоянию корней и реестру
        room_running = 0
        for rec in records:
            root_id = str(rec.get("root_id"))
            state = state_of(root_id)
            status = str(state.get("status") or "")
            members = max(0, _as_int(state.get("members_working")))
            if status in ACTIVE_STATUSES or members > 0:
                started = (_as_int(state.get("since")) or _as_int(state.get("started_at"))
                           or _as_int(rec.get("created_at")) or now)
                running.append({"room": room_id, "room_title": room_title,
                                "request_id": root_id, "request_title": request_title(root_id),
                                # запрос идёт и без взятых карточек — руководитель работает
                                "members_working": members or (1 if status in ACTIVE_STATUSES else 0),
                                "since": started})
                room_running += 1
            if _as_int(rec.get("created_at")) > since_ts:
                new_requests += 1
            if _as_int(rec.get("answer_at")) > since_ts:
                new_answers += 1
            if status == "done":
                at = _as_int(state.get("finished_at"))
                if at > since_ts:
                    finished.append({"room": room_id, "request_id": root_id,
                                     "request_title": request_title(root_id), "at": at})
            if status == "failed":
                at = (_as_int(state.get("finished_at")) or _as_int(state.get("started_at")) or now)
                reason = _text(_mask(state.get("error") or "")) or "Работа остановилась с ошибкой"
                needs.append({"room": room_id, "room_title": room_title,
                              "request_id": root_id, "request_title": request_title(root_id),
                              "kind": "failure", "text": reason, "since": at, "urgent": True,
                              "recommended": RECOMMENDED["failure"]})
                room_needs += 1
                if at > since_ts:
                    failures.append({"room": room_id, "request_id": root_id,
                                     "request_title": request_title(root_id), "at": at,
                                     "reason": reason})

        rooms_out.append({"id": room_id, "title": room_title, "needs_you": room_needs,
                          "overdue": room_overdue, "running": room_running})

    # порядок контракта §3.1: срочные сначала, дальше — кто ждёт дольше
    needs.sort(key=lambda item: (0 if item.get("urgent") else 1, _as_int(item.get("since"))))
    overdue.sort(key=lambda item: (-_as_int(item.get("days_overdue")), item.get("item_id") or ""))
    soon.sort(key=lambda item: (_as_int(item.get("days_left")), item.get("item_id") or ""))
    running.sort(key=lambda item: _as_int(item.get("since")))
    finished.sort(key=lambda item: -_as_int(item.get("at")))
    failures.sort(key=lambda item: -_as_int(item.get("at")))

    return {
        "generated_at": now,
        "last_visit_at": last,
        # W27-E3: «просрочено» считает только закрываемое — пункт без действия
        # не должен числиться за человеком (§1 W27, правило «без тупиков»).
        "counters": {"needs_you": len(needs),
                     "overdue": sum(1 for row in overdue if row.get("actionable")),
                     "due_soon": len(soon),
                     "running": len(running),
                     "changed": len(finished) + new_requests + new_answers + len(failures)},
        "needs_you": needs,
        "overdue": overdue,
        "due_soon": soon,
        "running": running,
        "changed": {"finished": finished, "new_requests": new_requests,
                    "new_answers": new_answers, "failures": failures},
        "rooms": rooms_out,
    }


# ------------------------------------------------------- отметка захода ---
def state_path(cfg) -> Path:
    """Файл отметки захода: ``data/state/today.json`` (единственное хранилище N2)."""
    return Path(cfg.data_dir) / "state" / STATE_FILE


def last_visit(cfg) -> int | None:
    """Предыдущий заход (unix) или None: файла нет, он битый или значение странное."""
    try:
        data = sf.read_json(state_path(cfg), None)
    except Exception:  # noqa: BLE001 — отметка вторична, экран важнее
        return None
    value = _as_int(data.get("last_visit_at")) if isinstance(data, dict) else 0
    return value if value > 0 else None


def mark_seen(cfg, now=None) -> dict:
    """Записать заход (`POST /api/today/seen`): идемпотентно, не чаще раза в 60 с.

    ``last_visit_at`` в ответе — то значение, которое теперь считается предыдущим
    заходом (его же отдаёт `GET /api/today`). ``changed: false`` — заход был
    отмечен только что, файл не переписывался.
    """
    now = _now() if now is None else int(now)
    path = state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sf.lock_for(path):
            data = sf.read_json(path, None)
            previous = _as_int(data.get("last_visit_at")) if isinstance(data, dict) else 0
            if previous > 0 and abs(now - previous) < SEEN_MIN_GAP_S:
                return {"ok": True, "last_visit_at": previous, "previous": previous,
                        "changed": False}
            sf.write_json_atomic(path, {"last_visit_at": now, "updated_at": now})
            return {"ok": True, "last_visit_at": now, "previous": previous or None,
                    "changed": True}
    except Exception as exc:  # noqa: BLE001 — без файла экран работает, «изменилось» — за сутки
        return {"ok": False, "last_visit_at": last_visit(cfg), "previous": None,
                "changed": False, "warning": "Не удалось записать отметку захода: %s"
                                            % _text(str(exc), 120)}


__all__ = ["HUMAN", "TEXT_MAX", "DUE_HORIZON_DAYS", "CACHE_TTL_S", "SEEN_MIN_GAP_S",
           "URGENT_AFTER_S", "URGENT_STALL_MIN", "ACTIVE_STATUSES", "WAITING_STATUSES",
           "KINDS", "EXACT_KINDS", "RECOMMENDED",
           "STATE_FILE", "Sources", "build", "state_path", "last_visit", "mark_seen"]
