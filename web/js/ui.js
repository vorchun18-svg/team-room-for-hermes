/**
 * ui.js — общая библиотека элементов интерфейса «Комнаты команды».
 *
 * Правила, зашитые в файл:
 *  - наружу отдаём готовые DOM-узлы; текст кладём только текстовыми узлами;
 *  - единственный HTML в проекте — результат renderMarkdown() из md.js, и он
 *    разбирается встроенным мини-парсером в узлы (innerHTML не используется
 *    вообще), поэтому разметка агента не может выполнить скрипт;
 *  - стили — только токены tokens.css и классы components.css; добавки —
 *    в web/css/view-ui.css, который подключается лениво при первом вызове.
 *
 * Публичное API фиксировано (см. docs/status/view-ui.md).
 */

import { h, initials, hashColor, toDate, uid } from './util.js';
import { icon } from './icons.js';
import { renderMarkdown } from './md.js';
import { api } from './api.js';

/* ------------------------------------------------------------------ стили */

const CSS_HREF = 'css/view-ui.css';
const cssPromises = new Map();

/**
 * Подключить таблицу стилей один раз. Ошибка загрузки не блокирует вью.
 * @param {string} href
 * @returns {Promise<void>}
 */
export function ensureCss(href) {
  const key = String(href === null || href === undefined ? '' : href).trim();
  if (!key) return Promise.resolve();
  if (cssPromises.has(key)) return cssPromises.get(key);

  const d = globalThis.document;
  if (!d || !d.head || typeof d.createElement !== 'function') {
    const done = Promise.resolve();
    cssPromises.set(key, done);
    return done;
  }

  let already = null;
  try {
    if (typeof d.querySelectorAll === 'function') {
      already = d.querySelectorAll('link[rel="stylesheet"]');
    }
  } catch { already = null; }
  if (already) {
    for (const link of Array.from(already)) {
      if (link.getAttribute('href') === key) {
        const done = Promise.resolve();
        cssPromises.set(key, done);
        return done;
      }
    }
  }

  const p = new Promise((resolve) => {
    const link = d.createElement('link');
    link.setAttribute('rel', 'stylesheet');
    link.setAttribute('href', key);
    if (typeof link.addEventListener === 'function') {
      link.addEventListener('load', () => resolve());
      link.addEventListener('error', () => resolve());
    }
    d.head.appendChild(link);
    if (typeof link.addEventListener !== 'function') resolve();
  });
  cssPromises.set(key, p);
  return p;
}

let stylesPromise = null;

/** Ленивое подключение view-ui.css (первый вызов любого элемента). */
function useStyles() {
  if (!stylesPromise) stylesPromise = ensureCss(CSS_HREF);
  return stylesPromise;
}

/* -------------------------------------------------------------- статусы */

/**
 * @typedef {{label: string, tone: 'ok'|'warn'|'danger'|'info'|'muted'|'accent'|'outline'|'', pulse?: boolean, hint: string}} StatusMeta
 */

/** @type {Record<string, StatusMeta>} */
const STATUS_META = {
  /* запросы */
  queued: { label: 'в очереди', tone: 'muted', hint: 'Ждёт разбора на работы' },
  working: { label: 'в работе', tone: 'info', pulse: true, hint: 'Команда работает' },
  reviewing: { label: 'проверка', tone: 'info', pulse: true, hint: 'Критик проверяет свод' },
  synthesizing: { label: 'сводит', tone: 'info', pulse: true, hint: 'Руководитель комнаты дорабатывает свод' },
  needs_you: { label: 'нужен ответ', tone: 'warn', hint: 'Нужен ответ Михаила' },
  done: { label: 'готово', tone: 'ok', hint: 'Запрос завершён' },
  failed: { label: 'ошибка', tone: 'danger', hint: 'Выполнение сорвалось' },
  /* карточки канбана */
  triage: { label: 'разбор', tone: 'muted', hint: 'Ждёт разбора' },
  todo: { label: 'к выполнению', tone: 'muted', hint: 'Запланирована' },
  ready: { label: 'готова', tone: 'accent', hint: 'Готова к запуску' },
  running: { label: 'идёт', tone: 'info', pulse: true, hint: 'Выполняется' },
  review: { label: 'проверка', tone: 'info', pulse: true, hint: 'На проверке' },
  blocked: { label: 'заблокирована', tone: 'warn', hint: 'Ждёт зависимости или ответа' },
  archived: { label: 'архив', tone: 'muted', hint: 'В архиве' },
  scheduled: { label: 'отложена', tone: 'muted', hint: 'Отложена по расписанию' },
};

function normKey(v) {
  return String(v === null || v === undefined ? '' : v).trim().toLowerCase();
}

/**
 * Русская подпись статуса. Неизвестный статус возвращается как есть.
 * @param {string} status
 * @returns {string}
 */
export function formatStatusRu(status) {
  const key = normKey(status);
  if (!key) return '';
  const meta = STATUS_META[key];
  if (meta) return meta.label;
  const raw = String(status === null || status === undefined ? '' : status).trim();
  return raw || '';
}

