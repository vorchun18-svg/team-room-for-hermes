/**
 * request-attempts.test.mjs — W28-C2 «ошибки, которых нет» (дерево работ).
 *
 * Что было не так (дефект 5, интерфейс): убитые контролем моделей попытки
 * (`reclaimed`, «контроль моделей: неверная модель») рисовались красной плашкой
 * «Задача завершилась ошибкой» и попадали в счётчик; подпись «ушёл на» бралась из
 * первой попытки, а не из последней.
 *
 * Здесь проверяется только то, что видит человек:
 *   * убитая попытка — СПОКОЙНАЯ строка «Попытка 1 остановлена контролем моделей:
 *     была не та модель (вызовов 1)», без `banner--error`;
 *   * красное — только по итогу карточки (crashed/failed/timed_out/gave_up);
 *   * «Попытки · N» перечисляет все попытки с ИХ моделью и числом вызовов;
 *   * счётчик «ошибок» (дерево) не считает убитые попытки.
 *
 * DOM — мини-DOM из tests/fake-dom.mjs: браузер и сеть не нужны.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement, FakeText } from './fake-dom.mjs';
import { renderTree as renderTreeRaw } from '../web/js/views/request-parts-tree.js';
import {
  attemptList, attemptRows, guardAttempts, guardAttemptLine, cardFailed,
  attemptOutcome, isGuardAttempt, BAD_OUTCOMES, attemptTotal,
} from '../web/js/views/request-parts-model.js';
import { treeProblems, treeSummaryChips } from '../web/js/views/section-collapse.js';

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

let fakeRoot = null;

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
      for (const s of String(sel).split(',')) {
        for (const el of walk(this)) if (matches(el, s)) return el;
      }
      return null;
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
  if (!('isConnected' in P)) {
    Object.defineProperty(P, 'isConnected', {
      configurable: true,
      get() { let cur = this; while (cur) { if (cur === fakeRoot) return true; cur = cur.parentNode; } return false; },
    });
  }
}

function mount() {
  fakeRoot = installFakeDom().createElement('div');
  installHelpers();
  return fakeRoot;
}

/** Дерево свёрнуто по умолчанию (У-1): раскрываем блок тем же кликом, что человек. */
function renderTree(detail, ctx, opts) {
  const card = renderTreeRaw(detail, ctx, opts);
  const toggle = card.querySelector ? card.querySelector('.sec__toggle') : null;
  if (toggle) toggle.dispatch('click');
  return card;
}

/* ────────────────────────────────────────────────────────────── данные стенда */

const GUARD = 'manual_reclaim: контроль моделей: неверная модель';

/** Попытка в форме API (attempts[] из сервера, W28-C2). */
function attempt(n, outcome, over = {}) {
  return Object.assign({
    n, outcome, reason: '', model: '', provider: '', calls: null,
    started_at: 1795120000 + n * 100, ended_at: 1795120000 + n * 100 + 40,
    duration_s: 40, guard: false, ok: outcome === 'completed', final: false,
  }, over);
}

function guardAttempt(n, over = {}) {
  return attempt(n, 'reclaimed', Object.assign({
    reason: 'остановлена контролем моделей: была не та модель',
    guard: true, ok: false, calls: 1, model: 'glm-5.3-flash', provider: 'ollama-cloud',
  }, over));
}

function okAttempt(n, model, calls, over = {}) {
  return attempt(n, 'completed', Object.assign({
    model, provider: 'ollama-cloud', calls, final: true,
  }, over));
}

/**
 * Запрос из приёмки: две карточки, у каждой убитая контролем попытка 1 и
 * успешная попытка 2. Итоговых сбоев нет.
 */
