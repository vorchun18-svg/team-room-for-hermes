/**
 * tasks-board.test.mjs — доска «Задачи» комнаты (W31-B1: четыре колонки и переносы).
 *
 * Проверяет без браузера:
 *   • колонки — ровно контракт 31 (`new`, `working`, `review`, `done`) в порядке,
 *     с подписями «Новая / В работе / На проверке / Готово»;
 *   • старые стадии волны 30 читаются без потерь: `needs_you` → «В работе» с
 *     меткой, `done_system` → «На проверке», `accepted` → «Готово»;
 *   • метка «Ждёт вас · N вопросов» — только на карточке в работе;
 *   • переносы — только контракт 31 (`move`): проверка → готово (принять),
 *     проверка → работа (на доработку, с комментарием), готово → работа,
 *     работа → проверка; остальное — отказ с человеческой причиной;
 *   • `allowed_moves` карточки главнее таблицы;
 *   • клавиатура ведёт только туда, где перенос разрешён;
 *   • «Принять» на карточке в «На проверке» отложено на 10 с общим `undo.js`:
 *     до конца отсчёта запроса нет, «Вернуть» его не отправляет;
 *   • прошлые данные показываются мгновенно (снимок), обновление идёт фоном;
 *   • блок «Задача команде» стоит над доской, «+» ставит фокус в поле;
 *   • никакого `innerHTML`, внешних CDN и хардкода цветов.
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
  BOARD_COLUMNS, ageText, boardColumns, boardCounts, boardSummary, cardActionsOf, columnOfStage,
  keyboardTarget, listGroups, moveOutcome, moveWarning, normalizeTask, normalizeTasks, pluralRu, stageOf,
  stepsText, taskHref, waitingBadge,
} = await import('../web/js/views/tasks-model.js');
const {
  renderBoard, renderList, mount, cacheKey, readSnapshot, saveSnapshot,
} = await import('../web/js/views/tasks-board.js');
const { sharedUndo, resetSharedUndo } = await import('../web/js/undo.js');
const { h } = await import('../web/js/util.js');

/** Фиксированное «сейчас»: 25 ноября 2026, 23:00 (локально). */
const NOW = new Date(2026, 10, 25, 23, 0, 0).getTime();
const NOW_S = Math.floor(NOW / 1000);

const task = (over = {}) => normalizeTask(Object.assign({
  id: 't_aaaa1111',
  title: 'Задача',
  column: 'working',
  assignee: '',
  steps_done: 0,
  steps_total: 0,
  waiting_you: false,
  waiting_count: 0,
  children_count: 0,
  updated_at: NOW_S - 600,
  summary_short: '',
}, over));

const TASKS = [
  task({ id: 't_0000a001', title: 'Согласовать график поставок упаковки', column: 'new', updated_at: NOW_S - 300 }),
  task({ id: 't_0000a002', title: 'Себестоимость недельного меню', column: 'working', assignee: 'analyst', steps_done: 2, steps_total: 5 }),
  task({ id: 't_0000a003', title: 'Выбор поставщика зерна', column: 'working', assignee: 'analyst', waiting_you: true, waiting_count: 2, needs_you: { count: 2, blocking: true } }),
  task({ id: 't_0000a004', title: 'Договор аренды витрины', column: 'review', steps_done: 3, steps_total: 3 }),
  task({ id: 't_0000a005', title: 'Смета на химчистку формы', column: 'review', waiting_you: true, waiting_count: 1, needs_you: { count: 1, blocking: false } }),
  task({ id: 't_0000a006', title: 'Тарифы грузоперевозчика', column: 'done', updated_at: NOW_S - 86400 }),
  task({ id: 't_0000a007', title: 'Архивная задача', column: 'archived', archived: true }),
];

/* ─────────────────────────────────────────────────────────── модель доски */

