/**
 * palette.js — командная палитра (Ctrl+K).
 *
 * app.js лениво импортирует этот файл и ожидает `open(ctx)`; ctx — тот же, что
 * у вьюх ({api, store, navigate, toast, route, room}), но без контейнера.
 *
 * В окне: поле, список команд (новый запрос, комнаты, вкладки, тема, настройки,
 * создание комнаты), последние запросы текущей комнаты и — с 2 символов —
 * результаты /api/search. Логика списка и скоринг — в palette-model.js.
 */

import { h, debounce } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, formatStatusRu } from '../ui.js';
import { TABS } from '../router.js';
import {
  buildCommandItems, composeList, flattenGroups, moveIndex,
} from './palette-model.js';

const VIEW_CSS = 'css/view-palette.css';

/** Открытый экземпляр (палитра одна на приложение). */
let active = null;

/** Применить тему так же, как app.js: dataset + localStorage + store. */
function applyTheme(ctx, value) {
  const pref = ['auto', 'light', 'dark'].includes(value) ? value : 'auto';
  try { document.documentElement.dataset.theme = pref; } catch { /* ignore */ }
  try { localStorage.setItem('teamroom.theme', pref); } catch { /* приватный режим */ }
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const resolved = pref === 'auto' ? (media && media.matches ? 'dark' : 'light') : pref;
  const store = ctx.store || {};
  if (store.theme && store.theme.set) store.theme.set({ pref, resolved });
  const names = { auto: 'как в системе', light: 'светлая', dark: 'тёмная' };
  if (typeof ctx.toast === 'function') ctx.toast('Тема: ' + names[pref], 'info');
}

/** Уйти по хэшу и (для нового запроса) поставить курсор в поле запроса. */
function go(ctx, hash, { focusComposer = false } = {}) {
  if (typeof ctx.navigate === 'function') ctx.navigate(hash);
  else location.hash = hash;
  if (!focusComposer) return;
  let tries = 0;
  const tick = () => {
    const field = document.querySelector('#view .textarea--composer, #view textarea, #view .composer textarea');
    if (field && typeof field.focus === 'function') { field.focus(); return; }
    if (tries++ < 24) setTimeout(tick, 60);
  };
  setTimeout(tick, 40);
}

/* --------------------------------------------------------------- разметка */

function commandItemRow(item, index, onPick) {
  const btn = h('button', {
    class: 'pal-item',
    type: 'button',
    role: 'option',
    id: 'pal-item-' + index,
    'aria-selected': 'false',
    dataset: { index: String(index) },
    onclick: (e) => { e.preventDefault(); onPick(item); },
    onmousemove: () => { if (typeof btn.__setActive === 'function') btn.__setActive(); },
  });
  btn.appendChild(h('span', { class: 'pal-item__icon', 'aria-hidden': 'true' }, icon(item.icon || 'chevron-right', { size: 14 })));
  const main = h('span', { class: 'pal-item__main' },
    h('span', { class: 'pal-item__title' }, String(item.title || '')),
    item.hint ? h('span', { class: 'pal-item__hint' }, String(item.hint)) : null,
  );
  btn.appendChild(main);
  if (item.external) btn.appendChild(h('span', { class: 'chip chip--outline pal-item__tag' }, 'найдено'));
  return btn;
}

function groupTitle(title) {
  return h('div', { class: 'pal-group', role: 'presentation' }, String(title));
}

/* ---------------------------------------------------------------- палитра */

/**
 * Открыть палитру. Повторный вызов при открытой палитре закрывает её
 * (так Ctrl+K работает как переключатель).
 * @param {object} ctx
 * @returns {Promise<{close: Function}|null>}
 */
