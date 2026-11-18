/**
 * answers-explain-ui.test.mjs — «Пояснить» без текста (B-44) и ответы без кнопки
 * досылки при включённом автообновлении свода (B-45), пакет Q1 волны 21.
 *
 * Что проверяем (docs/WAVE21-RULES.md §2, Q1; замечания B-44, B-45):
 *  - пустое поле + «Пояснить» → уходит `need_explanation:true` БЕЗ `text`,
 *    тост «Просьба о пояснении отправлена — вопрос остаётся открытым»;
 *  - «Отправить» с пустым полем — по-прежнему отказ (запроса нет);
 *  - с текстом «Пояснить» отправляет текст как раньше;
 *  - подсказки говорят «текст можно не писать»;
 *  - `synthesis.live.auto == true` → кнопки «Отправить руководителю накопленные
 *    ответы» НЕТ, вместо «N ответа без реакции» — «Ответы переданы руководителю…
 *    (около 14:20)», время из `next_update_at`; без времени — «в ближайшее время»;
 *  - `auto == false` (и нет поля вовсе — старый сервер) → кнопка есть, слова
 *    «без реакции» нет.
 *
 * Запуск: node --test tests/answers-explain-ui.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { createAside } from '../web/js/views/request-aside.js';
import { FOLD_STORE } from '../web/js/views/aside-fold.js';
import { ANSWERS_RU } from '../web/js/text-ru.js';

/* ── мини-DOM: то, чего нет в fake-dom.mjs ─────────────────────────────── */

function installHelpers() {
  const P = FakeElement.prototype;
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (!('childElementCount' in P)) {
    Object.defineProperty(P, 'childElementCount', {
      configurable: true,
      get() { return (this.childNodes || []).filter((n) => n.nodeType === 1).length; },
    });
  }
  if (typeof P.querySelector !== 'function') {
    P.querySelector = function querySelector(selector) {
      const sel = String(selector);
      const found = sel.startsWith('.') ? byClass(this, sel.slice(1))
        : findAll(this, (n) => String(n.localName) === sel.toLowerCase());
      return found.length ? found[0] : null;
    };
  }
  if (typeof P.insertBefore !== 'function') {
    P.insertBefore = function insertBefore(node, ref) {
      const idx = ref ? this.childNodes.indexOf(ref) : -1;
      node.parentNode = this;
      if (idx >= 0) this.childNodes.splice(idx, 0, node);
      else this.childNodes.push(node);
      return node;
    };
  }
  if (typeof P.removeChild !== 'function') {
    P.removeChild = function removeChild(node) {
      const i = this.childNodes.indexOf(node);
      if (i >= 0) this.childNodes.splice(i, 1);
      node.parentNode = null;
      return node;
    };
  }
  // Поле ответа получает фокус при отказе («Отправить» с пустым полем).
  if (typeof P.focus !== 'function') P.focus = function focus() { this.focused = true; };
}

function findAll(root, pred) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && pred(n)) out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

