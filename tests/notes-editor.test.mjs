/**
 * notes-editor.test.mjs — окно-«блокнот» «Особенности для этой комнаты» (WAVE17 P6).
 *
 * Проверяем без браузера (мини-DOM tests/fake-dom.mjs):
 *  • подписи: заголовок «…— имя и должность», подзаголовок про SOUL.md;
 *  • счётчик «N из 24 записей · M из 8000 знаков» и предупреждение у лимита;
 *  • поиск и фильтр «все / ваши / руководителя»;
 *  • открытие окна: список записей, редактор выбранной записи на всю высоту;
 *  • сохранение правки (PATCH) и новой записи (POST), Ctrl+S;
 *  • несохранённые изменения не теряются: закрытие просит подтверждение;
 *  • ссылка «Особенности (N)» открывает окно и обновляет счётчик после правки.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';
import {
  openNotesEditor, notesLink, editorTitle, filterEntries, limitsText, limitsState,
  metaLine, sourceRu, previewText, EDITOR_SUBTITLE,
} from '../web/js/notes-editor.js';

/* ── доработка мини-DOM (как в других тестах пакета) ───────────────────── */

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
FakeElement.prototype.removeChild = function removeChild(node) {
  const idx = this.childNodes.indexOf(node);
  if (idx !== -1) this.childNodes.splice(idx, 1);
  node.parentNode = null;
  return node;
};
FakeElement.prototype.focus = function focus() {};
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.closest = function closest() { return null; };

function matches(el, selector) {
  const parts = String(selector).split(',').map((one) => one.trim()).filter(Boolean);
  return parts.some((sel) => {
    if (sel.startsWith('.')) {
      const need = sel.split('.').filter(Boolean);
      const have = String(el.className || '').split(/\s+/).filter(Boolean);
      return need.every((cls) => have.includes(cls));
    }
    return String(el.tagName || '').toLowerCase() === sel.toLowerCase();
  });
}

