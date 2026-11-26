/**
 * proposal-card.test.mjs — карточка «Предложение руководителя» (W30-C, контракт §2).
 *
 * Что проверяется:
 *  1) модель: разовая задача / объёмная с ветками / применённое предложение,
 *     итоги по веткам, подписи стадий, связи «родитель → подзадачи»;
 *  2) разовая задача: «Согласен» / «Не согласен» / «Свой вариант» — тела
 *     действий ровно по контракту (`branch_id:null`, `decision`, `text`);
 *  3) объёмная задача: у каждой ветки «Да» / «Нет» / «Свой вариант», внизу
 *     «Принять всё предложение» / «Отклонить» / «Свой вариант», при принятии
 *     уходят `accept_all` и `apply` (подзадачи создаёт сервер);
 *  4) после применения — ссылки на созданные задачи и блок «Подзадачи»;
 *  5) «Свой вариант» — поле + кнопка «Отправить» (диктовку ставит dictation-auto
 *     по общему правилу: поле и кнопка-действие в одной строке);
 *  6) никакого `innerHTML` из данных — только `h()`.
 *
 * DOM — tests/fake-dom.mjs, браузер не нужен. Живая проверка — Chromium на моке
 * `tools/mock_w30c.py` (порт 8855), снимки в docs/shots/w30-c/.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';
import {
  buildProposalCard, buildProjectBlock, buildParentNote,
  proposalModel, proposalStateMeta, branchMeta, proposalTotals, projectChildren,
  stageLabel, taskHref,
} from '../web/js/views/proposal-card.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

/* ─────────────────────────────────────────────────────────── мини-DOM утилиты */

function all(root, cls) {
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

function byAttr(root, name, value) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && n.getAttribute && n.getAttribute(name) === value) out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

function buttons(root) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && String(n.tagName || '').toLowerCase() === 'button') out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

/** Кнопка по подписи (по началу текста). */
function press(root, label) {
  const btn = buttons(root).find((b) => text(b).trim().startsWith(label));
  assert.ok(btn, 'нет кнопки «' + label + '»');
  btn.dispatch('click');
  return btn;
}

/* ──────────────────────────────────────────────────────────────── фикстуры */

const SINGLE = {
  id: 'pr_1', root_id: 't_root1', kind: 'single', state: 'pending',
  summary: 'Разовая задача: посчитать экономику приёма вещей на дому.',
  author: 'team-director', created_at: 1764190000,
  single: { decision: 'pending', text: '' }, branches: [], resolution: null,
};

const BIG = {
  id: 'pr_2', root_id: 't_root2', kind: 'big', state: 'pending',
  summary: 'Тема объёмная: три направления.',
  author: 'team-director', created_at: 1764191000,
  single: { decision: 'pending', text: '' },
  branches: [
    { id: 'b1', title: 'Сравнить лизинг и наёмные рейсы', goal: 'Таблица по трём вариантам',
      assignee: 'analyst', questions: ['Сколько рейсов нужно?'], order: 1,
      decision: 'pending', custom_text: '', task_id: null },
    { id: 'b2', title: 'Проверить договор с перевозчиком', goal: 'Ответственность за груз',
      assignee: 'lawyer', questions: [], order: 2, decision: 'yes', custom_text: '', task_id: null },
    { id: 'b3', title: 'График выхода машины', goal: 'Календарь без простоя',
      assignee: 'team-ops', questions: [], order: 3, decision: 'no', custom_text: '', task_id: null },
  ],
  resolution: null,
};

const APPLIED = {
  id: 'pr_3', root_id: 't_root3', kind: 'big', state: 'applied',
  summary: 'Запускаем через партнёра-курьера.',
  author: 'team-director', created_at: 1764100000,
  single: { decision: 'pending', text: '' },
  branches: [
    { id: 'b1', title: 'Договор с курьером', goal: '', assignee: 'lawyer', questions: [],
      order: 1, decision: 'yes', custom_text: '', task_id: 't_c0ffee04' },
    { id: 'b2', title: 'Памятка приёмщика', goal: '', assignee: 'team-ops', questions: [],
      order: 2, decision: 'custom', custom_text: 'Добавить фото вещи', task_id: 't_c0ff0101' },
    { id: 'b3', title: 'Цена выезда', goal: '', assignee: 'analyst', questions: [],
      order: 3, decision: 'no', custom_text: '', task_id: null },
  ],
  resolution: { action: 'apply', text: '', at: 1764189000 },
};

