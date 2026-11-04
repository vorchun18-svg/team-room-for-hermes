/**
 * agent-model.test.mjs — «Модель, провайдер и мышление» участника комнаты (волна 10).
 *
 * Проверяем интерфейс, а не сервер:
 *  • у каждого участника строка «модель: …» и редактор с провайдером и поиском моделей;
 *  • только настроенные провайдеры; ставший недоступным — предупреждение, не выбор;
 *  • фильтр моделей по вводу, группы по провайдерам, клавиатура ↑ ↓ Enter Esc;
 *  • «Размышление» выкл = 'none', «Усилия» по-русски из каталога (Минимальное…Максимум);
 *  • «по умолчанию: …», «Вернуть по умолчанию», метка «изменено для этой комнаты»;
 *  • вкладка «Команда»: «Применить» → PUT и понятный ответ + «Перезапустить» при pending;
 *  • нет технических слов в видимом тексте, нет предупреждений о стоимости;
 *  • ошибка сети показывается по-русски, значение при этом не теряется.
 *
 * DOM — самодельный мини-DOM (tests/fake-dom.mjs), браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, FakeText, createFakeDocument } from './fake-dom.mjs';
import {
  mountAgentModel, openAgentModelEditor, agentModelLine, filterModels, effortOptions,
  modelSettingsOf, agentDefaultOf, reasoningLabel, normalizeReasoning, providersOf,
  defaultsText, modelLabelOf, EFFORT_RU, THINKING_OFF,
} from '../web/js/agent-model-ui.js';

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
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  // head нет: ensureCss() считает стили уже подключёнными и не ждёт события load
  doc.addEventListener = () => {};
  doc.removeEventListener = () => {};
  // без head+addEventListener ensureCss() считает стили подключёнными и не ждёт load
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

/* ─────────────────────────────────────────────────────────── каталог */

const CATALOG = {
  providers: [
    {
      id: 'ollama-cloud', label: 'Ollama (облако)', configured: true,
      models: [
        { id: 'qwen3.6:35b', label: 'Qwen 3.6' },
        { id: 'deepseek-v4', label: 'DeepSeek 4' },
      ],
    },
    {
      id: 'anthropic', label: 'Anthropic', configured: true,
      models: [{ id: 'claude-sonnet-4.5', label: 'Claude Sonnet 4.5' }],
    },
    {
      id: 'openai', label: 'OpenAI', configured: false,
      models: [{ id: 'gpt-5', label: 'GPT-5' }],
    },
  ],
  efforts: ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  source: 'hermes-config',
};

const ROOM = {
  id: 'cafe',
  title: 'Лето',
  orchestrator: 'biz-director',
  critic: 'biz-critic',
  members: [
    {
      profile: 'biz-director', role: 'orchestrator',
      model_settings: { provider: 'ollama-cloud', model: 'qwen3.6:35b', reasoning: 'medium', source: 'agent' },
      agent_default: { provider: 'ollama-cloud', model: 'qwen3.6:35b', reasoning: 'medium' },
    },
    {
      profile: 'biz-cfo', role: 'worker',
      // В комнате уже поменяли: провайдер перестал быть настроенным.
      model_settings: { provider: 'openai', model: 'gpt-5', reasoning: 'none', source: 'room' },
      agent_default: { provider: 'anthropic', model: 'claude-sonnet-4.5', reasoning: 'high' },
    },
  ],
};

const ctx = { api: {}, toast: () => {} };

function fakeApi(overrides = {}) {
  const calls = [];
  const api = {
    calls,
    get: async (path) => {
      calls.push(['GET', path, null]);
      if (overrides.get) return overrides.get(path);
      return CATALOG;
    },
    put: async (path, body) => {
      calls.push(['PUT', path, body]);
      if (overrides.put) return overrides.put(path, body);
      return { member: {}, applied: { cards_updated: 0, pending_running: 0 }, warnings: [] };
    },
    post: async (path, body) => {
      calls.push(['POST', path, body]);
      if (overrides.post) return overrides.post(path, body);
      return { restarted: 0 };
    },
  };
  return api;
}

