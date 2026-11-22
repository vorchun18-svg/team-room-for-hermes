/**
 * delo-timeline.js — «Время» экрана «Дела» (W29B-Q5): сроки дел по неделям и месяцам.
 *
 * Как и `delo-board.js`, файл делится на чистые функции (диапазон, ячейки, раскладка
 * по срокам — их проверяет `tests/delo-board.test.mjs`) и отрисовку.
 *
 * Правила:
 *   • срок — это `due` дела (ISO-дата, локальная полночь); просроченные дела
 *     не прячутся в прошлое, а собраны в отдельной панели «Просрочено»;
 *   • листание ‹ › по неделям/месяцам и кнопка «Сегодня» — всегда на месте;
 *   • в месяце ячейка показывает несколько дел и «ещё N» — остальные раскрываются
 *     на месте, без окон;
 *   • дела без срока и дела вне периода честно перечислены под сеткой.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { dueInfo, parseDue } from './delo-model.js';

const WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
  'сентября', 'октября', 'ноября', 'декабря'];
const MONTHS_NOM = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август',
  'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];

/** Сколько дел показываем в ячейке месяца до «ещё N». */
export const CELL_LIMIT = 3;

export const SCALES = [
  { id: 'week', title: 'Неделя' },
  { id: 'month', title: 'Месяц' },
];

/** Полночь той же даты (локальное время). */
export function startOfDay(date) {
  const d = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(d.getTime())) return null;
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** «2026-11-21» по локальным полям (не `toISOString` — тот сдвигает день). */
export function isoDate(date) {
  const d = startOfDay(date);
  if (!d) return '';
  return String(d.getFullYear()) + '-'
    + String(d.getMonth() + 1).padStart(2, '0') + '-'
    + String(d.getDate()).padStart(2, '0');
}

/** Понедельник недели, в которую попадает дата. */
export function weekStart(date) {
  const d = startOfDay(date);
  if (!d) return null;
  const shift = (d.getDay() + 6) % 7;            // пн → 0, вс → 6
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - shift);
}

/** Сдвиг периода: неделя — на 7 дней от начала недели, месяц — на календарный месяц. */
export function shiftAnchor(anchor, scale, delta) {
  const d = startOfDay(anchor) || startOfDay(new Date());
  const step = Number(delta) || 0;
  if (String(scale) === 'month') return new Date(d.getFullYear(), d.getMonth() + step, 1);
  const start = weekStart(d);
  return new Date(start.getFullYear(), start.getMonth(), start.getDate() + 7 * step);
}

/** Заголовок периода: «17–23 ноября 2026» / «ноябрь 2026». */
export function periodTitle(anchor, scale) {
  const d = startOfDay(anchor) || startOfDay(new Date());
  if (String(scale) === 'month') return MONTHS_NOM[d.getMonth()] + ' ' + d.getFullYear();
  const from = weekStart(d);
  const to = new Date(from.getFullYear(), from.getMonth(), from.getDate() + 6);
  const left = String(from.getDate()) + (from.getMonth() === to.getMonth() ? '' : ' ' + MONTHS[from.getMonth()]);
  return left + '–' + to.getDate() + ' ' + MONTHS[to.getMonth()] + ' ' + to.getFullYear();
}

/** Дни сетки: неделя — 7 дней одной строкой, месяц — все недели месяца целиком. */
export function gridDays(anchor, scale) {
  const d = startOfDay(anchor) || startOfDay(new Date());
  if (String(scale) === 'month') {
    const first = new Date(d.getFullYear(), d.getMonth(), 1);
    const last = new Date(d.getFullYear(), d.getMonth() + 1, 0);
    const start = weekStart(first);
    const out = [];
    for (let cur = new Date(start); cur <= last; cur = new Date(cur.getFullYear(), cur.getMonth(), cur.getDate() + 1)) {
      out.push(new Date(cur));
    }
    // добиваем до целой последней недели — сетка не «рвётся»
    while (out.length % 7) {
      const lastDay = out[out.length - 1];
      out.push(new Date(lastDay.getFullYear(), lastDay.getMonth(), lastDay.getDate() + 1));
    }
    return out;
  }
  const start = weekStart(d);
  return Array.from({ length: 7 }, (_v, i) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + i));
}

/** Строки-недели из плоского списка дней. */
export function gridWeeks(days) {
  const list = Array.isArray(days) ? days : [];
  const out = [];
  for (let i = 0; i < list.length; i += 7) out.push(list.slice(i, i + 7));
  return out;
}

