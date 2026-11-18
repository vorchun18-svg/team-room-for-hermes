/**
 * model-guard-ui.test.mjs — «Контроль моделей» (W23-MG2), клиент без браузера.
 *
 * Проверяем то, что человек видит и нажимает:
 *  1) строка карточки участника: «Модель: <ожидаемая> · последний запуск: …✓/⚠»,
 *     «Проверить сейчас» реально перечитывает сверку;
 *  2) сравнение моделей без ложных срабатываний (регистр, `провайдер:модель`,
 *     версии `qwen36` ↔ `qwen36v` — РАЗНЫЕ модели);
 *  3) событие сторожа человеческим текстом: «Остановлено: запуск шёл на … вместо …
 *     — исправлено и перезапущено, сэкономлено ≈ $2,45»; стоимость спокойно;
 *  4) лента на странице запроса: события от новых к старым, пустая — не рисуется;
 *  5) кнопки управления работой: «Остановить» отправляет `card_stop` один раз,
 *     «Продолжить» — `card_resume`, «Сменить модель…» — `card_model` с выбором
 *     из каталога (и «по умолчанию — модель профиля» = пустые provider/model);
 *  6) строка «Остановлено вами в 10:41 · модель: …» с «Продолжить»;
 *  7) режим комнаты: по умолчанию «строго», переключается на «только предупреждать».
 *
 * DOM — tests/fake-dom.mjs. Живая проверка — Chromium на моке (tools/cdp.py).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, FakeElement } from './fake-dom.mjs';

installFakeDom();
// Диалог «Сменить модель» живёт в document.body — в мини-DOM его нет, добавляем.
if (!globalThis.document.body) globalThis.document.body = new FakeElement('body');

// Мини-DOM: методы, которыми пользуются кнопки (как в tests/approvals-ui.test.mjs).
{
  const P = FakeElement.prototype;
  if (typeof P.replaceChildren !== 'function') {
    P.replaceChildren = function replaceChildren(...nodes) {
      this.childNodes = [];
      for (const node of nodes) if (node) this.appendChild(node);
    };
  }
  if (typeof P.remove !== 'function') {
    P.remove = function remove() {
      if (!this.parentNode) return;
      const idx = this.parentNode.childNodes.indexOf(this);
      if (idx !== -1) this.parentNode.childNodes.splice(idx, 1);
      this.parentNode = null;
    };
  }
}

const {
  normalizeModel, sameModel, settingsOf, costText, clockText, memberState, stateMark,
  problemText, guardModeOf, guardModeLabel, guardEventsOf, guardEventText, guardSummary,
  runState, canStop, canResume, stoppedText, cardErrorText,
  guardLine, cardControls, stoppedLine, buildRequestFeed, guardModeField,
  openCardModelDialog, applyCardModel, modelInCatalog, GUARD_MODES,
} = await import('../web/js/views/model-guard.js');
const { MODEL_GUARD_RU } = await import('../web/js/text-ru.js');

/* ─────────────────────────────────────────────────────────── помощники тестов */

function textOf(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.textContent || '');
  let out = '';
  for (const child of node.childNodes || []) out += textOf(child) + ' ';
  return out;
}

function findByClass(node, cls) {
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    if (String(child.className || '').split(/\s+/).includes(cls)) return child;
    const nested = findByClass(child, cls);
    if (nested) return nested;
  }
  return null;
}

function allByTag(node, tag) {
  const out = [];
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) continue;
    if (String(child.tagName).toLowerCase() === tag) out.push(child);
    out.push(...allByTag(child, tag));
  }
  return out;
}

const CATALOG = {
  providers: [
    {
      id: 'lokal', label: 'Локальный сервер', configured: true,
      models: [{ id: 'dilizhans-home', label: 'Дилижанс домашний' }],
    },
    {
      id: 'oblako-a', label: 'Облако-А', configured: true,
      models: [
        { id: 'dilizhans-pro', label: 'Дилижанс про' },
        { id: 'dilizhans-ultra', label: 'Дилижанс ультра' },
      ],
    },
  ],
};

const MEMBER_BAD = {
  profile: 'coder',
  title: 'Код и автоматизация',
  expected: { provider: 'lokal', model: 'dilizhans-home', source: 'room' },
  last_run: { provider: 'oblako-a', model: 'dilizhans-ultra', at: 1793169000, cost_usd: 2.45 },
  ok: false,
  problem: '',
};

