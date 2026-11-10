// graph.js — граф связей на чистом SVG: layered | force | radial.
// Контракт: renderGraph(spec, ctx) -> Element
// spec = {title?, layout?, direction?, nodes:[{id,label,group?,note?,size?}],
//         edges:[{from,to,label?,kind?:solid|dashed,weight?}]}

import { h, s, clamp } from '../util.js';
import { layoutGraph, edgeGeometry, edgePoint } from './graph-layout.js';

const FALLBACK = ['#4c7dff', '#e07a3f', '#2f9e6a', '#9a6ad6', '#c4a02a', '#2f9fb5', '#cf5c86', '#6c7a8c'];
const COLORS = FALLBACK.map((c, i) => `var(--chart-${i + 1}, ${c})`);
const LAYOUT_RU = { layered: 'послойная', force: 'силовая', radial: 'радиальная' };
let seq = 0;
const px = (n) => Math.round(Number(n) * 100) / 100;
const asArray = (v) => (Array.isArray(v) ? v : []);

function colorForGroup(group, groups) {
  const i = Math.max(0, groups.indexOf(group));
  return COLORS[i % COLORS.length];
}

function emptyBox() {
  return h(
    'div',
    { class: 'rc-empty', role: 'note' },
    h('span', { class: 'rc-empty__mark', 'aria-hidden': 'true' }, '—'),
    h('span', {}, 'Нет данных о связях'),
  );
}

/**
 * Отрисовывает граф связей.
 * @param {object} spec graph{title?,layout?,direction?,nodes,edges}
 * @param {object} ctx {fileUrl}
 * @returns {HTMLElement}
 */
