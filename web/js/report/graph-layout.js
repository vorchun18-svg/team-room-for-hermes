// graph-layout.js — чистые раскладки графа (без DOM). Используется graph.js и node-тестами.

import { clamp } from '../util.js';

/** Детерминированный PRNG (mulberry32). */
/** Детерминированный PRNG. @param {number} [seed] @returns {Function} */
export function mulberry32(seed = 1) {
  let a = Number(seed) >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Индексы графа: узлы по id, уникальные соседи, список рёбер. */
/** Индекс узлов/рёбер. @param {Array} [nodes] @param {Array} [edges] @returns {object} */
export function buildGraph(nodes = [], edges = []) {
  const ids = [];
  const byId = new Map();
  for (const n of Array.isArray(nodes) ? nodes : []) {
    if (!n || n.id == null) continue;
    const id = String(n.id);
    if (byId.has(id)) continue;
    byId.set(id, { ...n, id });
    ids.push(id);
  }
  const out = new Map(ids.map((id) => [id, []]));
  const inc = new Map(ids.map((id) => [id, []]));
  const edgeList = [];
  for (const e of Array.isArray(edges) ? edges : []) {
    if (!e) continue;
    const from = String(e.from);
    const to = String(e.to);
    if (!byId.has(from) || !byId.has(to) || from === to) continue;
    edgeList.push({ ...e, from, to });
    if (!out.get(from).includes(to)) out.get(from).push(to);
    if (!inc.get(to).includes(from)) inc.get(to).push(from);
  }
  const degree = new Map(ids.map((id) => [id, out.get(id).length + inc.get(id).length]));
  return { ids, byId, out, inc, edgeList, degree };
}

/**
 * Послойная раскладка (longest path). Циклы не вешают алгоритм: при пустой очереди
 * источников берётся оставшийся узел с минимальной входящей степенью.
 * @returns {{layers: string[][], levelOf: Map<string, number>, graph: object, cycleBroken: boolean}}
 */
/** Разбиение на слои (топосорт). @param {Array} [nodes] @param {Array} [edges] @returns {object} */
export function layeredLayers(nodes = [], edges = []) {
  const g = buildGraph(nodes, edges);
  const indeg = new Map(g.ids.map((id) => [id, 0]));
  for (const id of g.ids) indeg.set(id, g.inc.get(id).length);
  const levelOf = new Map();
  const remaining = new Set(g.ids);
  let queue = g.ids.filter((id) => indeg.get(id) === 0);
  let guard = 0;
  let cycleBroken = false;
  const cap = g.ids.length * 4 + 16;
  while (remaining.size && guard < cap) {
    guard += 1;
    if (!queue.length) {
      let best = null;
      let bestDeg = Infinity;
      for (const id of g.ids) {
        if (!remaining.has(id)) continue;
        const d = indeg.get(id);
        if (d < bestDeg) {
          bestDeg = d;
          best = id;
        }
      }
      if (!best) break;
      cycleBroken = true;
      if (!levelOf.has(best)) levelOf.set(best, 0);
      queue = [best];
    }
    const id = queue.shift();
    if (!remaining.has(id)) continue;
    remaining.delete(id);
    if (!levelOf.has(id)) levelOf.set(id, 0);
    for (const to of g.out.get(id)) {
      if (!remaining.has(to)) continue;
      indeg.set(to, Math.max(0, indeg.get(to) - 1));
      const cand = levelOf.get(id) + 1;
      if (!levelOf.has(to) || levelOf.get(to) < cand) levelOf.set(to, cand);
      if (indeg.get(to) === 0) queue.push(to);
    }
  }
  // страховка: если guard исчерпан, раскладываем остаток в последний слой
  const maxKnown = Math.max(0, ...levelOf.values());
  for (const id of g.ids) if (!levelOf.has(id)) levelOf.set(id, maxKnown + 1);

  const maxLevel = Math.max(0, ...levelOf.values());
  const layers = Array.from({ length: maxLevel + 1 }, () => []);
  g.ids.forEach((id, index) => {
    // порядок внутри слоя — исходный порядок узлов
    layers[levelOf.get(id)].push({ id, index });
  });
  return {
    layers: layers.map((layer) => layer.sort((a, b) => a.index - b.index).map((x) => x.id)),
    levelOf,
    graph: g,
    cycleBroken,
  };
}

/** Барицентрическое упорядочивание слоёв: 4 прохода вниз/вверх, детерминированно. */
/** Упорядочивание внутри слоёв. @param {Array} layers @param {object} graph @param {number} [iterations] @returns {Array} */
export function orderLayers(layers, graph, iterations = 4) {
  const pos = new Map();
  layers.forEach((layer, li) => layer.forEach((id, i) => pos.set(id, i)));
  for (let it = 0; it < iterations; it += 1) {
    const down = it % 2 === 0;
    const indexes = layers.map((_, i) => i);
    const seq = down ? indexes.slice(1) : indexes.slice(0, -1).reverse();
    for (const li of seq) {
      const ref = layers[down ? li - 1 : li + 1] || [];
      const refSet = new Set(ref);
      const score = new Map();
      layers[li].forEach((id, i) => {
        const nbrs = [...(graph.out.get(id) || []), ...(graph.inc.get(id) || [])].filter((x) =>
          refSet.has(x),
        );
        score.set(id, nbrs.length ? nbrs.reduce((a, x) => a + pos.get(x), 0) / nbrs.length : i);
      });
      layers[li].sort((a, b) => score.get(a) - score.get(b) || pos.get(a) - pos.get(b));
      layers[li].forEach((id, i) => pos.set(id, i));
    }
  }
  return layers;
}

/** Перенос длинной подписи по словам, максимум maxLines строк, с «…». */
/** Перенос длинной подписи по строкам. @param {string} text @param {number} [maxChars] @param {number} [maxLines] @returns {string[]} */
export function wrapLabel(text, maxChars = 16, maxLines = 3) {
  const max = Math.max(4, Math.floor(Number(maxChars) || 16));
  const limit = Math.max(1, Math.floor(Number(maxLines) || 1));
  const words = String(text ?? '')
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!words.length) return [''];
  const raw = [];
  let cur = '';
  for (const word of words) {
    let w = word;
    while (w.length > max) {
      if (cur) {
        raw.push(cur);
        cur = '';
      }
      raw.push(w.slice(0, max));
      w = w.slice(max);
    }
    if (!w) continue;
    if (!cur) cur = w;
    else if (cur.length + 1 + w.length <= max) cur += ` ${w}`;
    else {
      raw.push(cur);
      cur = w;
    }
  }
  if (cur) raw.push(cur);
  if (raw.length <= limit) return raw;
  const kept = raw.slice(0, limit);
  const tail = raw.slice(limit - 1).join(' ');
  const last = tail.length > max ? `${tail.slice(0, Math.max(1, max - 1)).trimEnd()}…` : tail;
  kept[limit - 1] = last;
  return kept;
}

