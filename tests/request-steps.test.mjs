/**
 * request-steps.test.mjs — блок «Следующие шаги» и окно шага (WAVE14, пакет H, п. 12).
 *
 * Проверяем новую модель: шаг — план РУКОВОДИТЕЛЯ, галочек у пользователя нет.
 * Выполненные отделены («Сделано»), предстоящие — «Предстоит» (вкладка «Дальше»
 * переименована в «От вас нужно», поэтому группа шагов называется иначе); клик по шагу
 * открывает окно, где «Согласен / Прокомментировать / Переместить / Убрать»
 * уходят руководителю КАК ОТВЕТ НА ЭТОТ ШАГ и показывают его реакцию.
 *
 * Заменяет tests/request-aside-steps.test.mjs (тот проверял чекбоксы, которых
 * больше нет). DOM — мини-DOM из tests/fake-dom.mjs; браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { createAside } from '../web/js/views/request-aside.js';
import {
  splitSteps, stepState, openStepsCount, isStepDone, STEPS_HINT, stepNotice,
} from '../web/js/views/request-steps.js';
import { movePayload, stepWhy, stepOwner, stepReactionLine } from '../web/js/views/step-dialog.js';

/* ------------------------------------------------ недостающие методы мини-DOM */

function installHelpers() {
  const P = FakeElement.prototype;
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (typeof P.replaceChildren !== 'function') {
    P.replaceChildren = function replaceChildren(...nodes) {
      this.childNodes = [];
      for (const node of nodes) this.append(node);
    };
  }
  if (typeof P.replaceWith !== 'function') {
    P.replaceWith = function replaceWith(node) {
      const parent = this.parentNode;
      if (!parent) return;
      const i = parent.childNodes.indexOf(this);
      if (i === -1) return;
      node.parentNode = parent;
      this.parentNode = null;
      parent.childNodes[i] = node;
    };
  }
  if (!('childElementCount' in P)) {
    Object.defineProperty(P, 'childElementCount', {
      configurable: true,
      get() { return (this.childNodes || []).filter((n) => n.nodeType === 1).length; },
    });
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
  if (typeof P.querySelector !== 'function') {
    P.querySelector = function querySelector(selector) {
      const sel = String(selector);
      const found = sel.startsWith('.')
        ? byClass(this, sel.slice(1))
        : findAll(this, (n) => String(n.tagName || '').toLowerCase() === sel.toLowerCase());
      return found.length ? found[0] : null;
    };
  }
  if (typeof P.querySelectorAll !== 'function') {
    P.querySelectorAll = function querySelectorAll(selector) {
      const sel = String(selector);
      return sel.startsWith('.')
        ? byClass(this, sel.slice(1))
        : findAll(this, (n) => String(n.tagName || '').toLowerCase() === sel.toLowerCase());
    };
  }
}

function findAll(root, pred) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && pred(n)) out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

