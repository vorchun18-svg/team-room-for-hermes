"""Метаданные ответа → объекты API (RequestDetail/RequestSummary) — чистые функции.

Второй слой чтения: здесь только преобразование уже канонической metadata
(``server.normalize.normalize_metadata``) в «плоские» объекты контракта —
шаги, вопросы, решения, фазы, артефакты, отчёт, прогресс, блок критика.

Никакого sqlite, сети и состояния: всё принимает аргументы и возвращает
новые dict'ы. Поэтому модуль легко тестировать и он не тянет за собой
``engine_read`` (обратной зависимости нет).
"""
from __future__ import annotations

import re
from pathlib import Path

try:  # пакетный и «плоский» импорт (python -m unittest из корня проекта)
    from .config import (ALLOWED_EXT, external_allowed, is_denied_name, mirror_root_of,
                         path_allowed, project_root_of, room_roots)
    from . import phase_book as _phases
except ImportError:  # pragma: no cover
    from config import (ALLOWED_EXT, external_allowed, is_denied_name,  # type: ignore
                        mirror_root_of, path_allowed, project_root_of, room_roots)
    import phase_book as _phases  # type: ignore

PREVIEW_MAX = 200

# --- артефакты запроса (W20-A1, B-43, контракт WAVE20 §1.4) --------------------
#: Больше 200 файлов в блоке «Артефакты» не показываем (§1.4): список нужен,
#: чтобы найти свой файл, а не читать весь диск.
ARTIFACT_LIMIT = 200
#: Потолок обхода папок материалов: сколько записей смотрим за одно открытие
#: страницы (защита от папки-свалки на десятки тысяч файлов; на обычном проекте
#: не срабатывает — см. docs/status/review-wave1.md).
VISIT_LIMIT = 5000
#: Глубина сканирования папки материалов запроса: сама папка и две подпапки.
ARTIFACT_DEPTH = 2
#: Группы блока в порядке показа (итоговые — первыми).
ARTIFACT_GROUPS = ("final", "material", "service")
#: Слова в имени файла, по которым он итоговый (B-43: «письмо», «краткая сводка»).
#: «ответ» сюда НЕ входит: «05-Проект-ответа» — рабочий материал, а не итог.
FINAL_WORDS = ("итог", "письмо", "кратко", "final", "summary", "letter")
#: Служебные файлы приложения и агентов: `_state.json`, `_index.html` и подобные.
SERVICE_EXT = (".json", ".html", ".htm", ".log")
#: Подкаталог зеркала комнаты с заметками запросов — это не материалы запроса.
MIRROR_NOTE_DIR = "Запросы"
#: Чужой id запроса в имени файла (`t_1a2b3c4d`) — файл другой работы.
_REQUEST_ID_RE = re.compile(r"t_[0-9a-f]{8}", re.I)
# Этапы проекта — свойство КОМНАТЫ (W17-R §1, B-8): списка в коде больше нет.
# Имена оставлены пустыми списками только для совместимости импортов: код,
# который ещё спрашивает `PHASES`, получает «этапов нет» и не рисует чужих этапов.
PHASES: list = []
PHASE_IDS: list = []
CRITIC_STATES = ("off", "waiting", "reviewing", "approved", "revised", "failed")
CLOSED = ("done", "archived")
ACTIVE = ("ready", "running", "review")


