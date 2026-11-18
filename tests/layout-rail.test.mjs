/**
 * layout-rail.test.mjs — адаптив каркаса и страницы запроса (волна 21, R1).
 *
 * Жалоба 11.11: окно в пол-экрана, боковые колонки прежние, центр «сжимается до
 * неимоверности». Здесь проверяются правила, которыми это лечится — без браузера
 * (живой замер — tools/probe_layout_split.py в настоящем Chromium):
 *
 *   1) режим меню: auto → полоса значков, пока РАЗВЁРНУТОЕ меню съедает вид ниже
 *      1400 px (оболочка < 1648), обычное меню начиная с 1648; ручное
 *      «развёрнуто/свернуто» помнится, но не отменяет ящик < 900 px;
 *      порог — W25-L1, канон R5 §7.1 («рейка при виде < 1400»);
 *   2) мусор в localStorage и недоступное хранилище не ломают каркас;
 *   3) колонки страницы запроса считаются по ширине ВИДА (≥ 1100 px → две),
 *      а не окна; минимумы основного блока 640/320 px (R5 §9 пп. 1–2);
 *   4) CSS-договор: пороги и ширины совпадают в tokens.css, shell-rail.css и
 *      view-request.css; в view-request.css у `.req__grid` НЕТ @media по окну
 *      (иначе жалоба возвращается), есть @container view и плашка;
 *   5) в полосе значков подписи комнат живут в title/aria-label (мышь и скринридер).
 *
 *   node --test tests/layout-rail.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  RAIL_BREAKPOINT, MENU_WIDE_MIN, DRAWER_BREAKPOINT, TWO_COL_MIN, MIN_MAIN_TWO_COL,
  MIN_MAIN_ANY, MIN_ASIDE_TWO_COL, RAIL_KEY, readMode, storeMode, effectiveMode,
  nextMode, columnsForView, mainWidthOk, railButtonLabels, installShellRail,
} from '../web/js/shell-rail.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

/** Поддельное хранилище: как localStorage, но без браузера. */
function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    get size() { return map.size; },
  };
}

/** Хранилище, которое падает на любом обращении (приватный режим, запрет Cookies). */
const brokenStorage = {
  getItem() { throw new Error('denied'); },
  setItem() { throw new Error('denied'); },
  removeItem() { throw new Error('denied'); },
};

/** Минимальный элемент для applyMode: только classList/style/attribute. */
function fakeEl() {
  const classes = new Set();
  return {
    classList: {
      toggle: (name, on) => { if (on) classes.add(name); else classes.delete(name); },
      contains: (name) => classes.has(name),
    },
    dataset: {},
    setAttribute(name, value) { this[name] = String(value); },
    getAttribute(name) { return this[name] === undefined ? null : this[name]; },
    querySelector: () => null,
    addEventListener() {},
  };
}

/* ── 1. Режим меню по ширине окна ───────────────────────────────────────── */

test('menu: auto — рейка, пока развёрнутое меню съедает вид ниже 1400 (оболочка < 1648)', () => {
  // W25-L1: 1648 = вид 1400 + панель 248. При окне 1920 с сайдбаром Hermes 280
  // оболочка 1640 → рейка, и вид получает 1580 вместо 1392.
  assert.equal(MENU_WIDE_MIN, 1648);
  assert.equal(RAIL_BREAKPOINT, MENU_WIDE_MIN);
  assert.equal(effectiveMode('auto', 1920), 'wide');
  assert.equal(effectiveMode('auto', 1648), 'wide');
  assert.equal(effectiveMode('auto', 1647), 'rail');
  assert.equal(effectiveMode('auto', 1640), 'rail', 'окно 1920 с сайдбаром Hermes 280');
  assert.equal(effectiveMode('auto', 1100), 'rail');
  assert.equal(effectiveMode('auto', 901), 'rail');
});

test('menu: ниже 900 px — выдвижной ящик, как было (выбор пользователя не отменяет)', () => {
  assert.equal(DRAWER_BREAKPOINT, 900);
  assert.equal(effectiveMode('auto', 900), 'rail');
  assert.equal(effectiveMode('auto', 899), 'drawer');
  assert.equal(effectiveMode('wide', 899), 'drawer');
  assert.equal(effectiveMode('rail', 899), 'drawer');
  assert.equal(effectiveMode('wide', 900), 'wide');
  assert.equal(effectiveMode('rail', 900), 'rail');
});

test('menu: ручной выбор сильнее ширины окна, но только на постоянном меню', () => {
  assert.equal(effectiveMode('wide', 1100), 'wide');
  assert.equal(effectiveMode('rail', 1900), 'rail');
  // Неизвестное значение (старый ключ, мусор) — авто, а не падение.
  assert.equal(effectiveMode('nonsense', 1100), 'rail');
  assert.equal(effectiveMode(undefined, 1700), 'wide');
});

test('menu: нажатие переключателя всегда меняет состояние', () => {
  assert.equal(nextMode('auto', 1100), 'wide');   // было полосой → развернуть
  assert.equal(nextMode('auto', 1700), 'rail');   // было широким → свернуть
  assert.equal(nextMode('rail', 1700), 'wide');
  assert.equal(nextMode('wide', 1100), 'rail');
});

