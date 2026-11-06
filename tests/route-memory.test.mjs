/**
 * route-memory.test.mjs — память маршрута и прокрутки (волна 17, B-16).
 *
 * Проверяем без браузера:
 *   * разбор и очистка хэшей (мусор, чужие схемы, «дом»);
 *   * выбор маршрута при загрузке: явный хэш → память → комната; удалённая
 *     комната, удалённый запрос, неизвестная вкладка;
 *   * терпимость к испорченному localStorage (битый JSON, отрицательные и
 *     огромные значения, лишние ключи);
 *   * снятие и восстановление прокрутки контейнеров (#view, колонка, лента,
 *     [data-scroll-key]), ожидание «страница ещё не доросла»;
 *   * сообщение хозяину `team-room:route` при каждой смене маршрута;
 *   * живое восстановление после «перезагрузки» (новый контроллер с тем же
 *     хранилищем) — позиция возвращается.
 *
 *   node --test tests/route-memory.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ROUTE_KEY, ROUTE_MESSAGE, ROUTE_REQUEST, MAX_HASH,
  isBareHash, normalizeHash, isRouteHash, parseRouteHash, buildRoomHash,
  chooseHash, parseStored, serializeRecord, captureScroll, applyScroll,
  elementForKey, scrollKeyOf, routeMessage, createRouteMemory,
} from '../web/js/route-memory.js';

const TABS = ['composer', 'team', 'roadmap', 'backlog', 'reports', 'journal'];
const ROOMS = ['cafe', 'fn'];

/* ─────────────────────────────────────────────────────── мини-DOM */

function fakeEl({ id = '', cls = '', sk = '', top = 0, height = 4000, client = 600 } = {}) {
  const attrs = {};
  if (sk) attrs['data-scroll-key'] = sk;
  const el = {
    id, className: cls, scrollTop: top, scrollHeight: height, clientHeight: client,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    setAttribute: (name, value) => { attrs[name] = String(value); },
  };
  return el;
}

function fakeDoc({ view, aside, feed, keys = [], height = 4000 } = {}) {
  const bySelector = new Map();
  if (view) bySelector.set('#view', view);
  if (aside) bySelector.set('.req__aside', aside);
  if (feed) bySelector.set('.thr__feed', feed);
  return {
    ...bySelector,
    height,
    querySelector(sel) { return bySelector.get(sel) || null; },
    querySelectorAll(sel) { return sel === '[data-scroll-key]' ? keys : []; },
    visibilityState: 'visible',
    addEventListener() {},
    removeEventListener() {},
  };
}

function fakeStorage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    dump: () => Object.fromEntries(map),
  };
}

