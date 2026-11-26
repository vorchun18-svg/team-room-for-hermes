/**
 * tasks-board.js — комната, вкладка «Задачи» (W30-D2): канбан задач комнаты.
 *
 * Экран отвечает на один вопрос: «где сейчас каждая задача и что нужно от меня».
 * Колонки — стадии контракта §3 волны 30 (серверная часть — пакет D1):
 *   Новая → В работе → Ждёт вас → На проверке → Готово → Принята (+ «Архив»).
 *
 * Правила, которые здесь соблюдаются:
 *   • перенос карточки = только существующее действие сервера: «Принять»
 *     (`accept`) из «Готово» и «Вернуть в работу» (`reopen_accepted`) из
 *     «Принята». Остальные переходы делает работа агентов и ответы человека —
 *     доска объясняет это словами, а не молчаливым отказом (`moveOutcome`);
 *   • принятие отложено на 10 секунд общим компонентом `undo.js`: пока идёт
 *     отсчёт, на сервере ничего не менялось, «Вернуть» просто не выполняет
 *     действие, а в карточке видна рабочая кнопка «Отменить»;
 *   • счётчик колонки = числу карточек в ней (считаем по списку, не по ответу);
 *   • «Принята» по умолчанию свёрнута, счётчик виден всегда, выбор помнится;
 *   • пустая колонка объясняет, зачем она и что сделать сейчас; «+ Новая
 *     задача» открывает композер комнаты (вкладка «Запрос»);
 *   • никакого `innerHTML` из данных: всё через `h()`; цвета — токенами темы.
 *
 * Данные: GET /api/rooms/{room}/tasks (контракт §3).
 * Действия: POST /api/requests/{id}/actions {action:'accept'|'reopen_accepted'}.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { poll } from '../api.js';
import { ensureCss, emptyState, errorBanner, lsGet, lsSet, segmented, skeleton } from '../ui.js';
import { undo } from '../undo.js';
import {
  BOARD_COLUMNS, ageText, boardColumns, boardSummary, keyboardTarget, listGroups, moveOutcome,
  normalizeTasks, pluralRu, stageOf, stepsPercent, stepsText, taskHref, waitingOf,
} from './tasks-model.js';

export const VIEW_CSS = 'css/view-tasks.css';
export const POLL_MS = 20000;
export const DRAG_THRESHOLD = 4;

const MODE_KEY = 'teamroom.tasks.mode';
const COLLAPSE_KEY = 'teamroom.tasks.acceptedCollapsed';
const MODES = [
  { value: 'board', label: 'Доска' },
  { value: 'list', label: 'Список' },
];

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/* ─────────────────────────────────────────────────────────────── отрисовка */

/** Кнопка действия карточки (не открывает задачу: клик гасим). */
function cardButton(label, cls, title, onClick) {
  return h('button', {
    class: ['btn', 'btn--sm', cls, 'tasks-card__btn'],
    type: 'button',
    title,
    onclick: (ev) => {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
      onClick();
    },
  }, label);
}

/** Кнопка «Принять» / «Отменить» / «Вернуть в работу» — по стадии и состоянию. */
function cardActions(task, opts) {
  const stage = stageOf(task);
  const pending = typeof opts.isPending === 'function' ? opts.isPending(task.id) : false;
  if (pending) {
    return cardButton('Отменить', 'btn--ghost', 'Отменить принятие — задача останется в «Готово»',
      () => opts.onCancel(task));
  }
  if (stage === 'done_system' && opts.onAccept) {
    return cardButton('Принять', 'btn--primary', 'Принять итог: задача замораживается, в память комнаты пишется паспорт. Отменить можно 10 секунд',
      () => opts.onAccept(task));
  }
  if (stage === 'accepted' && opts.onReopen) {
    return cardButton('Вернуть в работу', 'btn--secondary', 'Вернуть принятую задачу в работу — откроется снова для агентов',
      () => opts.onReopen(task));
  }
  return null;
}

