// charts-draw.js — отрисовка графиков (SVG). Вызывается из charts.js.
// Математика — charts-math.js, общие кирпичи (палитра, форматтер, <svg>) —
// charts-primitives.js. Так файл держится в пределах ~600 строк (§10.5).

import { h, s, clamp } from '../util.js';
import {
  niceTicks,
  makeScale,
  bandScale,
  stackSeries,
  pieArcs,
  arcPath,
  polar,
  linePath,
  seriesExtent,
  scatterExtent,
  indexTicks,
  charsForWidth,
  stepDecimals,
} from './charts-math.js';
import {
  px, COLORS, resolveColor, makeFormatter, isFiniteNumber,
  emptyBox, svgText, labelWithTitle, svgRoot,
} from './charts-primitives.js';

// Публичный набор charts-draw.js остался прежним: charts.js и тесты импортируют
// палитру/форматтер/px именно отсюда.
export * from './charts-primitives.js';
export { clamp };

const KIND_RU = {
  bar: 'Столбчатая диаграмма',
  hbar: 'Горизонтальная диаграмма',
  line: 'Линейный график',
  area: 'График с областью',
  pie: 'Круговая диаграмма',
  donut: 'Кольцевая диаграмма',
  stacked: 'Столбчатая диаграмма с накоплением',
  scatter: 'Точечная диаграмма',
};
let seq = 0;

/* ------------------------------- cartesian ------------------------------- */

