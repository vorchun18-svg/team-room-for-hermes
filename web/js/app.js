/**
 * app.js — каркас приложения: тема, шапка с вкладками, баннеры здоровья, тосты,
 * Ctrl+K, опрос сводок. Сайдбар (комнаты, «Недавнее», движок) — в shell-sidebar.js.
 *
 * Старт: /api/health + /api/rooms → сайдбар → вкладки комнаты → вьюха в #view.
 * Если файла вьюхи нет, router.js показывает «Раздел в разработке».
 */

import { h, hashColor } from './util.js';
import { icon } from './icons.js';
import { api, poll } from './api.js';
import { createRouter, TABS, buildHash, parseHash, tabTitle } from './router.js';
import { rooms, currentRoom, toasts, theme, health } from './store.js';
import {
  initShellFeatures, installHotkeys, openRoomDialog, toggleNotifications,
  applyNotifyLabel, notifyChanges,
} from './shell-features.js';
import { initStatusBanners, renderBanners, dispatcherRunning } from './shell-status.js';
import {
  APP_TITLE, pick, buildSidebar, renderRoomList, markActiveRoom, loadRecent,
  renderEngineStatus, localTitle,
} from './shell-sidebar.js';
import { displayName } from './shell-labels.js';
import { crumbItems, requestTitle as fetchRequestTitle, scopeHash } from './scope.js';
import { folderActionButton, roomSettingsButton, projectDirHint } from './project-dir-button.js';
import {
  waitingFor, refreshInbox, subscribeInbox, startInboxPolling,
} from './shell-inbox.js';

export { dispatcherRunning };

const THEME_KEY = 'teamroom.theme';
const LAST_ROOM_KEY = 'teamroom.lastRoom';
/** Вкладки, которые принимают фильтр ?request=<id> (Д3, имя согласовано с BACKLOG-FIX). */
const FILTER_TABS = ['backlog', 'reports', 'journal'];

const $ = (sel) => document.querySelector(sel);
const el = {
  app: $('#app'),
  sidebar: $('#sidebar'),
  topbar: $('#topbar'),
  view: $('#view'),
  toasts: $('#toasts'),
};

/* ─────────────────────────────────────────────────────────────── утилиты */

/** Первое числовое значение из списка путей. */
function pickNumber(obj, paths) {
  for (const p of paths) {
    const v = pick(obj, p);
    const n = typeof v === 'number' ? v : Number(v);
    if (Number.isFinite(n)) return n;
  }
  return 0;
}

/** Нормализация комнаты из ответа сервера (терпим к форме). */
function normalizeRoom(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = String(raw.id || raw.slug || raw.room_id || '');
  if (!id) return null;
  const members = Array.isArray(raw.members) ? raw.members : [];
  return Object.assign({}, raw, {
    id,
    title: String(raw.title || raw.name || id),
    icon: String(raw.icon || 'home'),
    members,
    // API (server/app.py) отдаёт counters.{working,needs_you,done_today}; поля
    // counts.* и summary.* — legacy, поддерживаем их вторыми в списке (F-3).
    counts: {
      working: pickNumber(raw, ['counters.working', 'counts.working', 'summary.working', 'working_count', 'working']),
      needs_you: pickNumber(raw, ['counters.needs_you', 'counts.needs_you', 'summary.needs_you', 'needs_you_count', 'needs_you']),
      done: pickNumber(raw, ['counters.done_today', 'counters.done', 'counts.done_today', 'counts.done', 'summary.done_today', 'summary.done']),
    },
  });
}

function normalizeRooms(payload) {
  const arr = Array.isArray(payload) ? payload
    : Array.isArray(payload && payload.rooms) ? payload.rooms
      : Array.isArray(pick(payload, 'data.rooms')) ? pick(payload, 'data.rooms')
        : [];
  return arr.map(normalizeRoom).filter(Boolean);
}

function findRoom(id) {
  return rooms.get('list').find((r) => r.id === id) || null;
}

/** Фаза проекта комнаты (чип в шапке). */
function roomPhase(room) {
  if (!room) return null;
  const raw = room.phase || pick(room, 'stage.current') || pick(room, 'stage.title') || null;
  // API отдаёт фазу объектом {id, title, status}: в чипе нужен текст, не объект.
  if (raw && typeof raw === 'object') return String(raw.title || raw.name || raw.id || '') || null;
  return raw;
}