/* ─────────────────────────────────────────────────────── данные/подписи */

test('усилия: русские подписи из каталога, порядок Minimal…Ultra', () => {
  const list = effortOptions(CATALOG);
  assert.deepEqual(list.map((e) => e.value), ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
  assert.deepEqual(list.map((e) => e.label), ['Минимальное', 'Низкое', 'Среднее', 'Высокое',
    'Очень высокое', 'Максимум', 'Ультра']);
  // каталог пуст — берём общий список Hermes
  assert.equal(effortOptions(null).length, 7);
  assert.equal(EFFORT_RU.high, 'Высокое');
});

test('мышление: «выкл» — это none, подписи по-русски', () => {
  assert.equal(THINKING_OFF, 'none');
  assert.equal(normalizeReasoning('OFF'), 'none');
  assert.equal(normalizeReasoning(''), '');
  assert.equal(reasoningLabel('none'), 'без размышления');
  assert.equal(reasoningLabel('xhigh'), 'Очень высокое');
  assert.equal(reasoningLabel(''), 'как у агента');
});

test('только настроенные провайдеры; подписи моделей и «по умолчанию»', () => {
  const providers = providersOf(CATALOG);
  assert.deepEqual(providers.map((p) => p.id), ['ollama-cloud', 'anthropic']);
  assert.equal(providers.some((p) => p.id === 'openai'), false, 'без ключа провайдера не показываем');
  assert.equal(modelLabelOf(CATALOG, 'ollama-cloud', 'qwen3.6:35b'), 'Qwen 3.6');
  assert.equal(defaultsText(CATALOG, { provider: 'anthropic', model: 'claude-sonnet-4.5', reasoning: 'high' }),
    'по умолчанию: Anthropic · Claude Sonnet 4.5 · высокое');
});

test('фильтр моделей: без ввода — выбранный провайдер, с вводом — группы по провайдерам', () => {
  const own = filterModels(CATALOG, { provider: 'ollama-cloud' });
  assert.equal(own.length, 1);
  assert.deepEqual(own[0].models.map((m) => m.id), ['qwen3.6:35b', 'deepseek-v4']);

  const search = filterModels(CATALOG, { provider: 'ollama-cloud', query: 'claude' });
  assert.equal(search.length, 1);
  assert.equal(search[0].id, 'anthropic');
  assert.deepEqual(search[0].models.map((m) => m.id), ['claude-sonnet-4.5']);

  const both = filterModels(CATALOG, { query: 'e' });
  assert.equal(both.length, 2, 'ищем по всем настроенным провайдерам');
  assert.deepEqual(filterModels(CATALOG, { query: 'нет-такой' }), []);
});

test('настройки участника и умолчания агента', () => {
  const settings = modelSettingsOf(ROOM, 'biz-cfo');
  assert.deepEqual(settings, { provider: 'openai', model: 'gpt-5', reasoning: 'none', source: 'room' });
  assert.deepEqual(agentDefaultOf(ROOM, 'biz-cfo'), {
    provider: 'anthropic', model: 'claude-sonnet-4.5', reasoning: 'high',
  });
  // нет записи в комнате — действует настройка агента
  const plain = modelSettingsOf({ members: [{ profile: 'x', agent_default: { provider: 'a', model: 'm', reasoning: 'low' } }] }, 'x');
  assert.equal(plain.source, 'agent');
  assert.equal(modelSettingsOf(ROOM, 'нет-такого').model, '');
});

/* ─────────────────────────────────────────────────────── рендер */

test('рендер: строка на каждого участника, без технических слов и цен', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, ctx, { room: ROOM, catalog: CATALOG, mode: 'draft' });

  const text = host.textContent;
  for (const who of ['biz-director', 'biz-cfo']) assert.ok(text.includes(who), 'нет участника ' + who);
  assert.match(text, /Модель/);
  assert.match(text, /Размышление/);
  assert.match(text, /Усилия/);
  assert.match(text, /по умолчанию: Ollama \(облако\) · Qwen 3\.6 · среднее/);
  assert.match(text, /изменено для этой комнаты/, 'метка у участника с настройкой комнаты');

  // технические id провайдеров и профилей — только в подсказках
  const visible = walk(host, (n) => n.nodeType === 1).map((n) => (n.childNodes || [])
    .filter((c) => c.nodeType === 3).map((c) => c.data).join('')).join(' ');
  assert.equal(/ollama-cloud|anthropic|openai/.test(visible), false,
    'id провайдеров не должны попадать в видимый текст');
  assert.equal(/biz-director|biz-cfo/.test(visible.replace(/biz-(director|cfo)/g, '')), false);

  // про стоимость не предупреждаем — этого не просили
  assert.equal(/дорог|стоимост|расход|лимит|цене|дешев/i.test(text), false);

  // в комнате три человека: руководитель, критик и исполнитель из состава
  const rows = byClass(host, 'agent-model__row');
  assert.equal(rows.length, 3);
  assert.ok(text.includes('biz-critic'), 'критик тоже участник');
  assert.equal(ui.rowFor('biz-director').chipText().startsWith('модель: Qwen 3.6'), true);
});

