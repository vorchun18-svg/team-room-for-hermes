/**
 * today-model.js — чистые решения экрана «Сегодня» (W24-N3).
 *
 * Здесь нет DOM: всё, что можно посчитать словами и числами, считается здесь и
 * проверяется в `tests/today-ui.test.mjs`. Вид (`today.js`) только раскладывает
 * готовые строки по разметке. Так экран не «думает» дважды и не расходится сам с собой.
 *
 * Контракты данных — `docs/WAVE24-RULES.md` §3.1 (сегодня), §3.2 (дежурства),
 * §3.3 (здоровье). Модель терпима к пропускам: сервер может не отдать раздел —
 * экран покажет пустое состояние, а не упадёт.
 */

import { countNoun } from '../text-ru.js';

/** Порядок и названия разделов «Сегодня» — один источник для вида и тестов. */
export const TODAY_SECTIONS = [
  // W29B-Q8: «Итог дня» — первым: утром и вечером человек смотрит сюда, а не в
  // список текущих ожиданий (он ниже и остаётся на месте).
  { key: 'daily', title: 'Итог дня' },
  { key: 'health', title: 'Здоровье' },
  { key: 'needs_you', title: 'Ждёт вас' },
  { key: 'overdue', title: 'Просрочено' },
  { key: 'due_soon', title: 'Ближайшие 3 дня' },
  { key: 'changed', title: 'Что изменилось с прошлого раза' },
  { key: 'running', title: 'Идёт сейчас' },
  { key: 'duties', title: 'Дежурства' },
];

/** Виды дел «ждёт вас» по-русски (контракт §3.1: kind). */
export const KIND_LABELS = {
  question: 'вопрос',
  decision: 'решение',
  approval: 'согласование',
  step: 'шаг',
  draft: 'черновик',
  failure: 'сбой',
  blocked: 'заблокированная задача',
};

/** Виды проблем дежурства (§3.2: problem.kind). */
export const PROBLEM_LABELS = {
  failed: 'сбой запуска',
  stale: 'давно не запускалось',
  never_ran: 'ни разу не запускалось',
  disabled_but_due: 'выключено, а запуск был нужен',
};

/** Статусы проверок здоровья (§3.3) — от худшего к лучшему. */
export const HEALTH_ORDER = { fail: 0, warn: 1, unknown: 2, ok: 3 };

/** Сколько карточек «ждёт вас» видно сразу (остальное — под кнопкой). */
export const NEEDS_YOU_LIMIT = 7;

/** Склонения для чисел экрана — в одном месте, чтобы подписи не разъезжались. */
export const FORMS = {
  case: ['дело', 'дела', 'дел'],
  day: ['день', 'дня', 'дней'],
  minute: ['минуту', 'минуты', 'минут'],
  hour: ['час', 'часа', 'часов'],
  request: ['задача', 'задачи', 'задач'],
  answer: ['ответ', 'ответа', 'ответов'],
  check: ['проверка', 'проверки', 'проверок'],
  duty: ['дежурство', 'дежурства', 'дежурств'],
};

function list(value) {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === 'object') : [];
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/* ─────────────────────────────────────────────────────────────── время */

/**
 * «как давно»: короткая человеческая подпись без секунд.
 * @param {number|null} since unix
 * @param {number} [now] unix
 * @returns {string} «только что» | «12 минут назад» | «2 часа назад» | «3 дня назад»
 */
export function agoText(since, now = Math.floor(Date.now() / 1000)) {
  const at = num(since);
  if (!at) return '';
  const delta = Math.max(0, num(now) - at);
  if (delta < 60) return 'только что';
  const minutes = Math.floor(delta / 60);
  if (minutes < 60) return countNoun(minutes, FORMS.minute) + ' назад';
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return countNoun(hours, FORMS.hour) + ' назад';
  const days = Math.floor(hours / 24);
  if (days === 1) return 'вчера';
  if (days < 30) return countNoun(days, FORMS.day) + ' назад';
  const months = Math.floor(days / 30);
  return months + ' мес. назад';
}

