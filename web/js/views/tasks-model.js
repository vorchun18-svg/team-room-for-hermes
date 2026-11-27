/**
 * tasks-model.js — правила доски «Задачи» комнаты (W31-B1).
 *
 * Доска — ровно четыре колонки общего контракта волны 31:
 *   `new` «Новая» → `working` «В работе» → `review` «На проверке» → `done` «Готово».
 * «Ждёт вас» — НЕ колонка, а метка на карточке в работе («Ждёт вас · N вопросов»).
 *
 * Здесь только чистые функции: колонки, порядок карточек, допустимые переносы,
 * клавиатура, подписи. Ни DOM, ни сети — поэтому всё это проверяется без
 * браузера (`tests/tasks-board.test.mjs`), а вьюха (`tasks-board.js`) занимается
 * только отрисовкой и действиями.
 *
 * Контракт API волны 31:
 *   GET /api/rooms/{room}/tasks →
 *     {tasks:[{id, title, column, waiting_you, waiting_count, assignee,
 *              steps_done, steps_total, children_count, updated_at,
 *              summary_short, allowed_moves}], columns, counts, updated_at}
 *   POST /api/requests/{id}/actions {action:'move', to, comment?, confirm?}
 *   GET  /api/requests/{id} → {board_column, allowed_moves}
 *
 * Старые ответы (волна 30: new/working/needs_you/review/done_system/accepted)
 * читаются без потерь: `stage` переводится в колонку по таблице `LEGACY_STAGE`.
 */

/** Подписи колонок: одни и те же слова на доске, в списке и в карточке. */
export const STAGE_LABEL = {
  new: 'Новая',
  working: 'В работе',
  review: 'На проверке',
  done: 'Готово',
  archived: 'В архиве',
};

/** Тон колонки — для точки в шапке (только токены темы). */
export const STAGE_TONE = {
  new: 'muted',
  working: 'info',
  review: 'warn',
  done: 'ok',
  archived: 'muted',
};

/** Старая стадия → колонка контракта 31. */
export const LEGACY_STAGE = {
  new: 'new',
  working: 'working',
  needs_you: 'working',
  review: 'review',
  done_system: 'review',
  accepted: 'done',
  done: 'done',
  archived: 'archived',
};

/**
 * Допустимые переносы контракта 31 (`POST …/actions {action:'move'}`).
 * `accept` — перенос в «Готово» равен принятию (отмена 10 с на клиенте).
 */
export const MOVE_RULES = {
  new: {},
  working: { review: {} },
  review: { done: { accept: true }, working: { comment: true } },
  done: { working: {} },
};

/**
 * Колонки доски — в порядке контракта.
 * `empty` — что делать, если колонка пуста (пустое место без выхода запрещено).
 */
export const BOARD_COLUMNS = [
  {
    id: 'new', title: STAGE_LABEL.new, tone: STAGE_TONE.new, create: true,
    hint: 'Задача поставлена, руководитель оценивает объём.',
    empty: 'Пусто. Новая задача сразу уходит в работу — здесь она бывает только в первые минуты.',
  },
  {
    id: 'working', title: STAGE_LABEL.working, tone: STAGE_TONE.working, create: false,
    hint: 'Агенты работают. Метка «Ждёт вас» — у задачи есть открытые вопросы.',
    empty: 'Пусто. Сюда задачи попадают сами, когда руководитель распределил работу.',
  },
  {
    id: 'review', title: STAGE_LABEL.review, tone: STAGE_TONE.review, create: false,
    hint: 'Итог проверяется. Примите его или верните на доработку с комментарием.',
    empty: 'Пусто. Сюда задачи попадают, когда итог готов и его проверяют.',
  },
  {
    id: 'done', title: STAGE_LABEL.done, tone: STAGE_TONE.done, create: false,
    hint: 'Принято вами: задача заморожена, итог записан в память комнаты.',
    empty: 'Пусто. Принятые задачи хранятся здесь, пока вы не вернёте их в работу.',
  },
];

const COLUMN_BY_ID = new Map(BOARD_COLUMNS.map((c) => [c.id, c]));

