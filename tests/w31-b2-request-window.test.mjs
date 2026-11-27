/**
 * w31-b2-request-window.test.mjs — пакет B2 волны 31: окно задачи.
 *
 * Проверяем то, что видит Михаил (дефекты 25, 26, 27, 30, 31):
 *   * блок статуса в шапке строится по `board_column`/`allowed_moves` сервера:
 *     в «На проверке» — «Принять» (с отменой 10 с) и «Вернуть на доработку»,
 *     в «Готово» — только «Вернуть в работу»;
 *   * «Свод», «Разрешения агентам» и «Проверка критика» — те же сворачиваемые
 *     блоки, что остальные (умолчания + запоминание выбора);
 *   * колонки прокручиваются независимо: колесо/клавиши — колонке под курсором,
 *     вторая стоит;
 *   * «Результаты агентов» показывают имя агента и роль, есть фильтр по агенту.
 *
 * DOM — мини-DOM (tests/mini-dom.mjs), браузер не нужен.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { installMiniDom, resetMiniDom, byClass, byData, textOf, DOC } from './mini-dom.mjs';

installMiniDom();

const {
  buildStatusBlock, boardColumnOf, allowedMovesOf, statusActionsFor, openQuestionsOf,
} = await import('../web/js/views/request-status.js');
const { installColumnScroll, scrollByKey, scrollByWheel, isScrollable } = await import('../web/js/views/column-scroll.js');
const {
  createSection, sectionDefaults, pendingApprovals,
} = await import('../web/js/views/section-collapse.js');
const { renderAgentResults } = await import('../web/js/views/agent-results.js');

/* ─────────────────────────────────────────────────────────── помощники */

const click = (el) => el.dispatch('click', { type: 'click', target: el });

function undoStub() {
  const items = [];
  return {
    items,
    offer(o) { items.push(o); return String(items.length); },
    commit() { return true; },
    revert() { return true; },
  };
}

function statusDetail(extra = {}) {
  return {
    id: 't_1000a001',
    board_column: 'review',
    allowed_moves: ['done', 'working'],
    status: 'done',
    open_questions: [],
    ...extra,
  };
}

/* ─────────────────────────────────────── 1. колонка и допустимые переносы */

test('board_column с сервера — источник правды; без него колонка выводится из stage/status', () => {
  assert.equal(boardColumnOf({ board_column: 'review' }), 'review');
  assert.equal(boardColumnOf({ board_column: 'done' }), 'done');
  // старый сервер: стадии волны 30 → колонки волны 31
  assert.equal(boardColumnOf({ stage: 'accepted' }), 'done');
  assert.equal(boardColumnOf({ stage: 'done_system' }), 'review');
  assert.equal(boardColumnOf({ stage: 'needs_you' }), 'working');
  assert.equal(boardColumnOf({ status: 'done' }), 'review', 'работа готова — ждёт приёмки');
  assert.equal(boardColumnOf({ status: 'failed' }), 'working');
});

test('allowed_moves с сервера уважается; без него — правила контракта', () => {
  assert.deepEqual(allowedMovesOf({ board_column: 'done', allowed_moves: ['working'] }), ['working']);
  assert.deepEqual(allowedMovesOf({ board_column: 'review', allowed_moves: ['done', 'working', 'чушь'] }), ['done', 'working']);
  assert.deepEqual(allowedMovesOf({ board_column: 'review' }), ['done', 'working']);
  assert.deepEqual(allowedMovesOf({ board_column: 'done' }), ['working']);
  assert.deepEqual(allowedMovesOf({ board_column: 'working' }), ['review']);
});

test('кнопки статуса: «Принять» и «Вернуть на доработку» — только из «На проверке»', () => {
  const review = statusActionsFor(statusDetail()).map((a) => a.id);
  assert.deepEqual(review, ['accept', 'rework']);

  const done = statusActionsFor(statusDetail({ board_column: 'done', allowed_moves: ['working'] })).map((a) => a.id);
  assert.deepEqual(done, ['reopen'], 'в «Готово» — только «Вернуть в работу»');

  const working = statusActionsFor(statusDetail({ board_column: 'working', allowed_moves: ['review'] })).map((a) => a.id);
  assert.deepEqual(working, ['send_review']);

  const none = statusActionsFor({ board_column: 'new', allowed_moves: [] });
  assert.deepEqual(none, []);
});

