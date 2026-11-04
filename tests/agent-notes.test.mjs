/**
 * agent-notes.test.mjs — «Особые указания для этой комнаты» (WAVE9-RULES §9).
 *
 * Проверяем интерфейс, а не сервер:
 *  • у каждого участника (руководитель, критик, исполнитель) есть раскрывающееся поле;
 *  • счётчик «N из 8000», предупреждение при переполнении, обрезка при сохранении;
 *  • индикатор «есть указания» и примеры-подсказки, которые вставляются в поле;
 *  • микрофон автоматически: на поле НЕТ data-mic="off", а справа оставлено 40px;
 *  • value() отдаёт только участников с текстом; composition меняется — поле следует;
 *  • строка на вкладке «Команда»: «не заданы» / «есть у N участников» + «Показать».
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, FakeText, createFakeDocument } from './fake-dom.mjs';
import { mountAgentNotes, notesSummaryLine, notesOf, participantsOf, MAX_TEXT } from '../web/js/agent-notes-ui.js';

/* ───────────────────────────────────────────────────────── мини-DOM */

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
  // без head: ensureCss() считает стили подключёнными и не ждёт события load
  globalThis.document = doc;
  return doc;
}

const ROOM = {
  id: 'cafe',
  title: 'Лето',
  orchestrator: 'biz-director',
  critic: 'biz-critic',
  members: [
    { profile: 'biz-lawyer', role: 'worker' },
    { profile: 'biz-cfo', role: 'worker' },
  ],
  agent_notes: {
    'biz-lawyer': { text: 'Всегда проверяй по закону X', updated_at: 1700000000 },
    'biz-ghost': { text: 'указание убранного участника' },
    'biz-cfo': { text: '   ' },
  },
};

const ctx = { api: {}, toast: () => {} };

/** Обход дерева мини-DOM: все узлы, подходящие под предикат. */
function walk(node, pred, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    if (pred(child)) out.push(child);
    walk(child, pred, out);
  }
  return out;
}

/* ─────────────────────────────────────────────────────────── данные */

test('notesOf: строки и объекты, пустое не считается', () => {
  const notes = notesOf(ROOM);
  assert.deepEqual(Object.keys(notes).sort(), ['biz-ghost', 'biz-lawyer']);
  assert.equal(notes['biz-lawyer'].text, 'Всегда проверяй по закону X');
  assert.equal(notes['biz-lawyer'].updated_at, 1700000000);
  assert.deepEqual(notesOf(null), {});
  assert.deepEqual(notesOf({ agent_notes: 'мусор' }), {});
});

test('participantsOf: руководитель, критик, исполнители без дублей', () => {
  const people = participantsOf(ROOM).map((p) => p.profile + ':' + p.role);
  assert.deepEqual(people, ['biz-director:orchestrator', 'biz-critic:critic',
    'biz-lawyer:worker', 'biz-cfo:worker']);
  const dup = participantsOf({
    orchestrator: 'biz-director', critic: null,
    members: [{ profile: 'biz-director', role: 'worker' }],
  }).map((p) => p.profile);
  assert.deepEqual(dup, ['biz-director']);
});

/* ─────────────────────────────────────────────────────── монтирование */

test('поле у каждого участника: подписи, счётчик, индикатор', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountAgentNotes(host, ctx, { room: ROOM });

  const rows = host.find('div').parentNode;   // просто убеждаемся, что дерево собрано
  assert.ok(rows);
  const text = host.textContent;
  for (const profile of ['biz-director', 'biz-critic', 'biz-lawyer', 'biz-cfo']) {
    assert.ok(text.includes(profile), 'нет участника ' + profile);
  }
  assert.ok(text.includes('Особые указания для этой комнаты'), 'нет подписи раскрытия');
  assert.ok(text.includes('Отвечай коротко и только цифрами'), 'нет примеров-подсказок');
  assert.ok(text.includes('из 8000'), 'нет счётчика «N из 8000»');

  // индикатор «есть указания» — только у участника с текстом
  assert.equal(ui.has('biz-lawyer'), true);
  assert.equal(ui.has('biz-cfo'), false);
  assert.equal(ui.counterFor('biz-lawyer'), String('Всегда проверяй по закону X'.length) + ' из ' + MAX_TEXT);
  assert.equal(ui.counterFor('biz-cfo'), '0 из ' + MAX_TEXT);
  assert.deepEqual(ui.value(), { 'biz-lawyer': 'Всегда проверяй по закону X' });
});

