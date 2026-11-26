"""Проект «задача → подзадачи»: предложение руководителя и отдельные задачи-корни (W30 §2).

Правила (общий контракт волны 30, §2)
-------------------------------------
* Руководитель пишет оценку задачи ОДНИМ комментарием `[Руководитель · оценка задачи]`
  с metadata ``{task_kind, summary, branches}`` (текст правила — `contract_project.py`).
  Комментарии карточки в базе движка не имеют поля metadata, поэтому JSON живёт прямо
  в тексте комментария (ограждённый блок или первый JSON-объект). Разбор — здесь.
* Запись предложения идемпотентна: один и тот же комментарий при повторном чтении
  не создаёт вторую запись и НЕ теряет решения человека.

    data/state/<room>/proposals/<root_id>.json      — предложение (форма §2)
    data/state/<room>/projects.json                 — связь «родитель → подзадачи»

* Подзадачи — ОТДЕЛЬНЫЕ задачи-корни (`engine_write.create_subtask_request`), а НЕ
  дочерние карточки родителя: дочерняя карточка слилась бы с деревом родителя, её
  работа попала бы в его свод как «шаг», а не как самостоятельная задача. Связь
  «родитель → подзадачи» живёт только в `projects.json` (никаких `task_links`).
* Критик наследуется от родителя (запись реестра подзадачи повторяет `critic`,
  `critic_mode`, `critic_round_cap`): цикл критика и живой свод подхватят её сами,
  как обычную задачу комнаты.
* Ничего не запускается само: задачи создаются только по явному действию человека
  («Принять всё» / «Применить»). Никаких авто-применений к существующим данным.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

try:  # пакетный и «плоский» импорт (python server/app.py)
    from . import contract_project as rule
    from . import statefiles as sf
except ImportError:  # pragma: no cover
    import contract_project as rule        # type: ignore
    import statefiles as sf                # type: ignore

PROPOSALS_DIR = "proposals"
PROJECTS_FILE = "projects.json"

KINDS = ("single", "big")
STATE_RU = {"pending": "ждёт вашего решения", "approved": "согласовано",
            "rejected": "отклонено", "custom": "свой вариант", "applied": "задачи созданы"}
SINGLE_DECISIONS = ("pending", "agree", "disagree", "custom")
BRANCH_DECISIONS = ("pending", "yes", "no", "custom")
RESOLVE_ACTIONS = ("accept_all", "reject_all", "custom", "apply")

MAX_BRANCHES = int(rule.MAX_BRANCHES or 8)
MAX_TITLE = 120
MAX_GOAL = 600
MAX_SUMMARY = 600
MAX_QUESTIONS = 5
MAX_QUESTION = 300
MAX_TEXT = 2000
MAX_CHILDREN = 24

TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
_BRANCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

#: Статус карточки движка → стадия задачи (подписи — на клиенте, D1/D2).
_STAGE_OF = {
    "triage": "new", "queued": "new", "todo": "new", "scheduled": "new",
    "ready": "working", "running": "working", "waiting_quota": "working",
    "blocked": "needs_you", "review": "review",
    "done": "done_system", "failed": "working", "archived": "archived",
}
STATUS_RU = {
    "triage": "ждёт разбора", "queued": "в очереди", "todo": "в очереди",
    "scheduled": "запланирована", "ready": "в очереди", "running": "в работе",
    "waiting_quota": "ждёт лимита", "blocked": "ждёт решения", "review": "на проверке",
    "done": "готово", "failed": "с ошибкой", "archived": "в архиве", "": "неизвестно",
}


class ProjectError(RuntimeError):
    """Отказ действия по предложению: русский текст + машинный код + HTTP-статус."""

    def __init__(self, message: str, code: str = "bad_request", http: int = 400,
                 detail: dict | None = None):
        super().__init__(message)
        self.code = str(code or "bad_request")
        self.message = str(message or "Действие невозможно")
        self.http = int(http or 400)
        self.detail = dict(detail or {})


# ---------------------------------------------------------------- модули (лениво)
def _engine():
    try:
        from . import engine_write as mod      # type: ignore
    except ImportError:                        # pragma: no cover - плоский импорт
        import engine_write as mod             # type: ignore
    return mod


def _registry():
    try:
        from . import registry as mod          # type: ignore
    except ImportError:                        # pragma: no cover
        import registry as mod                 # type: ignore
    return mod


def _reader():
    try:
        from . import engine_read as mod       # type: ignore
    except ImportError:                        # pragma: no cover
        import engine_read as mod              # type: ignore
    return mod


def _thread_mod():
    try:
        from . import thread as mod            # type: ignore
    except ImportError:                        # pragma: no cover
        import thread as mod                   # type: ignore
    return mod


def _guard():
    try:
        from . import board_guard as mod       # type: ignore
    except ImportError:                        # pragma: no cover
        import board_guard as mod              # type: ignore
    return mod


def _now() -> int:
    return int(time.time())


def _room_id(room) -> str:
    return str((room or {}).get("id") or "") if isinstance(room, dict) else str(room or "")


def _board(room) -> str:
    return str((room or {}).get("board") or "") if isinstance(room, dict) else ""


def _text(value, limit: int = 0) -> str:
    """Строка из чего угодно, одна строка, без NUL, не длиннее `limit`."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    clean = " ".join(value.replace("\x00", " ").split())
    if limit and len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean


# ---------------------------------------------------------------- пути и хранилище
def proposals_dir(cfg, room_id: str) -> Path:
    return Path(cfg.state_dir) / str(room_id) / PROPOSALS_DIR


def proposal_path(cfg, room_id: str, root_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_]+", "-", str(root_id))[:40] or "root"
    return proposals_dir(cfg, room_id) / ("%s.json" % safe)


def projects_path(cfg, room_id: str) -> Path:
    return Path(cfg.state_dir) / str(room_id) / PROJECTS_FILE


def load_proposal(cfg, room_id: str, root_id: str) -> dict | None:
    """Предложение из файла (только чтение, без разбора комментариев)."""
    path = proposal_path(cfg, room_id, root_id)
    data = sf.read_json(path, None)
    if not isinstance(data, dict) or not data:
        return None
    return _norm_proposal(data, root_id)


def save_proposal(cfg, room_id: str, proposal: dict) -> dict:
    """Записать предложение (атомарно, под блокировкой файла)."""
    proposal = _norm_proposal(proposal, str(proposal.get("root_id") or ""))
    proposal["updated_at"] = _now()
    path = proposal_path(cfg, room_id, str(proposal.get("root_id") or ""))
    with sf.lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write_json_atomic(path, proposal)
    return proposal


def load_projects(cfg, room_id: str) -> dict:
    """Связи «родитель → подзадачи»: ``{root_id: {children: [...], parent: str|null}}``."""
    data = sf.read_json(projects_path(cfg, room_id), {})
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        children = [str(c) for c in (value.get("children") or [])
                    if isinstance(c, str) and TASK_ID_RE.fullmatch(c)]
        parent = value.get("parent")
        parent = str(parent) if isinstance(parent, str) and TASK_ID_RE.fullmatch(parent) else None
        out[key] = {"children": list(dict.fromkeys(children))[:MAX_CHILDREN], "parent": parent}
    return out


def save_projects(cfg, room_id: str, data: dict) -> None:
    path = projects_path(cfg, room_id)
    with sf.lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write_json_atomic(path, data)


def link_child(cfg, room_id: str, parent_id: str, child_id: str) -> dict:
    """Записать связь «родитель → подзадача» (идемпотентно, обе стороны)."""
    parent_id, child_id = str(parent_id or ""), str(child_id or "")
    if not (TASK_ID_RE.fullmatch(parent_id) and TASK_ID_RE.fullmatch(child_id)):
        raise ProjectError("Некорректный id задачи для связи проекта.", "bad_request", 400,
                           {"parent": parent_id, "child": child_id})
    path = projects_path(cfg, room_id)
    with sf.lock_for(path):
        data = load_projects(cfg, room_id)
        node = data.setdefault(parent_id, {"children": [], "parent": None})
        if child_id not in node["children"]:
            node["children"].append(child_id)
        node["children"] = node["children"][:MAX_CHILDREN]
        child = data.setdefault(child_id, {"children": [], "parent": None})
        child["parent"] = parent_id
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write_json_atomic(path, data)
    return data


def project_of(cfg, room_id: str, root_id: str) -> dict:
    """Запись проекта одной задачи: ``{children: [...], parent: str|null}``."""
    node = load_projects(cfg, room_id).get(str(root_id)) or {}
    return {"children": list(node.get("children") or []), "parent": node.get("parent")}


def children_ids(cfg, room, root_id: str) -> list[str]:
    """id подзадач задачи — для списков и стадий (`parent_id`/`children_count`, §3)."""
    return list(project_of(cfg, _room_id(room), root_id).get("children") or [])


# ---------------------------------------------------------------- разбор предложения
def _balanced_json(text: str, start: int) -> str:
    """Сбалансированный JSON-объект с позиции `start` (строки и экранирование учтены)."""
    depth, in_str, esc = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_str:
            if esc:
                esc = False
            elif char == "\\":
                esc = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def _json_payload(text: str) -> dict:
    """JSON предложения из текста комментария: ограждённый блок или первый подходящий объект."""
    raw = str(text or "")
    candidates: list[str] = []
    for match in re.finditer(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", raw, re.S):
        candidates.append(match.group(1))
    start = raw.find("{")
    while start >= 0 and len(candidates) < 8:
        chunk = _balanced_json(raw, start)
        if chunk:
            candidates.append(chunk)
        start = raw.find("{", start + 1)
    for chunk in candidates:
        try:
            data = json.loads(chunk, strict=False)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and any(k in data for k in
                                          ("task_kind", "kind", "branches", "summary")):
            return data
    return {}


def _kind_of(value) -> str:
    low = _text(value, 40).lower()
    if low in ("big", "large", "много", "объёмная", "объемная", "объёмный", "объемный"):
        return "big"
    return "single"


def _norm_questions(raw) -> list[str]:
    out: list[str] = []
    items = raw if isinstance(raw, (list, tuple)) else ([raw] if isinstance(raw, str) else [])
    for item in items:
        text = _text(item, MAX_QUESTION)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def _norm_branch(raw, index: int) -> dict | None:
    """Ветка предложения в канонической форме §2 (без `decision` — его ставит человек)."""
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("title") or raw.get("name"), MAX_TITLE)
    if not title:
        return None
    bid = _text(raw.get("id"), 16)
    if not _BRANCH_ID_RE.fullmatch(bid or ""):
        bid = "b%d" % index
    goal = _text(raw.get("goal") or raw.get("what") or raw.get("why"), MAX_GOAL)
    assignee = _text(raw.get("assignee") or raw.get("owner") or raw.get("profile"), 64)
    if assignee.lower() in ("none", "unassign", "-"):
        assignee = ""
    try:
        order = int(raw.get("order"))
    except (TypeError, ValueError):
        order = index
    decision = _text(raw.get("decision"), 16).lower()
    if decision not in BRANCH_DECISIONS:
        decision = "pending"
    task_id = _text(raw.get("task_id"), 20)
    return {"id": bid, "title": title, "goal": goal or title, "assignee": assignee,
            "questions": _norm_questions(raw.get("questions")),
            "order": order if order > 0 else index, "decision": decision,
            "custom_text": _text(raw.get("custom_text"), MAX_TEXT),
            "task_id": task_id if TASK_ID_RE.fullmatch(task_id) else None}