/**
 * Чип статуса запроса или карточки.
 * @param {string} status  queued|working|reviewing|synthesizing|needs_you|done|failed
 *                         |triage|todo|ready|running|review|blocked|archived|scheduled
 * @returns {HTMLElement}
 */
export function statusChip(status) {
  useStyles();
  const key = normKey(status);
  const meta = STATUS_META[key];
  const tone = meta ? meta.tone : 'outline';
  const classes = ['chip', 'status-chip'];
  if (tone) classes.push('chip--' + tone);
  if (meta && meta.pulse) classes.push('status-chip--live');

  const el = h('span', {
    class: classes,
    title: meta ? meta.hint : undefined,
    'data-status': key || undefined,
  });
  if (meta && meta.pulse) el.appendChild(h('span', { class: 'chip__dot chip__dot--pulse' }));
  el.appendChild(h('span', { class: 'status-chip__text' }, meta ? meta.label : (formatStatusRu(status) || '—')));
  return el;
}

/* ------------------------------------------------------------------ роли */

/** @type {Record<string, {label: string, tone: string, hint: string}>} */
const ROLE_META = {
  root: { label: 'корень', tone: 'accent', hint: 'Главная задача запроса' },
  worker: { label: 'исполнитель', tone: 'muted', hint: 'Исполнитель' },
  synthesizer: { label: 'руководитель', tone: 'info', hint: 'Руководитель комнаты сводит ответ' },
  critic: { label: 'критик', tone: 'warn', hint: 'Проверяет свод' },
  revision: { label: 'доработка', tone: 'info', hint: 'Доработка ответа' },
  /* роли участника комнаты */
  orchestrator: { label: 'руководитель', tone: 'accent', hint: 'Руководитель комнаты: раздаёт работу и сводит ответ' },
  observer: { label: 'наблюдатель', tone: 'muted', hint: 'Наблюдатель' },
};

/**
 * Чип роли в дереве запроса или в составе комнаты.
 * @param {string} role  root|worker|synthesizer|critic|revision|orchestrator|observer
 * @returns {HTMLElement}
 */
export function roleChip(role) {
  useStyles();
  const key = normKey(role);
  const meta = ROLE_META[key];
  const classes = ['chip', 'role-chip'];
  if (meta) classes.push('chip--' + meta.tone);
  else classes.push('chip--outline');
  const el = h('span', {
    class: classes,
    title: meta ? meta.hint : undefined,
    'data-role': key || undefined,
  });
  const label = meta ? meta.label : (String(role === null || role === undefined ? '' : role).trim() || '—');
  el.appendChild(h('span', { class: 'role-chip__text' }, label));
  return el;
}

/* --------------------------------------------------------------- аватары */

