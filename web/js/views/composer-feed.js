/**
 * views/composer-feed.js — лента запросов комнаты и блок «Найденные на доске
 * запросы». Вынесено из composer.js: поле и отправка — там, лента — здесь.
 *
 * Данные: GET /api/rooms/{id}/requests?limit=50, GET /api/requests/{rid},
 * GET /api/rooms/{id}/candidates, POST /api/adopt,
 * POST /api/requests/{rid}/actions (clone|archive).
 *
 * DOM ленты перерисовывается только при изменении подписи данных, фокус и
 * открытое меню сохраняются.
 */

import { h, timeAgo, fmtDuration, debounce, plural, fromForm, plainTerms } from '../util.js';
import { icon } from '../icons.js';
import { mdToText } from '../md.js';
import {
  statusChip, emptyState, skeleton, errorBanner, sectionHeader, segmented,
  confirmDialog, iconButton,
} from '../ui.js';
import { briefBadge, briefSummaryChip, summaryOf } from './plan-req.js';
import { titleShortOf, titleHint } from '../request-title.js';
import { skeletonCards } from '../shell-skeleton.js';

const ACTIVE = new Set(['working', 'reviewing', 'synthesizing']);

const FILTERS = [
  { value: 'all', label: 'Все' },
  { value: 'active', label: 'В работе' },
  { value: 'needs', label: 'Ждут вас' },
  { value: 'done', label: 'Готово' },
];

/** Примеры формулировок для пустой ленты (по комнате). */
const EXAMPLES = {
  cafe: [
    'Собери себестоимость набора из 6 моти по каналам и предложи цену продажи',
    'Что мешает запуску сейчас: разбери блокеры по этапам и скажи, что делать первым',
    'Сравни «Едок» и «Развоз» для первых 100 наборов: комиссии, охват, сроки',
  ],
  fn: [
    'Подготовь план вывода продукта на рынок на 30 дней и проверь риски',
    'Разбери договор с поставщиком и выдели пункты, которые нам невыгодны',
    'Собери факты по проекту и скажи, где мы сейчас и чего не хватает',
  ],
  _: [
    'Разбери текущее состояние проекта и скажи, где мы сейчас',
    'Что мешает двигаться дальше: три главных блокера и что с ними делать',
    'Составь план на две недели с ответственными и сроками',
  ],
};

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/** Детали запросов (начало ответа, активные исполнители, версия) — кэш. */
const detailCache = new Map();
const detailInflight = new Set();

function detailKey(item) {
  const p = item && item.progress ? item.progress : {};
  return [item && item.status, p.done, p.total, item && item.finished_at, item && item.needs_you].join('|');
}

function activeNames(detail) {
  const nodes = Array.isArray(detail && detail.tree) ? detail.tree : [];
  const names = [];
  for (const n of nodes) {
    if (!n || n.role === 'root') continue;
    if (!['ready', 'running', 'review'].includes(String(n.status || ''))) continue;
    const who = n.assignee ? String(n.assignee) : '';
    if (who && !names.includes(who)) names.push(who);
  }
  return names.slice(0, 3);
}

/** «biz-ops» → «ops»: в ленте не должно быть системных имён. */
function personName(id) {
  const raw = String(id == null ? '' : id).trim();
  if (!raw) return '';
  if (raw === 'mikhail') return 'Михаил';
  return raw.replace(/^(?:biz|room)-/, '');
}

