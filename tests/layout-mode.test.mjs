/**
 * tests/layout-mode.test.mjs — режимы раскладки по ширине вида (W25-L1).
 *
 * Проверяется то, что можно проверить без браузера: чистые функции режима,
 * нижнего листа и липкой панели свода, а также договор CSS с JS по порогам.
 * Живая проверка — tools/probe_w25_modes.py в Chromium.
 *
 *   node --test tests/layout-mode.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  LAYOUT_MIN, LAYOUT_MODES, TWO_COL_MIN, MIN_MAIN_TWO_COL, MIN_ASIDE_TWO_COL,
  MAX_ASIDE_TWO_COL, SHEET_H, SHEET_H_XS, ASIDE_KEY, DENSITY_KEY,
  modeForView, asideForView, resolveAside, readDensity, readManualAside, installLayoutMode,
} from '../web/js/layout-mode.js';
import { handleText, handleAria, countWaiting, OPEN_CLASS, HANDLE_CLASS } from '../web/js/shell-aside-sheet.js';
import { barLabel, barNote, BAR_H } from '../web/js/shell-svod-bar.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
  };
}

/* ── 1. Режимы: канон R5 §7.1 ───────────────────────────────────────────── */

test('режимы: пять порогов, посчитанных от ширины ВИДА', () => {
  assert.deepEqual(LAYOUT_MODES, ['xl', 'l', 'm', 's', 'xs']);
  assert.equal(LAYOUT_MIN.xl, 1400);
  assert.equal(LAYOUT_MIN.l, 1100);
  assert.equal(LAYOUT_MIN.m, 900);
  assert.equal(LAYOUT_MIN.s, 700);

  assert.equal(modeForView(1920), 'xl');
  assert.equal(modeForView(1400), 'xl');
  assert.equal(modeForView(1399), 'l', 'было бы «два меню»: 1392 при окне 1920');
  assert.equal(modeForView(1100), 'l');
  assert.equal(modeForView(1099), 'm');
  assert.equal(modeForView(900), 'm');
  assert.equal(modeForView(899), 's');
  assert.equal(modeForView(700), 's');
  assert.equal(modeForView(699), 'xs');
  assert.equal(modeForView(403), 'xs');
  assert.equal(modeForView(0), 'xs');
  assert.equal(modeForView(undefined), 'xs');
});

test('полуэкран: 1366 с сайдбаром Hermes 280 — колонки ещё есть, листа нет', () => {
  // 1366 − 280 = 1086 → режим m (лист), а не «колонка 287 px + центр 683».
  assert.equal(modeForView(1086), 'm');
  assert.equal(asideForView(1086), 'sheet');
  // 1100 вида — граница: центр 742 px при правой колонке 286 px.
  assert.equal(modeForView(1100), 'l');
  assert.equal(asideForView(1100), 'column');
});

test('«От вас нужно»: колонка только там, где центру остаётся ≥ 640 px', () => {
  assert.equal(TWO_COL_MIN, 1100);
  assert.equal(MIN_MAIN_TWO_COL, 640);
  assert.ok(MIN_ASIDE_TWO_COL >= 280 && MAX_ASIDE_TWO_COL <= 320);
  // 1100 вида − 48 поля − 24 зазор − 286 колонка = 742 px центра.
  const center = 1100 - 48 - 24 - 286;
  assert.ok(center >= MIN_MAIN_TWO_COL, `центр ${center} px`);
  assert.equal(asideForView(1099), 'sheet');
  assert.equal(asideForView(700), 'sheet');
  assert.equal(asideForView(1920), 'column');
});

test('ручной выбор сильнее автоматики, мусор — автоматика', () => {
  assert.equal(resolveAside(1920, 'sheet'), 'sheet');
  assert.equal(resolveAside(683, 'column'), 'column');
  assert.equal(resolveAside(1920, 'hidden'), 'hidden');
  assert.equal(resolveAside(1920, null), 'column');
  assert.equal(resolveAside(683, null), 'sheet');
  assert.equal(resolveAside(683, 'nonsense'), 'sheet');
});