/** Одно число «дел ждёт вас» для вкладки, сайдбара и заголовка окна (D4). */
const waitingOf = waitingFor;

/** Роль участника комнаты по её настройкам. */
function memberRole(room, member) {
  const role = String((member && member.role) || '');
  if (role) return role;
  const profile = String((member && (member.profile || member.name)) || '');
  if (room && profile === String(room.orchestrator || '')) return 'orchestrator';
  if (room && profile === String(room.critic || '')) return 'critic';
  return 'worker';
}

/** Цвет аватара: --h из хеша имени. */
function avatarHue(name) {
  const m = /hsl\(\s*([\d.]+)/i.exec(hashColor(name || '?'));
  return m ? Math.round(Number(m[1])) : 220;
}

function avatarEl(name, cls = '', hint = '') {
  // Одна буква от имени: «Юрист проекта» → «Ю». Системные id в тексте не нужны.
  const short = String(name || '').replace(/^(?:biz|room)-/, '');
  const a = h('span', { class: 'avatar ' + cls, title: String(hint || name || '') }, (Array.from(short)[0] || '?').toUpperCase());
  a.style.setProperty('--h', String(avatarHue(short || name)));
  return a;
}

/* ────────────────────────────────────────────────────────────────── тема */

const media = window.matchMedia('(prefers-color-scheme: dark)');

function readThemePref() {
  const url = new URLSearchParams(location.search).get('theme');
  if (url && ['auto', 'light', 'dark'].includes(url)) return { pref: url, persist: false };
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* приватный режим */ }
  return { pref: ['auto', 'light', 'dark'].includes(saved) ? saved : 'auto', persist: true };
}

function applyTheme(pref, persist = true) {
  const value = ['auto', 'light', 'dark'].includes(pref) ? pref : 'auto';
  document.documentElement.dataset.theme = value;
  theme.set({ pref: value, resolved: value === 'auto' ? (media.matches ? 'dark' : 'light') : value });
  if (persist) {
    try { localStorage.setItem(THEME_KEY, value); } catch { /* ignore */ }
  }
  const btn = $('#theme-toggle');
  if (btn) {
    const names = { auto: 'как в системе', light: 'светлая', dark: 'тёмная' };
    const label = 'Тема: ' + names[value];
    btn.setAttribute('aria-label', label);
    btn.setAttribute('title', label);
    const text = btn.querySelector('[data-theme-label]');
    if (text) text.textContent = names[value];
  }
}

function cycleTheme() {
  const order = ['auto', 'light', 'dark'];
  const next = order[(order.indexOf(theme.get('pref')) + 1) % order.length];
  applyTheme(next, true);
  const names = { auto: 'как в системе', light: 'светлая', dark: 'тёмная' };
  toast('Тема: ' + names[next], 'info');
}

media.addEventListener('change', () => {
  if (theme.get('pref') === 'auto') {
    theme.set({ resolved: media.matches ? 'dark' : 'light' });
  }
});

/* ────────────────────────────────────────────────────────────────── тосты */

let toastSeq = 0;

/**
 * Показать тост.
 * @param {string} text
 * @param {'info'|'ok'|'warn'|'danger'} [kind]
 */
export function toast(text, kind = 'info') {
  if (!el.toasts) return;
  const id = 'toast-' + (++toastSeq);
  const icons = { ok: 'pass', warn: 'warning', danger: 'error', info: 'info' };
  const node = h('div', { class: 'toast toast--' + (icons[kind] ? kind : 'info'), id, role: 'status' },
    icon(icons[kind] || 'info', { size: 14 }),
    h('div', { class: 'toast__text' }, String(text)),
  );
  el.toasts.appendChild(node);
  toasts.set({ list: toasts.get('list').concat([{ id, text: String(text), kind, at: Date.now() }]) });

  const close = () => {
    if (!node.isConnected) return;
    node.classList.add('is-leaving');
    setTimeout(() => {
      node.remove();
      toasts.set({ list: toasts.get('list').filter((t) => t.id !== id) });
    }, 130);
  };
  node.addEventListener('click', close);
  setTimeout(close, kind === 'danger' ? 7000 : 4200);
}

/* ─────────────────────────────────────────────────────────────── шапка: ключ перерисовки */

