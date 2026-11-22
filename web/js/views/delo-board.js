/**
 * delo-board.js — «Доска» экрана «Дела» (W29B-Q5): колонки по статусам.
 *
 * Здесь чистые функции (колонки, порядок, правила переноса, клавиатура) и
 * отрисовка. Чистые функции проверяются без браузера (`tests/delo-board.test.mjs`),
 * поэтому вьюха `delo.js` только отдаёт данные и действия.
 *
 * Правила, которые здесь соблюдаются:
 *   • колонка = настоящий статус дела (`derived_status` сервера), своих статусов
 *     интерфейс не выдумывает;
 *   • перенос между колонками — только существующее действие сервера
 *     (`pause`, `resume`, `done`, `reopen`); там, где действия нет, колонка честно
 *     объясняет, почему дело туда не перетаскивается (`dragOutcome`);
 *   • у каждой колонки есть «+ Дело» либо фраза, как дело в неё попадает — пустых
 *     мест без выхода нет;
 *   • перетаскивание — указателем (Pointer Events): работает мышью, пером и
 *     пальцем, и его можно проверить живой мышью в Chromium; клавиатура —
 *     ArrowLeft/ArrowRight на карточке, Enter открывает дело;
 *   • цвета — только токенами (`var(--…)`), обе темы.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { STATUS_LABEL, STATUS_TONE, dueInfo, sortDelos } from './delo-model.js';

/** Порог, после которого нажатие считается перетаскиванием, а не кликом. */
export const DRAG_THRESHOLD = 4;

/**
 * Колонки доски. `create` — статус, с которым создаётся дело из этой колонки
 * (нет — значит создать дело сразу в этом статусе нельзя, и колонка об этом скажет).
 * @type {{id:string,title:string,hint:string,tone:string,create:string|null,accepts:string|null}[]}
 */
export const BOARD_COLUMNS = [
  {
    id: 'open', title: 'В работе', tone: STATUS_TONE.open,
    hint: 'Дело идёт само — от вас ничего не нужно.',
    create: 'open', accepts: 'вернуть в работу из «На паузе», «Закрыто» или «Отменено»',
  },
  {
    id: 'waiting', title: 'Ждёт ответа', tone: STATUS_TONE.waiting,
    hint: 'Дело чего-то ждёт: ответа человека, письма, чужого шага.',
    create: null, accepts: null,
  },
  {
    id: 'paused', title: 'На паузе', tone: STATUS_TONE.paused,
    hint: 'Дело остановлено вами — работа не идёт.',
    create: 'paused', accepts: 'поставить на паузу',
  },
  {
    id: 'done', title: 'Закрыто', tone: STATUS_TONE.done,
    hint: 'Дело доведено до конца.',
    create: 'done', accepts: 'закрыть дело',
  },
  {
    id: 'cancelled', title: 'Отменено', tone: STATUS_TONE.cancelled,
    hint: 'Дело отменено при создании — работы по нему не было.',
    create: null, accepts: null,
  },
];

const COLUMN_BY_ID = new Map(BOARD_COLUMNS.map((c) => [c.id, c]));

/** Колонка дела по его статусу; неизвестный статус считаем «в работе». */
export function columnOf(delo) {
  const raw = String((delo && delo.status) || 'open');
  return COLUMN_BY_ID.has(raw) ? raw : 'open';
}

/** Статус для «+ Дело» в колонке; `null` — создать сразу в этом статусе нельзя. */
export function createStatusFor(columnId) {
  const col = COLUMN_BY_ID.get(String(columnId));
  return col ? col.create : null;
}

/**
 * Что произойдёт, если перетащить дело из колонки `fromId` в колонку `toId`.
 * Разрешены только действия, которые сервер действительно умеет (`ACTIONS`).
 * @returns {{ok:true, action:string}|{ok:false, reason:string, noop?:boolean}}
 */
