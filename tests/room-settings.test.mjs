/**
 * room-settings.test.mjs — W10: папка проекта у каждой комнаты, постоянная кнопка
 * «Настройки комнаты», понятные ошибки при открытии папки.
 *
 * Проверяем то, что видит Михаил:
 *  • место в шапке занято всегда: «Открыть папку» при заданном пути, «Задать папку»
 *    без пути; «Задать папку» открывает настройки комнаты и ставит курсор в поле
 *    «Папка проекта»;
 *  • «Настройки комнаты» — отдельная постоянная кнопка (шестерёнка + подпись);
 *  • тихая подсказка «Задайте папку проекта…» живёт под вкладками и тоже ведёт
 *    в настройки;
 *  • при смене папки диалог предупреждает: старые файлы остаются на месте;
 *  • если папка не открылась — окно «Не удалось открыть папку» с путём,
 *    «Скопировать путь» и (когда папки нет) «Создать папку и открыть».
 *
 * DOM — самодельный мини-DOM (как в room-dialog.test.mjs), браузер не нужен.
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
Object.defineProperty(FakeElement.prototype, 'options', {
  get() { return this.childNodes.filter((n) => n.nodeType === 1); },
  configurable: true,
});
FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
  const idx = ref ? this.childNodes.indexOf(ref) : -1;
  if (idx === -1) return this.appendChild(node);
  node.parentNode = this;
  return this.childNodes.splice(idx, 0, node)[0];
};

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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const DIR = 'C:\\TeamRoom\\Projects\\Лето';

function roomWithDir() {
  return {
    id: 'cafe', title: 'Лето', orchestrator: 'biz-director', critic: null,
    members: [{ profile: 'biz-director', role: 'orchestrator' }], context_files: [],
    project_dir: DIR, critic_pass: false, project_mode: true, launch_mode: 'plan_first',
    max_parallel_streams: 2, agent_notes: {}, agent_models: {},
  };
}

function roomWithoutDir() {
  return Object.assign(roomWithDir(), { id: 'fn', title: 'Рабочее пространство', project_dir: '' });
}

function ctxFor(room, { post } = {}) {
  const calls = [];
  const toasts = [];
  const api = {
    get: async (path) => {
      calls.push(['GET', path]);
      if (path === '/api/profiles') return [{ profile: 'biz-director', title: 'Директор', kind: 'director' }];
      if (path === '/api/models') return { models: [] };
      if (path === '/api/rooms/check-dir') return { allowed: true, exists: true };
      if (String(path).startsWith('/api/rooms/')) return room;
      return { rooms: [] };
    },
    post: async (path, body) => {
      calls.push(['POST', path, body]);
      return post ? post(path, body) : { ok: true, revealed: true };
    },
    patch: async (path, body) => {
      calls.push(['PATCH', path, body]);
      return Object.assign({}, room, body);
    },
    put: async (path, body) => { calls.push(['PUT', path, body]); return {}; },
    del: async (path) => { calls.push(['DEL', path]); return {}; },
  };
  return {
    calls, toasts, api,
    toast: (text, kind) => toasts.push([text, kind]),
    navigate: () => {},
    store: {},
    refresh: () => {},
  };
}

/* ─────────────────────────────────────────── кнопки шапки комнаты */

test('папка в шапке: «Открыть папку» при пути и «Задать папку» без пути', async () => {
  installDom();
  const { folderActionButton, OPEN_LABEL, ASK_LABEL } = await import('../web/js/project-dir-button.js');
  assert.equal(OPEN_LABEL, 'Открыть папку');
  assert.equal(ASK_LABEL, 'Задать папку');

  const withDir = folderActionButton(ctxFor(roomWithDir()), roomWithDir());
  assert.equal(withDir.getAttribute('data-pd'), 'open');
  assert.ok(withDir.textContent.includes('Открыть папку'), withDir.textContent);
  assert.equal(withDir.getAttribute('title'), DIR, 'полный путь — в подсказке');

  const without = folderActionButton(ctxFor(roomWithoutDir()), roomWithoutDir());
  assert.equal(without.getAttribute('data-pd'), 'ask', 'у комнаты без папки место тоже занято');
  assert.ok(without.textContent.includes('Задать папку'), without.textContent);
});

test('«Настройки комнаты» — постоянная кнопка с шестерёнкой и подписью', async () => {
  installDom();
  const { roomSettingsButton, SETTINGS_LABEL } = await import('../web/js/project-dir-button.js');
  const btn = roomSettingsButton(ctxFor(roomWithDir()), roomWithDir());
  assert.equal(btn.getAttribute('data-action'), 'room-settings');
  assert.equal(btn.getAttribute('aria-label'), SETTINGS_LABEL);
  assert.ok(btn.textContent.includes('Настройки комнаты'), btn.textContent);
  assert.ok(walk(btn).some((el) => String(el.className).includes('codicon')), 'иконка шестерёнки');
  assert.ok(btn.getAttribute('title').includes('папка проекта'), 'подсказка перечисляет, что настраивается');
});

