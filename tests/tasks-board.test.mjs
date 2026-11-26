/**
 * tasks-board.test.mjs — доска «Задачи» комнаты (W30-D2).
 *
 * Проверяет без браузера:
 *   • колонки доски = стадии контракта §3, в контрактном порядке, с подписями;
 *   • счётчик колонки = числу карточек в ней (и в доске, и в списке);
 *   • перенос карточки — только существующее действие сервера (`accept`,
 *     `reopen_accepted`); остальные переходы честно отказывают с причиной;
 *   • клавиатура ведёт только туда, где действие есть;
 *   • «Принять» в «Готово» ОТЛОЖЕНО на 10 секунд общим `undo.js`: до конца
 *     отсчёта запроса на сервер нет, «Вернуть» его не отправляет, по истечении
 *     срока действие уходит ровно один раз;
 *   • «Принята» свёрнута по умолчанию, счётчик виден;
 *   • пустое состояние объясняет экран и даёт кнопку (без тупиков);
 *   • никакого `innerHTML`: весь текст приходит через `h()`.
 *
 * Данные вымышленные (кофейня, химчистка, грузоперевозки), как требуют правила волны.
 *
 *   node --test tests/tasks-board.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installMiniDom, resetMiniDom, DOC, byData, byClass, textOf, tick } from './mini-dom.mjs';

installMiniDom();

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const {
  BOARD_COLUMNS, STAGE_LABEL, ageText, boardColumns, boardCounts, boardSummary, isArchived,
  keyboardTarget, listGroups, moveOutcome, normalizeTask, normalizeTasks, pluralRu, sortTasks, stageOf,
  stepsPercent, stepsText, taskHref, waitingOf,
} = await import('../web/js/views/tasks-model.js');
const { renderBoard, renderList, mount } = await import('../web/js/views/tasks-board.js');
const { sharedUndo, resetSharedUndo } = await import('../web/js/undo.js');
const { h } = await import('../web/js/util.js');

/** Фиксированное «сейчас»: 25 ноября 2026, 23:00 (локально). */
const NOW = new Date(2026, 10, 25, 23, 0, 0).getTime();
const NOW_S = Math.floor(NOW / 1000);

const task = (over = {}) => normalizeTask({
  id: 't_aaaa1111',
  title: 'Задача',
  stage: 'new',
  status: 'queued',
  assignee: '',
  steps_done: 0,
  steps_total: 0,
  needs_you: { count: 0, blocking: false },
  accepted: false,
  parent_id: null,
  children_count: 0,
  updated_at: NOW_S - 600,
  summary_short: '',
  ...over,
});

const TASKS = [
  task({ id: 't_0000a001', title: 'График поставок упаковки', stage: 'new', steps_total: 4 }),
  task({ id: 't_0000a002', title: 'Себестоимость меню', stage: 'working', assignee: 'analyst',
    steps_done: 2, steps_total: 5 }),
  task({ id: 't_0000a003', title: 'Выбор поставщика зерна', stage: 'needs_you',
    needs_you: { count: 2, blocking: true } }),
  task({ id: 't_0000a004', title: 'Состав меню на декабрь', stage: 'needs_you',
    needs_you: { count: 4, blocking: false } }),
  task({ id: 't_0000a005', title: 'Смета на ремонт витрины', stage: 'review', steps_done: 4, steps_total: 4 }),
  task({ id: 't_0000a006', title: 'Дорожная карта квартала', stage: 'done_system', steps_done: 5, steps_total: 5 }),
  task({ id: 't_0000a007', title: 'Переезд склада', stage: 'accepted', accepted: true }),
  task({ id: 't_0000a008', title: 'Старый вариант вывески', stage: 'archived' }),
];

const OPTS = (over = {}) => ({
  roomId: 'cafe',
  nowMs: NOW,
  onOpen: () => {},
  onMove: () => {},
  onBlocked: (reason) => { throw new Error('неожиданный отказ: ' + reason); },
  onAccept: () => {},
  onReopen: () => {},
  onCancel: () => {},
  onCreate: () => {},
  onToggleAccepted: () => {},
  ...over,
});

/* ─────────────────────────────────────────────────────────────── колонки */

test('колонки доски — стадии контракта §3, в контрактном порядке', () => {
  assert.deepEqual(BOARD_COLUMNS.map((c) => c.id),
    ['new', 'working', 'needs_you', 'review', 'done_system', 'accepted']);
  for (const col of BOARD_COLUMNS) {
    assert.equal(col.title, STAGE_LABEL[col.id], 'подпись колонки ' + col.id + ' из словаря стадий');
    assert.ok(col.hint, 'у колонки ' + col.id + ' есть пояснение');
    assert.ok(col.empty, 'у колонки ' + col.id + ' есть текст пустого состояния');
  }
});

