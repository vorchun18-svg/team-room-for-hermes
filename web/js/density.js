/**
 * density.js — плотность интерфейса (волна 25, пакет L2, R5 §7.9).
 *
 * Три значения: `compact` / `normal` (умолчание) / `roomy`. Значение живёт
 * атрибутом `data-density` на `#app` — тот же контракт, что завела L1
 * (`layout-mode.js` читает ключ и ставит умолчание). CSS считает от атрибута,
 * поэтому JS ничего не перерисовывает: смена плотности — это одна запись
 * в `localStorage` и один атрибут.
 *
 * Свод плотность НЕ трогает (R5 §7.9, последняя строка): читаемость длинного
 * текста не жертвуется. В CSS это закреплено отдельно (`--svod-fs`).
 *
 * Ручной выбор сильнее автоматики (R5 §7.7): сохранённое значение всегда
 * применяется, как бы ни менялась ширина вида.
 *
 * Модуль без DOM-зависимостей сверх `dataset`/`localStorage` — проверяется
 * в `node --test tests/density.test.mjs`.
 */

import { DENSITIES, DENSITY_KEY, readDensity } from './layout-mode.js';

export { DENSITIES, DENSITY_KEY };
// `readDensity` — из layout-mode.js (L1): ключ и разбор хранилища живут там,
// чтобы плотность не разъехалась между оболочкой и страницей (R5 §7.7).
export { readDensity };

/** Человеческие подписи — по ним строится меню и подсказка. */
export const DENSITY_LABEL = {
  compact: 'Компактно',
  normal: 'Обычно',
  roomy: 'Просторно',
};

/** Порядок перебора по кругу (одна кнопка «плотнее/просторнее»). */
export const DENSITY_ORDER = ['compact', 'normal', 'roomy'];

/** Короткая подсказка, что даёт значение. */
export const DENSITY_HINT = {
  compact: 'строки 28 px, блоки 8 px',
  normal: 'строки 36 px, блоки 12–16 px',
  roomy: 'строки 44 px, блоки 20 px',
};

/**
 * Привести что угодно к известному значению плотности.
 * @param {*} value
 * @returns {'compact'|'normal'|'roomy'}
 */
export function normalizeDensity(value) {
  const v = String(value == null ? '' : value).trim();
  return DENSITIES.includes(v) ? v : 'normal';
}

/**
 * Следующая плотность по кругу: compact → normal → roomy → compact.
 * @param {string} value текущая
 * @param {number} [delta] +1 (плотнее вниз по списку) или −1
 * @returns {'compact'|'normal'|'roomy'}
 */
export function nextDensity(value, delta = 1) {
  const cur = normalizeDensity(value);
  const i = DENSITY_ORDER.indexOf(cur);
  const step = Number(delta) < 0 ? -1 : 1;
  const total = DENSITY_ORDER.length;
  return DENSITY_ORDER[(i + step + total) % total];
}

/** Подпись плотности для меню: «Плотность: обычно». */
export function densityLabel(value) {
  return DENSITY_LABEL[normalizeDensity(value)] || DENSITY_LABEL.normal;
}

/** Безопасное хранилище (приватный режим/запрет — null). */
function safeStorage(win) {
  try {
    return win && win.localStorage ? win.localStorage : null;
  } catch {
    return null;
  }
}

/**
 * Запомнить выбор плотности. Возвращает записанное значение (даже если
 * хранилище недоступно — тогда выбор живёт до перезагрузки).
 * @param {Storage|null} storage
 * @param {string} value
 * @returns {'compact'|'normal'|'roomy'}
 */
export function writeDensity(storage, value) {
  const v = normalizeDensity(value);
  try { if (storage && storage.setItem) storage.setItem(DENSITY_KEY, v); } catch { /* переполнено */ }
  return v;
}

/**
 * Поставить значение на `#app` и, если можно, запомнить.
 * @param {HTMLElement|null} app
 * @param {string} value
 * @param {Storage|null} [storage]
 * @returns {'compact'|'normal'|'roomy'} что в итоге стоит на элементе
 */
export function applyDensity(app, value, storage) {
  const v = writeDensity(storage, value);
  if (app && app.dataset) app.dataset.density = v;
  return v;
}

/**
 * Подключить плотность: прочитать сохранённое, поставить на `#app` и вернуть
 * контроллер. Повторный вызов на том же элементе безопасен (идемпотентно).
 *
 * @param {{app: HTMLElement, win?: Window, storage?: Storage}} opts
 * @returns {{value: string, set: Function, next: Function, label: Function, destroy: Function}}
 */
export function installDensity(opts = {}) {
  const app = opts.app || null;
  const win = opts.win || (typeof window !== 'undefined' ? window : null);
  const storage = opts.storage !== undefined ? opts.storage : safeStorage(win);
  let value = readDensity(storage);
  if (app && app.dataset) app.dataset.density = value;

  const api = {
    get value() { return value; },
    /** Поставить конкретное значение. */
    set(next) {
      value = applyDensity(app, next, storage);
      return value;
    },
    /** Переключить по кругу — «ещё плотнее/просторнее» одной кнопкой. */
    next(delta) {
      value = applyDensity(app, nextDensity(value, delta), storage);
      return value;
    },
    label() { return densityLabel(value); },
    destroy() { /* слушателей нет: состояние — атрибут */ },
  };
  return api;
}

export default {
  DENSITIES, DENSITY_KEY, DENSITY_LABEL, DENSITY_ORDER, DENSITY_HINT,
  normalizeDensity, nextDensity, densityLabel, writeDensity, applyDensity, installDensity,
};