function byClass(root, cls) {
  return findAll(root, (n) => String(n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

function byAttr(root, name, value) {
  return findAll(root, (n) => n.getAttribute && n.getAttribute(name) === value);
}

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

/* ─────────────────────────────────────────────────────────── данные и стенд */

const STEPS = [
  {
    id: 'ns_1', text: 'Решить по составу команды: добавить team-cfo', owner: 'team-cfo',
    due: '2026-11-07', state: 'open', can_agree: true, can_drop: true, history: [],
  },
  {
    id: 'ns_2', text: 'Проверить аллергены на этикетке', owner: 'team-lawyer',
    state: 'done', can_agree: false, can_drop: false,
    history: [{ at: 1, kind: 'state', label: 'сделано' }],
  },
  {
    id: 'ns_3', text: 'Добавить юриста в команду', state: 'open',
    conflicts_with_answer: true, can_remove: true, can_agree: true, can_drop: true,
    question: { id: 'q_1', text: 'Добавляем ли юриста?' },
    answer: 'Нет, никакого юриста не добавляем',
    user_action: { kind: 'agree', at: 2, label: 'вы согласовали' },
    reaction: null, reaction_pending: true, reaction_label: 'руководитель ещё не ответил',
    history: [{ at: 2, kind: 'agree', label: 'согласовано вами' }],
  },
];

function setup(opts = {}) {
  const doc = installFakeDom();
  installHelpers();
  doc.body = new FakeElement('body');
  const posted = [];
  const toasts = [];
  const detail = JSON.parse(JSON.stringify({
    id: 't_root1',
    next_steps: opts.steps || STEPS,
    open_questions: [], decisions: [], future_questions: [],
  }));
  const state = { refresh: 0 };
  const ctx = {
    api: {
      async post(path, body) {
        posted.push({ path, body });
        if (opts.fail) throw new Error('нет связи');
        return opts.response ? opts.response(body) : {};
      },
      async get(path) {
        posted.push({ path, method: 'GET' });
        if (opts.detail) return opts.detail;
        return detail;
      },
    },
    toast: (message, kind) => toasts.push({ message, kind }),
    room: { id: 'cafe', members: [{ profile: 'team-cfo', role: 'worker' }] },
    navigate() {},
  };
  const R = {
    ctx, rid: 't_root1', keepValues: new Map(), roomId: 'cafe',
    get detail() { return detail; },
    backlog: new Map(),
    async refresh() { state.refresh += 1; },
  };
  const { buildAside } = createAside(R);
  const host = doc.createElement('div');
  host.appendChild(buildAside());
  return { host, posted, toasts, state, doc, detail };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/* ─────────────────────────────────────────────────────────── чистые функции */

test('splitSteps: предстоящие / сделанные / снятые', () => {
  const parts = splitSteps([
    ...STEPS,
    { id: 'ns_4', text: 'снят ответом', state: 'cancelled' },
    { id: 'ns_5', text: 'убран', state: 'dismissed' },
  ]);
  assert.deepEqual(parts.current.map((s) => s.id), ['ns_1', 'ns_3']);
  assert.deepEqual(parts.done.map((s) => s.id), ['ns_2']);
  assert.deepEqual(parts.removed.map((s) => s.id), ['ns_4', 'ns_5']);
  assert.equal(openStepsCount(parts.all), 2);
});

test('stepState: конфликт важнее служебного статуса, состояния по-русски', () => {
  assert.equal(stepState({ state: 'open' }).label, 'предстоит');
  assert.equal(stepState({ state: 'done' }).label, 'сделано');
  assert.equal(stepState({ state: 'dismissed' }).label, 'убран');
  assert.equal(stepState({ state: 'cancelled' }).label, 'снят по вашему ответу');
  const conflict = stepState({ state: 'open', conflicts_with_answer: true });
  assert.equal(conflict.key, 'conflict');
  assert.equal(conflict.label, 'противоречит вашему ответу');
  assert.equal(isStepDone({ state: 'done' }), true);
  assert.equal(isStepDone({ state: 'open' }, { status: 'done' }), true);
});

test('movePayload: отправляем только то, что изменили', () => {
  const step = { id: 'ns_1', due: '2026-11-07', owner: 'team-cfo' };
  assert.equal(movePayload({ due: '2026-11-07', owner: 'team-cfo' }, step), null);
  assert.deepEqual(movePayload({ due: '2026-12-01' }, step),
    { action: 'step_move', step_id: 'ns_1', due: '2026-12-01' });
  assert.deepEqual(movePayload({ owner: 'team-lawyer' }, step),
    { action: 'step_move', step_id: 'ns_1', owner: 'team-lawyer' });
  assert.deepEqual(movePayload({ direction: 'up' }, step),
    { action: 'step_move', step_id: 'ns_1', direction: 'up' });
});

test('stepWhy / stepOwner / stepReactionLine', () => {
  const why = stepWhy(STEPS[2]);
  assert.equal(why.question.id, 'q_1');
  assert.equal(why.answer, 'Нет, никакого юриста не добавляем');
  assert.match(stepWhy({}).text, /источник/);

  assert.equal(stepOwner({ owner: 'team-cfo' }, { members: [{ profile: 'team-cfo', role: 'worker' }] }).profile, 'team-cfo');
  assert.equal(stepOwner({}).title, 'не назначен');
  assert.equal(stepOwner({ owner: 'mikhail' }).title, 'Михаил');

  assert.equal(stepReactionLine(STEPS[0]), null);
  const pending = stepReactionLine(STEPS[2]);
  assert.equal(pending.kind, 'pending');
  assert.equal(pending.label, 'руководитель ещё не ответил');
  const reacted = stepReactionLine({
    reaction: { text: 'Принято', cards: ['t_e9c6c2b2'] }, reaction_label: 'принято',
  });
  assert.equal(reacted.label, 'принято');
  assert.deepEqual(reacted.cards, ['t_e9c6c2b2']);
});

/* ───────────────────────────────────────────── блок «Следующие шаги» в колонке */

test('в блоке нет чекбоксов, есть подпись «план руководителя» и группы', () => {
  const s = setup();
  try {
    const card = byClass(s.host, 'req-steps')[0];
    assert.ok(card, 'блок шагов отрисован');
    assert.equal(findAll(card, (n) => String(n.tagName).toLowerCase() === 'input').length, 0,
      'у пользователя нет галочек');
    assert.match(text(card), new RegExp(STEPS_HINT.slice(0, 24)));
    assert.match(text(card), /Предстоит/);
    const doneFold = byClass(card, 'req-steps__fold--done')[0];
    assert.ok(doneFold, 'выполненные шаги отделены');
    assert.match(text(doneFold), /Проверить аллергены/);
    assert.equal(byClass(card, 'req-step--done').length, 1);
  } finally {
    uninstallFakeDom();
  }
});

test('шаг открывается кликом и с клавиатуры (role=button)', () => {
  const s = setup();
  try {
    const opener = byClass(s.host, 'req-step__open')[0];
    assert.equal(opener.getAttribute('role'), 'button');
    assert.equal(opener.getAttribute('tabindex'), '0');
    assert.equal(opener.getAttribute('aria-haspopup'), 'dialog');
    opener.dispatch('keydown', { key: 'Escape' }); // не открывает
    assert.equal(byClass(s.doc.body, 'stepdlg').length, 0);
    opener.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    assert.equal(byClass(s.doc.body, 'stepdlg').length, 1, 'окно шага открылось с клавиатуры');
    byClass(s.doc.body, 'stepdlg__close')[0].dispatch('click');
    assert.equal(byClass(s.doc.body, 'stepdlg').length, 0, 'окно закрылось');
  } finally {
    uninstallFakeDom();
  }
});

test('конфликтный шаг: метка «противоречит вашему ответу» и кнопка «Убрать»', async () => {
  const s = setup();
  try {
    const conflict = byClass(s.host, 'req-step--conflict')[0];
    assert.ok(conflict, 'конфликтный шаг помечен');
    assert.match(text(conflict), /противоречит вашему ответу/);
    const remove = byClass(conflict, 'req-step__remove')[0];
    assert.ok(remove, 'кнопка «Убрать» есть');
    remove.dispatch('click', { stopPropagation() {} });
    await tick();
    await tick();
    const sent = s.posted.filter((p) => p.body && p.body.action === 'step_drop');
    assert.equal(sent.length, 1, JSON.stringify(s.posted));
    assert.deepEqual(sent[0].body, { action: 'step_drop', step_id: 'ns_3' });
    assert.equal(s.state.refresh, 1);
    assert.match(s.toasts[0].message, /Шаг убран/);
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────────────────────────────────────── окно шага */

test('окно шага: «Согласен» уходит ответом на шаг и показывает реакцию', async () => {
  const s = setup({
    response: () => ({
      ok: true, step: {
        ...STEPS[0], user_action: { kind: 'agree', at: 5, label: 'вы согласовали' },
        reaction_pending: true, reaction_label: 'руководитель ещё не ответил',
        history: [{ at: 5, kind: 'agree', label: 'согласовано вами' }],
      },
      reaction_label: 'руководитель ещё не ответил',
    }),
  });
  try {
    byClass(s.host, 'req-step__open')[0].dispatch('click');
    const dialog = byClass(s.doc.body, 'stepdlg')[0];
    assert.ok(dialog, 'окно открыто');
    assert.match(text(dialog), /Шаг плана/);
    assert.match(text(dialog), /Зачем нужен/);
    const agree = byAttr(dialog, 'data-step-action', 'agree')[0];
    assert.ok(agree, 'кнопка «Согласен»');
    agree.dispatch('click');
    await tick();
    await tick();
    const sent = s.posted.filter((p) => p.body && p.body.action);
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0].body, { action: 'step_agree', step_id: 'ns_1' });
    assert.match(sent[0].path, /\/api\/requests\/t_root1\/actions$/);
    assert.match(text(s.doc.body), /руководитель ещё не ответил/);
    assert.equal(s.state.refresh, 1, 'страница перечитала данные');
  } finally {
    uninstallFakeDom();
  }
});

test('окно шага: комментарий (текст уходит), перемещение и «Убрать»', async () => {
  const s = setup();
  try {
    byClass(s.host, 'req-step__open')[0].dispatch('click');
    let dialog = byClass(s.doc.body, 'stepdlg')[0];
    byAttr(dialog, 'data-step-action', 'comment')[0].dispatch('click');
    const ta = byClass(dialog, 'stepdlg__comment')[0];
    ta.value = 'Сначала уточните страну';
    byAttr(dialog, 'data-step-send', 'comment')[0].dispatch('click');
    await tick();
    await tick();
    let sent = s.posted.filter((p) => p.body && p.body.action);
    assert.deepEqual(sent[0].body,
      { action: 'step_comment', step_id: 'ns_1', text: 'Сначала уточните страну' });

    // «Переместить»: новый срок
    byClass(dialog, 'stepdlg__close')[0].dispatch('click');
    byClass(s.host, 'req-step__open')[0].dispatch('click');
    dialog = byClass(s.doc.body, 'stepdlg')[0];
    byAttr(dialog, 'data-step-action', 'move')[0].dispatch('click');
    const due = findAll(dialog, (n) => String(n.tagName).toLowerCase() === 'input'
      && n.getAttribute('type') === 'date')[0];
    assert.ok(due, 'поле срока в «Переместить»');
    due.value = '2026-12-01';
    byAttr(dialog, 'data-step-send', 'move')[0].dispatch('click');
    await tick();
    await tick();
    sent = s.posted.filter((p) => p.body && p.body.action);
    assert.deepEqual(sent[1].body, { action: 'step_move', step_id: 'ns_1', due: '2026-12-01' });

    // «Убрать» — двухшаговое подтверждение
    byAttr(dialog, 'data-step-action', 'drop')[0].dispatch('click');
    const confirm = byAttr(dialog, 'data-step-confirm', 'drop')[0];
    assert.match(text(confirm), /Не согласен/);
    confirm.dispatch('click');
    assert.match(text(confirm), /Точно убрать/);
    confirm.dispatch('click');
    await tick();
    await tick();
    sent = s.posted.filter((p) => p.body && p.body.action);
    assert.deepEqual(sent[2].body, { action: 'step_drop', step_id: 'ns_1' });
  } finally {
    uninstallFakeDom();
  }
});

test('ошибка сети: тост «danger», окно остаётся открытым', async () => {
  const s = setup({ fail: true });
  try {
    byClass(s.host, 'req-step__open')[0].dispatch('click');
    const dialog = byClass(s.doc.body, 'stepdlg')[0];
    byAttr(dialog, 'data-step-action', 'agree')[0].dispatch('click');
    await tick();
    await tick();
    assert.equal(byClass(s.doc.body, 'stepdlg').length, 1, 'окно не закрылось');
    const last = s.toasts[s.toasts.length - 1];
    assert.equal(last.kind, 'danger');
    assert.match(last.message, /нет связи/);
    assert.equal(s.state.refresh, 0);
  } finally {
    uninstallFakeDom();
  }
});

test('Escape закрывает окно шага', () => {
  const s = setup();
  try {
    byClass(s.host, 'req-step__open')[0].dispatch('click');
    const overlay = byClass(s.doc.body, 'stepdlg-backdrop')[0];
    assert.ok(overlay);
    overlay.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
    assert.equal(byClass(s.doc.body, 'stepdlg').length, 0);
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────── W21-L1: строка «Шаг учтён» (недоделка U4) ───────── */

test('stepNotice: «Шаг учтён» только после конечного исхода, без слова «учтено»', () => {
  const at = 1762900000;
  const applied = stepNotice({
    reaction: { status: 'applied', at, text: 'Работа пересмотрена: карточки t_e9c6c2b2' },
    reaction_label: 'учтено: работа изменена',
  });
  assert.ok(applied, 'после «учтено» строка должна быть');
  assert.match(applied.notice, /^Шаг учтён( · \d\d:\d\d)?$/);
  assert.equal(applied.rest, 'работа изменена', 'второго «учтено» в подписи нет');

  const noChange = stepNotice({
    reaction: { status: 'no_change', at }, reaction_label: 'учтено: менять не нужно',
  });
  assert.match(noChange.notice, /^Шаг учтён/);
  assert.equal(noChange.rest, 'менять не нужно');

  // Пока руководитель не ответил или только прочитал — «учтён» было бы неправдой.
  assert.equal(stepNotice({ reaction: null, reaction_pending: true }), null);
  assert.equal(stepNotice({ reaction: { status: 'received', at } }), null);
  assert.equal(stepNotice({ reaction: { status: 'sent', at } }), null);
  assert.equal(stepNotice({}), null);
  // «шаг убран» / «срок изменён» — своя подпись, строку «учтён» не добавляем
  assert.equal(stepNotice({ reaction: { status: 'removed', at } }), null);
});

test('в строке шага видно «Шаг учтён», а подпись реакции не повторяет «учтено»', () => {
  const at = 1762900000;
  const steps = [{
    id: 'ns_1', text: 'Согласовать упаковку с поставщиком', owner: 'team-cfo',
    state: 'open', can_agree: true, can_drop: true,
    user_action: { kind: 'agree', at: at - 600, label: 'вы согласовали' },
    reaction: { status: 'applied', at, text: 'Работа пересмотрена: карточки t_e9c6c2b2',
      cards: ['t_e9c6c2b2'] },
    reaction_label: 'учтено: работа изменена',
    history: [],
  }];
  const s = setup({ steps });
  try {
    const row = byClass(s.host, 'req-step')[0];
    const notices = byClass(row, 'req-step__notice');
    assert.equal(notices.length, 1, 'строка «Шаг учтён» одна');
    assert.match(text(notices[0]), /^Шаг учтён/);
    assert.equal(byClass(notices[0], 'req-step__reaction-label').length, 1);
    const labels = byClass(row, 'req-step__reaction-label').map(text).join(' | ');
    assert.match(labels, /работа изменена/);
    assert.doesNotMatch(labels, /учтено/i, 'слово «учтено» не дублируется');
    // реакция руководителя по-прежнему видна (его слова и карточки)
    assert.match(text(row), /Работа пересмотрена/);
  } finally {
    uninstallFakeDom();
  }
});