function drawCartesian(model, width, ui) {
  const { kind, labels, series, fmt, spec } = model;
  const horizontal = kind === 'hbar';
  const stacked = kind === 'stacked';
  const isLine = kind === 'line' || kind === 'area';
  const visible = series
    .map((sr, i) => ({ ...sr, i }))
    .filter((sr) => !ui.hidden.has(sr.i));
  const height = horizontal
    ? clamp(84 + labels.length * 30, 190, 460)
    : width < 520
      ? 250
      : 310;

  if (!visible.length) {
    return { el: emptyBox(), legend: series, height };
  }
  const titleId = `rc-t-${(seq += 1)}`;
  const descId = `rc-d-${(seq += 1)}`;

  const values = [];
  let stack = null;
  if (stacked) {
    stack = stackSeries(visible.map((sr) => ({ data: sr.data })));
    values.push(stack.min, stack.max);
  } else {
    visible.forEach((sr) =>
      sr.data.forEach((v) => {
        if (isFiniteNumber(v)) values.push(Number(v));
      }),
    );
  }
  let lo = Math.min(...values, 0);
  let hi = Math.max(...values, 0);
  if (isLine && lo >= 0 && lo > hi * 0.45) lo = Math.min(...values);
  const ticks = niceTicks(lo, hi, height > 270 ? 5 : 4);
  const tickDecimals = Math.min(2, stepDecimals(ticks.step));

  const tickLabels = ticks.ticks.map((t) => fmt(t, tickDecimals));
  const maxTickChars = tickLabels.reduce((m, t) => Math.max(m, t.length), 3);
  const labelCol = horizontal
    ? clamp(labels.reduce((m, l) => Math.max(m, String(l).length), 3) * 7 + 18, 84, width * 0.4)
    : 0;
  const margin = {
    top: 16,
    right: horizontal ? 58 : 18,
    bottom: 40,
    left: horizontal ? labelCol : clamp(maxTickChars * 6.6 + 14, 40, 96),
  };
  const plot = {
    x0: margin.left,
    y0: margin.top,
    x1: width - margin.right,
    y1: height - margin.bottom,
  };
  if (plot.x1 - plot.x0 < 40) plot.x1 = plot.x0 + 40;

  const title = spec.title || KIND_RU[kind];
  const svg = svgRoot(width, height, titleId, descId, {
    title,
    text: `${KIND_RU[kind]}${spec.title ? ` «${spec.title}»` : ''}: ${labels.length} категорий, ${visible.length} серий.`,
  });
  ui.svg = svg;

  const xScale = makeScale(ticks.min, ticks.max, plot.x0, plot.x1);
  const yScale = makeScale(ticks.min, ticks.max, plot.y1, plot.y0);
  const cats = labels.length;
  const band = bandScale(cats, horizontal ? plot.y0 : plot.x0, horizontal ? plot.y1 : plot.x1, 0.26);

  // сетка и деления
  const grid = s('g', { class: 'rc-grid', 'aria-hidden': 'true' });
  const axisLabels = s('g', { class: 'rc-axis', 'aria-hidden': 'true' });
  ticks.ticks.forEach((t) => {
    if (horizontal) {
      const x = xScale(t);
      grid.append(s('line', { x1: px(x), y1: px(plot.y0), x2: px(x), y2: px(plot.y1) }));
      axisLabels.append(svgText(x, plot.y1 + 20, fmt(t, tickDecimals), { 'text-anchor': 'middle' }));
    } else {
      const y = yScale(t);
      grid.append(s('line', { x1: px(plot.x0), y1: px(y), x2: px(plot.x1), y2: px(y) }));
      axisLabels.append(svgText(plot.x0 - 8, y + 4, fmt(t, tickDecimals), { 'text-anchor': 'end' }));
    }
  });
  svg.append(grid);

  // нулевая линия
  const zeroInRange = ticks.min < 0 && ticks.max > 0;
  if (zeroInRange) {
    const z = horizontal ? xScale(0) : yScale(0);
    svg.append(
      s('line', {
        class: 'rc-zero',
        x1: horizontal ? px(z) : px(plot.x0),
        y1: horizontal ? px(plot.y0) : px(z),
        x2: horizontal ? px(z) : px(plot.x1),
        y2: horizontal ? px(plot.y1) : px(z),
      }),
    );
  }

  const bars = s('g', { class: 'rc-bars' });
  const lines = s('g', { class: 'rc-lines' });
  const valueLabels = s('g', { class: 'rc-values', 'aria-hidden': 'true' });

  const fmtVal = (v) => fmt(v, Math.min(2, stepDecimals(Math.abs(ticks.step) || 1)), false);
  const perBar = band.step / Math.max(1, visible.length);
  const showValues =
    !isLine &&
    width > 430 &&
    cats * visible.length <= 24 &&
    (horizontal ? perBar >= 22 : perBar >= (visible.length > 1 ? 62 : 50));

  if (stacked) {
    const bw = band.band;
    visible.forEach((sr, si) => {
      const pts = stack.points[si];
      const g = s('g', { class: 'rc-series', 'data-series': sr.name });
      pts.forEach((p, i) => {
        const yTop = yScale(Math.max(p.y0, p.y1));
        const yBot = yScale(Math.min(p.y0, p.y1));
        const hgt = Math.max(1, yBot - yTop);
        const rect = s('rect', {
          class: 'rc-bar rc-anim',
          x: px(band.start(i)),
          y: px(yTop),
          width: px(bw),
          height: px(hgt),
          rx: Math.min(3, bw / 6),
          style: `--rc-i:${i};--rc-delay:${Math.min(12, si * 2 + i)}ms;fill:${sr.color}`,
        });
        g.append(rect);
        if (si === visible.length - 1 && showValues && Math.abs(p.y1 - p.y0) > 0) {
          valueLabels.append(
            svgText(band.center(i), p.y1 >= 0 ? yTop - 5 : yBot + 13, fmtVal(p.y1), {
              'text-anchor': 'middle',
              class: 'rc-value',
                          }),
          );
        }
      });
      bars.append(g);
    });
  } else if (!isLine) {
    const groupW = band.band / Math.max(1, visible.length);
    visible.forEach((sr, si) => {
      const g = s('g', { class: 'rc-series', 'data-series': sr.name });
      sr.data.forEach((raw, i) => {
        if (i >= cats) return;
        const v = Number(raw);
        if (!isFiniteNumber(raw)) return;
        const zero = horizontal ? xScale(Math.max(ticks.min, Math.min(ticks.max, 0))) : yScale(clamp(0, ticks.min, ticks.max));
        const val = horizontal ? xScale(clamp(v, ticks.min, ticks.max)) : yScale(clamp(v, ticks.min, ticks.max));
        const rect = s('rect', {
          class: 'rc-bar rc-anim',
          x: px(horizontal ? Math.min(zero, val) : band.start(i) + si * groupW + groupW * 0.08),
          y: px(horizontal ? band.start(i) + groupW * 0.08 : Math.min(zero, val)),
          width: px(horizontal ? Math.max(1, Math.abs(val - zero)) : groupW * 0.84),
          height: px(horizontal ? groupW * 0.84 : Math.max(1, Math.abs(val - zero))),
          rx: Math.min(3, groupW / 6),
          style: `--rc-i:${i};--rc-delay:${Math.min(12, si * 2 + i)}ms;fill:${sr.color}`,
        });
        g.append(rect);
        if (showValues) {
          if (horizontal) {
            const tx = Math.max(plot.x0 + 4, Math.min(plot.x1 + margin.right - 8, val + (v >= 0 ? 6 : -6)));
            valueLabels.append(
              svgText(tx, band.center(i) + 4, fmtVal(v), {
                'text-anchor': v >= 0 ? 'start' : 'end',
                class: 'rc-value',
                              }),
            );
          } else {
            valueLabels.append(
              svgText(band.center(i) + si * groupW + groupW / 2, val + (v >= 0 ? -6 : 13), fmtVal(v), {
                'text-anchor': 'middle',
                class: 'rc-value',
                              }),
            );
          }
        }
      });
      bars.append(g);
    });
  } else {
    visible.forEach((sr) => {
      const pts = [];
      sr.data.forEach((raw, i) => {
        if (!isFiniteNumber(raw) || i >= cats) {
          pts.push(null);
          return;
        }
        const v = clamp(Number(raw), ticks.min, ticks.max);
        pts.push({ x: xScalePoint(i, cats, plot), y: yScale(v), v: Number(raw) });
      });
      const line = linePath(pts, { smooth: !(kind === 'line' && cats < 3) });
      if (kind === 'area' && pts.filter(Boolean).length) {
        const base = yScale(clamp(0, ticks.min, ticks.max));
        const valid = pts.filter(Boolean);
        const areaD = `${line} L ${px(valid[valid.length - 1].x)} ${px(base)} L ${px(valid[0].x)} ${px(base)} Z`;
        lines.append(
          s('path', { class: 'rc-area rc-anim', d: areaD, style: `--rc-i:0;fill:${sr.color}` }),
        );
      }
      const pathEl = s('path', {
        class: 'rc-line rc-anim',
        d: line,
        fill: 'none',
        style: `--rc-i:${sr.colorIndex};--rc-delay:0ms;stroke:${sr.color}`,
      });
      lines.append(pathEl);
      if (cats <= 40) {
        const dots = s('g', { class: 'rc-dots' });
        pts.forEach((p) => {
          if (!p) return;
          dots.append(s('circle', { cx: px(p.x), cy: px(p.y), r: 3.2, style: `fill:${sr.color}` }));
        });
        lines.append(dots);
      }
    });
  }

  svg.append(bars, lines);

  // подписи категорий
  const axisX = s('g', { class: 'rc-xaxis', 'aria-hidden': 'true' });
  const maxCatChars = Math.max(3, charsForWidth(horizontal ? plot.x1 - plot.x0 : band.step - 4, 11) - 1);
  const catIndexes = horizontal ? labels.map((_, i) => i) : indexTicks(cats, Math.max(2, Math.floor((plot.x1 - plot.x0) / 56)));
  catIndexes.forEach((i) => {
    const label = labels[i] ?? `#${i + 1}`;
    if (horizontal) {
      const maxChars = Math.max(4, Math.floor((margin.left - 22) / 6.7));
      axisX.append(
        labelWithTitle(plot.x0 - 10, band.center(i) + 4, label, maxChars, {
          'text-anchor': 'end',
          class: 'rc-cat',
        }),
      );
    } else {
      axisX.append(
        labelWithTitle(band.center(i), plot.y1 + 18, label, Math.min(maxCatChars, 18), {
          'text-anchor': 'middle',
          class: 'rc-cat',
        }),
      );
    }
  });
  svg.append(axisLabels, axisX);

  // ось
  svg.append(
    s('line', {
      class: 'rc-axisline',
      x1: px(plot.x0),
      y1: px(plot.y1),
      x2: px(plot.x1),
      y2: px(plot.y1),
    }),
  );

  // интерактивные зоны + подсказки
  const hits = s('g', { class: 'rc-hits' });
  if (stacked || !isLine) {
    for (let i = 0; i < cats; i += 1) {
      const rows = visible.map((sr) => {
        const v = Number(sr.data[i]);
        return { color: sr.color, name: sr.name, value: isFiniteNumber(sr.data[i]) ? fmtVal(v) : '—' };
      });
      const payload = { title: labels[i] ?? `#${i + 1}`, rows };
      const hit = s('rect', {
        class: 'rc-hit',
        x: px(horizontal ? plot.x0 : band.start(i) - 2),
        y: px(horizontal ? band.start(i) : plot.y0),
        width: px(horizontal ? plot.x1 - plot.x0 : band.band + 4),
        height: px(horizontal ? band.band : plot.y1 - plot.y0),
        tabindex: '0',
        role: 'button',
        'aria-label': `${labels[i] ?? i + 1}: ${rows.map((r) => `${r.name} ${r.value}`).join(', ')}`,
      });
      bindHover(hit, ui.tip, ui.frame, payload);
      hits.append(hit);
    }
  } else {
    visible.forEach((sr) => {
      sr.data.forEach((raw, i) => {
        if (!isFiniteNumber(raw) || i >= cats) return;
        const p = { x: xScalePoint(i, cats, plot), y: yScale(clamp(Number(raw), ticks.min, ticks.max)) };
        const payload = {
          title: labels[i] ?? `#${i + 1}`,
          rows: [{ color: sr.color, name: sr.name, value: fmtVal(Number(raw)) }],
        };
        const hit = s('circle', {
          class: 'rc-hit rc-hit--dot',
          cx: px(p.x),
          cy: px(p.y),
          r: 9,
          tabindex: '0',
          role: 'button',
          'aria-label': `${labels[i] ?? i + 1}, ${sr.name}: ${fmtVal(Number(raw))}`,
        });
        bindHover(hit, ui.tip, ui.frame, payload);
        hits.append(hit);
      });
    });
  }
  svg.append(hits);
  svg.append(valueLabels);

  return { el: svg, height, legend: series };
}

