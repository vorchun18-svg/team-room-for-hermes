/**
 * backlog-model.js — чистая логика вкладок «Дальше» и «Журнал».
 *
 * Здесь нет DOM и fetch: только сортировки, группировки и фильтры,
 * поэтому модуль целиком покрывается node --test (tests/backlog-model.test.mjs).
 * Даты — локальные календарные дни `YYYY-MM-DD`; epoch-секунды API
 * переводятся в локальный день, чтобы «сегодня» не съезжало в UTC.
 */

/* ------------------------------------------------------------------ даты */

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

const DAY_MS = 86400000;

/** Local Date → 'YYYY-MM-DD' (без UTC-сдвига). */
function dateKey(d) {
  const y = d.getFullYear();
  const m = d.getMonth() + 1;
  const day = d.getDate();
  return `${y}-${m < 10 ? '0' + m : m}-${day < 10 ? '0' + day : day}`;
}

/** 'YYYY-MM-DD' | Date | epoch(s|ms) → local Date в полночь, либо null. */
export function parseDay(value) {
  if (value === null || value === undefined || value === '') return null;
  if (value instanceof Date) {
    return isNaN(value.getTime()) ? null : new Date(value.getFullYear(), value.getMonth(), value.getDate());
  }
  const s = String(value).trim();
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (m) {
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    return isNaN(d.getTime()) ? null : d;
  }
  if (/^-?\d+(\.\d+)?$/.test(s)) {
    const n = Number(s);
    const d = new Date(Math.abs(n) >= 1e11 ? n : n * 1000);
    return isNaN(d.getTime()) ? null : new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }
  return null;
}

/** Нормализовать дату в 'YYYY-MM-DD' (локально) или null. */
export function isoDay(value) {
  const d = parseDay(value);
  return d ? dateKey(d) : null;
}

/** Сдвинуть календарный день на n суток. */
export function addDays(day, n) {
  const d = parseDay(day);
  if (!d) return null;
  d.setDate(d.getDate() + Number(n || 0));
  return dateKey(d);
}

/** a − b в целых днях (a позже → > 0). */
export function dayDiff(a, b) {
  const da = parseDay(a);
  const db = parseDay(b);
  if (!da || !db) return null;
  const utc = (x) => Date.UTC(x.getFullYear(), x.getMonth(), x.getDate());
  return Math.round((utc(da) - utc(db)) / DAY_MS);
}

/** '28 окт' */
export function shortDay(day) {
  const d = parseDay(day);
  return d ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : '';
}

/** '28 окт, ср' */
export function dayWithWeekday(day) {
  const d = parseDay(day);
  return d ? `${shortDay(day)}, ${WEEKDAYS[d.getDay()]}` : '';
}

/** «Сегодня» / «Завтра» / «Вчера» / «28 окт, ср». */
export function dayTitle(day, today) {
  const diff = dayDiff(day, today);
  if (diff === null) return '';
  if (diff === 0) return 'Сегодня';
  if (diff === 1) return 'Завтра';
  if (diff === -1) return 'Вчера';
  return dayWithWeekday(day);
}

/** Склонение: 1 день / 2 дня / 5 дней. */
export function pluralDays(n) {
  const abs = Math.abs(Number(n) || 0);
  const mod100 = abs % 100;
  const mod10 = abs % 10;
  if (mod100 >= 11 && mod100 <= 14) return 'дней';
  if (mod10 === 1) return 'день';
  if (mod10 >= 2 && mod10 <= 4) return 'дня';
  return 'дней';
}

/**
 * Состояние срока. `days` — разница с сегодняшним днём (отрицательная — просрочка).
 * @returns {{state:'none'|'overdue'|'today'|'tomorrow'|'soon'|'later', days:number|null, label:string}}
 */
export function dueInfo(due, today) {
  if (!due) return { state: 'none', days: null, label: 'Без срока' };
  const diff = dayDiff(due, today);
  if (diff === null) return { state: 'none', days: null, label: 'Без срока' };
  if (diff < 0) {
    const n = -diff;
    return { state: 'overdue', days: diff, label: `Просрочено на ${n} ${pluralDays(n)}` };
  }
  if (diff === 0) return { state: 'today', days: 0, label: 'Сегодня' };
  if (diff === 1) return { state: 'tomorrow', days: 1, label: 'Завтра' };
  if (diff <= 7) return { state: 'soon', days: diff, label: `Через ${diff} ${pluralDays(diff)}` };
  return { state: 'later', days: diff, label: shortDay(due) };
}