/* ─────────────────────────────────────────────────────── 1. сравнение моделей */

test('W23-MG2: сравнение моделей — без ложных «совпадает»', () => {
  assert.equal(normalizeModel('  Qwen36V '), 'qwen36v');
  assert.equal(normalizeModel('llamacpp:qwen36v'), 'qwen36v');
  assert.equal(normalizeModel('deepseek-v4.1-flash:1017'), 'deepseek-v4.1-flash:1017');

  assert.equal(sameModel('Qwen36V', 'qwen36v'), true);
  assert.equal(sameModel('llamacpp:qwen36v', 'qwen36v'), true);
  assert.equal(sameModel({ provider: 'lokal', model: 'dilizhans-home' }, { provider: 'LOKAL', model: 'DiliZhans-Home' }), true);

  // Версии и чужие модели — расхождение.
  assert.equal(sameModel('qwen36', 'qwen36v'), false);
  assert.equal(sameModel('claude-sonnet-5', 'claude-sonnet-5-5'), false);
  // Одна и та же модель у разных провайдеров — тоже расхождение.
  assert.equal(sameModel({ provider: 'lokal', model: 'qwen36v' }, { provider: 'anthropic', model: 'qwen36v' }), false);
  // Пустые значения не «совпадают» никогда.
  assert.equal(sameModel('', ''), false);
});

test('W23-MG2: состояние сверки и пометка — слова, а не значки без объяснения', () => {
  assert.equal(memberState(MEMBER_BAD), 'warn');
  assert.equal(memberState({ expected: MEMBER_BAD.expected, last_run: MEMBER_BAD.expected, ok: true }), 'ok');
  assert.equal(memberState({ expected: { provider: '', model: '' }, last_run: null }), 'unknown');
  assert.equal(memberState({ expected: MEMBER_BAD.expected, last_run: null }), 'new');
  // Модель сменили позже последнего запуска: сервер говорит ok, но это «ждёт запуска».
  assert.equal(memberState({
    expected: MEMBER_BAD.expected,
    last_run: { provider: 'oblako-a', model: 'dilizhans-ultra' },
    ok: true,
  }), 'pending');
  assert.match(stateMark('pending'), /ждёт запуска/);

  assert.match(stateMark('ok'), /совпадает/);
  assert.match(stateMark('warn'), /не совпадает/);
  assert.match(stateMark('new'), /запусков ещё не было/);
});

test('W23-MG2: стоимость и время — спокойно и по-русски', () => {
  assert.equal(costText(2.45), '≈ $2,45');
  assert.equal(costText(0), '');
  assert.equal(costText('нет'), '');
  assert.equal(costText(120.4), '≈ $120');
  assert.equal(clockText(1793169000).length, 5);
  assert.match(clockText(1793169000), /^\d\d:\d\d$/);
});

test('W23-MG2: причина расхождения человеческим языком', () => {
  const text = problemText(MEMBER_BAD, CATALOG);
  assert.match(text, /Ожидалась Дилижанс домашний, запущено Дилижанс ультра/);
  // Серверная формулировка главнее — она знает профиль и провайдера.
  assert.equal(problemText({ problem: 'ожидалась qwen36v (llamacpp), запущено glm-5.3-flash (ollama-cloud)' }, CATALOG),
    'ожидалась qwen36v (llamacpp), запущено glm-5.3-flash (ollama-cloud)');
});

/* ───────────────────────────────────────────── 2. строка в карточке участника */

test('W23-MG2: строка карточки — ожидаемая модель, последний запуск, пометка', () => {
  let checked = '';
  const line = guardLine(MEMBER_BAD, { catalog: CATALOG, onCheck: (profile) => { checked = profile; } });
  const text = textOf(line);
  assert.match(text, /Модель:/);
  assert.match(text, /Дилижанс домашний/);
  assert.match(text, /последний запуск: Дилижанс ультра/);
  assert.match(text, /≈ \$2,45/);
  assert.match(text, /не совпадает/);
  assert.match(String(line.className), /mg-line--warn/);

  const btn = allByTag(line, 'button').find((b) => /Проверить сейчас/.test(textOf(b)));
  assert.ok(btn, 'кнопка «Проверить сейчас» есть');
  btn.dispatch('click', { type: 'click', stopPropagation() {} });
  assert.equal(checked, 'coder');
});

