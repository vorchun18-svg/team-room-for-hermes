"""Особые указания участникам комнаты — добавка к SOUL.md ТОЛЬКО для этой комнаты.

Требование Михаила 29.10.2026 (docs/WAVE9-RULES.md §9). Файлы профилей
(SOUL.md, config.yaml, skills) НЕ меняются никогда: указания живут в реестре
комнат (`data/rooms.json`, поле `agent_notes`) и доходят до агента двумя каналами:

1. блок «Особые указания участникам этой комнаты» в теле карточки
   (`context_pack.build_context_block`, общий лимит раздела 3 000 знаков);
2. автокомментарий `[Указание комнаты · <hash8>] <текст>` на карточки дерева
   запроса (`ensure_comments`, вызывается из watcher лениво и идемпотентно).

Публичные функции::

    validate(value, members=None) -> (notes, warnings)     # строгая проверка (API)
    normalize(value) -> notes                              # мягкое чтение реестра
    notes_of(room) -> {профиль: {text, updated_at}}
    participants(room) -> [(профиль, роль по-русски)]
    get(cfg, room_id, profile) -> {profile, text, updated_at}
    set(cfg, room_id, profile, text) -> {...}              # запись в реестр комнат
    render_block(cfg, room, limit=3000) -> str             # раздел для карточки
    comment_text(profile, text, *, updated=False) -> str   # одна строка с маркером
    ensure_comments(cfg, room, root_id) -> {written, skipped, errors}

Свойства: ничего не пишем в файлы профилей; комментарии — только через
`engine_write` (CLI `hermes kanban`), только для карточек дерева запроса
комнаты; ошибки никогда не роняют watcher.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

try:                                    # import server.agent_notes
    from . import statefiles as sf      # noqa: F401  (единый стиль с соседями)
except ImportError:                      # python server/app.py (плоский импорт)
    import statefiles as sf              # type: ignore  # noqa: F401

#: публичная функция `set` (договорённость §9) занимает имя — конструктор множеств прячем
_new_set = set

MAX_TEXT = 8000                 # знаков в указаниях одного участника
SECTION_LIMIT = 3000            # знаков на раздел в теле карточки (WAVE9 §9)
MAX_SECTION_ENTRY = 700         # знаков на одного участника внутри раздела
MAX_CARDS_PER_RUN = 12          # сколько карточек обходим за один заход
MARKER_PREFIX = "[Указание комнаты · "
UPDATED_MARK = "(обновлено)"
TITLE = "Особые указания участникам этой комнаты"
RULE = ("Руководитель при раздаче подзадач обязан переписать указание участника "
        "в тело его карточки. Указания комнаты дополняют твою роль и не отменяют "
        "правил безопасности и запретов.")
ROLE_RU = {"orchestrator": "руководитель комнаты", "critic": "критик",
           "worker": "исполнитель", "observer": "наблюдатель"}

#: чем пугаем, если в тексте похоже на секрет (маскируем, но не блокируем)
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"(?i)\b(?:password|passwd|пароль|токен|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
)

#: единая маска для ВСЕХ внешних выводов указаний (Vault, CONTEXT.md, комментарии, карточка).
#: В реестре комнаты текст хранится как есть — маскируем только на выходе.
SECRET_MASK = "[скрыто]"
_MASK_RULES = (
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}"), SECRET_MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}"), SECRET_MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"), SECRET_MASK),
    (re.compile(r"(?i)((?:password|passwd|пароль|токен|token|secret|api[_-]?key)\s*[:=]\s*)\S+"),
     r"\1" + SECRET_MASK),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 " + SECRET_MASK),
)
_WS_RE = re.compile(r"\s+")
_MARKER_RE = re.compile(r"\[Указание комнаты · ([0-9a-f]{8})\]")


def mask_secrets(text) -> str:
    """Спрятать похожее на ключ/пароль: ``sk-…``, ``ghp_…``, ``password=…``, ``Bearer …``.

    Одна маска на все внешние выводы указаний — в Vault, CONTEXT.md, комментарии
    карточки и тело запроса. Текст в реестре комнаты не меняется.
    """
    out = "" if text is None else str(text)
    for pattern, repl in _MASK_RULES:
        out = pattern.sub(repl, out)
    return out


class NotesError(Exception):
    """Ошибка указаний: code = bad_request|not_found (роут отдаст 4xx по-русски)."""

    def __init__(self, message: str, code: str = "bad_request", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _now() -> int:
    return int(time.time())


def _profile(value) -> str:
    return str(value or "").strip()


def _one_line(text) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


# ---------------------------------------------------------------- проверка
def secret_warnings(text) -> list[str]:
    """Предупреждения о похожем на секрет (маска в тексте, не блокировка)."""
    raw = str(text or "")
    found: list[str] = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.findall(raw):
            head = str(match).split(":", 1)[0].split("=", 1)[0].strip()
            masked = ("%s…" % head[:6]) if head and head.lower() not in ("password", "passwd",
                                                                      "пароль", "токен", "token",
                                                                      "secret", "api_key", "apikey") \
                else "%s=…" % (head or "секрет")
            if masked not in found:
                found.append(masked)
    if not found:
        return []
    return ["Похоже на ключ или пароль (%s). В указаниях лучше не хранить секреты — "
            "они попадут в карточки и в Obsidian." % ", ".join(found[:4])]


def validate(value, members=None) -> tuple[dict, list[str]]:
    """Строгая проверка поля `agent_notes` (API): профили — из состава комнаты.

    Возвращает `(notes, warnings)`; `notes = {профиль: {text, updated_at}}`.
    Пустой текст участника — указаний нет (запись удаляется). Ошибки — `NotesError`
    с русским сообщением и кодом `bad_request` (роут отдаст 400, не 500).
    """
    if value in (None, ""):
        return {}, []
    if not isinstance(value, dict):
        raise NotesError("Особые указания: нужен объект «профиль → текст»", "bad_request",
                         {"field": "agent_notes", "type": type(value).__name__})
    allowed = None
    if members is not None:
        allowed = {_profile(p) for p in members if _profile(p)}
    notes: dict[str, dict] = {}
    warnings: list[str] = []
    for raw_profile, raw_value in value.items():
        profile = _profile(raw_profile)
        if not profile:
            raise NotesError("Особые указания: пустое имя профиля", "bad_request",
                             {"field": "agent_notes"})
        if allowed is not None and profile not in allowed:
            raise NotesError(
                "Указания можно задать только участнику комнаты: «%s» в составе нет" % profile,
                "bad_request",
                {"field": "agent_notes", "profile": profile, "members": sorted(allowed)},
            )
        text = ""
        updated = 0
        if isinstance(raw_value, dict):
            text = raw_value.get("text")
            updated = raw_value.get("updated_at") or 0
        elif isinstance(raw_value, str):
            text = raw_value
        elif raw_value is not None:
            raise NotesError("Особые указания «%s»: нужен текст строкой" % profile,
                             "bad_request", {"field": "agent_notes", "profile": profile})
        text = "" if text is None else str(text)
        if "\x00" in text:
            raise NotesError("В указаниях «%s» есть непечатаемый символ" % profile,
                             "bad_request", {"field": "agent_notes", "profile": profile})
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        if len(text) > MAX_TEXT:
            raise NotesError(
                "Указания «%s»: %d знаков, а можно не больше %d. Сократите текст."
                % (profile, len(text), MAX_TEXT), "bad_request",
                {"field": "agent_notes", "profile": profile, "length": len(text), "limit": MAX_TEXT})
        try:
            stamp = int(updated)
        except (TypeError, ValueError):
            stamp = 0
        notes[profile] = {"text": text, "updated_at": stamp or _now()}
        warnings.extend(secret_warnings(text))
    return notes, warnings


def normalize(value) -> dict:
    """Мягкое чтение реестра: мусор и чужие профили не роняют список комнат."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for raw_profile, raw_value in value.items():
        profile = _profile(raw_profile)
        if not profile:
            continue
        if isinstance(raw_value, dict):
            text, stamp = raw_value.get("text"), raw_value.get("updated_at") or 0
        elif isinstance(raw_value, str):
            text, stamp = raw_value, 0
        else:
            continue
        text = "" if text is None else str(text)
        if not text.strip():
            continue
        try:
            stamp = int(stamp)
        except (TypeError, ValueError):
            stamp = 0
        out[profile] = {"text": text[:MAX_TEXT], "updated_at": stamp}
    return out