/* ── 2. Память выбора ───────────────────────────────────────────────────── */

test('storage: выбор помнится, «авто» — снимает ключ', () => {
  const store = memoryStorage();
  assert.equal(readMode(store), 'auto');
  assert.equal(storeMode(store, 'rail'), 'rail');
  assert.equal(store.getItem(RAIL_KEY), 'rail');
  assert.equal(readMode(store), 'rail');
  storeMode(store, 'wide');
  assert.equal(readMode(store), 'wide');
  storeMode(store, 'auto');
  assert.equal(store.getItem(RAIL_KEY), null);
  assert.equal(readMode(store), 'auto');
});

test('storage: мусор и недоступное хранилище не ломают каркас', () => {
  assert.equal(readMode(memoryStorage({ [RAIL_KEY]: '{"битый json' })), 'auto');
  assert.equal(readMode(memoryStorage({ [RAIL_KEY]: 'RAIL' })), 'auto');
  assert.equal(readMode(brokenStorage), 'auto');
  assert.equal(readMode(null), 'auto');
  assert.doesNotThrow(() => storeMode(brokenStorage, 'rail'));
  assert.equal(storeMode(null, 'rail'), 'rail');
});

test('installShellRail: режим ставится классом is-rail и переключается кнопкой', () => {
  const app = fakeEl();
  const button = fakeEl();
  const store = memoryStorage();
  let click = null;
  button.addEventListener = (name, fn) => { if (name === 'click') click = fn; };
  const win = { innerWidth: 1100, addEventListener() {}, removeEventListener() {} };
  const rail = installShellRail({ app, button, storage: store, win });
  assert.equal(app.classList.contains('is-rail'), true, 'окно 1100 px — полоса значков');
  assert.equal(button['aria-pressed'], 'true');
  assert.equal(app.dataset.shellMode, 'rail');
  click({ preventDefault() {} });
  assert.equal(app.classList.contains('is-rail'), false, 'нажатие развернуло меню');
  assert.equal(store.getItem(RAIL_KEY), 'wide');
  click({ preventDefault() {} });
  assert.equal(app.classList.contains('is-rail'), true);
  assert.equal(store.getItem(RAIL_KEY), 'rail');
  assert.equal(rail.state, 'rail');
  // Широкое окно из «свёрнутого» — по-прежнему полоса (выбор пользователя).
  assert.equal(effectiveMode(store.getItem(RAIL_KEY), 1920), 'rail');
});

test('installShellRail: destroy снимает обработчики (нет утечек при повторной установке)', () => {
  const app = fakeEl();
  const added = new Map();
  const removed = [];
  const button = fakeEl();
  button.addEventListener = (name, fn) => { added.set(name, fn); };
  button.removeEventListener = (name, fn) => { removed.push([name, fn]); };
  const winAdded = [];
  const winRemoved = [];
  const win = { innerWidth: 1100, addEventListener: (n) => winAdded.push(n),
    removeEventListener: (n) => winRemoved.push(n) };
  const rail = installShellRail({ app, button, storage: memoryStorage(), win });
  assert.deepEqual(winAdded, ['resize']);
  rail.destroy();
  assert.deepEqual(winRemoved, ['resize'], 'обработчик resize снят');
  const click = removed.find(([name]) => name === 'click');
  assert.ok(click, 'обработчик кнопки тоже снят');
  assert.equal(click[1], added.get('click'), 'снят тот же самый обработчик');
});

/* ── 3. Колонки страницы запроса ────────────────────────────────────────── */

test('колонки: две только при виде ≥ 1100 px — по ширине вида, а не окна', () => {
  assert.equal(TWO_COL_MIN, 1100);
  assert.equal(columnsForView(1099), 1);
  assert.equal(columnsForView(1100), 2);
  assert.equal(columnsForView(1120), 2);
  assert.equal(columnsForView(0), 1);
  assert.equal(columnsForView(undefined), 1);
});

test('минимумы: основной блок 640 px в двух колонках, 320 px в любой раскладке', () => {
  assert.equal(MIN_MAIN_TWO_COL, 640, 'R5 §9 п. 1: центр ≥ 640 при виде ≥ 1100');
  assert.equal(MIN_MAIN_ANY, 320);
  assert.equal(MIN_ASIDE_TWO_COL, 280);
  assert.deepEqual(mainWidthOk(640, 2), { ok: true, min: 640 });
  assert.equal(mainWidthOk(639, 2).ok, false);
  assert.equal(mainWidthOk(430, 2).ok, false, 'замер 11.11 (1100 px окна) не проходит');
  assert.equal(mainWidthOk(320, 1).ok, true);
  assert.equal(mainWidthOk(319, 1).ok, false);
  assert.equal(mainWidthOk(671, 1).ok, true);
});

