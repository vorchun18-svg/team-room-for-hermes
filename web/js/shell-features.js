/**
 * shell-features.js — действия каркаса, которым нужен контекст приложения:
 * «+ Комната», палитра Ctrl+K, уведомления о сводках комнат и горячие клавиши.
 *
 * Зависимости инжектируются один раз из app.js (initShellFeatures), чтобы модуль
 * не тянул app.js обратно и не плодил циклы. Здесь же — единственный контекст
 * вьюхи для диалогов: тот же набор полей, что даёт router.
 */

import { h } from './util.js';
import { buildHash, parseHash } from './router.js';
import { softRoomName } from './shell-labels.js';

const NOTIFY_KEY = 'teamroom:notify';
const G_WINDOW_MS = 1400;

let D = null;

/**
 * @param {{toast:Function, api:object, stores:object, navigate:Function,
 *          getRoute:Function, render:Function}} deps
 */
export function initShellFeatures(deps) {
  D = deps;
}

function route() {
  return D && D.getRoute ? D.getRoute() : parseHash();
}

/**
 * Закрыть выдвижное меню комнат (мобильный режим). Диалоги и палитра всегда
 * открываются поверх страницы и не должны делить экран с панелью.
 */
export function closeNav() {
  if (typeof document === 'undefined') return;
  const app = document.getElementById('app');
  if (app && app.classList.contains('nav-open')) app.classList.remove('nav-open');
}

/** Контекст вьюхи для диалогов и палитры (совпадает с ctx из router.js). */
export function shellCtx(extra = {}) {
  const r = route();
  return Object.assign({
    route: r,
    params: Object.assign({}, r.params || {}, r.query || {}),
    api: D.api,
    store: D.stores,
    navigate: D.navigate,
    toast: D.toast,
    room: D.stores && D.stores.currentRoom ? D.stores.currentRoom.get('room') : null,
    refresh: () => { if (D.render) D.render(); },
  }, extra);
}

/* ─────────────────────────────────────────────────────── «+ Комната» */

let roomDialogBusy = false;

/**
 * Открыть диалог новой комнаты. Файл пишет отдельный исполнитель, поэтому его
 * может не быть: тогда честный тост «раздел в разработке», каркас не падает.
 * Перед открытием закрываем выдвижное меню: диалог всегда поверх страницы.
 */
export async function openRoomDialog() {
  if (roomDialogBusy) return;
  roomDialogBusy = true;
  closeNav();
  try {
    const mod = await import('./views/room-dialog.js');
    if (!mod || typeof mod.openRoomDialog !== 'function') {
      throw new Error('views/room-dialog.js не экспортирует openRoomDialog(ctx)');
    }
    await mod.openRoomDialog(shellCtx());
  } catch (err) {
    console.warn('[room-dialog] не открылась', err);
    D.toast('Раздел в разработке: создание комнаты появится позже', 'info');
  } finally {
    roomDialogBusy = false;
  }
}

/* ──────────────────────────────────────────────────────── палитра Ctrl+K */

let paletteBusy = false;

/** Ctrl+K: палитра, если views/palette.js уже есть; иначе молчим. */
export async function openPalette() {
  if (paletteBusy) return;
  paletteBusy = true;
  closeNav();
  try {
    let mod;
    try {
      mod = await import('./views/palette.js');
    } catch {
      return; // файла ещё нет — тихо игнорируем (см. DESIGN-NOTES)
    }
    const ctx = shellCtx();
    if (typeof mod.open === 'function') {
      await mod.open(ctx);
      return;
    }
    if (typeof mod.mount === 'function') {
      const host = h('div', { class: 'dialog-backdrop' }, h('div', { class: 'dialog dialog--wide' }));
      document.body.appendChild(host);
      const inst = await mod.mount(host.querySelector('.dialog'), ctx);
      const close = () => { if (inst && inst.unmount) inst.unmount(); host.remove(); };
      host.addEventListener('click', (e) => { if (e.target === host) close(); });
      document.addEventListener('keydown', function esc(e) {
        if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
      });
    }
  } catch (err) {
    console.warn('[palette] не открылась', err);
  } finally {
    paletteBusy = false;
  }
}

