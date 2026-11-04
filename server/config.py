"""Конфигурация «Комнаты команды»: пути, песочница, белый список файлов.

Ничего машино-специфичного: всё берётся из окружения или стандартных каталогов
пользователя. Переменные окружения (все необязательны):

  TEAMROOM_HOME         корень данных приложения (по умолчанию %LOCALAPPDATA%/TeamRoom
                        на Windows, ~/TeamRoom на остальных)
  TEAMROOM_DATA         каталог данных (по умолчанию <app>/data)
  TEAMROOM_VAULT_DIR    куда писать зеркало; "" / off / none — зеркало выключено.
                        По умолчанию ищем хранилище Obsidian рядом с приложением
                        и в домашнем каталоге (см. detect_vault_dir)
  TEAMROOM_PORT         порт (по умолчанию 8777)
  TEAMROOM_HERMES_CLI   путь к исполняемому hermes (по умолчанию ищем в PATH и в
                        каталоге Hermes; если не нашли — режим просмотра без записи)
  TEAMROOM_HERMES_HOME  песочница канбана (kanban_home); реальный hermes_home
                        остаётся только для чтения
  TEAMROOM_EXTRA_ROOTS  дополнительные разрешённые корни для файлов, через ;
  HERMES_HOME           корень Hermes Desktop (по умолчанию определяем сами)
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Версия приложения — единственный источник правды: файл `VERSION` в корне
#: проекта (рядом с `server/`). Сборка релиза кладёт туда же `VERSION`, поэтому
#: сервер, плагин и архив всегда показывают одну и ту же цифру.
APP_DIR = Path(__file__).resolve().parent.parent  # .../team-room


def read_version() -> str:
    """`1.0.0` из файла VERSION; если файла нет — честная заглушка, а не выдумка."""
    try:
        text = (APP_DIR / "VERSION").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0+unknown"
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first or "0.0.0+unknown"


VERSION = read_version()
DEFAULT_PORT = 8777
HOST = "127.0.0.1"

#: значения TEAMROOM_VAULT_DIR, которыми зеркало выключают вручную
VAULT_OFF_VALUES = frozenset({"", "0", "off", "none", "no", "false", "disable", "disabled"})

#: подсказка, когда зеркало выключено (её показывают /api/health и интерфейс)
VAULT_HINT_RU = ("Зеркало в Obsidian выключено: укажите TEAMROOM_VAULT_DIR "
                 "(каталог хранилища Obsidian) и перезапустите приложение")

# имена файлов, которые никогда не отдаём наружу (регистр не важен)
DENY_NAME_PARTS = (".env", "auth.json", "token", "secret", ".session", "google_", "credentials", "password", "id_rsa")
ALLOWED_EXT = {
    ".md", ".txt", ".json", ".csv", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".pdf", ".docx", ".xlsx", ".pptx", ".ics", ".log",
}


@dataclass
class Cfg:
    hermes_home: Path          # реальный HERMES_HOME (только чтение: профили, gateway_state, config)
    kanban_home: Path          # где лежит kanban/boards/<slug>/kanban.db (в песочнице — временная папка)
    data_dir: Path
    vault_dir: Path           # корень зеркала по комнатам (при выключенном зеркале — внутри data)
    port: int = DEFAULT_PORT
    host: str = HOST
    hermes_cli: str = "hermes"
    sandbox: bool = False
    app_dir: Path = APP_DIR
    allowed_roots: list = field(default_factory=list)
    vault_enabled: bool = True

    # --- производные пути ---
    @property
    def web_dir(self) -> Path:
        return self.app_dir / "web"

    @property
    def reportkit_dir(self) -> Path:
        return self.app_dir / "reportkit"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def kanban_db(self, board: str) -> Path:
        """Путь к БД доски. Для 'default' — корневой kanban.db (см. ENGINE-FACTS)."""
        if board in ("", "default"):
            return self.kanban_home / "kanban.db"
        return self.kanban_home / "kanban" / "boards" / board / "kanban.db"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.state_dir, self.reports_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ каталоги

def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()).expanduser()


def _local_appdata() -> Path | None:
    """`%LOCALAPPDATA%` на Windows, `$XDG_DATA_HOME` на остальных; иначе None."""
    env = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return _home() / "AppData" / "Local"
    return None


def teamroom_home() -> Path:
    """Корень данных приложения: TEAMROOM_HOME, иначе стандартный каталог пользователя."""
    env = os.environ.get("TEAMROOM_HOME")
    if env:
        return Path(env).expanduser()
    local = _local_appdata()
    return (local or _home()) / "TeamRoom"


def _hermes_home_candidates() -> list[Path]:
    """Где обычно живёт Hermes Desktop: %LOCALAPPDATA%/hermes, Application Support, ~/.hermes."""
    out: list[Path] = []
    local = _local_appdata()
    if local:
        out.append(local / "hermes")
    if sys.platform == "darwin":
        out.append(_home() / "Library" / "Application Support" / "hermes")
    out.append(_home() / ".hermes")
    return out


def _looks_like_hermes_home(path: Path) -> bool:
    try:
        return any((path / name).exists() for name in ("config.yaml", "profiles", "kanban", "cache"))
    except OSError:
        return False


def _hermes_home_from_cli(cli: str | None) -> Path | None:
    """Каталог Hermes по пути к исполняемому файлу: `…/hermes/bin/hermes.exe` → `…`."""
    if not cli:
        return None
    try:
        current = Path(cli).expanduser()
        if current.is_file() or current.parent.is_dir():
            current = current.parent
        for _ in range(6):
            if current.name and _looks_like_hermes_home(current):
                return current
            if current.parent == current:
                break
            current = current.parent
    except (OSError, ValueError):
        return None
    return None


def real_hermes_home() -> Path:
    """Корень Hermes Desktop: HERMES_HOME, иначе автоопределение (PATH, LOCALAPPDATA, ~/.hermes)."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    from_cli = _hermes_home_from_cli(shutil.which("hermes") or shutil.which("hermes.exe"))
    if from_cli is not None:
        return from_cli
    for candidate in _hermes_home_candidates():
        if candidate.is_dir():
            return candidate
    return _hermes_home_candidates()[0]


