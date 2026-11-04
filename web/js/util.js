/**
 * Shared UI utilities for the Team Room app.
 * Pure ES module, no dependencies. All user-visible strings are Russian.
 *
 * Safety rules baked in here:
 *  - esc() is the only way text reaches HTML; it escapes & < > " '
 *  - h()/s() build DOM nodes with createTextNode/appendChild, NEVER innerHTML
 */

/* ------------------------------------------------------------------ escaping */

/**
 * Escape a value for safe interpolation into HTML text or an attribute value.
 * null/undefined become ''. Escapes: & < > " '
 * @param {*} s
 * @returns {string}
 */
export function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, (c) => (
    c === '&' ? '&amp;'
      : c === '<' ? '&lt;'
        : c === '>' ? '&gt;'
          : c === '"' ? '&quot;'
            : '&#39;'
  ));
}

/* ---------------------------------------------------------------------- dates */

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

/** Two-digit zero pad. */
function pad2(n) {
  return n < 10 ? '0' + n : String(n);
}

/**
 * Coerce epoch-seconds | epoch-ms | ISO string | Date into a valid Date (local).
 * Returns null for junk. Date-only ISO strings are parsed as LOCAL dates
 * (the JS spec would treat them as UTC and shift the day in negative offsets).
 * @param {number|string|Date|null|undefined} x
 * @returns {Date|null}
 */
