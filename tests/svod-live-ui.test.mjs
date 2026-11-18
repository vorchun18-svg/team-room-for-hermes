/**
 * svod-live-ui.test.mjs — W20-FA §1.1 (B-36): строка «живого свода» в блоке
 * «Свод» и кнопка «Обновить сейчас».
 *
 * Что проверяем:
 *  * вверху блока — строка по `synthesis.live`: «Актуально…», «Обновляется…»,
 *    «Отстаёт…»; прежде всего берётся готовая подпись сервера (`live.text`);
 *  * кнопка «Обновить сейчас» отправляет `{action:"svod_refresh"}`; ответ 409
 *    («обновление уже идёт») не пугает человека, а показывает спокойное сообщение
 *    и обновляет страницу;
 *  * во время обновления кнопки нет — вместо неё «дождитесь новой версии»;
 *  * старая плашка «в своде не учтено» больше не показывается, когда сервер
 *    присылает `synthesis.live` (и остаётся, если сервер старый — без `live`);
 *  * стили строки есть в CSS.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installMiniDom, resetMiniDom, DOC, byClass, byData, textOf } from './mini-dom.mjs';

installMiniDom();

const { createAnswer, liveLineText, liveCountsText } = await import('../web/js/views/request-answer.js');

const HERE = dirname(fileURLToPath(import.meta.url));
const css = (name) => readFileSync(join(HERE, '..', 'web', 'css', name), 'utf8');

const tick = () => new Promise((r) => setTimeout(r, 0));

/* ── данные: вымышленный запрос с длинным сводом ───────────────────────── */

const ANSWER_TEXT = [
  '## Что важно',
  '',
  'Набор на шесть месяцев вместо двенадцати: вход дешевле примерно на 38%, а спрос до первой '
    + 'поставки ещё не подтверждён ни одной отгрузкой. Если за квартал продажи выйдут на 70% плана, '
    + 'добираем вторую половину набора по цене фиксации из приложения Б.',
  '',
  '## Что учесть',
  '',
  '- Склад считается по двум сценариям: 12 месяцев — 240 тыс. руб. в квартал, 6 месяцев — 118 тыс.;',
  '- Возвратная тара покрывает разницу, если поставщик согласится на самовывоз;',
  '- Письмо уходит 20 ноября, после сверки сроков с производством.',
].join('\n');

function makeDetail(live, over = {}) {
  return Object.assign({
    id: 't_root1',
    room: 'leto',
    status: 'done',
    open_questions: [],
    next_steps: [],
    decisions: [],
    tree: [],
    answers: { summary: {} },
    synthesis: {
      state: 'ready',
      version: 2,
      text_md: ANSWER_TEXT,
      author: 'team-director',
      at: 1700000000,
      history: [
        { version: 1, kind: 'initial', task_id: 't_root1', at: 1699990000, note: '', verdict: null },
        { version: 2, kind: 'revision', task_id: 't_rev2', at: 1700000000, note: 'учли сроки', verdict: 'approve' },
      ],
      ...(live === null ? {} : { live }),
    },
  }, over);
}

function makeStand(live, opts = {}) {
  resetMiniDom();
  const posted = [];
  const toasts = [];
  let refreshes = 0;
  const detail = makeDetail(live, opts.detail);
  const R = {
    ctx: {
      api: {
        async post(path, body) {
          posted.push({ path, body });
          if (opts.failWith) throw opts.failWith;
          return { ok: true };
        },
      },
      toast: (message, kind) => toasts.push({ message, kind }),
      room: { id: 'leto' },
      navigate() {},
    },
    rid: 't_root1',
    roomId: 'leto',
    state: { version: null },
    get detail() { return detail; },
    async refresh() { refreshes += 1; },
    render() {},
    cloneRequest() {},
  };
  const { buildAnswer } = createAnswer(R);
  const host = DOC.createElement('div');
  DOC.body.appendChild(host);
  host.appendChild(buildAnswer());
  return { host, posted, toasts, detail, refreshes: () => refreshes };
}

const LIVE_PENDING = {
  state: 'pending', since_version: 2,
  behind: { answers: 2, notes: 1, decisions: 0, cards: 0 },
  last_event_at: 1700000300, next_update_at: 1700000480, auto: true,
  text: 'Отстаёт: 2 ответа и 1 сообщение не учтены — обновится в 14:05',
};

