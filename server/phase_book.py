"""Этапы проекта — свойство КОМНАТЫ, а не список в коде (WAVE17 §R, B-8).

Раньше «Карта проекта» показывала всем комнатам один и тот же список из шести
этапов (константа `PHASES`), потому что он был зашит в трёх местах. Теперь у
комнаты есть необязательное поле `phases: [{id, title}]`:

* новая комната и комната без этапов — полосы этапов нет (никаких заглушек);
* этапы добавляет, переименовывает, переставляет и удаляет сам пользователь в
  настройках комнаты;
* переименование и порядок не ломают привязку данных: всё связывается по `id`.

Модуль держит три вещи в одном месте, чтобы не было второго мнения о этапах:

1. `check` / `norm` — запись и чтение списка этапов комнаты;
2. `stage` / `current` — «этап × статус из metadata ответа» для страницы запроса;
3. `migrate` — миграция ЧТЕНИЕМ: у комнат, которые уже вели карту (в
   `state/<комната>/roadmap.json` есть реальные статусы/заметки), этапы
   подхватываются из их собственного файла. Данные не переписываются, у
   комнаты без следов карты этапов не появляется.
4. W20 §1.7 (P1) — ПРЕДЛОЖЕНИЕ этапов руководителем: `capture` кладёт
   `metadata.phases_proposal` в поле комнаты `phases_proposal` и НЕ трогает
   этапы пользователя, `decide` применяет решение (принять все / выбранные /
   отклонить), `propose` ставит карточку «предложи этапы». Порядок,
   переименования, удаления и добавления пользователя сохраняются всегда.

Ни одного названия этапа какого-либо проекта в коде нет: заголовки приходят
только из данных комнаты (или из её файла карты при миграции).
"""
from __future__ import annotations

import hashlib
import re
import time

try:  # пакетный и «плоский» импорт (python server/app.py)
    from . import statefiles as sf
except ImportError:  # pragma: no cover
    import statefiles as sf  # type: ignore

MAX_PHASES = 24
MAX_TITLE = 80
MAX_ID = 40
PHASE_STATUSES = ("done", "active", "next", "blocked")
# Статусы, которые считаются «в этой комнате карту действительно вели»:
# миграция чтением не должна принимать за данные пустую заготовку.
REAL_STATUSES = ("done", "active", "blocked")

_ID_RE = re.compile(r"[^a-zа-я0-9_-]+")


class PhaseError(Exception):
    """Ошибка списка этапов: сообщение по-русски для 400 от роута."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


def _slug(text) -> str:
    """Заголовок → стабильный id этапа (латиница/кириллица, цифры, `-`, `_`)."""
    raw = str(text or "").strip().lower()
    slug = _ID_RE.sub("-", raw).strip("-_")
    return slug[:MAX_ID].strip("-_") or "phase"


def _title(value) -> str:
    return " ".join(str(value or "").split())[:MAX_TITLE]


def norm(value) -> list[dict]:
    """Прочитать список этапов терпимо: мусор не роняет карту комнаты.

    Принимается список `{id?, title}`; id выводится из заголовка, если его нет.
    Повторы id и пустые заголовки отбрасываются — порядок сохраняется.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = _title(item.get("title") or item.get("name"))
        pid = _slug(item.get("id")) if str(item.get("id") or "").strip() else _slug(title)
        if not title or not pid or pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "title": title})
        if len(out) >= MAX_PHASES:
            break
    return out