test('W23-MG2: совпадение показывается спокойно, без кнопки «Проверить» без обработчика', () => {
  const ok = guardLine({
    profile: 'team-ops', expected: { provider: 'lokal', model: 'dilizhans-home' },
    last_run: { provider: 'lokal', model: 'dilizhans-home', at: 1793169000, cost_usd: 0 }, ok: true,
  }, { catalog: CATALOG });
  assert.match(textOf(ok), /совпадает/);
  assert.match(String(ok.className), /mg-line--ok/);
  assert.equal(allByTag(ok, 'button').length, 0);
});

test('W23-MG2: длинное имя модели не обрезается насмерть — оно в тексте целиком', () => {
  const line = guardLine({
    profile: 'surveyor', expected: { provider: 'ollama-cloud', model: 'deepseek-v4.1-flash:1017' },
    last_run: { provider: 'ollama-cloud', model: 'deepseek-v4.1-flash:1017', at: 1793169000 }, ok: true,
  }, {});
  assert.match(textOf(line), /deepseek-v4\.1-flash:1017/);
});

/* ─────────────────────────────────────────────────────── 3. события сторожа */

test('W23-MG2: событие «остановлено» — что было, что стало, сколько сэкономили', () => {
  const text = guardEventText({
    kind: 'stopped',
    expected: { provider: 'lokal', model: 'dilizhans-home' },
    actual: { provider: 'oblako-a', model: 'dilizhans-ultra' },
    cost_usd: 2.45,
  }, CATALOG);
  assert.match(text, /^Остановлено: запуск шёл на Дилижанс ультра/);
  assert.match(text, /вместо Дилижанс домашний/);
  assert.match(text, /исправлено и перезапущено/);
  assert.match(text, /сэкономлено ≈ \$2,45/);

  const fixed = guardEventText({
    kind: 'fixed',
    expected: { provider: 'lokal', model: 'dilizhans-home' },
    actual: { provider: 'oblako-a', model: 'dilizhans-pro' },
  }, CATALOG);
  assert.match(fixed, /^Исправлено до запуска/);
  assert.match(fixed, /модель приведена к ожидаемой/);

  const unknown = guardEventText({ kind: 'unknown' }, CATALOG);
  assert.match(unknown, /контроль не вмешивался/);

  // Серверный текст (§W23.5) показываем КАК ЕСТЬ, ничего не доклеиваем.
  const server = 'Запуск шёл на claude-sonnet-5-5 вместо локальной qwen36v — остановлено и перезапущено на qwen36v';
  assert.equal(guardEventText({ kind: 'stopped', text: server, cost_usd: 2.45 }, CATALOG), server);

  // «Только предупреждать» — расхождение есть, запуск не останавливали.
  assert.match(guardEventText({ kind: 'warned', expected: { model: 'qwen36v' }, actual: { model: 'glm-5.3-flash' } }, CATALOG),
    /запуск не останавливали/);
});

test('W23-MG2: события раскладываются от новых к старым, мусор отбрасывается', () => {
  const events = guardEventsOf({
    model_guard: [
      { at: 100, kind: 'fixed', card_id: 't_1', profile: 'coder' },
      null,
      { at: 300, kind: 'stopped', card_id: 't_2', profile: 'coder', cost_usd: 2.45 },
      { at: 200, kind: 'чушь' },
    ],
  });
  assert.equal(events.length, 3);
  assert.equal(events[0].kind, 'stopped');
  assert.equal(events[1].kind, 'unknown');
  assert.equal(events[2].kind, 'fixed');

  assert.equal(guardEventsOf({ model_guard: [{ at: 1, kind: 'warned' }, { at: 2, kind: 'stopped_by_user' }] }).length, 2);
  assert.equal(guardEventsOf({ model_guard: [{ at: 1, kind: 'warned' }] })[0].kind, 'warned');

  const sum = guardSummary(events);
  assert.equal(sum.stopped, 1);
  assert.equal(sum.fixed, 1);
  assert.equal(sum.unknown, 1);
  assert.equal(Math.round(sum.saved_usd * 100) / 100, 2.45);
});

/* ─────────────────────────────────────── 4. лента на странице запроса */

