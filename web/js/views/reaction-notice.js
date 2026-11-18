/**
 * views/reaction-notice.js — «руководитель отреагировал на ваш ответ» (W20-U4, P9).
 *
 * Три места, где человек должен увидеть реакцию руководителя, и один источник слов:
 *
 *  1. бейдж на вкладке «От вас нужно» — «есть новые реакции, их ещё не смотрели»;
 *  2. отметка в «Недавнем» бокового меню у того запроса, где отреагировали;
 *  3. строка «Ответ учтён: …» прямо у вопроса (её рисует request-aside.js).
 *
 * Откуда данные
 * -------------
 * `GET /api/rooms/summary` отдаёт по комнате `last_reaction_at` (время последней
 * реакции руководителя), `reactions_unseen` (сколько реакций найдено) и
 * `reactions: {<id запроса>: {count, last_at}}`. Точное «прочитано» сервер знать
 * не может — отметка живёт в браузере (`localStorage`, ключ
 * `teamroom.reactions.<комната>`); «непрочитанное» = реакция позже отметки.
 *
 * Модуль без побочных эффектов при импорте: DOM трогают только билдеры
 * (`reactionTabBadge`, `recentReactionMark`), хранилище — только через
 * переданный/найденный store. Поэтому он проверяется node --test без браузера.
 */

/** Префикс ключа хранилища отметок «видел». */
export const REACTIONS_KEY = 'teamroom.reactions.';

/** Слова экрана — в одном месте (text-ru.js держит общий словарь U1). */
export const NOTICE_RU = {
  tabNew: 'Новые реакции руководителя',
  tabOne: 'Новая реакция руководителя',
  tabHint: 'Руководитель отреагировал на ваш ответ. Откройте «От вас нужно» или запрос — там строка «Ответ учтён».',
  recent: 'руководитель ответил на ваш ответ',
  answered: 'Ответ учтён',
  read: 'Руководитель прочитал ответ',
  step: 'Шаг учтён',
  decision: 'Решение учтено',
  seen: 'прочитано',
};

/** Метки реакции руководителя в ветке: «ответ», «шаг», «решение». */
const KIND_OF_MARK = [
  [/реакция на ответ/i, 'answer'],
  [/реакция на шаг/i, 'step'],
  [/реакция на решение/i, 'decision'],
];

/**
 * Вид реакции по тексту сообщения руководителя (`[Руководитель · реакция на шаг …]`).
 * @param {string} text
 * @returns {'answer'|'step'|'decision'}
 */
export function reactionKind(text) {
  const raw = String(text === null || text === undefined ? '' : text);
  for (const [re, kind] of KIND_OF_MARK) if (re.test(raw)) return kind;
  return 'answer';
}

/** «13:48» из секунд эпохи (локальное время); пустая строка, если времени нет. */
export function clockOf(at) {
  const ts = Number(at) || 0;
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return hh + ':' + mm;
}

/**
 * Убрать из подписи сервера повтор «учтено», чтобы строка не читалась дважды.
 * «учтено: работа пересмотрена» → «работа пересмотрена».
 * @param {string} label
 * @returns {string}
 */
export function cleanReactionLabel(label) {
  return String(label === null || label === undefined ? '' : label)
    .replace(/^\s*учтено\s*[:\-—–]*\s*/i, '')
    .trim();
}

/**
 * Строка у вопроса: «Ответ учтён: работа пересмотрена · 13:48».
 *
 * Статус берём только из полей API (`status`, `label`); своих догадок нет.
 * @param {{status?:string, label?:string, text?:string, at?:number, kind?:string}} reaction
 * @param {{kind?: 'answer'|'step'|'decision', time?: string}} [opts]
 * @returns {string}
 */
export function noticeLine(reaction, opts = {}) {
  const r = reaction && typeof reaction === 'object' ? reaction : null;
  if (!r) return '';
  const kind = String((opts && opts.kind) || r.kind || 'answer');
  const status = String(r.status || '');
  const when = String((opts && opts.time) || clockOf(r.at) || '');
  const label = cleanReactionLabel(r.label) || cleanReactionLabel(r.text);
  const head = NOTICE_RU[kind] || NOTICE_RU.answered;
  const parts = [];
  if (status === 'received' && kind === 'answer') {
    parts.push(NOTICE_RU.read);
  } else {
    parts.push(head + (label ? ': ' + label : ''));
  }
  const line = parts.filter(Boolean).join(' ');
  return when ? line + ' · ' + when : line;
}

