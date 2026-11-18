/**
 * duties-view.test.mjs — раздел «Дежурства» (W26-D8), клиент без браузера.
 *
 * Проверяем то, что человек видит и нажимает:
 *  1) недельная сетка: семь дней, «+N ещё», итог дня, план в будущем;
 *  2) пометки: ошибка/просрочка — красные, выключено — жёлтое, «ни разу» — замечание;
 *  3) у каждого дежурства есть действия: запуск, пауза/возобновление, расписание,
 *     привязка/отвязка — и ни одного тупика;
 *  4) подтверждение объясняет последствия, а правка расписания честно называется
 *     предложением («расписание в Hermes не меняется»);
 *  5) строки времени: «последний запуск: 2 часа назад · выполнено».
 *
 * DOM — tests/fake-dom.mjs.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom } from './fake-dom.mjs';

installFakeDom();

const {
  weekModel, weekGrid, daySummary, dayTitle, dutyState, actionList, confirmText,
  lastRunLine, nextRunLine, relText, dutiesSummary, weekTotalsText, verdictText,
  renderDuty, ACTIONS, PROBLEM_LABELS, WEEK_LABELS,
} = await import('../web/js/views/duties.js');

const NOW = 1_764_000_000;   // «сейчас» для расчётов: 2025-11-24 00:00 UTC

function textOf(node) {
  if (!node) return '';
  const own = node.nodeType === 3 ? node.data : '';
  const kids = Array.isArray(node.childNodes) ? node.childNodes.map(textOf).join(' ') : '';
  return (own + ' ' + kids).replace(/\s+/g, ' ').trim();
}

function walk(node, out = []) {
  if (!node) return out;
  out.push(node);
  for (const child of node.childNodes || []) walk(child, out);
  return out;
}

function findByAttr(root, attr, value) {
  return walk(root).filter((n) => n.attributes && n.getAttribute(attr) === value);
}

const WEEK = {
  start: '2026-11-16', end: '2026-11-22', runs: 5, failed: 1, missed: 1,
  days: [
    { date: '2026-11-16', weekday: 0, label: 'пн', day: 16, month: 11, today: false, future: false,
      runs: 2, failed: 0, missed: 0, planned: 49, more: 0,
      jobs: [{ job_id: 'j_daily', name: 'Утренняя сводка', runs: 2, state: 'ok' }] },
    { date: '2026-11-17', weekday: 1, label: 'вт', day: 17, month: 11, today: true, future: false,
      runs: 3, failed: 1, missed: 1, planned: 49, more: 2,
      jobs: [{ job_id: 'j_watch', name: 'Проверка сроков', runs: 2, state: 'failed' },
        { job_id: 'j_old', name: 'Резервная копия', runs: 1, state: 'ok' }] },
    { date: '2026-11-18', weekday: 2, label: 'ср', day: 18, month: 11, today: false, future: true,
      runs: 0, failed: 0, missed: 0, planned: 48, more: 0, jobs: [] },
    { date: '2026-11-19', weekday: 3, label: 'чт', day: 19, month: 11, today: false, future: true,
      runs: 0, failed: 0, missed: 0, planned: 48, more: 0, jobs: [] },
    { date: '2026-11-20', weekday: 4, label: 'пт', day: 20, month: 11, today: false, future: true,
      runs: 0, failed: 0, missed: 0, planned: 48, more: 0, jobs: [] },
    { date: '2026-11-21', weekday: 5, label: 'сб', day: 21, month: 11, today: false, future: true,
      runs: 0, failed: 0, missed: 0, planned: 48, more: 0, jobs: [] },
    { date: '2026-11-22', weekday: 6, label: 'вс', day: 22, month: 11, today: false, future: true,
      runs: 0, failed: 0, missed: 0, planned: 48, more: 0, jobs: [] },
  ],
};

const DUTY = {
  job_id: 'j_watch', name: 'Проверка сроков', enabled: true,
  schedule: '*/30 * * * *', schedule_human: 'каждые 30 минут',
  bound_rooms: ['studio'],
  last_run: { status: 'failed', started_at: NOW - 7200, finished_at: NOW - 7100,
    scheduled_for: NOW - 7300, late_s: 12, delivered: false, error: 'файл занят' },
  next_run_at: NOW + 900, missed_recent: 3,
  recent: [{ status: 'completed', started_at: NOW - 7200 }, { status: 'failed', started_at: NOW - 10800 }],
  excerpt: 'Проверено 12 сроков, 1 просрочен.',
  problem: { kind: 'failed', text: 'последний запуск завершился ошибкой — файл занят' },
};

/* ──────────────────────────────────────────────────────────── неделя */

test('недельная сетка: семь дней с подписями и итогом', () => {
  const week = weekModel(WEEK);
  assert.equal(week.days.length, 7);
  assert.deepEqual(week.days.map((d) => d.label), WEEK_LABELS);
  assert.equal(dayTitle(week.days[0]), 'пн, 16 ноября');
  assert.equal(daySummary(week.days[0]), '2 запуска');
  assert.match(daySummary(week.days[1]), /3 запуска · 1 с ошибкой · 1 пропущено/);
});

test('будущий день честно показывает план, а не «0 запусков»', () => {
  const week = weekModel(WEEK);
  const future = week.days[2];
  assert.equal(daySummary(future), 'план: 48');
  assert.match(textOf(weekGrid(WEEK, () => {})), /план: 48/);
});

test('сетка: «+N ещё» и число дней-кнопок', () => {
  const grid = weekGrid(WEEK, () => {});
  const days = walk(grid).filter((n) => String(n.tagName).toLowerCase() === 'button'
    && n.getAttribute('data-date'));
  assert.equal(days.length, 7);
  assert.match(textOf(grid), /\+2 ещё/);
  assert.match(textOf(grid), /2 запуска/);
});

