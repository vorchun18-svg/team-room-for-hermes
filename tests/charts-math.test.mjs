// node --test tests/charts-math.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  niceStep,
  stepDecimals,
  roundTo,
  niceTicks,
  makeScale,
  bandScale,
  stackSeries,
  pieArcs,
  arcPath,
  linePath,
  bbox,
  seriesExtent,
  scatterExtent,
  indexTicks,
  truncate,
  unitKind,
  charsForWidth,
} from '../web/js/report/charts-math.js';

const close = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;

test('niceStep выдаёт шаги 1/2/2.5/5 × 10^k', () => {
  assert.equal(niceStep(100, 5), 20);
  assert.equal(niceStep(30, 5), 10);
  assert.equal(niceStep(0.3, 5), 0.1);
  assert.equal(niceStep(0, 5), 0.2); // диапазон 0 трактуется как 1/5
});

test('stepDecimals и roundTo убирают плавающий мусор', () => {
  assert.equal(stepDecimals(2.5), 1);
  assert.equal(stepDecimals(0.25), 2);
  assert.equal(stepDecimals(100), 0);
  assert.equal(roundTo(0.1 + 0.2, 0.1), 0.3);
});

test('niceTicks: отрицательные значения и нулевая линия внутри', () => {
  const t = niceTicks(-30, 70, 5);
  assert.ok(t.min <= -30 && t.max >= 70);
  assert.ok(t.ticks.includes(0), 'ноль должен быть делением');
  assert.ok(t.ticks.length >= 4 && t.ticks.length <= 9);
  for (let i = 1; i < t.ticks.length; i += 1) assert.ok(t.ticks[i] > t.ticks[i - 1]);
});

test('niceTicks: плоские данные (0..0) и узкий диапазон', () => {
  const zero = niceTicks(0, 0);
  assert.deepEqual([zero.min, zero.max], [0, 1]);
  assert.ok(zero.ticks.includes(0) && zero.ticks.includes(1));
  const flat = niceTicks(5, 5);
  assert.ok(flat.min < 5 && flat.max > 5);
  const tiny = niceTicks(0, 0.06, 5);
  assert.ok(tiny.max >= 0.06);
  assert.ok(tiny.ticks.length >= 3);
});

test('niceTicks: NaN и перевёрнутый диапазон не ломают шкалу', () => {
  const a = niceTicks(NaN, 10);
  assert.ok(Number.isFinite(a.min) && Number.isFinite(a.max));
  const b = niceTicks(20, 10);
  assert.ok(b.min <= 10 && b.max >= 20);
});

test('makeScale: масштаб и обратный пересчёт', () => {
  const s = makeScale(0, 100, 300, 20);
  assert.equal(s(0), 300);
  assert.equal(s(100), 20);
  assert.ok(close(s.invert(160), 50));
  assert.ok(close(s.invert(s(37)), 37, 1e-9));
});

test('bandScale: полосы внутри диапазона и по порядку', () => {
  const b = bandScale(4, 100, 500);
  assert.equal(b.step, 100);
  assert.equal(b.band, 76);
  assert.ok(b.center(0) < b.center(1));
  assert.ok(b.start(0) >= 100 && b.start(3) + b.band <= 500);
  assert.equal(b.start(0), 112);
});

test('stackSeries: положительные вверх, отрицательные вниз', () => {
  const st = stackSeries([
    { data: [10, -5] },
    { data: [4, -2] },
  ]);
  assert.deepEqual(st.points[0][0], { y0: 0, y1: 10, v: 10 });
  assert.deepEqual(st.points[1][0], { y0: 10, y1: 14, v: 4 });
  assert.deepEqual(st.points[0][1], { y0: 0, y1: -5, v: -5 });
  assert.deepEqual(st.points[1][1], { y0: -5, y1: -7, v: -2 });
  assert.equal(st.max, 14);
  assert.equal(st.min, -7);
});

test('stackSeries: пустые и дырявые данные не падают', () => {
  const st = stackSeries([{ data: [1, null, 3] }, {}, { data: [] }]);
  assert.equal(st.points.length, 3);
  assert.equal(st.points[0][1].v, 0);
  assert.equal(st.points[2][0].v, 0);
  assert.equal(stackSeries().max, 0);
});