/** Стадии вне доски: архив и этапы «Разведки». */
const OFF_BOARD = new Set(['archived', 'scouting', 'overview_ready']);

/** Колонка по стадии сервера (новая `column` важнее старого `stage`). */
export function columnOfStage(raw) {
  const stage = String(raw == null ? '' : raw);
  if (COLUMN_BY_ID.has(stage)) return stage;
  if (LEGACY_STAGE[stage]) return LEGACY_STAGE[stage];
  return 'new';
}

/**
 * Нормализовать задачу: у карточки всегда есть все поля, даже если сервер
 * прислал неполный объект (старые данные, мок).
 * @param {object} raw
 * @returns {object}
 */
export function normalizeTask(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const needs = src.needs_you && typeof src.needs_you === 'object' ? src.needs_you : {};
  const done = Math.max(0, Number(src.steps_done) || 0);
  const total = Math.max(0, Number(src.steps_total) || 0);
  const column = columnOfStage(src.column || src.stage);
  const waitingCount = Math.max(0, Number(src.waiting_count != null ? src.waiting_count : needs.count) || 0);
  const waiting = src.waiting_you != null ? Boolean(src.waiting_you) : waitingCount > 0;
  const moves = Array.isArray(src.allowed_moves)
    ? src.allowed_moves.map((m) => String(m)).filter((m) => COLUMN_BY_ID.has(m))
    : null;
  return {
    id: String(src.id || ''),
    title: String(src.title || src.title_short || 'Без названия'),
    // `stage` и `column` — одно и то же значение колонки: старые читатели
    // (`stageOf`) не ломаются, новые берут `column`.
    column,
    stage: column,
    status: String(src.status || ''),
    assignee: String(src.assignee || ''),
    steps_done: done,
    steps_total: total,
    waiting_you: waiting,
    waiting_count: waitingCount,
    needs_you: { count: waitingCount, blocking: Boolean(needs.blocking) },
    accepted: column === 'done',
    parent_id: src.parent_id ? String(src.parent_id) : null,
    children_count: Math.max(0, Number(src.children_count) || 0),
    updated_at: Number(src.updated_at) || 0,
    summary_short: String(src.summary_short || ''),
    allowed_moves: moves,
    archived: Boolean(src.archived),
  };
}

/** Нормализовать список задач. */
export function normalizeTasks(list) {
  return (Array.isArray(list) ? list : []).filter(Boolean).map(normalizeTask);
}

/** Колонка карточки (старое имя `stageOf` оставлено для совместимости). */
export function stageOf(task) {
  const t = task || {};
  return columnOfStage(t.column || t.stage);
}

/** Задача в архиве (не на доске; ссылка на раздел «Архив»). */
export function isArchived(task) {
  const raw = String((task && (task.column || task.stage)) || '');
  return raw === 'archived' || Boolean(task && task.archived);
}

/** Вне доски: архив и этапы «Разведки» (они ждут обзора, а не решения о приёмке). */
export function isOffBoard(task) {
  return isArchived(task) || OFF_BOARD.has(String((task && (task.column || task.stage)) || ''));
}

/**
 * Порядок внутри колонки: сначала то, что ждёт человека и блокирует работу,
 * затем просто ждёт, затем — по свежести изменения, затем по названию.
 */
export function sortTasks(list) {
  const arr = Array.isArray(list) ? list.filter(Boolean).slice() : [];
  const weight = (t) => {
    const count = Number(t.waiting_count != null ? t.waiting_count : (t.needs_you || {}).count) || 0;
    if (count > 0 && (t.needs_you || {}).blocking) return 0;
    if (count > 0) return 1;
    return 2;
  };
  arr.sort((a, b) => {
    const wa = weight(a);
    const wb = weight(b);
    if (wa !== wb) return wa - wb;
    const ua = Number(a.updated_at) || 0;
    const ub = Number(b.updated_at) || 0;
    if (ua !== ub) return ub - ua;
    return String(a.title || '').localeCompare(String(b.title || ''), 'ru');
  });
  return arr;
}

