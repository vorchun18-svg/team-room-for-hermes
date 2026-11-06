/**
 * tree-state.test.mjs — дерево работ: раскрытость, закрываемая плашка ошибки и кнопка
 * повтора (WAVE19, пакет T: B-19, B-24, B-25).
 *
 * Тесты «красные» на коде 1.0.3:
 *   * раскрытость жила только в DOM — после перерисовки проблемный узел снова раскрывался,
 *     а свёрнутый человеком — раскрывался обратно (B-19);
 *   * плашка ошибки не имела кнопки закрытия и списка попыток (B-24);
 *   * у кнопки «Повторить» не было состояний «отправлено/принято» и ключа идемпотентности (B-25).
 *
 * DOM — мини-DOM из tests/fake-dom.mjs: браузер и сеть не нужны.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { renderTree } from '../web/js/views/request-parts-tree.js';
import {
  attemptCount, failureStamp, failureSignature,
  retrySignature, retryPhase, retryKey, RETRY_TTL_MS, RETRY_SENT_TEXT,
} from '../web/js/views/request-parts-model.js';
import { createTreeState, problemSignature, treeStateKey } from '../web/js/views/request-tree-state.js';
import { createNotices, createMarks, noticesKey } from '../web/js/views/notice-state.js';
import { newIdempotencyKey } from '../web/js/views/retry-button.js';

/* ─────────────────────────────────────────────────────────── мини-DOM: дополнения */

const P = FakeElement.prototype;

function walk(el, out = []) {
  for (const child of el.childNodes || []) {
    if (child.nodeType !== 1) continue;
    out.push(child);
    walk(child, out);
  }
  return out;
}

function matches(el, sel) {
  const s = String(sel).trim();
  if (!s) return false;
  if (s.startsWith('.')) return String(el.className || '').split(/\s+/).includes(s.slice(1));
  if (s.startsWith('[data-')) {
    const name = s.slice(1, -1);
    return el.hasAttribute(name);
  }
  return String(el.localName) === s.toLowerCase();
}

function installHelpers() {
  if (!('hidden' in P)) {
    Object.defineProperty(P, 'hidden', {
      configurable: true,
      get() { return this.hasAttribute('hidden'); },
      set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
    });
  }
  if (typeof P.remove !== 'function') {
    P.remove = function remove() {
      const parent = this.parentNode;
      if (!parent) return;
      const idx = parent.childNodes.indexOf(this);
      if (idx >= 0) parent.childNodes.splice(idx, 1);
      this.parentNode = null;
    };
  }
  if (typeof P.querySelector !== 'function') {
    P.querySelector = function querySelector(sel) {
      const list = [];
      for (const s of String(sel).split(',')) {
        for (const el of walk(this)) if (matches(el, s)) { list.push(el); break; }
        if (list.length) break;
      }
      return list[0] || null;
    };
  }
  if (typeof P.querySelectorAll !== 'function') {
    P.querySelectorAll = function querySelectorAll(sel) {
      const sels = String(sel).split(',').map((s) => s.trim()).filter(Boolean);
      return walk(this).filter((el) => sels.some((s) => matches(el, s)));
    };
  }
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) {
        if (node && typeof node.nodeType === 'number') this.appendChild(node);
        else if (node !== null && node !== undefined && node !== false) this.appendChild(new FakeText(node));
      }
    };
  }
  if (typeof P.focus !== 'function') P.focus = function focus() {};
  if (typeof P.scrollIntoView !== 'function') P.scrollIntoView = function scrollIntoView() {};
  p_get_isConnected();
}

function p_get_isConnected() {
  if ('isConnected' in P) return;
  Object.defineProperty(P, 'isConnected', {
    configurable: true,
    get() { let cur = this; while (cur) { if (cur === fakeRoot) return true; cur = cur.parentNode; } return false; },
  });
}

let fakeRoot = null;

function mount() {
  fakeRoot = installFakeDom().createElement('div');
  installHelpers();
  return fakeRoot;
}