/** Склонение «реакция/реакции/реакций» — для подсказки бейджа. */
export function reactionsWord(n) {
  const num = Math.abs(Number(n) || 0);
  const mod10 = num % 10;
  const mod100 = num % 100;
  if (mod100 >= 11 && mod100 <= 14) return 'реакций';
  if (mod10 === 1) return 'реакция';
  if (mod10 >= 2 && mod10 <= 4) return 'реакции';
  return 'реакций';
}

/** Склонение «запрос/запроса/запросов». */
export function requestsWord(n) {
  const num = Math.abs(Number(n) || 0);
  const mod10 = num % 10;
  const mod100 = num % 100;
  if (mod100 >= 11 && mod100 <= 14) return 'запросов';
  if (mod10 === 1) return 'запроса';
  if (mod10 >= 2 && mod10 <= 4) return 'запроса';
  return 'запросов';
}

/**
 * Разобрать данные сводки по комнате в одну запись.
 * @param {object} room элемент `rooms[]` из /api/rooms/summary
 * @returns {{count:number, last_at:number, requests: Object<string,{count:number,last_at:number}>}}
 */
export function roomReactionInfo(room) {
  const src = room && typeof room === 'object' ? room : {};
  const requests = {};
  const raw = src.reactions && typeof src.reactions === 'object' ? src.reactions : {};
  for (const [rid, value] of Object.entries(raw)) {
    const v = value && typeof value === 'object' ? value : {};
    const at = Number(v.last_at || v.at) || 0;
    const count = Number(v.count) || (at ? 1 : 0);
    if (!rid || (!count && !at)) continue;
    requests[String(rid)] = { count, last_at: at };
  }
  let count = Number(src.reactions_unseen);
  if (!Number.isFinite(count)) count = Number(src.reactions_total);
  if (!Number.isFinite(count)) {
    count = Object.values(requests).reduce((sum, v) => sum + (Number(v.count) || 0), 0);
  }
  let lastAt = Number(src.last_reaction_at) || 0;
  if (!lastAt) {
    lastAt = Object.values(requests).reduce((max, v) => Math.max(max, Number(v.last_at) || 0), 0);
  }
  // Сводка без карты запросов, но с временем: реакция есть, адресат неизвестен.
  if (!Object.keys(requests).length && lastAt) {
    const guess = Math.max(1, count || 1);
    requests[''] = { count: guess, last_at: lastAt };
    count = guess;
  }
  return { count: Math.max(0, count || 0), last_at: lastAt, requests };
}

/**
 * «Что не прочитано»: реакция новее отметки.
 *
 * Отметку храним по комнате (`at` — «прочитал всё до этого времени») и по запросу
 * (`req[rid]` — открывал конкретный запрос). Итог — новые реакции и их запросы.
 * @param {object} info как из roomReactionInfo()
 * @param {{at?:number, req?: Object<string, number>}} seen
 * @returns {{count:number, requests: string[], last_at:number, total:number}}
 */
export function unseenOf(info, seen) {
  const data = info && typeof info === 'object' ? info : { count: 0, last_at: 0, requests: {} };
  const s = seen && typeof seen === 'object' ? seen : {};
  const roomAt = Number(s.at) || 0;
  const byReq = s.req && typeof s.req === 'object' ? s.req : {};
  const requests = [];
  let count = 0;
  let lastAt = 0;
  const entries = Object.entries(data.requests || {});
  if (!entries.length) {
    // Только время: считаем «есть новое», пока отметка комнаты старше.
    const total = Math.max(0, Number(data.count) || 0);
    if (Number(data.last_at) > roomAt) {
      return { count: Math.max(1, total), requests: [], last_at: Number(data.last_at) || 0, total };
    }
    return { count: 0, requests: [], last_at: 0, total };
  }
  for (const [rid, value] of entries) {
    const at = Number(value && value.last_at) || 0;
    const floor = Math.max(roomAt, Number(byReq[rid]) || 0);
    if (at && at <= floor) continue;
    if (!at && !(Number(value && value.count) || 0)) continue;
    requests.push(rid);
    count += Math.max(1, Number(value && value.count) || 1);
    lastAt = Math.max(lastAt, at);
  }
  return { count, requests, last_at: lastAt, total: Math.max(0, Number(data.count) || 0) };
}

