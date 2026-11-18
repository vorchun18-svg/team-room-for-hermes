/**
 * worklog-ui.test.mjs — блок «Ход работы» (W20-FB) без браузера.
 *
 * Проверяем то, что видит человек: индикатор «Сейчас работают…», ленту по
 * участникам с аватарами, рассуждения курсивом и «ещё», передачи со стрелкой,
 * промежуточный итог сторожа, личный переключатель Подробно/Кратко/Выкл.,
 * кнопку «К новым» вместо автопрокрутки и то, что опрос не пересобирает
 * строки (нет мигания и потери прокрутки).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, FakeElement } from './fake-dom.mjs';

installFakeDom();

/* ── мини-DOM: методы, которые есть в браузере, но не в fake-dom ─────────── */

if (typeof FakeElement.prototype.replaceChildren !== 'function') {
  FakeElement.prototype.replaceChildren = function replaceChildren(...nodes) {
    this.childNodes = [];
    for (const node of nodes) if (node) this.appendChild(node);
  };
}
if (typeof FakeElement.prototype.removeChild !== 'function') {
  FakeElement.prototype.removeChild = function removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
    return node;
  };
}
if (typeof FakeElement.prototype.insertBefore !== 'function') {
  FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const idx = ref ? this.childNodes.indexOf(ref) : -1;
    if (idx === -1) this.childNodes.push(node);
    else this.childNodes.splice(idx, 0, node);
    node.parentNode = this;
    return node;
  };
}
if (typeof FakeElement.prototype.replaceChild !== 'function') {
  FakeElement.prototype.replaceChild = function replaceChild(node, old) {
    const idx = this.childNodes.indexOf(old);
    if (idx === -1) throw new Error('replaceChild: нет такого узла');
    node.parentNode = this;
    old.parentNode = null;
    this.childNodes[idx] = node;
    return old;
  };
}
if (typeof FakeElement.prototype.remove !== 'function') {
  FakeElement.prototype.remove = function remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  };
}

function matches(el, selector) {
  const s = String(selector).trim();
  if (!s) return false;
  const tag = String(el.tagName || '').toLowerCase();
  if (s.startsWith('.')) return el.className.split(/\s+/).includes(s.slice(1));
  if (s.startsWith('[')) {
    const m = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(s);
    if (!m) return false;
    return m[2] === undefined ? el.hasAttribute(m[1]) : el.getAttribute(m[1]) === m[2];
  }
  return tag === s.toLowerCase();
}
if (typeof FakeElement.prototype.querySelectorAll !== 'function') {
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
}
if (typeof FakeElement.prototype.querySelector !== 'function') {
  FakeElement.prototype.querySelector = function querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  };
}

// localStorage: личный выбор уровня живёт там
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(String(k)) ? store.get(String(k)) : null),
  setItem: (k, v) => { store.set(String(k), String(v)); },
  removeItem: (k) => { store.delete(String(k)); },
  clear: () => store.clear(),
};

const { h } = await import('../web/js/util.js');
const {
  mountWorklog, mergeItems, runsOf, effectiveMode, levelOf, pollDelay, liveLine,
  liveListText, handoffParts, digestOf, countText, normalizeItem, toolIcon,
  TITLE, POLL_LIVE, POLL_DONE, MAX_ITEMS,
} = await import('../web/js/views/request-worklog.js');

test.beforeEach(() => { store.clear(); });

/* ─────────────────────────────────────────────────────────── помощники */

const RID = 't_1a2b3c4d';
const ROOM = 'demo';

function el(tag = 'div') { return h(tag, null); }

function collect(root, pred) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (pred(child)) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}
const byClass = (root, name) => collect(root, (el) => el.classList.contains(name));
const textOf = (node) => String(node ? node.textContent : '');

