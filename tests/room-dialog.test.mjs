/**
 * room-dialog.test.mjs — поле «Папка проекта» и кнопка «Открыть папку проекта».
 *
 * Проверяем Д2 WAVE8-RULES:
 *  • «Предложить» подставляет `C:\Users\Public\Documents\<Название комнаты>`;
 *  • живая проверка пути идёт через `POST /api/rooms/check-dir` с задержкой 400 мс;
 *  • вердикт по-русски: «Папка существует» / «Будет создана» / «Нельзя: …»;
 *  • микрофон ставится автоматически (поле НЕ помечено data-mic="off");
 *  • кнопка «Открыть папку проекта» ходит в `/api/open`; без пути и без showHint — null;
 *  • без пути с showHint — тихая подсказка со ссылкой в настройки комнаты.
 *
 * DOM — самодельный мини-DOM (как tests/fake-dom.mjs), браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, FakeText, createFakeDocument } from './fake-dom.mjs';

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
  // без head: ensureCss() сразу считает стили подключёнными и не ждёт load
  globalThis.document = doc;
  return doc;
}

// то, чего нет в мини-DOM: фокус и список <option> у <select>
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

function byText(root, text, tag) {
  return walk(root).find((el) => String(el.tagName).toLowerCase() === tag
    && el.textContent.trim() === text) || null;
}

function byAttr(root, name, value) {
  return walk(root).find((el) => el.getAttribute(name) === value) || null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const PROFILES = [
  { profile: 'biz-director', title: 'Директор Лето', kind: 'director' },
  { profile: 'biz-ops', title: 'Операции', kind: 'worker' },
  { profile: 'biz-cfo', title: 'Экономика Лето', kind: 'worker' },
];

/* ─────────────────────────────────────────────── чистые функции */

