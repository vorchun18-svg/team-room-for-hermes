/**
 * delo-board.test.mjs — «Доска дел и таймлайн» (W29B-Q5).
 *
 * Проверяет без браузера:
 *   • колонки доски = настоящие статусы дела, счётчики и порядок внутри колонки;
 *   • правила переноса: какое действие сервера соответствует переносу, а какие
 *     колонки честно отказывают (и почему) — ни одного «мёртвого» переноса;
 *   • каждое действие доски есть в `server/delo.py:ACTIONS` (контракт клиент↔сервер);
 *   • клавиатуру: стрелки ведут только в колонку, которая примет дело;
 *   • таймлайн: недели/месяцы, просрочено, без срока, вне периода, «сегодня»;
 *   • вьюха `delo.js` действительно подключила оба режима.
 *
 * Данные — вымышленные (кофейня, химчистка, грузоперевозки), как требуют правила волны.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  BOARD_COLUMNS, boardColumns, boardSummary, columnOf, createStatusFor, dragOutcome, keyboardTarget,
} from '../web/js/views/delo-board.js';
import {
  buildTimeline, gridDays, gridWeeks, isoDate, periodTitle, shiftAnchor, startOfDay, weekStart,
} from '../web/js/views/delo-timeline.js';
import { STATUS_LABEL, sortDelos } from '../web/js/views/delo-model.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/** Фиксированное «сегодня» — суббота 21 ноября 2026, полдень. */
const NOW = new Date(2026, 10, 21, 12, 0, 0).getTime();

const delo = (over = {}) => ({
  id: 'dl_test0001', room: 'cafe', title: 'Дело', status: 'open', due: null,
  owner: 'manager', members: [], updated_at: 1763700000, request_id: null,
  stages: [], waiting_on: [], needs_you: [], result: { parts: [], files: [] },
  outbox: [], found: [], timeline: [], duties: [], ...over,
});

const ITEMS = [
  delo({ id: 'dl_aaaa1111', room: 'cafe', title: 'Меню на неделю', status: 'open', due: '2026-11-18' }),
  delo({ id: 'dl_bbbb2222', room: 'laundry', title: 'Ответ клиенту', status: 'waiting', due: '2026-11-21',
    needs_you: [{ id: 'ny_1', kind: 'approve', text: 'Утвердить ответ' }] }),
  delo({ id: 'dl_cccc3333', room: 'freight', title: 'Сверка накладных', status: 'done', due: '2026-11-30' }),
  delo({ id: 'dl_dddd4444', room: 'cafe', title: 'Ремонт витрины', status: 'paused', due: null }),
  delo({ id: 'dl_eeee5555', room: 'laundry', title: 'Старая заявка', status: 'cancelled', due: '2026-11-17' }),
];

/* ─────────────────────────────────────────────────────────────── колонки */

test('колонки доски — ровно статусы дела, в порядке работы', () => {
  assert.deepEqual(BOARD_COLUMNS.map((c) => c.id), ['open', 'waiting', 'paused', 'done', 'cancelled']);
  for (const col of BOARD_COLUMNS) {
    assert.ok(Object.prototype.hasOwnProperty.call(STATUS_LABEL, col.id),
      'у колонки ' + col.id + ' есть подпись статуса');
    assert.ok(col.title && col.hint, 'у колонки ' + col.id + ' есть название и пояснение');
  }
});

test('boardColumns: каждое дело в своей колонке, пустые колонки на месте', () => {
  const cols = boardColumns(ITEMS);
  assert.equal(cols.length, BOARD_COLUMNS.length, 'пустые колонки не выбрасываем');
  const byId = Object.fromEntries(cols.map((c) => [c.id, c]));
  assert.deepEqual(byId.open.items.map((d) => d.id), ['dl_aaaa1111']);
  assert.deepEqual(byId.waiting.items.map((d) => d.id), ['dl_bbbb2222']);
  assert.deepEqual(byId.paused.items.map((d) => d.id), ['dl_dddd4444']);
  assert.deepEqual(byId.done.items.map((d) => d.id), ['dl_cccc3333']);
  assert.deepEqual(byId.cancelled.items.map((d) => d.id), ['dl_eeee5555']);
  for (const col of cols) assert.equal(col.count, col.items.length, 'счётчик = число карточек');
  assert.equal(boardColumns([]).reduce((n, c) => n + c.count, 0), 0);
});

