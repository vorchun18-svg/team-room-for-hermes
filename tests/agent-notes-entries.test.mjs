/**
 * agent-notes-entries.test.mjs — «Особенности для этой комнаты» как СПИСОК записей
 * (WAVE14 пакет I, пункт 13).
 *
 * Проверяем:
 *  • старый формат `{профиль: {text}}` читается как одна закреплённая запись;
 *  • подпись записи: «закреплено» / кто добавил, источник, дата, «пригодилось N раз»;
 *  • редактор (не live-режим): список записей, «Добавить», счётчик записей и знаков,
 *    микрофон не отключён (data-mic не стоит), значение — склейка записей;
 *  • в «Команде» у участника есть «Особенности для этой комнаты» и редактор записей;
 *  • в окне «Изменить» существующая комната правит записи сразу (mountNotesLive).
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';
import {
  mountNotesEntries, entriesOfItem, entryMetaText, MAX_TEXT,
} from '../web/js/agent-notes-ui.js';

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
FakeElement.prototype.closest = function closest() { return null; };
FakeElement.prototype.focus = function focus() {};
FakeElement.prototype.blur = function blur() {};

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
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

const ROOM = { id: 'cafe', title: 'Лето', orchestrator: 'biz-director', members: [] };

const ENTRIES = [
  {
    id: 'n_1', text: 'Всегда проверяй по закону X', source: 'user', origin: 'ручная запись',
    created_at: 1700000000, used_count: 2, pinned: true,
  },
  {
    id: 'n_2', text: 'Ornith — слабая модель, но локальная: простые задачи ей', source: 'leader',
    origin: 'комментарий Михаила', created_at: 1700000000, used_count: 0, pinned: false,
  },
];

/* ─────────────────────────────────────────────────────────── данные */

test('entriesOfItem: старый текст — одна закреплённая запись', () => {
  const old = entriesOfItem({ text: 'Отвечай цифрами', updated_at: 1700000000 });
  assert.equal(old.length, 1);
  assert.equal(old[0].text, 'Отвечай цифрами');
  assert.equal(old[0].pinned, true);
  assert.equal(old[0].source, 'user');
  assert.deepEqual(entriesOfItem(null), []);
  assert.deepEqual(entriesOfItem({ text: '   ' }), []);
});

test('entriesOfItem: список записей сохраняет источник и закрепление', () => {
  const list = entriesOfItem({ entries: ENTRIES });
  assert.equal(list.length, 2);
  assert.equal(list[1].source, 'leader');
  assert.equal(list[1].pinned, false);
  assert.equal(list[0].used_count, 2);
});

test('entryMetaText: «закреплено · источник · дата · пригодилось»', () => {
  const meta = entryMetaText(ENTRIES[0]);
  assert.match(meta, /закреплено/);
  assert.match(meta, /источник: ручная запись/);
  assert.match(meta, /пригодилось 2 раз/);
  const lead = entryMetaText(ENTRIES[1]);
  assert.match(lead, /руководитель комнаты/);
  assert.doesNotMatch(lead, /пригодилось/);
});

/* ────────────────────────────────────────────────────── редактор записей */

test('редактор: список записей, счётчики и поле с микрофоном', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountNotesEntries(host, {}, { profile: 'biz-lawyer', entries: ENTRIES });
  const text = host.textContent;
  assert.match(text, /Всегда проверяй по закону X/);
  assert.match(text, /Ornith — слабая модель/);
  assert.match(text, /записей 2 из 24/);
  assert.match(text, new RegExp('из ' + MAX_TEXT + ' знаков'));
  assert.match(text, /Действует только в этой комнате/);

  const area = host.find('textarea');
  assert.ok(area, 'нет поля «Добавить»');
  assert.equal(area.getAttribute('data-mic'), null, 'микрофон должен ставиться автоматически');
});

test('редактор: «Добавить» дописывает запись и склеивает value()', async () => {
  installDom();
  const host = new FakeElement('div');
  const seen = [];
  const ui = await mountNotesEntries(host, {}, {
    profile: 'biz-lawyer', entries: ENTRIES,
    onChange: (profile, text) => seen.push([profile, text]),
  });
  const area = host.find('textarea');
  area.value = 'Не согласовывай сроки без меня';
  const add = walk(host, (n) => n.tagName === 'button' && /Добавить/.test(n.textContent))[0];
  assert.ok(add, 'нет кнопки «Добавить»');
  add.dispatch('click', { type: 'click', target: add });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(ui.count(), 3);
  assert.match(ui.value(), /Не согласовывай сроки без меня/);
  assert.match(host.textContent, /записей 3 из 24/);
  assert.equal(seen.length, 1);
  assert.equal(seen[0][0], 'biz-lawyer');
});

test('редактор: пустое поле не добавляет запись', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountNotesEntries(host, {}, { profile: 'biz-lawyer', entries: [] });
  assert.equal(ui.count(), 0);
  const add = walk(host, (n) => n.tagName === 'button' && /Добавить/.test(n.textContent))[0];
  add.dispatch('click', { type: 'click', target: add });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(ui.count(), 0);
  assert.match(host.textContent, /Напишите или надиктуйте/);
});

/* ─────────────────────────────────────────── видимое место на карточке */

test('«Команда»: у участника есть блок «Особенности для этой комнаты»', () => {
  const src = readFileSync(new URL('../web/js/views/team.js', import.meta.url), 'utf8');
  assert.match(src, /Особенности для этой комнаты/);
  assert.match(src, /mountNotesEntries/);
  assert.match(src, /tm-card__notes/);
  const css = readFileSync(new URL('../web/css/agent-notes.css', import.meta.url), 'utf8');
  assert.match(css, /\.notes-entry\b/, 'нет стилей записи');
  assert.match(css, /\.tm-card__notes\b/, 'нет стилей блока на карточке');
  assert.match(css, /\.agent-note__area\s*{[^}]*padding-right:\s*40px/s,
    'кнопке микрофона нужно ~40px справа');
});

test('окно «Изменить»: существующая комната правит записи сразу', () => {
  const src = readFileSync(new URL('../web/js/views/room-dialog.js', import.meta.url), 'utf8');
  assert.match(src, /mountNotesLive/);
  assert.match(src, /const notesLive = Boolean\(full && full\.id\)/);
  assert.match(src, /if \(!notesLive\)/, 'старый полный текст не должен перезаписывать записи');
  assert.match(src, /Особенности для этой комнаты/);
  assert.match(src, /журнал/i);
  assert.ok(ROOM);
});
