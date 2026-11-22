"""Хранилище: где что лежит, что можно убрать и когда (W29-P8).

Три правила, вокруг которых построен модуль:

1. **Убираем только своё.** Корни очистки перечислены в коде (диктовки, логи
   приложения, бэкапы Team Room, временные папки приёмки, кэш сборок). Путь,
   пришедший снаружи, принимается только если он лежит ВНУТРИ одного из этих
   корней — и проверка идёт по resolved-пути без разворота ссылок/junction.
2. **Чужое не трогаем никогда.** Vault (Obsidian), папка Hermes и материалы,
   принесённые человеком, попадают в отчёт только цифрами: «занято», «можно
   заменить ссылками». Удаления для них нет — только список и кнопка-предложение.
3. **Сухой прогон по умолчанию.** ``clean()`` без явного ``dry_run=False``
   ничего не удаляет и возвращает ровно тот же список, что удалил бы.

Материалы запросов не копируются в Vault целиком: крупный или нетекстовый файл
описывается заметкой-ссылкой (путь, размер, хэш, «что это»), копия остаётся
только для небольших текстовых файлов (``material_plan``).

Всё локально, без сети. Запись — только в ``data/state/storage.json`` (настройки)
и внутрь перечисленных корней (по явному действию человека).
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import time
from pathlib import Path

def _proc_run(argv, **kw):
    """Запуск процесса только через server/proc.py (без мигающего окна)."""
    try:
        from . import proc as proc_mod
    except ImportError:             # запуск как скрипт
        import proc as proc_mod     # type: ignore
    return proc_mod.run(argv, **kw)


try:  # пакетный и «плоский» импорт
    from . import config as config_mod
    from . import statefiles
except ImportError:  # pragma: no cover - запуск как скрипт
    import config as config_mod          # type: ignore
    import statefiles                    # type: ignore

MB = 1024 * 1024
KB = 1024

#: Расширения, которые считаем текстом (маленькие копируются как раньше).
TEXT_EXT = frozenset({
    ".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".ini", ".cfg",
    ".conf", ".html", ".htm", ".xml", ".svg", ".py", ".js", ".ts", ".css", ".sql",
})

#: «Что это» по расширению — для заметки-ссылки в Vault.
KIND_RU = {
    ".pdf": "PDF-документ",
    ".doc": "документ Word", ".docx": "документ Word", ".rtf": "документ Word",
    ".xls": "таблица Excel", ".xlsx": "таблица Excel", ".ods": "таблица",
    ".ppt": "презентация", ".pptx": "презентация",
    ".eml": "письмо (файл .eml)", ".msg": "письмо Outlook",
    ".zip": "архив", ".rar": "архив", ".7z": "архив", ".tar": "архив", ".gz": "архив",
    ".mp3": "аудиозапись", ".m4a": "аудиозапись", ".ogg": "аудиозапись",
    ".webm": "аудиозапись", ".wav": "аудиозапись", ".opus": "аудиозапись",
    ".png": "изображение", ".jpg": "изображение", ".jpeg": "изображение",
    ".gif": "изображение", ".webp": "изображение",
    ".csv": "таблица (CSV)", ".md": "текст", ".txt": "текст", ".json": "данные (JSON)",
}

#: Настройки хранения. Значения — «сколько хранить»; 0 у дней/часов = «сразу».
DEFAULT_POLICY = {
    "voice_hours": 72,          # голосовые диктовки (длинные записи)
    "voice_max": 30,            # и не больше стольких записей
    "logs_days": 30,            # логи приложения
    "backups_keep": 5,          # бэкапы Team Room: последние N
    "backups_per_release": 1,   # + по одному на релиз
    "build_cache_days": 7,      # кэш сборок релиза
    "tmp_hours": 24,            # временные копии приложения (data/tmp)
    "accept_immediately": 1,    # временные папки песочниц/приёмки — сразу
    "copy_max_bytes": 2 * MB,   # что ещё копируем в Vault (иначе — ссылка)
}

#: Границы настройки: человек не может попросить «хранить вечно» минусом.
POLICY_LIMITS = {
    "voice_hours": (0, 24 * 365), "voice_max": (0, 5000), "logs_days": (0, 3650),
    "backups_keep": (1, 200), "backups_per_release": (0, 10), "build_cache_days": (0, 365),
    "tmp_hours": (0, 24 * 365), "accept_immediately": (0, 1),
    "copy_max_bytes": (64 * KB, 64 * MB),
}

MATERIALS_DIRNAME = "Материалы"
MAX_SCAN_ENTRIES = 300_000
_HASH_CAP = 64 * MB


class StorageError(Exception):
    """Ошибка хранилища: код + текст для человека."""

    def __init__(self, message: str, code: str = "bad_request", status: int = 400, detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = int(status)
        self.detail = detail or {}


# ------------------------------------------------------------------ размеры


def human_size(value) -> str:
    """«1,4 ГБ» / «336 МБ» / «8 КБ» — как в проводнике, но по-русски."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    if number < KB:
        return "%d Б" % int(number)
    for unit, size in (("ГБ", 1024 * MB), ("МБ", MB), ("КБ", KB)):
        if number >= size:
            text = ("%.1f" % (number / size)).rstrip("0").rstrip(".")
            return "%s %s" % (text.replace(".", ","), unit)
    return "%d Б" % int(number)