/** Лента с вымышленными участниками: кафе «Лето», поставщик «Ромашка». */
function demoPayload(extra = {}) {
  return {
    request: RID,
    room: ROOM,
    mode: 'full',
    level: 'thoughts',
    items: [
      { id: 'w_01', ts: 1762749600, card_id: 't_1a2b3c4d', profile: 'team-director', label: 'Глеб · руководитель', role: 'orchestrator', kind: 'thought', text: 'думает: упаковка для кафе «Лето» — сначала разложу на три части', tool: '', ok: null },
      { id: 'w_02', ts: 1762749605, card_id: 't_1a2b3c4d', profile: 'team-director', label: 'Глеб · руководитель', role: 'orchestrator', kind: 'handoff', text: 'Руководитель → Операции: «Собрать шорт-лист поставщиков упаковки»', tool: '', ok: null },
      { id: 'w_03', ts: 1762749610, card_id: 't_9f3c2a13', profile: 'team-ops', label: 'Марина · поставщики', role: 'worker', kind: 'action', text: 'читает файл …\\Отчёты\\котировки\\итоговая-таблица-поставщиков-упаковки-2026-11.xlsx', tool: 'read_file', ok: null },
      { id: 'w_04', ts: 1762749612, card_id: 't_9f3c2a13', profile: 'team-ops', label: 'Марина · поставщики', role: 'worker', kind: 'result', text: 'готово · 4 строки', tool: 'read_file', ok: true },
      { id: 'w_05', ts: 1762749614, card_id: 't_9f3c2a13', profile: 'team-ops', label: 'Марина · поставщики', role: 'worker', kind: 'status', text: 'взял в работу «Собрать шорт-лист»', tool: '', ok: null },
      { id: 'w_06', ts: 1762749620, card_id: null, profile: '', label: '', role: '', kind: 'digest', text: 'Прошло 3 мин. Работают: Марина — читает договоры. Готово 2 из 5.', tool: '', ok: null },
    ],
    cursor: 'w1.cursor1',
    more: false,
    live: [
      { profile: 'team-ops', label: 'Марина · поставщики', role: 'worker', card_id: 't_9f3c2a13', state: 'working', since: 1762749540, last_action: 'собирает котировки', tools: 7, messages: 12, stuck: false, stuck_text: '' },
      { profile: 'team-cfo', label: 'Ольга · финансы', role: 'worker', card_id: 't_9f3c2a14', state: 'waiting', since: 1762749300, last_action: 'ждёт вашего ответа по MOQ', tools: 3, messages: 6, stuck: false, stuck_text: '' },
    ],
    digest: { id: 'd_1', at: 1762749620, text: 'Прошло 3 мин. Работают: Марина — читает договоры. Готово 2 из 5.', minutes: 3, workers: [], done: 2, total: 5 },
    stuck: [],
    generated_at: 1762749630,
    ...extra,
  };
}

function makeApi(get) {
  const calls = [];
  return {
    calls,
    get: async (path) => { calls.push(path); return get(path); },
    post: async (path) => { calls.push('POST ' + path); return {}; },
    patch: async () => ({}),
  };
}

async function mountWith(payload, { detail = { status: 'working' }, roomId = ROOM } = {}) {
  const host = el();
  const api = makeApi(async () => payload);
  const ctl = mountWorklog(host, { api, toast: () => {} }, {
    roomId, rid: RID, detail, autoPoll: false,
  });
  await ctl.ready;
  return { host, ctl, api };
}

/* ─────────────────────────────────────────────── чистые помощники */

test('mergeItems: без дублей, по времени, не больше лимита', () => {
  const a = [{ id: 'b', ts: 2, kind: 'action', text: 'второе' }, { id: 'a', ts: 1, kind: 'action', text: 'первое' }];
  const b = [{ id: 'b', ts: 2, kind: 'action', text: 'второе (новое)' }, { id: 'c', ts: 3, kind: 'action', text: 'третье' }];
  const out = mergeItems(a, b);
  assert.deepEqual(out.map((i) => i.id), ['a', 'b', 'c']);
  assert.equal(out[1].text, 'второе (новое)');
  assert.equal(mergeItems(out, [], 2).length, 2);
  assert.deepEqual(mergeItems(out, [], 2).map((i) => i.id), ['b', 'c']);
});

