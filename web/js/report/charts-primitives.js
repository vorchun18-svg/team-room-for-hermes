// charts-primitives.js — общие кирпичи SVG-графиков: палитра, формат чисел,
// текстовые узлы и корень <svg>. Вынесено из charts-draw.js (лимит §10.5 ≤ 600 строк).
// Зависимости только вниз: util.js и charts-math.js.

import { h, s, fmtNumber, fmtMoney, fmtPercent } from '../util.js';
import { unitKind, truncate } from './charts-math.js';

const FALLBACK = ['#4c7dff', '#e07a3f', '#2f9e6a', '#9a6ad6', '#c4a02a', '#2f9fb5', '#cf5c86', '#6c7a8c'];
export const COLORS = FALLBACK.map((c, i) => `var(--chart-${i + 1}, ${c})`);

export const px = (n) => Math.round(Number(n) * 100) / 100;


export function resolveColor(color, index) {
  if (!color) return COLORS[index % COLORS.length];
  const c = String(color).trim();
  if (c.startsWith('--')) return `var(${c})`;
  return c;
}

function tryCall(fn, args, fallback) {
  try {
    const out = typeof fn === 'function' ? fn(...args) : null;
    return out == null || out === '' ? fallback : String(out);
  } catch {
    return fallback;
  }
}

/** Код валюты по единице измерения оси. `R$`/`BRL` проверяются ПЕРВЫМИ: в «R$»
 *  есть «$», и раньше он превращался в доллар. Неизвестное — реалы (BRL). */
export function currencyOf(unit) {
  const u = String(unit || '').trim();
  if (/R\$|BRL/i.test(u)) return 'BRL';
  if (/USD|\$/i.test(u)) return 'USD';
  if (/EUR|€/.test(u)) return 'EUR';
  if (/₽|RUB|руб/i.test(u)) return 'RUB';
  return 'BRL';
}

export function makeFormatter(unit) {
  const kind = unitKind(unit);
  const currency = currencyOf(unit);
  const plain = (n, d) =>
    n.toLocaleString('ru-RU', { maximumFractionDigits: d == null ? 2 : d, minimumFractionDigits: 0 });
  return function format(value, decimals, withUnit = false) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    if (kind === 'percent') {
      const out = tryCall(fmtPercent, [n, { decimals: decimals ?? 1, ratio: false }], null);
      return out ?? `${plain(n, decimals ?? 1)} %`;
    }
    if (kind === 'money') {
      const out = tryCall(fmtMoney, [n, { currency, decimals: decimals ?? 2 }], null);
      return out ?? `R$ ${plain(n, decimals ?? 2)}`;
    }
    const out = tryCall(fmtNumber, [n, decimals == null ? {} : { decimals }], null) ?? plain(n, decimals);
    return withUnit && unit ? `${out} ${unit}` : out;
  };
}

export function isFiniteNumber(v) {
  return typeof v === 'number' ? Number.isFinite(v) : v != null && v !== '' && Number.isFinite(Number(v));
}

export function svgText(x, y, text, attrs = {}) {
  return s('text', { x: px(x), y: px(y), ...attrs }, text);
}

export function labelWithTitle(x, y, text, maxChars, attrs = {}) {
  const short = truncate(text, maxChars);
  const el = svgText(x, y, short, attrs);
  if (short !== text) el.append(s('title', {}, text));
  return el;
}

export function emptyBox() {
  return h(
    'div',
    { class: 'rc-empty', role: 'note' },
    h('span', { class: 'rc-empty__mark', 'aria-hidden': 'true' }, '—'),
    h('span', {}, 'Нет данных'),
  );
}

export function svgRoot(width, height, titleId, descId, desc) {
  return s(
    'svg',
    {
      class: 'rc-svg',
      viewBox: `0 0 ${px(width)} ${px(height)}`,
      width: '100%',
      height: px(height),
      role: 'img',
      'aria-labelledby': `${titleId} ${descId}`,
      preserveAspectRatio: 'xMidYMid meet',
    },
    s('title', { id: titleId }, desc.title),
    s('desc', { id: descId }, desc.text),
  );
}
