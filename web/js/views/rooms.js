/**
 * views/rooms.js — домашний экран (#/): обзор комнат.
 *
 * Данные: GET /api/rooms?include_archived=1. Держит store.rooms.list в той же
 * форме, что и app.js (id/title/icon/members/counts), чтобы сайдбар и шапка
 * обновлялись без перезагрузки. Опрос — 15 с, с паузой в скрытой вкладке.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import {
  ensureCss, avatar, emptyState, skeleton, errorBanner, sectionHeader,
} from '../ui.js';

const POLL_MS = 15000;

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/** Счётчики комнаты из любой формы ответа (counters | counts | summary). */
function countersOf(raw) {
  const c = (raw && (raw.counters || raw.counts || raw.summary)) || {};
  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };
  return {
    working: num(c.working),
    needs_you: num(c.needs_you !== undefined ? c.needs_you : c.needsYou),
    done_today: num(c.done_today !== undefined ? c.done_today : c.done),
  };
}

/** Нормализация комнаты в форму store.rooms.list (как в app.js). */
export function normalizeRoom(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = String(raw.id || raw.slug || raw.room_id || '');
  if (!id) return null;
  const c = countersOf(raw);
  return Object.assign({}, raw, {
    id,
    title: String(raw.title || raw.name || id),
    icon: String(raw.icon || 'home'),
    members: Array.isArray(raw.members) ? raw.members : [],
    counters: c,
    counts: { working: c.working, needs_you: c.needs_you, done: c.done_today },
    phase: raw.phase || null,
    archived: Boolean(raw.archived),
  });
}

/** Список комнат из ответа /api/rooms (массив или {rooms}). */
function roomArray(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.rooms)) return payload.rooms;
  return [];
}

/** «biz-ops» → «ops»: системные id остаются в title, не в тексте. */
function personName(id) {
  const s = String(id == null ? '' : id).trim();
  if (!s) return '';
  if (s === 'mikhail') return 'Михаил';
  return s.replace(/^(?:biz|room)-/, '');
}

/** Подпись руководителя комнаты без повтора роли. */
function directorText(id) {
  const n = personName(id);
  return n === 'director' || n === 'default' ? 'руководитель комнаты' : 'руководитель комнаты · ' + n;
}

/** Аватар с одной буквой — без «DI/DE» из системных id. */
function letterAvatar(name) {
  const short = personName(name) || String(name || '');
  const el = avatar(short, { size: 'sm' });
  el.textContent = (Array.from(short)[0] || '?').toUpperCase();
  el.title = String(name || '');
  return el;
}

function phaseLabel(phase) {
  if (!phase) return '';
  if (typeof phase === 'string') return phase;
  return String(phase.title || phase.id || '');
}

/**
 * Комната в проектном режиме (Д2)? Поле необязательное — читаем через `?.`,
 * иначе старые ответы API ломали бы карточку.
 * @param {object|null} room
 * @returns {boolean}
 */
export function isProjectMode(room) {
  return room?.project_mode === true;
}

