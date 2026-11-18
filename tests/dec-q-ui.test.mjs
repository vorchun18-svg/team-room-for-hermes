/**
 * dec-q-ui.test.mjs — W20-FA §1.2 (B-37, B-38, B-41): решения и вопросы
 * в правой колонке запроса.
 *
 * Что проверяем:
 *  * «Снять — неактуально» — отдельное действие с окном и полем; снятое решение
 *    серое (`status_muted`), с кнопкой «Вернуть» (одно нажатие), без трёх
 *    привычных кнопок и без ожидания ответа;
 *  * «устарело» — понятно, с какой версии и почему (отозвал руководитель / нет в
 *    новой версии свода);
 *  * вопросы руководителя из его сообщений: пометка источника у `comment` и
 *    `candidate`, «Это не вопрос» — только у кандидата, с отправкой
 *    `question_dismiss`;
 *  * `replied_text` («руководитель ответил текстом — посмотрите в ленте») виден
 *    у вопроса в «Отвечено» и убирает его из «Нужен ваш ответ»;
 *  * снятый вопрос («это не вопрос») не просит ответа и формы не показывает.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installMiniDom, resetMiniDom, DOC, byClass, byData, textOf, tick } from './mini-dom.mjs';

installMiniDom();

const { createAside } = await import('../web/js/views/request-aside.js');
const { decisionMutedActions, decisionStatusOf } = await import('../web/js/views/decision-dialog.js');

const HERE = dirname(fileURLToPath(import.meta.url));
const css = (name) => readFileSync(join(HERE, '..', 'web', 'css', name), 'utf8');

/* ── данные: вымышленные решения и вопросы (длинные тексты, как в жизни) ── */

const DECISION_LONG = 'Стартовать с набора из шести месяцев вместо двенадцати: вход дешевле '
  + 'примерно на 38% (по смете закупки от 05.11), а спрос до первой поставки ещё не подтверждён '
  + 'ни одной отгрузкой. Если за квартал продажи выйдут на 70% плана — добираем вторую половину '
  + 'набора по цене фиксации, она зафиксирована в приложении Б и не индексируется.';

const QUESTION_LONG = 'Подтвердите, пожалуйста, срок: письмо уходит 20 ноября или ждём ответа '
  + 'поставщика по партии? Если ждём — нужно ли предупредить заказчика отдельным письмом '
  + 'и что писать про объём первой поставки: шесть месяцев или «по согласованию»?';

function makeDetail(over = {}) {
  return Object.assign({
    id: 't_root1',
    open_questions: [
      {
        id: 'cq_1a2b3c4d', text: QUESTION_LONG, source: 'comment', comment_id: 'c7',
        at: 180, for: 'mikhail', answered: false, kind: 'question',
      },
      {
        id: 'cq_5e6f7a8b', text: 'Согласовать возвратную тару с поставщиком до 1 декабря',
        source: 'candidate', comment_id: 'c7', at: 180, for: 'mikhail', answered: false, kind: 'question',
      },
      {
        id: 'q_replied', text: 'Нужно ли отдельное письмо заказчику?', source: 'card',
        answered: true, answer: 'Да, короткое письмо на одну страницу', answered_at: 200,
        answer_state: 'replied_text',
        answer_label: 'руководитель ответил текстом — посмотрите в ленте', for: 'mikhail',
        answered_text_only: true,
      },
      {
        id: 'cq_dismissed', text: 'Это просто напоминание, а не вопрос', source: 'candidate',
        dismissed: true, answered: false, for: 'mikhail', kind: 'question',
      },
    ],
    next_steps: [],
    future_questions: [],
    decisions: [
      {
        id: 'd_open1', text: DECISION_LONG, status: 'proposed', open: true, needs_answer: true,
        can_dismiss: true, can_restore: false,
        muted_actions: [
          { action: 'dismiss', label: 'Снять — неактуально', hint: 'Убрать из «Нужен ваш ответ»', available: true },
          { action: 'restore', label: 'Вернуть', hint: 'Вернуть снятое решение', available: false },
        ],
      },
      {
        id: 'd_gone1', text: 'Считать цену сделки от базовой ставки за кубометр',
        status: 'dismissed', status_label: 'снято — не актуально', status_muted: true,
        open: false, needs_answer: false, can_dismiss: false, can_restore: true, dismissed_at: 400,
        muted_actions: [
          { action: 'dismiss', label: 'Снять — неактуально', hint: '', available: false },
          { action: 'restore', label: 'Вернуть', hint: 'Вернуть снятое решение', available: true },
        ],
      },
      {
        id: 'd_old1', text: 'Брать упаковку у местного поставщика в прежнем объёме',
        status: 'superseded', status_label: 'устарело', status_muted: true, open: false,
        needs_answer: false, can_dismiss: false, can_restore: false,
        superseded_by: 3, superseded_why: 'absent',
      },
    ],
    tree: [],
    answers: { summary: {} },
  }, over || {});
}

