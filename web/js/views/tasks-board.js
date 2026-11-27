/**
 * tasks-board.js — комната, вкладка «Задачи» (W31-B1): композер и канбан.
 *
 * Главный экран комнаты. Отвечает на один вопрос: «где каждая задача и что
 * нужно от меня». Колонки — контракт волны 31, ровно четыре, в одну линию:
 *   «Новая» → «В работе» → «На проверке» → «Готово».
 * У каждой колонки свой маленький заголовок: название, счётчик и «+».
 * «Ждёт вас» — не колонка, а метка на карточке в работе («Ждёт вас · N вопросов»).
 *
 * Правила, которые здесь соблюдаются:
 *   • перенос карточки = действие сервера `move` (контракт 31): с проверки —
 *     «Принять» (в «Готово», отмена 10 с на клиенте) и «Вернуть на доработку»
 *     (в работу, с комментарием); из «Готово» — «Вернуть в работу»; из работы —
 *     «На проверку». Остальное доска честно объясняет словами (`moveOutcome`);
 *   • принятие отложено на 10 секунд общим `undo.js`: пока идёт отсчёт, на
 *     сервере ничего не менялось, «Вернуть» просто не выполняет действие;
 *   • метка «Ждёт вас · N вопросов» живёт на карточке в работе; у принятой
 *     задачи вопросов нет (сервер закрывает их при переходе в «Готово»);
 *   • блок «Задача команде» (композер) стоит НАД доской: вкладки «Новая задача»
 *     больше нет, `#/room/<id>/composer` ведёт сюда с фокусом на поле;
 *   • прошлые данные показываются сразу из локального снимка, обновление идёт
 *     фоном (без серых заглушек на тёплом входе);
 *   • доска на узком окне прокручивается ВНУТРИ себя, страница по горизонтали
 *     не едет; цвета — только токенами темы, никакого `innerHTML`.
 *
 * Данные: GET /api/rooms/{room}/tasks (контракт 31).
 * Действия: POST /api/requests/{id}/actions {action:'move', to, comment?, confirm?}.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { poll } from '../api.js';
import {
  confirmDialog, emptyState, ensureCss, errorBanner, lsGet, lsSet, promptDialog, segmented, skeleton,
} from '../ui.js';
import { undo } from '../undo.js';
import {
  BOARD_COLUMNS, ageText, boardColumns, boardSummary, cardActionsOf, keyboardTarget, listGroups,
  moveOutcome, moveWarning, normalizeTasks, pluralRu, refusalText, stageOf, stepsPercent, stepsText, taskHref,
  waitingBadge,
} from './tasks-model.js';

export const VIEW_CSS = 'css/view-tasks.css';
export const POLL_MS = 20000;
export const DRAG_THRESHOLD = 4;

const MODE_KEY = 'teamroom.tasks.mode';
const COMPOSE_KEY = 'teamroom.tasks.composeCollapsed';
const MODES = [
  { value: 'board', label: 'Доска' },
  { value: 'list', label: 'Список' },
];

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/** Снимок прошлой доски: ключ localStorage на комнату. */
export function cacheKey(roomId) {
  return 'teamroom.tasks.cache.' + String(roomId || '');
}

/**
 * Сохранить прошлую доску. Снимок маленький (id, колонка, название): он нужен
 * только чтобы доска не была пустой первые полсекунды тёплого входа.
 */
export function saveSnapshot(roomId, tasks) {
  try {
    const slim = (Array.isArray(tasks) ? tasks : []).slice(0, 60).map((t) => ({
      id: t.id, title: t.title, column: t.column, assignee: t.assignee,
      steps_done: t.steps_done, steps_total: t.steps_total,
      waiting_you: t.waiting_you, waiting_count: t.waiting_count,
      children_count: t.children_count, updated_at: t.updated_at,
    }));
    lsSet(cacheKey(roomId), { at: Date.now(), tasks: slim });
  } catch { /* хранилище недоступно — работаем без снимка */ }
}

/** Прошлая доска комнаты (или null). */
export function readSnapshot(roomId) {
  try {
    const raw = lsGet(cacheKey(roomId), null);
    if (!raw || typeof raw !== 'object' || !Array.isArray(raw.tasks)) return null;
    return { at: Number(raw.at) || 0, tasks: normalizeTasks(raw.tasks) };
  } catch { return null; }
}

