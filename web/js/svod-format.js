/**
 * svod-format.js — читаемый «Свод» из любого текста агента (W24-N1).
 *
 * Замечание Михаила 18.11: свод выглядел «наскальной живописью» — один абзац на
 * 800 знаков, перечни «1) … 2) … 3) …» внутри строки, метки «ИТОГ», «НЕ НАЙДЕНО»,
 * длинные пути Windows прямо в тексте. Приложение не должно зависеть от того, как
 * написал агент, поэтому текст нормализуется ПЕРЕД показом.
 *
 * Что делает `formatSvod(md)` (чистая функция, без модели и без DOM):
 *   а) инлайн-перечни «1) … 2) …» и «а) б) в)» при ≥ 3 пунктах → настоящий список;
 *   б) абзацы длиннее 450 знаков режутся по границам предложений на абзацы
 *      по 1–4 предложения (не внутри сокращений «т.е.», «г.», инициалов,
 *      десятичных чисел, путей, ссылок и кода);
 *   в) метки ИТОГ / ВЫВОД / НЕ НАЙДЕН / НЕ ПРОВЕРЕНО / ВАЖНО / ВНИМАНИЕ в начале
 *      абзаца → плашка `> [!МЕТКА]` (её рисует `web/js/md.js`);
 *   г) пути Windows/Unix и ссылки в тексте → моноширинный `code`;
 *   д) заголовки, готовые списки, таблицы, цитаты и блоки кода не трогаются.
 *
 * Свойства (проверяются `tests/svod-format.test.mjs`):
 *   • детерминированность: один и тот же вход даёт один и тот же выход;
 *   • идемпотентность: `formatSvod(formatSvod(x)) === formatSvod(x)`;
 *   • без потери текста: «голый» текст до и после совпадает (`svodPlainText`);
 *     метки-плашки, маркеры перечней и обратные кавычки — оформление, не текст.
 *
 * Публично:
 *   formatSvod(md)            → md
 *   svodPlainText(md)         → текст без разметки (для сверки «ничего не пропало»)
 *   svodHeadline(md, meta)    → {text, source} | null — «Коротко»
 *   svodParts(md, meta)       → {headline, body} — «Коротко» + текст без дубля
 *
 * Слова для интерфейса — в `web/js/text-ru.js`; здесь только разметка и цифры.
 */

/** Длиннее этого абзац режется по предложениям. */
export const SVOD_MAX_PARAGRAPH = 450;
/** Сколько знаков в «Коротко» — предел ПО ЦЕЛЫМ предложениям (W28b, дефект 9).
 *
 * Было 240 и обрезка с «…»: блок обрывался посреди мысли, и человек всё равно
 * перечитывал свод. Теперь предел 700, режем только по границам предложений;
 * если места мало — интерфейс показывает «Читать далее» (`headlineSplit`),
 * а не троеточие. Смысл: короткая резолюция обязана быть законченной. */
export const SVOD_HEADLINE_MAX = 700;
/** Сколько знаков «Коротко» видно до «Читать далее». */
export const SVOD_HEADLINE_VISIBLE = 400;
/** Сколько пунктов нужно, чтобы инлайн-перечень стал списком. */
export const SVOD_MIN_ITEMS = 3;
/** Больше этого предложений в одном абзаце не собираем. */
const MAX_SENTENCES = 4;

/** Метки-плашки: [метка, вид для оформления]. Длинные — раньше коротких. */
const CALLOUT_LABELS = [
  ['НЕ ПРОВЕРЕНО', 'muted'],
  ['НЕ НАЙДЕНО', 'muted'],
  ['НЕ НАЙДЕН', 'muted'],
  ['ВНИМАНИЕ', 'warn'],
  ['ВЫВОДЫ', 'ok'],
  ['ИТОГО', 'ok'],
  ['ВЫВОД', 'ok'],
  ['ВАЖНО', 'warn'],
  ['ИТОГ', 'ok'],
];

/** Сокращения, после которых точка не кончает предложение. */
const ABBR = new Set([
  'т.е', 'т.к', 'т.д', 'т.п', 'т.ч', 'и.т.д', 'и.т.п', 'г', 'гг', 'д', 'п', 'др', 'пр', 'см',
  'рис', 'табл', 'стр', 'напр', 'ок', 'руб', 'тыс', 'млн', 'млрд', 'коп', 'шт', 'чел', 'им',
  'ул', 'пер', 'обл', 'ст', 'гор', 'долл', 'евро', 'мск', 'vs', 'etc', 'e.g', 'i.e', 'no',
  'approx', 'p', 'pp', 'т.о', 'в.т.ч',
]);

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;
const HEADING_RE = /^ {0,3}#{1,6}[ \t]+\S/;
const HR_RE = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const QUOTE_RE = /^ {0,3}>/;
const LIST_RE = /^( *)([-*+]|\d{1,9}[.)])[ \t]+/;
const TABLE_DELIM_RE = /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;
const CALLOUT_RE = /^ {0,3}>[ \t]*\[![^\]]{1,40}\][ \t]*$/;

