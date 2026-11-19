/**
 * artifacts-order.test.mjs — блок «Артефакты»: группы, порядок, папки, ссылки (W20-A1, B-43).
 *
 * Проверяется контракт WAVE20 §1.4 на стороне интерфейса:
 *   • группы «Итоговые / Материалы / Служебные» идут в этом порядке, итоговые первыми;
 *   • у файла видны время изменения и размер, бейдж «новее свода» — только когда
 *     сервер сказал `newer_than_svod: true`;
 *   • подпапка показана свёрнутой группой `<details>` со счётчиком, раскрытие
 *     запоминается между перерисовками;
 *   • длинные названия файлов не обрезаются в разметке (обрезка — только CSS,
 *     полное имя в `title`);
 *   • 30+ элементов и папка с вложенными файлами не ломают список;
 *   • ссылка на локальный файл внутри свода открывается в контексте комнаты
 *     (`POST /api/files/launch` с `room`, W26-D3; абсолютный путь — чип файла,
 *     W26-D4) — раньше прямая ссылка отвечала 403.
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, installFakeDom, uninstallFakeDom, expandSections } from './fake-dom.mjs';
import {
  renderArtifacts as renderArtifactsRaw, artifactGroup, isFolderNode, artifactsCount, openArtifactPath,
} from '../web/js/views/request-parts.js';
import { mdBlock } from '../web/js/ui.js';
import { installFoundWiring } from '../web/js/found.js';

if (typeof FakeElement.prototype.remove !== 'function') {
  FakeElement.prototype.remove = function remove() {
    if (this.parentNode) {
      const idx = this.parentNode.childNodes.indexOf(this);
      if (idx !== -1) this.parentNode.childNodes.splice(idx, 1);
    }
    this.parentNode = null;
  };
}
/**
 * W25-L2: блок «Артефакты» свёрнут по умолчанию (R5 §7.2) и собирается лениво —
 * тест разворачивает его нажатием по заголовку, как человек. Умолчание и
 * «свёрнутая строка со счётчиком» проверяются в `request-blocks.test.mjs`.
 */
function renderArtifacts(detailArg, ctx, opts) {
  return expandSections(renderArtifactsRaw(detailArg, ctx, opts));
}

/* Мини-`closest`: только те селекторы, которые реально спрашивают код-участник
   (`mdBlock` и обработчик чипов `found.js`). Настоящий CSS-движок мини-DOM не
   изображает — честный список того, что нужно тестам. */
function nodeMatches(node, sel) {
  const has = (name) => !!(node.hasAttribute && node.hasAttribute(name));
  const cls = (name) => String(node.getAttribute('class') || '').split(/\s+/).includes(name);
  const clsPart = (part) => String(node.getAttribute('class') || '').includes(part);
  const tag = String(node.tagName || '').toLowerCase();
  switch (sel) {
    case 'a[data-path]': return tag === 'a' && has('data-path');
    case '.md-file-chip[data-path]': return cls('md-file-chip') && has('data-path');
    case '.md-file-chip': return cls('md-file-chip');
    case '.md-url-chip': return cls('md-url-chip');
    case '.found-card': return cls('found-card');
    case '.md-file-chip__name': return cls('md-file-chip__name');
    case '.found-card__name': return cls('found-card__name');
    case '[data-path]': return has('data-path');
    case '[data-file-owner]': return has('data-file-owner');
    case '[class*="--open"]': return clsPart('--open');
    case '[class*="--reveal"]': return clsPart('--reveal');
    case '[class*="--copy"]': return clsPart('--copy');
    default: return false;
  }
}

FakeElement.prototype.closest = function closest(selector) {
  const sels = String(selector).split(',').map((s) => s.trim()).filter(Boolean);
  let node = this;
  while (node && node.nodeType === 1) {
    if (sels.some((s) => nodeMatches(node, s))) return node;
    node = node.parentNode;
  }
  return null;
};
FakeElement.prototype.focus = function focus() {};
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};

