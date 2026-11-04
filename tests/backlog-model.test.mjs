import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  isoDay, addDays, dayDiff, shortDay, dayWithWeekday, dayTitle, pluralDays, dueInfo,
  kindLabel, statusLabel, isActive, isClosed, sortByDue, partitionBacklog, inboxCount,
  blockedCards, backlogQuestionId, groupStepsBySource, groupStepsByDue, groupSteps,
  buildAgenda, agendaRange, journalKindInfo, JOURNAL_FILTERS, journalMatchesType,
  journalMatchesQuery, filterJournal, journalDayKey, journalTime, groupJournal,
  mergeJournal, icsItemUrl, icsAllUrl, JOURNAL_PAGE,
  itemRequestId, filterByRequest, dueChipLabel, requestFilterMode, setRequestParam,
  REQUEST_FILTER_ALL, REQUEST_FILTER_ONE,
} from '../web/js/views/backlog-model.js';

const fixture = (name) => JSON.parse(readFileSync(new URL(`../fixtures/${name}`, import.meta.url), 'utf8'));
const backlog = fixture('backlog_cafe.json');
const journal = fixture('journal_cafe.json');
const needsYou = fixture('request_needs_you.json');

/* -------------------------------------------------------------------- даты */

test('isoDay/addDays/dayDiff работают по локальному календарю', () => {
  assert.equal(isoDay('2026-10-28'), '2026-10-28');
  assert.equal(addDays('2026-10-31', 1), '2026-11-01');
  assert.equal(addDays('2026-03-01', -1), '2026-02-28');
  assert.equal(addDays('2028-02-28', 1), '2028-02-29');
  assert.equal(dayDiff('2026-11-02', '2026-10-28'), 5);
  assert.equal(dayDiff('2026-10-27', '2026-10-28'), -1);
  assert.equal(dayDiff('нет даты', '2026-10-28'), null);
  assert.equal(isoDay(''), null);
  assert.equal(isoDay(null), null);
});

test('epoch-секунды превращаются в локальный день, а не UTC', () => {
  const noon = new Date(2026, 9, 28, 12, 0, 0);
  assert.equal(isoDay(Math.floor(noon.getTime() / 1000)), '2026-10-28');
  assert.equal(journalDayKey(Math.floor(noon.getTime() / 1000)), '2026-10-28');
});

test('shortDay, dayWithWeekday и dayTitle дают русские подписи', () => {
  assert.equal(shortDay('2026-10-28'), '28 окт');
  assert.equal(dayWithWeekday('2026-10-28'), '28 окт, ср');
  assert.equal(dayTitle('2026-10-28', '2026-10-28'), 'Сегодня');
  assert.equal(dayTitle('2026-10-29', '2026-10-28'), 'Завтра');
  assert.equal(dayTitle('2026-10-27', '2026-10-28'), 'Вчера');
  assert.equal(dayTitle('2026-11-02', '2026-10-28'), '2 ноя, пн');
  assert.equal(dayTitle(null, '2026-10-28'), '');
});

test('pluralDays склоняет 1/2/5 и 11–14', () => {
  assert.equal(pluralDays(1), 'день');
  assert.equal(pluralDays(2), 'дня');
  assert.equal(pluralDays(5), 'дней');
  assert.equal(pluralDays(11), 'дней');
  assert.equal(pluralDays(21), 'день');
});

/* ------------------------------------------------------------------- сроки */

test('dueInfo различает просрочку, сегодня, завтра, скоро и позже', () => {
  const today = '2026-10-28';
  assert.deepEqual(dueInfo(null, today), { state: 'none', days: null, label: 'Без срока' });
  assert.equal(dueInfo('2026-10-25', today).state, 'overdue');
  assert.equal(dueInfo('2026-10-25', today).days, -3);
  assert.match(dueInfo('2026-10-25', today).label, /Просрочено на 3 дня/);
  assert.equal(dueInfo('2026-10-28', today).state, 'today');
  assert.equal(dueInfo('2026-10-29', today).state, 'tomorrow');
  assert.equal(dueInfo('2026-11-01', today).state, 'soon');
  assert.equal(dueInfo('2026-11-20', today).state, 'later');
  assert.equal(dueInfo('2026-11-20', today).label, '20 ноя');
});

/* --------------------------------------------------------------- элементы */

