/**
 * room-dialog-tabs.test.mjs — волна 19, пакет R2 (B-30…B-33).
 *
 * Что проверяем (по замечаниям Михаила):
 *  • B-30 — в окне нет поля «Доска», пока доска одна; в запрос создания она
 *    по-прежнему уходит (сервер её ждёт);
 *  • B-31 — окно НЕ закрывается само ни от опроса, ни от перерисовки страницы,
 *    ни от смены маршрута; Esc/клик мимо окна/«Отмена» с несохранёнными правками
 *    ничего не теряют (плашка «Сохранить / Закрыть без сохранения / Вернуться»);
 *    введённое восстанавливается после перезагрузки страницы;
 *  • B-32 — пять разделов-вкладок, один на экране; модели и «Особенности» — одной
 *    строкой со ссылкой в карточку команды, без второго редактора;
 *  • B-33 — «Сейчас: включён/выключен» заметной плашкой, включение сохраняется
 *    и видно после повторного открытия.
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';

/* ───────────────────────────────────────────────────────── мини-DOM */

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  doc.activeElement = null;
  doc.listeners = Object.create(null);
  doc.addEventListener = (type, fn) => {
    (doc.listeners[type] || (doc.listeners[type] = [])).push(fn);
  };
  doc.removeEventListener = (type, fn) => {
    const list = doc.listeners[type] || [];
    const idx = list.indexOf(fn);
    if (idx !== -1) list.splice(idx, 1);
  };
  globalThis.document = doc;
  try { if (globalThis.sessionStorage) globalThis.sessionStorage.clear(); } catch { /* нет хранилища */ }
  return doc;
}

FakeElement.prototype.focus = function focus() { globalThis.document.activeElement = this; };
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};
FakeElement.prototype.setSelectionRange = function setSelectionRange() {};
FakeElement.prototype.closest = function closest() { return null; };
FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
  this.childNodes = [];
  for (const node of nodes) if (node) this.appendChild(node);
};
FakeElement.prototype.removeChild = function removeChild(node) {
  const idx = this.childNodes.indexOf(node);
  if (idx === -1) throw new Error('removeChild: нет такого узла');
  this.childNodes.splice(idx, 1);
  node.parentNode = null;
  return node;
};
FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
  const idx = ref ? this.childNodes.indexOf(ref) : -1;
  if (idx === -1) return this.appendChild(node);
  node.parentNode = this;
  return this.childNodes.splice(idx, 0, node)[0];
};
Object.defineProperty(FakeElement.prototype, 'options', {
  get() { return this.childNodes.filter((n) => n.nodeType === 1); },
  configurable: true,
});

function walk(node, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    out.push(child);
    walk(child, out);
  }
  return out;
}

function byAttr(root, name, value) {
  return walk(root).find((el) => el.getAttribute(name) === value) || null;
}

function byText(root, text, tag) {
  return walk(root).find((el) => String(el.tagName).toLowerCase() === tag
    && el.textContent.trim() === text) || null;
}