function byClass(root, cls) {
  return findAll(root, (n) => String(n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

function installStorage() {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
  };
  return store;
}

function clearStorage() {
  try { globalThis.localStorage.removeItem(FOLD_STORE); } catch { /* ignore */ }
}

/* ── данные: вымышленный запрос (никаких реальных имён и текстов) ───────── */

const Q_OPEN = 'На какой склад оформляем первую партию — свой на окраине города или '
  + 'арендованный у поставщика, если у него свободны только дальние стеллажи?';

function makeDetail(over = {}) {
  return Object.assign({
    id: 't_root1',
    next_steps: [],
    open_questions: [
      { id: 'q_1', text: Q_OPEN, for: 'mikhail', answered: false, answer: null, answer_state: 'open' },
      {
        id: 'q_2', text: 'Сколько дней держим запас на складе?', for: 'mikhail',
        answered: true, answered_at: 5, answer: 'Две недели', answer_state: 'sent',
        leader_pending: true, can_remind: true, reaction: null,
      },
    ],
    decisions: [],
    future_questions: [],
    tree: [],
    answers: {
      summary: {
        can_resend: true, resend_pending: 1,
        resend_label: 'Отправить руководителю накопленные ответы',
        resend_hint: ANSWERS_RU.delivered,
      },
    },
  }, over || {});
}

function makeStand(opts = {}) {
  const doc = installFakeDom();
  installHelpers();
  doc.body = new FakeElement('body');
  const posted = [];
  const toasts = [];
  const detail = makeDetail(opts.detail);
  const state = { host: null };
  const ctx = {
    api: {
      async post(path, body) {
        posted.push({ path, body });
        if (opts.postDelay) await new Promise((resolve) => setTimeout(resolve, opts.postDelay));
        if (opts.failWith) throw opts.failWith;
        return opts.response ? opts.response(body) : { ok: true };
      },
      async get() { return {}; },
    },
    toast: (message, kind) => toasts.push({ message, kind }),
    room: { id: 'cafe', members: [] },
    navigate() {},
  };
  const R = {
    ctx, rid: 't_root1', keepValues: new Map(), roomId: 'cafe',
    get detail() { return detail; },
    get backlog() { return new Map(); },
    async refresh() {},
    render() { /* в тестах перерисовки нет — узлы остаются те же */ },
  };
  const { buildAside } = createAside(R);
  const host = doc.createElement('div');
  host.appendChild(buildAside());
  state.host = host;
  return { host, posted, toasts, detail, doc };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Строка вопроса q_1 и её поле/кнопки. */
function questionRow(host) {
  return byClass(host, 'req-q').find((row) => row.getAttribute('data-q') === 'q_1');
}

function controls(host) {
  const row = questionRow(host);
  assert.ok(row, 'строка вопроса q_1 есть');
  return {
    row,
    field: byClass(row, 'req-q__input')[0],
    send: byClass(row, 'req-q__send')[0],
    explain: byClass(row, 'req-q__explain')[0],
  };
}

/* ─────────────────────────────────────────── B-44: «Пояснить» без текста */

test('B-44: «Пояснить» с пустым полем — просьба уходит без текста', async () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const c = controls(s.host);
    assert.ok(c.explain, 'кнопка «Пояснить» есть');
    assert.equal(c.field.value || '', '', 'поле пустое');
    c.explain.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.posted.length, 1, 'ровно один запрос');
    assert.equal(s.posted[0].path, '/api/requests/t_root1/actions');
    assert.deepEqual(s.posted[0].body,
      { action: 'answer', question_id: 'q_1', need_explanation: true },
      'пустое поле — нет текста, только флаг пояснения');
    assert.equal(s.toasts.length, 1);
    assert.equal(s.toasts[0].message, ANSWERS_RU.explainSent);
    assert.match(s.toasts[0].message, /вопрос остаётся открытым/);
    assert.doesNotMatch(JSON.stringify(s.toasts), /Напишите, что пояснить/);
  } finally {
    uninstallFakeDom();
  }
});

test('B-44: «Отправить» с пустым полем — отказ, запроса нет', async () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const c = controls(s.host);
    c.send.dispatch('click');
    await tick();
    assert.equal(s.posted.length, 0, 'ничего не отправлено');
    assert.equal(s.toasts.length, 1);
    assert.equal(s.toasts[0].message, ANSWERS_RU.answerEmpty);
    assert.equal(s.toasts[0].kind, 'warn');
  } finally {
    uninstallFakeDom();
  }
});

test('B-44: «Пояснить» с текстом — текст уходит как раньше', async () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const c = controls(s.host);
    c.field.value = 'уточни, что значит «свой склад» — аренда или покупка';
    c.explain.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.posted.length, 1);
    assert.deepEqual(s.posted[0].body, {
      action: 'answer', question_id: 'q_1',
      text: 'уточни, что значит «свой склад» — аренда или покупка',
      need_explanation: true,
    });
    assert.equal(s.toasts[0].message, ANSWERS_RU.explainSent);
  } finally {
    uninstallFakeDom();
  }
});

test('B-44: обычный ответ уходит с текстом и без флага пояснения', async () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const c = controls(s.host);
    c.field.value = 'На свой склад, аренду не берём';
    c.send.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.posted.length, 1);
    assert.deepEqual(s.posted[0].body,
      { action: 'answer', question_id: 'q_1', text: 'На свой склад, аренду не берём' });
    assert.match(s.toasts[0].message, /Ответ отправлен/);
  } finally {
    uninstallFakeDom();
  }
});

test('B-44: второе нажатие, пока просьба летит, не отправляет её дважды', async () => {
  installStorage();
  clearStorage();
  const s = makeStand({ postDelay: 30 });
  try {
    const c = controls(s.host);
    c.explain.dispatch('click');
    assert.equal(c.explain.disabled, true, 'кнопка занята, пока идёт запрос');
    c.explain.dispatch('click');                    // второй клик в полёте — молчит
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.equal(s.posted.length, 1, 'улетела одна просьба');
    assert.equal(c.explain.disabled, false, 'после ответа кнопка снова живая');
  } finally {
    uninstallFakeDom();
  }
});