/**
 * Колонки с задачами. Порядок колонок постоянный, пустые колонки тоже на месте:
 * пустая колонка объясняет, зачем она.
 * @param {object[]} tasks
 */
export function boardColumns(tasks) {
  const list = (Array.isArray(tasks) ? tasks : []).filter(Boolean).filter((t) => !isOffBoard(t));
  const buckets = new Map(BOARD_COLUMNS.map((c) => [c.id, []]));
  for (const task of list) buckets.get(stageOf(task)).push(task);
  return BOARD_COLUMNS.map((col) => {
    const items = sortTasks(buckets.get(col.id));
    return {
      id: col.id,
      title: col.title,
      tone: col.tone,
      hint: col.hint,
      empty: col.empty,
      create: Boolean(col.create),
      items,
      count: items.length,
    };
  });
}

/** Счётчики по колонкам (ровно по карточкам, которые видит человек). */
export function boardCounts(tasks) {
  const out = {};
  for (const col of BOARD_COLUMNS) out[col.id] = 0;
  for (const task of (Array.isArray(tasks) ? tasks : []).filter(Boolean)) {
    if (isOffBoard(task)) continue;
    out[stageOf(task)] = (out[stageOf(task)] || 0) + 1;
  }
  return out;
}

/** Итоги шапки: всего, ждут вашего ответа, на проверке, принято, в архиве. */
export function boardSummary(tasks) {
  const list = (Array.isArray(tasks) ? tasks : []).filter(Boolean);
  const onBoard = list.filter((t) => !isOffBoard(t));
  return {
    total: onBoard.length,
    waiting: onBoard.filter((t) => stageOf(t) === 'working' && Boolean(t.waiting_you)).length,
    questions: onBoard.reduce((sum, t) => sum + (stageOf(t) === 'working' ? Math.max(0, Number(t.waiting_count) || 0) : 0), 0),
    review: onBoard.filter((t) => stageOf(t) === 'review').length,
    done: onBoard.filter((t) => stageOf(t) === 'done').length,
    archived: list.filter(isArchived).length,
  };
}

/** Правило переноса колонок (или null, если такого переноса нет в контракте). */
export function moveRule(fromId, toId) {
  const from = String(fromId || '');
  const to = String(toId || '');
  const row = MOVE_RULES[from];
  if (!row || !Object.prototype.hasOwnProperty.call(row, to)) return null;
  return row[to];
}

/**
 * Что произойдёт, если перенести задачу из колонки `fromId` в колонку `toId`.
 *
 * Разрешены ровно переносы контракта 31: с проверки — принять (в «Готово») или
 * вернуть на доработку (в «В работу», с комментарием), из «Готово» — вернуть в
 * работу, из «В работе» — отправить на проверку. Остальное — честный отказ
 * с причиной. `allowed` — `allowed_moves` карточки/детали, если сервер его
 * прислал: он главнее таблицы, клиент не догадывается.
 *
 * @returns {{ok:true, action:'move', to:string, accept:boolean, comment:boolean}|{ok:false, reason:string, noop?:boolean}}
 */
export function moveOutcome(fromId, toId, allowed) {
  const from = columnOfStage(fromId);
  const to = String(toId || '');
  const target = COLUMN_BY_ID.get(to);
  if (!target) return { ok: false, reason: 'Такой колонки нет на доске.' };
  if (from === to) return { ok: false, noop: true, reason: 'Задача уже в этой колонке.' };
  const allowedList = Array.isArray(allowed) ? allowed.map(String) : null;
  if (allowedList && allowedList.length && !allowedList.includes(to)) {
    return { ok: false, reason: 'Так перенести нельзя: «' + target.title + '» у этой задачи сейчас недоступно.' };
  }
  const rule = moveRule(from, to);
  if (rule) return { ok: true, action: 'move', to, accept: Boolean(rule.accept), comment: Boolean(rule.comment) };
  return { ok: false, reason: refusalText(from, to) };
}

