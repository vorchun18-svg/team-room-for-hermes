/**
 * aside-p3.test.mjs — правая колонка запроса в волне 17 (пакет D).
 *
 * Что проверяем (требования аудита B1, B3, B13, B15, P3 и W17-D):
 *  - «Нужен ваш ответ (N)» идёт первым и раскрыт, «Шаги», «Решения», «Будущие
 *    вопросы», «Отвечено» — свёрнуты до заголовка со счётчиком; состояние
 *    запоминается (aside-fold.js), а не сбрасывается при перерисовке;
 *  - счётчики колонки и вкладки «Дальше» считаются ОДНОЙ функцией
 *    (inboxSummary/blockedCards) — одно число и одна расшифровка (B13);
 *  - кнопка «Отправить руководителю накопленные ответы» есть только при
 *    `answers.summary.can_resend` и шлёт `answers_resend` (W17-B §3);
 *  - «Напомнить руководителю» на 409 с найденной реакцией показывает её у
 *    вопроса и убирает кнопку — без противоречащей подписи (B-12);
 *  - у списков и прокручиваемых контейнеров есть `data-key`/`data-scroll-key`
 *    (WAVE17 §1: прокрутка и узлы переживают точечное обновление);
 *  - шаг — одна строка: иконка статуса, название с обрезкой в 2 строки и
 *    подсказкой, исполнитель и срок; в CSS нет сетки 15 px (B1);
 *  - таблицы «Свода» лежат в обёртке с прокруткой и тенью-подсказкой, ячейки
 *    переносят слова (B3).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { createAside } from '../web/js/views/request-aside.js';
import { createSteps } from '../web/js/views/request-steps.js';
import { wrapTables } from '../web/js/views/request-util.js';
import { FOLD_STORE, foldState, setFoldState } from '../web/js/views/aside-fold.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const css = (name) => readFileSync(join(HERE, '..', 'web', 'css', name), 'utf8');

/* ── мини-DOM: то, чего нет в fake-dom.mjs ─────────────────────────────── */

function installHelpers() {
  const P = FakeElement.prototype;
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (!('childElementCount' in P)) {
    Object.defineProperty(P, 'childElementCount', {
      configurable: true,
      get() { return (this.childNodes || []).filter((n) => n.nodeType === 1).length; },
    });
  }
  if (typeof P.querySelector !== 'function') {
    P.querySelector = function querySelector(selector) {
      const sel = String(selector);
      const found = sel.startsWith('.') ? byClass(this, sel.slice(1))
        : findAll(this, (n) => String(n.localName) === sel.toLowerCase());
      return found.length ? found[0] : null;
    };
  }
  if (typeof P.querySelectorAll !== 'function') {
    P.querySelectorAll = function querySelectorAll(selector) {
      const sel = String(selector);
      return sel.startsWith('.') ? byClass(this, sel.slice(1))
        : findAll(this, (n) => String(n.localName) === sel.toLowerCase());
    };
  }
  if (typeof P.closest !== 'function') {
    P.closest = function closest(selector) {
      const cls = String(selector).replace(/^\./, '');
      let cur = this;
      while (cur && cur.nodeType === 1) {
        if (String(cur.getAttribute('class') || '').split(/\s+/).includes(cls)) return cur;
        cur = cur.parentNode;
      }
      return null;
    };
  }
  if (typeof P.insertBefore !== 'function') {
    P.insertBefore = function insertBefore(node, ref) {
      const idx = ref ? this.childNodes.indexOf(ref) : -1;
      node.parentNode = this;
      if (idx >= 0) this.childNodes.splice(idx, 0, node);
      else this.childNodes.push(node);
      return node;
    };
  }
  if (typeof P.removeChild !== 'function') {
    P.removeChild = function removeChild(node) {
      const i = this.childNodes.indexOf(node);
      if (i >= 0) this.childNodes.splice(i, 1);
      node.parentNode = null;
      return node;
    };
  }
}

function findAll(root, pred) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && pred(n)) out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

