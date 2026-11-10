/**
 * dictation-recover.test.mjs — «нерасшифрованная запись»: убрал — значит навсегда,
 * и пустые «хвосты» не копятся.
 *
 * Что случилось у Михаила (дословно): микрофон отвалился во время диктовки,
 * запись не удалась; при новом открытии панели — «Есть нерасшифрованная запись»;
 * «Распознать» → «не удалось»; «Убрать» → исчезает; но при следующем открытии
 * панели (даже после удачной диктовки) надпись снова на месте.
 *
 * Причины, которые эти тесты фиксируют (каждая воспроизводится на старом коде):
 *   1. `rememberPending(rid, {parts:0, seconds:0})` вызывался в момент СТАРТА
 *      записи. Микрофон отваливался — звука нет, а «след» остаётся, и «Распознать»
 *      по нему честно не может ничего найти («не удалось»);
 *   2. нажатие «Надиктовать» каждый раз даёт новый rid, а чистился только текущий:
 *      каждая сорванная попытка добавляла ещё один «хвост» (до 20 штук);
 *   3. список показывал только ПЕРВУЮ запись и после «Убрать» не перечитывался:
 *      при следующем открытии вылезал следующий хвост — «убрал, а оно снова висит»;
 *   4. «Убрать» не удалял серверную копию (она живёт 72 часа);
 *   5. обрыв микрофона во время записи никак не замечался: браузер останавливает
 *      рекордер, а панель ещё минуту показывает «Запись» с бегущим таймером.
 *
 * DOM — минимальный tests/fake-dom.mjs (как в dictation.test.mjs).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createFakeDocument, FakeElement } from './fake-dom.mjs';
import { createMic, resetSettings } from '../web/js/dictation.js';
import { listPending, getPart, clearAll } from '../web/js/dictation-store.js';

/* ────────────────────────────────────────────── доработка fake-dom */

if (typeof FakeElement.prototype.replaceChildren !== 'function') {
  FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
    this.childNodes = [];
    for (const node of nodes) if (node) this.appendChild(node);
  };
}
if (typeof FakeElement.prototype.remove !== 'function') {
  FakeElement.prototype.remove = function remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  };
}
if (typeof FakeElement.prototype.removeChild !== 'function') {
  FakeElement.prototype.removeChild = function removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
    return node;
  };
}
if (typeof FakeElement.prototype.insertBefore !== 'function') {
  FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
    if (!node || typeof node.nodeType !== 'number') throw new TypeError('insertBefore expects a node');
    if (node.parentNode) node.parentNode.removeChild(node);
    const idx = ref ? this.childNodes.indexOf(ref) : -1;
    if (idx === -1) this.childNodes.push(node);
    else this.childNodes.splice(idx, 0, node);
    node.parentNode = this;
    return node;
  };
}
if (typeof FakeElement.prototype.dispatchEvent !== 'function') {
  FakeElement.prototype.dispatchEvent = function dispatchEvent(ev) {
    for (const fn of this.listeners[(ev && ev.type) || ''] || []) fn(ev);
    return true;
  };
}
function matches(el, selector) {
  const s = String(selector).trim();
  if (!s) return false;
  const tag = String(el.tagName || '').toLowerCase();
  if (s.startsWith('.')) return el.className.split(/\s+/).includes(s.slice(1));
  if (s.startsWith('[')) {
    const m = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(s);
    if (!m) return false;
    return m[2] === undefined ? el.hasAttribute(m[1]) : el.getAttribute(m[1]) === m[2];
  }
  return tag === s.toLowerCase();
}
if (typeof FakeElement.prototype.querySelectorAll !== 'function') {
  FakeElement.prototype.querySelectorAll = function querySelectorAll(selector) {
    const parts = String(selector).split(',').map((x) => x.trim()).filter(Boolean);
    const out = [];
    const walk = (node) => {
      for (const child of node.childNodes || []) {
        if (child.nodeType !== 1) continue;
        if (parts.some((p) => matches(child, p))) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  };
}
if (typeof FakeElement.prototype.querySelector !== 'function') {
  FakeElement.prototype.querySelector = function querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  };
}
if (typeof FakeElement.prototype.closest !== 'function') {
  FakeElement.prototype.closest = function closest(selector) {
    const parts = String(selector).split(',').map((x) => x.trim()).filter(Boolean);
    let node = this;
    while (node && node.nodeType === 1) {
      if (parts.some((p) => matches(node, p))) return node;
      node = node.parentNode;
    }
    return null;
  };
}
if (typeof FakeElement.prototype.getBoundingClientRect !== 'function') {
  FakeElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
    return { left: 100, top: 200, right: 400, bottom: 226, width: 300, height: 26 };
  };
}
if (typeof globalThis.Event !== 'function') {
  globalThis.Event = class Event {
    constructor(type, opts) { this.type = String(type); Object.assign(this, opts || {}); }
  };
}

