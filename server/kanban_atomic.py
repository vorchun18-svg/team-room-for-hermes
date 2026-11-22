"""Остановка карточки ОДНИМ процессом Hermes: воркер убит И карточка удержана (W23-AT1).

Зачем отдельный процесс. Штатные глаголы Hermes устроены так, что остановка «в два шага»
оставляет окно гонки:

* ``hermes kanban reclaim <id>`` — единственный глагол, который **завершает процесс воркера**
  (``_terminate_reclaimed_worker``), но карточку он возвращает в очередь (``ready``/``todo``);
* ``hermes kanban block <id>`` — парковывает карточку в ``blocked``, но процесс воркера
  **не убивает** (``block_task`` просто чистит ``worker_pid``/``claim_lock``).

Если делать это двумя вызовами CLI (~1–2 с каждый), диспетчер (тик 30 с, но и 0,2 с после
освобождения слота) успевает взять карточку из ``ready`` между ними — и человек получает
«остановлено», под которым работает живой воркер. Здесь обе штатные функции вызываются
**подряд в одном процессе**: между коммитом ``reclaim`` и вызовом ``block`` — микросекунды
(замер на поддельной доске: 13–19 мкс против 1,4–2,0 с у второго вызова CLI), результат
проверяется по доске, и при неудаче действие повторяется.

Протокол: одна JSON-строка на входе (аргументом или в stdin) и одна на выходе.

    → {"op": "stop", "task_id": "t_375d5a5f", "db": "<…>/kanban.db", "home": "<kanban home>",
       "tree": "<дерево hermes-agent>", "reason": "остановлено пользователем",
       "kind": "needs_input"|null, "attempts": 3}
    → {"op": "resume", "task_id": "…", …}                       — снять паузу (+ set_model)
    → {"op": "change_model_and_resume", "task_id": "…", "model": "qwen36v"|null,
       "provider": "llamacpp", …}                              — set_model → unblock/reclaim → ready
    → {"op": "restart", "task_id": "…", "model": …}             — set_model → reclaim (для сторожа)

    ← {"ok": true, "op": "stop", "task_id": "…", "status": "blocked", "terminated": true,
       "worker_alive": false, "attempts": 1, "steps": ["reclaim", "block"],
       "card": {"status": "blocked", "worker_pid": null, …}, "ms": 96}
    ← {"ok": false, "op": "stop", "code": "conflict"|"not_found"|"no_hermes"|…, "message": "…"}

``db`` — путь к ``kanban.db`` (его знает сервер: ``cfg.kanban_db(board)``), ``home`` —
``HERMES_KANBAN_HOME`` (чтобы ``kanban_home()`` Hermes не ушёл в боевой корень), ``tree`` —
дерево ``hermes-agent`` для ``sys.path``. Пути не угадываются: их передаёт вызывающая
сторона, поэтому в песочнице (``TEAMROOM_HERMES_HOME``) скрипт работает по поддельной доске.

Публично: ``run(req)``, ``main(argv)`` (сторона Hermes) и ``stop`` / ``resume`` /
``change_model_and_resume`` / ``restart_on_model`` / ``available`` (сторона сервера).
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from pathlib import Path

try:                                             # пакетный импорт (сервер)
    from . import proc
except ImportError:                              # запуск файлом интерпретатором Hermes
    import proc                                  # type: ignore

WORKER = "kanban_atomic.py"
#: автор события в доске (что видит человек в истории карточки)
ACTOR = "team-room"
DEFAULT_ATTEMPTS = 3
MAX_ATTEMPTS = 5
#: Сколько ждать один вызов: reclaim сам ждёт смерть воркера до 5 с.
DEFAULT_TIMEOUT = 45
PROBE_TIMEOUT = 40
MAX_REASON = 200
MAX_MODEL = 200
#: id карточек: `t_<hex8>` (в тестах — `t_<sha1[:8]>`), с запасом на будущее.
TASK_ID_RE = re.compile(r"^t_[A-Za-z0-9_\-]{3,64}$")
#: Статусы, в которых карточку поднимет диспетчер (значит, «удержана» — нет).
SPAWN_STATUSES = ("ready", "review")
#: Статусы «карточка удержана, воркер сам не появится».
HELD_STATUSES = ("blocked", "todo")
#: Переменные окружения агентской сессии: в подпроцесс их передавать нельзя.
CLEAN_ENV_KEYS = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)
STOP_REASON = "остановлено пользователем"
RESUME_REASON = "продолжаем"
#: «Модель не задана — не трогать» (отличать от «снять переопределение» = ``None``).
KEEP = object()
#: Колонки карточки, которые нужны и для решения, и для проверки результата.
CARD_COLUMNS = ("id", "status", "assignee", "claim_lock", "worker_pid", "worker_started_at",
                "current_run_id", "model_override", "provider_override", "block_kind",
                "block_recurrences")


class AtomicError(Exception):
    """Отказ с кодом для вызывающей стороны (``bad_request``|``not_found``|``conflict``|…)."""

    def __init__(self, message: str, code: str = "failed", detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = dict(detail or {})


# ---------------------------------------------------------------- разбор запроса

def _clean_task_id(value) -> str:
    text = str(value or "").strip()
    if not TASK_ID_RE.match(text):
        raise AtomicError("Некорректный id карточки", "bad_request", {"field": "task_id"})
    return text


def _clean_reason(value) -> str:
    """Причина — одна строка, без NUL, до 200 знаков (как `--reason` в CLI)."""
    text = " ".join(str(value or "").split())
    if "\x00" in text:
        raise AtomicError("В причине есть NUL-байт", "bad_request", {"field": "reason"})
    return text[:MAX_REASON]


def _clean_db(value) -> Path:
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise AtomicError("Не передан путь к доске канбана", "bad_request", {"field": "db"})
    path = Path(text)
    if not path.is_absolute():
        raise AtomicError("Путь к доске должен быть абсолютным", "bad_request", {"field": "db"})
    if not path.is_file():
        raise AtomicError("Доска канбана не найдена", "unavailable", {"db": str(path)})
    return path


def _clean_home(value) -> Path:
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise AtomicError("Не передан каталог канбана (HERMES_KANBAN_HOME)", "bad_request",
                          {"field": "home"})
    path = Path(text)
    if not path.is_absolute():
        raise AtomicError("Каталог канбана должен быть абсолютным", "bad_request", {"field": "home"})
    if not path.is_dir():
        raise AtomicError("Каталог канбана не найден", "bad_request", {"field": "home"})
    return path


def _clean_tree(value) -> Path:
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise AtomicError("Не передано дерево Hermes", "no_hermes", {"field": "tree"})
    path = Path(text)
    if not (path / "hermes_cli" / "kanban_db.py").is_file():
        raise AtomicError("В дереве Hermes нет hermes_cli/kanban_db.py", "no_hermes",
                          {"tree": str(path)})
    return path


def _clean_model(value) -> str:
    """Имя модели: пусто/`none`/`null`/`-` — снять переопределение."""
    text = str(value or "").strip()
    if len(text) > MAX_MODEL:
        raise AtomicError("Слишком длинное имя модели", "bad_request", {"field": "model"})
    if "\x00" in text:
        raise AtomicError("В имени модели есть NUL-байт", "bad_request", {"field": "model"})
    if text.lower() in ("", "none", "null", "-"):
        return ""
    if text.startswith("-"):
        raise AtomicError("Некорректное имя модели", "bad_request", {"field": "model"})
    return text


def _clean_provider(value) -> str:
    text = str(value or "").strip()
    if "\x00" in text or text.startswith("-") or len(text) > MAX_MODEL:
        raise AtomicError("Некорректный провайдер", "bad_request", {"field": "provider"})
    return text


def _clean_attempts(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ATTEMPTS
    return max(1, min(MAX_ATTEMPTS, number))


#: Статусы, в которые разрешено возвращать карточку из архива. `archived` здесь нет:
#: возврат из архива не оставляет карточку в архиве. `done` — работа была завершена,
#: остальные — «приостановлено»: карточка удержана и сама не запустится (W29-P4).
RESTORE_STATUSES = ("done", "blocked", "todo", "review", "triage")


def _clean_items(value) -> list:
    """Список ``[{task_id, status}]`` для возврата из архива: строгая проверка."""
    if not isinstance(value, list) or not value:
        raise AtomicError("Список карточек для возврата пуст", "bad_request", {"field": "items"})
    out: list = []
    seen: set = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise AtomicError("Элемент списка карточек должен быть объектом", "bad_request",
                              {"field": "items"})
        task_id = _clean_task_id(raw.get("task_id"))
        status = str(raw.get("status") or "").strip().lower()
        if status not in RESTORE_STATUSES:
            raise AtomicError("Недопустимый статус возврата: %s" % (status or "пусто"),
                              "bad_request", {"field": "status", "allowed": list(RESTORE_STATUSES)})
        if task_id in seen:
            continue
        seen.add(task_id)
        out.append({"task_id": task_id, "status": status})
    return out


def _normalize(req: dict) -> dict:
    """Проверить запрос до импорта Hermes (быстрый отказ на мусоре)."""
    op = str(req.get("op") or "stop").strip().lower()
    if op not in OPS:
        raise AtomicError("Неизвестная операция: %s" % op, "bad_request")
    out = {"op": op,
           # `restore` работает со списком карточек: одиночного id у него нет.
           "task_id": "" if op == "restore" else _clean_task_id(req.get("task_id")),
           "db": _clean_db(req.get("db")), "home": _clean_home(req.get("home")),
           "tree": _clean_tree(req.get("tree")),
           "reason": _clean_reason(req.get("reason")), "attempts": _clean_attempts(req.get("attempts")),
           "kind": req.get("kind")}
    if op in ("resume", "change_model_and_resume", "restart") and "model" in req:
        out["model"] = _clean_model(req.get("model"))
        out["provider"] = _clean_provider(req.get("provider"))
    if op == "restore":
        out["items"] = _clean_items(req.get("items"))
    return out


# ---------------------------------------------------------------- окружение Hermes

def _prepare_env(home: Path, tree: Path) -> None:
    """Окружение и sys.path для импорта Hermes: только присланный каталог канбана."""
    for name in CLEAN_ENV_KEYS:
        os.environ.pop(name, None)
    for name in ("HERMES_YOLO_MODE", "HERMES_PROFILE"):
        os.environ.pop(name, None)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    text = str(tree)
    if text not in sys.path:
        sys.path.insert(0, text)


def _import_kb():
    """Штатные функции канбана Hermes: ``kanban_db`` (глаголы) и ``kanban_db_connect`` (соединение)."""
    try:
        from hermes_cli import kanban_db as kb                     # noqa: PLC0415
        from hermes_cli import kanban_db_connect as kbc           # noqa: PLC0415
    except Exception as exc:                                       # noqa: BLE001
        raise AtomicError("Hermes не импортируется (%s)" % (str(exc)[:200] or "ошибка"),
                          "no_hermes") from exc
    return kb, kbc


def _connect(kb, kbc, db: Path):
    try:
        return kbc.connect(db_path=db)
    except Exception as exc:                                       # noqa: BLE001
        raise AtomicError("Доска канбана недоступна: %s" % (str(exc)[:200] or "ошибка"),
                          "unavailable", {"db": str(db)}) from exc


# ---------------------------------------------------------------- доска: чтение и живость

def _snapshot(con, task_id: str) -> dict:
    """Строка карточки (или пустой словарь — карточки нет)."""
    row = con.execute("SELECT %s FROM tasks WHERE id = ?" % ", ".join(CARD_COLUMNS),
                      (task_id,)).fetchone()
    return dict(row) if row is not None else {}


def _alive(kb, pid, started_at) -> bool:
    """Жив ли ИМЕННО тот процесс, которого мы видели (отпечаток запуска учитывается)."""
    try:
        number = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        return bool(kb._worker_alive(number, started_at))          # noqa: SLF001 - штатная проверка
    except Exception:                                              # noqa: BLE001
        pass
    try:
        return bool(kb._pid_alive(number))                         # noqa: SLF001
    except Exception:                                              # noqa: BLE001
        return False


def _status_of(snap: dict) -> str:
    return str((snap or {}).get("status") or "")


def _stop_kind(kb, snap: dict, asked) -> str:
    """Тип блокировки так, чтобы счётчик петель Hermes не принял это за цикл.

    ``block_task`` считает повторы: второй `block` с ТЕМ ЖЕ ``kind`` уводит карточку в
    ``triage`` (``BLOCK_RECURRENCE_LIMIT = 2``). Для человека это ложное срабатывание:
    «остановил → продолжил → остановил» — не петля воркера. Поэтому тип чередуется:
    ``needs_input`` ↔ без типа (оба читаются интерфейсом как «остановлена»).
    """
    kinds = getattr(kb, "VALID_BLOCK_KINDS", None) or set()
    asked = str(asked or "").strip() or None
    if asked is not None and kinds and asked not in kinds:
        raise AtomicError("Неизвестный тип блокировки: %s" % asked, "bad_request", {"kind": asked})
    kind = asked or "needs_input"
    if str((snap or {}).get("block_kind") or "") == kind:
        return None                       # тот же тип уже стоит — второй раз считался бы петлёй
    return kind


# ---------------------------------------------------------------- операции

def _set_model(kb, con, req: dict, steps: list) -> None:
    """Переопределение модели карточки штатной функцией Hermes (``set_model_override``)."""
    model = req.get("model") or ""
    provider = req.get("provider") or ""
    try:
        kb.set_model_override(con, req["task_id"], model or None,
                              (provider or None) if model else None)
    except Exception as exc:                                       # noqa: BLE001
        raise AtomicError("Движок не принял модель: %s" % (str(exc)[:200] or "ошибка"),
                          "bad_request", {"field": "model"}) from exc
    steps.append("set_model" if model else "clear_model")


def _promote(kb, con, req: dict, steps: list) -> bool:
    """`todo` → `ready` штатной функцией (``promote_task``); отказ — не ошибка."""
    reason = req.get("reason") or ""
    try:
        result = kb.promote_task(con, req["task_id"], actor=ACTOR, reason=reason or None)
    except TypeError:                                              # другая версия Hermes
        try:
            result = kb.promote_task(con, req["task_id"])
        except Exception:                                          # noqa: BLE001
            steps.append("promote_refused")
            return False
    except Exception:                                              # noqa: BLE001
        steps.append("promote_refused")
        return False
    ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
    steps.append("promote" if ok else "promote_refused")
    return ok


def _refuse_status(status: str, op: str) -> None:
    if status in ("done", "archived"):
        raise AtomicError("Карточка уже завершена — останавливать нечего", "conflict",
                          {"status": status, "op": op})
    if status == "review":
        raise AtomicError("Карточка ждёт проверки — работа уже не идёт", "conflict",
                          {"status": status, "op": op})


def _op_stop(kb, con, req: dict, steps: list) -> None:
    """`reclaim` (убить воркер) + `block` (удержать) подряд, в одном процессе."""
    task_id = req["task_id"]
    reason = req.get("reason") or STOP_REASON
    snap = _snapshot(con, task_id)
    if not snap:
        raise AtomicError("Карточка не найдена в доске канбана", "not_found",
                          {"task_id": task_id})
    _refuse_status(_status_of(snap), "stop")
    if _status_of(snap) == "running" or snap.get("claim_lock"):
        try:
            if kb.reclaim_task(con, task_id, reason=reason):
                steps.append("reclaim")
        except Exception as exc:                                   # noqa: BLE001
            raise AtomicError("Не удалось снять работу: %s" % (str(exc)[:200] or "ошибка"),
                              "failed") from exc
        snap = _snapshot(con, task_id) or snap
    status = _status_of(snap)
    if status in ("running", "ready"):
        kind = _stop_kind(kb, snap, req.get("kind"))
        try:
            done = kb.block_task(con, task_id, reason=reason, kind=kind)
        except Exception as exc:                                   # noqa: BLE001
            raise AtomicError("Не удалось поставить на паузу: %s" % (str(exc)[:200] or "ошибка"),
                              "failed") from exc
        if done:
            steps.append("block" if kind else "block:no_kind")
        snap = _snapshot(con, task_id) or snap
    if _status_of(snap) == "todo":
        steps.append("held_by_parents")


def _resume_core(kb, con, req: dict, steps: list) -> None:
    """Снять паузу (и, если задана, модель) — `unblock` + `promote` в одном процессе."""
    task_id = req["task_id"]
    reason = req.get("reason") or RESUME_REASON
    snap = _snapshot(con, task_id)
    if not snap:
        raise AtomicError("Карточка не найдена в доске канбана", "not_found",
                          {"task_id": task_id})
    status = _status_of(snap)
    if status == "running":
        raise AtomicError("Карточка уже работает", "conflict", {"status": status, "op": "resume"})
    _refuse_status(status, "resume")
    if "model" in req:                       # модель ставим ДО того, как карточка станет ready
        _set_model(kb, con, req, steps)
        snap = _snapshot(con, task_id) or snap
        status = _status_of(snap)
    if status in ("blocked", "scheduled"):
        try:
            if kb.unblock_task(con, task_id):
                steps.append("unblock")
        except Exception as exc:                                   # noqa: BLE001
            raise AtomicError("Не удалось снять паузу: %s" % (str(exc)[:200] or "ошибка"),
                              "conflict", {"status": status}) from exc
        snap = _snapshot(con, task_id) or snap
        status = _status_of(snap)
    if status == "todo":
        _promote(kb, con, req, steps)
    elif status == "ready":
        steps.append("queued")


def _op_change_model_and_resume(kb, con, req: dict, steps: list) -> None:
    """«Сменить модель и продолжить»: set_model → (reclaim|unblock) → ready, одним процессом."""
    task_id = req["task_id"]
    reason = req.get("reason") or "меняем модель и продолжаем"
    snap = _snapshot(con, task_id)
    if not snap:
        raise AtomicError("Карточка не найдена в доске канбана", "not_found",
                          {"task_id": task_id})
    status = _status_of(snap)
    _refuse_status(status, "change_model_and_resume")
    if status == "running":
        if "model" in req:
            _set_model(kb, con, req, steps)
        try:
            if kb.reclaim_task(con, task_id, reason=reason):
                steps.append("reclaim")
        except Exception as exc:                                   # noqa: BLE001
            raise AtomicError("Не удалось снять работу: %s" % (str(exc)[:200] or "ошибка"),
                              "failed") from exc
        snap = _snapshot(con, task_id) or snap
        if _status_of(snap) == "todo":
            _promote(kb, con, req, steps)
        return
    _resume_core(kb, con, req, steps)


def _op_restart(kb, con, req: dict, steps: list) -> None:
    """Перезапуск на верной модели (сторож): set_model (если задана) → reclaim → ready."""
    task_id = req["task_id"]
    reason = req.get("reason") or "контроль моделей: неверная модель"
    snap = _snapshot(con, task_id)
    if not snap:
        raise AtomicError("Карточка не найдена в доске канбана", "not_found",
                          {"task_id": task_id})
    status = _status_of(snap)
    _refuse_status(status, "restart")
    if status == "blocked":
        raise AtomicError("Карточка уже остановлена — её поднимает «Продолжить»", "conflict",
                          {"status": status, "op": "restart"})
    if "model" in req:
        _set_model(kb, con, req, steps)
    if status == "running" or snap.get("claim_lock"):
        try:
            if kb.reclaim_task(con, task_id, reason=reason):
                steps.append("reclaim")
        except Exception as exc:                                   # noqa: BLE001
            raise AtomicError("Не удалось снять работу: %s" % (str(exc)[:200] or "ошибка"),
                              "failed") from exc
        snap = _snapshot(con, task_id) or snap
    if _status_of(snap) == "todo":
        _promote(kb, con, req, steps)
    elif _status_of(snap) == "ready":
        steps.append("queued")


def _op_restore(kb, con, req: dict, steps: list) -> dict:
    """Вернуть карточки из архива: `archived` → сохранённый статус, одним заходом.

    Штатного глагола «разархивировать» у Hermes нет (архив терминален), поэтому
    статус возвращаем функциями самой библиотеки канбана в ОДНОМ процессе — тем же
    путём, что `stop`/`resume` (W23-AT1). Модели при этом не запускаются: карточки
    возвращаются в `done` (работа была завершена) либо в `blocked`/`todo`
    (приостановлено, поднимает человек кнопкой «Продолжить»).
    """
    items = req.get("items") or []
    write_txn = getattr(kb, "write_txn", None)
    append = getattr(kb, "_append_event", None)
    restored: list = []

    def apply() -> None:
        for item in items:
            cur = con.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, worker_started_at = NULL, current_run_id = NULL "
                "WHERE id = ? AND status = 'archived'",
                (item["status"], item["task_id"]))
            if not cur.rowcount:
                continue
            steps.append("restore:%s" % item["task_id"])
            if callable(append):
                append(con, item["task_id"], "status",
                       {"status": item["status"], "previous_status": "archived",
                        "reason": "restored_from_archive"})
            restored.append(dict(item))

    if callable(write_txn):
        with write_txn(con):
            apply()
    else:                                   # pragma: no cover - старая библиотека
        apply()
        con.commit()
    return {"ok": bool(restored), "op": "restore", "restored": restored,
            "count": len(restored), "status": ""}


OPS = {
    "stop": _op_stop,
    "resume": _resume_core,
    "change_model_and_resume": _op_change_model_and_resume,
    "restart": _op_restart,
    # W29-P4: возврат из архива — не одиночная карточка, а список (см. `run`).
    "restore": _op_restore,
}


def _verdict(op: str, snap: dict, worker_alive: bool) -> bool:
    """Достигнуто ли то, что обещает операция (без вранья про «остановлено»)."""
    status = _status_of(snap)
    if op == "stop":
        return status in HELD_STATUSES and not worker_alive
    return status in ("ready", "todo", "review") and not worker_alive


# ---------------------------------------------------------------- одна операция целиком

def _board_guard(cfg, db=None) -> None:
    """W29-I1: песочница не пишет на реальную доску Hermes — и библиотекой тоже."""
    try:
        from . import board_guard as guard
    except ImportError:                                       # pragma: no cover
        import board_guard as guard                           # type: ignore
    found = guard.check(cfg, db=db)
    if found:
        raise AtomicError(found["message"], guard.CODE, dict(found["detail"] or {}))


def run(req: dict) -> dict:
    """Выполнить операцию из запроса. Ошибки — словарём с ``ok: false`` (не исключением)."""
    started = time.perf_counter()
    if not isinstance(req, dict):
        return {"ok": False, "code": "bad_request", "message": "Запрос должен быть объектом"}
    op = str(req.get("op") or "stop").strip().lower()
    steps: list = []
    try:
        # W29-I1: проверка ДО разбора запроса — иначе отказ «нет hermes_cli» маскирует запрет.
        _board_guard(None, str(req.get("db") or "") or None)
        out = _normalize(req)
        _prepare_env(out["home"], out["tree"])
        kb, kbc = _import_kb()
        con = _connect(kb, kbc, out["db"])
        try:
            if out["op"] == "restore":
                result = _op_restore(kb, con, out, steps)
            else:
                result = _perform(kb, con, out, steps)
        finally:
            try:
                con.close()
            except Exception:                                      # noqa: BLE001
                pass
    except AtomicError as exc:
        return {"ok": False, "op": op, "code": exc.code, "message": exc.message,
                "detail": dict(exc.detail), "steps": steps,
                "ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:                                       # noqa: BLE001
        return {"ok": False, "op": op, "code": "failed",
                "message": "Не удалось: %s" % (str(exc)[:240] or exc.__class__.__name__),
                "steps": steps, "ms": int((time.perf_counter() - started) * 1000)}
    result["steps"] = steps
    result["ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _perform(kb, con, req: dict, steps: list) -> dict:
    """Цикл «действие → проверка по доске → повтор», до ``attempts`` раз."""
    task_id = req["task_id"]
    op = req["op"]
    first = _snapshot(con, task_id)
    if not first:
        raise AtomicError("Карточка не найдена в доске канбана", "not_found",
                          {"task_id": task_id})
    pid = int(first.get("worker_pid") or 0)
    started_at = first.get("worker_started_at")
    lock = str(first.get("claim_lock") or "")
    used = 0
    snap = first
    alive = _alive(kb, pid, started_at)
    for attempt in range(1, req["attempts"] + 1):
        used = attempt
        OPS[op](kb, con, req, steps)
        snap = _snapshot(con, task_id) or snap
        alive = _alive(kb, pid, started_at)
        if _verdict(op, snap, alive):
            break
        if attempt < req["attempts"]:
            time.sleep(0.15)
    if alive and pid and lock and op in ("stop", "restart", "change_model_and_resume"):
        # Последняя попытка добить: та же штатная проверка отпечатка (чужой pid не сигналим).
        try:
            kb._terminate_reclaimed_worker(pid, lock, started_at=started_at)  # noqa: SLF001
            steps.append("terminate")
        except Exception:                                          # noqa: BLE001
            pass
        alive = _alive(kb, pid, started_at)
    return {"ok": bool(_verdict(op, snap, alive)), "op": op, "task_id": task_id,
            "status": _status_of(snap), "terminated": bool(pid) and not alive,
            "worker_alive": bool(alive), "attempts": used,
            "card": {"status": _status_of(snap), "assignee": str(snap.get("assignee") or ""),
                     "worker_pid": snap.get("worker_pid"), "claim_lock": snap.get("claim_lock"),
                     "model_override": snap.get("model_override"),
                     "provider_override": snap.get("provider_override"),
                     "block_kind": snap.get("block_kind")}}


# ---------------------------------------------------------------- запуск (сторона Hermes)

def _utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    """Один запрос — один ответ. Код возврата 0 даже при отказе: отказ в JSON."""
    _utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    raw = args[0] if args else ""
    if not raw or raw == "-":
        try:
            raw = sys.stdin.read()
        except Exception:                                          # noqa: BLE001
            raw = ""
    try:
        req = json.loads(raw) if raw.strip() else {}
    except ValueError:
        result = {"ok": False, "code": "bad_request", "message": "Некорректный JSON-запрос"}
    else:
        result = run(req)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- сторона сервера

_PYTHON_CACHE: dict = {}


def hermes_tree(cfg) -> Path | None:
    """Дерево ``hermes-agent`` с ``hermes_cli/kanban_db.py`` (пути автора не зашиты)."""
    home = Path(getattr(cfg, "hermes_home", "") or "")
    candidates = [home / "hermes-agent", home.parent / "hermes-agent",
                  Path(__file__).resolve().parent.parent / "hermes-agent"]
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if local:
        candidates.append(Path(local) / "hermes" / "hermes-agent")
    for candidate in candidates:
        try:
            if (candidate / "hermes_cli" / "kanban_db.py").is_file():
                return candidate
        except OSError:
            continue
    return None


def python_candidates(cfg) -> list[str]:
    """Кандидаты: явный ``TEAMROOM_HERMES_PYTHON``, свой интерпретатор, venv'ы Hermes."""
    explicit = os.environ.get("TEAMROOM_HERMES_PYTHON")
    if explicit is not None:
        return [str(explicit).strip()] if str(explicit).strip() else []
    found: list[str] = [sys.executable] if sys.executable else []
    home = Path(getattr(cfg, "hermes_home", "") or "")
    roots = [home, ]
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if local:
        roots.append(Path(local) / "hermes")
    for base in roots:
        try:
            pattern = str(base / "installs" / "*" / "environments" / "*" /
                          "venv" / "Scripts" / "python.exe")
            for path in sorted(glob.glob(pattern), reverse=True):
                if path not in found:
                    found.append(path)
        except OSError:
            continue
    return found


