/**
 * phases-editor.test.mjs — этапы проекта: предложение руководителя и редактор
 * (W20 §1.7, F-2).
 *
 * Что проверяем:
 *  • плашка «Руководитель предлагает этапы» с четырьмя действиями —
 *    «Принять все / Принять выбранные / Изменить / Отклонить»;
 *  • «Принять выбранные» уходит с выбранными id, «Принять все» — без ids;
 *  • отказ сервера (уже решено) — человеческая фраза, без кодов;
 *  • редактор: добавить, переименовать, порядок кнопками «выше/ниже», удалить,
 *    счётчик «N из 24», пустые строки не сохраняются;
 *  • предложение внутри редактора: «Добавить выбранные / Добавить все» не
 *    дублирует уже существующие этапы;
 *  • «Предложить этапы»: карточка руководителю; пока предложение ждёт решения —
 *    понятная подсказка вместо второй карточки (409);
 *  • сохранение — `PATCH /api/rooms/{id} {phases}` (этапы = свойство комнаты);
 *  • точки вставки в карте проекта, «От вас нужно» и настройках комнаты на месте;
 *  • CSS: токены, нет текста мельче 11 px, узкий экран.
 *
 * DOM — мини-DOM из tests/fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';
import {
  MAX_PHASES, MAX_TITLE, DECISION_LABELS,
  slugId, phaseIdFor, normPhases, phasesPayload, movePhase, phaseCountLabel, phasesCountText,
  proposalOf, proposalPending, proposalHeadline, proposalIntro,
  decidePayload, decisionToast, proposalErrorText, proposePayload,
  createPhasesEditor, createProposalBanner, createProposeButton, openPhasesEditor,
} from '../web/js/views/phases-editor.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const css = (name) => readFileSync(join(HERE, '..', 'web', 'css', name), 'utf8');
const source = (rel) => readFileSync(join(HERE, '..', 'web', 'js', rel), 'utf8');

/* ── мини-DOM: то, чего нет в fake-dom.mjs ─────────────────────────────── */

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  doc.head = new FakeElement('head');
  doc.activeElement = null;
  doc.documentElement = new FakeElement('html');
  doc.getElementById = () => null;
  globalThis.document = doc;
  return doc;
}

FakeElement.prototype.focus = function focus() { globalThis.document.activeElement = this; };
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};
FakeElement.prototype.closest = function closest() { return null; };
FakeElement.prototype.append = function append(...nodes) {
  for (const node of nodes) if (node) this.appendChild(node);
};
FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
  this.childNodes = [];
  for (const node of nodes) if (node) this.appendChild(node);
};
FakeElement.prototype.removeChild = function removeChild(node) {
  const idx = this.childNodes.indexOf(node);
  if (idx === -1) throw new Error('removeChild: нет такого узла');
  this.childNodes.splice(idx, 1);
  node.parentNode = null;
  return node;
};
FakeElement.prototype.remove = function remove() {
  if (this.parentNode) this.parentNode.removeChild(this);
};
// Как в браузере: атрибут value — это и есть значение поля.
Object.defineProperty(FakeElement.prototype, 'value', {
  get() { return this.getAttribute('value') === null ? '' : this.getAttribute('value'); },
  set(v) { this.setAttribute('value', String(v)); },
});

/* ── помощники ─────────────────────────────────────────────────────────── */

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

function all(node, out = []) {
  if (!node) return out;
  if (node.nodeType === 1) out.push(node);
  for (const child of node.childNodes || []) all(child, out);
  return out;
}

function byText(root, label) {
  const want = String(label);
  const found = all(root).filter((el) => text(el).trim() === want);
  // Кнопка, а не подпись внутри неё: клик по span никуда не уходит.
  const clickable = found.filter((el) => String(el.tagName).toLowerCase() === 'button'
    || (el.listeners && el.listeners.click && el.listeners.click.length));
  return clickable[clickable.length - 1] || found[found.length - 1] || null;
}

function click(el) {
  assert.ok(el, 'кнопка для клика не найдена');
  el.dispatch('click', { type: 'click', target: el, preventDefault() {}, stopPropagation() {} });
}

function type(el, value) {
  el.value = value;
  el.dispatch('input', { type: 'input', target: el });
}

async function tick(n = 4) {
  for (let i = 0; i < n; i += 1) await new Promise((r) => setTimeout(r, 0));
}

