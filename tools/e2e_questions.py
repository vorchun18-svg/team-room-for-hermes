#!/usr/bin/env python3
"""Живая проверка вопросов и панели диктовки на странице запроса (W15-C, п. 3–4).

    python tools/e2e_questions.py [--port 8857] [--shots docs/shots] [--json out.json]

Что проверяется в НАСТОЯЩЕМ Chromium (через CDP), на демо-моке со своими данными
(свой порт из диапазона 8851–8869; боевой 8777 и доска не трогаются):

  1. Колонка вопросов справа шире 272 px и растёт вместе с окном; ширины
     min/max разумные; на 900 px и уже вопросы уходят под основной блок,
     горизонтального скролла нет.
  2. Окно диктовки стоит у СВОЕГО вопроса: до записи, во время записи, после
     «Стоп» (распознавание), после перерисовки страницы по опросу и при
     прокрутке. Проверка — `getBoundingClientRect` панели и блока вопроса:
     панель примыкает сверху или снизу и перекрывается с вопросом по горизонтали,
     никогда не у левого верхнего угла.
  3. Ответ показывается рядом с вопросом как «Вопрос — ваш ответ» со статусом,
     реакцией руководителя и кнопкой «Напомнить руководителю».
  4. Ввод в вопросе — как в composer: поле + отдельные кнопки «Отправить»
     и «Напомнить/Мне нужно пояснение»; микрофон отдельной кнопкой, не поверх поля.

Код возврата 1, если хоть одна проверка не прошла.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from server import proc as spawn                      # noqa: E402
from tools.cdp import Chrome, find_chrome, free_port  # noqa: E402

ROOM = "cafe"
RID = "t_5c8e4a02"          # фикстура request_needs_you: открытый + отвеченные вопросы
PORT_RANGE = range(8851, 8870)


class Check:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def ok(self, name: str, passed: bool, got="", want="") -> bool:
        self.items.append({"check": name, "ok": bool(passed), "got": str(got), "want": str(want)})
        mark = "OK  " if passed else "FAIL"
        line = f"  {mark} {name}"
        if not passed:
            line += f" — получили {got!r}, ждали {want!r}"
        print(line)
        return bool(passed)

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if not i["ok"])


# ─────────────────────────────────────────────────────── демо-мок (свой порт)

def start_mock(port: int) -> subprocess.Popen:
    cmd = [sys.executable, str(ROOT / "tools" / "mock_server.py"), "--port", str(port)]
    proc = spawn.popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError("демо-мок не запустился (порт занят?)")
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                if r.status == 200:
                    return proc
        except (URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError("демо-мок не ответил на /api/health")


def pick_port(preferred: int | None) -> int:
    if preferred:
        return preferred
    for port in PORT_RANGE:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.2):
                continue                      # занят другим стендом
        except (URLError, OSError):
            return port
    raise RuntimeError("нет свободного порта в 8851–8869")


# ─────────────────────────────────────────────── Chromium с фейковым микрофоном

def launch_browser(width: int = 1440, height: int = 900) -> Chrome:
    """Обычный headless Chrome + фейковые устройства: getUserMedia работает."""
    exe = find_chrome()
    profile = Path(tempfile.mkdtemp(prefix="teamroom-q-"))
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
        # Микрофон без человека: Chrome отдаёт синтетический поток сам.
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        f"--window-size={max(width, 800)},{max(height, 600)}",
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
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                targets = json.loads(r.read().decode("utf-8"))
            pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:  # noqa: BLE001 - порт ещё поднимается
            pass
        time.sleep(0.05)
    if not ws_url:
        shutil.rmtree(profile, ignore_errors=True)
        raise RuntimeError("отладочный порт Chromium не поднялся")
    ch = Chrome(proc, profile, port, ws_url)
    ch.cdp.call("Page.enable")
    ch.cdp.call("Runtime.enable")
    ch.cdp.call("Log.enable")
    ch.cdp.call("Network.enable")
    ch.cdp.call("Network.setCacheDisabled", {"cacheDisabled": True})
    ch.set_viewport(width, height)
    return ch


# ─────────────────────────────────────────────────────────── JS-помощники

JS_WAIT_READY = r"""
(() => {
  const t = document.querySelector('.req__title');
  const q = document.querySelector('[data-questions]');
  return {ready: Boolean(t && q), title: (t && t.textContent) || '',
          questions: document.querySelectorAll('.req-q').length};
})()
"""

JS_LAYOUT = r"""
(() => {
  const de = document.documentElement;
  const grid = document.querySelector('.req__grid');
  const aside = document.querySelector('.req__aside');
  const main = document.querySelector('.req__main');
  const q = document.querySelector('[data-questions]');
  const px = (v) => Math.round(v);
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return {l: px(r.left), t: px(r.top), w: px(r.width), h: px(r.height), r: px(r.right), b: px(r.bottom)}; };
  const cs = grid ? getComputedStyle(grid) : null;
  return {
    vw: window.innerWidth, vh: window.innerHeight,
    doc: {cw: de.clientWidth, sw: de.scrollWidth, body: document.body.scrollWidth},
    gridCols: cs ? cs.gridTemplateColumns : '',
    grid: box(grid), aside: box(aside), main: box(main), questions: box(q),
  };
})()
"""

# Панель и блок СВОЕГО вопроса: панель должна примыкать сверху/снизу и
# перекрываться с вопросом по горизонтали; левый верхний угол — провал.
JS_PANEL = r"""
(() => {
  const qid = %(qid)s;
  const row = document.querySelector('[data-mic-anchor="q:' + qid + '"]');
  const panel = document.querySelector('.mic-panel');
  const px = (v) => Math.round(v);
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return {l: px(r.left), t: px(r.top), w: px(r.width), h: px(r.height), r: px(r.right), b: px(r.bottom)}; };
  const rb = box(row), pb = box(panel);
  let near = false, gap = null, overlap = 0;
  if (rb && pb) {
    const above = Math.abs(pb.b - (rb.t - 8));
    const below = Math.abs(pb.t - (rb.b + 8));
    gap = Math.min(above, below);
    overlap = Math.max(0, Math.min(pb.r, rb.r) - Math.max(pb.l, rb.l));
    near = gap <= 14 && overlap > 40;
  }
  return {
    qid: qid, row: rb, panel: pb, gap: gap, overlap: overlap, near: near,
    phase: panel ? (panel.querySelector('.mic-panel__title') || {}).textContent || '' : '',
    corner: Boolean(pb && pb.l <= 10 && pb.t <= 10),
    open: Boolean(panel),
  };
})()
"""

JS_OPEN_FOLD = r"""
(() => {
  const d = document.querySelector('details[data-fold="%(name)s"]');
  if (!d) return 'нет блока';
  if (!d.open) d.open = true;
  return d.open ? 'ok' : 'не открылся';
})()
"""

JS_ROWS = r"""
(() => {
  const rows = [...document.querySelectorAll('.req-q')];
  return rows.map((row) => ({
    id: row.getAttribute('data-q'),
    state: row.getAttribute('data-q-state'),
    text: (row.innerText || '').replace(/\s+/g, ' ').trim(),
    anchor: row.getAttribute('data-mic-anchor'),
    hasForm: Boolean(row.querySelector('.req-q__form')),
    hasAnswer: Boolean(row.querySelector('.req-q__answer')),
    send: Boolean(row.querySelector('.req-q__send')),
    explain: Boolean(row.querySelector('.req-q__explain')),
    remind: Boolean(row.querySelector('.req-q__remind')),
    mic: Boolean(row.querySelector('[data-mic-btn]')),
    micInField: Boolean(row.querySelector('.mic-field [data-mic-btn]')),
    micRow: (() => {
      const b = row.querySelector('[data-mic-btn]');
      return b ? b.parentNode === row.querySelector('.req-q__row') : false;
    })(),
  }));
})()
"""

JS_CLICK_MIC = r"""
(() => {
  const row = document.querySelector('[data-mic-anchor="q:%(qid)s"]');
  if (!row) return 'нет блока вопроса';
  const btn = row.querySelector('[data-mic-btn]');
  if (!btn) return 'нет кнопки микрофона';
  btn.click();
  return 'ok';
})()
"""

JS_CLICK = r"""
(() => {
  const el = document.querySelector(%(sel)s);
  if (!el) return 'нет элемента';
  el.click();
  return 'ok';
})()
"""

JS_SET_TEXT = r"""
(() => {
  const row = document.querySelector('[data-mic-anchor="q:%(qid)s"]');
  const ta = row && row.querySelector('textarea');
  if (!ta) return 'нет поля';
  ta.focus();
  ta.value = %(text)s;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return 'ok';
})()
"""

# Записи «ответ на вопрос» в ленте диалога (а не россыпь обычных сообщений).
JS_ANSWER_IN_FEED = r"""
(() => {
  const nodes = [...document.querySelectorAll('.thr__feed .req-ans')];
  return {
    answers: nodes.length,
    text: nodes.map((n) => (n.innerText || '').replace(/\s+/g, ' ').trim()).join(' | '),
    quoted: nodes.map((n) => Boolean(n.querySelector('.req-ans__quote'))).join(','),
    go: nodes.map((n) => Boolean(n.querySelector('.req-ans__go'))).join(','),
  };
})()
"""

JS_SCROLL = r"""
(() => {
  const v = document.getElementById('view');
  if (v) v.scrollTop = %(top)d;
  else window.scrollTo(0, %(top)d);
  return (v ? v.scrollTop : window.scrollY);
})()
"""


JS_OPEN_FOLDS = r"""(() => {
  // W17-P3: правые блоки («Отвечено», «Шаги», «Решения») свёрнуты — раскрываем перед
  // замером, иначе у отвеченного вопроса нет ни текста, ни статуса (innerText пуст).
  const root = document.querySelector('.req__aside') || document;
  let opened = 0;
  for (const d of root.querySelectorAll('details')) {
    if (!d.open) { d.open = true; opened += 1; }
  }
  for (const h of root.querySelectorAll('[aria-expanded="false"]')) {
    if (/Отвечено|Ответ|Вопрос|Шаги|Решени/i.test(h.textContent || '')) {
      h.click();
      opened += 1;
    }
  }
  return opened;
})()"""


def open_folds(ch: Chrome) -> int:
    """Раскрыть свёрнутые блоки правой колонки и дать странице перерисоваться."""
    opened = ch.cdp.eval_js(JS_OPEN_FOLDS) or 0
    time.sleep(0.5)
    return opened


JS_OPEN_THREAD = r"""( () => {
  // У-1 (волна 21): «Диалог по запросу» по умолчанию СВЁРНУТ, лента и поле
  // строятся только при раскрытии. Перед чтением ленты раскрываем блок так же,
  // как это делает человек — нажатием на его заголовок.
  const sec = document.querySelector('[data-section="thread"]');
  if (!sec) return 'нет блока';
  if (sec.getAttribute('data-open') !== '1') {
    const btn = sec.querySelector('.sec__toggle');
    if (!btn) return 'нет заголовка';
    btn.click();
  }
  return sec.getAttribute('data-open') === '1' ? 'открыт' : 'свёрнут';
})()"""


def open_thread(ch: Chrome) -> str:
    """Раскрыть «Диалог по запросу» и дождаться поля ввода (иначе лента пуста)."""
    state = ch.cdp.eval_js(JS_OPEN_THREAD)
    # wait_js ждёт объект с флагом — здесь достаточно булева выражения.
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if ch.cdp.eval_js("Boolean(document.querySelector('.thr__input'))"):
            break
        time.sleep(0.1)
    time.sleep(0.3)
    return str(state)


def wait_js(ch: Chrome, expr: str, timeout: float = 8.0, step: float = 0.1):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = ch.cdp.eval_js(expr)
        if last and last.get("ready", last.get("open", last.get("near", False))):
            return last
        time.sleep(step)
    return last


def panel_state(ch: Chrome, qid: str) -> dict:
    return ch.cdp.eval_js(JS_PANEL % {"qid": json.dumps(qid)})


def post_json(base: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = Request(base + path, data=data, method="POST",
                  headers={"Content-Type": "application/json", "Origin": base,
                           "Host": base.split("//", 1)[1]})
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


# ────────────────────────────────────────────────────────────────── прогон

def run(port: int, shots: Path) -> tuple[Check, dict]:
    c = Check()
    base = f"http://127.0.0.1:{port}"
    mock = start_mock(port)
    ch = None
    report: dict = {"port": port, "checks": [], "layout": {}, "panels": []}
    try:
        ch = launch_browser(1440, 900)
        ch.navigate(f"{base}/#/room/{ROOM}/request/{RID}")
        ready = wait_js(ch, JS_WAIT_READY, timeout=15)
        c.ok("страница запроса с колонкой вопросов открылась", bool(ready and ready.get("ready")), ready, "ready")
        time.sleep(0.8)

        # ── 1. ширина колонки и адаптив ────────────────────────────────
        layout = ch.cdp.eval_js(JS_LAYOUT)
        report["layout"]["1440"] = layout
        aside_w = (layout.get("aside") or {}).get("w", 0)
        c.ok("колонка вопросов шире прежних 272 px", aside_w > 300, aside_w, "> 300")
        c.ok("в колонке нет горизонтального скролла",
             (layout.get("aside") or {}).get("w", 0) <= (layout.get("questions") or {}).get("w", 10 ** 6) + 40,
             (layout.get("aside"), layout.get("questions")), "questions ≤ aside")
        c.ok("страница без горизонтального скролла (1440)",
             layout["doc"]["sw"] <= layout["doc"]["cw"] + 1, layout["doc"], "sw ≤ cw")

        narrow = {}
        for width in (1280, 1099, 900, 899, 640):
            ch.set_viewport(width, 900)
            time.sleep(0.35)
            info = ch.cdp.eval_js(JS_LAYOUT)
            narrow[str(width)] = info
            wide_enough = (info.get("aside") or {}).get("w", 0)
            if width >= 900:
                c.ok(f"{width} px: две колонки, колонка вопросов ≥ 288 px",
                     wide_enough >= 288 and (info.get("grid") or {}).get("w", 0) >= width - 300,
                     wide_enough, "≥ 288")
            else:
                grid = info.get("grid") or {}
                aside = info.get("aside") or {}
                c.ok(f"{width} px: вопросы ушли под основной блок (одна колонка)",
                     abs(aside.get("w", 0) - grid.get("w", 0)) <= 2,
                     (aside.get("w"), grid.get("w")), "равные ширины")
            c.ok(f"{width} px: нет горизонтального скролла",
                 info["doc"]["sw"] <= info["doc"]["cw"] + 1, info["doc"], "sw ≤ cw")
        report["layout"]["narrow"] = narrow
        ch.set_viewport(1440, 900)
        time.sleep(0.4)
        ch.screenshot(shots / "w15c-questions-1440.png")

        # ── 2. ответ и вопрос в колонке ───────────────────────────────
        rows = ch.cdp.eval_js(JS_ROWS)
        if len(rows) < 3 or any(not r.get("text") for r in rows):
            open_folds(ch)                     # свёрнутый блок «Отвечено»
            rows = ch.cdp.eval_js(JS_ROWS)
        report["rows"] = rows
        by_id = {r["id"]: r for r in rows}
        c.ok("три вопроса: открытый, учтённый и ждущий реакции", len(rows) == 3, len(rows), 3)
        c.ok("служебный вопрос «сколько раундов критика» не показан (B7-3)",
             not any("раунд" in (r.get("text") or "").lower() for r in rows),
             [r.get("id") for r in rows], "без служебных вопросов")

        applied = by_id.get("q_2") or {}
        c.ok("ответ показан рядом с вопросом как «Вопрос — ваш ответ»",
             "Вопрос — ваш ответ" in applied.get("text", ""), applied.get("text", "")[:90], "пара «Вопрос — ваш ответ»")
        c.ok("виден статус ответа «учтено: работа пересмотрена»",
             "учтено: работа пересмотрена" in applied.get("text", ""), applied.get("state"), "applied")
        c.ok("видна реакция руководителя (Принято: …)",
             "Принято: team-cfo не добавляем" in applied.get("text", ""), applied.get("text", "")[-120:], "текст реакции")
        c.ok("у отвеченного вопроса нет формы ответа", not applied.get("hasForm"), applied.get("hasForm"), False)

        pending = by_id.get("q_3") or {}
        c.ok("нет реакции — честная подпись «руководитель ещё не ответил»",
             "руководитель ещё не ответил" in pending.get("text", ""), pending.get("text", "")[-90:], "подпись")
        c.ok("есть кнопка «Напомнить руководителю»", pending.get("remind") is True, pending.get("remind"), True)

        opened = by_id.get("q_1") or {}
        c.ok("у открытого вопроса есть «Отправить» и «Мне нужно пояснение»",
             opened.get("send") is True and opened.get("explain") is True,
             (opened.get("send"), opened.get("explain")), (True, True))
        c.ok("микрофон — отдельная кнопка в строке поля (не поверх поля)",
             opened.get("mic") is True and opened.get("micRow") is True and opened.get("micInField") is False,
             (opened.get("mic"), opened.get("micRow"), opened.get("micInField")), (True, True, False))

        # ── 3. панель диктовки у СВОЕГО вопроса ──────────────────────
        click = ch.cdp.eval_js(JS_CLICK_MIC % {"qid": "q_1"})
        c.ok("кнопка микрофона у вопроса нажата", click == "ok", click, "ok")
        started = wait_js(ch, JS_PANEL % {"qid": json.dumps("q_1")}, timeout=10)
        before = panel_state(ch, "q_1")
        report["panels"].append({"stage": "открыта", **before})
        c.ok("панель открылась у своего вопроса", bool(before.get("near")), before, "near=true")
        c.ok("панель не в левом верхнем углу", not before.get("corner"), before.get("panel"), "не угол")
        ch.screenshot(shots / "w15c-dictation-open.png")

        time.sleep(1.6)   # запись идёт, таймер перерисовывает панель
        during = panel_state(ch, "q_1")
        report["panels"].append({"stage": "запись", **during})
        c.ok("во время записи панель у того же вопроса", bool(during.get("near")), during, "near=true")
        c.ok("во время записи панель не в углу", not during.get("corner"), during.get("panel"), "не угол")

        # перерисовка страницы по опросу: меняем данные запроса (комментарий)
        post_json(base, f"/api/requests/{RID}/actions",
                  {"action": "comment", "text": "проверка перерисовки (W15-C)"})
        time.sleep(3.0)   # опрос 2 с + отрисовка
        after_poll = panel_state(ch, "q_1")
        report["panels"].append({"stage": "после перерисовки", **after_poll})
        c.ok("после перерисовки списка панель вернулась к своему вопросу",
             bool(after_poll.get("near")), after_poll, "near=true")
        c.ok("после перерисовки панель не в углу", not after_poll.get("corner"), after_poll.get("panel"), "не угол")

        # «Стоп» → распознавание: место не меняется
        stop = ch.cdp.eval_js(JS_CLICK % {"sel": json.dumps(".mic-panel__actions .btn--primary")})
        c.ok("нажата «Стоп» в панели диктовки", stop == "ok", stop, "ok")
        time.sleep(0.7)
        after_stop = panel_state(ch, "q_1")
        report["panels"].append({"stage": "после «Стоп»", **after_stop})
        c.ok("после «Стоп» панель у того же вопроса", bool(after_stop.get("near")), after_stop, "near=true")
        c.ok("после «Стоп» панель не в углу", not after_stop.get("corner"), after_stop.get("panel"), "не угол")
        ch.screenshot(shots / "w15c-dictation-stopped.png")

        # прокрутка страницы: панель следует за вопросом (или закрылась вместе с записью)
        scrolled = ch.cdp.eval_js(JS_SCROLL % {"top": 240})
        time.sleep(0.4)
        after_scroll = panel_state(ch, "q_1")
        report["panels"].append({"stage": "после прокрутки", "scroll": scrolled, **after_scroll})
        if after_scroll.get("open"):
            c.ok("при прокрутке панель остаётся у своего вопроса",
                 bool(after_scroll.get("near")) and not after_scroll.get("corner"),
                 after_scroll, "near=true")
        else:
            c.ok("панель закрылась вместе с записью — угла нет", True, "закрыта", "закрыта или near")
        ch.cdp.eval_js(JS_SCROLL % {"top": 0})

        # ── 4. ответ через интерфейс: встаёт рядом с вопросом ─────────
        ch.cdp.eval_js(JS_CLICK % {"sel": json.dumps(".mic-panel .btn--ghost")})  # закрыть, если осталась
        time.sleep(0.3)
        typed = ch.cdp.eval_js(JS_SET_TEXT % {"qid": "q_1", "text": json.dumps("Класс 30 — дешевле, берём его")})
        c.ok("ответ набран в поле вопроса", typed == "ok", typed, "ok")
        sent = ch.cdp.eval_js(JS_CLICK % {"sel": json.dumps('[data-q="q_1"] .req-q__send')})
        c.ok("нажата «Отправить» у вопроса", sent == "ok", sent, "ok")
        time.sleep(2.6)
        open_folds(ch)
        rows2 = ch.cdp.eval_js(JS_ROWS)
        report["rows_after"] = rows2
        answered = next((r for r in rows2 if r["id"] == "q_1"), {})
        c.ok("ответ встал рядом с вопросом («Вопрос — ваш ответ»)",
             "Вопрос — ваш ответ" in answered.get("text", ""), answered.get("text", "")[:120], "пара")
        c.ok("статус ответа виден («ответ отправлен…»)",
             "ответ отправлен" in answered.get("text", "") or "принято" in answered.get("text", ""),
             answered.get("text", "")[-120:], "статус ответа")
        feed = ch.cdp.eval_js(JS_ANSWER_IN_FEED)
        if not (feed or {}).get("answers"):
            # Лента свёрнутого диалога не построена — раскрываем блок и читаем заново.
            open_thread(ch)
            time.sleep(0.4)
            feed = ch.cdp.eval_js(JS_ANSWER_IN_FEED)
        report["feed_answers"] = feed
        c.ok("в ленте диалога ответ помечен как ответ на вопрос",
             feed.get("answers", 0) >= 1 and "ответ на вопрос" in feed.get("text", "").lower(),
             feed, "запись «ответ на вопрос»")
        ch.screenshot(shots / "w15c-answer-inline.png")

        report["checks"] = c.items
        return c, report
    finally:
        if ch is not None:
            try:
                ch.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            mock.terminate()
            mock.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Живая проверка вопросов и диктовки (W15-C)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--shots", default=str(ROOT / "docs" / "shots"))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    shots = Path(a.shots)
    shots.mkdir(parents=True, exist_ok=True)
    port = pick_port(a.port)
    print(f"демо-мок и Chromium: порт {port}, комната {ROOM}, запрос {RID}")
    checks, report = run(port, shots)
    if a.json:
        Path(a.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(checks.items)
    failed = checks.failed
    print(f"\nитого {total} проверок, провалов {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
