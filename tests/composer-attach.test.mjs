/**
 * composer-attach.test.mjs — W28-A (дефект 4): вложения в запросе.
 *
 * Что проверяем (и почему это красный тест на 1.4.0):
 *  • скрепка открывает окно выбора файлов через серверную ручку POST
 *    /api/files/pick и берёт НЕСКОЛЬКО файлов сразу (раньше был только ввод пути);
 *  • каждый чип показывает имя, размер и три действия — «Открыть», «Открыть в
 *    папке», «Убрать» (раньше был только путь с крестиком);
 *  • отмена окна — не ошибка, а сообщение; недоступное окно раскрывает поле пути
 *    как запасной путь (без тупика);
 *  • поле пути принимает несколько путей — по одному на строку;
 *  • `.env` не блокируется навсегда: «Всё равно прикрепить» → запрос уходит с
 *    `confirm_secret:true`;
 *  • непригодный файл (нет такого) — причина рядом с файлом и отправка не уходит
 *    молча;
 *  • предел 30 (было 10).
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
  // `head` НЕ создаём: без него ensureCss() сразу отдаёт готовое обещание.
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

function walk(node, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    out.push(child);
    walk(child, out);
  }
  return out;
}

function byClass(root, cls) {
  return walk(root).filter((el) => String(el.className).split(/\s+/).includes(cls));
}

function byId(root, id) {
  return walk(root).find((el) => el.getAttribute('id') === id) || null;
}

function buttonWith(root, text) {
  return walk(root).find((el) => String(el.tagName).toLowerCase() === 'button'
    && el.textContent.trim() === text) || null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ─────────────────────────────────────────────────────────── данные комнаты */

const ROOM = {
  id: 'kafe-leto',
  title: 'Кафе «Лето»',
  board: 'demo',
  orchestrator: 'anna',
  critic: '',
  critic_pass: false,
  members: [{ profile: 'anna', role: 'worker', name: 'Анна', job: 'закупки' }],
  project_mode: false,
};

const XLSX = { path: 'C:/Загрузки/Смета Альфа.xlsx', name: 'Смета Альфа.xlsx', size: 24576, ext: '.xlsx' };
const ZIP = { path: 'C:/Загрузки/архив.zip', name: 'архив.zip', size: 2048, ext: '.zip' };
const ENV = { path: 'C:/Проекты/Альфа/.env', name: '.env', size: 120, ext: '' };
const GHOST = { path: 'C:/Проекты/Альфа/нет-такого.pdf', name: 'нет-такого.pdf', size: null, ext: '.pdf' };

/**
 * Фейковый сервер вложений: pick отдаёт заданный список, attach-check отвечает
 * по правилам (секрет → needs_confirm, «нет-такого» → not_found).
 */
function attachServer(opts = {}) {
  const calls = [];
  const toasts = [];
  const pick = opts.pick === undefined ? { ok: true, paths: [XLSX.path, ZIP.path], cancelled: false } : opts.pick;
  const pickError = opts.pickError || null;
  const api = {
    get: async (path) => {
      if (path === '/api/rooms/kafe-leto') return Object.assign({}, ROOM);
      if (path.startsWith('/api/rooms/kafe-leto/backlog')) return { items: [] };
      if (path.startsWith('/api/rooms/kafe-leto/requests')) return { items: [] };
      if (path === '/api/profiles') return { items: [] };
      return {};
    },
    post: async (path, body) => {
      calls.push([path, body]);
      if (path === '/api/files/pick') {
        if (pickError) throw pickError;
        return pick;
      }
      if (path === '/api/files/attach-check') {
        const confirm = body && body.confirm_secret === true;
        const known = { [XLSX.path]: XLSX, [ZIP.path]: ZIP, [ENV.path]: ENV, [GHOST.path]: GHOST };
        const items = (body.attach || []).map((raw) => {
          const info = known[raw] || { path: raw, name: raw.split('/').pop(), size: 10, ext: '' };
          if (info === GHOST) {
            return { path: raw, ok: false, code: 'not_found', secret: false,
              message: 'Недоступно: файла нет по пути «' + raw + '». Проверьте путь или выберите файл заново.',
              name: info.name, size: null, ext: '.pdf' };
          }
          if (info === ENV && !confirm) {
            return { path: raw, ok: false, code: 'needs_confirm', secret: true,
              message: '«.env» похож на секрет. Если он правда нужен команде — нажмите «Всё равно прикрепить».',
              name: info.name, size: info.size, ext: '' };
          }
          return { path: raw, ok: true, code: 'ok', message: '', secret: info === ENV,
            name: info.name, size: info.size, ext: info.ext };
        });
        return { ok: true, items, needs_confirm: [], accepted: [], max: 30 };
      }
      if (path === '/api/files/attach-open') return { ok: true, path: body.path, mode: body.mode };
      if (path.endsWith('/requests')) return { root_id: 'req_new0001' };
      return {};
    },
    patch: async () => ({}),
    del: async () => ({}),
  };
  const el = new FakeElement('div');
  globalThis.document.body.appendChild(el);
  const ctx = {
    api, room: ROOM, params: { roomId: ROOM.id }, route: { roomId: ROOM.id },
    toast: (message, kind) => toasts.push({ message: String(message), kind }),
    navigate: () => {}, store: {},
  };
  return { el, ctx, calls, toasts };
}