def notes_of(room) -> dict:
    """Указания комнаты в канонической форме (нет поля — пусто)."""
    return normalize((room or {}).get("agent_notes") if isinstance(room, dict) else {})


def participants(room) -> list[tuple[str, str]]:
    """Состав комнаты по порядку: руководитель, критик, исполнители (без дублей)."""
    room = room if isinstance(room, dict) else {}
    out: list[tuple[str, str]] = []
    seen: set[str] = _new_set()
    lead = _profile(room.get("orchestrator"))
    if lead:
        out.append((lead, ROLE_RU["orchestrator"]))
        seen.add(lead)
    critic = _profile(room.get("critic"))
    if critic and critic not in seen:
        out.append((critic, ROLE_RU["critic"]))
        seen.add(critic)
    for member in room.get("members") or []:
        if not isinstance(member, dict):
            continue
        profile = _profile(member.get("profile"))
        if not profile or profile in seen:
            continue
        seen.add(profile)
        out.append((profile, ROLE_RU.get(str(member.get("role") or "worker"), "участник")))
    return out


def role_ru(room, profile) -> str:
    name = _profile(profile)
    for item, role in participants(room):
        if item == name:
            return role
    return "участник"


# ---------------------------------------------------------------- доступ к реестру
def _rooms():
    try:
        from . import rooms as mod
    except ImportError:  # pragma: no cover - плоский импорт
        import rooms as mod  # type: ignore
    return mod


