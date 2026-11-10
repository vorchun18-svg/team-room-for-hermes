/**
 * agent-model-select.test.mjs — выбор модели: что именно уходит в комнату.
 *
 * Регрессия на три жалобы Михаила (04.11.2026):
 *  1. список моделей не раскрывался, пока не сменишь провайдера туда-обратно;
 *  2. в поле выбора модели был микрофон — его там быть не должно;
 *  3. после «Применить» сохранялась НЕ та модель, что выбрана.
 *
 * Первопричина (3): обработчик клика замыкался на общий счётчик цикла, поэтому
 * ВСЕ строки списка выбирали ПОСЛЕДНЮЮ модель. Отсюда и «подмена»: у ollama-cloud
 * последняя в каталоге — deepseek-v4-pro (вместо выбранной deepseek-v4.1-flash:1017),
 * у anthropic — claude-fable-5-1 (вместо claude-sonnet-5-5).
 *
 * Проверяем поведение, а не реализацию: клик по строке и клавиатура ↑ ↓ Enter,
 * затем читаем тело PUT /api/rooms/{id}/agents/{profile}/model.
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';
import { mountAgentModel } from '../web/js/agent-model-ui.js';
import { enhanceAll, eligible, isCombobox } from '../web/js/dictation-auto.js';

/* ────────────────────────────────────────────── доработка fake-dom */

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
FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
  if (node.parentNode) node.parentNode.remove();
  const idx = ref ? this.childNodes.indexOf(ref) : -1;
  if (idx === -1) this.childNodes.push(node);
  else this.childNodes.splice(idx, 0, node);
  node.parentNode = this;
  return node;
};
FakeElement.prototype.removeChild = function removeChild(node) {
  const idx = this.childNodes.indexOf(node);
  if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
  return node;
};
FakeElement.prototype.focus = function focus() {};
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};
FakeElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
  return { left: 100, top: 200, right: 220, bottom: 226, width: 120, height: 26 };
};
FakeElement.prototype.closest = function closest() { return null; };

function matches(el, selector) {
  const s = String(selector).trim();
  if (!s) return false;
  const tag = String(el.tagName || '').toLowerCase();
  if (s.startsWith('.')) return String(el.className || '').split(/\s+/).includes(s.slice(1));
  if (s.startsWith('[')) {
    const m = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(s);
    if (!m) return false;
    return m[2] === undefined ? el.hasAttribute(m[1]) : el.getAttribute(m[1]) === m[2];
  }
  return tag === s.toLowerCase();
}
FakeElement.prototype.querySelectorAll = function querySelectorAll(selector) {
  const parts = String(selector).split(',').map((x) => x.trim()).filter(Boolean);
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (parts.some((p) => matches(child, p))) out.push(child);
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

function byClass(root, name) {
  return walk(root, (n) => String(n.className || '').split(/\s+/).includes(name));
}

/** Строки моделей в том порядке, в каком их видит человек. */
const optionsOf = (row) => byClass(row.element, 'agent-model__option');
const listOf = (row) => byClass(row.element, 'agent-model__models')[0];

/* ─────────────────────────────────────────────────────────── каталоги */

/** Модели-близнецы: отличаются только хвостом (`:1017`, `-5`, `-1`). */
const TWINS = {
  providers: [
    {
      id: 'ollama-cloud', label: 'Ollama Cloud', configured: true,
      models: [
        { id: 'deepseek-v4.1-flash', label: 'DeepSeek V4.1 Flash' },
        { id: 'deepseek-v4.1-flash:1017', label: 'DeepSeek V4.1 Flash · 1017' },
        { id: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro' },
        { id: 'deepseek-v4-pro:0813', label: 'DeepSeek V4 Pro · 0813' },
        { id: 'kimi-k2.7-code', label: 'Kimi K2.7 Code' },
      ],
    },
    {
      id: 'anthropic', label: 'Anthropic', configured: true,
      models: [
        { id: 'claude-sonnet-5', label: 'Claude Sonnet 5' },
        { id: 'claude-sonnet-5-5', label: 'Claude Sonnet 5.5' },
        { id: 'claude-fable-5', label: 'Claude Fable 5' },
        { id: 'claude-fable-5-1', label: 'Claude Fable 5.1' },
      ],
    },
    {
      id: 'closed', label: 'Закрытый контур', configured: false,
      models: [{ id: 'closed-1', label: 'Закрытая 1' }],
    },
  ],
  efforts: [
    { id: 'minimal', label: 'Минимальные' },
    { id: 'low', label: 'Низкие' },
    { id: 'medium', label: 'Средние' },
    { id: 'high', label: 'Высокие' },
    { id: 'ultra', label: 'Предельные' },
  ],
  source: 'hermes:inventory',
};

/** Участник по умолчанию идёт на модели, которой уже нет в каталоге. */
const ROOM = {
  id: 'cafe',
  title: 'Лето',
  orchestrator: 'biz-director',
  members: [{
    profile: 'biz-director', role: 'orchestrator',
    model_settings: { provider: 'ollama-cloud', model: 'kimi-k2.6', reasoning: 'high', source: 'agent' },
    agent_default: { provider: 'ollama-cloud', model: 'kimi-k2.6', reasoning: 'high' },
  }],
};

const FIXTURE_CATALOG = JSON.parse(
  readFileSync(new URL('../fixtures/models.json', import.meta.url), 'utf8'),
);

function fakeApi() {
  const calls = [];
  return {
    calls,
    get: async (path) => { calls.push(['GET', path, null]); return FIXTURE_CATALOG; },
    put: async (path, body) => {
      calls.push(['PUT', path, body]);
      return { member: {}, applied: { cards_updated: 0, pending_running: 0, cards_failed: [] }, warnings: [] };
    },
    post: async (path, body) => { calls.push(['POST', path, body]); return {}; },
  };
}

const lastPut = (api) => [...api.calls].reverse().find((c) => c[0] === 'PUT');

async function mountLive(catalog, room) {
  installDom();
  const api = fakeApi();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api }, {
    room: room || ROOM, catalog, mode: 'live', profile: 'biz-director',
  });
  return { api, ui, row: ui.rowFor('biz-director') };
}

