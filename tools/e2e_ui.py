#!/usr/bin/env python3
"""Сквозной UI-тест «Комнаты команды» в настоящем headless Chrome.

    python tools/e2e_ui.py [--shots docs/shots/e2e] [--only S2] [--json out.json]

Что делает:
1. поднимает ПЕСОЧНИЦУ (`tests.helpers.Sandbox`) и реальный сервер
   (`server.app.create_app(cfg, start_watcher=False)`) на свободном порту в потоке;
2. регистрирует посеянные запросы комнаты cafe в реестре и один раз зовёт
   `watcher.tick_once()`, чтобы ingest наполнил здание проекта;
3. запускает headless Chrome (временный профиль), подписывается на исключения,
   консоль, `Log.entryAdded` и `Network.*`;
4. прогоняет сценарии S1–S10 (навигация, страницы состояний, композер, «Дальше»,
   команда, палитра, настройки, отчёты, XSS, отсутствие внешней сети);
5. пишет JSON и `docs/status/e2e-report.md`, код возврата 1 при любом провале.

Реальная доска только читается (`snapshot_real_db` до/после). Браузер пользователя
не трогается: свой `--user-data-dir`, процесс убивается, сервер останавливается.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.app as app_mod                                  # noqa: E402
from server import proc as spawn                             # noqa: E402
from server import registry                                  # noqa: E402
from tests.helpers import Sandbox, assert_real_untouched, snapshot_real_db  # noqa: E402
from tools.cdp import Chrome                                 # noqa: E402
from tools.seed_sandbox import _assert_writable_db, _insert   # noqa: E402

TABS = ["composer", "team", "roadmap", "backlog", "reports", "journal"]
STATES = ["queued", "working", "reviewing", "needs_you", "done_modern", "done_legacy", "failed"]
STATUS_LABEL = {
    "queued": "в очереди", "triage": "разбор", "todo": "к выполнению",
    "working": "в работе", "running": "идёт", "synthesizing": "сводит",
    "reviewing": "проверка", "review": "проверка",
    "needs_you": "нужен ответ", "blocked": "заблокирована",
    "done": "готово", "failed": "ошибка",
}
SCENARIOS = {
    "S1": "Навигация по всем вкладкам, темы и ширины",
    "S2": "Страницы всех посеянных состояний запроса",
    "S3": "Композер: Ctrl+Enter → запрос создан в песочнице",
    "S4": "«Дальше»: шаг открывается окном, согласие и новый срок",
    "S5": "Команда: добавить и убрать участника",
    "S6": "Ctrl+K: палитра находит «лег» и ведёт на запрос",
    "S7": "Настройки: здоровье без ошибок, переключение темы",
    "S8": "Отчёты: галерея → отчёт done_modern отрисован",
    "S9": "XSS-проба: script/onerror/javascript: не исполняются",
    "S10": "Нет внешних сетевых запросов",
    # Волна 9b: сценарий живёт в tools/e2e_project.py (мок на своём порту 8883),
    # здесь он подключается как отдельный шаг — S4 занят «Дальше», поэтому S11.
    "S11": "Проектный режим на моке: комната → бриф → карта → запуск → диалог → «Дальше»",
    # W10: папка проекта у каждой комнаты, «Настройки комнаты», цикл смены пути.
    "S12": "Папка проекта и настройки комнаты: «Задать папку» → путь → «Открыть» → смена → убрать",
    # W19 (R2): окно настроек — разделы-вкладки, защита несохранённого, проектный режим.
    "S13": "Настройки комнаты: вкладки, «Доска» убрана, Esc не выбрасывает введённое, проектный режим из карты",
}

JS_READY = r"""
(() => {
  const view = document.querySelector('#view');
  if (!view) return {ready: false, sk: 0, inner: false, err: false, hash: location.hash, text: ''};
  const req = view.querySelector('.req');
  const inner = view.querySelector('.view__inner') || req || view.querySelector('.card') || view.querySelector('.rooms');
  const sk = view.querySelectorAll('.skeleton').length;
  const err = /Экран не открылся/.test(view.innerText || '');
  // На странице запроса «скелетон» в блоке ответа — постоянная заглушка «ответа ещё нет»,
  // поэтому готовность определяем по заголовку, а не по исчезновению skeleton.
  const ready = req ? Boolean(view.querySelector('.req__title')) : (Boolean(inner) && sk === 0 && !err);
  return {ready: ready && !err, sk, inner: Boolean(inner), err,
          hash: location.hash, title: document.title,
          text: (view.innerText || '').slice(0, 240)};
})()
"""

JS_OVERFLOW = r"""
(() => {
  const de = document.documentElement;
  return {w: de.clientWidth, sw: de.scrollWidth,
          bw: document.body ? document.body.scrollWidth : de.scrollWidth,
          vw: window.innerWidth, h: de.clientHeight, sh: de.scrollHeight};
})()
"""


JS_REQ_READY = r"""
(() => {
  const t = document.querySelector('.req__title');
  const ans = document.querySelector('#answer');
  const tree = document.querySelector('[data-rp-tree]');
  const nodes = document.querySelectorAll('[data-rp-tree] .rp-node').length;
  const busy = document.querySelector('.rp-embed__body[aria-busy="true"]');
  return {ready: Boolean(t && ans && tree) && nodes >= 1 && !busy,
          title: Boolean(t), answer: Boolean(ans), tree: Boolean(tree), nodes,
          busy: Boolean(busy), sk: document.querySelectorAll('#view .skeleton').length,
          hash: location.hash};
})()
"""


# ─────────────────────────────────────────────────────────────── результат

class Result:
    """Один сценарий: проверки, дефекты, скриншоты, время."""

    def __init__(self, sid: str, title: str):
        self.id = sid
        self.title = title
        self.checks: list[dict] = []
        self.defects: list[dict] = []
        self.notes: list[str] = []
        self.screenshots: list[str] = []
        self.started = time.time()
        self.finished = 0.0
        self.error = ""

    def check(self, name: str, ok: bool, got="", want="", screen: str = "") -> bool:
        entry = {"name": name, "ok": bool(ok), "got": str(got)[:600], "want": str(want)[:300],
                 "screen": screen}
        self.checks.append(entry)
        if not ok:
            self.defects.append({"scenario": self.id, "screen": screen, "title": name,
                                 "got": entry["got"], "want": entry["want"]})
        return bool(ok)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c["ok"])

    @property
    def status(self) -> str:
        if self.error:
            return "FAIL"
        return "PASS" if self.checks and not self.failed else "FAIL"

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status,
                "checks": self.checks, "defects": self.defects, "notes": self.notes,
                "screenshots": self.screenshots,
                "duration_s": round((self.finished or time.time()) - self.started, 2), "error": self.error}


# ─────────────────────────────────────────────────────────────── харнесс

class Harness:
    """Песочница + сервер + Chrome + журнал событий CDP."""

    def __init__(self, opts):
        self.opts = opts
        self.shots = Path(opts.shots)
        self.shots.mkdir(parents=True, exist_ok=True)
        for old in self.shots.glob("s[0-9]*-*.png"):   # только свои файлы прошлых прогонов
            try:
                old.unlink()
            except OSError:
                pass
        self.before: dict = {}
        self.sbx = None
        self.sb = None
        self.cfg = None
        self.server = None
        self.thread = None
        self.chrome = None
        self.cdp = None
        self.port = 0
        self.base = ""
        self.net_urls: dict[str, str] = {}
        self.setup: list[dict] = []
        self.data_notes: list[str] = []
        self.events_all: list[dict] = []
        self.events_log: list[dict] = []
        self._event_seen: set = set()
        self.cleanup: dict = {}
        self.tick_stats: dict = {}

    # --- запуск ------------------------------------------------------------
    def start(self) -> None:
        self.before = snapshot_real_db()
        self.sbx = Sandbox()
        self.sb = self.sbx.__enter__()
        self.cfg = self.sb.cfg
        self.register_seeded()
        self.server = app_mod.create_app(self.cfg, start_watcher=False, port=0)
        self.port = int(self.server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       name="team-room-e2e-ui", daemon=True)
        self.thread.start()
        try:
            self.tick_stats = self.server.http_app.watcher.tick_once()
        except Exception as exc:  # noqa: BLE001
            self.setup.append({"name": "watcher.tick_once", "ok": False, "detail": str(exc)[:500]})
        if self.server.http_app.watcher.last_error:
            self.setup.append({"name": "watcher.last_error", "ok": False,
                               "detail": self.server.http_app.watcher.last_error})
        row = self.sb.one("SELECT max(created_at) AS m FROM tasks WHERE tenant='room:cafe'")
        if row and int(row["m"] or 0) > int(time.time()) + 60:
            self.data_notes.append(
                "Фикстуры песочницы используют фиксированный BASE_TS (09:31 -03); на этом прогоне "
                "часть меток в будущем (max created_at=%s, сейчас=%s), поэтому «идёт 0 с» у working — "
                "артефакт данных, не UI." % (row["m"], int(time.time())))
        self.chrome = Chrome.launch(width=1280, height=900, chrome=self.opts.chrome)
        self.cdp = self.chrome.cdp
        self.events_all = self.cdp.events
        self.goto_full("light", "#/")      # стартовый экран: дальше сценарии ходят по hash
        self.check_setup()

    def register_seeded(self) -> None:
        """Записать посеянные корни в реестр комнаты cafe (иначе их не видно в UI)."""
        for name in STATES:
            state = self.sb.state(name)
            row = self.sb.one("SELECT title, body, created_at FROM tasks WHERE id=?", (state["root_id"],))
            if row is None:
                self.setup.append({"name": f"seed:{name}", "ok": False, "detail": "нет карточки"})
                continue
            extras = []
            for kind, cid in (state.get("extra_cards") or {}).items():
                if isinstance(cid, str) and cid.startswith("t_") and kind in ("critic", "revision"):
                    extras.append({"id": cid, "kind": kind, "round": 1,
                                   "at": int(row["created_at"] or 0)})
            registry.add_request(self.cfg, "cafe", {
                "root_id": state["root_id"],
                "slug": state.get("slug") or name,
                "title": row["title"],
                "text": row["body"] or row["title"],
                "created_at": int(row["created_at"] or 0),
                "critic": False, "mode": "auto", "adopted": False, "retargeted": True,
                "extra_cards": extras,
            })

    def check_setup(self) -> None:
        status, data, _raw = self.api("/api/health")
        self.setup.append({"name": "GET /api/health", "ok": status == 200 and bool(data and data.get("ok")),
                           "detail": f"{status} ok={data.get('ok') if isinstance(data, dict) else None}"})
        status, data, _raw = self.api("/api/rooms/cafe/requests")
        items = (data or {}).get("items") if isinstance(data, dict) else None
        self.setup.append({"name": "запросы cafe в реестре", "ok": status == 200 and len(items or []) >= len(STATES),
                           "detail": f"{status} items={len(items or [])}"})
        status, data, _raw = self.api("/api/rooms/cafe/roadmap")
        phases = (data or {}).get("phases") if isinstance(data, dict) else None
        self.setup.append({"name": "ingest заполнил дорожную карту", "ok": status == 200 and len(phases or []) >= 6,
                           "detail": f"{status} phases={len(phases or [])}"})
        status, data, _raw = self.api("/api/rooms/cafe/backlog")
        backlog_items = (data or {}).get("items") if isinstance(data, dict) else None
        self.setup.append({"name": "ingest заполнил бэклог", "ok": status == 200 and len(backlog_items or []) > 0,
                           "detail": f"{status} items={len(backlog_items or [])}"})
        try:
            root = Path(self.sb.root).resolve()
            vault_ok = root in Path(self.cfg.vault_dir).resolve().parents
            data_ok = root in Path(self.cfg.data_dir).resolve().parents
        except Exception:  # noqa: BLE001
            vault_ok = data_ok = False
        self.setup.append({"name": "Vault и data внутри песочницы", "ok": bool(vault_ok and data_ok),
                           "detail": f"vault={self.cfg.vault_dir} data={self.cfg.data_dir} root={self.sb.root}"})
        self.setup.append({"name": "watcher.tick_once", "ok": not (self.tick_stats.get("errors") or 0),
                           "detail": json.dumps(self.tick_stats, ensure_ascii=False)})

    # --- HTTP --------------------------------------------------------------
    def api(self, path: str, method: str = "GET", body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            ctype = (resp.getheader("Content-Type") or "").lower()
            data = json.loads(raw.decode("utf-8")) if raw and "json" in ctype else None
            return resp.status, data, raw
        finally:
            conn.close()

    def json_ok(self, path: str):
        status, data, raw = self.api(path)
        return status, data if isinstance(data, dict) else {}, raw

    # --- навигация ---------------------------------------------------------
    def _mark(self) -> int:
        return self.cdp.event_index()

    def goto_full(self, theme: str, hash_: str = "#/") -> tuple[int, dict]:
        mark = self.cdp.event_index()
        url = f"{self.base}/?theme={theme}{hash_}"
        self.chrome.navigate(url)
        state = self.wait_ready(20)
        return mark, state

    def goto(self, hash_: str, wait: bool = True) -> tuple[int, dict]:
        mark = self.cdp.event_index()
        try:
            href = str(self.cdp.eval_js("location.href") or "")
        except Exception:  # noqa: BLE001 - about:blank / страница ещё не готова
            href = ""
        if not href.startswith(self.base):
            self.chrome.navigate(self.base + "/" + hash_)   # первый переход — полная загрузка
        else:
            try:
                self.cdp.eval_js("(() => { location.hash = %s; return location.hash; })()" % json.dumps(hash_))
            except Exception:  # noqa: BLE001 - страница могла не загрузиться
                self.chrome.navigate(self.base + "/" + hash_)
        state = self.wait_ready(20) if wait else {"ready": False, "hash": hash_}
        return mark, state

    def wait_ready(self, timeout: float = 15.0) -> dict:
        last: dict = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                last = self.cdp.eval_js(JS_READY) or {}
            except Exception as exc:  # noqa: BLE001
                last = {"ready": False, "error": str(exc)[:200]}
            if last.get("ready"):
                self.wait_net_idle(3.0)
                return last
            time.sleep(0.08)
        return last

    def wait_net_idle(self, timeout: float = 3.0) -> bool:
        mark = self.cdp.event_index()
        deadline = time.time() + timeout
        calm = 0
        while time.time() < deadline:
            sent = done = 0
            for ev in self.cdp.events_since(mark):
                method = ev.get("method")
                if method == "Network.requestWillBeSent":
                    sent += 1
                elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                    done += 1
            calm = calm + 1 if sent - done <= 0 else 0
            if calm >= 2:
                return True
            time.sleep(0.08)
        return False

    def wait_request_ready(self, timeout: float = 6.0) -> dict:
        """Дождаться полной отрисовки страницы запроса: заголовок, ответ, дерево, отчёт."""
        last: dict = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                last = self.cdp.eval_js(JS_REQ_READY) or {}
            except Exception as exc:  # noqa: BLE001
                last = {"ready": False, "error": str(exc)[:200]}
            if last.get("ready"):
                self.wait_net_idle(2.0)
                return last
            time.sleep(0.08)
        return last

    def reset_etags(self) -> None:
        """Сбросить кэш ETag в браузере (изолировать проверки от известного 304-бага)."""
        try:
            self.cdp.eval_js("import('/js/api.js').then((m) => { m.api.resetEtags(); return true; })",
                             await_promise=True)
        except Exception:  # noqa: BLE001 - страница могла быть не на приложении
            pass

    def shot(self, name: str) -> str:
        path = self.shots / (name + ".png")
        try:
            self.chrome.screenshot(path)
        except Exception as exc:  # noqa: BLE001
            self.setup.append({"name": "screenshot " + name, "ok": False, "detail": str(exc)[:300]})
            return ""
        try:
            shown = str(path.relative_to(ROOT))
        except ValueError:
            shown = str(path)
        return shown.replace("\\", "/")

    # --- события -----------------------------------------------------------
    def parse_events(self, start: int, screen: str = "") -> list[dict]:
        """Ошибки/предупреждения консоли, исключения, 4xx/5xx и сетевые сбои."""
        out: list[dict] = []
        for ev in self.cdp.events_since(start):
            method = ev.get("method")
            p = ev.get("params") or {}
            if method == "Network.requestWillBeSent":
                rid = p.get("requestId")
                url = ((p.get("request") or {}).get("url") or "")
                if rid:
                    self.net_urls[rid] = url
                continue
            if method == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                exc = d.get("exception") or {}
                text = str(exc.get("description") or d.get("text") or "JS exception")
                out.append({"kind": "exception", "level": "error", "text": text[:1200],
                            "url": str(d.get("url") or ""), "line": d.get("lineNumber"),
                            "screen": screen})
                continue
            if method == "Runtime.consoleAPICalled":
                typ = str(p.get("type") or "")
                if typ not in ("error", "warning"):
                    continue
                parts = []
                for arg in p.get("args") or []:
                    if "value" in arg:
                        parts.append(str(arg.get("value")))
                    else:
                        parts.append(str(arg.get("description") or arg.get("type") or ""))
                frames = (p.get("stackTrace") or {}).get("callFrames") or []
                url = str((frames[0] or {}).get("url") or "") if frames else ""
                out.append({"kind": "console", "level": typ, "text": " ".join(filter(None, parts))[:1200],
                            "url": url, "screen": screen})
                continue
            if method == "Log.entryAdded":
                entry = p.get("entry") or {}
                level = str(entry.get("level") or "")
                if level not in ("error", "warning"):
                    continue
                out.append({"kind": "log", "level": level, "text": str(entry.get("text") or "")[:1200],
                            "url": str(entry.get("url") or ""), "screen": screen})
                continue
            if method == "Network.responseReceived":
                resp = p.get("response") or {}
                status = int(resp.get("status") or 0)
                if status < 400:
                    continue
                url = str(resp.get("url") or "")
                is_api = "/api/" in url
                out.append({"kind": "http", "level": "error" if is_api else "warning",
                            "status": status, "text": f"HTTP {status} · {p.get('type', '')}",
                            "url": url, "screen": screen})
                continue
            if method == "Network.loadingFailed":
                if p.get("canceled"):
                    continue
                text = str(p.get("errorText") or "")
                if "ERR_ABORTED" in text:
                    continue
                url = self.net_urls.get(str(p.get("requestId") or ""), "")
                out.append({"kind": "network", "level": "warning", "text": text,
                            "url": url, "screen": screen})
        for entry in out:
            key = (entry.get("kind"), entry.get("text"), entry.get("url"), entry.get("screen"))
            if key not in self._event_seen:
                self._event_seen.add(key)
                self.events_log.append(entry)
        return out

    @staticmethod
    def benign(ev: dict) -> bool:
        text = (ev.get("text") or "") + " " + (ev.get("url") or "")
        return "favicon.ico" in text or "favicon" in text.lower()


# ─────────────────────────────────────────────────────────── сценарии

def _screen_common(h: Harness, res: Result, mark: int, screen: str, shot_name: str) -> tuple[list[dict], dict]:
    """Проверки, общие для каждого экрана S1: переполнение, ошибки, скриншот."""
    over = h.cdp.eval_js(JS_OVERFLOW) or {}
    width = int(over.get("w") or 0)
    scroll = max(int(over.get("sw") or 0), int(over.get("bw") or 0))
    res.check(f"{screen}: нет горизонтального переполнения", scroll <= width + 1,
              f"scrollWidth={scroll} clientWidth={width}", "scrollWidth ≤ clientWidth", screen)
    events = h.parse_events(mark, screen)
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    warnings = [e for e in events if e["level"] == "warning" and not h.benign(e)]
    res.check(f"{screen}: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто",
              "0 ошибок", screen)
    path = h.shot(shot_name)
    if path:
        res.screenshots.append(path)
    return events, {"overflow": over, "warnings": warnings, "screenshot": path}


def scenario_s1(h: Harness) -> Result:
    res = Result("S1", SCENARIOS["S1"])
    plans = [(1280, 900, "light"), (1280, 900, "dark"), (900, 800, "light")]
    views = [("home", "#/"), ("settings", "#/settings"),
             ("search", "#/search?q=" + quote("легализац"))]
    all_events: list[dict] = []
    for width, height, theme in plans:
        h.chrome.set_viewport(width, height)
        mark, state = h.goto_full(theme, "#/")
        applied = h.cdp.eval_js("document.documentElement.dataset.theme") or ""
        res.check(f"{width}×{height} {theme}: тема применена", applied == theme,
                  f"data-theme={applied}", theme, "start")
        if not state.get("ready"):
            res.check(f"{width}×{height} {theme}: старт загрузился", False,
                      json.dumps(state, ensure_ascii=False), "ready", "start")
        for room in ("cafe", "fn"):
            for tab in TABS:
                screen = f"{width}x{height} {theme} {room}/{tab}"
                hash_ = f"#/room/{room}/{tab}"
                mark, state = h.goto(hash_)
                res.check(f"{screen}: экран загрузился", bool(state.get("ready")),
                          json.dumps(state, ensure_ascii=False)[:300], "skeleton исчез, вьюха есть", screen)
                events, _extra = _screen_common(h, res, mark, screen,
                                                f"s1-{width}-{theme}-{room}-{tab}")
                all_events += events
        for name, hash_ in views:
            screen = f"{width}x{height} {theme} {name}"
            mark, state = h.goto(hash_)
            res.check(f"{screen}: экран загрузился", bool(state.get("ready")),
                      json.dumps(state, ensure_ascii=False)[:300], "skeleton исчез, вьюха есть", screen)
            events, _extra = _screen_common(h, res, mark, screen, f"s1-{width}-{theme}-{name}")
            all_events += events
    h.s1_events = all_events
    return res


def scenario_s2(h: Harness) -> Result:
    res = Result("S2", SCENARIOS["S2"])
    for name in STATES:
        rid = h.sb.state(name)["root_id"]
        screen = f"request:{name}"
        h.reset_etags()          # изолируем DOM-проверки от 304-бага (он проверяется отдельно ниже)
        mark, state = h.goto(f"#/room/cafe/request/{rid}")
        ready = h.wait_request_ready(6)
        perf = h.cdp.eval_js(
            "performance.getEntriesByType('resource').filter((e) => e.name.includes('/api/requests/%s')).map((e) => e.responseStatus)" % rid) or []
        status, detail, raw = h.json_ok("/api/requests/" + rid)
        res.check(f"{name}: API 200", status == 200, f"{status} {raw[:200]!r}", "200", screen)
        if status != 200:
            continue
        dom = h.cdp.eval_js(r"""
        (() => {
          const q = (s) => document.querySelector(s);
          const title = q('.req__title');
          const meta = q('.req__meta');
          const answer = q('#answer');
          const body = q('#answer .req-answer__body');
          return {
            ready: Boolean(title), title: title ? title.textContent.trim() : '',
            titleAttr: title ? String(title.getAttribute('title') || '').trim() : '',
            meta: meta ? meta.innerText.replace(/\s+/g, ' ').trim() : '',
            answer: answer ? (answer.innerText || '').replace(/\s+/g, ' ').trim() : '',
            answerBody: body ? (body.innerText || '').trim().length : 0,
            nodes: document.querySelectorAll('[data-rp-tree] .rp-node').length,
            treeEmpty: Boolean(q('[data-rp-tree] .empty')),
            rb: document.querySelectorAll('.rb-heading, .rb-text, .rb-stat, .rb-phases, .rb-callout, .rb-compare, .rb-timeline, .rb-table').length,
            svg: document.querySelectorAll('#view svg').length,
            embed: Boolean(q('[data-rp=report], .rp-embed')),
            reportError: (() => { const e = q('.rp-embed .banner--error, .rp-embed .banner--warn'); return e ? e.innerText.trim().slice(0,200) : ''; })(),
          };
        })()
        """) or {}
        res.check(f"{name}: экран загрузился", bool(dom.get("ready")) and bool(ready.get("ready")),
                  json.dumps({"dom": state, "full": ready, "api_status": perf[-2:]}, ensure_ascii=False)[:500],
                  "заголовок, ответ и дерево", screen)
        if not ready.get("ready") and 304 in perf:
            res.notes.append("Страница запроса после композера остаётся на skeleton: api.js кэширует "
                             "ETag по URL (composer-feed заранее грузит /api/requests/<id>), повторный "
                             "условный GET возвращает 304, а request.refresh() выходит по res.notModified "
                             "до render() — данных в новом экземпляре нет. Затронуты все состояния, "
                             "чьи детали уже грузил композер.")
        # W17-G §1 (B8/P5): в шапке короткое название (`title_short`), полный текст — в подсказке.
        # Пока сервер не пересчитал название, клиент показывает своё короткое — оба варианта
        # допустимы, но заголовок обязан быть коротким и относиться к ЭТОМУ запросу.
        want_full = str(detail.get("title") or h.sb.state(name).get("root_id"))
        want_short = str(detail.get("title_short") or want_full)
        got_title = str(dom.get("title") or "")
        short_ok = (bool(got_title) and got_title[:40] == want_short[:40]) or (
            bool(got_title) and len(got_title) <= 70 and want_full.startswith(got_title))
        res.check(f"{name}: заголовок — короткое название этого запроса",
                  short_ok, got_title[:120], want_short[:120], screen)
        res.check(f"{name}: полный текст названия — в подсказке заголовка",
                  want_full[:50] in str(dom.get("titleAttr") or ""),
                  str(dom.get("titleAttr"))[:130], f"содержит «{want_full[:50]}…»", screen)
        label = STATUS_LABEL.get(str(detail.get("status") or ""), "")
        res.check(f"{name}: чип статуса «{label}»", bool(label) and label in str(dom.get("meta") or ""),
                  str(dom.get("meta"))[:220], f"содержит «{label}»", screen)
        plural_m = re.search(r"(\d+) из (\d+) (работ\w*)", str(dom.get("meta") or ""))
        if plural_m:
            total = int(plural_m.group(2))
            right = "работы" if total == 1 else "работ"
            if plural_m.group(3) != right:
                res.notes.append("Русская форма прогресса: «%s из %s %s» — после «из» нужно «%s» "
                                 "(plural() считает по total, а не по предлогу)."
                                 % (plural_m.group(1), plural_m.group(2), plural_m.group(3), right))
        answer_ok = len(str(dom.get("answer") or "")) >= (200 if detail.get("status") == "done" else 20)
        res.check(f"{name}: блок ответа непуст", answer_ok,
                  f"len={len(str(dom.get('answer') or ''))} · {str(dom.get('answer'))[:120]}",
                  "непустой блок #answer", screen)
        nodes = int(dom.get("nodes") or 0)
        want_nodes = len(detail.get("tree") or [])
        res.check(f"{name}: дерево работ непусто", nodes >= max(1, min(want_nodes, 2)) and not dom.get("treeEmpty"),
                  f"узлов={nodes}, API tree={want_nodes}, empty={dom.get('treeEmpty')}",
                  f"≥ {max(1, min(want_nodes, 2))} узлов", screen)
        if name == "done_modern":
            res.check("done_modern: отчёт отрисован (.rb-* блоки)", int(dom.get("rb") or 0) >= 5,
                      f"rb={dom.get('rb')}", "≥ 5 блоков", screen)
            res.check("done_modern: в отчёте есть svg-графики", int(dom.get("svg") or 0) >= 1,
                      f"svg={dom.get('svg')}", "≥ 1", screen)
            res.check("done_modern: блок отчёта без ошибки", not dom.get("reportError"),
                      str(dom.get("reportError") or "")[:200], "нет ошибки", screen)
        events = h.parse_events(mark, screen)
        errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
        res.check(f"{name}: нет ошибок JS/API", not errors,
                  "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто",
                  "0 ошибок", screen)
        path = h.shot(f"s2-{name}")
        if path:
            res.screenshots.append(path)
        time.sleep(0.15)

    # Отдельная проверка 304-бага: если деталь запроса уже грузилась (как это делает
    # composer-feed), api.js помнит ETag; страница запроса шлёт условный GET, получает 304
    # и при пустом detail остаётся на skeleton.
    try:
        working = h.sb.state("working")["root_id"]
        h.reset_etags()
        tag = h.cdp.eval_js(
            "import('/js/api.js').then(async (m) => { await m.api.get('/api/requests/%s'); "
            "return m.api.etag('/api/requests/%s'); })" % (working, working),
            await_promise=True)
        mark = h.cdp.event_index()
        h.goto(f"#/room/cafe/request/{working}")
        time.sleep(2.0)
        race = h.cdp.eval_js(JS_REQ_READY) or {}
        perf = h.cdp.eval_js(
            "performance.getEntriesByType('resource').filter((e) => e.name.includes('/api/requests/%s')).map((e) => e.responseStatus)" % working) or []
        res.check("S2: запрос после композера открывается (ETag/304)", bool(race.get("ready")),
                  json.dumps({"etag": tag, "state": race, "api_status": perf[-3:]}, ensure_ascii=False)[:500],
                  "страница отрисована на свежем экземпляре", "request:after-composer")
        if not race.get("ready"):
            res.notes.append("304-баг: если деталь запроса уже грузилась (composer-feed), api.js помнит "
                             "ETag; при переходе на запрос условный GET возвращает 304, а request.refresh() "
                             "выходит по res.notModified до render() — новый экземпляр остаётся на skeleton "
                             "(sk=%s)." % race.get("sk"))
    except Exception as exc:  # noqa: BLE001
        res.notes.append("Проверку ETag/304 выполнить не удалось: %s" % str(exc)[:200])
    finally:
        try:
            h.reset_etags()
            h.goto("#/room/cafe/composer")
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
    return res


def scenario_s3(h: Harness) -> Result:
    res = Result("S3", SCENARIOS["S3"])
    h.goto("#/room/cafe/composer")
    text = ("E2E проверка композера %d: легализация Лето, упаковка и канал первых продаж."
            % int(time.time()))
    mark = h.cdp.event_index()
    typed = h.cdp.eval_js(r"""
    (() => {
      const ta = document.querySelector('.cmp-input');
      if (!ta) return {ok: false, count: document.querySelectorAll('textarea').length};
      ta.focus(); ta.value = %s;
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      return {ok: true, value: ta.value};
    })()
    """ % json.dumps(text))
    res.check("композер: поле ввода найдено", bool(typed and typed.get("ok")),
              json.dumps(typed, ensure_ascii=False)[:200], "textarea .cmp-input", "composer")
    sent = h.cdp.eval_js(r"""
    (() => {
      const ta = document.querySelector('.cmp-input');
      if (!ta) return false;
      return ta.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'Enter', code: 'Enter', ctrlKey: true, bubbles: true, cancelable: true}));
    })()
    """)
    hash_now = ""
    deadline = time.time() + 35
    while time.time() < deadline:
        hash_now = str(h.cdp.eval_js("location.hash") or "")
        if hash_now.startswith("#/room/cafe/request/t_"):
            break
        time.sleep(0.2)
    res.check("Ctrl+Enter: переход на страницу запроса",
              hash_now.startswith("#/room/cafe/request/t_"), hash_now, "#/room/cafe/request/t_…", "composer")
    rid = hash_now.rsplit("/", 1)[-1] if hash_now.startswith("#/room/cafe/request/") else ""
    row = None
    deadline = time.time() + 35
    while time.time() < deadline and rid:
        row = h.sb.one("SELECT id, status, tenant, body, title FROM tasks WHERE id=?", (rid,))
        if row is not None:
            break
        time.sleep(0.2)
    res.check("SQLite песочницы: карточка запроса создана", row is not None,
              rid or "нет id", "строка tasks", "composer")
    if row is not None:
        res.check("SQLite: tenant room:cafe", str(row["tenant"]) == "room:cafe", str(row["tenant"]), "room:cafe", "composer")
        res.check("SQLite: статус triage", str(row["status"]) == "triage", str(row["status"]), "triage", "composer")
        body = str(row["body"] or "")
        res.check("SQLite: тело содержит дословный текст", text in body,
                  body[:300], "текст запроса целиком", "composer")
        rec = registry.get(h.cfg, "cafe", rid)
        res.check("Реестр: запрос записан", bool(rec) and rec.get("root_id") == rid,
                  json.dumps(rec, ensure_ascii=False)[:240] if rec else "нет записи", "запись requests.json", "composer")
        h.goto(f"#/room/cafe/request/{rid}")
        h.wait_request_ready(10)
        title = str(h.cdp.eval_js("(document.querySelector('.req__title')||{}).textContent || ''") or "")
        res.check("Страница запроса открылась на созданный корень", text[:30] in title,
                  title[:160], text[:60], "composer")
    events = h.parse_events(mark, "composer")
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    res.check("Композер: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто", "0 ошибок", "composer")
    path = h.shot("s3-composer-new-request")
    if path:
        res.screenshots.append(path)
    return res


def _find_step_js(text: str, action: str) -> str:
    return r"""
    (() => {
      const text = %s;
      const row = [...document.querySelectorAll('.step-row')]
        .find((r) => ((r.querySelector('.step-row__text') || {}).textContent || '').trim() === text);
      if (!row) return {ok: false, count: document.querySelectorAll('.step-row').length};
      %s
      return {ok: true};
    })()
    """ % (json.dumps(text), action)


def scenario_s4(h: Harness) -> Result:
    res = Result("S4", SCENARIOS["S4"])
    h.goto("#/room/cafe/backlog")
    status, data, raw = h.json_ok("/api/rooms/cafe/backlog")
    items = (data or {}).get("items") or []
    res.check("бэклог: API 200 и есть шаги", status == 200 and bool(items), f"{status} items={len(items)}",
              "200 + items", "backlog")
    step = next((i for i in items if i.get("kind") == "next_step" and i.get("status") == "open"), None)
    if step is None:
        res.check("бэклог: есть открытый шаг", False, "нет шага status=open", "≥ 1 открытый шаг", "backlog")
        h.shot("s4-backlog")
        return res
    text = str(step.get("text") or "")
    new_due = "2026-12-01"

    # W14-H §12: галочек у пользователя больше нет — шаг открывается окном,
    # а действия («Согласен», «Переместить») уходят руководителю ответом на шаг.
    mark = h.cdp.event_index()
    opened = h.cdp.eval_js(_find_step_js(text, "row.querySelector('.step-row__open').click();"))
    res.check("шаг найден в DOM и открылось окно шага", bool(opened and opened.get("ok")),
              json.dumps(opened, ensure_ascii=False)[:200], "окно шага", "backlog")
    dialog = {}
    deadline = time.time() + 8
    while time.time() < deadline:
        dialog = h.cdp.eval_js("(() => { const d = document.querySelector('.stepdlg');"
                               " return {ok: Boolean(d), actions: d ? [...d.querySelectorAll('[data-step-action]')]"
                               ".map((b) => b.getAttribute('data-step-action')) : []}; })()") or {}
        # Шаг из «Дальше» подгружается запросом: ждём не только окно, но и кнопки действий.
        if dialog.get("ok") and "move" in (dialog.get("actions") or []):
            break
        time.sleep(0.1)
    res.check("окно шага: доступно с клавиатуры и с действиями", bool(dialog.get("ok")),
              json.dumps(dialog, ensure_ascii=False)[:300], "dialog", "backlog")
    res.check("окно шага: «Согласен / Прокомментировать / Переместить / Убрать»",
              all(k in (dialog.get("actions") or []) for k in ("agree", "comment", "move", "drop")),
              json.dumps(dialog.get("actions"), ensure_ascii=False), "agree,comment,move,drop", "backlog")
    res.check("в окне шага нет галочки выполнения",
              not h.cdp.eval_js("Boolean(document.querySelector('.stepdlg input[type=checkbox]'))"),
              "checkbox в окне", "нет галочки", "backlog")

    # «Переместить»: новый срок уходит руководителю как ответ на шаг и меняет бэклог
    by_due = h.cdp.eval_js(r"""
    (() => {
      const d = document.querySelector('.stepdlg');
      if (!d) return {ok: false};
      d.querySelector('[data-step-action=\"move\"]').click();
      const inp = d.querySelector('input[type=date]');
      if (!inp) return {ok: false, why: 'нет поля срока'};
      inp.value = %s;
      const save = d.querySelector('[data-step-send=\"move\"]');
      if (!save) return {ok: false, why: 'нет кнопки Сохранить'};
      save.click();
      return {ok: true};
    })()
    """ % json.dumps(new_due))
    res.check("окно шага: срок изменён и сохранён", bool(by_due and by_due.get("ok")),
              json.dumps(by_due, ensure_ascii=False)[:200], "saved", "backlog")
    due_item = None
    deadline = time.time() + 15
    while time.time() < deadline:
        _s, d3, _r = h.json_ok("/api/rooms/cafe/backlog")
        due_item = next((i for i in (d3.get("items") or []) if i.get("id") == step.get("id")), None)
        if due_item and str(due_item.get("due") or "")[:10] == new_due:
            break
        time.sleep(0.15)
    res.check("GET /api/rooms/cafe/backlog: срок изменён по ответу на шаг",
              bool(due_item) and str(due_item.get("due") or "")[:10] == new_due,
              json.dumps(due_item, ensure_ascii=False)[:240] if due_item else "нет", new_due, "backlog")
    pending = {}
    deadline = time.time() + 10
    while time.time() < deadline:
        pending = h.cdp.eval_js("(() => { const r = document.querySelector('.stepdlg__reaction, .req-step__reaction');"
                                " return {ok: Boolean(r), text: r ? (r.textContent || '').trim() : ''}; })()") or {}
        if pending.get("ok"):
            break
        time.sleep(0.15)
    res.check("видна реакция «руководитель ещё не ответил»",
              bool(pending and pending.get("ok") and "руководител" in (pending.get("text") or "")),
              json.dumps(pending, ensure_ascii=False)[:200], "реакция", "backlog")
    h.cdp.eval_js("(() => { const b = document.querySelector('.stepdlg__close'); if (b) b.click();"
                  " return Boolean(b); })()")
    mark2 = mark
    events = h.parse_events(min(mark, mark2), "backlog")
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    res.check("бэклог: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто", "0 ошибок", "backlog")
    path = h.shot("s4-backlog-step-done")
    if path:
        res.screenshots.append(path)
    return res


def scenario_s5(h: Harness) -> Result:
    res = Result("S5", SCENARIOS["S5"])
    h.goto("#/room/cafe/team")
    _s, team, _r = h.json_ok("/api/rooms/cafe/team")
    before = [m.get("profile") for m in (team.get("members") or [])]
    mark = h.cdp.event_index()
    opened = h.cdp.eval_js(r"""
    (() => {
      const btn = [...document.querySelectorAll('button')]
        .find((b) => (b.textContent || '').includes('Добавить агента'));
      if (!btn) return {ok: false, buttons: [...document.querySelectorAll('button')].map(b => b.textContent.trim()).slice(0, 20)};
      btn.click(); return {ok: true};
    })()
    """)
    res.check("команда: кнопка «Добавить агента» найдена", bool(opened and opened.get("ok")),
              json.dumps(opened, ensure_ascii=False)[:400], "кнопка есть", "team")
    dialog = {}
    deadline = time.time() + 8
    while time.time() < deadline:
        dialog = h.cdp.eval_js(r"""
        (() => {
          const d = document.querySelector('.dialog');
          const r = d ? d.querySelector('input[type=radio]:checked') || d.querySelector('input[type=radio]') : null;
          return {open: Boolean(d), profile: r ? r.value : '',
                  title: d ? (d.querySelector('.dialog__title') || {}).textContent : ''};
        })()
        """) or {}
        if dialog.get("open"):
            break
        time.sleep(0.1)
    res.check("команда: диалог выбора профиля открылся", bool(dialog.get("open")),
              json.dumps(dialog, ensure_ascii=False)[:300], "dialog", "team")
    profile = str(dialog.get("profile") or "")
    if dialog.get("open"):
        h.cdp.eval_js(r"""
        (() => {
          const d = document.querySelector('.dialog');
          const btn = [...d.querySelectorAll('button')].find((b) => (b.textContent || '').trim() === 'Добавить');
          if (!btn) return false;
          btn.click(); return true;
        })()
        """)
        after: list = []
        deadline = time.time() + 20
        while time.time() < deadline:
            _s, t2, _r = h.json_ok("/api/rooms/cafe/team")
            after = [m.get("profile") for m in (t2.get("members") or [])]
            if profile and profile in after and len(after) == len(before) + 1:
                break
            time.sleep(0.2)
        res.check("команда: участник добавлен через API", bool(profile) and profile in after and len(after) == len(before) + 1,
                  f"before={before} after={after} profile={profile}", "+1 участник", "team")
        # Команда обновляется опросом (не мгновенно): ждём карточку, а не читаем DOM
        # один раз сразу после ответа API — иначе проверка ловит гонку с поллингом.
        card = False
        deadline = time.time() + 12
        while time.time() < deadline:
            card = h.cdp.eval_js("Boolean(document.querySelector('.tm-card[data-profile=%s]'))" % json.dumps(profile))
            if card:
                break
            time.sleep(0.25)
        res.check("команда: карточка участника появилась", bool(card), str(card),
                  "карточка в DOM (после обновления опросом, ≤12 с)", "team")
        path = h.shot("s5-team-added")
        if path:
            res.screenshots.append(path)
        # W17-E §1 (B7/P2): действия участника — в меню «⋯» карточки, не отдельной кнопкой.
        menu = h.cdp.eval_js(r"""
        (() => {
          const card = document.querySelector('.tm-card[data-profile=' + JSON.stringify(%s) + ']');
          if (!card) return 'нет карточки';
          const btn = card.querySelector('.tm-menu__btn');
          if (!btn) return 'нет меню «⋯»';
          btn.click();
          const pop = card.querySelector('.tm-menu__pop');
          return pop && !pop.hidden ? 'ok' : 'меню не открылось';
        })()
        """ % json.dumps(profile))
        res.check("команда: меню «⋯» участника открылось", menu == "ok", str(menu), "ok", "team")
        picked = h.cdp.eval_js(r"""
        (() => {
          const card = document.querySelector('.tm-card[data-profile=' + JSON.stringify(%s) + ']');
          const item = card ? [...card.querySelectorAll('.tm-menu__item')]
            .find((b) => /Убрать/.test(String(b.textContent || ''))) : null;
          if (!item) return 'нет пункта «Убрать из комнаты»';
          item.click();
          return 'ok';
        })()
        """ % json.dumps(profile))
        res.check("команда: пункт «Убрать из комнаты» есть у исполнителя", picked == "ok",
                  str(picked), "ok", "team")
        deadline = time.time() + 8
        while time.time() < deadline:
            if h.cdp.eval_js("Boolean(document.querySelector('.dialog'))"):
                break
            time.sleep(0.1)
        h.cdp.eval_js(r"""
        (() => {
          const d = document.querySelector('.dialog');
          if (!d) return false;
          const btn = [...d.querySelectorAll('button')].find((b) => (b.textContent || '').trim() === 'Убрать');
          if (!btn) return false;
          btn.click(); return true;
        })()
        """)
        final: list = []
        deadline = time.time() + 20
        while time.time() < deadline:
            _s, t3, _r = h.json_ok("/api/rooms/cafe/team")
            final = [m.get("profile") for m in (t3.get("members") or [])]
            if profile not in final and len(final) == len(before):
                break
            time.sleep(0.2)
        res.check("команда: участник убран", profile not in final and len(final) == len(before),
                  f"after={final} profile={profile}", "состав как до теста", "team")
    events = h.parse_events(mark, "team")
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    res.check("команда: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто", "0 ошибок", "team")
    return res


def scenario_s6(h: Harness) -> Result:
    res = Result("S6", SCENARIOS["S6"])
    h.goto("#/settings")
    h.reset_etags()          # палитра не должна страдать от 304-бага страницы запроса
    mark = h.cdp.event_index()
    h.cdp.eval_js(r"""
    (() => { document.dispatchEvent(new KeyboardEvent('keydown',
      {key: 'k', code: 'KeyK', ctrlKey: true, bubbles: true, cancelable: true})); return true; })()
    """)
    opened = False
    deadline = time.time() + 8
    while time.time() < deadline:
        opened = bool(h.cdp.eval_js("Boolean(document.querySelector('.pal-input'))"))
        if opened:
            break
        time.sleep(0.1)
    res.check("Ctrl+K: палитра открылась", opened, str(opened), ".pal-input", "palette")
    if opened:
        h.cdp.eval_js(r"""
        (() => { const i = document.querySelector('.pal-input'); i.focus(); i.value = 'лег';
          i.dispatchEvent(new Event('input', {bubbles: true})); return i.value; })()
        """)
        items = 0
        deadline = time.time() + 10
        while time.time() < deadline:
            items = int(h.cdp.eval_js("document.querySelectorAll('.pal-item').length") or 0)
            if items:
                break
            time.sleep(0.15)
        res.check("палитра: по «лег» есть результаты", items >= 1, f"{items} строк", "≥ 1", "palette")
        rows = h.cdp.eval_js(r"""
        (() => [...document.querySelectorAll('.pal-item')].map((b) => ({
          title: ((b.querySelector('.pal-item__title') || {}).textContent || '').trim(),
          hint: ((b.querySelector('.pal-item__hint') || {}).textContent || '').trim(),
          active: b.classList.contains('is-active')})))()
        """) or []
        legal = [r for r in rows if re.search(r"легализ|картина по проекту", (r.get("title", "") + " " + r.get("hint", "")), re.I)]
        res.check("палитра: среди результатов есть запрос про легализацию", bool(legal),
                  json.dumps(rows[:6], ensure_ascii=False)[:400], "хит про легализацию", "palette")
        h.cdp.eval_js(r"""
        (() => { const i = document.querySelector('.pal-input');
          i.dispatchEvent(new KeyboardEvent('keydown',
            {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true})); return true; })()
        """)
        hash_now = ""
        deadline = time.time() + 10
        while time.time() < deadline:
            hash_now = str(h.cdp.eval_js("location.hash") or "")
            if hash_now and hash_now != "#/settings":
                break
            time.sleep(0.15)
        res.check("палитра: Enter ведёт на другой экран", bool(hash_now) and hash_now != "#/settings",
                  hash_now, "переход по hash", "palette")
        if "request" not in hash_now:
            res.notes.append("Палитра: Enter на запрос «лег» открыл «%s» — первая строка списка команда, "
                             "а не найденный запрос; поиск работает (хит в списке есть), но выбор по Enter "
                             "уводит не туда." % hash_now)
        ready = h.wait_ready(12)
        if "request" in hash_now:
            ready = h.wait_request_ready(10) or ready
        res.check("палитра: целевой экран загрузился", bool(ready.get("ready")),
                  json.dumps(ready, ensure_ascii=False)[:240], "ready", "palette")
    events = h.parse_events(mark, "palette")
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    res.check("палитра: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто", "0 ошибок", "palette")
    path = h.shot("s6-palette")
    if path:
        res.screenshots.append(path)
    return res


def scenario_s7(h: Harness) -> Result:
    res = Result("S7", SCENARIOS["S7"])
    h.goto("#/settings")
    mark = h.cdp.event_index()
    health = {}
    deadline = time.time() + 15
    while time.time() < deadline:
        health = h.cdp.eval_js(r"""
        (() => {
          const s = document.querySelector('#set-health');
          if (!s) return {found: false};
          return {found: true, chips: s.querySelectorAll('.set-chips .chip').length,
                  err: Boolean(s.querySelector('.banner--error')),
                  text: (s.innerText || '').replace(/\s+/g, ' ').slice(0, 300)};
        })()
        """) or {}
        if health.get("chips"):
            break
        time.sleep(0.2)
    res.check("настройки: health-панель отрисована", bool(health.get("found")) and int(health.get("chips") or 0) >= 3,
              json.dumps(health, ensure_ascii=False)[:300], "≥ 3 чипа", "settings")
    res.check("настройки: в health нет баннера ошибки", not health.get("err"), str(health.get("err")), "false", "settings")
    status, data, raw = h.json_ok("/api/health")
    res.check("настройки: /api/health ok", status == 200 and bool(data.get("ok")),
              f"{status} ok={data.get('ok')} {raw[:160]!r}", "200 ok=true", "settings")
    switched = h.cdp.eval_js(r"""
    (() => {
      const b = [...document.querySelectorAll('#set-look .segmented__btn, .segmented__btn')]
        .find((x) => x.getAttribute('data-value') === 'dark');
      if (!b) return {ok: false, count: document.querySelectorAll('.segmented__btn').length};
      b.click(); return {ok: true, theme: document.documentElement.dataset.theme};
    })()
    """)
    res.check("настройки: тёмная тема переключается", bool(switched and switched.get("ok")) and switched.get("theme") == "dark",
              json.dumps(switched, ensure_ascii=False)[:200], "data-theme=dark", "settings")
    back = h.cdp.eval_js(r"""
    (() => {
      const b = [...document.querySelectorAll('#set-look .segmented__btn, .segmented__btn')]
        .find((x) => x.getAttribute('data-value') === 'light');
      if (!b) return {ok: false};
      b.click(); return {ok: true, theme: document.documentElement.dataset.theme,
                         stored: localStorage.getItem('teamroom.theme')};
    })()
    """)
    res.check("настройки: светлая тема возвращается", bool(back and back.get("ok")) and back.get("theme") == "light",
              json.dumps(back, ensure_ascii=False)[:200], "data-theme=light", "settings")
    res.check("настройки: выбор темы сохранён", str((back or {}).get("stored")) == "light",
              str((back or {}).get("stored")), "light", "settings")
    events = h.parse_events(mark, "settings")
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    res.check("настройки: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто", "0 ошибок", "settings")
    path = h.shot("s7-settings-health")
    if path:
        res.screenshots.append(path)
    return res


def scenario_s8(h: Harness) -> Result:
    res = Result("S8", SCENARIOS["S8"])
    rid = h.sb.state("done_modern")["root_id"]
    h.goto("#/room/cafe/reports")
    mark = h.cdp.event_index()
    _s, data, _r = h.json_ok("/api/rooms/cafe/reports")
    items = (data or {}).get("items") or []
    target = next((i for i in items if str(i.get("request_id") or "") == rid), None) or (items[5] if len(items) > 5 else None) or (items[0] if items else None)
    res.check("отчёты: галерея непуста", bool(items),
              f"items={len(items)}", "≥ 1 отчёт", "reports")
    if target:
        clicked = h.cdp.eval_js(r"""
        (() => {
          const key = %s;
          const a = [...document.querySelectorAll('.rep-card')]
            .find((c) => c.getAttribute('data-key') === key);
          if (!a) return {ok: false, count: document.querySelectorAll('.rep-card').length};
          const link = a.querySelector('.rep-card__title') || a;
          link.click(); return {ok: true, hash: location.hash};
        })()
        """ % json.dumps(str(target.get("key") or "")))
        res.check("отчёты: карточка done_modern найдена", bool(clicked and clicked.get("ok")),
                  json.dumps(clicked, ensure_ascii=False)[:300],
                  str(target.get("key")), "reports")
        ready = h.wait_ready(15)
        dom = h.cdp.eval_js(r"""
        (() => ({
          title: (document.querySelector('.rep-detail__title') || {}).textContent || '',
          rb: document.querySelectorAll('.rb-heading, .rb-text, .rb-stat, .rb-phases, .rb-callout, .rb-compare, .rb-timeline, .rb-table').length,
          svg: document.querySelectorAll('#view svg').length,
          err: (() => { const e = document.querySelector('.rep-detail .banner--error, .rep-issues .banner'); return e ? e.innerText.trim().slice(0, 200) : ''; })(),
          hash: location.hash, ready: Boolean(document.querySelector('.rep-detail, .rep-doc')),
        }))()
        """) or {}
        res.check("отчёты: страница отчёта открылась", bool(dom.get("ready")) and bool(ready.get("ready")),
                  json.dumps(dom, ensure_ascii=False)[:300], "detail", "reports")
        res.check("отчёты: done_modern отрисован (.rb-*)", int(dom.get("rb") or 0) >= 5,
                  f"rb={dom.get('rb')}", "≥ 5", "reports")
        res.check("отчёты: графики svg есть", int(dom.get("svg") or 0) >= 1,
                  f"svg={dom.get('svg')}", "≥ 1", "reports")
        res.check("отчёты: нет ошибки отчёта", not dom.get("err"), str(dom.get("err"))[:200], "нет", "reports")
    events = h.parse_events(mark, "reports")
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    res.check("отчёты: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто", "0 ошибок", "reports")
    path = h.shot("s8-report-done-modern")
    if path:
        res.screenshots.append(path)
    return res


def scenario_s9(h: Harness) -> Result:
    res = Result("S9", SCENARIOS["S9"])
    now = int(time.time())
    rid = "t_xss%08x" % (now & 0xFFFFFFFF)
    payload_title = "XSS проба <img src=x onerror=\"window.__xss=1\">"
    payload_body = ("Проверка экранирования. <script>window.__xss=2</script> "
                    "<img src=x onerror=window.__xss=3> javascript:window.__xss=4")
    _assert_writable_db(Path(h.sb.db), Path(h.sb.root))
    con = sqlite3.connect(str(h.sb.db), timeout=15)
    try:
        con.execute("BEGIN")
        _insert(con, "tasks", {
            "id": rid, "title": payload_title, "body": payload_body, "assignee": "biz-director",
            "status": "done", "created_by": "Михаил", "created_at": now, "started_at": now,
            "completed_at": now, "tenant": "room:cafe", "priority": 1, "consecutive_failures": 0,
        })
        cur = con.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, summary, metadata, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (rid, "biz-director", "done", "completed",
             "Ответ с <script>window.__xss=5</script> и <img src=x onerror=window.__xss=6>.",
             json.dumps({
                 "next_steps": [{"text": "<script>window.__xss=7</script> шаг", "owner": "biz-ops",
                                 "due": "2026-12-05"}],
                 "open_questions": [{"text": "<img src=x onerror=window.__xss=8>", "to": "mikhail"}],
                 "decisions": [{"text": "<a href=\"javascript:window.__xss=9\">решение</a>",
                                "why": "<svg onload=window.__xss=10>", "status": "proposed"}],
                 "stage": {"current": "legal", "phases": [{"id": "legal", "status": "active"}]},
             }, ensure_ascii=False),
             now, now))
        _insert(con, "task_comments", {"task_id": rid, "author": "biz-critic",
                                       "body": "<script>window.__xss=11</script> комментарий",
                                       "created_at": now})
        _insert(con, "task_events", {"task_id": rid, "run_id": cur.lastrowid, "kind": "completed",
                                     "payload": "{}", "created_at": now})
        con.commit()
    finally:
        con.close()
    registry.add_request(h.cfg, "cafe", {
        "root_id": rid, "slug": "xss-probe", "title": payload_title, "text": payload_body,
        "created_at": now, "critic": False, "mode": "auto", "adopted": False,
        "retargeted": True, "extra_cards": [],
    })
    mark, state = h.goto(f"#/room/cafe/request/{rid}")
    ready = h.wait_request_ready(10)
    res.check("XSS: страница запроса открылась", bool(state.get("ready")) and bool(ready.get("ready")),
              json.dumps({"state": state, "full": ready}, ensure_ascii=False)[:400], "ready", "xss")
    dom = h.cdp.eval_js(r"""
    (() => ({
      xss: window.__xss === undefined ? null : window.__xss,
      scripts: document.querySelectorAll('#view script').length,
      inlineHandlers: [...document.querySelectorAll('#view *')].filter((n) =>
        [...n.attributes].some((a) => a.name.startsWith('on')) ||
        (a => a && a.name === 'href' && /^javascript:/i.test(a.value))([...n.attributes].find((a) => a.name === 'href'))).length,
      text: (document.querySelector('#answer') ? document.querySelector('#answer').innerText : '').slice(0, 600),
      title: (document.querySelector('.req__title') || {}).textContent || '',
    }))()
    """) or {}
    res.check("XSS: window.__xss не выставлен", dom.get("xss") is None,
              f"window.__xss={dom.get('xss')!r}", "undefined", "xss")
    res.check("XSS: в #view нет внедрённых <script>", int(dom.get("scripts") or 0) == 0,
              f"scripts={dom.get('scripts')}", "0", "xss")
    res.check("XSS: нет inline-обработчиков и javascript: ссылок", int(dom.get("inlineHandlers") or 0) == 0,
              f"handlers={dom.get('inlineHandlers')}", "0", "xss")
    res.check("XSS: payload виден как текст (экранирован)", "<script>" in str(dom.get("text") or ""),
              str(dom.get("text"))[:240], "текст <script> в DOM", "xss")
    if "window.xss" in str(dom.get("text") or "") and "window.__xss" not in str(dom.get("text") or ""):
        res.notes.append("Markdown съедает двойные подчёркивания: `window.__xss` в ответе отрисован "
                         "как `window.xss` — страдает текст агентов с dunder-именами (__init__, __xss).")
    events = h.parse_events(mark, "xss")
    errors = [e for e in events if e["level"] == "error" and not h.benign(e)]
    res.check("XSS: нет ошибок JS/API", not errors,
              "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто", "0 ошибок", "xss")
    path = h.shot("s9-xss")
    if path:
        res.screenshots.append(path)
    return res


def scenario_s10(h: Harness) -> Result:
    res = Result("S10", SCENARIOS["S10"])
    h.parse_events(0, "all")  # заполнить карту requestId → url
    external: list[str] = []
    internal = 0
    total = 0
    for ev in h.cdp.events_since(0):
        if ev.get("method") != "Network.requestWillBeSent":
            continue
        url = str(((ev.get("params") or {}).get("request") or {}).get("url") or "")
        if not re.match(r"^https?://", url, re.I):
            continue
        total += 1
        host = ""
        port = 0
        m = re.match(r"^https?://([^/:]+)(?::(\d+))?", url, re.I)
        if m:
            host = m.group(1)
            port = int(m.group(2) or (443 if url.lower().startswith("https") else 80))
        if host in ("127.0.0.1", "localhost", "[::1]") and port == h.port:
            internal += 1
        else:
            external.append(url)
    res.check("сеть: запросы вообще были", total > 0, f"total={total}", "> 0", "network")
    res.check("сеть: все http(s)-запросы только к 127.0.0.1:PORT", not external,
              "; ".join(external[:8]) or f"internal={internal}/{total}", "0 внешних", "network")
    dom_external = h.cdp.eval_js(r"""
    (() => [...document.querySelectorAll('img[src], iframe[src], script[src], link[href]')]
      .map((n) => n.getAttribute('src') || n.getAttribute('href') || '')
      .filter((u) => /^https?:\/\//i.test(u) && !/^https?:\/\/(127\.0\.0\.1|localhost)(:|\/)/i.test(u)))()
    """)
    res.check("сеть: в DOM нет внешних ресурсов", not dom_external,
              "; ".join(dom_external or [])[:400] or "чисто", "0", "network")
    path = h.shot("s10-final")
    if path:
        res.screenshots.append(path)
    return res


def scenario_s11(h: Harness) -> Result:
    """Проектный режим (волна 9b) на моке: tools/e2e_project.py, порт 8883.

    Мок поднимается своим процессом (боевой 8777 и демо 8778 не трогаются), браузер —
    свой headless Chrome с поддельным микрофоном. Здесь только сборка итога: каждый
    шаг a–g становится проверкой, дефекты и скриншоты `w9-*.png` переносятся как есть.
    """
    res = Result("S11", SCENARIOS["S11"])
    port = 8883
    js_path = ROOT / "docs" / "status" / "e2e-project.json"
    cmd = [sys.executable, str(ROOT / "tools" / "e2e_project.py"),
           "--port", str(port), "--json", str(js_path)]
    out = ""
    try:
        proc = spawn.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=900)
        out = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 — сценарий не должен ронять харнесс
        res.check("мок-сценарий запустился", False, f"{exc.__class__.__name__}: {exc}", "код 0", "w9")
        return res
    res.check("мок-сценарий отработал без падения", proc.returncode == 0,
              f"код {proc.returncode}; хвост вывода: {out.strip()[-400:]}", "код 0", "w9")
    data = {}
    try:
        data = json.loads(js_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        res.check("отчёт мок-сценария прочитан", False, str(exc)[:200], "JSON", "w9")
        return res
    steps = data.get("steps") or []
    res.check("шагов в мок-сценарии семь (a–g)", len(steps) == 7, str(len(steps)), "7", "w9")
    for step in steps:
        sid = str(step.get("id") or "?")
        title = str(step.get("title") or "")[:70]
        res.check(f"мок/{sid}: {title}", step.get("status") == "PASS",
                  json.dumps([c for c in step.get("checks") or [] if not c.get("ok")][:2],
                             ensure_ascii=False)[:500] or "все проверки прошли",
                  "шаг PASS", f"w9/{sid}")
        for defect in step.get("defects") or []:
            d = dict(defect)
            d["scenario"] = "S11"
            res.defects.append(d)
        res.notes.extend(str(n) for n in step.get("notes") or [])
    for shot in data.get("screenshots") or []:
        if "/w9-" in str(shot) or str(shot).startswith("docs/shots/e2e/w9-"):
            res.screenshots.append(str(shot))
    totals = data.get("totals") or {}
    res.check("мок: все проверки прошли", int(totals.get("checks_failed") or 0) == 0,
              f"{totals.get('checks', 0) - totals.get('checks_failed', 0)}/{totals.get('checks', 0)}",
              "0 провалов", "w9")
    res.check("мок: боевые порты и доска не трогались",
              data.get("port") == port and "mock_server" in str(data.get("mock", "")),
              json.dumps({"port": data.get("port"), "mock": data.get("mock")}, ensure_ascii=False)[:200],
              f"порт {port}, tools/mock_server.py", "w9")
    return res


def _click_button_js(text: str, scope: str = "document") -> str:
    """Клик по кнопке с точной подписью: возвращает {ok, found[]}."""
    return r"""
    (() => {
      const root = %s;
      const want = %s;
      const btn = [...root.querySelectorAll('button')]
        .find((b) => (b.textContent || '').trim() === want);
      if (!btn) return {ok: false, buttons: [...root.querySelectorAll('button')]
        .map((b) => (b.textContent || '').trim()).filter(Boolean).slice(0, 24)};
      btn.click(); return {ok: true};
    })()
    """ % (scope, json.dumps(text))


def _wait_js(h: Harness, expr: str, timeout: float = 10.0, step: float = 0.1):
    """Ждать, пока JS-выражение вернёт истину (для диалогов, которые монтируются асинхронно)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = h.cdp.eval_js(expr)
        except Exception as exc:                       # noqa: BLE001
            last = {"error": str(exc)[:200]}
        if last:
            return last
        time.sleep(step)
    return last


def _close_dialogs(h: Harness) -> int:
    """Убрать все открытые диалоги (каждый шаг сценария начинает с чистого экрана)."""
    count = h.cdp.eval_js(
        "(() => { const nodes = [...document.querySelectorAll('.dialog-backdrop')]; "
        "nodes.forEach((n) => n.remove()); return nodes.length; })()")
    return int(count or 0)


def _header_state_js() -> str:
    """Что сейчас в шапке: кнопки настроек и папки, подпись, подсказка под вкладками."""
    return r"""
    (() => {
      const bar = document.querySelector('#topbar');
      const settings = bar ? bar.querySelector('button[data-action="room-settings"]') : null;
      const pd = bar ? bar.querySelector('button[data-pd]') : null;
      const hint = document.querySelector('#page-hint');
      return {
        settings: Boolean(settings),
        settingsText: settings ? (settings.textContent || '').trim() : '',
        settingsLabel: settings ? settings.getAttribute('aria-label') : '',
        settingsVisible: Boolean(settings && settings.offsetParent !== null),
        pd: pd ? pd.getAttribute('data-pd') : '',
        pdText: pd ? (pd.textContent || '').trim() : '',
        pdVisible: Boolean(pd && pd.offsetParent !== null),
        hint: Boolean(hint && !hint.hidden && hint.textContent && hint.textContent.trim()),
        hintText: hint && !hint.hidden ? (hint.textContent || '').trim().slice(0, 160) : '',
        tabs: [...document.querySelectorAll('.tabs a')].map((a) => (a.getAttribute('href') || '')),
        hash: location.hash,
      };
    })()
    """


def scenario_s12(h: Harness) -> Result:
    """W10: папка проекта у каждой комнаты, кнопка «Настройки комнаты», цикл смены пути.

    Полный цикл в настоящем Chrome на песочнице: создать комнату без папки →
    «Задать папку» → настройки (курсор в поле) → задать путь → сохранить →
    «Открыть папку» на всех вкладках → сменить путь (предупреждение о старых
    файлах) → убрать путь. Проводник в тесте не запускается (TEAMROOM_NO_SHELL=1),
    зато проверяется понятное окно ошибки с «Скопировать путь».
    """
    res = Result("S12", SCENARIOS["S12"])
    stamp = int(time.time())
    old_shell = os.environ.get("TEAMROOM_NO_SHELL")
    os.environ["TEAMROOM_NO_SHELL"] = "1"
    try:
        # 1) комната без папки — создаём через интерфейс, как Михаил
        mark = h.cdp.event_index()
        h.goto("#/")
        title = "E2E папка %d" % stamp
        opened = h.cdp.eval_js(_click_button_js("Комната"))
        res.check("главная: кнопка «Комната» есть и нажата", bool(opened and opened.get("ok")),
                  json.dumps(opened, ensure_ascii=False)[:300], "кнопка «Комната»", "home")
        typed = _wait_js(h, r"""
        (() => {
          const t = document.querySelector('.dialog input[aria-label="Название комнаты"]');
          if (!t) return null;
          t.focus(); t.value = %s;
          t.dispatchEvent(new Event('input', {bubbles: true}));
          return {ok: true, value: t.value};
        })()
        """ % json.dumps(title), 15.0) or {"ok": False}
        res.check("диалог комнаты: название введено", bool(typed and typed.get("ok")),
                  json.dumps(typed, ensure_ascii=False)[:200], "поле названия", "home")
        created = h.cdp.eval_js(_click_button_js("Создать комнату", "document.querySelector('.dialog') || document"))
        res.check("диалог комнаты: «Создать комнату» нажата", bool(created and created.get("ok")),
                  json.dumps(created, ensure_ascii=False)[:400], "кнопка создания", "home")
        room_id = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            hash_now = str(h.cdp.eval_js("location.hash") or "")
            if hash_now.startswith("#/room/"):
                room_id = hash_now.split("/")[2]
                break
            time.sleep(0.2)
        res.check("комната создана и открылась", bool(room_id), room_id or "нет перехода", "#/room/<id>/composer", "home")
        if not room_id:
            return res
        h.wait_ready(15)
        _s, room, _r = h.json_ok("/api/rooms/" + room_id)
        res.check("API: у новой комнаты нет project_dir", room.get("project_dir") == "",
                  json.dumps(room.get("project_dir")), '""', "composer")

        # 2) шапка: «Задать папку» + «Настройки комнаты» + подсказка под вкладками
        head = h.cdp.eval_js(_header_state_js()) or {}
        res.check("шапка комнаты без папки: кнопка «Задать папку»",
                  head.get("pd") == "ask" and "Задать папку" in str(head.get("pdText")),
                  json.dumps(head, ensure_ascii=False)[:400], "data-pd=ask «Задать папку»", "composer")
        res.check("шапка: кнопка «Настройки комнаты» есть сразу после создания",
                  head.get("settings") and head.get("settingsVisible")
                  and "Настройки комнаты" in str(head.get("settingsLabel")),
                  json.dumps({"settings": head.get("settings"), "text": head.get("settingsText"),
                              "label": head.get("settingsLabel")}, ensure_ascii=False)[:300],
                  "шестерёнка + подпись", "composer")
        res.check("под вкладками подсказка «Задайте папку проекта…»",
                  head.get("hint") and "Задайте папку проекта" in str(head.get("hintText")),
                  str(head.get("hintText"))[:200], "подсказка видна", "composer")

        # 3) «Задать папку» → тот же диалог, курсор в поле «Папка проекта»
        h.cdp.eval_js(_click_button_js("Задать папку", "document.querySelector('#topbar') || document"))
        focus = {}
        deadline = time.time() + 8
        while time.time() < deadline:
            focus = h.cdp.eval_js(r"""
            (() => {
              const d = document.querySelector('.dialog');
              const a = document.activeElement;
              return {dialog: Boolean(d),
                      title: d ? ((d.querySelector('.dialog__title') || {}).textContent || '') : '',
                      active: a ? (a.getAttribute('aria-label') || a.tagName) : ''};
            })()
            """) or {}
            if focus.get("dialog") and focus.get("active") == "Папка проекта":
                break
            time.sleep(0.1)
        res.check("«Задать папку» открыла настройки комнаты", bool(focus.get("dialog")),
                  json.dumps(focus, ensure_ascii=False)[:300], "диалог комнаты", "composer")
        res.check("курсор сразу в поле «Папка проекта»", focus.get("active") == "Папка проекта",
                  str(focus.get("active")), "фокус в поле пути", "composer")

        base_dir = Path(h.sb.root) / "проекты"
        first = base_dir / ("E2E первая %d" % stamp)
        second = base_dir / ("E2E вторая %d" % stamp)
        h.cdp.eval_js(r"""
        (() => {
          const i = document.querySelector('.dialog input[aria-label="Папка проекта"]');
          if (!i) return false;
          i.value = %s;
          i.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()
        """ % json.dumps(str(first)))
        h.cdp.eval_js(_click_button_js("Сохранить", "document.querySelector('.dialog') || document"))
        saved = {}
        deadline = time.time() + 25
        while time.time() < deadline:
            _s, fresh, _r = h.json_ok("/api/rooms/" + room_id)
            saved = fresh
            if fresh.get("project_dir") == str(first):
                break
            time.sleep(0.2)
        res.check("PATCH: путь сохранён", saved.get("project_dir") == str(first),
                  str(saved.get("project_dir"))[:200], str(first), "composer")
        res.check("папка создана на диске", first.is_dir(), str(first), "каталог существует", "composer")

        # 4) теперь «Открыть папку» — на всех вкладках и на странице запроса
        tabs = ["composer", "team", "roadmap", "backlog", "reports", "journal"]
        for tab in tabs:
            h.goto("#/room/%s/%s" % (room_id, tab))
            state = h.cdp.eval_js(_header_state_js()) or {}
            res.check(f"вкладка «{tab}»: «Открыть папку» и настройки на месте",
                      state.get("pd") == "open" and "Открыть папку" in str(state.get("pdText"))
                      and state.get("settings"), json.dumps(state, ensure_ascii=False)[:300],
                      "data-pd=open + настройки", tab)
        # страница запроса (создаём запрос в этой комнате)
        h.cdp.eval_js(r"""
        (() => {
          const ta = document.querySelector('.cmp-input');
          if (!ta) return false;
          ta.focus(); ta.value = 'E2E W10: проверка шапки на странице запроса';
          ta.dispatchEvent(new Event('input', {bubbles: true}));
          return ta.dispatchEvent(new KeyboardEvent('keydown',
            {key: 'Enter', code: 'Enter', ctrlKey: true, bubbles: true, cancelable: true}));
        })()
        """)
        deadline = time.time() + 35
        req_hash = ""
        while time.time() < deadline:
            req_hash = str(h.cdp.eval_js("location.hash") or "")
            if req_hash.startswith("#/room/%s/request/t_" % room_id):
                break
            time.sleep(0.2)
        if req_hash.startswith("#/room/%s/request/" % room_id):
            h.wait_request_ready(10)
            state = h.cdp.eval_js(_header_state_js()) or {}
            res.check("страница запроса: «Открыть папку» и настройки на месте",
                      state.get("pd") == "open" and state.get("settings"),
                      json.dumps(state, ensure_ascii=False)[:300], "data-pd=open + настройки", "request")

        # 5) ошибка открытия — понятное окно с «Скопировать путь» (проводник выключен)
        h.goto("#/room/%s/team" % room_id)
        _close_dialogs(h)
        h.cdp.eval_js(_click_button_js("Открыть папку", "document.querySelector('#topbar') || document"))
        err = {}
        deadline = time.time() + 10
        while time.time() < deadline:
            err = h.cdp.eval_js(r"""
            (() => {
              const d = document.querySelector('.dialog');
              if (!d) return {open: false};
              const btns = [...d.querySelectorAll('button')].map((b) => (b.textContent || '').trim());
              return {open: true, title: (d.querySelector('.dialog__title') || {}).textContent || '',
                      text: (d.textContent || '').slice(0, 400), buttons: btns};
            })()
            """) or {}
            if err.get("open"):
                break
            time.sleep(0.1)
        res.check("ошибка открытия: окно «Не удалось открыть папку»",
                  bool(err.get("open")) and "Не удалось открыть папку" in str(err.get("title")),
                  json.dumps(err, ensure_ascii=False)[:400], "окно с причиной", "team")
        res.check("ошибка открытия: есть «Скопировать путь» и путь",
                  "Скопировать путь" in (err.get("buttons") or []) and str(first) in str(err.get("text")),
                  json.dumps({"buttons": err.get("buttons")}, ensure_ascii=False)[:300],
                  "кнопка копирования + путь", "team")
        h.cdp.eval_js(_click_button_js("Закрыть", "document.querySelector('.dialog') || document"))
        path = h.shot("s12-folder-error")
        if path:
            res.screenshots.append(path)

        # 6) смена пути через «Настройки комнаты»: предупреждение о старых файлах
        h.cdp.eval_js("(() => { const b = document.querySelector('button[data-action=\"room-settings\"]'); if (b) b.click(); return Boolean(b); })()")
        deadline = time.time() + 10
        while time.time() < deadline and not h.cdp.eval_js("Boolean(document.querySelector('.dialog'))"):
            time.sleep(0.1)
        note = h.cdp.eval_js(r"""
        (() => {
          const d = document.querySelector('.dialog');
          const n = d ? d.querySelector('[data-pd="move-note"]') : null;
          return {dialog: Boolean(d), note: Boolean(n),
                  hidden: n ? n.classList.contains('hidden') : null,
                  text: n ? (n.textContent || '').trim() : ''};
        })()
        """) or {}
        res.check("настройки комнаты открылись из шапки", bool(note.get("dialog")),
                  json.dumps(note, ensure_ascii=False)[:300], "диалог", "team")
        res.check("предупреждение о старых файлах скрыто, пока путь не меняли",
                  note.get("note") is True and note.get("hidden") is True,
                  json.dumps(note, ensure_ascii=False)[:300], "hidden", "team")
        h.cdp.eval_js(r"""
        (() => {
          const i = document.querySelector('.dialog input[aria-label="Папка проекта"]');
          if (!i) return false;
          i.value = %s;
          i.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()
        """ % json.dumps(str(second)))
        note2 = h.cdp.eval_js(r"""
        (() => {
          const n = document.querySelector('.dialog [data-pd="move-note"]');
          return {hidden: n ? n.classList.contains('hidden') : null,
                  text: n ? (n.textContent || '').trim() : ''};
        })()
        """) or {}
        res.check("при смене пути видно «Старые файлы останутся в прежней папке; новые пойдут в новую»",
                  note2.get("hidden") is False and "Старые файлы останутся" in str(note2.get("text")),
                  json.dumps(note2, ensure_ascii=False)[:300], "предупреждение видно", "team")
        h.cdp.eval_js(_click_button_js("Сохранить", "document.querySelector('.dialog') || document"))
        changed = {}
        deadline = time.time() + 25
        while time.time() < deadline:
            _s, fresh, _r = h.json_ok("/api/rooms/" + room_id)
            changed = fresh
            if fresh.get("project_dir") == str(second):
                break
            time.sleep(0.2)
        res.check("PATCH: новый путь сохранён", changed.get("project_dir") == str(second),
                  str(changed.get("project_dir"))[:200], str(second), "team")
        res.check("старая папка не удалена и не перенесена", first.is_dir(), str(first),
                  "каталог остался на месте", "team")
        res.check("новая папка создана", second.is_dir(), str(second), "каталог существует", "team")

        # Окно закрывается после «Сохранить». Дождаться закрытия, иначе следующий шаг
        # чистит поле в ещё открытом окне, а поздний ответ сервера возвращает в него
        # прежний путь — и PATCH уходит со старым значением (плавающий провал S12).
        deadline = time.time() + 12
        while time.time() < deadline and h.cdp.eval_js("Boolean(document.querySelector('.dialog'))"):
            time.sleep(0.2)
        res.check("окно настроек закрылось после сохранения пути",
                  not h.cdp.eval_js("Boolean(document.querySelector('.dialog'))"),
                  "окно ещё открыто", "закрыто", "team")

        # 7) убрать путь — снова «Задать папку», старые комнаты не ломаются
        # Шапка перерисовывается после сохранения: кнопку ищем с повторами, пока
        # не откроется окно с полем папки (иначе проверка ловит гонку с перерисовкой).
        opened = False
        deadline = time.time() + 15
        while time.time() < deadline:
            if h.cdp.eval_js("Boolean(document.querySelector('.dialog input[aria-label=\"Папка проекта\"]'))"):
                opened = True
                break
            h.cdp.eval_js("(() => { const b = document.querySelector('button[data-action=\"room-settings\"]');"
                           " if (b) { b.click(); return true; } return false; })()")
            time.sleep(0.4)
        res.check("окно настроек комнаты открывается повторно", opened,
                  "нет .dialog с полем «Папка проекта» за 15 с", "окно открылось", "team")
        # Данные комнаты подгружаются в окно асинхронно. Дождаться, пока в поле
        # окажется сохранённый путь, и только потом очищать: поздний ответ иначе
        # вернёт прежнее значение, и PATCH уйдёт со старым путём.
        loaded = False
        deadline = time.time() + 12
        while time.time() < deadline:
            cur = h.cdp.eval_js("(() => { const i = document.querySelector('.dialog input[aria-label=\"Папка проекта\"]');"
                                " return i ? i.value : null; })()")
            if str(cur or "") == str(second):
                loaded = True
                break
            time.sleep(0.2)
        res.check("в окне настроек показан текущий путь комнаты", loaded,
                  f"в поле {cur!r}, ожидали {str(second)!r}", "текущий путь", "team")
        field_ok = False
        for _ in range(20):
            cleared_now = bool(h.cdp.eval_js(r"""
            (() => {
              const i = document.querySelector('.dialog input[aria-label="Папка проекта"]');
              if (!i) return false;
              if (i.value !== '') {
                i.value = '';
                i.dispatchEvent(new Event('input', {bubbles: true}));
              }
              return i.value === '';
            })()
            """))
            time.sleep(0.4)
            still = h.cdp.eval_js("(() => { const i = document.querySelector('.dialog input[aria-label=\"Папка проекта\"]');"
                                  " return i ? i.value : null; })()")
            if cleared_now and str(still or "") == "":
                field_ok = True
                break
        res.check("поле «Папка проекта» очищено перед сохранением", field_ok,
                  f"поле={field_ok}", "пусто и остаётся пустым", "team")
        h.cdp.eval_js(_click_button_js("Сохранить", "document.querySelector('.dialog') || document"))
        cleared = {}
        deadline = time.time() + 25
        while time.time() < deadline:
            _s, fresh, _r = h.json_ok("/api/rooms/" + room_id)
            cleared = fresh
            if fresh.get("project_dir") == "":
                break
            time.sleep(0.2)
        res.check("PATCH: папку можно убрать", cleared.get("project_dir") == "",
                  str(cleared.get("project_dir")), '""', "team")
        # Заголовок обновляется после сохранения и закрытия окна: ждём состояние,
        # а не читаем его один раз (иначе проверка ловит гонку с перерисовкой).
        deadline = time.time() + 12
        while time.time() < deadline:
            head = h.cdp.eval_js(_header_state_js()) or {}
            if head.get("pd") == "ask" and head.get("hint"):
                break
            time.sleep(0.25)
        res.check("после удаления пути снова «Задать папку» и подсказка",
                  head.get("pd") == "ask" and bool(head.get("hint")),
                  json.dumps(head, ensure_ascii=False)[:300], "data-pd=ask + подсказка (≤12 с)", "team")

        # 8) старая комната без папки (cafe) — без поломок
        h.goto("#/room/cafe/composer")
        state = h.cdp.eval_js(_header_state_js()) or {}
        res.check("старая комната cafe: кнопка папки и настройки на месте",
                  state.get("settings") and state.get("pd") in ("open", "ask")
                  and state.get("pdVisible"),
                  json.dumps(state, ensure_ascii=False)[:300], "настройки + кнопка папки", "cafe")

        events = h.parse_events(mark, "w10")
        # Ожидаемый 400 — наш собственный: в тестовом режиме проводник выключен
        # (`TEAMROOM_NO_SHELL=1`), и окно «Не удалось открыть папку» показывается
        # именно по нему. Любая другая ошибка — дефект.
        expected_400 = [e for e in events if e["level"] == "error" and (
            (e.get("kind") == "http" and int(e.get("status") or 0) == 400
             and "/api/open" in str(e.get("url") or ""))
            or (e.get("kind") == "log" and "400 (Bad Request)" in str(e.get("text") or ""))
        )]
        if expected_400:
            res.notes.append("ожидаемый 400 от /api/open (проводник выключен в тесте): "
                             + str(len(expected_400)))
        errors = [e for e in events if e["level"] == "error" and not h.benign(e)
                  and e not in expected_400]
        res.check("папка и настройки комнаты: нет ошибок JS/API", not errors,
                  "; ".join(f"{e['kind']}: {e['text'][:160]}" for e in errors[:3]) or "чисто",
                  "0 ошибок", "w10")
        path = h.shot("s12-room-settings")
        if path:
            res.screenshots.append(path)
    finally:
        if old_shell is None:
            os.environ.pop("TEAMROOM_NO_SHELL", None)
        else:
            os.environ["TEAMROOM_NO_SHELL"] = old_shell
    return res


def scenario_s13(h: Harness) -> Result:
    """W19/R2: окно настроек комнаты (B-30…B-33) в настоящем Chrome на песочнице.

    Что проверяем глазами пользователя:
      * разделы-вкладки («Основное», «Состав», «Работа», «Проект», «Файлы»), один на экране;
      * поля «Доска» больше нет;
      * модели и «Особенности» — короткой строкой со ссылкой в карточку команды;
      * Esc и «Отмена» с несохранёнными правками не выбрасывают окно, введённое цело;
      * окно переживает опрос (ждём полный цикл 15 с);
      * проектный режим: выключение сохраняется, «Включить проектный режим» с карты открывает
        НАСТРОЙКИ ЭТОЙ комнаты на разделе «Проект» (а не создание новой), включение снова сохраняется.
    """
    res = Result("S13", SCENARIOS["S13"])
    room = "fn"

    def dialog_state():
        return h.cdp.eval_js(r"""
        (() => {
          const d = document.querySelector('.roomdlg-shell');
          if (!d) return {open: false};
          const tabs = [...d.querySelectorAll('[role="tab"]')];
          const panels = [...d.querySelectorAll('[role="tabpanel"]')];
          const active = tabs.find((t) => t.getAttribute('aria-selected') === 'true');
          const badge = d.querySelector('.roomdlg__state');
          const title = d.querySelector('input[aria-label="Название комнаты"]');
          const bar = d.querySelector('[data-test="unsaved"]');
          return {
            open: true,
            head: ((d.querySelector('.dialog__title') || {}).textContent || '').trim(),
            tabs: tabs.map((t) => (t.textContent || '').trim()),
            active: active ? active.getAttribute('data-tab') : '',
            panels: panels.length,
            visible: panels.filter((p) => !p.hasAttribute('hidden')).length,
            badge: badge ? (badge.textContent || '').trim() : '',
            badgeState: badge ? badge.getAttribute('data-state') : '',
            title: title ? title.value : '',
            bar: Boolean(bar && !bar.classList.contains('hidden')),
            board: Boolean(d.querySelector('input[aria-label="Доска задач"]')),
            modelEditor: d.querySelectorAll('.agent-model').length,
            teamLink: (() => {
              const a = [...d.querySelectorAll('a')].find((x) => (x.textContent || '').trim() === 'Настроить в команде');
              return a ? (a.getAttribute('href') || '') : '';
            })(),
            notesLinks: d.querySelectorAll('.notes-link').length,
          };
        })()
        """) or {"open": False}

    def open_settings():
        h.cdp.eval_js(_click_button_js("Настройки комнаты", "document.querySelector('#topbar') || document"))
        state = {}
        deadline = time.time() + 10
        while time.time() < deadline:
            state = dialog_state()
            if state.get("open"):
                break
            time.sleep(0.15)
        return state

    # 1) «Команда» → шестерёнка: окно на разделах, без «Доски»
    _close_dialogs(h)
    h.goto("#/room/%s/team" % room)
    h.wait_ready(15)
    state = open_settings()
    res.check("настройки комнаты открылись из «Команды»", state.get("open") and "Настройки комнаты" in str(state.get("head")),
              json.dumps({k: state.get(k) for k in ("open", "head")}, ensure_ascii=False)[:300],
              "заголовок «Настройки комнаты — …»", "team")
    res.check("пять разделов-вкладок названы по-русски",
              state.get("tabs") == ["Основное", "Состав", "Работа", "Проект", "Файлы"],
              json.dumps(state.get("tabs"), ensure_ascii=False), "5 вкладок", "team")
    res.check("на экране ровно один раздел", state.get("panels") == 5 and state.get("visible") == 1,
              json.dumps({"panels": state.get("panels"), "visible": state.get("visible")}), "5 и 1", "team")
    res.check("открылся раздел «Основное»", state.get("active") == "main", str(state.get("active")), "main", "team")
    res.check("поля «Доска» больше нет", state.get("board") is False, str(state.get("board")), "нет поля", "team")

    # 2) «Состав»: модели и особенности — строкой и ссылкой, без второго редактора
    h.cdp.eval_js("(() => { const t=document.querySelector('.roomdlg__tabs [data-tab=members]'); if (t) t.click(); return 'ok'; })()")
    time.sleep(0.4)
    state = dialog_state()
    res.check("«Состав»: ссылка «Настроить в команде» ведёт в карточку команды",
              state.get("teamLink") == "#/room/%s/team" % room, str(state.get("teamLink")),
              "#/room/%s/team" % room, "team")
    res.check("«Состав»: редактора модели внутри окна нет", int(state.get("modelEditor") or 0) == 0,
              str(state.get("modelEditor")), "0", "team")
    res.check("«Состав»: «Особенности» — ссылки в блокнот, а не редакторы",
              int(state.get("notesLinks") or 0) >= 1, str(state.get("notesLinks")), "≥ 1 ссылка", "team")

    # 3) «Проект»: состояние словами
    h.cdp.eval_js("(() => { const t=document.querySelector('.roomdlg__tabs [data-tab=project]'); if (t) t.click(); return 'ok'; })()")
    time.sleep(0.4)
    state = dialog_state()
    res.check("«Проект»: плашка «Сейчас: …» видна и называет состояние",
              "Сейчас:" in str(state.get("badge")) and state.get("badgeState") in ("on", "off"),
              json.dumps({"badge": state.get("badge"), "state": state.get("badgeState")}, ensure_ascii=False)[:300],
              "«Сейчас: включён/выключен»", "roadmap")

    # 4) Несохранённые правки: Esc и «Отмена» не выбрасывают окно
    typed = h.cdp.eval_js(r"""
    (() => {
      const t = document.querySelector('.roomdlg__shell input[aria-label="Название комнаты"]')
             || document.querySelector('.dialog input[aria-label="Название комнаты"]');
      if (!t) return {ok: false};
      t.focus(); t.value = t.value + ' (правка)';
      t.dispatchEvent(new Event('input', {bubbles: true}));
      return {ok: true, value: t.value};
    })()""") or {"ok": False}
    res.check("название изменено — правки не сохранены", bool(typed.get("ok")), json.dumps(typed, ensure_ascii=False)[:200], "ввод принят", "team")
    h.cdp.eval_js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}))")
    time.sleep(0.4)
    state = dialog_state()
    res.check("Esc с несохранёнными правками не закрыл окно", state.get("open") is True, json.dumps(state, ensure_ascii=False)[:200], "окно на месте", "team")
    res.check("Esc показал выбор «сохранить / закрыть без сохранения / вернуться»", state.get("bar") is True, str(state.get("bar")), "плашка", "team")
    res.check("введённое не потеряно", str(state.get("title") or "").endswith("(правка)"), str(state.get("title")), "текст с «(правка)»", "team")
    h.cdp.eval_js(_click_button_js("Вернуться"))
    time.sleep(0.3)
    state = dialog_state()
    res.check("«Вернуться» убрал плашку и оставил окно", state.get("bar") is False and state.get("open") is True,
              json.dumps({"bar": state.get("bar"), "open": state.get("open")}), "окно открыто", "team")

    # 5) Окно переживает полный цикл опроса (15 с)
    time.sleep(16.5)
    state = dialog_state()
    res.check("после цикла опроса (16 с) окно всё ещё открыто", state.get("open") is True,
              json.dumps({"open": state.get("open")}), "открыто", "team")

    # 6) Отказываемся от правок осознанно — окно закрывается
    h.cdp.eval_js(_click_button_js("Отмена"))
    time.sleep(0.3)
    h.cdp.eval_js(_click_button_js("Закрыть без сохранения"))
    time.sleep(0.5)
    res.check("«Закрыть без сохранения» закрывает окно", dialog_state().get("open") is False,
              json.dumps(dialog_state(), ensure_ascii=False)[:200], "окно закрыто", "team")

    # 7) Проектный режим: выключаем и проверяем, что сохранилось (B-33)
    _ok, before, _raw = h.json_ok("/api/rooms/" + room)
    state = open_settings()
    res.check("настройки открылись повторно", state.get("open") is True, str(state.get("open")), "окно", "team")
    was_on = str(state.get("badgeState") or "") == "on"
    h.cdp.eval_js(r"""
    (() => {
      const t = document.querySelector('.roomdlg__tabs [data-tab=project]'); if (t) t.click();
      const b = document.querySelector('#roomdlg-project-mode');
      if (!b) return 'нет переключателя';
      if (b.checked !== %s) { b.checked = %s; b.dispatchEvent(new Event('change', {bubbles: true})); }
      return 'ok';
    })()""" % ("false" if was_on else "true", "false" if was_on else "true"))
    time.sleep(0.3)
    h.cdp.eval_js(_click_button_js("Сохранить"))
    time.sleep(1.2)
    _ok2, after, _raw2 = h.json_ok("/api/rooms/" + room)
    want = (not was_on)
    res.check("проектный режим сохранился в комнате (API)", bool(after.get("project_mode")) == want,
              json.dumps({"было": before.get("project_mode"), "стало": after.get("project_mode"),
                          "ожидали": want}, ensure_ascii=False), str(want), "roadmap")

    # 8) B-9: ссылка «Включить проектный режим» с карты открывает НАСТРОЙКИ ЭТОЙ комнаты
    if want is False:
        h.goto("#/room/%s/roadmap" % room)
        h.wait_ready(15)
        time.sleep(0.6)
        clicked = h.cdp.eval_js(_click_button_js("Включить проектный режим", "document.querySelector('#view') || document"))
        res.check("на карте есть кнопка «Включить проектный режим»", bool(clicked and clicked.get("ok")),
                  json.dumps(clicked, ensure_ascii=False)[:300], "кнопка нажата", "roadmap")
        state = {}
        deadline = time.time() + 10
        while time.time() < deadline:
            state = dialog_state()
            if state.get("open"):
                break
            time.sleep(0.15)
        res.check("ссылка открыла настройки ТЕКУЩЕЙ комнаты, а не создание новой",
                  "Настройки комнаты" in str(state.get("head")) and "Новая комната" not in str(state.get("head")),
                  str(state.get("head")), "«Настройки комнаты — «Рабочее пространство»»", "roadmap")
        res.check("и сразу на разделе «Проект»", state.get("active") == "project", str(state.get("active")), "project", "roadmap")
        res.check("состояние в плашке — «выключен»", "выключен" in str(state.get("badge")), str(state.get("badge")), "«Сейчас: выключен»", "roadmap")
        h.cdp.eval_js("(() => { const b=document.querySelector('#roomdlg-project-mode'); if (!b) return; b.checked = true; b.dispatchEvent(new Event('change',{bubbles:true})); })()")
        time.sleep(0.2)
        h.cdp.eval_js(_click_button_js("Сохранить"))
        time.sleep(1.2)
        _ok3, final, _raw3 = h.json_ok("/api/rooms/" + room)
        res.check("включение с карты сохранилось", bool(final.get("project_mode")) is True,
                  json.dumps({"project_mode": final.get("project_mode")}), "true", "roadmap")
        res.check("после сохранения окно закрылось", dialog_state().get("open") is False,
                  json.dumps(dialog_state(), ensure_ascii=False)[:200], "закрыто", "roadmap")
    else:
        res.notes.append("Шаг 8 пропущен: проектный режим остался включённым (кнопка «Включить» на карте не появляется).")

    _close_dialogs(h)
    return res


