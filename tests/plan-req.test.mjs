/**
 * plan-req.test.mjs — «Проектный режим» на странице брифа/части работы (PLAN-FE-REQ).
 *
 * Что проверяем (критерии волны 9, §4–§6):
 *  • флажок «Это бриф проекта»: подсказка дословно, правило «включён в пустой
 *    комнате», поле `brief` уходит только в проектной комнате;
 *  • бейдж «бриф» и мини-счётчик «частей работы: N, идёт: M» из `streams_summary`;
 *  • плашка «Предложенная карта»: части работы компактно, «Запустить всё»,
 *    «Открыть карту проекта», пустое состояние, ошибка сети по-русски;
 *  • крошка «Комната › Бриф › Часть работы» (со ссылкой на бриф и без неё);
 *  • журнал плана: «добавил часть работы «X», потому что …»;
 *  • Артефакты: путь внутри папки проекта — относительно неё;
 *  • в видимом тексте нет технических слов.
 *
 * DOM — мини-DOM из tests/fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';

installFakeDom();

const {
  BRIEF_LABEL, BRIEF_HINT, PLAN_TITLE, PLAN_EMPTY_TEXT, PLAN_ERROR_TEXT,
  planOf, isBrief, streamsOf, summaryOf, summaryText, streamStatusInfo,
  proposedCount, briefDefaultOn, briefPayload, briefBadge, briefSummaryChip,
  buildPlanCard, planLogEvents, whoName,
} = await import('../web/js/views/plan-req.js');
const { briefCrumbItems } = await import('../web/js/scope.js');
const { relativeToDir } = await import('../web/js/views/request-parts.js');

test.after(() => uninstallFakeDom());

/* --------------------------------------------------------------- помощники */

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

function all(node, out = []) {
  if (!node) return out;
  if (node.nodeType === 1) out.push(node);
  for (const child of node.childNodes || []) all(child, out);
  return out;
}