/* ───────────────────────────── 3. клик выбирает ровно ту модель ───────── */

test('клик по каждой модели каждого провайдера: в PUT уходит ровно она', async () => {
  const { api, row } = await mountLive(TWINS);
  for (const provider of TWINS.providers.filter((p) => p.configured)) {
    row.setProvider(provider.id);
    assert.deepEqual(row.models().map((m) => m.id), provider.models.map((m) => m.id),
      'список показывает модели выбранного провайдера: ' + provider.id);
    for (const model of provider.models) {
      const node = optionsOf(row).find((n) => n.dataset.model === model.id);
      assert.ok(node, 'в списке есть строка ' + model.id);
      node.dispatch('click', { type: 'click', target: node });
      assert.equal(row.provider(), provider.id);
      assert.equal(row.model(), model.id, 'клик выбрал ровно ' + model.id);
      await row.save();
      assert.deepEqual(lastPut(api)[2],
        { provider: provider.id, model: model.id, reasoning: 'high' },
        'в комнату уходит ровно выбранная модель, а не последняя в списке');
    }
  }
});

test('клавиатура ↑ ↓ Enter: в PUT уходит ровно подсвеченная модель', async () => {
  const { api, row } = await mountLive(TWINS);
  for (const provider of TWINS.providers.filter((p) => p.configured)) {
    row.setProvider(provider.id);
    for (let i = 0; i < provider.models.length; i += 1) {
      row.key('Escape'); // список заведомо открыт; Esc его только сворачивает
      row.searchInput().dispatch('focus', { type: 'focus' });
      for (let step = 0; step <= i; step += 1) row.key('ArrowDown');
      row.key('Enter');
      assert.equal(row.model(), provider.models[i].id,
        'Enter берёт подсвеченную строку ' + i + ' у ' + provider.id);
      await row.save();
      assert.deepEqual(lastPut(api)[2],
        { provider: provider.id, model: provider.models[i].id, reasoning: 'high' });
    }
  }
});

