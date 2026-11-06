/**
 * artifacts-block.test.mjs — блок «Артефакты» карточки запроса (WAVE19, пакет A2).
 *
 * B-22: файл внутри папки проекта открывается, кнопки работают, а адрес файла несёт
 * комнату (`?path=…&room=…`) — без неё сервер не знает, чью папку проекта разрешать.
 * B-27: файл вне папки проекта помечен «вне папки проекта» и всё равно открывается.
 * Пропавший файл подписан «Файл удалён или перемещён», а не «не найден».
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';

FakeElement.prototype.remove = function remove() {
  if (this.parentNode) {
    const idx = this.parentNode.childNodes.indexOf(this);
    if (idx !== -1) this.parentNode.childNodes.splice(idx, 1);
  }
  this.parentNode = null;
};
FakeElement.prototype.closest = function closest() { return null; };
FakeElement.prototype.focus = function focus() {};
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  doc.addEventListener = () => {};
  doc.removeEventListener = () => {};
  globalThis.document = doc;
  return doc;
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

const ROOM = 'fn';
const PROJECT = 'C:\\TeamRoom\\Agents\\default';

function detail() {
  return {
    id: 't_825ada4c',
    room: ROOM,
    artifacts: [
      {
        path: `${PROJECT}\\2026-11-06 Письмо Лебедев — ответ\\Письмо Лебедев — ответ.docx`,
        title: 'Письмо Лебедев — ответ',
        kind: 'docx', exists: true, size: 18432, state: 'ok', outside_project: false, reason: null,
      },
      {
        path: `${PROJECT}\\2026-11-06 Письмо Лебедев — ответ\\черновик.md`,
        title: 'Черновик', kind: 'md', exists: false, size: null, state: 'missing',
        outside_project: false, reason: 'Файл удалён или перемещён',
      },
      {
        path: 'C:\\Users\\User\\AppData\\Local\\Temp\\work\\смета.xlsx',
        title: 'Смета (сохранена вне папки проекта)', kind: 'xlsx', exists: true, size: 4096,
        state: 'outside', outside_project: true, reason: 'Файл сохранён вне папки проекта',
      },
      {
        path: `${PROJECT}\\схема.png`,
        title: 'Схема', kind: 'image', exists: true, size: 2048, state: 'ok',
        outside_project: false, reason: null,
      },
    ],
  };
}

function mount() {
  installDom();
  return import('../web/js/views/request-parts.js');
}

function makeCtx(calls) {
  return {
    api: {
      post: async (url, body) => { calls.push({ url, body }); return { ok: true }; },
    },
    toast: () => {},
  };
}

test('артефакт в папке проекта: кнопки есть, путь относительный', async () => {
  const { renderArtifacts } = await mount();
  const calls = [];
  const card = renderArtifacts(detail(), makeCtx(calls), { projectDir: PROJECT });
  const text = textOf(card);
  assert.ok(text.includes('Письмо Лебедев — ответ'), text);
  assert.ok(text.includes('2026-11-06 Письмо Лебедев — ответ\\Письмо Лебедев — ответ.docx')
    || text.includes('Письмо Лебедев — ответ.docx'), 'путь показан относительно папки проекта');
  const buttons = walk(card, (n) => n.tagName === 'button').map(textOf);
  assert.ok(buttons.includes('Открыть'), buttons.join('|'));
  assert.ok(buttons.includes('Копировать путь'), buttons.join('|'));
  assert.ok(buttons.includes('В проводнике'), buttons.join('|'));
});

test('пропавший файл: «Файл удалён или перемещён», без кнопок открытия', async () => {
  const { renderArtifacts } = await mount();
  const card = renderArtifacts(detail(), makeCtx([]), { projectDir: PROJECT });
  const text = textOf(card);
  assert.ok(text.includes('Файл удалён или перемещён'), text);
  assert.ok(!text.includes('файл не найден'), 'старая подпись не должна оставаться');
  assert.ok(!text.includes('недоступен'), text);
  // у пропавшего файла остаётся только «Копировать путь»
  const items = walk(card, (n) => String(n.getAttribute('class') || '').split(/\s+/).includes('rp-art'));
  const missing = items.find((n) => textOf(n).includes('Черновик'));
  const actions = walk(missing, (n) => n.tagName === 'button').map(textOf);
  assert.ok(!actions.includes('Открыть'), actions.join('|'));
  assert.ok(actions.includes('Копировать путь'), actions.join('|'));
});

test('файл вне папки проекта: предупреждение и работающие кнопки (B-27)', async () => {
  const { renderArtifacts } = await mount();
  const calls = [];
  const card = renderArtifacts(detail(), makeCtx(calls), { projectDir: PROJECT });
  const text = textOf(card);
  assert.ok(text.includes('вне папки проекта'), text);
  assert.ok(text.includes('Сохранён вне папки проекта'), 'пояснение под путём');
  const items = walk(card, (n) => String(n.getAttribute('class') || '').split(/\s+/).includes('rp-art'));
  const outside = items.find((n) => textOf(n).includes('Смета'));
  const buttons = walk(outside, (n) => n.tagName === 'button');
  const labels = buttons.map(textOf);
  assert.ok(labels.includes('В проводнике'), labels.join('|'));
  const reveal = buttons.find((n) => textOf(n) === 'В проводнике');
  await (reveal.listeners.click || []).reduce((p, fn) => p.then(() => fn({})), Promise.resolve());
  assert.equal(calls.length, 1, 'одно действие — один запрос');
  assert.equal(calls[0].url, '/api/artifacts/open');
  assert.equal(calls[0].body.room, ROOM, 'комната уходит вместе с путём');
  assert.equal(calls[0].body.path, 'C:\\Users\\User\\AppData\\Local\\Temp\\work\\смета.xlsx');
});

test('адрес файла и картинки несёт комнату (папка проекта этой комнаты)', async () => {
  const { artifactUrl, renderArtifacts } = await mount();
  const url = artifactUrl('C:\\TeamRoom\\Agents\\default\\отчёт.md', ROOM);
  assert.ok(url.startsWith('/api/file?path='), url);
  assert.ok(url.includes('&room=fn'), url);
  assert.equal(artifactUrl('C:\\x\\отчёт.md', ''), '/api/file?path=' + encodeURIComponent('C:\\x\\отчёт.md'));

  const card = renderArtifacts(detail(), makeCtx([]), { projectDir: PROJECT });
  const img = walk(card, (n) => n.tagName === 'img')[0];
  assert.ok(img, 'у картинки есть превью');
  assert.ok(String(img.getAttribute('src')).includes('&room=fn'), img.getAttribute('src'));
});

test('без комнаты в детали адреса остаются прежними (совместимость)', async () => {
  const { artifactUrl } = await mount();
  assert.ok(!artifactUrl('C:\\x\\a.md', '').includes('room='));
});
