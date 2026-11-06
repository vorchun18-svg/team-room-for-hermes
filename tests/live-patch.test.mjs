/**
 * live-patch — WAVE17-P4: точечное обновление страницы без потери прокрутки,
 * фокуса, выделения, черновиков и раскрытых блоков.
 *
 * Тесты «красные» на старом механизме (`root.replaceChildren`) и зелёные на
 * live-patch. DOM свой, мини-DOM: нужен insertBefore/removeChild/attributes,
 * которых нет в tests/fake-dom.mjs.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applyLivePatch, captureSnapshot, restoreSnapshot, isEditing,
  hashTree, keyOf, stateSignature,
} from '../web/js/live-patch.js';

/* ───────────────────────────────────────────────────────────────── мини-DOM */

class El {
  constructor(tag) {
    this.nodeType = 1;
    this.localName = String(tag).toLowerCase();
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = [];
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.style = { setProperty() {} };
    this.listeners = Object.create(null);
  }

  setAttribute(name, value) {
    const hit = this.attributes.find((a) => a.name === name);
    if (hit) hit.value = String(value);
    else this.attributes.push({ name, value: String(value) });
  }

  getAttribute(name) {
    const hit = this.attributes.find((a) => a.name === name);
    return hit ? hit.value : null;
  }

  hasAttribute(name) { return Boolean(this.getAttribute(name) !== null); }
  removeAttribute(name) { this.attributes = this.attributes.filter((a) => a.name !== name); }

  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }

  get dataset() {
    const self = this;
    const attrName = (prop) => 'data-' + String(prop).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
    return new Proxy({}, {
      get(_t, prop) { const v = self.getAttribute(attrName(prop)); return v === null ? undefined : v; },
      set(_t, prop, value) { self.setAttribute(attrName(prop), value); return true; },
      has(_t, prop) { return self.hasAttribute(attrName(prop)); },
    });
  }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    if (!ref) { this.childNodes.push(node); return node; }
    const idx = this.childNodes.indexOf(ref);
    this.childNodes.splice(idx < 0 ? this.childNodes.length : idx, 0, node);
    return node;
  }

  removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx >= 0) this.childNodes.splice(idx, 1);
    node.parentNode = null;
    return node;
  }

  contains(node) {
    let cur = node;
    while (cur) { if (cur === this) return true; cur = cur.parentNode; }
    return false;
  }

  addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); }
  removeEventListener(type, fn) {
    const arr = this.listeners[type] || [];
    const i = arr.indexOf(fn);
    if (i >= 0) arr.splice(i, 1);
  }

  focus() {
    // Фокус — в документа текущей страницы (mountPage пересоздаёт doc).
    doc.activeElement = this;
    this.focused = true;
  }

  get textContent() {
    return this.childNodes.map((n) => (n.nodeType === 3 ? n.data : n.textContent)).join('');
  }

  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
}

class Txt {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
}

function makeDom() {
  const doc = {
    body: new El('body'),
    documentElement: new El('html'),
    activeElement: null,
    views: new Map(),
    createElement(tag) { const el = new El(tag); el.ownerDocument = doc; return el; },
    createTextNode(text) { const t = new Txt(text); t.ownerDocument = doc; return t; },
    getElementById(id) { return this.views.get(id) || null; },
  };
  doc.activeElement = doc.body;
  return doc;
}

const h = (tag, attrs = {}, ...kids) => {
  const el = doc.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.setAttribute('class', v);
    else if (k === 'data') for (const [dk, dv] of Object.entries(v)) el.setAttribute('data-' + dk, dv);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(typeof kid === 'string' ? doc.createTextNode(kid) : kid);
  }
  return el;
};

let doc = makeDom();
const mk = (tag, attrs, ...kids) => h(tag, attrs, ...kids);

/** Страница запроса: правая колонка с шагами и лента диалога. */
function buildPage({ steps = ['Первый шаг', 'Второй шаг'], feed = ['привет', 'как дела'], note = 'note' } = {}) {
  return mk('div', { class: 'req' },
    mk('header', { class: 'req__head' }, mk('h1', { class: 'req__title' }, 'Запрос')),
    mk('div', { class: 'req__grid' },
      mk('main', { class: 'req__main' },
        mk('section', { class: 'card req-answer' }, mk('p', null, note)),
        mk('section', { class: 'req-thread' },
          mk('div', { class: 'thr__feed', 'data-scroll-key': 'feed' },
            ...feed.map((item, i) => {
              const id = typeof item === 'string' ? 'm' + i : item.id;
              const text = typeof item === 'string' ? item : item.text;
              return mk('div', { class: 'thr-msg', 'data-id': id }, text);
            })))),
      mk('aside', { class: 'req__aside' },
        mk('section', { class: 'card req-side', 'data-key': 'steps' },
          mk('h2', null, 'Шаги'),
          mk('ul', null, ...steps.map((s, i) => mk('li', { class: 'req-step', 'data-key': 's' + i }, s)))),
        mk('section', { class: 'card req-side', 'data-key': 'questions' },
          mk('h2', null, 'Нужен ваш ответ'),
          mk('textarea', { class: 'req-q__input', 'data-keep': 'q:1' })))));
}

