/**
 * stale-model.js — «неактуальные пункты» по ОДНОМУ контракту (волна 27, §1).
 *
 * Дефект 18.11 вечером: сервер отдавал `approvals[].state = "closed"` (работа
 * карточки закончена), а интерфейс такого состояния не знал и рисовал строку как
 * `pending` — «ждёт вашего решения», с кнопками и в счётчике. Нажатие уходило на
 * сервер, сервер отвечал `409 already_decided`, и человек получал девять красных
 * плашек подряд.
 *
 * Здесь собрано то, чем пользуются все места страницы, чтобы второго мнения о
 * состояниях не появилось:
 *
 *   * `CLOSED_REASON_RU` / `closedReasonText` — почему пункт закрыт, словами;
 *   * `isAlreadyDecided` — 409 `already_decided` это НЕ ошибка, а «перерисуйся»;
 *   * `toastOnce` — один тост на причину (дедупликация), а не девять;
 *   * `dismissBody` / `restoreBody` — тела действий §1 (`target:{kind,id}`);
 *   * `needsYouCount` — число «ждёт вас» с сервера (`needs_you_count`), а не
 *     пересчитанное по списку;
 *   * `staleSummary` — сколько пунктов у завершённого запроса ещё можно закрыть
 *     массово («Закрыть все неактуальные (N)»).
 *
 * Модуль чистый: ни DOM, ни fetch, ни импортов из видов (иначе получился бы цикл
 * approvals ↔ stale-model). Тесты — `node --test tests/w27-stale-fe.test.mjs`.
 */

/** Статусы запроса, у которого работа уже закончена (массовое закрытие — только для них). */
export const REQUEST_FINISHED = new Set(['done', 'archived']);

/** Причины закрытия (`closed_reason` из §1) — словами. */
export const CLOSED_REASON_RU = {
  card_finished: 'работа завершена',
  request_done: 'задача завершена',
  // W29-P4: пункты архивного запроса закрыты, пока он в архиве; после «Вернуть
  // из архива» они открываются снова сами.
  request_archived: 'задача в архиве',
  // W30-D1: задача принята человеком — она заморожена; после «Вернуть в работу»
  // пункты открываются снова сами (закрытие считается из флага, не пишется в данные).
  request_accepted: 'задача принята — заморожена',
  dismissed: 'закрыто как неактуальное',
  superseded: 'устарело: работа пошла иначе',
};

/** Состояния, в которых пункт уже не ждёт человека (§1, нерешаемые). */
export const NOT_WAITING = new Set([
  'closed', 'dismissed', 'expired', 'granted_once', 'granted_always', 'denied',
  'answered', 'accepted', 'rejected', 'done', 'acked',
]);

/** Почему пункт закрыт — короткой строкой; пусто, если причина неизвестна. */
export function closedReasonText(item) {
  const src = item && typeof item === 'object' ? item : {};
  const reason = String(src.closed_reason || src.close_reason || '').trim().toLowerCase();
  if (reason && CLOSED_REASON_RU[reason]) return CLOSED_REASON_RU[reason];
  if (reason === 'dismissed' || src.dismissed === true) return CLOSED_REASON_RU.dismissed;
  if (src.superseded === true || String(src.status || '') === 'superseded') {
    return CLOSED_REASON_RU.superseded;
  }
  return '';
}

/** Код ошибки сервера (в теле или в details) — без догадок о форме ответа. */
export function errorCode(err) {
  const src = err && typeof err === 'object' ? err : {};
  const details = (src.details && typeof src.details === 'object') ? src.details : {};
  const body = (src.body && typeof src.body === 'object') ? src.body : {};
  const inner = (details.error && typeof details.error === 'object') ? details.error : {};
  const list = [src.code, src.error, src.reason, details.code, body.code, inner.code];
  for (const value of list) {
    const text = String(value || '').trim().toLowerCase();
    if (text) return text;
  }
  return '';
}

/**
 * `409 already_decided` — пункт уже решён (человек или автозакрытие).
 * Для клиента это не ошибка: тихая перерисовка, без красной плашки.
 */
export function isAlreadyDecided(err) {
  const code = errorCode(err);
  if (code === 'already_decided' || code === 'already_answered' || code === 'already_dismissed') {
    return true;
  }
  const status = Number(err && (err.status || (err.details && err.details.status))) || 0;
  return status === 409 && (code === 'conflict' || code === '');
}

/** Пункт ушёл — 404: тоже не «ошибка человека», а устаревший экран. */
export function isGone(err) {
  const status = Number(err && (err.status || (err.details && err.details.status))) || 0;
  return status === 404 || errorCode(err) === 'not_found';
}

/* ─────────────────────────────────────────────────── тосты: один на причину */

const TOAST_SEEN = new Map();
const TOAST_WINDOW_MS = 8000;

/**
 * Показать тост не чаще одного раза на причину за 8 секунд.
 *
 * Девять одинаковых «Это разрешение уже решено» перекрывали экран (дефект 1).
 * Ключ — код причины, а не текст: разные формулировки одной причины тоже гасятся.
 *
 * @param {(text:string, tone:string)=>void} sink `ctx.toast`
 * @param {string} key причина (код ошибки, действие)
 * @param {string} text что сказать
 * @param {string} [tone]
 * @param {number} [at] момент времени (тесты)
 * @returns {boolean} показали ли тост
 */
