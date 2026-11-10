# -*- coding: utf-8 -*-
"""Проектный режим в мок-сервере: карта направлений (Plan), approve, правка, rebuild, бриф.

Модель — `docs/WAVE9-RULES.md` §1:

    Plan   = {version, brief_root_id, updated_at, status, streams[], log[]}
    Stream = {id, title, goal, owner, members[], depends_on[], due, status,
              request_id, reason, created_by}
    LogItem= {at, who, how(kind), text, stream_id}

Поведение мока (демо-темп, в тестах делится на `phase_scale`):

* `GET  /api/rooms/{r}/plan` — карта или `{"plan": null}`; попутно «тикает» очередь:
  освободился слот и зависимости закрыты → `queued` становится `running` и создаёт
  под-запрос в ленте комнаты (у под-запроса `plan_link`);
* `POST .../plan/approve` — `proposed` → `queued` → (tick) `running`; запуск уважает
  `depends_on` и `room.max_parallel_streams` (1..4); повтор идемпотентен;
* таймеры: `running` → `review` (RUN_S) → `done` (REVIEW_S); `waiting_quota`
  возвращается в работу через QUOTA_S — как сторож лимита (A8);
* `PATCH/POST/DELETE .../plan/streams[/{id}]` — ручная правка Михаила, всё в журнал;
* `POST .../plan/rebuild` и `POST /api/rooms/{r}/requests {brief:true}` — запрос-бриф,
  через BRIEF_MIN..BRIEF_MAX секунд «приходит» ответ руководителя с черновиком карты.

Валидация — по-русски, 400 (цикл, больше 8 направлений, чужой исполнитель, плохая дата).
На диск ничего не пишется: состояние живёт в памяти мока.
"""
from __future__ import annotations

import copy
import random
import re
import time

from mock_core import ApiError, now

STREAM_STATUSES = ("proposed", "queued", "running", "waiting_quota", "review", "done", "dropped")
PLAN_STATUSES = ("draft", "approved", "running", "done")
LOG_KINDS = ("created", "changed", "added", "launched", "dropped", "quota")
LOG_WHO = ("manager", "mikhail", "system")
LAUNCH_MODES = ("plan_first", "auto")
MAX_STREAMS = 8
MAX_PARALLEL = (1, 4)
TITLE_MIN, TITLE_MAX = 3, 120
GOAL_MAX = 600
REASON_MAX = 300

RUN_S = 9.0              # running → review
REVIEW_S = 4.0           # review → done
QUOTA_S = 12.0           # waiting_quota → running (лимит «сняли»)
BRIEF_MIN, BRIEF_MAX = 5.0, 8.0     # «раздумье» руководителя перед черновиком карты

DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SID_RE = re.compile(r"^s\d{1,3}$")

# Сообщение Михаила руководителю может добавить направление (§2: ответ на «Уточнение»
# содержит `plan_patch.add`). В демо — по ключевым словам, которых ещё нет в карте.
NOTE_TOPICS = (
    (("сертифик", "anvisa", "санит", "маркиров"), "Сертификация и маркировка",
     "Что нужно для продажи еды: Роспотребнадзор, санитарные требования, маркировка упаковки."),
    (("поставщик", "закупк", "мука", "рис", "ингредиент"), "Закупки и поставщики",
     "Найти поставщиков риса, бобов и начинок, сравнить цены и сроки, зафиксировать одного."),
    (("найм", "сотрудник", "повар", "команд", "люди"), "Команда и найм",
     "Кто нужен на старте: повар, сборщик заказов, курьер; сколько это стоит в месяц."),
    (("сайт", "лендинг", "instagram", "контент", "фото"), "Сайт и контент",
     "Лендинг с меню и доставкой, фото набора, тексты для Instagram."),
    (("кредит", "деньг", "инвест", "бюджет", "финансир"), "Деньги на старт",
     "Сколько нужно на запуск, откуда взять: свои, кредит, партнёр; график возврата."),
)


# --- чистые помощники -------------------------------------------------------
def roster(room: dict) -> list:
    """Состав комнаты: руководитель + участники (+ критик) — кому можно быть owner."""
    out = []
    for profile in [room.get("orchestrator")] + [m.get("profile") for m in room.get("members") or []]:
        if profile and profile not in out:
            out.append(profile)
    if room.get("critic") and room["critic"] not in out:
        out.append(room["critic"])
    return out


#: роль по-русски, если в комнате нет описания участника (технический id в интерфейс не отдаём)
ROLE_SLUG_RU = {"director": "руководитель комнаты", "ops": "операции", "cfo": "экономика",
                "marketing": "маркетинг", "strategy": "стратегия", "lawyer": "юрист",
                "critic": "критик", "runner": "сбор фактов", "coder": "программист",
                "surveyor": "исследователь", "analyst": "аналитик", "default": "руководитель комнаты"}