test('счётчики колонок = числу карточек (архив не на доске)', () => {
  const cols = boardColumns(TASKS);
  const total = cols.reduce((sum, c) => sum + c.count, 0);
  assert.equal(total, TASKS.length - 1, 'архивная задача не считается на доске');
  for (const col of cols) assert.equal(col.count, col.items.length, 'колонка ' + col.id);
  assert.deepEqual(boardCounts(TASKS), {
    new: 1, working: 1, needs_you: 2, review: 1, done_system: 1, accepted: 1,
  });
  const summary = boardSummary(TASKS);
  assert.equal(summary.total, 7);
  assert.equal(summary.waiting, 2);
  assert.equal(summary.blocking, 1);
  assert.equal(summary.ready, 1);
  assert.equal(summary.accepted, 1);
  assert.equal(summary.archived, 1);
});

test('порядок в колонке: блокирующее — выше, затем свежие', () => {
  const cols = boardColumns(TASKS);
  const needs = cols.find((c) => c.id === 'needs_you');
  assert.deepEqual(needs.items.map((t) => t.id), ['t_0000a003', 't_0000a004']);
  const newer = sortTasks([
    task({ id: 't_0000b001', updated_at: NOW_S - 3600 }),
    task({ id: 't_0000b002', updated_at: NOW_S - 60 }),
  ]);
  assert.deepEqual(newer.map((t) => t.id), ['t_0000b002', 't_0000b001']);
});

test('неизвестная стадия не теряет карточку: считаем «Новой»', () => {
  assert.equal(stageOf({ stage: 'wat' }), 'new');
  assert.equal(stageOf({ stage: '', accepted: true }), 'accepted');
  assert.equal(stageOf({}), 'new');
  assert.equal(isArchived({ stage: 'archived' }), true);
  assert.equal(isArchived({ stage: 'new' }), false);
});

test('W30-QA1: «ждут вашего ответа» в шапке = карточки колонки «Ждёт вас»', () => {
  // У готовой задачи остаются НЕОБЯЗАТЕЛЬНЫЕ пункты (например, пакет решений):
  // работа по ней уже не ждёт, поэтому шапка не должна считать её «ждёт вас».
  const list = [
    task({ id: 't_0000c001', stage: 'needs_you', needs_you: { count: 2, blocking: true } }),
    task({ id: 't_0000c002', stage: 'done_system', needs_you: { count: 3, blocking: false } }),
    task({ id: 't_0000c003', stage: 'done_system', needs_you: { count: 0, blocking: false } }),
  ];
  const cols = boardColumns(list);
  const needs = cols.find((c) => c.id === 'needs_you');
  const summary = boardSummary(list);
  assert.equal(summary.waiting, needs.count, 'шапка спорит с колонкой «Ждёт вас»');
  assert.equal(summary.waiting, 1);
  assert.equal(summary.ready, 2, '«готовы к приёмке» = карточки колонки «Готово»');
  assert.equal(summary.total, cols.reduce((sum, c) => sum + c.count, 0), 'всего = карточки на доске');
});

/* ─────────────────────────────────────────────────────────────── переносы */

test('перенос = только действия сервера: accept и reopen_accepted', () => {
  assert.deepEqual(moveOutcome('done_system', 'accepted'), { ok: true, action: 'accept' });
  assert.deepEqual(moveOutcome('accepted', 'working'), { ok: true, action: 'reopen_accepted' });

  const allowed = new Set();
  for (const from of BOARD_COLUMNS) {
    for (const to of BOARD_COLUMNS) {
      const outcome = moveOutcome(from.id, to.id);
      if (outcome.ok) {
        allowed.add(outcome.action);
        assert.notEqual(from.id, to.id, 'перенос в ту же колонку не действие');
      } else {
        assert.ok(outcome.reason, 'у отказа ' + from.id + '→' + to.id + ' есть причина');
      }
    }
  }
  assert.deepEqual([...allowed].sort(), ['accept', 'reopen_accepted'],
    'интерфейс не придумывает действий, которых нет у сервера');
});

test('честные отказы: принять можно только из «Готово», принятую — только в работу', () => {
  const wrongAccept = moveOutcome('working', 'accepted');
  assert.equal(wrongAccept.ok, false);
  assert.match(wrongAccept.reason, /Готово/);

  const parked = moveOutcome('accepted', 'done_system');
  assert.equal(parked.ok, false);
  assert.match(parked.reason, /вернуть в работу/i);

  const forward = moveOutcome('new', 'working');
  assert.equal(forward.ok, false);
  assert.match(forward.reason, /агент/i);

  assert.equal(moveOutcome('new', 'new').noop, true);
  assert.equal(moveOutcome('new', 'nope').ok, false);
});

