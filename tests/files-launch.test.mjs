/**
 * files-launch.test.mjs — «Файлы» вместо «Артефактов» и рабочие кнопки (W26-D3).
 *
 * Что проверяется (по разделу 7 подготовки волны 26):
 *   • блок называется «Файлы» — слова «артефакт» человек больше не видит;
 *   • «Открыть» — серверное действие `/api/files/launch {mode:"open"}`: файл
 *     открывает программа по умолчанию. `window.open` не вызывается ни разу
 *     (внутри окна Hermes Desktop новые окна блокируются — кнопка не работала);
 *   • «Показать в папке» — `/api/files/launch {mode:"reveal"}`;
 *   • «Просмотр» есть только у текстовых типов и показывает текст во встроенном
 *     окне приложения (а не новой вкладкой);
 *   • у пропавшего файла кнопок открытия нет — остаётся «Копировать путь».
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, FakeElement as FE, installFakeDom, uninstallFakeDom, expandSections } from './fake-dom.mjs';
import {
  renderArtifacts as renderArtifactsRaw, canPreview, openPreview, openFile, revealFile,
} from '../web/js/views/request-parts.js';
import { FILES_RU } from '../web/js/text-ru.js';

if (typeof FakeElement.prototype.remove !== 'function') {
  FakeElement.prototype.remove = function remove() {
    if (this.parentNode) {
      const idx = this.parentNode.childNodes.indexOf(this);
      if (idx !== -1) this.parentNode.childNodes.splice(idx, 1);
    }
    this.parentNode = null;
  };
}
if (typeof FE.prototype.removeChild !== 'function') {
  FE.prototype.removeChild = function removeChild(node) {
    const idx = (this.childNodes || []).indexOf(node);
    if (idx !== -1) this.childNodes.splice(idx, 1);
    if (node) node.parentNode = null;
    return node;
  };
}
FE.prototype.closest = function closest() { return null; };
FE.prototype.focus = function focus() {};
FE.prototype.blur = function blur() {};
FE.prototype.scrollIntoView = function scrollIntoView() {};

const ROOM = 'fn';
const PROJECT = 'C:\\TeamRoom\\Agents\\default';

function renderArtifacts(detailArg, ctx, opts) {
  return expandSections(renderArtifactsRaw(detailArg, ctx, opts));
}

/**
 * Поставить поддельный `document` с телом: диалог «Просмотра» монтируется в
 * `document.body`, а `installFakeDom` тело не создаёт.
 */
function installDom() {
  const doc = installFakeDom();
  doc.body = new FakeElement('body');
  doc.addEventListener = () => {};
  doc.removeEventListener = () => {};
  return doc;
}

function uninstallDom() {
  uninstallFakeDom();
}

function walk(node, pred, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    if (pred(child)) out.push(child);
    walk(child, pred, out);
  }
  return out;
}

function textOf(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data);
  return (node.childNodes || []).map(textOf).join('');
}

function rowOf(card, needle) {
  const rows = walk(card, (n) => String(n.getAttribute('class') || '').split(/\s+/).includes('rp-art'));
  return rows.find((n) => textOf(n).includes(needle));
}

/** Кнопки-действия (чип пути — тоже <button>, его в этот список не берём). */
function buttons(card) {
  return walk(card, (n) => n.tagName === 'button'
    && /(^|\s)btn(\s|$)/.test(String(n.getAttribute('class') || '')));
}

async function press(node) {
  await (node.listeners.click || []).reduce((p, fn) => p.then(() => fn({})), Promise.resolve());
}

function makeCtx(text = '') {
  const calls = { posts: [], texts: [] };
  return {
    calls,
    api: {
      post: async (url, body) => { calls.posts.push({ url, body }); return { ok: true }; },
      text: async (url) => { calls.texts.push(url); return text; },
    },
    toast: () => {},
  };
}

function detail() {
  return {
    id: 't_w26d3',
    room: ROOM,
    artifacts: [
      { path: `${PROJECT}\\Итог\\Письмо поставщику — ответ.docx`, title: 'Письмо поставщику — ответ',
        kind: 'docx', exists: true, size: 2048, state: 'ok', outside_project: false },
      { path: `${PROJECT}\\Итог\\сводка.md`, title: 'Сводка', kind: 'md', exists: true, size: 512,
        state: 'ok', outside_project: false },
      { path: `${PROJECT}\\Итог\\пропал.md`, title: 'Пропал', kind: 'md', exists: false, size: null,
        state: 'missing', outside_project: false },
    ],
  };
}

test('блок называется «Файлы»: слова «артефакт» человек не видит', () => {
  installDom();
  try {
    const card = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
    const text = textOf(card);
    assert.ok(text.includes(FILES_RU.title), text.slice(0, 200));
    assert.ok(text.includes('Файлы'), 'заголовок блока — «Файлы»');
    assert.ok(!/артефакт/i.test(text), 'ни одного «артефакта» в интерфейсе');
    assert.equal(FILES_RU.title, 'Файлы');
    assert.equal(FILES_RU.open, 'Открыть');
    assert.equal(FILES_RU.reveal, 'Показать в папке');
    assert.equal(FILES_RU.copyPath, 'Копировать путь');
    assert.ok(!/артефакт/i.test(JSON.stringify(FILES_RU)), 'словарь подписей без «артефактов»');
  } finally { uninstallDom(); }
});

