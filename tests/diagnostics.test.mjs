/**
 * diagnostics.test.mjs — экран «Проверка системы» (WAVE 12 §12.2).
 *
 * Что проверяем:
 *  • заголовок, сводка и по одному пункту на каждую проверку сервера;
 *  • проблемы идут первыми, «в порядке» — в конце;
 *  • «Обновить» перезапрашивает данные;
 *  • «Скопировать отчёт для поддержки» кладёт в буфер версию, ОС и статусы —
 *    без личных путей (страховка поверх сервера);
 *  • «Открыть документацию» подтягивает docs/INSTALL.md текстом в приложении.
 *
 * DOM — мини-DOM из tests/fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, installFakeDom, uninstallFakeDom } from './fake-dom.mjs';

/* ─────────────────────────────────────────────────────────── расширения DOM */

if (typeof FakeElement.prototype.replaceChildren !== 'function') {
  FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
    this.childNodes = [];
    for (const node of nodes) if (node) this.appendChild(node);
  };
}
if (typeof FakeElement.prototype.append !== 'function') {
  FakeElement.prototype.append = function append(...nodes) {
    for (const node of nodes) if (node) this.appendChild(node);
  };
}
if (typeof FakeElement.prototype.remove !== 'function') {
  FakeElement.prototype.remove = function remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  };
}
if (typeof FakeElement.prototype.scrollIntoView !== 'function') {
  FakeElement.prototype.scrollIntoView = function scrollIntoView() {};
}
if (typeof FakeElement.prototype.removeChild !== 'function') {
  FakeElement.prototype.removeChild = function removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
    return node;
  };
}

const { default: mountView, buildSupportText, summarize, sortChecks, statusLabel } =
  await import('../web/js/views/diagnostics.js');

/* ──────────────────────────────────────────────────────────────── данные */

const DATA = {
  ok: false,
  generated_at: 1762180000,
  app: { name: 'Комната команды', version: '1.0.0', os: 'Windows 11', python: '3.14.9', sandbox: false },
  summary: { ok: 2, warn: 1, fail: 1, off: 1, total: 5 },
  checks: [
    { id: 'version', title: 'Версия приложения', status: 'ok', value: '1.0.0',
      hint: 'Единый номер версии — файл VERSION в корне программы.', fix: null, details: [] },
    { id: 'hermes_cli', title: 'Программа hermes', status: 'warn', value: 'не найдена',
      hint: 'Без неё комната только показывает доску.',
      fix: { kind: 'doc', label: 'Открыть документацию', value: 'docs/INSTALL.md#cli' }, details: [] },
    { id: 'board', title: 'Доска задач', status: 'fail', value: 'файл доски не найден',
      hint: 'Доски «team» нет: список задач будет пустым.',
      fix: { kind: 'doc', label: 'Открыть документацию', value: 'docs/INSTALL.md' }, details: [] },
    { id: 'dictation', title: 'Диктовка', status: 'off', value: 'не настроена',
      hint: 'Нет ни локального распознавания, ни провайдера в Hermes.',
      fix: { kind: 'doc', label: 'Как включить', value: 'docs/INSTALL.md#dictation' }, details: [] },
    { id: 'plugin', title: 'Плагин в Hermes Desktop', status: 'ok', value: 'установлен · версия 1.0.0',
      hint: 'Открывается из левого меню Hermes.',
      fix: { kind: 'command', label: 'Скопировать команду', value: 'python tools/install_desktop_plugin.py' },
      details: [] },
  ],
};

function walk(el, out = []) {
  for (const child of el.childNodes || []) {
    if (child.nodeType === 1) { out.push(child); walk(child, out); }
  }
  return out;
}

function byClass(root, cls) {
  return walk(root).filter((el) => String(el.getAttribute('class') || '').split(/\s+/).includes(cls));
}

function byText(root, text) {
  return walk(root).filter((el) => el.textContent.includes(text));
}

function makeCtx(overrides = {}) {
  const calls = { get: [], text: [] };
  const ctx = {
    api: {
      async get(path) { calls.get.push(path); return DATA; },
      async text(path) { calls.text.push(path); return '# Установка\n\nТекст документации.'; },
    },
    store: {},
    route: { name: 'settings', params: {} },
    toast() {},
    ...overrides,
  };
  return { ctx, calls };
}

/* ───────────────────────────────────────────────────────────────── тесты */

test('сводка и сортировка: проблемы первыми, «в порядке» — в конце', () => {
  assert.equal(summarize(DATA.checks).fail, 1);
  assert.equal(summarize(DATA.checks).total, 5);
  assert.equal(summarize([]).total, 0);
  assert.equal(statusLabel('fail'), 'не работает');
  assert.equal(statusLabel('что-то'), 'обратите внимание');

  const order = sortChecks(DATA.checks).map((item) => item.status);
  assert.equal(order[0], 'fail');
  assert.equal(order[order.length - 1], 'ok');
});

