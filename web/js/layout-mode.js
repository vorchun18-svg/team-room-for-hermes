/**
 * layout-mode.js — режимы раскладки по ширине ВИДА (волна 25, пакет L1).
 *
 * Зачем отдельный модуль: до этого пороги жили в четырёх местах (@container в CSS,
 * `TWO_COL_MIN` в shell-rail.js, `--view-two-col-min` в tokens.css, проверки в
 * tools/probe_layout_split.py) и разъезжались. Теперь режим считает ОДНА функция,
 * а результат лежит на `#app` атрибутом `data-layout` — его читают и CSS, и JS.
 * В `<webview>` ширина окна хозяина недоступна, поэтому всё считается от ширины
 * области Team Room (`#view`), а не от `innerWidth`.
 *
 * Режимы (R5 §7.1, канон):
 *   xl  ≥ 1400 — меню развёрнуто, две колонки, поля 24, шапка в одну строку;
 *   l   1100–1399 — рейка 60, две колонки, поля 24;
 *   m   900–1099 — рейка 60, одна колонка, «От вас нужно» — нижний лист, поля 24;
 *   s   700–899 — ящик, одна колонка, лист, поля 16;
 *   xs  < 700 — ящик, одна колонка, лист 52, поля 12.
 *
 * Контракт для пакета L2 (стабильные атрибуты на `#app`):
 *   data-layout  xl|l|m|s|xs   — режим по ширине вида (единственный источник);
 *   data-density compact|normal|roomy — плотность (значение читает density.js L2);
 *   data-focus   on|off        — «Фокус» (ставит focus-mode.js L2, здесь только умолчание);
 *   data-aside   column|sheet|hidden — где живёт «От вас нужно».
 *
 * Ручной выбор пользователя сильнее автоматики (R5 §7.7): если в localStorage
 * лежит `teamroom.aside`, он и применяется; автоматика — когда выбора нет.
 */

/** Режимы от широкого к узкому. */
export const LAYOUT_MODES = ['xl', 'l', 'm', 's', 'xs'];

/** Нижние границы режимов, px ширины вида (одно число на режим). */
export const LAYOUT_MIN = { xl: 1400, l: 1100, m: 900, s: 700, xs: 0 };

/** Две колонки (центр + «От вас нужно») только с этой ширины вида. */
export const TWO_COL_MIN = LAYOUT_MIN.l;
/** Минимальная ширина центра при двух колонках (канон R5 §9 п. 1). */
export const MIN_MAIN_TWO_COL = 640;
/** Минимальная ширина правой колонки при двух колонках. */
export const MIN_ASIDE_TWO_COL = 280;
/** Максимальная ширина правой колонки: дальше она отнимает у свода. */
export const MAX_ASIDE_TWO_COL = 320;
/** Нижний лист «От вас нужно» в свёрнутом виде, px. */
export const SHEET_H = 56;
export const SHEET_H_XS = 52;

/** Ключи localStorage (R5 §7.7). */
export const ASIDE_KEY = 'teamroom.aside';
export const DENSITY_KEY = 'teamroom.density';

/** Плотности, которые понимает `data-density`. */
export const DENSITIES = ['compact', 'normal', 'roomy'];

/**
 * Режим по ширине вида.
 * @param {number} width ширина области Team Room, px
 * @returns {'xl'|'l'|'m'|'s'|'xs'}
 */
export function modeForView(width) {
  const w = Number(width) || 0;
  if (w >= LAYOUT_MIN.xl) return 'xl';
  if (w >= LAYOUT_MIN.l) return 'l';
  if (w >= LAYOUT_MIN.m) return 'm';
  if (w >= LAYOUT_MIN.s) return 's';
  return 'xs';
}

/**
 * Где живёт «От вас нужно» при такой ширине вида.
 * Правило одной строкой (R5 §7.5): колонка никогда не отнимает ширину у свода,
 * если центру остаётся меньше 640 px — а это ровно случай вида < 1100.
 * @param {number} width ширина вида, px
 * @returns {'column'|'sheet'}
 */
export function asideForView(width) {
  return (Number(width) || 0) >= TWO_COL_MIN ? 'column' : 'sheet';
}

/**
 * Итоговый режим ящика с учётом ручного выбора.
 * @param {number} width ширина вида
 * @param {'column'|'sheet'|'hidden'|null|undefined} manual выбор пользователя
 * @returns {'column'|'sheet'|'hidden'}
 */
export function resolveAside(width, manual) {
  if (manual === 'column' || manual === 'sheet' || manual === 'hidden') return manual;
  return asideForView(width);
}

