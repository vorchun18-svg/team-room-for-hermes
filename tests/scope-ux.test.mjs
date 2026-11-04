import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import {
  REQUEST_PARAM, WHOLE_ROOM_LABEL, FILTER_LABEL,
  requestFilterOf, scopeHash, filterByRequest, crumbItems,
  requestTitle, resetRequestTitles, sourceTitle, sourceHint, requestFilterChip,
} from '../web/js/scope.js';

/* --------------------------------------------------------------- помощники */

function collect(root, cls) {
  const out = [];
  const walk = (node) => {
    if (!node) return;
    if (node.nodeType === 1) {
      const value = (node.getAttribute('class') || '').split(/\s+/);
      if (cls.some((c) => value.includes(c))) out.push(node);
    }
    for (const child of node.childNodes || []) walk(child);
  };
  walk(root);
  return out;
}

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

/** Маршрут страницы запроса (как parseHash). */
const requestRoute = {
  name: 'request', view: 'request', roomId: 'cafe', tab: 'request', id: 't_a1b2c3d4',
  params: { roomId: 'cafe', tab: 'request', id: 't_a1b2c3d4' }, query: {},
};
const tabRoute = (tab, query = {}) => ({
  name: 'room', view: tab, roomId: 'cafe', tab, id: null,
  params: Object.assign({ roomId: 'cafe', tab, id: null }, query), query,
});

/* ---------------------------------------------------------------- крошки */

test('параметр фильтра называется request (согласовано с BACKLOG-FIX)', () => {
  assert.equal(REQUEST_PARAM, 'request');
  assert.equal(WHOLE_ROOM_LABEL, 'вся комната');
  assert.equal(FILTER_LABEL, 'Только этот запрос');
});

test('крошки: страница запроса — комната, затем «Запрос «…»»', () => {
  const items = crumbItems({ route: requestRoute, roomTitle: 'Лето', requestTitle: 'Юнит-экономика набора' });
  assert.equal(items.length, 2);
  assert.equal(items[0].text, 'Комната «Лето»');
  // ссылка ведёт на уровень выше — в комнату, а не на саму страницу
  assert.equal(items[0].href, '#/room/cafe/composer');
  assert.notEqual(items[0].current, true);
  assert.equal(items[1].text, 'Запрос «Юнит-экономика набора»');
  assert.equal(items[1].href, undefined);
  assert.equal(items[1].current, true);
});

test('крошки: без названия запроса — просто «Запрос» (без id в тексте)', () => {
  const items = crumbItems({ route: requestRoute, roomTitle: 'Лето', requestTitle: '' });
  assert.equal(items[1].text, 'Запрос');
  assert.ok(!/t_[0-9a-f]+/.test(items[1].text));
});

test('крошки: у вкладок комнаты бейдж «вся комната»', () => {
  const items = crumbItems({
    route: tabRoute('reports'),
    roomTitle: 'Лето',
    tabTitle: (id) => (id === 'reports' ? 'Отчёты' : id),
  });
  assert.equal(items.length, 2);
  assert.equal(items[0].href, '#/room/cafe/composer');
  assert.equal(items[1].text, 'Отчёты');
  assert.equal(items[1].badge, 'вся комната');
  assert.equal(items[1].current, true);
});

test('крошки: на обзоре комнат и без комнаты — не рисуем', () => {
  assert.deepEqual(crumbItems({ route: { name: 'home', roomId: null }, roomTitle: '' }), []);
  assert.deepEqual(crumbItems({ route: requestRoute, roomTitle: '' }), []);
});

/* ---------------------------------------------------------------- фильтр */

test('requestFilterOf: читает route.query, params и голый query', () => {
  assert.equal(requestFilterOf(tabRoute('journal', { request: 't_a1b2c3d4' })), 't_a1b2c3d4');
  assert.equal(requestFilterOf({ query: { request: 't_abc12345' } }), 't_abc12345');
  assert.equal(requestFilterOf({ request: ' t_abc12345 ' }), 't_abc12345');
  assert.equal(requestFilterOf(tabRoute('journal')), '');
  assert.equal(requestFilterOf(null), '');
});