def _norm_single(raw) -> dict:
    value = raw if isinstance(raw, dict) else {}
    decision = _text(value.get("decision"), 16).lower()
    if decision not in SINGLE_DECISIONS:
        decision = "pending"
    return {"decision": decision, "text": _text(value.get("text"), MAX_TEXT)}


def _norm_resolution(raw) -> dict | None:
    if not isinstance(raw, dict) or not raw:
        return None
    action = _text(raw.get("action"), 20).lower()
    if action not in ("accept_all", "reject_all", "custom", "partial"):
        action = "custom"
    try:
        at = int(raw.get("at") or 0)
    except (TypeError, ValueError):
        at = 0
    return {"action": action, "text": _text(raw.get("text"), MAX_TEXT), "at": at}


def _norm_proposal(data: dict, root_id: str = "") -> dict:
    """Привести предложение к форме §2, не теряя решений человека."""
    data = data if isinstance(data, dict) else {}
    kind = _text(data.get("kind"), 16).lower()
    if kind not in KINDS:
        kind = "single"
    state = _text(data.get("state"), 16).lower()
    if state not in STATE_RU:
        state = "pending"
    branches: list[dict] = []
    for index, raw in enumerate(data.get("branches") or [], start=1):
        branch = _norm_branch(raw, index)
        if branch:
            branches.append(branch)
    return {
        "id": _text(data.get("id"), 24) or ("pr_" + hashlib.sha1(
            ("%s|%s" % (root_id, data.get("comment_id") or "")).encode("utf-8")).hexdigest()[:8]),
        "root_id": str(data.get("root_id") or root_id or ""),
        "kind": kind, "state": state,
        "summary": _text(data.get("summary"), MAX_SUMMARY),
        "author": _text(data.get("author"), 64),
        "created_at": int(data.get("created_at") or 0) or _now(),
        "updated_at": int(data.get("updated_at") or 0) or _now(),
        "single": _norm_single(data.get("single")),
        "branches": branches[:MAX_BRANCHES],
        "resolution": _norm_resolution(data.get("resolution")),
        "comment_id": _text(data.get("comment_id"), 24),
        #: Отпечаток разобранного комментария — по нему повторное чтение ничего не меняет.
        "signature": _text(data.get("signature"), 32),
        "problems": [str(p) for p in (data.get("problems") or []) if isinstance(p, str)][:6],
    }


def parse_comment(text: str) -> dict | None:
    """Текст комментария руководителя → черновик предложения (без записи на диск).

    Поддерживаются оба вида metadata: ограждённый ```json-блок и просто JSON-объект в
    тексте. Без JSON предложение всё равно читается: вид — по словам «объёмная/ветки»,
    сводка — текст после метки (руководитель мог написать по-человечески).
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if not any(low.startswith(alias.lower()) for alias in rule.MARK_ALIASES):
        # метка может стоять во второй строке (заголовок/жирный) — ищем в первых 3 строках
        head = "\n".join(raw.splitlines()[:3]).lower()
        if not any(alias.lower() in head for alias in rule.MARK_ALIASES):
            return None
    body = raw
    for alias in rule.MARK_ALIASES:
        index = body.lower().find(alias.lower())
        if index >= 0:
            body = body[index + len(alias):]
            break
    data = _json_payload(raw)
    prose = _text(body.replace("```", " "), MAX_SUMMARY)
    if data:
        kind = _kind_of(data.get("task_kind") or data.get("kind"))
        summary = _text(data.get("summary"), MAX_SUMMARY) or prose
    else:
        kind = "big" if re.search(r"объ[её]мн|несколько направлен|ветк|подзадач", low) else "single"
        summary = prose
    draft = {"kind": kind, "summary": summary,
             "branches": data.get("branches") if isinstance(data.get("branches"), list) else []}
    return draft


def _proposal_id(root_id: str, comment_id: str, signature: str) -> str:
    raw = "%s|%s|%s" % (root_id, comment_id, signature)
    return "pr_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _signature(draft: dict) -> str:
    payload = json.dumps(draft, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _merge_decisions(new: dict, old: dict | None) -> dict:
    """Перенести решения человека в заново разобранное предложение (идемпотентность)."""
    if not isinstance(old, dict):
        return new
    if old.get("id") != new.get("id"):
        return new                      # другая оценка — решения прежней не переносим
    new["single"] = _norm_single(old.get("single") if new["single"]["decision"] == "pending"
                                 else new["single"])
    old_branches = {str(b.get("id")): b for b in old.get("branches") or [] if isinstance(b, dict)}
    by_title = {_text(b.get("title"), MAX_TITLE).lower(): b for b in old.get("branches") or []
                if isinstance(b, dict)}
    for branch in new["branches"]:
        prev = old_branches.get(branch["id"]) or by_title.get(branch["title"].lower())
        if not prev:
            continue
        branch["decision"] = prev.get("decision") or "pending"
        branch["custom_text"] = prev.get("custom_text") or ""
        branch["task_id"] = prev.get("task_id") or None
    for key in ("state", "resolution", "created_at"):
        if old.get(key):
            new[key] = old[key]
    return new


def _thread_items(cfg, room, root_id: str, thread=None) -> list:
    if isinstance(thread, list):
        return thread
    try:
        return _thread_mod().get_thread(cfg, root_id, room=room, board=_board(room)) or []
    except Exception:                       # noqa: BLE001 - лента вторична для предложения
        return []


def _lead_comments(items: list) -> list:
    """Комментарии агентов с меткой оценки (свежие в конце)."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if str(item.get("author_kind") or "") == "mikhail":
            continue
        if parse_comment(text):
            out.append(item)
    return out


