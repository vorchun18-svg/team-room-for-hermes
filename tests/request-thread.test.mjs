/**
 * request-thread.test.mjs — блок «Диалог по запросу» без браузера.
 *
 * Проверяем то, что видит Михаил: лента (кто справа, кто с аватаром, события,
 * даты), пустое состояние, кнопки по статусу, оптимистичную отправку, живой
 * черновик при ошибке сети и то, что перерисовка страницы (опрос) не сбрасывает
 * набранное, фокус и прокрутку.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom, FakeElement } from './fake-dom.mjs';

installFakeDom(); // h() строит узлы только при наличии document
// В мини-DOM нет focus(): для проверки возврата фокуса хватает отметки.
FakeElement.prototype.focus = function focus() { this._focused = true; };

const HERE = dirname(fileURLToPath(import.meta.url));
const {
  mountThread, secondAction, canReviseWithMessage, ruText, roleLabel, authorTitle, mergeThreads,
  EMPTY_TEXT, SEND_HINT,
} = await import('../web/js/views/request-thread.js');

/* ─────────────────────────────────────────────────────────── помощники */

const now = Math.floor(Date.now() / 1000);

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
const textOf = (el) => String(el ? el.textContent : '');
const click = (el) => el.dispatch('click', { type: 'click', target: el });
const tick = () => new Promise((r) => setTimeout(r, 0));

function makeCtx(apiPost, extra = {}) {
  const calls = [];
  const toasts = [];
  const ctx = {
    room: { id: 'cafe', members: [{ profile: 'biz-ops', note: 'Операции: поставки' }] },
    toast: (msg, kind) => toasts.push({ msg, kind }),
    api: {
      post: async (path, body) => {
        calls.push({ path, body });
        return apiPost ? apiPost(path, body) : {};
      },
    },
    ...extra,
  };
  return { ctx, calls, toasts };
}

function mount(detail, opts = {}) {
  const { ctx, calls, toasts } = makeCtx(opts.apiPost, opts.extra);
  const host = new FakeElement('div');
  const sent = [];
  const ctl = mountThread(host, ctx, {
    roomId: 'cafe', rid: 't_root', detail, onSent: () => { sent.push(1); },
  });
  return { host, ctx, ctl, calls, toasts, sent };
}

const THREAD = [
  {
    id: 'c1', at: now - 86400, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
    text: 'Добавьте срок подачи в Роспатент', kind: 'message', task_id: null,
  },
  {
    id: 'c2', at: now - 86000, author_kind: 'system', author: 'system', author_label: 'Система',
    text: 'Версия 2 готова', kind: 'event', task_id: null,
  },
  {
    id: 'c3', at: now - 60, author_kind: 'agent', author: 'biz-ops', author_label: 'Глеб · директор',
    text: 'Принял, передал юристу', kind: 'comment', task_id: 't_child',
  },
];

/* ─────────────────────────────────────────────────────────── лента */

test('лента: Михаил справа и выделен, команда с аватаром и ролью, события строкой, даты', () => {
  const { host } = mount({ id: 't_root', status: 'working', thread: THREAD });

  assert.equal(textOf(byClass(host, 'thr__title')[0]).trim(), 'Диалог по запросу');

  const mine = byClass(host, 'thr-msg--mine');
  assert.equal(mine.length, 1, 'сообщение Михаила одно');
  assert.match(textOf(mine[0]), /Роспатент/);
  assert.equal(byClass(mine[0], 'avatar').length, 0, 'у своих сообщений аватар не нужен');

  const agent = byClass(host, 'thr-msg').filter((el) => !el.classList.contains('thr-msg--mine'));
  assert.equal(agent.length, 1);
  assert.equal(byClass(agent[0], 'avatar').length, 1, 'у команды есть аватар');
  assert.match(textOf(agent[0]), /Принял, передал юристу/);
  assert.match(textOf(agent[0]), /руководитель комнаты/, 'роль по-русски');
  assert.doesNotMatch(textOf(agent[0]), /директор/i, 'старое слово не показываем');

  const events = byClass(host, 'thr-ev');
  assert.equal(events.length, 1, 'системное событие — отдельной строкой');
  assert.match(textOf(events[0]), /Версия 2 готова/);

  const days = byClass(host, 'thr__day');
  assert.equal(days.length, 2, 'два дня — два разделителя');
  assert.equal(textOf(days[0]).trim(), 'Вчера');
  assert.equal(textOf(days[1]).trim(), 'Сегодня');
});

