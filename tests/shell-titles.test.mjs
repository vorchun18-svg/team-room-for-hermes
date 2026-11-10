/**
 * tests/shell-titles.test.mjs — пакет G (волна 17): названия запросов,
 * переименование карандашом, заглушки загрузки, «Дальше» без дублей и повторов.
 *
 * Проверяется то, что видно человеку: короткое название вместо сырой диктовки,
 * полный текст в подсказке, приоритет пользовательского названия, схлопывание
 * близнецов вопросов, «Загружаю…» вместо нулей.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom } from './fake-dom.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const js = (name) => readFileSync(join(here, '..', 'web', 'js', name), 'utf8');

const {
  TITLE_SHORT_MAX, cleanTitleText, deriveTitle, titleShortOf, titleFullOf,
  titleLockedOf, titleSourceOf, titleHint, applyTitlePayload, validateTitle,
  renameBody, lockHint,
} = await import('../web/js/request-title.js');

const {
  questionTextKey, isServiceQuestion, questionStateOf, dedupeByText,
  visibleQuestions, partitionBacklog,
} = await import('../web/js/views/backlog-model.js');

const { skeletonCards, skeletonRows, countOrLoading, loadingLabel, LOADING_TEXT } = await import('../web/js/shell-skeleton.js');

/* ────────────────────────── названия: короткий текст и подсказка ───────── */

const DICTATION = 'Так, начнём работу в этой комнате. Работа будет построена следующим образом. '
  + 'Для начала нужно разобрать договор поставки с поставщиком и проверить условия оплаты, '
  + 'потом собрать риски и предложить план действий на две недели с ответственными и сроками.';

test('deriveTitle убирает вводные слова и берёт первое предложение', () => {
  const out = deriveTitle(DICTATION);
  assert.ok(!/^Так,/i.test(out), 'вводное «Так,» не осталось');
  assert.ok(out.startsWith('Начнём работу в этой комнате'), out);
  assert.ok(Array.from(out).length <= TITLE_SHORT_MAX + 1, 'не длиннее 60 знаков');
});

test('deriveTitle: длинная диктовка 3000 знаков не ломается и обрезается по слову', () => {
  const long = 'В общем, ' + ('нужно проверить документы по упаковке и бренду '.repeat(70));
  const out = deriveTitle(long);
  assert.ok(out.length > 0);
  assert.ok(Array.from(out).length <= TITLE_SHORT_MAX + 1);
  assert.ok(!/\s$/.test(out), 'без висящего пробела');
  assert.ok(!/^В общем/i.test(out), 'вводное «В общем» не осталось');
});

test('deriveTitle: пустой текст — пустая строка, без исключения', () => {
  assert.equal(deriveTitle(''), '');
  assert.equal(deriveTitle(null), '');
  assert.equal(deriveTitle(undefined), '');
});

test('cleanTitleText убирает markdown и лишние пробелы', () => {
  assert.equal(cleanTitleText('## **Ответ** на [запрос](http://x)'), 'Ответ на запрос');
  assert.equal(cleanTitleText('- пункт\n- ещё'), 'пункт ещё');
});

test('titleShortOf берёт title_short с сервера, не сырую диктовку', () => {
  const item = { title: DICTATION, title_short: 'Ответ на запрос заказчика по договору' };
  assert.equal(titleShortOf(item), 'Ответ на запрос заказчика по договору');
});

test('titleShortOf считает сам, если сервер старый (без title_short)', () => {
  const out = titleShortOf({ title: DICTATION });
  assert.ok(out.length > 8 && out.length <= TITLE_SHORT_MAX + 1, out);
  assert.ok(!/^Так,/i.test(out));
});

test('titleFullOf — полный текст для подсказки, titleHint добавляет источник', () => {
  const item = { title: DICTATION, title_short: 'Ответ по договору', title_source: 'user' };
  assert.equal(titleFullOf(item), DICTATION);
  const hint = titleHint(item, 'Комната: Гранит');
  assert.ok(hint.includes(DICTATION), 'полный текст в подсказке');
  assert.ok(hint.includes('задано вами'), 'сказано, что название задано человеком');
  assert.ok(hint.includes('Гранит'));
});