export function toastOnce(sink, key, text, tone = 'info', at = Date.now()) {
  if (typeof sink !== 'function') return false;
  const id = String(key || text || '').trim();
  if (!id) return false;
  const nowMs = Number(at) || 0;
  const seen = TOAST_SEEN.get(id);
  if (seen && nowMs - seen < TOAST_WINDOW_MS) return false;
  TOAST_SEEN.set(id, nowMs);
  sink(String(text || ''), String(tone || 'info'));
  return true;
}

/** Забыть показанные тосты (тесты и выход со страницы). */
export function resetToastOnce() {
  TOAST_SEEN.clear();
}

/* ─────────────────────────────────────────────── тела действий §1 */

/**
 * «Не актуально»: `{action:"dismiss", target:{kind, id}, reason?}`.
 *
 * @param {'question'|'decision'|'approval'|'step'} kind
 * @param {string} id
 * @param {string} [reason] почему (необязательно)
 */
export function dismissBody(kind, id, reason) {
  const body = { action: 'dismiss', target: { kind: String(kind || ''), id: String(id || '') } };
  const why = String(reason || '').trim();
  if (why) body.reason = why;
  return body;
}

/** «Вернуть»: `{action:"restore", target:{kind, id}}`. */
export function restoreBody(kind, id) {
  return { action: 'restore', target: { kind: String(kind || ''), id: String(id || '') } };
}

/** Массово: «Close all stale» у завершённого запроса — на всю комнату. */
export function dismissStaleBody(scope = 'finished_requests') {
  return { scope: String(scope || 'finished_requests') };
}

/* ───────────────────────────────────────────── счётчики с сервера (§1) */

/**
 * `needs_you_count` запроса как объект чисел; `null`, если сервер его не прислал
 * (старый сервер — тогда вид считает по списку, но честно помечает это).
 *
 * @param {object} detail деталь запроса
 * @returns {{questions:number, decisions:number, approvals:number, steps:number,
 *            total:number, fromServer:boolean}|null}
 */
export function needsYouCount(detail) {
  const raw = detail && detail.needs_you_count;
  if (!raw || typeof raw !== 'object') return null;
  const num = (value) => {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
  };
  const questions = num(raw.questions);
  const decisions = num(raw.decisions);
  const approvals = num(raw.approvals);
  const steps = num(raw.steps);
  const blocked = num(raw.blocked);
  const total = Number.isFinite(Number(raw.total)) && Number(raw.total) >= 0
    ? Math.trunc(Number(raw.total))
    : questions + decisions + approvals + steps + blocked;
  return { questions, decisions, approvals, steps, blocked, total, fromServer: true };
}

/** Запрос уже закончен (массовое закрытие неактуальных имеет смысл). */
export function isFinishedRequest(detail) {
  const status = String((detail && (detail.status || detail.card_state)) || '').trim().toLowerCase();
  if (REQUEST_FINISHED.has(status)) return true;
  return Boolean(detail && (detail.finished_at || detail.archived === true));
}

/**
 * Что у запроса ещё можно закрыть как неактуальное.
 *
 * Считаем пункты, которые ждут человека, а работа уже закончена: массовое
 * действие сервера (`dismiss_stale`) закрывает именно их и не трогает решённое.
 *
 * @param {object} detail
 * @param {{isWaiting?:Function, isOpenQuestion?:Function, isOpenDecision?:Function}} [probe]
 * @returns {{total:number, finished:boolean}}
 */
export function staleSummary(detail, probe = {}) {
  const finished = isFinishedRequest(detail);
  if (!finished) return { total: 0, finished: false };
  const approvals = Array.isArray(detail && detail.approvals) ? detail.approvals : [];
  const questions = Array.isArray(detail && detail.open_questions) ? detail.open_questions : [];
  const decisions = Array.isArray(detail && detail.decisions) ? detail.decisions : [];
  const waitApproval = typeof probe.isWaiting === 'function'
    ? probe.isWaiting : (a) => String((a && a.state) || 'pending') === 'pending';
  const openQuestion = typeof probe.isOpenQuestion === 'function'
    ? probe.isOpenQuestion : (q) => Boolean(q) && !q.answered && !q.dismissed;
  const openDecision = typeof probe.isOpenDecision === 'function'
    ? probe.isOpenDecision : (d) => Boolean(d) && (d.open === true
      || String(d.status || 'proposed') === 'proposed');
  const total = approvals.filter(waitApproval).length
    + questions.filter(openQuestion).length
    + decisions.filter(openDecision).length;
  return { total, finished };
}

export default {
  CLOSED_REASON_RU, NOT_WAITING, REQUEST_FINISHED, closedReasonText, errorCode,
  isAlreadyDecided, isGone, toastOnce, resetToastOnce, dismissBody, restoreBody,
  dismissStaleBody, needsYouCount, isFinishedRequest, staleSummary,
};