/** Карточка задачи на доске: название, ответственный, шаги, что ждёт, возраст. */
function cardNode(task, columnId, opts, nowMs) {
  const wait = waitingOf(task);
  const steps = stepsText(task);
  const pct = stepsPercent(task);
  const age = ageText(task.updated_at, nowMs);
  const pending = typeof opts.isPending === 'function' ? opts.isPending(task.id) : false;
  const title = String(task.title || 'Без названия');

  const card = h('div', {
    class: ['tasks-card', 'tasks-card--' + wait.tone, pending ? 'tasks-card--pending' : ''],
    'data-task': task.id,
    'data-stage': columnId,
    role: 'link',
    tabindex: '0',
    'aria-label': title + ' · колонка «' + (BOARD_COLUMNS.find((c) => c.id === columnId) || {}).title + '»'
      + (task.children_count ? ' · подзадач: ' + task.children_count : ''),
    title: 'Открыть задачу. Стрелки ← → переносят карточку, если сервер это умеет.',
  },
  h('div', { class: 'tasks-card__top' },
    h('span', { class: 'tasks-card__title' }, title),
    Number(task.children_count) ? h('span', {
      class: 'chip chip--outline tasks-card__kids', title: 'Подзадач в проекте: ' + task.children_count,
    }, icon('git-branch', { size: 10 }), String(task.children_count)) : null),
  h('div', { class: 'tasks-card__meta small muted' },
    task.assignee ? h('span', { class: 'tasks-card__who' }, icon('person', { size: 11 }), task.assignee) : null,
    steps ? h('span', { class: 'tasks-card__steps' }, steps) : null),
  steps ? h('div', {
    class: 'tasks-card__bar', role: 'progressbar', 'aria-valuenow': String(pct),
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': steps,
  }, h('span', { class: 'tasks-card__bar-fill', style: 'width:' + pct + '%' })) : null,
  h('div', { class: ['tasks-card__wait', 'tiny', 'tasks-card__wait--' + wait.tone] }, wait.text),
  h('div', { class: 'tasks-card__foot' },
    h('span', { class: 'tasks-card__age tiny muted' }, age ? 'обновлено ' + age : ''),
    pending ? h('span', { class: 'tasks-card__pending tiny' }, 'принимается…') : null,
    cardActions(task, opts)));

  // Перетаскивание — указателем (мышь, перо, палец), как на доске «Дел».
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
    dragging = false;
    suppressClick = false;
    startX = ev.clientX;
    startY = ev.clientY;
    try { card.setPointerCapture(ev.pointerId); } catch { /* старые браузеры */ }
  });

  card.addEventListener('pointermove', (ev) => {
    if (!card.hasPointerCapture || !card.hasPointerCapture(ev.pointerId)) return;
    const dx = Math.abs(ev.clientX - startX);
    const dy = Math.abs(ev.clientY - startY);
    if (!dragging && dx < DRAG_THRESHOLD && dy < DRAG_THRESHOLD) return;
    if (!dragging) {
      dragging = true;
      card.classList.add('tasks-card--dragging');
      document.body.classList.add('tasks-dragging');
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
    const outcome = moveOutcome(columnId, target);
    if (outcome.ok) opts.onMove(task, outcome, columnId, target);
    else if (!outcome.noop) opts.onBlocked(outcome.reason, task);
  };

  card.addEventListener('pointerup', (ev) => finish(ev, false));
  card.addEventListener('pointercancel', (ev) => finish(ev, true));
  card.addEventListener('click', (ev) => {
    if (suppressClick) { suppressClick = false; ev.preventDefault(); ev.stopPropagation(); return; }
    if (dragging) { ev.preventDefault(); return; }
    opts.onOpen(task);
  });
  card.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opts.onOpen(task); return; }
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const target = keyboardTarget(columnId, ev.key === 'ArrowLeft' ? -1 : 1);
    if (!target) { opts.onBlocked('Дальше по доске задача не переносится: таких действий у сервера нет.', task); return; }
    const outcome = moveOutcome(columnId, target);
    if (outcome.ok) opts.onMove(task, outcome, columnId, target);
    else opts.onBlocked(outcome.reason, task);
  });
  return card;
}