/** Габариты узла по подписи и size (1 = обычный). */
/** Габариты узла по подписи. @param {object} [node] @param {object} [opts] @returns {{w:number,h:number,lines:string[]}} */
export function nodeBox(node = {}, opts = {}) {
  const fontSize = Number(opts.fontSize) || 12;
  const label = String(node.label ?? node.id ?? '');
  const mult = clamp(Number(node.size) || 1, 0.75, 1.7);
  const maxChars = Math.max(8, Math.round((Number(opts.maxChars) || 16) * mult));
  const lines = wrapLabel(label, maxChars, 3);
  const longest = lines.reduce((m, l) => Math.max(m, l.length), 0);
  const width = clamp(26 + longest * fontSize * 0.6, 96, Number(opts.maxWidth) || 220) * mult;
  const height = (30 + (lines.length - 1) * 16) * Math.max(0.85, mult * 0.92);
  return { w: Math.round(width), h: Math.round(height), lines, mult };
}

function withCenters(positions) {
  for (const p of positions.values()) {
    p.cx = p.x + p.w / 2;
    p.cy = p.y + p.h / 2;
  }
  return positions;
}

/**
 * Послойная (иерархическая) раскладка.
 * @param {'LR'|'TB'} direction
 */
/** Послойная раскладка. @param {Array} [nodes] @param {Array} [edges] @param {object} [opts] @returns {object} */
export function layeredLayout(nodes = [], edges = [], opts = {}) {
  const direction = opts.direction === 'TB' ? 'TB' : 'LR';
  const layerGap = Number(opts.layerGap) || 96;
  const nodeGap = Number(opts.nodeGap) || 26;
  const padding = Number(opts.padding) || 28;
  const { layers, levelOf, graph } = layeredLayers(nodes, edges);
  orderLayers(layers, graph, opts.orderIterations ?? 4);
  const boxes = new Map(graph.ids.map((id) => [id, nodeBox(graph.byId.get(id), opts)]));
  const horizontal = direction === 'LR';
  const thicknessOf = layers.map((layer) =>
    Math.max(60, ...layer.map((id) => (horizontal ? boxes.get(id).w : boxes.get(id).h))),
  );
  const breadthOf = layers.map((layer) =>
    layer.reduce(
      (sum, id, i) =>
        sum + (horizontal ? boxes.get(id).h : boxes.get(id).w) + (i ? nodeGap : 0),
      0,
    ),
  );
  const maxBreadth = Math.max(0, ...breadthOf);
  const positions = new Map();
  let offset = padding;
  layers.forEach((layer, li) => {
    const thickness = thicknessOf[li];
    let breadth = padding + (maxBreadth - breadthOf[li]) / 2;
    for (const id of layer) {
      const b = boxes.get(id);
      if (horizontal) {
        positions.set(id, { x: offset + (thickness - b.w) / 2, y: breadth, w: b.w, h: b.h, lines: b.lines });
        breadth += b.h + nodeGap;
      } else {
        positions.set(id, { x: breadth, y: offset + (thickness - b.h) / 2, w: b.w, h: b.h, lines: b.lines });
        breadth += b.w + nodeGap;
      }
    }
    offset += thickness + layerGap;
  });
  withCenters(positions);
  const contentW = horizontal ? offset - layerGap + padding : padding * 2 + maxBreadth;
  const contentH = horizontal ? padding * 2 + maxBreadth : offset - layerGap + padding;
  return {
    positions,
    width: Math.max(120, Math.round(contentW)),
    height: Math.max(120, Math.round(contentH)),
    layers,
    levelOf,
    graph,
    direction,
  };
}