def kind_of(path) -> str:
    """«Что это» по расширению (для заметки-ссылки)."""
    suffix = Path(str(path or "")).suffix.lower()
    return KIND_RU.get(suffix, "файл .%s" % suffix.lstrip(".") if suffix else "файл")


def is_text_name(path) -> bool:
    return Path(str(path or "")).suffix.lower() in TEXT_EXT


# ------------------------------------------------------------------ настройки


def policy_path(cfg) -> Path:
    return Path(cfg.state_dir) / "storage.json"


def load_policy(cfg) -> dict:
    """Настройки хранения: значения по умолчанию + сохранённые, в границах."""
    data = statefiles.read_json(policy_path(cfg), default=None)
    out = dict(DEFAULT_POLICY)
    if isinstance(data, dict):
        for key, value in data.items():
            if key not in DEFAULT_POLICY:
                continue
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                continue
    for key, (low, high) in POLICY_LIMITS.items():
        try:
            out[key] = max(low, min(high, int(out.get(key, DEFAULT_POLICY[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULT_POLICY[key]
    return out


def save_policy(cfg, patch) -> dict:
    """Сохранить настройки хранения и вернуть итог (неизвестные ключи — ошибка)."""
    patch = patch if isinstance(patch, dict) else {}
    unknown = sorted(k for k in patch if k not in DEFAULT_POLICY)
    if unknown:
        raise StorageError("Неизвестная настройка хранения: %s" % ", ".join(unknown),
                           detail={"fields": unknown})
    current = load_policy(cfg)
    for key, value in patch.items():
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise StorageError("Настройка «%s» должна быть числом" % key,
                               detail={"field": key, "value": str(value)[:40]})
        low, high = POLICY_LIMITS[key]
        if number < low or number > high:
            raise StorageError("Настройка «%s»: допустимо от %d до %d" % (key, low, high),
                               detail={"field": key, "value": number})
        current[key] = number
    try:
        Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
        statefiles.write_json_atomic(policy_path(cfg), current)
    except OSError as exc:
        raise StorageError("Не удалось сохранить настройки хранения: %s" % exc,
                           code="storage_error", status=502) from exc
    return current


# ------------------------------------------------------------------ корни
#
# Все корни, внутри которых разрешено удаление, перечислены здесь. Всё, что вне
# этого списка (Vault, Hermes, папка программы, домашний каталог), удалить нельзя
# никаким запросом — только показать человеку список.


def voice_root(cfg) -> Path:
    return Path(cfg.data_dir) / "stt-recordings"


def logs_root(cfg) -> Path:
    return Path(cfg.log_dir)


def tmp_root(cfg) -> Path:
    return Path(cfg.tmp_dir)


def backups_root(cfg) -> Path:
    raw = str(os.environ.get("TEAMROOM_BACKUPS") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(cfg.data_dir) / "backups"


def accept_root(cfg) -> Path | None:
    """Временные папки песочниц/приёмки — только если путь задан явно."""
    raw = str(os.environ.get("TEAMROOM_ACCEPT_DIR") or "").strip()
    return Path(raw).expanduser() if raw else None


def build_cache_root(cfg) -> Path | None:
    raw = str(os.environ.get("TEAMROOM_BUILD_CACHE") or "").strip()
    return Path(raw).expanduser() if raw else None


def _resolve(path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(str(path)))


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(Path(root))
        return True
    except (ValueError, OSError):
        return False


def _is_link(path: Path) -> bool:
    """symlink или junction (Windows). Неизвестно — считаем ссылкой (безопаснее)."""
    try:
        if os.path.islink(path):
            return True
        checker = getattr(os.path, "isjunction", None)
        if checker is not None:
            return bool(checker(path))
        try:
            info = os.lstat(path)
        except OSError:
            return False
        return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def _vault_root(cfg) -> Path | None:
    root = config_mod.vault_root_dir(cfg)
    return _resolve(root) if root else None


def delete_roots(cfg) -> list[Path]:
    """Корни, внутри которых разрешено удаление (по категориям выше)."""
    out: list[Path] = []
    for candidate in (voice_root(cfg), logs_root(cfg), tmp_root(cfg), backups_root(cfg),
                      accept_root(cfg), build_cache_root(cfg)):
        if candidate is None:
            continue
        path = _resolve(candidate)
        if path not in out:
            out.append(path)
    return out


def forbidden_roots(cfg) -> list[Path]:
    """Что не удаляем никогда: Vault, Hermes, папка программы."""
    out: list[Path] = []
    for value in (_vault_root(cfg), getattr(cfg, "hermes_home", None), getattr(cfg, "app_dir", None)):
        if not value:
            continue
        path = _resolve(value)
        if path not in out:
            out.append(path)
    return out


def check_deletable(cfg, path) -> tuple[Path | None, str]:
    """``(путь, '')`` если удалять можно, иначе ``(None, причина для человека)``."""
    raw = str(path or "").strip()
    if not raw:
        return None, "пустой путь"
    target = _resolve(raw)
    vault = _vault_root(cfg)
    hermes = _resolve(getattr(cfg, "hermes_home", "") or "") if getattr(cfg, "hermes_home", None) else None
    app_dir = _resolve(getattr(cfg, "app_dir", "") or "") if getattr(cfg, "app_dir", None) else None
    if vault is not None and (_under(target, vault) or target == vault):
        return None, ("Это Vault (Obsidian) — там ваши заметки и документы, приложение их "
                      "не удаляет. Убрать можно вручную или заменой на ссылку.")
    if hermes is not None and (_under(target, hermes) or target == hermes):
        return None, "Это папка Hermes — чужие данные, приложение их не удаляет (только показывает размер)."
    if app_dir is not None and (_under(target, app_dir) or target == app_dir):
        return None, "Это папка самой программы — удалять из неё нельзя."
    inside = [root for root in delete_roots(cfg) if _under(target, root) and target != root]
    if not inside:
        return None, ("Путь вне разрешённых мест хранения — не удаляю. "
                      "Убирать можно только внутри: диктовки, логи, бэкапы, временные папки.")
    if _is_link(target):
        return None, "Это ссылка (junction/символическая) — не разворачиваю и не удаляю."
    if not target.exists():
        return None, "Уже нет — удалять нечего."
    # Между разрешённым корнем и целью не должно быть ссылок: иначе rmtree ушёл бы наружу.
    for root in inside:
        current = target.parent
        while _under(current, root):
            if _is_link(current):
                return None, "По пути стоит ссылка — внутрь не хожу, не удаляю."
            if current == root:
                break
            current = current.parent
    return target, ""


def _tree_size(target: Path, limit: int = MAX_SCAN_ENTRIES) -> tuple[int, int, bool]:
    """``(байты, файлы, обрезано)`` — без захода в ссылки/junction."""
    if not target.exists():
        return 0, 0, False
    if target.is_file():
        try:
            return int(target.stat().st_size), 1, False
        except OSError:
            return 0, 0, False
    total = 0
    files = 0
    seen = 0
    stack = [str(target)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > limit:
                return total, files, True
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += int(entry.stat(follow_symlinks=False).st_size)
                    files += 1
            except OSError:
                continue
    return total, files, False


# ------------------------------------------------------------------ кандидаты


def _dirs_newest_first(root: Path) -> list[tuple[float, Path]]:
    rows = []
    try:
        for item in root.iterdir():
            if _is_link(item):
                continue
            try:
                if not item.is_dir():
                    continue
                rows.append((item.stat().st_mtime, item))
            except OSError:
                continue
    except OSError:
        return []
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def _files_older(root: Path, hours: float, now: float) -> list[Path]:
    out = []
    try:
        for item in root.iterdir():
            if _is_link(item):
                continue
            try:
                if not item.is_file():
                    continue
                if hours <= 0 or (now - item.stat().st_mtime) > hours * 3600.0:
                    out.append(item)
            except OSError:
                continue
    except OSError:
        return []
    out.sort(key=lambda p: p.name)
    return out


def _entries_older(root: Path, hours: float, now: float) -> list[Path]:
    out = []
    try:
        for item in root.iterdir():
            if _is_link(item):
                continue
            try:
                if hours > 0 and (now - item.stat().st_mtime) <= hours * 3600.0:
                    continue
            except OSError:
                continue
            out.append(item)
    except OSError:
        return []
    out.sort(key=lambda p: p.name)
    return out


_RELEASE_RE = re.compile(r"(\d+\.\d+\.\d+)")


def _backups_candidates(cfg, policy: dict, now: float) -> list[Path]:
    """Последние ``backups_keep`` + по одному на релиз, остальные — кандидаты."""
    root = backups_root(cfg)
    rows = []
    try:
        for item in root.iterdir():
            if _is_link(item):
                continue
            try:
                rows.append((item.stat().st_mtime, item))
            except OSError:
                continue
    except OSError:
        return []
    rows.sort(key=lambda r: r[0], reverse=True)
    keep = max(1, int(policy.get("backups_keep") or 1))
    per_release = max(0, int(policy.get("backups_per_release") or 0))
    kept_versions: dict = {}
    out = []
    for index, (_mtime, item) in enumerate(rows):
        if index < keep:
            continue
        match = _RELEASE_RE.search(item.name)
        if match and per_release:
            version = match.group(1)
            if kept_versions.get(version, 0) < per_release:
                kept_versions[version] = kept_versions.get(version, 0) + 1
                continue
        out.append(item)
    out.sort(key=lambda p: p.name)
    return out


def _voice_candidates(cfg, policy: dict, now: float) -> list[Path]:
    rows = _dirs_newest_first(voice_root(cfg))
    keep = max(0, int(policy.get("voice_max") or 0))
    hours = float(policy.get("voice_hours") or 0)
    out = []
    for index, (mtime, item) in enumerate(rows):
        too_many = keep > 0 and index >= keep
        too_old = hours > 0 and (now - mtime) > hours * 3600.0
        if too_many or too_old:
            out.append(item)
    out.sort(key=lambda p: p.name)
    return out


def _specs(cfg, policy: dict, now: float) -> list[dict]:
    """Описание категорий: корень, что убираем, что показываем только цифрами."""
    specs: list[dict] = []

    def add(**kwargs):
        specs.append(kwargs)

    add(id="voice", title="Голосовые диктовки", root=voice_root(cfg), kind="ours",
        cleanable=True,
        policy="старше %d ч и сверх %d записей" % (policy["voice_hours"], policy["voice_max"]),
        why="Части длинных записей на диске. Расшифровка уже сохранена в запросе — аудио нужно "
            "только для повторной расшифровки.",
        candidates=_voice_candidates(cfg, policy, now))

    add(id="logs", title="Логи приложения", root=logs_root(cfg), kind="ours", cleanable=True,
        policy="старше %d дн." % policy["logs_days"],
        why="Журнал app.log и его ротации. Нужны для разбора недавних сбоев.",
        candidates=_files_older(logs_root(cfg), policy["logs_days"] * 24.0, now))

    add(id="backups", title="Бэкапы Team Room", root=backups_root(cfg), kind="ours", cleanable=True,
        policy="последние %d + по одному на релиз" % policy["backups_keep"],
        why="Копии перед обновлением. Свежие нужны для отката, старые — нет.",
        candidates=_backups_candidates(cfg, policy, now))

    add(id="tmp", title="Временные копии приложения", root=tmp_root(cfg), kind="ours",
        cleanable=True, policy="старше %d ч" % policy["tmp_hours"],
        why="Промежуточные файлы экспорта и сборки отчётов.",
        candidates=_entries_older(tmp_root(cfg), float(policy["tmp_hours"]), now))

    builds = build_cache_root(cfg)
    add(id="builds", title="Кэш сборок релиза", root=builds or tmp_root(cfg) / "builds",
        kind="ours", cleanable=builds is not None,
        policy="старше %d дн." % policy["build_cache_days"],
        why="Наши сборки релиза. Нужны только на время сборки и проверки.",
        candidates=(_entries_older(builds, policy["build_cache_days"] * 24.0, now)
                    if builds else []))

    accept = accept_root(cfg)
    add(id="accept", title="Временные папки приёмки", root=accept or tmp_root(cfg) / "accept",
        kind="ours", cleanable=accept is not None,
        policy="сразу после приёмки" if policy["accept_immediately"] else "не удалять",
        why="Копии проекта для проверки перед переносом на боевой.",
        candidates=(_entries_older(accept, 0.0 if policy["accept_immediately"] else 1e9, now)
                    if accept else []))

    vault = _vault_root(cfg)
    add(id="vault", title="Vault (Obsidian)", root=vault or Path(cfg.data_dir) / "vault-off",
        kind="foreign", cleanable=False, policy="не удаляется",
        why="Заметки и материалы комнат. Ваши документы — их убирает только человек.",
        candidates=[])

    hermes = Path(getattr(cfg, "hermes_home", "") or cfg.data_dir)
    add(id="hermes_cache", title="Кэш Hermes (scratch)", root=hermes / "cache" / "scratch",
        kind="foreign", cleanable=False, policy="только показ",
        why="Рабочие файлы Hermes и сборок. Чистит сам Hermes, мы только показываем размер.",
        candidates=[])
    add(id="hermes_logs", title="Логи Hermes", root=hermes / "logs", kind="foreign",
        cleanable=False, policy="только показ",
        why="Журналы Hermes Desktop. Нужны для разбора сбоев моделей.",
        candidates=[])
    return specs


def _vault_proposal(vault: Path | None, limit: int) -> dict:
    """Сколько места в Vault занимают копии, которые можно заменить ссылками."""
    if vault is None or not vault.is_dir():
        return {"files": 0, "bytes": 0, "sample": [], "available": False}
    total = 0
    files = 0
    sample = []
    stack = [str(vault)]
    seen = 0
    while stack and seen < 20000:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            try:
                size = int(entry.stat(follow_symlinks=False).st_size)
            except OSError:
                continue
            if size <= limit and is_text_name(entry.name):
                continue
            total += size
            files += 1
            if len(sample) < 5:
                sample.append({"name": entry.name, "size": size, "size_text": human_size(size)})
    return {"files": files, "bytes": total, "sample": sample, "available": True}


def overview(cfg, now: float | None = None) -> dict:
    """Экран «Хранилище»: размеры по категориям и что будет убрано."""
    now = float(now if now is not None else time.time())
    policy = load_policy(cfg)
    rows = []
    total = 0
    for spec in _specs(cfg, policy, now):
        root = _resolve(spec["root"])
        exists = False
        try:
            exists = root.is_dir()
        except OSError:
            exists = False
        size, files, truncated = _tree_size(root) if exists else (0, 0, False)
        total += size
        candidates = spec.get("candidates") or []
        will_bytes = 0
        will_files = 0
        preview = []
        for item in candidates[:50]:
            item_size, item_files, _ = _tree_size(item)
            will_bytes += item_size
            will_files += item_files or 1
            if len(preview) < 5:
                preview.append({"name": item.name, "size": item_size, "size_text": human_size(item_size),
                                "path": str(item)})
        rows.append({
            "id": spec["id"], "title": spec["title"], "path": str(root),
            "exists": exists, "bytes": size, "size_text": human_size(size), "files": files,
            "foreign": spec["kind"] == "foreign",
            "cleanable": bool(spec["cleanable"]),
            "policy": spec["policy"], "why": spec["why"],
            "will_remove": {"files": will_files if candidates else 0,
                            "bytes": will_bytes if candidates else 0,
                            "size_text": human_size(will_bytes if candidates else 0),
                            "count": len(candidates), "preview": preview},
            "truncated": truncated,
        })
    vault_row = next((r for r in rows if r["id"] == "vault"), None)
    proposal = _vault_proposal(_vault_root(cfg), int(policy["copy_max_bytes"]))
    if vault_row is not None:
        vault_row["proposal"] = proposal
    free = None
    try:
        import shutil
        free = int(shutil.disk_usage(str(cfg.data_dir)).free)
    except OSError:
        free = None
    warning = None
    if vault_row is not None and not vault_row.get("exists"):
        warning = ("Vault не найден — зеркало комнат в Obsidian выключено. "
                   "Укажите каталог хранилища в настройках приложения.")
    return {"ok": True, "generated_at": int(now), "total_bytes": total,
            "total_text": human_size(total), "free_bytes": free,
            "free_text": human_size(free) if free is not None else None,
            "categories": rows, "policy": policy, "warning": warning,
            "root": str(cfg.data_dir)}


# ------------------------------------------------------------------ очистка


def _remove(target: Path) -> None:
    """Удалить файл, дерево или пустую папку (без разворота ссылок)."""
    if target.is_file():
        target.unlink()
        return
    # rmtree не идёт по junction (Windows), но свою проверку мы уже сделали.
    import shutil
    shutil.rmtree(str(target))


def delete_paths(cfg, paths, dry_run: bool = True) -> dict:
    """Удалить перечисленные пути, но только внутри разрешённых корней.

    По умолчанию — сухой прогон: возвращает тот же отчёт, ничего не удаляя.
    """
    result = {"dry_run": bool(dry_run), "removed": [], "would_remove": [], "refused": [],
              "errors": [], "bytes": 0, "files": 0}
    seen: list[str] = []
    for raw in paths or []:
        target, reason = check_deletable(cfg, raw)
        if target is None:
            result["refused"].append({"path": str(raw), "reason": reason})
            continue
        key = os.path.normcase(str(target))
        if key in seen:
            continue
        seen.append(key)
        size, files, _ = _tree_size(target)
        if dry_run:
            result["would_remove"].append({"path": str(target), "bytes": size,
                                           "size_text": human_size(size), "files": files})
            result["bytes"] += size
            result["files"] += files
            continue
        try:
            _remove(target)
        except OSError as exc:
            result["errors"].append({"path": str(target), "reason": str(exc)[:200]})
            continue
        result["removed"].append({"path": str(target), "bytes": size,
                                  "size_text": human_size(size), "files": files})
        result["bytes"] += size
        result["files"] += files
    return result


def clean(cfg, categories=None, dry_run: bool = True, confirm: bool = False,
          now: float | None = None) -> dict:
    """Очистка по категориям. ``dry_run`` — по умолчанию; удаление требует ``confirm``."""
    now = float(now if now is not None else time.time())
    policy = load_policy(cfg)
    wanted = [str(c).strip() for c in (categories or []) if str(c).strip()]
    specs = _specs(cfg, policy, now)
    known = {spec["id"] for spec in specs}
    unknown = [c for c in wanted if c not in known]
    if unknown:
        raise StorageError("Неизвестная категория хранилища: %s" % ", ".join(unknown),
                           detail={"categories": unknown, "known": sorted(known)})
    chosen = [spec for spec in specs if spec["cleanable"] and (not wanted or spec["id"] in wanted)]
    foreign = [spec["id"] for spec in specs if spec["kind"] == "foreign" and wanted
               and spec["id"] in wanted]
    if foreign:
        raise StorageError("Эти данные не удаляются приложением: %s" % ", ".join(foreign),
                           detail={"categories": foreign})
    paths = []
    used = []
    by_category = {}
    for spec in chosen:
        candidates = spec.get("candidates") or []
        if not candidates:
            continue
        used.append(spec["id"])
        by_category[spec["id"]] = len(candidates)
        paths.extend(candidates)
    if not dry_run and not confirm:
        raise StorageError("Удаление без подтверждения не выполняется: покажите список и "
                           "нажмите «Удалить» ещё раз", code="confirm_required",
                           detail={"categories": used})
    result = delete_paths(cfg, paths, dry_run=dry_run)
    result.update({"categories": used, "by_category": by_category, "generated_at": int(now),
                   "policy": policy})
    result["message"] = (
        "Сухой прогон: ничего не удалено. К удалению — %d объект(ов), %s."
        % (len(result["would_remove"]), human_size(result["bytes"]))
        if dry_run else
        "Удалено %d объект(ов), освобождено %s. Не удалось: %d."
        % (len(result["removed"]), human_size(result["bytes"]), len(result["errors"])))
    return result


def preview_clean(cfg, categories=None) -> dict:
    """Только список к удалению — без записи и без удаления."""
    return clean(cfg, categories=categories, dry_run=True)


# ------------------------------------------------------------------ материалы


def material_plan(cfg, path, limit: int | None = None) -> dict:
    """Копировать файл в Vault или описать ссылкой.

    Правило: маленький текстовый (≤ ``copy_max_bytes``) — копия, как раньше.
    Крупный или нетекстовый — заметка-ссылка: файл остаётся на месте.
    """
    policy = load_policy(cfg)
    max_bytes = int(limit if limit is not None else policy["copy_max_bytes"])
    raw = str(path or "").strip()
    target = Path(raw) if raw else None
    exists = False
    size = 0
    if target is not None:
        try:
            exists = target.is_file()
            size = int(target.stat().st_size) if exists else 0
        except OSError:
            exists = False
    text = is_text_name(raw)
    copy = bool(exists and text and size <= max_bytes)
    if not exists:
        reason = "Файл не найден — в заметке будет только путь."
    elif copy:
        reason = "Небольшой текстовый файл — копия в Vault, как раньше."
    elif not text:
        reason = "Нетекстовый файл — в Vault только ссылка, оригинал остаётся на месте."
    else:
        reason = ("Файл больше %s — в Vault только ссылка, оригинал остаётся на месте."
                  % human_size(max_bytes))
    return {"path": raw, "name": target.name if target is not None else "", "exists": exists,
            "size": size, "size_text": human_size(size), "text": text, "copy": copy,
            "link": not copy, "limit": max_bytes, "limit_text": human_size(max_bytes),
            "kind": kind_of(raw), "reason": reason}


def file_hash(path, limit: int = _HASH_CAP) -> str:
    """SHA-256 файла (до ``limit`` байт). Пустая строка — файл не прочитался."""
    digest = hashlib.sha256()
    read = 0
    try:
        with open(path, "rb") as handle:
            while read < limit:
                chunk = handle.read(min(MB, limit - read))
                if not chunk:
                    break
                digest.update(chunk)
                read += len(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def material_reference(cfg, path) -> dict:
    """Заметка-ссылка на файл: путь, размер, хэш, «что это»."""
    plan = material_plan(cfg, path)
    digest = file_hash(plan["path"]) if plan["exists"] else ""
    plan["sha256"] = digest
    plan["sha256_short"] = digest[:16]
    plan["note"] = ("В Vault копия не создаётся: файл остаётся на месте (%s)." % plan["reason"]
                    if plan["link"] else "")
    return plan


def material_note_lines(cfg, path, label=None) -> list[str]:
    """Строки markdown для зеркала: ссылка на файл с размером, хэшем и пояснением."""
    ref = material_reference(cfg, path)
    name = str(label or ref["name"] or Path(ref["path"]).name or "файл")
    head = "- **%s** — %s · %s" % (name, ref["size_text"], ref["kind"])
    if ref["sha256_short"]:
        head += " · `sha256:%s…`" % ref["sha256_short"]
    lines = [head, "  - где лежит: `%s`" % ref["path"]]
    if ref["link"]:
        lines.append("  - копии в Vault нет — %s" % ref["reason"].rstrip("."))
    return lines


# --------------------------------------------------- папка проекта и junction


def default_project_dir(cfg, room) -> Path | None:
    """Один дом: папка проекта комнаты по умолчанию — её зеркало в Vault."""
    root = config_mod.vault_root_dir(cfg)
    room = room if isinstance(room, dict) else {}
    room_id = str(room.get("id") or "").strip()
    if not root or not room_id:
        return None
    try:
        return config_mod.room_vault_dir(cfg, room_id).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def home_proposal(cfg, room) -> dict:
    """Где папка проекта сейчас и совпадает ли она с домом в Vault."""
    room = room if isinstance(room, dict) else {}
    default = default_project_dir(cfg, room)
    current = str(room.get("project_dir") or "").strip()
    same = False
    if default is not None and current:
        try:
            same = os.path.normcase(str(Path(current).resolve())) == os.path.normcase(str(default))
        except (OSError, ValueError, RuntimeError):
            same = False
    return {"default": str(default) if default is not None else None,
            "current": current or None, "same": same,
            "suggest": bool(default is not None and current and not same),
            "hint": ("Папка проекта лежит вне Vault — можно поставить ссылку «Материалы» "
                     "в Obsidian, ничего не копируя." if default is not None and current and not same
                     else "Папка проекта уже в Vault — Obsidian видит материалы комнаты.")}


def materials_link_path(cfg, room) -> Path | None:
    """``<зеркало комнаты>/Материалы`` — место ссылки на внешнюю папку проекта."""
    mirror = default_project_dir(cfg, room)
    return (mirror / MATERIALS_DIRNAME) if mirror is not None else None


def link_state(cfg, room) -> dict:
    link = materials_link_path(cfg, room)
    current = str(((room or {}) if isinstance(room, dict) else {}).get("project_dir") or "").strip()
    out = {"ok": True, "path": str(link) if link is not None else None,
           "target": current or None, "exists": False, "is_link": False}
    if link is None:
        out["ok"] = False
        out["reason"] = "Зеркало в Vault выключено — ссылку поставить некуда."
        return out
    try:
        out["exists"] = link.exists() or _is_link(link)
        out["is_link"] = _is_link(link) if out["exists"] else False
    except OSError:
        pass
    return out


def _mklink_junction(link: Path, target: Path) -> tuple[bool, str]:
    """Создать junction Windows. Возвращает (получилось, причина)."""
    try:
        proc = _proc_run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                         capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:200]
    if proc.returncode != 0:
        raw = proc.stderr or proc.stdout or b""
        try:
            text = raw.decode("mbcs", "replace")
        except (LookupError, AttributeError, UnicodeDecodeError):
            text = raw.decode("utf-8", "replace")
        return False, (text.strip() or "mklink вернул код %d" % proc.returncode)[:200]
    return True, ""


def create_materials_link(cfg, room, target=None, note: bool = True) -> dict:
    """Поставить в Vault ссылку ``Материалы`` на внешнюю папку проекта.

    Только по явному действию человека. Данные не копируются и не переносятся.
    """
    link = materials_link_path(cfg, room)
    if link is None:
        raise StorageError("Зеркало в Vault выключено — ссылку поставить некуда. "
                           "Укажите каталог хранилища Obsidian и перезапустите приложение.",
                           code="vault_off", status=409)
    raw_target = str(target or ((room or {}) if isinstance(room, dict) else {}).get("project_dir") or "")
    if not raw_target.strip():
        raise StorageError("Сначала укажите папку проекта комнаты — ссылке нужна цель.",
                           code="no_target")
    try:
        target_path = Path(raw_target).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise StorageError("Папка проекта не найдена: %s" % raw_target, code="no_target")
    if not target_path.is_dir():
        raise StorageError("Папка проекта — это не папка: %s" % target_path, code="no_target")
    state = link_state(cfg, room)
    if state.get("exists") and state.get("is_link"):
        return {"ok": True, "created": False, "path": str(link), "target": str(target_path),
                "message": "Ссылка «%s» уже стоит." % MATERIALS_DIRNAME}
    if state.get("exists"):
        raise StorageError("В Vault уже есть папка «%s» с файлами — заменять её ссылкой не буду. "
                           "Уберите папку вручную, если она не нужна." % MATERIALS_DIRNAME,
                           code="exists", status=409)
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError("Не удалось подготовить папку зеркала: %s" % exc,
                           code="storage_error", status=502) from exc
    ok, reason = _mklink_junction(link, target_path)
    if not ok:
        raise StorageError("Windows не дал создать ссылку: %s" % reason,
                           code="link_failed", status=502, detail={"target": str(target_path)})
    if note:
        try:
            text = ("# Проект\n\nПапка проекта этой комнаты лежит вне Vault:\n\n"
                    "`%s`\n\nВ Obsidian она видна как ссылка «%s» — файлы не копируются.\n"
                    % (target_path, MATERIALS_DIRNAME))
            statefiles.write_text_if_changed(link.parent / "Проект.md", text)
        except OSError:
            pass
    return {"ok": True, "created": True, "path": str(link), "target": str(target_path),
            "message": "Ссылка «%s» создана — Obsidian видит папку проекта." % MATERIALS_DIRNAME}


def remove_materials_link(cfg, room) -> dict:
    """Снять ссылку ``Материалы``. Данные в папке проекта остаются на месте."""
    link = materials_link_path(cfg, room)
    if link is None:
        raise StorageError("Зеркало в Vault выключено.", code="vault_off", status=409)
    if not link.exists() and not _is_link(link):
        return {"ok": True, "removed": False, "path": str(link),
                "message": "Ссылки нет — снимать нечего."}
    if not _is_link(link):
        raise StorageError("«%s» в Vault — обычная папка, а не ссылка. Её не удаляю: "
                           "внутри могут быть ваши файлы." % MATERIALS_DIRNAME,
                           code="not_link", status=409)
    try:
        os.rmdir(str(link))          # снимает только саму ссылку, содержимое не трогает
    except OSError as exc:
        raise StorageError("Не удалось снять ссылку: %s" % exc, code="storage_error",
                           status=502) from exc
    return {"ok": True, "removed": True, "path": str(link),
            "message": "Ссылка снята. Файлы в папке проекта на месте."}


__all__ = [
    "StorageError", "DEFAULT_POLICY", "POLICY_LIMITS", "MATERIALS_DIRNAME",
    "human_size", "kind_of", "is_text_name",
    "policy_path", "load_policy", "save_policy",
    "delete_roots", "forbidden_roots", "check_deletable", "delete_paths",
    "overview", "clean", "preview_clean",
    "material_plan", "material_reference", "material_note_lines", "file_hash",
    "default_project_dir", "home_proposal", "materials_link_path", "link_state",
    "create_materials_link", "remove_materials_link",
]
