/**
 * request-thread.js — блок «Диалог по запросу» на странице запроса.
 *
 * Лента: сообщения Михаила — справа и выделены; команда — слева, с аватаром и
 * ролью по-русски; системные события — тонкой серой строкой; между днями —
 * разделители. Поле ввода растёт, микрофон ставится сам (dictation-auto),
 * скрепка прикрепляет путь к файлу (общий компонент attach-picker.js, как в композере).
 *
 * Отправка — ОДНА кнопка «Отправить руководителю» (Ctrl+Enter):
 *   POST /api/requests/{rid}/messages {text, attach, urgent}
 * Сообщение сразу появляется в ленте (оптимистично), затем лента берётся из
 * ответа сервера. Ошибка сети — человеческий текст под полем, набранное
 * остаётся в поле.
 *
 * Вторая кнопка зависит от статуса запроса:
 *   ошибка  → «Повторить с уточнением»;
 *   иначе   → второй кнопки нет.
 *
 * W17-S §1: кнопки «Отправить на доработку» здесь БОЛЬШЕ НЕТ — доработка свода
 * запускается одним местом («Доработать свод» в блоке «Свод»). Если свод
 * готов, под полем есть переключатель «и доработать свод по этому сообщению»:
 * сообщение уходит как уточнение и тем же текстом запускает доработку (revise).
 *
 * W20-C1 (B-40): блок монтируется ОДИН раз на страницу, дальше страница зовёт
 * `update(detail)` — пересобрать диалог заново нельзя: живая кнопка «Отправить»
 * оставалась от новой сборки, а поле с набранным текстом — от старой, и кнопка
 * читала значение своего (не вставленного в страницу) поля. Поэтому:
 *   * страница вставляет блок один раз и обновляет только данные;
 *   * `send()` берёт текст из ВИДИМОГО поля блока, а не из своего замыкания;
 *   * поле помечено `data-keep`, весь блок — `data-keep-tree`: точечная правка
 *     страницы (live-patch.js) его не заменяет и внутрь не заходит.
 *
 * Публично: mountThread(container, ctx, {roomId, detail, rid, onSent}) →
 *   {element, update(detail), focus(), destroy()}.
 */

import { h, fmtDate, fmtDateTime, toDate } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, avatar } from '../ui.js';
// W30-F (дефект 15): прикрепление файлов — ОДИН общий компонент, как в композере.
import { createAttachPicker } from '../attach-picker.js';
import { countNoun, errorText } from '../text-ru.js';
import { profileName } from './request-util.js';
import { createSection } from './section-collapse.js';

const CSS_HREF = 'css/view-thread.css';
export const EMPTY_TEXT = 'Здесь можно уточнить задачу, пока команда работает, или задать вопрос после ответа';
export const SEND_HINT = 'Сообщение получит руководитель комнаты, он передаст нужным участникам';
const URGENT_LABEL = 'Срочно: пересмотреть план целиком';
const SEND_LABEL = 'Отправить руководителю';

const FINAL = new Set(['done', 'failed']);

/* ─────────────────────────────────────────── роли по-русски */

/** Падежи подписи главы комнаты: «руководитель комнаты», «руководителю комнаты». */
const LEAD_FORMS = {
  '': 'руководитель комнаты',
  а: 'руководителя комнаты',
  у: 'руководителю комнаты',
  ом: 'руководителем комнаты',
  е: 'руководителе комнаты',
};

/**
 * Заменить устаревшее название главы комнаты (в любом падеже и с «комнаты»)
 * понятной подписью. Ключи API и профили не трогаем — только читаемый текст.
 * @param {*} text
 * @returns {string}
 */
export function ruText(text) {
  return String(text === null || text === undefined ? '' : text).replace(
    /([Дд])иректор(ом|а|у|е)?(\s+комнаты)?/g,
    (m, first, end) => {
      const word = LEAD_FORMS[end || ''] || LEAD_FORMS[''];
      return first === 'Д' ? word[0].toUpperCase() + word.slice(1) : word;
    },
  );
}

/**
 * Роль участника для подписи: «team-ops» → «операции», «mikhail» → «Михаил».
 * @param {{author?: string, author_kind?: string}} item
 * @param {object} [room]
 * @returns {string}
 */
export function roleLabel(item, room) {
  const raw = String((item && item.author) || '').trim();
  if (raw === 'mikhail') return 'Михаил';
  if (raw === 'system') return 'Система';
  if (!raw) return 'команда';
  return ruText(profileName(raw, room) || raw);
}

/**
 * Подпись автора сообщения: готовая с сервера («Глеб · роль»), иначе роль
 * по профилю. Старое название главы комнаты заменяется понятным.
 * @param {object} item
 * @param {object} [room]
 * @returns {string}
 */
export function authorTitle(item, room) {
  const raw = String((item && item.author_label) || '').trim();
  if (raw) return ruText(raw);
  return roleLabel(item, room);
}

/**
 * Вторая кнопка по статусу запроса. Доработки свода здесь нет (W17-S §1):
 * у готового свода вместо кнопки — переключатель «и доработать свод».
 * @param {string} status
 * @returns {{key: string, label: string}|null}
 */
export function secondAction(status) {
  const s = String(status === null || status === undefined ? '' : status);
  if (s === 'failed') return { key: 'retry', label: 'Повторить с уточнением' };
  return null;
}

