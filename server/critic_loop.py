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
import time

try:  # пакетный и «плоский» импорт (python -m unittest из корня проекта)
    from . import contract as _contract
    from . import engine_write as _engine
except ImportError:  # pragma: no cover
    import contract as _contract  # type: ignore
    import engine_write as _engine  # type: ignore

DEFAULT_MAX_ROUNDS = 1
MAX_ROUNDS_LIMIT = 2

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
    max_rounds = _max_rounds(record, room, detail)
    cards = _extra_cards(record, detail)
    critics = sorted([c for c in cards if c["kind"] == "critic"], key=lambda c: (c["round"], c["at"]))
    revisions = [c for c in cards if c["kind"] == "revision"]
    critic_info = detail.get("critic") if isinstance(detail.get("critic"), dict) else None

    if not critic:
        # критик не назначен — проверять некому, отдаём ответ в ingest
        if not record.get("ingest_pending"):
            actions.append({"kind": "mark_ingest", "root_id": root_id, "reason": "критик не назначен"})
        return actions

    if not critics:
        actions.append({"kind": "create_critic", "root_id": root_id, "parent_id": root_id,
                        "assignee": critic, "round": 1, "reason": "проверка ответа"})
        return actions

    latest = critics[-1]
    node = _node(detail, str(latest.get("id")))
    if node is None and critic_info and str(critic_info.get("task_id") or "") == str(latest.get("id"))             and critic_info.get("verdict"):
        # движок ещё не отдал карточку в дереве, но вердикт уже известен из detail.critic
        node = {"id": str(latest.get("id")), "status": "done", "metadata": {}, "summary_md": ""}
    if not node or str(node.get("status") or "") != "done":
        return actions  # критик ещё работает
    verdict = _verdict(node, critic_info)
    if verdict == "approve":
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
    if not rev_node or str(rev_node.get("status") or "") != "done":
        return actions  # доработка ещё идёт
    if latest["round"] < max_rounds:
        actions.append({"kind": "create_critic", "root_id": root_id, "parent_id": root_id,
                        "assignee": critic, "round": latest["round"] + 1,
                        "reason": "повторная проверка после доработки"})
    elif not record.get("ingest_pending"):
        actions.append({"kind": "mark_ingest", "root_id": root_id,
                        "reason": "раунды критика исчерпаны"})
    return actions


# ---------------------------------------------------------------- исполнение

def _create_card(cfg, room: dict, record: dict, detail: dict, kind: str, round_no: int,
                 user_note=None, parent_id=None, issues=None) -> dict:
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
    card_id = _engine.create_child_card(cfg, room, board, parent, assignee, title, body, kind,
                                        idem_key=f"{kind}-{root_id}-r{round_no}")
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
    return result


def run_step(cfg, room: dict, record: dict, detail: dict) -> list[dict]:
    """Выполнить plan() через engine_write. Возвращает отчёт по каждому действию."""
    room = room or {}
    record = record or {}
    detail = detail or {}
    board = str(room.get("board") or "")
    room_id = str(room.get("id") or "")
    root = _root_node(detail, record)
    root_id = str(record.get("root_id") or root.get("id") or "")
    now = int(time.time())
    results: list[dict] = []
    if not root_id or not board:
        return results

    for action in plan(room, record, detail, now):
        kind = action.get("kind")
        entry = {"kind": kind, "ok": False}
        try:
            if kind == "retarget_root":
                res = _engine.retarget_root(cfg, board, root_id, action["profile"])
                entry.update(ok=True, detail=res.get("detail"), cmd_ms=res.get("cmd_ms", 0),
                             registry=_registry_update(cfg, room_id, root_id, {"retargeted": True}))
            elif kind == "create_critic":
                entry.update(_create_card(cfg, room, record, detail, "critic",
                                          int(action.get("round", 1))), kind="create_critic")
            elif kind == "create_revision":
                entry.update(_create_card(cfg, room, record, detail, "revision",
                                          int(action.get("round", 1)),
                                          user_note=action.get("user_note"),
                                          parent_id=action.get("parent_id"),
                                          issues=action.get("issues")),
                             kind="create_revision")
            elif kind == "mark_ingest":
                entry.update(ok=True, detail="Ответ готов к ingest.",
                             registry=_registry_update(cfg, room_id, root_id,
                                                       {"ingest_pending": True}))
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
