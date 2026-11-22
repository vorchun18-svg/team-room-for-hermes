/**
 * agent-model-catalog.test.mjs — плашка «Список моделей неполный» (W29-P1, дефект 11).
 *
 * Правило пакета: запасной каталог («из профилей») больше НЕ прячется. В диалоге модели
 * видно, что Hermes не ответил и почему, рядом — рабочая кнопка «Обновить», которая
 * запрашивает список заново (`GET /api/models?refresh=1`).
 *
 *   node --test tests/agent-model-catalog.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';
import { mountAgentModel, catalogNotice } from '../web/js/agent-model-ui.js';

FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
  this.childNodes = [];
  for (const node of nodes) if (node) this.appendChild(node);
};
FakeElement.prototype.remove = function remove() {
  if (this.parentNode) {
    const idx = this.parentNode.childNodes.indexOf(this);
    if (idx !== -1) this.parentNode.childNodes.splice(idx, 1);
  }
  this.parentNode = null;
};
FakeElement.prototype.closest = function closest() { return null; };
FakeElement.prototype.focus = function focus() {};
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  doc.addEventListener = () => {};
  doc.removeEventListener = () => {};
  globalThis.document = doc;
  return doc;
}

function walk(node, pred, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    if (pred(child)) out.push(child);
    walk(child, pred, out);
  }
  return out;
}

const ROOM = {
  id: 'cafe',
  orchestrator: 'team-director',
  critic: '',
  members: [{ profile: 'team-cfo', role: 'worker' }],
};

const FULL = {
  source: 'hermes:inventory',
  providers: [
    { id: 'anthropic', label: 'Anthropic', configured: true,
      models: [{ id: 'claude-sonnet-5-5', label: 'claude-sonnet-5-5' }] },
  ],
  efforts: [{ id: 'high', label: 'Высокие' }],
  warnings: [],
};

const PARTIAL = {
  source: 'profiles',
  providers: [
    { id: 'anthropic', label: 'Anthropic', configured: true,
      models: [{ id: 'claude-sonnet-5-5', label: 'claude-sonnet-5-5' }] },
  ],
  efforts: [{ id: 'high', label: 'Высокие' }],
  warnings: ['Hermes не ответил — список собран из настроек профилей.',
             'No module named ruamel'],
};

test('полный каталог — плашки нет', () => {
  assert.equal(catalogNotice(FULL), null);
  assert.equal(catalogNotice(null), null);
  assert.equal(catalogNotice({ source: 'override', warnings: ['x'] }), null);
});

test('запасной каталог — плашка с причиной', () => {
  const info = catalogNotice(PARTIAL);
  assert.ok(info, 'плашка должна появиться');
  assert.match(info.text, /Список моделей неполный/);
  assert.match(info.text, /Hermes не ответил/);
  assert.match(info.text, /ruamel/);
});

test('в диалоге видна плашка и кнопка «Обновить» запрашивает список заново', async () => {
  installDom();
  const calls = [];
  const api = {
    get: async (path) => {
      calls.push(path);
      return path.includes('refresh') ? FULL : PARTIAL;
    },
  };
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api }, { room: ROOM, catalog: null, mode: 'draft' });
  assert.equal(calls[0], '/api/models');

  const banners = walk(host, (n) => String(n.className || '').includes('catalog-incomplete'));
  assert.equal(banners.length, 1, 'плашки нет в диалоге');
  const text = String(banners[0].textContent || '');
  assert.match(text, /Список моделей неполный/);

  const buttons = walk(host, (n) => String(n.className || '').includes('catalog-refresh'));
  assert.equal(buttons.length, 1, 'нет кнопки «Обновить»');
  assert.match(String(buttons[0].textContent || ''), /Обновить/);

  await buttons[0].dispatch('click', { type: 'click', target: buttons[0] });
  await new Promise((resolve) => setTimeout(resolve, 0));   // обработчик асинхронный
  assert.ok(calls.some((p) => p.includes('refresh=1')), calls);
  // полный список пришёл — плашка убрана, поле больше не «неполное»
  assert.equal(ui.catalog.source, 'hermes:inventory');
  const after = walk(host, (n) => String(n.className || '').includes('catalog-incomplete'));
  assert.equal(after.length, 1);
  assert.doesNotMatch(String(after[0].textContent || ''), /Список моделей неполный/,
    'после «Обновить» плашка должна исчезнуть');
});

test('без каталога (сеть) — прежняя ошибка, плашки о неполноте нет', async () => {
  installDom();
  const api = { get: async () => { throw new Error('нет сети'); } };
  const host = new FakeElement('div');
  await mountAgentModel(host, { api }, { room: ROOM, catalog: null, mode: 'draft' });
  const banners = walk(host, (n) => String(n.className || '').includes('catalog-error'));
  assert.equal(banners.length, 1, 'ошибка недоступности списка должна быть');
});