test('W23-MG2: лента на странице запроса — заголовок, события, итог', () => {
  const feed = buildRequestFeed({
    model_guard: [
      { at: 1793169000, kind: 'stopped', card_id: 't_7b0c55a1', profile: 'coder', cost_usd: 2.45,
        expected: { provider: 'lokal', model: 'dilizhans-home' },
        actual: { provider: 'oblako-a', model: 'dilizhans-ultra' } },
      { at: 1793165000, kind: 'fixed', card_id: 't_8c21ab77', profile: 'coder',
        expected: { provider: 'lokal', model: 'dilizhans-home' },
        actual: { provider: 'oblako-a', model: 'dilizhans-pro' } },
    ],
  }, null, { catalog: CATALOG });
  assert.ok(feed);
  assert.equal(feed.getAttribute('id'), 'model-guard');
  const text = textOf(feed);
  assert.match(text, /Контроль моделей/);
  assert.match(text, /1 остановка/);
  assert.match(text, /1 исправление/);
  assert.match(text, /сэкономлено ≈ \$2,45/);
  assert.equal(allByTag(feed, 'li').length, 2);
  // Первым идёт свежее событие.
  assert.match(textOf(allByTag(feed, 'li')[0]), /Остановлено/);
});

test('W23-MG2: нет событий — нет пустой карточки на странице', () => {
  assert.equal(buildRequestFeed({ model_guard: [] }, null), null);
  assert.equal(buildRequestFeed({}, null), null);
  assert.equal(buildRequestFeed(null, null), null);
});

/* ─────────────────────────────────── 5. кнопки управления работой карточки */

function apiStub(calls, fail) {
  return {
    post: async (url, body) => {
      calls.push({ url, body });
      if (fail) throw fail;
      return { ok: true };
    },
  };
}

test('W23-MG2: «Остановить» отправляет card_stop один раз и блокирует кнопку', async () => {
  const calls = [];
  const node = { id: 't_9f3c2a13', status: 'running', request_id: 't_9f3c2a10' };
  const box = cardControls({ api: apiStub(calls) }, {
    node, requestId: 't_9f3c2a10', taskId: 't_9f3c2a13', confirm: async () => true,
  });
  const stop = allByTag(box, 'button').find((b) => /Остановить/.test(textOf(b)));
  assert.ok(stop, 'кнопка «Остановить» есть у идущей карточки');

  await stop.listeners.click[0]({ type: 'click' });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/requests/t_9f3c2a10/actions');
  assert.deepEqual(calls[0].body, { action: 'card_stop', task_id: 't_9f3c2a13' });
  // Во время запроса кнопка выключена, после ответа снова живая — двойного POST нет.
  assert.equal(stop.hasAttribute('disabled'), false);
});

test('W23-MG2: «Продолжить» у остановленной карточки — одна кнопка, в строке остановки', async () => {
  const calls = [];
  const node = {
    id: 't_9f3c2a13', status: 'paused', request_id: 't_9f3c2a10',
    stopped: { by: 'user', at: 1793169060, model: { provider: 'lokal', model: 'dilizhans-home' } },
  };
  assert.equal(runState(node), 'stopped');

  // В строке «Остановлено вами…» — «Продолжить» со своим действием.
  const line = stoppedLine({ api: apiStub(calls) }, { node, catalog: CATALOG });
  const resume = allByTag(line, 'button').find((b) => /Продолжить/.test(textOf(b)));
  assert.ok(resume, 'кнопка «Продолжить» есть у остановленной');
  await resume.listeners.click[0]({ type: 'click' });
  assert.deepEqual(calls[0].body, { action: 'card_resume', task_id: 't_9f3c2a13' });

  // Блок кнопок работы второй такой же не рисует — «Остановить» у не идущей нет.
  const box = cardControls({ api: apiStub([]) }, { node, requestId: 't_9f3c2a10' });
  assert.equal(allByTag(box, 'button').filter((b) => /Продолжить/.test(textOf(b))).length, 0);
  assert.equal(allByTag(box, 'button').filter((b) => /Остановить/.test(textOf(b))).length, 0);
});