export function toDate(x) {
  if (x === null || x === undefined || x === '') return null;
  if (x instanceof Date) return isNaN(x.getTime()) ? null : x;
  if (typeof x === 'number') {
    if (!isFinite(x)) return null;
    const ms = Math.abs(x) >= 1e11 ? x : x * 1000; // seconds vs ms
    const d = new Date(ms);
    return isNaN(d.getTime()) ? null : d;
  }
  const s = String(x).trim();
  if (!s) return null;
  if (/^-?\d+(\.\d+)?$/.test(s)) return toDate(Number(s));
  let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) return new Date(+m[1], +m[2] - 1, +m[3]);
  m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(s);
  if (m && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) {
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

/**
 * '28 окт 2026'
 * @param {number|string|Date} x
 * @returns {string}
 */
export function fmtDate(x) {
  const d = toDate(x);
  if (!d) return '';
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

/**
 * '28 окт, 09:31'
 * @param {number|string|Date} x
 * @returns {string}
 */
export function fmtDateTime(x) {
  const d = toDate(x);
  if (!d) return '';
  return `${d.getDate()} ${MONTHS[d.getMonth()]}, ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/**
 * Human relative time: 'только что' | '5 мин назад' | '2 ч назад' | 'вчера'
 * | '3 дн назад' (>=30 days falls back to fmtDate).
 * @param {number|string|Date} x
 * @param {number|string|Date} [now] reference moment, default Date.now()
 * @returns {string}
 */
export function timeAgo(x, now) {
  const d = toDate(x);
  if (!d) return '';
  const refRaw = now === undefined || now === null ? Date.now() : now;
  const ref = typeof refRaw === 'number' ? refRaw : (toDate(refRaw) || { getTime: () => Date.now() });
  const refMs = typeof ref === 'number' ? ref : ref.getTime();
  let sec = Math.floor((refMs - d.getTime()) / 1000);
  if (!isFinite(sec) || sec < 0) sec = 0;
  if (sec < 60) return 'только что';
  if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} ч назад`;
  const days = Math.floor(sec / 86400);
  if (days === 1) return 'вчера';
  if (days < 30) return `${days} дн назад`;
  return fmtDate(d);
}

/**
 * '45 с' | '12 мин' | '1 ч 12 мин' | '2 дн 3 ч'
 * @param {number} sec
 * @returns {string}
 */
export function fmtDuration(sec) {
  const raw = Number(sec);
  if (!isFinite(raw) || raw < 0) return '—';
  const t = Math.floor(raw);
  if (t < 60) return `${t} с`;
  if (t < 3600) return `${Math.floor(t / 60)} мин`;
  const days = Math.floor(t / 86400);
  const hours = Math.floor((t % 86400) / 3600);
  if (days > 0) return hours ? `${days} дн ${hours} ч` : `${days} дн`;
  const mins = Math.floor((t % 3600) / 60);
  return mins ? `${hours} ч ${mins} мин` : `${hours} ч`;
}

/* ------------------------------------------------------------------ numbers */

/**
 * Dev-режим: приложение крутится на локальном хосте, там и ловим неверные
 * сигнатуры. В тестах (Node, нет location) — молчим, чтобы не шуметь.
 */
const DEV = (() => {
  try {
    const host = globalThis.location && globalThis.location.hostname;
    return host === '127.0.0.1' || host === 'localhost' || host === '::1' || host === '[::1]';
  } catch { return false; }
})();

const signatureWarned = new Set();

/** Один раз на функцию подсказать правильную сигнатуру (только dev). */
function warnSignature(fn, hint) {
  if (!DEV || signatureWarned.has(fn)) return;
  signatureWarned.add(fn);
  if (typeof console !== 'undefined' && typeof console.warn === 'function') {
    console.warn('[util] ' + fn + ': ' + hint);
  }
}

/**
 * Второй аргумент — объект опций; число принимаем как сокращение (совместимость
 * с позиционным вызовом), всё остальное — ошибка типа.
 * @param {string} fn имя функции для сообщения
 * @param {*} opts
 * @param {string} key куда положить число
 * @returns {object}
 */
function normalizeOpts(fn, opts, key = 'decimals') {
  if (opts === undefined || opts === null) return {};
  if (typeof opts === 'number') {
    warnSignature(fn, `позиционный аргумент устарел, пишите {${key}: ${opts}}`);
    return { [key]: opts };
  }
  if (typeof opts === 'object') return opts;
  throw new TypeError(fn + ': опции — объект {' + key + ': number}, получено ' + typeof opts);
}

/**
 * Russian plural form picker: 1 задача / 2 задачи / 5 задач, 11-14 → many.
 * Формы — массивом: plural(n, ['задача', 'задачи', 'задач']).
 * Позиционная форма plural(n, one, few, many) тоже работает (с предупреждением в dev).
 * @param {number} n
 * @param {string[]|string} forms [one, few, many]
 * @param {...string} [rest] хвост позиционной формы
 * @returns {string}
 */
export function plural(n, forms, ...rest) {
  let list = forms;
  if (!Array.isArray(forms) && forms !== undefined && forms !== null) {
    if (typeof forms !== 'string' || rest.some((x) => typeof x !== 'string')) {
      throw new TypeError("plural(n, [one, few, many]): формы должны быть массивом строк");
    }
    list = [forms, ...rest];
    warnSignature('plural', 'формы передаются массивом: plural(n, [one, few, many])');
  }
  const f = Array.isArray(list) ? list : [];
  const one = f[0] !== undefined ? f[0] : '';
  const few = f[1] !== undefined ? f[1] : one;
  const many = f[2] !== undefined ? f[2] : few;
  const num = Math.abs(Number(n));
  if (!isFinite(num)) return many;
  const i = Math.floor(num);
  const mod10 = i % 10;
  const mod100 = i % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

export { fromForm, fromCount, plainTerms } from './text-ru.js';

/** Insert a space every three digits. */
function groupThousands(digits) {
  let out = '';
  let c = 0;
  for (let i = digits.length - 1; i >= 0; i--) {
    out = digits[i] + out;
    c++;
    if (c % 3 === 0 && i > 0) out = ' ' + out;
  }
  return out;
}

/** Turn '1234.5' into '1 234,5' (sign preserved, fraction trimmed when asked). */
function localizeNumberString(raw, stripTrailing) {
  let s = String(raw);
  if (/e/i.test(s)) return s.replace('.', ',');
  let sign = '';
  if (s[0] === '-') { sign = '-'; s = s.slice(1); }
  let [int, frac] = s.split('.');
  int = int || '0';
  if (frac && stripTrailing) frac = frac.replace(/0+$/, '');
  if (!/([1-9])/.test(int + (frac || ''))) sign = ''; // '-0,00' → '0,00'
  return sign + groupThousands(int) + (frac ? ',' + frac : '');
}

/**
 * Number with space thousands separator and comma decimals.
 * No `decimals` → up to 3 fraction digits, trailing zeros trimmed.
 * @param {number|string} n
 * @param {{decimals?: number}} [opts] (число = сокращение для {decimals})
 * @returns {string} '1 234,50' | '—' for non-finite input
 */
export function fmtNumber(n, opts = {}) {
  const v = typeof n === 'string' ? Number(n.replace(/\s/g, '').replace(',', '.')) : Number(n);
  if (!isFinite(v)) return '—';
  const optsObj = normalizeOpts('fmtNumber', opts);
  const hasDecimals = optsObj.decimals !== undefined && optsObj.decimals !== null;
  let s;
  if (hasDecimals) {
    const d = Math.max(0, Math.min(6, Math.floor(Number(optsObj.decimals)) || 0));
    s = Math.abs(v) >= 1e21 ? String(v) : v.toFixed(d);
  } else {
    if (Math.abs(v) >= 1e21) return String(v);
    s = String(Math.round(v * 1000) / 1000);
  }
  return localizeNumberString(s, !hasDecimals);
}

const CURRENCIES = {
  BRL: { symbol: 'R$', pos: 'prefix' },
  USD: { symbol: '$', pos: 'prefix' },
  RUB: { symbol: '₽', pos: 'suffix' },
  EUR: { symbol: '€', pos: 'suffix' },
};

/**
 * Money: 'R$ 1 234,50' | '1 234,50 ₽'. Default currency BRL, default 2 decimals.
 * @param {number} n
 * @param {{currency?: 'BRL'|'RUB'|'USD'|'EUR'|string, decimals?: number}} [opts]
 * @returns {string}
 */
export function fmtMoney(n, opts = {}) {
  const o = opts || {};
  const code = String(o.currency || 'BRL').toUpperCase();
  const meta = CURRENCIES[code] || { symbol: code, pos: 'suffix' };
  const decimals = o.decimals === undefined || o.decimals === null ? 2 : o.decimals;
  const num = fmtNumber(n, { decimals });
  if (num === '—') return '—';
  const neg = num.startsWith('-');
  const body = neg ? num.slice(1) : num;
  const joined = meta.pos === 'prefix' ? `${meta.symbol} ${body}` : `${body} ${meta.symbol}`;
  return neg ? '-' + joined : joined;
}

/**
 * Percent: '12%' | '12,5%'. `ratio: true` multiplies by 100 first.
 * @param {number} x
 * @param {{decimals?: number, ratio?: boolean}} [opts] (число = {decimals})
 * @param {boolean} [ratio] позиционная форма: fmtPercent(0.42, 1, true) → '42,0%'
 * @returns {string}
 */
export function fmtPercent(x, opts = {}, ratio) {
  const positional = typeof opts === 'number';
  const o = normalizeOpts('fmtPercent', opts);
  const useRatio = positional && ratio !== undefined ? !!ratio : !!o.ratio;
  const n = Number(x);
  if (!isFinite(n)) return '—';
  const v = useRatio ? n * 100 : n;
  const decimals = o.decimals === undefined || o.decimals === null ? 0 : o.decimals;
  return fmtNumber(v, { decimals }) + '%';
}

/* ------------------------------------------------------------------- helpers */

/**
 * Trailing-edge debounce. Returned function has .cancel() and .flush().
 * @param {Function} fn
 * @param {number} [ms]
 * @returns {Function & {cancel: Function, flush: Function}}
 */
export function debounce(fn, ms = 100) {
  let timer = null;
  let lastArgs = null;
  let lastThis = null;
  const delay = Math.max(0, Number(ms) || 0);
  function invoke() {
    const args = lastArgs;
    const self = lastThis;
    timer = null;
    lastArgs = null;
    lastThis = null;
    if (args) fn.apply(self, args);
  }
  const wrapped = function debounced(...args) {
    lastArgs = args;
    lastThis = this;
    if (timer) clearTimeout(timer);
    timer = setTimeout(invoke, delay);
    return wrapped;
  };
  wrapped.cancel = () => {
    if (timer) clearTimeout(timer);
    timer = null;
    lastArgs = null;
    lastThis = null;
  };
  wrapped.flush = () => {
    if (timer) { clearTimeout(timer); invoke(); }
  };
  return wrapped;
}

/**
 * Clamp a number into [a, b].
 * @param {number} n
 * @param {number} a
 * @param {number} b
 * @returns {number}
 */
export function clamp(n, a, b) {
  const v = Number(n);
  if (!isFinite(v)) return Number(a) || 0;
  return Math.min(Math.max(v, Math.min(a, b)), Math.max(a, b));
}

let uidSeq = 0;

/**
 * Unique, CSS-selector-safe id: 'md-lq3k2a4x1b2c'.
 * @param {string} [prefix]
 * @returns {string}
 */
export function uid(prefix = 'id') {
  uidSeq = (uidSeq + 1) % 46656; // 36^3
  const p = String(prefix || 'id').replace(/[^A-Za-z0-9_-]/g, '');
  const t = Date.now().toString(36).slice(-6);
  const r = Math.random().toString(36).slice(2, 6);
  return `${p || 'id'}-${t}${uidSeq.toString(36)}${r}`;
}

/**
 * Compact display form of a kanban id: 't_605613e0' stays as is,
 * long ids are trimmed to their prefix + 8 chars, then '…'.
 * @param {string} id
 * @returns {string}
 */
export function shortId(id) {
  const s = String(id === null || id === undefined ? '' : id);
  if (!s) return '';
  const m = /^([A-Za-z]{1,4})_(.+)$/.exec(s);
  if (m && m[2].length > 8) return `${m[1]}_${m[2].slice(0, 8)}`;
  if (s.length > 12) return s.slice(0, 8) + '…';
  return s;
}

/**
 * Avatar initials: 'Михаил Петров' → 'МП', 'lawyer' → 'LA', '' → '?'.
 * @param {string} name
 * @returns {string}
 */
export function initials(name) {
  const s = String(name === null || name === undefined ? '' : name).trim();
  if (!s) return '?';
  const parts = s.split(/[\s._\-/\\–—]+/).filter(Boolean);
  const chars = (w) => Array.from(w);
  const first = (w) => (chars(w)[0] || '');
  if (parts.length >= 2) return (first(parts[0]) + first(parts[1])).toUpperCase();
  const one = Array.from(parts[0] || s);
  const take = one.length >= 2 && /[\p{L}\p{N}]/u.test(one[0]) ? 2 : 1;
  return one.slice(0, take).join('').toUpperCase();
}

/**
 * Deterministic pastel-safe HSL colour for a name (avatars, chips).
 * @param {string} name
 * @returns {string} e.g. 'hsl(214 58% 46%)'
 */
export function hashColor(name) {
  const s = String(name === null || name === undefined ? '' : name);
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  const hue = Math.round(((h % 1000) / 1000) * 360);
  return `hsl(${hue} 58% 46%)`;
}

/**
 * Group array items by key. `key` is a property name or a (item) => key function.
 * Safe for '__proto__' keys.
 * @template T
 * @param {T[]} arr
 * @param {string|Function} key
 * @returns {Record<string, T[]>}
 */
export function byKey(arr, key) {
  const out = {};
  if (!Array.isArray(arr)) return out;
  const getter = typeof key === 'function'
    ? key
    : (item) => (key === null || key === undefined ? item : (item == null ? undefined : item[key]));
  for (const item of arr) {
    const raw = getter(item);
    const k = raw === null || raw === undefined ? '' : String(raw);
    if (!Object.prototype.hasOwnProperty.call(out, k)) {
      Object.defineProperty(out, k, { value: [], writable: true, enumerable: true, configurable: true });
    }
    out[k].push(item);
  }
  return out;
}

/* ----------------------------------------------------------------------- DOM */

const SVG_NS = 'http://www.w3.org/2000/svg';
const SVG_TAGS = new Set([
  'svg', 'g', 'path', 'circle', 'rect', 'line', 'text', 'polyline', 'polygon',
  'defs', 'marker', 'title', 'tspan', 'ellipse', 'use', 'lineargradient',
  'radialgradient', 'stop', 'clippath', 'mask', 'pattern', 'symbol', 'desc',
  'textpath', 'image', 'filter', 'fegaussianblur', 'foreignobject',
]);

/** Resolve the global document lazily so tests can inject a fake DOM. */
function doc() {
  const d = globalThis.document;
  if (!d || typeof d.createElement !== 'function') {
    throw new Error('util.h(): no document — inject globalThis.document before calling');
  }
  return d;
}

function isNode(v) {
  return v !== null && typeof v === 'object' && typeof v.nodeType === 'number';
}

/** Is `v` an attribute bag (and not a child)? */
function isAttrs(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v) && !isNode(v)
    && !(v instanceof Date);
}

function toClass(v) {
  if (v === null || v === undefined || v === false) return '';
  if (Array.isArray(v)) return v.filter(Boolean).join(' ');
  if (typeof v === 'object') return Object.keys(v).filter((k) => v[k]).join(' ');
  return String(v);
}

function applyStyle(el, style) {
  if (style === null || style === undefined || style === false) return;
  if (typeof style === 'string') { el.setAttribute('style', style); return; }
  if (typeof style !== 'object') return;
  const st = el.style;
  if (!st) return;
  for (const [rawKey, rawVal] of Object.entries(style)) {
    if (rawVal === null || rawVal === undefined || rawVal === false) continue;
    const val = String(rawVal);
    if (rawKey === 'cssText') { st.cssText = val; continue; }
    if (rawKey.startsWith('--')) {
      if (typeof st.setProperty === 'function') st.setProperty(rawKey, val);
      else st[rawKey] = val;
      continue;
    }
    const key = rawKey.includes('-')
      ? rawKey.replace(/-([a-z])/g, (_, c) => c.toUpperCase())
      : rawKey;
    st[key] = val;
  }
}

/** Attributes that would inject HTML and therefore have no business in h(). */
const FORBIDDEN_ATTRS = new Set(['innerhtml', 'outerhtml', 'srcdoc', 'dangerouslysethtml']);

function applyAttrs(el, attrs) {
  if (!attrs) return;
  for (const [key, val] of Object.entries(attrs)) {
    if (val === null || val === undefined || val === false) continue;
    // F-5: HTML в атрибут — всегда ошибка. setAttribute('innerHTML') молча
    // создаёт мёртвый атрибут вместо узлов (так и потерялся текст отчёта),
    // поэтому падаем громко. Единственный разрешённый путь HTML — md-node.js.
    if (FORBIDDEN_ATTRS.has(String(key).toLowerCase())) {
      throw new Error(
        `h(): атрибут "${key}" запрещён — h() строит узлы, а не HTML. `
        + 'Для markdown отчёта используйте report/md-node.js (mdNode).',
      );
    }
    if (key === 'class' || key === 'className') {
      const cls = toClass(val);
      if (cls) el.setAttribute('class', cls);
      continue;
    }
    if (key === 'style') { applyStyle(el, val); continue; }
    if (key === 'dataset') {
      if (typeof val !== 'object') continue;
      for (const [dk, dv] of Object.entries(val)) {
        if (dv === null || dv === undefined || dv === false) continue;
        const name = 'data-' + String(dk).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
        el.setAttribute(name, dv === true ? '' : String(dv));
      }
      continue;
    }
    if (/^on[a-z]+$/i.test(key)) {
      if (typeof val !== 'function') continue;
      const type = key.slice(2).toLowerCase();
      if (typeof el.addEventListener === 'function') el.addEventListener(type, val);
      else el[key] = val;
      continue;
    }
    if (typeof val === 'function') continue;
    el.setAttribute(key, val === true ? '' : String(val));
  }
}

function appendChildren(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) { appendChildren(el, child); continue; }
    if (isNode(child)) { el.appendChild(child); continue; }
    const t = typeof child;
    if (t === 'string') {
      if (child === '') continue;
      el.appendChild(doc().createTextNode(child));
      continue;
    }
    if (t === 'number' || t === 'bigint') {
      el.appendChild(doc().createTextNode(String(child)));
    }
  }
}