test('normalizeItem: id есть всегда (он же data-key), мусор отбрасывается', () => {
  assert.equal(normalizeItem(null), null);
  assert.equal(normalizeItem({ kind: 'action', text: '   ' }), null);
  const one = normalizeItem({ ts: 10, profile: 'team-ops', kind: 'action', text: 'пишет в карточку' });
  assert.ok(one.id);
  assert.equal(one.kind, 'action');
  const wrong = normalizeItem({ id: 'x', ts: 1, kind: 'непонятное', text: 'что-то' });
  assert.equal(wrong.kind, 'action');
});

test('runsOf: подряд идущие записи одного участника — одна группа', () => {
  const items = [
    { id: '1', profile: 'a', label: 'Анна', kind: 'action', text: 'x' },
    { id: '2', profile: 'a', label: 'Анна', kind: 'action', text: 'y' },
    { id: '3', profile: 'b', label: 'Лев', kind: 'action', text: 'z' },
    { id: '4', profile: 'a', label: 'Анна', kind: 'action', text: 'w' },
  ];
  const runs = runsOf(items);
  assert.equal(runs.length, 3);
  assert.equal(runs[0].items.length, 2);
  assert.equal(runs[1].label, 'Лев');
});

test('режимы: личный выбор только сужает настройку комнаты', () => {
  assert.equal(effectiveMode('full', ''), 'full');
  assert.equal(effectiveMode('full', 'brief'), 'brief');
  assert.equal(effectiveMode('full', 'off'), 'off');
  assert.equal(effectiveMode('brief', 'full'), 'brief');   // рассуждений нет в комнате
  assert.equal(effectiveMode('brief', 'off'), 'off');
  assert.equal(effectiveMode('off', 'full'), 'off');       // комната выключила — не обещаем
  assert.equal(effectiveMode('off', ''), 'off');
  assert.equal(levelOf('full'), 'thoughts');
  assert.equal(levelOf('brief'), 'steps');
  assert.equal(levelOf('off'), 'steps');
});

test('опрос: 3 с в работе, 30 с после', () => {
  assert.equal(pollDelay('working'), POLL_LIVE);
  assert.equal(pollDelay('reviewing'), POLL_LIVE);
  assert.equal(pollDelay('done'), POLL_DONE);
  assert.equal(pollDelay('failed'), POLL_DONE);
  assert.equal(POLL_LIVE, 3000);
  assert.equal(POLL_DONE, 30000);
});

test('liveLine: «Сейчас работают: … — N мин», ожидание отдельно', () => {
  const now = (1762749600 + 120) * 1000;   // через 2 минуты после старта
  const live = [
    { profile: 'team-ops', label: 'Марина', state: 'working', since: 1762749600 },
    { profile: 'team-cfo', label: 'Ольга', state: 'waiting', since: 1762749500 },
  ];
  assert.equal(liveLine(live, { nowMs: now }), 'Сейчас работают: Марина — 2 мин');
  assert.equal(liveListText(live, now), 'Марина — 2 мин, Ольга — ждёт вашего ответа');
  assert.equal(liveLine([], { final: false }), 'Сейчас никто не работает');
  assert.equal(liveLine([], { final: true }), 'Работа закончена — новых событий не будет');
  const waitingOnly = [{ profile: 'x', label: 'Лев', state: 'waiting', since: 0 }];
  assert.match(liveLine(waitingOnly), /^Сейчас ждут вашего ответа: Лев — ждёт вашего ответа$/);
  // Зависший не идёт в одном ряду с работающими: это повод посмотреть доску.
  const withStuck = [
    { profile: 'a', label: 'Марина', state: 'working', since: 1762749600 },
    { profile: 'b', label: 'Юрист', state: 'stuck', since: 1762749000, stuck_minutes: 18 },
  ];
  assert.equal(liveLine(withStuck, { nowMs: now }),
    'Сейчас работают: Марина — 2 мин. Похоже, завис: Юрист — возможно завис: 18 мин без событий');
});

