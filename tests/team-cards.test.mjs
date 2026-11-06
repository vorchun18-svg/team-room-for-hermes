/**
 * tests/team-cards.test.mjs — компактная карточка участника (WAVE17, P2/B-17).
 *
 * Проверяем то, что легко сломать при правке вида:
 *  1) на карточке ОДИН чип модели — выбранная модель, без «ушло на» и без
 *     служебного «как у агента» (если модель не задана — «не выбрана»);
 *  2) предупреждение о расхождении берётся только из последней карточки
 *     участника (`model_sent.mismatch`), а не из выбора комнаты;
 *  3) обычная (не компактная) строка модели не изменилась: чип + «Изменить»,
 *     и «ушло на …» остаётся там, где он был нужен (регрессия).
 *
 * DOM — tests/fake-dom.mjs, браузер не нужен. Живая проверка — tools/probe_w17e_team2.py.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';

installFakeDom();
const { agentModelLine, modelSent } = await import('../web/js/agent-model-ui.js');

function walk(node, out = []) {
  if (!node) return out;
  out.push(node);
  for (const child of node.childNodes || []) walk(child, out);
  return out;
}

function textOf(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(textOf).join('');
}

function withClass(root, cls) {
  return walk(root).filter((n) => n.nodeType === 1
    && String(n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

const ROOM = {
  id: 'fn',
  orchestrator: 'default',
  members: [
    { profile: 'lawyer', role: 'worker', label: '',
      model_settings: { provider: 'oblako-b', model: 'vektor-5', reasoning: 'high', source: 'room' },
      agent_default: { provider: 'oblako-b', model: 'vektor-5', reasoning: 'medium' } },
  ],
};

test('modelSent: расхождение берётся из последней карточки участника', () => {
  assert.equal(modelSent({}), null);
  assert.equal(modelSent({ model_sent: null }), null);
  assert.equal(modelSent({ model_sent: { text: '' } }), null);
  const sent = modelSent({ model_sent: { text: 'ушло на: glm-5.3-flash', mismatch: true, confirmed: true } });
  assert.equal(sent.mismatch, true);
  assert.equal(sent.confirmed, true);
  assert.equal(sent.text, 'ушло на: glm-5.3-flash');
});

test('modelSent: «применится со следующей задачи» — ожидание, а не тревога (B-29)', () => {
  const sent = modelSent({
    model_sent: {
      text: 'ушло на: ollama-cloud · glm-5.3-flash · высокие',
      mismatch: false,
      confirmed: true,
      pending: true,
      pending_text: 'Новая модель применится со следующей задачи',
    },
  });
  assert.equal(sent.mismatch, false, 'нейтральное состояние — не предупреждение');
  assert.equal(sent.pending, true);
  assert.equal(sent.pendingText, 'Новая модель применится со следующей задачи');
  // Старое поле без pending остаётся ожиданием «нет» — обратная совместимость.
  const legacy = modelSent({ model_sent: { text: 'ушло на: qwen36', mismatch: true } });
  assert.equal(legacy.pending, false);
  assert.equal(legacy.pendingText, '');
});

test('компактный чип: ОДНА выбранная модель, без «ушло на» и «как у агента»', () => {
  const line = agentModelLine(ROOM, 'lawyer', {
    profile: 'lawyer',
    model_settings: { provider: 'oblako-b', model: 'vektor-5', reasoning: 'high', source: 'room' },
    agent_default: { provider: 'oblako-b', model: 'vektor-5', reasoning: 'medium' },
    model_sent: { text: 'другая модель', mismatch: true, confirmed: true },
  }, { compact: true });

  const chips = withClass(line, 'agent-model__line-chip');
  assert.equal(chips.length, 1, 'на лице карточки ровно один чип модели');
  const text = textOf(line);
  assert.ok(!/ушло на/i.test(text), 'нет «ушло на» у чипа');
  assert.ok(!/как у агента/i.test(text), 'нет служебного «как у агента»');
  assert.match(text, /Вектор|vektor/i, 'видно выбранную модель');
  // Кнопки «Изменить» на лице нет — она в меню «⋯».
  assert.equal(withClass(line, 'agent-model__line-btn').length, 0);
  // В подсказке — источник выбора и профиль, но пользователю это только в title.
  assert.match(chips[0].getAttribute('title') || '', /профиль: lawyer/);
});

test('компактный чип без модели: «не выбрана», а не «как у агента»', () => {
  const line = agentModelLine({ id: 'fn', members: [] }, 'default', {}, { compact: true });
  const text = textOf(line);
  assert.equal(text, 'не выбрана');
  assert.match(withClass(line, 'agent-model__line-chip')[0].getAttribute('title') || '',
    /не задана ни в комнате, ни в профиле/);
});

test('компактный чип-кнопка: клик открывает редактор модели (B-23)', () => {
  const called = [];
  const line = agentModelLine(ROOM, 'lawyer', {
    profile: 'lawyer',
    model_settings: { provider: 'oblako-b', model: 'vektor-5', reasoning: 'high', source: 'room' },
  }, { compact: true, onEdit: (profile) => called.push(profile) });

  const chips = withClass(line, 'agent-model__line-chip');
  assert.equal(chips.length, 1);
  const chip = chips[0];
  assert.equal(String(chip.tagName).toLowerCase(), 'button', 'чип — настоящая кнопка: работает Enter/Space');
  assert.ok(withClass(line, 'agent-model__line-chip--btn').length === 1);
  assert.equal(chip.getAttribute('type'), 'button');
  assert.equal(chip.getAttribute('aria-haspopup'), 'dialog');
  assert.match(chip.getAttribute('aria-label') || '', /Изменить модель/);
  assert.match(chip.getAttribute('title') || '', /нажмите, чтобы изменить/);
  chip.dispatch('click');
  assert.deepEqual(called, ['lawyer'], 'клик по чипу зовёт редактор модели этого участника');
  // Текст прежний: один чип выбранной модели, без «модель:».
  assert.match(textOf(line), /Вектор|vektor/i);
  assert.ok(!/^модель:/.test(textOf(line)));
});

test('компактный чип без обработчика остаётся пассивным (регрессия)', () => {
  const line = agentModelLine(ROOM, 'lawyer', {}, { compact: true });
  const chip = withClass(line, 'agent-model__line-chip')[0];
  assert.equal(String(chip.tagName).toLowerCase(), 'span');
});

test('регрессия: обычная строка модели по-прежнему с «Изменить» и «ушло на»', () => {
  const line = agentModelLine(ROOM, 'lawyer', {
    profile: 'lawyer',
    model_settings: { provider: 'oblako-b', model: 'vektor-5', reasoning: 'high', source: 'room' },
    model_sent: { text: 'glm-5.3-flash', mismatch: true, confirmed: true },
  }, {});
  assert.equal(withClass(line, 'agent-model__line-btn').length, 1, 'кнопка «Изменить» на месте');
  const text = textOf(line);
  assert.match(text, /модель: /);
  assert.match(text, /glm-5.3-flash/);
  assert.match(text, /расходится с выбором комнаты/);
});

test('cleanup', () => {
  uninstallFakeDom();
});