test('клавиатура ведёт только в колонку, где действие есть', () => {
  assert.equal(keyboardTarget('done_system', 1), 'accepted');
  assert.equal(keyboardTarget('accepted', -1), 'working');
  assert.equal(keyboardTarget('new', 1), null, 'из «Новой» вправо переносить нечего');
  assert.equal(keyboardTarget('nope', 1), null);
});

/* ─────────────────────────────────────────────────────────────── подписи */

test('что ждёт и от кого — человеческой строкой', () => {
  assert.deepEqual(waitingOf(task({ stage: 'needs_you', needs_you: { count: 2, blocking: true } })),
    { text: 'ждёт вас: 2 вопроса — работа стоит', tone: 'danger', blocking: true });
  assert.equal(waitingOf(task({ stage: 'needs_you', needs_you: { count: 1, blocking: false } })).text,
    'ждёт вас: 1 вопрос');
  assert.equal(waitingOf(task({ stage: 'review' })).text, 'на проверке у критика');
  assert.equal(waitingOf(task({ stage: 'working', assignee: 'lawyer' })).text, 'в работе у «lawyer»');
  assert.equal(waitingOf(task({ stage: 'done_system' })).text, 'готово — примите итог');
  assert.equal(waitingOf(task({ stage: 'accepted', accepted: true })).text, 'принято вами — заморожено');
  // W30-QA1: принятая задача заморожена — даже если в данных остались пункты
  // закрытой работы, карточка не говорит «ждёт вас».
  assert.equal(waitingOf(task({ stage: 'accepted', accepted: true, needs_you: { count: 3, blocking: true } })).text,
    'принято вами — заморожено');
});

test('шаги, возраст и ссылка на задачу', () => {
  assert.equal(stepsText(task({ steps_done: 2, steps_total: 5 })), 'шаги 2 из 5');
  assert.equal(stepsText(task({ steps_total: 0 })), '');
  assert.equal(stepsPercent(task({ steps_done: 1, steps_total: 4 })), 25);
  assert.equal(stepsPercent(task({ steps_done: 9, steps_total: 4 })), 100);

  assert.equal(ageText(NOW_S - 30, NOW), 'только что');
  assert.equal(ageText(NOW_S - 300, NOW), '5 мин');
  assert.equal(ageText(NOW_S - 7200, NOW), '2 ч');
  assert.equal(ageText(NOW_S - 2 * 86400, NOW), '2 дн');
  assert.equal(ageText(0, NOW), '');

  assert.equal(taskHref('cafe', 't_0000a001'), '#/room/cafe/request/t_0000a001');
});

test('сводка на доске согласует числа по-русски', () => {
  assert.equal(pluralRu(1, ['готова', 'готовы', 'готово']), 'готова');
  assert.equal(pluralRu(2, ['готова', 'готовы', 'готово']), 'готовы');
  assert.equal(pluralRu(5, ['готова', 'готовы', 'готово']), 'готово');
  assert.equal(pluralRu(11, ['задача', 'задачи', 'задач']), 'задач');
  assert.equal(pluralRu(21, ['задача', 'задачи', 'задач']), 'задача');
  assert.equal(pluralRu(0, ['задача', 'задачи', 'задач']), 'задач');
});

/* ─────────────────────────────────────────────────────────────── отрисовка */

test('доска: 6 колонок, счётчик = карточки, «Принять» только в «Готово»', () => {
  resetMiniDom();
  const board = renderBoard(TASKS, OPTS());
  DOC.body.appendChild(board);

  const cols = byData(board, 'data-column');
  assert.equal(cols.length, 6, 'все колонки на месте, включая пустые');

  const expected = new Map(boardColumns(TASKS).map((c) => [c.id, c.count]));
  for (const col of cols) {
    const id = col.getAttribute('data-column');
    const cards = byData(col, 'data-task');
    const countNode = col.querySelector('.tasks-bcol__count');
    const shown = Number(textOf(countNode));
    assert.equal(shown, expected.get(id), 'счётчик колонки ' + id + ' = числу карточек');
    if (!col.className.includes('tasks-bcol--collapsed')) {
      assert.equal(cards.length, shown, 'не в свёрнутой колонке ' + id + ' карточек столько же');
    }
  }

  const acceptBtns = byClass(board, 'tasks-card__btn').filter((b) => textOf(b) === 'Принять');
  assert.equal(acceptBtns.length, 1, 'кнопка «Принять» — ровно у готовой задачи');
  const doneCard = byData(board, 'data-task', 't_0000a006')[0];
  assert.ok(doneCard, 'готовая задача на доске');
  assert.ok(textOf(doneCard).includes('готово — примите итог'));

  const reopenBtns = byClass(board, 'tasks-card__btn').filter((b) => textOf(b) === 'Вернуть в работу');
  assert.equal(reopenBtns.length, 0, 'принятая колонка свёрнута — кнопка возврата появится при раскрытии');

  resetMiniDom();
  const opened = renderBoard(TASKS, OPTS({ acceptedCollapsed: false }));
  const reopenOpen = byClass(opened, 'tasks-card__btn').filter((b) => textOf(b) === 'Вернуть в работу');
  assert.equal(reopenOpen.length, 1, 'кнопка «Вернуть в работу» — у принятой задачи');
});