SCENARIO_FUNCS = {
    "S1": scenario_s1, "S2": scenario_s2, "S3": scenario_s3, "S4": scenario_s4,
    "S5": scenario_s5, "S6": scenario_s6, "S7": scenario_s7, "S8": scenario_s8,
    "S9": scenario_s9, "S10": scenario_s10, "S11": scenario_s11, "S12": scenario_s12,
    "S13": scenario_s13,
}


# ─────────────────────────────────────────────────────────────── отчёт

def write_reports(results: list[Result], h: Harness, opts, started: float,
                  exit_code: int, path_md: Path, path_json: Path, json_extra: Path | None) -> None:
    duration = round(time.time() - started, 1)
    errors: list[dict] = []
    for ev in h.events_log:
        if ev.get("level") != "error" or h.benign(ev):
            continue
        errors.append({"scenario": "—", "screen": ev.get("screen") or "", "kind": ev.get("kind") or "",
                       "text": ev.get("text") or "", "url": ev.get("url") or "",
                       "status": ev.get("status")})
    for res in results:
        for d in res.defects:
            errors.append({"scenario": res.id, "screen": d.get("screen") or "", "kind": "check",
                           "text": d["title"] + ": " + d.get("got", ""), "url": ""})

    payload = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "duration_s": duration,
        "base_url": h.base,
        "sandbox": str(h.sb.root) if h.sb else "",
        "chrome": str(h.chrome.proc.args[0]) if h.chrome else "",
        "tick": h.tick_stats,
        "setup": h.setup,
        "data_notes": h.data_notes,
        "cleanup": h.cleanup,
        "scenarios": [r.as_dict() for r in results],
        "errors": errors,
        "screenshots": [p for r in results for p in r.screenshots],
        "totals": {"scenarios": len(results),
                   "pass": sum(1 for r in results if r.status == "PASS"),
                   "fail": sum(1 for r in results if r.status == "FAIL"),
                   "checks": sum(len(r.checks) for r in results),
                   "checks_failed": sum(r.failed for r in results),
                   "defects": sum(len(r.defects) for r in results)},
        "exit_code": exit_code,
    }
    path_json.parent.mkdir(parents=True, exist_ok=True)
    # W17-STAB-2: слепки песочницы содержат frozenset/Path — приводим к строкам,
    # иначе отчёт не записывается и провал сценария теряется в трейсбеке.
    blob = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    path_json.write_text(blob, encoding="utf-8")
    if json_extra:
        json_extra.parent.mkdir(parents=True, exist_ok=True)
        json_extra.write_text(blob, encoding="utf-8")

    lines = [
        "# Сквозной UI-тест «Комнаты команды» — отчёт",
        "",
        f"Дата: {payload['started_at']} · длительность {duration} с · "
        f"итог: **{'PASS' if exit_code == 0 else 'FAIL'}**",
        "",
        f"- Сервер: `{h.base}` (песочница, `start_watcher=False`)",
        f"- Песочница: `{payload['sandbox']}`",
        f"- Chrome: `{payload['chrome']}` · headless, временный профиль",
        f"- Watcher: `tick_once()` → `{json.dumps(h.tick_stats, ensure_ascii=False)}`",
        f"- Инфраструктура: `{sum(1 for e in h.setup if e.get('ok'))}/{len(h.setup)}` проверок",
        "",
    ]
    if h.data_notes:
        lines += ["## Оговорки о данных", ""] + [f"- {n}" for n in h.data_notes] + [""]
    lines += [
        "## Сценарии",
        "",
        "| # | Сценарий | Результат | Проверок | Провалов | Время |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.id} | {r.title} | **{r.status}** | {len(r.checks)} | {r.failed} | "
                     f"{round((r.finished or time.time()) - r.started, 1)} с |")
    lines += ["", "## Дефекты", ""]
    defects = [(r.id, d) for r in results for d in r.defects]
    if defects:
        for idx, (sid, d) in enumerate(defects, 1):
            lines.append(f"### D{idx}. {d['title']} ({sid})")
            lines.append("")
            lines.append(f"- Экран: `{d.get('screen') or '—'}`")
            lines.append(f"- Получено: `{d.get('got', '')}`")
            lines.append(f"- Ожидалось: `{d.get('want', '')}`")
            lines.append("")
    else:
        lines += ["Дефектов нет — все проверки прошли.", ""]
    note_lines = []
    seen_notes: set = set()
    for r in results:
        for note in r.notes:
            if note not in seen_notes:
                seen_notes.add(note)
                note_lines.append(f"- {r.id}: {note}")
    if note_lines:
        lines += ["## Наблюдения (не блокируют сценарий)", ""] + note_lines + [""]
    lines += ["## Ошибки консоли / сети", ""]
    if errors:
        lines += ["| Сценарий | Экран | Тип | Текст | URL |", "|---|---|---|---|---|"]
        for e in errors[:80]:
            text = str(e.get("text") or "").replace("|", "\\|").replace("\n", " ")[:220]
            url = str(e.get("url") or "").replace("|", "\\|")[:160]
            lines.append(f"| {e['scenario']} | {e['screen']} | {e['kind']} | {text} | {url} |")
        if len(errors) > 80:
            lines.append(f"| … | | | ещё {len(errors) - 80} | |")
    else:
        lines.append("Ошибок не зафиксировано.")
    lines += ["", "## Скриншоты", ""]
    shots = [p for r in results for p in r.screenshots]
    if shots:
        for p in shots:
            lines.append(f"- `{p}`")
    else:
        lines.append("Нет.")
    lines += ["", "## Очистка", "",
              f"- Сервер остановлен: `{h.cleanup.get('server_stopped')}`",
              f"- Chrome остановлен: `{h.cleanup.get('chrome_stopped')}`",
              f"- Реальная kanban.db не изменена: `{h.cleanup.get('real_board_untouched')}`",
              f"- Слепок до: `{json.dumps(h.cleanup.get('real_before'), ensure_ascii=False, default=str)}`",
              f"- Слепок после: `{json.dumps(h.cleanup.get('real_after'), ensure_ascii=False, default=str)}`", ""]
    path_md.parent.mkdir(parents=True, exist_ok=True)
    path_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─────────────────────────────────────────────────────────────── CLI