/* ───────────────────────────────────────────────────────────────— данные стенда */

const RID_ERR = 't_tree0err';
const RID_OK = 't_tree0ok';

function failedRun(id, startedAt, error) {
  return { id, profile: 'biz-cfo', status: 'failed', outcome: 'failed',
           started_at: startedAt, ended_at: startedAt + 60, error };
}

function errorDetail(rid, over = {}) {
  const runs = over.runs || [
    failedRun(1, 1793160510, "exit 1: KeyError('cost_packaging')"),
    failedRun(2, 1793160700, "exit 1: KeyError('cost_packaging') (повтор)"),
  ];
  const message = over.message
    || "Процесс воркера завершился с кодом 1: KeyError('cost_packaging') — в таблице "
     + 'поставщика C:\\TeamRoom\\Agents\\default\\2026-11-06 Письмо Лебедев — ответ\\custo.xlsx '
     + 'нет колонки с ценой короба, поэтому расчёт маржи по каналам остановлен.';
  return {
    id: rid,
    room: 'cafe',
    status: 'failed',
    status_reason: 'Воркер расчёта упал дважды (exit 1)',
    progress: { done: 1, total: 2 },
    tree: [
      { id: 't_root', parent_ids: [], title: 'Юнит-экономика', role: 'root', status: 'blocked',
        assignee: 'biz-director', started_at: 1793159930, runs: [], comments: [], error: null },
      { id: 't_err', parent_ids: ['t_root'], title: 'Посчитать маржу по каналам', role: 'worker',
        status: 'blocked', assignee: 'biz-cfo', started_at: 1793160510, runs,
        comments: [], needs_human: false, outside_room: false,
        error: { code: 'worker_crashed', message } },
      { id: 't_ok', parent_ids: ['t_root'], title: 'Собрать себестоимость', role: 'worker',
        status: 'done', assignee: 'biz-ops', started_at: 1793159960, finished_at: 1793160500,
        runs: [{ id: 5, profile: 'biz-ops', status: 'done', outcome: 'completed',
                 started_at: 1793159960, ended_at: 1793160500, error: null }],
        comments: [{ author: 'biz-ops', text: 'Себестоимость готова', at: 1793160500 }],
        needs_human: false, error: null, outside_room: false },
    ],
    warnings: [{ code: 'card_failed', message: 'Карточка расчёта упала дважды', detail: { task_id: 't_err' } }],
    timeline: [],
  };
}

function ctxStub() {
  const toasts = [];
  return {
    toasts,
    room: { id: 'cafe', members: [], orchestrator: 'biz-director', critic: '' },
    toast(msg, kind) { toasts.push({ msg, kind }); },
    api: { get: async () => ({ id: 'cafe', members: [] }), post: async () => ({ ok: true }) },
  };
}

function rowOf(card, taskId) {
  return walk(card).find((el) => el.getAttribute('data-task-id') === String(taskId)) || null;
}

/* ─────────────────────────────────────────────────────────── раскрытость (B-19) */

test('B-19: проблемный узел раскрыт сразу, выбор человека переживает перерисовку', () => {
  mount();
  const rid = 't_tree19a';
  const detail = errorDetail(rid);
  const first = renderTree(detail, ctxStub(), {});
  const row = rowOf(first, 't_err');
  assert.ok(row, 'строка карточки в дереве');
  const panel = row.querySelector('.rp-node__panel');
  assert.equal(panel.hidden, false, 'ошибку видно без клика');

  // человек сворачивает ветку
  row.querySelector('.rp-node__toggle').dispatch('click');
  assert.equal(panel.hidden, true);

  // страница перерисовалась по опросу — состояние берём из данных, а не из DOM
  const second = renderTree(detail, ctxStub(), {});
  assert.equal(rowOf(second, 't_err').querySelector('.rp-node__panel').hidden, true,
    'свёрнутое человеком остаётся свёрнутым');
  // и ли-строка имеет стабильный ключ — точечная правка узнаёт её
  assert.equal(rowOf(second, 't_err').getAttribute('data-key'), 'rp-node-t_err');
  uninstallFakeDom();
});