test('handoffParts: «Руководитель → Юрист: …» на части', () => {
  assert.deepEqual(handoffParts('Руководитель → Операции: «Собрать шорт-лист»'),
    { from: 'Руководитель', to: 'Операции', what: '«Собрать шорт-лист»' });
  assert.equal(handoffParts('без стрелки'), null);
});

test('digestOf: принимает и ts, и at (демо отдаёт at)', () => {
  const d = digestOf({ digest: { id: 'd', at: 100, text: 'Прошло 3 мин', done: 2, total: 5 } });
  assert.equal(d.ts, 100);
  assert.equal(d.done, 2);
  assert.equal(digestOf({ digest: { text: '  ' } }), null);
  assert.equal(digestOf({}), null);
});

test('подписи и иконки инструментов — человеческие', () => {
  assert.equal(countText(1), '1 событие');
  assert.equal(countText(3), '3 события');
  assert.equal(countText(7), '7 событий');
  assert.equal(toolIcon('web_search'), 'globe');
  assert.equal(toolIcon('read_file'), 'file-text');
  assert.equal(toolIcon('kanban_create'), 'checklist');
  assert.equal(toolIcon(''), 'circle-filled');
});

/* ─────────────────────────────────────────────────────── блок в DOM */

test('блок: заголовок, «Сейчас работают», лента по участникам с аватарами', async () => {
  const { host, ctl } = await mountWith(demoPayload());
  const section = ctl.element;
  assert.equal(section.parentNode, host);
  assert.equal(section.classList.contains('wl'), true);
  assert.equal(section.hasAttribute('data-keep-tree'), true, 'live-patch не должен пересобирать блок');

  assert.equal(textOf(section.querySelector('.wl__title')), TITLE);
  assert.match(textOf(section.querySelector('.wl__livetext')), /^Сейчас работают: Марина/);
  assert.ok(byClass(section, 'avatar').length >= 2, 'аватары участников');

  const feed = section.querySelector('.wl__feed');
  assert.equal(feed.getAttribute('data-scroll-key'), 'worklog:' + RID);

  const rows = byClass(feed, 'wl-item');
  assert.equal(rows.length, 6);
  assert.deepEqual(rows.map((r) => r.dataset.key),
    ['w_01', 'w_02', 'w_03', 'w_04', 'w_05', 'w_06']);

  // Рассуждение — курсивом, передача — со стрелкой, итог — отдельным блоком.
  assert.equal(byClass(feed, 'wl-item__text--thought').length, 1);
  assert.equal(byClass(feed, 'wl-item__text--handoff').length, 1);
  assert.ok(textOf(feed.querySelector('.wl-item__from')).includes('Руководитель'));
  assert.ok(textOf(feed.querySelector('.wl-item__to')).includes('Операции'));
  assert.equal(byClass(feed, 'wl-item--digest').length, 1);
  assert.match(textOf(feed.querySelector('.wl-item--digest')), /Промежуточный итог/);

  // Действие — строкой с иконкой; ошибки/успех различаются.
  const action = feed.querySelector('.wl-item--action');
  assert.ok(action.querySelector('.codicon'));
  assert.equal(byClass(feed, 'wl-item__ico').some((n) => n.classList.contains('is-ok')), true);
});

test('рассуждение: длинное — свёрнуто и раскрывается «ещё»', async () => {
  const long = 'думает: ' + 'проверяет пункты договора, сроки и объёмы кафе «Лето». '.repeat(30);
  const payload = demoPayload({
    items: [{ id: 'w_long', ts: 1762749600, profile: 'team-lawyer', label: 'Антон · юрист', kind: 'thought', text: long }],
  });
  const { ctl } = await mountWith(payload);
  const text = ctl.element.querySelector('.wl-item__text--thought');
  assert.equal(text.classList.contains('is-clamped'), true);
  const btn = ctl.element.querySelector('.wl-item__more');
  assert.equal(textOf(btn), 'ещё');
  btn.dispatch('click', { type: 'click', target: btn });
  assert.equal(text.classList.contains('is-clamped'), false);
  assert.equal(textOf(btn), 'свернуть');
});

