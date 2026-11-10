/**
 * dictation-store.js — где лежит наговоренное, пока оно не стало текстом.
 *
 * Две вещи, обе переживают перезагрузку страницы:
 *   1) **части записи** — аудио в IndexedDB (`teamroom-dictation` → `parts`);
 *   2) **список незаконченных записей** — в localStorage (`teamroom.dictation.pending`),
 *      чтобы после обрыва предложить «Распознать».
 *
 * Почему IndexedDB: у поля диктовки нет своего сервера — если веб-окно закроют,
 * из памяти JS не вернуть ничего. IndexedDB пишет блоб на диск браузера; при
 * обрыве сети или отказе сервера запись остаётся здесь.
 *
 * Где хранилища нет (тесты в node, очень старый webview) — та же логика работает
 * на Map в памяти: поведение одинаковое, отличается только живучесть.
 *
 * Публично: putPart, getPart, listParts, dropPart, dropRecording, clearAll,
 *           listPending, rememberPending, forgetPending, storageKind
 */

const DB_NAME = 'teamroom-dictation';
const DB_VERSION = 1;
const STORE = 'parts';
const PENDING_KEY = 'teamroom.dictation.pending';
const PENDING_TTL_MS = 72 * 60 * 60 * 1000;   // как и на сервере: трое суток
/**
 * Меньше одной части — восстанавливать нечего: такого «хвоста» не заводим и
 * старые выбрасываем. Раньше след писался в момент СТАРТА записи (`parts: 0`),
 * поэтому каждая сорванная попытка (отвалился микрофон) оставляла строку
 * «Есть нерасшифрованная запись», по которой «Распознать» честно не находил
 * ничего — отсюда «не удалось» и «убрал, а оно снова висит».
 */
const MIN_PARTS = 1;

const memory = new Map();          // 'rid/n' → Blob
let dbPromise = null;
let memoryPending = [];
let localBroken = false;           // localStorage отказал — держим список в памяти

/* ───────────────────────────────────────────────── IndexedDB */

function idb() {
  const factory = globalThis.indexedDB;
  return factory && typeof factory.open === 'function' ? factory : null;
}

