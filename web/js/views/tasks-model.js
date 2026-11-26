/**
 * tasks-model.js — правила доски «Задачи» комнаты (W30-D2).
 *
 * Здесь только чистые функции: стадии, колонки, порядок карточек, правила
 * переноса, клавиатура, подписи ожидания и возраста. Ни DOM, ни сети — поэтому
 * всё это проверяется без браузера (`tests/tasks-board.test.mjs`), а вьюха
 * (`tasks-board.js`) занимается только отрисовкой и действиями.
 *
 * Контракт API §3 волны 30 (пакет D1 — серверная часть):
 *   GET /api/rooms/{room}/tasks →
 *     {tasks:[{id,title,stage,status,assignee,steps_done,steps_total,
 *              needs_you:{count,blocking},accepted,parent_id,children_count,
 *              updated_at,summary_short}],
 *      columns:[...], counts:{...}}
 *
 * Правила, которые здесь соблюдаются:
 *   • колонка = настоящая стадия задачи (`stage` сервера), своих стадий
 *     интерфейс не выдумывает и порядок колонок не переставляет;
 *   • перенос карточки — ТОЛЬКО существующее действие сервера: `accept`
 *     («Готово» → «Принята») и `reopen_accepted` («Принята» → «В работе»).
 *     Остальные переходы делает работа агентов, критик и ответы человека —
 *     доска честно объясняет, почему их нельзя поставить переносом;
 *   • счётчик колонки считается по карточкам этой же колонки (число = числу),
 *     а не берётся из ответа сервера: расхождение счётчика и списка — дефект;
 *   • «Принята» по умолчанию свёрнута, но счётчик виден всегда.
 */

/** Подписи стадий: одни и те же слова на доске, в списке и в карточке. */
export const STAGE_LABEL = {
  new: 'Новая',
  working: 'В работе',
  needs_you: 'Ждёт вас',
  review: 'На проверке',
  done_system: 'Готово',
  accepted: 'Принята',
  archived: 'В архиве',
  scouting: 'Разведка',
  overview_ready: 'Обзор сдан',
};

/** Тон стадии — для точки в шапке колонки и чипа (только токены темы). */
export const STAGE_TONE = {
  new: 'muted',
  working: 'info',
  needs_you: 'warn',
  review: 'info',
  done_system: 'ok',
  accepted: 'ok',
  archived: 'muted',
};

/**
 * Колонки доски — в порядке контракта §3.
 * `empty` — что делать, если колонка пуста (пустое место без выхода запрещено).
 */
export const BOARD_COLUMNS = [
  {
    id: 'new', title: STAGE_LABEL.new, tone: STAGE_TONE.new, create: true,
    hint: 'Задача поставлена, работа ещё не началась.',
    empty: 'Пусто. Поставьте задачу кнопкой «+ Новая задача» — руководитель оценит её и распределит работу.',
  },
  {
    id: 'working', title: STAGE_LABEL.working, tone: STAGE_TONE.working, create: false,
    hint: 'Агенты работают: карточки идут своим ходом, от вас ничего не нужно.',
    empty: 'Пусто. Сюда задачи попадают сами, когда руководитель распределил работу.',
  },
  {
    id: 'needs_you', title: STAGE_LABEL.needs_you, tone: STAGE_TONE.needs_you, create: false,
    hint: 'Без вашего ответа работа стоит — откройте задачу и ответьте.',
    empty: 'Пусто — и это хорошо: ни одна задача не ждёт вашего ответа.',
  },
  {
    id: 'review', title: STAGE_LABEL.review, tone: STAGE_TONE.review, create: false,
    hint: 'Критик или руководитель проверяет итог перед сдачей.',
    empty: 'Пусто. Сюда задачи попадают, когда итог готов и его проверяют.',
  },
  {
    id: 'done_system', title: STAGE_LABEL.done_system, tone: STAGE_TONE.done_system, create: false,
    hint: 'Система считает работу законченной. Примите итог, если согласны.',
    empty: 'Пусто. Готовые задачи появятся здесь, и вы сможете их принять.',
  },
  {
    id: 'accepted', title: STAGE_LABEL.accepted, tone: STAGE_TONE.accepted, create: false,
    hint: 'Вы подтвердили итог: задача заморожена, итог записан в память комнаты.',
    empty: 'Пусто. Принятые задачи хранятся здесь и не меняются, пока вы не вернёте их в работу.',
    collapsible: true,
  },
];

const COLUMN_BY_ID = new Map(BOARD_COLUMNS.map((c) => [c.id, c]));

/** Стадии, которые на доске не показываются полосой колонок. */
const OFF_BOARD = new Set(['archived', 'scouting', 'overview_ready']);

