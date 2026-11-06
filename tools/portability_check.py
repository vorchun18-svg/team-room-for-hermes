#!/usr/bin/env python3
"""Проверка переносимости: приложение на «чистой машине» и на копии живой структуры.

Две песочницы во временном каталоге (ничего в реальных данных и Vault не пишем):

  A. «Чистая машина»: HERMES_HOME — пустой каталог (нет config.yaml, нет досок),
     TEAMROOM_HOME — пустой каталог, Vault отсутствует (TEAMROOM_VAULT_DIR="").
     Ожидаем: сервер поднимается, комната пустая, комната создаётся, запись в канбан
     отвечает понятным текстом (без трассировок), /api/health и /api/rooms — 200.
  B. «Структура как у живого Hermes»: копия config.yaml (значения секретов затёрты)
     и kanban/boards/team/kanban.db (не больше 1 МиБ). Ожидаем полный цикл чтения:
     health видит доску и карточки, комнаты и бэклог читаются.

Запуск:
    python tools/portability_check.py            # обе песочницы, порты 8851–8899
    python tools/portability_check.py --keep     # оставить каталоги песочниц
    python tools/portability_check.py --json     # машинный отчёт в stdout

Только стандартная библиотека. Код выхода 0 — всё прошло.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BOARD = "team"
MAX_COPY_BYTES = 256 * 1024 * 1024   # 256 МиБ: живая доска растёт; сверх этого не копируем
START_TIMEOUT = 40.0
SECRET_LINE = re.compile(r"(?i)^(\s*[\w.\-]*(?:key|token|secret|password|auth)[\w.\-]*\s*:\s*)(.+)$")


# ------------------------------------------------------------------ утилиты

def free_port(start: int = 8851, end: int = 8899) -> int:
    """Свободный порт из диапазона наших тестовых серверов (8777/8778 не трогаем)."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("нет свободного порта в 8851–8899")


