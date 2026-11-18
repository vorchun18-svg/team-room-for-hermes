/**
 * section-collapse.test.mjs — сворачиваемые блоки страницы запроса (У-1, волна 21).
 *
 * Проверяем то, что видит Михаил:
 *   * у «Диалога», «Дерева работ», «Артефактов» и «Запроса» — один и тот же
 *     заголовок со стрелкой; он настоящая кнопка (клик и Enter/Space),
 *     `aria-expanded` и `aria-controls` на месте;
 *   * умолчания: диалог свёрнут всегда; дерево и артефакты развёрнуты у запроса
 *     в работе и свёрнуты у готового; выбор человека сильнее умолчаний и
 *     переживает перерисовку (состояние в localStorage по id запроса);
 *   * свёрнутый блок НЕ строит тяжёлое содержимое (дерево/артефакты) до раскрытия;
 *   * свёрнутый диалог получает метку «есть новый ответ», раскрытие её снимает,
 *     отправка сообщения разворачивает блок;
 *   * «развернуть всё» (печать) разворачивает блоки, не перезаписывая выбор.
 *
 * На коде 1.1.0 файла `web/js/views/section-collapse.js` нет — набор красный.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installMiniDom, resetMiniDom, byClass, byData, textOf, DOC } from './mini-dom.mjs';

installMiniDom();

const {
  createSection, sectionDefaults, treeSummaryChips, artifactsSummaryChips,
  treeProblems, artifactsSummary, expandAllSections, liveSections,
} = await import('../web/js/views/section-collapse.js');
const { createSectionState, sectionStateKey } = await import('../web/js/views/request-tree-state.js');
const { renderTree } = await import('../web/js/views/request-parts-tree.js');
const { renderArtifacts } = await import('../web/js/views/request-parts.js');
const { mountThread } = await import('../web/js/views/request-thread.js');

/* ─────────────────────────────────────────────────────────── помощники */

/** Своё хранилище «как localStorage» на один тест (без общего состояния). */
function memStore() {
  const map = new Map();
  return {
    map,
    get: (k) => (map.has(k) ? map.get(k) : null),
    set: (k, v) => { map.set(k, String(v)); },
    remove: (k) => { map.delete(k); },
  };
}

function storeFor(rid, store) {
  return createSectionState({ requestId: rid, store });
}

const chips = (card) => byClass(card, 'sec__chip').map((el) => textOf(el).trim());
const toggleOf = (card) => byClass(card, 'sec__toggle')[0];
const bodyOf = (card) => byClass(card, 'sec__body')[0];
const click = (el) => el.dispatch('click', { type: 'click', target: el });
const hiddenOf = (el) => el.hasAttribute('hidden');

const ctxStub = () => ({
  api: { get: async () => ({}), post: async () => ({ ok: true }) },
  toast() {},
  room: { id: 'cafe', members: [] },
});

/* ─────────────────────────────────────────────────────────── умолчания */

test('умолчания W25 (R5 §7.2): диалог, ход работы, дерево и артефакты свёрнуты на любом виде', () => {
  for (const status of ['working', 'needs_you', 'done', 'failed']) {
    const d = sectionDefaults({ status });
    assert.equal(d.thread, false, `${status}: диалог свёрнут`);
    assert.equal(d.worklog, false, `${status}: ход работы свёрнут`);
    assert.equal(d.tree, false, `${status}: дерево — строка со счётчиком`);
    assert.equal(d.artifacts, false, `${status}: артефакты — строка со счётчиком`);
    assert.equal(d.ask, false, `${status}: исходный запрос свёрнут`);
    assert.equal(d.timeline, false, `${status}: хронология — последние 5`);
  }
  // «Что изменилось» зависит от ширины вида и версии — проверяется в request-blocks.test.mjs.
});

test('умолчания W25: выбор человека сильнее умолчания (дерево, артефакты)', () => {
  // Прежнее умолчание W21 (развёрнуто у запроса в работе) заменено каноном R5 §7.2:
  // длина страницы на 1366 с сайдбаром Hermes перестала укладываться в ≤ 4 экрана.
  const d = sectionDefaults({ status: 'working' }, { layout: 'l' });
  assert.equal(d.tree, false);
  assert.equal(d.artifacts, false);
});

