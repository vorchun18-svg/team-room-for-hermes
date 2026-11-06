"""Пост-синтезный цикл критика (ARCH §12.4): чистый plan() + исполнение run_step().

Цикл НЕ перепаивает граф движка: критик и доработка — обычные карточки-дети
корня, их id приложение хранит само в реестре запросов (`extra_cards`).

* `plan(room, record, detail, now)` — что нужно сделать прямо сейчас; чистая
  функция, вызывается на каждом тике watcher-а, поэтому идемпотентна: решения
  принимаются только по `record.extra_cards` / `record.retargeted` и статусам
  карточек из `detail.tree`;
* `run_step(cfg, room, record, detail)` — исполняет план через `engine_write` и
  дописывает результат в реестр через `registry` (если модуль уже есть).

Вердикт критика читается из metadata завершённого run'а карточки-критика
(`{"verdict": "approve|revise", "issues": [...]}`), фолбэк — `detail.critic`.
"""
from __future__ import annotations

import importlib
import re
import time

try:  # пакетный и «плоский» импорт (python -m unittest из корня проекта)
    from . import contract as _contract
    from . import engine_write as _engine
except ImportError:  # pragma: no cover
    import contract as _contract  # type: ignore
    import engine_write as _engine  # type: ignore

DEFAULT_MAX_ROUNDS = 1
MAX_ROUNDS_LIMIT = 2
#: Режим «до полного одобрения критиком» (W13-E §9): цикл идёт, пока verdict=approve.
MODE_ROUNDS = "rounds"
MODE_UNTIL_APPROVED = "until_approved"
CRITIC_MODES = (MODE_ROUNDS, MODE_UNTIL_APPROVED)
DEFAULT_ROUND_CAP = 12          # мягкий потолок раундов режима «до одобрения»
MIN_ROUND_CAP = 2
MAX_ROUND_CAP = 30
STALL_ROUNDS = 3                # столько одинаковых наборов замечаний подряд = стоп
RATE_LIMIT_PAUSE = 600          # первая пауза при лимите провайдера — 10 минут
RATE_LIMIT_PAUSE_MAX = 3600     # дальше ждём не дольше часа
RATE_LIMIT_HINTS = ("429", "rate limit", "ratelimit", "usage limit", "usage_limit",
                    "too many requests", "quota", "лимит запросов", "превышен лимит")
_RATE_LIMIT_RE = re.compile("|".join(re.escape(h) for h in RATE_LIMIT_HINTS), re.IGNORECASE)

FAILED_STATUSES = ("failed", "crashed")
OPEN_STATUSES = ("triage", "todo", "ready", "scheduled", "blocked", "review", "running")

# класс ошибки берём у настоящего engine_write один раз: тесты подменяют `_engine`,
# но ловить исключение нужно всегда одинаково
EngineWriteError = _engine.EngineWriteError

ACTIVE_ROOT_STATUSES = ("triage", "todo", "ready", "scheduled", "blocked", "review")

# ---------------------------------------------------------------- реестр

_REG = None


def set_registry(module) -> None:
    """Подменить модуль реестра (тесты; в бою — server/registry.py)."""
    global _REG
    _REG = module


def _registry():
    global _REG
    if _REG is not None:
        return _REG
    for name in ("server.registry", "registry"):
        try:
            _REG = importlib.import_module(name)
            return _REG
        except Exception:
            continue
    return None


def _registry_update(cfg, room_id: str, root_id: str, patch: dict) -> bool:
    reg = _registry()
    fn = getattr(reg, "update", None) if reg is not None else None
    if not callable(fn):
        return False
    try:
        fn(cfg, room_id, root_id, patch)
        return True
    except Exception:
        return False


def _registry_add_extra_card(cfg, room_id: str, root_id: str, card: dict) -> bool:
    reg = _registry()
    fn = getattr(reg, "add_extra_card", None) if reg is not None else None
    if not callable(fn):
        return False
    try:
        fn(cfg, room_id, root_id, card)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- helpers

def _node(detail: dict, task_id: str) -> dict | None:
    for node in (detail or {}).get("tree") or []:
        if isinstance(node, dict) and node.get("id") == task_id:
            return node
    return None


