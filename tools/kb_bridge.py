#!/usr/bin/env python
"""Мост к штатному коду Hermes: каталог моделей и пин модели/мышления на карточку.

Это ЕДИНСТВЕННОЕ место приложения, которое импортирует `hermes_cli`. Скрипт запускается
интерпретатором Hermes (в нём есть hermes_cli и его зависимости) и делает только две вещи:

1. ``--catalog`` — печатает JSON каталога выбора модели: тот же список, что в Hermes
   Desktop («Поиск моделей») — ``hermes_cli.inventory.build_models_payload`` с
   ``non_blocking_catalogs=True`` и без проб custom-провайдеров. **Сеть не используется**:
   только дисковый кэш каталогов и встроенные списки. Ключи/токены не читаются и не печатаются.
2. ``--set`` — ставит карточке модель/провайдера и уровень мышления штатными функциями
   ``hermes_cli.kanban_db.set_model_override`` / ``set_reasoning_effort``. Своего SQL нет:
   обе функции сами пишут события ``model_override_set`` / ``reasoning_effort_set``.

Почему мост: в CLI ``hermes kanban`` нет флага для `reasoning_effort`, а прямых запросов в
`kanban.db` приложение не делает никогда (docs/ENGINE-FACTS.md, WAVE 7 §1).

Песочница: адрес доски приходит флагом ``--db`` (в тестах — временная БД из
``tests.helpers.Sandbox``), поэтому реальная доска не меняется. Переменные окружения
агентской сессии (``HERMES_KANBAN_*``, ``HERMES_DELEGATED_CHILD_CONTEXT``) вычищаются ДО
импорта hermes_cli, чтобы «дочерний» контекст не запретил запись.

    python tools/kb_bridge.py --catalog
    python tools/kb_bridge.py --db <kanban.db> --set --task t_1 [--task t_2] \
        [--model M --provider P | --clear-model] [--effort high | --clear-effort]

Ответ — один JSON в stdout, коды возврата: 0 — всё сделано, 2 — ошибка разбора/аргументов,
3 — часть карточек не обновлена (подробности в ``errors``), 1 — непредвиденная ошибка.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: переменные агентской сессии: доска/задача родителя не должны влиять на наш вызов
CLEAN_ENV = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK",
             "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT",
             "HERMES_KANBAN_ATTACHMENTS_ROOT")
CATALOG_TIMEOUT_NOTE = "каталог из кэша Hermes, без сетевых запросов"


def _clean_env() -> None:
    for key in CLEAN_ENV:
        os.environ.pop(key, None)


def _agent_paths(agent_home: str | None) -> list[str]:
    """Где искать пакет ``hermes_cli`` (по порядку)."""
    roots: list[Path] = []
    for value in (os.environ.get("HERMES_AGENT_PATH"), os.environ.get("HERMES_AGENT_HOME")):
        if value:
            roots.append(Path(value))
    for base in (agent_home, os.environ.get("HERMES_HOME")):
        if base:
            roots.append(Path(base) / "hermes-agent")
    here = Path(__file__).resolve().parent.parent
    roots += [here / "hermes-agent", here.parent / "hermes" / "hermes-agent"]
    out: list[str] = []
    for root in roots:
        try:
            text = str(root)
        except (TypeError, ValueError):
            continue
        if text and (root / "hermes_cli").is_dir() and text not in out:
            out.append(text)
    return out


def _import_hermes(agent_home: str | None):
    """Импорт ``hermes_cli`` (после этого работает и kanban_db)."""
    try:
        import hermes_cli  # noqa: F401
        return
    except ImportError:
        pass
    for path in _agent_paths(agent_home):
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import hermes_cli  # noqa: F401
            return
        except ImportError:
            continue
    raise SystemExit(json.dumps({"ok": False, "code": "engine_error",
                                 "error": "hermes_cli не найден — мост к Hermes недоступен",
                                 "paths": _agent_paths(agent_home)}, ensure_ascii=False))


# ---------------------------------------------------------------- каталог

def catalog_payload() -> dict:
    """Каталог выбора модели: провайдеры с рабочим ключом и их модели (без сети)."""
    from hermes_cli.inventory import build_models_payload, load_picker_context

    ctx = load_picker_context()
    payload = build_models_payload(
        ctx, for_picker=True, picker_hints=False, canonical_order=True,
        non_blocking_catalogs=True, probe_custom_providers=False,
        probe_current_custom_provider=False, include_unconfigured=False,
        explicit_only=False, capabilities=False, featured=False, pricing=False,
    )
    providers = []
    for row in payload.get("providers") or []:
        slug = str(row.get("slug") or "").strip()
        if not slug or slug == "moa":            # виртуальный провайдер — не модель для карточки
            continue
        models = []
        seen = set()
        for item in row.get("models") or []:
            name = str(item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            models.append({"id": name})
        providers.append({"id": slug, "label": str(row.get("name") or slug),
                          "configured": True, "models": models})
    return {"ok": True, "providers": providers, "source": "hermes:inventory",
            "note": CATALOG_TIMEOUT_NOTE,
            "current": {"provider": payload.get("provider") or "", "model": payload.get("model") or ""}}


# ---------------------------------------------------------------- пин карточки

def _set_tasks(db_path: str, tasks: list[str], model: str | None, provider: str | None,
               effort: str | None, clear_model: bool, clear_effort: bool) -> dict:
    from hermes_cli.kanban_db import set_model_override, set_reasoning_effort
    from hermes_cli.kanban_db_connect import connect_closing

    result: dict = {"ok": True, "tasks": {}, "errors": {}}
    path = Path(db_path)
    if not path.exists():
        raise SystemExit(json.dumps({"ok": False, "code": "not_found",
                                     "error": "База канбана не найдена: %s" % path},
                                    ensure_ascii=False))
    with connect_closing(path) as conn:
        for task_id in tasks:
            item: dict = {}
            try:
                if clear_model:
                    item["changed_model"] = bool(set_model_override(conn, task_id, None))
                    item["model"], item["provider"] = None, None
                elif model:
                    item["changed_model"] = bool(set_model_override(conn, task_id, model,
                                                                    provider=provider or None))
                    item["model"], item["provider"] = model, provider or None
                if clear_effort:
                    item["changed_effort"] = bool(set_reasoning_effort(conn, task_id, None))
                    item["effort"] = None
                elif effort is not None:
                    item["changed_effort"] = bool(set_reasoning_effort(conn, task_id, effort))
                    item["effort"] = effort
            except ValueError as exc:            # кривой уровень мышления/модель
                result["errors"][task_id] = str(exc)[:300]
                result["ok"] = False
                continue
            except RuntimeError as exc:          # архивная карточка и подобное
                result["errors"][task_id] = str(exc)[:300]
                result["ok"] = False
                continue
            result["tasks"][task_id] = item
    return result


# ---------------------------------------------------------------- CLI

def _parse(argv: list[str]) -> dict:
    opts = {"tasks": [], "model": None, "provider": None, "effort": None,
            "db": "", "agent_home": "", "catalog": False, "set": False,
            "clear_model": False, "clear_effort": False}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--catalog":
            opts["catalog"] = True
        elif token == "--set":
            opts["set"] = True
        elif token == "--clear-model":
            opts["clear_model"] = True
        elif token == "--clear-effort":
            opts["clear_effort"] = True
        elif token in ("--task", "--model", "--provider", "--effort", "--db", "--agent-home"):
            if i + 1 >= len(argv):
                raise SystemExit(json.dumps({"ok": False, "code": "bad_request",
                                             "error": "У флага %s нет значения" % token},
                                            ensure_ascii=False))
            value = argv[i + 1]
            key = token[2:].replace("-", "_")
            if key == "task":
                opts["tasks"].append(str(value))
            elif key == "agent_home":
                opts["agent_home"] = str(value)
            else:
                opts[key] = str(value)
            i += 1
        else:
            raise SystemExit(json.dumps({"ok": False, "code": "bad_request",
                                         "error": "Неизвестный аргумент: %s" % token},
                                        ensure_ascii=False))
        i += 1
    return opts


def main(argv: list[str]) -> int:
    opts = _parse(argv)
    _clean_env()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _import_hermes(opts["agent_home"] or None)
    if opts["catalog"]:
        print(json.dumps(catalog_payload(), ensure_ascii=False))
        return 0
    if not opts["set"]:
        print(json.dumps({"ok": False, "code": "bad_request",
                          "error": "Нужен режим --catalog или --set"}, ensure_ascii=False))
        return 2
    if not opts["db"] or not opts["tasks"]:
        print(json.dumps({"ok": False, "code": "bad_request",
                          "error": "Нужны --db и хотя бы один --task"}, ensure_ascii=False))
        return 2
    res = _set_tasks(opts["db"], opts["tasks"], opts["model"], opts["provider"],
                     opts["effort"], opts["clear_model"], opts["clear_effort"])
    print(json.dumps(res, ensure_ascii=False))
    if res["errors"]:
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - мост обязан вернуть JSON, а не трассировку
        print(json.dumps({"ok": False, "code": "engine_error",
                          "error": "%s: %s" % (exc.__class__.__name__, str(exc)[:300])},
                         ensure_ascii=False))
        sys.exit(1)
