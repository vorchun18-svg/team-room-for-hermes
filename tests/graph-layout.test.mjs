// node --test tests/graph-layout.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  mulberry32,
  buildGraph,
  layeredLayers,
  orderLayers,
  wrapLabel,
  nodeBox,
  layeredLayout,
  forceLayout,
  radialLayout,
  borderPoint,
  edgeGeometry,
  edgePoint,
  layoutGraph,
  relaxOverlaps,
} from '../web/js/report/graph-layout.js';

const nodes = (ids) => ids.map((id) => ({ id, label: id }));
const coords = (layout) =>
  [...layout.positions.entries()]
    .map(([id, p]) => [id, p.x, p.y])
    .sort((a, b) => a[0].localeCompare(b[0]));

test('layeredLayers: ромб A→B,C→D по слоям 0/1/1/2', () => {
  const { layers, levelOf } = layeredLayers(nodes(['A', 'B', 'C', 'D']), [
    { from: 'A', to: 'B' },
    { from: 'A', to: 'C' },
    { from: 'B', to: 'D' },
    { from: 'C', to: 'D' },
  ]);
  assert.deepEqual(levelOf.get('A'), 0);
  assert.deepEqual(levelOf.get('D'), 2);
  assert.equal(layers.length, 3);
  assert.equal(layers[2][0], 'D');
  assert.equal(new Set([...levelOf.keys()]).size, 4);
});

test('layeredLayers: длинная цепочка даёт по узлу на слой', () => {
  const ids = ['n0', 'n1', 'n2', 'n3', 'n4'];
  const edges = ids.slice(1).map((id, i) => ({ from: ids[i], to: id }));
  const { layers, levelOf } = layeredLayers(nodes(ids), edges);
  assert.equal(layers.length, 5);
  ids.forEach((id, i) => assert.equal(levelOf.get(id), i));
});

test('layeredLayers: цикл не вешает и размещает все узлы', () => {
  const start = Date.now();
  const { layers, levelOf, cycleBroken } = layeredLayers(nodes(['A', 'B', 'C', 'D']), [
    { from: 'A', to: 'B' },
    { from: 'B', to: 'C' },
    { from: 'C', to: 'A' },
    { from: 'A', to: 'D' },
  ]);
  assert.ok(Date.now() - start < 1000);
  assert.equal(levelOf.size, 4);
  assert.equal(cycleBroken, true);
  const flat = layers.flat();
  assert.equal(flat.length, 4);
  assert.ok(levelOf.get('D') > levelOf.get('A'));
});

test('layeredLayers: петля и рёбра в неизвестные узлы игнорируются', () => {
  const { graph, layers } = layeredLayers(nodes(['A', 'B']), [
    { from: 'A', to: 'A' },
    { from: 'A', to: 'zzz' },
    { from: 'A', to: 'B' },
  ]);
  assert.equal(graph.edgeList.length, 1);
  assert.equal(layers.flat().length, 2);
});

test('orderLayers: детерминирован и не теряет узлы', () => {
  const input = nodes(['A', 'B', 'C', 'D', 'E']);
  const edges = [
    { from: 'A', to: 'D' },
    { from: 'B', to: 'D' },
    { from: 'C', to: 'E' },
    { from: 'B', to: 'E' },
  ];
  const one = layeredLayers(input, edges);
  const two = layeredLayers(input, edges);
  orderLayers(one.layers, one.graph, 4);
  orderLayers(two.layers, two.graph, 4);
  assert.deepEqual(one.layers, two.layers);
  assert.deepEqual(one.layers.flat().sort(), ['A', 'B', 'C', 'D', 'E']);
});

test('wrapLabel: длинная русская подпись ≤ 3 строк и без потери смысла', () => {
  const lines = wrapLabel('Легализация документов для постоянной визы в России', 16, 3);
  assert.ok(lines.length <= 3);
  assert.ok(lines.every((l) => l.length <= 16), JSON.stringify(lines));
  assert.ok(lines[lines.length - 1].endsWith('…'));
  assert.equal(wrapLabel('Моти', 16, 3).length, 1);
  assert.deepEqual(wrapLabel('', 16, 3), ['']);
  const longWord = wrapLabel('Сверхдлинноесловобезпробелов', 8, 3);
  assert.ok(longWord.length <= 3);
  assert.ok(longWord.every((l) => l.length <= 8));
});