test('открытые вопросы считаются только живые', () => {
  const detail = {
    open_questions: [
      { state: 'open' },
      { state: 'open', answered: true },
      { state: 'closed' },
      {},
    ],
  };
  assert.equal(openQuestionsOf(detail), 2);
});

/* ─────────────────────────────────────── 2. блок статуса в шапке задачи */

test('блок статуса: чип, кнопки по allowed_moves и «Принять» с отменой 10 с', async () => {
  const undo = undoStub();
  const calls = [];
  const block = buildStatusBlock({
    detail: statusDetail(),
    requestId: 't_1000a001',
    undo,
    onMove: async (to, opts) => { calls.push([to, opts]); return { ok: true }; },
    toast() {},
  });

  const chip = byClass(block, 'req-status__chip')[0];
  assert.ok(chip, 'чип статуса есть');
  assert.equal(textOf(chip).trim(), 'На проверке');
  assert.equal(chip.getAttribute('data-board-column'), 'review');

  const buttons = byClass(block, 'req-status__btn');
  assert.deepEqual(buttons.map((b) => b.getAttribute('data-move')), ['done', 'working']);
  assert.equal(textOf(buttons[0]).trim(), 'Принять');
  assert.equal(textOf(buttons[1]).trim(), 'Вернуть на доработку');

  // «Принять» не отправляет запрос сразу: сначала 10 секунд на отмену.
  click(buttons[0]);
  assert.equal(calls.length, 0, 'при нажатии «Принять» запрос ещё не ушёл');
  assert.equal(undo.items.length, 1, 'предложена отмена');
  assert.equal(undo.items[0].text, 'Задача будет принята');
  assert.equal(undo.items[0].key, 'accept:t_1000a001', 'одно действие на задачу');

  await undo.items[0].action();
  assert.deepEqual(calls, [['done', { confirm: true }]], 'после срока — перенос в «Готово»');
});

test('блок статуса: без действий — честная строка, а не пустое место', () => {
  const block = buildStatusBlock({
    detail: { board_column: 'new', allowed_moves: [], status: 'queued' },
    onMove: async () => ({ ok: true }),
    undo: undoStub(),
    toast() {},
  });
  assert.equal(byClass(block, 'req-status__btn').length, 0);
  assert.match(textOf(byClass(block, 'req-status__hint')[0]), /Действий нет/);
});

test('блок статуса: «Принять» при открытых вопросах спрашивает подтверждение', () => {
  const undo = undoStub();
  const block = buildStatusBlock({
    detail: statusDetail({
      open_questions: [{ state: 'open' }, { state: 'open' }],
      board_column: 'review',
      allowed_moves: ['done'],
    }),
    requestId: 't_1000a002',
    undo,
    onMove: async () => ({ ok: true }),
    toast() {},
  });
  click(byClass(block, 'req-status__btn')[0]);
  // Окно подтверждения — не в отмене: сразу 10 секунд на принятие с вопросами нельзя.
  assert.equal(undo.items.length, 0, 'сначала подтверждение, потом отмена');
});

test('блок статуса: у упавшей задачи переносов нет — только честная строка', () => {
  const detail = { status: 'failed', board_column: 'working', allowed_moves: ['review'] };
  assert.deepEqual(statusActionsFor(detail), [], 'переносом упавшую задачу не поднять');
  const block = buildStatusBlock({ detail, onMove: async () => ({ ok: true }), undo: undoStub(), toast() {} });
  assert.equal(byClass(block, 'req-status__btn').length, 0);
  assert.match(textOf(byClass(block, 'req-status__hint')[0]), /повторить/i);
});

/* ─────────────────────────────────── 3. единое сворачивание всех блоков */

