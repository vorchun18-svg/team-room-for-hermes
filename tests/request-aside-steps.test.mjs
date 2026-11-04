/**
 * request-aside-steps.test.mjs — колонка «Дальше» на странице запроса:
 * отметка шага должна менять строку сразу и оставлять галочку доступной
 * (дефект e2e S11: после отметки чекбокс оставался disabled, строка без
 * `is-done` до F5, потому что refresh({force:true}) не перерисовывал страницу —
 * деталь запроса не менялась, а шаг живёт в бэклоге комнаты).
 *
 * DOM — мини-DOM из tests/fake-dom.mjs; браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { createAside } from '../web/js/views/request-aside.js';

/* ------------------------------------------------ недостающие методы мини-DOM */

function patchDomHelpers() {
  const P = FakeElement.prototype;
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (!('childElementCount' in P)) {
    Object.defineProperty(P, 'childElementCount', {
      configurable: true,
      get() { return (this.childNodes || []).filter((n) => n.nodeType === 1).length; },
    });
  }
  if (typeof P.replaceChild !== 'function') {
    P.replaceChild = function replaceChild(node, old) {
      const i = this.childNodes.indexOf(old);
      if (i === -1) return old;
      node.parentNode = this;
      old.parentNode = null;
      this.childNodes[i] = node;
      return old;
    };
  }
  if (typeof P.closest !== 'function') {
    P.closest = function closest(selector) {
      const cls = String(selector).replace(/^\./, '');
      let cur = this;
      while (cur && cur.nodeType === 1) {
        const list = String(cur.getAttribute('class') || '').split(/\s+/);
        if (list.includes(cls)) return cur;
        cur = cur.parentNode;
      }
      return null;
    };
  }
  const findClass = (root, cls) => {
    const out = [];
    const walk = (n) => {
      if (!n) return;
      if (n.nodeType === 1 && String(n.getAttribute('class') || '').split(/\s+/).includes(cls)) out.push(n);
      for (const c of n.childNodes || []) walk(c);
    };
    walk(root);
    return out;
  };
  if (typeof P.querySelector !== 'function') {
    P.querySelector = function querySelector(selector) {
      const found = findClass(this, String(selector).replace(/^\./, ''));
      return found.length ? found[0] : null;
    };
  }
  P.byClass = function byClass(cls) { return findClass(this, cls); };
}

function byClass(root, cls) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && String(n.getAttribute('class') || '').split(/\s+/).includes(cls)) out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

const DETAIL = {
  id: 't_root1',
  next_steps: [{ id: 'ns1', text: 'Собрать котировки у поставщиков', state: 'open' }],
  open_questions: [],
  decisions: [],
  future_questions: [],
};