def get(cfg, room_id, profile) -> dict:
    """Указания одного участника: `{profile, role, text, updated_at}`."""
    room = _rooms().get_room(cfg, str(room_id)) or {}
    if not room:
        raise NotesError("Комната не найдена", "not_found", {"room": str(room_id)})
    name = _profile(profile)
    notes = notes_of(room)
    item = notes.get(name) or {"text": "", "updated_at": 0}
    return {"profile": name, "role": role_ru(room, name), "text": item["text"],
            "updated_at": int(item.get("updated_at") or 0), "agent_notes": notes}


def set(cfg, room_id, profile, text) -> dict:      # noqa: A001 - имя по договорённости §9
    """Записать (или очистить) указания участника. Файлы профилей не трогаем."""
    room = _rooms().get_room(cfg, str(room_id))
    if not room:
        raise NotesError("Комната не найдена", "not_found", {"room": str(room_id)})
    name = _profile(profile)
    if not name:
        raise NotesError("Не указан профиль участника", "bad_request", {"field": "profile"})
    members = [p for p, _role in participants(room)]
    if name not in members:
        raise NotesError(
            "«%s» не в составе комнаты — указания задаются только участникам" % name,
            "bad_request", {"field": "profile", "profile": name, "members": members})
    notes = notes_of(room)
    if text is None or not str(text).strip():
        notes.pop(name, None)
        cleared = True
    else:
        clean, _warnings = validate({name: {"text": text}}, members=[name])
        notes.update(clean)
        cleared = False
    updated = _rooms().update_room(cfg, str(room_id), {"agent_notes": notes})
    fresh = notes_of(updated if isinstance(updated, dict) else room)
    item = fresh.get(name) or {"text": "", "updated_at": 0}
    return {"profile": name, "role": role_ru(room, name), "text": item["text"],
            "updated_at": int(item.get("updated_at") or 0), "cleared": cleared,
            "agent_notes": fresh, "warnings": secret_warnings(item["text"])}


