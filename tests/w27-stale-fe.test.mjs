/**
 * w27-stale-fe.test.mjs — «интерфейс знает все состояния» (W27-E2, §1 правил).
 *
 * Дефект 18.11 вечером: сервер отдавал `approvals[].state = "closed"` (20 штук),
 * интерфейс такого слова не знал, рисовал строки как «ждёт вашего решения»,
 * складывал их в счётчик («27») и на каждое нажатие получал 409 — девять красных
 * плашек подряд. Здесь проверяется ровно то, что человек видит:
 *
 *  1) `closed` — известное состояние, в «ждёт вас» и счётчики НЕ идёт;
 *  2) незнакомое слово не превращается в «ждёт» (раньше — превращалось);
 *  3) счётчик берётся с сервера (`needs_you_count`), а не пересчитывается;
 *  4) у закрытого пункта нет кнопок решений, но есть «Вернуть»;
 *  5) 409 `already_decided` — тихая перерисовка, без красной плашки и без тоста;
 *  6) тосты дедуплицируются: девять одинаковых причин → один тост;
 *  7) «Не актуально» / «Вернуть» — одно тело действия `target:{kind,id}` для
 *     вопроса, решения, разрешения и шага;
 *  8) массово «Закрыть все неактуальные» — `dismiss_stale` по комнате, с числом.
 *
 * DOM — tests/fake-dom.mjs. Живая проверка — Chromium на моке (порт 8852).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, FakeElement } from './fake-dom.mjs';
import {
  approvalCard, approvalState, isClosed, isRestorable, closedApprovals, closedSummary,
  waitingApprovals, stateLine, approvalStaleActions, createApprovals,
} from '../web/js/views/approvals.js';
import {
  needsYouCount, staleSummary, dismissBody, restoreBody, isAlreadyDecided,
  toastOnce, resetToastOnce, closedReasonText, isFinishedRequest,
} from '../web/js/views/stale-model.js';
import {
  createRequestStore, resetRequestStores,
} from '../web/js/views/request-store.js';
import { inboxSummary } from '../web/js/views/backlog-model.js';

/* ────────────────────────────────────────────── мини-DOM (как в approvals-ui) */

