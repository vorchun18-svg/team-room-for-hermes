/**
 * inbox-ui.test.mjs — «Входящие» и полоса «Надиктовать» (W26, пакет D9).
 *
 *  1) экран «Входящие» показывает записи с источником («диктовка» / «вручную»),
 *     временем и действиями; предложение руководителя читается как
 *     «Предлагаю оформить так: … [Принять][Изменить][Отклонить]»;
 *  2) кнопки реально ходят на сервер: «Принять» → POST …/actions {action:accept},
 *     «Удалить» → delete, у удалённой записи есть «Вернуть» (без тупиков);
 *  3) полоса «Надиктовать» видна только в комнате, пишет во входящие с честным
 *     источником (микрофон → dictation) и обновляет общий счётчик «Входящих»;
 *  4) вкладка «Входящие» есть в маршрутизаторе и в словаре подписей.
 *
 * DOM — tests/fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { mount } from '../web/js/views/inbox.js';
import { installQuickCapture, captureBadge, captureCount, markDictated } from '../web/js/quick-capture.js';
import { TABS, TAB_IDS } from '../web/js/router.js';
import { TAB_TITLES } from '../web/js/text-ru.js';

/* ────────────────────────────────────────────── доработка мини-DOM */

if (typeof FakeElement.prototype.append !== 'function') {
  FakeElement.prototype.append = function append(...nodes) {
    for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
  };
}

if (typeof FakeElement.prototype.replaceChildren !== 'function') {
  FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
    this.childNodes = [];
    for (const node of nodes) if (node) this.appendChild(node);
  };
}

if (typeof FakeElement.prototype.insertBefore !== 'function') {
  FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const idx = ref ? this.childNodes.indexOf(ref) : -1;
    if (idx === -1) this.childNodes.push(node);
    else this.childNodes.splice(idx, 0, node);
    node.parentNode = this;
    return node;
  };
}

if (typeof FakeElement.prototype.removeChild !== 'function') {
  FakeElement.prototype.removeChild = function removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
    return node;
  };
}

if (typeof FakeElement.prototype.remove !== 'function') {
  FakeElement.prototype.remove = function remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  };
}

if (typeof FakeElement.prototype.blur !== 'function') {
  FakeElement.prototype.blur = function blur() { /* в мини-DOM фокуса нет */ };
}

function walk(node, out = []) {
  for (const child of node.childNodes || []) {
    if (!child || child.nodeType !== 1) continue;
    out.push(child);
    walk(child, out);
  }
  return out;
}

function byAttr(node, attr, value) {
  return walk(node).filter((n) => n.getAttribute && n.getAttribute(attr) === String(value));
}

function actionsOf(node) {
  return walk(node)
    .filter((n) => n.getAttribute && n.getAttribute('data-inbox-act'))
    .map((n) => n.getAttribute('data-inbox-act'));
}

function textOf(node) {
  return walk(node).map((n) => n.textContent).join(' ');
}

/* ────────────────────────────────────────────── данные

Форма записи — та, что отдаёт server/inbox.py (поля и подписи кнопок). */

const ITEM_NEW = {
  id: 'in_aaaa1111', room: 'r1',
  text: 'Подготовить письмо клиенту по договору',
  source: 'dictation', source_title: 'диктовка',
  created_at: new Date(Date.now() - 3600e3).toISOString(), created_hhmm: '09:15',
  status: 'new', kind: null, proposal: null, resolution: null, note: '',
  actions: [
    { id: 'propose', label: 'Разобрать руководителем' },
    { id: 'to_delo', label: 'Дело' },
    { id: 'to_step', label: 'Шаг дела' },
    { id: 'to_question', label: 'Вопрос руководителю' },
    { id: 'to_duty', label: 'Дежурство' },
    { id: 'to_note', label: 'Справка' },
    { id: 'delete', label: 'Удалить' },
  ],
};