function hueOf(name) {
  const raw = hashColor(name);
  const m = /hsl\(\s*(\d+)/i.exec(String(raw));
  return m ? Number(m[1]) : 220;
}

/**
 * Кружок с инициалами; цвет детерминирован именем.
 * @param {string} name
 * @param {{size?: 'sm'|'md'|'lg'|number, title?: string}} [opts]
 * @returns {HTMLElement}
 */
export function avatar(name, opts = {}) {
  useStyles();
  const o = opts || {};
  const nm = name === null || name === undefined ? '' : String(name);
  const size = o.size === undefined ? 'md' : o.size;
  const classes = ['avatar'];
  const style = { '--h': hueOf(nm) };
  if (typeof size === 'number' && isFinite(size) && size > 0) {
    const px = Math.round(size);
    style.width = px + 'px';
    style.height = px + 'px';
    style.fontSize = Math.max(8, Math.round(px * 0.4)) + 'px';
  } else if (size === 'sm' || size === 'lg') {
    classes.push('avatar--' + size);
  }
  const el = h('span', {
    class: classes,
    style,
    role: 'img',
    'aria-label': nm || 'Без имени',
    title: o.title === undefined ? undefined : o.title,
  });
  el.textContent = initials(nm);
  return el;
}

/**
 * Аватар + имя профиля.
 * @param {string|{profile?: string, id?: string, name?: string, display_name?: string, title?: string}|null} profile
 * @param {{size?: 'sm'|'md'|'lg'}} [opts]
 * @returns {HTMLElement}
 */
export function profileLabel(profile, opts = {}) {
  useStyles();
  const o = opts || {};
  const isStr = typeof profile === 'string';
  const id = isStr ? profile : String((profile && (profile.profile || profile.id)) || '');
  const name = isStr
    ? profile
    : String((profile && (profile.name || profile.display_name || profile.title)) || id || '');
  const el = h('span', { class: 'profile-label', title: id || name || undefined });
  const av = avatar(name || id, { size: o.size || 'sm' });
  av.setAttribute('aria-hidden', 'true');
  el.appendChild(av);
  el.appendChild(h('span', { class: 'profile-label__name truncate' }, name || id || '—'));
  return el;
}

/* ------------------------------------------------------- пусто/загрузка */

/**
 * Пустое состояние с подсказкой, что делать.
 * @param {{icon?: string, title?: string, text?: string,
 *          action?: {label: string, onclick?: Function, variant?: string}|Array}} [opts]
 * @returns {HTMLElement}
 */
export function emptyState(opts = {}) {
  useStyles();
  const o = opts || {};
  const el = h('div', { class: 'empty' });
  el.appendChild(icon(o.icon || 'inbox', { size: 22 }));
  el.appendChild(h('div', { class: 'empty__title' }, o.title || 'Пока пусто'));
  if (o.text) el.appendChild(h('div', { class: 'empty__text' }, o.text));

  const list = Array.isArray(o.action) ? o.action : (o.action ? [o.action] : []);
  if (list.length) {
    const actions = h('div', { class: 'empty__actions' });
    for (const a of list) {
      if (!a || !a.label) continue;
      const variant = a.variant === 'primary' ? 'primary' : (a.variant || 'secondary');
      actions.appendChild(h('button', {
        class: ['btn', 'btn--' + variant],
        type: 'button',
        onclick: typeof a.onclick === 'function' ? a.onclick : undefined,
      }, String(a.label)));
    }
    el.appendChild(actions);
  }
  return el;
}

/**
 * Заглушка загрузки: строка-заголовок и линии.
 * @param {number} [lines=3]
 * @returns {HTMLElement}
 */
export function skeleton(lines = 3) {
  useStyles();
  const raw = Math.floor(Number(lines));
  const n = isFinite(raw) ? Math.max(1, Math.min(12, raw)) : 3;
  const el = h('div', {
    class: 'skeleton-stack',
    role: 'status',
    'aria-busy': 'true',
    'aria-label': 'Загрузка',
  });
  for (let i = 0; i < n; i++) {
    const cls = i === 0
      ? 'skeleton skeleton--title'
      : (i % 3 === 0 ? 'skeleton skeleton--line skeleton--short' : 'skeleton skeleton--line');
    el.appendChild(h('div', { class: cls }));
  }
  return el;
}

/**
 * Полоса ошибки с кнопкой «Повторить».
 * @param {Error|{message?: string, code?: string}|string} err
 * @param {Function} [onRetry]
 * @returns {HTMLElement}
 */
export function errorBanner(err, onRetry) {
  useStyles();
  const e = err && typeof err === 'object' ? err : null;
  const message = e && e.message
    ? String(e.message)
    : (typeof err === 'string' && err.trim() ? err.trim() : 'Не удалось загрузить данные');
  const code = e && e.code ? String(e.code) : '';

  const textBox = h('div', { class: 'banner__text' }, h('div', { class: 'error-banner__message' }, message));
  if (code === 'network') {
    textBox.appendChild(h('div', { class: 'small error-banner__hint' }, 'Проверьте, что приложение запущено на этом компьютере (start-team-room.cmd).'));
  } else if (code) {
    textBox.appendChild(h('div', { class: 'small error-banner__hint mono' }, code));
  }

  const el = h('div', { class: 'banner banner--error error-banner', role: 'alert' }, icon('error', { size: 14 }), textBox);
  if (typeof onRetry === 'function') {
    el.appendChild(h('button', {
      class: 'btn btn--sm btn--secondary error-banner__retry',
      type: 'button',
      onclick: onRetry,
    }, icon('refresh', { size: 12 }), 'Повторить'));
  }
  return el;
}

/* ---------------------------------------------------------------- markdown */

const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'area', 'base', 'col', 'embed', 'param', 'track', 'wbr']);
const ALLOWED_TAGS = new Set([
  'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'hr', 'br', 'a', 'img', 'strong',
  'b', 'em', 'i', 'del', 's', 'details', 'summary', 'span', 'div', 'input', 'sup', 'sub', 'kbd',
]);
const ALLOWED_ATTRS = new Set(['class', 'href', 'src', 'alt', 'title', 'target', 'rel', 'type', 'checked', 'disabled', 'loading', 'data-path']);

const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };

/** Раскодировать сущности, которые выдаёт esc()/md.js. */
function decodeEntities(s) {
  const str = String(s);
  if (str.indexOf('&') === -1) return str;
  return str.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);/g, (m, body) => {
    if (body[0] === '#') {
      const hex = body[1] === 'x' || body[1] === 'X';
      const code = hex ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      if (!isFinite(code) || code <= 0 || code > 0x10ffff) return m;
      try { return String.fromCodePoint(code); } catch { return m; }
    }
    const v = NAMED_ENTITIES[body];
    return v === undefined ? m : v;
  });
}

/** Найти '>' открывающего тега, не задевая кавычки в значениях. */
function findTagEnd(src, from) {
  let quote = '';
  for (let i = from; i < src.length; i++) {
    const c = src[i];
    if (quote) { if (c === quote) quote = ''; continue; }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === '>') return i;
  }
  return -1;
}

/** Разобрать текст атрибутов в плоский объект. */
function parseAttrs(text) {
  const out = Object.create(null);
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  let m;
  while ((m = re.exec(text))) {
    const name = m[1].toLowerCase();
    if (name in out) continue;
    const raw = m[2] !== undefined ? m[2] : (m[3] !== undefined ? m[3] : m[4]);
    out[name] = raw === undefined ? true : decodeEntities(raw);
  }
  return out;
}