function mountPage(page) {
  doc = makeDom();
  const view = doc.createElement('div');
  view.setAttribute('id', 'view');
  doc.views.set('view', view);
  const host = doc.createElement('div');
  host.setAttribute('class', 'req');
  view.appendChild(host);
  // первая отрисовка — как в request.js: страница строится отдельно и применяется
  applyLivePatch(host, page, { doc });
  return { host, view };
}

/* ────────────────────────────────────────────────────────────────── тесты */

test('прокрутка правой колонки и ленты сохраняется при обновлении данных', () => {
  const { host } = mountPage(buildPage());
  const aside = host.childNodes[1].childNodes[1];
  const feed = host.childNodes[1].childNodes[0].childNodes[1].childNodes[0];
  assert.equal(aside.className, 'req__aside');
  assert.equal(feed.className, 'thr__feed');

  aside.scrollTop = 1840;
  feed.scrollTop = 640;

  const next = buildPage({ steps: ['Первый шаг', 'Второй шаг', 'Третий шаг'], feed: ['привет', 'как дела', 'новое'] });
  applyLivePatch(host, next, { doc });

  assert.equal(aside.scrollTop, 1840, 'правая колонка не должна прыгать вверх');
  assert.equal(feed.scrollTop, 640, 'лента не должна прыгать вверх');
  // Контейнеры остались теми же узлами (не пересозданы).
  assert.equal(host.childNodes[1].childNodes[1], aside);
  assert.equal(host.childNodes[1].childNodes[0].childNodes[1].childNodes[0], feed);
});

test('прокрутка #view тоже возвращается на место', () => {
  const { host, view } = mountPage(buildPage());
  view.scrollTop = 900;
  applyLivePatch(host, buildPage({ note: 'другой текст' }), { doc });
  assert.equal(view.scrollTop, 900);
});

test('фокус, выделение и черновик в поле сохраняются', () => {
  const { host } = mountPage(buildPage());
  const ta = host.childNodes[1].childNodes[1].childNodes[1].childNodes[1];
  assert.equal(ta.localName, 'textarea');
  ta.value = 'Мой ответ на вопрос';
  ta.focus();
  ta.setSelectionRange(4, 9);

  applyLivePatch(host, buildPage({ note: 'обновились данные' }), { doc, draftKeys: new Map([['q:1', 'Мой ответ на вопрос']]) });

  const again = host.childNodes[1].childNodes[1].childNodes[1].childNodes[1];
  assert.equal(again.localName, 'textarea');
  assert.equal(doc.activeElement, again, 'фокус должен остаться в том же поле');
  assert.equal(again.value, 'Мой ответ на вопрос', 'черновик не теряется');
  assert.equal(again.selectionStart, 4);
  assert.equal(again.selectionEnd, 9);
});

test('очищенный после отправки черновик не возвращается (ключ убран из карты)', () => {
  const { host } = mountPage(buildPage());
  const ta = host.childNodes[1].childNodes[1].childNodes[1].childNodes[1];
  ta.value = 'Ответ отправлен';
  ta.focus();
  // Пользователь отправил ответ: страница убрала ключ из карты, поле очистилось.
  ta.value = '';
  applyLivePatch(host, buildPage(), { doc, draftKeys: new Map() });
  assert.equal(ta.value, '', 'старый текст не должен возвращаться после отправки');
});

test('открытый пользователем блок остаётся открытым', () => {
  const page = mk('div', { class: 'req' },
    mk('details', { class: 'req-ask', 'data-key': 'ask' }, mk('summary', null, 'Запрос'), mk('p', null, 'текст')));
  const { host } = mountPage(page);
  const det = host.childNodes[0];
  det.open = false;
  det.open = true; // пользователь раскрыл
  applyLivePatch(host, mk('div', { class: 'req' },
    mk('details', { class: 'req-ask', 'data-key': 'ask' }, mk('summary', null, 'Запрос'), mk('p', null, 'текст 2'))), { doc });
  assert.equal(host.childNodes[0].open, true);
});

test('неизменившаяся карточка не пересоздаётся, изменившаяся — заменяется', () => {
  const { host } = mountPage(buildPage());
  const steps = host.childNodes[1].childNodes[1].childNodes[0];
  const step0 = steps.childNodes[1].childNodes[0];

  const next = buildPage({ steps: ['Первый шаг', 'Второй шаг (изменён)'], feed: ['привет', 'как дела'] });
  const stats = applyLivePatch(host, next, { doc });

  const stepsAfter = host.childNodes[1].childNodes[1].childNodes[0];
  assert.equal(stepsAfter.childNodes[1].childNodes[0], step0, 'неизменённый шаг переиспользован');
  assert.equal(stepsAfter.childNodes[1].childNodes[1].textContent, 'Второй шаг (изменён)');
  assert.ok(stats.reused > 0);
});

