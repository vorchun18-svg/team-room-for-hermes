"""Зеркало комнаты в Vault: STATE.md, Журнал.md, заметки запросов и CONTEXT.md.

Часть 1 (волна 7) — ``_state_md`` / ``_journal_md``: чистое зеркало состояния комнаты,
пишет ``ingest.write_vault_mirror``.

Часть 2 (волна 8, Блок C): ``sync_request`` / ``sync_room`` / ``context_summary`` —
Obsidian-слой по умолчанию: заметка на каждый запрос
(``Запросы/<YYYY-MM-DD> <название> (<id>).md``) и автообновляемый ``CONTEXT.md`` комнаты.
Служебные блоки лежат между маркерами ``<!-- teamroom:begin NAME -->…<!-- teamroom:end -->``;
всё, что Михаил дописал руками ВНЕ маркеров, сохраняется. Запись атомарная (tmp+replace,
повтор при PermissionError), только внутри ``cfg.vault_dir``. В мок/демо не пишем ничего.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from pathlib import Path

from .ingest_common import MONTHS_NOM, STATUS_RU, _as_int, _clean
from .config import safe_dir_name
from . import registry

def _fmt_date(ts) -> str:
    ts = _as_int(ts)
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def _fmt_due(value) -> str:
    """Дата срока: ISO `YYYY-MM-DD` → DD.MM.YYYY; пустое → ''."""
    raw = _clean(value)
    if not raw:
        return ""
    try:
        return _dt.date.fromisoformat(raw).strftime("%d.%m.%Y")
    except ValueError:
        return raw


def _fmt_dt(ts) -> str:
    ts = _as_int(ts)
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def _cell(text) -> str:
    return _clean(text).replace("|", "\\|")


def _latest_at(state: dict) -> int:
    at = _as_int(((state.get("roadmap") or {}).get("updated_at")))
    at = max(at, _as_int(((state.get("backlog") or {}).get("updated_at"))))
    for decision in state.get("decisions") or []:
        at = max(at, _as_int(decision.get("updated_at")) or _as_int(decision.get("at")))
    for event in state.get("journal") or []:
        at = max(at, _as_int(event.get("at")))
    return at


def _link(cfg, room_id: str, root_id: str) -> str:
    return f"[{root_id}](#/room/{room_id}/request/{root_id})"


def _state_md(cfg, room_id: str, title: str, state: dict) -> str:
    roadmap = state.get("roadmap") or {}
    backlog = state.get("backlog") or {}
    decisions = state.get("decisions") or []
    requests = registry.list_requests(cfg, room_id, 10)
    latest = _latest_at(state)
    lines = [f"# {title} — состояние проекта", ""]
    stamp = f" по состоянию на {_fmt_date(latest)}" if latest else ""
    lines += [f"_Файл генерируется приложением «Комната команды»{stamp}. "
              "Правки руками будут перетёрты при следующем обновлении._", ""]

    current = roadmap.get("current") or {}
    lines += ["## Где мы сейчас", ""]
    if current:
        phase = next((p for p in roadmap.get("phases") or [] if p.get("id") == current.get("id")), {})
        lines.append(f"- **Фаза:** {current.get('title') or current.get('id')} — "
                     f"{STATUS_RU.get(current.get('status'), current.get('status'))}")
        if phase.get("note"):
            lines.append(f"- **Что происходит:** {_clean(phase['note'])}")
    else:
        lines.append("- Этапы ещё не описаны ни одним запросом.")
    lines.append("")

    lines += ["## Этапы проекта", "", "| Этап | Статус | Комментарий |", "| --- | --- | --- |"]
    for phase in roadmap.get("phases") or []:
        lines.append(f"| {phase.get('title') or phase.get('id')} "
                     f"| {STATUS_RU.get(phase.get('status'), phase.get('status') or '')} | {_cell(phase.get('note'))} |")
    lines.append("")

    lines += ["## Решения", ""]
    accepted = [d for d in decisions if d.get("status") == "accepted"]
    proposed = [d for d in decisions if d.get("status") == "proposed"]
    if not accepted and not proposed:
        lines.append("Решений пока нет.")
    for label, group in (("Принято", accepted), ("Предложено", proposed)):
        if group:
            lines.append(f"**{label}**")
            for decision in group:
                why = f" — почему: {_clean(decision['why'])}" if decision.get("why") else ""
                source = decision.get("source_request")
                ref = f" ({_link(cfg, room_id, str(source))})" if source else ""
                lines.append(f"- {_clean(decision.get('text'))}{why}{ref}")
            lines.append("")

    inbox = (backlog.get("sections") or {}).get("inbox") or []
    lines += ["## Ждёт Михаила", ""]
    if not inbox:
        lines.append("Сейчас вопросов к тебе нет.")
    for item in inbox:
        due = f", срок {_fmt_due(item['due'])}" if item.get("due") else ""
        src = item.get("source_request")
        ref = f", {_link(cfg, room_id, str(src))}" if src else ""
        lines.append(f"- [ ] {_clean(item.get('text'))}{due}{ref}")
    lines.append("")

    steps = (backlog.get("sections") or {}).get("next_steps") or []
    lines += ["## Следующие шаги", ""]
    if not steps:
        lines.append("Следующих шагов пока нет.")
    for item in steps:
        owner = f" — владелец: {item['owner']}" if item.get("owner") else ""
        due = f", срок {_fmt_due(item['due'])}" if item.get("due") else ""
        lines.append(f"- [ ] {_clean(item.get('text'))}{owner}{due}")
    done_steps = [i for i in backlog.get("items") or []
                  if i.get("kind") == "next_step" and i.get("status") == "done"]
    if done_steps:
        lines += ["", f"Выполнено шагов: {len(done_steps)}."]
    lines.append("")

    future = (backlog.get("sections") or {}).get("future_questions") or []
    lines += ["## Будущие вопросы", ""]
    if not future:
        lines.append("Будущих вопросов пока нет.")
    for item in future:
        lines.append(f"- {_clean(item.get('text'))}"
                     + (f" (источник: {_link(cfg, room_id, str(item['source_request']))})"
                        if item.get("source_request") else ""))
    lines.append("")

    lines += ["## Последние запросы", ""]
    if not requests:
        lines.append("Запросов ещё не было.")
    for rec in requests:
        lines.append(f"- **{_clean(rec.get('title')) or rec.get('root_id')}** — "
                     f"{_fmt_date(rec.get('created_at')) or '—'}, карточка "
                     f"{_link(cfg, room_id, str(rec.get('root_id')))}")
    lines.append("")

    reports = state.get("reports") or []
    if reports:
        lines += ["## Отчёты", ""]
        for report in reports:
            lines.append(f"- {_clean(report.get('title')) or report.get('key')} — {report.get('path')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _journal_md(cfg, room_id: str, title: str, state: dict) -> str:
    events = state.get("journal") or []
    lines = [f"# Журнал комнаты «{title}»", "",
             "_Файл генерируется приложением «Комната команды»; правки руками будут перетёрты._", ""]
    if not events:
        lines += ["Событий пока нет.", ""]
        return "\n".join(lines)
    groups: dict[tuple[int, int], list[dict]] = {}
    for event in events:
        at = _as_int(event.get("at"))
        if not at:
            continue
        moment = _dt.datetime.fromtimestamp(at)
        groups.setdefault((moment.year, moment.month), []).append(event)
    for (year, month) in sorted(groups, reverse=True):
        lines += [f"## {MONTHS_NOM[month - 1]} {year}", ""]
        for event in sorted(groups[(year, month)], key=lambda e: (_as_int(e.get("at")), str(e.get("id"))), reverse=True):
            lines.append(f"- **{_fmt_dt(event.get('at'))}** — {_clean(event.get('text'))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- Блок C: Obsidian

NOTE_DIR = "Запросы"
CONTEXT_FILE = "CONTEXT.md"
CONTEXT_LIMIT = 6000            # знаков в CONTEXT.md (требование Блока C)
MAX_TITLE = 70                 # знаков названия запроса в имени файла
MAX_ID = 24                    # знаков короткого id в имени файла
MAX_NAME = 150                 # общий предел имени файла (Windows — 255 UTF-16)

#: зарезервированные имена Windows (файл с таким именем создать нельзя)
RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {"com%d" % i for i in range(1, 10)}
    | {"lpt%d" % i for i in range(1, 10)}
)

REQUEST_STATUS_RU = {
    "queued": "в очереди", "triage": "ждёт разбора", "scheduled": "запланирован",
    "todo": "ждёт зависимостей", "ready": "готов к работе", "running": "в работе",
    "working": "в работе", "review": "на проверке", "reviewing": "на проверке",
    "synthesizing": "собирает ответ", "blocked": "ждёт решения",
    "needs_you": "ждёт тебя", "done": "готово", "failed": "ошибка",
    "cancelled": "отменён", "archived": "в архиве",
}

_BLOCK_NAMES = ("head", "request", "dialog", "answer", "next", "questions", "decisions",
                "artifacts", "team", "links", "agent_notes")


# --- защита, имена и пути ---------------------------------------------------

def _writes_disabled(cfg) -> bool:
    """Мок/демо не пишут в Vault вообще (Блок C).

    Боевой сервер и песочница тестов пишут в ``cfg.vault_dir`` (в песочнице это
    временный каталог). Признаки мока: поле ``cfg.mock``/``cfg.demo`` или
    ``TEAMROOM_MOCK=1``.
    """
    if getattr(cfg, "mock", False) or getattr(cfg, "demo", False):
        return True
    if not getattr(cfg, "vault_enabled", True):
        return True                      # Vault не найден/выключен: зеркало не пишем
    return str(os.environ.get("TEAMROOM_MOCK") or "").strip().lower() in ("1", "true", "yes", "on")


def safe_stem(text, limit: int = MAX_TITLE) -> str:
    """Безопасное для Windows имя без расширения: без ``<>:"/\\|?*`` и управляющих."""
    raw = str(text or "")
    for ch in '<>:"/\\|?*':
        raw = raw.replace(ch, "-")
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    if not raw:
        raw = "Без названия"
    stem = raw[:limit].rstrip(" .")
    if not stem:
        stem = "Без названия"
    if stem.split(".")[0].strip().lower() in RESERVED_NAMES:
        stem = "_" + stem          # CON.md → _CON.md
    return stem


