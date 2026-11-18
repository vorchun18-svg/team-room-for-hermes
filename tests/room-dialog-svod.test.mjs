/**
 * room-dialog-svod.test.mjs — W20-FA §1.1: «Автообновление свода» во вкладке
 * «Работа» диалога настроек комнаты.
 *
 * Что проверяем:
 *  * переключатель «Свод обновляется сам» + «Подождать тишины» и «И не чаще раза в» —
 *    простыми словами, с подсказкой;
 *  * сохранённые значения комнаты приходят в поля (180 с / 600 с по умолчанию);
 *  * выключили автообновление — настройки частоты гаснут (не выглядят действующими);
 *  * при сохранении уходят `auto_svod`, `auto_svod_quiet_s`, `auto_svod_min_gap_s`.
 *
 * DOM — мини-DOM как в tests/room-dialog-tabs.test.mjs (браузер не нужен).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';

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
  if (idx !== -1) this.childNodes.splice(idx, 1);
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

const byId = (root, id) => walk(root).find((el) => el.getAttribute('id') === id) || null;
const byClass = (root, cls) => walk(root).filter((el) => String(el.className).split(/\s+/).includes(cls));
const textOf = (el) => String((el && el.textContent) || '');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PROFILES = [
  { profile: 'team-director', title: 'Руководитель «Лето»', kind: 'director' },
  { profile: 'team-critic', title: 'Критик «Лето»', kind: 'critic' },
  { profile: 'team-ops', title: 'Операции «Лето»', kind: 'worker' },
];

function room(over) {
  return Object.assign({
    id: 'leto', title: 'Кафе «Лето»', icon: 'project',
    orchestrator: 'team-director', critic: 'team-critic',
    members: [
      { profile: 'team-director', role: 'orchestrator' },
      { profile: 'team-critic', role: 'critic' },
      { profile: 'team-ops', role: 'worker' },
    ],
    context_files: [], project_dir: 'C:/Проекты/Кафе Лето',
    critic_pass: true, max_rounds: 2, critic_mode: 'rounds', critic_round_cap: 12,
    project_mode: false, launch_mode: 'plan_first', max_parallel_streams: 2,
    phases: [], agent_notes: {}, agent_models: {}, agent_models_meta: {},
    auto_svod: true, auto_svod_quiet_s: 180, auto_svod_min_gap_s: 600,
  }, over || {});
}

function ctxFor(state) {
  const calls = [];
  const api = {
    get: async (path) => {
      calls.push(['GET', path]);
      if (path === '/api/profiles') return PROFILES;
      if (path === '/api/models') {
        return { providers: [{ id: 'p', label: 'Провайдер', models: [{ id: 'm1', label: 'Модель' }] }], efforts: [] };
      }
      if (String(path).startsWith('/api/rooms/')) return state.room;
      return { rooms: [] };
    },
    post: async (path, body) => { calls.push(['POST', path, body]); return { allowed: true, exists: true }; },
    patch: async (path, body) => {
      calls.push(['PATCH', path, body]);
      state.room = Object.assign({}, state.room, body);
      return state.room;
    },
    put: async (path, body) => { calls.push(['PUT', path, body]); return {}; },
    del: async (path) => { calls.push(['DEL', path]); return {}; },
  };
  return { calls, api, toast: () => {}, navigate: () => {}, store: {}, refresh: () => {} };
}

/** Дождаться, пока окно настроек появится в документе. */
async function waitForDialog(doc) {
  for (let i = 0; i < 60; i += 1) {
    const found = byClass(doc.body, 'roomdlg')[0];
    if (found) return found;
    await sleep(10);
  }
  return null;
}

/** Открыть диалог на вкладке «Работа» и дождаться отрисовки. */
async function openWork(over) {
  const doc = installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const state = { room: room(over) };
  const ctx = ctxFor(state);
  openRoomDialog(ctx, state.room, { focus: 'work' });
  const dlg = await waitForDialog(doc);
  await sleep(30);
  return { doc, ctx, state, dlg };
}

/* ───────────────────────────── тесты ────────────────────────────────── */