const UNSAFE_SCHEME = /^(?:javascript|vbscript|file):/i;
const DATA_IMAGE = /^data:image\//i;

/** Перенести безопасные атрибуты на узел. */
function applySafeAttrs(el, tag, attrs) {
  for (const name of Object.keys(attrs)) {
    const value = attrs[name];
    if (name === 'style' && (tag === 'th' || tag === 'td')) {
      const m = /text-align\s*:\s*(left|center|right)/i.exec(String(value));
      if (m && el.style) el.style.textAlign = m[1].toLowerCase();
      continue;
    }
    if (!ALLOWED_ATTRS.has(name)) continue;
    if (name === 'href' || name === 'src') {
      const v = String(value).trim();
      const probe = v.replace(/[\u0000-\u0020\u007f]/g, '');
      if (!probe || UNSAFE_SCHEME.test(probe)) continue;
      if (/^data:/i.test(probe) && !(name === 'src' && DATA_IMAGE.test(probe))) continue;
      el.setAttribute(name, v);
      continue;
    }
    if (name === 'checked' || name === 'disabled') {
      el.setAttribute(name, name);
      continue;
    }
    el.setAttribute(name, String(value));
  }
}

/**
 * Разобрать уже санитизированный HTML md.js в узлы — без innerHTML.
 * Незнакомые теги и атрибуты отбрасываются, текстовые узлы создаются
 * текстом (никогда не разметкой).
 * @param {string} html
 * @param {Document} doc
 * @returns {HTMLElement} контейнер с готовыми детьми
 */
function htmlToNodes(html, doc) {
  const root = doc.createElement('div');
  const stack = [root];
  const src = String(html === null || html === undefined ? '' : html);
  const top = () => stack[stack.length - 1];

  const appendText = (raw) => {
    if (!raw) return;
    const text = decodeEntities(raw);
    if (!text) return;
    top().appendChild(doc.createTextNode(text));
  };

  const closeTag = (name) => {
    for (let k = stack.length - 1; k > 0; k--) {
      const tag = String(stack[k].localName || stack[k].tagName || '').toLowerCase();
      if (tag === name) { stack.length = k; return; }
    }
  };

  let i = 0;
  while (i < src.length) {
    const lt = src.indexOf('<', i);
    if (lt === -1) { appendText(src.slice(i)); break; }
    if (lt > i) appendText(src.slice(i, lt));
    if (src.startsWith('<!--', lt)) {
      const end = src.indexOf('-->', lt + 4);
      i = end === -1 ? src.length : end + 3;
      continue;
    }
    const gt = findTagEnd(src, lt + 1);
    if (gt === -1) { appendText(src.slice(lt)); break; }
    const raw = src.slice(lt + 1, gt);
    i = gt + 1;
    if (!raw) continue;

    if (raw[0] === '/') { closeTag(raw.slice(1).trim().toLowerCase()); continue; }

    const selfClose = /\/\s*$/.test(raw);
    const body = selfClose ? raw.replace(/\/\s*$/, '') : raw;
    const m = /^([a-zA-Z][a-zA-Z0-9-]*)/.exec(body);
    if (!m) continue;
    const tag = m[1].toLowerCase();
    const attrs = parseAttrs(body.slice(m[0].length));
    if (!ALLOWED_TAGS.has(tag)) continue; // тег выкидываем, содержимое остаётся текстом

    const el = doc.createElement(tag);
    applySafeAttrs(el, tag, attrs);
    top().appendChild(el);
    if (!selfClose && !VOID_TAGS.has(tag)) stack.push(el);
  }
  return root;
}

/**
 * Блок Markdown: единственный путь HTML агента в DOM.
 * @param {string} src
 * @param {{fileUrl?: (path: string) => string}} [opts]
 * @returns {HTMLElement} div.markdown
 */
export function mdBlock(src, opts = {}) {
  useStyles();
  const o = opts || {};
  const d = globalThis.document;
  if (!d || typeof d.createElement !== 'function') {
    throw new Error('ui.mdBlock: нет document');
  }
  const el = h('div', { class: 'markdown' });
  const html = renderMarkdown(src === null || src === undefined ? '' : String(src), {
    fileUrl: typeof o.fileUrl === 'function' ? o.fileUrl : undefined,
  });
  const holder = htmlToNodes(html, d);
  for (const node of Array.from(holder.childNodes)) el.appendChild(node);

  el.addEventListener('click', (ev) => {
    const target = ev && ev.target;
    if (!target || typeof target.closest !== 'function') return;
    const link = target.closest('a[data-path]');
    if (!link) return;
    ev.preventDefault();
    openPath(link.getAttribute('data-path')).catch(() => {});
  });
  return el;
}

/* ------------------------------------------------------------ файлы/пути */

const PREVIEW_RE = /\.(?:md|markdown|txt|log|json|jsonl|csv|tsv|ya?ml|xml|html?|pdf|png|jpe?g|gif|webp|svg|bmp|avif|ics|py|js|css|sh)$/i;

/**
 * Открыть путь: превьюемые типы — новой вкладкой через /api/file,
 * остальные — «показать в проводнике» (POST /api/open).
 * @param {string} path
 * @returns {Promise<void>}
 */
