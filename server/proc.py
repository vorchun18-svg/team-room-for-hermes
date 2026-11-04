"""Единая точка запуска внешних процессов: без мигающих чёрных консольных окон.

Зачем: на Windows `subprocess` из процесса без своей консоли (сервер, сторож,
диктовка) создаёт ребёнку НОВОЕ консольное окно — при входе в комнату и переходах
между комнатами оно на мгновение мигает чёрным. Лечится `creationflags=
CREATE_NO_WINDOW`; забыть флаг в одном месте достаточно, чтобы окно вернулось,
поэтому все запуски в `server/` и `tools/` идут только через этот модуль.

    from . import proc
    proc.run(argv, ...)              # дождаться результата: CLI, ffmpeg, проверки
    proc.popen(argv, ...)            # долгоживущий процесс: сервер, воркер диктовки
    proc.popen(argv, detached=True)  # фоновый, переживает родителя
    proc.popen(argv, gui=True)       # GUI-программа (проводник): окно видно, флага нет

Правила:
  * `shell=True` запрещён — argv передаётся списком, пути не попадают в оболочку;
  * на не-Windows `creationflags` не передаётся вовсе (POSIX его не принимает);
  * исключения (`TimeoutExpired`, `FileNotFoundError`, `OSError`) не глотаются —
    вызывающая сторона по-прежнему превращает их в свои ошибки.

Публично: no_window(), detached_flags(), run(), popen().
"""
from __future__ import annotations

import os
import subprocess
import sys

#: Значения Win32 (в `subprocess` они есть только на Windows).
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
#: `DETACHED_PROCESS` сознательно НЕ используется: замер `tools/window_spy.py`
#: показал, что процесс с этим флагом получает новое окно Windows Terminal
#: (в сессии, где терминал выбран приложением по умолчанию) — то самое мигание.
DETACHED_PROCESS = 0x00000008

#: Windows ли это (на POSIX флаги не передаются вообще).
IS_WINDOWS = os.name == "nt"
#: macOS ли это (открытие папок — `open`, а не `xdg-open`).
IS_MACOS = sys.platform == "darwin"


def no_window() -> int:
    """`CREATE_NO_WINDOW` на Windows, 0 в остальных системах."""
    return CREATE_NO_WINDOW if IS_WINDOWS else 0


def detached_flags() -> int:
    """Флаги фонового долгоживущего процесса: без окна и в своей группе.

    `CREATE_NEW_PROCESS_GROUP` отделяет ребёнка от группы родителя (Ctrl+C и
    закрытие терминала его не трогают), а `DETACHED_PROCESS` не ставим: он сам
    открывает окно Windows Terminal (проверено `tools/window_spy.py`), при этом
    живучесть обеспечивает и одна новая группа — процесс переживает выход родителя.
    """
    if not IS_WINDOWS:
        return 0
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def _flags(extra: int | None = None, *, base: int | None = None) -> int:
    """Слить базовые флаги (по умолчанию «без окна») с явно переданными."""
    value = no_window() if base is None else base
    with_extra = int(extra) if extra else 0
    return value | with_extra


def _reject_shell(kwargs: dict) -> None:
    """`shell=True` с путём — это окно cmd и инъекция; отказываем сразу."""
    if kwargs.get("shell"):
        raise ValueError("proc: shell=True запрещён — аргументы передаются списком")


def _apply(options: dict, flags: int) -> None:
    """Записать creationflags, но только там, где их принимают."""
    if flags or IS_WINDOWS:
        options["creationflags"] = flags


def run(argv, *, module=None, timeout=None, creationflags=None, **kwargs):
    """Запустить процесс, дождаться конца. Возвращает `CompletedProcess`.

    ``module`` — объект с `.run` для подмены в тестах (по умолчанию `subprocess`):
    существующие тесты подменяют `engine_cli.subprocess`, и вызовы должны идти
    через ту же подмену. Флаги окна считает этот модуль, а не вызывающий код.
    """
    _reject_shell(kwargs)
    mod = subprocess if module is None else module
    options = dict(kwargs)
    _apply(options, _flags(creationflags))
    if timeout is not None:
        options["timeout"] = timeout
    return mod.run([str(a) for a in argv], **options)


def popen(argv, *, module=None, gui: bool = False, detached: bool = False,
          creationflags=None, **kwargs):
    """Запустить процесс и не ждать. Возвращает `Popen`.

    ``gui=True`` — GUI-программа (проводник): её окно должно быть видно, поэтому
    `CREATE_NO_WINDOW` не ставится, а потоки уводятся в DEVNULL, чтобы ребёнок не
    держал наши дескрипторы. ``detached=True`` — фоновый долгоживущий (сервер).
    """
    _reject_shell(kwargs)
    mod = subprocess if module is None else module
    options = dict(kwargs)
    base = 0 if gui else no_window()
    flags = _flags(creationflags, base=base)
    if detached:
        flags |= detached_flags() if IS_WINDOWS else 0
    _apply(options, flags)
    options.setdefault("close_fds", True)
    if gui:
        options.setdefault("stdin", subprocess.DEVNULL)
        options.setdefault("stdout", subprocess.DEVNULL)
        options.setdefault("stderr", subprocess.DEVNULL)
    return mod.Popen([str(a) for a in argv], **options)


def open_path(target) -> bool:
    """Открыть папку или файл программой по умолчанию в сессии пользователя.

    Windows — `os.startfile` (ShellExecute), macOS — `open`, остальные POSIX —
    `xdg-open`. Возвращает False, если открыть не удалось (нет программы,
    запрещено окружением); исключения наружу не выходят — вызывающий решает,
    что показать пользователю.
    """
    target = str(target)
    if IS_WINDOWS and hasattr(os, "startfile"):
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        except (OSError, ValueError):
            pass
    argv = ["open", target] if IS_MACOS else ["xdg-open", target]
    try:
        popen(argv, gui=True)
    except (OSError, ValueError):
        return False
    return True