function xScalePoint(i, count, plot) {
  if (count <= 1) return (plot.x0 + plot.x1) / 2;
  return plot.x0 + (i / (count - 1)) * (plot.x1 - plot.x0);
}

/* --------------------------------- scatter -------------------------------- */

function drawScatter(model, width, ui) {
  const { series, labels, fmt } = model;
  const visible = series.map((sr, i) => ({ ...sr, i })).filter((sr) => !ui.hidden.has(sr.i));
  const height = width < 520 ? 260 : 330;
  const all = visible.flatMap((sr) =>
    sr.data.filter((p) => p && typeof p === 'object' && isFiniteNumber(p.x) && isFiniteNumber(p.y)),
  );
  if (!all.length) return { el: emptyBox(), height, legend: series };
  const ext = scatterExtent(visible);
  const xt = niceTicks(ext.xMin, ext.xMax, 5);
  const yt = niceTicks(ext.yMin, ext.yMax, 5);
  const xd = Math.min(2, stepDecimals(xt.step));
  const yd = Math.min(2, stepDecimals(yt.step));
  const titleId = `rc-t-${(seq += 1)}`;
  const descId = `rc-d-${(seq += 1)}`;
  const title = model.spec.title || KIND_RU.scatter;
  const svg = svgRoot(width, height, titleId, descId, {
    title,
    text: `${KIND_RU.scatter}${model.spec.title ? ` «${model.spec.title}»` : ''}: ${all.length} точек, ${visible.length} серий.`,
  });
  ui.svg = svg;
  const plot = { x0: 54, y0: 16, x1: width - 20, y1: height - 42 };
  const x = makeScale(xt.min, xt.max, plot.x0, plot.x1);
  const y = makeScale(yt.min, yt.max, plot.y1, plot.y0);
  const grid = s('g', { class: 'rc-grid', 'aria-hidden': 'true' });
  const ax = s('g', { class: 'rc-axis', 'aria-hidden': 'true' });
  xt.ticks.forEach((t) => {
    grid.append(s('line', { x1: px(x(t)), y1: px(plot.y0), x2: px(x(t)), y2: px(plot.y1) }));
    ax.append(svgText(x(t), plot.y1 + 18, fmt(t, xd), { 'text-anchor': 'middle' }));
  });
  yt.ticks.forEach((t) => {
    grid.append(s('line', { x1: px(plot.x0), y1: px(y(t)), x2: px(plot.x1), y2: px(y(t)) }));
    ax.append(svgText(plot.x0 - 8, y(t) + 4, fmt(t, yd), { 'text-anchor': 'end' }));
  });
  svg.append(grid, ax, s('line', { class: 'rc-axisline', x1: px(plot.x0), y1: px(plot.y1), x2: px(plot.x1), y2: px(plot.y1) }));
  const g = s('g', { class: 'rc-points' });
  const hits = s('g', { class: 'rc-hits' });
  const labelsG = s('g', { class: 'rc-values', 'aria-hidden': 'true' });
  const showLabels = all.length <= 14;
  visible.forEach((sr, si) => {
    sr.data.forEach((p, i) => {
      if (!p || typeof p !== 'object' || !isFiniteNumber(p.x) || !isFiniteNumber(p.y)) return;
      const cx = x(Number(p.x));
      const cy = y(Number(p.y));
      g.append(
        s('circle', {
          class: 'rc-point rc-anim',
          cx: px(cx),
          cy: px(cy),
          r: 5,
          style: `--rc-i:${Math.min(20, i)};--rc-delay:${Math.min(10, si * 2)}ms;fill:${sr.color}`,
        }),
      );
      const label = String(p.label || `${p.x}; ${p.y}`);
      if (showLabels && p.label) {
        labelsG.append(
          labelWithTitle(cx + 8, cy + 4, String(p.label), 16, {
            'text-anchor': 'start',
            class: 'rc-value',
                      }),
        );
      }
      const hit = s('circle', {
        class: 'rc-hit rc-hit--dot',
        cx: px(cx),
        cy: px(cy),
        r: 10,
        tabindex: '0',
        role: 'button',
        'aria-label': `${sr.name}: ${label}`,
      });
      bindHover(hit, ui.tip, ui.frame, {
        title: sr.name,
        rows: [
          { color: sr.color, name: 'x', value: fmt(Number(p.x), 2) },
          { color: sr.color, name: 'y', value: fmt(Number(p.y), 2) },
          ...(p.label ? [{ name: 'метка', value: String(p.label) }] : []),
        ],
      });
      hits.append(hit);
    });
  });
  svg.append(g, labelsG, hits);
  return { el: svg, height, legend: series };
}