test('поиск: клик по любой из похожих строк даёт именно её', async () => {
  const { api, row } = await mountLive(TWINS);
  const click = (id) => {
    const node = optionsOf(row).find((n) => n.dataset.model === id);
    assert.ok(node, 'строка на месте: ' + id);
    node.dispatch('click', { type: 'click', target: node });
  };

  row.setProvider('anthropic');
  row.setQuery('sonnet-5');
  assert.deepEqual(row.models().map((m) => m.id), ['claude-sonnet-5', 'claude-sonnet-5-5']);
  click('claude-sonnet-5');           // первая из пары
  assert.equal(row.model(), 'claude-sonnet-5');
  await row.save();
  assert.deepEqual(lastPut(api)[2],
    { provider: 'anthropic', model: 'claude-sonnet-5', reasoning: 'high' });
  row.setQuery('sonnet-5');
  click('claude-sonnet-5-5');         // и вторая
  assert.equal(row.model(), 'claude-sonnet-5-5');
  await row.save();
  assert.deepEqual(lastPut(api)[2],
    { provider: 'anthropic', model: 'claude-sonnet-5-5', reasoning: 'high' });

  // то же для пары «просто модель / модель с :тегом»
  row.setQuery('deepseek-v4.1-flash');
  assert.deepEqual(row.models().map((m) => m.id),
    ['deepseek-v4.1-flash', 'deepseek-v4.1-flash:1017']);
  click('deepseek-v4.1-flash');
  assert.equal(row.model(), 'deepseek-v4.1-flash');
  await row.save();
  assert.deepEqual(lastPut(api)[2],
    { provider: 'ollama-cloud', model: 'deepseek-v4.1-flash', reasoning: 'high' });
  click('deepseek-v4.1-flash:1017');
  assert.equal(row.model(), 'deepseek-v4.1-flash:1017');
  await row.save();
  assert.deepEqual(lastPut(api)[2],
    { provider: 'ollama-cloud', model: 'deepseek-v4.1-flash:1017', reasoning: 'high' });
});

test('фикстура fixtures/models.json: каждая модель каждого провайдера — по клику и по клавиатуре', async () => {
  const providers = FIXTURE_CATALOG.providers.filter((p) => p.configured);
  const defaults = FIXTURE_CATALOG.agent_defaults['biz-director'];
  const room = {
    id: 'cafe', title: 'Лето', orchestrator: 'biz-director',
    members: [{
      profile: 'biz-director', role: 'orchestrator',
      model_settings: { ...defaults, source: 'agent' },
      agent_default: { ...defaults },
    }],
  };
  const { api, row } = await mountLive(FIXTURE_CATALOG, room);
  // Усилия меняем заранее: иначе модель, совпавшая с настройкой агента,
  // по правилам уходит как «вернуть по умолчанию» (пустой объект).
  row.setEffort('ultra');
  const effort = 'ultra';

  for (const provider of providers) {
    row.setProvider(provider.id);
    for (const model of provider.models) {
      const byClick = optionsOf(row).find((n) => n.dataset.model === model.id);
      assert.ok(byClick, 'строка есть в списке: ' + model.id);
      byClick.dispatch('click', { type: 'click', target: byClick });
      assert.equal(row.model(), model.id);
      await row.save();
      assert.deepEqual(lastPut(api)[2], { provider: provider.id, model: model.id, reasoning: effort },
        'клик: уходит ' + model.id);
    }
  }
  for (const provider of providers) {
    row.setProvider(provider.id);
    for (let i = 0; i < provider.models.length; i += 1) {
      for (let step = 0; step <= i; step += 1) row.key('ArrowDown');
      row.key('Enter');
      assert.equal(row.model(), provider.models[i].id);
      await row.save();
      assert.deepEqual(lastPut(api)[2],
        { provider: provider.id, model: provider.models[i].id, reasoning: effort },
        'клавиатура: уходит ' + provider.models[i].id);
    }
  }
});

/* ─────────────────────────── 1. список раскрыт сразу и всегда ──────────── */

test('список моделей открыт сразу при уже выбранном провайдере', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api: fakeApi() }, { room: ROOM, catalog: TWINS, mode: 'live', profile: 'biz-director' });
  const row = ui.rowFor('biz-director');

  assert.equal(row.provider(), 'ollama-cloud', 'провайдер уже выбран до открытия');
  assert.equal(row.listOpen(), true, 'список раскрыт без смены провайдера');
  assert.equal(String(listOf(row).className).includes('hidden'), false, 'блок списка виден');
  assert.deepEqual(optionsOf(row).map((n) => n.dataset.model),
    TWINS.providers[0].models.map((m) => m.id), 'видны все модели выбранного провайдера');
  assert.equal(row.searchInput().getAttribute('aria-expanded'), 'true');
});