/** Стенд: страница запроса без перерисовки (как в браузере при неизменной детали). */
function setup(opts = {}) {
  installFakeDom();
  patchDomHelpers();
  const posted = [];
  const toasts = [];
  const backlog = new Map();
  const detail = JSON.parse(JSON.stringify(DETAIL));
  const state = { refresh: 0, fail: !!opts.fail };

  const ctx = {
    api: {
      async post(path, body) {
        posted.push({ path, body });
        if (state.fail) throw new Error('нет связи');
        return {};
      },
      async get() { return {}; },
    },
    toast: (message, kind) => toasts.push({ message, kind }),
    room: { id: 'cafe', members: [] },
    navigate() {},
  };

  const R = {
    ctx,
    rid: 't_root1',
    keepValues: new Map(),
    roomId: 'cafe',
    get detail() { return detail; },
    get backlog() { return backlog; },
    // Важно: refresh НЕ вызывает render() — деталь запроса не изменилась.
    async refresh() { state.refresh += 1; },
  };

  const { buildAside } = createAside(R);
  const host = globalThis.document.createElement('div');
  host.appendChild(buildAside());
  return { host, posted, toasts, backlog, detail, state };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test('отметка шага: строка сразу «is-done», галочка доступна, бэклог обновлён', async () => {
  const s = setup();
  try {
    const row = byClass(s.host, 'req-step')[0];
    assert.ok(row, 'строка шага отрисована');
    assert.doesNotMatch(String(row.getAttribute('class')), /is-done/);

    const cb = byClass(row, 'req-step__check')[0];
    cb.checked = true;
    cb.dispatch('change');
    await tick();
    await tick();

    assert.deepEqual(s.posted.map((p) => p.body), [{ status: 'done' }], 'ушёл status=done');
    assert.match(String(s.posted[0].path), /\/api\/rooms\/cafe\/backlog\/t_root1(%3A|:)ns1$/);

    const fresh = byClass(s.host, 'req-step')[0];
    assert.match(String(fresh.getAttribute('class')), /is-done/, 'строка помечена выполненной без F5');
    const cb2 = byClass(fresh, 'req-step__check')[0];
    assert.ok(!cb2.disabled, 'галочка снова доступна');
    assert.ok(cb2.hasAttribute('checked'), 'галочка стоит');
    assert.equal(s.backlog.get('t_root1:ns1').status, 'done', 'бэклог в памяти обновлён');
    assert.equal(s.state.refresh, 1, 'страница всё равно попробовала обновиться');
    // Ссылка «в календарь» у выполненного шага убирается.
    assert.equal(byClass(fresh, 'req-step__ics').length, 0);
  } finally {
    uninstallFakeDom();
  }
});

test('повторный клик возвращает шаг в работу (без ошибки)', async () => {
  const s = setup();
  try {
    let cb = byClass(s.host, 'req-step__check')[0];
    cb.checked = true;
    cb.dispatch('change');
    await tick();
    await tick();
    assert.equal(s.detail.next_steps[0].state, 'done', 'локальная отметка сохранена');

    cb = byClass(s.host, 'req-step__check')[0];
    cb.checked = false;
    cb.dispatch('change');
    await tick();
    await tick();

    assert.deepEqual(s.posted.map((p) => p.body), [{ status: 'done' }, { status: 'open' }]);
    const row = byClass(s.host, 'req-step')[0];
    assert.doesNotMatch(String(row.getAttribute('class')), /is-done/, 'строка снова в работе');
    const cb3 = byClass(row, 'req-step__check')[0];
    assert.ok(!cb3.disabled);
    assert.ok(!cb3.hasAttribute('checked'));
    assert.ok(byClass(row, 'req-step__ics').length, 'ссылка «в календарь» вернулась');
  } finally {
    uninstallFakeDom();
  }
});

test('ошибка сохранения: галочка и строка возвращаются, чекбокс доступен', async () => {
  const s = setup({ fail: true });
  try {
    const cb = byClass(s.host, 'req-step__check')[0];
    cb.checked = true;
    cb.dispatch('change');
    await tick();
    await tick();

    const row = byClass(s.host, 'req-step')[0];
    assert.doesNotMatch(String(row.getAttribute('class')), /is-done/, 'выполненным не помечаем');
    const cb2 = byClass(s.host, 'req-step__check')[0];
    assert.ok(!cb2.disabled, 'чекбокс разблокирован');
    assert.equal(cb2.checked, false, 'галочка снята');
    assert.equal(s.backlog.size, 0, 'в бэклоге ничего не записано');
    const last = s.toasts[s.toasts.length - 1];
    assert.equal(last.kind, 'danger');
    assert.match(last.message, /Не удалось сохранить отметку/);
  } finally {
    uninstallFakeDom();
  }
});

test('срок шага: сохраняется вместе со статусом, строка перерисовывается', async () => {
  const s = setup();
  try {
    const date = byClass(s.host, 'req-step__date')[0];
    date.value = '2026-11-20';
    date.dispatch('change');
    await tick();
    await tick();

    assert.deepEqual(s.posted.map((p) => p.body), [{ status: 'open', due: '2026-11-20' }]);
    const row = byClass(s.host, 'req-step')[0];
    assert.equal(byClass(row, 'req-step__date')[0].getAttribute('value'), '2026-11-20');
    assert.equal(s.toasts[0].message, 'Срок сохранён');
    assert.match(text(row), /20 ноя/);
  } finally {
    uninstallFakeDom();
  }
});