/* ──────────────────────────────────────────────────────── уведомления */

/** Включены ли уведомления (флаг в localStorage + разрешение браузера). */
function notifyEnabled() {
  try { return localStorage.getItem(NOTIFY_KEY) === 'true'; } catch { return false; }
}

/** Подпись у кнопки «Уведомления» и её состояние для скринридера. */
export function applyNotifyLabel() {
  const label = document.querySelector('[data-notify-label]');
  const btn = document.querySelector('#notify-toggle');
  const supported = typeof Notification !== 'undefined';
  const granted = supported && Notification.permission === 'granted';
  const on = notifyEnabled() && granted;
  if (label) label.textContent = !supported ? 'нет' : on ? 'вкл' : 'выкл';
  if (btn) {
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.title = !supported ? 'Браузер не поддерживает уведомления'
      : on ? 'Уведомления включены (клик — выключить)'
        : 'Включить уведомления о новых запросах и готовых ответах';
  }
}

/** Переключатель уведомлений; разрешение спрашиваем только по клику. */
export async function toggleNotifications() {
  if (typeof Notification === 'undefined') {
    D.toast('Браузер не поддерживает уведомления', 'warn');
    return;
  }
  if (notifyEnabled()) {
    try { localStorage.setItem(NOTIFY_KEY, 'false'); } catch { /* приватный режим */ }
    D.toast('Уведомления выключены', 'info');
    applyNotifyLabel();
    return;
  }
  let perm = Notification.permission;
  if (perm === 'default') {
    try { perm = await Notification.requestPermission(); } catch { perm = 'denied'; }
  }
  if (perm !== 'granted') {
    D.toast('Браузер не разрешил уведомления — останутся тосты', 'warn');
    applyNotifyLabel();
    return;
  }
  try { localStorage.setItem(NOTIFY_KEY, 'true'); } catch { /* ignore */ }
  D.toast('Уведомления включены', 'ok');
  applyNotifyLabel();
}

/** Системное уведомление; молча ничего не делает без разрешения. */
function notifyNow(title, body) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  try {
    new Notification(title, { body, tag: 'teamroom', lang: 'ru' });
  } catch (err) {
    console.warn('[notify] не удалось показать', err);
  }
}

let seenCounts = null;

function snapshotCounts(list) {
  const map = new Map();
  for (const r of list) map.set(r.id, { needs_you: r.counts.needs_you, done: r.counts.done });
  return map;
}

/**
 * Сравнить сводки комнат с предыдущим опросом: новые «ждут решения» и рост
 * «готово сегодня» → системное уведомление + тост. Первый опрос — только
 * запоминаем (иначе при старте прилетело бы уведомление про всё сразу).
 * @param {Array<object>} list нормализованные комнаты из /api/rooms
 */