export async function openPath(path) {
  const p = path === null || path === undefined ? '' : String(path).trim();
  if (!p) return;
  const url = '/api/file?path=' + encodeURIComponent(p);
  const openTab = () => {
    if (typeof window !== 'undefined' && typeof window.open === 'function') {
      window.open(url, '_blank', 'noopener');
      return true;
    }
    return false;
  };
  if (PREVIEW_RE.test(p)) { openTab(); return; }
  try {
    await api.post('/api/open', { path: p });
  } catch (err) {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('ui.openPath: не удалось открыть в проводнике', err);
    }
    openTab();
  }
}

function shortPath(p) {
  const s = String(p);
  if (s.length <= 46) return s;
  const parts = s.split(/[\\/]+/).filter(Boolean);
  if (parts.length >= 2) {
    const tail = parts.slice(-2).join('/');
    return '…/' + (tail.length > 44 ? parts[parts.length - 1].slice(0, 42) + '…' : tail);
  }
  return s.slice(0, 22) + '…' + s.slice(-20);
}

/**
 * Чип пути к файлу; клик открывает файл.
 * @param {string} path
 * @returns {HTMLElement}
 */
export function pathChip(path) {
  useStyles();
  const p = path === null || path === undefined ? '' : String(path);
  const el = h('button', {
    class: 'chip chip--outline path-chip',
    type: 'button',
    title: p || undefined,
    'aria-label': p ? 'Открыть: ' + p : 'Файл',
  // B8 (пакет G): у обрезаемого текста своя подсказка — полный путь.
  }, icon('file-text', { size: 11 }), h('span', { class: 'path-chip__text mono', title: p || undefined }, shortPath(p)));
  el.addEventListener('click', () => { openPath(p).catch(() => {}); });
  return el;
}

/* ------------------------------------------------------------------ поле */

/**
 * Подпись + контрол + подсказка. Для input/textarea/select создаётся <label>.
 * @param {{label?: string, control?: HTMLElement, hint?: string, error?: string, id?: string}} [opts]
 * @returns {HTMLElement}
 */
export function field(opts = {}) {
  useStyles();
  const o = opts || {};
  const control = o.control || null;
  const tag = control ? String(control.tagName || '').toUpperCase() : '';
  const isForm = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  const wrap = h(isForm ? 'label' : 'div', { class: 'field' });

  if (o.label) wrap.appendChild(h('span', { class: 'field__label' }, String(o.label)));
  if (control) {
    if (isForm) {
      if (!control.id) control.id = o.id || uid('fld');
      wrap.setAttribute('for', control.id);
    }
    wrap.appendChild(control);
  }
  if (o.hint) wrap.appendChild(h('span', { class: 'field__hint' }, String(o.hint)));
  if (o.error) wrap.appendChild(h('span', { class: 'field__error' }, String(o.error)));
  return wrap;
}

/* ---------------------------------------------------------------- кнопки */

/**
 * Квадратная иконочная кнопка с подписью для скринридера.
 * @param {{icon?: string, title?: string, onclick?: Function,
 *          variant?: 'ghost'|'primary'|'secondary'|'danger', size?: 'sm'|'md'}} [opts]
 * @returns {HTMLElement}
 */
export function iconButton(opts = {}) {
  useStyles();
  const o = opts || {};
  const variant = o.variant || 'ghost';
  const classes = ['btn', 'btn--icon'];
  if (variant !== 'ghost') classes.push('btn--' + variant);
  else classes.push('btn--ghost');
  if (o.size === 'sm') classes.push('btn--sm');
  const label = o.title ? String(o.title) : '';
  const btn = h('button', {
    class: classes,
    type: 'button',
    title: label || undefined,
    'aria-label': label || undefined,
    onclick: typeof o.onclick === 'function' ? o.onclick : undefined,
  });
  btn.appendChild(icon(o.icon || 'ellipsis', { size: o.size === 'sm' ? 13 : 14 }));
  return btn;
}

/* ------------------------------------------------------- заголовок секции */

/**
 * Заголовок секции с счётчиком и действиями справа.
 * @param {string} title
 * @param {{count?: number, actions?: HTMLElement|HTMLElement[]}} [opts]
 * @returns {HTMLElement}
 */
export function sectionHeader(title, opts = {}) {
  useStyles();
  const o = opts || {};
  const left = h('div', { class: 'row row--tight section-header__main' },
    h('h2', { class: 'section-header__title' }, String(title === null || title === undefined ? '' : title)));
  const count = o.count === null || o.count === undefined ? NaN : Number(o.count);
  if (isFinite(count)) {
    left.appendChild(h('span', { class: 'chip chip--muted section-header__count tnum' }, String(Math.trunc(count))));
  }

  const right = h('div', { class: 'row row--tight section-header__actions' });
  const actions = Array.isArray(o.actions) ? o.actions : (o.actions ? [o.actions] : []);
  for (const a of actions) if (a) right.appendChild(a);

  const el = h('div', { class: 'section-header row row--between' }, left);
  if (right.childNodes.length) el.appendChild(right);
  return el;
}