test('умолчания: свод открыт, разрешения — когда ждут решения, критика свёрнута', () => {
  const plain = sectionDefaults({ status: 'working' });
  assert.equal(plain.svod, true);
  assert.equal(plain.approvals, false);
  assert.equal(plain.critic, false);
  // прежние блоки не изменились
  assert.equal(plain.thread, false);
  assert.equal(plain.worklog, false);
  assert.equal(plain.tree, false);

  const withApprovals = sectionDefaults({
    status: 'working',
    approvals: [{ state: 'pending' }, { state: 'granted_once' }],
  });
  assert.equal(withApprovals.approvals, true, 'работа стоит — блок раскрыт');
  assert.equal(pendingApprovals({ approvals: [{ state: 'pending' }, { state: 'pending' }] }), 2);
});

test('«Свод» сворачивается тем же компонентом; выбор человека запоминается', () => {
  const store = {
    map: new Map(),
    has(k) { return this.map.has(k); },
    isOpen(k, def) { return this.map.has(k) ? this.map.get(k) === '1' : def === true; },
    set(k, v) { this.map.set(k, v ? '1' : '0'); },
  };
  const body = DOC.createElement('div');
  body.appendChild(DOC.createTextNode('текст свода'));
  const sec = createSection({
    key: 'svod', requestId: 't_1', title: 'Свод', store, defaultOpen: true, card: false, content: () => [body],
  });
  assert.equal(sec.isOpen(), true);
  assert.equal(byClass(sec.element, 'sec__toggle')[0].getAttribute('aria-expanded'), 'true');

  sec.collapse();
  assert.equal(sec.isOpen(), false);
  assert.equal(store.map.get('svod'), '0', 'состояние записано');

  const again = createSection({
    key: 'svod', requestId: 't_1', title: 'Свод', store, defaultOpen: true, card: false, content: () => [],
  });
  assert.equal(again.isOpen(), false, 'перерисовка страницы помнит выбор');
});

/* ───────────────────────────────────── 4. независимая прокрутка колонок */

test('scrollByKey: PageUp/PageDown/Home/End двигают только свою колонку', () => {
  const col = DOC.createElement('div');
  col.clientHeight = 400;
  col.scrollHeight = 1200;
  assert.equal(isScrollable(col), true);
  assert.equal(scrollByKey(col, 'PageDown'), true);
  assert.equal(col.scrollTop, 360, '90% высоты окна колонки');
  assert.equal(scrollByKey(col, 'Home'), true);
  assert.equal(col.scrollTop, 0);
  assert.equal(scrollByKey(col, 'End'), true);
  assert.equal(col.scrollTop, 800);

  const short = DOC.createElement('div');
  short.clientHeight = 500;
  short.scrollHeight = 300;
  assert.equal(isScrollable(short), false);
  assert.equal(scrollByKey(short, 'End'), false, 'короткую колонку не трогаем');
});

test('scrollByWheel: колесо двигает колонку, пока есть что прокручивать', () => {
  const col = DOC.createElement('div');
  col.clientHeight = 300;
  col.scrollHeight = 900;
  assert.equal(scrollByWheel(col, 120), true);
  assert.equal(col.scrollTop, 120);
});

