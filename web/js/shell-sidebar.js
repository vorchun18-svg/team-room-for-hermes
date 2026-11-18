/**
 * shell-sidebar.js — сайдбар приложения: марка, список комнат, «Недавнее»,
 * индикатор движка и строка «локально», подвал с кнопками.
 *
 * Вынесено из app.js, чтобы каркас (тема, маршрут, шапка, опрос) оставался
 * короче ~650 строк. Модуль не знает про app.js: всё, что нужно от каркаса
 * (открыть диалог комнаты, уведомления, тему), приходит колбэками в buildSidebar().
 */

import { h } from './util.js';
import { icon, hasIcon } from './icons.js';
import { api } from './api.js';
import { buildHash, parseHash } from './router.js';
import { rooms, health } from './store.js';
import { dispatcherRunning } from './shell-status.js';
import { waitingFor } from './shell-inbox.js';
import { countText, COUNT_FORMS } from './shell-labels.js';
import { titleShortOf, titleHint } from './request-title.js';
import { ingestSummary, recentReactionMark } from './views/reaction-notice.js';
import { viewModeButton, watchViewMode, isSimple } from './view-mode.js';
import { FOLD_TITLE, railBadges, roomNavTitle } from './shell-rail.js';

export const APP_TITLE = 'Комната команды';
const RECENT_LIMIT = 5;

/** Текст плашки демо-режима — один и тот же в сайдбаре и в тестах. */
export const DEMO_TEXT = 'ДЕМО — данные выдуманные, агенты не запускаются';

/** Мок-движок? Реальный сервер поле engine.mock не отдаёт. */
export function isMockHealth(data) {
  return !!(data && typeof data === 'object' && data.engine && data.engine.mock === true);
}

/** Заголовок вкладки: «ДЕМО · Комната команды» с сохранением счётчика «(3)». */
export function demoTabTitle(title) {
  const raw = String(title === null || title === undefined ? '' : title).trim();
  const count = /^\(\d+\)\s*/.exec(raw);
  const rest = raw.replace(/^\(\d+\)\s*/, '').replace(/^ДЕМО · /, '');
  const base = rest || APP_TITLE;
  return (count ? count[0] : '') + 'ДЕМО · ' + base;
}

/** Плашка «ДЕМО» для сайдбара; null, если движок боевой. */
export function demoBadge(data) {
  if (!isMockHealth(data)) return null;
  return h('div', {
    class: 'demo-badge',
    id: 'demo-badge',
    role: 'status',
    'aria-live': 'polite',
    dataset: { demo: '1' },
    title: DEMO_TEXT,
    style: {
      display: 'flex', alignItems: 'flex-start', gap: '8px',
      margin: '0 8px 8px', padding: '8px 10px', borderRadius: '8px',
      background: 'var(--warn-soft)', color: 'var(--warn-ink)',
      border: '1px solid color-mix(in srgb, var(--warn) 35%, transparent)',
      fontSize: '11.5px', lineHeight: '1.35',
    },
  },
  icon('beaker', { size: 14 }),
  h('span', { class: 'demo-badge__text' }, DEMO_TEXT));
}

/** Перерисовать плашку по /api/health. */
export function renderDemoBadge() {
  if (typeof document === 'undefined' || !document.querySelector) return;
  const slot = document.querySelector('#demo-badge-slot');
  if (!slot) return;
  const badge = demoBadge(health.get('data'));
  slot.replaceChildren(...(badge ? [badge] : []));
}

/** Префикс «ДЕМО ·» в tab браузера, когда health.engine.mock=true. */
export function applyDemoTitle() {
  if (typeof document === 'undefined') return;
  if (!isMockHealth(health.get('data'))) return;
  const next = demoTabTitle(document.title);
  if (document.title !== next) document.title = next;
}

let titleObserver = null;

