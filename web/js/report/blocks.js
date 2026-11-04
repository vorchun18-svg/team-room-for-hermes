// Блоки отчёта, кроме table/chart/graph. Каждый рендерер: (spec, ctx) => Node.
// Вложенные блоки (tabs/accordion/columns) рендерит общий ctx.renderBlocks.

import { h, fmtDate } from '../util.js';
import { mdNode } from './md-node.js';

const TONES = new Set(['info', 'success', 'warn', 'danger']);
const ICONS = { info: 'i', success: '✓', warn: '!', danger: '×' };
const STATUSES = new Set(['done', 'active', 'next', 'blocked']);

/**
 * Markdown-абзац блока. HTML ставится только через mdNode() — единственный
 * разрешённый путь innerHTML в проекте (см. report/md-node.js).
 * @param {*} src
 * @param {object} ctx
 * @param {string} [cls]
 * @returns {HTMLElement}
 */
function mdDiv(src, ctx, cls = 'rb-md') {
  return mdNode(src, ctx, cls);
}

function dateSafe(v) {
  if (v === null || v === undefined || v === '') return '';
  try { const out = fmtDate(v); return out || String(v); } catch { return String(v); }
}

function statusOf(v) { return STATUSES.has(v) ? v : 'next'; }

/* ---------- heading / text / callout / stats ---------- */

/**
 * Заголовок блока.
 * @param {object} spec {text, level}
 * @returns {HTMLElement}
 */
export function heading(spec, ctx) {
  const level = Math.min(3, Math.max(1, Number(spec.level) || 2));
  const tag = `h${Math.min(6, level + 1)}`;
  const id = (ctx.headingId && ctx.headingId(spec)) || undefined;
  return h(tag, { class: `rb-heading rb-heading--${level}`, id }, String(spec.text ?? ''));
}

/**
 * Абзац markdown.
 * @param {object} spec {md}
 * @param {object} ctx контекст рендера (fileUrl)
 * @returns {HTMLElement}
 */
export function text(spec, ctx) {
  return mdDiv(spec.md, ctx, 'rb-text rb-md');
}

/**
 * Выноска с тоном info|success|warn|danger.
 * @param {object} spec {tone, title, md}
 * @param {object} ctx
 * @returns {HTMLElement}
 */
export function callout(spec, ctx) {
  const tone = TONES.has(spec.tone) ? spec.tone : 'info';
  const box = h('aside', { class: 'rb-callout', dataset: { tone }, role: tone === 'danger' || tone === 'warn' ? 'note' : 'note' });
  box.appendChild(h('span', { class: 'rb-callout__icon', 'aria-hidden': 'true' }, ICONS[tone]));
  const body = h('div', { class: 'rb-callout__body' });
  if (spec.title) body.appendChild(h('p', { class: 'rb-callout__title' }, String(spec.title)));
  if (spec.md) body.appendChild(mdDiv(spec.md, ctx));
  box.appendChild(body);
  return box;
}

/**
 * Сетка KPI-карточек.
 * @param {object} spec {items:[{label,value,delta,hint,tone}]}
 * @returns {HTMLElement}
 */
export function stats(spec) {
  const items = Array.isArray(spec.items) ? spec.items : [];
  const grid = h('div', { class: 'rb-stats' });
  for (const it of items) {
    const card = h('div', { class: 'rb-stat', dataset: it.tone ? { tone: String(it.tone) } : null });
    card.appendChild(h('div', { class: 'rb-stat__label' }, String(it.label ?? '')));
    card.appendChild(h('div', { class: 'rb-stat__value' }, String(it.value ?? '')));
    if (it.delta !== undefined && it.delta !== null && it.delta !== '') {
      const numeric = typeof it.delta === 'number';
      const dir = numeric ? (it.delta > 0 ? 'up' : it.delta < 0 ? 'down' : 'flat') : 'flat';
      const shown = numeric
        ? (it.delta > 0 ? '+' : '') + it.delta.toLocaleString('ru-RU', { maximumFractionDigits: 2 })
        : String(it.delta);
      card.appendChild(h('div', { class: 'rb-stat__delta', dataset: { dir } }, shown));
    }
    if (it.hint) card.appendChild(h('div', { class: 'rb-stat__hint' }, String(it.hint)));
    grid.appendChild(card);
  }
  return grid;
}