def _short_id(root_id) -> str:
    return safe_stem(root_id, MAX_ID) or "request"


def note_stem(root_id, title, created_at) -> str:
    """``<YYYY-MM-DD> <название> (<id>)`` — основа имени заметки запроса."""
    at = _as_int(created_at)
    try:
        day = _dt.datetime.fromtimestamp(at).strftime("%Y-%m-%d") if at else "0000-00-00"
    except (OverflowError, OSError, ValueError):
        day = "0000-00-00"
    stem = "%s %s (%s)" % (day, safe_stem(title), _short_id(root_id))
    return stem[:MAX_NAME].rstrip(" .")


def _inside(base: Path, target: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except (ValueError, OSError):
        return False


def _vault_base(cfg) -> Path:
    """Корень зеркала; создаётся при необходимости. Всё дальнейшее — только под ним."""
    base = Path(cfg.vault_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve(strict=True)


def _subdir(base: Path, *parts) -> Path:
    """Каталог под ``base``: проверка выхода за vault_dir до и после создания."""
    target = base
    for part in parts:
        target = target / part
    resolved = target.resolve(strict=False)
    if not _inside(base, resolved):
        raise ValueError("путь вне vault_dir: %s" % resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    resolved = resolved.resolve(strict=True)
    if not _inside(base, resolved):
        raise ValueError("путь вне vault_dir: %s" % resolved)
    return resolved


def _checked_target(base: Path, path: Path) -> Path:
    """Итоговая проверка файла: resolve внутри vault_dir + safe_resolve для существующих."""
    resolved = path.resolve(strict=False)
    if not _inside(base, resolved):
        raise ValueError("путь вне vault_dir: %s" % resolved)
    if resolved.exists():
        try:
            safe_resolve = _import("files").safe_resolve
        except ImportError:  # pragma: no cover - плоский импорт
            safe_resolve = None
        checked = safe_resolve(base, resolved) if safe_resolve else None
        if checked is not None:
            return checked
        # safe_resolve отказывает и по «опасным» именам (token/secret/…). Наши имена
        # уже прошли safe_stem и лежат внутри vault_dir — этого достаточно.
        if not resolved.is_file():
            raise ValueError("не файл: %s" % resolved)
    return resolved


def _read_text_retry(path: Path):
    """Прочитать файл; None — файла нет. PermissionError — 3 попытки (Windows)."""
    for attempt in range(3):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt >= 2:
                return None
            time.sleep(0.02 * (attempt + 1))
        except OSError:
            return None
    return None


def _atomic_write(path: Path, text: str) -> bool:
    """Атомарная запись UTF-8 (tmp+replace), повторы при занятом файле. False — без изменений."""
    data = text.encode("utf-8")
    try:
        current = path.read_bytes()
    except (FileNotFoundError, OSError):
        current = None
    if current == data:
        return False
    for attempt in range(6):
        tmp = path.with_name("%s.%s.%s.tmp" % (path.name, os.getpid(), attempt))
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp), str(path))
            return True
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            transient = (isinstance(exc, PermissionError)
                         or getattr(exc, "winerror", None) in (5, 32, 33)
                         or getattr(exc, "errno", None) in (11, 13, 16))
            if not transient or attempt >= 5:
                raise
            time.sleep(0.05 * (attempt + 1))
    return False


# --- маркеры и frontmatter --------------------------------------------------

_FRONT_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.S)


