/**
 * search.js — глобальный поиск (#/search?q=).
 *
 * Поле с debounce и автофокусом, фильтр по комнате, результаты группами по типу
 * (запросы / решения / бэклог / журнал / отчёты), подсветка совпадений через
 * <mark>, построенный h() — без innerHTML. Клавиатура: ↑↓, Enter, Home/End.
 */

import { h, debounce, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, emptyState, skeleton, errorBanner, segmented } from '../ui.js';
import { sectionForKind, routeForHit, highlightSegments } from './palette-model.js';

const VIEW_CSS = 'css/view-search.css';

/** Порядок групп в выдаче. */
const KIND_ORDER = ['request', 'decision', 'backlog', 'journal', 'report'];

const EXAMPLES = ['Роспатент', 'упаковка', 'критик', 'юнит-экономика'];

/** Текст с подсветкой токенов запроса: <mark> собирается узлами. */
function highlighted(text, query) {
  const frag = document.createDocumentFragment();
  for (const seg of highlightSegments(text, query)) {
    if (seg.match) frag.appendChild(h('mark', { class: 'srch-mark' }, seg.text));
    else frag.appendChild(document.createTextNode(seg.text));
  }
  return frag;
}

/** Заголовок группы результатов с числом. */
function groupHead(id, title, count) {
  return h('div', { class: 'srch-group__head', id: 'srch-' + id },
    h('h3', { class: 'srch-group__title' }, title),
    h('span', { class: 'chip chip--muted tnum' }, String(count)),
  );
}

