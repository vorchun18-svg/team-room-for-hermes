#!/usr/bin/env python3
"""Сторож «Командных комнат» — без LLM, для крона Hermes (раз в 2 минуты).

Что делает по порядку:
  1. сервер  — GET http://127.0.0.1:<port>/api/health; если не отвечает и порт
               свободен, поднимает `server/app.py --port <port>` скрыто и
               отдельно от Hermes;
  2. плагин  — сверяет <hermes home>/desktop-plugins/team-room/plugin.js с
               исходником проекта и восстанавливает, если файла нет или он
               разошёлся;
  3. диктовка — после обновления Hermes в собранном бандле приложения снова
               оказывается лимит записи 120 секунд: находит и правит на 600
               (с бэкапом *.bak-voicecap), плюс два python-дефолта;
  4. состояние — data/state/guard.json, журнал data/logs/guard.log (≤1 МБ).

Правила вывода: stdout ПУСТ, когда всё в порядке (пустой вывод = крону нечего
отправлять). Одна короткая русская строка на каждое исправление или поломку.

Ничего не пишет в config.yaml, profiles/*, auth, .env, в базу Канбана.
Все исключения ловятся и попадают в журнал; код возврата всегда 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

#: Единый скрытый запуск процессов (server/proc.py рядом с этим файлом): без него
#: поднятый сервер мигает чёрным консольным окном.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import proc  # noqa: E402  (путь добавляется выше)

DEFAULT_PORT = 8777
PLUGIN_ID = "team-room"
MAX_STARTS = 3
START_WINDOW_S = 600
HEALTH_WAIT_S = 25.0
LOG_LIMIT = 1024 * 1024
STATE_VERSION = 1

BUNDLE_REL = (
    "hermes-agent/apps/desktop/release/win-unpacked/resources/app.asar.unpacked/dist/assets"
)
CONFIG_DEFAULTS_REL = "hermes-agent/hermes_cli/config_defaults.py"
VOICE_MIXIN_REL = "hermes-agent/hermes_cli/cli_voice_mixin.py"

# Те же два шаблона, что в скилле hermes-voice-config (имена минифицированы и
# меняются от сборки к сборке, поэтому только регулярки).
BUNDLE_PATTERNS = (
    (
        re.compile(
            rb"(var \$?[A-Za-z_]+=)120(;function [A-Za-z_]+\(e\)\{return typeof e==`number`"
            rb"&&Number\.isFinite\(e\)&&e>0\?e:)"
        ),
        rb"\g<1>600\g<2>",
    ),
    (re.compile(rb"(maxRecordingSeconds:[A-Za-z_$]+=)120(,profile:)"), rb"\g<1>600\g<2>"),
)

CONFIG_DEFAULT_PATTERN = re.compile(rb'("max_recording_seconds"\s*:\s*)120\b')
CONFIG_DEFAULT_REPLACEMENT = rb"\g<1>600"
VOICE_ELSE_PATTERN = re.compile(rb"(else\s+)120(\.0)?\b")
VOICE_ELSE_REPLACEMENT = rb"\g<1>600\g<2>"


# ── мелкие утилиты ───────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def default_hermes_home() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "hermes" if local else Path.home() / ".hermes"


class Journal:
    """Журнал сторожа с ротацией. В пробном запуске не пишет ничего."""

    def __init__(self, path: Path, echo: bool):
        self.path = path
        self.echo = echo
        self.lines: list[str] = []

    def add(self, level: str, message: str) -> None:
        line = "%s [%s] %s" % (now_iso(), level, message)
        self.lines.append(line)
        if self.echo:
            print(line, file=sys.stderr)

    def flush(self) -> None:
        if not self.echo or not self.lines:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file() and self.path.stat().st_size > LOG_LIMIT:
                rotated = self.path.with_name(self.path.name + ".1")
                try:
                    if rotated.exists():
                        rotated.unlink()
                    self.path.rename(rotated)
                except OSError:
                    pass
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(self.lines) + "\n")
        except OSError:
            pass


# ── сеть и процессы ──────────────────────────────────────────────────────────
def health_ok(port: int, timeout: float = 3.0) -> bool:
    """Отвечает ли ИМЕННО «Комната команды» (а не любой процесс на порту)."""
    url = "http://127.0.0.1:%d/api/health" % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return False
            payload = json.loads(response.read(65536).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    app = payload.get("app")
    return isinstance(app, dict) and str(app.get("name") or "") == "team-room"


def port_open(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    """Жив ли процесс. На Windows — только через OpenProcess.

    os.kill(pid, 0) здесь НЕЛЬЗЯ: в CPython на Windows он вызывает
    TerminateProcess(handle, 0), то есть убивает процесс, а не проверяет его.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - на Windows бывает разное
        return False


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def kill_pid(pid: int) -> None:
    try:
        proc.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def spawn_flags() -> int:
    """Флаги фонового сервера: без окна, отдельная группа, отвязан от родителя."""
    return proc.detached_flags()