def _yaml(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _frontmatter(tags, fields) -> str:
    lines = ["---", "tags:"]
    for tag in tags:
        lines.append("  - " + _yaml(tag))
    for key, value in fields.items():
        if isinstance(value, int) and not isinstance(value, bool):
            lines.append("%s: %d" % (key, value))
        else:
            lines.append("%s: %s" % (key, _yaml(value)))
    lines += ["---", ""]
    return "\n".join(lines) + "\n"


def _strip_front(text: str) -> str:
    match = _FRONT_RE.match(text or "")
    return (text or "")[match.end():] if match else (text or "")


def _block(name: str, body: str) -> str:
    return "<!-- teamroom:begin %s -->\n%s\n<!-- teamroom:end -->" % (name, str(body or "").strip())


def _merge_blocks(existing_body: str, blocks) -> str:
    """Заменить служебные блоки; всё вне маркеров сохранить; устаревшие блоки убрать."""
    text = (existing_body or "").lstrip("\r\n")
    present = {name for name, _ in blocks}
    for name in _BLOCK_NAMES:
        if name in present:
            continue
        stale = re.compile(r"<!--\s*teamroom:begin\s+%s\s*-->.*?<!--\s*teamroom:end(?:\s+%s)?\s*-->"
                           % (re.escape(name), re.escape(name)), re.S)
        text = stale.sub("", text)
    for name, body in blocks:
        pattern = re.compile(r"<!--\s*teamroom:begin\s+%s\s*-->.*?<!--\s*teamroom:end(?:\s+%s)?\s*-->"
                             % (re.escape(name), re.escape(name)), re.S)
        block = _block(name, body)
        if pattern.search(text):
            text = pattern.sub(lambda _m, b=block: b, text, count=1)
        else:
            text = (text.rstrip() + "\n\n" + block + "\n") if text.strip() else block + "\n"
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.rstrip() + "\n"


def _compose(front: str, blocks) -> str:
    body = "\n\n".join(_block(name, body) for name, body in blocks)
    return (front + "\n" + body).strip() + "\n"


# --- общие данные -----------------------------------------------------------

def _import(name: str):
    """Ленивый импорт соседнего модуля с учётом подмены ``sys.modules`` в тестах."""
    import importlib
    package = __package__
    if package:
        try:
            return importlib.import_module("." + name, package)
        except ImportError:
            pass
    return importlib.import_module(name)


def _room(cfg, room_id: str) -> dict:
    try:
        rooms_mod = _import("rooms")
    except ImportError:  # pragma: no cover - плоский импорт
        return {"id": str(room_id), "title": str(room_id)}
    try:
        room = rooms_mod.get_room(cfg, room_id)
    except Exception:  # noqa: BLE001 - реестр комнат вторичен для заметки
        room = None
    return room or {"id": str(room_id), "title": str(room_id)}


def _state(cfg, room_id: str) -> dict:
    try:
        ingest_mod = _import("ingest")
    except ImportError:  # pragma: no cover
        return {}
    try:
        return ingest_mod.load_state(cfg, room_id) or {}
    except Exception:  # noqa: BLE001 - здание проекта может быть ещё пустым
        return {}


def _thread_items(cfg, room: dict, root_id: str) -> list:
    """Лента диалога из ``server.thread``; модуля нет/ошибка — пусто (ленивый импорт)."""
    try:
        thread_mod = _import("thread")
    except ImportError:  # pragma: no cover
        return []
    try:
        items = thread_mod.get_thread(cfg, root_id, room=room)
    except TypeError:
        try:
            items = thread_mod.get_thread(cfg, root_id)
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001 - диалог вторичен для заметки
        return []
    return [item for item in (items or []) if isinstance(item, dict)]


def _file_url(path) -> str:
    text = str(path or "").replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", text):
        return "file:///" + text
    return "file:///" + text.lstrip("/")


def _md_link(label: str, path) -> str:
    clean = _clean(label) or Path(str(path)).name or "файл"
    clean = clean.replace("[", "(").replace("]", ")")
    return "[%s](<%s>)" % (clean, _file_url(path))


# --- заметка запроса --------------------------------------------------------

def _request_sections(cfg, room: dict, root_id: str, detail: dict) -> list:
    title = _clean(detail.get("title")) or root_id
    status = str(detail.get("status") or "")
    created = _fmt_date(detail.get("created_at"))
    syn = detail.get("synthesis") if isinstance(detail.get("synthesis"), dict) else {}
    version = _as_int(syn.get("version"))
    room_id = str(room.get("id") or "")
    blocks = []

    head = ["# %s" % title, ""]
    head.append("**Статус:** %s%s" % (REQUEST_STATUS_RU.get(status, status or "неизвестно"),
                                     (" · **создан:** " + created) if created else ""))
    head.append("")
    head.append("**Комната:** [[CONTEXT|%s]] · **Карточка:** [%s](#/room/%s/request/%s)"
                % (str(room.get("title") or room_id), root_id, room_id, root_id))
    blocks.append(("head", "\n".join(head)))

    request = ["## Запрос", ""]
    text = str(detail.get("text") or "").strip()
    if text:
        request.append("\n".join(("> " + line) if line.strip() else ">" for line in text.splitlines()))
    else:
        request.append("_Текст запроса не сохранён._")
    attach = detail.get("attach") or detail.get("attachments") or []
    if isinstance(attach, (list, tuple)) and attach:
        request += ["", "**Вложения:**", ""]
        for item in attach:
            path = item.get("path") if isinstance(item, dict) else item
            label = (item.get("title") if isinstance(item, dict) else None) or Path(str(path)).name
            if path:
                request.append("- " + _md_link(label, path))
    blocks.append(("request", "\n".join(request)))

    items = _thread_items(cfg, room, root_id)
    dialog = ["## Диалог", ""]
    if not items:
        dialog.append("Пока сообщений нет.")
    for item in items:
        at = _fmt_dt(item.get("at"))
        who = _clean(item.get("author_label") or item.get("author") or "")
        body = str(item.get("text") or "").strip().replace("\r\n", "\n")
        body = "\n".join(("  " + line) if line.strip() else "" for line in body.split("\n")).strip()
        if item.get("kind") == "event":
            dialog.append("_%s%s_ — %s" % (at, (" · " + who) if who else "", body))
        else:
            dialog.append("- **%s · %s** — %s" % (at, who or "команда", body))
        dialog.append("")
    blocks.append(("dialog", "\n".join(dialog).rstrip()))

    answer = ["## Ответ", ""]
    latest = str(syn.get("text_md") or syn.get("text") or "").strip()
    if syn.get("state") == "pending":
        answer.append("_Ответ ещё готовится._")
    if latest:
        if version:
            answer.append("**Версия %d** · %s%s" % (version, _clean(syn.get("author")) or "команда",
                                                   (" · " + _fmt_dt(syn.get("at"))) if syn.get("at") else ""))
            answer.append("")
        answer.append(latest)
    elif syn.get("state") != "pending":
        answer.append("_Ответа пока нет._")
    history = [h for h in (syn.get("history") or [])
               if isinstance(h, dict) and _as_int(h.get("version")) != version]
    for old in sorted(history, key=lambda h: _as_int(h.get("version")), reverse=True):
        number = _as_int(old.get("version"))
        if not number:
            continue
        meta = " · ".join(part for part in (
            _clean(old.get("author")),
            {"initial": "первый вариант", "revision": "доработка", "critic": "правка критика"}.get(
                str(old.get("kind") or ""), ""),
            _fmt_dt(old.get("at")),
        ) if part)
        answer += ["", "### Версия %d" % number, ""]
        if meta:
            answer += ["_" + meta + "_", ""]
        if old.get("note"):
            answer += [_clean(old.get("note")), ""]
        old_text = str(old.get("text_md") or old.get("text") or "").strip()
        if old_text:
            answer.append(old_text)
    blocks.append(("answer", "\n".join(answer).strip()))

    steps = [s for s in (detail.get("next_steps") or []) if isinstance(s, dict)]
    nxt = ["## Дальше", ""]
    if not steps:
        nxt.append("Шагов пока нет.")
    for step in steps:
        mark = "x" if str(step.get("state")) in ("done", "completed") else " "
        tail = []
        if step.get("owner"):
            tail.append("владелец: %s" % _clean(step.get("owner")))
        due = _fmt_due(step.get("due"))
        if due:
            tail.append("срок: %s" % due)
        nxt.append("- [%s] %s%s" % (mark, _clean(step.get("text")),
                                   (" — " + ", ".join(tail)) if tail else ""))
    blocks.append(("next", "\n".join(nxt)))

    questions = ["## Вопросы к Михаилу", ""]
    asks = [q for q in (detail.get("open_questions") or []) if isinstance(q, dict)]
    if not asks:
        questions.append("Открытых вопросов нет.")
    for question in asks:
        line = "- %s" % _clean(question.get("text"))
        if question.get("for"):
            line += " — кому: %s" % _clean(question.get("for"))
        if question.get("answered"):
            line += " — **отвечено**"
            if question.get("answer"):
                line += ": %s" % _clean(question.get("answer"))
        questions.append(line)
    future = [str(x) for x in (detail.get("future_questions") or []) if str(x).strip()]
    if future:
        questions += ["", "**На будущее:**", ""]
        questions += ["- %s" % _clean(x) for x in future]
    blocks.append(("questions", "\n".join(questions)))

    decisions = ["## Решения", ""]
    made = [d for d in (detail.get("decisions") or []) if isinstance(d, dict)]
    if not made:
        decisions.append("Решений по запросу пока нет.")
    for decision in made:
        label = {"accepted": "принято", "proposed": "предложено",
                 "rejected": "отклонено"}.get(str(decision.get("status") or ""), "")
        line = "- %s" % _clean(decision.get("text"))
        if label:
            line += " — *%s*" % label
        if decision.get("why"):
            line += "; почему: %s" % _clean(decision.get("why"))
        decisions.append(line)
    blocks.append(("decisions", "\n".join(decisions)))

    artifacts = ["## Артефакты", ""]
    project_dir = str(room.get("project_dir") or "").strip()
    if project_dir:
        artifacts += ["**Папка проекта:** %s" % _md_link(project_dir, project_dir), ""]
    files = [a for a in (detail.get("artifacts") or []) if isinstance(a, dict) and a.get("path")]
    if not files:
        artifacts.append("_Файлов пока нет._")
    for art in files:
        line = "- " + _md_link(art.get("title") or Path(str(art.get("path"))).name, art.get("path"))
        if art.get("exists") is False:
            line += " _(файл не найден)_"
        if project_dir:
            try:
                rel = Path(str(art.get("path"))).resolve().relative_to(Path(project_dir).resolve())
                line += " · `%s`" % rel
            except (ValueError, OSError):
                pass
        artifacts.append(line)
    report = detail.get("report") if isinstance(detail.get("report"), dict) else None
    if report and report.get("available") and report.get("path"):
        artifacts += ["", "- Отчёт: " + _md_link(report.get("title") or "Отчёт", report["path"])]
    blocks.append(("artifacts", "\n".join(artifacts)))

    nodes = [n for n in (detail.get("tree") or []) if isinstance(n, dict) and n.get("id")]
    team = ["## Кто что делал", ""]
    if not nodes:
        team.append("_Карточек работ пока нет._")
    else:
        index = {str(n["id"]): n for n in nodes}
        children: dict = {}
        roots = []
        for node in nodes:
            parents = [str(p) for p in (node.get("parent_ids") or []) if str(p) in index]
            if parents:
                children.setdefault(parents[0], []).append(node)
            else:
                roots.append(node)

        def render(node, depth, seen):
            if str(node["id"]) in seen or depth > 6:
                return []
            seen = seen | {str(node["id"])}
            bits = [_clean(node.get("assignee")) or "без исполнителя",
                    REQUEST_STATUS_RU.get(str(node.get("status") or ""), str(node.get("status") or ""))]
            start, end = _as_int(node.get("started_at")), _as_int(node.get("finished_at"))
            if start and end and end >= start:
                bits.append("%d мин" % max(1, (end - start) // 60))
            summary = _clean(str(node.get("summary_md") or "").split("\n")[0])[:160]
            line = "%s- **%s** — %s%s" % ("  " * depth, _clean(node.get("title")) or node["id"],
                                        " · ".join(b for b in bits if b),
                                        (" — " + summary) if summary else "")
            out = [line]
            for child in sorted(children.get(str(node["id"]), []),
                                key=lambda n: (_as_int(n.get("started_at")), str(n.get("id")))):
                out += render(child, depth + 1, seen)
            return out

        for root_node in sorted(roots, key=lambda n: (_as_int(n.get("started_at")), str(n.get("id")))):
            team += render(root_node, 0, frozenset())
    blocks.append(("team", "\n".join(team)))

    links = ["## См. также", "",
             "- [[CONTEXT|Контекст комнаты]]",
             "- [[STATE|Состояние проекта]]",
             "- [[Журнал|Журнал комнаты]]"]
    siblings = []
    for other in registry.list_requests(cfg, room_id, 8):
        other_id = str(other.get("root_id") or "")
        if not other_id or other_id == root_id:
            continue
        siblings.append("[[%s]]" % note_stem(other_id, other.get("title"), other.get("created_at")))
        if len(siblings) >= 5:
            break
    if siblings:
        links += ["", "**Соседние запросы:** " + ", ".join(siblings)]
    blocks.append(("links", "\n".join(links)))
    return blocks


def _find_note(notes_dir: Path, root_id: str) -> Path:
    """Существующая заметка этого запроса (по хвосту ``(<id>).md``) или новый путь."""
    suffix = "(%s).md" % _short_id(root_id)
    try:
        names = sorted(p for p in notes_dir.iterdir() if p.is_file() and p.name.endswith(suffix))
    except OSError:
        names = []
    return names[0] if names else None


def sync_request(cfg, room_id, root_id, detail=None) -> dict:
    """Заметка запроса в Obsidian. ``{"path", "changed"}``; changed=False — текст тот же."""
    room_id = str(room_id or "").strip()
    root_id = str(root_id or "").strip()
    if not room_id or not root_id:
        return {"path": "", "changed": False}
    if _writes_disabled(cfg):
        return {"path": "", "changed": False, "skipped": "mock"}

    room = _room(cfg, room_id)
    record = registry.get(cfg, room_id, root_id) or {}
    data = dict(detail) if isinstance(detail, dict) else {}
    data.setdefault("id", root_id)
    if not data.get("title"):
        data["title"] = record.get("title") or root_id
    if not data.get("text"):
        data["text"] = record.get("text") or ""
    if not data.get("created_at"):
        data["created_at"] = record.get("created_at") or 0
    if not data.get("status"):
        data["status"] = record.get("status") or ""
    if not isinstance(data.get("synthesis"), dict) or not data["synthesis"]:
        if record.get("summary") or record.get("version"):
            data["synthesis"] = {"state": "ready", "text_md": record.get("summary") or "",
                                  "version": _as_int(record.get("version")) or 1,
                                  "at": record.get("answer_at") or 0}
        else:
            data["synthesis"] = {}

    title = _clean(data.get("title")) or root_id
    stem = note_stem(root_id, title, data.get("created_at"))
    base = _vault_base(cfg)
    notes_dir = _subdir(base, safe_dir_name(room_id), NOTE_DIR)
    existing = _find_note(notes_dir, root_id)
    path = _checked_target(base, existing or (notes_dir / (stem + ".md")))

    syn = data.get("synthesis") or {}
    front = _frontmatter(
        ["teamroom", "комната/%s" % safe_stem(room_id, 40), "запрос"],
        {"room": room_id, "request_id": root_id, "title": title,
         "status": str(data.get("status") or ""), "created": _fmt_date(data.get("created_at")),
         "versions": _as_int(syn.get("version"))},
    )
    blocks = _request_sections(cfg, room, root_id, data)
    body = _strip_front(_read_text_retry(path) or "")
    text = front + _merge_blocks(body, blocks)
    changed = _atomic_write(path, text)
    return {"path": str(path), "changed": bool(changed)}


# --- CONTEXT.md комнаты -----------------------------------------------------

def _room_line(room: dict) -> str:
    members = [m for m in (room.get("members") or []) if isinstance(m, dict)]
    parts = []
    lead = str(room.get("orchestrator") or "").strip()
    if lead:
        parts.append("**руководитель:** %s" % lead)
    if members:
        parts.append("**участники:** " + ", ".join(
            "%s (%s)" % (_clean(m.get("profile")), _clean(m.get("role")) or "участник")
            for m in members if m.get("profile")))
    if room.get("critic"):
        parts.append("**критик:** %s" % _clean(room.get("critic")))
    return " · ".join(parts)


def _notes_room(room: dict) -> tuple[dict, list]:
    """`(указания, [(профиль, роль)])` из `server/agent_notes` (лениво, без падений)."""
    try:
        notes_mod = _import("agent_notes")
    except ImportError:  # pragma: no cover - модуль ещё не готов
        return {}, []
    try:
        notes = notes_mod.notes_of(room)
        people = notes_mod.participants(room)
    except Exception:  # noqa: BLE001 - зеркало важнее указаний
        return {}, []
    return notes, people


NOTES_FILE = "Указания участникам.md"


def _mask_notes(text) -> str:
    """Единая маска секретов (`agent_notes.mask_secrets`) для внешних файлов Vault.

    В реестре комнаты текст хранится как есть (Михаил видит своё), а в Obsidian
    и в CONTEXT.md пароль/ключ попадать открытым текстом не должен.
    """
    try:
        notes_mod = _import("agent_notes")
        mask = getattr(notes_mod, "mask_secrets", None)
        if callable(mask):
            return mask(text)
    except Exception:  # noqa: BLE001 - без модуля указаний пишем как есть
        pass
    return "" if text is None else str(text)


def _notes_section(room: dict) -> str:
    """Раздел «Особые указания» для CONTEXT.md (WAVE9 §9)."""
    notes, people = _notes_room(room)
    lines = ["## Особые указания", ""]
    if not notes:
        lines.append("_Особых указаний участникам пока нет._")
        return "\n".join(lines)
    for profile, role in people:
        item = notes.get(profile)
        if not item:
            continue
        one = " ".join(_mask_notes(item.get("text")).split())
        lines.append("- **%s (%s):** %s" % (role, _clean(profile), one[:400]))
    missing = [p for p in notes if p not in {name for name, _r in people}]
    for profile in missing:      # указания остались от убранного участника — не теряем
        one = " ".join(_mask_notes(notes[profile].get("text")).split())
        lines.append("- **%s:** %s" % (_clean(profile), one[:400]))
    lines += ["", "Полные тексты — [[Указания участникам]]."]
    return "\n".join(lines)


def _notes_file_text(cfg, room: dict) -> tuple:
    """Содержимое `Указания участникам.md`: записи каждого участника + журнал изменений."""
    room_id = str(room.get("id") or "")
    title = str(room.get("title") or room_id)
    notes, people = _notes_room(room)
    front = _frontmatter(
        ["teamroom", "указания", "комната/%s" % safe_stem(room_id, 40)],
        {"room": room_id, "title": title, "kind": "agent_notes"},
    )
    body = ["## Особые указания участникам комнаты «%s»" % title, ""]
    if not notes:
        body.append("_Особых указаний пока нет. Добавьте их в настройках комнаты — "
                    "они будут действовать только в этой комнате._")

    def _entries(item: dict) -> list:
        try:
            return list(_import("agent_notes").entries_of(item))
        except Exception:  # noqa: BLE001 - без модуля печатаем сплошной текст
            return []

    def _who(entry: dict) -> str:
        marks = []
        marks.append("закреплено" if entry.get("pinned") else
                     {"user": "Михаил", "leader": "руководитель комнаты",
                      "auto": "автоматически"}.get(str(entry.get("source") or ""), "руководитель"))
        if entry.get("origin"):
            marks.append("источник: %s" % _clean(entry["origin"]))
        stamp = _fmt_dt(entry.get("created_at"))
        if stamp:
            marks.append(stamp)
        used = int(entry.get("used_count") or 0)
        if used:
            marks.append("пригодилось %d раз" % used)
        return " · ".join(marks)

    for profile, role in people:
        item = notes.get(profile)
        if not item:
            continue
        stamp = _fmt_dt(item.get("updated_at"))
        body += ["### %s (%s)" % (role.capitalize(), _clean(profile)),
                 ("_обновлено: %s_" % stamp) if stamp else "", ""]
        entries = _entries(item)
        if not entries:
            body += [_mask_notes(item.get("text")), ""]
            continue
        for entry in entries:
            body += ["- %s" % _mask_notes(entry.get("text")),
                     "  _%s_" % _who(entry), ""]
    for profile in [p for p in notes if p not in {name for name, _r in people}]:
        item = notes[profile]
        body += ["### %s" % _clean(profile), "", _mask_notes(item.get("text")), ""]
    events = []
    try:
        raw = room.get("agent_notes_log")
        events = [e for e in (raw if isinstance(raw, list) else []) if isinstance(e, dict)][:20]
    except Exception:  # noqa: BLE001 - журнал не обязателен
        events = []
    if events:
        body += ["## Журнал изменений (последние %d)" % len(events), ""]
        for event in events:
            stamp = _fmt_dt(event.get("at"))
            action = {"add": "добавлено", "update": "изменено", "remove": "удалено",
                      "merge": "объединено", "review": "переоценка", "replace": "заменено",
                      "rollback": "откат", "pinned": "закреплено",
                      "unpin": "снято закрепление",
                      # WAVE19 B-27/B-35: журнал комнаты в заметке — по-русски
                      "artifact_outside": "файл вне папки проекта",
                      "leader_review": "оценка работы руководителем",
                      "leader_reminder": "напоминание руководителю"}.get(str(event.get("action")),
                                                                       str(event.get("action") or ""))
            head = "- %s · %s · %s" % (stamp or "—", _clean(event.get("profile")) or "—", action)
            tail = " — %s" % _mask_notes(event.get("text") or "") if event.get("text") else ""
            note = (" (%s)" % _clean(event.get("note"))) if event.get("note") else ""
            flag = " · _откачено_" if event.get("rolled_back") else ""
            body += [head + tail + note + flag, ""]
    body += ["---", "",
             "Указания действуют только в комнате «%s»: они дописываются к роли "
             "участника и не меняют его SOUL.md." % title]
    blocks = [("agent_notes", "\n".join(body).strip())]
    return front, blocks


def _write_notes_file(cfg, base: Path, room_dir: Path, room: dict) -> bool:
    """`Указания участникам.md` рядом с CONTEXT.md; ручное вне маркеров сохраняется."""
    front, blocks = _notes_file_text(cfg, room)
    path = _checked_target(base, room_dir / NOTES_FILE)
    body = _strip_front(_read_text_retry(path) or "")
    return bool(_atomic_write(path, front + _merge_blocks(body, blocks)))


def _context_blocks(cfg, room: dict, state: dict, requests: list, include_glossary=True) -> list:
    room_id = str(room.get("id") or "")
    title = str(room.get("title") or room_id)
    blocks = []
    intro = ["# Контекст комнаты «%s»" % title, "", "## О комнате", ""]
    intro.append("**Комната «%s»**%s" % (title, (" — " + _room_line(room)) if _room_line(room) else "."))
    if room.get("description"):
        intro += ["", _clean(room.get("description"))]
    blocks.append(("head", "\n".join(intro)))

    roadmap = state.get("roadmap") or {}
    current = roadmap.get("current") or {}
    stage = ["## Текущий этап", ""]
    if current:
        stage.append("**%s** — %s" % (_clean(current.get("title") or current.get("id")),
                                     STATUS_RU.get(current.get("status"), current.get("status") or "")))
        phase = next((p for p in (roadmap.get("phases") or []) if p.get("id") == current.get("id")), {})
        if phase.get("note"):
            stage += ["", _clean(phase["note"])]
    else:
        stage.append("_Этапы проекта ещё не описаны ни одним запросом._")
    blocks.append(("stage", "\n".join(stage)))

    # Особые указания участникам этой комнаты (WAVE9 §9): в карточку и в CONTEXT.md.
    blocks.append(("agent_notes", _notes_section(room)))

    decisions = [d for d in (state.get("decisions") or []) if isinstance(d, dict)]
    decisions.sort(key=lambda d: (_as_int(d.get("at")) or _as_int(d.get("updated_at")) or 0), reverse=True)
    body = ["## Решения (последние 30)", ""]
    if not decisions:
        body.append("_Решений пока нет._")
    for decision in decisions[:30]:
        label = {"accepted": "принято", "proposed": "предложено",
                 "rejected": "отклонено"}.get(str(decision.get("status") or ""), "")
        body.append("- %s%s" % (_clean(decision.get("text")),
                                (" — *%s*" % label) if label else ""))
    blocks.append(("decisions", "\n".join(body)))

    backlog = state.get("backlog") or {}
    sections = backlog.get("sections") or {}
    questions = [i for i in (sections.get("inbox") or []) if isinstance(i, dict)]
    body = ["## Открытые вопросы", ""]
    if not questions:
        body.append("_Открытых вопросов нет._")
    for item in questions[:15]:
        line = "- %s" % _clean(item.get("text"))
        if item.get("source_request"):
            line += " — [%s](#/room/%s/request/%s)" % (item.get("source_request"), room_id,
                                                      item.get("source_request"))
        body.append(line)
    blocks.append(("questions", "\n".join(body)))

    steps = [i for i in (sections.get("next_steps") or []) if isinstance(i, dict)]
    body = ["## Ближайшие шаги", ""]
    if not steps:
        body.append("_Следующих шагов пока нет._")
    for item in steps[:15]:
        tail = []
        if item.get("owner"):
            tail.append("владелец: %s" % _clean(item.get("owner")))
        due = _fmt_due(item.get("due"))
        if due:
            tail.append("срок: %s" % due)
        body.append("- [ ] %s%s" % (_clean(item.get("text")),
                                    (" — " + ", ".join(tail)) if tail else ""))
    blocks.append(("next", "\n".join(body)))

    body = ["## Последние 10 запросов", ""]
    if not requests:
        body.append("_Запросов ещё не было._")
    for record in requests[:10]:
        root_id = str(record.get("root_id") or "")
        stem = note_stem(root_id, record.get("title"), record.get("created_at"))
        one = _clean(str(record.get("summary") or record.get("text") or "").split("\n")[0])[:110]
        line = "- [[%s|%s]]" % (stem, _clean(record.get("title")) or root_id)
        if one:
            line += " — " + one
        body.append(line)
    blocks.append(("requests", "\n".join(body)))

    if include_glossary:
        terms = _glossary_terms(cfg)
        if terms:
            body = ["## Глоссарий", "", ", ".join(terms[:40]) + ("." if len(terms) > 40 else "")]
            blocks.append(("glossary", "\n".join(body)))

    where = ["## Где искать", ""]
    room_dir = _vault_room_path(cfg, room_id)
    where += ["- Контекст комнаты: `%s`" % (room_dir / CONTEXT_FILE),
              "- Состояние проекта: [[STATE|STATE.md]] (`%s`)" % (room_dir / "STATE.md"),
              "- Журнал: [[Журнал|Журнал.md]]",
              "- Заметки запросов: `%s`" % (room_dir / NOTE_DIR)]
    project_dir = str(room.get("project_dir") or "").strip()
    if project_dir:
        where.append("- Папка проекта: `%s`" % project_dir)
    for path in (room.get("context_files") or []):
        where.append("- Файл контекста: `%s`" % str(path))
    blocks.append(("where", "\n".join(where)))
    return blocks


def _vault_room_path(cfg, room_id: str) -> Path:
    return Path(cfg.vault_dir) / safe_dir_name(room_id)


def _glossary_terms(cfg) -> list:
    """Термины словаря диктовки (data/dictation/dictionary.json), если файл есть."""
    path = Path(cfg.data_dir) / "dictation" / "dictionary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    terms = []
    for item in (data.get("terms") if isinstance(data, dict) else None) or []:
        text = _clean(item)
        if text and text not in terms:
            terms.append(text)
    for item in (data.get("replacements") if isinstance(data, dict) else None) or []:
        target = _clean(item.get("to")) if isinstance(item, dict) else ""
        if target and target not in terms:
            terms.append(target)
    return terms


def _shrink(blocks) -> bool:
    """Убрать одну строку из наименее важного блока (для лимита 6 000 знаков)."""
    order = ("glossary", "requests", "decisions", "questions", "next", "where", "agent_notes",
             "stage", "head")
    for name in order:
        for index, (block_name, body) in enumerate(blocks):
            if block_name != name:
                continue
            lines = body.split("\n")
            for j in range(len(lines) - 1, 0, -1):
                if not lines[j].strip() or lines[j].strip().startswith("#"):
                    continue
                if len(lines) <= 2:
                    break
                del lines[j]
                blocks[index] = (block_name, "\n".join(lines).rstrip())
                return True
    return False


def sync_room(cfg, room_id) -> dict:
    """``CONTEXT.md`` комнаты + ``STATE.md``/``Журнал.md``. ``{"context_path", "changed"}``."""
    room_id = str(room_id or "").strip()
    if not room_id:
        return {"context_path": "", "changed": False}
    if _writes_disabled(cfg):
        return {"context_path": "", "changed": False, "skipped": "mock"}
    room = _room(cfg, room_id)
    state = _state(cfg, room_id)
    requests = registry.list_requests(cfg, room_id, 10)
    title = str(room.get("title") or room_id)
    front = _frontmatter(
        ["teamroom", "контекст", "комната/%s" % safe_stem(room_id, 40)],
        {"room": room_id, "title": title, "kind": "context"},
    )
    blocks = _context_blocks(cfg, room, state, requests)
    while len(_compose(front, blocks)) > CONTEXT_LIMIT and _shrink(blocks):
        pass
    text = _compose(front, blocks)
    if len(text) > CONTEXT_LIMIT:      # крайний случай: режем самый длинный блок
        overflow = len(text) - CONTEXT_LIMIT
        for _ in range(20):
            index = max(range(len(blocks)), key=lambda i: len(blocks[i][1])) if blocks else None
            if index is None or len(blocks[index][1]) <= 120:
                break
            body = blocks[index][1]
            keep = max(120, len(body) - overflow - 40)
            blocks[index] = (blocks[index][0], body[:keep].rstrip() + "\n\n_…дальше в заметках запросов._")
            text = _compose(front, blocks)
            if len(text) <= CONTEXT_LIMIT:
                break
            overflow = len(text) - CONTEXT_LIMIT

    base = _vault_base(cfg)
    room_dir = _subdir(base, safe_dir_name(room_id))
    path = _checked_target(base, room_dir / CONTEXT_FILE)
    body = _strip_front(_read_text_retry(path) or "")
    changed = _atomic_write(path, front + _merge_blocks(body, blocks))

    notes_changed = False
    try:                                   # «Указания участникам.md» (WAVE9 §9)
        notes_changed = _write_notes_file(cfg, base, room_dir, room)
    except Exception:  # noqa: BLE001 - CONTEXT.md уже записан, отдельный файл вторичен
        notes_changed = False

    state_changed = False
    try:
        ingest_mod = _import("ingest")
        state_changed = bool(ingest_mod.write_vault_mirror(cfg, room_id))
    except Exception:  # noqa: BLE001 - заметки уже записаны, STATE/Журнал вторичны
        state_changed = False
    return {"context_path": str(path), "changed": bool(changed or notes_changed or state_changed),
            "notes_path": str(room_dir / NOTES_FILE)}


def context_summary(cfg, room_id, max_chars: int = 1500) -> str:
    """Короткая выжимка для контракта карточки (Блок D), ≤ max_chars знаков."""
    try:
        limit = max(200, int(max_chars))
    except (TypeError, ValueError):
        limit = 1500
    room_id = str(room_id or "").strip()
    if not room_id:
        return ""
    room = _room(cfg, room_id)
    state = _state(cfg, room_id)
    requests = registry.list_requests(cfg, room_id, 10)
    blocks = _context_blocks(cfg, room, state, requests, include_glossary=False)
    wanted = ("stage", "decisions", "questions", "next", "requests")
    text = "\n\n".join(body for name, body in blocks if name in wanted)
    text = text.strip()
    if len(text) > limit:
        text = text[: max(10, limit - 1)].rstrip() + "…"
    return text


__all__ = ["sync_request", "sync_room", "context_summary", "note_stem", "safe_stem",
           "NOTE_DIR", "CONTEXT_FILE", "CONTEXT_LIMIT"]