def _root_node(detail: dict, record: dict) -> dict:
    detail = detail or {}
    record = record or {}
    tree = [n for n in detail.get("tree") or [] if isinstance(n, dict)]
    root_id = str(record.get("root_id") or "")
    for node in tree:
        if root_id and node.get("id") == root_id:
            return node
    for node in tree:
        if node.get("role") == "root":
            return node
    return tree[0] if tree else {}


def _synthesis(detail: dict) -> dict:
    value = (detail or {}).get("synthesis")
    return value if isinstance(value, dict) else {}


def _synthesis_ready(detail: dict) -> bool:
    synthesis = _synthesis(detail)
    return synthesis.get("state") == "ready" and bool(str(synthesis.get("text_md") or "").strip())


def _extra_cards(record: dict, detail: dict) -> list[dict]:
    """Карточки цикла: реестр + (если движок уже знает) critic.task_id."""
    cards = [dict(c) for c in (record or {}).get("extra_cards") or [] if isinstance(c, dict)]
    critic_info = (detail or {}).get("critic")
    if isinstance(critic_info, dict):
        task_id = str(critic_info.get("task_id") or "")
        if task_id and not any(str(c.get("id")) == task_id for c in cards):
            cards.append({"id": task_id, "kind": "critic",
                          "round": int(critic_info.get("round") or 1), "at": 0, "source": "detail"})
    for card in cards:
        card["kind"] = str(card.get("kind") or "")
        try:
            card["round"] = int(card.get("round") or 0)
        except (TypeError, ValueError):
            card["round"] = 0
        try:
            card["at"] = int(card.get("at") or 0)
        except (TypeError, ValueError):
            card["at"] = 0
    return cards


