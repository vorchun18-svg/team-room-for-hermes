/**
 * home-start.test.mjs — стартовая страница «Комнаты» и запуск без «прыжка» (W27-E4).
 *
 * Дефект 3 (18.11, вечер): «на стартовую страницу не ведёт ни одна кнопка»,
 * «страница меняется через время, что-то подгружается позже».
 *
 * Что проверяем:
 *   1) `planStart` — ОДНО решение адреса: явный `#/` не перекидывается никогда,
 *      «Сегодня» сильнее памяти, «Комнаты»/«Последнее место» из настроек,
 *      deep link сильнее настройки, «Сегодня» без списка комнат — ждём (ready:false);
 *   2) «Стартовая страница» — три значения (сегодня / комнаты / последнее место),
 *      мусор читается как «Сегодня» (старые записи не ломаются);
 *   3) клавиши G, H переходят на `#/` событием `teamroom:navigate`, а не дёргают
 *      роутер напрямую; в поле ввода и по одиночной G — молчат;
 *   4) каркас: заглушка-скелетон до первой отрисовки, адрес ставится
 *      `replaceState` (без `hashchange`), кэш списка комнат в localStorage,
 *      `initialHash()` больше нет;
 *   5) меню: пункт «Все комнаты» (значок дома) и заголовок-ссылка на `#/`,
 *      крошка «Комнаты» первой, подсветка пункта на `#/`;
 *   6) страница «Комнаты»: быстрое создание комнаты остаётся, «Быстрый запрос»
 *      отправляет запрос в комнату, «Надиктовать»/«Входящие» доступны.
 *
 *   node --test tests/home-start.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  planStart, normalizeStartPref, START_OPTIONS, START_KEY, START_TODAY, START_ROOMS, START_LAST,
  chooseHash, isBareHash,
} from '../web/js/route-memory.js';
import {
  HOTKEY_HELP, NAV_EVENT, SEQUENCE_MS, installHotkeys, uninstallHotkeys, requestNavigate,
} from '../web/js/hotkeys.js';
import { crumbItems } from '../web/js/scope.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

const ROOMS = ['cafe', 'fn'];
const TABS = ['composer', 'team', 'backlog'];
const STORED = { hash: '#/room/cafe/backlog', scroll: {}, routes: {}, at: 1 };

/** Поддельный документ: только события (для проверки клавиш). */
function eventDoc() {
  const listeners = new Map();
  const sent = [];
  return {
    sent,
    addEventListener(type, fn) {
      (listeners.get(type) || listeners.set(type, []).get(type)).push(fn);
    },
    removeEventListener(type, fn) {
      const arr = listeners.get(type) || [];
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    dispatch(type, event) {
      for (const fn of listeners.get(type) || []) fn(event);
    },
    dispatchEvent(ev) {
      sent.push(ev);
      return true;
    },
  };
}

/** Нажатие клавиши без модификаторов. */
function key(k, extra = {}) {
  return Object.assign({
    key: k, code: '', ctrlKey: false, metaKey: false, altKey: false,
    target: null, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
  }, extra);
}

/* ────────────────────────────────────────────── 1. одно решение адреса */

test('старт: явный «дом» #/ НЕ перекидывается ни на «Сегодня», ни в память', () => {
  for (const pref of [START_TODAY, START_ROOMS, START_LAST]) {
    const plan = planStart({
      urlHash: '#/', startPref: pref, stored: STORED, roomIds: ROOMS, tabIds: TABS,
    });
    assert.equal(plan.hash, '#/', 'pref=' + pref);
    assert.equal(plan.ready, true);
    assert.equal(plan.why, 'explicit-home');
  }
});

test('старт: пустой адрес — стартовая страница из настроек', () => {
  const today = planStart({ urlHash: '', startPref: START_TODAY, stored: STORED, roomIds: ROOMS, tabIds: TABS });
  assert.equal(today.hash, '#/today');
  assert.equal(today.why, 'pref-today');

  const rooms = planStart({ urlHash: '', startPref: START_ROOMS, stored: STORED, roomIds: ROOMS, tabIds: TABS });
  assert.equal(rooms.hash, '#/');
  assert.equal(rooms.why, 'pref-rooms');

  const last = planStart({ urlHash: '', startPref: START_LAST, stored: STORED, roomIds: ROOMS, tabIds: TABS });
  assert.equal(last.hash, '#/room/cafe/backlog', 'последнее место — полный маршрут');
  assert.equal(last.why, 'pref-last');
});

test('старт: явный адрес в строке сильнее настройки (deep link)', () => {
  const plan = planStart({
    urlHash: '#/room/fn/composer', startPref: START_TODAY, stored: STORED, roomIds: ROOMS, tabIds: TABS,
  });
  assert.equal(plan.hash, '#/room/fn/composer');
  assert.equal(plan.why, 'deep-link');
  // Настройка «Комнаты» тоже не перебивает ссылку на запрос.
  const deep = planStart({
    urlHash: '#/today', startPref: START_ROOMS, stored: STORED, roomIds: ROOMS, tabIds: TABS,
  });
  assert.equal(deep.hash, '#/today');
});

test('старт: «Сегодня» без списка комнат — ждём, адрес не выдумываем', () => {
  const plan = planStart({ urlHash: '', startPref: START_TODAY, stored: STORED, roomIds: null, tabIds: TABS });
  assert.equal(plan.ready, false);
  assert.equal(plan.hash, '');
  assert.equal(plan.why, 'wait-rooms');
  // Список пришёл и он пуст — открываем «Комнаты» (пустой свод бессмыслен).
  const empty = planStart({ urlHash: '', startPref: START_TODAY, stored: STORED, roomIds: [], tabIds: TABS });
  assert.equal(empty.hash, '#/');
  assert.equal(empty.why, 'pref-today-no-rooms');
});

test('старт: «Комнаты» и «Последнее место» не ждут сервер (кэш не нужен)', () => {
  for (const pref of [START_ROOMS, START_LAST]) {
    const plan = planStart({ urlHash: '#', startPref: pref, stored: STORED, roomIds: null, tabIds: TABS });
    assert.equal(plan.ready, true, pref + ' решается сразу — прыжка нет');
    assert.ok(plan.hash.startsWith('#/'));
  }
});

test('старт: память о удалённой комнате не открывается (правила chooseHash)', () => {
  const plan = planStart({
    urlHash: '', startPref: START_LAST, stored: { hash: '#/room/gone/backlog' }, roomIds: ROOMS, tabIds: TABS,
  });
  assert.notEqual(plan.hash, '#/room/gone/backlog');
  assert.equal(plan.hash, chooseHash({ urlHash: '', stored: { hash: '#/room/gone/backlog' }, roomIds: ROOMS, tabIds: TABS }));
});

/* ────────────────────────────────────────────── 2. настройка стартовой страницы */

test('настройка: три варианта, старые записи читаются, мусор → «Сегодня»', () => {
  assert.equal(START_OPTIONS.length, 3);
  assert.deepEqual(START_OPTIONS.map((o) => o.value), [START_TODAY, START_ROOMS, START_LAST]);
  assert.deepEqual(START_OPTIONS.map((o) => o.label), ['Сегодня', 'Комнаты', 'Последнее место']);
  assert.equal(normalizeStartPref('rooms'), 'rooms');
  assert.equal(normalizeStartPref('last'), 'last');
  assert.equal(normalizeStartPref('today'), 'today');
  assert.equal(normalizeStartPref(null), 'today');
  assert.equal(normalizeStartPref('комнаты'), 'today', 'мусор не ломает запуск');
  assert.equal(START_KEY, 'teamroom.start', 'ключ прежний — выбор не теряется');
});

test('настройки: сегмент «Стартовая страница» берёт значения из route-memory.js', () => {
  const src = read('web/js/views/settings.js');
  assert.match(src, /START_OPTIONS\.map/);
  assert.match(src, /normalizeStartPref\(value\)/);
  assert.match(src, /localStorage\.setItem\(START_KEY, next\)/);
  assert.doesNotMatch(src, /localStorage\.getItem\('teamroom\.start'\)/, 'ключ не дублируется строкой');
  assert.match(src, /data-setting': 'start-page'/);
});

/* ────────────────────────────────────────────── 3. клавиши G, H */

test('клавиши G, H открывают «Все комнаты» событием, а не напрямую роутером', () => {
  const doc = eventDoc();
  const ctrl = installHotkeys({ doc });
  try {
    doc.dispatch('keydown', key('g'));
    assert.equal(doc.sent.length, 0, 'одиночная G ничего не открывает');
    doc.dispatch('keydown', key('h'));
    assert.equal(doc.sent.length, 1, 'G, H — один переход');
    assert.equal(doc.sent[0].type, NAV_EVENT);
    assert.equal(doc.sent[0].detail.hash, '#/');
  } finally {
    if (ctrl && typeof ctrl.destroy === 'function') ctrl.destroy();
    uninstallHotkeys();
  }
});

test('клавиши G, H работают в русской раскладке и не срабатывают в поле ввода', () => {
  const doc = eventDoc();
  const ctrl = installHotkeys({ doc });
  try {
    doc.dispatch('keydown', key('п', { code: 'KeyG' }));
    doc.dispatch('keydown', key('р', { code: 'KeyH' }));
    assert.equal(doc.sent.length, 1, 'физические клавиши те же');

    doc.sent.length = 0;
    const field = { nodeType: 1, tagName: 'TEXTAREA', isContentEditable: false };
    doc.dispatch('keydown', key('g', { target: field }));
    doc.dispatch('keydown', key('h', { target: field }));
    assert.equal(doc.sent.length, 0, 'пока человек печатает, клавиши молчат');
  } finally {
    if (ctrl && typeof ctrl.destroy === 'function') ctrl.destroy();
    uninstallHotkeys();
  }
});

test('клавиши G, H: ожидание второй клавиши ограничено по времени', () => {
  assert.ok(SEQUENCE_MS > 0 && SEQUENCE_MS <= 3000);
  const doc = eventDoc();
  const ctrl = installHotkeys({ doc });
  try {
    doc.dispatch('keydown', key('g'));
    // Протухшее ожидание: вторая клавиша приходит слишком поздно.
    const realNow = Date.now;
    try {
      Date.now = () => realNow() + SEQUENCE_MS + 100;
      doc.dispatch('keydown', key('h'));
    } finally {
      Date.now = realNow;
    }
    assert.equal(doc.sent.length, 0, 'через паузу последовательность не срабатывает');
  } finally {
    if (ctrl && typeof ctrl.destroy === 'function') ctrl.destroy();
    uninstallHotkeys();
  }
});

test('справка по клавишам знает G, H', () => {
  const row = HOTKEY_HELP.find((r) => Array.isArray(r.keys) && r.keys.join('') === 'GH');
  assert.ok(row, 'строка G, H есть в справке');
  assert.match(row.what, /комнат/i);
});

test('requestNavigate: без документа или с пустым адресом — тихо ничего не делает', () => {
  assert.equal(requestNavigate('', eventDoc()), false);
  assert.equal(requestNavigate('#/', null), false);
});

/* ────────────────────────────────────────────── 4. каркас: запуск без прыжка */

test('каркас: адрес выбирается ОДИН раз, до первой отрисовки, без hashchange', () => {
  const src = read('web/js/app.js');
  assert.doesNotMatch(src, /initialHash/);
  assert.match(src, /planStart\(\{/);
  assert.match(src, /history\.replaceState/, 'адрес ставится без события — одна отрисовка');
  assert.match(src, /startRouter\(await planBootRoute\(\)\)/);
  // router.start() ровно один вызов — в startRouter, а не в mountShell.
  assert.equal((src.match(/router\.start\(\)/g) || []).length, 1, 'один запуск роутера');
  const mount = src.slice(src.indexOf('function mountShell'), src.indexOf('/* ─────────────────────────────────────────────────────── старт приложения'));
  assert.doesNotMatch(mount, /router\.start\(\)/, 'до выбора адреса роутер не стартует');
  assert.match(mount, /paintBootSkeleton\(\)/);
});

test('каркас: заглушка-скелетон вместо «заготовки» и кэш списка комнат', () => {
  const src = read('web/js/app.js');
  assert.match(src, /data-boot': 'skeleton'/);
  assert.match(src, /'Загружаю…'/);
  assert.match(src, /ROOMS_CACHE_KEY = 'teamroom\.roomsCache'/);
  assert.match(src, /function readRoomsCache\(/);
  assert.match(src, /saveRoomsCache\(list\)/, 'свежий список попадает в память');
  assert.match(src, /saveRoomsCache\(next\)/, 'тихое обновление тоже обновляет память');
  const css = read('web/css/shell-rail.css');
  assert.match(css, /\.boot-start\s*\{/);
});

/* ────────────────────────────────────────────── 5. меню и крошки */

test('меню: пункт «Все комнаты» со значком дома — первым в главном меню', () => {
  const src = read('web/js/shell-sidebar.js');
  assert.match(src, /id: 'home-nav'/);
  assert.match(src, /href: '#\/', id: 'home-link'/);
  assert.match(src, /nav-item nav-item--home/);
  assert.match(src, /icon\('home'/);
  assert.match(src, /'Все комнаты'/);
  const homeAt = src.indexOf("id: 'home-link'");
  const todayAt = src.indexOf("id: 'today-link'");
  assert.ok(homeAt > 0 && todayAt > homeAt, '«Все комнаты» выше «Сегодня»');
});

test('меню: заголовок «Комната команды» — ссылка на #/', () => {
  const src = read('web/js/shell-sidebar.js');
  const brand = src.slice(src.indexOf("h('div', { class: 'brand' }"), src.indexOf("id: 'demo-badge-slot'"));
  assert.match(brand, /class: 'brand__home', href: '#\/'/);
  assert.match(brand, /APP_TITLE/);
  assert.doesNotMatch(brand, /h\('div', \{ class: 'brand__mark'/, 'марка больше не div внутри div');
  // Кнопка закрытия меню осталась вне ссылки (в ссылке интерактивный элемент — ошибка).
  const closeAt = brand.indexOf('sidebar__close');
  const linkEnd = brand.indexOf("h('span', { class: 'brand__text' }");
  assert.ok(closeAt > linkEnd, 'кнопка закрытия вне ссылки');
});

test('меню: пункт «Все комнаты» подсвечивается только на #/', () => {
  const src = read('web/js/shell-sidebar.js');
  assert.match(src, /getElementById\('home-link'\)/);
  assert.match(src, /route\.name === 'home'/);
});

test('крошки: первый элемент — «Комнаты» со ссылкой на #/', () => {
  const src = read('web/js/app.js');
  assert.match(src, /items\.unshift\(\{ text: 'Комнаты', href: '#\/'/);
  // crumbItems остаётся чистым: «Комнаты» добавляет каркас, а не чистая функция.
  assert.equal(crumbItems({ route: { name: 'home', roomId: null }, roomTitle: '' }).length, 0);
  assert.equal(crumbItems({ route: { name: 'room', roomId: 'cafe', tab: 'backlog' }, roomTitle: 'Лето' }).length, 2);
});

test('шапка: у #/ своё название, а не «заготовка»', () => {
  const src = read('web/js/app.js');
  assert.match(src, /route\.name === 'home'[\s\S]{0,400}'Все комнаты'/);
});

/* ────────────────────────────────────────────── 6. страница «Комнаты» */

test('«Комнаты»: создание комнаты и быстрый запрос живут на одной странице', () => {
  const src = read('web/js/views/rooms.js');
  assert.match(src, /'Комната'/, 'кнопка создания комнаты на месте');
  assert.match(src, /data-action': 'quick-request'/);
  assert.match(src, /data-action': 'quick-dictate'/);
  assert.match(src, /data-action': 'quick-inbox'/);
  assert.match(src, /api\.post\('\/api\/rooms\/' \+ enc\(roomId\) \+ '\/requests'/, 'запрос уходит в комнату');
  assert.match(src, /ctx\.navigate|navigate\('#\/room\//, 'после отправки открывается запрос');
  assert.match(src, /'Надиктовать'/);
  assert.match(src, /'Входящие'/);
  const css = read('web/css/view-rooms.css');
  assert.match(css, /\.rooms-quick__text[\s\S]{0,200}min-width: 0/, 'без горизонтальной прокрутки');
});

test('«Комнаты»: пустой список не оставляет тупика', () => {
  const src = read('web/js/views/rooms.js');
  assert.match(src, /Комнат пока нет — создайте первую/, 'подсказка вместо мёртвой формы');
  assert.match(src, /quickSend\.disabled = empty/);
});

test('«Комнаты»: явный #/ не редиректится самой вьюхой', () => {
  const src = read('web/js/views/rooms.js');
  assert.doesNotMatch(src, /navigate\('#\/today'\)/);
  assert.doesNotMatch(src, /location\.hash = '#\/today'/);
  assert.equal(isBareHash('#/'), true);
});
