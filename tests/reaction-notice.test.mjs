/**
 * reaction-notice.test.mjs — W20-U4 (P9): уведомление о реакции руководителя.
 *
 * Проверяем то, что видит человек:
 *  • строка у вопроса — «Ответ учтён: работа пересмотрена · 13:48», а не сырой текст API;
 *  • «непрочитанное» считается по локальной отметке (браузер знает, что смотрели);
 *  • бейдж вкладки и отметка в «Недавнем» появляются только для нового;
 *  • отметка «прочитано» живёт в хранилище и переживает перезагрузку модуля.
 *
 * Красный на старом коде: модуля web/js/views/reaction-notice.js не было вовсе —
 * ни строки «Ответ учтён», ни счётчика новых реакций, ни отметки в «Недавнем».
 */

import test from 'node:test';
import assert from 'node:assert/strict';

const mod = await import('../web/js/views/reaction-notice.js');

/* ───────────────────────────────────────── подписи: одни слова для всех мест */

test('noticeLine: «учтено: …» не дублируется в строке «Ответ учтён»', () => {
  const line = mod.noticeLine({ status: 'applied', label: 'учтено: работа пересмотрена', at: 1762800000 });
  assert.match(line, /^Ответ учтён: работа пересмотрена/);
  assert.ok(!/учтено: учтено/i.test(line), 'нет двойного «учтено»');
  assert.match(line, /\d\d:\d\d$/, 'время реакции в конце строки');
});

test('noticeLine: «менять ничего не нужно» и «принято» — те же слова', () => {
  assert.match(mod.noticeLine({ status: 'no_change', label: 'учтено: менять ничего не нужно', at: 1762800000 }),
    /^Ответ учтён: менять ничего не нужно/);
  assert.match(mod.noticeLine({ status: 'received', label: 'руководитель прочитал', at: 1762800000 }),
    /^Руководитель прочитал ответ/);
  assert.match(mod.noticeLine({ status: 'applied', label: 'принято', at: 0 }), /^Ответ учтён: принято$/);
});

test('noticeLine: шаг и решение называют себя, пустая реакция — пустая строка', () => {
  assert.match(mod.noticeLine({ status: 'accepted', label: 'срок изменён', at: 0 }, { kind: 'step' }),
    /^Шаг учтён: срок изменён$/);
  assert.match(mod.noticeLine({ status: 'applied', label: 'принято', at: 0 }, { kind: 'decision' }),
    /^Решение учтено: принято$/);
  assert.equal(mod.noticeLine(null), '');
  assert.equal(mod.clockOf(0), '');
});

test('reactionKind: вид реакции по метке руководителя', () => {
  assert.equal(mod.reactionKind('[Руководитель · реакция на ответ q_1] Учтено'), 'answer');
  assert.equal(mod.reactionKind('[Руководитель · реакция на шаг ns_1] принято'), 'step');
  assert.equal(mod.reactionKind('[Руководитель · реакция на решение d_1] принято'), 'decision');
  assert.equal(mod.reactionKind(''), 'answer');
});

/* ────────────────────────────────────────── разбор сводки и «что не прочитано» */

const ROOM = {
  id: 'kafe-leto',
  reactions_unseen: 3,
  last_reaction_at: 1762800300,
  reactions: {
    req_alpha: { count: 1, last_at: 1762800000 },
    req_beta: { count: 2, last_at: 1762800300 },
  },
};

test('roomReactionInfo: сводка → карта реакций по запросам', () => {
  const info = mod.roomReactionInfo(ROOM);
  assert.equal(info.count, 3);
  assert.equal(info.last_at, 1762800300);
  assert.deepEqual(Object.keys(info.requests).sort(), ['req_alpha', 'req_beta']);
  assert.equal(info.requests.req_beta.count, 2);
});

test('roomReactionInfo: сводка без карты запросов — реакция всё равно видна', () => {
  const info = mod.roomReactionInfo({ id: 'r', last_reaction_at: 1762800300 });
  assert.equal(info.last_at, 1762800300);
  assert.equal(info.count >= 1, true);
  const unseen = mod.unseenOf(info, { at: 0, req: {} });
  assert.equal(unseen.count >= 1, true, 'без карты запросов реакция считается новой');
});

test('unseenOf: отметка комнаты скрывает прочитанное', () => {
  const info = mod.roomReactionInfo(ROOM);
  const fresh = mod.unseenOf(info, { at: 0, req: {} });
  assert.equal(fresh.count, 3);
  assert.deepEqual(fresh.requests.sort(), ['req_alpha', 'req_beta']);

  const half = mod.unseenOf(info, { at: 1762800000, req: {} });
  assert.equal(half.count, 2, 'реакция req_alpha уже прочитана');
  assert.deepEqual(half.requests, ['req_beta']);

  const all = mod.unseenOf(info, { at: 1762800300, req: {} });
  assert.equal(all.count, 0);
});