test('boardColumns: «ждёт вас» выше, затем ближний срок, затем свежее обновление', () => {
  const list = [
    delo({ id: 'dl_zzzz0001', status: 'open', due: '2026-11-25', updated_at: 100 }),
    delo({ id: 'dl_zzzz0002', status: 'open', due: '2026-11-23', updated_at: 100 }),
    delo({ id: 'dl_zzzz0003', status: 'open', due: '2027-01-10', updated_at: 999,
      needs_you: [{ id: 'ny_7', kind: 'question', text: 'Что выбрать?' }] }),
    delo({ id: 'dl_zzzz0004', status: 'open', due: null, updated_at: 500 }),
  ];
  const open = boardColumns(list).find((c) => c.id === 'open');
  assert.deepEqual(open.items.map((d) => d.id),
    ['dl_zzzz0003', 'dl_zzzz0002', 'dl_zzzz0001', 'dl_zzzz0004']);
  assert.deepEqual(open.items, sortDelos(list), 'порядок колонки = общий порядок списка');
});

test('boardSummary: всего, ждут решения, просрочено, закрыто', () => {
  const sum = boardSummary(ITEMS, NOW);
  assert.equal(sum.total, 5);
  assert.equal(sum.needs, 1);
  assert.equal(sum.overdue, 2, 'срок 18 и 17 ноября уже прошёл');
  assert.equal(sum.done, 1);
});

test('columnOf: неизвестный статус не прячет дело', () => {
  assert.equal(columnOf(delo({ status: 'waiting' })), 'waiting');
  assert.equal(columnOf(delo({ status: 'мусор' })), 'open');
  assert.equal(columnOf(null), 'open');
});

/* ─────────────────────────────────────────────────────────── перенос карточки */

test('dragOutcome: перенос = настоящее действие сервера', () => {
  const cases = [
    ['open', 'paused', 'pause'],
    ['waiting', 'paused', 'pause'],
    ['paused', 'open', 'resume'],
    ['open', 'done', 'done'],
    ['waiting', 'done', 'done'],
    ['paused', 'done', 'done'],
    ['done', 'open', 'reopen'],
    ['cancelled', 'open', 'reopen'],
    ['cancelled', 'done', 'done'],
  ];
  for (const [from, to, action] of cases) {
    const out = dragOutcome(from, to);
    assert.equal(out.ok, true, from + ' → ' + to + ' должен переноситься');
    assert.equal(out.action, action, from + ' → ' + to);
  }
});

test('dragOutcome: отказ называет причину и не молчит', () => {
  for (const to of ['waiting', 'cancelled']) {
    const out = dragOutcome('open', to);
    assert.equal(out.ok, false, 'в «' + to + '» перетаскиванием не попасть');
    assert.ok(out.reason && out.reason.length > 20, 'причина у отказа в «' + to + '»');
  }
  const back = dragOutcome('waiting', 'open');
  assert.equal(back.ok, false);
  assert.match(back.reason, /ожидан/i, 'объясняем, что мешает вернуть дело в работу');
  const noop = dragOutcome('open', 'open');
  assert.equal(noop.ok, false);
  assert.equal(noop.noop, true, 'перенос в свою же колонку — не ошибка, а «ничего не делаем»');
  assert.equal(dragOutcome('open', 'нет-такой').ok, false);
});