/* ---------- timeline / phases ---------- */

/**
 * Лента событий.
 * @param {object} spec {items:[{date,title,owner,md,status}]}
 * @param {object} ctx
 * @returns {HTMLElement}
 */
export function timeline(spec, ctx) {
  const ol = h('ol', { class: 'rb-timeline' });
  for (const it of Array.isArray(spec.items) ? spec.items : []) {
    const li = h('li', { class: 'rb-timeline__item', dataset: { status: statusOf(it.status) } });
    li.appendChild(h('span', { class: 'rb-timeline__dot', 'aria-hidden': 'true' }));
    const body = h('div', { class: 'rb-timeline__body' });
    const meta = h('div', { class: 'rb-timeline__meta' });
    if (it.date) meta.appendChild(h('time', { class: 'rb-timeline__date', datetime: String(it.date) }, dateSafe(it.date)));
    if (it.owner) meta.appendChild(h('span', { class: 'rb-timeline__owner' }, String(it.owner)));
    if (meta.childNodes.length) body.appendChild(meta);
    if (it.title) body.appendChild(h('div', { class: 'rb-timeline__title' }, String(it.title)));
    if (it.md) body.appendChild(mdDiv(it.md, ctx, 'rb-timeline__md rb-md'));
    li.appendChild(body);
    ol.appendChild(li);
  }
  return ol;
}

/**
 * Полоса этапов проекта.
 * @param {object} spec {items:[{title,note,status}]}
 * @returns {HTMLElement}
 */
export function phases(spec) {
  const wrap = h('div', { class: 'rb-phases', role: 'list' });
  for (const it of Array.isArray(spec.items) ? spec.items : []) {
    const st = statusOf(it.status);
    const step = h('div', { class: 'rb-phase', dataset: { status: st }, role: 'listitem' });
    step.appendChild(h('div', { class: 'rb-phase__rail', 'aria-hidden': 'true' },
      h('span', { class: 'rb-phase__dot' })));
    step.appendChild(h('div', { class: 'rb-phase__title' }, String(it.title ?? '')));
    if (it.note) step.appendChild(h('div', { class: 'rb-phase__note' }, String(it.note)));
    wrap.appendChild(step);
  }
  return wrap;
}

/* ---------- compare / matrix ---------- */

/**
 * Сравнение вариантов (плюсы/минусы/оценка).
 * @param {object} spec {criteria, options:[{name,score,pros,cons,verdict}]}
 * @returns {HTMLElement}
 */
export function compare(spec) {
  const wrap = h('div', { class: 'rb-compare' });
  if (Array.isArray(spec.criteria) && spec.criteria.length) {
    const legend = h('div', { class: 'rb-compare__criteria' },
      h('span', { class: 'rb-compare__criteria-label' }, 'Критерии: '));
    for (const c of spec.criteria) legend.appendChild(h('span', { class: 'rb-chip' }, String(c)));
    wrap.appendChild(legend);
  }
  const grid = h('div', { class: 'rb-compare__grid' });
  for (const opt of Array.isArray(spec.options) ? spec.options : []) {
    const card = h('div', { class: 'rb-compare__card' });
    const top = h('div', { class: 'rb-compare__top' });
    top.appendChild(h('h4', { class: 'rb-compare__name' }, String(opt.name ?? '')));
    if (opt.score !== undefined && opt.score !== null) {
      top.appendChild(h('span', { class: 'rb-compare__score', title: 'Оценка' }, String(opt.score)));
    }
    card.appendChild(top);
    for (const [field, label, tone] of [['pros', 'Плюсы', 'ok'], ['cons', 'Минусы', 'danger']]) {
      const list = Array.isArray(opt[field]) ? opt[field] : [];
      if (!list.length) continue;
      card.appendChild(h('div', { class: `rb-compare__label rb-compare__label--${tone}` }, label));
      const ul = h('ul', { class: `rb-compare__list rb-compare__list--${tone}` });
      for (const x of list) ul.appendChild(h('li', {}, String(x)));
      card.appendChild(ul);
    }
    if (opt.verdict) card.appendChild(h('p', { class: 'rb-compare__verdict' }, String(opt.verdict)));
    grid.appendChild(card);
  }
  wrap.appendChild(grid);
  return wrap;
}

