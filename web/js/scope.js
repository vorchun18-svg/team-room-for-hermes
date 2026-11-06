/**
 * scope.js — «к чему относится экран»: хлебные крошки, бейдж «вся комната»
 * и фильтр `?request=<id>` для вкладок комнаты (волна 8, Д3).
 *
 * Одно имя параметра на весь продукт — `request` в query-части хэша:
 *   #/room/cafe/reports?request=t_a1b2c3d4
 *   #/room/cafe/journal?request=t_a1b2c3d4
 * BACKLOG-FIX («Дальше», «Ждёт твоего решения») использует то же имя.
 *
 * Чистые функции без побочных эффектов проверяются node --test
 * (tests/scope-ux.test.mjs), DOM-часть — через tests/fake-dom.mjs.
 */

import { h } from './util.js';
import { deriveTitle } from './request-title.js';

/** Имя query-параметра фильтра по запросу. */
export const REQUEST_PARAM = 'request';

/** Бейдж у вкладок комнаты: раздел относится ко всей комнате целиком. */
export const WHOLE_ROOM_LABEL = 'вся комната';

/** Подпись фильтра на вкладках «Отчёты» / «Журнал» / «Дальше». */
export const FILTER_LABEL = 'Только этот запрос';

/**
 * Похоже ли на системный id запроса (t_a1b2c3d4) — такое в тексте не показываем.
 * @param {*} text
 * @returns {boolean}
 */
export function looksLikeRequestId(text) {
  return /^(t_[0-9a-f]{4,}|[0-9a-f]{8,24})$/i.test(String(text === null || text === undefined ? '' : text).trim());
}

/**
 * Достать id запроса-фильтра из маршрута, query или params.
 * Принимает route ({query}), голый query-объект или params вьюхи.
 * @param {*} source
 * @returns {string} '' — фильтра нет
 */
export function requestFilterOf(source) {
  const src = source && typeof source === 'object' ? source : {};
  const bags = [src.query, src.params, src];
  for (const bag of bags) {
    if (!bag || typeof bag !== 'object') continue;
    const raw = bag[REQUEST_PARAM];
    if (raw === undefined || raw === null) continue;
    const value = String(raw).trim();
    if (value) return value;
  }
  return '';
}

/**
 * Хэш вкладки комнаты с фильтром или без него.
 * @param {string} roomId
 * @param {string} tab
 * @param {string} [requestId] id запроса; пусто — фильтра нет
 * @returns {string}
 */
export function scopeHash(roomId, tab, requestId) {
  const base = '#/room/' + encodeURIComponent(String(roomId || ''))
    + (tab ? '/' + encodeURIComponent(String(tab)) : '');
  const rid = String(requestId || '').trim();
  return rid ? base + '?' + REQUEST_PARAM + '=' + encodeURIComponent(rid) : base;
}

/**
 * Оставить только записи одного запроса (клиентская фильтрация, без сервера).
 * @param {object[]} items
 * @param {string} requestId
 * @param {string} [field] имя поля с id запроса
 * @returns {object[]}
 */
export function filterByRequest(items, requestId, field = 'request_id') {
  const list = (Array.isArray(items) ? items : []).filter((item) => item && typeof item === 'object');
  const rid = String(requestId || '').trim();
  if (!rid) return list.slice();
  return list.filter((item) => {
    const value = item[field] !== undefined ? item[field] : item.request;
    return String(value === null || value === undefined ? '' : value) === rid;
  });
}

/**
 * Хлебные крошки шапки: `Комната «Лето» › Запрос «…»`.
 * Ссылка ведёт на уровень выше, последняя крошка — текущая страница.
 * @param {{route?:object, roomTitle?:string, requestTitle?:string, tabTitle?:Function}} opts
 * @returns {{text:string, href?:string, current?:boolean, badge?:string}[]}
 */
export function crumbItems(opts = {}) {
  const route = opts.route && typeof opts.route === 'object' ? opts.route : {};
  const roomId = String(route.roomId || '');
  const roomTitle = String(opts.roomTitle || '').trim();
  if (!roomId || !roomTitle) return [];

  const root = { text: 'Комната «' + roomTitle + '»', href: scopeHash(roomId, 'composer') };
  const tabTitleOf = typeof opts.tabTitle === 'function' ? opts.tabTitle : null;

  if (route.name === 'request') {
    const title = String(opts.requestTitle || '').trim();
    const item = { text: title ? 'Запрос «' + title + '»' : 'Запрос', current: true };
    // Полный текст запроса — в подсказке: крошка показывает короткое название.
    const hint = String(opts.requestHint || '').trim();
    if (hint) item.hint = hint;
    return [root, item];
  }

  const tab = String(route.tab || '');
  if (!tab) return [root];
  const title = (tabTitleOf && tabTitleOf(tab)) || tab;
  return [root, { text: title, current: true, badge: WHOLE_ROOM_LABEL }];
}

/**
 * Крошки под-запроса направления (проектный режим, волна 9):
 * `Комната «…» › Бриф «…» › Часть работы «…»`.
 *
 * Под-запрос приходит с `RequestDetail.plan_link = {stream_id, title,
 * brief_root_id?, brief_title?}`. Id на экран не выводим; название брифа
 * необязательно — тогда просто «Бриф».
 *
 * @param {{roomId?:string, roomTitle?:string, briefRootId?:string,
 *          briefTitle?:string, streamTitle?:string}} opts
 * @returns {{text:string, href?:string, current?:boolean}[]} [] — нечего рисовать
 */