test('scopeHash: добавляет и снимает ?request', () => {
  assert.equal(scopeHash('cafe', 'reports', 't_a1b2c3d4'), '#/room/cafe/reports?request=t_a1b2c3d4');
  assert.equal(scopeHash('cafe', 'journal'), '#/room/cafe/journal');
  assert.equal(scopeHash('cafe', 'journal', ''), '#/room/cafe/journal');
  assert.equal(scopeHash('cafe', 'reports', null), '#/room/cafe/reports');
});

test('filterByRequest: оставляет только записи этого запроса', () => {
  const items = [
    { id: 1, request_id: 't_a1b2c3d4' },
    { id: 2, request_id: 't_ffff0000' },
    { id: 3 },
    null,
    { id: 4, request: 't_a1b2c3d4' },
  ];
  assert.deepEqual(filterByRequest(items, 't_a1b2c3d4').map((x) => x.id), [1, 4]);
  assert.deepEqual(filterByRequest(items, '').map((x) => x.id), [1, 2, 3, 4]); // без фильтра — все объекты
  assert.deepEqual(filterByRequest([], 't_a1b2c3d4'), []);
  assert.deepEqual(filterByRequest(null, 't_a1b2c3d4'), []);
});

test('filterByRequest: журнал фильтруется по request_id события', () => {
  const events = [
    { id: 'e1', text: 'Запрос создан', request_id: 't_a1b2c3d4' },
    { id: 'e2', text: 'Решение', request_id: null },
  ];
  assert.deepEqual(filterByRequest(events, 't_a1b2c3d4').map((e) => e.id), ['e1']);
});

/* --------------------------------------------------------- название вместо id */

test('sourceTitle/sourceHint: id в тексте не показываем, только в title', () => {
  assert.equal(sourceTitle('Юнит-экономика набора', 't_a1b2c3d4'), 'Юнит-экономика набора');
  assert.equal(sourceTitle('t_a1b2c3d4', 't_a1b2c3d4'), 'запрос-источник');
  assert.equal(sourceTitle('', 't_a1b2c3d4'), 'запрос-источник');
  const hint = sourceHint('Юнит-экономика набора', 't_a1b2c3d4');
  assert.ok(hint.includes('Юнит-экономика набора'));
  assert.ok(hint.includes('t_a1b2c3d4'));
  assert.ok(!/t_[0-9a-f]+/.test(sourceTitle('Юнит-экономика набора', 't_a1b2c3d4')));
});

test('requestTitle: берёт название из /api/requests и кеширует', async () => {
  resetRequestTitles();
  const calls = [];
  const api = {
    get: async (path) => { calls.push(path); return { id: 't_a1b2c3d4', title: 'Каналы продаж' }; },
  };
  assert.equal(await requestTitle(api, 't_a1b2c3d4'), 'Каналы продаж');
  assert.equal(await requestTitle(api, 't_a1b2c3d4'), 'Каналы продаж');
  assert.equal(calls.length, 1);
  assert.equal(calls[0], '/api/requests/t_a1b2c3d4');
  resetRequestTitles();
});

test('requestTitle: сервер молчит — пустая строка, а не падение', async () => {
  resetRequestTitles();
  const api = { get: async () => { throw new Error('нет связи'); } };
  assert.equal(await requestTitle(api, 't_deadbeef'), '');
  assert.equal(await requestTitle(api, ''), '');
  resetRequestTitles();
});

/* ------------------------------------------------------------- чип фильтра */

if (typeof FakeElement.prototype.removeChild !== 'function') {
  FakeElement.prototype.removeChild = function removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
    return node;
  };
}

test('чип «Только этот запрос ✕»: клик снимает фильтр', () => {
  const dom = installFakeDom();
  try {
    let cleared = 0;
    const chip = requestFilterChip({
      requestId: 't_a1b2c3d4',
      title: 'Каналы продаж',
      onClear: () => { cleared += 1; },
    });
    assert.equal(chip.tagName.toLowerCase(), 'button');
    assert.ok((chip.getAttribute('class') || '').includes('scope-chip'));
    assert.equal(text(chip), FILTER_LABEL + '✕');
    assert.ok((chip.getAttribute('title') || '').includes('t_a1b2c3d4'));

    const handler = (chip.listeners && chip.listeners.click) || [];
    assert.equal(handler.length, 1);
    handler[0]({ stopPropagation() {} });
    assert.equal(cleared, 1);
    assert.ok(dom);
  } finally {
    uninstallFakeDom();
  }
});
