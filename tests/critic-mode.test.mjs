/**
 * critic-mode.test.mjs — режим критика «до полного одобрения» и объяснение раундов
 * (волна 13, пакет E, пункты 7 и 9).
 *
 *  • criticInfo/criticRoundLabel/criticProgressLabel: раунды, режим, ход цикла,
 *    состояния paused / stalled / capped — без ломки прежних ключей;
 *  • диалог комнаты: три варианта раундов, «?» с пояснением, страховочный потолок,
 *    payload уходит с critic_mode / critic_round_cap;
 *  • единое слово «раунд» в интерфейсе: в FAQ и в текстах настроек нет «прогонов».
 *
 * DOM — мини-DOM из fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { FakeElement, createFakeDocument } from './fake-dom.mjs';

/* ───────────────────────────────────────────────────────── мини-DOM */

function installDom() {
  const doc = createFakeDocument();
  doc.body = new FakeElement('body');
  doc.activeElement = null;
  doc.listeners = Object.create(null);
  doc.addEventListener = (type, fn) => {
    (doc.listeners[type] || (doc.listeners[type] = [])).push(fn);
  };
  doc.removeEventListener = (type, fn) => {
    const list = doc.listeners[type] || [];
    const idx = list.indexOf(fn);
    if (idx !== -1) list.splice(idx, 1);
  };
  globalThis.document = doc;
  return doc;
}

FakeElement.prototype.focus = function focus() { globalThis.document.activeElement = this; };
FakeElement.prototype.blur = function blur() {};
FakeElement.prototype.scrollIntoView = function scrollIntoView() {};
FakeElement.prototype.setSelectionRange = function setSelectionRange() {};
FakeElement.prototype.closest = function closest() { return null; };
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
FakeElement.prototype.insertBefore = function insertBefore(node, ref) {
  const idx = ref ? this.childNodes.indexOf(ref) : -1;
  if (idx === -1) return this.appendChild(node);
  node.parentNode = this;
  return this.childNodes.splice(idx, 0, node)[0];
};
Object.defineProperty(FakeElement.prototype, 'options', {
  get() { return this.childNodes.filter((n) => n.nodeType === 1); },
  configurable: true,
});

function walk(node, out = []) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    out.push(child);
    walk(child, out);
  }
  return out;
}