const ROOM = 'fn';
const PROJECT = 'C:\\TeamRoom\\Agents\\default';
const BASE = 1760000000;

function withDom(fn) {
  installFakeDom();
  try { return fn(); } finally { uninstallFakeDom(); }
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

function byClass(root, cls) {
  return walk(root, (el) => el.className.split(/\s+/).includes(cls));
}

function groups(root) {
  return walk(root, (el) => (el.getAttribute('data-group') || '') !== '');
}

function rowsOf(section) {
  return walk(section, (el) => el.className.split(/\s+/).includes('rp-art'));
}

const LONG_NAME = '09-Проверка-расчётов-по-поставке-Ромашка-за-первый-квартал-с-учётом-'
  + 'изменений-в-смете-и-письма-поставщику-от-кафе-Лето';

function fileItem(over) {
  return Object.assign({
    path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\00-Бриф.md',
    title: '00-Бриф',
    kind: 'md',
    exists: true,
    size: 1536,
    mtime: BASE + 100,
    group: 'material',
    folder: '',
    state: 'ok',
    outside_project: false,
    reason: null,
    newer_than_svod: false,
  }, over || {});
}

/** 30+ элементов: 2 итоговых, 26 материалов, служебный файл и папка из трёх копий. */
function detail() {
  const items = [
    fileItem({
      title: '11-Итог-дня', group: 'final', mtime: BASE + 9000, size: 2048,
      newer_than_svod: true,
      path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\11-Итог-дня.md',
    }),
    fileItem({
      title: '07-Итог-ред2', group: 'final', mtime: BASE + 7000, size: 4096,
      path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\07-Итог-ред2.md',
    }),
  ];
  for (let i = 0; i < 26; i += 1) {
    items.push(fileItem({
      title: i === 25 ? LONG_NAME : `Материал-${String(i).padStart(2, '0')}-сверка-цифр`,
      size: 1024 * (i + 1),
      mtime: BASE + 6000 - i * 100,
      path: PROJECT + `\\2026-03-04 Письмо поставщику — ответ\\Материал-${i}.md`,
    }));
  }
  items.push(fileItem({
    title: '_состояние', kind: 'json', group: 'service', size: 512, mtime: BASE + 50,
    path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\_состояние.json',
  }));
  items.push({
    kind: 'folder',
    title: '_ред2-копии',
    folder: '_ред2-копии',
    path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\_ред2-копии',
    exists: true,
    size: null,
    mtime: BASE + 8000,
    group: 'material',
    state: 'ok',
    outside_project: false,
    reason: null,
    newer_than_svod: false,
    count: 3,
    items: [
      fileItem({
        title: '07-Итог-ред2-копия', folder: '_ред2-копии', mtime: BASE + 8000,
        path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\_ред2-копии\\07-Итог-ред2-копия.md',
      }),
      fileItem({
        title: '09-Проверка-цифр-копия', folder: '_ред2-копии', mtime: BASE + 7900,
        path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\_ред2-копии\\09-Проверка-цифр-копия.md',
      }),
      fileItem({
        title: '_оглавление', kind: 'html', folder: '_ред2-копии', mtime: BASE + 7800,
        path: PROJECT + '\\2026-03-04 Письмо поставщику — ответ\\_ред2-копии\\_оглавление.html',
      }),
    ],
  });
  return { id: 't_a1b2c3d4', room: ROOM, artifacts: items };
}

function makeCtx() {
  const posts = [];
  return {
    posts,
    room: { id: ROOM, project_dir: PROJECT },
    api: {
      post(path, body) {
        posts.push({ path, body });
        return Promise.resolve({ ok: true, revealed: true });
      },
    },
    toast() {},
  };
}

test('группы идут в порядке «Итоговые → Материалы → Служебные» (B-43)', () => withDom(() => {
  const card = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
  assert.ok(card, 'блок построен');
  const order = groups(card).map((el) => el.getAttribute('data-group'));
  assert.deepEqual(order, ['final', 'material', 'service']);

  const [final, material, service] = groups(card);
  assert.match(textOf(final), /Итоговые/);
  assert.match(textOf(material), /Материалы/);
  assert.match(textOf(service), /Служебные/);

  // итоговых ровно два, и они именно в «Итоговых»
  const finalTitles = rowsOf(final).map((row) => textOf(row));
  assert.equal(finalTitles.length, 2);
  assert.ok(finalTitles[0].includes('11-Итог-дня'));
  assert.ok(finalTitles[1].includes('07-Итог-ред2'));
  // материалы не содержат итоговых файлов
  assert.ok(!textOf(material).includes('11-Итог-дня'));
  // служебная группа — один файл
  assert.equal(rowsOf(service).length, 1);
  assert.match(textOf(service), /_состояние/);
}));

test('счётчик блока считает файлы внутри папок (30+ элементов)', () => withDom(() => {
  const d = detail();
  assert.equal(artifactsCount(d.artifacts), 32);
  const card = renderArtifacts(d, makeCtx(), { projectDir: PROJECT });
  const head = textOf(card);
  assert.match(head, /32 файла/);
  const allRows = byClass(card, 'rp-art');
  assert.equal(allRows.length, 32, 'каждый файл — своя строка (включая файлы папки)');
}));

test('у файла время изменения и размер, бейдж «новее свода» только у свежего', () => withDom(() => {
  const card = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
  const [final] = groups(card);
  const rows = rowsOf(final);
  const fresh = rows[0];
  const older = rows[1];
  assert.match(textOf(fresh), /новее свода/);
  assert.ok(!textOf(older).includes('новее свода'));
  // время и размер
  assert.ok(byClass(fresh, 'rp-art__time').length === 1, 'время изменения видно');
  assert.ok(byClass(fresh, 'rp-art__info').length === 1, 'строка с размером');
  assert.match(textOf(fresh), /КБ|Б/);
  // подсказка у бейджа объясняет смысл
  const badge = byClass(fresh, 'rp-art__newer')[0];
  assert.match(badge.getAttribute('title'), /позже последней версии свода/);
}));

test('подпапка — свёрнутая группа со счётчиком, раскрытие запоминается', () => withDom(() => {
  const card = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
  const details = walk(card, (el) => String(el.tagName).toLowerCase() === 'details');
  assert.equal(details.length, 1);
  const folder = details[0];
  assert.equal(folder.getAttribute('data-folder'), '_ред2-копии');
  assert.notEqual(folder.open, true, 'по умолчанию папка свёрнута');
  const summary = walk(folder, (el) => String(el.tagName).toLowerCase() === 'summary')[0];
  assert.match(textOf(summary), /_ред2-копии/);
  assert.match(textOf(summary), /3 файла/);
  const inner = byClass(folder, 'rp-art');
  assert.equal(inner.length, 3, 'файлы папки лежат внутри неё');

  // пользователь раскрыл папку — после перерисовки она остаётся раскрытой
  folder.open = true;
  folder.dispatch('toggle');
  const again = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
  const reopened = walk(again, (el) => String(el.tagName).toLowerCase() === 'details')[0];
  assert.equal(reopened.open, true, 'состояние папки не теряется');
}));

test('длинное название файла не обрезается в разметке, полное — в подсказке', () => withDom(() => {
  const card = renderArtifacts(detail(), makeCtx(), { projectDir: PROJECT });
  const titleEl = byClass(card, 'rp-art__title').find((el) => textOf(el) === LONG_NAME);
  assert.ok(titleEl, 'длинное название показано целиком');
  assert.equal(titleEl.getAttribute('title'), LONG_NAME);
}));

test('группа и папка без серверного group считаются по имени файла', () => withDom(() => {
  const d = detail();
  const raw = d.artifacts.map((item) => Object.assign({}, item));
  delete raw[0].group;
  delete raw[1].group;
  assert.equal(artifactGroup(raw[0]), 'final');
  assert.equal(artifactGroup(raw[2]), 'material');
  assert.equal(artifactGroup(fileItem({ title: '_индекс', kind: 'json', group: '',
    path: PROJECT + '\папка\_индекс.json' })), 'service');
  assert.equal(isFolderNode(raw[raw.length - 1]), true);
  assert.equal(isFolderNode(raw[0]), false);
}));

test('пустой список — блока нет', () => withDom(() => {
  assert.equal(renderArtifacts({ room: ROOM, artifacts: [] }, makeCtx(), {}), null);
  assert.equal(renderArtifacts({ room: ROOM }, makeCtx(), {}), null);
}));

/* ── ссылка на локальный файл внутри свода (W20-A1 §1.4, W26-D3/D4, W26-QA1) ──
 * Абсолютный путь в своде теперь ЧИП файла (`span.md-file-chip[data-path]`), а не
 * `a[data-path]`: у чипа есть «Открыть · Показать в папке · Копировать путь».
 * Открытие файла — `POST /api/files/launch {path, room, mode:"open"}`; комната
 * берётся из контекста свода, а не из адреса. Эти три теста держат главное:
 * путь доходит целиком (пробел, кириллица), ровно ОДИН запрос на клик, комната
 * в теле запроса. */

const FILE_WITH_SPACES = 'C:/Work/Rooms/кафе Лето/Письмо поставщику.docx';

/** Чип файла в разметке свода (или null). */
function chipOf(root) {
  return byClass(root, 'md-file-chip')[0] || null;
}

/** Имя-нажатие внутри чипа: по нему человек «открывает» файл. */
function chipName(chip) {
  return byClass(chip, 'md-file-chip__name')[0] || null;
}

/** Синтетический клик: как настоящий — с целью и preventDefault. */
function clickOn(node) {
  const ev = { type: 'click', target: node, prevented: 0, stopped: 0,
    preventDefault() { this.prevented += 1; },
    stopPropagation() { this.stopped += 1; },
    stopImmediatePropagation() { this.stopped += 1; } };
  return ev;
}

/** Фальшивый документ для документного обработчика found.js (capture-слушатель). */
function fakeWiringDoc() {
  const handlers = [];
  return {
    __foundWired: false,
    handlers,
    addEventListener(type, fn) { if (type === 'click') handlers.push(fn); },
    createElement(tag) { return new FakeElement(tag); },
    querySelector() { return null; },
    body: new FakeElement('body'),
  };
}

/** Асинхронный вариант withDom: обработчик чипа отвечает промисом. */
async function withDomAsync(fn) {
  installFakeDom();
  const saved = { fetch: globalThis.fetch, location: globalThis.location };
  try { return await fn(); } finally {
    uninstallFakeDom();
    if (saved.fetch === undefined) delete globalThis.fetch; else globalThis.fetch = saved.fetch;
    if (saved.location === undefined) delete globalThis.location; else globalThis.location = saved.location;
  }
}

/** Подмена сети: считает запросы и отвечает «получилось». */
function stubFetch(calls) {
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: (opts && opts.method) || 'GET',
      body: opts && opts.body ? JSON.parse(opts.body) : null });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
}