function byClass(root, cls) {
  return findAll(root, (n) => String(n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

/** Простое хранилище вместо localStorage (в node его нет). */
function installStorage() {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
  };
  return store;
}

function clearStorage() {
  try { globalThis.localStorage.removeItem(FOLD_STORE); } catch { /* ignore */ }
}

/* ── данные: длинный запрос, как в fixtures/request_long.json ───────────── */

const QUESTION_LONG = 'Противоречие по штрафам: оставляем 0,3% в день с ограничением 10% от суммы '
  + 'партии (как в приложении Б) или 0,1% в день без ограничения (как в разделе 7.3)? '
  + 'Первый вариант выгоднее нам при короткой просрочке, второй — при длинной; '
  + 'юрист говорит, что суд будет толковать противоречие против того, кто готовил договор, '
  + 'а договор готовил поставщик — проверьте формулировки в разделе 7.3 и приложении Б.';

function makeDetail(over = {}) {
  return Object.assign({
    id: 't_root1',
    next_steps: [
      {
        id: 'ns_1', owner: 'biz-cfo', due: '2026-11-10', state: 'open',
        text: 'Свести противоречие по штрафам: раздел 7.3 говорит 0,1% в день, приложение Б — '
          + '0,3% в день и не более 10% от суммы партии. Нужно одно правило и явная ссылка '
          + 'из приложения в тело договора, иначе спор уйдёт в толкование против нас.',
      },
      {
        id: 'ns_2', owner: 'biz-ops', due: '2026-11-12', state: 'asked',
        text: 'Проверить приложение с спецификацией партий: объёмы, даты, единицы измерения '
          + '(штуки против коробов), кратность упаковки и вес — в текущей версии единицы не совпадают.',
      },
      { id: 'ns_3', owner: 'biz-lawyer', state: 'done', text: 'Проверить историю правок договора от 04.11' },
    ],
    open_questions: [
      { id: 'q_1', text: QUESTION_LONG, for: 'mikhail', answered: false, answer: null, answer_state: 'open' },
      {
        id: 'q_2', text: 'Скрытый брак: ставим срок претензии 30 дней или 15?', for: 'mikhail',
        answered: true, answered_at: 5, answer: 'Ставим 30 дней', answer_state: 'sent',
        leader_pending: true, can_remind: true, reaction_overdue: true, reaction_waited_s: 1800,
        reaction: null,
      },
      {
        id: 'q_3', text: 'Курс: расширяем коридор до 4,90–5,80?', for: 'mikhail',
        answered: true, answered_at: 7, answer: 'Да, расширяем', answer_state: 'applied',
        reaction: { text: 'Принято: коридор расширен', at: 8, cards: ['t_2f6c1b90'] },
        answer_changes_done: true,
      },
      { id: 'q_4', text: 'Когда платим аванс?', for: 'mikhail', answered: false, answer: null, answer_state: 'open' },
    ],
    decisions: [
      { id: 'd_1', text: 'Штраф 0,3% в день', why: 'ограничение 10%', status: 'accepted' },
      { id: 'd_2', text: 'Уменьшить партию на 20% без санкций', why: 'спрос непредсказуем', status: 'proposed' },
    ],
    future_questions: [
      'Стоит ли включить в договор право на аудит производства поставщика раз в год?',
      'Нужен ли отдельный договор на хранение упаковки на складе поставщика?',
    ],
    tree: [
      { id: 't_root1', status: 'done' },
      { id: 't_2f6c1b93', status: 'blocked', title: 'Проверка спецификации партий', summary_md: 'Не сходятся единицы' },
    ],
    answers: { summary: { can_resend: true, resend_pending: 1, resend_label: 'Отправить руководителю накопленные ответы' } },
  }, over || {});
}

function makeStand(opts = {}) {
  const doc = installFakeDom();
  installHelpers();
  doc.body = new FakeElement('body');
  const posted = [];
  const toasts = [];
  const detail = makeDetail(opts.detail);
  const state = { host: null, renders: 0 };
  const ctx = {
    api: {
      async post(path, body) {
        posted.push({ path, body });
        if (opts.failWith) throw opts.failWith;
        return opts.response ? opts.response(body) : { sent: 1 };
      },
      async get() { return {}; },
    },
    toast: (message, kind) => toasts.push({ message, kind }),
    room: { id: 'cafe', members: [{ profile: 'biz-cfo', role: 'worker' }] },
    navigate() {},
  };
  const R = {
    ctx, rid: 't_root1', keepValues: new Map(), roomId: 'cafe',
    get detail() { return detail; },
    get backlog() { return new Map(); },
    async refresh() {},
    // Как в приложении: перерисовка пересобирает содержимое на месте.
    render() {
      state.renders += 1;
      if (!state.host) return;
      state.host.childNodes = [];
      state.host.appendChild(buildAside());
    },
  };
  const { buildAside } = createAside(R);
  const host = doc.createElement('div');
  host.appendChild(buildAside());
  state.host = host;
  return { host, posted, toasts, detail, doc, buildAside, renders: () => state.renders };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/* ──────────────────────────────────────────────────── свёрнутые блоки (P3) */

test('«Нужен ваш ответ» первым и раскрыт; остальные блоки свёрнуты со счётчиками', () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const aside = byClass(s.host, 'req__aside')[0];
    assert.ok(aside, 'колонка отрисована');
    const first = Array.from(aside.childNodes).filter((n) => n.nodeType === 1)
      .find((n) => !String(n.getAttribute('class') || '').includes('req-aside__scope'));
    assert.ok(String(first.getAttribute('class')).includes('req-side--pin'),
      'первый блок — закреплённый «Нужен ваш ответ»');
    assert.match(text(first), /Нужен ваш ответ/);

    const folds = byClass(aside, 'req-side--fold');
    const names = folds.map((f) => f.getAttribute('data-fold'));
    assert.deepEqual(names.sort(), ['answered', 'decisions', 'future', 'steps'],
      'шаги, решения, будущие вопросы и отвеченное — свёрнутые блоки');
    for (const fold of folds) {
      assert.equal(String(fold.localName), 'details');
      assert.equal(fold.getAttribute('open'), null, fold.getAttribute('data-fold') + ': свёрнут по умолчанию');
      assert.ok(byClass(fold, 'req-side__fold-count').length, 'счётчик в заголовке есть');
    }
  } finally {
    uninstallFakeDom();
  }
});

test('состояние свёрнутости запоминается между сборками', () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    setFoldState('t_root1', 'steps', true);
    setFoldState('t_root1', 'decisions', false);
    const host = s.doc.createElement('div');
    host.appendChild(s.buildAside());
    const steps = byClass(host, 'req-side--fold').find((f) => f.getAttribute('data-fold') === 'steps');
    assert.ok(steps.getAttribute('open') !== null, 'шаги раскрыты — так оставил пользователь');
    const decisions = byClass(host, 'req-side--fold').find((f) => f.getAttribute('data-fold') === 'decisions');
    assert.equal(decisions.getAttribute('open'), null, 'решения свёрнуты');
    assert.equal(foldState('t_root1', 'steps'), true);
    assert.equal(foldState('t_root1', 'unknown', true), true, 'неизвестный блок — по умолчанию');
    // другой запрос — своё состояние
    assert.equal(foldState('t_other', 'steps'), false);
  } finally {
    uninstallFakeDom();
  }
});