/* ─────────────────────────────────────────────────────────────── отрисовка */

/** Кнопка действия карточки (не открывает задачу: клик гасим). */
function cardButton(label, kind, title, onClick) {
  return h('button', {
    class: ['btn', 'btn--sm', 'btn--' + (kind || 'secondary'), 'tasks-card__btn'],
    type: 'button',
    title,
    onclick: (ev) => {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
      onClick();
    },
  }, label);
}

/** Кнопки карточки: на проверке — «Принять»/«Вернуть на доработку»; в готовом — «Вернуть в работу». */
function cardActions(task, opts) {
  const pending = typeof opts.isPending === 'function' ? opts.isPending(task.id) : false;
  if (pending) {
    return [cardButton('Отменить', 'ghost', 'Отменить принятие — задача останется в «На проверке»',
      () => opts.onCancel(task))];
  }
  return cardActionsOf(task).map((a) => cardButton(a.label, a.kind, a.title,
    () => opts.onMove(task, a.to)));
}

/** Строка «что с задачей» без метки «Ждёт вас» (метка вынесена в чип). */
function stateTextOf(task) {
  const column = stageOf(task);
  if (column === 'done') return { text: 'принято вами — заморожено', tone: 'muted' };
  if (column === 'review') return { text: 'итог готов', tone: 'ok' };
  if (column === 'working') {
    return { text: task.assignee ? 'в работе у «' + task.assignee + '»' : 'в работе у команды', tone: 'muted' };
  }
  return { text: 'работа ещё не началась', tone: 'muted' };
}

/** Соседняя колонка для стрелки (для честного отказа, когда переноса нет). */
function neighbourColumn(columnId, dir) {
  const i = BOARD_COLUMNS.findIndex((c) => c.id === columnId);
  const next = BOARD_COLUMNS[i + (dir < 0 ? -1 : 1)];
  return next ? next.id : null;
}