/** app.js пересчитывает title на каждом опросе — следим за <title> и держим префикс. */
function watchDemoTitle() {
  if (titleObserver || typeof MutationObserver === 'undefined' || typeof document === 'undefined') return;
  if (!document.head) return;
  titleObserver = new MutationObserver(() => applyDemoTitle());
  titleObserver.observe(document.head, { subtree: true, childList: true, characterData: true });
}

const $ = (sel) => document.querySelector(sel);
export const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/* ─────────────────────────────────────────────────────────────── утилиты */

/** Достать значение по пути 'a.b.c' из объекта. */
export function pick(obj, path) {
  if (!obj) return undefined;
  let cur = obj;
  for (const key of String(path).split('.')) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[key];
  }
  return cur;
}


const navItems = new Map();

/* ─────────────────────────────────────────────────────────────── сайдбар */

function createNavItem(room) {
  const badges = h('span', { class: 'nav-item__badges' });
  const node = h('a', {
    class: 'nav-item',
    href: buildHash({ roomId: room.id, tab: 'composer' }),
    dataset: { room: room.id },
    // W21-R1: в полосе значков подписи скрыты — имя комнаты живёт в подсказке
    // и в aria-label (иначе значок комнаты не читается ни мышью, ни скринридером).
    title: room.title, 'aria-label': room.title,
  }, icon(room.icon, { size: 15 }),
  // Подсказка и на самой подписи: длинное название обрезается многоточием, и
  // проверка вёрстки (ui_audit) требует title у обрезанного элемента, а не только у строки.
  h('span', { class: 'nav-item__text', title: room.title }, room.title), badges);
  return { node, badges, room };
}

function updateNavItem(item, room) {
  item.room = room;
  item.node.href = buildHash({ roomId: room.id, tab: 'composer' });
  const c = room.counts || {};
  // W26-D7: число у комнаты — это ждущие ДЕЛА из общего склада (вопросы,
  // решения, блокировки, разрешения), а не `counts.needs_you` сервера: там
  // реакции руководителя и запросы, у которых нет ни одного действия.
  const waits = waitingFor(room);
  const badges = railBadges(waits, c);
  // Подсказка несёт и название, и числа: в полосе значков подписи нет.
  const navTitle = roomNavTitle(room.title, badges);
  if (item.node.title !== navTitle) item.node.title = navTitle;
  if (item.node.getAttribute('aria-label') !== navTitle) item.node.setAttribute('aria-label', navTitle);
  item.node.dataset.waiting = waits.n === null ? '' : String(badges.you ? badges.you.n : 0);
  const text = item.node.querySelector('.nav-item__text');
  if (text && text.textContent !== room.title) text.textContent = room.title;
  if (text && text.title !== room.title) text.title = room.title;
  const ic = item.node.querySelector('.codicon');
  if (ic && room.icon) ic.className = 'codicon codicon-' + (hasIcon(room.icon) ? room.icon : 'home');

  item.badges.replaceChildren();
  if (waits.n === null) {
    // Не знаем — так и говорим (D19): «0» и число запросов вместо дел врут.
    const hint = rooms.get('error')
      ? 'Нет данных: приложение не отвечает'
      : 'Считаю, сколько дел ждёт вас: число появится через пару секунд';
    item.badges.appendChild(h('span', {
      class: 'count-pill count-pill--you count-pill--pending',
      title: hint, 'aria-label': hint,
    }, '—'));
  } else if (badges.you && badges.you.n > 0) {
    item.badges.appendChild(h('span', {
      class: 'count-pill count-pill--you',
      title: 'Ждут вас: ' + waits.text,
      'aria-label': 'Ждут вас: ' + waits.text,
    }, String(badges.you.n)));
  }
  if (badges.live) {
    const works = countText(badges.live.n, COUNT_FORMS.work);
    item.badges.appendChild(h('span', {
      class: 'count-pill count-pill--live',
      title: 'Идёт: ' + works,
      'aria-label': 'Идёт: ' + works,
    }, h('span', { class: 'count-pill__dot' }), String(badges.live.n)));
  }
}