function heatValue(cellValue) {
  if (cellValue && typeof cellValue === 'object') return Number(cellValue.v ?? cellValue.value);
  return Number(cellValue);
}

function cellLabel(cellValue) {
  if (cellValue && typeof cellValue === 'object') return String(cellValue.label ?? cellValue.v ?? '');
  return cellValue === null || cellValue === undefined ? '' : String(cellValue);
}

/**
 * Тепловая матрица или SWOT.
 * @param {object} spec {kind:'heat'|'grid'|'swot', rows, cols, cells, swot}
 * @returns {HTMLElement}
 */
export function matrix(spec) {
  const kind = spec.kind || 'heat';
  if (kind === 'swot') {
    const swot = spec.swot || {};
    const wrap = h('div', { class: 'rb-swot' });
    const quads = [
      ['strengths', 'Сильные стороны', 'ok'], ['weaknesses', 'Слабые стороны', 'warn'],
      ['opportunities', 'Возможности', 'info'], ['threats', 'Угрозы', 'danger'],
    ];
    for (const [key, title, tone] of quads) {
      const box = h('div', { class: 'rb-swot__quad', dataset: { tone } });
      box.appendChild(h('div', { class: 'rb-swot__title' }, title));
      const ul = h('ul', { class: 'rb-swot__list' });
      for (const x of Array.isArray(swot[key]) ? swot[key] : []) {
        ul.appendChild(h('li', {}, String(x && typeof x === 'object' ? (x.text ?? x.title ?? '') : x)));
      }
      box.appendChild(ul);
      wrap.appendChild(box);
    }
    return wrap;
  }

  const rows = Array.isArray(spec.rows) ? spec.rows : [];
  const cols = Array.isArray(spec.cols) ? spec.cols : [];
  const cells = Array.isArray(spec.cells) ? spec.cells : [];
  const wrap = h('div', { class: `rb-matrix rb-matrix--${kind === 'grid' ? 'grid' : 'heat'}` });
  const table = h('table', { class: 'rb-matrix__table' });
  if (cols.length) {
    const hr = h('tr');
    hr.appendChild(h('th', { class: 'rb-matrix__corner', scope: 'col' }, ''));
    for (const c of cols) hr.appendChild(h('th', { scope: 'col' }, String(c)));
    table.appendChild(h('thead', {}, hr));
  }
  const nums = [];
  for (const row of cells) for (const cv of Array.isArray(row) ? row : []) {
    const n = heatValue(cv);
    if (Number.isFinite(n)) nums.push(n);
  }
  const min = nums.length ? Math.min(...nums) : 0;
  const max = nums.length ? Math.max(...nums) : 1;
  const span = max - min || 1;

  const tbody = h('tbody');
  cells.forEach((row, ri) => {
    const tr = h('tr');
    if (rows.length) tr.appendChild(h('th', { scope: 'row' }, String(rows[ri] ?? '')));
    for (const cv of Array.isArray(row) ? row : []) {
      const n = heatValue(cv);
      const td = h('td', { class: 'rb-matrix__cell' });
      if (kind !== 'grid' && Number.isFinite(n)) {
        const heat = Math.max(0, Math.min(1, (n - min) / span));
        td.style.setProperty('--rb-heat', heat.toFixed(3));
        td.dataset.heat = String(n);
      }
      td.appendChild(h('span', { class: 'rb-matrix__v' }, cellLabel(cv)));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

/* ---------- checklist / kanban ---------- */

/**
 * Чек-лист со счётчиком «N из M».
 * @param {object} spec {items:[{text,done,owner,due}]}
 * @returns {HTMLElement}
 */
export function checklist(spec) {
  const items = Array.isArray(spec.items) ? spec.items : [];
  const isDone = (v) => v === true || ['done', 'да', 'готово', 'true', '1'].includes(String(v).toLowerCase());
  const state = items.map((it) => isDone(it.done));
  const wrap = h('div', { class: 'rb-checklist' });
  const counter = h('span', { class: 'rb-checklist__count', role: 'status', 'aria-live': 'polite' });
  const list = h('ul', { class: 'rb-checklist__list' });
  const update = () => {
    const done = state.filter(Boolean).length;
    counter.textContent = `${done} из ${items.length}`;
    list.querySelectorAll('.rb-checklist__item').forEach((li, i) => {
      li.classList.toggle('is-done', state[i]);
      const box = li.querySelector('input');
      if (box) box.checked = state[i];
    });
  };
  items.forEach((it, i) => {
    const id = `rb-check-${Math.random().toString(36).slice(2, 9)}`;
    const box = h('input', {
      type: 'checkbox', class: 'rb-checklist__box', id,
      onchange: (e) => { state[i] = e.target.checked; update(); },
    });
    box.checked = state[i];
    const label = h('label', { class: 'rb-checklist__text', for: id }, String(it.text ?? ''));
    const li = h('li', { class: 'rb-checklist__item' }, box, label);
    if (it.owner) li.appendChild(h('span', { class: 'rb-checklist__owner' }, String(it.owner)));
    if (it.due) li.appendChild(h('span', { class: 'rb-checklist__due' }, dateSafe(it.due)));
    list.appendChild(li);
  });
  wrap.appendChild(h('div', { class: 'rb-checklist__head' }, counter));
  wrap.appendChild(list);
  update();
  return wrap;
}

/**
 * Мини-доска: дорожки с карточками.
 * @param {object} spec {lanes:[{title,cards:[{title,tag,note}]}]}
 * @returns {HTMLElement}
 */
export function kanban(spec) {
  const wrap = h('div', { class: 'rb-kanban' });
  for (const lane of Array.isArray(spec.lanes) ? spec.lanes : []) {
    const cards = Array.isArray(lane.cards) ? lane.cards : [];
    const col = h('div', { class: 'rb-kanban__lane' });
    col.appendChild(h('div', { class: 'rb-kanban__lane-head' },
      h('span', { class: 'rb-kanban__lane-title' }, String(lane.title ?? '')),
      h('span', { class: 'rb-kanban__lane-count' }, String(cards.length))));
    const body = h('div', { class: 'rb-kanban__cards' });
    for (const card of cards) {
      const el = h('div', { class: 'rb-kanban__card' });
      if (card.tag) el.appendChild(h('span', { class: 'rb-chip rb-chip--sm' }, String(card.tag)));
      el.appendChild(h('div', { class: 'rb-kanban__card-title' }, String(card.title ?? '')));
      if (card.note) el.appendChild(h('div', { class: 'rb-kanban__card-note' }, String(card.note)));
      body.appendChild(el);
    }
    col.appendChild(body);
    wrap.appendChild(col);
  }
  return wrap;
}

/* ---------- gallery + lightbox ---------- */

let lightboxEl = null;
function closeLightbox() {
  if (!lightboxEl) return;
  lightboxEl.remove();
  lightboxEl = null;
  document.removeEventListener('keydown', onLightboxKey);
}
function onLightboxKey(e) { if (e.key === 'Escape') closeLightbox(); }
function openLightbox(src, caption) {
  closeLightbox();
  const img = h('img', { class: 'rb-lightbox__img', src, alt: caption || '' });
  const cap = caption ? h('div', { class: 'rb-lightbox__caption' }, caption) : null;
  const box = h('div', {
    class: 'rb-lightbox', role: 'dialog', 'aria-modal': 'true', 'aria-label': caption || 'Просмотр изображения',
    onclick: (e) => { if (e.target === box || e.target.closest('.rb-lightbox__close')) closeLightbox(); },
  }, h('button', { type: 'button', class: 'rb-lightbox__close', 'aria-label': 'Закрыть' }, '×'), img, cap);
  document.body.appendChild(box);
  lightboxEl = box;
  document.addEventListener('keydown', onLightboxKey);
  box.querySelector('.rb-lightbox__close').focus();
}

/**
 * Галерея картинок с лайтбоксом.
 * @param {object} spec {images:[{src,caption}]}
 * @param {object} ctx (resolveSrc)
 * @returns {HTMLElement}
 */
export function gallery(spec, ctx) {
  const wrap = h('div', { class: 'rb-gallery' });
  for (const img of Array.isArray(spec.images) ? spec.images : []) {
    const src = (ctx.resolveSrc && ctx.resolveSrc(img.src)) || img.src;
    const caption = img.caption ? String(img.caption) : '';
    const figure = h('figure', { class: 'rb-gallery__item' });
    const el = h('img', { class: 'rb-gallery__img', src, alt: caption, loading: 'lazy' });
    el.addEventListener('error', () => figure.classList.add('is-broken'));
    const btn = h('button', {
      type: 'button', class: 'rb-gallery__btn', title: caption || 'Открыть',
      onclick: () => openLightbox(src, caption),
    }, el);
    figure.appendChild(btn);
    if (caption) figure.appendChild(h('figcaption', { class: 'rb-gallery__caption' }, caption));
    wrap.appendChild(figure);
  }
  return wrap;
}

/* ---------- code ---------- */

function copyText(t) {
  if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(t);
  const ta = h('textarea', { style: 'position:fixed;left:-9999px' });
  ta.value = t;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } finally { ta.remove(); }
  return Promise.resolve();
}

/**
 * Блок кода с кнопкой «Копировать».
 * @param {object} spec {lang, text}
 * @returns {HTMLElement}
 */
export function code(spec) {
  const wrap = h('div', { class: 'rb-code' });
  const bar = h('div', { class: 'rb-code__bar' });
  bar.appendChild(h('span', { class: 'rb-code__lang' }, String(spec.lang || 'text')));
  const btn = h('button', { type: 'button', class: 'rb-btn rb-btn--ghost rb-code__copy' }, 'Копировать');
  btn.addEventListener('click', () => {
    copyText(String(spec.text ?? '')).then(() => {
      btn.textContent = 'Скопировано';
      setTimeout(() => { btn.textContent = 'Копировать'; }, 1500);
    }).catch(() => { btn.textContent = 'Не удалось'; });
  });
  bar.appendChild(btn);
  wrap.appendChild(bar);
  const pre = h('pre', { class: 'rb-code__pre' });
  pre.appendChild(h('code', { class: `language-${spec.lang || 'text'}` }, String(spec.text ?? '')));
  wrap.appendChild(pre);
  return wrap;
}

/* ---------- tabs / accordion / columns ---------- */

/**
 * Вкладки с ленивым рендером панелей.
 * @param {object} spec {tabs:[{title,blocks}]}
 * @param {object} ctx (renderBlocks)
 * @returns {HTMLElement}
 */
export function tabs(spec, ctx) {
  const list = Array.isArray(spec.tabs) ? spec.tabs : [];
  const wrap = h('div', { class: 'rb-tabs' });
  const bar = h('div', { class: 'rb-tabs__bar', role: 'tablist' });
  const panels = h('div', { class: 'rb-tabs__panels' });
  const buttons = [];
  const panelEls = [];
  const rendered = new Set();

  const activate = (i) => {
    buttons.forEach((b, bi) => {
      const on = bi === i;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle('is-active', on);
      panelEls[bi].hidden = !on;
    });
    if (!rendered.has(i)) {
      rendered.add(i);
      panelEls[i].appendChild(ctx.renderBlocks(list[i].blocks || []));
    }
  };

  list.forEach((tab, i) => {
    const btn = h('button', {
      type: 'button', class: 'rb-tabs__tab', role: 'tab', id: `rb-tab-${i}-${Math.random().toString(36).slice(2, 7)}`,
      'aria-controls': `rb-tabpanel-${i}`,
      onclick: () => activate(i),
      onkeydown: (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const next = (i + (e.key === 'ArrowRight' ? 1 : -1) + list.length) % list.length;
        activate(next);
        buttons[next].focus();
      },
    }, String(tab.title ?? `Вкладка ${i + 1}`));
    const panel = h('div', { class: 'rb-tabs__panel', role: 'tabpanel', id: `rb-tabpanel-${i}`, 'aria-labelledby': btn.id });
    buttons.push(btn);
    panelEls.push(panel);
    bar.appendChild(btn);
    panels.appendChild(panel);
  });
  wrap.appendChild(bar);
  wrap.appendChild(panels);
  if (list.length) activate(0);
  return wrap;
}

/**
 * Аккордеон на <details>.
 * @param {object} spec {items:[{title,open,blocks}]}
 * @param {object} ctx (renderBlocks)
 * @returns {HTMLElement}
 */
export function accordion(spec, ctx) {
  const wrap = h('div', { class: 'rb-accordion' });
  for (const item of Array.isArray(spec.items) ? spec.items : []) {
    const details = h('details', { class: 'rb-accordion__item' });
    if (item.open) details.open = true;
    details.appendChild(h('summary', { class: 'rb-accordion__summary' }, String(item.title ?? '')));
    const body = h('div', { class: 'rb-accordion__body' });
    body.appendChild(ctx.renderBlocks(item.blocks || []));
    details.appendChild(body);
    wrap.appendChild(details);
  }
  return wrap;
}

/**
 * Колонки с вложенными блоками.
 * @param {object} spec {cols:[[blocks]], widths}
 * @param {object} ctx (renderBlocks)
 * @returns {HTMLElement}
 */
export function columns(spec, ctx) {
  const cols = Array.isArray(spec.cols) ? spec.cols : [];
  const widths = Array.isArray(spec.widths) ? spec.widths : null;
  const template = widths && widths.length === cols.length
    ? widths.map((w) => (typeof w === 'number' ? `${w}fr` : String(w))).join(' ')
    : '';
  const wrap = h('div', { class: 'rb-columns', style: template ? { gridTemplateColumns: template } : null });
  wrap.dataset.cols = String(cols.length || 2);
  for (const blocks of cols) {
    const col = h('div', { class: 'rb-columns__col' });
    col.appendChild(ctx.renderBlocks(Array.isArray(blocks) ? blocks : []));
    wrap.appendChild(col);
  }
  return wrap;
}

/* ---------- links / sources ---------- */

function refLink(ref, ctx) {
  const s = String(ref ?? '');
  if (!s) return null;
  if (/^(https?:|mailto:)/i.test(s)) {
    return h('a', { class: 'rb-link', href: s, target: '_blank', rel: 'noopener noreferrer' }, s);
  }
  return h('a', { class: 'rb-link', href: '#', dataset: { path: s }, title: s }, s);
}

/**
 * Список ссылок (внешние и файловые пути).
 * @param {object} spec {items:[{title,url,path,note}]}
 * @param {object} ctx
 * @returns {HTMLElement}
 */
export function links(spec, ctx) {
  const wrap = h('ul', { class: 'rb-links' });
  for (const it of Array.isArray(spec.items) ? spec.items : []) {
    const li = h('li', { class: 'rb-links__item' });
    const target = it.url || it.path;
    const title = String(it.title ?? target ?? '');
    if (target) {
      const a = /^(https?:|mailto:)/i.test(String(target))
        ? h('a', { class: 'rb-link rb-links__a', href: String(target), target: '_blank', rel: 'noopener noreferrer' }, title)
        : h('a', { class: 'rb-link rb-links__a', href: '#', dataset: { path: String(target) }, title: String(target) }, title);
      li.appendChild(a);
    } else {
      li.appendChild(h('span', { class: 'rb-links__a' }, title));
    }
    if (it.note) li.appendChild(h('span', { class: 'rb-links__note' }, String(it.note)));
    wrap.appendChild(li);
  }
  return wrap;
}

/**
 * Нумерованный список источников.
 * @param {object} spec {items:[{title,ref,note}]}
 * @param {object} ctx
 * @returns {HTMLElement}
 */
export function sources(spec, ctx) {
  const wrap = h('ol', { class: 'rb-sources' });
  for (const it of Array.isArray(spec.items) ? spec.items : []) {
    const li = h('li', { class: 'rb-sources__item' });
    li.appendChild(h('span', { class: 'rb-sources__title' }, String(it.title ?? '')));
    const link = refLink(it.ref, ctx);
    if (link) li.appendChild(h('span', { class: 'rb-sources__ref' }, link));
    if (it.note) li.appendChild(h('span', { class: 'rb-sources__note' }, String(it.note)));
    wrap.appendChild(li);
  }
  return wrap;
}

export const BLOCKS = {
  heading, text, callout, stats, timeline, phases, compare, matrix,
  checklist, kanban, gallery, code, tabs, accordion, columns, links, sources,
};

export { openLightbox, closeLightbox };