const byClass = (root, cls) => walk(root).filter((el) => String(el.className).split(/\s+/).includes(cls));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Событие всплывает до form/document — как в браузере (в мини-DOM нет всплытия). */
function bubble(node, type) {
  let current = node;
  const ev = { type, target: node, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  while (current) {
    current.dispatch(type, ev);
    current = current.parentNode;
  }
  return ev;
}

/** Нажать клавишу «как в браузере»: обработчики документа получают событие. */
function fireKey(doc, key, target) {
  const ev = {
    key,
    target: target || doc.body,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
    stopImmediatePropagation() {},
  };
  for (const fn of (doc.listeners.keydown || []).slice()) fn(ev);
  return ev;
}

/* ────────────────────────────────────────────────────────── данные */

const PROFILES = [
  { profile: 'biz-director', title: 'Руководитель проекта', kind: 'director' },
  { profile: 'biz-critic', title: 'Критик', kind: 'critic' },
  { profile: 'biz-ops', title: 'Операции', kind: 'worker' },
  { profile: 'biz-cfo', title: 'Экономика Лето', kind: 'worker' },
];

function room(over) {
  return Object.assign({
    id: 'cafe', title: 'Лето', icon: 'project',
    orchestrator: 'biz-director', critic: 'biz-critic',
    members: [
      { profile: 'biz-director', role: 'orchestrator' },
      { profile: 'biz-critic', role: 'critic' },
      { profile: 'biz-ops', role: 'worker' },
      { profile: 'biz-cfo', role: 'worker' },
    ],
    context_files: [], project_dir: 'C:\\Проекты\\Лето',
    critic_pass: true, max_rounds: 2, critic_mode: 'rounds', critic_round_cap: 12,
    project_mode: false, launch_mode: 'plan_first', max_parallel_streams: 2,
    phases: [], agent_notes: {}, agent_models: {},
    agent_models_meta: {
      'biz-ops': { profile: 'biz-ops', role: 'worker', model_settings: { provider: 'p', model: 'dilizhans-pro', reasoning: 'high', source: 'room' } },
    },
  }, over || {});
}

function ctxFor(state) {
  const calls = [];
  const toasts = [];
  const navigated = [];
  const api = {
    get: async (path) => {
      calls.push(['GET', path]);
      if (path === '/api/profiles') return PROFILES;
      if (path === '/api/models') {
        return {
          providers: [{ id: 'p', label: 'Провайдер', models: [{ id: 'dilizhans-pro', label: 'Дилижанс про' }] }],
          efforts: [{ id: 'high', label: 'высокое мышление' }],
        };
      }
      if (path === '/api/rooms/check-dir') return { allowed: true, exists: true };
      if (String(path).startsWith('/api/rooms/')) return state.room;
      return { rooms: [] };
    },
    post: async (path, body) => {
      calls.push(['POST', path, body]);
      if (path === '/api/rooms/check-dir') return { allowed: true, exists: true, warnings: [] };
      if (path === '/api/rooms') { state.room = Object.assign({}, body, { id: 'new-room' }); return state.room; }
      return { ok: true };
    },
    patch: async (path, body) => {
      calls.push(['PATCH', path, body]);
      state.room.title_short = state.room.title;
      state.room = Object.assign({}, state.room, body);
      return state.room;
    },
    put: async (path, body) => { calls.push(['PUT', path, body]); return {}; },
    del: async (path) => { calls.push(['DEL', path]); return {}; },
  };
  return {
    calls, toasts, navigated, state,
    api,
    toast: (text, kind) => toasts.push([String(text), kind]),
    navigate: (hash) => navigated.push(String(hash)),
    store: {},
    refresh: () => {},
  };
}

/* ───────────────────────────────────────────────────── B-30: нет «Доски» */

test('B-30: поля «Доска» в окне нет, но доска по-прежнему уходит при создании', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const state = { room: null };
  const ctx = ctxFor(state);
  const done = openRoomDialog(ctx, null);
  await sleep(40);

  assert.ok(!doc.body.textContent.includes('Доска'), 'ни слова «Доска» в окне');
  assert.equal(walk(doc.body).filter((el) => el.getAttribute('aria-label') === 'Доска задач').length, 0,
    'поля выбора доски нет');
  assert.equal(walk(doc.body).filter((el) => /доск/i.test(String(el.getAttribute('aria-label') || ''))).length, 0,
    'и никакого скрытого поля доски');

  const title = byAttr(doc.body, 'aria-label', 'Название комнаты');
  title.value = 'Новая комната';
  const form = walk(doc.body).find((el) => el.tagName.toLowerCase() === 'form');
  form.dispatch('submit', { type: 'submit', preventDefault() {} });
  await sleep(80);

  const post = ctx.calls.find((c) => c[0] === 'POST' && c[1] === '/api/rooms');
  assert.ok(post, 'комната создаётся');
  assert.equal(post[2].board, 'team', 'сервер по-прежнему получает одну общую доску');
  await done;
  assert.deepEqual(ctx.navigated.slice(-1), ['#/room/new-room/composer']);
});

/* ────────────────────────────────────────── B-32: вкладки вместо простыни */