def _real_hermes_home() -> Path:
    """Псевдоним для тестов и старых вызовов (`tests/helpers.py`, `tools/seed_sandbox.py`)."""
    return real_hermes_home()


# ---------------------------------------------------------- hermes CLI и Vault

def _hermes_cli_candidates() -> list[str]:
    """Где искать исполняемый файл hermes: PATH, затем каталоги установки Hermes."""
    out: list[str] = []
    found = shutil.which("hermes") or shutil.which("hermes.exe")
    if found:
        out.append(found)
    names = ("hermes.exe", "hermes.cmd", "hermes") if os.name == "nt" else ("hermes",)
    for base in [real_hermes_home(), *_hermes_home_candidates()]:
        for sub in (("bin",), ("hermes-agent", ".hermes", "bin")):
            for name in names:
                candidate = base.joinpath(*sub, name)
                if candidate.is_file():
                    text = str(candidate)
                    if text not in out:
                        out.append(text)
    return out


def detect_hermes_cli() -> str:
    """Путь к hermes CLI или '' — тогда приложение работает в режиме просмотра (без записи)."""
    env = os.environ.get("TEAMROOM_HERMES_CLI")
    if env is not None:                       # явное указание, даже если файла нет
        return str(env).strip()
    candidates = _hermes_cli_candidates()
    return candidates[0] if candidates else ""


def _vault_roots_candidates() -> list[Path]:
    """Кандидаты корня Obsidian-Vault (сам Vault, не «Rooms»)."""
    out: list[Path] = []
    env = os.environ.get("OBSIDIAN_VAULT_PATH")
    if env:
        out.append(Path(env).expanduser())
    home = _home()
    out += [
        APP_DIR.parent.parent / "Vault",     # Vault рядом с папкой, где лежит приложение
        teamroom_home() / "Vault",
        home / "Vault",
        home / "Obsidian",
        home / "Documents" / "Vault",
    ]
    unique: list[Path] = []
    for item in out:
        if item not in unique:
            unique.append(item)
    return unique


def _is_vault_root(path: Path) -> bool:
    """Похоже ли на хранилище Obsidian: есть `.obsidian` или подкаталог `Projects`."""
    try:
        if not Path(path).is_dir():
            return False
        return (Path(path) / ".obsidian").is_dir() or (Path(path) / "Projects").is_dir()
    except OSError:
        return False


def detect_vault_dir() -> Path | None:
    """Куда писать зеркало: TEAMROOM_VAULT_DIR, иначе найденный Vault; None — выключено."""
    env = os.environ.get("TEAMROOM_VAULT_DIR")
    if env is not None:
        text = str(env).strip().strip('"').strip("'")
        if text.lower() in VAULT_OFF_VALUES:
            return None
        return Path(text).expanduser()
    root = detected_vault_root()
    return root / "Projects" / "Rooms" if root else None


def detected_vault_root() -> Path | None:
    """Корень найденного хранилища Obsidian на этой машине (без учёта TEAMROOM_VAULT_DIR)."""
    for root in _vault_roots_candidates():
        if _is_vault_root(root):
            return root
    return None


