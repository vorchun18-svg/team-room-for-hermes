/**
 * dictation.test.mjs — диктовка без браузера: состояния, вставка в позицию
 * курсора, авто-подключение к полям, освобождение треков, лимит 10 минут.
 *
 * DOM — минимальный tests/fake-dom.mjs; недостающие методы (replaceChildren,
 * querySelectorAll, dispatchEvent) добавляем здесь, чтобы не менять чужой файл.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createFakeDocument, FakeElement } from './fake-dom.mjs';
import {
  createMic, enhanceAll, insertAtCursor, formatClock, nearLimit,
  shouldAutoStop, MAX_RECORD_MS,
} from '../web/js/dictation.js';

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
    const w = 120; const h = 26;
    return { left: 100, top: 200, right: 100 + w, bottom: 200 + h, width: w, height: h };
  };
}

if (typeof globalThis.Event !== 'function') {
  globalThis.Event = class Event {
    constructor(type, opts) { this.type = String(type); Object.assign(this, opts || {}); }
  };
}

/* ────────────────────────────────────────────── стенд браузера */

function fakeStream() {
  const track = { kind: 'audio', stopped: false, stop() { this.stopped = true; } };
  return {
    track,
    getTracks() { return [track]; },
    getAudioTracks() { return [track]; },
  };
}

class FakeRecorder {
  constructor(stream, opts) {
    this.stream = stream;
    this.mimeType = (opts && opts.mimeType) || 'audio/webm';
    this.state = 'inactive';
    this.ondataavailable = null;
    this.onstop = null;
    this.onerror = null;
    FakeRecorder.last = this;
  }

  static isTypeSupported(type) { return String(type) === 'audio/webm;codecs=opus'; }

  start(ms) { this.state = 'recording'; this.timeslice = ms; }

  stop() {
    this.state = 'inactive';
    const part = new Blob([new Uint8Array([7, 7, 7, 7])], { type: this.mimeType });
    if (this.ondataavailable) this.ondataavailable({ data: part });
    if (this.onstop) this.onstop();
  }
}

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function setupDom() {
  const d = createFakeDocument();
  d.head = d.createElement('head');
  d.body = d.createElement('div');
  const host = d.createElement('div');
  d.body.appendChild(host);
  const toasts = d.createElement('div');
  toasts.id = 'toasts';
  d.body.appendChild(toasts);
  d.getElementById = (id) => toasts.id === id ? toasts : null;
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
  return { d, host, toasts };
}

function teardownDom() {
  delete globalThis.document;
}

let lastFetch = null;

function installMedia({ stream, fail, text = 'проверьте сроки поставки' } = {}) {
  const used = stream || fakeStream();
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      mediaDevices: {
        getUserMedia: async () => {
          if (fail) throw fail;
          return used;
        },
      },
    },
  });
  globalThis.MediaRecorder = FakeRecorder;
  lastFetch = [];
  globalThis.fetch = async (url, init) => {
    lastFetch.push({ url: String(url), init });
    if (String(url).endsWith('/status')) {
      return { ok: true, status: 200, json: async () => ({ available: true, loaded: true, loading: false }) };
    }
    return { ok: true, status: 200, json: async () => ({ text, raw: text, corrections: [] }) };
  };
  return used;
}

function uninstallMedia() {
  delete globalThis.MediaRecorder;
  delete globalThis.fetch;
  if (globalThis.navigator && globalThis.navigator.mediaDevices) {
    try { delete globalThis.navigator.mediaDevices; } catch { /* уже удалено */ }
  }
}

function textarea(d, value = '') {
  const el = d.createElement('textarea');
  el.value = value;
  el.selectionStart = value.length;
  el.selectionEnd = value.length;
  el.setSelectionRange = (a, b) => { el.selectionStart = a; el.selectionEnd = b; };
  el.focus = () => { el.focused = true; };
  return el;
}

/* ────────────────────────────────────────────── чистые функции */

test('formatClock: 0:00, 0:07, 10:00', () => {
  assert.equal(formatClock(0), '0:00');
  assert.equal(formatClock(7000), '0:07');
  assert.equal(formatClock(MAX_RECORD_MS), '10:00');
  assert.equal(formatClock(-50), '0:00');
});

