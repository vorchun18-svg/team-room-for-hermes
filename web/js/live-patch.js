/**
 * live-patch.js — точечное обновление страницы без потери состояния.
 *
 * Задача (WAVE17, B2/P4): перерисовка страницы запроса больше не должна сбрасывать
 * прокрутку правой колонки (`.req__aside`), ленты диалога (`.thr__feed`), любого
 * контейнера с `data-scroll-key`, фокус, выделение, раскрытые блоки и черновики.
 *
 * Как работает:
 *  1. `captureSnapshot()` запоминает состояние: прокрутку всех прокручиваемых
 *     контейнеров (и `#view`), фокус и выделение, значения полей `[data-keep]`,
 *     раскрытость `<details>`.
 *  2. `applyLivePatch()` сверяет построенное дерево с тем, что на экране:
 *     одинаковые поддеревья не трогает вовсе, различающиеся спускается глубже,
 *     а заменяет только тот элемент, который действительно изменился. Элементы
 *     с `data-key`/`data-id` сопоставляются по ключу (шаги, вопросы, решения,
 *     сообщения ленты), остальные — по позиции и имени тега.
 *  3. `restoreSnapshot()` возвращает прокрутку, фокус, выделение, черновики и
 *     раскрытые блоки — даже если часть дерева всё-таки заменена.
 *
 * Правило безопасности: узел переиспользуется ТОЛЬКО если он полностью совпал
 * (хеш поддерева). Иначе он заменяется целиком — так обработчики кликов никогда
 * не остаются от старой карточки (например, с чужим id вопроса).
 *
 * Модуль не зависит от других файлов приложения: только DOM, который ему дали.
 */

/* ─────────────────────────────────────────────────────────── вспомогательное */

const ELEMENT = 1;
const TEXT = 3;

/**
 * Подпись данных «изменилось ли что-то на экране».
 *
 * Heartbeat (события «Признак жизни» и поле `heartbeats`) меняется каждую минуту,
 * но на экране его нет — иначе страница перерисовывалась бы впустую. Новый сервер
 * уже не отдаёт их в `timeline`; старый — отфильтровываем здесь.
 * @param {object} data  ответ `GET /api/requests/{id}`
 * @returns {string}
 */
export function stateSignature(data) {
  if (!data || typeof data !== 'object') return JSON.stringify(data);
  const copy = Object.assign({}, data);
  delete copy.heartbeats;
  delete copy.heartbeat;
  for (const field of ['timeline', 'events']) {
    if (Array.isArray(copy[field])) {
      copy[field] = copy[field].filter((ev) => !ev || String(ev.kind || ev.type || '') !== 'heartbeat');
    }
  }
  return JSON.stringify(copy);
}

/** Теги, внутрь которых можно «спускаться» при точечной правке. */
const CONTAINER_TAGS = new Set([
  'div', 'section', 'aside', 'main', 'nav', 'header', 'footer', 'article',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'details', 'summary', 'figure', 'figcaption',
  'p', 'span', 'strong', 'em', 'small', 'code', 'pre', 'blockquote',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'form', 'label',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'time', 'mark', 'abbr',
]);

/** Поля ввода, в которых набор текста нельзя прерывать перерисовкой. */
const TEXT_INPUT_TYPES = new Set([
  'text', 'search', 'email', 'url', 'tel', 'password', 'number',
  'date', 'datetime-local', 'month', 'time', 'week',
]);

const SCROLL_CLASSES = ['req__aside', 'thr__feed'];

