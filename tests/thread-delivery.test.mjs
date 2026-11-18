/**
 * thread-delivery.test.mjs — W20-FA §1.1–§1.2: подпись доставки у сообщения
 * человека и плашка «ждёт ваших ответов: N → справа» у сообщения руководителя.
 *
 * Что проверяем:
 *  * каждое сообщение пользователя показывает `delivery` с сервера («Учтено в
 *    версии 4», «Ждёт обновления свода — в 14:05», «Не учтено — …»), свой цвет по
 *    состоянию, и подпись обновляется вместе с данными (`update`);
 *  * сообщение руководителя с `asks` получает кнопку «ждёт ваших ответов: N →
 *    справа»; клик прокручивает к блоку «Нужен ваш ответ»;
 *  * служебные события и сообщения без `delivery`/`asks` ничего лишнего не рисуют;
 *  * монтирование остаётся ОДНО (W20-C1): `update(detail)` не пересобирает ленту
 *    без нужды и не теряет набранный черновик.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installMiniDom, resetMiniDom, DOC, byClass, byData, textOf, tick } from './mini-dom.mjs';

installMiniDom();

const { mountThread: mountThreadRaw } = await import('../web/js/views/request-thread.js');

/**
 * У-1 (волна 21): диалог по умолчанию свёрнут — тесты открывают его так же,
 * как человек: заголовком общего компонента (иначе ленты и поля нет в DOM).
 */
function mountThread(...args) {
  const ctl = mountThreadRaw(...args);
  if (ctl && typeof ctl.expand === 'function') ctl.expand();
  return ctl;
}

const HERE = dirname(fileURLToPath(import.meta.url));
const css = (name) => readFileSync(join(HERE, '..', 'web', 'css', name), 'utf8');

const NOW = 1793160000;

const THREAD = [
  {
    id: 'c1', at: NOW - 900, author_kind: 'mikhail', author: 'mikhail', author_label: 'Анна',
    kind: 'message', task_id: null,
    text: 'Сроки сдвинулись: письмо уходит 20 ноября, а не 12-го. Учтите это в своде '
      + 'и проверьте, что в приложении Б дата тоже исправлена.',
    delivery: { state: 'applied', version: 4, cards: [], reason: '', text: 'Учтено в версии 4' },
  },
  {
    id: 'c2', at: NOW - 800, author_kind: 'mikhail', author: 'mikhail', author_label: 'Анна',
    kind: 'message', task_id: null,
    text: 'И ещё: считайте склад по шести месяцам, а не по двенадцати.',
    delivery: { state: 'waiting', version: null, cards: [], reason: '',
      text: 'Ждёт обновления свода — в 14:05' },
  },
  {
    id: 'c3', at: NOW - 700, author_kind: 'agent', author: 'team-director',
    author_label: 'Глеб · руководитель комнаты', kind: 'comment', task_id: null,
    text: 'Принял: собираю новую версию ответа.\n[Руководитель · вопрос] Подтвердите срок: '
      + 'письмо уходит 20 ноября или ждём поставщика?',
    asks: ['cq_aaaaaaaa', 'cq_bbbbbbbb'],
  },
  {
    id: 'c4', at: NOW - 600, author_kind: 'system', author: 'system',
    author_label: 'Комната команды', kind: 'event', task_id: null,
    text: 'Версия 4 готова: учтено уточнение',
  },
];

function makeCtx() {
  return {
    room: { id: 'leto', orchestrator: 'team-director' },
    toast() {},
    api: { async post() { return { message: {}, thread: [] }; } },
  };
}

function mount(detail) {
  resetMiniDom();
  // Куда должна привести плашка «→ справа»: блок «Нужен ваш ответ».
  const questions = DOC.createElement('section');
  questions.setAttribute('id', 'questions');
  questions.setAttribute('data-questions', '');
  questions.setAttribute('class', 'card req-side');
  DOC.body.appendChild(questions);
  const host = DOC.createElement('div');
  DOC.body.appendChild(host);
  const ctl = mountThread(host, makeCtx(), { roomId: 'leto', rid: 't_root1', detail });
  return { ctl, host, questions };
}

/* ─────────────────────── подписи доставки ────────────────────────────── */

