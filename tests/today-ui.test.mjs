/**
 * today-ui.test.mjs — экран «Сегодня» (W24-N3), целевые проверки.
 *
 * Что покрыто:
 *   • тексты и формы (склонения, «как давно», «просрочено на N дней»);
 *   • порядок карточек (срочные первыми), лимит 7 и «показать все»;
 *   • плашка здоровья: видна только при warn/fail, худший тон — «fail»;
 *   • «что изменилось»: число в заголовке = сумма групп;
 *   • дежурства: тон значка по проблеме/статусу запуска, текст проблемы;
 *   • пустые состояния (не «null», а человеческий текст);
 *   • фикстуры N3 совпадают с контрактом §3 (поля, типы, сортировка, связи с
 *     комнатами) — от них зависят пакеты N2 и N4;
 *   • роутер знает адрес `#/today` (пункт меню и прямой адрес).
 *
 * Настоящий DOM в проверках не нужен: весь разбор — чистые функции
 * `today-model.js`, а живая проверка — `tools/probe_today_ui.py` в Chromium.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  EMPTY_TEXT, NEEDS_YOU_LIMIT, agoText, changedSummary, counterChips, dueRow, dueSoonText,
  dutyProblemText, dutyRunText, dutyTone, dutiesProblemCount, healthProblems, isEmptyDay,
  kindLabel, needsYouCard, needsYouVisible, overdueText, runningRow, shortDate, sortNeedsYou,
} from '../web/js/views/today-model.js';
import { parseHash } from '../web/js/router.js';
import { parseRouteHash } from '../web/js/route-memory.js';
import { dayTitle, updatedText } from '../web/js/views/today.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const fx = (name) => JSON.parse(readFileSync(join(ROOT, 'fixtures', name), 'utf8'));

const NOW = 1794977000;

/* ─────────────────────────────────────────────────────────── время и формы */

test('«как давно»: минуты, часы, дни, «вчера»', () => {
  assert.equal(agoText(NOW - 20, NOW), 'только что');
  assert.equal(agoText(NOW - 60 * 5, NOW), '5 минут назад');
  assert.equal(agoText(NOW - 60 * 60 * 2, NOW), '2 часа назад');
  assert.equal(agoText(NOW - 86400, NOW), 'вчера');
  assert.equal(agoText(NOW - 86400 * 3, NOW), '3 дня назад');
  assert.equal(agoText(null, NOW), '');
});

test('склонения в подписях сроков', () => {
  assert.equal(overdueText({ days_overdue: 1 }), 'просрочено на 1 день');
  assert.equal(overdueText({ days_overdue: 3 }), 'просрочено на 3 дня');
  assert.equal(overdueText({ days_overdue: 7 }), 'просрочено на 7 дней');
  assert.equal(dueSoonText({ days_left: 1 }), 'через 1 день');
  assert.equal(dueSoonText({ days_left: 3 }), 'через 3 дня');
  assert.equal(dueSoonText({ days_left: 0 }), 'сегодня последний день');
});

test('дата без года и подписи видов дел', () => {
  assert.equal(shortDate('2026-11-11'), '11.11');
  assert.equal(shortDate(''), '');
  assert.equal(kindLabel('question'), 'вопрос');
  assert.equal(kindLabel('failure'), 'сбой');
  assert.equal(kindLabel('unknown-kind'), '');
});

test('заголовок дня и «обновлено» — по-русски и без секунд', () => {
  const wed = new Date(2026, 10, 18, 9, 5).getTime();
  assert.equal(dayTitle(wed), 'среда, 18 ноября');
  assert.equal(updatedText(Math.floor(new Date(2026, 10, 18, 1, 45).getTime() / 1000)), 'обновлено 01:45');
  assert.equal(updatedText(0), '');
});

/* ─────────────────────────────────────────────────────── порядок и лимит */

test('срочные карточки первыми, затем самые давние', () => {
  const items = [
    { text: 'a', since: 100, urgent: false },
    { text: 'b', since: 300, urgent: true },
    { text: 'c', since: 200, urgent: true },
    { text: 'd', since: 50, urgent: false },
  ];
  assert.deepEqual(sortNeedsYou(items).map((x) => x.text), ['c', 'b', 'd', 'a']);
});

test('видимых карточек не больше семи, остальные — под кнопкой', () => {
  const items = Array.from({ length: 9 }, (_, i) => ({ text: 't' + i, since: i, urgent: false }));
  const view = needsYouVisible(items);
  assert.equal(view.visible.length, NEEDS_YOU_LIMIT);
  assert.equal(view.rest.length, 2);
  assert.equal(view.total, 9);
  assert.equal(needsYouVisible(items, 5).visible.length, 5);
});

