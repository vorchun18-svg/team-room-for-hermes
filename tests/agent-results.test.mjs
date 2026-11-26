/**
 * agent-results.test.mjs — блок «Результаты агентов» в задаче (W30-E).
 *
 * Проверяем то, из-за чего дефект и появился: под «Сводом» видно КАЖДОГО
 * исполнителя, его вывод, файлы с путями и «что осталось»; кнопки — существующие
 * ручки (`/api/files/launch`), «Просмотр» открывает панель вложений; пустое
 * состояние объясняет, что будет дальше; у родителя проекта есть свёртка по
 * подзадачам со ссылками.
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs): innerHTML запрещён, браузер
 * не нужен.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { FakeElement, createFakeDocument, expandSections } from './fake-dom.mjs';

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
  globalThis.window = globalThis.window || {};
  globalThis.localStorage = {
    store: new Map(),
    getItem(k) { return this.store.has(k) ? this.store.get(k) : null; },
    setItem(k, v) { this.store.set(k, String(v)); },
    removeItem(k) { this.store.delete(k); },
  };
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

function buttonsIn(node) {
  return walk(node, (n) => String(n.tagName).toLowerCase() === 'button');
}

function payload() {
  return {
    counts: { agents: 3, hidden_agents: 0, files: 3, agents_with_files: 2, remaining: 2, done: 2 },
    agents: [
      {
        agent: 'team-lawyer', role: 'исполнитель', role_raw: 'worker',
        title: 'Юр-блок: аренда и договор', status: 'done', status_label: 'готово',
        verdict: 'Договор аренды проверен: два пункта переписать, остальное годится.',
        remaining: 'Получить подписанный экземпляр; согласовать штрафы',
        verdict_tag: '', files: [
          { path: 'C:\\Проекты\\Химчистка\\01-legal.md', name: '01-legal.md', exists: true },
        ], files_total: 1, files_hidden: 0, files_mirrors: 0, card_id: 't_aa000001',
      },
      {
        agent: 'team-ops', role: 'исполнитель', role_raw: 'worker',
        title: 'Оборудование: три варианта', status: 'running', status_label: 'в работе',
        verdict: '', remaining: '',
        verdict_tag: '', files: [], files_total: 0, files_hidden: 0, files_mirrors: 0,
        card_id: 't_aa000002',
      },
      {
        agent: 'team-critic', role: 'критик', role_raw: 'critic',
        title: 'Критика ответа (раунд 1)', status: 'done', status_label: 'готово',
        verdict: 'Вердикт: доработать — нет срока по договору.',
        remaining: 'Сверить штрафы с котировками', verdict_tag: 'revise',
        files: [{ path: 'C:\\Проекты\\Химчистка\\review.md', name: 'review.md', exists: false }],
        files_total: 1, files_hidden: 0, files_mirrors: 0, card_id: 't_aa000003',
      },
    ],
  };
}

function detail(extra = {}) {
  return {
    id: 't_aa000000', room: 'clean', status: 'done',
    agent_results: payload(),
    ...extra,
  };
}

function makeCtx(calls = []) {
  return {
    api: {
      post: async (url, body) => { calls.push({ url, body }); return { ok: true }; },
      get: async (url) => { calls.push({ url }); return { agents: [] }; },
    },
    toast: (text, kind) => { calls.push({ toast: text, kind }); },
  };
}

async function mount() {
  installDom();
  const mod = await import('../web/js/views/agent-results.js');
  return mod;
}

test('блок под сводом: каждый агент, вывод, «что осталось», файлы', async () => {
  const { renderAgentResults } = await mount();
  const el = expandSections(renderAgentResults(detail(), makeCtx(), { roomId: 'clean' }));
  const text = textOf(el);
  assert.ok(text.includes('Результаты агентов'), text.slice(0, 200));
  // все три исполнителя видны сразу, без переходов
  assert.ok(text.includes('Юр-блок: аренда и договор'), 'задача первого агента');
  assert.ok(text.includes('Договор аренды проверен'), 'вывод первого агента');
  assert.ok(text.includes('Оборудование: три варианта'), 'второй агент');
  assert.ok(text.includes('Критика ответа'), 'критик тоже в списке');
  // что осталось — отдельной строкой
  assert.ok(text.includes('Осталось:'), text);
  assert.ok(text.includes('Получить подписанный экземпляр'), 'текст «что осталось»');
  // файл и путь
  assert.ok(text.includes('01-legal.md'), 'имя файла');
  assert.ok(text.includes('C:\\Проекты\\Химчистка\\01-legal.md'), 'полный путь');
  // вердикт критика по-русски
  assert.ok(text.includes('на доработку'), 'машинный вердикт переведён');
  // счётчики в заголовке
  assert.ok(text.includes('3 агента'), text.slice(0, 400));
});

test('работа в процессе: подпись вместо пустого вывода', async () => {
  const { renderAgentResults } = await mount();
  const el = expandSections(renderAgentResults(detail(), makeCtx(), { roomId: 'clean' }));
  assert.ok(textOf(el).includes('Вывод ещё не готов — работа идёт.'), textOf(el));
});

test('кнопки файла ведут на существующие ручки, у пропавшего файла — выключены', async () => {
  const { renderAgentResults } = await mount();
  const calls = [];
  const el = expandSections(renderAgentResults(detail(), makeCtx(calls), { roomId: 'clean' }));
  const rows = walk(el, (n) => String(n.getAttribute('class') || '').split(/\s+/).includes('agr__file'));
  const first = rows.find((n) => textOf(n).includes('01-legal.md'));
  const labels = buttonsIn(first).map(textOf);
  assert.deepEqual(labels, ['Просмотр', 'Открыть', 'Открыть в папке'], labels.join('|'));
  // «Открыть» — POST /api/files/launch с путём и комнатой
  buttonsIn(first)[1].dispatch('click');
  await new Promise((r) => setTimeout(r, 0));
  const open = calls.find((c) => c.url === '/api/files/launch');
  assert.ok(open, 'вызов ручки открытия');
  assert.equal(open.body.mode, 'open');
  assert.equal(open.body.room, 'clean');
  assert.equal(open.body.path, 'C:\\Проекты\\Химчистка\\01-legal.md');

  const missingRow = rows.find((n) => textOf(n).includes('review.md'));
  assert.ok(textOf(missingRow).includes('файла нет на месте'), textOf(missingRow));
  const missingButtons = buttonsIn(missingRow);
  assert.ok(missingButtons.length === 3, 'кнопки есть, но выключены');
  assert.ok(missingButtons.every((b) => b.getAttribute('disabled') === 'disabled'),
    'у пропавшего файла кнопки выключены');
});

test('фильтр «Что осталось» оставляет только строки с остатками', async () => {
  const { renderAgentResults } = await mount();
  const el = expandSections(renderAgentResults(detail(), makeCtx(), { roomId: 'clean' }));
  const filter = buttonsIn(el).find((b) => textOf(b) === 'Что осталось');
  assert.ok(filter, 'кнопка фильтра есть');
  filter.dispatch('click');
  const text = textOf(el);
  assert.ok(text.includes('Юр-блок: аренда и договор'), 'строка с остатками осталась');
  assert.ok(!text.includes('Оборудование: три варианта'), 'строка без остатков скрыта');
});

test('пустое состояние: зачем блок и что будет дальше', async () => {
  const { renderAgentResults } = await mount();
  const el = expandSections(renderAgentResults(
    { id: 't_aa000001', room: 'clean', status: 'working', agent_results: { agents: [] } },
    makeCtx(), { roomId: 'clean' },
  ));
  const text = textOf(el);
  assert.ok(text.includes('Работа ещё не сдана'), text);
  assert.ok(text.includes('что он изучил или сделал'), 'объяснение, что появится');
  assert.ok(text.includes('Задача в работе'), 'что сделать сейчас — подождать');
});

test('у родителя проекта — свёртка «по подзадачам» со ссылками и ленивой загрузкой', async () => {
  const { renderAgentResults } = await mount();
  const calls = [];
  const d = detail({
    project: {
      parent_id: null,
      children: [
        { id: 't_bb000001', title: 'Ветка 1: аренда', status: 'done', stage: 'done_system' },
        { id: 't_bb000002', title: 'Ветка 2: оборудование', status: 'working', stage: 'working' },
      ],
    },
  });
  const el = expandSections(renderAgentResults(d, makeCtx(calls), { roomId: 'clean' }));
  const text = textOf(el);
  assert.ok(text.includes('По подзадачам'), text.slice(0, 300));
  assert.ok(text.includes('Ветка 1: аренда'));
  assert.ok(text.includes('Готово'), 'стадия подзадачи по-русски');
  const links = walk(el, (n) => String(n.tagName).toLowerCase() === 'a');
  const href = links.map((a) => a.getAttribute('href')).find((h) => h && h.includes('t_bb000001'));
  assert.ok(href && href.includes('#/room/clean/request/t_bb000001'), href);
  // «Результаты» подзадачи грузятся только по нажатию
  const toggle = buttonsIn(el).find((b) => textOf(b) === 'Результаты');
  assert.ok(toggle, 'кнопка загрузки результатов подзадачи');
  assert.equal(calls.filter((c) => c.url).length, 0, 'до нажатия запросов нет');
  toggle.dispatch('click');
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(calls.some((c) => String(c.url).includes('/api/requests/t_bb000001/agent-results')),
    JSON.stringify(calls));
});

test('нет данных — блока нет (страница живёт без него)', async () => {
  const { renderAgentResults } = await mount();
  assert.equal(renderAgentResults(null, makeCtx()), null);
  const el = renderAgentResults({ id: 't_aa000002' }, makeCtx());
  assert.ok(el && textOf(expandSections(el)).includes('Работа ещё не сдана'),
    'без ключа agent_results — честное пустое состояние');
});

test('в блоке нет innerHTML: разметка строится только h()', async () => {
  const { renderAgentResults } = await mount();
  const el = expandSections(renderAgentResults(detail(), makeCtx(), { roomId: 'clean' }));
  const all = [el, ...walk(el, () => true)];
  for (const node of all) {
    // fake-dom падает на чтении innerHTML — значит, ни один узел его не выставляет.
    assert.equal(Object.getOwnPropertyDescriptor(node, 'innerHTML'), undefined,
      'innerHTML не используется');
  }
});
