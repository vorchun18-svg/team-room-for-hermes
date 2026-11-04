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
  }, icon(room.icon, { size: 15 }), h('span', { class: 'nav-item__text' }, room.title), badges);
  return { node, badges, room };
}

function updateNavItem(item, room) {
  item.room = room;
  item.node.href = buildHash({ roomId: room.id, tab: 'composer' });
  item.node.title = room.title;
  const text = item.node.querySelector('.nav-item__text');
  if (text && text.textContent !== room.title) text.textContent = room.title;
  const ic = item.node.querySelector('.codicon');
  if (ic && room.icon) ic.className = 'codicon codicon-' + (hasIcon(room.icon) ? room.icon : 'home');

  const c = room.counts || {};
  item.badges.replaceChildren();
  // То же число, что у вкладки «Дальше»: дела, а не запросы (D4).
  const waits = waitingFor(room);
  if (waits.n === null) {
    item.badges.appendChild(h('span', {
      class: 'count-pill count-pill--you',
      title: 'Нет данных: приложение не отвечает',
      'aria-label': 'Нет данных: приложение не отвечает',
    }, '—'));
  } else if (waits.n > 0) {
    item.badges.appendChild(h('span', {
      class: 'count-pill count-pill--you',
      title: 'Ждут вас: ' + waits.text,
      'aria-label': 'Ждут вас: ' + waits.text,
    }, String(waits.n)));
  }
  if (c.working > 0) {
    const works = countText(c.working, COUNT_FORMS.work);
    item.badges.appendChild(h('span', {
      class: 'count-pill count-pill--live',
      title: 'Идёт: ' + works,
      'aria-label': 'Идёт: ' + works,
    }, h('span', { class: 'count-pill__dot' }), String(c.working)));
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
  for (const [roomId, item] of navItems) {
    item.node.classList.toggle('nav-item--active', roomId === id);
    if (roomId === id) item.node.setAttribute('aria-current', 'page');
    else item.node.removeAttribute('aria-current');
  }
  applyDemoTitle();
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

/** Собрать последние запросы по всем комнатам (по 5 с комнаты) и перерисовать. */
export async function loadRecent() {
  if (recentLoading) return;
  const list = rooms.get('list').filter((r) => !r.archived);
  if (!list.length) { renderRecent([]); return; }

  recentLoading = true;
  try {
    const batches = await Promise.all(list.map(async (room) => {
      try {
        const data = await api.get('/api/rooms/' + enc(room.id) + '/requests?limit=' + RECENT_LIMIT);
        const items = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
        return items
          .filter((i) => i && i.id && !i.archived)
          .map((i) => ({ id: i.id, title: i.title || 'Без заголовка', status: i.status, at: i.created_at, roomId: room.id, roomTitle: room.title }));
      } catch {
        return [];
      }
    }));
    const merged = batches.flat()
      .sort((a, b) => (Number(b.at) || 0) - (Number(a.at) || 0))
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

  const sig = items.map((i) => [i.id, i.title, i.status, i.roomId].join('|')).join('#');
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
    const full = String(item.title);
    // Подсказка с полным текстом: пять обрезанных строк иначе не различить (D13).
    const hint = full + ' · ' + String(item.roomTitle);
    list.appendChild(h('a', {
      class: 'recent__item',
      href: '#/room/' + enc(item.roomId) + '/request/' + enc(item.id),
      title: hint,
    },
    h('span', { class: 'status-dot status-dot--' + tone, 'aria-hidden': 'true' }),
    h('span', { class: 'recent__body' },
      h('span', { class: 'recent__title', title: hint }, full),
      h('span', { class: 'recent__room', title: String(item.roomTitle) }, String(item.roomTitle))),
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
  const tools = h('div', { class: 'sidebar__tools' },
    h('button', {
      class: 'btn btn--ghost btn--icon btn--sm', type: 'button', id: 'notify-toggle',
      title: 'Включить уведомления о новых запросах и готовых ответах',
      'aria-label': 'Уведомления',
      onclick: () => onToggleNotifications(),
    }, icon('bell', { size: 15 })),
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

  host.appendChild(scroll);
  host.appendChild(h('div', { class: 'sidebar__foot' },
    h('div', { class: 'engine', id: 'engine', 'aria-live': 'polite' }),
    tools,
  ));
}
