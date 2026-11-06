/**
 * decisions-ui.test.mjs — решения руководителя в правой колонке (WAVE19, D2: B-20, B-21).
 *
 * Что проверяем:
 *  - у решения в статусе «предложено» есть действия «Принять», «Отклонить»,
 *    «Обсудить…» (раньше был только текст и метка — принять было нельзя);
 *  - «Принять» отправляет `{action:"decision", decision_id, decision:"accept"}` сразу;
 *    «Отклонить» и «Обсудить» открывают окно с полем и диктовкой (decision-dialog.js);
 *  - принятое решение уходит из «Нужен ваш ответ» (данные API: `open`/`status`);
 *  - цепочка статуса ответа — «ответ отправлен» → «руководитель прочитал» → «учтено»
 *    с числом карточек; подсказка «что значит» есть в title и в подсказке блока;
 *  - CSS окон и действий есть, читаемого текста мельче 11 px нет.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { createAside } from '../web/js/views/request-aside.js';
import {
  CHAIN_HINT, DECISION_ACTIONS, decisionActions, decisionAnswerOf, decisionPayload, decisionStatusOf,
} from '../web/js/views/decision-dialog.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const css = (name) => readFileSync(join(HERE, '..', 'web', 'css', name), 'utf8');

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
  if (typeof P.querySelectorAll !== 'function') {
    P.querySelectorAll = function querySelectorAll(selector) {
      const sel = String(selector);
      return sel.startsWith('.') ? byClass(this, sel.slice(1))
        : findAll(this, (n) => String(n.localName) === sel.toLowerCase());
    };
  }
  if (typeof P.closest !== 'function') {
    P.closest = function closest(selector) {
      const cls = String(selector).replace(/^\./, '');
      let cur = this;
      while (cur && cur.nodeType === 1) {
        if (String(cur.getAttribute('class') || '').split(/\s+/).includes(cls)) return cur;
        cur = cur.parentNode;
      }
      return null;
    };
  }
  if (typeof P.replaceChildren !== 'function') {
    P.replaceChildren = function replaceChildren(...nodes) {
      this.childNodes = [];
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (typeof P.focus !== 'function') P.focus = function focus() {};
  if (typeof P.remove !== 'function') {
    P.remove = function remove() {
      const parent = this.parentNode;
      if (parent && typeof parent.removeChild === 'function') parent.removeChild(this);
    };
  }
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

function byAttr(root, name, value) {
  return findAll(root, (n) => String(n.getAttribute(name) || '') === String(value));
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

/* ── данные: длинное реальное решение (проверка вёрстки) ─────────────────── */

const DECISION_LONG = 'Стартовать с набора из шести месяцев вместо двенадцати: вход дешевле '
  + 'примерно на 38% (по смете закупки от 05.11), а спрос до первой поставки ещё не подтверждён '
  + 'ни одной отгрузкой. Если за квартал продажи выйдут на 70% плана — добираем вторую половину '
  + 'набора по цене фиксации, она зафиксирована в приложении Б к договору поставки и не индексируется.';

const DECISION_WHY = 'Считать пришлось по двум сценариям: при 12 месяцах склад стоит 240 тыс. руб. '
  + 'в квартал, при 6 — 118 тыс.; разница покрывает возвратную тару, если поставщик согласится '
  + 'на условиях самовывоза со склада в C:\\TeamRoom\\Agents\\default\\2026-11-06 Набор — смета.';

function makeDetail(over = {}) {
  return Object.assign({
    id: 't_root1',
    open_questions: [],
    next_steps: [],
    future_questions: [],
    decisions: [
      {
        id: 'd_1', text: DECISION_LONG, why: DECISION_WHY, status: 'proposed', open: true,
        actions: [
          { action: 'accept', label: 'Принять', hint: 'Согласиться с решением', current: false },
          { action: 'reject', label: 'Отклонить', hint: 'Отклонить решение', current: false },
          { action: 'discuss', label: 'Обсудить', hint: 'Обсудить с руководителем', current: false },
        ],
      },
      {
        id: 'd_2', text: 'Брать упаковку у местного поставщика', why: 'логистика', status: 'proposed', open: true,
      },
      {
        id: 'd_3', text: 'Штраф 0,3% в день с ограничением 10%', why: null, status: 'accepted', open: false,
        answer_state: 'applied', answer_label: 'учтено: изменено 2 карточки',
        answer_hint: 'Руководитель учёл ответ: работа пересмотрена.',
        answer_text: 'Согласен, оставляем 0,3%', answered_at: 5,
        reaction: { text: 'Учтено: переписал раздел 7.3 и приложение Б', at: 6, cards: ['t_aaaa1111', 't_bbbb2222'] },
      },
    ],
    tree: [],
    answers: { summary: {} },
  }, over || {});
}

