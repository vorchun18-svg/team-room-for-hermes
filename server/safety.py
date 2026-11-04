"""Системная защита реального Hermes и Vault от записи.

Инцидент 28.10.2026: `tools/check_conformance.py` записал заглушки в
`cfg.hermes_home` (РЕАЛЬНЫЙ `%LOCALAPPDATA%\\hermes`) и затёр `config.yaml` и
профили; тест послал `POST /api/dispatcher/kick` без подмены CLI — диспетчер
запустил настоящих агентов (5.7 млн входных токенов, 5 файлов в реальном Vault).

Этот модуль — единая точка правды: пути реального Hermes/Vault и два
контекстных запрета, которые включаются в `tests.helpers.Sandbox` и в
инструментах `tools/`. Правила для людей и агентов — `docs/SAFETY-RULES.md`.

    from server import safety
    safety.assert_not_real(path, "запись отчёта")     # точечная проверка
    with safety.forbid_real_writes():                # перехват всех записей
        ...
    with safety.forbid_real_subprocess():            # запрет агентского CLI
        ...
"""
from __future__ import annotations

import builtins
import contextlib
import io
import ntpath
import os
import posixpath
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

__all__ = [
    "SafetyError",
    "real_hermes_home",
    "real_vault",
    "is_under_real",
    "assert_not_real",
    "guarded_write_text",
    "guarded_write_bytes",
    "guarded_mkdir",
    "guarded_copy2",
    "guarded_rmtree",
    "forbid_real_writes",
    "forbid_real_subprocess",
]

#: Реальный Vault (Obsidian): путь берём из TEAMROOM_REAL_VAULT, иначе из
#: TEAMROOM_VAULT_DIR приложения. Никаких путей автора: на чужой машине Vault
#: может не быть вовсе — тогда проверка просто ничего не запрещает.
VAULT_ENV = "TEAMROOM_REAL_VAULT"

#: Глаголы `hermes`, которые запускают агентов, меняют конфиг или авторизацию.
BLOCKED_HERMES_VERBS = frozenset({
    "dispatch", "decompose", "swarm", "kick", "gateway", "profile",
    "config", "auth", "update", "setup",
})
HERMES_EXECUTABLES = frozenset({
    "hermes", "hermes.exe", "hermes.cmd", "hermes.bat", "hermes.ps1", "hermes.py",
})


class SafetyError(RuntimeError):
    """Попытка записи в реальный Hermes/Vault или запуска агентского CLI."""


# ---------------------------------------------------------------- пути

