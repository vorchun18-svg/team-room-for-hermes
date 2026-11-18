/**
 * composer-critic-select.test.mjs — W20-U4 (P10): композер нового запроса.
 *
 * Что проверяем (и почему это красный тест на старом коде):
 *  • «С критиком» и «раунды» — ОДИН селектор. На 1.0.4 в панели стояли
 *    переключатель-кнопка `role="switch"` и отдельный `<select>` раундов, который
 *    появлялся только при включённом переключателе: два управления об одном;
 *  • значения остаются совместимыми с API: «Без критика» → `critic:false`,
 *    «1/2 раунда» → `critic_mode:'rounds'` + `max_rounds`, «До полного одобрения»
 *    → `critic_mode:'until_approved'`;
 *  • «Отправить» неактивна на пустом поле (и снова активна, когда текст появился);
 *  • у аватаров команды подсказка «имя — роль», а не системный id профиля.
 *
 * DOM — мини из fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';

/* ─────────────────────────────────────────────── мини-DOM для mount() */

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  // `head` НЕ создаём: без него ensureCss()/useStyles() сразу отдают готовое
  // обещание, а не ждут события load у ссылки (в мини-DOM его никто не шлёт).
  doc.documentElement = new FakeElement('html');
  doc.activeElement = null;
  doc.hidden = false;
  doc.listeners = Object.create(null);
  doc.addEventListener = (type, fn) => {
    (doc.listeners[type] || (doc.listeners[type] = [])).push(fn);
  };
  doc.removeEventListener = (type, fn) => {
    const list = doc.listeners[type] || [];
    const idx = list.indexOf(fn);
    if (idx !== -1) list.splice(idx, 1);
  };
  doc.querySelector = () => null;
  doc.querySelectorAll = () => [];
  globalThis.document = doc;

  globalThis.window = {
    innerHeight: 900,
    innerWidth: 1440,
    listeners: Object.create(null),
    addEventListener(type, fn) {
      (this.listeners[type] || (this.listeners[type] = [])).push(fn);
    },
    removeEventListener(type, fn) {
      const list = this.listeners[type] || [];
      const idx = list.indexOf(fn);
      if (idx !== -1) list.splice(idx, 1);
    },
  };

  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => store.clear(),
  };
  globalThis.sessionStorage = globalThis.localStorage;
  try { globalThis.navigator = { mediaDevices: null, userAgent: 'node-test' }; } catch { /* только чтение */ }
  return doc;
}

FakeElement.prototype.focus = function focus() { globalThis.document.activeElement = this; };
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};
FakeElement.prototype.setSelectionRange = function setSelectionRange() {};
FakeElement.prototype.closest = function closest() { return null; };
FakeElement.prototype.remove = function remove() {
  if (this.parentNode && Array.isArray(this.parentNode.childNodes)) {
    const idx = this.parentNode.childNodes.indexOf(this);
    if (idx !== -1) this.parentNode.childNodes.splice(idx, 1);
  }
  this.parentNode = null;
};
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
// Поле ввода и select в мини-DOM: `value` по умолчанию '', как в браузере.
Object.defineProperty(FakeElement.prototype, 'value', {
  get() { return this._value === undefined ? '' : this._value; },
  set(v) { this._value = v === null || v === undefined ? '' : String(v); },
  configurable: true,
});
Object.defineProperty(FakeElement.prototype, 'disabled', {
  get() { return this._disabled === true; },
  set(v) { this._disabled = v === true; },
  configurable: true,
});
Object.defineProperty(FakeElement.prototype, 'checked', {
  get() { return this._checked === true; },
  set(v) { this._checked = v === true; },
  configurable: true,
});
// Простой querySelector для вьюх: '#id', '.class', 'tag' — первого совпадения хватает.
FakeElement.prototype.querySelector = function querySelector(sel) {
  const s = String(sel || '').trim();
  const match = (el) => {
    if (s.startsWith('#')) return el.getAttribute('id') === s.slice(1);
    if (s.startsWith('.')) return String(el.className).split(/\s+/).includes(s.slice(1));
    return String(el.tagName).toLowerCase() === s.toLowerCase();
  };
  for (const el of walk(this)) if (match(el)) return el;
  return null;
};
FakeElement.prototype.querySelectorAll = function querySelectorAll(sel) {
  const s = String(sel || '').trim();
  return walk(this).filter((el) => {
    if (s.startsWith('#')) return el.getAttribute('id') === s.slice(1);
    if (s.startsWith('.')) return String(el.className).split(/\s+/).includes(s.slice(1));
    return String(el.tagName).toLowerCase() === s.toLowerCase();
  });
};

