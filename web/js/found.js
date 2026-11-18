/**
 * found.js — универсальный блок «Найдено» и кликабельные чипы-ссылки (W26-D4).
 *
 * Принцип Михаила: «что искали → что нашли → как открыть» для любого поиска —
 * диск, интернет, почта, база. Ядро не знает слов «отчёт» или «месторождение»:
 * тип объекта и отношение — справочник, который расширяется пакетом направления.
 *
 * Машинная форма (контракт `contract.summary_format_rule()`), блок в своде:
 *
 *     ```найдено
 *     - название: Годовой план
 *       тип: файл
 *       где: C:/Работа/Планы/Годовой план.md
 *       описание: план на год по направлениям
 *       отношение: искомое
 *       комментарий: раздел 3 отвечает на вопрос
 *     ```
 *
 * Понимается и JSON (массив объектов или `{искали, где_искали, найдено}`),
 * и обычный YAML-список; ключи — русские или английские.
 *
 * Публично:
 *   parseFound(src)            → {items, query, places, note}
 *   renderFoundBlock(src)      → HTML блока «Найдено» (карточки, группы)
 *   renderFoundFromText(text)  → запасной разбор обычного текста (пути/адреса)
 *   foundSection(md)           → текст раздела «Найдено» из свода | ''
 *   findChips(path)            → HTML чипа файла (тот же вид, что в своде)
 *   installFoundWiring(doc?)   → обработчик нажатий на чипы (в браузере — сам)
 *   checkChips(root)           → проверка существования файлов через /api/files/info
 *
 * Безопасность: все значения экранируются, HTML из данных не вставляется.
 */
import { esc } from './util.js';

/** Порядок групп и их подписи. */
export const FOUND_GROUPS = [
  ['exact', 'Нашли то, что искали'],
  ['mentions', 'Нашли упоминания'],
  ['adjacent', 'Нашли смежное'],
  ['wrong', 'Похожее, но не то'],
];

const RELATION = {
  'искомое': 'exact', 'искомый': 'exact', 'exact': 'exact', 'то самое': 'exact',
  'упоминает': 'mentions', 'упоминает искомое': 'mentions', 'упоминание': 'mentions',
  'mentions': 'mentions',
  'смежное': 'adjacent', 'смежное по теме': 'adjacent', 'adjacent': 'adjacent', 'related': 'adjacent',
  'не то': 'wrong', 'похожее но не то': 'wrong', 'похожее, но не то': 'wrong', 'wrong': 'wrong',
};

const TYPES = {
  'файл': 'file', 'папка': 'file', 'file': 'file', 'folder': 'file', 'dir': 'file',
  'сайт': 'site', 'ссылка': 'site', 'страница': 'site', 'site': 'site', 'url': 'site', 'web': 'site',
  'письмо': 'email', 'почта': 'email', 'email': 'email', 'mail': 'email',
  'запись': 'record', 'crm': 'record', 'record': 'record', 'задача': 'record', 'дело': 'record',
};

const TYPE_RU = { file: 'файл', site: 'сайт', email: 'письмо', record: 'запись' };
const REL_RU = { exact: 'Искомое', mentions: 'Упоминает искомое', adjacent: 'Смежное', wrong: 'Похожее, но не то' };

const KEY_SYNONYMS = {
  'название': 'title', 'имя': 'title', 'заголовок': 'title', 'title': 'title', 'name': 'title',
  'тип': 'type', 'вид': 'type', 'type': 'type', 'kind': 'type',
  'где': 'where', 'путь': 'where', 'адрес': 'where', 'ссылка': 'where', 'where': 'where',
  'path': 'where', 'url': 'where', 'location': 'where', 'файл': 'where',
  'описание': 'description', 'что это': 'description', 'description': 'description', 'desc': 'description',
  'отношение': 'relation', 'отношение к искомому': 'relation', 'relation': 'relation', 'match': 'relation',
  'комментарий': 'comment', 'примечание': 'comment', 'comment': 'comment', 'note': 'comment',
  'искали': 'query', 'что искали': 'query', 'query': 'query',
  'где искали': 'places', 'где_искали': 'places', 'places': 'places', 'источники': 'places',
  'поиск': 'query',
};