export function renderRoomList() {
  const nav = $('#room-list');
  if (!nav) return;
  const list = rooms.get('list');
  const seen = new Set();

  for (const room of list) {
    let item = navItems.get(room.id);
    if (!item) {
      item = createNavItem(room);
      navItems.set(room.id, item);
      nav.appendChild(item.node);
    }
    updateNavItem(item, room);
    nav.appendChild(item.node);
    seen.add(room.id);
  }
  for (const [id, item] of Array.from(navItems)) {
    if (!seen.has(id)) { item.node.remove(); navItems.delete(id); }
  }

  const empty = $('#rooms-empty');
  if (empty) {
    empty.classList.toggle('hidden', list.length > 0);
    const note = empty.querySelector('[data-empty-note]');
    if (note && rooms.get('error')) note.textContent = rooms.get('error');
  }
  markActiveRoom();
}

export function markActiveRoom() {
  const id = parseHash().roomId;
  const route = parseHash();
  for (const [roomId, item] of navItems) {
    item.node.classList.toggle('nav-item--active', roomId === id);
    if (roomId === id) item.node.setAttribute('aria-current', 'page');
    else item.node.removeAttribute('aria-current');
  }
  const todayLink = typeof document !== 'undefined' && document.getElementById
    ? document.getElementById('today-link') : null;
  if (todayLink) {
    const on = route && route.name === 'today';
    todayLink.classList.toggle('nav-item--active', !!on);
    if (on) todayLink.setAttribute('aria-current', 'page');
    else todayLink.removeAttribute('aria-current');
  }
  // W26-D6: «Дела» подсвечиваются и на списке (#/delos), и на карточке (#/delo/<id>),
  // и на вкладке комнаты (tab === 'delos').
  const delosLink = typeof document !== 'undefined' && document.getElementById
    ? document.getElementById('delos-link') : null;
  if (delosLink) {
    const on = !!route && (route.name === 'delos' || route.name === 'delo' || route.tab === 'delos');
    delosLink.classList.toggle('nav-item--active', on);
    if (on) delosLink.setAttribute('aria-current', 'page');
    else delosLink.removeAttribute('aria-current');
  }
  applyDemoTitle();
}

/**
 * Число «ждёт вас» у пункта «Сегодня» (W24-N3).
 *
 * Считается на экране «Сегодня» и при загрузке приложения — из `/api/today`.
 * `null` — данных ещё нет: значок не рисуем, но и «0» не врём.
 * @param {number|null} count
 */
export function setTodayCount(count) {
  if (typeof document === 'undefined' || !document.getElementById) return;
  const host = document.getElementById('today-badge');
  if (!host) return;
  const n = Number(count);
  const value = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  const label = value > 0 ? 'Ждут вас: ' + countText(value, COUNT_FORMS.case) : '';
  if (!value) {
    host.replaceChildren();
    host.setAttribute('data-empty', '1');
    const link = document.getElementById('today-link');
    if (link) link.setAttribute('aria-label', 'Сегодня');
    return;
  }
  host.setAttribute('data-empty', null);
  const pill = h('span', {
    class: 'count-pill count-pill--you',
    title: 'Ждут вас: ' + value,
    'aria-label': 'Ждут вас: ' + value,
  }, String(value));
  host.replaceChildren(pill);
  const link = document.getElementById('today-link');
  if (link) link.setAttribute('aria-label', 'Сегодня — ' + label);
}

/**
 * W26-D6: число «дел, где ждёт ваше решение» у пункта «Дела».
 * `null` — данных ещё нет (значок не рисуем, но и «0» не врём).
 * @param {number|null} count
 */