test('переключатель режима: «Кратко» просит level=steps, «Выкл.» не читает', async () => {
  const { ctl, api } = await mountWith(demoPayload());
  const modes = byClass(ctl.element, 'wl__mode');
  assert.deepEqual(modes.map((b) => b.dataset.mode), ['full', 'brief', 'off']);
  assert.equal(modes[0].getAttribute('aria-pressed'), 'true');

  modes[1].dispatch('click', { type: 'click', target: modes[1] });
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(api.calls.some((p) => p.includes('level=steps')), 'кратко = только действия');

  const before = api.calls.length;
  modes[2].dispatch('click', { type: 'click', target: modes[2] });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(api.calls.length, before, '«Выкл.» не должен ходить на сервер');
  assert.match(textOf(ctl.element.querySelector('.wl__empty')), /выключен/i);
  assert.equal(store.get('teamroom:worklog:mode'), '"off"');
});

test('настройка комнаты «Кратко»: «Подробно» недоступно, с объяснением', async () => {
  const { ctl } = await mountWith(demoPayload({ mode: 'brief', level: 'steps' }));
  const full = byClass(ctl.element, 'wl__mode').find((b) => b.dataset.mode === 'full');
  assert.equal(full.disabled, true);
  assert.match(full.getAttribute('title'), /настройках комнаты/);
});

test('комната выключила ленту: читаем один раз и больше не спрашиваем', async () => {
  const api = makeApi(async () => demoPayload({ mode: 'off', items: [], live: [] }));
  const host = el();
  const ctl = mountWorklog(host, { api }, { roomId: ROOM, rid: RID, detail: { status: 'working' }, autoPoll: false });
  await ctl.ready;
  // Первое чтение неизбежно: до ответа мы не знаем настройку комнаты. Сервер
  // в режиме «выключено» ничего не читает и сразу говорит mode:off.
  assert.equal(api.calls.length, 1);
  await ctl.refresh();
  await ctl.refresh();
  assert.equal(api.calls.length, 1, 'дальше в «выключено» на сервер не ходим');
  assert.match(textOf(ctl.element.querySelector('.wl__empty')), /настройках комнаты/);
  assert.equal(textOf(ctl.element.querySelector('.wl__chip')), 'выключено');
  const full = byClass(ctl.element, 'wl__mode').find((b) => b.dataset.mode === 'full');
  assert.equal(full.disabled, true);
});

test('«к новым» вместо автопрокрутки: прокрутка человека не меняется', async () => {
  let payload = demoPayload();
  const host = el();
  const api = makeApi(async () => payload);
  const ctl = mountWorklog(host, { api }, { roomId: ROOM, rid: RID, detail: { status: 'working' }, autoPoll: false });
  await ctl.ready;

  const feed = ctl.element.querySelector('.wl__feed');
  // Человек читает середину ленты.
  feed.scrollHeight = 800;
  feed.clientHeight = 200;
  feed.scrollTop = 120;
  feed.dispatch('scroll', { type: 'scroll', target: feed });

  const firstRow = ctl.element.querySelector('.wl-item');
  payload = demoPayload({
    items: demoPayload().items.concat([
      { id: 'w_07', ts: 1762749630, profile: 'team-ops', label: 'Марина · поставщики', kind: 'action', text: 'ищет в сети: котировки упаковки', tool: 'web_search' },
    ]),
  });
  await ctl.refresh();

  assert.equal(feed.scrollTop, 120, 'прокрутку не трогаем');
  const btn = ctl.element.querySelector('.wl__new');
  assert.equal(btn.hasAttribute('hidden'), false);
  assert.match(textOf(btn), /К новым · 1/);
  // Та же строка осталась тем же узлом — лента не пересобиралась (не мигает).
  assert.equal(ctl.element.querySelector('.wl-item'), firstRow);
});

