/**
 * memory-search.js — поиск по памяти комнаты: «почему мы так решили» (W29B-Q8).
 *
 * Зачем отдельный экран, если есть поиск по запросам: человек спрашивает не
 * «где это слово», а «почему мы так решили». Поэтому в выдаче — цитата из
 * решения/свода/ответа/журнала/заметки, дата и подпись источника, а не список
 * похожих названий. Поиск живёт в двух местах:
 *
 *   • полем на «Сегодня» (этот файл, `createMemorySearch`);
 *   • разделом «Память» в командной палитре Ctrl+K (`palette.js` — те же данные
 *     через `/api/memory/search`, без второго запроса в разметке).
 *
 * Правила: пустой запрос ничего не ищет (и говорит об этом), ошибка сервера
 * показывает причину и что сделать, у каждой находки есть ссылка.
 */

import { h, debounce } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';

export const VIEW_CSS = 'css/view-memory.css';

/** Сколько находок показываем сразу (дальше — «показать все»). */
export const VISIBLE_LIMIT = 6;

/** Подписи видов памяти — один словарь для поля и палитры. */
export const KIND_TITLES = {
  decision: 'Решение',
  svod: 'Свод',
  answer: 'Ответ',
  journal: 'Журнал',
  note: 'Заметка',
};

/**
 * Строки выдачи: только то, что нужно разметке (никакого разбора payload в виде).
 * @param {object} payload ответ `/api/memory/search`
 * @returns {Array<{kind, kindTitle, title, snippet, citation, at, room, route, requestId}>}
 */
export function resultRows(payload) {
  const items = Array.isArray(payload && payload.items) ? payload.items : [];
  return items.filter((item) => item && typeof item === 'object').map((item) => ({
    kind: String(item.kind || ''),
    kindTitle: KIND_TITLES[String(item.kind || '')] || 'Память',
    title: String(item.title || ''),
    snippet: String(item.snippet || ''),
    citation: String(item.citation || ''),
    at: item.at || null,
    room: String(item.room || ''),
    requestId: String(item.request_id || ''),
    route: String(item.route || ''),
  }));
}

/**
 * Строка статуса под полем: сколько нашлось и чего не хватает.
 * @param {string} query
 * @param {object} payload
 * @param {boolean} [pending]
 * @returns {{text: string, tone: 'muted'|'warn'|'fail'}}
 */
export function statusText(query, payload, pending = false) {
  const q = String(query || '').trim();
  if (!q) return { text: 'Например: «почему выбрали подрядчика» — ищем в решениях, сводах, ответах и журнале.', tone: 'muted' };
  if (q.length < 2) return { text: 'Ещё одна буква — и начнём искать.', tone: 'muted' };
  if (pending) return { text: 'Ищем в памяти комнаты…', tone: 'muted' };
  const total = Number((payload && payload.total) || 0);
  if (!total) {
    return { text: String((payload && payload.note) || 'В памяти ничего не нашлось — попробуйте другое слово.'), tone: 'warn' };
  }
  const byKind = (payload && payload.by_kind) || {};
  const parts = Object.keys(byKind)
    .filter((key) => Number(byKind[key]) > 0)
    .map((key) => (KIND_TITLES[key] || key) + ': ' + byKind[key]);
  return { text: 'Нашлось: ' + total + (parts.length ? ' (' + parts.join(', ') + ')' : ''), tone: 'muted' };
}

/** Сколько скрыто под «показать все». */
export function hiddenCount(payload, limit = VISIBLE_LIMIT) {
  const rows = resultRows(payload);
  return Math.max(0, rows.length - Math.max(1, Number(limit) || VISIBLE_LIMIT));
}

/* ───────────────────────────────────────────────────────────────── разметка */

function resultNode(row, query) {
  const title = h('span', { class: 'mem-item__kind chip chip--outline' }, row.kindTitle);
  const head = h('div', { class: 'mem-item__head' },
    title,
    h('span', { class: 'mem-item__title' }, row.title));
  const quote = h('p', { class: 'mem-item__snippet' }, row.snippet || row.title);
  const foot = h('div', { class: 'mem-item__foot small muted' },
    h('span', { class: 'mem-item__citation' }, row.citation),
    row.route
      ? h('a', { class: 'mem-item__open', href: row.route, title: 'Открыть, где это записано' },
        'Открыть', icon('arrow-right', { size: 11 }))
      : null);
  return h('a', {
    class: 'mem-item',
    href: row.route || '#/today',
    'data-kind': row.kind,
    'data-key': 'mem|' + row.kind + '|' + row.room + '|' + row.title.slice(0, 24),
    'aria-label': row.kindTitle + ': ' + row.title,
  }, head, quote, foot);
}