def sync(cfg, room, root_id: str, thread=None) -> dict | None:
    """Разобрать комментарии и идемпотентно записать предложение → оно или прежнее/None."""
    room_id = _room_id(room)
    if not room_id or not root_id:
        return None
    stored = load_proposal(cfg, room_id, root_id)
    comments = _lead_comments(_thread_items(cfg, room, root_id, thread))
    if not comments:
        return stored
    last = comments[-1]
    draft = parse_comment(str(last.get("text") or ""))
    if not draft:
        return stored
    comment_id = str(last.get("id") or "")
    signature = _signature(draft)
    if stored and stored.get("comment_id") == comment_id \
            and _text(stored.get("signature"), 32) == signature:
        return stored                       # тот же комментарий — ничего не меняем
    branches = [b for b in (_norm_branch(raw, i) for i, raw in enumerate(draft["branches"], 1)) if b]
    if draft["kind"] == "big" and len(branches) > MAX_BRANCHES:
        branches = branches[:MAX_BRANCHES]
    proposal = {
        "id": _proposal_id(root_id, comment_id, signature),
        "root_id": root_id, "kind": draft["kind"], "state": "pending",
        "summary": draft["summary"], "author": _text(last.get("author"), 64),
        "created_at": int(last.get("at") or 0) or _now(),
        "single": {"decision": "pending", "text": ""},
        "branches": branches,
        "resolution": None, "comment_id": comment_id, "signature": signature,
        "problems": ([] if (draft["kind"] == "single" or branches)
                     else ["в предложении нет веток — разбивать нечего"]),
    }
    proposal = _merge_decisions(_norm_proposal(proposal, root_id), stored)
    if stored and json.dumps(_strip_volatile(stored), sort_keys=True, ensure_ascii=False) == \
            json.dumps(_strip_volatile(proposal), sort_keys=True, ensure_ascii=False):
        return stored
    try:
        return save_proposal(cfg, room_id, proposal)
    except Exception:                       # noqa: BLE001 - чтение важнее записи
        return proposal if not stored else stored


def _strip_volatile(proposal: dict) -> dict:
    return {k: v for k, v in (proposal or {}).items() if k != "updated_at"}


def get(cfg, room, root_id: str) -> dict | None:
    """Предложение задачи без разбора ленты (для внутренних вызовов)."""
    return load_proposal(cfg, _room_id(room), root_id)


def ready(proposal: dict | None) -> bool:
    """Можно ли действовать по предложению: оно есть, ветки не применены."""
    if not isinstance(proposal, dict) or not proposal:
        return False
    if proposal.get("state") == "applied":
        return False
    if proposal.get("kind") == "big":
        return bool(proposal.get("branches"))
    return True


# ---------------------------------------------------------------- решения человека
def _need(cfg, room, root_id: str) -> dict:
    proposal = get(cfg, room, root_id)
    if not proposal:
        raise ProjectError("Руководитель ещё не предложил разбиение этой задачи — решать нечего.",
                           "no_proposal", 409, {"rid": root_id})
    return proposal


def _decide_single(proposal: dict, decision: str, text: str) -> str:
    if decision not in ("agree", "disagree", "custom"):
        raise ProjectError("Для разовой задачи ответы: «Согласен», «Не согласен», «Свой вариант».",
                           "bad_decision", 400, {"allowed": ["agree", "disagree", "custom"]})
    if decision == "custom" and not text:
        raise ProjectError("«Свой вариант» — напишите, что изменить.", "text_required", 400,
                           {"field": "text"})
    proposal["single"] = {"decision": decision, "text": text}
    proposal["state"] = {"agree": "approved", "disagree": "rejected",
                         "custom": "custom"}[decision]
    return {"agree": "Согласовано: выполняем одной задачей.",
            "disagree": "Отклонено: руководитель пересоберёт оценку.",
            "custom": "Ваш вариант сохранён — руководитель учтёт его."}[decision]