export function setDelosCount(count) {
  if (typeof document === 'undefined' || !document.getElementById) return;
  const host = document.getElementById('delos-badge');
  if (!host) return;
  const n = Number(count);
  const value = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  const link = document.getElementById('delos-link');
  if (!value) {
    host.replaceChildren();
    if (link) link.setAttribute('aria-label', 'Дела');
    return;
  }
  const label = 'Дела: ждут вашего решения — ' + value;
  host.replaceChildren(h('span', {
    class: 'count-pill count-pill--you', title: label, 'aria-label': label,
  }, String(value)));
  if (link) link.setAttribute('aria-label', 'Дела — ждут вашего решения: ' + value);
}

/* ─────────────────────────────────────── «Недавнее»: последние запросы */

let recentSig = '';
let recentLoading = false;

const TONE = {
  done: 'ok',
  failed: 'danger',
  needs_you: 'warn',
  working: 'info',
  reviewing: 'info',
  synthesizing: 'info',
  queued: 'muted',
};

/**
 * «Недавнее» одним запросом: `GET /api/rooms/summary` отдаёт `last[]` всех комнат
 * с короткими названиями (W17-C §2.4, W17-G §4). Сервер без сводки — null,
 * тогда работаем по-старому (по одному запросу на комнату).
 * @param {object[]} list комнаты
 * @returns {Promise<object[]|null>}
 */
async function recentFromSummary(list) {
  try {
    const data = await api.get('/api/rooms/summary?requests=' + RECENT_LIMIT, { etag: true });
    // 304 — данные не менялись: список уже на экране, ничего не перерисовываем.
    if (data && data.notModified) return 'unchanged';
    if (!data) return null;
    // W20-U4 (P9): та же сводка несёт реакции руководителя — запоминаем их здесь,
    // чтобы бейдж вкладки и отметки в «Недавнем» не делали второй запрос.
    ingestSummary(data);
    const raw = Array.isArray(data.rooms) ? data.rooms
      : (data.rooms && typeof data.rooms === 'object' ? Object.values(data.rooms) : null);
    if (!Array.isArray(raw) || !raw.length) return null;
    const items = [];
    for (const room of raw) {
      if (!room || typeof room !== 'object') continue;
      const roomTitle = String(room.title || '');
      const roomId = String(room.id || room.room_id || '');
      const last = Array.isArray(room.last) ? room.last : [];
      for (const rec of last) {
        if (!rec || !rec.id) continue;
        items.push({
          id: String(rec.id),
          title: String(rec.title || ''),
          title_short: String(rec.title_short || ''),
          preview: String(rec.preview || ''),
          status: rec.status,
          at: rec.created_at || rec.finished_at,
          roomId,
          roomTitle,
          // W20-U3 (P13): закреплённый запрос держится сверху «Недавнего».
          pinned: rec.pinned === true,
          pinned_at: Number(rec.pinned_at) || 0,
        });
      }
    }
    items.sort((a, b) => (Number(b.pinned) - Number(a.pinned))
      || ((Number(b.at) || 0) - (Number(a.at) || 0)));
    return items.slice(0, RECENT_LIMIT);
  } catch {
    return null;
  }
}

/** Собрать последние запросы по всем комнатам (одним запросом сводки, иначе порциями). */
export async function loadRecent() {
  if (recentLoading) return;
  const list = rooms.get('list').filter((r) => !r.archived);
  if (!list.length) { renderRecent([]); return; }

  recentLoading = true;
  try {
    const viaSummary = await recentFromSummary(list);
    if (viaSummary === 'unchanged') return;
    if (Array.isArray(viaSummary)) { renderRecent(viaSummary); return; }
    const batches = await Promise.all(list.map(async (room) => {
      try {
        const data = await api.get('/api/rooms/' + enc(room.id) + '/requests?limit=' + RECENT_LIMIT);
        const items = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
        return items
          .filter((i) => i && i.id && !i.archived)
          .map((i) => ({ id: i.id, title: i.title || '', title_short: i.title_short || '',
            preview: i.preview || '', status: i.status, at: i.created_at, roomId: room.id,
            roomTitle: room.title, pinned: i.pinned === true }));
      } catch {
        return [];
      }
    }));
    const merged = batches.flat()
      .sort((a, b) => (Number(b.pinned) - Number(a.pinned))
        || ((Number(b.at) || 0) - (Number(a.at) || 0)))
      .slice(0, RECENT_LIMIT);
    renderRecent(merged);
  } finally {
    recentLoading = false;
  }
}