function recoveredDetail(over = {}) {
  return Object.assign({
    id: 't_w28c2aa',
    room: 'fn',
    status: 'done',
    progress: { done: 2, total: 2 },
    model_guard: [{ kind: 'stopped', at: 1795120050, card_id: 't_lawyer', profile: 'lawyer' }],
    tree: [
      { id: 't_root', parent_ids: [], title: 'Запрос', role: 'root', status: 'done',
        assignee: 'default', started_at: 1795119900, runs: [], comments: [], error: null },
      { id: 't_lawyer', parent_ids: ['t_root'], title: 'Юридический разбор', role: 'worker',
        status: 'done', assignee: 'lawyer', started_at: 1795120000, finished_at: 1795120400,
        comments: [], needs_human: false, outside_room: false, error: null,
        model_sent: { text: 'ушло на: ollama-cloud · deepseek-v4.1-flash:1017', confirmed: true },
        attempts: [guardAttempt(1), okAttempt(2, 'deepseek-v4.1-flash:1017', 35)],
        runs: [{ id: 152, profile: 'lawyer', status: 'reclaimed', outcome: 'reclaimed',
                 started_at: 1795120000, ended_at: 1795120017, error: GUARD },
        { id: 154, profile: 'lawyer', status: 'done', outcome: 'completed',
          started_at: 1795120060, ended_at: 1795120400, error: null }] },
      { id: 't_surveyor', parent_ids: ['t_root'], title: 'Проверить факты', role: 'worker',
        status: 'done', assignee: 'surveyor', started_at: 1795120000, finished_at: 1795120685,
        comments: [], needs_human: false, outside_room: false, error: null,
        model_sent: { text: 'ушло на: ollama-cloud · deepseek-v4.1-flash:1017', confirmed: true },
        attempts: [guardAttempt(1, { calls: 3, model: 'claude-sonnet-5-5', provider: 'anthropic' }),
                   okAttempt(2, 'deepseek-v4.1-flash:1017', 40)],
        runs: [] },
    ],
    warnings: [],
    timeline: [],
  }, over);
}

/** Запрос, где карточка действительно упала (два failed) — красное должно остаться. */
function failedDetail() {
  return {
    id: 't_w28c2bb',
    room: 'fn',
    status: 'failed',
    progress: { done: 1, total: 2 },
    tree: [
      { id: 't_root', parent_ids: [], title: 'Запрос', role: 'root', status: 'failed',
        started_at: 1795119900, runs: [], comments: [], error: null },
      { id: 't_bad', parent_ids: ['t_root'], title: 'Посчитать маржу', role: 'worker',
        status: 'failed', assignee: 'cfo', started_at: 1795120000, comments: [],
        needs_human: false, outside_room: false,
        error: { code: 'failed', message: "exit 1: KeyError('cost_packaging')" },
        attempts: [
          attempt(1, 'failed', { reason: "exit 1: KeyError('cost_packaging')", calls: 2, model: 'deepseek-v4.1-flash:1017' }),
          attempt(2, 'timed_out', { reason: 'timeout after 1800s', calls: 4, model: 'deepseek-v4.1-flash:1017', final: true }),
        ],
        runs: [{ id: 90, profile: 'cfo', status: 'failed', outcome: 'failed',
                 started_at: 1795120000, ended_at: 1795120100, error: "exit 1: KeyError('cost_packaging')" },
        { id: 91, profile: 'cfo', status: 'failed', outcome: 'timed_out',
          started_at: 1795120200, ended_at: 1795122000, error: 'timeout after 1800s' }] },
    ],
    warnings: [{ code: 'card_failed', message: 'Есть упавшие карточки: 1', detail: { task_ids: ['t_bad'] } }],
    timeline: [],
  };
}

function ctxStub() {
  const toasts = [];
  return {
    toasts,
    room: { id: 'fn', members: [], orchestrator: 'default', critic: '' },
    toast(msg, kind) { toasts.push({ msg, kind }); },
    api: { get: async () => ({ id: 'fn', members: [] }), post: async () => ({ ok: true }) },
  };
}

function rowOf(card, taskId) {
  return walk(card).find((el) => el.getAttribute('data-task-id') === String(taskId)) || null;
}

function textOf(el) {
  if (!el) return '';
  if (el.nodeType === 3) return el.data;
  return (el.childNodes || []).map(textOf).join(' ');
}

/* ──────────────────────────────────────────────── чистые данные попыток */