/* ────────────────────────────────────────────── стенд: DOM, микрофон, сеть */

const PENDING_KEY = 'teamroom.dictation.pending';
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

/** Дождаться условия (таймеры под нагрузкой идут неровно). */
async function waitFor(check, label = '', timeout = 4000) {
  const end = Date.now() + timeout;
  for (;;) {
    const value = check();
    if (value) return value;
    if (Date.now() > end) throw new Error('не дождались: ' + (label || String(check)));
    await tick(15);
  }
}

function installLocalStorage() {
  const data = new Map();
  const ls = {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(String(key), String(value)); },
    removeItem: (key) => { data.delete(String(key)); },
    clear: () => data.clear(),
    get length() { return data.size; },
    raw: data,
  };
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: ls });
  return ls;
}

function installWindowEvents() {
  if (!globalThis.__winHandlers) {
    globalThis.__winHandlers = new Map();
    globalThis.addEventListener = (type, fn) => {
      const list = globalThis.__winHandlers.get(type) || [];
      list.push(fn);
      globalThis.__winHandlers.set(type, list);
    };
    globalThis.removeEventListener = (type, fn) => {
      const list = globalThis.__winHandlers.get(type) || [];
      const index = list.indexOf(fn);
      if (index !== -1) list.splice(index, 1);
    };
  }
  return {
    dispatch(type, ev) {
      for (const fn of globalThis.__winHandlers.get(type) || []) fn(ev || { type });
    },
  };
}

/** Дорожка с событиями, как у настоящего MediaStreamTrack. */
function fakeTrack() {
  const track = {
    kind: 'audio',
    readyState: 'live',
    listeners: Object.create(null),
    addEventListener(type, fn) { (track.listeners[type] || (track.listeners[type] = [])).push(fn); },
    removeEventListener(type, fn) {
      const arr = track.listeners[type] || [];
      const i = arr.indexOf(fn);
      if (i !== -1) arr.splice(i, 1);
    },
    stop() { track.readyState = 'ended'; },
    fire(type) { for (const fn of track.listeners[type] || []) fn({ type, target: track }); },
  };
  return track;
}

function fakeStream() {
  const track = fakeTrack();
  return { track, getTracks() { return [track]; }, getAudioTracks() { return [track]; } };
}

/** Микрофон «отвалился»: устройство исчезло, дорожка закончилась. */
function unplug(stream) {
  stream.track.readyState = 'ended';
  stream.track.fire('ended');
}

/**
 * Рекордер: `stop()` — наш явный стоп (отдаёт кусок), `lose()` — браузер
 * остановил запись сам (микрофон пропал), `lose(false)` — без звука.
 */
class LossRecorder {
  constructor(stream, opts) {
    this.stream = stream;
    this.mimeType = (opts && opts.mimeType) || 'audio/webm;codecs=opus';
    this.state = 'inactive';
    this.ondataavailable = null;
    this.onstop = null;
    this.onerror = null;
    LossRecorder.all.push(this);
  }
  static reset() { LossRecorder.all = []; }
  static isTypeSupported() { return true; }
  static get last() { return LossRecorder.all[LossRecorder.all.length - 1] || null; }
  start() { this.state = 'recording'; }
  emit() {
    if (this.ondataavailable) {
      this.ondataavailable({ data: new Blob([new Uint8Array([1, 2, 3, 4, 5, 6])], { type: this.mimeType }) });
    }
  }
  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    this.emit();
    if (this.onstop) this.onstop();
  }
  /** Микрофон пропал: Chrome сам гасит рекордер и отдаёт что успел. */
  lose(withAudio = true) {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    if (withAudio) this.emit();
    if (this.onstop) this.onstop();
  }
  /** Ошибка записи: рекордер уже неактивен, отдаёт последний кусок и onerror. */
  breakUp() {
    this.state = 'inactive';
    this.emit();
    if (this.onerror) this.onerror({ error: { name: 'NotReadableError' } });
  }
}
LossRecorder.all = [];