test('«Принята» свёрнута по умолчанию, счётчик виден и раскрывается', () => {
  resetMiniDom();
  const board = renderBoard(TASKS, OPTS());
  const accCol = byData(board, 'data-column', 'accepted')[0];
  assert.ok(accCol.className.includes('tasks-bcol--collapsed'), 'колонка свёрнута');
  assert.equal(textOf(accCol.querySelector('.tasks-bcol__count')), '1', 'счётчик виден в свёрнутой колонке');
  assert.equal(byData(accCol, 'data-task').length, 0, 'карточки скрыты');
  const toggle = accCol.querySelector('.tasks-bcol__toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');

  resetMiniDom();
  const opened = renderBoard(TASKS, OPTS({ acceptedCollapsed: false }));
  const accOpen = byData(opened, 'data-column', 'accepted')[0];
  assert.equal(byData(accOpen, 'data-task').length, 1, 'раскрытая колонка показывает карточку');
});

test('пустая колонка объясняет, зачем она, а не молчит', () => {
  resetMiniDom();
  const board = renderBoard([task({ id: 't_0000c001', stage: 'new' })], OPTS());
  const review = byData(board, 'data-column', 'review')[0];
  const empty = review.querySelector('.tasks-bcol__empty');
  assert.ok(empty, 'пустая колонка имеет пояснение');
  assert.match(textOf(empty), /Итог|проверяют/i);
});

test('пустая доска: зачем экран и кнопка «Поставить задачу»', () => {
  resetMiniDom();
  let created = 0;
  const board = renderBoard([], OPTS({ onCreate: () => { created += 1; } }));
  DOC.body.appendChild(board);
  assert.match(textOf(board), /Задач пока нет/);
  const btn = byClass(board, 'btn')[0];
  assert.ok(btn, 'у пустого состояния есть рабочая кнопка');
  btn.dispatch('click', {});
  assert.equal(created, 1);
});

test('список: каждая задача один раз, ссылки ведут на страницу задачи', () => {
  resetMiniDom();
  const list = renderList(TASKS, OPTS());
  DOC.body.appendChild(list);
  const rows = byData(list, 'data-task');
  assert.equal(rows.length, 7, 'все неархивные задачи в списке');
  const ids = rows.map((r) => r.getAttribute('data-task')).sort();
  assert.deepEqual(ids, TASKS.filter((t) => t.stage !== 'archived').map((t) => t.id).sort());

  const link = rows.find((r) => r.getAttribute('data-task') === 't_0000a001').querySelector('a.tasks-row__title');
  assert.equal(link.getAttribute('href'), '#/room/cafe/request/t_0000a001');

  const groups = listGroups(TASKS);
  assert.equal(groups.reduce((sum, g) => sum + g.count, 0), 7);
  assert.deepEqual(groups.map((g) => g.id), ['new', 'working', 'needs_you', 'review', 'done_system', 'accepted']);
});

