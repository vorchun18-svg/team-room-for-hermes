/**
 * request-store.test.mjs — «одно действие — везде» (волна 26, пакет D2).
 *
 * Замечание Михаила (Prep §10): решение, принятое в одном месте страницы, должно
 * мгновенно менять ВСЕ остальные — основной блок, правую колонку, нижний лист,
 * «Сегодня» и бейджи. Проверяем это на четырёх видах пунктов, которые
 * показываются в нескольких местах: разрешение, вопрос, решение, шаг.
 *
 * Два «места страницы» здесь — это два независимых вида (`createApprovals` +
 * живые участки на `bindLive`), которые читают ОДИН стор. Действие делается
 * один раз, в одном месте.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, FakeElement } from './fake-dom.mjs';

/* ──────────────────────────────── мини-DOM: то, что нужно этим видам ─────── */

installFakeDom();
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
    const i = this.childNodes.indexOf(ref);
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, node);
    node.parentNode = this;
    return node;
  };
}
if (typeof P.querySelector !== 'function') P.querySelector = function querySelector() { return null; };
if (typeof P.querySelectorAll !== 'function') P.querySelectorAll = function querySelectorAll() { return []; };

const { createApprovals, isWaiting, waitingApprovals, floorApprovals, approvalState } =
  await import('../web/js/views/approvals.js');
const {
  createRequestStore, requestStoreFor, resetRequestStores, bindLive, REQUEST_CHANGED,
  slicesSignature, sliceOfDetail,
} = await import('../web/js/views/request-store.js');

/* ───────────────────────────────────────────────────────────── фикстуры ──── */

const rid = 't_5c8e4a02';

function approval(extra = {}) {
  return Object.assign({
    id: 'ap_once01', state: 'pending', kind: 'terminal', command: 'python build.py',
    description: 'запуск команды', member_title: 'Анна', floor: false, grantable: true,
    request_id: rid,
  }, extra);
}

function detailWith(items = {}) {
  return Object.assign({
    id: rid, room: 'cafe',
    approvals: [],
    open_questions: [],
    decisions: [],
    next_steps: [],
  }, items);
}

function text(node) {
  let out = '';
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 3) { out += n.data; return; }
    for (const child of n.childNodes || []) walk(child);
  };
  walk(node);
  return out;
}