/**
 * Честная строка о переносе, который сервер записал, но на доске не переставил
 * (`warnings[].code = board_not_moved`: движок доски отказал). Возвращает текст
 * предупреждения или '' — тогда перенос прошёл целиком.
 * @param {{warnings?: Array<{code?: string, message?: string}>}|null} res
 * @returns {string}
 */
export function moveWarning(res) {
  const list = res && Array.isArray(res.warnings) ? res.warnings : [];
  const hit = list.find((w) => w && w.code === 'board_not_moved');
  if (!hit) return '';
  return 'Состояние задачи записано, но карточку на доске переставить не удалось — она обновится следующим проходом.';
}

/** Человеческая причина отказа (без внутренних кодов). */
export function refusalText(fromId, toId) {
  const from = columnOfStage(fromId);
  const to = String(toId || '');
  const title = (COLUMN_BY_ID.get(to) || {}).title || to;
  if (from === 'new') {
    return 'Из «Новой» переносить нечего: задачу сразу забирает руководитель, она уходит в работу сама.';
  }
  if (to === 'new') return 'В «Новую» задачу не вернуть: это начало пути, а не решение.';
  if (from === 'working' && to === 'done') {
    return 'Принять можно только с проверки: сначала отправьте задачу на проверку — итог должен быть готов.';
  }
  if (from === 'done' && to === 'review') {
    return 'Принятую задачу можно только вернуть в работу — дальше её стадию поведут агенты.';
  }
  return 'Так перенести нельзя: из «' + ((COLUMN_BY_ID.get(from) || {}).title || from) + '» в «' + title + '» доска не переставляет.';
}

/**
 * Соседняя колонка, которая примет задачу с клавиатуры: колонки без действия
 * пропускаем, чтобы стрелка не вела в отказ.
 * @param {string} fromId
 * @param {number} delta -1 влево, +1 вправо
 * @param {string[]|null} [allowed] allowed_moves карточки
 * @returns {string|null}
 */
export function keyboardTarget(fromId, delta, allowed) {
  const from = columnOfStage(fromId);
  const start = BOARD_COLUMNS.findIndex((c) => c.id === from);
  if (start < 0) return null;
  const step = Number(delta) < 0 ? -1 : 1;
  for (let i = start + step; i >= 0 && i < BOARD_COLUMNS.length; i += step) {
    if (moveOutcome(from, BOARD_COLUMNS[i].id, allowed).ok) return BOARD_COLUMNS[i].id;
  }
  return null;
}

/** Сколько секунд/минут/часов/дней прошло — короткой подписью. */
export function ageText(ts, nowMs) {
  const then = Number(ts) || 0;
  if (then <= 0) return '';
  const now = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
  const sec = Math.max(0, Math.round((now - then * 1000) / 1000));
  if (sec < 60) return 'только что';
  const min = Math.round(sec / 60);
  if (min < 60) return min + ' мин';
  const hours = Math.round(min / 60);
  if (hours < 24) return hours + ' ч';
  const days = Math.round(hours / 24);
  if (days < 31) return days + ' дн';
  const months = Math.round(days / 30);
  return months + ' мес';
}

/**
 * Русское согласование числа: 1 задача, 2 задачи, 5 задач.
 * @param {number} n
 * @param {[string,string,string]} forms [одна, две, пять]
 */