test('экран «Задачи»: «Принять» отложено на 10 с и отменяется общим undo.js', async () => {
  resetMiniDom();
  resetSharedUndo();
  const toasts = h('div', { class: 'toasts', id: 'toasts' });
  DOC.body.appendChild(toasts);

  const posts = [];
  const api = {
    get: async () => ({
      tasks: TASKS.filter((t) => t.stage !== 'archived'),
      columns: BOARD_COLUMNS.map((c) => c.id),
      counts: boardCounts(TASKS),
    }),
    post: async (path, body) => { posts.push([path, body]); return { ok: true }; },
  };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const inst = await mount(el, {
    route: { roomId: 'cafe' }, api, toast: () => {}, navigate: () => {},
  });
  await tick();

  const board = el.querySelector('.tasks-board');
  assert.ok(board, 'доска отрисована');

  // 1. Нажали «Принять» — запроса на сервер ещё нет.
  const accept = byClass(el, 'tasks-card__btn').find((b) => textOf(b) === 'Принять');
  assert.ok(accept, 'кнопка «Принять» есть');
  accept.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 0, 'до конца отсчёта на сервере ничего не менялось');
  assert.match(textOf(el), /принимается/, 'карточка показывает, что принятие ждёт подтверждения');

  const toast = DOC.getElementById('toasts').querySelector('[data-undo-id]');
  assert.ok(toast, 'тост отмены показан');
  assert.match(textOf(toast), /Задача принята/);
  const undoId = toast.getAttribute('data-undo-id');

  // 2. «Вернуть» — действие не выполняется.
  const back = byClass(toast, 'undo__btn')[0];
  back.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 0, 'отмена не отправляет запрос');
  assert.doesNotMatch(textOf(el), /принимается/, 'карточка вернулась в «Готово»');

  // 3. Повторили и дали сроку истечь (тот же компонент, но коммит вручную).
  const accept2 = byClass(el, 'tasks-card__btn').find((b) => textOf(b) === 'Принять');
  accept2.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 0);
  await sharedUndo().commit(DOC.getElementById('toasts').querySelector('[data-undo-id]').getAttribute('data-undo-id'));
  assert.equal(posts.length, 1, 'по истечении срока действие ушло на сервер ровно один раз');
  assert.equal(posts[0][0], '/api/requests/t_0000a006/actions');
  assert.equal(posts[0][1].action, 'accept');

  // 4. «Вернуть в работу» — обычное действие сервера, сразу. Колонка «Принята»
  //    по умолчанию свёрнута — сначала раскрываем её штатной кнопкой.
  const accCol = byData(el, 'data-column', 'accepted')[0];
  const toggle = accCol.querySelector('.tasks-bcol__toggle');
  assert.ok(toggle, 'у колонки «Принята» есть кнопка раскрытия');
  toggle.dispatch('click', {});
  await tick();
  const reopen = byClass(el, 'tasks-card__btn').find((b) => textOf(b) === 'Вернуть в работу');
  assert.ok(reopen, 'кнопка возврата есть у принятой задачи');
  reopen.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 2);
  assert.equal(posts[1][1].action, 'reopen_accepted');

  await inst.unmount();
  assert.equal(undoId.length > 0, true);
});

test('«+ Новая задача» ведёт в композер комнаты', async () => {
  resetMiniDom();
  const api = { get: async () => ({ tasks: [], columns: [], counts: {} }), post: async () => ({}) };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const seen = [];
  const inst = await mount(el, {
    route: { roomId: 'laundry' }, api, toast: () => {}, navigate: (hash) => seen.push(hash),
  });
  await tick();
  const btn = byData(el, 'data-act', 'task-new')[0];
  assert.ok(btn, 'кнопка новой задачи есть');
  btn.dispatch('click', {});
  assert.deepEqual(seen, ['#/room/laundry/composer']);
  await inst.unmount();
});

test('ошибка загрузки показывает причину и кнопку повтора, а не пустой экран', async () => {
  resetMiniDom();
  const api = {
    get: async () => { throw new Error('Нет связи с приложением'); },
    post: async () => ({}),
  };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const inst = await mount(el, { route: { roomId: 'cafe' }, api, toast: () => {}, navigate: () => {} });
  await tick();
  assert.match(textOf(el), /Нет связи/);
  await inst.unmount();
});

/* ─────────────────────────────────────────────────────────────── защита */

test('ни одного innerHTML: весь текст собирается через h()', () => {
  for (const file of ['web/js/views/tasks-model.js', 'web/js/views/tasks-board.js']) {
    const src = readFileSync(join(ROOT, file), 'utf8');
    assert.equal(/\.innerHTML\s*=/.test(src), false, file + ': innerHTML запрещён');
    assert.equal(/insertAdjacentHTML/.test(src), false, file + ': insertAdjacentHTML запрещён');
    assert.equal(/https?:\/\//.test(src.replace(/^\/\*\*[\s\S]*?\*\//, '')), false,
      file + ': внешние адреса запрещены (никаких CDN)');
  }
});

test('стили доски — только токены темы, обе темы без хардкода цветов', () => {
  const css = readFileSync(join(ROOT, 'web/css/view-tasks.css'), 'utf8');
  const body = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const literal = body.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g) || [];
  assert.deepEqual(literal, [], 'цвета — только переменными темы');
  assert.match(css, /@container tasks/);
});