test('пустая неделя не рисует пустоту без объяснения', () => {
  const grid = weekGrid({ days: [] }, () => {});
  assert.match(textOf(grid), /Недельная сетка пуста/);
});

test('итог недели одной строкой', () => {
  assert.match(weekTotalsText(WEEK), /5 запусков за неделю/);
  assert.match(weekTotalsText(WEEK), /1 с ошибкой/);
  assert.equal(weekTotalsText({ days: [] }), 'неделя не посчитана');
});

/* ──────────────────────────────────────────────────────── пометки и состояния */

test('пометки: ошибка — красная, выключено — жёлтое, норма — зелёное', () => {
  assert.equal(dutyState(DUTY).kind, 'fail');
  assert.equal(dutyState({ ...DUTY, problem: null }).kind, 'ok');
  assert.equal(dutyState({ ...DUTY, problem: { kind: 'disabled_but_due', text: 'x' } }).kind, 'off');
  assert.equal(dutyState({ ...DUTY, problem: null, enabled: false }).kind, 'off');
  assert.equal(PROBLEM_LABELS.never_ran, 'ни разу не запускалось');
});

test('сводка списка считает замечания, выключенные и идущие', () => {
  const summary = dutiesSummary([DUTY, { ...DUTY, job_id: 'b', problem: null, enabled: false },
    { ...DUTY, job_id: 'c', problem: null, last_run: { status: 'running' } }]);
  assert.equal(summary.total, 3);
  assert.equal(summary.problems, 1);
  assert.equal(summary.off, 1);
  assert.equal(summary.running, 1);
});

test('вердикт говорит, что делать', () => {
  assert.match(verdictText({ total: 2, problems: 1, off: 0 }), /есть действие/);
  assert.match(verdictText({ total: 0, problems: 0, off: 0 }), /Дежурств нет/);
});

/* ───────────────────────────────────────────────────────────── действия */

test('у дежурства есть действия: запуск, пауза, расписание, привязка', () => {
  const ids = actionList(DUTY, 'studio').map((a) => a.id);
  assert.deepEqual(ids, ['run', 'pause', 'schedule', 'unbind']);
  assert.equal(actionList({ ...DUTY, enabled: false }, '').map((a) => a.id).join(','),
    'run,resume,schedule');
  assert.equal(actionList({ ...DUTY, bound_rooms: [] }, 'studio').map((a) => a.id).pop(), 'bind');
  assert.equal(actionList(DUTY, '').map((a) => a.id).includes('bind'), false);
});

test('подтверждение объясняет последствия', () => {
  assert.match(confirmText('run', DUTY), /вне расписания/);
  assert.match(confirmText('pause', DUTY), /пока вы не нажмёте/);
  assert.match(confirmText('bind', DUTY), /Hermes не меняется/);
  assert.match(confirmText('schedule', DUTY, 'every 2h'), /every 2h/);
  assert.match(confirmText('schedule', DUTY, 'every 2h'), /не изменится/);
});

test('карточка дежурства: кнопки с действиями и подтверждением', () => {
  const card = renderDuty(DUTY, { roomId: 'studio', now: NOW, onAction: () => {} });
  const buttons = walk(card).filter((n) => String(n.tagName).toLowerCase() === 'button');
  assert.deepEqual(buttons.map((b) => b.getAttribute('data-action')),
    ['run', 'pause', 'schedule', 'unbind']);
  for (const btn of buttons) {
    assert.ok(btn.getAttribute('title') && btn.getAttribute('title').length > 5);
    assert.equal(btn.getAttribute('data-job-id'), 'j_watch');
  }
  const text = textOf(card);
  assert.match(text, /каждые 30 минут/);
  assert.match(text, /пропущено за неделю: 3/);
  assert.match(text, /Краткий итог последнего запуска/);
  assert.match(text, /файл занят/);
  assert.equal(ACTIONS.run.label, 'Запустить сейчас');
});

test('выключенное дежурство не повторяет «выключено» дважды', () => {
  const card = renderDuty({
    ...DUTY, enabled: false, problem: { kind: 'disabled_but_due', text: 'дежурство выключено — по расписанию не запускается' },
  }, { roomId: 'studio', now: NOW });
  const chips = walk(card).filter((n) => String(n.className).includes('duty-chip--off'));
  assert.equal(chips.length, 1);
  const problem = walk(card).filter((n) => String(n.className).includes('duty__problem--off'));
  assert.equal(problem.length, 1, 'замечание выключенного — жёлтым, а не красным');
});

test('карточка без итога не показывает пустой блок', () => {
  const card = renderDuty({ ...DUTY, excerpt: '' }, { roomId: 'studio', now: NOW });
  assert.equal(findByAttr(card, 'class', 'duty__excerpt').length, 0);
  assert.match(textOf(card), /Расписание…/);
});

/* ───────────────────────────────────────────────────────────── время */

test('относительное время по-русски', () => {
  assert.equal(relText(-30), 'меньше минуты назад');
  assert.equal(relText(-120), '2 минуты назад');
  assert.equal(relText(3600), 'через 1 час');
  assert.equal(relText(-2 * 86400), '2 дня назад');
  assert.equal(relText('x'), '');
});

test('строки последнего и следующего запуска', () => {
  assert.match(lastRunLine(DUTY, NOW), /последний запуск: 2 часа назад · ошибка/);
  assert.match(nextRunLine(DUTY, NOW), /через 15 минут/);
  assert.equal(lastRunLine({ last_run: null }, NOW), 'запусков ещё не было');
  assert.equal(nextRunLine({ next_run_at: null }, NOW), 'следующий запуск не назначен');
});