const ITEM_WAITING = {
  id: 'in_bbbb2222', room: 'r1',
  text: 'каждую пятницу присылать обзор почты',
  source: 'manual', source_title: 'вручную',
  created_at: new Date(Date.now() - 600e3).toISOString(), created_hhmm: '10:05',
  status: 'processing', kind: 'duty',
  proposal: {
    id: 'pr_1', at: new Date().toISOString(), kind: 'duty', kind_title: 'Дежурство',
    title: 'каждую пятницу присылать обзор почты',
    body: 'Дежурство: «каждую пятницу присылать обзор почты». Что делать: …',
    state: 'waiting', by: 'шаблон',
    actions: [{ id: 'accept', label: 'Принять' }, { id: 'edit', label: 'Изменить' }, { id: 'reject', label: 'Отклонить' }],
  },
  resolution: null, note: 'Разбор: Дежурство. Проверьте предложение и примите.',
  actions: [
    { id: 'accept', label: 'Принять' }, { id: 'edit', label: 'Изменить' },
    { id: 'reject', label: 'Отклонить' }, { id: 'delete', label: 'Удалить' },
  ],
};

const ITEM_DELETED = {
  id: 'in_cccc3333', room: 'r1', text: 'старая мысль', source: 'manual',
  source_title: 'вручную', created_at: new Date(Date.now() - 7200e3).toISOString(),
  created_hhmm: '08:10', status: 'deleted', kind: null, proposal: null,
  resolution: null, note: 'Удалено — можно вернуть.', actions: [{ id: 'restore', label: 'Вернуть' }],
};

function listing(items, counts) {
  return {
    room: 'r1', generated_at: new Date().toISOString(), filter: 'active',
    counts: counts || { new: 1, processing: 1, processed: 0, deleted: 1, total: 3, active: 2, proposals: 1 },
    items, notes: [], queue: [{ id: 'mq_1', item_id: 'in_bbbb2222', text: 'Разобрать входящее: …', at: new Date().toISOString(), state: 'waiting' }],
    updated_at: null, warning: '',
  };
}

function fakeApi(seed, onPost) {
  const calls = [];
  return {
    calls,
    async get(path) { calls.push(['get', path]); return seed; },
    async post(path, body) {
      calls.push(['post', path, body]);
      if (typeof onPost === 'function') return onPost(path, body);
      return { ok: true, item: seed.items[0], counts: seed.counts };
    },
  };
}

function newEl(tag = 'div') { return new FakeElement(tag); }

/* ────────────────────────────────────────────── экран «Входящие» */

test('входящие: записи, источник, предложение и кнопки на месте', async () => {
  installFakeDom();
  try {
    const api = fakeApi(listing([ITEM_WAITING, ITEM_NEW]));
    const el = newEl();
    await mount(el, { route: { name: 'room', roomId: 'r1' }, api, toast() {}, navigate() {} });

    const text = textOf(el);
    assert.match(text, /Входящие комнаты/, 'заголовок экрана');
    assert.match(text, /Подготовить письмо клиенту по договору/, 'текст записи');
    assert.match(text, /диктовка/, 'источник «диктовка» подписан честно');
    assert.match(text, /вручную/, 'источник «вручную» подписан честно');
    assert.match(text, /Предлагаю оформить так:/, 'шаблон предложения виден');
    assert.match(text, /Дежурство/, 'вид предложения виден');

    // У записи с предложением — ровно три кнопки решения, без дублей «Разобрать».
    const waiting = byAttr(el, 'data-inbox-item', 'in_bbbb2222')[0];
    assert.ok(waiting, 'карточка записи нарисована');
    const waitingActions = actionsOf(waiting);
    assert.deepEqual(waitingActions, ['accept', 'edit', 'reject', 'delete']);

    // У новой записи — ручной разбор и «Разобрать руководителем».
    const fresh = byAttr(el, 'data-inbox-item', 'in_aaaa1111')[0];
    const freshActions = actionsOf(fresh);
    assert.equal(freshActions[0], 'propose');
    for (const id of ['to_delo', 'to_step', 'to_question', 'to_duty', 'to_note', 'delete']) {
      assert.ok(freshActions.includes(id), 'нет кнопки ' + id);
    }

    // Очередь руководителя — свёрнутый блок с карточкой.
    assert.match(text, /В очереди у руководителя: 1/);
  } finally {
    uninstallFakeDom();
  }
});