test('список не схлопывается: смена провайдера туда-обратно и нажатие в поле', async () => {
  const { row } = await mountLive(TWINS);
  row.setProvider('anthropic');
  assert.equal(row.listOpen(), true);
  assert.deepEqual(optionsOf(row).map((n) => n.dataset.model),
    TWINS.providers[1].models.map((m) => m.id));

  row.setProvider('ollama-cloud');
  assert.equal(row.listOpen(), true, 'вернулись к прежнему провайдеру — список по-прежнему открыт');
  assert.equal(optionsOf(row).length, TWINS.providers[0].models.length);

  // свернули Esc — нажатие в поле раскрывает снова
  row.searchInput().dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
  assert.equal(row.listOpen(), false);
  row.searchInput().dispatch('click', { type: 'click' });
  assert.equal(row.listOpen(), true, 'клик в поле раскрывает список');

  // и стрелка-раскрывашка работает в обе стороны
  const toggle = byClass(row.element, 'agent-model__toggle')[0];
  assert.ok(toggle, 'у поля есть стрелка списка');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  toggle.dispatch('click', { type: 'click', target: toggle, preventDefault() {} });
  assert.equal(row.listOpen(), false);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  toggle.dispatch('click', { type: 'click', target: toggle, preventDefault() {} });
  assert.equal(row.listOpen(), true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
});

test('в поле видно текущую модель; выбор обновляет подпись', async () => {
  const { row } = await mountLive(TWINS);
  // Действующая модель участника — kimi-k2.6, её уже нет в каталоге: показываем id.
  assert.equal(row.searchInput().getAttribute('placeholder'), 'kimi-k2.6',
    'в пустом поле видно, какая модель действует сейчас');
  // Выбираем конкретную модель — подсказка и строка «Выбрано» догоняют.
  const node = optionsOf(row).find((n) => n.dataset.model === 'deepseek-v4.1-flash:1017');
  node.dispatch('click', { type: 'click', target: node });
  assert.equal(row.searchInput().getAttribute('placeholder'), 'DeepSeek V4.1 Flash · 1017');
  assert.match(row.element.textContent, /Выбрано: DeepSeek V4\.1 Flash · 1017/);
  assert.equal(String(listOf(row).className).includes('hidden'), false,
    'после выбора список остаётся открытым — видно галочку');
  const chosen = optionsOf(row).find((n) => n.getAttribute('aria-selected') === 'true');
  assert.equal(chosen.dataset.model, 'deepseek-v4.1-flash:1017');
});

/* ────────────────────────── 2. микрофона в поле модели нет ─────────────── */

test('в поле модели нет микрофона: ни авто-кнопки, ни обёртки', async () => {
  const { row } = await mountLive(TWINS);
  const search = row.searchInput();
  assert.equal(search.getAttribute('data-mic'), 'off', 'поле помечено «без диктовки»');
  assert.equal(isCombobox(search), true, 'поле — combobox, диктовка к нему не относится');
  assert.equal(eligible(search), false);
  assert.equal(String(search.parentNode.className).includes('mic-field'), false,
    'поле не обёрнуто в .mic-field');

  const added = enhanceAll(row.element);
  assert.equal(added, 0, 'авто-вставка микрофона ничего сюда не добавила');
  assert.equal(byClass(row.element, 'mic-btn').length, 0, 'кнопок микрофона в редакторе нет');
  assert.equal(String(search.parentNode.className).includes('agent-model__search-wrap'), true);
});

test('обычное текстовое поле микрофон по-прежнему получает', () => {
  installDom();
  const host = new FakeElement('div');
  const plain = new FakeElement('input');
  plain.setAttribute('type', 'text');
  plain.setAttribute('class', 'input');
  plain.setAttribute('aria-label', 'Особое указание участнику');
  host.appendChild(plain);
  assert.equal(eligible(plain), true, 'правило не стало запретом на диктовку вообще');
  enhanceAll(host);
  assert.equal(byClass(host, 'mic-btn').length, 1);
});

/* ─────────────────────────── CSS: место только под стрелку ────────────── */

test('CSS: справа в поле — стрелка списка, а не микрофон', () => {
  const css = readFileSync(new URL('../web/css/agent-model.css', import.meta.url), 'utf8');
  assert.equal(/\.agent-model__search\s*{[^}]*padding-right:\s*40px/s.test(css), false,
    'место под микрофон в поле модели больше не резервируется');
  assert.match(css, /\.agent-model__search\s*{[^}]*padding-right:\s*34px/s);
  assert.match(css, /\.agent-model__toggle\s*{[^}]*position:\s*absolute/s);
  assert.match(css, /\.agent-model__toggle\[aria-expanded="true"\]\s+\.codicon\s*{[^}]*transform:\s*rotate\(180deg\)/s);
  assert.match(css, /\.agent-model__models\s*{[^}]*max-height:[^}]*overflow-y:\s*auto/s,
    'список по-прежнему прокручивается сам');
});