FakeElement.prototype.querySelectorAll = function querySelectorAll(selector) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (matches(child, selector)) out.push(child);
      walk(child);
    }
  };
  walk(this);
  return out;
};
FakeElement.prototype.querySelector = function querySelector(selector) {
  return this.querySelectorAll(selector)[0] || null;
};

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  doc.activeElement = null;
  doc.listeners = Object.create(null);
  doc.addEventListener = (type, fn) => { (doc.listeners[type] || (doc.listeners[type] = [])).push(fn); };
  doc.removeEventListener = (type, fn) => {
    const list = doc.listeners[type] || [];
    const idx = list.indexOf(fn);
    if (idx !== -1) list.splice(idx, 1);
  };
  doc.fire = (type, event) => { for (const fn of [...(doc.listeners[type] || [])]) fn(event); };
  doc.querySelectorAll = (selector) => {
    const out = [];
    const walk = (node) => {
      for (const child of node.childNodes || []) {
        if (child.nodeType !== 1) continue;
        if (matches(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(doc.body);
    return out;
  };
  doc.getElementById = () => null;
  globalThis.document = doc;
  return doc;
}

function uninstallDom() {
  delete globalThis.document;
}

function byClass(root, cls) {
  return root.querySelectorAll('.' + cls);
}

function byText(root, text) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (String(child.textContent || '').trim() === text) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

/* ── фальшивый API ─────────────────────────────────────────────────────── */

function makeApi(initial) {
  const state = {
    entries: (initial || []).map((one) => Object.assign({}, one)),
    calls: [],
    journals: [{ id: 'j_1', at: 1700000000, action: 'add', profile: 'biz-lawyer',
      text: 'Первая запись', source: 'user', why: '', rolled_back: false }],
  };
  let seq = 0;
  const limits = { max_entries: 24, max_text: 8000, review_every: 5 };
  const payload = () => ({
    ok: true, profile: 'biz-lawyer', entries: state.entries.map((one) => Object.assign({}, one)),
    limits, text: state.entries.map((one) => one.text).join('\n'),
  });
  const api = {
    state,
    async get(path) {
      state.calls.push(['GET', path]);
      if (path.includes('/notes/journal')) return { ok: true, events: state.journals };
      if (path.endsWith('/notes')) return payload();
      throw new Error('unexpected GET ' + path);
    },
    async post(path, body) {
      state.calls.push(['POST', path, body]);
      if (path.endsWith('/notes/entries')) {
        seq += 1;
        const entry = { id: 'n_' + seq, text: body.text, source: body.source || 'user',
          origin: body.origin || '', created_at: 1700000100 + seq, used_count: 0, pinned: false };
        state.entries.push(entry);
        return Object.assign(payload(), { entry });
      }
      if (path.endsWith('/pin')) {
        const id = path.split('/').slice(-2)[0];
        const entry = state.entries.find((one) => one.id === id);
        if (entry) entry.pinned = Boolean(body.pinned);
        return Object.assign(payload(), { entry });
      }
      if (path.endsWith('/notes/review')) {
        return Object.assign(payload(), { before: state.entries.length, after: state.entries.length,
          drops: [], merges: [], applied: false });
      }
      throw new Error('unexpected POST ' + path);
    },
    async patch(path, body) {
      state.calls.push(['PATCH', path, body]);
      const id = path.split('/').pop();
      const entry = state.entries.find((one) => one.id === id);
      if (entry) entry.text = body.text;
      return Object.assign(payload(), { entry });
    },
    async del(path) {
      state.calls.push(['DELETE', path]);
      const id = path.split('/').slice(-1)[0].split('?')[0];
      state.entries = state.entries.filter((one) => one.id !== id);
      return payload();
    },
  };
  return api;
}

const ENTRIES = [
  { id: 'n_one', text: 'Всегда проверяй договор по закону X и указывай сроки в днях.',
    source: 'user', origin: 'наблюдение', created_at: 1700000000, used_count: 2, pinned: true },
  { id: 'n_two', text: 'Ornith слабая модель, но локальная — простые задачи поручать ей.',
    source: 'leader', origin: '', created_at: 1700001000, used_count: 0, pinned: false },
];

/* ── чистые функции ────────────────────────────────────────────────────── */

test('заголовок и подзаголовок окна — дословно из ТЗ', () => {
  assert.equal(editorTitle('Анна · финансы и экономика (CFO)'),
    'Особенности для этой комнаты — Анна · финансы и экономика (CFO)');
  assert.equal(editorTitle(''), 'Особенности для этой комнаты');
  assert.match(EDITOR_SUBTITLE, /^Добавка к SOUL\.md только для этой комнаты\./);
  assert.match(EDITOR_SUBTITLE, /Профиль Hermes не меняется/);
});

test('счётчик лимитов: N из 24 записей · M из 8000 знаков', () => {
  const text = limitsText(ENTRIES, { max_entries: 24, max_text: 8000 });
  assert.match(text, /^2 из 24 записей · \d+ из 8000 знаков$/);
  assert.equal(limitsState(ENTRIES, { max_entries: 24, max_text: 8000 }), '');
  const almost = Array.from({ length: 23 }, (_, i) => ({ text: 'п' + i }));
  assert.equal(limitsState(almost, { max_entries: 24, max_text: 8000 }), 'warn');
  const over = Array.from({ length: 25 }, () => ({ text: 'п' }));
  assert.equal(limitsState(over, { max_entries: 24, max_text: 8000 }), 'over');
  assert.equal(limitsState([{ text: 'x'.repeat(8001) }], { max_entries: 24, max_text: 8000 }), 'over');
});

test('фильтр «все / ваши / руководителя» и поиск', () => {
  assert.equal(filterEntries(ENTRIES, '', 'all').length, 2);
  assert.equal(filterEntries(ENTRIES, '', 'user').length, 1);
  assert.equal(filterEntries(ENTRIES, '', 'leader').length, 1);
  assert.equal(filterEntries(ENTRIES, 'ornith', 'all').length, 1);
  assert.equal(filterEntries(ENTRIES, 'ornith', 'user').length, 0);
  assert.equal(filterEntries(ENTRIES, 'ЗАКОНУ', 'all').length, 1);
  assert.equal(filterEntries(ENTRIES, 'нет-такого', 'all').length, 0);
});

test('подпись записи: кто добавил, когда, сколько раз пригодилась', () => {
  assert.equal(sourceRu({ source: 'user' }), 'вы');
  assert.equal(sourceRu({ source: 'leader' }), 'руководитель');
  assert.equal(sourceRu({}), 'руководитель');
  const line = metaLine(ENTRIES[0]);
  assert.match(line, /^вы · \d\d\.\d\d/);
  assert.match(line, /пригодилось 2 раза/);
  assert.equal(previewText('a'.repeat(300)).length, 220);
  assert.match(previewText('короткий текст'), /^короткий текст$/);
});

/* ── ссылка и окно ─────────────────────────────────────────────────────── */

test('ссылка «Особенности (N)» открывает окно и обновляет счётчик', async () => {
  const doc = installDom();
  const api = makeApi(ENTRIES);
  try {
    const link = notesLink({ api }, {
      roomId: 'cafe', profile: 'biz-lawyer', label: 'Юрист · договоры',
      room: { id: 'cafe', agent_notes: { 'biz-lawyer': { entries: ENTRIES } } },
    });
    assert.equal(String(link.textContent).trim(), 'Особенности (2)');
    doc.body.appendChild(link);
    link.dispatch('click');
    await new Promise((resolve) => setTimeout(resolve, 10));   // окно дочитывает записи
    const dialogs = byClass(doc.body, 'notes-ed');
    assert.equal(dialogs.length, 1, 'окно открылось');
    const title = byClass(dialogs[0], 'notes-ed__title')[0];
    assert.match(String(title.textContent), /^Особенности для этой комнаты — Юрист · договоры$/);
  } finally {
    uninstallDom();
  }
});

test('окно: список записей, редактор выбранной записи, счётчик лимитов', async () => {
  const doc = installDom();
  const api = makeApi(ENTRIES);
  try {
    const ui = openNotesEditor({ ctx: { api }, roomId: 'cafe', profile: 'biz-lawyer', label: 'Юрист' });
    await ui.loaded;

    const dialog = ui.element;
    assert.ok(dialog, 'каркас окна есть');
    assert.equal(dialog.getAttribute('aria-modal'), 'true');
    assert.ok(String(byClass(dialog, 'notes-ed__sub')[0].textContent).includes('Профиль Hermes не меняется'));

    const items = byClass(dialog, 'notes-ed__item');
    assert.equal(items.length, 2, 'в списке обе записи');
    assert.ok(items[0].className.includes('notes-ed__item--on'), 'первая выбрана');
    assert.ok(items[0].className.includes('notes-ed__item--pinned'), 'закрепление видно');
    assert.match(String(items[0].getAttribute('title')), /закону X/);
    assert.equal(byClass(dialog, 'notes-ed__itemsrc')[0].textContent, 'вы');
    assert.equal(byClass(dialog, 'notes-ed__itemsrc')[1].textContent, 'руководитель');

    const area = byClass(dialog, 'notes-ed__area')[0];
    assert.equal(area.value, ENTRIES[0].text, 'в редакторе — текст выбранной записи');
    assert.equal(area.getAttribute('data-mic'), 'manual', 'микрофон ставим сами, не поверх поля');
    assert.ok(byClass(dialog, 'mic-btn').length >= 1, 'кнопка диктовки есть');
    assert.ok(byClass(dialog, 'notes-ed__limit')[0].textContent.includes('2 из 24 записей'));

    const filters = byClass(dialog, 'notes-ed__filter');
    assert.deepEqual(filters.map((one) => one.textContent), ['Все', 'Ваши', 'Руководителя']);
    assert.equal(filters[0].getAttribute('aria-pressed'), 'true');

    // фильтр «Ваши» оставляет одну запись
    filters[1].dispatch('click', { type: 'click', target: filters[1] });
    assert.equal(byClass(dialog, 'notes-ed__item').length, 1);

    // поиск сужает список
    filters[0].dispatch('click', { type: 'click', target: filters[0] });
    const search = byClass(dialog, 'notes-ed__search')[0];
    search.value = 'ornith';
    search.dispatch('input', { type: 'input', target: search });
    const found = byClass(dialog, 'notes-ed__item');
    assert.equal(found.length, 1);
    assert.match(String(found[0].textContent), /Ornith/);
    ui.destroy();
  } finally {
    uninstallDom();
  }
});

test('сохранение правки: PATCH в записи и «Сохранено»', async () => {
  const doc = installDom();
  const api = makeApi(ENTRIES);
  const changes = [];
  try {
    const ui = openNotesEditor({
      ctx: { api }, roomId: 'cafe', profile: 'biz-lawyer', label: 'Юрист',
      onChange: (summary) => changes.push(summary),
    });
    await ui.loaded;
    const area = byClass(ui.element, 'notes-ed__area')[0];
    area.value = 'Проверяй договор по закону X, сроки — в рабочих днях.';
    area.dispatch('input', { type: 'input', target: area });
    const save = byClass(ui.element, 'notes-ed__save')[0];
    save.dispatch('click', { type: 'click', target: save });
    await new Promise((resolve) => setTimeout(resolve, 10));

    const patch = api.state.calls.find((one) => one[0] === 'PATCH');
    assert.ok(patch, 'PATCH отправлен');
    assert.equal(patch[1], '/api/rooms/cafe/agents/biz-lawyer/notes/entries/n_one');
    assert.match(patch[2].text, /рабочих днях/);
    assert.equal(byClass(ui.element, 'notes-ed__status')[0].textContent, 'Сохранено');
    assert.equal(changes.length, 1);
    assert.equal(changes[0].count, 2);
    ui.destroy();
    assert.equal(byClass(doc.body, 'notes-ed').length, 0, 'закрылось');
  } finally {
    uninstallDom();
  }
});

test('новая запись: POST и выбор созданной записи', async () => {
  const doc = installDom();
  const api = makeApi(ENTRIES);
  try {
    const ui = openNotesEditor({ ctx: { api }, roomId: 'cafe', profile: 'biz-lawyer', label: 'Юрист' });
    await ui.loaded;
    const newBtn = byText(ui.element, 'Новая запись')[0];
    newBtn.dispatch('click', { type: 'click', target: newBtn });
    const area = byClass(ui.element, 'notes-ed__area')[0];
    assert.equal(area.value, '', 'поле очищено для новой записи');
    area.value = 'Не обсуждай цены ниже согласованных.';
    area.dispatch('input', { type: 'input', target: area });
    const save = byClass(ui.element, 'notes-ed__save')[0];
    save.dispatch('click', { type: 'click', target: save });
    await new Promise((resolve) => setTimeout(resolve, 10));

    const post = api.state.calls.find((one) => one[0] === 'POST' && one[1].endsWith('/notes/entries'));
    assert.ok(post, 'POST отправлен');
    assert.match(post[2].text, /цены ниже/);
    assert.equal(post[2].source, 'user');
    assert.equal(api.state.entries.length, 3);
    assert.equal(byClass(ui.element, 'notes-ed__item').length, 3);
    assert.equal(byClass(ui.element, 'notes-ed__area')[0].value, 'Не обсуждай цены ниже согласованных.');
    ui.destroy();
  } finally {
    uninstallDom();
  }
});

test('Ctrl+S сохраняет, а закрытие с правкой просит подтверждение', async () => {
  const doc = installDom();
  const api = makeApi(ENTRIES);
  try {
    const ui = openNotesEditor({ ctx: { api }, roomId: 'cafe', profile: 'biz-lawyer', label: 'Юрист' });
    await ui.loaded;
    const area = byClass(ui.element, 'notes-ed__area')[0];
    area.value = 'Новый текст правила';
    area.dispatch('input', { type: 'input', target: area });

    // закрытие без сохранения: окно остаётся, появляется полоса решения
    ui.close();
    assert.equal(byClass(doc.body, 'notes-ed').length, 1, 'окно не закрылось');
    assert.equal(byClass(ui.element, 'notes-ed__leave')[0].className.includes('hidden'), false);

    // «Вернуться» — продолжаем править
    const back = byText(ui.element, 'Вернуться')[0];
    back.dispatch('click', { type: 'click', target: back });
    assert.equal(byClass(ui.element, 'notes-ed__leave')[0].className.includes('hidden'), true);

    // Ctrl+S — сохраняет без кнопки
    doc.fire('keydown', { key: 's', ctrlKey: true, preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.ok(api.state.calls.some((one) => one[0] === 'PATCH'), 'PATCH по Ctrl+S');

    // теперь правок нет — закрывается сразу
    ui.close();
    assert.equal(byClass(doc.body, 'notes-ed').length, 0, 'окно закрыто');
  } finally {
    uninstallDom();
  }
});

test('журнал изменений открывается и показывает откат', async () => {
  const doc = installDom();
  const api = makeApi(ENTRIES);
  try {
    const ui = openNotesEditor({ ctx: { api }, roomId: 'cafe', profile: 'biz-lawyer', label: 'Юрист' });
    await ui.loaded;
    const journalBtn = byText(ui.element, 'Журнал')[0];
    journalBtn.dispatch('click', { type: 'click', target: journalBtn });
    await new Promise((resolve) => setTimeout(resolve, 10));
    const box = byClass(ui.element, 'notes-ed__journal')[0];
    assert.equal(box.className.includes('hidden'), false, 'журнал виден');
    assert.ok(api.state.calls.some((one) => one[0] === 'GET' && one[1].includes('/notes/journal')));
    assert.equal(byText(box, 'Откатить').length, 1, 'есть «Откатить»');
    assert.ok(String(box.textContent).includes('добавлено'));
    ui.destroy();
  } finally {
    uninstallDom();
  }
});