# ── 1. сервер ────────────────────────────────────────────────────────────────
def ensure_server(ctx: dict, messages: list[str]) -> None:
    port = ctx["port"]
    if health_ok(port):
        return

    pid_path = ctx["run_dir"] / ("app-%d.pid" % port)
    running_pid = read_pid(pid_path)

    if running_pid and pid_alive(running_pid):
        # уже запущен, просто ещё не готов — дать ему шанс
        for _ in range(10):
            time.sleep(1.0)
            if health_ok(port):
                return
        messages.append(
            "Сервер «Комнаты команды» запущен (pid %d), но не отвечает — см. data/logs/server-%d.err"
            % (running_pid, port)
        )
        ctx["journal"].add("warning", "server pid %d alive but unhealthy" % running_pid)
        return

    if port_open(port):
        # Порт занят, а health молчит: это НЕ наш процесс, убивать нельзя.
        ctx["journal"].add("warning", "port %d busy, foreign process" % port)
        messages.append(
            "Порт %d занят не «Комнатой команды» — я его не трогаю. Проверьте, что там."
            % port
        )
        return

    now = time.time()
    starts = [t for t in ctx["state"].get("starts", []) if now - t < START_WINDOW_S]
    ctx["state"]["starts"] = starts

    if len(starts) >= MAX_STARTS:
        ctx["journal"].add("error", "start rate limit hit (%d in %ds)" % (len(starts), START_WINDOW_S))
        messages.append(
            "Не могу поднять сервис «Комнаты команды» (%d попытки за 10 минут), см. data/logs/server-%d.err"
            % (len(starts), port)
        )
        return

    server_py = ctx["project_root"] / "server" / "app.py"
    if not server_py.is_file():
        ctx["journal"].add("error", "no server/app.py at %s" % server_py)
        messages.append(
            "Не нашёл сервер «Комнаты команды»: %s — возможно, папку проекта перенесли." % server_py
        )
        return

    if ctx["dry_run"]:
        messages.append("Сервер «Комнаты команды» не отвечает — запустил бы заново на порту %d." % port)
        return

    logs_dir = ctx["logs_dir"]
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / ("server-%d.out" % port)
    err_path = logs_dir / ("server-%d.err" % port)
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")

    try:
        with out_path.open("ab") as out, err_path.open("ab") as err:
            process = proc.popen(
                [sys.executable, str(server_py), "--port", str(port)],
                cwd=str(ctx["project_root"]),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                detached=True,
                close_fds=True,
            )
    except OSError as exc:
        ctx["journal"].add("error", "spawn failed: %s" % exc)
        messages.append("Не смог запустить сервер «Комнаты команды»: %s" % exc)
        return

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid_path.write_text("%d %s\n" % (process.pid, now_iso()), encoding="utf-8")
    except OSError:
        pass

    ctx["state"]["starts"] = starts + [now]
    ctx["journal"].add("info", "server spawned pid=%d port=%d" % (process.pid, port))

    deadline = time.time() + HEALTH_WAIT_S
    while time.time() < deadline:
        if health_ok(port):
            messages.append("Сервер «Комнаты команды» не отвечал — запустил заново на порту %d." % port)
            return
        if process.poll() is not None:
            break
        time.sleep(1.0)

    if process.poll() is not None:
        messages.append(
            "Сервер «Комнаты команды» упал при запуске — см. data/logs/server-%d.err" % port
        )
        ctx["journal"].add("error", "server exited rc=%s" % process.returncode)
    else:
        messages.append("Сервер «Комнаты команды» запущен, но ещё не отвечает — попробую снова через две минуты.")