test('W23-ST: метка остановки от сервера (плоские model/provider) даёт понятное название', () => {
  // Сервер отдаёт `{by, at, model, provider, cost_usd}` — строками (docs/API.md §W23.8.1).
  const node = {
    id: 't_9f3c2a13', status: 'blocked',
    stopped: { by: 'user', at: 1793169060, model: 'dilizhans-home', provider: 'lokal', cost_usd: 0.37 },
  };
  assert.equal(runState(node), 'stopped');
  const line = stoppedText(node, CATALOG);
  assert.match(line, /Остановлено вами/);
  assert.match(line, /Дилижанс домашний/, 'название модели из каталога, а не её id: ' + line);
  assert.ok(!line.includes('dilizhans-home'), 'технический id наружу не выводится: ' + line);
  assert.match(line, /0,37/, 'стоимость на момент остановки: ' + line);
  // старая форма (объект) тоже понимается — данные демо/старых записей
  const legacy = stoppedText({ id: 't_x', status: 'blocked',
    stopped: { by: 'user', at: 1793169060, model: { provider: 'lokal', model: 'dilizhans-home' } } },
  CATALOG);
  assert.match(legacy, /Дилижанс домашний/);
});

test('W23-MG2: «Сменить модель…» открывает выбор и отправляет card_model', async () => {
  const calls = [];
  const api = apiStub(calls);
  const ctx = { api };
  const promise = openCardModelDialog(ctx, {
    requestId: 't_9f3c2a10', taskId: 't_9f3c2a13', title: 'Запросить котировки',
    current: { provider: 'lokal', model: 'dilizhans-home' }, catalog: CATALOG,
  });
  const overlay = globalThis.document.body.childNodes.find((n) => n.nodeType === 1);
  assert.ok(overlay, 'диалог появился');
  const select = allByTag(overlay, 'select')[0];
  assert.ok(select, 'есть выбор модели');
  // Первая строка — «по умолчанию — модель профиля».
  const first = allByTag(select, 'option')[0];
  assert.equal(textOf(first).trim(), MODEL_GUARD_RU.defaultModel);
  // В списке есть модели каталога, текущая выбрана.
  assert.equal(select.value, 'lokal::dilizhans-home');

  select.value = 'oblako-a::dilizhans-pro';
  const submit = allByTag(overlay, 'button').find((b) => /Сменить и продолжить/.test(textOf(b)));
  assert.ok(submit, 'кнопка «Сменить и продолжить»');
  await submit.listeners.click[0]({ type: 'click' });
  await promise;
  assert.deepEqual(calls[0].body, {
    action: 'card_model', task_id: 't_9f3c2a13', provider: 'oblako-a', model: 'dilizhans-pro',
  });
});

test('W23-MG2: «по умолчанию — модель профиля» = model: "none" (как в API §W23.8)', async () => {
  const calls = [];
  await applyCardModel({ api: apiStub(calls) }, { requestId: 't_9f3c2a10', taskId: 't_9f3c2a13', provider: '', model: '' });
  assert.deepEqual(calls[0].body, { action: 'card_model', task_id: 't_9f3c2a13', provider: '', model: 'none' });
});

test('W23-MG2: ошибка действия — человеческий текст, без кодов', () => {
  assert.match(cardErrorText({ status: 409, message: 'conflict' }), /обновите страницу/);
  assert.match(cardErrorText({ status: 404 }), /не найдена/);
  assert.equal(cardErrorText({ status: 500, message: 'нет связи' }), 'нет связи');
});

test('W23-MG2: без номера запроса кнопки не показываются (нет мёртвых кнопок)', () => {
  const box = cardControls({ api: apiStub([]) }, {
    node: { id: 't_1', status: 'running' }, requestId: '', taskId: 't_1',
  });
  assert.equal(box.hidden, true);
});

test('W23-MG2: у завершённой карточки нет «Остановить»', () => {
  const box = cardControls({ api: apiStub([]) }, {
    node: { id: 't_1', status: 'done', request_id: 't_2', finished_at: 1793169000 },
    requestId: 't_2', taskId: 't_1',
  });
  assert.equal(allByTag(box, 'button').filter((b) => /Остановить/.test(textOf(b))).length, 0);
  assert.equal(canStop({ id: 't_1', status: 'done' }), false);
  assert.equal(canResume({ id: 't_1', status: 'paused' }), true);
});