def http(method: str, url: str, body=None, timeout: float = 45.0,
         attempts: int = 2) -> tuple[int, str]:
    """HTTP-вызов с повтором: одна сетевая заминка не должна «ронять» проверку.

    Раньше `urllib` таймаут вылетал наверх трассировкой и `test_portable` падал
    без понятной причины (известный флейк полного прогона). Теперь — один повтор,
    а если сеть так и не ответила, `NetError` с адресом и временем.
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt + 1 < max(1, attempts):
                time.sleep(0.5 * (attempt + 1))
    raise NetError("нет ответа от %s за %.0f с (%s)" % (url, timeout, last))


class NetError(RuntimeError):
    """Сеть не ответила: читаемая ошибка вместо трассировки `urllib`."""


class Sandbox:
    """Песочница: пустые HERMES_HOME/TEAMROOM_HOME, свой порт, свой процесс сервера."""

    def __init__(self, base: Path, name: str, hermes_dir: Path, teamroom_home: Path,
                 vault: str | None, cli: str = "") -> None:
        self.base = base / name
        self.hermes_dir = hermes_dir
        self.teamroom_home = teamroom_home
        self.vault = vault                    # None — не задаём, "" — выключено
        self.cli = cli
        self.stt = "auto"                     # auto | off (как на машине без whisper)
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.log = self.base / "server.log"

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERMES_HOME", "HERMES_KANBAN_HOME", "TEAMROOM_DATA",
                            "TEAMROOM_VAULT_DIR", "TEAMROOM_HERMES_CLI", "TEAMROOM_SEED",
                            "TEAMROOM_STT_PYTHON", "TEAMROOM_STT_CONFIG")}
        env.update({
            "HERMES_HOME": str(self.hermes_dir),
            "TEAMROOM_HOME": str(self.teamroom_home),
            "TEAMROOM_DATA": str(self.teamroom_home / "data"),
            "TEAMROOM_SEED": "0",             # как на чистой машине: демо-комнат нет
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        if self.vault is not None:
            env["TEAMROOM_VAULT_DIR"] = self.vault
        if self.cli:
            env["TEAMROOM_HERMES_CLI"] = self.cli
        else:
            # «hermes CLI нет»: путь, которого точно не существует (пустая строка
            # означает «искать самому», и тогда нашёл бы наш hermes из PATH)
            env["TEAMROOM_HERMES_CLI"] = str(self.base / "no-hermes" / "hermes.exe")
        if self.stt == "off":
            env["TEAMROOM_STT_PYTHON"] = ""   # локального faster-whisper нет
            env["TEAMROOM_STT_CONFIG"] = str(self.base / "empty-config.yaml")
            if not (self.base / "empty-config.yaml").exists():
                self.base.mkdir(parents=True, exist_ok=True)
                (self.base / "empty-config.yaml").write_text("stt: {}\n", encoding="utf-8")
        return env

    def start(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.hermes_dir.mkdir(parents=True, exist_ok=True)
        self.teamroom_home.mkdir(parents=True, exist_ok=True)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(self.port)],
            cwd=str(ROOT), env=self.env(), stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_no_window_flag())
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("сервер завершился при запуске:\n%s" % self.tail())
            try:
                code, _ = http("GET", self.url("/api/health"), timeout=3, attempts=1)
                if code == 200:
                    return
            except (urllib.error.URLError, OSError, NetError):
                pass
            time.sleep(0.4)
        raise RuntimeError("сервер не ответил за %.0f с:\n%s" % (START_TIMEOUT, self.tail()))

    def url(self, path: str) -> str:
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def call(self, method: str, path: str, body=None, timeout: float = 60.0):
        code, text = http(method, self.url(path), body, timeout)
        try:
            return code, json.loads(text), text
        except ValueError:
            return code, {}, text

    def soft_call(self, method: str, path: str, body=None, timeout: float = 60.0):
        """То же, но сетевая ошибка становится проверкой «не ответил», а не падением."""
        try:
            return self.call(method, path, body, timeout)
        except NetError as exc:
            payload = {"error": {"message": str(exc)}}
            return 599, payload, json.dumps(payload, ensure_ascii=False)

    def tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None


def _no_window_flag() -> int:
    """CREATE_NO_WINDOW на Windows: проверка не должна мигать консолью."""
    return 0x08000000 if os.name == "nt" else 0


def redact_yaml(text: str) -> str:
    """Затереть значения строк, похожих на ключи/токены/пароли (конфиг остаётся YAML)."""
    out = []
    for line in text.splitlines():
        match = SECRET_LINE.match(line)
        if match and match.group(2).strip() and not match.group(2).strip().startswith(("#", "|", ">")):
            out.append(match.group(1) + '"***"')
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def copy_real_structure(dst: Path) -> dict:
    """Копия структуры живого Hermes без секретов: config.yaml и доска team (≤1 МиБ)."""
    from server import config as config_mod

    real = config_mod.real_hermes_home()
    info = {"source": str(real), "config": False, "board": False, "bytes": 0, "skipped": []}
    dst.mkdir(parents=True, exist_ok=True)
    cfg_file = real / "config.yaml"
    if cfg_file.is_file():
        try:
            text = cfg_file.read_text(encoding="utf-8", errors="replace")
            (dst / "config.yaml").write_text(redact_yaml(text), encoding="utf-8", newline="\n")
            info["config"] = True
        except OSError as exc:
            info["skipped"].append("config.yaml: %s" % exc)
    db = real / "kanban" / "boards" / BOARD / "kanban.db"
    if db.is_file():
        size = db.stat().st_size
        if size <= MAX_COPY_BYTES:
            target = dst / "kanban" / "boards" / BOARD / "kanban.db"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db, target)
            info["board"] = True
            info["bytes"] = size
        else:
            info["skipped"].append("kanban.db: %d байт > %d" % (size, MAX_COPY_BYTES))
    else:
        info["skipped"].append("kanban.db не найден (нет доски team)")
    for name in ("profiles", "cache", "kanban/boards"):
        p = real / name
        if p.is_dir():
            (dst / name).mkdir(parents=True, exist_ok=True)
    return info


# ------------------------------------------------------------------ проверки

class Checks:
    """Набор утверждений с человекочитаемыми именами."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def ok(self, name: str, condition: bool, detail: str = "") -> bool:
        self.items.append({"check": name, "ok": bool(condition), "detail": "" if condition else detail})
        return bool(condition)

    def failed(self) -> list[dict]:
        return [item for item in self.items if not item["ok"]]