def check(value) -> list[dict]:
    """Проверить список этапов для записи (PATCH комнаты).

    Отличия от `norm`: пустой список допустим (значит «этапов нет»), а всё
    сомнительное — ошибка с понятным текстом, а не молчаливая потеря.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise PhaseError("Этапы проекта: нужен список этапов", {"field": "phases"})
    if len(value) > MAX_PHASES:
        raise PhaseError("Этапы проекта: не больше %d этапов" % MAX_PHASES,
                         {"field": "phases", "max": MAX_PHASES})
    out: list[dict] = []
    seen: set[str] = set()
    for pos, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            raise PhaseError("Этап %d: нужен объект с заголовком" % pos,
                             {"field": "phases", "index": pos})
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            raise PhaseError("Этап %d: нужен заголовок" % pos, {"field": "phases", "index": pos})
        if len(title) > MAX_TITLE:
            raise PhaseError("Этап %d: заголовок не длиннее %d знаков (сейчас %d)"
                             % (pos, MAX_TITLE, len(title)),
                             {"field": "phases", "index": pos, "max": MAX_TITLE})
        pid = _slug(item.get("id")) if str(item.get("id") or "").strip() else _slug(title)
        if pid in seen:
            raise PhaseError("Этап %d: повторяется id «%s»" % (pos, pid),
                             {"field": "phases", "index": pos, "id": pid})
        seen.add(pid)
        out.append({"id": pid, "title": title})
    return out


def ids(phases) -> list[str]:
    return [str(p.get("id")) for p in (phases or []) if isinstance(p, dict) and p.get("id")]


def by_id(phases) -> dict:
    return {str(p["id"]): p for p in (phases or []) if isinstance(p, dict) and p.get("id")}


def _derive_current(phases: list[dict]) -> str | None:
    """Текущий этап: первый active|blocked, иначе последний done, иначе первый next."""
    for phase in phases:
        if phase.get("status") in ("active", "blocked"):
            return phase["id"]
    done = [p["id"] for p in phases if p.get("status") == "done"]
    if done:
        return done[-1]
    for phase in phases:
        if phase.get("status") == "next":
            return phase["id"]
    return None


def statuses_of(canon: dict) -> dict:
    """Статусы этапов из metadata ответа: `{id: {status, note}}` (чужие id не берём)."""
    stage = canon.get("stage") if isinstance(canon, dict) else None
    if not isinstance(stage, dict):
        return {}
    out: dict = {}
    for item in stage.get("phases") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        pid = str(item["id"])
        status = item.get("status") if item.get("status") in PHASE_STATUSES else "next"
        out[pid] = {"status": status, "note": str(item.get("note") or "")}
    return out


def _key(text) -> str:
    """Ключ сравнения названий: регистр, лишние пробелы, ё/е не важны."""
    return " ".join(str(text or "").lower().replace("ё", "е").split())


_PUNCT_RE = re.compile(r"[^\w\s-]+", re.UNICODE)


def _plain(text) -> str:
    """Ключ без знаков препинания: «Проверка спроса, цен» = «проверка спроса цен»."""
    return " ".join(_PUNCT_RE.sub(" ", _key(text)).split())


def _meta_index(canon: dict) -> tuple[dict, dict]:
    """Metadata ответа → (по id, по названию). Второй — для старых ключей-названий."""
    by_ident: dict = {}
    by_title: dict = {}
    stage = canon.get("stage") if isinstance(canon, dict) else None
    if not isinstance(stage, dict):
        return by_ident, by_title
    for item in stage.get("phases") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        status = item.get("status") if item.get("status") in PHASE_STATUSES else "next"
        pid = str(item["id"])
        got = {"id": pid, "status": status, "note": str(item.get("note") or "")}
        by_ident[pid] = got
        by_title.setdefault(_key(item.get("title")), got)
        by_title.setdefault(_key(pid), got)
    return by_ident, by_title


def stage(phases, canon: dict | None = None) -> dict:
    """API §3.4: `{current, phases[]}` ТОЛЬКО по этапам комнаты.

    Порядок и заголовки — из комнаты (переименование не ломает связку по id),
    статус и заметка — из metadata ответа. Этапы, которых комната не объявила,
    в ответ не попадают: карта комнаты не растёт сама по себе.

    Связка «этап комнаты ↔ этап из metadata» сначала по `id`, затем по
    названию: старые ответы называли этапы словами, а комната может называть
    их иначе — статус всё равно не теряется.
    """
    declared = norm(phases)
    if not declared:
        return {"current": None, "phases": []}
    by_ident, by_title = _meta_index(canon or {})
    out = []
    source_of: dict = {}
    for item in declared:
        got = by_ident.get(item["id"]) or by_title.get(_key(item["title"])) or {}
        source_of[item["id"]] = got.get("id")
        out.append({"id": item["id"], "title": item["title"],
                    "status": got.get("status") or "next", "note": got.get("note") or ""})
    raw_current = (canon or {}).get("stage")
    current = None
    if isinstance(raw_current, dict):
        raw_current = str(raw_current.get("current") or "")
        if raw_current in by_id(out):
            current = raw_current
        else:
            hit = by_ident.get(raw_current) or by_title.get(_key(raw_current))
            if hit and hit.get("id") in by_id(out):
                current = hit["id"]
            elif hit:
                current = next((pid for pid, src in source_of.items() if src == hit.get("id")), None)
    if current is None:
        current = _derive_current(out)
    return {"current": current, "phases": out}


# --- W20 §1.7 (P1): предложение этапов руководителем -------------------------
#
# Руководитель присылает предложение в `metadata.phases_proposal` ЛЮБОЙ своей
# карточки. Приложение кладёт его в поле комнаты `phases_proposal` и НЕ трогает
# `phases`: порядок, переименования, удаления и добавления пользователя остаются
# его. Решение («принять все / принять выбранные / отклонить») — отдельным
# действием: принятые этапы ДОПИСЫВАЮТСЯ в конец `phases`, а уже существующие
# (тот же id или то же название) не переименовываются и не переставляются.

PROPOSAL_STATES = ("proposed", "accepted", "rejected")
PROPOSAL_ACTIONS = ("accept", "accept_selected", "reject")
MAX_DESCRIPTION = 400
PROPOSE_TITLE = "Какие этапы у проекта"


def _num(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _desc(value) -> str:
    return " ".join(str(value or "").split())[:MAX_DESCRIPTION]


def _plural(count: int, forms: tuple) -> str:
    """«1 этап / 2 этапа / 5 этапов» — по-русски, без «этап(ов)»."""
    number = abs(int(count or 0)) % 100
    if 11 <= number <= 14:
        return forms[2]
    tail = number % 10
    if tail == 1:
        return forms[0]
    if 2 <= tail <= 4:
        return forms[1]
    return forms[2]


def norm_items(value, *, strict: bool = False) -> list[dict]:
    """`[{id,title,description}]` — терпимо; `strict=True` → PhaseError с позицией.

    Терпимое чтение нужно для данных от руководителя и для старых записей реестра:
    мусор не должен ронять карту комнаты. Строгое — для записи после проверки.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        if strict:
            raise PhaseError("Этапы проекта: нужен список этапов", {"field": "phases_proposal"})
        return []
    if strict and len(value) > MAX_PHASES:
        raise PhaseError("Этапы проекта: не больше %d этапов" % MAX_PHASES,
                         {"field": "phases_proposal", "max": MAX_PHASES})
    out: list[dict] = []
    seen: set[str] = set()
    for pos, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            if strict:
                raise PhaseError("Этап %d: нужен объект с заголовком" % pos,
                                 {"field": "phases_proposal", "index": pos})
            continue
        title = " ".join(str(item.get("title") or item.get("name") or "").split())
        if not title:
            if strict:
                raise PhaseError("Этап %d: нужен заголовок" % pos,
                                 {"field": "phases_proposal", "index": pos})
            continue
        if len(title) > MAX_TITLE:
            if strict:
                raise PhaseError("Этап %d: заголовок не длиннее %d знаков (сейчас %d)"
                                 % (pos, MAX_TITLE, len(title)),
                                 {"field": "phases_proposal", "index": pos, "max": MAX_TITLE})
            title = title[:MAX_TITLE].strip()
        raw_id = str(item.get("id") or "").strip()
        pid = _slug(raw_id) if raw_id else _slug(title)
        if pid in seen:
            if strict:
                raise PhaseError("Этап %d: повторяется id «%s»" % (pos, pid),
                                 {"field": "phases_proposal", "index": pos, "id": pid})
            continue
        seen.add(pid)
        out.append({"id": pid, "title": title, "description": _desc(item.get("description"))})
        if len(out) >= MAX_PHASES:
            break
    return out


