/**
 * app.js — каркас приложения: тема, шапка с вкладками, баннеры здоровья, тосты,
 * Ctrl+K, опрос сводок. Сайдбар (комнаты, «Недавнее», движок) — в shell-sidebar.js.
 *
 * Старт: /api/health + /api/rooms → сайдбар → вкладки комнаты → вьюха в #view.
 * Если файла вьюхи нет, router.js показывает «Раздел в разработке».
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { api, poll } from './api.js';
import { createRouter, TABS, TAB_IDS, buildHash, parseHash, tabTitle } from './router.js';
// W26-D9: полоса «Надиктовать» и бейдж вкладки «Входящие» (один счётчик на все места).
import { installQuickCapture, captureBadge } from './quick-capture.js';
import {
  createRouteMemory, parseRouteHash, isBareHash, planStart,
  normalizeStartPref, START_KEY, buildRoomHash,
} from './route-memory.js';
import { rooms, currentRoom, toasts, theme, health } from './store.js';
import {
  initShellFeatures, installHotkeys, openRoomDialog, toggleNotifications,
  applyNotifyLabel, notifyChanges, openPalette,
} from './shell-features.js';
import { initStatusBanners, renderBanners, dispatcherRunning } from './shell-status.js';
// W29-P9: компактный индикатор расхода в шапке (одна строка, ссылка в настройки).
import { limitsChip, refreshLimits, chipSignature } from './limits-chip.js';
import {
  APP_TITLE, pick, buildSidebar, renderRoomList, markActiveRoom, loadRecent,
  renderEngineStatus, localTitle, setTodayCount, setDelosCount, renderBookmarks,
} from './shell-sidebar.js';
// W26-D6: число у пункта «Дела» — дела, где ждёт ваше решение (одно чтение при старте).
import { fetchNeedsBadge } from './views/delo-model.js';
import { displayName } from './shell-labels.js';
// W21-R1: режим меню (полоса значков) и пороги раскладки вида.
import { installShellRail, columnsForView } from './shell-rail.js';
// W25-L1: режимы раскладки по ширине вида (`data-layout` на #app) и нижний лист.
import { installLayoutMode } from './layout-mode.js';
import { installAsideSheet } from './shell-aside-sheet.js';
import { crumbItems, requestTitleInfo as fetchRequestTitleInfo, scopeHash, requestFilterOf } from './scope.js';
import { folderActionButton, roomSettingsButton, projectDirHint } from './project-dir-button.js';
import {
  waitingFor, refreshInbox, subscribeInbox, startInboxPolling,
} from './shell-inbox.js';
import { installHotkeys as installFieldHotkeys, openHotkeyHelp, NAV_EVENT } from './hotkeys.js';
import { applyViewMode, toggleViewMode } from './view-mode.js';
// W27-E4: заглушка-скелетон при запуске (без смены адреса и «прыжка» страницы).
import { skeletonCards } from './shell-skeleton.js';
import {
  FILTER_TABS as SCOPE_FILTER_TABS, phaseLabel, scopeModel, scopeSwitcher, toolsGroup,
} from './topbar-scope.js';
import { termHelp } from './term-help.js';
// W29-Q7: вкладки открытых запросов над страницей, «Недавнее»/«Закладки» в меню
// и общая «Отмена» 10 с — один каркас на всё приложение.
import { createRequestTabs, installTabKeys } from './views/request-tabs.js';
import { pushRecent, renameRecent } from './recents.js';
import { sharedUndo } from './undo.js';
// W20-U4 (P9): «руководитель отреагировал на ваш ответ» — бейдж вкладки и отметки.
import { reactionTabBadge, subscribeReactions } from './views/reaction-notice.js';

export { dispatcherRunning };

const THEME_KEY = 'teamroom.theme';
const LAST_ROOM_KEY = 'teamroom.lastRoom';
// W24-N3 → W27-E4: «Стартовая страница» — «Сегодня» (по умолчанию), «Комнаты»
// или «Последнее место». Ключ и значения живут в route-memory.js (один источник
// для настроек, каркаса и тестов). Здесь только чтение/запись в localStorage.
// W27-E4: последний список комнат — в локальной памяти: до ответа /api/rooms
// каркас сразу знает, есть ли комнаты, и выбирает адрес ОДИН раз.
const ROOMS_CACHE_KEY = 'teamroom.roomsCache';
const ROOMS_CACHE_MAX = 40;
/** Вкладки, которые принимают фильтр ?request=<id> (Д3, имя согласовано с BACKLOG-FIX). */
const FILTER_TABS = SCOPE_FILTER_TABS;

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

/* W20-U1 (P8): аватары команды и роли рисовала шапка; теперь состав комнаты
   живёт на вкладке «Команда», и эти помощники из app.js убраны — одна команда,
   одно место. */

/* ────────────────────────────────────────────────────────────────── тема */

const media = window.matchMedia('(prefers-color-scheme: dark)');

/**
 * Стартовая страница из настроек: 'today' (по умолчанию), 'rooms' или 'last'.
 * @returns {'today'|'rooms'|'last'}
 */
export function readStartPref() {
  try {
    return normalizeStartPref(localStorage.getItem(START_KEY));
  } catch { return 'today'; }
}

/** Сохранить выбор стартовой страницы (экран «Настройки»). */
export function saveStartPref(value) {
  const next = normalizeStartPref(value);
  try { localStorage.setItem(START_KEY, next); } catch { /* приватный режим */ }
  return next;
}

/* ─────────────────────────── W27-E4: локальная память списка комнат ─────── */