/** Плотность из хранилища; мусор и недоступное хранилище → 'normal'. */
export function readDensity(storage) {
  try {
    const raw = storage && storage.getItem ? String(storage.getItem(DENSITY_KEY) || '') : '';
    return DENSITIES.includes(raw) ? raw : 'normal';
  } catch {
    return 'normal';
  }
}

/** Ручной выбор «от вас нужно» из хранилища; нет выбора → null (автоматика). */
export function readManualAside(storage) {
  try {
    const raw = storage && storage.getItem ? String(storage.getItem(ASIDE_KEY) || '') : '';
    return raw === 'column' || raw === 'sheet' || raw === 'hidden' ? raw : null;
  } catch {
    return null;
  }
}

function safeStorage(win) {
  try {
    return win && win.localStorage ? win.localStorage : null;
  } catch {
    return null;
  }
}

/**
 * Подключить режимы: пересчитывать по ширине вида и держать `data-*` на `#app`.
 *
 * Наблюдаем ДВА элемента: `.app` (ширина оболочки — её отдаёт хозяин) и `#view`
 * (ширина области). Меню-полоса меняет ширину вида, не меняя ширину оболочки:
 * без второго наблюдателя `data-layout` отставал бы от CSS на 188 px.
 *
 * @param {{app: HTMLElement, view?: HTMLElement, win?: Window, storage?: Storage,
 *          onMode?: (state: {mode: string, aside: string, width: number}) => void}} opts
 * @returns {{apply: Function, mode: ?string, aside: ?string, width: number, destroy: Function}}
 */
export function installLayoutMode(opts = {}) {
  const app = opts.app;
  if (!app || !app.dataset) return { apply() {}, mode: null, aside: null, width: 0, destroy() {} };
  const win = opts.win || (typeof window !== 'undefined' ? window : null);
  const view = opts.view || (app.querySelector ? app.querySelector('#view') : null);
  const storage = opts.storage !== undefined ? opts.storage : safeStorage(win);
  const onMode = typeof opts.onMode === 'function' ? opts.onMode : null;

  let mode = null;
  let aside = null;
  let width = 0;
  let density = readDensity(storage);
  let observers = [];

  const readWidth = () => Math.round(
    (view && view.clientWidth) || (app.clientWidth ? app.clientWidth : 0),
  );

  function apply() {
    const w = readWidth();
    if (!w) return { mode, aside, width };
    width = w;
    const nextMode = modeForView(w);
    const nextAside = resolveAside(w, readManualAside(storage));
    density = readDensity(storage);

    app.dataset.layout = nextMode;
    app.dataset.viewWidth = String(w);
    app.dataset.density = density;
    app.dataset.aside = nextAside;
    // L2 владеет «Фокусом»: не затираем его состояние, только ставим умолчание.
    if (!app.dataset.focus) app.dataset.focus = 'off';

    // То же число — в CSS-переменную: по ней считаются ширины, где `cqi` неудобен.
    const root = (win && win.document && win.document.documentElement) || null;
    if (root && root.style) root.style.setProperty('--view-inline', w + 'px');

    const changed = nextMode !== mode || nextAside !== aside;
    mode = nextMode;
    aside = nextAside;
    if (changed && onMode) {
      try {
        onMode({ mode, aside, width });
      } catch (err) {
        console.warn('[layout-mode] onMode:', err);
      }
    }
    if (changed && win && typeof win.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
      try {
        win.dispatchEvent(new CustomEvent('teamroom:layout', { detail: { mode, aside, width } }));
      } catch { /* старый браузер без CustomEvent */ }
    }
    return { mode, aside, width };
  }

  const RO = win && win.ResizeObserver ? win.ResizeObserver : (typeof ResizeObserver !== 'undefined' ? ResizeObserver : null);
  if (RO) {
    const obs = new RO(() => { apply(); });
    obs.observe(app);
    if (view && view !== app) obs.observe(view);
    observers.push(obs);
  } else if (win && win.addEventListener) {
    win.addEventListener('resize', apply);
    observers.push({ disconnect: () => win.removeEventListener('resize', apply) });
  }

  apply();

  return {
    apply,
    get mode() { return mode; },
    get aside() { return aside; },
    get width() { return width; },
    destroy() {
      for (const o of observers) { try { o.disconnect(); } catch { /* уже снят */ } }
      observers = [];
    },
  };
}

export default { installLayoutMode, modeForView, asideForView, resolveAside, LAYOUT_MIN, TWO_COL_MIN };
