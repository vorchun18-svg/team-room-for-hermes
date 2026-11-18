/**
 * quick-capture.js — постоянная полоса «Надиктовать» внизу экранов комнаты (W26, D9).
 *
 * Что делает:
 *  - полоса видна на всех экранах комнаты (вкладки, страница запроса) и скрыта
 *    на «Сегодня», в настройках и в поиске — там она была бы лишней;
 *  - микрофон — СУЩЕСТВУЮЩАЯ диктовка (`createMic` из dictation.js), ничего не
 *    дублируем и не переписываем: текст встаёт в поле по курсору;
 *  - текст уходит во «Входящие комнаты» (`POST /api/rooms/{id}/inbox`),
 *    источник пишется честно: «диктовка», если в этой записи был микрофон,
 *    иначе «вручную»;
 *  - клавиша `N` (и русская `Т`) ставит курсор в поле; `Ctrl+Enter` — записать,
 *    `Enter` — записать, `Shift+Enter` — перенос строки, `Esc` — свернуть поле;
 *  - рядом ссылка «Входящие» с числом: счётчик общий с вкладкой (бейдж в шапке),
 *    обновляется после каждой записи.
 *
 * Публично:
 *   installQuickCapture({app, host, api, toast, getRoute, navigate}) → ручка
 *   captureBadge(roomId) → узел бейджа для вкладки «Входящие» (0 — спрятан)
 *   captureCount(roomId) → последнее известное число (для тестов/шапки)
 *   markDictated(state) — пометить, что микрофон в этой записи использован
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { ensureCss } from './ui.js';
import { createMic } from './dictation.js';

/** Счётчики по комнатам: `roomId → {active, at}`. */
const counts = new Map();
/** Живые бейджи: `roomId → Set<element>` — один счётчик, много мест. */
const badges = new Map();
/** Сколько живёт счётчик, прежде чем перечитать (мс). */
const COUNT_TTL = 20000;

function rememberBadge(roomId, el) {
  let set = badges.get(roomId);
  if (!set) { set = new Set(); badges.set(roomId, set); }
  set.add(el);
  return () => { set.delete(el); };
}

function paint(roomId, n) {
  const set = badges.get(roomId);
  if (!set) return;
  const value = Math.max(0, Number(n) || 0);
  for (const el of set) {
    try {
      el.textContent = value ? String(value) : '';
      if (value) el.setAttribute('title', `${value} ${value === 1 ? 'запись ждёт' : 'записей ждут'} разбора`);
      else el.setAttribute('title', '');
      if (value) el.classList.remove('hidden');
      else el.classList.add('hidden');
    } catch { /* узел мог уйти из документа */ }
  }
}

/** Последнее известное число записей, ждущих разбора (0 — не знаем/пусто). */
export function captureCount(roomId) {
  const row = counts.get(String(roomId || ''));
  return row ? Number(row.active) || 0 : 0;
}

/**
 * Бейдж для вкладки «Входящие». Возвращает узел всегда: число 0 — бейдж скрыт,
 * но остаётся в разметке, чтобы обновиться без перерисовки шапки.
 */
export function captureBadge(roomId) {
  const id = String(roomId || '');
  const n = captureCount(id);
  const el = h('span', {
    class: 'tab__count count-pill' + (n ? '' : ' hidden'),
    'data-capture-badge': 'true',
    'data-room': id,
  }, n ? String(n) : '');
  if (n) el.setAttribute('title', `${n} ${n === 1 ? 'запись ждёт' : 'записей ждут'} разбора`);
  rememberBadge(id, el);
  return el;
}

/** Пометить источник записи как «диктовка» (микрофон действительно использован). */
export function markDictated(state) {
  if (state && typeof state === 'object') state.dictated = true;
  return state;
}