export function dragOutcome(fromId, toId) {
  const from = String(fromId || '');
  const to = String(toId || '');
  if (!COLUMN_BY_ID.has(to)) return { ok: false, reason: 'Такой колонки нет на доске.' };
  if (from === to) return { ok: false, noop: true, reason: 'Дело уже в этой колонке.' };
  if (to === 'paused') return { ok: true, action: 'pause' };
  if (to === 'done') return { ok: true, action: 'done' };
  if (to === 'waiting') {
    return {
      ok: false,
      reason: '«Ждёт ответа» ставится ожиданием, а не переносом: откройте дело и добавьте, кого ждём.',
    };
  }
  if (to === 'cancelled') {
    return {
      ok: false,
      reason: 'Отменить дело нельзя — такого действия нет. Закройте его или верните в работу.',
    };
  }
  // to === 'open'
  if (from === 'paused') return { ok: true, action: 'resume' };
  if (from === 'done' || from === 'cancelled') return { ok: true, action: 'reopen' };
  if (from === 'waiting') {
    return {
      ok: false,
      reason: 'Дело ждёт ответа: снимите ожидание в карточке дела — и оно вернётся в работу.',
    };
  }
  return { ok: false, reason: 'Так перенести дело нельзя.' };
}

/**
 * Соседняя колонка, которая примет дело с клавиатуры: колонки без действия
 * («Ждёт ответа», «Отменено») пропускаем, чтобы стрелка не вела в отказ.
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
    const outcome = dragOutcome(from, BOARD_COLUMNS[i].id);
    if (outcome.ok) return BOARD_COLUMNS[i].id;
  }
  return null;
}

/** Колонки с делами: порядок колонок постоянный, пустые тоже показываем. */
export function boardColumns(items) {
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  const buckets = new Map(BOARD_COLUMNS.map((c) => [c.id, []]));
  for (const delo of list) buckets.get(columnOf(delo)).push(delo);
  return BOARD_COLUMNS.map((col) => ({
    id: col.id,
    title: col.title,
    hint: col.hint,
    tone: col.tone,
    create: col.create,
    accepts: col.accepts,
    items: sortDelos(buckets.get(col.id)),
    count: buckets.get(col.id).length,
  }));
}

/** Счётчики шапки доски: всего, ждут решения, просрочено. */
export function boardSummary(items, nowMs) {
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  return {
    total: list.length,
    needs: list.reduce((sum, d) => sum + (Array.isArray(d.needs_you) ? d.needs_you.length : 0), 0),
    overdue: list.filter((d) => dueInfo(d && d.due, nowMs).overdue).length,
    done: list.filter((d) => columnOf(d) === 'done').length,
  };
}

/* ─────────────────────────────────────────────────────────────── отрисовка */