function fakeApi(handlers = {}) {
  const calls = [];
  const api = {
    calls,
    async get(path) {
      calls.push(['GET', path, null]);
      if (handlers.get) return handlers.get(path);
      return {};
    },
    async post(path, body) {
      calls.push(['POST', path, body]);
      if (handlers.post) return handlers.post(path, body);
      return { ok: true };
    },
    async patch(path, body) {
      calls.push(['PATCH', path, body]);
      if (handlers.patch) return handlers.patch(path, body);
      return { id: 'r1', phases: body && body.phases };
    },
  };
  return api;
}

const PROPOSAL = {
  at: 1770000000,
  by: 'lead',
  state: 'proposed',
  items: [
    { id: 'ideya', title: 'Идея и вывеска', description: 'что проверяем и каким способом' },
    { id: 'spros', title: 'Проверка спроса', description: 'мини-опрос у стойки' },
    { id: 'prodazhi', title: 'Первые продажи', description: 'канал, цена, первая партия' },
  ],
};

/* ── чистые помощники ──────────────────────────────────────────────────── */

test('id этапа: из названия, латиница/кириллица, при переименовании не рвётся', () => {
  assert.equal(slugId('Идея и вывеска'), 'идея-и-вывеска');
  assert.equal(slugId('  Продажи  '), 'продажи');
  assert.equal(slugId(''), 'etap');
  assert.ok(slugId('Очень длинное название этапа проекта на восемьдесят знаков подряд').length <= 48);
  assert.equal(phaseIdFor('идея', new Set(['идея'])), 'идея-2');
  assert.equal(phaseIdFor('идея', new Set(['идея', 'идея-2'])), 'идея-3');
  assert.equal(phaseIdFor('идея', new Set()), 'идея');
});

test('normPhases: мусор и повторы отбрасываются, порядок и id сохраняются', () => {
  const out = normPhases([
    { title: 'Идея', id: 'ideya' },
    { title: 'Идея' },
    { title: '', id: 'empty' },
    null,
    'строка',
    { id: 'spros', title: '  Спрос  ' },
  ]);
  assert.deepEqual(out.map((p) => p.title), ['Идея', 'Идея', 'Спрос']);
  assert.equal(out[0].id, 'ideya');
  assert.notEqual(out[1].id, 'ideya', 'повтор id получает свой');
  assert.equal(out[2].id, 'spros');
  const long = 'Я'.repeat(MAX_TITLE + 30);
  assert.equal(normPhases([{ title: long }])[0].title.length, MAX_TITLE);
  const many = Array.from({ length: MAX_PHASES + 5 }, (_, i) => ({ title: 'Этап ' + i }));
  assert.equal(normPhases(many).length, MAX_PHASES);
  assert.deepEqual(normPhases('нет'), []);
});

test('phasesPayload: обрезает, выбрасывает пустые, id уникальные', () => {
  const out = phasesPayload([
    { id: 'x', title: '  Идея  ' },
    { title: '' },
    { id: 'x', title: 'Запуск' },
  ]);
  // Повтор id: этап получает свой id из названия — переименование не склеивает этапы.
  assert.deepEqual(out, [{ id: 'x', title: 'Идея' }, { id: 'запуск', title: 'Запуск' }]);
});

test('movePhase: порядок меняется, границы не рвутся', () => {
  const list = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
  assert.deepEqual(movePhase(list, 0, 1).map((p) => p.id), ['b', 'a', 'c']);
  assert.deepEqual(movePhase(list, 2, -1).map((p) => p.id), ['a', 'c', 'b']);
  assert.deepEqual(movePhase(list, 0, -1).map((p) => p.id), ['a', 'b', 'c']);
  assert.deepEqual(movePhase(list, 2, 1).map((p) => p.id), ['a', 'b', 'c']);
  assert.deepEqual(movePhase(list, 9, 1).map((p) => p.id), ['a', 'b', 'c']);
  assert.deepEqual(list.map((p) => p.id), ['a', 'b', 'c'], 'исходный список не меняется');
});

test('счётчики: «12 из 24» и «3 этапа»', () => {
  assert.equal(phaseCountLabel(12), '12 из 24');
  assert.equal(phasesCountText(1), '1 этап');
  assert.equal(phasesCountText(3), '3 этапа');
  assert.equal(phasesCountText(11), '11 этапов');
  assert.equal(phasesCountText(0), '0 этапов');
});