/**
 * Показывать ли переключатель «и доработать свод по этому сообщению».
 * Только когда свод готов (дорабатывать есть что).
 * @param {string} status
 * @returns {boolean}
 */
export function canReviseWithMessage(status) {
  return String(status === null || status === undefined ? '' : status) === 'done';
}

/* ─────────────────────────────────────────── данные ленты */

function num(v) {
  const n = Number(v);
  return isFinite(n) ? n : 0;
}

function itemKey(item) {
  const id = String((item && item.id) || '').trim();
  if (id) return id;
  return [num(item && item.at), item && item.author, item && item.text].join('|');
}

/**
 * Склеить две ленты по id (сервер и оптимистичная копия), по времени.
 * Серверная запись побеждает локальную с тем же id.
 * @param {Array} a
 * @param {Array} b
 * @returns {Array}
 */
export function mergeThreads(a, b) {
  const out = [];
  const at = new Map();
  for (const list of [a, b]) {
    for (const item of Array.isArray(list) ? list : []) {
      if (!item || typeof item !== 'object') continue;
      const key = itemKey(item);
      const idx = at.get(key);
      if (idx === undefined) {
        at.set(key, out.length);
        out.push({ ...item, id: key });
      } else if (item.pending !== true) {
        out[idx] = { ...out[idx], ...item, id: key };
      }
    }
  }
  out.sort((x, y) => num(x.at) - num(y.at));
  return out;
}