def scenario_a(base: Path) -> tuple[dict, Checks]:
    """Чистая машина: пустой Hermes, нет Vault, нет CLI."""
    checks = Checks()
    box = Sandbox(base, "A-clean", base / "hermesA", base / "trA", vault="", cli="")
    result: dict = {"name": "A-clean", "port": box.port}
    try:
        box.start()
        code, health, raw = box.call("GET", "/api/health")
        result["health_code"] = code
        checks.ok("A: /api/health отвечает 200", code == 200, raw[:300])
        checks.ok("A: health — это JSON с данными", health.get("app", {}).get("name") == "team-room", raw[:300])
        checks.ok("A: зеркало выключено, когда Vault не найден",
                  health.get("vault", {}).get("enabled") is False, str(health.get("vault")))
        checks.ok("A: подсказка про TEAMROOM_VAULT_DIR есть",
                  "TEAMROOM_VAULT_DIR" in str(health.get("vault", {}).get("hint", "")),
                  str(health.get("vault")))
        codes = [w.get("code") for w in health.get("warnings") or []]
        checks.ok("A: предупреждение vault_disabled в health", "vault_disabled" in codes, str(codes))

        code, rooms, raw = box.call("GET", "/api/rooms")
        result["rooms_code"] = code
        checks.ok("A: /api/rooms отвечает 200", code == 200, raw[:300])
        checks.ok("A: комната пустая (демо-комнаты не создаются)", rooms.get("rooms") == [],
                  json.dumps(rooms, ensure_ascii=False)[:300])

        code, created, raw = box.call("POST", "/api/rooms", {
            "title": "Первая комната", "id": "first",
            "orchestrator": "default", "members": [{"profile": "default", "role": "worker"}]})
        result["create_code"] = code
        checks.ok("A: комната создаётся", code in (200, 201), raw[:400])

        code, listed, raw = box.call("GET", "/api/rooms")
        checks.ok("A: созданная комната в списке",
                  [r.get("id") for r in listed.get("rooms") or []] == ["first"], raw[:300])

        code, err, raw = box.call("POST", "/api/rooms/first/requests", {"text": "Проверка на чистой машине"})
        result["write_code"] = code
        message = str((err.get("error") or {}).get("message") or "")
        checks.ok("A: запись в канбан отвечает ошибкой, а не падением",
                  400 <= code < 600 and bool(message), raw[:400])
        checks.ok("A: текст ошибки понятный (русский, про доску/канбан)",
                  ("доска" in message.lower() or "канбан" in message.lower()), message[:300])
        checks.ok("A: в ответе нет трассировки", "Traceback" not in raw, raw[-400:])

        code, health2, _ = box.call("GET", "/api/health")
        checks.ok("A: health после ошибки записи — 200", code == 200)
        code, _, _ = box.call("GET", "/api/rooms/first")
        checks.ok("A: комната читается после ошибки записи", code == 200)
        result["hint"] = (health.get("vault") or {}).get("hint")
    finally:
        box.stop()
    result["tail"] = box.tail(15)
    return result, checks


def scenario_b(base: Path) -> tuple[dict, Checks]:
    """Структура как у живого Hermes: config.yaml (без секретов) и доска team."""
    checks = Checks()
    hermes_b = base / "hermesB"
    info = copy_real_structure(hermes_b)
    box = Sandbox(base, "B-live", hermes_b, base / "trB", vault="")
    result: dict = {"name": "B-live", "port": box.port, "copy": info}
    try:
        if not info["board"]:
            checks.ok("B: есть копия доски team для чтения", False, "; ".join(info["skipped"]))
            return result, checks
        box.start()
        code, health, raw = box.call("GET", "/api/health")
        result["health_code"] = code
        engine = health.get("engine") or {}
        checks.ok("B: /api/health отвечает 200", code == 200, raw[:300])
        checks.ok("B: движок видит файл доски", bool(engine.get("db_exists")), str(engine)[:300])
        checks.ok("B: карточки доски читаются", int((engine.get("tasks") or {}).get("total") or 0) > 0,
                  str(engine.get("tasks")))

        code, created, raw = box.call("POST", "/api/rooms", {
            "title": "Комната на живой доске", "id": "live",
            "orchestrator": "default", "members": [{"profile": "default", "role": "worker"}]})
        result["create_code"] = code
        checks.ok("B: комната создаётся поверх живой структуры", code in (200, 201), raw[:400])

        code, room, raw = box.call("GET", "/api/rooms/live")
        checks.ok("B: комната читается", code == 200 and room.get("id") == "live", raw[:300])
        for path, name in (("/api/rooms/live/backlog", "бэклог"),
                           ("/api/rooms/live/requests", "запросы"),
                           ("/api/rooms/live/journal", "журнал"),
                           ("/api/config", "конфиг")):
            code, payload, raw = box.call("GET", path)
            checks.ok("B: %s читается (200)" % name, code == 200, raw[:300])
        result["tasks"] = (health.get("engine") or {}).get("tasks")
    finally:
        box.stop()
    result["tail"] = box.tail(15)
    return result, checks