test('редактор участника: провайдер, список моделей, выбор', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, ctx, { room: ROOM, catalog: CATALOG, mode: 'draft' });
  const row = ui.rowFor('biz-director');

  row.open();
  const selects = walk(row.element, (n) => n.tagName === 'select');
  assert.equal(selects.length, 1);
  const values = (selects[0].childNodes || []).filter((n) => n.nodeType === 1).map((o) => o.getAttribute('value'));
  assert.deepEqual(values, ['ollama-cloud', 'anthropic'], 'OpenAI без ключа не предлагаем');
  assert.equal(selects[0].value, 'ollama-cloud');

  // список моделей выбранного провайдера
  assert.deepEqual(row.models().map((m) => m.id), ['qwen3.6:35b', 'deepseek-v4']);
  assert.deepEqual(row.visibleGroupLabels(), ['Ollama (облако)']);

  // смена провайдера — список переехал, модель старого провайдера не остаётся выбранной
  row.setProvider('anthropic');
  assert.deepEqual(row.models().map((m) => m.id), ['claude-sonnet-4.5']);
  assert.equal(row.provider(), 'anthropic');

  // поиск по вводу: ищем по всем настроенным, группы видны
  row.setQuery('qwen');
  assert.deepEqual(row.models().map((m) => m.id), ['qwen3.6:35b']);
  row.setQuery('нет-такой');
  assert.deepEqual(row.models(), []);
  assert.match(row.element.textContent, /Ничего не нашлось/);

  // выбор мышью
  row.setQuery('claude');
  assert.equal(row.choose(0), true);
  assert.equal(row.model(), 'claude-sonnet-4.5');
  assert.match(row.chipText(), /Claude Sonnet 4\.5/);
  assert.equal(row.state().changed, true);
  assert.match(row.element.textContent, /изменено для этой комнаты/);
});

test('клавиатура: ↑ ↓ Enter Esc', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, ctx, { room: ROOM, catalog: CATALOG, mode: 'draft' });
  const row = ui.rowFor('biz-director');
  row.open();
  row.setQuery('');

  assert.equal(row.key('ArrowDown'), 0, 'первое нажатие встаёт на первую модель');
  assert.equal(row.key('ArrowDown'), 1);
  assert.equal(row.key('ArrowDown'), 1, 'дальше списка не уходим');
  assert.equal(row.key('ArrowUp'), 0);
  row.key('Enter');
  assert.equal(row.model(), 'qwen3.6:35b');

  // Enter выбирает активную, Esc закрывает список, второй Esc — панель
  row.setQuery('deep');
  row.key('ArrowDown');
  row.key('Enter');
  assert.equal(row.model(), 'deepseek-v4');

  row.open();
  row.setQuery('qwen');
  row.key('Escape');
  assert.equal(row.state().changed, true, 'Esc не сбрасывает выбор');
  row.key('Escape');
  assert.equal(row.isOpen(), false, 'второй Esc сворачивает редактор');
});