test('пустая лента объясняет, зачем писать', () => {
  const { host } = mount({ id: 't_root', status: 'working' });
  const empty = byClass(host, 'thr__empty');
  assert.equal(empty.length, 1);
  assert.equal(textOf(byClass(host, 'thr__empty-text')[0]).trim(), EMPTY_TEXT);
  assert.equal(byClass(host, 'thr-msg').length, 0);

  const hint = byClass(host, 'thr__hint')[0];
  assert.equal(textOf(hint).trim(), SEND_HINT);

  const ta = host.find('textarea');
  assert.ok(ta, 'поле ввода есть всегда');
  assert.equal(ta.getAttribute('data-mic'), null, 'микрофон ставится сам, запрета нет');
  assert.match(String(ta.getAttribute('class')), /thr__input/);
});

test('кнопки по статусу: в работе одна, готово — переключатель доработки, ошибка — повтор', () => {
  const working = mount({ id: 't_root', status: 'working', thread: [] });
  assert.equal(byClass(working.host, 'thr__second').length, 0, 'в работе второй кнопки нет');
  assert.equal(textOf(byClass(working.host, 'thr__send')[0]).trim(), 'Отправить руководителю');
  assert.equal(byClass(working.host, 'thr__urgent').length, 1, 'галочка «Срочно» доступна во время работы');
  assert.equal(byClass(working.host, 'thr__revise').length, 0, 'пока свода нет — дорабатывать нечего');

  const done = mount({ id: 't_root', status: 'done', thread: [] });
  // W17-S §1: второй кнопки «Отправить на доработку» больше нет.
  assert.equal(byClass(done.host, 'thr__second').length, 0, 'второй кнопки доработки в ленте нет');
  const reviseBox = byClass(done.host, 'thr__revise')[0];
  assert.ok(reviseBox, 'у готового свода есть переключатель «и доработать свод»');
  assert.match(textOf(reviseBox), /и доработать свод по этому сообщению/);
  assert.equal(byClass(done.host, 'thr__urgent').length, 0, 'после ответа пересматривать нечего');

  const failed = mount({ id: 't_root', status: 'failed', thread: [] });
  assert.equal(textOf(byClass(failed.host, 'thr__second')[0]).trim(), 'Повторить с уточнением');

  assert.equal(secondAction('working'), null);
  assert.equal(secondAction('queued'), null);
  assert.equal(secondAction('done'), null, 'доработка живёт только в блоке «Свод команды»');
  assert.equal(secondAction('failed').key, 'retry');
  assert.equal(canReviseWithMessage('done'), true);
  assert.equal(canReviseWithMessage('working'), false);
});

/* ─────────────────────────────────────────────────────────── отправка */

