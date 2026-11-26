/**
 * questions-ui.test.mjs — колонка вопросов на странице запроса (W15-C, п. 3–6):
 *
 *  1) панель диктовки привязана к СВОЕМУ вопросу и не прыгает в левый верхний
 *     угол, когда страница перерисовалась (кнопка/поле заменены, узел выпал
 *     из документа) — `panelPlacement` + `createPanel` на мини-DOM;
 *  2) строка вопроса переживает перерисовку: тот же узел поля и микрофона,
 *     набранный текст и привязка `data-mic-anchor` на месте;
 *  3) ответ показывается рядом с вопросом как «Вопрос — ваш ответ», со статусом
 *     (`answer_label`), реакцией руководителя и кнопкой «Напомнить руководителю»;
 *  4) у открытого вопроса — отдельные кнопки «Отправить» и «Мне нужно пояснение»
 *     (единый стиль ввода, как в composer: поле + отдельные кнопки).
 *
 * DOM — tests/fake-dom.mjs, браузер не нужен. Живая проверка положения панели —
 * tools/e2e_questions.py (Chromium через CDP).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, FakeElement } from './fake-dom.mjs';
import { createAside } from '../web/js/views/request-aside.js';
import { createPanel, panelPlacement, anchorKeyOf } from '../web/js/dictation-panel.js';

/* ────────────────────────────────────────────── доработка мини-DOM */

function patchDom() {
  const P = FakeElement.prototype;
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (typeof P.replaceChildren !== 'function') {
    P.replaceChildren = function replaceChildren(...nodes) {
      this.childNodes = [];
      for (const node of nodes) if (node) this.appendChild(node);
    };
  }
  if (typeof P.replaceChild !== 'function') {
    P.replaceChild = function replaceChild(node, old) {
      const i = this.childNodes.indexOf(old);
      if (i === -1) return old;
      node.parentNode = this;
      old.parentNode = null;
      this.childNodes[i] = node;
      return node;
    };
  }
  if (typeof P.removeChild !== 'function') {
    P.removeChild = function removeChild(node) {
      const i = this.childNodes.indexOf(node);
      if (i !== -1) { this.childNodes.splice(i, 1); node.parentNode = null; }
      return node;
    };
  }
  if (!('childElementCount' in P)) {
    Object.defineProperty(P, 'childElementCount', {
      configurable: true,
      get() { return (this.childNodes || []).filter((n) => n.nodeType === 1).length; },
    });
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
}

function all(root, cls) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && String(n.getAttribute('class') || '').split(/\s+/).includes(cls)) out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

function byAttr(root, name, value) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1 && n.getAttribute && n.getAttribute(name) === value) out.push(n);
    for (const c of n.childNodes || []) walk(c);
  };
  walk(root);
  return out;
}

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

/* ─────────────────────────────── 1. математика места панели */

test('панель встаёт над кнопкой своего вопроса, а не в угол', () => {
  const box = panelPlacement(
    { left: 800, right: 1000, top: 400, bottom: 440 },
    { width: 1280, height: 900 }, 150,
  );
  assert.equal(box.width, 300, 'ширина панели');
  assert.equal(box.left, 700, 'правый край панели по правому краю кнопки');
  assert.equal(box.top, 242, 'над кнопкой с отступом 8 px');
  assert.ok(box.left > 8 && box.top > 8, 'не левый верхний угол');
});

test('сверху нет места — панель под кнопкой; за края окна не вылезает', () => {
  const under = panelPlacement(
    { left: 800, right: 1000, top: 20, bottom: 60 },
    { width: 1280, height: 900 }, 200,
  );
  assert.equal(under.top, 68, 'под кнопкой');
  assert.ok(under.left + under.width <= 1280 - 8, 'внутри окна по горизонтали');

  const narrow = panelPlacement(
    { left: 20, right: 60, top: 500, bottom: 540 },
    { width: 320, height: 600 }, 150,
  );
  assert.equal(narrow.width, 300, 'панель не шире окна минус отступы');
  assert.ok(narrow.left >= 8, 'левый край не отрицательный');
  assert.ok(narrow.top >= 8, 'верхний край не отрицательный');
});

