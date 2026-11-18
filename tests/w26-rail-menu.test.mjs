/**
 * w26-rail-menu.test.mjs — левое меню волны 26, пакет D7.
 *
 * Что проверяем (жалоба 18.11: «сворачивание есть в коде, но человек его не
 * находит; счётчик комнаты считает реакции руководителя»):
 *
 *   1) шеврон на правом краю панели: 24 px, виден всегда, подсказка называет
 *      клавишу, кнопки в нижнем ряду больше нет (она была «потерянной»);
 *   2) Ctrl+B: событие `teamroom:toggle-rail` из hotkeys.js сворачивает и
 *      разворачивает меню; на узком окне (< 900 px) открывает выдвижной ящик
 *      и НЕ перебивает запомненный выбор;
 *   3) ручной выбор сильнее автоматики и переживает перезагрузку;
 *   4) полоса значков показывает числа-бейджи (и подписи в title), а не
 *      прячет их;
 *   5) счётчик комнаты — ждущие ДЕЛА, а не `counts.needs_you` (там реакции
 *      руководителя): пока число не посчитано — «—», а не завышенное значение.
 *
 *   node --test tests/w26-rail-menu.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  RAIL_EVENT, RAIL_HOTKEY, FOLD_TITLE, UNFOLD_TITLE, RAIL_KEY,
  railButtonLabels, railBadges, roomNavTitle, railToggleTitle, applyMode,
  effectiveMode, nextMode, readMode, storeMode, installShellRail,
} from '../web/js/shell-rail.js';
import { waitingFor, setInboxCounts } from '../web/js/shell-inbox.js';
import { HOTKEY_HELP, installHotkeys, uninstallHotkeys, requestRailToggle } from '../web/js/hotkeys.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

/** Поддельное хранилище. */
function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    get size() { return map.size; },
  };
}

/** Минимальный элемент: classList/dataset/атрибуты/события. */
function fakeEl(extra = {}) {
  const classes = new Set(extra.classes || []);
  const attrs = {};
  return Object.assign({
    classList: {
      toggle: (name, on) => { if (on === undefined ? !classes.has(name) : on) classes.add(name); else classes.delete(name); },
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
    dataset: {},
    title: '',
    setAttribute(name, value) { attrs[name] = String(value); this[name] = String(value); },
    getAttribute(name) { return attrs[name] === undefined ? null : attrs[name]; },
    querySelector: () => null,
    addEventListener() {},
    removeEventListener() {},
  }, extra);
}

/** Поддельный документ: хранит подписки и умеет отправлять события. */
function fakeDoc() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(name, fn) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(fn);
    },
    removeEventListener(name, fn) {
      if (listeners.has(name)) listeners.get(name).delete(fn);
    },
    dispatchEvent(ev) {
      for (const fn of Array.from(listeners.get(ev.type) || [])) fn(ev);
      return true;
    },
    count(name) { return (listeners.get(name) || new Set()).size; },
  };
}

/* ── 1. Шеврон на краю панели ───────────────────────────────────────────── */

test('шеврон: подсказка называет действие и клавишу, значок смотрит в сторону движения', () => {
  assert.equal(railToggleTitle(false), FOLD_TITLE);
  assert.equal(railToggleTitle(true), UNFOLD_TITLE);
  assert.match(FOLD_TITLE, /Свернуть меню/);
  assert.match(UNFOLD_TITLE, /Развернуть меню/);
  assert.equal(RAIL_HOTKEY, 'Ctrl+B');
  assert.match(FOLD_TITLE, /\(Ctrl\+B\)/, 'клавиша — в самой подсказке');
  assert.equal(railButtonLabels('wide').icon, 'chevron-left', 'развёрнуто → «свернуть» влево');
  assert.equal(railButtonLabels('rail').icon, 'chevron-right', 'полоса → «развернуть» вправо');
  assert.equal(railButtonLabels('rail').pressed, true);
  assert.equal(railButtonLabels('wide').pressed, false);
});