test('плотность и ручной выбор читаются из localStorage без падений', () => {
  assert.equal(DENSITY_KEY, 'teamroom.density');
  assert.equal(ASIDE_KEY, 'teamroom.aside');
  assert.equal(readDensity(memoryStorage({ 'teamroom.density': 'compact' })), 'compact');
  assert.equal(readDensity(memoryStorage({ 'teamroom.density': 'roomy' })), 'roomy');
  assert.equal(readDensity(memoryStorage({ 'teamroom.density': 'huge' })), 'normal');
  assert.equal(readDensity(null), 'normal');
  assert.equal(readManualAside(memoryStorage({ 'teamroom.aside': 'sheet' })), 'sheet');
  assert.equal(readManualAside(memoryStorage({ 'teamroom.aside': 'column' })), 'column');
  assert.equal(readManualAside(memoryStorage({ 'teamroom.aside': 'x' })), null);
  const broken = { getItem() { throw new Error('приватный режим'); } };
  assert.equal(readDensity(broken), 'normal');
  assert.equal(readManualAside(broken), null);
});

/* ── 2. Установка режима в DOM ──────────────────────────────────────────── */

function fakeShell(widths) {
  const docEl = { style: { setProperty() {} } };
  const view = { clientWidth: widths.view, addEventListener() {} };
  const app = {
    dataset: {},
    clientWidth: widths.app,
    querySelector: () => view,
  };
  return { app, view, docEl };
}

test('installLayoutMode пишет data-layout/data-aside/data-density на #app', () => {
  const { app, view } = fakeShell({ app: 1640, view: 1580 });
  const seen = [];
  const m = installLayoutMode({
    app, view, storage: memoryStorage(), win: { document: { documentElement: { style: { setProperty() {} } } } },
    onMode: (s) => seen.push(s),
  });
  assert.equal(app.dataset.layout, 'xl');
  assert.equal(app.dataset.aside, 'column');
  assert.equal(app.dataset.density, 'normal');
  assert.equal(app.dataset.focus, 'off');
  assert.equal(app.dataset.viewWidth, '1580');
  assert.equal(m.mode, 'xl');
  assert.equal(m.width, 1580);
  assert.deepEqual(seen.map((s) => s.mode), ['xl']);
  m.destroy();
});

test('installLayoutMode уважает ручной выбор листа и не падает без ResizeObserver', () => {
  const { app, view } = fakeShell({ app: 900, view: 840 });
  const m = installLayoutMode({
    app, view, storage: memoryStorage({ 'teamroom.aside': 'column' }),
    win: { document: { documentElement: { style: { setProperty() {} } } } },
  });
  assert.equal(app.dataset.layout, 's');
  assert.equal(app.dataset.aside, 'column', 'ручной выбор сильнее автоматики');
  m.destroy();
});

/* ── 3. Нижний лист ─────────────────────────────────────────────────────── */

test('лист: подпись счётчика и высоты из канона', () => {
  assert.equal(SHEET_H, 56);
  assert.equal(SHEET_H_XS, 52);
  assert.equal(handleText(0), 'От вас нужно');
  assert.equal(handleText(5), 'От вас нужно · 5');
  assert.equal(handleText('7'), 'От вас нужно · 7');
  assert.equal(HANDLE_CLASS, 'req-sheet__handle');
  // Область числа названа: на вкладке комнаты число другое (по всей комнате).
  assert.equal(handleAria(2), 'От вас нужно: 2 в этом запросе, развернуть');
  assert.equal(handleAria(0), 'От вас нужно, развернуть');
  assert.equal(OPEN_CLASS, 'is-sheet-open');
});

test('лист: считает только строки с полем ввода (что видит человек)', () => {
  const rows = [
    { querySelector: (sel) => (sel === 'textarea' ? {} : null) },
    { querySelector: () => null },
    { querySelector: () => null },
  ];
  const aside = { querySelectorAll: () => rows };
  assert.equal(countWaiting(aside), 1);
  assert.equal(countWaiting({ querySelectorAll: () => [] }), 0);
  assert.equal(countWaiting(null), 0);
});

/* ── 4. Липкая панель свода ─────────────────────────────────────────────── */