/** Карточка задачи: название, метка «Ждёт вас», шаги, ответственный, кнопки. */
function cardNode(task, columnId, opts, nowMs) {
  const stateText = stateTextOf(task);
  const steps = stepsText(task);
  const pct = stepsPercent(task);
  const age = ageText(task.updated_at, nowMs);
  const pending = typeof opts.isPending === 'function' ? opts.isPending(task.id) : false;
  const grabbed = typeof opts.isGrabbed === 'function' ? opts.isGrabbed(task.id) : false;
  const title = String(task.title || 'Без названия');
  const colTitle = (BOARD_COLUMNS.find((c) => c.id === columnId) || {}).title || '';
  const badge = waitingBadge(task);
  const actions = cardActions(task, opts);

  const card = h('div', {
    class: [
      'tasks-card', 'tasks-card--' + stateText.tone,
      pending ? 'tasks-card--pending' : '',
      grabbed ? 'tasks-card--grabbed' : '',
    ],
    'data-task': task.id,
    'data-stage': columnId,
    'data-column': columnId,
    role: 'button',
    tabindex: '0',
    'aria-grabbed': grabbed ? 'true' : 'false',
    'aria-label': title + ' · колонка «' + colTitle + '»'
      + (badge ? ' · ' + badge.text : '')
      + (task.children_count ? ' · подзадач: ' + task.children_count : ''),
    title: 'Enter — открыть задачу. Пробел — взять карточку, ← → перенести, Esc — отпустить.',
  },
  h('div', { class: 'tasks-card__top' },
    h('span', { class: 'tasks-card__title' }, title),
    Number(task.children_count) ? h('span', {
      class: 'chip chip--outline tasks-card__kids', title: 'Подзадач в проекте: ' + task.children_count,
    }, icon('git-branch', { size: 10 }), String(task.children_count)) : null),
  badge ? h('div', {
    class: ['tasks-card__wait', 'tasks-card__wait--' + badge.tone, 'tasks-card__badge'],
    title: badge.blocking ? 'Без вашего ответа работа стоит' : 'У задачи есть открытые вопросы',
  }, icon('comment-discussion', { size: 11 }), badge.text) : null,
  h('div', { class: 'tasks-card__meta small muted' },
    task.assignee ? h('span', { class: 'tasks-card__who' }, icon('person', { size: 11 }), task.assignee) : null,
    steps ? h('span', { class: 'tasks-card__steps' }, steps) : null),
  steps ? h('div', {
    class: 'tasks-card__bar', role: 'progressbar', 'aria-valuenow': String(pct),
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': steps,
  }, h('span', { class: 'tasks-card__bar-fill', style: 'width:' + pct + '%' })) : null,
  h('div', { class: ['tasks-card__wait', 'tiny', 'tasks-card__wait--' + stateText.tone] }, stateText.text),
  h('div', { class: 'tasks-card__foot' },
    h('span', { class: 'tasks-card__age tiny muted' }, age ? 'обновлено ' + age : ''),
    pending ? h('span', { class: 'tasks-card__pending tiny' }, 'принимается…') : null),
  actions.length ? h('div', { class: 'tasks-card__actions' }, ...actions) : null);

  // Перетаскивание указателем (мышь, перо, палец).
  let dragging = false;
  let suppressClick = false;
  let startX = 0;
  let startY = 0;
  let overCol = null;

  const clearOver = () => {
    if (!overCol) return;
    const body = document.querySelector('[data-column-body="' + overCol + '"]');
    if (body) body.classList.remove('tasks-bcol--over');
    overCol = null;
  };

  card.addEventListener('pointerdown', (ev) => {
    if (ev.button !== undefined && ev.button !== 0) return;
    // Кнопки и ссылки внутри карточки — не ручка перетаскивания: иначе захват
    // указателя перетягивает клик на карточку и открывает задачу вместо «Принять».
    const target = ev.target;
    if (target && typeof target.closest === 'function' && target.closest('button,a,input,textarea,select')) return;
    dragging = false;
    suppressClick = false;
    startX = ev.clientX;
    startY = ev.clientY;
  });

  card.addEventListener('pointermove', (ev) => {
    if (!startX && !startY) return;             // нажали не на карточку — не тащим
    const dx = Math.abs(ev.clientX - startX);
    const dy = Math.abs(ev.clientY - startY);
    if (!dragging && dx < DRAG_THRESHOLD && dy < DRAG_THRESHOLD) return;
    if (!dragging) {
      dragging = true;
      card.classList.add('tasks-card--dragging');
      document.body.classList.add('tasks-dragging');
      // Захват ставим только когда перенос начался: до этого кнопки карточки
      // должны получать свой обычный клик.
      try { card.setPointerCapture(ev.pointerId); } catch { /* старые браузеры */ }
    }
    const under = document.elementFromPoint(ev.clientX, ev.clientY);
    const body = under && under.closest ? under.closest('[data-column-body]') : null;
    const colId = body ? body.getAttribute('data-column-body') : null;
    if (colId !== overCol) {
      clearOver();
      if (body && colId !== columnId) {
        overCol = colId;
        body.classList.add('tasks-bcol--over');
      }
    }
  });

  const finish = (ev, cancelled) => {
    if (card.hasPointerCapture && card.hasPointerCapture(ev.pointerId)) {
      try { card.releasePointerCapture(ev.pointerId); } catch { /* уже отпущено */ }
    }
    const target = overCol;
    clearOver();
    card.classList.remove('tasks-card--dragging');
    document.body.classList.remove('tasks-dragging');
    if (!dragging) return;                    // обычный клик — откроем задачу
    dragging = false;
    // После переноса браузер всё равно шлёт click — гасим ровно один.
    suppressClick = true;
    if (cancelled || !target) return;
    opts.onDrop(task, columnId, target);
  };

  card.addEventListener('pointerup', (ev) => finish(ev, false));
  card.addEventListener('pointercancel', (ev) => finish(ev, true));
  card.addEventListener('click', (ev) => {
    if (suppressClick) { suppressClick = false; ev.preventDefault(); ev.stopPropagation(); return; }
    if (dragging) { ev.preventDefault(); return; }
    opts.onOpen(task);
  });
  card.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); opts.onOpen(task); return; }
    if (ev.key === ' ' || ev.key === 'Spacebar') {
      ev.preventDefault();
      opts.onGrab(task, !grabbed);
      return;
    }
    if (ev.key === 'Escape' && grabbed) { ev.preventDefault(); opts.onGrab(task, false); return; }
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const dir = ev.key === 'ArrowLeft' ? -1 : 1;
    const target = keyboardTarget(columnId, dir, task.allowed_moves);
    if (!target) {
      const near = neighbourColumn(columnId, dir);
      opts.onBlocked(near ? refusalText(columnId, near)
        : 'Дальше по доске переносить некуда: это крайняя колонка.', task);
      return;
    }
    opts.onDrop(task, columnId, target);
  });
  return card;
}