test('B-32: пять разделов, на экране ровно один, переключение работает', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = ctxFor({ room: room() });
  const done = openRoomDialog(ctx, room());
  await sleep(40);

  const tabs = walk(doc.body).filter((el) => el.getAttribute('role') === 'tab');
  assert.deepEqual(tabs.map((t) => t.textContent.trim()),
    ['Основное', 'Состав', 'Работа', 'Проект', 'Файлы'], 'разделы названы по-русски');

  const panels = walk(doc.body).filter((el) => el.getAttribute('role') === 'tabpanel');
  assert.equal(panels.length, 5);
  const visible = () => panels.filter((p) => !p.hasAttribute('hidden'));
  assert.equal(visible().length, 1, 'одновременно виден один раздел');
  assert.equal(visible()[0].dataset.panel, 'main', 'открывается «Основное»');
  assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].getAttribute('aria-controls'), 'roomdlg-panel-main');

  tabs[2].dispatch('click');
  assert.equal(visible()[0].dataset.panel, 'work', 'по клику открывается «Работа»');
  assert.equal(tabs[2].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].getAttribute('aria-selected'), 'false');

  // «Работа» — это критик и раунды (B-32), а не общая простыня.
  const work = visible()[0];
  assert.ok(walk(work).some((el) => el.id === 'roomdlg-critic-rounds'));
  assert.ok(work.textContent.includes('Раунды проверки'));

  tabs[4].dispatch('click');
  assert.equal(visible()[0].dataset.panel, 'files');
  assert.ok(byAttr(visible()[0], 'aria-label', 'Папка проекта'), 'папка и файлы — в «Файлах»');

  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done;
});

test('B-32: модели и «Особенности» — строка и ссылка, без второго редактора', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = ctxFor({ room: room() });
  const done = openRoomDialog(ctx, room(), { focus: 'members' });
  await sleep(40);

  // Никакого редактора модели внутри окна настроек.
  assert.equal(byClass(doc.body, 'agent-model__edit').length, 0, 'редактора модели здесь нет');
  assert.equal(walk(doc.body).filter((el) => String(el.className).includes('agent-model')).length, 0);

  // Краткая строка про модель — человеческими словами.
  const modelLine = walk(doc.body).find((el) => String(el.className).includes('roomdlg__person-model'));
  assert.ok(modelLine, 'строка модели есть');
  assert.equal(modelLine.textContent.trim(), 'Дилижанс про · мышление: высокое мышление',
    'подпись из каталога, без технических id');

  // Ссылка в карточку команды — там полный интерфейс.
  const cfg = walk(doc.body).find((el) => el.tagName.toLowerCase() === 'a'
    && el.textContent.trim() === 'Настроить в команде');
  assert.ok(cfg, 'ссылка «Настроить в команде»');
  assert.equal(cfg.getAttribute('href'), '#/room/cafe/team');

  // «Особенности» — только кнопка-ссылка в блокнот, редактора в окне нет.
  assert.ok(byClass(doc.body, 'notes-link').length > 0, 'ссылки в блокнот есть');
  assert.equal(walk(doc.body).filter((el) => String(el.className).includes('notes-ed__')).length, 0,
    'редактор особенностей здесь не рисуется');

  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done;
});

test('B-32/B-9: opts.focus открывает нужный раздел и ставит курсор', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = ctxFor({ room: room() });

  const done = openRoomDialog(ctx, room(), { focus: 'project_mode' });
  await sleep(40);
  const tabs = walk(doc.body).filter((el) => el.getAttribute('role') === 'tab');
  assert.equal(tabs[3].getAttribute('aria-selected'), 'true', 'открылся раздел «Проект»');
  assert.equal(doc.activeElement && doc.activeElement.id, 'roomdlg-project-mode', 'курсор на переключателе');
  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done;

  const doc2 = globalThis.document;
  const done2 = openRoomDialog(ctx, room(), { focus: 'project_dir' });
  await sleep(40);
  const tabs2 = walk(doc2.body).filter((el) => el.getAttribute('role') === 'tab');
  assert.equal(tabs2[4].getAttribute('aria-selected'), 'true', 'открылся раздел «Файлы»');
  assert.equal(doc2.activeElement && doc2.activeElement.getAttribute('aria-label'), 'Папка проекта');
  byText(doc2.body, 'Отмена', 'button').dispatch('click');
  await done2;
});