def _has_kanban(python: str, tree, timeout: int = PROBE_TIMEOUT) -> bool:
    """Видит ли интерпретатор штатные функции канбана Hermes."""
    if not python or not Path(python).is_file():
        return False
    code = "import sys; sys.path.insert(0, %r); import hermes_cli.kanban_db" % (str(tree or ""),)
    try:
        import subprocess                                        # noqa: PLC0415
        done = proc.run([python, "-c", code], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=timeout)
    except Exception:                                            # noqa: BLE001
        return False
    return getattr(done, "returncode", 1) == 0


def find_python(cfg, refresh: bool = False) -> str | None:
    """Интерпретатор Hermes (умеет ``import hermes_cli.kanban_db``); ``None`` — не нашли."""
    key = str(getattr(cfg, "hermes_home", "")) + "|" + str(getattr(cfg, "sandbox", False))
    cached = _PYTHON_CACHE.get(key)
    if cached and not refresh and Path(str(cached)).is_file():
        return str(cached)
    tree = hermes_tree(cfg)
    if tree is None:
        return None
    for path in python_candidates(cfg):
        if _has_kanban(path, tree):
            _PYTHON_CACHE[key] = str(path)
            return str(path)
    return None


def reset_cache() -> None:
    """Сбросить кеш поиска интерпретатора (тесты)."""
    _PYTHON_CACHE.clear()


