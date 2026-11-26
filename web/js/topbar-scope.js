/**
 * topbar-scope.js — шапка страницы: этап словами, переключатель охвата и
 * сгруппированные значки (P8, W20-U1 пункт 12).
 *
 * Почему отдельным модулем: шапку рисует app.js (большой файл с DOM), а слова и
 * правила охвата должны проверяться без браузера — tests/topbar-scope.test.mjs.
 *
 * Правила охвата — один смысл в одном месте:
 *   • на странице запроса экран показывает ОДИН запрос → активно «Этот запрос»,
 *     «Вся комната» ведёт к комнате (вкладка «Запрос»);
 *   • на вкладках «От вас нужно / Отчёты / Журнал» фильтр `?request=<id>` — это
 *     «Этот запрос», без фильтра — «Вся комната»;
 *   • где охват не выбирается (Команда, Карта проекта, настройки), переключателя нет:
 *     лишний орган управления только путает.
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { SCOPE_LABELS, tabTitleOf } from './text-ru.js';
import { scopeHash } from './scope.js';

/** Вкладки комнаты, у которых есть фильтр «только этот запрос». */
export const FILTER_TABS = ['backlog', 'reports', 'journal'];

/**
 * Подпись этапа в шапке: «Этап: Идея и стратегия».
 * Голое название этапа рядом с крошками читалось как часть названия комнаты.
 * @param {*} phase
 * @returns {string} '' — этапов у комнаты нет
 */
export function phaseLabel(phase) {
  const text = String(phase === null || phase === undefined ? '' : phase).trim();
  return text ? 'Этап: ' + text : '';
}

/**
 * Что показывает экран: комната целиком или один запрос.
 * @param {{roomId?: string, tab?: string, requestId?: string, name?: string}} opts
 *        name — имя маршрута ('request' — страница запроса)
 * @returns {{active: 'whole'|'request', whole: {text:string, href:string, active:boolean},
 *            request: (null|{text:string, href:string, active:boolean})}|null} null — переключать нечего
 */
export function scopeModel(opts = {}) {
  const o = opts || {};
  const roomId = String(o.roomId || '').trim();
  const tab = String(o.tab || '').trim();
  const requestId = String(o.requestId || '').trim();
  const onRequest = String(o.name || '') === 'request' || String(o.kind || '') === 'request';
  if (!roomId) return null;

  const whole = {
    text: SCOPE_LABELS.whole,
    href: scopeHash(roomId, onRequest ? 'composer' : tab),
    active: false,
  };
  const request = {
    text: SCOPE_LABELS.request,
    href: '',
    active: false,
  };

  if (onRequest && requestId) {
    whole.href = scopeHash(roomId, 'composer');
    request.href = '#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(requestId);
    request.active = true;
    return { active: 'request', whole, request };
  }

  if (!FILTER_TABS.includes(tab)) return null;

  if (requestId) {
    whole.href = scopeHash(roomId, tab);
    request.href = scopeHash(roomId, tab, requestId);
    request.active = true;
    return { active: 'request', whole, request };
  }

  // Фильтра нет: показать «Этот запрос» некуда — это и есть вся комната.
  whole.active = true;
  return { active: 'whole', whole, request: null };
}

/** Подсказки сегментов: что произойдёт по клику. */
const SCOPE_HINTS = {
  whole: 'Показать комнату целиком: все задачи и дела',
  request: 'Показать только эту задачу',
};

/**
 * Переключатель «Вся комната / Этот запрос» (заменяет прежнюю кучу кнопок
 * «вся команда»: состояние охвата видно одним взглядом).
 * @param {ReturnType<typeof scopeModel>} model
 * @returns {HTMLElement|null}
 */
export function scopeSwitcher(model) {
  if (!model) return null;
  const nav = h('nav', {
    class: 'topbar__scope scope-switch',
    'aria-label': 'Что показывает экран',
    'data-scope': model.active,
  });
  const seg = (key, item) => h('a', {
    class: 'scope-switch__seg' + (item.active ? ' scope-switch__seg--active' : ''),
    href: item.href || null,
    'aria-current': item.active ? 'page' : null,
    title: SCOPE_HINTS[key] || item.text,
    'data-scope-seg': key,
  }, item.text);
  nav.appendChild(seg('whole', model.whole));
  if (model.request) nav.appendChild(seg('request', model.request));
  return nav;
}

/**
 * Значки-инструменты шапки: Поиск, Палитра, Настройки. Одна группа, без дублей:
 * поиск — это поиск (а не палитра), у каждого значка своя подсказка и своё
 * сочетание клавиш ровно там, где оно работает.
 * @param {{navigate: Function, openPalette?: Function, onSettings?: boolean}} opts
 * @returns {HTMLElement}
 */
export function toolsGroup(opts = {}) {
  const o = opts || {};
  const navigate = typeof o.navigate === 'function' ? o.navigate : () => {};
  const openPalette = typeof o.openPalette === 'function' ? o.openPalette : null;
  const group = h('div', { class: 'topbar__group topbar__group--tools', role: 'group', 'aria-label': 'Инструменты' });

  group.appendChild(h('button', {
    class: 'btn btn--ghost btn--icon btn--sm topbar__tool',
    type: 'button',
    'data-tool': 'search',
    title: 'Поиск по комнатам и задачам',
    'aria-label': 'Поиск',
    onclick: () => navigate('#/search'),
  }, icon('search', { size: 15 })));

  if (openPalette) {
    group.appendChild(h('button', {
      class: 'btn btn--ghost btn--icon btn--sm topbar__tool',
      type: 'button',
      'data-tool': 'palette',
      title: 'Палитра команд · Ctrl K',
      'aria-label': 'Палитра команд',
      onclick: () => openPalette(),
    }, icon('list-unordered', { size: 15 })));
  }

  if (o.onSettings !== false) {
    group.appendChild(h('a', {
      class: 'btn btn--ghost btn--icon btn--sm topbar__tool',
      href: '#/settings',
      'data-tool': 'settings',
      title: 'Настройки приложения',
      'aria-label': 'Настройки приложения',
    }, icon('settings-gear', { size: 15 })));
  }
  return group;
}

/** Название вкладки по id — через общий словарь (пусто для неизвестной). */
export function tabName(id) {
  return tabTitleOf(id);
}

export default { FILTER_TABS, phaseLabel, scopeModel, scopeSwitcher, toolsGroup, tabName };