/* ─────────────────────────────────────────────────────────── заголовок */

test('заголовок блока — кнопка: aria-expanded, aria-controls, стрелка, краткая суть', () => {
  resetMiniDom();
  const host = DOC.createElement('div');
  DOC.body.appendChild(host);
  const store = storeFor('t_hdr', memStore());
  const sec = createSection({
    key: 'tree', requestId: 't_hdr', title: 'Дерево работ', defaultOpen: false, store,
    summary: [{ text: '18 из 18 готово', tone: 'ok' }, { text: 'есть ошибка', tone: 'danger' }],
    content: () => DOC.createElement('ul'),
  });
  host.appendChild(sec.element);

  const btn = toggleOf(sec.element);
  assert.equal(String(btn.tagName).toLowerCase(), 'button', 'заголовок — настоящая кнопка');
  assert.equal(btn.getAttribute('type'), 'button', 'кнопка не отправляет формы');
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  const body = bodyOf(sec.element);
  assert.equal(btn.getAttribute('aria-controls'), body.getAttribute('id'), 'aria-controls ведёт к содержимому');
  assert.ok(byClass(sec.element, 'sec__chev').length, 'стрелка есть');
  assert.deepEqual(chips(sec.element), ['18 из 18 готово', 'есть ошибка'], 'суть видна в заголовке');
  assert.ok(hiddenOf(body), 'свёрнутый блок скрыт');

  click(btn);
  assert.equal(btn.getAttribute('aria-expanded'), 'true', 'клик разворачивает');
  assert.ok(!hiddenOf(body), 'содержимое показано');
  click(btn);
  assert.equal(btn.getAttribute('aria-expanded'), 'false', 'повторный клик сворачивает');
  assert.ok(hiddenOf(body));
});

test('ленивая сборка: свёрнутый блок не строит тяжёлое до раскрытия', () => {
  resetMiniDom();
  let built = 0;
  const sec = createSection({
    key: 'tree', requestId: 't_lazy', title: 'Дерево работ', defaultOpen: false,
    store: storeFor('t_lazy', memStore()),
    content: () => { built += 1; return DOC.createElement('ul'); },
  });
  assert.equal(built, 0, 'пока свёрнут — содержимое не строилось');
  assert.equal(sec.isBuilt(), false);
  sec.expand();
  assert.equal(built, 1, 'при раскрытии построено один раз');
  sec.collapse();
  sec.expand();
  assert.equal(built, 1, 'повторное раскрытие не пересобирает (не мигает)');
  assert.equal(byClass(sec.element, 'sec__body')[0].childNodes.length, 1);
});

test('выбор человека сильнее умолчаний и переживает перерисовку страницы', () => {
  resetMiniDom();
  const store = storeFor('t_keep', memStore());
  // Запрос в работе: умолчание «развёрнуто».
  const first = createSection({
    key: 'tree', requestId: 't_keep', title: 'Дерево', defaultOpen: true, store,
    content: () => DOC.createElement('ul'),
  });
  assert.equal(first.isOpen(), true, 'умолчание применилось');

  click(toggleOf(first.element));            // человек свернул руками
  assert.equal(first.isOpen(), false);
  assert.deepEqual(store.snapshot(), { tree: false }, 'выбор записан по id запроса');

  // Страница перерисовалась (данные обновились) — блок собирается заново.
  const second = createSection({
    key: 'tree', requestId: 't_keep', title: 'Дерево', defaultOpen: true, store,
    content: () => DOC.createElement('ul'),
  });
  assert.equal(second.isOpen(), false, 'свёрнутое человеком остаётся свёрнутым');
  assert.equal(toggleOf(second.element).getAttribute('aria-expanded'), 'false');

  // Умолчание для другого блока того же запроса не задето.
  const thread = createSection({
    key: 'thread', requestId: 't_keep', title: 'Диалог', defaultOpen: false, store,
  });
  assert.equal(thread.isOpen(), false);
});