/** «просрочено на 7 дней» — по полю days_overdue контракта. */
export function overdueText(row) {
  const days = Math.max(1, num(row && row.days_overdue));
  return 'просрочено на ' + countNoun(days, FORMS.day);
}

/** «через 1 день» / «сегодня» — по полю days_left контракта. */
export function dueSoonText(row) {
  const days = num(row && row.days_left);
  if (days <= 0) return 'сегодня последний день';
  return 'через ' + countNoun(days, FORMS.day);
}

/** «11.11» из «2026-11-11» (без года: на экране «сегодня» он лишний). */
export function shortDate(value) {
  const text = String(value || '');
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  return match ? match[3] + '.' + match[2] : text;
}

/* ─────────────────────────────────────────────────────── «ждёт вас» */

/** Подпись вида дела: «вопрос», «сбой». Неизвестный вид — пустая строка. */
export function kindLabel(kind) {
  return KIND_LABELS[String(kind || '')] || '';
}

/**
 * Порядок карточек: сначала срочные, затем по времени ожидания (сверху — самые
 * давние). Сервер уже сортирует так, но экран не должен зависеть от этого.
 */
export function sortNeedsYou(items) {
  return list(items).slice().sort((a, b) => {
    const ua = a.urgent === true ? 0 : 1;
    const ub = b.urgent === true ? 0 : 1;
    if (ua !== ub) return ua - ub;
    return num(a.since) - num(b.since);
  });
}

/**
 * Сколько карточек показать сразу и сколько осталось.
 * @param {Array} items
 * @param {number} [limit]
 * @returns {{visible: Array, rest: Array, total: number}}
 */
export function needsYouVisible(items, limit = NEEDS_YOU_LIMIT) {
  const sorted = sortNeedsYou(items);
  const cap = Math.max(1, num(limit) || NEEDS_YOU_LIMIT);
  return { visible: sorted.slice(0, cap), rest: sorted.slice(cap), total: sorted.length };
}

/** Карточка «ждёт вас» — готовые строки для разметки. */
export function needsYouCard(item, now = Math.floor(Date.now() / 1000)) {
  const it = item || {};
  return {
    room: String(it.room || ''),
    roomTitle: String(it.room_title || it.room || ''),
    requestId: String(it.request_id || ''),
    requestTitle: String(it.request_title || ''),
    kind: String(it.kind || ''),
    kindLabel: kindLabel(it.kind),
    text: String(it.text || ''),
    ago: agoText(it.since, now),
    urgent: it.urgent === true,
    recommended: it.recommended ? String(it.recommended) : '',
  };
}

/* ───────────────────────────────────────────────────── просрочка/сроки */

/** Что можно сделать со шагом прямо из строки (§1 W27: шаги, четыре действия). */
export const STEP_ACTIONS = ['done', 'postpone', 'dismiss', 'assign'];

/** Подписи быстрых действий — один словарь для строки и режима «по очереди». */
export const STEP_ACTION_LABELS = {
  done: 'Готово',
  postpone: 'Перенести срок…',
  dismiss: 'Не актуально',
  assign: 'Поручить…',
  open: 'Открыть',
};

/**
 * Адрес шага: страница запроса с якорем шага (`?step=ns_…`).
 * Без запроса — вкладка «От вас нужно» комнаты: переход есть всегда.
 * @param {{room?:string, requestId?:string, stepId?:string}} ref
 * @returns {string} хэш или '' (нет комнаты — вести некуда)
 */
export function stepHash(ref) {
  const it = ref || {};
  const room = String(it.room || '');
  const rid = String(it.requestId || '');
  const sid = String(it.stepId || '');
  if (!room) return '';
  const base = rid
    ? '#/room/' + encodeURIComponent(room) + '/request/' + encodeURIComponent(rid)
    : '#/room/' + encodeURIComponent(room) + '/backlog';
  return sid && rid ? base + '?step=' + encodeURIComponent(sid) : base;
}

/** `t_x:ns_1` → `ns_1` (id шага в плане запроса); `ns_1` остаётся собой. */
export function localStepId(itemId) {
  const text = String(itemId || '');
  const at = text.indexOf(':');
  return at >= 0 ? text.slice(at + 1) : text;
}