/** Колонка: шапка со счётчиком, тело для переноса, пояснение пустоты. */
function columnNode(col, opts, nowMs) {
  const collapsed = Boolean(col.collapsible) && opts.acceptedCollapsed !== false;
  const head = h('div', { class: 'tasks-bcol__head' },
    h('span', { class: 'tasks-bcol__title' },
      h('span', { class: ['tasks-bcol__dot', 'tasks-bcol__dot--' + col.id], 'aria-hidden': 'true' }),
      col.title,
      h('span', { class: 'tasks-bcol__count tnum' }, String(col.count)),
      collapsed ? h('span', { class: 'tasks-bcol__collapsed tiny muted' }, 'свёрнуто') : null),
    col.collapsible
      ? h('button', {
        class: 'btn btn--ghost btn--sm tasks-bcol__toggle', type: 'button',
        'data-act': collapsed ? 'expand' : 'collapse',
        title: collapsed ? 'Показать принятые задачи' : 'Свернуть принятые задачи',
        'aria-expanded': collapsed ? 'false' : 'true',
        onclick: () => opts.onToggleAccepted(),
      }, icon(collapsed ? 'chevron-down' : 'chevron-up', { size: 12 }), collapsed ? 'Показать' : 'Свернуть')
      : null,
    col.create && opts.allowCreate !== false
      ? h('button', {
        class: 'btn btn--sm btn--ghost tasks-bcol__add', type: 'button',
        'data-act': 'task-new', title: 'Поставить новую задачу комнаты',
        onclick: () => opts.onCreate(),
      }, icon('plus', { size: 12 }), 'Новая')
      : null);

  const body = h('div', {
    class: 'tasks-bcol__body',
    'data-column-body': col.id,
    role: 'list',
    'aria-label': 'Колонка «' + col.title + '»',
  });
  if (collapsed) {
    body.classList.add('hidden');
  } else if (col.items.length) {
    for (const task of col.items) body.appendChild(cardNode(task, col.id, opts, nowMs));
  } else {
    body.appendChild(h('p', { class: 'tasks-bcol__empty small muted' }, col.empty));
  }

  return h('section', {
    class: ['tasks-bcol', 'tasks-bcol--' + col.id, collapsed ? 'tasks-bcol--collapsed' : ''],
    'data-column': col.id,
  }, head, body,
  col.items.length ? null : h('p', { class: 'tasks-bcol__note tiny muted' }, col.hint));
}

/**
 * Доска целиком.
 * @param {object[]} tasks уже нормализованные задачи
 * @param {{onOpen:Function,onMove:Function,onBlocked:Function,onAccept?:Function,
 *          onReopen?:Function,onCancel?:Function,onCreate?:Function,onToggleAccepted?:Function,
 *          isPending?:Function,acceptedCollapsed?:boolean,allowCreate?:boolean,nowMs?:number}} opts
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
      text: 'Задачи комнаты идут по стадиям: новая → в работе → ждёт вас → на проверке → готово → принята. '
        + 'Поставьте первую — руководитель оценит её, распределит работу между участниками, '
        + 'а вы примете итог, когда работа будет готова.',
      action: o.allowCreate === false ? null
        : { label: 'Поставить задачу', variant: 'primary', onclick: () => o.onCreate && o.onCreate() },
    });
  }
  return h('div', { class: 'tasks-board', role: 'group', 'aria-label': 'Задачи комнаты по стадиям' },
    ...columns.map((col) => columnNode(col, o, nowMs)));
}

/** Строка режима «Список»: та же карточка, но читаемой строкой. */
function rowNode(task, opts, nowMs) {
  const wait = waitingOf(task);
  const steps = stepsText(task);
  const age = ageText(task.updated_at, nowMs);
  const pending = typeof opts.isPending === 'function' ? opts.isPending(task.id) : false;
  const href = taskHref(opts.roomId, task.id);
  return h('article', {
    class: ['tasks-row', 'tasks-row--' + wait.tone, pending ? 'tasks-row--pending' : ''],
    role: 'listitem', 'data-task': task.id, 'data-stage': stageOf(task),
  },
  h('div', { class: 'tasks-row__main' },
    h('div', { class: 'tasks-row__titleline' },
      h('a', { class: 'tasks-row__title', href, title: task.title }, task.title),
      h('span', { class: 'chip chip--outline tasks-row__stage' }, (BOARD_COLUMNS.find((c) => c.id === stageOf(task)) || {}).title || ''),
      pending ? h('span', { class: 'chip chip--warn' }, 'принимается…') : null),
    h('div', { class: 'tasks-row__meta small muted' },
      task.assignee ? h('span', null, icon('person', { size: 11 }), task.assignee) : null,
      steps ? h('span', null, steps) : null,
      h('span', { class: 'tasks-row__wait' }, wait.text),
      age ? h('span', null, 'обновлено ' + age) : null)),
  h('div', { class: 'tasks-row__side' },
    h('a', { class: 'btn btn--ghost btn--sm', href }, 'Открыть'),
    cardActions(task, opts)));
}