export function briefCrumbItems(opts = {}) {
  const o = opts || {};
  const roomId = String(o.roomId || '');
  const briefRoot = String(o.briefRootId || '');
  const streamTitle = String(o.streamTitle || '').trim();
  if (!roomId || !streamTitle) return [];

  const roomTitle = String(o.roomTitle || '').trim();
  const briefTitle = String(o.briefTitle || '').trim();
  // Названия брифа нет — пишем просто «Бриф»; id брифа нет (карта ещё не
  // пришла) — крошка без ссылки на бриф, но с направлением: путь понятен.
  const briefItem = { text: briefTitle ? 'Бриф «' + briefTitle + '»' : 'Бриф' };
  if (briefRoot) briefItem.href = '#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(briefRoot);
  return [
    { text: roomTitle ? 'Комната «' + roomTitle + '»' : 'Комната', href: scopeHash(roomId, 'composer') },
    briefItem,
    { text: 'Часть работы «' + streamTitle + '»', current: true },
  ];
}

/**
 * Название запроса для подписи вместо id. Запросы кешируются на сессию:
 * список запросов и отчёты ссылаются на один и тот же корень много раз.
 * @param {{get: Function}} api
 * @param {string} requestId
 * @returns {Promise<string>} '' — если сервер не ответил
 */
const titleCache = new Map();

/**
 * Название запроса для подписей: короткое (как на экране) и полное (для подсказки).
 * Кеш один на сессию: список запросов и отчёты ссылаются на один корень много раз.
 * @param {{get: Function}} api
 * @param {string} requestId
 * @returns {Promise<{short: string, full: string}>}
 */
export async function requestTitleInfo(api, requestId) {
  const rid = String(requestId || '').trim();
  if (!rid) return { short: '', full: '' };
  if (titleCache.has(rid)) return titleCache.get(rid);
  let info = { short: '', full: '' };
  try {
    const detail = await api.get('/api/requests/' + encodeURIComponent(rid));
    const full = String((detail && detail.title) || '').trim();
    // W17-G §1: в крошке — короткое название, полный текст — в подсказке.
    const short = String((detail && detail.title_short) || '').trim()
      || (full ? deriveTitle(full) : '');
    info = { short, full };
  } catch {
    info = { short: '', full: '' };
  }
  titleCache.set(rid, info);
  return info;
}

/**
 * Название запроса для подписи вместо id (короткое).
 * @param {{get: Function}} api
 * @param {string} requestId
 * @returns {Promise<string>} '' — если сервер не ответил
 */
export async function requestTitle(api, requestId) {
  const info = await requestTitleInfo(api, requestId);
  return info.short || '';
}

/** Сбросить кеш названий (тесты и смена комнаты). */
export function resetRequestTitles() {
  titleCache.clear();
}

/**
 * Подпись запроса-источника: название, а не `t_…`. id уходит в title.
 * @param {string} title
 * @param {string} [requestId]
 * @returns {string}
 */
export function sourceTitle(title, requestId) {
  const text = String(title === null || title === undefined ? '' : title).trim();
  if (text && !looksLikeRequestId(text)) return text;
  const rid = String(requestId || '').trim();
  return rid ? 'запрос-источник' : '';
}

/**
 * Подсказка к ссылке на запрос: название видно, id — только здесь.
 * @param {string} title
 * @param {string} [requestId]
 * @returns {string}
 */
export function sourceHint(title, requestId) {
  const text = sourceTitle(title, requestId);
  const rid = String(requestId || '').trim();
  const head = text && text !== 'запрос-источник' ? 'Запрос-источник: ' + text : 'Открыть запрос-источник';
  return rid ? head + ' (' + rid + ')' : head;
}

/**
 * Чип фильтра «Только этот запрос ✕». Клик в любом месте снимает фильтр.
 * @param {{requestId?:string, title?:string, onClear?:Function}} opts
 * @returns {HTMLElement}
 */
export function requestFilterChip(opts = {}) {
  const rid = String(opts.requestId || '').trim();
  const title = String(opts.title || '').trim();
  const hint = title && !looksLikeRequestId(title)
    ? FILTER_LABEL + ': «' + title + '» — нажмите, чтобы показать всю комнату'
    : FILTER_LABEL + ' — нажмите, чтобы показать всю комнату';
  const chip = h('button', {
    class: 'chip chip--outline scope-chip',
    type: 'button',
    title: hint + (rid ? ' (' + rid + ')' : ''),
    'aria-label': hint,
    onclick: (ev) => {
      if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
      if (typeof opts.onClear === 'function') opts.onClear();
    },
  },
  h('span', { class: 'scope-chip__text' }, FILTER_LABEL),
  h('span', { class: 'scope-chip__x', 'aria-hidden': 'true' }, '✕'));
  return chip;
}

export default {
  REQUEST_PARAM,
  WHOLE_ROOM_LABEL,
  FILTER_LABEL,
  looksLikeRequestId,
  requestFilterOf,
  scopeHash,
  filterByRequest,
  crumbItems,
  briefCrumbItems,
  requestTitle,
  requestTitleInfo,
  resetRequestTitles,
  sourceTitle,
  sourceHint,
  requestFilterChip,
};