function makeStand(over = {}) {
  resetMiniDom();
  const posted = [];
  const toasts = [];
  let renders = 0;
  const detail = makeDetail(over.detail);
  const R = {
    ctx: {
      api: {
        async post(path, body) { posted.push({ path, body }); return { ok: true }; },
        async get() { return {}; },
      },
      toast: (message, kind) => toasts.push({ message, kind }),
      room: { id: 'leto' },
      navigate() {},
    },
    rid: 't_root1',
    keepValues: new Map(),
    roomId: 'leto',
    get detail() { return detail; },
    get backlog() { return new Map(); },
    async refresh() { renders += 1; },
    render() {},
  };
  const { buildAside } = createAside(R);
  const host = DOC.createElement('div');
  DOC.body.appendChild(host);
  host.appendChild(buildAside());
  return { host, posted, toasts, detail, renders: () => renders };
}

const rowsOf = (root, id) => byData(root, 'data-decision-id', id)
  .filter((n) => String(n.getAttribute('class') || '').split(/\s+/).includes('req-decision'));

/* ─────────────────── B-37: «снять — неактуально» и «вернуть» ─────────── */

test('«Снять — неактуально» есть у решения и открывает окно с полем', async () => {
  const s = makeStand();
  const row = rowsOf(s.host, 'd_open1')[0];
  assert.ok(row, 'решение нарисовано');
  const dismiss = byData(row, 'data-action', 'decision-dismiss')[0];
  assert.ok(dismiss, 'кнопка «Снять — неактуально»');
  assert.match(textOf(dismiss), /Снять — неактуально/);
  assert.ok(String(dismiss.getAttribute('title')).length > 20, 'у кнопки есть подсказка');

  dismiss.dispatch('click');
  await tick();
  const dialogs = byClass(DOC.body, 'decdlg');
  assert.equal(dialogs.length, 1, 'окно открылось');
  assert.match(textOf(dialogs[0]), /Снять решение/);
  assert.match(textOf(dialogs[0]), /Стартовать с набора из шести месяцев/, 'в окне видно решение');
  const field = byClass(dialogs[0], 'decdlg__input')[0];
  assert.ok(field, 'поле для причины (необязательное)');
  assert.match(String(field.getAttribute('placeholder')), /[Пп]очему/);
});

test('снятие отправляет decision:dismiss и закрывает окно', async () => {
  const s = makeStand();
  rowsOf(s.host, 'd_open1')[0];
  byData(s.host, 'data-action', 'decision-dismiss')[0].dispatch('click');
  await tick();
  const dialog = byClass(DOC.body, 'decdlg')[0];
  byClass(dialog, 'decdlg__input')[0].value = 'Цена уже не наша тема';
  byData(dialog, 'data-action', 'decision-dismiss')[0].dispatch('click');
  await tick();
  await tick();
  assert.equal(s.posted.length, 1);
  assert.deepEqual(s.posted[0].body, {
    action: 'decision', decision_id: 'd_open1', decision: 'dismiss', text: 'Цена уже не наша тема',
  });
  assert.equal(byClass(DOC.body, 'decdlg').length, 0, 'окно закрылось');
  assert.ok(s.toasts.some((t) => /Решение снято/.test(t.message)), 'человек видит, что произошло');
  assert.equal(s.renders(), 1, 'данные перечитаны');
});

