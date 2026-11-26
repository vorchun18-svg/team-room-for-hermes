"""«Результаты агентов» по задаче (W30-E, контракт §4, дефект 16).

Зачем
-----
В задаче работают десяток исполнителей: каждый оставляет карточку, файл и
вывод. Раньше это тонуло в ленте сообщений и дереве работ: чтобы понять «кто что
сделал и где лежит файл», человек листал дерево карточек и открывал попытки.
Здесь один плоский блок: исполнитель → что делал (название карточки) → вывод
2–3 строки → файлы с путями → что осталось.

Источник — ТОЛЬКО дерево карточек и их метаданные (`summary_md`, `metadata`)
из ``GET /api/requests/{id}``: никаких вызовов моделей, никакой записи.

Что отдаём (docs/WAVE30-PACKAGES.md §4)::

    {"agents": [{"agent", "role", "title", "status", "verdict",
                 "files": [{"path", "name", "exists"}], "remaining", "card_id"}]}

Добавлены (не заменяют существующие, как разрешает контракт): ``role_raw``,
``verdict_tag``, ``files_hidden``, ``files_total``, ``summary`` и на верхнем
уровне ``counts`` — для счётчиков в заголовке блока.

Правила безопасности: путь файла показываем как есть (без белых списков —
человеку нужен настоящий путь), но существование проверяем только чтением
(`os.path.isfile`) и файлы, похожие на секреты по имени (`.env`, `*.pem`,
`*token*`), в список не попадают: их имя и путь не раскрываем.
"""
from __future__ import annotations

import os
import re

try:                                     # пакетный и «плоский» импорт
    from .attachments import is_secret as _is_secret
except ImportError:                      # pragma: no cover
    from attachments import is_secret as _is_secret  # type: ignore

#: Сколько исполнителей максимум отдаём (дальше — только счётчик `hidden_agents`).
MAX_AGENTS = 60
#: Сколько файлов показываем у одного исполнителя.
MAX_FILES = 10
#: Пределы текста: вывод и «что осталось» — 2–3 строки, не диссертация.
VERDICT_CHARS = 400
SUMMARY_CHARS = 700
REMAINING_CHARS = 320

#: Ключи метаданных, из которых берём «что осталось». Порядок = приоритет:
#: сначала явное «не сделано», потом шаги/пробелы/вопросы.
REMAINING_KEYS = ("not_done", "remaining", "next_steps", "next", "follow_up", "followup",
                  "followups", "gaps", "gaps_for_mikhail", "key_gaps", "open_gaps",
                  "not_verified", "needs_verification", "needs_human", "unconfirmed",
                  "open_questions", "open_questions_for_contador", "open_for_pavel",
                  "blockers", "risks", "limitations", "todo", "followups_proposed")

#: Ключи метаданных с файлами результата.
FILE_KEYS = ("artifacts", "artifact", "files", "changed_files")

#: Метки статуса карточки — те же слова, что в остальном интерфейсе.
_STATUS_RU = {"done": "готово", "review": "на проверке", "running": "в работе",
              "blocked": "остановлено", "failed": "ошибка", "todo": "не начато",
              "queued": "в очереди", "archived": "в архиве"}

#: Роль карточки в дереве — по-русски (в карточке она машинная: `root`, `worker`).
_ROLE_RU = {"root": "руководитель", "lead": "руководитель", "dir": "руководитель",
            "synthesizer": "свод", "svod": "свод", "critic": "критик",
            "reviewer": "проверка", "worker": "исполнитель", "executor": "исполнитель"}

_MD_LINK = re.compile(r"\[([^\]]{1,200})\]\(([^)]{0,400})\)")
# Жирный и код снимаем; одиночные «_»/«*» не трогаем — иначе имена файлов
# (`02_legal.md`) и слова с подчёркиваниями теряют знаки.
_MD_NOISE = re.compile(r"\*\*|__|`{1,3}")
_MD_HEAD = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*(?:[-—•·*+]|\d{1,2}[.)])\s+", re.MULTILINE)
_SPACES = re.compile(r"[ \t\u00a0]+")
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


# --- текст -----------------------------------------------------------------