SLUG_RE = re.compile(r"^(?:biz|room)-")
NOTE_HEAD_RE = re.compile(r"[:.,;]|\s[—–]\s")
OWNER_LABEL_MAX = 32


def owner_label(room: dict, profile, limit: int = OWNER_LABEL_MAX) -> str:
    """Русская подпись исполнителя для интерфейса — как на сервере (поле `owner_label`)."""
    raw = str(profile or "").strip()
    if not raw:
        return ""
    if raw == "mikhail":
        return "Михаил"
    member = None
    for item in room.get("members") or []:
        if isinstance(item, dict) and str(item.get("profile") or "") == raw:
            member = item
            break
    if member is not None:
        head = NOTE_HEAD_RE.split(str(member.get("note") or ""))[0].strip(" .—-")
        if head and 1 < len(head) <= limit:
            return head
        role = str(member.get("role") or "")
        if role == "critic":
            return "критик"
        if role == "observer":
            return "наблюдатель"
    if raw == str(room.get("orchestrator") or ""):
        return "руководитель комнаты"
    if raw == str(room.get("critic") or ""):
        return "критик"
    slug = SLUG_RE.sub("", raw)
    label = ROLE_SLUG_RU.get(slug, slug)[:limit]
    return label[:1].upper() + label[1:] if label else label


def count_noun(number, forms) -> str:
    """«1 направление / 2 направления / 5 направлений» — для текста под-запроса."""
    try:
        value = abs(int(number))
    except (TypeError, ValueError):
        value = 0
    if value % 10 == 1 and value % 100 != 11:
        word = forms[0]
    elif 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        word = forms[1]
    else:
        word = forms[2]
    return "%d %s" % (value, word)


def max_parallel(room: dict) -> int:
    try:
        value = int(room.get("max_parallel_streams") or 2)
    except (TypeError, ValueError):
        value = 2
    return min(MAX_PARALLEL[1], max(MAX_PARALLEL[0], value))


def summarize(streams: list, plan: dict | None = None) -> dict:
    """`streams_summary` брифа — как `server/plan.py:streams_summary` (4 ключа).

    `running` считает и `waiting_quota`: направление в работе, просто ждёт квоту.
    """
    plan = plan or {}
    return {"total": len(streams),
            "running": sum(1 for s in streams if s.get("status") in ("running", "waiting_quota")),
            "done": sum(1 for s in streams if s.get("status") == "done"),
            "status": plan.get("status")}


def find(plan: dict, sid: str) -> dict | None:
    return next((s for s in plan.get("streams") or [] if s.get("id") == sid), None)


def next_id(plan: dict) -> str:
    used = {s.get("id") for s in plan.get("streams") or []}
    number = 1
    while "s%d" % number in used:
        number += 1
    return "s%d" % number


def cycle_errors(streams: list) -> list:
    """Циклы в depends_on. Возвращает список фраз по-русски (пусто — всё хорошо)."""
    graph = {s["id"]: [d for d in (s.get("depends_on") or []) if isinstance(d, str)]
             for s in streams if isinstance(s, dict) and s.get("id")}
    state, out = {}, []

    def walk(node: str, path: list) -> bool:
        mark = state.get(node)
        if mark == 1:                      # нашли кольцо
            ring = path[path.index(node):] + [node] if node in path else [node]
            out.append("Цикл зависимостей: %s" % " → ".join(ring))
            return True
        if mark == 2:
            return False
        state[node] = 1
        for dep in graph.get(node, []):
            if dep in graph and walk(dep, path + [node]):
                return True
        state[node] = 2
        return False

    for sid in graph:
        if state.get(sid) is None:
            walk(sid, [])
    return out


def validate(plan: dict, room: dict, stream: dict, *, partial: bool = False) -> None:
    """Проверка одного направления перед записью. Ошибки — ApiError 400 по-русски."""
    people = roster(room)
    if not partial:
        title = str(stream.get("title") or "").strip()
        if not TITLE_MIN <= len(title) <= TITLE_MAX:
            raise ApiError(400, "bad_request",
                           "Название направления — от %d до %d знаков" % (TITLE_MIN, TITLE_MAX),
                           {"field": "title", "length": len(title)})
        stream["title"] = title
    if "title" in stream and not partial:
        pass
    owner = stream.get("owner")
    if owner is not None and owner not in people:
        raise ApiError(400, "bad_request",
                       "Исполнитель «%s» не участвует в комнате «%s»" % (owner, room.get("title") or room.get("id")),
                       {"field": "owner", "profile": owner, "available": people})
    for member in stream.get("members") or []:
        if member not in people:
            raise ApiError(400, "bad_request",
                           "Участник «%s» не участвует в комнате «%s»" % (member, room.get("title") or room.get("id")),
                           {"field": "members", "profile": member, "available": people})
    if "due" in stream and stream["due"] is not None and not DUE_RE.match(str(stream["due"])):
        raise ApiError(400, "bad_request", "Срок направления — дата вида 2026-11-20",
                       {"field": "due", "value": stream.get("due")})
    if "status" in stream and stream["status"] not in STREAM_STATUSES:
        raise ApiError(400, "bad_request", "Статус направления: %s" % "|".join(STREAM_STATUSES),
                       {"field": "status", "value": stream.get("status")})