test('nodeBox: размер растёт от подписи и size', () => {
  const small = nodeBox({ id: 'a', label: 'Моти' });
  const big = nodeBox({ id: 'b', label: 'Договор с поставщиком моти в Сан-Паулу', size: 1.4 });
  assert.ok(big.w >= small.w);
  assert.ok(big.h >= small.h);
  assert.ok(big.lines.length <= 3);
  assert.ok(small.w >= 96);
});

test('layeredLayout: LR и TB дают конечные координаты, направление меняет оси', () => {
  const n = nodes(['A', 'B', 'C', 'D']);
  const e = [
    { from: 'A', to: 'B' },
    { from: 'A', to: 'C' },
    { from: 'B', to: 'D' },
  ];
  const lr = layeredLayout(n, e, { direction: 'LR' });
  const tb = layeredLayout(n, e, { direction: 'TB' });
  for (const p of [...lr.positions.values(), ...tb.positions.values()]) {
    assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y));
    assert.ok(p.w > 0 && p.h > 0);
  }
  assert.ok(lr.width > lr.height, 'LR шире, чем выше');
  assert.ok(tb.height > tb.width * 0.5);
  const a = lr.positions.get('A');
  const d = lr.positions.get('D');
  assert.ok(d.x > a.x, 'в LR потомок правее');
});

test('forceLayout: детерминизм при одинаковом seed', () => {
  const n = nodes(['A', 'B', 'C', 'D', 'E', 'F']);
  const e = [
    { from: 'A', to: 'B' },
    { from: 'A', to: 'C' },
    { from: 'B', to: 'D' },
    { from: 'C', to: 'E' },
    { from: 'D', to: 'F' },
    { from: 'E', to: 'F' },
  ];
  const one = forceLayout(n, e, { seed: 42, width: 640, height: 420 });
  const two = forceLayout(n, e, { seed: 42, width: 640, height: 420 });
  assert.deepEqual(coords(one), coords(two));
  const other = forceLayout(n, e, { seed: 7, width: 640, height: 420 });
  assert.notDeepEqual(coords(one), coords(other));
});

test('forceLayout: 500 узлов раскладываются без зависания', () => {
  const n = Array.from({ length: 500 }, (_, i) => ({ id: `n${i}`, label: `Узел ${i}` }));
  const e = [];
  for (let i = 1; i < 500; i += 1) e.push({ from: `n${(i - 1) % 40}`, to: `n${i}` });
  const t0 = Date.now();
  const layout = forceLayout(n, e, { seed: 3, width: 1000, height: 700 });
  const ms = Date.now() - t0;
  assert.equal(layout.positions.size, 500);
  assert.ok(ms < 15000, `force 500 занял ${ms} мс`);
  for (const p of layout.positions.values()) assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y));
});

test('radialLayout: центр — максимальная степень, кольца по BFS', () => {
  const n = nodes(['hub', 'a', 'b', 'c', 'd']);
  const e = [
    { from: 'hub', to: 'a' },
    { from: 'hub', to: 'b' },
    { from: 'hub', to: 'c' },
    { from: 'a', to: 'd' },
  ];
  const layout = radialLayout(n, e, { ringGap: 120 });
  assert.equal(layout.center, 'hub');
  assert.equal(layout.dist.get('hub'), 0);
  assert.equal(layout.dist.get('a'), 1);
  assert.equal(layout.dist.get('d'), 2);
  assert.equal(layout.positions.size, 5);
  const hub = layout.positions.get('hub');
  const far = layout.positions.get('d');
  const distHub = Math.hypot(hub.cx - far.cx, hub.cy - far.cy);
  const near = layout.positions.get('a');
  assert.ok(distHub > Math.hypot(hub.cx - near.cx, hub.cy - near.cy));
});

