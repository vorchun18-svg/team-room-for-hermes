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
import { FOLD_TITLE, railBadges, roomNavTitle, homeNavTitle } from './shell-rail.js';
import {
  ORDER_ENDPOINT, MAX_GROUP_TITLE, menuSections, planDrop, dropIndex, pinPayload,
  groupIdFromTitle, normalizeGroups, canDrag,
} from './rooms-order.js';
import { normalizeRoom } from './views/rooms.js';
// W29-Q7: «Недавнее» (10 запросов и дел) и «Закладки» (★) — один источник на
// меню. Локальный список знает, что человек открывал; сервер знает, что
// закреплено (`pinned`), поэтому звёзды сверяются с его ответом.
import {
  RECENT_MAX, readRecents, mergeRecents, syncBookmarks, bookmarkLabel, setBookmark,
} from './recents.js';

export const APP_TITLE = 'Комната команды';
/** «Недавнее» — 10 пунктов (запросы и дела), столько же отдаёт сводка за раз. */
const RECENT_LIMIT = RECENT_MAX;

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
/** Разделы меню (закреплённые / группы / без группы) — как их видит человек. */
let sidebarSections = [];
/** Действия каркаса, нужные раскладке: перезагрузить комнаты и сказать об ошибке. */
let layoutActions = { reload: null, notify: null };

function sectionKeyOf(section) {
  return section && section.kind === 'pinned' ? '__pinned__' : String((section && section.id) || '');
}

function currentSections() {
  return menuSections(rooms.get('list') || [], rooms.get('groups') || []);
}

function layoutSignature(sections) {
  return sections.map((s) => [sectionKeyOf(s), s.title, s.collapsed ? '1' : '0',
    (s.rooms || []).map((r) => r.id + (r.pinned ? '+' : '')).join(',')].join('|')).join('#');
}

/* ─────────────────────────────────────────────────────────────── сайдбар */

/**
 * Строка комнаты: ручка перетаскивания (⋮⋮), значок, название, счётчики, 📌.
 *
 * Ручка и «закрепить» — `span role=button` внутри ссылки: строка остаётся одной
 * ссылкой (клик по названию открывает комнату), а действия не уводят со страницы.
 */
