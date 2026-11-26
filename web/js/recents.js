/**
 * recents.js — «Недавние» (10 запросов и дел) и «Закладки» (★) в меню (W29-Q7).
 *
 * Два списка, один смысл: вернуться туда, где вы только что были, и держать
 * под рукой то, к чему возвращаетесь всегда.
 *
 *   * «Недавние» — последние 10 открытых запросов и дел. Список локальный:
 *     человек открывает страницу — значит, она ему нужна; сервер об этом
 *     ничего не знает и не должен.
 *   * «Закладки» — запросы с ★. Звезда — это штатное закрепление запроса в
 *     реестре комнаты (`PATCH /api/requests/{id} {pinned}`), поэтому источник
 *     правды один: сервер. Локально хранится только список id, чтобы закладка
 *     оставалась в меню, даже если сводка комнаты её не прислала.
 *
 * Проверки: tests/recents.test.mjs (node --test).
 */

export const RECENT_KEY = 'teamroom.recent';
export const BOOKMARK_KEY = 'teamroom.bookmarks';
/** «Недавнее» — ровно 10 пунктов: больше не читается с одного взгляда. */
export const RECENT_MAX = 10;

/* ───────────────────────────────────────────────────────────── утилиты */

function safeStorage(store) {
  if (store && typeof store.getItem === 'function') return store;
  try {
    if (typeof localStorage !== 'undefined' && localStorage) return localStorage;
  } catch { /* приватный режим */ }
  return null;
}

function readJson(store, key, fallback) {
  const s = safeStorage(store);
  if (!s) return fallback;
  try {
    const raw = s.getItem(key);
    if (!raw) return fallback;
    const data = JSON.parse(raw);
    return data;
  } catch { return fallback; }
}