test('kindLabel/statusLabel/isActive/isClosed', () => {
  assert.equal(kindLabel('next_step'), 'Шаг');
  assert.equal(kindLabel('future_question'), 'Будущий вопрос');
  assert.equal(kindLabel('unknown'), 'Элемент');
  assert.equal(statusLabel('asked'), 'Задан');
  assert.equal(isActive({ status: 'open' }), true);
  assert.equal(isActive({ status: 'asked' }), true);
  assert.equal(isActive({ status: 'done' }), false);
  assert.equal(isClosed({ status: 'dismissed' }), true);
  assert.equal(isClosed(null), false);
});

test('sortByDue ставит ближайший срок первым, без срока — в конец', () => {
  const items = [
    { id: 'c', due: null, created_at: 1 },
    { id: 'b', due: '2026-11-05', created_at: 2 },
    { id: 'a', due: '2026-10-30', created_at: 3 },
    { id: 'd', due: '2026-10-30', created_at: 1 },
  ];
  assert.deepEqual(sortByDue(items).map((i) => i.id), ['d', 'a', 'b', 'c']);
  assert.deepEqual(items.map((i) => i.id), ['c', 'b', 'a', 'd'], 'исходный массив не мутируется');
});

test('partitionBacklog из фикстуры: 1 вопрос, 1 решение, 4 шага, 1 будущий, 2 закрытых', () => {
  const parts = partitionBacklog(backlog.items);
  assert.equal(parts.questions.length, 1);
  assert.equal(parts.questions[0].id, 't_5c8e4a02:q_1');
  assert.equal(parts.decisions.length, 1);
  assert.equal(parts.decisions[0].text, 'Стартовать с набора из 6 моти, не с поштучной продажи');
  assert.equal(parts.steps.length, 4);
  assert.equal(parts.steps[0].due, '2026-10-30', 'шаги отсортированы по сроку');
  assert.equal(parts.future.length, 1);
  assert.equal(parts.future[0].id, 't_a1b2c3d4:fq_1');
  assert.deepEqual(parts.closed.map((i) => i.id).sort(), ['t_a1b2c3d4:fq_2', 't_a1b2c3d4:ns_5']);
  assert.equal(inboxCount(parts), 2);
  assert.equal(inboxCount({ ...parts, blocked: [{}] }), 3);
});

test('blockedCards достаёт blocked-карточки из деталей запросов', () => {
  const cards = blockedCards([needsYou]);
  assert.equal(cards.length, 1);
  assert.equal(cards[0].task_id, 't_5c8e4a03');
  assert.equal(cards[0].request_id, 't_5c8e4a02');
  assert.match(cards[0].text, /Подготовить подачу в Роспатент/);
  assert.match(cards[0].hint, /Пакет документов готов/);
  assert.equal(blockedCards([]).length, 0);
  assert.equal(blockedCards(null).length, 0);
});

test('backlogQuestionId вытаскивает id вопроса для actions.answer', () => {
  assert.equal(backlogQuestionId('t_5c8e4a02:q_1'), 'q_1');
  assert.equal(backlogQuestionId('q_7'), 'q_7');
  assert.equal(backlogQuestionId('t_a1b2c3d4:ns_1'), null);
  assert.equal(backlogQuestionId(''), null);
});

/* ------------------------------------------------------------- группировки */

test('groupStepsBySource группирует по запросу и сортирует группы по сроку', () => {
  const steps = partitionBacklog(backlog.items).steps;
  const groups = groupStepsBySource(steps);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].key, 't_a1b2c3d4');
  assert.match(groups[0].title, /Дорожная карта проекта/);
  assert.equal(groups[0].due, '2026-10-30');
  assert.deepEqual(groups[0].items.map((i) => i.due), ['2026-10-30', '2026-11-01', '2026-11-05', '2026-11-06']);
});

test('groupStepsBySource держит несколько источников в правильном порядке', () => {
  const steps = [
    { id: 'b1', source_request: 't_b', source_title: 'Б', due: '2026-11-10', created_at: 1 },
    { id: 'a1', source_request: 't_a', source_title: 'А', due: null, created_at: 2 },
    { id: 'b0', source_request: 't_b', source_title: 'Б', due: '2026-11-02', created_at: 3 },
  ];
  const groups = groupStepsBySource(steps);
  assert.deepEqual(groups.map((g) => g.key), ['t_b', 't_a']);
  assert.equal(groups[0].due, '2026-11-02');
  assert.deepEqual(groups[0].items.map((i) => i.id), ['b0', 'b1']);
});