def decide(cfg, room, root_id: str, branch_id: str | None = None, decision: str = "",
           text: str = "") -> dict:
    """Решение человека по ветке (`yes|no|custom`) или по разовой задаче (`agree|disagree|custom`)."""
    proposal = _need(cfg, room, root_id)
    if proposal.get("state") == "applied":
        raise ProjectError("Задачи по предложению уже созданы — менять решение поздно. "
                           "Нужны правки — напишите руководителю.", "already_applied", 409)
    text = _text(text, MAX_TEXT)
    decision = _text(decision, 16).lower()
    if proposal.get("kind") == "single":
        message = _decide_single(proposal, decision, text)
    else:
        branches = proposal.get("branches") or []
        if not branches:
            raise ProjectError("В предложении нет веток — нечего утверждать.", "no_branches", 409)
        if not branch_id:
            raise ProjectError("Не сказано, по какой ветке решение.", "branch_required", 400,
                               {"field": "branch_id"})
        branch = next((b for b in branches if b.get("id") == branch_id), None)
        if branch is None:
            raise ProjectError("Такой ветки в предложении нет.", "branch_not_found", 404,
                               {"branch_id": branch_id})
        if decision not in ("yes", "no", "custom"):
            raise ProjectError("По ветке ответы: «Да», «Нет», «Свой вариант».", "bad_decision", 400,
                               {"allowed": ["yes", "no", "custom"]})
        if decision == "custom" and not text:
            raise ProjectError("«Свой вариант» — напишите, что изменить в ветке.", "text_required",
                               400, {"field": "text", "branch_id": branch_id})
        branch["decision"] = decision
        branch["custom_text"] = text if decision == "custom" else ""
        message = {"yes": "Ветка «%s»: да.", "no": "Ветка «%s»: нет.",
                   "custom": "Ветка «%s»: ваш вариант сохранён."}[decision] % branch.get("title")
    proposal = save_proposal(cfg, _room_id(room), proposal)
    return {"proposal": proposal, "message": message, "ready": ready(proposal)}


def resolve(cfg, room, rec: dict, root_id: str, action: str, text: str = "",
            board: str = "") -> dict:
    """Общее решение: принять всё / отклонить / свой вариант / создать задачи по веткам."""
    action = _text(action, 20).lower()
    if action not in RESOLVE_ACTIONS:
        raise ProjectError("Неизвестное решение: %s." % (action or "(пусто)"), "bad_action", 400,
                           {"allowed": list(RESOLVE_ACTIONS)})
    proposal = _need(cfg, room, root_id)
    text = _text(text, MAX_TEXT)
    if action == "custom" and not text:
        raise ProjectError("«Свой вариант» — напишите, что изменить в предложении.",
                           "text_required", 400, {"field": "text"})
    kind = proposal.get("kind")
    if action == "accept_all":
        if kind == "big":
            for branch in proposal.get("branches") or []:
                branch["decision"] = "yes"
                branch["custom_text"] = ""
        else:
            proposal["single"] = {"decision": "agree", "text": text}
        proposal["state"] = "approved"
        proposal["resolution"] = {"action": "accept_all", "text": text, "at": _now()}
        proposal = save_proposal(cfg, _room_id(room), proposal)
        return {"proposal": proposal, "created": [], "duplicate": False,
                "message": "Принято всё предложение. Нажмите «Создать задачи», чтобы начать работу."
                           if kind == "big" else "Согласовано: выполняем одной задачей."}
    if action == "reject_all":
        if kind == "big":
            for branch in proposal.get("branches") or []:
                branch["decision"] = "no"
        else:
            proposal["single"] = {"decision": "disagree", "text": text}
        proposal["state"] = "rejected"
        proposal["resolution"] = {"action": "reject_all", "text": text, "at": _now()}
        proposal = save_proposal(cfg, _room_id(room), proposal)
        _tell_lead(cfg, room, root_id, "отклонил предложение", text)
        return {"proposal": proposal, "created": [], "duplicate": False,
                "message": "Предложение отклонено. Руководитель пересоберёт оценку."}
    if action == "custom":
        if kind == "big":
            for branch in proposal.get("branches") or []:
                if branch.get("decision") == "pending":
                    branch["decision"] = "custom"
                    branch["custom_text"] = text
        else:
            proposal["single"] = {"decision": "custom", "text": text}
        proposal["state"] = "custom"
        proposal["resolution"] = {"action": "custom", "text": text, "at": _now()}
        proposal = save_proposal(cfg, _room_id(room), proposal)
        warning = _tell_lead(cfg, room, root_id, "предложил свой вариант", text)
        return {"proposal": proposal, "created": [], "duplicate": False, "warning": warning,
                "message": "Ваш вариант сохранён и отправлен руководителю."}
    return _apply(cfg, room, rec, root_id, proposal, board=board, text=text)