/* ------------------------------------------------------------------ сроки */

const MONTHS_SHORT = ['янв.', 'февр.', 'мар.', 'апр.', 'мая', 'июн.', 'июл.', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.'];

function startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function parseDue(due) {
  if (due instanceof Date) return isNaN(due.getTime()) ? null : startOfDay(due);
  if (typeof due === 'number') {
    const d = toDate(due);
    return d ? startOfDay(d) : null;
  }
  const s = String(due === null || due === undefined ? '' : due).trim();
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T]|$)/.exec(s);
  if (m) {
    const y = +m[1]; const mo = +m[2]; const day = +m[3];
    const d = new Date(y, mo - 1, day);
    if (isNaN(d.getTime())) return null;
    if (d.getFullYear() !== y || d.getMonth() !== mo - 1 || d.getDate() !== day) return null;
    return d;
  }
  const d = toDate(s);
  return d ? startOfDay(d) : null;
}

function shortDate(d, refYear) {
  const tail = d.getFullYear() !== refYear ? ' ' + d.getFullYear() : '';
  return `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]}${tail}`;
}

/**
 * Чип срока: просрочено (красный), сегодня/завтра (жёлтый), дальше — нейтральный.
 * @param {string|Date|number|null} due  дата 'YYYY-MM-DD'
 * @param {Date|number|string} [now]  опорный момент (для тестов)
 * @returns {HTMLElement|null}
 */
export function dueBadge(due, now) {
  const d = parseDue(due);
  if (!d) return null;
  useStyles();

  const refRaw = now === undefined || now === null ? new Date() : now;
  const refDate = refRaw instanceof Date ? refRaw : toDate(refRaw);
  const today = refDate && !isNaN(refDate.getTime()) ? startOfDay(refDate) : startOfDay(new Date());

  const diff = Math.round((d.getTime() - today.getTime()) / 86400000);
  let tone = '';
  let label;
  if (diff < 0) {
    tone = 'danger';
    label = 'просрочено · ' + shortDate(d, today.getFullYear());
  } else if (diff === 0) {
    tone = 'warn';
    label = 'сегодня';
  } else if (diff === 1) {
    tone = 'warn';
    label = 'завтра';
  } else {
    label = 'до ' + shortDate(d, today.getFullYear());
  }

  const classes = ['chip', 'due-badge'];
  if (tone) classes.push('chip--' + tone);
  const el = h('span', {
    class: classes,
    title: `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]} ${d.getFullYear()}`,
    'data-due': `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`,
  }, icon('calendar', { size: 11 }), h('span', { class: 'due-badge__text' }, label));
  return el;
}

/* ------------------------------------------------------------- сегменты */

/**
 * Переключатель из 2–5 вариантов (роли radio, стрелки, Home/End).
 * @param {{options?: Array<{value: any, label: string}>, value?: any, onchange?: (v:any)=>void}} [opts]
 * @returns {HTMLElement}
 */
export function segmented(opts = {}) {
  useStyles();
  const o = opts || {};
  const options = Array.isArray(o.options) ? o.options.filter((x) => x && x.value !== undefined) : [];
  const el = h('div', { class: 'segmented', role: 'radiogroup' });
  const buttons = [];
  let value = o.value === undefined ? (options[0] ? options[0].value : undefined) : o.value;

  const apply = (v, fire) => {
    value = v;
    buttons.forEach((b, idx) => {
      const on = String(options[idx].value) === String(v);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.classList.toggle('segmented__btn--active', on);
      b.tabIndex = on ? 0 : -1;
    });
    if (fire && typeof o.onchange === 'function') o.onchange(v);
  };

  options.forEach((opt) => {
    const btn = h('button', {
      class: 'segmented__btn',
      type: 'button',
      role: 'radio',
      'aria-checked': 'false',
      'data-value': String(opt.value),
    }, String(opt.label === undefined ? opt.value : opt.label));
    btn.addEventListener('click', () => apply(opt.value, true));
    buttons.push(btn);
    el.appendChild(btn);
  });

  el.addEventListener('keydown', (ev) => {
    const idx = buttons.indexOf(ev.target);
    if (idx === -1) return;
    let next = -1;
    const last = buttons.length - 1;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') next = idx === last ? 0 : idx + 1;
    else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') next = idx === 0 ? last : idx - 1;
    else if (ev.key === 'Home') next = 0;
    else if (ev.key === 'End') next = last;
    if (next === -1 || !buttons[next]) return;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    apply(options[next].value, true);
    if (typeof buttons[next].focus === 'function') buttons[next].focus();
  });

  apply(value, false);
  return el;
}

/* --------------------------------------------------------------- буфер */

/**
 * Скопировать текст; true при успехе. Фолбэк — скрытая textarea.
 * @param {string} text
 * @returns {Promise<boolean>}
 */