test('ключ привязки берётся у ближайшего блока с data-mic-anchor', () => {
  installFakeDom();
  try {
    const row = globalThis.document.createElement('div');
    row.setAttribute('data-mic-anchor', 'q:q_7decf20e');
    const field = globalThis.document.createElement('div');
    const btn = globalThis.document.createElement('button');
    field.appendChild(btn);
    row.appendChild(field);
    assert.equal(anchorKeyOf(btn), 'q:q_7decf20e');
    assert.equal(anchorKeyOf(globalThis.document.createElement('span')), '');
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────── 2. панель и перерисовка страницы */

/** Мини-стенд панели: fake DOM с body, querySelector по data-mic-anchor. */
function panelStand() {
  installFakeDom();
  patchDom();
  const body = globalThis.document.createElement('div');
  const doc = globalThis.document;
  doc.body = body;
  doc.querySelector = (selector) => {
    const m = /^\[data-mic-anchor="(.+)"\]$/.exec(String(selector));
    if (!m) return null;
    const found = byAttr(body, 'data-mic-anchor', m[1].replace(/\\(.)/g, '$1'));
    return found.length ? found[0] : null;
  };
  globalThis.innerWidth = 1280;
  globalThis.innerHeight = 900;
  return { body, doc };
}

test('перерисовка списка вопросов: панель уходит к СВОЕМУ вопросу, а не в угол', () => {
  const { body, doc } = panelStand();
  try {
    const row = doc.createElement('div');
    row.setAttribute('data-mic-anchor', 'q:q_1');
    // Панель держится у БЛОКА вопроса (кнопка внутри — её место внизу строки).
    row.getBoundingClientRect = () => ({ left: 800, right: 1000, top: 400, bottom: 530, width: 200, height: 130 });
    const btn = doc.createElement('button');
    btn.setAttribute('data-mic-btn', 'true');
    btn.getBoundingClientRect = () => ({ left: 800, right: 1000, top: 470, bottom: 500, width: 200, height: 30 });
    row.appendChild(btn);
    body.appendChild(row);

    const panel = createPanel({});
    panel.open(btn);
    const left0 = panel.el.style.left;
    const top0 = panel.el.style.top;
    assert.ok(left0 && top0, 'панель получила место');
    assert.ok(Number(String(left0).replace('px', '')) > 8, 'не в левом краю');

    // Перерисовка: узел кнопки выпал из документа (прямоугольник нулевой),
    // в списке появился новый блок того же вопроса.
    body.removeChild(row);

    const fresh = doc.createElement('div');
    fresh.setAttribute('data-mic-anchor', 'q:q_1');
    fresh.getBoundingClientRect = () => ({ left: 600, right: 780, top: 300, bottom: 430, width: 180, height: 130 });
    const freshBtn = doc.createElement('button');
    freshBtn.setAttribute('data-mic-btn', 'true');
    freshBtn.getBoundingClientRect = () => ({ left: 600, right: 780, top: 370, bottom: 400, width: 180, height: 30 });
    fresh.appendChild(freshBtn);
    body.appendChild(fresh);

    // Старый блок вне документа — привязка должна найти живой блок вопроса.
    row.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
    panel.place();
    assert.equal(panel.el.style.left, '480px', 'прижата к живому блоку вопроса');
    assert.equal(panel.el.style.top, '142px', 'над ним');

    // Блока вопроса нет вовсе — панель остаётся на последнем верном месте.
    body.removeChild(fresh);
    panel.place();
    assert.equal(panel.el.style.left, '480px', 'место не сброшено');
    assert.equal(panel.el.style.top, '142px', 'в угол не прыгнули');
    panel.close();
  } finally {
    uninstallFakeDom();
  }
});

test('панель не привязана ни к чему: показывается у нижнего края, не в углу', () => {
  const { doc } = panelStand();
  try {
    const free = doc.createElement('button');
    free.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
    const panel = createPanel({});
    panel.open(free);
    assert.notEqual(panel.el.style.left, '8px', 'не прилипла к левому краю');
    assert.ok(Number(String(panel.el.style.top).replace('px', '')) > 100, 'не в верхнем углу');
    panel.close();
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────── 3. вопросы и ответы в колонке */

const ANSWERS = [
  {
    id: 'q_2',
    text: 'Добавляем ли в команду биз-финансиста (team-cfo)?',
    for: 'mikhail',
    answered: true,
    answer: 'Нет, никакого финансиста не добавляем.',
    answered_at: Math.floor(Date.now() / 1000) - 3600,
    answer_state: 'applied',
    answer_label: 'учтено: работа пересмотрена',
    leader_pending: false,
    can_remind: false,
    answer_changes_done: true,
    reaction: {
      text: 'Принято: team-cfo не добавляем, шаг по составу команды снят.',
      at: Math.floor(Date.now() / 1000) - 3500,
      status: 'applied',
      cards: ['t_643c9d89'],
    },
  },
  {
    id: 'q_3',
    text: 'Публиковать прайс до регистрации бренда?',
    for: 'mikhail',
    answered: true,
    answer: 'Да, публикуйте.',
    answered_at: Math.floor(Date.now() / 1000) - 400,
    answer_state: 'sent',
    answer_label: 'ответ отправлен — руководитель ещё не ответил',
    leader_pending: true,
    can_remind: true,
    reaction_overdue: true,
    reaction_waited_s: 400,
    reaction: null,
  },
  {
    id: 'q_1',
    text: 'Класс 30 или 30+43?',
    for: 'mikhail',
    answered: false,
    answer: null,
    answered_at: null,
    answer_state: 'open',
  },
];

const DETAIL = {
  id: 't_root1',
  next_steps: [],
  open_questions: ANSWERS,
  decisions: [],
  future_questions: [],
};

function asideStand() {
  installFakeDom();
  patchDom();
  const posted = [];
  const toasts = [];
  const detail = JSON.parse(JSON.stringify(DETAIL));
  const ctx = {
    api: {
      async post(path, body) { posted.push({ path, body }); return {}; },
      async get() { return {}; },
    },
    toast: (message, kind) => toasts.push({ message, kind }),
    room: { id: 'cafe', members: [] },
    navigate() {},
  };
  const R = {
    ctx, rid: 't_root1', keepValues: new Map(), roomId: 'cafe',
    get detail() { return detail; },
    get backlog() { return new Map(); },
    async refresh() {},
  };
  const { buildAside } = createAside(R);
  return { posted, toasts, detail, R, buildAside };
}

test('ответ рядом с вопросом: «Вопрос — ваш ответ», статус, реакция, карточки', () => {
  const s = asideStand();
  try {
    const host = globalThis.document.createElement('div');
    host.appendChild(s.buildAside());
    const rows = all(host, 'req-q');
    assert.equal(rows.length, 3, 'все вопросы на месте');

    const answered = rows.find((r) => r.getAttribute('data-q') === 'q_2');
    assert.ok(answered, 'строка отвеченного вопроса');
    const txt = text(answered);
    assert.match(txt, /Вопрос — ваш ответ/, 'пара показана явно');
    assert.match(txt, /Нет, никакого финансиста не добавляем/, 'виден ответ');
    assert.match(txt, /учтено: работа пересмотрена/, 'виден статус ответа');
    assert.match(txt, /Принято: team-cfo не добавляем/, 'видна реакция руководителя');
    assert.equal(all(answered, 'req-q__card').length, 1, 'карточка из реакции');
    assert.match(txt, /ответ изменил уже сделанное/, 'видно, что работа пересматривается');
    assert.equal(all(answered, 'req-q__form').length, 0, 'отвеченный вопрос не просит ответа снова');
  } finally {
    uninstallFakeDom();
  }
});

test('нет реакции руководителя: подпись и кнопка «Напомнить руководителю»', async () => {
  const s = asideStand();
  try {
    const host = globalThis.document.createElement('div');
    host.appendChild(s.buildAside());
    const row = all(host, 'req-q').find((r) => r.getAttribute('data-q') === 'q_3');
    const txt = text(row);
    assert.match(txt, /руководитель ещё не ответил/, 'честная подпись');
    const remind = all(row, 'req-q__remind')[0];
    assert.ok(remind, 'кнопка напоминания есть');
    assert.match(text(remind), /Напомнить руководителю/);
    remind.dispatch('click');
    await tick();
    await tick();
    assert.deepEqual(s.posted.map((p) => p.body), [{ action: 'answer_remind', question_id: 'q_3' }],
      'ушло действие answer_remind');
  } finally {
    uninstallFakeDom();
  }
});

test('открытый вопрос: поле + отдельные «Отправить» и «Пояснить…» (P3)', async () => {
  const s = asideStand();
  try {
    const host = globalThis.document.createElement('div');
    host.appendChild(s.buildAside());
    const row = all(host, 'req-q').find((r) => r.getAttribute('data-q') === 'q_1');
    assert.ok(row, 'строка открытого вопроса');
    const field = all(row, 'req-q__input')[0];
    assert.ok(field, 'поле ответа');
    assert.equal(field.getAttribute('data-keep'), 'q:q_1', 'набранное переживёт перерисовку');
    const send = all(row, 'req-q__send')[0];
    assert.ok(send, 'кнопка «Отправить»');
    assert.match(text(send), /Отправить/);
    // WAVE17 P3/B15: длинная подпись «Мне нужно пояснение» у каждого вопроса —
    // это была стена одинаковых кнопок. Теперь короткая кнопка, а само
    // объяснение («Мне нужно пояснение…») живёт в title/aria и в ОДНОЙ общей
    // подсказке блока.
    const explain = all(row, 'req-q__explain')[0];
    assert.ok(explain, 'кнопка «Пояснить…»');
    assert.match(text(explain), /Пояснить/);
    // W21-Q1 (B-44): в подсказке главное — «текст можно не писать».
    assert.match(String(explain.getAttribute('title') || ''), /объяснит вопрос подробнее/);
    assert.match(String(explain.getAttribute('title') || ''), /текст можно не писать/);
    const hints = all(host, 'req-q__common-hint');
    assert.equal(hints.length, 1, 'подсказка про пояснение — одна на блок, не ×N');
    assert.match(text(hints[0]), /Пояснить/);
    // Поле и «Отправить» — соседи в одной строке: dictation-auto увидит кнопку-действие.
    assert.equal(send.parentNode, field.parentNode, 'кнопка в той же строке, что и поле');

    field.value = 'Класс 30';
    field.dispatch('input');
    send.dispatch('click');
    await tick();
    await tick();
    assert.deepEqual(s.posted[0].body, { action: 'answer', question_id: 'q_1', text: 'Класс 30' });

    explain.dispatch('click');
    await tick();
    await tick();
    assert.equal(s.posted[1].body.need_explanation, true, 'встречный вопрос отправляется как просьба пояснить');
  } finally {
    uninstallFakeDom();
  }
});

test('перерисовка не пересобирает строку вопроса: поле и микрофон — те же узлы', () => {
  const s = asideStand();
  try {
    const first = globalThis.document.createElement('div');
    first.appendChild(s.buildAside());
    const field1 = all(first, 'req-q__input').find((f) => f.getAttribute('data-keep') === 'q:q_1');
    assert.ok(field1, 'поле нарисовано');

    // Имитируем опрос: деталь не изменилась, страница собрана заново.
    const second = globalThis.document.createElement('div');
    second.appendChild(s.buildAside());
    const field2 = all(second, 'req-q__input').find((f) => f.getAttribute('data-keep') === 'q:q_1');
    assert.equal(field2, field1, 'поле ответа — тот же узел (фокус и диктовка не теряются)');
    const rows = all(second, 'req-q');
    assert.equal(rows.length, 3, 'дублей строк нет');
    const row = rows.find((r) => r.getAttribute('data-q') === 'q_1');
    assert.equal(row.getAttribute('data-mic-anchor'), 'q:q_1', 'привязка панели на месте');
  } finally {
    uninstallFakeDom();
  }
});

test('встречный вопрос («поясни») остаётся открытым: ответ показан, форма на месте', () => {
  const s = asideStand();
  try {
    s.detail.open_questions = [{
      id: 'q_9',
      text: 'Порог прогона критика — сколько раундов?',
      for: 'mikhail',
      answered: false,
      answer: 'Поясни, что такое порог прогона.',
      answered_at: Math.floor(Date.now() / 1000) - 60,
      answer_state: 'waiting_explanation',
      answer_label: 'ждёт вашего решения после пояснения',
      needs_explanation: true,
    }];
    const host = globalThis.document.createElement('div');
    host.appendChild(s.buildAside());
    const row = all(host, 'req-q')[0];
    const txt = text(row);
    assert.match(txt, /Вопрос — ваш ответ/, 'пара показана');
    assert.match(txt, /Поясни, что такое порог прогона/, 'виден встречный вопрос');
    assert.match(txt, /ждёт вашего решения после пояснения/, 'вопрос НЕ закрыт');
    assert.ok(all(row, 'req-q__form').length, 'форма ответа осталась');
    assert.match(text(host), /1 вопрос/, 'счётчик колонки считает его открытым');
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────── W30-C: вопрос по-человечески (контракт §1) ────────────────
 *
 * Дефект 13/14: вопрос приходил строкой без «зачем», без вариантов и без
 * пояснения руководителя. Здесь проверяется то, что видит человек:
 *   * «Зачем это нужно» — на виду, «Откуда взялось» — раскрывается;
 *   * варианты кнопками с последствиями, выбор уходит действием `answer_option`;
 *   * «Свой ответ» — поле + «Отправить» (диктовка ставится общим правилом),
 *     для вопроса с вариантами это действие `answer_custom`;
 *   * состояние `explained` — пояснение ПОД вопросом, кнопки активны;
 *   * «Пояснить ещё», «Отложить», «Не актуально» — у каждого вопроса;
 *   * блокирующий вопрос и «пакет решений» (неблокирующие) различаются.
 */

const Q_W30 = [
  {
    id: 'q_w1',
    text: 'Приём вещей на дому делаем своей машиной или через партнёра-курьера?',
    for: 'mikhail',
    why: 'От этого зависит, нужен ли свой водитель — 34 000 ₽ в месяц против 9 000 ₽ за курьера.',
    context: 'Сейчас вещи принимают только в пункте. Два клиента спрашивали выезд на дом.',
    options: [
      { id: 'own', label: 'Своя машина', consequence: 'Полный контроль, но платёж каждый месяц.' },
      { id: 'partner', label: 'Партнёр-курьер', consequence: 'Платим только за выезд.' },
    ],
    blocking: true,
    plain_ok: true,
    answer_state: 'open',
    answered: false,
  },
  {
    id: 'q_w2',
    text: 'Сколько берём за выезд на дом?',
    for: 'mikhail',
    why: 'Цена выезда решает, поедет ли клиент.',
    context: 'Средний чек в пункте — 1 240 ₽.',
    options: [{ id: 'flat', label: '700 ₽ за адрес', consequence: 'Окупается на трёх вещах.' }],
    blocking: true,
    plain_ok: true,
    answer_state: 'explained',
    answered: false,
    explanation: 'Выезд — способ забрать вещи, а не отдельная услуга. 700 ₽ покрывают расходы на двух вещах.',
  },
  {
    id: 'q_w3',
    text: 'Какую ткань считаем деликатной?',
    for: 'mikhail',
    why: 'Внутреннее правило приёмки, работу не останавливает.',
    context: 'В памятке приёмщика: шёлк, кашемир, вискоза.',
    options: [],
    blocking: false,
    plain_ok: false,
    plain_issues: ['внутренний код (SKL-2) без расшифровки'],
    answer_state: 'open',
    answered: false,
  },
  {
    id: 'q_w4',
    text: 'Печатать ли номер заказа крупно?',
    for: 'mikhail',
    why: 'Мелкий шрифт — единственная жалоба за месяц.',
    context: '4 жалобы за месяц.',
    options: [],
    blocking: false,
    plain_ok: true,
    answer_state: 'open',
    answered: false,
  },
  {
    id: 'q_w5',
    text: 'Хранить вещи дольше 30 дней?',
    for: 'mikhail',
    why: 'Склад занят, но просрочек мало.',
    context: 'Две вещи пролежали 40 дней.',
    options: [],
    blocking: false,
    plain_ok: true,
    answer_state: 'postponed',
    answered: false,
    postponed: true,
  },
];

let standSeq = 0;

function w30Stand(questions) {
  installFakeDom();
  patchDom();
  const posted = [];
  const toasts = [];
  const detail = {
    id: 't_root1',
    next_steps: [],
    open_questions: JSON.parse(JSON.stringify(questions)),
    decisions: [],
    future_questions: [],
    needs_you_count: {
      questions: questions.filter((q) => !q.postponed).length,
      decisions: 0, approvals: 0, steps: 0,
      total: questions.filter((q) => !q.postponed).length,
    },
    questions_summary: { open: 4, blocking: 2, hidden_duplicates: 2, closed_topics: 1 },
  };
  const ctx = {
    api: {
      async post(path, body) { posted.push({ path, body }); return {}; },
      async get() { return {}; },
    },
    toast: (message, kind) => toasts.push({ message, kind }),
    room: { id: 'cafe', members: [] },
    navigate() {},
  };
  // Свой id на каждый стенд: стор запроса кэшируется по rid на процесс, и общий
  // rid отдал бы тесту api предыдущего стенда — запросы ушли бы в чужой массив.
  standSeq += 1;
  const R = {
    ctx, rid: 't_w30root' + standSeq, keepValues: new Map(), roomId: 'cafe',
    get detail() { return detail; },
    get backlog() { return new Map(); },
    async refresh() {},
  };
  const { buildAside } = createAside(R);
  const host = globalThis.document.createElement('div');
  host.appendChild(buildAside());
  return { host, posted, toasts, detail, R };
}

const rowOf = (host, qid) => all(host, 'req-q').find((r) => r.getAttribute('data-q') === qid);

test('вопрос: «Зачем это нужно» на виду, «Откуда взялось» — раскрываемо', () => {
  const s = w30Stand(Q_W30);
  try {
    const row = rowOf(s.host, 'q_w1');
    const txt = text(row);
    assert.match(txt, /Зачем это нужно/, 'зачем — видно без раскрытия');
    assert.match(txt, /34 000 ₽ в месяц против 9 000 ₽/, 'текст «зачем» из API');
    assert.match(txt, /Откуда взялось/, 'контекст подписан');
    assert.match(txt, /Два клиента спрашивали выезд на дом/, 'текст контекста из API');
    assert.equal(all(row, 'req-q__ctx').length, 1, 'контекст — раскрываемый блок');
    assert.match(text(row), /Блокирует работу/, 'блокирующий вопрос помечен');
  } finally {
    uninstallFakeDom();
  }
});

test('варианты: кнопки с последствиями, выбор уходит answer_option', async () => {
  const s = w30Stand(Q_W30);
  try {
    const row = rowOf(s.host, 'q_w1');
    const opts = all(row, 'req-q__option');
    assert.equal(opts.length, 2, 'оба варианта на месте');
    assert.match(text(opts[0]), /Своя машина/);
    assert.match(text(opts[0]), /Платёж каждый месяц/i, 'последствие видно словами');
    assert.equal(opts[0].getAttribute('data-option-id'), 'own');
    opts[1].dispatch('click');
    await tick(); await tick();
    assert.deepEqual(s.posted[0].body,
      { action: 'answer_option', question_id: 'q_w1', option_id: 'partner' });
    assert.equal(s.posted[0].path, '/api/requests/' + s.R.rid + '/actions');
  } finally {
    uninstallFakeDom();
  }
});

test('«Свой ответ»: поле + «Отправить» рядом (диктовка) — действие answer_custom', async () => {
  const s = w30Stand(Q_W30);
  try {
    const row = rowOf(s.host, 'q_w1');
    assert.match(text(row), /Свой ответ/, 'у вопроса с вариантами есть «Свой ответ»');
    const field = all(row, 'req-q__input')[0];
    const send = all(row, 'req-q__send')[0];
    assert.ok(field && send);
    assert.equal(send.parentNode, field.parentNode, 'поле и кнопка-действие в одной строке');
    field.value = 'Только партнёр, своей машины не покупаем';
    field.dispatch('input');
    send.dispatch('click');
    await tick(); await tick();
    assert.deepEqual(s.posted[0].body, {
      action: 'answer_custom', question_id: 'q_w1',
      text: 'Только партнёр, своей машины не покупаем',
    });
  } finally {
    uninstallFakeDom();
  }
});

test('пояснено: пояснение руководителя ПОД вопросом, кнопки активны', () => {
  const s = w30Stand(Q_W30);
  try {
    const row = rowOf(s.host, 'q_w2');
    const txt = text(row);
    assert.match(txt, /пояснено — выберите ответ/, 'состояние explained подписано');
    assert.match(txt, /Пояснение руководителя/, 'пояснение под вопросом');
    assert.match(txt, /700 ₽ покрывают расходы на двух вещах/, 'текст пояснения из API');
    assert.equal(all(row, 'req-q__option').length, 1, 'варианты активны');
    assert.ok(all(row, 'req-q__input').length, 'свой ответ доступен');
    assert.ok(all(row, 'req-q__stale').length, '«Не актуально» доступно и в состоянии «пояснено»');
  } finally {
    uninstallFakeDom();
  }
});

test('«Пояснить ещё» и «Отложить»: свои действия контракта §1', async () => {
  const s = w30Stand(Q_W30);
  try {
    const explained = rowOf(s.host, 'q_w2');
    const more = all(explained, 'req-q__explain-more')[0];
    assert.ok(more, 'у объяснённого вопроса есть «Пояснить ещё»');
    more.dispatch('click');
    await tick(); await tick();
    assert.deepEqual(s.posted[0].body, { action: 'explain_more', question_id: 'q_w2' });

    const open = rowOf(s.host, 'q_w1');
    const later = all(open, 'req-q__postpone')[0];
    assert.ok(later, '«Отложить» есть у открытого вопроса');
    later.dispatch('click');
    await tick(); await tick();
    assert.deepEqual(s.posted.filter((p) => p.body).pop().body,
      { action: 'postpone', question_id: 'q_w1' });
  } finally {
    uninstallFakeDom();
  }
});

test('«Не актуально» уходит с возвратом, отложенные видны отдельным списком', () => {
  const s = w30Stand(Q_W30);
  try {
    const row = rowOf(s.host, 'q_w1');
    const stale = all(row, 'req-q__stale')[0];
    assert.match(text(stale), /Не актуально/);

    const postponed = all(s.host, 'req-side__postponed')[0];
    assert.ok(postponed, 'блок «Отложенные» есть');
    assert.match(text(postponed), /Отложенные вопросы: 1/);
    const back = all(postponed, 'req-q__stale')[0];
    assert.match(text(back), /Вернуть/, 'отложенный вопрос возвращается, а не пропадает');
  } finally {
    uninstallFakeDom();
  }
});

test('блокирующие и неблокирующие: полные строки и «пакет решений»', () => {
  const s = w30Stand(Q_W30);
  try {
    const packet = all(s.host, 'req-side__packet')[0];
    assert.ok(packet, 'неблокирующие — «пакетом решений»');
    assert.match(text(packet), /Пакет решений: 2 вопроса/);
    assert.match(text(packet), /работу не блокируют/);
    const packets = all(packet, 'req-q').map((r) => r.getAttribute('data-q'));
    assert.deepEqual(packets, ['q_w3', 'q_w4'], 'в пакете ровно неблокирующие вопросы');
    assert.equal(all(s.host, 'req-q').length, 4, 'все открытые вопросы на экране (без отложенных)');
    assert.match(text(s.host), /Похожие вопросы скрыты как дубли: 2/, 'дубли темы объяснены тихо');
  } finally {
    uninstallFakeDom();
  }
});

test('вопрос без проверки «понятно человеку» помечен тихо, а не запрещён', () => {
  const s = w30Stand(Q_W30);
  try {
    const row = rowOf(s.host, 'q_w3');
    const mark = all(row, 'req-q__plain')[0];
    assert.ok(mark, 'служебная пометка есть');
    assert.match(text(mark), /проверка простоты не пройдена/);
    assert.match(String(mark.getAttribute('title') || ''), /SKL-2/, 'причина — в подсказке');
    assert.ok(all(row, 'req-q__input').length, 'ответить по-прежнему можно');
  } finally {
    uninstallFakeDom();
  }
});
