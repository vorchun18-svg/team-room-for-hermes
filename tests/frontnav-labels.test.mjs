/**
 * frontnav-labels.test.mjs — человеческие подписи и единый счётчик дел (волна 7).
 *
 * Проверяем чистые функции: D8 (имя профиля вместо id), D3 (текст ошибки без
 * `(members)`), D4 (число дел и его расшифровка), D16 (мягкое имя комнаты), D18
 * (число с существительным).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  roleNoun, roleTitle, isMachineName, displayName, profileOptionLabel,
  countText, COUNT_FORMS, humanApiMessage, softRoomName,
} from '../web/js/shell-labels.js';
import { inboxSummary, inboxCount } from '../web/js/views/backlog-model.js';

const profiles = JSON.parse(readFileSync(new URL('../fixtures/profiles.json', import.meta.url), 'utf8')).items;

/* ---------------------------------------------------------------------- D8 */

test('машинные названия профилей распознаются: id через пробел — не имя', () => {
  assert.equal(isMachineName('biz director', 'biz-director'), true);
  assert.equal(isMachineName('default', 'default'), true);
  assert.equal(isMachineName('biz director', 'biz-ops'), true);   // без кириллицы
  assert.equal(isMachineName('Директор Лето', 'biz-director'), false);
  assert.equal(isMachineName('', 'biz-ops'), true);
});

test('displayName: человеческое название профиля, иначе роль с большой буквы', () => {
  assert.equal(displayName('biz-director', 'orchestrator', 'Директор Лето'), 'Директор Лето');
  assert.equal(displayName('biz-director', 'orchestrator', 'biz director'), 'Руководитель');
  assert.equal(displayName('biz-ops', 'worker', ''), 'Исполнитель');
  assert.equal(displayName('biz-critic', 'critic', 'Критик'), 'Критик');
  // Ни одного системного id в подписи.
  for (const p of profiles) {
    const label = displayName(p.profile, p.kind, p.title);
    assert.ok(label && !label.includes(p.profile), `${p.profile} → ${label}`);
  }
});

test('profileOptionLabel не повторяет id: «default · default» больше нет', () => {
  assert.equal(profileOptionLabel({ profile: 'default', title: 'default' }), 'default');
  assert.equal(profileOptionLabel({ profile: 'biz-ops', title: 'biz ops' }), 'biz-ops');
  assert.equal(profileOptionLabel({ profile: 'biz-ops', title: 'Операции' }), 'Операции · biz-ops');
});

test('roleTitle и roleNoun дают русскую роль комнаты', () => {
  assert.equal(roleTitle('orchestrator'), 'руководитель комнаты');
  assert.equal(roleNoun('orchestrator'), 'руководитель');
  assert.equal(roleTitle('critic'), 'критик комнаты');
  assert.equal(roleTitle('worker'), 'исполнитель');
  assert.equal(roleTitle(null), 'исполнитель');
  assert.equal(roleNoun('observer'), 'наблюдатель');
});

/* ---------------------------------------------------------------------- D3 */

test('ошибка сервера о составе читается по-русски и без (members)', () => {
  const text = humanApiMessage('В комнате должен быть хотя бы один исполнитель (members)');
  assert.ok(!/members|\(|\)/.test(text), text);
  assert.match(text, /Отметьте хотя бы одного исполнителя/);
  assert.equal(
    humanApiMessage('Нужен директор комнаты (orchestrator)'),
    'Выберите руководителя комнаты — он раздаёт работу.',
  );
  assert.equal(humanApiMessage('Комната «cafe» уже есть'), 'Комната «cafe» уже есть');
});

/* ---------------------------------------------------------------------- D4 */

test('число дел = вопросы + блокировки + решения, с расшифровкой', () => {
  const questions = new Array(7).fill({});
  const decisions = new Array(1).fill({});
  const blocked = new Array(1).fill({});
  const summary = inboxSummary({ questions, decisions, blocked });
  assert.equal(summary.total, 9);
  assert.equal(summary.questions, 7);
  assert.equal(summary.text, '7 вопросов · 1 блокировка · 1 решение');
  assert.equal(summary.full, '9 дел: 7 вопросов · 1 блокировка · 1 решение');
  // inboxCount считает то же самое — один источник правды.
  assert.equal(inboxCount({ questions, decisions, blocked }), summary.total);

  const empty = inboxSummary({});
  assert.equal(empty.total, 0);
  assert.equal(empty.text, '');
});

test('расшифровка склоняется: 1 вопрос, 2 вопроса, 11 вопросов', () => {
  const one = inboxSummary({ questions: [{}] });
  assert.equal(one.text, '1 вопрос');
  const two = inboxSummary({ questions: [{}, {}] });
  assert.equal(two.text, '2 вопроса');
  const eleven = inboxSummary({ questions: new Array(11).fill({}) });
  assert.equal(eleven.text, '11 вопросов');
});

/* --------------------------------------------------------------------- D18 */

test('число с существительным для бейджей', () => {
  assert.equal(countText(1, COUNT_FORMS.request), '1 запрос');
  assert.equal(countText(3, COUNT_FORMS.request), '3 запроса');
  assert.equal(countText(7, COUNT_FORMS.question), '7 вопросов');
  assert.equal(countText(9, COUNT_FORMS.case), '9 дел');
  assert.equal(countText(0, COUNT_FORMS.case), '0 дел');
});

/* --------------------------------------------------------------------- D16 */

test('служебные подписи комнаты — без подчёркивания, имя остаётся в списке', () => {
  assert.equal(softRoomName('Рабочее пространство'), 'Рабочее пространство');
  assert.equal(softRoomName('Лето'), 'Лето');
  assert.equal(softRoomName(null), '');
});