/**
 * Раскладка дел по срокам.
 * @param {object[]} items дела (нормализованные)
 * @param {{scale?:string, anchor?:Date|string, nowMs?:number}} opts
 * @returns {{scale:string, title:string, days:object[], weeks:object[][],
 *            overdue:object[], noDue:object[], outside:number, total:number}}
 */
export function buildTimeline(items, opts = {}) {
  const scale = String(opts.scale) === 'month' ? 'month' : 'week';
  const now = Number.isFinite(Number(opts.nowMs)) ? Number(opts.nowMs) : Date.now();
  const anchor = startOfDay(opts.anchor instanceof Date ? opts.anchor : new Date(now)) || startOfDay(new Date());
  const today = startOfDay(new Date(now));
  const days = gridDays(anchor, scale);
  const first = days[0];
  const last = days[days.length - 1];
  const list = Array.isArray(items) ? items.filter(Boolean) : [];

  const cells = days.map((date) => ({
    iso: isoDate(date),
    date,
    dayNum: date.getDate(),
    weekday: WEEKDAYS[(date.getDay() + 6) % 7],
    isToday: isoDate(date) === isoDate(today),
    isPast: date.getTime() < today.getTime(),
    inMonth: date.getMonth() === anchor.getMonth() && date.getFullYear() === anchor.getFullYear(),
    items: [],
  }));
  const byIso = new Map(cells.map((c) => [c.iso, c]));
  const overdue = [];
  const noDue = [];
  let outside = 0;

  for (const delo of list) {
    const due = parseDue(delo.due);
    if (!due) { noDue.push(delo); continue; }
    if (due.getTime() < today.getTime()) { overdue.push(delo); continue; }
    const cell = byIso.get(isoDate(due));
    if (cell) { cell.items.push(delo); continue; }
    outside += 1;
  }
  for (const cell of cells) {
    cell.items.sort((a, b) => {
      const na = Array.isArray(a.needs_you) && a.needs_you.length ? 0 : 1;
      const nb = Array.isArray(b.needs_you) && b.needs_you.length ? 0 : 1;
      if (na !== nb) return na - nb;
      return String(a.title || '').localeCompare(String(b.title || ''), 'ru');
    });
  }
  overdue.sort((a, b) => String(a.due).localeCompare(String(b.due)));

  return {
    scale,
    title: periodTitle(anchor, scale),
    anchor,
    days,
    weeks: gridWeeks(cells),
    cells,
    overdue,
    noDue,
    outside,
    total: list.length,
  };
}

/* ─────────────────────────────────────────────────────────────── отрисовка */

function cardChip(delo, onOpen, extraClass) {
  const due = dueInfo(delo.due, Date.now());
  const needs = Array.isArray(delo.needs_you) ? delo.needs_you.length : 0;
  return h('button', {
    class: ['delo-tl__card', needs ? 'delo-tl__card--needs' : '', extraClass || ''],
    type: 'button',
    'data-delo': delo.id,
    title: String(delo.title || '') + ' · ' + due.text,
    onclick: () => onOpen(delo),
  },
  h('span', { class: 'delo-tl__card-title' }, String(delo.title || 'Без названия')),
  needs ? h('span', { class: 'chip chip--warn delo-tl__card-needs' }, icon('bell', { size: 10 }), String(needs)) : null);
}

function cellNode(cell, onOpen) {
  const limit = cell.items.length > CELL_LIMIT ? CELL_LIMIT : cell.items.length;
  const body = h('div', { class: 'delo-tl__cell-body', 'data-day': cell.iso });
  cell.items.slice(0, limit).forEach((delo) => body.appendChild(cardChip(delo, onOpen)));
  if (cell.items.length > limit) {
    const rest = cell.items.slice(limit);
    const more = h('button', {
      class: 'delo-tl__more tiny', type: 'button',
      title: 'Показать все дела этого дня',
      onclick: () => {
        rest.forEach((delo) => body.insertBefore(cardChip(delo, onOpen), more));
        more.remove();
      },
    }, 'ещё ' + rest.length);
    body.appendChild(more);
  }
  return h('div', {
    class: ['delo-tl__cell', cell.isToday ? 'delo-tl__cell--today' : '',
      cell.isPast ? 'delo-tl__cell--past' : '', cell.inMonth === false ? 'delo-tl__cell--outside' : ''],
    'data-day-cell': cell.iso,
    role: 'group',
    'aria-label': cell.iso + (cell.isToday ? ', сегодня' : ''),
  },
  h('div', { class: 'delo-tl__cell-head tiny' },
    h('span', { class: 'delo-tl__cell-day tnum' }, String(cell.dayNum)),
    cell.isToday ? h('span', { class: 'delo-tl__today' }, 'сегодня') : null),
  body);
}

