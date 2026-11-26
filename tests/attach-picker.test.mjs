/**
 * attach-picker.test.mjs — W30-F (дефект 15): скрепка везде открывает проводник.
 *
 * Что проверяем (и почему это красный тест до W30-F):
 *  • один общий компонент `web/js/attach-picker.js` зовёт серверное окно выбора
 *    файлов `POST /api/files/pick` — и в композере, и в «Диалоге по запросу»
 *    (раньше диалог просил вставить путь через `promptDialog`);
 *  • прикрепляются ПУТИ, а не копии: содержимое файла никуда не загружается
 *    (никаких `/api/upload`);
 *  • чип: «Просмотр / Открыть / Открыть в папке / Убрать» через сервер;
 *  • секрет (`.env`, токен) — чип с причиной и «Всё равно прикрепить» → `confirm_secret`;
 *  • предел 30, дубли не добавляются, отмена окна — не ошибка;
 *  • ручной путь — скрытая ссылка внутри компонента (не основной способ);
 *  • страж: в `web/js` не осталось `promptDialog` с подписью «Путь к файлу»;
 *  • «Запросы без дела» на РЕАЛЬНОЙ форме ответа `GET /api/rooms/{id}/results`
 *    (fixtures/w30f_results.json): служебные статусы done/archived/failed
 *    показываются по-русски, латиницы в подписи нет.
 *
 * DOM — мини из fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, extname } from 'node:path';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/* ─────────────────────────────────────────────────────── мини-DOM */

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  doc.documentElement = new FakeElement('html');
  doc.head = null;
  doc.activeElement = null;
  doc.hidden = false;
  doc.listeners = Object.create(null);
  doc.addEventListener = (type, fn) => { (doc.listeners[type] || (doc.listeners[type] = [])).push(fn); };
  doc.removeEventListener = (type, fn) => {
    const list = doc.listeners[type] || [];
    const idx = list.indexOf(fn);
    if (idx !== -1) list.splice(idx, 1);
  };
  doc.querySelector = () => null;
  doc.querySelectorAll = () => [];
  globalThis.document = doc;
  globalThis.window = {
    innerHeight: 900, innerWidth: 1440,
    listeners: Object.create(null),
    addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); },
    removeEventListener() {},
  };
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => store.clear(),
  };
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
FakeElement.prototype.querySelector = function querySelector(sel) {
  const s = String(sel || '').trim();
  const match = (el) => {
    if (s.startsWith('.')) return String(el.className).split(/\s+/).includes(s.slice(1));
    if (s.startsWith('#')) return el.getAttribute('id') === s.slice(1);
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

const byClass = (root, cls) => walk(root).filter((el) => String(el.className).split(/\s+/).includes(cls));
const buttonWith = (root, text) => walk(root).find((el) => String(el.tagName).toLowerCase() === 'button'
  && el.textContent.trim() === text) || null;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ─────────────────────────────────────────────────────── «сервер» файлов */

const SMEТА = 'C:/Загрузки/смета-зерно.xlsx';
const DOGOVOR = 'C:/Загрузки/договор-перевозки.docx';
const TOKEN = 'C:/Проекты/Кофейня/token-demo.txt';
const GHOST = 'C:/Загрузки/нет-такого.pdf';

function fileServer(opts = {}) {
  const calls = [];
  const toasts = [];
  const pick = opts.pick === undefined
    ? { ok: true, paths: [SMEТА, DOGOVOR], cancelled: false }
    : opts.pick;
  const known = {
    [SMEТА]: { name: 'смета-зерно.xlsx', size: 24576, ext: '.xlsx' },
    [DOGOVOR]: { name: 'договор-перевозки.docx', size: 51200, ext: '.docx' },
    [TOKEN]: { name: 'token-demo.txt', size: 120, ext: '.txt' },
  };
  const api = {
    get: async () => ({ items: [] }),
    post: async (path, body) => {
      calls.push([path, body]);
      if (path === '/api/files/pick') {
        if (opts.pickError) throw opts.pickError;
        return pick;
      }
      if (path === '/api/files/attach-check') {
        const confirm = body && body.confirm_secret === true;
        const items = (body.attach || []).map((raw) => {
          if (raw === GHOST) {
            return { path: raw, ok: false, code: 'not_found', secret: false, name: 'нет-такого.pdf',
              size: null, ext: '.pdf',
              message: 'Файла нет по пути «' + raw + '». Выберите файл заново.' };
          }
          const info = known[raw] || { name: String(raw).split('/').pop(), size: 10, ext: '' };
          const secret = raw === TOKEN;
          if (secret && !confirm) {
            return { path: raw, ok: false, code: 'needs_confirm', secret: true, name: info.name,
              size: info.size, ext: info.ext,
              message: '«' + info.name + '» похож на секрет. Если он правда нужен команде — '
                + 'нажмите «Всё равно прикрепить».' };
          }
          return { path: raw, ok: true, code: 'ok', message: '', secret,
            name: info.name, size: info.size, ext: info.ext };
        });
        return { ok: true, items, needs_confirm: [], accepted: [], max: 30 };
      }
      if (path === '/api/files/attach-open') return { ok: true, path: body.path, mode: body.mode };
      return {};
    },
    patch: async () => ({}),
    del: async () => ({}),
  };
  return { api, calls, toasts };
}

async function makePicker(opts = {}) {
  installDom();
  const { createAttachPicker } = await import('../web/js/attach-picker.js');
  const world = fileServer(opts);
  const picker = createAttachPicker({
    api: world.api,
    toast: (message, kind) => world.toasts.push({ message: String(message), kind }),
    prefix: opts.prefix || 'cmp-attach',
    title: 'Прикрепить файлы',
  });
  const host = new FakeElement('div');
  host.appendChild(picker.button);
  host.appendChild(picker.chipsHost);
  host.appendChild(picker.row);
  return { ...world, picker, host };
}

/* ─────────────────────────────────────── скрепка → окно выбора → пути */

test('скрепка: серверное окно выбора файлов, несколько путей сразу', async () => {
  const { picker, host, calls, toasts } = await makePicker();
  const btn = byClass(host, 'cmp-attach__btn-label')[0];
  assert.ok(btn, 'у скрепки есть подпись «Прикрепить файлы», а не только иконка');
  assert.equal(btn.textContent, 'Прикрепить файлы');

  await picker.open();
  await sleep(10);

  const pickCall = calls.find((c) => c[0] === '/api/files/pick');
  assert.ok(pickCall, 'скрепка зовёт POST /api/files/pick — системное окно Windows');
  assert.equal(typeof pickCall[1].title, 'string');

  assert.deepEqual(picker.paths(), [SMEТА, DOGOVOR], 'прикреплены пути обоих файлов');
  const chips = byClass(host, 'cmp-attach__item');
  assert.equal(chips.length, 2);
  assert.match(chips[0].textContent, /смета-зерно\.xlsx/);
  assert.match(chips[0].textContent, /24 КБ/, 'размер виден в чипе');
  assert.equal(toasts.filter((t) => t.kind === 'warn').length, 0);
});

test('прикрепляются пути, а не копии: содержимое файла не загружается', async () => {
  const { picker, calls } = await makePicker();
  await picker.open();
  await sleep(10);
  const uploads = calls.filter((c) => /upload|content|multipart/i.test(String(c[0])));
  assert.deepEqual(uploads, [], 'нет ни одной ручки загрузки файла');
  assert.deepEqual(calls.map((c) => c[0]).sort(),
    ['/api/files/attach-check', '/api/files/pick'], 'только выбор и проверка пути');
});

test('чип: «Просмотр», «Открыть», «Открыть в папке», «Убрать»', async () => {
  const { picker, host, calls } = await makePicker();
  await picker.open();
  await sleep(10);
  const chip = byClass(host, 'cmp-attach__item')[0];
  assert.ok(byClass(chip, 'cmp-attach__name')[0], 'имя файла');
  assert.ok(byClass(chip, 'cmp-attach__size')[0], 'размер файла');
  const preview = buttonWith(chip, 'Просмотр');
  const open = buttonWith(chip, 'Открыть');
  const reveal = buttonWith(chip, 'Открыть в папке');
  const drop = buttonWith(chip, 'Убрать');
  assert.ok(preview && open && reveal && drop, 'все четыре действия на месте');

  open.dispatch('click', { type: 'click', target: open });
  await sleep(10);
  let last = calls.filter((c) => c[0] === '/api/files/attach-open').pop();
  assert.equal(last[1].mode, 'open');
  assert.equal(last[1].path, SMEТА);

  reveal.dispatch('click', { type: 'click', target: reveal });
  await sleep(10);
  last = calls.filter((c) => c[0] === '/api/files/attach-open').pop();
  assert.equal(last[1].mode, 'reveal');

  drop.dispatch('click', { type: 'click', target: drop });
  await sleep(10);
  assert.equal(byClass(host, 'cmp-attach__item').length, 1, '«Убрать» убирает только свой чип');
  assert.deepEqual(picker.paths(), [DOGOVOR]);
});

test('окно закрыли без выбора — не ошибка и не чип', async () => {
  const { picker, host, toasts } = await makePicker({ pick: { ok: true, paths: [], cancelled: true } });
  await picker.open();
  await sleep(10);
  assert.equal(byClass(host, 'cmp-attach__item').length, 0);
  assert.equal(toasts.filter((t) => t.kind === 'danger').length, 0);
  assert.ok(toasts.some((t) => /отменён/i.test(t.message)));
});

test('окно недоступно: причина названа, ручной путь раскрыт (без тупика)', async () => {
  const { ApiError } = await import('../web/js/api.js');
  const err = new ApiError('pick_unavailable',
    'Окно выбора файлов есть только в Windows. Вставьте путь в поле «Путь к файлу».', 501);
  const { picker, host, toasts } = await makePicker({ pickError: err });

  await picker.open();
  await sleep(10);

  assert.ok(picker.isRowOpen(), 'запасное поле пути раскрыто');
  const field = byClass(host, 'cmp-attach__path')[0];
  assert.ok(field, 'поле пути существует внутри компонента');
  assert.ok(toasts.some((t) => /Windows/.test(t.message)), 'причина названа');
  assert.equal(picker.button.disabled, false, 'кнопка снова активна');
});

test('ручной путь: несколько строк — несколько путей', async () => {
  const { picker, host, calls } = await makePicker({ pick: { ok: true, paths: [], cancelled: true } });
  picker.showRow(true);
  const field = byClass(host, 'cmp-attach__path')[0];
  field.value = SMEТА + '\r\n' + DOGOVOR + '\r\n\r\n';
  const add = buttonWith(host, 'Прикрепить путь');
  add.dispatch('click', { type: 'click', target: add });
  await sleep(15);

  assert.deepEqual(picker.paths(), [SMEТА, DOGOVOR]);
  const check = calls.find((c) => c[0] === '/api/files/attach-check');
  assert.deepEqual(check[1].attach, [SMEТА, DOGOVOR]);
  assert.equal(field.value, '', 'поле очищено после добавления');
});

test('секрет: вопрос на чипе, «Всё равно прикрепить» → confirm_secret', async () => {
  const { picker, host, calls, toasts } = await makePicker({
    pick: { ok: true, paths: [TOKEN], cancelled: false },
  });
  await picker.open();
  await sleep(10);

  const chip = byClass(host, 'cmp-attach__item')[0];
  assert.match(chip.className, /cmp-attach__item--bad/, 'секрет помечен');
  assert.match(chip.textContent, /похож на секрет/, 'причина видна');
  assert.ok(picker.blocked(), 'компонент говорит «отправлять нельзя»');
  const confirm = buttonWith(chip, 'Всё равно прикрепить');
  assert.ok(confirm, 'кнопка подтверждения есть — тупика нет');

  confirm.dispatch('click', { type: 'click', target: confirm });
  await sleep(10);
  assert.equal(picker.blocked(), null, 'после подтверждения чип обычный');
  assert.deepEqual(picker.confirmedPaths(), [TOKEN]);
  assert.deepEqual(picker.paths(), [TOKEN], 'путь остался тем же — копии нет');
  assert.ok(toasts.some((t) => /будет прикреплён/.test(t.message)));
  assert.ok(calls.some((c) => c[0] === '/api/files/attach-check'), 'проверка шла через сервер');
});

test('несуществующий файл: причина рядом с файлом', async () => {
  const { picker, host, toasts } = await makePicker({
    pick: { ok: true, paths: [GHOST], cancelled: false },
  });
  await picker.open();
  await sleep(10);
  const chip = byClass(host, 'cmp-attach__item')[0];
  assert.match(chip.className, /cmp-attach__item--bad/);
  assert.match(chip.textContent, /файла нет/i, 'причина в чипе');
  assert.ok(toasts.some((t) => /файла нет/i.test(t.message)), 'и тостом');
  assert.deepEqual(picker.goodPaths(), [], 'непригодный путь не попадает в отправку');
});

test('предел 30: лишние не теряются молча, дубли не добавляются', async () => {
  const many = [];
  for (let i = 0; i < 31; i += 1) many.push('C:/Загрузки/файл ' + i + '.txt');
  const { picker, host, toasts } = await makePicker({
    pick: { ok: true, paths: many, cancelled: false, dropped: 1, total: 31, max: 30 },
  });
  await picker.open();
  await sleep(30);
  assert.equal(byClass(host, 'cmp-attach__item').length, 30, 'ровно 30 вложений');
  assert.ok(toasts.some((t) => /первые 30/.test(t.message)), 'о лишних сказали');

  const before = picker.count();
  await picker.add([many[0]]);
  await sleep(10);
  assert.equal(picker.count(), before, 'повтор того же пути не создаёт второй чип');
  assert.ok(toasts.some((t) => /уже прикреплён/.test(t.message)));
});

test('компонент не рисует innerHTML: только h()', async () => {
  const src = readFileSync(join(ROOT, 'web', 'js', 'attach-picker.js'), 'utf8');
  assert.equal(/\.innerHTML\s*=/.test(src), false, 'innerHTML из данных запрещён');
});

/* ─────────────────────────────────────── страж: promptDialog «Путь к файлу» */

function jsFiles(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const stat = statSync(full);
    if (stat.isDirectory()) jsFiles(full, out);
    else if (extname(full) === '.js') out.push(full);
  }
  return out;
}

test('страж: в web/js нет promptDialog с подписью «Путь к файлу»', () => {
  const offenders = [];
  for (const file of jsFiles(join(ROOT, 'web', 'js'))) {
    const src = readFileSync(file, 'utf8');
    let idx = src.indexOf('promptDialog(');
    while (idx !== -1) {
      const chunk = src.slice(idx, idx + 420);
      if (chunk.includes('Путь к файлу') && !/^\s*\*/.test(chunk)) {
        offenders.push(file.replace(ROOT + '\\', '').replace(ROOT + '/', ''));
        break;
      }
      idx = src.indexOf('promptDialog(', idx + 1);
    }
  }
  assert.deepEqual(offenders, [], 'скрепка не должна просить вставить путь: ' + offenders.join(', '));
});

test('страж: «Диалог по запросу» больше не просит путь вручную', () => {
  const src = readFileSync(join(ROOT, 'web', 'js', 'views', 'request-thread.js'), 'utf8');
  assert.equal(src.includes('promptDialog'), false, 'в диалоге нет promptDialog');
  assert.ok(src.includes("from '../attach-picker.js'"), 'диалог берёт общий компонент');
  assert.match(src, /createAttachPicker\(/, 'скрепка диалога — тот же компонент');
});

test('страж: композер тоже пользуется общим компонентом', () => {
  const src = readFileSync(join(ROOT, 'web', 'js', 'views', 'composer.js'), 'utf8');
  assert.match(src, /createAttachPicker\(/, 'композер берёт общий компонент');
  assert.equal(src.includes("'/api/files/pick'"), false, 'ручка выбора файлов живёт в одном месте');
});

/* ─────────────── «Запросы без дела» на реальной форме ответа (только чтение) */

test('«Запросы без дела»: форма боевого ответа — статусы по-русски, без латиницы', async () => {
  installDom();
  const { mount } = await import('../web/js/views/delo.js');
  const data = JSON.parse(readFileSync(join(ROOT, 'fixtures', 'w30f_results.json'), 'utf8'));

  // Форма записана с боевого GET /api/rooms/{id}/results (25.11.2026): у pending
  // нет slug/finished_at/short/reports, статусы — служебные коды.
  for (const row of data.pending) {
    assert.equal('slug' in row, false, 'у pending нет slug — как в бою');
    assert.equal('finished_at' in row, false, 'у pending нет finished_at — как в бою');
  }
  assert.ok(data.pending.some((r) => r.status === 'archived'), 'в бою встречается archived');
  assert.ok(data.pending.some((r) => r.status === 'failed'), 'и failed');

  const el = new FakeElement('div');
  globalThis.document.body.appendChild(el);
  // `without_delo` сервер собирает сам из items+pending (в бою — так же).
  const payload = Object.assign({}, data, {
    without_delo: [...data.items, ...data.pending].filter((row) => !row.delo),
    counts: { requests: data.items.length + data.pending.length, results: data.items.length,
      pending: data.pending.length, reports: 0, without_delo: data.items.length + data.pending.length },
  });
  const api = {
    get: async (path) => {
      if (path.startsWith('/api/rooms/cafe/results')) return payload;
      if (path.startsWith('/api/delos')) return { items: [] };
      return { items: [] };
    },
    post: async () => ({ ok: true }),
    patch: async () => ({}),
    del: async () => ({}),
  };
  const view = await mount(el, {
    api, room: { id: 'cafe', title: 'Кофейня' }, route: { roomId: 'cafe' },
    params: { roomId: 'cafe' }, toast: () => {}, navigate: () => {}, store: {},
  });
  await sleep(30);

  const block = byClass(el, 'delo-nodelo')[0];
  assert.ok(block, 'блок «Запросы без дела» отрисован на реальной форме ответа');
  assert.match(block.textContent, /Задачи без дела/);

  const statuses = byClass(block, 'delo-nodelo__status').map((n) => n.textContent.trim());
  assert.equal(statuses.length, data.items.length + data.pending.length, 'подпись у каждой строки');
  for (const label of statuses) {
    assert.equal(/[A-Za-z]/.test(label), false, 'служебный код латиницей в подписи: ' + label);
  }
  assert.ok(statuses.includes('завершён'), 'done → «завершён»');
  assert.ok(statuses.includes('в архиве'), 'archived → «в архиве» (раньше было сырое archived)');
  assert.ok(statuses.includes('с ошибкой'), 'failed → «с ошибкой»');
  assert.ok(statuses.includes('в работе'), 'working → «в работе»');

  const rows = byClass(block, 'delo-nodelo__row');
  assert.equal(rows.length, data.items.length + data.pending.length, 'каждая запись — строка');
  for (const row of rows) {
    const link = walk(row).find((n) => String(n.tagName).toLowerCase() === 'a');
    assert.ok(link && /^#\/room\/cafe\/request\/t_/.test(link.getAttribute('href')), 'ссылка на запрос');
    assert.ok(buttonWith(row, 'Сделать делом'), 'у каждой строки есть действие');
  }
  // Отписка от таймеров экрана: тестовый ctx неполный, поэтому ошибку уборки
  // глушим — она не про блок «Запросы без дела».
  if (view && typeof view.unmount === 'function') { try { view.unmount(); } catch { /* тестовый ctx */ } }
});