export function notifyChanges(list) {
  const prev = seenCounts;
  seenCounts = snapshotCounts(list);
  if (!prev) return;
  if (!notifyEnabled() || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  for (const room of list) {
    const before = prev.get(room.id);
    if (!before) continue;
    const freshNeeds = room.counts.needs_you - before.needs_you;
    const freshDone = room.counts.done - before.done;
    const name = softRoomName(room.title);
    if (freshNeeds > 0) {
      notifyNow('«' + name + '»: ждут вашего решения',
        'Новых: ' + freshNeeds + ' · всего ждут: ' + room.counts.needs_you);
      D.toast('«' + name + '»: ждут вашего решения (' + room.counts.needs_you + ')', 'warn');
    }
    if (freshDone > 0) {
      notifyNow('«' + name + '»: готов ответ', 'Завершено сегодня: ' + room.counts.done);
      D.toast('«' + name + '»: готов ответ', 'ok');
    }
  }
}

/* ────────────────────────────────────────────────────── горячие клавиши */

/** g → вкладка комнаты: r Запрос, t Команда, m Карта, d Дальше, o Отчёты, j Журнал. */
const TAB_KEYS = { r: 'composer', t: 'team', m: 'roadmap', d: 'backlog', o: 'reports', j: 'journal' };
let gPressedAt = 0;

/** Фокус в поле ввода? Тогда горячие клавиши молчат. */
function isEditable(node) {
  if (!node || node.nodeType !== 1) return false;
  const tag = String(node.tagName || '').toLowerCase();
  if (tag === 'textarea' || tag === 'select') return true;
  if (tag === 'input') {
    const type = String(node.type || 'text').toLowerCase();
    return !['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'file', 'color', 'image'].includes(type);
  }
  return node.isContentEditable === true;
}

/**
 * '/' — фокус в поиск (D15). Если поле поиска уже на экране — просто ставим
 * курсор; иначе ведём на экран поиска и фокусируем поле после отрисовки.
 * @returns {boolean} обработали ли клавишу
 */
export function focusSearch() {
  const here = document.querySelector('#view .srch-input, #view input[type=\"search\"], .topbar input[type=\"search\"]');
  if (here && typeof here.focus === 'function') {
    here.focus();
    return true;
  }
  if (D && typeof D.navigate === 'function') D.navigate('#/search');
  else location.hash = '#/search';
  setTimeout(() => {
    const later = document.querySelector('#view .srch-input, #view input[type=\"search\"]');
    if (later && typeof later.focus === 'function') later.focus();
  }, 260);
  return true;
}

/** n — фокус в поле запроса (если фокуса в поле нет). */
export function focusComposer() {
  const field = document.querySelector(
    '#view textarea, #view [contenteditable="true"], #view .composer textarea, #view input[type="text"]',
  );
  if (field && typeof field.focus === 'function') {
    field.focus();
    return true;
  }
  const r = route();
  if (!r.roomId) return false;
  if (r.tab !== 'composer') D.navigate(buildHash({ roomId: r.roomId, tab: 'composer' }));
  setTimeout(() => {
    const later = document.querySelector('#view textarea, #view [contenteditable="true"]');
    if (later && typeof later.focus === 'function') later.focus();
  }, 240);
  return true;
}

/**
 * Повесить обработчик клавиш: Ctrl+K, Esc (закрыть мобильный сайдбар),
 * g+<буква> (вкладки комнаты), n (фокус в поле запроса).
 * @param {{closeNav?: Function}} [opts]
 */
export function installHotkeys(opts = {}) {
  const closeNav = opts.closeNav || (() => {});
  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && (e.key === 'k' || e.key === 'K' || e.key === 'л' || e.key === 'Л')) {
      e.preventDefault();
      openPalette();
      return;
    }
    if (e.key === 'Escape') {
      closeNav();
      gPressedAt = 0;
      return;
    }
    if (mod || e.altKey) return;
    const active = document.activeElement;
    if (isEditable(active)) return;

    const key = String(e.key || '').toLowerCase();
    if (key === 'g' && !e.shiftKey) {
      gPressedAt = Date.now();
      return;
    }
    if (gPressedAt && Date.now() - gPressedAt < G_WINDOW_MS) {
      const tab = TAB_KEYS[key];
      gPressedAt = 0;
      if (!tab) return;
      const r = route();
      if (!r.roomId) {
        D.toast('Сначала откройте комнату', 'info');
        return;
      }
      e.preventDefault();
      D.navigate(buildHash({ roomId: r.roomId, tab }));
      return;
    }
    if (key === '/' && !e.shiftKey) {
      if (focusSearch()) e.preventDefault();
      return;
    }
    if (key === 'n' && !e.shiftKey) {
      if (focusComposer()) e.preventDefault();
    }
  });
}