test('W23-MG2: остановка в форме сервера (blocked + метка) тоже даёт «Продолжить»', () => {
  // §W23.8: card_stop паркует карточку в blocked и ставит метку stopped.
  const parked = { id: 't_9f3c2a13', status: 'blocked', stopped_by: 'user', stopped_at: 1793169060 };
  assert.equal(runState(parked), 'stopped');
  assert.equal(canResume(parked), true);
  assert.match(stoppedText(parked, CATALOG), /^Остановлено вами/);

  // Обычная blocked (ждёт зависимости) — это не остановка: «Продолжить» не предлагаем.
  const blocked = { id: 't_1', status: 'blocked' };
  assert.equal(runState(blocked), 'idle');
  assert.equal(canResume(blocked), false);
});

/* ─────────────────────────────────────── 6. строка «Остановлено вами…» */

test('W23-MG2: строка остановки называет время и модель', () => {
  const node = {
    id: 't_9f3c2a13', status: 'paused', request_id: 't_9f3c2a10',
    stopped: {
      by: 'user', at: 1793169060,
      model: { provider: 'lokal', model: 'dilizhans-home' }, cost_usd: 0.41,
    },
  };
  const text = stoppedText(node, CATALOG);
  assert.match(text, /^Остановлено вами в \d\d:\d\d/);
  assert.match(text, /модель: Дилижанс домашний/);
  assert.match(text, /≈ \$0,41/);

  const guardStop = stoppedText({ status: 'paused', stopped: { by: 'guard', at: 1793169060 } });
  assert.match(guardStop, /^Остановлено контролем моделей/);

  const line = stoppedLine({ api: apiStub([]) }, { node, catalog: CATALOG });
  assert.ok(line, 'строка есть у остановленной карточки');
  assert.ok(allByTag(line, 'button').some((b) => /Продолжить/.test(textOf(b))));
  assert.equal(stoppedLine({}, { node: { status: 'running' } }, null), null);
});

/* ─────────────────────────────────────────────── 7. режим комнаты */

test('W23-MG2: режим контроля — «строго» по умолчанию и переключение', () => {
  assert.equal(guardModeOf({}), 'enforce');
  assert.equal(guardModeOf({ model_guard: 'warn' }), 'warn');
  assert.equal(guardModeOf({ model_guard: 'чепуха' }), 'enforce');
  assert.equal(guardModeLabel('enforce'), 'строго');
  assert.equal(guardModeLabel('warn'), 'только предупреждать');
  assert.deepEqual(GUARD_MODES, ['enforce', 'warn']);

  let changes = 0;
  const field = guardModeField({ room: { model_guard: 'warn' }, onChange: () => { changes += 1; } });
  assert.equal(field.value(), 'warn');
  field.set('enforce');
  assert.equal(field.value(), 'enforce');
  assert.equal(field.el.getAttribute('data-model-guard'), 'enforce');
  assert.match(textOf(field.el), /строго/);
  assert.match(textOf(field.el), /только предупреждать/);
  assert.match(textOf(field.el), /не останавливать/);
  assert.equal(changes, 0);
});

test('W23-MG2: модель из каталога не дублируется в диалоге', () => {
  assert.equal(modelInCatalog(CATALOG, { provider: 'lokal', model: 'dilizhans-home' }), true);
  assert.equal(modelInCatalog(CATALOG, { provider: 'lokal', model: 'чужая-модель' }), false);
});

test('W23-MG2: настройки модели в один вид — объект и строка', () => {
  assert.deepEqual(settingsOf('qwen36v'), { provider: '', model: 'qwen36v' });
  assert.deepEqual(settingsOf('llamacpp:qwen36v'), { provider: 'llamacpp', model: 'qwen36v' });
  assert.deepEqual(settingsOf({ provider: 'Lokal', model: 'x' }), { provider: 'lokal', model: 'x' });
  assert.deepEqual(settingsOf(null), { provider: '', model: '' });
});

/* fake-dom: у наших элементов не должно быть innerHTML-сборки */
test('W23-MG2: разметка собирается узлами, без innerHTML', () => {
  const feed = buildRequestFeed({
    model_guard: [{ at: 1793169000, kind: 'fixed', expected: { model: 'qwen36v' }, actual: { model: 'glm-5.3-flash' } }],
  }, null, {});
  assert.ok(feed instanceof FakeElement);
  assert.throws(() => { feed.innerHTML = '<b>x</b>'; });
});