test('radialLayout: изолированные узлы не теряются', () => {
  const layout = radialLayout(
    nodes(['A', 'B', 'C']),
    [{ from: 'A', to: 'B' }],
    { ringGap: 100 },
  );
  assert.equal(layout.positions.size, 3);
  assert.ok(layout.dist.get('C') >= 1);
});

test('borderPoint и edgeGeometry: рёбра выходят из границ, а не из центров', () => {
  const a = { x: 0, y: 0, w: 100, h: 40 };
  const b = { x: 300, y: 0, w: 100, h: 40 };
  const p = borderPoint(a, 350, 20);
  assert.equal(p.x, 100);
  assert.ok(p.y > 0 && p.y < 40);
  const g = edgeGeometry(a, b);
  assert.ok(g.d.startsWith('M 100'));
  assert.equal(g.x2, 300);
  assert.ok(Number.isFinite(g.mx) && Number.isFinite(g.my));
  assert.ok(g.d.includes('C'));
});

test('edgePoint: концы и середина ребра', () => {
  const a = { x: 0, y: 0, w: 100, h: 40 };
  const b = { x: 300, y: 0, w: 100, h: 40 };
  const geo = edgeGeometry(a, b, { curved: false });
  assert.deepEqual(edgePoint(geo, 0), { x: geo.p0.x, y: geo.p0.y });
  assert.deepEqual(edgePoint(geo, 1), { x: geo.p3.x, y: geo.p3.y });
  const mid = edgePoint(geo, 0.5);
  assert.ok(Math.abs(mid.x - geo.mx) < 0.001 && Math.abs(mid.y - geo.my) < 0.001);
  const curved = edgeGeometry(a, b);
  const c = edgePoint(curved, 0.5);
  assert.ok(Number.isFinite(c.x) && Number.isFinite(c.y));
});

test('relaxOverlaps: разводит наложенные узлы и детерминирован', () => {
  const make = () => new Map([
    ['a', { x: 0, y: 0, w: 100, h: 40 }],
    ['b', { x: 10, y: 5, w: 100, h: 40 }],
    ['c', { x: 300, y: 200, w: 80, h: 40 }],
  ]);
  const one = relaxOverlaps(make(), { padding: 10, iterations: 50 });
  const two = relaxOverlaps(make(), { padding: 10, iterations: 50 });
  const rect = (p) => [p.x, p.y, p.w, p.h];
  assert.deepEqual([...one.values()].map(rect), [...two.values()].map(rect));
  const a = one.get('a');
  const b = one.get('b');
  const overlapX = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const overlapY = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  assert.ok(!(overlapX > 1 && overlapY > 1), `узлы всё ещё пересекаются: ${overlapX}x${overlapY}`);
});

test('forceLayout: после разведения нет пересечений узлов', () => {
  const n = nodes(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']);
  const e = [
    { from: 'A', to: 'B' },
    { from: 'A', to: 'C' },
    { from: 'B', to: 'D' },
    { from: 'C', to: 'E' },
    { from: 'D', to: 'F' },
    { from: 'E', to: 'G' },
    { from: 'F', to: 'H' },
  ];
  const { positions } = forceLayout(n, e, { seed: 5, width: 700, height: 500 });
  const arr = [...positions.values()];
  for (let i = 0; i < arr.length; i += 1) {
    for (let j = i + 1; j < arr.length; j += 1) {
      const a = arr[i];
      const b = arr[j];
      const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      assert.ok(!(ox > 1 && oy > 1), `пересечение ${i}/${j}`);
    }
  }
});

test('layoutGraph: переключение раскладок', () => {
  const spec = { nodes: nodes(['A', 'B']), edges: [{ from: 'A', to: 'B' }] };
  assert.equal(layoutGraph({ ...spec, layout: 'layered' }).kind, 'layered');
  assert.equal(layoutGraph({ ...spec, layout: 'force' }).kind, 'force');
  assert.equal(layoutGraph({ ...spec, layout: 'radial' }).kind, 'radial');
  assert.equal(layoutGraph({ ...spec, layout: 'unknown' }).kind, 'layered');
  assert.ok(mulberry32(5)() < 1);
  assert.equal(buildGraph([], []).ids.length, 0);
});