test('W31: четыре колонки контракта, в порядке и с подписями', () => {
  assert.deepEqual(BOARD_COLUMNS.map((c) => c.id), ['new', 'working', 'review', 'done']);
  assert.deepEqual(BOARD_COLUMNS.map((c) => c.title), ['Новая', 'В работе', 'На проверке', 'Готово']);
  // «Ждёт вас» и «Принята» больше не колонки (дефект 23)
  assert.equal(BOARD_COLUMNS.some((c) => c.id === 'needs_you'), false);
  assert.equal(BOARD_COLUMNS.some((c) => c.id === 'accepted'), false);
});

test('W31: старые стадии волны 30 читаются как новые колонки', () => {
  assert.equal(columnOfStage('new'), 'new');
  assert.equal(columnOfStage('working'), 'working');
  assert.equal(columnOfStage('needs_you'), 'working');
  assert.equal(columnOfStage('review'), 'review');
  assert.equal(columnOfStage('done_system'), 'review');
  assert.equal(columnOfStage('accepted'), 'done');
  assert.equal(columnOfStage(''), 'new', 'неизвестная стадия не теряет карточку');

  const legacy = normalizeTask({ id: 't_1', title: 'Старая', stage: 'needs_you', needs_you: { count: 2, blocking: true } });
  assert.equal(legacy.column, 'working');
  assert.equal(legacy.waiting_count, 2);
  assert.equal(legacy.waiting_you, true);
  const accepted = normalizeTask({ id: 't_2', title: 'Принятая', stage: 'accepted', accepted: true });
  assert.equal(accepted.column, 'done');
  assert.equal(accepted.accepted, true);
});

test('счётчики колонок = числу карточек; архив вне доски', () => {
  assert.deepEqual(boardCounts(TASKS), { new: 1, working: 2, review: 2, done: 1 });
  const cols = boardColumns(TASKS);
  for (const col of cols) assert.equal(col.count, col.items.length);
  assert.equal(cols.reduce((sum, c) => sum + c.count, 0), 6);
});

test('порядок в колонке: блокирующее — выше, затем свежие', () => {
  const working = boardColumns(TASKS).find((c) => c.id === 'working');
  assert.deepEqual(working.items.map((t) => t.id), ['t_0000a003', 't_0000a002']);
});

test('W31: метка «Ждёт вас · N вопросов» — только у карточки в работе', () => {
  const w = waitingBadge(task({ column: 'working', waiting_you: true, waiting_count: 2, needs_you: { count: 2, blocking: true } }));
  assert.equal(w.text, 'Ждёт вас · 2 вопроса');
  assert.equal(w.tone, 'danger');
  assert.equal(waitingBadge(task({ column: 'working', waiting_you: true, waiting_count: 5, needs_you: { count: 5 } })).text,
    'Ждёт вас · 5 вопросов');
  assert.equal(waitingBadge(task({ column: 'working', waiting_you: true, waiting_count: 1, needs_you: { count: 1 } })).text,
    'Ждёт вас · 1 вопрос');
  // не колонка «В работе» — метки нет
  assert.equal(waitingBadge(task({ column: 'review', waiting_you: true, waiting_count: 1 })), null);
  assert.equal(waitingBadge(task({ column: 'done', waiting_you: true, waiting_count: 1 })), null);
  assert.equal(waitingBadge(task({ column: 'new', waiting_you: true, waiting_count: 1 })), null);
  assert.equal(waitingBadge(task({ column: 'working' })), null);
});