test('вкладка «Работа»: переключатель и две настройки простыми словами', async () => {
  const { dlg } = await openWork();
  assert.ok(dlg, 'окно открылось');
  const panel = byId(dlg, 'roomdlg-panel-work');
  assert.ok(panel, 'вкладка «Работа» есть');
  const box = byClass(panel, 'roomdlg__svod')[0];
  assert.ok(box, 'раздел «Живой свод»');
  assert.match(textOf(box), /Живой свод/);
  assert.match(textOf(box), /Свод обновляется сам/);
  assert.match(textOf(box), /Подождать тишины/);
  assert.match(textOf(box), /И не чаще раза в/);
  assert.match(textOf(box), /тишина/i, 'объяснение «тишины» простыми словами');
  assert.match(textOf(box), /Обновить сейчас/, 'сказано, что кнопка есть всегда');

  const auto = byId(box, 'roomdlg-auto-svod');
  assert.ok(auto, 'переключатель автообновления');
  assert.equal(auto.checked, true, 'по умолчанию включено');

  const quiet = byId(box, 'roomdlg-svod-quiet');
  const gap = byId(box, 'roomdlg-svod-gap');
  assert.equal(quiet.value, '180', 'по умолчанию 3 минуты тишины');
  assert.equal(gap.value, '600', 'по умолчанию не чаще раза в 10 минут');
  assert.equal(quiet.disabled, false);
  assert.equal(gap.disabled, false);
  // Подписи в минутах, а не в секундах: человеку так понятнее.
  assert.match(textOf(quiet.options.find((o) => o.getAttribute('value') === '180')), /3 минуты/);
  assert.match(textOf(gap.options.find((o) => o.getAttribute('value') === '600')), /10 минут/);
});

test('выключили автообновление — настройки частоты гаснут', async () => {
  const { dlg } = await openWork();
  const auto = byId(dlg, 'roomdlg-auto-svod');
  const quiet = byId(dlg, 'roomdlg-svod-quiet');
  const gap = byId(dlg, 'roomdlg-svod-gap');
  auto.checked = false;
  auto.dispatch('change');
  assert.equal(quiet.disabled, true);
  assert.equal(gap.disabled, true);
  auto.checked = true;
  auto.dispatch('change');
  assert.equal(quiet.disabled, false);
  assert.equal(gap.disabled, false);
});

test('сохранение: auto_svod и обе частоты уходят в PATCH', async () => {
  const { dlg, ctx, doc } = await openWork();
  const auto = byId(dlg, 'roomdlg-auto-svod');
  const quiet = byId(dlg, 'roomdlg-svod-quiet');
  const gap = byId(dlg, 'roomdlg-svod-gap');
  auto.checked = true;
  quiet.value = '600';
  gap.value = '3600';

  const submit = walk(doc.body).find((el) => /^Сохранить/.test(textOf(el))
    && String(el.tagName).toLowerCase() === 'button');
  assert.ok(submit, 'кнопка «Сохранить»');
  dlg.dispatch('submit', { type: 'submit', preventDefault() {} });
  for (let i = 0; i < 40 && !ctx.calls.some((c) => c[0] === 'PATCH'); i += 1) await sleep(10);

  const patch = ctx.calls.find((c) => c[0] === 'PATCH');
  assert.ok(patch, 'PATCH комнаты отправлен');
  assert.equal(patch[2].auto_svod, true);
  assert.equal(patch[2].auto_svod_quiet_s, 600);
  assert.equal(patch[2].auto_svod_min_gap_s, 3600);
});

test('сохранённые значения комнаты приходят в поля', async () => {
  const { dlg } = await openWork({ auto_svod: false, auto_svod_quiet_s: 300, auto_svod_min_gap_s: 1800 });
  assert.equal(byId(dlg, 'roomdlg-auto-svod').checked, false);
  assert.equal(byId(dlg, 'roomdlg-svod-quiet').value, '300');
  assert.equal(byId(dlg, 'roomdlg-svod-gap').value, '1800');
  assert.equal(byId(dlg, 'roomdlg-svod-quiet').disabled, true, 'выключено — частота не действует');
});