function patchDom() {
  const P = FakeElement.prototype;
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (typeof P.replaceChildren !== 'function') {
    P.replaceChildren = function replaceChildren(...nodes) {
      this.childNodes = [];
      for (const node of nodes) if (node) this.appendChild(node);
    };
  }
  if (typeof P.insertBefore !== 'function') {
    P.insertBefore = function insertBefore(node, ref) {
      const i = ref ? this.childNodes.indexOf(ref) : -1;
      node.parentNode = this;
      if (i === -1) this.childNodes.push(node);
      else this.childNodes.splice(i, 0, node);
      return node;
    };
  }
  if (typeof P.querySelector !== 'function') {
    P.querySelector = function querySelector(selector) {
      const cls = String(selector || '').replace(/^\./, '').split(/[\[\s.]/)[0];
      const walk = (node) => {
        for (const child of node.childNodes || []) {
          if (child.nodeType !== 1) continue;
          if (cls && String(child.getAttribute('class') || '').split(/\s+/).includes(cls)) return child;
          const deep = walk(child);
          if (deep) return deep;
        }
        return null;
      };
      return walk(this);
    };
  }
}

patchDom();
installFakeDom();

const text = (node) => {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
};

function buttons(root) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (String(child.tagName).toLowerCase() === 'button') out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

const byClass = (root, cls) => buttons(root).filter((b) => String(b.getAttribute('class') || '').includes(cls));

/** Ровно те 20 закрытых разрешений, что были в боевых данных дефекта. */
function closedApprovalsFixture(n = 20) {
  return Array.from({ length: n }, (_, i) => ({
    id: 'ap_closed_' + String(i).padStart(3, '0'),
    task_id: 't_a1b2c3d4',
    profile: 'anna-assistant',
    member_title: 'Анна',
    kind: i >= n - 5 ? 'execute_code' : 'terminal',
    command: 'python tools/build_report.py --input C:/Work/Rooms/demo/orders.csv',
    description: 'запуск скрипта сборки отчёта',
    pattern_key: i >= n - 5 ? 'execute_code' : 'script execution via heredoc',
    floor: false,
    grantable: i < n - 5,
    dead_end: i >= n - 5 ? 'workaround' : 'grantable',
    actions: i >= n - 5 ? ['hint_agent', 'trust_profile', 'dismiss'] : ['once', 'always', 'deny'],
    state: 'closed',
    closed_reason: 'card_finished',
    decided_at: 1794940000,
    card_state: 'done',
    count: 1,
    request_id: 't_a1b2c3d4',
    request_title: 'Собрать отчёт по упаковке',
  }));
}

/* ───────────────────────────────────────────── 1. состояния по контракту §1 */

test('state: `closed` — известное состояние, а не «ждёт вашего решения»', () => {
  const a = closedApprovalsFixture(1)[0];
  assert.equal(approvalState(a), 'closed');
  assert.equal(isClosed(a), true);
  assert.equal(isRestorable(a), true, 'закрытое можно вернуть');
  assert.equal(stateLine(a, 'closed'), 'Закрыто автоматически — работа завершена');
  assert.equal(closedReasonText(a), 'работа завершена');
});

test('state: незнакомое слово больше НЕ считается «ждёт» (тихая сторона)', () => {
  assert.equal(approvalState({ id: 'x', state: 'processing_weird' }), 'unknown');
  assert.equal(waitingApprovals([{ id: 'x', state: 'processing_weird' }]).length, 0);
  // запись без `state` у завершённой карточки — тоже закрытая, а не ждущая
  assert.equal(approvalState({ id: 'y', card_state: 'done' }), 'closed');
  assert.equal(approvalState({ id: 'z', decided_at: 1794940000 }), 'granted_once');
  // старый сервер: без полей — по-прежнему «ждёт» (обратная совместимость)
  assert.equal(approvalState({ id: 'w' }), 'unknown');
});

test('счётчик: 20 закрытых разрешений не попадают в «ждёт вас»', () => {
  const list = closedApprovalsFixture(20);
  assert.equal(waitingApprovals(list).length, 0, 'в «Нужен ваш ответ» — ноль');
  assert.equal(closedApprovals(list).length, 20, 'все в «Закрытые»');
  const summary = closedSummary(list);
  assert.equal(summary.count, 20);
  assert.match(summary.text, /работа завершена/);
  // и в счётчике запроса — ноль разрешений
  const count = needsYouCount({ needs_you_count: { questions: 4, decisions: 3, approvals: 0, total: 7 } });
  assert.equal(count.approvals, 0);
  assert.equal(count.total, 7);
});

/* ───────────────────────────────────────── 2. счётчик берётся с сервера */

test('счётчик: `needs_you_count` сервера главнее пересчёта по списку', () => {
  const questions = [{ id: 'q_1' }, { id: 'q_2' }];
  const decisions = [{ id: 'd_1', status: 'proposed', open: true }];
  const approvals = closedApprovalsFixture(20);
  const withoutServer = inboxSummary({ questions, decisions, approvals });
  assert.equal(withoutServer.total, 3, 'без сервера считаем по списку (2+1+0)');

  const withServer = inboxSummary({
    questions, decisions, approvals,
    counts: { questions: 4, decisions: 3, approvals: 0, total: 7 },
  });
  assert.equal(withServer.fromServer, true);
  assert.equal(withServer.total, 7, 'число — с сервера, а не 3');
  assert.equal(withServer.approvals, 0, 'закрытые разрешения не в счёте');
  assert.match(withServer.text, /4 вопроса/);
});

test('меню: шаги не входят в число «ждёт вас» — как и на экране «Сегодня»', () => {
  // Дефект W27-FIX: у запроса `needs_you_count = {questions: 2, steps: 2, total: 4}`,
  // «Сегодня» показывало 2 (вопросы+решения+разрешения), а меню — 4 (через `total`).
  // Шаги живут в «Просрочено/Ближайшие», в числе «ждёт вас» их быть не должно.
  const summary = inboxSummary({
    questions: [{ id: 'q_1' }], decisions: [], approvals: [],
    counts: { questions: 1, decisions: 0, approvals: 0, steps: 2, total: 3 },
  });
  assert.equal(summary.total, 1, 'число меню = вопросы + решения + разрешения');
  assert.equal(summary.steps, undefined, 'шагов в расшифровке числа нет');
});

/* ─────────────────────────────────────── 3. карточка закрытого пункта */

test('карточка `closed`: чип «работа завершена», нет решений, есть «Вернуть»', () => {
  const card = approvalCard(closedApprovalsFixture(1)[0], {});
  const body = text(card);
  assert.match(body, /работа завершена/);
  assert.doesNotMatch(body, /ждёт вашего решения/);
  assert.equal(card.getAttribute('data-ap-state'), 'closed');
  const labels = buttons(card).map((b) => text(b));
  assert.ok(labels.some((l) => /Вернуть/.test(l)), 'кнопка «Вернуть» есть');
  assert.ok(!labels.some((l) => /Разрешить один раз|Разрешать всегда|Запретить/.test(l)),
    'кнопок решений у закрытого пункта нет');
});

test('«Не актуально» есть и у обычного ждущего разрешения', () => {
  const card = approvalCard({
    id: 'ap_live', task_id: 't_5c8e4a02', state: 'pending', kind: 'terminal',
    command: 'python -c "print(1)"', description: 'запуск команды', count: 1,
    card_state: 'blocked', at: 1794940000,
  }, { onDecide: async () => ({}) });
  const labels = buttons(card).map((l) => text(l));
  assert.ok(labels.some((l) => /Не актуально/.test(l)), 'счётчик можно обнулить без решения');
});

test('действия закрытого пункта — только «Вернуть»', () => {
  const a = closedApprovalsFixture(1)[0];
  const acts = approvalStaleActions(a);
  assert.equal(acts.length, 1);
  assert.equal(acts[0].action, 'restore');
  assert.equal(approvalStaleActions({ id: 'p', state: 'pending' }).length, 0);
});

/* ───────────────────────────────────────── 4. тихий 409 и дедупликация */

test('409 already_decided: ни ошибки, ни тоста — тихая перерисовка', async () => {
  const err = new Error('Разрешение уже решено');
  err.status = 409;
  err.code = 'already_decided';
  assert.equal(isAlreadyDecided(err), true, 'код ловится');
  assert.equal(isAlreadyDecided(Object.assign(new Error('x'), { status: 409 })), true,
    'старый сервер отдаёт просто 409 — тоже «уже решено»');

  const toasts = [];
  let conflicts = 0;
  const card = approvalCard({
    id: 'ap_live_1', task_id: 't_a1b2c3d4', profile: 'anna-assistant', member_title: 'Анна',
    kind: 'terminal', command: 'python -c "print(1)"', description: 'запуск скрипта',
    count: 1, card_state: 'blocked', at: 1794940000, state: 'pending',
    grantable: true, dead_end: 'grantable', floor: false,
  }, {
    onDecide: async () => { throw err; },
    onConflict: () => { conflicts += 1; },
    toast: (t, tone) => toasts.push([t, tone]),
  });
  const once = buttons(card).find((b) => /Разрешить один раз/.test(text(b)));
  assert.ok(once, 'кнопка «Разрешить один раз» на месте');
  once.dispatch('click', { type: 'click', target: once, preventDefault() {} });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const body = text(card);
  assert.match(body, /Уже решено/);
  assert.doesNotMatch(body, /Это разрешение уже решено — страница обновится/, 'красной плашки нет');
  assert.equal(card.getAttribute('data-ap-conflict'), 'already_decided');
  assert.equal(toasts.length, 0, 'тоста об ошибке нет');
  assert.equal(conflicts, 1, 'стор попросили перечитать состояние');
});

test('тосты: девять одинаковых причин → один тост', () => {
  resetToastOnce();
  const seen = [];
  for (let i = 0; i < 9; i += 1) {
    toastOnce((t, tone) => seen.push([t, tone]), 'already_decided', 'Это разрешение уже решено');
  }
  assert.equal(seen.length, 1, 'девять плашек подряд — это и был дефект');
  // другая причина проходит отдельно
  toastOnce((t) => seen.push(t), 'network', 'Нет связи');
  assert.equal(seen.length, 2);
});

/* ─────────────────────────────────── 5. «Не актуально» / «Вернуть» тела */

test('тело действия §1: `target:{kind,id}` для всех четырёх видов', () => {
  assert.deepEqual(dismissBody('question', 'q_1'),
    { action: 'dismiss', target: { kind: 'question', id: 'q_1' } });
  assert.deepEqual(restoreBody('decision', 'd_1'),
    { action: 'restore', target: { kind: 'decision', id: 'd_1' } });
  const withReason = dismissBody('approval', 'ap_1', 'работа закончена');
  assert.equal(withReason.reason, 'работа закончена');
  assert.deepEqual(restoreBody('step', 'ns_7'), { action: 'restore', target: { kind: 'step', id: 'ns_7' } });
});

test('стор: «Не актуально» у вопроса уходит одним запросом и меняет состояние', async () => {
  resetRequestStores();
  const sent = [];
  const detail = {
    id: 't_a1b2c3d4', room: 'demo', status: 'done',
    open_questions: [{ id: 'q_1', text: 'Нужен ли раздел про упаковку?', state: 'open' }],
    decisions: [], next_steps: [], approvals: [],
    needs_you_count: { questions: 1, decisions: 0, approvals: 0, total: 1 },
  };
  const api = {
    get: async () => detail,
    post: async (url, body) => { sent.push([url, body]); return { ok: true, question: { id: 'q_1', dismissed: true, state: 'dismissed' } }; },
  };
  const store = createRequestStore({ rid: 't_a1b2c3d4', roomId: 'demo', detail, api });
  await store.targetAction('questions', 'q_1', 'dismiss');
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], '/api/requests/t_a1b2c3d4/actions');
  assert.deepEqual(sent[0][1], { action: 'dismiss', target: { kind: 'question', id: 'q_1' } });
  assert.equal(store.find('questions', 'q_1').dismissed, true);
});

