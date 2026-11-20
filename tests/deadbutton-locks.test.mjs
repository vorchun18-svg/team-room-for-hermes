/**
 * deadbutton-locks.test.mjs — W28-D7 (дефект 7): «кнопки умирают».
 *
 * Что было: кнопка пункта блокировалась на время запроса (`btn.disabled = true`),
 * а снимался замок только в ветке ошибки или «когда перерисовка случится». Если
 * ответ не приходил вовсе (сервер завис, связь оборвалась, ноутбук уснул) или
 * данные не менялись и страница не перерисовывалась, кнопка оставалась мёртвой
 * до перезагрузки — «помогает только выйти и зайти». То же с ключом «в полёте»
 * в сторе: `state.waiting` держал ключ, и все кнопки пункта молча не отвечали.
 *
 * Проверяется три правила (они же — защита от всего класса):
 *   1. `locked()` снимает замок при ЛЮБОМ исходе: успех, ошибка, таймаут;
 *   2. `withTimeout()` заканчивает ожидание ошибкой `timeout` с понятным текстом;
 *   3. `dispatch()` освобождает ключ `waiting` и после зависшего запроса, а
 *      повторное нажатие получает внятный отказ (`busy`), а не тишину.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { locked, withTimeout, lockTimeout, ACTION_TIMEOUT_MS } from '../web/js/store.js';
import { createRequestStore, resetRequestStores } from '../web/js/views/request-store.js';

/** Кнопка «как в браузере»: disabled + aria-busy, которые ставит `locked`. */
function fakeButton() {
  return {
    disabled: false,
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    removeAttribute(name) { delete this.attrs[name]; },
  };
}

/* ─────────────────────────────────────────────────────── 1. замок кнопки ── */

test('locked: замок снимается после успеха', async () => {
  const btn = fakeButton();
  const out = await locked(btn, async () => 'ok');
  assert.equal(out, 'ok');
  assert.equal(btn.disabled, false, 'кнопка осталась выключенной после успеха');
  assert.equal(btn.attrs['aria-busy'], undefined, 'остался aria-busy');
});

test('locked: замок снимается после ошибки', async () => {
  const btn = fakeButton();
  await assert.rejects(locked(btn, async () => { throw new Error('нет связи'); }), /нет связи/);
  assert.equal(btn.disabled, false, 'кнопка осталась выключенной после ошибки');
  assert.equal(btn.attrs['aria-busy'], undefined);
});

test('locked: замок снимается после таймаута (запрос не отвечает вовсе)', async () => {
  const btn = fakeButton();
  const hang = new Promise(() => {});          // ответа не будет никогда
  await assert.rejects(
    locked(btn, () => hang, { ms: 40, label: 'Снятие вопроса' }),
    (err) => err.code === 'timeout' && /Снятие вопроса/.test(err.message),
  );
  assert.equal(btn.disabled, false, 'зависший запрос оставил кнопку выключенной');
  assert.equal(btn.attrs['aria-busy'], undefined, 'зависший запрос оставил aria-busy');
});

test('locked: пока идёт работа, кнопка занята и второй раз не нажимается', async () => {
  const btn = fakeButton();
  let runs = 0;
  let release;
  const first = locked(btn, () => new Promise((res) => { runs += 1; release = res; }));
  assert.equal(btn.disabled, true, 'кнопка не заблокирована на время запроса');
  assert.equal(btn.attrs['aria-busy'], 'true', 'нет aria-busy на время работы');
  const second = await locked(btn, async () => { runs += 1; });
  assert.equal(second, undefined, 'второе нажатие не должно запускать работу');
  assert.equal(runs, 1, 'работа запустилась дважды');
  release('готово');
  await first;
  assert.equal(btn.disabled, false);
});

test('locked: без кнопки действие выполняется (кнопки может не быть)', async () => {
  assert.equal(await locked(null, async () => 7), 7);
});

/* ─────────────────────────────────────────────────────── 2. предел ожидания ── */

test('withTimeout: успешный ответ проходит как есть', async () => {
  assert.equal(await withTimeout(Promise.resolve('да'), 50), 'да');
});

test('withTimeout: ошибка запроса не подменяется таймаутом', async () => {
  await assert.rejects(withTimeout(Promise.reject(new Error('500')), 50), /500/);
});