test('подсказка «Задайте папку проекта…» ведёт в настройки с курсором в поле', async () => {
  const doc = installDom();
  const { projectDirHint, HINT_TEXT } = await import('../web/js/project-dir-button.js');
  const ctx = ctxFor(roomWithoutDir());
  const hint = projectDirHint(ctx, { roomId: 'fn' });
  assert.equal(hint.getAttribute('data-pd'), 'hint');
  assert.ok(hint.textContent.includes(HINT_TEXT));
  const link = byAttr(hint, 'data-pd', 'hint-link');
  assert.equal(link.getAttribute('href'), '#/room/fn/team');
  link.dispatch('click', { preventDefault() {} });
  await sleep(40);
  assert.ok(ctx.calls.some((c) => c[0] === 'GET' && c[1] === '/api/rooms/fn'), 'открылись настройки комнаты');
  const active = doc.activeElement;
  assert.ok(active, 'курсор должен стоять в поле');
  assert.equal(active.getAttribute('aria-label'), 'Папка проекта');
});

test('«Задать папку» из шапки открывает тот же диалог комнаты', async () => {
  const doc = installDom();
  const { folderActionButton } = await import('../web/js/project-dir-button.js');
  const room = roomWithoutDir();
  const ctx = ctxFor(room);
  const btn = folderActionButton(ctx, room);
  btn.dispatch('click', {});
  await sleep(60);
  assert.ok(doc.body.childNodes.length, 'диалог появился');
  const active = doc.activeElement;
  assert.equal(active && active.getAttribute('aria-label'), 'Папка проекта',
    'сразу можно печатать путь');
  assert.ok(ctx.calls.some((c) => c[0] === 'GET' && c[1] === '/api/rooms/fn'));
});

/* ────────────────────────────────── смена папки: что будет со старыми файлами */

test('диалог комнаты: при смене папки показывает предупреждение о старых файлах', async () => {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const { MOVE_NOTE } = await import('../web/js/project-dir-button.js');
  const ctx = ctxFor(roomWithDir());
  const done = openRoomDialog(ctx, roomWithDir(), { focus: 'project_dir' });
  await sleep(40);

  const note = byAttr(doc.body, 'data-pd', 'move-note');
  assert.ok(note, 'элемент предупреждения есть в диалоге');
  assert.ok(note.textContent.includes(MOVE_NOTE));
  assert.match(MOVE_NOTE, /Старые файлы останутся в прежней папке; новые пойдут в новую/);
  assert.ok(note.className.includes('hidden'), 'пока путь не меняли — не показываем');

  const input = byAttr(doc.body, 'aria-label', 'Папка проекта');
  input.value = 'C:\\TeamRoom\\Projects\\Лето 2';
  input.dispatch('input', { type: 'input' });
  assert.ok(!note.className.includes('hidden'), 'путь изменили — предупреждение видно');

  input.value = '';
  input.dispatch('input', { type: 'input' });
  assert.ok(!note.className.includes('hidden'), 'удаление папки — тоже предупреждение');

  input.value = DIR;
  input.dispatch('input', { type: 'input' });
  assert.ok(note.className.includes('hidden'), 'вернули прежний путь — предупреждение скрыто');

  const cancel = byText(doc.body, 'Отмена', 'button');
  cancel.dispatch('click');
  await done;
});

/* ───────────────────────────────────── ошибка открытия папки (W10, дефект 3) */