def plain(text) -> str:
    """Markdown → одна строка без разметки (для вывода и «что осталось»)."""
    value = str(text if text is not None else "")
    if not value.strip():
        return ""
    value = _MD_LINK.sub(r"\1", value)
    value = _MD_HEAD.sub("", value)
    value = _MD_BULLET.sub("", value)
    value = _MD_NOISE.sub("", value)
    value = _SPACES.sub(" ", value)
    return " ".join(part.strip() for part in value.splitlines() if part.strip()).strip()


def _clip(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    cut = value[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:—–-") + "…"


def verdict_of(summary_md) -> str:
    """Вывод 2–3 строки из сводки карточки.

    Сводки исполнителей начинаются с вывода («Коротко: …», «Готово: …»), поэтому
    берём НАЧАЛО текста: 2–3 предложения или до 400 знаков. Разметку снимаем.
    """
    text = plain(summary_md)
    if not text:
        return ""
    parts = [part for part in _SENT_SPLIT.split(text) if part.strip()]
    out = ""
    for part in parts[:3]:
        candidate = (out + " " + part).strip()
        if len(candidate) > VERDICT_CHARS and out:
            break
        out = candidate
        if len(out) >= VERDICT_CHARS * 0.75:
            break
    return _clip(out or text, VERDICT_CHARS)


def _item_text(item) -> str:
    """Текст одного пункта метаданных: строка, dict с text/title/label, число."""
    if item is None or isinstance(item, bool):
        return ""
    if isinstance(item, str):
        return plain(item)
    if isinstance(item, (int, float)):
        return ""                       # «open_questions: 3» — это счётчик, не пункт
    if isinstance(item, dict):
        for key in ("text", "title", "label", "question", "what", "summary", "name", "item"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return plain(value)
        return ""
    if isinstance(item, (list, tuple)):
        return "; ".join(text for text in (_item_text(sub) for sub in item) if text)
    return ""


def remaining_of(meta) -> str:
    """«Что осталось» из метаданных карточки (первый непустой ключ, 2 пункта)."""
    data = meta if isinstance(meta, dict) else {}
    for key in REMAINING_KEYS:
        if key not in data:
            continue
        raw = data.get(key)
        items = raw if isinstance(raw, list) else [raw]
        texts = [text for text in (_item_text(item) for item in items) if text]
        if not texts:
            continue
        head = "; ".join(texts[:2])
        extra = len(texts) - 2
        if extra > 0:
            head += "; и ещё %d" % extra
        return _clip(head, REMAINING_CHARS)
    return ""


# --- файлы -----------------------------------------------------------------

def _exists(path: str) -> bool:
    """Существует ли файл — только чтение, любые сбои = «нет»."""
    try:
        return bool(path) and os.path.isfile(path)
    except (OSError, ValueError):
        return False


def _file_items(meta) -> list:
    """Пути файлов результата из метаданных в порядке объявления."""
    data = meta if isinstance(meta, dict) else {}
    paths, seen = [], set()
    for key in FILE_KEYS:
        raw = data.get(key)
        items = raw if isinstance(raw, list) else [raw] if raw else []
        for item in items:
            path = ""
            if isinstance(item, str):
                path = item.strip()
            elif isinstance(item, dict):
                path = str(item.get("path") or item.get("file") or "").strip()
            if not path or path in seen:
                continue
            # Служебные ссылки вида "http://…" — не файл.
            if "://" in path:
                continue
            seen.add(path)
            paths.append(path)
        if paths:
            break                        # первый непустой ключ — источник (без дублей)
    return paths


def files_of(meta) -> dict:
    """``{"files": [...], "hidden": N, "total": N, "mirrors": N}`` — файлы результата.

    Один и тот же файл часто лежит в папке задачи и зеркалом в Vault: в списке
    оставляем ОДИН (самый короткий путь — обычно папка задачи), остальные
    считаем в ``mirrors``. Иначе у исполнителя видно «02_legal.md» дважды.
    """
    paths = _file_items(meta)
    by_name, mirrors = {}, 0
    for path in paths:
        name = os.path.basename(path.replace("\\", "/")) or path
        if _is_secret(name):
            continue
        key = name.lower()
        if key in by_name:
            mirrors += 1
            if len(path) < len(by_name[key]):
                by_name[key] = path
            continue
        by_name[key] = path
    files = []
    for name, path in [(os.path.basename(p.replace("\\", "/")) or p, p) for p in by_name.values()]:
        files.append({"path": path, "name": name, "exists": _exists(path)})
    hidden = sum(1 for path in paths
                 if _is_secret(os.path.basename(path.replace("\\", "/")) or path))
    return {"files": files[:MAX_FILES], "hidden": hidden, "total": len(files),
            "mirrors": mirrors}


# --- роль ------------------------------------------------------------------

def _role_label(node: dict, room) -> str:
    """Человеческая роль исполнителя: подпись комнаты, иначе роль карточки."""
    data = room if isinstance(room, dict) else {}
    who = str(node.get("assignee") or "")
    labels = data.get("role_labels") if isinstance(data.get("role_labels"), dict) else {}
    for key in (who, str(node.get("profile") or "")):
        if key and labels.get(key):
            return str(labels[key]).strip()
    for member in data.get("members") or []:
        if isinstance(member, dict) and str(member.get("profile") or "") == who:
            for key in ("role", "job", "title", "name"):
                if str(member.get(key) or "").strip():
                    return str(member[key]).strip()
    raw = str(node.get("role") or "").strip()
    return _ROLE_RU.get(raw.lower(), raw or "исполнитель")


def _verdict_tag(meta) -> str:
    """Машинный вердикт критика (`ship`/`revise`/…), если он записан."""
    data = meta if isinstance(meta, dict) else {}
    raw = data.get("verdict")
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("verdict") or ""
    text = str(raw or "").strip().lower()
    return text[:24]


# --- сборка ----------------------------------------------------------------

def agent_of(node: dict, room=None) -> dict:
    """Одна строка результата по карточке дерева (никогда не падает)."""
    node = node if isinstance(node, dict) else {}
    meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    try:
        files = files_of(meta)
    except Exception:                    # noqa: BLE001 — список файлов вторичен
        files = {"files": [], "hidden": 0, "total": 0}
    status = str(node.get("status") or "")
    return {
        "agent": str(node.get("assignee") or ""),
        "role": _role_label(node, room),
        "role_raw": str(node.get("role") or ""),
        "title": str(node.get("title") or ""),
        "status": status,
        "status_label": _STATUS_RU.get(status, status),
        "verdict": verdict_of(node.get("summary_md")),
        "summary": _clip(plain(node.get("summary_md")), SUMMARY_CHARS),
        "verdict_tag": _verdict_tag(meta),
        "files": files["files"],
        "files_hidden": files["hidden"],
        "files_mirrors": files.get("mirrors", 0),
        "files_total": files["total"],
        "remaining": remaining_of(meta),
        "card_id": str(node.get("id") or ""),
        "started_at": node.get("started_at") if isinstance(node.get("started_at"), int) else None,
        "finished_at": node.get("finished_at") if isinstance(node.get("finished_at"), int) else None,
    }


def build(nodes, room=None) -> dict:
    """``{"agents": [...], "counts": {...}}`` по дереву карточек задачи.

    Порядок — как в дереве (руководитель и свод сверху), без пересортировки:
    человек читает тот же порядок, что и в «Дереве работ».
    """
    items = [node for node in (nodes or []) if isinstance(node, dict) and node.get("id")]
    agents = []
    for node in items[:MAX_AGENTS]:
        try:
            row = agent_of(node, room)
        except Exception:                # noqa: BLE001 — одна карточка не рушит блок
            continue
        if row.get("card_id"):
            agents.append(row)
    files_total = sum(int(row.get("files_total") or 0) for row in agents)
    with_remaining = sum(1 for row in agents if row.get("remaining"))
    with_files = sum(1 for row in agents if row.get("files"))
    done = sum(1 for row in agents if row.get("status") in ("done", "archived"))
    return {"agents": agents,
            "counts": {"agents": len(agents), "hidden_agents": max(0, len(items) - MAX_AGENTS),
                       "files": files_total, "agents_with_files": with_files,
                       "remaining": with_remaining, "done": done},
            "generated_at": None}


def attach(detail, room=None) -> dict:
    """Положить ``agent_results`` в деталь задачи и вернуть тот же блок.

    Ключ деталь-ответа и ручки — один и тот же объект (контракт §4: «то же»),
    чтобы клиент не собирал данные двумя путями.
    """
    payload = build((detail or {}).get("tree"), room)
    if isinstance(detail, dict):
        detail["agent_results"] = payload
    return payload


__all__ = ["MAX_AGENTS", "MAX_FILES", "REMAINING_KEYS", "build", "attach", "agent_of",
           "files_of", "remaining_of", "verdict_of", "plain"]