test('groupStepsByDue раскладывает по корзинам и не показывает пустые', () => {
  const steps = [
    { id: 'o', due: '2026-10-26' },
    { id: 't', due: '2026-10-28' },
    { id: 'm', due: '2026-10-29' },
    { id: 'w', due: '2026-11-01' },
    { id: 'l', due: '2026-12-01' },
    { id: 'n', due: null },
  ];
  const groups = groupStepsByDue(steps, '2026-10-28');
  assert.deepEqual(groups.map((g) => g.key), ['overdue', 'today', 'tomorrow', 'week', 'later', 'none']);
  assert.deepEqual(groups[0].items.map((i) => i.id), ['o']);
  assert.equal(groups[0].tone, 'danger');
  assert.equal(groups[1].tone, 'warn');
  assert.equal(groups[5].items[0].id, 'n');
  assert.deepEqual(groupSteps(steps, 'due', '2026-10-28').map((g) => g.key), groups.map((g) => g.key));
});

/* -------------------------------------------------------------- повестка */

test('buildAgenda: просроченные сверху, горизонт 14 дней, без срока не входит', () => {
  const items = [
    { id: 'past', kind: 'next_step', status: 'open', due: '2026-10-20' },
    { id: 'today', kind: 'next_step', status: 'open', due: '2026-10-28' },
    { id: 'horizon', kind: 'next_step', status: 'open', due: '2026-11-10' },
    { id: 'outside', kind: 'next_step', status: 'open', due: '2026-11-11' },
    { id: 'none', kind: 'next_step', status: 'open', due: null },
    { id: 'done', kind: 'next_step', status: 'done', due: '2026-10-29' },
    { id: 'future', kind: 'future_question', status: 'open', due: '2026-10-29' },
  ];
  const agenda = buildAgenda(items, '2026-10-28', 14);
  assert.deepEqual(agenda.map((g) => g.date), ['2026-10-20', '2026-10-28', '2026-11-10']);
  assert.equal(agenda[0].overdue, true);
  assert.equal(agenda[1].title, 'Сегодня');
  assert.equal(agenda[2].overdue, false);
  assert.equal(agenda.some((g) => g.items.some((i) => i.id === 'done')), false);
  assert.equal(agenda.some((g) => g.items.some((i) => i.id === 'future')), false);
});

test('buildAgenda из фикстуры: 4 открытых шага с датой', () => {
  const agenda = buildAgenda(backlog.items, '2026-10-28', 14);
  const ids = agenda.flatMap((g) => g.items.map((i) => i.id));
  assert.equal(ids.length, 4);
  assert.equal(agenda[0].date, '2026-10-30');
  assert.match(agendaRange(14), /Ближайшие 14 дней/);
  assert.deepEqual(buildAgenda(backlog.items, null), []);
});

/* ------------------------------------------------------------------ журнал */

test('journalKindInfo знает все типы из API и даёт запасной вариант', () => {
  for (const kind of ['request_created', 'request_done', 'question_open', 'question_answered',
    'decision_proposed', 'decision_accepted', 'step_added', 'step_done', 'ingest', 'error']) {
    const info = journalKindInfo(kind);
    assert.ok(info.icon && info.label && info.tone && info.group, `нет метаданных для ${kind}`);
  }
  assert.equal(journalKindInfo('request_done').tone, 'ok');
  assert.equal(journalKindInfo('question_open').icon, 'comment-draft');
  assert.equal(journalKindInfo('decision_proposed').icon, 'lightbulb');
  assert.equal(journalKindInfo('critic_verdict').icon, 'verified');
  assert.equal(journalKindInfo('revision_created').icon, 'debug-restart');
  assert.equal(journalKindInfo('critic').icon, 'verified', 'короткие имена тоже понимаются');
  assert.equal(journalKindInfo('ingested').icon, 'sync');
  assert.equal(journalKindInfo('created').icon, 'comment-discussion');
  assert.equal(journalKindInfo('совсем-новый').label, 'Событие');
  assert.deepEqual(JOURNAL_FILTERS.map((f) => f.value), ['all', 'requests', 'questions', 'decisions', 'steps']);
});

test('filterJournal фильтрует по типу и ищет по тексту и автору', () => {
  const items = journal.items;
  const questions = filterJournal(items, { type: 'questions' });
  assert.deepEqual(questions.map((e) => e.kind), ['question_open']);
  const decisions = filterJournal(items, { type: 'decisions' });
  assert.deepEqual(decisions.map((e) => e.kind), ['decision_proposed']);
  assert.equal(filterJournal(items, { type: 'all' }).length, items.length);
  assert.equal(filterJournal(items, { q: 'упаковк' }).length, 2);
  assert.equal(filterJournal(items, { q: 'BIZ-OPS' }).length >= 1, true, 'автор ищется без учёта регистра');
  assert.equal(filterJournal(items, { q: '  ' }).length, items.length);
  assert.equal(filterJournal(null, {}).length, 0);
  assert.equal(journalMatchesType({ kind: 'room_created' }, 'requests'), false);
  assert.equal(journalMatchesQuery({ text: 'Привет', actor: 'mikhail' }, 'прив'), true);
});

