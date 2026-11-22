/**
 * svod-diff.js — «что изменилось» между версиями свода (W29B-Q4, п. 2).
 *
 * Разница считается ЗДЕСЬ, в браузере, по уже загруженным текстам версий: ни
 * одного обращения к модели, ни одной новой ручки. Две ступени:
 *
 *   1) по абзацам — что появилось, что убрано, что переписано;
 *   2) внутри переписанного абзаца — по словам (общая часть не пересчитывается,
 *      поэтому видно именно правку: «срок 3 дня» → «срок 5 дней»).
 *
 * Функции чистые (без DOM и состояния), поэтому проверяются node-тестами
 * `tests/svod-diff.test.mjs`: границы, пустые тексты, счётчики, переходы.
 */

/** Сколько токенов в абзаце ещё считаем по словам (LCS держит памятью N×M). */
export const WORD_LIMIT = 1200;
/** Сколько абзацев сравниваем (свод длиннее — режем, честно помечаем). */
export const PARA_LIMIT = 120;

/** Абзацы: пустая строка — граница. Заголовки и списки остаются целыми строками. */
export function paragraphs(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/\r\n?/g, '\n')
    .split(/\n{2,}/)
    .map((p) => p.replace(/\s+$/, ''))
    .filter((p) => p.trim())
    .slice(0, PARA_LIMIT);
}

/** Токены абзаца: слово и следующий за ним пробел — один токен (пробелы видны). */
export function words(text) {
  const s = String(text === null || text === undefined ? '' : text).replace(/\r\n?/g, '\n');
  if (!s) return [];
  const out = s.match(/\s+|[^\s]+/g);
  return out || [];
}

/** Совпадение «до» и «после»: длины по LCS (числа, без указателей — память). */
function lcsTable(a, b) {
  const n = a.length;
  const m = b.length;
  const w = m + 1;
  const dp = new Int32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i * w + j] = a[i] === b[j]
        ? dp[(i + 1) * w + (j + 1)] + 1
        : Math.max(dp[(i + 1) * w + j], dp[i * w + (j + 1)]);
    }
  }
  return { dp, w, n, m };
}

/** Склеить соседние токены одного вида: меньше узлов в разметке. */
function mergeOps(ops) {
  const out = [];
  for (const op of ops) {
    const last = out[out.length - 1];
    if (last && last.kind === op.kind) last.text += op.text;
    else out.push({ kind: op.kind, text: op.text });
  }
  return out;
}

/**
 * Разница по словам: `[{kind:'same'|'add'|'del', text}]` в порядке «до/после».
 * Слишком длинный абзац не считается по словам — возвращается грубая разница
 * (весь абзац убран и добавлен), чтобы страница не подвисала.
 */
export function diffWords(before, after) {
  const a = words(before);
  const b = words(after);
  if (!a.length && !b.length) return [];
  if (a.length > WORD_LIMIT || b.length > WORD_LIMIT) {
    const out = [];
    if (before) out.push({ kind: 'del', text: String(before) });
    if (after) out.push({ kind: 'add', text: String(after) });
    return out;
  }
  // Общая голова и хвост — сразу в «same»: LCS остаётся коротким.
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head
    && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;
  const aMid = a.slice(head, a.length - tail);
  const bMid = b.slice(head, b.length - tail);
  const { dp, w, n, m } = lcsTable(aMid, bMid);
  const mid = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (aMid[i] === bMid[j]) { mid.push({ kind: 'same', text: aMid[i] }); i += 1; j += 1; }
    else if (dp[(i + 1) * w + j] >= dp[i * w + (j + 1)]) { mid.push({ kind: 'del', text: aMid[i] }); i += 1; }
    else { mid.push({ kind: 'add', text: bMid[j] }); j += 1; }
  }
  while (i < n) { mid.push({ kind: 'del', text: aMid[i] }); i += 1; }
  while (j < m) { mid.push({ kind: 'add', text: bMid[j] }); j += 1; }
  const out = [];
  if (head) out.push({ kind: 'same', text: a.slice(0, head).join('') });
  out.push(...mid);
  if (tail) out.push({ kind: 'same', text: a.slice(a.length - tail).join('') });
  return mergeOps(out);
}

/**
 * Разница версий по абзацам:
 *   `{kind:'same', text}` | `{kind:'change', before, after, words}`
 * Пара «убрали и добавили» на одном месте становится одной правкой `change`.
 */
export function diffParagraphs(before, after) {
  const a = paragraphs(before);
  const b = paragraphs(after);
  const { dp, w, n, m } = lcsTable(a, b);
  const raw = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { raw.push({ kind: 'same', text: a[i] }); i += 1; j += 1; }
    else if (dp[(i + 1) * w + j] >= dp[i * w + (j + 1)]) { raw.push({ kind: 'del', text: a[i] }); i += 1; }
    else { raw.push({ kind: 'add', text: b[j] }); j += 1; }
  }
  while (i < n) { raw.push({ kind: 'del', text: a[i] }); i += 1; }
  while (j < m) { raw.push({ kind: 'add', text: b[j] }); j += 1; }
  // «Убрали» + «добавили» подряд — это переписанный абзац: показываем правку по словам.
  const out = [];
  for (let k = 0; k < raw.length; k += 1) {
    const cur = raw[k];
    const next = raw[k + 1];
    if (cur.kind === 'del' && next && next.kind === 'add') {
      out.push({ kind: 'change', before: cur.text, after: next.text,
        words: diffWords(cur.text, next.text) });
      k += 1;
      continue;
    }
    if (cur.kind === 'add' && next && next.kind === 'del') {
      out.push({ kind: 'change', before: next.text, after: cur.text,
        words: diffWords(next.text, cur.text) });
      k += 1;
      continue;
    }
    if (cur.kind === 'same') out.push({ kind: 'same', text: cur.text });
    else if (cur.kind === 'del') out.push({ kind: 'change', before: cur.text, after: '',
      words: diffWords(cur.text, ''), removed: true });
    else out.push({ kind: 'change', before: '', after: cur.text,
      words: diffWords('', cur.text), added: true });
  }
  return out;
}