test('у сообщения пользователя — подпись доставки с сервера', () => {
  const { host } = mount({ id: 't_root1', status: 'done', thread: THREAD });
  const nodes = byClass(host, 'thr-msg__delivery');
  assert.equal(nodes.length, 2, 'подпись у двух сообщений Михаила');
  assert.match(textOf(nodes[0]), /Учтено в версии 4/);
  assert.equal(nodes[0].getAttribute('data-delivery'), 'applied');
  assert.match(nodes[0].getAttribute('class'), /thr-msg__delivery--applied/);
  assert.match(textOf(nodes[1]), /Ждёт обновления свода — в 14:05/);
  assert.match(nodes[1].getAttribute('class'), /thr-msg__delivery--waiting/);
  // У системного события и сообщения руководителя подписи нет.
  const event = byData(host, 'data-kind', 'system')[0];
  assert.equal(byClass(event, 'thr-msg__delivery').length, 0);
  const leader = byClass(host, 'thr-msg').find((n) => /Принял: собираю/.test(textOf(n)));
  assert.ok(leader);
  assert.equal(byClass(leader, 'thr-msg__delivery').length, 0,
    'руководитель не отчитывается о доставке своего же сообщения');
});

test('«Не учтено — автообновление выключено» — отдельный тон', () => {
  const thread = [{
    id: 'c9', at: NOW - 100, author_kind: 'mikhail', author: 'mikhail', author_label: 'Анна',
    kind: 'message', task_id: null, text: 'Проверьте ещё раз объём первой поставки',
    delivery: { state: 'declined', version: null, cards: [], reason: 'auto_off',
      text: 'Не учтено — автообновление свода выключено, нажмите «Обновить сейчас»' },
  }];
  const { host } = mount({ id: 't_root1', status: 'done', thread });
  const node = byClass(host, 'thr-msg__delivery')[0];
  assert.match(node.getAttribute('class'), /thr-msg__delivery--declined/);
  assert.match(textOf(node), /автообновление свода выключено/);
});

test('update(): подпись меняется вместе с данными', async () => {
  const { ctl, host } = mount({ id: 't_root1', status: 'done', thread: THREAD });
  const next = THREAD.map((item) => (item.id === 'c2'
    ? { ...item, delivery: { state: 'applied_nochange', version: 5, cards: [], reason: '', text: 'Учтено, менять нечего' } }
    : item));
  ctl.update({ id: 't_root1', status: 'done', thread: next });
  await tick();
  const texts = byClass(host, 'thr-msg__delivery').map(textOf);
  assert.deepEqual(texts, ['Учтено в версии 4', 'Учтено, менять нечего']);
});

/* ────────────────── «ждёт ваших ответов: N → справа» ─────────────────── */

test('сообщение руководителя с вопросами: плашка и прокрутка к «Нужен ваш ответ»', () => {
  const { host, questions } = mount({ id: 't_root1', status: 'done', thread: THREAD });
  const ask = byClass(host, 'thr-ask')[0];
  assert.ok(ask, 'плашка нарисована');
  assert.equal(ask.getAttribute('data-asks'), '2');
  assert.match(textOf(ask), /ждёт ваших ответов: 2 → справа/);
  assert.equal(byData(host, 'data-action', 'jump-asks').length, 1);

  ask.dispatch('click');
  assert.equal(questions.scrolledIntoView, true, 'прокрутка к блоку вопросов');
  assert.match(questions.getAttribute('class'), /is-flash/, 'блок подсвечен');
});

test('без asks плашки нет; сообщение пользователя её не получает', () => {
  const thread = THREAD.map((item) => {
    const copy = { ...item };
    delete copy.asks;
    return copy;
  });
  const { host } = mount({ id: 't_root1', status: 'done', thread });
  assert.equal(byClass(host, 'thr-ask').length, 0);
});

/* ───────────── монтирование одно (W20-C1): черновик не теряется ──────── */

test('update() не пересобирает поле ввода: набранный текст остаётся', async () => {
  const { ctl, host } = mount({ id: 't_root1', status: 'done', thread: THREAD });
  const field = byClass(host, 'thr__input')[0];
  assert.ok(field);
  field.value = 'черновик, который нельзя потерять';
  ctl.update({ id: 't_root1', status: 'done', thread: THREAD });
  await tick();
  const same = byClass(host, 'thr__input')[0];
  assert.equal(same, field, 'поле — тот же узел');
  assert.equal(same.value, 'черновик, который нельзя потерять');
});

/* ───────────────────────────────── CSS ──────────────────────────────── */

test('CSS: подпись доставки и плашка описаны, текст не мельче 11 px', () => {
  const sheet = css('view-thread.css');
  for (const cls of ['.thr-msg__delivery', '.thr-msg__delivery--applied',
    '.thr-msg__delivery--waiting', '.thr-msg__delivery--declined', '.thr-msg__asks', '.thr-ask']) {
    assert.ok(sheet.includes(cls), 'нет стилей для ' + cls);
  }
  const sizes = [...sheet.matchAll(/[^\n]*font-size:\s*([0-9.]+)px/g)]
    .filter((m) => !/avatar/.test(m[0]))
    .map((m) => Number(m[1]));
  assert.deepEqual(sizes.filter((v) => v < 11), [], 'читаемого текста мельче 11 px нет');
});
