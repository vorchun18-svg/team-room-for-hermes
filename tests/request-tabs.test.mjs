/**
 * request-tabs.test.mjs — открытые запросы вкладками (W29-Q7).
 *
 * Проверяем без браузера:
 *   1) список вкладок: добавление, дубли, потолок, закрепление;
 *   2) соседняя вкладка по кругу (Alt+←/→) и выбор после закрытия;
 *   3) отрисовка: ссылка с настоящим адресом, подсветка активной, × и 📌;
 *   4) закрытие активной переводит на соседнюю, последней — на страницу комнаты;
 *   5) закреплённая вкладка не закрывается Ctrl+W;
 *   6) состояние переживает «перезапуск» (то же хранилище, новый компонент);
 *   7) терпимость к мусору в localStorage.
 *
 *   node --test tests/request-tabs.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installMiniDom, resetMiniDom, DOC } from './mini-dom.mjs';

installMiniDom();

const {
  TABS_KEY, MAX_TABS, parseTabs, serializeTabs, addTab, removeTab, toggleTabPin,
  sortTabs, neighborTab, afterClose, tabHash, tabLabel,
  createRequestTabs, installTabKeys,
} = await import('../web/js/views/request-tabs.js');
const { h } = await import('../web/js/util.js');

function storage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    dump: () => Object.fromEntries(map),
  };
}

function freshHost() {
  resetMiniDom();
  const box = h('div', { id: 'req-tabs' });
  DOC.body.appendChild(box);
  return box;
}

test('потолок вкладок и порядок: закреплённые впереди', () => {
  assert.equal(MAX_TABS, 8);
  let list = [];
  for (let i = 1; i <= 10; i += 1) list = addTab(list, { roomId: 'cafe', id: 'r' + i, title: 'Запрос ' + i });
  assert.equal(list.length, MAX_TABS);
  assert.deepEqual(list.map((t) => t.id), ['r3', 'r4', 'r5', 'r6', 'r7', 'r8', 'r9', 'r10']);
  list = toggleTabPin(list, 'r7');
  assert.equal(list[0].id, 'r7', 'закреплённая вкладка идёт первой');
  assert.equal(toggleTabPin(list, 'r7')[0].pinned, false);
});

test('повторное открытие не плодит вкладку, а обновляет название', () => {
  let list = addTab([], { roomId: 'cafe', id: 'r1', title: 'Старое' });
  list = addTab(list, { roomId: 'cafe', id: 'r1', title: 'Новое' });
  assert.equal(list.length, 1);
  assert.equal(list[0].title, 'Новое');
});

test('соседняя вкладка по кругу и выбор после закрытия', () => {
  const list = [
    { id: 'a', roomId: 'cafe', title: 'A', pinned: false },
    { id: 'b', roomId: 'cafe', title: 'B', pinned: false },
    { id: 'c', roomId: 'cafe', title: 'C', pinned: false },
  ];
  assert.equal(neighborTab(list, 'a', 1), 'b');
  assert.equal(neighborTab(list, 'c', 1), 'a', 'вправо с последней — по кругу');
  assert.equal(neighborTab(list, 'a', -1), 'c', 'влево с первой — по кругу');
  assert.equal(neighborTab(list, '', 1), 'a', 'активной нет — первая');
  assert.equal(neighborTab([list[0]], 'a', 1), '', 'одна вкладка — переключать некуда');
  assert.deepEqual(afterClose(list, 'b', 'b'), { id: 'c', roomId: 'cafe' });
  assert.deepEqual(afterClose(list, 'c', 'c'), { id: 'b', roomId: 'cafe' });
  assert.deepEqual(afterClose([list[0]], 'a', 'a'), { id: '', roomId: '' });
});

test('адрес и подпись вкладки', () => {
  assert.equal(tabHash({ roomId: 'cafe', id: 'r 1' }), '#/room/cafe/request/r%201');
  assert.equal(tabHash(null), '#/');
  assert.equal(tabLabel({ title: '' }), 'Новая задача');
  assert.equal(tabLabel({ title: 'Ответ клиенту на претензию по химчистке' }), 'Ответ клиенту на претензию по химчистке');
  assert.equal(tabLabel({ title: 'О'.repeat(60) }).length, 42);
});

test('мусор в localStorage не ломает список', () => {
  assert.deepEqual(parseTabs('{не json'), []);
  assert.deepEqual(parseTabs(null), []);
  assert.deepEqual(parseTabs([null, 3, { id: 'x' }, { id: 'y', roomId: '' }]), []);
  const good = parseTabs([{ id: 'a', roomId: 'cafe', title: 'A', pinned: true },
    { id: 'a', roomId: 'cafe' }]);
  assert.deepEqual(good, [{ id: 'a', roomId: 'cafe', title: 'A', pinned: true }]);
});

test('полоса вкладок: ссылка, подсветка, закрытие и переключение', () => {
  const box = freshHost();
  const store = storage();
  const went = [];
  const tabs = createRequestTabs({ host: box, storage: store, navigate: (hash) => went.push(hash) });

  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r1' }, { title: 'Сверка накладных' });
  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r2' }, { title: 'Меню на неделю' });
  assert.equal(tabs.list().length, 2);
  assert.equal(tabs.activeId(), 'r2');
  assert.equal(box.hidden, false);

  const links = box.querySelectorAll('.req-tabs__link');
  assert.equal(links.length, 2);
  assert.equal(links[0].getAttribute('href'), '#/room/cafe/request/r1');
  assert.equal(links[1].getAttribute('aria-current'), 'page', 'текущая вкладка помечена');
  assert.equal(links[0].getAttribute('aria-current'), null);

  // Alt+← — на соседнюю слева
  assert.equal(tabs.step(-1), true);
  assert.deepEqual(went, ['#/room/cafe/request/r1']);

  // переключение по кругу: слева от первой — последняя
  assert.equal(tabs.step(-1), true);
  assert.deepEqual(went, ['#/room/cafe/request/r1', '#/room/cafe/request/r2']);
  assert.deepEqual(JSON.parse(store.dump()[TABS_KEY]).tabs.map((t) => t.id), ['r1', 'r2']);
});

test('закрытие активной вкладки ведёт на соседнюю, последней — на страницу комнаты', () => {
  const box = freshHost();
  const went = [];
  const tabs = createRequestTabs({ host: box, storage: storage(), navigate: (hash) => went.push(hash) });
  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r1' }, { title: 'A' });
  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r2' }, { title: 'B' });
  tabs.close('r2');
  assert.deepEqual(went, ['#/room/cafe/request/r1']);
  assert.equal(tabs.list().length, 1);
  tabs.close('r1');
  assert.deepEqual(went, ['#/room/cafe/request/r1', '#/room/cafe/composer']);
  assert.equal(box.hidden, true, 'вкладок нет — полосы нет');
});

test('закреплённая вкладка переживает Ctrl+W, обычная — закрывается', () => {
  const box = freshHost();
  const went = [];
  const tabs = createRequestTabs({ host: box, storage: storage(), navigate: (hash) => went.push(hash) });
  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r1' }, { title: 'A' });
  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r2' }, { title: 'B' });
  tabs.togglePin('r2');
  assert.equal(tabs.list()[0].id, 'r2', 'закреплённая — впереди');
  assert.equal(tabs.closeActive(), false, 'закреплённую Ctrl+W не закрывает');
  assert.equal(tabs.list().length, 2);
});

test('клавиши вкладок: Alt+←/→ переключают, Ctrl+W закрывает', () => {
  const box = freshHost();
  const went = [];
  const tabs = createRequestTabs({ host: box, storage: storage(), navigate: (hash) => went.push(hash) });
  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r1' }, { title: 'A' });
  tabs.sync({ name: 'request', roomId: 'cafe', id: 'r2' }, { title: 'B' });

  const handlers = [];
  const doc = {
    addEventListener: (type, fn) => handlers.push([type, fn]),
    removeEventListener: () => {},
  };
  const off = installTabKeys({ getTabs: () => tabs, doc });
  assert.equal(handlers[0][0], 'keydown');
  const fire = (ev) => handlers[0][1](ev);

  let prevented = 0;
  fire({ key: 'ArrowLeft', altKey: true, preventDefault: () => { prevented += 1; } });
  assert.equal(prevented, 1);
  assert.deepEqual(went, ['#/room/cafe/request/r1']);

  fire({ key: 'ArrowRight', altKey: true, ctrlKey: false, preventDefault: () => {} });
  assert.deepEqual(went, ['#/room/cafe/request/r1', '#/room/cafe/request/r2']);

  prevented = 0;
  fire({ key: 'w', ctrlKey: true, preventDefault: () => { prevented += 1; }, stopPropagation: () => {} });
  assert.equal(prevented, 1, 'Ctrl+W закрыл вкладку, а не окно браузера');
  assert.equal(tabs.list().length, 1);
  off();
});

test('состояние вкладок переживает перезапуск', () => {
  const store = storage();
  const first = freshHost();
  const t1 = createRequestTabs({ host: first, storage: store, navigate: () => {} });
  t1.sync({ name: 'request', roomId: 'cafe', id: 'r1' }, { title: 'Сверка накладных' });
  t1.togglePin('r1');
  t1.destroy();

  const second = freshHost();
  const t2 = createRequestTabs({ host: second, storage: store, navigate: () => {} });
  t2.sync({ name: 'room', roomId: 'cafe', tab: 'composer' });
  assert.deepEqual(t2.list().map((x) => x.id), ['r1']);
  assert.equal(t2.list()[0].pinned, true);
  assert.equal(t2.activeId(), '', 'на вкладке комнаты активной вкладки запроса нет');
  assert.equal(second.hidden, false, 'вкладки остаются на виду в комнате');
});