test('сообщения ленты сопоставляются по data-id: старые узлы остаются собой', () => {
  const { host } = mountPage(buildPage({ feed: [{ id: 'm1', text: 'a' }, { id: 'm2', text: 'b' }] }));
  const feed = host.childNodes[1].childNodes[0].childNodes[1].childNodes[0];
  const first = feed.childNodes[0];
  // Новое сообщение пришло первым (лента перевёрнута сервером) — ключи важнее позиций.
  applyLivePatch(host, buildPage({
    feed: [{ id: 'm9', text: 'new' }, { id: 'm1', text: 'a' }, { id: 'm2', text: 'b' }],
  }), { doc });
  assert.equal(feed.childNodes.length, 3);
  assert.equal(feed.childNodes[0].textContent, 'new');
  assert.equal(feed.childNodes[1], first, 'сообщение m1 осталось тем же узлом');
  assert.equal(feed.childNodes[2].textContent, 'b');
});

test('isEditing видит набор текста и диктовку, но не нажатие кнопки', () => {
  const page = mk('div', { class: 'req' },
    mk('div', { class: 'thr__bar' },
      mk('textarea', { class: 'thr__input', 'data-keep': 'thread' }),
      mk('button', { class: 'mic-btn', 'data-mic-btn': 'true', 'data-state': 'idle' }, 'Диктовать')));
  const { host } = mountPage(page);
  const ta = host.childNodes[0].childNodes[0];
  const mic = host.childNodes[0].childNodes[1];

  assert.equal(isEditing(host, doc), false);
  ta.focus();
  assert.equal(isEditing(host, doc), true, 'набор текста — обновление откладывается');
  mic.focus();
  assert.equal(isEditing(host, doc), false, 'кнопка — не набор текста');
  mic.setAttribute('data-state', 'recording');
  assert.equal(isEditing(host, doc), true, 'идёт диктовка — обновление откладывается');
  mic.setAttribute('data-state', 'idle');
  ta.focus();
  ta.value = '';
  doc.activeElement = doc.body;
  assert.equal(isEditing(host, doc), false);
});

test('снимок и восстановление возвращают позиции контейнеров', () => {
  const { host, view } = mountPage(buildPage());
  view.scrollTop = 900;
  const snap = captureSnapshot(host, doc);
  assert.equal(snap.scrolls.length >= 3, true, 'view + aside + feed');
  assert.equal(snap.drafts.length, 1);
  view.scrollTop = 10;
  restoreSnapshot(host, snap, doc);
  assert.equal(view.scrollTop, 900);
});

test('хеш поддерева одинаков у одинаковых веток и различается у разных', () => {
  const a = mk('div', { class: 'x' }, mk('span', null, 'текст'));
  const b = mk('div', { class: 'x' }, mk('span', null, 'текст'));
  const c = mk('div', { class: 'x' }, mk('span', null, 'другой'));
  assert.equal(hashTree(a), hashTree(b));
  assert.notEqual(hashTree(a), hashTree(c));
});

test('ключ элемента: data-key, затем data-id', () => {
  assert.equal(keyOf(mk('li', { 'data': { key: 's1' } })), 'li:s1');
  assert.equal(keyOf(mk('div', { 'data': { id: 'm2' } })), 'div:m2');
  assert.equal(keyOf(mk('div', null)), '');
});

test('подпись данных не считает heartbeat изменением', () => {
  const base = {
    id: 't_1',
    status: 'working',
    heartbeats: { count: 1, last_at: 100 },
    timeline: [{ kind: 'created', at: 1, text: 'создан' }, { kind: 'heartbeat', at: 2, text: 'Признак жизни' }],
  };
  const later = {
    id: 't_1',
    status: 'working',
    heartbeats: { count: 2, last_at: 160 },
    timeline: [{ kind: 'created', at: 1, text: 'создан' }, { kind: 'heartbeat', at: 3, text: 'Признак жизни' }],
  };
  assert.equal(stateSignature(base), stateSignature(later), 'только heartbeat — не повод перерисовывать');

  const real = { ...later, status: 'reviewing' };
  assert.notEqual(stateSignature(base), stateSignature(real));
});

test('страница запроса больше не сносит всё дерево (нет replaceChildren)', async () => {
  const { readFile } = await import('node:fs/promises');
  const src = await readFile(new URL('../web/js/views/request.js', import.meta.url), 'utf8');
  assert.equal(/root\.replaceChildren\(\.\.\.next/.test(src), false, 'render() не должен сносить дерево целиком');
  assert.equal(/applyLivePatch\(/.test(src), true);
  assert.equal(/deferIfEditing/.test(src), true, 'набор текста откладывает обновление');
  assert.equal(/stateSignature/.test(src), true);
});
