#!/usr/bin/env python3
"""Сквозной E2E «проектный режим» в настоящем Chrome на МОКЕ (волна 9b).

    python tools/e2e_project.py [--port 8883] [--shots docs/shots/e2e]
                               [--json docs/status/e2e-project.json]
                               [--report docs/status/e2e-project.md]
                               [--only a,b,c,d,e,f,g] [--phase-scale 0.4]

Что проверяет (каждый пункт — факт из браузера, не из разметки):

a) создание комнаты через диалог: имя, руководитель, состав, папка проекта
   (на моке — без диска), «особые указания» одному участнику, режим «Сначала план»;
b) композер: флажок «Это бриф проекта» → запрос-бриф создан;
c) черновик карты: плашка «Предложенная карта» на странице брифа и блок
   «Части работы» на вкладке «Карта проекта»;
d) «Запустить всё» с подтверждением → части работы идут по очереди: лимит
   параллельности не превышен, зависимая часть стартует только после
   того, как зависимость закрыта;
e) страница под-запроса: крошка «Комната › Бриф › Часть работы», поле «Диалог»,
   диктовка в поддельный микрофон (WAV + фейковое устройство Chrome), отправка
   руководителю, кнопка не залипает на «Отправляю…», в журнале запись «добавил»;
f) «От вас нужно»: галочек у шагов нет (W14-H §12) — строка открывает окно шага с
   действиями «Согласен / Прокомментировать / Переместить / Не согласен — убрать»;
   «Согласен» уходит ответом на шаг без 404 и меняет строку сразу, без перезагрузки;
   «Ответить» с микрофоном — кнопка и поле не накладываются (getBoundingClientRect);
g) адаптив 780/560/360 на этих же экранах, обе темы.

Честные ограничения (см. отчёт):
* сервер — `tools/mock_server.py` на своём порту (по умолчанию 8883), состояние
  только в памяти, на диск ничего не пишется; боевой 8777 и демо 8778 не трогаются;
* распознавание речи — заглушка (`mock_stt.MODULE_OVERRIDE`): настоящий Whisper не
  запускается, текст ответа фиксированный. Реальны цепочка «микрофон Chrome →
  MediaRecorder → POST /api/stt/transcribe → вставка в поле» и фейковое аудиоустройство;
* реальных агентов нет: под-запросы создаёт мок, доска/конфиги не меняются.

Код возврата 1 при любом провале; отчёт — `docs/status/e2e-project.md`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for _p in (str(ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.cdp import Chrome                      # noqa: E402
from tools.mic_probe import WAV, launch_media     # noqa: E402
import mock_server                                # noqa: E402
import mock_stt                                   # noqa: E402

DEFAULT_PORT = 8883
DEFAULT_SHOTS = ROOT / "docs" / "shots" / "e2e"
DEFAULT_JSON = ROOT / "docs" / "status" / "e2e-project.json"
DEFAULT_MD = ROOT / "docs" / "status" / "e2e-project.md"

ROOM_NAME = "Проект Лето · десерты"
PROJECT_DIR = r"C:\Проекты\Комната — демо"
NOTE_PROFILE = "team-ops"
NOTE_TEXT = "Отвечай коротко и только цифрами. Всегда проверяй сроки поставки по закону."
MEMBERS = ("team-ops", "team-cfo", "team-marketing")
DIRECTOR = "team-director"
MAX_PARALLEL = 1
BRIEF_TEXT = ("Запустить набор десертов Лето во Заозёрье: рецептура и граммовки, "
              "упаковка и бренд, цена с маржой, первые каналы продаж. "
              "Нужна карта направлений с ответственными и сроками.")
STT_TEXT = "Добавьте поставщиков риса и бобов: сравните цены и сроки до пятницы."

STEPS = ("a", "b", "c", "d", "e", "f", "g")


# ─────────────────────────────────────────────── заглушка распознавания речи

class StubSTT:
    """Тот же интерфейс, что `server/stt.py`, но без Whisper: текст фиксированный."""

    model = "e2e-stub"

    def status(self, cfg):
        return {"available": True, "loaded": True, "loading": False, "model": self.model,
                "device": "cpu", "reason": None}

    def transcribe(self, cfg, audio: bytes, content_type: str = "audio/webm", language=None):
        return {"text": STT_TEXT, "raw": STT_TEXT, "duration_s": 1.4, "elapsed_s": 0.02,
                "language": language or "ru", "model": self.model, "device": "cpu",
                "corrections": []}

    def get_dictionary(self, cfg):
        return {"terms": [], "replacements": [], "updated_at": 0}

    def put_dictionary(self, cfg, obj):
        return {"terms": [], "replacements": [], "updated_at": int(time.time())}

    def learn(self, cfg, raw: str, final: str, apply: bool = False):
        return {"suggestions": [], "saved": []}


# ───────────────────────────────────────────────────────────────── результат

class Result:
    """Один шаг сценария: проверки, скриншоты, дефекты."""

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
        self.checks.append({"name": name, "ok": bool(ok), "got": str(got)[:700],
                            "want": str(want)[:300], "screen": screen})
        if not ok:
            self.defects.append({"step": self.id, "screen": screen, "title": name,
                                 "got": str(got)[:700], "want": str(want)[:300]})
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
                "duration_s": round((self.finished or time.time()) - self.started, 2),
                "error": self.error}


# ───────────────────────────────────────────────────────────────── браузер

class Probe:
    """Тонкая обёртка над CDP: js/wait/goto/скриншот/HTTP-ошибки."""

    def __init__(self, ch: Chrome, base: str, shots: Path):
        self.ch = ch
        self.cdp = ch.cdp
        self.base = base
        self.shots = shots
        self.net_urls: dict[str, str] = {}

    # --- JS ---------------------------------------------------------------
    def js(self, expr: str, await_promise: bool = False, timeout: float = 20.0):
        return self.cdp.eval_js(expr, await_promise=await_promise, timeout=timeout)

    def wait_js(self, expr: str, timeout: float = 15.0, label: str = ""):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                last = self.js(expr)
            except Exception:  # noqa: BLE001 — страница могла ещё грузиться
                last = None
            if last:
                return last
            time.sleep(0.08)
        raise AssertionError("не дождались %s (последнее: %r)" % (label or expr, last))

    def goto(self, url: str, wait_expr: str = "", timeout: float = 25.0) -> None:
        for attempt in range(3):
            self.ch.navigate(url)
            try:
                self.wait_js("document.readyState !== 'loading' && Boolean(document.querySelector('#view'))",
                             15, "разбора DOM")
                if wait_expr:
                    self.wait_js(wait_expr, timeout, wait_expr[:80])
                time.sleep(0.15)
                return
            except AssertionError:
                if attempt == 2:
                    raise
                time.sleep(0.4)

    def reload(self) -> None:
        """Настоящая перезагрузка страницы (навигация по тому же hash не перезагружает)."""
        self.cdp.call("Page.reload", {"ignoreCache": True})
        self.wait_js("document.readyState !== 'loading' && Boolean(document.querySelector('#view'))",
                     20, "перезагрузки")
        time.sleep(0.4)

    def click(self, selector: str, hold: float = 0.06) -> dict:
        """Щёлкнуть настоящей мышью по центру элемента (Input.dispatchMouseEvent).

        Нужен там, где проверяется ЖЕСТ человека (заголовок сворачиваемого блока):
        `element.click()` такого не воспроизводит — нажатие и отпускание должны
        попадать в один и тот же узел.
        """
        if not getattr(self, "_mouse_warm", False):
            try:
                self.cdp.call("Page.bringToFront")
            except Exception:                        # noqa: BLE001 — старый Chrome
                pass
            self.cdp.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": 8, "y": 8, "button": "none", "buttons": 0, "clickCount": 0})
            self.cdp.call("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": 8, "y": 8, "button": "left", "buttons": 1, "clickCount": 1})
            self.cdp.call("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": 8, "y": 8, "button": "left", "buttons": 0, "clickCount": 1})
            time.sleep(0.2)
            self._mouse_warm = True
        box = self.js("""(() => {
          const el = document.querySelector(%s);
          if (!el) return null;
          try { el.scrollIntoView({block: 'center'}); } catch { /* нет прокрутки */ }
          const r = el.getBoundingClientRect();
          if (!r.width || !r.height) return null;
          return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        })()""" % json.dumps(selector)) or None
        if not box:
            return {"ok": False, "reason": "нет элемента", "selector": selector}
        x, y = float(box["x"]), float(box["y"])
        self.cdp.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 0})
        self.cdp.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
        time.sleep(hold)
        self.cdp.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1})
        time.sleep(0.25)
        return {"ok": True, "at": [round(x, 1), round(y, 1)]}

    def open_thread(self) -> dict:
        """Раскрыть «Диалог по запросу» (У-1: по умолчанию свёрнут) настоящим кликом.

        Поле ввода и лента строятся только при раскрытии — без этого шага
        `.thr__input` не существует вовсе, и проверки диалога становятся «слепыми».
        """
        state = self.js("""(() => {
          const sec = document.querySelector('[data-section="thread"]');
          if (!sec) return {found: false};
          return {found: true, open: sec.getAttribute('data-open') === '1',
                  aria: (sec.querySelector('.sec__toggle') || {}).getAttribute
                        ? sec.querySelector('.sec__toggle').getAttribute('aria-expanded') : null};
        })()""") or {}
        if not state.get("found"):
            return {"found": False, "clicks": 0, "state": state}
        clicks = 0
        for _ in range(2):
            if state.get("open"):
                break
            self.click('[data-section="thread"] .sec__toggle')
            clicks += 1
            deadline = time.time() + 4
            while time.time() < deadline:
                state = self.js("""(() => {
                  const sec = document.querySelector('[data-section="thread"]');
                  if (!sec) return {found: false};
                  const btn = sec.querySelector('.sec__toggle');
                  return {found: true, open: sec.getAttribute('data-open') === '1',
                          aria: btn ? btn.getAttribute('aria-expanded') : null};
                })()""") or {}
                if state.get("open"):
                    break
                time.sleep(0.1)
        if state.get("open"):
            try:
                self.wait_js("Boolean(document.querySelector('.thr__input'))", 8, "поля «Диалог»")
            except AssertionError:
                pass
        return {"found": True, "open": bool(state.get("open")), "aria": state.get("aria"),
                "clicks": clicks}

    def shot(self, name: str) -> str:
        path = self.shots / (name + ".png")
        try:
            self.ch.screenshot(path)
        except Exception as exc:  # noqa: BLE001
            print("   ! скриншот %s не снялся: %s" % (name, exc))
            return ""
        try:
            return str(path.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            return str(path)

    # --- сеть -------------------------------------------------------------
    def http_errors(self, mark: int, only_api: bool = True) -> list[dict]:
        """Ответы 4xx/5xx после отметки `mark` (сеть уже собирается CDP)."""
        out = []
        for ev in self.cdp.events_since(mark):
            if ev.get("method") == "Network.requestWillBeSent":
                p = ev.get("params") or {}
                rid = p.get("requestId")
                if rid:
                    self.net_urls[rid] = ((p.get("request") or {}).get("url") or "")
                continue
            if ev.get("method") != "Network.responseReceived":
                continue
            resp = ev.get("response") or {}
            status = int(resp.get("status") or 0)
            if status < 400:
                continue
            url = str(resp.get("url") or "")
            if only_api and "/api/" not in url:
                continue
            out.append({"status": status, "url": url})
        return out

    def js_errors(self, mark: int) -> list[str]:
        out = []
        for ev in self.cdp.events_since(mark):
            method = ev.get("method")
            p = ev.get("params") or {}
            if method == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                text = str((d.get("exception") or {}).get("description") or d.get("text") or "")
                out.append("exception: " + text[:300])
            elif method == "Runtime.consoleAPICalled" and str(p.get("type")) == "error":
                parts = [str(a.get("value") or a.get("description") or "") for a in p.get("args") or []]
                out.append("console: " + " ".join(parts)[:300])
            elif method == "Log.entryAdded":
                entry = p.get("entry") or {}
                if str(entry.get("level")) == "error" and "favicon" not in str(entry.get("url") or ""):
                    out.append("log: " + str(entry.get("text") or "")[:300])
        return [e for e in out if "favicon" not in e.lower()]

    def api(self, path: str, method: str = "GET", body=None):
        import http.client
        port = int(self.base.rsplit(":", 1)[-1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        headers, payload = {}, None
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


# ─────────────────────────────────────────────────────────── общий каркас

def start_mock(port: int, phase_scale: float):
    """Мок-сервер на своём порту (не 8777/8778) + заглушка STT без Whisper."""
    mock_stt.reset()
    mock_stt.MODULE_OVERRIDE = StubSTT()
    server = mock_server.create_server(port=port, phase_scale=phase_scale)

    def _handle_error(request, client_address):
        """Не шуметь на обрывах соединения браузером (ConnectionReset при навигации)."""
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionResetError):
            return
        traceback.print_exc()

    server.handle_error = _handle_error
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, name="teamroom-e2e-project",
                              daemon=True)
    thread.start()
    return server, thread


def open_dialog_js() -> str:
    """Кнопка создания комнаты: в шапке «Комнаты» (когда комнаты есть) или в пустом состоянии."""
    return r"""
    (() => {
      const heads = [...document.querySelectorAll('.rooms__head-actions button, .rooms__head button')];
      const empty = [...document.querySelectorAll('button')]
        .filter((b) => (b.textContent || '').trim() === 'Создать комнату');
      const btn = heads[0] || empty[0];
      if (!btn) {
        return {ok: false, buttons: [...document.querySelectorAll('button')]
          .map((b) => (b.textContent || '').trim()).filter(Boolean).slice(0, 25)};
      }
      btn.click();
      return {ok: true, label: (btn.textContent || '').trim()};
    })()
    """


def fill_dialog_js() -> str:
    return r"""
    (() => {
      const d = document.querySelector('.dialog.roomdlg-shell') || document.querySelector('.dialog');
      if (!d) return {ok: false, why: 'нет диалога'};
      const fire = (el, value) => {
        el.value = value;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      const title = d.querySelector('input[aria-label="Название комнаты"]');
      if (!title) return {ok: false, why: 'нет поля названия'};
      fire(title, %(name)s);
      const dir = d.querySelector('select[aria-label="Руководитель комнаты"]');
      if (!dir) return {ok: false, why: 'нет выбора руководителя'};
      fire(dir, %(director)s);
      const wanted = %(members)s;
      const boxes = [...d.querySelectorAll('.roomdlg__member input[type=checkbox]')];
      for (const cb of boxes) {
        const profile = String(cb.id || '').replace('roomdlg-m-', '');
        const want = wanted.includes(profile);
        if (cb.checked !== want) cb.click();
      }
      const path = d.querySelector('input[aria-label="Папка проекта"]');
      if (!path) return {ok: false, why: 'нет поля папки проекта'};
      fire(path, %(dir_path)s);
      path.dispatchEvent(new Event('blur', {bubbles: true}));
      const radio = d.querySelector('#roomdlg-launch-plan_first');
      if (radio && !radio.checked) radio.click();
      const streams = d.querySelector('select[aria-label="Одновременно частей работы"]');
      if (streams) fire(streams, %(max_parallel)s);
      const note = null;             // W17 P6: записи живут в блокноте «Особенности», не в диалоге
      let noteOk = false;
      const mode = d.querySelector('#roomdlg-project-mode');
      const links = [...d.querySelectorAll('.notes-link')];
      const hint = [...d.querySelectorAll('#roomdlg-panel-members .roomdlg__hint')]
        .find((n) => /Особенност/.test(n.innerText || ''));
      return {ok: true, title: title.value, director: dir.value,
              members: boxes.filter((cb) => cb.checked).map((cb) => String(cb.id).replace('roomdlg-m-', '')),
              projectDir: path.value, note: noteOk, noteCount: 0,
              launchMode: radio ? radio.checked : null,
              streams: streams ? streams.value : null,
              projectMode: mode ? mode.checked : null,
              notesLinks: links.length,
              notesHint: hint ? String(hint.innerText || '').replace(/\s+/g, ' ').trim() : '',
              notesLabels: links.map((n) => String(n.textContent || '').trim()),
              notesProfiles: links.map((n) => n.getAttribute('data-profile'))};
    })()
    """ % {"name": json.dumps(ROOM_NAME), "director": json.dumps(DIRECTOR),
           "members": json.dumps(list(MEMBERS)), "dir_path": json.dumps(PROJECT_DIR),
           "max_parallel": json.dumps(str(MAX_PARALLEL)), "note_profile": json.dumps(NOTE_PROFILE),
           "note_text": json.dumps(NOTE_TEXT)}


def submit_dialog_js() -> str:
    return r"""
    (() => {
      const d = document.querySelector('.dialog.roomdlg-shell') || document.querySelector('.dialog');
      if (!d) return {ok: false, why: 'нет диалога'};
      const err = d.querySelector('.roomdlg__error');
      const btn = [...d.querySelectorAll('button')]
        .find((b) => (b.textContent || '').trim() === 'Создать комнату' && String(b.type) === 'submit');
      if (!btn) return {ok: false, why: 'нет кнопки создания', error: err ? err.innerText : ''};
      btn.click();
      return {ok: true};
    })()
    """


# ─────────────────────────────────────────────────── a. комната через диалог

def step_a(p: Probe, res: Result, state: dict, port: int) -> None:
    res.check("мок отвечает на /api/health", *health(p))
    p.goto(f"{p.base}/?theme=light#/", "Boolean(document.querySelector('#view'))")
    # `#/` на старте приложение заменяет на комнату (initialHash); возвращаемся
    # на обзор комнат уже после загрузки — это обычный маршрут, не хак.
    time.sleep(0.8)
    p.js("(() => { if (location.hash !== '#/') location.hash = '#/'; return location.hash; })()")
    p.wait_js("Boolean(document.querySelector('.rooms__head'))", 12, "обзора комнат")
    mark = p.cdp.event_index()
    opened = p.js(open_dialog_js())
    res.check("кнопка «Создать комнату» нажата", bool(opened and opened.get("ok")),
              json.dumps(opened, ensure_ascii=False)[:400], "кнопка есть", "rooms")
    try:
        p.wait_js("Boolean(document.querySelector('.dialog .roomdlg') && document.querySelector('#roomdlg-panel-members'))",
                  10, "диалога комнаты и блока особенностей")
        p.wait_js("document.querySelectorAll('.roomdlg__member input[type=checkbox]').length >= 2",
                  10, "списка состава")
    except AssertionError as exc:
        res.check("диалог комнаты открылся с составом и указаниями", False, str(exc)[:300], "поля на месте", "room-dialog")
        return
    filled = p.js(fill_dialog_js()) or {}
    res.check("диалог: поля заполнены (имя, руководитель, состав)",
              bool(filled.get("ok")) and filled.get("director") == DIRECTOR
              and sorted(filled.get("members") or []) == sorted(MEMBERS),
              json.dumps(filled, ensure_ascii=False)[:500],
              f"{ROOM_NAME} / {DIRECTOR} / {list(MEMBERS)}", "room-dialog")
    res.check("диалог: папка проекта вписана", str(filled.get("projectDir") or "") == PROJECT_DIR,
              str(filled.get("projectDir")), PROJECT_DIR, "room-dialog")
    res.check("диалог: блок «Особенности» объясняет пустое состояние",
              "После создания комнаты" in str(filled.get("notesHint") or "")
              and "«Команда»" in str(filled.get("notesHint") or ""),
              str(filled.get("notesHint"))[:200],
              "подсказка: настраивается в карточке на вкладке «Команда»", "room-dialog")
    res.check("диалог: режим «Сначала план» выбран", filled.get("launchMode") is True,
              f"launchMode={filled.get('launchMode')}", "plan_first", "room-dialog")
    res.check("диалог: одновременно частей работы = %d" % MAX_PARALLEL,
              str(filled.get("streams")) == str(MAX_PARALLEL),
              str(filled.get("streams")), str(MAX_PARALLEL), "room-dialog")
    res.check("диалог: проектный режим включён", filled.get("projectMode") is True,
              f"project_mode={filled.get('projectMode')}", "true", "room-dialog")
    # живая проверка пути папки (мок — без диска)
    try:
        verdict = p.wait_js("(() => { const n = document.querySelector('.pd-check'); "
                            "const t = n ? (n.innerText || '').trim() : ''; "
                            "return t && !t.startsWith('Проверяю') ? t : ''; })()", 10,
                            "вердикта папки")
        res.check("диалог: проверка папки проекта ответила", bool(str(verdict).strip())
                  and "недоступна" not in str(verdict),
                  str(verdict)[:200], "человеческий вердикт", "room-dialog")
        res.check("диалог: папка не признана ошибкой",
                  "нельзя" not in str(verdict).lower() and "зарезервирован" not in str(verdict).lower(),
                  str(verdict)[:200], "без запрета", "room-dialog")
    except AssertionError as exc:
        res.check("диалог: проверка папки проекта ответила", False, str(exc)[:200], "вердикт", "room-dialog")
    shot = p.shot("w9-01-room-dialog")
    if shot:
        res.screenshots.append(shot)

    mark_create = p.cdp.event_index()
    p.js(submit_dialog_js())
    room_id = ""
    deadline = time.time() + 25
    while time.time() < deadline:
        href = str(p.js("location.hash") or "")
        m = re.match(r"#/room/([^/]+)/composer", href)
        if m:
            room_id = m.group(1)
            break
        time.sleep(0.15)
    res.check("комната создана: переход на её вкладку «Запрос»", bool(room_id), room_id,
              "#/room/<id>/composer", "rooms")
    if not room_id:
        err = p.js("(() => { const n = document.querySelector('.roomdlg__error'); return n ? n.innerText : ''; })()")
        res.notes.append("Диалог не создал комнату; ошибка в диалоге: " + str(err)[:300])
        # STAB-3: почему комната не создалась — сколько POST-ов ушло и что сервер знает
        try:
            posts = [((ev.get("params") or {}).get("request") or {}).get("url") or ""
                     for ev in p.cdp.events_since(mark_create)
                     if ev.get("method") == "Network.requestWillBeSent"
                     and (((ev.get("params") or {}).get("request") or {}).get("method") == "POST")]
            res.notes.append("POST-ов после нажатия «Создать»: %d · %s"
                             % (len(posts), "; ".join(posts[:6])))
            app = state.get("app")
            if app is not None:
                res.notes.append("комнаты в моке: " + json.dumps(
                    [r.get("id") for r in (app.rooms or [])], ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 — диагностика не должна ронять шаг
            res.notes.append("диагностика не снялась: %s" % exc)
        return
    state["room_id"] = room_id
    status, room, raw = p.api("/api/rooms/" + quote(room_id))
    res.check("GET /api/rooms/{id}: комната сохранена", status == 200, f"{status} {raw[:200]!r}", "200", "rooms")
    if status == 200 and isinstance(room, dict):
        res.check("API: руководитель комнаты", str(room.get("orchestrator")) == DIRECTOR,
                  str(room.get("orchestrator")), DIRECTOR, "rooms")
        members = sorted(str(m.get("profile")) for m in room.get("members") or [])
        res.check("API: состав комнаты", members == sorted(MEMBERS),
                  json.dumps(members, ensure_ascii=False), json.dumps(sorted(MEMBERS)), "rooms")
        res.check("API: папка проекта", str(room.get("project_dir") or "") == PROJECT_DIR,
                  str(room.get("project_dir")), PROJECT_DIR, "rooms")
        res.check("API: режим запуска plan_first", str(room.get("launch_mode")) == "plan_first",
                  str(room.get("launch_mode")), "plan_first", "rooms")
        res.check("API: проектный режим включён", room.get("project_mode") is True,
                  str(room.get("project_mode")), "true", "rooms")
        res.check("API: одновременно частей работы", int(room.get("max_parallel_streams") or 0) == MAX_PARALLEL,
                  str(room.get("max_parallel_streams")), str(MAX_PARALLEL), "rooms")
        notes = room.get("agent_notes") or {}
        res.check("API: новых записей особенностей нет (создаются блокнотом)", not notes,
                  json.dumps(notes, ensure_ascii=False)[:300], "{}", "rooms")
    # W17 P6: особенность комнаты пишется блокнотом «Особенности (N)» в настройках
    # комнаты, а не полем в карточке участника — проверяем настоящий путь в UI.
    try:
        p.goto(f"{p.base}/?theme=light#/room/{quote(room_id)}/team",
               "Boolean(document.querySelector('#view'))")
        # B-32: «Особенности» правятся из карточки участника на вкладке «Команда»
        # (в диалоге настроек комнаты остаётся только справка и ссылка «Настроить в команде»).
        p.wait_js("Boolean(document.querySelector('.notes-link'))", 15, "ссылок «Особенности» в карточках команды")
        opened = p.js("(() => { const l = document.querySelector('.notes-link[data-profile=\"%s\"]'); "
                      "if (!l) return {ok: false, links: [...document.querySelectorAll('.notes-link')]"
                      ".map((n) => n.getAttribute('data-profile'))}; l.click(); return {ok: true}; })()"
                      % NOTE_PROFILE) or {}
        res.check("команда: ссылка «Особенности» у нужного участника",
                  bool(opened.get("ok")), json.dumps(opened, ensure_ascii=False)[:300],
                  NOTE_PROFILE, "team")
        p.wait_js("Boolean(document.querySelector('.notes-ed .notes-ed__area'))", 10, "блокнота особенностей")
        p.js(r"""(() => {
          const newBtn = [...document.querySelectorAll('.notes-ed button')]
            .find((b) => /Новая запись/.test(String(b.textContent || '')));
          if (newBtn) newBtn.click();
          const area = document.querySelector('.notes-ed .notes-ed__area');
          if (!area) return false;
          area.focus(); area.value = %s;
          area.dispatchEvent(new Event('input', {bubbles: true}));
          area.dispatchEvent(new Event('change', {bubbles: true}));
          const save = document.querySelector('.notes-ed__save');
          if (save) save.click();
          return true;
        })()""" % json.dumps(NOTE_TEXT))
        saved = ""
        deadline = time.time() + 10
        while time.time() < deadline:
            _s, room2, _r = p.api("/api/rooms/" + quote(room_id))
            got = str(((room2 or {}).get("agent_notes") or {}).get(NOTE_PROFILE, {}).get("text") or "") \
                if isinstance(room2, dict) else ""
            if NOTE_TEXT in got:
                saved = got
                break
            time.sleep(0.2)
        res.check("блокнот особенностей: запись сохранена через API", NOTE_TEXT in saved,
                  saved[:200] or "не сохранилось", NOTE_TEXT[:60], "notes-editor")
        p.js("(() => { const c = document.querySelector('.notes-ed__head button[aria-label=\"Закрыть\"]'); "
             "if (c) c.click(); return Boolean(c); })()")
        time.sleep(0.4)
        p.js("(() => { const c = [...document.querySelectorAll('.dialog button')]"
             ".find((b) => /^Не сохранять$/.test(String(b.textContent || '').trim())); "
             "if (c) c.click(); return Boolean(c); })()")
        # STAB-3: закрытие с ожиданием, а не один клик по таймеру. Запись может
        # появиться в API раньше, чем вернётся ответ сохранения: в этот момент
        # блокнот занят (`if (busy) return` в requestClose) или ещё не показал
        # полосу «Не сохранять», и окно остаётся открытым — а модальная подложка
        # потом ломает все следующие шаги (наблюдение: ~1 из 10 прогонов,
        # «блокнот особенностей: окно закрылось → осталось открытым», затем шаг d
        # видел текст блокнота вместо подтверждения). Жмём в цикле, пока окно не
        # исчезнет: видимую полосу «Не сохранять» — иначе «Закрыть».
        deadline = time.time() + 8
        while time.time() < deadline:
            gone = p.js(r"""(() => {
              if (!document.querySelector('.notes-ed')) return true;
              const bar = document.querySelector('.notes-ed__leave');
              const shown = bar && !bar.classList.contains('hidden')
                && bar.getBoundingClientRect().height > 1;
              if (shown) {
                const drop = [...bar.querySelectorAll('button')]
                  .find((b) => /^Не сохранять$/.test(String(b.textContent || '').trim()));
                if (drop) { drop.click(); return false; }
              }
              const c = document.querySelector('.notes-ed__head button[aria-label="Закрыть"]');
              if (c) c.click();
              return false;
            })()""")
            if gone:
                break
            time.sleep(0.2)
        res.check("блокнот особенностей: окно закрылось", not bool(p.js("Boolean(document.querySelector('.notes-ed'))")),
                  "осталось открытым" if p.js("Boolean(document.querySelector('.notes-ed'))") else "закрыто",
                  "закрыто", "notes-editor")
        p.js("(() => { const c = [...document.querySelectorAll('.roomdlg__head button, .dialog__foot button')]"
             ".find((b) => /^Отмена$/.test(String(b.textContent || '').trim())); "
             "if (c) { c.click(); return true; } return false; })()")
        deadline = time.time() + 5
        while time.time() < deadline and p.js("Boolean(document.querySelector('.dialog.roomdlg-shell, .dialog'))"):
            time.sleep(0.15)
    except AssertionError as exc:
        res.check("блокнот особенностей: окно открылось", False, str(exc)[:300], "окно", "notes-editor")
    errors = p.http_errors(mark_create)
    res.check("создание комнаты: нет ответов 4xx/5xx", not errors,
              "; ".join(f"{e['status']} {e['url']}" for e in errors[:3]) or "чисто", "0", "rooms")
    state["shot_dialog"] = shot


def health(p: Probe):
    status, data, raw = p.api("/api/health")
    ok = status == 200 and isinstance(data, dict) and bool(data.get("ok"))
    return ok, f"{status} {raw[:120]!r}", "200 ok=true"


# ─────────────────────────────────────────────────── b. бриф из композера

def step_b(p: Probe, res: Result, state: dict) -> None:
    room = state["room_id"]
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/composer",
           "Boolean(document.querySelector('#cmp-input'))")
    filled = p.js(r"""
    (() => {
      const t = document.querySelector('#cmp-input');
      if (!t) return {ok: false};
      t.focus(); t.value = %s;
      t.dispatchEvent(new Event('input', {bubbles: true}));
      const wrap = document.querySelector('.cmp-brief');
      const brief = document.querySelector('#cmp-brief');
      const visible = Boolean(wrap) && !wrap.classList.contains('hidden');
      const before = brief ? brief.checked : null;
      if (brief && !brief.checked) brief.click();
      return {ok: true, len: t.value.length, briefVisible: visible, before,
              after: brief ? brief.checked : null,
              hint: (() => { const n = document.querySelector('.cmp-brief__hint'); return n ? !n.classList.contains('hidden') : null; })(),
              send: (() => { const b = document.querySelector('.cmp-send'); return b ? !b.disabled : null; })()};
    })()
    """ % json.dumps(BRIEF_TEXT))
    res.check("композер: текст брифа введён", bool(filled and filled.get("ok")),
              json.dumps(filled, ensure_ascii=False)[:300], "поле #cmp-input", "composer")
    res.check("композер: флажок «Это бриф проекта» виден", bool(filled.get("briefVisible")),
              json.dumps(filled, ensure_ascii=False)[:300], "виден", "composer")
    res.check("композер: флажок «Это бриф проекта» включён", filled.get("after") is True,
              f"before={filled.get('before')} after={filled.get('after')}", "checked", "composer")
    res.check("композер: подсказка брифа показана", filled.get("hint") is True,
              f"hint={filled.get('hint')}", "true", "composer")
    shot = p.shot("w9-02-composer-brief")
    if shot:
        res.screenshots.append(shot)
    mark = p.cdp.event_index()
    p.js("(() => { const b = document.querySelector('.cmp-send'); if (b) { b.click(); return true; } return false; })()")
    brief_rid = ""
    deadline = time.time() + 30
    while time.time() < deadline:
        href = str(p.js("location.hash") or "")
        m = re.match(r"#/room/[^/]+/request/(t_[0-9a-f]+)", href)
        if m:
            brief_rid = m.group(1)
            break
        time.sleep(0.15)
    res.check("композер: запрос-бриф создан и открыт", bool(brief_rid), brief_rid,
              "#/room/<id>/request/t_…", "composer")
    if not brief_rid:
        return
    state["brief_rid"] = brief_rid
    app = state.get("app")
    live = (getattr(app, "live", {}) or {}).get(brief_rid) if app is not None else None
    res.check("запрос создан именно как бриф (brief=true в теле)",
              bool(live and live.get("brief") is True),
              json.dumps({"brief": (live or {}).get("brief"), "due": bool((live or {}).get("brief_due"))},
                         ensure_ascii=False), "brief=true", "composer")
    status, detail, raw = p.api("/api/requests/" + brief_rid)
    res.check("API: страница брифа открывается", status == 200 and isinstance(detail, dict),
              f"{status} {raw[:120]!r}", "200", "composer")
    errors = p.http_errors(mark)
    res.check("создание брифа: нет ответов 4xx/5xx", not errors,
              "; ".join(f"{e['status']} {e['url']}" for e in errors[:3]) or "чисто", "0", "composer")


# ─────────────────────────────────────────── c. черновик карты и «Части работы»

def wait_plan(p: Probe, room: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, data, _raw = p.api("/api/rooms/" + quote(room) + "/plan")
        plan = (data or {}).get("plan") if isinstance(data, dict) else None
        last = plan or {}
        if plan and plan.get("streams"):
            return plan
        time.sleep(0.3)
    return last


def step_c(p: Probe, res: Result, state: dict) -> None:
    room, brief = state["room_id"], state["brief_rid"]
    plan = wait_plan(p, room, 40)
    res.check("карта: руководитель предложил черновик", bool(plan.get("streams")),
              json.dumps({"status": plan.get("status"), "n": len(plan.get("streams") or [])},
                         ensure_ascii=False), "≥ 3 направлений", "brief")
    if not plan.get("streams"):
        res.notes.append("Черновик карты не появился за 40 с: мок доставляет его через 5–8 с "
                         "(при --phase-scale 0.4 — 2–3 с) при чтении карты или запроса.")
        return
    state["plan"] = plan
    streams = plan["streams"]
    res.check("карта: направлений 3–8", 3 <= len(streams) <= 8, str(len(streams)), "3–8", "brief")
    res.check("карта: у направлений есть зависимости",
              any(s.get("depends_on") for s in streams),
              json.dumps([[s.get("id"), s.get("depends_on")] for s in streams], ensure_ascii=False),
              "хотя бы одно «после …»", "brief")
    res.check("карта: все направления ждут запуска (proposed)",
              all(s.get("status") == "proposed" for s in streams),
              json.dumps([s.get("status") for s in streams], ensure_ascii=False), "proposed", "brief")

    # страница брифа: плашка «Предложенная карта»
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/request/{brief}",
           "Boolean(document.querySelector('.req__title'))")
    try:
        p.wait_js("Boolean(document.querySelector('.plan-card'))", 15, "плашки «Предложенная карта»")
    except AssertionError:
        pass
    card = p.js(r"""
    (() => {
      const c = document.querySelector('.plan-card');
      if (!c) return {found: false, title: (document.querySelector('.req__title') || {}).textContent || ''};
      return {found: true,
              title: ((c.querySelector('.plan-card__title') || {}).textContent || '').trim(),
              count: c.querySelectorAll('.plan-stream').length,
              chip: ((c.querySelector('.plan-card__count') || {}).textContent || '').trim(),
              launch: ((c.querySelector('.plan-card__launch') || {}).textContent || '').trim(),
              first: ((c.querySelector('.plan-stream__title') || {}).textContent || '').trim(),
              deps: [...c.querySelectorAll('.plan-stream__meta-item')].map((n) => n.textContent.trim()).filter((t) => t.startsWith('после'))};
    })()
    """) or {}
    res.check("бриф: плашка «Предложенная карта» на месте", bool(card.get("found")),
              json.dumps(card, ensure_ascii=False)[:400], "плашка", "brief")
    res.check("бриф: заголовок плашки «Предложенная карта»",
              str(card.get("title")) == "Предложенная карта", str(card.get("title")),
              "Предложенная карта", "brief")
    res.check("бриф: в плашке все направления карты", int(card.get("count") or 0) == len(streams),
              f"{card.get('count')} из {len(streams)}", str(len(streams)), "brief")
    res.check("бриф: у направлений видна зависимость «после …»", bool(card.get("deps")),
              json.dumps(card.get("deps"), ensure_ascii=False)[:300], "«после …»", "brief")
    res.check("бриф: кнопка «Запустить всё» в плашке", "Запустить всё" in str(card.get("launch")),
              str(card.get("launch")), "Запустить всё", "brief")
    note_text = str((p.js("(() => { const n = document.querySelector('.plan-card__note'); "
                          "return n ? n.textContent.trim() : ''; })()") or ""))
    if re.search(r"\d+\s+направлени\w*\s+ждёт", note_text):
        res.notes.append("Плашка карты: «%s» — глагол не согласован с числом (plan-req.js: "
                         "к countNoun(...) приклеено «ждёт запуска»)." % note_text[:80])
    owner = str((p.js("(() => { const n = [...document.querySelectorAll('.plan-stream__meta-item')]"
                      ".find((x) => /ответственный/.test(x.textContent)); "
                      "return n ? n.textContent.trim() : ''; })()") or ""))
    if re.search(r"ответственный:\s*[a-z-]+$", owner):
        res.notes.append("Плашка карты: «%s» — whoName() срезает «team-» и оставляет латинский "
                         "огрызок вместо человеческого имени (plan-req.js)." % owner[:80])
    p.js("(() => { const c = document.querySelector('.plan-card'); "
         "if (c) c.scrollIntoView({block: 'center'}); return true; })()")
    time.sleep(0.3)
    shot = p.shot("w9-03-brief-plan-card")
    if shot:
        res.screenshots.append(shot)

    # вкладка «Карта проекта»: блок «Части работы»
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/roadmap",
           "Boolean(document.querySelector('.ps, .card'))")
    try:
        p.wait_js("Boolean(document.querySelector('.ps .ps-card'))", 15, "карточек направлений")
    except AssertionError:
        pass
    ps = p.js(r"""
    (() => {
      const c = document.querySelector('.ps');
      if (!c) return {found: false};
      return {found: true,
              head: ((c.querySelector('.card__title') || {}).textContent || '').trim(),
              count: c.querySelectorAll('.ps-card').length,
              launchAll: ((c.querySelector('.ps-launch-all') || {}).textContent || '').trim(),
              chips: [...c.querySelectorAll('[data-stream-status]')].map((n) => n.getAttribute('data-stream-status'))};
    })()
    """) or {}
    res.check("карта проекта: блок «Части работы»", str(ps.get("head")) == "Части работы",
              json.dumps(ps, ensure_ascii=False)[:400], "Части работы", "roadmap")
    res.check("карта проекта: карточки всех частей работы", int(ps.get("count") or 0) == len(streams),
              f"{ps.get('count')} из {len(streams)}", str(len(streams)), "roadmap")
    res.check("карта проекта: кнопка «Запустить всё»",
              "Запустить всё" in str(ps.get("launchAll")), str(ps.get("launchAll")),
              "Запустить всё", "roadmap")
    empty = str((p.js(r"(() => { const n = [...document.querySelectorAll('.empty')]"
                      r".find((x) => /Карта проекта пока пуста/.test(x.textContent || '')); "
                      r"return n ? n.innerText.replace(/\s+/g, ' ').trim().slice(0, 120) : ''; })()") or ""))
    if empty:
        res.notes.append("Карта проекта: под «Частями работы» остаётся пустой блок «%s» с кнопкой "
                         "«Задать первый запрос», хотя направления уже есть (roadmap.js)." % empty[:100])
    shot = p.shot("w9-04-roadmap-streams")
    if shot:
        res.screenshots.append(shot)
    errors = p.http_errors(0)
    res.check("страницы карты: нет ответов 4xx/5xx", not errors,
              "; ".join(f"{e['status']} {e['url']}" for e in errors[:3]) or "чисто", "0", "roadmap")


# ───────────────────────────────────────────── d. «Запустить всё» и очередь

def step_d(p: Probe, res: Result, state: dict) -> None:
    room = state["room_id"]
    plan = state["plan"]
    streams = plan["streams"]
    deps = {s["id"]: list(s.get("depends_on") or []) for s in streams}
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/roadmap",
           "Boolean(document.querySelector('.ps-launch-all'))")
    mark = p.cdp.event_index()
    p.js("(() => { const b = document.querySelector('.ps-launch-all'); if (b) { b.click(); return true; } return false; })()")
    try:
        p.wait_js("Boolean(document.querySelector('.dialog'))", 8, "подтверждения запуска")
    except AssertionError:
        res.check("«Запустить всё»: подтверждение открылось", False, "диалога нет", ".dialog", "roadmap")
        return
    dlg = p.js(r"""
    (() => {
      const d = document.querySelector('.dialog');
      const text = (d.innerText || '').replace(/\s+/g, ' ').trim();
      const btns = [...d.querySelectorAll('button')].map((b) => (b.textContent || '').trim());
      return {title: ((d.querySelector('.dialog__title') || {}).textContent || '').trim(),
              text, btns};
    })()
    """) or {}
    res.check("подтверждение: заголовок про запуск направлений",
              "Запустить" in str(dlg.get("title")), str(dlg.get("title")), "«Запустить …?»", "confirm")
    res.check("подтверждение: сказано, сколько направлений начнут",
              "Начнут работу" in str(dlg.get("text")), str(dlg.get("text"))[:300],
              "«Начнут работу N направлений»", "confirm")
    res.check("подтверждение: сказано про одновременность и лимит",
              "Одновременно — до %d" % MAX_PARALLEL in str(dlg.get("text")),
              str(dlg.get("text"))[:300], "«Одновременно — до %d»" % MAX_PARALLEL, "confirm")
    if re.search(r"до\s+1\s+направлений", str(dlg.get("text"))):
        res.notes.append("Подтверждение запуска: «до 1 направлений» — неверная форма после «до» "
                         "(plan-streams.js launchConfirm → fromForm(limit, …)).")
    if re.search(r"\d+\s+направлений", str(dlg.get("text"))) and "настоящие агенты" in str(dlg.get("text")):
        res.notes.append("Подтверждение обещает «настоящие агенты команды», хотя на моке агенты "
                         "не запускаются: текст годится для боевого, для демо вводит в заблуждение.")
    # ждём конец анимации появления (160 мс), иначе скриншот ловит полупрозрачный диалог
    try:
        p.wait_js("(() => { const d = document.querySelector('.dialog'); "
                  "return Boolean(d) && Number(getComputedStyle(d).opacity) >= 0.99; })()",
                  5, "анимации диалога")
        dlg["opacity"] = p.js("(() => { const d = document.querySelector('.dialog'); "
                              "return d ? getComputedStyle(d).opacity : ''; })()")
    except AssertionError:
        pass
    shot = p.shot("w9-05-launch-confirm")
    if shot:
        res.screenshots.append(shot)
    clicked = p.js(r"""
    (() => {
      const d = document.querySelector('.dialog');
      if (!d) return false;
      const b = [...d.querySelectorAll('button')].find((x) => (x.textContent || '').trim() === 'Запустить всё');
      if (!b) return false;
      b.click(); return true;
    })()
    """)
    res.check("подтверждение: кнопка «Запустить всё» нажата", bool(clicked), str(clicked),
              "true", "confirm")

    # следим за очередью: лимит параллельности и зависимости
    samples: list[dict] = []
    max_running = 0
    started_ids: list[str] = []
    bad_dep: list[str] = []
    deadline = time.time() + 45
    done_at: dict[str, float] = {}
    while time.time() < deadline:
        status, data, _raw = p.api("/api/rooms/" + quote(room) + "/plan")
        live = (data or {}).get("plan") or {}
        rows = {s["id"]: s.get("status") for s in live.get("streams") or []}
        if rows:
            running = sum(1 for st in rows.values() if st in ("running", "review"))
            max_running = max(max_running, running)
            for sid, st in rows.items():
                if st in ("running", "review") and sid not in started_ids:
                    started_ids.append(sid)
                if st == "done" and sid not in done_at:
                    done_at[sid] = time.time()
                if st in ("running", "review"):
                    for dep in deps.get(sid, []):
                        if not (done_at.get(dep) or rows.get(dep) == "done"):
                            bad_dep.append(f"{sid} стартовал до {dep}")
            samples.append({"at": round(time.time(), 1), "statuses": dict(rows), "running": running})
            if running == 0 and started_ids and all(st in ("done", "dropped") for st in rows.values()):
                break
        time.sleep(0.25)

    res.check("очередь: хотя бы одно направление запущено", bool(started_ids),
              json.dumps(started_ids, ensure_ascii=False), "≥ 1", "queue")
    res.check("очередь: лимит параллельности не превышен", max_running <= MAX_PARALLEL,
              f"max одновременно={max_running} при лимите {MAX_PARALLEL}", f"≤ {MAX_PARALLEL}", "queue")
    waited = [s for s in samples
              if s["statuses"] and s["running"] >= MAX_PARALLEL
              and any(st == "queued" for st in s["statuses"].values())]
    res.check("очередь: лимит сдерживал — при занятых местах были ждущие", bool(waited),
              f"сэмплов с очередью: {len(waited)} из {len(samples)}, max_running={max_running}",
              "направление ждало свободного места", "queue")
    res.check("очередь: зависимость соблюдена", not bad_dep,
              "; ".join(sorted(set(bad_dep))[:3]) or "чисто", "зависимое — только после закрытия", "queue")
    dependent = [sid for sid, dp in deps.items() if dp]
    if dependent:
        res.check("очередь: зависимое направление всё же стартовало после зависимости",
                  all(sid in started_ids for sid in dependent),
                  f"started={started_ids} deps={deps}", "зависимые запущены", "queue")
    res.check("очередь: направления дошли до «готово»", bool(done_at),
              f"done={sorted(done_at)}", "≥ 1 done", "queue")

    status, data, _raw = p.api("/api/rooms/" + quote(room) + "/plan")
    live = (data or {}).get("plan") or {}
    state["plan2"] = live
    subs = [s for s in live.get("streams") or [] if s.get("request_id")]
    res.check("запуск: у направлений появились под-запросы", bool(subs),
              json.dumps([(s["id"], s.get("request_id")) for s in live.get("streams") or []],
                         ensure_ascii=False)[:400], "request_id у запущенных", "queue")
    if subs:
        state["stream"], state["sub_rid"] = subs[0]["id"], subs[0]["request_id"]
    errors = p.http_errors(mark)
    res.check("запуск: нет ответов 4xx/5xx", not errors,
              "; ".join(f"{e['status']} {e['url']}" for e in errors[:3]) or "чисто", "0", "queue")
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/roadmap",
           "Boolean(document.querySelector('.ps .ps-card'))")
    shot = p.shot("w9-06-streams-running")
    if shot:
        res.screenshots.append(shot)
    state["queue_samples"] = samples[-6:]


# ─────────────────────────────────── e. под-запрос: крошка, диалог, журнал

def record_and_stop(p: Probe, field_id: str, seconds: float = 1.5) -> dict:
    btn = "document.querySelector('[data-mic-btn][data-mic-for=\"%s\"]')" % field_id
    clicked = p.js("(() => { const b = %s; if (b) { b.click(); return true; } return false; })()" % btn)
    if not clicked:
        return {"ok": False, "why": "кнопки микрофона нет"}
    end = time.time() + 10
    state = ""
    while time.time() < end:
        state = str(p.js("(() => { const b = %s; return b ? b.getAttribute('data-state') : ''; })()" % btn) or "")
        if state == "recording":
            break
        if state == "error":
            panel = p.js("(() => { const n = document.querySelector('.mic-panel'); return n ? n.innerText : ''; })()")
            return {"ok": False, "why": "запись не началась", "panel": panel}
        time.sleep(0.1)
    if state != "recording":
        return {"ok": False, "why": "состояние не recording: " + state}
    time.sleep(seconds)
    stopped = p.js(r"""
    (() => {
      const b = [...document.querySelectorAll('.mic-panel .btn')]
        .find((x) => (x.textContent || '').trim().startsWith('Стоп'));
      if (!b) return false;
      b.click(); return true;
    })()
    """)
    return {"ok": True, "stopped": bool(stopped)}


def step_e(p: Probe, res: Result, state: dict) -> None:
    room = state["room_id"]
    sub = state.get("sub_rid")
    if not sub:
        res.check("под-запрос: направление с под-запросом найдено", False, "нет request_id",
                  "хотя бы одно", "subrequest")
        return
    stream_title = next((s["title"] for s in (state["plan2"].get("streams") or [])
                         if s["id"] == state["stream"]), "")
    mark = p.cdp.event_index()
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/request/{sub}",
           "Boolean(document.querySelector('.req__title'))")
    # У-1 (волна 21): «Диалог по запросу» по умолчанию СВЁРНУТ, лента и поле строятся
    # при раскрытии. Раскрываем как человек — кликом по заголовку блока.
    opened = p.open_thread()
    res.check("«Диалог»: свёрнут по умолчанию и раскрывается кликом по заголовку",
              bool(opened.get("found")) and bool(opened.get("open")) and opened.get("aria") == "true",
              json.dumps(opened, ensure_ascii=False)[:300], "data-open=1, aria-expanded=true",
              "subrequest")
    p.wait_js("Boolean(document.querySelector('.thr__input'))", 15, "блока «Диалог»")
    crumbs = p.js(r"""
    (() => {
      const all = [...document.querySelectorAll('.req-scope-crumbs')].map((n) => {
        const r = n.getBoundingClientRect();
        return {text: (n.innerText || '').replace(/\s+/g, ' ').trim(),
                seps: n.querySelectorAll('.req-scope-crumbs__sep').length,
                top: Math.round(r.top), h: Math.round(r.height), w: Math.round(r.width),
                visible: r.width > 0 && r.height > 0};
      });
      const shown = all.filter((c) => c.visible).sort((a, b) => a.top - b.top);
      const mine = shown.find((c) => /Бриф/.test(c.text)) || shown[0] || null;
      return {found: Boolean(mine), count: all.length, all,
              text: mine ? mine.text : '', seps: mine ? mine.seps : 0,
              items: mine ? mine.text.split('›').map((x) => x.trim()).filter(Boolean) : [],
              header: (document.querySelector('.req__head') ? 'req__head' : '')};
    })()
    """) or {}
    res.check("под-запрос: крошка «Комната › Бриф › Часть работы»", bool(crumbs.get("found")),
              json.dumps(crumbs, ensure_ascii=False)[:500], "навигационная крошка", "subrequest")
    text = str(crumbs.get("text") or "")
    res.check("крошка: три уровня и разделители", int(crumbs.get("seps") or 0) >= 2,
              f"seps={crumbs.get('seps')} text={text[:200]}", "2 разделителя ›", "subrequest")
    res.check("крошка: комната, бриф, часть работы по-русски",
              "Комната" in text and "Бриф" in text and "Часть работы" in text
              and (not stream_title or stream_title[:18] in text),
              text[:300], f"Комната › Бриф › Часть работы «{stream_title[:30]}»", "subrequest")
    res.check("под-запрос: нет id вида t_… в крошке", "t_" not in text, text[:200], "без id", "subrequest")
    res.check("крошка: одна видимая крошка области страницы", int(crumbs.get("count") or 0) == 1,
              json.dumps(crumbs.get("all"), ensure_ascii=False)[:400], "1", "subrequest")
    p.js("window.scrollTo(0, 0)")
    time.sleep(0.2)
    shot = p.shot("w9-07-subrequest-crumb")
    if shot:
        res.screenshots.append(shot)

    dialog = p.js(r"""
    (() => {
      const s = document.querySelector('.thr');
      if (!s) return {found: false};
      const ta = s.querySelector('#thr-input');
      const btn = s.querySelector('[data-mic-btn]');
      return {found: true, head: ((s.querySelector('.thr__title') || {}).textContent || '').trim(),
              hint: ((s.querySelector('.thr__hint') || {}).textContent || '').trim().slice(0, 80),
              hasArea: Boolean(ta), label: ((s.querySelector('.thr__send') || {}).textContent || '').trim(),
              mic: Boolean(btn)};
    })()
    """) or {}
    res.check("«Диалог»: блок и поле ввода", bool(dialog.get("found")) and bool(dialog.get("hasArea")),
              json.dumps(dialog, ensure_ascii=False)[:400], "поле #thr-input", "subrequest")
    res.check("«Диалог»: кнопка отправки руководителю",
              "Отправить руководителю" in str(dialog.get("label")), str(dialog.get("label")),
              "Отправить руководителю", "subrequest")

    rec = record_and_stop(p, "thr-input", 1.6)
    res.check("диктовка в «Диалог»: запись пошла с поддельного микрофона", bool(rec.get("ok")),
              json.dumps(rec, ensure_ascii=False)[:300], "recording → Стоп", "subrequest")
    got = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        got = str(p.js("(() => { const t = document.querySelector('#thr-input'); return t ? t.value : ''; })()") or "")
        if got:
            break
        time.sleep(0.15)
    res.check("диктовка: текст встал в поле «Диалог»", bool(got) and STT_TEXT[:20] in got,
              got[:200], STT_TEXT[:60], "subrequest")
    shot = p.shot("w9-08-subrequest-dictated")
    if shot:
        res.screenshots.append(shot)

    mark_send = p.cdp.event_index()
    p.js("(() => { const b = document.querySelector('.thr__send'); if (b) { b.click(); return true; } return false; })()")
    labels, label = [], ""
    deadline = time.time() + 25
    while time.time() < deadline:
        snap = p.js(r"""
        (() => {
          const b = document.querySelector('.thr__send');
          if (!b) return {label: '', busy: '', disabled: true};
          return {label: (b.textContent || '').trim(), busy: b.getAttribute('aria-busy'),
                  disabled: b.disabled};
        })()
        """) or {}
        label = str(snap.get("label") or "")
        labels.append(label)
        if label == "Отправить руководителю" and snap.get("busy") == "false" and not snap.get("disabled"):
            if len(labels) > 3:
                break
        time.sleep(0.15)
    seen_sending = any("Отправля" in x for x in labels)      # «Отправляется…» / «Отправляю…»
    res.check("отправка: кнопка показала «Отправляется…»", seen_sending,
              f"наблюдали: {sorted(set(labels))}", "«Отправляется…» хотя бы раз", "subrequest")
    res.check("отправка: кнопка не залипла на «Отправляется…»", label == "Отправить руководителю",
              f"итог: «{label}», история: {sorted(set(labels))[:6]}", "Отправить руководителю", "subrequest")
    feed = p.js(r"""
    (() => {
      const t = [...document.querySelectorAll('.thr-msg__text')].map((n) => (n.textContent || '').trim());
      return {count: t.length, last: t.slice(-2)};
    })()
    """) or {}
    res.check("отправка: сообщение видно в ленте диалога",
              any(STT_TEXT[:20] in x for x in (feed.get("last") or [])),
              json.dumps(feed, ensure_ascii=False)[:400], "моё сообщение в ленте", "subrequest")
    body_text = str((p.js("(() => { const n = document.querySelector('#view'); "
                          "return n ? n.innerText.slice(0, 4000) : ''; })()") or ""))
    if re.search(r"Бриф:\s*t_[0-9a-f]+", body_text):
        res.notes.append("Текст под-запроса (его читает агент) содержит технический id брифа "
                         "«Бриф: t_…» и «Карта: 3 направлений» — id на экран/в текст не нужен, "
                         "форма слова тоже неверная (mock_plan._launch).")
    errors = p.http_errors(mark_send)
    res.check("отправка: нет ответов 4xx/5xx", not errors,
              "; ".join(f"{e['status']} {e['url']}" for e in errors[:3]) or "чисто", "0", "subrequest")
    shot = p.shot("w9-09-subrequest-sent")
    if shot:
        res.screenshots.append(shot)

    # журнал: запись плана «добавил»
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/journal",
           "Boolean(document.querySelector('.journal__list, .journal'))")
    added = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        added = str(p.js(r"""
        (() => {
          const rows = [...document.querySelectorAll('.journal-event')].map((n) => ({
            text: ((n.querySelector('.journal-event__text') || {}).textContent || '').trim(),
            kind: n.getAttribute('data-kind'),
            actor: ((n.querySelector('.journal-event__actor') || {}).textContent || '').trim()}));
          const hit = rows.find((r) => /добавил часть работы/i.test(r.text));
          return hit ? JSON.stringify({hit, total: rows.length}) : '';
        })()
        """) or "")
        if added:
            break
        time.sleep(0.3)
    res.check("журнал: запись «добавил часть работы …» появилась", bool(added),
              added[:400] or "записи нет за 20 с", "«добавил часть работы …, потому что …»", "journal")
    if added:
        try:
            info = json.loads(added)
            res.check("журнал: у записи человеческий автор", bool(info["hit"].get("actor")),
                      json.dumps(info["hit"], ensure_ascii=False)[:300], "автор по-русски", "journal")
            res.check("журнал: сказано, что именно добавили",
                      "«" in info["hit"]["text"], info["hit"]["text"][:200], "название части работы", "journal")
            if re.search(r"потому что\s+[Дд]обавил", info["hit"]["text"]):
                res.notes.append("Журнал: запись тавтологична — «добавил часть работы «X», потому что "
                                 "Добавил часть работы «X» по сообщению Михаила…»: plan-req.planLogEvents "
                                 "приклеивает «, потому что» к тексту, который уже начинается с «Добавил» "
                                 "(так его пишет mock_plan._log).")
        except (ValueError, KeyError):
            pass
    status, plan_now, _raw = p.api("/api/rooms/" + quote(room) + "/plan")
    log = (plan_now or {}).get("plan", {}).get("log") if isinstance(plan_now, dict) else None
    kinds = [i.get("kind") for i in log or []]
    res.check("журнал плана: есть записи created/launched/added",
              all(k in kinds for k in ("created", "launched", "added")),
              json.dumps(kinds[-8:], ensure_ascii=False), "created, launched, added", "journal")
    shot = p.shot("w9-10-journal-added")
    if shot:
        res.screenshots.append(shot)


# ──────────────────────────────────────────────── f. «От вас нужно»: шаг и «Ответить»

def step_f(p: Probe, res: Result, state: dict) -> None:
    room, sub = state["room_id"], state.get("sub_rid")
    if not sub:
        res.check("«От вас нужно»: под-запрос найден", False, "нет", "есть", "next")
        return
    # шаги/вопросы живого под-запроса появляются в фазе «готово» (мок: 35 с × scale)
    have = False
    deadline = time.time() + 45
    while time.time() < deadline:
        status, detail, _raw = p.api("/api/requests/" + sub)
        if status == 200 and isinstance(detail, dict) and detail.get("next_steps"):
            have = True
            break
        time.sleep(0.5)
    res.check("«От вас нужно»: у под-запроса появились шаги", have,
              "next_steps пуст" if not have else "ok", "≥ 1 шаг", "next")
    if not have:
        return
    p.goto(f"{p.base}/?theme=light#/room/{quote(room)}/request/{sub}",
           "Boolean(document.querySelector('.req__title'))")
    try:
        p.wait_js("Boolean(document.querySelector('.req-step__open'))", 15, "шага в «От вас нужно»")
    except AssertionError:
        res.check("«От вас нужно»: шаг отрисован", False, "нет .req-step__open", "строка-шаг", "next")
        return
    # W14-H §12: галочек у шагов больше нет — статус ведёт руководитель, а владелец
    # действует в окне шага («Согласен» / «Прокомментировать» / «Переместить» /
    # «Не согласен — убрать»). Смысл проверки прежний: действие над шагом меняет
    # строку сразу, без перезагрузки, и уходит без 404.
    boxes = p.js("document.querySelectorAll('.req-step input[type=checkbox]').length")
    res.check("«От вас нужно»: у шага нет галочки", not boxes,
              f"{boxes} чекбоксов в шагах", "0 (решает окно шага)", "next")

    mark = p.cdp.event_index()
    p.js("(() => { window.__e2eNoReload = 'alive'; return true; })()")
    step = p.js(r"""
    (() => {
      const row = document.querySelector('.req-step');
      const open = row ? row.querySelector('.req-step__open') : null;
      if (!open) return {ok: false};
      const text = ((row.querySelector('.req-step__text') || {}).textContent || '').trim().slice(0, 60);
      open.scrollIntoView({block: 'center'});
      open.click();
      return {ok: true, text, id: open.getAttribute('data-step-id')};
    })()
    """) or {}
    res.check("«От вас нужно»: строка шага открывает окно шага", bool(step.get("ok")),
              json.dumps(step, ensure_ascii=False)[:300], "клик → окно шага", "next")
    if not step.get("ok"):
        return
    try:
        p.wait_js("Boolean(document.querySelector('.stepdlg'))", 10, "окна шага")
    except AssertionError:
        res.check("«От вас нужно»: окно шага открылось", False, "нет .stepdlg", "окно шага", "next")
        return
    dlg = p.js(r"""
    (() => {
      const d = document.querySelector('.stepdlg');
      if (!d) return {ok: false};
      const acts = [...d.querySelectorAll('.stepdlg__act')].map((b) => (b.textContent || '').trim());
      return {ok: true, acts,
              checkboxes: d.querySelectorAll('input[type=checkbox]').length,
              state: ((d.querySelector('[data-step-state]') || {}).textContent || '').trim()};
    })()
    """) or {}
    want = ["Согласен", "Прокомментировать", "Переместить", "Не согласен — убрать"]
    res.check("«От вас нужно»: в окне шага все четыре действия",
              bool(dlg.get("ok")) and all(w in (dlg.get("acts") or []) for w in want),
              json.dumps(dlg, ensure_ascii=False)[:400], "; ".join(want), "next")
    res.check("«От вас нужно»: в окне шага нет галочки выполнения",
              bool(dlg.get("ok")) and not dlg.get("checkboxes"),
              json.dumps(dlg, ensure_ascii=False)[:200], "0 чекбоксов", "next")

    clicked = p.js(r"""
    (() => {
      const d = document.querySelector('.stepdlg');
      if (!d) return false;
      const b = [...d.querySelectorAll('.stepdlg__act')]
        .find((x) => /^Согласен$/.test((x.textContent || '').trim()));
      if (!b) return false;
      b.click();
      return true;
    })()
    """)
    res.check("«От вас нужно»: нажато «Согласен»", bool(clicked), str(clicked), "true", "next")

    after: dict = {}
    deadline = time.time() + 10
    while time.time() < deadline:
        after = p.js(r"""
        (() => {
          const row = document.querySelector('.req-step');
          const line = row ? row.querySelector('.req-step__reaction') : null;
          const st = row ? row.querySelector('.req-step__state') : null;
          const res = document.querySelector('.stepdlg__result-v');
          const toast = document.querySelector('.toasts');
          return {line: line ? (line.textContent || '').trim().slice(0, 80) : '',
                  kind: line ? String(line.className) : '',
                  state: st ? (st.textContent || '').trim() : '',
                  dlg: res ? (res.textContent || '').trim() : '',
                  toast: toast ? (toast.textContent || '').trim().slice(0, 90) : '',
                  rows: document.querySelectorAll('.req-step').length,
                  reloaded: window.__e2eNoReload !== 'alive'};
        })()
        """) or {}
        if after.get("line"):
            break
        time.sleep(0.2)
    res.check("«От вас нужно»: после «Согласен» строка меняется сразу, без перезагрузки",
              bool(after.get("line")) and after.get("reloaded") is False,
              json.dumps(after, ensure_ascii=False)[:400],
              "строка показывает ответ на шаг, страница не перезагружалась", "next")

    errors = [e for e in p.http_errors(mark) if "/actions" in e["url"]]
    res.check("«От вас нужно»: действие по шагу без 404", not errors,
              "; ".join(f"{e['status']} {e['url']}" for e in errors[:3]) or "чисто",
              "POST /api/requests/{id}/actions → 200", "next")
    # STAB-3: читаем с ожиданием. Мок пересобирает деталь на каждый GET и отдаёт
    # `next_steps` только в фазе «готово»; сразу после шага e («отправка
    # руководителю») мок по расписанию применяет доработку (`_thread_tick` →
    # `_apply_revision` → `jump("synthesizing")`) и на ~6 с отдаёт
    # `status="synthesizing"`, `next_steps: []` — хотя действие по шагу уже лежит
    # в реестре мока (наблюдение: diag-запись «200 synthesizing [] override
    # {"kind":"agree"}»). В бэкенде шаги не зависят от фазы (engine_read берёт их
    # из канонического свода карты), поэтому это гонка сценария, а не дефект
    # продукта. Проверка НЕ ослаблена: ждём именно записанное действие у шага с
    # тем же id и падаем по таймауту, показывая, что успели увидеть.
    want_id = str(step.get("id") or "")
    status, detail_now, mine, action_now = 0, {}, {}, {}
    seen: list[str] = []
    deadline = time.time() + 25
    while True:
        status, detail_now, _raw = p.api("/api/requests/" + quote(sub))
        steps_now = (detail_now or {}).get("next_steps") or []
        mine = next((s for s in steps_now if str(s.get("id")) == want_id), {})
        action_now = mine.get("user_action") if isinstance(mine.get("user_action"), dict) else {}
        good = status == 200 and (action_now.get("kind") == "agree" or bool(mine.get("reaction_pending")))
        note = "%s/%d шагов" % (str((detail_now or {}).get("status")), len(steps_now))
        if not seen or seen[-1] != note:
            seen.append(note)
        if good or time.time() >= deadline:
            break
        time.sleep(0.25)
    res.check("«От вас нужно»: ответ на шаг записан в запросе",
              status == 200 and (action_now.get("kind") == "agree" or bool(mine.get("reaction_pending"))),
              json.dumps({**{k: mine.get(k) for k in ("id", "user_action_label", "reaction_pending")},
                          "status": status, "seen": seen[-4:], "want_id": want_id},
                         ensure_ascii=False)[:400], "user_action.kind=agree", "next")

    p.js("(() => { const c = document.querySelector('.stepdlg__close'); if (c) c.click(); return true; })()")
    shot = p.shot("w9-11-next-step-done")
    if shot:
        res.screenshots.append(shot)

    # «Ответить» на вопрос + микрофон без наложения
    try:
        p.wait_js("Boolean(document.querySelector('.req-q__input'))", 10, "поля ответа на вопрос")
    except AssertionError:
        res.check("«От вас нужно»: вопрос Михаилу и поле «Ответить»", False,
                  "нет .req-q__input", "поле ответа", "next")
        return
    geo = p.js(r"""
    (() => {
      const ta = document.querySelector('.req-q__input');
      if (!ta) return {ok: false, why: 'нет поля ответа'};
      // W15: форма ответа — как в composer: поле в строке, рядом отдельные
      // «Надиктовать» (ставит dictation-auto.js) и «Отправить».
      const row = ta.closest('.req-q__row') || ta.parentNode;
      const mic = row ? row.querySelector('[data-mic-btn]') : null;
      const btn = row ? row.querySelector('.req-q__send') : null;
      const r = (n) => { const b = n.getBoundingClientRect(); return {l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width, h: b.height}; };
      if (!mic) return {ok: false, why: 'нет микрофона у поля ответа', row: Boolean(row)};
      const a = r(mic), b = r(ta);
      const ox = Math.min(a.r, b.r) - Math.max(a.l, b.l);
      const oy = Math.min(a.b, b.b) - Math.max(a.t, b.t);
      const br = btn ? r(btn) : null;
      const ob = br ? Math.min(a.r, br.r) - Math.max(a.l, br.l) : 0;
      const obY = br ? Math.min(a.b, br.b) - Math.max(a.t, br.t) : 0;
      return {ok: true, mic: a, area: b, micWidth: Math.round(a.w),
              sameRow: Boolean(row) && mic.parentNode === row && ta.parentNode === row,
              overlapField: (ox > 1 && oy > 1) ? Math.round(ox) + 'x' + Math.round(oy) : 0,
              overlapBtn: (br && ob > 1 && obY > 1) ? Math.round(ob) + 'x' + Math.round(obY) : 0,
              micInView: a.l >= -1 && a.r <= innerWidth + 1 && a.t >= -1 && a.b <= innerHeight + 1,
              btnLabel: btn ? (btn.textContent || '').trim() : '', btnVisible: Boolean(btn) && br.w > 0};
    })()
    """) or {}
    res.check("«Ответить»: микрофон у поля ответа есть", bool(geo.get("ok")),
              json.dumps(geo, ensure_ascii=False)[:400], "кнопка микрофона", "next")
    # Единый стиль поля (как в composer): микрофон — отдельная кнопка в строке с полем,
    # поэтому он не накрывает ни текст, ни «Отправить» и виден целиком.
    res.check("«Ответить»: микрофон стоит отдельной кнопкой рядом с полем",
              bool(geo.get("sameRow")),
              json.dumps({"sameRow": geo.get("sameRow")}, ensure_ascii=False),
              "микрофон и поле — соседи в .req-q__row", "next")
    res.check("«Ответить»: микрофон не накрывает поле ответа (getBoundingClientRect)",
              geo.get("ok") and geo.get("overlapField") == 0, f"пересечение={geo.get('overlapField')}", "0", "next")
    res.check("«Ответить»: микрофон не накрывает кнопку «Отправить» (getBoundingClientRect)",
              geo.get("overlapBtn") == 0, f"пересечение={geo.get('overlapBtn')}", "0", "next")
    res.check("«Ответить»: микрофон и кнопка видны целиком",
              bool(geo.get("micInView")) and geo.get("btnLabel") == "Отправить" and bool(geo.get("btnVisible")),
              json.dumps({"label": geo.get("btnLabel"), "visible": geo.get("btnVisible"),
                          "inView": geo.get("micInView")}, ensure_ascii=False),
              "«Отправить» рядом, микрофон в окне", "next")
    p.js("(() => { const n = document.querySelector('.req-q'); if (n) n.scrollIntoView({block: 'center'}); return true; })()")
    time.sleep(0.25)
    shot = p.shot("w9-12-next-answer")
    if shot:
        res.screenshots.append(shot)
    # диктовка в поле ответа: кнопка работает без наложения на кнопку «Ответить»
    mic_state = p.js(r"""
    (() => {
      const ta = document.querySelector('.req-q__input');
      const mic = ta.parentNode.querySelector('[data-mic-btn]');
      if (!mic) return '';
      mic.click();
      return mic.getAttribute('data-state') || 'clicked';
    })()
    """)
    started = False
    deadline = time.time() + 8
    while time.time() < deadline:
        st = str(p.js("(() => { const ta = document.querySelector('.req-q__input'); "
                      "const m = ta ? ta.parentNode.querySelector('[data-mic-btn]') : null; "
                      "return m ? m.getAttribute('data-state') : ''; })()") or "")
        if st == "recording":
            started = True
            break
        time.sleep(0.12)
    res.check("«Ответить»: микрофон начинает запись", started, str(mic_state), "recording", "next")
    if started:
        time.sleep(1.2)
        p.js(r"""
        (() => {
          const b = [...document.querySelectorAll('.mic-panel .btn')]
            .find((x) => (x.textContent || '').trim().startsWith('Стоп'));
          if (b) b.click();
          return Boolean(b);
        })()
        """)
        filled = ""
        deadline = time.time() + 15
        while time.time() < deadline:
            filled = str(p.js("(() => { const t = document.querySelector('.req-q__input'); return t ? t.value : ''; })()") or "")
            if filled:
                break
            time.sleep(0.15)
        res.check("«Ответить»: надиктованный текст встал в поле", bool(filled) and STT_TEXT[:20] in filled,
                  filled[:160], STT_TEXT[:50], "next")
        mark2 = p.cdp.event_index()
        sent = p.js(r"""
        (() => {
          const ta = document.querySelector('.req-q__input');
          const row = ta ? (ta.closest('.req-q__row') || ta.parentNode) : null;
          const b = row ? row.querySelector('.req-q__send') : null;
          if (!b) return false;
          b.click(); return true;
        })()
        """)
        time.sleep(1.5)
        errors = [e for e in p.http_errors(mark2) if "/actions" in e["url"]]
        res.check("«Ответить»: ответ отправлен без 4xx", bool(sent) and not errors,
                  "; ".join(f"{e['status']} {e['url']}" for e in errors[:2]) or f"sent={sent}", "200", "next")
    shot = p.shot("w9-12b-next-answered")
    if shot:
        res.screenshots.append(shot)


# ─────────────────────────────────────────────────────────────── g. адаптив

GEOMETRY_JS = r"""
(() => {
  const W = innerWidth;
  const out = {problems: [], sw: document.scrollingElement.scrollWidth, w: W, h: innerHeight};
  if (out.sw > W + 1) out.problems.push('hscroll ' + out.sw + '>' + W);
  const row = document.querySelector('.topbar__row');
  if (row) {
    const boxes = [];
    for (const node of row.children) {
      const cs = getComputedStyle(node);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const r = node.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      boxes.push({cls: String(node.className).split(' ')[0], l: r.left, rr: r.right, t: r.top, b: r.bottom});
    }
    for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      const ox = Math.min(a.rr, b.rr) - Math.max(a.l, b.l);
      const oy = Math.min(a.b, b.b) - Math.max(a.t, b.t);
      if (ox > 1 && oy > 1) out.problems.push('overlap:' + a.cls + '/' + b.cls + ' ' + Math.round(ox) + 'px');
    }
    if (row.scrollWidth > row.clientWidth + 1) out.problems.push('row overflow ' + row.scrollWidth + '>' + row.clientWidth);
  }
  for (const node of document.querySelectorAll('#view .card, #view .ps, #view .req__grid')) {
    const r = node.getBoundingClientRect();
    if (r.width > 0 && r.right > W + 2 && getComputedStyle(node).position !== 'fixed') {
      out.problems.push('past viewport: ' + String(node.className).slice(0, 30));
      break;
    }
  }
  return out;
})()
"""

RESPONSIVE_SCREENS = (
    ("brief", "/request/{brief}", ".plan-card"),
    ("roadmap", "/roadmap", ".ps"),
    ("subrequest", "/request/{sub}", ".thr"),
)


def step_g(p: Probe, res: Result, state: dict) -> None:
    room = state["room_id"]
    brief, sub = state.get("brief_rid"), state.get("sub_rid")
    if not (brief and sub):
        res.check("адаптив: экраны для замера есть", False, "нет брифа или под-запроса", "оба", "responsive")
        return
    widths = (780, 560, 360)
    for theme in ("light", "dark"):
        for name, tail, sel in RESPONSIVE_SCREENS:
            url = f"{p.base}/?theme={theme}#/room/{quote(room)}" + tail.format(brief=brief, sub=sub)
            p.goto(url, "Boolean(document.querySelector('%s'))" % sel, timeout=20)
            applied = p.js("document.documentElement.dataset.theme") or ""
            for w in widths:
                p.ch.set_viewport(w, 900)
                time.sleep(0.25)
                geo = p.js(GEOMETRY_JS) or {}
                screen = f"{name}/{theme}/{w}"
                res.check(f"адаптив {screen}: нет горизонтальной прокрутки",
                          not [x for x in (geo.get("problems") or []) if x.startswith(("hscroll", "row overflow"))],
                          f"sw={geo.get('sw')} w={geo.get('w')} · {'; '.join((geo.get('problems') or [])[:3])}",
                          "scrollWidth ≤ innerWidth", screen)
                res.check(f"адаптив {screen}: шапка без наложений",
                          not [x for x in (geo.get("problems") or []) if x.startswith("overlap")],
                          "; ".join((geo.get("problems") or [])[:3]) or "чисто", "0 наложений", screen)
                res.check(f"адаптив {screen}: ключевой блок на экране",
                          bool(p.js("(() => { const n = document.querySelector('%s'); if (!n) return false; "
                                    "const r = n.getBoundingClientRect(); return r.width > 80 && r.height > 40; })()" % sel)),
                          "блок виден", "ширина > 80", screen)
                res.check(f"адаптив {screen}: тема применена", applied == theme,
                          f"data-theme={applied}", theme, screen)
                if w == 360:
                    tabs = p.js(r"""
                    (() => {
                      const t = document.querySelector('.tabs');
                      if (!t) return {ok: false};
                      const ox = getComputedStyle(t).overflowX;
                      return {ok: true, sw: t.scrollWidth, cw: t.clientWidth, ox,
                              allReachable: t.scrollWidth <= t.clientWidth + 1 || ox === 'auto'};
                    })()
                    """) or {}
                    res.check(f"адаптив {screen}: все вкладки доступны (лента прокручивается)",
                              bool(tabs.get("allReachable")),
                              json.dumps(tabs, ensure_ascii=False), "scrollWidth ≤ clientWidth или overflow auto", screen)
            if theme == "dark" or w == 360:
                p.ch.set_viewport(360 if theme == "light" else 780, 900)
                time.sleep(0.2)
                shot = p.shot(f"w9-13-{name}-{theme}")
                if shot:
                    res.screenshots.append(shot)
    p.ch.set_viewport(1280, 900)


# ────────────────────────────────────────────────────────────────── запуск

STEP_FUNCS = {"a": step_a, "b": step_b, "c": step_c, "d": step_d, "e": step_e, "f": step_f, "g": step_g}
STEP_TITLES = {
    "a": "Комната через диалог: имя, руководитель, состав, папка, указания, «Сначала план»",
    "b": "Бриф из композера: флажок «Это бриф проекта» → запрос создан",
    "c": "Черновик карты: плашка «Предложенная карта» и блок «Части работы»",
    "d": "«Запустить всё» с подтверждением: очередь по зависимостям и лимиту",
    "e": "Под-запрос: крошка «Комната › Бриф › Часть работы», диалог с диктовкой, журнал",
    "f": "«От вас нужно»: окно шага («Согласен» меняет строку сразу) без 404, «Ответить» с микрофоном без наложения",
    "g": "Адаптив 780/560/360 на экранах карты, обе темы",
}


def write_reports(results: list[Result], meta: dict, started: float, exit_code: int,
                  path_md: Path, path_json: Path) -> None:
    duration = round(time.time() - started, 1)
    payload = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "duration_s": duration,
        "mock": meta.get("mock"),
        "port": meta.get("port"),
        "phase_scale": meta.get("phase_scale"),
        "room": meta.get("room_id"),
        "brief": meta.get("brief_rid"),
        "sub_request": meta.get("sub_rid"),
        "stt": {"mode": "stub", "why": "настоящий Whisper не запускается; текст фиксированный",
                "wave": str(WAV), "text": STT_TEXT},
        "steps": [r.as_dict() for r in results],
        "queue_samples": meta.get("queue_samples") or [],
        "defect_notes": meta.get("defect_notes") or [],
        "screenshots": [s for r in results for s in r.screenshots],
        "totals": {"steps": len(results),
                   "pass": sum(1 for r in results if r.status == "PASS"),
                   "fail": sum(1 for r in results if r.status == "FAIL"),
                   "checks": sum(len(r.checks) for r in results),
                   "checks_failed": sum(r.failed for r in results),
                   "defects": sum(len(r.defects) for r in results)},
        "exit_code": exit_code,
    }
    path_json.parent.mkdir(parents=True, exist_ok=True)
    path_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# E2E «проектный режим» — мок, настоящий Chrome",
        "",
        f"Дата: {payload['started_at']} · длительность {duration} с · "
        f"итог: **{'PASS' if exit_code == 0 else 'FAIL'}**",
        "",
        f"- Мок: `{meta.get('mock')}` (порт {meta.get('port')}, состояние в памяти, на диск не пишет)",
        f"- Темп демо: `phase_scale={meta.get('phase_scale')}` (мок эволюционирует быстрее)",
        f"- Комната: `{meta.get('room_id')}` · бриф `{meta.get('brief_rid')}` · "
        f"под-запрос `{meta.get('sub_rid')}`",
        f"- Диктовка: WAV `{Path(str(WAV)).name}` в поддельный микрофон Chrome + заглушка "
        "распознавания (`mock_stt.MODULE_OVERRIDE`), настоящий Whisper НЕ запускается",
        f"- Проверок: {payload['totals']['checks']}, провалов: {payload['totals']['checks_failed']}",
        "",
        "## Шаги",
        "",
        "| # | Шаг | Результат | Проверок | Провалов | Время |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.id} | {r.title} | **{r.status}** | {len(r.checks)} | {r.failed} | "
                     f"{round((r.finished or time.time()) - r.started, 1)} с |")
    lines += ["", "## Дефекты", ""]
    defects = [(r.id, d) for r in results for d in r.defects]
    if defects:
        lines += ["Воспроизведение: `python tools/e2e_project.py --port 8883` — сценарий идёт сам "
                  "по шагам a→g; ниже экран, фактическое и ожидаемое.", ""]
        for i, (sid, d) in enumerate(defects, 1):
            lines += [f"### D{i}. {d['title']} (шаг {sid})", "",
                      f"- Экран: `{d.get('screen') or '—'}`",
                      f"- Получено: `{d.get('got', '')}`",
                      f"- Ожидалось: `{d.get('want', '')}`", ""]
    else:
        lines += ["Дефектов нет — все проверки прошли в настоящем Chrome.", ""]
    extra = meta.get("defect_notes") or []
    if extra:
        lines += ["### Детали воспроизведения", ""] + [f"- {x}" for x in extra] + [""]
    lines += ["## Как воспроизвести", "",
              "```",
              "python tools/e2e_project.py --port 8883      # все шаги a–g, ~50 с",
              "python tools/e2e_project.py --only a,d       # отдельные шаги",
              "python tools/e2e_ui.py                       # весь сквозной набор (S1–S10 + S11)",
              "```",
              "",
              "В наборе `tools/e2e_ui.py` сценарий подключён как **S11** — номер S4 занят "
              "сценарием «От вас нужно» (бэклог), поэтому новый шаг получил следующий свободный.",
              "",
              "Мок поднимается в процессе теста на 8883 (`tools/mock_server.py:create_server`), "
              "Chrome — свой headless профиль с поддельным микрофоном. Боевой 8777, демо 8778, "
              "реальная доска и конфиги не используются.", ""]
    notes = [f"- {n}" for r in results for n in r.notes]
    if notes:
        lines += ["## Наблюдения", ""] + notes + [""]
    lines += ["## Скриншоты", ""]
    shots = payload["screenshots"]
    lines += [f"- `{s}`" for s in shots] if shots else ["Нет."]
    lines += ["", "## Чужие файлы: что замечено (не правил)", "",
              "| Где | Что |", "|---|---|"]
    seen = set()
    for r in results:
        for d in r.defects:
            key = d["title"]
            if key in seen:
                continue
            seen.add(key)
            lines.append("| шаг %s · `%s` | %s |" % (r.id, d.get("screen") or "—",
                                                    str(d["title"]).replace("|", "/")))
        for n in r.notes:
            key = n[:40]
            if key in seen:
                continue
            seen.add(key)
            lines.append("| шаг %s | %s |" % (r.id, str(n).replace("|", "/")[:300]))
    lines += ["", "## Ограничения (честно)", "",
              "- Сервер — мок: под-запросы создаёт `tools/mock_plan.py`, не настоящие агенты.",
              "- Распознавание речи подменено заглушкой: проверяется цепочка "
              "«микрофон → MediaRecorder → POST /api/stt/transcribe → вставка», не качество модели.",
              "- Папка проекта на моке не создаётся и не читается (таблица демо-каталогов).",
              "- Боевой 8777, демо 8778, реальная доска и конфиги не трогались.", ""]
    path_md.parent.mkdir(parents=True, exist_ok=True)
    path_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="E2E «проектный режим» на моке (настоящий Chrome).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--shots", default=str(DEFAULT_SHOTS))
    ap.add_argument("--json", default=str(DEFAULT_JSON))
    ap.add_argument("--report", default=str(DEFAULT_MD))
    ap.add_argument("--only", default="", help="шаги через запятую: a,b,c (по умолчанию все)")
    ap.add_argument("--phase-scale", type=float, default=0.4, help="темп мока (1.0 — как в демо)")
    ap.add_argument("--keep-open", action="store_true", help="не закрывать браузер (отладка)")
    args = ap.parse_args(argv)

    only = [s.strip().lower() for s in re.split(r"[,\s]+", args.only or "") if s.strip()]
    unknown = [s for s in only if s not in STEP_FUNCS]
    if unknown:
        print("Неизвестные шаги: " + ", ".join(unknown), file=sys.stderr)
        return 2
    selected = only or list(STEPS)

    if not Path(WAV).is_file():
        print("Нет WAV для поддельного микрофона:", WAV, file=sys.stderr)
        return 2

    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    for old in shots.glob("w9-*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    started = time.time()
    state: dict = {}
    results: list[Result] = []
    meta = {"port": args.port, "mock": str(ROOT / "tools" / "mock_server.py"),
            "phase_scale": args.phase_scale}
    server = thread = chrome = None
    fatal = ""
    try:
        server, thread = start_mock(args.port, args.phase_scale)
        state["app"] = server.app       # белый ящик: что именно отправило приложение
        base = f"http://127.0.0.1:{args.port}"
        meta["mock"] = "tools/mock_server.py (create_server, в процессе теста)"
        chrome = launch_media(1280, 900, Path(WAV))
        p = Probe(chrome, base, shots)
        status, data, raw = p.api("/api/health")
        if status != 200:
            raise RuntimeError("мок не отвечает: %s %r" % (status, raw[:200]))
        for sid in selected:
            res = Result(sid, STEP_TITLES[sid])
            try:
                chrome.set_viewport(1280, 900)
                if sid in ("a",):
                    STEP_FUNCS[sid](p, res, state, args.port)
                else:
                    STEP_FUNCS[sid](p, res, state)
            except Exception as exc:  # noqa: BLE001 — шаг не должен ронять прогон
                res.error = f"{exc.__class__.__name__}: {exc}"
                res.check("шаг выполнился без исключения", False, res.error, "без ошибок", sid)
                print(traceback.format_exc()[:1500], file=sys.stderr)
            res.finished = time.time()
            results.append(res)
            print(f"{sid}: {res.status} · проверок {len(res.checks)} · провалов {res.failed}"
                  + (f" · {res.error}" if res.error else ""), flush=True)
            meta.update({k: state.get(k) for k in ("room_id", "brief_rid", "sub_rid", "queue_samples")
                         if state.get(k)})
            if sid == "f" and res.failed:
                meta["defect_notes"] = [
                    "Открыть любой запрос с шагами во вкладке «От вас нужно» (например, "
                    "`#/room/cafe/request/t_2a6f5c93`) и нажать чекбокс шага: он становится "
                    "выключенным и строка не получает класс `is-done`; ждём 8 с — без изменений. "
                    "После F5 шаг виден выполненным, чекбокс снова доступен.",
                    "Воспроизводится и «настоящей» мышью: `Input.dispatchMouseEvent` по центру "
                    "чекбокса даёт то же состояние — дело не в программном `.click()` теста.",
                    "В бэклоге комнаты шаг при этом сохранён как `done` (POST "
                    "/api/rooms/{id}/backlog/{root}:{step} → 200, без 404), т.е. данные в порядке, "
                    "страдает только вид колонки «От вас нужно».",
                    "Ожидаемое поведение: кнопка/чекбокс возвращаются в рабочее состояние, строка "
                    "помечается выполненной сразу, без перезагрузки.",
                ]
    except Exception as exc:  # noqa: BLE001 — инфраструктурный сбой
        fatal = f"{exc.__class__.__name__}: {exc}"
        print("ЗАПУСК: " + fatal, file=sys.stderr)
        print(traceback.format_exc()[:2000], file=sys.stderr)
    finally:
        if chrome is not None and not args.keep_open:
            try:
                chrome.close()
            except Exception as exc:  # noqa: BLE001
                print("! браузер не закрылся: %s" % exc, file=sys.stderr)
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None:
            thread.join(timeout=5)
        mock_stt.reset()

    if not results and fatal:
        res = Result("!", "инфраструктура")
        res.error = fatal
        res.check("мок и браузер поднялись", False, fatal, "без ошибок", "harness")
        res.finished = time.time()
        results = [res]

    exit_code = 0 if (results and all(r.status == "PASS" for r in results) and not fatal) else 1
    write_reports(results, meta, started, exit_code, Path(args.report), Path(args.json))
    total = sum(len(r.checks) for r in results)
    failed = sum(r.failed for r in results)
    print(f"\nИТОГ: {'PASS' if exit_code == 0 else 'FAIL'} · шагов "
          f"{sum(1 for r in results if r.status == 'PASS')}/{len(results)} · "
          f"проверок {total - failed}/{total} · {round(time.time() - started, 1)} с")
    print("Отчёт:", args.report)
    print("JSON: ", args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