test('шеврон: 24 px, у правого края, виден в обоих состояниях (CSS-договор)', () => {
  const css = read('web/css/shell-rail.css');
  const block = /\.rail-fold\s*\{([^}]*)\}/.exec(css);
  assert.ok(block, 'нет правила .rail-fold');
  assert.match(block[1], /position:\s*absolute/);
  assert.match(block[1], /width:\s*24px/);
  assert.match(block[1], /height:\s*24px/);
  assert.match(block[1], /right:\s*6px/);
  // В полосе значков шеврон не прячется — он самым верхним, по центру, а марка
  // и значки сдвинуты вниз на его высоту (иначе нажимать было бы некуда).
  assert.match(css, /\.app\.is-rail \.rail-fold\s*\{[^}]*top:\s*4px/s);
  assert.match(css, /\.app\.is-rail \.brand\s*\{[^}]*calc\(var\(--space-1\) \+ 30px\)/s);
  const hideRules = [...css.matchAll(/[^}\n]*\.rail-fold[^{]*\{[^}]*display:\s*none/g)]
    .map((m) => m[0]);
  const bad = hideRules.filter((rule) => !/data-shell-mode='drawer'/.test(rule));
  assert.deepEqual(bad, [], 'шеврон прячется не только в выдвижном ящике');
});