const PROJECT_PARENT = {
  parent_id: null,
  children: [
    { id: 't_c0ffee04', title: 'Договор с курьером и страхование вещей', status: 'running', stage: 'working' },
    { id: 't_c0ff0101', title: 'Памятка приёмщика для выезда на дом', status: 'blocked', stage: 'needs_you' },
  ],
};

/** Стенд карточки: записывает отправленные запросы и перерисовки. */
function stand(proposal, extra = {}) {
  installFakeDom();
  const posted = [];
  const toasts = [];
  const api = {
    async post(path, body) { posted.push({ path, body }); return { ok: true }; },
    async get() { return {}; },
  };
  const keepValues = new Map();
  const node = buildProposalCard(Object.assign({
    proposal,
    roomId: 'cafe',
    requestId: 't_root1',
    api,
    toast: (message, kind) => toasts.push({ message, kind }),
    refresh: async () => { posted.push({ refresh: true }); },
    keepValues,
  }, extra));
  return { node, posted, toasts, api, keepValues };
}

/* ─────────────────────────────────────────────────────────────── 1. модель */

test('модель: разовая задача, ветки и применённое предложение', () => {
  const single = proposalModel(SINGLE);
  assert.equal(single.present, true);
  assert.equal(single.kind, 'single');
  assert.equal(single.kindLabel, 'разовая задача');
  assert.equal(single.canDecide, true);
  assert.equal(single.branches.length, 0);

  const big = proposalModel(BIG);
  assert.equal(big.kind, 'big');
  assert.equal(big.kindLabel, 'объёмная задача');
  assert.equal(big.branches.length, 3);
  assert.equal(big.branches[0].id, 'b1');
  assert.equal(big.branches[0].questions[0], 'Сколько рейсов нужно?');
  const totals = proposalTotals(BIG);
  assert.deepEqual(
    { yes: totals.yes, no: totals.no, pending: totals.pending, tasks: totals.tasks },
    { yes: 1, no: 1, pending: 1, tasks: 0 });

  const applied = proposalModel(APPLIED);
  assert.equal(applied.applied, true);
  assert.equal(applied.canDecide, false, 'по применённому предложению решать нечего');
  assert.equal(proposalTotals(APPLIED).tasks, 2);

  assert.equal(proposalModel(null).present, false);
  assert.equal(buildProposalCard({ proposal: null, requestId: 't_1' }), null,
    'без предложения карточки нет — страница не обрастает пустыми блоками');
});

test('подписи: состояние предложения, ветка, стадия подзадачи', () => {
  assert.equal(proposalStateMeta(SINGLE).label, 'ждёт вашего решения');
  assert.equal(proposalStateMeta(APPLIED).label, 'задачи созданы');
  assert.equal(proposalStateMeta({ state: 'rejected' }).label, 'отклонено');
  assert.equal(branchMeta({ decision: 'yes' }).label, 'да');
  assert.equal(branchMeta({ decision: 'no' }).label, 'нет');
  assert.equal(branchMeta({ decision: 'custom' }).label, 'свой вариант');
  assert.equal(branchMeta({}).label, 'ждёт решения');
  assert.equal(stageLabel('working'), 'В работе');
  assert.equal(stageLabel('done_system'), 'Готово');
  assert.equal(stageLabel('accepted'), 'Принята');
  assert.equal(stageLabel('needs_you'), 'Ждёт вас');
  assert.equal(stageLabel('', 'running'), 'running', 'неизвестная стадия не выдумывается');
  assert.equal(taskHref('cafe', 't_1'), '#/room/cafe/request/t_1');
  assert.equal(taskHref('', 't_1'), '#/request/t_1');
  assert.equal(projectChildren(PROJECT_PARENT).length, 2);
});

/* ─────────────────────────────────────────────── 2. разовая задача: кнопки */

