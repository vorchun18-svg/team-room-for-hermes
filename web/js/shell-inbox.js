/**
 * shell-inbox.js — сколько дел ждёт решения человека в каждой комнате.
 *
 * Один источник правды для трёх мест: бейдж вкладки «От вас нужно», число у комнаты
 * в сайдбаре и счётчик в заголовке окна. Считаем «дела», а не запросы:
 * вопросы + заблокированные задачи + предложенные решения — ровно те строки,
 * которые человек видит в блоке «Ждут вашего решения».
 *
 * Числа кладёт сюда вкладка «От вас нужно», когда сама всё посчитала (setInboxCounts),
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
let soon = null;

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
 *
 * W26-D7: считаем только ДЕЛА, которые человек видит и может решить
 * (вопросы + блокировки + решения + разрешения — их кладёт вкладка «От вас
 * нужно» или фоновый опрос). Раньше при отсутствии этих чисел подставлялся
 * `counts.needs_you` сервера — а он считает запросы и включает реакции
 * руководителя, из-за чего меню показывало «20» там, где ждёт решения один
 * пункт. Теперь неизвестное число так и показывается: «—» (D19), без вранья.
 * @param {object|null} room нормализованная комната из /api/rooms
 * @returns {{n: number|null, text: string}}
 */
export function waitingFor(room) {
  const cases = inboxTotal(room && room.id);
  if (cases !== null) return { n: cases, text: countText(cases, COUNT_FORMS.case) };
  return { n: null, text: rooms.get('error') ? 'нет данных' : 'считаю' };
}

/**
 * Положить посчитанные числа (вызывает вкладка «От вас нужно» после своей отрисовки).
 * @param {string} roomId
 * @param {{questions?:number, blocked?:number, decisions?:number, approvals?:number}} parts
 */
export function setInboxCounts(roomId, parts = {}) {
  const id = String(roomId === null || roomId === undefined ? '' : roomId);
  if (!id) return;
  const questions = Math.max(0, Number(parts.questions) || 0);
  const blocked = Math.max(0, Number(parts.blocked) || 0);
  const decisions = Math.max(0, Number(parts.decisions) || 0);
  // W22-AP2: запрос разрешения агенту — тоже дело, ждущее человека.
  const approvals = Math.max(0, Number(parts.approvals) || 0);
  // W27-QA2 (§1): серверное число «ждёт вас» главнее суммы — клиент не
  // пересчитывает список. Без поля — прежняя сумма (старый сервер / моки).
  // W27-FIX: серверное число приходит из «Сегодня» и НЕ содержит шагов: шаги
  // живут в «Просрочено/Ближайшие», и число меню обязано совпадать с экраном.
  const given = Number(parts.total);
  const total = (parts.total !== undefined && parts.total !== null && Number.isFinite(given) && given >= 0)
    ? given
    : questions + blocked + decisions + approvals;
  const prev = cache.get(id);
  if (prev && prev.total === total && prev.questions === questions
    && prev.blocked === blocked && prev.decisions === decisions
    && prev.approvals === approvals) return;
  cache.set(id, { questions, blocked, decisions, approvals, total, at: Date.now(), source: 'view' });
  emit();
}