# ---------------------------------------------------------------- раздел для карточки
def _vault_hint(cfg, room_id) -> str:
    """Путь к полному тексту в Obsidian (для пометки об усечении)."""
    try:
        from . import config as config_mod
        folder = Path(config_mod.room_vault_dir(cfg, room_id))
    except Exception:  # noqa: BLE001 - подсказка не должна ронять блок
        try:
            folder = Path(cfg.vault_dir) / str(room_id)
        except Exception:  # noqa: BLE001
            return ""
    return str(folder / "Указания участникам.md")


def render_block(cfg, room, limit: int = SECTION_LIMIT) -> str:
    """Раздел «Особые указания участникам этой комнаты» (≤ limit знаков) или ''.

    Пусто — если указаний нет вовсе (тогда и раздел в карточке не появляется).
    Длинный текст обрезается «…» с пометкой, где лежит полный (Vault).
    """
    room = room if isinstance(room, dict) else {}
    notes = notes_of(room)
    if not notes:
        return ""
    try:
        limit = max(400, int(limit))
    except (TypeError, ValueError):
        limit = SECTION_LIMIT
    room_id = str(room.get("id") or "")
    hint = _vault_hint(cfg, room_id) if room_id else ""
    tail = RULE
    budget = max(200, limit - len(tail) - 2)
    lines = [TITLE + ":"]
    used = len(lines[0]) + 1
    cut = False
    for profile, role in participants(room):
        item = notes.get(profile)
        if not item:
            continue
        text = _one_line(mask_secrets(item.get("text")))
        if not text:
            continue
        entry = "- %s (%s): %s" % (role, profile, text)
        if len(entry) > MAX_SECTION_ENTRY:
            entry = entry[: MAX_SECTION_ENTRY - 1].rstrip() + "…"
            cut = True
        if used + len(entry) + 1 > budget:
            # Место кончилось: не теряем участника целиком — оставляем начало текста.
            available = budget - used - 1
            cut = True
            if available >= 120:
                entry = entry[: available - 1].rstrip() + "…"
            else:
                continue
        lines.append(entry)
        used += len(entry) + 1
    if cut:
        note = "…полный текст — в Vault"
        if hint:
            note += ": %s" % hint
        if used + len(note) + 2 <= budget:
            lines.append(note)
    lines.append("")
    lines.append(tail)
    text = "\n".join(lines)
    if len(text) > limit:                     # страховка: правило не теряем
        keep = max(0, limit - len(tail) - 2)
        text = text[:keep].rstrip() + "\n" + tail
    return text


# ---------------------------------------------------------------- автокомментарии
def comment_hash(profile, text) -> str:
    """hash8 указания: маркер меняется вместе с текстом (идемпотентность)."""
    raw = "%s|%s" % (_profile(profile), _one_line(text))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def comment_text(profile, text, *, updated: bool = False) -> str:
    """Одна строка комментария: `[Указание комнаты · hash8] <текст>` (секреты маскируются)."""
    marker = "%s%s]" % (MARKER_PREFIX, comment_hash(profile, text))
    body = _one_line(mask_secrets(text))
    if updated:
        return "%s %s %s" % (marker, UPDATED_MARK, body)
    return "%s %s" % (marker, body)


def _board_of(cfg, room) -> str:
    return str((room or {}).get("board") or getattr(cfg, "board", "") or "team")


def _connect(cfg, board: str):
    """Чтение доски: sqlite mode=ro (писать в БД приложение не имеет права)."""
    try:
        db = Path(cfg.kanban_db(board))
    except Exception:  # noqa: BLE001 - доски нет/путь не тот
        return None
    if not db.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=3)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql: str, params) -> list:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _tree_ids(cfg, board: str, root_id: str) -> set:
    try:
        from .engine_write_guard import tree_ids
    except ImportError:  # pragma: no cover
        from engine_write_guard import tree_ids  # type: ignore
    try:
        return _new_set(tree_ids(cfg, board, root_id))
    except Exception:  # noqa: BLE001 - чужая утилита не должна ронять проход
        return {str(root_id)}