function renderRecent(items) {
  const host = $('#sidebar-recent');
  const list = $('#recent-list');
  if (!host || !list) return;

  const sig = items.map((i) => [i.id, i.title_short, titleShortOf(i), i.status, i.roomId,
    i.pinned ? 'pin' : '',
    recentReactionMark(i) ? '!' : ''].join('|')).join('#');
  if (sig === recentSig) return;
  recentSig = sig;

  list.replaceChildren();
  if (!items.length) {
    host.classList.add('hidden');
    return;
  }
  host.classList.remove('hidden');
  for (const item of items) {
    const tone = TONE[String(item.status || '')] || 'muted';
    // B8/P5: короткое название на экране, полный текст диктовки — в подсказке.
    const short = titleShortOf(item) || 'Без названия';
    const hint = titleHint(item, item.roomTitle ? 'Комната: ' + item.roomTitle : '');
    const fullHint = hint || short;
    // W20-U4 (P9): руководитель ответил на ваш ответ в этом запросе — отметка
    // справа от названия, пока запрос не открыли.
    const mark = recentReactionMark(item);
    list.appendChild(h('a', {
      class: 'recent__item' + (mark ? ' recent__item--reacted' : '') + (item.pinned ? ' recent__item--pinned' : ''),
      href: '#/room/' + enc(item.roomId) + '/request/' + enc(item.id),
      title: (item.pinned ? 'Закреплён · ' : '') + fullHint,
      'aria-label': short + ' — ' + String(item.roomTitle || 'комната')
        + (item.pinned ? ', закреплён' : '')
        + (mark ? ', есть новая реакция руководителя' : ''),
    },
    h('span', { class: 'status-dot status-dot--' + tone, 'aria-hidden': 'true' }),
    h('span', { class: 'recent__body' },
      h('span', { class: 'recent__title', title: fullHint },
        item.pinned ? icon('pin', { size: 11, className: 'recent__pin' }) : null,
        short),
      h('span', { class: 'recent__room', title: String(item.roomTitle) }, String(item.roomTitle))),
    mark,
    ));
  }
}

/* ─────────────────────────────────────────── статус движка и «локально» */

/** Состояние движка коротко: точка + подпись, подробности — в title. */
function engineInfo() {
  const data = health.get('data');
  const dataPath = pick(data, 'paths.data') || pick(data, 'data_dir') || 'data/';
  const vault = pick(data, 'paths.vault') || pick(data, 'vault_dir') || '';
  // Адрес и порт живут только в «Настройки → Здоровье»: в сайдбаре — суть, не техника.
  const details = [];
  details.push('Данные: ' + dataPath);
  if (vault) details.push('Заметки: ' + vault);

  if (health.get('loading')) {
    return { tone: 'warn', label: 'Проверяю связь…', title: 'Приложение проверяет связь с движком команды.' };
  }
  if (health.get('error') || !data) {
    return {
      tone: 'danger',
      label: 'Нет связи с приложением',
      title: ['Приложение не отвечает.', String(health.get('error') || '')].filter(Boolean).join(' '),
    };
  }
  const schemaBad = pick(data, 'engine.schema_ok') === false;
  const disp = dispatcherRunning(data);
  if (schemaBad) {
    return {
      tone: 'danger',
      label: 'Движок: база изменилась',
      title: ['Данные на доске не совпадают с ожидаемым форматом — доступно только чтение.', ...details].join('\n'),
    };
  }
  if (disp === false) {
    return {
      tone: 'warn',
      label: 'Движок на паузе',
      title: ['Новые запросы не начнут работу, пока движок не запущен.', ...details].join('\n'),
    };
  }
  return { tone: 'ok', label: 'Движок работает', title: ['Очередь задач обрабатывается.', ...details].join('\n') };
}