def real_hermes_home() -> Path:
    """Реальный `%HERMES_HOME%` (по умолчанию `%LOCALAPPDATA%\\hermes`).

    Не `.resolve()`: каталог может ещё не существовать, а сравнивать пути
    нужно с точным корнем, который читает сам Hermes.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "hermes"


def real_vault() -> Path:
    """Реальный Vault машины: TEAMROOM_REAL_VAULT, иначе найденный рядом с приложением.

    Путь автора не зашит: на чужой машине Vault ищется рядом с приложением/в домашнем
    каталоге, а если его нет — возвращается безопасный ``~/Vault`` (запрет в пустоту).
    """
    env = os.environ.get(VAULT_ENV)
    if env and str(env).strip():
        return Path(env).expanduser()
    try:
        from . import config as _config          # server.safety
    except ImportError:                          # плоский импорт (server/app.py)
        import config as _config                 # type: ignore
    try:
        detected = _config.detected_vault_root()
    except Exception:                            # noqa: BLE001 — защита не должна падать
        detected = None
    return Path(detected) if detected else Path.home() / "Vault"


def _abs(path) -> Path:
    """Абсолютный нормализованный путь без требования существования файла."""
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    p = Path(os.fspath(path))
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        return p.resolve()
    except (OSError, RuntimeError):        # битый symlink/слишком длинный путь
        return Path(os.path.abspath(str(p)))


def _key(path) -> str:
    return os.path.normcase(str(_abs(path)))


def _under(path, parent) -> bool:
    """path == parent или лежит внутри parent (регистр на Windows не важен)."""
    child, root = _key(path), _key(parent)
    if child == root:
        return True
    return child.startswith(root.rstrip("\\/") + os.sep)


def _scratch_root() -> Path:
    return real_hermes_home() / "cache" / "scratch"


#: Считаем один раз при импорте: `tempfile.gettempdir()` сам создаёт пробный файл
#: через `open()`, а `open` на время запрета патчится — вызов внутри проверки дал бы
#: рекурсию (найдено тестом 28.10).
_BASE_TEMP = Path(tempfile.gettempdir())


def _temp_roots() -> list[Path]:
    roots = [_BASE_TEMP]
    extra = os.environ.get("TEAMROOM_TEST_TMP")
    if extra:
        roots.append(Path(extra))
    return roots


def is_under_real(path) -> bool:
    """True, если путь — реальный Hermes/Vault (cache/scratch и temp не в счёт)."""
    try:
        p = _abs(path)
    except (TypeError, ValueError):
        return False
    if _under(p, _scratch_root()) or any(_under(p, t) for t in _temp_roots()):
        return False
    return _under(p, real_hermes_home()) or _under(p, real_vault())


def assert_not_real(path, what: str = "запись") -> Path | None:
    """Бросить `SafetyError`, если `path` внутри реального Hermes или Vault.

    Возвращает нормализованный путь (для дальнейшей записи). Для файлового
    дескриптора/`None` проверять нечего — возвращает `None`.
    """
    if path is None or isinstance(path, int):
        return None
    try:
        p = _abs(path)
    except (TypeError, ValueError) as exc:
        raise SafetyError(f"не разобрал путь для проверки ({what}): {path!r}") from exc
    if _under(p, _scratch_root()) or any(_under(p, t) for t in _temp_roots()):
        return p
    home = real_hermes_home()
    if _under(p, home):
        raise SafetyError(
            f"ЗАПРЕЩЕНО: {what} в реальный Hermes — {p}\n"
            f"  реальный корень: {home}\n"
            "  Тесты и инструменты пишут только в песочницу tests.helpers.Sandbox, "
            f"в {_scratch_root()} или во временный каталог. См. docs/SAFETY-RULES.md.")
    vault = real_vault()
    if _under(p, vault):
        raise SafetyError(
            f"ЗАПРЕЩЕНО: {what} в реальный Vault — {p}\n"
            f"  реальный корень: {vault}\n"
            "  Зеркало комнат в боевом Vault пишет только сервер с TEAMROOM_VAULT_DIR; "
            "тесты — во временный каталог. См. docs/SAFETY-RULES.md.")
    return p


# ---------------------------------------------------------------- guarded-обёртки

def guarded_write_text(path, data: str, *, encoding: str = "utf-8", errors=None,
                       newline=None):
    """`Path.write_text` с проверкой, что путь не в реальном Hermes/Vault."""
    p = assert_not_real(path, "запись текста")
    return Path(p).write_text(data, encoding=encoding, errors=errors, newline=newline)


def guarded_write_bytes(path, data: bytes):
    """`Path.write_bytes` с проверкой, что путь не в реальном Hermes/Vault."""
    p = assert_not_real(path, "запись байтов")
    return Path(p).write_bytes(data)


def guarded_mkdir(path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False):
    """`Path.mkdir` с проверкой, что каталог не в реальном Hermes/Vault."""
    p = assert_not_real(path, "создание каталога")
    return Path(p).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)


def guarded_copy2(src, dst, *, follow_symlinks: bool = True):
    """`shutil.copy2`: читать можно откуда угодно, писать — только вне реального Hermes/Vault."""
    target = assert_not_real(dst, "копирование файла")
    return shutil.copy2(src, target, follow_symlinks=follow_symlinks)


def guarded_rmtree(path, ignore_errors: bool = False, onerror=None, **kwargs):
    """`shutil.rmtree` с проверкой, что дерево не в реальном Hermes/Vault."""
    p = assert_not_real(path, "удаление дерева")
    if onerror is not None:
        return shutil.rmtree(p, ignore_errors=ignore_errors, onerror=onerror, **kwargs)
    return shutil.rmtree(p, ignore_errors=ignore_errors, **kwargs)


# ---------------------------------------------------------------- патчи записи

def _write_mode(mode) -> bool:
    return isinstance(mode, str) and any(ch in mode for ch in "wax+")


def _wrap_open(original):
    def guarded_open(file, mode="r", *args, **kwargs):
        if _write_mode(mode):
            assert_not_real(file, "открытие файла на запись")
        return original(file, mode, *args, **kwargs)

    guarded_open.__name__ = "open"
    guarded_open.__doc__ = "open() с запретом записи в реальный Hermes/Vault"
    return guarded_open


def _wrap_path_self(original, what: str):
    """Метод Path, который пишет в САМ путь: проверяется только `self`.

    Данные (`write_text(data)`, `write_bytes(data)`) — НЕ путь и проверяться
    не должны: PNG-байты, начинающиеся с «C:\\…», иначе принимались за путь
    и падало `SafetyError` (D20 из docs/critic/wave5-ui.md).
    """
    def guarded(self, *args, **kwargs):
        assert_not_real(self, what)
        return original(self, *args, **kwargs)

    guarded.__name__ = original.__name__
    return guarded


def _wrap_path_two(original, what: str):
    """Метод Path с двумя путями (`rename(target)`, `replace(target)`)."""
    def guarded(self, target, *args, **kwargs):
        assert_not_real(self, what)
        assert_not_real(target, what)
        return original(self, target, *args, **kwargs)

    guarded.__name__ = original.__name__
    return guarded


def _wrap_os_func(original, what: str):
    def guarded(path, *args, **kwargs):
        assert_not_real(path, what)
        return original(path, *args, **kwargs)

    guarded.__name__ = original.__name__
    return guarded


def _wrap_os_two(original, what: str):
    def guarded(src, dst, *args, **kwargs):
        assert_not_real(src, what)
        assert_not_real(dst, what)
        return original(src, dst, *args, **kwargs)

    guarded.__name__ = original.__name__
    return guarded


def _wrap_shutil_copy(original, what: str):
    def guarded(src, dst, *args, **kwargs):
        assert_not_real(dst, what)          # копирование только ПИШЕТ в dst; src можно читать
        return original(src, dst, *args, **kwargs)

    guarded.__name__ = original.__name__
    return guarded


def _wrap_shutil_move(original):
    def guarded(src, dst, *args, **kwargs):
        assert_not_real(src, "перемещение")   # move удаляет src
        assert_not_real(dst, "перемещение")
        return original(src, dst, *args, **kwargs)

    guarded.__name__ = original.__name__
    return guarded


def _wrap_os_open(original):
    flags_mask = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def guarded(path, flags, *args, **kwargs):
        if int(flags) & flags_mask:
            assert_not_real(path, "открытие файла на запись (os.open)")
        return original(path, flags, *args, **kwargs)

    guarded.__name__ = "open"
    return guarded


class _WriteGuard:
    """Устанавливает и снимает патчи записи; используется через `forbid_real_writes`."""

    def __init__(self) -> None:
        self._patches: list[tuple[object, str, object]] = []

    def _patch(self, obj, name: str, factory) -> None:
        original = getattr(obj, name)
        setattr(obj, name, factory(original))
        self._patches.append((obj, name, original))

    def install(self) -> None:
        self._patch(builtins, "open", _wrap_open)
        self._patch(io, "open", _wrap_open)
        self._patch(os, "open", _wrap_os_open)
        for name, what in (("write_text", "запись текста"), ("write_bytes", "запись байтов"),
                           ("mkdir", "создание каталога"), ("unlink", "удаление файла"),
                           ("touch", "создание файла")):
            # только путь (self); аргументы-данные — не пути и не проверяются
            self._patch(Path, name, lambda orig, w=what: _wrap_path_self(orig, w))
        for name in ("rename", "replace"):
            self._patch(Path, name, lambda orig, w=name: _wrap_path_two(orig, w))
        for name, what in (("replace", "замена файла"), ("rename", "переименование")):
            self._patch(os, name, lambda orig, w=what: _wrap_os_two(orig, w))
        for name, what in (("remove", "удаление файла"), ("unlink", "удаление файла")):
            self._patch(os, name, lambda orig, w=what: _wrap_os_func(orig, w))
        self._patch(os, "rmdir", lambda orig: _wrap_os_func(orig, "удаление каталога"))
        self._patch(shutil, "rmtree", lambda orig: _wrap_os_func(orig, "удаление дерева"))
        for name in ("copy", "copy2", "copyfile", "copytree"):
            self._patch(shutil, name, lambda orig, w=name: _wrap_shutil_copy(orig, w))
        self._patch(shutil, "move", _wrap_shutil_move)

    def restore(self) -> None:
        while self._patches:
            obj, name, original = self._patches.pop()
            setattr(obj, name, original)


# ---------------------------------------------------------------- патч subprocess

def _argv_tokens(argv) -> list[str]:
    if isinstance(argv, bytes):
        argv = os.fsdecode(argv)
    if isinstance(argv, str):
        return [tok for tok in argv.replace("\t", " ").split(" ") if tok]
    tokens = []
    for item in argv:
        tokens.append(os.fsdecode(item) if isinstance(item, bytes) else str(item))
    return tokens


def _exe_name(token: str) -> str:
    text = token.strip().strip("\"'")
    return ntpath.basename(posixpath.basename(text)).lower()


def blocked_hermes_verb(argv) -> str | None:
    """Вернуть запрещённый глагол, если argv запускает агентский CLI Hermes."""
    try:
        tokens = _argv_tokens(argv)
    except TypeError:
        return None
    if not tokens:
        return None
    if not any(_exe_name(tok) in HERMES_EXECUTABLES for tok in tokens):
        return None
    for tok in tokens:
        verb = tok.strip().strip("\"'").lower()
        if verb in BLOCKED_HERMES_VERBS:
            return verb
    return None


def _raise_blocked(argv, verb: str) -> None:
    try:
        shown = " ".join(_argv_tokens(argv))
    except TypeError:
        shown = repr(argv)
    raise SafetyError(
        f"ЗАПРЕЩЕНО запускать агентский CLI Hermes: глагол «{verb}».\n"
        f"  команда: {shown}\n"
        "  Это поднимает РЕАЛЬНЫХ агентов на реальных профилях/ключах и пишет в Vault "
        "(инцидент 28.10). В тестах CLI подменяется shim'ом; на боевом сервере — только "
        "осознанный запуск человеком. См. docs/SAFETY-RULES.md.")


def _wrap_popen(original):
    """Подкласс `subprocess.Popen`: сам Popen — класс, его наследует asyncio.

    Функция-обёртка здесь ломает `class Popen(subprocess.Popen)` в
    `asyncio.windows_utils` (проверено импортом unittest.mock под запретом).
    """

    class _GuardedPopen(original):  # noqa: N801 - имя как у оригинала
        def __init__(self, args, *a, **kwargs):
            verb = blocked_hermes_verb(args)
            if verb:
                _raise_blocked(args, verb)
            super().__init__(args, *a, **kwargs)

    _GuardedPopen.__name__ = "Popen"
    _GuardedPopen.__qualname__ = "Popen"
    _GuardedPopen.__module__ = "subprocess"
    return _GuardedPopen


def _wrap_run(original):
    def guarded_run(args, *a, **kwargs):
        verb = blocked_hermes_verb(args)
        if verb:
            _raise_blocked(args, verb)
        return original(args, *a, **kwargs)

    guarded_run.__name__ = "run"
    return guarded_run


def _wrap_system(original):
    def guarded_system(command):
        verb = blocked_hermes_verb(command)
        if verb:
            _raise_blocked(command, verb)
        return original(command)

    guarded_system.__name__ = "system"
    return guarded_system


class __SubprocessGuard:
    def __init__(self) -> None:
        self._patches: list[tuple[object, str, object]] = []

    def _patch(self, obj, name: str, factory) -> None:
        original = getattr(obj, name)
        setattr(obj, name, factory(original))
        self._patches.append((obj, name, original))

    def install(self) -> None:
        self._patch(subprocess, "Popen", _wrap_popen)
        self._patch(subprocess, "run", _wrap_run)
        self._patch(os, "system", _wrap_system)

    def restore(self) -> None:
        while self._patches:
            obj, name, original = self._patches.pop()
            setattr(obj, name, original)


# ---------------------------------------------------------------- контексты

_lock = threading.RLock()
_writes_guard: _WriteGuard | None = None
_writes_depth = 0
_sub_guard: "__SubprocessGuard | None" = None
_sub_depth = 0


@contextlib.contextmanager
def forbid_real_writes():
    """На время блока запретить запись в реальный Hermes/Vault (вложенный — счётчик)."""
    global _writes_guard, _writes_depth
    with _lock:
        if _writes_depth == 0:
            _writes_guard = _WriteGuard()
            _writes_guard.install()
        _writes_depth += 1
    try:
        yield
    finally:
        with _lock:
            _writes_depth -= 1
            if _writes_depth == 0 and _writes_guard is not None:
                _writes_guard.restore()
                _writes_guard = None


@contextlib.contextmanager
def forbid_real_subprocess():
    """На время блока запретить `hermes dispatch|decompose|swarm|profile|...`."""
    global _sub_guard, _sub_depth
    with _lock:
        if _sub_depth == 0:
            _sub_guard = __SubprocessGuard()
            _sub_guard.install()
        _sub_depth += 1
    try:
        yield
    finally:
        with _lock:
            _sub_depth -= 1
            if _sub_depth == 0 and _sub_guard is not None:
                _sub_guard.restore()
                _sub_guard = None