function walk(node, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    out.push(child);
    walk(child, out);
  }
  return out;
}

function byId(root, id) {
  return walk(root).find((el) => el.getAttribute('id') === id) || null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ─────────────────────────────────────────────────────────── данные комнаты */

const ROOM = {
  id: 'kafe-leto',
  title: 'Кафе «Лето»',
  icon: 'cake',
  board: 'demo',
  orchestrator: 'anna',
  critic: 'lev',
  critic_pass: true,
  critic_mode: 'rounds',
  max_rounds: 2,
  members: [
    { profile: 'anna', role: 'worker', name: 'Анна', job: 'закупки' },
    { profile: 'marina', role: 'worker', name: 'Марина', job: 'бухгалтерия' },
    { profile: 'lev', role: 'critic', name: 'Лев', job: 'проверка' },
  ],
  project_mode: false,
};

function composerCtx() {
  const calls = [];
  const api = {
    get: async (path) => {
      if (path === '/api/rooms/kafe-leto') return Object.assign({}, ROOM);
      if (path.startsWith('/api/rooms/kafe-leto/backlog')) return { items: [] };
      if (path.startsWith('/api/rooms/kafe-leto/requests')) return { items: [] };
      if (path === '/api/profiles') return { items: [] };
      return {};
    },
    post: async (path, body) => {
      calls.push(['POST', path, body]);
      return { root_id: 'req_new0001' };
    },
    patch: async (path, body) => { calls.push(['PATCH', path, body]); return {}; },
    del: async () => ({}),
  };
  const el = new FakeElement('div');
  globalThis.document.body.appendChild(el);
  return {
    calls,
    el,
    ctx: {
      api, room: ROOM, params: { roomId: ROOM.id }, route: { roomId: ROOM.id },
      toast: () => {}, navigate: (hash) => calls.push(['NAV', hash]), store: {},
    },
  };
}

async function mountComposer() {
  installDom();
  const { mount } = await import('../web/js/views/composer.js');
  const { el, ctx, calls } = composerCtx();
  const view = await mount(el, ctx);
  await sleep(20);
  return { el, view, calls };
}

/** Все элементы с классом из списка (по классу, а не по id). */
function byClass(root, cls) {
  return walk(root).filter((el) => String(el.className).split(/\s+/).includes(cls));
}

/* ───────────────────────────────────────────── один селектор вместо двух */

test('композер: «без критика / 1 / 2 / до одобрения» — ОДИН селектор, переключателя нет', async () => {
  const { el, view } = await mountComposer();

  const select = byId(el, 'cmp-critic-mode');
  assert.ok(select, 'селектор проверки есть');
  assert.equal(select.tagName.toLowerCase(), 'select');

  const switches = walk(el).filter((node) => node.getAttribute('role') === 'switch');
  assert.equal(switches.length, 0, 'переключателя «С критиком» больше нет');
  assert.ok(!el.textContent.includes('С критиком'), 'подписи «С критиком» в панели нет');

  const critSelects = byClass(el, 'cmp-critic-mode');
  assert.equal(critSelects.length, 1, 'одно управление проверкой, а не два');

  const values = select.options.map((o) => o.getAttribute('value'));
  assert.deepEqual(values, ['off', '1', '2', 'until_approved']);
  const labels = select.options.map((o) => o.textContent.trim());
  assert.deepEqual(labels, ['Без критика', '1 раунд — быстро', '2 раунда', 'До полного одобрения']);
  assert.equal(select.value, '2', 'по умолчанию — режим комнаты (2 раунда из настроек)');

  // Слово одно: «раунд», а не «прогон» (словарь U1).
  assert.ok(!/прогон/i.test(el.textContent));
  assert.ok(String(select.getAttribute('title')).includes('Раунд — один цикл'), 'подсказка объясняет раунд');

  view.unmount();
});

test('композер: критик не настроен в комнате — селектора проверки нет', async () => {
  installDom();
  const { mount } = await import('../web/js/views/composer.js');
  const room = Object.assign({}, ROOM, { critic: '', critic_pass: false });
  const el = new FakeElement('div');
  document.body.appendChild(el);
  const view = await mount(el, {
    api: { get: async () => ({}), post: async () => ({}), patch: async () => ({}), del: async () => ({}) },
    room, params: { roomId: room.id }, toast: () => {}, navigate: () => {}, store: {},
  });
  await sleep(10);
  assert.equal(byId(el, 'cmp-critic-mode'), null, 'без критика нечего выбирать');
  view.unmount();
});

/* ───────────────────────────────────────────── «Отправить» на пустом поле */

test('композер: «Отправить» неактивна на пустом поле и оживает с текстом', async () => {
  const { el, view } = await mountComposer();
  const send = walk(el).find((node) => String(node.className).split(/\s+/).includes('cmp-send'));
  assert.ok(send, 'кнопка отправки есть');
  assert.equal(send.disabled, true, 'пустое поле — кнопка неактивна');
  assert.equal(send.getAttribute('aria-disabled'), 'true');
  assert.match(String(send.getAttribute('title')), /Напишите или надиктуйте/);

  const textarea = byId(el, 'cmp-input');
  textarea.value = '   ';
  textarea.dispatch('input', { type: 'input', target: textarea });
  assert.equal(send.disabled, true, 'одни пробелы — по-прежнему неактивна');

  textarea.value = 'Нужно свести заявки поставщика «Ромашка» за неделю';
  textarea.dispatch('input', { type: 'input', target: textarea });
  assert.equal(send.disabled, false, 'текст появился — кнопка активна');
  assert.equal(send.getAttribute('aria-disabled'), 'false');
  view.unmount();
});

/* ───────────────────────────────── тело запроса: совместимость с API */

async function submitWith(choice, text = 'Свести заявки за неделю') {
  const { el, view, calls } = await mountComposer();
  const select = byId(el, 'cmp-critic-mode');
  if (select) {
    select.value = choice;
    select.dispatch('change', { type: 'change', target: select });
  }
  const textarea = byId(el, 'cmp-input');
  textarea.value = text;
  textarea.dispatch('input', { type: 'input', target: textarea });
  const form = walk(el).find((node) => node.tagName.toLowerCase() === 'form');
  form.dispatch('submit', { preventDefault() {}, type: 'submit' });
  await sleep(30);
  const post = calls.find((c) => c[0] === 'POST' && String(c[1]).endsWith('/requests'));
  view.unmount();
  return post ? post[2] : null;
}

test('композер: «1 раунд» → critic:true, critic_mode:rounds, max_rounds:1', async () => {
  const body = await submitWith('1');
  assert.ok(body, 'запрос ушёл');
  assert.equal(body.critic, true);
  assert.equal(body.critic_mode, 'rounds');
  assert.equal(body.max_rounds, 1);
});

test('композер: «До полного одобрения» → critic_mode:until_approved без max_rounds', async () => {
  const body = await submitWith('until_approved');
  assert.equal(body.critic, true);
  assert.equal(body.critic_mode, 'until_approved');
  assert.ok(!('max_rounds' in body), 'у «до одобрения» нет числа раундов');
});

test('композер: «Без критика» → critic:false и никаких полей режима', async () => {
  const body = await submitWith('off');
  assert.equal(body.critic, false);
  assert.ok(!('critic_mode' in body));
  assert.ok(!('max_rounds' in body));
});

/* ───────────────────────────────────────── подсказки у аватаров команды */

test('композер: аватары команды подсказывают «имя — роль», а не id профиля', async () => {
  const { el, view } = await mountComposer();
  const team = byClass(el, 'cmp-hint__team')[0];
  assert.ok(team, 'ряд аватаров команды есть');
  const avatars = team.childNodes.filter((n) => n.nodeType === 1);
  assert.ok(avatars.length >= 2, 'в команде больше одного участника');

  for (const av of avatars) {
    const title = String(av.getAttribute('title') || '');
    assert.ok(title.length > 0, 'у аватара есть подсказка');
    assert.ok(title.includes(' — '), 'подсказка в виде «имя — роль»: ' + title);
    assert.ok(!/^(team-|room-)/.test(title), 'системного id в подсказке нет');
    assert.ok(String(av.getAttribute('aria-label') || '').length > 0, 'подсказка доступна с клавиатуры');
  }
  // Подсказка собирает всю команду, а не только видимые пять.
  assert.match(String(team.getAttribute('title')), /Команда: .+—/);
  view.unmount();
});
