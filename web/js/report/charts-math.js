// charts-math.js — чистая математика графиков. Без DOM и импортов: покрывается node-тестами.

/** Шаг «красивой» шкалы (1/2/2.5/5/10 × 10^k) для диапазона span и желаемого числа делений. */
/** Красивый шаг шкалы. @param {number} span @param {number} [count] @returns {number} */
export function niceStep(span, count = 5) {
  const s = Math.abs(Number(span));
  const safeSpan = Number.isFinite(s) && s > 0 ? s : 1;
  const raw = safeSpan / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  let mult;
  if (norm <= 1) mult = 1;
  else if (norm <= 2) mult = 2;
  else if (norm <= 2.5) mult = 2.5;
  else if (norm <= 5) mult = 5;
  else mult = 10;
  return mult * mag;
}

/** Число знаков после запятой, нужное шагу (2.5 → 1, 0.25 → 2, 100 → 0). */
/** Знаков после запятой для шага. @param {number} step @returns {number} */
export function stepDecimals(step) {
  const s = Math.abs(Number(step));
  if (!Number.isFinite(s) || s === 0) return 0;
  let d = 0;
  let v = s;
  while (d < 6 && Math.abs(v - Math.round(v)) > 1e-9) {
    v *= 10;
    d += 1;
  }
  return d;
}

/** Округление до точности шага, чтобы убрать плавающий мусор (0.30000000000000004). */
/** Округление до шага. @param {number} value @param {number} step @returns {number} */
export function roundTo(value, step) {
  const d = stepDecimals(step);
  return Number(Number(value).toFixed(d));
}

/**
 * Красивый домен и деления: расширяет [min,max] до границ шага.
 * @returns {{min:number,max:number,step:number,ticks:number[]}}
 */
/** Набор делений оси. @param {number} min @param {number} max @param {number} [count] @returns {number[]} */
export function niceTicks(min, max, count = 5) {
  let lo = Number(min);
  let hi = Number(max);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
    lo = 0;
    hi = 1;
  }
  if (lo > hi) [lo, hi] = [hi, lo];
  if (lo === hi) {
    if (lo === 0) {
      lo = 0;
      hi = 1;
    } else {
      const pad = Math.abs(lo) * 0.25;
      lo -= pad;
      hi += pad;
    }
  }
  const step = niceStep(hi - lo, count);
  const nMin = Math.floor(lo / step + 1e-9) * step;
  const nMax = Math.ceil(hi / step - 1e-9) * step;
  const ticks = [];
  for (let i = 0; i <= 60; i += 1) {
    const v = roundTo(nMin + i * step, step);
    if (v > nMax + step * 1e-9) break;
    ticks.push(v);
  }
  if (!ticks.length) ticks.push(roundTo(nMin, step));
  return { min: roundTo(nMin, step), max: roundTo(nMax, step), step, ticks };
}

/** Линейная шкала domain → range; у результата есть .invert(p) и .domain/.range. */
/** Линейная шкала domain→range. @param {number} d0 @param {number} d1 @param {number} r0 @param {number} r1 @returns {Function} */
export function makeScale(d0, d1, r0, r1) {
  const span = d1 - d0 || 1;
  const k = (r1 - r0) / span;
  const f = (v) => r0 + (Number(v) - d0) * k;
  f.invert = (p) => d0 + (Number(p) - r0) / k;
  f.domain = [d0, d1];
  f.range = [r0, r1];
  return f;
}

/** Полосовая шкала для категорий: step, band, start(i), center(i). */
/** Полосная шкала для категорий. @param {number} count @param {number} r0 @param {number} r1 @param {number} [padding] @returns {Function} */
export function bandScale(count, r0, r1, padding = 0.24) {
  const n = Math.max(1, Math.floor(Number(count) || 0));
  const step = (r1 - r0) / n;
  const band = step * (1 - Math.max(0, Math.min(0.9, padding)));
  return {
    step,
    band,
    start: (i) => r0 + step * i + (step - band) / 2,
    center: (i) => r0 + step * (i + 0.5),
  };
}

/** Накопительное суммирование серий: положительные вверх, отрицательные вниз от нуля. */
/** Накопление серий. @param {Array} [series] @returns {{totals:number[], stacked:Array}} */
export function stackSeries(series = []) {
  const list = Array.isArray(series) ? series : [];
  const n = list.reduce((m, s) => Math.max(m, (s && s.data ? s.data.length : 0)), 0);
  const pos = new Array(n).fill(0);
  const neg = new Array(n).fill(0);
  const points = list.map((s) => {
    const data = Array.isArray(s && s.data) ? s.data : [];
    const out = [];
    for (let i = 0; i < n; i += 1) {
      const raw = Number(data[i]);
      const v = Number.isFinite(raw) ? raw : 0;
      let y0;
      let y1;
      if (v >= 0) {
        y0 = pos[i];
        y1 = pos[i] + v;
        pos[i] = y1;
      } else {
        y0 = neg[i];
        y1 = neg[i] + v;
        neg[i] = y1;
      }
      out.push({ y0, y1, v });
    }
    return out;
  });
  const values = pos.concat(neg);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  return { points, min, max };
}