test('замок: title_locked и title_source=user читаются одинаково', () => {
  assert.equal(titleLockedOf({ title_locked: true }), true);
  assert.equal(titleLockedOf({ title_source: 'user' }), true);
  assert.equal(titleLockedOf({ title_source: 'llm' }), false);
  assert.equal(titleSourceOf({ title_source: 'leader' }), 'leader');
  assert.equal(titleSourceOf({ title_source: 'мусор' }), '');
  assert.match(lockHint(true), /сбросить/);
});

test('applyTitlePayload подмешивает ответ PATCH в объект запроса', () => {
  const detail = { id: 't_1a2b3c4d', title: 'старое', title_short: 'Старое' };
  applyTitlePayload(detail, { ok: true, request: { title_short: 'Новое', title_source: 'user', title_locked: true } });
  assert.equal(detail.title_short, 'Новое');
  assert.equal(detail.title_source, 'user');
  assert.equal(detail.title_locked, true);
});

test('validateTitle: пусто/то же/длинно/нормально', () => {
  const item = { title_short: 'Ответ по договору' };
  assert.equal(validateTitle('   ', item).reason, 'empty');
  assert.equal(validateTitle('Ответ по договору', item).reason, 'same');
  assert.equal(validateTitle('а'.repeat(121), item).reason, 'long');
  const ok = validateTitle('  Свод по уставу  ', item);
  assert.equal(ok.ok, true);
  assert.equal(ok.value, 'Свод по уставу');
  assert.deepEqual(renameBody(''), { title: '' });
});

/* ───────────────────────── «Дальше»: близнецы и служебные вопросы ─────── */

// Живые образцы аудита 06.11 (B-7) из fixtures/questions_live_fn.json: один
// вопрос под разными id — тексты отличаются только скобочным уточнением.
const Q_A = { id: 'q_d47fe5e7', text: 'Ольга К. (маркшейдер, TaskFlow): передавать маркшейдерские части или вне контура?' };
const Q_A2 = { id: 'q_d5b4abf0', text: 'Ольга К. (маркшейдер): передавать маркшейдерские части или вне контура?' };
const Q_SVC = { id: 'q_6f877b08', text: 'Порог обязательного прогона через критика (рекомендация: всё, что уходит наружу, и выводы с деньгами от 100 000 ₽)' };
const Q_PLAIN = { id: 'q_789a8dce', text: 'Первая работа: дело ООО Каменка (Заречное-1)?' };

test('questionTextKey не считает разницей регистр, знаки и скобки', () => {
  assert.equal(
    questionTextKey('Не к спеху: Договор с поставщиком (уточнение) — подходит ли форма?'),
    questionTextKey('договор с поставщиком подходит ли форма'),
  );
});

test('dedupeByText схлопывает близнецов и помнит все id в aliases', () => {
  const out = dedupeByText([Q_A, Q_A2]);
  assert.equal(out.length, 1);
  const ids = [out[0].id, ...(out[0].aliases || [])];
  assert.ok(ids.includes('q_d47fe5e7'));
  assert.ok(ids.includes('q_d5b4abf0'));
});

test('dedupeByText: отвеченный близнец представляет пару (не висит «open»)', () => {
  const answered = { ...Q_A2, status: 'done', answer: 'Типовая подходит', answer_state: 'applied' };
  const out = dedupeByText([Q_A, answered]);
  assert.equal(out.length, 1);
  assert.equal(questionStateOf(out[0]).state, 'answered');
  assert.equal(out[0].status, 'done');
});

test('isServiceQuestion скрывает «сколько раундов критика», но не обычный вопрос', () => {
  assert.equal(isServiceQuestion(Q_SVC.text), true);
  assert.equal(isServiceQuestion('Сколько стоит доставка до склада?'), false);
  assert.equal(isServiceQuestion(Q_PLAIN.text), false);
  assert.equal(visibleQuestions([Q_A, Q_A2, Q_SVC]).length, 1);
});

test('questionStateOf берёт состояние из полей API, без догадок', () => {
  assert.equal(questionStateOf({ id: 'q_1', status: 'open' }).state, 'open');
  assert.equal(questionStateOf({ id: 'q_1', status: 'open', answer_state: 'waiting_explanation' }).state, 'explanation');
  assert.equal(questionStateOf({ id: 'q_1', status: 'done' }).state, 'answered');
  assert.equal(questionStateOf({ id: 'q_1', answer: 'да' }).state, 'answered');
  assert.equal(questionStateOf({ id: 'q_1', status: 'done', answer_label: 'отвечен' }).label, 'отвечен');
});

