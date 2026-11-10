/**
 * text-ru-fix9.test.mjs — склонения и согласование глагола (FIX9-FE).
 *
 * Поводы с экрана:
 *  • «до 1 частей работы» → «до 1 части работы» (подтверждение «Запустить всё»);
 *  • «3 части работы ждёт запуска» → «3 части работы ждут запуска» (плашка брифа).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';
import { pluralVerb, countNoun, fromForm, fromCount } from '../web/js/text-ru.js';
import { buildPlanCard } from '../web/js/views/plan-req.js';

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

test('pluralVerb: 1 ждёт, 2–4 ждут, 11 ждут, 21 ждёт', () => {
  const forms = ['ждёт', 'ждут'];
  assert.equal(pluralVerb(1, forms), 'ждёт');
  assert.equal(pluralVerb(2, forms), 'ждут');
  assert.equal(pluralVerb(3, forms), 'ждут');
  assert.equal(pluralVerb(4, forms), 'ждут');
  assert.equal(pluralVerb(5, forms), 'ждут');
  assert.equal(pluralVerb(11, forms), 'ждут');
  assert.equal(pluralVerb(21, forms), 'ждёт');
  assert.equal(pluralVerb(0, forms), 'ждут');
  assert.equal(pluralVerb('2', forms), 'ждут');
  assert.equal(pluralVerb(NaN, forms), 'ждут');
});

test('«3 части работы ждут запуска» — глагол согласован с числом', () => {
  installFakeDom();
  try {
    const three = buildPlanCard({
      plan: {
        status: 'draft',
        streams: [
          { id: 's1', title: 'A', status: 'proposed' },
          { id: 's2', title: 'B', status: 'proposed' },
          { id: 's3', title: 'C', status: 'proposed' },
        ],
      },
      onLaunchAll: () => {},
    });
    assert.match(text(three), /3 части работы ждут запуска/);
    assert.doesNotMatch(text(three), /части работы ждёт запуска/);

    const one = buildPlanCard({
      plan: { status: 'draft', streams: [{ id: 's1', title: 'A', status: 'proposed' }] },
      onLaunchAll: () => {},
    });
    assert.match(text(one), /1 часть работы ждёт запуска/);
  } finally {
    uninstallFakeDom();
  }
});

test('fromForm/fromCount и countNoun не сломаны добавлением глагола', () => {
  assert.equal(fromForm(1, ['часть работы', 'части работы', 'частей работы']), 'части работы');
  assert.equal(fromForm(4, ['часть работы', 'части работы', 'частей работы']), 'частей работы');
  assert.equal(fromCount(1, 4, ['шаг', 'шага', 'шагов']), '1 из 4 шагов');
  assert.equal(countNoun(3, ['часть работы', 'части работы', 'частей работы']), '3 части работы');
});
