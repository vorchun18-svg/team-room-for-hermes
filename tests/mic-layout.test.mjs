/**
 * mic-layout.test.mjs — ОДНО правило размещения микрофона во всём интерфейсе
 * (docs/DESIGN-NOTES.md, «Единые компоненты»):
 *
 *   • рядом с полем в той же строке есть кнопка-действие («Ответить»,
 *     «Добавить», «Отправить») → микрофон отдельная кнопка ПЕРЕД ней;
 *   • рядом кнопки нет → поле в обёртке .mic-field, кнопка внутри обёртки;
 *   • нигде нет absolute у родителя-строки — налезть на «Ответить» нечем.
 *
 * DOM — tests/fake-dom.mjs; недостающее добавляем здесь, чужой файл не правим.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createFakeDocument, FakeElement } from './fake-dom.mjs';
import {
  attachMic, enhanceAll, eligible, placement, actionSibling, wantLabel,
  noMicPlace, LABEL_MIN_WIDTH,
} from '../web/js/dictation-auto.js';
import { createMic } from '../web/js/dictation.js';

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
  if (s.includes(',')) return false;
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
    const w = Number(this.__width) || 120;
    const h = 26;
    return { left: 100, top: 200, right: 100 + w, bottom: 200 + h, width: w, height: h };
  };
}

if (typeof globalThis.Event !== 'function') {
  globalThis.Event = class Event {
    constructor(type, opts) { this.type = String(type); Object.assign(this, opts || {}); }
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
  d.removeEventListener = () => {};
  globalThis.document = d;
  return { d, host };
}

function teardownDom() {
  delete globalThis.document;
  delete globalThis.getComputedStyle;
}

function el(d, tag, attrs, text) {
  const node = d.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (text) node.textContent = text;
  if (tag === 'input') node.type = (attrs && attrs.type) || 'text';
  return node;
}

/* ───────────────────────────────────────── правило: чистая функция */

test('placement: рядом кнопка-действие → отдельная кнопка, иначе — в обёртке поля', () => {
  const { d } = setupDom();
  try {
    const ta = el(d, 'textarea');
    const input = el(d, 'input', { type: 'text' });
    const button = el(d, 'button', { class: 'btn btn--primary btn--sm' }, 'Ответить');
    assert.equal(placement(ta, button), 'action');
    assert.equal(placement(input, button), 'action');
    assert.equal(placement(ta, null), 'field');
    assert.equal(placement(input, null), 'field');
    assert.equal(placement(el(d, 'div'), button), 'field', 'не поле — всегда обёртка');
  } finally { teardownDom(); }
});

test('подпись «Надиктовать» только в широкой строке', () => {
  assert.equal(wantLabel(LABEL_MIN_WIDTH - 1), false);
  assert.equal(wantLabel(LABEL_MIN_WIDTH), true);
  assert.equal(wantLabel(1280), true);
  assert.equal(wantLabel(0), false);
});

/* ─────────────────────────── «Дальше»: поле + кнопка «Ответить» ─────────── */

test('рядом «Ответить»: микрофон отдельной кнопкой ПЕРЕД ней, без обёртки и absolute', () => {
  const { d, host } = setupDom();
  try {
    const form = el(d, 'form', { class: 'backlog__answer' });
    const input = el(d, 'input', { class: 'input backlog__answer-input', type: 'text' });
    const answer = el(d, 'button', { class: 'btn btn--primary btn--sm', type: 'submit' }, 'Ответить');
    form.appendChild(input);
    form.appendChild(answer);
    host.appendChild(form);

    assert.equal(actionSibling(input), answer, 'кнопка-действие найдена рядом');

    const btn = attachMic(input);
    assert.ok(btn, 'микрофон поставлен');
    assert.equal(btn.getAttribute('data-mic-btn'), 'true');
    assert.ok(btn.classList.contains('mic-btn--action'), 'отдельная кнопка');
    assert.ok(btn.classList.contains('btn--secondary'), 'тот же вид, что в композере');

    // Порядок в строке: поле → микрофон → «Ответить».
    const order = form.childNodes.map((n) => (n === input ? 'field' : n === btn ? 'mic' : n === answer ? 'action' : '?'));
    assert.deepEqual(order, ['field', 'mic', 'action']);
    assert.equal(btn.nextSibling === undefined ? answer : form.childNodes[2], answer);

    // Никакого absolute и никакой обёртки: поле осталось прямым ребёнком строки.
    assert.equal(input.parentNode, form, 'поле не оборачивали');
    assert.equal(form.getAttribute('data-mic-pos'), null, 'строка не становится relative');
    const absolute = host.querySelectorAll('*').filter((n) => String((n.style && n.style.position) || '') === 'absolute');
    assert.equal(absolute.length, 0, 'absolute нет ни у кого');
    assert.equal(input.getAttribute('data-mic'), 'auto');
    assert.equal(enhanceAll(host), 0, 'второй раз не ставим');
  } finally { teardownDom(); }
});

