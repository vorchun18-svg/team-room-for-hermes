/**
 * tests/focus-mode.test.mjs — «Фокус» внутри страницы (W25-L2, R5 §7.4, §7.7).
 * Чистые функции + контроллер: состояние на маршрут, атрибут на `#app`, сброс
 * при уходе со страницы. Без браузера.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FOCUS_KEY, routeKeyOf, readFocusMap, focusFor, writeFocus, focusAttr, installFocusMode,
} from '../web/js/focus-mode.js';

function memStorage(init = {}) {
  const map = new Map(Object.entries(init));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    dump: () => Object.fromEntries(map),
  };
}

test('routeKeyOf: свой ключ у запроса, комнаты и простой строки', () => {
  assert.equal(routeKeyOf({ name: 'request', roomId: 'cafe', id: 't1' }), 'request/cafe/t1');
  assert.equal(routeKeyOf({ name: 'backlog', roomId: 'cafe' }), 'backlog/cafe');
  assert.equal(routeKeyOf('room/cafe'), 'room/cafe');
  assert.equal(routeKeyOf(null), '~');
  assert.equal(routeKeyOf(''), '~');
  assert.equal(routeKeyOf({}), '~');
});

test('focusFor по умолчанию выключен', () => {
  const store = memStorage();
  assert.equal(focusFor(store, 'request/cafe/t1'), false);
});

test('writeFocus помнит состояние НА МАРШРУТ, соседний маршрут не меняется', () => {
  const store = memStorage();
  writeFocus(store, 'request/cafe/t1', true);
  assert.equal(focusFor(store, 'request/cafe/t1'), true);
  assert.equal(focusFor(store, 'request/cafe/t2'), false);
  writeFocus(store, 'request/cafe/t1', false);
  assert.equal(focusFor(store, 'request/cafe/t1'), false);
});

test('состояние живёт под ключом teamroom.focus', () => {
  const store = memStorage();
  writeFocus(store, 'request/cafe/t1', true);
  const raw = store.dump()[FOCUS_KEY];
  assert.ok(raw, 'ключ teamroom.focus должен быть записан');
  assert.deepEqual(JSON.parse(raw), { 'request/cafe/t1': true });
});

test('битое хранилище читается как «выключено», запись не бросает', () => {
  assert.deepEqual(readFocusMap(null), {});
  assert.deepEqual(readFocusMap({ getItem() { throw new Error('denied'); } }), {});
  const broken = { getItem: () => '{не json', setItem() { throw new Error('denied'); } };
  assert.deepEqual(readFocusMap(broken), {});
  assert.equal(writeFocus(broken, 'x', true), true);
  assert.deepEqual(readFocusMap(memStorage({ [FOCUS_KEY]: '"строка"' })), {});
});

test('карта не растёт бесконечно: помним не больше 40 маршрутов', () => {
  const store = memStorage();
  for (let i = 0; i < 60; i += 1) writeFocus(store, 'r' + i, true);
  const map = readFocusMap(store);
  assert.ok(Object.keys(map).length <= 40, 'карта должна обрезаться до 40');
  assert.equal(map.r59, true, 'последняя запись должна остаться');
});

test('focusAttr: включено → on, выключено → off', () => {
  assert.equal(focusAttr(true), 'on');
  assert.equal(focusAttr(false), 'off');
  assert.equal(focusAttr(undefined), 'off');
});

test('installFocusMode: on/off ставят data-focus и пишут на маршрут', () => {
  const store = memStorage();
  const app = { dataset: {} };
  const ctl = installFocusMode({ app, storage: store, route: { name: 'request', roomId: 'cafe', id: 't1' } });
  assert.equal(ctl.isOn(), false);
  assert.equal(ctl.on(), true);
  assert.equal(app.dataset.focus, 'on');
  assert.equal(focusFor(store, 'request/cafe/t1'), true);
  assert.equal(ctl.off(), false);
  assert.equal(app.dataset.focus, 'off');
  assert.equal(focusFor(store, 'request/cafe/t1'), false);
});

test('installFocusMode: переживает «перезагрузку» — сохранённое состояние применяется', () => {
  const store = memStorage({ [FOCUS_KEY]: JSON.stringify({ 'request/cafe/t1': true }) });
  const app = { dataset: {} };
  const ctl = installFocusMode({ app, storage: store, route: { name: 'request', roomId: 'cafe', id: 't1' } });
  assert.equal(ctl.isOn(), true);
  ctl.on(); // применяем состояние к атрибуту (так делает страница при монтировании)
  assert.equal(app.dataset.focus, 'on');
});

test('installFocusMode: toggle переключает одним нажатием', () => {
  const store = memStorage();
  const app = { dataset: {} };
  const ctl = installFocusMode({ app, storage: store, routeKey: 'request/cafe/t9' });
  assert.equal(ctl.toggle(), true);
  assert.equal(app.dataset.focus, 'on');
  assert.equal(ctl.toggle(), false);
  assert.equal(app.dataset.focus, 'off');
});

test('installFocusMode: onChange сообщает наружу (для перерисовки)', () => {
  const seen = [];
  const app = { dataset: {} };
  const ctl = installFocusMode({ app, storage: memStorage(), routeKey: 'r', onChange: (on) => seen.push(on) });
  ctl.on();
  ctl.off();
  assert.deepEqual(seen, [true, false]);
});

test('installFocusMode: destroy снимает фокус — он не переносится на другие экраны', () => {
  const store = memStorage();
  const app = { dataset: {} };
  const ctl = installFocusMode({ app, storage: store, routeKey: 'r' });
  ctl.on();
  ctl.destroy();
  assert.equal(app.dataset.focus, 'off');
  assert.equal(ctl.isOn(), false);
  // Запись на маршруте при этом сохранена: вернувшись на запрос, человек получит свой выбор.
  assert.equal(readFocusMap(store).r, true);
});