def draft_streams(room: dict) -> list:
    """Черновик карты: 3–4 направления под комнату (имитация ответа руководителя)."""
    people = roster(room)
    lead = room.get("orchestrator") or (people[0] if people else "default")
    who = lambda profile: profile if profile in people else lead       # noqa: E731

    if room.get("id") == "cafe":
        rows = [
            ("Рецептура и технология", "Утвердить состав набора из 6 моти, граммовки и сроки хранения.",
             who("biz-ops"), [who("biz-ops")], [], "2026-11-08"),
            ("Упаковка и бренд", "Форма коробки, поставщик, MOQ и логотип под печать.",
             who("biz-ops"), [who("biz-ops"), who("biz-marketing")], [], "2026-11-20"),
            ("Цена и юнит-экономика", "Себестоимость набора и минимальная цена с маржой 35%.",
             who("biz-cfo"), [who("biz-cfo")], ["s1"], "2026-11-27"),
            ("Продажи и доставка", "Каналы (Едок, Развоз, WhatsApp) и доставка по Заозёрью.",
             who("biz-marketing"), [who("biz-marketing"), who("biz-runner")], ["s2"], "2026-12-05"),
        ]
    elif room.get("id") == "fn":
        rows = [
            ("Документы на участок", "Зона, категория земли, обременения, кадастр Росреестра.",
             who("lawyer"), [who("lawyer")], [], "2026-11-12"),
            ("Вода, свет, подъезд", "Что есть на участке на самом деле: скважина, ЛЭП, дорога.",
             who("surveyor"), [who("surveyor")], [], "2026-11-19"),
            ("Смета первого этапа", "Подготовительные работы и календарь на ноябрь–декабрь.",
             lead, [lead, who("lawyer")], ["s1", "s2"], "2026-11-30"),
        ]
    else:
        rows = [
            ("Разобраться в задаче", "Что уже известно и чего не хватает для решения.",
             lead, [lead], [], None),
            ("Собрать факты", "Цифры, документы и цены по теме запроса.",
             lead, [lead], [], None),
            ("Предложить шаги", "Три шага на две недели с ответственными.",
             lead, [lead], ["s1", "s2"], None),
        ]
    out = []
    for number, (title, goal, owner, members, deps, due) in enumerate(rows, start=1):
        out.append({"id": "s%d" % number, "title": title, "goal": goal, "owner": owner,
                    "owner_label": owner_label(room, owner),
                    "members": list(members), "depends_on": list(deps), "due": due,
                    "status": "proposed", "request_id": None,
                    "reason": "Предложено руководителем комнаты по брифу", "created_by": "manager"})
    return out