function fakeWindow(hash = '', extra = {}) {
  const listeners = {};
  const posted = [];
  const win = {
    location: { hash },
    parent: { postMessage: (message) => posted.push(message) },
    posted,
    addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    removeEventListener(name, fn) { listeners[name] = (listeners[name] || []).filter((f) => f !== fn); },
    dispatch(name, event) {
      for (const fn of listeners[name] || []) fn(event || {});
    },
    listeners,
    ...extra,
  };
  return win;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ───────────────────────────────────────────────────────── хэши */

test('хэш: «дом» и мусор отличаются от маршрута', () => {
  for (const bare of ['', null, undefined, '#', '#/', '   ']) assert.equal(isBareHash(bare), true, String(bare));
  assert.equal(isBareHash('#/room/cafe/composer'), false);

  assert.equal(normalizeHash('#/room/fn/request/t_1'), '#/room/fn/request/t_1');
  assert.equal(normalizeHash('/room/fn'), '#/room/fn');
  assert.equal(normalizeHash('javascript:alert(1)'), '');
  assert.equal(normalizeHash('#/room/cafe\\"><script>'), '');
  assert.equal(normalizeHash('#' + 'a'.repeat(MAX_HASH)), '');

  assert.equal(isRouteHash('#/room/cafe'), true);
  assert.equal(isRouteHash('#/'), false);
});

test('хэш: разбор маршрута и сборка вкладки', () => {
  assert.deepEqual(parseRouteHash('#/room/cafe/request/t_9f3c2a10'),
    { kind: 'request', roomId: 'cafe', tab: 'request', id: 't_9f3c2a10' });
  assert.equal(parseRouteHash('#/room/cafe/backlog').kind, 'room');
  assert.equal(parseRouteHash('#/room/cafe').tab, 'composer');
  assert.equal(parseRouteHash('#/settings/diagnostics').kind, 'settings');
  assert.equal(parseRouteHash('#/search?q=упаковка').kind, 'search');
  assert.equal(parseRouteHash('#/мусор/вот'), null);

  assert.equal(buildRoomHash('cafe'), '#/room/cafe/composer');
  assert.equal(buildRoomHash('cafe', 'backlog'), '#/room/cafe/backlog');
  assert.equal(buildRoomHash('', 'team'), '#/');
});

/* ───────────────────────────────────────────── выбор маршрута на старте */

test('старт: явный хэш сильнее памяти', () => {
  const stored = { hash: '#/room/cafe/request/t_old', scroll: {} };
  assert.equal(chooseHash({ urlHash: '#/room/fn/team', stored, roomIds: ROOMS, tabIds: TABS }),
    '#/room/fn/team');
});

test('старт: без хэша открывается ПОЛНЫЙ прежний маршрут (B-16)', () => {
  const stored = { hash: '#/room/fn/request/t_9f3c2a10', scroll: {} };
  for (const urlHash of ['', '#', undefined, null]) {
    assert.equal(chooseHash({ urlHash, stored, roomIds: ROOMS, tabIds: TABS, requestOk: true }),
      '#/room/fn/request/t_9f3c2a10', 'хэш в адресе: ' + String(urlHash));
  }
});

test('старт: явный «#/» — человек на всех комнатах, остаёмся там', () => {
  const stored = { hash: '#/room/fn/request/t_9f3c2a10', scroll: {} };
  assert.equal(chooseHash({ urlHash: '#/', stored, roomIds: ROOMS, tabIds: TABS, requestOk: true }),
    '#/');
});

test('старт: несуществующий запрос → вкладка комнаты, а не пустая страница', () => {
  const stored = { hash: '#/room/fn/request/t_deleted', scroll: {} };
  assert.equal(chooseHash({ urlHash: '', stored, roomIds: ROOMS, tabIds: TABS, requestOk: false }),
    '#/room/fn/composer');
});

test('старт: сервер молчит (requestOk=null) — маршрут не выбрасываем', () => {
  const stored = { hash: '#/room/fn/request/t_9f3c2a10', scroll: {} };
  assert.equal(chooseHash({ urlHash: '', stored, roomIds: ROOMS, tabIds: TABS, requestOk: null }),
    '#/room/fn/request/t_9f3c2a10');
});

test('старт: удалённая комната и неизвестная вкладка', () => {
  const stored = { hash: '#/room/old/request/t_1', scroll: {} };
  assert.equal(chooseHash({ urlHash: '', stored, roomIds: ROOMS, tabIds: TABS, defaultRoom: 'cafe' }),
    '#/room/cafe/composer');

  const tabStored = { hash: '#/room/cafe/старое', scroll: {} };
  assert.equal(chooseHash({ urlHash: '', stored: tabStored, roomIds: ROOMS, tabIds: TABS }),
    '#/room/cafe/composer');
});

test('старт: пустая память и нет комнат → «все комнаты»', () => {
  assert.equal(chooseHash({ urlHash: '', stored: null, roomIds: [], tabIds: TABS }), '#/');
  assert.equal(chooseHash({ urlHash: '', stored: parseStored(null), roomIds: ['fn'], tabIds: TABS }),
    '#/room/fn/composer');
  assert.equal(chooseHash({ urlHash: '#/settings', stored: null, roomIds: [], tabIds: TABS }), '#/settings');
  assert.equal(chooseHash({ urlHash: '#/', stored: null, roomIds: ['fn'], tabIds: TABS }), '#/');
});

/* ─────────────────────────────────────────────── испорченное хранилище */

test('запись: битый JSON и мусорные значения не ломают память', () => {
  assert.deepEqual(parseStored('{не json'), { hash: '', scroll: {}, routes: {}, at: 0 });
  assert.deepEqual(parseStored('[]'), { hash: '', scroll: {}, routes: {}, at: 0 });
  assert.deepEqual(parseStored(null), { hash: '', scroll: {}, routes: {}, at: 0 });

  const dirty = JSON.stringify({
    hash: '#/room/cafe',
    at: 'вчера',
    scroll: { view: 120, aside: -5, feed: 'нет', 'плохой ключ': 10, huge: 1e12, 'sk:q': 300 },
    routes: { '#/room/fn/backlog': { feed: 40 }, 'мусор': { view: 10 } },
  });
  const parsed = parseStored(dirty);
  assert.equal(parsed.hash, '#/room/cafe');
  assert.deepEqual(parsed.scroll, { view: 120, 'sk:q': 300 });
  assert.deepEqual(parsed.routes, { '#/room/fn/backlog': { feed: 40 } });
  assert.equal(parsed.at, 0);

  const again = parseStored(serializeRecord(parsed));
  assert.deepEqual(again.scroll, parsed.scroll);
  assert.equal(again.hash, parsed.hash);
});

/* ───────────────────────────────────────────────────────── прокрутка */

test('прокрутка: снимаем только сдвинутые контейнеры, ключи стабильны', () => {
  const aside = fakeEl({ cls: 'req__aside', top: 1200 });
  const feed = fakeEl({ cls: 'thr__feed', top: 0 });
  const view = fakeEl({ id: 'view', top: 80 });
  const extra = fakeEl({ sk: 'steps', top: 42 });
  const doc = fakeDoc({ view, aside, feed, keys: [extra] });

  assert.equal(scrollKeyOf(aside), 'aside');
  assert.equal(scrollKeyOf(feed), 'feed');
  assert.equal(scrollKeyOf(view), 'view');
  assert.equal(scrollKeyOf(extra), 'sk:steps');

  assert.deepEqual(captureScroll(doc), { view: 80, aside: 1200, 'sk:steps': 42 });
});

test('прокрутка: восстановление ставит позиции и ждёт «не доросло»', () => {
  const aside = fakeEl({ cls: 'req__aside', top: 0, height: 4000, client: 600 });
  const feed = fakeEl({ cls: 'thr__feed', top: 0, height: 500, client: 600 });
  const doc = fakeDoc({ aside, feed });

  assert.equal(elementForKey(doc, 'aside'), aside);
  const first = applyScroll(doc, { aside: 1500, feed: 200 });
  assert.equal(aside.scrollTop, 1500, 'колонка встала на место');
  assert.equal(first.done, false, 'лента ещё короткая — нужна ещё попытка');
  assert.deepEqual(first.waiting, ['feed']);

  feed.scrollHeight = 2000;          // данные доехали
  const second = applyScroll(doc, { aside: 1500, feed: 200 });
  assert.equal(second.done, true);
  assert.equal(feed.scrollTop, 200);
  assert.deepEqual(second.applied.sort(), ['aside', 'feed']);
});

/* ───────────────────────────────────────────────────── контроллер */

test('память: маршрут и прокрутка переживают «перезагрузку» страницы', async () => {
  const storage = fakeStorage();
  const aside = fakeEl({ cls: 'req__aside', top: 2400, height: 9000, client: 700 });
  const doc = fakeDoc({ aside });
  const win = fakeWindow('#/room/fn/request/t_9f3c2a10');

  const first = createRouteMemory({ doc, win, storage });
  first.connect();
  first.remember('#/room/fn/request/t_9f3c2a10');
  first.saveScroll();
  assert.equal(win.posted.at(-1).type, ROUTE_MESSAGE);
  assert.equal(win.posted.at(-1).hash, '#/room/fn/request/t_9f3c2a10');

  // «Полная перезагрузка»: новый документ, тот же localStorage, адрес без хэша.
  const fresh = fakeEl({ cls: 'req__aside', top: 0, height: 9000, client: 700 });
  const freshDoc = fakeDoc({ aside: fresh });
  const freshWin = fakeWindow('');
  const second = createRouteMemory({ doc: freshDoc, win: freshWin, storage });

  assert.equal(second.read().hash, '#/room/fn/request/t_9f3c2a10');
  assert.equal(chooseHash({
    urlHash: '', stored: second.read(), roomIds: ROOMS, tabIds: TABS, requestOk: true,
  }), '#/room/fn/request/t_9f3c2a10');

  second.restore('#/room/fn/request/t_9f3c2a10', { interval: 20 });
  await wait(120);
  assert.equal(fresh.scrollTop, 2400, 'позиция колонки вернулась');
});

test('память: смена маршрута сохраняет старое место и рассказывает хозяину', () => {
  const storage = fakeStorage();
  const aside = fakeEl({ cls: 'req__aside', top: 900 });
  const doc = fakeDoc({ aside });
  const win = fakeWindow('#/room/fn/request/t_1');
  const memory = createRouteMemory({ doc, win, storage });
  memory.connect();

  aside.scrollTop = 900;
  win.location.hash = '#/room/fn/backlog';
  win.dispatch('hashchange');

  const stored = parseStored(storage.getItem(ROUTE_KEY));
  assert.equal(stored.hash, '#/room/fn/backlog');
  assert.equal(stored.routes['#/room/fn/request/t_1'].aside, 900, 'место запроса запомнено');
  assert.equal(win.posted.at(-1).hash, '#/room/fn/backlog');
});

test('память: запрос хозяина «повтори маршрут» — отвечаем текущим', () => {
  const storage = fakeStorage();
  const win = fakeWindow('#/room/cafe/request/t_2');
  const memory = createRouteMemory({ doc: fakeDoc({}), win, storage });
  memory.connect();
  win.dispatch('message', { data: { type: ROUTE_REQUEST } });
  assert.equal(win.posted.at(-1).hash, '#/room/cafe/request/t_2');
});

test('память: «дом» забывает маршрут, мусор — нет', () => {
  const storage = fakeStorage();
  const memory = createRouteMemory({ doc: fakeDoc({}), win: fakeWindow(''), storage });
  memory.remember('#/room/cafe/request/t_3');
  assert.equal(memory.read().hash, '#/room/cafe/request/t_3');

  memory.remember('#/');
  assert.equal(memory.read().hash, '', 'возврат «на все комнаты» — маршрут забыт');

  memory.remember('#/room/cafe/team');
  memory.remember('javascript:alert(1)');
  assert.equal(memory.read().hash, '#/room/cafe/team', 'мусор память не портит');
});

test('память: без localStorage (приватный режим) работает и не падает', () => {
  const win = fakeWindow('#/room/cafe');
  const memory = createRouteMemory({ doc: fakeDoc({}), win, storage: null });
  memory.remember('#/room/cafe/backlog');
  memory.saveScroll();
  assert.equal(memory.read().hash, '', 'хранилища нет — помнить нечего');
  assert.equal(routeMessage('#/room/cafe').type, ROUTE_MESSAGE);
});