function dayKey(x) {
  const d = toDate(x);
  if (!d) return '';
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

function dayLabel(x, now = Date.now()) {
  const d = toDate(x);
  if (!d) return '';
  const today = new Date(now);
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (dayKey(d) === dayKey(today)) return 'Сегодня';
  if (dayKey(d) === dayKey(yesterday)) return 'Вчера';
  return fmtDate(d);
}

function clock(x) {
  const d = toDate(x);
  if (!d) return '';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return hh + ':' + mm;
}

function isSystem(item) {
  return item.author_kind === 'system' || item.kind === 'event';
}

/**
 * Запись «ответ на конкретный вопрос» (W14-D §1). В ленте это НЕ обычное
 * сообщение: сверху цитата вопроса, ниже ответ, и кнопка «К вопросу» —
 * ответ живёт у своего вопроса, а не россыпью в общем чате (W15-C, п. 5).
 */
export function isAnswer(item) {
  return Boolean(item) && String(item.kind || '') === 'answer';
}

function isMine(item) {
  return item.author_kind === 'mikhail' || item.author === 'mikhail';
}

/** Заменить детей узла (replaceChildren есть не в каждом окружении). */
function setChildren(el, ...nodes) {
  if (typeof el.replaceChildren === 'function') { el.replaceChildren(...nodes); return; }
  for (const child of Array.from(el.childNodes || [])) {
    child.parentNode = null;
    try { if (typeof el.removeChild === 'function') el.removeChild(child); } catch { /* ignore */ }
  }
  el.childNodes = [];
  for (const node of nodes) if (node) el.appendChild(node);
}

/* ─────────────────────────────────────────── состояние (переживает перерисовку) */

const STATE = new WeakMap();
const FALLBACK = new Map();

/**
 * Черновик, вложения, фокус и прокрутка — по одному на страницу и запрос.
 * @param {object} ctx
 * @param {string} rid
 * @returns {object}
 */
function stateFor(ctx, rid) {
  const usable = ctx && typeof ctx === 'object';
  let byRid = usable ? STATE.get(ctx) : FALLBACK;
  if (!byRid) {
    byRid = new Map();
    if (usable) STATE.set(ctx, byRid);
  }
  let st = byRid.get(rid);
  if (!st) {
    st = {
      thread: [], text: '', attach: [], urgent: false, revise: false,
      focus: false, feedScroll: 0, atBottom: true,
      sending: false, error: '', seq: 0, clientId: '',
    };
    byRid.set(rid, st);
  }
  return st;
}

/* ─────────────────────────────────────────── монтирование */

/**
 * Собрать блок «Диалог по запросу» в контейнере.
 * @param {HTMLElement} container
 * @param {object} ctx  контекст вьюхи ({api, room, toast, ...})
 * @param {{roomId?: string, detail?: object, rid?: string, onSent?: Function}} opts
 * @returns {{focus: Function, destroy: Function, element: HTMLElement}}
 */
export function mountThread(container, ctx, opts = {}) {
  const o = opts || {};
  const detail = o.detail || null;
  const roomId = String(o.roomId || (detail && detail.room) || '');
  const rid = String(o.rid || (detail && (detail.root_id || detail.id)) || '');
  // Статус меняется по ходу работы (working → done): от него зависят переключатели
  // под полем, поэтому это не константа монтажа, а состояние блока.
  let status = String((detail && detail.status) || '');
  const st = stateFor(ctx, rid || 'thread');

  ensureCss(CSS_HREF);

  const api = (ctx && ctx.api) || {};
  const toast = (msg, kind) => {
    try {
      if (ctx && typeof ctx.toast === 'function') ctx.toast(msg, kind || 'info');
    } catch { /* тост не критичен */ }
  };
  const enc = encodeURIComponent;

  if (Array.isArray(detail && detail.thread)) {
    st.thread = mergeThreads(st.thread, detail.thread);
  }
  if (!Array.isArray(st.thread)) st.thread = [];

  /* ── лента ─────────────────────────────────────────────── */

  const feed = h('div', {
    class: 'thr__feed', role: 'log', 'aria-live': 'polite',
    'aria-label': 'Диалог по задаче', tabindex: '0',
  });

  function messageNode(item) {
    const mine = isMine(item);
    const title = authorTitle(item, ctx && ctx.room);
    const role = mine || title.indexOf('·') !== -1 ? '' : roleLabel(item, ctx && ctx.room);
    const person = title.split('·')[0].trim() || (mine ? 'Михаил' : role || 'команда');

    const body = h('div', { class: 'thr-msg__body' },
      h('div', { class: 'thr-msg__head' },
        h('span', { class: 'thr-msg__who' }, title),
        role ? h('span', { class: 'thr-msg__role' }, role) : null,
        h('span', { class: 'thr-msg__time tnum', title: fmtDateTime(item.at) }, clock(item.at))),
      h('div', { class: 'thr-msg__text' }, String(item.text || '')));

    // W20 §1.1 (B-36): у каждого сообщения человека — что с ним стало в своде.
    const delivery = deliveryNode(item);
    if (delivery) body.appendChild(delivery);
    // W20 §1.2 (B-38): у сообщения руководителя с вопросами — «ждёт ваших ответов».
    const asks = asksNode(item);
    if (asks) body.appendChild(h('div', { class: 'thr-msg__asks' }, asks));

    const cls = ['thr-msg'];
    if (mine) cls.push('thr-msg--mine');
    if (item.pending) cls.push('thr-msg--pending');
    const showAvatar = !mine && !item.pending;
    return h('div', { class: cls, 'data-id': String(item.id || ''), 'data-kind': mine ? 'mikhail' : 'agent' },
      showAvatar ? avatar(person, { size: 'sm', title }) : null,
      body);
  }

  function systemNode(item) {
    return h('div', { class: 'thr-ev', title: fmtDateTime(item.at), 'data-kind': 'system' },
      h('span', { class: 'thr-ev__text' }, String(item.text || '')));
  }

  /** Показать вопрос в колонке справа (там ответ и реакция руководителя). */
  function revealQuestion(qid) {
    const id = String(qid || '');
    let host = null;
    try {
      host = id ? document.querySelector('[data-mic-anchor="q:' + id.replace(/["\\]/g, '\\$&') + '"]') : null;
      if (!host) host = document.getElementById('questions');
    } catch { host = null; }
    if (!host || typeof host.scrollIntoView !== 'function') return;
    try { host.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch { /* без плавности */ }
    host.classList.add('is-flash');
    setTimeout(() => { try { host.classList.remove('is-flash'); } catch { /* узел уже другой */ } }, 1400);
  }

  /** Прокрутить к блоку «Нужен ваш ответ» справа (плашка «ждёт ваших ответов»). */
  function revealQuestions() {
    let host = null;
    try {
      host = document.querySelector ? document.querySelector('[data-questions]') : null;
      if (!host) host = document.getElementById('questions');
    } catch { host = null; }
    if (!host || typeof host.scrollIntoView !== 'function') return;
    try { host.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch { /* без плавности */ }
    host.classList.add('is-flash');
    setTimeout(() => { try { host.classList.remove('is-flash'); } catch { /* узел уже другой */ } }, 1400);
  }

  /**
   * Подпись доставки сообщения (W20 §1.1, B-36): «Учтено в версии 4»,
   * «Ждёт обновления свода — в 14:05», «Не учтено — автообновление выключено».
   * Приходит с сервера полем `delivery`; своих догадок здесь нет.
   * @param {object} item элемент ленты
   * @returns {HTMLElement|null}
   */
  function deliveryNode(item) {
    const d = item && item.delivery;
    if (!d || typeof d !== 'object') return null;
    const state = String(d.state || '');
    const text = String(d.text || '').trim();
    if (!text) return null;
    const iconName = (state === 'applied' || state === 'applied_nochange') ? 'check'
      : state === 'declined' ? 'warning' : state === 'working' ? 'sync' : 'clock';
    return h('div', {
      class: 'thr-msg__delivery thr-msg__delivery--' + (state || 'waiting'),
      'data-delivery': state || 'waiting',
    }, icon(iconName, { size: 11 }), h('span', null, text));
  }

  /**
   * Плашка у сообщения руководителя с вопросами (W20 §1.2, B-38):
   * «ждёт ваших ответов: 7 → справа»; клик прокручивает к «Нужен ваш ответ».
   * @param {object} item элемент ленты с `asks`
   * @returns {HTMLElement|null}
   */
  function asksNode(item) {
    const asks = Array.isArray(item && item.asks) ? item.asks.filter(Boolean) : [];
    if (!asks.length) return null;
    return h('button', {
      type: 'button', class: 'thr-ask', 'data-action': 'jump-asks', 'data-asks': String(asks.length),
      title: 'Перейти к «Нужен ваш ответ» — там можно ответить на каждый вопрос',
      onclick: () => revealQuestions(),
    }, icon('comment-discussion', { size: 11 }),
      h('span', null, `ждёт ваших ответов: ${asks.length} → справа`));
  }

  /**
   * Ответ на вопрос: «Ответ на вопрос «…»» + сам ответ + переход к вопросу.
   * Так он не читается как ещё одно сообщение в общем чате.
   */
  function answerNode(item) {
    const qid = String(item.question_id || '');
    const qtext = String(item.question_text || '').trim();
    const ask = String(item.answer_kind || '') === 'explanation_request';
    const go = h('button', {
      class: 'btn btn--ghost btn--sm req-ans__go', type: 'button',
      title: 'Показать этот вопрос и статус ответа в колонке справа',
      onclick: () => revealQuestion(qid),
    }, icon('arrow-right', { size: 11 }), ask ? 'К пояснению' : 'К вопросу');
    return h('div', {
      class: 'thr-msg thr-msg--mine req-ans' + (ask ? ' req-ans--ask' : ''),
      'data-id': String(item.id || ''), 'data-kind': 'answer', 'data-question': qid,
    }, h('div', { class: 'thr-msg__body' },
      h('div', { class: 'thr-msg__head' },
        h('span', { class: 'thr-msg__who' }, authorTitle(item, ctx && ctx.room)),
        h('span', { class: 'req-ans__kind' }, ask ? 'просит пояснить' : 'ответ на вопрос'),
        h('span', { class: 'thr-msg__time tnum', title: fmtDateTime(item.at) }, clock(item.at))),
      qtext ? h('div', { class: 'req-ans__quote' }, '«' + qtext + '»') : null,
      h('div', { class: 'thr-msg__text' }, String(item.text || '')),
      h('div', { class: 'req-ans__foot' }, go)));
  }

  function paintFeed() {
    const list = Array.isArray(st.thread) ? st.thread : [];
    if (!list.length) {
      setChildren(feed, h('div', { class: 'thr__empty' },
        icon('comment-discussion', { size: 18 }),
        h('div', { class: 'thr__empty-text' }, EMPTY_TEXT)));
      return;
    }
    const nodes = [];
    let day = null;
    for (const item of list) {
      const k = dayKey(item.at);
      if (k && k !== day) {
        day = k;
        nodes.push(h('div', { class: 'thr__day' }, h('span', null, dayLabel(item.at))));
      }
      nodes.push(isAnswer(item) ? answerNode(item) : (isSystem(item) ? systemNode(item) : messageNode(item)));
    }
    setChildren(feed, ...nodes);
  }

  /* ── поле ввода и кнопки ───────────────────────────────── */

  const ta = h('textarea', {
    class: 'textarea thr__input', rows: 3, id: 'thr-input',
    placeholder: 'Напишите или надиктуйте сообщение…',
    'aria-label': 'Сообщение руководителю комнаты',
    spellcheck: 'false',
    // W20-C1 (B-40): черновик переживает любую правку страницы — поле с этим
    // ключом live-patch не заменяет, а значение восстанавливает.
    'data-keep': 'thread-draft',
  });
  ta.value = st.text || '';

  const sendLabel = h('span', null, SEND_LABEL);
  const sendBtn = h('button', {
    class: 'btn btn--primary thr__send', type: 'button', 'data-action': 'send',
  }, icon('send', { size: 13 }), sendLabel);

  // W20-C1 (B-40): кнопка «Отправить» и поле живут до конца страницы. Рядом —
  // переключатели, которые зависят от статуса запроса: их пересобирает
  // `paintOptions()` только при смене статуса, а не на каждом обновлении.
  const optsHost = h('div', { class: 'thr__opts' });
  try { optsHost.style.display = 'contents'; } catch { /* нет стилей — не критично */ }
  let optsStatus = null;
  let secondBtn = null;
  let urgentBox = null;
  let reviseBox = null;
  let urgentInput = null;
  let reviseInput = null;

  const errLine = h('div', { class: 'thr__err hidden', role: 'alert' });
  // W30-F (дефект 15): прикрепление файлов — один общий компонент. Скрепка
  // открывает системное окно выбора файлов Windows (POST /api/files/pick), пути
  // прикрепляются как пути (копий нет), предел 30, секреты — через
  // «Всё равно прикрепить». `st.attach` держит пути для отправки сообщения.
  const picker = createAttachPicker({
    api,
    toast,
    prefix: 'thr-attach',
    title: 'Прикрепить файлы к сообщению',
    label: '',
    buttonClass: 'btn btn--ghost btn--icon thr__clip',
    buttonTitle: 'Прикрепить файлы — окно выбора откроется на компьютере',
    rowOnAdd: false,
    onChange: (items) => { st.attach = items.map((item) => item.path); },
  });
  const filesHost = picker.chipsHost;
  /** Блок-обёртка со стрелкой (У-1): заголовок, счётчик, метка нового ответа. */
  let sec = null;
  /**
   * Сколько сообщений команды пришло, пока диалог был свёрнут (У-1).
   * Показывается меткой «есть новый ответ» в заголовке: новый ответ руководителя
   * в свёрнутом блоке не теряется. Обнуляется при раскрытии.
   */
  let unread = 0;
  /** Какие сообщения команды уже видели — чтобы считать только новые. */
  let seenAgent = new Set();

  function agentIds(list) {
    const out = new Set();
    for (const item of Array.isArray(list) ? list : []) {
      if (!item || isSystem(item) || isMine(item)) continue;
      out.add(String(item.id || ''));
    }
    out.delete('');
    return out;
  }

  function showError(text) {
    st.error = String(text || '');
    setChildren(errLine);
    if (st.error) {
      errLine.appendChild(icon('error', { size: 12 }));
      errLine.appendChild(h('span', null, st.error));
    }
    errLine.classList.toggle('hidden', !st.error);
  }

  /**
   * Показать прикреплённые пути в общем компоненте: после сборки блока и после
   * отправки (когда `st.attach` очищен). Сами чипы рисует `attach-picker.js` —
   * у них те же «Просмотр / Открыть / Открыть в папке / Убрать», что в композере.
   */
  function paintFiles() {
    const list = Array.isArray(st.attach) ? st.attach : [];
    if (list.length) picker.set(list);
    else picker.clear();
  }

  /**
   * Заголовок блока (У-1): число сообщений и метка «есть новый ответ».
   * Раньше это был отдельный чип под заголовком — теперь краткая суть живёт
   * в общем компоненте (`section-collapse.js`), в том числе в свёрнутом виде.
   */
  function paintCount() {
    if (!sec) return;
    const n = (st.thread || []).filter((m) => !isSystem(m)).length;
    const chips = [];
    if (n) chips.push({ text: countNoun(n, ['сообщение', 'сообщения', 'сообщений']), tone: 'muted' });
    if (unread > 0) {
      chips.push({
        text: unread === 1 ? 'есть новый ответ' : 'новых ответов: ' + unread,
        tone: 'info',
        dot: true,
      });
    }
    sec.setSummary(chips);
  }

  /** Метка «есть новый ответ» снимается, когда человек раскрыл блок. */
  function markRead() {
    if (!unread) return;
    unread = 0;
    paintCount();
  }

  /**
   * Поле, в котором человек на самом деле печатает (W20-C1, B-40).
   *
   * Обычно это наше `ta`. Если же этот блок по какой-то причине оказался не в
   * странице (а на экране — другой), читаем видимое поле страницы: кнопка не
   * должна смотреть в пустое невидимое поле и отвечать «Напишите сообщение».
   */
  function visibleField() {
    try {
      if (section && section.isConnected === true) return ta;
    } catch { /* нет DOM */ }
    const d = globalThis.document;
    if (d && typeof d.querySelector === 'function') {
      try {
        const found = d.querySelector('.thr__input') || d.querySelector('textarea.thr__input');
        if (found && typeof found.value === 'string') return found;
      } catch { /* страница без селекторов */ }
    }
    return ta;
  }

  /** Текст в поле на этот момент (без пробелов по краям). */
  function draftText() {
    const field = visibleField();
    const raw = field && field.value !== undefined && field.value !== null ? field.value : st.text;
    return String(raw === undefined || raw === null ? '' : raw).trim();
  }

  function syncButtons() {
    // W20-C1: пустое поле — кнопка неактивна (нажимать нечего), идёт отправка —
    // «Отправляется…» и тоже неактивна. Так двойное нажатие не даёт двух сообщений.
    const empty = draftText() === '';
    sendBtn.disabled = Boolean(st.sending) || empty;
    sendLabel.textContent = st.sending ? 'Отправляется…' : SEND_LABEL;
    sendBtn.setAttribute('aria-busy', st.sending ? 'true' : 'false');
    if (secondBtn) secondBtn.disabled = Boolean(st.sending) || empty;
  }

  /**
   * Перерисовать кнопки у ЖИВОГО блока. С W20-C1 диалог монтируется один раз,
   * поэтому «живой» — это он сам; прежний поиск последней копии в `st.sync`
   * оставлен для совместимости (страница могла ещё держать свою ссылку).
   */
  function syncLive() {
    const live = st.sync;
    if (typeof live === 'function' && live !== syncButtons) { live(); return; }
    syncButtons();
  }
  st.sync = syncButtons;

  function autosize() {
    const h0 = typeof ta.scrollHeight === 'number' ? ta.scrollHeight : NaN;
    if (!isFinite(h0) || h0 <= 0) return;
    const win = globalThis.window;
    const cap = Math.max(140, Math.round(((win && win.innerHeight) || 700) * 0.34));
    ta.style.height = 'auto';
    const next = Math.min(h0 + 2, cap);
    ta.style.height = next + 'px';
    ta.style.overflowY = h0 > cap ? 'auto' : 'hidden';
  }

  // Дефект 15: скрепка открывает проводник, а не просит вставить путь.
  const clipBtn = picker.button;

  /**
   * Переключатели и вторая кнопка по статусу запроса. Собираются заново ТОЛЬКО
   * при смене статуса (W20-C1): кнопка «Отправить» и поле при этом не трогаются.
   */
  function paintOptions() {
    if (optsStatus === status) return;
    optsStatus = status;
    secondBtn = null;
    urgentBox = null;
    reviseBox = null;
    urgentInput = null;
    reviseInput = null;
    setChildren(optsHost);

    urgentInput = h('input', { type: 'checkbox', 'data-urgent': '' });
    urgentInput.checked = Boolean(st.urgent);
    urgentInput.addEventListener('change', () => { st.urgent = Boolean(urgentInput.checked); });
    urgentBox = FINAL.has(status)
      ? null
      : h('label', {
        class: 'checkbox thr__urgent',
        title: 'Руководитель комнаты пересмотрит оставшиеся шаги; начатую работу никто не останавливает',
      }, urgentInput, h('span', null, URGENT_LABEL));

    /* W17-S §1: вместо второй кнопки «Отправить на доработку» — переключатель.
       Сообщение уходит как обычное уточнение и этим же текстом запускает доработку. */
    reviseInput = h('input', { type: 'checkbox', 'data-revise-with-message': '' });
    reviseInput.checked = Boolean(st.revise);
    reviseInput.addEventListener('change', () => { st.revise = Boolean(reviseInput.checked); });
    reviseBox = canReviseWithMessage(status)
      ? h('label', {
        class: 'checkbox thr__revise',
        title: 'Сообщение уйдёт руководителю комнаты как уточнение и сразу запустит доработку свода по нему',
      }, reviseInput, h('span', null, 'и доработать свод по этому сообщению'))
      : null;

    const second = secondAction(status);
    secondBtn = second
      ? h('button', {
        class: 'btn btn--secondary thr__second', type: 'button', 'data-action': second.key,
      }, second.key === 'revise' ? icon('comment-draft', { size: 13 }) : icon('refresh', { size: 13 }), second.label)
      : null;
    if (secondBtn) secondBtn.addEventListener('click', () => { send(second.key); });

    for (const node of [reviseBox, urgentBox, secondBtn]) if (node) optsHost.appendChild(node);
    if (reviseBox) hintRevise.classList.remove('hidden');
    else hintRevise.classList.add('hidden');
  }

  /* ── отправка ─────────────────────────────────────────── */

  /**
   * Применить ответ сервера `{message, thread, routed_to, merged}`.
   * Лента берётся из ответа; отправленное сообщение обязано попасть в неё даже
   * если сервер прислал `thread` без него (иначе оптимистичная копия навсегда
   * осталась бы «отправляется»).
   * @param {*} res
   * @param {string} localId
   */
  function applyResponse(res, localId) {
    const msg = res && res.message && typeof res.message === 'object' ? res.message : null;
    const list = res && Array.isArray(res.thread) ? res.thread : null;
    if (list) {
      st.thread = mergeThreads(list, msg ? [msg] : []);
      return;
    }
    st.thread = (st.thread || []).map((m) => {
      if (m.id !== localId) return m;
      if (msg) return { ...m, ...msg, pending: false };
      return { ...m, pending: false };
    });
  }

  /** Ключ одного нажатия (W20-C1, B-39): повтор доставки не создаёт дубля. */
  function newClientId() {
    const rnd = Math.random().toString(36).slice(2, 8);
    return 'msg-' + Date.now().toString(36) + '-' + (rnd || 'x0x0x0');
  }

  async function send(kind) {
    if (st.sending) return;
    // У-1: отправка разворачивает свёрнутый диалог — человек должен увидеть,
    // что ушло и что ответил руководитель.
    if (sec && !sec.isOpen()) sec.expand();
    // W20-C1 (B-40): текст — из ВИДИМОГО поля блока, а не из замыкания монтажа.
    const field = visibleField();
    const text = String(field && field.value !== undefined && field.value !== null
      ? field.value : '').trim();
    if (!text) {
      showError('');
      toast('Напишите сообщение — оно уйдёт руководителю комнаты', 'info');
      try { field.focus(); } catch { /* нет DOM */ }
      return;
    }
    if (!rid) {
      showError('Не удалось отправить: в адресе нет номера задачи. Обновите страницу.');
      return;
    }

    if (kind === 'revise') {
      // Оставлено для совместимости вызовов, но кнопки в интерфейсе нет (W17-S §1):
      // доработка запускается только из блока «Свод».
      st.sending = true;
      syncButtons();
      try {
        await api.post(`/api/requests/${enc(rid)}/actions`, { action: 'revise', text });
        st.text = '';
        field.value = '';
        ta.value = '';
        showError('');
        toast('Свод отправлен на доработку руководителю комнаты', 'ok');
        if (typeof o.onSent === 'function') await o.onSent();
      } catch (err) {
        showError(errorText(err).message + ' Текст остался в поле — попробуйте ещё раз.');
      } finally {
        st.sending = false;
        syncButtons();
      }
      return;
    }

    st.seq += 1;
    const localId = 'local:' + st.seq;
    const reviseToo = Boolean(st.revise) && canReviseWithMessage(status);
    if (reviseToo) { st.revise = false; if (reviseInput) reviseInput.checked = false; }
    const local = {
      id: localId, at: Math.floor(Date.now() / 1000), author: 'mikhail', author_kind: 'mikhail',
      author_label: 'Михаил', text, task_id: null, kind: 'message', pending: true,
      attach: (st.attach || []).slice(),
    };
    st.thread = mergeThreads(st.thread, [local]);
    st.sending = true;
    st.text = '';
    field.value = '';
    if (field !== ta) ta.value = '';
    st.atBottom = true;
    showError('');
    paintFeed();
    paintCount();
    syncButtons();
    try { feed.scrollTop = feed.scrollHeight || 0; } catch { /* нет прокрутки */ }

    // Ключ нажатия переживает неудачную попытку: если запрос всё-таки дошёл,
    // повтор с тем же ключом получит прежний ответ, а не создаст второе сообщение.
    if (!st.clientId) st.clientId = newClientId();
    try {
      const body = { text, attach: (st.attach || []).slice(), client_id: st.clientId };
      if (st.urgent && !FINAL.has(status)) body.urgent = true;
      const res = await api.post(`/api/requests/${enc(rid)}/messages`, body);
      st.clientId = '';
      applyResponse(res, localId);
      st.attach = [];
      st.urgent = false;
      if (urgentInput) urgentInput.checked = false;
      picker.clear();
      if (res && res.duplicate) {
        // Сервер уже принял это сообщение чуть раньше — второй раз не нужно.
        toast('Это сообщение уже отправлено — второго не будет', 'info');
      } else if (reviseToo) {
        // W17-S §1: то же сообщение запускает доработку свода; дайджест ответов
        // и уточнений руководитель получит автоматически (build_revision_body).
        try {
          await api.post(`/api/requests/${enc(rid)}/actions`, { action: 'revise', text });
          toast('Сообщение отправлено — руководитель доработает свод по нему', 'ok');
        } catch (err) {
          toast('Сообщение ушло, но доработку запустить не удалось: ' + (errorText(err).message || ''), 'danger');
        }
      } else {
        toast(kind === 'retry'
          ? 'Повтор с уточнением отправлен руководителю комнаты'
          : 'Сообщение отправлено руководителю комнаты', 'ok');
      }
      paintFeed();
      paintCount();
      // Снимаем «Отправляется…» ДО onSent: страница может обновить данные и
      // пересобрать кнопки — блок обязан быть уже в спокойном состоянии.
      st.sending = false;
      syncLive();
      if (typeof o.onSent === 'function') await o.onSent();
    } catch (err) {
      st.thread = (st.thread || []).filter((m) => m.id !== localId);
      st.text = text;
      field.value = text;
      if (field !== ta) ta.value = text;
      showError(errorText(err).message + ' Текст остался в поле — попробуйте ещё раз, когда связь вернётся.');
      paintFeed();
      paintCount();
      try { field.focus(); } catch { /* нет DOM */ }
    } finally {
      st.sending = false;
      syncLive();
      autosize();
    }
  }

  sendBtn.addEventListener('click', () => { send('primary'); });

  ta.addEventListener('input', () => {
    st.text = ta.value;
    st.focus = true;
    if (st.error) showError('');
    autosize();
    syncButtons();          // пустое поле — «Отправить» неактивна
    // У-1: диктовка пишет в поле тем же `input`-событием — набранное не должно
    // оказаться в свёрнутом блоке (человек не увидел бы, что получилось).
    if (sec && !sec.isOpen()) sec.expand();
  });
  ta.addEventListener('focus', () => { st.focus = true; if (sec && !sec.isOpen()) sec.expand(); });
  ta.addEventListener('blur', () => { st.focus = false; });
  ta.addEventListener('keydown', (ev) => {
    if (!ev || !(ev.ctrlKey || ev.metaKey)) return;
    if (!(ev.key === 'Enter' || ev.keyCode === 13)) return;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    send('primary');
  });

  feed.addEventListener('scroll', () => {
    st.feedScroll = Number(feed.scrollTop) || 0;
    const total = Number(feed.scrollHeight) || 0;
    const seen = st.feedScroll + (Number(feed.clientHeight) || 0);
    st.atBottom = total - seen < 48;
  });

  /* ── сборка ────────────────────────────────────────────── */

  // W20-C1: `data-keep-tree` — метка «этот блок страница не пересобирает».
  // live-patch.js его не заменяет и внутрь не заходит; содержимое обновляет
  // `update(detail)`. Так живая кнопка и живое поле всегда из одной сборки.
  const hintRevise = h('div', { class: 'thr__hint thr__hint--revise hidden' },
    'Без переключателя сообщение только уточняет задачу; с ним — ещё и запускает доработку свода.');

  const form = h('div', { class: 'thr__form' },
    h('div', { class: 'thr__field' }, ta),
    filesHost,
    // Запасной путь: раскрывается сам, только если окно выбора недоступно.
    picker.row,
    h('div', { class: 'thr__bar' },
      clipBtn,
      h('div', { class: 'spacer' }),
      optsHost,
      sendBtn),
    hintRevise,
    h('div', { class: 'thr__hint' }, SEND_HINT),
    errLine);

  // У-1 (волна 21): «Диалог по запросу» по умолчанию СВЁРНУТ — один общий
  // компонент заголовка со стрелкой (section-collapse.js). Лента и поле ввода
  // строятся при первом раскрытии, но живут одним узлом: `data-keep-tree`
  // страница не пересобирает, а `update(detail)` обновляет данные.
  sec = createSection({
    key: 'thread',
    requestId: rid || String((detail && detail.id) || ''),
    title: 'Диалог по задаче',
    titleClass: 'thr__title',
    defaultOpen: false,
    className: 'thr',
    attrs: { 'data-thread': '', 'data-keep-tree': '', 'aria-labelledby': 'thr-title' },
    content: () => h('div', { class: 'thr__inner' }, feed, form),
    onToggle: (open) => {
      if (!open) return;
      markRead();
      // Пока блок был свёрнут, лента не собиралась — собираем сейчас.
      if (st.dirtyFeed) { paintFeed(); st.dirtyFeed = false; }
      // Пока блок был свёрнут, у ленты не было высоты: после раскрытия
      // ставим прокрутку на место и пересчитываем высоту поля.
      autosize();
      stick();
    },
  });
  const section = sec.element;
  // Сколько сообщений команды уже на экране: «новое» считается от этого набора.
  seenAgent = agentIds(st.thread);

  paintOptions();
  // У-1: свёрнутый диалог не строит ленту — 200 сообщений собираются в момент
  // раскрытия (и один раз: дальше обновляются на месте).
  if (sec.isOpen()) paintFeed();
  else st.dirtyFeed = true;
  paintFiles();
  paintCount();
  syncButtons();
  autosize();

  container.appendChild(section);

  // Прокрутка ленты: при первом показе и после нового сообщения — вниз,
  // иначе возвращаем то место, где человек читал (опрос не должен дёргать).
  // Повторяем на следующих кадрах: стили блока подключаются лениво, и до них
  // у ленты ещё нет своей высоты (одного микротаска мало).
  function stick() {
    try {
      feed.scrollTop = st.atBottom ? (Number(feed.scrollHeight) || 0) : (st.feedScroll || 0);
      if (!st.atBottom) feed.scrollTop = st.feedScroll || 0;
    } catch { /* нет прокрутки */ }
  }
  queueMicrotask(stick);
  if (typeof globalThis.requestAnimationFrame === 'function') {
    globalThis.requestAnimationFrame(() => { stick(); globalThis.requestAnimationFrame(stick); });
  }
  setTimeout(stick, 160);

  // Фокус: возвращаем только если он у нас был и страница перерисовалась
  // целиком (активный элемент — body), а не если человек ушёл в другое поле.
  if (st.focus) {
    queueMicrotask(() => {
      const d = globalThis.document;
      const active = d && d.activeElement;
      if (active && active !== d.body && active !== ta) return;
      try { ta.focus({ preventScroll: true }); } catch { /* нет DOM */ }
    });
  }

  /** Подпись ленты: видно ли человеку, что она изменилась (id, время, длинный текст). */
  function feedSignature(list) {
    return (Array.isArray(list) ? list : [])
      .map((m) => [m.id, m.at, String(m.text || '').length, m.pending ? 'p' : '',
        m.answer_state || m.delivery && m.delivery.state || '',
        Array.isArray(m.asks) ? m.asks.length : ''].join('~'))
      .join('|');
  }

  /**
   * Новые данные запроса (W20-C1, B-40) — страница зовёт это вместо повторного
   * `mountThread`. Лента и состояние кнопок обновляются на месте: поле, кнопка
   * «Отправить» и фокус остаются теми же узлами (их не заменяют).
   * @param {object} next  ответ `GET /api/requests/{id}`
   */
  function update(next) {
    if (!next || typeof next !== 'object') return;
    const nextStatus = String(next.status || '');
    if (nextStatus !== status) {
      status = nextStatus;
      paintOptions();          // переключатели зависят от статуса — и только они
      syncButtons();
    }
    if (Array.isArray(next.thread)) {
      const before = feedSignature(st.thread);
      const prevAgents = seenAgent;
      st.thread = mergeThreads(st.thread, next.thread);
      const nowAgents = agentIds(st.thread);
      seenAgent = nowAgents;
      if (feedSignature(st.thread) !== before) {
        // У-1: свёрнутый диалог не молчит о новом ответе руководителя —
        // считаем пришедшее и ставим метку в заголовке. Раскрытие её снимает.
        let added = 0;
        for (const id of nowAgents) if (!prevAgents.has(id)) added += 1;
        if (added > 0 && sec && !sec.isOpen()) unread += added;
        // Ленту свёрнутого блока не пересобираем: она соберётся при раскрытии.
        if (!sec || sec.isOpen()) paintFeed();
        else st.dirtyFeed = true;
        paintCount();
        if (sec && sec.isOpen()) stick();
      }
    }
    syncButtons();
  }

  return {
    element: section,
    update,
    /**
     * У-1: раскрыть диалог (страница зовёт из «Добавить сообщение руководителю»,
     * тесты — чтобы добраться до ленты). Выбор запоминается как выбор человека.
     */
    expand() {
      if (sec) sec.expand();
      return section;
    },
    /** Свёрнут ли блок сейчас (проверки). */
    isOpen() { return sec ? sec.isOpen() : true; },
    focus() {
      // У-1: «Добавить сообщение руководителю» из меню должно показать поле,
      // даже если диалог был свёрнут.
      if (sec && !sec.isOpen()) sec.expand();
      st.focus = true;
      try {
        if (typeof section.scrollIntoView === 'function') section.scrollIntoView({ block: 'center', behavior: 'smooth' });
      } catch { /* нет прокрутки */ }
      try { ta.focus({ preventScroll: false }); } catch { /* нет DOM */ }
    },
    destroy() {
      if (st.sync === syncButtons) st.sync = null;
      if (sec) sec.destroy();
      if (section && section.parentNode) section.parentNode.removeChild(section);
    },
  };
}

export default mountThread;