test('suggestProjectDir: C:\\Users\\Public\\Documents\\<Название комнаты>', async () => {
  const { suggestProjectDir } = await import('../web/js/views/room-dialog.js');
  assert.equal(suggestProjectDir('Лето'), 'C:\\Users\\Public\\Documents\\Лето');
  assert.equal(suggestProjectDir('  Рабочее пространство '), 'C:\\Users\\Public\\Documents\\Рабочее пространство');
  // запрещённые в Windows символы не попадают в путь
  const weird = suggestProjectDir('Отчёт: "касса" / смета? *');
  assert.ok(!/[<>:"/\\|?*]/.test(weird.slice('C:\\Users\\Public\\Documents\\'.length)), weird);
  // пустое название — понятная папка по умолчанию
  assert.equal(suggestProjectDir(''), 'C:\\Users\\Public\\Documents\\Новая комната');
  assert.equal(suggestProjectDir(' . '), 'C:\\Users\\Public\\Documents\\Новая комната');
  assert.ok(suggestProjectDir('очень длинное название '.repeat(10)).length <= 80);
});

test('dirVerdict: «Папка существует» / «Будет создана» / «Нельзя: …»', async () => {
  const { dirVerdict } = await import('../web/js/views/room-dialog.js');
  assert.deepEqual(dirVerdict({ allowed: true, exists: true, will_create: false }),
    { tone: 'ok', text: 'Папка существует', warn: '' });
  assert.deepEqual(dirVerdict({ allowed: true, exists: false, will_create: true }),
    { tone: 'new', text: 'Будет создана', warn: '' });
  const bad = dirVerdict({ allowed: false, reason: 'Папка внутри служебных каталогов Hermes — выберите другую' });
  assert.equal(bad.tone, 'bad');
  assert.equal(bad.text, 'Нельзя: Папка внутри служебных каталогов Hermes — выберите другую');
  // ответ без вердикта — «Проверяю…», а не пустота
  assert.equal(dirVerdict(null).text, 'Проверяю…');
  // предупреждение сервера (например, про Vault) показывается рядом
  const warned = dirVerdict({ allowed: true, exists: true, warnings: ['Папка внутри Vault'] });
  assert.equal(warned.warn, 'Папка внутри Vault');
});

/* ─────────────────────────────────────────── кнопка папки проекта */

test('projectDirButton: без пути и без showHint — null', async () => {
  installDom();
  const { projectDirButton } = await import('../web/js/project-dir-button.js');
  assert.equal(projectDirButton({ api: {} }, { roomId: 'cafe', projectDir: '', showHint: false }), null);
  assert.equal(projectDirButton({ api: {} }, { roomId: 'cafe' , showHint: false }), null);
});

test('projectDirButton: подсказка ведёт в настройки комнаты', async () => {
  installDom();
  const { projectDirButton, HINT_TEXT } = await import('../web/js/project-dir-button.js');
  const hint = projectDirButton({ api: {} }, { roomId: 'cafe', projectDir: '' });
  assert.ok(hint, 'подсказка должна быть');
  assert.equal(hint.getAttribute('data-pd'), 'hint');
  assert.ok(hint.textContent.includes(HINT_TEXT));
  const link = walk(hint).find((el) => el.tagName.toLowerCase() === 'a');
  assert.equal(link.getAttribute('href'), '#/room/cafe/team');
});

test('projectDirButton: кнопка открывает папку через /api/open', async () => {
  installDom();
  const { projectDirButton } = await import('../web/js/project-dir-button.js');
  const calls = [];
  const api = { post: async (path, body) => { calls.push([path, body]); return { ok: true, revealed: true }; } };
  const toasts = [];
  const btn = projectDirButton({ api, toast: (t) => toasts.push(t) },
    { roomId: 'cafe', projectDir: 'C:\\TeamRoom\\Projects\\Лето' });
  assert.equal(btn.getAttribute('data-pd'), 'open');
  assert.ok(btn.textContent.includes('Открыть папку проекта'));
  btn.dispatch('click');
  await sleep(20);
  assert.deepEqual(calls, [['/api/open', { path: 'C:\\TeamRoom\\Projects\\Лето' }]]);
  assert.deepEqual(toasts, []);

  // папку не открыли — честная подсказка, без «внутренней ошибки»
  const api2 = { post: async () => ({ ok: true, revealed: false }) };
  const btn2 = projectDirButton({ api: api2, toast: (t) => toasts.push(t) },
    { roomId: 'cafe', projectDir: 'C:\\TeamRoom\\Projects\\Нет' });
  btn2.dispatch('click');
  await sleep(20);
  assert.ok(toasts.length === 1 && toasts[0].includes('Папка не открылась'), toasts.join('|'));
});

/* ──────────────────────────────────────────────── диалог комнаты */

function dialogCtx() {
  const calls = [];
  const api = {
    get: async (path) => (path === '/api/profiles' ? PROFILES : { rooms: [] }),
    post: async (path, body) => {
      calls.push(['POST', path, body]);
      if (path === '/api/rooms/check-dir') {
        return String(body.path).includes('Windows')
          ? { allowed: false, reason: 'Системный каталог — выберите другую папку' }
          : { allowed: true, exists: false, will_create: true, warnings: [] };
      }
      return { id: 'test-room' };
    },
    patch: async (path, body) => { calls.push(['PATCH', path, body]); return { id: 'test-room' }; },
    del: async () => ({}),
  };
  return { api, calls, toast: () => {}, navigate: () => {}, store: {} };
}

test('диалог: поле «Папка проекта», «Предложить» и живая проверка через 400 мс', async () => {
  const doc = installDom();
  const { openRoomDialog, CHECK_DELAY_MS } = await import('../web/js/views/room-dialog.js');
  const ctx = dialogCtx();
  assert.equal(CHECK_DELAY_MS, 400);

  const done = openRoomDialog(ctx, null);
  await sleep(30);

  const input = byAttr(doc.body, 'aria-label', 'Папка проекта');
  assert.ok(input, 'поле «Папка проекта» должно быть в диалоге');
  assert.equal(input.getAttribute('data-mic'), null, 'микрофон подключается сам — data-mic не ставим');
  assert.ok(walk(doc.body).some((el) => el.className.includes('field__label')),
    'у поля есть подпись');

  // подсказка «зачем»
  const text = doc.body.textContent;
  assert.ok(text.includes('Сюда команда будет складывать отчёты и материалы по этой комнате'), 'подсказка о смысле папки');
  assert.ok(text.includes('Папка проекта'));

  // название комнаты → «Предложить» подставляет путь
  const titleInput = byAttr(doc.body, 'aria-label', 'Название комнаты');
  titleInput.value = 'Лето';
  const suggest = byText(doc.body, 'Предложить', 'button');
  assert.ok(suggest, 'кнопка «Предложить»');
  suggest.dispatch('click');
  assert.equal(input.value, 'C:\\Users\\Public\\Documents\\Лето');

  // проверка идёт с задержкой: сразу запроса ещё нет
  assert.equal(ctx.calls.filter((c) => c[1] === '/api/rooms/check-dir').length, 0);
  await sleep(150);
  assert.equal(ctx.calls.filter((c) => c[1] === '/api/rooms/check-dir').length, 0,
    'до 400 мс запрос не уходит');
  await sleep(400);
  const checks = ctx.calls.filter((c) => c[1] === '/api/rooms/check-dir');
  assert.equal(checks.length, 1);
  assert.deepEqual(checks[0][2], { path: 'C:\\Users\\Public\\Documents\\Лето' });
  assert.ok(doc.body.textContent.includes('Будет создана'), doc.body.textContent.slice(-200));

  // запрещённый путь → «Нельзя: …» и сохранение останавливается
  input.value = 'C:\\Windows\\System32';
  input.dispatch('input', { type: 'input' });
  await sleep(CHECK_DELAY_MS + 120);
  assert.ok(doc.body.textContent.includes('Нельзя: Системный каталог'), doc.body.textContent.slice(-200));
  const alert = byAttr(doc.body, 'role', 'alert');
  assert.equal(alert, null, 'ошибка показывается при попытке сохранить, а не сразу');

  // папка очищена — вердикт убирается
  input.value = '';
  input.dispatch('input', { type: 'input' });
  await sleep(20);
  assert.ok(!doc.body.textContent.includes('Нельзя'), 'пустое поле — без вердикта');

  // закрыть диалог (обещание закрывается)
  const cancel = byText(doc.body, 'Отмена', 'button');
  cancel.dispatch('click');
  await done;
});

test('room-dialog.js: путь уходит в POST/PATCH как project_dir', () => {
  const source = readFileSync(new URL('../web/js/views/room-dialog.js', import.meta.url), 'utf8');
  assert.ok(source.includes('project_dir: pathInput.value.trim()'), 'project_dir в теле запроса');
  assert.ok(source.includes("'/api/rooms/check-dir'"), 'адрес живой проверки');
  assert.ok(source.includes('suggestProjectDir'), 'предложение пути берётся из project-dir-button');
  assert.ok(!/C:[\\/]+Hermes/.test(source), 'в коде нет пути автора');
  assert.ok(!/pathInput[\s\S]{0,200}data-mic/.test(source), 'поле пути не отключает микрофон');
});

/* ──────────────────────────────── проектный режим комнаты (A1/A2) */

const PM_ROOM = {
  id: 'cafe', title: 'Лето', icon: 'cake',
  orchestrator: 'biz-director', critic: '',
  members: [{ profile: 'biz-ops', role: 'worker' }],
  context_files: [], project_dir: '', critic_pass: false, max_rounds: 1,
  launch_mode: 'plan_first', max_parallel_streams: 2, project_mode: true,
};

function modeNodes(doc) {
  const all = walk(doc.body);
  // W17-R §3: пояснение про расходы и нагрузку на модели (слова Михаила).
  const hint = all.find((el) => String(el.className).includes('roomdlg__hint')
    && el.textContent.includes('нагрузки на модели')) || null;
  return {
    box: all.find((el) => el.getAttribute('id') === 'roomdlg-project-mode') || null,
    radios: all.filter((el) => el.getAttribute('name') === 'roomdlg-launch'),
    streams: all.find((el) => el.getAttribute('aria-label') === 'Одновременно частей работы') || null,
    hint,
  };
}

test('диалог: «Проектный режим» новой комнаты — включён, запуск сразу, 2 части работы', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const done = openRoomDialog(dialogCtx(), null);
  await sleep(30);

  const { box, radios, streams, hint } = modeNodes(doc);
  assert.ok(box, 'переключатель «Руководитель делит большой запрос на части работы»');
  assert.equal(box.checked, true, 'у новой комнаты проектный режим включён');
  assert.equal(radios.length, 2);
  // W17-R §3 (решение Михаила 06.11): у НОВОЙ комнаты части запускаются сразу.
  assert.equal(radios[0].getAttribute('value'), 'auto');
  assert.equal(radios[0].checked, true, 'по умолчанию «сразу, без подтверждения»');
  assert.equal(radios[1].checked, false);
  assert.equal(streams.value, '2', 'одновременно 2 части работы');
  assert.equal(streams.childNodes.filter((n) => n.nodeType === 1).length, 4, '1..4');
  assert.deepEqual(streams.childNodes.filter((n) => n.nodeType === 1)
    .map((n) => n.textContent.trim()),
  ['1 часть работы', '2 части работы', '3 части работы', '4 части работы']);
  assert.ok(hint && !hint.className.includes('hidden'), 'пояснение про расход лимита видно');

  const text = doc.body.textContent;
  for (const word of ['директор', 'диспетчер', 'канбан', 'JSON', 'API', 'токен']) {
    assert.ok(!text.includes(word), 'без технических слов: ' + word);
  }
  assert.ok(text.includes('Руководитель комнаты'), 'слово «руководитель комнаты»');

  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done;
});

test('диалог: выключенный проектный режим — поля недоступны и есть объяснение', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const done = openRoomDialog(dialogCtx(), Object.assign({}, PM_ROOM, { project_mode: false }));
  await sleep(30);

  let nodes = modeNodes(doc);
  assert.equal(nodes.box.checked, false);
  assert.equal(nodes.radios[0].disabled, true, 'режим запуска неактивен');
  assert.equal(nodes.streams.disabled, true);
  assert.ok(doc.body.textContent.includes('Сейчас: выключен'), 'состояние словами');
  assert.ok(nodes.hint.className.includes('hidden'), 'пояснение про лимит скрыто');

  nodes.box.checked = true;
  nodes.box.dispatch('change');
  nodes = modeNodes(doc);
  assert.equal(nodes.radios[0].disabled, false, 'включили — поля снова доступны');
  assert.equal(nodes.streams.disabled, false);
  assert.ok(!nodes.hint.className.includes('hidden'), 'пояснение про лимит снова видно');

  byText(doc.body, 'Отмена', 'button').dispatch('click');
  await done;
});

