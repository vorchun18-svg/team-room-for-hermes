/**
 * dictation-long.test.mjs — длинная диктовка: лимит из настроек Hermes, части
 * записи, авто-распознавание при лимите, обрыв страницы и повтор.
 *
 * Что проверяем (замечание 2 из Defects-2026-11-04):
 *   1. лимит записи берётся из `/api/stt/status` (он читает config.yaml Hermes),
 *      меньше 10 минут не опускается и не «зашит» в интерфейсе;
 *   2. длинная запись режется на части, каждая уходит на сервер и распознаётся
 *      отдельно, текст собирается по порядку и ничего не теряется;
 *   3. достигли лимита — запись сама уходит на распознавание;
 *   4. обрыв сети и повтор: часть помечается, остальные не страдают,
 *      «Распознать ещё раз» дописывает недостающее без дублей;
 *   5. закрытие страницы (pagehide) НЕ выбрасывает запись: маячок на сервер +
 *      след в списке незаконченных; позже её можно распознать.
 *
 * DOM — минимальный tests/fake-dom.mjs (как в dictation.test.mjs).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createFakeDocument, FakeElement } from './fake-dom.mjs';
import {
  createMic, settingsFromStatus, recordSettings, resetSettings,
  MIN_RECORD_MS, MAX_RECORD_MS, DEFAULT_PART_MS,
} from '../web/js/dictation.js';
import { listPending, getPart } from '../web/js/dictation-store.js';

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

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

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
  // Слушатели живут на одном общем реестре: bindDoc() привязывается один раз
  // на всё приложение, и подменять его между тестами нельзя.
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
    count(type) {
      return (globalThis.__winHandlers.get(type) || []).length;
    },
  };
}

function fakeStream() {
  const track = { kind: 'audio', stopped: false, stop() { this.stopped = true; } };
  return { track, getTracks() { return [track]; }, getAudioTracks() { return [track]; } };
}

/** Рекордер, который на каждый stop отдаёт очередной кусок аудио. */
class PartRecorder {
  constructor(stream, opts) {
    this.stream = stream;
    this.mimeType = (opts && opts.mimeType) || 'audio/webm;codecs=opus';
    this.state = 'inactive';
    this.ondataavailable = null;
    this.onstop = null;
    this.onerror = null;
    this.timeslice = 0;
    PartRecorder.all.push(this);
  }
  static reset() { PartRecorder.all = []; }
  static isTypeSupported(type) { return String(type) === 'audio/webm;codecs=opus'; }
  start(ms) { this.state = 'recording'; this.timeslice = ms; }
  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    const chunk = new Blob([new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8])],
                           { type: this.mimeType });
    if (this.ondataavailable) this.ondataavailable({ data: chunk });
    if (this.onstop) this.onstop();
  }
}
PartRecorder.all = [];

/**
 * Сеть: настраиваемые ответы.
 *   opts.status   — ответ /api/stt/status
 *   opts.texts    — что отвечает распознавание по порядку
 *   opts.failPart — номер распознавания (с 1), на котором сервер «падает»
 */
function installNetwork(opts = {}) {
  const calls = [];
  let transcribeNo = 0;
  const state = {
    status: Object.assign({ available: true, loaded: true, loading: false,
                            max_recording_s: 600, part_seconds: 120,
                            max_recording_source: 'hermes' }, opts.status || {}),
    texts: opts.texts || ['первая часть', 'вторая часть', 'третья часть', 'ещё часть'],
    failTranscribe: opts.failTranscribe || null,   // function(n) → true
    offline: false,
  };
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({ url: u, method, body: init.body, ctype: (init.headers || {})['Content-Type'] });
    if (state.offline) {
      const err = new TypeError('Failed to fetch');
      throw err;
    }
    if (u.endsWith('/api/stt/status')) {
      return { ok: true, status: 200, json: async () => state.status };
    }
    if (/\/api\/stt\/recordings\/[^/]+\/parts\/\d+$/.test(u) && method === 'PUT') {
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (/\/api\/stt\/recordings\/[^/]+\/parts\/\d+$/.test(u) && method === 'GET') {
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
      return { ok: true, status: 200, json: async () => ({ removed: true }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return {
    calls, state,
    beacons: [],
  };
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
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      mediaDevices: { getUserMedia: async () => used },
      sendBeacon: (url, data) => {
        globalThis.__beacons = globalThis.__beacons || [];
        globalThis.__beacons.push({ url: String(url), data });
        return true;
      },
    },
  });
  PartRecorder.reset();
  globalThis.MediaRecorder = PartRecorder;
  globalThis.__beacons = [];
  return used;
}

function teardown() {
  for (const btn of micRegistry.splice(0)) {
    try { btn.mic.destroy(); } catch { /* уже убран */ }
  }
  delete globalThis.MediaRecorder;
  delete globalThis.fetch;
  delete globalThis.document;
  delete globalThis.__beacons;
  resetSettings();
}

const click = (btn) => btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });

/** Микрофон, который teardown() уберёт даже при упавшей проверке. */
const micRegistry = [];
function makeMic(el, opts) {
  const btn = createMic(el, opts);
  micRegistry.push(btn);
  return btn;
}

