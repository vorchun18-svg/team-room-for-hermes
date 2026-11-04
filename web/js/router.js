/**
 * router.js — хэш-маршрутизация и монтирование вьюх.
 *
 * Маршруты:
 *   #/                          — обзор комнат (views/rooms.js)
 *   #/room/<id>                 — комната, вкладка по умолчанию «Запрос»
 *   #/room/<id>/<tab>           — вкладка: composer|team|roadmap|backlog|reports|journal
 *   #/room/<id>/request/<root>  — страница запроса
 *   #/search?q=<текст>          — глобальный поиск
 *   #/settings                  — настройки
 *
 * Контракт вьюхи:  export async function mount(el, ctx) → { unmount?() }
 * ctx = { route, params, api, store, navigate, toast, room, refresh }
 */

import { h, esc } from './util.js';
import { icon } from './icons.js';

export const TABS = [
  { id: 'composer', title: 'Запрос', icon: 'comment-discussion' },
  { id: 'team', title: 'Команда', icon: 'organization' },
  { id: 'roadmap', title: 'Карта проекта', icon: 'map' },
  { id: 'backlog', title: 'Дальше', icon: 'arrow-circle-right' },
  { id: 'reports', title: 'Отчёты', icon: 'report' },
  { id: 'journal', title: 'Журнал', icon: 'history' },
];

export const TAB_IDS = TABS.map((t) => t.id);

/** Заголовок вкладки по id ('' для неизвестной). */
export function tabTitle(id) {
  const t = TABS.find((x) => x.id === id);
  return t ? t.title : '';
}

/**
 * Разобрать хэш в маршрут.
 * @param {string} [hash]
 * @returns {{name:string, roomId:string|null, tab:string|null, id:string|null, params:object, hash:string}}
 */
