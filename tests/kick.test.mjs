/**
 * kick.test.mjs — «Проверить доску сейчас» (WAVE13 §8, пакет F).
 *
 * Проверяем то, что видит Михаил:
 *   * одно название и одна подсказка (`KICK_LABEL`/`KICK_HINT`);
 *   * итог — панель с подробностями: «Запущено 2 исполнителя: …», «Запускать
 *     нечего: 1 ждёт вашего ответа, 3 уже идут»;
 *   * кнопка показывается только когда есть готовые к запуску или зависшие
 *     карточки; иначе — объяснение, почему она не нужна;
 *   * старый ответ сервера (без `report`) и ошибка прохода тоже дают итог.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom } from './fake-dom.mjs';

installFakeDom(); // h() строит узлы только при наличии document

const {
  KICK_LABEL, KICK_HINT, kickStateFromTree, kickUsefulness, kickWhyNot,
  kickReport, kickError, kickText, kickPanel, kickNow,
} = await import('../web/js/views/kick-panel.js');

const NOW = 1793912795000; // мс — как на боевой доске 05.11

const collect = (root, pred) => {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (pred(child)) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
};
const textOf = (el) => String(el ? el.textContent : '');
const byKick = (root, part) => collect(root, (el) => el.getAttribute('data-kick') === part);

const node = (id, status, extra = {}) => ({ id, title: 'Работа ' + id, status, assignee: 'biz-ops', ...extra });

/* ─────────────────────────────────────────── название и подсказка */

test('название и подсказка — одни и те же слова везде', () => {
  assert.equal(KICK_LABEL, 'Проверить доску сейчас');
  assert.equal(KICK_HINT, 'Не ждать минутного таймера — запустить готовые задачи');
  assert.doesNotMatch(KICK_LABEL, /подтолкнуть|протолкнуть/i);
});

/* ─────────────────────────────────────────── состояние по дереву */

test('дерево: готовые, идущие, ждут ответа и других задач считаются раздельно', () => {
  const state = kickStateFromTree([
    node('t_1', 'ready'),
    node('t_2', 'running', { started_at: NOW / 1000 - 60 }),
    node('t_3', 'blocked', { needs_human: true }),
    node('t_4', 'blocked'),
    node('t_5', 'todo'),
    node('t_6', 'done'),
    node('t_7', 'review'),
  ], NOW);
  assert.equal(state.counts.ready, 1);
  assert.equal(state.counts.running, 1);
  assert.equal(state.counts.input, 1);
  assert.equal(state.counts.blocked, 1);
  assert.equal(state.counts.deps, 1);
  assert.equal(state.counts.review, 1);
  assert.equal(state.useful, true);
});

test('дерево: работа идёт дольше 45 минут — считаем зависшей', () => {
  const state = kickStateFromTree([
    node('t_1', 'running', { started_at: NOW / 1000 - 46 * 60 }),
  ], NOW);
  assert.equal(state.counts.stuck, 1);
  assert.equal(state.counts.running, 0);
  assert.equal(state.useful, true, 'зависшую карточку проход вернёт в работу');
});

test('дерево: пока ждём ответа или всё готово — кнопка не нужна', () => {
  const waiting = kickStateFromTree([
    node('t_1', 'blocked', { needs_human: true }),
    node('t_2', 'running', { started_at: NOW / 1000 - 30 }),
    node('t_3', 'done'),
  ], NOW);
  assert.equal(kickUsefulness(waiting).useful, false);
  const why = kickWhyNot(waiting);
  assert.match(why, /не нужно/i);
  assert.match(why, /1 ждёт вашего ответа/);
  assert.match(why, /1 уже идёт/);
  assert.doesNotMatch(why, /Запущено/);

  const allDone = kickStateFromTree([node('t_1', 'done'), node('t_2', 'archived')], NOW);
  assert.equal(kickUsefulness(allDone).useful, false);
  assert.match(kickWhyNot(allDone), /запускать нечего — всё готово/i);
});

/* ─────────────────────────────────────────── итог прохода */

test('итог «запущено»: число, имена и подробности', () => {
  const report = {
    kind: 'started', spawned: 2, reclaimed: 1, promoted: 0,
    started: [
      { id: 't_1', assignee: 'biz-lawyer', title: 'Юр-проверка ИП' },
      { id: 't_2', assignee: 'biz-ops', title: 'Смета упаковки' },
    ],
    counts: { ready: 0, stuck: 0, running: 3, input: 1, blocked: 0, deps: 2 },
  };
  const text = kickText(report, { labelOf: (p) => (p === 'biz-lawyer' ? 'Юрист' : 'Операции') });
  assert.equal(text.title, 'Запущено 2 исполнителя');
  assert.equal(text.tone, 'ok');
  assert.match(text.lines[0], /Юрист — Юр-проверка ИП/);
  assert.match(text.lines[0], /Операции — Смета упаковки/);
  assert.ok(text.lines.some((line) => /Возвращено зависших: 1/.test(line)));
  assert.ok(text.lines.some((line) => /3 уже идут/.test(line)), text.lines.join(' | '));
});