/**
 * Строка «просрочено» или «ближайшие 3 дня» — со всем, что нужно строке:
 * переход, действия, id шага в плане.
 *
 * W27-E3: раньше это был «постер» (текст без действий). Теперь у каждой строки
 * есть адрес (`href`) и набор действий (`actions`), а «закрываемость» (`actionable`)
 * решает, идёт ли строка в счётчик «просрочено».
 *
 * @param {object} row элемент overdue[] / due_soon[]
 * @param {'overdue'|'soon'} kind
 * @returns {{text, owner, when, due, dueRaw, room, roomTitle, requestId, requestTitle,
 *            itemId, stepId, href, actionable, actions, tone}}
 */
export function dueRow(row, kind) {
  const it = row || {};
  const overdue = kind === 'overdue';
  const itemId = String(it.item_id || '');
  const requestId = String(it.request_id || '');
  // Закрыть пункт можно там, где он живёт: шаг бэклога (`<rid>:ns_1`) знает
  // свой статус и срок; строка без id — только показать.
  const actionable = it.actionable === false ? false : Boolean(itemId);
  const room = String(it.room || '');
  const stepId = localStepId(itemId);
  return {
    text: String(it.text || ''),
    owner: String(it.owner || ''),
    when: overdue ? overdueText(it) : dueSoonText(it),
    due: shortDate(it.due),
    dueRaw: String(it.due || ''),
    room,
    roomTitle: String(it.room_title || it.room || ''),
    requestId,
    requestTitle: String(it.request_title || ''),
    itemId,
    stepId,
    href: stepHash({ room, requestId, stepId }),
    actionable,
    actions: actionable ? STEP_ACTIONS.slice() : [],
    tone: overdue ? 'danger' : 'warn',
  };
}

/** Сколько строк ещё можно закрыть (счётчик «просрочено» — только по ним). */
export function actionableCount(rows) {
  return list(rows).filter((row) => (row || {}).actionable !== false && (row || {}).item_id).length;
}

/** `YYYY-MM-DD` из Date (местная дата, без времени). */
export function isoDate(d) {
  const date = d instanceof Date ? d : new Date(d === undefined ? Date.now() : d);
  const pad = (n) => String(n).padStart(2, '0');
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
}

/** `YYYY-MM-DD` + N дней (в календарных сутках, без часовых поясов). */
export function addDays(iso, days) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  const base = m
    ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
    : new Date(new Date().getFullYear(), new Date().getMonth(), new Date().getDate());
  base.setDate(base.getDate() + Number(days || 0));
  return isoDate(base);
}

/** «19.11» — срок в подписи кнопки (год на экране «сегодня» лишний). */
function dueChip(iso) {
  return shortDate(iso);
}

/**
 * Варианты «Перенести срок…»: +1 день, пятница, своя дата.
 *
 * Срок не назначаем в прошлое: считаем от «сегодня» или от текущего срока —
 * что позже. Пятница — ближайшая будущая (если сегодня пятница, следующая).
 *
 * @param {string} [dueIso] текущий срок строки `YYYY-MM-DD`
 * @param {Date|number} [now]
 * @returns {Array<{key:string, label:string, due:string}>}
 */
export function postponeOptions(dueIso, now) {
  const date = now instanceof Date ? now : new Date(now === undefined ? Date.now() : now);
  const today = isoDate(date);
  const start = /^\d{4}-\d{2}-\d{2}$/.test(String(dueIso || '')) && String(dueIso) > today
    ? String(dueIso) : today;
  const out = [];
  const plus1 = addDays(start, 1);
  out.push({ key: 'plus1', label: '+1 день — ' + dueChip(plus1), due: plus1 });
  // Пятница: 5 = пятница; 0 — воскресенье.
  const weekday = new Date(start + 'T00:00:00').getDay();
  let shift = (5 - weekday + 7) % 7;
  if (shift === 0) shift = 7;
  const friday = addDays(start, shift);
  if (friday !== plus1) out.push({ key: 'friday', label: 'пятница — ' + dueChip(friday), due: friday });
  return out;
}