test('карточка «ждёт вас»: подписи и рекомендация', () => {
  const card = needsYouCard({
    room: 'cafe', room_title: 'Студия', request_id: 't_1', request_title: 'Сайт студии',
    kind: 'decision', text: 'Выберите язык', since: NOW - 600, urgent: true, recommended: 'Только русский',
  }, NOW);
  assert.equal(card.kindLabel, 'решение');
  assert.equal(card.ago, '10 минут назад');
  assert.equal(card.urgent, true);
  assert.equal(card.recommended, 'Только русский');
  assert.equal(card.roomTitle, 'Студия');
});

/* ─────────────────────────────────────────────────────────────── здоровье */

test('плашка здоровья показывается только при warn/fail', () => {
  assert.equal(healthProblems({ checks: [{ id: 'a', status: 'ok' }] }).shown, false);
  assert.equal(healthProblems({ checks: [] }).shown, false);

  const warn = healthProblems({ checks: [{ id: 'a', title: 'A', status: 'warn', detail: 'старо' }] });
  assert.equal(warn.shown, true);
  assert.equal(warn.tone, 'warn');
  assert.equal(warn.title, 'Одно замечание');

  const both = healthProblems({
    checks: [
      { id: 'w', status: 'warn', title: 'W' },
      { id: 'f', status: 'fail', title: 'F' },
      { id: 'ok', status: 'ok', title: 'O' },
    ],
  });
  assert.equal(both.tone, 'fail');
  assert.equal(both.items.length, 2);
  assert.equal(both.items[0].id, 'f', 'сбой должен идти первым');
});

test('«неизвестно» — не проблема, но и не «всё хорошо»', () => {
  const only = healthProblems({ checks: [{ id: 'p', status: 'unknown' }] });
  assert.equal(only.shown, false);
  assert.equal(only.tone, '');
});

/* ──────────────────────────────────────────────────────── «изменилось» */

test('«что изменилось»: число в заголовке = сумма групп', () => {
  const data = {
    changed: {
      finished: [{ room: 'cafe', request_id: 't_1', request_title: 'A', at: 1 }],
      new_requests: 2,
      new_answers: 3,
      failures: [{ room: 'fn', request_id: 't_2', request_title: 'B', at: 2, reason: 'не открылся файл' }],
    },
  };
  const sum = changedSummary(data);
  assert.equal(sum.total, 7);
  assert.equal(sum.finished.length, 1);
  assert.equal(sum.failures[0].reason, 'не открылся файл');
  assert.equal(sum.finished[0].room, 'cafe');
  assert.equal(changedSummary({}).total, 0);
});

test('«идёт сейчас»: число участников по-русски', () => {
  assert.equal(runningRow({ members_working: 1 }).membersText, '1 участник работает');
  assert.equal(runningRow({ members_working: 3 }).membersText, '3 участника работает');
  assert.equal(runningRow({ members_working: 5 }).membersText, '5 участников работает');
});

/* ───────────────────────────────────────────────────────────── дежурства */

test('значок дежурства: проблема важнее статуса запуска', () => {
  assert.equal(dutyTone({ problem: { kind: 'failed' }, last_run: { status: 'completed' } }), 'fail');
  assert.equal(dutyTone({ problem: { kind: 'stale' }, last_run: { status: 'completed' } }), 'warn');
  assert.equal(dutyTone({ last_run: { status: 'completed' } }), 'ok');
  assert.equal(dutyTone({ last_run: { status: 'failed' } }), 'fail');
  assert.equal(dutyTone({ last_run: { status: 'missed' } }), 'warn');
  assert.equal(dutyTone({ last_run: null }), 'muted');
  assert.equal(dutyTone({ enabled: false, last_run: { status: 'completed' } }), 'muted');
});

test('подпись последнего запуска и текста проблемы', () => {
  assert.equal(dutyRunText({ last_run: null }), 'не запускалось');
  assert.equal(dutyRunText({ last_run: { status: 'running' } }), 'выполняется сейчас');
  assert.equal(dutyRunText({ last_run: { status: 'completed', finished_at: NOW - 300 } }, NOW), 'запуск 5 минут назад');
  assert.equal(dutyRunText({ last_run: { status: 'failed', finished_at: NOW - 300 } }, NOW), 'сбой 5 минут назад');

  const text = dutyProblemText({ problem: { kind: 'failed', text: 'файл занят' } });
  assert.match(text, /^сбой запуска: файл занят$/);
  assert.equal(dutyProblemText({ problem: null }), '');
  assert.equal(dutiesProblemCount([{ problem: { kind: 'failed' } }, { problem: null }, {}]), 1);
});