/**
 * Поле поиска по памяти с результатами.
 *
 * @param {object} opts {api, toast?, room?, limit?, title?, placeholder?}
 * @returns {{element: HTMLElement, destroy: Function, search: Function}}
 */
export function createMemorySearch(opts = {}) {
  const api = opts.api;
  const toast = typeof opts.toast === 'function' ? opts.toast : () => {};
  const limit = Number(opts.limit) || 20;
  const room = opts.room ? String(opts.room) : '';

  let query = '';
  let payload = null;
  let pending = false;
  let expanded = false;
  let stopped = false;
  let seq = 0;
  let controller = null;

  const input = h('input', {
    class: 'input mem-search__input',
    type: 'search',
    placeholder: opts.placeholder || 'Почему мы так решили? Например: «подрядчик», «срок»',
    'aria-label': 'Поиск по памяти комнаты',
    autocomplete: 'off',
    spellcheck: 'false',
  });
  const status = h('p', { class: 'mem-search__status small muted', 'aria-live': 'polite' });
  const results = h('div', { class: 'mem-search__results' });

  const element = h('div', { class: 'mem-search', 'data-key': 'memory-search' },
    h('div', { class: 'mem-search__head' },
      h('span', { class: 'mem-search__icon', 'aria-hidden': 'true' }, icon('search', { size: 14 })),
      input,
      opts.title ? h('span', { class: 'mem-search__title small muted' }, String(opts.title)) : null),
    status, results);

  function render() {
    if (stopped) return;
    const state = statusText(query, payload, pending);
    status.textContent = state.text;
    status.classList.toggle('is-warn', state.tone === 'warn');
    status.classList.toggle('is-fail', state.tone === 'fail');
    const rows = resultRows(payload);
    if (!query || !rows.length) {
      results.replaceChildren();
      return;
    }
    const shown = expanded ? rows : rows.slice(0, VISIBLE_LIMIT);
    const nodes = shown.map((row) => resultNode(row, query));
    const rest = rows.length - shown.length;
    if (rest > 0) {
      nodes.push(h('button', {
        class: 'btn btn--ghost btn--sm mem-search__more',
        type: 'button',
        onclick: () => { expanded = true; render(); },
      }, 'Показать все ' + rows.length));
    }
    results.replaceChildren(...nodes);
  }

  async function search(text) {
    query = String(text || '').trim();
    expanded = false;
    if (query.length < 2) {
      payload = null;
      pending = false;
      render();
      return null;
    }
    const my = ++seq;
    pending = true;
    render();
    if (controller) { try { controller.abort(); } catch { /* ignore */ } }
    controller = typeof AbortController === 'function' ? new AbortController() : null;
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    if (room) params.set('room', room);
    try {
      const got = await api.get('/api/memory/search?' + params.toString(),
        controller ? { signal: controller.signal } : undefined);
      if (stopped || my !== seq) return null;
      payload = got || null;
      pending = false;
      render();
      return payload;
    } catch (err) {
      if (stopped || my !== seq) return null;
      if (err && err.name === 'AbortError') return null;
      pending = false;
      payload = null;
      const message = String((err && err.message) || 'Поиск не ответил');
      status.textContent = 'Поиск по памяти не сработал: ' + message + '. Попробуйте ещё раз.';
      status.classList.add('is-fail');
      results.replaceChildren();
      toast('Поиск по памяти не сработал: ' + message, 'warn');
      return null;
    }
  }

  const debounced = debounce((text) => { search(text); }, 220);
  input.addEventListener('input', () => {
    query = input.value.trim();
    render();
    debounced(query);
  });
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      search(input.value);
    }
  });

  render();

  return {
    element,
    search,
    destroy() {
      stopped = true;
      if (controller) { try { controller.abort(); } catch { /* ignore */ } }
    },
  };
}

/** Подключить стили поля (вызывается из вида «Сегодня»). */
export async function ensureStyles() {
  await ensureCss(VIEW_CSS);
}

export default { createMemorySearch, resultRows, statusText, hiddenCount, ensureStyles, VIEW_CSS };
