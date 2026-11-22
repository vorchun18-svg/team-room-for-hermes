/**
 * request-tabs.js — открытые запросы вкладками над страницей (W29-Q7).
 *
 * Зачем: человек ходит между двумя-тремя запросами одной комнаты (сверить
 * свод, вернуться к вопросу), а каждый переход — заново «назад» по истории.
 * Вкладки держат открытые запросы на виду, переключаются клавишами и
 * переживают перезапуск приложения: список хранится в localStorage.
 *
 * Что умеет:
 *   * открыть запрос — вкладка появляется над страницей (активная подсвечена);
 *   * × — закрыть; закрепление (📌) держит вкладку впереди и защищает от Ctrl+W;
 *   * Alt+←/→ — соседняя вкладка; Ctrl+W — закрыть текущую;
 *   * закрыть все/лишние; состояние — локально, на сервер ничего не пишем.
 *
 * Данные вкладки: {roomId, id, title, pinned}. Название приходит из крошек
 * (app.js) и обновляется, когда оно стало известно.
 *
 * Проверки: tests/request-tabs.test.mjs (модель) и живая проверка в Chromium.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';

export const TABS_KEY = 'teamroom.requestTabs';
/** Больше восьми вкладок не помещаются над страницей — старые закрываются. */
export const MAX_TABS = 8;

/* ──────────────────────────────────────────────────────────────── модель */

function asObj(x) {
  return x && typeof x === 'object' ? x : null;
}

/**
 * Разобрать сохранённый список вкладок, терпимо к мусору.
 * @param {string|object[]|null} raw
 * @param {number} [max]
 * @returns {{roomId:string,id:string,title:string,pinned:boolean}[]}
 */
export function parseTabs(raw, max = MAX_TABS) {
  let list = raw;
  if (typeof raw === 'string') {
    try { list = JSON.parse(raw); } catch { return []; }
  }
  if (asObj(list) && Array.isArray(list.tabs)) list = list.tabs;
  if (!Array.isArray(list)) return [];
  const out = [];
  const seen = new Set();
  for (const item of list) {
    const rec = asObj(item);
    if (!rec) continue;
    const id = String(rec.id || '').trim();
    const roomId = String(rec.roomId || rec.room_id || '').trim();
    if (!id || !roomId || seen.has(id)) continue;
    seen.add(id);
    out.push({
      id,
      roomId,
      title: String(rec.title || '').trim(),
      pinned: rec.pinned === true,
    });
    if (out.length >= max) break;
  }
  return out;
}

/** Сериализовать список для localStorage (только нужные поля). */
export function serializeTabs(list) {
  return JSON.stringify({
    v: 1,
    tabs: (Array.isArray(list) ? list : []).map((t) => ({
      id: String(t.id), roomId: String(t.roomId), title: String(t.title || ''), pinned: t.pinned === true,
    })),
  });
}

/** Закреплённые — впереди (порядок остальных не меняется). */
export function sortTabs(list) {
  const arr = Array.isArray(list) ? list.slice() : [];
  const pinned = arr.filter((t) => t.pinned);
  const rest = arr.filter((t) => !t.pinned);
  return pinned.concat(rest);
}

/**
 * Добавить/обновить вкладку запроса.
 * @param {object[]} list
 * @param {{roomId:string,id:string,title?:string}} entry
 * @param {number} [max]
 * @returns {object[]} новый список
 */
export function addTab(list, entry, max = MAX_TABS) {
  const src = asObj(entry) || {};
  const id = String(src.id || '').trim();
  const roomId = String(src.roomId || '').trim();
  if (!id || !roomId) return sortTabs(list);
  const arr = (Array.isArray(list) ? list : []).filter((t) => t && t.id !== id);
  const prev = (Array.isArray(list) ? list : []).find((t) => t && t.id === id) || null;
  const tab = {
    id,
    roomId,
    title: String(src.title || (prev && prev.title) || '').trim(),
    pinned: src.pinned === undefined ? Boolean(prev && prev.pinned) : src.pinned === true,
  };
  // Новые — в хвост (там же, где открывают новую вкладку в браузере), но не
  // больше потолка: самая старая незакреплённая уходит.
  let next = arr.concat([tab]);
  while (next.length > Math.max(1, max)) {
    const victim = next.findIndex((t) => !t.pinned);
    if (victim < 0) break;
    next = next.slice(0, victim).concat(next.slice(victim + 1));
  }
  return sortTabs(next);
}

