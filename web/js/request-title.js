/**
 * request-title.js — «как называется запрос»: одна логика на весь интерфейс.
 *
 * Зачем: до волны 17 везде показывался `title` — сырой текст диктовки
 * («Так, начнём работу в этой комнате. Работа будет построена следующим
 * образом. Для…»). Сервер (W17-B §4) отдаёт короткое название `title_short`,
 * источник `title_source` (`derived|llm|leader|user`) и флаг `title_locked`.
 * Здесь — чтение этих полей и запасной расчёт короткого названия, если сервер
 * старый (без `title_short`): тот же смысл, что у Hermes `derive_title`.
 *
 * Модуль без DOM и fetch: проверяется node --test (tests/shell-titles.test.mjs).
 */

/** Предел короткого названия на экране (знаков). */
export const TITLE_SHORT_MAX = 60;

/** Вводные слова, с которых начинают диктовку; в названии они не нужны. */
const LEAD_INS = [
  'так', 'итак', 'ну', 'в общем', 'короче', 'ладно', 'хорошо', 'окей', 'ок',
  'привет', 'здравствуйте', 'добрый день', 'добрый вечер', 'давай', 'давайте',
  // «начнём», «поехали» — не вводные: «Начнём работу в этой комнате» это и есть суть.
  'смотрите', 'смотри', 'значит', 'кстати',
];

/** «Так,» / «В общем…» / «Давайте,» — вводная часть отдельным куском. */
// Граница слова — не `\\b`: в JS он считает словом только латиницу, и «Так,»
// перед запятой границей не считается. Нужен явный разделитель после слова.
const LEAD_IN_SEP = '\\s,.:;!\\u2014\\u2013-';
const LEAD_IN_RE = new RegExp(
  '^(?:' + LEAD_INS.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  + ')(?=[' + LEAD_IN_SEP + ']|$)[' + LEAD_IN_SEP + ']*',
  'i',
);

/** Источник названия → человеческая подпись (в подсказке, не на экране). */
export const TITLE_SOURCE_LABELS = {
  derived: 'название черновое, уточнится само',
  llm: 'название предложено ИИ',
  leader: 'название предложено руководителем комнаты',
  user: 'название задано вами',
};

/**
 * Убрать markdown-разметку, служебные префиксы и лишние пробелы.
 * @param {*} text
 * @returns {string}
 */