def _tell_lead(cfg, room, root_id: str, what: str, text: str) -> str:
    """Сообщить руководителю решение человека комментарием в корне (сбой — предупреждение)."""
    body = "[Михаил · %s по предложению задач] %s" % (what, text or "без пояснений")
    try:
        _engine().act(cfg, _board(room), root_id, "comment",
                      text=body[:MAX_TEXT], author="mikhail")
        return ""
    except Exception as exc:                # noqa: BLE001 - решение уже сохранено
        return "Ваш вариант сохранён, но руководителю сообщить не удалось: %s" % str(exc)[:160]


# ---------------------------------------------------------------- создание подзадач
def _subtask_body(parent_title: str, branch: dict) -> str:
    """Тело подзадачи: «Подзадача проекта <родитель>: <цель ветки>» + вопросы + порядок."""
    goal = _text(branch.get("goal") or branch.get("title"), MAX_GOAL)
    lines = ["Подзадача проекта %s: %s" % (_text(parent_title, MAX_TITLE) or "комнаты", goal), ""]
    if goal and goal != _text(branch.get("title"), MAX_TITLE):
        lines.append(goal)
        lines.append("")
    if branch.get("questions"):
        lines.append("Вопросы к этой ветке (ответить в её своде):")
        lines += ["- %s" % q for q in branch["questions"]]
        lines.append("")
    order = branch.get("order")
    if isinstance(order, int) and order > 0:
        lines.append("Порядок в проекте: %d." % order)
    if branch.get("custom_text"):
        lines.append("Уточнение Михаила: %s" % branch["custom_text"])
    lines.append("Работа сдаётся руководителю комнаты; критик — как у главной задачи проекта.")
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def _child_record(parent_rec: dict, branch: dict, slug: str, title: str, body: str) -> dict:
    """Запись реестра подзадачи: критик и режим проверки наследуются от родителя."""
    return {
        "root_id": "", "slug": slug, "title": title, "text": body,
        "created_at": _now(),
        "critic": bool(parent_rec.get("critic")),
        "mode": "auto",
        "critic_mode": parent_rec.get("critic_mode") or "rounds",
        "critic_round_cap": parent_rec.get("critic_round_cap"),
        "adopted": False, "extra_cards": [], "ingested": {}, "retargeted": False,
        "kind": "subtask", "branch_id": branch.get("id") or "",
    }


def _apply(cfg, room, rec: dict, root_id: str, proposal: dict, board: str = "",
           text: str = "") -> dict:
    """Создать задачи по утверждённым веткам (идемпотентно, ничего не дублируя)."""
    room_id = _room_id(room)
    board = board or _board(room)
    if proposal.get("state") == "applied":
        return {"proposal": proposal, "duplicate": True,
                "created": _created_view(proposal), "message": "Задачи по этому предложению уже созданы."}
    if proposal.get("kind") == "single":
        if proposal.get("single", {}).get("decision") == "pending":
            raise ProjectError("Сначала ответьте на оценку: «Согласен» или «Не согласен».",
                               "not_decided", 409)
        proposal["state"] = "applied"
        proposal["resolution"] = {"action": "accept_all", "text": text, "at": _now()}
        proposal = save_proposal(cfg, room_id, proposal)
        return {"proposal": proposal, "created": [], "duplicate": False,
                "message": "Разовая задача — работа идёт в этой же задаче, отдельных подзадач нет."}
    branches = [b for b in proposal.get("branches") or []
                if b.get("decision") in ("yes", "custom")]
    if not branches:
        raise ProjectError("Нет утверждённых веток: отметьте «Да» хотя бы у одной или нажмите "
                           "«Принять всё предложение».", "nothing_to_apply", 409)
    blocked = _guard().check(cfg, board)
    if blocked:
        raise ProjectError(str(blocked.get("message") or "Запись в доску отклонена"),
                           str(blocked.get("code") or "sandbox_real_board"), 409,
                           dict(blocked.get("detail") or {}))
    parent_title = _text(rec.get("title"), MAX_TITLE) or str(root_id)
    created: list[dict] = []
    warnings: list[str] = []
    engine = _engine()
    #: Идём по id веток, а не по ссылкам на словари: `save_proposal` возвращает
    #: нормализованную КОПИЮ, и ссылка на прежний словарь потеряла бы `task_id`
    #: следующей ветки (так уже созданные задачи превращались в «несозданные»).
    branch_ids = [str(b.get("id")) for b in branches]
    for branch_id in branch_ids:
        branch = next((b for b in proposal.get("branches") or []
                       if str(b.get("id")) == branch_id), None)
        if branch is None:
            continue
        if branch.get("task_id"):
            continue                        # уже создана — второй раз не создаём
        title = _text(branch.get("title"), MAX_TITLE) or "Подзадача"
        body = _subtask_body(parent_title, branch)
        assignee = _text(branch.get("assignee"), 64)
        slug = "r-sub-%s-%s" % (str(root_id)[:10], branch.get("id") or "b")
        try:
            res = engine.create_subtask_request(
                cfg, room, slug, title, body, assignee,
                idem_key="r-sub-%s-%s" % (root_id, branch.get("id") or "b"))
        except ProjectError:
            raise
        except Exception as exc:            # noqa: BLE001 - показываем причину и ветку
            proposal = save_proposal(cfg, room_id, proposal)   # уже созданное не теряем
            raise ProjectError("Не удалось создать задачу по ветке «%s»: %s"
                               % (title, getattr(exc, "message_ru", None) or str(exc)[:200]),
                               getattr(exc, "code", "engine_error") or "engine_error", 502,
                               {"branch_id": branch.get("id"), "created": created}) from exc
        child_id = str((res or {}).get("root_id") or (res or {}).get("id") or "")
        if not child_id:
            proposal = save_proposal(cfg, room_id, proposal)
            raise ProjectError("Движок не вернул id задачи по ветке «%s»." % title,
                               "engine_error", 502, {"branch_id": branch.get("id")})
        branch["task_id"] = child_id
        record = _child_record(rec, branch, slug, title, body)
        record["root_id"] = child_id
        try:
            _registry().add_request(cfg, room_id, record)
        except Exception as exc:            # noqa: BLE001 - карточка уже есть, реестр догонит
            warnings.append("задача «%s» создана, но реестр не записался: %s"
                            % (title, str(exc)[:120]))
        try:
            link_child(cfg, room_id, root_id, child_id)
        except Exception as exc:            # noqa: BLE001 - связь восстановится повторным apply
            warnings.append("задача «%s» создана, но связь проекта не записалась: %s"
                            % (title, str(exc)[:120]))
        proposal = save_proposal(cfg, room_id, proposal)
        created.append({"branch_id": branch.get("id"), "task_id": child_id, "title": title,
                        "assignee": assignee})
        if isinstance(res, dict) and res.get("warning"):
            warnings.append(str((res.get("warning") or {}).get("message") or "")[:200])
    all_yes = all(b.get("decision") == "yes" for b in proposal.get("branches") or [])
    proposal["state"] = "applied"
    proposal["resolution"] = {"action": "accept_all" if all_yes else "partial",
                              "text": text, "at": _now()}
    proposal = save_proposal(cfg, room_id, proposal)
    message = ("Создано задач: %d." % len(created)) if created else \
        "Все задачи по веткам уже созданы раньше."
    return {"proposal": proposal, "created": created, "duplicate": not created,
            "warnings": [w for w in warnings if w], "message": message}


