/**
 * hotkeys-viewmode.test.mjs — горячие клавиши и режим вида (W20-U3, пункты 17–18).
 *
 * Проверяем то, что легко сломать и трудно заметить глазами:
 *
 *  1) `J` / `K` ходят по вопросам правой колонки и ставят фокус в поле ответа,
 *     ходят по кругу; без вопросов клавиши молчат;
 *  2) одиночные клавиши (`?`, `J`, `K`, `V`) НЕ срабатывают, когда человек
 *     печатает в поле, и не мешают вводу в textarea;
 *  3) `Ctrl+Enter` не отправляет дважды: если поле уже обработало клавишу
 *     (`defaultPrevented`), общий обработчик ничего не делает;
 *  4) `?` открывает справку, и в ней перечислены реальные клавиши;
 *  5) режим вида: по умолчанию «подробный» (как раньше), выбор хранится
 *     локально, «простой» ставит `data-view-mode="simple"` на <html>;
 *  6) экспорт: подписи кнопок «В Obsidian», «Скачать HTML», «Печать / PDF»,
 *     группа кнопок в блоке свода, имя файла из заголовка ответа.
 *
 * Браузер не нужен: мини-DOM + поддельный документ.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import {
  HOTKEY_HELP, isTypingTarget, stepIndex, focusedIndex, moveQuestion,
  installHotkeys, uninstallHotkeys, openHotkeyHelp, closeHotkeyHelp, submitFromField,
  QUESTION_ITEM_SELECTOR,
} from '../web/js/hotkeys.js';
import {
  VIEW_MODE_KEY, VIEW_MODE_FULL, VIEW_MODE_SIMPLE, normalizeMode, readStoredMode,
  applyViewMode, getViewMode, setViewMode, toggleLabel, viewModeButton, VIEW_MODE_TEXT,
} from '../web/js/view-mode.js';
import { EXPORT_LABELS, EXPORT_HINTS, exportGroup, fileNameFromDisposition } from '../web/js/views/request-export.js';

/* ─────────────────────────────────────────────── мини-DOM: дополнения */

