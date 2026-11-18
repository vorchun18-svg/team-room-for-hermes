#!/usr/bin/env python3
"""Мини-клиент Chrome DevTools Protocol на стандартной библиотеке.

Зачем свой: CDN/pip запрещены, а `websocket-client` в окружении нет. Здесь только
`socket` + `struct`: рукопожатие WebSocket (RFC 6455, без сжатия), текстовые кадры и
синхронный CDP поверх фонового потока-читателя. Фоновый читатель важен для UI-тестов:
события (`Runtime.exceptionThrown`, `Log.entryAdded`, `Network.*`) приходят между
командами и не должны теряться, пока мы ждём ответ на `Runtime.evaluate`.

Публично:
    find_chrome() -> Path
    free_port() -> int
    Chrome.launch(width=1280, height=900, chrome=None) -> Chrome   (контекстный менеджер)
    Chrome.cdp.eval_js(expr) / .call(method, params) / .events_since(idx)
    Chrome.navigate(url) / .screenshot(path) / .set_viewport(w, h)

Браузер пользователя не трогаем: временный `--user-data-dir`, свой порт, процесс
убивается в `close()`. Реализация совместима с `tools/shot.py` (тот же набор флагов).
"""
from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

#: Единый скрытый запуск процессов (server/proc.py): иначе Chrome мигает консолью.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import proc as spawn  # noqa: E402  (корень проекта добавлен выше)


# ─────────────────────────────────────────────────────────────── браузер

def find_chrome() -> Path:
    """chrome.exe: переменные окружения → hermes tools → стандартные пути."""
    env = os.environ.get("TEAMROOM_CHROME") or os.environ.get("CHROME_PATH") or os.environ.get("CHROME")
    candidates: list[str] = []
    if env:
        candidates.append(env)
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    candidates += sorted(
        glob.glob(os.path.join(local, "hermes", "tools", "chromium-*", "chrome-win64", "chrome.exe")),
        reverse=True,
    )
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise RuntimeError("Не найден Chromium/Chrome. Задайте путь: TEAMROOM_CHROME=C:\\...\\chrome.exe")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ─────────────────────────────────────────────────────── мини WebSocket