test('proposalOf: терпим к мусору, описания на месте, состояние строгое', () => {
  const p = proposalOf(PROPOSAL);
  assert.equal(p.state, 'proposed');
  assert.equal(p.items.length, 3);
  assert.equal(p.items[1].description, 'мини-опрос у стойки');
  assert.equal(proposalOf(null), null);
  assert.equal(proposalOf({ state: 'waiting', items: [{ title: 'A' }] }), null, 'неизвестное состояние — нет предложения');
  assert.equal(proposalOf({ state: 'proposed', items: [] }), null, 'пустое предложение не показываем');
  assert.equal(proposalOf({ state: 'proposed', items: [{ title: 'A' }, { title: '' }] }).items.length, 1);
  assert.equal(proposalPending(PROPOSAL), true);
  assert.equal(proposalPending({ state: 'accepted', items: [{ title: 'A' }] }), false);
  assert.equal(proposalHeadline(PROPOSAL), 'Руководитель предлагает этапы');
  assert.match(proposalIntro(PROPOSAL), /допишутся в конец/);
});

test('decidePayload: три решения, выбранные — без повторов', () => {
  assert.deepEqual(decidePayload('accept'), { action: 'accept' });
  assert.deepEqual(decidePayload('reject'), { action: 'reject' });
  assert.deepEqual(decidePayload('accept_selected', ['b', 'a', 'b']), { action: 'accept_selected', ids: ['b', 'a'] });
  assert.throws(() => decidePayload('accept_selected', []), /Не выбран ни один/);
  assert.throws(() => decidePayload('delete_all'), /Неизвестное решение/);
});

test('decisionToast и ошибки: по-русски, без кодов', () => {
  assert.equal(decisionToast({ text: 'Этапы приняты (2 этапа)' }), 'Этапы приняты (2 этапа)');
  assert.equal(decisionToast({ action: 'reject' }), 'Предложение этапов отклонено');
  assert.equal(decisionToast({}), 'Этапы сохранены');
  const conflict = { status: 409, details: { error: { detail: { reason: 'already_decided' } } } };
  assert.match(proposalErrorText(conflict), /Решение по этому предложению уже принято/);
  const pending = { details: { error: { detail: { reason: 'proposal_pending' } } } };
  assert.match(proposalErrorText(pending), /уже предложил этапы/);
  assert.match(proposalErrorText({ message: 'Комната в архиве — этапы не меняются' }), /в архиве/);
  assert.match(proposalErrorText({}), /попробуйте ещё раз/);
  assert.ok(!/409|409/.test(proposalErrorText(conflict)), 'код не показываем');
});

test('proposePayload: пустое пояснение не отправляем, длинное — режем', () => {
  assert.deepEqual(proposePayload('   '), {});
  assert.deepEqual(proposePayload(null), {});
  assert.deepEqual(proposePayload('  учесть  '), { note: 'учесть' });
  assert.equal(proposePayload('я'.repeat(2500)).note.length, 2000);
});

/* ── редактор ──────────────────────────────────────────────────────────── */

test('редактор: список, счётчик, переименование, порядок, удаление, добавление', () => {
  installDom();
  const changes = [];
  const editor = createPhasesEditor({
    phases: [{ id: 'a', title: 'Идея' }, { id: 'b', title: 'Запуск' }],
    onChange: (list) => changes.push(list),
  });
  const rows = () => all(editor.el).filter((el) => el.classList.contains('pe-row'));
  assert.equal(rows().length, 2);
  assert.match(text(editor.el), /2 из 24/);
  const inputs = all(editor.el).filter((el) => String(el.tagName).toLowerCase() === 'input');
  assert.deepEqual(inputs.map((i) => i.value), ['Идея', 'Запуск']);

  // Переименование: id сохраняется — привязка данных не рвётся.
  type(inputs[1], 'Запуск и продажи');
  assert.deepEqual(editor.getPhases(), [{ id: 'a', title: 'Идея' }, { id: 'b', title: 'Запуск и продажи' }]);
  assert.ok(changes.length > 0);

  // «Ниже» у первого этапа — порядок меняется.
  const down = all(rows()[0]).find((el) => el.getAttribute('title') === 'Ниже');
  click(down);
  assert.deepEqual(editor.getPhases().map((p) => p.id), ['b', 'a']);

  // «Выше» у первого — обратно.
  const up = all(rows()[1]).find((el) => el.getAttribute('title') === 'Выше');
  click(up);
  assert.deepEqual(editor.getPhases().map((p) => p.id), ['a', 'b']);

  // Удаление.
  const del = all(rows()[0]).find((el) => el.getAttribute('title') === 'Убрать этап');
  click(del);
  assert.equal(editor.getPhases().length, 1);
  assert.match(text(editor.el), /1 из 24/);

  // Добавление: пустая строка в модели есть, в сохранение не попадает.
  click(editor.addButton);
  assert.equal(rows().length, 2);
  assert.match(text(editor.el), /2 из 24/);
  assert.equal(editor.getPhases().length, 1, 'пустой этап не сохраняется');
  assert.equal(editor.isDirty(), true);
});