test('входящие: «Принять» уходит на сервер и карточка обновляется', async () => {
  installFakeDom();
  try {
    const accepted = Object.assign({}, ITEM_WAITING, {
      status: 'processed',
      proposal: Object.assign({}, ITEM_WAITING.proposal, { state: 'accepted' }),
      resolution: { action: 'accept', kind: 'duty', kind_title: 'Дежурство', title: 'обзор почты' },
      actions: [{ id: 'restore', label: 'Вернуть в работу' }, { id: 'delete', label: 'Удалить' }],
    });
    const api = fakeApi(listing([ITEM_WAITING, ITEM_NEW]), () => ({
      ok: true, item: accepted,
      counts: { new: 1, processing: 0, processed: 1, deleted: 1, total: 3, active: 1, proposals: 0 },
    }));
    const el = newEl();
    await mount(el, { route: { name: 'room', roomId: 'r1' }, api, toast() {}, navigate() {} });

    const accept = byAttr(el, 'data-inbox-act', 'accept')[0];
    assert.ok(accept, 'кнопка «Принять» есть');
    assert.equal(accept.textContent.includes('Принять'), true);
    accept.dispatch('click');
    await new Promise((resolve) => setImmediate(resolve));

    const post = api.calls.find((c) => c[0] === 'post');
    assert.ok(post, 'запрос ушёл');
    assert.equal(post[1], '/api/rooms/r1/inbox/in_bbbb2222/actions');
    assert.equal(post[2].action, 'accept');

    const card = byAttr(el, 'data-inbox-item', 'in_bbbb2222')[0];
    assert.match(textOf(card), /Разобрано: Дежурство/, 'после приёма видно «Разобрано»');
    assert.deepEqual(actionsOf(card), ['restore', 'delete'], 'у разобранной записи есть действия');
  } finally {
    uninstallFakeDom();
  }
});

test('входящие: удалённая запись предлагает «Вернуть» (без тупиков)', async () => {
  installFakeDom();
  try {
    const api = fakeApi(listing([ITEM_DELETED], {
      new: 0, processing: 0, processed: 0, deleted: 1, total: 1, active: 0, proposals: 0,
    }));
    const el = newEl();
    await mount(el, { route: { name: 'room', roomId: 'r1' }, api, toast() {}, navigate() {} });
    // В фильтре «Новые» удалённых нет — переключаемся на «Все».
    const allFilter = walk(el).find((n) => n.getAttribute && n.getAttribute('data-value') === 'all');
    assert.ok(allFilter, 'фильтр «Все» есть');
    allFilter.dispatch('click');
    await new Promise((resolve) => setImmediate(resolve));

    const card = byAttr(el, 'data-inbox-item', 'in_cccc3333')[0];
    assert.ok(card, 'удалённая запись видна во «Всех»');
    assert.deepEqual(actionsOf(card), ['restore']);
    assert.match(textOf(card), /Вернуть/);

    api.calls.length = 0;
    byAttr(card, 'data-inbox-act', 'restore')[0].dispatch('click');
    await new Promise((resolve) => setImmediate(resolve));
    const post = api.calls.find((c) => c[0] === 'post');
    assert.equal(post[2].action, 'restore');
  } finally {
    uninstallFakeDom();
  }
});

test('входящие: пустой список подсказывает, как записать', async () => {
  installFakeDom();
  try {
    const api = fakeApi(listing([], {
      new: 0, processing: 0, processed: 0, deleted: 0, total: 0, active: 0, proposals: 0,
    }));
    const el = newEl();
    await mount(el, { route: { name: 'room', roomId: 'r1' }, api, toast() {}, navigate() {} });
    const text = textOf(el);
    assert.match(text, /Новых записей нет/);
    assert.match(text, /клавишу N/, 'пустой экран объясняет, что делать (без тупиков)');
  } finally {
    uninstallFakeDom();
  }
});

/* ────────────────────────────────────────────── полоса «Надиктовать» */

function fakeHost() {
  return { app: newEl(), host: newEl(), toasts: [] };
}