test('ссылка на локальный файл внутри свода открывается с комнатой (§1.4)', () => withDom(() => {
  const ctx = makeCtx();
  const text = 'Итог: [письмо поставщику](<' + FILE_WITH_SPACES + '>) готово.';
  const block = mdBlock(text, { openPath: (path) => openArtifactPath(ctx, path, ROOM) });

  const chip = chipOf(block);
  assert.ok(chip, 'ссылка на абсолютный путь построена чипом файла');
  assert.equal(chip.getAttribute('data-path'), FILE_WITH_SPACES,
    'путь с пробелом и кириллицей сохранён целиком');
  assert.equal(chip.getAttribute('title'), FILE_WITH_SPACES, 'полный путь — в подсказке');
  const name = chipName(chip);
  assert.ok(name, 'у чипа есть имя, по которому открывают файл');
  assert.equal(textOf(name), 'письмо поставщику', 'метка ссылки не потеряна');
  assert.ok(!walk(block, (el) => String(el.tagName).toLowerCase() === 'a'
    && el.getAttribute('data-path') !== null).length, 'старой ссылки a[data-path] больше нет');

  // Блок знает свою комнату — он и открывает файл (документный обработчик отступает).
  assert.equal(block.getAttribute('data-file-owner'), '1',
    'блок с openPath сам отвечает за открытие файлов внутри себя');

  const ev = clickOn(name);
  block.dispatch('click', ev);
  assert.equal(ev.prevented, 1, 'переход по «#» отменён');
  assert.equal(ctx.posts.length, 1, 'ровно один запрос: /api/files/launch (W26-D3)');
  assert.equal(ctx.posts[0].path, '/api/files/launch');
  assert.equal(ctx.posts[0].body.mode, 'open', 'ссылка открывает файл, а не папку');
  assert.equal(ctx.posts[0].body.room, ROOM, 'без комнаты сервер отвечал 403');
  assert.equal(ctx.posts[0].body.path, FILE_WITH_SPACES, 'путь уходит без искажений');
}));

