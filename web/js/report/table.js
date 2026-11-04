// Блок «table»: сортировка, фильтр, итоги, пагинация, CSV, ARIA.
// Чистая логика — в table-model.js (тестируется в node без DOM).

import { h, debounce } from '../util.js';
import {
  sortRows, filterRows, computeTotals, formatTotal, formatValue,
  pageRows, toCSV, statusTone, isBlank, PAGE_LIMIT,
} from './table-model.js';

const MAX_ROWS = 5000;

function alignOf(col) {
  if (col.align) return col.align;
  return ['number', 'money', 'percent'].includes(col.type) ? 'right' : 'left';
}

function downloadCsv(name, text) {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function safeFileName(title, fallback = 'table') {
  const base = String(title || fallback).trim().replace(/[\\/:*?"<>|]+/g, '-').replace(/\s+/g, ' ').slice(0, 80);
  return `${base || fallback}.csv`;
}

function linkCell(value) {
  const obj = value && typeof value === 'object' ? value : { href: String(value ?? '') };
  const href = String(obj.href || obj.url || obj.path || '');
  const label = String(obj.title || obj.label || href);
  if (!href) return h('span', {}, label);
  if (/^(https?:|mailto:)/i.test(href)) {
    return h('a', { class: 'rb-link', href, target: '_blank', rel: 'noopener noreferrer' }, label);
  }
  return h('a', { class: 'rb-link', href: '#', dataset: { path: href }, title: href }, label);
}

function cell(value, col) {
  if (isBlank(value)) return h('td', { class: 'rb-td rb-td--empty', 'aria-label': 'пусто' }, '—');
  const td = h('td', { class: `rb-td rb-td--${col.type || 'text'}`, style: col.align ? { textAlign: col.align } : null });
  if (col.type === 'status') {
    td.appendChild(h('span', { class: 'rb-chip', dataset: { tone: statusTone(value) } }, String(value)));
  } else if (col.type === 'link') {
    td.appendChild(linkCell(value));
  } else {
    td.textContent = formatValue(value, col);
  }
  return td;
}

/**
 * @param {object} spec {title,columns,rows,sortable,filter,totals,footnote,pageSize,density}
 * @param {{fileUrl?:string}} ctx
 * @returns {HTMLElement}
 */
export function renderTable(spec, ctx = {}) {
  const columns = Array.isArray(spec.columns) ? spec.columns.filter((c) => c && c.key) : [];
  if (!columns.length) throw new Error('таблица без колонок');
  const allRows = Array.isArray(spec.rows) ? spec.rows.slice(0, MAX_ROWS) : [];
  const truncated = Array.isArray(spec.rows) && spec.rows.length > MAX_ROWS;
  const pageSize = Math.max(20, Number(spec.pageSize) || PAGE_LIMIT);
  const sortable = spec.sortable !== false;
  // SPEC 4.5: filter по умолчанию включается при 25+ строках
  const showFilter = spec.filter === true || (spec.filter !== false && allRows.length >= 25);

  const state = {
    query: '',
    sortKey: spec.sortKey || null,
    sortDir: spec.sortDir === 'desc' ? 'desc' : 'asc',
    shown: pageSize,
    density: spec.density === 'compact' ? 'compact' : 'normal',
  };

  const root = h('section', {
    class: 'rb-table',
    dataset: { density: state.density },
  });

  /* ---- шапка ---- */
  const head = h('header', { class: 'rb-table__head' });
  if (spec.title) head.appendChild(h('h3', { class: 'rb-table__title' }, String(spec.title)));

  const tools = h('div', { class: 'rb-table__tools' });
  const countEl = h('span', { class: 'rb-table__count', role: 'status', 'aria-live': 'polite' });
  let filterInput = null;
  if (showFilter) {
    filterInput = h('input', {
      type: 'search',
      class: 'rb-input rb-table__filter',
      placeholder: 'Фильтр по всем колонкам',
      'aria-label': 'Фильтр по таблице',
      autocomplete: 'off',
      oninput: debounce((e) => { state.query = e.target.value; state.shown = pageSize; paint(); }, 160),
    });
    tools.appendChild(filterInput);
  }
  tools.appendChild(countEl);

  const densityBtn = h('button', {
    type: 'button', class: 'rb-btn rb-btn--ghost rb-table__density', title: 'Плотность строк',
    'aria-label': 'Переключить плотность строк',
    onclick: () => {
      state.density = state.density === 'compact' ? 'normal' : 'compact';
      root.dataset.density = state.density;
      densityBtn.textContent = state.density === 'compact' ? 'Обычно' : 'Плотно';
    },
  }, 'Плотно');
  tools.appendChild(densityBtn);

  const csvBtn = h('button', {
    type: 'button', class: 'rb-btn rb-btn--ghost', title: 'Скачать видимые строки в CSV',
    onclick: () => {
      const view = currentView();
      downloadCsv(safeFileName(spec.title), toCSV(columns, view.rows));
    },
  }, 'CSV');
  tools.appendChild(csvBtn);
  head.appendChild(tools);
  root.appendChild(head);

  /* ---- таблица ---- */
  const scroll = h('div', { class: 'rb-table__scroll' });
  const table = h('table', { class: 'rb-table__grid', 'aria-label': String(spec.title || 'Таблица отчёта') });
  const thead = h('thead');
  const headRow = h('tr');
  const ths = [];

  for (const col of columns) {
    const th = h('th', {
      scope: 'col',
      class: `rb-th rb-th--${col.type || 'text'}`,
      style: { textAlign: alignOf(col) },
    });
    const label = String(col.label ?? col.key);
    if (sortable) {
      const btn = h('button', {
        type: 'button',
        class: 'rb-th__btn',
        dataset: { key: col.key },
        onclick: () => {
          if (state.sortKey === col.key) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
          else { state.sortKey = col.key; state.sortDir = 'asc'; }
          state.shown = pageSize;
          paint();
        },
      }, label, h('span', { class: 'rb-th__arrow', 'aria-hidden': 'true' }));
      th.appendChild(btn);
    } else {
      th.textContent = label;
    }
    ths.push({ col, th });
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = h('tbody');
  table.appendChild(tbody);
  const tfoot = h('tfoot');
  table.appendChild(tfoot);
  scroll.appendChild(table);
  root.appendChild(scroll);

  const foot = h('div', { class: 'rb-table__foot' });
  const more = h('div', { class: 'rb-table__more' });
  foot.appendChild(more);
  root.appendChild(foot);

  if (spec.footnote) root.appendChild(h('p', { class: 'rb-table__note' }, String(spec.footnote)));
  if (truncated) {
    root.appendChild(h('p', { class: 'rb-table__note rb-table__note--warn' },
      `Показаны первые ${MAX_ROWS} строк из ${spec.rows.length}.`));
  }

  /* ---- вычисления ---- */
  function currentView() {
    let rows = filterRows(allRows, columns, state.query);
    if (state.sortKey) rows = sortRows(rows, columns, { key: state.sortKey, dir: state.sortDir });
    return { rows };
  }

  function renderBody(view) {
    tbody.textContent = '';
    tfoot.textContent = '';
    const page = pageRows(view.rows, state.shown);
    if (!page.rows.length) {
      tbody.appendChild(h('tr', { class: 'rb-table__empty-row' },
        h('td', { colspan: String(columns.length), class: 'rb-table__empty' },
          state.query ? 'Ничего не найдено — измените фильтр.' : 'Нет данных.')));
    }
    const frag = document.createDocumentFragment();
    for (const row of page.rows) {
      const tr = h('tr');
      for (const col of columns) tr.appendChild(cell(row?.[col.key], col));
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);

    // итоги (по отфильтрованным строкам)
    const totals = computeTotals(view.rows, columns, spec.totals);
    const keys = Object.keys(totals);
    if (keys.length) {
      const tr = h('tr', { class: 'rb-table__totals' });
      let first = true;
      for (const col of columns) {
        const entry = totals[col.key];
        const td = h('td', { class: 'rb-td', style: { textAlign: alignOf(col) } });
        if (first) { td.textContent = 'Итого'; first = false; }
        else if (entry) td.textContent = formatTotal(entry, col);
        tr.appendChild(td);
      }
      tfoot.appendChild(tr);
    }

    more.textContent = '';
    if (page.hasMore) {
      const left = page.total - page.rows.length;
      more.appendChild(h('button', {
        type: 'button', class: 'rb-btn',
        onclick: () => { state.shown += pageSize; paint(); },
      }, `Показать ещё ${Math.min(pageSize, left)}`));
      if (page.total <= MAX_ROWS) {
        more.appendChild(h('button', {
          type: 'button', class: 'rb-btn rb-btn--ghost',
          onclick: () => { state.shown = page.total; paint(); },
        }, `Все ${page.total}`));
      }
    }
    return page;
  }

  function paint() {
    const view = currentView();
    const page = renderBody(view);
    countEl.textContent = view.rows.length === allRows.length
      ? `${view.rows.length}`
      : `${view.rows.length} из ${allRows.length}`;
    for (const { col, th } of ths) {
      const active = state.sortKey === col.key;
      th.setAttribute('aria-sort', active ? (state.sortDir === 'asc' ? 'ascending' : 'descending') : 'none');
      th.classList.toggle('is-sorted', active);
      const arrow = th.querySelector('.rb-th__arrow');
      if (arrow) arrow.textContent = active ? (state.sortDir === 'asc' ? '↑' : '↓') : '';
    }
    root.classList.toggle('is-empty', page.total === 0);
  }

  paint();
  return root;
}

export default renderTable;