export async function open(ctx = {}) {
  if (active) { active.close(); return null; }
  await ensureCss(VIEW_CSS);
  if (active) { active.close(); return null; }

  const api = ctx.api;
  const store = ctx.store || {};
  const roomList = (store.rooms && store.rooms.get && store.rooms.get('list')) || [];
  const currentRoomId = (ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || null;
  const themePref = (store.theme && store.theme.get && store.theme.get('pref')) || 'auto';

  const commands = buildCommandItems({
    rooms: roomList,
    currentRoomId,
    tabs: TABS,
    theme: themePref,
  });

  let recents = [];
  let hits = [];
  let query = '';
  let groups = composeList({ commands, recents, hits, query: '' });
  let rows = flattenGroups(groups);
  let activeIndex = rows.length ? 0 : -1;
  let stopped = false;
  let controller = null;
  let seq = 0;

  const input = h('input', {
    class: 'pal-input',
    type: 'text',
    role: 'combobox',
    'aria-label': 'Команда или поиск',
    'aria-autocomplete': 'list',
    'aria-controls': 'pal-list',
    'aria-expanded': 'true',
    placeholder: 'Команда, комната, запрос…',
    autocomplete: 'off',
    spellcheck: 'false',
  });

  const list = h('div', { class: 'pal-list', id: 'pal-list', role: 'listbox', 'aria-label': 'Команды и результаты' });
  const status = h('span', { class: 'pal-status small muted', 'aria-live': 'polite' });

  const dialog = h('div', {
    class: 'pal-dialog',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': 'Командная палитра',
  },
    h('div', { class: 'pal-search' }, icon('search', { size: 15, className: 'pal-search__icon' }), input),
    list,
    h('div', { class: 'pal-foot' },
      h('span', { class: 'pal-keys' },
        h('kbd', { class: 'kbd' }, '↑'), h('kbd', { class: 'kbd' }, '↓'), h('span', { class: 'muted small' }, 'выбор'),
        h('kbd', { class: 'kbd' }, 'Enter'), h('span', { class: 'muted small' }, 'открыть'),
        h('kbd', { class: 'kbd' }, 'Esc'), h('span', { class: 'muted small' }, 'закрыть'),
      ),
      h('span', { class: 'spacer' }),
      status,
      h('span', { class: 'small muted' }, 'Ctrl+K'),
    ),
  );

  const backdrop = h('div', { class: 'pal-backdrop' }, dialog);
  document.body.appendChild(backdrop);
  document.documentElement.classList.add('pal-open');

  /* ---------------------------------------------------------- отрисовка */

  function applyActive(scroll) {
    const items = Array.from(list.querySelectorAll('.pal-item'));
    items.forEach((node, i) => {
      const on = i === activeIndex;
      node.classList.toggle('is-active', on);
      node.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    const current = items[activeIndex];
    if (current) {
      input.setAttribute('aria-activedescendant', current.id);
      if (scroll && typeof current.scrollIntoView === 'function') {
        current.scrollIntoView({ block: 'nearest' });
      }
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function render() {
    rows = flattenGroups(groups);
    if (activeIndex >= rows.length) activeIndex = rows.length ? rows.length - 1 : -1;
    if (activeIndex < 0 && rows.length) activeIndex = 0;

    const frag = document.createDocumentFragment();
    let index = 0;
    for (const group of groups) {
      if (!group.items.length) continue;
      frag.appendChild(groupTitle(group.title));
      for (const item of group.items) {
        const row = commandItemRow(item, index, pick);
        const myIndex = index;
        row.__setActive = () => {
          if (activeIndex === myIndex) return;
          activeIndex = myIndex;
          applyActive(false);
        };
        frag.appendChild(row);
        index++;
      }
    }
    if (!rows.length) {
      frag.appendChild(h('div', { class: 'pal-empty' },
        h('div', { class: 'pal-empty__title' }, query ? 'Ничего не найдено' : 'Пока нечего показать'),
        h('div', { class: 'pal-empty small muted' },
          query ? 'Попробуйте другое слово — или начните с команды: «комната», «тема», «настройки».'
            : 'Введите команду или запрос. Например: «отчёты», «новый запрос», «тема».'),
      ));
    }
    list.replaceChildren(frag);
    applyActive(false);
    status.textContent = query.length >= 2 && rows.length
      ? rows.length + ' ' + (rows.length === 1 ? 'совпадение' : rows.length < 5 ? 'совпадения' : 'совпадений')
      : '';
  }

  /* --------------------------------------------------------------- данные */

  async function loadHits(q) {
    const my = ++seq;
    if (controller) { try { controller.abort(); } catch { /* ignore */ } }
    controller = typeof AbortController === 'function' ? new AbortController() : null;
    const params = new URLSearchParams({ q, limit: '12' });
    try {
      const payload = await api.get('/api/search?' + params.toString(), controller ? { signal: controller.signal } : undefined);
      if (stopped || my !== seq) return;
      hits = Array.isArray(payload && payload.items) ? payload.items : [];
    } catch (err) {
      if (stopped || my !== seq) return;
      if (err && err.name === 'AbortError') return;
      hits = [];
    }
    regroup();
  }

  async function loadRecents() {
    if (!currentRoomId) return;
    try {
      const payload = await api.get('/api/rooms/' + encodeURIComponent(currentRoomId) + '/requests?limit=6');
      if (stopped) return;
      const items = Array.isArray(payload && payload.items) ? payload.items : [];
      recents = items.slice(0, 6).map((req) => ({
        id: 'req:' + String(req.id),
        group: 'Последние запросы',
        icon: req.status === 'needs_you' ? 'bell' : 'comment-discussion',
        title: req.title || req.preview || req.id || 'Запрос',
        hint: [formatStatusRu(req.status) || req.status || '', req.needs_you ? 'ждёт вас' : '']
          .filter(Boolean).join(' · '),
        keywords: (req.text || req.preview || '') + ' ' + (req.title || ''),
        action: { type: 'navigate', hash: '#/room/' + encodeURIComponent(currentRoomId) + '/request/' + encodeURIComponent(req.id) },
      }));
    } catch {
      recents = [];
    }
    regroup();
  }

  function regroup() {
    groups = composeList({ commands, recents, hits, query });
    // Активная строка всегда первая: команда, последний запрос или первый результат.
    activeIndex = groups.length ? 0 : -1;
    render();
  }

  /* --------------------------------------------------------------- выбор */

  function pick(item) {
    if (!item || !item.action) return;
    const action = item.action;
    if (action.type === 'navigate') {
      const focusComposer = /\/composer$/.test(String(action.hash || '')) && query === '';
      close();
      go(ctx, action.hash, { focusComposer });
      return;
    }
    if (action.type === 'new-request') {
      close();
      go(ctx, '#/room/' + encodeURIComponent(action.room) + '/composer', { focusComposer: true });
      return;
    }
    if (action.type === 'theme') {
      applyTheme(ctx, action.value);
      commands.length = 0;
      commands.push(...buildCommandItems({ rooms: roomList, currentRoomId, tabs: TABS, theme: action.value }));
      regroup();
      return;
    }
    if (action.type === 'create-room') {
      close();
      go(ctx, '#/settings?new=1');
      if (typeof ctx.toast === 'function') ctx.toast('Заполните название и руководителя комнаты', 'info');
    }
  }

  function close() {
    if (stopped) return;
    stopped = true;
    active = null;
    document.removeEventListener('keydown', onKey, true);
    document.documentElement.classList.remove('pal-open');
    if (controller) { try { controller.abort(); } catch { /* ignore */ } }
    backdrop.remove();
  }

  /* ------------------------------------------------------------ клавиатура */

  function onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === 'Tab') {
      // В палитре один орган управления — не выпускаем Tab наружу.
      e.preventDefault();
      input.focus();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!rows.length) return;
      e.preventDefault();
      activeIndex = moveIndex(activeIndex, e.key === 'ArrowDown' ? 1 : -1, rows.length);
      applyActive(true);
      return;
    }
    if (e.key === 'Enter') {
      // У строки в фокусе Enter обработает нативный click — не дублируем.
      const target = e.target;
      if (target && typeof target.closest === 'function' && target.closest('.pal-item')) return;
      e.preventDefault();
      const item = rows[activeIndex];
      if (item) pick(item);
      return;
    }
    if (e.key === 'Home' || e.key === 'End') {
      if (!rows.length) return;
      e.preventDefault();
      activeIndex = e.key === 'Home' ? 0 : rows.length - 1;
      applyActive(true);
    }
  }

  const searchDebounced = debounce((q) => { loadHits(q); }, 190);

  input.addEventListener('input', () => {
    query = input.value.trim();
    hits = query.length >= 2 ? hits : [];
    regroup();
    if (query.length >= 2) searchDebounced(query);
  });

  document.addEventListener('keydown', onKey, true);
  backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) close(); });
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });

  render();
  try {
    input.focus({ preventScroll: true });
  } catch { /* ignore */ }

  active = { close, isOpen: () => !stopped };
  loadRecents();
  return active;
}

/** Закрыть палитру, если открыта. */
export function close() {
  if (active) active.close();
}

/** Открыта ли палитра. */
export function isOpen() {
  return Boolean(active);
}

export default { open, close, isOpen };