test('состояние: своё хранилище на запрос, битые данные не ломают страницу', () => {
  const store = memStore();
  const a = createSectionState({ requestId: 't_a', store });
  a.set('tree', false);
  assert.deepEqual(a.snapshot(), { tree: false });
  assert.equal(sectionStateKey('t_a').startsWith('teamroom.sections.'), true);

  const again = createSectionState({ requestId: 't_a', store });
  assert.equal(again.isOpen('tree', true), false, 'выбор прочитан из хранилища');
  assert.equal(again.isOpen('artifacts', true), true, 'нет выбора — действует умолчание');
  assert.equal(again.has('artifacts'), false);

  const other = createSectionState({ requestId: 't_b', store });
  assert.equal(other.isOpen('tree', true), true, 'у другого запроса своё состояние');

  store.set(sectionStateKey('t_a'), '{не json');
  const broken = createSectionState({ requestId: 't_a', store });
  assert.equal(broken.isOpen('tree', true), true, 'битые данные — просто умолчание');

  assert.equal(again.forget('tree'), true);
  assert.equal(again.isOpen('tree', true), true, 'забыли выбор — снова умолчание');
});

test('«развернуть всё» (печать) разворачивает блоки и не перезаписывает выбор', () => {
  resetMiniDom();
  const store = storeFor('t_all', memStore());
  const one = createSection({ key: 'tree', requestId: 't_all', title: 'Дерево', defaultOpen: false, store });
  const two = createSection({ key: 'artifacts', requestId: 't_all', title: 'Артефакты', defaultOpen: false, store });
  DOC.body.appendChild(one.element);
  DOC.body.appendChild(two.element);
  assert.equal(one.isOpen(), false);
  assert.equal(two.isOpen(), false);
  const live = liveSections();
  assert.ok(live.length >= 2, 'блоки зарегистрированы для печати');
  assert.ok(expandAllSections() >= 2);
  assert.equal(one.isOpen(), true, 'печать видит развёрнутое дерево');
  assert.equal(two.isOpen(), true);
  assert.deepEqual(store.snapshot(), {}, 'выбор человека не перезаписан печатью');
});

/* ───────────────────────────────────────────────────── краткая суть блоков */

test('дерево: «18 из 18 готово» + «есть ошибка» и «ждёт вашего ответа»', () => {
  const detail = {
    status: 'needs_you',
    progress: { done: 18, total: 18 },
    tree: [
      { id: 't1', status: 'done' },
      { id: 't2', status: 'failed', error: { code: 'exit', message: 'упал' } },
      { id: 't3', status: 'blocked', needs_human: true },
    ],
  };
  const texts = treeSummaryChips(detail).map((c) => c.text);
  assert.deepEqual(texts, ['18 из 18 готово', 'есть ошибка', 'ждёт вашего ответа']);
  assert.deepEqual(treeProblems(detail), { errors: 1, waiting: 1 });

  const done = treeSummaryChips({ status: 'done', progress: { done: 18, total: 18 }, tree: [{ id: 't', status: 'done' }] });
  assert.deepEqual(done.map((c) => c.text), ['18 из 18 готово']);
});

test('артефакты: число файлов (с папками) и «есть новее свода»', () => {
  const detail = {
    status: 'working',
    artifacts: [
      { path: 'C:\\Проекты\\Лето\\итог.md', group: 'final' },
      { kind: 'folder', title: 'Копии', items: [{ path: 'a.md' }, { path: 'b.md', newer_than_svod: true }] },
      { path: 'C:\\Проекты\\Лето\\смета.xlsx', group: 'material', newer_than_svod: true },
    ],
  };
  assert.deepEqual(artifactsSummary(detail), { total: 4, newer: true });
  assert.deepEqual(artifactsSummaryChips(detail).map((c) => c.text), ['4 файла', 'есть новее свода']);
  assert.deepEqual(artifactsSummaryChips({ artifacts: [] }), []);
});