test('attemptList: серверное attempts[] и запасной разбор runs', () => {
  const detail = recoveredDetail();
  const lawyer = detail.tree.find((n) => n.id === 't_lawyer');
  const list = attemptList(lawyer);
  assert.equal(list.length, 2);
  assert.deepEqual(list.map((a) => a.outcome), ['reclaimed', 'completed']);
  assert.equal(list[0].guard, true, 'попытку убил контроль моделей');
  assert.equal(list[0].calls, 1);
  assert.equal(list[1].final, true);
  // старый ответ без attempts[] — то же самое из runs
  const legacy = attemptList({ runs: lawyer.runs });
  assert.deepEqual(legacy.map((a) => a.outcome), ['reclaimed', 'completed']);
  assert.equal(legacy[0].guard, true, 'сторож виден и по тексту причины');
});

test('attemptOutcome: итоги узнаются по outcome и по status', () => {
  assert.equal(attemptOutcome({ status: 'done', outcome: 'completed' }), 'completed');
  assert.equal(attemptOutcome({ status: 'reclaimed' }), 'reclaimed');
  assert.equal(attemptOutcome({ outcome: 'timeout' }), 'timed_out');
  assert.equal(attemptOutcome({ status: 'gave_up' }), 'gave_up');
  assert.deepEqual(BAD_OUTCOMES, ['crashed', 'failed', 'timed_out', 'gave_up']);
  assert.equal(isGuardAttempt({ outcome: 'reclaimed', reason: GUARD }), true);
  assert.equal(isGuardAttempt({ outcome: 'reclaimed', reason: 'manual_reclaim: таймаут' }), false);
});

test('cardFailed: итог карточки, а не число неудачных попыток', () => {
  const detail = recoveredDetail();
  const lawyer = detail.tree.find((n) => n.id === 't_lawyer');
  assert.equal(cardFailed(lawyer), false, 'убитая попытка + успешный повтор — не сбой');
  // карточка ещё в очереди после убитой попытки — тоже не сбой
  assert.equal(cardFailed({ status: 'ready', attempts: [guardAttempt(1)] }), false);
  // настоящий сбой остаётся красным
  assert.equal(cardFailed(failedDetail().tree[1]), true);
  // сбой, закрытый успешным повтором, красным не остаётся
  assert.equal(cardFailed({ status: 'done', attempts: [attempt(1, 'crashed'), attempt(2, 'completed', { final: true })] }), false);
});

test('guardAttemptLine: спокойная строка с числом вызовов', () => {
  assert.equal(guardAttemptLine({ n: 1, calls: 1 }),
    'Попытка 1 остановлена контролем моделей: была не та модель (вызовов 1)');
  assert.equal(guardAttemptLine({ n: 2, calls: 0 }),
    'Попытка 2 остановлена контролем моделей: была не та модель (вызовов 0)');
  assert.equal(guardAttemptLine({ n: 3, calls: null }),
    'Попытка 3 остановлена контролем моделей: была не та модель');
});

test('attemptRows: у каждой попытки своя модель и вызовы', () => {
  const detail = recoveredDetail();
  const surveyor = detail.tree.find((n) => n.id === 't_surveyor');
  const rows = attemptRows(surveyor);
  assert.equal(rows.length, 2);
  assert.match(rows[0].text, /остановлена контролем моделей/);
  assert.equal(rows[0].model, 'anthropic · claude-sonnet-5-5');
  assert.match(rows[0].detail, /вызовов 3/);
  assert.equal(rows[1].status, 'готово');
  assert.match(rows[1].detail, /deepseek-v4\.1-flash:1017/);
  assert.equal(guardAttempts(surveyor).length, 1);
  assert.equal(attemptTotal(surveyor), 2);
});

/* ────────────────────────────────────────────────────────────── дерево работ */

test('W28-C2: убитая попытка — спокойная строка, красной плашки нет', () => {
  mount();
  const detail = recoveredDetail();
  const card = renderTree(detail, ctxStub(), {});
  const row = rowOf(card, 't_lawyer');
  assert.ok(row, 'строка карточки есть');

  const note = row.querySelector('.rp-node__attempt-note');
  assert.ok(note, 'спокойная строка под карточкой');
  assert.match(textOf(note), /Попытка 1 остановлена контролем моделей: была не та модель \(вызовов 1\)/);
  assert.match(textOf(note), /glm-5\.3-flash/, 'модель убитой попытки названа');
  assert.equal(row.querySelector('.banner--error'), null, 'красного баннера нет');
  assert.equal(row.querySelector('.rp-node__error-slot'), null, 'плашки ошибки нет вообще');
  uninstallFakeDom();
});

