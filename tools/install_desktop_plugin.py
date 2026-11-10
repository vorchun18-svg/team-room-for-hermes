#!/usr/bin/env python3
"""Установка плагина «Командные комнаты» в Hermes Desktop.

Кладёт `desktop-plugin/plugin.js` в <hermes home>/desktop-plugins/team-room/.
Приложение следит за этой папкой и подхватывает файл за несколько секунд —
перезапуск не нужен.

Копирование атомарное и только при отличии хэшей: повторный запуск ничего
не трогает, а половина файла на диск не попадает.

Примеры:
    python tools/install_desktop_plugin.py
    python tools/install_desktop_plugin.py --dry-run
    python tools/install_desktop_plugin.py --also-profiles
    python tools/install_desktop_plugin.py --uninstall

Только stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

PLUGIN_ID = "team-room"
PLUGIN_NAME = "Командные комнаты"
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SOURCE = PROJECT_ROOT / "desktop-plugin" / "plugin.js"

BASE_URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")
DEFAULT_PORT = 8777


def config_port(data: bytes) -> int:
    """Порт, на который смотрит плагин (из строки BASE_URL)."""
    match = BASE_URL_RE.search(data.decode("utf-8", "replace"))
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return DEFAULT_PORT
    return DEFAULT_PORT


def set_port(data: bytes, port: int) -> bytes:
    """Подставить порт сервера в копию плагина (исходник не меняется)."""
    text = data.decode("utf-8")
    text = BASE_URL_RE.sub("http://127.0.0.1:%d/" % int(port), text, count=1)
    return text.encode("utf-8")


def default_hermes_home() -> Path:
    """Где живёт Hermes Desktop: HERMES_HOME, затем обычные места по системам.

    Windows — `%LOCALAPPDATA%\\hermes`, macOS — `~/Library/Application Support/hermes`,
    остальные — `~/.hermes`. Существующая папка важнее порядка: приложение не
    должно создавать плагин там, где Hermes не живёт.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "hermes")
    if sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "hermes")
    candidates.append(home / ".hermes")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plugin_targets(hermes_home: Path, also_profiles: bool) -> list[Path]:
    roots = [hermes_home]
    if also_profiles:
        profiles = hermes_home / "profiles"
        if profiles.is_dir():
            roots.extend(sorted(p for p in profiles.iterdir() if p.is_dir()))
    return [root / "desktop-plugins" / PLUGIN_ID / "plugin.js" for root in roots]


def atomic_copy(data: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp-" + str(os.getpid()))
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def install_one(data: bytes, target: Path, source_hash: str, dry_run: bool) -> str:
    if target.is_file():
        try:
            current = sha256_file(target)
        except OSError as exc:
            current = ""
            print("  не смог прочитать установленный файл (%s) — перезапишу" % exc)
        if current and current == source_hash:
            return "уже установлен и совпадает"
    action = "перезаписан" if target.is_file() else "установлен"
    if dry_run:
        return "будет " + action + " (пробный запуск, ничего не пишу)"
    try:
        atomic_copy(data, target)
    except OSError as exc:
        return "ОШИБКА записи: %s" % exc
    return action


def remove_one(target: Path, dry_run: bool) -> str:
    if not target.exists():
        return "не установлен — удалять нечего"
    if dry_run:
        return "будет удалён (пробный запуск, ничего не пишу)"
    try:
        target.unlink()
    except OSError as exc:
        return "ОШИБКА удаления: %s" % exc
    folder = target.parent
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
    return "удалён"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ставит плагин «%s» в Hermes Desktop" % PLUGIN_NAME
    )
    parser.add_argument(
        "--root",
        default=None,
        help="папка hermes (по умолчанию: $HERMES_HOME, затем LOCALAPPDATA/hermes, "
             "Application Support/hermes или ~/.hermes)",
    )
    parser.add_argument(
        "--also-profiles",
        action="store_true",
        help="копировать ещё и в profiles/*/desktop-plugins (по умолчанию выключено)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="порт локального сервера, который откроет плагин (по умолчанию 8777)",
    )
    parser.add_argument("--uninstall", action="store_true", help="удалить плагин")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    args = parser.parse_args(argv)

    hermes_home = Path(args.root).expanduser() if args.root else default_hermes_home()
    if not hermes_home.is_dir():
        print("Не нашёл папку Hermes: %s" % hermes_home)
        print("Укажите её явно: --root <папка hermes>, например --root ~/.hermes")
        return 2

    print("Плагин: «%s» (%s)" % (PLUGIN_NAME, PLUGIN_ID))
    print("Папка Hermes: %s" % hermes_home)

    targets = plugin_targets(hermes_home, args.also_profiles)

    if args.uninstall:
        problems = 0
        for target in targets:
            result = remove_one(target, args.dry_run)
            print("  %s — %s" % (target, result))
            if result.startswith("ОШИБКА"):
                problems += 1
        if not problems and not args.dry_run:
            print("Готово. Строка меню исчезнет после перезагрузки плагинов (Ctrl+K → Reload desktop plugins).")
        return 0 if not problems else 1

    if not SOURCE.is_file():
        print("Не нашёл исходник плагина: %s" % SOURCE)
        print("Похоже, папка проекта «Комната команды» повреждена или перенесена.")
        return 2

    data = SOURCE.read_bytes()
    if args.port and args.port != config_port(data):
        data = set_port(data, args.port)
        print("Порт сервера: %d (в копии плагина)" % args.port)
    source_hash = hashlib.sha256(data).hexdigest()
    print("Исходник: %s" % SOURCE)
    print("Хэш: %s" % source_hash[:16])

    problems = 0
    changed = 0
    for target in targets:
        result = install_one(data, target, source_hash, args.dry_run)
        print("  %s — %s" % (target, result))
        if result.startswith("ОШИБКА"):
            problems += 1
        elif result in ("установлен", "перезаписан"):
            changed += 1

    if problems:
        print("Не всё получилось — см. строки с ОШИБКА выше.")
        return 1

    if args.dry_run:
        print("Пробный запуск: ничего не записано.")
        return 0

    if changed:
        print("Готово. Приложение подхватит файл само за несколько секунд.")
        print("Если строки «Командные комнаты» в левом меню нет — Ctrl+K → Reload desktop plugins.")
    else:
        print("Всё уже на месте — ничего менять не пришлось.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