/* ----------------------------------- pie ---------------------------------- */

function drawPie(model, width, ui) {
  const { kind, labels, series, fmt, spec } = model;
  const height = clamp(width * 0.62, 240, 340);
  const src = series[0] || { data: [], color: COLORS[0] };
  const items = labels.map((label, i) => ({
    label,
    value: Math.max(0, Number(src.data[i]) || 0),
    color: COLORS[i % COLORS.length],
  }));
  const shown = items
    .map((it, i) => ({ ...it, i }))
    .filter((it) => !ui.hidden.has(it.i) && it.value > 0);
  const total = shown.reduce((a, b) => a + b.value, 0);
  if (!shown.length || total <= 0) return { el: emptyBox(), height, legend: items, pie: true };
  const arcs = pieArcs(shown.map((s) => s.value));
  const titleId = `rc-t-${(seq += 1)}`;
  const descId = `rc-d-${(seq += 1)}`;
  const title = spec.title || KIND_RU[kind];
  const svg = svgRoot(width, height, titleId, descId, {
    title,
    text: `${KIND_RU[kind]}${spec.title ? ` «${spec.title}»` : ''}: ${shown.length} долей, всего ${fmt(total, 0, true)}.`,
  });
  ui.svg = svg;
  const cx = width / 2;
  const cy = height / 2;
  const outer = Math.min(width, height) / 2 - (width > 420 ? 54 : 18);
  const inner = kind === 'donut' ? outer * 0.58 : 0;
  const g = s('g', { class: 'rc-pie' });
  const labelsG = s('g', { class: 'rc-pie-labels', 'aria-hidden': 'true' });
  arcs.forEach((arc, ai) => {
    const it = shown[ai];
    const d = arcPath(cx, cy, inner, outer, arc.start, arc.end);
    const path = s('path', {
      class: 'rc-slice rc-anim',
      d,
      style: `--rc-i:${ai};--rc-delay:${Math.min(12, ai)}ms;fill:${it.color}`,
      tabindex: '0',
      role: 'button',
      'aria-label': `${it.label}: ${fmt(it.value, 0, true)}, ${Math.round(arc.frac * 100)} %`,
    });
    bindHover(path, ui.tip, ui.frame, {
      title: it.label,
      rows: [
        { color: it.color, name: 'Значение', value: fmt(it.value, Number.isInteger(it.value) ? 0 : 2, true) },
        { color: it.color, name: 'Доля', value: `${Math.round(arc.frac * 100)} %` },
      ],
    });
    g.append(path);
    if (arc.frac >= 0.045 && width > 380) {
      const a = arc.mid;
      const p1 = polar(cx, cy, outer + 2, a);
      const p2 = polar(cx, cy, outer + 9, a);
      const p3 = polar(cx, cy, outer + 16, a);
      labelsG.append(
        s('polyline', {
          class: 'rc-leader',
          points: `${px(p1.x)},${px(p1.y)} ${px(p2.x)},${px(p2.y)} ${px(p3.x)},${px(p3.y)}`,
        }),
      );
      const right = Math.cos(a) >= 0;
      labelsG.append(
        svgText(p3.x + (right ? 3 : -3), p3.y + 4, `${Math.round(arc.frac * 100)} %`, {
          'text-anchor': right ? 'start' : 'end',
          class: 'rc-pie-pct',
        }),
      );
    }
  });
  svg.append(g);
  if (kind === 'donut') {
    svg.append(
      svgText(cx, cy - 2, fmt(total, 0, false), {
        'text-anchor': 'middle',
        class: 'rc-donut-total',
              }),
      svgText(cx, cy + 16, 'всего', { 'text-anchor': 'middle', class: 'rc-donut-sub' }),
    );
  }
  svg.append(labelsG);
  return { el: svg, height, legend: items, pie: true };
}