test('редактор: не больше 24 этапов, «Добавить» предупреждает', () => {
  installDom();
  const messages = [];
  const phases = Array.from({ length: MAX_PHASES }, (_, i) => ({ id: 'p' + i, title: 'Этап ' + i }));
  const editor = createPhasesEditor({ phases, toast: (m) => messages.push(m) });
  click(editor.addButton);
  assert.equal(editor.getPhases().length, MAX_PHASES);
  assert.match(messages.join(' '), /Не больше 24 этапов/);
});

test('редактор: этапы из предложения руководителя добавляются без дублей', () => {
  installDom();
  const messages = [];
  const editor = createPhasesEditor({
    phases: [{ id: 'ideya', title: 'Идея и вывеска' }],
    proposal: PROPOSAL,
    toast: (m) => messages.push(m),
  });
  assert.match(text(editor.el), /Предложение руководителя/);
  const addAll = byText(editor.el, 'Добавить все');
  click(addAll);
  const titles = editor.getPhases().map((p) => p.title);
  assert.deepEqual(titles, ['Идея и вывеска', 'Проверка спроса', 'Первые продажи'], 'совпавший этап не дублируется');
  assert.match(messages.join(' '), /Добавлено: 2 этапа/);
  assert.match(messages.join(' '), /Уже были/);

  // Выбранные: снимаем отметку — добавляется только отмеченное.
  installDom();
  const editor2 = createPhasesEditor({ phases: [], proposal: PROPOSAL, toast: () => {} });
  const boxes = all(editor2.el).filter((el) => el.getAttribute('type') === 'checkbox');
  boxes[1].checked = false;
  boxes[1].dispatch('change', { type: 'change', target: boxes[1] });
  click(byText(editor2.el, 'Добавить выбранные (2)'));
  assert.deepEqual(editor2.getPhases().map((p) => p.title), ['Идея и вывеска', 'Первые продажи']);
});

test('редактор: setPhases и isDirty считают сохранённое состояние', () => {
  installDom();
  const editor = createPhasesEditor({ phases: [{ id: 'a', title: 'Идея' }] });
  assert.equal(editor.isDirty(), false);
  const input = all(editor.el).find((el) => String(el.tagName).toLowerCase() === 'input');
  type(input, 'Идея и вывеска');
  assert.equal(editor.isDirty(), true);
  editor.setPhases([{ id: 'a', title: 'Идея и вывеска' }]);
  assert.equal(editor.isDirty(), false);
  assert.equal(editor.rowCount(), 1);
});

/* ── плашка предложения ────────────────────────────────────────────────── */

test('плашка: заголовок, все этапы, четыре действия', () => {
  installDom();
  const banner = createProposalBanner({ proposal: PROPOSAL, roomId: 'r1', api: fakeApi(), toast: () => {} });
  const body = text(banner);
  assert.match(body, /Руководитель предлагает этапы/);
  assert.match(body, /3 этапа/);
  for (const label of Object.values(DECISION_LABELS)) assert.ok(byText(banner, label), 'нет действия: ' + label);
  for (const item of PROPOSAL.items) assert.match(body, new RegExp(item.title));
  assert.match(body, /Ваши этапы, их порядок и названия не изменятся/);
});

test('плашка: «Принять все» отправляет accept и обновляет карту', async () => {
  installDom();
  const api = fakeApi({ post: () => ({ ok: true, action: 'accept', state: 'accepted', text: 'Этапы приняты (3 этапа)' }) });
  const messages = [];
  const decided = [];
  const banner = createProposalBanner({
    proposal: PROPOSAL, roomId: 'r1', api,
    toast: (m) => messages.push(m),
    onDecided: (res) => decided.push(res),
  });
  click(byText(banner, DECISION_LABELS.accept));
  await tick();
  const call = api.calls.find((c) => c[0] === 'POST');
  assert.equal(call[1], '/api/rooms/r1/phases/proposal');
  assert.deepEqual(call[2], { action: 'accept' });
  assert.equal(messages[0], 'Этапы приняты (3 этапа)');
  assert.equal(decided.length, 1);
});

