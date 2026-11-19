"""Правила дежурств и разбор результата запуска (W27-E7, `docs/WAVE27-RULES.md` §3).

Источник расписания — Hermes (`cron/jobs.json`), а **правила и история** — здесь,
в ``data/state/<room>/duties.json``: без второй базы расписаний.

Что здесь:

* хранилище правил ``rules[<job_id>] = {changed_rule, on_change, level, notify,
  source, template}``, корзина ``trash[]`` (30 дней) и ``history[]`` запусков;
* разбор машинного блока ```` ```результат ```` из вывода запуска (``parse_result``);
* ``process_runs(room)`` — что делать с результатом: тихая строка в истории
  (`изменилось: нет`) или карточка во «Входящих» / Дело — по правилу, с уровнем
  автономии, лимитом ≤ 5 обработок за тик и идемпотентностью по `run_id`;
* карточки «Что выполнено / Что обнаружено / Предлагаю» и решения по ним
  (`decide_card`): принять (создать Дело), изменить, «не надо», «сделать правилом»;
* предложения руководителя и черновики из диктовки (`proposals`,
  `sync_inbox_drafts`) — не больше двух в неделю, отказ запоминается.

Границы: наружу (письмо, платёж, задача в чужой системе) отсюда **ничего не уходит**.
`level: self` разрешает только обратимые записи внутри комнаты (карточка, Дело, шаг).

    from server import duty_rules
    duty_rules.process_runs(cfg, room)
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

try:                                      # пакетный импорт
    from . import delo as delo_mod
    from . import duties as duties_mod
    from . import inbox as inbox_mod
    from . import rooms as rooms_mod
    from . import statefiles
except ImportError:                       # pragma: no cover - плоский импорт
    import delo as delo_mod               # type: ignore
    import duties as duties_mod           # type: ignore
    import inbox as inbox_mod             # type: ignore
    import rooms as rooms_mod             # type: ignore
    import statefiles                     # type: ignore

#: Что бывает при изменении (контракт §3).
ON_CHANGE = ("log", "inbox", "delo", "step", "question")
ON_CHANGE_TITLES = {"log": "только журнал", "inbox": "входящие", "delo": "предложить дело",
                    "step": "предложить шаг", "question": "вопрос мне"}
#: Уровень автономии правила.
LEVELS = ("self", "propose", "ask")
LEVEL_TITLES = {"self": "сам (обратимое внутри комнаты)", "propose": "предложить",
                "ask": "спросить"}
#: Откуда правило взялось.
SOURCES = ("manual", "manager", "dictation", "delo")
SOURCE_TITLES = {"manual": "создано вручную", "manager": "предложил руководитель",
                 "dictation": "из диктовки", "delo": "из дела"}
#: Пределы. Обработок за тик — не больше пяти (контракт §3), история и корзина конечны.
MAX_PROCESS_PER_TICK = 5
HISTORY_MAX = 400
TRASH_MAX = 60
TRASH_DAYS = 30
CARD_TEXT_MAX = 4000
CHANGED_RULE_MAX = 300
#: Предложения руководителя: не больше двух в неделю на комнату.
MAX_PROPOSALS_WEEK = 2
PROPOSALS_MAX = 60
PROPOSAL_TTL_DAYS = 21
#: Сколько файлов вывода задания смотрим за один проход (свежие сверху).
OUTPUT_SCAN = 20

#: Машинный блок результата: ```результат … ```
RESULT_MARKS = ("результат", "result")
_BLOCK_RE = re.compile(r"^```\s*(результат|result)\s*$(.*?)^```\s*$", re.M | re.S | re.I)

#: Ключи блока → поля ответа (с «ё» и без).
_KEY_MAP = {
    "изменилось": "changed", "changed": "changed",
    "сделано": "done", "done": "done",
    "обнаружено": "found", "found": "found",
    "предложения": "proposals", "proposals": "proposals",
    "ссылки": "links", "links": "links",
}
#: Ключи одного предложения.
_PROPOSAL_KEYS = {"что": "what", "what": "what", "почему": "why", "why": "why",
                  "действие": "action", "action": "action"}

#: Кнопки карточки результата (контракт §3): принять / изменить / не надо / правилом.
CARD_ACTIONS = [
    {"id": "accept", "label": "Принять"},
    {"id": "edit", "label": "Изменить"},
    {"id": "reject", "label": "Не надо"},
    {"id": "make_rule", "label": "Сделать правилом"},
]


class DutyRuleError(Exception):
    """Ошибка правил дежурства: HTTP-статус, русский текст, машинный код."""

    def __init__(self, message: str, http: int = 400, code: str = "bad_request", detail=None):
        super().__init__(message)
        self.message = str(message)
        self.http = int(http)
        self.code = str(code)
        self.detail = dict(detail or {})


# ------------------------------------------------------------------ хранилище

def _now(now=None) -> int:
    return int(now if now is not None else time.time())


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(int(stamp), timezone.utc).astimezone().isoformat(timespec="seconds")


def room_id_of(room) -> str:
    """Комната словарём или строкой — всегда id (``""``, если не понять)."""
    if isinstance(room, dict):
        return str(room.get("id") or "").strip()
    return str(room or "").strip()


def store_path(cfg, room) -> Path:
    """``data/state/<room>/duties.json`` — правила, корзина и история комнаты."""
    return Path(str(cfg.data_dir)) / "state" / room_id_of(room) / "duties.json"


def _empty(room_id: str) -> dict:
    return {"version": 1, "room": room_id, "rules": {}, "trash": [], "history": [],
            "updated_at": None}


def load(cfg, room) -> dict:
    """Прочитать файл правил комнаты (битый — в карантин, начинаем с чистого)."""
    room_id = room_id_of(room)
    value, info = statefiles.read_json_doc(store_path(cfg, room_id), None)
    if info.get("state") == "corrupt":
        statefiles.quarantine(info.get("path") or "")
        return _empty(room_id)
    if not isinstance(value, dict):
        return _empty(room_id)
    out = dict(value)
    out["room"] = str(value.get("room") or room_id)
    out["rules"] = {str(key): dict(one) for key, one in (value.get("rules") or {}).items()
                    if isinstance(one, dict)}
    for key in ("trash", "history"):
        rows = value.get(key)
        out[key] = [one for one in rows if isinstance(one, dict)] if isinstance(rows, list) else []
    return out


def save(cfg, room, doc: dict) -> dict:
    doc = doc if isinstance(doc, dict) else _empty(room_id_of(room))
    doc["room"] = room_id_of(room)
    doc["rules"] = doc.get("rules") if isinstance(doc.get("rules"), dict) else {}
    doc["trash"] = [one for one in (doc.get("trash") or []) if isinstance(one, dict)][-TRASH_MAX:]
    doc["history"] = [one for one in (doc.get("history") or []) if isinstance(one, dict)][-HISTORY_MAX:]
    doc["updated_at"] = _iso(_now())
    path = store_path(cfg, room)
    path.parent.mkdir(parents=True, exist_ok=True)
    statefiles.write_json_atomic(path, doc)
    return doc


# --------------------------------------------------------------------- правила

def _text(value, limit: int = CHANGED_RULE_MAX) -> str:
    return " ".join(str(value or "").split())[:limit]


def check_rule(payload: dict, base: dict | None = None) -> dict:
    """Проверить и собрать правило: ``on_change``, ``level``, ``notify``, текст."""
    data = payload if isinstance(payload, dict) else {}
    out = dict(base or {})
    if "changed_rule" in data:
        out["changed_rule"] = _text(data.get("changed_rule"))
    out.setdefault("changed_rule", "")
    if "on_change" in data or "on_change" not in out:
        choice = str(data.get("on_change") or out.get("on_change") or "inbox").strip().lower()
        if choice not in ON_CHANGE:
            raise DutyRuleError("Неизвестно, что делать при изменении: %s" % choice,
                                400, "bad_request",
                                {"field": "on_change", "allowed": list(ON_CHANGE)})
        out["on_change"] = choice
    if "level" in data or "level" not in out:
        level = str(data.get("level") or out.get("level") or "propose").strip().lower()
        if level not in LEVELS:
            raise DutyRuleError("Неизвестный уровень автономии: %s" % level, 400, "bad_request",
                                {"field": "level", "allowed": list(LEVELS)})
        out["level"] = level
    if "notify" in data:
        raw = data.get("notify")
        if raw is None:
            out["notify"] = []
        elif not isinstance(raw, list):
            raise DutyRuleError("«Куда сообщить» — список", 400, "bad_request",
                                {"field": "notify"})
        else:
            out["notify"] = [str(one).strip()[:24] for one in raw if str(one or "").strip()][:6]
    out.setdefault("notify", [])
    if "source" in data:
        source = str(data.get("source") or "manual").strip().lower()
        out["source"] = source if source in SOURCES else "manual"
    out.setdefault("source", "manual")
    if "template" in data:
        out["template"] = _text(data.get("template"), 40)
    out["updated_at"] = _now()
    return out


def rule(cfg, room, job_id) -> dict | None:
    """Правило дежурства (``None`` — правила нет: дежурство живёт без реакции)."""
    doc = load(cfg, room)
    found = doc["rules"].get(str(job_id or ""))
    return dict(found) if isinstance(found, dict) else None


def set_rule(cfg, room, job_id, payload: dict, base: dict | None = None) -> dict:
    """Записать правило (создание дежурства и ``PATCH`` используют один путь)."""
    job_id = str(job_id or "").strip()
    if not job_id:
        raise DutyRuleError("Не указано дежурство", 400, "bad_request", {"field": "job_id"})
    doc = load(cfg, room)
    current = doc["rules"].get(job_id) if isinstance(doc["rules"].get(job_id), dict) else base
    made = check_rule(payload, current)
    made["job_id"] = job_id
    doc["rules"][job_id] = made
    save(cfg, room, doc)
    return dict(made)


def drop_rule(cfg, room, job_id) -> dict | None:
    """Убрать правило (при удалении дежурства оно уезжает в корзину вместе с ним)."""
    doc = load(cfg, room)
    gone = doc["rules"].pop(str(job_id or ""), None)
    save(cfg, room, doc)
    return dict(gone) if isinstance(gone, dict) else None


def public_rule(data: dict | None) -> dict | None:
    """Правило наружу: русские подписи рядом с машинными значениями."""
    if not isinstance(data, dict):
        return None
    out = dict(data)
    out["on_change_title"] = ON_CHANGE_TITLES.get(str(data.get("on_change")), "")
    out["level_title"] = LEVEL_TITLES.get(str(data.get("level")), "")
    out["source_title"] = SOURCE_TITLES.get(str(data.get("source")), "")
    return out


# -------------------------------------------------------------------- история

def history(cfg, room, job_id=None, limit: int = 40) -> list[dict]:
    """История запусков: ``[{run_id, job_id, at, changed, done, found, proposals, created}]``."""
    doc = load(cfg, room)
    rows = [one for one in doc["history"] if isinstance(one, dict)]
    if job_id:
        rows = [one for one in rows if str(one.get("job_id")) == str(job_id)]
    rows.sort(key=lambda one: int(one.get("at") or 0), reverse=True)
    return rows[:max(1, int(limit))]


def add_history(cfg, room, entry: dict) -> dict:
    """Добавить строку истории (одна запись на `run_id`, повтор не дублируется)."""
    doc = load(cfg, room)
    run_id = str(entry.get("run_id") or "")
    if run_id:
        for one in doc["history"]:
            if str(one.get("run_id")) == run_id:
                return one
    row = dict(entry)
    row["at"] = int(row.get("at") or _now())
    doc["history"].append(row)
    save(cfg, room, doc)
    return row


def history_has(cfg, room, run_id) -> bool:
    wanted = str(run_id or "")
    if not wanted:
        return False
    return any(str(one.get("run_id")) == wanted for one in load(cfg, room)["history"])


# --------------------------------------------------------------------- корзина

def trash_add(cfg, room, job_id, item: dict, payload: dict | None = None,
              now=None, rule: dict | None = None) -> dict:
    """Положить удалённое дежурство в корзину на 30 дней (с чем его вернуть)."""
    at = _now(now)
    doc = load(cfg, room)
    doc["trash"] = [one for one in doc["trash"] if str(one.get("job_id")) != str(job_id)]
    row = {"job_id": str(job_id), "at": at, "at_text": _iso(at),
           "delete_after": at + TRASH_DAYS * 86400,
           "delete_after_text": time.strftime("%d.%m.%Y", time.localtime(at + TRASH_DAYS * 86400)),
           "name": _text((item or {}).get("name"), 200),
           "payload": dict(payload or {}),
           "rule": (dict(rule) if isinstance(rule, dict) else
                    doc["rules"].get(str(job_id)) if isinstance(doc["rules"].get(str(job_id)), dict)
                    else None),
           "item": copy.deepcopy(item or {})}
    doc["trash"].append(row)
    doc["rules"].pop(str(job_id), None)
    save(cfg, room, doc)
    return row


def trash(cfg, room, now=None) -> dict:
    """Корзина комнаты: ``{items, kept_days, expired}`` — просроченное не возвращаем."""
    at = _now(now)
    doc = load(cfg, room)
    rows = [one for one in doc["trash"] if isinstance(one, dict)]
    live = [one for one in rows if int(one.get("delete_after") or 0) > at]
    expired = [one for one in rows if int(one.get("delete_after") or 0) <= at]
    if expired:                          # чистим по сроку только при чтении
        doc["trash"] = live
        save(cfg, room, doc)
    out = []
    for one in sorted(live, key=lambda row: int(row.get("at") or 0), reverse=True):
        item = dict(one)
        left = max(0, int(one.get("delete_after") or 0) - at)
        item["days_left"] = int(left // 86400)
        item["restorable"] = True
        out.append(item)
    return {"items": out, "kept_days": TRASH_DAYS, "expired": len(expired), "generated_at": at}


def trash_take(cfg, room, job_id, now=None) -> dict | None:
    """Забрать дежурство из корзины (для возврата); просроченное не отдаём."""
    at = _now(now)
    wanted = str(job_id or "")
    doc = load(cfg, room)
    for one in list(doc["trash"]):
        if str(one.get("job_id")) == wanted:
            if int(one.get("delete_after") or 0) <= at:
                doc["trash"] = [row for row in doc["trash"] if row is not one]
                save(cfg, room, doc)
                raise DutyRuleError("Срок хранения в корзине истёк (30 дней) — вернуть нельзя",
                                    410, "trash_expired", {"job_id": wanted})
            doc["trash"] = [row for row in doc["trash"] if row is not one]
            save(cfg, room, doc)
            return dict(one)
    return None


# ------------------------------------------------------------ разбор результата

def result_block(text: str) -> str:
    """Тело машинного блока `результат` из ответа агента (``""`` — блока нет)."""
    found = _BLOCK_RE.search(str(text or ""))
    return found.group(2).strip() if found else ""


def _flat(value: str) -> str:
    return " ".join(str(value or "").split())


def _yes_no(value) -> bool | None:
    text = _flat(value).lower().replace("ё", "е")
    if text in ("да", "yes", "true", "1", "так"):
        return True
    if text in ("нет", "no", "false", "0", "не"):
        return False
    return None


def _parse_lines(body: str) -> dict:
    """Разбор блока без PyYAML: ключи верхнего уровня, список предложений и ссылок."""
    out: dict = {}
    section = ""
    last_key = ""
    for raw in str(body or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        stripped = line.strip()
        if indent == 0 and ":" in stripped and not stripped.startswith("-"):
            key, _, value = stripped.partition(":")
            name = _KEY_MAP.get(key.strip().lower().replace("ё", "е"))
            if name is None:
                section, last_key = "", ""
                continue
            section, last_key = name, name
            value = value.strip()
            if name == "proposals":
                out[name] = out.get(name) or []
            elif name == "links":
                out[name] = out.get(name) or []
            elif value:
                out[name] = value
            continue
        if not section:
            continue
        item = stripped[1:].strip() if stripped.startswith("-") else stripped
        if section == "proposals":
            out.setdefault("proposals", [])
            if stripped.startswith("-"):
                out["proposals"].append({})
            if not out["proposals"]:
                out["proposals"].append({})
            target = out["proposals"][-1]
            if ":" in item:
                key, _, value = item.partition(":")
                name = _PROPOSAL_KEYS.get(key.strip().lower().replace("ё", "е"))
                if name:
                    target[name] = _flat(value)
                elif value.strip():
                    target.setdefault("what", _flat(item))
            elif item:
                target["what"] = (target.get("what") or _flat(item))
            continue
        if section == "links":
            if item:
                out.setdefault("links", []).append(_flat(item)[:300])
            continue
        if section in ("done", "found", "changed"):
            out[section] = (_flat(out.get(section)) + " " + _flat(item)).strip()
    return out


def parse_result(text: str) -> dict | None:
    """Разобрать блок `результат` → ``{changed, done, found, proposals, links, raw}``.

    Понимает и YAML (если он есть в окружении), и простой разбор строками: блок
    пишут модели, и требовать от них идеального YAML нельзя. Без блока — ``None``.
    """
    body = result_block(text)
    if not body:
        return None
    data: dict = {}
    try:                                  # PyYAML есть не всегда — это необязательный путь
        import yaml                       # type: ignore
        loaded = yaml.safe_load(body)
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                name = _KEY_MAP.get(str(key).strip().lower().replace("ё", "е"))
                if name:
                    data[name] = value
    except Exception:                     # noqa: BLE001 - падаем на простой разбор
        data = {}
    if not data:
        data = _parse_lines(body)
    changed = data.get("changed")
    if isinstance(changed, bool):
        pass
    elif isinstance(changed, str):
        changed = _yes_no(changed)
    else:
        changed = None
    proposals: list[dict] = []
    raw_props = data.get("proposals")
    if isinstance(raw_props, list):
        for one in raw_props:
            if isinstance(one, dict):
                mapped: dict = {}
                for key, value in one.items():
                    name = _PROPOSAL_KEYS.get(str(key).strip().lower().replace("ё", "е"))
                    if name and _flat(value):
                        mapped[name] = _flat(value)
                if mapped:
                    proposals.append(mapped)
            elif _flat(one):
                proposals.append({"what": _flat(one)})
    links: list[str] = []
    raw_links = data.get("links")
    if isinstance(raw_links, list):
        for one in raw_links:
            text_one = _flat(one if not isinstance(one, dict) else
                             (one.get("path") or one.get("путь") or one.get("url") or one))
            if text_one:
                links.append(text_one[:300])
    elif _flat(raw_links):
        links = [_flat(raw_links)[:300]]
    return {"changed": changed, "done": _flat(data.get("done"))[:1000],
            "found": _flat(data.get("found"))[:1000], "proposals": proposals[:12],
            "links": links[:20], "raw": body[:2000]}


# --------------------------------------------------------------- карточки

def _card_text(result: dict, job_name: str) -> str:
    """Текст карточки: «Что выполнено / Что обнаружено / Предлагаю»."""
    lines = ["Дежурство «%s»: есть изменение." % (job_name or "без названия")]
    if result.get("done"):
        lines.append("Что выполнено: %s" % result["done"])
    if result.get("found"):
        lines.append("Что обнаружено: %s" % result["found"])
    if result.get("proposals"):
        lines.append("Предлагаю:")
        for one in result["proposals"][:6]:
            text = one.get("what") or ""
            tail = " — " + one["why"] if one.get("why") else ""
            action = " (действие: %s)" % one["action"] if one.get("action") else ""
            lines.append("• %s%s%s" % (text, tail, action))
    if result.get("links"):
        lines.append("Ссылки: %s" % ", ".join(result["links"][:5]))
    return "\n".join(lines)[:CARD_TEXT_MAX]


def _card_kind(rule: dict | None) -> str:
    choice = str((rule or {}).get("on_change") or "inbox")
    return choice if choice in ("delo", "step", "question") else "delo"


def _card_title(result: dict, job_name: str) -> str:
    base = result.get("done") or result.get("found") or job_name or "Результат дежурства"
    return inbox_mod._title_of(base)


def _put_card(cfg, room, *, job_id, job_name, run_id, result, rule, created_at: int) -> dict:
    """Положить карточку во «Входящие» комнаты (через хранилище `inbox.py`)."""
    room_id = room_id_of(room)
    doc, _info = inbox_mod.load(cfg, room_id)
    title = _card_title(result, job_name)
    kind = _card_kind(rule)
    item = {
        "id": inbox_mod.new_id(),
        "room": room_id,
        "text": _card_text(result, job_name),
        "source": "duty",
        "created_at": inbox_mod._now_iso(),
        "status": "processing",
        "kind": kind,
        "proposal": {
            "id": "pr_" + secrets.token_hex(4),
            "at": inbox_mod._now_iso(),
            "kind": kind,
            "kind_title": inbox_mod.KIND_TITLES.get(kind, ""),
            "title": title,
            "body": _card_text(result, job_name),
            "state": "waiting",
            "by": "дежурство",
            "actions": [dict(one) for one in CARD_ACTIONS],
        },
        "resolution": None,
        "note": "Результат дежурства: примите, измените, скажите «не надо» или "
                "«сделать правилом».",
        "duty": {
            "job_id": str(job_id),
            "job_name": str(job_name or ""),
            "run_id": str(run_id),
            "at": int(created_at),
            "changed": bool(result.get("changed")),
            "done": result.get("done") or "",
            "found": result.get("found") or "",
            "proposals": copy.deepcopy(result.get("proposals") or []),
            "links": list(result.get("links") or []),
            "level": str((rule or {}).get("level") or "propose"),
            "on_change": str((rule or {}).get("on_change") or "inbox"),
            "notify": list((rule or {}).get("notify") or []),
            "actions": [dict(one) for one in CARD_ACTIONS],
            "created": [],
        },
    }
    doc["items"].append(item)
    inbox_mod.save(cfg, room_id, doc)
    return item


def _create_delo(cfg, room, *, title: str, result: dict, job_id, kind: str) -> dict | None:
    """Создать Дело по принятому результату (только внутри комнаты, обратимо)."""
    room_dict = room if isinstance(room, dict) else {"id": room_id_of(room)}
    payload = {
        "title": _flat(title)[:200] or "Дело из дежурства",
        "owner": "manager",
        "stages": [{"title": "Сделать"}, {"title": "Проверить"}]
        if kind != "step" else [{"title": _flat(title)[:120] or "Шаг из дежурства"}],
        "found": [{"what": result.get("found") or result.get("done") or "",
                   "where": (result.get("links") or [""])[0],
                   "source": "дежурство %s" % job_id}],
        "duties": [{"job_id": str(job_id), "title": _flat(title)[:120]}],
    }
    try:
        made = delo_mod.create(cfg, room_dict, payload)
    except Exception:                     # noqa: BLE001 - дело вторично для ответа
        return None
    return made if isinstance(made, dict) else None


def decide_card(cfg, room, item_id, action, payload: dict | None = None,
                now=None) -> dict:
    """Решение по карточке результата: accept | edit | reject | make_rule | delete.

    «Принять» создаёт Дело (шаг) только для обратимой записи внутри комнаты;
    наружу не уходит ничего. «Сделать правилом» переводит правило в `level: self`
    и запоминает, что дальше такие изменения дежурство делает само.
    """
    data = payload if isinstance(payload, dict) else {}
    name = str(action or "").strip().lower()
    if name not in ("accept", "edit", "reject", "make_rule", "delete", "restore"):
        raise DutyRuleError("Неизвестное действие с карточкой: %s" % name, 400, "bad_request",
                            {"allowed": ["accept", "edit", "reject", "make_rule", "delete",
                                         "restore"]})
    room_id = room_id_of(room)
    doc, _info = inbox_mod.load(cfg, room_id)
    item = None
    for one in doc.get("items") or []:
        if str(one.get("id")) == str(item_id):
            item = one
            break
    if item is None or not isinstance(item.get("duty"), dict):
        raise DutyRuleError("Карточка результата не найдена", 404, "not_found",
                            {"item_id": str(item_id)[:60]})
    duty = item["duty"]
    job_id = str(duty.get("job_id") or "")
    result = {"changed": duty.get("changed"), "done": duty.get("done"),
              "found": duty.get("found"), "proposals": duty.get("proposals") or [],
              "links": duty.get("links") or []}
    made = None
    if name == "accept":
        if str(item.get("status")) not in ("new", "processing"):
            raise DutyRuleError("Карточка уже разобрана", 409, "already_decided",
                                {"item_id": item_id, "status": item.get("status")})
        kind = _card_kind({"on_change": duty.get("on_change")})
        if kind in ("delo", "step"):
            made = _create_delo(cfg, room, title=data.get("title") or
                                (item.get("proposal") or {}).get("title") or "",
                                result=result, job_id=job_id, kind=kind)
        item["status"] = "processed"
        item["note"] = "Принято: %s." % ("создано дело" if made else "учтено")
        item["resolution"] = {"action": "accept", "kind": kind, "at": inbox_mod._now_iso(),
                              "delo_id": (made or {}).get("id")}
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        proposal["state"] = "accepted"
        item["proposal"] = proposal
    elif name == "edit":
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        text = str(data.get("text") or data.get("body") or "").strip()
        if not text:
            raise DutyRuleError("Нужен текст — что сделать иначе", 400, "bad_request",
                                {"missing": ["text"]})
        proposal["body"] = text[:CARD_TEXT_MAX]
        if str(data.get("title") or "").strip():
            proposal["title"] = str(data["title"]).strip()[:inbox_mod.TITLE_LIMIT]
        proposal["state"] = "waiting"
        proposal["by"] = "ваша правка"
        item["proposal"] = proposal
        item["note"] = "Изменено — примите или скажите «не надо»."
    elif name == "reject":
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        proposal["state"] = "rejected"
        item["proposal"] = proposal
        item["status"] = "processed"
        item["note"] = str(data.get("text") or "").strip()[:500] or "«Не надо» — учтено."
        item["resolution"] = {"action": "reject", "at": inbox_mod._now_iso()}
    elif name == "make_rule":
        current = rule(cfg, room_id, job_id)
        if current is None and job_id:
            current = set_rule(cfg, room_id, job_id, {"changed_rule": "", "on_change": "inbox",
                                                      "level": "self", "source": "manual"})
        if current is None:
            raise DutyRuleError("Не нашёл правило этого дежурства — откройте дежурство и "
                                "поставьте «сам» вручную", 409, "no_rule", {"job_id": job_id})
        set_rule(cfg, room_id, job_id, {"level": "self"})
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        proposal["state"] = "accepted"
        item["proposal"] = proposal
        item["status"] = "processed"
        item["note"] = ("Сделано правилом: дальше такие изменения дежурство делает само "
                        "(обратимое внутри комнаты).")
        item["resolution"] = {"action": "make_rule", "at": inbox_mod._now_iso(),
                              "level": "self"}
    elif name == "delete":
        item["status"] = "deleted"
        item["deleted_at"] = inbox_mod._now_iso()
    else:                                  # restore
        item["status"] = "processing"
        item["note"] = "Возвращено в работу."
    inbox_mod.save(cfg, room_id, doc)
    listing = inbox_mod.items(cfg, room_id, "active")
    return {"ok": True, "action": name, "item": inbox_mod._public(item),
            "delo": made, "counts": listing["counts"],
            "rule": public_rule(rule(cfg, room_id, job_id)) if job_id else None}


# ------------------------------------------------------- обработка запусков

def bound_jobs(cfg, room) -> list[str]:
    """Дежурства, привязанные к комнате (реестр комнат, поле `duties`)."""
    if isinstance(room, dict):
        return [str(one) for one in (room.get("duties") or []) if str(one or "").strip()]
    room_id = room_id_of(room)
    try:
        for one in rooms_mod.list_rooms(cfg, include_archived=False):
            if str(one.get("id")) == room_id:
                return [str(x) for x in (one.get("duties") or []) if str(x or "").strip()]
    except Exception:                      # noqa: BLE001 - без реестра дежурств нет
        return []
    return []


def _job_name(cfg, job_id) -> str:
    for job in duties_mod.jobs(cfg):
        if str(job.get("id")) == str(job_id):
            return str(job.get("name") or job_id)
    return str(job_id)


def candidates(cfg, room, limit: int = OUTPUT_SCAN) -> list[dict]:
    """Новые (ещё не обработанные) выводы дежурств комнаты, старые сверху."""
    out: list[dict] = []
    seen = {str(one.get("run_id")) for one in load(cfg, room)["history"]}
    for job_id in bound_jobs(cfg, room):
        files = duties_mod.output_files(cfg, job_id)[-max(1, int(limit)):]
        for path in files:
            run_id = "%s:%s" % (job_id, path.name)
            if run_id in seen:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            stamp = duties_mod._file_day(path) or int(duties_mod._mtime(path) or 0)
            out.append({"job_id": job_id, "job_name": _job_name(cfg, job_id),
                        "run_id": run_id, "at": int(stamp or _now()), "path": path,
                        "result": parse_result(text)})
    out.sort(key=lambda one: int(one.get("at") or 0))
    return out


def _perform(cfg, room, cand: dict, rule_now: dict | None, now: int) -> dict:
    """Обработать один запуск: история + (по правилу) карточка или Дело."""
    result = cand.get("result") or {}
    level = str((rule_now or {}).get("level") or "propose")
    on_change = str((rule_now or {}).get("on_change") or "log")
    created: list = []
    entry = {"run_id": cand["run_id"], "job_id": cand["job_id"],
             "job_name": cand.get("job_name") or "", "at": int(cand.get("at") or now),
             "processed_at": now, "changed": result.get("changed"),
             "done": result.get("done") or "", "found": result.get("found") or "",
             "proposals": copy.deepcopy(result.get("proposals") or []),
             "links": list(result.get("links") or []), "created": created,
             "on_change": on_change, "level": level,
             "has_block": bool(result), "silent": False}
    if result.get("changed") is not True:
        entry["silent"] = True          # «нет» или блока нет — тихая строка, не «ждёт вас»
        add_history(cfg, room, entry)
        return {"kind": "silent", "entry": entry}
    if on_change == "log":
        add_history(cfg, room, entry)
        return {"kind": "log", "entry": entry}
    if level == "self" and on_change in ("delo", "step"):
        made = _create_delo(cfg, room, title=result.get("done") or result.get("found") or
                            cand.get("job_name") or "", result=result,
                            job_id=cand["job_id"], kind=on_change)
        if made:
            created.append({"kind": on_change, "id": made.get("id")})
            entry["created"] = created
            add_history(cfg, room, entry)
            return {"kind": "self", "entry": entry, "delo": made}
        # не получилось создать — честно оставляем карточку человеку
    card = _put_card(cfg, room, job_id=cand["job_id"], job_name=cand.get("job_name") or "",
                     run_id=cand["run_id"], result=result, rule=rule_now, created_at=now)
    created.append({"kind": "card", "id": card.get("id")})
    entry["created"] = created
    add_history(cfg, room, entry)
    return {"kind": "card", "entry": entry, "card": card}


def process_runs(cfg, room, now=None, limit: int = MAX_PROCESS_PER_TICK) -> dict:
    """Обработать свежие результаты дежурств комнаты (тик сторожа).

    Лимит ≤ 5 обработок за тик и идемпотентность по `run_id`: повторный вызов
    ничего не удваивает. Ничего не запускает и наружу не пишет.
    """
    at = _now(now)
    cap = max(1, min(int(limit or MAX_PROCESS_PER_TICK), MAX_PROCESS_PER_TICK))
    stats = {"room": room_id_of(room), "at": at, "processed": 0, "cards": 0,
             "self": 0, "silent": 0, "logs": 0, "skipped": 0, "limit": cap}
    try:
        waiting = candidates(cfg, room)
    except Exception:                      # noqa: BLE001 - чтение не валит тик
        waiting = []
    if len(waiting) > cap:
        stats["skipped"] = len(waiting) - cap
        waiting = waiting[:cap]
    for cand in waiting:
        if not cand.get("result"):
            # Блока «результат» нет (старый вывод, скрипт без модели): тихая строка,
            # чтобы не читать тот же файл на следующем тике.
            duty = {"has_block": False, "silent": True, "created": [], "changed": None,
                    "done": "", "found": "", "proposals": [], "links": [],
                    "run_id": cand["run_id"], "job_id": cand["job_id"],
                    "job_name": cand.get("job_name") or "", "at": int(cand.get("at") or at),
                    "processed_at": at, "on_change": "", "level": ""}
            add_history(cfg, room, duty)
            stats["processed"] += 1
            stats["silent"] += 1
            continue
        rule_now = rule(cfg, room, cand["job_id"])
        try:
            done = _perform(cfg, room, cand, rule_now, at)
        except Exception:                  # noqa: BLE001 - один запуск не валит проход
            stats["skipped"] += 1
            continue
        stats["processed"] += 1
        kind = done.get("kind")
        if kind == "card":
            stats["cards"] += 1
        elif kind == "self":
            stats["self"] += 1
        elif kind == "log":
            stats["logs"] += 1
        else:
            stats["silent"] += 1
    return stats


# ------------------------------------------------- предложения руководителя

def proposals_path(cfg, room) -> Path:
    return Path(str(cfg.data_dir)) / "state" / room_id_of(room) / "duty-proposals.json"


def _empty_proposals(room_id: str) -> dict:
    return {"room": room_id, "items": [], "declined": [], "updated_at": None}


def _load_proposals(cfg, room) -> dict:
    room_id = room_id_of(room)
    value, info = statefiles.read_json_doc(proposals_path(cfg, room_id), None)
    if info.get("state") == "corrupt":
        statefiles.quarantine(info.get("path") or "")
        return _empty_proposals(room_id)
    if not isinstance(value, dict):
        return _empty_proposals(room_id)
    out = dict(value)
    out["room"] = str(value.get("room") or room_id)
    out["items"] = [one for one in (value.get("items") or []) if isinstance(one, dict)]
    out["declined"] = [str(one) for one in (value.get("declined") or []) if str(one or "")]
    return out


def _save_proposals(cfg, room, doc: dict) -> dict:
    doc["items"] = [one for one in (doc.get("items") or []) if isinstance(one, dict)][-PROPOSALS_MAX:]
    doc["updated_at"] = _iso(_now())
    path = proposals_path(cfg, room)
    path.parent.mkdir(parents=True, exist_ok=True)
    statefiles.write_json_atomic(path, doc)
    return doc


def _decline_key(title) -> str:
    return _flat(title).lower().replace("ё", "е")[:60]


def _week_count(doc: dict, now: int) -> int:
    """Сколько предложений руководителя за последние 7 дней (лимит — 2)."""
    left = now - 7 * 86400
    return sum(1 for one in doc["items"]
               if str(one.get("source")) == "manager"
               and str(one.get("state")) in ("proposed", "accepted", "edited")
               and int(one.get("at") or 0) >= left)


def propose(cfg, room, payload: dict, now=None) -> dict:
    """Черновик-предложение дежурства (руководитель, диктовка, из Дела).

    Не создаёт крон: это карточка «Предлагаю дежурство: …» с кнопками. Предложение
    руководителя ограничено двумя в неделю; «не предлагать такое» запоминается и
    такие названия больше не предлагаются.
    """
    at = _now(now)
    data = payload if isinstance(payload, dict) else {}
    title = _flat(data.get("title"))[:200]
    if not title:
        raise DutyRuleError("Нужно название дежурства", 400, "bad_request",
                            {"missing": ["title"]})
    source = str(data.get("source") or "manager").strip().lower()
    if source not in SOURCES:
        source = "manager"
    doc = _load_proposals(cfg, room)
    key = _decline_key(title)
    if key and any(key in str(one) for one in doc["declined"]):
        return {"ok": True, "skipped": True, "reason": "declined",
                "text": "Такое дежурство вы просили не предлагать — пропущено.",
                "proposal": None}
    if source == "manager" and _week_count(doc, at) >= MAX_PROPOSALS_WEEK:
        raise DutyRuleError("Предложений уже %d за неделю — больше не предлагаю. "
                            "Создайте дежурство вручную, если нужно."
                            % MAX_PROPOSALS_WEEK, 409, "too_many_proposals",
                            {"limit": MAX_PROPOSALS_WEEK})
    wizard = {
        "title": title,
        "kind": str(data.get("kind") or ("monitor" if data.get("monitor_url")
                                         or data.get("monitor_script") else "model")),
        "schedule": str(data.get("schedule") or data.get("when") or "0 9 * * 1-5"),
        "prompt": _flat(data.get("prompt") or data.get("what"))[:2000],
        "changed_rule": _text(data.get("changed_rule") or data.get("changed")),
        "on_change": str(data.get("on_change") or "inbox"),
        "level": str(data.get("level") or "propose"),
        "notify": list(data.get("notify") or ["today"]),
    }
    if wizard["on_change"] not in ON_CHANGE:
        wizard["on_change"] = "inbox"
    if wizard["level"] not in LEVELS:
        wizard["level"] = "propose"
    proposal = {
        "id": "dpr_" + secrets.token_hex(4),
        "room": room_id_of(room),
        "at": at,
        "at_text": _iso(at),
        "title": title,
        "why": _flat(data.get("why"))[:600],
        "when": duties_mod.schedule_human(wizard["schedule"]),
        "what": wizard["prompt"],
        "state": "proposed",
        "source": source,
        "source_title": SOURCE_TITLES.get(source, ""),
        "template": _text(data.get("template"), 40),
        "wizard": wizard,
        "decline_key": key,
        "expires_at": at + PROPOSAL_TTL_DAYS * 86400,
        "actions": [{"id": "create", "label": "Создать"}, {"id": "edit", "label": "Изменить"},
                    {"id": "dismiss", "label": "Не надо"},
                    {"id": "never", "label": "Не предлагать такое"}],
    }
    doc["items"].append(proposal)
    _save_proposals(cfg, room, doc)
    return {"ok": True, "skipped": False, "proposal": _public_proposal(proposal),
            "text": "Предложение дежурства сохранено: создаётся только по вашему «Создать»."}


def _public_proposal(one: dict) -> dict:
    out = dict(one)
    out["actions"] = [dict(action) for action in one.get("actions") or []]
    return out


def proposals(cfg, room, state: str = "proposed", now=None) -> dict:
    """Карточки «Предлагаю дежурство» (по умолчанию — ждущие решения)."""
    at = _now(now)
    doc = _load_proposals(cfg, room)
    wanted = str(state or "proposed")
    rows = [one for one in doc["items"]
            if wanted == "all" or str(one.get("state")) == wanted]
    for one in rows:
        one["expired"] = int(one.get("expires_at") or 0) <= at
    rows.sort(key=lambda one: int(one.get("at") or 0), reverse=True)
    return {"room": room_id_of(room), "generated_at": at, "filter": wanted,
            "week_used": _week_count(doc, at), "week_limit": MAX_PROPOSALS_WEEK,
            "declined": list(doc["declined"]),
            "items": [_public_proposal(one) for one in rows]}


def act_proposal(cfg, room, proposal_id, action, payload: dict | None = None,
                 now=None) -> dict:
    """Решение по предложению: create (отдаёт черновик мастера) | edit | dismiss | never."""
    at = _now(now)
    name = str(action or "").strip().lower()
    if name not in ("create", "edit", "dismiss", "never"):
        raise DutyRuleError("Неизвестное действие с предложением: %s" % name, 400,
                            "bad_request",
                            {"allowed": ["create", "edit", "dismiss", "never"]})
    data = payload if isinstance(payload, dict) else {}
    doc = _load_proposals(cfg, room)
    found = None
    for one in doc["items"]:
        if str(one.get("id")) == str(proposal_id):
            found = one
            break
    if found is None:
        raise DutyRuleError("Предложение не найдено", 404, "not_found",
                            {"proposal_id": str(proposal_id)[:60]})
    if name == "edit":
        wizard = found.get("wizard") if isinstance(found.get("wizard"), dict) else {}
        for key in ("title", "schedule", "prompt", "changed_rule"):
            if str(data.get(key) or "").strip():
                wizard[key] = _flat(data[key])[:2000]
        for key in ("on_change", "level", "kind"):
            if str(data.get(key) or "").strip():
                wizard[key] = str(data[key]).strip().lower()
        if isinstance(data.get("notify"), list):
            wizard["notify"] = [str(one)[:24] for one in data["notify"]][:6]
        found["wizard"] = wizard
        found["title"] = wizard.get("title") or found.get("title")
        found["what"] = wizard.get("prompt") or found.get("what")
        found["when"] = duties_mod.schedule_human(wizard.get("schedule"))
        found["state"] = "edited"
        found["edited_at"] = at
        _save_proposals(cfg, room, doc)
        return {"ok": True, "action": name, "proposal": _public_proposal(found),
                "draft": wizard,
                "text": "Изменено. Дальше — «Создать»: крон появится только после этого."}
    if name in ("dismiss", "never"):
        found["state"] = "dismissed"
        found["decided_at"] = at
        note = "«Не надо» — предложение закрыто."
        if name == "never":
            key = found.get("decline_key") or _decline_key(found.get("title"))
            if key and key not in doc["declined"]:
                doc["declined"].append(key)
            note = "Больше такое не предлагаю (запомнил)."
        _save_proposals(cfg, room, doc)
        return {"ok": True, "action": name, "proposal": _public_proposal(found), "text": note,
                "declined": list(doc["declined"])}
    # create: отдаём готовый черновик мастера — крон создаёт POST /api/rooms/{id}/duties
    found["state"] = "accepted"
    found["decided_at"] = at
    _save_proposals(cfg, room, doc)
    wizard = dict(found.get("wizard") or {})
    wizard.setdefault("source", found.get("source") or "manager")
    if found.get("template"):
        wizard.setdefault("template", found.get("template"))
    return {"ok": True, "action": name, "proposal": _public_proposal(found),
            "draft": wizard,
            "text": "Готово к созданию: проверьте команду и подтвердите — крон появится "
                    "только после «Создать» в мастере.",
            "next": "POST /api/rooms/%s/duties с полями draft, preview:false, confirm:true"
                    % room_id_of(room)}


# ----------------------------------------------- диктовка и «сделать регулярным»

def sync_inbox_drafts(cfg, room, now=None) -> dict:
    """Принять заготовки дежурств из «Входящих» (D9 `to_duty`) как предложения.

    Диктовка не создаёт крон: она даёт черновик-предложение «Предлагаю оформить
    так…». Повторный проход не дублирует (сверка по `draft_id`).
    """
    at = _now(now)
    room_id = room_id_of(room)
    try:
        doc, _info = inbox_mod.load(cfg, room_id)
    except inbox_mod.InboxError:
        return {"room": room_id, "made": 0, "skipped": 0, "at": at}
    drafts = [one for one in (doc.get("duties_drafts") or []) if isinstance(one, dict)]
    store = _load_proposals(cfg, room_id)
    known = {str(one.get("draft_id")) for one in store["items"] if one.get("draft_id")}
    made = 0
    skipped = 0
    for draft in drafts:
        draft_id = str(draft.get("id") or "")
        if not draft_id or draft_id in known:
            skipped += 1
            continue
        text = _flat(draft.get("text") or draft.get("title"))
        if not text:
            skipped += 1
            continue
        try:
            out = propose(cfg, room_id, {"title": draft.get("title") or text[:120],
                                         "what": text, "why": "Надиктовано в «Входящих».",
                                         "source": "dictation", "on_change": "inbox",
                                         "level": "propose", "schedule": "0 9 * * 1-5"}, at)
        except DutyRuleError:
            skipped += 1
            continue
        if out.get("skipped"):
            skipped += 1
            continue
        pid = (out.get("proposal") or {}).get("id")
        store = _load_proposals(cfg, room_id)
        for one in store["items"]:
            if str(one.get("id")) == str(pid):
                one["draft_id"] = draft_id
                one["source"] = "dictation"
        _save_proposals(cfg, room_id, store)
        made += 1
    return {"room": room_id, "made": made, "skipped": skipped, "at": at}


def from_delo(cfg, room, delo_id, now=None) -> dict:
    """«Сделать регулярным» из Дела: черновик-предложение с теми же параметрами."""
    at = _now(now)
    try:
        found = delo_mod.get(cfg, delo_id)
    except Exception as exc:               # noqa: BLE001 - дела может не быть
        raise DutyRuleError("Дело не найдено: %s" % (str(delo_id)[:60]), 404, "not_found",
                            {"delo_id": str(delo_id)[:60]}) from exc
    if not isinstance(found, dict):
        raise DutyRuleError("Дело не найдено", 404, "not_found", {"delo_id": str(delo_id)[:60]})
    title = _flat(found.get("title"))[:200] or "Дежурство по делу"
    return propose(cfg, room, {
        "title": title,
        "what": "Повторять то, что делали по делу «%s»: проверь, что изменилось, "
                "и назови это коротко." % title,
        "why": "Дело уже делали руками — дальше это можно проверять по расписанию.",
        "source": "delo", "on_change": "inbox", "level": "propose",
        "schedule": "0 9 * * 1-5", "changed_rule": "Появилось то, чего не было в прошлый раз.",
    }, at)


__all__ = ["CARD_ACTIONS", "CHANGED_RULE_MAX", "DutyRuleError", "HISTORY_MAX", "LEVELS",
           "LEVEL_TITLES", "MAX_PROCESS_PER_TICK", "MAX_PROPOSALS_WEEK", "ON_CHANGE",
           "ON_CHANGE_TITLES", "OUTPUT_SCAN", "PROPOSALS_MAX", "PROPOSAL_TTL_DAYS", "SOURCES",
           "SOURCE_TITLES", "TRASH_DAYS", "act_proposal", "add_history", "bound_jobs",
           "candidates", "check_rule", "decide_card", "drop_rule", "from_delo", "history",
           "history_has", "load", "parse_result", "process_runs", "proposals", "proposals_path",
           "propose", "public_rule", "result_block", "room_id_of", "rule", "save", "set_rule",
           "store_path", "sync_inbox_drafts", "trash", "trash_add", "trash_take"]