# ── 2. плагин ────────────────────────────────────────────────────────────────
def ensure_plugin(ctx: dict, messages: list[str]) -> bool:
    source = ctx["project_root"] / "desktop-plugin" / "plugin.js"
    target = ctx["hermes_home"] / "desktop-plugins" / PLUGIN_ID / "plugin.js"

    if not source.is_file():
        ctx["journal"].add("error", "plugin source missing: %s" % source)
        messages.append(
            "Исходник плагина «Командные комнаты» не найден (%s) — восстановить не смогу." % source
        )
        return False

    source_hash = sha256_file(source)
    if target.is_file():
        try:
            if sha256_file(target) == source_hash:
                return True
        except OSError as exc:
            ctx["journal"].add("warning", "plugin dest unreadable: %s" % exc)

    if ctx["dry_run"]:
        messages.append("Плагин «Командные комнаты» не на месте — восстановил бы.")
        return False

    try:
        atomic_write_bytes(target, source.read_bytes())
    except OSError as exc:
        ctx["journal"].add("error", "plugin install failed: %s" % exc)
        messages.append("Не смог положить плагин «Командные комнаты»: %s" % exc)
        return False

    ctx["journal"].add("info", "plugin installed/restored -> %s" % target)
    messages.append(
        "Плагин «Командные комнаты» пропал или устарел — восстановил. "
        "Если строки в меню нет, нажмите Ctrl+K → Reload desktop plugins."
    )
    return True


# ── 3. лимит диктовки ────────────────────────────────────────────────────────
def bundle_fingerprint(assets_dir: Path) -> dict:
    files: dict[str, str] = {}
    if assets_dir.is_dir():
        for path in sorted(assets_dir.glob("index-*.js")):
            try:
                files[path.name] = sha256_file(path)
            except OSError:
                continue
    return files


def patch_bundle(assets_dir: Path, ctx: dict, messages: list[str]) -> None:
    if not assets_dir.is_dir():
        return  # не собранная сборка / другой вариант установки
    hit = False
    for path in sorted(assets_dir.glob("index-*.js")):
        try:
            data = path.read_bytes()
        except OSError as exc:
            ctx["journal"].add("warning", "bundle unreadable %s: %s" % (path.name, exc))
            continue
        new = data
        for pattern, replacement in BUNDLE_PATTERNS:
            new = pattern.sub(replacement, new)
        if new == data:
            continue
        hit = True
        if ctx["dry_run"]:
            ctx["journal"].add("info", "would patch bundle %s" % path.name)
            continue
        backup = path.with_name(path.name + ".bak-voicecap")
        try:
            if not backup.exists():
                backup.write_bytes(data)
            atomic_write_bytes(path, new)
            ctx["journal"].add("info", "bundle patched: %s" % path.name)
        except OSError as exc:
            ctx["journal"].add("error", "bundle patch failed %s: %s" % (path.name, exc))
            messages.append("Не смог поправить лимит диктовки в приложении: %s" % exc)
            return

    if hit:
        ctx["state"].setdefault("fixes", {})["dictation_bundle"] = now_iso()
        messages.append(
            "После обновления Hermes вернулся лимит диктовки 2 минуты — исправил. "
            "Нажмите Ctrl+R в Hermes."
        )


def patch_python_defaults(ctx: dict, messages: list[str]) -> None:
    targets = (
        (ctx["hermes_home"] / CONFIG_DEFAULTS_REL, CONFIG_DEFAULT_PATTERN, CONFIG_DEFAULT_REPLACEMENT),
        (ctx["hermes_home"] / VOICE_MIXIN_REL, VOICE_ELSE_PATTERN, VOICE_ELSE_REPLACEMENT),
    )
    changed_any = False
    for path, pattern, replacement in targets:
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        new = pattern.sub(replacement, data)
        if new == data:
            continue
        changed_any = True
        if ctx["dry_run"]:
            ctx["journal"].add("info", "would patch python default %s" % path.name)
            continue
        backup = path.with_name(path.name + ".bak-voicecap")
        try:
            if not backup.exists():
                backup.write_bytes(data)
            atomic_write_bytes(path, new)
            ctx["journal"].add("info", "python default patched: %s" % path)
        except OSError as exc:
            ctx["journal"].add("error", "python default patch failed %s: %s" % (path, exc))

    if changed_any:
        ctx["state"].setdefault("fixes", {})["dictation_python"] = now_iso()
        messages.append(
            "В настройках Hermes вернулся прежний лимит диктовки — исправил на 10 минут."
        )