test('W31: переносы — только контракт `move`', () => {
  const accept = moveOutcome('review', 'done');
  assert.deepEqual([accept.ok, accept.action, accept.to, accept.accept], [true, 'move', 'done', true]);

  const back = moveOutcome('review', 'working');
  assert.deepEqual([back.ok, back.action, back.to, back.comment, back.accept], [true, 'move', 'working', true, false]);

  const reopen = moveOutcome('done', 'working');
  assert.deepEqual([reopen.ok, reopen.action, reopen.to, reopen.accept], [true, 'move', 'working', false]);

  const review = moveOutcome('working', 'review');
  assert.deepEqual([review.ok, review.action, review.to], [true, 'move', 'review']);

  // «из Новой» переносить нечего: задача сразу уходит в работу
  assert.equal(moveOutcome('new', 'working').ok, false);
  assert.match(moveOutcome('new', 'working').reason, /Новой/);
  // принять можно только с проверки
  assert.equal(moveOutcome('working', 'done').ok, false);
  assert.match(moveOutcome('working', 'done').reason, /только с проверки/);
  // принятую — только в работу
  assert.equal(moveOutcome('done', 'review').ok, false);
  // в «Новую» не вернуть
  assert.equal(moveOutcome('working', 'new').ok, false);
  // та же колонка — не действие, а не ошибка
  assert.equal(moveOutcome('review', 'review').noop, true);
});

test('W31: allowed_moves карточки главнее таблицы', () => {
  const denied = moveOutcome('review', 'done', ['working']);
  assert.equal(denied.ok, false);
  assert.match(denied.reason, /недоступно/);
  assert.equal(moveOutcome('working', 'review', ['review']).ok, true);
});

test('клавиатура ведёт только туда, где перенос разрешён', () => {
  assert.equal(keyboardTarget('review', 1), 'done');
  assert.equal(keyboardTarget('review', -1), 'working');
  assert.equal(keyboardTarget('working', 1), 'review');
  assert.equal(keyboardTarget('done', -1), 'working');
  assert.equal(keyboardTarget('done', 1), null);
  assert.equal(keyboardTarget('new', 1), null, 'из «Новой» переносить нечего');
  assert.equal(keyboardTarget('working', -1), null, 'слева — «Новая», туда нельзя');
  assert.equal(keyboardTarget('working', 1, ['done']), null, 'allowed_moves сужает шаг');
});

test('кнопки карточки: принять/вернуть на проверке, вернуть в работу в готовом', () => {
  assert.deepEqual(cardActionsOf(task({ column: 'review' })).map((a) => a.label),
    ['Принять', 'Вернуть на доработку']);
  assert.deepEqual(cardActionsOf(task({ column: 'done' })).map((a) => a.label), ['Вернуть в работу']);
  assert.deepEqual(cardActionsOf(task({ column: 'working' })), []);
  assert.deepEqual(cardActionsOf(task({ column: 'new' })), []);
});

test('сводка: ждут вас — карточки в работе с меткой; готово — принятые', () => {
  const s = boardSummary(TASKS);
  assert.equal(s.total, 6);
  assert.equal(s.waiting, 1);
  assert.equal(s.questions, 2);
  assert.equal(s.review, 2);
  assert.equal(s.done, 1);
  assert.equal(s.archived, 1);
});

test('шаги, возраст, ссылка на задачу', () => {
  assert.equal(stepsText({ steps_done: 2, steps_total: 5 }), 'шаги 2 из 5');
  assert.equal(stepsText({ steps_done: 0, steps_total: 0 }), '');
  assert.equal(ageText(NOW_S - 600, NOW), '10 мин');
  assert.equal(taskHref('cafe', 't_0000a006'), '#/room/cafe/request/t_0000a006');
  assert.equal(pluralRu(2, ['задача', 'задачи', 'задач']), 'задачи');
});

/* ─────────────────────────────────────────────────────────── отрисовка */

test('W31: доска — четыре колонки в одну линию, у каждой заголовок и «+»', () => {
  resetMiniDom();
  const board = renderBoard(TASKS, { onCreate: () => {}, nowMs: NOW });
  assert.equal(board.className, 'tasks-board');
  const cols = byClass(board, 'tasks-bcol');
  assert.deepEqual(cols.map((c) => c.getAttribute('data-column')), ['new', 'working', 'review', 'done']);
  for (const col of cols) {
    const head = col.querySelector('.tasks-bcol__title');
    assert.ok(head, 'у колонки есть свой заголовок');
    assert.ok(col.querySelector('.tasks-bcol__count'), 'в заголовке есть счётчик');
    assert.ok(col.querySelector('.tasks-bcol__add'), 'в заголовке есть «+»');
  }
  assert.equal(textOf(cols[1].querySelector('.tasks-bcol__title')), 'В работе2');
});

