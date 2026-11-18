/**
 * focus-mode.js — «Фокус» внутри страницы (волна 25, пакет L2, R5 §7.4).
 *
 * Честная граница (R5 §5): это НЕ режим хозяина. Сайдбар Hermes 280 px
 * остаётся, отдельного окна и `focusMode` в контракте десктопа нет. Здесь
 * убирается только СВОЁ: меню/рейка Team Room, шапка до одной строки, вкладки,
 * правая колонка (или нижний лист) и все свёрнутые строки блоков.
 * Остаётся: свод, липкая панель действий свода и тонкая строка
 * «От вас нужно N» (28 px) — единственный зов к действию.
 *
 * Состояние помнится НА МАРШРУТ (R5 §7.7, ключ `teamroom.focus`) и
 * сбрасывается при уходе со страницы запроса: в фокусе, открытом на одном
 * запросе, не должно открываться всё остальное приложение.
 *
 * Модуль без DOM-зависимостей сверх `dataset`/`localStorage` —
 * `node --test tests/focus-mode.test.mjs`.
 */

export const FOCUS_KEY = 'teamroom.focus';
export const FOCUS_ON = 'on';
export const FOCUS_OFF = 'off';

/**
 * Ключ состояния для маршрута: свой у каждого запроса и комнаты.
 * Пустой маршрут → общий ключ `~` (страницы вне комнаты).
 * @param {object|string} route маршрут роутера или строка
 * @returns {string}
 */
export function routeKeyOf(route) {
  if (route == null) return '~';
  if (typeof route === 'string') return route.trim() || '~';
  const r = route || {};
  const name = String(r.name || r.view || r.page || '').trim();
  const room = String(r.roomId || r.room || '').trim();
  const id = String(r.id || r.requestId || r.root || '').trim();
  if (name || room || id) return [name, room, id].filter(Boolean).join('/') || '~';
  return '~';
}

/** Прочитать карту «маршрут → включён ли фокус» из хранилища. */
export function readFocusMap(storage) {
  try {
    const raw = storage && storage.getItem ? storage.getItem(FOCUS_KEY) : null;
    if (!raw) return {};
    const parsed = JSON.parse(String(raw));
    const out = {};
    if (!parsed || typeof parsed !== 'object') return out;
    for (const k of Object.keys(parsed)) {
      if (parsed[k] === true) out[k] = true;
      else if (parsed[k] === false) out[k] = false;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Включён ли фокус на маршруте. Значение хранится строкой 'on'|'off',
 * отсутствие — выключен.
 * @param {Storage|null} storage
 * @param {string} routeKey
 * @returns {boolean}
 */
export function focusFor(storage, routeKey) {
  const map = readFocusMap(storage);
  return map[String(routeKey || '~')] === true;
}

/**
 * Запомнить состояние фокуса на маршруте.
 * @param {Storage|null} storage
 * @param {string} routeKey
 * @param {boolean} on
 * @returns {boolean} что записали
 */
export function writeFocus(storage, routeKey, on) {
  const key = String(routeKey || '~');
  const map = readFocusMap(storage);
  map[key] = on === true;
  // Карта маленькая, но мусор чистим: помним не больше 40 маршрутов.
  const keys = Object.keys(map);
  if (keys.length > 40) {
    for (const k of keys.slice(0, keys.length - 40)) delete map[k];
  }
  try {
    if (storage && storage.setItem) storage.setItem(FOCUS_KEY, JSON.stringify(map));
  } catch { /* переполнено — состояние живёт до перезагрузки */ }
  return on === true;
}

/** Значение атрибута `data-focus` по флагу. */
export function focusAttr(on) {
  return on === true ? FOCUS_ON : FOCUS_OFF;
}

/** Безопасное хранилище. */
function safeStorage(win) {
  try { return win && win.localStorage ? win.localStorage : null; } catch { return null; }
}

/**
 * Строка фокуса (28 px): «От вас нужно N» и выход одним нажатием.
 * @param {{total?: number, onExit?: Function}} [opts]
 * @returns {HTMLElement|null}
 */
export function buildFocusBar(opts = {}) {
  const doc = (typeof globalThis !== 'undefined' && globalThis.document) || null;
  if (!doc) return null;
  const total = Math.max(0, Number(opts.total) || 0);
  const bar = doc.createElement('div');
  bar.className = 'req-focusbar';
  bar.setAttribute('data-focusbar', '');
  const left = doc.createElement('span');
  left.className = 'req-focusbar__text';
  left.textContent = total
    ? 'От вас нужно: ' + total + ' · Esc — выйти из фокуса'
    : 'Фокус включён · Esc — вернуться к полному виду';
  const btn = doc.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn--ghost btn--sm';
  btn.setAttribute('data-action', 'focus-exit');
  btn.textContent = 'Выйти из фокуса';
  btn.addEventListener('click', () => { if (typeof opts.onExit === 'function') opts.onExit(); });
  bar.appendChild(left);
  bar.appendChild(btn);
  return bar;
}

/**
 * Подключить «Фокус» к странице.
 *
 * @param {{app: HTMLElement, win?: Window, storage?: Storage, route?: object|string,
 *          onChange?: (on: boolean) => void, startOn?: boolean}} opts
 * @returns {{on: Function, off: Function, toggle: Function, isOn: Function,
 *            routeKey: string, destroy: Function}}
 */
export function installFocusMode(opts = {}) {
  const app = opts.app || null;
  const win = opts.win || (typeof window !== 'undefined' ? window : null);
  const storage = opts.storage !== undefined ? opts.storage : safeStorage(win);
  const routeKey = String(opts.routeKey || routeKeyOf(opts.route));
  const onChange = typeof opts.onChange === 'function' ? opts.onChange : null;
  // Первое состояние: сохранённое на маршруте, либо явно переданное.
  let on = opts.startOn === true ? true : focusFor(storage, routeKey);

  function apply() {
    if (app && app.dataset) app.dataset.focus = focusAttr(on);
    if (onChange) {
      try { onChange(on); } catch (err) { console.warn('[focus-mode] onChange:', err); }
    }
    return on;
  }

  const api = {
    routeKey,
    isOn() { return on; },
    on() { on = true; writeFocus(storage, routeKey, true); return apply(); },
    off() { on = false; writeFocus(storage, routeKey, false); return apply(); },
    toggle() { return on ? api.off() : api.on(); },
    /** Сбросить без записи (уход со страницы запроса). */
    reset() { on = false; return apply(); },
    destroy() {
      // Страница ушла — фокус не переносится на другие экраны.
      on = false;
      apply();
    },
  };
  return api;
}

export default {
  FOCUS_KEY, FOCUS_ON, FOCUS_OFF, routeKeyOf, readFocusMap, focusFor, writeFocus,
  focusAttr, buildFocusBar, installFocusMode,
};