test('размышление выключено = none, усилия возвращаются', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, ctx, { room: ROOM, catalog: CATALOG, mode: 'draft' });
  const row = ui.rowFor('biz-director');
  row.open();

  assert.equal(row.reasoning(), 'medium');
  assert.equal(row.setThinking(false), 'none');
  assert.match(row.chipText(), /без размышления/);
  assert.match(row.element.textContent, /Мышление выключено/);

  // кнопка «Размышление» в разметке действительно гасит усилия
  const input = walk(row.element, (n) => n.tagName === 'input' && n.getAttribute('type') === 'checkbox')[0];
  assert.ok(input);
  assert.equal(input.checked, false);
  input.checked = true;
  input.dispatch('change', { type: 'change', target: input });
  assert.equal(row.reasoning(), 'medium', 'включили обратно — прежний уровень');

  // сегменты «Усилия» — русские подписи, 7 штук
  const segments = byClass(row.element, 'agent-model__segments')[0];
  const labels = walk(segments, (n) => n.tagName === 'button').map((b) => b.textContent);
  assert.deepEqual(labels, ['Минимальное', 'Низкое', 'Среднее', 'Высокое', 'Очень высокое', 'Максимум', 'Ультра']);

  row.setEffort('xhigh');
  assert.equal(row.reasoning(), 'xhigh');
  assert.equal(row.state().changed, true);
});

test('«Вернуть по умолчанию» снимает изменение комнаты', async () => {
  installDom();
  const host = new FakeElement('div');
  const seen = [];
  const ui = await mountAgentModel(host, ctx, {
    room: ROOM, catalog: CATALOG, mode: 'draft',
    onChange: (profile, value) => seen.push([profile, value]),
  });
  const row = ui.rowFor('biz-cfo');
  row.open();
  assert.equal(row.state().changed, true);
  assert.equal(row.provider(), 'openai', 'показан провайдер, которого больше нет в каталоге');
  assert.match(row.element.textContent, /Провайдер больше не настроен/);
  assert.deepEqual(ui.value()['biz-cfo'], { provider: 'openai', model: 'gpt-5', reasoning: 'none' });

  row.reset();
  assert.equal(row.provider(), 'anthropic');
  assert.equal(row.model(), 'claude-sonnet-4.5');
  assert.equal(row.reasoning(), 'high');
  assert.equal(row.state().changed, false);
  assert.equal(ui.value()['biz-cfo'], undefined, 'по умолчанию в комнату не пишем');
  assert.deepEqual(seen.pop(), ['biz-cfo', null]);
});

test('значение уходит в комнату только для изменённых участников', async () => {
  installDom();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, ctx, { room: ROOM, catalog: CATALOG, mode: 'draft' });
  ui.rowFor('biz-director').open();
  ui.rowFor('biz-director').setProvider('anthropic');
  ui.rowFor('biz-director').setQuery('claude');
  ui.rowFor('biz-director').choose(0);
  ui.rowFor('biz-director').setThinking(false);
  assert.deepEqual(ui.value(), {
    'biz-director': { provider: 'anthropic', model: 'claude-sonnet-4.5', reasoning: 'none' },
    'biz-cfo': { provider: 'openai', model: 'gpt-5', reasoning: 'none' },
  });
  assert.equal(ui.value()['biz-critic'], undefined, 'нетронутый участник в комнату не уходит');
});