function byAttr(root, name, value) {
  return walk(root).find((el) => el.getAttribute(name) === value) || null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ─────────────────────────────────────── criticInfo: режим и ход цикла */

const PROGRESS = { round: 3, rounds: 3, issues_before: 4, closed: 2, open: 2,
  closed_items: ['срез cfo · старая себестоимость'], open_items: ['шаги · нет сроков'],
  verdict: 'revise', stop: null };

test('criticInfo: прежние ключи остаются (регрессия режима 1–2 раунда)', async () => {
  const { criticInfo, criticRoundLabel } = await import('../web/js/views/request-parts-model.js');
  const info = criticInfo({
    status: 'reviewing',
    synthesis: { version: 2 },
    critic: { enabled: true, state: 'reviewing', verdict: null, round: 2, max_rounds: 2,
      issues: [], task_id: 't_crit0002', mode: 'rounds' },
  });
  assert.equal(info.enabled, true);
  assert.equal(info.state, 'reviewing');
  assert.equal(info.round, 2);
  assert.equal(info.maxRounds, 2);
  assert.equal(info.mode, 'rounds');
  assert.equal(info.modeLabel, 'по числу раундов');
  assert.equal(info.taskId, 't_crit0002');
  assert.equal(criticRoundLabel(info), 'раунд 2 из 2');
});

test('criticInfo: «до полного одобрения» — подпись, ход, отсутствие потолка в тексте', async () => {
  const { criticInfo, criticRoundLabel, criticProgressLabel } = await import('../web/js/views/request-parts-model.js');
  const info = criticInfo({
    status: 'reviewing',
    synthesis: { version: 3 },
    critic: { enabled: true, state: 'reviewing', round: 3, max_rounds: 1, mode: 'until_approved',
      round_cap: 12, issues: [{ where: 'Шаги', problem: 'нет сроков', fix: 'проставить' }],
      progress: PROGRESS },
  });
  assert.equal(info.mode, 'until_approved');
  assert.equal(info.roundCap, 12);
  assert.equal(criticRoundLabel(info),
    'раунд 3 · до полного одобрения (потолок 12)');
  assert.equal(criticProgressLabel(info), 'замечаний было 4, закрыто 2, осталось 2');
});

test('criticInfo: остановка по страховке — state stalled и текст причины', async () => {
  const { criticInfo } = await import('../web/js/views/request-parts-model.js');
  const info = criticInfo({
    status: 'done',
    synthesis: { version: 4 },
    critic: {
      enabled: true, state: 'stalled', round: 3, max_rounds: 1, mode: 'until_approved',
      round_cap: 12, issues: [], progress: PROGRESS,
      stopped: { kind: 'no_progress', round: 3, needs_user: true,
        message: 'Критик повторяет одни и те же замечания 3 раунда подряд.' },
    },
  });
  assert.equal(info.state, 'stalled');
  assert.equal(info.label, 'остановлено');
  assert.match(info.note, /повторяет одни и те же замечания/);
  assert.equal(info.stop.kind, 'no_progress');
  assert.equal(info.stop.needs_user, true);
});

test('criticInfo: потолок раундов — свой ярлык и текст', async () => {
  const { criticInfo } = await import('../web/js/views/request-parts-model.js');
  const info = criticInfo({
    synthesis: { version: 5 },
    critic: { enabled: true, state: 'capped', round: 12, max_rounds: 1, mode: 'until_approved',
      round_cap: 12, issues: [],
      stopped: { kind: 'cap', round: 12, cap: 12, needs_user: true,
        message: 'Достигнут потолок 12 раундов — цикл остановлен.' } },
  });
  assert.equal(info.state, 'capped');
  assert.equal(info.label, 'потолок раундов');
  assert.match(info.note, /потолок 12 раундов/);
});

test('criticInfo: пауза из-за лимита провайдера объясняется по-русски', async () => {
  const { criticInfo } = await import('../web/js/views/request-parts-model.js');
  const until = Math.floor(Date.now() / 1000) + 600;
  const info = criticInfo({
    synthesis: { version: 1 },
    critic: { enabled: true, state: 'paused', round: 2, mode: 'until_approved',
      max_rounds: 1, round_cap: 12, issues: [], paused: { until, round: 2 } },
  });
  assert.equal(info.state, 'paused');
  assert.equal(info.label, 'пауза');
  assert.match(info.note, /лимит/i);
  assert.match(info.note, /продолжим сами/);
});

test('criticInfo: нет данных о ходе — строка прогресса пустая, а не «0 из 0»', async () => {
  const { criticInfo, criticProgressLabel } = await import('../web/js/views/request-parts-model.js');
  const info = criticInfo({ critic: { enabled: true, state: 'waiting', round: 0, max_rounds: 1 } });
  assert.equal(criticProgressLabel(info), '');
});

/* ─────────────────────────────────── диалог комнаты: три варианта раундов */

const PROFILES = [
  { profile: 'biz-director', title: 'Директор Лето', kind: 'director' },
  { profile: 'biz-critic', title: 'Критик', kind: 'critic' },
  { profile: 'biz-ops', title: 'Операции', kind: 'worker' },
];

function dialogCtx() {
  const calls = [];
  const api = {
    get: async (path) => (path === '/api/profiles' ? PROFILES : { rooms: [] }),
    post: async (path, body) => {
      calls.push(['POST', path, body]);
      if (path === '/api/rooms/check-dir') return { allowed: true, exists: true, will_create: false, warnings: [] };
      return { id: 'test-room' };
    },
    patch: async (path, body) => { calls.push(['PATCH', path, body]); return { id: 'test-room' }; },
    del: async () => ({}),
  };
  return { api, calls, toast: () => {}, navigate: () => {}, store: {} };
}

const EDIT_ROOM = {
  id: 'cafe', title: 'Лето', icon: 'cake', board: 'team', tenant: 'room:cafe',
  orchestrator: 'biz-director', critic: 'biz-critic',
  members: [{ profile: 'biz-ops', role: 'worker' }],
  context_files: [], project_dir: '', critic_pass: true, max_rounds: 2,
  critic_mode: 'rounds', critic_round_cap: 12,
  launch_mode: 'plan_first', max_parallel_streams: 2, project_mode: true,
};

function roundsSelect(doc) {
  return byAttr(doc.body, 'id', 'roomdlg-critic-rounds');
}

test('диалог: три варианта раундов, включая «До полного одобрения критиком»', async () => {
  installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const doc = globalThis.document;
  const done = openRoomDialog(dialogCtx(), null);
  await sleep(30);

  const select = roundsSelect(doc);
  assert.ok(select, 'поле «Раунды проверки» есть в диалоге');
  const values = select.options.map((o) => o.getAttribute('value'));
  assert.deepEqual(values, ['1', '2', 'approve']);
  const labels = select.options.map((o) => o.textContent.trim());
  assert.deepEqual(labels, ['1 раунд — быстро', '2 раунда — тщательнее',
    'До полного одобрения критиком']);
  const text = doc.body.textContent;
  assert.ok(text.includes('Раунд — один цикл'), 'пояснение рядом с настройкой');
  assert.ok(!text.includes('прогон'), 'в интерфейсе нет слова «прогон»');

  const cancel = walk(doc.body).find((el) => el.tagName === 'button'
    && el.textContent.trim() === 'Отмена');
  cancel.dispatch('click');
  await done;
});

test('диалог: «?» раскрывает объяснение про критика и раунды', async () => {
  installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const doc = globalThis.document;
  const done = openRoomDialog(dialogCtx(), null);
  await sleep(30);

  const help = byAttr(doc.body, 'id', 'roomdlg-critic-help');
  assert.ok(help, 'блок пояснения существует');
  assert.ok(String(help.className).includes('hidden'), 'по умолчанию свёрнут');
  const btn = byAttr(doc.body, 'aria-controls', 'roomdlg-critic-help');
  assert.ok(btn, 'кнопка «?» связана с блоком');
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  btn.dispatch('click');
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.ok(!String(help.className).includes('hidden'), 'после нажатия — раскрыт');
  assert.ok(help.textContent.includes('один цикл «критик проверил'));
  assert.ok(help.textContent.includes('пока критик не скажет «принято»'));

  const cancel = walk(doc.body).find((el) => el.tagName === 'button'
    && el.textContent.trim() === 'Отмена');
  cancel.dispatch('click');
  await done;
});

test('диалог: «до одобрения» показывает потолок и сохраняет critic_mode', async () => {
  installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const doc = globalThis.document;
  const ctx = dialogCtx();
  const done = openRoomDialog(ctx, EDIT_ROOM);
  await sleep(30);

  const select = roundsSelect(doc);
  assert.equal(select.value, '2', 'из комнаты взят режим «2 раунда»');
  const cap = byAttr(doc.body, 'id', 'roomdlg-critic-cap');
  assert.ok(cap, 'поле потолка есть');
  const capBox = walk(doc.body).find((el) => String(el.className).includes('roomdlg__cap-box'));
  assert.ok(String(capBox.className).includes('hidden'), 'в режиме раундов потолок скрыт');

  select.value = 'approve';
  select.dispatch('change');
  assert.ok(!String(capBox.className).includes('hidden'), 'для «до одобрения» потолок виден');
  cap.value = '9';

  const form = walk(doc.body).find((el) => el.tagName === 'form');
  form.dispatch('submit', { preventDefault() {} });
  await sleep(80);

  const patch = ctx.calls.find((c) => c[0] === 'PATCH');
  assert.ok(patch, 'сохранение ушло');
  assert.equal(patch[2].critic_mode, 'until_approved');
  assert.equal(patch[2].critic_round_cap, 9);
  await done;
});

test('диалог: в режиме 1 раунда уходит critic_mode=rounds и max_rounds=1', async () => {
  installDom();
  const { openRoomDialog } = await import('../web/js/views/room-dialog.js');
  const doc = globalThis.document;
  const ctx = dialogCtx();
  const done = openRoomDialog(ctx, Object.assign({}, EDIT_ROOM, {
    critic_mode: 'until_approved', critic_round_cap: 7,
  }));
  await sleep(30);

  const select = roundsSelect(doc);
  assert.equal(select.value, 'approve', 'комната в режиме «до одобрения»');
  select.value = '1';
  select.dispatch('change');
  const form = walk(doc.body).find((el) => el.tagName === 'form');
  form.dispatch('submit', { preventDefault() {} });
  await sleep(80);
  const patch = ctx.calls.find((c) => c[0] === 'PATCH');
  assert.equal(patch[2].critic_mode, 'rounds');
  assert.equal(patch[2].max_rounds, 1);
  await done;
});

/* ─────────────────────────────── единая терминология и настройки комнаты */

test('settings: FAQ объясняет критика и раунды простыми словами', () => {
  const source = readFileSync(new URL('../web/js/views/settings.js', import.meta.url), 'utf8');
  assert.ok(source.includes('Что такое раунд критика и сколько их ставить?'));
  assert.ok(source.includes('один цикл «критик проверил'));
  assert.ok(source.includes('1 раунд — быстро'));
  assert.ok(source.includes('До полного одобрения критиком'));
  assert.ok(!/прогон/i.test(source), 'в FAQ нет слова «прогон»');
});

test('team.js: строка раундов объясняет режим и что такое раунд', () => {
  const source = readFileSync(new URL('../web/js/views/team.js', import.meta.url), 'utf8');
  assert.ok(source.includes('Критик — участник, который проверяет сводный ответ'));
  assert.ok(source.includes('пока не одобрит полностью'));
  assert.ok(source.includes('1 раунд — быстро') || source.includes("'— быстро'"),
    'подпись «быстро/тщательнее» есть в строке раундов');
  assert.ok(source.includes('critic_mode'), 'режим комнаты участвует в отрисовке');
  assert.ok(!/прогон/i.test(source), 'нет слова «прогон»');
});

test('composer: режим проверки выбирается при постановке запроса', () => {
  const source = readFileSync(new URL('../web/js/views/composer.js', import.meta.url), 'utf8');
  assert.ok(source.includes('cmp-critic-mode'));
  assert.ok(source.includes("critic_mode: 'until_approved'"));
  assert.ok(source.includes("critic_mode: 'rounds'"));
  assert.ok(!/прогон/i.test(source), 'нет слова «прогон»');
});

test('request-header: чип критика в шапке — «раунд N»', () => {
  const source = readFileSync(new URL('../web/js/views/request-header.js', import.meta.url), 'utf8');
  assert.ok(source.includes('критик: раунд'));
  assert.ok(source.includes('Раунд — один цикл'));
});