/**
 * Нормализовать задачу из ответа сервера: у карточки всегда есть все поля,
 * даже если сервер прислал неполный объект (старые данные, мок).
 * @param {object} raw
 * @returns {object}
 */
export function normalizeTask(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const needs = src.needs_you && typeof src.needs_you === 'object' ? src.needs_you : {};
  const done = Math.max(0, Number(src.steps_done) || 0);
  const total = Math.max(0, Number(src.steps_total) || 0);
  return {
    id: String(src.id || ''),
    title: String(src.title || src.title_short || 'Без названия'),
    stage: String(src.stage || 'new'),
    status: String(src.status || ''),
    assignee: String(src.assignee || ''),
    steps_done: done,
    steps_total: total,
    needs_you: {
      count: Math.max(0, Number(needs.count) || 0),
      blocking: Boolean(needs.blocking),
    },
    accepted: Boolean(src.accepted),
    parent_id: src.parent_id ? String(src.parent_id) : null,
    children_count: Math.max(0, Number(src.children_count) || 0),
    updated_at: Number(src.updated_at) || 0,
    summary_short: String(src.summary_short || ''),
    archived: Boolean(src.archived),
  };
}

/** Нормализовать список задач. */
export function normalizeTasks(list) {
  return (Array.isArray(list) ? list : []).filter(Boolean).map(normalizeTask);
}

/** Стадия карточки; неизвестную считаем «Новой» (карточка не пропадает). */
export function stageOf(task) {
  const raw = String((task && task.stage) || '');
  if (COLUMN_BY_ID.has(raw)) return raw;
  if (raw === 'accepted' || (task && task.accepted)) return 'accepted';
  return 'new';
}

/** Задача в архиве (не на доске; ссылка на раздел «Архив»). */
export function isArchived(task) {
  const raw = String((task && task.stage) || '');
  return raw === 'archived' || Boolean(task && task.archived);
}

/** Вне доски: архив и этапы «Разведки» (они ждут обзора, а не решения о приёмке). */
export function isOffBoard(task) {
  return isArchived(task) || OFF_BOARD.has(String((task && task.stage) || ''));
}

/**
 * Порядок внутри колонки: сначала то, что ждёт человека и блокирует работу,
 * затем просто ждёт, затем — по свежести изменения, затем по названию.
 */
export function sortTasks(list) {
  const arr = Array.isArray(list) ? list.filter(Boolean).slice() : [];
  const weight = (t) => {
    const n = t.needs_you || {};
    if (Number(n.count) > 0 && n.blocking) return 0;
    if (Number(n.count) > 0) return 1;
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
 * пустая колонка объясняет, зачем она и что сделать сейчас.
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
      collapsible: Boolean(col.collapsible),
      items,
      count: items.length,
    };
  });
}

/** Счётчики по стадиям (ровно по карточкам, которые видит человек). */
export function boardCounts(tasks) {
  const out = {};
  for (const col of BOARD_COLUMNS) out[col.id] = 0;
  for (const task of (Array.isArray(tasks) ? tasks : []).filter(Boolean)) {
    if (isOffBoard(task)) continue;
    out[stageOf(task)] = (out[stageOf(task)] || 0) + 1;
  }
  return out;
}

/**
 * Итоги шапки: всего на доске, ждут вашего ответа, готовы к приёмке, приняты,
 * в архиве (отдельно — ссылка на раздел).
 */
export function boardSummary(tasks) {
  const list = (Array.isArray(tasks) ? tasks : []).filter(Boolean);
  const onBoard = list.filter((t) => !isOffBoard(t));
  return {
    total: onBoard.length,
    // W30-QA1: «ждут вашего ответа» в шапке — те же карточки, что в колонке
    // «Ждёт вас» (число в шапке = числу карточек в ней). Раньше считались все
    // задачи с открытыми пунктами, и шапка спорила с колонкой: у готовой задачи
    // остаются необязательные пункты, но работа по ней уже не ждёт.
    waiting: onBoard.filter((t) => stageOf(t) === 'needs_you').length,
    blocking: onBoard.filter((t) => Boolean((t.needs_you || {}).blocking)).length,
    ready: onBoard.filter((t) => stageOf(t) === 'done_system').length,
    accepted: onBoard.filter((t) => stageOf(t) === 'accepted').length,
    archived: list.filter(isArchived).length,
  };
}

/**
 * Что произойдёт, если перенести задачу из колонки `fromId` в колонку `toId`.
 *
 * Разрешены только действия, которые сервер действительно умеет
 * (`/api/requests/{id}/actions`: `accept`, `reopen_accepted`). Всё остальное —
 * честный отказ с причиной: стадию ведут агенты, критик и ответы человека.
 *
 * @returns {{ok:true, action:string}|{ok:false, reason:string, noop?:boolean}}
 */