function makeStand(opts = {}) {
  const doc = installFakeDom();
  installHelpers();
  doc.body = new FakeElement('body');
  const posted = [];
  const toasts = [];
  let renders = 0;
  const detail = makeDetail(opts.detail);
  const R = {
    ctx: {
      api: {
        async post(path, body) {
          posted.push({ path, body });
          if (opts.failWith) throw opts.failWith;
          return { ok: true };
        },
        async get() { return {}; },
      },
      toast: (message, kind) => toasts.push({ message, kind }),
      room: { id: 'cafe' },
      navigate() {},
    },
    rid: 't_root1',
    keepValues: new Map(),
    roomId: 'cafe',
    get detail() { return detail; },
    get backlog() { return new Map(); },
    async refresh() { renders += 1; },
    render() {},
  };
  const { buildAside } = createAside(R);
  const host = doc.createElement('div');
  host.appendChild(buildAside());
  return { host, posted, toasts, detail, doc, buildAside, renders: () => renders };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/* ───────────────────────────── B-20: действия над решением ────────────── */

test('предложенное решение: три действия и понятный статус', () => {
  installStorage();
  const s = makeStand();
  try {
    const row = byAttr(s.host, 'data-decision-id', 'd_1')[0];
    assert.ok(row, 'строка решения нарисована');
    assert.equal(row.getAttribute('data-decision-status'), 'proposed');
    const buttons = findAll(row, (n) => n.nodeType === 1 && String(n.localName) === 'button'
      && String(n.getAttribute('data-action') || '').startsWith('decision-'));
    assert.deepEqual(buttons.map((b) => b.getAttribute('data-action')),
      ['decision-accept', 'decision-reject', 'decision-discuss']);
    assert.match(text(buttons[0]), /Принять/);
    assert.match(text(buttons[1]), /Отклонить/);
    assert.match(text(buttons[2]), /Обсудить/);
    assert.match(String(buttons[0].getAttribute('class')), /btn--primary/, '«Принять» — главное действие');
    for (const btn of buttons) {
      assert.ok(String(btn.getAttribute('title') || '').length > 10, 'у каждой кнопки — подсказка');
      assert.ok(String(btn.getAttribute('aria-label') || '').includes('Стартовать'), 'кнопка называет решение');
    }
    assert.match(text(row), /предложено/);
  } finally {
    uninstallFakeDom();
  }
});

test('«Принять» отправляет решение руководителю сразу, без окна', async () => {
  installStorage();
  const s = makeStand();
  try {
    const btn = byAttr(s.host, 'data-action', 'decision-accept')[0];
    btn.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.posted.length, 1, 'один запрос');
    assert.equal(s.posted[0].path, '/api/requests/t_root1/actions');
    assert.deepEqual(s.posted[0].body, { action: 'decision', decision_id: 'd_1', decision: 'accept' });
    assert.equal(s.renders(), 1, 'данные пересчитаны сразу после ответа');
    assert.ok(s.toasts.some((t) => /Решение принято/.test(t.message)));
    assert.equal(byClass(s.doc.body, 'decdlg').length, 0, 'окно для «Принять» не нужно');
  } finally {
    uninstallFakeDom();
  }
});

test('«Отклонить» открывает окно с полем и отправляет текст', async () => {
  installStorage();
  const s = makeStand();
  try {
    byAttr(s.host, 'data-action', 'decision-reject')[0].dispatch('click');
    await tick();
    const dialogs = byClass(s.doc.body, 'decdlg');
    assert.equal(dialogs.length, 1, 'окно открылось');
    const dialog = dialogs[0];
    assert.match(text(dialog), /Отклонить решение/);
    assert.match(text(dialog), /Стартовать с набора из шести месяцев/, 'в окне видно само решение');
    const field = byClass(dialog, 'decdlg__input')[0];
    assert.ok(field, 'поле для текста');
    field.value = 'Нужен год — иначе не успеваем набрать объём';
    const submit = byAttr(dialog, 'data-action', 'decision-reject')[0];
    submit.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.posted.length, 1);
    assert.deepEqual(s.posted[0].body, {
      action: 'decision', decision_id: 'd_1', decision: 'reject',
      text: 'Нужен год — иначе не успеваем набрать объём',
    });
    assert.equal(byClass(s.doc.body, 'decdlg').length, 0, 'окно закрылось после отправки');
  } finally {
    uninstallFakeDom();
  }
});

test('«Обсудить» без текста не отправляется — подсказка вместо запроса', async () => {
  installStorage();
  const s = makeStand();
  try {
    byAttr(s.host, 'data-action', 'decision-discuss')[0].dispatch('click');
    await tick();
    const dialog = byClass(s.doc.body, 'decdlg')[0];
    assert.ok(dialog, 'окно обсуждения открылось');
    assert.match(text(dialog), /Обсудить решение/);
    byAttr(dialog, 'data-action', 'decision-discuss')[0].dispatch('click');
    await tick();
    assert.equal(s.posted.length, 0, 'пустой текст не уходит');
    assert.ok(s.toasts.some((t) => t.kind === 'warn' && /надиктуйте/.test(t.message)));
  } finally {
    uninstallFakeDom();
  }
});

/* ───────────────────────── B-21: цепочка статусов ответа ──────────────── */

