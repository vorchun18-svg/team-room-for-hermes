// Рендерер отчёта: шапка, реестр блоков, оглавление, печать, error boundary.

import { h } from '../util.js';
import { mdNode } from './md-node.js';
import { BLOCKS } from './blocks.js';
import { renderTable } from './table.js';

const registry = new Map();
for (const [type, fn] of Object.entries(BLOCKS)) registry.set(type, fn);
registry.set('table', renderTable);

/* ---------- стили отчёта: index.html их не знает, подключаем сами ---------- */

const STYLE_FILES = ['../../css/report.css', '../../css/report-charts.css', '../../css/report-graph.css'];
let stylesRequested = false;

/** Уже есть <link> на файл с таким именем? (демо/экспорт подключают сами) */
function styleLoaded(name) {
  const links = document.querySelectorAll('link[rel="stylesheet"]');
  for (const link of links) {
    const href = link.getAttribute('href') || '';
    if (href === name || href.endsWith('/' + name)) return true;
  }
  return false;
}

/**
 * Подключить CSS отчёта один раз на документ. Пути считаются от import.meta.url,
 * поэтому работают и в приложении (`/js/report/index.js`), и в демо-страницах.
 * В автономном HTML reportkit модули живут в blob: — там стили уже встроены.
 * @returns {void}
 */
export function ensureReportStyles() {
  if (stylesRequested || typeof document === 'undefined' || !document.head) return;
  stylesRequested = true;
  let base = '';
  try { base = import.meta.url; } catch { base = ''; }
  if (/^blob:/i.test(base)) return;
  for (const rel of STYLE_FILES) {
    const name = rel.split('/').pop();
    if (styleLoaded(name)) continue;
    let href = rel;
    try { href = new URL(rel, base).href; } catch { /* оставляем относительный путь */ }
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    link.dataset.reportStyle = name;
    document.head.appendChild(link);
  }
}

/* ---------- chart / graph: динамический import с заглушкой ---------- */

function fallbackCard(title, note) {
  return h('div', { class: 'rb-fallback' },
    h('div', { class: 'rb-fallback__title' }, title),
    note ? h('div', { class: 'rb-fallback__note' }, note) : null);
}

function chartLike(type) {
  return (spec, ctx) => {
    const slot = h('div', { class: 'rb-chart-slot is-pending' }, 'Загрузка графика…');
    const mod = type === 'chart' ? './charts.js' : './graph.js';
    import(mod).then((m) => {
      const fn = type === 'chart' ? m.renderChart : m.renderGraph;
      if (typeof fn !== 'function') throw new Error(`в ${mod} нет экспорта ${type === 'chart' ? 'renderChart' : 'renderGraph'}`);
      const node = fn(spec, ctx);
      slot.textContent = '';
      slot.classList.remove('is-pending');
      if (node instanceof Node) slot.appendChild(node);
      else slot.appendChild(fallbackCard(type === 'chart' ? 'График недоступен' : 'Граф недоступен', 'модуль вернул пустой результат'));
    }).catch((err) => {
      slot.textContent = '';
      slot.classList.remove('is-pending');
      slot.classList.add('is-missing');
      slot.appendChild(fallbackCard(type === 'chart' ? 'График недоступен' : 'Граф недоступен', String(err && err.message || err)));
    });
    return slot;
  };
}

registry.set('chart', chartLike('chart'));
registry.set('graph', chartLike('graph'));

/** Добавить/переопределить рендерер блока. */
export function registerBlock(type, fn) {
  if (typeof type !== 'string' || !type) throw new Error('registerBlock: нужен тип');
  if (typeof fn !== 'function') throw new Error('registerBlock: нужен рендерер');
  registry.set(type, fn);
  return fn;
}

/** Список зарегистрированных типов блоков. */
export function blockTypes() {
  return [...registry.keys()].sort();
}

/* ---------- утилиты ---------- */