export function moveOutcome(fromId, toId) {
  const from = String(fromId || '');
  const to = String(toId || '');
  const target = COLUMN_BY_ID.get(to);
  if (!target) return { ok: false, reason: 'Такой колонки нет на доске.' };
  if (!COLUMN_BY_ID.has(from)) return { ok: false, reason: 'Задача не на доске — переносить нечего.' };
  if (from === to) return { ok: false, noop: true, reason: 'Задача уже в этой колонке.' };

  if (to === 'accepted') {
    if (from !== 'done_system') {
      return {
        ok: false,
        reason: 'Принять можно только готовую задачу: сначала работа должна дойти до колонки «Готово».',
      };
    }
    return { ok: true, action: 'accept' };
  }

  if (from === 'accepted') {
    if (to === 'working') return { ok: true, action: 'reopen_accepted' };
    return {
      ok: false,
      reason: 'Принятую задачу можно только вернуть в работу: дальше её стадию поведут агенты.',
    };
  }

  return {
    ok: false,
    reason: 'Стадию задачи ведут работа агентов, проверка критика и ваши ответы — переносом её не поставить.'
      + ' Откройте задачу, чтобы увидеть, что её ждёт.',
  };
}

/**
 * Соседняя колонка, которая примет задачу с клавиатуры: колонки без действия
 * пропускаем, чтобы стрелка не вела в отказ.
 * @param {string} fromId
 * @param {number} delta -1 влево, +1 вправо
 * @returns {string|null}
 */
export function keyboardTarget(fromId, delta) {
  const from = String(fromId || '');
  const start = BOARD_COLUMNS.findIndex((c) => c.id === from);
  if (start < 0) return null;
  const step = Number(delta) < 0 ? -1 : 1;
  for (let i = start + step; i >= 0 && i < BOARD_COLUMNS.length; i += step) {
    const outcome = moveOutcome(from, BOARD_COLUMNS[i].id);
    if (outcome.ok) return BOARD_COLUMNS[i].id;
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
 * Что задача ждёт и от кого — одна строка на карточке.
 * @returns {{text:string, tone:'warn'|'danger'|'muted', blocking:boolean}}
 */
export function waitingOf(task) {
  const t = task || {};
  const n = t.needs_you || {};
  const count = Number(n.count) || 0;
  const stage = stageOf(t);
  // W30-QA1: принятая задача заморожена — «ждёт вас» на ней быть не может,
  // даже если в данных остались пункты закрытой работы.
  if (stage === 'accepted') return { text: 'принято вами — заморожено', tone: 'muted', blocking: false };
  if (count > 0) {
    const plural = count === 1 ? 'вопрос' : count < 5 ? 'вопроса' : 'вопросов';
    return n.blocking
      ? { text: 'ждёт вас: ' + count + ' ' + plural + ' — работа стоит', tone: 'danger', blocking: true }
      : { text: 'ждёт вас: ' + count + ' ' + plural, tone: 'warn', blocking: false };
  }
  if (stage === 'review') return { text: 'на проверке у критика', tone: 'muted', blocking: false };
  if (stage === 'done_system') return { text: 'готово — примите итог', tone: 'ok', blocking: false };
  if (stage === 'accepted') return { text: 'принято вами — заморожено', tone: 'muted', blocking: false };
  if (stage === 'working') {
    return {
      text: t.assignee ? 'в работе у «' + t.assignee + '»' : 'в работе у команды',
      tone: 'muted', blocking: false,
    };
  }
  return { text: 'работа ещё не началась', tone: 'muted', blocking: false };
}

/** Группы для режима «Список»: стадия → задачи (пустые стадии не показываем). */
export function listGroups(tasks) {
  return boardColumns(tasks).filter((col) => col.count > 0)
    .map((col) => ({ id: col.id, title: col.title, tone: col.tone, items: col.items, count: col.count }));
}

/** Ссылка на страницу задачи внутри комнаты. */
export function taskHref(roomId, taskId) {
  return '#/room/' + encodeURIComponent(String(roomId || '')) + '/request/' + encodeURIComponent(String(taskId || ''));
}

export default {
  BOARD_COLUMNS, STAGE_LABEL, STAGE_TONE, ageText, boardColumns, boardCounts, boardSummary,
  isArchived, isOffBoard, keyboardTarget, listGroups, moveOutcome, normalizeTask, normalizeTasks,
  pluralRu, sortTasks, stageOf, stepsPercent, stepsText, taskHref, waitingOf,
};
