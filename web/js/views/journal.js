/**
 * journal.js — комната, вкладка «Журнал».
 *
 * Хронология событий комнаты: группировка по дням («Сегодня», «Вчера», «27 окт»),
 * иконки по типу события, ссылки на запросы, фильтр по типу и поиск с debounce.
 * «Показать ещё» догружает по 50 событий. Опрос — раз в 15 секунд, молча.
 */

import { h, debounce } from '../util.js';
import { icon } from '../icons.js';
import { poll as pollApi } from '../api.js';
import {
  ensureCss, emptyState, skeleton, errorBanner, segmented, sectionHeader,
} from '../ui.js';
import {
  groupJournal, mergeJournal, journalKindInfo, journalTime,
  journalMatchesType, journalMatchesQuery,
  JOURNAL_FILTERS, JOURNAL_PAGE,
} from './backlog-model.js';
import {
  requestFilterOf, requestFilterChip, filterByRequest, requestTitle, scopeHash,
} from '../scope.js';
import { planOf, planLogEvents } from './plan-req.js';

const POLL_MS = 15000;
// Карта проекта меняется реже журнала: не дёргаем файл на каждом тике.
const PLAN_TTL_MS = 20000;

/** Событие карты проекта: иконка, подпись и группа фильтра. */
const PLAN_KIND = { icon: 'map', tone: 'info', label: 'Карта проекта', group: 'steps' };

function requestHref(roomId, rid) {
  return `#/room/${encodeURIComponent(roomId)}/request/${encodeURIComponent(rid)}`;
}

/**
 * Убрать тавтологию в записях карты: сервер пишет «Добавил часть работы «X»
 * по сообщению Михаила», а журнал ставит перед причиной «, потому что» —
 * получалось «добавил часть работы «X», потому что Добавил часть работы «X» по
 * сообщению Михаила». Если причина начинается с того же, что уже сказано,
 * повтор снимаем: «добавил часть работы «X» — по сообщению Михаила».
 * Чистая функция (проверяется node --test).
 * @param {string} text
 * @returns {string}
 */
export function untautologize(text) {
  const full = String(text === null || text === undefined ? '' : text).trim();
  // Два способа приклеить причину: «, потому что …» и «: …».
  const cuts = [];
  const because = full.indexOf(', потому что ');
  if (because > 0) cuts.push({ at: because, len: ', потому что '.length });
  const colon = full.indexOf(': ');
  if (colon > 0) cuts.push({ at: colon, len: 2 });
  if (!cuts.length) return full;
  cuts.sort((a, b) => a.at - b.at);
  const head = full.slice(0, cuts[0].at).trim();
  const why = full.slice(cuts[0].at + cuts[0].len).trim();
  if (!head || !why) return full;
  const lowHead = head.toLowerCase();
  const lowWhy = why.toLowerCase();
  if (lowWhy === lowHead) return head;

  // 1. Причина дословно начинается со сказанного (регистр/кавычки не в счёт).
  if (lowWhy.startsWith(lowHead)) {
    let i = 0;
    while (i < lowHead.length && i < lowWhy.length && lowHead[i] === lowWhy[i]) i += 1;
    const rest = why.slice(i).trim().replace(/^[,.;:—-]\s*/, '').trim();
    return rest ? head + ' — ' + rest : head;
  }

  // 2. Причина повторяет название части работы: «добавил часть работы «X»,
  //    потому что Добавил «X»: без каналов…», «изменил часть работы «X»: «X» —
  //    готово» — первая фраза уже сказана, повтор убираем.
  const names = Array.from(head.matchAll(/«([^»]+)»/g)).map((m) => m[1].trim().toLowerCase());
  for (const name of names) {
    if (!name) continue;
    const marker = '«' + name + '»';
    const at2 = lowWhy.indexOf(marker);
    if (at2 === -1 || at2 > 64) continue;
    const rest = why.slice(at2 + marker.length).trim().replace(/^[,.;:—-]\s*/, '').trim();
    return rest ? head + ' — ' + rest : head;
  }
  return full;
}