def available(cfg) -> bool:
    """Есть ли чем выполнить действие одним процессом (интерпретатор + дерево Hermes)."""
    return hermes_tree(cfg) is not None and find_python(cfg) is not None


def worker_path() -> Path:
    return Path(__file__).resolve().parent / WORKER


def _env(cfg) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CLEAN_ENV_KEYS}
    env["HERMES_KANBAN_HOME"] = str(getattr(cfg, "kanban_home", "") or "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def request(cfg, board: str, op: str, task_id, **extra) -> dict:
    """Запрос для подпроцесса: пути передаём явно (боевая доска и песочница — одинаково)."""
    payload = {"op": str(op), "task_id": str(task_id or ""),
               "db": str(cfg.kanban_db(board or "")), "home": str(cfg.kanban_home),
               "attempts": DEFAULT_ATTEMPTS}
    payload.update({k: v for k, v in extra.items() if v is not KEEP})
    return payload


def launch(cfg, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Выполнить действие интерпретатором Hermes и разобрать ответ. Ошибки — AtomicError."""
    _board_guard(cfg, (payload or {}).get("db") if isinstance(payload, dict) else None)
    python = find_python(cfg)
    tree = hermes_tree(cfg)
    if not python or tree is None:
        raise AtomicError("Не нашёл интерпретатор Hermes — действие в один процесс недоступно",
                          "no_hermes")
    data = dict(payload)
    data.setdefault("tree", str(tree))
    try:
        import subprocess                                        # noqa: PLC0415
        done = proc.run([python, str(worker_path()),
                         json.dumps(data, ensure_ascii=False)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=timeout, env=_env(cfg),
                        cwd=str(getattr(cfg, "app_dir", "") or "") or None)
    except Exception as exc:                                     # noqa: BLE001
        raise AtomicError("Действие не запустилось: %s" % (str(exc)[:200] or "ошибка"),
                          "failed", {"op": data.get("op")}) from exc
    out = getattr(done, "stdout", "") or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    result = _parse(out)
    if result is None:
        err = (getattr(done, "stderr", "") or "")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        raise AtomicError("Hermes не ответил понятным JSON: %s"
                          % (" ".join(str(err).split())[:200] or "пустой ответ"),
                          "failed", {"op": data.get("op")})
    if not result.get("ok") and result.get("code"):
        # Отказ самого действия (not_found/conflict/bad_request) — исключением: вызывающая
        # сторона не должна принять его за результат. «Воркер жив» — результат, не отказ.
        raise AtomicError(str(result.get("message") or "Действие отклонено"),
                          str(result.get("code")),
                          dict(result.get("detail") or {}) if isinstance(result.get("detail"), dict)
                          else {})
    return result


def _parse(text: str) -> dict | None:
    """Последняя JSON-строка вывода (Hermes может напечатать предупреждения до неё)."""
    for line in reversed([item for item in str(text or "").splitlines() if item.strip()]):
        try:
            value = json.loads(line, strict=False)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and "ok" in value:
            return value
    return None


def stop(cfg, board: str, task_id, *, reason: str = "", kind=None,
         timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Убить воркер И удержать карточку в ``blocked`` одним процессом Hermes."""
    return launch(cfg, request(cfg, board, "stop", task_id, reason=reason or STOP_REASON,
                               kind=kind), timeout=timeout)


def resume(cfg, board: str, task_id, *, reason: str = "", model=KEEP, provider: str = "",
           timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Снять паузу; если задана модель — поставить её в том же процессе (до ``ready``)."""
    extra: dict = {"reason": reason or RESUME_REASON}
    if model is not KEEP:
        extra["model"] = None if model is None else model
        extra["provider"] = provider or ""
    return launch(cfg, request(cfg, board, "resume", task_id, **extra), timeout=timeout)


def change_model_and_resume(cfg, board: str, task_id, *, model=None, provider: str = "",
                            reason: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """`set_model` → (`reclaim` | `unblock`) → `ready`: «Сменить модель и продолжить»."""
    return launch(cfg, request(cfg, board, "change_model_and_resume", task_id,
                               model=None if model is None else model,
                               provider=provider or "",
                               reason=reason or "меняем модель и продолжаем"), timeout=timeout)


def restart_on_model(cfg, board: str, task_id, *, model=KEEP, provider: str = "",
                     reason: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Перезапуск на верной модели (сторож): `set_model` (если задана) + `reclaim`, без паузы."""
    extra: dict = {"reason": reason or "контроль моделей: неверная модель"}
    if model is not KEEP:
        extra["model"] = None if model is None else model
        extra["provider"] = provider or ""
    return launch(cfg, request(cfg, board, "restart", task_id, **extra), timeout=timeout)


def restore(cfg, board: str, items, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Вернуть карточки из архива одним процессом Hermes (``items``: id → статус)."""
    return launch(cfg, request(cfg, board, "restore", "", items=list(items or [])),
                  timeout=timeout)


__all__ = ["AtomicError", "HELD_STATUSES", "KEEP", "RESTORE_STATUSES", "SPAWN_STATUSES",
           "STOP_REASON", "available",
           "change_model_and_resume", "find_python", "hermes_tree", "launch", "main",
           "python_candidates", "request", "reset_cache", "restart_on_model", "restore",
           "resume", "run", "stop", "worker_path"]


if __name__ == "__main__":
    sys.exit(main())