test('подписи кнопки меню: состояние видно и словами, и значком', () => {
  const rail = railButtonLabels('rail');
  const wide = railButtonLabels('wide');
  assert.equal(rail.pressed, true);
  assert.match(rail.ariaLabel, /Развернуть/);
  // W26-D7: это шеврон на краю панели, а не «потерянный» значок в подвале.
  assert.equal(rail.icon, 'chevron-right');
  assert.match(rail.title, /Ctrl\+B/, 'подсказка называет клавишу');
  assert.equal(wide.pressed, false);
  assert.match(wide.ariaLabel, /Свернуть/);
  assert.equal(wide.icon, 'chevron-left');
  assert.match(wide.title, /Ctrl\+B/);
});

/* ── 4. CSS-договор: одни и те же числа в трёх файлах ───────────────────── */

const TOKENS = read('web/css/tokens.css');
const RAIL_CSS = read('web/css/shell-rail.css');
const REQ_CSS = read('web/css/view-request.css');
const COMPONENTS = read('web/css/components.css');

test('токены: полоса значков 56–64 px и порог двух колонок 1100 px', () => {
  const rail = /--sidebar-rail:\s*(\d+)px/.exec(TOKENS);
  assert.ok(rail, 'нет токена --sidebar-rail');
  const width = Number(rail[1]);
  assert.ok(width >= 56 && width <= 64, `полоса ${width} px вне 56–64`);
  assert.match(TOKENS, /--view-two-col-min:\s*1100px/);
  // W25-L1: минимум центра в двух колонках — тоже токен (R5 §9 п. 1).
  assert.match(TOKENS, /--view-main-min:\s*640px/);
  assert.equal(Number(rail[1]), 60);
});

test('каркас: вид — контейнер запросов, полоса не двигает содержимое', () => {
  assert.match(COMPONENTS, /\.app__main\s*\{[^}]*container-type:\s*inline-size/s);
  assert.match(COMPONENTS, /container-name:\s*view/);
  // Панель вынута из потока — содержимое обязано стоять во второй колонке.
  assert.match(RAIL_CSS, /\.app\.is-rail\s*\{[^}]*--sidebar-rail/s);
  assert.match(RAIL_CSS, /\.app\.is-rail \.app__main\s*\{[^}]*grid-column:\s*2/s);
  // Раскрытие поверх контента: панель fixed, ширина меняется по наведению.
  assert.match(RAIL_CSS, /\.app\.is-rail \.sidebar\s*\{[^}]*position:\s*fixed/s);
  assert.match(RAIL_CSS, /\.app\.is-rail \.sidebar:hover/);
  // Подписи прячутся, но их возвращает наведение и фокус с клавиатуры.
  assert.match(RAIL_CSS, /\.app\.is-rail \.nav-item__text,?[\s\S]{0,200}?display:\s*none/);
  assert.match(RAIL_CSS, /\.sidebar:focus-within \.nav-item__text/);
});

test('страница запроса: колонки через @container view, а не @media по окну', () => {
  assert.match(REQ_CSS, /@container view \(min-width: 1100px\)/);
  // Правая колонка 280–320 px: шире — уже отнимает у свода (R5 §7.1).
  assert.match(REQ_CSS, /clamp\(280px, 26cqi, 320px\)/);
  assert.match(REQ_CSS, /minmax\(640px, 1fr\)/);
  // Регрессия 11.11: раскладка, зависящая от ширины ОКНА.
  const mediaGrid = /@media[^{]*\{[^@]*?\.req__grid\s*\{[^}]*grid-template-columns/s.exec(REQ_CSS);
  assert.equal(mediaGrid, null, 'найдено @media, меняющее колонки .req__grid (жалоба вернётся)');
  // Одноколоночный режим: колонка не липнет, строка ответа — в столбик.
  assert.match(REQ_CSS, /@container view \(max-width: 1099px\)/);
  assert.match(REQ_CSS, /@container reqaside \(max-width: 330px\)/);
});

test('страница запроса: плашка «Нужен ваш ответ» есть в CSS и в разметке колонки', () => {
  assert.match(REQ_CSS, /\.req-pinbar\s*\{[^}]*position:\s*sticky/s);
  assert.match(REQ_CSS, /@container view \(min-width: 1100px\)\s*\{\s*\.req-pinbar\s*\{\s*display:\s*none/);
  const aside = read('web/js/views/request-aside.js');
  assert.match(aside, /req-pinbar/);
  assert.match(aside, /LAYOUT_RU/);
  assert.match(read('web/js/text-ru.js'), /pinbar:\s*'Нужен ваш ответ'/);
});

test('кнопка режима меню стоит рядом с темой и помнит состояние', () => {
  const sidebar = read('web/js/shell-sidebar.js');
  assert.match(sidebar, /id:\s*'rail-toggle'/);
  assert.match(sidebar, /aria-pressed/);
  const app = read('web/js/app.js');
  assert.match(app, /installShellRail/);
  // W25-L1: наблюдатель переехал в layout-mode.js (один источник режима).
  assert.match(app, /installLayoutMode/);
  assert.match(read('web/js/layout-mode.js'), /new RO\(/);
  // Подписи комнат не теряются в полосе значков.
  assert.match(sidebar, /'aria-label':\s*room\.title/);
});