let topbarKey = '';

/* ─────────────────────────────────────────────────────────── шапка */

/** Название запроса для крошки: подгружается один раз на маршрут (Д3). */
let requestCrumbId = '';
let requestCrumbTitle = '';

async function ensureRequestCrumb(route) {
  if (!route || route.name !== 'request' || !route.id) {
    requestCrumbId = '';
    requestCrumbTitle = '';
    return;
  }
  const id = String(route.id);
  if (id === requestCrumbId && requestCrumbTitle) return;
  requestCrumbId = id;
  requestCrumbTitle = '';
  const title = await fetchRequestTitle(api, id);
  if (requestCrumbId !== id) return;
  requestCrumbTitle = title;
  topbarKey = '';
  renderTopbar(router ? router.route : parseHash());
}

/** Открыто ли выдвижное меню (на широком экране класс просто не мешает). */
function navIsOpen() {
  return !!(el.app && el.app.classList.contains('nav-open'));
}

/**
 * Единственная точка открытия/закрытия меню: класс, aria-expanded у гамбургера
 * и подпись кнопки. Раньше класс дёргали в пяти местах — отсюда «непонятное
 * поведение» на узком окне.
 */
function setNavOpen(open) {
  if (!el.app) return;
  const want = !!open;
  if (navIsOpen() === want) return;
  el.app.classList.toggle('nav-open', want);
  const toggle = el.topbar && el.topbar.querySelector('.nav-toggle');
  if (toggle) {
    toggle.setAttribute('aria-expanded', want ? 'true' : 'false');
    const label = want ? 'Закрыть меню комнат' : 'Открыть меню комнат';
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('title', label);
  }
  // Шапка не перерисовывается: правим только состояние гамбургера.
}

function toggleNav() {
  setNavOpen(!navIsOpen());
}

window.addEventListener('resize', () => {
  // Широкое окно: выдвижное меню не нужно, иначе остаётся «залипший» класс.
  if (window.innerWidth >= 900 && navIsOpen()) setNavOpen(false);
});

/** Крошки шапки + бейдж «вся комната» (бейдж живёт в блоке меты, не в крошках). */
function crumbsNav(route, room) {
  const items = crumbItems({
    route,
    roomTitle: room && room.title,
    requestTitle: requestCrumbTitle,
    tabTitle,
  });
  if (!items.length) return null;
  const nav = h('nav', { class: 'crumbs', 'aria-label': 'Где вы находитесь' });
  // Бейдж — текст, а не элемент крошек: он рисуется в блоке меты (вторая строка
  // шапки на узком экране), чтобы не отнимать место у названия страницы.
  const badge = (items.find((item) => item.badge) || {}).badge || '';
  items.forEach((item, i) => {
    if (i) nav.appendChild(h('span', { class: 'crumbs__sep', 'aria-hidden': 'true' }, '›'));
    if (item.href) {
      nav.appendChild(h('a', { class: 'crumbs__link', href: item.href, title: item.text }, item.text));
    } else {
      nav.appendChild(h('span', {
        class: 'crumbs__here',
        'aria-current': item.current ? 'page' : null,
        title: item.text,
      }, item.text));
    }
  });
  return { nav, badge };
}

/** Подсказка страницы под вкладками (место папки проекта — не в шапке). */
function renderPageHint(node) {
  const host = document.getElementById('page-hint');
  if (!host) return;
  if (node) {
    host.replaceChildren(node);
    host.hidden = false;
  } else {
    host.replaceChildren();
    host.hidden = true;
  }
}