async function mountSearch(el, ctx) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  const roomsStore = ctx.store && ctx.store.rooms;
  const roomList = (roomsStore && roomsStore.get && roomsStore.get('list')) || [];
  const initialQuery = String((ctx.params && ctx.params.q) || '');

  const state = {
    q: initialQuery,
    room: '',
    items: [],
    total: 0,
    loading: false,
    error: null,
    answered: false,
  };
  let seq = 0;
  let controller = null;
  let stopped = false;

  const root = h('div', { class: 'view__inner srch' });
  const input = h('input', {
    class: 'input srch-input',
    type: 'search',
    placeholder: 'Что искать: запрос, решение, шаг, событие журнала…',
    'aria-label': 'Поисковый запрос',
    autocomplete: 'off',
    spellcheck: 'false',
    enterkeyhint: 'search',
  });
  input.value = initialQuery;

  const form = h('form', { class: 'srch-form', role: 'search', onsubmit: (e) => { e.preventDefault(); run({ immediate: true }); } },
    h('div', { class: 'search-field grow' }, icon('search', { size: 14 }), input),
    h('button', { class: 'btn btn--primary', type: 'submit' }, 'Найти'),
  );

  const filterRow = h('div', { class: 'srch-filter row row--between row--wrap' });
  const metaHost = h('div', { class: 'srch-meta', 'aria-live': 'polite' });
  const results = h('div', { class: 'srch-results' });

  root.appendChild(form);

  if (roomList.length) {
    const options = [{ value: '', label: 'Все комнаты' }].concat(
      roomList.map((r) => ({ value: r.id, label: r.title || r.id })),
    );
    const seg = segmented({
      options,
      value: '',
      onchange: (value) => {
        state.room = String(value || '');
        run({ immediate: true });
      },
    });
    filterRow.appendChild(h('div', { class: 'row row--tight srch-filter__rooms' },
      h('span', { class: 'small muted' }, 'Комната:'), seg));
  }
  filterRow.appendChild(h('span', { class: 'srch-filter__hint small muted' },
    'Ищем по запросам, ответам, решениям, шагам и журналу'));
  root.appendChild(filterRow);
  root.appendChild(metaHost);
  root.appendChild(results);
  el.appendChild(root);

  /* ------------------------------------------------------------ маршрут */

  function syncHash() {
    const q = state.q;
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (state.room) params.set('room', state.room);
    const next = '#/search' + (params.toString() ? '?' + params.toString() : '');
    try { history.replaceState(null, '', next); } catch { /* file:// или старый браузер */ }
  }

  /* -------------------------------------------------------------- выдача */

  function renderMeta() {
    metaHost.replaceChildren();
    if (state.error) return;
    if (state.loading && !state.answered) {
      metaHost.appendChild(h('span', { class: 'small muted' },
        h('span', { class: 'codicon codicon-sync codicon-modifier-spin', 'aria-hidden': 'true' }), ' Ищем…'));
      return;
    }
    if (!state.answered) return;
    const roomName = state.room
      ? ((roomList.find((r) => r.id === state.room) || {}).title || state.room)
      : 'все комнаты';
    const found = state.total || state.items.length;
    const suffix = state.loading ? ' · обновляем…' : '';
    metaHost.appendChild(h('span', { class: 'small muted' },
      (found ? 'Найдено ' + String(found) + ' · ' + roomName
        : 'Ничего не найдено · ' + roomName) + suffix));
    if (found > 0) {
      metaHost.appendChild(h('span', { class: 'spacer' }));
      metaHost.appendChild(h('span', { class: 'small muted' }, '↑↓ — выбор, Enter — открыть'));
    }
  }

  function emptyQueryState() {
    const box = emptyState({
      icon: 'search',
      title: 'Что найти?',
      text: 'Поиск идёт по запросам и ответам, решениям, шагам «Дальше», журналу и отчётам. '
        + 'Начните вводить — или выберите пример ниже.',
    });
    const chips = h('div', { class: 'srch-examples' });
    for (const example of EXAMPLES) {
      chips.appendChild(h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => { input.value = example; state.q = example; run({ immediate: true }); input.focus(); },
      }, example));
    }
    box.appendChild(chips);
    return box;
  }

  function shortQueryState() {
    return emptyState({
      icon: 'search',
      title: 'Ещё пара букв',
      text: 'Поиск принимает запросы от двух символов — так находится точнее.',
    });
  }

  function noResultsState() {
    return emptyState({
      icon: 'inbox',
      title: 'Ничего не нашлось',
      text: 'Попробуйте короче или другими словами: поиск ищет по началу слов, без морфологии. '
        + 'Можно снять фильтр по комнате.',
      action: state.room
        ? { label: 'Искать во всех комнатах', onclick: () => { state.room = ''; el.querySelectorAll('.segmented__btn').forEach((b) => { if (b.dataset.value === '') b.click(); }); } }
        : undefined,
    });
  }

  function hitRow(hit) {
    const title = hit.title || hit.id || 'Совпадение';
    const snippet = hit.snippet || '';
    const href = routeForHit(hit);
    const row = h('a', { class: 'srch-hit', href, dataset: { kind: hit.kind || '' } });
    const badge = h('span', { class: 'srch-hit__icon', 'aria-hidden': 'true' },
      icon(hit.kind === 'report' ? 'report' : hit.kind === 'decision' ? 'law'
        : hit.kind === 'journal' ? 'history' : hit.kind === 'backlog' ? 'arrow-circle-right' : 'comment-discussion', { size: 14 }));

    const main = h('div', { class: 'srch-hit__main' },
      h('div', { class: 'srch-hit__title' }, highlighted(title, state.q)),
      snippet ? h('div', { class: 'srch-hit__snippet' }, highlighted(snippet, state.q)) : null,
      h('div', { class: 'srch-hit__meta small muted' },
        hit.room ? h('span', { class: 'chip chip--outline' }, String(hit.room)) : null,
        hit.at ? h('span', { title: 'Когда' }, timeAgo(hit.at)) : null,
      ),
    );
    row.appendChild(badge);
    row.appendChild(main);
    row.appendChild(h('span', { class: 'srch-hit__go', 'aria-hidden': 'true' }, icon('chevron-right', { size: 14 })));
    return row;
  }

  function renderResults() {
    results.replaceChildren();
    if (state.error) {
      results.appendChild(errorBanner(state.error, () => run({ immediate: true })));
      return;
    }
    if (state.loading && !state.answered) {
      results.appendChild(skeleton(5));
      return;
    }
    if (!state.answered) {
      results.appendChild(emptyQueryState());
      return;
    }
    if ((state.q || '').length < 2) {
      results.appendChild(shortQueryState());
      return;
    }
    if (!state.items.length) {
      results.appendChild(noResultsState());
      return;
    }

    const buckets = new Map();
    for (const hit of state.items) {
      const section = sectionForKind(hit.kind);
      if (!buckets.has(section.id)) buckets.set(section.id, { id: section.id, title: section.title, items: [] });
      buckets.get(section.id).items.push(hit);
    }
    const ordered = [...buckets.values()].sort((a, b) => {
      const ai = KIND_ORDER.indexOf(a.id.replace('hits:', ''));
      const bi = KIND_ORDER.indexOf(b.id.replace('hits:', ''));
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
    });
    for (const bucket of ordered) {
      const section = h('section', { class: 'srch-section' });
      section.appendChild(groupHead(bucket.id.replace('hits:', ''), bucket.title, bucket.items.length));
      const list = h('div', { class: 'srch-list', role: 'list' });
      for (const hit of bucket.items) {
        const row = hitRow(hit);
        row.setAttribute('role', 'listitem');
        list.appendChild(row);
      }
      section.appendChild(list);
      results.appendChild(section);
    }
  }

  function renderAll() {
    renderMeta();
    renderResults();
  }

  async function run() {
    state.q = String(input.value || '').trim();
    syncHash();
    if (state.q.length < 2) {
      state.loading = false;
      state.error = null;
      state.answered = true;
      state.items = [];
      state.total = 0;
      renderAll();
      return;
    }
    const my = ++seq;
    state.loading = true;
    state.error = null;
    renderAll();

    if (controller) { try { controller.abort(); } catch { /* ignore */ } }
    controller = typeof AbortController === 'function' ? new AbortController() : null;

    const params = new URLSearchParams({ q: state.q });
    if (state.room) params.set('room', state.room);
    try {
      const payload = await api.get('/api/search?' + params.toString(), controller ? { signal: controller.signal } : undefined);
      if (stopped || my !== seq) return;
      state.items = Array.isArray(payload && payload.items) ? payload.items : [];
      state.total = Number(payload && payload.total) || state.items.length;
      state.loading = false;
      state.answered = true;
      state.error = null;
    } catch (err) {
      if (stopped || my !== seq) return;
      if (err && err.name === 'AbortError') return;
      state.loading = false;
      state.answered = true;
      state.items = [];
      state.total = 0;
      state.error = err;
      if (err && err.code === 'not_found' && state.room) {
        state.room = '';
        if (ctx.toast) ctx.toast('Комната не найдена — ищем по всем', 'warn');
      }
    }
    renderAll();
  }

  const debounced = debounce(() => run(), 260);
  input.addEventListener('input', () => { state.q = input.value; debounced(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); run({ immediate: true }); return; }
    if (e.key === 'ArrowDown') {
      const first = results.querySelector('.srch-hit');
      if (first) { e.preventDefault(); first.focus(); }
    }
  });
  results.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return;
    const rows = Array.from(results.querySelectorAll('.srch-hit'));
    if (!rows.length) return;
    const at = rows.indexOf(document.activeElement);
    let next = -1;
    if (e.key === 'ArrowDown') next = at < 0 ? 0 : Math.min(rows.length - 1, at + 1);
    else if (e.key === 'ArrowUp') next = at <= 0 ? 0 : at - 1;
    else if (e.key === 'Home') next = 0;
    else next = rows.length - 1;
    if (next < 0) return;
    e.preventDefault();
    rows[next].focus();
  });

  // Первый показ: запрос из адреса, фокус в поле.
  if (state.q) {
    run({ immediate: true });
  } else {
    state.answered = false;
    renderAll();
  }
  try {
    input.focus({ preventScroll: true });
    const len = input.value.length;
    input.setSelectionRange(len, len);
  } catch { /* не критично */ }

  return {
    unmount() {
      stopped = true;
      seq++;
      if (controller) { try { controller.abort(); } catch { /* ignore */ } }
      root.remove();
    },
  };
}

/**
 * @param {HTMLElement} el
 * @param {object} ctx
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  return mountSearch(el, ctx);
}

export default mount;
