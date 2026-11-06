/**
 * request-thread.js — блок «Диалог по запросу» на странице запроса.
 *
 * Лента: сообщения Михаила — справа и выделены; команда — слева, с аватаром и
 * ролью по-русски; системные события — тонкой серой строкой; между днями —
 * разделители. Поле ввода растёт, микрофон ставится сам (dictation-auto),
 * скрепка прикрепляет путь к файлу (pathChip, как в композере).
 *
 * Отправка — ОДНА кнопка «Отправить руководителю» (Ctrl+Enter):
 *   POST /api/requests/{rid}/messages {text, attach, urgent}
 * Сообщение сразу появляется в ленте (оптимистично), затем лента берётся из
 * ответа сервера. Ошибка сети — человеческий текст под полем, набранное
 * остаётся в поле. Вторая кнопка зависит от статуса запроса:
 *   готово  → «Отправить на доработку» (существующее действие revise);
 *   ошибка  → «Повторить с уточнением»;
 *   в работе → второй кнопки нет.
 *
 * Перерисовка страницы (опрос 2/10 с) не сбрасывает набранное, фокус и
 * прокрутку ленты: черновик и позиция живут в записи, привязанной к ctx.
 *
 * Публично: mountThread(container, ctx, {roomId, detail, rid, onSent}).
 */

import { h, fmtDate, fmtDateTime, toDate } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, avatar, pathChip, promptDialog } from '../ui.js';
import { countNoun, errorText } from '../text-ru.js';
import { profileName } from './request-util.js';

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
 * Роль участника для подписи: «biz-ops» → «операции», «mikhail» → «Михаил».
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
 * Вторая кнопка по статусу запроса; у идущей работы её нет.
 * @param {string} status
 * @returns {{key: string, label: string}|null}
 */