function renderTopbar(route) {
  const bar = el.topbar;
  if (!bar) return;
  const room = route.roomId ? findRoom(route.roomId) : null;

  const navToggle = h('button', {
    class: 'btn btn--ghost btn--icon btn--sm nav-toggle',
    type: 'button',
    'aria-label': navIsOpen() ? 'Закрыть меню комнат' : 'Открыть меню комнат',
    'aria-controls': 'sidebar',
    'aria-expanded': navIsOpen() ? 'true' : 'false',
    title: navIsOpen() ? 'Закрыть меню комнат' : 'Меню комнат',
    onclick: () => toggleNav(),
  }, icon('list-unordered', { size: 15 }));

  // Перерисовываем шапку только когда реально изменились данные — иначе
  // пятисекундный опрос дёргал бы вкладки под курсором.
  const key = [
    route.name, route.roomId, route.tab, route.id, requestCrumbTitle,
    room && room.title, roomPhase(room), room && room.project_dir,
    waitingOf(room).text,
    room && room.counts && room.counts.working,
    room && Array.isArray(room.members) ? room.members.length : 0,
    health.get('ok'), health.get('error'), dispatcherRunning(health.get('data')),
    pick(health.get('data'), 'engine.schema_ok'), navIsOpen(),
  ].join('|');
  if (key === topbarKey) return;
  topbarKey = key;

  // Три зоны: lead (крошки) · meta (этап, «вся комната», команда) · actions.
  const lead = [];
  const meta = [];
  const right = [];
  let hintNode = null;

  if (route.name === 'search') {
    lead.push(h('div', { class: 'topbar__title' }, h('span', { class: 'topbar__name' }, 'Поиск')));
  } else if (route.name === 'settings') {
    lead.push(h('div', { class: 'topbar__title' }, h('span', { class: 'topbar__name' }, 'Настройки')));
  } else if (room) {
    const phase = roomPhase(room);
    // «локально» осталось одно место — строка под сайдбаром (D13): здесь только фаза.
    // Д3: вместо голого названия комнаты — крошки «Комната «…» › …», чтобы было
    // видно, к чему относится экран: к запросу или ко всей комнате.
    const crumbs = crumbsNav(route, room);
    lead.push(h('div', { class: 'topbar__title' }, crumbs ? crumbs.nav : h('span', { class: 'topbar__name' }, room.title)));
    if (phase) {
      meta.push(h('span', {
        class: 'chip chip--outline topbar__phase',
        title: 'Этап проекта: общий путь комнаты, не одного запроса',
      }, phase));
    }
    if (crumbs && crumbs.badge) {
      meta.push(h('span', {
        class: 'chip chip--muted crumbs__badge',
        title: 'Раздел относится ко всей комнате, а не к одному запросу',
      }, crumbs.badge));
    }

    const members = Array.isArray(room.members) ? room.members : [];
    const shown = members.slice(0, 4);
    if (shown.length) {
      const names = members.map((m) => {
        const profile = String((m && (m.profile || m.name)) || '');
        return displayName(profile, memberRole(room, m), m && m.title);
      });
      const team = h('div', { class: 'topbar__team', title: 'Команда: ' + names.filter(Boolean).join(', ') });
      for (const m of shown) {
        const profile = String((m && (m.profile || m.name)) || '');
        const name = displayName(profile, memberRole(room, m), m && m.title);
        team.appendChild(avatarEl(name, 'avatar--sm avatar--stacked', name + (profile ? ' · ' + profile : '')));
      }
      if (members.length > shown.length) {
        team.appendChild(h('span', { class: 'avatar avatar--sm avatar--stacked', style: { '--h': 220 } }, '+' + (members.length - shown.length)));
      }
      meta.push(team);
    }
    // Д2 + W10: на всех вкладках комнаты (и на странице запроса) место в шапке
    // занято всегда — «Настройки комнаты» и папка проекта. Путь задан → «Открыть
    // папку», пути нет → «Задать папку» (те же настройки, курсор в поле «Папка
    // проекта»). Подсказка «Задайте папку проекта…» живёт в полосе под вкладками:
    // в шапке длинная фраза ломала раскладку на 560–900 px (DESIGN-NOTES §10).
    const roomCtx = {
      api, toast, navigate,
      store: { rooms, currentRoom, toasts, theme, health },
      refresh: () => loadRooms({ silent: true }),
    };
    right.push(roomSettingsButton(roomCtx, room));
    right.push(folderActionButton(roomCtx, room));
    if (!String(room.project_dir || '').trim()) {
      hintNode = projectDirHint(roomCtx, { roomId: room.id });
    }
  } else {
    lead.push(h('div', { class: 'topbar__title' },
      h('span', { class: 'topbar__name' }, APP_TITLE),
      h('span', { class: 'topbar__sub', title: localTitle() }, 'локально'),
    ));
  }

  right.push(h('button', {
    class: 'btn btn--ghost btn--sm topbar__search',
    type: 'button',
    title: 'Поиск по комнатам и запросам',
    'aria-label': 'Поиск',
    onclick: () => navigate('#/search'),
  }, icon('search', { size: 13 }),
  h('span', { class: 'topbar__search-label' }, 'Поиск'),
  h('span', { class: 'kbd' }, 'Ctrl K')));

  bar.replaceChildren(h('div', { class: 'topbar__row' },
    navToggle,
    h('div', { class: 'topbar__lead' }, ...lead),
    h('div', { class: 'topbar__meta' }, ...meta),
    h('div', { class: 'topbar__actions' }, ...right),
  ));
  renderPageHint(hintNode);

  // Вкладки комнаты
  if (room && route.name !== 'search' && route.name !== 'settings') {
    const active = route.tab === 'request' ? 'composer' : route.tab;
    // Д3: со страницы запроса ссылки на «Дальше / Отчёты / Журнал» ведут уже
    // с фильтром ?request=<корень> — на вкладке сразу виден чип «Только этот запрос».
    const filterTab = route.name === 'request' && route.id ? String(route.id) : '';
    const waits = waitingOf(room);
    const tabs = h('nav', { class: 'tabs', 'aria-label': 'Разделы комнаты' });
    for (const t of TABS) {
      const isActive = t.id === active;
      // Это навигация, а не tablist: aria-current="page" + подпись у счётчика
      // дают скринридеру «Дальше, 9 дел, текущая страница» (F-9, D18).
      let count = null;
      if (t.id === 'backlog') {
        if (waits.n === null) {
          count = h('span', {
            class: 'tab__count count-pill count-pill--you',
            'aria-label': 'Нет данных: приложение не отвечает',
            title: 'Нет данных: приложение не отвечает',
          }, '—');
        } else if (waits.n > 0) {
          count = h('span', {
            class: 'tab__count count-pill count-pill--you',
            'aria-label': waits.text + ' ждут вашего решения',
            title: waits.text + ' ждут вашего решения',
          }, String(waits.n));
        }
      }
      tabs.appendChild(h('a', {
        class: 'tab' + (isActive ? ' tab--active' : ''),
        href: FILTER_TABS.includes(t.id)
          ? scopeHash(room.id, t.id, filterTab)
          : buildHash({ roomId: room.id, tab: t.id }),
        'aria-current': isActive ? 'page' : null,
        'aria-label': count ? `${t.title}, ${waits.text} ждут вашего решения` : null,
      },
      icon(t.icon, { size: 13 }),
      t.title,
      count,
      ));
    }
    bar.appendChild(tabs);
  }

  const banners = h('div', { id: 'banners', class: 'topbar__banners' });
  bar.appendChild(banners);
  renderBanners();
}