/** Карточка дела на доске. */
function cardNode(delo, columnId, opts, nowMs) {
  const needs = Array.isArray(delo.needs_you) ? delo.needs_you.length : 0;
  const due = dueInfo(delo.due, nowMs);
  const stages = Array.isArray(delo.stages) ? delo.stages.filter((s) => s && s.state === 'now') : [];
  const roomTitle = typeof opts.roomTitle === 'function' ? opts.roomTitle(delo.room) : '';
  const title = String(delo.title || 'Без названия');
  const card = h('div', {
    class: ['delo-bcard', needs ? 'delo-bcard--needs' : '', due.overdue ? 'delo-bcard--overdue' : ''],
    'data-delo': delo.id,
    'data-column': columnId,
    role: 'link',
    tabindex: '0',
    'aria-label': title + (needs ? ', ждёт вашего решения: ' + needs : '')
      + ' · колонка «' + (COLUMN_BY_ID.get(columnId) || {}).title + '»',
    title: 'Открыть дело. Стрелки ← → переносят дело между колонками.',
  },
  h('div', { class: 'delo-bcard__top' },
    h('span', { class: 'delo-bcard__title' }, title),
    needs ? h('span', { class: 'chip chip--warn delo-bcard__needs', title: 'Ждёт вашего решения' },
      icon('bell', { size: 11 }), String(needs)) : null),
  h('div', { class: 'delo-bcard__meta small muted' },
    roomTitle ? h('span', { class: 'delo-bcard__room' }, roomTitle) : null,
    h('span', { class: ['chip', 'chip--' + (due.tone === 'muted' ? 'outline' : due.tone), 'delo-bcard__due'] }, due.text)),
  stages.length ? h('div', { class: 'delo-bcard__stage tiny muted' }, '● ' + String(stages[0].title || '')) : null);

  let dragging = false;
  let suppressClick = false;
  let startX = 0;
  let startY = 0;
  let overCol = null;

  const clearOver = () => {
    if (overCol) {
      const el = document.querySelector('[data-column-body="' + overCol + '"]');
      if (el) el.classList.remove('delo-bcol--over');
      overCol = null;
    }
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
      card.classList.add('delo-bcard--dragging');
      document.body.classList.add('delos-dragging');
    }
    const under = document.elementFromPoint(ev.clientX, ev.clientY);
    const body = under && under.closest ? under.closest('[data-column-body]') : null;
    const colId = body ? body.getAttribute('data-column-body') : null;
    if (colId !== overCol) {
      clearOver();
      if (body && colId !== columnId) {
        overCol = colId;
        body.classList.add('delo-bcol--over');
      }
    }
  });

  const finish = (ev, cancelled) => {
    if (card.hasPointerCapture && card.hasPointerCapture(ev.pointerId)) {
      try { card.releasePointerCapture(ev.pointerId); } catch { /* уже отпущено */ }
    }
    const target = overCol;
    clearOver();
    card.classList.remove('delo-bcard--dragging');
    document.body.classList.remove('delos-dragging');
    if (!dragging) return;                     // обычный клик — откроем дело
    dragging = false;
    // После переноса браузер всё равно шлёт click — иначе карточка открылась бы
    // вместо смены колонки. Гасим ровно один клик.
    suppressClick = true;
    if (cancelled || !target) return;
    const outcome = dragOutcome(columnId, target);
    if (outcome.ok) opts.onMove(delo, outcome, columnId, target);
    else if (!outcome.noop) opts.onBlocked(outcome.reason, delo);
  };

  card.addEventListener('pointerup', (ev) => finish(ev, false));
  card.addEventListener('pointercancel', (ev) => finish(ev, true));
  card.addEventListener('click', (ev) => {
    if (suppressClick) { suppressClick = false; ev.preventDefault(); ev.stopPropagation(); return; }
    if (dragging) { ev.preventDefault(); return; }
    opts.onOpen(delo);
  });
  card.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opts.onOpen(delo); return; }
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const target = keyboardTarget(columnId, ev.key === 'ArrowLeft' ? -1 : 1);
    if (!target) { opts.onBlocked('Дальше по доске дело не переносится.', delo); return; }
    const outcome = dragOutcome(columnId, target);
    if (outcome.ok) opts.onMove(delo, outcome, columnId, target);
    else opts.onBlocked(outcome.reason, delo);
  });
  return card;
}