export async function copyText(text) {
  const s = text === null || text === undefined ? '' : String(text);
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch { /* уходим в фолбэк */ }

  const d = globalThis.document;
  if (!d || !d.body || typeof d.execCommand !== 'function') return false;
  const ta = d.createElement('textarea');
  ta.value = s;
  ta.setAttribute('readonly', '');
  ta.setAttribute('aria-hidden', 'true');
  if (ta.style) {
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
  }
  d.body.appendChild(ta);
  let ok = false;
  try {
    if (typeof ta.select === 'function') ta.select();
    ok = !!d.execCommand('Copy');
  } catch { ok = false; }
  if (typeof ta.remove === 'function') ta.remove();
  else if (ta.parentNode) ta.parentNode.removeChild(ta);
  return ok;
}

/* ------------------------------------------------------------ localStorage */

const LS_PREFIX = 'teamroom:';

function lsStore() {
  try {
    return globalThis.localStorage || null;
  } catch { return null; }
}

/**
 * Прочитать JSON из localStorage (префикс teamroom:).
 * @param {string} key
 * @param {*} [def=null] значение при отсутствии/битом JSON
 * @returns {*}
 */
export function lsGet(key, def = null) {
  const s = lsStore();
  if (!s) return def;
  try {
    const raw = s.getItem(LS_PREFIX + String(key));
    if (raw === null || raw === undefined) return def;
    return JSON.parse(raw);
  } catch { return def; }
}

/**
 * Записать JSON в localStorage. `undefined` удаляет ключ.
 * @param {string} key
 * @param {*} val
 * @returns {boolean} удалось ли
 */
export function lsSet(key, val) {
  const s = lsStore();
  if (!s) return false;
  try {
    const k = LS_PREFIX + String(key);
    if (val === undefined) { s.removeItem(k); return true; }
    s.setItem(k, JSON.stringify(val));
    return true;
  } catch { return false; }
}

/* --------------------------------------------------------------- диалоги */

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(root) {
  try {
    if (!root || typeof root.querySelectorAll !== 'function') return [];
    return Array.from(root.querySelectorAll(FOCUSABLE)).filter(Boolean);
  } catch { return []; }
}

function trapTab(ev, root) {
  if (ev.key !== 'Tab') return;
  const items = focusables(root);
  if (!items.length) { ev.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  const active = globalThis.document ? globalThis.document.activeElement : null;
  if (ev.shiftKey && (active === first || !root.contains(active))) {
    ev.preventDefault();
    if (typeof last.focus === 'function') last.focus();
  } else if (!ev.shiftKey && active === last) {
    ev.preventDefault();
    if (typeof first.focus === 'function') first.focus();
  }
}

/**
 * Создать каркас диалога, смонтировать его и вернуть управление.
 * @param {{title?: string, wide?: boolean, body?: Node|string, buttons?: HTMLElement[],
 *          onCancel: Function, onKey?: Function, initialFocus?: HTMLElement}} cfg
 * @returns {{dialog: HTMLElement, body: HTMLElement, foot: HTMLElement, close: Function}}
 */
/**
 * Собрать модальное окно (общий каркас для confirm/prompt и своих диалогов).
 * @param {{title?: string, body?: any, buttons?: HTMLElement[], initialFocus?: HTMLElement,
 *          wide?: boolean, onCancel: Function, onKey?: Function}} cfg
 * @returns {{dialog: HTMLElement, body: HTMLElement, foot: HTMLElement, overlay: HTMLElement, close: Function}}
 */
export function buildDialog(cfg) {
  const d = globalThis.document;
  const cfgObj = cfg || {};
  const titleId = uid('dlg');
  const prevFocus = d ? d.activeElement : null;

  const dialog = h('div', {
    class: ['dialog', cfgObj.wide ? 'dialog--wide' : ''],
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': titleId,
  });

  const head = h('div', { class: 'dialog__head' },
    h('h2', { class: 'dialog__title', id: titleId }, String(cfgObj.title || '')),
    iconButton({ icon: 'close', size: 'sm', title: 'Закрыть', onclick: () => cfgObj.onCancel() }));
  const body = h('div', { class: 'dialog__body' });
  if (cfgObj.body) {
    if (typeof cfgObj.body === 'string') body.appendChild(h('div', { class: 'dialog__text' }, cfgObj.body));
    else body.appendChild(cfgObj.body);
  }
  const foot = h('div', { class: 'dialog__foot' });
  for (const b of cfgObj.buttons || []) if (b) foot.appendChild(b);
  dialog.appendChild(head);
  dialog.appendChild(body);
  dialog.appendChild(foot);

  const overlay = h('div', { class: 'dialog-backdrop' });
  overlay.appendChild(dialog);

  overlay.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
      cfgObj.onCancel();
      return;
    }
    if (cfgObj.onKey && cfgObj.onKey(ev, dialog) === true) return;
    trapTab(ev, dialog);
  });
  overlay.addEventListener('mousedown', (ev) => {
    if (ev && ev.target === overlay) cfgObj.onCancel();
  });

  const mountTo = d.body || d.documentElement;
  mountTo.appendChild(overlay);
  // Диалог всегда поверх страницы: выдвижное меню (узкое окно) закрываем, иначе
  // оно делит экран с окном и перехватывает клики.
  if (d.getElementById) {
    const app = d.getElementById('app');
    if (app && app.classList && app.classList.contains('nav-open')) app.classList.remove('nav-open');
  }

  if (cfgObj.initialFocus && typeof cfgObj.initialFocus.focus === 'function') {
    cfgObj.initialFocus.focus();
  }

  return {
    dialog,
    body,
    foot,
    overlay,
    close() {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      if (prevFocus && prevFocus.isConnected !== false && typeof prevFocus.focus === 'function') {
        try { prevFocus.focus(); } catch { /* узел уже мог исчезнуть */ }
      }
    },
  };
}