function installNetwork(opts = {}) {
  const calls = [];
  let transcribeNo = 0;
  const state = {
    status: Object.assign({ available: true, loaded: true, loading: false,
                            max_recording_s: 600, part_seconds: 120,
                            max_recording_source: 'hermes' }, opts.status || {}),
    texts: opts.texts || ['первая часть', 'вторая часть', 'третья часть'],
    failTranscribe: opts.failTranscribe || null,
    haveParts: opts.haveParts || null,        // Set номеров частей на «сервере»
    deletes: [],
  };
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({ url: u, method });
    if (u.endsWith('/api/stt/status')) return { ok: true, status: 200, json: async () => state.status };
    if (/\/api\/stt\/recordings\/[^/]+\/parts\/\d+$/.test(u) && method === 'PUT') {
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (/\/api\/stt\/recordings\/[^/]+\/parts\/\d+$/.test(u) && method === 'GET') {
      const n = Number(u.split('/').pop());
      if (state.haveParts && !state.haveParts.has(n)) {
        return { ok: false, status: 404, json: async () => ({ error: { code: 'bad_request' } }) };
      }
      return { ok: true, status: 200, blob: async () => new Blob([new Uint8Array([9, 9, 9])],
                                                                 { type: 'audio/webm' }) };
    }
    if (u.endsWith('/api/stt/transcribe')) {
      transcribeNo += 1;
      const n = transcribeNo;
      if (state.failTranscribe && state.failTranscribe(n)) {
        return { ok: false, status: 500,
                 json: async () => ({ error: { code: 'stt_failed', message: 'Модель упала' } }) };
      }
      const text = state.texts[(n - 1) % state.texts.length];
      return { ok: true, status: 200,
               json: async () => ({ text, raw: text, duration_s: 2.0, corrections: [] }) };
    }
    if (/\/api\/stt\/recordings\/[^/]+$/.test(u) && method === 'DELETE') {
      state.deletes.push(u.split('/').pop());
      return { ok: true, status: 200, json: async () => ({ removed: true }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return { calls, state, deletes: state.deletes };
}

function setupDom() {
  const d = createFakeDocument();
  d.head = d.createElement('head');
  d.body = d.createElement('div');
  const host = d.createElement('div');
  d.body.appendChild(host);
  const toasts = d.createElement('div');
  toasts.id = 'toasts';
  d.body.appendChild(toasts);
  d.getElementById = (id) => (toasts.id === id ? toasts : null);
  d.querySelectorAll = (sel) => d.body.querySelectorAll(sel);
  d.querySelector = (sel) => d.body.querySelector(sel);
  d.listeners = Object.create(null);
  d.addEventListener = (type, fn) => { (d.listeners[type] || (d.listeners[type] = [])).push(fn); };
  d.removeEventListener = (type, fn) => {
    const arr = d.listeners[type] || [];
    const i = arr.indexOf(fn);
    if (i !== -1) arr.splice(i, 1);
  };
  d.dispatch = (type, ev) => { for (const fn of d.listeners[type] || []) fn(ev || { type }); };
  globalThis.document = d;
  return { d, host };
}

function textarea(d, value = '') {
  const el = d.createElement('textarea');
  el.value = value;
  el.selectionStart = value.length;
  el.selectionEnd = value.length;
  el.setSelectionRange = (a, b) => { el.selectionStart = a; el.selectionEnd = b; };
  el.focus = () => {};
  return el;
}

function installMic(stream) {
  const used = stream || fakeStream();
  const nav = {
    mediaDevices: {
      getUserMedia: async () => {
        if (nav.__fail) throw nav.__fail;
        return used;
      },
    },
    sendBeacon: () => true,
  };
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: nav });
  LossRecorder.reset();
  globalThis.MediaRecorder = LossRecorder;
  return used;
}

const micRegistry = [];
function makeMic(el, opts) {
  const btn = createMic(el, opts);
  micRegistry.push(btn);
  return btn;
}

function teardown() {
  for (const btn of micRegistry.splice(0)) {
    try { btn.mic.destroy(); } catch { /* уже убран */ }
  }
  // document/localStorage/fetch не убираем: отложенные колбэки (тост живёт 6 с,
  // распознавание может договорить после конца проверки) не должны падать на
  // «нет document». Следующая проверка всё равно ставит своё окружение.
  delete globalThis.MediaRecorder;
  resetSettings();
}

const click = (el) => el.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });

/** Текст строк «есть нерасшифрованная запись» во всех открытых панелях (или ''). */
function recoverText(d) {
  const boxes = d.body.querySelectorAll('.mic-panel__recover');
  return boxes.filter((box) => box && !box.classList.contains('hidden'))
    .map((box) => box.textContent).join(' ');
}

function panelButtons(d) {
  const panel = d.body.querySelector('.mic-panel');
  return panel ? Array.from(panel.querySelectorAll('.btn')) : [];
}

function clickPanel(d, label) {
  const btn = panelButtons(d).find((b) => String(b.textContent).trim().startsWith(label));
  if (!btn) return false;
  click(btn);
  return true;
}

function seedPending(ls, items) {
  ls.setItem(PENDING_KEY, JSON.stringify(items));
}

/* ─────────────────────────────────────────────── 1. микрофон отвалился */

test('микрофон отвалился во время записи: запись останавливается сама, сказанное распознаётся', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ texts: ['наговоренное до обрыва'] });
  const stream = installMic();
  try {
    await clearAll();
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    click(btn);
    await waitFor(() => btn.getAttribute('data-state') === 'recording', 'начала записи');
    assert.deepEqual(listPending(), [], 'пока идёт запись, пустого следа в списке нет');

    const rec = LossRecorder.last;
    assert.ok(rec, 'рекордер запущен');
    rec.lose(true);                 // браузер сам остановил запись
    unplug(stream);                 // и дорожка закончилась

    await waitFor(() => el.value === 'наговоренное до обрыва', 'текста в поле');
    assert.equal(btn.getAttribute('data-state'), 'idle', 'запись закончилась сама');
    assert.deepEqual(listPending(), [], 'следа в списке незаконченных нет');
    const rid = net.deletes[net.deletes.length - 1];
    assert.match(String(rid), /^r_/, 'серверную копию убрали после вставки текста');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('микрофон пропал до первой части: честное сообщение, никакого «Распознать» в пустоту', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  installNetwork();
  const stream = installMic();
  try {
    await clearAll();
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    click(btn);
    await waitFor(() => btn.getAttribute('data-state') === 'recording', 'начала записи');
    LossRecorder.last.lose(false);   // звука не было вовсе
    unplug(stream);

    await waitFor(() => btn.getAttribute('data-state') === 'error', 'честной ошибки');
    const text = d.body.querySelector('.mic-panel').textContent;
    assert.match(text, /микрофон/i, 'сказано про микрофон, а не «речь не обнаружена»');
    assert.deepEqual(listPending(), [], 'пустой «хвост» не остаётся');
    assert.equal(recoverText(d), '', 'строки про нерасшифрованную запись нет');
    assert.equal(clickPanel(d, 'Распознать'), false, 'кнопки «Распознать» нет — распознавать нечего');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('пустая попытка не всплывает при следующем открытии панели (жалоба Михаила)', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  installNetwork();
  const stream = installMic();
  try {
    await clearAll();
    const el = textarea(d, '');
    host.appendChild(el);
    const first = makeMic(el, { partMs: 60000 });
    click(first);
    await tick(40);
    LossRecorder.last.lose(false);
    unplug(stream);
    await tick(120);
    first.mic.destroy();            // панель закрыта

    // Другое поле = другой микрофон = «новое открытие панели».
    const other = textarea(d, '');
    host.appendChild(other);
    const btn = makeMic(other, { partMs: 60000 });
    btn.mic.openPanel();
    await tick(30);
    assert.equal(recoverText(d), '', 'надписи «Есть нерасшифрованная запись» больше нет');
    assert.deepEqual(listPending(), [], 'и в списке пусто — всплыть нечему');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

/* ───────────────────────────────────────────────── 2. «Убрать» — навсегда */

test('«Убрать» убирает навсегда: строка не возвращается, серверная копия удалена', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ haveParts: new Set([1, 2]) });
  installMic();
  const rid = 'r_ghost0001';
  seedPending(ls, [{ rid, at: Date.now(), parts: 2, seconds: 65 }]);
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    btn.mic.openPanel();
    await tick(20);
    const shown = recoverText(d);
    assert.match(shown, /Есть нерасшифрованная запись/);
    assert.match(shown, /1:05/, 'видно длительность: ' + shown);

    assert.equal(clickPanel(d, 'Убрать'), true, 'кнопка «Убрать» есть');
    await tick(60);
    assert.equal(recoverText(d), '', 'строка исчезла сразу');
    assert.deepEqual(listPending(), [], 'список пуст');
    assert.deepEqual(net.deletes, [rid], 'серверная копия удалена');
    assert.equal(clickPanel(d, 'Распознать'), false, 'распознавать больше нечего');

    // Открыли панель ещё раз (как после удачной диктовки) — строки нет.
    btn.mic.openPanel();
    await tick(30);
    assert.equal(recoverText(d), '', 'при следующем открытии надпись НЕ возвращается');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('несколько записей: «Убрать все» убирает разом и ничего не возвращает', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ haveParts: new Set([1, 2]) });
  installMic();
  const now = Date.now();
  seedPending(ls, [{ rid: 'r_first001', at: now - 5000, parts: 1, seconds: 30 },
                   { rid: 'r_second01', at: now, parts: 2, seconds: 65 }]);
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    btn.mic.openPanel();
    await tick(20);
    const shown = recoverText(d);
    assert.match(shown, /2/, 'видно, сколько записей ждёт: ' + shown);

    assert.equal(clickPanel(d, 'Убрать все'), true, 'для нескольких — «Убрать все»');
    await tick(60);
    assert.equal(recoverText(d), '', 'строка исчезла');
    assert.deepEqual(listPending(), [], 'список пуст');
    assert.deepEqual(net.deletes.sort(), ['r_first001', 'r_second01'], 'обе копии на сервере убраны');

    btn.mic.openPanel();
    await tick(30);
    assert.equal(recoverText(d), '', 'надпись не вернулась');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('старые пустые «хвосты» (parts: 0) вычищаются и не показываются', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  installNetwork({ haveParts: new Set([1, 2]) });
  installMic();
  const now = Date.now();
  seedPending(ls, [{ rid: 'r_tail00001', at: now - 1000, parts: 0, seconds: 0 },
                   { rid: 'r_tail00002', at: now - 900, parts: 0, seconds: 0 },
                   { rid: 'r_real00001', at: now - 800, parts: 2, seconds: 40 }]);
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    btn.mic.openPanel();
    await tick(20);
    const shown = recoverText(d);
    assert.match(shown, /0:40/, 'показана только настоящая запись: ' + shown);
    assert.deepEqual(listPending().map((item) => item.rid), ['r_real00001'],
                     'пустые хвосты вычищены из списка');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('запись, которой уже нет на диске, не висит вечно', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ haveParts: new Set([]) });   // части не отдаются
  installMic();
  seedPending(ls, [{ rid: 'r_expired01', at: Date.now(), parts: 2, seconds: 20 }]);
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    btn.mic.openPanel();
    await tick(20);
    assert.equal(clickPanel(d, 'Распознать запись'), true);
    await tick(120);
    const text = d.body.querySelector('.mic-panel').textContent;
    assert.match(text, /удал/i, 'сказано по-человечески, что записи уже нет: ' + text);
    assert.deepEqual(listPending(), [], 'строка убрана — второй раз не всплывёт');
    assert.ok(net.deletes.includes('r_expired01'), 'серверную копию тоже убрали');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

/* ───────────────────────────────────────── 3. удачная диктовка убирает всё */

test('удачная диктовка убирает и след, и серверную копию', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ texts: ['готово'] });
  installMic();
  try {
    await clearAll();
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    click(btn);
    await waitFor(() => btn.getAttribute('data-state') === 'recording', 'начала записи');
    assert.deepEqual(listPending(), [], 'пока идёт запись, следа нет (появится с первой частью звука)');
    clickPanel(d, 'Стоп');
    await waitFor(() => el.value === 'готово', 'текста в поле');
    await waitFor(() => listPending().length === 0, 'уборки следа');
    assert.equal(net.deletes.length, 1, 'серверная копия убрана: ' + JSON.stringify(net.deletes));
    assert.match(net.deletes[0], /^r_/);
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

/* ──────────────────────────────── 4. старый rid не путается с новым */
test('неудачное восстановление не теряет свою запись и не портит новую', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ haveParts: new Set([1, 2]), failTranscribe: () => true });
  installMic();
  const rid = 'r_old0000001';
  seedPending(ls, [{ rid, at: Date.now() - 60000, parts: 2, seconds: 40 }]);
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    btn.mic.openPanel();
    await waitFor(() => recoverText(d).includes('нерасшифрованная'), 'строки о записи');
    assert.equal(clickPanel(d, 'Распознать запись'), true);
    await waitFor(() => btn.getAttribute('data-state') === 'error', 'ошибки распознавания');
    const mine = listPending().find((item) => item.rid === rid);
    assert.ok(mine, 'неудачная запись осталась в списке');
    assert.equal(mine.parts, 2, 'и помнит свои части');

    // Новая попытка записи: сначала закрываем панель ошибки, у новой свой rid.
    assert.equal(clickPanel(d, 'Закрыть'), true, 'панель можно закрыть');
    await waitFor(() => btn.getAttribute('data-state') === 'idle', 'закрытия панели');
    click(btn);
    await waitFor(() => btn.getAttribute('data-state') === 'recording', 'новой записи');
    clickPanel(d, 'Стоп');
    await waitFor(() => listPending().length === 2, 'двух записей в списке');
    const rids = listPending().map((item) => item.rid);
    assert.equal(new Set(rids).size, 2, 'у новой записи свой rid: ' + JSON.stringify(rids));
    assert.ok(rids.includes(rid), 'старая на месте');
    assert.equal(listPending().find((item) => item.rid === rid).parts, 2, 'старые части целы');
    // Дать очереди распознавания закончить, чтобы после проверки не осталось
    // отложенной работы (иначе node --test считает это «активностью после теста»).
    await waitFor(() => btn.mic.parts().every((job) => job.state === 'done'
      || job.state === 'failed'), 'окончания распознавания');
    btn.mic.cancel();
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

/* ───────────────── 5. во время записи в другом поле ничего не предлагаем */

test('пока идёт запись, вторая панель не предлагает «распознать» её части', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork();
  installMic();
  try {
    await clearAll();
    const first = textarea(d, '');
    host.appendChild(first);
    const recording = makeMic(first, { partMs: 60 });   // часть закроется сразу
    click(recording);
    await waitFor(() => recording.getAttribute('data-state') === 'recording', 'записи');
    await waitFor(() => listPending().length === 1, 'первой части на диске');

    const second = textarea(d, '');
    host.appendChild(second);
    const other = makeMic(second, { partMs: 60000 });
    other.mic.openPanel();
    await tick(50);
    assert.equal(recoverText(d), '',
                 'чужую незаконченную запись не предлагают, пока она пишется: ' + recoverText(d));

    recording.mic.cancel();
    recording.mic.destroy();
    other.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

/* ─────────────────────── 6. ошибка записи не дублирует сказанное */
test('ошибка рекордера не распознаёт одно и то же аудио дважды', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ texts: ['единственный раз'] });
  installMic();
  try {
    await clearAll();
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    click(btn);
    await waitFor(() => btn.getAttribute('data-state') === 'recording', 'начала записи');
    LossRecorder.last.breakUp();       // микрофон отказал на середине
    await waitFor(() => btn.getAttribute('data-state') === 'error', 'ошибки записи');
    await waitFor(() => btn.mic.parts().every((job) => job.state !== 'new'), 'очереди');
    await tick(60);
    const parts = btn.mic.parts();
    assert.equal(parts.length, 1, 'часть одна, а не две: ' + JSON.stringify(parts));
    const transcribes = net.calls.filter((c) => c.url.endsWith('/api/stt/transcribe')).length;
    assert.equal(transcribes, 1, 'распознавание одно: ' + transcribes);
    assert.equal(el.value, 'единственный раз', el.value);
    assert.deepEqual(listPending(), [], 'след убран — текст в поле');
    btn.mic.cancel();
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});