/**
 * Очередь «Разобрать по очереди»: просроченное, затем ближайшее; только то,
 * что можно закрыть (строка без действия в очередь не попадает — незачем).
 * @param {object} data ответ /api/today
 * @returns {Array<object>} строки `dueRow`
 */
export function stepQueue(data) {
  const d = data || {};
  const rows = [];
  for (const row of list(d.overdue)) rows.push(dueRow(row, 'overdue'));
  for (const row of list(d.due_soon)) rows.push(dueRow(row, 'soon'));
  return rows.filter((row) => row.actionable && row.href);
}

/* ───────────────────────────────────────────────────────── здоровье */

/**
 * Что показывать в плашке здоровья: только warn/fail (контракт: плашка видна,
 * когда есть проблемы). unknown — не проблема, но и не «всё хорошо».
 *
 * @param {object} health ответ /api/health/integrations
 * @returns {{shown: boolean, tone: 'fail'|'warn'|'', items: Array, total: number, title: string}}
 */
export function healthProblems(health) {
  const checks = list(health && health.checks);
  const items = checks
    .filter((c) => c.status === 'fail' || c.status === 'warn')
    .sort((a, b) => (HEALTH_ORDER[a.status] ?? 9) - (HEALTH_ORDER[b.status] ?? 9))
    .map((c) => ({
      id: String(c.id || ''),
      title: String(c.title || c.id || ''),
      status: String(c.status || 'unknown'),
      detail: String(c.detail || ''),
      hint: c.hint ? String(c.hint) : '',
      since: c.since || null,
    }));
  const fails = items.filter((c) => c.status === 'fail').length;
  const tone = fails > 0 ? 'fail' : (items.length ? 'warn' : '');
  let title = '';
  if (fails > 0) title = fails === 1 ? 'Одна проверка не проходит' : 'Не проходят: ' + countNoun(fails, FORMS.check);
  else if (items.length) title = items.length === 1 ? 'Одно замечание' : 'Замечания: ' + countNoun(items.length, FORMS.check);
  return { shown: items.length > 0, tone, items, total: items.length, title };
}

/* ─────────────────────────────────────────────────────── «изменилось» */

/**
 * Свод «что изменилось с прошлого раза»: завершённые запросы, новые ответы,
 * сбои. Число в заголовке секции считается здесь.
 * @param {object} data ответ /api/today
 * @returns {{finished: Array, failures: Array, newRequests: number, newAnswers: number, total: number}}
 */
export function changedSummary(data) {
  const changed = (data && data.changed) || {};
  const finished = list(changed.finished).map((it) => ({
    room: String(it.room || ''),
    roomTitle: String(it.room_title || it.room || ''),
    requestId: String(it.request_id || ''),
    requestTitle: String(it.request_title || ''),
    at: it.at || null,
  }));
  const failures = list(changed.failures).map((it) => ({
    room: String(it.room || ''),
    roomTitle: String(it.room_title || it.room || ''),
    requestId: String(it.request_id || ''),
    requestTitle: String(it.request_title || ''),
    at: it.at || null,
    reason: String(it.reason || ''),
  }));
  const newRequests = num(changed.new_requests);
  const newAnswers = num(changed.new_answers);
  return {
    finished,
    failures,
    newRequests,
    newAnswers,
    total: finished.length + failures.length + newRequests + newAnswers,
  };
}

/** «Идёт сейчас»: строка с числом участников. */
export function runningRow(item, now = Math.floor(Date.now() / 1000)) {
  const it = item || {};
  const members = Math.max(1, num(it.members_working));
  return {
    roomTitle: String(it.room_title || it.room || ''),
    requestId: String(it.request_id || ''),
    requestTitle: String(it.request_title || ''),
    members,
    membersText: countNoun(members, ['участник', 'участника', 'участников']) + ' работает',
    ago: agoText(it.since, now),
  };
}

/* ───────────────────────────────────────────────────────── дежурства */

/**
 * Состояние дежурства для значка: проблема важнее статуса запуска.
 * @param {object} item элемент /api/duties
 * @returns {'ok'|'warn'|'fail'|'muted'}
 */