/** Normalize (attrs?, ...children) overloading. */
function splitArgs(attrs, children) {
  if (isAttrs(attrs)) return { attrs, kids: children };
  if (attrs === undefined || attrs === null) return { attrs: null, kids: children };
  return { attrs: null, kids: [attrs, ...children] };
}

/**
 * Build an HTML element. Text children become text nodes — innerHTML is never used.
 *
 * attrs: class/className (string | array | {name:bool}), dataset{}, style
 * (string | object, kebab or camel keys, `--vars` supported), on* (functions →
 * addEventListener), true → empty attribute, false/null/undefined → skipped,
 * everything else → String() attribute.
 * children: strings/numbers → text nodes, Nodes appended as is, arrays flattened,
 * null/undefined/false/true skipped.
 *
 * @param {string} tag
 * @param {Object|Node|string} [attrs]
 * @param {...*} children
 * @returns {HTMLElement}
 */
export function h(tag, attrs, ...children) {
  const name = String(tag === null || tag === undefined || tag === '' ? 'div' : tag);
  const { attrs: a, kids } = splitArgs(attrs, children);
  const el = SVG_TAGS.has(name.toLowerCase())
    ? s(name, a, ...kids)
    : doc().createElement(name);
  applyAttrs(el, a);
  appendChildren(el, kids);
  return el;
}

/**
 * Build an SVG element (any tag) in the SVG namespace.
 * @param {string} tag
 * @param {Object|Node|string} [attrs]
 * @param {...*} children
 * @returns {SVGElement}
 */
export function s(tag, attrs, ...children) {
  const name = String(tag === null || tag === undefined || tag === '' ? 'g' : tag);
  const { attrs: a, kids } = splitArgs(attrs, children);
  const d = doc();
  const el = typeof d.createElementNS === 'function'
    ? d.createElementNS(SVG_NS, name)
    : d.createElement(name);
  applyAttrs(el, a);
  appendChildren(el, kids);
  return el;
}

export const SVG_NAMESPACE = SVG_NS;
