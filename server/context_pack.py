"""Блок «КОНТЕКСТ КОМНАТЫ» в теле новой карточки запроса (WAVE 8, Блок D / Д2).

Зачем: новый запрос не должен начинаться «с чистого листа». В теле карточки
агент сразу видит, что за комната, где выжимка проекта, какие файлы читать
первыми, что уже решали по похожим запросам и куда складывать итоговые файлы.

Что кладём (в таком порядке):
1. название и описание комнаты;
2. выжимку проекта — `vault_mirror.context_summary` (лениво, если модуль уже
   умеет), иначе разделы `STATE.md` комнаты из `cfg.vault_dir`;
3. `room["context_files"]` — пути, которые читать первыми;
4. «Похожие прошлые запросы» — до 5 по пересечению слов названия и текста
   (простая русская нормализация: нижний регистр, ё→е, отсечение окончаний);
5. обязательную инструкцию исполнителям (Obsidian — источник правды);
6. правило папки проекта (Д2) либо, если она не задана, — «Материалы\\» в Vault.

Свойства: детерминированно (порядок и усечение заданы явно; от времени зависит
только дата подпапки запроса), ≤ 4 000 знаков, без токенов/паролей/переменных
окружения — подозрительные строки вырезаются (`_scrub`), файлы читаются ТОЛЬКО
внутри `cfg.vault_dir`. Ничего не пишем и в сеть не ходим.

Интерфейс (WAVE 8, «Интерфейсы»)::

    build_context_block(cfg, room, text) -> str          # тело-вставка для карточки
    similar_requests(cfg, room_id, text, limit=5) -> list[{root_id,title,summary_line,note_path}]
    insert_into_body(body, block) -> str                 # ставит блок в тело карточки

`insert_into_body` согласован с чтением: `contract.visible_text` режет тело по
маркеру контракта, поэтому блок ставится ПОСЛЕ открывающей строки контракта —
он остаётся в карточке для агентов, но не попадает в `RequestDetail.text`.
Если `visible_text` когда-нибудь научится резать и по нашему маркеру (проба ниже
проверяет это на месте), блок автоматически встанет перед контрактом.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MAX_BLOCK_CHARS = 4000
MAX_NOTES_CHARS = 3000          # раздел «Особые указания участникам» (WAVE9 §9)
MAX_SUMMARY_CHARS = 1500
MAX_DESC_CHARS = 320
MAX_TITLE_CHARS = 120
MAX_SUMMARY_LINE = 170
MAX_PATH_CHARS = 150
MAX_FILES = 8
DEFAULT_LIMIT = 5
REGISTRY_LIMIT = 200

CONTEXT_OPEN = ("<<<КОНТЕКСТ КОМНАТЫ  (служебный блок: прочитать до начала работы; "
                "в подзадачи не копировать)")
CONTEXT_CLOSE = "КОНТЕКСТ КОМНАТЫ>>>"
CONTRACT_MARKER = "<<<КОМНАТА-КОНТРАКТ"
NOTE_DIR = "Запросы"
MATERIALY = "Материалы"

INSTRUCTION = (
    "Источник правды — Obsidian (путь хранилища указан в настройках комнаты). Сначала прочитай "
    "CONTEXT.md комнаты и "
    "релевантные заметки (search_files по ключевым словам запроса). Недостающие данные ищи "
    "в Vault и в архиве запросов комнаты — не спрашивай Михаила, пока ответа нет в Vault. "
    "Пользуйся своими обычными навыками и инструментами Hermes."
)

#: чем вырезаем похожее на секреты (ключи, токены, пароли, bearer)
_SCRUB = (
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}"), "[скрыто]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[скрыто]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "[скрыто]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 [скрыто]"),
    (re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|токен|пароль|секрет)\s*[:=]\s*)\S+"),
     r"\1[скрыто]"),
)

_WORD_RE = re.compile(r"[0-9a-zа-я]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_WIN_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: окончания, которые отсекаем при «простой русской нормализации» (длинные первыми)
_ENDINGS = (
    "иями", "ями", "ами", "ов", "ев", "ей", "ой", "ый", "ий", "ая", "ое", "ые", "ых",
    "им", "ым", "ах", "ях", "ам", "ям", "ую", "юю", "ия", "ии", "ие", "у", "ю", "а",
    "я", "о", "е", "ы", "и", "ь",
)

_STOP = {
    "что", "как", "для", "или", "при", "без", "над", "под", "про", "через", "чтобы", "тоже",
    "это", "этот", "эта", "эти", "весь", "все", "мне", "меня", "мой", "моя", "мои", "твой",
    "наш", "ваш", "они", "она", "оно", "есть", "быть", "был", "была", "были", "будет",
    "надо", "нужно", "можно", "надо", "если", "когда", "где", "чем", "кто", "сколько",
    "какой", "какие", "какая", "теперь", "сейчас", "еще", "очень", "также", "потом",
    "надо", "этого", "этому", "который", "которая", "которые", "the", "and", "for",
}

_SECTION_KEYS = ("где мы сейчас", "решения", "ждет михаила")


# ---------------------------------------------------------------- утилиты
def _fold(text) -> str:
    """Нижний регистр + ё→е (без внешних библиотек)."""
    return str("" if text is None else text).lower().replace("ё", "е")


def _one_line(text, limit: int) -> str:
    """Одна строка: схлопнуть пробелы/переводы строк и обрезать по длине."""
    flat = _SPACE_RE.sub(" ", _scrub(str("" if text is None else text))).strip()
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


def _clamp(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _scrub(text) -> str:
    """Вырезать то, что похоже на токены/пароли (в теле карточки секретов быть не должно).

    Единая маска с указаниями (`agent_notes.mask_secrets`): Vault, CONTEXT.md,
    комментарии и тело карточки прячут одно и то же одинаково. Свои правила ниже —
    только запасной путь, если модуль указаний недоступен.
    """
    out = "" if text is None else str(text)
    try:
        notes_mod = None
        try:
            from . import agent_notes as notes_mod
        except ImportError:  # pragma: no cover - плоский импорт (python server/app.py)
            import agent_notes as notes_mod  # type: ignore
        mask = getattr(notes_mod, "mask_secrets", None)
        if callable(mask):
            return mask(out)
    except Exception:  # noqa: BLE001 - без модуля указаний работаем своими правилами
        pass
    for pattern, repl in _SCRUB:
        out = pattern.sub(repl, out)
    return out


def _stem(word: str) -> str:
    """Отсечь типичное русское окончание, если основа остаётся ≥ 4 знаков."""
    for ending in _ENDINGS:
        if len(word) - len(ending) >= 4 and word.endswith(ending):
            return word[: -len(ending)]
    return word


def _words(text) -> set:
    """Множество основ слов: нижний регистр, ё→е, без стоп-слов и коротких слов."""
    out = set()
    for raw in _WORD_RE.findall(_fold(text)):
        if len(raw) < 3 or raw in _STOP:
            continue
        stem = _stem(raw)
        if len(stem) >= 3 and stem not in _STOP:
            out.add(stem)
    return out


def _safe_path(value) -> str:
    """Путь в нативном виде, одной строкой, без лишней длины."""
    if value is None or value == "":
        return ""
    try:
        raw = str(Path(value))
    except (TypeError, ValueError):
        raw = str(value)
    return _one_line(raw, MAX_PATH_CHARS)


def _join_win(base: str, part: str) -> str:
    """`<base>\\<part>` — с обратными слэшами, без двойного разделителя."""
    head = str(base or "").rstrip("\\/")
    tail = str(part or "").strip("\\/")
    return f"{head}\\{tail}" if tail else head


def _win_safe(name: str) -> str:
    text = _WIN_BAD_RE.sub("-", str(name or ""))
    return _SPACE_RE.sub(" ", text).strip(" .") or "Запрос"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _short_id(root_id: str) -> str:
    """Короткий id запроса для имени заметки: `t_1a2b3c4d` → `1a2b3c4d`."""
    root = str(root_id or "")
    tail = root.split("_")[-1] if "_" in root else root
    return tail[:8] or root[:8]


# ---------------------------------------------------------------- доступ к данным
def _vault_room_dir(cfg, room_id: str) -> Path | None:
    try:
        from . import config as config_mod
        return Path(config_mod.room_vault_dir(cfg, room_id))
    except Exception:  # noqa: BLE001 - путь зеркала не должен ронять сборку блока
        try:
            return Path(cfg.vault_dir) / str(room_id)
        except Exception:  # noqa: BLE001
            return None


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")[:limit]
    except (OSError, ValueError):
        return ""


def _requests(cfg, room_id: str) -> list:
    """Запросы комнаты из реестра (или из `data/state/<room>/requests.json`)."""
    try:
        from . import registry as registry_mod
    except Exception:  # noqa: BLE001
        registry_mod = None
    if registry_mod is not None:
        try:
            got = registry_mod.list_requests(cfg, room_id, REGISTRY_LIMIT)
            if isinstance(got, list):
                return [rec for rec in got if isinstance(rec, dict)]
        except Exception:  # noqa: BLE001 - битый/занятый реестр не роняет запись
            pass
    try:
        path = Path(cfg.data_dir) / "state" / str(room_id) / "requests.json"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("requests")
    return [rec for rec in (data or []) if isinstance(rec, dict)]


# ---------------------------------------------------------------- выжимка проекта
def _state_md_summary(cfg, room_id: str) -> str:
    """Запасной вариант выжимки: разделы `STATE.md` комнаты из `cfg.vault_dir`."""
    folder = _vault_room_dir(cfg, room_id)
    if folder is None:
        return ""
    text = _read_text(folder / "STATE.md")
    if not text.strip():
        return ""
    out: list[str] = []
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = _fold(stripped[3:]).strip()
            continue
        if not current or current not in _SECTION_KEYS:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("_"):
            continue
        out.append(stripped)
        if len(out) >= 14:
            break
    return _clamp("\n".join(out), MAX_SUMMARY_CHARS)


def project_summary(cfg, room_id: str) -> str:
    """Выжимка проекта: `vault_mirror.context_summary`, иначе `STATE.md` комнаты."""
    try:
        from . import vault_mirror as mirror_mod
    except Exception:  # noqa: BLE001 - модуль зеркала пишет сосед, его может ещё не быть
        mirror_mod = None
    getter = getattr(mirror_mod, "context_summary", None) if mirror_mod is not None else None
    if callable(getter):
        try:
            got = getter(cfg, room_id, MAX_SUMMARY_CHARS)
        except Exception:  # noqa: BLE001
            got = ""
        if isinstance(got, str) and got.strip():
            return _clamp(_scrub(got).strip(), MAX_SUMMARY_CHARS)
    return _state_md_summary(cfg, room_id)


# ---------------------------------------------------------------- похожие запросы
def _note_path(cfg, room_id: str, rec: dict) -> str:
    """Путь к заметке запроса в Vault, если она уже есть (`Запросы/… (<id>).md`)."""
    folder = _vault_room_dir(cfg, room_id)
    if folder is None:
        return ""
    notes = folder / NOTE_DIR
    root_id = str(rec.get("root_id") or "")
    stem = ""
    try:
        from . import vault_mirror as mirror_mod
    except Exception:  # noqa: BLE001 - имя заметки задаёт соседний модуль
        mirror_mod = None
    maker = getattr(mirror_mod, "note_stem", None) if mirror_mod is not None else None
    if callable(maker):
        try:
            got = maker(root_id, rec.get("title") or "", rec.get("created_at") or 0)
        except Exception:  # noqa: BLE001
            got = ""
        if isinstance(got, str) and got.strip():
            stem = got.strip()
            candidate = notes / f"{stem}.md"
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                pass
    short = _short_id(root_id)
    if not short:
        return ""
    try:
        found = sorted(p for p in notes.glob("*.md") if f"({short})" in p.name)
    except OSError:
        return ""
    return str(found[0]) if found else ""


def _answer_from_note(path: str) -> str:
    """Первая строка раздела «Ответ» заметки запроса (если заметка уже есть)."""
    if not path:
        return ""
    text = _read_text(Path(path), 40_000)
    if not text:
        return ""
    grab = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            grab = _fold(stripped[3:]).startswith("ответ")
            continue
        if grab and stripped and not stripped.startswith(("#", ">", "<!--")):
            return _one_line(stripped, MAX_SUMMARY_LINE)
    return ""


def _summary_line(cfg, room_id: str, rec: dict, note_path: str) -> str:
    """Одна строка итога: из реестра, из заметки Vault или, в крайнем случае, из текста."""
    stored = rec.get("summary")
    if isinstance(stored, str) and stored.strip():
        return "Итог: " + _one_line(stored, MAX_SUMMARY_LINE)
    from_note = _answer_from_note(note_path)
    if from_note:
        return "Итог: " + from_note
    first = str(rec.get("text") or "").strip().splitlines()
    if first:
        return "Запрос: " + _one_line(first[0], MAX_SUMMARY_LINE)
    return "Итог: пока не записан."


def similar_requests(cfg, room_id: str, text: str, limit: int = DEFAULT_LIMIT) -> list:
    """До `limit` похожих прошлых запросов комнаты по пересечению основ слов.

    Возвращает `[{root_id, title, summary_line, note_path}]`; `note_path` — пустая
    строка, если заметки в Vault ещё нет. Сортировка: сначала больше совпадений,
    затем свежие, затем `root_id` — чтобы результат не «плавал» между вызовами.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(0, min(limit, DEFAULT_LIMIT))
    if not limit:
        return []
    query = _words(text)
    if not query:
        return []
    scored: list[tuple] = []
    for rec in _requests(cfg, room_id):
        candidate = _words(f"{rec.get('title') or ''} {rec.get('text') or ''}")
        if not candidate:
            continue
        score = len(query & candidate)
        if score <= 0:
            continue
        try:
            created = int(rec.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0
        scored.append((-score, -created, str(rec.get("root_id") or ""), rec))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    out: list[dict] = []
    for _score, _created, root_id, rec in scored[:limit]:
        note = _note_path(cfg, room_id, rec)
        out.append({
            "root_id": root_id,
            "title": _one_line(rec.get("title") or root_id, MAX_TITLE_CHARS),
            "summary_line": _summary_line(cfg, room_id, rec, note),
            "note_path": note,
        })
    return out


# ---------------------------------------------------------------- правило файлов
def _request_subdir(text: str) -> str:
    """Подпапка запроса: `project_dir.request_subdir`, иначе `<дата> <название>`."""
    title = _one_line(text, 60) or "Запрос"
    try:
        from . import project_dir as pd_mod
    except Exception:  # noqa: BLE001 - модуль папки проекта пишет сосед
        pd_mod = None
    maker = getattr(pd_mod, "request_subdir", None) if pd_mod is not None else None
    if callable(maker):
        try:
            got = maker(title, int(time.time()))
        except Exception:  # noqa: BLE001
            got = ""
        if isinstance(got, str) and got.strip():
            return got.strip()
    return _win_safe(f"{_today()} {title}")[:80]


def files_rule(cfg, room: dict, text: str = "") -> str:
    """Правило Д2: куда складывать итоговые файлы (папка проекта либо Vault/Материалы)."""
    room = room if isinstance(room, dict) else {}
    project_dir = _one_line(room.get("project_dir") or "", MAX_PATH_CHARS)
    if not project_dir:
        folder = _vault_room_dir(cfg, str(room.get("id") or ""))
        tail = _join_win(str(folder), MATERIALY) if folder is not None else MATERIALY + "\\"
        return (
            "Папка проекта не задана — итоговые файлы сохраняй в Vault, в папку комнаты "
            f"Материалы\\ (`{tail}`)."
        )
    subdir = _request_subdir(text)
    target = _join_win(project_dir, subdir)
    return (
        f"Папка проекта: {project_dir}. Все итоговые файлы (отчёты, таблицы, документы) "
        f"сохраняй в `{target}` и перечисли пути в artifacts. Дублируй суть в Obsidian "
        f"(заметка запроса ссылается на папку). Исключение — только если Михаил явно "
        f"указал другое место."
    )


# ---------------------------------------------------------------- сборка блока
def _describe(room: dict, room_id: str, title: str) -> str:
    """Описание комнаты: поле комнаты, иначе — кто руководит и сколько участников."""
    for key in ("description", "about", "subtitle", "note", "purpose"):
        value = room.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value, MAX_DESC_CHARS)
    parts = []
    orchestrator = str(room.get("orchestrator") or "").strip()
    if orchestrator:
        parts.append(f"руководитель — {orchestrator}")
    members = room.get("members") or []
    if isinstance(members, list) and members:
        parts.append(f"участников: {len(members)}")
    board = str(room.get("board") or "").strip()
    if board:
        parts.append(f"доска «{board}»")
    if not parts:
        return ""
    text = "; ".join(parts) + "."
    return _one_line(text[:1].upper() + text[1:], MAX_DESC_CHARS)


