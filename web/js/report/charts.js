// charts.js — рендер графиков чистым SVG (без зависимостей).
// Контракт: renderChart(spec, ctx) -> Element
// spec = {kind, title?, x?: string[], series:[{name, data, color?}], unit?, note?}

import { h } from '../util.js';
import { COLORS, drawChart, emptyBox, makeFormatter, isFiniteNumber, resolveColor } from './charts-draw.js';

const KINDS = ['bar', 'hbar', 'line', 'area', 'pie', 'donut', 'stacked', 'scatter'];

function normalize(spec = {}) {
  const kind = KINDS.includes(spec.kind) ? spec.kind : 'bar';
  let labels = Array.isArray(spec.x) ? spec.x.map((x) => String(x ?? '')) : [];
  const series = (Array.isArray(spec.series) ? spec.series : []).map((sr, i) => ({
    name: String((sr && sr.name) || `Ряд ${i + 1}`),
    data: Array.isArray(sr && sr.data) ? sr.data : [],
    color: resolveColor(sr && sr.color, i),
    colorIndex: i,
  }));
  const maxLen = series.reduce((m, sr) => Math.max(m, sr.data.length), 0);
  if (!labels.length && maxLen) labels = Array.from({ length: maxLen }, (_, i) => `#${i + 1}`);
  const hasData =
    kind === 'scatter'
      ? series.some((sr) =>
          sr.data.some((p) => p && typeof p === 'object' && isFiniteNumber(p.x) && isFiniteNumber(p.y)),
        )
      : kind === 'pie' || kind === 'donut'
        ? series.some((sr) => sr.data.some((v) => Number(v) > 0))
        : series.some((sr) => sr.data.some((v) => isFiniteNumber(v)));
  return { kind, labels, series, hasData, spec, unit: spec.unit || '', fmt: makeFormatter(spec.unit || '') };
}

/** Кнопки легенды: клик/Enter переключают серию. */
function buildLegend(items, hidden, onToggle) {
  const wrap = h('div', { class: 'rc-legend', role: 'group', 'aria-label': 'Серии' });
  items.forEach((item, i) => {
    const btn = h(
      'button',
      {
        type: 'button',
        class: 'rc-legend__item',
        'aria-pressed': hidden.has(i) ? 'false' : 'true',
        style: `--rc-swatch:${item.color}`,
        onclick: () => onToggle(i),
      },
      h('span', { class: 'rc-legend__dot', 'aria-hidden': 'true' }),
      h('span', { class: 'rc-legend__label' }, item.name),
    );
    wrap.append(btn);
  });
  return wrap;
}

function buildTable(model, hidden) {
  const { kind, labels, series, fmt, spec } = model;
  const visible = series.filter((_, i) => !hidden.has(i));
  const caption = spec.title ? `Данные: ${spec.title}` : 'Данные графика';
  if (kind === 'scatter') {
    const rows = visible.flatMap((sr) =>
      sr.data
        .filter((p) => p && typeof p === 'object')
        .map((p) => h('tr', {}, h('th', { scope: 'row' }, String(p.label || sr.name)), h('td', {}, fmt(p.x, 2)), h('td', {}, fmt(p.y, 2)))),
    );
    return h(
      'table',
      {},
      h('caption', {}, caption),
      h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Точка'), h('th', { scope: 'col' }, 'X'), h('th', { scope: 'col' }, 'Y'))),
      h('tbody', {}, ...rows),
    );
  }
  const head = h(
    'tr',
    {},
    h('th', { scope: 'col' }, kind === 'pie' || kind === 'donut' ? 'Доля' : 'Категория'),
    ...(kind === 'pie' || kind === 'donut'
      ? [h('th', { scope: 'col' }, 'Значение')]
      : visible.map((sr) => h('th', { scope: 'col' }, sr.name))),
  );
  const body = labels.map((label, i) =>
    h(
      'tr',
      {},
      h('th', { scope: 'row' }, label),
      ...(kind === 'pie' || kind === 'donut'
        ? [h('td', {}, fmt(series[0] && series[0].data[i], 2))]
        : visible.map((sr) => h('td', {}, fmt(sr.data[i], 2)))),
    ),
  );
  return h('table', {}, h('caption', {}, caption), h('thead', {}, head), h('tbody', {}, ...body));
}

/* --------------------------------- публичное ------------------------------- */

/**
 * Отрисовывает график по spec.
 * @param {object} spec chart{kind,title?,x?,series,unit?,note?}
 * @param {object} ctx {fileUrl}
 * @returns {HTMLElement}
 */
export function renderChart(spec = {}, ctx = {}) {
  const model = normalize(spec);
  const fig = h('figure', {
    class: `rc rc-chart rc-chart--${model.kind}`,
    'data-kind': model.kind,
    'data-file': ctx && ctx.fileUrl ? String(ctx.fileUrl) : undefined,
  });
  if (spec.title || spec.unit) {
    fig.append(
      h(
        'figcaption',
        { class: 'rc-chart__head' },
        spec.title ? h('span', { class: 'rc-chart__title' }, String(spec.title)) : null,
        spec.unit ? h('span', { class: 'rc-chart__unit' }, String(spec.unit)) : null,
      ),
    );
  }
  const frame = h('div', { class: 'rc-chart__frame' });
  const tip = h('div', { class: 'rc-tip', hidden: '', 'aria-hidden': 'true' });
  frame.append(tip);
  fig.append(frame);
  const legendHost = h('div', { class: 'rc-legend-host' });
  fig.append(legendHost);
  const tableHost = h('div', { class: 'rc-table-host' });
  fig.append(tableHost);
  if (spec.note) fig.append(h('p', { class: 'rc-note' }, String(spec.note)));

  const hidden = new Set();
  let lastWidth = 0;
  let renderCount = 0;
  let timer = 0;

  const render = (width) => {
    const w = Math.max(240, Math.round(width || 720));
    lastWidth = w;
    const hideLimit = model.kind === 'pie' || model.kind === 'donut' ? model.labels.length : model.series.length;
    hidden.forEach((i) => {
      if (i >= hideLimit) hidden.delete(i);
    });
    frame.replaceChildren(tip);
    tip.hidden = true;
    tip.replaceChildren();
    legendHost.replaceChildren();
    tableHost.replaceChildren();
    const ui = { frame, tip, hidden, svg: null };
    const animate = renderCount === 0;
    fig.classList.toggle('rc-no-anim', !animate);
    let out;
    if (!model.hasData) out = { el: emptyBox(), height: 0, legend: [] };
    else out = drawChart(model, w, ui);
    frame.append(out.el);
    const legendItems = (out.legend || []).map((it, i) => ({
      name: it.name || it.label || `#${i + 1}`,
      color: it.color || COLORS[i % COLORS.length],
    }));
    if (legendItems.length > 1 || (out.pie && legendItems.length > 0)) {
      const legend = buildLegend(legendItems, hidden, (i) => {
        if (hidden.has(i)) hidden.delete(i);
        else hidden.add(i);
        render(lastWidth);
      });
      if (out.pie) legend.classList.add('rc-legend--pie');
      legendHost.append(legend);
    }
    if (model.hasData) tableHost.append(h('div', { class: 'rc-sr' }, buildTable(model, hidden)));
    renderCount += 1;
  };

  render(720);
  const schedule = (w) => {
    lastWidth = w;
    clearTimeout(timer);
    timer = setTimeout(() => render(w), 70);
  };
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (!w || Math.abs(w - lastWidth) < 12) return;
      if (!lastWidth) render(w);
      else schedule(w);
    });
    ro.observe(fig);
  }
  return fig;
}

export default renderChart;