/** Перерисовать компактный индикатор движка и строку «локально». */
export function renderEngineStatus() {
  renderDemoBadge();
  applyDemoTitle();
  const box = $('#engine');
  if (!box) return;
  const info = engineInfo();
  box.replaceChildren(
    h('div', { class: 'engine__row', title: info.title },
      h('span', { class: 'status-dot status-dot--' + info.tone, 'aria-hidden': 'true' }),
      h('span', { class: 'engine__text' }, info.label)),
    h('div', { class: 'engine__local', title: localTitle() },
      icon('lock', { size: 11 }),
      h('span', null, 'локально')),
  );
}

/** Подсказка строки «локально»: полный адрес и пути. */
export function localTitle() {
  const data = health.get('data');
  const dataPath = pick(data, 'paths.data') || pick(data, 'data_dir') || 'data/';
  const vault = pick(data, 'paths.vault') || pick(data, 'vault_dir') || '';
  return ['Только на этом компьютере: внешних сервисов нет, всё лежит в локальных папках.',
    'Данные: ' + dataPath, vault ? 'Заметки: ' + vault : ''].filter(Boolean).join('\n');
}

/* ─────────────────────────────────────────────────────── каркас сайдбара */

/**
 * Собрать содержимое сайдбара (марка, комнаты, «Недавнее», подвал).
 * @param {HTMLElement} host контейнер #sidebar
 * @param {{onNewRoom: Function, onToggleNotifications: Function, onCycleTheme: Function}} actions
 */