/* ────────────────────────────────────────── счётчики: один источник (B13) */

test('счётчик «Нужен ваш ответ» = вопросы + решения + блокировки, с расшифровкой', () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const pin = byClass(s.host, 'req-side--pin')[0];
    const head = text(pin);
    // 2 открытых вопроса (q_1, q_4) + 1 предложенное решение (d_2) + 1 блокировка = 4
    assert.match(head, /Нужен ваш ответ/);
    assert.match(head, /4/, 'общее число дел');
    assert.match(head, /2 вопроса · 1 блокировка · 1 решение/,
      'та же расшифровка, что у вкладки «Дальше» (inboxSummary)');
    // предложенное решение видно в списке, а не только в числе
    assert.match(head, /Уменьшить партию на 20%/);
    assert.match(head, /ждёт вашего решения — разобрать в «Дальше»/);
  } finally {
    uninstallFakeDom();
  }
});

test('числа колонки и вкладки «Дальше» сходятся на одних данных', async () => {
  const { inboxSummary, blockedCards } = await import('../web/js/views/backlog-model.js');
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const d = s.detail;
    const openQs = d.open_questions.filter((q) => !q.answered);
    const proposed = d.decisions.filter((x) => x.status === 'proposed');
    const blocked = blockedCards([d]);
    const inbox = inboxSummary({ questions: openQs, decisions: proposed, blocked });
    const pin = byClass(s.host, 'req-side--pin')[0];
    assert.ok(text(pin).includes(String(inbox.total)), 'число совпадает с inboxSummary');
    assert.ok(text(pin).includes(inbox.text), 'расшифровка совпадает слово в слово');
  } finally {
    uninstallFakeDom();
  }
});

/* ───────────────────────────────────── досылка ответов (answers_resend) */

test('кнопка досылки ответов: только при can_resend, шлёт answers_resend', async () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const btn = byClass(s.host, 'req-side__resend')[0];
    assert.ok(btn, 'кнопка есть, когда есть ответы без реакции');
    assert.match(text(btn), /Отправить руководителю накопленные ответы/);
    assert.match(text(byClass(s.host, 'req-side__resend-row')[0]), /1 ответ без реакции/);
    btn.dispatch('click');
    await tick();
    await tick();
    assert.deepEqual(s.posted.map((p) => p.body), [{ action: 'answers_resend' }]);
    assert.match(s.toasts[0].message, /Руководителю отправлено одной карточкой: 1 ответ/);
  } finally {
    uninstallFakeDom();
  }
});