/* ─────────────────────────────────────────────────────────────── данные */

async function loadHealth() {
  try {
    const data = await api.get('/api/health');
    const ok = data && data.ok !== false;
    health.set({ loading: false, ok, error: null, data: data || null });
  } catch (err) {
    health.set({ loading: false, ok: false, error: err && err.message ? err.message : 'Нет связи', data: null });
  }
  renderEngineStatus();
  renderBanners();
  const route = router ? router.route : parseHash();
  if (route && route.name !== 'search' && route.name !== 'settings') renderTopbar(route);
}

async function loadRooms({ silent = false } = {}) {
  if (!silent) rooms.set({ loading: true });
  try {
    const payload = await api.get('/api/rooms', { etag: silent });
    if (payload && payload.notModified) {
      loadRecent();
      return;
    }
    const list = normalizeRooms(payload);
    rooms.set({ list, loadedAt: Date.now(), loading: false, error: null });
    notifyChanges(list);
  } catch (err) {
    rooms.set({ loading: false, error: err && err.message ? err.message : 'Список комнат недоступен' });
  }
  renderRoomList();
  updateTitle();
  const route = router ? router.route : parseHash();
  if (route && route.roomId) {
    const room = findRoom(route.roomId);
    currentRoom.set({ id: route.roomId, room, phase: roomPhase(room) });
    renderTopbar(route);
  }
  loadRecent();
  refreshInbox();
}

function updateTitle() {
  const n = rooms.get('list').reduce((sum, r) => sum + (waitingOf(r).n || 0), 0);
  document.title = (n > 0 ? '(' + n + ') ' : '') + APP_TITLE;
}

/* ─────────────────────────────────────────────────── маршрут и палитра */

let router = null;

