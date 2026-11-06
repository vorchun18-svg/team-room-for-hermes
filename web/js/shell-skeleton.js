/**
 * shell-skeleton.js — «Загружаю…» в форме будущих карточек (B14).
 *
 * Аудит 06.11: на «Команде» скелетон держался дольше секунды, а в «Дальше»
 * вместо карточек были серые полосы и «0» в счётчиках. Здесь заглушка имеет
 * вид настоящей карточки (заголовок, подпись, строки текста, кнопки), а вместо
 * нулей показывается понятное «Загружаю…».
 *
 * Правила:
 *  * скелетон появляется не сразу (140 мс) — быстрый ответ не мигает полосами;
 *  * исчезает мгновенно, как только пришли данные (никаких искусственных пауз);
 *  * для скринридера — текст «Загружаю…», полосы скрыты (aria-hidden).
 */

import { h } from './util.js';
import { skeleton, ensureCss } from './ui.js';

/** Подпись загрузки — одна на весь каркас. */
export const LOADING_TEXT = 'Загружаю…';

/** Задержка перед показом заглушки: быстрый ответ обходится без мигания. */
export const SKELETON_DELAY_MS = 140;

/** Видимый текст для скринридера и подписи счётчиков. */
export function loadingLabel(what) {
  const tail = String(what || '').trim();
  return tail ? LOADING_TEXT + ' ' + tail : LOADING_TEXT;
}

/**
 * Число или «Загружаю…»: счётчик не должен показывать ноль, пока данных нет.
 * @param {number|null} n
 * @param {boolean} loading
 * @returns {string}
 */
export function countOrLoading(n, loading) {
  if (loading) return LOADING_TEXT;
  if (n === null || n === undefined || n === '') return '—';
  const value = Number(n);
  return Number.isFinite(value) ? String(value) : '—';
}

function line(cls = '') {
  return h('div', { class: ('skeleton skeleton--line ' + cls).trim() });
}

/**
 * Одна заглушка-карточка в форме настоящей.
 * @param {{variant?: 'card'|'row'|'question', lines?: number}} [opts]
 * @returns {HTMLElement}
 */
export function skeletonCard(opts = {}) {
  // Стили карточек-заглушек живут в shell-titles.css (общие файлы не правим).
  ensureCss('css/shell-titles.css');
  const variant = String(opts.variant || 'card');
  const lines = Math.max(1, Math.min(4, Number(opts.lines) || 2));
  const body = [];
  for (let i = 0; i < lines; i += 1) body.push(line(i === 0 ? '' : 'skeleton--short'));
  return h('div', { class: 'skel-card skel-card--' + variant, 'aria-hidden': 'true' },
    h('div', { class: 'skel-card__head' },
      h('div', { class: 'skeleton skeleton--title' }),
      h('div', { class: 'skeleton skel-card__pill' })),
    h('div', { class: 'skel-card__body' }, body),
    variant === 'question'
      ? h('div', { class: 'skel-card__foot' },
        h('div', { class: 'skeleton skel-card__input' }),
        h('div', { class: 'skeleton skel-card__btn' }))
      : null);
}

/**
 * Набор заглушек «в форме будущих карточек» + честная подпись «Загружаю…».
 * @param {{rows?: number, variant?: 'card'|'row'|'question', lines?: number,
 *          label?: string, class?: string}} [opts]
 * @returns {HTMLElement}
 */
export function skeletonCards(opts = {}) {
  const rows = Math.max(1, Math.min(8, Number(opts.rows) || 2));
  const variant = String(opts.variant || 'card');
  const wrap = h('div', {
    class: ('skel-stack ' + String(opts.class || '')).trim(),
    role: 'status',
    'aria-busy': 'true',
    'aria-live': 'polite',
  }, h('span', { class: 'sr-only' }, String(opts.label || LOADING_TEXT)));
  for (let i = 0; i < rows; i += 1) {
    wrap.appendChild(skeletonCard({ variant, lines: opts.lines }));
  }
  return wrap;
}

/** Компактный вариант: строки (шаги, повестка). */
export function skeletonRows(rows = 3) {
  ensureCss('css/shell-titles.css');
  const n = Math.max(1, Math.min(12, Number(rows) || 3));
  return h('div', { class: 'skel-rows', role: 'status', 'aria-busy': 'true' },
    h('span', { class: 'sr-only' }, LOADING_TEXT),
    Array.from({ length: n }, () => h('div', { class: 'skel-row', 'aria-hidden': 'true' },
      h('div', { class: 'skeleton skel-row__dot' }),
      h('div', { class: 'skel-row__text' },
        h('div', { class: 'skeleton skeleton--line' }),
        h('div', { class: 'skeleton skeleton--line skeleton--short' })))));
}

/**
 * Показать заглушку с задержкой; вернуть управление для мгновенного снятия.
 * @param {HTMLElement} host
 * @param {HTMLElement} node
 * @param {{delay?: number}} [opts]
 * @returns {{cancel: Function, show: Function}}
 */
export function showSkeletonAfter(host, node, opts = {}) {
  const delay = Math.max(0, Number(opts.delay) === undefined ? SKELETON_DELAY_MS : Number(opts.delay));
  let timer = null;
  let shown = false;
  const show = () => {
    if (!host || typeof host.replaceChildren !== 'function') return;
    host.replaceChildren(node);
    shown = true;
  };
  if (delay <= 0) show();
  else timer = setTimeout(show, delay);
  return {
    show,
    /** Данные пришли раньше срока — заглушку не показываем вовсе. */
    cancel() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (!shown) return;
      if (host && typeof host.replaceChildren === 'function') host.replaceChildren();
      shown = false;
    },
  };
}

export default { LOADING_TEXT, SKELETON_DELAY_MS, loadingLabel, countOrLoading, skeletonCard, skeletonCards, skeletonRows, showSkeletonAfter };