test('W28-C2: счётчик «ошибок» не растёт от убитых попыток', () => {
  const problems = treeProblems(recoveredDetail());
  assert.equal(problems.errors, 0, 'ошибок нет — их и не было');
  const chips = treeSummaryChips(recoveredDetail()).map((c) => c.text);
  assert.ok(!chips.some((t) => /ошиб/i.test(t)), 'чипа «ошибок» нет');
  // а настоящий сбой по-прежнему считается
  assert.equal(treeProblems(failedDetail()).errors, 1);
  assert.ok(treeSummaryChips(failedDetail()).some((c) => c.text === 'есть ошибка'));
});

test('W28-C2: «Попытки · 2» перечисляет обе попытки с их моделями', () => {
  mount();
  const detail = recoveredDetail();
  const card = renderTree(detail, ctxStub(), {});
  const row = rowOf(card, 't_lawyer');
  row.querySelector('.rp-node__toggle').dispatch('click');       // раскрыть карточку
  const runs = row.querySelector('.rp-runs');
  assert.ok(runs, 'блок попыток в раскрытой карточке');
  assert.match(textOf(runs), /Попытки · 2/);
  const text = textOf(runs);
  assert.match(text, /Попытка 1/, 'первая (убитая) попытка видна');
  assert.match(text, /glm-5\.3-flash/, 'её модель — своя');
  assert.match(text, /deepseek-v4\.1-flash:1017/, 'модель второй попытки');
  assert.match(text, /35/, 'число вызовов второй попытки');
  assert.equal(runs.querySelectorAll('.rp-run--guard').length, 1,
    'убитая попытка помечена нейтрально (свой класс, не «упавшая»)');
  uninstallFakeDom();
});

test('W28-C2: «ушёл на» — из последней попытки', () => {
  mount();
  const detail = recoveredDetail();
  const card = renderTree(detail, ctxStub(), {});
  const row = rowOf(card, 't_surveyor');
  const sub = row.querySelector('.rp-node__sub');
  const text = textOf(sub);
  assert.match(text, /deepseek-v4\.1-flash:1017/, 'последняя модель');
  assert.ok(!/claude-sonnet-5-5/.test(text), 'модель убитой попытки в подписи не висит');
  uninstallFakeDom();
});

test('W28-C2: настоящий сбой остаётся красным, и обе попытки перечислены', () => {
  mount();
  const card = renderTree(failedDetail(), ctxStub(), {});
  const row = rowOf(card, 't_bad');
  const plaque = row.querySelector('.rp-node__error-slot');
  assert.ok(plaque, 'красная плашка есть — карточка правда упала');
  const text = textOf(plaque);
  assert.match(text, /Задача не справилась/, 'причина названа по-русски');
  assert.match(text, /Попытка 1/);
  assert.match(text, /Попытка 2/);
  assert.ok(!/KeyError|timeout after|exit 1/.test(text), 'технический лог в лицо не лезет');
  uninstallFakeDom();
});

test('W28-C2: у спокойной строки есть кликабельная ссылка на «Контроль моделей»', () => {
  mount();
  const card = renderTree(recoveredDetail(), ctxStub(), {});
  const link = rowOf(card, 't_lawyer').querySelector('.rp-attempt__link');
  assert.ok(link, 'ссылка на ленту контроля — кнопка, а не текст');
  assert.equal(String(link.localName), 'button');
  assert.match(textOf(link), /Контроль моделей/);
  // ленты нет в данных — мёртвую ссылку не рисуем
  const without = renderTree(recoveredDetail({ model_guard: [] }), ctxStub(), {});
  assert.equal(rowOf(without, 't_lawyer').querySelector('.rp-attempt__link'), null);
  uninstallFakeDom();
});

test('W28-C2: без попыток карточка тихая, строка не появляется', () => {
  mount();
  const card = renderTree(recoveredDetail(), ctxStub(), {});
  const root = rowOf(card, 't_root');
  assert.equal(root.querySelector('.rp-node__attempt-note'), null);
  assert.equal(root.querySelector('.rp-node__error-slot'), null);
  uninstallFakeDom();
});