test('без openPath чип свода открывает файл общим обработчиком (комната из адреса)',
  () => withDomAsync(async () => {
    const block = mdBlock('см. [итог](C:/Work/Rooms/demo/07-Итог.md)');
    const chip = chipOf(block);
    assert.ok(chip, 'чип построен и без openPath');
    assert.equal(chip.getAttribute('data-path'), 'C:/Work/Rooms/demo/07-Итог.md');
    assert.equal(block.getAttribute('data-file-owner'), null,
      'без openPath блок файлы не перехватывает — работает общий обработчик');

    const doc = fakeWiringDoc();
    const calls = [];
    stubFetch(calls);
    globalThis.location = { hash: '#/room/fn/request/r1' };
    installFoundWiring(doc);
    assert.equal(doc.handlers.length, 1, 'один слушатель на документ');

    const ev = clickOn(chipName(chip));
    doc.handlers[0](ev);
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(calls.length, 1, 'один запрос на клик');
    assert.equal(calls[0].url, '/api/files/launch', 'тот же маршрут, что у openPath');
    assert.equal(calls[0].method, 'POST');
    assert.equal(calls[0].body.mode, 'open');
    assert.equal(calls[0].body.room, 'fn', 'комната из адреса — когда своей нет');
    assert.equal(calls[0].body.path, 'C:/Work/Rooms/demo/07-Итог.md');
  }));

test('клик по чипу в своде не срабатывает дважды и берёт комнату свода',
  () => withDomAsync(async () => {
    const ctx = makeCtx();
    const block = mdBlock('см. [итог](<' + FILE_WITH_SPACES + '>)',
      { openPath: (path) => openArtifactPath(ctx, path, ROOM) });
    const chip = chipOf(block);
    const doc = fakeWiringDoc();
    const calls = [];
    stubFetch(calls);
    // Адрес говорит про другую комнату: свод обязан открыть файл в СВОЕЙ.
    globalThis.location = { hash: '#/room/other/request/r2' };
    installFoundWiring(doc);

    const ev = clickOn(chipName(chip));
    doc.handlers[0](ev);           // capture-обработчик документа
    block.dispatch('click', ev);   // обработчик блока (всплытие)
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(calls.length, 0, 'документный обработчик не перехватил клик');
    assert.equal(ctx.posts.length, 1, 'один клик — один запрос, без двойного срабатывания');
    assert.equal(ctx.posts[0].body.room, ROOM, 'комната свода, а не из адреса');
    assert.equal(ctx.posts[0].body.mode, 'open');
    assert.equal(ctx.posts[0].body.path, FILE_WITH_SPACES);
  }));