test('partitionBacklog: служебный вопрос не попадает в «ждут решения»', () => {
  const parts = partitionBacklog([
    { ...Q_SVC, kind: 'open_question', status: 'open' },
    { ...Q_A, kind: 'open_question', status: 'open' },
    { ...Q_A2, kind: 'open_question', status: 'open' },
    { ...Q_PLAIN, kind: 'open_question', status: 'open' },
  ]);
  assert.equal(parts.questions.length, 2);
  assert.ok(parts.questions.some((q) => q.id === 'q_d47fe5e7'));
  assert.ok(!parts.questions.some((q) => q.id === 'q_6f877b08'));
});

/* ───────────────────────────── заглушки загрузки (B14) ────────────────── */

test('skeletonCards: карточки в форме будущих + «Загружаю…» для скринридера', () => {
  installFakeDom();
  const node = skeletonCards({ rows: 2, variant: 'question' });
  assert.equal(node.getAttribute('role'), 'status');
  assert.equal(node.getAttribute('aria-busy'), 'true');
  assert.match(node.textContent, /Загружаю/);
  const cards = node.childNodes.filter((n) => n.nodeType === 1 && /skel-card/.test(n.className));
  assert.equal(cards.length, 2);
  assert.ok(cards[0].className.includes('skel-card--question'));
  const hidden = node.childNodes.find((n) => n.nodeType === 1 && /sr-only/.test(n.className));
  assert.ok(hidden, 'подпись есть');
});

test('skeletonRows и подписи счётчиков: «Загружаю…», а не «0»', () => {
  installFakeDom();
  const rows = skeletonRows(3);
  assert.equal(rows.childNodes.filter((n) => n.nodeType === 1 && /skel-row/.test(n.className)).length, 3);
  assert.equal(countOrLoading(0, true), LOADING_TEXT);
  assert.equal(countOrLoading(4, false), '4');
  assert.equal(countOrLoading(null, false), '—');
  assert.equal(loadingLabel('вопросы'), 'Загружаю… вопросы');
});

/* ───────────────────── переименование карандашом (в шапке) ────────────── */

test('buildTitleBlock: короткое название, карандаш, замок только у «ваше название»', async () => {
  installFakeDom();
  const { buildTitleBlock } = await import('../web/js/title-edit.js');
  let detail = { id: 't_1a2b3c4d', title: DICTATION, title_short: 'Ответ по договору' };
  const api = { patch: async () => ({ ok: true, request: { title_short: 'Новое название', title_source: 'user', title_locked: true } }) };
  const block = buildTitleBlock({ getDetail: () => detail, api, toast: () => {} });
  const title = block.find('h1');
  assert.equal(title.textContent, 'Ответ по договору');
  assert.ok(title.getAttribute('title').includes(DICTATION), 'полный текст в подсказке');
  const lock = block.childNodes.find((n) => n.nodeType === 1 && /req__lock/.test(n.className));
  assert.equal(lock.hidden, true, 'без переименования замка нет');
  const pencil = block.childNodes.find((n) => n.nodeType === 1 && n.getAttribute('data-act') === 'rename');
  assert.ok(pencil, 'карандаш есть');

  // Переименовали на сервере → замок виден (перерисовка шапки опросом).
  detail = { ...detail, title_source: 'user', title_locked: true };
  const block2 = buildTitleBlock({ getDetail: () => detail, api, toast: () => {} });
  const lock2 = block2.childNodes.find((n) => n.nodeType === 1 && /req__lock/.test(n.className));
  assert.equal(lock2.hidden, false);
  assert.match(lock2.getAttribute('title'), /сбросить/);
});