export function cleanTitleText(text) {
  let s = String(text === null || text === undefined ? '' : text);
  s = s.replace(/\u0000/g, ' ').replace(/\r\n?/g, '\n');
  // markdown: заголовки, списки, цитаты, код, ссылки, жирный/курсив
  s = s.replace(/^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d{1,2}[.)][ \t]+)/gm, '');
  s = s.replace(/```[\s\S]*?```/g, ' ');
  s = s.replace(/`([^`]*)`/g, '$1');
  s = s.replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1');
  s = s.replace(/(\*\*|__)(.*?)\1/g, '$2');
  s = s.replace(/(^|[\s(])([*_])([^*_\n]+)\2/g, '$1$3');
  s = s.replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, ' ').trim();
  // вводные «Так,», «В общем…» (не больше трёх подряд)
  for (let i = 0; i < 3; i += 1) {
    const next = s.replace(LEAD_IN_RE, '').trim();
    if (next === s || !next) break;
    s = next;
  }
  return s;
}

/**
 * Первое осмысленное предложение (до . ! ? … или перевода строки).
 * Служебные «Теперь.»/«Да.» (короче 12 знаков) пропускаются.
 * @param {*} text
 * @param {number} [limit=TITLE_SHORT_MAX]
 * @returns {string}
 */
export function firstSentence(text, limit = TITLE_SHORT_MAX) {
  const clean = cleanTitleText(text);
  if (!clean) return '';
  const m = /^(.*?[.!?…])(\s|$)/.exec(clean);
  const head = m ? clean.slice(0, m[1].length) : clean;
  const sentence = head.replace(/[.!?…]+$/, '').trim();
  if (sentence.length >= 12 && sentence.length <= Math.max(limit, 48)) return sentence;
  return '';
}

/**
 * Короткое название из длинного текста — запасной вариант (как `derive_title`).
 * Никогда не бросает: пустой текст → ''.
 * @param {*} text
 * @param {{limit?: number}} [opts]
 * @returns {string}
 */
export function deriveTitle(text, opts = {}) {
  const limit = Math.max(12, Math.min(120, Number(opts.limit) || TITLE_SHORT_MAX));
  let base = firstSentence(text, limit);
  if (!base) {
    const clean = cleanTitleText(text);
    if (!clean) return '';
    if (clean.length <= limit) base = clean;
    else {
      const cut = clean.slice(0, limit + 1);
      const at = cut.lastIndexOf(' ');
      base = (at > limit * 0.5 ? cut.slice(0, at) : clean.slice(0, limit)).trim();
      base = base.replace(/[\s,;:.!?—–-]+$/, '');
    }
  }
  base = base.replace(/[\s,;:—–-]+$/, '').trim();
  if (!base) return '';
  const first = Array.from(base)[0];
  const tail = base.slice(first.length);
  return (first.toUpperCase() + tail) + (Array.from(base).length < Array.from(cleanTitleText(text)).length ? '…' : '');
}

/** Есть ли у записи поле (объект может быть пустым/чужим). */
function has(obj) {
  return Boolean(obj) && typeof obj === 'object';
}

/**
 * Короткое название записи: `title_short` с сервера, иначе расчёт по `title`,
 * иначе по тексту. Пустая строка — вызывающий сам решает, что показать.
 * @param {object} item  RequestSummary | RequestDetail | элемент бэклога
 * @param {{limit?: number}} [opts]
 * @returns {string}
 */
export function titleShortOf(item, opts = {}) {
  if (!has(item)) return '';
  const short = String(item.title_short || '').trim();
  if (short) return short;
  const full = String(item.title || '').trim();
  if (full) return deriveTitle(full, opts);
  return deriveTitle(item.text || item.preview || '', opts);
}

/**
 * Полный текст названия (то, что было записано/надиктовано) — для подсказки.
 * @param {object} item
 * @returns {string}
 */
export function titleFullOf(item) {
  if (!has(item)) return '';
  const full = String(item.title || '').trim();
  if (full) return full;
  const text = String(item.text || item.preview || '').trim();
  if (!text) return '';
  const line = text.split(/\n/).map((s) => s.trim()).find(Boolean) || '';
  return line.length > 400 ? line.slice(0, 400) + '…' : line;
}

/** Источник названия: 'derived' | 'llm' | 'leader' | 'user' | '' (старый сервер). */
export function titleSourceOf(item) {
  const src = String((has(item) && item.title_source) || '').trim().toLowerCase();
  return ['derived', 'llm', 'leader', 'user'].includes(src) ? src : '';
}

/** Переименовал ли пользователь (замок). */
export function titleLockedOf(item) {
  if (!has(item)) return false;
  if (item.title_locked === true) return true;
  return titleSourceOf(item) === 'user';
}

/** Подпись источника для подсказки. */
export function titleSourceLabel(item) {
  return TITLE_SOURCE_LABELS[titleSourceOf(item)] || '';
}

/**
 * Подсказка к названию: полный текст + откуда взялось название.
 * Пользователь видит полный текст диктовки, а не обрезку.
 * @param {object} item
 * @param {string} [extra] дополнительная строка (например, название комнаты)
 * @returns {string}
 */
export function titleHint(item, extra) {
  const full = titleFullOf(item);
  const short = titleShortOf(item);
  const lines = [];
  if (full) lines.push(full);
  else if (short) lines.push(short);
  const src = titleSourceLabel(item);
  if (src && src !== TITLE_SOURCE_LABELS.derived) lines.push(src);
  const note = String(extra || '').trim();
  if (note) lines.push(note);
  return lines.join('\n');
}

/** Подсказка значка замка. */
export function lockHint(locked) {
  return locked
    ? 'Название задано вами. Нажмите, чтобы сбросить и вернуть автоматическое.'
    : 'Название формируется автоматически';
}

/**
 * Подмешать ответ `PATCH /api/requests/{id}` в объект запроса (мутация на месте).
 * Сервер может ответить как `{request: {...}}`, так и самим объектом.
 * @param {object} detail
 * @param {*} payload
 * @returns {object} detail
 */
export function applyTitlePayload(detail, payload) {
  if (!has(detail) || !has(payload)) return detail;
  const src = has(payload.request) ? payload.request : payload;
  if (!has(src)) return detail;
  for (const key of ['title', 'title_short', 'title_source', 'title_locked']) {
    if (src[key] !== undefined) detail[key] = src[key];
  }
  return detail;
}

/**
 * Текст названия для замка: «название задано вами».
 * @param {object} item
 * @returns {string}
 */
export function lockNote(item) {
  return titleLockedOf(item) ? 'название задано вами' : '';
}

/**
 * Тело PATCH для переименования (пустое значение снимает блокировку).
 * @param {*} title
 * @returns {{title: string}}
 */
export function renameBody(title) {
  return { title: String(title === null || title === undefined ? '' : title).trim() };
}

/**
 * Можно ли сохранить новое название (не пустое, не то же самое, ≤ 120 знаков).
 * @param {*} next
 * @param {object} item
 * @returns {{ok: boolean, reason: string, value: string}}
 */
export function validateTitle(next, item) {
  const value = String(next === null || next === undefined ? '' : next).replace(/\s+/g, ' ').trim();
  if (!value) return { ok: false, reason: 'empty', value };
  if (Array.from(value).length > 120) return { ok: false, reason: 'long', value };
  const current = titleShortOf(item);
  if (value === current || value === titleFullOf(item)) return { ok: false, reason: 'same', value };
  return { ok: true, reason: '', value };
}

export default {
  TITLE_SHORT_MAX,
  TITLE_SOURCE_LABELS,
  cleanTitleText,
  firstSentence,
  deriveTitle,
  titleShortOf,
  titleFullOf,
  titleSourceOf,
  titleLockedOf,
  titleSourceLabel,
  titleHint,
  lockHint,
  lockNote,
  applyTitlePayload,
  renameBody,
  validateTitle,
};
