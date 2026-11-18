/**
 * shell-rail.js — режим левого меню и пороги раскладки вида (волна 21, R1).
 *
 * Жалоба 11.11: «адаптивен только центр» — постоянное меню 208–248 px съедало
 * место, а центральная часть сжималась. Здесь три состояния меню:
 *
 *   auto  — по ширине ОКНА: < 1280 px полоса значков, иначе обычное меню (умолчание);
 *   wide  — закреплено развёрнутым (выбор пользователя, помнится локально);
 *   rail  — закреплено полосой значков (то же);
 *   < 900 px — выдвижной ящик (гамбургер), как было; выбор пользователя не теряется.
 *
 * Чистые функции вынесены отдельно от DOM: их проверяет tests/layout-rail.test.mjs
 * без браузера. Ширины и пороги — те же числа, что в CSS (shell-rail.css,
 * view-request.css: @container view min-width 1000px). Меняете здесь — меняйте там.
 */

/**
 * Ключ localStorage: 'auto' | 'wide' | 'rail'.
 */
export const RAIL_KEY = 'teamroom.sidebarMode';

/* ───────────────────────────── W26-D7: шеврон на краю панели и Ctrl+B ──── */

/** Подсказка кнопки-шеврона: клавиша рядом с действием (всегда одна и та же). */
export const FOLD_TITLE = 'Свернуть меню (Ctrl+B)';
export const UNFOLD_TITLE = 'Развернуть меню (Ctrl+B)';
/** Горячая клавиша сворачивания меню — одна на справку, подсказку и hotkeys.js. */
export const RAIL_HOTKEY = 'Ctrl+B';
/**
 * Событие «свернуть/развернуть меню»: его шлёт hotkeys.js по Ctrl+B, слушает
 * installShellRail. Так клавиша не требует правки каркаса (app.js) и работает
 * там, где полоса значков вообще есть.
 */
export const RAIL_EVENT = 'teamroom:toggle-rail';
/**
 * W25-L1: рейка включается, когда РАЗВЁРНУТОЕ меню съедает вид ниже 1400 px
 * (R5 §7.1: «рейка при виде < 1400»). Отсюда порог оболочки = 1400 + 248 = 1648:
 * при окне 1920 с сайдбаром Hermes 280 оболочка 1640 → рейка, вид 1580 вместо 1392.
 * Прежний порог 1280 считался от ширины окна и на 1920 оставлял панель 248.
 */
export const MENU_WIDE_MIN = 1400 + 248;
/** Совместимость с волной 21: тот же порог, прежнее имя. */
export const RAIL_BREAKPOINT = MENU_WIDE_MIN;
/** Окно уже этого — меню уходит в выдвижной ящик (как в components.css). */
export const DRAWER_BREAKPOINT = 900;
/** Вид шире этого — две колонки на странице запроса (см. @container view). */
export const TWO_COL_MIN = 1100;
/** Минимальная ширина основного блока: две колонки / любая раскладка. */
export const MIN_MAIN_TWO_COL = 640;
export const MIN_MAIN_ANY = 320;
/** Минимальная ширина правой колонки в двухколоночном режиме. */
export const MIN_ASIDE_TWO_COL = 280;

/** Прочитать выбор пользователя; мусор и недоступное хранилище → 'auto'. */
export function readMode(storage) {
  try {
    const raw = storage && storage.getItem ? storage.getItem(RAIL_KEY) : null;
    return raw === 'wide' || raw === 'rail' ? raw : 'auto';
  } catch {
    return 'auto';
  }
}

/** Запомнить выбор; недоступное хранилище (приватный режим) не ломает интерфейс. */
export function storeMode(storage, mode) {
  const value = mode === 'wide' || mode === 'rail' ? mode : 'auto';
  try {
    if (!storage) return value;
    if (value === 'auto') storage.removeItem(RAIL_KEY);
    else storage.setItem(RAIL_KEY, value);
  } catch {
    /* хранилище недоступно — режим живёт до перезагрузки */
  }
  return value;
}

/**
 * Что показать при данной ширине окна.
 * @param {'auto'|'wide'|'rail'} mode выбор пользователя
 * @param {number} width ширина окна, px
 * @returns {'drawer'|'rail'|'wide'}
 */