function createNavItem(room) {
  const badges = h('span', { class: 'nav-item__badges' });
  const handle = h('span', {
    class: 'room-drag', 'aria-hidden': 'true',
    title: 'Перетащить комнату · Alt+↑/↓ — сдвинуть с клавиатуры',
  }, '⋮⋮');
  const pin = h('span', {
    class: 'room-pin', role: 'button', tabindex: '0',
    title: 'Закрепить наверху', 'aria-label': 'Закрепить комнату ' + room.title,
    'aria-pressed': 'false',
  }, icon('pin', { size: 12 }));
  const node = h('a', {
    class: 'nav-item',
    href: buildHash({ roomId: room.id, tab: 'composer' }),
    dataset: { room: room.id },
    // W21-R1: в полосе значков подписи скрыты — имя комнаты живёт в подсказке
    // и в aria-label (иначе значок комнаты не читается ни мышью, ни скринридером).
    title: room.title, 'aria-label': room.title,
  }, handle, icon(room.icon, { size: 15 }),
  // Подсказка и на самой подписи: длинное название обрезается многоточием, и
  // проверка вёрстки (ui_audit) требует title у обрезанного элемента, а не только у строки.
  h('span', { class: 'nav-item__text', title: room.title }, room.title), badges, pin);

  handle.addEventListener('pointerdown', (ev) => beginDrag(ev, room.id));
  handle.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); });
  pin.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const current = (rooms.get('list') || []).find((r) => String(r.id) === String(room.id));
    saveLayout(pinPayload(room.id, !(current && current.pinned)));
  });
  pin.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    ev.preventDefault();
    ev.stopPropagation();
    const current = (rooms.get('list') || []).find((r) => String(r.id) === String(room.id));
    saveLayout(pinPayload(room.id, !(current && current.pinned)));
  });
  // W29-P7: Alt+↑/↓ — тот же порядок с клавиатуры (мышь не обязательна).
  node.addEventListener('keydown', (ev) => {
    if (!ev.altKey || (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown')) return;
    ev.preventDefault();
    moveWithKeyboard(room.id, ev.key === 'ArrowUp' ? -1 : 1);
  });
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

  // W29-P7: 📌 показывает состояние «закреплена/нет» и объясняет, что будет.
  const pin = item.node.querySelector('.room-pin');
  if (pin) {
    const on = room.pinned === true;
    pin.classList.toggle('room-pin--on', on);
    pin.setAttribute('aria-pressed', on ? 'true' : 'false');
    const label = on ? 'Открепить «' + room.title + '»' : 'Закрепить «' + room.title + '» наверху';
    if (pin.getAttribute('aria-label') !== label) pin.setAttribute('aria-label', label);
    if (pin.title !== label) pin.title = label;
  }

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

/** Заголовок группы: свернуть, переименовать, убрать (комнаты остаются). */
function createGroupHead(section) {
  const key = sectionKeyOf(section);
  const carets = section.kind === 'group'
    ? [h('button', {
      class: 'room-group__fold', type: 'button',
      title: section.collapsed ? 'Развернуть группу' : 'Свернуть группу',
      'aria-label': (section.collapsed ? 'Развернуть группу ' : 'Свернуть группу ') + section.title,
      'aria-expanded': section.collapsed ? 'false' : 'true',
      onclick: (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const groups = normalizeGroups(rooms.get('groups') || []).map((g) => (g.id === section.id
          ? Object.assign({}, g, { collapsed: !section.collapsed }) : g));
        saveLayout({ groups });
      },
    }, icon(section.collapsed ? 'chevron-right' : 'chevron-down', { size: 12 }))] : [];
  const tools = section.kind === 'group'
    ? [h('button', {
      class: 'room-group__tool', type: 'button', title: 'Переименовать группу',
      'aria-label': 'Переименовать группу ' + section.title,
      onclick: (ev) => { ev.preventDefault(); ev.stopPropagation(); editGroup(section, 'rename'); },
    }, icon('edit', { size: 11 })),
    h('button', {
      class: 'room-group__tool', type: 'button', title: 'Убрать группу (комнаты останутся)',
      'aria-label': 'Убрать группу ' + section.title,
      onclick: (ev) => { ev.preventDefault(); ev.stopPropagation(); removeGroup(section); },
    }, icon('trash', { size: 11 }))] : [];
  const head = h('div', {
    class: 'room-group__head' + (section.kind === 'pinned' ? ' room-group__head--pinned' : ''),
    dataset: { roomSection: key },
  },
  h('div', { class: 'room-group__title' },
    ...carets,
    icon(section.kind === 'pinned' ? 'pin' : 'folder-opened', { size: 11 }),
    h('span', { class: 'room-group__name', title: section.title }, section.title),
    h('span', { class: 'room-group__count' }, String((section.rooms || []).length))),
  h('div', { class: 'room-group__tools' }, ...tools));
  return head;
}

/** Строка «новая группа»: название → id, Enter — создать, Esc — отмена. */
function groupEditor(initial, { onSave, onCancel }) {
  const input = h('input', {
    class: 'room-group__input', type: 'text', value: initial || '',
    maxlength: String(MAX_GROUP_TITLE), placeholder: 'Название группы',
    'aria-label': 'Название группы',
  });
  const commit = () => {
    const title = String(input.value || '').trim().split(/\s+/).join(' ');
    if (!title) { onCancel(); return; }
    onSave(title);
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); commit(); }
    if (ev.key === 'Escape') { ev.preventDefault(); onCancel(); }
  });
  const row = h('div', { class: 'room-group__edit' }, input,
    h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: commit }, 'Сохранить'),
    h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: onCancel }, 'Отмена'));
  queueMicrotask(() => { try { input.focus(); input.select(); } catch { /* нет фокуса — не беда */ } });
  return row;
}

function editGroup(section, mode) {
  const nav = $('#room-list');
  if (!nav) return;
  const holder = h('div', { class: 'room-group__edit-holder' });
  const close = () => holder.remove();
  holder.appendChild(groupEditor(mode === 'rename' ? section.title : '', {
    onSave: (title) => {
      const groups = normalizeGroups(rooms.get('groups') || []).map((g) => (g.id === section.id
        ? Object.assign({}, g, { title }) : g));
      close();
      saveLayout({ groups });
    },
    onCancel: close,
  }));
  nav.appendChild(holder);
}