test('полоса записи: видна в комнате, скрыта на «Сегодня»', () => {
  installFakeDom();
  try {
    const { app, host } = fakeHost();
    const capture = installQuickCapture({ app, host, api: fakeApi(listing([], null)), toast() {}, navigate() {} });
    capture.sync({ name: 'today' });
    assert.equal(/hidden/.test(capture.bar.getAttribute('class')), true, 'на «Сегодня» полосы нет');
    capture.sync({ name: 'room', roomId: 'r1' });
    assert.equal(/hidden/.test(capture.bar.getAttribute('class')), false, 'в комнате полоса видна');
    capture.destroy();
  } finally {
    uninstallFakeDom();
  }
});

test('полоса записи: текст уходит во входящие и обновляет счётчик', async () => {
  installFakeDom();
  try {
    const { app, host } = fakeHost();
    const posted = [];
    const api = {
      async get() { return { counts: { active: 0 } }; },
      async post(path, body) {
        posted.push([path, body]);
        return { ok: true, item: { id: 'in_new1', text: body.text }, counts: { active: 3 } };
      },
    };
    const capture = installQuickCapture({ app, host, api, toast() {}, navigate() {} });
    capture.sync({ name: 'room', roomId: 'r1' });

    capture.field.value = '  Позвонить в банк и уточнить лимит  ';
    capture.bar.querySelector = undefined;
    byAttr(capture.bar, 'data-capture-save', 'true')[0].dispatch('click');
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(posted.length, 1);
    assert.equal(posted[0][0], '/api/rooms/r1/inbox');
    assert.deepEqual(posted[0][1], { text: 'Позвонить в банк и уточнить лимит', source: 'manual' });
    assert.equal(capture.field.value, '', 'поле очищено после записи');
    assert.equal(captureCount('r1'), 3, 'счётчик обновлён ответом сервера');

    // Бейдж вкладки получает то же число (один счётчик — все места).
    const badge = captureBadge('r1');
    assert.equal(badge.textContent, '3');
    capture.destroy();
  } finally {
    uninstallFakeDom();
  }
});

test('полоса записи: источник «диктовка», если был микрофон', async () => {
  installFakeDom();
  try {
    const { app, host } = fakeHost();
    const posted = [];
    const api = {
      async get() { return { counts: { active: 0 } }; },
      async post(path, body) { posted.push(body); return { ok: true, counts: { active: 1 } }; },
    };
    const capture = installQuickCapture({ app, host, api, toast() {}, navigate() {} });
    capture.sync({ name: 'room', roomId: 'r1' });
    markDictated(capture.state);                 // то, что делает onInsert микрофона
    capture.field.value = 'надиктованная мысль';
    await capture.submit();
    assert.equal(posted[0].source, 'dictation');
    assert.equal(capture.state.dictated, false, 'после записи пометка сбрасывается');
    capture.destroy();
  } finally {
    uninstallFakeDom();
  }
});

test('полоса записи: пустой текст не отправляется', async () => {
  installFakeDom();
  try {
    const { app, host } = fakeHost();
    let posted = 0;
    const api = { async get() { return { counts: { active: 0 } }; }, async post() { posted += 1; } };
    const capture = installQuickCapture({ app, host, api, toast() {}, navigate() {} });
    capture.sync({ name: 'room', roomId: 'r1' });
    capture.field.value = '   ';
    const ok = await capture.submit();
    assert.equal(ok, false);
    assert.equal(posted, 0, 'пустая запись не уходит на сервер');
    capture.destroy();
  } finally {
    uninstallFakeDom();
  }
});

/* ────────────────────────────────────────────── вкладка */

test('вкладка «Входящие»: в маршрутах и в словаре подписей', () => {
  const tab = TABS.find((t) => t.id === 'inbox');
  assert.ok(tab, 'вкладка объявлена в router.js TABS');
  assert.equal(tab.title, 'Входящие');
  assert.equal(tab.icon, 'inbox');
  assert.ok(TAB_IDS.includes('inbox'), 'маршрут #/room/<id>/inbox разбирается');
  assert.equal(TAB_TITLES.inbox, 'Входящие', 'подпись — один источник в text-ru.js');
});