/** Убрать вкладку по id. */
export function removeTab(list, id) {
  const key = String(id || '');
  return (Array.isArray(list) ? list : []).filter((t) => t && t.id !== key);
}

/** Переключить закрепление вкладки. */
export function toggleTabPin(list, id) {
  const key = String(id || '');
  return sortTabs((Array.isArray(list) ? list : []).map((t) => (
    t && t.id === key ? Object.assign({}, t, { pinned: !t.pinned }) : t
  )));
}

/**
 * Соседняя вкладка по направлению (−1 влево, +1 вправо), с переходом по кругу.
 * @returns {string} id соседней вкладки или '' если её нет
 */
export function neighborTab(list, activeId, dir) {
  const arr = Array.isArray(list) ? list.filter((t) => t && t.id) : [];
  if (arr.length < 2) return '';
  const key = String(activeId || '');
  const at = arr.findIndex((t) => t.id === key);
  if (at < 0) return String(arr[0].id);
  const step = Number(dir) < 0 ? -1 : 1;
  return String(arr[(at + step + arr.length) % arr.length].id);
}

/**
 * Что открыть после закрытия вкладки: сосед справа, иначе слева, иначе адрес
 * комнаты (страница запросов). Пустой id — вкладок не осталось.
 * @returns {{id:string, roomId:string}}
 */
export function afterClose(list, closedId, activeId) {
  const arr = (Array.isArray(list) ? list : []).filter((t) => t && t.id);
  const closed = String(closedId || '');
  const at = arr.findIndex((t) => t.id === closed);
  if (at < 0) {
    // Закрывают не ту вкладку, что открыта: остаёмся на месте.
    const keep = arr.find((t) => t.id === String(activeId || '')) || arr[0] || null;
    return keep ? { id: String(keep.id), roomId: String(keep.roomId) } : { id: '', roomId: '' };
  }
  // Сосед справа, иначе слева — тот же порядок, что у вкладок браузера.
  const pick = arr[at + 1] || arr[at - 1] || null;
  return pick ? { id: String(pick.id), roomId: String(pick.roomId) } : { id: '', roomId: '' };
}

/** Адрес вкладки запроса. */
export function tabHash(tab) {
  if (!tab || !tab.id || !tab.roomId) return '#/';
  return '#/room/' + encodeURIComponent(tab.roomId) + '/request/' + encodeURIComponent(tab.id);
}

/** Подпись вкладки: короткое название или «Запрос» (пустых подписей не бывает). */
export function tabLabel(tab) {
  const t = String((tab && tab.title) || '').trim();
  if (!t) return 'Запрос';
  return t.length > 42 ? t.slice(0, 41).trimEnd() + '…' : t;
}

/* ───────────────────────────────────────────────────────────── компонент */

/**
 * Полоса вкладок над страницей.
 *
 * @param {{
 *   host: HTMLElement,
 *   navigate: (hash:string) => void,
 *   storage?: Storage|null,
 *   getRoute?: () => object|null,
 * }} opts
 * @returns {{
 *   sync: (route:object, info?:object) => void,
 *   render: () => void,
 *   close: (id:string) => void,
 *   closeActive: () => boolean,
 *   step: (dir:number) => boolean,
 *   togglePin: (id:string) => void,
 *   list: () => object[],
 *   activeId: () => string,
 *   destroy: () => void,
 * }}
 */