test('W31: метка «Ждёт вас» на карточке в работе; кнопки — на проверке', () => {
  resetMiniDom();
  const board = renderBoard(TASKS, { onCreate: () => {}, nowMs: NOW });
  const waitCard = byData(board, 'data-task', 't_0000a003')[0];
  assert.match(textOf(waitCard), /Ждёт вас · 2 вопроса/);
  const plainCard = byData(board, 'data-task', 't_0000a002')[0];
  assert.doesNotMatch(textOf(plainCard), /Ждёт вас/);

  const reviewCard = byData(board, 'data-task', 't_0000a004')[0];
  assert.match(textOf(reviewCard), /Принять/);
  assert.match(textOf(reviewCard), /Вернуть на доработку/);
  const doneCard = byData(board, 'data-task', 't_0000a006')[0];
  assert.match(textOf(doneCard), /Вернуть в работу/);
  assert.doesNotMatch(textOf(doneCard), /Принять/);
});

test('пустая колонка объясняет себя, а не молчит', () => {
  resetMiniDom();
  const board = renderBoard([task({ id: 't_1', column: 'review' })], { onCreate: () => {}, nowMs: NOW });
  const emptyWork = byData(board, 'data-column', 'working')[0].querySelector('.tasks-bcol__empty');
  assert.ok(emptyWork);
  assert.match(textOf(emptyWork), /Пусто/);
});

test('пустая доска: зачем экран и кнопка «Написать задачу»', () => {
  resetMiniDom();
  let clicked = 0;
  const empty = renderBoard([], { onCreate: () => { clicked += 1; }, nowMs: NOW });
  assert.match(textOf(empty), /Задач пока нет/);
  const btn = byClass(empty, 'btn')[0];
  btn.dispatch('click', {});
  assert.equal(clicked, 1);
});

test('список: каждая задача один раз, ссылки ведут на страницу задачи', () => {
  resetMiniDom();
  const list = renderList(TASKS, { roomId: 'cafe', onCreate: () => {}, nowMs: NOW });
  const rows = byData(list, 'data-task');
  assert.equal(rows.length, 6);
  const hrefs = byClass(list, 'tasks-row__title').map((a) => a.getAttribute('href'));
  assert.ok(hrefs.includes('#/room/cafe/request/t_0000a006'));
  const groups = listGroups(TASKS);
  assert.deepEqual(groups.map((g) => g.id), ['new', 'working', 'review', 'done']);
});

/* ─────────────────────────────────────────────────────────── экран */

/** Заглушка композера: поле + счётчик фокусов (без импорта настоящей вьюхи). */
function composerStub() {
  const calls = { focus: 0, mounted: 0, unmounted: 0 };
  const factory = async (target) => {
    calls.mounted += 1;
    const input = h('textarea', { class: 'textarea cmp-input', id: 'cmp-input' });
    target.appendChild(input);
    return {
      focus() { calls.focus += 1; input.focus(); },
      unmount() { calls.unmounted += 1; input.remove(); },
    };
  };
  return { calls, factory };
}

test('W31: прошлые данные видны сразу, обновление идёт фоном', async () => {
  resetMiniDom();
  try { localStorage.removeItem(cacheKey('cafe')); } catch { /* нет хранилища */ }
  saveSnapshot('cafe', [task({ id: 't_old', title: 'Прошлая задача', column: 'working' })]);
  assert.equal(readSnapshot('cafe').tasks.length, 1);

  let resolveGet = null;
  const api = {
    get: () => new Promise((resolve) => { resolveGet = resolve; }),
    post: async () => ({}),
  };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const stub = composerStub();
  const mounting = mount(el, {
    route: { roomId: 'cafe' }, api, toast: () => {}, navigate: () => {}, composerMount: stub.factory,
  });
  await tick();

  // До ответа сервера на экране уже прошлая доска — без серых заглушек.
  assert.match(textOf(el), /Прошлая задача/);
  assert.equal(byClass(el, 'skeleton').length, 0, 'на тёплом входе нет заглушек');
  assert.match(textOf(el), /обновляем/);

  resolveGet({ tasks: [{ id: 't_new', title: 'Свежая задача', column: 'review' }], columns: [], counts: {} });
  const inst = await mounting;
  await tick();
  assert.match(textOf(el), /Свежая задача/);
  assert.doesNotMatch(textOf(el), /Прошлая задача/);
  await inst.unmount();
});