test('нет ответов без реакции — кнопки досылки нет', () => {
  installStorage();
  clearStorage();
  const s = makeStand({
    detail: { answers: { summary: { can_resend: false, resend_pending: 0 } } },
  });
  try {
    assert.equal(byClass(s.host, 'req-side__resend').length, 0, 'кнопки нет');
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────────── 409 с реакцией руководителя (B-12) */

test('«Напомнить» вернул 409 с реакцией: она показана, кнопки нет, подписи не спорят', async () => {
  installStorage();
  clearStorage();
  const reaction = { text: 'Принято: срок прейтензии 30 дней, юрист вносит правку', at: 9, cards: [], status: 'received' };
  const err = new Error('Руководитель уже ответил на этот вопрос');
  err.code = 'conflict';
  err.status = 409;
  err.details = { error: { code: 'conflict', message: err.message, detail: { question_id: 'q_2', reaction } } };
  const s = makeStand({ failWith: err });
  try {
    const row = byClass(s.host, 'req-q').find((r) => r.getAttribute('data-q') === 'q_2');
    assert.ok(row, 'строка вопроса с ответом без реакции');
    const remind = byClass(row, 'req-q__remind')[0];
    assert.ok(remind, 'кнопка «Напомнить» до клика есть');
    remind.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.renders(), 1, 'страница перерисована с реакцией');
    const after = byClass(byClass(s.host, 'req-q').find((r) => r.getAttribute('data-q') === 'q_2'), 'req-q__reaction')[0];
    assert.ok(after, 'реакция видна в окне вопроса (а не только в тосте)');
    assert.match(text(after), /Принято: срок прейтензии 30 дней/);
    assert.match(s.toasts[s.toasts.length - 1].message, /Руководитель уже ответил/);
    assert.equal(String(s.detail.open_questions[1].answer_label || ''), 'принято руководителем');
    assert.equal(s.detail.open_questions[1].can_remind, false, 'напоминать больше не о чем');
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────── ключи прокрутки и перерисовки (WAVE17 §1) */

test('data-scroll-key на колонке и списках, data-key у вопросов и решений', () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const aside = byClass(s.host, 'req__aside')[0];
    assert.equal(aside.getAttribute('data-scroll-key'), 'request-aside');
    for (const key of ['questions-open', 'questions-answered', 'decisions', 'future', 'steps-next']) {
      assert.ok(findAll(s.host, (n) => n.getAttribute && n.getAttribute('data-scroll-key') === key).length,
        'есть ключ прокрутки ' + key);
    }
    assert.equal(aside.getAttribute('data-keep'), null, 'черновики — только у полей');
    const q1 = byClass(s.host, 'req-q').find((r) => r.getAttribute('data-q') === 'q_1');
    assert.ok(q1.getAttribute('data-key'), 'у строки вопроса есть data-key');
    assert.equal(q1.getAttribute('data-mic-anchor'), 'q:q_1');
    assert.ok(findAll(s.host, (n) => n.getAttribute && String(n.getAttribute('data-key')).startsWith('dec')).length,
      'у решения есть data-key');
    const field = byClass(q1, 'req-q__input')[0];
    assert.equal(field.getAttribute('data-keep'), 'q:q_1', 'черновик переживает обновление');
  } finally {
    uninstallFakeDom();
  }
});

/* ────────────────────────────────────────────── шаг — одна строка (B1) */

test('шаг: иконка статуса, название с подсказкой, исполнитель и срок в строке', () => {
  installStorage();
  clearStorage();
  const s = makeStand();
  try {
    const row = byClass(s.host, 'req-step').find((r) => r.getAttribute('data-step') === 'ns_1');
    assert.ok(row, 'строка шага');
    assert.equal(row.getAttribute('data-key'), 'ns_1', 'ключ перерисовки');
    const open = byClass(row, 'req-step__open')[0];
    assert.equal(open.getAttribute('role'), 'button');
    const status = byClass(open, 'req-step__status')[0];
    assert.ok(status, 'иконка статуса');
    assert.match(String(status.getAttribute('title')), /предстоит/);
    const t = byClass(open, 'req-step__text')[0];
    assert.match(String(t.getAttribute('title')), /Свести противоречие по штрафам/,
      'полный текст — в подсказке (обрезка в 2 строки)');
    const meta = byClass(open, 'req-step__meta')[0];
    const owner = byClass(meta, 'req-step__owner')[0];
    assert.ok(owner, 'исполнитель в строке шага');
    assert.match(String(owner.getAttribute('title')), /Исполнитель/, 'подпись исполнителя (title)');
    assert.ok(byClass(meta, 'due-chip').length, 'срок в строке шага');
    // Порядок колонок: статус, текст, исполнитель со сроком — не 15 px под чекбокс.
    const kids = open.childNodes.filter((n) => n.nodeType === 1);
    assert.deepEqual(kids.map((n) => String(n.getAttribute('class')).split(' ')[0]),
      ['req-step__status', 'req-step__text', 'req-step__meta']);
  } finally {
    uninstallFakeDom();
  }
});

test('B1 не вернётся: нет сетки 15 px и стили шагов подключены на странице', () => {
  const request = css('view-request.css');
  assert.ok(!/grid-template-columns:\s*15px/.test(request), 'сетка 15 px убрана из view-request.css');
  assert.ok(!/\.req-step__check\b/.test(request), 'правил под чекбокс больше нет');
  const steps = css('view-steps.css');
  assert.match(steps, /\.req-steps \.req-step__open\s*\{[^}]*grid-template-areas:\s*"status text meta"/s,
    'строка шага — три области: статус, текст, исполнитель');
  assert.match(steps, /-webkit-line-clamp:\s*2/, 'название обрезается двумя строками');
  const js = readFileSync(join(HERE, '..', 'web', 'js', 'views', 'request-steps.js'), 'utf8');
  assert.match(js, /ensureCss\('css\/view-steps\.css'\)/,
    'блок сам подключает свои стили (без этого сетка 15 px оставалась в силе)');
});

/* ──────────────────────────── таблицы «Свода» не вылезают (B3) */

test('таблица Свода: обёртка с прокруткой, ключом и тенью-подсказкой', () => {
  installStorage();
  const doc = installFakeDom();
  installHelpers();
  try {
    const host = doc.createElement('div');
    const body = doc.createElement('div');
    body.appendChild(doc.createElement('p'));
    const table = doc.createElement('table');
    body.appendChild(table);
    host.appendChild(body);
    wrapTables(host);
    const wrap = byClass(body, 'req-table')[0];
    assert.ok(wrap, 'обёртка появилась');
    assert.equal(wrap.getAttribute('data-edge'), 'none', 'подсказка появляется только при переполнении');
    assert.match(String(wrap.getAttribute('title')), /прокрутите/);
    const scroll = byClass(wrap, 'req-table__scroll')[0];
    assert.ok(scroll, 'внутри отдельный прокручиваемый контейнер');
    assert.equal(scroll.getAttribute('data-scroll-key'), 'answer-table-1',
      'горизонтальная позиция переживёт перерисовку');
    assert.ok(byClass(scroll, 'md-table').length === 0);
    assert.ok(findAll(scroll, (n) => String(n.localName) === 'table').length === 1);
  } finally {
    uninstallFakeDom();
  }
});

test('B3 не вернётся: ячейки переносят слова, кода-новостей нет nowrap', () => {
  const request = css('view-request.css');
  assert.ok(!/\.req-table th[^{]*\{[^}]*white-space:\s*nowrap/s.test(request),
    'nowrap в ячейках таблицы убран');
  assert.match(request, /\.req-table__scroll\s*\{[^}]*overflow-x:\s*auto/s);
  assert.match(request, /\.req-table\[data-edge='end'\]::after/, 'тень справа по факту переполнения');
  assert.match(request, /overflow-wrap:\s*anywhere/, 'длинные пути рвутся');
});

/* ──────────────────────────────── пустое состояние и «карта проекта» */

test('нечего ждать: честная строка вместо пустых блоков', () => {
  installStorage();
  clearStorage();
  const s = makeStand({
    detail: {
      open_questions: [], decisions: [], future_questions: [], next_steps: [],
      tree: [{ id: 't_root1', status: 'done' }],
      answers: { summary: { can_resend: false } },
    },
  });
  try {
    const pin = byClass(s.host, 'req-side--pin')[0];
    assert.match(text(pin), /Сейчас от вас ничего не нужно/);
    assert.equal(byClass(s.host, 'req-side--fold').length, 0, 'пустых свёрнутых блоков нет');
  } finally {
    uninstallFakeDom();
  }
});