export function createRequestTabs(opts) {
  const { host, navigate } = opts || {};
  const storage = opts && 'storage' in opts ? opts.storage : safeStorage();
  const getRoute = typeof (opts && opts.getRoute) === 'function' ? opts.getRoute : null;
  let list = readTabs(storage);
  let active = '';

  function readTabs(store) {
    if (!store || typeof store.getItem !== 'function') return [];
    try { return parseTabs(store.getItem(TABS_KEY)); } catch { return []; }
  }

  function save() {
    if (!storage || typeof storage.setItem !== 'function') return;
    try { storage.setItem(TABS_KEY, serializeTabs(list)); } catch { /* приватный режим */ }
  }

  function go(tab) {
    const hash = tabHash(tab);
    if (!hash || hash === '#/') return;
    // Подсветку ставим сразу: клавиши и закрытие работают и тогда, когда
    // переход ещё не дошёл до каркаса (медленный hashchange, повторное нажатие).
    active = String(tab.id || '');
    if (typeof navigate === 'function') navigate(hash);
    else if (typeof location !== 'undefined') location.hash = hash;
    render();
  }

  /**
   * Сверить вкладки с маршрутом: открыть запрос вкладкой, обновить название,
   * снять подсветку на других экранах.
   */
  function sync(route, info) {
    const r = route || {};
    const meta = info || {};
    if (r.name === 'request' && r.id && r.roomId) {
      const id = String(r.id);
      list = addTab(list, { roomId: r.roomId, id, title: meta.title || '' });
      active = id;
      save();
    } else {
      active = '';
    }
    render();
  }

  function render() {
    if (!host) return;
    if (!list.length) {
      host.replaceChildren();
      host.hidden = true;
      host.classList.add('is-empty');
      return;
    }
    host.hidden = false;
    host.classList.remove('is-empty');
    const nav = h('div', { class: 'req-tabs__row', role: 'list' });
    for (const tab of list) {
      const on = tab.id === active;
      const title = tabLabel(tab);
      const pin = h('button', {
        type: 'button',
        class: 'req-tabs__pin' + (tab.pinned ? ' is-on' : ''),
        title: tab.pinned ? 'Открепить вкладку' : 'Закрепить вкладку: держится впереди, не закрывается Ctrl+W',
        'aria-pressed': tab.pinned ? 'true' : 'false',
        'aria-label': (tab.pinned ? 'Открепить' : 'Закрепить') + ' вкладку «' + title + '»',
        onclick: (ev) => { if (ev && ev.stopPropagation) ev.stopPropagation(); togglePin(tab.id); },
      }, icon('pin', { size: 12 }));
      const close = h('button', {
        type: 'button',
        class: 'req-tabs__close',
        title: 'Закрыть вкладку (Ctrl+W)',
        'aria-label': 'Закрыть вкладку «' + title + '»',
        onclick: (ev) => { if (ev && ev.stopPropagation) ev.stopPropagation(); close(tab.id); },
      }, icon('close', { size: 12 }));
      const link = h('a', {
        class: 'req-tabs__link' + (on ? ' is-active' : ''),
        href: tabHash(tab),
        title: tab.title || title,
        'aria-current': on ? 'page' : null,
        'data-request-tab': tab.id,
        onclick: (ev) => {
          // Ссылка настоящая (средняя кнопка, копирование адреса работают), но
          // внутри приложения идём через navigate — без перезагрузки страницы.
          if (typeof navigate !== 'function') return;
          if (ev && (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1)) return;
          if (ev && ev.preventDefault) ev.preventDefault();
          go(tab);
        },
      }, h('span', { class: 'req-tabs__title' }, title));
      nav.appendChild(h('div', {
        class: 'req-tabs__item' + (on ? ' is-active' : '') + (tab.pinned ? ' is-pinned' : ''),
        role: 'listitem',
      }, link, pin, close));
    }
    // Кнопка «закрыть все» — только когда вкладок и правда много: одна вкладка
    // не повод показывать ещё одну кнопку.
    if (list.length > 1) {
      nav.appendChild(h('button', {
        type: 'button', class: 'req-tabs__all', title: 'Закрыть все вкладки, кроме закреплённых',
        'aria-label': 'Закрыть все вкладки',
        onclick: () => closeAll(),
      }, 'Закрыть все'));
    }
    host.replaceChildren(nav);
  }

  function closeAll() {
    const pinned = list.filter((t) => t.pinned);
    const hadActivePinned = pinned.some((t) => t.id === active);
    list = pinned;
    save();
    if (!hadActivePinned) {
      active = '';
      const route = getRoute ? getRoute() : null;
      const roomId = (route && route.roomId) || (pinned[0] && pinned[0].roomId) || '';
      if (roomId && typeof navigate === 'function') navigate('#/room/' + encodeURIComponent(roomId) + '/composer');
    }
    render();
  }

  function close(id) {
    const key = String(id || '');
    const wasActive = key === active;
    const tab = list.find((t) => t.id === key) || null;
    const next = afterClose(list, key, wasActive ? key : active);
    list = removeTab(list, key);
    save();
    if (wasActive) {
      active = '';
      if (next.id) {
        go(next);
      } else if (tab && tab.roomId && typeof navigate === 'function') {
        navigate('#/room/' + encodeURIComponent(tab.roomId) + '/composer');
      }
    }
    render();
  }

  /** Ctrl+W: закрыть текущую (закреплённую так просто не закрываем — правило вкладок). */
  function closeActive() {
    if (!active) return false;
    const tab = list.find((t) => t.id === active);
    if (!tab) return false;
    if (tab.pinned) return false;
    close(active);
    return true;
  }

  /** Alt+←/→: соседняя вкладка. */
  function step(dir) {
    const next = neighborTab(list, active, dir);
    if (!next) return false;
    const tab = list.find((t) => t.id === next);
    if (!tab) return false;
    go(tab);
    return true;
  }

  function togglePin(id) {
    const wasPinned = list.some((t) => t.id === String(id) && t.pinned);
    const keep = list.find((t) => t.id === String(id)) || null;
    list = toggleTabPin(list, id);
    save();
    render();
    if (typeof opts.onNotice === 'function' && keep) {
      opts.onNotice(wasPinned
        ? 'Вкладка «' + tabLabel(keep) + '» откреплена'
        : 'Вкладка «' + tabLabel(keep) + '» закреплена — держится впереди', 'info');
    }
  }

  return {
    sync, render, close, closeActive, step, togglePin,
    list: () => list.slice(),
    activeId: () => active,
    destroy() { if (host) { host.replaceChildren(); host.hidden = true; } },
  };
}