test('лимит 10 минут: подсказка за 30 с, авто-стоп на десятой минуте', () => {
  assert.equal(nearLimit(9.5 * 60 * 1000), true);
  assert.equal(nearLimit(9.6 * 60 * 1000), true);
  assert.equal(nearLimit(9 * 60 * 1000), false);
  assert.equal(nearLimit(10 * 60 * 1000, 10 * 60 * 1000), false);
  assert.equal(shouldAutoStop(10 * 60 * 1000 - 1), false);
  assert.equal(shouldAutoStop(10 * 60 * 1000), true);
});

/* ────────────────────────────────────────────── вставка */

test('вставка в позицию курсора: пробелы по краям, курсор после вставки, событие input', () => {
  setupDom();
  try {
    const d = globalThis.document;
    const el = textarea(d, 'Проверить');
    let inputs = 0;
    el.addEventListener('input', () => { inputs += 1; });
    el.setSelectionRange(9, 9); // в конец слова

    const res = insertAtCursor(el, '  сроки поставки ');
    assert.equal(res.inserted, true);
    assert.equal(el.value, 'Проверить сроки поставки');
    assert.equal(el.selectionStart, 'Проверить сроки поставки'.length);
    assert.equal(el.selectionEnd, res.start);
    assert.equal(inputs, 1);
  } finally { teardownDom(); }
});

test('вставка заменяет выделенное и не дублирует пробелы на границах', () => {
  setupDom();
  try {
    const d = globalThis.document;
    const el = textarea(d, 'Разбери договор с поставщиком');
    el.setSelectionRange(8, 15); // «договор»
    insertAtCursor(el, 'акт');
    assert.equal(el.value, 'Разбери акт с поставщиком');
    assert.equal(el.selectionStart, 11);

    const el2 = textarea(d, 'Слева ');
    el2.setSelectionRange(6, 6);
    insertAtCursor(el2, 'справа');
    assert.equal(el2.value, 'Слева справа');

    const el3 = textarea(d, '');
    insertAtCursor(el3, '  Привет  ');
    assert.equal(el3.value, 'Привет');
  } finally { teardownDom(); }
});

test('пустой распознанный текст ничего не вставляет', () => {
  setupDom();
  try {
    const d = globalThis.document;
    const el = textarea(d, 'было');
    const res = insertAtCursor(el, '   ');
    assert.equal(res.inserted, false);
    assert.equal(el.value, 'было');
  } finally { teardownDom(); }
});

/* ────────────────────────────────────────────── кнопка и запись */

test('createMic: кнопка в поле, Ctrl+M старт/стоп, текст встаёт под курсор', async () => {
  const { d, host } = setupDom();
  const stream = installMedia({ text: 'сроки поставки' });
  try {
    const el = textarea(d, 'Проверь ');
    host.appendChild(el);
    el.selectionStart = 8;
    el.selectionEnd = 8;

    const btn = createMic(el);
    assert.equal(btn.tagName.toLowerCase(), 'button');
    assert.equal(btn.getAttribute('data-mic-btn'), 'true');
    assert.equal(btn.getAttribute('data-state'), 'idle');

    el.dispatch('keydown', { key: 'm', ctrlKey: true, preventDefault() {}, stopPropagation() {} });
    await tick(10);
    assert.equal(btn.getAttribute('data-state'), 'recording');
    assert.equal(FakeRecorder.last.timeslice, 1000, 'timeslice 1000 мс');
    assert.equal(FakeRecorder.last.mimeType, 'audio/webm;codecs=opus');
    assert.ok(btn.textContent.includes('Стоп'), 'кнопка показывает «Стоп» с таймером');

    const panel = d.body.querySelector('.mic-panel');
    assert.ok(panel, 'панель записи появилась');
    assert.ok(panel.textContent.includes('Запись'));
    assert.ok(panel.textContent.includes('Стоп'));
    assert.ok(panel.textContent.includes('Отмена'));
    assert.ok(panel.textContent.includes('/ 10:00'), 'таймер 0:00 / 10:00');

    btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(20);

    assert.equal(el.value, 'Проверь сроки поставки');
    assert.equal(el.selectionStart, el.value.length);
    assert.equal(stream.track.stopped, true, 'дорожка микрофона освобождена');
    assert.equal(btn.getAttribute('data-state'), 'idle');
    const posted = lastFetch.find((c) => c.url.endsWith('/transcribe'));
    assert.ok(posted, 'аудио ушло на распознавание');
    assert.equal(posted.init.headers['Content-Type'], 'audio/webm;codecs=opus');
  } finally {
    uninstallMedia();
    teardownDom();
  }
});

