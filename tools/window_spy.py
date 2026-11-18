#!/usr/bin/env python3
"""Пробник мигающих консольных окон «Комнаты команды».

Считает НОВЫЕ окна консольных классов (``ConsoleWindowClass``,
``CASCADIA_HOSTING_WINDOW_CLASS``, ``PseudoConsoleWindow``), которые появляются,
пока пользователь работает в комнатах: вход, все вкладки, диалог комнаты,
сохранение, диктовка, тик сторожа. Цель — 0.

Как устроено:
  * сам пробник перезапускает себя БЕЗ консоли (`DETACHED_PROCESS`) — как Hermes
    Desktop, который и создаёт мигание: у процесса с собственной консолью дочерние
    процессы наследуют её и новых окон не появляется, поэтому «проверить из
    терминала» ничего не доказывает;
  * сервер поднимается своим процессом на 8896 скрыто (`DETACHED|CREATE_NO_WINDOW`)
    в песочнице (`tests.helpers.Sandbox`), демо — мок на 8897; боевые 8777/8778
    не трогаются никогда;
  * окна перечисляются через ``EnumWindows``/``GetWindowThreadProcessId`` (ctypes)
    в фоновом потоке каждые 40 мс: мигающее окно живёт доли секунды, обычный
    «снимок до/после» его пропускает. Каждое окно запоминается со временем
    появления, шаги сценария — со своими интервалами, поэтому окно попадает
    в тот шаг, когда оно реально появилось, а не в следующий;
  * мигающим считается окно с ненулевой площадью: у ``PseudoConsoleWindow``
    (служебное окно ConPTY рядом со СКРЫТОЙ консолью) размер 0×0 — пользователь
    его не видит;
  * первым шагом идёт КОНТРОЛЬ: три запуска БЕЗ флагов обязаны дать окно;
  * сценарий идёт в headless Chrome через CDP (временный профиль в temp), поэтому
    окна пользователя не мешают; без Chrome сценарий проходит через HTTP.

    python tools/window_spy.py                # полный прогон, код 1 при миганиях
    python tools/window_spy.py --no-ui        # только сервер и API, без браузера
    python tools/window_spy.py --inline       # не перезапускаться (с консолью)
    python tools/window_spy.py --keep         # не убирать песочницу (отладка)

Важно: CLI канбана подменён на сам python (`TEAMROOM_HERMES_CLI`), чтобы сторож
не запускал настоящих агентов; сам запуск процесса при этом настоящий — флаг
`CREATE_NO_WINDOW` проверяется по-честному.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import proc                                        # noqa: E402
from tools.cdp import Chrome, find_chrome                      # noqa: E402

#: Классы консольных окон Windows (обычная консоль, Windows Terminal, псевдоконсоль).
CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow")

ROOM = "cafe"
TABS = ("composer", "team", "roadmap", "backlog", "reports", "journal")
POLL_S = 0.04
STEP_SETTLE_S = 1.1
LAG_SETTLE_S = 1.6          # запас: новое консольное окно появляется не мгновенно
NEW_CONSOLE = 0x00000010    # контроль: принудительно видимая консоль


# ───────────────────────────────────────────────────── окна Windows
def _process_name(pid: int) -> str:
    """Имя исполняемого файла процесса по pid (Windows; иначе пусто)."""
    if os.name != "nt" or not pid:
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))     # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, ValueError):
        return ""


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def console_windows() -> dict:
    """{hwnd: {class, pid, exe, title, size}} видимых консольных окон системы."""
    if os.name != "nt":
        return {}
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (OSError, ValueError):
        return {}
    found: dict = {}

    def _callback(hwnd, _lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(256)
            if not user32.GetClassNameW(hwnd, cls_buf, 256):
                return True
            cls = cls_buf.value
            if cls not in CONSOLE_CLASSES:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = _Rect()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            size = (int(rect.right - rect.left), int(rect.bottom - rect.top))
            if size[0] <= 0 or size[1] <= 0:
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buf, 256)
            found[int(hwnd)] = {"class": cls, "pid": int(pid.value),
                                "exe": _process_name(int(pid.value)),
                                "title": title_buf.value[:120], "size": size}
        except Exception:  # noqa: BLE001 - окно могло исчезнуть между вызовами
            pass
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(_callback)
    try:
        user32.EnumWindows(callback, 0)
    except (OSError, ValueError):
        return found
    return found


class WindowSpy:
    """Фоновый счётчик новых консольных окон + интервалы шагов сценария."""

    def __init__(self) -> None:
        self.baseline = console_windows()
        self.new: dict = {}                      # hwnd -> описание + время появления
        self.steps: list = []                    # [(название, t_start, t_end)]
        self.t0 = time.monotonic()
        self._seen = set(self.baseline)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: list = []

    # --- фон
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._harvest()
            except Exception as exc:  # noqa: BLE001 - счётчик не должен падать
                self.errors.append("%s: %s" % (exc.__class__.__name__, exc))
            time.sleep(POLL_S)

    def _harvest(self) -> None:
        current = console_windows()
        now = time.monotonic() - self.t0
        with self._lock:
            for hwnd, info in current.items():
                if hwnd not in self._seen:
                    self._seen.add(hwnd)
                    self.new[hwnd] = dict(info, seen_at=now)

    # --- отметки
    def begin(self) -> float:
        """Начало шага: время от старта счётчика."""
        return time.monotonic() - self.t0

    def mark(self, name: str, started: float) -> list:
        """Закрыть шаг и вернуть окна, впервые замеченные за время шага.

        Окно принадлежит ровно одному шагу (первому, в чей интервал оно попало):
        иначе одно и то же окно считалось бы дважды при пересчёте.
        """
        self._harvest()
        ended = time.monotonic() - self.t0
        with self._lock:
            self.steps.append((name, started, ended))
        return [group for title, group in self.groups() if title == name][-1]

    def groups(self) -> list:
        """[(название, [окна])] — раскладка по времени, без двойного счёта."""
        out = []
        with self._lock:
            taken: set = set()
            for name, started, ended in self.steps:
                group = []
                for hwnd, info in self.new.items():
                    if hwnd in taken:
                        continue
                    if started <= info["seen_at"] < ended:
                        taken.add(hwnd)
                        group.append(info)
                out.append((name, group))
        return out

    @property
    def total(self) -> int:
        """Сколько окон открыло само приложение (без контрольного запуска)."""
        return sum(len(group) for name, group in self.groups() if not name.startswith("[контроль]"))


# ───────────────────────────────────────────────────── HTTP
def _http(url: str, *, method: str = "GET", body: bytes | None = None,
          headers: dict | None = None, timeout: float = 20.0):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (OSError, ValueError) as exc:
        return 0, str(exc).encode("utf-8")


def wait_health(port: int, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        status, raw = _http("http://127.0.0.1:%d/api/health" % port, timeout=3.0)
        if status == 200:
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                last = "ответ не JSON"
        else:
            last = "HTTP %s" % status
        time.sleep(0.25)
    raise RuntimeError("сервер на %d не ответил: %s" % (port, last))


class Step:
    def __init__(self, title: str) -> None:
        self.title = title
        self.ok = True
        self.note = ""


class Probe:
    """Сценарий: песочница → скрытый сервер → браузер/API → отчёт по шагам."""

    def __init__(self, args) -> None:
        self.args = args
        self.spy = WindowSpy()
        self.steps: list = []
        self.server = None
        self.mock = None
        self.chrome = None
        self.sandbox = None
        self.cfg = None
        self.logs: dict = {}
        self.env: dict = {}

    # --- шаги
    def _step(self, title: str, fn) -> None:
        step = Step(title)
        started = self.spy.begin()
        try:
            fn(step)
        except Exception as exc:  # noqa: BLE001 - провал шага не рушит прогон
            step.ok = False
            step.note = "%s: %s" % (exc.__class__.__name__, str(exc)[:160])
        self._settle(LAG_SETTLE_S)          # окно консоли появляется с задержкой
        fresh = self.spy.mark(title, started)
        self.steps.append(step)
        print("  %-32s окон: %d%s" % (title, len(fresh),
                                      "" if step.ok else "   (шаг не выполнен: %s)" % step.note))

    def _settle(self, seconds: float = STEP_SETTLE_S) -> None:
        time.sleep(seconds)

    # --- браузерные помощники
    def _js(self, expr: str):
        return self.chrome.cdp.eval_js(expr)

    def _goto(self, path_hash: str) -> None:
        base = "http://127.0.0.1:%d/" % self.args.port
        self.chrome.navigate(base)
        self._wait_view()
        if path_hash:
            self._js("location.hash = %s" % json.dumps(path_hash))
        self._settle()

    def _wait_view(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self._js("!!document.querySelector('#view') && "
                            "document.querySelector('#view').children.length > 0"):
                    return
            except Exception:  # noqa: BLE001 - страница ещё грузится
                pass
            time.sleep(0.1)
        raise RuntimeError("страница не отрисовалась")

    def _click(self, pattern: str, selector: str = "button") -> bool:
        expr = ("(() => { const re = new RegExp(%s); const el = [...document.querySelectorAll(%s)]"
                ".find(n => re.test((n.textContent || '').trim()));"
                " if (!el) return false; el.click(); return true; })()"
                % (json.dumps(pattern), json.dumps(selector)))
        return bool(self._js(expr))

    # --- прогон
    def run(self) -> int:
        print("Пробник консольных окон «Комнаты команды»")
        print("  консольных окон в базе: %d (то, что было до старта, не считается)"
              % len(self.spy.baseline))
        self.spy.start()
        try:
            self._setup()
            self._scenario()
        finally:
            self._teardown()
            self.spy.stop()
        return self._report()

    def _setup(self) -> None:
        from tests.helpers import Sandbox
        self.sandbox = Sandbox().__enter__()
        self.cfg = self.sandbox.cfg
        env = dict(os.environ)
        env.update({
            "TEAMROOM_PORT": str(self.args.port),
            "TEAMROOM_HERMES_CLI": sys.executable,   # заглушка: агентов не запускаем
            "TEAMROOM_NO_SHELL": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        })
        self.env = env
        logs = Path(self.cfg.data_dir) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.logs = {name: (logs / name) for name in ("spy-server.log", "spy-mock.log")}

        def control(step: Step) -> None:
            """Контроль чувствительности: видимая консоль обязана попасть в счёт.

            `CREATE_NEW_CONSOLE` — единственный флаг, который гарантированно даёт
            видимое окно: без флагов и с `DETACHED_PROCESS` поведение зависит от
            того, какое приложение терминала выбрано в системе.
            """
            if not self.args.control:
                step.note = "контроль отключён"
                return
            before = len(self.spy.new)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(2.0)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=NEW_CONSOLE)
            deadline = time.time() + 6.0
            while time.time() < deadline and len(self.spy.new) <= before:
                time.sleep(0.05)
            child.wait(timeout=30)

        self._step("[контроль] запуск без флагов", control)

        def start_server(step: Step) -> None:
            out = self.logs["spy-server.log"].open("ab")
            self.server = proc.popen(
                [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(self.args.port)],
                cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                detached=True)
            payload = wait_health(self.args.port)
            step.note = "rooms=%s" % ((payload.get("data") or {}).get("rooms"))

        def start_mock(step: Step) -> None:
            out = self.logs["spy-mock.log"].open("ab")
            self.mock = proc.popen(
                [sys.executable, str(ROOT / "tools" / "mock_server.py"),
                 "--port", str(self.args.mock_port)],
                cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                detached=True)
            deadline = time.time() + 40
            while time.time() < deadline:
                status, _ = _http("http://127.0.0.1:%d/api/health" % self.args.mock_port, timeout=2)
                if status == 200:
                    step.note = "мок отвечает"
                    return
                time.sleep(0.25)
            raise RuntimeError("мок на %d не поднялся" % self.args.mock_port)

        self._step("старт сервера %d" % self.args.port, start_server)
        self._step("старт демо-мока %d" % self.args.mock_port, start_mock)

        def start_browser(step: Step) -> None:
            if self.args.no_ui:
                step.note = "без браузера (--no-ui)"
                return
            self.chrome = Chrome.launch(width=1280, height=900)
            step.note = find_chrome().name

        self._step("headless Chrome", start_browser)

    def _scenario(self) -> None:
        port = self.args.port
        base = "http://127.0.0.1:%d/" % port

        def open_room(step: Step) -> None:
            if self.chrome is None:
                status, _ = _http(base + "api/rooms?include_archived=1")
                step.note = "HTTP %s (без браузера)" % status
                return
            self._goto("#/rooms")
            self._goto("#/room/%s" % ROOM)

        self._step("вход в комнату", open_room)

        def all_tabs(step: Step) -> None:
            if self.chrome is None:
                for _tab in TABS:
                    _http("%sapi/rooms/%s" % (base, ROOM))
                step.note = "%d вкладок по API" % len(TABS)
                return
            for tab in TABS:
                self._goto("#/room/%s/%s" % (ROOM, tab))
            step.note = "%d вкладок" % len(TABS)

        self._step("переходы по вкладкам", all_tabs)

        def open_dialog(step: Step) -> None:
            if self.chrome is None:
                step.note = "диалог пропущен (--no-ui)"
                return
            self._goto("#/room/%s/team" % ROOM)
            # именно настройки комнаты: кнопок «Изменить…» на вкладке несколько
            clicked = self._js(
                "(() => { const b = document.querySelector('button[title*=\"Руководитель комнаты\"]');"
                " if (b) { b.click(); return true; }"
                " const any = [...document.querySelectorAll('button')]"
                ".find(x => /Изменить/.test(x.textContent || ''));"
                " if (!any) return false; any.click(); return true; })()")
            if not clicked:
                raise RuntimeError("кнопка настроек комнаты не найдена")
            self._settle(0.9)
            if not self._js("!!document.querySelector('.roomdlg')"):
                raise RuntimeError("диалог комнаты не открылся")
            step.note = "открыт"

        self._step("диалог комнаты", open_dialog)

        def save_room(step: Step) -> None:
            if self.chrome is None:
                step.note = "сохранение пропущено (--no-ui)"
                return
            if not self._click("^\\s*Сохранить|^\\s*Создать комнату"):
                raise RuntimeError("кнопка «Сохранить» не найдена")
            self._settle(1.4)
            closed = self._js("!document.querySelector('.roomdlg')")
            step.note = "диалог закрылся" if closed else "диалог остался открыт"

        self._step("сохранение комнаты", save_room)

        def engine_spawn(step: Step) -> None:
            """Главный подозреваемый: `engine_cli._run` → `hermes kanban …`."""
            status, _ = _http("%sapi/rooms/%s/requests" % (base, ROOM), method="POST",
                              body=json.dumps({"text": "Проверка тишины: краткий запрос."}).encode(),
                              headers={"Content-Type": "application/json"})
            self._settle(1.2)
            log = Path(self.cfg.log_dir) / "engine.log"
            calls = 0
            try:
                calls = len(log.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                calls = 0
            step.note = "HTTP %s, вызовов CLI в журнале: %d" % (status, calls)
            if status not in (200, 201, 400, 409, 422, 500, 503):
                raise RuntimeError("неожиданный ответ на запрос: %s" % status)

        self._step("запрос в комнату (движок)", engine_spawn)

        def dictation(step: Step) -> None:
            url = "http://127.0.0.1:%d/api/stt/transcribe" % self.args.mock_port
            status, _ = _http(url, method="POST", body=b"\x1aE\xdf\xa3" + b"\x00" * 400,
                              headers={"Content-Type": "audio/webm"})
            step.note = "мок STT: HTTP %s" % status
            if status not in (200, 400, 413, 415):
                raise RuntimeError("неожиданный ответ заглушки: %s" % status)

        self._step("диктовка (заглушка STT)", dictation)

        def watcher(step: Step) -> None:
            first = wait_health(port).get("watcher") or {}
            started = first.get("last_run_at")
            deadline = time.time() + 40
            while time.time() < deadline:
                payload = wait_health(port, timeout=5)
                state = payload.get("watcher") or {}
                if state.get("last_run_at") and state.get("last_run_at") != started:
                    step.note = "тик %s" % state.get("last_run_at")
                    self._settle(1.0)
                    return
                time.sleep(0.5)
            step.note = "тик не дождались за 40 с (last_run_at=%s)" % started

        self._step("тик сторожа", watcher)

    def _teardown(self) -> None:
        if self.chrome is not None:
            try:
                self.chrome.close()
            except Exception:  # noqa: BLE001
                pass
        for child in (self.server, self.mock):
            if child is None:
                continue
            try:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=6)
                    except Exception:  # noqa: BLE001
                        child.kill()
            except Exception:  # noqa: BLE001
                pass
        if self.args.keep or self.sandbox is None:
            return
        try:
            self.sandbox.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    def _report(self) -> int:
        total = self.spy.total
        groups = self.spy.groups()
        control = [group for name, group in groups if name.startswith("[контроль]")]
        print("")
        for name, group in groups:
            if not group:
                continue
            print("  %s — %d:" % (name, len(group)))
            for info in group:
                print("    · %s pid=%s %s %dx%d «%s»"
                      % (info["class"], info["pid"], info["exe"] or "?",
                         info["size"][0], info["size"][1], info["title"]))
        caught = len(control[0]) if control else 0
        if self.args.control:
            if caught:
                print("контроль: запуск без флагов поймали (%d окно) — счётчик зрячий" % caught)
            else:
                print("контроль не поймал окно: в этой сессии новые консоли открываются "
                      "вкладкой Windows Terminal, а не окном — замер по окнам невозможен")
        print("новых консольных окон: %d" % total)
        for step in self.steps:
            if step.note:
                print("  %s: %s" % (step.title, step.note))
        if total:
            print("ОКНО МИГАЕТ: ищите запуск мимо server/proc.py (creationflags=CREATE_NO_WINDOW).")
        else:
            print("тихо: за прогон приложение не открыло ни одного консольного окна")
        if self.spy.errors:
            print("счётчик окон: %d ошибок, первая — %s" % (len(self.spy.errors), self.spy.errors[0]))
        failed = [s.title for s in self.steps if not s.ok]
        if failed:
            print("шаги, которые не выполнились: %s" % ", ".join(failed))
        return 0 if total == 0 else 1


# ───────────────────────────────────────────────────── запуск
def _relaunch_without_console(argv: list[str]) -> int:
    """Перезапустить пробник без своей консоли и отдать его отчёт в наш stdout."""
    log = Path(tempfile.mkdtemp(prefix="win-spy-")) / "report.txt"
    with log.open("wb") as out:
        child = proc.popen([sys.executable, str(Path(__file__).resolve()), "--child", *argv],
                           stdout=out, stderr=subprocess.STDOUT, detached=True)
        try:
            code = child.wait(timeout=900)
        except subprocess.TimeoutExpired:
            child.kill()
            print("пробник не закончил за 15 минут", file=sys.stderr)
            return 3
    print(log.read_text(encoding="utf-8", errors="replace"))
    return code


def main(argv=None) -> int:
    try:                                     # русский вывод в консоль Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Пробник мигающих консольных окон")
    parser.add_argument("--port", type=int, default=8896, help="порт своего сервера (песочница)")
    parser.add_argument("--mock-port", type=int, default=8897, help="порт демо-мока")
    parser.add_argument("--no-ui", action="store_true", help="без браузера: только сервер и API")
    parser.add_argument("--keep", action="store_true", help="не убирать песочницу (отладка)")
    parser.add_argument("--inline", action="store_true",
                        help="не перезапускаться без консоли (тогда мигание не воспроизвести)")
    parser.add_argument("--no-control", dest="control", action="store_false",
                        help="без контрольного запуска")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for port in (args.port, args.mock_port):
        if port in (8777, 8778):
            print("ОТКАЗ: 8777/8778 — боевой и демо-серверы, пробник их не трогает.", file=sys.stderr)
            return 2
    if os.name != "nt":
        print("Пробник окон рассчитан на Windows: других консольных окон здесь не бывает.",
              file=sys.stderr)
        return 2
    if not args.child and not args.inline:
        passthrough = list(argv if argv is not None else sys.argv[1:])
        return _relaunch_without_console(passthrough)
    return Probe(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