/**
 * Разводит пересекающиеся прямоугольники узлов (детерминированно, без DOM).
 * Нужен после force/radial: сила считается по центрам и не знает про ширину подписей.
 */
/** Развести пересекающиеся прямоугольники. @param {Map} positions @param {object} [opts] @returns {Map} */
export function relaxOverlaps(positions, { padding = 10, iterations = 60, damping = 0.6 } = {}) {
  const arr = [...positions.values()];
  for (let it = 0; it < iterations; it += 1) {
    let worst = 0;
    for (let i = 0; i < arr.length; i += 1) {
      for (let j = i + 1; j < arr.length; j += 1) {
        const a = arr[i];
        const b = arr[j];
        const dx = b.x + b.w / 2 - (a.x + a.w / 2);
        const dy = b.y + b.h / 2 - (a.y + a.h / 2);
        const ox = (a.w + b.w) / 2 + padding - Math.abs(dx);
        const oy = (a.h + b.h) / 2 + padding - Math.abs(dy);
        if (ox <= 0 || oy <= 0) continue;
        worst = Math.max(worst, Math.min(ox, oy));
        if (ox < oy) {
          const push = (ox / 2 + 0.5) * damping * (dx >= 0 ? 1 : -1);
          a.x -= push;
          b.x += push;
        } else {
          const push = (oy / 2 + 0.5) * damping * (dy >= 0 ? 1 : -1);
          a.y -= push;
          b.y += push;
        }
      }
    }
    if (worst < 0.5) break;
  }
  arr.forEach((p) => {
    p.cx = p.x + p.w / 2;
    p.cy = p.y + p.h / 2;
  });
  return positions;
}