class MockPlan:
    """Карта направлений комнаты: состояние в памяти мока."""

    # --- состояние --------------------------------------------------------
    def plan_init(self) -> None:
        self.plans = {}
        for room in self.room_details.values():
            name = "plan_%s.json" % room["id"]
            if name in self.fx:
                self.plans[room["id"]] = copy.deepcopy(self.fx[name])
        self.plan_clock = {}      # room → {sid: monotonic старта}
        self.plan_quota = {}      # (room, sid) → monotonic, когда заметили лимит
        self.plan_seq = 0

    def _plan_delay(self, lo: float, hi: float) -> float:
        """Демо-темп; в тестах делится на `phase_scale` (у MockThread свой `_delay`)."""
        return random.uniform(lo, hi) * max(0.001, float(self.phase_scale))

    def _log(self, plan: dict, *, who: str, kind: str, text: str, stream_id: str | None = None) -> None:
        self.plan_seq += 1
        plan.setdefault("log", []).append({"at": now(), "who": who, "kind": kind,
                                           "text": text, "stream_id": stream_id})
        plan["updated_at"] = now()

    # --- чтение -----------------------------------------------------------
    def plan_for(self, room_id: str) -> dict | None:
        self.room(room_id)
        return self.plans.get(room_id)

    def get_plan(self, room_id: str) -> dict:
        """`GET /api/rooms/{r}/plan` → `{plan, summary}` (как `server/routes_plan.py`)."""
        self.plan_for(room_id)
        self._tick_briefs()
        plan = self.plans.get(room_id)
        if plan is None:
            return {"plan": None, "summary": None}
        self.tick_plan(room_id)
        return {"plan": copy.deepcopy(plan), "summary": summarize(plan["streams"], plan)}

    def _tick_briefs(self) -> None:
        """Доставить черновики карт, которые «руководитель уже додумал» (без визита страницы)."""
        for rid, item in list(self.live.items()):
            if item.get("brief") and item.get("brief_due") is not None:
                self.plan_tick(rid)

    # --- очередь и таймеры ------------------------------------------------
    def _slots_used(self, plan: dict) -> int:
        return sum(1 for s in plan["streams"] if s.get("status") in ("running", "review"))

    def _deps_done(self, plan: dict, stream: dict) -> bool:
        index = {s["id"]: s for s in plan["streams"]}
        return all((index.get(dep) or {}).get("status") == "done" for dep in stream.get("depends_on") or [])

    def tick_plan(self, room_id: str) -> None:
        """Подвинуть направления по времени и запустить то, что можно (depends_on + лимит)."""
        plan = self.plans.get(room_id)
        if plan is None:
            return
        room = self.room(room_id)
        mono = time.monotonic()
        scale = max(0.001, float(self.phase_scale))
        clock = self.plan_clock.setdefault(room_id, {})
        moved = False
        for stream in plan["streams"]:
            sid = stream["id"]
            status = stream.get("status")
            if status == "waiting_quota":
                since = self.plan_quota.setdefault((room_id, sid), mono)
                if (mono - since) / scale >= QUOTA_S and self._slots_used(plan) < max_parallel(room):
                    stream["status"] = "running"
                    stream["reason"] = "Квота освободилась — направление снова в работе"
                    clock[sid] = mono
                    self.plan_quota.pop((room_id, sid), None)
                    self._log(plan, who="system", kind="quota",
                              text="Квота освободилась: «%s» снова в работе" % stream["title"], stream_id=sid)
                    moved = True
                continue
            if sid not in clock:
                continue
            elapsed = (mono - clock[sid]) / scale
            if status == "running" and elapsed >= RUN_S:
                stream["status"] = "review"
                stream["reason"] = "Работа сделана, руководитель комнаты проверяет результат"
                self._log(plan, who="manager", kind="changed",
                          text="«%s» — на проверке: свод собирается" % stream["title"], stream_id=sid)
                moved = True
            elif status == "review" and elapsed >= RUN_S + REVIEW_S:
                stream["status"] = "done"
                stream["reason"] = "Готово: итог направления в под-запросе"
                clock.pop(sid, None)
                if stream.get("request_id"):
                    self._finish_stream_request(stream["request_id"])
                self._log(plan, who="manager", kind="changed",
                          text="«%s» — готово" % stream["title"], stream_id=sid)
                moved = True
        for stream in plan["streams"]:
            if stream.get("status") != "queued" or not self._deps_done(plan, stream):
                continue
            if self._slots_used(plan) >= max_parallel(room):
                break
            self._launch(room, plan, stream)
            moved = True
        if moved:
            plan["updated_at"] = now()
        statuses = {s.get("status") for s in plan["streams"]}
        if plan.get("status") in ("approved", "running"):
            if statuses <= {"done", "dropped"} and statuses:
                plan["status"] = "done"
            elif statuses & {"running", "review", "queued", "waiting_quota"}:
                plan["status"] = "running"

    def _finish_stream_request(self, rid: str) -> None:
        """Под-запрос закрывается вместе с направлением (мок: время эволюции не ждём)."""
        req = self.live.get(rid)
        if req is not None:
            req["stream_done"] = True
        stored = self.requests.get(rid)
        if isinstance(stored, dict):
            stored["status"] = "done"
            stored["status_reason"] = None

    def _brief_title(self, plan: dict) -> str:
        """Название брифа для тела под-запроса (id на экран и в текст не выводим)."""
        rid = str(plan.get("brief_root_id") or "").strip()
        if not rid:
            return ""
        for item in (self.live.get(rid), self.requests.get(rid)):
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                if title:
                    return title
        return ""

    def _launch(self, room: dict, plan: dict, stream: dict) -> str:
        brief = self._brief_title(plan)
        text = ("«%s» — направление карты проекта комнаты «%s».\n\nЦель: %s\n\n"
                "Это направление из карты проекта. Главный запрос (бриф) — %s\n"
                "Карта: %s, запущено по очереди (не больше %d одновременно).\n"
                "Верни короткий итог и 2–3 следующих шага.\n\n"
                "Служебное (для поиска, в ответ не переносить): бриф %s, направление %s."
                % (stream["title"], room.get("title") or room["id"], stream.get("goal") or "—",
                   ("«%s»" % brief) if brief else "в комнате",
                   count_noun(len(plan["streams"]), ("направление", "направления", "направлений")),
                   max_parallel(room), plan.get("brief_root_id") or "—", stream.get("id") or "—"))
        detail = self.new_live_request(room["id"], text, stream["title"], bool(room.get("critic_pass")))
        rid = detail["id"]
        self.live[rid]["plan_link"] = {"stream_id": stream["id"], "title": stream["title"]}
        stream.update({"status": "running", "request_id": rid,
                       "reason": "Запущено по карте: под-запрос %s" % rid})
        self.plan_clock.setdefault(room["id"], {})[stream["id"]] = time.monotonic()
        self.plan_quota.pop((room["id"], stream["id"]), None)
        self._log(plan, who="manager", kind="launched",
                  text="Запустил «%s» — под-запрос %s" % (stream["title"], rid), stream_id=stream["id"])
        return rid

    # --- запуск по approve -----------------------------------------------
    def approve(self, room_id: str, body: dict) -> tuple:
        room = self.room(room_id)
        plan = self.plans.get(room_id)
        if plan is None:
            raise ApiError(404, "not_found", "Карты проекта пока нет — сначала напишите главный запрос",
                           {"room": room_id})
        if not room.get("project_mode"):
            raise ApiError(409, "conflict", "Проектный режим в комнате выключен — включите его в настройках",
                           {"reason": "project_mode_off", "room": room_id})
        wanted = body.get("streams", "all")
        if isinstance(wanted, str):
            if wanted != "all":
                raise ApiError(400, "bad_request", "streams: «all» или список id направлений",
                               {"field": "streams", "value": wanted})
            targets = [s for s in plan["streams"] if s["status"] == "proposed"]
        elif isinstance(wanted, list):
            if not wanted:
                raise ApiError(400, "bad_request", "Некого запускать: список направлений пуст", {"field": "streams"})
            targets = []
            for sid in wanted:
                if not isinstance(sid, str) or not SID_RE.match(sid):
                    raise ApiError(400, "bad_request", "Некорректный id направления",
                                   {"field": "streams", "value": sid})
                stream = find(plan, sid)
                if stream is None:
                    raise ApiError(404, "not_found", "Направления «%s» нет в карте" % sid, {"stream_id": sid})
                if stream["status"] == "dropped":
                    raise ApiError(409, "conflict", "Направление «%s» отложено — сначала верните его в карту"
                                   % stream["title"], {"reason": "stream_dropped", "stream_id": sid})
                targets.append(stream)
        else:
            raise ApiError(400, "bad_request", "streams: «all» или список id направлений", {"field": "streams"})

        for stream in targets:
            if stream["status"] != "proposed":
                continue
            stream["status"] = "queued"
            stream["reason"] = "В очереди: ждёт свободного места и закрытых зависимостей"
            self._log(plan, who="mikhail", kind="launched",
                      text="Михаил запустил «%s»" % stream["title"], stream_id=stream["id"])
        before = {s["id"]: s["status"] for s in plan["streams"]}
        if plan.get("status") == "draft":
            plan["status"] = "approved"
        self.tick_plan(room_id)
        started = [s["id"] for s in plan["streams"] if s["status"] == "running" and before.get(s["id"]) != "running"]
        return 200, {"plan": copy.deepcopy(plan), "started": started}

    # --- ручная правка Михаила -------------------------------------------
    def patch_stream(self, room_id: str, sid: str, body: dict) -> tuple:
        room = self.room(room_id)
        plan = self.plans.get(room_id)
        if plan is None:
            raise ApiError(404, "not_found", "Карты проекта пока нет", {"room": room_id})
        stream = find(plan, sid)
        if stream is None:
            raise ApiError(404, "not_found", "Направления «%s» нет в карте" % sid, {"stream_id": sid})
        allowed = {"title", "goal", "due", "members", "owner", "status", "reason", "depends_on"}
        bad = sorted(set(body) - allowed)
        if bad:
            raise ApiError(400, "bad_request", "Эти поля направления менять нельзя",
                           {"fields": bad, "allowed": sorted(allowed)})
        patch = {}
        if "depends_on" in body:
            deps = body["depends_on"]
            if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
                raise ApiError(400, "bad_request", "Поле «depends_on» — список id направлений",
                               {"field": "depends_on"})
            unknown = [d for d in deps if find(plan, d) is None]
            if unknown:
                raise ApiError(400, "bad_request", "Зависимость «%s» — такого направления нет в карте" % unknown[0],
                               {"field": "depends_on", "value": unknown[0]})
            patch["depends_on"] = [d for d in deps if d != sid]      # на себя — не зависимость
        if "title" in body:
            patch["title"] = str(body["title"] or "").strip()
        if "goal" in body:
            if not isinstance(body["goal"], str):
                raise ApiError(400, "bad_request", "Поле «goal» должно быть строкой", {"field": "goal"})
            patch["goal"] = body["goal"].strip()[:GOAL_MAX]
        if "reason" in body:
            patch["reason"] = str(body["reason"] or "").strip()[:REASON_MAX]
        if "due" in body:
            patch["due"] = None if body["due"] in (None, "") else str(body["due"])
        if "owner" in body:
            patch["owner"] = str(body["owner"] or "").strip()
            patch["owner_label"] = owner_label(room, patch["owner"])
        if "members" in body:
            members = body["members"]
            if not isinstance(members, list) or any(not isinstance(m, str) for m in members):
                raise ApiError(400, "bad_request", "Поле «members» — список профилей", {"field": "members"})
            patch["members"] = [m.strip() for m in members if m.strip()]
            if len(patch["members"]) > 6:
                raise ApiError(400, "bad_request", "В направлении не больше 6 участников", {"field": "members"})
        if "status" in body:
            status = body["status"]
            if status not in ("proposed", "queued", "dropped"):
                raise ApiError(400, "bad_request",
                               "Руками ставят proposed, queued или dropped; запуск — кнопкой «Запустить»",
                               {"field": "status", "value": status, "allowed": ["proposed", "queued", "dropped"]})
            patch["status"] = status
        if not patch:
            raise ApiError(400, "bad_request", "Нет полей для изменения", {"allowed": sorted(allowed)})
        merged = dict(stream)
        merged.update(patch)
        validate(plan, room, merged, partial="title" not in patch)
        before = dict(stream)
        stream.update(patch)
        rings = cycle_errors(plan["streams"])
        if rings:
            stream["depends_on"] = list(before.get("depends_on") or [])
            raise ApiError(400, "bad_request", rings[0], {"field": "depends_on"})
        changes = []
        for key, value in patch.items():
            if before.get(key) != value:
                changes.append("%s: %s → %s" % (key, _show(before.get(key)), _show(value)))
        if patch.get("status") == "dropped":
            self._drop_side_effects(room_id, plan, stream)
        self._log(plan, who="mikhail", kind="changed",
                  text="Михаил изменил «%s»: %s" % (stream["title"], "; ".join(changes) or "без изменений"),
                  stream_id=sid)
        if patch.get("status") == "queued":
            self.tick_plan(room_id)
        return 200, {"plan": copy.deepcopy(plan)}

    def add_stream(self, room_id: str, body: dict) -> tuple:
        room = self.room(room_id)
        plan = self.plans.get(room_id)
        if plan is None:
            raise ApiError(404, "not_found", "Карты проекта пока нет — сначала напишите главный запрос",
                           {"room": room_id})
        if len(plan["streams"]) >= MAX_STREAMS:
            raise ApiError(400, "bad_request", "В карте не больше %d направлений — сначала закройте лишние"
                           % MAX_STREAMS, {"field": "streams", "max": MAX_STREAMS})
        title = str(body.get("title") or "").strip()
        if not title:
            raise ApiError(400, "bad_request", "Нужно название направления", {"field": "title"})
        owner = str(body.get("owner") or room.get("orchestrator") or "").strip()
        members = body.get("members")
        if members is None:
            members = [owner] if owner else []
        if not isinstance(members, list) or any(not isinstance(m, str) for m in members):
            raise ApiError(400, "bad_request", "Поле «members» — список профилей", {"field": "members"})
        depends = body.get("depends_on") or []
        if not isinstance(depends, list) or any(not isinstance(d, str) for d in depends):
            raise ApiError(400, "bad_request", "Поле «depends_on» — список id направлений", {"field": "depends_on"})
        stream = {"id": next_id(plan), "title": title, "goal": str(body.get("goal") or "").strip()[:GOAL_MAX],
                  "owner": owner, "owner_label": owner_label(room, owner),
                  "members": [m.strip() for m in members if m.strip()],
                  "depends_on": list(depends), "due": None if body.get("due") in (None, "") else str(body["due"]),
                  "status": "proposed", "request_id": None,
                  "reason": str(body.get("reason") or "Добавлено Михаилом вручную").strip()[:REASON_MAX],
                  "created_by": "mikhail"}
        validate(plan, room, stream)
        unknown = [d for d in stream["depends_on"] if find(plan, d) is None]
        if unknown:
            raise ApiError(400, "bad_request", "Зависимость «%s» — такого направления нет в карте" % unknown[0],
                           {"field": "depends_on", "value": unknown[0]})
        plan["streams"].append(stream)
        rings = cycle_errors(plan["streams"])
        if rings:
            plan["streams"].pop()
            raise ApiError(400, "bad_request", rings[0], {"field": "depends_on"})
        self._log(plan, who="mikhail", kind="added",
                  text="Михаил добавил направление «%s»" % stream["title"], stream_id=stream["id"])
        return 201, {"plan": copy.deepcopy(plan), "stream": copy.deepcopy(stream)}

    def drop_stream(self, room_id: str, sid: str) -> tuple:
        plan = self.plans.get(room_id)
        if plan is None:
            raise ApiError(404, "not_found", "Карты проекта пока нет", {"room": room_id})
        stream = find(plan, sid)
        if stream is None:
            raise ApiError(404, "not_found", "Направления «%s» нет в карте" % sid, {"stream_id": sid})
        if stream["status"] == "dropped":
            return 200, {"plan": copy.deepcopy(plan)}
        stream["status"] = "dropped"
        stream["reason"] = "Отложено Михаилом: направление убрано из работы, карточка осталась"
        self._drop_side_effects(room_id, plan, stream)
        self._log(plan, who="mikhail", kind="dropped",
                  text="Михаил отложил «%s»" % stream["title"], stream_id=sid)
        return 200, {"plan": copy.deepcopy(plan)}

    def _drop_side_effects(self, room_id: str, plan: dict, stream: dict) -> None:
        """Отложенное направление: комментарий в карточку под-запроса (не удаляем)."""
        rid = stream.get("request_id")
        if not rid:
            return
        text = "[Михаил · направление отложено] «%s» убрано из работы по карте" % stream["title"]
        stored = self.requests.get(rid)
        if isinstance(stored, dict) and stored.get("tree"):
            self.comment_seq = int(getattr(self, "comment_seq", 0)) + 1
            stored["tree"][0].setdefault("comments", []).append(
                {"id": self.comment_seq, "author": "mikhail", "text": text, "at": now()})
        elif rid in self.live:
            self.comment_seq = int(getattr(self, "comment_seq", 0)) + 1
            self.live[rid].setdefault("comments", []).append(
                {"id": self.comment_seq, "author": "mikhail", "text": text, "at": now()})

    # --- бриф: rebuild и POST /requests {brief:true} ----------------------
    def rebuild(self, room_id: str) -> tuple:
        room = self.room(room_id)
        existing = self.summaries(room_id)[:12]
        lines = ["%s — %s (%s)" % (item["title"], item["status"], item["id"]) for item in existing] or ["(пусто)"]
        text = ("Собери карту проекта комнаты «%s» из существующих запросов.\n\nЗапросы комнаты:\n%s\n\n"
                "Верни направления (3–8) с исполнителями, зависимостями и сроками."
                % (room.get("title") or room_id, "\n".join("- " + line for line in lines)))
        detail = self.new_live_request(room_id, text, "Собрать карту из запросов комнаты",
                                       bool(room.get("critic_pass")))
        rid = detail["id"]
        self.live[rid]["brief"] = True
        self.live[rid]["brief_kind"] = "map_from_existing"
        self.live[rid]["brief_due"] = time.monotonic() + self._plan_delay(BRIEF_MIN, BRIEF_MAX)
        plan = self.plans.get(room_id)
        if plan is not None:
            self._log(plan, who="manager", kind="created",
                      text="Руководитель собирает карту из запросов комнаты — запрос %s" % rid)
        return 201, {"request": _summary(detail), "root_id": rid, "warning": None}

    def mark_brief(self, rid: str, room_id: str, kind: str = "brief") -> None:
        """`POST /api/rooms/{r}/requests {brief:true}` — ответ с черновиком через 5–8 с."""
        item = self.live.get(rid)
        if item is None:
            return
        item["brief"] = True
        item["brief_kind"] = kind
        item["brief_due"] = time.monotonic() + self._plan_delay(BRIEF_MIN, BRIEF_MAX)

    def plan_tick(self, rid: str) -> None:
        """Доставить черновик карты, когда «руководитель додумал» (вызывается из detail)."""
        item = self.live.get(rid)
        if not item or not item.get("brief") or item.get("brief_due") is None:
            return
        if time.monotonic() < item["brief_due"]:
            return
        item["brief_due"] = None
        self.ingest_draft(item["room"], rid, item.get("brief_kind") or "brief")

    def ingest_draft(self, room_id: str, rid: str, kind: str = "brief") -> dict:
        """Черновик карты из ответа руководителя: все направления proposed, запись в журнал."""
        from mock_thread import author_label, lead_profile

        room = self.room(room_id)
        old = self.plans.get(room_id)
        plan = {"version": (int((old or {}).get("version") or 0) + 1), "brief_root_id": rid,
                "updated_at": now(), "status": "draft", "streams": draft_streams(room), "log": []}
        if old is not None:
            plan["log"] = list(old.get("log") or [])[-20:]
            taken = {s.get("id") for s in plan["streams"]}
            for stream in old["streams"]:
                if stream.get("status") not in ("running", "review") or not stream.get("request_id"):
                    continue
                carried = copy.deepcopy(stream)
                if carried.get("id") in taken:      # id занят новым направлением — выдаём свободный
                    carried["id"] = next_id(plan)
                taken.add(carried["id"])
                carried["reason"] = "Идёт с прошлой карты — не останавливаю"
                plan["streams"].append(carried)
        self.plans[room_id] = plan
        auto = str(room.get("launch_mode") or "plan_first") == "auto"
        if auto:
            # режим комнаты «сразу»: ждать кнопку не нужно (как на сервере, plan.tick)
            queued = [s["id"] for s in plan["streams"] if s.get("status") == "proposed"]
            for stream in plan["streams"]:
                if stream.get("id") in queued:
                    stream["status"] = "queued"
            if queued:
                plan["status"] = "approved"
                self._log(plan, who="system", kind="changed",
                          text="Запуск без подтверждения — режим комнаты «сразу»: направлений — %d"
                               % len(queued))
        lead = lead_profile(room, {})
        names = ", ".join("«%s»" % s["title"] for s in plan["streams"][:4])
        head = ("Пересобрал карту из запросов комнаты" if kind == "map_from_existing"
                else "Принял. Предлагаю карту проекта")
        self._log(plan, who="manager", kind="created",
                  text="Руководитель предложил карту: %d направлений" % len(plan["streams"]))
        self.thread_seq += 1
        self.thread_store.setdefault(rid, []).append({
            "id": "c%d" % self.thread_seq, "at": now(), "author_kind": "agent", "author": lead,
            "author_label": author_label(lead, "orchestrator"),
            "text": (("%s: %d направлений — %s. Запускаю сразу — в комнате режим «сразу»."
                      if auto else
                      "%s: %d направлений — %s. Ничего не запускаю без вашего «Запустить».")
                     % (head, len(plan["streams"]), names)),
            "task_id": rid, "kind": "comment"})
        self.thread_seq += 1
        self.thread_store.setdefault(rid, []).append({
            "id": "e%d" % self.thread_seq, "at": now(), "author_kind": "system", "author": "system",
            "author_label": "Комната команды",
            "text": "Руководитель комнаты предложил карту: %d направлений" % len(plan["streams"]),
            "task_id": rid, "kind": "event"})
        return plan

    # --- поля в RequestDetail ---------------------------------------------
    def plan_meta(self, rid: str, detail: dict) -> None:
        """`streams_summary` брифа и `plan_link` под-запроса — как `server/plan.py:link_for`."""
        room_id = detail.get("room")
        plan = self.plans.get(room_id) or {}
        detail.setdefault("brief", False)
        detail.setdefault("streams_summary", None)
        detail.setdefault("plan_link", None)
        link = next(({"stream_id": s["id"], "title": s["title"], "status": s.get("status")}
                     for s in plan.get("streams") or [] if s.get("request_id") == rid), None)
        if link is not None:
            detail["plan_link"] = link
        if plan.get("brief_root_id") and plan.get("brief_root_id") == rid:
            detail["brief"] = True
            detail["streams_summary"] = summarize(plan.get("streams") or [], plan)

    # --- руководитель правит карту по сообщению Михаила --------------------
    def note_message(self, rid: str, detail: dict, text: str) -> dict | None:
        """Сообщение Михаилу руководителю может добавить предложенное направление (§2, A6).

        Правило простое и предсказуемое для демо: если в сообщении есть ключевые слова
        темы, которой в карте ещё нет, руководитель отвечает новой строкой в журнале
        `added` и направлением в статусе `proposed` (запуск — только по «Запустить»).
        """
        room_id = detail.get("room")
        plan = self.plans.get(room_id)
        if plan is None or not self.room(room_id).get("project_mode"):
            return None
        if plan.get("brief_root_id") != rid and not plan.get("streams"):
            return None
        low = str(text or "").casefold()
        topic = next(((words, title, goal) for words, title, goal in NOTE_TOPICS
                      if any(word in low for word in words)), None)
        if topic is None:
            return None
        _, title, goal = topic
        if any(s.get("title", "").casefold() == title.casefold() for s in plan["streams"]):
            return None
        if len(plan["streams"]) >= MAX_STREAMS:
            self._log(plan, who="manager", kind="changed",
                      text="Хотел добавить «%s», но в карте уже %d направлений — сначала закройте лишние"
                           % (title, MAX_STREAMS))
            return None
        owner = self.room(room_id).get("orchestrator")
        room_now = self.room(room_id)
        stream = {"id": next_id(plan), "title": title, "goal": goal, "owner": owner,
                  "owner_label": owner_label(room_now, owner),
                  "members": [owner], "depends_on": [], "due": None, "status": "proposed",
                  "request_id": None,
                  "reason": "Добавил руководитель по сообщению Михаила: «%s»" % _short_note(text),
                  "created_by": "manager"}
        plan["streams"].append(stream)
        self._log(plan, who="manager", kind="added",
                  text="Добавил направление «%s» по сообщению Михаила — ждёт «Запустить»" % title,
                  stream_id=stream["id"])
        return stream


def _short_note(text: str, limit: int = 90) -> str:
    """Короткая суть сообщения Михаила для журнала плана (без markdown и переносов)."""
    line = re.sub(r"[#*`>]+", "", str(text or "")).strip().replace("\n", " ")
    if len(line) > limit:
        line = line[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return line or "без текста"


def _show(value) -> str:
    if value in (None, "", []):
        return "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)[:60]


def _summary(detail: dict) -> dict:
    from mock_core import detail_to_summary
    return detail_to_summary(detail)