test('итог «один исполнитель» — единственное число', () => {
  const text = kickText({ kind: 'started', spawned: 1,
    started: [{ id: 't_1', assignee: 'biz-ops', title: 'Смета' }], counts: {} });
  assert.equal(text.title, 'Запущен 1 исполнитель');
});

test('итог «запускать нечего» объясняет, кто чего ждёт', () => {
  const text = kickText({ kind: 'nothing', spawned: 0, reclaimed: 0, promoted: 0,
    counts: { ready: 0, stuck: 0, running: 3, review: 1, input: 2, blocked: 0, deps: 1 } });
  assert.equal(text.title, 'Запускать нечего');
  assert.equal(text.tone, 'info');
  const all = text.lines.join(' ');
  assert.match(all, /4 уже идут/);
  assert.match(all, /2 ждут вашего ответа/);
  assert.match(all, /1 ждёт других задач/);
});

test('итог «вернули зависших» и «переведено из ожидания»', () => {
  const back = kickText({ kind: 'reclaimed', spawned: 0, reclaimed: 2, counts: {} });
  assert.equal(back.title, 'Возвращено зависших: 2');
  assert.match(back.lines[0], /вернутся в очередь/);

  const promoted = kickText({ kind: 'promoted', spawned: 0, reclaimed: 0, promoted: 3, counts: {} });
  assert.equal(promoted.title, 'Готовы к запуску: 3');
  assert.match(promoted.lines[0], /в течение минуты/);
});

test('ошибка прохода — понятный текст, без «движок подтолкнут»', () => {
  const report = kickError({ message: 'Диспетчер уже запускали 5 с назад — не чаще раза в 30 секунд.' });
  const text = kickText(report);
  assert.equal(text.title, 'Не получилось проверить доску');
  assert.match(text.lines[0], /не чаще раза в 30 секунд/);
  assert.equal(text.tone, 'warn');
});

test('итог по запросу: запуски из других запросов помечены', () => {
  const report = { kind: 'started', spawned: 1,
    started: [{ id: 't_other', assignee: 'biz-ops', title: 'Чужая работа' }], counts: {} };
  const text = kickText(report, { scopeIds: new Set(['t_mine']) });
  assert.match(text.lines.join(' '), /В этом запросе новых запусков нет/);
});

test('старый ответ сервера (без report) тоже превращается в итог', () => {
  const report = kickReport({ spawned: 1, reclaimed: 0, promoted: 0,
    planned: [{ id: 't_1', assignee: 'biz-ops' }] });
  assert.equal(report.kind, 'started');
  assert.equal(report.legacy, true);
  assert.equal(report.started[0].id, 't_1');
  const empty = kickReport({ spawned: 0, reclaimed: 0, promoted: 0 });
  assert.equal(empty.kind, 'nothing');
});

test('панель: заголовок, строки и кнопка «Скрыть»', () => {
  let closed = 0;
  const el = kickPanel({ kind: 'started', spawned: 1,
    started: [{ id: 't_1', assignee: 'biz-ops', title: 'Смета упаковки' }], counts: {} },
  { labelOf: () => 'Операции', onClose: () => { closed += 1; } });
  assert.equal(el.getAttribute('data-kick'), 'result');
  assert.equal(textOf(byKick(el, 'title')[0]), 'Запущен 1 исполнитель');
  assert.ok(byKick(el, 'line').length >= 1);
  const close = byKick(el, 'close')[0];
  assert.ok(close, 'у панели есть «Скрыть»');
  close.dispatch('click', { type: 'click', target: close });
  assert.equal(closed, 1);
});

test('панель — заметный блок, а не одна строка тоста', () => {
  const el = kickPanel({ kind: 'nothing', counts: { input: 1, running: 2 } });
  assert.match(el.getAttribute('class'), /banner/);
  assert.equal(el.getAttribute('role'), 'status');
  assert.equal(el.getAttribute('aria-live'), 'polite');
  assert.ok(byKick(el, 'line').length >= 1);
});

/* ─────────────────────────────────────────── запросы к API */

test('kickNow: страница запроса идёт через действия запроса', async () => {
  const calls = [];
  const api = { post: async (path, body) => { calls.push({ path, body }); return { report: { kind: 'nothing', counts: {} } }; } };
  const report = await kickNow(api, 't_1b0aa253');
  assert.equal(calls[0].path, '/api/requests/t_1b0aa253/actions');
  assert.equal(calls[0].body.action, 'dispatch');
  assert.equal(report.kind, 'nothing');

  const second = [];
  const api2 = { post: async (path, body) => { second.push({ path, body }); return { ok: true, spawned: 0, reclaimed: 0, promoted: 0 }; } };
  const plain = await kickNow(api2, '');
  assert.equal(second[0].path, '/api/dispatcher/kick');
  assert.equal(plain.kind, 'nothing');
});

test('kickNow: ошибка сервера не бросает, а даёт итог', async () => {
  const api = { post: async () => { throw new Error('Диспетчер уже запускали 3 с назад'); } };
  const report = await kickNow(api, '');
  assert.equal(report.kind, 'error');
  assert.match(kickText(report).lines[0], /уже запускали/);
});