test('ввод текста: value(), onChange, счётчик и переполнение', async () => {
  installDom();
  const host = new FakeElement('div');
  const seen = [];
  const ui = await mountAgentNotes(host, ctx, {
    room: ROOM,
    onChange: (profile, text) => seen.push([profile, text]),
  });

  assert.equal(ui.setText('biz-cfo', 'Отвечай коротко и только цифрами'), true);
  assert.deepEqual(seen.pop(), ['biz-cfo', 'Отвечай коротко и только цифрами']);
  assert.equal(ui.counterFor('biz-cfo'), 'Отвечай коротко и только цифрами'.length + ' из ' + MAX_TEXT);
  assert.equal(ui.has('biz-cfo'), true);

  // значение обрезается до 8000 при сохранении, поле — без жёсткого лимита
  ui.setText('biz-cfo', 'я'.repeat(MAX_TEXT + 50));
  assert.equal(ui.value()['biz-cfo'].length, MAX_TEXT);
  assert.match(host.textContent, /Длиннее 8000 знаков не сохранится/);
});

test('состав изменился — поле следует (refresh), пример вставляется в раскрытое', async () => {
  installDom();
  const host = new FakeElement('div');
  let people = [{ profile: 'biz-lawyer', role: 'worker' }];
  const ui = await mountAgentNotes(host, ctx, {
    room: ROOM,
    participants: () => people,
  });
  assert.equal(ui.setText('biz-lawyer', 'Коротко'), true);

  people = [
    { profile: 'biz-lawyer', role: 'worker' },
    { profile: 'biz-cfo', role: 'worker' },
  ];
  ui.refresh();
  assert.equal(ui.setText('biz-cfo', 'Цифрами'), true);
  assert.deepEqual(Object.keys(ui.value()).sort(), ['biz-cfo', 'biz-lawyer']);

  people = [{ profile: 'biz-cfo', role: 'worker' }];
  ui.refresh();
  assert.deepEqual(ui.value(), { 'biz-cfo': 'Цифрами' },
    'указания выведенного из состава участника в комнату не уходят');

  // первый пример вставляется в поле первого участника
  const example = walk(host, (n) => n.tagName === 'button'
    && String(n.className).includes('agent-notes__example'))[0];
  assert.ok(example, 'нет кнопки-примера');
  example.dispatch('click', { type: 'click', target: example });
  assert.match(ui.textFor('biz-cfo'), /Отвечай коротко и только цифрами/);
});

test('микрофон: поле не отключено и справа оставлено 40px', async () => {
  installDom();
  const host = new FakeElement('div');
  await mountAgentNotes(host, ctx, { room: ROOM });
  const area = host.find('textarea');
  assert.ok(area, 'нет поля ввода');
  assert.equal(area.getAttribute('data-mic'), null,
    'data-mic="off" запретил бы автоматический микрофон');
  const css = readFileSync(new URL('../web/css/agent-notes.css', import.meta.url), 'utf8');
  assert.match(css, /\.agent-note__area\s*{[^}]*padding-right:\s*40px/s,
    'кнопке микрофона нужно ~40px справа');
});

/* ─────────────────────────────────────────── строка на вкладке «Команда» */

test('строка «Команда»: «не заданы», иначе «есть у N» + «Показать»', () => {
  installDom();
  const empty = notesSummaryLine({ id: 'cafe', orchestrator: 'biz-director', members: [] });
  assert.match(empty.textContent, /Особые указания/);
  assert.match(empty.textContent, /не заданы/);

  const line = notesSummaryLine(ROOM);
  assert.match(line.textContent, /есть у 1 участника/);
  assert.match(line.textContent, /Всегда проверяй по закону X/);
  const btn = walk(line, (n) => n.tagName === 'button')[0];
  assert.ok(btn, 'нет кнопки «Показать»');
  assert.equal(btn.textContent.trim(), 'Показать');
  const list = walk(line, (n) => String(n.className || '').includes('agent-notes-line__list'))[0];
  assert.ok(list, 'нет свёрнутого списка указаний');
  assert.ok(String(list.className).includes('hidden'), 'список должен быть свёрнут');
  btn.dispatch('click', { type: 'click', target: btn });
  assert.ok(!String(list.className).includes('hidden'), 'после «Показать» список раскрыт');
  assert.equal(btn.textContent.trim(), 'Скрыть');
});