test('отправка: сообщение появляется сразу, потом лента берётся из ответа', async () => {
  let release;
  const pending = new Promise((r) => { release = r; });
  const { host, calls, sent } = mount(
    { id: 't_root', status: 'working', thread: [] },
    { apiPost: () => pending },
  );

  const ta = host.find('textarea');
  ta.value = 'Уточняю: срок — до 30 ноября';
  ta.dispatch('input', { type: 'input', target: ta });
  click(byClass(host, 'thr__send')[0]);
  await tick();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/requests/t_root/messages');
  assert.equal(calls[0].body.text, 'Уточняю: срок — до 30 ноября');
  assert.deepEqual(calls[0].body.attach, []);
  assert.equal(calls[0].body.urgent, undefined);

  const mine = byClass(host, 'thr-msg--mine');
  assert.equal(mine.length, 1, 'сообщение видно до ответа сервера');
  assert.match(textOf(mine[0]), /до 30 ноября/);
  assert.equal(ta.value, '', 'поле очищено');
  assert.equal(sent.length, 0, 'обновление страницы — только после ответа');

  release({
    message: {
      id: 'c9', at: now, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
      text: 'Уточняю: срок — до 30 ноября', kind: 'message', task_id: null,
    },
    thread: [
      ...THREAD,
      {
        id: 'c9', at: now, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
        text: 'Уточняю: срок — до 30 ноября', kind: 'message', task_id: null,
      },
    ],
  });
  await tick();

  const mineAfter = byClass(host, 'thr-msg--mine');
  assert.equal(mineAfter.length, 2, 'Михаил в ленте: старое сообщение и новое');
  const ids = mineAfter.map((el) => el.getAttribute('data-id'));
  assert.ok(ids.includes('c9'), 'у нового сообщения серверный id');
  assert.ok(!ids.some((id) => String(id).startsWith('local:')), 'оптимистичной копии не осталось');
  assert.equal(sent.length, 1, 'onSent вызван — страница обновится');
});

/**
 * Баг волны 8: после 201 `{message, thread, routed_to, merged}` кнопка «живого»
 * блока оставалась «Отправляю…» и недоступной. Причина — порядок: `onSent`
 * перерисовывает страницу и монтирует диалог заново, пока `st.sending` ещё true,
 * а `finally` обновлял кнопки уже отсоединённого блока.
 */
const OK_201 = (text) => ({
  message: {
    id: 'c9', at: now, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
    text, task_id: null, kind: 'message',
  },
  thread: [
    ...THREAD,
    {
      id: 'c9', at: now, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
      text, task_id: null, kind: 'message',
    },
    {
      id: 'c10', at: now, author_kind: 'agent', author: 'biz-ops',
      author_label: 'Глеб · руководитель комнаты', text: 'Принял: учту на шаге 2',
      task_id: 't_child', kind: 'comment',
    },
  ],
  routed_to: 'biz-ops', merged: false,
});

test('после 201 {message, thread, routed_to, merged} живая кнопка снова «Отправить руководителю»', async () => {
  let hostB = null;
  const { ctx } = makeCtx((path, body) => OK_201(body.text));
  const hostA = new FakeElement('div');
  mountThread(hostA, ctx, {
    roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: THREAD },
    // Так ведёт себя настоящая страница: refresh() перерисовывает всё и монтирует
    // диалог заново с тем же ctx.
    onSent: async () => {
      hostB = new FakeElement('div');
      mountThread(hostB, ctx, {
        roomId: 'cafe', rid: 't_root',
        detail: { id: 't_root', status: 'working', thread: OK_201('Ну и?').thread },
      });
    },
  });

  const ta = hostA.find('textarea');
  ta.value = 'Уточняю: срок — до 30 ноября';
  ta.dispatch('input', { type: 'input', target: ta });
  click(byClass(hostA, 'thr__send')[0]);
  await tick();
  await tick();
  await tick();

  assert.ok(hostB, 'страница перерисовалась и диалог смонтирован заново');
  const send = byClass(hostB, 'thr__send')[0];
  assert.equal(textOf(send).trim(), 'Отправить руководителю', 'не «Отправляю…»');
  assert.equal(send.disabled, false, 'кнопка снова доступна');
  assert.equal(send.getAttribute('aria-busy'), 'false');
  assert.equal(byClass(hostB, 'thr-msg--pending').length, 0, 'оптимистичной «отправляю» не осталось');
  assert.ok(
    byClass(hostB, 'thr-msg--mine').some((el) => el.getAttribute('data-id') === 'c9'),
    'сообщение в ленте с серверным id',
  );
});