def as_list(value) -> list:
    """Значение → список: None/'' → [], скаляр → [значение]."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


# ------------------------------------------------------------------ поля ответа ---
def stage(canon: dict, phases=None) -> dict:
    """Канонический stage → Stage API §3.4: только этапы КОМНАТЫ (W17-R §1).

    ``phases`` — список этапов комнаты (`[{id, title}]`, `server/phase_book.py`).
    Нет этапов у комнаты — пустой stage: полоса этапов и чип «этап» не рисуются.
    """
    return _phases.stage(phases, canon)


def steps(canon: dict) -> list:
    out = []
    for i, item in enumerate(as_list(canon.get("next_steps")), 1):
        item = {"text": item} if isinstance(item, str) else item
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            out.append({"id": item.get("id") or f"ns_{i}", "text": str(item["text"]).strip(),
                        "owner": item.get("owner"), "due": item.get("due"),
                        "state": item.get("state") or "open"})
    return out


def questions(canon: dict) -> list:
    out = []
    for i, item in enumerate(as_list(canon.get("open_questions")), 1):
        item = {"text": item, "for": "mikhail"} if isinstance(item, str) else item
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            out.append({"id": item.get("id") or f"q_{i}", "text": str(item["text"]).strip(),
                        "for": item.get("for") or "mikhail", "answered": bool(item.get("answered")),
                        "answer": item.get("answer"), "answered_at": item.get("answered_at")})
    return out


def decisions(canon: dict, source: str, at) -> list:
    out = []
    for i, item in enumerate(as_list(canon.get("decisions")), 1):
        item = {"text": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        # W20-D1 §1.2 (B-37): статусов решения больше — снято/устарело/обсуждаем.
        status = item.get("status") if item.get("status") in (
            "proposed", "accepted", "rejected", "discuss", "dismissed", "superseded") else "proposed"
        out.append({"id": item.get("id") or f"d_{i}", "text": str(item["text"]).strip(),
                    "why": item.get("why"), "status": status, "at": item.get("at") or at,
                    "source_request": source})
    return out


def future(canon: dict) -> list:
    return [str(x).strip() for x in as_list(canon.get("future_questions")) if str(x).strip()]


def all_questions(canon: dict, canons: list) -> list:
    """Открытые вопросы запроса: из ответа + из карточек-детей (id уникальны)."""
    out, seen_ids, seen_text = [], set(), set()

    def add(items):
        for q in items:
            text = (q.get("text") or "").strip()
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            qid = str(q.get("id") or f"q_{len(out) + 1}")
            while qid in seen_ids:
                qid += "_2"
            seen_ids.add(qid)
            out.append(dict(q, id=qid))

    add(questions(canon))
    for node, node_canon in canons:
        if node["role"] in ("root", "synthesizer"):
            continue
        add(questions(node_canon))
    return out


def stage_filled(canon: dict) -> bool:
    st = canon.get("stage")
    return isinstance(st, dict) and any(p.get("status") in ("done", "active", "blocked")
                                        for p in as_list(st.get("phases")) if isinstance(p, dict))


def merge_canon(primary: dict, canons: list) -> dict:
    """Заполнить пустые разделы ответа из metadata корня/синтезатора/детей."""
    out = dict(primary)
    for key in ("next_steps", "open_questions", "decisions", "stage", "future_questions",
                "artifacts", "report"):
        if out.get(key) and (key != "stage" or stage_filled(out)):
            continue
        for _, node_canon in canons:
            if key == "stage":
                if stage_filled(node_canon):
                    out[key] = node_canon["stage"]
                    break
            elif node_canon.get(key):
                out[key] = node_canon[key]
                break
    return out


def kind_of(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return {"md": "md", "json": "json", "html": "html", "htm": "html", "csv": "csv", "pdf": "pdf",
            "docx": "docx", "xlsx": "xlsx", "pptx": "pptx", "png": "image", "jpg": "image",
            "jpeg": "image", "gif": "image", "webp": "image", "svg": "image"}.get(ext, "other")


def _is_file(path: str) -> bool:
    """Есть ли файл на диске (без исключений платформы и прав)."""
    try:
        return Path(str(path)).is_file()
    except (OSError, ValueError):
        return False


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def log_outside_artifacts(cfg, room, items) -> list:
    """Записать в журнал комнаты файлы вне папки проекта (B-27). Ошибки не всплывают."""
    if not isinstance(room, dict) or not room.get("id") or not items:
        return []
    try:
        try:
            from . import agent_notes as notes
        except ImportError:  # pragma: no cover
            import agent_notes as notes  # type: ignore
        return notes.log_artifact_outside(cfg, str(room["id"]), items)
    except Exception:  # noqa: BLE001 — журнал вторичен, чтение важнее
        return []


def _norm_path(path) -> str:
    """Путь к файлу в одном виде: прямые слэши (сравнение и ключ дедупликации)."""
    return str(path or "").replace("\\", "/")


def _file_stat(path: Path):
    """`(mtime, size)` файла; `(None, None)`, если файла нет или нет прав."""
    try:
        stat = Path(path).stat()
        return int(stat.st_mtime), int(stat.st_size)
    except (OSError, ValueError):
        return None, None


def group_of(path, explicit=None) -> str:
    """Группа артефакта: ``final`` | ``material`` | ``service`` (W20-A1, B-43).

    Явная группа из метаданных карточки важнее имени файла. Иначе: служебные
    файлы приложения/агентов (`_state.json`, `_index.html`) — ``service``;
    письмо, итог и краткая сводка — ``final``; всё остальное — ``material``.
    """
    if explicit in ARTIFACT_GROUPS:
        return str(explicit)
    name = str(Path(str(path or "")).name)
    low = name.lower()
    if low.startswith("_") and Path(low).suffix in SERVICE_EXT:
        return "service"
    stem = Path(low).stem
    if any(word in stem for word in FINAL_WORDS):
        return "final"
    return "material"


def _is_service_name(name: str) -> bool:
    """Служебный файл приложения/агента: `_state.json`, `_index.html` (группа service)."""
    low = str(name or "").lower()
    return low.startswith("_") and Path(low).suffix in SERVICE_EXT


def _scan_folder(root: Path, limit: int, request_id: str = "") -> list:
    """Файлы папки материалов запроса: глубина ≤ 2, только разрешённые типы имён.

    Возвращает ``[{path, dir, folder, mtime, size, name}]``, где ``folder`` —
    подпапка относительно ``root`` (пусто — файл лежит прямо в папке материалов).
    Чужие файлы (другой id запроса в имени) и запрещённые имена не попадают.

    Обход ограничен `VISIT_LIMIT` записями: папка проекта может оказаться огромной
    (свалка на 20 000 файлов), а деталь запроса читается на каждом открытии
    страницы — показ списка не должен ждать полного обхода.
    """
    out: list = []
    root = Path(root)
    visited = 0

    def walk(folder: Path, depth: int, rel: str) -> None:
        nonlocal visited
        if len(out) >= limit or visited >= VISIT_LIMIT:
            return
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            visited += 1
            if len(out) >= limit or visited > VISIT_LIMIT:
                return
            try:
                if entry.is_dir():
                    if depth < ARTIFACT_DEPTH and not entry.name.startswith("."):
                        walk(entry, depth + 1, (rel + "/" + entry.name).strip("/"))
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if entry.suffix.lower() not in ALLOWED_EXT or is_denied_name(entry.name):
                continue
            if request_id:
                foreign = [m for m in _REQUEST_ID_RE.findall(entry.name)
                           if m.lower() != request_id.lower()]
                if foreign:
                    continue           # файл другой работы в общей папке
            mtime, size = _file_stat(entry)
            out.append({"path": _norm_path(entry), "dir": _norm_path(folder),
                        "folder": rel, "mtime": mtime, "size": size, "name": entry.name})
    walk(root, 0, "")
    return out


def _dirs_with_id(root: Path, request_id: str) -> list:
    """Подпапки ``root`` (глубина ≤ 2), в названии которых есть id запроса.

    Обход ограничен `VISIT_LIMIT` записями (та же причина, что у `_scan_folder`):
    перечисление десятков тысяч подпапок на каждом открытии страницы недопустимо.
    """
    found: list = []
    needle = str(request_id or "").strip().lower()
    if not needle or root is None:
        return found
    visited = 0

    def walk(folder: Path, depth: int) -> None:
        nonlocal visited
        if visited >= VISIT_LIMIT:
            return
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            visited += 1
            if visited > VISIT_LIMIT:
                return
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            if needle in entry.name.lower():
                found.append(entry)
            if depth < ARTIFACT_DEPTH:
                walk(entry, depth + 1)
    walk(Path(root), 1)
    return found


def material_roots(cfg, room, request_id: str = "", known_paths=()) -> list:
    """Где искать материалы запроса (§1.4): папка материалов + подпапки проекта.

    Источники (только внутри разрешённых корней комнаты):

    * подпапки папки проекта комнаты и зеркала, в названии которых есть id запроса;
    * папка, в которой уже лежат файлы из метаданных карточек (это и есть «папка
      материалов запроса» — так её видит команда);
    * заметки запросов в зеркале (`Запросы/`) материалами не считаются.
    """
    if not isinstance(room, dict):
        return []
    out: list = []
    seen: set = set()
    root_paths = [p for p in (project_root_of(cfg, room), mirror_root_of(cfg, room)) if p is not None]

    def add(folder) -> None:
        try:
            real = Path(folder).resolve()
        except (OSError, ValueError, RuntimeError):
            return
        if real in seen or real in root_paths:
            return
        try:
            if not real.is_dir():
                return
        except OSError:
            return
        if real.name == MIRROR_NOTE_DIR:
            return
        seen.add(real)
        out.append(real)

    for base in root_paths:
        for folder in _dirs_with_id(base, request_id):
            add(folder)
    for raw in known_paths:
        try:
            candidate = Path(str(raw)).resolve(strict=False)
        except (OSError, ValueError, RuntimeError):
            continue
        for base in root_paths:
            if not _under(candidate, base):
                continue
            try:
                rel = candidate.relative_to(base)
            except ValueError:
                continue
            if len(rel.parts) >= 2:
                add(base / rel.parts[0])
    return out


def _folder_of(path: str, roots) -> str:
    """Относительная подпапка файла внутри ближайшей папки материалов (`''` — нет)."""
    for root in roots:
        if not _under(Path(path), Path(root)):
            continue
        try:
            rel = Path(path).relative_to(root)
        except ValueError:
            continue
        return "/".join(rel.parts[:-1])
    return ""


def _sorted_artifacts(items: list) -> list:
    """Порядок §1.4: ``final`` сначала, дальше по времени изменения (новые выше)."""
    return sorted(items, key=lambda a: (0 if a.get("group") == "final" else 1,
                                        -(a.get("mtime") or 0),
                                        str(a.get("title") or "").lower()))


def _fold_folders(items: list) -> list:
    """Свернуть подпапки в элементы `{kind:'folder', …}` (B-43, §1.4).

    Папка стоит там, где стоял её самый свежий файл; внутри — все её файлы
    в том же порядке. Группа папки — группа самого свежего файла.
    """
    groups: dict = {}
    for item in items:
        folder = str(item.get("folder") or "")
        if folder:
            groups.setdefault(folder, []).append(item)
    if not groups:
        return items
    out, done = [], set()
    for item in items:
        folder = str(item.get("folder") or "")
        if not folder:
            out.append(item)
            continue
        if folder in done:
            continue
        done.add(folder)
        members = groups[folder]
        member_groups = {str(m.get("group") or "material") for m in members}
        folder_group = member_groups.pop() if len(member_groups) == 1 else "material"
        out.append({"kind": "folder", "title": folder.split("/")[-1], "folder": folder,
                    "path": members[0].get("dir") or "", "exists": True, "size": None,
                    "state": "ok", "outside_project": False, "reason": None,
                    "mtime": max([int(m.get("mtime") or 0) for m in members] or [0]) or None,
                    "newer_than_svod": any(bool(m.get("newer_than_svod")) for m in members),
                    "group": folder_group,
                    "count": len(members), "items": members})
    return out


def artifacts(cfg, canon: dict, warnings: list, room=None, request_id=None,
              svod_at=None) -> list:
    """Artifact[] запроса (W20-A1, B-43, §1.4).

    Источники: метаданные карточек + папка материалов запроса (зеркало комнаты и
    подпапки проекта с id запроса). Только внутри ``config.room_roots``, глубина
    ≤ 2, ≤ 200 файлов. У файла — ``mtime``, ``size``, ``group``, ``folder`` и
    ``newer_than_svod`` (изменён позже последней версии свода). Подпапки
    возвращаются элементом ``{kind:'folder', title, count, items}``.

    Состояния прежние (B-22/B-27): ``ok`` — файл есть и лежит в разрешённом месте;
    ``missing`` — «Файл удалён или перемещён»; ``outside`` — файл есть, но вне
    папки проекта (предупреждение + журнал комнаты); ``denied`` — открыть нельзя.
    """
    room = room if isinstance(room, dict) else None
    rid = str(request_id or canon.get("id") or "").strip()
    svod_ts = int(svod_at) if isinstance(svod_at, int) and svod_at else None
    roots = room_roots(cfg, room) if room is not None else None
    local = [root for root in (project_root_of(cfg, room), mirror_root_of(cfg, room))
             if root is not None] if room is not None else []

    entries: dict = {}          # нормализованный путь → элемент
    raw_items = []
    for item in as_list(canon.get("artifacts")):
        item = {"path": item} if isinstance(item, str) else item
        if isinstance(item, dict) and item.get("path"):
            raw_items.append(item)

    scan_dirs = material_roots(cfg, room, rid, [str(i["path"]) for i in raw_items])
    outside = []

    # 1) метаданные карточек: прежние проверки существования и места сохранения
    for item in raw_items:
        path = _norm_path(item["path"])
        if path in entries:
            continue
        real = path_allowed(cfg, path, roots=roots)
        state, exists, size, reason, resolved = "ok", True, None, None, real
        if real is not None:
            mtime, size = _file_stat(real)
        else:
            on_disk = external_allowed(cfg, path) if _is_file(path) else None
            resolved = on_disk
            if on_disk is not None:
                if local and not any(_under(on_disk, root) for root in local):
                    state, reason = "outside", "Файл сохранён вне папки проекта"
                    outside.append({"path": path, "title": item.get("title") or Path(path).name,
                                    "resolved": str(on_disk)})
                else:
                    state = "denied"
                    reason = "Файл нельзя открыть: запрещённый тип или имя"
                    exists = False
                mtime, size = _file_stat(on_disk)
            elif _is_file(path):
                state, exists = "denied", False
                reason = "Файл нельзя открыть: запрещённый тип или имя"
                mtime = None
            else:
                state, exists = "missing", False
                reason = "Файл удалён или перемещён"
                mtime = None
        entries[path] = {
            "path": path, "title": item.get("title") or Path(path).name,
            "kind": item.get("kind") or kind_of(path), "exists": bool(exists),
            "size": size, "state": state, "outside_project": state == "outside",
            "reason": reason, "mtime": mtime,
            "folder": _folder_of(path, scan_dirs),
            "dir": resolved.parent.as_posix() if resolved is not None else "",
            "group": group_of(path, item.get("group")), "from_card": True,
        }
        if state == "missing":
            warnings.append({"code": "missing_artifact",
                             "message": f"Файл удалён или перемещён: {Path(path).name}",
                             "detail": {"path": path, "state": state}})
        elif state == "denied":
            warnings.append({"code": "artifact_denied",
                             "message": f"Файл нельзя открыть: {Path(path).name} — "
                                        f"запрещённый тип или имя",
                             "detail": {"path": path, "state": state}})
        elif state == "outside":
            warnings.append({"code": "artifact_outside", "message": f"{reason}: {path}",
                             "detail": {"path": path, "state": state,
                                        "outside_project": True}})
    if outside:
        log_outside_artifacts(cfg, room, outside)

    # 2) папка материалов запроса: файлы, которых нет в метаданных карточек
    budget = max(0, ARTIFACT_LIMIT - len(entries))
    for folder in scan_dirs:
        if budget <= 0:
            break
        for found in _scan_folder(folder, budget, rid):
            path = found["path"]
            if path in entries:
                # файл уже известен из карточки — дополняем папку и время
                entry = entries[path]
                if entry.get("mtime") is None:
                    entry["mtime"] = found["mtime"]
                if not entry.get("folder"):
                    entry["folder"] = found["folder"]
                continue
            real = path_allowed(cfg, path, roots=roots)
            if real is None:
                continue
            entries[path] = {
                "path": path, "title": Path(path).stem or Path(path).name,
                "kind": kind_of(path), "exists": True, "size": found["size"],
                "state": "ok", "outside_project": False, "reason": None,
                "mtime": found["mtime"], "folder": found["folder"],
                "dir": found["dir"], "group": group_of(path), "from_card": False,
            }
            budget -= 1
            if budget <= 0:
                break

    items = list(entries.values())
    for item in items:
        item["newer_than_svod"] = bool(svod_ts and item.get("mtime") and
                                       int(item["mtime"]) > svod_ts)
    ordered = _fold_folders(_sorted_artifacts(items))
    return ordered[:ARTIFACT_LIMIT] if len(ordered) > ARTIFACT_LIMIT else ordered


def report(cfg, canon: dict) -> dict:
    raw = canon.get("report")
    path = raw.get("path") if isinstance(raw, dict) else raw
    title = raw.get("title") if isinstance(raw, dict) else None
    if not path:
        return {"available": False, "kind": None, "path": None, "title": None}
    path = str(path).replace("\\", "/")
    real = path_allowed(cfg, path)
    if not real:
        return {"available": False, "kind": None, "path": path, "title": title}
    return {"available": True,
            "kind": "html" if Path(path).suffix.lower() in (".html", ".htm") else "spec",
            "path": str(real), "title": title or Path(path).stem}


def progress(nodes: list, status: str) -> dict:
    children = nodes[1:]
    total, done = len(children), sum(1 for n in children if n.get("status") in CLOSED)
    percent = 100 if status == "done" else (0 if total == 0 else int(100 * done / total))
    running = sorted({n["assignee"] for n in children
                      if n.get("status") in ACTIVE and n.get("assignee")})
    return {"done": done, "total": total, "percent": percent, "running": running}


def preview(text) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[#*_`>~\[\]()]+", " ", str(text))).strip()[:PREVIEW_MAX]


def critic_brief(critic) -> dict:
    """Сжатый блок критика для RequestSummary: {enabled, state, verdict, round}."""
    critic = critic if isinstance(critic, dict) else {}
    state = str(critic.get("state") or "off")
    verdict = critic.get("verdict")
    return {"enabled": bool(critic.get("enabled")), "state": state if state in CRITIC_STATES else "off",
            "verdict": verdict if verdict in ("approve", "revise") else None,
            "round": int(critic.get("round") or 0)}


__all__ = ["as_list", "stage", "steps", "questions", "decisions", "future", "all_questions",
           "stage_filled", "merge_canon", "kind_of", "artifacts", "report", "progress",
           "preview", "critic_brief", "PHASES", "PHASE_IDS", "CRITIC_STATES", "PREVIEW_MAX",
           "log_outside_artifacts", "group_of", "material_roots", "ARTIFACT_LIMIT",
           "ARTIFACT_DEPTH", "ARTIFACT_GROUPS"]