def _context_files(room: dict) -> list:
    raw = room.get("context_files") or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        path = _safe_path(item)
        if path and path not in out:
            out.append(path)
        if len(out) >= MAX_FILES:
            break
    return out


def notes_section(cfg, room, limit: int = MAX_NOTES_CHARS) -> str:
    """Раздел «Особые указания участникам этой комнаты» (WAVE9 §9).

    Собирает `server/agent_notes.render_block`; нет модуля/указаний — пустая строка,
    поэтому блок контекста выглядит как раньше. Ошибки не мешают созданию запроса.
    """
    try:
        try:
            from . import agent_notes as notes_mod
        except ImportError:  # pragma: no cover - плоский импорт
            import agent_notes as notes_mod  # type: ignore
        render = getattr(notes_mod, "render_block", None)
        if not callable(render):
            return ""
        got = render(cfg, room if isinstance(room, dict) else {}, limit)
        return _scrub(got).strip() if isinstance(got, str) else ""
    except Exception:  # noqa: BLE001 - указания не должны ломать карточку
        return ""


def _render(room_id: str, title: str, desc: str, summary: str, summary_path: str,
            files: list, similar: list, rule: str, notes: str = "") -> str:
    lines = [CONTEXT_OPEN]
    head = f"Комната: «{title}»" if title else "Комната"
    if room_id:
        head += f" ({room_id})"
    if desc:
        head += f". {desc}"
    lines.append(head)

    lines.append("")
    if summary:
        lines.append(f"Выжимка проекта (CONTEXT.md: {summary_path}):")
        lines.extend(summary.splitlines())
    elif summary_path:
        lines.append(f"Выжимка проекта пока пуста — CONTEXT.md: {summary_path} "
                     f"(появится после первого разбора запросов).")
    else:
        lines.append("Выжимка проекта пока пуста.")

    if notes:
        lines.append("")
        lines.extend(notes.splitlines())

    lines.append("")
    if files:
        lines.append("Файлы контекста комнаты (читать первыми):")
        lines.extend(f"- {path}" for path in files)
    else:
        lines.append("Файлы контекста комнаты не заданы.")

    lines.append("")
    if similar:
        lines.append("Похожие прошлые запросы (не начинай с нуля — посмотри, что уже решили):")
        for index, item in enumerate(similar, 1):
            row = f"{index}. «{item['title']}» — {item['summary_line']}"
            if item.get("note_path"):
                row += f" — {item['note_path']}"
            lines.append(row)
    else:
        lines.append("Похожих прошлых запросов не нашлось — это первый запрос по этой теме.")

    lines.append("")
    lines.append(INSTRUCTION)
    lines.append("")
    lines.append(rule)
    lines.append(CONTEXT_CLOSE)
    return "\n".join(lines)