test('состав изменился — редактор следует (refresh)', async () => {
  installDom();
  const host = new FakeElement('div');
  let people = [{ profile: 'biz-director', role: 'orchestrator' }];
  const ui = await mountAgentModel(host, ctx, {
    room: ROOM, catalog: CATALOG, mode: 'draft', participants: () => people,
  });
  ui.rowFor('biz-director').setQuery('claude');
  ui.rowFor('biz-director').choose(0);

  people = [
    { profile: 'biz-director', role: 'orchestrator' },
    { profile: 'biz-cfo', role: 'worker' },
  ];
  ui.refresh();
  assert.equal(ui.rowFor('biz-cfo') !== null, true);
  assert.equal(ui.rowFor('biz-director').model(), 'claude-sonnet-4.5', 'выбор не потерялся');

  people = [{ profile: 'biz-cfo', role: 'worker' }];
  ui.refresh();
  assert.deepEqual(Object.keys(ui.value()), ['biz-cfo']);
});

/* ───────────────────────────────────────────── вкладка «Команда»: сохранение */

test('«Применить»: PUT и понятный ответ; «Перезапустить» при задачах в работе', async () => {
  installDom();
  const api = fakeApi({
    put: () => ({ member: {}, applied: { cards_updated: 2, pending_running: 3 }, warnings: [] }),
    post: () => ({ restarted: 2 }),
  });
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api }, {
    room: ROOM, catalog: CATALOG, mode: 'live', profile: 'biz-director',
    member: ROOM.members[0],
  });
  const row = ui.rowFor('biz-director');
  row.setProvider('anthropic');
  row.setQuery('claude');
  row.choose(0);

  await row.save();
  const put = api.calls.find((c) => c[0] === 'PUT');
  assert.equal(put[1], '/api/rooms/cafe/agents/biz-director/model');
  assert.deepEqual(put[2], { provider: 'anthropic', model: 'claude-sonnet-4.5', reasoning: 'medium' });
  assert.match(row.statusText(), /^Сохранено\. Следующая задача уже пойдёт на новой модели/);
  assert.match(row.statusText(), /3 задачи в работе доделают на старой/);
  assert.equal(row.state().pending, 3);

  const restart = byClass(row.element, 'agent-model__restart')[0];
  assert.ok(restart, 'кнопка перезапуска есть');
  assert.equal(String(restart.className).includes('hidden'), false);

  await row.restart();
  const post = api.calls.find((c) => c[0] === 'POST');
  assert.equal(post[1], '/api/rooms/cafe/agents/biz-director/restart');
  assert.match(row.statusText(), /Перезапущено: 2 задачи/);
  assert.equal(row.state().pending, 0);
});

test('нет задач в работе — одна фраза, без кнопки перезапуска', async () => {
  installDom();
  const api = fakeApi();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api }, {
    room: ROOM, catalog: CATALOG, mode: 'live', profile: 'biz-director',
  });
  const row = ui.rowFor('biz-director');
  row.setProvider('anthropic');
  row.setQuery('claude');
  row.choose(0);
  await row.save();
  assert.equal(row.statusText(), 'Сохранено. Следующая задача уже пойдёт на новой модели.');
  assert.equal(String(byClass(row.element, 'agent-model__restart')[0].className).includes('hidden'), true);
});

test('«Вернуть по умолчанию» на вкладке «Команда» отправляет пустой объект', async () => {
  installDom();
  const api = fakeApi();
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api }, {
    room: ROOM, catalog: CATALOG, mode: 'live', profile: 'biz-cfo',
    member: ROOM.members[1],
  });
  const row = ui.rowFor('biz-cfo');
  row.reset();
  await Promise.resolve();
  const put = api.calls.find((c) => c[0] === 'PUT');
  assert.deepEqual(put[2], {}, 'пустой объект = вернуть настройку агента');
  assert.match(row.statusText(), /Сохранено\. Следующая задача пойдёт на модели по умолчанию/);
});

/* ─────────────────────────────────────────────────────────── ошибки */

test('ошибка сети: по-русски, значение не теряется', async () => {
  installDom();
  const api = fakeApi({
    put: () => { const err = new Error('Нет связи с приложением'); err.code = 'network'; throw err; },
  });
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api }, {
    room: ROOM, catalog: CATALOG, mode: 'live', profile: 'biz-director',
  });
  const row = ui.rowFor('biz-director');
  row.setProvider('anthropic');
  row.setQuery('claude');
  row.choose(0);
  assert.equal(await row.save(), null);
  assert.match(row.errorText(), /Нет связи с приложением/);
  assert.equal(row.model(), 'claude-sonnet-4.5', 'выбор остался в поле');
  assert.equal(String(byClass(row.element, 'agent-model__error')[0].className).includes('hidden'), false);
});