def load() -> Cfg:
    hermes_home = real_hermes_home()
    sandbox_home = os.environ.get("TEAMROOM_HERMES_HOME")
    sandbox = bool(sandbox_home)
    kanban_home = Path(sandbox_home) if sandbox else Path(os.environ.get("HERMES_KANBAN_HOME") or hermes_home)
    if sandbox:
        # защита: песочница не может совпадать с реальным домом Hermes
        try:
            if Path(sandbox_home).resolve() == hermes_home.resolve():
                raise RuntimeError("TEAMROOM_HERMES_HOME указывает на реальный HERMES_HOME — отказ")
        except OSError:
            pass
    data_dir = Path(os.environ.get("TEAMROOM_DATA") or (APP_DIR / "data"))
    vault_dir = detect_vault_dir()
    if sandbox and os.environ.get("TEAMROOM_VAULT_DIR") is None:
        # песочница не пишет в найденный на машине Vault: только явный TEAMROOM_VAULT_DIR
        vault_dir = None
    vault_enabled = vault_dir is not None
    if vault_dir is None:
        # зеркало выключено: путь-заглушка внутри data, туда ничего не пишется
        vault_dir = data_dir / "Vault" / "Projects" / "Rooms"
    port = int(os.environ.get("TEAMROOM_PORT") or DEFAULT_PORT)
    cli = detect_hermes_cli()

    roots = [data_dir]
    if vault_dir is not None:
        roots.append(vault_dir)
        # корень Vault целиком: оттуда приходят BRIEF.md/ROADMAP.md проекта
        vault_root = Path(vault_dir).parent.parent
        if _is_vault_root(vault_root):
            roots.append(vault_root)
    if not sandbox:
        home = teamroom_home()
        roots += [home / "Vault", home / "Team", home / "Agents"]
        boards = kanban_home / "kanban" / "boards"
        if boards.is_dir():
            roots += [b / "workspaces" for b in boards.iterdir() if (b / "workspaces").is_dir()]
    for extra in (os.environ.get("TEAMROOM_EXTRA_ROOTS") or "").split(";"):
        if extra.strip():
            roots.append(Path(extra.strip()))
    unique_roots: list[Path] = []
    for root in roots:
        if root not in unique_roots:
            unique_roots.append(root)
    cfg = Cfg(hermes_home=hermes_home, kanban_home=kanban_home, data_dir=data_dir, vault_dir=vault_dir,
              port=port, hermes_cli=cli, sandbox=sandbox, allowed_roots=unique_roots,
              vault_enabled=vault_enabled)
    return cfg


# --- зеркало комнаты в Vault: единственный источник путей ----------------------

STATE_FILE = "STATE.md"
JOURNAL_FILE = "Журнал.md"


def safe_dir_name(name) -> str:
    """Безопасное имя каталога: без разделителей пути и управляющих символов, ≤80 символов."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or "room")[:80]


def vault_root_dir(cfg) -> Path | None:
    """Корень зеркала или None, если зеркало выключено (Vault не найден/не указан)."""
    if not getattr(cfg, "vault_enabled", True):
        return None
    value = getattr(cfg, "vault_dir", None)
    return Path(value) if value else None


def room_vault_dir(cfg, room_id) -> Path:
    """Каталог зеркала комнаты: ``<vault_dir>/<room.id>/``.

    По ``room.id`` (стабилен, не зависит от переименования комнаты). Единственное место,
    где строится этот путь: его читают contract (путь в теле карточки) и ingest (запись).
    Если зеркало выключено — вызывающий обязан проверить ``vault_root_dir(cfg)``.
    """
    return Path(cfg.vault_dir) / safe_dir_name(room_id)


def state_md_path(cfg, room_id) -> Path:
    """``<vault_dir>/<room.id>/STATE.md`` — главный файл контекста комнаты."""
    return room_vault_dir(cfg, room_id) / STATE_FILE


def journal_md_path(cfg, room_id) -> Path:
    """``<vault_dir>/<room.id>/Журнал.md`` — хронология комнаты для людей."""
    return room_vault_dir(cfg, room_id) / JOURNAL_FILE


def is_denied_name(name: str) -> bool:
    low = name.lower()
    return any(p in low for p in DENY_NAME_PARTS)


def path_allowed(cfg: Cfg, path: str | os.PathLike) -> Path | None:
    """Вернёт resolved-путь, если файл лежит в белом списке корней и не в чёрном списке имён; иначе None."""
    try:
        p = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not p.is_file():
        return None
    if is_denied_name(p.name) or any(is_denied_name(part) for part in p.parts[-4:]):
        return None
    if p.suffix.lower() not in ALLOWED_EXT:
        return None
    for root in cfg.allowed_roots:
        try:
            p.relative_to(Path(root).resolve())
            return p
        except (ValueError, OSError):
            continue
    return None