export function effectiveMode(mode, width) {
  const w = Number(width) || 0;
  const picked = mode === 'wide' || mode === 'rail' ? mode : 'auto';
  if (w < DRAWER_BREAKPOINT) return 'drawer';
  if (picked === 'rail') return 'rail';
  if (picked === 'wide') return 'wide';
  return w < RAIL_BREAKPOINT ? 'rail' : 'wide';
}

/**
 * Следующий выбор пользователя по нажатию переключателя: развернуть ↔ свернуть.
 * Из 'auto' первое нажатие фиксирует противоположное текущему поведение, чтобы
 * кнопка всегда что-то меняла (а не «возвращала авто» незаметно).
 */
export function nextMode(mode, width) {
  const now = effectiveMode(mode, width);
  return now === 'rail' ? 'wide' : 'rail';
}

/**
 * Колонки страницы запроса по доступной ширине вида (без ширины окна):
 * две — только начиная с TWO_COL_MIN, иначе одна.
 * @param {number} viewWidth ширина колонки содержимого (#view), px
 * @returns {1|2}
 */
export function columnsForView(viewWidth) {
  return (Number(viewWidth) || 0) >= TWO_COL_MIN ? 2 : 1;
}

/**
 * Проверка ширины основного блока: в двух колонках ≥ 520 px, в любой ≥ 320 px.
 * @param {number} mainWidth
 * @param {1|2} columns
 * @returns {{ok: boolean, min: number}}
 */
export function mainWidthOk(mainWidth, columns) {
  const min = Number(columns) === 2 ? MIN_MAIN_TWO_COL : MIN_MAIN_ANY;
  const w = Math.round(Number(mainWidth) || 0);
  return { ok: w >= min, min };
}

/** Подписи кнопки меню по текущему состоянию (W26-D7: шеврон + клавиша в подсказке). */
export function railButtonLabels(state) {
  const rail = state === 'rail';
  return {
    title: rail ? UNFOLD_TITLE : FOLD_TITLE,
    ariaLabel: rail ? 'Развернуть меню' : 'Свернуть меню',
    pressed: rail,
    // Шеврон смотрит туда, куда уедет панель: ««» — свернуть, «»» — развернуть.
    icon: rail ? 'chevron-right' : 'chevron-left',
  };
}

/**
 * Числа у пункта комнаты: сколько ДЕЛ ждёт человека и сколько работы идёт.
 *
 * W26-D7: счётчик меню считает ждущие ДЕЙСТВИЯ (вопросы, решения, блокировки,
 * разрешения — их даёт общий склад `shell-inbox.js`), а не запросы и не реакции
 * руководителя: реакции показывает отдельная отметка «Недавнего».
 *
 * @param {{n: number|null, text?: string}} waits результат waitingFor()
 * @param {object} [counts] `room.counts` — только идущая работа
 * @returns {{you: {n: number, text: string}|null, live: {n: number, text: string}|null}}
 */
export function railBadges(waits, counts) {
  const n = waits && Number.isFinite(waits.n) ? Math.max(0, Math.floor(waits.n)) : null;
  const working = Math.max(0, Math.floor(Number((counts && counts.working) || 0)));
  return {
    you: n === null ? null : { n, text: String((waits && waits.text) || '') },
    live: working > 0 ? { n: working, text: '' } : null,
  };
}

/**
 * Подсказка пункта комнаты в меню (W26-D7): в полосе значков подписи нет, и
 * единственное место, где видно и название, и числа, — title и aria-label.
 * @param {string} title название комнаты
 * @param {{you?: {n:number}|null, live?: {n:number}|null}} badges из railBadges()
 * @returns {string}
 */
export function roomNavTitle(title, badges) {
  const name = String(title === null || title === undefined ? '' : title).trim() || 'Комната';
  const parts = [];
  const you = badges && badges.you;
  if (you && you.n > 0) parts.push('ждут вас: ' + you.n);
  const live = badges && badges.live;
  if (live && live.n > 0) parts.push('идёт: ' + live.n);
  return parts.length ? name + ' — ' + parts.join(', ') : name;
}

/**
 * Подсказка кнопки-шеврона для текущего состояния — одна строка на все места
 * (кнопка, справка, тесты): «Свернуть меню (Ctrl+B)».
 * @param {boolean} rail
 * @returns {string}
 */