def signature(items) -> str:
    """Отпечаток предложения: по id И названию (переименование = другое предложение)."""
    raw = "|".join("%s=%s" % (item.get("id"), _key(item.get("title")))
                   for item in (items or []))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _title_seq(items) -> list[str]:
    return [_key(item.get("title")) for item in (items or [])]


def norm_proposal(value) -> dict | None:
    """Чтение `phases_proposal`: мусор и пустое предложение — `None` (его нет)."""
    if not isinstance(value, dict):
        return None
    items = norm_items(value.get("items"))
    if not items:
        return None
    state = str(value.get("state") or "").strip().lower()
    if state not in PROPOSAL_STATES:
        state = "proposed"
    out = {"at": _num(value.get("at")), "by": str(value.get("by") or "").strip(),
           "state": state, "items": items,
           "signature": str(value.get("signature") or "").strip() or signature(items),
           "source": str(value.get("source") or "").strip(),
           "note": _desc(value.get("note"))}
    if _num(value.get("decided_at")):
        out["decided_at"] = _num(value.get("decided_at"))
    accepted = value.get("accepted_ids")
    if isinstance(accepted, list):
        out["accepted_ids"] = [str(x) for x in accepted if str(x or "").strip()]
    return out


def proposal_of(room) -> dict | None:
    """Предложение комнаты в каноническом виде (или None)."""
    return norm_proposal(room.get("phases_proposal")) if isinstance(room, dict) else None