/** Подсказка и подпись бейджа: «2 запроса · 3 реакции». */
export function badgeText(unseen) {
  const u = unseen && typeof unseen === 'object' ? unseen : { count: 0, requests: [] };
  const n = Math.max(0, Number(u.count) || 0);
  const reqs = Math.max(0, Array.isArray(u.requests) ? u.requests.length : 0);
  if (!n) return { count: 0, label: '', title: '' };
  const title = NOTICE_RU.tabNew + ': ' + n + ' ' + reactionsWord(n)
    + (reqs ? ' в ' + reqs + ' ' + requestsWord(reqs) : '')
    + '. ' + NOTICE_RU.tabHint;
  return { count: n, label: n > 99 ? '99+' : String(n), title };
}

/* ─────────────────────────────────────────────────────── хранилище отметок */

const MEMORY = new Map();

/** Хранилище по умолчанию: localStorage, иначе память модуля (node, приватный режим). */
function defaultStore() {
  try {
    if (typeof localStorage !== 'undefined' && localStorage) {
      const probe = REACTIONS_KEY + '__probe';
      localStorage.setItem(probe, '1');
      localStorage.removeItem(probe);
      return {
        get: (key) => localStorage.getItem(key),
        set: (key, value) => localStorage.setItem(key, String(value)),
      };
    }
  } catch { /* нет доступа — живём в памяти */ }
  return {
    get: (key) => (MEMORY.has(key) ? MEMORY.get(key) : null),
    set: (key, value) => { MEMORY.set(key, String(value)); },
  };
}

/** Ключ хранилища комнаты. */
export function reactionsKey(roomId) {
  const id = String(roomId === null || roomId === undefined ? '' : roomId).trim();
  return REACTIONS_KEY + (id || 'unknown');
}

/**
 * Отметки «видел» одной комнаты. Состояние: `{at, req: {<rid>: <время>}}`.
 * @param {string} roomId
 * @param {{store?: {get:Function, set:Function}, key?: string}} [opts]
 */
export function createSeen(roomId, opts = {}) {
  const o = opts || {};
  const key = String(o.key || reactionsKey(roomId));
  const store = o.store || defaultStore();
  let state = read();

  function read() {
    try {
      const raw = store.get(key);
      if (!raw) return { at: 0, req: {} };
      const value = JSON.parse(String(raw));
      if (!value || typeof value !== 'object' || Array.isArray(value)) return { at: 0, req: {} };
      const req = {};
      const rawReq = value.req && typeof value.req === 'object' && !Array.isArray(value.req) ? value.req : {};
      for (const [rid, at] of Object.entries(rawReq)) {
        const num = Number(at) || 0;
        if (rid && num) req[String(rid)] = num;
      }
      return { at: Number(value.at) || 0, req };
    } catch { return { at: 0, req: {} }; }
  }

  function save() {
    try { store.set(key, JSON.stringify(state)); } catch { /* переполнено — живём в памяти */ }
  }

  return {
    key,
    snapshot() { return { at: Number(state.at) || 0, req: Object.assign({}, state.req) }; },
    /** «Прочитал всё по комнате до времени at». */
    markAll(at) {
      const ts = Number(at) || 0;
      if (!ts) return false;
      const next = Math.max(Number(state.at) || 0, ts);
      if (next === state.at) return false;
      state.at = next;
      save();
      return true;
    },
    /** «Открыл этот запрос»: реакция в нём считается прочитанной. */
    markRequest(requestId, at) {
      const rid = String(requestId || '');
      const ts = Number(at) || 0;
      if (!rid || !ts) return false;
      const prev = Number(state.req[rid]) || 0;
      if (ts <= prev) return false;
      state.req[rid] = ts;
      save();
      return true;
    },
  };
}

/* ──────────────────────────────────────── кэш сводки и подписки (браузер) */

const infos = new Map();
const listeners = new Set();
const seenCache = new Map();

function seenFor(roomId) {
  const id = String(roomId === null || roomId === undefined ? '' : roomId);
  if (!seenCache.has(id)) seenCache.set(id, createSeen(id));
  return seenCache.get(id);
}