test('кнопки у файла: Открыть · Просмотр · Копировать путь · Показать в папке', () => {
  installDom();
  try {
    const card = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
    const row = rowOf(card, 'Сводка');
    const labels = buttons(row).map(textOf);
    assert.deepEqual(labels, ['Открыть', 'Просмотр', 'Копировать путь', 'Показать в папке'], labels.join('|'));
  } finally { uninstallDom(); }
});

test('«Просмотр» только у текстовых типов', () => {
  assert.equal(canPreview('C:\\x\\итог.md'), true);
  assert.equal(canPreview('C:\\x\\лог.txt'), true);
  assert.equal(canPreview('C:\\x\\письмо.docx'), false);
  assert.equal(canPreview('C:\\x\\таблица.xlsx'), false);
  assert.equal(canPreview('C:\\x\\схема.png'), false);
  assert.equal(canPreview(''), false);
});

test('«Открыть» идёт на сервер и НЕ вызывает window.open', async () => {
  installDom();
  const opened = [];
  globalThis.window = { open: (...args) => { opened.push(args); return null; } };
  try {
    const ctx = makeCtx();
    const card = renderArtifacts(detail(), ctx, { projectDir: PROJECT });
    const open = buttons(rowOf(card, 'Письмо поставщику')).find((n) => textOf(n) === 'Открыть');
    await press(open);
    assert.equal(opened.length, 0, 'window.open в Hermes Desktop блокируется — его больше нет');
    assert.equal(ctx.calls.posts.length, 1);
    assert.equal(ctx.calls.posts[0].url, '/api/files/launch');
    assert.equal(ctx.calls.posts[0].body.mode, 'open', '«Открыть» открывает ФАЙЛ программой по умолчанию');
    assert.equal(ctx.calls.posts[0].body.room, ROOM, 'комната нужна: корни папки проекта');
    assert.equal(ctx.calls.posts[0].body.path, `${PROJECT}\\Итог\\Письмо поставщику — ответ.docx`);
  } finally {
    delete globalThis.window;
    uninstallDom();
  }
});

test('«Показать в папке» — reveal, тем же адресом', async () => {
  installDom();
  try {
    const ctx = makeCtx();
    const card = renderArtifacts(detail(), ctx, { projectDir: PROJECT });
    const reveal = buttons(rowOf(card, 'Сводка')).find((n) => textOf(n) === 'Показать в папке');
    await press(reveal);
    assert.equal(ctx.calls.posts.length, 1);
    assert.equal(ctx.calls.posts[0].url, '/api/files/launch');
    assert.equal(ctx.calls.posts[0].body.mode, 'reveal');
  } finally { uninstallDom(); }
});

test('пропавший файл: открытия нет, путь скопировать можно', () => {
  installDom();
  try {
    const card = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
    const labels = buttons(rowOf(card, 'Пропал')).map(textOf);
    assert.deepEqual(labels, ['Копировать путь'], labels.join('|'));
  } finally { uninstallDom(); }
});

test('«Просмотр» показывает текст во встроенном окне, без новой вкладки', async () => {
  installDom();
  const opened = [];
  globalThis.window = { open: (...args) => { opened.push(args); return null; } };
  try {
    const ctx = makeCtx('# Сводка\n\nКоротко: всё готово.');
    const dlg = await openPreview(ctx, `${PROJECT}\\Итог\\сводка.md`, ROOM);
    assert.equal(opened.length, 0, 'никаких новых окон');
    assert.equal(ctx.calls.texts.length, 1);
    assert.ok(String(ctx.calls.texts[0]).startsWith('/api/file?path='), String(ctx.calls.texts[0]));
    assert.ok(String(ctx.calls.texts[0]).includes('&room=fn'), 'текст берём в контексте комнаты');
    const pre = walk(dlg.dialog, (n) => n.tagName === 'pre')[0];
    assert.ok(pre, 'во встроенном окне есть область текста');
    assert.ok(textOf(pre).includes('всё готово'), textOf(pre));
    dlg.close();
  } finally {
    delete globalThis.window;
    uninstallDom();
  }
});

test('openFile/revealFile возвращают признак результата и терпят отказ', async () => {
  installDom();
  try {
    const ok = makeCtx();
    assert.equal(await openFile(ok, 'C:\\x\\a.md', ROOM), true);
    const bad = {
      api: { post: async () => { throw new Error('Файл удалён или перемещён'); } },
      toast: () => {},
    };
    assert.equal(await revealFile(bad, 'C:\\x\\a.md', ROOM), false);
  } finally { uninstallDom(); }
});