/* ────────────────────────────────────────────── лимит из настроек Hermes */

test('лимит записи берётся из настроек Hermes, а не из своей константы', () => {
  const next = settingsFromStatus({ max_recording_s: 900, part_seconds: 90,
                                    max_recording_source: 'hermes',
                                    max_recording_hermes_s: 900 });
  assert.equal(next.maxMs, 900 * 1000);
  assert.equal(next.partMs, 90 * 1000);
  assert.equal(next.source, 'hermes');
  assert.equal(next.hermesS, 900);

  // В Hermes стоит 120 с (по умолчанию) — ниже 10 минут лимит не опускаем.
  const short = settingsFromStatus({ max_recording_s: 120, max_recording_hermes_s: 120 });
  assert.equal(short.maxMs, MIN_RECORD_MS);
  assert.equal(short.hermesS, 120);

  // Сервер промолчал — остаётся прежнее значение, а не «пусто».
  const kept = settingsFromStatus({}, { maxMs: 700000, partMs: 60000, source: 'hermes' });
  assert.equal(kept.maxMs, 700000);
  assert.equal(kept.partMs, 60000);

  // Совсем пусто — запасные 10 минут и части по 2 минуты.
  const fallback = settingsFromStatus(null, { maxMs: MAX_RECORD_MS, partMs: DEFAULT_PART_MS,
                                              source: 'fallback' });
  assert.equal(fallback.maxMs, MAX_RECORD_MS);
  assert.equal(fallback.partMs, DEFAULT_PART_MS);
  assert.equal(fallback.hermesS, null);

  // Часть короче 10 секунд не бывает, длиннее лимита — тоже.
  assert.equal(settingsFromStatus({ max_recording_s: 600, part_seconds: 1 }).partMs, 10000);
  assert.equal(settingsFromStatus({ max_recording_s: 600, part_seconds: 9000 }).partMs, 600000);
});

test('панель показывает лимит из настроек Hermes (15:00), а не 10:00', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  installNetwork({ status: { max_recording_s: 900, part_seconds: 300 } });
  installMic();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el);
    click(btn);
    await tick(30);
    assert.equal(btn.getAttribute('data-state'), 'recording');
    assert.equal(btn.mic.settings().maxMs, 900 * 1000, 'лимит 15 минут из настроек');
    const panel = d.body.querySelector('.mic-panel');
    assert.ok(panel.textContent.includes('/ 15:00'), panel.textContent);
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('recordSettings() отдаёт применённые значения (для подписи в настройках)', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  installNetwork({ status: { max_recording_s: 720, part_seconds: 240, max_recording_source: 'hermes' } });
  installMic();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el);
    click(btn);
    await tick(30);
    assert.equal(recordSettings().maxMs, 720 * 1000);
    btn.mic.cancel();
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

/* ─────────────────────────────────── части записи и распознавание */