test('ответ без thread: сообщение из ленты не остаётся «отправляется»', async () => {
  const { host } = mount(
    { id: 't_root', status: 'working', thread: [] },
    { apiPost: (path, body) => ({ message: { id: 'c11', at: now, author: 'mikhail',
      author_kind: 'mikhail', author_label: 'Михаил', text: body.text, kind: 'message' }, routed_to: 'biz-ops' }) },
  );
  const ta = host.find('textarea');
  ta.value = 'Короткое уточнение';
  ta.dispatch('input', { type: 'input', target: ta });
  click(byClass(host, 'thr__send')[0]);
  await tick();
  await tick();

  assert.equal(byClass(host, 'thr-msg--pending').length, 0, 'pending снят');
  assert.equal(byClass(host, 'thr-msg--mine')[0].getAttribute('data-id'), 'c11', 'id с сервера');
  assert.equal(textOf(byClass(host, 'thr__send')[0]).trim(), 'Отправить руководителю');
});

test('ошибка сети: человеческий текст, черновик остаётся в поле', async () => {
  const { host, toasts } = mount(
    { id: 't_root', status: 'working', thread: [] },
    { apiPost: () => Promise.reject({ code: 'network' }) },
  );

  const ta = host.find('textarea');
  ta.value = 'Важное уточнение';
  ta.dispatch('input', { type: 'input', target: ta });
  click(byClass(host, 'thr__send')[0]);
  await tick();
  await tick();

  assert.equal(ta.value, 'Важное уточнение', 'текст не потерян');
  const err = byClass(host, 'thr__err')[0];
  assert.ok(err, 'ошибка показана под полем');
  assert.equal(err.classList.contains('hidden'), false);
  assert.match(textOf(err), /Текст остался в поле/);
  assert.match(textOf(err), /Нет связи с приложением/);
  assert.doesNotMatch(textOf(err), /network|undefined/i, 'без технических слов');
  assert.equal(byClass(host, 'thr-msg--mine').length, 0, 'неотправленное сообщение из ленты убрано');
  assert.equal(toasts.length, 0, 'ошибка — строкой под полем, а не тостом');
});

test('готовый запрос: переключатель «и доработать свод» шлёт сообщение и revise', async () => {
  const { host, calls, sent } = mount({ id: 't_root', status: 'done', thread: [] });

  const ta = host.find('textarea');
  ta.value = 'Добавь срок подачи и MOQ';
  ta.dispatch('input', { type: 'input', target: ta });
  const box = byClass(host, 'thr__revise')[0];
  const input = collect(box, (el) => String(el.tagName).toUpperCase() === 'INPUT')[0];
  input.checked = true;
  input.dispatch('change', { type: 'change', target: input });

  click(byClass(host, 'thr__send')[0]);
  await tick();
  await tick();

  assert.equal(calls[0].path, '/api/requests/t_root/messages', 'сначала обычное сообщение');
  assert.equal(calls[0].body.text, 'Добавь срок подачи и MOQ');
  assert.equal(calls[1].path, '/api/requests/t_root/actions', 'потом доработка тем же текстом');
  assert.equal(calls[1].body.action, 'revise');
  assert.equal(calls[1].body.text, 'Добавь срок подачи и MOQ');
  assert.equal(sent.length, 1);
});

test('готовый запрос: без переключателя доработка не запускается', async () => {
  const { host, calls } = mount({ id: 't_root', status: 'done', thread: [] });
  const ta = host.find('textarea');
  ta.value = 'Просто уточнение';
  ta.dispatch('input', { type: 'input', target: ta });
  click(byClass(host, 'thr__send')[0]);
  await tick();
  assert.equal(calls.length, 1, 'только сообщение — руководитель сам решит, что делать');
  assert.equal(calls[0].path, '/api/requests/t_root/messages');
});

test('Ctrl+Enter отправляет, пустой текст — нет', async () => {
  const { host, calls, toasts } = mount({ id: 't_root', status: 'working', thread: [] });
  const ta = host.find('textarea');

  ta.dispatch('keydown', { type: 'keydown', key: 'Enter', ctrlKey: true, target: ta });
  await tick();
  assert.equal(calls.length, 0, 'пустое сообщение не уходит');
  assert.ok(toasts.some((t) => /Напишите сообщение/.test(t.msg)));

  ta.value = 'Ещё вопрос';
  ta.dispatch('keydown', { type: 'keydown', key: 'Enter', ctrlKey: true, target: ta });
  await tick();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.text, 'Ещё вопрос');
});