def build_context_block(cfg, room, text) -> str:
    """Блок «КОНТЕКСТ КОМНАТЫ» для тела карточки (≤ 4 000 знаков, детерминированный).

    `room` — словарь комнаты (`id`, `title`, `description`, `context_files`,
    `project_dir`), `text` — текст запроса Михаила (для поиска похожих и для
    названия подпапки). Пустая комната — аккуратный блок без падений.
    """
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "").strip()
    title = _one_line(room.get("title") or room_id, MAX_TITLE_CHARS)
    desc = _describe(room, room_id, title)
    summary = project_summary(cfg, room_id) if room_id else ""
    folder = _vault_room_dir(cfg, room_id) if room_id else None
    summary_path = _one_line(str(folder / "CONTEXT.md"), MAX_PATH_CHARS) if folder is not None else ""
    files = _context_files(room)
    similar = similar_requests(cfg, room_id, text, DEFAULT_LIMIT) if room_id else []
    rule = files_rule(cfg, room, text)
    notes = notes_section(cfg, room, MAX_NOTES_CHARS)

    counts = sorted({len(similar), min(3, len(similar)), min(2, len(similar)), min(1, len(similar)), 0},
                    reverse=True)
    caps = (MAX_SUMMARY_CHARS, 900, 500, 200, 0)
    notes_caps = (MAX_NOTES_CHARS, 1500, 700, 300, 0) if notes else (0,)
    block = ""
    for notes_cap in notes_caps:
        piece = notes if not notes_cap else notes_section(cfg, room, notes_cap)
        if not notes_cap:
            piece = ""
        for count in counts:
            for cap in caps:
                block = _render(room_id, title, desc, _clamp(summary, cap), summary_path,
                                files, similar[:count], rule, piece)
                if len(block) <= MAX_BLOCK_CHARS:
                    return block
    return block[: MAX_BLOCK_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------- вставка в тело
def _visible(body: str) -> str:
    """`contract.visible_text`, если он есть (иначе — тело как есть)."""
    try:
        from . import contract as contract_mod
    except Exception:  # noqa: BLE001
        return body
    getter = getattr(contract_mod, "visible_text", None)
    if not callable(getter):
        return body
    try:
        return str(getter(body) or "")
    except Exception:  # noqa: BLE001
        return body


def insert_into_body(body, block) -> str:
    """Поставить блок в тело карточки: после текста Михаила, до правил контракта.

    Тело строит `contract.build_request_body` (текст → шапка → контракт). Блок
    встаёт сразу после открывающей строки контракта: для агента он идёт до правил,
    а `visible_text` режет тело по этой же строке, поэтому в интерфейс блок не
    попадает. Если читатель уже умеет резать по маркеру блока (проба `_visible`),
    блок встанет прямо перед контрактом. Повторная вставка не дублируется.
    """
    text = body if isinstance(body, str) else str(body or "")
    piece = (block or "").strip("\n")
    if not piece or CONTEXT_OPEN in text:
        return text
    index = text.find(CONTRACT_MARKER)
    if index < 0:
        return (text.rstrip() + "\n\n" + piece).strip("\n") if text.strip() else piece
    if not _visible(piece).strip():
        return text[:index] + piece + "\n\n" + text[index:]
    line_end = text.find("\n", index)
    if line_end < 0:
        return text + "\n" + piece
    return text[: line_end + 1] + piece + "\n" + text[line_end + 1:]