test('каждое действие доски объявлено сервером (server/delo.py:ACTIONS)', () => {
  const src = readFileSync(join(ROOT, 'server', 'delo.py'), 'utf8');
  const m = /ACTIONS\s*=\s*\(([^)]*)\)/.exec(src);
  assert.ok(m, 'в server/delo.py есть кортеж ACTIONS');
  const actions = new Set((m[1].match(/"[a-z_]+"/g) || []).map((s) => s.replace(/"/g, '')));
  for (const from of ['open', 'waiting', 'paused', 'done', 'cancelled']) {
    for (const to of ['open', 'waiting', 'paused', 'done', 'cancelled']) {
      const out = dragOutcome(from, to);
      if (out.ok) assert.ok(actions.has(out.action), 'сервер знает действие ' + out.action);
    }
  }
  assert.ok(actions.has('reopen'), 'возврат закрытого дела в работу — действие сервера');
});

test('«+ Дело» предлагается только там, где дело правда создаётся в этом статусе', () => {
  assert.equal(createStatusFor('open'), 'open');
  assert.equal(createStatusFor('paused'), 'paused');
  assert.equal(createStatusFor('done'), 'done');
  assert.equal(createStatusFor('waiting'), null, '«ждёт ответа» — это ожидание, а не статус создания');
  assert.equal(createStatusFor('cancelled'), null);
  assert.equal(createStatusFor('нет-такой'), null);
});

test('клавиатура: стрелка ведёт только в колонку, которая примет дело', () => {
  assert.equal(keyboardTarget('open', 1), 'paused', 'из «В работе» вправо — «На паузе», «Ждёт ответа» пропускаем');
  assert.equal(keyboardTarget('paused', 1), 'done');
  assert.equal(keyboardTarget('done', 1), null, 'правее только «Отменено», туда нельзя');
  assert.equal(keyboardTarget('done', -1), 'paused');
  assert.equal(keyboardTarget('cancelled', -1), 'done');
  assert.equal(keyboardTarget('open', -1), null);
  assert.equal(keyboardTarget('мусор', 1), null);
  // «в работе» достижимо из «Закрыто» — значит стрелка влево от «На паузе» ведёт в работу
  assert.equal(keyboardTarget('paused', -1), 'open');
});

/* ──────────────────────────────────────────────────────────────────── время */

test('даты считаются по локальному календарю, без сдвига дня', () => {
  assert.equal(isoDate(new Date(2026, 0, 5)), '2026-01-05');
  assert.equal(isoDate(startOfDay(new Date(2026, 11, 31, 23, 30))), '2026-12-31');
  const monday = weekStart(new Date(2026, 10, 21));       // суббота
  assert.equal(isoDate(monday), '2026-11-16');
  assert.equal(monday.getDay(), 1, 'неделя начинается с понедельника');
  assert.equal(isoDate(weekStart(new Date(2026, 10, 16))), '2026-11-16', 'понедельник остаётся собой');
  assert.equal(isoDate(weekStart(new Date(2026, 10, 22))), '2026-11-16', 'воскресенье — в той же неделе');
});

test('период: заголовок, шаг ‹ › и сетка недели/месяца', () => {
  assert.equal(periodTitle(new Date(2026, 10, 21), 'week'), '16–22 ноября 2026');
  assert.equal(periodTitle(new Date(2026, 10, 21), 'month'), 'ноябрь 2026');
  const week = gridDays(new Date(2026, 10, 21), 'week');
  assert.equal(week.length, 7);
  assert.equal(isoDate(week[0]), '2026-11-16');
  assert.equal(isoDate(week[6]), '2026-11-22');

  const month = gridDays(new Date(2026, 10, 10), 'month');
  assert.equal(month.length % 7, 0, 'сетка месяца — целые недели');
  assert.equal(isoDate(month[0]), '2026-10-26', '1 ноября 2026 — воскресенье, сетка идёт с понедельника');
  assert.ok(month.some((d) => isoDate(d) === '2026-11-30'));
  assert.equal(gridWeeks(month).length, month.length / 7);

  assert.equal(isoDate(shiftAnchor(new Date(2026, 10, 21), 'week', 1)), '2026-11-23');
  assert.equal(isoDate(shiftAnchor(new Date(2026, 10, 21), 'week', -2)), '2026-11-02');
  assert.equal(isoDate(shiftAnchor(new Date(2026, 10, 21), 'month', 1)), '2026-12-01');
  assert.equal(isoDate(shiftAnchor(new Date(2026, 10, 21), 'month', -1)), '2026-10-01');
});

test('buildTimeline (неделя): просрочено, сегодня, вне периода, без срока', () => {
  const data = buildTimeline(ITEMS, { scale: 'week', anchor: new Date(2026, 10, 21), nowMs: NOW });
  assert.equal(data.scale, 'week');
  assert.equal(data.days.length, 7);
  assert.equal(data.total, 5);
  assert.deepEqual(data.overdue.map((d) => d.id), ['dl_eeee5555', 'dl_aaaa1111'],
    'просрочено — по возрастанию срока');
  assert.deepEqual(data.noDue.map((d) => d.id), ['dl_dddd4444']);
  assert.equal(data.outside, 1, 'срок 30 ноября — вне недели 16–22');
  const today = data.cells.find((c) => c.iso === '2026-11-21');
  assert.equal(today.isToday, true);
  assert.deepEqual(today.items.map((d) => d.id), ['dl_bbbb2222']);
  const past = data.cells.find((c) => c.iso === '2026-11-16');
  assert.equal(past.isPast, true, 'прошедшие дни недели помечены');
  assert.equal(data.cells.find((c) => c.iso === '2026-11-22').isPast, false);
  assert.equal(data.weeks.length, 1);
  assert.equal(data.weeks[0].length, 7);
});

test('buildTimeline (месяц): весь месяц, добивка чужих дней помечена', () => {
  const data = buildTimeline(ITEMS, { scale: 'month', anchor: new Date(2026, 10, 10), nowMs: NOW });
  assert.equal(data.scale, 'month');
  assert.equal(data.title, 'ноябрь 2026');
  assert.equal(data.outside, 0, 'все сроки месяца попали в сетку');
  const cell = data.cells.find((c) => c.iso === '2026-11-30');
  assert.deepEqual(cell.items.map((d) => d.id), ['dl_cccc3333']);
  assert.equal(cell.inMonth, true);
  assert.equal(data.cells[0].inMonth, false, '26–31 октября — добивка сетки, показана бледно');
  assert.equal(data.cells[0].iso, '2026-10-26');
});

test('buildTimeline: в один день «ждёт вас» выше, затем по названию', () => {
  const list = [
    delo({ id: 'dl_kkkk0001', title: 'Ящики', due: '2026-11-21' }),
    delo({ id: 'dl_kkkk0002', title: 'Акт', due: '2026-11-21',
      needs_you: [{ id: 'ny_9', kind: 'approve', text: 'Утвердить акт' }] }),
    delo({ id: 'dl_kkkk0003', title: 'Витрина', due: '2026-11-21' }),
  ];
  const data = buildTimeline(list, { scale: 'week', anchor: new Date(2026, 10, 21), nowMs: NOW });
  const cell = data.cells.find((c) => c.iso === '2026-11-21');
  assert.deepEqual(cell.items.map((d) => d.id), ['dl_kkkk0002', 'dl_kkkk0003', 'dl_kkkk0001']);
});

test('buildTimeline: пусто — честно пусто, без выдуманных дел', () => {
  const data = buildTimeline([], { scale: 'week', anchor: new Date(2026, 10, 21), nowMs: NOW });
  assert.equal(data.total, 0);
  assert.equal(data.overdue.length, 0);
  assert.equal(data.noDue.length, 0);
  assert.ok(data.cells.every((c) => c.items.length === 0));
});

/* ──────────────────────────────────────────────────────────── подключение */

test('вьюха «Дела» подключила оба режима и помнит выбор человека', () => {
  const src = readFileSync(join(ROOT, 'web', 'js', 'views', 'delo.js'), 'utf8');
  assert.match(src, /from '\.\/delo-board\.js'/, 'доска подключена');
  assert.match(src, /from '\.\/delo-timeline\.js'/, 'таймлайн подключен');
  assert.match(src, /teamroom\.delos\.mode/, 'выбор режима запоминается');
  assert.match(src, /css\/view-delo-board\.css/, 'стили режимов подключены');
  assert.match(src, /mode === 'board'/, 'режим доски отрисовывается');
  assert.match(src, /mode === 'time'/, 'режим времени отрисовывается');
  const css = readFileSync(join(ROOT, 'web', 'css', 'view-delo-board.css'), 'utf8');
  assert.match(css, /\.delo-board\s*\{/, 'стили доски на месте');
  assert.match(css, /\.delo-tl__grid\s*\{/, 'стили таймлайна на месте');
  assert.ok(!/var\(--(?![\w-]+\s*[,)])/.test(css), 'в стилях только токены, без «голых» значений цвета');
});