function byClass(root, cls) {
  return all(root).filter((n) => String(n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

const PLAN = {
  version: 2,
  brief_root_id: 't_root',
  status: 'draft',
  streams: [
    {
      id: 's1', title: 'Упаковка', goal: 'Собрать варианты коробок', owner: 'biz-ops',
      members: ['biz-ops', 'biz-design'], depends_on: [], due: '2026-11-05',
      status: 'proposed', reason: 'первый шаг',
    },
    {
      id: 's2', title: 'Логистика', goal: 'Посчитать доставку', owner: 'biz-ops',
      members: [], depends_on: ['s1'], due: null, status: 'running', reason: '',
    },
    {
      id: 's3', title: 'Цены', goal: 'Прайс', owner: 'biz-fin', members: [], depends_on: [],
      due: null, status: 'done', reason: '',
    },
  ],
  log: [],
};

/* ------------------------------------------------------- тексты флажка брифа */

test('флажок брифа: подпись и подсказка — дословно из ТЗ, без технических слов', () => {
  assert.equal(BRIEF_LABEL, 'Это бриф проекта');
  assert.ok(BRIEF_HINT.includes('Руководитель комнаты не будет глубоко исследовать'));
  assert.ok(BRIEF_HINT.includes('предложит карту частей работы'));
  assert.ok(BRIEF_HINT.includes('«Запустить»'));
  assert.ok(!/директор|dispatcher|kanban|t_/i.test(BRIEF_LABEL + BRIEF_HINT));
});

test('флажок брифа: в пустой проектной комнате включён, дальше — только руками', () => {
  assert.equal(briefDefaultOn({ projectMode: true, emptyRoom: true }), true);
  assert.equal(briefDefaultOn({ projectMode: true, emptyRoom: false }), false,
    'в комнате уже есть запросы — по умолчанию выключен');
  assert.equal(briefDefaultOn({ projectMode: false, emptyRoom: true }), false,
    'без проектного режима флажка нет');
  assert.equal(briefDefaultOn({ projectMode: true, emptyRoom: true, touched: true, brief: false }), false,
    'ручной выбор Михаила важнее правила по умолчанию');
  assert.equal(briefDefaultOn({ projectMode: true, emptyRoom: false, touched: true, brief: true }), true);
});

test('флажок брифа: `brief` в теле запроса — только в проектной комнате', () => {
  assert.deepEqual(briefPayload({ projectMode: false, brief: true }), {});
  assert.deepEqual(briefPayload({ projectMode: true, brief: true }), { brief: true });
  assert.deepEqual(briefPayload({ projectMode: true, brief: false }), { brief: false });
  assert.deepEqual(briefPayload(), {}, 'без аргументов поле не добавляем');
});

/* ----------------------------------------------------- бейджи, сводка, статусы */

test('бриф: поле читается терпимо, сводка — total/running или count/active', () => {
  assert.equal(isBrief({ brief: true }), true);
  assert.equal(isBrief({ is_brief: true }), true);
  assert.equal(isBrief({}), false);
  assert.equal(isBrief(null), false);

  assert.deepEqual(summaryOf({ streams_summary: { total: 4, running: 2 } }), { total: 4, running: 2 });
  assert.deepEqual(summaryOf({ streams_summary: { count: 3, active: 1 } }), { total: 3, running: 1 });
  assert.deepEqual(summaryOf({ streams_summary: { total: 0 } }), { total: 0, running: null });
  assert.equal(summaryOf({}), null, 'нет сводки — нет счётчика');
  assert.equal(summaryOf({ streams_summary: { total: 'много' } }), null);
  assert.equal(summaryOf(null), null);
});

test('счётчик: «частей работы: N, идёт: M»; пустая строка — сводки нет', () => {
  assert.equal(summaryText({ total: 4, running: 2 }), 'частей работы: 4, идёт: 2');
  assert.equal(summaryText({ total: 1, running: null }), 'частей работы: 1');
  assert.equal(summaryText(null), '');
  const chip = briefSummaryChip({ total: 4, running: 2 });
  assert.equal(text(chip), 'частей работы: 4, идёт: 2');
  assert.equal(briefSummaryChip(null), null);
});

test('бейдж «бриф» — короткая подпись и понятная подсказка', () => {
  const badge = briefBadge();
  assert.equal(text(badge).trim(), 'бриф');
  assert.match(badge.getAttribute('title'), /бриф проекта/i);
});

test('статусы частей работы по-русски, предложенные считаются', () => {
  assert.equal(streamStatusInfo('proposed').label, 'предложено');
  assert.equal(streamStatusInfo('running').label, 'идёт');
  assert.equal(streamStatusInfo('waiting_quota').label, 'ждёт лимита');
  assert.equal(streamStatusInfo('что-то').label, 'предложено', 'неизвестный статус — как предложенный');
  assert.equal(proposedCount(PLAN), 1);
  assert.equal(streamsOf(PLAN).length, 3);
  assert.equal(streamsOf(null).length, 0);
});

test('planOf: `{plan}` и сам план, пустой ответ — null', () => {
  assert.equal(planOf({ plan: PLAN }).streams.length, 3);
  assert.equal(planOf(PLAN).streams.length, 3);
  assert.equal(planOf({ plan: null }), null);
  assert.equal(planOf(null), null);
});

test('имена без системных id: руководитель комнаты и профиль', () => {
  assert.equal(whoName('manager'), 'руководитель комнаты');
  assert.equal(whoName('biz-ops'), 'ops');
  assert.equal(whoName('mikhail'), 'Михаил');
  assert.equal(whoName(''), '');
});

/* ------------------------------------------------- плашка «Предложенная карта» */

test('плашка: заголовок, части работы компактно, зависимости и сроки', () => {
  const card = buildPlanCard({ plan: PLAN, roomId: 'cafe' });
  assert.equal(text(byClass(card, 'plan-card__title')[0]), PLAN_TITLE);

  const rows = byClass(card, 'plan-stream');
  assert.equal(rows.length, 3, 'все части работы в списке');
  assert.deepEqual(rows.map((r) => r.getAttribute('data-stream')), ['s1', 's2', 's3']);

  const first = text(rows[0]);
  assert.match(first, /Упаковка/);
  assert.match(first, /Собрать варианты коробок/);
  assert.match(first, /ответственный: ops/);
  assert.match(first, /срок: 2026-11-05/);
  assert.match(first, /предложено/);

  const second = text(rows[1]);
  assert.match(second, /после «Упаковка»/, 'зависимость читается названием, а не id');
  assert.match(second, /идёт/);
});

test('плашка: «Запустить всё» зовёт обработчик, когда есть предложенные', () => {
  let called = 0;
  const card = buildPlanCard({ plan: PLAN, roomId: 'cafe', onLaunchAll: () => { called += 1; } });
  const btn = byClass(card, 'plan-card__launch')[0];
  assert.ok(btn, 'кнопка есть');
  assert.equal(text(btn), 'Запустить всё');
  assert.equal(btn.getAttribute('disabled'), null);
  btn.dispatch('click');
  assert.equal(called, 1);

  const link = byClass(card, 'plan-card__link')[0];
  assert.equal(link.getAttribute('href'), '#/room/cafe/roadmap');
  assert.match(text(link), /Открыть карту проекта/);
});

test('плашка: нечего запускать — кнопка недоступна и объясняет почему', () => {
  const plan = { ...PLAN, streams: PLAN.streams.map((s) => ({ ...s, status: 'done' })) };
  const card = buildPlanCard({ plan, roomId: 'cafe', onLaunchAll: () => { throw new Error('нельзя'); } });
  const btn = byClass(card, 'plan-card__launch')[0];
  assert.ok(btn.getAttribute('disabled') !== null, 'кнопка недоступна');
  assert.match(btn.getAttribute('title'), /уже запущены или убраны/);
  btn.dispatch('click');
});

test('плашка: «Запускаю…» пока идёт запуск, повторное нажатие не проходит', () => {
  let called = 0;
  const card = buildPlanCard({ plan: PLAN, roomId: 'cafe', launching: true, onLaunchAll: () => { called += 1; } });
  const btn = byClass(card, 'plan-card__launch')[0];
  assert.equal(text(btn), 'Запускаю…');
  assert.ok(btn.getAttribute('disabled') !== null, 'кнопка недоступна');
  btn.dispatch('click');
  assert.equal(called, 0);
});

test('плашка: пустая карта и ошибка сети — человеческим текстом, с повтором', () => {
  const empty = buildPlanCard({ plan: null, roomId: 'cafe' });
  assert.match(text(empty), new RegExp(PLAN_EMPTY_TEXT.slice(0, 24)));

  let retried = 0;
  const failed = buildPlanCard({
    plan: null, roomId: 'cafe', error: new Error('fetch failed'),
    onRetry: () => { retried += 1; },
  });
  const body = text(failed);
  assert.ok(body.includes(PLAN_ERROR_TEXT));
  assert.ok(!/fetch|Error|ECONN/i.test(body), 'технических слов в тексте нет');
  const retry = byClass(failed, 'btn').find((b) => /Повторить/.test(text(b)));
  retry.dispatch('click');
  assert.equal(retried, 1);
});

/* ---------------------------------------------------- крошка под-запроса */

test('крошка под-запроса: «Комната › Бриф › Часть работы», бриф — ссылкой', () => {
  const items = briefCrumbItems({
    roomId: 'cafe', roomTitle: 'Лето', briefRootId: 't_root',
    briefTitle: 'Запуск линейки', streamTitle: 'Упаковка',
  });
  assert.equal(items.length, 3);
  assert.equal(items[0].text, 'Комната «Лето»');
  assert.equal(items[0].href, '#/room/cafe/composer');
  assert.equal(items[1].text, 'Бриф «Запуск линейки»');
  assert.equal(items[1].href, '#/room/cafe/request/t_root');
  assert.equal(items[2].text, 'Часть работы «Упаковка»');
  assert.equal(items[2].current, true);
  assert.ok(!JSON.stringify(items).includes('s1'), 'id части работы на экран не выводим');
});

test('крошка под-запроса: без id и названия брифа — всё равно путь', () => {
  const items = briefCrumbItems({ roomId: 'cafe', streamTitle: 'Логистика' });
  assert.equal(items.length, 3);
  assert.equal(items[1].text, 'Бриф');
  assert.equal(items[1].href, undefined, 'некуда вести — ссылки нет');
  assert.equal(items[2].text, 'Часть работы «Логистика»');
  assert.deepEqual(briefCrumbItems({ roomId: 'cafe' }), [], 'без части работы крошки нет');
  assert.deepEqual(briefCrumbItems({ streamTitle: 'X' }), [], 'без комнаты крошки нет');
});

/* ------------------------------------------------------- журнал плана */

test('журнал плана: «добавил часть работы «X», потому что …»', () => {
  const plan = {
    ...PLAN,
    log: [
      { at: 1793200000, who: 'manager', kind: 'created', text: 'предложил карту: 3 части работы', stream_id: null },
      { at: 1793200100, who: 'manager', kind: 'added', text: 'появился новый блок работ', stream_id: 's1' },
      { at: 1793200200, who: 'mikhail', kind: 'changed', text: 'срок сдвинулся', stream_id: 's2' },
      { at: 1793200300, who: 'system', kind: 'dropped', text: 'передумали', stream_id: 's3' },
      { at: 1793200400, who: 'system', kind: 'quota', text: 'лимит провайдера', stream_id: 's2' },
      { at: 1793200500, who: 'manager', kind: 'launched', text: '', stream_id: 's2' },
    ],
  };
  const events = planLogEvents(plan);
  assert.equal(events.length, 6);
  assert.deepEqual(events.map((e) => e.kind), Array(6).fill('plan'));
  assert.equal(events[0].text, 'предложил карту: 3 части работы', 'фраза сервера не удваивается');
  assert.equal(events[1].text, 'добавил часть работы «Упаковка», потому что появился новый блок работ');
  assert.equal(events[1].actor, 'руководитель комнаты');
  assert.equal(events[2].actor, 'Михаил');
  assert.equal(events[2].text, 'изменил часть работы «Логистика»: срок сдвинулся');
  assert.equal(events[3].text, 'убрал часть работы «Цены», потому что передумали');
  assert.equal(events[4].text, 'часть работы «Логистика» ждёт лимита — очередь подождёт');
  assert.equal(events[5].text, 'запустил часть работы «Логистика»');
  assert.equal(events[1].request_id, 't_root', 'запись ведёт на бриф');
  assert.ok(!events.map((e) => e.text).join(' ').includes('s1'), 'id части работы в тексте нет');
});

test('журнал плана: пустой и битый лог не роняют вкладку', () => {
  assert.deepEqual(planLogEvents(null), []);
  assert.deepEqual(planLogEvents({ streams: [] }), []);
  assert.deepEqual(planLogEvents({ log: [null, 5, 'x'] }), []);
});

/* --------------------------------------------------- артефакты и папка проекта */

test('путь внутри папки проекта показывается относительно неё', () => {
  assert.equal(
    relativeToDir('C:\\TeamRoom\\Projects\\Лето\\2026-10-29 Запуск\\отчёт.md', 'C:\\TeamRoom\\Projects\\Лето'),
    '2026-10-29 Запуск\\отчёт.md',
  );
  assert.equal(
    relativeToDir('c:/TeamRoom/projects/Лето/итог.xlsx', 'C:\\TeamRoom\\Projects\\Лето\\'),
    'итог.xlsx',
    'регистр и разделители не важны',
  );
  assert.equal(relativeToDir('C:\\Другое\\файл.md', 'C:\\TeamRoom\\Projects\\Лето'), '',
    'вне папки проекта — полный путь как раньше');
  assert.equal(relativeToDir('C:\\TeamRoom\\Projects\\Лето', 'C:\\TeamRoom\\Projects\\Лето'), '');
  assert.equal(relativeToDir('C:\\TeamRoom\\Projects\\Лето2\\файл.md', 'C:\\TeamRoom\\Projects\\Лето'), '',
    'похожее имя папки не считается вложенностью');
  assert.equal(relativeToDir('', 'C:\\X'), '');
  assert.equal(relativeToDir('C:\\X\\a.md', ''), '');
});

test('в моих текстах нет технических слов', () => {
  const card = buildPlanCard({ plan: PLAN, roomId: 'cafe' });
  const visible = text(card);
  assert.ok(!/dispatcher|kanban|tenant|stream_id|t_[0-9a-f]{6,}/i.test(visible), visible);
});

test('PLAN_ERROR_TEXT — человеческая фраза', () => {
  assert.match(PLAN_ERROR_TEXT, /Не удалось показать карту/);
  assert.ok(!/error|fetch/i.test(PLAN_ERROR_TEXT));
});