/** Строка начинает блок (значит, абзац на ней кончается). */
function isBlockStart(lines, i) {
  const line = lines[i];
  if (!line || !line.trim()) return false;
  if (FENCE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line)) return true;
  if (QUOTE_RE.test(line) || LIST_RE.test(line)) return true;
  if (line.indexOf('|') !== -1 && i + 1 < lines.length && TABLE_DELIM_RE.test(lines[i + 1] || '')) return true;
  return false;
}

/** Границы участков, которые нельзя менять (код и ссылки). */
function protectedRanges(text) {
  const out = [];
  const re = /`[^`\n]*`|!?\[[^\]\n]*\]\([^)\n]*\)/gu;
  let m;
  while ((m = re.exec(text))) out.push([m.index, m.index + m[0].length]);
  return out;
}

function overlaps(ranges, start, end) {
  for (const [a, b] of ranges) if (start < b && end > a) return true;
  return false;
}

/* ── (в) метки-плашки ─────────────────────────────────────────────────── */

/**
 * Метка в начале абзаца: «НЕ НАЙДЕНО: …», «ИТОГ — …», «ВАЖНО. …».
 * Слово-метка без двоеточия/тире не считается («Важно понимать, что…» — текст).
 * @param {string} text
 * @returns {{label:string, kind:string, rest:string}|null}
 */
export function matchCalloutLabel(text) {
  const src = String(text || '').replace(/^[*_]*/, '');
  const head = src.slice(0, 60);
  for (const [label, kind] of CALLOUT_LABELS) {
    if (head.length <= label.length) continue;
    if (head.slice(0, label.length).toUpperCase() !== label) continue;
    const sep = /^[ \t]*[:—–.\-][ \t]*/.exec(src.slice(label.length));
    if (!sep) continue;
    const rest = src.slice(label.length + sep[0].length).trim();
    if (!rest) continue;
    return { label, kind, rest };
  }
  return null;
}

/* ── (а) инлайн-перечни ───────────────────────────────────────────────── */

const NUM_MARK_RE = /(^|[\s(«"„“'])(\d{1,2})[.)][ \t]+/gu;
const LET_MARK_RE = /(^|[\s(«"„“'])([а-яё])[.)][ \t]+/gu;
const LETTERS = 'абвгдеёжзийклмнопрстуфхцчшщэюя';

function marksOf(text, re) {
  const out = [];
  re.lastIndex = 0;
  let m;
  while ((m = re.exec(text))) {
    out.push({ at: m.index + m[1].length, len: m[0].length - m[1].length, label: m[2] });
  }
  return out;
}

/** Самый длинный подряд идущий кусок ≥ 3 пунктов (1,2,3… или а,б,в…). */
function longestRun(marks, kind) {
  let best = null;
  let cur = [];
  const good = (prev, next) => {
    if (!prev) return true;
    if (kind === 'num') return Number(next.label) === Number(prev.label) + 1;
    return LETTERS.indexOf(next.label) === LETTERS.indexOf(prev.label) + 1;
  };
  for (const mk of marks) {
    if (cur.length && !good(cur[cur.length - 1], mk)) {
      if (!best || cur.length > best.length) best = cur;
      cur = [mk];
      continue;
    }
    cur.push(mk);
  }
  if (!best || cur.length > best.length) best = cur;
  return best && best.length >= SVOD_MIN_ITEMS ? best : null;
}

/**
 * Перечень внутри строки → список.
 * @param {string} text
 * @returns {{kind:'num'|'let', start:number, lead:string, items:string[]}|null}
 */
export function inlineEnumeration(text) {
  const src = String(text || '');
  if (!src.trim()) return null;
  const runs = [];
  for (const [kind, re] of [['num', NUM_MARK_RE], ['let', LET_MARK_RE]]) {
    const run = longestRun(marksOf(src, re), kind);
    if (run) runs.push({ kind, run });
  }
  if (!runs.length) return null;
  // при равной длине выигрывает числовой перечень
  runs.sort((a, b) => b.run.length - a.run.length || (a.kind === 'num' ? -1 : 1));
  const { kind, run } = runs[0];
  const lead = src.slice(0, run[0].at).trim();
  const items = [];
  for (let i = 0; i < run.length; i += 1) {
    const from = run[i].at + run[i].len;
    const to = i + 1 < run.length ? run[i + 1].at : src.length;
    const item = src.slice(from, to).trim();
    if (item) items.push(item);
  }
  if (items.length < SVOD_MIN_ITEMS) return null;
  return { kind, start: kind === 'num' ? Number(run[0].label) : 1, lead, items };
}

/* ── (б) предложения и абзацы ─────────────────────────────────────────── */

/**
 * Разбить текст на предложения. Не режет по «т.е.», «г.», инициалам «А. С.»,
 * десятичным дробям, путям, ссылкам и коду.
 * @param {string} text
 * @returns {string[]}
 */
export function splitSentences(text) {
  const src = String(text || '');
  const prot = protectedRanges(src);
  const out = [];
  let start = 0;
  for (let i = 0; i < src.length; i += 1) {
    const ch = src[i];
    if ('.!?…'.indexOf(ch) === -1) continue;
    if (overlaps(prot, i, i + 1)) continue;
    if (ch === '.' && (src[i - 1] === '.' || src[i + 1] === '.')) continue;
    if (ch === '.' && /\d/.test(src[i - 1] || '') && /\d/.test(src[i + 1] || '')) continue;
    let j = i + 1;
    while (j < src.length && /[»"'”’)\]]/.test(src[j])) j += 1;
    if (!/\s/.test(src[j] || '')) continue;
    let k = j;
    while (k < src.length && /\s/.test(src[k])) k += 1;
    const next = src[k];
    if (!next || !/[\p{Lu}\p{N}«"„(“]/u.test(next)) continue;
    let w = i;
    while (w > 0 && !/[\s(«"„“]/.test(src[w - 1])) w -= 1;
    const word = src.slice(w, i);
    if (/[\\/]$/.test(word)) continue;               // путь или ссылка
    if (word.length === 1 && /\p{Lu}/u.test(word)) continue; // инициал «А.»
    if (ABBR.has(word.toLowerCase())) continue;
    const part = src.slice(start, i + 1).trim();
    if (part) out.push(part);
    start = k;
    i = k - 1;
  }
  const tail = src.slice(start).trim();
  if (tail) out.push(tail);
  return out;
}

/** Уложить предложения в абзацы: до 450 знаков и не больше 4 предложений. */
/** Длина «как видит человек»: адрес ссылки не считается (иначе ссылки раздувают абзац). */
function visLen(text) {
  return String(text || '').replace(/!?\[([^\]\n]*)\]\([^)\n]*\)/g, '$1').length;
}

function packParagraphs(sentences, limit = SVOD_MAX_PARAGRAPH) {
  const out = [];
  let cur = [];
  let len = 0;
  for (const s of sentences) {
    if (!cur.length) { cur = [s]; len = visLen(s); continue; }
    if (cur.length < MAX_SENTENCES && len + 1 + visLen(s) <= limit) { cur.push(s); len += 1 + visLen(s); continue; }
    out.push(cur.join(' '));
    cur = [s];
    len = visLen(s);
  }
  if (cur.length) out.push(cur.join(' '));
  return out;
}

/* ── (г) пути и ссылки → чипы-ссылки (W26-D4) ─────────────────────────── */

// W26-D4: раньше путь становился `code` — «наскальная живопись», и нажать его было
// нельзя. Теперь путь превращается в ссылку со схемой `file:` (метка — сам путь),
// из которой `md.js` рисует чип «имя · Открыть · Показать в папке · Копировать
// путь», а адрес — в ссылку с меткой чипа `"tr-chip"` («Перейти · Копировать»).
// Текст пути остаётся текстом ссылки, поэтому «ничего не пропало»
// (`svodPlainText`) сохраняется, а повторный проход ничего не портит.

//: сегмент пути без пробелов; каталог с пробелом («Годовой отчёт») доклеивается ниже
const SEG = '[^\\\\/\\s`"\'«»()\\[\\]<>|]+';
const WIN_PATH_RE = new RegExp('(?<![\\p{L}\\p{N}_`\\\\/])(?:[A-Za-z]:[\\\\/]|\\\\\\\\\\\\)(?:' + SEG + '[\\\\/])*' + SEG, 'gu');
const UNIX_PATH_RE = new RegExp('(?<![\\p{L}\\p{N}_`\\\\/])(?:~?\\/)(?:' + SEG + '\\/)*' + SEG, 'gu');
const URL_RE = /(?<![\p{L}\p{N}_`])(?:https?:\/\/[^\s`"'«»()\[\]<>]+)/gu;
const TRAIL = /[.,;:!?]+$/;
//: продолжение пути после пробела: слово, за которым сразу идёт разделитель
const SEG_CONT = /^[ \t]([^\\/\s`"'«»()\[\]<>|]+)(?=[\\/])/u;

/** Метка чипа-ссылки в `title`: обычные ссылки агента её не несут. */
export const CHIP_TITLE = 'tr-chip';

function hasSpace(s) {
  return /\s/u.test(s);
}

/** Windows-путь начинается с «C:\» или «\\сервер». */
function winStart(t) {
  return (t.length > 2 && t[1] === ':' && /[A-Za-z]/.test(t[0]) && (t[2] === '\\' || t[2] === '/'))
    || (t.length > 2 && t[0] === '\\' && t[1] === '\\');
}

/** Unix-путь начинается с «/» или «~/». */
function unixStart(t) {
  return t[0] === '/' || (t.length > 1 && t[0] === '~' && t[1] === '/');
}

/** Строка целиком — путь или адрес (содержимое обратных кавычек). */
export function pureLinkTarget(text) {
  const t = String(text || '').trim();
  if (!t || t.length < 3) return null;
  if (/^https?:\/\//iu.test(t)) return hasSpace(t) ? null : { kind: 'url', value: t };
  if (hasSpace(t) && !winStart(t)) return null;
  if (!winStart(t) && !unixStart(t)) return null;
  // ссылка уже могла быть обёрнута — второй раз не надо
  if (/[`"'<>|]/u.test(t)) return null;
  return { kind: 'file', value: t };
}

/**
 * Путь → markdown-ссылка со схемой `file:` (метка — сам путь: текст не теряется).
 * @param {string} path
 * @returns {string}
 */
export function fileLink(path) {
  const p = String(path || '').trim();
  if (!p) return '';
  // W26-QA2: путь с обратным слэшем на конце («…\Результаты\») закрывал ссылку
  // экранированной скобкой: `[путь\](<file:…>)` — парсер видел `\]` и оставлял
  // в своде сырой markdown. Последний `\` пишем как `\\` (в тексте это один `\`).
  const tail = (p.match(/\\+$/) || [''])[0];
  const label = (tail ? p.slice(0, -tail.length) : p).replace(/[\[\]]/g, (c) => '\\' + c)
    + tail.replace(/\\/g, '\\\\');
  // путь в `<…>` — целиком, без процентного кодирования: длина строки не растёт,
  // поэтому повторный проход нормализатора ничего не переразбивает.
  return '[' + label + '](<file:' + p + '>)';
}

/**
 * Адрес → markdown-ссылка с меткой чипа (`"tr-chip"` в title).
 * @param {string} url
 * @returns {string}
 */
export function urlLink(url) {
  const u = String(url || '').trim();
  if (!u) return '';
  return '[' + u.replace(/[\[\]]/g, (c) => '\\' + c) + '](' + u + ' "' + CHIP_TITLE + '")';
}

/** Кусок текста → ссылка-чип, если это ровно путь или адрес, иначе исходное. */
function linkOrSame(raw) {
  const t = pureLinkTarget(raw);
  if (!t) return raw;
  return t.kind === 'url' ? urlLink(t.value) : fileLink(t.value);
}

//: конец имени файла — расширение (тогда пробелы внутри пути допустимы)
const EXT_TAIL = /\.(?:md|markdown|txt|log|json|jsonl|csv|tsv|ya?ml|xml|html?|pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|webp|svg|bmp|avif|ics|py|js|css|sh)(?![\p{L}\p{N}_])/iu;

/**
 * Доклеить «хвост с пробелами»: `Витрина черновик 2025.md`, `Сетка блоков.csv`.
 * Склеиваем только если среди ближайших 3 слов есть имя файла с расширением —
 * иначе обычная фраза после пути («…папка и его на диске нет») не съедается.
 * @param {string} src
 * @param {number} from
 * @returns {number} сколько знаков доклеить
 */
function spacedTail(src, from) {
  const tail = src.slice(from);
  if (tail[0] !== ' ' && tail[0] !== '\t') return 0;
  let used = 0;
  for (let i = 0; i < 3; i += 1) {
    const m = /^[ \t]([^\\/\s`"'«»()\[\]<>|]+)/u.exec(tail.slice(used));
    if (!m) return 0;
    used += m[0].length;
    const next = tail[used];
    if (next === '\\' || next === '/') return 0;      // это уже обычный сегмент пути
    if (EXT_TAIL.test(m[1])) return used;               // дошли до имени файла
    if (!next || next === '\n') return 0;
    if (/[.,;:!?—–)]$/u.test(m[1])) return 0;           // слово закончило фразу
  }
  return 0;
}

/**
 * Пути и адреса в тексте → кликабельные чипы-ссылки (W26-D4).
 * Код, готовые ссылки и изображения не трогаются; в обратных кавычках путь тоже
 * становится ссылкой (контракт велит писать пути в кавычках — и они должны нажиматься).
 * @param {string} text
 * @returns {string}
 */
export function wrapPaths(text) {
  let src = String(text || '');
  if (!src) return src;
  // 1) `C:/папка/файл.md` и `https://…` целиком в обратных кавычках → ссылка-чип
  src = src.replace(/`([^`\n]{3,500})`/gu, (all, inner) => linkOrSame(inner) || all);
  const prot = protectedRanges(src);
  let out = '';
  let pos = 0;
  const hits = [];
  for (const re of [WIN_PATH_RE, UNIX_PATH_RE, URL_RE]) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(src))) {
      const at = m.index;
      let end = at + m[0].length;
      // «…\Годовой отчёт\…»: доклеиваем сегменты с пробелом и продолжение по «/»
      for (;;) {
        let step = 0;
        const tail = src.slice(end);
        const cont = SEG_CONT.exec(tail);
        if (cont) {
          const sepAt = end + cont[0].length;
          if (src[sepAt] === '\\' || src[sepAt] === '/') {
            const seg = new RegExp('^' + SEG, 'u').exec(src.slice(sepAt + 1));
            if (seg) step = cont[0].length + 1 + seg[0].length;
          }
        }
        if (!step && (tail[0] === '\\' || tail[0] === '/')) {
          const seg = new RegExp('^' + SEG, 'u').exec(tail.slice(1));
          if (seg) step = 1 + seg[0].length;
        }
        if (!step) step = spacedTail(src, end);
        if (!step) break;
        end += step;
      }
      re.lastIndex = end;
      const raw = src.slice(at, end);
      const trail = (TRAIL.exec(raw) || [''])[0];
      const body = trail ? raw.slice(0, raw.length - trail.length) : raw;
      if (!body || body.length < 3 || !/\p{L}/u.test(body)) continue;
      if (overlaps(prot, at, at + raw.length)) continue;
      hits.push({ at, end: at + body.length, body, trail, url: re === URL_RE });
    }
  }
  if (!hits.length) return src;
  hits.sort((a, b) => a.at - b.at);
  for (const h of hits) {
    if (h.at < pos) continue;
    const chip = h.url ? urlLink(h.body) : fileLink(h.body);
    out += src.slice(pos, h.at) + chip + h.trail;
    pos = h.end + h.trail.length;
  }
  out += src.slice(pos);
  return out;
}

/* ── сборка абзаца ────────────────────────────────────────────────────── */

function paragraphText(joined) {
  return String(joined || '').replace(/\s*\n\s*/g, ' ').replace(/[ \t]{2,}/g, ' ').trim();
}

const LABEL_SPLIT_RE = new RegExp(
  '(?<=[.!?…)\\]])\\s+(?=(?:' + CALLOUT_LABELS.map(([l]) => l.replace(/ /g, '\\s+')).join('|') + ')[ \\t]*[:—–.\\-])',
  'u',
);

/** Метка, начавшаяся в середине абзаца («… круге. НЕ ПРОВЕРЕНО: …») — с новой строки. */
function splitBeforeLabels(text) {
  const parts = String(text || '').split(LABEL_SPLIT_RE).map((p) => p.trim()).filter(Boolean);
  return parts.length ? parts : [String(text || '').trim()];
}

/**
 * Один смысловой кусок абзаца → md-блоки: плашка, абзацы, список.
 * @param {string} piece
 * @returns {string[]}
 */
function formatPiece(piece) {
  const text = paragraphText(piece);
  if (!text) return [];
  const label = matchCalloutLabel(text);
  const body = label ? label.rest : text;
  const en = inlineEnumeration(body);
  const lead = en ? en.lead : body;
  const blocks = [];
  const paras = [];
  if (lead) {
    const pack = visLen(lead) > SVOD_MAX_PARAGRAPH ? packParagraphs(splitSentences(lead)) : [lead];
    for (const p of pack) {
      const wrapped = wrapPaths(p);
      if (wrapped) paras.push(wrapped);
    }
  }
  if (label && paras.length) {
    const lines = ['> [!' + label.label + ']'];
    paras.forEach((p, idx) => {
      if (idx) lines.push('>');
      lines.push('> ' + p);
    });
    blocks.push(lines.join('\n'));
  } else {
    for (const p of paras) blocks.push(p);
  }
  let tail = '';
  if (en) {
    const items = en.items.slice();
    // Последний пункт перечня не ограничен маркером: если после него идёт обычная
    // проза («… версии. Черновик лежит в …»), она уходит из списка в текст.
    const lastSentences = splitSentences(items[items.length - 1] || '');
    if (lastSentences.length > 1) {
      items[items.length - 1] = lastSentences[0];
      tail = lastSentences.slice(1).join(' ');
    }
    const lines = items.map((item, idx) => (en.kind === 'num' ? (en.start + idx) + '. ' + wrapPaths(item)
      : '- ' + wrapPaths(item)));
    blocks.push(lines.join('\n'));
  }
  if (tail) {
    const pack = visLen(tail) > SVOD_MAX_PARAGRAPH ? packParagraphs(splitSentences(tail)) : [tail];
    for (const p of pack) if (wrapPaths(p)) blocks.push(wrapPaths(p));
  }
  return blocks;
}

/**
 * Один абзац (уже склеенный) → md-блоки: плашка, абзацы, список.
 * @param {string} joined
 * @returns {string[]} блоки markdown
 */
export function formatParagraph(joined) {
  const text = paragraphText(joined);
  if (!text) return [];
  const out = [];
  for (const piece of splitBeforeLabels(text)) out.push(...formatPiece(piece));
  return out;
}

/* ── главная функция ──────────────────────────────────────────────────── */

/**
 * Привести свод к читаемому виду. Детерминированно, идемпотентно, без потери текста.
 * @param {string} md текст свода (markdown)
 * @returns {string} тот же текст с нормальной структурой
 */
export function formatSvod(md) {
  const src = md === null || md === undefined ? '' : String(md);
  if (!src.trim()) return src;
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;
  const n = lines.length;
  while (i < n) {
    const line = lines[i];
    const fence = FENCE_RE.exec(line);
    if (fence) {
      // блок кода — как есть, до закрывающего маркера
      const mark = fence[1];
      out.push(line);
      i += 1;
      const closeRe = new RegExp('^ {0,3}' + (mark[0] === '`' ? '`' : '~') + '{' + mark.length + ',}[ \\t]*$');
      while (i < n) {
        out.push(lines[i]);
        const done = closeRe.test(lines[i]);
        i += 1;
        if (done) break;
      }
      continue;
    }
    if (!line.trim()) { out.push(line); i += 1; continue; }
    if (CALLOUT_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line) || QUOTE_RE.test(line)
      || LIST_RE.test(line)) {
      out.push(line); i += 1; continue;
    }
    if (line.indexOf('|') !== -1 && i + 1 < n && TABLE_DELIM_RE.test(lines[i + 1] || '')) {
      while (i < n && lines[i].trim() && lines[i].indexOf('|') !== -1) { out.push(lines[i]); i += 1; }
      continue;
    }
    // абзац: подряд идущие строки до пустой или до начала блока
    const buf = [line];
    i += 1;
    while (i < n && lines[i].trim() && !isBlockStart(lines, i)) { buf.push(lines[i]); i += 1; }
    const before = buf.join('\n');
    const after = formatParagraph(before).join('\n\n');
    out.push(after || before);
  }
  return out.join('\n');
}

/* ── «голый» текст (для сверки «ничего не пропало») ───────────────────── */

/**
 * Текст без разметки: заголовки, маркеры списков (включая «1)» и «а)»),
 * плашки, кавычки цитат, таблицы и код — оформление, а не содержание.
 * @param {string} md
 * @returns {string}
 */
export function svodPlainText(md) {
  let t = md === null || md === undefined ? '' : String(md);
  t = t.replace(/\r\n?/g, '\n');
  t = t.replace(/^ {0,3}(`{3,}|~{3,})[^\n]*$/gm, ' ');          // заборы кода
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  t = t.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
  t = t.replace(/^ {0,3}>[ \t]*\[![^\]]*\][ \t]*$/gm, ' ');       // плашки
  t = t.replace(/^ {0,3}#{1,6}[ \t]+/gm, '');
  t = t.replace(/^ {0,3}>[ \t]?/gm, '');
  // Метка-плашка («ИТОГ: …», «НЕ НАЙДЕНО — …») — оформление, а не текст: она
  // переезжает в заголовок плашки, поэтому в «голом» тексте её нет ни до, ни после.
  t = t.replace(new RegExp('^[ \\t]*(?:' + CALLOUT_LABELS.map(([l]) => l.replace(/ /g, '\\s+')).join('|')
    + ')[ \\t]*[:—–.\\-][ \\t]*', 'gmu'), '');
  // та же метка, начавшаяся посреди абзаца («… круге. НЕ ПРОВЕРЕНО: …»)
  t = t.replace(new RegExp('([.!?…][)»"]?[ \\t]+)(?:'
    + CALLOUT_LABELS.map(([l]) => l.replace(/ /g, '\\s+')).join('|')
    + ')[ \\t]*[:—–.\\-][ \\t]*', 'gu'), '$1');
  t = t.replace(/^[ \t]*\|?[ \t:|-]{4,}$/gm, ' ');
  t = t.replace(/^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/gm, ' ');
  t = t.replace(/^[ \t]*([-*+]|\d{1,9}[.)])[ \t]+/gm, '');
  t = t.replace(/(^|[\s(«"„“'])(?:\d{1,2}|[а-яё])[.)][ \t]+/gu, '$1');
  t = t.replace(/`+/g, '');
  // W26-QA2: в метке ссылки последний `\` пути записан как `\\` (иначе он
  // экранировал бы закрывающую скобку) — в «голом» тексте он один, как в источнике.
  t = t.replace(/\\\\/g, '\\');
  t = t.replace(/\*\*|~~/g, '');
  t = t.replace(/\|/g, ' ');
  t = t.replace(/[ \t]+/g, ' ');
  t = t.replace(/[ \t]*\n[ \t]*/g, '\n');
  t = t.replace(/\s+/g, ' ').trim();
  return t;
}

/* ── «Коротко» ────────────────────────────────────────────────────────── */

/** Снять с начала абзаца ту часть, которую уже показали в «Коротко» (W28b).
 *
 * Раньше блок убирался только при полном совпадении — если «Коротко» брало два
 * предложения из четырёх, они показывались дважды. Теперь отрезаем ровно те
 * предложения, из которых собрана карточка: остаток остаётся в своде.
 *
 * @param {string} mdText абзац в markdown
 * @param {string} headText текст «Коротко» (без разметки)
 * @returns {string} остаток абзаца ('' — если он весь ушёл в «Коротко»)
 */
function dropHeadPrefix(mdText, headText) {
  const head = String(headText || '').replace(/\s+/g, ' ').trim();
  const src = String(mdText || '').trim();
  if (!head || !src) return src;
  const sentences = splitSentences(src);
  if (!sentences.length) return src;
  if (sentences.length === 1) {
    return svodPlainText(src).replace(/\s+/g, ' ').trim() === head ? '' : src;
  }
  let acc = '';
  for (let i = 0; i < sentences.length; i += 1) {
    acc = acc ? acc + ' ' + sentences[i] : sentences[i];
    const plain = svodPlainText(acc).replace(/\s+/g, ' ').trim();
    if (plain === head) return sentences.slice(i + 1).join(' ').trim();
    if (plain.length > head.length) return src;
  }
  return src;
}

/**
 * Уложить текст в предел ПО ЦЕЛЫМ предложениям (W28b, дефект 9).
 *
 * Никакого «…» на обрыве: берём столько целых предложений, сколько влезает в
 * `max`. Если даже первое предложение длиннее предела — возвращаем его целиком
 * (законченная длинная мысль лучше оборванной), но никогда не режем посреди.
 *
 * @param {string} text
 * @param {number} [max]
 * @returns {string}
 */
export function fitSentences(text, max = SVOD_HEADLINE_MAX) {
  const one = String(text || '').replace(/\s+/g, ' ').trim();
  if (!one) return '';
  if (one.length <= max) return one;
  const sentences = splitSentences(one);
  if (sentences.length < 2) return one;
  const out = [];
  let len = 0;
  for (const s of sentences) {
    const next = out.length ? len + 1 + s.length : s.length;
    if (out.length && next > max) break;
    out.push(s);
    len = next;
  }
  return out.length ? out.join(' ') : one;
}

/**
 * «Коротко» → видимая часть и остаток для «Читать далее» (W28b, дефект 9).
 *
 * Раскрытие вместо троеточия: граница — конец предложения, поэтому видимый
 * кусок тоже законченный. Если предложение одно или текст короткий — остатка
 * нет и кнопка не нужна.
 *
 * @param {string} text
 * @param {number} [limit]
 * @returns {{lead: string, rest: string}}
 */
export function headlineSplit(text, limit = SVOD_HEADLINE_VISIBLE) {
  const one = String(text || '').replace(/\s+/g, ' ').trim();
  if (!one) return { lead: '', rest: '' };
  if (one.length <= limit) return { lead: one, rest: '' };
  const sentences = splitSentences(one);
  if (sentences.length < 2) return { lead: one, rest: '' };   // одно длинное — целиком
  const lead = [];
  let len = 0;
  for (const s of sentences) {
    const next = lead.length ? len + 1 + s.length : s.length;
    if (lead.length && next > limit) break;
    lead.push(s);
    len = next;
  }
  if (!lead.length || lead.length === sentences.length) return { lead: one, rest: '' };
  return { lead: lead.join(' '), rest: sentences.slice(lead.length).join(' ') };
}

/** Первый содержательный абзац свода. Плашка `> [!МЕТКА]` тоже считается:
 *  её текст — это и есть первая мысль свода (а не проза, идущая после списка). */
function firstParagraph(md) {
  const src = String(md || '').replace(/\r\n?/g, '\n');
  const lines = src.split('\n');
  let buf = null;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const blank = !line.trim();
    if (blank) {
      if (buf) return paragraphText(buf.join('\n'));
      continue;
    }
    if (CALLOUT_RE.test(line) || QUOTE_RE.test(line)) {
      if (buf) return paragraphText(buf.join('\n'));
      // текст плашки/цитаты: снимаем «>» и берём первый абзац
      const inner = [];
      i += 1;
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        inner.push(lines[i].replace(/^ {0,3}>[ \t]?/, ''));
        i += 1;
      }
      i -= 1;
      const firstInner = paragraphText(inner.join('\n').split(/\n[ \t]*\n/)[0] || '');
      if (firstInner) return firstInner;
      continue;
    }
    if (isBlockStart(lines, i)) {
      if (buf) return paragraphText(buf.join('\n'));
      // пропускаем блок кода целиком
      const fence = FENCE_RE.exec(line);
      if (fence) {
        const mark = fence[1];
        const closeRe = new RegExp('^ {0,3}' + (mark[0] === '`' ? '`' : '~') + '{' + mark.length + ',}[ \\t]*$');
        i += 1;
        while (i < lines.length && !closeRe.test(lines[i])) i += 1;
      }
      continue;
    }
    if (!buf) buf = [];
    buf.push(line);
  }
  return buf ? paragraphText(buf.join('\n')) : '';
}

/**
 * «Коротко» для карточки над сводом. Всегда законченная мысль, без «…».
 *
 * Порядок источников (W28b, дефект 9):
 *   1. `metadata.short` — то, что модель написала сама (2–4 предложения: ответ
 *      на вопрос, главный вывод, что делать). `metadata.headline` — старое имя;
 *   2. первый содержательный абзац/плашка свода;
 *   3. первая выделенная **жирным** мысль абзаца;
 *   4. целые предложения первого абзаца до предела (никогда не обрезаем).
 *
 * @param {string} md текст свода
 * @param {{short?:string, headline?:string}} [meta] `synthesis.metadata.svod`
 * @returns {{text:string, source:'metadata'|'paragraph'|'bold'|'sentences'}|null}
 */
export function svodHeadline(md, meta) {
  const m = meta && typeof meta === 'object' ? meta : {};
  const own = fitSentences(m.short || m.headline || '', SVOD_HEADLINE_MAX);
  if (own) return { text: own, source: 'metadata' };
  const first = firstParagraph(md);
  if (!first) return null;
  const plain = svodPlainText(first);
  if (!plain) return null;
  if (plain.length <= SVOD_HEADLINE_MAX) return { text: plain, source: 'paragraph' };
  const bold = /^\*\*([^*]{4,400})\*\*/u.exec(first.trim());
  const sentences = splitSentences(plain);
  if (bold && bold[1].trim().length <= SVOD_HEADLINE_MAX) {
    return { text: bold[1].trim(), source: 'bold' };
  }
  if (sentences.length) {
    return { text: fitSentences(plain, SVOD_HEADLINE_MAX), source: 'sentences' };
  }
  return null;
}

/** Разбить текст на блоки по пустым строкам (код не разрывается: внутри ``` нет пустых строк). */
function topBlocks(body) {
  const out = [];
  let cur = [];
  for (const line of String(body || '').split('\n')) {
    if (!line.trim()) {
      if (cur.length) { out.push(cur.join('\n')); cur = []; }
      continue;
    }
    cur.push(line);
  }
  if (cur.length) out.push(cur.join('\n'));
  return out;
}

/** Плашка → {inner: [абзацы]} (или null, если блок не плашка). */
function calloutParts(block) {
  const lines = String(block || '').split('\n');
  if (!CALLOUT_RE.test(lines[0] || '')) return null;
  const mark = lines[0];
  const inner = lines.slice(1).map((l) => l.replace(/^ {0,3}>[ \t]?/, ''));
  const paras = inner.join('\n').split(/\n[ \t]*\n/).map((p) => paragraphText(p)).filter(Boolean);
  return { mark, paras };
}

/**
 * «Коротко» + текст без повторения этой же фразы внутри.
 *
 * Повтор убираем на уровне блоков: если первая мысль — отдельный абзац, он уходит
 * в карточку «Коротко»; если это первая фраза плашки «ВЫВОД/ИТОГ» — уходит она,
 * а плашка остаётся с тем, что в ней ещё есть (или исчезает совсем).
 *
 * @param {string} md
 * @param {{headline?:string}} [meta]
 * @returns {{headline:string, body:string}}
 */
export function svodParts(md, meta) {
  const body = formatSvod(md);
  const head = svodHeadline(body, meta);
  if (!head) return { headline: '', body };
  const blocks = topBlocks(body);
  if (!blocks.length) return { headline: head.text, body };

  const first = blocks[0];
  const callout = calloutParts(first);
  let rest = blocks.slice();
  if (callout) {
    const inner0 = callout.paras[0] || '';
    const left0 = dropHeadPrefix(inner0, head.text);
    if (inner0 && left0 !== inner0) {
      const left = (left0 ? [left0] : []).concat(callout.paras.slice(1));
      if (left.length) {
        rest[0] = [callout.mark, ...left.map((p, idx) => (idx ? ['>', '> ' + p] : ['> ' + p]).join('\n'))].join('\n');
      } else {
        rest = rest.slice(1);
      }
    }
  } else {
    const leftFirst = dropHeadPrefix(first, head.text);
    if (!leftFirst) {
      // Над абзацем мог стоять заголовок «## Коротко» — он остался бы пустым.
      rest = rest.slice(1);
      if (rest.length && /^#{1,6}[ \t]+\S/.test(rest[0]) && (rest[0] || '').trim().length < 40) {
        rest = rest.slice(1);
      }
    } else if (leftFirst !== first) {
      // «Коротко» взяло первые предложения абзаца — остаток остаётся в своде.
      rest[0] = leftFirst;
    } else if (/^#{1,6}[ \t]+(коротко|итог|вывод)/i.test(first)
      && blocks[1] && svodPlainText(blocks[1]) === head.text) {
      // «## Коротко» + ровно тот же абзац под ним: заголовок и абзац уходят в карточку.
      rest = rest.slice(2);
    }
  }
  return { headline: head.text, body: rest.join('\n\n').trim() };
}

export default { formatSvod, svodPlainText, svodHeadline, svodParts, wrapPaths, splitSentences,
  inlineEnumeration, fitSentences, headlineSplit };
