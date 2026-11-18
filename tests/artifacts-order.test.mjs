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
 *     (`POST /api/artifacts/open` с `room`) — раньше прямая ссылка отвечала 403.
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

FakeElement.prototype.closest = function closest(selector) {
  const wantsAnchor = /a\[data-path\]/.test(String(selector));
  let node = this;
  while (node && node.nodeType === 1) {
    if (wantsAnchor && String(node.tagName).toLowerCase() === 'a' && node.hasAttribute('data-path')) {
      return node;
    }
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

test('ссылка на локальный файл внутри свода открывается с комнатой (§1.4)', () => withDom(() => {
  const ctx = makeCtx();
  const text = 'Итог: [письмо поставщику](C:/Work/Rooms/demo/Письмо-Ромашка.docx) готово.';
  const block = mdBlock(text, { openPath: (path) => openArtifactPath(ctx, path, ROOM) });
  const anchor = walk(block, (el) => String(el.tagName).toLowerCase() === 'a')[0];
  assert.ok(anchor, 'ссылка на локальный файл построена');
  assert.equal(anchor.getAttribute('data-path'), 'C:/Work/Rooms/demo/Письмо-Ромашка.docx');
  block.dispatch('click', { type: 'click', target: anchor, preventDefault() {} });
  assert.equal(ctx.posts.length, 1, 'открытие идёт через /api/files/launch (W26-D3)');
  assert.equal(ctx.posts[0].path, '/api/files/launch');
  assert.equal(ctx.posts[0].body.mode, 'open', 'ссылка открывает файл, а не папку');
  assert.equal(ctx.posts[0].body.room, ROOM, 'без комнаты сервер отвечал 403');
  assert.equal(ctx.posts[0].body.path, 'C:/Work/Rooms/demo/Письмо-Ромашка.docx');
}));

test('без openPath разметка свода работает как раньше', () => withDom(() => {
  const block = mdBlock('см. [итог](C:/Work/Rooms/demo/07-Итог.md)');
  const anchor = walk(block, (el) => String(el.tagName).toLowerCase() === 'a')[0];
  assert.equal(anchor.getAttribute('data-path'), 'C:/Work/Rooms/demo/07-Итог.md');
  // клик без комнаты не падает: обработчик по умолчанию открывает /api/file
  block.dispatch('click', { type: 'click', target: anchor, preventDefault() {} });
}));