test('цепочка ответа: «учтено: изменено 2 карточки» и реакция руководителя', () => {
  installStorage();
  const s = makeStand();
  try {
    const row = byAttr(s.host, 'data-decision-id', 'd_3')[0];
    assert.ok(row, 'принятое решение в свёрнутом блоке «Решения»');
    const chip = byClass(row, 'req-decision__state')[0];
    assert.equal(text(chip), 'учтено: изменено 2 карточки');
    assert.match(String(chip.getAttribute('title') || ''), /учёл ответ/i, 'подсказка «что значит»');
    assert.match(text(row), /Учтено: переписал раздел 7\.3/);
    const cards = byClass(row, 'req-q__card');
    assert.deepEqual(cards.map(text), ['t_aaaa1111', 't_bbbb2222']);
    assert.equal(row.getAttribute('data-decision-status'), 'accepted');
  } finally {
    uninstallFakeDom();
  }
});

test('принятое решение уходит из «Нужен ваш ответ» и из счётчика', () => {
  installStorage();
  const s = makeStand();
  try {
    const pin = byClass(s.host, 'req-side--pin')[0];
    const rows = (root, id) => byAttr(root, 'data-decision-id', id)
      .filter((n) => String(n.getAttribute('class') || '').split(/\s+/).includes('req-decision'));
    assert.equal(rows(pin, 'd_3').length, 0, 'решённое решение не ждёт Михаила');
    assert.equal(rows(pin, 'd_1').length, 1, 'предложенное — ждёт');
    assert.match(text(pin), /2 решения/, 'в расшифровке только те, что ждут слова');
    const fold = byClass(s.host, 'req-side--fold').find((f) => f.getAttribute('data-fold') === 'decisions');
    assert.ok(fold, 'блок «Решения» есть');
    assert.equal(rows(fold, 'd_3').length, 1, 'принятое — в «Решениях»');
  } finally {
    uninstallFakeDom();
  }
});

test('подсказка «что значит» у блока решений и в окне', () => {
  installStorage();
  const s = makeStand();
  try {
    const hint = byClass(s.host, 'req-decision__hint')[0];
    assert.ok(hint, 'подсказка у блока есть');
    assert.match(text(hint), /отправлен → прочитал → учтено/);
    assert.match(String(hint.getAttribute('title') || ''), /метаданных реакции/);
  } finally {
    uninstallFakeDom();
  }
});

test('модель решения: статусы, действия и тело запроса — в одном месте', () => {
  assert.equal(decisionStatusOf({ status: 'proposed' }).label, 'предложено');
  assert.equal(decisionStatusOf({ status: 'rejected' }).label, 'отклонено вами');
  assert.equal(decisionStatusOf({ status: 'discuss' }).tone, 'info');
  assert.deepEqual(DECISION_ACTIONS.map((a) => a.action), ['accept', 'reject', 'discuss']);
  assert.deepEqual(decisionActions({}).map((a) => a.action), ['accept', 'reject', 'discuss']);
  assert.equal(decisionAnswerOf({}), null);
  assert.equal(decisionAnswerOf({ answer_state: 'open' }), null, 'цепочки без ответа нет');
  assert.equal(decisionStatusOf({ status: 'accepted', status_source: 'leader' }).label,
    'принято руководителем', 'решение закрыл руководитель — не приписываем Михаилу');
  assert.equal(decisionStatusOf({ status: 'accepted', status_source: 'user' }).label, 'принято вами');
  const answer = decisionAnswerOf({ answer_state: 'sent', answer_label: 'ответ отправлен', leader_pending: true });
  assert.equal(answer.key, 'sent');
  assert.equal(answer.pending, true);
  assert.match(CHAIN_HINT, /ответ отправлен/);
  assert.deepEqual(decisionPayload({ id: 'd_9' }, 'accept'), {
    action: 'decision', decision_id: 'd_9', decision: 'accept',
  });
  assert.deepEqual(decisionPayload({ id: 'd_9' }, 'discuss', '  текст  '), {
    action: 'decision', decision_id: 'd_9', decision: 'discuss', text: 'текст',
  });
});

/* ───────────────────────────────────────────── CSS и доступность ─────── */

test('CSS: действия, цепочка и окно описаны; текст не мельче 11 px', () => {
  const sheet = css('view-request.css');
  for (const cls of ['.req-decision__actions', '.req-decision__state', '.req-decision__answer',
    '.req-decision__pending', '.req-decision__hint', '.decdlg', '.decdlg__input', '.decdlg__foot']) {
    assert.ok(sheet.includes(cls), 'нет стилей для ' + cls);
  }
  // Аватары — исключение по правилам волны 17; всё остальное не мельче 11 px.
  const sizes = [...sheet.matchAll(/[^\n]*font-size:\s*([0-9.]+)px/g)]
    .filter((m) => !/avatar/.test(m[0]))
    .map((m) => Number(m[1]));
  assert.deepEqual(sizes.filter((v) => v < 11), [], 'читаемого текста мельче 11 px нет');
});