test('длинная запись режется на части: каждая сохранена и распознана, текст по порядку', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  const net = installNetwork({ texts: ['раз', 'два', 'три', 'четыре', 'пять'] });
  installMic();
  try {
    const el = textarea(d, 'Проверь ');
    host.appendChild(el);
    el.selectionStart = el.selectionEnd = 8;
    const btn = makeMic(el, { partMs: 120 });   // часть записи 120 мс (в бою — 2 минуты)
    click(btn);
    await tick(30);
    assert.equal(btn.getAttribute('data-state'), 'recording');

    await tick(420);                              // ~3 части записи
    const during = btn.mic.parts();
    assert.ok(during.length >= 2, 'запись разрезана на части: ' + during.length);
    assert.equal(el.value, 'Проверь ', 'во время записи текст в поле не лезет');

    const puts = net.calls.filter((c) => c.method === 'PUT'
      && /\/api\/stt\/recordings\/[^/]+\/parts\/\d+$/.test(c.url));
    assert.equal(puts.length, during.length, 'каждая часть сразу ушла на сервер');
    const rid = puts[0].url.split('/')[4];
    assert.match(rid, /^r_[a-z0-9]+$/, 'номер записи в формате сервера');

    click(btn);                                   // Стоп
    await tick(150);
    const parts = btn.mic.parts();
    assert.equal(parts.filter((p) => p.state === 'done').length, parts.length);
    assert.equal(el.value, 'Проверь раз два три четыре пять'.slice(0, el.value.length));
    assert.ok(el.value.startsWith('Проверь раз'), el.value);
    assert.equal(btn.getAttribute('data-state'), 'idle');
    assert.deepEqual(listPending(), [], 'после успеха незаконченных записей нет');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('лимит достигнут — запись сама уходит на распознавание (не выбрасывается)', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  installNetwork({ texts: ['готовая расшифровка'] });
  installMic();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { maxMs: 300, partMs: 60000 });
    click(btn);
    await tick(900);                              // тик 250 мс → авто-стоп на 300 мс
    assert.equal(btn.getAttribute('data-state'), 'idle', 'после лимита запись закрыта');
    assert.equal(el.value, 'готовая расшифровка', 'текст вставлен автоматически');
    assert.deepEqual(listPending(), []);
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('обрыв сети: части не теряются, «Распознать ещё раз» дописывает недостающее', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  const net = installNetwork({
    texts: ['первая', 'вторая', 'третья'],
    failTranscribe: (n) => n === 2,        // вторая часть «падает»
  });
  installMic();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60 });
    click(btn);
    await tick(260);
    click(btn);                            // Стоп → 3–4 части, вторая падает
    await tick(300);

    assert.equal(btn.getAttribute('data-state'), 'error');
    const panel = d.body.querySelector('.mic-panel');
    assert.ok(panel.textContent.includes('Распознать ещё раз'), panel.textContent);
    const parts = btn.mic.parts();
    assert.equal(parts.filter((p) => p.state === 'failed').length, 1, 'упала ровно одна часть');
    assert.ok(el.value.length > 0, 'распознанное не потеряно');

    net.state.failTranscribe = null;       // сервер ожил
    const before = el.value;
    btn.mic.retry();
    await tick(300);
    assert.equal(btn.getAttribute('data-state'), 'idle');
    assert.equal(btn.mic.parts().filter((p) => p.state === 'failed').length, 0);
    assert.ok(el.value.length > before.length, 'недостающая часть дописана');
    assert.equal(el.value.split('вторая').length - 1, 1, 'без дублей');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

/* ─────────────────────────────────── закрытие страницы и восстановление */

test('pagehide во время записи ничего не выбрасывает: маячок на сервер и след в списке', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  const net = installNetwork();
  installMic();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    click(btn);
    await tick(30);
    assert.equal(btn.getAttribute('data-state'), 'recording');

    // В настоящем Chromium каждая секунда записи даёт кусок; здесь — один.
    PartRecorder.all[0].ondataavailable({ data: new Blob([new Uint8Array([1, 2, 3])],
                                                          { type: 'audio/webm' }) });
    windowEvents.dispatch('pagehide');
    await tick(20);

    assert.equal(btn.getAttribute('data-state'), 'idle');
    const beacons = globalThis.__beacons || [];
    assert.equal(beacons.length, 1, 'аудио ушло маячком');
    assert.match(beacons[0].url, /\/api\/stt\/recordings\/r_[a-z0-9]+\/parts\/1$/);
    assert.ok(beacons[0].data instanceof Blob);
    const pending = listPending();
    assert.equal(pending.length, 1, 'запись запомнена как незаконченная');
    assert.match(pending[0].rid, /^r_[a-z0-9]+$/);
    assert.equal(net.calls.filter((c) => c.method === 'DELETE').length, 0,
      'страница не удаляет наговоренное');
    btn.mic.destroy();
  } finally {
    teardown();
  }
});

test('незаконченную запись можно распознать позже: части с сервера → текст в поле', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  const ls = installLocalStorage();
  const net = installNetwork({ texts: ['первая половина', 'вторая половина'] });
  installMic();
  const rid = 'r_recover001';
  const at = Date.now();
  ls.setItem('teamroom.dictation.pending',
             JSON.stringify([{ rid, at, parts: 2, seconds: 65 }]));
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });

    btn.mic.openPanel();
    const panel = d.body.querySelector('.mic-panel');
    assert.ok(panel.textContent.includes('Есть нерасшифрованная запись'),
              'в панели видно, что есть нерасшифрованное');
    const recoverBtn = Array.from(panel.querySelectorAll('.btn'))
      .find((b) => String(b.textContent).includes('Распознать запись'));
    assert.ok(recoverBtn, 'кнопка «Распознать запись» есть');
    recoverBtn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(200);

    assert.equal(el.value, 'первая половина вторая половина', el.value);
    assert.equal(net.calls.filter((c) => c.method === 'GET').length, 2, 'части взяты с сервера');
    assert.deepEqual(listPending(), [], 'запись больше не висит в списке');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('части пишутся и в IndexedDB-хранилище (локальная копия на случай обрыва)', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  installNetwork();
  installMic();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 80 });
    click(btn);
    await tick(200);
    const rid = btn.mic.parts().length ? null : null;
    const pending = listPending();
    assert.equal(pending.length, 1);
    const blob = await getPart(pending[0].rid, 1);
    assert.ok(blob, 'часть лежит локально, даже если сервер недоступен');
    btn.mic.cancel();
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});

test('отмена (Esc) — единственный случай, когда запись убирается совсем', async () => {
  const { d, host } = setupDom();
  const windowEvents = installWindowEvents();
  installLocalStorage();
  const net = installNetwork();
  installMic();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = makeMic(el, { partMs: 60000 });
    click(btn);
    await tick(30);
    d.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
    await tick(30);
    assert.equal(btn.getAttribute('data-state'), 'idle');
    assert.deepEqual(listPending(), []);
    assert.ok(net.calls.some((c) => c.method === 'DELETE'), 'сервер попросили убрать запись');
    assert.equal(el.value, '');
    btn.mic.destroy();
  } finally {
    windowEvents.dispatch('pagehide');
    teardown();
  }
});