test('нижний ряд: «потерянной» кнопки больше нет, кнопка одна', () => {
  const sidebar = read('web/js/shell-sidebar.js');
  assert.equal((sidebar.match(/id:\s*'rail-toggle'/g) || []).length, 1, 'ровно одна кнопка режима');
  // Кнопка стоит в начале панели (абсолютная, у правого края), а не в подвале.
  const body = sidebar.slice(sidebar.indexOf('export function buildSidebar'));
  const foldAt = body.indexOf("id: 'rail-toggle'");
  const toolsAt = body.indexOf('sidebar__tools');
  assert.ok(foldAt > 0 && toolsAt > 0);
  assert.ok(foldAt < toolsAt, 'шеврон объявлен до подвала и в подвал не попадает');
  assert.match(sidebar, /icon\('chevron-left'/, 'значок — шеврон, а не крошечная иконка');
  assert.match(sidebar, /class: 'rail-fold'/);
  assert.match(sidebar, /['"]aria-controls['"]:\s*'room-list'/);
});

/* ── 2. Ctrl+B ──────────────────────────────────────────────────────────── */

test('Ctrl+B: событие из hotkeys.js сворачивает и разворачивает меню', () => {
  const app = fakeEl();
  const button = fakeEl();
  const doc = fakeDoc();
  const store = memoryStorage();
  const win = { innerWidth: 1900, addEventListener() {}, removeEventListener() {} };
  const rail = installShellRail({ app, button, storage: store, win, doc });
  assert.equal(rail.state, 'wide', 'широкое окно — развёрнуто');
  assert.equal(doc.count(RAIL_EVENT), 1, 'каркас слушает клавишу');

  doc.dispatchEvent({ type: RAIL_EVENT, preventDefault() {} });
  assert.equal(rail.state, 'rail');
  assert.equal(app.classList.contains('is-rail'), true);
  assert.equal(store.getItem(RAIL_KEY), 'rail', 'выбор запомнен');

  doc.dispatchEvent({ type: RAIL_EVENT, preventDefault() {} });
  assert.equal(rail.state, 'wide');
  assert.equal(store.getItem(RAIL_KEY), 'wide');

  rail.destroy();
  assert.equal(doc.count(RAIL_EVENT), 0, 'подписка снята вместе с каркасом');
});

test('Ctrl+B: настоящая клавиша отправляет событие и не даёт браузеру своё', () => {
  const sent = [];
  const doc = fakeDoc();
  doc.dispatchEvent = (ev) => { sent.push(ev.type); return true; };
  uninstallHotkeys();
  installHotkeys({ doc });
  const ev = { key: 'b', code: 'KeyB', ctrlKey: true, defaultPrevented: false,
    target: null, preventDefault() { this.defaultPrevented = true; } };
  // Обработчик висит на keydown поддельного документа.
  const onKey = Array.from(doc.listeners.get('keydown'))[0];
  onKey(ev);
  assert.equal(ev.defaultPrevented, true);
  assert.deepEqual(sent, [RAIL_EVENT]);
  // Русская раскладка — та же физическая клавиша.
  sent.length = 0;
  onKey({ key: 'и', code: 'KeyB', ctrlKey: true, defaultPrevented: false, target: null,
    preventDefault() { this.defaultPrevented = true; } });
  assert.deepEqual(sent, [RAIL_EVENT]);
  uninstallHotkeys();
});

test('Ctrl+B: поддельный документ без CustomEvent не ломает приложение', () => {
  const doc = { dispatchEvent() { throw new Error('нет такого события'); } };
  assert.equal(requestRailToggle(doc), false);
  assert.equal(requestRailToggle(null), false);
});

test('справка по клавишам знает Ctrl+B', () => {
  const row = HOTKEY_HELP.find((r) => r.keys.join(' ') === 'Ctrl + B');
  assert.ok(row, 'нет строки Ctrl+B в справке');
  assert.match(row.what, /меню/);
  assert.match(row.what, /полосу значков|полоса значков/);
});

test('узкое окно: Ctrl+B открывает ящик и не перебивает запомненный выбор', () => {
  const app = fakeEl();
  const store = memoryStorage({ [RAIL_KEY]: 'wide' });
  const doc = fakeDoc();
  const win = { innerWidth: 820, addEventListener() {}, removeEventListener() {} };
  const rail = installShellRail({ app, button: fakeEl(), storage: store, win, doc });
  assert.equal(rail.state, 'drawer');
  doc.dispatchEvent({ type: RAIL_EVENT, preventDefault() {} });
  assert.equal(app.classList.contains('nav-open'), true, 'ящик открылся');
  assert.equal(store.getItem(RAIL_KEY), 'wide', 'выбор пользователя не тронут');
  doc.dispatchEvent({ type: RAIL_EVENT, preventDefault() {} });
  assert.equal(app.classList.contains('nav-open'), false, 'ящик закрылся');
  rail.destroy();
});

/* ── 3. Память выбора ───────────────────────────────────────────────────── */

test('ручной выбор сильнее ширины окна и переживает перезагрузку', () => {
  const store = memoryStorage();
  assert.equal(readMode(store), 'auto');
  storeMode(store, 'rail');
  // «Перезагрузка»: новый каркас читает тот же ключ.
  assert.equal(readMode(store), 'rail');
  assert.equal(effectiveMode(readMode(store), 1920), 'rail', 'широкое окно не разворачивает');
  assert.equal(effectiveMode(readMode(store), 1000), 'rail');
  storeMode(store, 'wide');
  assert.equal(effectiveMode(readMode(store), 1000), 'wide', 'узкое окно не сворачивает');
  // Автоматика остаётся автоматикой только без выбора.
  assert.equal(effectiveMode('auto', 1000), 'rail');
  assert.equal(nextMode('auto', 1000), 'wide');
});

/* ── 4. Бейджи в полосе значков ─────────────────────────────────────────── */

test('бейджи: в полосе видны числа, подписи — в title', () => {
  const badges = railBadges({ n: 3, text: '3 дела' }, { working: 2 });
  assert.deepEqual(badges.you, { n: 3, text: '3 дела' });
  assert.deepEqual(badges.live, { n: 2, text: '' });
  assert.equal(roomNavTitle('Руководство', badges), 'Руководство — ждут вас: 3, идёт: 2');
  assert.equal(roomNavTitle('Руководство', railBadges({ n: 0 }, {})), 'Руководство');
  assert.equal(roomNavTitle('', railBadges({ n: null }, {})), 'Комната');
  // Ноль дел — плашки нет (пустая плашка читается как «что-то есть»).
  assert.equal(railBadges({ n: 0 }, {}).you.n, 0);
  assert.equal(railBadges({ n: null }, {}).you, null);
  assert.equal(railBadges({ n: null }, { working: 0 }).live, null);
});

test('бейджи: CSS не прячет числа в полосе значков', () => {
  const css = read('web/css/shell-rail.css');
  // Плашки спрятаны только списком подписей, а бейджи — нет.
  const hideBlock = /\.app\.is-rail \.brand__text,([\s\S]{0,600}?)\{\s*display:\s*none/.exec(css);
  assert.ok(hideBlock, 'не найден блок скрытия подписей в полосе');
  assert.doesNotMatch(hideBlock[1], /\.nav-item__badges/, 'бейджи снова спрятаны');
  assert.match(css, /\.app\.is-rail \.nav-item__badges\s*\{[^}]*position:\s*absolute/s);
  assert.match(css, /\.app\.is-rail \.nav-item__badges \.count-pill\s*\{[^}]*min-width:\s*15px/s);
  assert.match(css, /\.app\.is-rail \.sidebar:hover \.nav-item__badges,[\s\S]{0,80}position:\s*static/);
});

/* ── 5. Верные счётчики ─────────────────────────────────────────────────── */

test('счётчик комнаты: ждущие дела, а не запросы с реакциями руководителя', () => {
  // Сервер отдаёт counts.needs_you = 20 (запросы + реакции) — в меню этого быть не должно.
  const room = { id: 'r-d7-a', title: 'Руководство', counts: { needs_you: 20, working: 0 } };
  const unknown = waitingFor(room);
  assert.equal(unknown.n, null, 'до счёта дел число неизвестно, а не 20');
  assert.equal(unknown.text, 'считаю');
  assert.equal(railBadges(unknown, room.counts).you, null);

  // Точное число дел кладёт вкладка «От вас нужно» (или фоновый опрос).
  setInboxCounts('r-d7-a', { questions: 2, decisions: 1, blocked: 0, approvals: 1 });
  const exact = waitingFor(room);
  assert.equal(exact.n, 4);
  assert.equal(exact.text, '4 дела');
  assert.deepEqual(railBadges(exact, room.counts).you, { n: 4, text: '4 дела' });

  // Дел нет — ноль, а не 20 запросов.
  setInboxCounts('r-d7-b', { questions: 0, decisions: 0, blocked: 0, approvals: 0 });
  assert.equal(waitingFor({ id: 'r-d7-b', counts: { needs_you: 20 } }).n, 0);
});

test('счётчик: стор дел — один источник, реакции живут отдельной отметкой', () => {
  const inbox = read('web/js/shell-inbox.js');
  const fn = /export function waitingFor[\s\S]*?\n\}/.exec(inbox);
  assert.ok(fn, 'нет waitingFor');
  assert.doesNotMatch(fn[0], /counts\.needs_you/, 'снова подставляются запросы вместо дел');
  const sidebar = read('web/js/shell-sidebar.js');
  assert.match(sidebar, /railBadges\(waits, c\)/);
  assert.match(sidebar, /roomNavTitle\(room\.title, badges\)/);
  assert.match(sidebar, /dataset: \{ room: room\.id \}/);
});

/* ── 6. Применение режима ко всем кнопкам ───────────────────────────────── */

test('applyMode ставит состояние каждой кнопке (дубли не расходятся)', () => {
  const app = fakeEl();
  const one = fakeEl();
  const two = fakeEl();
  const state = applyMode({ app, buttons: [one, two] }, 'rail', 1900);
  assert.equal(state, 'rail');
  assert.equal(one['aria-pressed'], 'true');
  assert.equal(two['aria-pressed'], 'true');
  assert.equal(one.title, UNFOLD_TITLE);
  assert.equal(two.title, UNFOLD_TITLE);
  assert.equal(one.dataset.railState, 'rail');
  assert.equal(app.dataset.shellMode, 'rail');
});