test('B-19: обычный узел закрыт по умолчанию и остаётся раскрытым после перерисовки', () => {
  mount();
  const detail = errorDetail('t_tree19b');
  const first = renderTree(detail, ctxStub(), {});
  const row = rowOf(first, 't_ok');
  const panel = row.querySelector('.rp-node__panel');
  assert.equal(panel.hidden, true, 'без проблемы узел закрыт');
  row.querySelector('.rp-node__toggle').dispatch('click');
  assert.equal(panel.hidden, false);
  const second = renderTree(detail, ctxStub(), {});
  assert.equal(rowOf(second, 't_ok').querySelector('.rp-node__panel').hidden, false,
    'раскрытое человеком переживает перерисовку');
  uninstallFakeDom();
});

test('B-19: авто-раскрытие — один раз на ошибку, новая ошибка раскрывает снова', () => {
  mount();
  const rid = 't_tree19c';
  const detail = errorDetail(rid);
  const first = renderTree(detail, ctxStub(), {});
  const row = rowOf(first, 't_err');
  row.querySelector('.rp-node__toggle').dispatch('click');          // свернул
  assert.equal(row.querySelector('.rp-node__panel').hidden, true);

  // та же ошибка, новая попытка (номер попытки вырос) — это уже другая подпись
  const more = errorDetail(rid, {
    runs: [
      failedRun(1, 1793160510, "exit 1: KeyError('cost_packaging')"),
      failedRun(2, 1793160700, "exit 1: KeyError('cost_packaging') (повтор)"),
      failedRun(3, 1793160900, 'timeout after 1800s'),
    ],
  });
  const second = renderTree(more, ctxStub(), {});
  assert.equal(rowOf(second, 't_err').querySelector('.rp-node__panel').hidden, false,
    'новая попытка показывает проблему снова');

  // и второй раз подряд та же подпись уже не раскрывается
  const third = renderTree(more, ctxStub(), {});
  assert.equal(rowOf(third, 't_err').querySelector('.rp-node__panel').hidden, false,
    'после авто-раскрытия состояние стабильно');
  uninstallFakeDom();
});

/* ─────────────────────────────────────────────────────── плашка ошибки (B-24) */

test('B-24: плашка показывает причину и попытки, закрывается «Понятно» и помнит это', () => {
  mount();
  const rid = 't_tree24a';
  const detail = errorDetail(rid);
  const first = renderTree(detail, ctxStub(), {});
  const row = rowOf(first, 't_err');
  const plaque = row.querySelector('.rp-node__error');
  assert.ok(plaque, 'плашка ошибки есть');
  assert.match(plaque.textContent, /Задача не справилась: в данных не нашлось поле/);
  assert.match(plaque.textContent, /Попытка 1/);
  assert.match(plaque.textContent, /Попытка 2/);
  const close = plaque.querySelector('.rp-node__error-close');
  assert.ok(close, 'кнопка закрытия — настоящая кнопка (доступна с клавиатуры)');
  assert.equal(close.getAttribute('aria-label'), 'Понятно: скрыть сообщение об ошибке');

  close.dispatch('click');
  assert.equal(row.querySelector('.rp-node__error'), null, 'плашка исчезла');
  assert.match(row.querySelector('.rp-node__panel').textContent, /Причина ошибки скрыта/);

  // перерисовка по опросу: закрытая плашка не возвращается
  const second = renderTree(detail, ctxStub(), {});
  assert.equal(rowOf(second, 't_err').querySelector('.rp-node__error'), null,
    'закрытие переживает перерисовку');

  // новая (третья) попытка — плашка снова
  const more = errorDetail(rid, {
    runs: [failedRun(1, 1793160510, "exit 1: KeyError('cost_packaging')"),
           failedRun(2, 1793160700, "exit 1: KeyError('cost_packaging') (повтор)"),
           failedRun(3, 1793160900, 'timeout after 1800s')],
  });
  const third = renderTree(more, ctxStub(), {});
  assert.ok(rowOf(third, 't_err').querySelector('.rp-node__error'),
    'новая попытка показывает плашку снова');
  uninstallFakeDom();
});

