#!/usr/bin/env python3
"""Проверка модуля «Командные комнаты»: всё ли на месте и работает ли.

Человекочитаемый отчёт по-русски, без технических подробностей на первом плане:
  · плагин установлен и совпадает с исходником проекта;
  · сервер 127.0.0.1:8777 отвечает;
  · нет ли в журнале приложения ошибок загрузки плагина;
  · исправлен ли лимит диктовки (120 → 600 секунд);
  · есть ли задача-сторож в кроне;
  · какая сборка Hermes стоит.

Код возврата: 0 — всё в порядке, 1 — есть, что чинить.
Только stdlib, только чтение.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

PLUGIN_ID = "team-room"
BUNDLE_REL = "hermes-agent/apps/desktop/release/win-unpacked/resources/app.asar.unpacked/dist/assets"
CONFIG_DEFAULTS_REL = "hermes-agent/hermes_cli/config_defaults.py"
VOICE_MIXIN_REL = "hermes-agent/hermes_cli/cli_voice_mixin.py"
LOG_TAIL_BYTES = 400_000

OK = "✓"
BAD = "✗"
WARN = "·"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_hermes_home() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "hermes" if local else Path.home() / ".hermes"


def check_plugin(source: Path, target: Path) -> tuple[bool, str]:
    if not source.is_file():
        return False, "нет исходника плагина: %s" % source
    if not target.is_file():
        return False, "плагин не установлен (%s)" % target
    try:
        source_hash = sha256_file(source)
        target_hash = sha256_file(target)
    except OSError as exc:
        return False, "не читается файл плагина: %s" % exc
    version = ""
    try:
        match = re.search(r"PLUGIN_VERSION\s*=\s*['\"]([^'\"]+)['\"]", target.read_text(encoding="utf-8"))
        version = match.group(1) if match else ""
    except OSError:
        pass
    size = target.stat().st_size
    if source_hash == target_hash:
        return True, "установлен и совпадает с проектом (v%s, %d КБ)" % (version or "?", max(1, size // 1024))
    return False, "установленная копия отличается от проекта — сторож перезапишет"


def check_server(port: int) -> tuple[bool, str]:
    url = "http://127.0.0.1:%d/api/health" % port
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:
            payload = json.loads(response.read(65536).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return False, "не отвечает (%s) — сторож поднимет" % exc.__class__.__name__
    app = payload.get("app") if isinstance(payload, dict) else None
    if not isinstance(app, dict):
        return False, "на порту %d отвечает что-то другое" % port
    uptime = app.get("uptime_s")
    hours = ("%.1f ч" % (uptime / 3600.0)) if isinstance(uptime, (int, float)) else "?"
    return True, "отвечает http://127.0.0.1:%d (работает %s)" % (port, hours)


def check_log(hermes_home: Path) -> tuple[bool, str]:
    path = hermes_home / "logs" / "desktop.log"
    if not path.is_file():
        return True, "журнала приложения нет — проверить нечего"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return True, "журнал не читается: %s" % exc

    lines = text.splitlines()
    ours = [line for line in lines if PLUGIN_ID in line]
    failures = [
        line
        for line in ours
        if re.search(r"failed to load|error-boundary|TypeError|ReferenceError|SyntaxError", line)
    ]
    if failures:
        return False, "в журнале %d строк(и) об ошибке плагина — последняя: %s" % (
            len(failures),
            failures[-1].strip()[:160],
        )
    if not ours:
        return True, "упоминаний плагина в журнале нет (приложение ещё не загружало его)"
    return True, "ошибок плагина в журнале нет (строк про него: %d)" % len(ours)


def bundle_cap_state(hermes_home: Path) -> tuple[bool, str]:
    assets = hermes_home / BUNDLE_REL
    if not assets.is_dir():
        return True, "собранного приложения не нашёл — не проверял"
    files = sorted(assets.glob("index-*.js"))
    if not files:
        return True, "файлы приложения не найдены — не проверял"
    bad = []
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        found = len(re.findall(rb"maxRecordingSeconds:[A-Za-z_$]+=120", data))
        found += len(
            re.findall(
                rb"var \$?[A-Za-z_]+=120;function [A-Za-z_]+\(e\)\{return typeof e==`number`", data
            )
        )
        if found:
            bad.append(path.name)
    if bad:
        return False, "в приложении снова лимит 2 минуты (%s) — сторож исправит" % ", ".join(bad)
    return True, "лимит диктовки 10 минут на месте"


def python_defaults_state(hermes_home: Path) -> tuple[bool, str]:
    problems = []
    config_defaults = hermes_home / CONFIG_DEFAULTS_REL
    if config_defaults.is_file():
        try:
            text = config_defaults.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if re.search(r'"max_recording_seconds"\s*:\s*120\b', text):
            problems.append("config_defaults.py")
    mixin = hermes_home / VOICE_MIXIN_REL
    if mixin.is_file():
        try:
            text = mixin.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if re.search(r"else\s+120(\.0)?\b", text):
            problems.append("cli_voice_mixin.py")
    if problems:
        return False, "в настройках Hermes снова 120 секунд (%s)" % ", ".join(problems)
    return True, "в настройках Hermes 600 секунд"


def check_cron(hermes_home: Path) -> tuple[bool, str]:
    path = hermes_home / "cron" / "jobs.json"
    if not path.is_file():
        return False, "задач крона нет (%s)" % path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, "не читается список задач крона: %s" % exc
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return False, "непонятный формат задач крона"
    for job in jobs:
        if not isinstance(job, dict):
            continue
        haystack = " ".join(str(job.get(key) or "") for key in ("name", "script", "prompt"))
        if "team_room_guard" in haystack or "team-room-guard" in haystack:
            schedule = job.get("schedule_display") or job.get("schedule")
            if isinstance(schedule, dict):
                schedule = schedule.get("display") or schedule.get("expr") or "?"
            enabled = job.get("enabled", True)
            last = job.get("last_run_at") or "ещё не запускался"
            return True, "задача «%s» (%s), включена: %s, последний запуск: %s" % (
                job.get("name") or "team-room-guard",
                schedule,
                "да" if enabled else "НЕТ",
                last,
            )
    return False, "задачи-сторожа в кроне нет — её создаёт оркестратор (team_room_guard.py)"


def hermes_version(hermes_home: Path) -> str:
    stamp = hermes_home / "desktop-build-stamp.json"
    built = ""
    if stamp.is_file():
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
            built = str(data.get("builtAt") or "")
        except (OSError, ValueError):
            built = ""
    head = ""
    repo = hermes_home / "hermes-agent"
    if (repo / ".git").exists():
        try:
            head = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%h %cs"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            head = ""
    parts = [part for part in (head, ("сборка " + built[:16]) if built else "") if part]
    return " / ".join(parts) if parts else "не определил"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка модуля «Командные комнаты»")
    parser.add_argument("--hermes-home", default=None, help="папка hermes")
    parser.add_argument("--project-root", default=None, help="папка проекта «Комната команды»")
    parser.add_argument("--port", type=int, default=8777, help="порт сервера")
    args = parser.parse_args(argv)

    project_root = (
        Path(args.project_root).expanduser()
        if args.project_root
        else Path(__file__).resolve().parent.parent
    )
    hermes_home = Path(args.hermes_home).expanduser() if args.hermes_home else default_hermes_home()

    source = project_root / "desktop-plugin" / "plugin.js"
    target = hermes_home / "desktop-plugins" / PLUGIN_ID / "plugin.js"

    rows: list[tuple[bool, str, str]] = []

    def add(title: str, ok: bool, text: str) -> None:
        rows.append((ok, title, text))

    plugin_ok, plugin_text = check_plugin(source, target)
    add("Плагин", plugin_ok, plugin_text)

    server_ok, server_text = check_server(args.port)
    add("Сервер", server_ok, server_text)

    log_ok, log_text = check_log(hermes_home)
    add("Журнал приложения", log_ok, log_text)

    cap_ok, cap_text = bundle_cap_state(hermes_home)
    add("Лимит диктовки", cap_ok, cap_text)

    py_ok, py_text = python_defaults_state(hermes_home)
    add("Настройки Hermes", py_ok, py_text)

    cron_ok, cron_text = check_cron(hermes_home)
    add("Сторож в кроне", cron_ok, cron_text)

    print("Командные комнаты — проверка модуля")
    print("")
    width = max(len(title) for _, title, _ in rows)
    for ok, title, text in rows:
        mark = OK if ok else BAD
        print("  %s %s  %s" % (mark, title.ljust(width), text))
    print("")
    print("  %s Версия Hermes  %s" % (WARN, hermes_version(hermes_home)))
    print("")

    problems = [title for ok, title, _ in rows if not ok]
    if problems:
        print("Что делать: python tools/install_desktop_plugin.py — вернуть плагин;")
        print("python tools/team_room_guard.py — починить сразу; перезапуск Hermes — поднять сервер.")
        print("Не в порядке: %s." % ", ".join(problems))
        return 1

    print("Всё в порядке: модуль на месте, сервер отвечает, лимит диктовки исправлен.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