/** Колонка: маленький заголовок (название + счётчик + «+») и тело для переноса. */
function columnNode(col, opts, nowMs) {
  const head = h('div', { class: 'tasks-bcol__head' },
    h('span', { class: 'tasks-bcol__title' },
      h('span', { class: ['tasks-bcol__dot', 'tasks-bcol__dot--' + col.id], 'aria-hidden': 'true' }),
      h('span', { class: 'tasks-bcol__name' }, col.title),
      h('span', { class: 'tasks-bcol__count tnum', title: 'Задач в колонке: ' + col.count }, String(col.count))),
    h('button', {
      class: 'btn btn--ghost btn--sm tasks-bcol__add', type: 'button',
      'data-act': col.create ? 'task-new' : 'task-add-' + col.id,
      title: 'Поставить новую задачу — откроется поле «Задача команде»',
      'aria-label': 'Новая задача в колонке «' + col.title + '»',
      onclick: () => opts.onCreate(),
    }, icon('plus', { size: 12 })));

  const body = h('div', {
    class: 'tasks-bcol__body',
    'data-column-body': col.id,
    role: 'list',
    'aria-label': 'Колонка «' + col.title + '», задач: ' + col.count,
  });
  if (col.items.length) {
    for (const task of col.items) body.appendChild(cardNode(task, col.id, opts, nowMs));
  } else {
    body.appendChild(h('p', { class: 'tasks-bcol__empty tiny muted' }, col.empty));
  }

  return h('section', {
    class: ['tasks-bcol', 'tasks-bcol--' + col.id],
    'data-column': col.id,
    'aria-label': col.title,
  }, head, body);
}

/**
 * Доска целиком: четыре колонки в одну линию, у каждой свой заголовок.
 * @param {object[]} tasks уже нормализованные задачи
 * @param {{onOpen:Function,onDrop:Function,onMove:Function,onGrab?:Function,onBlocked:Function,
 *          onCancel?:Function,onCreate?:Function,isPending?:Function,isGrabbed?:Function,
 *          nowMs?:number}} opts
 */
export function renderBoard(tasks, opts) {
  const o = opts || {};
  const nowMs = Number.isFinite(Number(o.nowMs)) ? Number(o.nowMs) : Date.now();
  const summary = boardSummary(tasks);
  const columns = boardColumns(tasks);
  if (!summary.total) {
    return emptyState({
      icon: 'project',
      title: 'Задач пока нет',
      text: 'Задача комнаты проходит четыре колонки: новая → в работе → на проверке → готово. '
        + 'Напишите её в поле «Задача команде» выше: руководитель раздаст работу команде, '
        + 'а вы примете итог на проверке.',
      action: { label: 'Написать задачу', variant: 'primary', onclick: () => o.onCreate && o.onCreate() },
    });
  }
  return h('div', {
    class: 'tasks-board', role: 'group', 'aria-label': 'Задачи комнаты по колонкам',
  }, ...columns.map((col) => columnNode(col, o, nowMs)));
}

/** Строка режима «Список»: та же карточка, но читаемой строкой. */
function rowNode(task, opts, nowMs) {
  const stateText = stateTextOf(task);
  const badge = waitingBadge(task);
  const steps = stepsText(task);
  const age = ageText(task.updated_at, nowMs);
  const pending = typeof opts.isPending === 'function' ? opts.isPending(task.id) : false;
  const href = taskHref(opts.roomId, task.id);
  const actions = cardActions(task, opts);
  return h('article', {
    class: ['tasks-row', 'tasks-row--' + (badge ? badge.tone : stateText.tone), pending ? 'tasks-row--pending' : ''],
    role: 'listitem', 'data-task': task.id, 'data-stage': stageOf(task),
  },
  h('div', { class: 'tasks-row__main' },
    h('div', { class: 'tasks-row__titleline' },
      h('a', { class: 'tasks-row__title', href, title: task.title }, task.title),
      h('span', { class: 'chip chip--outline tasks-row__stage' },
        (BOARD_COLUMNS.find((c) => c.id === stageOf(task)) || {}).title || ''),
      badge ? h('span', { class: 'chip chip--' + (badge.tone === 'danger' ? 'danger' : 'warn') }, badge.text) : null,
      pending ? h('span', { class: 'chip chip--warn' }, 'принимается…') : null),
    h('div', { class: 'tasks-row__meta small muted' },
      task.assignee ? h('span', null, icon('person', { size: 11 }), task.assignee) : null,
      steps ? h('span', null, steps) : null,
      h('span', { class: 'tasks-row__wait' }, stateText.text),
      age ? h('span', null, 'обновлено ' + age) : null)),
  h('div', { class: 'tasks-row__side' },
    h('a', { class: 'btn btn--ghost btn--sm', href }, 'Открыть'),
    ...actions));
}