test('разовая задача: «Согласен» отправляет решение по задаче', async () => {
  const s = stand(SINGLE);
  try {
    const t = text(s.node);
    assert.match(t, /Предложение руководителя/);
    assert.match(t, /разовая задача/);
    assert.match(t, /Согласен/);
    assert.match(t, /Не согласен/);
    assert.match(t, /Свой вариант/);
    assert.match(t, /одной задаче/, 'человеку сказано, что будет после кнопки');

    press(s.node, 'Согласен');
    await tick(); await tick();
    const sent = s.posted.find((p) => p.body);
    assert.ok(sent, 'решение ушло на сервер');
    assert.equal(sent.path, '/api/requests/t_root1/proposal/decision');
    assert.deepEqual(sent.body, { branch_id: null, decision: 'agree' });
  } finally {
    uninstallFakeDom();
  }
});

test('разовая задача: «Не согласен» — отказ с причиной, «Свой вариант» — текст', async () => {
  const s = stand(SINGLE);
  try {
    press(s.node, 'Не согласен');
    await tick(); await tick();
    assert.deepEqual(s.posted[0].body, { branch_id: null, decision: 'disagree' });

    // «Свой вариант» разворачивает поле; пустое поле не отправляется.
    press(s.node, 'Свой вариант');
    const field = all(s.node, 'proposal__custom-input')[0];
    assert.ok(field, 'поле своего варианта');
    assert.equal(field.getAttribute('data-keep'), 'pr:pr_1', 'набранное переживёт перерисовку');
    // Поле и «Отправить» — в одной строке: dictation-auto поставит микрофон рядом.
    const send = all(s.node, 'proposal__custom-send')[0];
    assert.ok(send, 'кнопка «Отправить»');
    assert.equal(send.parentNode, field.parentNode, 'поле и кнопка-действие в одной строке');
    send.dispatch('click');
    await tick();
    assert.equal(s.toasts.some((x) => x.kind === 'warn'), true, 'пустой текст — предупреждение, не запрос');

    field.value = 'Разбить на две задачи: договор и памятка';
    field.dispatch('input');
    assert.equal(s.keepValues.get('pr:pr_1'), 'Разбить на две задачи: договор и памятка');
    send.dispatch('click');
    await tick(); await tick();
    const custom = s.posted.filter((p) => p.body).pop();
    assert.deepEqual(custom.body,
      { branch_id: null, decision: 'custom', text: 'Разбить на две задачи: договор и памятка' });
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────────────────────── 3. объёмная задача: ветки */

test('объёмная задача: у каждой ветки «Да» / «Нет» / «Свой вариант»', async () => {
  const s = stand(BIG);
  try {
    const rows = all(s.node, 'proposal__branch');
    assert.equal(rows.length, 3, 'все ветки на месте');
    const first = rows.find((r) => r.getAttribute('data-branch') === 'b1');
    const t = text(first);
    assert.match(t, /Сравнить лизинг и наёмные рейсы/);
    assert.match(t, /Таблица по трём вариантам/, 'цель ветки видна');
    assert.match(t, /analyst|аналитик/i, 'исполнитель ветки виден');
    assert.match(t, /Вопросы к ветке: 1/);
    assert.deepEqual(buttons(first).map((b) => text(b).trim().split('\n')[0]).filter(Boolean)
      .filter((x) => ['Да', 'Нет', 'Свой вариант'].includes(x)),
    ['Да', 'Нет', 'Свой вариант']);

    // Подпись статуса ветки — решением человека, а не догадкой.
    const second = rows.find((r) => r.getAttribute('data-branch') === 'b2');
    assert.equal(second.getAttribute('data-branch-decision'), 'yes');
    assert.match(text(second), /да/);

    press(first, 'Да');
    await tick(); await tick();
    assert.deepEqual(s.posted.find((p) => p.body).body, { branch_id: 'b1', decision: 'yes' });

    press(first, 'Нет');
    await tick(); await tick();
    assert.deepEqual(s.posted.filter((p) => p.body).pop().body, { branch_id: 'b1', decision: 'no' });
  } finally {
    uninstallFakeDom();
  }
});

test('объёмная задача: внизу «Принять всё» / «Отклонить» / «Свой вариант»', async () => {
  const s = stand(BIG);
  try {
    const t = text(s.node);
    assert.match(t, /Принять всё предложение/);
    assert.match(t, /Отклонить/);
    assert.match(t, /да: 1/);
    assert.match(t, /нет: 1/);
    assert.match(t, /без решения: 1/);

    press(s.node, 'Принять всё предложение');
    await tick(); await tick(); await tick();
    const bodies = s.posted.filter((p) => p.body).map((p) => p.body);
    assert.deepEqual(bodies.slice(0, 2), [{ action: 'accept_all' }, { action: 'apply' }],
      'сначала «все да», потом создание подзадач');
    assert.ok(s.posted.every((p) => !p.body || p.path.endsWith('/proposal/resolve')),
      'общее решение идёт только маршрутом resolve');
    assert.ok(s.posted.some((p) => p.refresh), 'после решения страница перечитывается');
  } finally {
    uninstallFakeDom();
  }
});

test('объёмная задача: «Отклонить» ничего не создаёт', async () => {
  const s = stand(BIG);
  try {
    press(s.node, 'Отклонить');
    await tick(); await tick();
    assert.deepEqual(s.posted.find((p) => p.body).body, { action: 'reject_all' });
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────────────── 4. применённое: ссылки на задачи */

test('предложение применено: видны созданные подзадачи и «Часть проекта»', () => {
  const s = stand(APPLIED);
  try {
    const links = all(s.node, 'proposal__created-link');
    assert.equal(links.length, 2, 'ссылка на каждую созданную задачу');
    assert.equal(links[0].getAttribute('href'), '#/room/cafe/request/t_c0ffee04');
    assert.match(text(s.node), /Созданные подзадачи/);
    assert.equal(all(s.node, 'proposal__footer').length, 0, 'по применённому решать нечего — и кнопок нет');
    assert.equal(all(s.node, 'proposal__branch-act').length, 0);
  } finally {
    uninstallFakeDom();
  }
});

test('блок «Подзадачи» у родителя: название, стадия, ссылка', () => {
  installFakeDom();
  try {
    const node = buildProjectBlock({ project: PROJECT_PARENT, roomId: 'cafe' });
    assert.ok(node, 'блок есть');
    assert.equal(node.getAttribute('data-project-children'), '2');
    const links = all(node, 'proposal-project__link');
    assert.equal(links.length, 2);
    assert.equal(links[1].getAttribute('href'), '#/room/cafe/request/t_c0ff0101');
    const stages = all(node, 'proposal-project__stage').map((x) => text(x));
    assert.deepEqual(stages, ['В работе', 'Ждёт вас']);
    assert.equal(buildProjectBlock({ project: { children: [] }, roomId: 'cafe' }), null,
      'без подзадач блока нет');
  } finally {
    uninstallFakeDom();
  }
});

test('подзадача: строка «Часть проекта: <родитель>» со ссылкой', () => {
  installFakeDom();
  try {
    const node = buildParentNote({ project: { parent_id: 't_c0ffee03' }, roomId: 'cafe' });
    assert.ok(node);
    assert.match(text(node), /Часть проекта/);
    assert.equal(all(node, 'proposal-parent__link')[0].getAttribute('href'),
      '#/room/cafe/request/t_c0ffee03');
    assert.equal(buildParentNote({ project: { parent_id: null }, roomId: 'cafe' }), null);
  } finally {
    uninstallFakeDom();
  }
});

/* ───────────────────────────────────────────── 5. безопасность разметки */

test('никакого innerHTML из данных: карточка строится только h()', () => {
  const src = readFileSync(join(ROOT, 'web', 'js', 'views', 'proposal-card.js'), 'utf8');
  assert.equal(/innerHTML\s*=/.test(src), false, 'innerHTML не используется');
  assert.equal(/insertAdjacentHTML/.test(src), false);
  assert.match(src, /'\/api\/requests\/' \+ encodeURIComponent\(requestId\) \+ '\/proposal\/'/,
    'единственная точка отправки — маршруты контракта §2');
});
