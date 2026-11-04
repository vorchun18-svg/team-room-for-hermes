"""Запись в канбан «Комнаты команды» — только через CLI `hermes kanban`.

Правила (docs/ENGINE-FACTS.md, ARCHITECTURE §1.4, §12):
* приложение НИКОГДА не пишет в kanban.db напрямую — только subprocess CLI;
* `--board` ставится ПЕРЕД глаголом;
* тело карточки — через `--body-file` (UTF-8 без BOM), комментарий — позициональным
  текстом после `--`, автора задаём `--author`;
* env перед вызовом чистится от `HERMES_DELEGATED_CHILD_CONTEXT`, `HERMES_KANBAN_DB`,
  `HERMES_KANBAN_TASK`, `HERMES_KANBAN_BOARD` и получает `HERMES_KANBAN_HOME`;
* каждый вызов пишется в `data/logs/engine.log` (команда без тела, мс, код возврата);
* `decompose` — только по явному вызову пользователя (LLM, таймаут 180 с).

Чтение (статус карточки перед действием) — sqlite `mode=ro`, это разрешено.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- раскладка по файлам: перенесённые имена доступны отсюда, как и раньше ---
try:
    from .engine_cli import (CLEAN_ENV_KEYS, DEFAULT_TIMEOUT, EngineWriteError, _MSG, _argv, _classify, _env,
        _error_from, _guard_argv, _log, _log_args, _run, _run_json, _tail, opt, positional)  # noqa: F401
    from .engine_write_guard import (AUTHOR_MAXIM, DISPATCH_COOLDOWN_S, DISPATCH_MAX_DEFAULT,
        DISPATCH_MAX_LIMIT, TASK_ID_RE, UNASSIGN_VALUES, assert_author, assert_owned,
        assert_profile, assert_task_id, dispatch_board, dispatch_guard, dispatch_max, no_nul,
        reset_dispatch_guard, tree_ids)  # noqa: F401
except ImportError:  # pragma: no cover
    from engine_cli import (CLEAN_ENV_KEYS, DEFAULT_TIMEOUT, EngineWriteError, _MSG, _argv, _classify, _env,
        _error_from, _guard_argv, _log, _log_args, _run, _run_json, _tail, opt, positional)  # noqa: F401  # type: ignore
    from engine_write_guard import (AUTHOR_MAXIM, DISPATCH_COOLDOWN_S, DISPATCH_MAX_DEFAULT,  # type: ignore
        DISPATCH_MAX_LIMIT, TASK_ID_RE, UNASSIGN_VALUES, assert_author, assert_owned,
        assert_profile, assert_task_id, dispatch_board, dispatch_guard, dispatch_max, no_nul,
        reset_dispatch_guard, tree_ids)  # noqa: F401

try:  # пакетный и «плоский» импорт: аккуратное число в текстах ошибок
    from .app_common import ru_num
except ImportError:  # pragma: no cover
    from app_common import ru_num  # type: ignore

DECOMPOSE_TIMEOUT = 180
#: Предел текста заметки (комментарий, ответ, доработка). Текст запроса
#: проверяется в HTTP-слое по `app_common.MAX_TEXT` (1 000 000) и в карточку
#: идёт файлом, а не аргументом командной строки.
MAX_TEXT = 200_000
CREATED_BY = "Комната команды"
TENANT_PREFIX = "room:"


_ACTIONS = ("dispatch", "comment", "answer", "unblock", "reassign", "rework", "retry",
            "archive", "decompose", "create_card")


# ---------------------------------------------------------------- служебное


def _write_body(cfg, body: str, slug: str) -> Path:
    """Временный файл тела: UTF-8 без BOM.

    Канбан читает `--body-file` через `read_text(encoding='utf-8-sig')`, то есть с
    универсальными переводами строк: CRLF всё равно станет LF. Нормализуем заранее,
    чтобы в карточке было ровно то, что мы записали (содержимое и кавычки — дословно).
    """
    tmp_dir = Path(cfg.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(slug or "body"))[:40] or "body"
    path = tmp_dir / f"{safe}-{uuid.uuid4().hex[:8]}.md"
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(text.encode("utf-8"))
    return path


def _title(title: str, body: str = "") -> str:
    text = (title or "").strip()
    if not text:
        for line in (body or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                text = line[:120]
                break
    text = " ".join(text.split()) or "Запрос"
    if text.startswith("-"):
        text = "Запрос: " + text
    return text[:200]


def _validate_text(text: str, field: str = "текст") -> str:
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    if not text.strip():
        raise EngineWriteError("bad_request", f"Пустой {field}.")
    if len(text) > MAX_TEXT:
        raise EngineWriteError("bad_request",
                               f"Слишком длинный {field}: {ru_num(len(text))} знаков, "
                               f"а можно не больше {ru_num(MAX_TEXT)}. Сократите текст.")
    return no_nul(text, field)


# ---------------------------------------------------------------- чтение статуса

def _db_path(cfg, board: str) -> Path:
    return Path(cfg.kanban_db(board))


def _task_row(cfg, board: str, task_id: str) -> dict | None:
    """Строка tasks (только чтение, mode=ro). None — карточки/БД нет."""
    if not task_id:
        return None
    db = _db_path(cfg, board)
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return None
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id,title,status,assignee,tenant,created_at FROM tasks WHERE id=?",
                          (str(task_id),)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def last_comment(cfg, board: str, task_id: str) -> dict | None:
    """Последний комментарий карточки: ``{id, author, text, at}`` (docs/API.md §6.3)."""
    if not task_id:
        return None
    db = _db_path(cfg, board)
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return None
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id, author, body, created_at FROM task_comments "
                          "WHERE task_id = ? ORDER BY id DESC LIMIT 1", (str(task_id),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None:
        return None
    return {"id": int(row["id"]), "author": str(row["author"] or "mikhail"),
            "text": str(row["body"] or ""), "at": int(row["created_at"] or 0)}


def _related_ids(cfg, board: str, task_id: str) -> list[str]:
    """id карточки и её соседей в обе стороны (decompose: parent_id→child_id=root)."""
    db = _db_path(cfg, board)
    if not db.exists():
        return [task_id]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return [task_id]
    try:
        rows = con.execute(
            "SELECT id FROM tasks WHERE id IN ("
            " SELECT ? UNION SELECT parent_id FROM task_links WHERE child_id=?"
            " UNION SELECT child_id FROM task_links WHERE parent_id=?)",
            (task_id, task_id, task_id)).fetchall()
        return [r[0] for r in rows] or [task_id]
    except sqlite3.Error:
        return [task_id]
    finally:
        con.close()


# ---------------------------------------------------------------- диспетчер

def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _gateway_alive(cfg) -> bool:
    """Диспетчер без engine_read: gateway_state.json (свежесть ≤120 с) + живой pid."""
    home = Path(cfg.hermes_home)
    try:
        state = json.loads((home / "gateway_state.json").read_text("utf-8", "replace"))
    except (OSError, ValueError):
        state = {}
    if not state:
        try:
            pid = json.loads((home / "gateway.pid").read_text("utf-8", "replace")).get("pid")
        except (OSError, ValueError):
            return False
        return _pid_alive(pid)
    if str(state.get("gateway_state") or "").lower() != "running":
        return False
    updated = str(state.get("updated_at") or "")
    if updated:
        try:
            stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - stamp).total_seconds() > 120:
                return False
        except ValueError:
            pass
    return _pid_alive(state.get("pid"))


def dispatcher_ok(cfg) -> bool:
    """Запущен ли диспетчер (engine_read.dispatcher_status, если он уже есть)."""
    reader = None
    try:  # чужой модуль может отсутствовать — тогда своя минимальная проверка
        from . import engine_read as reader  # type: ignore
    except Exception:
        try:
            import engine_read as reader  # type: ignore
        except Exception:
            reader = None
    if reader is not None and hasattr(reader, "dispatcher_status"):
        try:
            status = reader.dispatcher_status(cfg) or {}
            state = status.get("state") or ("running" if status.get("ok") else "")
            return state == "running" and not status.get("warning")
        except Exception:
            pass
    return _gateway_alive(cfg)


def _dispatcher_warning(cfg) -> dict | None:
    if dispatcher_ok(cfg):
        return None
    return {"code": "dispatcher_down",
            "message": "Запрос создан, но диспетчер не запущен — работа не начнётся сама."}


# ---------------------------------------------------------------- профили

def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _yaml_scalar(text: str, section: str | None, key: str) -> str:
    """Достать `key: value` из секции (2 уровня, как model.default) без PyYAML."""
    in_section = section is None
    for raw in (text or "").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            in_section = (section is None) or (line == f"{section}:")
            if section is None and line.startswith(f"{key}:"):
                return _unquote(line.split(":", 1)[1])
            continue
        if in_section and indent == 2 and line.startswith(f"{key}:"):
            return _unquote(line.split(":", 1)[1])
        if in_section and indent == 2 and not line.startswith(f"{key}:"):
            continue
    return ""


def list_profiles(cfg) -> list[dict]:
    """Ростер профилей из файлов (без subprocess): name/description/model/provider/is_default."""
    root = Path(cfg.hermes_home)
    dirs = [root]
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        dirs += sorted(d for d in profiles_dir.iterdir() if d.is_dir())
    out: list[dict] = []
    for folder in dirs:
        config_file = folder / "config.yaml"
        if not config_file.exists():
            continue
        name = "default" if folder == root else folder.name
        try:
            text = config_file.read_text("utf-8", "replace")
        except OSError:
            continue
        description = ""
        profile_yaml = folder / "profile.yaml"
        if profile_yaml.exists():
            try:
                description = _yaml_scalar(profile_yaml.read_text("utf-8", "replace"), None, "description")
            except OSError:
                description = ""
        out.append({
            "name": name,
            "description": description,
            "model": _yaml_scalar(text, "model", "default"),
            "provider": _yaml_scalar(text, "model", "provider"),
            "is_default": folder == root,
        })
    out.sort(key=lambda item: (not item["is_default"], item["name"].lower()))
    return out


# ---------------------------------------------------------------- доска и запрос

def ensure_board(cfg, slug: str) -> bool:
    """True, если БД доски существует. Доски не создаём (это делает пользователь/CLI)."""
    if not slug:
        return False
    try:
        return _db_path(cfg, slug).exists()
    except (OSError, ValueError):
        return False


def with_context_pack(cfg, room: dict, body: str) -> str:
    """Тело карточки запроса с блоком «КОНТЕКСТ КОМНАТЫ» (WAVE 8, Блок D).

    Блок собирает `server/context_pack.py`: комната и её описание, выжимка
    проекта, файлы контекста, похожие прошлые запросы, правило «Obsidian —
    источник правды» и правило папки проекта. Вставка идёт после текста Михаила
    и до правил контракта, в интерфейс не попадает (`contract.visible_text`).
    Любая ошибка сборки не должна мешать создать запрос — тогда тело как есть.
    """
    try:
        try:
            from . import context_pack
        except ImportError:  # pragma: no cover - плоский импорт (python server/app.py)
            import context_pack  # type: ignore
        text = ""
        try:
            try:
                from . import contract as contract_mod
            except ImportError:  # pragma: no cover
                import contract as contract_mod  # type: ignore
            text = contract_mod.visible_text(body)
        except Exception:  # noqa: BLE001 - без текста блок всё равно полезен
            text = ""
        block = context_pack.build_context_block(cfg, room or {}, text)
        return context_pack.insert_into_body(body, block)
    except Exception:  # noqa: BLE001 - блок контекста не должен ломать создание запроса
        return body


def brief_body(cfg, room: dict, body: str) -> str:
    """Тело в режиме БРИФА (A5): инструкции собирает `contract.build_brief_body` (лениво).

    Модуль контракта пишет сосед (PLAN-CONTRACT); нет функции или она упала —
    тело остаётся как есть, запрос всё равно создаётся.
    """
    try:
        try:
            from . import contract as contract_mod
        except ImportError:  # pragma: no cover - плоский импорт
            import contract as contract_mod  # type: ignore
        build = getattr(contract_mod, "build_brief_body", None)
        if callable(build):
            return build(cfg, room, body)
    except Exception:  # noqa: BLE001 - бриф-режим не должен ломать создание запроса
        pass
    return body


def _model_args(cfg, room: dict, profile: str) -> tuple[list, dict]:
    """`--model/--provider` для новой карточки и настройки участника (WAVE 10).

    Модель комнаты берётся у участника, которому карточка достанется: у корня запроса —
    руководитель комнаты (или явно переданный профиль направления). Файлы профилей не
    меняются: всё это только поля карточки.
    """
    name = str(profile or "").strip()
    if not name:
        return [], {}
    try:
        try:
            from . import agent_models as models_mod
        except ImportError:  # pragma: no cover - плоский импорт
            import agent_models as models_mod  # type: ignore
        settings = models_mod.card_settings(cfg, room, name)
    except Exception:  # noqa: BLE001 - выбор модели не должен мешать создать карточку
        return [], {}
    args: list = []
    if settings.get("model"):
        args.append(opt("model", settings["model"]))
        if settings.get("provider"):
            args.append(opt("provider", settings["provider"]))
    return args, settings


def create_request(cfg, room: dict, slug: str, title: str, body: str, *,
                   brief: bool = False, idem_key: str | None = None,
                   model_profile: str | None = None) -> dict:
    """Создать корневую карточку запроса (triage) с телом и вернуть {root_id, warning}.

    ``brief=True`` — тело в режиме брифа проекта (WAVE 9, A5); ``idem_key`` — свой ключ
    идемпотентности (у направлений карты ``plan:<room>:<stream_id>``). ``model_profile`` —
    чей выбор модели применить к карточке (по умолчанию руководитель комнаты; направления
    передают своего владельца).
    """
    if not isinstance(room, dict):
        raise EngineWriteError("bad_request", "Не передана комната запроса.")
    board = (room.get("board") or "").strip()
    if not board:
        raise EngineWriteError("bad_request", "У комнаты не указана доска канбана.")
    if not (slug or "").strip():
        raise EngineWriteError("bad_request", "Не указан слаг запроса.")
    tenant = (room.get("tenant") or f"{TENANT_PREFIX}{room.get('id') or ''}").strip()
    body = body if isinstance(body, str) else str(body or "")
    title = _title(title, body)
    if "\x00" in title or "\x00" in body:
        # D12: NUL доходит до subprocess как ValueError — отказываем до записи
        raise EngineWriteError("bad_request", "В заголовке или теле карточки есть NUL-байт.",
                               detail={"field": "title" if "\x00" in title else "body"})
    body = with_context_pack(cfg, room, brief_body(cfg, room, body) if brief else body)   # WAVE 8/9
    model_args, settings = _model_args(cfg, room, model_profile or room.get("orchestrator") or "")
    path = _write_body(cfg, body, slug)
    try:
        args = ["create", title, "--triage", opt("tenant", tenant), opt("created-by", CREATED_BY),
                *model_args,
                opt("idempotency-key", str(idem_key or slug)), opt("body-file", path), "--json"]
        data, _ms = _run_json(cfg, board, args)
    finally:
        try:
            path.unlink()
        except Exception:  # noqa: BLE001 - уборка временного тела не должна ронять запись
            pass
    root_id = str((data or {}).get("id") or "")
    if not root_id:
        raise EngineWriteError("engine_error", "Канбан не вернул id созданной карточки.",
                               json.dumps(data, ensure_ascii=False)[:600])
    warning = _dispatcher_warning(cfg)
    # Уровень мышления: в CLI `create` флага нет — ставим штатной функцией движка через мост.
    if settings.get("reasoning"):
        try:
            try:
                from . import agent_models as models_mod
            except ImportError:  # pragma: no cover
                import agent_models as models_mod  # type: ignore
            _done, failed = models_mod.push_effort(cfg, board, [root_id], settings["reasoning"])
        except Exception as exc:  # noqa: BLE001 - карточка уже есть, уровень мышления вторичен
            failed = [{"id": root_id, "reason": str(exc)[:160]}]
        if failed and warning is None:
            warning = {"code": "reasoning_not_set",
                       "message": "Запрос создан, но уровень мышления не применился. "
                                  "Проверьте выбор модели в комнате."}
    return {"root_id": root_id, "warning": warning}


def create_child_card(cfg, room: dict, board: str, parent_id: str, assignee: str,
                      title: str, body: str, kind: str, idem_key: str | None = None) -> str:
    """Создать карточку-ребёнка (критик/доработка/подзадача) и вернуть её id."""
    if not (assignee or "").strip():
        raise EngineWriteError("bad_request", "Не указан исполнитель новой карточки.")
    if not (parent_id or "").strip():
        raise EngineWriteError("bad_request", "Не указана родительская карточка.")
    assignee = assert_profile(cfg, assignee, allow_unassign=False, field="assignee")
    parent_id = assert_task_id(parent_id, "parent_id")
    tenant = (room.get("tenant") or f"{TENANT_PREFIX}{room.get('id') or ''}").strip()
    body = body if isinstance(body, str) else str(body or "")
    key = idem_key or f"tr-{kind}-{parent_id}-{hashlib.sha1(body.encode('utf-8')).hexdigest()[:10]}"
    path = _write_body(cfg, body, f"{kind}-{parent_id}")
    try:
        args = ["create", _title(title, body), opt("assignee", assignee), opt("parent", parent_id),
                opt("tenant", tenant), opt("created-by", CREATED_BY),
                opt("idempotency-key", key), opt("body-file", path), "--json"]
        data, _ms = _run_json(cfg, board, args)
    finally:
        try:
            path.unlink()
        except Exception:  # noqa: BLE001 - уборка временного тела не должна ронять запись
            pass
    card_id = str((data or {}).get("id") or "")
    if not card_id:
        raise EngineWriteError("engine_error", "Канбан не вернул id созданной карточки.",
                               json.dumps(data, ensure_ascii=False)[:600])
    return card_id


def retarget_root(cfg, board: str, root_id: str, profile: str) -> dict:
    """Переназначить корень комнаты на директора комнаты (assign, для running — reassign)."""
    if not (profile or "").strip():
        raise EngineWriteError("bad_request", "Не указан директор комнаты.")
    root_id = assert_task_id(root_id, "root_id")
    profile = assert_profile(cfg, profile, allow_unassign=False)
    row = _task_row(cfg, board, root_id) or {}
    status = str(row.get("status") or "")
    if status in ("done", "archived"):
        return {"ok": True, "detail": "Карточка уже завершена — переназначение не требуется.",
                "cmd_ms": 0, "changed": False}
    if status == "running":
        _out, ms = _run(cfg, board, ["reassign", root_id, profile, "--reclaim",
                                     opt("reason", "директор комнаты")], timeout=DEFAULT_TIMEOUT)
    else:
        _out, ms = _run(cfg, board, ["assign", *positional(root_id, profile)])
    return {"ok": True, "detail": f"Корень {root_id} назначен на {profile}.",
            "cmd_ms": ms, "changed": True}


# ---------------------------------------------------------------- действия

def _parse_json_soft(text: str) -> dict:
    try:
        value = json.loads(text, strict=False)
        return value if isinstance(value, dict) else {"value": value}
    except (json.JSONDecodeError, ValueError):
        return {}


def act(cfg, board: str, root_id: str, action: str, **kw) -> dict:
    """Действие над запросом. Возвращает {ok, detail, cmd_ms, ...} или EngineWriteError."""
    action = str(action or "").strip()
    if not board:
        raise EngineWriteError("bad_request", "Не указана доска канбана.")
    if action not in _ACTIONS:
        raise EngineWriteError("bad_request", f"Неизвестное действие: {action or '(пусто)'}.")
    started = time.perf_counter()

    def done(detail: str, ms: int, **extra) -> dict:
        payload = {"ok": True, "detail": detail, "cmd_ms": int(ms)}
        payload.update(extra)
        return payload

    if action == "dispatch":
        res = dispatch_board(cfg, board, max_cards=kw.get("max"), confirm=bool(kw.get("confirm")))
        return done(res.get("detail") or "Диспетчер: проход выполнен.", res.get("cmd_ms") or 0,
                    result=res.get("result") or {}, spawned=res.get("spawned") or 0,
                    promoted=res.get("promoted") or 0, reclaimed=res.get("reclaimed") or 0,
                    planned=res.get("planned") or [], max=res.get("max"))

    if action == "comment":
        text = _validate_text(kw.get("text"), "текст комментария")
        task_id = assert_owned(cfg, board, root_id, kw.get("task_id") or root_id)
        author = assert_author(cfg, kw.get("author") or AUTHOR_MAXIM)
        _out, ms = _run(cfg, board, ["comment", task_id, opt("author", author), "--", text])
        return done("Комментарий добавлен.", ms, task_id=task_id)

    if action == "answer":
        text = _validate_text(kw.get("text"), "текст ответа")
        task_id = str(kw.get("task_id") or "").strip()
        question_id = str(kw.get("question_id") or "").strip()
        blocked = [tid for tid in tree_ids(cfg, board, root_id)
                   if (_task_row(cfg, board, tid) or {}).get("status") in ("blocked", "scheduled")]
        if task_id:
            task_id = assert_owned(cfg, board, root_id, task_id)
        elif question_id:
            # id вопроса бывает `q_1` (не карточка) — тогда отвечаем корню и снимаем
            # блокировку со всех заблокированных карточек запроса; `t_…` — сама карточка
            task_id = (assert_owned(cfg, board, root_id, question_id)
                       if TASK_ID_RE.fullmatch(question_id) else root_id)
        task_id = task_id or root_id
        _out, ms = _run(cfg, board, ["comment", task_id, opt("author", AUTHOR_MAXIM), "--", text])
        targets = [task_id] if task_id in blocked else []
        targets += [tid for tid in blocked if tid not in targets]
        reason = "Ответ Михаила: " + " ".join(text.split())[:120]
        warning = None
        if targets:
            try:
                _out2, ms2 = _run(cfg, board, ["unblock", *targets, opt("reason", reason)])
                ms += ms2
            except EngineWriteError as exc:
                if exc.code != "conflict":
                    raise
                warning = "Карточки уже не были заблокированы — ответ сохранён комментарием."
                targets = []
        elif kw.get("task_id"):
            warning = "Карточка не была заблокирована — ответ сохранён комментарием."
        else:
            warning = "В запросе нет заблокированных карточек — ответ сохранён комментарием."
        return done("Ответ отправлен" + ("." if not warning else " (без разблокировки)."),
                    ms, unblocked=targets, warning=warning, task_id=task_id)

    if action == "unblock":
        text = kw.get("text")
        reason = " ".join(str(text).split())[:200] if text else "Разблокировано из комнаты команды"
        if kw.get("task_id"):
            ids = [assert_owned(cfg, board, root_id, kw["task_id"])]
        else:
            ids = [tid for tid in tree_ids(cfg, board, root_id)
                   if (_task_row(cfg, board, tid) or {}).get("status") in ("blocked", "scheduled")]
        if not ids:
            raise EngineWriteError("conflict", "Нечего разблокировать: в запросе нет заблокированных карточек.")
        _out, ms = _run(cfg, board, ["unblock", *ids, opt("reason", reason)])
        return done(f"Разблокировано: {len(ids)}.", ms, unblocked=ids)

    if action == "reassign":
        task_id = assert_owned(cfg, board, root_id, kw.get("task_id"))
        profile = assert_profile(cfg, kw.get("profile"))
        row = _task_row(cfg, board, task_id) or {}
        if row.get("status") == "running":
            _out, ms = _run(cfg, board, ["reassign", task_id, profile, "--reclaim",
                                         opt("reason", "переназначено в комнате команды")])
        else:
            _out, ms = _run(cfg, board, ["assign", *positional(task_id, profile)])
        return done(f"Карточка {task_id} назначена на {profile}.", ms, task_id=task_id, profile=profile)

    if action == "rework":
        text = _validate_text(kw.get("text"), "текст доработки")
        task_id = assert_owned(cfg, board, root_id, kw.get("task_id") or root_id)
        chosen = kw.get("profile")
        chosen = assert_profile(cfg, chosen) if chosen not in (None, "") else ""
        row = _task_row(cfg, board, task_id) or {}
        status = str(row.get("status") or "")
        if status in ("done", "archived"):
            raise EngineWriteError("conflict", "Карточка уже завершена — доработка создаётся новой "
                                               "карточкой (кнопка «Доработать» у ответа).")
        total = 0
        steps: list[str] = []
        if status == "review":
            _out, ms = _run(cfg, board, ["reopen-review", task_id, opt("reason", text)])
            total += ms
            steps.append("возвращена из проверки")
        else:
            if status in ("blocked", "scheduled"):
                _out, ms = _run(cfg, board, ["unblock", task_id,
                                             opt("reason", "на доработку: " + " ".join(text.split())[:160])])
                total += ms
                steps.append("снята блокировка")
            target = str(chosen or row.get("assignee") or "").strip()
            if target:
                _out, ms = _run(cfg, board, ["assign", *positional(task_id, target)])
                total += ms
                steps.append(f"назначена на {target}")
            if not steps:
                raise EngineWriteError("conflict", "Не удалось вернуть карточку в работу.")
        return done("На доработку: " + ", ".join(steps) + ".", total, task_id=task_id)

    if action == "retry":
        task_id = assert_owned(cfg, board, root_id, kw.get("task_id") or root_id)
        row = _task_row(cfg, board, task_id) or {}
        status = str(row.get("status") or "")
        if status == "todo":
            _out, ms = _run(cfg, board, ["promote", task_id, opt("reason", "повторный запуск")])
        elif status in ("blocked", "scheduled"):
            _out, ms = _run(cfg, board, ["unblock", task_id, opt("reason", "повторный запуск")])
        elif status == "review":
            _out, ms = _run(cfg, board, ["reopen-review", task_id, opt("reason", "повторный запуск")])
        elif status == "ready":
            return done("Карточка уже в очереди — диспетчер подхватит её сам.", 0, task_id=task_id)
        elif status == "running":
            raise EngineWriteError("conflict", "Карточка уже выполняется.")
        else:
            raise EngineWriteError("conflict", "Карточку нельзя перезапустить в текущем состоянии.")
        return done(f"Перезапуск: карточка {task_id} возвращена в очередь.", ms, task_id=task_id)

    if action == "archive":
        if kw.get("task_id"):
            ids = [assert_owned(cfg, board, root_id, kw["task_id"])]
        else:
            ids = sorted(tree_ids(cfg, board, root_id))
        ids = [tid for tid in ids if (_task_row(cfg, board, tid) or {}).get("status") != "archived"]
        if not ids:
            return done("Все карточки уже в архиве.", 0, archived=[])
        _out, ms = _run(cfg, board, ["archive", *positional(*ids)])
        return done(f"В архив отправлено: {len(ids)}.", ms, archived=ids)

    if action == "decompose":
        task_id = assert_owned(cfg, board, root_id, kw.get("task_id") or root_id)
        row = _task_row(cfg, board, task_id) or {}
        status = str(row.get("status") or "")
        if status not in ("triage", "queued"):
            raise EngineWriteError("conflict",
                                   "Запрос уже разложен или не ждёт декомпозиции (статус %s)." % status,
                                   detail={"reason": "already_decomposed", "status": status,
                                           "task_id": task_id}, http_status=409)
        out, ms = _run(cfg, board, ["decompose", *positional(task_id)], timeout=DECOMPOSE_TIMEOUT)
        first = next((line.strip() for line in (out or "").splitlines() if line.strip()), "")
        return done(first or "Декомпозиция запущена.", ms, task_id=task_id, output=_tail(out, 4000))

    # create_card — внутреннее действие (критик/доработка) через create_child_card
    room = kw.get("room")
    if not isinstance(room, dict):
        raise EngineWriteError("bad_request", "Для создания карточки нужна комната.")
    kind = str(kw.get("kind") or "card")
    card_id = create_child_card(
        cfg, room, board, str(kw.get("parent_id") or root_id), str(kw.get("assignee") or ""),
        str(kw.get("title") or "Новая карточка"), kw.get("body") or "", kind,
        idem_key=kw.get("idem_key"))
    ms = int((time.perf_counter() - started) * 1000)
    return done(f"Создана карточка {card_id} ({kind}).", ms, task_id=card_id)