/** localStorage без исключений (приватный режим, тесты). */
function safeStorage() {
  try {
    if (typeof localStorage !== 'undefined' && localStorage) return localStorage;
  } catch { /* приватный режим */ }
  return null;
}

/**
 * Клавиши вкладок: Alt+←/→ — соседняя, Ctrl+W — закрыть текущую.
 *
 * Один обработчик на документ, ставится каркасом приложения. В полях ввода
 * Alt+←/→ браузер тоже листает — на странице запроса вкладки важнее, но
 * Ctrl+W никогда не «съедаем», если вкладок нет (пусть работает браузер).
 *
 * @param {{getTabs: () => object, doc?: Document}} opts
 * @returns {() => void} снять обработчики
 */
export function installTabKeys(opts) {
  const getTabs = typeof (opts && opts.getTabs) === 'function' ? opts.getTabs : () => null;
  const doc = (opts && opts.doc) || (typeof document !== 'undefined' ? document : null);
  if (!doc || typeof doc.addEventListener !== 'function') return () => {};

  function onKey(ev) {
    if (!ev) return;
    const tabs = getTabs();
    if (!tabs || typeof tabs.step !== 'function') return;
    const key = String(ev.key || '');
    if (ev.altKey && !ev.ctrlKey && !ev.metaKey && (key === 'ArrowLeft' || key === 'ArrowRight')) {
      if (tabs.step(key === 'ArrowLeft' ? -1 : 1)) {
        if (ev.preventDefault) ev.preventDefault();
      }
      return;
    }
    const mod = ev.ctrlKey || ev.metaKey;
    if (mod && !ev.altKey && (key === 'w' || key === 'W' || key === 'ы' || key === 'Ы')) {
      if (tabs.closeActive()) {
        if (ev.preventDefault) ev.preventDefault();
        if (ev.stopPropagation) ev.stopPropagation();
      }
    }
  }

  doc.addEventListener('keydown', onKey, true);
  return () => doc.removeEventListener('keydown', onKey, true);
}