async function mountComposer(opts = {}) {
  installDom();
  const { mount } = await import('../web/js/views/composer.js');
  const world = attachServer(opts);
  const view = await mount(world.el, world.ctx);
  await sleep(20);
  return { ...world, view };
}

function attachBtnOf(el) {
  return byClass(el, 'cmp-attach-btn')[0];
}

function chipsOf(el) {
  return byClass(el, 'cmp-attach__item');
}

function textareaOf(el) {
  return byId(el, 'cmp-input');
}

function pathFieldOf(el) {
  return byClass(el, 'cmp-attach__path')[0];
}

async function submitForm(el) {
  const form = walk(el).find((node) => String(node.tagName).toLowerCase() === 'form');
  form.dispatch('submit', { preventDefault() {}, type: 'submit' });
  await sleep(30);
}

/* ───────────────────────────────────── скрепка → окно выбора → чипы */

test('скрепка: одно нажатие — окно выбора файлов, несколько файлов сразу', async () => {
  const { el, view, calls, toasts } = await mountComposer();

  const btn = attachBtnOf(el);
  assert.ok(btn, 'кнопка вложений есть');
  assert.match(btn.textContent, /Прикрепить файлы/, 'у скрепки есть подпись, а не только иконка');
  assert.equal(btn.getAttribute('aria-label'), 'Прикрепить файлы');

  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(30);

  const pickCall = calls.find((c) => c[0] === '/api/files/pick');
  assert.ok(pickCall, 'скрепка зовёт серверное окно выбора файлов');
  assert.equal(typeof pickCall[1].title, 'string');

  const chips = chipsOf(el);
  assert.equal(chips.length, 2, 'взяты оба файла из окна');
  assert.match(chips[0].textContent, /Смета Альфа\.xlsx/);
  assert.match(chips[0].textContent, /24 КБ/, 'размер виден в чипе');
  assert.match(chips[1].textContent, /архив\.zip/, '.zip больше не запрещён');
  assert.equal(toasts.filter((t) => t.kind === 'warn').length, 0, 'ошибок нет');

  view.unmount();
});

test('чип: имя, размер, «Открыть», «Открыть в папке», «Убрать»', async () => {
  const { el, view, calls } = await mountComposer();
  const btn = attachBtnOf(el);
  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(30);

  const chip = chipsOf(el)[0];
  assert.match(chip.className, /cmp-attach__item/);
  assert.ok(byClass(chip, 'cmp-attach__name')[0], 'имя файла');
  assert.ok(byClass(chip, 'cmp-attach__size')[0], 'размер файла');
  const open = buttonWith(chip, 'Открыть');
  const reveal = buttonWith(chip, 'Открыть в папке');
  const drop = buttonWith(chip, 'Убрать');
  assert.ok(open && reveal && drop, 'все три действия на месте');

  open.dispatch('click', { type: 'click', target: open });
  await sleep(20);
  const openCall = calls.find((c) => c[0] === '/api/files/attach-open');
  assert.ok(openCall, '«Открыть» идёт через сервер');
  assert.equal(openCall[1].mode, 'open');
  assert.equal(openCall[1].path, XLSX.path);

  reveal.dispatch('click', { type: 'click', target: reveal });
  await sleep(20);
  const last = calls.filter((c) => c[0] === '/api/files/attach-open').pop();
  assert.equal(last[1].mode, 'reveal');

  drop.dispatch('click', { type: 'click', target: drop });
  await sleep(10);
  assert.equal(chipsOf(el).length, 1, '«Убрать» убирает только свой чип');

  view.unmount();
});

test('окно закрыли без выбора — это не ошибка и не чип', async () => {
  const { el, view, toasts } = await mountComposer({ pick: { ok: true, paths: [], cancelled: true } });
  const btn = attachBtnOf(el);
  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(30);

  assert.equal(chipsOf(el).length, 0, 'чипов нет');
  assert.equal(toasts.filter((t) => t.kind === 'danger').length, 0, 'красной ошибки нет');
  assert.ok(toasts.some((t) => /отменён/i.test(t.message)), 'человеку сказали, что выбор отменён');
  view.unmount();
});

test('окно недоступно (не-Windows): причина и поле пути как запасной путь', async () => {
  const { ApiError } = await import('../web/js/api.js');
  const err = new ApiError('pick_unavailable',
    'Окно выбора файлов есть только в Windows. Вставьте путь в поле «Путь к файлу».', 501);
  const { el, view, toasts } = await mountComposer({ pickError: err });

  const btn = attachBtnOf(el);
  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(30);

  const field = pathFieldOf(el);
  assert.ok(field, 'поле пути существует');
  const row = field.parentNode.parentNode;
  assert.ok(!String(row.className).split(/\s+/).includes('hidden'),
    'поле пути раскрыто — человеку есть куда вставить путь');
  assert.ok(toasts.some((t) => /Windows/.test(t.message)), 'причина названа');
  assert.equal(btn.disabled, false, 'кнопка снова активна');
  view.unmount();
});