/** Подписка на изменение реакций / отметок (каркас перерисовывает бейджи). */
export function subscribeReactions(fn) {
  if (typeof fn !== 'function') return () => {};
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit() {
  for (const fn of Array.from(listeners)) {
    try { fn(); } catch (err) { console.error('[reaction-notice] subscriber failed', err); }
  }
}

/**
 * Принять ответ `/api/rooms/summary`: запомнить реакции по всем комнатам.
 * Вызывается из бокового меню — один запрос на всё приложение.
 * @param {object} payload
 * @returns {Map<string, object>} что запомнили
 */
export function ingestSummary(payload) {
  const rooms = payload && Array.isArray(payload.rooms) ? payload.rooms : null;
  if (!rooms) return infos;
  let changed = false;
  const alive = new Set();
  for (const room of rooms) {
    if (!room || typeof room !== 'object') continue;
    const id = String(room.id || room.room_id || '');
    if (!id) continue;
    alive.add(id);
    const next = roomReactionInfo(room);
    const prev = infos.get(id);
    if (!prev || prev.count !== next.count || prev.last_at !== next.last_at
      || JSON.stringify(prev.requests) !== JSON.stringify(next.requests)) {
      infos.set(id, next);
      changed = true;
    }
  }
  for (const id of Array.from(infos.keys())) if (!alive.has(id)) { infos.delete(id); changed = true; }
  if (changed) emit();
  return infos;
}

/** Что известно о реакциях комнаты (или пустая запись). */
export function roomReactions(roomId) {
  const id = String(roomId === null || roomId === undefined ? '' : roomId);
  return infos.get(id) || { count: 0, last_at: 0, requests: {} };
}

/** Непрочитанные реакции комнаты. */
export function unseenFor(roomId) {
  return unseenOf(roomReactions(roomId), seenFor(roomId).snapshot());
}

/** «Открыл этот запрос» — реакция в нём больше не новая. */
export function markRequestSeen(roomId, requestId, at) {
  const changed = seenFor(roomId).markRequest(requestId, at);
  if (changed) emit();
  return changed;
}

/** «Прочитал всё по комнате» (кнопка/переход на вкладку «От вас нужно»). */
export function markRoomSeen(roomId, at) {
  const changed = seenFor(roomId).markAll(at);
  if (changed) emit();
  return changed;
}

/** Для тестов и «Сбросить отметки» в настройках. */
export function resetReactionState() {
  infos.clear();
  seenCache.clear();
  emit();
}

/* ─────────────────────────────────────────────────────── элементы экрана */

/** Бейдж вкладки: число непрочитанных реакций; null, если новых нет. */
export function reactionTabBadge(roomId) {
  const unseen = unseenFor(roomId);
  const text = badgeText(unseen);
  if (!text.count) return null;
  const node = document.createElement('span');
  // W25.5: у вкладки ОДНО число — сколько дел ждёт вас. Второе число (новые
  // реакции руководителя) читалось как «7 / 23» и путало. Поэтому здесь —
  // точка-метка: «есть новое» видно, а число живёт в подсказке и в aria-label
  // (и в самой вкладке «От вас нужно», где строка реакции подписана).
  node.className = 'tab__count reaction-badge reaction-badge--dot';
  node.setAttribute('data-reaction-badge', String(text.count));
  node.setAttribute('aria-label', text.title);
  node.setAttribute('title', text.title);
  node.setAttribute('aria-hidden', 'false');
  node.setAttribute('role', 'img');
  return node;
}

/** Отметка в «Недавнем»: точка «руководитель ответил»; null, если нечего метить. */
export function recentReactionMark(item) {
  const rid = String((item && item.id) || '');
  const roomId = String((item && (item.roomId || item.room)) || '');
  if (!rid || !roomId) return null;
  const unseen = unseenOf(roomReactions(roomId), seenFor(roomId).snapshot());
  if (!unseen.requests.includes(rid)) return null;
  const node = document.createElement('span');
  node.className = 'recent__mark';
  node.setAttribute('role', 'img');
  node.setAttribute('aria-label', NOTICE_RU.recent);
  node.setAttribute('title', NOTICE_RU.recent);
  node.setAttribute('data-reaction-mark', '1');
  // Точка без текста: слово «руководитель ответил» живёт в подсказке и aria-label,
  // иначе ui_audit справедливо ругается на текст мельче 11 px.
  return node;
}

export default {
  NOTICE_RU,
  REACTIONS_KEY,
  reactionKind, clockOf, cleanReactionLabel, noticeLine, reactionsWord, requestsWord,
  roomReactionInfo, unseenOf, badgeText,
  createSeen, reactionsKey,
  subscribeReactions, ingestSummary, roomReactions, unseenFor,
  markRequestSeen, markRoomSeen, resetReactionState,
  reactionTabBadge, recentReactionMark,
};
