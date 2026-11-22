/**
 * room-dialog-size.test.mjs — волна 29, пакет P3 (дефект 12).
 *
 * Что проверяем:
 *  • разделы окна «Настройки комнаты» лежат в ОДНОЙ ячейке grid и неактивные
 *    скрыты `visibility: hidden`, а не `display: none`: высота окна не зависит
 *    от активной вкладки (иначе окно меняет размер и место — это и есть дефект);
 *  • у окна задана фиксированная высота и она переживает узкий экран (100dvh − отступы);
 *  • прокручивается только тело окна, строка вкладок и подвал на месте;
 *  • та же дисциплина у окна «Модель участника», панели диктовки и шагов мастера дежурств;
 *  • в DOM: панели в обёртке .roomdlg__panels, неактивная получает класс
 *    `roomdlg__panel--off` и атрибут `hidden`, активная — ни того, ни другого.
 *
 * Точные замеры в браузере — tools/probe_dialog_size.py (0 px отклонения).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';

const read = (p) => readFileSync(new URL('../' + p, import.meta.url), 'utf8');
const ROOMS_CSS = read('web/css/view-rooms.css');
const MODEL_CSS = read('web/css/agent-model.css');
const DICT_CSS = read('web/css/dictation.css');
const DUTIES_CSS = read('web/css/view-duties.css');

/* ─────────────────────────────────────── CSS: размер окна не зависит от вкладки */

/** Тело правила по селектору (первое совпадение, пробелы нормализованы). */
function rule(css, selector) {
  const flat = css.replace(/\s+/g, ' ');
  const idx = flat.indexOf(selector + ' {');
  if (idx === -1) return '';
  return flat.slice(idx + selector.length, flat.indexOf('}', idx));
}