def parse_meta_proposal(raw) -> tuple[list[dict], list[str]]:
    """`metadata.phases_proposal` → (этапы, предупреждения).

    Принимается список объектов, один объект или список строк. Всё непонятное —
    в предупреждения (это текст для журнала/ответа, не ошибка приёма ответа).
    """
    warnings: list[str] = []
    if raw is None:
        return [], warnings
    if isinstance(raw, dict) and isinstance(raw.get("items"), (list, tuple)):
        raw = raw.get("items")                       # уже готовая форма предложения
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        warnings.append("Предложение этапов не принято: нужен список этапов")
        return [], warnings
    items = norm_items(raw)
    if not items:
        warnings.append("Предложение этапов пустое — пропущено")
        return [], warnings
    dropped = len(raw) - len(items)
    if dropped > 0:
        warnings.append("В предложении этапов %d пункт(ов) без названия — пропущены" % dropped)
    if len(items) > MAX_PHASES:
        warnings.append("Этапов в предложении больше %d — взяты первые %d"
                        % (len(raw), MAX_PHASES))
        items = items[:MAX_PHASES]
    return items, warnings


def capture(room, items, by: str = "", source: str = "", at=None) -> dict:
    """Положить предложение руководителя в комнату, НЕ трогая `phases`.

    Возвращает `{applied, reason, proposal?, state}`:
    * `empty` — этапов нет (ничего не меняем);
    * `unchanged` — то же предложение уже лежит (повторное чтение карточки
      не сбрасывает решение пользователя — в том числе «отклонено»);
    * `same_as_phases` — предложение совпадает с этапами комнаты: показывать
      нечего, этапы уже такие;
    * `proposed` — записано новое предложение (`state="proposed"`).
    """
    got = norm_items(items)
    if not got:
        return {"applied": False, "reason": "empty"}
    sig = signature(got)
    current = proposal_of(room) or {}
    if current.get("signature") == sig:
        return {"applied": False, "reason": "unchanged", "state": current.get("state"),
                "proposal": current}
    phases = norm((room or {}).get("phases")) if isinstance(room, dict) else []
    if phases and _title_seq(phases) == _title_seq(got):
        return {"applied": False, "reason": "same_as_phases", "proposal": current or None}
    proposal = {"at": _num(at) or int(time.time()), "by": str(by or "").strip(),
                "state": "proposed", "items": got, "signature": sig,
                "source": str(source or "").strip()}
    return {"applied": True, "reason": "proposed", "proposal": proposal}