function escapeSel(value) {
  const s = String(value == null ? '' : value);
  if (typeof CSS !== 'undefined' && CSS && typeof CSS.escape === 'function') return CSS.escape(s);
  return s.replace(/["\\\]]/g, '\\$&');
}

/**
 * Собрать ленту запросов и блок кандидатов.
 * @param {{api: object, roomId: string, toast: Function,
 *          insertText: (text: string) => void, navigate?: Function}} opts
 * @returns {{sections: HTMLElement[], load: Function, loadCandidates: Function,
 *            hasActive: Function, destroy: Function}}
 */
export function createFeed(opts) {
  const o = opts || {};
  const api = o.api;
  const roomId = String(o.roomId || '');
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const insertText = typeof o.insertText === 'function' ? o.insertText : () => {};
  const examples = EXAMPLES[roomId] || EXAMPLES._;

  const state = {
    requests: [],
    loaded: false,
    error: null,
    filter: 'all',
    query: '',
    candOpen: false,
    candLoaded: false,
    candItems: [],
    candError: null,
    candLoading: false,
  };

  /* ── каркас ─────────────────────────────────────────────────────────── */

  const listEl = h('div', { class: 'cmp-feed', role: 'list', 'aria-live': 'polite' });
  const feedNote = h('div', { class: 'cmp-feed__note small muted' });
  const searchInput = h('input', {
    class: 'input', type: 'search', placeholder: 'Поиск по заголовку',
    'aria-label': 'Поиск по заголовку запроса',
  });
  const searchBox = h('div', { class: 'search-field cmp-search' }, icon('search', { size: 13 }), searchInput);
  const filtersHost = h('div', { class: 'cmp-filters' });
  const refreshBtn = iconButton({
    icon: 'refresh', size: 'sm', title: 'Обновить ленту',
    onclick: () => { loadFeed(); loadCandidates(true); },
  });

  const feedHead = sectionHeader('Запросы', { count: 0, actions: [refreshBtn] });
  const feedCount = feedHead.querySelector('.section-header__count');
  if (feedCount) {
    feedCount.classList.add('count-pill');
    // B16: у числа есть пояснение «что считается».
    feedCount.title = 'Запросы комнаты: считаются все, кроме архива';
    feedCount.setAttribute('aria-label', 'Запросы комнаты: считаются все, кроме архива');
  }
  const toolbar = h('div', { class: 'cmp-toolbar' }, filtersHost, searchBox);

  const feed = h('section', { class: 'cmp-feedwrap' }, feedHead, toolbar, feedNote, listEl);

  /* кандидаты */
  const candToggle = h('button', { class: 'cmp-cands__toggle', type: 'button', 'aria-expanded': 'false' },
    icon('chevron-right', { size: 13, className: 'cmp-cands__chev' }),
    h('span', null, 'Найденные на доске запросы'),
    h('span', { class: 'count-pill count-pill--quiet cmp-cands__count hidden' }, '0'));
  const candBody = h('div', { class: 'cmp-cands__body hidden' });
  const cands = h('section', { class: 'cmp-cands' }, candToggle, candBody);

  /* ── меню карточки ──────────────────────────────────────────────────── */

  let menuOpenFor = null;
  let menuEl = null;

  function closeMenu() {
    if (menuEl && menuEl.parentNode) menuEl.parentNode.removeChild(menuEl);
    menuEl = null;
    if (menuOpenFor) {
      const btn = listEl.querySelector('[data-rid="' + escapeSel(menuOpenFor) + '"] [data-act="menu"]');
      if (btn) btn.setAttribute('aria-expanded', 'false');
    }
    menuOpenFor = null;
  }

  function openMenu(btn, item) {
    const rid = String(item.id);
    if (menuOpenFor === rid) { closeMenu(); return; }
    closeMenu();
    menuOpenFor = rid;
    btn.setAttribute('aria-expanded', 'true');

    const closeOnPick = (fn) => () => { closeMenu(); fn(); };
    const menu = h('div', { class: 'popover menu cmp-menu', role: 'menu', 'aria-label': 'Действия с запросом' },
      h('button', { class: 'menu__item', type: 'button', role: 'menuitem', onclick: closeOnPick(() => cloneRequest(item)) },
        icon('copy', { size: 13 }), 'Клонировать'),
      h('div', { class: 'menu__sep' }),
      h('button', { class: 'menu__item menu__item--danger', type: 'button', role: 'menuitem', onclick: closeOnPick(() => archiveRequest(item)) },
        icon('archive', { size: 13 }), 'Архивировать'),
    );
    btn.parentNode.appendChild(menu);
    menuEl = menu;
    const first = menu.querySelector('button');
    if (first) first.focus();
    menu.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); closeMenu(); btn.focus(); }
    });
  }

  async function cloneRequest(item) {
    try {
      const res = await api.post('/api/requests/' + enc(item.id) + '/actions', { action: 'clone' });
      const rid = res && (res.root_id || (res.request && res.request.id));
      let text = '';
      if (rid) {
        try { const d = await api.get('/api/requests/' + enc(rid)); text = String((d && d.text) || ''); } catch { text = ''; }
      }
      if (!text) text = String(item.preview || '');
      if (text) insertText(text);
      toast(text ? 'Копия создана — текст в поле запроса' : 'Копия создана', 'ok');
      await loadFeed();
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось клонировать запрос', 'danger');
    }
  }

  async function archiveRequest(item) {
    const ok = await confirmDialog({
      title: 'Архивировать запрос?',
      text: '«' + String(item.title || item.id) + '» скроется из ленты. Задачи на доске сохранятся.',
      confirmLabel: 'Архивировать', danger: true,
    });
    if (!ok) return;
    try {
      await api.post('/api/requests/' + enc(item.id) + '/actions', { action: 'archive' });
      toast('Запрос в архиве', 'ok');
      await loadFeed();
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось архивировать', 'danger');
    }
  }

  /* ── данные ─────────────────────────────────────────────────────────── */

  function needDetail(item) {
    const st = String(item.status || '');
    return st !== 'queued' && st !== 'archived';
  }

  async function hydrateDetails(items) {
    const todo = [];
    for (const item of items) {
      if (!item || !item.id || !needDetail(item)) continue;
      const cached = detailCache.get(item.id);
      if (cached && cached.key === detailKey(item)) continue;
      if (detailInflight.has(item.id)) continue;
      if (todo.length >= 10) break;
      todo.push(item);
    }
    if (!todo.length) return;
    await Promise.all(todo.map(async (item) => {
      detailInflight.add(item.id);
      try {
        const d = await api.get('/api/requests/' + enc(item.id));
        const synth = (d && d.synthesis) || {};
        detailCache.set(item.id, {
          key: detailKey(item),
          version: Number(synth.version || 0),
          text: String(synth.text_md || ''),
          names: activeNames(d),
          criticState: String((d && d.critic && d.critic.state) || 'off'),
          // Проектный режим: бриф и сводка направлений (поля необязательные).
          brief: Boolean(d && (d.brief === true || d.is_brief === true)),
          summary: summaryOf(d),
        });
      } catch {
        /* карточку могли архивировать — живём без деталей */
      } finally {
        detailInflight.delete(item.id);
      }
    }));
    renderFeed();
  }

  function visibleItems() {
    const q = state.query.trim().toLowerCase();
    return state.requests.filter((item) => {
      const st = String(item.status || '');
      if (state.filter === 'active' && !ACTIVE.has(st)) return false;
      if (state.filter === 'needs' && !(item.needs_you || st === 'needs_you')) return false;
      if (state.filter === 'done' && st !== 'done') return false;
      // Ищем и по короткому названию, и по полному тексту запроса.
      if (q) {
        const hay = (String(item.title || '') + ' ' + String(item.title_short || '')).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function itemsSignature(items) {
    return items.map((i) => {
      const p = i.progress || {};
      return [i.id, i.status, p.done, p.total, i.finished_at, i.created_at, i.needs_you, i.title, i.title_short, i.title_source].join(':');
    }).join('|');
  }

  function detailsSignature(items) {
    return items.map((i) => {
      const d = detailCache.get(i.id);
      return d
        ? [i.id, d.version, d.text.length, d.names.join(','),
          d.brief ? 'B' : '', d.summary ? d.summary.total + '/' + d.summary.running : ''].join(':')
        : i.id;
    }).join('|');
  }

  /* ── карточка ───────────────────────────────────────────────────────── */

  /** Прогресс и «сейчас» — только у активных: у готового это уже история. */
  function progressRow(item, detail) {
    const st = String(item.status || '');
    const p = item.progress || {};
    // «в очереди» уже сказано статус-чипом — второй строки не нужно.
    if (!ACTIVE.has(st)) return null;

    const total = Number(p.total || 0);
    const done = Number(p.done || 0);
    const row = h('div', { class: 'cmp-card__progress' });
    if (total) {
      const pct = Number.isFinite(Number(p.percent)) ? Math.max(0, Math.min(100, Number(p.percent))) : 0;
      row.appendChild(h('div', {
        class: 'progress',
        role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(total),
        'aria-valuenow': String(done), 'aria-label': 'Выполнено ' + done + ' из ' + total,
      }, h('div', { class: 'progress__bar', style: { width: pct + '%' } })));
      row.appendChild(h('span', {
        class: 'cmp-card__count small muted nowrap tnum',
        title: 'Выполнено ' + done + ' работ из ' + total + ' — так команда разложила этот запрос',
      }, done + '/' + total));
    }
    let now = 'руководитель комнаты разбирает запрос';
    if (st === 'reviewing') now = 'критик проверяет свод';
    else if (st === 'synthesizing') now = 'руководитель комнаты сводит ответ';
    else if (detail && detail.names.length) now = detail.names.map(personName).join(', ');
    row.appendChild(h('span', { class: 'cmp-card__now small muted truncate', title: now }, now));
    return row;
  }

  function durationText(item) {
    const st = String(item.status || '');
    if (item.duration_s != null && Number.isFinite(Number(item.duration_s))) return fmtDuration(Number(item.duration_s));
    if (ACTIVE.has(st) && item.started_at) return fmtDuration(Math.max(0, Math.floor(Date.now() / 1000) - Number(item.started_at)));
    return '';
  }

  function card(item) {
    const st = String(item.status || 'queued');
    const detail = detailCache.get(item.id);
    const href = '#/room/' + enc(roomId) + '/request/' + enc(item.id);
    // B8/P5: на карточке короткое название, полный текст — в подсказке.
    const short = titleShortOf(item) || 'Без названия';

    // Один статус: «ждёт тебя» важнее «в работе», второй чип не нужен.
    const shown = item.needs_you || st === 'needs_you' ? 'needs_you' : st;

    // Бриф проекта: бейдж «бриф» и мини-счётчик направлений — только у брифа.
    const brief = Boolean(item && (item.brief === true || item.is_brief === true))
      || Boolean(detail && detail.brief === true);
    const summary = summaryOf(item) || (detail && detail.summary) || null;

    const meta = h('div', { class: 'cmp-card__row cmp-card__meta' },
      statusChip(shown),
      brief ? briefBadge() : null,
      brief ? briefSummaryChip(summary) : null,
      h('span', { class: 'nowrap' }, timeAgo(item.created_at)),
      durationText(item) ? h('span', { class: 'nowrap' }, '· ' + durationText(item)) : null,
      detail && detail.version > 1
        ? h('span', {
          class: 'count-pill count-pill--quiet', title: 'Версия ' + detail.version + ' — после замечаний критика',
        }, 'v' + detail.version)
        : null,
      item.critic
        ? h('span', { class: 'cmp-card__flag', title: 'Свод проверит критик', 'aria-label': 'Свод проверит критик' },
          icon('verified', { size: 12 }))
        : null,
    );

    let snippet = null;
    if (st === 'done' && detail && detail.text) {
      const text = mdToText(detail.text, 320);
      if (text) snippet = h('p', { class: 'cmp-card__snippet', title: text }, text);
    } else if (st === 'done' && !detail) {
      snippet = h('p', { class: 'cmp-card__snippet cmp-card__snippet--pending' }, 'Ответ загружается…');
    } else if (item.status_reason) {
      snippet = h('p', { class: 'cmp-card__snippet', title: plainTerms(item.status_reason) }, plainTerms(item.status_reason));
    }

    const main = h('a', {
      class: 'cmp-card__main', href, 'data-act': 'open',
      'aria-label': short + ' — открыть страницу запроса',
    },
    h('div', { class: 'cmp-card__row' },
      h('h3', { class: 'cmp-card__title', title: titleHint(item) || short }, short)),
    meta, progressRow(item, detail), snippet);

    const menuBtn = h('button', {
      class: 'btn btn--ghost btn--icon btn--sm cmp-card__menu-btn',
      type: 'button', 'data-act': 'menu',
      'aria-haspopup': 'menu', 'aria-expanded': 'false',
      'aria-label': 'Действия: ' + short,
      title: 'Действия',
    }, icon('ellipsis', { size: 14 }));
    menuBtn.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); openMenu(menuBtn, item); });

    return h('article', { class: 'cmp-card', role: 'listitem', dataset: { rid: String(item.id) } },
      main, h('div', { class: 'cmp-menu-wrap' }, menuBtn));
  }

  /* ── рендер ленты ───────────────────────────────────────────────────── */

  function captureFocus() {
    const active = document.activeElement;
    if (!active || !listEl.contains(active)) return null;
    const cardEl = active.closest('[data-rid]');
    if (!cardEl) return null;
    return { rid: cardEl.getAttribute('data-rid'), act: active.getAttribute('data-act') };
  }

  function restoreFocus(sig) {
    if (!sig) return;
    const rid = '[data-rid="' + escapeSel(sig.rid) + '"]';
    const target = listEl.querySelector(rid + ' [data-act="' + String(sig.act || 'open') + '"]')
      || listEl.querySelector(rid + ' [data-act="open"]');
    if (target && typeof target.focus === 'function') target.focus();
  }

  let feedSig = '';

  function renderFeed(force) {
    const items = visibleItems();
    const sig = [state.loaded, state.error ? 'E' : '', state.filter, state.query,
      itemsSignature(items), detailsSignature(items)].join('#');
    if (!force && sig === feedSig) return;
    if (!force && (menuOpenFor || captureFocus())) return;
    feedSig = sig;

    const focusSig = captureFocus();
    const viewEl = document.getElementById('view');
    const scrollTop = viewEl ? viewEl.scrollTop : 0;
    listEl.replaceChildren();

    if (feedCount) {
      feedCount.textContent = String(state.requests.length);
      feedCount.classList.toggle('hidden', !state.requests.length);
    }
    feedNote.textContent = feedNoteText(items);

    if (!state.loaded && !state.error) {
      // B14: заглушка в форме будущих карточек, а не серые полосы.
      listEl.appendChild(skeletonCards({ rows: 3, variant: 'card', lines: 2 }));
    } else if (state.error) {
      listEl.appendChild(errorBanner(state.error, () => loadFeed()));
      if (state.requests.length) listEl.appendChild(h('div', { class: 'cmp-feed__stale small muted' }, 'Показаны данные с прошлого успешного обновления.'));
      for (const item of items) listEl.appendChild(card(item));
    } else if (!state.requests.length) {
      listEl.appendChild(emptyState({
        icon: 'comment-discussion',
        title: 'Пока ни одного запроса',
        text: 'Напишите запрос выше — руководитель комнаты разложит его по команде. Можно начать с примера:',
        action: examples.map((text) => ({
          label: text,
          variant: 'secondary',
          onclick: () => insertText(text),
        })),
      }));
    } else if (!items.length) {
      const total = state.requests.length;
      const text = state.query.trim()
        ? 'По запросу «' + state.query.trim() + '» ничего не нашлось среди ' + total + ' ' + fromForm(total, ['запрос', 'запроса', 'запросов']) + '.'
        : 'Под этот фильтр не подходит ни один из ' + total + ' ' + fromForm(total, ['запрос', 'запроса', 'запросов']) + '.';
      listEl.appendChild(emptyState({
        icon: 'filter',
        title: 'Ничего не найдено',
        text,
        action: { label: 'Сбросить фильтры', onclick: () => resetFilters() },
      }));
    } else {
      for (const item of items) listEl.appendChild(card(item));
    }

    if (viewEl) viewEl.scrollTop = scrollTop;
    restoreFocus(focusSig);
  }

  function feedNoteText(items) {
    if (!state.requests.length) return '';
    const active = state.requests.filter((i) => ACTIVE.has(String(i.status))).length;
    const parts = [];
    if (items.length !== state.requests.length) parts.push('показано ' + items.length + ' из ' + state.requests.length);
    if (active) parts.push(active === 1 ? 'идёт 1 запрос' : 'идут ' + active + ' ' + plural(active, ['запрос', 'запроса', 'запросов']));
    return parts.join(' · ');
  }

  function resetFilters() {
    state.filter = 'all';
    state.query = '';
    searchInput.value = '';
    renderFilters();
    renderFeed(true);
  }

  let filtersEl = null;
  function renderFilters() {
    const next = segmented({
      options: FILTERS, value: state.filter,
      onchange: (v) => { state.filter = String(v); renderFeed(true); },
    });
    if (filtersEl) filtersEl.replaceWith(next); else filtersHost.appendChild(next);
    filtersEl = next;
  }

  searchInput.addEventListener('input', debounce(() => {
    state.query = searchInput.value;
    renderFeed();
  }, 150));

  async function loadFeed() {
    try {
      const data = await api.get('/api/rooms/' + enc(roomId) + '/requests?limit=50');
      const items = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
      // Архив в ленте не показываем: он живёт только на доске.
      state.requests = items.filter((item) => item && !item.archived);
      state.error = null;
      state.loaded = true;
      renderFeed();
      hydrateDetails(items);
    } catch (err) {
      state.error = err;
      state.loaded = true;
      renderFeed(true);
    }
  }

  /* ── кандидаты ──────────────────────────────────────────────────────── */

  function renderCandidates() {
    candToggle.setAttribute('aria-expanded', state.candOpen ? 'true' : 'false');
    candToggle.classList.toggle('is-open', state.candOpen);
    candBody.classList.toggle('hidden', !state.candOpen);
    if (!state.candOpen) return;

    if (state.candError) {
      candBody.replaceChildren(errorBanner(state.candError, () => loadCandidates(true)));
      return;
    }
    if (!state.candLoaded) {
      candBody.replaceChildren(skeleton(2));
      return;
    }
    if (!state.candItems.length) {
      candBody.replaceChildren(h('p', { class: 'small muted cmp-cands__empty' },
        'На доске нет задач с разложением, которые ещё не привязаны к комнате.'));
      return;
    }
    const frag = [];
    for (const c of state.candItems) {
      frag.push(h('div', { class: 'cmp-cand' },
        h('div', { class: 'cmp-cand__main' },
          h('div', { class: 'cmp-card__row' }, h('span', { class: 'cmp-cand__title' }, String(c.title || c.id)), statusChip(String(c.status || 'queued'))),
          h('div', { class: 'cmp-card__row cmp-card__meta' },
            c.assignee ? h('span', { class: 'mono small' }, personName(c.assignee)) : null,
            h('span', { class: 'small' }, Number(c.children || 0) + ' ' + plural(Number(c.children || 0), ['ребёнок', 'ребёнка', 'детей'])),
            c.tenant && c.tenant !== 'room:' + roomId
              ? h('span', { class: 'chip chip--warn', title: 'Задача уже в другой комнате' }, 'чужая комната')
              : null,
          ),
          c.preview ? h('p', { class: 'cmp-card__snippet' }, String(c.preview)) : null,
        ),
        h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Привязать'),
      ));
      const btn = frag[frag.length - 1].querySelector('button');
      btn.addEventListener('click', () => adopt(c, btn));
    }
    candBody.replaceChildren(...frag);
  }

  async function loadCandidates(force) {
    if (!state.candOpen && !force) return;
    if (state.candLoading) return;
    state.candLoading = true;
    state.candError = null;
    renderCandidates();
    try {
      const data = await api.get('/api/rooms/' + enc(roomId) + '/candidates');
      state.candItems = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
      state.candLoaded = true;
    } catch (err) {
      state.candError = err;
    } finally {
      state.candLoading = false;
      syncCandCount();
      renderCandidates();
    }
  }

  function syncCandCount() {
    const chip = candToggle.querySelector('.cmp-cands__count');
    if (!chip) return;
    chip.textContent = String(state.candItems.length);
    chip.classList.toggle('hidden', !state.candItems.length);
  }

  async function adopt(cand, btn) {
    btn.disabled = true;
    try {
      await api.post('/api/adopt', { room: roomId, task_id: cand.id });
      toast('Задача привязана к комнате', 'ok');
      state.candItems = state.candItems.filter((c) => c.id !== cand.id);
      syncCandCount();
      renderCandidates();
      await loadFeed();
    } catch (err) {
      btn.disabled = false;
      toast(err && err.message ? err.message : 'Не удалось привязать задачу', 'danger');
    }
  }

  candToggle.addEventListener('click', () => {
    state.candOpen = !state.candOpen;
    renderCandidates();
    if (state.candOpen && !state.candLoaded) loadCandidates(true);
  });

  /* ── события страницы ────────────────────────────────────────────────── */

  function onDocClick(ev) {
    if (!menuOpenFor) return;
    const t = ev && ev.target;
    const inside = t && typeof t.closest === 'function' && t.closest('.cmp-menu, [data-act="menu"]');
    if (!inside) closeMenu();
  }

  document.addEventListener('click', onDocClick);

  // Перерисовка, отложенная из-за фокуса в ленте, догоняется, когда фокус ушёл.
  const onFocusOut = () => {
    setTimeout(() => { if (!menuOpenFor) renderFeed(); }, 0);
  };
  listEl.addEventListener('focusout', onFocusOut);

  renderFilters();
  renderFeed(true);
  renderCandidates();

  return {
    sections: [feed, cands],
    load: loadFeed,
    loadCandidates,
    hasActive: () => state.requests.some((i) => ACTIVE.has(String(i.status))),
    // Сколько запросов в комнате: по этому композер решает, пустая ли комната.
    count: () => state.requests.length,
    loaded: () => state.loaded,
    destroy() {
      document.removeEventListener('click', onDocClick);
      listEl.removeEventListener('focusout', onFocusOut);
      closeMenu();
      feed.remove();
      cands.remove();
    },
  };
}

export default { createFeed };
