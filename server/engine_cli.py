"""Запись в канбан: запуск CLI `hermes kanban …` (subprocess), чистый env, журнал `engine.log`, ошибки.

Нижний слой `engine_write`: без знания о карточках комнат. Тело карточки передаётся файлом,
команда в журнал пишется без тела; `EngineWriteError(code, message_ru, stderr_tail)` — единая ошибка записи.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

try:                                    # import server.engine_cli
    from . import proc
except ImportError:                      # python server/app.py (плоский импорт)
    import proc                          # type: ignore

DEFAULT_TIMEOUT = 30
# переменные окружения, которые нельзя наследовать от агентской сессии
CLEAN_ENV_KEYS = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)


class EngineWriteError(RuntimeError):
    """Ошибка записи в канбан: машинный код + русский текст + хвост stderr.

    ``detail`` — диагностика для тела ответа (``error.detail``), ``http_status`` —
    подсказка HTTP-слою (409 для запрета/состояния, 429 для частоты).
    """

    def __init__(self, code: str, message_ru: str, stderr_tail: str = "",
                 detail: dict | None = None, http_status: int | None = None):
        super().__init__(message_ru)
        self.code = str(code or "engine_error")
        self.message_ru = str(message_ru or "Ошибка канбана")
        self.stderr_tail = clean_stderr((stderr_tail or "")[-4000:])[-1200:]
        self.detail = dict(detail or {})
        self.http_status = http_status

    def to_dict(self) -> dict:
        """Форма ошибки API: {error: {code, message, detail}} (см. docs/API.md §1)."""
        detail = dict(self.detail)
        if self.stderr_tail:
            detail.setdefault("stderr", self.stderr_tail)
        return {"error": {"code": self.code, "message": self.message_ru,
                          "detail": detail}}

    def __str__(self) -> str:  # в логах и тестах — русский текст
        return self.message_ru


def _tail(value, limit: int = 1200) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return (value or "")[-limit:]


def clean_stderr(text: str) -> str:
    """Убрать из stderr Python-трассировки: пользователю нужен смысл, а не стек.

    Hermes CLI при первом запуске печатает «preparing dependencies…» и на сбое —
    многострочный traceback. В ответ API (и в плашку интерфейса) он не нужен:
    оставляем строки без кадров ``File "…", line N`` и без самого «Traceback».
    """
    lines = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Traceback (most recent call last)"):
            continue
        if stripped.startswith('File "') or stripped.startswith("File '"):
            continue
        if stripped.startswith(("~~~", "^^^")) or set(stripped) <= set("~^ "):
            continue
        if stripped.startswith("Exception ignored"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _env(cfg) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CLEAN_ENV_KEYS}
    env["HERMES_KANBAN_HOME"] = str(cfg.kanban_home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _argv(cfg, board: str | None, args) -> list[str]:
    argv = [str(cfg.hermes_cli), "kanban"]
    if board:
        argv += ["--board", str(board)]
    argv += [str(a) for a in args]
    return argv


def opt(name: str, value) -> str:
    """Флаг со значением одним токеном: `--tenant=room:cafe`.

    Так значение, начинающееся с дефиса, не может стать отдельным флагом CLI
    (D1: `task_id="--rm=…"` больше не превращается в опцию).
    """
    return "--%s=%s" % (name, value)


def positional(*values) -> list[str]:
    """Позиционные значения после `--` (значения с ведущим дефисом — отказ).

    Возвращает `["--", v1, v2, …]`: `hermes kanban … archive -- <id>` — argparse
    после `--` считает всё позиционными аргументами.
    """
    out = ["--"]
    for value in values:
        text = str(value)
        if not text or text.startswith("-") or "\x00" in text:
            raise EngineWriteError("bad_request",
                                   "Недопустимое значение аргумента команды канбана.",
                                   detail={"value": text[:80]})
        out.append(text)
    return out


def _guard_argv(argv: list[str]) -> None:
    """NUL в аргументе → 400, а не ValueError из subprocess (D12)."""
    for token in argv:
        if "\x00" in token:
            raise EngineWriteError("bad_request", "В параметрах команды есть NUL-байт.",
                                   detail={"field": "argv"})


def _log_args(argv: list[str]) -> str:
    """Команда для лога: без тел (длинные токены и текст после `--` скрываются)."""
    out: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            out.append(f"<текст {len(token)} симв.>")
            redact_next = False
            continue
        if token == "--":
            out.append(token)
            redact_next = True
            continue
        if "\n" in token or "\r" in token or len(token) > 60:
            out.append(f"<{len(token)} симв.>")
            continue
        out.append(token)
    return " ".join(out)


def _log(cfg, argv: list[str], ms: int, code: int | None, err: str = "") -> None:
    """Строка в data/logs/engine.log: время, мс, код, команда (без тела)."""
    try:
        log_dir = Path(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} | {ms}ms | code={code} | {_log_args(argv)}"
        if err:
            line += " | err=" + re.sub(r"\s+", " ", _tail(err, 300))
        with open(log_dir / "engine.log", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _classify(stderr: str) -> str:
    text = (stderr or "").lower()
    if not text:
        return "engine_error"
    if "does not exist" in text or "no board" in text:
        return "engine_error"
    if "cannot " in text or "terminal state" in text or "not in review" in text \
            or "not blocked" in text or "active run" in text or "already" in text:
        return "conflict"
    if "unknown task" in text or "no such task" in text or "unknown id" in text or "not found" in text:
        return "not_found"
    return "engine_error"


_MSG = {
    "not_found": "Карточка не найдена в доске канбана.",
    "conflict": "Действие невозможно в текущем состоянии карточки.",
    "engine_error": "Канбан вернул ошибку.",
    "bad_request": "Некорректные параметры действия.",
}


def _error_from(stderr: str, code: str | None = None) -> EngineWriteError:
    kind = code or _classify(stderr)
    message = _MSG.get(kind, _MSG["engine_error"])
    if "board" in (stderr or "").lower() and "does not exist" in (stderr or "").lower():
        message = "Доска канбана не найдена."
    return EngineWriteError(kind, message, stderr)


def _run(cfg, board: str | None, args, *, timeout: int = DEFAULT_TIMEOUT):
    """Один вызов CLI. Возвращает (stdout, ms); ошибки → EngineWriteError."""
    argv = _argv(cfg, board, args)
    _guard_argv(argv)
    if board and not Path(cfg.kanban_db(board)).is_file():
        # Доски нет — CLI начал бы создавать окружение и висеть минутами. Отвечаем сразу.
        raise EngineWriteError(
            "engine_error",
            "Доска канбана «%s» ещё не создана (%s) — записи недоступны, чтение работает."
            % (board, cfg.kanban_db(board)),
            detail={"board": board, "db": str(cfg.kanban_db(board))})
    env = _env(cfg)
    started = time.perf_counter()
    try:
        cwd = str(getattr(cfg, "app_dir", "") or "") or None
        # module=subprocess: тесты подменяют `engine_cli.subprocess`, а флаги окна
        # (CREATE_NO_WINDOW) ставит proc — иначе CLI мигает консолью.
        done = proc.run(argv, module=subprocess, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=timeout,
                        env=env, cwd=cwd)
    except FileNotFoundError:
        ms = int((time.perf_counter() - started) * 1000)
        _log(cfg, argv, ms, None, "hermes not found")
        raise EngineWriteError("engine_error",
                               "Не найден исполняемый файл hermes — записи в канбан недоступны.")
    except subprocess.TimeoutExpired as exc:
        ms = int((time.perf_counter() - started) * 1000)
        _log(cfg, argv, ms, None, "timeout")
        raise EngineWriteError("engine_error",
                               f"Канбан не ответил за {int(timeout)} секунд.", _tail(exc.stderr))
    ms = int((time.perf_counter() - started) * 1000)
    _log(cfg, argv, ms, done.returncode, done.stderr or "")
    if done.returncode != 0:
        raise _error_from(done.stderr or done.stdout or "")
    return done.stdout or "", ms


def _run_json(cfg, board: str | None, args, *, timeout: int = DEFAULT_TIMEOUT):
    """Вызов CLI с `--json`: (распарсенный объект, ms)."""
    out, ms = _run(cfg, board, args, timeout=timeout)
    try:
        return json.loads(out, strict=False), ms
    except (json.JSONDecodeError, ValueError):
        raise EngineWriteError("engine_error", "Не удалось разобрать ответ канбана.",
                               _tail(out))