/**
 * Последний известный список комнат из локальной памяти.
 *
 * Зачем: при запуске адрес выбирается ОДИН раз и до первой отрисовки (дефект 3:
 * «страница прыгает»). Без кэша «Сегодня» не отличить от «комнат нет», и адрес
 * пришлось бы менять после ответа сервера — это и был видимый прыжок. Кэш даёт
 * мгновенный ответ; свежие данные приходят следом тихим опросом.
 *
 * @returns {object[]} [] — кэша нет или он испорчен (не ошибка)
 */
function readRoomsCache() {
  try {
    const raw = localStorage.getItem(ROOMS_CACHE_KEY);
    if (!raw) return [];
    const data = JSON.parse(raw);
    const list = Array.isArray(data && data.rooms) ? data.rooms : [];
    return list.map(normalizeRoom).filter(Boolean);
  } catch { return []; }
}

/** Запомнить список комнат (только поля, нужные меню и шапке). */
function saveRoomsCache(list) {
  try {
    const rows = (Array.isArray(list) ? list : []).slice(0, ROOMS_CACHE_MAX).map((r) => ({
      id: r.id,
      title: r.title,
      icon: r.icon,
      phase: r.phase,
      project_dir: r.project_dir,
      members: Array.isArray(r.members) ? r.members.slice(0, 8) : [],
      counts: r.counts,
    }));
    localStorage.setItem(ROOMS_CACHE_KEY, JSON.stringify({ at: Date.now(), rooms: rows }));
  } catch { /* приватный режим или места нет — работаем без кэша */ }
}

/**
 * Число «ждёт вас» у пункта «Сегодня» (W24-N3).
 *
 * Один лёгкий GET при загрузке и после возврата окна: значок в меню должен быть
 * верным и тогда, когда человек работает в комнате, а не на «Сегодня».
 * Ошибку молчим — это украшение, а не данные экрана.
 */
async function loadTodayBadge() {
  try {
    const data = await api.get('/api/today');
    const count = Number((data && data.counters && data.counters.needs_you) || 0);
    setTodayCount(count);
  } catch { /* сервер занят или старый — значок просто не показываем */ }
}

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

/* ───────────────────────────────────── вид «простой / подробный» (P14) ── */

/** Переключить вид и сказать одной строкой, что изменилось (клавиша `V`, кнопка). */
function toggleView() {
  const mode = toggleViewMode();
  toast(mode === 'simple'
    ? 'Простой вид: скрыты модель, внутренние номера и служебные строки'
    : 'Подробный вид: снова видны все подробности', 'info');
}