def _created_view(proposal: dict) -> list[dict]:
    return [{"branch_id": b.get("id"), "task_id": b.get("task_id"), "title": b.get("title")}
            for b in proposal.get("branches") or [] if b.get("task_id")]


# ---------------------------------------------------------------- чтение для страницы
def stage_of(status) -> str:
    """Стадия задачи по статусу карточки движка (§3: русские подписи — на клиенте)."""
    return _STAGE_OF.get(str(status or ""), "new")


def view(cfg, room, root_id: str) -> dict:
    """``project`` деталей задачи: родитель и подзадачи (название, статус, стадия)."""
    room_id = _room_id(room)
    node = project_of(cfg, room_id, root_id)
    children = node.get("children") or []
    out = {"parent_id": node.get("parent"), "children": []}
    if not children:
        return out
    board = _board(room)
    titles: dict = {}
    states: dict = {}
    try:
        roots = _reader().list_roots(cfg, board, tenant=_tenant(room), limit=200) or []
        titles = {str(r.get("id")): str(r.get("title") or "") for r in roots if isinstance(r, dict)}
    except Exception:                       # noqa: BLE001 - название вторично
        titles = {}
    try:
        states = _reader().request_states(cfg, board, children) or {}
    except Exception:                       # noqa: BLE001 - статус вторичен
        states = {}
    for child in children:
        state = states.get(child) or {}
        status = str(state.get("status") or "")
        out["children"].append({"id": child, "title": titles.get(child) or child,
                                "status": status, "stage": stage_of(status)})
    return out


def _tenant(room) -> str:
    if not isinstance(room, dict):
        return ""
    return str(room.get("tenant") or ("room:%s" % (room.get("id") or "")))


def attach(cfg, room, rec: dict, detail: dict, thread=None) -> None:
    """Добавить в детали задачи `proposal` и `project` (W30 §2). Сбой страницу не роняет."""
    if not isinstance(detail, dict):
        return
    root_id = str(detail.get("id") or (rec or {}).get("root_id") or "")
    if not root_id:
        return
    try:
        proposal = sync(cfg, room, root_id, thread=thread or detail.get("thread"))
    except Exception:                       # noqa: BLE001
        proposal = load_proposal(cfg, _room_id(room), root_id)
    detail["proposal"] = proposal
    try:
        detail["project"] = view(cfg, room, root_id)
    except Exception:                       # noqa: BLE001
        detail["project"] = {"parent_id": None, "children": []}


# ---------------------------------------------------------------- итоги подзадач (свод)
def _one_line(value, limit: int = 160) -> str:
    return _text(value, limit)