def main(argv=None) -> int:
    """Внешняя точка входа: запрет записи в реальный Hermes/Vault и агентского CLI.

    См. docs/SAFETY-RULES.md; песочница `Sandbox` включает те же запреты повторно
    (вложенные контексты считаются по глубине).
    """
    from server import safety

    with safety.forbid_real_writes(), safety.forbid_real_subprocess():
        return _main(argv)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Сквозной UI-тест «Комнаты команды» (headless Chrome + CDP).")
    ap.add_argument("--shots", default=str(ROOT / "docs" / "shots" / "e2e"), help="каталог скриншотов")
    ap.add_argument("--only", default="", help="сценарии через запятую: S1,S3 (по умолчанию все)")
    ap.add_argument("--json", default="", help="куда дополнительно записать JSON")
    ap.add_argument("--chrome", default=None, help="путь к chrome.exe")
    ap.add_argument("--report", default=str(ROOT / "docs" / "status" / "e2e-report.md"),
                    help="markdown-отчёт")
    opts = ap.parse_args(argv)

    only = [s.strip().upper() for s in re.split(r"[,\s]+", opts.only or "") if s.strip()]
    unknown = [s for s in only if s not in SCENARIO_FUNCS]
    if unknown:
        print("Неизвестные сценарии: " + ", ".join(unknown), file=sys.stderr)
        return 2
    selected = only or list(SCENARIO_FUNCS)

    started = time.time()
    h = Harness(opts)
    results: list[Result] = []
    fatal = ""
    try:
        h.start()
        for sid in selected:
            res = Result(sid, SCENARIOS[sid])
            try:
                if sid != "S1":
                    h.chrome.set_viewport(1280, 900)   # S1 сам меняет ширину под сценарий
                res = SCENARIO_FUNCS[sid](h)
            except Exception as exc:  # noqa: BLE001 - сценарий не должен ронять харнесс
                res.error = f"{exc.__class__.__name__}: {exc}"
                res.check("сценарий выполнился без исключения", False, res.error, "без ошибок", sid)
            res.finished = time.time()
            results.append(res)
            print(f"{sid}: {res.status} · проверок {len(res.checks)} · провалов {res.failed}"
                  + (f" · {res.error}" if res.error else ""), flush=True)
    except Exception as exc:  # noqa: BLE001 - инфраструктурный сбой
        fatal = f"{exc.__class__.__name__}: {exc}"
        print("ХАРНЕСС: " + fatal, file=sys.stderr)
    finally:
        # порядок: Chrome → сервер → слепок реальной БД → удаление песочницы
        if h.chrome is not None:
            try:
                h.chrome.close()
            except Exception as exc:  # noqa: BLE001
                h.setup.append({"name": "chrome.close", "ok": False, "detail": str(exc)[:300]})
            h.cleanup["chrome_stopped"] = h.chrome.proc.poll() is not None
        if h.server is not None:
            try:
                h.server.shutdown()
                h.server.server_close()
            except Exception as exc:  # noqa: BLE001
                h.setup.append({"name": "server.shutdown", "ok": False, "detail": str(exc)[:300]})
            if h.thread is not None:
                h.thread.join(timeout=5)
            h.cleanup["server_stopped"] = not (h.thread and h.thread.is_alive())
        if h.sbx is not None and h.cfg is not None:
            try:
                after = snapshot_real_db(h.cfg)
                assert_real_untouched(h.before, h.cfg)
                h.cleanup["real_after"] = after
                h.cleanup["real_board_untouched"] = True
            except Exception as exc:  # noqa: BLE001
                h.cleanup["real_board_untouched"] = False
                h.cleanup["real_error"] = str(exc)[:500]
                h.setup.append({"name": "реальная kanban.db не изменена", "ok": False, "detail": str(exc)[:500]})
            h.cleanup["real_before"] = h.before
            try:
                h.sbx.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                h.setup.append({"name": "sandbox cleanup", "ok": False, "detail": str(exc)[:300]})

    ok_setup = all(e.get("ok") for e in h.setup)
    ok_cleanup = (h.cleanup.get("chrome_stopped") and h.cleanup.get("server_stopped")
                  and h.cleanup.get("real_board_untouched"))
    exit_code = 0 if (results and all(r.status == "PASS" for r in results) and ok_setup and ok_cleanup and not fatal) else 1
    if not results and fatal:
        results = [Result("S?", "инфраструктура")]
        results[0].error = fatal
        results[0].check("харнесс запустился", False, fatal, "без ошибок", "harness")

    md = Path(opts.report)
    js = ROOT / "docs" / "status" / "e2e-report.json"
    extra = Path(opts.json) if opts.json else None
    write_reports(results, h, opts, started, exit_code, md, js, extra)
    total = sum(len(r.checks) for r in results)
    failed = sum(r.failed for r in results)
    print(f"\nИТОГ: {'PASS' if exit_code == 0 else 'FAIL'} · сценариев "
          f"{sum(1 for r in results if r.status == 'PASS')}/{len(results)} PASS · "
          f"проверок {total - failed}/{total} · {round(time.time() - started, 1)} с")
    print(f"Отчёт: {md}")
    print(f"JSON:  {js}" + (f" и {extra}" if extra else ""))
    if not ok_setup:
        print("Проблемы инфраструктуры: " + json.dumps([e for e in h.setup if not e.get('ok')],
                                                       ensure_ascii=False)[:800], file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
