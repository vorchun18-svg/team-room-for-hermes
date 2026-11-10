import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';
import {
  APP_TITLE, DEMO_TEXT, demoBadge, demoTabTitle, isMockHealth,
} from '../web/js/shell-sidebar.js';

function withDom(fn) {
  installFakeDom();
  try { return fn(); } finally { uninstallFakeDom(); }
}

test('isMockHealth: только явный engine.mock === true', () => {
  assert.equal(isMockHealth({ engine: { mock: true } }), true);
  assert.equal(isMockHealth({ engine: { mock: 'true' } }), false);
  assert.equal(isMockHealth({ engine: { ok: true } }), false);
  assert.equal(isMockHealth({ app: { name: 'team-room' } }), false);
  assert.equal(isMockHealth(null), false);
});

test('demoTabTitle: «ДЕМО ·» с сохранением счётчика и без дубля', () => {
  assert.equal(demoTabTitle(APP_TITLE), 'ДЕМО · ' + APP_TITLE);
  assert.equal(demoTabTitle('(3) ' + APP_TITLE), '(3) ДЕМО · ' + APP_TITLE);
  assert.equal(demoTabTitle('ДЕМО · ' + APP_TITLE), 'ДЕМО · ' + APP_TITLE);
  assert.equal(demoTabTitle('(2) ДЕМО · ' + APP_TITLE), '(2) ДЕМО · ' + APP_TITLE);
  assert.equal(demoTabTitle(''), 'ДЕМО · ' + APP_TITLE);
});

test('demoBadge: плашка с точным текстом только для мока', () => withDom(() => {
  const badge = demoBadge({ engine: { mock: true } });
  assert.ok(badge, 'плашка должна быть');
  assert.equal(badge.getAttribute('role'), 'status');
  assert.match(badge.className, /demo-badge/);
  assert.equal(badge.dataset.demo, '1');
  assert.equal(badge.find('span').textContent, DEMO_TEXT);
  assert.equal(badge.getAttribute('title'), DEMO_TEXT);
  assert.equal(demoBadge({ engine: { ok: true } }), null);
  assert.equal(demoBadge(null), null);
}));