export function dutyTone(item) {
  const it = item || {};
  const run = it.last_run || null;
  if (it.problem && it.problem.kind) {
    return it.problem.kind === 'failed' ? 'fail' : 'warn';
  }
  if (!run) return 'muted';
  if (run.status === 'failed') return 'fail';
  if (run.status === 'missed' || run.status === 'unknown') return 'warn';
  if (run.status === 'running') return 'info';
  if (it.enabled === false) return 'muted';
  return 'ok';
}

/** Подпись последнего запуска: «сегодня, 5 минут назад» → просто «5 минут назад». */
export function dutyRunText(item, now = Math.floor(Date.now() / 1000)) {
  const run = (item && item.last_run) || null;
  if (!run) return 'не запускалось';
  if (run.status === 'running') return 'выполняется сейчас';
  const stamp = run.finished_at || run.started_at || run.scheduled_for;
  const ago = agoText(stamp, now);
  if (!ago) return 'не запускалось';
  if (run.status === 'failed') return 'сбой ' + ago;
  if (run.status === 'missed') return 'пропущено ' + ago;
  return 'запуск ' + ago;
}

/** Проблема дежурства одной строкой; пусто — проблем нет. */
export function dutyProblemText(item) {
  const problem = item && item.problem;
  if (!problem || !problem.kind) return '';
  const label = PROBLEM_LABELS[String(problem.kind)] || '';
  const text = String(problem.text || '');
  return label ? (text ? label + ': ' + text : label) : text;
}

/** Сколько дежурств с проблемой (для заголовка секции). */
export function dutiesProblemCount(items) {
  return list(items).filter((it) => it.problem && it.problem.kind).length;
}

/* ─────────────────────────────────────────────────── общий счётчик */

/**
 * Строка счётчиков под заголовком: только непустые, чтобы не было «0 · 0 · 0».
 * @param {object} data ответ /api/today
 * @returns {Array<{key:string, text:string, tone:string}>}
 */
export function counterChips(data) {
  const c = (data && data.counters) || {};
  const chips = [];
  const needs = num(c.needs_you);
  const overdue = num(c.overdue);
  const soon = num(c.due_soon);
  const running = num(c.running);
  if (needs > 0) chips.push({ key: 'needs_you', text: 'ждёт вас: ' + needs, tone: 'warn' });
  if (overdue > 0) chips.push({ key: 'overdue', text: 'просрочено: ' + overdue, tone: 'danger' });
  if (soon > 0) chips.push({ key: 'due_soon', text: 'срок близко: ' + soon, tone: 'info' });
  if (running > 0) chips.push({ key: 'running', text: 'идёт: ' + running, tone: 'ok' });
  return chips;
}

/** Пустые состояния — дружелюбные, без канцелярита. */
export const EMPTY_TEXT = {
  needs_you: 'Всё спокойно: ждать от вас нечего',
  overdue: 'Просроченного нет',
  due_soon: 'В ближайшие три дня сроков нет',
  changed: 'С прошлого раза ничего не изменилось',
  running: 'Сейчас никто не работает',
  duties: 'Дежурств нет',
};

/** Есть ли на экране хоть что-то, кроме пустых состояний. */
export function isEmptyDay(data) {
  const d = data || {};
  return !list(d.needs_you).length && !list(d.overdue).length && !list(d.due_soon).length
    && !list(d.running).length && changedSummary(d).total === 0;
}

export default {
  TODAY_SECTIONS, KIND_LABELS, PROBLEM_LABELS, EMPTY_TEXT, NEEDS_YOU_LIMIT,
  STEP_ACTIONS, STEP_ACTION_LABELS,
  agoText, overdueText, dueSoonText, shortDate, kindLabel, sortNeedsYou, needsYouVisible,
  needsYouCard, dueRow, stepHash, localStepId, actionableCount, isoDate, addDays,
  postponeOptions, stepQueue, healthProblems, changedSummary, runningRow, dutyTone, dutyRunText,
  dutyProblemText, dutiesProblemCount, counterChips, isEmptyDay,
};