export function parseHash(hash) {
  const raw = String(hash === undefined || hash === null ? (typeof location !== 'undefined' ? location.hash : '') : hash);
  const clean = raw.replace(/^#/, '');
  const [pathPart, queryPart] = clean.split('?');
  const segments = pathPart.split('/').filter(Boolean).map((s) => {
    try { return decodeURIComponent(s); } catch { return s; }
  });
  const query = {};
  if (queryPart) {
    for (const pair of queryPart.split('&')) {
      if (!pair) continue;
      const eq = pair.indexOf('=');
      const k = eq < 0 ? pair : pair.slice(0, eq);
      const v = eq < 0 ? '' : pair.slice(eq + 1);
      try { query[decodeURIComponent(k.replace(/\+/g, ' '))] = decodeURIComponent(v.replace(/\+/g, ' ')); } catch { /* ignore */ }
    }
  }

  const base = { name: 'home', roomId: null, tab: null, id: null, params: {}, query, hash: '#' + pathPart };

  if (segments.length === 0) return base;

  if (segments[0] === 'room' && segments[1]) {
    const tab = segments[2] || 'composer';
    const route = {
      name: 'room',
      roomId: segments[1],
      tab,
      id: segments[3] || null,
      params: { roomId: segments[1], tab, id: segments[3] || null },
      query,
      hash: '#' + pathPart,
    };
    if (tab === 'request' && segments[3]) { route.name = 'request'; route.view = 'request'; }
    else route.view = TAB_IDS.includes(tab) ? tab : 'composer';
    return route;
  }

  if (segments[0] === 'search') {
    return Object.assign(base, { name: 'search', view: 'search', params: { q: query.q || '' }, query });
  }

  if (segments[0] === 'settings') {
    // #/settings/diagnostics — «Проверка системы» (своя вьюха, тот же раздел
    // настроек: крошки и подсветка меню остаются «Настройками»).
    if (segments[1] === 'diagnostics') {
      return Object.assign(base, { name: 'settings', view: 'diagnostics',
        params: { section: 'diagnostics' }, query, hash: '#/settings/diagnostics' });
    }
    return Object.assign(base, { name: 'settings', view: 'settings' });
  }

  return Object.assign(base, { name: 'notfound', view: null, hash: '#' + pathPart });
}

/** Собрать хэш из частей. */
export function buildHash({ roomId, tab, id } = {}) {
  if (!roomId) return '#/';
  const parts = ['#/room', encodeURIComponent(roomId)];
  if (tab) parts.push(encodeURIComponent(tab));
  if (id) parts.push(encodeURIComponent(id));
  return parts.join('/');
}

/** Карточка «раздел не открылся» — человеческий текст и кнопка «Повторить» (D12). */
const OPEN_FAILED_TEXT = 'Обновите страницу (Ctrl+R). Если повторится — сообщите администратору.';

function failCard(title, detail, retry) {
  const actions = h('div', { class: 'empty__actions' },
    h('button', {
      class: 'btn btn--secondary', type: 'button',
      onclick: () => { if (typeof retry === 'function') retry(); else location.reload(); },
    }, icon('refresh', { size: 13 }), 'Повторить'));
  return h(
    'div',
    { class: 'card' },
    h('div', { class: 'empty' },
      icon('debug-disconnect', { size: 22 }),
      h('div', { class: 'empty__title' }, title),
      h('p', { class: 'empty__text' }, OPEN_FAILED_TEXT),
      detail ? h('p', { class: 'empty__text small muted', title: detail }, detail) : null,
      actions,
    ),
  );
}

/** Заглушка вместо технического «ожидается web/js/views/team.js». */
function stubView(name, retry) {
  const title = tabTitle(name) || (name === 'rooms' ? 'Комнаты' : name === 'search' ? 'Поиск' : name === 'settings' ? 'Настройки' : 'Раздел');
  const box = failCard('Не удалось открыть раздел «' + title + '»', 'Файл раздела не загрузился: web/js/views/' + (name || '…') + '.js', retry);
  return { mount(el) { el.appendChild(box); return { unmount() { box.remove(); } }; } };
}

function errorView(err, retry) {
  const box = failCard('Не удалось открыть раздел', String(err && err.message ? err.message : err), retry);
  return { mount(el) { el.appendChild(box); return { unmount() { box.remove(); } }; } };
}

function notFoundView(route) {
  const box = h(
    'div',
    { class: 'card' },
    h('div', { class: 'empty' },
      icon('map', { size: 22 }),
      h('div', { class: 'empty__title' }, 'Такой страницы нет'),
      h('p', { class: 'empty__text mono' }, route.hash || '#/'),
    ),
  );
  return { mount(el) { el.appendChild(box); return { unmount() { box.remove(); } }; } };
}

/**
 * Загрузка вьюхи. Раньше наличие файла проверялось HEAD-запросом, но мок-сервер
 * (и любой сервер без do_HEAD) отвечал 501, а DESIGN-NOTES обещал 404 (F-8).
 * Теперь пробуем import() и ловим отказ: отсутствующий файл — это заглушка,
 * а не исключение наружу.
 */
const viewCache = new Map();

/** Похоже ли, что модуля просто нет (а не он сломан изнутри)? */
function isMissingModule(err) {
  const msg = String((err && err.message) || err || '');
  return /dynamically imported module|module script failed|Failed to fetch|404|not found/i.test(msg);
}

/**
 * @param {string} name имя вьюхи (без .js)
 * @returns {Promise<{ok:boolean, mod?:object, err?:Error}>}
 */
async function loadView(name) {
  if (viewCache.has(name)) return viewCache.get(name);
  let entry;
  try {
    const mod = await import('./views/' + name + '.js');
    if (!mod || typeof mod.mount !== 'function') {
      throw new Error('views/' + name + '.js не экспортирует mount(el, ctx)');
    }
    entry = { ok: true, mod };
  } catch (err) {
    entry = { ok: false, err };
  }
  viewCache.set(name, entry);
  return entry;
}

/**
 * @param {{
 *   el: HTMLElement,
 *   api: object,
 *   store: object,
 *   toast: (msg:string, kind?:string)=>void,
 *   getRoom: (id:string)=>object|null,
 *   onChange?: (route:object, viewName:string)=>void,
 * }} deps
 */
export function createRouter(deps) {
  const { el, api, store, toast, getRoom, onChange } = deps;
  let current = null;
  let token = 0;
  let route = parseHash();

  function navigate(hash) {
    const next = String(hash || '#/');
    if (location.hash === next) render();
    else location.hash = next;
  }

  function retryView(name) {
    viewCache.delete(name);
    render();
  }

  async function mountFor(r) {
    if (r.name === 'notfound') return { name: 'notfound', mod: notFoundView(r) };
    const name = r.view || 'rooms';
    const entry = await loadView(name);
    if (!entry.ok) {
      if (isMissingModule(entry.err)) {
        console.warn('[router] модуль раздела не загрузился:', name, entry.err);
        return { name, mod: stubView(name, () => retryView(name)), missing: true };
      }
      console.warn('[router] view failed:', name, entry.err);
      return { name, mod: errorView(entry.err, () => retryView(name)), failed: true };
    }
    return { name, mod: entry.mod };
  }

  async function render() {
    const my = ++token;
    route = parseHash();
    if (current && typeof current.unmount === 'function') {
      try { current.unmount(); } catch (err) { console.warn('[router] unmount failed', err); }
    }
    current = null;
    el.replaceChildren();

    if (onChange) onChange(route, route.view || 'rooms');

    const { name, mod, failed } = await mountFor(route);
    if (my !== token || !el.isConnected) return;

    const ctx = {
      route,
      params: Object.assign({}, route.params, route.query || {}),
      api,
      store,
      navigate,
      toast,
      room: route.roomId && getRoom ? getRoom(route.roomId) : null,
      refresh: () => { if (!failed) render(); },
    };

    try {
      const inst = await mod.mount(el, ctx);
      current = inst && typeof inst === 'object' ? inst : null;
    } catch (err) {
      console.error('[router] mount threw:', name, err);
      const fallback = errorView(err);
      el.replaceChildren();
      fallback.mount(el, ctx);
    }
    el.scrollTop = 0;
    if (typeof document !== 'undefined') {
      document.documentElement.dataset.view = name;
    }
  }

  function start() {
    window.addEventListener('hashchange', render);
    render();
  }

  return { start, render, reload: render, navigate, get route() { return route; }, get current() { return current; } };
}

export default createRouter;