test('unseenOf: открытый запрос гасит только свою отметку', () => {
  const info = mod.roomReactionInfo(ROOM);
  const seen = { at: 0, req: { req_beta: 1762800300 } };
  const unseen = mod.unseenOf(info, seen);
  assert.deepEqual(unseen.requests, ['req_alpha']);
  assert.equal(unseen.count, 1);
});

test('badgeText: число и подсказка «сколько и где»', () => {
  const text = mod.badgeText({ count: 2, requests: ['a', 'b'] });
  assert.equal(text.label, '2');
  assert.match(text.title, /Новые реакции руководителя: 2 реакции в 2 задачи/);
  assert.equal(mod.badgeText({ count: 0, requests: [] }).count, 0);
  assert.equal(mod.badgeText({ count: 120, requests: ['a'] }).label, '99+');
});

test('склонения: реакции и запросы', () => {
  assert.equal(mod.reactionsWord(1), 'реакция');
  assert.equal(mod.reactionsWord(2), 'реакции');
  assert.equal(mod.reactionsWord(5), 'реакций');
  assert.equal(mod.reactionsWord(11), 'реакций');
  assert.equal(mod.requestsWord(1), 'задачи');
  assert.equal(mod.requestsWord(3), 'задачи');
  assert.equal(mod.requestsWord(8), 'задач');
});

/* ────────────────────────────────────────────── локальная отметка «прочитано» */

function memoryStore() {
  const data = new Map();
  return {
    data,
    get: (key) => (data.has(key) ? data.get(key) : null),
    set: (key, value) => { data.set(key, String(value)); },
  };
}

test('createSeen: отметка запроса и комнаты, состояние переживает перезагрузку', () => {
  const store = memoryStore();
  const seen = mod.createSeen('kafe-leto', { store });
  assert.equal(seen.markRequest('req_alpha', 1762800000), true);
  assert.equal(seen.markRequest('req_alpha', 1762800000), false, 'повтор не меняет состояние');
  assert.equal(seen.markRequest('req_alpha', 1000), false, 'старое время не откатывает отметку');
  assert.equal(seen.markAll(1762800100), true);
  assert.deepEqual(seen.snapshot(), { at: 1762800100, req: { req_alpha: 1762800000 } });

  const again = mod.createSeen('kafe-leto', { store });
  assert.deepEqual(again.snapshot(), { at: 1762800100, req: { req_alpha: 1762800000 } });
  assert.equal(mod.reactionsKey('kafe-leto'), 'teamroom.reactions.kafe-leto');
});

test('createSeen: битое хранилище не ломает отметки', () => {
  const store = memoryStore();
  store.set('teamroom.reactions.kafe-leto', '{не json');
  const seen = mod.createSeen('kafe-leto', { store });
  assert.deepEqual(seen.snapshot(), { at: 0, req: {} });
  assert.equal(seen.markAll(50), true);
});

/* ───────────────────────────────────────── сводка → бейдж (без DOM) */

test('ingestSummary/unseenFor: бейдж появляется на новых и гаснет после просмотра', () => {
  mod.resetReactionState();
  let events = 0;
  const off = mod.subscribeReactions(() => { events += 1; });
  assert.equal(mod.unseenFor('kafe-leto').count, 0, 'пока данных нет — новых нет');

  mod.ingestSummary({ rooms: [ROOM] });
  assert.equal(mod.unseenFor('kafe-leto').count, 3);
  assert.equal(events, 1, 'подписчик узнал об изменении один раз');

  mod.ingestSummary({ rooms: [ROOM] });
  assert.equal(events, 1, 'те же данные не будят подписчиков');

  // Открыли запрос — отметка локальная, сводка не меняется.
  assert.equal(mod.markRequestSeen('kafe-leto', 'req_beta', 1762800300), true);
  assert.equal(mod.unseenFor('kafe-leto').count, 1);
  assert.equal(events, 2);

  off();
  mod.markRequestSeen('kafe-leto', 'req_alpha', 1762800000);
  assert.equal(events, 2, 'после отписки событий нет');
  assert.equal(mod.unseenFor('kafe-leto').count, 0);
});

test('ingestSummary: комнату убрали — реакций по ней больше нет', () => {
  mod.resetReactionState();
  mod.ingestSummary({ rooms: [ROOM] });
  assert.equal(mod.roomReactions('kafe-leto').count, 3);
  mod.ingestSummary({ rooms: [{ id: 'drugaya' }] });
  assert.equal(mod.roomReactions('kafe-leto').count, 0);
});

test('ingestSummary: сервер без новых полей — пустая сводка, без ошибок', () => {
  mod.resetReactionState();
  assert.doesNotThrow(() => mod.ingestSummary({ ok: true, rooms: [{ id: 'staraya' }] }));
  assert.equal(mod.unseenFor('staraya').count, 0);
  assert.equal(mod.unseenFor('net-takoy').count, 0);
  assert.doesNotThrow(() => mod.ingestSummary(null));
});