def _same_title(left, right) -> bool:
    """Один и тот же этап, названный чуть иначе: «Проверка спроса» / «… , цен и меню».

    Человек переименовывает этап под себя — принятие предложения не должно
    создавать второй такой же этап. Совпадением считаем равные названия (без
    учёта регистра, ё/е и знаков препинания) и продолжение короткого названия
    целым словом (не «Идея» ↔ «Идейный»).
    """
    a, b = _plain(left), _plain(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short.split()) < 2:
        return False
    return long.startswith(short + " ")


def decide(phases, proposal, action: str, selected=None, at=None) -> dict:
    """Решение по предложению: `{phases, proposal, added, kept, skipped}`.

    `accept` — все этапы предложения, `accept_selected` — только `ids`,
    `reject` — этапы не меняются. Существующие этапы пользователя не
    переименовываются и не переставляются: новые ДОПИСЫВАЮТСЯ в конец.
    """
    action = str(action or "").strip()
    if action not in PROPOSAL_ACTIONS:
        raise PhaseError("Действие: принять все, принять выбранные или отклонить",
                         {"field": "action", "allowed": list(PROPOSAL_ACTIONS)})
    prop = norm_proposal(proposal)
    if prop is None:
        raise PhaseError("Руководитель ещё не предлагал этапы", {"reason": "no_proposal"})
    cur = norm(phases)
    now = _num(at) or int(time.time())
    if action == "reject":
        decided = dict(prop, state="rejected", decided_at=now, accepted_ids=[])
        return {"phases": cur, "proposal": decided, "added": [], "kept": ids(cur),
                "skipped": [], "state": "rejected"}
    chosen = list(prop["items"])
    if action == "accept_selected":
        wanted = {str(x).strip() for x in (selected or []) if str(x or "").strip()}
        if not wanted:
            raise PhaseError("Выберите хотя бы один этап", {"field": "ids", "reason": "empty_selection"})
        known = {item["id"] for item in prop["items"]}
        unknown = sorted(wanted - known)
        if unknown:
            raise PhaseError("В предложении нет таких этапов: %s" % ", ".join(unknown),
                             {"field": "ids", "unknown": unknown})
        chosen = [item for item in prop["items"] if item["id"] in wanted]
    have_ids = ids(cur)
    have_titles = [item["title"] for item in cur]
    out = list(cur)
    added: list[str] = []
    skipped: list[str] = []
    for item in chosen:
        if item["id"] in have_ids or any(_same_title(item["title"], title) for title in have_titles):
            skipped.append(item["id"])          # правку пользователя не затираем
            continue
        if len(out) >= MAX_PHASES:
            raise PhaseError("Нельзя добавить этапы: в комнате уже %d" % MAX_PHASES,
                             {"max": MAX_PHASES, "reason": "too_many"})
        out.append({"id": item["id"], "title": item["title"]})
        have_ids.append(item["id"])
        have_titles.append(item["title"])
        added.append(item["id"])
    decided = dict(prop, state="accepted", decided_at=now,
                   accepted_ids=[item["id"] for item in chosen])
    return {"phases": out, "proposal": decided, "added": added, "kept": have_ids,
            "skipped": skipped, "state": "accepted"}


def reconcile(phases, proposal) -> dict | None:
    """Правка пользователя совпала с предложением → предложение считается принятым.

    Нужно, чтобы плашка «Руководитель предлагает этапы» не висела после того,
    как пользователь сам собрал в редакторе ровно те же этапы.
    """
    prop = norm_proposal(proposal)
    if prop is None or prop.get("state") != "proposed":
        return prop
    if _title_seq(norm(phases)) == _title_seq(prop["items"]):
        return dict(prop, state="accepted", decided_at=int(time.time()),
                    accepted_ids=ids(prop["items"]), auto=True)
    return prop