/* ───────────────────────── B-31: окно не закрывается само и не теряет ввод */

test('B-31: перерисовка страницы и смена маршрута окно не закрывают', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const source = readFileSync(new URL('../web/js/views/room-dialog.js', import.meta.url), 'utf8');
  assert.ok(!/setInterval|setTimeout\([^)]*close\(/.test(source),
    'окно не ставит своих таймеров и не закрывается по времени');

  const view = new FakeElement('div');
  doc.body.appendChild(view);
  const ctx = ctxFor({ room: room() });
  const done = openRoomDialog(ctx, room());
  await sleep(40);

  const overlay = byClass(doc.body, 'dialog-backdrop')[0];
  assert.ok(overlay, 'окно в документе');
  assert.equal(overlay.parentNode, doc.body, 'окно живёт в document.body, а не внутри страницы');

  // Перерисовка страницы по опросу: #view пересобрали целиком.
  view.replaceChildren(new FakeElement('div'));
  assert.ok(byClass(doc.body, 'dialog-backdrop').length === 1, 'перерисовка страницы окно не трогает');
  assert.ok(byAttr(doc.body, 'aria-label', 'Название комнаты'), 'поля на месте');

  // Перерисовка по ctx.refresh() (роутер пересобирает #view) и смена маршрута —
  // окно остаётся: оно не ребёнок страницы.
  ctx.refresh = () => { view.replaceChildren(new FakeElement('div')); };
  ctx.refresh();
  assert.equal(byClass(doc.body, 'dialog-backdrop').length, 1, 'смена маршрута окно не закрывает');
  assert.ok(byAttr(doc.body, 'aria-label', 'Название комнаты'), 'поля по-прежнему на месте');

  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done;
});

test('B-31: Esc и клик мимо окна с несохранёнными правками сначала спрашивают', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = ctxFor({ room: room() });
  const done = openRoomDialog(ctx, room());
  await sleep(40);

  const title = byAttr(doc.body, 'aria-label', 'Название комнаты');
  title.value = 'Лето — новый сезон';
  bubble(title, 'input');

  const bar = () => byAttr(doc.body, 'data-test', 'unsaved');
  assert.ok(bar().className.includes('hidden'), 'пока не просили закрыть — плашки нет');

  fireKey(doc, 'Escape', title);
  await sleep(10);
  assert.ok(!bar().className.includes('hidden'), 'Esc показал выбор, а не закрыл окно');
  assert.ok(byClass(doc.body, 'dialog-backdrop').length === 1, 'окно на месте');
  assert.equal(title.value, 'Лето — новый сезон', 'введённое не потеряно');
  assert.equal(ctx.calls.filter((c) => c[0] === 'PATCH').length, 0, 'ничего не сохранили молча');

  // «Вернуться» — продолжаем править.
  byText(doc.body, 'Вернуться', 'button').dispatch('click');
  assert.ok(bar().className.includes('hidden'), 'плашка убрана');
  assert.equal(title.value, 'Лето — новый сезон');

  // Клик по затемнению мимо окна — тоже не выбрасывает.
  const overlay = byClass(doc.body, 'dialog-backdrop')[0];
  overlay.dispatch('mousedown', { type: 'mousedown', target: overlay });
  await sleep(10);
  assert.ok(!bar().className.includes('hidden'), 'клик мимо окна спрашивает');
  assert.ok(byClass(doc.body, 'dialog-backdrop').length === 1);

  // «Сохранить изменения» — сохраняем и закрываем, ничего не потеряв.
  byText(doc.body, 'Сохранить изменения', 'button').dispatch('click');
  await sleep(120);
  const patch = ctx.calls.find((c) => c[0] === 'PATCH');
  assert.ok(patch, 'сохранение ушло');
  assert.equal(patch[2].title, 'Лето — новый сезон');
  await done;
  assert.equal(byClass(doc.body, 'dialog-backdrop').length, 0, 'после сохранения окно закрыто');
});