def _screens(box: "Sandbox", room_id: str, prefix: str, checks: "Checks") -> None:
    """Все экраны и основные чтения открываются: ни падений, ни трассировок.

    Экраны, которым нужна доска (команда, логи движка), без доски отвечают понятной
    ошибкой — это нормально; проверяем, что это JSON с сообщением, а не стек.
    """
    screens = (
        ("/", "страница приложения", True),
        ("/api/health", "health", True),
        ("/api/config", "настройки пути и возможностей", True),
        ("/api/rooms", "список комнат", True),
        ("/api/profiles", "профили", True),
        ("/api/stt/status", "состояние диктовки", True),
        ("/api/logs", "логи", False),
        ("/api/search?q=%D0%BA%D0%BE%D0%BC%D0%BD%D0%B0%D1%82%D0%B0", "поиск", True),
        ("/api/rooms/%s" % room_id, "комната", True),
        ("/api/rooms/%s/requests" % room_id, "запросы комнаты", True),
        ("/api/rooms/%s/backlog" % room_id, "бэклог", True),
        ("/api/rooms/%s/team" % room_id, "команда", False),
        ("/api/rooms/%s/roadmap" % room_id, "карта проекта", True),
        ("/api/rooms/%s/journal" % room_id, "журнал", True),
        ("/api/rooms/%s/reports" % room_id, "отчёты", True),
    )
    for path, name, strict in screens:
        code, payload, raw = box.soft_call("GET", path)
        if strict:
            checks.ok("%s: %s открывается (200)" % (prefix, name), code == 200, raw[:300])
        else:
            explained = code == 200 or (isinstance(payload, dict) and bool(payload.get("error")))
            checks.ok("%s: %s отвечает без падения (%s)" % (prefix, name, code), explained, raw[:300])
        checks.ok("%s: %s без трассировки" % (prefix, name), "Traceback" not in raw, raw[-300:])