function actorName(actor) {
  if (!actor) return '';
  if (actor === 'mikhail') return 'Михаил';
  if (actor === 'system') return 'система';
  return String(actor);
}

/**
 * @param {HTMLElement} el
 * @param {object} ctx
 */
export async function mount(el, ctx) {
  ensureCss('css/view-journal.css');

  const roomId = String((ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || '');
  if (!roomId) {
    el.appendChild(h('div', { class: 'view__inner' },
      emptyState({ icon: 'error', title: 'Комната не выбрана', text: 'Откройте комнату в левой колонке.' })));
    return { unmount() {} };
  }

  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};

  const state = {
    items: [],
    planEvents: [],
    hasMore: false,
    limit: JOURNAL_PAGE,
    type: 'all',
    q: '',
    requestId: requestFilterOf(ctx.route),
    requestLabel: '',
    error: null,
    offline: false,
    busyMore: false,
  };
  if (state.requestId) state.requestLabel = await requestTitle(api, state.requestId);
  let disposed = false;
  let loadSeq = 0;
  let poller = null;
  let signature = '';
  let planAt = 0;
  const abort = new AbortController();

  /* ------------------------------------------------------------ каркас */

  const root = h('div', { class: 'view__inner journal' });
  const errorHost = h('div', { class: 'journal__error' });
  const scopeHost = h('div', { class: 'journal__scope row row--wrap' });
  const listHost = h('div', { class: 'journal__list' });
  const footHost = h('div', { class: 'journal__foot' });

  const search = h('input', {
    class: 'input',
    type: 'search',
    placeholder: 'Поиск по событиям…',
    'aria-label': 'Поиск по журналу',
    autocomplete: 'off',
  });

  const runSearch = debounce(() => {
    const value = String(search.value || '').trim();
    if (value === state.q) return;
    state.q = value;
    state.limit = JOURNAL_PAGE;
    load({ silent: false, reset: true });
  }, 300);

  search.addEventListener('input', runSearch);
  search.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && search.value) {
      search.value = '';
      runSearch.cancel();
      state.q = '';
      state.limit = JOURNAL_PAGE;
      load({ silent: false, reset: true });
    }
  });

  const toolbar = h('div', { class: 'journal__toolbar row row--between row--wrap' },
    h('div', { class: 'journal__search search-field' }, icon('search', { size: 13 }), search),
    h('div', { class: 'row row--wrap row--tight' },
      segmented({
        options: JOURNAL_FILTERS,
        value: state.type,
        onchange: (value) => {
          state.type = String(value || 'all');
          paint();
        },
      })));

  const heading = h('div', { class: 'journal__heading row row--between row--wrap' },
    // С фильтром это уже не «комната целиком» — заголовок говорит, что показано.
    sectionHeader(state.requestId ? 'Хронология запроса' : 'Хронология комнаты', { count: 0 }),
    h('div', { class: 'row row--tight small muted' },
      icon('pulse', { size: 12 }), 'обновляется автоматически'));

  root.append(errorHost, heading, scopeHost, toolbar, listHost, footHost);
  el.appendChild(root);

  /* --------------------------------------------------------- область показа */

  /** Снять фильтр: вкладка та же, параметра ?request больше нет. */
  function clearRequestFilter() {
    const hash = scopeHash(roomId, 'journal');
    if (typeof ctx.navigate === 'function') ctx.navigate(hash);
    else location.hash = hash;
  }

  function paintScope() {
    if (!state.requestId) { scopeHost.replaceChildren(); return; }
    scopeHost.replaceChildren(requestFilterChip({
      requestId: state.requestId,
      title: state.requestLabel,
      onClear: clearRequestFilter,
    }));
  }

  paintScope();

  /* ------------------------------------------------------------- рендер */

  function buildEvent(event) {
    // Записи карты проекта («добавил часть работы «X», потому что …») приходят
    // из плана комнаты — у них своя подпись и иконка.
    const info = event.kind === 'plan' ? PLAN_KIND : journalKindInfo(event.kind);
    const tone = info.tone && info.tone !== 'muted' ? ' journal-event--' + info.tone : '';
    return h('article', { class: 'journal-event' + tone, 'data-kind': event.kind || '' },
      h('span', { class: 'journal-event__icon', title: info.label }, icon(info.icon, { size: 14 })),
      h('div', { class: 'journal-event__body' },
        h('div', { class: 'journal-event__text' }, String(event.text || '')),
        h('div', { class: 'journal-event__meta' },
          h('span', { class: 'journal-event__time tnum' }, journalTime(event.at)),
          h('span', { class: 'journal-event__kind' }, info.label),
          event.actor ? h('span', { class: 'journal-event__actor' }, actorName(event.actor)) : null,
          event.request_id
            ? h('a', {
              class: 'journal-event__link',
              href: requestHref(roomId, event.request_id),
              title: 'Открыть запрос ' + event.request_id,
            }, icon('arrow-right', { size: 11 }), 'к запросу')
            : null)));
  }

  /** Тип события: у карты проекта своя группа — «Шаги» (журнал kinds не мой файл). */
  function typeMatch(event, type) {
    if (!type || type === 'all') return true;
    if (event && event.kind === 'plan') return type === 'steps';
    return journalMatchesType(event, type);
  }

  function filtered() {
    const all = [...state.items, ...(state.planEvents || [])];
    const byType = all.filter((e) => typeMatch(e, state.type) && journalMatchesQuery(e, state.q));
    return filterByRequest(byType, state.requestId);
  }

  function buildList() {
    const events = filtered();
    if (!events.length) {
      if (state.q) {
        return emptyState({
          icon: 'search',
          title: 'Ничего не найдено',
          text: `По запросу «${state.q}» событий нет. Попробуйте короче или снимите фильтр.`,
          action: { label: 'Сбросить поиск', onclick: resetFilters },
        });
      }
      if (state.type !== 'all') {
        return emptyState({
          icon: 'filter',
          title: 'Событий этого типа нет',
          text: 'Выберите другой тип или покажите все события.',
          action: { label: 'Показать всё', onclick: resetFilters },
        });
      }
      if (state.requestId) {
        return emptyState({
          icon: 'history',
          title: 'В этом запросе событий пока нет',
          text: 'Журнал показывает только события одного запроса. Снимите фильтр, чтобы увидеть всю комнату.',
          action: { label: 'Показать весь журнал', onclick: clearRequestFilter },
        });
      }
      return emptyState({
        icon: 'history',
        title: 'Журнал пока пуст',
        text: 'Здесь появятся события комнаты: созданные запросы, ответы, вопросы, решения и шаги.',
      });
    }
    const days = groupJournal(events, new Date());
    return h('div', { class: 'journal__days' }, days.map((day) => h('section', { class: 'journal-day' },
      h('div', { class: 'journal-day__head' },
        h('h3', { class: 'journal-day__title' }, day.title),
        h('span', { class: 'chip chip--muted tnum' }, String(day.items.length))),
      h('div', { class: 'journal-day__events' }, day.items.map(buildEvent)))));
  }

  function paintFoot() {
    footHost.replaceChildren();
    if (state.busyMore) {
      footHost.appendChild(h('div', { class: 'row row--tight small muted' },
        icon('loading', { size: 12, className: 'codicon-modifier-spin' }), 'Загружаю ещё…'));
      return;
    }
    if (state.hasMore && state.limit < 200) {
      footHost.appendChild(h('div', { class: 'row row--between row--wrap journal__more' },
        h('span', { class: 'small muted' }, `Показано ${state.items.length} событий`),
        h('button', {
          class: 'btn btn--secondary btn--sm',
          type: 'button',
          onclick: loadMore,
        }, icon('chevron-down', { size: 12 }), 'Показать ещё 50')));
    } else if (state.hasMore) {
      footHost.appendChild(h('div', { class: 'small muted journal__end' }, 'Показаны последние 200 событий — уточните поиск.'));
    } else if (state.items.length > JOURNAL_PAGE) {
      footHost.appendChild(h('div', { class: 'small muted journal__end' }, 'Это все события комнаты.'));
    }
  }

  function paint() {
    if (disposed) return;
    const events = filtered();
    const head = root.querySelector('.journal__heading .section-header__count');
    if (head) head.textContent = String(events.length);
    const sig = [state.type, state.q, state.hasMore, state.busyMore,
      events.map((e) => e.id).join(','), state.planEvents.map((e) => e.id).join(',')].join('#');
    if (sig !== signature) {
      signature = sig;
      listHost.replaceChildren(buildList());
    }
    paintFoot();
  }

  function paintError() {
    if (state.error) {
      errorHost.replaceChildren(errorBanner(state.error, () => load({ silent: false, reset: true })));
    } else if (state.offline) {
      errorHost.replaceChildren(h('div', { class: 'banner banner--warn', role: 'status' },
        icon('warning', { size: 14 }),
        h('span', { class: 'banner__text' }, 'Нет связи с приложением. Показываю последние известные события.')));
    } else {
      errorHost.replaceChildren();
    }
  }

  function resetFilters() {
    // Полный ремаунт вьюхи: заодно сбрасывается визуальное состояние segmented.
    if (typeof ctx.refresh === 'function') {
      ctx.refresh();
      return;
    }
    search.value = '';
    state.q = '';
    state.type = 'all';
    state.limit = JOURNAL_PAGE;
    load({ silent: false, reset: true });
  }

  /* ------------------------------------------------------------- загрузка */

  /**
   * Записи карты проекта комнаты: журнал плана («предложил карту»,
   * «добавил часть работы X, потому что …»). Нет карты — журнал работает без них.
   */
  async function loadPlan() {
    if (planAt && Date.now() - planAt < PLAN_TTL_MS) return;
    planAt = Date.now();
    try {
      const res = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/plan`, { signal: abort.signal });
      state.planEvents = planLogEvents(planOf(res))
        .map((e) => Object.assign({}, e, { text: untautologize(e.text) }));
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      // Связи нет — оставляем то, что уже показано: журнал не должен пустеть.
    }
  }

  async function load({ silent = false, reset = false } = {}) {
    if (disposed) return;
    const seq = ++loadSeq;
    if (!silent) {
      if (reset) {
        state.items = [];
        listHost.replaceChildren(skeleton(5));
      } else if (!state.items.length) {
        listHost.replaceChildren(skeleton(5));
      }
    }
    const planTask = loadPlan();
    try {
      const params = new URLSearchParams();
      params.set('limit', String(state.limit));
      if (state.q) params.set('q', state.q);
      const data = await api.get(
        `/api/rooms/${encodeURIComponent(roomId)}/journal?${params.toString()}`,
        { signal: abort.signal },
      );
      if (disposed || seq !== loadSeq) return;
      const page = Array.isArray(data && data.items) ? data.items : [];
      state.items = reset ? mergeJournal([], page) : mergeJournal(state.items, page);
      state.hasMore = !!(data && data.has_more);
      state.error = null;
      state.offline = false;
      await planTask;
      paintError();
      paint();
    } catch (err) {
      if (disposed || seq !== loadSeq || (err && err.name === 'AbortError')) return;
      if (!silent || !state.items.length) {
        state.error = err;
        paintError();
        if (!state.items.length) {
          listHost.replaceChildren();
          footHost.replaceChildren();
        }
      } else {
        state.offline = true;
        paintError();
      }
    }
  }

  async function loadMore() {
    if (state.busyMore || state.limit >= 200) return;
    state.busyMore = true;
    paintFoot();
    state.limit = Math.min(200, state.limit + JOURNAL_PAGE);
    try {
      await load({ silent: true });
    } finally {
      state.busyMore = false;
      paintFoot();
    }
  }

  await load({ silent: false, reset: true });

  poller = pollApi(async () => {
    if (state.q || state.type !== 'all') return;
    await load({ silent: true });
  }, POLL_MS, {
    pauseHidden: true,
    immediate: false,
    onError: () => { /* мягко: следующий тик повторит */ },
  });

  return {
    unmount() {
      disposed = true;
      abort.abort();
      if (typeof runSearch.cancel === 'function') runSearch.cancel();
      if (poller && typeof poller.stop === 'function') poller.stop();
      root.remove();
    },
  };
}