test('W31: «Задача команде» стоит над доской, «+» ставит фокус в поле', async () => {
  resetMiniDom();
  try { localStorage.removeItem(cacheKey('laundry')); } catch { /* нет хранилища */ }
  const api = { get: async () => ({ tasks: [task({ id: 't_1', title: 'Задача' })] }), post: async () => ({}) };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const stub = composerStub();
  const inst = await mount(el, {
    route: { roomId: 'laundry' }, api, toast: () => {}, navigate: () => {}, composerMount: stub.factory,
  });
  await tick();

  const compose = el.querySelector('.tasks-compose');
  const host = el.querySelector('.tasks__host');
  assert.ok(compose && host, 'и блок задачи, и доска на экране');
  assert.equal(compose.parentNode.childNodes.indexOf(compose) < compose.parentNode.childNodes.indexOf(host), true,
    'композер выше доски');
  assert.equal(stub.calls.mounted, 1);

  const newBtn = byData(el, 'data-act', 'task-new')[0];
  newBtn.dispatch('click', {});
  await tick();
  assert.equal(stub.calls.focus, 1, 'кнопка «Новая задача» ставит фокус в поле');
  assert.equal(DOC.activeElement && DOC.activeElement.id, 'cmp-input');

  // «+» в заголовке колонки делает то же самое.
  const colAdd = byData(el, 'data-act', 'task-add-review')[0];
  assert.ok(colAdd, 'в заголовке колонки есть «+»');
  colAdd.dispatch('click', {});
  await tick();
  assert.equal(stub.calls.focus, 2);

  await inst.unmount();
  assert.equal(stub.calls.unmounted, 1, 'композер снимается вместе с экраном');
});

test('экран «Задачи»: «Принять» отложено на 10 с и отменяется общим undo.js', async () => {
  resetMiniDom();
  resetSharedUndo();
  try { localStorage.removeItem(cacheKey('cafe')); } catch { /* нет хранилища */ }
  const toasts = h('div', { class: 'toasts', id: 'toasts' });
  DOC.body.appendChild(toasts);

  const posts = [];
  const api = {
    get: async () => ({
      tasks: [{ id: 't_0000a004', title: 'Договор аренды витрины', column: 'review', waiting_count: 0 }],
      columns: ['new', 'working', 'review', 'done'],
      counts: { review: 1 },
    }),
    post: async (path, body) => { posts.push([path, body]); return { ok: true, task: {} }; },
  };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const stub = composerStub();
  const inst = await mount(el, {
    route: { roomId: 'cafe' }, api, toast: () => {}, navigate: () => {}, composerMount: stub.factory,
  });
  await tick();

  const accept = byClass(el, 'tasks-card__btn').find((b) => textOf(b) === 'Принять');
  assert.ok(accept, 'кнопка «Принять» есть у карточки на проверке');
  accept.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 0, 'до конца отсчёта на сервере ничего не менялось');
  assert.match(textOf(el), /принимается/, 'карточка показывает, что принятие ждёт');

  const toast = DOC.getElementById('toasts').querySelector('[data-undo-id]');
  assert.ok(toast, 'тост отмены показан');
  assert.match(textOf(toast), /Задача принята/);
  const undoId = toast.getAttribute('data-undo-id');

  const back = byClass(toast, 'undo__btn')[0];
  back.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 0, 'отмена не отправляет запрос');
  assert.doesNotMatch(textOf(el), /принимается/);

  // Повторили и дали сроку истечь (коммит вручную — тот же компонент).
  const accept2 = byClass(el, 'tasks-card__btn').find((b) => textOf(b) === 'Принять');
  accept2.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 0);
  await sharedUndo().commit(DOC.getElementById('toasts').querySelector('[data-undo-id]').getAttribute('data-undo-id'));
  assert.equal(posts.length, 1, 'по истечении срока действие ушло на сервер ровно один раз');
  assert.equal(posts[0][0], '/api/requests/t_0000a004/actions');
  assert.equal(posts[0][1].action, 'move');
  assert.equal(posts[0][1].to, 'done');
  assert.equal(posts[0][1].confirm, true);

  await inst.unmount();
  assert.equal(undoId.length > 0, true);
});