test('стор: 409 при «Вернуть» не бросает исключение (тихо)', async () => {
  resetRequestStores();
  const err = new Error('Вопрос и так ждёт ответа');
  err.status = 409;
  err.code = 'already_decided';
  const detail = { id: 't_a1b2c3d4', room: 'demo', status: 'done', open_questions: [], decisions: [], next_steps: [], approvals: [] };
  const api = { get: async () => detail, post: async () => { throw err; } };
  const store = createRequestStore({ rid: 't_a1b2c3d4', roomId: 'demo', detail, api });
  const res = await store.targetAction('questions', 'q_1', 'restore');
  assert.equal(res.already_decided, true);
});

test('массово: `dismiss_stale` по комнате и число закрытых', async () => {
  resetRequestStores();
  const sent = [];
  const detail = {
    id: 't_a1b2c3d4', room: 'demo', status: 'done',
    open_questions: [{ id: 'q_1', state: 'open' }, { id: 'q_2', state: 'open' }],
    decisions: [{ id: 'd_3', status: 'proposed', open: true }],
    next_steps: [], approvals: closedApprovalsFixture(20),
  };
  const api = {
    get: async () => detail,
    post: async (url, body) => {
      sent.push([url, body]);
      return { ok: true, closed: { questions: 2, decisions: 1, approvals: 0, steps: 0 } };
    },
  };
  const store = createRequestStore({ rid: 't_a1b2c3d4', roomId: 'demo', detail, api });
  const res = await store.dismissStale();
  assert.equal(sent[0][0], '/api/rooms/demo/dismiss_stale');
  assert.deepEqual(sent[0][1], { scope: 'finished_requests' });
  assert.equal(res.closed.questions, 2);
  assert.equal(res.closed.decisions, 1);
});

