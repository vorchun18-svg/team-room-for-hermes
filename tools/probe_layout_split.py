#!/usr/bin/env python3
"""Замер раскладки страницы запроса и всех маршрутов комнаты при разной ширине окна.

    python tools/mock_server.py --port 8851 --fixtures <набор> &
    python tools/probe_layout_split.py --base http://127.0.0.1:8851 \
        [--room cafe] [--routes all|request] [--dataset normal] [--min-main 520] [--json out.json]

По каждой ширине окна печатает реальные ширины: левое меню приложения, область вида,
колонку «основной блок» и правую колонку вопросов. Падает (код 1), если основной блок
уже `--min-main` px (жалоба Михаила 11.11: на пол-экрана центр схлопывался до
неузнаваемости, а боковые колонки оставались прежними) или есть горизонтальная прокрутка.

W25-L1: пороги — канон R5 §9 (две колонки при виде ≥ 1100, центр ≥ 640 px,
правая колонка 280–320 px, строка 45–80 знаков). Старые 1000/520 не использовать.

W21-R1 (расширение по §1 п. 6):
  • проверяются ВСЕ маршруты комнаты (запрос, «От вас нужно», команда, карта,
    отчёты, журнал, настройки, композер, поиск), а не только страница запроса;
  • минимальные ширины колонок из §1 пп. 1–3: основной блок ≥ 520 px при двух
    колонках и ≥ 320 px в любом режиме, правая колонка ≥ 280 px при двух колонках,
    две колонки только при ширине ВИДА ≥ 1000 px (и обязательно при ней);
  • строка свода измеряется по-настоящему (символы ÷ строки из client rects),
    требование ≥ 45 знаков;
  • JSON-отчёт со всеми замерами и списком проблем (`--json`), код возврата ≠ 0
    при нарушении; метка набора данных (`--dataset`) — для отчёта о трёх прогонах.

Свой Chromium во временном каталоге; боевой 8777 не трогает.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cdp import Chrome  # noqa: E402

WIDTHS = (1920, 1600, 1440, 1366, 1280, 1180, 1100, 1024, 960, 900, 820, 760, 700, 640, 560)
#: Порог двух колонок — то же число, что в view-request.css (@container view),
#: web/js/shell-rail.js и web/js/layout-mode.js (TWO_COL_MIN). W25-L1: 1100.
TWO_COL_MIN = 1100
#: Минимум центра в двух колонках (R5 §9 п. 1): 640, а не 520.
MIN_MAIN_TWO_COL = 640
MIN_MAIN_ONE = 320
MIN_LINE_CHARS = 45

#: Маршруты комнаты: (имя, шаблон хэша). Запрос подставляется отдельно.
ROUTES = (
    ("request", "#/room/{room}/request/{rid}"),
    ("backlog", "#/room/{room}/backlog"),
    ("team", "#/room/{room}/team"),
    ("roadmap", "#/room/{room}/roadmap"),
    ("reports", "#/room/{room}/reports"),
    ("journal", "#/room/{room}/journal"),
    ("composer", "#/room/{room}/composer"),
    ("settings", "#/settings"),
    ("search", "#/search?q=%D0%B7%D0%B0%D0%BF%D1%80%D0%BE%D1%81"),
)

JS = r"""
(() => {
  const w = (sel) => { const e = document.querySelector(sel); if (!e) return null;
    const b = e.getBoundingClientRect(); return Math.round(b.width); };
  const first = (...sels) => { for (const s of sels) { const v = w(s); if (v !== null) return v; } return null; };
  const grid = document.querySelector('.req__grid');
  // Строка свода: настоящие строки вёрстки (client rects диапазона), а не
  // оценка «ширина ÷ средний знак».
  let charsPerLine = null;
  const paras = [...document.querySelectorAll('.req-answer__body p, .markdown p')]
    .filter((p) => (p.textContent || '').trim().length >= 120);
  if (paras.length) {
    const p = paras.sort((a, b) => b.textContent.length - a.textContent.length)[0];
    const range = document.createRange();
    range.selectNodeContents(p);
    const lines = range.getClientRects().length;
    const chars = p.textContent.trim().length;
    if (lines > 0) charsPerLine = Math.round(chars / lines);
  }
  const app = document.getElementById('app');
  return {
    vw: innerWidth,
    menu: first('.sidebar', '#sidebar', '.shell-sidebar', 'aside.side', '.app__sidebar', '.app__nav'),
    menuMode: (() => { const sb = document.querySelector('.sidebar');
      if (!sb) return null; const cs = getComputedStyle(sb);
      if (cs.position === 'fixed') return sb.getBoundingClientRect().left < 0 ? 'drawer-closed' : 'fixed';
      return 'static'; })(),
    shellMode: app && app.dataset ? app.dataset.shellMode || null : null,
    viewCols: app && app.dataset ? app.dataset.viewCols || null : null,
    view: w('#view'), inner: w('.view__inner'), grid: w('.req__grid'),
    main: w('.req__main'), aside: w('.req__aside'),
    cols: grid ? getComputedStyle(grid).gridTemplateColumns : null,
    // W25-L1: ручка нижнего листа — «От вас нужно · N» (замена плашки при виде < 1100).
    sheet: (() => { const h = document.querySelector('.req-sheet__handle');
      return h ? Math.round(h.getBoundingClientRect().height) : null; })(),
    asideMode: app && app.dataset ? app.dataset.aside || null : null,
    layout: app && app.dataset ? app.dataset.layout || null : null,
    pinbar: (() => { const b = document.querySelector('.req-pinbar'); if (!b) return null;
      const cs = getComputedStyle(b); return { shown: cs.display !== 'none' && b.getBoundingClientRect().height > 0,
        w: Math.round(b.getBoundingClientRect().width), text: b.textContent.trim().slice(0, 60) }; })(),
    charsPerLine,
    hscroll: document.scrollingElement.scrollWidth - innerWidth,
    innerHscroll: (() => { const e = document.querySelector('.view__inner');
      return e ? e.scrollWidth - e.clientWidth : 0; })(),
  };
})()
"""


def room_requests(base: str, room: str) -> list:
    data = json.load(urllib.request.urlopen(f"{base}/api/rooms/{room}/requests", timeout=20))
    return data.get("items") or data.get("requests") or []


def pick_request(items: list):
    """Запрос для замера: сначала тот, что ждёт ответа, иначе первый в списке."""
    return next((i for i in items if i.get("needs_you")), items[0] if items else None)


def measure(ch, url: str, width: int, pause: float) -> dict:
    ch.set_viewport(width, 900)
    ch.navigate(url)
    time.sleep(pause)
    return ch.cdp.eval_js(JS) or {}


def judge(name: str, row: dict, args) -> list:
    """Проблемы одного замера. Пустой список — раскладка в норме."""
    problems = []
    view = row.get("view")
    if (row.get("hscroll") or 0) > 1:
        problems.append(f"горизонтальная прокрутка окна {row['hscroll']}px")
    if (row.get("innerHscroll") or 0) > 1:
        problems.append(f"горизонтальная прокрутка вида {row['innerHscroll']}px")
    if view is not None and view < MIN_MAIN_ONE:
        problems.append(f"вид сжался до {view}px")
    if name != "request":
        return problems

    main_w, aside_w = row.get("main"), row.get("aside")
    two = (view or 0) >= TWO_COL_MIN
    if two and aside_w and aside_w < args.min_aside:
        problems.append(f"правая колонка {aside_w}px < {args.min_aside}px")
    if two and not aside_w:
        problems.append(f"нет правой колонки при виде ≥ {TWO_COL_MIN}px")
    # В режиме листа колонка вынута из потока (fixed) — «шириной вида» она не является.
    if not two and not row.get("sheet") and aside_w and main_w and aside_w != main_w:
        problems.append(f"две колонки при виде {view}px < {TWO_COL_MIN}px")
    min_main = args.min_main if two else MIN_MAIN_ONE
    if main_w is not None and main_w < min_main:
        where = f"при колонке справа {aside_w}px" if two else "в одноколоночном режиме"
        problems.append(f"основной блок {main_w}px < {min_main}px {where}")
    cpl = row.get("charsPerLine")
    if cpl is not None and cpl < MIN_LINE_CHARS:
        problems.append(f"строка свода {cpl} знаков < {MIN_LINE_CHARS}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--room", default="cafe")
    ap.add_argument("--routes", default="all", choices=("all", "request"),
                    help="all — все маршруты комнаты, request — только страница запроса")
    ap.add_argument("--dataset", default="normal", help="метка набора данных для отчёта")
    ap.add_argument("--request", default="", help="id запроса для замера (по умолчанию — ждущий ответа)")
    ap.add_argument("--min-main", type=int, default=MIN_MAIN_TWO_COL, help="минимум основного блока в двух колонках (R5 §9 п. 1)")
    ap.add_argument("--min-aside", type=int, default=280, help="минимум правой колонки в двух колонках")
    ap.add_argument("--json", default=None)
    ap.add_argument("--theme", default="light")
    ap.add_argument("--widths", default=",".join(str(w) for w in WIDTHS))
    ap.add_argument("--pause", type=float, default=1.5, help="пауза после загрузки страницы, с")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    widths = [int(x) for x in args.widths.split(",") if x.strip()]

    pick = pick_request(room_requests(base, args.room))
    rid = args.request or (pick["id"] if pick else "")
    if not rid and args.routes == "request":
        print("в комнате нет запросов — замер страницы запроса невозможен")
        return 2

    routes = [r for r in ROUTES if r[0] == "request"] if args.routes == "request" else list(ROUTES)
    rows, bad = [], []
    with Chrome.launch(width=1280, height=900) as ch:
        for name, tpl in routes:
            if "{rid}" in tpl and not rid:
                continue
            hash_ = tpl.format(room=args.room, rid=rid)
            url = f"{base}/?theme={args.theme}{hash_}"
            for width in widths:
                row = measure(ch, url, width, args.pause)
                row.update({"route": name, "width": width, "dataset": args.dataset, "hash": hash_})
                rows.append(row)
                problem = judge(name, row, args)
                if problem:
                    bad.append({"route": name, "width": width, "problems": problem})
                extra = ""
                if name == "request":
                    extra = (f" ({row.get('layout')}/{row.get('asideMode')}) основной={row.get('main')} справа={row.get('aside')}"
                             f" строк≈{row.get('charsPerLine')}")
                    if row.get("pinbar"):
                        extra += f" плашка={'да' if row['pinbar']['shown'] else 'нет'}"
                print(f"{name:>9} {width:>5}: меню={row.get('menu')} ({row.get('menuMode')})"
                      f" вид={row.get('view')}{extra}"
                      + ("  ⚠ " + "; ".join(problem) if problem else ""))
    report = {
        "base": base, "dataset": args.dataset, "room": args.room, "request": rid,
        "theme": args.theme, "routes": [r[0] for r in routes], "widths": widths,
        "rows": rows, "problems": bad,
    }
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if bad:
        print(f"\nПроблем: {len(bad)} из {len(rows)} замеров")
        return 1
    print(f"\nРаскладка в норме ({len(rows)} замеров, набор «{args.dataset}»)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