function hashStep(h, str) {
  const s = String(str == null ? '' : str);
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

/** Все элементы дерева (без рекурсии в глубину стека — страницы бывают большие). */
export function* walkElements(root) {
  if (!root) return;
  const stack = [root];
  while (stack.length) {
    const node = stack.pop();
    if (!node) continue;
    if (node.nodeType === ELEMENT) {
      yield node;
      const kids = node.childNodes || [];
      for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
    }
  }
}

/** Клаю элемента в виде массива классов. */
function classesOf(el) {
  return String((el && (el.className || '')) || '').split(/\s+/).filter(Boolean);
}

function hasClass(el, name) {
  return classesOf(el).includes(name);
}

/** Ключ элемента для сопоставления: `data-key`, затем `data-id`. */
export function keyOf(node) {
  if (!node || node.nodeType !== ELEMENT) return '';
  const ds = node.dataset || {};
  const raw = ds.key || ds.id || '';
  if (!raw) return '';
  return String(node.localName) + ':' + String(raw);
}

/** Узел внутри контейнера? (в тестовом DOM contains нет) */
export function containsNode(host, node) {
  if (!host || !node) return false;
  if (typeof host.contains === 'function') return host.contains(node);
  let cur = node;
  while (cur) {
    if (cur === host) return true;
    cur = cur.parentNode;
  }
  return false;
}

/** Хеш поддерева: одинаковый хеш ⇒ узлы можно считать одинаковыми. */
export function hashTree(node, memo) {
  if (!node) return '0';
  if (memo && memo.has(node)) return memo.get(node);
  let h = 2166136261;
  if (node.nodeType === TEXT) {
    h = hashStep(h, '#text');
    h = hashStep(h, node.data);
  } else if (node.nodeType === ELEMENT) {
    h = hashStep(h, '<' + String(node.localName));
    const attrs = typeof node.attributes !== 'undefined' ? Array.from(node.attributes) : [];
    for (const attr of attrs) {
      h = hashStep(h, attr.name + '=' + String(attr.value) + '\u0000');
    }
    const kids = node.childNodes || [];
    for (let i = 0; i < kids.length; i++) h = hashStep(h, hashTree(kids[i], memo));
  } else {
    h = hashStep(h, '#' + String(node.nodeType));
  }
  const out = String(h >>> 0);
  if (memo) memo.set(node, out);
  return out;
}

/** Путь до узла: индексы в childNodes от контейнера. */
export function pathOf(host, node) {
  const path = [];
  let cur = node;
  while (cur && cur !== host) {
    const parent = cur.parentNode;
    if (!parent) return null;
    const kids = parent.childNodes || [];
    const idx = Array.prototype.indexOf.call(kids, cur);
    if (idx < 0) return null;
    path.unshift(idx);
    cur = parent;
  }
  return cur === host ? path : null;
}

function byPath(host, path) {
  if (!host || !Array.isArray(path)) return null;
  let cur = host;
  for (const idx of path) {
    const kids = cur && cur.childNodes ? cur.childNodes : null;
    if (!kids || !kids[idx]) return null;
    cur = kids[idx];
  }
  return cur === host ? null : cur;
}

/** Цепочка ключей `data-key`/`data-id` от контейнера до узла. */
function keyPathOf(host, node) {
  const out = [];
  let cur = node;
  while (cur && cur !== host) {
    const k = keyOf(cur);
    if (k) out.unshift(k);
    cur = cur.parentNode;
  }
  return out.length ? out : null;
}

function byKeyPath(host, keyPath) {
  if (!Array.isArray(keyPath) || !keyPath.length) return null;
  let scope = host;
  let found = null;
  for (const key of keyPath) {
    found = null;
    for (const el of walkElements(scope)) {
      if (el !== scope && keyOf(el) === key) { found = el; break; }
    }
    if (!found) return null;
    scope = found;
  }
  return found;
}

/* ─────────────────────────────────────────────────────────────── снимок */

/**
 * Снять состояние страницы до перерисовки.
 * @param {Element} host  контейнер страницы (`.req`)
 * @param {Document} [doc]
 * @returns {{scrolls:Array, drafts:Array, details:Array, focus:object|null}}
 */
export function captureSnapshot(host, doc) {
  const d = doc || (typeof document !== 'undefined' ? document : null);
  const snap = { scrolls: [], drafts: [], details: [], focus: null };
  if (!host) return snap;

  // 1. Внешняя прокрутка: #view живёт вне страницы запроса.
  const view = d && typeof d.getElementById === 'function' ? d.getElementById('view') : null;
  if (view) {
    snap.scrolls.push({
      view: true,
      top: Number(view.scrollTop) || 0,
      left: Number(view.scrollLeft) || 0,
    });
  }

  // 2. Все прокручиваемые контейнеры страницы: с ключом, по классу или фактически сдвинутые.
  for (const el of walkElements(host)) {
    const key = el.dataset && el.dataset.scrollKey ? String(el.dataset.scrollKey) : '';
    const named = SCROLL_CLASSES.some((c) => hasClass(el, c));
    const top = Number(el.scrollTop) || 0;
    const left = Number(el.scrollLeft) || 0;
    if (!key && !named && !top && !left) continue;
    snap.scrolls.push({
      key,
      cls: classesOf(el).join(' '),
      tag: String(el.localName),
      path: pathOf(host, el),
      keyPath: keyPathOf(host, el),
      top,
      left,
    });
  }

  // 3. Черновики: значения полей с data-keep (пользователь мог печатать).
  for (const el of walkElements(host)) {
    const keep = el.dataset && el.dataset.keep ? String(el.dataset.keep) : '';
    if (!keep) continue;
    snap.drafts.push({
      keep,
      value: typeof el.value === 'string' ? el.value : undefined,
      checked: typeof el.checked === 'boolean' ? el.checked : undefined,
      path: pathOf(host, el),
    });
  }

  // 4. Раскрытые/свёрнутые блоки.
  for (const el of walkElements(host)) {
    if (String(el.localName) !== 'details') continue;
    snap.details.push({
      open: Boolean(el.open),
      path: pathOf(host, el),
      keyPath: keyPathOf(host, el),
    });
  }

  // 5. Фокус и выделение.
  const active = d && d.activeElement;
  if (active && active !== d.body && active !== d.documentElement
      && (containsNode(host, active) || active === view)) {
    const f = {
      keep: active.dataset && active.dataset.keep ? String(active.dataset.keep) : '',
      key: keyOf(active),
      tag: String(active.localName || ''),
      path: pathOf(host, active),
      keyPath: keyPathOf(host, active),
      view: active === view,
      start: null,
      end: null,
      value: typeof active.value === 'string' ? active.value : undefined,
    };
    try {
      if (typeof active.selectionStart === 'number') {
        f.start = active.selectionStart;
        f.end = typeof active.selectionEnd === 'number' ? active.selectionEnd : active.selectionStart;
      }
    } catch { /* у input[type=number] выделения нет */ }
    snap.focus = f;
  }
  return snap;
}

function resolveTarget(host, item) {
  if (!host || !item) return null;
  if (item.key) {
    for (const el of walkElements(host)) {
      if (el.dataset && el.dataset.scrollKey === item.key) return el;
    }
  }
  if (item.keyPath) {
    const found = byKeyPath(host, item.keyPath);
    if (found) return found;
  }
  if (item.path) {
    const found = byPath(host, item.path);
    if (found) return found;
  }
  if (item.cls) {
    const wanted = item.cls.split(/\s+/).filter(Boolean).sort().join(' ');
    for (const el of walkElements(host)) {
      if (classesOf(el).sort().join(' ') === wanted && String(el.localName) === item.tag) return el;
    }
  }
  return null;
}

/**
 * Вернуть состояние, снятое `captureSnapshot()`.
 * @param {Element} host
 * @param {object} snap
 * @param {Document} [doc]
 * @param {{draftKeys?: {has:Function}}} [opts]  какие черновики восстанавливать
 *   (страница передаёт карту `data-keep`, чтобы очищенное после отправки поле
 *   не заполнилось старым текстом)
 */
export function restoreSnapshot(host, snap, doc, opts = {}) {
  if (!host || !snap) return;
  const d = doc || (typeof document !== 'undefined' ? document : null);
  const allowed = opts.draftKeys && typeof opts.draftKeys.has === 'function' ? opts.draftKeys : null;

  // Черновики — до прокрутки: правка значения может изменить высоту.
  for (const draft of snap.drafts || []) {
    if (allowed && !allowed.has(draft.keep)) continue;
    let el = null;
    for (const node of walkElements(host)) {
      if (node.dataset && node.dataset.keep === draft.keep) { el = node; break; }
    }
    if (!el && draft.path) el = byPath(host, draft.path);
    if (!el) continue;
    if (draft.value !== undefined && typeof el.value === 'string' && el.value !== draft.value) {
      el.value = draft.value;
    }
    if (draft.checked !== undefined && typeof el.checked === 'boolean' && el.checked !== draft.checked) {
      el.checked = draft.checked;
    }
  }

  // Раскрытые блоки: пользователь мог открыть/закрыть их сам.
  for (const item of snap.details || []) {
    const el = resolveTarget(host, item) || (item.path ? byPath(host, item.path) : null);
    if (!el) continue;
    if (Boolean(el.open) !== Boolean(item.open)) el.open = Boolean(item.open);
  }

  // Прокрутка.
  const view = d && typeof d.getElementById === 'function' ? d.getElementById('view') : null;
  for (const item of snap.scrolls || []) {
    const el = item.view ? view : resolveTarget(host, item);
    if (!el) continue;
    // Браузерное «привязывание прокрутки» (scroll anchoring) сдвигает scrollTop,
    // когда над видимой частью меняется содержимое, — то есть сразу после нашей
    // же правки. Для этих панелей важнее сохранить позицию, поэтому отключаем.
    try { if (el.style && 'overflowAnchor' in el.style) el.style.overflowAnchor = 'none'; } catch { /* ignore */ }
    const top = Number(item.top) || 0;
    const left = Number(item.left) || 0;
    if (top && Math.abs((Number(el.scrollTop) || 0) - top) > 0.5) el.scrollTop = top;
    if (left && Math.abs((Number(el.scrollLeft) || 0) - left) > 0.5) el.scrollLeft = left;
  }

  // Фокус: только если он потерялся из-за перерисовки.
  const f = snap.focus;
  if (!f) return;
  let target = null;
  if (f.keep) {
    for (const node of walkElements(host)) {
      if (node.dataset && node.dataset.keep === f.keep) { target = node; break; }
    }
  }
  if (!target && f.keyPath) target = byKeyPath(host, f.keyPath);
  if (!target && f.path) target = byPath(host, f.path);
  if (!target && f.view) target = view;
  if (!target) return;
  const active = d && d.activeElement;
  if (active !== target && typeof target.focus === 'function') {
    try { target.focus({ preventScroll: true }); } catch { /* ignore */ }
  }
  if (f.start != null && typeof target.setSelectionRange === 'function') {
    try { target.setSelectionRange(f.start, f.end == null ? f.start : f.end); } catch { /* ignore */ }
  }
}

/* ───────────────────────────────────────────────────────────── сверка */

function patchAttributes(oldEl, newEl) {
  const want = newEl.attributes ? Array.from(newEl.attributes) : [];
  const have = oldEl.attributes ? Array.from(oldEl.attributes) : [];
  const wanted = new Set(want.map((a) => a.name));
  for (const attr of have) {
    if (!wanted.has(attr.name)) oldEl.removeAttribute(attr.name);
  }
  for (const attr of want) {
    if (oldEl.getAttribute(attr.name) !== attr.value) oldEl.setAttribute(attr.name, attr.value);
  }
}

/**
 * Сверить детей двух контейнеров: одинаковые оставить, различающиеся — обновить.
 * @returns {number} сколько узлов пришлось заменить
 */
export function reconcile(oldParent, newParent, stats, memo) {
  const s = stats || { reused: 0, patched: 0, replaced: 0 };
  const m = memo || new Map();
  const oldKids = Array.from(oldParent.childNodes || []);
  const newKids = Array.from(newParent.childNodes || []);

  const keyed = new Map();
  const plain = [];
  for (const node of oldKids) {
    const k = keyOf(node);
    if (k && !keyed.has(k)) keyed.set(k, node);
    else plain.push(node);
  }

  const takePlain = (like) => {
    for (let i = 0; i < plain.length; i++) {
      const cand = plain[i];
      if (cand.nodeType !== like.nodeType) continue;
      if (like.nodeType === ELEMENT && String(cand.localName) !== String(like.localName)) continue;
      plain.splice(i, 1);
      return cand;
    }
    return null;
  };

  const out = [];
  for (const newNode of newKids) {
    const k = keyOf(newNode);
    let match = k && keyed.has(k) ? keyed.get(k) : null;
    if (match) keyed.delete(k);
    else if (!k) match = takePlain(newNode);
    if (match) {
      out.push(patchNode(match, newNode, s, m));
    } else {
      s.replaced += 1;
      out.push(newNode);
    }
  }

  // Порядок и удаление лишнего: старые узлы не пересоздаём, только переставляем.
  for (let i = 0; i < out.length; i++) {
    const want = out[i];
    const cur = oldParent.childNodes[i];
    if (cur !== want) oldParent.insertBefore(want, cur || null);
  }
  while ((oldParent.childNodes || []).length > out.length) {
    oldParent.removeChild(oldParent.childNodes[oldParent.childNodes.length - 1]);
  }
  return s.replaced || 0;
}

function patchNode(oldNode, newNode, stats, memo) {
  if (oldNode === newNode) return oldNode;
  if (oldNode.nodeType !== newNode.nodeType) {
    stats.replaced += 1;
    return newNode;
  }
  if (oldNode.nodeType === TEXT) {
    if (oldNode.data !== newNode.data) {
      oldNode.data = newNode.data;
      stats.patched += 1;
    } else {
      stats.reused += 1;
    }
    return oldNode;
  }
  if (oldNode.nodeType !== ELEMENT) return newNode;
  if (hashTree(oldNode, memo) === hashTree(newNode, memo)) {
    stats.reused += 1;
    return oldNode;
  }
  if (String(oldNode.localName) !== String(newNode.localName)) {
    stats.replaced += 1;
    return newNode;
  }
  if (oldNode.namespaceURI && newNode.namespaceURI && oldNode.namespaceURI !== newNode.namespaceURI) {
    stats.replaced += 1;
    return newNode;
  }
  if (!CONTAINER_TAGS.has(String(newNode.localName))) {
    stats.replaced += 1;
    return newNode;
  }
  // Ключ тот же ⇒ это тот же объект (шаг, вопрос, сообщение): обработчики
  // кликов на нём по-прежнему верные, поэтому спускаемся внутрь и меняем
  // только изменившихся детей. Разные ключи — разные объекты: заменяем.
  const key = keyOf(newNode);
  if (key && keyOf(oldNode) !== key) {
    stats.replaced += 1;
    return newNode;
  }
  patchAttributes(oldNode, newNode);
  reconcile(oldNode, newNode, stats, memo);
  stats.patched += 1;
  return oldNode;
}

/**
 * Применить новое дерево к тому, что на экране, сохранив состояние.
 *
 * @param {Element} host  живой контейнер (например `.req`)
 * @param {Element} next  только что построенная страница (не в документе)
 * @param {{doc?: Document, snapshot?: object, restore?: boolean}} [opts]
 * @returns {{reused:number, patched:number, replaced:number, snapshot:object}}
 */
export function applyLivePatch(host, next, opts = {}) {
  const doc = opts.doc || (typeof document !== 'undefined' ? document : null);
  const stats = { reused: 0, patched: 0, replaced: 0 };
  if (!host || !next) return Object.assign(stats, { snapshot: null });
  const snap = opts.snapshot || captureSnapshot(host, doc);
  const memo = new Map();

  patchAttributes(host, next);
  reconcile(host, next, stats, memo);
  if (opts.restore !== false) restoreSnapshot(host, snap, doc, opts);
  stats.snapshot = snap;
  return stats;
}

/* ───────────────────────────────────────────────────────── ввод и диктовка */

/**
 * Пользователь сейчас печатает или диктует внутри `host`?
 * Пока это так, страницу обновлять нельзя — обновление откладывается.
 * @param {Element} host
 * @param {Document} [doc]
 */
export function isEditing(host, doc) {
  const d = doc || (typeof document !== 'undefined' ? document : null);
  if (!host || !d) return false;
  const active = d.activeElement;
  if (active && active !== d.body && active !== d.documentElement && containsNode(host, active)) {
    const tag = String(active.localName || '');
    if (active.isContentEditable) return true;
    if (tag === 'textarea') return true;
    if (tag === 'select') return true;
    if (tag === 'input') {
      const type = String(active.type || 'text').toLowerCase();
      if (TEXT_INPUT_TYPES.has(type)) return true;
    }
  }
  // Идёт диктовка: кнопка микрофона в состоянии записи/подготовки.
  for (const el of walkElements(host)) {
    const ds = el.dataset || {};
    if (ds.micBtn === 'true' && (ds.state === 'recording' || ds.state === 'arming')) return true;
  }
  return false;
}

export default {
  applyLivePatch, captureSnapshot, restoreSnapshot, isEditing, reconcile,
  hashTree, keyOf, stateSignature,
};