test('staleSummary: считает только завершённый запрос', () => {
  const closedList = closedApprovalsFixture(20);
  const unfinished = { status: 'working', approvals: closedList,
    open_questions: [{ id: 'q_1' }], decisions: [{ id: 'd_1', status: 'proposed' }] };
  assert.equal(staleSummary(unfinished).total, 0, 'у идущего запроса закрывать нечего');
  assert.equal(isFinishedRequest(unfinished), false);

  const finished = Object.assign({}, unfinished, { status: 'done' });
  const sum = staleSummary(finished, {
    isWaiting: () => false,
    isOpenQuestion: (q) => Boolean(q),
    isOpenDecision: (d) => Boolean(d),
  });
  assert.equal(sum.total, 2, 'вопрос + решение (закрытые разрешения не в счёте)');
  assert.equal(sum.finished, true);
});

/* ─────────────────────────────────── 6. блок разрешений на странице */

test('блок запроса: `Закрыть все неактуальные (N)` у завершённой карточки', () => {
  resetRequestStores();
  const detail = {
    id: 't_a1b2c3d4', room: 'demo', status: 'done',
    approvals: closedApprovalsFixture(20),
    open_questions: [{ id: 'q_1', state: 'open', text: 'Вопрос' }],
    decisions: [{ id: 'd_3', status: 'proposed', open: true, text: 'Решение' }],
    next_steps: [],
    needs_you_count: { questions: 1, decisions: 1, approvals: 0, total: 2 },
  };
  const api = { get: async () => detail, post: async () => ({ ok: true }) };
  const ui = createApprovals({ ctx: { api, room: { id: 'demo' }, toast: () => {} }, rid: 't_a1b2c3d4', roomId: 'demo', detail });
  const block = ui.buildRequestBlock();
  const body = text(block);
  assert.match(body, /Закрыть все неактуальные \(2\)/, 'N — по неактуальным пунктам, не 20');
  assert.match(body, /Закрытые: 20 задач/, 'закрытые — свёрнутым списком');
  assert.match(body, /работа завершена/, 'с причиной');
  // в шапке блока — серверное число (0 разрешений), а не 20
  assert.match(body, /все решены/);
});