def _child_result(cfg, room, board: str, child_id: str) -> dict:
    """Заголовок, статус и вывод одной подзадачи (только чтение, без вызова моделей)."""
    entry = {"id": child_id, "title": child_id, "status": "", "text": ""}
    try:
        detail = _reader().request_detail(cfg, board, child_id, room) or {}
    except Exception:                       # noqa: BLE001 - одна подзадача не прячет остальные
        detail = {}
    synth = detail.get("synthesis") if isinstance(detail.get("synthesis"), dict) else {}
    meta = (synth.get("metadata") or {}).get("svod") if isinstance(synth.get("metadata"), dict) else {}
    entry["title"] = _text(detail.get("title_short") or detail.get("title"), MAX_TITLE) or child_id
    entry["status"] = str(detail.get("status") or "")
    text = _one_line((meta or {}).get("short") or (meta or {}).get("headline"))
    if not text:
        text = _one_line(synth.get("text_md"), 200)
    entry["text"] = text
    return entry


def child_results_text(cfg, room, root_id: str) -> str:
    """«Итоги подзадач» для контекста пересборки свода родителя (по строке на ветку)."""
    room_id = _room_id(room)
    children = project_of(cfg, room_id, root_id).get("children") or []
    if not children:
        return ""
    board = _board(room)
    lines = ["ИТОГИ ПОДЗАДАЧ (проект: %s):" % (root_id)]
    for child_id in children[:8]:
        entry = _child_result(cfg, room, board, child_id)
        status = STATUS_RU.get(entry["status"], entry["status"] or "неизвестно")
        line = "- «%s» — %s" % (entry["title"], status)
        if entry["text"]:
            line += ": %s" % entry["text"]
        lines.append(line)
    text = "\n".join(lines)
    return text[:1200]


# ---------------------------------------------------------------- «Разбить на задачи»
def split(cfg, room, rec: dict, root_id: str, board: str = "") -> dict:
    """Карточка руководителю «подготовь предложение задач» (W30 §2, только по кнопке).

    Идемпотентно: одна карточка на задачу — повтор нажатия возвращает прежнюю.
    """
    room_id = _room_id(room)
    board = board or _board(room)
    lead = _text((room or {}).get("orchestrator"), 64) if isinstance(room, dict) else ""
    if not lead:
        raise ProjectError("В комнате не выбран руководитель — предложение подготовить некому.",
                           "no_leader", 409)
    existing = _split_card_id(rec)
    if existing:
        return {"task_id": existing, "duplicate": True, "created": False,
                "message": "Предложение задач уже запрошено — карточка у руководителя."}
    proposal = get(cfg, room, root_id)
    if proposal and proposal.get("state") != "applied":
        return {"task_id": None, "duplicate": True, "created": False,
                "message": "Предложение уже есть — посмотрите карточку «Предложение руководителя»."}
    blocked = _guard().check(cfg, board)
    if blocked:
        raise ProjectError(str(blocked.get("message") or "Запись в доску отклонена"),
                           str(blocked.get("code") or "sandbox_real_board"), 409,
                           dict(blocked.get("detail") or {}))
    team = ""
    if isinstance(room, dict):
        members = [str((m or {}).get("profile") or "") for m in room.get("members") or []
                   if isinstance(m, dict)]
        team = ", ".join([p for p in members if p][:8])
    title = rule.split_card_title()
    body = rule.split_request_body(_text(rec.get("title"), MAX_TITLE), team)
    key = "split-%s" % root_id
    try:
        card_id = _engine().create_child_card(cfg, room, board, root_id, lead, title, body,
                                              "split", idem_key=key)
    except Exception as exc:                # noqa: BLE001
        reason = getattr(exc, "message_ru", None) or str(exc)[:200]
        code = str(getattr(exc, "code", "") or "engine_error")
        if code in ("bad_request", "validation", "not_found"):
            # «Профиля нет в ростере Hermes», «не указан исполнитель» — это настройка
            # комнаты, а не сбой движка: человеку нужен 409 и причина, а не 5xx.
            raise ProjectError(
                "Карточку руководителю не поставить: %s Профиль руководителя комнаты — «%s»; "
                "проверьте его в ростере Hermes (настройки комнаты)." % (reason, lead),
                code, 409, {"profile": lead}) from exc
        raise ProjectError("Не удалось поставить карточку руководителю: %s" % reason,
                           code, 502) from exc
    _remember_split(cfg, room_id, root_id, card_id)
    return {"task_id": card_id, "duplicate": False, "created": True,
            "message": "Карточка руководителю поставлена: он предложит ветки, вы утвердите."}


def _split_card_id(rec: dict) -> str:
    cards = (rec or {}).get("extra_cards") or []
    for card in cards:
        if not isinstance(card, dict):
            continue
        if str(card.get("kind") or "") == "split" and TASK_ID_RE.fullmatch(str(card.get("id") or "")):
            return str(card.get("id"))
    return ""


def _remember_split(cfg, room_id: str, root_id: str, card_id: str) -> None:
    try:
        _registry().add_extra_card(cfg, room_id, root_id,
                                   {"id": card_id, "kind": "split", "at": _now(),
                                    "note": "предложение задач по просьбе человека"})
    except Exception:                       # noqa: BLE001 - карточка уже создана
        pass


__all__ = ["ProjectError", "KINDS", "STATE_RU", "RESOLVE_ACTIONS", "proposals_dir",
           "proposal_path", "projects_path", "load_proposal", "save_proposal", "load_projects",
           "save_projects", "link_child", "project_of", "children_ids", "parse_comment", "sync", "get", "ready",
           "decide", "resolve", "stage_of", "view", "attach", "child_results_text", "split",
           "STATUS_RU"]