/**
 * @param {HTMLElement} el
 * @param {object} ctx
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  await ensureCss('css/view-rooms.css');

  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const store = ctx.store || {};
  const roomsStore = store.rooms || null;
  const navigate = typeof ctx.navigate === 'function' ? ctx.navigate : (hash) => { location.hash = hash; };

  const state = { rooms: [], archived: [], loaded: false, error: null };

  const root = h('div', { class: 'view__inner rooms' });

  const addBtn = h('button', { class: 'btn btn--primary', type: 'button' }, icon('add', { size: 14 }), 'Комната');
  const head = h('header', { class: 'rooms__head' },
    h('div', { class: 'rooms__head-text' },
      h('h1', { class: 'rooms__title' }, 'Комнаты'),
      h('p', { class: 'rooms__sub' },
        'Проект и его команда в одном месте. Пишете один запрос — руководитель комнаты раздаёт работу, вы читаете один ответ.'),
    ),
    h('div', { class: 'rooms__head-actions' }, addBtn),
  );

  const steps = h('section', { class: 'rooms-howto', 'aria-label': 'Как это работает' },
    h('div', { class: 'rooms-howto__step' },
      h('span', { class: 'rooms-howto__num', 'aria-hidden': 'true' }, '1'),
      h('div', null,
        h('div', { class: 'rooms-howto__title' }, 'Пишете запрос'),
        h('div', { class: 'rooms-howto__text' }, 'Одним текстом, как диктуете. Исполнителя выбирать не нужно.'),
      ),
    ),
    h('div', { class: 'rooms-howto__step' },
      h('span', { class: 'rooms-howto__num', 'aria-hidden': 'true' }, '2'),
      h('div', null,
        h('div', { class: 'rooms-howto__title' }, 'Руководитель комнаты раздаёт'),
        h('div', { class: 'rooms-howto__text' }, 'Комната делит работу между агентами и следит за ходом.'),
      ),
    ),
    h('div', { class: 'rooms-howto__step' },
      h('span', { class: 'rooms-howto__num', 'aria-hidden': 'true' }, '3'),
      h('div', null,
        h('div', { class: 'rooms-howto__title' }, 'Читаете один ответ'),
        h('div', { class: 'rooms-howto__text' }, 'Свод, шаги, вопросы к вам и отчёт — на одной странице.'),
      ),
    ),
  );

  const grid = h('div', { class: 'rooms__grid' });
  const gridHost = h('section', { class: 'rooms__section' },
    sectionHeader('Комнаты', { count: 0 }),
    grid,
  );
  const gridCount = gridHost.querySelector('.section-header__count');

  const archHost = h('section', { class: 'rooms__arch hidden' });

  const footnote = h('p', { class: 'rooms__note small muted' },
    icon('lock', { size: 12 }),
    'Всё локально: комнаты, запросы и файлы лежат на этом компьютере. Внешних сервисов нет.');

  root.appendChild(head);
  root.appendChild(steps);
  root.appendChild(gridHost);
  root.appendChild(archHost);
  root.appendChild(footnote);
  el.appendChild(root);

  function cardsSource() {
    const list = state.loaded ? state.rooms : (roomsStore ? roomsStore.get('list') || [] : []);
    return list;
  }

  function countChip(iconName, n, cls, title) {
    return h('span', { class: 'chip ' + cls, title }, icon(iconName, { size: 11 }), String(n));
  }

  function roomCard(room) {
    const c = countersOf(room);
    const phase = phaseLabel(room.phase);
    const members = Array.isArray(room.members) ? room.members
      : (Array.isArray(room.members_list) ? room.members_list : []);
    const memberCount = Number(room.members_count != null ? room.members_count : members.length) || 0;
    const href = '#/room/' + enc(room.id) + '/composer';

    const team = h('div', { class: 'rooms-card__team' });
    if (members.length) {
      for (const m of members.slice(0, 5)) {
        const name = String((m && (m.profile || m.name)) || '?');
        team.appendChild(avatar(name, { size: 'sm', title: name }));
      }
      if (memberCount > 5) {
        team.appendChild(h('span', { class: 'avatar avatar--sm rooms-card__more', title: 'Ещё ' + (memberCount - 5) },
          '+' + (memberCount - 5)));
      }
      team.appendChild(h('span', { class: 'rooms-card__team-count small muted' },
        memberCount + ' в команде'));
    } else if (memberCount) {
      team.appendChild(h('span', { class: 'rooms-card__team-count small muted' },
        memberCount + ' в команде'));
    } else {
      team.appendChild(h('span', { class: 'rooms-card__team-count small muted' }, 'Команда ещё не задана'));
    }

    const counts = h('div', { class: 'rooms-card__counts' });
    if (c.working) counts.appendChild(countChip('pulse', c.working, 'chip--info', 'Идёт работ: ' + c.working));
    if (c.needs_you) counts.appendChild(countChip('person', c.needs_you, 'chip--warn', 'Ждёт вашего ответа: ' + c.needs_you));
    if (c.done_today) counts.appendChild(countChip('check', c.done_today, 'chip--ok', 'Готово сегодня: ' + c.done_today));
    if (!counts.childNodes.length) counts.appendChild(h('span', { class: 'chip chip--muted' }, 'пока тихо'));

    // Д2: комнаты в проектном режиме копят материалы в папке проекта — это
    // важное отличие, показываем бейджем (поле необязательное, читаем терпимо).
    if (isProjectMode(room)) {
      counts.appendChild(h('span', {
        class: 'chip chip--info rooms-card__mode',
        title: 'Проектный режим: материалы и итоговые файлы команда складывает в папку проекта',
      }, icon('folder-opened', { size: 11 }), 'проектный режим'));
    }

    return h('a', {
      class: 'rooms-card', href,
      'aria-label': String(room.title || room.id) + ' — открыть комнату',
    },
    h('div', { class: 'rooms-card__top' },
      h('span', { class: 'rooms-card__icon' }, icon(room.icon || 'home', { size: 16 })),
      h('span', { class: 'rooms-card__title' }, String(room.title || room.id)),
    ),
    h('div', { class: 'rooms-card__dir' },
      room.orchestrator
        ? h('span', { class: 'rooms-card__director', title: 'Профиль руководителя комнаты: ' + room.orchestrator },
          letterAvatar(room.orchestrator),
          h('span', { class: 'truncate' }, directorText(room.orchestrator)))
        : h('span', { class: 'small muted' }, 'руководитель комнаты не задан'),
      phase ? h('span', { class: 'chip chip--muted rooms-card__phase', title: 'Текущая фаза проекта' },
        icon('milestone', { size: 11 }), h('span', { class: 'truncate' }, phase)) : null,
    ),
    team,
    counts,
    h('div', { class: 'rooms-card__foot small muted' },
      room.updated_at ? 'обновлено ' + timeAgo(room.updated_at) : 'локальная комната',
      room.critic ? h('span', { class: 'rooms-card__flag', title: 'Свод проверяет критик' }, '· с критиком') : null,
    ));
  }

  function renderArchived() {
    archHost.replaceChildren();
    const list = state.archived;
    if (!list.length) { archHost.classList.add('hidden'); return; }
    archHost.classList.remove('hidden');
    const details = h('details', { class: 'rooms-arch' },
      h('summary', { class: 'rooms-arch__summary' },
        icon('archive', { size: 13 }),
        'Архивные комнаты',
        h('span', { class: 'chip chip--muted' }, String(list.length)),
      ),
    );
    const body = h('div', { class: 'rooms-arch__list' });
    for (const room of list) {
      body.appendChild(h('a', { class: 'rooms-arch__item', href: '#/room/' + enc(room.id) + '/composer' },
        icon(room.icon || 'home', { size: 14 }),
        h('span', { class: 'truncate' }, String(room.title || room.id)),
        h('span', { class: 'small muted' }, room.orchestrator ? String(room.orchestrator) : ''),
      ));
    }
    details.appendChild(body);
    archHost.appendChild(details);
  }

  let renderSig = '';

  function render(force) {
    const list = cardsSource();
    const sig = JSON.stringify(list.map((r) => [r.id, r.title, r.icon, r.updated_at, r.phase,
      countersOf(r).working, countersOf(r).needs_you, countersOf(r).done_today,
      isProjectMode(r)])) + '#' + state.loaded + '#' + Boolean(state.error);
    if (!force && sig === renderSig) return;
    renderSig = sig;

    grid.replaceChildren();
    if (!state.loaded && !state.error) {
      for (let i = 0; i < 2; i++) {
        grid.appendChild(h('div', { class: 'rooms-card rooms-card--skeleton' },
          skeleton(3)));
      }
    } else if (state.error && !list.length) {
      grid.appendChild(errorBanner(state.error, () => load(true)));
    } else if (!list.length) {
      grid.appendChild(emptyState({
        icon: 'comment-discussion',
        title: 'Пока нет комнат',
        text: 'Комната — это проект и его команда: руководитель комнаты, исполнители и критик. Создайте первую — и напишите запрос.',
        action: { label: 'Создать комнату', variant: 'primary', onclick: () => openCreate() },
      }));
    } else {
      for (const room of list) grid.appendChild(roomCard(room));
      if (state.error) grid.appendChild(errorBanner(state.error, () => load(true)));
    }

    const active = state.rooms.filter((r) => !r.archived).length;
    if (gridCount) {
      gridCount.textContent = String(active);
      gridCount.classList.toggle('hidden', !active);
    }

    renderArchived();
  }

  async function load(force) {
    if (stopped) return;
    try {
      const payload = await api.get('/api/rooms?include_archived=1', force ? undefined : { etag: true });
      if (payload && payload.notModified) return;
      const all = roomArray(payload).map(normalizeRoom).filter(Boolean);
      state.rooms = all.filter((r) => !r.archived);
      state.archived = all.filter((r) => r.archived);
      state.loaded = true;
      state.error = null;
      if (roomsStore) {
        roomsStore.set({ list: state.rooms, loading: false, error: null, loadedAt: Date.now() });
      }
    } catch (err) {
      state.error = err;
    }
    render(force);
  }

  async function openCreate() {
    try {
      const mod = await import('./room-dialog.js');
      if (mod && typeof mod.openRoomDialog === 'function') {
        await mod.openRoomDialog(ctx, null);
        await load(true);
        return;
      }
    } catch (err) {
      console.warn('[rooms] диалог комнаты не открылся', err);
    }
    toast('Не удалось открыть форму комнаты', 'danger');
  }

  addBtn.addEventListener('click', () => openCreate());

  /* ─────────────────────────────────────────────────────────────── опрос */

  let timer = null;
  let stopped = false;

  function schedule() {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(tick, POLL_MS);
  }

  async function tick() {
    if (stopped) return;
    if (typeof document !== 'undefined' && document.hidden) { schedule(); return; }
    await load(false);
    schedule();
  }

  function onVisible() {
    if (typeof document !== 'undefined' && !document.hidden && !stopped) {
      clearTimeout(timer);
      tick();
    }
  }

  document.addEventListener('visibilitychange', onVisible);

  render(true);
  await load(true);
  schedule();

  return {
    unmount() {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
      root.remove();
    },
  };
}

export default { mount };