/** Список: группы по стадиям с заголовками и счётчиками. */
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

  const state = {
    tasks: [],
    mode: String(lsGet(MODE_KEY, 'board') || 'board') === 'list' ? 'list' : 'board',
    acceptedCollapsed: lsGet(COLLAPSE_KEY, true) !== false,
    pending: new Set(),
    loaded: false,
    error: null,
  };
  let disposed = false;
  let seq = 0;
  let filterEl = null;

  const head = h('div', { class: 'tasks__head' },
    h('div', { class: 'tasks__heading' },
      h('div', { class: 'tasks__title', role: 'heading', 'aria-level': '1' }, 'Задачи'),
      h('p', { class: 'tasks__intro small muted' },
        'Каждая задача комнаты идёт по стадиям. Принятая задача замораживается: работа по ней'
        + ' больше не идёт, а итог («паспорт») остаётся в памяти комнаты.')),
    h('div', { class: 'tasks__actions row row--tight' },
      filterEl = segmented({
        options: MODES, value: state.mode,
        onchange: (v) => {
          state.mode = String(v) === 'list' ? 'list' : 'board';
          lsSet(MODE_KEY, state.mode);
          paint();
        },
      }),
      h('button', {
        class: 'btn btn--primary btn--sm', type: 'button', 'data-act': 'task-new',
        title: 'Поставить новую задачу — откроется композер комнаты',
        onclick: () => goComposer(),
      }, icon('plus', { size: 13 }), 'Новая задача'),
      h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        title: 'Перечитать доску',
        onclick: () => { load(); },
      }, icon('refresh', { size: 13 }), 'Обновить')));

  const noteHost = h('p', { class: 'tasks__note small muted' });
  const boardHost = h('div', { class: 'tasks__host' });
  const root = h('div', { class: 'view__inner tasks' }, head, noteHost, boardHost);
  el.appendChild(root);

  function goComposer() {
    if (typeof ctx.navigate === 'function') ctx.navigate('#/room/' + enc(roomId) + '/composer');
    else location.hash = '#/room/' + enc(roomId) + '/composer';
  }

  function openTask(task) {
    const href = taskHref(roomId, task.id);
    if (typeof ctx.navigate === 'function') ctx.navigate(href);
    else location.hash = href;
  }

  /** Действие над задачей: существующая ручка `/api/requests/{id}/actions`. */
  function postAction(task, action, extra) {
    return api.post('/api/requests/' + enc(task.id) + '/actions',
      Object.assign({ action: String(action) }, extra || {}));
  }

  /**
   * «Принять»: действие ОТЛОЖЕНО на 10 секунд общим `undo.js`. Пока идёт
   * отсчёт, на сервере ничего не менялось — «Вернуть» (тост или кнопка в
   * карточке) просто не выполняет действие.
   */
  function accept(task) {
    const key = 'accept-' + task.id;
    state.pending.add(task.id);
    paint();
    undo.offer({
      text: 'Задача принята: ' + shortTitle(task.title),
      key,
      action: async () => {
        await postAction(task, 'accept');
        state.pending.delete(task.id);
      },
      onCommit: () => {
        state.pending.delete(task.id);
        toast('Задача принята — итог записан в память комнаты', 'ok');
        load({ quiet: true });
      },
      onUndo: () => {
        state.pending.delete(task.id);
        toast('Принятие отменено — задача осталась в «Готово»', 'info');
        paint();
      },
    });
  }

  function cancel(task) {
    state.pending.delete(task.id);
    undo.revert('accept-' + task.id);
    paint();
  }

  async function reopen(task) {
    try {
      await postAction(task, 'reopen_accepted');
      toast('Задача возвращена в работу', 'ok');
      await load({ quiet: true });
    } catch (err) {
      toast((err && err.message) || 'Не удалось вернуть задачу в работу', 'danger');
    }
  }

  function move(task, outcome) {
    if (outcome.action === 'accept') accept(task);
    else if (outcome.action === 'reopen_accepted') reopen(task);
    else toast('Такого переноса у сервера нет', 'warn');
  }

  function paint() {
    if (disposed) return;
    const summary = boardSummary(state.tasks);
    noteHost.textContent = summary.total
      ? [
        summary.total + ' на доске',
        summary.waiting ? summary.waiting + ' ' + pluralRu(summary.waiting, ['ждёт', 'ждут', 'ждут']) + ' вашего ответа' : '',
        summary.ready ? summary.ready + ' ' + pluralRu(summary.ready, ['готова', 'готовы', 'готово']) + ' к приёмке' : '',
        summary.accepted ? summary.accepted + ' ' + pluralRu(summary.accepted, ['принята', 'принято', 'принято']) : '',
        summary.archived ? summary.archived + ' в архиве' : '',
      ].filter(Boolean).join(' · ')
      : '';

    boardHost.replaceChildren();
    if (!state.loaded && !state.error) {
      boardHost.appendChild(skeleton(4));
      return;
    }
    if (state.error) {
      boardHost.appendChild(errorBanner(state.error, () => load()));
      return;
    }
    const opts = {
      roomId,
      acceptedCollapsed: state.acceptedCollapsed,
      isPending: (id) => state.pending.has(String(id)),
      onOpen: openTask,
      onMove: move,
      onBlocked: (reason) => toast(reason, 'warn'),
      onAccept: accept,
      onReopen: (task) => { reopen(task); },
      onCancel: cancel,
      onCreate: goComposer,
      onToggleAccepted: () => {
        state.acceptedCollapsed = !state.acceptedCollapsed;
        lsSet(COLLAPSE_KEY, state.acceptedCollapsed);
        paint();
      },
    };
    boardHost.appendChild(state.mode === 'list' ? renderList(state.tasks, opts) : renderBoard(state.tasks, opts));

    if (summary.archived) {
      boardHost.appendChild(h('p', { class: 'tasks__archive small muted' },
        'В архиве задач: ' + summary.archived + '. ',
        h('a', { href: '#/room/' + enc(roomId) + '/archive' }, 'Открыть раздел «Архив»'),
        ' — они не идут по доске и не ждут вашего решения.'));
    }
  }

  async function load(opts) {
    if (disposed) return;
    const quiet = Boolean(opts && opts.quiet);
    const my = ++seq;
    if (!quiet) { state.error = null; if (!state.tasks.length) state.loaded = false; paint(); }
    try {
      const data = await api.get('/api/rooms/' + enc(roomId) + '/tasks');
      if (disposed || my !== seq) return;
      state.tasks = normalizeTasks(data && data.tasks);
      state.loaded = true;
      state.error = null;
    } catch (err) {
      if (disposed || my !== seq) return;
      state.error = err;
      state.loaded = true;
    }
    paint();
  }

  // Опрос: обновляем доску, не перебивая ожидающие принятия (они видны по `pending`).
  const timer = poll(() => load({ quiet: true }), POLL_MS, { pauseHidden: true, immediate: false });
  await load();

  return {
    unmount() {
      disposed = true;
      try { timer.stop(); } catch { /* опрос уже остановлен */ }
      // Отложенное принятие не теряем: человек мог уйти на страницу задачи.
      for (const id of [...state.pending]) {
        try { undo.commit('accept-' + id); } catch { /* ничего не поделать */ }
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

export default { mount, renderBoard, renderList, VIEW_CSS, POLL_MS, DRAG_THRESHOLD };