/** Детерминированная force-раскладка (seeded PRNG, фиксированные итерации). */
/** Детерминированная force-раскладка. @param {Array} [nodes] @param {Array} [edges] @param {object} [opts] @returns {object} */
export function forceLayout(nodes = [], edges = [], opts = {}) {
  const g = buildGraph(nodes, edges);
  const n = g.ids.length;
  const width = Math.max(240, Number(opts.width) || 900);
  const height = Math.max(200, Number(opts.height) || 600);
  const iterations = Math.max(
    10,
    Math.floor(Number(opts.iterations) || (n > 150 ? 80 : n > 60 ? 160 : 260)),
  );
  const seed = opts.seed ?? 7;
  const rand = mulberry32(seed);
  const boxes = new Map(g.ids.map((id) => [id, nodeBox(g.byId.get(id), opts)]));
  const points = g.ids.map((id, i) => {
    const angle = (i / Math.max(1, n)) * Math.PI * 2;
    const radius = Math.min(width, height) * (0.18 + 0.24 * rand());
    return {
      id,
      x: width / 2 + Math.cos(angle) * radius,
      y: height / 2 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
    };
  });
  const index = new Map(points.map((p, i) => [p.id, i]));
  const k = Math.sqrt((width * height) / Math.max(1, n)) * 0.62;
  const areaPad = 46;
  for (let step = 0; step < iterations; step += 1) {
    const cool = 1 - step / (iterations * 1.15);
    for (let i = 0; i < n; i += 1) {
      const a = points[i];
      for (let j = i + 1; j < n; j += 1) {
        const b = points[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) {
          dx = (rand() - 0.5) * 2 || 0.5;
          dy = (rand() - 0.5) * 2 || 0.5;
          d2 = dx * dx + dy * dy || 1;
        }
        const d = Math.sqrt(d2);
        const rep = (k * k) / d;
        const fx = (dx / d) * rep;
        const fy = (dy / d) * rep;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }
    const linkDist = k * 0.82;
    for (const e of g.edgeList) {
      const a = points[index.get(e.from)];
      const b = points[index.get(e.to)];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (d - linkDist) * 0.045;
      const fx = (dx / d) * force;
      const fy = (dy / d) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    const maxStep = Math.max(4, 18 * cool);
    for (const p of points) {
      p.vx += (width / 2 - p.x) * 0.008;
      p.vy += (height / 2 - p.y) * 0.008;
      p.vx *= 0.86;
      p.vy *= 0.86;
      const v = Math.hypot(p.vx, p.vy);
      if (v > maxStep) {
        p.vx = (p.vx / v) * maxStep;
        p.vy = (p.vy / v) * maxStep;
      }
      p.x = clamp(p.x + p.vx, areaPad, width - areaPad);
      p.y = clamp(p.y + p.vy, areaPad, height - areaPad);
    }
  }
  const positions = new Map();
  points.forEach((p) => {
    const b = boxes.get(p.id);
    positions.set(p.id, {
      x: Math.round((p.x - b.w / 2) * 100) / 100,
      y: Math.round((p.y - b.h / 2) * 100) / 100,
      w: b.w,
      h: b.h,
      lines: b.lines,
    });
  });
  relaxOverlaps(positions, { padding: 12 });
  const pad = 20;
  let minX = Infinity;
  let minY = Infinity;
  positions.forEach((p) => {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    p.cx = p.x + p.w / 2;
    p.cy = p.y + p.h / 2;
  });
  let maxX = 0;
  let maxY = 0;
  positions.forEach((p) => {
    p.x += pad - minX;
    p.y += pad - minY;
    p.cx = p.x + p.w / 2;
    p.cy = p.y + p.h / 2;
    maxX = Math.max(maxX, p.x + p.w);
    maxY = Math.max(maxY, p.y + p.h);
  });
  return {
    positions,
    width: Math.round(Math.max(width, maxX + pad)),
    height: Math.round(Math.max(height, maxY + pad)),
    graph: g,
    seed,
    iterations,
  };
}

/** Радиальная раскладка: центр — узел с максимальной степенью, кольца по BFS-расстоянию. */
/** Радиальная раскладка по BFS. @param {Array} [nodes] @param {Array} [edges] @param {object} [opts] @returns {object} */
export function radialLayout(nodes = [], edges = [], opts = {}) {
  const g = buildGraph(nodes, edges);
  const ringGap = Number(opts.ringGap) || 140;
  const padding = Number(opts.padding) || 30;
  if (!g.ids.length) {
    return { positions: new Map(), width: 120, height: 120, dist: new Map(), center: null, graph: g };
  }
  let center = g.ids[0];
  for (const id of g.ids) {
    if (g.degree.get(id) > g.degree.get(center)) center = id;
  }
  const dist = new Map([[center, 0]]);
  const queue = [center];
  while (queue.length) {
    const id = queue.shift();
    const nbrs = [...g.out.get(id), ...g.inc.get(id)];
    for (const nb of nbrs) {
      if (dist.has(nb)) continue;
      dist.set(nb, dist.get(id) + 1);
      queue.push(nb);
    }
  }
  let extra = 0;
  for (const id of g.ids) {
    if (dist.has(id)) continue;
    // отдельная компонента: свой BFS, кольца снаружи основного дерева
    const base = Math.max(0, ...dist.values()) + 1 + extra;
    extra += 1;
    dist.set(id, base);
    const q = [id];
    while (q.length) {
      const cur = q.shift();
      for (const nb of [...g.out.get(cur), ...g.inc.get(cur)]) {
        if (dist.has(nb)) continue;
        dist.set(nb, dist.get(cur) + 1);
        q.push(nb);
      }
    }
  }
  const rings = new Map();
  for (const id of g.ids) {
    const d = dist.get(id);
    if (!rings.has(d)) rings.set(d, []);
    rings.get(d).push(id);
  }
  const boxes = new Map(g.ids.map((id) => [id, nodeBox(g.byId.get(id), opts)]));
  const raw = new Map();
  const ringKeys = [...rings.keys()].sort((a, b) => a - b);
  for (const d of ringKeys) {
    const ids = rings.get(d);
    const circumference = ids.reduce((sum, id) => sum + boxes.get(id).w + 16, 0);
    const r = Math.max(d * ringGap, circumference / (Math.PI * 2));
    const offset = (d % 2) * (Math.PI / Math.max(1, ids.length));
    ids.forEach((id, i) => {
      const angle = ids.length === 1 && d === 0 ? 0 : offset + (i / ids.length) * Math.PI * 2;
      const cx = Math.cos(angle) * r;
      const cy = Math.sin(angle) * r;
      const b = boxes.get(id);
      raw.set(id, { x: cx - b.w / 2, y: cy - b.h / 2, w: b.w, h: b.h, lines: b.lines });
    });
  }
  raw.forEach((p) => {
    p.cx = p.x + p.w / 2;
    p.cy = p.y + p.h / 2;
  });
  relaxOverlaps(raw, { padding: 10, iterations: 40 });
  let minX = Infinity;
  let minY = Infinity;
  raw.forEach((p) => {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
  });
  const positions = new Map();
  raw.forEach((p, id) => {
    positions.set(id, {
      x: Math.round((p.x - minX + padding) * 100) / 100,
      y: Math.round((p.y - minY + padding) * 100) / 100,
      w: p.w,
      h: p.h,
      lines: p.lines,
    });
  });
  let maxX = 0;
  let maxY = 0;
  positions.forEach((p) => {
    p.cx = p.x + p.w / 2;
    p.cy = p.y + p.h / 2;
    maxX = Math.max(maxX, p.x + p.w);
    maxY = Math.max(maxY, p.y + p.h);
  });
  return {
    positions,
    width: Math.round(maxX + padding),
    height: Math.round(maxY + padding),
    dist,
    center,
    graph: g,
  };
}

/** Точка на границе прямоугольника в направлении (tx,ty). */
/** Точка на границе рамки в направлении цели. @param {object} box @param {number} tx @param {number} ty @returns {object} */
export function borderPoint(box, tx, ty) {
  const cx = box.x + box.w / 2;
  const cy = box.y + box.h / 2;
  const dx = tx - cx;
  const dy = ty - cy;
  if (!dx && !dy) return { x: cx, y: cy };
  const sx = dx ? box.w / 2 / Math.abs(dx) : Infinity;
  const sy = dy ? box.h / 2 / Math.abs(dy) : Infinity;
  const s = Math.min(sx, sy, 1);
  return { x: cx + dx * s, y: cy + dy * s };
}

/** Геометрия ребра между блоками: границы, кубическая кривая и точка подписи. */
/** Геометрия ребра (кубическая кривая). @param {object} fromBox @param {object} toBox @param {object} [opts] @returns {object} */
export function edgeGeometry(fromBox, toBox, opts = {}) {
  const curved = opts.curved !== false;
  const fx = fromBox.x + fromBox.w / 2;
  const fy = fromBox.y + fromBox.h / 2;
  const tx = toBox.x + toBox.w / 2;
  const ty = toBox.y + toBox.h / 2;
  const a = borderPoint(fromBox, tx, ty);
  const b = borderPoint(toBox, fx, fy);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  let c1;
  let c2;
  if (Math.abs(dx) >= Math.abs(dy)) {
    c1 = { x: a.x + dx * 0.45, y: a.y };
    c2 = { x: b.x - dx * 0.45, y: b.y };
  } else {
    c1 = { x: a.x, y: a.y + dy * 0.45 };
    c2 = { x: b.x, y: b.y - dy * 0.45 };
  }
  const d = curved
    ? `M ${a.x} ${a.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${b.x} ${b.y}`
    : `M ${a.x} ${a.y} L ${b.x} ${b.y}`;
  const straight = !curved || Math.abs(dx) < 2 || Math.abs(dy) < 2;
  return {
    d,
    p0: a,
    p3: b,
    c1: straight ? { x: a.x + dx / 3, y: a.y + dy / 3 } : c1,
    c2: straight ? { x: a.x + (dx * 2) / 3, y: a.y + (dy * 2) / 3 } : c2,
    x1: a.x,
    y1: a.y,
    x2: b.x,
    y2: b.y,
    mx: (a.x + b.x) / 2,
    my: (a.y + b.y) / 2,
    straight,
  };
}

/** Точка на ребре в доле t (0..1) — для поиска свободного места подписи. */
/** Точка на ребре по параметру t. @param {object} geo @param {number} t @returns {{x:number,y:number}} */
export function edgePoint(geo, t) {
  if (!geo || !geo.p0 || !geo.p3) return { x: 0, y: 0 };
  if (geo.straight) {
    return {
      x: geo.p0.x + (geo.p3.x - geo.p0.x) * t,
      y: geo.p0.y + (geo.p3.y - geo.p0.y) * t,
    };
  }
  const mt = 1 - t;
  return {
    x: mt ** 3 * geo.p0.x + 3 * mt * mt * t * geo.c1.x + 3 * mt * t * t * geo.c2.x + t ** 3 * geo.p3.x,
    y: mt ** 3 * geo.p0.y + 3 * mt * mt * t * geo.c1.y + 3 * mt * t * t * geo.c2.y + t ** 3 * geo.p3.y,
  };
}

/** Раскладка по имени (для graph.js и тестов). */
/** Раскладка по имени (graph.js/тесты). @param {object} [spec] @param {object} [size] @returns {object} */
export function layoutGraph(spec = {}, size = {}) {
  const kind = spec.layout === 'force' || spec.layout === 'radial' ? spec.layout : 'layered';
  const opts = { ...(spec.layoutOptions || {}) };
  if (kind === 'force') {
    return { kind, ...forceLayout(spec.nodes, spec.edges, { width: size.width, height: size.height, ...opts }) };
  }
  if (kind === 'radial') return { kind, ...radialLayout(spec.nodes, spec.edges, opts) };
  return { kind, ...layeredLayout(spec.nodes, spec.edges, { direction: spec.direction, ...opts }) };
}