/** Список: группы по колонкам с заголовками и счётчиками. */
export function renderList(tasks, opts) {
  const o = opts || {};
  const nowMs = Number.isFinite(Number(o.nowMs)) ? Number(o.nowMs) : Date.now();
  const summary = boardSummary(tasks);
  if (!summary.total) return renderBoard(tasks, o);
  const groups = listGroups(tasks);
  return h('div', { class: 'tasks-list', role: 'group', 'aria-label': 'Список задач комнаты' },
    ...groups.map((g) => h('section', { class: 'tasks-list__group', 'data-column': g.id },
      h('div', { class: 'tasks-list__head' },
        h('span', { class: ['tasks-bcol__dot', 'tasks-bcol__dot--' + g.id], 'aria-hidden': 'true' }),
        h('span', { class: 'tasks-list__title' }, g.title),
        h('span', { class: 'tasks-bcol__count tnum' }, String(g.count))),
      h('div', { class: 'tasks-list__rows', role: 'list' },
        ...g.items.map((task) => rowNode(task, o, nowMs))))));
}

/* ─────────────────────────────────────────────────────────────── экран */

export async function mount(el, ctx) {
  ensureCss(VIEW_CSS);
  const roomId = String((ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || '');
  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  if (!roomId) {
    el.appendChild(emptyState({
      icon: 'error', title: 'Комната не выбрана',
      text: 'Откройте комнату в левой колонке — тогда будет видно её задачи.',
    }));
    return { unmount() {} };
  }

  const snapshot = readSnapshot(roomId);
  const state = {
    tasks: snapshot ? snapshot.tasks : [],
    mode: String(lsGet(MODE_KEY, 'board') || 'board') === 'list' ? 'list' : 'board',
    pending: new Set(),
    grabbed: null,
    loaded: Boolean(snapshot),
    stale: Boolean(snapshot),
    error: null,
    composeCollapsed: lsGet(COMPOSE_KEY, false) === true,
    composeNote: '',
  };
  let disposed = false;
  let seq = 0;
  let composerInst = null;
  let composerFailed = false;

  const head = h('div', { class: 'tasks__head' },
    h('div', { class: 'tasks__heading' },
      h('div', { class: 'tasks__title', role: 'heading', 'aria-level': '1' }, 'Задачи'),
      h('p', { class: 'tasks__intro small muted' },
        'Задача идёт по четырём колонкам. «Ждёт вас» — метка на карточке: у задачи есть открытые вопросы. '
        + 'Принятый итог замораживается и остаётся в памяти комнаты.')),
    h('div', { class: 'tasks__actions row row--tight' },
      segmented({
        options: MODES, value: state.mode,
        onchange: (v) => {
          state.mode = String(v) === 'list' ? 'list' : 'board';
          lsSet(MODE_KEY, state.mode);
          paint();
        },
      }),
      h('button', {
        class: 'btn btn--primary btn--sm', type: 'button', 'data-act': 'task-new',
        title: 'Написать новую задачу — фокус на поле «Задача команде»',
        onclick: () => focusComposer(),
      }, icon('plus', { size: 13 }), 'Новая задача'),
      h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        title: 'Перечитать доску',
        onclick: () => { load(); },
      }, icon('refresh', { size: 13 }), 'Обновить')));

  /* ── «Задача команде»: тот же композер, что был вкладкой, но над доской ── */

  const composerBody = h('div', { class: 'tasks-compose__body' });
  const composeToggle = h('button', {
    class: 'btn btn--ghost btn--sm tasks-compose__toggle', type: 'button',
    onclick: () => {
      state.composeCollapsed = !state.composeCollapsed;
      lsSet(COMPOSE_KEY, state.composeCollapsed);
      paintCompose();
      if (!state.composeCollapsed) focusComposer();
    },
  });
  const composeHost = h('section', {
    class: 'tasks-compose', 'aria-labelledby': 'tasks-compose-title',
  }, h('div', { class: 'tasks-compose__head' },
    h('div', { class: 'tasks-compose__heading' },
      h('div', {
        class: 'tasks-compose__title', id: 'tasks-compose-title', role: 'heading', 'aria-level': '2',
      }, 'Задача команде'),
      h('p', { class: 'tasks-compose__hint tiny muted' },
        'Одна задача — один сводный ответ. Ctrl+Enter — отправить.')),
    composeToggle), composerBody);

  const noteHost = h('p', { class: 'tasks__note small muted' });
  const boardHost = h('div', { class: 'tasks__host' });
  const root = h('div', { class: 'view__inner tasks' }, head, composeHost, noteHost, boardHost);
  el.appendChild(root);

  function paintCompose() {
    composerBody.classList.toggle('hidden', state.composeCollapsed);
    composeHost.classList.toggle('tasks-compose--collapsed', state.composeCollapsed);
    composeToggle.replaceChildren(
      icon(state.composeCollapsed ? 'chevron-down' : 'chevron-up', { size: 12 }),
      h('span', null, state.composeCollapsed ? 'Развернуть' : 'Свернуть'));
    composeToggle.setAttribute('aria-expanded', state.composeCollapsed ? 'false' : 'true');
    composeToggle.setAttribute('title', state.composeCollapsed ? 'Развернуть поле задачи' : 'Свернуть поле задачи');
  }

  /** Смонтировать композер один раз (фоном: доска уже на экране). */
  async function ensureComposer() {
    if (composerInst || composerFailed || disposed) return composerInst;
    try {
      const factory = typeof ctx.composerMount === 'function'
        ? ctx.composerMount
        : async (target, c) => {
          const mod = await import('./composer.js');
          return mod.mount(target, c);
        };
      composerInst = await factory(composerBody, Object.assign({}, ctx, {
        room: ctx.room && ctx.room.id ? ctx.room : { id: roomId },
        embed: true,
        heading: false,
        onSent: () => {
          state.composeNote = 'Задача отправлена — она появится на доске';
          load({ quiet: true });
        },
      }));
    } catch (err) {
      composerFailed = true;
      composerBody.replaceChildren(h('div', { class: 'tasks-compose__fallback small muted' },
        'Поле задачи не загрузилось. Обновите страницу (Ctrl+R) — доска работает и без него.'));
      if (typeof console !== 'undefined' && console.warn) console.warn('[tasks] композер не смонтирован', err);
    }
    return composerInst;
  }

  /** «+ Новая задача» и «+» в колонках: развернуть поле и поставить фокус. */
  function focusComposer() {
    if (state.composeCollapsed) {
      state.composeCollapsed = false;
      lsSet(COMPOSE_KEY, false);
      paintCompose();
    }
    ensureComposer().then((inst) => {
      if (disposed) return;
      if (inst && typeof inst.focus === 'function') {
        try { inst.focus(); } catch { /* фокус не критичен */ }
      }
      if (composeHost && typeof composeHost.scrollIntoView === 'function') {
        composeHost.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    });
  }

  function openTask(task) {
    const href = taskHref(roomId, task.id);
    if (typeof ctx.navigate === 'function') ctx.navigate(href);
    else location.hash = href;
  }

  /** Действие над задачей: `move` из контракта 31. */
  function postMove(task, body) {
    return api.post('/api/requests/' + enc(task.id) + '/actions',
      Object.assign({ action: 'move' }, body || {}));
  }

  /**
   * «Принять» (перенос в «Готово»): отложено на 10 секунд общим `undo.js`.
   * Пока идёт отсчёт, на сервере ничего не менялось — «Вернуть» просто не
   * выполняет действие. Открытые вопросы закрываются вместе с приёмкой,
   * поэтому при них сначала спрашиваем подтверждение.
   */
  async function accept(task) {
    const questions = Math.max(0, Number(task.waiting_count) || 0);
    if (questions > 0) {
      const ok = await confirmDialog({
        title: 'Принять задачу?',
        text: 'У задачи ' + questions + ' ' + pluralRu(questions, ['открытый вопрос', 'открытых вопроса', 'открытых вопросов'])
          + '. При приёмке они закроются («задача принята») и уйдут из «Сегодня» и из счётчиков.',
        confirmLabel: 'Принять',
      });
      if (!ok) return;
    }
    const key = 'accept-' + task.id;
    let acceptWarning = '';
    state.pending.add(task.id);
    paint();
    undo.offer({
      text: 'Задача принята: ' + shortTitle(task.title),
      key,
      action: async () => {
        let res = await postMove(task, { to: 'done', confirm: true });
        // Сервер мог ответить «нужно подтверждение» — спрашиваем и повторяем.
        if (res && res.needs_confirm) {
          const ok = await confirmDialog({
            title: 'Принять задачу?',
            text: 'У задачи ' + Number(res.open_questions || questions)
              + ' открытых вопросов. При приёмке они закроются.',
            confirmLabel: 'Принять',
          });
          if (!ok) { state.pending.delete(task.id); return; }
          res = await postMove(task, { to: 'done', confirm: true });
        }
        acceptWarning = moveWarning(res);
        state.pending.delete(task.id);
      },
      onCommit: () => {
        state.pending.delete(task.id);
        if (acceptWarning) toast(acceptWarning, 'warn');
        else toast('Задача принята — итог записан в память комнаты', 'ok');
        load({ quiet: true });
      },
      onUndo: () => {
        state.pending.delete(task.id);
        toast('Принятие отменено — задача осталась в «На проверке»', 'info');
        paint();
      },
    });
  }

  function cancel(task) {
    state.pending.delete(task.id);
    undo.revert('accept-' + task.id);
    paint();
  }

  /** «Вернуть на доработку»: комментарий желателен, перенос сразу. */
  async function sendBack(task) {
    const comment = await promptDialog({
      title: 'Вернуть на доработку',
      label: 'Что исправить (увидит руководитель комнаты)',
      placeholder: 'Например: добавьте расчёт по двум каналам и сроки',
      multiline: true,
      submitLabel: 'Вернуть на доработку',
    });
    if (comment === null) return;
    try {
      const res = await postMove(task, { to: 'working', comment: String(comment || '').trim() });
      const warning = moveWarning(res);
      if (warning) toast(warning, 'warn');
      else toast('Задача возвращена на доработку', 'ok');
      await load({ quiet: true });
    } catch (err) {
      toast((err && err.message) || 'Не удалось вернуть задачу на доработку', 'danger');
    }
  }

  /** Остальные переносы контракта: на проверку и обратно в работу. */
  async function moveTo(task, to) {
    try {
      let res = await postMove(task, { to });
      if (res && res.needs_confirm) {
        const ok = await confirmDialog({
          title: 'Принять задачу?',
          text: 'У задачи ' + Number(res.open_questions || 0) + ' открытых вопросов. При приёмке они закроются.',
          confirmLabel: 'Принять',
        });
        if (!ok) return;
        res = await postMove(task, { to, confirm: true });
      }
      const warning = moveWarning(res);
      if (warning) toast(warning, 'warn');
      else {
        toast(to === 'review' ? 'Задача отправлена на проверку'
          : to === 'working' ? 'Задача возвращена в работу'
            : 'Готово', 'ok');
      }
      await load({ quiet: true });
    } catch (err) {
      toast((err && err.message) || 'Не удалось перенести задачу', 'danger');
    }
  }

  /** Один вход для мыши и клавиатуры: проверить перенос и выполнить. */
  function move(task, fromColumn, toColumn) {
    const outcome = moveOutcome(fromColumn, toColumn, task.allowed_moves);
    if (!outcome.ok) {
      if (!outcome.noop) toast(outcome.reason, 'warn');
      return;
    }
    if (outcome.accept) { accept(task); return; }
    if (outcome.comment) { sendBack(task); return; }
    moveTo(task, outcome.to);
  }

  function paint() {
    if (disposed) return;
    const summary = boardSummary(state.tasks);
    const parts = [
      summary.total + ' ' + pluralRu(summary.total, ['задача', 'задачи', 'задач']) + ' на доске',
      summary.waiting ? summary.waiting + ' ' + pluralRu(summary.waiting, ['ждёт', 'ждут', 'ждут']) + ' вашего ответа' : '',
      summary.review ? summary.review + ' на проверке' : '',
      summary.done ? summary.done + ' ' + pluralRu(summary.done, ['принята', 'принято', 'принято']) : '',
      summary.archived ? summary.archived + ' в архиве' : '',
    ].filter(Boolean);
    if (state.stale && state.loaded && !state.error) parts.push('обновляем…');
    if (state.composeNote) parts.push(state.composeNote);
    noteHost.textContent = summary.total || state.stale ? parts.join(' · ') : '';

    boardHost.replaceChildren();
    if (!state.loaded && !state.error) {
      boardHost.appendChild(skeleton(4));
      return;
    }
    if (state.error && !state.tasks.length) {
      boardHost.appendChild(errorBanner(state.error, () => load()));
      return;
    }
    const opts = {
      roomId,
      isPending: (id) => state.pending.has(String(id)),
      isGrabbed: (id) => state.grabbed === String(id),
      onOpen: openTask,
      onDrop: move,
      onMove: (task, to) => move(task, stageOf(task), to),
      onGrab: (task, grabbed) => {
        state.grabbed = grabbed ? String(task.id) : null;
        paint();
        if (grabbed) toast('Карточка взята: ← → перенести, Esc — отпустить', 'info');
      },
      onBlocked: (reason) => toast(reason, 'warn'),
      onCancel: cancel,
      onCreate: focusComposer,
    };
    boardHost.appendChild(state.mode === 'list' ? renderList(state.tasks, opts) : renderBoard(state.tasks, opts));
    // После переноса с клавиатуры карточка перерисована — возвращаем ей фокус.
    if (state.grabbed) {
      const card = boardHost.querySelector('[data-task="' + state.grabbed + '"]');
      if (card && typeof card.focus === 'function') card.focus();
    }
    if (summary.archived) {
      boardHost.appendChild(h('p', { class: 'tasks__archive small muted' },
        'В архиве задач: ' + summary.archived + '. ',
        h('a', { href: '#/room/' + enc(roomId) + '/archive' }, 'Открыть раздел «Архив»'),
        ' — они не идут по доске и не ждут вашего решения.'));
    }
    if (state.error && state.tasks.length) {
      boardHost.appendChild(errorBanner(state.error, () => load({ quiet: true })));
    }
  }

  async function load(opts) {
    if (disposed) return;
    const quiet = Boolean(opts && opts.quiet);
    const my = ++seq;
    if (!quiet) state.error = null;
    if (!quiet && !state.tasks.length) { state.loaded = false; paint(); }
    try {
      const data = await api.get('/api/rooms/' + enc(roomId) + '/tasks');
      if (disposed || my !== seq) return;
      state.tasks = normalizeTasks(data && data.tasks);
      state.loaded = true;
      state.stale = false;
      state.error = null;
      saveSnapshot(roomId, state.tasks);
    } catch (err) {
      if (disposed || my !== seq) return;
      state.error = err;
      state.loaded = true;
      state.stale = false;
    }
    paint();
  }

  paintCompose();
  paint();                       // прошлые данные видны сразу, без заглушек
  ensureComposer();              // поле задачи — фоном, доска уже на экране
  const timer = poll(() => load({ quiet: true }), POLL_MS, { pauseHidden: true, immediate: false });
  await load({ quiet: Boolean(snapshot) });
  if (ctx.route && ctx.route.query && ctx.route.query.focus === 'input') focusComposer();

  return {
    focusComposer,
    unmount() {
      disposed = true;
      try { timer.stop(); } catch { /* опрос уже остановлен */ }
      // Отложенное принятие не теряем: человек мог уйти на страницу задачи.
      for (const id of [...state.pending]) {
        try { undo.commit('accept-' + id); } catch { /* ничего не поделать */ }
      }
      if (composerInst && typeof composerInst.unmount === 'function') {
        try { composerInst.unmount(); } catch { /* уже снят */ }
      }
      root.remove();
    },
  };
}

/** Короткое название для тоста: не длиннее 48 знаков, по границе слова. */
function shortTitle(text) {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  if (s.length <= 48) return s;
  const cut = s.slice(0, 48);
  const sp = cut.lastIndexOf(' ');
  return (sp > 20 ? cut.slice(0, sp) : cut).replace(/[\s.,:;—-]+$/, '') + '…';
}

export default { mount, renderBoard, renderList, VIEW_CSS, POLL_MS, DRAG_THRESHOLD, cacheKey, readSnapshot, saveSnapshot };