/** Похоже ли, что фокус в поле ввода (тогда горячие клавиши не перехватываем). */
function inField(node) {
  if (!node || node.nodeType !== 1) return false;
  const tag = String(node.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
  if (node.getAttribute && node.getAttribute('contenteditable') === 'true') return true;
  return Boolean(node.closest && node.closest('[contenteditable="true"]'));
}

/**
 * Поставить полосу «Надиктовать» в каркас приложения.
 *
 * @param {{app:HTMLElement, host?:HTMLElement, api:object, toast?:Function,
 *          getRoute?:Function, navigate?:Function}} deps  host — колонка, к низу
 *   которой крепится полоса (по умолчанию `.app__main`, иначе app).
 * @returns {{sync:Function, destroy:Function, submit:Function, focus:Function,
 *            state:object, bar:HTMLElement}}
 */
export function installQuickCapture(deps = {}) {
  ensureCss('css/view-inbox.css');
  const app = deps.app;
  const api = deps.api || { post() {}, get() {} };
  const toast = typeof deps.toast === 'function' ? deps.toast : () => {};
  const go = typeof deps.navigate === 'function' ? deps.navigate : (hash) => { location.hash = hash; };
  const getRoute = typeof deps.getRoute === 'function' ? deps.getRoute : () => ({});
  const doc = (typeof document !== 'undefined' ? document : null);

  const state = { roomId: '', dictated: false, busy: false, error: '' };

  const field = h('textarea', {
    id: 'quick-capture-text',
    class: 'capture__input',
    rows: '1',
    maxlength: '4000',
    'data-mic': 'manual',            // микрофон ставим сами: второй не нужен
    'aria-label': 'Быстрая запись во входящие комнаты',
    placeholder: 'Мысль, поручение, вопрос — надиктуйте или наберите. N — к записи, Ctrl+Enter — сохранить.',
  });

  const saveBtn = h('button', {
    class: 'btn btn--primary capture__save', type: 'button', 'data-capture-save': 'true',
    title: 'Записать во входящие (Ctrl+Enter)',
  }, icon('check', { size: 13 }), 'Записать');

  const inboxBadge = h('span', { class: 'count-pill hidden', 'data-capture-badge': 'true' }, '');
  const inboxLink = h('a', {
    class: 'capture__inbox',
    href: '#',
    title: 'Входящие комнаты: разобрать записи',
    onclick: (ev) => {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      if (state.roomId) go(`#/room/${encodeURIComponent(state.roomId)}/inbox`);
    },
  }, icon('inbox', { size: 13 }), 'Входящие', inboxBadge);

  const status = h('span', { class: 'capture__status', 'aria-live': 'polite' }, '');

  const foot = h('div', { class: 'capture__foot hidden' }, status);
  const row = h('div', { class: 'capture__row' },
    h('span', {
      class: 'capture__label',
      title: 'Запись попадает во «Входящие» комнаты: разобрать можно потом — '
        + 'в дело, шаг, вопрос, дежурство или справку.',
    }, icon('lightbulb', { size: 13 }), 'Надиктовать'),
    h('div', { class: 'capture__field' }, field),
    saveBtn,
    inboxLink,
  );
  const bar = h('div', {
    class: 'capture hidden', 'data-capture': 'true', role: 'region',
    'aria-label': 'Быстрая запись во входящие комнаты',
  },
  row,
  foot);

  // Микрофон — существующая диктовка. Она сама вставляет текст в поле по курсору
  // и сообщает об этом через onInsert: источник записи станет «диктовка».
  let mic = null;
  try {
    mic = createMic(field, {
      label: true,
      className: 'mic-btn--action',
      toast,
      onInsert: () => { markDictated(state); say('Надиктовано — проверьте текст и запишите.'); },
    });
    if (mic && typeof row.insertBefore === 'function') row.insertBefore(mic, saveBtn);
  } catch (err) {
    // Микрофона может не быть (нет прав, старый браузер) — текст всё равно работает.
    mic = null;
  }

  function setBarVisible(show) {
    bar.classList.toggle('hidden', !show);
  }

  /** Строка состояния нужна только когда есть что сказать — иначе полоса ниже. */
  function say(text) {
    status.textContent = String(text || '');
    foot.classList.toggle('hidden', !status.textContent);
  }

  function roomOf(route) {
    const r = route || getRoute() || {};
    if (r.name === 'room' || r.name === 'request') return String(r.roomId || '');
    return '';
  }

  function bindRoom(roomId) {
    if (roomId === state.roomId) return;
    const previous = badges.get(state.roomId);
    if (previous) previous.delete(inboxBadge);
    state.roomId = roomId;
    if (roomId) rememberBadge(roomId, inboxBadge);
    paint(roomId, captureCount(roomId));
  }

  async function refreshCount() {
    const roomId = state.roomId;
    if (!roomId || typeof api.get !== 'function') return;
    const row0 = counts.get(roomId);
    if (row0 && Date.now() - Number(row0.at || 0) < COUNT_TTL) { paint(roomId, row0.active); return; }
    try {
      const data = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/inbox?status=active`);
      const active = Number((data && data.counts && data.counts.active) || 0);
      counts.set(roomId, { active, at: Date.now() });
      paint(roomId, active);
    } catch (err) {
      // Молчим: полоса записи не должна ругаться из-за счётчика.
    }
  }

  async function submit() {
    if (state.busy) return false;
    const text = String(field.value || '').trim();
    if (!text) {
      say('Пусто: надиктуйте или наберите текст.');
      try { field.focus(); } catch { /* ignore */ }
      return false;
    }
    const roomId = state.roomId;
    if (!roomId) return false;
    const source = state.dictated ? 'dictation' : 'manual';
    state.busy = true;
    saveBtn.disabled = true;
    say('Сохраняю…');
    try {
      const data = await api.post(`/api/rooms/${encodeURIComponent(roomId)}/inbox`, { text, source });
      field.value = '';
      state.dictated = false;
      const active = Number((data && data.counts && data.counts.active) || 0);
      counts.set(roomId, { active, at: Date.now() });
      paint(roomId, active);
      say('Записано во входящие — разобрать можно на вкладке «Входящие».');
      toast('Записано во «Входящие» комнаты', 'success');
      return true;
    } catch (err) {
      const message = String((err && err.message) || 'не удалось записать');
      say('Не записано: ' + message);
      toast('Не удалось записать во входящие: ' + message, 'error');
      return false;
    } finally {
      state.busy = false;
      saveBtn.disabled = false;
    }
  }

  saveBtn.addEventListener('click', () => { submit(); });
  field.addEventListener('keydown', (ev) => {
    const key = ev && ev.key;
    if (key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      submit();
    } else if (key === 'Escape') {
      field.blur();
    }
  });
  field.addEventListener('input', () => {
    say('');
    // Одна строка растёт до трёх: полоса не должна съедать экран.
    try {
      const lines = String(field.value || '').split('\n').length;
      field.rows = String(Math.min(3, Math.max(1, lines)));
    } catch { /* ignore */ }
  });

  let keyHandler = null;
  if (doc && typeof doc.addEventListener === 'function') {
    keyHandler = (ev) => {
      if (!ev || ev.defaultPrevented) return;
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      if (inField(ev.target) || inField(doc.activeElement)) return;
      const key = ev.key;
      if (key === 'n' || key === 'N' || key === 'т' || key === 'Т') {
        if (bar.classList.contains('hidden')) return;
        if (ev.preventDefault) ev.preventDefault();
        try { field.focus(); } catch { /* ignore */ }
      }
    };
    doc.addEventListener('keydown', keyHandler);
  }

  const host = (deps.host && typeof deps.host.appendChild === 'function' ? deps.host : app);
  if (host && typeof host.appendChild === 'function') host.appendChild(bar);

  function sync(route) {
    const roomId = roomOf(route);
    setBarVisible(Boolean(roomId));
    bindRoom(roomId);
    if (roomId) refreshCount();
  }

  function destroy() {
    if (doc && keyHandler && typeof doc.removeEventListener === 'function') {
      doc.removeEventListener('keydown', keyHandler);
    }
    const set = badges.get(state.roomId);
    if (set) set.delete(inboxBadge);
    if (bar.parentNode) bar.parentNode.removeChild(bar);
    if (mic && typeof mic.destroy === 'function') { try { mic.destroy(); } catch { /* ignore */ } }
  }

  return { sync, destroy, submit, focus: () => field.focus(), state, bar, field };
}

export default installQuickCapture;