function removeGroup(section) {
  const groups = normalizeGroups(rooms.get('groups') || []).filter((g) => g.id !== section.id);
  saveLayout({ groups });
}

function createGroup() {
  const nav = $('#room-list');
  if (!nav) return;
  const holder = h('div', { class: 'room-group__edit-holder' });
  const close = () => holder.remove();
  holder.appendChild(groupEditor('', {
    onSave: (title) => {
      const groups = normalizeGroups(rooms.get('groups') || []);
      const id = groupIdFromTitle(title, groups.map((g) => g.id));
      close();
      saveLayout({ groups: groups.concat([{ id, title, collapsed: false }]) });
    },
    onCancel: close,
  }));
  nav.appendChild(holder);
  const input = holder.querySelector('input');
  if (input) { try { input.focus(); } catch { /* необязательно */ } }
}

/* ── перетаскивание (Pointer Events, реальная мышь) ───────────────────────── */

let drag = null;

function dragEnabled() {
  const nav = typeof document !== 'undefined' && document.getElementById
    ? document.getElementById('app') : null;
  const rail = !!(nav && nav.classList && nav.classList.contains('is-rail'));
  const width = document.body ? document.documentElement.clientWidth : 0;
  return canDrag({ rail, width });
}

function beginDrag(ev, roomId) {
  if (ev.button !== undefined && ev.button !== 0) return;
  if (!dragEnabled()) return;
  const nav = $('#room-list');
  if (!nav) return;
  const item = navItems.get(roomId);
  if (!item) return;
  ev.preventDefault();
  ev.stopPropagation();
  drag = { id: String(roomId), active: false, x: ev.clientX, y: ev.clientY,
    pointerId: ev.pointerId, sectionKey: null, index: 0, marker: null };
  item.node.classList.add('nav-item--armed');
  window.addEventListener('pointermove', onDragMove);
  window.addEventListener('pointerup', onDragEnd);
  window.addEventListener('pointercancel', onDragCancel);
}

function dropMarker() {
  if (!drag) return null;
  if (!drag.marker) {
    drag.marker = h('div', { class: 'room-drop', 'aria-hidden': 'true' });
  }
  return drag.marker;
}

function onDragMove(ev) {
  if (!drag) return;
  const dx = ev.clientX - drag.x;
  const dy = ev.clientY - drag.y;
  if (!drag.active && Math.abs(dx) + Math.abs(dy) < 5) return;
  const item = navItems.get(drag.id);
  if (!item) return;
  if (!drag.active) {
    drag.active = true;
    item.node.classList.add('nav-item--dragging');
    item.node.classList.remove('nav-item--armed');
    document.body.classList.add('room-dragging');
  }
  ev.preventDefault();
  const el = document.elementFromPoint(ev.clientX, ev.clientY);
  const sectionEl = el && el.closest ? el.closest('[data-room-section]') : null;
  if (!sectionEl) return;
  const key = sectionEl.getAttribute('data-room-section');
  const body = sectionEl.querySelector('.room-group__body');
  if (!body) return;
  const rows = Array.from(body.querySelectorAll('.nav-item[data-room]'))
    .filter((node) => node.dataset.room !== drag.id);
  const rects = rows.map((node) => {
    const box = node.getBoundingClientRect();
    return { top: box.top, height: box.height };
  });
  const index = dropIndex(rects, ev.clientY);
  drag.sectionKey = key;
  drag.index = index;
  const marker = dropMarker();
  if (!marker) return;
  if (index >= rows.length) body.appendChild(marker);
  else body.insertBefore(marker, rows[index]);
}

function onDragEnd() {
  if (!drag) return;
  const { id, active, sectionKey, index } = drag;
  onDragCancel();
  if (!active) return;
  const plan = planDrop(currentSections(), id, sectionKey, index);
  if (!plan) return;
  const body = { order: plan.ids };
  if (Object.keys(plan.patch).length) body.rooms = { [id]: plan.patch };
  saveLayout(body);
}

function onDragCancel() {
  window.removeEventListener('pointermove', onDragMove);
  window.removeEventListener('pointerup', onDragEnd);
  window.removeEventListener('pointercancel', onDragCancel);
  if (typeof document !== 'undefined' && document.body) document.body.classList.remove('room-dragging');
  if (drag) {
    const item = navItems.get(drag.id);
    if (item) item.node.classList.remove('nav-item--dragging', 'nav-item--armed');
    if (drag.marker) drag.marker.remove();
  }
  drag = null;
}