/**
 * Счётчики изменения — словами и абзацами.
 * @param {Array} blocks результат `diffParagraphs`
 * @returns {{wordsAdded:number, wordsRemoved:number, added:number, removed:number,
 *            changed:number, same:number, empty:boolean}}
 */
export function diffCounts(blocks) {
  const list = Array.isArray(blocks) ? blocks : [];
  const out = { wordsAdded: 0, wordsRemoved: 0, added: 0, removed: 0, changed: 0, same: 0, empty: true };
  for (const block of list) {
    if (!block || block.kind === 'same') { out.same += 1; continue; }
    out.changed += 1;
    if (block.added) out.added += 1;
    else if (block.removed) out.removed += 1;
    for (const op of block.words || []) {
      if (op.kind === 'add') out.wordsAdded += countWords(op.text);
      else if (op.kind === 'del') out.wordsRemoved += countWords(op.text);
    }
  }
  out.empty = out.changed === 0;
  return out;
}

/** Слова в куске текста (пробелы и знаки не считаем). */
export function countWords(text) {
  const s = String(text === null || text === undefined ? '' : text);
  const hit = s.match(/[\p{L}\p{N}]+/gu);
  return hit ? hit.length : 0;
}

/** Номера правок (абзацев-изменений) по порядку — для «перейти к изменению». */
export function changeIndexes(blocks) {
  const out = [];
  (Array.isArray(blocks) ? blocks : []).forEach((block, index) => {
    if (block && block.kind === 'change') out.push(index);
  });
  return out;
}

/**
 * Следующая правка от текущей: по кругу, чтобы «дальше» всегда работало.
 * @param {number[]} indexes результат `changeIndexes`
 * @param {number} current текущий номер правки
 * @param {number} step +1 вперёд, −1 назад
 * @returns {number} номер правки (0), если правок нет
 */
export function nextChange(indexes, current, step = 1) {
  const list = Array.isArray(indexes) ? indexes : [];
  if (!list.length) return 0;
  const dir = Number(step) < 0 ? -1 : 1;
  const at = list.indexOf(Number(current));
  if (at < 0) return dir > 0 ? list[0] : list[list.length - 1];
  const next = (at + dir + list.length) % list.length;
  return list[next];
}

/** Строка счётчиков: «Добавлено 12 слов · убрано 4 · правок 3». */
export function countsText(counts) {
  const c = counts && typeof counts === 'object' ? counts : {};
  const parts = [];
  if (Number(c.wordsAdded) > 0) parts.push('добавлено ' + plural(Number(c.wordsAdded), 'слово', 'слова', 'слов'));
  if (Number(c.wordsRemoved) > 0) parts.push('убрано ' + plural(Number(c.wordsRemoved), 'слово', 'слова', 'слов'));
  if (!parts.length) {
    if (Number(c.added) > 0) parts.push(plural(Number(c.added), 'новый абзац', 'новых абзаца', 'новых абзацев'));
    if (Number(c.removed) > 0) parts.push('убрано ' + plural(Number(c.removed), 'абзац', 'абзаца', 'абзацев'));
  }
  const changed = Number(c.changed) || 0;
  if (changed) parts.push(plural(changed, 'правка', 'правки', 'правок'));
  return parts.length ? parts.join(' · ') : 'Различий нет';
}

/** Русское склонение по числу. */
export function plural(num, one, few, many) {
  const n = Math.abs(Number(num) || 0);
  const d10 = n % 10;
  const d100 = n % 100;
  if (d10 === 1 && d100 !== 11) return one;
  if (d10 >= 2 && d10 <= 4 && (d100 < 12 || d100 > 14)) return few;
  return many;
}

/**
 * Поиск фрагмента цитаты в тексте версии — для «перейти к фрагменту».
 * Сравнение без учёта регистра, ё/е и лишних пробелов.
 * @param {string} haystack текст свода
 * @param {string} quote цитата из второго мнения
 * @returns {{index:number, length:number}|null}
 */
export function findQuote(haystack, quote) {
  const text = String(haystack === null || haystack === undefined ? '' : haystack);
  const needle = String(quote === null || quote === undefined ? '' : quote).trim();
  if (!text || !needle) return null;
  const flat = (s) => s.replace(/\s+/g, ' ').toLowerCase().replace(/ё/g, 'е');
  const flatText = flat(text);
  // Хвостовое «…» (цитату сократили при показе) — не часть текста свода.
  const flatNeedle = flat(needle).replace(/[…\.\s]+$/u, '').trim() || flat(needle);
  const found = flatText.indexOf(flatNeedle);
  if (found < 0) {
    // Цитата могла быть сокращена с «…» — ищем первые 40 знаков.
    const head = flatNeedle.slice(0, 40);
    const short = head.length >= 12 ? flatText.indexOf(head) : -1;
    return short < 0 ? null : { index: short, length: head.length };
  }
  return { index: found, length: flatNeedle.length };
}

export default {
  paragraphs, words, diffWords, diffParagraphs, diffCounts, countsText, changeIndexes,
  nextChange, countWords, plural, findQuote, WORD_LIMIT, PARA_LIMIT,
};