const P = FakeElement.prototype;
if (typeof P.querySelectorAll !== 'function') {
  P.querySelectorAll = function querySelectorAll(sel) {
    const out = [];
    const want = String(sel || '').trim();
    const m = /^\[([a-z0-9-]+)\]$/i.exec(want);
    const walk = (node) => {
      for (const child of node.childNodes || []) {
        if (child.nodeType !== 1) continue;
        if (m && child.hasAttribute(m[1])) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  };
}
if (typeof P.querySelector !== 'function') {
  P.querySelector = function querySelector(sel) {
    const list = this.querySelectorAll(sel);
    return list.length ? list[0] : null;
  };
}
if (typeof P.contains !== 'function') {
  P.contains = function contains(node) {
    let cur = node;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentNode;
    }
    return false;
  };
}

/* ─────────────────────────────────────────────── режим вида (P14) */

test('view mode: по умолчанию подробный — вид не меняется у того, кто не нажимал', () => {
  assert.equal(normalizeMode(undefined), VIEW_MODE_FULL);
  assert.equal(normalizeMode(''), VIEW_MODE_FULL);
  assert.equal(normalizeMode('что-то'), VIEW_MODE_FULL);
  assert.equal(normalizeMode('SIMPLE'), VIEW_MODE_SIMPLE);
  assert.equal(readStoredMode({ getItem: () => null }), VIEW_MODE_FULL);
});

test('view mode: выбор берётся из локального хранилища и переживает битое хранилище', () => {
  assert.equal(readStoredMode({ getItem: (k) => (k === VIEW_MODE_KEY ? 'simple' : null) }),
    VIEW_MODE_SIMPLE);
  const broken = { getItem() { throw new Error('приватный режим'); } };
  assert.equal(readStoredMode(broken), VIEW_MODE_FULL, 'битое хранилище не ломает вид');
});

test('view mode: простой вид ставит data-view-mode и класс на <html>', () => {
  const classes = new Set();
  const doc = {
    documentElement: {
      dataset: {},
      classList: { toggle: (name, on) => { if (on) classes.add(name); else classes.delete(name); } },
    },
  };
  const applied = applyViewMode('simple', { doc, persist: false });
  assert.equal(applied, VIEW_MODE_SIMPLE);
  assert.equal(doc.documentElement.dataset.viewMode, 'simple');
  assert.ok(classes.has('view-simple'), 'класс для CSS');
  assert.equal(getViewMode(), VIEW_MODE_SIMPLE, 'getViewMode читает применённый режим');
  applyViewMode('full', { doc, persist: false });
  assert.equal(doc.documentElement.dataset.viewMode, 'full');
  assert.ok(!classes.has('view-simple'));
});

test('view mode: кнопка переключает режим и запоминает выбор', () => {
  assert.equal(toggleLabel('full'), VIEW_MODE_TEXT.simple);
  assert.equal(toggleLabel('simple'), VIEW_MODE_TEXT.full);
  const storage = new Map();
  globalThis.localStorage = {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
  };
  const classes = new Set();
  installFakeDom();
  globalThis.document.documentElement = {
    dataset: {},
    classList: { toggle: (name, on) => { if (on) classes.add(name); else classes.delete(name); } },
  };
  try {
    applyViewMode('full', { persist: false });
    const btn = viewModeButton();
    assert.ok(btn, 'кнопка построена');
    assert.equal(btn.getAttribute('data-action'), 'view-mode');
    assert.equal(btn.getAttribute('aria-pressed'), 'false', 'по умолчанию подробный');
    btn.dispatch('click', { type: 'click', target: btn });
    assert.equal(storage.get(VIEW_MODE_KEY), VIEW_MODE_SIMPLE, 'выбор сохранён локально');
    assert.equal(btn.getAttribute('aria-pressed'), 'true');
    const label = btn.querySelector('[data-viewmode-label]');
    assert.equal(label.textContent, 'просто', 'подпись сменилась');
    assert.ok(classes.has('view-simple'));
    btn.dispatch('click', { type: 'click', target: btn });
    assert.equal(storage.get(VIEW_MODE_KEY), VIEW_MODE_FULL, 'обратно в подробный');
  } finally {
    uninstallFakeDom();
    delete globalThis.localStorage;
  }
});

/* ─────────────────────────────────────────────── вопросы: J / K */

test('hotkeys: J/K ходят по кругу и ставят фокус в поле ответа', () => {
  const focused = [];
  const items = [0, 1, 2].map((i) => ({
    text: 'вопрос ' + i,
    querySelector: () => ({ focus: () => focused.push(i) }),
    scrollIntoView() {},
  }));
  const doc = {
    querySelectorAll: (sel) => (sel === QUESTION_ITEM_SELECTOR ? items.slice() : []),
    activeElement: null,
  };
  const first = moveQuestion(1, doc);
  assert.deepEqual(first, { index: 0, total: 3 });
  assert.deepEqual(focused, [0], 'поле первого вопроса получило фокус');
  doc.activeElement = items[0];          // в браузере фокус ставит сам ввод
  assert.equal(moveQuestion(1, doc).index, 1);
  doc.activeElement = items[1];
  assert.equal(moveQuestion(1, doc).index, 2);
  doc.activeElement = items[2];
  assert.equal(moveQuestion(1, doc).index, 0, 'после последнего — снова первый');
  doc.activeElement = items[0];
  assert.equal(moveQuestion(-1, doc).index, 2, 'назад с первого — последний');
});

test('hotkeys: без вопросов клавиши ничего не делают', () => {
  const doc = { querySelectorAll: () => [], activeElement: null };
  assert.equal(moveQuestion(1, doc), null);
  assert.equal(stepIndex(-1, 1, 0), -1);
});

test('hotkeys: индекс шага — с −1 вперёд первый, назад последний', () => {
  assert.equal(stepIndex(-1, 1, 4), 0);
  assert.equal(stepIndex(-1, -1, 4), 3);
  assert.equal(stepIndex(0, -1, 4), 3);
  assert.equal(stepIndex(3, 1, 4), 0);
  const row = { id: 'q1' };
  assert.equal(focusedIndex([row], row), 0, 'фокус на самой строке');
  assert.equal(focusedIndex([row], null), -1);
  assert.equal(focusedIndex([], { id: 'x' }), -1);
});

test('hotkeys: в поле ввода печатают — одиночные клавиши молчат', () => {
  assert.equal(isTypingTarget({ nodeType: 1, tagName: 'TEXTAREA' }), true);
  assert.equal(isTypingTarget({ nodeType: 1, tagName: 'INPUT', type: 'text' }), true);
  assert.equal(isTypingTarget({ nodeType: 1, tagName: 'INPUT', type: 'search' }), true);
  assert.equal(isTypingTarget({ nodeType: 1, tagName: 'INPUT', type: 'checkbox' }), false);
  assert.equal(isTypingTarget({ nodeType: 1, tagName: 'BUTTON' }), false);
  assert.equal(isTypingTarget({ nodeType: 1, tagName: 'DIV', isContentEditable: true }), true);
  assert.equal(isTypingTarget(null), false);
});

/* ─────────────────────────────────────────────── установка обработчика */

function harness(overrides = {}) {
  const listeners = [];
  const doc = {
    activeElement: null,
    querySelectorAll: () => [],
    addEventListener: (type, fn) => listeners.push({ type, fn }),
    removeEventListener: () => {},
    body: { appendChild() {} },
    ...overrides,
  };
  const fire = (ev) => {
    for (const l of listeners) if (l.type === 'keydown') l.fn(ev);
  };
  return { doc, fire };
}

test('hotkeys: `?` открывает справку, но не в поле ввода', () => {
  let helped = 0;
  const { doc, fire } = harness();
  installHotkeys({ doc, onHelp: () => { helped += 1; } });
  try {
    fire({ key: '?', target: { nodeType: 1, tagName: 'DIV' }, preventDefault() {} });
    assert.equal(helped, 1, 'справка открылась');
    fire({ key: '?', target: { nodeType: 1, tagName: 'TEXTAREA' }, preventDefault() {} });
    assert.equal(helped, 1, 'в поле `?` — это просто знак вопроса');
    assert.ok(doc.querySelectorAll);
  } finally {
    uninstallHotkeys();
  }
});

test('hotkeys: `V` переключает вид, `J`/`K` листают вопросы', () => {
  let toggles = 0;
  const moves = [];
  const field = { focus: () => moves.push('focus') };
  const item = {
    querySelector: () => field,
    scrollIntoView() {},
  };
  const { doc, fire } = harness({ querySelectorAll: () => [item] });
  installHotkeys({ doc, onToggleView: () => { toggles += 1; } });
  try {
    fire({ key: 'v', target: { nodeType: 1, tagName: 'DIV' }, preventDefault() {} });
    assert.equal(toggles, 1);
    fire({ key: 'j', target: { nodeType: 1, tagName: 'DIV' }, preventDefault() {} });
    assert.deepEqual(moves, ['focus'], 'фокус встал в поле вопроса');
    fire({ key: 'j', target: { nodeType: 1, tagName: 'INPUT', type: 'text' }, preventDefault() {} });
    assert.deepEqual(moves, ['focus'], 'в поле ввода J — это буква');
  } finally {
    uninstallHotkeys();
  }
});

test('hotkeys: Ctrl+Enter не отправляет дважды, если поле уже обработало', () => {
  let submitted = 0;
  const form = {
    requestSubmit: () => { submitted += 1; },
    querySelector: () => null,
  };
  const field = { closest: (sel) => (sel === 'form' ? form : null) };
  const handled = { key: 'Enter', ctrlKey: true, target: field, defaultPrevented: true, preventDefault() {} };
  assert.equal(submitFromField(handled), false, 'поле уже отправило — общий обработчик молчит');
  assert.equal(submitted, 0, 'defaultPrevented — повторной отправки нет');

  const raw = { key: 'Enter', ctrlKey: true, target: field, defaultPrevented: false, preventDefault() {} };
  assert.equal(submitFromField(raw), true, 'без обработчика форма отправляется');
  assert.equal(submitted, 1, 'отправка ровно одна');

  const { doc, fire } = harness();
  let clicks = 0;
  const btn = { click: () => { clicks += 1; }, disabled: false };
  const holder = {
    getAttribute: () => '[data-action="send"]',
    querySelector: () => btn,
  };
  const target = { closest: (sel) => (sel === 'form' ? null : (sel === '[data-enter-submit]' ? holder : null)) };
  installHotkeys({ doc });
  try {
    fire({ key: 'Enter', ctrlKey: true, target, preventDefault() {} });
    assert.equal(clicks, 1, 'Ctrl+Enter нажал кнопку отправки');
  } finally {
    uninstallHotkeys();
  }
});

test('hotkeys: справка перечисляет реальные клавиши', () => {
  const keys = HOTKEY_HELP.flatMap((row) => (Array.isArray(row.keys) ? row.keys : [row.keys])).join(' | ');
  for (const need of ['J', 'K', 'Ctrl + Enter', 'V', '?', 'Ctrl + K', '/', 'Esc']) {
    assert.ok(keys.includes(need), 'в справке есть ' + need);
  }
  assert.ok(HOTKEY_HELP.every((row) => row.what && row.what.length > 5), 'у каждой клавиши есть объяснение');
});

test('hotkeys: окно справки открывается и закрывается без падения', () => {
  installFakeDom();
  try {
    const shell = openHotkeyHelp();
    // В мини-DOM нет настоящих диалогов — проверяем, что функция не падает и
    // возвращает что-то закрываемое.
    if (shell) {
      assert.equal(typeof shell.close, 'function');
      closeHotkeyHelp();
      assert.equal(shell.dialog.className || '', 'dialog');
    }
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────────────────────── экспорт (P13) */

test('export: подписи меню — четыре, по-русски (W24-N1: одно меню)', () => {
  assert.deepEqual(Object.keys(EXPORT_LABELS).sort(), ['html', 'md', 'obsidian', 'print']);
  assert.equal(EXPORT_LABELS.obsidian, 'В Obsidian');
  assert.equal(EXPORT_LABELS.html, 'Скачать HTML');
  assert.equal(EXPORT_LABELS.print, 'Печать / PDF');
  assert.equal(EXPORT_LABELS.md, 'Скачать .md');
  assert.ok(EXPORT_HINTS.obsidian.length > 20, 'у кнопки есть подсказка');
});

test('export: имя файла берётся из заголовка ответа', () => {
  assert.equal(fileNameFromDisposition('attachment; filename="svod-2026.html"'), 'svod-2026.html');
  assert.equal(fileNameFromDisposition("attachment; filename*=UTF-8''%D1%81%D0%B2%D0%BE%D0%B4.html"),
    'свод.html');
  assert.equal(fileNameFromDisposition('', 'fallback.html'), 'fallback.html');
});

test('export: одно меню «Экспорт ▾» вместо четырёх кнопок подряд', () => {
  installFakeDom();
  try {
    const group = exportGroup({ api: { post: async () => ({ ok: true }) }, toast() {} }, 't_demo00001');
    assert.ok(group.className.includes('req-export'));
    assert.equal(group.tagName.toLowerCase(), 'details', 'это меню-раскрывашка');
    assert.equal(group.getAttribute('open'), null, 'по умолчанию закрыто');
    const items = group.querySelectorAll('[data-export]');
    assert.equal(items.length, 4, 'четыре пункта: три файла и .md');
    const kinds = items.map((b) => b.getAttribute('data-export')).join(',');
    assert.equal(kinds, 'obsidian,html,print,md');
    const buttons = items.filter((el) => el.tagName.toLowerCase() === 'button');
    assert.equal(buttons.length, 3, 'три действия-кнопки');
    for (const btn of buttons) {
      assert.ok(btn.getAttribute('title'), 'у каждого пункта есть подсказка');
      assert.ok(btn.listeners.click, 'пункт нажимается');
    }
    const md = items.find((el) => el.getAttribute('data-export') === 'md');
    assert.equal(md.tagName.toLowerCase(), 'a', '.md — ссылка, а не кнопка');
    assert.match(md.getAttribute('href'), /\/api\/export\/request\/t_demo00001\.md/);
    assert.ok(md.getAttribute('download') !== null, 'скачивается, а не открывается');
  } finally {
    uninstallFakeDom();
  }
});