/** Alt+↑/↓: сдвиг внутри своего раздела, без мыши. */
function moveWithKeyboard(roomId, delta) {
  const sections = currentSections();
  const section = sections.find((s) => (s.rooms || []).some((r) => String(r.id) === String(roomId)));
  if (!section) return;
  const index = (section.rooms || []).findIndex((r) => String(r.id) === String(roomId));
  const target = index + delta;
  if (target < 0 || target >= section.rooms.length) return;
  const plan = planDrop(sections, roomId, sectionKeyOf(section), target);
  if (!plan) return;
  const body = { order: plan.ids };
  if (Object.keys(plan.patch).length) body.rooms = { [roomId]: plan.patch };
  saveLayout(body);
}

/**
 * Сохранить раскладку: сразу показываем результат, затем пишем на сервер.
 *
 * Порядок «сначала экран, потом сервер» нужен, чтобы строка не «прыгала назад»
 * между отпусканием мыши и ответом. Ошибка — честное сообщение и возврат к
 * порядку сервера (без тихого вранья «сохранено»).
 */
export function saveLayout(body) {
  const payload = body && typeof body === 'object' ? body : {};
  if (Array.isArray(payload.order)) applyLayoutLocal(payload.order, payload.rooms || {});
  api.put(ORDER_ENDPOINT, payload).then((res) => {
    if (res && Array.isArray(res.rooms)) {
      const groups = Array.isArray(res.groups) ? res.groups
        : (payload.groups || rooms.get('groups') || []);
      rooms.set({ list: res.rooms.map(normalizeRoom).filter(Boolean), groups });
      renderRoomList();
    }
    if (typeof layoutActions.reload === 'function') layoutActions.reload();
  }).catch((err) => {
    const message = (err && err.message) ? err.message : 'Порядок комнат не сохранён';
    if (typeof layoutActions.notify === 'function') layoutActions.notify(message);
    else console.warn('[rooms-order]', message);
    if (typeof layoutActions.reload === 'function') layoutActions.reload();
  });
}

function applyLayoutLocal(ids, patch) {
  const list = rooms.get('list') || [];
  const byId = new Map(list.map((room) => [String(room.id), room]));
  const next = [];
  for (const id of ids) {
    const room = byId.get(String(id));
    if (room) next.push(room);
  }
  for (const room of list) if (!ids.map(String).includes(String(room.id))) next.push(room);
  const updated = next.map((room) => {
    const change = patch[String(room.id)];
    return change ? Object.assign({}, room, change) : room;
  });
  rooms.set({ list: updated });
  renderRoomList();
}

export function renderRoomList() {
  const nav = $('#room-list');
  if (!nav) return;
  const list = rooms.get('list');
  const sections = currentSections();
  const sig = layoutSignature(sections);

  if (sig !== renderedSig) buildRoomList(nav, sections);
  for (const section of sections) {
    for (const room of section.rooms) {
      const item = navItems.get(room.id);
      if (item) updateNavItem(item, room);
    }
  }

  const empty = $('#rooms-empty');
  if (empty) {
    empty.classList.toggle('hidden', list.length > 0);
    const note = empty.querySelector('[data-empty-note]');
    if (note && rooms.get('error')) note.textContent = rooms.get('error');
  }
  markActiveRoom();
}

let renderedSig = '';

/** Полная перестройка списка: разделы, группы, строки комнат. */
function buildRoomList(nav, sections) {
  nav.replaceChildren();
  navItems.clear();
  sidebarSections = sections;
  renderedSig = layoutSignature(sections);
  for (const section of sections) {
    const key = sectionKeyOf(section);
    const body = h('div', { class: 'room-group__body' });
    const item = h('section', {
      class: 'room-group' + (section.kind === 'group' ? ' room-group--own' : '')
        + (section.collapsed ? ' room-group--collapsed' : ''),
      dataset: { roomSection: key },
      'aria-label': section.title || 'Комнаты',
    }, createGroupHead(section), body);
    for (const room of section.rooms) {
      const navItem = createNavItem(room);
      navItems.set(room.id, navItem);
      body.appendChild(navItem.node);
      updateNavItem(navItem, room);
    }
    if (!section.rooms.length && section.kind === 'group') {
      body.appendChild(h('div', { class: 'room-group__empty' }, 'Перетащите сюда комнату'));
    }
    nav.appendChild(item);
  }
}