export function secondAction(status) {
  const s = String(status === null || status === undefined ? '' : status);
  if (s === 'done') return { key: 'revise', label: 'Отправить на доработку' };
  if (s === 'failed') return { key: 'retry', label: 'Повторить с уточнением' };
  return null;
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
      thread: [], text: '', attach: [], urgent: false,
      focus: false, feedScroll: 0, atBottom: true,
      sending: false, error: '', seq: 0,
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
  const status = String((detail && detail.status) || '');
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
    'aria-label': 'Диалог по запросу', tabindex: '0',
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
  });
  ta.value = st.text || '';

  const sendLabel = h('span', null, SEND_LABEL);
  const sendBtn = h('button', {
    class: 'btn btn--primary thr__send', type: 'button', 'data-action': 'send',
  }, icon('send', { size: 13 }), sendLabel);

  const second = secondAction(status);
  const secondBtn = second
    ? h('button', {
      class: 'btn btn--secondary thr__second', type: 'button', 'data-action': second.key,
    }, second.key === 'revise' ? icon('comment-draft', { size: 13 }) : icon('refresh', { size: 13 }), second.label)
    : null;

  const errLine = h('div', { class: 'thr__err hidden', role: 'alert' });
  const filesHost = h('div', { class: 'thr__files hidden' });
  const countNode = h('span', { class: 'chip chip--muted thr__count tnum hidden' });

  function showError(text) {
    st.error = String(text || '');
    setChildren(errLine);
    if (st.error) {
      errLine.appendChild(icon('error', { size: 12 }));
      errLine.appendChild(h('span', null, st.error));
    }
    errLine.classList.toggle('hidden', !st.error);
  }

  function paintFiles() {
    setChildren(filesHost);
    const list = Array.isArray(st.attach) ? st.attach : [];
    filesHost.classList.toggle('hidden', !list.length);
    for (const path of list) {
      filesHost.appendChild(h('span', { class: 'thr__file' },
        pathChip(path),
        h('button', {
          class: 'btn btn--ghost btn--icon btn--sm thr__unclip', type: 'button',
          title: 'Убрать файл', 'aria-label': 'Убрать файл',
          onclick: () => { st.attach = st.attach.filter((p) => p !== path); paintFiles(); },
        }, icon('close', { size: 11 }))));
    }
  }

  function paintCount() {
    const n = (st.thread || []).filter((m) => !isSystem(m)).length;
    countNode.textContent = n ? countNoun(n, ['сообщение', 'сообщения', 'сообщений']) : '';
    countNode.classList.toggle('hidden', !n);
  }

  function syncButtons() {
    sendBtn.disabled = Boolean(st.sending);
    sendLabel.textContent = st.sending ? 'Отправляю…' : SEND_LABEL;
    sendBtn.setAttribute('aria-busy', st.sending ? 'true' : 'false');
    if (secondBtn) secondBtn.disabled = Boolean(st.sending);
  }

  /**
   * Перерисовать кнопки у ЖИВОГО блока: страница по опросу перерисовывается
   * целиком и монтирует диалог заново, поэтому обновлять надо последнюю копию
   * (её syncButtons лежит в st.sync), а не свою — та может быть уже отсоединена.
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

  async function attachFile() {
    let path = '';
    try {
      path = await promptDialog({
        title: 'Прикрепить файл',
        label: 'Путь к файлу',
        placeholder: 'D:/Проекты/Комната/отчёт.md',
        submitLabel: 'Прикрепить',
        hint: 'Абсолютный путь на этом компьютере. Файл не копируется — руководитель откроет его по пути.',
      });
    } catch (err) {
      console.warn('[thread] прикрепление:', err);
      return;
    }
    const clean = String(path || '').trim();
    if (!clean) return;
    if (st.attach.includes(clean)) { toast('Этот файл уже прикреплён', 'info'); return; }
    if (st.attach.length >= 10) { toast('Больше 10 файлов к одному сообщению не приложить', 'warn'); return; }
    st.attach = [...st.attach, clean];
    paintFiles();
  }

  const clipBtn = h('button', {
    class: 'btn btn--ghost btn--icon thr__clip', type: 'button',
    title: 'Прикрепить файл', 'aria-label': 'Прикрепить файл',
    onclick: () => { attachFile(); },
  }, icon('attach', { size: 14 }));

  const urgentInput = h('input', { type: 'checkbox', 'data-urgent': '' });
  urgentInput.checked = Boolean(st.urgent);
  urgentInput.addEventListener('change', () => { st.urgent = Boolean(urgentInput.checked); });
  const urgentBox = FINAL.has(status)
    ? null
    : h('label', {
      class: 'checkbox thr__urgent',
      title: 'Руководитель комнаты пересмотрит оставшиеся шаги; начатую работу никто не останавливает',
    }, urgentInput, h('span', null, URGENT_LABEL));

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

  async function send(kind) {
    if (st.sending) return;
    const text = String(ta.value === undefined || ta.value === null ? '' : ta.value).trim();
    if (!text) {
      showError('');
      toast('Напишите сообщение — оно уйдёт руководителю комнаты', 'info');
      try { ta.focus(); } catch { /* нет DOM */ }
      return;
    }
    if (!rid) {
      showError('Не удалось отправить: в адресе нет номера запроса. Обновите страницу.');
      return;
    }

    if (kind === 'revise') {
      st.sending = true;
      syncButtons();
      try {
        await api.post(`/api/requests/${enc(rid)}/actions`, { action: 'revise', text });
        st.text = '';
        ta.value = '';
        showError('');
        toast('Ответ отправлен на доработку руководителю комнаты', 'ok');
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
    const local = {
      id: localId, at: Math.floor(Date.now() / 1000), author: 'mikhail', author_kind: 'mikhail',
      author_label: 'Михаил', text, task_id: null, kind: 'message', pending: true,
      attach: (st.attach || []).slice(),
    };
    st.thread = mergeThreads(st.thread, [local]);
    st.sending = true;
    st.text = '';
    ta.value = '';
    st.atBottom = true;
    showError('');
    paintFeed();
    paintCount();
    syncButtons();
    try { feed.scrollTop = feed.scrollHeight || 0; } catch { /* нет прокрутки */ }

    try {
      const body = { text, attach: (st.attach || []).slice() };
      if (st.urgent && !FINAL.has(status)) body.urgent = true;
      const res = await api.post(`/api/requests/${enc(rid)}/messages`, body);
      applyResponse(res, localId);
      st.attach = [];
      st.urgent = false;
      urgentInput.checked = false;
      paintFiles();
      toast(kind === 'retry'
        ? 'Повтор с уточнением отправлен руководителю комнаты'
        : 'Сообщение отправлено руководителю комнаты', 'ok');
      paintFeed();
      paintCount();
      // Снимаем «Отправляю…» ДО onSent: тот перерисовывает страницу целиком и
      // монтирует диалог заново, а новая кнопка читает st.sending. Иначе новый
      // блок рождается с «Отправляю…» и живёт так до следующей перерисовки.
      st.sending = false;
      syncLive();
      if (typeof o.onSent === 'function') await o.onSent();
    } catch (err) {
      st.thread = (st.thread || []).filter((m) => m.id !== localId);
      st.text = text;
      ta.value = text;
      showError(errorText(err).message + ' Текст остался в поле — попробуйте ещё раз, когда связь вернётся.');
      paintFeed();
      paintCount();
      try { ta.focus(); } catch { /* нет DOM */ }
    } finally {
      st.sending = false;
      syncLive();
      autosize();
    }
  }

  sendBtn.addEventListener('click', () => { send('primary'); });
  if (secondBtn) secondBtn.addEventListener('click', () => { send(second.key); });

  ta.addEventListener('input', () => {
    st.text = ta.value;
    st.focus = true;
    if (st.error) showError('');
    autosize();
  });
  ta.addEventListener('focus', () => { st.focus = true; });
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

  const section = h('section', { class: 'card thr', 'data-thread': '', 'aria-labelledby': 'thr-title' },
    h('div', { class: 'thr__head' },
      h('h2', { class: 'thr__title', id: 'thr-title' }, 'Диалог по запросу'),
      countNode),
    feed,
    h('div', { class: 'thr__form' },
      h('div', { class: 'thr__field' }, ta),
      filesHost,
      h('div', { class: 'thr__bar' },
        clipBtn,
        h('div', { class: 'spacer' }),
        urgentBox,
        secondBtn,
        sendBtn),
      h('div', { class: 'thr__hint' }, SEND_HINT),
      errLine));

  paintFeed();
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

  return {
    element: section,
    focus() {
      st.focus = true;
      try {
        if (typeof section.scrollIntoView === 'function') section.scrollIntoView({ block: 'center', behavior: 'smooth' });
      } catch { /* нет прокрутки */ }
      try { ta.focus({ preventScroll: false }); } catch { /* нет DOM */ }
    },
    destroy() {
      if (st.sync === syncButtons) st.sync = null;
      if (section && section.parentNode) section.parentNode.removeChild(section);
    },
  };
}

export default mountThread;
