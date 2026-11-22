/**
 * parity-view.test.mjs — W29B-Q2: экран «Совместимость с Hermes».
 *
 * Что проверяем:
 *  · сводка считается по фактическим статусам участников (число = число карточек);
 *  · модель подписывается с ИСТОЧНИКОМ («задано комнатой» / «из профиля») —
 *    человек видит, откуда взялась модель, а не только её имя;
 *  · средства (навыки / MCP / инструменты) не показывают «0 навыков», если есть
 *    общие, и называют MCP-серверы, а не число без смысла;
 *  · разрешения формулируются словами: «каждый запуск скрипта — вопрос», а не
 *    техническими `single_query_mode: deny`;
 *  · экран подключён к маршруту «Проверки» и к самой «Проверке» (одна точка входа);
 *  · «Сеанс в Hermes» показывается только при известном id сессии.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  summarize, modelLine, assetsLine, permissionLine, attentionCount, STATUS_LABEL,
} from '../web/js/views/parity.js';
import { sessionOf, attemptList } from '../web/js/views/request-parts-model.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(join(HERE, '..', rel), 'utf8');

const MEMBER = {
  profile: 'cafe-buyer',
  label: 'закупщик',
  role: 'исполнитель',
  status: 'fail',
  model: { provider: 'demo-cloud', model: 'demo-small', source: 'room' },
  profile_model: { provider: 'demo-cloud', model: 'demo-large', source: 'profile' },
  skills: { own: 0, shared: 12, total: 12, source: 'global' },
  mcp: { names: ['files'], count: 1, source: 'global' },
  tools: { names: ['terminal', 'files'], count: 2, source: 'profile' },
  permissions: { level: 'careful', level_label: 'Осторожный', single_query_mode: 'deny',
    allowlist: [], allowlist_count: 0, deny_count: 3 },
  diffs: [{ code: 'model_mismatch', severity: 'fail', text: 'разные модели' }],
  differences: [],
};

test('сводка = число участников, провал считается отдельно', () => {
  const summary = summarize([MEMBER, { ...MEMBER, status: 'ok' }, { ...MEMBER, status: 'warn' }]);
  assert.equal(summary.total, 3);
  assert.equal(summary.fail, 1);
  assert.equal(summary.warn, 1);
  assert.equal(summary.ok, 1);
  assert.equal(summary.off, 0);
  assert.equal(attentionCount([MEMBER, { ...MEMBER, status: 'ok' }]), 1);
});

test('непонятный статус — «не проверено», а не «совпадает»', () => {
  const summary = summarize([{ status: 'boom' }, null, {}]);
  assert.equal(summary.off, 2);
  assert.equal(summary.ok, 0);
  assert.equal(summary.total, 2);
});

test('модель подписана источником', () => {
  assert.equal(modelLine(MEMBER), 'demo-small — demo-cloud · задано комнатой');
  assert.match(modelLine({ model: { model: 'demo-large', provider: 'demo-cloud', source: 'agent' } }),
    /из профиля/);
  assert.equal(modelLine({ model: {} }), 'модель не задана');
});

test('средства: общие навыки видны, MCP назван, «нет» честное', () => {
  const line = assetsLine(MEMBER);
  assert.match(line, /навыков: 12 \(общих 12\)/);
  assert.match(line, /MCP: files/);
  assert.match(line, /наборов инструментов: 2/);
  const bare = assetsLine({ skills: { own: 0, shared: 0 }, mcp: { names: [], count: 0 }, tools: {} });
  assert.match(bare, /навыков: 0/);
  assert.match(bare, /MCP: нет/);
  assert.doesNotMatch(bare, /наборов инструментов/);
});

test('разрешения — словами, уровень комнаты назван', () => {
  assert.match(permissionLine(MEMBER), /уровень комнаты «Осторожный»/);
  assert.match(permissionLine(MEMBER), /каждый запуск скрипта — вопрос/);
  assert.match(permissionLine({ permissions: { single_query_mode: 'approve', level_label: 'Свободный' } }),
    /запуск кода без вопросов/);
  assert.match(permissionLine({ permissions: { allowlist_count: 4, level_label: 'Рабочий' } }),
    /разрешено без вопроса: 4/);
});

test('подписи статусов — словарь один', () => {
  assert.deepEqual(Object.keys(STATUS_LABEL).sort(), ['fail', 'off', 'ok', 'warn']);
  assert.equal(STATUS_LABEL.fail, 'расхождение');
});

test('«Сеанс в Hermes» только при известном id сессии', () => {
  const node = { attempts: [
    { n: 1, outcome: 'completed', session_id: '' },
    { n: 2, outcome: 'completed', session_id: 's_42' },
  ] };
  assert.equal(sessionOf(node), 's_42');
  assert.equal(sessionOf({ attempts: [{ n: 1, outcome: 'completed' }] }), '');
  assert.equal(sessionOf({}), '');
  // карта попыток доносит session_id из серверного attempts[]
  const rows = attemptList({ attempts: [{ n: 1, outcome: 'completed', session_id: 's_7' }] });
  assert.equal(rows[0].session_id, 's_7');
});

test('экран подключён к «Проверке» и к маршруту', () => {
  const router = read('web/js/router.js');
  assert.match(router, /segments\[1\] === 'parity'/);
  assert.match(router, /view: 'parity'/);
  const diag = read('web/js/views/diagnostics.js');
  assert.match(diag, /#\/settings\/parity/);
  const view = read('web/js/views/parity.js');
  assert.match(view, /export async function mount/);
  const tree = read('web/js/views/request-parts-tree.js');
  assert.match(tree, /Сеанс в Hermes/);
});