function writeJson(store, key, value) {
  const s = safeStorage(store);
  if (!s || typeof s.setItem !== 'function') return false;
  try { s.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}

/** Время записи: секунды (как у сервера), мс — тоже понимаем. */
export function stampOf(x) {
  const n = Number(x);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n >= 1e11 ? Math.round(n / 1000) : Math.round(n);
}

/* ─────────────────────────────────────────────────── «Недавние» (10) */

/**
 * Нормализовать запись «Недавнего».
 * @param {object} raw
 * @returns {{id:string, roomId:string, title:string, kind:string, at:number}|null}
 */
export function normRecent(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const kind = String(raw.kind || 'request') === 'delo' ? 'delo' : 'request';
  const id = String(raw.id || '').trim();
  if (!id) return null;
  const roomId = String(raw.roomId || raw.room_id || '').trim();
  if (kind === 'request' && !roomId) return null;
  return {
    id,
    roomId,
    title: String(raw.title || raw.title_short || '').trim(),
    kind,
    at: stampOf(raw.at || raw.created_at || raw.opened_at || Date.now() / 1000),
  };
}

/** Разобрать сохранённый список «Недавнего» (мусор не ломает меню). */
export function parseRecents(raw, max = RECENT_MAX) {
  const data = typeof raw === 'string' ? (() => { try { return JSON.parse(raw); } catch { return null; } })() : raw;
  const list = data && Array.isArray(data.items) ? data.items : (Array.isArray(data) ? data : []);
  const out = [];
  const seen = new Set();
  for (const item of list) {
    const rec = normRecent(item);
    if (!rec) continue;
    const key = rec.kind + ':' + rec.id;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(rec);
    if (out.length >= max) break;
  }
  return out;
}

/** Прочитать «Недавнее». */
export function readRecents(store, max = RECENT_MAX) {
  return parseRecents(readJson(store, RECENT_KEY, null), max);
}

/**
 * Записать открытие запроса/дела в «Недавнее» (самый свежий — первым).
 * @param {{id:string, roomId?:string, title?:string, kind?:string, at?:number}} entry
 * @param {{store?:Storage, max?:number}} [opts]
 * @returns {object[]} получившийся список
 */
export function pushRecent(entry, opts = {}) {
  const max = Number(opts.max) > 0 ? Number(opts.max) : RECENT_MAX;
  const rec = normRecent(Object.assign({ at: Math.round(Date.now() / 1000) }, entry || {}));
  if (!rec) return readRecents(opts.store, max);
  const prev = readRecents(opts.store, max).filter((r) => !(r.kind === rec.kind && r.id === rec.id));
  // Название не теряем: если в записи его нет, оставляем прежнее.
  const old = readRecents(opts.store, max).find((r) => r.kind === rec.kind && r.id === rec.id);
  if (!rec.title && old && old.title) rec.title = old.title;
  const next = [rec].concat(prev).slice(0, max);
  writeJson(opts.store, RECENT_KEY, { v: 1, items: next });
  return next;
}

/** Переименовали запрос — обновить подпись в «Недавнем» сразу. */
export function renameRecent(id, title, opts = {}) {
  const key = String(id || '');
  const text = String(title || '').trim();
  if (!key || !text) return readRecents(opts.store);
  const list = readRecents(opts.store).map((r) => (r.id === key ? Object.assign({}, r, { title: text }) : r));
  writeJson(opts.store, RECENT_KEY, { v: 1, items: list });
  return list;
}

/**
 * Слить «Недавнее» из двух источников: то, что человек открывал (локально),
 * и последние запросы комнат с сервера. Свежее — выше, дубли по id+вид.
 *
 * @param {object[]} local
 * @param {object[]} server
 * @param {number} [max]
 * @returns {object[]}
 */
export function mergeRecents(local, server, max = RECENT_MAX) {
  const out = [];
  const seen = new Set();
  // Локальные записи знают точное «когда открывал» — они идут первыми по времени.
  // ВАЖНО: поля серверной записи (status, pinned, title_short, roomTitle) не
  // теряем — по ним рисуются точка статуса, звезда и подпись комнаты.
  const all = (Array.isArray(local) ? local : []).concat(Array.isArray(server) ? server : []);
  all.sort((a, b) => (stampOf(b && (b.at || b.opened_at)) - stampOf(a && (a.at || a.opened_at))));
  for (const raw of all) {
    const rec = normRecent(raw);
    if (!rec) continue;
    const key = rec.kind + ':' + rec.id;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(Object.assign({}, raw, {
      id: rec.id,
      kind: rec.kind,
      roomId: rec.roomId,
      title: String(raw.title || rec.title || ''),
      at: rec.at,
    }));
    if (out.length >= Math.max(1, Number(max) || RECENT_MAX)) break;
  }
  return out;
}

/* ─────────────────────────────────────────────────────── «Закладки» (★) */

/**
 * Нормализовать закладку.
 * @returns {{id:string, roomId:string, title:string, at:number}|null}
 */
export function normBookmark(raw) {
  const rec = normRecent(Object.assign({ kind: 'request' }, raw || {}));
  if (!rec) return null;
  return { id: rec.id, roomId: rec.roomId, title: rec.title, at: rec.at };
}

/** Прочитать закладки (локальный список id — отображение звёзд). */
export function readBookmarks(store) {
  const data = readJson(store, BOOKMARK_KEY, []);
  const list = data && Array.isArray(data.items) ? data.items : (Array.isArray(data) ? data : []);
  const out = [];
  const seen = new Set();
  for (const item of list) {
    const rec = normBookmark(item);
    if (!rec || seen.has(rec.id)) continue;
    seen.add(rec.id);
    out.push(rec);
  }
  return out;
}

function writeBookmarks(store, list) {
  const items = (Array.isArray(list) ? list : []).map((b) => ({
    id: String(b.id), roomId: String(b.roomId || ''), title: String(b.title || ''), at: Number(b.at) || 0,
  }));
  writeJson(store, BOOKMARK_KEY, { v: 1, items });
  return items;
}

/** Есть ли звёздочка у запроса. */
export function isBookmarked(id, opts = {}) {
  const key = String(id || '');
  return readBookmarks(opts.store).some((b) => b.id === key);
}

/**
 * Поставить/снять звезду локально (сервер — отдельно, тем же действием).
 * @returns {{on:boolean, list:object[]}}
 */
export function setBookmark(entry, on, opts = {}) {
  const rec = normBookmark(Object.assign({ at: Math.round(Date.now() / 1000) }, entry || {}));
  const key = rec ? rec.id : String((entry && entry.id) || '');
  const prev = readBookmarks(opts.store).filter((b) => b.id !== key);
  if (!rec) return { on: false, list: prev };
  if (on) {
    const list = writeBookmarks(opts.store, [Object.assign({}, rec, { at: Math.round(Date.now() / 1000) })].concat(prev));
    return { on: true, list };
  }
  return { on: false, list: writeBookmarks(opts.store, prev) };
}

/** Переключить звезду. */
export function toggleBookmark(entry, opts = {}) {
  const id = String((entry && entry.id) || '');
  const have = isBookmarked(id, opts);
  return setBookmark(entry, !have, opts);
}

/**
 * Сверить закладки с ответом сервера: запрос без `pinned` больше не закладка.
 *
 * @param {object[]} serverRequests записи с полями id/roomId/title/pinned
 * @param {{store?:Storage}} [opts]
 * @returns {object[]} закладки после сверки
 */
export function syncBookmarks(serverRequests, opts = {}) {
  const rows = Array.isArray(serverRequests) ? serverRequests : [];
  const known = new Map(rows.map((r) => [String(r && r.id || ''), r]));
  const local = readBookmarks(opts.store);
  let changed = false;
  const next = [];
  for (const b of local) {
    const hit = known.get(b.id);
    if (hit && hit.pinned === false) { changed = true; continue; }
    if (hit) {
      const title = String(hit.title || hit.title_short || '').trim();
      const roomId = String(hit.roomId || hit.room || '').trim();
      if (title && title !== b.title) { b.title = title; changed = true; }
      if (roomId && roomId !== b.roomId) { b.roomId = roomId; changed = true; }
    }
    next.push(b);
  }
  // Закреплённые на сервере, но не у нас (звезда поставлена с другой копии окна
  // или до появления этого списка) — добавляем: источник правды — сервер.
  for (const row of rows) {
    if (!row || row.pinned !== true) continue;
    const id = String(row.id || '');
    if (!id || next.some((b) => b.id === id)) continue;
    const rec = normBookmark({
      id, roomId: row.roomId || row.room, title: row.title || row.title_short,
      at: row.at || row.created_at,
    });
    if (rec) { next.push(rec); changed = true; }
  }
  if (changed) writeBookmarks(opts.store, next);
  return next;
}

/** Заголовок закладки. */
export function bookmarkLabel(item) {
  const t = String((item && item.title) || '').trim();
  return t || 'Задача';
}