export function railToggleTitle(rail) {
  return rail ? UNFOLD_TITLE : FOLD_TITLE;
}

/**
 * Применить режим к `.app` (класс `is-rail`) и кнопкам-переключателям.
 * Кнопок может быть несколько (шеврон на краю панели и, если понадобится,
 * дубль в подвале) — состояние ставится всем: `button` (одна) или `buttons`.
 * @param {{app: HTMLElement, button?: HTMLElement, buttons?: HTMLElement[]}} els
 * @param {'auto'|'wide'|'rail'} mode
 * @param {number} width
 * @returns {'drawer'|'rail'|'wide'}
 */
export function applyMode(els, mode, width) {
  const state = effectiveMode(mode, width);
  const app = els && els.app;
  if (app && app.classList) {
    app.classList.toggle('is-rail', state === 'rail');
    app.dataset.shellMode = state;
  }
  const list = [];
  if (els && els.button) list.push(els.button);
  if (els && Array.isArray(els.buttons)) for (const b of els.buttons) if (b && list.indexOf(b) < 0) list.push(b);
  const labels = railButtonLabels(state);
  for (const btn of list) {
    btn.title = labels.title;
    btn.setAttribute('aria-label', labels.ariaLabel);
    btn.setAttribute('aria-pressed', labels.pressed ? 'true' : 'false');
    if (btn.dataset) btn.dataset.railState = state;
    const ic = btn.querySelector ? btn.querySelector('.codicon') : null;
    if (ic) ic.className = 'codicon codicon-' + labels.icon;
  }
  return state;
}

/**
 * Подключить режим: применить сейчас, пересчитывать при изменении окна, помнить
 * выбор пользователя. Возвращает ручку для повторного применения (тесты, демо).
 * @param {{app: HTMLElement, button?: HTMLElement, storage?: Storage, win?: Window}} opts
 */