function byAttr(root, attr, value) {
  const out = [];
  const walk = (n) => {
    for (const child of n.childNodes || []) {
      if (!child || child.nodeType !== 1) continue;
      if (child.getAttribute && child.getAttribute(attr) === value) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

/** Простое «место страницы»: подписалось на стор и рисует текст среза. */
function place(store, name, format) {
  const node = new FakeElement('div');
  const off = bindLive(store, node, () => {
    node.replaceChildren(new (class extends FakeElement {})( 'span'));
    node.childNodes = [];
    const span = new FakeElement('span');
    span.appendChild({ nodeType: 3, data: format(store.get()) });
    node.appendChild(span);
  });
  node.setAttribute('data-place', name);
  return { node, off, text: () => text(node) };
}

/* ────────────────────────────────────────────── 1. один стор на запрос ───── */

test('стор один на запрос: два места получают один и тот же объект', () => {
  resetRequestStores();
  const a = requestStoreFor(rid, { roomId: 'cafe' });
  const b = requestStoreFor(rid, { roomId: 'cafe' });
  assert.equal(a, b, 'сколько бы мест ни звало — стор один');
  const other = requestStoreFor('t_00000001', {});
  assert.notEqual(a, other, 'другой запрос — другой стор');
});

test('деталь страницы раскладывается на четыре среза и не теряет пустые', () => {
  const detail = detailWith({
    approvals: [approval()],
    open_questions: [{ id: 'q_1', text: 'Какой срок?', answer_state: 'open' }],
    decisions: [{ id: 'd_1', text: 'Берём подрядчика', status: 'proposed', open: true }],
    next_steps: [{ id: 's_1', title: 'Собрать смету', state: 'open' }],
  });
  const store = createRequestStore({ rid, detail });
  const snap = store.get();
  assert.equal(snap.approvals.length, 1);
  assert.equal(snap.questions.length, 1);
  assert.equal(snap.decisions.length, 1);
  assert.equal(snap.steps.length, 1);
  assert.equal(slicesSignature(sliceOfDetail(null)).includes('0'), true);
});

/* ───────────────────── 2. разрешение: решение в одном месте — везде ──────── */

test('разрешение: «Разрешить один раз» в колонке меняет и основной блок', async () => {
  resetRequestStores();
  const sent = [];
  const api = {
    post: async (url, body) => {
      sent.push({ url, body });
      return { ok: true, approval: { state: 'granted_once' }, applied: ['ap_once01'] };
    },
  };
  const detail = detailWith({ approvals: [approval()] });
  // Место 1 — блок «Разрешения агентам» на странице запроса.
  const main = createApprovals({ ctx: { api, toast: () => {} }, rid, roomId: 'cafe', detail });
  // Место 2 — правая колонка «Нужен ваш ответ» (свой экземпляр вида).
  const aside = createApprovals({ ctx: { api, toast: () => {} }, rid, roomId: 'cafe', detail });

  const block = main.buildRequestBlock();
  const asideCard = aside.pendingCards()[0];
  assert.match(text(block), /ждёт вашего решения/);
  assert.match(text(asideCard), /ждёт вашего решения/);

  const btn = byAttr(asideCard, 'data-decision', 'once')[0];
  btn.dispatch('click', { type: 'click', target: btn });
  await tick();
  await tick();

  // Один запрос — тело по контракту W22 §2.2.
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body, { action: 'approval', approval_id: 'ap_once01', decision: 'once' });
  // Оба места показывают итог без перезагрузки страницы.
  assert.match(text(block), /Разрешено один раз/);
  assert.equal(waitingApprovals(main.pending()).length, 0);
  assert.equal(waitingApprovals(aside.pending()).length, 0);
  assert.equal(main.store.get().approvals[0].state, 'granted_once');
});

test('«нельзя штатно»: подсказка участнику в колонке — сразу и в основном блоке', async () => {
  resetRequestStores();
  const sent = [];
  const api = {
    post: async (url, body) => { sent.push(body); return { ok: true, approval: { state: 'hinted' } }; },
  };
  const detail = detailWith({ approvals: [approval({ id: 'ap_code01', kind: 'execute_code', grantable: false })] });
  const main = createApprovals({ ctx: { api, toast: () => {} }, rid, roomId: 'cafe', detail });
  const aside = createApprovals({ ctx: { api, toast: () => {} }, rid, roomId: 'cafe', detail });
  const block = main.buildRequestBlock();
  const card = aside.pendingCards()[0];

  const hint = byAttr(card, 'data-action', 'approval-hint_agent')[0];
  assert.ok(hint, 'у «нельзя штатно» есть действенная кнопка подсказки');
  hint.dispatch('click', { type: 'click', target: hint });
  await tick();
  await tick();

  assert.deepEqual(sent[0], { action: 'hint_agent', approval_id: 'ap_code01' });
  assert.match(text(card), /Подсказка отправлена участнику/);
  // Основной блок пересобран из стора: чип состояния уже новый (это и есть
  // «одно действие — везде»: второе место не ждёт опроса страницы).
  assert.match(text(block), /подсказано участнику/);
  assert.equal(waitingApprovals(aside.pending()).length, 0, 'пункт больше не висит на человеке');
});

/* ───────────────────────────── 3. вопрос, решение, шаг — так же ──────────── */

test('вопрос: ответ из одного места виден во всех местах страницы', async () => {
  resetRequestStores();
  const api = {
    post: async () => ({ ok: true, question: { id: 'q_1', answered: true, answer_state: 'sent' } }),
  };
  const detail = detailWith({ open_questions: [{ id: 'q_1', text: 'Какой срок?', answer_state: 'open' }] });
  const store = createRequestStore({ rid, detail, api });
  const a = place(store, 'main', (s) => s.questions.map((q) => q.answer_state).join(','));
  const b = place(store, 'aside', (s) => s.questions.map((q) => q.answer_state).join(','));
  assert.equal(a.text(), 'open');
  assert.equal(b.text(), 'open');

  await store.answerQuestion(store.get().questions[0], { text: 'Две недели' });
  assert.equal(a.text(), 'sent');
  assert.equal(b.text(), 'sent');
  // Деталь страницы (её читают остальные модули) тоже обновлена.
  assert.equal(detail.open_questions[0].answer_state, 'sent');
});

test('решение: «Принять» в нижнем листе убирает строку и в колонке', async () => {
  resetRequestStores();
  const api = { post: async () => ({ ok: true, decision: { id: 'd_1', open: false, status: 'accepted' } }) };
  const detail = detailWith({ decisions: [{ id: 'd_1', text: 'Берём подрядчика', status: 'proposed', open: true }] });
  const store = createRequestStore({ rid, detail, api });
  const main = place(store, 'main', (s) => 'открытых: ' + s.decisions.filter((d) => d.open).length);
  const aside = place(store, 'aside', (s) => 'открытых: ' + s.decisions.filter((d) => d.open).length);
  assert.equal(main.text(), 'открытых: 1');

  await store.decisionAction(store.get().decisions[0], { outcome: 'accept' });
  assert.equal(main.text(), 'открытых: 0');
  assert.equal(aside.text(), 'открытых: 0');
});

test('шаг: «Убрать» в колонке виден в основном блоке', async () => {
  resetRequestStores();
  const api = { post: async () => ({ ok: true, step: { id: 's_1', state: 'dismissed', state_label: 'убран' } }) };
  const detail = detailWith({ next_steps: [{ id: 's_1', title: 'Собрать смету', state: 'open' }] });
  const store = createRequestStore({ rid, detail, api });
  const main = place(store, 'main', (s) => s.steps.map((x) => x.state).join(','));
  const aside = place(store, 'aside', (s) => s.steps.map((x) => x.state).join(','));
  await store.stepAction(store.get().steps[0], { action: 'step_drop' });
  assert.equal(main.text(), 'dismissed');
  assert.equal(aside.text(), 'dismissed');
});

/* ─────────────────────────────── 4. честность: откат и ответ сервера ────── */

test('ошибка сервера откатывает оптимистичную правку во всех местах', async () => {
  resetRequestStores();
  const api = { post: async () => { const e = new Error('нельзя'); e.status = 422; throw e; } };
  const detail = detailWith({ approvals: [approval()] });
  const store = createRequestStore({ rid, detail, api });
  const place1 = place(store, 'main', (s) => approvalState(s.approvals[0]));
  const place2 = place(store, 'aside', (s) => approvalState(s.approvals[0]));
  await assert.rejects(() => store.decideApproval(store.get().approvals[0], 'once'));
  assert.equal(place1.text(), 'pending', 'правка откатилась');
  assert.equal(place2.text(), 'pending');
  assert.equal(store.get().error.status, 422);
});

test('ответ сервера главнее: деталь целиком заменяет оптимистичное состояние', () => {
  resetRequestStores();
  const detail = detailWith({ approvals: [approval()] });
  const store = createRequestStore({ rid, detail });
  store.patch('approvals', 'ap_once01', { state: 'granted_always' });
  assert.equal(store.get().approvals[0].state, 'granted_always');
  store.setDetail(detailWith({ approvals: [approval({ state: 'denied', decided_at: 1 })] }),
    { source: 'server', force: true });
  assert.equal(store.get().approvals[0].state, 'denied');
});

/* ───────────────────── 5. «без тупиков»: счётчики и жёсткий запрет ──────── */

test('жёсткий запрет — информация: не ждёт человека и не идёт в счётчик', () => {
  const items = [
    approval({ id: 'ap_ok', grantable: true }),
    approval({ id: 'ap_code', kind: 'execute_code', grantable: false }),
    approval({ id: 'ap_floor', floor: true, grantable: false }),
  ];
  assert.equal(items.filter(isWaiting).length, 2, 'в счётчике только то, что можно решить');
  assert.equal(waitingApprovals(items).length, 2);
  assert.equal(floorApprovals(items).length, 1);
  assert.equal(approvalState(items[2]), 'pending', 'состояние остаётся исходным');
  assert.equal(isWaiting(items[2]), false, 'но в «ждёт вашего решения» его нет');
});

/* ───────────────────── 6. подписка, событие и отсутствие утечек ─────────── */

test('событие teamroom:request-changed несёт id запроса и версию', () => {
  resetRequestStores();
  const seen = [];
  const doc = globalThis.document;
  const had = typeof globalThis.CustomEvent === 'function';
  class TestEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
  if (!had) globalThis.CustomEvent = TestEvent;
  const oldDispatch = doc.dispatchEvent;
  doc.dispatchEvent = (ev) => { seen.push(ev); return true; };
  try {
    const store = createRequestStore({ rid, detail: detailWith({ approvals: [approval()] }) });
    store.patch('approvals', 'ap_once01', { state: 'dismissed' });
    assert.equal(seen.length, 1);
    assert.equal(seen[0].type, REQUEST_CHANGED);
    assert.equal(seen[0].detail.rid, rid);
    assert.ok(seen[0].detail.rev > 0);
  } finally {
    doc.dispatchEvent = oldDispatch;
    if (!had) delete globalThis.CustomEvent;
  }
});

test('узел убрали со страницы — подписка снимается сама', () => {
  resetRequestStores();
  const store = createRequestStore({ rid, detail: detailWith({ approvals: [approval()] }) });
  let paints = 0;
  const node = new FakeElement('div');
  node.isConnected = true;
  bindLive(store, node, () => { paints += 1; });
  assert.equal(paints, 1, 'первая отрисовка сразу');
  store.patch('approvals', 'ap_once01', { state: 'hinted' });
  assert.equal(paints, 2);
  node.isConnected = false;
  store.patch('approvals', 'ap_once01', { state: 'dismissed' });
  assert.equal(paints, 2, 'узел вне документа больше не перерисовывается');
});

test('двойное нажатие не отправляет второй запрос', async () => {
  resetRequestStores();
  let calls = 0;
  let release = null;
  const api = { post: () => { calls += 1; return new Promise((r) => { release = r; }); } };
  const detail = detailWith({ approvals: [approval()] });
  const store = createRequestStore({ rid, detail, api });
  const first = store.decideApproval(store.get().approvals[0], 'once');
  await assert.rejects(() => store.decideApproval(store.get().approvals[0], 'once'),
    (err) => err.code === 'busy');
  release({ ok: true, approval: { state: 'granted_once' } });
  await first;
  assert.equal(calls, 1);
});