test('buildTitleBlock: Enter в поле отправляет PATCH и обновляет название', async () => {
  installFakeDom();
  const { buildTitleBlock } = await import('../web/js/title-edit.js');
  const detail = { id: 't_1a2b3c4d', title: DICTATION, title_short: 'Старое' };
  const calls = [];
  const api = {
    patch: async (path, body) => {
      calls.push([path, body]);
      return { ok: true, request: { title: body.title, title_short: body.title, title_source: 'user', title_locked: true } };
    },
  };
  let saved = 0;
  const block = buildTitleBlock({ getDetail: () => detail, api, toast: () => {}, onSaved: () => { saved += 1; } });
  const pencil = block.childNodes.find((n) => n.nodeType === 1 && n.getAttribute('data-act') === 'rename');
  pencil.dispatch('click', { type: 'click', preventDefault() {} });
  const input = block.find('input');
  assert.equal(input.hidden, false, 'поле правки открылось');
  input.value = 'Свод по уставу';
  input.dispatch('keydown', { type: 'keydown', key: 'Enter', preventDefault() {} });
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/requests/t_1a2b3c4d');
  assert.equal(calls[0][1].title, 'Свод по уставу');
  assert.equal(detail.title_short, 'Свод по уставу');
  assert.equal(detail.title_locked, true);
  assert.equal(saved, 1);
});

test('buildTitleBlock: Esc отменяет правку без запроса к серверу', async () => {
  installFakeDom();
  const { buildTitleBlock } = await import('../web/js/title-edit.js');
  const detail = { id: 't_1a2b3c4d', title_short: 'Старое' };
  let calls = 0;
  const api = { patch: async () => { calls += 1; return {}; } };
  const block = buildTitleBlock({ getDetail: () => detail, api, toast: () => {} });
  block.childNodes.find((n) => n.nodeType === 1 && n.getAttribute('data-act') === 'rename')
    .dispatch('click', { type: 'click', preventDefault() {} });
  const input = block.find('input');
  input.value = 'Что-то другое';
  input.dispatch('keydown', { type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(calls, 0);
  assert.equal(input.hidden, true);
  assert.equal(block.find('h1').textContent, 'Старое');
});

/* ─────────────── где что показывается: статические проверки ───────────── */

test('«Недавнее» берёт данные одним запросом сводки и показывает title_short', () => {
  const src = js('shell-sidebar.js');
  assert.match(src, /api\/rooms\/summary\?requests=/);
  // 304 от сводки не должен гонять пачку запросов по комнатам заново.
  assert.match(src, /notModified\) return 'unchanged'/);
  assert.match(src, /titleShortOf\(item\)/);
  assert.match(src, /titleHint\(item/);
  assert.match(src, /from '\.\/request-title\.js'/);
});

test('Список запросов: короткое название + подсказка, заглушки-карточки', () => {
  const src = js('views/composer-feed.js');
  assert.match(src, /titleShortOf\(item\)/);
  assert.match(src, /title: titleHint\(item\)/);
  assert.match(src, /skeletonCards\(\{ rows: 3/);
  assert.doesNotMatch(src, /h\('h3', \{ class: 'cmp-card__title' \}, String\(item\.title/);
});

test('«Дальше»: единая форма ответа, близнецы, мгновенное снятие ответа', () => {
  const src = js('views/backlog.js');
  assert.match(src, /buildQuestionForm/);
  assert.doesNotMatch(src, /inlineForm\(\{\s*label: 'Ответ на вопрос'/);
  assert.match(src, /markAnswered\(item\)/);
  assert.match(src, /questionIds\(item\)/);
  assert.match(src, /Считаются вопросы без вашего ответа/);
  const model = js('views/backlog-model.js');
  assert.match(model, /visibleQuestions/);
  assert.match(model, /isServiceQuestion/);
});

test('Крошки: короткое название, полный текст в подсказке', () => {
  const app = js('app.js');
  assert.match(app, /requestTitleInfo as fetchRequestTitleInfo/);
  assert.match(app, /requestHint: requestCrumbHint/);
  assert.match(app, /const hint = item\.hint \|\| item\.text;/);
  const scope = js('scope.js');
  assert.match(scope, /hint: titleHint|item\.hint = hint/);
});

test('Шапка запроса: карандаш и замок подключены к заголовку страницы', () => {
  const src = js('views/request-header.js');
  assert.match(src, /buildTitleBlock\(/);
  assert.match(src, /resetRequestTitles\(\)/);
  assert.doesNotMatch(src, /\{ class: 'req__title' \}, d\.title \|\| firstLine\(d\.text\)/);
});
