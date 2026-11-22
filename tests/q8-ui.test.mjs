/**
 * q8-ui.test.mjs — W29B-Q8: «Итог дня» и поиск по памяти комнат.
 *
 * Что проверяем (только то, что можно проверить словами, без браузера):
 *  * счётчики итога совпадают со списками — число в шапке = число строк;
 *  * строка «ждёт вас» всегда со ссылкой (действием) — правило «без тупиков»;
 *  * расход и «Короче» показывают цену ДО нажатия; ошибка «Короче» не прячет итог;
 *  * пустой день объясняет зачем экран и что сделать;
 *  * поиск по памяти: подпись источника и дата в каждой находке, пустой запрос
 *    ничего не ищет;
 *  * палитра: находки памяти идут разделом «Память» и ведут в запрос.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  counterChips, decisionRows, doneRows, emptyText, fileRows, isEmptyDaily, money,
  periodTitle, sectionTitle, shortButtonText, shortState, spentText, stoppedRows,
  waitingRows,
} from '../web/js/views/daily-model.js';
import { hiddenCount, resultRows, statusText } from '../web/js/views/memory-search.js';
import { composeList, routeForHit, sectionForKind } from '../web/js/views/palette-model.js';

const DAY = {
  generated_at: 1763700000,
  day: '2026-11-21',
  day_start: 1763680800,
  period: 'evening',
  period_title: 'Вечер',
  counters: { done: 1, decisions: 1, files: 1, waiting: 2, stopped: 1, all: 6,
    spent_calls: 12, spent_tokens: 145000, spent_usd: 0.42 },
  done: [{ room: 'cafe', room_title: 'Кофейня', request_id: 't_cafe1',
    request_title: 'Меню на неделю', at: 1763690000 }],
  decisions: [{ room: 'cafe', room_title: 'Кофейня', id: 't_cafe1:d_1', at: 1763691000,
    title: 'Берём арабику 1 кг', status: 'accepted', request_id: 't_cafe1',
    request_title: 'Меню на неделю' }],
  files: [{ room: 'cafe', room_title: 'Кофейня', name: 'меню-на-неделю.md', at: 1763692000 }],
  waiting: [
    { room: 'cafe', room_title: 'Кофейня', kind: 'question', text: 'Какой объём зерна брать?',
      request_id: 't_cafe1', request_title: 'Меню на неделю', since: 1763693000,
      recommended: 'Ответить на вопрос' },
    { room: 'laundry', room_title: 'Химчистка', kind: 'blocked', text: 'Прайс не найден',
      request_id: 't_laundry1', request_title: 'Прайс на чистку', since: 1763694000,
      recommended: 'Ответить, что делать дальше' },
  ],
  waiting_total: 2,
  stopped: [{ room: 'cafe', room_title: 'Кофейня', request_id: 't_cafe2',
    request_title: 'Заказ зерна', at: 1763695000, reason: 'Не нашлась папка', kind: 'failed' }],
  spent: { calls: 12, tokens: 145000, cost_usd: 0.42, source: 'state.db', note: '' },
  empty: false,
};

const EMPTY_DAY = {
  generated_at: 1763700000, day: '2026-11-21', period: 'morning', period_title: 'Утро',
  counters: { done: 0, decisions: 0, files: 0, waiting: 0, stopped: 0, all: 0 },
  done: [], decisions: [], files: [], waiting: [], stopped: [], empty: true,
};

test('счётчики шапки совпадают со списками', () => {
  const chips = counterChips(DAY);
  const byKey = Object.fromEntries(chips.map((c) => [c.key, Number(c.text.split(': ')[1])]));
  assert.equal(byKey.done, doneRows(DAY).length);
  assert.equal(byKey.decisions, decisionRows(DAY).length);
  assert.equal(byKey.waiting, waitingRows(DAY).length);
  assert.equal(byKey.stopped, stoppedRows(DAY).length);
  assert.equal(byKey.done, DAY.counters.done);
});

test('строка «ждёт вас» всегда со ссылкой — тупиков нет', () => {
  for (const row of waitingRows(DAY)) {
    assert.ok(row.href.startsWith('#/room/'), row.text);
    assert.equal(row.action, 'open');
    assert.ok(row.recommended, 'у строки есть следующий шаг');
  }
});

test('ссылки итога ведут на страницу запроса', () => {
  assert.equal(doneRows(DAY)[0].href, '#/room/cafe/request/t_cafe1');
  assert.equal(decisionRows(DAY)[0].href, '#/room/cafe/request/t_cafe1');
  assert.equal(stoppedRows(DAY)[0].kind, 'failed');
});

test('расход одной строкой и деньги по-русски', () => {
  assert.equal(spentText(DAY.spent), '12 запусков · 145 тыс. токенов · $0,42');
  assert.equal(spentText({}), '');
  assert.equal(money(0.004), '$0,0040');
  assert.equal(money(0), '');
});

test('«Короче» показывает цену до нажатия', () => {
  const withPrice = shortButtonText({ input_tokens: 1200, output_tokens: 400, cost_usd: 0.003 });
  assert.match(withPrice, /≈ 1600 токенов/);
  assert.match(withPrice, /\$0,0030/);
  const unknown = shortButtonText({ input_tokens: 1200, output_tokens: 400, cost_usd: null });
  assert.match(unknown, /токенов/);
  assert.doesNotMatch(unknown, /\$/);
});

test('ошибка «Короче» не прячет итог', () => {
  const state = shortState({ ok: false, error: 'Модель не ответила за 120 с' });
  assert.equal(state.ok, false);
  assert.match(state.message, /не ответила/);
  assert.match(state.hint, /остаётся/);
  const ok = shortState({ ok: true, text: 'Всё сделано', fake: true });
  assert.equal(ok.text, 'Всё сделано');
  assert.match(ok.message, /песочниц/i);
});

test('пустой день объясняет зачем и что сделать', () => {
  assert.equal(isEmptyDaily(EMPTY_DAY), true);
  const info = emptyText(EMPTY_DAY);
  assert.match(info.title, /ничего не произошло/);
  assert.match(info.text, /Итог собирается сам/);
  assert.equal(isEmptyDaily(DAY), false);
});

test('период и заголовки разделов', () => {
  assert.equal(periodTitle(DAY), 'Вечер · 21 ноября');
  assert.equal(sectionTitle('done', 3), 'Сделано · 3');
  assert.equal(sectionTitle('done', 0), 'Сделано');
  assert.equal(fileRows(DAY)[0].name, 'меню-на-неделю.md');
});

const MEMORY = {
  q: 'подрядчик',
  total: 2,
  by_kind: { decision: 1, note: 1 },
  items: [
    { kind: 'decision', title: 'Доставку отдаём подрядчику', snippet: '…доставку отдаём подрядчику…',
      citation: 'Решение · 12.11.2026 · accepted', at: 1763000000, room: 'cafe', id: 't_cafe1:d_2',
      request_id: 't_cafe1', route: '#/room/cafe/request/t_cafe1', source: 'реестр решений' },
    { kind: 'note', title: 'Заметка про подрядчика', snippet: '…подрядчик возит по вторникам…',
      citation: 'Заметка · 10.11.2026', at: 1762800000, room: 'cafe', id: 'file:x',
      request_id: '', route: '#/room/cafe/backlog', source: 'заметка Vault' },
  ],
};

test('память: вид, цитата и дата в каждой находке', () => {
  const rows = resultRows(MEMORY);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].kindTitle, 'Решение');
  assert.ok(rows[0].snippet.includes('подрядчику'));
  assert.match(rows[0].citation, /12\.11\.2026/);
  assert.ok(rows.every((row) => row.route.startsWith('#/room/cafe')));
});

test('память: пустой запрос ничего не ищет и говорит об этом', () => {
  const status = statusText('', null);
  assert.match(status.text, /Например/);
  assert.equal(status.tone, 'muted');
  const short = statusText('п', null);
  assert.match(short.text, /Ещё одна буква/);
  const none = statusText('подрядчик', { total: 0, note: 'В памяти ничего не нашлось' });
  assert.match(none.text, /ничего не нашлось/);
  assert.equal(none.tone, 'warn');
  const found = statusText('подрядчик', MEMORY);
  assert.match(found.text, /Нашлось: 2/);
  assert.match(found.text, /Решение: 1/);
});

test('память: «показать все» считает скрытое', () => {
  const many = { items: new Array(9).fill(0).map((_v, i) => ({ kind: 'decision', title: 'r' + i })) };
  assert.equal(hiddenCount(many, 6), 3);
  assert.equal(hiddenCount(MEMORY, 6), 0);
});

test('палитра: память — отдельный раздел и ведёт в запрос', () => {
  assert.equal(sectionForKind('memory').title, 'Память');
  assert.equal(routeForHit({ kind: 'memory', room: 'cafe', request_id: 't_cafe1' }),
    '#/room/cafe/request/t_cafe1');
  assert.equal(routeForHit({ kind: 'memory', room: 'cafe', route: '#/room/cafe/backlog' }),
    '#/room/cafe/backlog');
  const groups = composeList({
    commands: [], recents: [], query: 'подрядчик',
    hits: [{ kind: 'memory', room: 'cafe', id: 'd1', request_id: 't_cafe1',
      title: 'Доставку отдаём подрядчику', snippet: '…подрядчику…' }],
  });
  const memory = groups.find((group) => group.id === 'hits:memory');
  assert.ok(memory, 'раздел «Память» есть');
  assert.equal(memory.items[0].action.hash, '#/room/cafe/request/t_cafe1');
});