function noDom() {
  return !globalThis.document || !globalThis.document.body;
}

/**
 * Диалог подтверждения. Enter — да, Esc — нет, клик по фону — нет.
 * @param {{title?: string, text?: string, confirmLabel?: string, cancelLabel?: string, danger?: boolean}} [opts]
 * @returns {Promise<boolean>}
 */
export function confirmDialog(opts = {}) {
  useStyles();
  const o = opts || {};
  return new Promise((resolve) => {
    if (noDom()) { resolve(false); return; }
    let done = false;
    let shell = null;
    const finish = (val) => {
      if (done) return;
      done = true;
      if (shell) shell.close();
      resolve(val);
    };

    const cancelBtn = h('button', {
      class: 'btn btn--secondary', type: 'button',
      onclick: () => finish(false),
    }, String(o.cancelLabel || 'Отмена'));

    const confirmBtn = h('button', {
      class: ['btn', o.danger ? 'btn--danger' : 'btn--primary'], type: 'button',
      onclick: () => finish(true),
    }, String(o.confirmLabel || 'Подтвердить'));

    shell = buildDialog({
      title: o.title || 'Подтвердить',
      body: o.text ? String(o.text) : '',
      buttons: [cancelBtn, confirmBtn],
      initialFocus: o.danger ? cancelBtn : confirmBtn,
      onCancel: () => finish(false),
      onKey: (ev) => {
        if (ev.key !== 'Enter' || ev.isComposing) return false;
        const t = ev.target;
        if (t && String(t.tagName || '').toUpperCase() === 'BUTTON') return false;
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        finish(true);
        return true;
      },
    });
  });
}

/**
 * Диалог ввода. Enter — сохранить (в multiline — Ctrl+Enter), Esc — отмена.
 * @param {{title?: string, label?: string, placeholder?: string, multiline?: boolean,
 *          submitLabel?: string, cancelLabel?: string, initial?: string, hint?: string}} [opts]
 * @returns {Promise<string|null>}
 */
export function promptDialog(opts = {}) {
  useStyles();
  const o = opts || {};
  return new Promise((resolve) => {
    if (noDom()) { resolve(null); return; }
    let done = false;
    let shell = null;
    const finish = (val) => {
      if (done) return;
      done = true;
      if (shell) shell.close();
      resolve(val);
    };

    const control = o.multiline
      ? h('textarea', { class: 'textarea', rows: 4, placeholder: o.placeholder || '' })
      : h('input', { class: 'input', type: 'text', placeholder: o.placeholder || '' });
    control.value = o.initial === null || o.initial === undefined ? '' : String(o.initial);

    const body = o.label ? field({ label: o.label, control }) : control;
    if (o.multiline) {
      body.appendChild(h('div', { class: 'dialog__hint' }, 'Ctrl+Enter — сохранить, Esc — отмена'));
    } else if (o.hint) {
      body.appendChild(h('div', { class: 'dialog__hint' }, String(o.hint)));
    }

    const cancelBtn = h('button', {
      class: 'btn btn--secondary', type: 'button',
      onclick: () => finish(null),
    }, String(o.cancelLabel || 'Отмена'));

    const submitBtn = h('button', {
      class: 'btn btn--primary', type: 'button',
      onclick: () => finish(control.value),
    }, String(o.submitLabel || 'Сохранить'));

    shell = buildDialog({
      title: o.title || 'Введите значение',
      body,
      buttons: [cancelBtn, submitBtn],
      initialFocus: control,
      onCancel: () => finish(null),
      onKey: (ev) => {
        if (ev.key !== 'Enter' || ev.isComposing) return false;
        const t = ev.target;
        if (t && String(t.tagName || '').toUpperCase() === 'BUTTON') return false;
        const withMod = ev.ctrlKey || ev.metaKey;
        const ok = o.multiline ? withMod : !withMod && !ev.shiftKey;
        if (!ok) return false;
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        finish(control.value);
        return true;
      },
    });

    try {
      if (typeof control.focus === 'function') control.focus();
      if (typeof control.setSelectionRange === 'function') {
        const len = String(control.value || '').length;
        control.setSelectionRange(len, len);
      } else if (typeof control.select === 'function' && !o.multiline) {
        control.select();
      }
    } catch { /* не критично */ }
  });
}

export default {
  ensureCss,
  formatStatusRu,
  statusChip,
  roleChip,
  avatar,
  profileLabel,
  emptyState,
  skeleton,
  errorBanner,
  mdBlock,
  openPath,
  copyText,
  confirmDialog,
  promptDialog,
  field,
  iconButton,
  pathChip,
  sectionHeader,
  dueBadge,
  segmented,
  lsGet,
  lsSet,
};