test('B-24: общий баннер запроса тоже закрывается и перечисляет попытки', () => {
  mount();
  const rid = 't_tree24b';
  const detail = errorDetail(rid);
  const card = renderTree(detail, ctxStub(), {});
  // общий баннер строит renderWarnings (request-parts.js) — проверяем через notice-state:
  const notices = createNotices({ requestId: rid });
  const stamp = failureSignature(detail);
  assert.equal(stamp, 't_err:2');
  assert.equal(notices.isDismissed('request', 'card_failed', stamp), false);
  notices.dismiss('request', 'card_failed', stamp);
  assert.equal(createNotices({ requestId: rid }).isDismissed('request', 'card_failed', stamp), true,
    'закрытие запоминается в хранилище запроса');
  // карточка дерева при этом остаётся на месте
  assert.ok(rowOf(card, 't_err'));
  uninstallFakeDom();
});

/* ──────────────────────────────────────────────────────── кнопка повтора (B-25) */

test('B-25: кнопка повтора блокируется после отправки и разблокируется на новой попытке', () => {
  mount();
  const rid = 't_tree25a';
  const detail = errorDetail(rid);
  const node = detail.tree.find((n) => n.id === 't_err');

  // отметка «уже отправлено» в том же хранилище, что использует дерево
  createMarks({ requestId: rid }).mark(retryKey(rid, 't_err'),
    { sig: retrySignature(node), at: Date.now() });

  const first = renderTree(detail, ctxStub(), {});
  const wrap = rowOf(first, 't_err').querySelector('.rp-retry');
  assert.ok(wrap, 'кнопка повтора на карточке');
  const btn = wrap.querySelector('button');
  assert.equal(btn.disabled, true, 'после отправки кнопка заблокирована');
  assert.match(wrap.textContent, new RegExp(RETRY_SENT_TEXT));

  // перерисовка по опросу не разблокирует
  const second = renderTree(detail, ctxStub(), {});
  assert.equal(rowOf(second, 't_err').querySelector('.rp-retry').querySelector('button').disabled, true);

  // пошла новая попытка — подпись другая, кнопка снова обычная
  const more = errorDetail(rid, {
    runs: [failedRun(1, 1793160510, "exit 1: KeyError('cost_packaging')"),
           failedRun(2, 1793160700, "exit 1: KeyError('cost_packaging') (повтор)"),
           failedRun(3, 1793160900, 'timeout after 1800s')],
  });
  const third = renderTree(more, ctxStub(), {});
  const again = rowOf(third, 't_err').querySelector('.rp-retry');
  assert.equal(again.querySelector('button').disabled, false, 'новая попытка разблокирует кнопку');
  assert.equal(again.textContent.includes(RETRY_SENT_TEXT), false);
  uninstallFakeDom();
});

test('B-25: ключ идемпотентности уникален для нажатия и пригоден серверу', () => {
  const a = newIdempotencyKey('t_err');
  const b = newIdempotencyKey('t_err');
  assert.notEqual(a, b);
  for (const key of [a, b]) {
    assert.match(key, /^[A-Za-z0-9._:\-]{6,120}$/);
  }
});

/* ───────────────────────────────────────────────────── чистые решения и хранилища */