def state_text(proposal) -> str:
    """Короткая строка для сводки комнаты (её же показывает карта проекта)."""
    prop = norm_proposal(proposal)
    if prop is None:
        return ""
    count = len(prop["items"])
    words = _plural(count, ("этап", "этапа", "этапов"))
    if prop["state"] == "proposed":
        titles = ", ".join(item["title"] for item in prop["items"][:3])
        if count > 3:
            titles += "…"
        return "Руководитель предлагает %d %s: %s" % (count, words, titles)
    if prop["state"] == "accepted":
        return "Этапы приняты (%d %s)" % (count, words)
    return "Предложение этапов отклонено (%d %s)" % (count, words)


def propose_body(room, phases, note: str = "") -> str:
    """Текст карточки «предложи этапы»: что нужно и в каком виде ответить."""
    title = str((room or {}).get("title") or (room or {}).get("id") or "комнаты")
    lines = ["Предложите этапы проекта для комнаты «%s»." % title, "",
             "Нужен только список этапов — по порядку, от первого к последнему: 3–8 штук.",
             "У каждого этапа короткое название (до 60 знаков) и одна строка пояснения, "
             "что в этот этап входит.", "",
             "Как ответить: завершите карточку с metadata",
             '  phases_proposal: [{"title": "<название>", "description": "<пояснение>"}, …]', "",
             "Работу по проекту не начинайте и других карточек не создавайте: этапы сначала "
             "посмотрит и поправит человек — он их примет или отклонит сам.", ""]
    if str(note or "").strip():
        lines += ["Что учесть в этот раз:", str(note).strip(), ""]
    if phases:
        lines.append("Этапы, которые уже заданы в комнате (их правил человек):")
        lines += ["%d. %s" % (pos, item["title"]) for pos, item in enumerate(phases, 1)]
        lines += ["", "Уже заданные этапы не переписывайте: в предложение включайте то, что "
                      "считаете нужным добавить или уточнить, — приложение покажет разницу, "
                      "а человек решит сам.", ""]
    else:
        lines += ["Этапов в комнате пока нет — предложите полный список.", ""]
    return "\n".join(lines).strip()


def propose(cfg, room, note: str = "", body: str | None = None) -> dict:
    """Создать карточку руководителю «предложи этапы» (W20 §1.7).

    Только постановка карточки: модели запускает штатный диспетчер, как у любого
    запроса. Ответ руководителя вернётся в `metadata.phases_proposal` и попадёт
    в комнату сам (`ingest` → `capture`).
    """
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "").strip()
    if not room_id:
        raise PhaseError("Не передана комната", {"reason": "no_room"})
    lead = str(room.get("orchestrator") or "").strip()
    if not lead:
        raise PhaseError("В комнате не назначен руководитель — предложить этапы некому",
                         {"reason": "no_lead"})
    try:
        from . import engine_write as eng
        from . import registry as reg
    except ImportError:  # pragma: no cover - плоский импорт
        import engine_write as eng  # type: ignore
        import registry as reg  # type: ignore
    text = body if isinstance(body, str) and body.strip() else propose_body(
        room, phases_of(cfg, room), note)
    slug = "phases-%s-%s" % (room_id, int(time.time()))
    try:
        res = eng.create_request(cfg, room, slug, PROPOSE_TITLE, text)
    except Exception as exc:  # noqa: BLE001 - ошибку движка отдаём по-русски
        message = (getattr(exc, "message_ru", None) or getattr(exc, "message", None)
                   or str(exc) or "движок не ответил")
        raise PhaseError("Не удалось поставить карточку руководителю: %s" % str(message)[:160],
                         {"reason": "create_failed"}) from exc
    root_id = str((res or {}).get("root_id") or (res or {}).get("id") or "")
    if not root_id:
        raise PhaseError("Движок не вернул id карточки «предложи этапы»",
                         {"reason": "no_card"})
    try:                       # реестр вторичен: карточка уже создана
        reg.add_request(cfg, room_id, {
            "root_id": root_id, "slug": slug, "title": PROPOSE_TITLE, "text": text,
            "created_at": int(time.time()), "critic": False, "mode": "auto",
            "adopted": False, "extra_cards": [], "ingested": {}, "retargeted": False,
            "kind": "phases_proposal",
        })
    except Exception:  # noqa: BLE001
        pass
    return {"root_id": root_id, "title": PROPOSE_TITLE, "slug": slug, "body": text,
            "warning": (res or {}).get("warning")}