export function buildSidebar(host, { onNewRoom, onToggleNotifications, onCycleTheme, onCloseNav }) {
  host.replaceChildren();
  watchDemoTitle();
  // W26-D7: шеврон на правом краю панели — 24 px, у верха, виден всегда
  // (и в развёрнутом меню, и в полосе значков). Id сохранён (`#rail-toggle`),
  // потому что каркас ищет кнопку по нему; подсказка — с клавишей.
  host.appendChild(h('button', {
    class: 'rail-fold', type: 'button', id: 'rail-toggle', 'data-rail-toggle': '',
    title: FOLD_TITLE, 'aria-label': 'Свернуть меню',
    'aria-pressed': 'false', 'aria-controls': 'room-list',
  }, icon('chevron-left', { size: 14 })));
  host.appendChild(h('div', { class: 'brand' },
    h('div', { class: 'brand__mark', 'aria-hidden': 'true' }, 'К'),
    h('div', { class: 'brand__text' },
      h('div', { class: 'brand__name' }, APP_TITLE),
      h('div', { class: 'brand__sub' }, 'только на этом компьютере'),
    ),
    // Видна только в выдвижном режиме (<900 px): явный способ закрыть меню.
    h('button', {
      class: 'btn btn--ghost btn--icon btn--sm sidebar__close', type: 'button',
      'aria-label': 'Закрыть меню комнат', title: 'Закрыть меню',
      onclick: () => { if (typeof onCloseNav === 'function') onCloseNav(); },
    }, icon('close', { size: 15 })),
  ));
  host.appendChild(h('div', { id: 'demo-badge-slot', 'aria-live': 'polite' }));
  renderDemoBadge();

  // Прокручиваемая середина: комнаты и «Недавнее». Подвал закреплён снизу.
  const scroll = h('div', { class: 'sidebar__scroll' },
    // W24-N3: «Сегодня» — первым пунктом меню. Число «ждёт вас» приходит из
    // /api/today (ставит экран «Сегодня» и загрузка приложения), не из комнат:
    // там считаются запросы, а здесь — дела.
    h('nav', { class: 'nav nav--top', id: 'today-nav', 'aria-label': 'Главное' },
      h('a', {
        class: 'nav-item nav-item--today', href: '#/today', id: 'today-link',
        title: 'Сегодня: что ждёт вас, что горит и что идёт', 'aria-label': 'Сегодня',
      }, icon('calendar', { size: 15 }),
      h('span', { class: 'nav-item__text' }, 'Сегодня'),
      h('span', { class: 'nav-item__badges', id: 'today-badge' }))),
    // W26-D6: «Дела» — вторым пунктом главного меню. Число — дела, у которых есть
    // «ждёт вашего решения» (ставит экран «Дела»); 0 — значок не рисуем.
    h('nav', { class: 'nav nav--top', id: 'delos-nav', 'aria-label': 'Дела' },
      h('a', {
        class: 'nav-item nav-item--delos', href: '#/delos', id: 'delos-link',
        title: 'Дела: что идёт, что ждёт вашего решения и чем всё кончилось',
        'aria-label': 'Дела',
      }, icon('checklist', { size: 15 }),
      h('span', { class: 'nav-item__text' }, 'Дела'),
      h('span', { class: 'nav-item__badges', id: 'delos-badge' }))),
    h('div', { class: 'sidebar__label' },
      'Комнаты',
      h('button', {
        class: 'btn btn--ghost btn--icon btn--sm', type: 'button', 'aria-label': 'Новая комната', title: 'Новая комната',
        onclick: () => onNewRoom(),
      }, icon('add', { size: 14 })),
    ),
    h('nav', { class: 'nav', id: 'room-list', 'aria-label': 'Комнаты' }),
    h('div', { class: 'empty hidden', id: 'rooms-empty', style: { padding: 'var(--space-4) var(--space-2)' } },
      icon('comment-discussion', { size: 18 }),
      h('div', { class: 'empty__text small', 'data-empty-note': '' }, 'Пока нет комнат'),
    ),
    h('section', { class: 'sidebar__recent hidden', id: 'sidebar-recent', 'aria-label': 'Недавние запросы' },
      h('div', { class: 'sidebar__label' }, 'Недавнее'),
      h('div', { class: 'recent', id: 'recent-list' }),
    ),
  );

  // Подвал: одна строка состояния движка, ряд иконок, подпись «локально».
  // W20-U3 (пункт 18): рядом с темой — переключатель «простой / подробный вид».
  const modeBtn = viewModeButton();
  const tools = h('div', { class: 'sidebar__tools' },
    h('button', {
      class: 'btn btn--ghost btn--icon btn--sm', type: 'button', id: 'notify-toggle',
      title: 'Включить уведомления о новых запросах и готовых ответах',
      'aria-label': 'Уведомления',
      onclick: () => onToggleNotifications(),
    }, icon('bell', { size: 15 })),
    modeBtn,
    // W26-D7: маленькая кнопка «свернуть» из нижнего ряда убрана — её место
    // занял шеврон на правом краю панели (`#rail-toggle`), его видно всегда.
    h('button', {
      class: 'btn btn--ghost btn--icon btn--sm', type: 'button', id: 'theme-toggle',
      title: 'Тема: как в системе', 'aria-label': 'Тема',
      onclick: () => onCycleTheme(),
    }, icon('color-mode', { size: 15 })),
    h('span', { class: 'spacer' }),
    h('a', {
      class: 'btn btn--ghost btn--icon btn--sm', href: '#/settings',
      title: 'Настройки', 'aria-label': 'Настройки',
    }, icon('settings-gear', { size: 15 })),
  );
  // Подпись кнопки следует за режимом, даже если его сменили клавишей `V`
  // или в другой вкладке: событие `teamroom:viewmode` приходит из view-mode.js.
  watchViewMode(modeBtn);

  host.appendChild(scroll);
  host.appendChild(h('div', { class: 'sidebar__foot' },
    h('div', { class: 'engine', id: 'engine', 'aria-live': 'polite' }),
    tools,
  ));
}