test('снятое решение — серым, с «Вернуть», без трёх кнопок', async () => {
  const s = makeStand();
  const fold = byClass(s.host, 'req-side--fold').find((f) => f.getAttribute('data-fold') === 'decisions');
  assert.ok(fold, 'блок «Решения» есть');
  const row = rowsOf(fold, 'd_gone1')[0];
  assert.ok(row, 'снятое решение остаётся в списке');
  assert.equal(row.getAttribute('data-decision-status'), 'dismissed');
  assert.equal(row.getAttribute('data-muted'), 'true');
  assert.match(row.getAttribute('class'), /req-decision--muted/);
  assert.match(textOf(row), /снято — не актуально/);
  assert.equal(byData(row, 'data-action', 'decision-accept').length, 0, 'привычных кнопок нет');
  assert.equal(byData(row, 'data-action', 'decision-reject').length, 0);
  const restore = byData(row, 'data-action', 'decision-restore')[0];
  assert.ok(restore, 'кнопка «Вернуть»');
  assert.match(textOf(restore), /Вернуть/);

  restore.dispatch('click');
  await tick();
  await tick();
  assert.deepEqual(s.posted[0].body, { action: 'decision', decision_id: 'd_gone1', decision: 'restore' });
  assert.ok(s.toasts.some((t) => /возвращено/.test(t.message)));
  assert.equal(s.renders(), 1);
});

test('устаревшее решение: «устарело» и причина с версией', () => {
  const s = makeStand();
  const row = rowsOf(s.host, 'd_old1')[0];
  assert.ok(row);
  assert.equal(row.getAttribute('data-decision-status'), 'superseded');
  assert.match(row.getAttribute('class'), /req-decision--muted/);
  assert.match(textOf(row), /устарело/);
  const note = byClass(row, 'req-decision__superseded')[0];
  assert.ok(note, 'строка «почему устарело»');
  assert.match(textOf(note), /с версии 3/);
  assert.match(textOf(note), /нет в новой версии свода/);
  assert.equal(byData(row, 'data-action', 'decision-restore').length, 0);
});

test('модель: «снять» и «вернуть» — отдельно от трёх действий', () => {
  assert.deepEqual(decisionMutedActions({}).map((a) => a.action), []);
  assert.deepEqual(decisionMutedActions({
    muted_actions: [
      { action: 'dismiss', label: 'Снять — неактуально', available: true },
      { action: 'restore', label: 'Вернуть', available: false },
    ],
  }).map((a) => a.action), ['dismiss']);
  assert.deepEqual(decisionMutedActions({ can_restore: true }).map((a) => a.label), ['Вернуть']);
  assert.equal(decisionStatusOf({ status: 'dismissed' }).label, 'снято — не актуально');
  assert.equal(decisionStatusOf({ status: 'superseded' }).label, 'устарело');
  assert.equal(decisionStatusOf({ status: 'dismissed' }).tone, 'muted');
});

/* ────────── B-38: вопросы из сообщений и «Это не вопрос» ──────────────── */