test('плашка: «Принять выбранные» уходит только с отмеченными этапами', async () => {
  installDom();
  const api = fakeApi({ post: () => ({ ok: true, action: 'accept_selected', text: 'Этапы приняты (2 этапа)' }) });
  const banner = createProposalBanner({ proposal: PROPOSAL, roomId: 'r1', api, toast: () => {} });
  const boxes = all(banner).filter((el) => el.getAttribute('type') === 'checkbox');
  assert.equal(boxes.length, 3);
  boxes[2].checked = false;                       // «Первые продажи» — не берём
  boxes[2].dispatch('change', { type: 'change', target: boxes[2] });
  click(byText(banner, 'Принять выбранные (2)'));
  await tick();
  const call = api.calls.find((c) => c[0] === 'POST');
  assert.deepEqual(call[2], { action: 'accept_selected', ids: ['ideya', 'spros'] });
});

test('плашка: «Изменить» зовёт редактор, «Отклонить» — с подтверждением', async () => {
  installDom();
  const api = fakeApi({ post: () => ({ ok: true, action: 'reject', text: 'Предложение этапов отклонено (3 этапа)' }) });
  let edited = 0;
  let asked = 0;
  const decided = [];
  const banner = createProposalBanner({
    proposal: PROPOSAL, roomId: 'r1', api, toast: () => {},
    onEdit: () => { edited += 1; },
    onDecided: (res) => decided.push(res),
    confirmReject: async () => { asked += 1; return true; },
  });
  click(byText(banner, 'Изменить'));
  assert.equal(edited, 1, '«Изменить» открывает общий редактор');
  click(byText(banner, DECISION_LABELS.reject));
  await tick();
  assert.equal(asked, 1, 'отклонение спрашивает подтверждение');
  const call = api.calls.find((c) => c[0] === 'POST');
  assert.deepEqual(call[2], { action: 'reject' });
  assert.equal(decided.length, 1);
});

test('плашка: отказ «уже решено» — человеческая фраза и перечитывание', async () => {
  installDom();
  const api = fakeApi({
    post: () => {
      const err = new Error('already decided');
      err.status = 409;
      err.details = { error: { detail: { reason: 'already_decided' } } };
      throw err;
    },
  });
  const messages = [];
  const decided = [];
  const banner = createProposalBanner({
    proposal: PROPOSAL, roomId: 'r1', api,
    toast: (m) => messages.push(m),
    onDecided: (res) => decided.push(res),
  });
  click(byText(banner, DECISION_LABELS.accept));
  await tick();
  assert.match(messages.join(' '), /Решение по этому предложению уже принято/);
  assert.equal(decided[0], null, 'после отказа карта перечитывается');
});

/* ── окно редактора и сохранение ───────────────────────────────────────── */

test('окно редактора: сохраняет этапы свойством комнаты (PATCH)', async () => {
  installDom();
  const api = fakeApi({ patch: () => ({ id: 'r1', phases: [{ id: 'a', title: 'Идея и вывеска' }] }) });
  const messages = [];
  const done = openPhasesEditor({
    api, roomId: 'r1',
    phases: [{ id: 'a', title: 'Идея' }],
    toast: (m) => messages.push(m),
  });
  await tick();
  const overlay = globalThis.document.body.childNodes.find((n) => n.classList && n.classList.contains('dialog-backdrop'));
  assert.ok(overlay, 'окно смонтировано');
  const input = all(overlay).find((el) => String(el.tagName).toLowerCase() === 'input');
  type(input, 'Идея и вывеска');
  click(byText(overlay, 'Сохранить этапы'));
  const saved = await done;
  const call = api.calls.find((c) => c[0] === 'PATCH');
  assert.equal(call[1], '/api/rooms/r1');
  assert.deepEqual(call[2], { phases: [{ id: 'a', title: 'Идея и вывеска' }] });
  assert.equal(saved.phases[0].title, 'Идея и вывеска');
  assert.match(messages.join(' '), /Этапы сохранены/);
});

test('окно редактора: читает комнату, если список этапов не передан', async () => {
  installDom();
  const api = fakeApi({
    get: () => ({ id: 'r1', phases: [{ id: 'a', title: 'Идея' }], phases_proposal: PROPOSAL }),
    patch: () => ({ id: 'r1', phases: [{ id: 'a', title: 'Идея' }] }),
  });
  const done = openPhasesEditor({ api, roomId: 'r1', toast: () => {} });
  await tick(6);
  assert.ok(api.calls.some((c) => c[0] === 'GET' && c[1] === '/api/rooms/r1'));
  const overlay = globalThis.document.body.childNodes.find((n) => n.classList && n.classList.contains('dialog-backdrop'));
  assert.ok(overlay);
  click(byText(overlay, 'Отмена'));
  assert.equal(await done, null);
});