test('диалог: «сразу» и 4 части работы уходят в PATCH, папка и название сохраняются', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const ctx = dialogCtx();
  ctx.api.patch = async (path, body) => { ctx.calls.push(['PATCH', path, body]); return { id: 'cafe' }; };
  const done = openRoomDialog(ctx, Object.assign({}, PM_ROOM, {
    project_dir: 'C:\\TeamRoom\\Projects\\Лето',
  }));
  await sleep(30);

  const nodes = modeNodes(doc);
  // radios[0] — «сразу» (auto), radios[1] — «сначала план» (plan_first).
  nodes.radios[0].checked = true;
  nodes.radios[0].dispatch('change', { type: 'change', target: nodes.radios[0] });
  nodes.streams.value = '4';
  nodes.streams.dispatch('change');

  const form = walk(doc.body).find((el) => el.tagName.toLowerCase() === 'form');
  form.dispatch('submit', { type: 'submit', preventDefault() {} });
  await sleep(50);

  const patch = ctx.calls.find((c) => c[0] === 'PATCH');
  assert.ok(patch, 'PATCH ушёл');
  assert.equal(patch[1], '/api/rooms/cafe');
  assert.equal(patch[2].launch_mode, 'auto');
  assert.equal(patch[2].max_parallel_streams, 4);
  assert.equal(patch[2].project_mode, true);
  assert.equal(patch[2].project_dir, 'C:\\TeamRoom\\Projects\\Лето', 'папка проекта не теряется');
  assert.equal(patch[2].title, 'Лето');
  assert.ok(!('members' in patch[2]), 'состав не переписывается общим сохранением');
  assert.equal(ctx.calls.filter((c) => c[0] === 'POST' && /\/members$/.test(c[1])).length, 0,
    'состав не изменился — участников не трогаем');
  await done;
});

test('room-dialog.js: слова для человека, без «директора» и общих режимов', () => {
  const source = readFileSync(new URL('../web/js/views/room-dialog.js', import.meta.url), 'utf8');
  assert.ok(!/директор/i.test(source), 'в диалоге — «руководитель комнаты», не «директор»');
  assert.ok(source.includes('Руководитель делит большой запрос на части работы'));
  assert.ok(source.includes('Как запускать части работы'));
  assert.ok(source.includes('Одновременно частей работы'));
  assert.ok(source.includes('max_parallel_streams: Number.isInteger(maxStreams)'));
  assert.ok(!/launch_mode[^\n]*все комнаты/i.test(source));
});