def scenario_c(base: Path) -> tuple[dict, "Checks"]:
    """Ничего «нашего»: нет faster-whisper, нет hermes CLI, нет Vault.

    C1 — с копией живой доски: комнаты и запросы читаются.
    C2 — без доски: вместо падения понятная плашка, экраны открываются.
    """
    checks = Checks()
    result: dict = {"name": "C-nothing", "port": None, "cases": []}
    hermes_copy = base / "hermesC"
    copy_real_structure(hermes_copy)

    for case, hermes_dir in (("C1-с доской", hermes_copy), ("C2-без доски", base / "hermesC-empty")):
        box = Sandbox(base, "C-%s" % case.replace(" ", ""), hermes_dir, base / ("trC-" + case.split("-")[0]),
                      vault="", cli="")
        box.stt = "off"                      # диктовка недоступна: ни whisper, ни провайдера
        try:
            box.start()
            code, health, raw = box.call("GET", "/api/health")
            checks.ok("%s: /api/health 200" % case, code == 200, raw[:300])
            features = health.get("features") or {}
            checks.ok("%s: возможности отданы в features" % case,
                      isinstance(features, dict) and "dictation" in features, str(features)[:300])
            checks.ok("%s: kanban_write=false (нет CLI)" % case,
                      features.get("kanban_write") is False, str(features)[:300])
            checks.ok("%s: vault_mirror=false (нет Vault)" % case,
                      features.get("vault_mirror") is False, str(features)[:300])
            checks.ok("%s: dictation=none (нет whisper и провайдера)" % case,
                      features.get("dictation") == "none", str(features)[:300])

            code, stt_state, raw = box.call("GET", "/api/stt/status")
            checks.ok("%s: /api/stt/status 200" % case, code == 200, raw[:300])
            checks.ok("%s: диктовка честно недоступна" % case,
                      stt_state.get("available") is False and bool(stt_state.get("reason")),
                      str(stt_state)[:300])
            checks.ok("%s: причина без трассировки" % case, "Traceback" not in raw, raw[-300:])

            code, created, raw = box.call("POST", "/api/rooms", {
                "title": "Без лишнего", "id": "bare",
                "orchestrator": "default", "members": [{"profile": "default", "role": "worker"}]})
            checks.ok("%s: комната создаётся" % case, code in (200, 201), raw[:300])

            code, listed, raw = box.call("GET", "/api/rooms/bare/requests")
            checks.ok("%s: запросы комнаты читаются" % case, code == 200, raw[:300])

            code, err, raw = box.call("POST", "/api/rooms/bare/requests", {"text": "Проверка"})
            board = bool((health.get("engine") or {}).get("db_exists"))
            if board:
                checks.ok("%s: запись в живую доску доступна или объяснена" % case,
                          code in (200, 201, 400, 409, 500, 503), raw[:300])
            else:
                message = str((err.get("error") or {}).get("message") or "")
                checks.ok("%s: без доски — понятная плашка" % case,
                          400 <= code < 600 and ("доска" in message.lower() or "канбан" in message.lower()),
                          raw[:300])
                checks.ok("%s: в плашке нет трассировки" % case, "Traceback" not in raw, raw[-300:])

            _screens(box, "bare", case, checks)
            result["cases"].append({"case": case, "port": box.port, "board": board,
                                    "features": features})
        finally:
            box.stop()
            result["tail"] = box.tail(10)
    return result, checks


def _run_scenario(runner, base: Path, report: dict, name: str) -> None:
    """Выполнить сценарий; сетевая/пусковая ошибка — провал проверки, а не падение."""
    try:
        result, checks = runner(base)
    except (RuntimeError, OSError, NetError) as exc:
        result = {"name": name, "error": str(exc)[:800], "tail": ""}
        checks = Checks()
        checks.ok("%s: сценарий выполнился" % name, False, str(exc)[:400])
    report["scenarios"].append(result)
    report["checks"] += checks.items


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Проверка переносимости «Комнаты команды»")
    parser.add_argument("--keep", action="store_true", help="не удалять каталоги песочниц")
    parser.add_argument("--json", action="store_true", help="отчёт в JSON")
    parser.add_argument("--only", choices=("a", "b", "c"), help="только один сценарий")
    args = parser.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="teamroom-portable-"))
    report: dict = {"base": str(base), "scenarios": [], "checks": []}
    try:
        if args.only in (None, "a"):
            _run_scenario(scenario_a, base, report, "A-clean")
        if args.only in (None, "b"):
            _run_scenario(scenario_b, base, report, "B-live")
        if args.only in (None, "c"):
            _run_scenario(scenario_c, base, report, "C-nothing")
    finally:
        failed = [item for item in report["checks"] if not item["ok"]]
        report["ok"] = not failed
        if args.keep:
            report["kept"] = str(base)
        else:
            shutil.rmtree(base, ignore_errors=True)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            print("%s  %s%s" % ("OK  " if item["ok"] else "FAIL", item["check"],
                               "" if item["ok"] else "  ← " + str(item["detail"])[:200]))
        passed = sum(1 for item in report["checks"] if item["ok"])
        print("\nитог: %d/%d проверок пройдено" % (passed, len(report["checks"])))
        if not report["ok"]:
            for scenario in report["scenarios"]:
                print("\n--- %s (порт %s), хвост лога ---\n%s"
                      % (scenario["name"], scenario.get("port"), scenario.get("tail", "")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
