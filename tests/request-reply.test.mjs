/**
 * request-reply.test.mjs — «Написать руководителю / дать указание» и кнопки
 * заблокированной карточки (W29-P2).
 *
 * Что проверяется:
 *  1) список кнопок заблокированной карточки совпадает с серверным
 *     (`stale.BLOCKED_ACTIONS`) и каждое действие есть в `ACTION_ALLOWED` —
 *     новых маршрутов нет, интерфейс не шлёт того, чего сервер не знает;
 *  2) тело каждого действия — по контракту `POST /api/requests/{id}/actions`;
 *  3) нижнее поле шлёт комментарий, а при блокировке ещё и снятие паузы (два
 *     действия одного нажатия), пустой текст не уходит;
 *  4) системная пауза сторожа не считается «ждёт вашего решения»: `blockedCards`
 *     её пропускает, `systemPauses` отдаёт отдельно (правило «число = список»).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');

const {
  blockedActionList, blockedActionBody, replyBody, replyPlan, roomModels, roomPeople,
  blockedCardActions,
} = await import('../web/js/views/request-reply.js');
const { blockedCards, systemPauses, isSystemPause, inboxSummary } =
  await import('../web/js/views/backlog-model.js');

const RULES = readFileSync(join(ROOT, 'server', 'app_common.py'), 'utf8')
  .replace(/^\s*#[^\n]*$/gm, '');
const STALE = readFileSync(join(ROOT, 'server', 'stale.py'), 'utf8');

test('кнопки карточки совпадают с серверным списком и ACTION_ALLOWED', () => {
  const ids = blockedActionList().map((a) => a.id);
  assert.deepEqual(ids, ['resume', 'instruct', 'model', 'reassign', 'dismiss']);
  // server/stale.py: BLOCKED_ACTIONS — тот же набор
  const serverList = /BLOCKED_ACTIONS = \(([^)]*)\)/.exec(STALE)[1]
    .split(',').map((s) => s.trim().replace(/"/g, '')).filter(Boolean);
  assert.deepEqual(ids, serverList);
  // каждое серверное действие есть в ACTION_ALLOWED (иначе 400 «Неизвестное действие»)
  const allowed = /ACTION_ALLOWED = \(([\s\S]*?)\)\n/.exec(RULES)[1];
  for (const name of ['unblock', 'comment', 'card_model', 'reassign', 'archive']) {
    assert.ok(allowed.includes(`"${name}"`), `нет действия ${name} в ACTION_ALLOWED`);
  }
});

test('тело действия — по контракту действий запроса', () => {
  assert.deepEqual(blockedActionBody('resume', { taskId: 't_1' }),
    { action: 'unblock', task_id: 't_1' });
  assert.deepEqual(blockedActionBody('model', { taskId: 't_1', model: 'a/b' }),
    { action: 'card_model', task_id: 't_1', model: 'a/b' });
  assert.deepEqual(blockedActionBody('reassign', { taskId: 't_1', profile: 'team-ops' }),
    { action: 'reassign', task_id: 't_1', profile: 'team-ops' });
  assert.deepEqual(blockedActionBody('dismiss', { taskId: 't_1' }),
    { action: 'archive', task_id: 't_1' });
  assert.deepEqual(blockedActionBody('instruct', { taskId: 't_1', text: 'делай так' }),
    { action: 'comment', task_id: 't_1', text: 'делай так' });
});

test('нижнее поле: комментарий, а при блокировке — ещё и снятие паузы', () => {
  assert.deepEqual(replyBody('t_9', { text: 'проверь цифры' }),
    { request_id: 't_9', action: 'comment', text: 'проверь цифры' });
  const plan = replyPlan('t_9', { text: 'делай по варианту 2', taskIds: ['t_a', 't_b'] });
  assert.equal(plan.length, 3);
  assert.equal(plan[0].body.action, 'comment');
  assert.deepEqual(plan.slice(1).map((p) => p.body),
    [{ action: 'unblock', task_id: 't_a', text: 'делай по варианту 2' },
      { action: 'unblock', task_id: 't_b', text: 'делай по варианту 2' }]);
  assert.ok(plan.every((p) => p.path === '/api/requests/t_9/actions'), 'путь действий один');
  // без блокировок — только комментарий
  assert.equal(replyPlan('t_9', { text: 'спасибо' }).length, 1);
});

test('модели и участники комнаты для форм', () => {
  const room = { members: [
    { profile: 'team-ops', model_settings: { provider: 'oblako-a', model: 'dilizhans-midi' } },
    { profile: 'team-buyer', model_settings: { provider: 'lokal', model: 'dilizhans-home' } },
    { profile: 'team-dup', model_settings: { provider: 'oblako-a', model: 'dilizhans-midi' } },
    { profile: '' },
  ] };
  assert.deepEqual(roomModels(room).map((m) => m.model), ['dilizhans-midi', 'dilizhans-home']);
  assert.deepEqual(roomPeople(room), ['team-ops', 'team-buyer', 'team-dup']);
});

test('системная пауза сторожа не считается «ждёт вашего решения»', () => {
  const details = [{ id: 't_1', title: 'Запрос', tree: [
    { id: 't_card', status: 'blocked', title: 'Сверить цены',
      actions: [{ id: 'instruct' }] },
    { id: 't_pause', status: 'blocked', title: 'Ждёт модели',
      hold: { by: 'guard', auto: true, at: 5, reason: 'модель не подтверждена' },
      actions: [{ id: 'resume' }] },
  ] }];
  assert.equal(isSystemPause(details[0].tree[1]), true);
  assert.equal(isSystemPause(details[0].tree[0]), false);
  assert.deepEqual(blockedCards(details).map((c) => c.task_id), ['t_card']);
  assert.deepEqual(blockedCards(details, { includeSystem: true }).map((c) => c.task_id),
    ['t_card', 't_pause']);
  const pauses = systemPauses(details);
  assert.equal(pauses.length, 1);
  assert.equal(pauses[0].task_id, 't_pause');
  assert.match(pauses[0].label, /Снимаю автоматически/);
  // число и список — один источник: сервер прислал 1 карточку — в списке 1
  const counts = { questions: 0, decisions: 0, approvals: 0, steps: 0, blocked: 1 };
  const sum = inboxSummary({ questions: [], decisions: [], approvals: [],
    blocked: blockedCards(details), counts });
  assert.equal(sum.total, 1);
  assert.equal(sum.blocked, 1);
});

test('у каждой кнопки карточки есть обработчик: нажатие шлёт действие', async () => {
  installFakeDom();
  try {
    const sent = [];
    const api = { post: async (path, body) => { sent.push({ path, body }); return { ok: true }; } };
    const node = blockedCardActions({
      card: { task_id: 't_card', request_id: 't_1', text: 'Сверить цены' },
      api, toast: () => {}, onChange: () => {},
    });
    const buttons = [];
    const walk = (n) => {
      for (const child of n.childNodes || []) {
        if (child.nodeType !== 1) continue;
        if (child.getAttribute && child.getAttribute('data-blocked-action')) buttons.push(child);
        walk(child);
      }
    };
    walk(node);
    assert.deepEqual(buttons.map((b) => b.getAttribute('data-blocked-action')),
      ['resume', 'instruct', 'model', 'reassign', 'dismiss']);
    buttons[0].dispatch('click', {});
    await new Promise((r) => setTimeout(r, 0));
    assert.deepEqual(sent[0], { path: '/api/requests/t_1/actions',
      body: { action: 'unblock', task_id: 't_card' } });
    // «Дать указание» раскрывает форму с микрофоном, а не уводит со страницы
    buttons[1].dispatch('click', {});
    const textareas = [];
    const find = (n) => {
      for (const child of n.childNodes || []) {
        if (child.nodeType !== 1) continue;
        if (String(child.localName || child.tagName).toLowerCase() === 'textarea') textareas.push(child);
        find(child);
      }
    };
    find(node);
    assert.equal(textareas.length, 1, 'нет поля указания');
    assert.equal(textareas[0].getAttribute('data-mic'), 'manual');
    assert.equal(sent.length, 1, 'раскрытие формы ничего не отправляет');
  } finally {
    uninstallFakeDom();
  }
});