test('B-31: Esc в выпадающем списке и при вложенном окне настройки не закрывает', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = ctxFor({ room: room() });
  const done = openRoomDialog(ctx, room());
  await sleep(40);

  const rounds = byAttr(doc.body, 'id', 'roomdlg-critic-rounds');
  // Esc, которым закрывают выпадающий список, не должен уносить всё окно.
  fireKey(doc, 'Escape', rounds);
  await sleep(10);
  assert.equal(byClass(doc.body, 'dialog-backdrop').length, 1, 'окно осталось');
  assert.ok(byAttr(doc.body, 'data-test', 'unsaved').className.includes('hidden'),
    'и ничего не спрашивает — правок не было');

  // Вложенное окно (блокнот особенностей) сверху: Esc принадлежит ему.
  const overlay = byClass(doc.body, 'dialog-backdrop')[0];
  const nested = new FakeElement('div');
  nested.className = 'dialog-backdrop notes-ed__backdrop';
  doc.body.appendChild(nested);
  doc.querySelectorAll = (sel) => (String(sel).includes('dialog-backdrop')
    ? [overlay, nested] : []);
  fireKey(doc, 'Escape', doc.body);
  await sleep(10);
  assert.equal(byClass(doc.body, 'dialog-backdrop').filter((n) => n !== nested).length, 1,
    'настройки под блокнотом не закрылись');

  nested.parentNode.removeChild(nested);
  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done;
});

test('B-31: введённое восстанавливается после перезагрузки страницы', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = ctxFor({ room: room() });
  const done = openRoomDialog(ctx, room());
  await sleep(40);

  const title = byAttr(doc.body, 'aria-label', 'Название комнаты');
  title.value = 'Лето — версия из черновика';
  bubble(title, 'input');
  const path = byAttr(doc.body, 'aria-label', 'Папка проекта');
  path.value = 'C:\\Проекты\\Лето — копия';
  bubble(path, 'input');

  // Перезагрузка webview: окно исчезло вместе со страницей, сохранить не успели.
  const overlay = byClass(doc.body, 'dialog-backdrop')[0];
  overlay.parentNode.removeChild(overlay);
  doc.body.replaceChildren();

  // Страница поднялась заново — открываем настройки той же комнаты.
  const again = ctxFor({ room: room() });
  const done2 = openRoomDialog(again, room());
  await sleep(40);

  assert.equal(byAttr(doc.body, 'aria-label', 'Название комнаты').value,
    'Лето — версия из черновика', 'название восстановлено');
  assert.equal(byAttr(doc.body, 'aria-label', 'Папка проекта').value,
    'C:\\Проекты\\Лето — копия', 'папка восстановлена');
  const restored = byAttr(doc.body, 'data-test', 'restored');
  assert.ok(restored && !restored.className.includes('hidden'), 'видно объяснение, что это черновик');

  // «Отменить восстановление» возвращает сохранённое.
  byText(doc.body, 'Отменить восстановление', 'button').dispatch('click');
  assert.equal(byAttr(doc.body, 'aria-label', 'Название комнаты').value, 'Лето');
  assert.ok(byText(doc.body, 'Отмена', 'button'), 'окно открыто дальше');
  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done2;
  void done;   // первый экземпляр «погиб при перезагрузке» — его обещание не закрывается
});

test('B-31: явное «Закрыть без сохранения» черновик не оставляет', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = ctxFor({ room: room() });
  const done = openRoomDialog(ctx, room());
  await sleep(40);

  const title = byAttr(doc.body, 'aria-label', 'Название комнаты');
  title.value = 'Черновик, который выбросили';
  bubble(title, 'input');

  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await sleep(10);
  byText(doc.body, 'Закрыть без сохранения', 'button').dispatch('click');
  await done;

  const again = ctxFor({ room: room() });
  const done2 = openRoomDialog(again, room());
  await sleep(40);
  assert.equal(byAttr(doc.body, 'aria-label', 'Название комнаты').value, 'Лето',
    'осознанный отказ ничего не восстанавливает');
  assert.ok(byAttr(doc.body, 'data-test', 'restored').className.includes('hidden'));
  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done2;
});