test('withTimeout: молчащий запрос даёт понятную ошибку timeout', async () => {
  const err = await withTimeout(new Promise(() => {}), 30, 'Действие по запросу')
    .then(() => null, (e) => e);
  assert.ok(err, 'таймаут не сработал');
  assert.equal(err.code, 'timeout');
  assert.equal(err.timeout, true);
  assert.match(err.message, /не ответило за/);
  assert.match(err.message, /Кнопка снова доступна/, 'нет подсказки, что делать');
});

test('lockTimeout: по умолчанию 20 с, в проверке — короче', () => {
  const saved = globalThis.__teamroomLockTimeoutMs;
  try {
    delete globalThis.__teamroomLockTimeoutMs;
    assert.equal(lockTimeout(), ACTION_TIMEOUT_MS);
    assert.equal(ACTION_TIMEOUT_MS, 20000);
    globalThis.__teamroomLockTimeoutMs = 2500;
    assert.equal(lockTimeout(), 2500);
  } finally {
    if (saved === undefined) delete globalThis.__teamroomLockTimeoutMs;
    else globalThis.__teamroomLockTimeoutMs = saved;
  }
});

/* ────────────────────────────────────────── 3. ключ «в полёте» в сторе ── */

const DETAIL = {
  id: 't_test',
  status: 'needs_you',
  approvals: [],
  open_questions: [{ id: 'q_1', text: 'Вопрос', answer_state: 'open' }],
  decisions: [],
  next_steps: [],
};

function storeWith(api) {
  resetRequestStores();
  // В проверке ждём 80 мс, а не 20 с: иначе тест «зависший запрос» шёл бы вечность.
  globalThis.__teamroomLockTimeoutMs = 80;
  return createRequestStore({ rid: 't_test', roomId: 'cafe', detail: DETAIL, api });
}

test('dispatch: ключ waiting освобождается после успеха', async () => {
  const store = storeWith({ get: async () => DETAIL, post: async () => ({ ok: true }) });
  assert.deepEqual(store.get().inflight, []);
  await store.targetAction('questions', 'q_1', 'dismiss');
  assert.deepEqual(store.get().inflight, [], 'ключ остался в полёте после успеха');
});

test('dispatch: ключ waiting освобождается после ошибки', async () => {
  const store = storeWith({ get: async () => DETAIL, post: async () => { throw new Error('400'); } });
  await assert.rejects(store.targetAction('questions', 'q_1', 'dismiss'), /400/);
  assert.deepEqual(store.get().inflight, [], 'ключ остался в полёте после ошибки');
});

test('dispatch: зависший запрос освобождает ключ по пределу ожидания', async () => {
  const store = storeWith({ get: async () => DETAIL, post: () => new Promise(() => {}) });
  const job = store.targetAction('questions', 'q_1', 'dismiss');
  // Пока ждём — ключ занят: второй клик получает понятный отказ, а не тишину.
  await assert.rejects(store.targetAction('questions', 'q_1', 'dismiss'), (err) => {
    assert.equal(err.code, 'busy');
    assert.match(err.message, /уже выполняется/);
    assert.ok(err.hint, 'у отказа нет подсказки, что делать');
    return true;
  });
  const err = await job.then(() => null, (e) => e);
  assert.ok(err && err.code === 'timeout', 'зависший запрос не прерван по таймауту');
  assert.deepEqual(store.get().inflight, [], 'после таймаута ключ остался в полёте');
  // Кнопка пункта снова работает: то же действие проходит.
  const store2 = store;
  await store2.targetAction('questions', 'q_1', 'dismiss').catch(() => {});
  assert.deepEqual(store2.get().inflight, []);
});

test('dispatch: после таймаута тот же пункт снова принимает действие', async () => {
  let calls = 0;
  const store = storeWith({
    get: async () => DETAIL,
    post: () => {
      calls += 1;
      return calls === 1 ? new Promise(() => {}) : Promise.resolve({ ok: true });
    },
  });
  await store.targetAction('questions', 'q_1', 'dismiss').catch(() => {});
  assert.deepEqual(store.get().inflight, []);
  const res = await store.targetAction('questions', 'q_1', 'restore');
  assert.equal(res.ok, true, 'кнопка пункта осталась мёртвой после таймаута');
  assert.equal(calls, 2);
});