/* ─────────────────────────────────────────────────── пустые состояния */

test('пустые состояния — человеческие тексты, не пустота', () => {
  for (const key of ['needs_you', 'overdue', 'due_soon', 'changed', 'running', 'duties']) {
    assert.ok(EMPTY_TEXT[key] && EMPTY_TEXT[key].length > 5, 'нет текста для ' + key);
  }
  assert.match(EMPTY_TEXT.needs_you, /Всё спокойно/);
  assert.equal(isEmptyDay({}), true);
  assert.equal(isEmptyDay({ overdue: [{ text: 'x' }] }), false);
});

test('счётчики не показывают нули', () => {
  assert.deepEqual(counterChips({ counters: { needs_you: 0, overdue: 0 } }), []);
  const chips = counterChips({ counters: { needs_you: 5, overdue: 3, due_soon: 2, running: 2 } });
  assert.equal(chips.length, 4);
  assert.equal(chips[0].text, 'ждёт вас: 5');
  assert.equal(chips[1].tone, 'danger');
});

test('строка срока: тон и подписи', () => {
  const over = dueRow({ text: 'Прислать логотип', owner: 'Вы', due: '2026-11-11', days_overdue: 7, room_title: 'Студия' }, 'overdue');
  assert.equal(over.tone, 'danger');
  assert.equal(over.when, 'просрочено на 7 дней');
  assert.equal(over.due, '11.11');
  const soon = dueRow({ text: 'Согласовать', owner: 'Вы', due: '2026-11-19', days_left: 1 }, 'soon');
  assert.equal(soon.tone, 'warn');
  assert.equal(soon.when, 'через 1 день');
});

/* ─────────────────────────────────────────────── фикстуры (для N2 и N4) */

test('fixtures/today.json — по контракту §3.1', () => {
  const data = fx('today.json');
  for (const key of ['generated_at', 'last_visit_at', 'counters', 'needs_you', 'overdue', 'due_soon',
    'running', 'changed', 'rooms']) {
    assert.ok(key in data, 'нет поля ' + key);
  }
  assert.equal(typeof data.generated_at, 'number');
  assert.ok(data.last_visit_at === null || typeof data.last_visit_at === 'number');

  for (const key of ['needs_you', 'overdue', 'due_soon', 'running', 'changed']) {
    assert.ok(key in data.counters, 'нет счётчика ' + key);
  }

  // все массивы — массивы, пустые разделы не null
  for (const key of ['needs_you', 'overdue', 'due_soon', 'running', 'rooms']) {
    assert.ok(Array.isArray(data[key]), key + ' должен быть массивом');
  }
  assert.ok(Array.isArray(data.changed.finished) && Array.isArray(data.changed.failures));

  // «ждёт вас»: обязательные поля и виды дел из контракта
  const kinds = new Set(['question', 'decision', 'approval', 'step', 'draft', 'failure']);
  for (const item of data.needs_you) {
    for (const key of ['room', 'room_title', 'request_id', 'request_title', 'kind', 'text', 'since', 'urgent']) {
      assert.ok(key in item, 'needs_you: нет поля ' + key);
    }
    assert.ok(kinds.has(item.kind), 'неизвестный вид: ' + item.kind);
    assert.ok(typeof item.text === 'string' && item.text.length <= 200, 'text ≤ 200 знаков');
    assert.equal(typeof item.urgent, 'boolean');
    assert.ok('recommended' in item);
  }
  // порядок: срочные сначала, затем по since ↑
  const sorted = sortNeedsYou(data.needs_you);
  assert.deepEqual(data.needs_you.map((x) => x.text), sorted.map((x) => x.text), 'порядок needs_you');

  // просрочка: days_overdue ↓ и связь с датой
  const days = data.overdue.map((x) => x.days_overdue);
  assert.deepEqual(days, [...days].sort((a, b) => b - a), 'просрочка по убыванию');
  for (const item of data.overdue) {
    for (const key of ['room', 'room_title', 'item_id', 'text', 'owner', 'due', 'days_overdue']) {
      assert.ok(key in item, 'overdue: нет поля ' + key);
    }
    assert.match(item.due, /^\d{4}-\d{2}-\d{2}$/);
  }
  const left = data.due_soon.map((x) => x.days_left);
  assert.deepEqual(left, [...left].sort((a, b) => a - b), 'ближайшие по возрастанию');

  // счётчики совпадают с массивами (иначе вид и данные разойдутся)
  assert.equal(data.counters.needs_you, data.needs_you.length);
  assert.equal(data.counters.overdue, data.overdue.length);
  assert.equal(data.counters.due_soon, data.due_soon.length);
  assert.equal(data.counters.running, data.running.length);
  // «изменилось» — сумма групп (finished + failures + новые запросы + ответы)
  assert.equal(data.counters.changed,
    data.changed.finished.length + data.changed.failures.length
    + data.changed.new_requests + data.changed.new_answers);

  // комнаты в сводке — без повторов
  const ids = data.rooms.map((r) => r.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const room of data.rooms) {
    for (const key of ['id', 'title', 'needs_you', 'overdue', 'running']) {
      assert.ok(key in room, 'rooms: нет поля ' + key);
    }
  }
});