export function pluralRu(n, forms) {
  const num = Math.abs(Number(n) || 0);
  const list = Array.isArray(forms) && forms.length === 3 ? forms : ['', '', ''];
  const mod10 = num % 10;
  const mod100 = num % 100;
  if (mod10 === 1 && mod100 !== 11) return list[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return list[1];
  return list[2];
}

/** «Шаги 2 из 5» (нет шагов — пусто, прочерк не нужен). */
export function stepsText(task) {
  const done = Math.max(0, Number((task && task.steps_done) || 0));
  const total = Math.max(0, Number((task && task.steps_total) || 0));
  if (!total) return '';
  return 'шаги ' + Math.min(done, total) + ' из ' + total;
}

/** Полоса шагов: доля выполненного (0…100). */
export function stepsPercent(task) {
  const done = Math.max(0, Number((task && task.steps_done) || 0));
  const total = Math.max(0, Number((task && task.steps_total) || 0));
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

/**
 * Метка «Ждёт вас · N вопросов» — только у карточки в работе (контракт 31):
 * «ждёт вас» не колонка, а состояние задачи. У принятой задачи метки нет.
 * @param {object} task
 * @returns {{text:string, short:string, count:number, tone:'warn'|'danger', blocking:boolean}|null}
 */
export function waitingBadge(task) {
  const t = task || {};
  const column = stageOf(t);
  const count = Math.max(0, Number(t.waiting_count != null ? t.waiting_count : (t.needs_you || {}).count) || 0);
  if (column !== 'working') return null;
  if (!t.waiting_you && count <= 0) return null;
  const n = Math.max(1, count);
  return {
    text: 'Ждёт вас · ' + n + ' ' + pluralRu(n, ['вопрос', 'вопроса', 'вопросов']),
    short: 'ждёт вас · ' + n,
    count: n,
    tone: Boolean((t.needs_you || {}).blocking) ? 'danger' : 'warn',
    blocking: Boolean((t.needs_you || {}).blocking),
  };
}

/**
 * Что происходит с задачей — одна строка на карточке.
 * @returns {{text:string, tone:'warn'|'danger'|'ok'|'muted', blocking:boolean}}
 */
export function waitingOf(task) {
  const t = task || {};
  const column = stageOf(t);
  const badge = waitingBadge(t);
  if (badge) {
    return {
      text: badge.text + (badge.blocking ? ' — работа стоит' : ''),
      tone: badge.tone,
      blocking: badge.blocking,
    };
  }
  if (column === 'done') return { text: 'принято вами — заморожено', tone: 'muted', blocking: false };
  if (column === 'review') return { text: 'итог готов: примите или верните на доработку', tone: 'ok', blocking: false };
  if (column === 'working') {
    return {
      text: t.assignee ? 'в работе у «' + t.assignee + '»' : 'в работе у команды',
      tone: 'muted', blocking: false,
    };
  }
  return { text: 'работа ещё не началась', tone: 'muted', blocking: false };
}

/** Кнопки карточки по колонке (одно место для подписей). */
export function cardActionsOf(task) {
  const column = stageOf(task);
  if (column === 'review') {
    return [
      { to: 'done', label: 'Принять', kind: 'primary', title: 'Принять итог: задача замораживается, итог уходит в память комнаты. Отменить можно 10 секунд' },
      { to: 'working', label: 'Вернуть на доработку', kind: 'secondary', title: 'Вернуть задачу в работу с комментарием — что исправить' },
    ];
  }
  if (column === 'done') {
    return [{ to: 'working', label: 'Вернуть в работу', kind: 'ghost', title: 'Вернуть принятую задачу в работу — агенты продолжат' }];
  }
  return [];
}

/** Группы для режима «Список»: колонка → задачи (пустые колонки не показываем). */
export function listGroups(tasks) {
  return boardColumns(tasks).filter((col) => col.count > 0)
    .map((col) => ({ id: col.id, title: col.title, tone: col.tone, items: col.items, count: col.count }));
}

/** Ссылка на страницу задачи внутри комнаты. */
export function taskHref(roomId, taskId) {
  return '#/room/' + encodeURIComponent(String(roomId || '')) + '/request/' + encodeURIComponent(String(taskId || ''));
}

/** Ссылка на доску комнаты (вход в комнату — всегда «Задачи»). */
export function tasksHref(roomId) {
  return '#/room/' + encodeURIComponent(String(roomId || '')) + '/tasks';
}

export default {
  BOARD_COLUMNS, LEGACY_STAGE, MOVE_RULES, STAGE_LABEL, STAGE_TONE, ageText, boardColumns,
  boardCounts, boardSummary, cardActionsOf, columnOfStage, isArchived, isOffBoard, keyboardTarget,
  listGroups, moveOutcome, moveRule, moveWarning, normalizeTask, normalizeTasks, pluralRu, refusalText, sortTasks,
  stageOf, stepsPercent, stepsText, taskHref, tasksHref, waitingBadge, waitingOf,
};