def _max_rounds(record: dict, room: dict, detail: dict | None = None) -> int:
    """Сколько раундов критика разрешено: реестр → detail.critic → комната (1..2)."""
    info = (detail or {}).get("critic") if isinstance((detail or {}).get("critic"), dict) else {}
    raw = (record or {}).get("max_rounds")
    if raw is None:
        raw = (info or {}).get("max_rounds")
    if raw is None:
        raw = (room or {}).get("max_rounds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_ROUNDS
    return max(1, min(MAX_ROUNDS_LIMIT, value))


def _critic_enabled(room: dict, record: dict, detail: dict) -> bool:
    """Флаг «с критиком»: реестр → detail → комната."""
    record = record or {}
    if "critic" in record and record.get("critic") is not None:
        return bool(record.get("critic"))
    info = (detail or {}).get("critic")
    if isinstance(info, bool):
        return info
    if isinstance(info, dict) and "enabled" in info:
        return bool(info.get("enabled"))
    return bool((room or {}).get("critic_pass", False))


def _decomposed(detail: dict, root: dict) -> bool:
    if len([n for n in (detail or {}).get("tree") or [] if isinstance(n, dict)]) > 1:
        return True
    for event in (detail or {}).get("timeline") or []:
        if isinstance(event, dict) and event.get("kind") == "decomposed":
            return True
    return bool(root.get("parent_ids"))


def _mode(record: dict, room: dict, detail: dict | None = None) -> str:
    """Режим критика: реестр → detail.critic → комната. Неизвестное — «rounds»."""
    info = (detail or {}).get("critic") if isinstance((detail or {}).get("critic"), dict) else {}
    raw = (record or {}).get("critic_mode")
    if not raw:
        raw = (info or {}).get("mode")
    if not raw:
        raw = (room or {}).get("critic_mode")
    mode = str(raw or "").strip()
    return mode if mode in CRITIC_MODES else MODE_ROUNDS


def _cap(record: dict, room: dict) -> int:
    """Мягкий потолок раундов режима «до одобрения» (2..30, по умолчанию 12)."""
    raw = (record or {}).get("critic_round_cap")
    if raw is None:
        raw = (room or {}).get("critic_round_cap")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_ROUND_CAP
    if isinstance(raw, bool):
        value = DEFAULT_ROUND_CAP
    return max(MIN_ROUND_CAP, min(MAX_ROUND_CAP, value))


def _issue_key(item) -> str:
    """Устойчивый ключ замечания: место + суть, без регистра и лишних пробелов."""
    if isinstance(item, dict):
        where = " ".join(str(item.get("where") or "").split()).lower()
        problem = " ".join(str(item.get("problem") or "").split()).lower()
        text = " · ".join([p for p in (where, problem) if p]) or " ".join(
            str(v).split()).lower()[:160]
    else:
        text = " ".join(str(item or "").split()).lower()
    return text[:200]


def _issue_keys(issues) -> list[str]:
    """Ключи замечаний без повторов, порядок сохраняется."""
    out: list[str] = []
    for item in issues or []:
        key = _issue_key(item)
        if key and key not in out:
            out.append(key)
    return out


def _round_history(record: dict) -> list[dict]:
    """История раундов из реестра: [{round, verdict, issues: [ключи], at}] по возрастанию."""
    raw = (record or {}).get("critic_rounds")
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("round") or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        issues = item.get("issues") if isinstance(item.get("issues"), list) else []
        out.append({"round": number, "verdict": str(item.get("verdict") or ""),
                    "issues": [str(k) for k in issues if str(k or "").strip()],
                    "at": int(item.get("at") or 0)})
    out.sort(key=lambda item: item["round"])
    return out


def history_from_detail(detail: dict, record: dict) -> list[dict]:
    """История раундов из дерева карточек (реестр может отставать на тик).

    Сливается с `record.critic_rounds` по номеру раунда: свежие данные из дерева
    перекрывают записанные ранее.
    """
    detail = detail or {}
    record = record or {}
    critic_info = detail.get("critic") if isinstance(detail.get("critic"), dict) else None
    merged = {item["round"]: dict(item) for item in _round_history(record)}
    for card in _extra_cards(record, detail):
        if card["kind"] != "critic":
            continue
        node = _node(detail, str(card.get("id")))
        if node is None:
            if (critic_info and str(critic_info.get("task_id") or "") == str(card.get("id"))
                    and critic_info.get("verdict")):
                node = {"id": str(card.get("id")), "status": "done", "metadata": {},
                        "summary_md": ""}
            else:
                continue
        if str(node.get("status") or "") != "done":
            continue
        verdict = _verdict(node, critic_info)
        if not verdict:
            continue
        merged[card["round"]] = {"round": card["round"], "verdict": verdict,
                                 "issues": _issue_keys(_issues(node, critic_info)),
                                 "at": int(card.get("at") or 0)}
    if not merged:
        return []
    return [merged[key] for key in sorted(merged)]


def approved(record: dict, detail: dict) -> bool:
    """Последний вердикт критика — «одобрено» (по истории раундов)."""
    history = _round_history(record)
    if history:
        return history[-1].get("verdict") == "approve"
    info = (detail or {}).get("critic") if isinstance((detail or {}).get("critic"), dict) else {}
    return str(info.get("verdict") or "") == "approve"


def delivery_hold(room: dict, record: dict, detail: dict) -> bool:
    """Ответ нельзя отдавать как готовый (режим «до одобрения», одобрения ещё нет).

    Watcher спрашивает это перед ingest: в режиме «до полного одобрения» версия
    ответа не публикуется, пока критик не сказал approve или цикл не остановлен
    страховкой (тогда ответ выдаётся с отчётом об остановке).
    """
    room, record, detail = room or {}, record or {}, detail or {}
    if _mode(record, room, detail) != MODE_UNTIL_APPROVED:
        return False
    if not _critic_enabled(room, record, detail):
        return False
    if record.get("critic_stop"):
        return False
    if not _synthesis_ready(detail):
        return False
    if approved(record, detail):
        return False
    return True


def progress(record: dict, detail: dict) -> dict:
    """Ход цикла: «раунд N, замечаний было K, закрыто M, осталось R» (W13-E §9д)."""
    history = _round_history(record) or history_from_detail(detail, record)
    stop = (record or {}).get("critic_stop")
    if not history:
        return {"round": 0, "rounds": 0, "issues_before": 0, "closed": 0, "open": 0,
                "closed_items": [], "open_items": [], "verdict": "", "stop": stop}
    first = history[0]
    last = history[-1]
    before = list(first.get("issues") or [])
    still_open = list(last.get("issues") or [])
    closed = [key for key in before if key not in still_open]
    return {"round": int(last.get("round") or 0), "rounds": len(history),
            "issues_before": len(before), "closed": len(closed), "open": len(still_open),
            "closed_items": closed, "open_items": still_open,
            "verdict": str(last.get("verdict") or ""), "stop": stop}


def _failed(node: dict | None) -> bool:
    return bool(node) and str(node.get("status") or "") in FAILED_STATUSES


def _rate_limited(node: dict | None) -> bool:
    """Ошибка карточки похожа на лимит провайдера (429 / usage limit)."""
    if not node:
        return False
    error = node.get("error")
    text = ""
    if isinstance(error, dict):
        text = " ".join(str(error.get(k) or "") for k in ("code", "message"))
    elif error:
        text = str(error)
    return bool(_RATE_LIMIT_RE.search(text))


def _stalled(record: dict, round_no: int, repeats: int = STALL_ROUNDS) -> dict | None:
    """Зацикливание: один и тот же набор замечаний `repeats` раундов подряд.

    Возвращает описание остановки (для журнала и баннера) или None.
    """
    history = [item for item in _round_history(record) if int(item.get("round") or 0) <= round_no]
    if len(history) < repeats:
        return None
    tail = history[-repeats:]
    keys = [tuple(sorted(set(item.get("issues") or []))) for item in tail]
    if any(not item for item in keys):
        return None                       # пустые наборы — это не спор, а другое состояние
    if len(set(keys)) != 1:
        return None
    counts = [len(item.get("issues") or []) for item in tail]
    if counts[-1] < counts[0]:
        return None                       # замечаний стало меньше — прогресс есть
    return {"kind": "no_progress", "round": int(round_no), "repeats": repeats,
            "issues": list(tail[-1].get("issues") or []),
            "message": "Критик повторяет одни и те же замечания %d раунда подряд — "
                       "продолжать спор бессмысленно." % repeats}


def _verdict(node: dict | None, critic_info) -> str:
    """Вердикт критика: metadata.verdict → detail.critic.verdict → текст summary."""
    if not node:
        return ""
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    verdict = str(metadata.get("verdict") or "").strip().lower()
    if not verdict and isinstance(critic_info, dict) and critic_info.get("task_id") == node.get("id"):
        verdict = str(critic_info.get("verdict") or "").strip().lower()
    if not verdict:
        text = str(node.get("summary_md") or "").lower()
        if "revise" in text:
            verdict = "revise"
        elif "approve" in text:
            verdict = "approve"
    if verdict.startswith("approve"):
        return "approve"
    if verdict.startswith("revise"):
        return "revise"
    return ""


def _issues(node: dict | None, critic_info) -> list:
    if node and isinstance(node.get("metadata"), dict) and node["metadata"].get("issues"):
        issues = node["metadata"]["issues"]
        if isinstance(issues, list):
            return issues
    if isinstance(critic_info, dict) and isinstance(critic_info.get("issues"), list):
        return critic_info["issues"]
    return []


def _latest_answer(detail: dict, record: dict) -> str:
    """Последняя завершённая версия ответа: доработка (если есть) → synthesis корня."""
    cards = [c for c in _extra_cards(record, detail) if c["kind"] == "revision"]
    for card in sorted(cards, key=lambda c: (c["round"], c["at"]), reverse=True):
        node = _node(detail, str(card.get("id")))
        if node and node.get("status") == "done" and str(node.get("summary_md") or "").strip():
            return str(node["summary_md"])
    return str(_synthesis(detail).get("text_md") or "")


def _request_text(record: dict, detail: dict) -> str:
    return str((record or {}).get("text") or (detail or {}).get("text") or "")


# ---------------------------------------------------------------- план

def plan(room: dict, record: dict, detail: dict, now: int) -> list[dict]:
    """Что сделать сейчас. Пустой список — цикл ждёт или завершён."""
    room = room or {}
    record = record or {}
    detail = detail or {}
    root = _root_node(detail, record)
    root_id = str(record.get("root_id") or root.get("id") or "")
    if not root_id:
        return []
    root_status = str(root.get("status") or "")
    actions: list[dict] = []
    if record.get("adopted"):
        return actions  # принятая чужая карточка: доска не трогается, пока не нажали вручную

    # 1) директор комнаты: assign корня один раз (после decompose)
    orchestrator = str(room.get("orchestrator") or "").strip()
    if (orchestrator and not record.get("retargeted") and root_status in ACTIVE_ROOT_STATUSES
            and str(root.get("assignee") or "") != orchestrator and _decomposed(detail, root)):
        actions.append({"kind": "retarget_root", "root_id": root_id, "profile": orchestrator,
                        "reason": "директор комнаты"})

    # 2) критик — только после готового синтеза
    if not _critic_enabled(room, record, detail):
        return actions
    if root_status != "done" or not _synthesis_ready(detail):
        return actions
    critic = str(room.get("critic") or "").strip()
    mode = _mode(record, room, detail)
    max_rounds = _max_rounds(record, room, detail)
    cap = _cap(record, room)
    cards = _extra_cards(record, detail)
    critics = sorted([c for c in cards if c["kind"] == "critic"], key=lambda c: (c["round"], c["at"]))
    revisions = [c for c in cards if c["kind"] == "revision"]
    critic_info = detail.get("critic") if isinstance(detail.get("critic"), dict) else None

    if not critic:
        # критик не назначен — проверять некому, отдаём ответ в ingest
        if not record.get("ingest_pending"):
            actions.append({"kind": "mark_ingest", "root_id": root_id, "reason": "критик не назначен"})
        return actions

    if record.get("critic_stop"):
        return actions  # цикл остановлен страховкой и ждёт решения Михаила

    if not critics:
        actions.append({"kind": "create_critic", "root_id": root_id, "parent_id": root_id,
                        "assignee": critic, "round": 1, "reason": "проверка ответа",
                        "mode": mode})
        return actions

    latest = critics[-1]
    node = _node(detail, str(latest.get("id")))
    if node is None and critic_info and str(critic_info.get("task_id") or "") == str(latest.get("id"))             and critic_info.get("verdict"):
        # движок ещё не отдал карточку в дереве, но вердикт уже известен из detail.critic
        node = {"id": str(latest.get("id")), "status": "done", "metadata": {}, "summary_md": ""}
    if _failed(node):
        # упавшая карточка критика: лимит провайдера — пауза и повтор (страховка «б»),
        # любая другая ошибка — ждём, как раньше (карточку перезапустят вручную)
        return actions + _rate_limit_step(record, room, latest, node, now, "critic")
    if not node or str(node.get("status") or "") != "done":
        return actions  # критик ещё работает
    verdict = _verdict(node, critic_info)
    if verdict == "approve":
        if record.get("critic_pause") or record.get("critic_stop"):
            actions.append({"kind": "clear_critic_state", "root_id": root_id,
                            "reason": "критик одобрил ответ"})
        return actions  # ответ принят, делать нечего
    if verdict != "revise":
        return actions  # вердикта нет/он непонятен — не плодим карточки

    # revise: доработка на каждый раунд — ровно одна
    same_round = [r for r in revisions if r["round"] == latest["round"]]
    if not same_round:
        if not orchestrator:
            actions.append({"kind": "mark_ingest", "root_id": root_id,
                            "reason": "в комнате нет директора для доработки"})
            return actions
        actions.append({"kind": "create_revision", "root_id": root_id,
                        "parent_id": str(latest.get("id")), "assignee": orchestrator,
                        "round": latest["round"], "issues": _issues(node, critic_info),
                        "reason": "критик просит доработку"})
        return actions

    revision = sorted(same_round, key=lambda c: c["at"])[-1]
    rev_node = _node(detail, str(revision.get("id")))
    if _failed(rev_node):
        return actions + _rate_limit_step(record, room, revision, rev_node, now, "revision")
    if not rev_node or str(rev_node.get("status") or "") != "done":
        return actions  # доработка ещё идёт

    if mode == MODE_UNTIL_APPROVED:
        # (а) нет прогресса: те же замечания N раундов подряд → стоп и вопрос Михаилу
        stalled = _stalled(record, latest["round"])
        if stalled:
            actions.append({"kind": "stop_no_progress", "root_id": root_id, "stop": stalled,
                            "reason": stalled["message"]})
            return actions
        # (в) мягкий потолок: не отдаём «как есть» тихо — останавливаем с отчётом
        if latest["round"] >= cap:
            actions.append({"kind": "stop_cap", "root_id": root_id,
                            "stop": {"kind": "cap", "round": latest["round"], "cap": cap,
                                     "issues": _issue_keys(_issues(node, critic_info)),
                                     "message": "Достигнут потолок %d раундов — цикл "
                                                "остановлен, ответ показан с замечаниями." % cap},
                            "reason": "потолок раундов"})
            return actions
        actions.append({"kind": "create_critic", "root_id": root_id, "parent_id": root_id,
                        "assignee": critic, "round": latest["round"] + 1, "mode": mode,
                        "reason": "проверка до полного одобрения"})
    elif latest["round"] < max_rounds:
        actions.append({"kind": "create_critic", "root_id": root_id, "parent_id": root_id,
                        "assignee": critic, "round": latest["round"] + 1,
                        "reason": "повторная проверка после доработки"})
    elif not record.get("ingest_pending"):
        actions.append({"kind": "mark_ingest", "root_id": root_id,
                        "reason": "раунды критика исчерпаны"})
    return actions


def _rate_limit_step(record: dict, room: dict, card: dict, node: dict, now: int,
                     kind: str) -> list[dict]:
    """Пауза и повтор упавшей карточки цикла, если причина — лимит провайдера.

    Первое падение — пауза (10 минут, дальше удваиваем до часа), затем повтор той же
    карточки с новым ключом идемпотентности. Не лимит — пусто (обычное ожидание).
    """
    if not _rate_limited(node):
        return []
    pause = record.get("critic_pause") if isinstance(record.get("critic_pause"), dict) else {}
    same = pause and str(pause.get("round") or "") == str(card.get("round")) \
        and str(pause.get("kind") or kind) == kind
    if not same:
        attempt = 0
        delay = RATE_LIMIT_PAUSE
        return [{"kind": "pause_rate_limit", "root_id": str((record or {}).get("root_id") or ""),
                 "card_kind": kind, "round": int(card.get("round") or 0),
                 "until": int(now) + delay, "attempt": attempt,
                 "reason": "лимит провайдера: ждём и продолжаем"}]
    until = int(pause.get("until") or 0)
    if int(now) < until:
        return []                       # пауза ещё идёт: не плодим карточки
    attempt = int(pause.get("attempt") or 0) + 1
    delay = min(RATE_LIMIT_PAUSE * (2 ** attempt), RATE_LIMIT_PAUSE_MAX)
    return [{"kind": "retry_card", "root_id": str((record or {}).get("root_id") or ""),
             "card_kind": kind, "round": int(card.get("round") or 0), "attempt": attempt,
             "next_pause": delay, "reason": "повтор после лимита провайдера"}]


# ---------------------------------------------------------------- исполнение

def _create_card(cfg, room: dict, record: dict, detail: dict, kind: str, round_no: int,
                 user_note=None, parent_id=None, issues=None, attempt: int = 0) -> dict:
    """Создать карточку критика/доработки и записать её в реестр. Общий путь для всех."""
    room = room or {}
    root = _root_node(detail, record)
    root_id = str((record or {}).get("root_id") or root.get("id") or "")
    board = str(room.get("board") or "")
    room_id = str(room.get("id") or "")
    if not root_id or not board:
        raise EngineWriteError("bad_request", "Не хватает комнаты или корневой карточки для цикла критика.")
    request_text = _request_text(record, detail)
    answer = _latest_answer(detail, record)
    parent = str(parent_id or (root_id if kind == "critic" else "")) or root_id
    if kind == "critic":
        assignee = str(room.get("critic") or "").strip()
        if not assignee:
            raise EngineWriteError("conflict", "В комнате не выбран критик.")
        body = _contract.build_critic_body(room, request_text, answer, cfg)
        title = f"Критика ответа (раунд {round_no})"
    elif kind == "revision":
        assignee = str(room.get("orchestrator") or "").strip()
        if not assignee:
            raise EngineWriteError("conflict", "В комнате не выбран директор для доработки.")
        if issues is None:
            critic_info = detail.get("critic") if isinstance(detail.get("critic"), dict) else {}
            issues = [] if str(user_note or "").strip() else (critic_info.get("issues") or [])
        body = _contract.build_revision_body(room, request_text, answer, issues, user_note, cfg)
        title = f"Доработка ответа (раунд {round_no})"
    else:
        raise EngineWriteError("bad_request", f"Неизвестный вид карточки цикла: {kind}")
    # Повтор после лимита провайдера — новый ключ идемпотентности (та же карточка
    # иначе не создастся: ключ занят упавшей). Номер раунда не меняется.
    suffix = f"-a{int(attempt)}" if int(attempt or 0) > 0 else ""
    card_id = _engine.create_child_card(cfg, room, board, parent, assignee, title, body, kind,
                                        idem_key=f"{kind}-{root_id}-r{round_no}{suffix}")
    registry = _registry_add_extra_card(cfg, room_id, root_id,
                                       {"id": card_id, "kind": kind, "round": int(round_no),
                                        "at": int(time.time())})
    return {"kind": kind, "ok": True, "task_id": card_id, "round": int(round_no),
            "assignee": assignee, "parent_id": parent, "registry": registry,
            "detail": "Создана карточка критика." if kind == "critic" else "Создана карточка доработки."}


def manual_critique(cfg, room: dict, record: dict, detail: dict, text: str | None = None) -> dict:
    """Ручной запуск критика (кнопка «Проверить ответ»)."""
    detail = detail or {}
    if not _synthesis_ready(detail):
        raise EngineWriteError("conflict", "Ответ ещё не готов — критиковать нечего.")
    info = detail.get("critic") if isinstance(detail.get("critic"), dict) else {}
    round_no = int(info.get("round") or 0) + 1
    result = _create_card(cfg, room, record, detail, "critic", round_no, user_note=text)
    result["action"] = "critique"
    _clear_stop(cfg, room, record, detail, result)      # ручной запуск снимает стоп страховки
    return result


def manual_revision(cfg, room: dict, record: dict, detail: dict, text: str | None = None) -> dict:
    """Ручная доработка ответа (кнопка «На доработку» с замечанием Михаила)."""
    detail = detail or {}
    if not _synthesis_ready(detail):
        raise EngineWriteError("conflict", "Ответ ещё не готов — дорабатывать нечего.")
    info = detail.get("critic") if isinstance(detail.get("critic"), dict) else {}
    round_no = max(1, int(info.get("round") or 0))
    parent = str(info.get("task_id") or "") or None
    result = _create_card(cfg, room, record, detail, "revision", round_no, user_note=text,
                          parent_id=parent)
    result["action"] = "revise"
    _clear_stop(cfg, room, record, detail, result)
    return result


def _clear_stop(cfg, room: dict, record: dict, detail: dict, result: dict) -> None:
    """Ручное действие снимает остановку/паузу цикла: страховка не должна блокировать."""
    if not (record.get("critic_stop") or record.get("critic_pause")):
        return
    root = _root_node(detail, record)
    root_id = str((record or {}).get("root_id") or root.get("id") or "")
    room_id = str((room or {}).get("id") or "")
    if not root_id or not room_id:
        return
    result["registry"] = _registry_update(cfg, room_id, root_id,
                                          {"critic_stop": None, "critic_pause": None})


def _now() -> int:
    return int(time.time())


def _journal(cfg, room_id, text: str, *, kind: str = "critic_verdict", actor: str = "app") -> None:
    """Запись в журнал комнаты (`journal.jsonl`) — формат как у ingest/agent_models.

    Журнал вторичен: любая ошибка файла/блокировки молча пропускается.
    """
    try:
        try:
            from . import statefiles as sf
        except ImportError:  # pragma: no cover - плоский импорт
            import statefiles as sf  # type: ignore
        from pathlib import Path
        path = Path(cfg.state_dir) / str(room_id) / "journal.jsonl"
        with sf.lock_for(path):
            number = 0
            for event in sf.read_jsonl(path):
                ident = str(event.get("id") or "")
                if ident.startswith("j_") and ident[2:].isdigit():
                    number = max(number, int(ident[2:]))
            sf.append_jsonl(path, {"id": "j_%04d" % (number + 1), "room": str(room_id),
                                   "at": _now(), "kind": kind, "actor": actor,
                                   "text": " ".join(str(text or "").split())[:600]})
    except Exception:  # noqa: BLE001 - журнал не должен ронять цикл
        pass


def run_step(cfg, room: dict, record: dict, detail: dict) -> list[dict]:
    """Выполнить plan() через engine_write. Возвращает отчёт по каждому действию."""
    room = room or {}
    record = dict(record or {})
    detail = detail or {}
    board = str(room.get("board") or "")
    room_id = str(room.get("id") or "")
    root = _root_node(detail, record)
    root_id = str(record.get("root_id") or root.get("id") or "")
    now = int(time.time())
    results: list[dict] = []
    if not root_id or not board:
        return results

    # История раундов: держим в реестре, чтобы UI показывал ход цикла, а страховки
    # («нет прогресса») работали даже если карточки пропали из дерева.
    history = history_from_detail(detail, record)
    if history and history != _round_history(record):
        record["critic_rounds"] = history
        _registry_update(cfg, room_id, root_id, {"critic_rounds": history})

    for action in plan(room, record, detail, now):
        kind = action.get("kind")
        entry = {"kind": kind, "ok": False}
        try:
            if kind == "retarget_root":
                res = _engine.retarget_root(cfg, board, root_id, action["profile"])
                entry.update(ok=True, detail=res.get("detail"), cmd_ms=res.get("cmd_ms", 0),
                             registry=_registry_update(cfg, room_id, root_id, {"retargeted": True}))
            elif kind == "create_critic":
                made = _create_card(cfg, room, record, detail, "critic",
                                    int(action.get("round", 1)))
                entry.update(made)
                entry["kind"] = "create_critic"
                _journal(cfg, room_id, "Критик проверяет ответ (раунд %d%s)." % (
                    made["round"], ", до полного одобрения" if action.get("mode") == MODE_UNTIL_APPROVED else ""),
                    kind="critic_review")
            elif kind == "create_revision":
                entry.update(_create_card(cfg, room, record, detail, "revision",
                                          int(action.get("round", 1)),
                                          user_note=action.get("user_note"),
                                          parent_id=action.get("parent_id"),
                                          issues=action.get("issues")),
                             kind="create_revision")
                _journal(cfg, room_id, "Критик вернул ответ на доработку (раунд %s)."
                         % action.get("round"), kind="revision_created")
            elif kind == "mark_ingest":
                entry.update(ok=True, detail="Ответ готов к ingest.",
                             registry=_registry_update(cfg, room_id, root_id,
                                                       {"ingest_pending": True}))
            elif kind in ("stop_no_progress", "stop_cap"):
                stop = dict(action.get("stop") or {})
                stop.setdefault("at", now)
                stop["needs_user"] = True
                upd = _registry_update(cfg, room_id, root_id,
                                       {"critic_stop": stop, "ingest_pending": True,
                                        "critic_pause": None})
                entry.update(ok=True, detail=stop.get("message"), stop=stop, registry=upd,
                             kind=kind)
                _journal(cfg, room_id, "Цикл критика остановлен: %s Ответ показан как есть, "
                                       "нужно ваше решение." % (stop.get("message") or ""),
                         kind="critic_verdict")
            elif kind == "clear_critic_state":
                entry.update(ok=True, detail="Состояние цикла сброшено: критик одобрил ответ.",
                             registry=_registry_update(cfg, room_id, root_id,
                                                       {"critic_pause": None, "critic_stop": None}))
            elif kind == "pause_rate_limit":
                pause = {"kind": action.get("card_kind"), "round": action.get("round"),
                         "until": int(action.get("until") or 0), "at": now,
                         "attempt": int(action.get("attempt") or 0),
                         "reason": "лимит провайдера"}
                entry.update(ok=True, detail="Лимит провайдера — пауза до %d." % pause["until"],
                             pause=pause,
                             registry=_registry_update(cfg, room_id, root_id,
                                                       {"critic_pause": pause}))
                _journal(cfg, room_id, "Лимит провайдера у карточки %s (раунд %s): пауза около "
                                       "%d мин, затем повтор." % (
                                           action.get("card_kind"), action.get("round"),
                                           max(1, (pause["until"] - now) // 60)),
                         kind="critic_review")
            elif kind == "retry_card":
                card_kind = str(action.get("card_kind") or "critic")
                made = _create_card(cfg, room, record, detail, card_kind,
                                    int(action.get("round", 1)),
                                    attempt=int(action.get("attempt") or 1))
                pause = {"kind": card_kind, "round": action.get("round"), "until": 0, "at": now,
                         "attempt": int(action.get("attempt") or 1), "reason": ""}
                entry.update(made)
                entry.update(ok=True, pause=pause, kind="retry_card",
                             registry=_registry_update(cfg, room_id, root_id,
                                                       {"critic_pause": None}))
                _journal(cfg, room_id, "Повтор карточки %s (раунд %s) после лимита провайдера."
                         % (card_kind, action.get("round")), kind="critic_review")
            else:
                entry.update(detail=f"Неизвестное действие плана: {kind}")
        except EngineWriteError as exc:
            entry.update(ok=False, error={"code": exc.code, "message": exc.message_ru,
                                          "stderr": exc.stderr_tail})
            results.append(entry)
            break
        except Exception as exc:  # сеть/файлы/реестр: не роняем watcher
            entry.update(ok=False, error={"code": "engine_error", "message": str(exc)})
            results.append(entry)
            break
        results.append(entry)
    return results
