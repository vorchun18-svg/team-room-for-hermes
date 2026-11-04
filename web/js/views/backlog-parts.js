/**
 * backlog-parts.js — мелкие элементы вкладки «Дальше»: ссылка на запрос-источник,
 * чип владельца, чип срока, «в календарь (.ics)», чип фильтра «Только этот запрос».
 *
 * Вынесено из backlog.js (файл держим ≤ ~650 строк); DOM-логики здесь нет —
 * только сборка узлов из готовых данных и обработчики, которые даёт вьюха.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { dueChipLabel, icsItemUrl } from './backlog-model.js';

export const STEP_HINT = 'Отметьте, когда шаг выполнен — он перейдёт в «Выполнено» комнаты';

/** 'mikhail' — человек, остальное — профиль агента. */
export function ownerName(owner) {
  if (!owner) return '';
  return owner === 'mikhail' ? 'Михаил' : String(owner);
}

export function requestHref(roomId, rid) {
  return `#/room/${encodeURIComponent(roomId)}/request/${encodeURIComponent(rid)}`;
}

/** Короткое имя источника: до двоеточия или первые 4 слова (D13). */
export function briefTitle(title, max = 34) {
  const full = String(title || '').trim();
  if (!full) return '';
  const head = full.split(/[:·—]/)[0].trim() || full;
  const words = head.split(/\s+/).filter(Boolean);
  const short = words.length > 4 ? words.slice(0, 4).join(' ') : head;
  return short.length > max ? short.slice(0, max - 1).trimEnd() + '…' : short;
}

/** Ссылка на запрос-источник: «из какого запроса» этот шаг/вопрос (Д3). */
export function sourceLink(roomId, rid, title) {
  if (!rid) return null;
  const full = String(title || '').trim();
  const label = full ? briefTitle(full) : 'Запрос';
  const caption = full ? 'Из запроса: ' + full : 'Открыть запрос-источник';
  return h('a', {
    class: 'backlog__source',
    href: requestHref(roomId, rid),
    title: caption,
    'aria-label': caption,
  }, icon('arrow-right', { size: 11 }), h('span', { class: 'backlog__source-text' }, label));
}

/** Чип владельца шага. */
export function ownerChip(owner) {
  if (!owner) return h('span', { class: 'chip chip--muted' }, 'без владельца');
  return h('span', {
    class: 'chip chip--outline owner-chip',
    title: owner === 'mikhail' ? 'Решение за Михаилом' : 'Исполнитель: ' + owner,
  }, icon('person', { size: 11 }), h('span', { class: 'owner-chip__text truncate' }, ownerName(owner)));
}

/** Ссылка-кнопка «в календарь»: файл .ics для одного шага. */
export function icsLink(roomId, itemId, label) {
  const title = label || 'Добавить в календарь (.ics)';
  return h('a', {
    class: 'btn btn--ghost btn--sm backlog__ics',
    href: icsItemUrl(roomId, itemId),
    download: '',
    title,
    'aria-label': title,
  }, icon('calendar', { size: 13 }), h('span', { class: 'backlog__ics-text' }, 'в календарь (.ics)'));
}

/**
 * Чип срока: показывает «Сегодня»/«28 окт», клик открывает выбор даты.
 * Поле даты лежит под чипом (прозрачное), поэтому e2e и клавиатура работают.
 */
export function dueChip(item, { busy = false, onChange } = {}) {
  const due = item && item.due ? String(item.due) : '';
  const input = h('input', {
    class: 'due-chip__input',
    type: 'date',
    value: due,
    disabled: busy,
    'aria-label': 'Срок шага: ' + ((item && item.text) || ''),
    title: due ? 'Срок: ' + due + ' — нажмите, чтобы изменить' : 'Срок не задан — нажмите, чтобы выбрать',
    onchange: (ev) => {
      if (typeof onChange === 'function') onChange((ev.target && ev.target.value) || null);
    },
  });
  return h('label', {
    class: 'due-chip' + (due ? '' : ' due-chip--empty'),
    title: due ? 'Срок: ' + due + ' — нажмите, чтобы изменить' : 'Срок не задан — нажмите, чтобы выбрать',
  }, icon('calendar', { size: 11 }),
  h('span', { class: 'due-chip__text' }, due ? dueChipLabel(due, new Date()) : 'Срок'), input);
}

/** Чип «Только этот запрос ✕» и кнопка «Все дела комнаты». */
export function filterBar({ roomId, requestId, title, onClear }) {
  const caption = 'Открыть запрос: ' + (title || requestId);
  return h('div', { class: 'backlog__filter row row--wrap row--tight' },
    h('a', {
      class: 'backlog__filter-link truncate',
      href: requestHref(roomId, requestId),
      title: caption,
      'aria-label': caption,
    }, h('span', { class: 'backlog__filter-chip' },
      icon('filter', { size: 11 }),
      h('span', {}, 'Только этот запрос'),
      h('span', { class: 'backlog__filter-sep', 'aria-hidden': 'true' }, '·'),
      h('span', { class: 'backlog__filter-name truncate' }, String(title || requestId)))),
    h('button', {
      class: 'btn btn--ghost btn--sm backlog__filter-clear',
      type: 'button',
      title: 'Показать все дела комнаты',
      'aria-label': 'Показать все дела комнаты',
      onclick: () => { if (typeof onClear === 'function') onClear(); },
    }, icon('close', { size: 12 }), 'Все дела комнаты'));
}

export const SAVE_FAILED = 'Не удалось сохранить отметку. Обновите страницу и попробуйте ещё раз';
