/**
 * shell-inbox.js — сколько дел ждёт решения человека в каждой комнате.
 *
 * Один источник правды для трёх мест: бейдж вкладки «Дальше», число у комнаты
 * в сайдбаре и счётчик в заголовке окна. Считаем «дела», а не запросы:
 * вопросы + заблокированные задачи + предложенные решения — ровно те строки,
 * которые человек видит в блоке «Ждут вашего решения».
 *
 * Числа кладёт сюда вкладка «Дальше», когда сама всё посчитала (setInboxCounts),
 * а если её не открывали — фоновый опрос догружает то же самое по API.
 * При обрыве связи остаётся последнее известное число, а не ноль (D19).
 */

import { api } from './api.js';
import { rooms } from './store.js';
import { countText, COUNT_FORMS } from './shell-labels.js';

const POLL_MS = 20000;
const DETAILS_LIMIT = 8;

/** roomId → {questions, blocked, decisions, total, at, source} */
const cache = new Map();
const listeners = new Set();
let timer = null;
let busy = false;

const enc = (s) => encodeURIComponent(String(s === null || s === undefined ? '' : s));

function itemsOf(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.items)) return payload.items;
  return [];
}

function emit() {
  for (const fn of Array.from(listeners)) {
    try { fn(); } catch (err) { console.error('[inbox] subscriber failed', err); }
  }
}

/**
 * Подписка на изменение чисел (каркас перерисовывает бейджи).
 * @param {Function} fn
 * @returns {Function} отписка
 */
export function subscribeInbox(fn) {
  if (typeof fn !== 'function') return () => {};
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Последнее известное число дел по комнате или null. */
export function inboxOf(roomId) {
  const hit = cache.get(String(roomId === null || roomId === undefined ? '' : roomId));
  return hit || null;
}

/** Число дел или null, если ещё не считали. */
export function inboxTotal(roomId) {
  const hit = inboxOf(roomId);
  return hit && Number.isFinite(hit.total) ? hit.total : null;
}

/**
 * Что показывать у комнаты: число дел и подпись с существительным.
 * Пока счёт не готов — число запросов (другое существительное, чтобы не
 * выдавать запросы за дела). При обрыве связи — null: «—», а не ноль (D19).
 * @param {object|null} room нормализованная комната из /api/rooms
 * @returns {{n: number|null, text: string}}
 */
export function waitingFor(room) {
  const cases = inboxTotal(room && room.id);
  if (cases !== null) return { n: cases, text: countText(cases, COUNT_FORMS.case) };
  if (rooms.get('error')) return { n: null, text: 'нет данных' };
  const n = Number((room && room.counts && room.counts.needs_you) || 0);
  return { n, text: countText(n, COUNT_FORMS.request) };
}

/**
 * Положить посчитанные числа (вызывает вкладка «Дальше» после своей отрисовки).
 * @param {string} roomId
 * @param {{questions?:number, blocked?:number, decisions?:number}} parts
 */
export function setInboxCounts(roomId, parts = {}) {
  const id = String(roomId === null || roomId === undefined ? '' : roomId);
  if (!id) return;
  const questions = Math.max(0, Number(parts.questions) || 0);
  const blocked = Math.max(0, Number(parts.blocked) || 0);
  const decisions = Math.max(0, Number(parts.decisions) || 0);
  const total = questions + blocked + decisions;
  const prev = cache.get(id);
  if (prev && prev.total === total && prev.questions === questions
    && prev.blocked === blocked && prev.decisions === decisions) return;
  cache.set(id, { questions, blocked, decisions, total, at: Date.now(), source: 'view' });
  emit();
}

/** Сколько заблокированных карточек в деталях запросов (уникальных по id). */
function countBlocked(details) {
  const seen = new Set();
  let n = 0;
  for (const detail of Array.isArray(details) ? details : []) {
    const tree = detail && Array.isArray(detail.tree) ? detail.tree : [];
    for (const node of tree) {
      if (!node || node.status !== 'blocked') continue;
      const id = String(node.id || '');
      if (id && seen.has(id)) continue;
      if (id) seen.add(id);
      n += 1;
    }
  }
  return n;
}

async function loadRoom(roomId) {
  const backlog = itemsOf(await api.get('/api/rooms/' + enc(roomId) + '/backlog'));
  const questions = backlog.filter((i) => i && i.kind === 'open_question' && i.status === 'open').length;
  const decisions = backlog.filter((i) => i && i.kind === 'decision' && i.status === 'open').length;

  let blocked = 0;
  try {
    const feed = itemsOf(await api.get('/api/rooms/' + enc(roomId) + '/requests?limit=50'));
    const waiting = feed
      .filter((r) => r && r.id && !r.archived && (r.needs_you || r.status === 'needs_you'))
      .slice(0, DETAILS_LIMIT);
    const details = [];
    for (const rec of waiting) {
      try {
        details.push(await api.get('/api/requests/' + enc(rec.id)));
      } catch { /* одна карточка не должна обнулять счёт */ }
    }
    blocked = countBlocked(details);
  } catch { /* нет списка запросов — считаем без блокировок */ }

  setInboxCounts(roomId, { questions, blocked, decisions });
}

/**
 * Пересчитать числа по всем комнатам. Ошибки отдельных комнат не мешают другим.
 * @returns {Promise<void>}
 */
export async function refreshInbox() {
  if (busy) return;
  busy = true;
  try {
    const list = rooms.get('list').filter((r) => r && r.id && !r.archived);
    for (const room of list) {
      try {
        await loadRoom(room.id);
      } catch { /* связи нет — оставляем последнее известное число */ }
    }
    // Комнаты, которых больше нет, не держим в кэше.
    const alive = new Set(list.map((r) => String(r.id)));
    for (const id of Array.from(cache.keys())) if (!alive.has(id)) cache.delete(id);
  } finally {
    busy = false;
  }
}

/**
 * Фоновый опрос чисел (20 с — дешевле, чем опрос каждой вкладки).
 * @returns {{stop: Function}}
 */
export function startInboxPolling() {
  refreshInbox();
  if (!timer) timer = setInterval(() => { refreshInbox(); }, POLL_MS);
  return {
    stop() {
      if (timer) { clearInterval(timer); timer = null; }
    },
  };
}

export default {
  subscribeInbox, inboxOf, inboxTotal, waitingFor, setInboxCounts, refreshInbox, startInboxPolling,
};