test('узкая строка: подпись скрыта, остаётся иконка (title + aria-label)', () => {
  const { d, host } = setupDom();
  try {
    const row = el(d, 'div', { class: 'row' });
    row.__width = 320;
    const input = el(d, 'input', { type: 'text' });
    const action = el(d, 'button', { class: 'btn btn--primary btn--sm' }, 'Добавить');
    row.appendChild(input);
    row.appendChild(action);
    host.appendChild(row);

    const btn = attachMic(input);
    assert.equal(btn.classList.contains('mic-btn--wide'), false, 'подпись спрятана');
    assert.ok(btn.querySelector('.mic-btn__text'), 'текст в разметке есть — его скрывает CSS');
    assert.equal(btn.getAttribute('title'), 'Надиктовать (Ctrl+M)');
    assert.equal(btn.getAttribute('aria-label'), 'Надиктовать (Ctrl+M)');
  } finally { teardownDom(); }
});

test('широкая строка: подпись «Надиктовать» видна', () => {
  const { d, host } = setupDom();
  try {
    const row = el(d, 'div', { class: 'row' });
    row.__width = 900;
    const input = el(d, 'input', { type: 'text' });
    const action = el(d, 'button', { class: 'btn btn--primary btn--sm' }, 'Отправить');
    row.appendChild(input);
    row.appendChild(action);
    host.appendChild(row);

    const btn = attachMic(input);
    assert.equal(btn.classList.contains('mic-btn--wide'), true);
    assert.ok(btn.textContent.includes('Надиктовать'));
  } finally { teardownDom(); }
});

/* ─────────────────────────── диалог комнаты / палитра: поле без кнопки ─── */

test('без кнопки рядом: обёртка .mic-field вокруг поля, кнопка внутри', () => {
  const { d, host } = setupDom();
  try {
    const dialog = el(d, 'div', { class: 'dialog__body' });
    const ta = el(d, 'textarea', { class: 'textarea' });
    dialog.appendChild(ta);
    host.appendChild(dialog);

    assert.equal(actionSibling(ta), null);

    const btn = attachMic(ta);
    const holder = ta.parentNode;
    assert.equal(holder.getAttribute('data-mic-wrap'), 'field');
    assert.ok(holder.classList.contains('mic-field'));
    assert.equal(holder.parentNode, dialog, 'обёртка стоит там, где было поле');
    assert.equal(btn.parentNode, holder, 'кнопка внутри обёртки поля');
    assert.ok(btn.classList.contains('mic-btn--field'));
    assert.ok(btn.classList.contains('mic-btn--area'));
    assert.ok(btn.classList.contains('mic-btn--compact'));
    assert.ok(btn.classList.contains('btn--secondary'));
    assert.equal(dialog.getAttribute('data-mic-pos'), null, 'у родителя-строки relative не включаем');
    assert.equal(holder.style.position, undefined, 'позицию задаёт CSS обёртки, не инлайн');
  } finally { teardownDom(); }
});

test('столбец (flex-direction: column): микрофон в обёртке, а не перед кнопкой', () => {
  const { d, host } = setupDom();
  try {
    globalThis.getComputedStyle = () => ({ display: 'flex', flexDirection: 'column' });
    const box = el(d, 'div', { class: 'stack' });
    const ta = el(d, 'textarea');
    const button = el(d, 'button', { class: 'btn btn--primary' }, 'Сохранить');
    box.appendChild(ta);
    box.appendChild(button);
    host.appendChild(box);

    assert.equal(actionSibling(ta), null, 'в столбце кнопка не «рядом в строке»');
    const btn = attachMic(ta);
    assert.equal(btn.parentNode.getAttribute('data-mic-wrap'), 'field');
  } finally { teardownDom(); }
});