export function renderGraph(spec = {}, ctx = {}) {
  const nodes = asArray(spec.nodes).filter((n) => n && n.id != null);
  const edges = asArray(spec.edges).filter(
    (e) => e && e.from != null && e.to != null && nodes.some((n) => String(n.id) === String(e.from)) && nodes.some((n) => String(n.id) === String(e.to)),
  );
  const kind = ['layered', 'force', 'radial'].includes(spec.layout) ? spec.layout : 'layered';

  const fig = h('figure', {
    class: `rc rc-graph rc-graph--${kind}`,
    'data-layout': kind,
    'data-file': ctx && ctx.fileUrl ? String(ctx.fileUrl) : undefined,
  });
  if (spec.title) {
    fig.append(
      h(
        'figcaption',
        { class: 'rc-graph__head' },
        h('span', { class: 'rc-graph__title' }, String(spec.title)),
        h('span', { class: 'rc-graph__hint' }, `${LAYOUT_RU[kind]} · ${nodes.length} узлов · ${edges.length} связей`),
      ),
    );
  }
  if (!nodes.length) {
    fig.append(emptyBox());
    return fig;
  }

  const groups = [...new Set(nodes.map((n) => String(n.group ?? 'Прочее')))];
  const idOf = (n) => String(n.id);
  const labelOf = (n) => String(n.label ?? n.id);
  const neighbors = new Map(nodes.map((n) => [idOf(n), new Set()]));
  edges.forEach((e) => {
    const a = String(e.from);
    const b = String(e.to);
    if (neighbors.has(a)) neighbors.get(a).add(b);
    if (neighbors.has(b)) neighbors.get(b).add(a);
  });

  const canvas = h('div', { class: 'rc-graph__canvas' });
  const tools = h(
    'div',
    { class: 'rc-graph__tools', role: 'group', 'aria-label': 'Масштаб' },
    h('button', { type: 'button', class: 'rc-graph__btn', 'aria-label': 'Приблизить', title: 'Приблизить' }, '+'),
    h('button', { type: 'button', class: 'rc-graph__btn', 'aria-label': 'Отдалить', title: 'Отдалить' }, '−'),
    h('button', { type: 'button', class: 'rc-graph__btn rc-graph__btn--fit', 'aria-label': 'Вписать граф', title: 'Вписать' }, 'вписать'),
  );
  canvas.append(tools);
  const panel = h('aside', { class: 'rc-graph__panel', hidden: '', 'aria-live': 'polite' });
  const body = h('div', { class: 'rc-graph__body' }, canvas, panel);
  fig.append(body);

  const legendHost = h('div', { class: 'rc-graph__legend', role: 'list', 'aria-label': 'Группы' });
  if (groups.length > 1) {
    groups.forEach((g) => {
      legendHost.append(
        h(
          'span',
          { class: 'rc-graph__group', role: 'listitem' },
          h('span', { class: 'rc-graph__dot', style: `background:${colorForGroup(g, groups)}`, 'aria-hidden': 'true' }),
          g,
        ),
      );
    });
  }
  fig.append(legendHost);
  if (spec.note) fig.append(h('p', { class: 'rc-note' }, String(spec.note)));

  let state = null;
  let ro = 0;
  let lastWidth = 0;

  const render = (width) => {
    panel.hidden = true;
    panel.replaceChildren();
    const W = Math.max(320, Math.round(width || 720));
    const isTB = kind === 'layered' && spec.direction === 'TB';
    const H = Math.round(isTB ? clamp(W * 0.72, 460, 720) : clamp(W * 0.62, 340, 560));
    const layoutOptions = {
      fontSize: 12,
      maxChars: 16,
      maxWidth: 205,
      nodeGap: 18,
      layerGap: isTB ? 64 : 76,
      ...(spec.layoutOptions || {}),
    };
    const layout = layoutGraph(
      { ...spec, nodes, edges, layout: kind, layoutOptions },
      { width: W, height: H },
    );
    const pos = layout.positions;
    if (!pos || !pos.size) {
      canvas.replaceChildren(tools, emptyBox());
      return;
    }
    const titleId = `rc-gt-${(seq += 1)}`;
    const descId = `rc-gd-${(seq += 1)}`;
    const svg = s(
      'svg',
      {
        class: 'rc-graph__svg',
        viewBox: `0 0 ${W} ${H}`,
        width: '100%',
        height: px(H),
        role: 'group',
        'aria-labelledby': `${titleId} ${descId}`,
      },
      s('title', { id: titleId }, spec.title || 'Граф связей'),
      s(
        'desc',
        { id: descId },
        `${spec.title || 'Граф связей'}: ${nodes.length} узлов, ${edges.length} связей, раскладка ${LAYOUT_RU[kind]}.`,
      ),
      s(
        'defs',
        {},
        s(
          'marker',
          {
            id: `rc-arrow-${seq}`,
            viewBox: '0 0 10 10',
            refX: '9',
            refY: '5',
            markerWidth: '6',
            markerHeight: '6',
            orient: 'auto-start-reverse',
          },
          s('path', { d: 'M 0 0 L 10 5 L 0 10 z', class: 'rc-arrow' }),
        ),
        s(
          'marker',
          {
            id: `rc-arrow-on-${seq}`,
            viewBox: '0 0 10 10',
            refX: '9',
            refY: '5',
            markerWidth: '7',
            markerHeight: '7',
            orient: 'auto-start-reverse',
          },
          s('path', { d: 'M 0 0 L 10 5 L 0 10 z', class: 'rc-arrow rc-arrow--on' }),
        ),
      ),
    );
    const viewport = s('g', { class: 'rc-graph__viewport' });
    svg.append(viewport);

    const edgeG = s('g', { class: 'rc-edges' });
    const edgeRefs = [];
    // bbox подписей узлов + уже поставленных подписей рёбер: подпись не должна
    // лечь ни на узел, ни на другую подпись (F-10 — раньше проверялись только узлы).
    const boxList = [...pos.values()].map((p) => ({ x: p.x, y: p.y, w: p.w, h: p.h }));
    const labelWidth = (text) => String(text).length * 4.7 + 6;
    /** Свободное место под подпись: возвращает её bbox или null. */
    const freeSpot = (pt, text, dy = 0) => {
      const w = labelWidth(text);
      const box = { x: pt.x - w / 2, y: pt.y - 12 + dy, w, h: 14 };
      if (box.x < 2 || box.y < 2 || box.x + box.w > W - 2 || box.y + box.h > H - 2) return null;
      const hit = boxList.some(
        (b) =>
          box.x < b.x + b.w + 3 &&
          box.x + box.w > b.x - 3 &&
          box.y < b.y + b.h + 3 &&
          box.y + box.h > b.y - 3,
      );
      return hit ? null : box;
    };
    const markerId = `rc-arrow-${seq}`;
    const markerOn = `rc-arrow-on-${seq}`;
    edges.forEach((e) => {
      const a = pos.get(String(e.from));
      const b = pos.get(String(e.to));
      if (!a || !b) return;
      const geo = edgeGeometry(a, b, { curved: kind !== 'radial' });
      const weight = clamp(Number(e.weight) || 1, 1, 4);
      const path = s('path', {
        class: `rc-edge${e.kind === 'dashed' ? ' rc-edge--dashed' : ''}`,
        d: geo.d,
        'marker-end': `url(#${markerId})`,
        style: `--rc-w:${px(1 + (weight - 1) * 0.7)}`,
        'data-from': String(e.from),
        'data-to': String(e.to),
      });
      if (e.label) path.append(s('title', {}, String(e.label)));
      edgeG.append(path);
      let labelEl = null;
      const edgeLen = Math.hypot(geo.x2 - geo.x1, geo.y2 - geo.y1);
      if (e.label && edgeLen > 46) {
        let spot = null;
        let spotBox = null;
        for (const t of [0.5, 0.36, 0.64, 0.26, 0.74, 0.15, 0.85]) {
          for (const dy of [0, -9, 9]) {
            const q = edgePoint(geo, t);
            const box = freeSpot(q, e.label, dy);
            if (box) { spot = { x: q.x, y: q.y + dy }; spotBox = box; break; }
          }
          if (spot) break;
        }
        if (spot) {
          labelEl = s(
            'text',
            { class: 'rc-edge__label', x: px(spot.x), y: px(spot.y - 4), 'text-anchor': 'middle' },
            String(e.label).length > 22 ? `${String(e.label).slice(0, 21)}…` : String(e.label),
          );
          edgeG.append(labelEl);
          boxList.push(spotBox);
        }
      }
      edgeRefs.push({ path, labelEl, from: String(e.from), to: String(e.to), markerId, markerOn });
    });
    viewport.append(edgeG);

    const nodeG = s('g', { class: 'rc-nodes' });
    const nodeRefs = new Map();
    nodes.forEach((n, i) => {
      const id = idOf(n);
      const p = pos.get(id);
      if (!p) return;
      const group = String(n.group ?? 'Прочее');
      const color = colorForGroup(group, groups);
      const lines = p.lines || [labelOf(n)];
      const g = s('g', {
        class: 'rc-node',
        'data-id': id,
        tabindex: '0',
        role: 'button',
        'aria-label': `${labelOf(n)}${group ? `, группа ${group}` : ''}, связей ${neighbors.get(id)?.size || 0}`,
      });
      g.append(
        s('rect', {
          class: 'rc-node__box',
          x: px(p.x),
          y: px(p.y),
          width: px(p.w),
          height: px(p.h),
          rx: 9,
          ry: 9,
          style: `fill:${color};stroke:${color}`,
        }),
      );
      const startY = p.cy - ((lines.length - 1) * 16) / 2 + 4;
      lines.forEach((line, li) => {
        const t = s(
          'text',
          { class: 'rc-node__label', x: px(p.cx), y: px(startY + li * 16), 'text-anchor': 'middle' },
          line,
        );
        if (line !== labelOf(n)) t.append(s('title', {}, labelOf(n)));
        g.append(t);
      });
      g.append(s('title', {}, n.note ? `${labelOf(n)}\n${n.note}` : labelOf(n)));
      nodeG.append(g);
      nodeRefs.set(id, { g, box: p, color, group, node: n, index: i });
    });
    viewport.append(nodeG);

    const W2 = W;
    const H2 = H;
    const view = { k: 1, tx: 0, ty: 0 };
    const apply = () => {
      viewport.setAttribute('transform', `translate(${px(view.tx)} ${px(view.ty)}) scale(${px(view.k)})`);
    };
    const fit = () => {
      const pad = 26;
      const lw = Math.max(1, layout.width);
      const lh = Math.max(1, layout.height);
      const k = clamp(Math.min((W2 - pad * 2) / lw, (H2 - pad * 2) / lh), 0.18, 1.35);
      view.k = k;
      view.tx = (W2 - lw * k) / 2;
      view.ty = (H2 - lh * k) / 2;
      apply();
    };
    const zoomAt = (factor, sx, sy) => {
      const k2 = clamp(view.k * factor, 0.18, 5);
      if (k2 === view.k) return;
      view.tx = sx - ((sx - view.tx) / view.k) * k2;
      view.ty = sy - ((sy - view.ty) / view.k) * k2;
      view.k = k2;
      apply();
    };
    fit();

    // ---- подсветка соседей ----
    const clearClasses = () => {
      nodeRefs.forEach((ref) => {
        ref.g.classList.remove('is-active', 'is-neighbor', 'is-dim');
      });
      edgeRefs.forEach((ref) => {
        ref.path.classList.remove('is-active', 'is-dim');
        ref.path.setAttribute('marker-end', `url(#${ref.markerId})`);
        if (ref.labelEl) ref.labelEl.classList.remove('is-on');
      });
    };
    const highlight = (id) => {
      clearClasses();
      if (!id) return;
      const near = neighbors.get(id) || new Set();
      nodeRefs.forEach((ref, nid) => {
        if (nid === id) ref.g.classList.add('is-active');
        else if (near.has(nid)) ref.g.classList.add('is-neighbor');
        else ref.g.classList.add('is-dim');
      });
      edgeRefs.forEach((ref) => {
        if (ref.from === id || ref.to === id) {
          ref.path.classList.add('is-active');
          ref.path.setAttribute('marker-end', `url(#${ref.markerOn})`);
          if (ref.labelEl) ref.labelEl.classList.add('is-on');
        } else ref.path.classList.add('is-dim');
      });
    };

    let selected = null;
    let hovered = null;
    const sync = () => highlight(hovered || selected);

    const closePanel = () => {
      selected = null;
      panel.hidden = true;
      panel.replaceChildren();
      sync();
    };
    const selectNode = (id) => {
      const ref = nodeRefs.get(id);
      if (!ref) return;
      selected = id;
      hovered = null;
      panel.hidden = false;
      const connections = edgeRefs
        .filter((e) => e.from === id || e.to === id)
        .map((e) => {
          const otherId = e.from === id ? e.to : e.from;
          const other = nodes.find((n) => idOf(n) === otherId);
          return { dir: e.from === id ? '→' : '←', otherId, otherLabel: other ? labelOf(other) : otherId, edgeLabel: e.label || '' };
        });
      const head = h(
        'div',
        { class: 'rc-graph__panel-head' },
        h('span', { class: 'rc-graph__dot', style: `background:${ref.color}`, 'aria-hidden': 'true' }),
        h('h4', { class: 'rc-graph__panel-title' }, labelOf(ref.node)),
        h('button', { type: 'button', class: 'rc-graph__panel-close', 'aria-label': 'Закрыть' }, '×'),
      );
      head.querySelector('button').addEventListener('click', closePanel);
      const meta = h('div', { class: 'rc-graph__panel-meta' }, `Группа: ${ref.group}`);
      const note = ref.node.note
        ? h('p', { class: 'rc-graph__panel-note' }, String(ref.node.note))
        : h('p', { class: 'rc-graph__panel-note rc-graph__panel-note--muted' }, 'Без описания');
      const list = h('div', { class: 'rc-graph__panel-links' });
      list.append(h('div', { class: 'rc-graph__panel-sub' }, `Связи (${connections.length})`));
      if (!connections.length) list.append(h('div', { class: 'rc-graph__panel-empty' }, 'Связей нет'));
      connections.forEach((c) => {
        const btn = h(
          'button',
          { type: 'button', class: 'rc-graph__link' },
          h('span', { class: 'rc-graph__link-dir', 'aria-hidden': 'true' }, c.dir),
          h('span', { class: 'rc-graph__link-name' }, c.otherLabel),
          c.edgeLabel ? h('span', { class: 'rc-graph__link-kind' }, c.edgeLabel) : null,
        );
        btn.addEventListener('click', () => {
          selectNode(c.otherId);
          const p = pos.get(c.otherId);
          if (p) {
            view.tx = W2 / 2 - p.cx * view.k;
            view.ty = H2 / 2 - p.cy * view.k;
            apply();
          }
          const target = nodeRefs.get(c.otherId);
          if (target) target.g.focus();
        });
        list.append(btn);
      });
      panel.replaceChildren(head, meta, note, list);
      panel.hidden = false;
      sync();
    };

    nodeRefs.forEach((ref, id) => {
      const g = ref.g;
      g.addEventListener('pointerenter', () => {
        hovered = id;
        sync();
      });
      g.addEventListener('pointerleave', () => {
        hovered = null;
        sync();
      });
      g.addEventListener('focus', () => {
        hovered = id;
        sync();
      });
      g.addEventListener('blur', () => {
        hovered = null;
        sync();
      });
      g.addEventListener('click', (evt) => {
        evt.stopPropagation();
        if (selected === id) closePanel();
        else selectNode(id);
      });
      g.addEventListener('keydown', (evt) => {
        if (evt.key === 'Enter' || evt.key === ' ') {
          evt.preventDefault();
          if (selected === id) closePanel();
          else selectNode(id);
        }
        if (evt.key === 'Escape') closePanel();
      });
    });

    // ---- пан / зум ----
    let drag = null;
    svg.addEventListener('pointerdown', (evt) => {
      if (evt.button !== 0) return;
      if (evt.target.closest && evt.target.closest('.rc-node')) return;
      drag = { x: evt.clientX, y: evt.clientY, tx: view.tx, ty: view.ty };
      svg.setPointerCapture?.(evt.pointerId);
      svg.classList.add('is-panning');
    });
    svg.addEventListener('pointermove', (evt) => {
      if (!drag) return;
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? W2 / rect.width : 1;
      view.tx = drag.tx + (evt.clientX - drag.x) * scale;
      view.ty = drag.ty + (evt.clientY - drag.y) * scale;
      apply();
    });
    const endDrag = () => {
      drag = null;
      svg.classList.remove('is-panning');
    };
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);
    svg.addEventListener('wheel', (evt) => {
      evt.preventDefault();
      const rect = svg.getBoundingClientRect();
      const sx = ((evt.clientX - rect.left) / Math.max(1, rect.width)) * W2;
      const sy = ((evt.clientY - rect.top) / Math.max(1, rect.height)) * H2;
      zoomAt(Math.exp(-evt.deltaY * 0.0016), sx, sy);
    }, { passive: false });
    svg.addEventListener('click', (evt) => {
      if (evt.target === svg || (evt.target.classList && evt.target.classList.contains('rc-graph__viewport'))) closePanel();
    });

    const [plus, minus, fitBtn] = tools.querySelectorAll('button');
    plus.onclick = () => zoomAt(1.25, W2 / 2, H2 / 2);
    minus.onclick = () => zoomAt(0.8, W2 / 2, H2 / 2);
    fitBtn.onclick = () => {
      fit();
      closePanel();
    };

    canvas.replaceChildren(tools, svg);
    state = { ids: [...nodeRefs.keys()], selectNode, closePanel };
  };

  render(760);
  if (typeof ResizeObserver !== 'undefined') {
    const obs = new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (!w || Math.abs(w - lastWidth) < 40) return;
      lastWidth = w;
      clearTimeout(ro);
      ro = setTimeout(() => render(w), 120);
    });
    obs.observe(fig);
  }
  fig._rcGraph = () => state;
  return fig;
}

export default renderGraph;