/* --------------------------------------------------------------- элементы */

export const KIND_LABELS = {
  next_step: 'Шаг',
  future_question: 'Будущий вопрос',
  open_question: 'Вопрос',
  decision: 'Решение',
};

export const STATUS_LABELS = {
  open: 'Открыт',
  asked: 'Задан',
  done: 'Готово',
  dismissed: 'Убран',
};

/** Ярлык вида элемента (русский). */
export function kindLabel(kind) {
  return KIND_LABELS[kind] || 'Элемент';
}

/** Ярлык статуса (русский). */
export function statusLabel(status) {
  return STATUS_LABELS[status] || String(status || '');
}

/** Элемент ещё в работе (не закрыт). */
export function isActive(item) {
  return !!item && (item.status === 'open' || item.status === 'asked');
}

/** Элемент закрыт (done/dismissed). */
export function isClosed(item) {
  return !!item && (item.status === 'done' || item.status === 'dismissed');
}

/** Стабильная сортировка по сроку: без срока — в конце, затем по created_at. */
export function sortByDue(items) {
  return [...(items || [])].sort((a, b) => {
    const da = a && a.due ? String(a.due) : '';
    const db = b && b.due ? String(b.due) : '';
    if (da && !db) return -1;
    if (!da && db) return 1;
    if (da !== db) return da < db ? -1 : 1;
    return (Number(a.created_at) || 0) - (Number(b.created_at) || 0);
  });
}


/* ------------------------------------------------- вопросы: состояние и близнецы */

/**
 * Нормализованный ключ текста вопроса: регистр, пробелы, знаки, служебные
 * префиксы («Не к спеху:») и скобочные уточнения не считаются разницей.
 * Тем же правилом сервер склеивает близнецов (B-7), поэтому ответ на любой
 * id закрывает всю пару и здесь.
 * @param {*} text
 * @returns {string}
 */
