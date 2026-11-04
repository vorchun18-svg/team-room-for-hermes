// Чистая логика таблиц отчёта: сортировка, фильтр, итоги, форматирование, CSV.
// Никакого DOM — модуль импортируется в node-тестах (tests/report-table.test.mjs).

const RU = 'ru-RU';
const NUMERIC_TYPES = new Set(['number', 'money', 'percent']);

/* ---------- значения ---------- */

/** Пустое ли значение. @param {*} v @returns {boolean} */
export function isBlank(v) {
  return v === null || v === undefined || v === '' || (typeof v === 'number' && Number.isNaN(v));
}

/** Число из строки «1 234,56», «R$ 1.234,56», «12%», 1234.5. */
/** Строка/число → число (пробелы, запятая, валюта). @param {*} v @returns {number|null} */
export function parseNumber(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (isBlank(v)) return null;
  let s = String(v).trim();
  if (!s) return null;
  s = s.replace(/[^\d,.\-+eE]/g, '');
  if (!s || s === '-' || s === '+') return null;
  const lastComma = s.lastIndexOf(',');
  const lastDot = s.lastIndexOf('.');
  if (lastComma > -1 && lastDot > -1) {
    // десятичный — тот, что правее; прочие считаем разделителями групп
    if (lastComma > lastDot) s = s.replace(/\./g, '').replace(',', '.');
    else s = s.replace(/,/g, '');
  } else if (lastComma > -1) {
    const parts = s.split(',');
    s = parts.length === 2 && parts[1].length !== 3 ? s.replace(',', '.')
      : parts.every((p, i) => i === 0 || p.length === 3) ? s.replace(/,/g, '') : s.replace(',', '.');
  } else if (lastDot > -1) {
    const parts = s.split('.');
    if (parts.length > 2) s = parts.join('');
    else if (parts[1].length === 3 && parts[0].length <= 3) s = parts.join(''); // 1.234 → 1234
  }
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** Дата из ISO / «28.10.2026» / «28.10.2026 14:05» / Date. null — если не разобрать. */
/** ISO/русская/Date → Date или null. @param {*} v @returns {Date|null} */
export function parseDate(v) {
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v;
  if (isBlank(v)) return null;
  if (typeof v === 'number') return null;
  const s = String(v).trim();
  let m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(s);
  if (m) {
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]),
      m[4] ? Number(m[4]) : 0, m[5] ? Number(m[5]) : 0);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ ,]+(\d{1,2}):(\d{2}))?/.exec(s);
  if (m) {
    const d = new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1]),
      m[4] ? Number(m[4]) : 0, m[5] ? Number(m[5]) : 0);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  return null;
}

/* ---------- форматирование ---------- */

function nf(decimals) {
  const d = decimals === undefined || decimals === null ? null : Number(decimals);
  return new Intl.NumberFormat(RU, {
    minimumFractionDigits: d === null ? 0 : Math.max(0, d),
    maximumFractionDigits: d === null ? 2 : Math.max(0, d),
  });
}

/** Число с разделителями. @param {*} value @param {number} [decimals] @returns {string} */
export function formatNumber(value, decimals) {
  if (isBlank(value)) return '';
  const n = parseNumber(value);
  if (n === null) return String(value);
  return nf(decimals).format(n);
}

const MONEY_SYMBOL = { BRL: 'R$', RUB: '₽', USD: '$', EUR: '€' };
//: Где стоит знак: у реалов и долларов — перед числом (`R$ 42,00`), у ₽/€ — после.
const MONEY_PREFIX = new Set(['BRL', 'USD', 'R$', '$', 'R', 'BRL$']);

/** Деньги с валютой. По умолчанию реалы (BRL) — валюта проекта.
 *  `formatMoney(42, 'BRL')` → `R$ 42,00`, `formatMoney(42, 'RUB')` → `42,00 ₽`.
 *  @param {*} value @param {string} [currency] @param {number} [decimals] @returns {string} */
export function formatMoney(value, currency = 'BRL', decimals) {
  if (isBlank(value)) return '';
  const n = parseNumber(value);
  if (n === null) return String(value);
  const raw = String(currency == null || currency === '' ? 'BRL' : currency).trim();
  const cur = raw.toUpperCase();
  const symbol = MONEY_SYMBOL[cur] || raw;
  const frac = decimals === undefined || decimals === null ? 2 : Math.max(0, Number(decimals));
  const body = new Intl.NumberFormat(RU, {
    minimumFractionDigits: frac, maximumFractionDigits: frac,
  }).format(n);
  return MONEY_PREFIX.has(cur) ? `${symbol} ${body}` : `${body} ${symbol}`;
}