export function markActiveRoom() {
  const id = parseHash().roomId;
  const route = parseHash();
  for (const [roomId, item] of navItems) {
    item.node.classList.toggle('nav-item--active', roomId === id);
    if (roomId === id) item.node.setAttribute('aria-current', 'page');
    else item.node.removeAttribute('aria-current');
  }
  // W27-E4: «Все комнаты» (`#/`) подсвечиваются, когда открыт список комнат.
  const homeLink = typeof document !== 'undefined' && document.getElementById
    ? document.getElementById('home-link') : null;
  if (homeLink) {
    const on = !!route && route.name === 'home';
    homeLink.classList.toggle('nav-item--active', on);
    if (on) homeLink.setAttribute('aria-current', 'page');
    else homeLink.removeAttribute('aria-current');
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
  // W26-QA2: число — это ПУНКТЫ «ждёт вашего решения», а не дела: у одного дела
  // их бывает три. Подпись говорит, что именно посчитано (правило «счётчики не врут»).
  const label = 'Дела: пунктов «ждёт вашего решения» — ' + value;
  host.replaceChildren(h('span', {
    class: 'count-pill count-pill--you', title: label, 'aria-label': label,
  }, String(value)));
  if (link) link.setAttribute('aria-label', 'Дела — пунктов «ждёт вашего решения»: ' + value);
}

/* ─────────────────────────────────────── «Недавнее»: последние запросы */

let recentSig = '';
let recentLoading = false;
/** Последний серверный список «Недавнего» — чтобы 304 не стирал локальные записи. */
let lastServerRecent = [];

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

/**
 * Последние запросы по всем комнатам (одним запросом сводки, иначе порциями).
 *
 * W29-Q7: к серверному списку добавляются местные «где я был» (запросы и дела),
 * поэтому «Недавнее» — это 10 настоящих последних мест, а не только те
 * запросы, что успели попасть в сводку.
 */
export async function loadRecent() {
  if (recentLoading) return;
  const list = rooms.get('list').filter((r) => !r.archived);
  if (!list.length) { renderRecent([]); return; }

  recentLoading = true;
  try {
    const viaSummary = await recentFromSummary(list);
    // 304: данные те же, но локальные записи могли измениться — перерисовываем.
    if (viaSummary === 'unchanged') { renderRecent(lastServerRecent); return; }
    if (Array.isArray(viaSummary)) { lastServerRecent = viaSummary; renderRecent(viaSummary); return; }
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
    lastServerRecent = merged;
    renderRecent(merged);
  } finally {
    recentLoading = false;
  }
}

function renderRecent(items) {
  const host = $('#sidebar-recent');
  const list = $('#recent-list');
  if (!host || !list) return;

  // «Недавнее» = что открывал человек (локально) + что пришло с сервера.
  // Свежее — выше; одна и та же запись не дублируется.
  const rows = mergeRecents(readRecents(), Array.isArray(items) ? items : [], RECENT_LIMIT);
  renderBookmarks(rows);

  const sig = rows.map((i) => [i.kind, i.id, i.title_short, titleShortOf(i), i.status, i.roomId,
    i.pinned ? 'pin' : '',
    recentReactionMark(i) ? '!' : ''].join('|')).join('#');
  if (sig === recentSig) return;
  recentSig = sig;

  list.replaceChildren();
  if (!rows.length) {
    host.classList.add('hidden');
    return;
  }
  host.classList.remove('hidden');
  for (const item of rows) {
    const tone = TONE[String(item.status || '')] || 'muted';
    // B8/P5: короткое название на экране, полный текст диктовки — в подсказке.
    const isDelo = item.kind === 'delo';
    const short = titleShortOf(item) || (isDelo ? 'Дело' : 'Без названия');
    // Название комнаты — из записи или из списка комнат: у локальных «где я был»
    // его нет, и «undefined» в меню выглядел бы поломкой.
    const roomTitle = String(item.roomTitle
      || ((rooms.get('list').find((r) => r && r.id === item.roomId) || {}).title || ''));
    const hint = titleHint(item, roomTitle ? 'Комната: ' + roomTitle : '');
    const fullHint = hint || short;
    // W20-U4 (P9): руководитель ответил на ваш ответ в этом запросе — отметка
    // справа от названия, пока запрос не открыли.
    const mark = isDelo ? null : recentReactionMark(item);
    if (isDelo) {
      // W29-Q7: дело — такое же «недавнее место», как запрос: ведёт в карточку дела.
      list.appendChild(h('a', {
        class: 'recent__item recent__item--delo',
        href: '#/delo/' + enc(item.id),
        title: 'Дело · ' + fullHint,
        'aria-label': short + ' — дело',
      },
      h('span', { class: 'status-dot status-dot--info', 'aria-hidden': 'true' }),
      h('span', { class: 'recent__body' },
        h('span', { class: 'recent__title', title: fullHint }, short),
        h('span', { class: 'recent__room' }, 'Дело')),
      ));
      continue;
    }
    list.appendChild(h('a', {
      class: 'recent__item' + (mark ? ' recent__item--reacted' : '') + (item.pinned ? ' recent__item--pinned' : ''),
      href: '#/room/' + enc(item.roomId) + '/request/' + enc(item.id),
      title: (item.pinned ? 'Закреплён · ' : '') + fullHint,
      'aria-label': short + ' — ' + (roomTitle || 'комната запроса')
        + (item.pinned ? ', закреплён' : '')
        + (mark ? ', есть новая реакция руководителя' : ''),
    },
    h('span', { class: 'status-dot status-dot--' + tone, 'aria-hidden': 'true' }),
    h('span', { class: 'recent__body' },
      h('span', { class: 'recent__title', title: fullHint },
        item.pinned ? icon('pin', { size: 11, className: 'recent__pin' }) : null,
        short),
      h('span', { class: 'recent__room', title: roomTitle || null }, roomTitle || 'Запрос')),
    mark,
    ));
  }
}

/* ───────────────────────────────────────────────── «Закладки» (★) */

/**
 * Нарисовать «Закладки» в меню.
 *
 * Звезда — это штатное закрепление запроса (PATCH `pinned`), поэтому список
 * сверяется с ответом сервера: сняли звезду в другом окне — здесь она тоже
 * гаснет. Пусто — секции нет вовсе: пустая полка в меню только мешает.
 *
 * @param {object[]} [serverRows] последние запросы комнат (там есть `pinned`)
 */
export function renderBookmarks(serverRows) {
  const host = $('#sidebar-bmarks');
  const list = $('#bmarks-list');
  if (!host || !list) return;
  // В свёрку идут только записи с ЯВНЫМ флагом: локальные «где я был» про
  // закрепление ничего не знают и не должны снимать звезду.
  const rows = (Array.isArray(serverRows) ? serverRows : [])
    .filter((r) => r && r.id && typeof r.pinned === 'boolean')
    .map((r) => ({ id: r.id, roomId: r.roomId, title: r.title_short || r.title, pinned: r.pinned }));
  const items = syncBookmarks(rows);
  list.replaceChildren();
  if (!items.length) {
    host.classList.add('hidden');
    return;
  }
  host.classList.remove('hidden');
  const label = $('#bmarks-count');
  if (label) label.textContent = String(items.length);
  for (const item of items) {
    const short = bookmarkLabel(item);
    const href = item.roomId ? '#/room/' + enc(item.roomId) + '/request/' + enc(item.id) : '#/';
    const off = h('button', {
      type: 'button', class: 'bmark__off',
      title: 'Снять закладку', 'aria-label': 'Снять закладку «' + short + '»',
    }, icon('close', { size: 11 }));
    off.addEventListener('click', (ev) => { if (ev && ev.stopPropagation) ev.stopPropagation(); unpinBookmark(item); });
    list.appendChild(h('div', { class: 'recent__item recent__item--bmark' },
      h('a', {
        class: 'bmark__link', href,
        title: 'Закладка · ' + short + (item.roomId ? ' · комната ' + item.roomId : ''),
        'aria-label': short + ' — закладка',
      },
      h('span', { class: 'bmark__star', 'aria-hidden': 'true' }, icon('star', { size: 12 })),
      h('span', { class: 'recent__body' },
        h('span', { class: 'recent__title', title: short }, short),
        h('span', { class: 'recent__room' }, 'Закладка'))),
      off,
    ));
  }
}

/**
 * Снять звезду прямо в меню: тот же `PATCH pinned:false`, что и на странице
 * запроса, — без похода на страницу. Ошибка называется словами.
 */
async function unpinBookmark(item) {
  try {
    await api.patch('/api/requests/' + enc(item.id), { pinned: false });
    setBookmark({ id: item.id, roomId: item.roomId, title: item.title }, false);
    renderBookmarks();
  } catch (err) {
    /* звезда остаётся — молча врать нельзя, поэтому говорим причину */
    if (typeof window !== 'undefined' && window.console) {
      window.console.warn('[sidebar] закладка не снята:', (err && err.message) || err);
    }
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
export function buildSidebar(host, { onNewRoom, onToggleNotifications, onCycleTheme, onCloseNav,
  onLayoutSaved, onNotice } = {}) {
  host.replaceChildren();
  watchDemoTitle();
  // W29-P7: раскладка меню пишет на сервер и просит каркас перечитать комнаты;
  // ошибка — человеческим текстом, а не молчанием.
  layoutActions = {
    reload: typeof onLayoutSaved === 'function' ? onLayoutSaved : null,
    notify: typeof onNotice === 'function' ? onNotice : null,
  };
  // W26-D7: шеврон на правом краю панели — 24 px, у верха, виден всегда
  // (и в развёрнутом меню, и в полосе значков). Id сохранён (`#rail-toggle`),
  // потому что каркас ищет кнопку по нему; подсказка — с клавишей.
  host.appendChild(h('button', {
    class: 'rail-fold', type: 'button', id: 'rail-toggle', 'data-rail-toggle': '',
    title: FOLD_TITLE, 'aria-label': 'Свернуть меню',
    'aria-pressed': 'false', 'aria-controls': 'room-list',
  }, icon('chevron-left', { size: 14 })));
  host.appendChild(h('div', { class: 'brand' },
    // W27-E4: заголовок «Комната команды» — ссылка на `#/` (список комнат).
    // Дефект 3: «пробовал надпись сверху — она не открывает стартовую страницу».
    h('a', {
      class: 'brand__home', href: '#/', id: 'home-link-brand',
      title: 'Все комнаты — список комнат (G, H)',
      'aria-label': APP_TITLE + ' — все комнаты',
    },
    h('span', { class: 'brand__mark', 'aria-hidden': 'true' }, 'К'),
    h('span', { class: 'brand__text' },
      h('span', { class: 'brand__name' }, APP_TITLE),
      h('span', { class: 'brand__sub' }, 'только на этом компьютере'),
    )),
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
    // W27-E4: «Все комнаты» — постоянный первый пункт главного меню (значок
    // дома). Дефект 3: на страницу `#/` не вела НИ ОДНА кнопка — теперь ведёт
    // пункт меню, заголовок «Комната команды» и клавиши G, H. Один и тот же
    // пункт работает и в развёрнутом меню, и в полосе значков (подпись прячет CSS).
    h('nav', { class: 'nav nav--top', id: 'home-nav', 'aria-label': 'Все комнаты' },
      h('a', {
        class: 'nav-item nav-item--home', href: '#/', id: 'home-link',
        title: homeNavTitle(), 'aria-label': 'Все комнаты',
      }, icon('home', { size: 15 }),
      h('span', { class: 'nav-item__text' }, 'Все комнаты'))),
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
        class: 'btn btn--ghost btn--icon btn--sm', type: 'button', id: 'group-add',
        'aria-label': 'Новая группа комнат', title: 'Новая группа: соберите комнаты в папку',
        onclick: () => createGroup(),
      }, icon('new-folder', { size: 14 })),
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
    // W29-Q7: «Закладки» (★) — над «Недавним»: то, что человек держит под рукой,
    // стоит выше того, где он просто недавно был. Пусто — секции нет.
    h('section', { class: 'sidebar__recent sidebar__bmarks hidden', id: 'sidebar-bmarks', 'aria-label': 'Закладки' },
      h('div', { class: 'sidebar__label' }, 'Закладки',
        h('span', { class: 'sidebar__label-count', id: 'bmarks-count' })),
      h('div', { class: 'recent', id: 'bmarks-list' }),
    ),
    h('section', { class: 'sidebar__recent hidden', id: 'sidebar-recent', 'aria-label': 'Недавние запросы и дела' },
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