# ── главный ход ─────────────────────────────────────────────────────────────
def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сторож «Командных комнат» для крона Hermes")
    parser.add_argument("--dry-run", action="store_true", help="ничего не менять и не писать")
    parser.add_argument("--hermes-home", default=None, help="папка hermes (по умолчанию %%LOCALAPPDATA%%\\hermes)")
    parser.add_argument("--project-root", default=None, help="папка проекта «Комната команды»")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт сервера (по умолчанию 8777)")
    args = parser.parse_args(argv)

    project_root = (
        Path(args.project_root).expanduser()
        if args.project_root
        else Path(__file__).resolve().parent.parent
    )
    hermes_home = Path(args.hermes_home).expanduser() if args.hermes_home else default_hermes_home()

    data_dir = project_root / "data"
    state_path = data_dir / "state" / "guard.json"
    journal = Journal(data_dir / "logs" / "guard.log", echo=not args.dry_run)

    state = read_json(state_path, {})
    if not isinstance(state, dict):
        state = {}

    ctx = {
        "dry_run": bool(args.dry_run),
        "port": int(args.port),
        "project_root": project_root,
        "hermes_home": hermes_home,
        "state": state,
        "journal": journal,
        "logs_dir": data_dir / "logs",
        "run_dir": data_dir / "run",
    }

    messages: list[str] = []

    # Что было до проверок — чтобы понять, обновился ли Hermes.
    assets_dir = hermes_home / BUNDLE_REL
    previous_files = state.get("bundle", {}).get("files", {}) if isinstance(state.get("bundle"), dict) else {}
    current_files = bundle_fingerprint(assets_dir)
    bundle_changed = bool(previous_files) and current_files != previous_files

    try:
        ensure_server(ctx, messages)
    except Exception as exc:  # noqa: BLE001
        journal.add("error", "ensure_server: %r" % exc)

    plugin_ok = False
    try:
        plugin_ok = ensure_plugin(ctx, messages)
    except Exception as exc:  # noqa: BLE001
        journal.add("error", "ensure_plugin: %r" % exc)

    try:
        patch_bundle(assets_dir, ctx, messages)
    except Exception as exc:  # noqa: BLE001
        journal.add("error", "patch_bundle: %r" % exc)

    try:
        patch_python_defaults(ctx, messages)
    except Exception as exc:  # noqa: BLE001
        journal.add("error", "patch_python_defaults: %r" % exc)

    if bundle_changed and not args.dry_run:
        health = "сервер отвечает" if health_ok(ctx["port"]) else "сервер не отвечает"
        messages.append(
            "Hermes обновился — модуль проверен: плагин %s, %s."
            % ("на месте" if plugin_ok else "пришлось восстановить", health)
        )
        journal.add("info", "bundle changed -> %s" % sorted(current_files))

    if not args.dry_run:
        state["version"] = STATE_VERSION
        state["checked_at"] = now_iso()
        state["hermes_home"] = str(hermes_home)
        state["project_root"] = str(project_root)
        state["port"] = ctx["port"]
        state["bundle"] = {"files": current_files, "checked_at": now_iso()}
        state["plugin_ok"] = plugin_ok
        state["messages"] = messages[-20:]
        try:
            write_json_atomic(state_path, state)
        except OSError as exc:
            journal.add("error", "state write failed: %s" % exc)

    journal.flush()

    if messages:
        for line in messages:
            print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - сторож не должен падать никогда
        try:
            print("Сторож «Командных комнат» сломался: %s" % exc)
        except Exception:  # noqa: BLE001
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