/** Справка по горячим клавишам (`?`) — окно поверх страницы, закрывается Esc. */
function openHelp() {
  openHotkeyHelp();
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
let requestCrumbHint = '';

async function ensureRequestCrumb(route) {
  if (!route || route.name !== 'request' || !route.id) {
    requestCrumbId = '';
    requestCrumbTitle = '';
    requestCrumbHint = '';
    return;
  }
  const id = String(route.id);
  if (id === requestCrumbId && requestCrumbTitle) return;
  requestCrumbId = id;
  requestCrumbTitle = '';
  requestCrumbHint = '';
  // W17-G §1: в крошке короткое название, полный текст диктовки — в подсказке.
  const info = await fetchRequestTitleInfo(api, id);
  if (requestCrumbId !== id) return;
  requestCrumbTitle = info.short || '';
  requestCrumbHint = info.full || '';
  // Название стало известно — подписываем вкладку и «Недавнее» тем же именем.
  if (requestCrumbTitle) {
    renameRecent(id, requestCrumbTitle);
    if (requestTabs) requestTabs.sync({ name: 'request', roomId: route.roomId, id }, { title: requestCrumbTitle });
  }
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
    requestHint: requestCrumbHint,
    tabTitle,
  });
  if (!items.length) return null;
  // W27-E4: первый элемент крошек — «Комнаты» со ссылкой на `#/`. Из любого
  // экрана комнаты до списка комнат один клик (дефект 3: «вернуться не смог»).
  items.unshift({ text: 'Комнаты', href: '#/', hint: 'Все комнаты — список комнат' });
  const nav = h('nav', { class: 'crumbs', 'aria-label': 'Где вы находитесь' });
  // Бейдж — текст, а не элемент крошек: он рисуется в блоке меты (вторая строка
  // шапки на узком экране), чтобы не отнимать место у названия страницы.
  const badge = (items.find((item) => item.badge) || {}).badge || '';
  items.forEach((item, i) => {
    if (i) nav.appendChild(h('span', { class: 'crumbs__sep', 'aria-hidden': 'true' }, '›'));
    const hint = item.hint || item.text;
    if (item.href) {
      nav.appendChild(h('a', { class: 'crumbs__link', href: item.href, title: hint }, item.text));
    } else {
      nav.appendChild(h('span', {
        class: 'crumbs__here',
        'aria-current': item.current ? 'page' : null,
        title: hint,
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

/**
 * W26-QA1: «Ещё ▾» для вкладок комнаты.
 *
 * Полоса вкладок прокручивается, но ползунка не видно: на 1026 px «Дежурства»
 * уезжали за правый край и достать их мышью было нельзя, на 683 px — три
 * вкладки из девяти (замер: tools/probe_w26_tabs.py). Теперь не поместившиеся
 * вкладки прячутся, а в шапке появляется «Ещё ▾» с теми же пунктами, счётчиками
 * и адресами. Активную вкладку прячем последней; если она всё же ушла в меню,
 * кнопка подсвечивается — видно, где вы.
 *
 * @param {HTMLElement} nav полоса вкладок (`nav.tabs`), уже в документе
 * @param {string} activeId id текущей вкладки
 */
function tabsOverflow(nav, activeId) {
  const all = Array.from(nav.querySelectorAll('.tab'));
  if (!all.length || typeof nav.querySelectorAll !== 'function') return;
  const more = h('button', {
    type: 'button',
    class: 'tab tab--more',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
    title: 'Остальные разделы комнаты',
  }, 'Ещё ▾');
  more.hidden = true;
  const pop = h('div', { class: 'popover tabs__more-pop', role: 'menu', 'aria-label': 'Разделы комнаты' });
  pop.hidden = true;
  let open = false;
  let onDoc = null;
  let onKey = null;

  const close = () => {
    if (!open) return;
    open = false;
    if (pop.parentNode) pop.parentNode.removeChild(pop);
    more.setAttribute('aria-expanded', 'false');
    if (onDoc) document.removeEventListener('mousedown', onDoc, true);
    if (onKey) document.removeEventListener('keydown', onKey, true);
    onDoc = null;
    onKey = null;
  };

  const openMenu = () => {
    const hiddenTabs = all.filter((a) => a.hidden);
    if (!hiddenTabs.length) return;
    pop.replaceChildren(h('div', { class: 'menu' }, ...hiddenTabs.map((a) => {
      const id = a.getAttribute('data-tab') || '';
      const on = a.classList.contains('tab--active');
      const count = a.querySelector('.tab__count');
      return h('a', {
        class: 'menu__item' + (on ? ' menu__item--on' : ''),
        href: a.getAttribute('href'),
        'aria-current': on ? 'page' : null,
        onclick: () => close(),
      }, tabTitle(id), count && count.textContent
        ? h('span', { class: 'menu__hint' }, count.textContent) : null);
    })));
    document.body.appendChild(pop);
    pop.hidden = false;
    // Положение — под кнопкой, не вылезая за окно (fixed: полоса вкладок режет overflow).
    const r = more.getBoundingClientRect();
    const w = pop.getBoundingClientRect().width || 214;
    const left = Math.max(8, Math.min(Math.round(r.left), Math.round(window.innerWidth - w - 12)));
    pop.style.top = Math.round(r.bottom + 4) + 'px';
    pop.style.left = left + 'px';
    open = true;
    more.setAttribute('aria-expanded', 'true');
    onDoc = (e) => { if (!pop.contains(e.target) && !more.contains(e.target)) close(); };
    onKey = (e) => { if (e.key === 'Escape') { close(); more.focus(); } };
    document.addEventListener('mousedown', onDoc, true);
    document.addEventListener('keydown', onKey, true);
  };

  more.addEventListener('click', () => { if (open) close(); else openMenu(); });
  nav.appendChild(more);

  const fit = () => {
    close();
    for (const a of all) a.hidden = false;
    more.hidden = true;
    const tight = () => nav.scrollWidth > nav.clientWidth + 1;
    if (!tight()) {
      more.classList.remove('tab--active');
      more.title = 'Остальные разделы комнаты';
      return;
    }
    more.hidden = false;
    // С конца, но активную вкладку — в последнюю очередь: человек должен видеть,
    // где он, а увести из-под него текущую вкладку хуже, чем соседнюю.
    const order = all.filter((a) => a.getAttribute('data-tab') !== activeId).reverse()
      .concat(all.filter((a) => a.getAttribute('data-tab') === activeId));
    for (const a of order) {
      if (!tight()) break;
      a.hidden = true;
    }
    const names = all.filter((a) => a.hidden).map((a) => tabTitle(a.getAttribute('data-tab')));
    more.classList.toggle('tab--active',
      all.some((a) => a.hidden && a.getAttribute('data-tab') === activeId));
    more.title = names.length ? 'Ещё: ' + names.join(', ') : 'Остальные разделы комнаты';
  };

  fit();
  // Ширина шапки меняется с окном и с режимом меню — пересчитываем по факту.
  if (typeof ResizeObserver === 'function') {
    let raf = 0;
    const ro = new ResizeObserver(() => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (!nav.isConnected) { ro.disconnect(); return; }
        fit();
      });
    });
    ro.observe(nav);
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
    route.name, route.roomId, route.tab, route.id, requestCrumbTitle, requestCrumbHint,
    requestFilterOf(route),
    room && room.title, roomPhase(room), room && room.project_dir,
    waitingOf(room).text,
    room && room.counts && room.counts.working,
    room && Array.isArray(room.members) ? room.members.length : 0,
    health.get('ok'), health.get('error'), dispatcherRunning(health.get('data')),
    pick(health.get('data'), 'engine.schema_ok'), navIsOpen(), chipSignature(),
  ].join('|');
  if (key === topbarKey) return;
  topbarKey = key;

  // W20-U1 (P8, пункт 12): шапка из трёх зон и минимума органов управления.
  //   lead   — крошки (где вы находитесь);
  //   meta   — этап словами («Этап: …») и переключатель охвата «Вся комната / Этот запрос»;
  //   right  — две группы значков: комната (папка, настройки комнаты) и
  //            инструменты (поиск, палитра, настройки приложения).
  // Аватары команды из шапки убраны: команда живёт на вкладке «Команда», а в
  // шапке она спорила с названием страницы и читалась как «вся команда».
  const lead = [];
  const meta = [];
  const right = [];
  let hintNode = null;

  if (route.name === 'search') {
    lead.push(h('div', { class: 'topbar__title' }, h('span', { class: 'topbar__name' }, 'Поиск')));
  } else if (route.name === 'settings') {
    lead.push(h('div', { class: 'topbar__title' }, h('span', { class: 'topbar__name' }, 'Настройки')));
  } else if (route.name === 'delo' || route.name === 'delos') {
    // W26-D6: «Дела» — отдельный экран вне комнаты: карточка (#/delo/<id>) или
    // список (#/delos). Крошек комнаты нет; название дела живёт в самой карточке.
    lead.push(h('div', { class: 'topbar__title' },
      h('span', { class: 'topbar__name' }, route.name === 'delo' ? 'Дело' : 'Дела'),
      h('span', { class: 'topbar__sub' },
        route.name === 'delo' ? 'этапы, результат и что ждёт вас' : 'что идёт и что ждёт вашего решения'),
    ));
  } else if (route.name === 'today') {
    // W24-N3: «Сегодня» — свод по всем комнатам; крошек «Комната …» здесь нет.
    lead.push(h('div', { class: 'topbar__title' },
      h('span', { class: 'topbar__name' }, 'Сегодня'),
      h('span', { class: 'topbar__sub' }, 'что ждёт вас и что идёт'),
    ));
  } else if (route.name === 'home') {
    // W27-E4: `#/` — полноценный экран («Комнаты»), а не «заготовка»: у него
    // своё название в шапке и подсказка, что здесь можно сделать.
    lead.push(h('div', { class: 'topbar__title' },
      h('span', { class: 'topbar__name' }, 'Все комнаты'),
      h('span', { class: 'topbar__sub' }, 'выберите комнату или начните новую'),
    ));
  } else if (room) {
    const phase = roomPhase(room);
    // «локально» осталось одно место — строка под сайдбаром (D13): здесь только фаза.
    // Д3: вместо голого названия комнаты — крошки «Комната «…» › …», чтобы было
    // видно, к чему относится экран: к запросу или ко всей комнате.
    const crumbs = crumbsNav(route, room);
    lead.push(h('div', { class: 'topbar__title' }, crumbs ? crumbs.nav : h('span', { class: 'topbar__name' }, room.title)));
    // P8: голое название этапа читалось как часть названия комнаты — пишем
    // «Этап: …» и объясняем слово по «?» (этап — свойство комнаты).
    const phaseText = phaseLabel(phase);
    if (phaseText) {
      meta.push(h('span', {
        class: 'chip chip--outline topbar__phase',
        title: 'Этап проекта: общий путь комнаты, а не одной задачи. Меняется в настройках комнаты.',
      }, phaseText, termHelp('phase', { className: 'term-help--chip' })));
    }
    // P8: один переключатель вместо значка «вся команда» и бейджа у крошек.
    const scope = scopeModel({
      roomId: room.id,
      tab: route.tab,
      name: route.name,
      requestId: route.name === 'request' ? route.id : requestFilterOf(route),
    });
    const switcher = scopeSwitcher(scope);
    if (switcher) meta.push(switcher);

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
    right.push(folderActionButton(roomCtx, room));
    right.push(roomSettingsButton(roomCtx, room));
    // W25-L1 (R5 §7.1): на странице запроса подсказка «Задайте папку проекта»
    // ушла в настройки комнаты (кнопка «Задать папку» в шапке осталась). Полоса
    // под вкладками стоила 34 px до содержания на каждом экране запроса.
    if (!String(room.project_dir || '').trim() && route.name !== 'request') {
      hintNode = projectDirHint(roomCtx, { roomId: room.id });
    }
  } else {
    lead.push(h('div', { class: 'topbar__title' },
      h('span', { class: 'topbar__name' }, APP_TITLE),
      h('span', { class: 'topbar__sub', title: localTitle() }, 'локально'),
    ));
  }

  // W20-U1: поиск, палитра и настройки приложения — одна группа значков с
  // подсказками. Раньше поиск стоял отдельной кнопкой и показывал «Ctrl K», хотя
  // это сочетание открывает палитру: два разных дела выглядели одним.
  // W29-P9: расход — одна строка в шапке, на любом экране. Чипа нет, пока
  // данных нет вовсе: прочерк в шапке только пугает.
  const chip = limitsChip({ navigate });
  if (chip) meta.push(chip);

  if (right.length) right.push(h('span', { class: 'topbar__divider', 'aria-hidden': 'true' }));
  right.push(toolsGroup({ navigate, openPalette }));

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
    // Д3: со страницы запроса ссылки на «От вас нужно / Отчёты / Журнал» ведут уже
    // с фильтром ?request=<корень> — на вкладке сразу виден чип «Этот запрос».
    const filterTab = route.name === 'request' && route.id ? String(route.id) : '';
    const waits = waitingOf(room);
    const tabs = h('nav', { class: 'tabs', 'aria-label': 'Разделы комнаты' });
    for (const t of TABS) {
      const isActive = t.id === active;
      // Это навигация, а не tablist: aria-current="page" + подпись у счётчика
      // дают скринридеру «От вас нужно, 9 дел, текущая страница» (F-9, D18).
      let count = null;
      if (t.id === 'backlog') {
        if (waits.n === null) {
          count = h('span', {
            class: 'tab__count count-pill count-pill--you',
            'aria-label': 'Нет данных: приложение не отвечает',
            title: 'Нет данных: приложение не отвечает',
          }, '—');
        } else if (waits.n > 0) {
          // B16: у числа есть пояснение «что считается» — иначе «7» не понять.
          const explain = waits.text + ' ждут вашего решения. Считаются вопросы без вашего'
            + ' ответа, заблокированные задачи и предложенные решения.';
          count = h('span', {
            class: 'tab__count count-pill count-pill--you',
            'aria-label': explain,
            title: explain,
          }, String(waits.n));
        }
      }
      // W20-U4 (P9): руководитель отреагировал на ваш ответ — второй, отдельный
      // бейдж на вкладке «От вас нужно» (дела и реакции — разные вещи: одно про
      // вопросы, другое про «ваш ответ учли»).
      const reactionCount = t.id === 'backlog' ? reactionTabBadge(room.id) : null;
      // W26-D9: сколько записей ждёт разбора во «Входящих». Узел возвращается
      // всегда (0 — скрыт), чтобы счётчик обновлялся без перерисовки шапки.
      const inboxCount = t.id === 'inbox' ? captureBadge(room.id) : null;
      const reactionTitle = reactionCount ? reactionCount.getAttribute('title') : '';
      const tabLabel = [
        count ? `${t.title}, ${waits.text} ждут вашего решения` : '',
        reactionTitle,
      ].filter(Boolean).join('. ');
      tabs.appendChild(h('a', {
        class: 'tab' + (isActive ? ' tab--active' : ''),
        'data-tab': t.id,
        href: FILTER_TABS.includes(t.id)
          ? scopeHash(room.id, t.id, filterTab)
          : buildHash({ roomId: room.id, tab: t.id }),
        'aria-current': isActive ? 'page' : null,
        'aria-label': tabLabel || null,
      },
      icon(t.icon, { size: 13 }),
      t.title,
      count,
      reactionCount,
      inboxCount,
      ));
    }
    bar.appendChild(tabs);
    // W26-QA1: не поместившиеся вкладки уходят в «Ещё ▾» (см. tabsOverflow).
    tabsOverflow(tabs, active);
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

/* ─────────────────────────── опрос бокового меню (WAVE17-P4: реже, одним запросом) */

// Раньше каждые 5 секунд уходило 11 запросов по всем комнатам (список + «Недавнее»
// + числа вопросов). Теперь: комнаты — из /api/rooms/summary (когда сервер умеет),
// «Недавнее» и числа — не чаще раза в минуту.
const ROOMS_POLL_MS = 15000;
const RECENT_POLL_MS = 60000;
const INBOX_POLL_MS = 45000;
let summarySupported = null; // null — не проверяли; false — сервер не умеет
let lastRecentAt = 0;
let lastInboxAt = 0;

/** Терпимо разобрать ответ /api/rooms/summary в список комнат. */
function summaryRooms(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const raw = Array.isArray(payload) ? payload
    : Array.isArray(payload.rooms) ? payload.rooms
      : payload.rooms && typeof payload.rooms === 'object' ? Object.values(payload.rooms) : null;
  if (!Array.isArray(raw)) return null;
  return raw.filter((r) => r && typeof r === 'object' && (r.id || r.room_id));
}

/**
 * Обновить счётчики комнат одним запросом сводки.
 * Сервер без `/api/rooms/summary` (старый) — просто возвращаем false: работаем как раньше.
 * @returns {Promise<boolean>} удалось ли (и можно ли не запрашивать /api/rooms)
 */
async function refreshSidebarSummary() {
  if (summarySupported === false) return false;
  try {
    const data = await api.get('/api/rooms/summary', { etag: true });
    if (data && data.notModified) { summarySupported = true; return true; }
    const list = summaryRooms(data);
    if (!list) { summarySupported = false; return false; }
    summarySupported = true;
    const current = rooms.get('list');
    if (!current.length) return true; // базового списка ещё нет — придёт из /api/rooms
    let changed = false;
    const next = current.map((room) => {
      const hit = list.find((r) => String(r.id || r.room_id || '') === String(room.id));
      if (!hit) return room;
      const counts = normalizeRoom(Object.assign({}, room, hit)).counts;
      if (counts.working === room.counts.working
          && counts.needs_you === room.counts.needs_you
          && counts.done === room.counts.done) return room;
      changed = true;
      return Object.assign({}, room, { counts });
    });
    if (changed) {
      rooms.set({ list: next, loadedAt: Date.now() });
      saveRoomsCache(next);
      renderRoomList();
      updateTitle();
    }
    return true;
  } catch {
    summarySupported = false;
    return false;
  }
}

/** Фоновая часть меню: «Недавнее» и числа — не чаще раз в минуту. */
function refreshSidebarExtras({ force = false } = {}) {
  const now = Date.now();
  if (force || now - lastRecentAt >= RECENT_POLL_MS) { lastRecentAt = now; loadRecent(); }
  if (force || now - lastInboxAt >= INBOX_POLL_MS) { lastInboxAt = now; refreshInbox(); }
}

/**
 * W26-D6: значок «Дела» в меню. Ошибку молчим — значок это украшение, экран
 * «Дела» всё равно скажет правду. Один заход при старте и после возврата в окно.
 */
async function loadDeloBadge() {
  const ids = rooms.get('list').map((r) => r.id);
  const n = await fetchNeedsBadge(api, ids);
  if (n !== null) setDelosCount(n);
}

async function loadRooms({ silent = false } = {}) {
  if (!silent) rooms.set({ loading: true });
  // Тихий опрос: сначала сводка (один запрос вместо пачки), иначе — прежний путь.
  if (silent && await refreshSidebarSummary()) {
    refreshSidebarExtras();
    return;
  }
  try {
    const payload = await api.get('/api/rooms', { etag: silent });
    if (payload && payload.notModified) {
      refreshSidebarExtras();
      return;
    }
    const list = normalizeRooms(payload);
    // W29-P7: группы левого меню идут вместе со списком комнат — один ответ,
    // одна перерисовка. Ответ без поля (старый сервер) группы не сбрасывает.
    const patch = { list, loadedAt: Date.now(), loading: false, error: null };
    if (Array.isArray(payload && payload.groups)) patch.groups = payload.groups;
    rooms.set(patch);
    // W27-E4: список — в локальную память: следующий запуск выберет адрес сразу.
    saveRoomsCache(list);
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
  refreshSidebarExtras();
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
  // B-16: каждый настоящий маршрут запоминаем (и рассказываем плагину — он
  // использует хэш при пересоздании webview). Пустой хэш здесь НЕ забываем:
  // роутер рисует «#» при старте, до того как мы прочитали память. Забывает
  // маршрут только осознанный переход «на все комнаты» (hashchange в route-memory).
  if (!isBareHash(route.hash)) routeMemory.remember(route.hash);
  // W29-Q7: открытый запрос — вкладка над страницей; «Недавнее» помнит, где вы
  // были (запрос или дело), чтобы вернуться одним нажатием.
  if (requestTabs) requestTabs.sync(route, { title: requestCrumbTitle });
  if (route.name === 'request' && route.id && route.roomId) {
    pushRecent({ id: route.id, roomId: route.roomId, title: requestCrumbTitle });
  } else if (route.name === 'delo' && route.id) {
    pushRecent({ id: route.id, kind: 'delo', title: '' });
  }
  ensureRequestCrumb(route);
  renderTopbar(route);
  markActiveRoom();
  updateTitle();
  setNavOpen(false);
  // W25-L1: смена экрана закрывает нижний лист — он принадлежит прежней странице.
  if (asideSheet) asideSheet.close();
  // W26-D9: полоса записи видна только в комнате (и на странице запроса).
  if (quickCapture) quickCapture.sync(route);
}

/* ──────────────────────────────────────────────────────────── монтирование */

/** Память маршрута: последний хэш + прокрутка (B-16). */
const routeMemory = createRouteMemory();
/** Сколько ждём проверки «запрос ещё существует», мс (сервер может тормозить). */
const ROUTE_CHECK_MS = 2500;

/* ── W21-R1: ширина вида и режим меню ────────────────────────────────────── */

/** Ручка режима меню (полоса значков) — для повторного применения без перезагрузки. */
let shellRail = null;
let quickCapture = null;
// W29-Q7: полоса вкладок открытых запросов живёт на уровне каркаса (как и
// «Надиктовать»): переживает перерисовку вида и меняется только по маршруту.
let requestTabs = null;
let viewObserver = null;
let layoutMode = null;
let asideSheet = null;

/**
 * W25-L1: ширина вида и режим раскладки. Режим (xl/l/m/s/xs) считает
 * `web/js/layout-mode.js` по ширине `#view` и пишет его в `data-layout` на
 * `#app`; CSS и JS читают ОДИН атрибут, а не два разных порога. Здесь же
 * пересчитывается режим меню: рейка считается от ширины оболочки, а не окна
 * (в `<webview>` окно хозяина недоступно, `innerWidth` — это и есть оболочка).
 */
function watchViewWidth() {
  const view = el.view;
  if (!view || !el.app) return;
  if (layoutMode) layoutMode.destroy();
  layoutMode = installLayoutMode({
    app: el.app,
    view,
    onMode: ({ mode }) => {
      // `data-view-cols` оставлен для проверок вёрстки (tools/probe_layout_split.py):
      // это ПРОИЗВОДНОЕ от режима, источник истины — `data-layout` и layout-mode.js.
      if (el.app) el.app.dataset.viewCols = String(columnsForView(layoutMode.width));
      if (shellRail) shellRail.apply();
      if (asideSheet) asideSheet.sync();
      if (mode === 'm' || mode === 's' || mode === 'xs') {
        // Лист свёрнут: развёрнутый лист на узком виде перекрыл бы свод после смены ширины.
        if (asideSheet) asideSheet.close();
      }
    },
  });
  if (el.app) el.app.dataset.viewCols = String(columnsForView(layoutMode.width));
}

/**
 * W27-E4: заглушка-скелетон до первой отрисовки.
 *
 * Пока адрес не выбран (первый запуск без локальной памяти комнат), страница
 * НЕ показывает «Комнаты» и не переключается на «Сегодня» через полсекунды —
 * именно это было видимым «прыжком». Вместо этого нейтральная заглушка в форме
 * будущих карточек; адрес в строке не меняется.
 */
function paintBootSkeleton() {
  if (!el.view) return;
  const box = h('div', { class: 'view__inner boot-start', 'data-boot': 'skeleton' },
    h('div', { class: 'boot-start__head', role: 'status', 'aria-live': 'polite' },
      h('span', { class: 'boot-start__label' }, 'Загружаю…'),
      h('span', { class: 'boot-start__note small muted' }, 'открываю рабочий экран'),
    ),
    skeletonCards({ rows: 3, label: 'Загружаю…' }),
  );
  el.view.replaceChildren(box);
}

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
    // W29-P7: раскладка меню сохранена/не сохранена — перечитать комнаты или сказать причину.
    onLayoutSaved: () => { loadRooms({ silent: true }); },
    onNotice: (text) => { if (typeof toast === 'function') toast(text, 'warn'); },
  });

  // W21-R1: меню — полоса значков на окне < 1280 px, кнопка рядом с темой,
  // выбор помнится локально. W25-L1: порог считаем от ширины ОБОЛОЧКИ, а не окна,
  // и берём её у `#app` (в webview `innerWidth` совпадает, в макете — нет).
  shellRail = installShellRail({
    app: el.app,
    button: el.sidebar.querySelector('#rail-toggle') || $('#rail-toggle'),
    getWidth: () => Math.round(el.app.clientWidth || 0),
  });
  // W25-L1: режимы раскладки (data-layout/data-aside на #app) и нижний лист
  // «От вас нужно». Лист живёт на уровне каркаса: он перекрывает страницу,
  // а не сидит внутри неё, и переживает точечную перерисовку вида.
  asideSheet = installAsideSheet({ app: el.app, view: el.view });
  watchViewWidth();

  // Затемнение под выдвижным сайдбаром — сосед .app__main, чтобы fixed
  // не привязывался к трансформированному <aside>.
  const scrim = h('div', { class: 'sidebar__scrim', 'aria-hidden': 'true', onclick: () => setNavOpen(false) });
  const mainCol = el.app.querySelector('.app__main');
  if (mainCol) el.app.insertBefore(scrim, mainCol);
  else el.app.appendChild(scrim);

  // W26-D9: полоса «Надиктовать» живёт на уровне каркаса (как и лист «От вас
  // нужно»): она перекрывает низ вида и переживает перерисовку страницы.
  quickCapture = installQuickCapture({
    app: el.app,
    host: (el.app && el.app.querySelector ? el.app.querySelector('.app__main') : null) || el.app,
    api,
    toast,
    getRoute: () => (router ? router.route : parseHash()),
    navigate: (hash) => { window.location.hash = hash; },
  });

  // W29-Q7: полоса вкладок открытых запросов — над страницей и вне #view:
  // перерисовка вида её не трогает, вкладки не мигают при обновлении свода.
  const tabsHost = h('div', {
    class: 'req-tabs', id: 'req-tabs', 'aria-label': 'Открытые задачи', hidden: true,
  });
  if (mainCol) mainCol.insertBefore(tabsHost, el.view);
  else el.app.appendChild(tabsHost);
  requestTabs = createRequestTabs({
    host: tabsHost,
    navigate: (hash) => navigate(hash),
    getRoute: () => (router ? router.route : parseHash()),
    onNotice: (text) => { if (typeof toast === 'function') toast(text, 'info'); },
  });
  // Alt+←/→ и Ctrl+W работают на любом экране: вкладки принадлежат каркасу.
  installTabKeys({ getTabs: () => requestTabs });

  router = createRouter({
    el: el.view,
    api,
    store: { rooms, currentRoom, toasts, theme, health },
    toast,
    getRoom: findRoom,
    onChange: onRoute,
  });

  renderEngineStatus();
  // W27-E4: пока адрес не выбран — заглушка-скелетон (адрес не меняется).
  // Если список комнат уже известен из локальной памяти, решение принимается
  // сразу: заглушка не нужна, и лишнего мигания не будет.
  // Роутер стартует в boot() ровно один раз, когда адрес уже известен.
  if (!rooms.get('list').length) {
    // Шапка нейтральная, но не пустая: пока адрес не выбран, она не должна
    // показывать чужой экран — только «Все комнаты».
    renderTopbar(parseHash());
    paintBootSkeleton();
  }
}

/* ─────────────────────────────────────────────────────── старт приложения */

/**
 * Существует ли запрос из маршрута (B-16: не открывать удалённый запрос).
 *
 * Ответ просим коротким: `?fields=artifacts` оставляет только ядро карточки
 * (id, название, статус) плюс необязательный блок — вместо 272 КБ детали.
 * 404/410 → запроса нет (false). Сеть молчит, сервер перезапускается или
 * ругается на параметр (400/501 — старый сервер) → `null`: маршрут НЕ
 * выбрасываем, страница покажет «не удалось открыть» с кнопкой «Повторить».
 * @param {string} id
 * @returns {Promise<boolean|null>}
 */
async function requestExists(id) {
  if (!id) return false;
  const check = api.get('/api/requests/' + encodeURIComponent(id) + '?fields=artifacts')
    .then((data) => {
      if (data && data.notModified) return true;
      return Boolean(data && (data.id || data.title || data.status || data.root_id));
    })
    .catch((err) => {
      const status = Number((err && err.status) || 0);
      if (status === 404 || status === 410) return false;
      return null;
    });
  const timeout = new Promise((resolve) => setTimeout(() => resolve(null), ROUTE_CHECK_MS));
  return Promise.race([check, timeout]);
}

/**
 * Маршрут для открытия — ОДНО решение до первой отрисовки (W27-E4).
 *
 * Порядок:
 *   1. `planStart` решает по адресу и настройке «Стартовая страница»
 *      (явный `#/` не перекидывается никогда);
 *   2. если решать нечем («Сегодня» без списка комнат) — ждём `/api/rooms`
 *      под заглушкой-скелетоном и решаем ОДИН раз: второй отрисовки с другим
 *      адресом не будет;
 *   3. устаревший запрос из памяти не восстанавливаем (B-16), но явный адрес
 *      не выбрасываем: страница сама скажет «не удалось открыть».
 *
 * @returns {Promise<string>} хэш, который надо открыть
 */
async function planBootRoute() {
  const stored = routeMemory.read();
  const urlHash = location.hash;
  const startPref = readStartPref();
  let last = '';
  try { last = localStorage.getItem(LAST_ROOM_KEY) || ''; } catch { /* ignore */ }
  // `null` — список комнат ещё не известен (кэша нет): «Сегодня» решать нечем,
  // ждём `/api/rooms` под заглушкой. Пустой МАССИВ — уже ответ «комнат нет».
  const knownIds = () => (rooms.get('list').length ? rooms.get('list').map((room) => room.id) : null);
  let roomIds = knownIds();
  const ask = () => planStart({
    urlHash,
    startPref,
    stored,
    roomIds,
    tabIds: TAB_IDS,
    defaultRoom: last,
  });

  let plan = ask();
  if (!plan.ready) {
    // Первый запуск без локальной памяти: адрес не трогаем, ждём список.
    await loadRooms();
    roomIds = knownIds();
    plan = ask();
  }
  if (!plan.ready) return '#/';

  // Память могла хранить удалённый запрос — проверяем только этот случай.
  const route = parseRouteHash(plan.hash);
  if (route && route.kind === 'request' && plan.why !== 'deep-link') {
    const ok = await requestExists(route.id);
    if (ok === false) return buildRoomHash(route.roomId, 'tasks');
  }
  return plan.hash;
}

/**
 * Старт роутера: адрес выставляется ОДИН раз и без события `hashchange`
 * (`replaceState`), поэтому первая отрисовка сразу правильная — прыжка нет.
 * @param {string} target
 */
function startRouter(target) {
  const hash = target || '#/';
  if (hash !== location.hash) {
    try {
      history.replaceState(null, '', hash);
    } catch {
      location.replace(hash);   // старый браузер — как раньше, через адрес
    }
  }
  if (!router) return;
  router.start();
  routeMemory.remember(router.route.hash || hash);
  routeMemory.restore(router.route.hash || hash);
}

async function boot() {
  const { pref, persist } = readThemePref();
  applyTheme(pref, persist);
  // B-16: память маршрута подключаем ДО роутера — иначе первая смена хэша
  // пришла бы после перерисовки и позиция прокрутки потерялась бы.
  routeMemory.connect();
  // W27-E4: список комнат из локальной памяти — ДО первой отрисовки. Меню и
  // выбор стартового адреса сразу знают, есть ли комнаты.
  const cached = readRoomsCache();
  if (cached.length) rooms.set({ list: cached, loading: true, error: null });
  initShellFeatures({
    toast,
    api,
    stores: { rooms, currentRoom, toasts, theme, health },
    navigate,
    getRoute: () => (router ? router.route : parseHash()),
    render: () => { if (router) router.render(); },
  });
  installHotkeys({ closeNav: () => setNavOpen(false) });
  // W20-U3: горячие клавиши страниц (J/K по вопросам, Ctrl+Enter из поля, `?` —
  // справка, `V` — простой/подробный вид). Ставим один раз на всё приложение:
  // обработчик не переустанавливается при переходах между экранами.
  installFieldHotkeys({
    onHelp: () => openHelp(),
    onToggleView: () => toggleView(),
  });
  // W27-E4: клавиша G, H (и любые другие клавиши навигации) просит каркас
  // перейти по адресу событием — так клавиши не знают про роутер.
  window.addEventListener(NAV_EVENT, (ev) => {
    const hash = String((ev && ev.detail && ev.detail.hash) || '').trim();
    if (hash) navigate(hash);
  });
  applyViewMode();
  initStatusBanners({
    pick,
    toast,
    retry: () => { loadHealth(); loadRooms({ silent: true }); },
    retryHealth: () => { loadHealth(); },
  });
  mountShell();
  applyNotifyLabel();

  // W27-E4: сначала адрес (один раз), потом первая отрисовка. Пока решаем
  // (только когда кэша нет) — заглушка-скелетон в #view.
  startRouter(await planBootRoute());
  renderRoomList();
  updateTitle();

  // Данные — следом, уже на правильном экране: без «прыжка» и без пустого свода.
  loadHealth();
  // Список уже пришёл (кэш или ожидание в planBootRoute) — тихое обновление;
  // иначе полная загрузка, иначе меню осталось бы пустым.
  loadRooms(rooms.get('loadedAt') ? { silent: true } : {});
  loadTodayBadge();
  loadDeloBadge().catch(() => {});

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

  // Переименование на странице запроса: крошка и заголовок окна обновляются сразу,
  // без перезагрузки и без ожидания опроса (W17-G §1).
  window.addEventListener('teamroom:request-title', (ev) => {
    const data = (ev && ev.detail) || {};
    if (String(data.id || '') !== requestCrumbId) return;
    requestCrumbTitle = String(data.title_short || '');
    topbarKey = '';
    // Переименование сразу видно и во вкладке, и в «Недавнем».
    renameRecent(requestCrumbId, requestCrumbTitle);
    if (requestTabs && router) requestTabs.sync(router.route, { title: requestCrumbTitle });
    const route = router ? router.route : parseHash();
    if (route) renderTopbar(route);
    // «Недавнее» в сайдбаре тоже должно показать новое название сразу.
    loadRecent();
  });

  // W29-Q7: звезда поставлена/снята на странице запроса — «Закладки» в меню
  // меняются сразу, без ожидания опроса сводки.
  window.addEventListener('teamroom:bookmarks', () => { renderBookmarks(); });

  subscribeInbox(() => {
    const route = router ? router.route : parseHash();
    if (route) renderTopbar(route);
    updateTitle();
    renderRoomList();
  });

  // W20-U4 (P9): пришли новые реакции руководителя (или запрос открыли и отметка
  // «прочитано» сняла новизну) → пересобрать бейдж вкладки и «Недавнее».
  subscribeReactions(() => {
    const route = router ? router.route : parseHash();
    if (route) renderTopbar(route);
    loadRecent();
  });

  startInboxPolling();

  poll(() => loadRooms({ silent: true }), ROOMS_POLL_MS, { pauseHidden: true, immediate: false, onError: () => {} });
  poll(() => loadHealth(), 15000, { pauseHidden: true, immediate: false, onError: () => {} });
  // W29-P9: расход в шапке — раз в минуту, только когда вкладка видна.
  refreshLimits(api).then(() => {
    const r = router ? router.route : parseHash();
    if (r) renderTopbar(r);
  });
  poll(() => refreshLimits(api, { force: true }).then(() => {
    const r = router ? router.route : parseHash();
    if (r) renderTopbar(r);
  }), 60000, { pauseHidden: true, immediate: false, onError: () => {} });

  // W29-Q7: уход со страницы не должен «съесть» отложенное действие — оно
  // выполняется сразу, а не пропадает вместе с окном.
  window.addEventListener('pagehide', () => {
    try { sharedUndo().commitAll(); } catch { /* окно уже закрывается */ }
  });

  window.addEventListener('online', () => { loadHealth(); loadRooms({ silent: true }); loadTodayBadge(); loadDeloBadge().catch(() => {}); });
  // Вернулись в окно — число в меню могло устареть (опрос «Сегодня» шёл без нас).
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadTodayBadge();
  });

  document.addEventListener('click', (e) => {
    const a = e.target instanceof Element ? e.target.closest('a[href^="#"]') : null;
    if (a) setNavOpen(false);
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();

export { api, navigate, router };