test('groupJournal группирует по дням, новые сверху', () => {
  const at = (d, h) => Math.floor(new Date(2026, 9, d, h, 0, 0).getTime() / 1000);
  const items = [
    { id: 'a', at: at(27, 10), kind: 'step_added', text: 'старое' },
    { id: 'b', at: at(28, 12), kind: 'request_done', text: 'сегодня' },
    { id: 'c', at: at(28, 9), kind: 'question_open', text: 'сегодня утром' },
    { id: 'd', at: at(26, 18), kind: 'room_created', text: 'позавчера' },
  ];
  const now = at(28, 20);
  const groups = groupJournal(items, now);
  assert.deepEqual(groups.map((g) => g.title), ['Сегодня', 'Вчера', '26 окт, пн']);
  assert.deepEqual(groups[0].items.map((i) => i.id), ['b', 'c']);
  assert.equal(journalTime(items[1].at), '12:00');
  assert.equal(groupJournal([], now).length, 0);
});

test('mergeJournal убирает дубли и держит порядок по времени', () => {
  const merged = mergeJournal(
    [{ id: 'a', at: 100 }, { id: 'b', at: 300 }],
    [{ id: 'b', at: 300 }, { id: 'c', at: 200 }],
  );
  assert.deepEqual(merged.map((e) => e.id), ['b', 'c', 'a']);
  assert.equal(mergeJournal([], []).length, 0);
  assert.equal(JOURNAL_PAGE, 50);
});

/* --------------------------------------------------------------------- ics */

test('ics-ссылки кодируют комнату и id шага', () => {
  assert.equal(icsItemUrl('cafe', 't_a1b2c3d4:ns_1'),
    '/api/ics?room=cafe&items=t_a1b2c3d4%3Ans_1');
  assert.equal(icsAllUrl('cafe'), '/api/ics?room=cafe&all=1');
  assert.equal(icsItemUrl('ру', null), '/api/ics?room=%D1%80%D1%83&items=');
});

/* -------------------------------------------- фильтр «Только этот запрос» */

test('filterByRequest оставляет только дела одного запроса', () => {
  const items = [
    { id: 't_a:ns_1', kind: 'next_step', source_request: 't_a' },
    { id: 't_b:ns_1', kind: 'next_step', source_request: 't_b' },
    { id: 't_b:q_1', kind: 'open_question', source_request: 't_b' },
  ];
  assert.deepEqual(filterByRequest(items, 't_b').map((i) => i.id), ['t_b:ns_1', 't_b:q_1']);
  assert.equal(filterByRequest(items, '').length, 3);
  assert.deepEqual(filterByRequest(null, 't_a'), []);
  assert.equal(itemRequestId({ source_request: ' t_a ' }), 't_a');
  assert.equal(itemRequestId(null), '');
});

test('filterByRequest не портит исходный список и работает на фикстуре', () => {
  const original = backlog.items.slice();
  const rid = original[0].source_request;
  const only = filterByRequest(backlog.items, rid);
  assert.ok(only.length >= 1);
  assert.ok(only.every((i) => i.source_request === rid));
  assert.equal(backlog.items.length, original.length);
});

test('requestFilterMode и setRequestParam переключают «Все · Только этот запрос»', () => {
  assert.equal(requestFilterMode(''), REQUEST_FILTER_ALL);
  assert.equal(requestFilterMode('t_a'), REQUEST_FILTER_ONE);
  const hash = '#/room/cafe/backlog';
  assert.equal(setRequestParam(hash, 't_a1'), '#/room/cafe/backlog?request=t_a1');
  assert.equal(setRequestParam(hash + '?request=t_a1', ''), hash);
  assert.equal(setRequestParam(hash + '?x=1&request=t_a1', 't_b'), '#/room/cafe/backlog?x=1&request=t_b');
  assert.equal(setRequestParam(hash + '?request=t_a1', 't_a1'), '#/room/cafe/backlog?request=t_a1');
});

test('dueChipLabel даёт короткую подпись чипа срока', () => {
  const today = new Date(2026, 9, 28);
  assert.equal(dueChipLabel(null, today), 'Срок');
  assert.equal(dueChipLabel('2026-10-28', today), 'Сегодня');
  assert.equal(dueChipLabel('2026-10-29', today), 'Завтра');
  assert.equal(dueChipLabel('2026-10-20', today), 'Просрочено');
  assert.equal(dueChipLabel('2026-11-15', today), '15 ноя');
});