function showTip(tip, frame, payload, client) {
  if (!payload) {
    tip.hidden = true;
    tip.replaceChildren();
    return;
  }
  const rows = (payload.rows || []).filter(Boolean);
  tip.replaceChildren(
    h('div', { class: 'rc-tip__title' }, payload.title || ''),
    ...rows.map((row) =>
      h(
        'div',
        { class: 'rc-tip__row' },
        row.color
          ? h('span', { class: 'rc-tip__swatch', style: `background:${row.color}`, 'aria-hidden': 'true' })
          : null,
        h('span', { class: 'rc-tip__name' }, row.name),
        h('span', { class: 'rc-tip__val' }, row.value),
      ),
    ),
  );
  tip.hidden = false;
  const rect = frame.getBoundingClientRect();
  const tw = tip.offsetWidth || 160;
  const th = tip.offsetHeight || 48;
  const x = Number.isFinite(client.x) ? client.x - rect.left : rect.width / 2;
  const y = Number.isFinite(client.y) ? client.y - rect.top : 0;
  const left = clamp(x, tw / 2 + 4, Math.max(tw / 2 + 4, rect.width - tw / 2 - 4));
  let top = y - th - 12;
  if (top < 4) top = y + 20;
  tip.style.left = `${px(left)}px`;
  tip.style.top = `${px(top)}px`;
}

function bindHover(el, tip, frame, payload) {
  const at = (evt) => {
    if (evt && Number.isFinite(evt.clientX) && evt.type !== 'focus') {
      return { x: evt.clientX, y: evt.clientY };
    }
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + 2 };
  };
  el.addEventListener('pointerenter', (evt) => showTip(tip, frame, typeof payload === 'function' ? payload() : payload, at(evt)));
  el.addEventListener('pointermove', (evt) => showTip(tip, frame, typeof payload === 'function' ? payload() : payload, at(evt)));
  el.addEventListener('pointerleave', () => showTip(tip, frame, null));
  el.addEventListener('focus', (evt) => showTip(tip, frame, typeof payload === 'function' ? payload() : payload, at(evt)));
  el.addEventListener('blur', () => showTip(tip, frame, null));
}

/* -------------------------------- sr-таблица ------------------------------- */

/** Диспетчер отрисовки по виду графика. */
export function drawChart(model, width, ui) {
  if (model.kind === 'pie' || model.kind === 'donut') return drawPie(model, width, ui);
  if (model.kind === 'scatter') return drawScatter(model, width, ui);
  return drawCartesian(model, width, ui);
}