test('колонка под курсором: клавиши прокручивают её, вторая стоит', () => {
  const root = DOC.createElement('div');
  const main = DOC.createElement('div');
  main.className = 'req__main';
  main.clientHeight = 400; main.scrollHeight = 2000;
  const aside = DOC.createElement('aside');
  aside.className = 'req__aside';
  aside.clientHeight = 400; aside.scrollHeight = 1400;
  const inner = DOC.createElement('div');
  aside.appendChild(inner);
  root.append(main, aside);

  // Подставной документ: mini-DOM сам события не рассылает, поэтому ловим обработчик.
  const handlers = {};
  const doc = {
    addEventListener(type, fn) { handlers[type] = fn; },
    removeEventListener(type) { delete handlers[type]; },
  };
  const ctl = installColumnScroll({ root, doc });

  // Курсор над правой колонкой → PageDown прокручивает её, левая стоит.
  root.dispatch('mouseover', { target: inner });
  assert.equal(ctl.hovered(), 'aside');
  let prevented = false;
  handlers.keydown({ key: 'PageDown', preventDefault() { prevented = true; } });
  assert.equal(aside.scrollTop, 360);
  assert.equal(main.scrollTop, 0, 'вторая колонка стоит');
  assert.equal(prevented, true);

  // Курсор над основной → колесо и клавиши действуют на неё.
  root.dispatch('mouseover', { target: main });
  root.dispatch('wheel', { target: main, deltaY: 100, preventDefault() {} });
  assert.equal(main.scrollTop, 100);
  assert.equal(aside.scrollTop, 360, 'правая не сдвинулась');

  // В поле ввода клавиши не перехватываем.
  root.dispatch('mouseover', { target: aside });
  const before = aside.scrollTop;
  handlers.keydown({ key: 'PageDown', target: { localName: 'textarea' } });
  assert.equal(aside.scrollTop, before);

  ctl.destroy();
  assert.equal(handlers.keydown, undefined, 'обработчик снят вместе со страницей');
});

/* ─────────────────────────────────── 5. «Результаты агентов»: имя и фильтр */

function agentDetail() {
  return {
    id: 't_1000a001',
    room: 'cafe',
    status: 'done',
    agent_results: {
      counts: { files: 1, remaining: 1 },
      agents: [
        { agent: 'team-lawyer', agent_name: 'Анна', agent_role: 'исполнитель', title: 'Юр-блок', status: 'done', verdict: 'ИП зарегистрирован', files: [], card_id: 't_a1' },
        { agent: 'team-ops', agent_name: 'Марина', agent_role: 'исполнитель', title: 'Поставщики', status: 'done', verdict: 'Три котировки', files: [{ path: 'C:/demo/a.md', name: 'a.md' }], card_id: 't_a2' },
        { agent: 'team-strategy', agent_name: 'Атлас', agent_role: 'критик', title: 'Бренд', status: 'review', verdict: 'Ждёт ответа', remaining: 'проверить знак', files: [], card_id: 't_a3' },
      ],
    },
  };
}

test('результаты агентов: имя участника и роль, а не «Исполнитель-воркер»', () => {
  const block = renderAgentResults(agentDetail(), { toast() {}, api: { get: async () => ({}) } }, { defaultOpen: true });
  const names = byClass(block, 'agr__person-name').map((el) => textOf(el).trim());
  assert.deepEqual(names, ['Анна', 'Марина', 'Атлас']);
  const roles = byClass(block, 'agr__role').map((el) => textOf(el).trim());
  assert.ok(roles.includes('критик'), 'роль видна отдельно от имени');
});

test('результаты агентов: фильтр по агенту оставляет только его строки', () => {
  const block = renderAgentResults(agentDetail(), { toast() {}, api: { get: async () => ({}) } }, { defaultOpen: true });
  const select = byClass(block, 'agr__agent-filter')[0];
  assert.ok(select, 'фильтр по агенту есть');
  assert.equal(select.childNodes.length, 4, '«Все агенты» + три имени');

  select.value = 'team-ops';
  select.dispatch('change', { type: 'change', target: select });

  const rows = byClass(block, 'agr__row');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].getAttribute('data-agent'), 'team-ops');
  assert.match(textOf(rows[0]), /Марина/);
});

test('результаты агентов: строки без agent_name не теряются (старый сервер)', () => {
  const detail = agentDetail();
  detail.agent_results.agents = [{ agent: 'team-ops', role: 'исполнитель', title: 'Поставщики', status: 'done', verdict: 'Есть', card_id: 't_a9' }];
  const block = renderAgentResults(detail, { toast() {}, api: { get: async () => ({}) } }, { defaultOpen: true });
  const name = textOf(byClass(block, 'agr__person-name')[0]).trim();
  assert.ok(name.length > 0, 'подпись не пустая');
  assert.equal(byClass(block, 'agr__agent-filter').length, 0, 'один агент — фильтр не нужен');
});

resetMiniDom();
