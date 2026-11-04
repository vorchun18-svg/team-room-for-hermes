# -*- coding: utf-8 -*-
"""Остановка серверов «Комнаты команды» по pid-файлу или порту.

Трогает только процессы Python, в командной строке которых есть «team-room»:
мок (tools/mock_server.py) и боевой сервер (server/app.py). Чужой процесс на
порту не убивается — скрипт сообщает об этом и возвращает код 3.

    python tools/stop_team_room.py                 # 8777 и 8778
    python tools/stop_team_room.py --port 8784     # один тестовый порт
    python tools/stop_team_room.py --port 8784 --port 8786
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import proc as spawn  # noqa: E402  (единый скрытый запуск процессов)

RUN_DIR = os.path.join(ROOT, "data", "run")
DEFAULT_PORTS = (8777, 8778)
MARKER = "team-room"


def _powershell(script: str) -> str:
    try:
        result = spawn.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("Не удалось вызвать PowerShell: %s" % exc, file=sys.stderr)
        return ""
    return (result.stdout or "").strip()


def _json_list(raw: str) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    if isinstance(data, int):
        return [data]
    return []


def listeners(port: int) -> list:
    raw = _powershell("@(Get-NetTCPConnection -State Listen -LocalPort %d -ErrorAction SilentlyContinue "
                      "| Select-Object -ExpandProperty OwningProcess -Unique) | ConvertTo-Json -Compress" % port)
    return [int(p) for p in _json_list(raw) if isinstance(p, int)]


def process_info(pid: int) -> dict:
    raw = _powershell("$p = Get-CimInstance Win32_Process -Filter 'ProcessId=%d'; "
                      "if ($p) { $p | Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress }" % pid)
    data = _json_list(raw)
    return data[0] if data else {}


def is_ours(info: dict) -> bool:
    name = str(info.get("Name") or "").lower()
    command = str(info.get("CommandLine") or "").lower()
    return name.startswith("python") and MARKER in command


def pid_files(port: int) -> list:
    out = []
    if not os.path.isdir(RUN_DIR):
        return out
    for name in os.listdir(RUN_DIR):
        if not name.lower().endswith(".pid") or ("-%d.pid" % port) not in name.lower():
            continue
        path = os.path.join(RUN_DIR, name)
        try:
            with open(path, encoding="ascii") as fh:
                pid = int((fh.read() or "").strip())
        except (OSError, ValueError):
            pid = 0
        out.append((path, pid))
    return out


def candidates(port: int) -> list:
    seen, out = set(), []
    for path, pid in pid_files(port):
        if pid > 0 and pid not in seen:
            seen.add(pid)
            out.append(pid)
    for pid in listeners(port):
        if pid > 0 and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def kill(pid: int) -> bool:
    try:
        result = spawn.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def stop_port(port: int) -> str:
    """'stopped' | 'free' | 'foreign' — что случилось с портом."""
    pids = candidates(port)
    ours, foreign = [], []
    for pid in pids:
        info = process_info(pid)
        if not info:
            continue
        (ours if is_ours(info) else foreign).append((pid, info))
    if not ours and not foreign:
        return "free"
    stopped = []
    for pid, info in ours:
        if kill(pid):
            stopped.append(pid)
            print("Порт %d: остановлен PID %d (%s)" % (port, pid, os.path.basename(str(info.get("Name") or "python"))))
        else:
            print("Порт %d: не удалось остановить PID %d — закройте вручную" % (port, pid), file=sys.stderr)
    for path, pid in pid_files(port):
        if pid in stopped:
            try:
                os.remove(path)
            except OSError:
                pass
    for pid, info in foreign:
        print("Порт %d: занят PID %d (%s) — это не процесс team-room, не трогаю"
              % (port, pid, info.get("Name") or "?"), file=sys.stderr)
    if foreign:
        return "foreign"
    if not stopped:
        return "free"
    deadline = time.time() + 5
    while time.time() < deadline:
        if not listeners(port):
            return "stopped"
        time.sleep(0.2)
    return "stopped"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Остановить серверы «Комнаты команды» (8777/8778)")
    ap.add_argument("--port", action="append", type=int, default=None,
                    help="порт (можно несколько раз); по умолчанию 8777 и 8778")
    args = ap.parse_args(argv)
    ports = args.port or list(DEFAULT_PORTS)
    for port in ports:
        if not 1 <= port <= 65535:
            print("Некорректный порт: %s" % port, file=sys.stderr)
            return 2
    results = [stop_port(port) for port in ports]
    if all(r == "free" for r in results):
        print("Серверы не запущены — останавливать нечего.")
        return 0
    if "foreign" in results:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