function navigate(hash) {
  if (router) router.navigate(hash);
  else location.hash = String(hash || '#/');
}

function onRoute(route) {
  const room = route.roomId ? findRoom(route.roomId) : null;
  currentRoom.set({ id: route.roomId || null, room, phase: roomPhase(room) });
  if (route.roomId) {
    try { localStorage.setItem(LAST_ROOM_KEY, route.roomId); } catch { /* ignore */ }
  }
  ensureRequestCrumb(route);
  renderTopbar(route);
  markActiveRoom();
  updateTitle();
  setNavOpen(false);
}

/* ──────────────────────────────────────────────────────────── монтирование */

function mountShell() {
  if (!el.sidebar || !el.topbar || !el.view) {
    console.error('[app] нет контейнеров #sidebar/#topbar/#view — проверьте index.html');
    return;
  }

  buildSidebar(el.sidebar, {
    onNewRoom: () => openRoomDialog(),
    onToggleNotifications: () => toggleNotifications(),
    onCycleTheme: () => cycleTheme(),
    onCloseNav: () => setNavOpen(false),
  });

  // Затемнение под выдвижным сайдбаром — сосед .app__main, чтобы fixed
  // не привязывался к трансформированному <aside>.
  const scrim = h('div', { class: 'sidebar__scrim', 'aria-hidden': 'true', onclick: () => setNavOpen(false) });
  const mainCol = el.app.querySelector('.app__main');
  if (mainCol) el.app.insertBefore(scrim, mainCol);
  else el.app.appendChild(scrim);

  router = createRouter({
    el: el.view,
    api,
    store: { rooms, currentRoom, toasts, theme, health },
    toast,
    getRoom: findRoom,
    onChange: onRoute,
  });

  renderEngineStatus();
  router.start();
}

/* ─────────────────────────────────────────────────────── старт приложения */

function initialHash() {
  if (location.hash && location.hash !== '#' && location.hash !== '#/') return location.hash;
  let last = null;
  try { last = localStorage.getItem(LAST_ROOM_KEY); } catch { /* ignore */ }
  const list = rooms.get('list');
  const room = (last && list.find((r) => r.id === last)) || list[0];
  return room ? buildHash({ roomId: room.id, tab: 'composer' }) : '#/';
}

async function boot() {
  const { pref, persist } = readThemePref();
  applyTheme(pref, persist);
  initShellFeatures({
    toast,
    api,
    stores: { rooms, currentRoom, toasts, theme, health },
    navigate,
    getRoute: () => (router ? router.route : parseHash()),
    render: () => { if (router) router.render(); },
  });
  installHotkeys({ closeNav: () => setNavOpen(false) });
  initStatusBanners({
    pick,
    toast,
    retry: () => { loadHealth(); loadRooms({ silent: true }); },
    retryHealth: () => { loadHealth(); },
  });
  mountShell();
  applyNotifyLabel();

  await Promise.all([loadHealth(), loadRooms()]);
  if (!location.hash || location.hash === '#' || location.hash === '#/') {
    location.replace(initialHash());
  }
  renderRoomList();
  updateTitle();

  // Сайдбар и вкладки живут на одном сторе: как только диалог комнаты или
  // вкладка обновили данные, рисуем сразу, а не ждём пятисекундного опроса (D3).
  let lastList = rooms.get('list');
  rooms.subscribe(() => {
    const list = rooms.get('list');
    if (list === lastList) return;
    lastList = list;
    renderRoomList();
    updateTitle();
    const route = router ? router.route : parseHash();
    if (route) renderTopbar(route);
  }, { immediate: false });

  subscribeInbox(() => {
    const route = router ? router.route : parseHash();
    if (route) renderTopbar(route);
    updateTitle();
    renderRoomList();
  });

  startInboxPolling();

  poll(() => loadRooms({ silent: true }), 5000, { pauseHidden: true, immediate: false, onError: () => {} });
  poll(() => loadHealth(), 15000, { pauseHidden: true, immediate: false, onError: () => {} });

  window.addEventListener('online', () => { loadHealth(); loadRooms({ silent: true }); });

  document.addEventListener('click', (e) => {
    const a = e.target instanceof Element ? e.target.closest('a[href^="#"]') : null;
    if (a) setNavOpen(false);
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();

export { api, navigate, router };