test('панель: подпись версии и пометка «отстаёт»', () => {
  assert.equal(BAR_H, 40);
  assert.equal(barLabel({ synthesis: { state: 'ready', version: 3, history: [{ version: 1 }, { version: 2 }, { version: 3 }] } }), 'Свод · v3');
  assert.equal(barLabel({ synthesis: { state: 'none' } }), '', 'свода нет — панели нет');
  assert.equal(barLabel(null), '');
  assert.equal(barNote({ synthesis: { state: 'ready', live: { state: 'stale' } } }), 'отстаёт');
  assert.equal(barNote({ synthesis: { state: 'ready', live: { state: 'fresh' } } }), '');
  assert.equal(barNote({}), '');
});

/* ── 5. CSS-договор с JS ────────────────────────────────────────────────── */

const TOKENS = read('web/css/tokens.css');
const REQ_CSS = read('web/css/view-request.css');
const COMPONENTS = read('web/css/components.css');

test('CSS: те же пороги, что в JS (1100 / 640 / 320)', () => {
  assert.match(TOKENS, /--view-two-col-min:\s*1100px/);
  assert.match(TOKENS, /--view-main-min:\s*640px/);
  assert.match(REQ_CSS, /@container view \(min-width: 1100px\)/);
  assert.match(REQ_CSS, /minmax\(640px, 1fr\) clamp\(280px, 26cqi, 320px\)/);
  assert.match(TOKENS, /--req-bar-h:\s*40px/);
  assert.match(TOKENS, /--sheet-h:\s*56px/);
  assert.match(TOKENS, /--sheet-h-xs:\s*52px/);
});

test('CSS: поля вида 24/16/12 и scroll-padding под панель', () => {
  assert.match(COMPONENTS, /scroll-padding-top:\s*calc\(var\(--req-bar-h\) \+ var\(--space-2\)\)/);
  // Липкая панель поднимается на верхнее поле вида — иначе над ней видно текст.
  assert.match(COMPONENTS, /--view-pad-top:\s*var\(--space-6\)/);
  assert.match(COMPONENTS, /\.app\[data-layout="s"\] \.view \{[^}]*padding:\s*var\(--space-4\)/);
  assert.match(COMPONENTS, /\.app\[data-layout="xs"\] \.view \{[^}]*padding:\s*var\(--space-3\)/);
});

test('CSS: лист 56 px, разворот 60 % и полный экран < 900', () => {
  assert.match(REQ_CSS, /\.app\[data-aside="sheet"\] \.req__aside \{[^}]*position:\s*fixed/s);
  assert.match(REQ_CSS, /\.app\[data-aside="sheet"\] \.req__aside \{[^}]*height:\s*var\(--sheet-h\)/s);
  assert.match(REQ_CSS, /\.app\[data-aside="sheet"\]\.is-sheet-open \.req__aside \{[^}]*height:\s*60dvh/s);
  assert.match(REQ_CSS, /height:\s*calc\(100dvh - 40px\)/);
  assert.match(REQ_CSS, /\.req-sheet__handle/);
});

test('CSS: панель свода липнет к верху прокрутки, а не сидит внизу карточки', () => {
  assert.match(REQ_CSS, /\.req-bar \{[^}]*position:\s*sticky[^}]*top:\s*calc\(-1 \* var\(--view-pad-top/s);
  assert.match(REQ_CSS, /\.req-bar \{[^}]*margin-top:\s*calc\(/s);
  assert.match(REQ_CSS, /\.req-bar \{[^}]*height:\s*var\(--req-bar-h\)/s);
});

test('страница запроса: шапка в одну строку, крошка внутри строки заголовка', () => {
  const header = read('web/js/views/request-header.js');
  const head = header.slice(header.indexOf("class: 'req__head'"));
  assert.match(head, /req__headrow[\s\S]{0,400}req__crumb/,
    'крошка должна стоять в строке заголовка, а не отдельным слоем');
  assert.match(REQ_CSS, /\.req__headrow \{[^}]*align-items:\s*center/);
});

test('панель свода не дублирует логику: нажимает кнопки карточки и берёт exportGroup', () => {
  const bar = read('web/js/shell-svod-bar.js');
  assert.match(bar, /req-answer__foot \[data-action="\$\{action\}"\]/);
  assert.match(bar, /from '\.\/views\/request-export\.js'/);
  assert.match(bar, /exportGroup\(/);
});