function makeResolver(fileUrl) {
  return (p) => {
    if (!p) return '';
    const s = String(p);
    if (/^(https?:|data:|blob:|file:)/i.test(s)) return s;
    if (/^\//.test(s)) return s;
    if (!fileUrl) return s;
    try { return new URL(s, fileUrl).href; } catch { return s; }
  };
}

function blockErrorCard(type, err) {
  return h('div', { class: 'rb-error', role: 'alert' },
    h('div', { class: 'rb-error__head' },
      h('span', { class: 'rb-error__icon', 'aria-hidden': 'true' }, '!'),
      h('span', { class: 'rb-error__title' }, 'Блок не отрисован')),
    h('div', { class: 'rb-error__type' }, `Тип: ${type}`),
    h('pre', { class: 'rb-error__msg' }, String((err && err.message) || err || 'неизвестная ошибка')));
}

function unknownCard(type) {
  return h('div', { class: 'rb-unknown' },
    h('div', { class: 'rb-unknown__title' }, 'Неизвестный тип блока'),
    h('code', {}, String(type)));
}

function slugId(text, i) {
  return `rb-sec-${i}`;
}

/** Только заголовки, реально видимые в потоке (верхний уровень + колонки). */
function collectHeadings(blocks, out = [], depth = 0) {
  if (!Array.isArray(blocks) || depth > 3) return out;
  for (const b of blocks) {
    if (!b || typeof b !== 'object') continue;
    if (b.type === 'heading' && (Number(b.level) || 2) <= 2) out.push(b);
    else if (b.type === 'columns' && Array.isArray(b.cols)) {
      for (const col of b.cols) collectHeadings(col, out, depth + 1);
    }
  }
  return out;
}

/* ---------- публичный API ---------- */

/**
 * Нарисовать отчёт в контейнере.
 * @param {HTMLElement} el контейнер
 * @param {object} spec {title,subtitle,summary,blocks}
 * @param {{fileUrl?:string}} [ctx] базовый URL для относительных картинок/ссылок
 * @returns {HTMLElement} корневой элемент отчёта
 */
export function renderReport(el, spec, ctx = {}) {
  if (!el) throw new Error('renderReport: нет контейнера');
  ensureReportStyles();
  const data = spec && typeof spec === 'object' ? spec : {};
  const blocks = Array.isArray(data.blocks) ? data.blocks : [];
  el.textContent = '';

  const headingIds = new WeakMap();
  const headings = collectHeadings(blocks);
  headings.forEach((b, i) => headingIds.set(b, slugId(b.text, i)));

  const baseCtx = {
    fileUrl: ctx.fileUrl || '',
    ...ctx,
    resolveSrc: makeResolver(ctx.fileUrl || ''),
    headingId: (b) => headingIds.get(b) || '',
  };

  /** Общий рендер массива блоков — используется в т.ч. вложенными блоками. */
  const renderBlocks = (list) => {
    const frag = document.createDocumentFragment();
    for (const block of Array.isArray(list) ? list : []) {
      const type = block && block.type ? String(block.type) : 'unknown';
      const wrap = h('div', { class: 'rb-block', dataset: { block: type } });
      try {
        const fn = registry.get(type);
        if (!fn) wrap.appendChild(unknownCard(type));
        else {
          const node = fn(block, baseCtx);
          if (node instanceof Node) wrap.appendChild(node);
          else if (node !== null && node !== undefined) wrap.appendChild(h('div', {}, String(node)));
        }
      } catch (err) {
        wrap.textContent = '';
        wrap.classList.add('rb-block--error');
        wrap.appendChild(blockErrorCard(type, err));
        if (typeof console !== 'undefined') console.error('[report] блок', type, err);
      }
      frag.appendChild(wrap);
    }
    return frag;
  };
  baseCtx.renderBlocks = renderBlocks;

  const root = h('article', { class: 'report' });

  /* ---- шапка ---- */
  const header = h('header', { class: 'report__head' });
  const headingBox = h('div', { class: 'report__heading' });
  if (data.title) headingBox.appendChild(h('h1', { class: 'report__title' }, String(data.title)));
  if (data.subtitle) headingBox.appendChild(h('p', { class: 'report__subtitle' }, String(data.subtitle)));
  if (data.summary) {
    // Единственный путь HTML на весь отчёт — общий с блоками (report/md-node.js).
    headingBox.appendChild(mdNode(data.summary, baseCtx, 'report__summary rb-md'));
  }
  header.appendChild(headingBox);

  const actions = h('div', { class: 'report__actions' });
  const printBtn = h('button', {
    type: 'button', class: 'rb-btn report__print', title: 'Печать или сохранение в PDF',
    onclick: () => window.print(),
  }, 'Печать / PDF');
  actions.appendChild(printBtn);
  header.appendChild(actions);
  root.appendChild(header);

  /* ---- оглавление + блоки ---- */
  const layout = h('div', { class: 'report__layout' });
  const toc = h('aside', { class: 'report__toc', 'aria-label': 'Оглавление отчёта' });
  if (headings.length > 1) {
    toc.appendChild(h('div', { class: 'report__toc-title' }, 'Содержание'));
    const nav = h('nav', { class: 'report__toc-nav' });
    const ul = h('ul');
    for (const b of headings) {
      const id = headingIds.get(b);
      const level = Number(b.level) || 2;
      ul.appendChild(h('li', { class: `report__toc-item report__toc-item--${level}` },
        h('a', { href: `#${id}` }, String(b.text ?? ''))));
    }
    nav.appendChild(ul);
    toc.appendChild(nav);
  } else {
    toc.classList.add('is-empty');
  }
  layout.appendChild(toc);

  const main = h('div', { class: 'report__main' });
  if (blocks.length) main.appendChild(renderBlocks(blocks));
  else main.appendChild(h('p', { class: 'report__empty' }, 'В отчёте нет блоков.'));
  layout.appendChild(main);
  root.appendChild(layout);

  el.appendChild(root);

  // печать: раскрыть свёрнутые блоки, чтобы в PDF попало всё
  // (typeof window — чтобы renderReport можно было гонять в тестах без DOM)
  if (!el.__rbPrintBound && typeof window !== 'undefined') {
    el.__rbPrintBound = true;
    window.addEventListener('beforeprint', () => {
      el.querySelectorAll('details').forEach((d) => { d.open = true; });
    });
  }
  return root;
}

export default renderReport;