test('fixtures/duties.json — по контракту §3.2', () => {
  const data = fx('duties.json');
  assert.ok(Array.isArray(data.items));
  assert.ok(data.items.length >= 3, 'нужны разные состояния дежурств');
  const statuses = new Set(['completed', 'failed', 'running', 'missed', 'unknown']);
  const problems = new Set(['failed', 'stale', 'never_ran', 'disabled_but_due']);
  const seenProblem = new Set();
  for (const item of data.items) {
    for (const key of ['job_id', 'name', 'enabled', 'schedule', 'schedule_human', 'bound_rooms',
      'last_run', 'next_run_at', 'missed_recent', 'recent', 'excerpt', 'problem']) {
      assert.ok(key in item, 'duties: нет поля ' + key);
    }
    assert.equal(typeof item.enabled, 'boolean');
    assert.ok(Array.isArray(item.bound_rooms));
    assert.ok(item.excerpt.length <= 600, 'excerpt ≤ 600 знаков');
    assert.ok(item.recent.length <= 5, 'recent до 5 записей');
    if (item.last_run) assert.ok(statuses.has(item.last_run.status), 'статус запуска: ' + item.last_run.status);
    for (const run of item.recent) assert.ok(statuses.has(run.status));
    if (item.problem) {
      assert.ok(problems.has(item.problem.kind), 'вид проблемы: ' + item.problem.kind);
      seenProblem.add(item.problem.kind);
    }
  }
  // фикстура должна покрывать хотя бы три разных проблемы — по ней проверяются N4 и вид
  assert.ok(seenProblem.size >= 3, 'мало видов проблем: ' + [...seenProblem].join(','));
  assert.equal(dutiesProblemCount(data.items), data.items.filter((i) => i.problem).length);
});

test('fixtures/health_integrations.json — по контракту §3.3', () => {
  const data = fx('health_integrations.json');
  assert.ok(['ok', 'warn', 'fail'].includes(data.overall));
  assert.ok(Array.isArray(data.checks) && data.checks.length >= 6);
  const statuses = new Set(['ok', 'warn', 'fail', 'unknown']);
  const ids = new Set();
  for (const check of data.checks) {
    for (const key of ['id', 'title', 'status', 'detail', 'since', 'hint']) {
      assert.ok(key in check, 'health: нет поля ' + key);
    }
    assert.ok(statuses.has(check.status), 'статус проверки: ' + check.status);
    assert.ok(!ids.has(check.id), 'повтор проверки: ' + check.id);
    ids.add(check.id);
  }
  // набор проверок из ТЗ
  for (const id of ['gateway', 'dispatcher', 'peer_link', 'calendar_token', 'duties', 'backups', 'disk']) {
    assert.ok(ids.has(id), 'нет проверки ' + id);
  }
  // overall = худший статус
  const worst = data.checks.some((c) => c.status === 'fail') ? 'fail'
    : (data.checks.some((c) => c.status === 'warn') ? 'warn' : 'ok');
  assert.equal(data.overall, worst);
  // плашка здоровья на этой фикстуре обязана появиться
  const problems = healthProblems(data);
  assert.equal(problems.shown, true);
  assert.equal(problems.tone, 'fail');
});

/* ────────────────────────────────────────────────────── маршрут и меню */

test('#/today разбирается и роутером, и памятью маршрута', () => {
  const route = parseHash('#/today');
  assert.equal(route.name, 'today');
  assert.equal(route.view, 'today');
  assert.equal(route.roomId, null);
  assert.equal(parseRouteHash('#/today').kind, 'today');
  // соседние адреса не сломались
  assert.equal(parseHash('#/search?q=1').name, 'search');
  assert.equal(parseHash('#/settings').view, 'settings');
  assert.equal(parseHash('#/room/cafe/team').name, 'room');
});