/** Углы секторов; старт по умолчанию — 12 часов, по часовой стрелке. */
/** Углы секторов пирога. @param {number[]} [values] @param {number} [startAngle] @returns {Array} */
export function pieArcs(values = [], startAngle = -Math.PI / 2) {
  const clean = (Array.isArray(values) ? values : []).map((v) => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : 0;
  });
  const total = clean.reduce((a, b) => a + b, 0);
  let cursor = startAngle;
  return clean.map((v, index) => {
    const frac = total > 0 ? v / total : 0;
    const start = cursor;
    const end = cursor + frac * Math.PI * 2;
    cursor = end;
    return { index, value: v, frac, start, end, mid: (start + end) / 2 };
  });
}

/** Точка на окружности в SVG-координатах (y вниз). */
/** Полярные координаты → x,y. @param {number} cx @param {number} cy @param {number} r @param {number} angle @returns {{x:number,y:number}} */
export function polar(cx, cy, r, angle) {
  return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
}

/** Путь сектора/кольца от a0 до a1 (радианы). r0=0 — сектор пирога. */
/** SVG-путь сектора/кольца. @param {number} cx @param {number} cy @param {number} r0 @param {number} r1 @param {number} a0 @param {number} a1 @returns {string} */
export function arcPath(cx, cy, r0, r1, a0, a1) {
  const full = a1 - a0 >= Math.PI * 2 - 1e-6;
  const rr0 = Math.max(0, r0);
  const rr1 = Math.max(rr0, r1);
  if (full) {
    const top = polar(cx, cy, rr1, a0);
    const bottom = polar(cx, cy, rr1, a0 + Math.PI);
    let d = `M ${top.x} ${top.y} A ${rr1} ${rr1} 0 1 1 ${bottom.x} ${bottom.y} A ${rr1} ${rr1} 0 1 1 ${top.x} ${top.y}`;
    if (rr0 > 0.01) {
      const it = polar(cx, cy, rr0, a0);
      const ib = polar(cx, cy, rr0, a0 + Math.PI);
      d += ` M ${it.x} ${it.y} A ${rr0} ${rr0} 0 1 0 ${ib.x} ${ib.y} A ${rr0} ${rr0} 0 1 0 ${it.x} ${it.y}`;
    }
    return d;
  }
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const p1 = polar(cx, cy, rr1, a0);
  const p2 = polar(cx, cy, rr1, a1);
  if (rr0 <= 0.01) {
    return `M ${cx} ${cy} L ${p1.x} ${p1.y} A ${rr1} ${rr1} 0 ${large} 1 ${p2.x} ${p2.y} Z`;
  }
  const p3 = polar(cx, cy, rr0, a1);
  const p4 = polar(cx, cy, rr0, a0);
  return `M ${p1.x} ${p1.y} A ${rr1} ${rr1} 0 ${large} 1 ${p2.x} ${p2.y} L ${p3.x} ${p3.y} A ${rr0} ${rr0} 0 ${large} 0 ${p4.x} ${p4.y} Z`;
}

/** Ломаная/сглаженная кривая по точкам; null и не-числа разрывают линию. */
/** SVG-путь линии (опц. сглаживание). @param {Array} [points] @param {{smooth?:boolean, tension?:number}} [opts] @returns {string} */
export function linePath(points = [], { smooth = false, tension = 1 / 6 } = {}) {
  const valid = (p) => p && Number.isFinite(p.x) && Number.isFinite(p.y);
  const chunks = [];
  let cur = [];
  for (const p of points) {
    if (valid(p)) cur.push(p);
    else if (cur.length) {
      chunks.push(cur);
      cur = [];
    }
  }
  if (cur.length) chunks.push(cur);
  const fmt = (n) => Math.round(n * 100) / 100;
  return chunks
    .map((chunk) => {
      if (chunk.length === 1) {
        return `M ${fmt(chunk[0].x)} ${fmt(chunk[0].y)} L ${fmt(chunk[0].x + 0.01)} ${fmt(chunk[0].y)}`;
      }
      if (!smooth) {
        return chunk
          .map((p, i) => `${i ? 'L' : 'M'} ${fmt(p.x)} ${fmt(p.y)}`)
          .join(' ');
      }
      let d = `M ${fmt(chunk[0].x)} ${fmt(chunk[0].y)}`;
      for (let i = 0; i < chunk.length - 1; i += 1) {
        const p0 = chunk[i - 1] || chunk[i];
        const p1 = chunk[i];
        const p2 = chunk[i + 1];
        const p3 = chunk[i + 2] || p2;
        const c1x = p1.x + (p2.x - p0.x) * tension;
        const c1y = p1.y + (p2.y - p0.y) * tension;
        const c2x = p2.x - (p3.x - p1.x) * tension;
        const c2y = p2.y - (p3.y - p1.y) * tension;
        d += ` C ${fmt(c1x)} ${fmt(c1y)}, ${fmt(c2x)} ${fmt(c2y)}, ${fmt(p2.x)} ${fmt(p2.y)}`;
      }
      return d;
    })
    .join(' ');
}