test('отчёт для поддержки: версия, ОС, статусы — без личных путей', () => {
  const text = buildSupportText({
    ...DATA,
    checks: DATA.checks.concat([{
      id: 'hermes_home', title: 'Hermes Desktop', status: 'ok',
      value: '~/AppData/Local/hermes', hint: 'читается только на чтение', fix: null, details: [],
    }]),
  });
  assert.match(text, /Версия: 1\.0\.0/);
  assert.match(text, /Система: Windows 11 · Python 3\.14\.9/);
  assert.match(text, /Итого: в порядке 3, требуют внимания 1, не работает 1, не настроено 1/);
  assert.match(text, /Доска задач: не работает/);
  assert.ok(!text.includes('~'), 'путь в ~-стиле не должен попадать в отчёт');
  assert.ok(!text.includes('C:\\'), 'полный путь не должен попадать в отчёт');
  assert.ok(!text.includes('User'), 'имя пользователя не должно попадать в отчёт');
});

test('экран: заголовок, сводка, пункты и действия', async () => {
  installFakeDom();
  try {
    const host = new FakeElement('div');
    const { ctx, calls } = makeCtx();
    const view = await mountView(host, ctx);

    const all = walk(host);
    assert.ok(all.some((el) => el.tagName === 'h1' && el.textContent === 'Проверка системы'),
      'нет заголовка «Проверка системы»');
    assert.ok(byText(host, 'Скопировать отчёт для поддержки').length > 0, 'нет кнопки отчёта');
    assert.ok(byText(host, 'Обновить').length > 0, 'нет кнопки «Обновить»');

    const chips = byClass(host, 'diag__count');
    assert.equal(chips.length, 4, 'сводка должна показывать четыре статуса');
    assert.ok(chips.some((chip) => chip.getAttribute('data-status') === 'fail' && chip.textContent.includes('1')));
    assert.ok(byText(host, 'не работает').length > 0);

    const cards = byClass(host, 'diag-item');
    assert.equal(cards.length, 5, 'по карточке на каждый пункт проверки');
    assert.equal(cards[0].getAttribute('data-status'), 'fail', 'проблема должна быть первой');
    assert.equal(cards[0].getAttribute('data-id'), 'board');
    assert.ok(cards.every((card) => card.textContent.length > 10), 'в карточке есть текст');
    assert.ok(byText(host, 'Версия приложения').length > 0);
    assert.ok(byText(host, 'Диктовка').length > 0);

    // Действия: «Открыть документацию» есть, «Скопировать команду» — тоже.
    assert.ok(byText(host, 'Открыть документацию').length > 0);
    assert.ok(byText(host, 'Скопировать команду').length > 0);

    // «Обновить» перезапрашивает данные.
    const before = calls.get.length;
    const refresh = byClass(host, 'btn').find((btn) => btn.textContent.includes('Обновить'));
    refresh.dispatch('click', { type: 'click', target: refresh });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(calls.get.length, before + 1, '«Обновить» должен перезапрашивать /api/diagnostics');
    assert.equal(calls.get[0], '/api/diagnostics');

    view.unmount();
    assert.equal(host.childNodes.length, 0, 'после ухода экран убирает себя');
  } finally {
    uninstallFakeDom();
  }
});

test('копирование отчёта кладёт в буфер версию, ОС и статусы', async () => {
  installFakeDom();
  const written = [];
  const navDesc = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  Object.defineProperty(globalThis, 'navigator', {
    value: { clipboard: { writeText: async (text) => { written.push(String(text)); } } },
    configurable: true, writable: true,
  });
  try {
    const host = new FakeElement('div');
    const { ctx } = makeCtx();
    await mountView(host, ctx);

    const copy = byClass(host, 'btn').find((btn) => btn.textContent.includes('Скопировать отчёт'));
    assert.ok(copy, 'нет кнопки «Скопировать отчёт для поддержки»');
    copy.dispatch('click', { type: 'click', target: copy });
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(written.length, 1, 'отчёт должен попасть в буфер обмена');
    assert.match(written[0], /Версия: 1\.0\.0/);
    assert.match(written[0], /Windows 11/);
    assert.match(written[0], /Доска задач: не работает/);
    assert.ok(!written[0].includes('C:\\'), 'в отчёте не должно быть полных путей');
  } finally {
    if (navDesc) Object.defineProperty(globalThis, 'navigator', navDesc);
    else delete globalThis.navigator;
    uninstallFakeDom();
  }
});

test('«Открыть документацию» показывает docs/INSTALL.md текстом', async () => {
  installFakeDom();
  try {
    const host = new FakeElement('div');
    const { ctx, calls } = makeCtx();
    await mountView(host, ctx);

    const docButton = byClass(host, 'btn').find((btn) => btn.textContent.includes('Открыть документацию'));
    assert.ok(docButton, 'нет кнопки «Открыть документацию»');
    docButton.dispatch('click', { type: 'click', target: docButton });
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(calls.text[0], '/api/docs?name=INSTALL.md');
    const doc = walk(host).find((el) => String(el.getAttribute('class') || '').includes('diag-doc__body'));
    assert.ok(doc, 'нет блока документации');
    assert.ok(doc.textContent.includes('Установка') || doc.textContent.includes('Загружаю'),
      'документация должна показаться текстом: ' + doc.textContent.slice(0, 80));
  } finally {
    uninstallFakeDom();
  }
});