def _cards(con, ids: list) -> dict:
    """`{id: {assignee, status}}` по карточкам дерева запроса."""
    if not ids:
        return {}
    rows = _rows(con, "SELECT id, assignee, status FROM tasks WHERE id IN (%s)" % _marks(ids), ids)
    return {str(r["id"]): {"assignee": str(r["assignee"] or ""), "status": str(r["status"] or "")}
            for r in rows}


def _markers(con, ids: list) -> dict:
    """`{task_id: set(hash8)}` — какие указания уже стоят комментариями."""
    if not ids:
        return {}
    rows = _rows(con, "SELECT task_id, body FROM task_comments WHERE task_id IN (%s) "
                      "AND body LIKE ?" % _marks(ids), list(ids) + [MARKER_PREFIX + "%"])
    out: dict[str, set] = {}
    for row in rows:
        found = _new_set(_MARKER_RE.findall(str(row["body"] or "")))
        if found:
            out.setdefault(str(row["task_id"]), _new_set()).update(found)
    return out


def ensure_comments(cfg, room, root_id, *, limit: int = MAX_CARDS_PER_RUN) -> dict:
    """Дописать указания комментариями на карточки дерева запроса (идемпотентно).

    Одна строка на карточку, лениво: только карточки дерева ЭТОГО запроса, чей
    исполнитель есть в составе с указаниями; уже стоящий маркер с тем же hash8
    повторно не пишется; при изменившемся тексте — новый комментарий «(обновлено)».
    Ошибки собираются в `errors` и наружу не бросаются.
    """
    result = {"written": [], "skipped": 0, "errors": []}
    room = room if isinstance(room, dict) else {}
    notes = notes_of(room)
    if not notes:
        return result
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return result
    root = str(root_id or "")
    if not root:
        return result
    board = _board_of(cfg, room)
    con = _connect(cfg, board)
    if con is None:
        return result
    try:
        ids = sorted(_tree_ids(cfg, board, root))
        cards = _cards(con, ids)
        markers = _markers(con, ids)
    finally:
        con.close()
    try:
        try:
            from . import engine_write as ew
        except ImportError:  # pragma: no cover
            import engine_write as ew  # type: ignore
    except Exception as exc:  # noqa: BLE001 - модуль записи может быть не готов
        result["errors"].append(str(exc)[:200])
        return result
    written = 0
    for card_id, card in sorted(cards.items()):
        if written >= max(1, int(limit)):
            break
        if card.get("status") == "archived":
            continue
        profile = _profile(card.get("assignee"))
        item = notes.get(profile)
        if not profile or not item:
            continue
        text = _one_line(item.get("text"))
        digest = comment_hash(profile, text)
        seen = markers.get(card_id) or _new_set()
        if digest in seen:
            result["skipped"] += 1
            continue
        body = comment_text(profile, text, updated=bool(seen))
        try:
            ew.act(cfg, board, root, "comment", text=body, task_id=card_id, author=profile)
        except Exception as exc:  # noqa: BLE001 - один комментарий не валит проход
            result["errors"].append("%s: %s" % (card_id, str(exc)[:160]))
            continue
        result["written"].append(card_id)
        written += 1
    return result


__all__ = ["MAX_TEXT", "SECTION_LIMIT", "MARKER_PREFIX", "TITLE", "RULE", "ROLE_RU", "SECRET_MASK",
           "NotesError", "validate", "normalize", "notes_of", "participants", "role_ru",
           "secret_warnings", "mask_secrets", "get", "set", "render_block", "comment_hash",
           "comment_text", "ensure_comments"]
