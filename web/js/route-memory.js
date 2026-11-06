/**
 * route-memory.js — помнит последний маршрут и прокрутку (волна 17, B-16).
 *
 * Почему: после полной перезагрузки страницы (пересоздание webview, перезапуск
 * сервера, обновление плагина) хэш терялся, и `app.js` открывал «Запрос»
 * последней комнаты — Михаила «выкидывало на все запросы».
 *
 * Что хранится в `localStorage['teamroom.lastRoute']`:
 *   { at, hash, scroll: {view, aside, feed, sk:<ключ>}, routes: {<hash>: scroll} }
 *   * `hash` — ПОЛНЫЙ последний маршрут (`#/room/fn/request/t_…`);
 *   * `scroll` — позиции основных прокручиваемых контейнеров на этом маршруте;
 *   * `routes` — те же позиции для последних 8 маршрутов (возврат назад
 *     открывает место, а не верх страницы).
 *
 * Границы ответственности:
 *   * модуль не знает про API и ничего не запрашивает — решение «открывать ли
 *     сохранённый маршрут» принимает `app.js` (см. `chooseHash`);
 *   * маршрут-запрос сначала проверяется на существование — устаревший не
 *     восстанавливаем (тогда открывается вкладка комнаты);
 *   * страница сообщает маршрут хозяину (`postMessage {type:'team-room:route',
 *     hash}`), чтобы плагин помнил его при пересоздании webview.
 *
 * Всё, кроме `createRouteMemory`, — чистые функции: их проверяют тесты без браузера.
 */

/** Ключ localStorage. */
export const ROUTE_KEY = 'teamroom.lastRoute';
/** Тип сообщения страница → хозяин (плагин) о смене маршрута. */
export const ROUTE_MESSAGE = 'team-room:route';
/** Запрос хозяина → страница: «повтори маршрут» (необязательный, для встраивания). */
export const ROUTE_REQUEST = 'team-room:route:request';
/** Сколько маршрутов помним (по прокрутке). */
export const MAX_ROUTES = 8;
/** Разумный предел длины хэша: длиннее — мусор, а не маршрут. */
export const MAX_HASH = 512;
/** Сколько ждём, пока страница дорастёт до сохранённой прокрутки, мс. */
export const RESTORE_TIMEOUT_MS = 4000;

const KEY_RE = /^[a-z0-9_:.-]{1,60}$/i;
const SCROLL_LIMIT = 10000000;   // 10 млн px — заведомо больше любой страницы

/* ───────────────────────────────────────────────────────── хэш и маршрут */

/** Пустой хэш (#, #/ или ничего) — «дом», а не маршрут. */
export function isBareHash(hash) {
  const raw = String(hash == null ? '' : hash).trim();
  return !raw || raw === '#' || raw === '#/';
}