test('строка (flex-direction: row) распознаётся как строка', () => {
  const { d, host } = setupDom();
  try {
    globalThis.getComputedStyle = () => ({ display: 'flex', flexDirection: 'row' });
    const row = el(d, 'div', { class: 'row' });
    const input = el(d, 'input', { type: 'text' });
    const button = el(d, 'button', { class: 'btn' }, 'Искать');
    row.appendChild(input);
    row.appendChild(button);
    host.appendChild(row);
    assert.equal(actionSibling(input), button);
  } finally { teardownDom(); }
});

/* ─────────────────────────── где микрофону не место ────────────────────── */

test('служебные поля: путь, дата, шапка — data-mic="off" по общему правилу', () => {
  const { d, host } = setupDom();
  try {
    const path = el(d, 'input', { type: 'text', id: 'file-path', class: 'input' });
    const date = el(d, 'input', { type: 'text', name: 'due_date' });
    const header = el(d, 'input', { type: 'search', class: 'search-input' });
    const topbar = el(d, 'div', { class: 'topbar' });
    topbar.appendChild(header);
    const plain = el(d, 'input', { type: 'text' });
    host.appendChild(path);
    host.appendChild(date);
    host.appendChild(topbar);
    host.appendChild(plain);

    assert.equal(noMicPlace(path), true);
    assert.equal(noMicPlace(date), true);
    assert.equal(noMicPlace(header), true, 'поиск в шапке');
    assert.equal(noMicPlace(plain), false);
    assert.equal(eligible(header), false);

    assert.equal(enhanceAll(host), 1, 'микрофон только в обычном поле');
    assert.equal(path.getAttribute('data-mic'), 'off');
    assert.equal(date.getAttribute('data-mic'), 'off');
    assert.equal(header.getAttribute('data-mic'), 'off');
    assert.equal(plain.getAttribute('data-mic'), 'auto');
  } finally { teardownDom(); }
});

/* ─────────────────────────── эталон: явная кнопка композера ─────────────── */

test('явная кнопка композера: тот же класс и подпись, вторая не появляется', () => {
  const { d, host } = setupDom();
  try {
    const ta = el(d, 'textarea', { id: 'cmp-input', 'data-mic': 'manual' });
    const bar = el(d, 'div', { class: 'cmp-bar' });
    bar.appendChild(ta);
    host.appendChild(bar);

    const mic = createMic(ta, { label: 'Надиктовать', className: 'cmp-mic' });
    bar.appendChild(mic);

    assert.ok(mic.classList.contains('mic-btn'));
    assert.ok(mic.classList.contains('mic-btn--action'), 'общий класс, как у автокнопок');
    assert.ok(mic.classList.contains('mic-btn--wide'), 'подпись видна');
    assert.ok(mic.classList.contains('btn--secondary'));
    assert.ok(mic.classList.contains('cmp-mic'));
    assert.equal(enhanceAll(host), 0, 'data-mic="manual" уважается');
  } finally { teardownDom(); }
});

test('состояния одинаковы: запись — точка и таймер, ошибка — значок', async () => {
  const { d, host } = setupDom();
  const track = { stop() {} };
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } },
  });
  globalThis.MediaRecorder = class {
    constructor(_s, opts) { this.mimeType = (opts && opts.mimeType) || 'audio/webm'; this.state = 'inactive'; }
    static isTypeSupported() { return true; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; if (this.ondataavailable) this.ondataavailable({ data: new Blob([new Uint8Array([1, 2])], { type: 'audio/webm' }) }); if (this.onstop) this.onstop(); }
  };
  globalThis.fetch = async () => ({ ok: false, status: 500, json: async () => ({ error: { code: 'stt_failed' } }) });
  try {
    const ta = el(d, 'textarea', { id: 'f1' });
    host.appendChild(ta);
    const btn = createMic(ta);
    btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(btn.getAttribute('data-state'), 'recording');
    assert.ok(btn.querySelector('.mic-btn__dot'), 'красная точка');
    assert.ok(btn.querySelector('.mic-btn__time'), 'таймер записи');

    btn.dispatch('click', { type: 'click', preventDefault() {}, stopPropagation() {} });
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(btn.getAttribute('data-state'), 'error');
    assert.ok(btn.classList.contains('mic-btn--error'));
    const icon = btn.querySelector('.codicon');
    assert.ok(icon && icon.classList.contains('codicon-warning'), 'в ошибке значок «внимание»');
    btn.mic.destroy();
  } finally {
    delete globalThis.navigator;
    delete globalThis.MediaRecorder;
    delete globalThis.fetch;
    teardownDom();
  }
});