/* ─────────────────────────────────────── перерисовка страницы (опрос) */

test('перерисовка не сбрасывает набранное, фокус и прокрутку', async () => {
  const { ctx } = makeCtx();
  const hostA = new FakeElement('div');
  mountThread(hostA, ctx, { roomId: 'cafe', rid: 't_root', detail: { id: 't_root', status: 'working', thread: [] } });

  const taA = hostA.find('textarea');
  taA.value = 'Черновик, который нельзя потерять';
  taA.dispatch('input', { type: 'input', target: taA });
  taA.dispatch('focus', { type: 'focus', target: taA });

  const feedA = byClass(hostA, 'thr__feed')[0];
  feedA.scrollHeight = 800;
  feedA.clientHeight = 200;
  feedA.scrollTop = 40;
  feedA.dispatch('scroll', { type: 'scroll', target: feedA });

  // Страница перерисовалась: новый контейнер, тот же ctx (опрос 2/10 с).
  const hostB = new FakeElement('div');
  mountThread(hostB, ctx, {
    roomId: 'cafe', rid: 't_root',
    detail: { id: 't_root', status: 'working', thread: THREAD },
  });
  await tick();

  const taB = hostB.find('textarea');
  assert.equal(taB.value, 'Черновик, который нельзя потерять', 'набранное сохранилось');
  assert.equal(taB._focused, true, 'фокус вернулся в поле');

  const feedB = byClass(hostB, 'thr__feed')[0];
  assert.equal(feedB.scrollTop, 40, 'человек остался на том же месте ленты');
  assert.equal(byClass(hostB, 'thr-msg').length, 2, 'лента обновилась из новых данных');
});

/* ─────────────────────────────────────────────────────── подписи и меню */

test('старое название главы комнаты заменяется понятным', () => {
  assert.equal(ruText('директор'), 'руководитель комнаты');
  assert.equal(ruText('Директор комнаты'), 'Руководитель комнаты');
  assert.equal(ruText('передал директору'), 'передал руководителю комнаты');
  assert.equal(ruText('Директором согласовано'), 'Руководителем комнаты согласовано');
  assert.equal(ruText('Глеб · директор'), 'Глеб · руководитель комнаты');
  assert.equal(ruText('руководитель'), 'руководитель');

  assert.equal(roleLabel({ author: 'biz-ops' }), 'операции');
  assert.equal(authorTitle({ author: 'biz-ops', author_label: 'Глеб · директор' }), 'Глеб · руководитель комнаты');
});

test('mergeThreads: склейка без дублей, порядок по времени', () => {
  const merged = mergeThreads(
    [{ id: 'b', at: 20, text: 'второе' }, { id: 'a', at: 10, text: 'первое' }],
    [{ id: 'b', at: 20, text: 'второе (сервер)' }, { id: 'c', at: 30, text: 'третье' }],
  );
  assert.deepEqual(merged.map((m) => m.id), ['a', 'b', 'c']);
  assert.equal(merged[1].text, 'второе (сервер)', 'серверная запись побеждает');
});

test('меню «Действия»: сообщение руководителю — первым, недоступное объясняет «когда»', () => {
  const src = readFileSync(join(HERE, '..', 'web', 'js', 'views', 'request-header.js'), 'utf8');
  const add = src.indexOf('Добавить сообщение руководителю');
  const clone = src.indexOf("label: 'Клонировать'");
  assert.ok(add > 0, 'пункт есть');
  assert.ok(clone > add, 'пункт стоит первым в меню');
  assert.match(src, /Станет доступно, когда /);
  assert.match(src, /R\.focusThread/);
  assert.doesNotMatch(src, /директор/i);
});