test('B-44: ошибка сервера — тост и кнопка снова живая (можно повторить)', async () => {
  installStorage();
  clearStorage();
  const err = new Error('Сервис недоступен');
  err.code = 'network';
  const s = makeStand({ failWith: err });
  try {
    const c = controls(s.host);
    c.explain.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.toasts.length, 1);
    assert.equal(s.toasts[0].kind, 'danger');
    assert.match(s.toasts[0].message, /Сервис недоступен/);
    assert.equal(c.explain.disabled, false, 'кнопка живая — просьбу можно повторить');
  } finally {
    uninstallFakeDom();
  }
});

test('B-44: подсказки говорят, что текст можно не писать', () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const c = controls(s.host);
    assert.equal(c.explain.getAttribute('title'), ANSWERS_RU.explainTitle);
    assert.match(c.explain.getAttribute('title'), /текст можно не писать/);
    const hint = byClass(s.host, 'req-q__common-hint')[0];
    assert.ok(hint, 'общая подсказка блока есть');
    assert.match(text(hint), /текст можно не писать/);
  } finally {
    uninstallFakeDom();
  }
});

/* ──────────────────────── B-45: кнопка досылки и строка «без реакции» ──── */

test('B-45: при auto=true кнопки досылки нет, есть спокойная строка со временем', () => {
  installStorage();
  clearStorage();
  const nextAt = Math.floor(Date.parse('2026-11-12T14:20:00') / 1000);
  const s = makeStand({
    detail: { synthesis: { state: 'ready', live: { auto: true, next_update_at: nextAt } } },
  });
  try {
    assert.equal(byClass(s.host, 'req-side__resend').length, 0, 'кнопки «Отправить накопленные» нет');
    const line = byClass(s.host, 'req-side__delivered')[0];
    assert.ok(line, 'строка о передаче ответов есть');
    assert.match(text(line), /Ответы переданы руководителю/);
    assert.match(text(line), /ближайшем обновлении свода/);
    assert.match(text(line), /\(около \d\d:\d\d\)/, 'время ближайшего обновления');
    const expected = new Date(nextAt * 1000);
    const hh = String(expected.getHours()).padStart(2, '0');
    const mm = String(expected.getMinutes()).padStart(2, '0');
    assert.ok(text(line).includes(`около ${hh}:${mm}`), text(line));
    // Слов «без реакции» в интерфейсе больше нет.
    assert.doesNotMatch(text(s.host), /без реакции/);
  } finally {
    uninstallFakeDom();
  }
});

test('B-45: auto=true без времени — «в ближайшее время», кнопки нет', () => {
  installStorage();
  clearStorage();
  const s = makeStand({ detail: { synthesis: { state: 'ready', live: { auto: true } } } });
  try {
    assert.equal(byClass(s.host, 'req-side__resend').length, 0);
    const line = byClass(s.host, 'req-side__delivered')[0];
    assert.ok(line, 'строка есть');
    assert.equal(text(line), ANSWERS_RU.deliveredSoon);
    assert.match(text(line), /в ближайшее время/);
  } finally {
    uninstallFakeDom();
  }
});

test('B-45: auto=false — кнопка остаётся, слова «без реакции» нет', () => {
  installStorage();
  clearStorage();
  const s = makeStand({
    detail: { synthesis: { state: 'ready', live: { auto: false, next_update_at: 1 } } },
  });
  try {
    const btn = byClass(s.host, 'req-side__resend')[0];
    assert.ok(btn, 'при выключенном автообновлении кнопка нужна');
    assert.match(text(btn), /Отправить руководителю накопленные ответы/);
    const row = byClass(s.host, 'req-side__resend-row')[0];
    assert.match(text(row), /1 ответ ждёт отправки/);
    assert.doesNotMatch(text(s.host), /без реакции/);
    assert.equal(byClass(s.host, 'req-side__delivered').length, 0, 'спокойной строки нет');
  } finally {
    uninstallFakeDom();
  }
});

test('B-45: живого состояния нет (старый сервер) — кнопка работает как раньше', () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    assert.ok(byClass(s.host, 'req-side__resend')[0], 'кнопка есть');
    assert.doesNotMatch(text(s.host), /без реакции/);
  } finally {
    uninstallFakeDom();
  }
});

test('B-45: автообновление включено и ждать нечего — ни кнопки, ни строки', () => {
  installStorage();
  clearStorage();
  const s = makeStand({
    detail: {
      answers: { summary: { can_resend: false, resend_pending: 0 } },
      synthesis: { state: 'ready', live: { auto: true, next_update_at: 1 } },
    },
  });
  try {
    assert.equal(byClass(s.host, 'req-side__resend').length, 0);
    assert.equal(byClass(s.host, 'req-side__delivered').length, 0,
      'нечего сообщать — все ответы уже учтены');
  } finally {
    uninstallFakeDom();
  }
});