/* ───────────────────────── строка состояния ──────────────────────────── */

test('«Отстаёт»: подпись сервера, кнопка «Обновить сейчас», старой плашки нет', () => {
  const s = makeStand(LIVE_PENDING);
  const line = byClass(s.host, 'req-answer__live')[0];
  assert.ok(line, 'строка живого свода нарисована');
  assert.equal(line.getAttribute('data-live'), 'pending');
  assert.match(line.getAttribute('class'), /req-answer__live--pending/);
  assert.match(textOf(line), /Отстаёт: 2 ответа и 1 сообщение не учтены — обновится в 14:05/);
  assert.equal(byClass(s.host, 'req-answer__stale').length, 0,
    'плашка «в своде не учтено» заменена строкой живого свода');
  const btn = byData(line, 'data-action', 'svod-refresh')[0];
  assert.ok(btn, 'кнопка «Обновить сейчас»');
  assert.match(textOf(btn), /Обновить сейчас/);
  assert.ok(String(btn.getAttribute('title')).length > 20, 'у кнопки есть подсказка');
  // Строка стоит сразу под заголовком блока, а не в подвале.
  const card = byClass(s.host, 'req-answer')[0];
  assert.equal(card.children[1], line, 'строка — первая под заголовком');
  assert.equal(card.children[0].getAttribute('class'), 'req-answer__head');
});

test('«Обновить сейчас» отправляет svod_refresh и обновляет страницу', async () => {
  const s = makeStand(LIVE_PENDING);
  const btn = byData(s.host, 'data-action', 'svod-refresh')[0];
  btn.dispatch('click');
  await tick();
  await tick();
  assert.equal(s.posted.length, 1, 'один запрос');
  assert.equal(s.posted[0].path, '/api/requests/t_root1/actions');
  assert.deepEqual(s.posted[0].body, { action: 'svod_refresh' });
  assert.ok(s.toasts.some((t) => t.kind === 'ok' && /Обновление свода запущено/.test(t.message)));
  assert.equal(s.refreshes(), 1, 'данные перечитаны — состояние станет «обновляется»');
  assert.equal(btn.disabled, false, 'кнопка снова доступна');
});

test('409 «уже идёт» — спокойное сообщение, без красной ошибки', async () => {
  const conflict = Object.assign(new Error('Обновление свода уже идёт — дождитесь новой версии.'),
    { status: 409, code: 'conflict' });
  const s = makeStand(LIVE_PENDING, { failWith: conflict });
  byData(s.host, 'data-action', 'svod-refresh')[0].dispatch('click');
  await tick();
  await tick();
  assert.equal(s.posted.length, 1);
  const toast = s.toasts.find((t) => /уже идёт/.test(t.message));
  assert.ok(toast, 'человек видит причину');
  assert.equal(toast.kind, 'info', 'это не ошибка — обновление просто уже идёт');
  assert.equal(s.refreshes(), 1, 'страница перечитана: скоро появится «Обновляется…»');
  assert.equal(byClass(s.host, 'req-answer__live').length, 1, 'строка на месте');
});

test('«Обновляется…»: кнопки нет, видно, что работа идёт', () => {
  const live = {
    state: 'updating', since_version: 2, behind: { answers: 2, notes: 1, decisions: 0, cards: 0 },
    last_event_at: 1700000300, next_update_at: null, auto: true,
    text: 'Обновляется… версия 3',
  };
  const s = makeStand(live);
  const line = byClass(s.host, 'req-answer__live')[0];
  assert.ok(line);
  assert.equal(line.getAttribute('data-live'), 'updating');
  assert.match(textOf(line), /Обновляется… версия 3/);
  assert.equal(byData(line, 'data-action', 'svod-refresh').length, 0, 'второй раз не запустить');
  assert.match(textOf(line), /дождитесь новой версии/);
});