test('краткая суть в заголовке: чип с подсказкой (обрезанный текст не теряется)', () => {
  resetMiniDom();
  const sec = createSection({
    key: 'artifacts', requestId: 't_chip', title: 'Артефакты', defaultOpen: false,
    store: storeFor('t_chip', memStore()),
    summary: [{ text: '23 файла', tone: 'muted' }, { text: 'есть новее свода', tone: 'info' }],
  });
  const first = byClass(sec.element, 'sec__chip')[0];
  assert.equal(textOf(first).trim(), '23 файла');
  assert.equal(first.getAttribute('title'), '23 файла', 'подсказка = сам текст');
});

test('перерисовка страницы не копит мёртвые блоки в списке печати', () => {
  resetMiniDom();
  const store = storeFor('t_leak', memStore());
  const count = () => liveSections().filter((s) => s.key === 'tree' && s.requestId === 't_leak').length;
  for (let i = 0; i < 5; i += 1) {
    createSection({ key: 'tree', requestId: 't_leak', title: 'Дерево', defaultOpen: false, store });
  }
  assert.equal(count(), 1, 'на блок — одна запись, сколько бы раз страница ни рисовалась');
});

/* ───────────────────────────────────────────────────── блоки страницы */

test('«Дерево работ» у готового запроса свёрнуто и строк не строит; раскрытие строит', () => {
  resetMiniDom();
  const detail = {
    id: 't_tree_collapse_' + Math.random().toString(36).slice(2, 8),
    room: 'cafe', status: 'done',
    progress: { done: 2, total: 2 },
    tree: [
      { id: 't_root', parent_ids: [], title: 'Письмо поставщику', role: 'root', status: 'done', depth: 0 },
      { id: 't_sub', parent_ids: ['t_root'], title: 'Собрать котировки', role: 'worker', status: 'done' },
    ],
  };
  const card = renderTree(detail, ctxStub(), {});
  assert.ok(byClass(card, 'sec__toggle').length, 'у дерева есть стрелка');
  assert.equal(byClass(card, 'rp-tree__list').length, 0, 'строки не строились в свёрнутом виде');
  assert.deepEqual(chips(card), ['2 из 2 готово'], 'суть видна в заголовке');

  click(toggleOf(card));
  assert.equal(byClass(card, 'rp-tree__list').length, 1, 'раскрыли — строки появились');
  assert.ok(byClass(card, 'rp-node').length >= 2, 'узлы дерева на месте');
});

test('«Артефакты» свёрнуты у запроса в любом статусе (R5 §7.2)', () => {
  resetMiniDom();
  const items = [
    { path: 'C:\Проекты\Лето\итоговое-письмо.md', title: 'Итоговое письмо', group: 'final', mtime: 1793160510 },
    { path: 'C:\Проекты\Лето\котировки.xlsx', title: 'Котировки', group: 'material', mtime: 1793160500 },
  ];
  const rid = (p) => p + Math.random().toString(36).slice(2, 8);
  const done = renderArtifacts({ id: rid('t_art_done_'), room: 'cafe', status: 'done', artifacts: items },
    ctxStub(), { projectDir: 'C:\Проекты\Лето' });
  assert.equal(done.getAttribute('data-section'), 'artifacts');
  assert.equal(byClass(done, 'rp-art').length, 0, 'строки не строились — ленивая сборка');
  assert.deepEqual(chips(done), ['2 файла']);
  click(toggleOf(done));
  assert.equal(byClass(done, 'rp-art').length, 2, 'после раскрытия — обе строки');

  const working = renderArtifacts({ id: rid('t_art_work_'), room: 'cafe', status: 'working', artifacts: items },
    ctxStub(), { projectDir: 'C:\Проекты\Лето' });
  assert.equal(toggleOf(working).getAttribute('aria-expanded'), 'false', 'в работе — тоже строка со счётчиком');
  assert.equal(chips(working).length, 1, 'счётчик виден в свёрнутой строке');
});

/* ─────────────────────────────────────────────────────────── диалог */