test('attemptCount, failureStamp, failureSignature: номер попытки — метка плашки', () => {
  const node = { id: 't_1', status: 'blocked', runs: [{}, {}], error: { message: 'boom' } };
  assert.equal(attemptCount(node), 2);
  assert.equal(failureStamp(node), 't_1:2');
  assert.equal(attemptCount({ id: 't_2', status: 'blocked' }), 0, 'без попыток и без ошибки — 0');
  assert.equal(failureStamp({ id: 't_2', status: 'blocked' }), '', 'нет ошибки — нет плашки');
  assert.equal(failureSignature({ tree: [{ id: 'b', runs: [{}], error: {} },
                                          { id: 'a', runs: [{}, {}], error: {} }] }), 'a:2|b:1');
  assert.equal(failureSignature({ tree: [] }), '');
});

test('retryPhase: отправлено → таймаут → обычная; новая попытка снимает блокировку', () => {
  const now = 1_000_000;
  const entry = { sig: 't_1|2|0', at: now };
  assert.equal(retryPhase(entry, 't_1|2|0', now + 1000), 'sent');
  assert.equal(retryPhase(entry, 't_1|3|0', now + 1000), 'idle', 'вырос номер попытки');
  assert.equal(retryPhase(entry, 't_1|2|0', now + RETRY_TTL_MS + 1), 'idle', 'таймаут');
  assert.equal(retryPhase(null, 't_1|2|0', now), 'idle');
});

test('problemSignature: новая ошибка и новая попытка меняют подпись', () => {
  const base = { id: 't_1', error: { code: 'worker_crashed', message: 'exit 1' }, runs: [{}] };
  const same = { ...base, runs: [{}, {}] };
  const other = { ...base, error: { code: 'timeout', message: 'timeout after 1800s' } };
  assert.equal(problemSignature({ id: 't_1' }), '');
  assert.notEqual(problemSignature(base), problemSignature(same));
  assert.notEqual(problemSignature(base), problemSignature(other));
});

test('createTreeState: состояние живёт в хранилище и читается новым экземпляром', () => {
  const memory = new Map();
  const store = { get: (k) => (memory.has(k) ? memory.get(k) : null), set: (k, v) => memory.set(k, String(v)) };
  const rid = 't_tree19store';
  const first = createTreeState({ requestId: rid, store });
  assert.equal(first.isOpen('a'), false);
  assert.equal(first.autoOpen('a', 'err:x'), true, 'первое появление проблемы');
  assert.equal(first.isOpen('a'), true);
  assert.equal(first.autoOpen('a', 'err:x'), false, 'второй раз не раскрываем');
  first.setOpen('a', false, 'err:x');
  assert.equal(first.isOpen('a'), false);

  const second = createTreeState({ requestId: rid, store });
  assert.equal(second.isOpen('a'), false, 'выбор человека прочитан из хранилища');
  assert.equal(second.autoOpen('a', 'err:x'), false, 'и авто-раскрытие больше не срабатывает');
  assert.equal(treeStateKey(rid), 'teamroom.tree.open.' + rid);
  second.retain(['b']);
  assert.equal(createTreeState({ requestId: rid, store }).snapshot().open.length, 0,
    'узлы, которых больше нет в дереве, забываются');
});

test('createNotices: закрытие по штампу, «показать снова» и другой штамп', () => {
  const memory = new Map();
  const store = { get: (k) => (memory.has(k) ? memory.get(k) : null), set: (k, v) => memory.set(k, String(v)) };
  const rid = 't_tree24store';
  const n = createNotices({ requestId: rid, store });
  assert.equal(n.isDismissed('node', 't_1', 't_1:2'), false);
  n.dismiss('node', 't_1', 't_1:2');
  assert.equal(n.isDismissed('node', 't_1', 't_1:2'), true);
  assert.equal(n.isDismissed('node', 't_1', 't_1:3'), false, 'новая попытка — плашка снова');
  assert.equal(n.isDismissed('node', 't_2', 't_1:2'), false, 'другая карточка не задета');
  n.restore('node', 't_1');
  assert.equal(n.isDismissed('node', 't_1', 't_1:2'), false);
  assert.equal(noticesKey(rid), 'teamroom.notices.' + rid);
});