test('W31: «Вернуть в работу» — перенос `move` сразу, без отсчёта', async () => {
  resetMiniDom();
  try { localStorage.removeItem(cacheKey('cafe')); } catch { /* нет хранилища */ }
  const posts = [];
  const api = {
    get: async () => ({ tasks: [{ id: 't_0000a006', title: 'Тарифы', column: 'done' }] }),
    post: async (path, body) => { posts.push([path, body]); return { ok: true }; },
  };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const stub = composerStub();
  const inst = await mount(el, {
    route: { roomId: 'cafe' }, api, toast: () => {}, navigate: () => {}, composerMount: stub.factory,
  });
  await tick();
  const reopen = byClass(el, 'tasks-card__btn').find((b) => textOf(b) === 'Вернуть в работу');
  assert.ok(reopen);
  reopen.dispatch('click', {});
  await tick();
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0][1], { action: 'move', to: 'working' });
  await inst.unmount();
});

test('ошибка загрузки показывает причину и кнопку повтора, а не пустой экран', async () => {
  resetMiniDom();
  try { localStorage.removeItem(cacheKey('cafe')); } catch { /* нет хранилища */ }
  const api = {
    get: async () => { throw new Error('Нет связи с приложением'); },
    post: async () => ({}),
  };
  const el = h('div', { class: 'view' });
  DOC.body.appendChild(el);
  const stub = composerStub();
  const inst = await mount(el, {
    route: { roomId: 'cafe' }, api, toast: () => {}, navigate: () => {}, composerMount: stub.factory,
  });
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
  // Четыре колонки в одну линию: ровно четыре трека, без «стопки» на узком окне.
  assert.match(css, /\.tasks-board\s*\{[^}]*grid-template-columns:\s*repeat\(4/);
  assert.match(css, /\.tasks-board\s*\{[^}]*overflow-x:\s*auto/);
});

test('снимок доски: маленький, без лишнего, читается обратно', () => {
  resetMiniDom();
  saveSnapshot('fn', TASKS);
  const back = readSnapshot('fn');
  assert.equal(back.tasks.length, TASKS.length);
  assert.equal(back.tasks[0].title, TASKS[0].title);
  assert.equal(back.tasks[0].summary_short, '', 'в снимок не пишем длинные поля');
  assert.equal(typeof back.at, 'number');
});

test('перенос записан, а карточка на доске не переставилась — честное предупреждение', () => {
  // Сервер A1 отвечает 200 и `warnings[].code = board_not_moved`, когда движок доски
  // отказал. Клиент не должен говорить «отправлена на проверку» — иначе человек видит
  // успех, а карточка осталась на месте (живая приёмка W31-QA1, порт 8883).
  assert.equal(moveWarning({ ok: true }), '');
  assert.equal(moveWarning(null), '');
  assert.equal(moveWarning({ warnings: [{ code: 'other', message: 'x' }] }), '');
  const text = moveWarning({ warnings: [{ code: 'board_not_moved', message: 'не вышло' }] });
  assert.match(text, /записано/);
  assert.match(text, /доске/);
  assert.doesNotMatch(text, /запрос/i);
});