export function questionTextKey(text) {
  let s = String(text === null || text === undefined ? '' : text).toLowerCase();
  s = s.replace(/\s+/g, ' ').trim();
  s = s.replace(/^не к спеху[:.,]?\s*/i, '');
  s = s.replace(/\([^)]*\)/g, ' ').replace(/\[[^\]]*\]/g, ' ');
  s = s.replace(/[«»"'`.,;:!?…—–-]+/g, ' ');
  return s.replace(/\s+/g, ' ').trim();
}

/**
 * Служебный вопрос руководителя («Сколько раундов/прогонов критика?») — не
 * показывается человеку (B-7). Правило то же, что у сервера.
 * @param {*} text
 * @returns {boolean}
 */
export function isServiceQuestion(text) {
  const key = questionTextKey(text);
  if (!key) return false;
  if (/критик/.test(key) && /(раунд|прогон|цикл)/.test(key)) return true;
  return /сколько/.test(key) && /(провер|раунд|прогон)/.test(key);
}

/** id-близнецы вопроса (могут прийти с сервера или быть вычислены по тексту). */
export function questionIds(item) {
  const own = String((item && item.id) || '');
  const extra = Array.isArray(item && item.aliases) ? item.aliases : [];
  return [own, ...extra.map((v) => String(v || ''))].filter(Boolean);
}

/** Состояние вопроса по полям API (без собственных догадок на клиенте). */
export function questionStateOf(item) {
  const state = String((item && (item.answer_state || item.state)) || '').trim().toLowerCase();
  const answered = Boolean(item && (item.answered === true
    || String(item.status || '') === 'done'
    || (item.answer !== undefined && item.answer !== null && String(item.answer).trim())
    || ['applied', 'answered', 'sent', 'replied', 'done'].includes(state)));
  if (answered) {
    return { state: 'answered', label: String((item && item.answer_label) || 'отвечен') };
  }
  if (state === 'waiting_explanation' || state === 'explanation' || (item && item.needs_explanation === true)) {
    return { state: 'explanation', label: String((item && item.state_label) || 'ждёт пояснения') };
  }
  return { state: 'open', label: String((item && item.state_label) || 'ждёт вашего ответа') };
}

/**
 * Схлопнуть близнецов: один вопрос = одна карточка (B-7).
 * Представителем становится уже закрытый/отвеченный вариант — иначе пара
 * «отвечен + открыт» осталась бы в списке.
 * @param {object[]} items
 * @returns {object[]}
 */
export function dedupeByText(items) {
  const list = Array.isArray(items) ? items.filter((i) => i && typeof i === 'object') : [];
  const groups = new Map();
  for (const item of list) {
    const key = questionTextKey(item.text) || String(item.id || '');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  const out = [];
  for (const group of groups.values()) {
    if (group.length === 1) { out.push(group[0]); continue; }
    const rep = group.find((i) => questionStateOf(i).state !== 'open') || group[0];
    const ids = [];
    for (const item of group) {
      for (const id of questionIds(item)) if (id !== rep.id && !ids.includes(id)) ids.push(id);
    }
    out.push(Object.assign({}, rep, { aliases: ids.reverse() }));
  }
  return out;
}

/**
 * Вопросы для показа: без служебных и без близнецов.
 * @param {object[]} items
 * @returns {object[]}
 */
export function visibleQuestions(items) {
  const list = (Array.isArray(items) ? items : []).filter((i) => i && !isServiceQuestion(i.text));
  return dedupeByText(list);
}

/*
 * Разложить плоский список `items` бэклога на блоки экрана.
 * @param {Array} items BacklogItem[]
 */
export function partitionBacklog(items) {
  const list = Array.isArray(items) ? items : [];
  return {
    // B-7: служебные вопросы скрыты, близнецы схлопнуты — один вопрос = одна строка.
    questions: sortByDue(visibleQuestions(list.filter((i) => i && i.kind === 'open_question' && i.status === 'open'))),
    decisions: sortByDue(list.filter((i) => i && i.kind === 'decision' && i.status === 'open')),
    steps: sortByDue(list.filter((i) => i && i.kind === 'next_step' && isActive(i))),
    future: sortByDue(dedupeByText(list.filter((i) => i && i.kind === 'future_question' && isActive(i)))),
    closed: sortByDue(list.filter(isClosed)),
  };
}

/** Сколько строк в «Ждут вашего решения» с учётом blocked-карточек. */
export function inboxCount({ questions = [], decisions = [], blocked = [] } = {}) {
  return questions.length + decisions.length + blocked.length;
}

/**
 * Расшифровка числа дел: «7 вопросов · 1 блокировка · 1 решение».
 * Одно число и одна расшифровка для блока, бейджа вкладки и сайдбара (D4).
 * @param {{questions?:Array, decisions?:Array, blocked?:Array}} parts
 * @returns {{total:number, questions:number, blocked:number, decisions:number, text:string, full:string}}
 */
export function inboxSummary({ questions = [], decisions = [], blocked = [] } = {}) {
  const q = Array.isArray(questions) ? questions.length : 0;
  const b = Array.isArray(blocked) ? blocked.length : 0;
  const d = Array.isArray(decisions) ? decisions.length : 0;
  const total = q + b + d;
  const nouns = (n, forms) => n + ' ' + pluralWord(n, forms);
  const parts = [];
  if (q) parts.push(nouns(q, ['вопрос', 'вопроса', 'вопросов']));
  if (b) parts.push(nouns(b, ['блокировка', 'блокировки', 'блокировок']));
  if (d) parts.push(nouns(d, ['решение', 'решения', 'решений']));
  return {
    total,
    questions: q,
    blocked: b,
    decisions: d,
    text: parts.join(' · '),
    full: parts.length ? nouns(total, ['дело', 'дела', 'дел']) + ': ' + parts.join(' · ') : '',
  };
}

/** Склонение внутри модели (без импорта util: модуль остаётся чистым). */
function pluralWord(n, forms) {
  const num = Math.abs(Number(n) || 0);
  const a = forms && forms[0] ? forms[0] : '';
  const b = forms && forms[1] ? forms[1] : a;
  const c = forms && forms[2] ? forms[2] : b;
  const tail100 = num % 100;
  const tail10 = num % 10;
  if (tail100 >= 11 && tail100 <= 14) return c;
  if (tail10 === 1) return a;
  if (tail10 >= 2 && tail10 <= 4) return b;
  return c;
}

/* --------------------------------------------------------- blocked-карточки */

/**
 * Достать заблокированные карточки из деталей запросов.
 * @param {Array} details RequestDetail[]
 * @returns {Array<{task_id,text,hint,request_id,request_title,assignee,since}>}
 */
export function blockedCards(details) {
  const out = [];
  const seen = new Set();
  for (const detail of Array.isArray(details) ? details : []) {
    if (!detail || !Array.isArray(detail.tree)) continue;
    for (const node of detail.tree) {
      if (!node || node.status !== 'blocked' || seen.has(node.id)) continue;
      seen.add(node.id);
      out.push({
        task_id: node.id,
        text: String(node.title || 'Заблокированная задача'),
        hint: String(node.summary_md || '').trim(),
        request_id: detail.id || null,
        request_title: String(detail.title || ''),
        assignee: node.assignee || null,
        since: node.started_at || null,
      });
    }
  }
  return out.sort((a, b) => (Number(a.since) || 0) - (Number(b.since) || 0));
}

/** id вопросов внутри запроса: 'q_1' (для actions.answer). */
export function backlogQuestionId(itemId) {
  const s = String(itemId || '');
  const idx = s.lastIndexOf(':');
  const id = idx >= 0 ? s.slice(idx + 1) : s;
  return /^q_/.test(id) ? id : null;
}

/* ----------------------------------------------------------- следующие шаги */

/** id запроса, к которому относится пункт бэклога ('' — нет источника). */
export function itemRequestId(item) {
  return String((item && item.source_request) || '').trim();
}

/** Только дела одного запроса — фильтр `?request=<root_id>` вкладки «Дальше». */
export function filterByRequest(items, requestId) {
  const list = Array.isArray(items) ? items : [];
  const rid = String(requestId || '').trim();
  if (!rid) return list.slice();
  return list.filter((i) => itemRequestId(i) === rid);
}

/** Подпись чипа срока: «Срок» / «Сегодня» / «Просрочено» / «28 окт». */
export function dueChipLabel(due, today) {
  const info = dueInfo(due, today);
  if (info.state === 'none') return 'Срок';
  if (info.state === 'overdue') return 'Просрочено';
  return info.label;
}

export const REQUEST_FILTER_ALL = 'all';
export const REQUEST_FILTER_ONE = 'request';

/** Текущий режим переключателя «Все · Только этот запрос». */
export function requestFilterMode(requestId) {
  return String(requestId || '').trim() ? REQUEST_FILTER_ONE : REQUEST_FILTER_ALL;
}

/** Хэш с параметром `request` (пустой id — параметр убирается). */
export function setRequestParam(hash, requestId) {
  const raw = String(hash || '');
  const cut = raw.indexOf('?');
  const path = cut >= 0 ? raw.slice(0, cut) : raw;
  const query = new URLSearchParams(cut >= 0 ? raw.slice(cut + 1) : '');
  const rid = String(requestId || '').trim();
  if (rid) query.set('request', rid);
  else query.delete('request');
  const rest = query.toString();
  return rest ? `${path}?${rest}` : path;
}

export const STEP_GROUP_MODES = [
  { value: 'source', label: 'По источнику' },
  { value: 'due', label: 'По сроку' },
];

/**
 * Группировка шагов по источнику-запросу.
 * @returns {Array<{key,title,request_id,items,due}>}
 */
export function groupStepsBySource(steps) {
  const map = new Map();
  for (const step of sortByDue(steps)) {
    const key = step.source_request || 'other';
    if (!map.has(key)) {
      map.set(key, {
        key,
        title: String(step.source_title || step.source_request || 'Без источника'),
        request_id: step.source_request || null,
        items: [],
        due: null,
      });
    }
    const group = map.get(key);
    group.items.push(step);
    if (step.due && (!group.due || step.due < group.due)) group.due = step.due;
  }
  return [...map.values()].sort((a, b) => {
    if (a.due && !b.due) return -1;
    if (!a.due && b.due) return 1;
    if (a.due !== b.due) return a.due < b.due ? -1 : 1;
    return a.title.localeCompare(b.title, 'ru');
  });
}

const DUE_BUCKETS = [
  { key: 'overdue', title: 'Просрочено', tone: 'danger' },
  { key: 'today', title: 'Сегодня', tone: 'warn' },
  { key: 'tomorrow', title: 'Завтра', tone: 'info' },
  { key: 'week', title: 'На этой неделе', tone: 'muted' },
  { key: 'later', title: 'Позже', tone: 'muted' },
  { key: 'none', title: 'Без срока', tone: 'muted' },
];

/** Группировка шагов по сроку (только непустые группы, в фиксированном порядке). */
export function groupStepsByDue(steps, today) {
  const buckets = new Map();
  for (const step of sortByDue(steps)) {
    const info = dueInfo(step.due, today);
    let key = 'later';
    if (info.state === 'overdue') key = 'overdue';
    else if (info.state === 'today') key = 'today';
    else if (info.state === 'tomorrow') key = 'tomorrow';
    else if (info.state === 'soon') key = 'week';
    else if (info.state === 'none') key = 'none';
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(step);
  }
  return DUE_BUCKETS.filter((b) => buckets.has(b.key))
    .map((b) => ({ ...b, items: buckets.get(b.key) }));
}

/** Универсальная группировка шагов для переключателя. */
export function groupSteps(steps, mode, today) {
  return mode === 'due' ? groupStepsByDue(steps, today) : groupStepsBySource(steps);
}

/* ------------------------------------------------------------------ повестка */

/**
 * Повестка: просроченные + ближайшие `days` дней, группы по датам.
 * @returns {Array<{date,title,overdue,items}>}
 */
export function buildAgenda(items, today, days = 14) {
  const base = isoDay(today);
  if (!base) return [];
  const horizon = addDays(base, Math.max(1, Number(days) || 14) - 1);
  const groups = new Map();
  for (const item of items || []) {
    if (!item || item.kind !== 'next_step' || !isActive(item) || !item.due) continue;
    const day = isoDay(item.due);
    if (!day) continue;
    const overdue = dayDiff(day, base) < 0;
    if (!overdue && day > horizon) continue;
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(item);
  }
  return [...groups.keys()]
    .sort()
    .map((day) => ({
      date: day,
      title: dayTitle(day, base),
      overdue: dayDiff(day, base) < 0,
      items: sortByDue(groups.get(day)),
    }));
}

/** Сколько дней в повестке (для подписи). */
export function agendaRange(days = 14) {
  return `Ближайшие ${days} ${pluralDays(days)}`;
}

/* -------------------------------------------------------------------- журнал */

const JOURNAL_KINDS = {
  request_created: { icon: 'comment-discussion', tone: 'info', label: 'Запрос создан', group: 'requests' },
  request_status: { icon: 'pulse', tone: 'muted', label: 'Статус запроса', group: 'requests' },
  request_done: { icon: 'pass-filled', tone: 'ok', label: 'Запрос завершён', group: 'requests' },
  step_added: { icon: 'checklist', tone: 'muted', label: 'Шаг добавлен', group: 'steps' },
  step_done: { icon: 'check', tone: 'ok', label: 'Шаг выполнен', group: 'steps' },
  question_open: { icon: 'comment-draft', tone: 'warn', label: 'Вопрос', group: 'questions' },
  question_answered: { icon: 'comment', tone: 'ok', label: 'Ответ на вопрос', group: 'questions' },
  decision_proposed: { icon: 'lightbulb', tone: 'info', label: 'Предложено решение', group: 'decisions' },
  decision_accepted: { icon: 'verified', tone: 'ok', label: 'Решение принято', group: 'decisions' },
  report_added: { icon: 'report', tone: 'info', label: 'Отчёт', group: 'steps' },
  artifact_added: { icon: 'file', tone: 'muted', label: 'Артефакт', group: 'steps' },
  member_added: { icon: 'person-add', tone: 'muted', label: 'Участник добавлен', group: 'members' },
  member_removed: { icon: 'person', tone: 'muted', label: 'Участник убран', group: 'members' },
  room_created: { icon: 'home', tone: 'info', label: 'Комната создана', group: 'members' },
  room_archived: { icon: 'archive', tone: 'muted', label: 'Комната в архиве', group: 'members' },
  ingest: { icon: 'sync', tone: 'muted', label: 'Обработка', group: 'requests' },
  error: { icon: 'error', tone: 'danger', label: 'Ошибка', group: 'requests' },
  critic_review: { icon: 'shield', tone: 'info', label: 'Проверка критика', group: 'requests' },
  critic_verdict: { icon: 'verified', tone: 'info', label: 'Вердикт критика', group: 'requests' },
  revision_created: { icon: 'debug-restart', tone: 'info', label: 'Доработка ответа', group: 'requests' },
};

const JOURNAL_FALLBACK = { icon: 'symbol-event', tone: 'muted', label: 'Событие', group: 'requests' };

/** Короткие имена, которыми бэкенд может называть те же события. */
const JOURNAL_ALIASES = {
  created: 'request_created',
  completed: 'request_done',
  critic: 'critic_verdict',
  revision: 'revision_created',
  ingested: 'ingest',
  decision: 'decision_proposed',
  question: 'question_open',
};

/** Иконка/тон/подпись события журнала. */
export function journalKindInfo(kind) {
  const key = String(kind || '');
  return JOURNAL_KINDS[key] || JOURNAL_KINDS[JOURNAL_ALIASES[key]] || JOURNAL_FALLBACK;
}

export const JOURNAL_FILTERS = [
  { value: 'all', label: 'Всё' },
  { value: 'requests', label: 'Запросы' },
  { value: 'questions', label: 'Вопросы' },
  { value: 'decisions', label: 'Решения' },
  { value: 'steps', label: 'Шаги' },
];

/** Относится ли событие к выбранному типу. */
export function journalMatchesType(event, type) {
  if (!type || type === 'all') return true;
  return journalKindInfo(event && event.kind).group === type;
}

/** Поиск по тексту и автору (регистронезависимо, без учёта лишних пробелов). */
export function journalMatchesQuery(event, query) {
  const q = String(query || '').trim().toLocaleLowerCase('ru');
  if (!q) return true;
  const hay = `${event && event.text ? event.text : ''} ${event && event.actor ? event.actor : ''}`.toLocaleLowerCase('ru');
  return hay.includes(q);
}

/** Отфильтровать журнал по типу и поисковой строке. */
export function filterJournal(items, { type = 'all', q = '' } = {}) {
  return (Array.isArray(items) ? items : []).filter(
    (e) => journalMatchesType(e, type) && journalMatchesQuery(e, q),
  );
}

/** Локальный ключ дня события (epoch секунды). */
export function journalDayKey(at) {
  return isoDay(at);
}

/** Время события 'ЧЧ:ММ'. */
export function journalTime(at) {
  const d = parseDay(at);
  if (!d) return '';
  const raw = typeof at === 'number' && Math.abs(at) < 1e11 ? new Date(at * 1000) : new Date(at);
  if (isNaN(raw.getTime())) return '';
  const h = raw.getHours();
  const m = raw.getMinutes();
  return `${h < 10 ? '0' + h : h}:${m < 10 ? '0' + m : m}`;
}

/**
 * Сгруппировать журнал по дням (новые сверху).
 * @returns {Array<{key,title,items}>}
 */
export function groupJournal(items, now) {
  const map = new Map();
  const sorted = [...(Array.isArray(items) ? items : [])].sort((a, b) => {
    const diff = (Number(b && b.at) || 0) - (Number(a && a.at) || 0);
    if (diff) return diff;
    return String(b && b.id).localeCompare(String(a && a.id));
  });
  for (const event of sorted) {
    const key = journalDayKey(event && event.at);
    if (!key) continue;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(event);
  }
  return [...map.keys()].sort().reverse().map((key) => ({
    key,
    title: dayTitle(key, now),
    items: map.get(key),
  }));
}

/** Добавить страницу журнала без дублей (по id), сохранив порядок по времени. */
export function mergeJournal(existing, page) {
  const seen = new Set();
  const out = [];
  for (const event of [...(existing || []), ...(page || [])]) {
    if (!event || seen.has(event.id)) continue;
    seen.add(event.id);
    out.push(event);
  }
  return out.sort((a, b) => {
    const diff = (Number(b.at) || 0) - (Number(a.at) || 0);
    if (diff) return diff;
    return String(b.id).localeCompare(String(a.id));
  });
}

export const JOURNAL_PAGE = 50;

/* --------------------------------------------------------------------- .ics */

/** Ссылка на .ics одного шага. */
export function icsItemUrl(room, itemId) {
  return `/api/ics?room=${encodeURIComponent(String(room || ''))}&items=${encodeURIComponent(String(itemId || ''))}`;
}

/** Ссылка на .ics всех открытых шагов с датой. */
export function icsAllUrl(room) {
  return `/api/ics?room=${encodeURIComponent(String(room || ''))}&all=1`;
}