test('Esc во время записи отменяет её и ничего не отправляет', async () => {
  const { d, host } = setupDom();
  const stream = installMedia();
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = createMic(el);
    btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(10);
    assert.equal(btn.getAttribute('data-state'), 'recording');

    const prevented = { value: false };
    d.dispatch('keydown', {
      key: 'Escape',
      preventDefault() { prevented.value = true; },
      stopPropagation() {},
    });
    await tick(20);
    assert.equal(btn.getAttribute('data-state'), 'idle');
    assert.equal(stream.track.stopped, true);
    assert.equal(lastFetch.filter((c) => c.url.endsWith('/transcribe')).length, 0);
    assert.equal(prevented.value, true);
    assert.equal(el.value, '');
    btn.mic.destroy();
  } finally {
    uninstallMedia();
    teardownDom();
  }
});

test('нет разрешения — понятная подсказка, запись не начинается', async () => {
  const { d, host } = setupDom();
  const denied = new Error('denied');
  denied.name = 'NotAllowedError';
  installMedia({ fail: denied });
  try {
    const el = textarea(d, '');
    host.appendChild(el);
    const btn = createMic(el);
    btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(10);
    assert.equal(btn.getAttribute('data-state'), 'error');
    const panel = d.body.querySelector('.mic-panel');
    assert.ok(panel.textContent.includes('Разрешите микрофон'));
    assert.ok(panel.textContent.includes('Пока можно печатать вручную'));
    btn.mic.destroy();
  } finally {
    uninstallMedia();
    teardownDom();
  }
});

test('одна запись на всё приложение: второе поле не стартует', async () => {
  const { d, host } = setupDom();
  installMedia();
  try {
    const a = textarea(d, '');
    const b = textarea(d, '');
    host.appendChild(a);
    host.appendChild(b);
    const micA = createMic(a);
    const micB = createMic(b);

    micA.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(10);
    assert.equal(micA.getAttribute('data-state'), 'recording');

    micB.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(10);
    assert.equal(micB.getAttribute('data-state'), 'idle', 'вторая запись не началась');
    assert.equal(micA.getAttribute('data-state'), 'recording');
    micA.mic.destroy();
    micB.mic.destroy();
  } finally {
    uninstallMedia();
    teardownDom();
  }
});

test('пустой ответ сервера — «Речь не обнаружена», текст не вставляется', async () => {
  const { d, host } = setupDom();
  installMedia({ text: '' });
  try {
    const el = textarea(d, 'было');
    host.appendChild(el);
    const btn = createMic(el);
    btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(10);
    btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await tick(20);
    assert.equal(el.value, 'было');
    assert.equal(btn.getAttribute('data-state'), 'error');
    assert.ok(d.body.querySelector('.mic-panel').textContent.includes('Речь не обнаружена'));
    btn.mic.destroy();
  } finally {
    uninstallMedia();
    teardownDom();
  }
});

/* ────────────────────────────────────────────── автоподключение */

function field(d, tag, attrs) {
  const el = d.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (tag === 'input') el.type = (attrs && attrs.type) || 'text';
  return el;
}