def _roadmap_path(cfg, room_id: str):
    from pathlib import Path
    return Path(cfg.state_dir) / str(room_id) / "roadmap.json"


def migrate(cfg, room_id: str) -> list[dict]:
    """Миграция ЧТЕНИЕМ: этапы комнаты из её собственного файла карты.

    Берём файл `state/<комната>/roadmap.json` — это то, что комната уже вела
    (его пишет ingest из metadata ответов). Принимаем список только если в нём
    есть настоящие следы работы: статус кроме «next», заметка или запрос-источник.
    Пустая заготовка (шесть строк «впереди», как её когда-то создавала константа)
    данных не даёт: у такой комнаты этапов нет.

    Ничего не записываем: файл комнаты и `rooms.json` остаются как были.
    """
    rid = str(room_id or "").strip()
    if not rid:
        return []
    try:
        data = sf.read_json(_roadmap_path(cfg, rid), None)
    except Exception:  # noqa: BLE001 — миграция вторична, карта не должна падать
        return []
    if not isinstance(data, dict):
        return []
    stored = [p for p in (data.get("phases") or []) if isinstance(p, dict)]
    evidence = any(
        str(p.get("status") or "") in REAL_STATUSES
        or str(p.get("note") or "").strip()
        or str(p.get("source_request") or "").strip()
        for p in stored
    )
    if not evidence:
        return []
    return norm([{"id": p.get("id"), "title": p.get("title")} for p in stored])


def phases_of(cfg, room) -> list[dict]:
    """Этапы комнаты: явное поле `phases` или миграция чтением из её карты."""
    if isinstance(room, dict):
        if isinstance(room.get("phases"), list):
            return norm(room.get("phases"))
        return migrate(cfg, str(room.get("id") or ""))
    if isinstance(room, str) and room:
        return migrate(cfg, room)
    return []


def current_of(cfg, room, phases=None) -> dict | None:
    """Текущий этап комнаты по её файлу карты: `{id,title,status}` или None."""
    rid = str(room.get("id") if isinstance(room, dict) else room or "").strip()
    declared = phases if isinstance(phases, list) else phases_of(cfg, room)
    if not rid or not declared:
        return None
    try:
        data = sf.read_json(_roadmap_path(cfg, rid), None)
    except Exception:  # noqa: BLE001
        data = None
    stored = {str(p.get("id")): p for p in
              ((data.get("phases") or []) if isinstance(data, dict) else []) if isinstance(p, dict)}
    items = []
    for item in declared:
        got = stored.get(item["id"]) or {}
        status = got.get("status") if got.get("status") in PHASE_STATUSES else "next"
        items.append({"id": item["id"], "title": item["title"], "status": status})
    raw_current = data.get("current") if isinstance(data, dict) else None
    if isinstance(raw_current, dict):
        raw_current = raw_current.get("id")
    current_id = str(raw_current or "")
    if current_id not in by_id(items):
        current_id = _derive_current(items) or ""
    found = by_id(items).get(current_id)
    return dict(found) if found else None


__all__ = ["MAX_PHASES", "MAX_TITLE", "PHASE_STATUSES", "PhaseError", "norm", "check", "ids",
           "by_id", "statuses_of", "stage", "migrate", "phases_of", "current_of",
           # W20 §1.7 (P1): предложение этапов руководителем
           "PROPOSAL_STATES", "PROPOSAL_ACTIONS", "MAX_DESCRIPTION", "PROPOSE_TITLE",
           "norm_items", "signature", "norm_proposal", "proposal_of", "parse_meta_proposal",
           "capture", "decide", "reconcile", "state_text", "propose_body", "propose"]