/* ───────────────────────────────────────── запасной путь в поле */

test('поле пути: несколько путей — по одному на строку', async () => {
  const { el, view, calls } = await mountComposer({ pick: { ok: true, paths: [], cancelled: true } });
  const btn = attachBtnOf(el);
  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(20);

  const field = pathFieldOf(el);
  field.value = XLSX.path + '\n' + ZIP.path + '\n\n';
  const add = buttonWith(el, 'Прикрепить путь');
  add.dispatch('click', { type: 'click', target: add });
  await sleep(30);

  assert.equal(chipsOf(el).length, 2, 'оба пути стали чипами');
  const check = calls.find((c) => c[0] === '/api/files/attach-check');
  assert.deepEqual(check[1].attach, [XLSX.path, ZIP.path]);
  assert.equal(field.value, '', 'поле очищено после добавления');
  view.unmount();
});

/* ───────────────────────────────────────── секреты и «Всё равно прикрепить» */

test('.env: сначала вопрос, потом «Всё равно прикрепить» → confirm_secret:true', async () => {
  const { el, view, calls, toasts } = await mountComposer({ pick: { ok: true, paths: [ENV.path], cancelled: false } });
  const btn = attachBtnOf(el);
  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(30);

  const chip = chipsOf(el)[0];
  assert.match(chip.className, /cmp-attach__item--bad/, 'секрет помечен');
  assert.match(chip.textContent, /похож на секрет/, 'причина видна на чипе');
  const confirm = buttonWith(chip, 'Всё равно прикрепить');
  assert.ok(confirm, 'кнопка подтверждения есть — тупика нет');

  // До подтверждения отправка не уходит молча.
  textareaOf(el).value = 'Свести заявки';
  textareaOf(el).dispatch('input', { type: 'input', target: textareaOf(el) });
  await submitForm(el);
  assert.equal(calls.filter((c) => String(c[0]).endsWith('/requests')).length, 0,
    'непринятый секрет не отправляется');
  assert.ok(toasts.some((t) => /Всё равно прикрепить/.test(t.message)), 'подсказка, что нажать');

  confirm.dispatch('click', { type: 'click', target: confirm });
  await sleep(20);
  assert.ok(!/cmp-attach__item--bad/.test(String(chipsOf(el)[0].className)), 'чип стал обычным');

  textareaOf(el).value = 'Свести заявки';
  textareaOf(el).dispatch('input', { type: 'input', target: textareaOf(el) });
  await submitForm(el);
  await sleep(20);
  const sent = calls.find((c) => String(c[0]).endsWith('/requests'));
  assert.ok(sent, 'запрос ушёл');
  assert.deepEqual(sent[1].attach, [ENV.path]);
  assert.equal(sent[1].confirm_secret, true, 'подтверждение уходит на сервер');
  view.unmount();
});

/* ───────────────────────────────────────── непригодный файл */

test('несуществующий файл: причина рядом с файлом, отправка не уходит', async () => {
  const { el, view, calls, toasts } = await mountComposer({ pick: { ok: true, paths: [GHOST.path], cancelled: false } });
  const btn = attachBtnOf(el);
  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(30);

  const chip = chipsOf(el)[0];
  assert.match(chip.className, /cmp-attach__item--bad/);
  assert.match(chip.textContent, /файла нет/, 'причина в чипе');
  assert.ok(byClass(chip, 'cmp-attach__why')[0], 'причина отдельным полем');
  assert.ok(toasts.some((t) => /файла нет/.test(t.message)), 'и тостом');

  textareaOf(el).value = 'Проверь смету';
  textareaOf(el).dispatch('input', { type: 'input', target: textareaOf(el) });
  await submitForm(el);
  assert.equal(calls.filter((c) => String(c[0]).endsWith('/requests')).length, 0,
    'запрос с непригодным файлом не уходит');
  view.unmount();
});

/* ───────────────────────────────────────── предел 30 */

test('предел вложений — 30, лишние не теряются молча', async () => {
  const many = [];
  for (let i = 0; i < 31; i += 1) many.push('C:/Загрузки/файл ' + i + '.txt');
  const { el, view, toasts } = await mountComposer({
    pick: { ok: true, paths: many, cancelled: false, dropped: 1, total: 31, max: 30 },
  });
  const btn = attachBtnOf(el);
  btn.dispatch('click', { type: 'click', target: btn });
  await sleep(30);

  assert.equal(chipsOf(el).length, 30, 'ровно 30 вложений');
  assert.ok(toasts.some((t) => /первые 30/.test(t.message)), 'о лишних сказали');
  view.unmount();
});
