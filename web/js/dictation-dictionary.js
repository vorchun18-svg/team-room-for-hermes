/**
 * dictation-dictionary.js — чистая логика словаря диктовки, без DOM.
 *
 * Здесь всё, что можно проверить в node: разбор и проверка словаря, применение
 * автозамен к фразе (те же правила, что у сервера) и человеческие подписи
 * состояния распознавания. Раздел настроек только рисует.
 */

export const MAX_TERMS = 300;
export const MAX_REPLACEMENTS = 200;
export const MAX_WORD = 60;

/* ---------------------------------------------------------------- слова */

export function cleanTerm(s) {
  return String(s === null || s === undefined ? '' : s).replace(/\s+/g, ' ').trim();
}

/** Одно ли это слово: без регистра и разницы ё/е. */
export function sameWord(a, b) {
  return cleanTerm(a).toLowerCase().replace(/ё/g, 'е') === cleanTerm(b).toLowerCase().replace(/ё/g, 'е');
}

/** Привести словарь с сервера или из файла к безопасному виду. */
export function normalizeDictionary(obj) {
  const src = obj && typeof obj === 'object' ? obj : {};
  const terms = [];
  for (const raw of Array.isArray(src.terms) ? src.terms : []) {
    const t = cleanTerm(raw);
    if (!t || t.length > MAX_WORD) continue;
    if (terms.some((x) => sameWord(x, t))) continue;
    terms.push(t);
  }
  const replacements = [];
  for (const raw of Array.isArray(src.replacements) ? src.replacements : []) {
    const from = cleanTerm(raw && raw.from);
    const to = cleanTerm(raw && raw.to);
    if (!from || !to || from.length > MAX_WORD || to.length > MAX_WORD || from === to) continue;
    if (replacements.some((x) => sameWord(x.from, from))) continue;
    replacements.push({ from, to, count: Number(raw.count) || 0, added_at: Number(raw.added_at) || 0 });
  }
  return {
    terms: terms.slice(0, MAX_TERMS),
    replacements: replacements.slice(0, MAX_REPLACEMENTS),
    updated_at: Number(src.updated_at) || 0,
  };
}

/**
 * Проверка загружаемого файла со словарём.
 * @returns {{ok: true, value: object}|{ok: false, error: string}}
 */
export function validateDictionaryFile(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    return { ok: false, error: 'В файле не словарь: нужен объект с полями «terms» и «replacements».' };
  }
  if (!Array.isArray(obj.terms) && !Array.isArray(obj.replacements)) {
    return { ok: false, error: 'В файле нет ни слов, ни автозамен — проверьте, что это словарь диктовки.' };
  }
  for (const t of obj.terms || []) {
    if (typeof t !== 'string') return { ok: false, error: 'В списке слов есть не текст — файл не подходит.' };
  }
  for (const r of obj.replacements || []) {
    if (!r || typeof r !== 'object' || typeof r.from !== 'string' || typeof r.to !== 'string') {
      return { ok: false, error: 'В автозаменах есть строка без «как распознал» или «как должно быть».' };
    }
  }
  const value = normalizeDictionary(obj);
  if (!value.terms.length && !value.replacements.length) {
    return { ok: false, error: 'После проверки в словаре не осталось ни одного слова.' };
  }
  return { ok: true, value };
}

/* ---------------------------------------------------------- автозамены */

function escapeRe(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Регистр первой буквы: «Тескфло» в начале фразы → «TaskFlow», а в середине
 * «тескфло» → «TaskFlow» — своё написание замены не ломаем.
 */
function keepCase(to, matched) {
  const dst = String(to);
  const src = String(matched);
  if (!dst) return dst;
  const upper = src[0] && src[0] === src[0].toUpperCase() && src[0] !== src[0].toLowerCase();
  return upper ? dst[0].toUpperCase() + dst.slice(1) : dst;
}

/**
 * Применить автозамены к тексту — теми же правилами, что и сервер: без учёта
 * регистра, по границам слов, регистр первой буквы сохраняется.
 *
 * @param {string} text
 * @param {Array<{from: string, to: string}>} replacements
 * @returns {{text: string, count: number, hits: Array<{from: string, to: string, count: number}>}}
 */
export function applyReplacements(text, replacements) {
  let out = String(text === null || text === undefined ? '' : text);
  const list = Array.isArray(replacements) ? replacements : [];
  const hits = [];
  for (const r of list) {
    const from = cleanTerm(r && r.from);
    const to = String(r && r.to !== undefined && r.to !== null ? r.to : '');
    if (!from || !to) continue;
    const body = escapeRe(from).replace(/\s+/g, '[\\s\u00a0]+');
    const re = new RegExp('(?<![\\p{L}\\p{N}])' + body + '(?![\\p{L}\\p{N}])', 'giu');
    let count = 0;
    out = out.replace(re, (m) => { count += 1; return keepCase(to, m); });
    if (count) hits.push({ from, to, count });
  }
  return { text: out, count: hits.reduce((n, x) => n + x.count, 0), hits };
}

/* ------------------------------------------------------------- подписи */

const DEVICE_RU = { cuda: 'на видеокарте', cpu: 'на процессоре', gpu: 'на видеокарте' };

/** 'cuda' → 'на видеокарте'; неизвестное устройство показываем как есть. */
export function deviceRu(device) {
  const d = cleanTerm(device).toLowerCase();
  if (!d) return '—';
  return DEVICE_RU[d] || d;
}

/**
 * Состояние распознавания человеческими словами.
 * @param {{available?: boolean, loaded?: boolean, loading?: boolean}|null} st
 * @returns {{tone: string, label: string, hint: string}}
 */
export function describeStatus(st) {
  const s = st || {};
  if (s.available === false) {
    return {
      tone: 'chip--warn',
      label: 'не запущено',
      hint: 'Распознавание сейчас не запущено — диктовать пока нельзя. Словарь работает: слова и автозамены сохранятся и подхватятся при следующем запуске.',
    };
  }
  if (s.loading) {
    return {
      tone: 'chip--info',
      label: 'загружается',
      hint: 'Модель загружается в память. Первый раз это занимает до минуты, дальше — секунды.',
    };
  }
  if (s.loaded) {
    return { tone: 'chip--ok', label: 'готово', hint: 'Можно диктовать: нажмите микрофон у поля и говорите.' };
  }
  return {
    tone: 'chip--muted',
    label: 'не загружено',
    hint: 'Загрузится при первой диктовке — это займёт 10–20 секунд, дальше распознавание идёт сразу.',
  };
}