/** Проценты. @param {*} value @param {{decimals?:number, ratio?:boolean}} [opts] @returns {string} */
export function formatPercent(value, { decimals = 1, ratio = false } = {}) {
  if (isBlank(value)) return '';
  const n = parseNumber(value);
  if (n === null) return String(value);
  const p = ratio ? n * 100 : n;
  return `${nf(decimals).format(p)}%`;
}

/** Дата ДД.ММ.ГГГГ. @param {*} value @returns {string} */
export function formatDate(value) {
  const d = parseDate(value);
  if (!d) return isBlank(value) ? '' : String(value);
  const pad = (x) => String(x).padStart(2, '0');
  const hasTime = /[T ]\d{2}:\d{2}/.test(String(value));
  const base = `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
  return hasTime ? `${base} ${pad(d.getHours())}:${pad(d.getMinutes())}` : base;
}

const TONE_RULES = [
  ['ok', /(^|\W)(done|ok|success|готов|выполнен|принят|оплачен|закрыт|завершен|завершён|подписан|успеш)/i],
  ['danger', /(^|\W)(blocked|fail|failed|danger|error|reject|отказ|ошибк|просроч|блок|провал|риск-крит)/i],
  ['warn', /(^|\W)(warn|wait|waiting|pending|todo|ждёт|ждет|ожида|вним|риск|скоро|черновик|draft)/i],
  ['info', /(^|\W)(active|work|in.?progress|new|info|в работе|актив|в процессе|начат)/i],
];

/** Тон статуса по тексту: ok | warn | danger | info | neutral. */
/** Тон статуса (ok|warn|danger|muted). @param {*} value @returns {string} */
export function statusTone(value) {
  const s = String(value ?? '');
  for (const [tone, re] of TONE_RULES) if (re.test(s)) return tone;
  return 'neutral';
}

/** Отображаемый текст значения (без HTML). */
/** Значение ячейки по типу колонки. @param {*} value @param {object} [column] @returns {string} */
export function formatValue(value, column = {}) {
  const type = column.type || 'text';
  switch (type) {
    case 'number': return formatNumber(value, column.decimals ?? 0); // SPEC: number по умолчанию без дробей
    case 'money': return formatMoney(value, column.currency, column.decimals);
    case 'percent': return formatPercent(value, { decimals: column.decimals ?? 1, ratio: !!column.ratio });
    case 'date': return formatDate(value);
    case 'link': {
      if (value && typeof value === 'object') return String(value.title ?? value.label ?? value.href ?? value.url ?? '');
      return String(value ?? '');
    }
    case 'status': return String(value ?? '');
    default: return value === null || value === undefined ? '' : String(value);
  }
}

/* ---------- сортировка ---------- */

function sortKeyOf(value, type) {
  if (isBlank(value)) return null;
  if (NUMERIC_TYPES.has(type)) return parseNumber(value);
  if (type === 'date') {
    const d = parseDate(value);
    return d ? d.getTime() : null;
  }
  if (type === 'link' && value && typeof value === 'object') {
    return String(value.title ?? value.label ?? value.href ?? '');
  }
  return String(value);
}

/** Сравнение значений колонки; пустые всегда в конце. */
/** Сравнение двух значений по типу. @param {*} a @param {*} b @param {string} [type] @param {string} [dir] @returns {number} */
export function compareValues(a, b, type = 'text', dir = 'asc') {
  const ka = sortKeyOf(a, type);
  const kb = sortKeyOf(b, type);
  if (ka === null && kb === null) return 0;
  if (ka === null) return 1;
  if (kb === null) return -1;
  let r;
  if (typeof ka === 'number' && typeof kb === 'number') r = ka - kb;
  else r = String(ka).localeCompare(String(kb), RU, { numeric: true, sensitivity: 'base' });
  if (r === 0) return 0;
  return dir === 'desc' ? -r : r;
}

/**
 * Сортировка строк по колонке. Возвращает НОВЫЙ массив, стабильную сортировку,
 * пустые значения — в конце при любом направлении.
 */
/** Сортировка строк по колонке. @param {Array} rows @param {Array} columns @param {{key?:string, dir?:string}} [opts] @returns {Array} */
export function sortRows(rows, columns, { key, dir = 'asc' } = {}) {
  const list = Array.isArray(rows) ? rows.slice() : [];
  if (!key) return list;
  const col = (columns || []).find((c) => c.key === key) || { key, type: 'text' };
  const indexed = list.map((row, i) => ({ row, i }));
  indexed.sort((x, y) => {
    const a = x.row ? x.row[key] : undefined;
    const b = y.row ? y.row[key] : undefined;
    const blankA = sortKeyOf(a, col.type) === null;
    const blankB = sortKeyOf(b, col.type) === null;
    if (blankA !== blankB) return blankA ? 1 : -1;
    const r = compareValues(a, b, col.type, dir);
    return r !== 0 ? r : x.i - y.i;
  });
  return indexed.map((x) => x.row);
}

/* ---------- фильтр ---------- */

function norm(s) {
  return String(s ?? '')
    .toLowerCase()
    .replace(/ё/g, 'е')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Поиск по всем колонкам: все слова запроса должны найтись (И). */
/** Фильтр по подстроке во всех колонках. @param {Array} rows @param {Array} columns @param {string} query @returns {Array} */
export function filterRows(rows, columns, query) {
  const list = Array.isArray(rows) ? rows : [];
  const q = norm(query);
  if (!q) return list.slice();
  const tokens = q.split(' ').filter(Boolean);
  const compactQ = q.replace(/ /g, '');
  return list.filter((row) => {
    const raw = (columns || []).map((c) => row?.[c.key]);
    const shown = (columns || []).map((c) => formatValue(row?.[c.key], c));
    const hay = norm(raw.concat(shown).join(' '));
    const compact = hay.replace(/ /g, '');
    if (hay.includes(q) || compact.includes(compactQ)) return true;
    return tokens.every((t) => hay.includes(t) || compact.includes(t));
  });
}

/* ---------- итоги ---------- */

/**
 * Итоги по спецификации {key: 'sum'|'avg'|'count'}.
 * Возвращает {columnKey: {kind, value}}; value — число.
 */
/** Итоги по числовым колонкам. @param {Array} rows @param {Array} columns @param {Array|object} [totals] @returns {object} */
export function computeTotals(rows, columns, totals) {
  const out = {};
  if (!totals || typeof totals !== 'object') return out;
  const list = Array.isArray(rows) ? rows : [];
  for (const [key, kind] of Object.entries(totals)) {
    const col = (columns || []).find((c) => c.key === key);
    if (!col) continue;
    const nums = list.map((r) => parseNumber(r?.[key])).filter((n) => n !== null);
    if (kind === 'count') {
      out[key] = { kind, value: list.filter((r) => !isBlank(r?.[key])).length };
    } else if (kind === 'sum') {
      out[key] = { kind, value: nums.reduce((a, b) => a + b, 0) };
    } else if (kind === 'avg') {
      out[key] = { kind, value: nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : 0 };
    }
  }
  return out;
}

/** Подпись итога. @param {object} entry @param {object} column @returns {string} */
export function formatTotal(entry, column) {
  if (!entry) return '';
  if (entry.kind === 'count') return formatNumber(entry.value, 0);
  if (column.type === 'money') return formatMoney(entry.value, column.currency, column.decimals ?? 2);
  if (column.type === 'percent') return formatPercent(entry.value, { decimals: column.decimals ?? 1, ratio: !!column.ratio });
  return formatNumber(entry.value, column.decimals ?? (entry.kind === 'avg' ? 2 : 0));
}

/* ---------- страницы ---------- */

export const PAGE_LIMIT = 200;

/** Первые N строк (лимит отчёта). @param {Array} rows @param {number} [shown] @returns {Array} */
export function pageRows(rows, shown = PAGE_LIMIT) {
  const list = Array.isArray(rows) ? rows : [];
  const n = Math.max(0, Number(shown) || 0);
  return { rows: list.slice(0, n), total: list.length, hasMore: list.length > n };
}

/* ---------- CSV ---------- */

function csvCell(text, delim) {
  const s = String(text ?? '');
  return /["\n\r;,\t]/.test(s) || s.includes(delim) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** CSV видимых строк: разделитель ';' (Excel RU), BOM, CRLF. */
/** CSV с BOM и разделителем. @param {Array} columns @param {Array} rows @param {{delimiter?:string, bom?:boolean}} [opts] @returns {string} */
export function toCSV(columns, rows, { delimiter = ';', bom = true } = {}) {
  const cols = columns || [];
  const lines = [cols.map((c) => csvCell(c.label ?? c.key, delimiter)).join(delimiter)];
  for (const row of rows || []) {
    lines.push(cols.map((c) => csvCell(formatValue(row?.[c.key], c), delimiter)).join(delimiter));
  }
  return (bom ? '\uFEFF' : '') + lines.join('\r\n');
}