/**
 * Таймлайн дел.
 * @param {object[]} items
 * @param {{scale?:string, anchor?:Date, nowMs?:number, onOpen:Function,
 *          onScale?:Function, onShift?:Function, onToday?:Function}} opts
 */
export function renderTimeline(items, opts) {
  const data = buildTimeline(items, opts);
  const onOpen = opts.onOpen;
  const head = h('div', { class: 'delo-tl__toolbar' },
    h('div', { class: 'delo-tl__nav' },
      h('button', {
        class: 'btn btn--sm btn--ghost delo-tl__step', type: 'button', 'data-act': 'tl-prev',
        title: 'Предыдущий период', 'aria-label': 'Предыдущий период',
        onclick: () => opts.onShift(-1),
      }, icon('chevron-left', { size: 12 })),
      h('span', { class: 'delo-tl__period' }, data.title),
      h('button', {
        class: 'btn btn--sm btn--ghost delo-tl__step', type: 'button', 'data-act': 'tl-next',
        title: 'Следующий период', 'aria-label': 'Следующий период',
        onclick: () => opts.onShift(1),
      }, icon('chevron-right', { size: 12 })),
      h('button', {
        class: 'btn btn--sm btn--secondary delo-tl__today-btn', type: 'button', 'data-act': 'tl-today',
        title: 'Вернуться к сегодняшнему дню',
        onclick: () => opts.onToday(),
      }, 'Сегодня')),
    h('div', { class: 'delo-tl__scales', role: 'group', 'aria-label': 'Масштаб таймлайна' },
      ...SCALES.map((s) => h('button', {
        class: ['chip', 'delo-tl__scale', data.scale === s.id ? 'chip--accent' : 'chip--outline'],
        type: 'button',
        'aria-pressed': data.scale === s.id ? 'true' : 'false',
        'data-scale': s.id,
        onclick: () => opts.onScale(s.id),
      }, s.title))));

  const grid = h('div', { class: 'delo-tl__grid delo-tl__grid--' + data.scale },
    h('div', { class: 'delo-tl__weekdays' },
      ...WEEKDAYS.map((w) => h('div', { class: 'delo-tl__weekday tiny muted' }, w))),
    ...data.weeks.map((week) => h('div', { class: 'delo-tl__week' },
      ...week.map((cell) => cellNode(cell, onOpen)))));

  const parts = [];
  if (data.overdue.length) {
    parts.push(h('div', { class: 'delo-tl__overdue', 'data-block': 'overdue' },
      h('div', { class: 'delo-tl__overdue-head' },
        icon('warning', { size: 13 }),
        h('b', null, 'Просрочено: ' + data.overdue.length),
        h('span', { class: 'small muted' }, 'срок прошёл, дело не закрыто')),
      h('div', { class: 'delo-tl__overdue-list' },
        ...data.overdue.map((delo) => {
          const due = dueInfo(delo.due, Date.now());
          return h('div', { class: 'delo-tl__overdue-row' },
            cardChip(delo, onOpen, 'delo-tl__card--overdue'),
            h('span', { class: 'chip chip--danger delo-tl__overdue-chip' }, due.text));
        }))));
  }
  if (data.noDue.length) {
    parts.push(h('div', { class: 'delo-tl__nodue', 'data-block': 'no-due' },
      h('span', { class: 'small muted' }, 'Без срока: ' + data.noDue.length + ' — срок можно поставить в карточке дела.'),
      h('div', { class: 'delo-tl__nodue-list' }, ...data.noDue.map((delo) => cardChip(delo, onOpen)))));
  }
  if (data.outside) {
    parts.push(h('p', { class: 'delo-tl__outside small muted' },
      'Вне выбранного периода: ' + data.outside + ' — листайте ‹ › или нажмите «Сегодня».'));
  }
  if (!data.total) {
    parts.unshift(h('div', { class: 'card delo-empty' },
      icon('calendar', { size: 20 }),
      h('div', { class: 'delo-empty__title' }, 'Сроков пока нет'),
      h('p', { class: 'small muted' },
        'Здесь видно все дела по срокам: что горит, что на этой неделе, что позже. '
        + 'Создайте дело на доске или в списке — и поставьте ему срок.'),
      h('button', {
        class: 'btn btn--sm btn--secondary', type: 'button',
        onclick: () => (typeof opts.onMode === 'function' ? opts.onMode('board') : opts.onScale('week')),
      }, 'Открыть доску')));
  }
  return h('div', { class: 'delo-tl' }, head, ...parts, grid);
}

export default { SCALES, CELL_LIMIT, buildTimeline, gridDays, gridWeeks, isoDate, periodTitle, renderTimeline, shiftAnchor, startOfDay, weekStart };
