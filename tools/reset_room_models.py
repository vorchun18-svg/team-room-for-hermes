# -*- coding: utf-8 -*-
"""Убрать у комнаты переопределения моделей — «Вернуть по умолчанию» оптом (W20 §1.5).

Зачем: модель участника по умолчанию берётся из настроек профиля Hermes
(`profiles/<p>/config.yaml`; для `default` — общий `config.yaml`). Переопределение
комнаты (`rooms.agent_models`) должно существовать ТОЛЬКО там, где пользователь сам
выбрал модель. После переезда данных или ручной правки в реестре остаются записи,
которых никто не выбирал: они перекрывают настройки профиля и путают интерфейс.

Как пользоваться (по умолчанию скрипт ТОЛЬКО ПОКАЗЫВАЕТ, ничего не меняя)::

    python tools/reset_room_models.py --room cafe
    python tools/reset_room_models.py --room cafe --apply

`--apply` сначала делает копию файла комнат (`rooms.json.bak-<дата-время>`) и только
потом убирает `agent_models` этой комнаты. Другие комнаты не трогаются.

Пути: `--data <папка>` (по умолчанию — `TEAMROOM_DATA` или `<приложение>/data`).
Файлы профилей Hermes не изменяются никогда: они читаются для подписи «по умолчанию».

    python tools/reset_room_models.py --room cafe --json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import agent_models as models_mod      # noqa: E402
from server import config as config_mod           # noqa: E402
from server import statefiles as sf               # noqa: E402

ROOMS_FILE = "rooms.json"


class ResetError(Exception):
    """Понятная ошибка для вывода в консоль (код возврата 2)."""


def rooms_path(cfg) -> Path:
    """Файл реестра комнат (как `server/rooms.py`: `<данные>/rooms.json`)."""
    return Path(cfg.data_dir) / ROOMS_FILE


def _cfg(data_dir: str | None = None):
    cfg = config_mod.load()
    if data_dir:
        cfg = dataclasses.replace(cfg, data_dir=Path(data_dir).expanduser().resolve())
    return cfg


def _read(path: Path) -> dict:
    value, info = sf.read_json_doc(path, None)
    state = str((info or {}).get("state") or "")
    if state == "missing":
        raise ResetError("Файл комнат не найден: %s" % path)
    if state in ("unreadable", "corrupt"):
        raise ResetError("Файл комнат не читается (%s): %s"
                         % (state, (info or {}).get("error") or path))
    if not isinstance(value, dict) or not isinstance(value.get("rooms"), list):
        raise ResetError("Файл комнат не в ожидаемом виде: %s" % path)
    return value


def _room(data: dict, room_id: str) -> dict:
    ident = str(room_id or "").strip()
    for room in data.get("rooms") or []:
        if isinstance(room, dict) and str(room.get("id")) == ident:
            return room
    raise ResetError("Комната «%s» не найдена в реестре." % ident)


def _labels(cfg) -> dict:
    """Подписи провайдеров из готового каталога (без запуска Hermes: каталог не трогаем)."""
    if not isinstance(getattr(cfg, "model_catalog_override", None), dict):
        return {}
    try:
        data = models_mod.catalog(cfg)
    except Exception:                      # noqa: BLE001 - подписи не обязательны
        return {}
    return {str(p.get("id")): str(p.get("label") or p.get("id"))
            for p in data.get("providers") or [] if isinstance(p, dict)}


def _label(value: dict, labels: dict | None = None) -> str:
    """«провайдер · модель · мышление» как есть (каталог Hermes не запускаем)."""
    names = labels or {}
    provider = names.get(str(value.get("provider") or "")) or (value.get("provider") or "")
    return models_mod.human(provider, value.get("model") or "",
                            value.get("reasoning") or "") or "модель не задана"


def describe(cfg, room: dict) -> dict:
    """Что уберём и что станет по умолчанию — без единой записи на диск."""
    stored = models_mod.settings_of(room)
    labels = _labels(cfg)
    rows = []
    for profile in sorted(stored):
        value = stored[profile]
        info = models_mod.default_info(cfg, profile)
        rows.append({"profile": profile,
                     "override": value,
                     "override_label": _label(value, labels),
                     "default": models_mod.agent_default(cfg, profile),
                     "default_label": _label(info, labels),
                     "default_source": info.get("source") or "none",
                     "note": info.get("note") or ""})
    return {"room": str(room.get("id") or ""), "title": str(room.get("title") or ""),
            "count": len(rows), "overrides": rows}


def plan(cfg, room_id: str) -> dict:
    """Показать, что будет убрано (диск только читаем)."""
    path = rooms_path(cfg)
    return dict(describe(cfg, _room(_read(path), room_id)), file=str(path), applied=False)


def apply(cfg, room_id: str) -> dict:
    """Убрать `agent_models` комнаты, предварительно скопировав `rooms.json`."""
    path = rooms_path(cfg)
    result = plan(cfg, room_id)
    if not result["count"]:
        result["applied"] = False
        result["backup"] = ""
        result["message"] = "У комнаты нет переопределений — менять нечего."
        return result
    if not path.exists():
        raise ResetError("Файл комнат не найден: %s" % path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name("%s.bak-%s" % (path.name, stamp))
    shutil.copy2(path, backup)              # копия ДО изменений — обязательна
    with sf.lock_for(path):
        data = _read(path)
        room = _room(data, room_id)
        room.pop("agent_models", None)      # «Вернуть по умолчанию» = записи нет
        sf.write_json_atomic(path, data)
    result["applied"] = True
    result["backup"] = str(backup)
    result["message"] = "Убрано переопределений: %d. Копия данных: %s" % (result["count"], backup.name)
    return result


def render(result: dict) -> str:
    """Текст для человека (русский, без техники)."""
    title = result.get("title") or result.get("room") or ""
    lines = []
    if not result.get("count"):
        lines.append("Комната «%s»: переопределений моделей нет — всё и так берётся "
                     "из настроек агентов." % title)
    else:
        lines.append("Комната «%s»: переопределений моделей — %d."
                     % (title, int(result.get("count") or 0)))
        for row in result.get("overrides") or []:
            tail = "по умолчанию (из настроек агента): %s" % row["default_label"]
            if row.get("default_source") == "global":
                tail += " — общая настройка Hermes"
            elif row.get("default_source") == "none":
                tail = "по умолчанию ничего не задано"
            lines.append("  %s: было «%s» → %s" % (row["profile"], row["override_label"], tail))
    if result.get("applied"):
        lines.append(str(result.get("message") or "Готово."))
    else:
        lines.append("Показ: ничего не изменено. Добавьте --apply, чтобы убрать "
                     "(файл комнат будет скопирован).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Убрать переопределения моделей комнаты (модель снова — из настроек агента)")
    ap.add_argument("--room", required=True, help="id комнаты, например cafe")
    ap.add_argument("--apply", action="store_true",
                    help="действительно убрать (сначала копия rooms.json)")
    ap.add_argument("--data", default=None, help="каталог данных приложения (иначе TEAMROOM_DATA)")
    ap.add_argument("--json", action="store_true", help="вывести результат в JSON")
    args = ap.parse_args(argv)
    try:
        cfg = _cfg(args.data)
        result = apply(cfg, args.room) if args.apply else plan(cfg, args.room)
    except ResetError as exc:
        print("Не получилось: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
