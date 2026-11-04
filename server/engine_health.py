"""Чтение канбана: здоровье движка (`health`) и состояние диспетчера (`dispatcher_status`).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

try:  # пакетный и «плоский» импорт (python server/app.py)
    from .engine_db import ACTIVE, EngineError, SCHEMA_VERSION, _conn, _schema_missing
    from .config import VERSION
except ImportError:  # pragma: no cover
    from engine_db import ACTIVE, EngineError, SCHEMA_VERSION, _conn, _schema_missing  # type: ignore
    from config import VERSION  # type: ignore

# -------------------------------------------------------------- публичные API ---
def health(cfg, board: str = "team") -> dict:
    """Диагностика движка. Никогда не бросает исключений."""
    out = {"ok": False, "version": VERSION, "time": int(time.time()),
           "engine": {"kanban_home": str(cfg.kanban_home), "board": board, "board_db": "",
                      "db_exists": False, "schema_ok": False, "schema_version": None,
                      "schema_missing": [], "db_bytes": None,
                      "tasks": None, "error": None, "dispatcher_ok": False,
                      "gateway": {"running": False, "pid": None, "state": "unknown", "age_s": None}}}
    try:
        db = Path(cfg.kanban_db(board))
        out["engine"]["board_db"] = str(db)
        out["engine"]["db_exists"] = db.is_file()
        if not out["engine"]["db_exists"]:
            out["engine"]["error"] = f"Нет файла БД доски «{board}»: {db}"
        else:
            out["engine"]["db_bytes"] = db.stat().st_size
            with _conn(cfg, board) as con:
                missing = _schema_missing(con)
                out["engine"]["schema_missing"] = missing
                out["engine"]["schema_ok"] = not missing
                out["engine"]["schema_version"] = SCHEMA_VERSION if not missing else None
                out["engine"]["tasks"] = _task_counts(con)
    except EngineError as exc:
        out["engine"]["error"] = exc.message
    except Exception as exc:  # noqa: BLE001 — health не падает никогда
        out["engine"]["error"] = f"{type(exc).__name__}: {exc}"
    try:
        disp = dispatcher_status(cfg)
    except Exception:  # noqa: BLE001
        disp = {}
    out["engine"]["gateway"] = {"running": bool(disp.get("running")), "pid": disp.get("pid"),
                                "state": disp.get("state"), "age_s": disp.get("age_s")}
    out["engine"]["dispatcher_ok"] = bool(disp.get("ok"))
    out["ok"] = bool(out["engine"]["db_exists"] and out["engine"]["schema_ok"])
    return out


def _task_counts(con):
    try:
        counts = {(r["status"] or ""): r["n"] for r in con.execute("SELECT status, count(*) n FROM tasks GROUP BY status")}
    except sqlite3.Error:
        return None
    return {"total": sum(counts.values()), "done": counts.get("done", 0),
            "blocked": counts.get("blocked", 0), "active": sum(counts.get(s, 0) for s in ACTIVE)}


def _pid_alive(pid) -> bool:
    """Живость процесса без его убийства (на Windows — OpenProcess+GetExitCodeProcess)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def dispatcher_status(cfg) -> dict:
    """Живость диспетчера: gateway.pid → gateway_state.json (свежесть <120 c) → проверка pid."""
    home = Path(cfg.hermes_home)
    try:
        pid_doc = json.loads((home / "gateway.pid").read_text("utf-8", "replace"), strict=False)
    except (OSError, ValueError):
        pid_doc = {}
    try:
        state_doc = json.loads((home / "gateway_state.json").read_text("utf-8", "replace"), strict=False)
    except (OSError, ValueError):
        state_doc = {}
    pid_doc = pid_doc if isinstance(pid_doc, dict) else {}
    state_doc = state_doc if isinstance(state_doc, dict) else {}
    pid, age_s, updated_at = state_doc.get("pid") or pid_doc.get("pid"), None, None
    if state_doc.get("updated_at"):
        try:
            dt = datetime.fromisoformat(str(state_doc["updated_at"]).replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            age_s, updated_at = int(datetime.now(timezone.utc).timestamp() - dt.timestamp()), int(dt.timestamp())
        except (ValueError, TypeError, OSError):
            pass
    alive = _pid_alive(pid) if pid else False
    running = alive and (age_s is None or age_s <= 120) and state_doc.get("gateway_state", "running") == "running"
    if running:
        return {"ok": True, "running": True, "pid": pid, "state": "running", "age_s": age_s,
                "updated_at": updated_at, "warning": None}
    if pid and not alive:
        return {"ok": False, "running": False, "pid": pid, "state": "stale", "age_s": age_s, "updated_at": updated_at,
                "warning": f"Шлюз Hermes (pid {pid}) не запущен — диспетчер не берёт карточки в работу."}
    if pid and alive:
        return {"ok": False, "running": False, "pid": pid, "state": "stale", "age_s": age_s, "updated_at": updated_at,
                "warning": "Шлюз Hermes жив, но состояние не обновлялось больше 120 с — диспетчер, похоже, завис."}
    return {"ok": False, "running": False, "pid": pid, "state": "stopped", "age_s": age_s, "updated_at": updated_at,
            "warning": "Шлюз Hermes не запущен (gateway.pid нет) — запросы не начнут работу сами."}