export function installShellRail(opts) {
  const app = opts && opts.app;
  const win = (opts && opts.win) || (typeof window !== 'undefined' ? window : null);
  const storage = opts && opts.storage !== undefined
    ? opts.storage
    : (() => { try { return win && win.localStorage; } catch { return null; } })();
  if (!app) return { apply() {}, toggle() {}, state: 'wide', mode: 'auto' };

  let mode = readMode(storage);
  // W25-L1: ширина оболочки — от `#app`, а не от окна: в webview это одно и то же,
  // в макете «окно Hermes + iframe» — нет (и тогда панель оставалась на 248 px).
  const widthOf = () => {
    if (opts && typeof opts.getWidth === 'function') {
      const w = Math.round(opts.getWidth() || 0);
      if (w) return w;
    }
    return win ? win.innerWidth : 1440;
  };
  let width = widthOf();
  let state = applyMode({ app, button: opts.button, buttons: opts.buttons }, mode, width);

  const raf = (fn) => {
    if (win && typeof win.requestAnimationFrame === 'function') return win.requestAnimationFrame(fn);
    return setTimeout(fn, 16);
  };

  let frame = 0;
  function reapply() {
    if (frame) return;
    frame = raf(() => {
      frame = 0;
      width = widthOf();
      state = applyMode({ app, button: opts.button, buttons: opts.buttons }, mode, width);
    });
  }

  /**
   * W26-D7: на узком окне (< 900 px) панель — выдвижной ящик, а не полоса
   * значков. Ctrl+B там не должен молча ничего делать: он открывает и
   * закрывает сам ящик (шеврон на краю в этом режиме скрыт).
   * @returns {boolean} ящик теперь открыт?
   */
  function toggleDrawer() {
    const on = !(app.classList && app.classList.contains('nav-open'));
    if (app.classList) app.classList.toggle('nav-open', on);
    const hamburger = app.querySelector ? app.querySelector('.nav-toggle') : null;
    if (hamburger) {
      hamburger.setAttribute('aria-expanded', on ? 'true' : 'false');
      const label = on ? 'Закрыть меню комнат' : 'Открыть меню комнат';
      hamburger.setAttribute('aria-label', label);
      hamburger.setAttribute('title', label);
    }
    return on;
  }

  function toggle() {
    // Ящик — не наш режим: выбор пользователя (wide|rail) не перебиваем.
    if (state === 'drawer') {
      toggleDrawer();
      return state;
    }
    mode = nextMode(mode, width);
    storeMode(storage, mode);
    state = applyMode({ app, button: opts.button, buttons: opts.buttons }, mode, width);
    return state;
  }

  if (win && win.addEventListener) win.addEventListener('resize', reapply);
  // W21-R2: Esc в полосе значков складывает раскрытую панель. Она появляется при
  // наведении/фокусе и закрывает содержимое; с клавиатуры из неё иначе не выйти
  // (фокус остаётся внутри, панель висит поверх). Возвращается при новом фокусе
  // или когда курсор уходит с меню.
  const sidebar = app.querySelector && typeof app.querySelector === 'function'
    ? app.querySelector('.sidebar') : null;
  let quietAt = 0;
  const setQuiet = () => {
    if (!sidebar || !sidebar.classList) return;
    sidebar.classList.add('rail--quiet');
    quietAt = Date.now();
  };
  const unquiet = () => {
    if (sidebar && sidebar.classList) sidebar.classList.remove('rail--quiet');
  };
  if (sidebar && sidebar.addEventListener) {
    sidebar.addEventListener('keydown', (ev) => {
      if (!ev || ev.key !== 'Escape' || !app.classList.contains('is-rail')) return;
      setQuiet();
      try {
        if (sidebar.contains(document.activeElement)) document.activeElement.blur();
      } catch { /* фокус уже вне меню */ }
    });
    sidebar.addEventListener('pointerenter', unquiet);
    sidebar.addEventListener('focusin', unquiet);
    // Chrome не считает «вход» в панель, если курсор в момент складывания уже
    // стоял на её прежней (широкой) области: pointerenter не приходит, и полоса
    // остаётся мёртвой до выхода за пределы. Движение мыши снимает «тишину»,
    // но не мгновенно после Esc/нажатия — иначе панель раскрывалась бы от дрожи.
    sidebar.addEventListener('pointermove', () => {
      if (quietAt && Date.now() - quietAt > 300) unquiet();
    });
  }

  let onClick = null;
  if (opts && opts.button && opts.button.addEventListener) {
    onClick = (ev) => {
      if (ev && ev.preventDefault) ev.preventDefault();
      const next = toggle();
      if (!sidebar) return;
      // W21-R2: фокус остаётся на кнопке внутри меню, а `:focus-within` держал бы
      // панель раскрытой — «свернуть» визуально не срабатывало. Складываем сразу;
      // при новом фокусе или наведении панель снова раскроется. Мышиный клик
      // снимает фокус с кнопки (иначе панель «залипала» раскрытой после наведения),
      // клавиатурный — оставляет: у клавиатуры есть Esc и Tab.
      sidebar.classList.toggle('rail--quiet', next === 'rail');
      if (next === 'rail') quietAt = Date.now();
      const byMouse = ev && typeof ev.detail === 'number' && ev.detail > 0;
      if (next === 'rail' && byMouse && opts.button.blur) opts.button.blur();
    };
    opts.button.addEventListener('click', onClick);
  }

  // W26-D7: Ctrl+B приходит событием из hotkeys.js — клавиша и кнопка делают
  // одно и то же, и каркасу (app.js) не нужно знать про меню.
  const doc = (opts && opts.doc) || (win && win.document)
    || (typeof document !== 'undefined' ? document : null);
  const onKeyToggle = (ev) => {
    if (ev && ev.preventDefault) ev.preventDefault();
    const next = toggle();
    if (!sidebar) return;
    sidebar.classList.toggle('rail--quiet', next === 'rail');
    if (next === 'rail') quietAt = Date.now();
  };
  if (doc && doc.addEventListener) doc.addEventListener(RAIL_EVENT, onKeyToggle);

  return {
    apply: reapply,
    toggle,
    get state() { return state; },
    get mode() { return mode; },
    destroy() {
      // W21-REV2: снимаем ОБА обработчика — иначе повторная установка (тесты, демо,
      // перерисовка каркаса) копила бы подписки на кнопку и окно.
      if (win && win.removeEventListener) win.removeEventListener('resize', reapply);
      if (doc && doc.removeEventListener) doc.removeEventListener(RAIL_EVENT, onKeyToggle);
      if (onClick && opts && opts.button && opts.button.removeEventListener) {
        opts.button.removeEventListener('click', onClick);
      }
    },
  };
}