/** Привести хэш к виду `#/…`; мусор и чужие схемы → ''. */
export function normalizeHash(hash) {
  const raw = String(hash == null ? '' : hash).trim();
  if (!raw) return '';
  if (raw.length > MAX_HASH) return '';
  const withHash = raw.charAt(0) === '#' ? raw : '#' + raw;
  // Только внутренние маршруты приложения: '#/…', без пробелов, кавычек и угловых скобок.
  if (!/^#\/(?:[^\s<>"']*)$/.test(withHash)) return '';
  return withHash;
}

/** Настоящий маршрут (не «дом» и не мусор)? */
export function isRouteHash(hash) {
  const h = normalizeHash(hash);
  return Boolean(h) && !isBareHash(h);
}

function decode(part) {
  try { return decodeURIComponent(part); } catch { return part; }
}

/**
 * Разобрать наш хэш в вид маршрута (без обращения к router.js — модуль
 * должен работать в тестах без DOM).
 * @param {string} hash
 * @returns {{kind:string, roomId?:string, tab?:string, id?:string|null}|null}
 */
export function parseRouteHash(hash) {
  const h = normalizeHash(hash);
  if (!h) return null;
  const path = h.slice(2).split('?')[0];
  const seg = path.split('/').filter(Boolean).map(decode);
  if (!seg.length) return { kind: 'home' };
  if (seg[0] === 'room' && seg[1]) {
    const tab = seg[2] || 'composer';
    if (tab === 'request' && seg[3]) return { kind: 'request', roomId: seg[1], tab, id: seg[3] };
    return { kind: 'room', roomId: seg[1], tab, id: seg[3] || null };
  }
  if (seg[0] === 'search') return { kind: 'search' };
  if (seg[0] === 'settings') return { kind: 'settings', tab: seg[1] || null };
  return null;
}

/** Хэш вкладки комнаты. */
export function buildRoomHash(roomId, tab = 'composer') {
  const id = String(roomId || '');
  if (!id) return '#/';
  return '#/room/' + encodeURIComponent(id) + '/' + encodeURIComponent(String(tab || 'composer'));
}

/**
 * Какой маршрут открыть при загрузке без хэша (и при негодном хэше).
 *
 * Правила:
 *   1. явный хэш в адресе сильнее памяти (deep link);
 *   2. нет хэша — сохранённый последний маршрут;
 *   3. комнаты нет в списке → маршрут отбрасываем (комната удалена);
 *   4. `requestOk === false` (запроса нет) → вкладка этой комнаты;
 *   5. `requestOk === null` (сервер молчит / перезапускается) → маршрут НЕ
 *      выбрасываем: страница покажет «не удалось открыть» с кнопкой «Повторить»;
 *   6. ничего не подошло → вкладка `defaultRoom` или `#/`.
 *
 * @param {{urlHash?:string, stored?:object|string|null, roomIds?:string[],
 *          tabIds?:string[], requestOk?:boolean|null, defaultRoom?:string,
 *          defaultTab?:string}} opts
 * @returns {string}
 */
export function chooseHash(opts = {}) {
  const o = opts || {};
  const urlHash = o.urlHash;
  const stored = parseStored(o.stored);
  const rooms = new Set((Array.isArray(o.roomIds) ? o.roomIds : []).map(String));
  const tabs = new Set((Array.isArray(o.tabIds) ? o.tabIds : []).map(String));
  const defaultTab = String(o.defaultTab || 'composer');
  const requestOk = o.requestOk === true ? true : o.requestOk === false ? false : null;

  // Явный «дом» в адресе (`#/`) — человек был на списке комнат и обновил
  // страницу: остаёмся там. Память работает только когда хэша нет вовсе.
  const rawHash = String(urlHash == null ? '' : urlHash).trim();
  if (rawHash === '#/') return '#/';
  const explicit = isBareHash(urlHash) ? '' : normalizeHash(urlHash);
  const candidates = explicit ? [explicit] : (stored.hash ? [stored.hash] : []);

  for (const hash of candidates) {
    const route = parseRouteHash(hash);
    if (!route) continue;
    if (route.kind === 'home') continue;
    if (route.kind === 'search' || route.kind === 'settings') return hash;
    if (!rooms.size) return hash;                    // список комнат ещё не пришёл — доверяем
    if (!rooms.has(String(route.roomId))) continue;  // комнаты больше нет
    if (route.kind === 'request' && requestOk === false) {
      return buildRoomHash(route.roomId, defaultTab);
    }
    if (route.kind === 'room' && route.tab && tabs.size && !tabs.has(String(route.tab))) {
      return buildRoomHash(route.roomId, defaultTab);
    }
    return hash;
  }

  const room = String(o.defaultRoom || '') || (rooms.size ? [...rooms][0] : '');
  if (!room) return '#/';
  if (rooms.size && !rooms.has(room)) return '#/';
  return buildRoomHash(room, defaultTab);
}

/* ──────────────────────────────────────────────────── запись в localStorage */

function cleanScroll(raw, limit = MAX_ROUTES * 4) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  let n = 0;
  for (const key of Object.keys(raw)) {
    if (n >= limit) break;
    if (!KEY_RE.test(key)) continue;
    const value = Math.round(Number(raw[key]));
    if (!Number.isFinite(value) || value <= 0 || value > SCROLL_LIMIT) continue;
    out[key] = value;
    n += 1;
  }
  return out;
}

function cleanRoutes(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  const keys = Object.keys(raw).filter((h) => isRouteHash(h)).slice(-MAX_ROUTES);
  for (const hash of keys) {
    const scroll = cleanScroll(raw[hash]);
    if (Object.keys(scroll).length) out[normalizeHash(hash)] = scroll;
  }
  return out;
}