test('вопрос из сообщения помечен источником; «Это не вопрос» — только у кандидата', () => {
  const s = makeStand();
  const pin = byClass(s.host, 'req-side--pin')[0];
  const comment = byData(pin, 'data-q', 'cq_1a2b3c4d')[0];
  const candidate = byData(pin, 'data-q', 'cq_5e6f7a8b')[0];
  assert.ok(comment && candidate, 'оба вопроса в «Нужен ваш ответ»');

  const commentMark = byClass(comment, 'req-q__source')[0];
  assert.ok(commentMark, 'пометка источника у вопроса из сообщения');
  assert.match(textOf(commentMark), /из сообщения руководителя/);
  assert.doesNotMatch(textOf(commentMark), /проверьте/, 'это не кандидат');
  assert.equal(byData(comment, 'data-action', 'question-dismiss').length, 0,
    'у явного вопроса кнопки «Это не вопрос» нет');

  const candidateMark = byClass(candidate, 'req-q__source')[0];
  assert.match(textOf(candidateMark), /проверьте, вопрос ли это/);
  const dismiss = byData(candidate, 'data-action', 'question-dismiss')[0];
  assert.ok(dismiss, 'у кандидата есть «Это не вопрос»');
  assert.match(textOf(dismiss), /Это не вопрос/);
  assert.equal(dismiss.getAttribute('data-question-id'), 'cq_5e6f7a8b');
});

test('«Это не вопрос» отправляет question_dismiss и убирает вопрос из счёта', async () => {
  const s = makeStand();
  byData(s.host, 'data-action', 'question-dismiss')[0].dispatch('click');
  await tick();
  await tick();
  assert.equal(s.posted.length, 1);
  assert.deepEqual(s.posted[0].body, { action: 'question_dismiss', question_id: 'cq_5e6f7a8b' });
  assert.ok(s.toasts.some((t) => /это не вопрос/i.test(t.message)));
  assert.equal(s.renders(), 1);
});

test('снятый вопрос не просит ответа и формы не показывает', () => {
  const s = makeStand();
  const pin = byClass(s.host, 'req-side--pin')[0];
  const row = byData(pin, 'data-q', 'cq_dismissed')[0];
  assert.equal(row, undefined, 'снятого вопроса нет в «Нужен ваш ответ»');
  assert.equal(byData(s.host, 'data-q', 'cq_dismissed').length, 0, 'и формы ответа тоже');
});

/* ─────────────────── B-41: «руководитель ответил текстом» ─────────────── */

test('replied_text: подпись в «Отвечено» и отсутствие в «Нужен ваш ответ»', () => {
  const s = makeStand();
  const pin = byClass(s.host, 'req-side--pin')[0];
  assert.equal(byData(pin, 'data-q', 'q_replied').length, 0, 'вопрос уже не ждёт ответа');
  const fold = byClass(s.host, 'req-side--fold').find((f) => f.getAttribute('data-fold') === 'answered');
  assert.ok(fold, 'блок «Отвечено» есть');
  const row = byData(fold, 'data-q', 'q_replied')[0];
  assert.ok(row, 'вопрос виден в «Отвечено»');
  const chip = byClass(row, 'req-q__state')[0];
  assert.equal(textOf(chip), 'руководитель ответил текстом — посмотрите в ленте');
  assert.match(chip.getAttribute('class'), /chip--info/);
  assert.match(String(chip.getAttribute('title')), /в ленте|диалог/i);
  assert.equal(row.getAttribute('data-q-state'), 'replied_text');
});

/* ───────────────────────────────── CSS ──────────────────────────────── */

test('CSS: пометки источника, снятые решения и их кнопки описаны', () => {
  const sheet = css('view-request.css');
  for (const cls of ['.req-q__src', '.req-q__source', '.req-q__dismiss', '.req-decision__muted-actions',
    '.req-decision--muted', '.req-decision__superseded']) {
    assert.ok(sheet.includes(cls), 'нет стилей для ' + cls);
  }
  const sizes = [...sheet.matchAll(/[^\n]*font-size:\s*([0-9.]+)px/g)]
    .filter((m) => !/avatar/.test(m[0]))
    .map((m) => Number(m[1]));
  assert.deepEqual(sizes.filter((v) => v < 11), [], 'читаемого текста мельче 11 px нет');
});
