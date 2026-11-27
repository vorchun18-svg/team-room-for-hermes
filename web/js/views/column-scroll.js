/**
 * column-scroll.js — независимая прокрутка колонок в окне задачи (W31-B2, дефект 25).
 *
 * Дефект: колесо над правой колонкой («От вас нужно») крутило левую (основную) —
 * у колонки не было своей прокрутки, и колесо уходило на общий скролл страницы,
 * который двигал обе. Нужно: колесо и клавиши действуют на колонку ПОД КУРСОРОМ,
 * вторая стоит; общая полоса страницы двигает обе вместе.
 *
 * Как сделано:
 *   * CSS (view-request.css) даёт каждой колонке свой `overflow-y:auto` и
 *     `overscroll-behavior:contain` — вложенный скролл не «сцепляется» с соседом;
 *   * этот модуль добавляет то, чего CSS не умеет: клавиши. Колесо обрабатывает
 *     браузер (элемент под курсором), а PageUp/PageDown/Home/End/↑/↓ после
 *     наведения на колонку прокручивают именно её;
 *   * если колонка не прокручивается совсем (содержимое короче окна), клавиши
 *     ведут себя как обычно — страница прокручивается штатно.
 *
 * Модуль не зависит от DOM-событий браузера целиком: `installColumnScroll`
 * принимает `root`/`doc`, события можно вызвать руками (`node --test`).
 */

/** Колонки страницы задачи: имя → селектор. */
export const COLUMNS = [
  { name: 'main', selector: '.req__main' },
  { name: 'aside', selector: '.req__aside' },
];

/** Клавиши прокрутки и что они делают (в долях высоты окна колонки). */
const KEY_STEPS = {
  PageDown: 0.9,
  PageUp: -0.9,
  ArrowDown: 0.08,
  ArrowUp: -0.08,
};

/**
 * Прокручивается ли узел (содержимое выше окна).
 * @param {HTMLElement|null} el
 * @returns {boolean}
 */
export function isScrollable(el) {
  if (!el) return false;
  const height = Number(el.clientHeight) || 0;
  const full = Number(el.scrollHeight) || 0;
  return full - height > 2;
}

/**
 * Прокрутить колонку на клавишу. Возвращает `true`, если клавиша «съедена».
 *
 * @param {HTMLElement|null} el
 * @param {string} key
 * @param {{top?: number, bottom?: number}} [viewport] высота окна колонки (для PageUp/Down)
 * @returns {boolean}
 */
export function scrollByKey(el, key, viewport) {
  if (!isScrollable(el)) return false;
  const height = Number(el.clientHeight) || Number(viewport && viewport.height) || 0;
  const max = Math.max(0, (Number(el.scrollHeight) || 0) - height);
  const k = String(key || '');
  if (k === 'Home') { el.scrollTop = 0; return true; }
  if (k === 'End') { el.scrollTop = max; return true; }
  const step = KEY_STEPS[k];
  if (!step) return false;
  const delta = Math.abs(step) <= 1 ? Math.round(height * step) : step;
  const next = Math.min(max, Math.max(0, (Number(el.scrollTop) || 0) + delta));
  el.scrollTop = next;
  return true;
}

/**
 * Прокрутить колонку колесом. Возвращает `true`, если колесо «съедено» колонкой.
 *
 * Браузер и сам прокручивает элемент под курсором, но только если тот
 * прокручиваемый; явная обработка делает поведение одинаковым в обеих колонках и
 * не даёт колесу «проваливаться» на страницу у края.
 *
 * @param {HTMLElement|null} el
 * @param {number} deltaY
 * @returns {boolean}
 */
export function scrollByWheel(el, deltaY) {
  if (!isScrollable(el)) return false;
  const delta = Number(deltaY) || 0;
  if (!delta) return false;
  const max = Math.max(0, (Number(el.scrollHeight) || 0) - (Number(el.clientHeight) || 0));
  el.scrollTop = Math.min(max, Math.max(0, (Number(el.scrollTop) || 0) + delta));
  return true;
}

/** Ближайший предок (включая узел), подходящий под селектор — переносимо. */
function closestOf(node, selector) {
  let cur = node;
  while (cur && cur.nodeType === 1) {
    if (typeof cur.matches === 'function') {
      if (cur.matches(selector)) return cur;
    } else if (typeof cur.closest === 'function') {
      const hit = cur.closest(selector);
      if (hit) return hit;
    }
    cur = cur.parentNode;
  }
  return null;
}

/**
 * Повесить поведение на страницу задачи.
 *
 * @param {object} opts
 * @param {HTMLElement} opts.root         корень страницы (`.req`)
 * @param {Document} [opts.doc]           документ (по умолчанию `document`)
 * @param {Function} [opts.isTyping]      `(target) => boolean` — в поле ввода клавиши молчат
 * @returns {{destroy:Function, hovered:Function, columnOf:Function}}
 */
export function installColumnScroll(opts = {}) {
  const o = opts || {};
  const root = o.root || null;
  const doc = o.doc || (typeof document !== 'undefined' ? document : null);
  const isTyping = typeof o.isTyping === 'function'
    ? o.isTyping
    : (target) => {
      const tag = String((target && target.localName) || '').toLowerCase();
      return tag === 'input' || tag === 'textarea' || tag === 'select'
        || Boolean(target && target.isContentEditable);
    };
  /** Какая колонка под курсором: имя, а не узел (дерево перерисовывается). */
  let hovered = '';

  function columnOf(node) {
    for (const col of COLUMNS) {
      const el = closestOf(node, col.selector);
      if (el) return col;
    }
    return null;
  }

  function live(name) {
    if (!root || typeof root.querySelector !== 'function') return null;
    const col = COLUMNS.find((c) => c.name === name);
    return col ? root.querySelector(col.selector) : null;
  }

  function onOver(ev) {
    const target = ev && ev.target;
    const col = columnOf(target);
    if (col) hovered = col.name;
  }

  function onWheel(ev) {
    const col = columnOf(ev && ev.target);
    if (!col) return;
    const el = live(col.name);
    hovered = col.name;
    if (!el) return;
    if (!isScrollable(el)) return;              // короткая колонка — страница как обычно
    // Колесо — только этой колонке: соседняя стоит, страница не дёргается.
    if (scrollByWheel(el, Number(ev.deltaY) || 0)) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
    }
  }

  function onKey(ev) {
    if (!ev || ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (isTyping(ev.target)) return;
    const key = String(ev.key || '');
    if (!hovered) return;                        // курсор не над колонкой — ведёт себя как обычно
    const el = live(hovered);
    if (!scrollByKey(el, key)) return;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
  }

  if (root && typeof root.addEventListener === 'function') {
    root.addEventListener('mouseover', onOver);
    root.addEventListener('wheel', onWheel, { passive: false });
  }
  if (doc && typeof doc.addEventListener === 'function') {
    doc.addEventListener('keydown', onKey);
  }

  return {
    destroy() {
      try { root && root.removeEventListener && root.removeEventListener('mouseover', onOver); } catch { /* ignore */ }
      try { root && root.removeEventListener && root.removeEventListener('wheel', onWheel); } catch { /* ignore */ }
      try { doc && doc.removeEventListener && doc.removeEventListener('keydown', onKey); } catch { /* ignore */ }
    },
    /** Какая колонка считается активной (для тестов и отладки). */
    hovered() { return hovered; },
    columnOf,
  };
}

export default { installColumnScroll, scrollByKey, scrollByWheel, isScrollable, COLUMNS };