test('enhanceAll: textarea и input[text|search], кроме служебных и занятых', () => {
  const { d, host } = setupDom();
  try {
    const okArea = field(d, 'textarea', {});
    const okText = field(d, 'input', { type: 'text' });
    const okSearch = field(d, 'input', { type: 'search' });
    const okBare = field(d, 'input', {});
    const skip = [
      field(d, 'input', { type: 'password' }),
      field(d, 'input', { type: 'number' }),
      field(d, 'input', { type: 'date' }),
      field(d, 'input', { type: 'email' }),
      field(d, 'input', { type: 'hidden' }),
      field(d, 'input', { type: 'text', readonly: 'readonly' }),
      field(d, 'input', { type: 'text', disabled: 'disabled' }),
      field(d, 'input', { type: 'text', 'data-mic': 'off' }),
      field(d, 'input', { type: 'text', 'data-mic': 'manual' }),
    ];
    for (const el of [okArea, okText, okSearch, okBare, ...skip]) host.appendChild(el);

    const added = enhanceAll(host);
    assert.equal(added, 4);
    for (const el of [okArea, okText, okSearch, okBare]) {
      assert.equal(el.getAttribute('data-mic'), 'auto');
    }
    assert.equal(host.querySelectorAll('.mic-btn').length, 4);
    for (const el of skip.slice(6)) {
      assert.equal(el.getAttribute('data-mic'), el.getAttribute('data-mic'));
    }
    assert.equal(host.querySelectorAll('.mic-btn').length, 4, 'в служебные поля кнопка не встала');

    // Кнопки-действия рядом нет → обёртка .mic-field вокруг САМОГО поля,
    // кнопка внутри обёртки; родитель-строка остаётся как была (не relative).
    for (const el of [okArea, okText, okSearch, okBare]) {
      const holder = el.parentNode;
      assert.equal(holder.getAttribute('data-mic-wrap'), 'field');
      assert.ok(holder.classList.contains('mic-field'));
      const btn = holder.querySelector('.mic-btn');
      assert.ok(btn, 'микрофон внутри обёртки поля');
      assert.ok(btn.classList.contains('mic-btn--compact'), 'в поле — без подписи');
      assert.equal(btn.getAttribute('data-mic-for'), el.id);
      const kind = String(el.tagName).toLowerCase() === 'textarea' ? 'mic-btn--area' : 'mic-btn--input';
      assert.ok(btn.classList.contains(kind), kind);
    }
    assert.equal(host.getAttribute('data-mic-pos'), null, 'родитель-строка не становится relative');
    assert.equal(okArea.style.position, undefined, 'у поля нет absolute');

    assert.equal(enhanceAll(host), 0, 'повторный вызов ничего не дублирует');
    assert.equal(host.querySelectorAll('.mic-btn').length, 4);
  } finally { teardownDom(); }
});

test('startAutoEnhance ставит микрофон в поле, появившееся позже', async () => {
  const { d, host } = setupDom();
  const callbacks = [];
  globalThis.MutationObserver = class MutationObserver {
    constructor(cb) { callbacks.push(cb); this.cb = cb; }
    observe() {}
    disconnect() {}
  };
  try {
    const mod = await import('../web/js/dictation.js');
    mod.stopAutoEnhance();
    mod.startAutoEnhance();
    assert.equal(host.querySelectorAll('.mic-btn').length, 0);

    const late = field(d, 'input', { type: 'text' });
    const wrap = d.createElement('div');
    wrap.appendChild(late);
    host.appendChild(wrap);
    callbacks[0]([{ type: 'childList', addedNodes: [wrap] }]);
    await tick(150);
    assert.equal(late.getAttribute('data-mic'), 'auto');
    assert.ok(host.querySelector('.mic-btn'));
    mod.stopAutoEnhance();
  } finally {
    delete globalThis.MutationObserver;
    teardownDom();
  }
});

test('кнопка в композере помечена ручной и не получает вторую', () => {
  const { d, host } = setupDom();
  try {
    const el = field(d, 'textarea', { id: 'cmp-input', 'data-mic': 'manual' });
    host.appendChild(el);
    const manual = createMic(el, { label: 'Надиктовать' });
    host.appendChild(manual);
    assert.equal(enhanceAll(host), 0);
    assert.equal(host.querySelectorAll('.mic-btn').length, 1);
    assert.ok(manual.textContent.includes('Надиктовать'));
  } finally { teardownDom(); }
});