test('«К новым» возвращает вниз и исчезает', async () => {
  let payload = demoPayload();
  const host = el();
  const api = makeApi(async () => payload);
  const ctl = mountWorklog(host, { api }, { roomId: ROOM, rid: RID, detail: { status: 'working' }, autoPoll: false });
  await ctl.ready;
  const feed = ctl.element.querySelector('.wl__feed');
  feed.scrollHeight = 900;
  feed.clientHeight = 200;
  feed.scrollTop = 0;
  feed.dispatch('scroll', { type: 'scroll', target: feed });
  payload = demoPayload({ items: demoPayload().items.concat([
    { id: 'w_08', ts: 1762749640, profile: 'team-ops', label: 'Марина', kind: 'action', text: 'пишет в карточку' },
  ]) });
  await ctl.refresh();
  const btn = ctl.element.querySelector('.wl__new');
  assert.equal(btn.hasAttribute('hidden'), false);
  btn.dispatch('click', { type: 'click', target: btn });
  assert.equal(btn.hasAttribute('hidden'), true);
  assert.equal(feed.scrollTop, 900);
});

test('запрос готов: блок сворачивается один раз и опрос реже', async () => {
  const { ctl } = await mountWith(demoPayload(), { detail: { status: 'working' } });
  const box = ctl.element.querySelector('details');
  assert.equal(box.open, true);
  ctl.update({ status: 'done' });
  assert.equal(box.open, false);
  assert.equal(pollDelay('done'), POLL_DONE);
  // Человек сам раскрыл — новая перерисовка страницы не сворачивает обратно.
  ctl.element.querySelector('.wl__summary').dispatch('click', { type: 'click' });
  assert.equal(box.open, true);
  ctl.update({ status: 'done' });
  assert.equal(box.open, true);
});

test('итог сторожа виден и в свёрнутом блоке', async () => {
  const { ctl } = await mountWith(demoPayload(), { detail: { status: 'done' } });
  const strip = ctl.element.querySelector('.wl__digest');
  assert.equal(strip.hasAttribute('hidden'), false);
  assert.match(textOf(strip), /Прошло 3 мин/);
});

test('«возможно завис»: пометка и кнопка «Проверить доску сейчас»', async () => {
  const payload = demoPayload({
    stuck: [{ card_id: 't_9f3c2a13', profile: 'team-ops', label: 'Марина · поставщики', minutes: 18, text: 'возможно завис: нет активности 18 мин. Проверьте доску' }],
  });
  const { ctl } = await mountWith(payload);
  const row = ctl.element.querySelector('.wl__stuckrow');
  assert.ok(row);
  assert.match(textOf(row), /возможно завис/);
  assert.match(textOf(row.querySelector('.wl__kick')), /Проверить доску сейчас/);
});

test('личный «Выкл.» при открытии: без запросов и с понятным текстом', async () => {
  store.set('teamroom:worklog:mode', '"off"');
  const api = makeApi(async () => demoPayload());
  const host = el();
  const ctl = mountWorklog(host, { api }, { roomId: ROOM, rid: RID, detail: { status: 'working' }, autoPoll: false });
  await ctl.ready;
  assert.equal(api.calls.length, 0);
  assert.match(textOf(ctl.element.querySelector('.wl__empty')), /для этого экрана/);
  assert.equal(byClass(ctl.element, 'wl__mode').find((b) => b.dataset.mode === 'off').getAttribute('aria-pressed'), 'true');
  // «Включить обратно» — кнопкой «Кратко»: лента читается уже в кратком виде.
  byClass(ctl.element, 'wl__mode').find((b) => b.dataset.mode === 'brief').dispatch('click', { type: 'click' });
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(api.calls.some((p) => p.includes('level=steps')));
});

test('ошибка ленты не стирает уже показанное', async () => {
  let fail = false;
  const host = el();
  const api = makeApi(async () => {
    if (fail) throw Object.assign(new Error('Лента временно недоступна'), { code: 'http_503' });
    return demoPayload();
  });
  const ctl = mountWorklog(host, { api }, { roomId: ROOM, rid: RID, detail: { status: 'working' }, autoPoll: false });
  await ctl.ready;
  const rowsBefore = byClass(ctl.element.querySelector('.wl__feed'), 'wl-item').length;
  fail = true;
  await ctl.refresh();
  assert.equal(byClass(ctl.element.querySelector('.wl__feed'), 'wl-item').length, rowsBefore);
  assert.equal(ctl.element.querySelector('.wl__error').hasAttribute('hidden'), false);
  assert.match(textOf(ctl.element.querySelector('.wl__error')), /недоступна/);
});