class WS:
    """Клиент WebSocket: только текстовые кадры, без сжатия (хватает для CDP)."""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        parts = urlsplit(ws_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path + (("?" + parts.query) if parts.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP: соединение закрыто при рукопожатии")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("CDP: WebSocket не принял рукопожатие: " + head.decode(errors="replace")[:200])
        self.sock.settimeout(None)          # читатель блокируется до события/закрытия
        self._send_lock = threading.Lock()

    def _recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("CDP: соединение закрыто")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        while True:
            b1, b2 = self._recv(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv(8))[0]
            masked = bool(b2 & 0x80)
            mask = self._recv(4) if masked else None
            data = self._recv(length) if length else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:  # close
                raise RuntimeError("CDP: браузер закрыл соединение")
            if opcode == 0x9:  # ping → pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2, 0x0):
                return data.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """Синхронный CDP с фоновым читателем: ответы по id, события — в список."""

    def __init__(self, ws: WS):
        self.ws = ws
        self.seq = 0
        self.events: list[dict] = []
        self._waiters: dict[int, dict] = {}
        self._lock = threading.Lock()
        self.closed = False
        self._thread = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        while not self.closed:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001 - закрытие/битый кадр завершают читателя
                self.closed = True
                with self._lock:
                    for waiter in self._waiters.values():
                        waiter["event"].set()
                return
            mid = msg.get("id")
            if mid is not None:
                with self._lock:
                    waiter = self._waiters.pop(int(mid), None)
                if waiter:
                    waiter["msg"] = msg
                    waiter["event"].set()
                continue
            with self._lock:
                self.events.append(msg)

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self.seq += 1
        mid = self.seq
        waiter = {"event": threading.Event(), "msg": None}
        with self._lock:
            self._waiters[mid] = waiter
        try:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            if not waiter["event"].wait(timeout):
                raise RuntimeError(f"CDP {method}: таймаут {timeout}s")
            msg = waiter["msg"]
            if msg is None:
                raise RuntimeError(f"CDP {method}: соединение закрыто")
            if "error" in msg:
                raise RuntimeError(f"CDP {method}: {msg['error']}")
            return msg.get("result", {})
        finally:
            with self._lock:
                self._waiters.pop(mid, None)

    def events_since(self, index: int) -> list[dict]:
        with self._lock:
            return list(self.events[index:])

    def event_index(self) -> int:
        with self._lock:
            return len(self.events)

    def wait_event(self, name: str, timeout: float = 20.0) -> bool:
        """Дождаться события по имени (для совместимости с tools/shot.py)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for ev in list(self.events):
                    if ev.get("method") == name:
                        self.events.remove(ev)
                        return True
            time.sleep(0.02)
        return False

    def eval_js(self, expression: str, await_promise: bool = False, timeout: float = 20.0):
        """Выполнить JS-выражение и вернуть значение (returnByValue)."""
        res = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": bool(await_promise), "userGesture": True,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text") or "JS error"
            raise RuntimeError("JS: " + str(text)[:500])
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        self.closed = True
        self.ws.close()


# ─────────────────────────────────────────────────────────────── Chrome

def _kill_tree(pid: int) -> None:
    """Убить процесс Chrome вместе с дочерними (Windows: taskkill /T, иначе terminate)."""
    if not pid:
        return
    try:
        if os.name == "nt":
            spawn.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001 - процесс уже мог завершиться
        pass


class Chrome:
    """Headless Chrome с временным профилем; `with Chrome.launch(...) as ch:`."""

    def __init__(self, proc, profile: Path, port: int, ws_url: str):
        self.proc = proc
        self.profile = profile
        self.port = port
        self.ws_url = ws_url
        self.ws = WS(ws_url)
        self.cdp = CDP(self.ws)

    @classmethod
    def launch(cls, width: int = 1280, height: int = 900, chrome: str | Path | None = None,
               dpr: float = 1.0) -> "Chrome":
        exe = Path(chrome) if chrome else find_chrome()
        profile = Path(tempfile.mkdtemp(prefix="teamroom-e2e-"))
        port = free_port()
        cmd = [
            str(exe), "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-software-rasterizer", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-sync",
            "--disable-component-update", "--disable-crashpad", "--disable-breakpad",
            "--disable-crash-reporter", "--disable-background-networking", "--no-pings",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "--force-color-profile=srgb", "--mute-audio",
            "--force-device-scale-factor=" + str(dpr),
            "--window-size=%d,%d" % (max(width, 800), max(height, 600)),
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "about:blank",
        ]
        proc = spawn.popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = ""
        for _ in range(200):
            if proc.poll() is not None:
                break
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1.0) as r:
                    targets = json.loads(r.read().decode("utf-8"))
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001 - порт ещё не поднялся
                pass
            time.sleep(0.05)
        if not ws_url:
            _kill_tree(proc.pid)
            shutil.rmtree(profile, ignore_errors=True)
            raise RuntimeError("отладочный порт Chromium не поднялся")
        inst = cls(proc, profile, port, ws_url)
        inst.cdp.call("Page.enable")
        inst.cdp.call("Runtime.enable")
        inst.cdp.call("Log.enable")
        inst.cdp.call("Network.enable")
        inst.cdp.call("Network.setCacheDisabled", {"cacheDisabled": True})
        inst.set_viewport(width, height, dpr)
        return inst

    def set_viewport(self, width: int, height: int, dpr: float = 1.0) -> None:
        self.cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": int(width), "height": int(height),
            "deviceScaleFactor": float(dpr), "mobile": False,
        })

    def navigate(self, url: str, timeout: float = 30.0) -> None:
        self.cdp.call("Page.navigate", {"url": url}, timeout=timeout)

    def screenshot(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = self.cdp.call("Page.captureScreenshot",
                             {"format": "png", "captureBeyondViewport": False}, timeout=30)
        raw = base64.b64decode(data.get("data") or "")
        if not raw:
            raise RuntimeError("CDP не вернул изображение")
        out.write_bytes(raw)
        return out

    def close(self) -> None:
        pid = self.proc.pid
        try:
            self.cdp.close()
        except Exception:  # noqa: BLE001
            pass
        # Сначала дерево целиком: terminate() убивает только главный процесс, а
        # renderer/gpu/crashpad на Windows остаются висеть с временным профилем.
        _kill_tree(pid)
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self) -> "Chrome":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False