test('«Актуально» и «Отстаёт — автообновление выключено»: разные тона', () => {
  // W25.5: когда свод актуален, пометку «Актуально» несёт липкая панель
  // (shell-svod-bar.js → .req-bar__live), а в карточке строка НЕ повторяется:
  // её убрали как второй экземпляр одного и того же («дубль шапки свода»).
  const fresh = makeStand({
    state: 'fresh', since_version: 4, behind: { answers: 0, notes: 0, decisions: 0, cards: 0 },
    last_event_at: null, next_update_at: null, auto: true,
    text: 'Актуально на 13:48 · версия 4 · учтено: 9 ответов, 3 сообщения, 2 решения',
  });
  assert.equal(byClass(fresh.host, 'req-answer__live').length, 0,
    'при актуальном своде строка живого свода в карточке не дублируется');

  const stale = makeStand({
    state: 'stale', since_version: 2, behind: { answers: 2, notes: 0, decisions: 0, cards: 0 },
    last_event_at: 1700000300, next_update_at: null, auto: false,
    text: 'Отстаёт: 2 ответа не учтены — автообновление свода выключено, обновите вручную',
  });
  const staleLine = byClass(stale.host, 'req-answer__live')[0];
  assert.match(staleLine.getAttribute('class'), /req-answer__live--stale/);
  assert.match(textOf(staleLine), /автообновление свода выключено/);
});

test('старый сервер без synthesis.live: остаётся прежняя плашка «не учтено»', () => {
  const s = makeStand(null, {
    detail: {
      open_questions: [{
        id: 'q_1', text: 'Какой срок подачи?', answered: true, answer: 'Двадцатое ноября',
        answered_at: 1700000200, for: 'mikhail',
      }],
    },
  });
  assert.equal(byClass(s.host, 'req-answer__live').length, 0, 'строки живого свода нет');
  const stale = byClass(s.host, 'req-answer__stale')[0];
  assert.ok(stale, 'прежняя плашка на месте');
  assert.match(textOf(stale), /в своде это ещё не учтено/);
});

/* ───────────────────────── чистые функции подписи ─────────────────────── */

test('подпись: слова сервера главнее, иначе — свои по состоянию', () => {
  assert.equal(liveLineText({ state: 'fresh', text: 'Актуально на 10:00 · версия 9' }),
    'Актуально на 10:00 · версия 9');
  assert.equal(liveLineText({ state: 'pending', since_version: 2, behind: { answers: 2, notes: 1 } }),
    'Не учтено: 2 ответа и 1 сообщение — свод обновится сам');
  assert.equal(liveLineText({ state: 'updating', since_version: 2 }), 'Обновляется… версия 3');
  assert.equal(liveLineText({ state: 'stale', behind: { answers: 1 } }),
    'Не учтено: 1 ответ — автообновление свода выключено, обновите вручную');
  assert.equal(liveLineText({ state: 'off' }),
    'Свод ещё не выпущен — появится после первого ответа команды.');
  assert.equal(liveLineText({ state: 'fresh', since_version: 4 }), 'Актуально · версия 4');
  assert.equal(liveLineText(null), 'Свод актуален');
});

test('счёт «что не учтено»: правильные формы и порядок', () => {
  assert.equal(liveCountsText({ answers: 1 }), '1 ответ');
  assert.equal(liveCountsText({ answers: 2, notes: 1, decisions: 1, cards: 3 }),
    '2 ответа, 1 сообщение, 1 решение и 3 карточки');
  assert.equal(liveCountsText({}), '');
  assert.equal(liveCountsText(null), '');
  assert.equal(liveCountsText({ answers: 0, notes: 0 }), '');
});

/* ───────────────────────────────── CSS ───────────────────────────────── */

test('CSS: строка живого свода описана, текст не мельче 11 px', () => {
  const sheet = css('view-request.css');
  for (const cls of ['.req-answer__live', '.req-answer__live-text', '.req-answer__live--fresh',
    '.req-answer__live--pending', '.req-answer__live--updating', '.req-answer__live--stale',
    '.req-answer__live-btn']) {
    assert.ok(sheet.includes(cls), 'нет стилей для ' + cls);
  }
  const sizes = [...sheet.matchAll(/[^\n]*font-size:\s*([0-9.]+)px/g)]
    .filter((m) => !/avatar/.test(m[0]))
    .map((m) => Number(m[1]));
  assert.deepEqual(sizes.filter((v) => v < 11), [], 'читаемого текста мельче 11 px нет');
});