function openDb() {
  if (dbPromise) return dbPromise;
  const factory = idb();
  if (!factory) return Promise.resolve(null);
  dbPromise = new Promise((resolve) => {
    let settled = false;
    const done = (value) => { if (!settled) { settled = true; resolve(value); } };
    let request = null;
    try { request = factory.open(DB_NAME, DB_VERSION); } catch { done(null); return; }
    if (!request) { done(null); return; }
    request.onupgradeneeded = () => {
      const db = request.result || {};
      try {
        if (db.objectStoreNames && !db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      } catch { /* хранилище уже есть */ }
    };
    request.onsuccess = () => done(request.result || null);
    request.onerror = () => done(null);
    // Некоторые webview не зовут ни onsuccess, ни onerror — не висим навсегда.
    setTimeout(() => done(null), 3000);
  });
  return dbPromise;
}

function tx(db, mode, run) {
  return new Promise((resolve) => {
    let request = null;
    try { request = run(db.transaction(STORE, mode).objectStore(STORE)); } catch { resolve(null); return; }
    if (!request) { resolve(null); return; }
    request.onsuccess = () => resolve(request.result === undefined ? null : request.result);
    request.onerror = () => resolve(null);
  });
}

const keyOf = (rid, n) => String(rid) + '/' + String(n);

/* ───────────────────────────────────────────────── часть записи */

/**
 * Положить часть записи в локальное хранилище.
 * @param {string} rid номер записи
 * @param {number} n номер части
 * @param {Blob} blob аудио
 * @returns {Promise<boolean>} сохранилось ли куда-нибудь
 */
export async function putPart(rid, n, blob) {
  if (!rid || !blob) return false;
  memory.set(keyOf(rid, n), blob);
  const db = await openDb();
  if (!db) return true;
  const saved = await tx(db, 'readwrite', (store) => store.put(blob, keyOf(rid, n)));
  return saved !== null;
}

/** Часть записи из хранилища (Blob) или null. */
export async function getPart(rid, n) {
  const key = keyOf(rid, n);
  const db = await openDb();
  if (db) {
    const found = await tx(db, 'readonly', (store) => store.get(key));
    if (found) return found;
  }
  return memory.get(key) || null;
}

/** Номера частей записи, которые есть локально. */
export async function listParts(rid) {
  const prefix = String(rid) + '/';
  const numbers = new Set();
  for (const key of memory.keys()) {
    if (key.startsWith(prefix)) numbers.add(Number(key.slice(prefix.length)));
  }
  const db = await openDb();
  if (db) {
    const keys = await tx(db, 'readonly', (store) => store.getAllKeys());
    for (const key of Array.isArray(keys) ? keys : []) {
      const text = String(key || '');
      if (text.startsWith(prefix)) numbers.add(Number(text.slice(prefix.length)));
    }
  }
  return Array.from(numbers).filter((n) => Number.isFinite(n)).sort((a, b) => a - b);
}

/** Убрать часть записи. */
export async function dropPart(rid, n) {
  const key = keyOf(rid, n);
  memory.delete(key);
  const db = await openDb();
  if (db) await tx(db, 'readwrite', (store) => store.delete(key));
}

/** Убрать все части записи. */
export async function dropRecording(rid) {
  for (const n of await listParts(rid)) await dropPart(rid, n);
}

/** Очистить всё (тесты и «забыть всё»). */
export async function clearAll() {
  memory.clear();
  memoryPending = [];
  const db = await openDb();
  if (db) await tx(db, 'readwrite', (store) => store.clear());
  try {
    const ls = globalThis.localStorage;
    if (ls && typeof ls.removeItem === 'function') ls.removeItem(PENDING_KEY);
  } catch { /* localStorage недоступен */ }
}

/** Что используется: 'indexeddb' или 'memory' (для журнала и тестов). */
export function storageKind() {
  return idb() ? 'indexeddb' : 'memory';
}

/* ───────────────────────────────── незаконченные записи */

function readLocal() {
  if (localBroken) return null;
  try {
    const ls = globalThis.localStorage;
    if (!ls || typeof ls.getItem !== 'function') return null;
    const raw = ls.getItem(PENDING_KEY);
    if (!raw) return [];
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : [];
  } catch {
    return null;
  }
}

function writeLocal(items) {
  try {
    const ls = globalThis.localStorage;
    if (!ls || typeof ls.setItem !== 'function') return false;
    ls.setItem(PENDING_KEY, JSON.stringify(items));
    localBroken = false;
    return true;
  } catch {
    localBroken = true;
    return false;
  }
}

/**
 * Незаконченные записи: `[{rid, at, seconds, parts}]`, свежие сверху.
 * Просроченные (старше трёх суток) выбрасываются — их уже нет и на сервере.
 * Пустые следы (`parts: 0`) тоже: в них нет ни одной части звука, распознавать
 * нечего — именно они раньше висели вечной строкой «Есть нерасшифрованная запись».
 */
export function listPending() {
  const stored = readLocal();
  const items = stored === null ? memoryPending.slice() : stored;
  const now = Date.now();
  const fresh = items.filter((item) => item && typeof item === 'object'
    && typeof item.rid === 'string' && item.rid
    && Number(item.parts) >= MIN_PARTS
    && now - Number(item.at || 0) < PENDING_TTL_MS);
  fresh.sort((a, b) => Number(b.at || 0) - Number(a.at || 0));
  if (stored === null) memoryPending = fresh;
  else if (fresh.length !== items.length) writeLocal(fresh);
  return fresh;
}

/**
 * Запомнить, что запись началась (или продолжилась): после обрыва по этому
 * следу её можно найти и распознать. Без звука след не заводится.
 */
export function rememberPending(rid, { parts = 0, seconds = 0, at = 0 } = {}) {
  if (!rid) return null;
  const items = listPending().filter((item) => item.rid !== rid);
  const count = Math.max(0, Number(parts) || 0);
  if (count < MIN_PARTS) {
    if (!writeLocal(items)) memoryPending = items;
    return null;
  }
  const item = { rid: String(rid), at: at || Date.now(),
                 parts: count,
                 seconds: Math.max(0, Number(seconds) || 0) };
  items.unshift(item);
  const kept = items.slice(0, 20);
  if (!writeLocal(kept)) memoryPending = kept;
  return item;
}

/** Забыть запись (текст вставлен, запись отменена или убрана человеком). */
export function forgetPending(rid) {
  const rids = (Array.isArray(rid) ? rid : [rid]).map(String).filter(Boolean);
  if (!rids.length) return;
  const items = listPending().filter((item) => !rids.includes(String(item.rid)));
  if (!writeLocal(items)) memoryPending = items;
}

export default {
  putPart, getPart, listParts, dropPart, dropRecording, clearAll, storageKind,
  listPending, rememberPending, forgetPending,
};