/* ──────────────────────────────── B-33: проектный режим сохраняется и заметен */

test('B-33: «Сейчас: …» заметно, меняется сразу и сохраняется', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const state = { room: room({ project_mode: false }) };
  const ctx = ctxFor(state);
  const done = openRoomDialog(ctx, room({ project_mode: false }));
  await sleep(40);

  const stateBox = () => walk(doc.body).find((el) => String(el.className).includes('roomdlg__state'));
  assert.equal(stateBox().dataset.state, 'off');
  assert.ok(stateBox().textContent.includes('Сейчас: выключен'), stateBox().textContent);
  assert.ok(walk(stateBox()).some((el) => el.tagName.toLowerCase() === 'b'),
    '«Сейчас: …» выделено, а не мелким серым текстом');

  // Включаем — состояние на экране меняется сразу, без сохранения.
  const box = byAttr(doc.body, 'id', 'roomdlg-project-mode');
  box.checked = true;
  box.dispatch('change', { type: 'change' });
  assert.equal(stateBox().dataset.state, 'on');
  assert.ok(stateBox().textContent.includes('Сейчас: включён'), stateBox().textContent);

  // Второго, «точкой», обозначения нет: состояние читается только из плашки
  // (один смысл — одно место), иначе её путают с пометкой «не сохранено».
  const tabs = walk(doc.body).filter((el) => el.getAttribute('role') === 'tab');
  assert.ok(!tabs[3].className.includes('roomdlg__tab--on'), 'на вкладке нет второй отметки');

  // Сохраняем — в запрос уходит project_mode: true.
  const form = walk(doc.body).find((el) => el.tagName.toLowerCase() === 'form');
  form.dispatch('submit', { type: 'submit', preventDefault() {} });
  await sleep(140);
  const patch = ctx.calls.find((c) => c[0] === 'PATCH');
  assert.ok(patch, 'PATCH ушёл');
  assert.equal(patch[2].project_mode, true, 'включение проектного режима сохраняется');
  assert.equal(patch[2].launch_mode, 'plan_first', 'режим запуска не сброшен');
  assert.equal(state.room.project_mode, true, 'сервер получил новое состояние');
  assert.ok(ctx.toasts.some(([text]) => text.includes('Проектный режим включён')),
    'человеку сказали, что изменилось: ' + JSON.stringify(ctx.toasts));
  await done;

  // Открываем снова — состояние читается из сохранённого, а не из памяти окна.
  const again = ctxFor(state);
  const done2 = openRoomDialog(again, state.room);
  await sleep(40);
  const box2 = walk(doc.body).find((el) => String(el.className).includes('roomdlg__state'));
  assert.equal(box2.dataset.state, 'on');
  assert.ok(box2.textContent.includes('Сейчас: включён'));
  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done2;
});

/* ─────────────────────────────────────────────── B-33: выключение тоже видно */

test('B-33: выключение проектного режима сохраняется и объясняется', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const state = { room: room({ project_mode: true }) };
  const ctx = ctxFor(state);
  const done = openRoomDialog(ctx, room({ project_mode: true }), { focus: 'project' });
  await sleep(40);

  const box = byAttr(doc.body, 'id', 'roomdlg-project-mode');
  assert.equal(box.checked, true);
  box.checked = false;
  box.dispatch('change', { type: 'change' });
  assert.ok(walk(doc.body).some((el) => String(el.className).includes('roomdlg__state')
    && el.textContent.includes('Сейчас: выключен')));

  const form = walk(doc.body).find((el) => el.tagName.toLowerCase() === 'form');
  form.dispatch('submit', { type: 'submit', preventDefault() {} });
  await sleep(140);
  const patch = ctx.calls.find((c) => c[0] === 'PATCH');
  assert.equal(patch[2].project_mode, false);
  assert.ok(ctx.toasts.some(([text]) => text.includes('Проектный режим выключен')));
  await done;
});