/** Габарит набора точек с отступом. */
/** Габариты набора точек. @param {Array} [points] @param {number} [pad] @returns {object} */
export function bbox(points = [], pad = 0) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    minX = Math.min(minX, p.x);
    maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }
  if (minX === Infinity) return { minX: 0, minY: 0, maxX: 0, maxY: 0, width: 0, height: 0 };
  return {
    minX: minX - pad,
    minY: minY - pad,
    maxX: maxX + pad,
    maxY: maxY + pad,
    width: maxX - minX + pad * 2,
    height: maxY - minY + pad * 2,
  };
}

/** Диапазон чисел серий (скаттер-точки-объекты игнорируются). */
/** min/max по всем сериям. @param {Array} [series] @returns {{min:number,max:number}} */
export function seriesExtent(series = []) {
  let min = Infinity;
  let max = -Infinity;
  for (const s of Array.isArray(series) ? series : []) {
    for (const v of (s && s.data) || []) {
      if (v && typeof v === 'object') continue;
      const n = Number(v);
      if (!Number.isFinite(n)) continue;
      min = Math.min(min, n);
      max = Math.max(max, n);
    }
  }
  if (min === Infinity) return { min: 0, max: 0, empty: true };
  return { min, max, empty: false };
}

/** Диапазоны x/y для скаттера. */
/** min/max по точкам scatter. @param {Array} [series] @returns {object} */
export function scatterExtent(series = []) {
  let xMin = Infinity;
  let xMax = -Infinity;
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const s of Array.isArray(series) ? series : []) {
    for (const p of (s && s.data) || []) {
      if (!p || typeof p !== 'object') continue;
      const x = Number(p.x);
      const y = Number(p.y);
      if (Number.isFinite(x)) {
        xMin = Math.min(xMin, x);
        xMax = Math.max(xMax, x);
      }
      if (Number.isFinite(y)) {
        yMin = Math.min(yMin, y);
        yMax = Math.max(yMax, y);
      }
    }
  }
  const ok = xMin !== Infinity && yMin !== Infinity;
  return ok
    ? { xMin, xMax, yMin, yMax, empty: false }
    : { xMin: 0, xMax: 1, yMin: 0, yMax: 1, empty: true };
}

/** Индексы подписей оси X, прореженные до maxLabels (последний всегда включён). */
/** Прореживание подписей оси. @param {number} count @param {number} maxLabels @returns {number[]} */
export function indexTicks(count, maxLabels) {
  const n = Math.max(0, Math.floor(Number(count) || 0));
  if (!n) return [];
  const limit = Math.max(1, Math.floor(Number(maxLabels) || 1));
  const step = Math.max(1, Math.ceil(n / limit));
  const out = [];
  for (let i = 0; i < n; i += step) out.push(i);
  if (out[out.length - 1] !== n - 1) out.push(n - 1);
  return out;
}

/** Обрезка строки с многоточием. */
/** Обрезка подписи с многоточием. @param {string} text @param {number} maxChars @returns {string} */
export function truncate(text, maxChars) {
  const t = String(text ?? '');
  const max = Math.floor(Number(maxChars));
  if (!Number.isFinite(max) || max <= 0) return '';
  if (t.length <= max) return t;
  if (max <= 1) return '…';
  return `${t.slice(0, max - 1).trimEnd()}…`;
}

/** Тип единицы измерения → способ форматирования. */
/** Тип единицы измерения. @param {string} unit @returns {string} */
export function unitKind(unit) {
  const u = String(unit || '').trim();
  if (!u) return 'number';
  if (/^(R\$|BRL|USD|\$|€|руб\.?|₽)$/i.test(u)) return 'money';
  if (u === '%' || /процент/i.test(u)) return 'percent';
  return 'count';
}

/** Приблизительное число символов, влезающих в width при fontSize px. */
/** Сколько символов влезает в ширину. @param {number} width @param {number} [fontSize] @returns {number} */
export function charsForWidth(width, fontSize = 11) {
  const per = Math.max(4.5, fontSize * 0.56);
  return Math.max(1, Math.floor(Number(width) / per));
}
