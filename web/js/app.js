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
  createRouteMemory, chooseHash, parseRouteHash, isBareHash,
} from './route-memory.js';
import { rooms, currentRoom, toasts, theme, health } from './store.js';
import {
  initShellFeatures, installHotkeys, openRoomDialog, toggleNotifications,
  applyNotifyLabel, notifyChanges, openPalette,
} from './shell-features.js';
import { initStatusBanners, renderBanners, dispatcherRunning } from './shell-status.js';
import {
  APP_TITLE, pick, buildSidebar, renderRoomList, markActiveRoom, loadRecent,
  renderEngineStatus, localTitle, setTodayCount, setDelosCount,
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
import { installHotkeys as installFieldHotkeys, openHotkeyHelp } from './hotkeys.js';
import { applyViewMode, toggleViewMode } from './view-mode.js';
import {
  FILTER_TABS as SCOPE_FILTER_TABS, phaseLabel, scopeModel, scopeSwitcher, toolsGroup,
} from './topbar-scope.js';
import { termHelp } from './term-help.js';
// W20-U4 (P9): «руководитель отреагировал на ваш ответ» — бейдж вкладки и отметки.
import { reactionTabBadge, subscribeReactions } from './views/reaction-notice.js';

export { dispatcherRunning };

const THEME_KEY = 'teamroom.theme';
const LAST_ROOM_KEY = 'teamroom.lastRoom';
// W24-N3: «Стартовая страница» — «Сегодня» (по умолчанию) или последняя комната.
const START_KEY = 'teamroom.start';
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

/** Стартовая страница из настроек: 'today' (по умолчанию) или 'last'. */
export function readStartPref() {
  try {
    const saved = localStorage.getItem(START_KEY);
    return saved === 'last' ? 'last' : 'today';
  } catch { return 'today'; }
}

/** Сохранить выбор стартовой страницы (экран «Настройки»). */
export function saveStartPref(value) {
  const next = value === 'last' ? 'last' : 'today';
  try { localStorage.setItem(START_KEY, next); } catch { /* приватный режим */ }
  return next;
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
    pick(health.get('data'), 'engine.schema_ok'), navIsOpen(),
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
        title: 'Этап проекта: общий путь комнаты, а не одного запроса. Меняется в настройках комнаты.',
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
  quickCapture.sync(router.route);
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
 * Маршрут для открытия: явный хэш → сохранённый последний → комната.
 * Решение принимает `chooseHash` (чистая функция, покрыта тестами).
 */
async function initialHash() {
  const stored = routeMemory.read();
  const urlHash = location.hash;
  const candidate = isBareHash(urlHash) ? stored.hash : urlHash;
  const route = parseRouteHash(candidate);
  const requestOk = route && route.kind === 'request' ? await requestExists(route.id) : null;
  let last = '';
  try { last = localStorage.getItem(LAST_ROOM_KEY) || ''; } catch { /* ignore */ }
  // W24-N3: «Стартовая страница» = «Сегодня» и в приложении есть хотя бы одна
  // комната → открываем «Сегодня». Явный адрес в строке (`#/room/...`) сильнее
  // настройки: человек пришёл по ссылке, а не «просто открыл приложение».
  if (readStartPref() === 'today' && isBareHash(urlHash) && rooms.get('list').length) {
    return '#/today';
  }
  return chooseHash({
    urlHash,
    stored,
    roomIds: rooms.get('list').map((room) => room.id),
    tabIds: TAB_IDS,
    requestOk,
    defaultRoom: last,
  });
}

async function boot() {
  const { pref, persist } = readThemePref();
  applyTheme(pref, persist);
  // B-16: память маршрута подключаем ДО роутера — иначе первая смена хэша
  // пришла бы после перерисовки и позиция прокрутки потерялась бы.
  routeMemory.connect();
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
  applyViewMode();
  initStatusBanners({
    pick,
    toast,
    retry: () => { loadHealth(); loadRooms({ silent: true }); },
    retryHealth: () => { loadHealth(); },
  });
  mountShell();
  applyNotifyLabel();

  await Promise.all([loadHealth(), loadRooms(), loadTodayBadge()]);
  loadDeloBadge().catch(() => {});
  // B-16: открываем ПОЛНЫЙ прежний маршрут (явный хэш → память → комната),
  // а не «Запрос» последней комнаты. Устаревший запрос не восстанавливаем.
  const target = await initialHash();
  if (target && target !== location.hash) {
    // hashchange → router.render + route-memory.restore (прокрутка)
    location.replace(target);
  } else {
    routeMemory.remember(target || '#/');
    routeMemory.restore(target || '');
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

  // Переименование на странице запроса: крошка и заголовок окна обновляются сразу,
  // без перезагрузки и без ожидания опроса (W17-G §1).
  window.addEventListener('teamroom:request-title', (ev) => {
    const data = (ev && ev.detail) || {};
    if (String(data.id || '') !== requestCrumbId) return;
    requestCrumbTitle = String(data.title_short || '');
    topbarKey = '';
    const route = router ? router.route : parseHash();
    if (route) renderTopbar(route);
    // «Недавнее» в сайдбаре тоже должно показать новое название сразу.
    loadRecent();
  });

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
