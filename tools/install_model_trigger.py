#!/usr/bin/env python
"""Установка/снятие защиты выбора модели на доске (W29-P10). ТОЛЬКО по явной команде.

Зачем: выбор модели комнаты живёт в реестре Team Room, который Hermes не читает; пин
«догоняет» карточку из Python и проигрывает гонку диспетчеру. Триггер SQLite ставит пин в
той же транзакции, что и создание карточки — см. docs/MODEL-TRIGGER.md.

    python tools/install_model_trigger.py status    --db <путь к kanban.db> [--json]
    python tools/install_model_trigger.py install   --db <путь> [--tenant room:cafe]
                                                    [--models models.json] [--no-backup]
    python tools/install_model_trigger.py uninstall --db <путь> [--purge]
    python tools/install_model_trigger.py sync      --db <путь> --tenant room:cafe
                                                    [--models models.json]

``--models`` — JSON «профиль → {provider, model, reasoning}» (что выбрано в комнате).
``status`` ничего не пишет. ``install`` сначала делает резервную копию доски (штатным
``sqlite3`` backup), проверяет схему `tasks`, затем создаёт служебную таблицу и триггеры.
Код возврата: 0 — успех, 1 — ошибка, 2 — неверные аргументы.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import model_trigger as trigger      # noqa: E402


def _rows(path: str | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("файл моделей должен быть объектом «профиль → настройки»")
    return data


def _print(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    text = payload.get("text") or payload.get("message") or ""
    print(text)
    for line in payload.get("problems") or []:
        print("  ! " + str(line))
    for line in payload.get("detail_lines") or []:
        print("  · " + str(line))
    if payload.get("backup"):
        print("  резервная копия: " + str(payload["backup"]))
    if payload.get("rows_written"):
        print("  записей в таблице: %s" % payload["rows_written"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Защита выбора модели на доске (W29-P10)")
    parser.add_argument("action", choices=("status", "install", "uninstall", "sync"))
    parser.add_argument("--db", required=True, help="путь к kanban.db доски")
    parser.add_argument("--tenant", default="", help="тенант комнаты, например room:cafe")
    parser.add_argument("--models", default="", help="JSON: профиль → {provider, model, reasoning}")
    parser.add_argument("--no-backup", action="store_true", help="без резервной копии (не советуем)")
    parser.add_argument("--purge", action="store_true", help="при удалении стереть и таблицу")
    parser.add_argument("--json", action="store_true", help="вывод целиком в JSON")
    args = parser.parse_args(argv)

    try:
        if args.action == "status":
            payload = trigger.status(args.db)
            _print(payload, args.json)
            if args.json:
                return 0
            for name, present in (payload.get("triggers") or {}).items():
                print("  %s %s" % ("✓" if present else "×", name))
            if payload.get("rows"):
                print("  участников в таблице: %d" % int(payload["rows"]))
            if payload.get("schema", {}).get("ok") is False:
                print("  схема доски: " + str(payload.get("schema", {}).get("error") or "не та"))
            return 0
        if args.action == "install":
            payload = trigger.install(args.db, rows=_rows(args.models), tenant=args.tenant,
                                      backup=not args.no_backup)
            payload["detail_lines"] = list(payload.get("created") or [])
            _print(payload, args.json)
            return 0 if payload.get("healthy") else 1
        if args.action == "uninstall":
            payload = trigger.uninstall(args.db, drop_table=args.purge)
            payload["text"] = ("Защита снята" + (" вместе с таблицей" if args.purge else "")
                               + ". Пины на карточках остались.")
            payload["detail_lines"] = list(payload.get("dropped") or [])
            _print(payload, args.json)
            return 0
        payload = trigger.sync(args.db, args.tenant, _rows(args.models))
        payload["text"] = ("Таблица выбора модели обновлена: записей %d"
                           % int(payload.get("synced") or 0))
        payload["detail_lines"] = (["пропуск: " + str(payload["skipped"])]
                                   if payload.get("skipped") else [])
        _print(payload, args.json)
        return 0 if not payload.get("skipped") else 1
    except trigger.TriggerError as exc:
        print("Не удалось: " + exc.message, file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print("Неверные аргументы: %s" % str(exc)[:200], file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