/* ── кнопка «Предложить этапы» ─────────────────────────────────────────── */

test('«Предложить этапы»: карточка руководителю и 409, пока ждёт решения', async () => {
  installDom();
  const sent = [];
  const api = fakeApi({ post: () => ({ ok: true, action: 'phases_propose', root_id: 't_0a1b2c3d' }) });
  const btn = createProposeButton({ api, roomId: 'r1', toast: () => {}, onSent: (res) => sent.push(res) });
  click(btn);
  await tick();
  const call = api.calls.find((c) => c[0] === 'POST');
  assert.equal(call[1], '/api/rooms/r1/phases/propose');
  assert.equal(sent[0].root_id, 't_0a1b2c3d');

  // Второй раз, пока предложение ждёт решения: сервер отвечает 409.
  const messages = [];
  const api409 = fakeApi({
    post: () => {
      const err = new Error('proposal pending');
      err.status = 409;
      err.details = { error: { detail: { reason: 'proposal_pending' } } };
      throw err;
    },
  });
  const btn2 = createProposeButton({ api: api409, roomId: 'r1', toast: (m) => messages.push(m), onSent: (res) => sent.push(res) });
  click(btn2);
  await tick();
  assert.match(messages.join(' '), /уже предложил этапы/);
  assert.equal(sent[sent.length - 1], null);
});

/* ── точки вставки в общих файлах ──────────────────────────────────────── */

test('карта проекта: плашка, кнопки «Изменить этапы» и «Предложить этапы»', () => {
  const src = source('views/roadmap.js');
  assert.match(src, /from '\.\/phases-editor\.js'/, 'карта берёт общий модуль этапов');
  assert.match(src, /createProposalBanner/);
  assert.match(src, /'Изменить этапы'/);
  assert.match(src, /proposeButton\(\)/);
  assert.match(src, /openPhasesEditor\(/, 'редактор этапов — один на приложение');
  assert.ok(!/openRoomSettings\('phases'\)/.test(src), '«Изменить этапы» больше не уводит в настройки комнаты');
});

test('«От вас нужно»: строка предложения из proposal_item с решениями', () => {
  const src = source('views/backlog.js');
  assert.match(src, /proposal_item/);
  assert.match(src, /'Принять все'/);
  assert.match(src, /'Отклонить'/);
  assert.match(src, /openPhasesEditor/);
  assert.match(src, /phases-editor\.css/);
});

test('настройки комнаты: вкладка «Проект» показывает тот же редактор', () => {
  const src = source('views/room-dialog.js');
  assert.match(src, /import \{ createPhasesEditor \} from '\.\/phases-editor\.js'/);
  assert.match(src, /phasesEditor\.getPhases\(\)/, 'правки уходят в PATCH комнаты');
  assert.ok(!/roomdlg__phase-actions/.test(src), 'старый отдельный редактор убран');
});

test('сохранение комнаты будит карту: событие room-saved с обеих сторон', () => {
  const dialog = source('views/room-dialog.js');
  const map = source('views/roadmap.js');
  assert.match(dialog, /teamroom:room-saved/, 'диалог сообщает о сохранении');
  assert.match(map, /addEventListener\('teamroom:room-saved'/, 'карта слушает сохранение');
  assert.match(map, /removeEventListener\('teamroom:room-saved'/, 'слушатель снимается при уходе со вкладки');
});

test('CSS: токены, нет мелкого текста, узкий экран', () => {
  const sheet = css('phases-editor.css');
  for (const cls of ['.pe-offer', '.pe-offer__actions', '.pe-row', '.pe-editor__list', '.pe-offer-inline', '.pe-wait']) {
    assert.ok(sheet.includes(cls), 'нет стиля: ' + cls);
  }
  assert.match(sheet, /@media \(max-width: 720px\)/);
  const sizes = [...sheet.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
  assert.deepEqual(sizes.filter((n) => n < 11), [], 'читаемый текст мельче 11 px');
  assert.match(sheet, /var\(--space-/, 'отступы из токенов');
  assert.match(sheet, /var\(--text-sm\)/);
});