/** «Похожее, но не то» → метка; неизвестное отношение считаем смежным. */
export function relationKind(raw) {
  const k = String(raw === null || raw === undefined ? '' : raw).toLowerCase()
    .replace(/\s+/g, ' ').replace(/[«»"'()]/g, '').trim();
  if (!k) return 'adjacent';
  if (RELATION[k]) return RELATION[k];
  for (const key of Object.keys(RELATION)) {
    if (k.startsWith(key)) return RELATION[key];
  }
  return 'adjacent';
}

/** Вид объекта; по «где» догадываемся, если тип не задан. */
export function typeKind(raw, where) {
  const k = String(raw === null || raw === undefined ? '' : raw).trim().toLowerCase();
  if (TYPES[k]) return TYPES[k];
  const w = String(where || '').trim();
  if (/^https?:\/\//i.test(w)) return 'site';
  if (/^mailto:/i.test(w)) return 'email';
  if (w) return 'file';
  return 'record';
}

function unquote(v) {
  let s = String(v === null || v === undefined ? '' : v).trim();
  if (s.length > 1 && ((s[0] === '"' && s[s.length - 1] === '"') || (s[0] === "'" && s[s.length - 1] === "'"))) {
    s = s.slice(1, -1);
  }
  return s.replace(/\\n/g, '\n').trim();
}

function keyOf(raw) {
  const k = String(raw || '').trim().toLowerCase().replace(/\s+/g, ' ');
  return KEY_SYNONYMS[k] || '';
}

/** Одна запись «ключ: значение» → [поле, значение] (или null). */
function splitPair(line) {
  const at = line.indexOf(':');
  if (at <= 0) return null;
  const field = keyOf(line.slice(0, at));
  if (!field) return null;
  return [field, unquote(line.slice(at + 1))];
}

/** Значение-список: «[a, b]» или строка через запятую/точку с запятой. */
function asList(value) {
  const v = String(value || '').trim();
  if (!v) return [];
  if (v[0] === '[') {
    try {
      const parsed = JSON.parse(v.replace(/'/g, '"'));
      if (Array.isArray(parsed)) return parsed.map((x) => String(x).trim()).filter(Boolean);
    } catch { /* не JSON — режем запятыми */ }
    return v.replace(/^\[|\]$/g, '').split(/[;,]/).map((x) => unquote(x)).filter(Boolean);
  }
  if (v.indexOf(',') !== -1 || v.indexOf(';') !== -1) {
    return v.split(/[;,]/).map((x) => unquote(x)).filter(Boolean);
  }
  return [v];
}

/** JSON-запись (объект) → нормализованная карточка. */
function fromObject(obj) {
  const out = {};
  for (const key of Object.keys(obj || {})) {
    const field = keyOf(key);
    if (field) out[field] = obj[key];
  }
  return out;
}

/**
 * Разобрать машинный блок «Найдено» (YAML-список или JSON).
 * @param {string} src
 * @returns {{items:Array<{title:string,type:string,where:string,description:string,relation:string,comment:string}>,
 *            query:string, places:string[], note:string}}
 */
export function parseFound(src) {
  const text = String(src === null || src === undefined ? '' : src).replace(/^\uFEFF/, '').trim();
  const out = { items: [], query: '', places: [], note: '' };
  if (!text) return out;

  const push = (raw) => {
    if (!raw) return;
    const title = unquote(raw.title || raw.name || '');
    const where = unquote(raw.where || '');
    const comment = unquote(raw.comment || '');
    const description = unquote(raw.description || '');
    if (!title && !where) return;
    if (raw.places) out.places = out.places.concat(asList(raw.places));
    if (raw.query && !out.query) out.query = unquote(raw.query);
    out.items.push({
      title: title || where || 'Без названия',
      type: typeKind(raw.type, where),
      where,
      description,
      relation: relationKind(raw.relation),
      comment,
    });
  };

  // JSON: массив записей или объект с обёрткой
  if (text[0] === '[' || text[0] === '{') {
    try {
      const data = JSON.parse(text);
      if (Array.isArray(data)) {
        data.forEach((row) => push(fromObject(row)));
        return out;
      }
      if (data && typeof data === 'object') {
        const box = fromObject(data);
        if (box.query) out.query = unquote(box.query);
        if (box.places) out.places = out.places.concat(asList(box.places));
        const list = Array.isArray(data['найдено']) ? data['найдено']
          : Array.isArray(data.items) ? data.items
            : Array.isArray(data.found) ? data.found : [];
        list.forEach((row) => push(fromObject(row)));
        if (!out.items.length) push(box);
        return out;
      }
    } catch { /* не JSON — ниже разберём построчно */ }
  }

  // YAML-список: «- ключ: значение» + продолжение с отступом
  let cur = null;
  const flush = () => { if (cur) { push(cur); cur = null; } };
  for (const line of text.replace(/\r\n?/g, '\n').split('\n')) {
    const raw = line.replace(/\t/g, '    ');
    if (!raw.trim()) continue;
    if (/^---+\s*$/.test(raw.trim())) { flush(); continue; }
    const bullet = /^\s*[-*]\s+(.*)$/.exec(raw);
    if (bullet) {
      const rest = bullet[1].trim();
      if (rest[0] === '{' && rest[rest.length - 1] === '}') {
        try { flush(); cur = fromObject(JSON.parse(rest)); continue; } catch { /* строка-текст */ }
      }
      const pair = splitPair(rest);
      if (pair) { flush(); cur = {}; cur[pair[0]] = pair[1]; continue; }
      // пункт без ключа — «что искали / где искали»
      if (/^(искали|что искали|поиск)\s*:/i.test(rest)) { out.query = unquote(rest.split(':').slice(1).join(':')); continue; }
      if (!cur) cur = { title: unquote(rest) };
      else if (!cur.comment) cur.comment = unquote(rest);
      continue;
    }
    const pair = splitPair(raw.trim());
    if (!pair) continue;
    if (pair[0] === 'query') { out.query = pair[1]; continue; }
    if (pair[0] === 'places') { out.places = out.places.concat(asList(pair[1])); continue; }
    if (!cur) cur = {};
    if (cur[pair[0]] === undefined) cur[pair[0]] = pair[1];
    else if (pair[0] === 'comment') cur.comment = (cur.comment + ' ' + pair[1]).trim();
  }
  flush();
  return out;
}

/* ── HTML ──────────────────────────────────────────────────────────────── */

/** Абсолютный путь (Windows, UNC, Unix) — только такие становятся чипами. */
export function isAbsolutePath(path) {
  const p = String(path || '').trim();
  if (!p) return false;
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  if (p.slice(0, 2) === '\\\\') return true;
  if (p[0] === '/') return true;
  return p.slice(0, 2) === '~/';
}

function baseName(path) {
  const parts = String(path || '').split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || '');
}

function hostOf(url) {
  try {
    const m = /^https?:\/\/([^/?#]+)/i.exec(String(url));
    return m ? m[1] : '';
  } catch { return ''; }
}

/**
 * Чип файла: имя (нажатие = открыть) + действия. Те же классы использует свод.
 * @param {{path:string, label?:string, missing?:boolean}} opts
 * @returns {string} HTML
 */
export function fileChipHtml(opts) {
  const path = String((opts || {}).path || '').trim();
  const raw = String((opts || {}).label || '').trim();
  // Метка свода — сам путь: человеку показываем имя файла, полный путь — в подсказке.
  const label = (!raw || raw === path) ? baseName(path) : raw;
  const missing = !!(opts || {}).missing;
  const cls = ['md-file-chip'];
  if (missing) cls.push('md-file-chip--missing');
  const acts = missing ? '' : '<span class="md-file-chip__acts">'
    + '<a href="#" class="md-file-chip__act md-file-chip__act--open" title="Открыть файл в программе по умолчанию">Открыть</a>'
    + '<a href="#" class="md-file-chip__act md-file-chip__act--reveal" title="Показать файл в его папке">Показать в папке</a>'
    + '<a href="#" class="md-file-chip__act md-file-chip__act--copy" title="Скопировать полный путь">Копировать путь</a>'
    + '</span>';
  return '<span class="' + cls.join(' ') + '" data-path="' + esc(path) + '" data-name="' + esc(label) + '"'
    + ' title="' + esc(path) + '">'
    + '<a href="#" class="md-file-chip__name" title="' + esc(path) + '">' + esc(label) + '</a>'
    + (missing ? '<span class="md-file-chip__state">не найден</span>' : acts)
    + '</span>';
}

/**
 * Чип адреса: название (переход) + «Копировать».
 * @param {{url:string, label?:string}} opts
 * @returns {string}
 */
export function urlChipHtml(opts) {
  const url = String((opts || {}).url || '').trim();
  const label = String((opts || {}).label || '').trim() || url;
  return '<span class="md-url-chip" data-url="' + esc(url) + '">'
    + '<a class="md-url-chip__name" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer"'
    + ' title="Перейти на сайт: ' + esc(url) + '">' + esc(label) + '</a>'
    + '<a href="#" class="md-url-chip__act md-url-chip__act--copy" title="Скопировать ссылку">Копировать</a>'
    + '</span>';
}

function actionsHtml(item) {
  const where = item.where;
  const acts = [];
  if (item.type === 'file' && where) {
    acts.push('<a href="#" class="found-card__act md-file-chip__act--open">Открыть</a>');
    acts.push('<a href="#" class="found-card__act md-file-chip__act--reveal">Показать в папке</a>');
    acts.push('<a href="#" class="found-card__act md-file-chip__act--copy">Копировать путь</a>');
  } else if (/^https?:\/\//i.test(where)) {
    const label = item.type === 'email' ? 'Открыть письмо' : item.type === 'record' ? 'Открыть в CRM' : 'Перейти на сайт';
    acts.push('<a class="found-card__act" href="' + esc(where) + '" target="_blank" rel="noopener noreferrer">'
      + label + '</a>');
    acts.push('<a href="#" class="found-card__act md-url-chip__act--copy">Копировать ссылку</a>');
  } else if (/^mailto:/i.test(where)) {
    acts.push('<a class="found-card__act" href="' + esc(where) + '">Открыть письмо</a>');
    acts.push('<a href="#" class="found-card__act md-url-chip__act--copy">Копировать адрес</a>');
  } else if (where) {
    acts.push('<a href="#" class="found-card__act md-file-chip__act--copy">Копировать</a>');
  }
  return acts.length ? '<div class="found-card__acts">' + acts.join('') + '</div>' : '';
}

function cardHtml(item) {
  const cls = ['found-card', 'found-card--' + item.relation, 'found-card--' + item.type];
  const head = '<div class="found-card__head">'
    + (item.type === 'file' && item.where
      ? '<span class="found-card__name md-file-chip__name--link" role="link" tabindex="0">' + esc(item.title) + '</span>'
      : (item.where && /^https?:\/\//i.test(item.where)
        ? '<a class="found-card__name" href="' + esc(item.where) + '" target="_blank" rel="noopener noreferrer">' + esc(item.title) + '</a>'
        : '<span class="found-card__name">' + esc(item.title) + '</span>'))
    + '<span class="found-card__badge found-card__badge--' + item.relation + '">' + esc(REL_RU[item.relation]) + '</span>'
    + '<span class="found-card__type">' + esc(TYPE_RU[item.type] || '') + '</span>'
    + '</div>';
  const desc = item.description ? '<p class="found-card__desc">' + esc(item.description) + '</p>' : '';
  const where = item.where
    ? '<div class="found-card__where mono" title="' + esc(item.where) + '">'
      + esc(item.type === 'site' ? (hostOf(item.where) || item.where) : item.where) + '</div>'
    : '';
  const comment = item.comment ? '<div class="found-card__comment">' + esc(item.comment) + '</div>' : '';
  return '<div class="' + cls.join(' ') + '"' + (item.where && item.type === 'file' ? ' data-path="' + esc(item.where) + '"' : '')
    + (item.where ? ' data-where="' + esc(item.where) + '"' : '') + '>'
    + head + desc + where + comment + actionsHtml(item) + '</div>';
}

/**
 * Машинный блок «Найдено» → HTML карточек (группы, честная плашка).
 * @param {string} src содержимое блока ```найдено
 * @returns {string}
 */
export function renderFoundBlock(src) {
  const data = typeof src === 'string' ? parseFound(src) : (src || { items: [], places: [], query: '' });
  const items = Array.isArray(data.items) ? data.items : [];
  const groups = FOUND_GROUPS.map(([kind, title]) => [kind, title, items.filter((it) => it.relation === kind)]);
  const total = items.length;
  const exact = items.some((it) => it.relation === 'exact');
  const parts = [];
  parts.push('<div class="found-block" data-found="1">');
  parts.push('<div class="found-block__head"><span class="found-block__title">Найдено</span>'
    + (total ? '<span class="found-block__count">' + total + '</span>' : '') + '</div>');
  if (data.query) parts.push('<div class="found-block__query">Искали: ' + esc(data.query) + '</div>');
  if (data.note) parts.push('<div class="found-block__note">' + esc(data.note) + '</div>');
  if (total && !exact) {
    parts.push('<div class="found-block__honest found-block__honest--warn">'
      + 'Искомого нет; нашли смежное — проверьте, годится ли оно.</div>');
  }
  if (!total) {
    parts.push('<div class="found-block__honest found-block__honest--empty">'
      + 'Искомого нет' + (data.places && data.places.length
        ? ': искали в ' + esc(data.places.join(', ')) + '.'
        : '. Что и где искали — не указано.') + '</div>');
  }
  for (const [kind, title, list] of groups) {
    if (!list.length) continue;
    parts.push('<div class="found-block__group found-block__group--' + kind + '">'
      + '<div class="found-block__group-title">' + esc(title) + ' <span class="found-block__group-count">(' + list.length + ')</span></div>'
      + list.map(cardHtml).join('') + '</div>');
  }
  if (data.places && data.places.length) {
    parts.push('<div class="found-block__places">Где искали: ' + esc(data.places.join(' · ')) + '</div>');
  }
  parts.push('</div>');
  return parts.join('');
}

/* ── запасной разбор обычного текста ───────────────────────────────────── */

const PATH_IN_TEXT = /(?:[A-Za-z]:[\\/]|\\\\)[^\s`"'<>|]+[\\/][^\s`"'<>|]*|\/(?:[^\s`"'<>|]+\/)+[^\s`"'<>|]+/g;
const URL_IN_TEXT = /https?:\/\/[^\s`"'<>)\]]+/g;

/**
 * Свод написан обычным текстом (агент не дал машинный блок) — вытащить пути и
 * адреса из раздела «Найдено», чтобы они всё равно стали карточками.
 * @param {string} text
 * @returns {string} HTML ('' если ничего не нашлось)
 */
export function renderFoundFromText(text) {
  const src = String(text || '');
  if (!src.trim()) return '';
  const files = [];
  const urls = [];
  const seen = new Set();
  let m;
  // сначала — то, что агент сам выделил кодом: там пути с пробелами и «—»
  const CODE_RE = /`([^`\n]{3,500})`/g;
  while ((m = CODE_RE.exec(src))) {
    const t = m[1].trim();
    if (!t || seen.has(t)) continue;
    if (/^https?:\/\//i.test(t)) { seen.add(t); urls.push(t); continue; }
    if (isAbsolutePath(t)) { seen.add(t); files.push(t); }
  }
  PATH_IN_TEXT.lastIndex = 0;
  while ((m = PATH_IN_TEXT.exec(src))) {
    const p = m[0].replace(/[.,;:!?]+$/, '');
    if (p.length < 4 || seen.has(p)) continue;
    seen.add(p);
    files.push(p);
  }
  URL_IN_TEXT.lastIndex = 0;
  while ((m = URL_IN_TEXT.exec(src))) {
    const u = m[0].replace(/[.,;:!?]+$/, '');
    if (seen.has(u)) continue;
    seen.add(u);
    urls.push(u);
  }
  if (!files.length && !urls.length) return '';
  const items = [];
  for (const p of files.slice(0, 30)) {
    items.push({ title: baseName(p), type: 'file', where: p, description: '', relation: 'mentions', comment: '' });
  }
  for (const u of urls.slice(0, 30)) {
    items.push({ title: hostOf(u) || u, type: 'site', where: u, description: '', relation: 'mentions', comment: '' });
  }
  return renderFoundBlock({ items, query: '', places: [], note: 'разобрано из текста' });
}

/** Раздел «Найдено» из свода: заголовок «## Найдено / Что нашли» и текст под ним. */
export function foundSection(md) {
  const lines = String(md === null || md === undefined ? '' : md).replace(/\r\n?/g, '\n').split('\n');
  let from = -1;
  let level = 0;
  for (let i = 0; i < lines.length; i += 1) {
    const h = /^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$/.exec(lines[i]);
    if (!h) continue;
    if (/^(найдено|что нашли|найденное)/i.test(h[2].trim())) { from = i + 1; level = h[1].length; break; }
  }
  if (from < 0) return '';
  const out = [];
  for (let i = from; i < lines.length; i += 1) {
    // раздел кончается на любом следующем заголовке (### «Что дальше» тоже)
    if (/^ {0,3}(#{1,6})[ \t]+/.test(lines[i])) break;
    out.push(lines[i]);
  }
  return out.join('\n').trim();
}

export default {
  FOUND_GROUPS,
  parseFound,
  renderFoundBlock,
  renderFoundFromText,
  foundSection,
  fileChipHtml,
  urlChipHtml,
  installFoundWiring,
  checkChips,
};

/* ── живые действия чипов (браузер) ────────────────────────────────────── */

/** Куда открывать/показывать файл: маршрут волны 26 (D3) и понятный запас. */
const LAUNCH_URL = '/api/files/launch';
const INFO_URL = '/api/files/info';

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  return { ok: res.ok, status: res.status, data };
}

async function copyText(text) {
  const s = String(text || '');
  if (!s) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch { /* ниже — старый способ */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = s;
    ta.setAttribute('readonly', 'readonly');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand && document.execCommand('copy');
    document.body.removeChild(ta);
    return !!ok;
  } catch { return false; }
}

/** Короткое сообщение человеку (не тупик: всегда видно, что произошло). */
function toast(text) {
  try {
    const d = document;
    let box = d.querySelector('.found-toast');
    if (!box) {
      box = d.createElement('div');
      box.className = 'found-toast';
      box.setAttribute('role', 'status');
      d.body.appendChild(box);
    }
    box.textContent = text;
    box.classList.add('found-toast--on');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => box.classList.remove('found-toast--on'), 2600);
  } catch { /* без DOM — молча */ }
}

/**
 * Открыть файл: маршрут `POST /api/files/launch` (волна 26), затем прежний
 * `/api/open` (проводник) — чтобы кнопка никогда не была «мёртвой».
 */
/** Комната из адреса (`#/room/<id>/…`) — нужна серверу для белых корней комнаты. */
function roomFromHash() {
  try {
    const m = /^#\/room\/([a-z0-9_-]{2,32})/i.exec(String(location.hash || ''));
    return m ? m[1] : '';
  } catch { return ''; }
}

async function launchFile(path, action) {
  const p = String(path || '').trim();
  if (!p) return;
  const room = roomFromHash();
  const body = { path: p, mode: action };
  if (room) body.room = room;
  try {
    const res = await postJson(LAUNCH_URL, body);
    if (res.ok) return;
    if (res.status !== 404 && res.status !== 405) {
      const msg = res.data && (res.data.error || res.data.message);
      toast(msg || (action === 'reveal' ? 'Не удалось показать в папке' : 'Не удалось открыть файл'));
      return;
    }
  } catch { /* маршрута ещё нет — ниже запас */ }
  if (action === 'reveal') {
    try {
      const legacy = await postJson('/api/open', { path: p });
      if (legacy.ok) return;
    } catch { /* ниже */ }
    toast('Не удалось показать в папке');
    return;
  }
  // запас: файл отдаёт /api/file (внешнее окно может быть закрыто оболочкой)
  try {
    window.open('/api/file?path=' + encodeURIComponent(p), '_blank', 'noopener');
  } catch {
    toast('Открыть здесь нельзя — скопируйте путь');
  }
}

function actOf(node) {
  const cls = String((node && node.className) || '');
  const m = /--(open|reveal|copy)\b/.exec(cls);
  return m ? m[1] : '';
}

/** Один обработчик на весь документ: чипы свода, карточки «Найдено», таблицы. */
export function installFoundWiring(doc) {
  const d = doc || (typeof document !== 'undefined' ? document : null);
  if (!d || d.__foundWired) return;
  d.__foundWired = true;
  d.addEventListener('click', (ev) => {
    const target = ev && ev.target;
    if (!target || typeof target.closest !== 'function') return;
    const chip = target.closest('.md-file-chip, .md-url-chip, .found-card');
    if (!chip) return;
    const hit = target.closest('[class*="--open"], [class*="--reveal"], [class*="--copy"]');
    const nameNode = target.closest('.md-file-chip__name, .found-card__name');
    const act = hit ? actOf(hit) : (nameNode && chip.classList.contains('md-file-chip') ? 'open' : '');
    if (!act) return;
    const holder = chip.classList.contains('md-url-chip') ? chip : (target.closest('[data-path]') || chip);
    const path = holder.getAttribute && holder.getAttribute('data-path');
    const url = holder.getAttribute && holder.getAttribute('data-url');
    ev.preventDefault();
    ev.stopPropagation();
    if (typeof ev.stopImmediatePropagation === 'function') ev.stopImmediatePropagation();
    if (act === 'copy') {
      const value = url || path;
      copyText(value).then((ok) => toast(ok
        ? (url ? 'Ссылка скопирована' : 'Путь скопирован')
        : 'Не удалось скопировать — выделите вручную'));
      return;
    }
    if (url && act === 'open') {
      try { window.open(url, '_blank', 'noopener'); } catch { toast('Не удалось открыть ссылку'); }
      return;
    }
    launchFile(path, act).catch(() => toast('Не удалось выполнить действие'));
  }, true);
}

/**
 * Проверить, существуют ли файлы чипов, через `GET /api/files/info`.
 * Нет файла — чип становится серым «не найден» (ложной кнопки не остаётся).
 * @param {ParentNode} root
 */
export function checkChips(root) {
  const d = typeof document !== 'undefined' ? document : null;
  const scope = root || (d && d.querySelector ? d : null);
  if (!scope || typeof scope.querySelectorAll !== 'function' || typeof fetch !== 'function') return;
  const chips = Array.from(scope.querySelectorAll('.md-file-chip[data-path]:not([data-checked])'));
  for (const chip of chips.slice(0, 60)) {
    chip.setAttribute('data-checked', '1');
    const path = chip.getAttribute('data-path') || '';
    if (!path) continue;
    fetch(INFO_URL + '?path=' + encodeURIComponent(path))
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!data || data.exists !== false) return;
        chip.classList.add('md-file-chip--missing');
        chip.setAttribute('data-missing', '1');
        const acts = chip.querySelector('.md-file-chip__acts');
        if (acts) acts.remove();
        if (!chip.querySelector('.md-file-chip__state')) {
          const st = d.createElement('span');
          st.className = 'md-file-chip__state';
          st.textContent = 'не найден';
          chip.appendChild(st);
        }
      })
      .catch(() => { /* проверка недоступна — чип остаётся обычным */ });
  }
}

if (typeof document !== 'undefined' && document && document.addEventListener) {
  installFoundWiring(document);
}