test('pieArcs: сумма долей 1 и полный круг 2π', () => {
  const arcs = pieArcs([30, 50, 20]);
  assert.ok(close(arcs.reduce((a, x) => a + x.frac, 0), 1));
  assert.ok(close(arcs[arcs.length - 1].end - arcs[0].start, Math.PI * 2));
  assert.ok(close(arcs[0].start, -Math.PI / 2));
  assert.ok(close(arcs[0].mid, -Math.PI / 2 + 0.3 * Math.PI));
});

test('pieArcs: нули и отрицательные не дают секторов, сумма 0', () => {
  const arcs = pieArcs([0, -5, 0]);
  assert.equal(arcs.reduce((a, x) => a + x.frac, 0), 0);
  assert.ok(arcs.every((a) => a.frac === 0));
});

test('arcPath: полный круг из двух дуг и обычный сектор', () => {
  const full = arcPath(0, 0, 10, 20, 0, Math.PI * 2);
  assert.equal((full.match(/A /g) || []).length, 4, 'кольцо — две дуги снаружи и две внутри');
  const pie = arcPath(0, 0, 0, 20, 0, Math.PI * 2);
  assert.equal((pie.match(/A /g) || []).length, 2);
  const half = arcPath(100, 100, 0, 50, 0, Math.PI);
  assert.ok(half.startsWith('M 100 100'));
  assert.ok(half.endsWith('Z'));
});

test('linePath: разрыв на null, сглаживание даёт C', () => {
  const p = [{ x: 0, y: 0 }, { x: 10, y: 5 }, { x: 20, y: null }, { x: 30, y: 8 }];
  const flat = linePath(p);
  assert.equal((flat.match(/M /g) || []).length, 2);
  assert.ok(!flat.includes('NaN'));
  const smooth = linePath([{ x: 0, y: 0 }, { x: 10, y: 5 }, { x: 20, y: 2 }], { smooth: true });
  assert.ok(smooth.includes('C'));
});

test('bbox и серии: габариты и пустые данные', () => {
  const box = bbox([{ x: 0, y: 0 }, { x: 10, y: 20 }], 5);
  assert.deepEqual(box, { minX: -5, minY: -5, maxX: 15, maxY: 25, width: 20, height: 30 });
  assert.equal(bbox([]).width, 0);
  assert.deepEqual(seriesExtent([]), { min: 0, max: 0, empty: true });
  assert.deepEqual(seriesExtent([{ data: [3, -2, null, 5] }]), { min: -2, max: 5, empty: false });
  const sc = scatterExtent([{ data: [{ x: 1, y: 2 }, { x: 4, y: -1 }] }]);
  assert.deepEqual([sc.xMin, sc.xMax, sc.yMin, sc.yMax], [1, 4, -1, 2]);
  assert.equal(scatterExtent([]).empty, true);
});

test('indexTicks: прореживание, последний индекс всегда есть', () => {
  assert.deepEqual(indexTicks(3, 10), [0, 1, 2]);
  const t = indexTicks(24, 6);
  assert.equal(t[0], 0);
  assert.equal(t[t.length - 1], 23);
  assert.ok(t.length <= 8);
  assert.deepEqual(indexTicks(0, 5), []);
});

test('truncate: русская подпись с многоточием, длина не превышает лимит', () => {
  const s = truncate('Легализация документов для визы', 12);
  assert.ok(s.endsWith('…'));
  assert.ok(s.length <= 12);
  assert.equal(truncate('Коротко', 20), 'Коротко');
  assert.equal(truncate('', 5), '');
  assert.equal(truncate('abc', 0), '');
});

test('unitKind: деньги/проценты/штуки', () => {
  assert.equal(unitKind('R$'), 'money');
  assert.equal(unitKind('BRL'), 'money');
  assert.equal(unitKind('%'), 'percent');
  assert.equal(unitKind('шт'), 'count');
  assert.equal(unitKind(''), 'number');
});

test('charsForWidth: оценка влезающих символов', () => {
  assert.ok(charsForWidth(120, 11) >= 14);
  assert.ok(charsForWidth(40, 11) >= 1);
});