/** Колонка: шапка со счётчиком, «+ Дело» или честная фраза, тело для переноса. */
function columnNode(col, opts, nowMs) {
  const canAdd = Boolean(col.create) && opts.allowCreate !== false;
  const head = h('div', { class: 'delo-bcol__head' },
    h('span', { class: 'delo-bcol__title' },
      h('span', { class: ['delo-bcol__dot', 'delo-bcol__dot--' + col.id], 'aria-hidden': 'true' }),
      col.title,
      h('span', { class: 'delo-bcol__count tnum' }, String(col.count))),
    canAdd
      ? h('button', {
        class: 'btn btn--sm btn--ghost delo-bcol__add', type: 'button',
        'data-act': 'delo-new', 'data-status': col.create,
        title: 'Новое дело в колонке «' + col.title + '»',
        onclick: () => openCreate(col.id, opts),
      }, icon('plus', { size: 12 }), 'Дело')
      : null);

  const body = h('div', {
    class: 'delo-bcol__body',
    'data-column-body': col.id,
    role: 'list',
    'aria-label': 'Колонка «' + col.title + '»',
  });

  if (col.items.length) {
    for (const delo of col.items) body.appendChild(cardNode(delo, col.id, opts, nowMs));
  } else {
    body.appendChild(h('p', { class: 'delo-bcol__empty small muted' },
      canAdd
        ? 'Пусто. Создайте дело кнопкой «+ Дело» или перетащите сюда карточку.'
        : 'Пусто — переносом дело сюда не попадает.'));
  }

  const foot = canAdd ? null : h('p', { class: 'delo-bcol__note tiny muted' }, col.hint);
  return h('section', { class: ['delo-bcol', 'delo-bcol--' + col.id], 'data-column': col.id },
    head, body, foot);
}

/** Мини-форма «+ Дело» прямо в колонке (без окон и прыжков). */
function openCreate(columnId, opts) {
  const body = document.querySelector('[data-column-body="' + columnId + '"]');
  if (!body || body.querySelector('.delo-bcreate')) {
    const field = body ? body.querySelector('.delo-bcreate input') : null;
    if (field) field.focus();
    return;
  }
  const input = h('input', {
    class: 'delo-bcreate__input', type: 'text', maxlength: '200',
    placeholder: 'Название дела', 'aria-label': 'Название нового дела',
  });
  const form = h('form', { class: 'delo-bcreate', 'data-status': columnId },
    input,
    h('div', { class: 'delo-bcreate__btns' },
      h('button', { class: 'btn btn--sm btn--primary', type: 'submit' }, 'Создать'),
      h('button', {
        class: 'btn btn--sm btn--ghost', type: 'button',
        onclick: () => form.remove(),
      }, 'Отмена')));
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const title = String(input.value || '').trim();
    if (!title) { opts.onBlocked('Впишите название дела — без него не создаём.', null); input.focus(); return; }
    form.remove();
    opts.onCreate(columnId, title);
  });
  input.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); form.remove(); } });
  body.appendChild(form);
  input.focus();
}

/**
 * Доска целиком.
 * @param {object[]} items дела (уже нормализованные `normalizeDelo`)
 * @param {{onMove:Function, onBlocked:Function, onOpen:Function, onCreate:Function,
 *          roomTitle?:Function, nowMs?:number}} opts
 */
export function renderBoard(items, opts) {
  const nowMs = Number.isFinite(Number(opts && opts.nowMs)) ? Number(opts.nowMs) : Date.now();
  const columns = boardColumns(items);
  const summary = boardSummary(items, nowMs);
  const nodes = columns.map((col) => columnNode(col, opts, nowMs));
  const board = h('div', {
    class: 'delo-board', role: 'group', 'aria-label': 'Доска дел по статусам',
  }, ...nodes);
  if (summary.total) return board;
  return h('div', { class: 'delo-board-wrap' },
    h('div', { class: 'card delo-empty' },
      icon('layout', { size: 20 }),
      h('div', { class: 'delo-empty__title' }, 'Досок пока не из чего собирать'),
      h('p', { class: 'small muted' },
        opts && opts.allowCreate === false
          ? 'Доска показывает дела по состоянию: в работе, ждёт ответа, на паузе, закрыто. '
            + 'Дела ведутся в комнате — откройте «Дела» комнаты и нажмите «+ Дело».'
          : 'Доска показывает дела по состоянию: в работе, ждёт ответа, на паузе, закрыто. '
            + 'Создайте первое дело кнопкой «+ Дело» в колонке «В работе» — и колонки оживут.')),
    board);
}

export default { BOARD_COLUMNS, DRAG_THRESHOLD, boardColumns, boardSummary, columnOf, createStatusFor, dragOutcome, keyboardTarget, renderBoard };