/** Сколько ждущих разрешений у запросов (по `detail.approvals`), без дублей. */
function countApprovals(details) {
  const seen = new Set();
  let n = 0;
  for (const detail of Array.isArray(details) ? details : []) {
    const list = detail && Array.isArray(detail.approvals) ? detail.approvals : [];
    for (const a of list) {
      if (!a) continue;
      // W27-E2 §1: ждёт человека только `pending`. Запись без `state` у завершённой
      // карточки — закрытая (`closed`), а не «ждёт»: иначе меню считало закрытое.
      const state = String(a.state || (a.card_state === 'done' || a.card_state === 'archived'
        ? 'closed' : 'pending')).toLowerCase();
      if (state !== 'pending') continue;
      const id = String(a.id || '');
      if (id && seen.has(id)) continue;
      if (id) seen.add(id);
      n += 1;
    }
  }
  return n;
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

/** Сколько вопросов и решений реально ждут ответа (по деталям запросов), без дублей. */
function countWaits(details) {
  const qs = new Set();
  const ds = new Set();
  for (const detail of Array.isArray(details) ? details : []) {
    for (const q of (detail && Array.isArray(detail.open_questions) ? detail.open_questions : [])) {
      if (!q || q.answered || q.dismissed || q.needs_answer === false) continue;
      qs.add(String(q.id || ''));
    }
    for (const d of (detail && Array.isArray(detail.decisions) ? detail.decisions : [])) {
      if (!d || d.dismissed || d.superseded) continue;
      if (String(d.status || '') !== 'proposed') continue;
      ds.add(String(d.id || ''));
    }
  }
  return { questions: qs.size, decisions: ds.size };
}

async function loadRoom(roomId, todayNumbers) {
  const backlog = itemsOf(await api.get('/api/rooms/' + enc(roomId) + '/backlog'));
  let questions = backlog.filter((i) => i && i.kind === 'open_question' && i.status === 'open').length;
  let decisions = backlog.filter((i) => i && i.kind === 'decision' && i.status === 'open').length;

  // W27-QA2 (§1): серверное число «ждёт вас» у запроса (`needs_you_count`).
  // Приходит из деталей — тогда меню показывает ЕГО, а не свой пересчёт.
  let serverTotal = null;

  let blocked = 0;
  let approvals = 0;
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
    // W22-AP2: ждущие разрешения считаются из тех же деталей.
    approvals = countApprovals(details);
    // W26-QA2: индекс бэклога может отставать от карточек (в снимке боевых данных
    // у вопроса один id в карточке и другой в индексе — «От вас нужно» показывало 7
    // там, где в карточках 10 пунктов). Если детали пришли по всем запросам, что
    // просят внимания, считаем по ним — это те же пункты, что видит человек.
    if (waiting.length && details.length === waiting.length) {
      const fromDetails = countWaits(details);
      questions = fromDetails.questions;
      decisions = fromDetails.decisions;
      // W27-QA2 (§1): если сервер прислал `needs_you_count`, число в меню — ЕГО.
      // Клиент не пересчитывает список: иначе меню показывало 2 там, где колонка
      // запроса и сервер говорят 4 (вопросы + шаги), и два числа расходились.
      // W27-FIX: шаги в число меню НЕ входят — у них своё место в «Просрочено/
      // Ближайшие», а «Сегодня» для комнаты считает ровно «вопросы + решения +
      // разрешения». Меню и экран «Сегодня» обязаны показывать одно число.
      let total = 0;
      let haveTotal = false;
      for (const detail of details) {
        const counts = detail && detail.needs_you_count;
        if (!counts || typeof counts !== 'object') continue;
        const parts = [counts.questions, counts.decisions, counts.approvals].map(Number);
        if (parts.some((value) => !Number.isFinite(value) || value < 0)) continue;
        // W31-FIX4: заблокированная карточка — такой же пункт «ждёт вас»: сервер
        // считает её в `needs_you_count.blocked` (и в `total`), поэтому и запасной
        // счёт меню обязан её включать, иначе число в меню меньше экрана.
        const blockedN = Number.isFinite(Number(counts.blocked)) ? Number(counts.blocked) : 0;
        total += parts.reduce((sum, value) => sum + value, 0) + blockedN;
        haveTotal = true;
      }
      if (haveTotal) {
        serverTotal = total;
      }
    }
  } catch { /* нет списка задач — считаем без блокировок */ }

  // W27-FIX: число меню/бейджа — ровно то, что показывает «Сегодня» для этой
  // комнаты (`/api/today` → `rooms[].needs_you`). Один запрос на все комнаты,
  // сервер кэширует ответ 10 с; клиент больше не собирает число по части списка
  // запросов (из-за лимита и фильтра статусов оно расходилось с экраном).
  // Нет ответа «Сегодня» — прежний путь: сумма `needs_you_count` по деталям.
  let total = serverTotal;
  if (todayNumbers && todayNumbers.has(String(roomId))) {
    total = todayNumbers.get(String(roomId));
  }
  setInboxCounts(roomId, { questions, blocked, decisions, approvals, total });
}

/**
 * Числа «Сегодня» по комнатам: `Map(roomId → needs_you)`.
 *
 * W27-FIX: единственный источник числа в меню — экран «Сегодня». Он берёт пункты
 * из самих запросов (тот же расчёт, что `needs_you_count` страницы запроса), а
 * шаги в это число не входят. Возвращает `null`, если ответ не пришёл: тогда
 * меню считает по деталям, как раньше.
 * @returns {Promise<Map<string, number>|null>}
 */
async function loadTodayNumbers() {
  try {
    const payload = await api.get('/api/today');
    const rows = payload && Array.isArray(payload.rooms) ? payload.rooms : [];
    const map = new Map();
    for (const row of rows) {
      if (!row || !row.id) continue;
      const value = Number(row.needs_you);
      if (Number.isFinite(value) && value >= 0) map.set(String(row.id), Math.trunc(value));
    }
    return map;
  } catch {
    return null;
  }
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
    // Числа «Сегодня» — один запрос на все комнаты (W27-FIX): меню и экран
    // показывают одно и то же число, клиент не пересчитывает его по части списка.
    const todayNumbers = await loadTodayNumbers();
    for (const room of list) {
      try {
        await loadRoom(room.id, todayNumbers);
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
  // W26-QA2: человек ответил на вопрос или принял решение — числа «ждёт вас»
  // должны догнать это сразу, а не через 20 секунд: стор запроса шлёт событие,
  // по нему пересчитываем (с небольшой задержкой — на пачку быстрых действий).
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('teamroom:request-changed', () => {
      if (soon) clearTimeout(soon);
      // Пересчёт идёт по восьми карточкам — если он уже идёт (`busy`), не теряем
      // событие, а повторяем попытку: иначе счётчик оставался старым до опроса.
      const tryRun = (left) => {
        soon = setTimeout(async () => {
          soon = null;
          if (busy && left > 0) { tryRun(left - 1); return; }
          await refreshInbox();
          if (busy && left > 0) tryRun(left - 1);
        }, 600);
      };
      tryRun(3);
    });
  }
  return {
    stop() {
      if (timer) { clearInterval(timer); timer = null; }
      if (soon) { clearTimeout(soon); soon = null; }
    },
  };
}

export default {
  subscribeInbox, inboxOf, inboxTotal, waitingFor, setInboxCounts, refreshInbox, startInboxPolling,
};