/**
 * Терпимо разобрать сохранённое (строка JSON или объект).
 * @param {string|object|null} raw
 * @returns {{hash:string, scroll:object, routes:object, at:number}}
 */
export function parseStored(raw) {
  const empty = { hash: '', scroll: {}, routes: {}, at: 0 };
  if (raw === null || raw === undefined || raw === '') return empty;
  let data = raw;
  if (typeof raw === 'string') {
    try { data = JSON.parse(raw); } catch { return empty; }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return empty;
  const hash = isBareHash(data.hash) ? '' : (normalizeHash(data.hash) || '');
  return {
    hash,
    scroll: cleanScroll(data.scroll),
    routes: cleanRoutes(data.routes),
    at: Number(data.at) > 0 ? Math.round(Number(data.at)) : 0,
  };
}

/** Собрать запись для localStorage (пустую — не сохраняем). */
export function serializeRecord(record) {
  const safe = parseStored(record);
  return JSON.stringify({
    at: safe.at || Date.now(),
    hash: safe.hash,
    scroll: safe.scroll,
    routes: safe.routes,
  });
}

/* ──────────────────────────────────────────────────────────── прокрутка */

/** Ключ контейнера: `view` | `aside` | `feed` | `sk:<data-scroll-key>`. */
export function scrollKeyOf(el) {
  if (!el || !el.getAttribute) return '';
  const sk = String(el.getAttribute('data-scroll-key') || '').trim();
  if (sk) return 'sk:' + sk.slice(0, 50);
  const cls = String((el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '');
  const id = String(el.id || '');
  if (id === 'view') return 'view';
  if (/\breq__aside\b/.test(cls)) return 'aside';
  if (/\bthr__feed\b/.test(cls)) return 'feed';
  return '';
}

/** Все прокручиваемые контейнеры страницы (те, что двигают). */
function scrollContainers(doc) {
  if (!doc || typeof doc.querySelector !== 'function') return [];
  const out = [];
  const push = (el) => { if (el && out.indexOf(el) < 0) out.push(el); };
  const one = (sel) => { try { return doc.querySelector(sel); } catch { return null; } };
  push(one('#view'));
  push(one('.req__aside'));
  push(one('.thr__feed'));
  let many = [];
  try { many = doc.querySelectorAll('[data-scroll-key]') || []; } catch { many = []; }
  for (const el of many) push(el);
  return out;
}

/**
 * Снять позиции прокрутки. Нули не пишем: «вверху» восстанавливать нечего.
 * @param {Document} doc
 * @returns {object}
 */
export function captureScroll(doc) {
  const out = {};
  for (const el of scrollContainers(doc)) {
    const key = scrollKeyOf(el);
    if (!key || out[key]) continue;
    const top = Math.round(Number(el.scrollTop) || 0);
    if (top > 0) out[key] = Math.min(top, SCROLL_LIMIT);
  }
  return out;
}

/** Найти контейнер по ключу. */
export function elementForKey(doc, key) {
  if (!doc || typeof doc.querySelector !== 'function') return null;
  const one = (sel) => { try { return doc.querySelector(sel); } catch { return null; } };
  if (key === 'view') return one('#view');
  if (key === 'aside') return one('.req__aside');
  if (key === 'feed') return one('.thr__feed');
  if (key.indexOf('sk:') === 0) {
    const want = key.slice(3);
    let nodes = [];
    try { nodes = doc.querySelectorAll('[data-scroll-key]') || []; } catch { nodes = []; }
    for (const el of nodes) {
      if (String(el.getAttribute('data-scroll-key') || '').trim() === want) return el;
    }
  }
  return null;
}

/**
 * Поставить прокрутку. `done:false` — страница ещё не доросла (нужна ещё попытка).
 * @param {Document} doc
 * @param {object} map
 * @returns {{done:boolean, applied:string[], waiting:string[]}}
 */
export function applyScroll(doc, map) {
  const result = { done: true, applied: [], waiting: [] };
  const saved = map && typeof map === 'object' ? map : {};
  for (const key of Object.keys(saved)) {
    const want = Math.round(Number(saved[key]));
    if (!Number.isFinite(want) || want <= 0) continue;
    const el = elementForKey(doc, key);
    if (!el) { result.done = false; result.waiting.push(key); continue; }
    const height = Number(el.scrollHeight) || 0;
    const client = Number(el.clientHeight) || 0;
    const max = Math.max(0, height - client);
    const target = Math.min(want, max);
    if (Math.abs((Number(el.scrollTop) || 0) - target) > 2) el.scrollTop = target;
    if (Math.abs((Number(el.scrollTop) || 0) - target) > 4) {
      result.done = false;
      result.waiting.push(key);
    } else if (max + 8 < want) {
      // Дошли до низа, но содержимое ещё грузится — попробуем ещё раз.
      result.done = false;
      result.waiting.push(key);
    } else {
      result.applied.push(key);
    }
  }
  return result;
}

/* ───────────────────────────────────────────────────── сообщение хозяину */

/** Тело сообщения страница → плагин. */
export function routeMessage(hash) {
  const h = isBareHash(hash) ? '' : normalizeHash(hash);
  return { type: ROUTE_MESSAGE, version: 1, hash: h || '' };
}

/* ───────────────────────────────────────────────────────────── контроллер */

function safeStorage(win) {
  try {
    const store = win && win.localStorage;
    if (!store) return null;
    const probe = ROUTE_KEY + '.probe';
    store.setItem(probe, '1');
    store.removeItem(probe);
    return store;
  } catch {
    return null;   // приватный режим / запрет хранилища — работаем без памяти
  }
}

/**
 * Живая память маршрута: чтение/запись, сохранение прокрутки, восстановление,
 * сообщение хозяину.
 * @param {{doc?:Document, win?:Window, storage?:Storage, now?:Function}} [env]
 */
export function createRouteMemory(env = {}) {
  const win = env.win || (typeof window !== 'undefined' ? window : null);
  const doc = env.doc || (typeof document !== 'undefined' ? document : null);
  const storage = env.storage !== undefined ? env.storage : safeStorage(win);
  const now = typeof env.now === 'function' ? env.now : () => Date.now();

  let record = read();
  let currentHash = normalizeHash(win && win.location ? win.location.hash : '');
  let saveTimer = null;
  let restoreTimer = null;
  let cancelRestore = () => {};
  const listeners = [];

  function read() {
    if (!storage) return parseStored(null);
    try { return parseStored(storage.getItem(ROUTE_KEY)); } catch { return parseStored(null); }
  }

  function write(next) {
    record = next;
    if (!storage) return record;
    try { storage.setItem(ROUTE_KEY, serializeRecord(next)); } catch { /* места нет — не беда */ }
    return record;
  }

  function scrollMapFor(hash) {
    const h = normalizeHash(hash);
    if (!h) return {};
    const fresh = read();
    if (fresh.hash === h && Object.keys(fresh.scroll).length) return fresh.scroll;
    return fresh.routes[h] || {};
  }

  /** Запомнить маршрут (и сразу рассказать хозяину). */
  function remember(hash) {
    const h = isBareHash(hash) ? '' : normalizeHash(hash);
    if (!h && !isBareHash(hash)) return record.hash;
    const fresh = read();
    const next = { hash: h, scroll: fresh.hash === h ? fresh.scroll : (fresh.routes[h] || {}),
      routes: fresh.routes, at: now() };
    write(next);
    publish(h);
    return h;
  }

  /** Сохранить прокрутку текущего маршрута. */
  function saveScroll(hash) {
    const h = normalizeHash(hash === undefined ? currentHash : hash);
    if (!h) return record;
    const scroll = captureScroll(doc);
    const fresh = read();
    const routes = Object.assign({}, fresh.routes);
    delete routes[h];
    if (Object.keys(scroll).length) routes[h] = scroll;
    const keys = Object.keys(routes).slice(-MAX_ROUTES);
    const trimmed = {};
    for (const key of keys) trimmed[key] = routes[key];
    if (fresh.hash === h) {
      return write({ hash: h, scroll, routes: trimmed, at: now() });
    }
    return write({ hash: fresh.hash, scroll: fresh.scroll, routes: trimmed, at: now() });
  }

  /** Сообщить хозяину (плагину): «я на таком-то маршруте». */
  function publish(hash) {
    const message = routeMessage(hash === undefined ? currentHash : hash);
    try {
      const parent = win && win.parent;
      if (parent && parent !== win && typeof parent.postMessage === 'function') {
        parent.postMessage(message, '*');
      }
    } catch { /* окно хозяина уже закрыто */ }
    return message;
  }

  /** Восстановить прокрутку маршрута (ждём, пока данные доедут). */
  function restore(hash, options) {
    const opts = options || {};
    const h = normalizeHash(hash === undefined ? currentHash : hash);
    cancelRestore();
    if (!h || !doc) return false;
    const map = scrollMapFor(h);
    if (!Object.keys(map).length) return false;

    const deadline = now() + (Number(opts.timeout) || RESTORE_TIMEOUT_MS);
    const step = Math.max(30, Number(opts.interval) || 90);
    let settled = 0;
    let stopped = false;
    const userEvents = ['wheel', 'touchstart', 'pointerdown', 'keydown'];

    const onUser = () => { stopped = true; cleanup(); };
    const addUser = () => {
      for (const name of userEvents) {
        try { win && win.addEventListener && win.addEventListener(name, onUser, true); } catch { /* нет окна */ }
      }
    };
    const cleanup = () => {
      stopped = true;
      clearTimeout(restoreTimer);
      restoreTimer = null;
      for (const name of userEvents) {
        try { win && win.removeEventListener && win.removeEventListener(name, onUser, true); } catch { /* нет окна */ }
      }
    };
    cancelRestore = cleanup;
    addUser();

    const tick = () => {
      if (stopped) return;
      const result = applyScroll(doc, map);
      settled = result.done ? settled + 1 : 0;
      // Два подряд успешных прохода: роутер и вьюха уже не сдвинут прокрутку.
      if (settled >= 2) { cleanup(); if (typeof opts.onDone === 'function') opts.onDone(result); return; }
      if (now() > deadline) { cleanup(); if (typeof opts.onDone === 'function') opts.onDone(result); return; }
      restoreTimer = setTimeout(tick, step);
    };
    tick();
    return true;
  }

  /** Подписки: смена маршрута, прокрутка, уход со страницы. */
  function connect() {
    if (!win) return { disconnect() {} };
    const onHash = () => {
      saveScroll(currentHash);           // старое место — под старым хэшем
      currentHash = isBareHash(win.location && win.location.hash) ? '' : normalizeHash(win.location.hash);
      remember(currentHash);
      restore(currentHash);
    };
    const onScroll = () => {
      if (saveTimer) return;
      saveTimer = setTimeout(() => {
        saveTimer = null;
        saveScroll(currentHash);
      }, 180);
    };
    const flush = () => saveScroll(currentHash);
    const onHide = () => { if (doc && doc.visibilityState === 'hidden') flush(); };
    const onMessage = (event) => {
      const data = event && event.data;
      if (data && data.type === ROUTE_REQUEST) publish(currentHash);
    };

    const bind = (target, name, fn, opts) => {
      try { target && target.addEventListener && target.addEventListener(name, fn, opts); } catch { /* нет цели */ }
      listeners.push([target, name, fn, opts]);
    };
    bind(win, 'hashchange', onHash);
    bind(doc, 'scroll', onScroll, { capture: true, passive: true });
    bind(win, 'pagehide', flush);
    bind(win, 'beforeunload', flush);
    bind(doc, 'visibilitychange', onHide);
    bind(win, 'message', onMessage);

    // Первое сообщение хозяину — сразу, не дожидаясь смены маршрута.
    publish(currentHash);
    return {
      disconnect() {
        for (const [target, name, fn, opts] of listeners.splice(0)) {
          try { target && target.removeEventListener && target.removeEventListener(name, fn, opts); } catch { /* уже снят */ }
        }
        clearTimeout(saveTimer);
        saveTimer = null;
        cancelRestore();
      },
    };
  }

  return {
    read, write, remember, saveScroll, publish, restore, connect,
    get record() { return record; },
    get hash() { return currentHash; },
    set hash(value) { currentHash = normalizeHash(value); },
  };
}

export default createRouteMemory;