test('ошибка открытия: окно с путём, «Скопировать путь» и «Создать папку и открыть»', async () => {
  const doc = installDom();
  const { openFolderWithHelp, OPEN_FAILED } = await import('../web/js/project-dir-button.js');
  let created = null;
  const ctx = ctxFor(roomWithDir(), {
    post: (_path, body) => (body && body.mkdir
      ? { ok: true, revealed: true, foreground: true, path: body.path }
      : { ok: false, revealed: false, kind: 'dir', path: DIR, can_create: true,
          reason: 'Папки нет на диске — её можно создать' }),
  });
  const res = await openFolderWithHelp(ctx, DIR);
  assert.equal(res.ok, false);

  const dialogEl = doc.body.find('div');
  const text = doc.body.textContent;
  assert.ok(text.includes(OPEN_FAILED), 'заголовок «Не удалось открыть папку»');
  assert.ok(text.includes('Папки нет на диске — её можно создать'), 'причина по-русски');
  assert.ok(text.includes(DIR), 'путь показан');
  const copy = byText(doc.body, 'Скопировать путь', 'button');
  assert.ok(copy, 'кнопка «Скопировать путь»');
  const create = byText(doc.body, 'Создать папку и открыть', 'button');
  assert.ok(create, 'кнопка «Создать папку и открыть»');

  create.dispatch('click', {});
  await sleep(40);
  created = ctx.calls.filter((c) => c[0] === 'POST' && c[1] === '/api/open').map((c) => c[2]);
  assert.equal(created.length, 2, 'первый запрос — открыть, второй — создать и открыть');
  assert.deepEqual(created[1], { path: DIR, kind: 'dir', mkdir: true });
  assert.ok(ctx.toasts.some((t) => String(t[0]).includes('Папка создана')), JSON.stringify(ctx.toasts));
  assert.ok(dialogEl);
});

test('ошибка открытия без «создать»: только путь и копирование', async () => {
  const doc = installDom();
  const { folderProblemDialog } = await import('../web/js/project-dir-button.js');
  folderProblemDialog(ctxFor(roomWithDir()), {
    path: DIR, message: 'Не удалось открыть папку: проводник не запустился', canCreate: false,
  });
  assert.ok(byText(doc.body, 'Скопировать путь', 'button'));
  assert.equal(byText(doc.body, 'Создать папку и открыть', 'button'), null,
    'кнопки создания нет, если папка есть');
  assert.ok(doc.body.textContent.includes('проводник не запустился'));
  byText(doc.body, 'Закрыть', 'button').dispatch('click');
  assert.equal(doc.body.childNodes.length, 0, 'окно закрывается');
});

test('openProjectDir: kind=dir в запросе и понятная ошибка от сервера', async () => {
  const seen = [];
  const api = {
    post: async (path, body) => {
      seen.push([path, body]);
      if (body.path.endsWith('Windows')) throw new Error('Системный каталог — выберите другую папку');
      return { ok: true, revealed: true, foreground: true };
    },
  };
  const { openProjectDir } = await import('../web/js/project-dir-button.js');
  const ok = await openProjectDir(api, DIR, { kind: 'dir' });
  assert.deepEqual(seen[0], ['/api/open', { path: DIR, kind: 'dir' }]);
  assert.equal(ok.ok, true);
  assert.equal(ok.foreground, true);

  const bad = await openProjectDir(api, 'C:\\Windows', { kind: 'dir' });
  assert.equal(bad.ok, false);
  assert.ok(bad.reason.includes('Системный каталог'), bad.reason);

  // окно не вышло вперёд — это не ошибка открытия, но человек должен знать
  const quiet = await openProjectDir({ post: async () => ({ ok: true, revealed: true, foreground: false }) }, DIR);
  assert.equal(quiet.ok, true);
});

/* ──────────────────────────────────────── договор с шапкой и адаптивом */

test('шапка: кнопки комнаты в правой группе, подпись настроек — с адаптивом', () => {
  const app = readFileSync(new URL('../web/js/app.js', import.meta.url), 'utf8');
  assert.match(app, /right\.push\(roomSettingsButton\(roomCtx, room\)\)/);
  assert.match(app, /right\.push\(folderActionButton\(roomCtx, room\)\)/);
  assert.match(app, /hintNode = projectDirHint\(roomCtx, \{ roomId: room\.id \}\)/);

  const css = readFileSync(new URL('../web/css/project-dir.css', import.meta.url), 'utf8');
  assert.match(css, /\.room-settings__label \{ font-size: var\(--text-sm\)/);
  assert.match(css, /@media \(max-width: 1099px\) \{\s*\.room-settings__label \{ display: none; \}/,
    'на узком экране — только иконка');
  assert.match(css, /\.pd-help__path/, 'блок с путём в окне ошибки');
  assert.doesNotMatch(css, /#(?:[0-9a-f]{3}){1,2}\b/i, 'только токены, без литеральных цветов');
});

test('путь в запросах не склеивается в shell-команду', () => {
  const src = readFileSync(new URL('../web/js/project-dir-button.js', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /shell\s*=\s*True|child_process|exec\(/, 'никаких shell-вызовов в интерфейсе');
  const py = readFileSync(new URL('../server/project_dir.py', import.meta.url), 'utf8');
  assert.doesNotMatch(py, /shell\s*=\s*True/, 'сервер не отдаёт путь оболочке');
  assert.match(py, /_find_explorer_window|EnumWindows/, 'окно проводника ищем через EnumWindows');
  assert.match(py, /SetForegroundWindow/, 'и выводим его вперёд');
});