test('каталог не пришёл: подсказка вместо пустого списка', async () => {
  installDom();
  const api = fakeApi({ get: () => { const e = new Error('Нет связи с приложением'); e.code = 'network'; throw e; } });
  const host = new FakeElement('div');
  const ui = await mountAgentModel(host, { api }, { room: ROOM, mode: 'draft' });
  assert.match(host.textContent, /Список моделей недоступен/);
  // по умолчанию агента всё равно показываем
  assert.match(host.textContent, /по умолчанию:/);
  assert.equal(ui.rowFor('biz-director') !== null, true);
});

/* ─────────────────────────────────────── строка на вкладке «Команда» + диалог */

test('строка «модель: …» + «Изменить» открывает тот же редактор', async () => {
  installDom();
  const calls = [];
  const line = agentModelLine(ROOM, 'biz-cfo', ROOM.members[1], {
    catalog: CATALOG, onEdit: (profile) => calls.push(profile),
  });
  assert.match(line.textContent, /модель: GPT-5/);
  assert.match(line.textContent, /без размышления/);
  const chip = byClass(line, 'agent-model__line-chip')[0];
  assert.match(chip.getAttribute('title'), /openai/, 'технический id — только в подсказке');
  assert.match(chip.getAttribute('title'), /изменено для этой комнаты/);
  const btn = byClass(line, 'agent-model__line-btn')[0];
  btn.dispatch('click', { type: 'click', target: btn });
  assert.deepEqual(calls, ['biz-cfo']);

  // у участника без записи в комнате — «как у агента» и нейтральный чип
  const plain = agentModelLine(ROOM, 'biz-director', ROOM.members[0], { catalog: CATALOG });
  assert.match(plain.textContent, /модель: Qwen 3\.6/);
  assert.equal(String(byClass(plain, 'agent-model__line-chip')[0].className).includes('chip--accent'), false);
});

test('диалог редактора: заголовок, «Применить», закрытие', async () => {
  installDom();
  const api = fakeApi();
  const done = openAgentModelEditor({ api }, {
    room: ROOM, profile: 'biz-director', member: ROOM.members[0], catalog: CATALOG,
  });
  await new Promise((r) => setTimeout(r, 0));
  const overlay = globalThis.document.body.childNodes.find((n) => n.nodeType === 1);
  assert.ok(overlay, 'диалог смонтирован');
  assert.match(overlay.textContent, /Модель участника/);
  assert.match(overlay.textContent, /Применить/);
  const close = walk(overlay, (n) => n.tagName === 'button' && n.textContent === 'Закрыть')[0];
  assert.ok(close);
  close.dispatch('click', { type: 'click', target: close });
  assert.equal(await done, false);
  assert.equal(globalThis.document.body.childNodes.length, 0, 'подложка убрана');
});

test('CSS: список моделей прокручивается сам и не вылезает за экран', () => {
  const css = readFileSync(new URL('../web/css/agent-model.css', import.meta.url), 'utf8');
  assert.match(css, /\.agent-model__models\s*{[^}]*max-height:[^}]*overflow-y:\s*auto/s);
  // Справа в поле — стрелка списка; микрофона здесь нет (жалоба 04.11).
  assert.match(css, /\.agent-model__search\s*{[^}]*padding-right:\s*34px/s,
    'место под стрелку списка');
  assert.equal(/\.agent-model__search\s*{[^}]*padding-right:\s*40px/s.test(css), false,
    'место под микрофон больше не резервируется');
  assert.match(css, /\.agent-model \.segmented\s*{[^}]*flex-wrap:\s*wrap/s,
    'семь уровней переносятся, а не уезжают за край');
});