test('лента ограничена 200 записями', () => {
  const many = [];
  for (let i = 0; i < 260; i += 1) {
    many.push({ id: 'w_' + i, ts: 1762749600 + i, profile: 'team-ops', kind: 'action', text: 'шаг ' + i });
  }
  const out = mergeItems([], many);
  assert.equal(out.length, MAX_ITEMS);
  assert.equal(out[out.length - 1].id, 'w_259');
  assert.equal(out[0].id, 'w_60');
});

/* ─────────────── W21-L1: «Показать раньше» по серверному курсору ──────────── */

function olderPayload() {
  return demoPayload({
    items: [
      { id: 'w_00a', ts: 1762749500, card_id: 't_1a2b3c4d', profile: 'team-director', label: 'Глеб · руководитель', role: 'orchestrator', kind: 'action', text: 'ставит задачу: «Собрать шорт-лист поставщиков»', tool: 'kanban_create', ok: null },
      { id: 'w_00b', ts: 1762749504, card_id: 't_1a2b3c4d', profile: 'team-director', label: 'Глеб · руководитель', role: 'orchestrator', kind: 'message', text: 'разложил работу на три части', tool: '', ok: null },
    ],
    before: 'w1.b0',
    more: true,
  });
}

test('«Показать раньше» догружает по курсору before: без повторов и без прыжка', async () => {
  const first = demoPayload({ before: 'w1.b1', more: true });
  const host = el();
  const api = makeApi(async (path) => (String(path).includes('before=') ? olderPayload() : first));
  const ctl = mountWorklog(host, { api }, { roomId: ROOM, rid: RID, detail: { status: 'working' }, autoPoll: false });
  await ctl.ready;

  const btn = ctl.element.querySelector('.wl__earlier');
  assert.equal(btn.hasAttribute('hidden'), false, 'кнопка видна, пока есть история');
  assert.match(textOf(btn), /Показать раньше/);

  const feed = ctl.element.querySelector('.wl__feed');
  feed.scrollHeight = 600;
  feed.clientHeight = 200;
  feed.scrollTop = 140;

  btn.dispatch('click');
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));

  const back = api.calls.find((path) => String(path).includes('before='));
  assert.ok(back, 'второй запрос идёт с курсором before');
  assert.match(String(back), /before=w1\.b1/);
  assert.match(String(back), /limit=80/, '«раньше» — страница, а не всё окно сразу');

  const ids = collect(ctl.element, (n) => n.classList.contains('wl-item'))
    .map((row) => String(row.getAttribute('data-key') || ''));
  assert.equal(ids.length, 8, 'шесть записей первой страницы + две старше');
  assert.equal(new Set(ids).size, ids.length, 'повторов на стыке страниц нет');
  assert.ok(ids.includes('w_00a') && ids.includes('w_01'));
  // Позиция чтения сохранена: прокрутка не прыгнула вверх.
  assert.ok(Number(feed.scrollTop) >= 140, 'прокрутка не сбросилась');
});

test('«Показать раньше» на сервере без курсора расширяет окно до 200', async () => {
  const first = demoPayload({ more: true });        // before нет — старый сервер
  const host = el();
  const api = makeApi(async () => first);
  const ctl = mountWorklog(host, { api }, { roomId: ROOM, rid: RID, detail: { status: 'working' }, autoPoll: false });
  await ctl.ready;
  ctl.element.querySelector('.wl__earlier').dispatch('click');
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  const fallback = api.calls[api.calls.length - 1];
  assert.match(String(fallback), /limit=200/);
  assert.doesNotMatch(String(fallback), /before=/);
});