const now = Math.floor(Date.now() / 1000);
const THREAD = [
  { id: 'c1', at: now - 3600, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
    text: 'Добавьте срок подачи', kind: 'message', task_id: null },
  { id: 'c2', at: now - 1800, author_kind: 'agent', author: 'team-ops', author_label: 'Операции',
    text: 'Срок поставили, документ в папке проекта', kind: 'comment', task_id: 't_root' },
];

function mountThreadBlock(rid, detail) {
  const host = DOC.createElement('div');
  DOC.body.appendChild(host);
  return { host, ctl: mountThread(host, ctxStub(), { roomId: 'cafe', rid, detail }) };
}

test('диалог: по умолчанию свёрнут, в заголовке число сообщений', () => {
  resetMiniDom();
  const { ctl } = mountThreadBlock('t_thr_' + Math.random().toString(36).slice(2, 8),
    { id: 't_root', status: 'working', thread: THREAD });
  assert.equal(ctl.isOpen(), false, 'диалог свёрнут при открытии страницы');
  assert.equal(toggleOf(ctl.element).getAttribute('aria-expanded'), 'false');
  assert.deepEqual(chips(ctl.element), ['2 сообщения'], 'число сообщений видно в заголовке');
  assert.equal(byClass(ctl.element, 'thr-msg').length, 0, 'лента не построена, пока свёрнуто');

  ctl.expand();
  assert.equal(byClass(ctl.element, 'thr-msg').length, 2, 'раскрыли — лента на месте');
});

test('свёрнутый диалог получает метку «есть новый ответ», раскрытие её снимает', () => {
  resetMiniDom();
  const rid = 't_thr_unread_' + Math.random().toString(36).slice(2, 8);
  const { ctl } = mountThreadBlock(rid, { id: 't_root', status: 'working', thread: THREAD });
  assert.equal(ctl.isOpen(), false);

  // Пока человек не смотрит — руководитель ответил (опрос принёс новое сообщение).
  ctl.update({
    id: 't_root', status: 'working',
    thread: THREAD.concat([{
      id: 'c3', at: now, author_kind: 'agent', author: 'lead', author_label: 'Руководитель',
      text: 'Учтено: срок в письме, проверьте вложение', kind: 'comment', task_id: 't_root',
    }]),
  });
  assert.deepEqual(chips(ctl.element), ['3 сообщения', 'есть новый ответ'],
    'свёрнутый диалог не молчит о новом ответе');
  assert.ok(byClass(ctl.element, 'sec__dot').length, 'точка на заголовке');
  assert.equal(ctl.isOpen(), false, 'метка не раскрывает блок сама');

  // Своё сообщение — не «новый ответ»: считаем только ответы команды.
  ctl.update({
    id: 't_root', status: 'working',
    thread: THREAD.concat([{
      id: 'c9', at: now, author_kind: 'mikhail', author: 'mikhail', author_label: 'Михаил',
      text: 'А вложение где?', kind: 'message', task_id: null,
    }]).concat([{
      id: 'c3', at: now, author_kind: 'agent', author: 'lead', author_label: 'Руководитель',
      text: 'Учтено: срок в письме, проверьте вложение', kind: 'comment', task_id: 't_root',
    }]),
  });
  assert.deepEqual(chips(ctl.element), ['4 сообщения', 'есть новый ответ'], 'своё сообщение метку не растит');

  ctl.expand();
  assert.deepEqual(chips(ctl.element), ['4 сообщения'], 'раскрыли — метка снята');
});

test('отправка сообщения разворачивает свёрнутый диалог', () => {
  resetMiniDom();
  const { ctl } = mountThreadBlock('t_thr_send_' + Math.random().toString(36).slice(2, 8),
    { id: 't_root', status: 'working', thread: THREAD });
  ctl.expand();
  click(toggleOf(ctl.element));                       // человек свернул
  assert.equal(ctl.isOpen(), false);

  const field = byClass(ctl.element, 'thr__input')[0];
  assert.ok(field, 'поле уже построено (блок раскрывали)');
  field.value = 'Проверьте, пожалуйста, вложение';
  click(byClass(ctl.element, 'thr__send')[0]);
  assert.equal(ctl.isOpen(), true, 'отправка показала диалог человеку');
});