test('P3: разделы в одной ячейке grid, неактивный скрыт visibility, а не display', () => {
  assert.match(rule(ROOMS_CSS, '.roomdlg__panels'), /display: grid/,
    'обёртка разделов — grid: все панели в одной ячейке');
  assert.match(rule(ROOMS_CSS, '.roomdlg__panel'), /grid-area: 1 \/ 1/,
    'каждая панель занимает одну и ту же ячейку');
  const off = rule(ROOMS_CSS, '.roomdlg__panel--off');
  assert.match(off, /visibility: hidden/, 'неактивный раздел невидим, но занимает место');
  assert.doesNotMatch(off, /display: none/, 'display:none вернул бы «прыгающее» окно');
  assert.doesNotMatch(ROOMS_CSS, /\.roomdlg__panel\[hidden\]\s*\{\s*display:\s*none/,
    'правила display:none для неактивного раздела быть не должно');
});

test('P3: у окна настроек фиксированная высота и запас по 100dvh', () => {
  const shell = rule(ROOMS_CSS, '.dialog.roomdlg-shell');
  assert.match(shell, /height: min\(/, 'высота задана, а не «по содержимому»');
  assert.match(shell, /max-height: calc\(100dvh/, 'на низком экране окно ужимается по 100dvh');
  // Узкий экран: та же фиксированная высота минус отступы подложки, подвал виден.
  const narrow = ROOMS_CSS.slice(ROOMS_CSS.indexOf('@media (max-width: 899px)'));
  assert.match(narrow.slice(0, 600), /\.dialog\.roomdlg-shell \{ height: min\(calc\(100dvh/,
    'на узком экране окно занимает 100dvh минус отступы');
});

test('P3: прокрутка только в теле окна — вкладки и подвал не в потоке содержимого', () => {
  // Тело окна прокручивается (components.css), строка вкладок и подвал — flex: none
  // (в view-rooms.css у полосы вкладок), поэтому они не «уезжают».
  const tabs = rule(ROOMS_CSS, '.roomdlg__tabs');
  assert.match(tabs, /flex: none/, 'строка вкладок не сжимается и не едет');
  assert.doesNotMatch(tabs, /position: (absolute|fixed)/, 'вкладки в обычном потоке окна');
});

test('P3: модель участника, диктовка и шаги мастера — тоже одного размера', () => {
  assert.match(rule(MODEL_CSS, '.dialog.agent-model__dialog'), /height: min\(/,
    'окно «Модель участника» не растягивается при раскрытии участника');
  const mic = DICT_CSS.slice(DICT_CSS.indexOf('.mic-panel {', DICT_CSS.indexOf('mic-panel-in')));
  assert.match(mic.slice(0, 500), /height: 208px/, 'панель диктовки одного размера');
  assert.match(mic.slice(0, 500), /overflow-y: auto/, 'лишнее едет внутри панели');
  assert.match(rule(DUTIES_CSS, '.dw__body'), /height: clamp\(/,
    'шаги мастера дежурств одной высоты');
});

/* ─────────────────────────────────────────────── DOM: обёртка и переключение */

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
FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
  const idx = ref ? this.childNodes.indexOf(ref) : -1;
  if (idx === -1) return this.appendChild(node);
  node.parentNode = this;
  return this.childNodes.splice(idx, 0, node)[0];
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

function walk(node, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    out.push(child);
    walk(child, out);
  }
  return out;
}

const byClass = (root, cls) => walk(root).filter((el) => String(el.className).split(/\s+/).includes(cls));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function ctxFor(room) {
  const api = {
    get: async (path) => {
      if (path === '/api/profiles') {
        return [
          { profile: 'team-director', title: 'Руководитель проекта', kind: 'director' },
          { profile: 'team-critic', title: 'Критик', kind: 'critic' },
          { profile: 'team-ops', title: 'Операции', kind: 'worker' },
        ];
      }
      if (path === '/api/models') {
        return { providers: [{ id: 'p', label: 'Провайдер', models: [{ id: 'm-1', label: 'Модель 1' }] }] };
      }
      if (path === '/api/rooms/check-dir') return { allowed: true, exists: true };
      if (String(path).startsWith('/api/rooms/')) return room;
      return { rooms: [] };
    },
    post: async () => ({ ok: true }),
    patch: async () => room,
    put: async () => ({}),
    del: async () => ({}),
  };
  return { api, toast: () => {}, navigate: () => {}, store: {}, refresh: () => {} };
}

function sampleRoom() {
  return {
    id: 'cafe', title: 'Кофейня', icon: 'project',
    orchestrator: 'team-director', critic: 'team-critic',
    members: [
      { profile: 'team-director', role: 'orchestrator' },
      { profile: 'team-critic', role: 'critic' },
      { profile: 'team-ops', role: 'worker' },
    ],
    context_files: [], project_dir: 'C:\\Проекты\\Кофейня',
    critic_pass: true, max_rounds: 2, critic_mode: 'rounds', critic_round_cap: 12,
    project_mode: false, launch_mode: 'plan_first', max_parallel_streams: 2,
    phases: [], agent_notes: {}, agent_models: {}, agent_models_meta: {},
  };
}

test('P3: панели в обёртке .roomdlg__panels, неактивные помечены, активная одна', async () => {
  installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const room = sampleRoom();
  const done = openRoomDialog(ctxFor(room), room);
  await sleep(40);

  const doc = globalThis.document;
  const stacks = byClass(doc.body, 'roomdlg__panels');
  assert.equal(stacks.length, 1, 'панели собраны в одну обёртку (одна ячейка grid)');

  const panels = walk(doc.body).filter((el) => el.getAttribute('role') === 'tabpanel');
  assert.ok(panels.length >= 5, 'разделы на месте');
  for (const panel of panels) assert.equal(panel.parentNode, stacks[0], 'каждая панель — внутри обёртки');

  const on = panels.filter((p) => !p.hasAttribute('hidden'));
  assert.equal(on.length, 1, 'на экране ровно один раздел');
  assert.equal(on[0].dataset.panel, 'main', 'открывается «Основное»');
  for (const panel of panels) {
    const off = panel !== on[0];
    assert.equal(String(panel.className).split(/\s+/).includes('roomdlg__panel--off'), off,
      'неактивные разделы помечены классом, который только скрывает видимость');
  }

  // Переключение: класс меняется местами, атрибут hidden остаётся у неактивных.
  const workBtn = walk(doc.body).find((el) => el.getAttribute('data-tab') === 'work'
    && /roomdlg__tab/.test(String(el.className)));
  workBtn.dispatch('click', { type: 'click', preventDefault() {} });
  const workPanel = panels.find((p) => p.dataset.panel === 'work');
  assert.equal(String(workPanel.className).split(/\s+/).includes('roomdlg__panel--off'), false,
    'активный раздел виден');
  assert.equal(workPanel.hasAttribute('hidden'), false);
  assert.equal(String(panels[0].className).split(/\s+/).includes('roomdlg__panel--off'), true,
    'прежний раздел скрыт, но не выпал из потока');

  // Закрываем окно «как человек» — иначе промис диалога не завершится.
  const closeBtn = walk(doc.body).find((el) => el.getAttribute('aria-label') === 'Закрыть');
  if (closeBtn) closeBtn.dispatch('click', { type: 'click', preventDefault() {} });
  const drop = walk(doc.body).find((el) => /Закрыть без сохранения/.test(el.textContent || ''));
  if (drop) drop.dispatch('click', { type: 'click', preventDefault() {} });
  await done;
});
