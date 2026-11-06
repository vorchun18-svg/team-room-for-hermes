/**
 * notes-editor.js — окно-«блокнот» «Особенности для этой комнаты» (WAVE17 P6).
 *
 * Зачем: маленькое поле в карточке участника не вмещало длинные инструкции.
 * Теперь записи живут в большом модальном окне: слева список с поиском и
 * фильтром, справа — редактор выбранной записи на всю высоту, внизу — счётчик
 * лимитов, «Новая запись», «Закрепить», «Удалить», «Сократить», «Журнал».
 *
 *   import { openNotesEditor, notesLink } from './notes-editor.js';
 *   openNotesEditor({ ctx, roomId: 'cafe', profile: 'biz-cfo', label: 'Анна · финансы (CFO)' });
 *
 * Контракт для пакета E (карточка участника):
 *   const link = notesLink(ctx, { roomId, profile, label, room, onChange });
 *   // link — кнопка «Особенности (3)»; клик открывает это окно, счётчик
 *   // обновляется сам после каждой правки.
 *
 * Работает на уже существующем API (`server/routes_notes.py`):
 *   GET    …/notes                     записи, лимиты, состояние переоценки
 *   POST   …/notes/entries            новая запись
 *   PATCH  …/notes/entries/{e}        правка текста
 *   DELETE …/notes/entries/{e}?force=1 убрать
 *   POST   …/notes/entries/{e}/pin    закрепить/снять закрепление (WAVE17)
 *   POST   …/notes/review             переоценка (предпросмотр → применение)
 *   POST   …/notes/journal/{e}/rollback  откат изменения
 *
 * Правила волны: только токены и классы дизайн-системы, русский интерфейс,
 * светлая/тёмная темы, микрофон — отдельной кнопкой в потоке (как в composer),
 * никакого position:absolute поверх поля; Esc/Ctrl+S; ловушка фокуса.
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { ensureCss, confirmDialog } from './ui.js';
import { createMic } from './dictation.js';
import { entriesOfItem, MAX_TEXT, SOURCE_RU, ACTION_RU, ROLLBACKABLE } from './agent-notes-ui.js';

/** Подзаголовок окна — дословно из ТЗ. */
export const EDITOR_SUBTITLE = 'Добавка к SOUL.md только для этой комнаты. '
  + 'Профиль Hermes не меняется.';

/** Лимиты по умолчанию, если сервер ещё не ответил. */
export const DEFAULT_LIMITS = { max_entries: 24, max_text: MAX_TEXT, review_every: 5 };

const FILTERS = [
  { key: 'all', label: 'Все' },
  { key: 'user', label: 'Ваши' },
  { key: 'leader', label: 'Руководителя' },
];

function enc(value) {
  return encodeURIComponent(String(value || ''));
}

/** «Особенности для этой комнаты — Анна · финансы (CFO)». */
export function editorTitle(label) {
  const who = String(label || '').replace(/\s+/g, ' ').trim();
  return 'Особенности для этой комнаты' + (who ? ' — ' + who : '');
}

/** Кто добавил запись, по-русски: «вы» / «руководитель». */
export function sourceRu(entry) {
  const source = String((entry && entry.source) || 'leader');
  if (source === 'user') return 'вы';
  if (source === 'auto') return 'руководитель (авто)';
  return 'руководитель';
}

/** «3 из 24 записей · 210 из 8000 знаков». */
export function limitsText(entries, limits) {
  const list = Array.isArray(entries) ? entries : [];
  const lim = Object.assign({}, DEFAULT_LIMITS, limits || {});
  const chars = list.reduce((sum, one) => sum + String((one && one.text) || '').length, 0);
  return list.length + ' из ' + lim.max_entries + ' записей · '
    + chars + ' из ' + lim.max_text + ' знаков';
}

/** Состояние лимита: '' | 'warn' (близко) | 'over' (уже за лимитом). */
export function limitsState(entries, limits) {
  const list = Array.isArray(entries) ? entries : [];
  const lim = Object.assign({}, DEFAULT_LIMITS, limits || {});
  const chars = list.reduce((sum, one) => sum + String((one && one.text) || '').length, 0);
  if (list.length > lim.max_entries || chars > lim.max_text) return 'over';
  if (list.length >= lim.max_entries - 1 || chars >= Math.floor(lim.max_text * 0.85)) return 'warn';
  return '';
}

/** Поиск и фильтр «все / ваши / руководителя» (чистая функция — её проверяют тесты). */
export function filterEntries(entries, query, who) {
  const list = Array.isArray(entries) ? entries : [];
  const needle = String(query || '').trim().toLowerCase();
  return list.filter((entry) => {
    if (!entry) return false;
    const source = String(entry.source || 'leader');
    if (who === 'user' && source !== 'user') return false;
    if (who === 'leader' && source === 'user') return false;
    if (!needle) return true;
    const hay = [entry.text, entry.origin, sourceRu(entry)].join(' ').toLowerCase();
    return hay.includes(needle);
  });
}

/** Строка под записью: «вы · 05.11 · пригодилось 2 раза». */
export function metaLine(entry) {
  const one = entry || {};
  const parts = [sourceRu(one)];
  const date = shortDate(one.created_at);
  if (date) parts.push(date);
  if (Number(one.used_count) > 0) {
    parts.push('пригодилось ' + Math.round(Number(one.used_count)) + ' '
      + plural(Number(one.used_count), ['раз', 'раза', 'раз']));
  }
  return parts.join(' · ');
}

function plural(n, forms) {
  const num = Math.abs(Number(n) || 0) % 100;
  if (num >= 11 && num <= 14) return forms[2];
  const last = num % 10;
  if (last === 1) return forms[0];
  if (last >= 2 && last <= 4) return forms[1];
  return forms[2];
}

/** Дата записи: «05.11», а чужие годы — «05.11.25». */
export function shortDate(ts) {
  const n = Number(ts) || 0;
  if (n <= 0) return '';
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const day = String(d.getDate()).padStart(2, '0');
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const year = d.getFullYear();
  const now = new Date().getFullYear();
  return day + '.' + month + (year !== now ? '.' + String(year).slice(2) : '');
}

/** Первые строки записи для списка (полный текст — в редакторе и подсказке). */
export function previewText(text, limit = 220) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : flat.slice(0, limit - 1).trimEnd() + '…';
}

function focusables(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return [];
  const list = root.querySelectorAll('button, [href], input, select, textarea, [tabindex]');
  return Array.from(list).filter((el) => {
    if (!el) return false;
    if (el.disabled === true) return false;
    if (el.getAttribute && el.getAttribute('tabindex') === '-1') return false;
    if (el.hidden === true) return false;
    if (el.getAttribute && el.getAttribute('hidden') !== null) return false;
    return true;
  });
}

function trapTab(ev, root) {
  if (!ev || ev.key !== 'Tab') return;
  const items = focusables(root);
  if (!items.length) { ev.preventDefault(); return; }
  const doc = globalThis.document;
  const active = doc ? doc.activeElement : null;
  const first = items[0];
  const last = items[items.length - 1];
  if (ev.shiftKey && (active === first || !root.contains(active))) {
    ev.preventDefault();
    if (typeof last.focus === 'function') last.focus();
  } else if (!ev.shiftKey && active === last) {
    ev.preventDefault();
    if (typeof first.focus === 'function') first.focus();
  }
}

/* ─────────────────────────────────────────────────────────────── окно */

/**
 * Открыть окно-«блокнот». Возвращает управление сразу; данные читаются из API.
 * @param {{ctx?: object, roomId?: string, profile?: string, label?: string,
 *          entries?: Array, limits?: object, onChange?: Function,
 *          initialEntryId?: string, api?: object}} opts
 * @returns {{element: HTMLElement, loaded: Promise, close: Function, reload: Function,
 *            destroy: Function, entries: Function, isOpen: Function}}
 */
export function openNotesEditor(opts = {}) {
  const o = opts || {};
  const ctx = o.ctx || {};
  const api = o.api || ctx.api || null;
  const roomId = String(o.roomId || (ctx.room && ctx.room.id) || '');
  const profile = String(o.profile || '');
  const label = String(o.label || profile).trim();
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const onChange = typeof o.onChange === 'function' ? o.onChange : () => {};
  const base = '/api/rooms/' + enc(roomId) + '/agents/' + enc(profile);

  let entries = entriesOfItem({ entries: o.entries || [] });
  let limits = Object.assign({}, DEFAULT_LIMITS, o.limits || {});
  let selectedId = String(o.initialEntryId || '');
  let mode = selectedId ? 'edit' : 'new';
  let stored = '';
  let dirty = false;
  let busy = false;
  let destroyed = false;
  let loaded = false;
  let who = 'all';
  let query = '';
  let pending = null;          // что сделать после решения о несохранённом
  let lastFocused = null;      // вернуть фокус при закрытии

  /* ── каркас ───────────────────────────────────────────────────── */

  const titleId = 'notes-ed-title-' + Math.random().toString(36).slice(2, 8);
  const titleEl = h('h2', { class: 'dialog__title notes-ed__title', id: titleId }, editorTitle(label));
  const closeBtn = h('button', {
    class: 'btn btn--ghost btn--icon btn--sm', type: 'button',
    title: 'Закрыть (Esc)', 'aria-label': 'Закрыть',
    onclick: () => { requestClose(); },
  }, icon('close', { size: 14 }));

  const sub = h('p', { class: 'notes-ed__sub', id: titleId + '-sub' }, EDITOR_SUBTITLE);

  const head = h('div', { class: 'dialog__head notes-ed__head' },
    h('div', { class: 'notes-ed__titles' }, titleEl, sub),
    closeBtn);

  /* левая часть: поиск, фильтр, список */
  const search = h('input', {
    class: 'input notes-ed__search', type: 'search', 'data-mic': 'off',
    placeholder: 'Найти в записях', 'aria-label': 'Поиск по записям',
    autocomplete: 'off',
  });
  search.addEventListener('input', () => { query = search.value; paintList(); });

  const filterBtns = FILTERS.map((one) => {
    const btn = h('button', {
      class: 'notes-ed__filter', type: 'button', 'data-who': one.key,
      'aria-pressed': one.key === who ? 'true' : 'false',
      onclick: () => { who = one.key; paintFilters(); paintList(); },
    }, one.label);
    return btn;
  });
  const filters = h('div', { class: 'notes-ed__filters', role: 'group', 'aria-label': 'Чьи записи' },
    ...filterBtns);

  // Список — кнопки в группе (кнопка остаётся кнопкой для скринридера).
  const listBox = h('div', { class: 'notes-ed__list', role: 'group', 'aria-label': 'Записи' });
  const listNote = h('div', { class: 'notes-ed__listnote small muted' });
  const leftPane = h('section', { class: 'notes-ed__left', 'aria-label': 'Список записей' },
    h('div', { class: 'notes-ed__searchrow' }, icon('search', { size: 13 }), search),
    filters, listBox, listNote);

  /* правая часть: редактор записи */
  const entryMeta = h('div', { class: 'notes-ed__entrymeta small muted' });
  const pinMark = h('span', { class: 'notes-ed__pin hidden', title: 'Запись закреплена' },
    icon('pin', { size: 12 }), 'закреплено');
  const area = h('textarea', {
    class: 'input notes-ed__area', rows: '8', spellcheck: 'false',
    // Микрофон ставим сами (как composer): место занято, авто-кнопка не нужна.
    'data-mic': 'manual',
    placeholder: 'Что делать иначе именно в этой комнате? Например: «Отвечай коротко '
      + 'и только цифрами» или «Ornith слабая модель, но локальная — простые задачи ей».',
    'aria-label': 'Текст особенности',
  });
  const micBtn = createMic(area, { label: 'Надиктовать', className: 'notes-ed__mic' });
  const saveBtn = h('button', { class: 'btn btn--primary notes-ed__save', type: 'button' },
    icon('check', { size: 13 }), 'Сохранить');
  const statusEl = h('span', { class: 'notes-ed__status small muted', role: 'status' });
  const editRow = h('div', { class: 'notes-ed__editrow' }, micBtn, saveBtn, statusEl);

  const journalBox = h('section', { class: 'notes-ed__journal hidden',
    'aria-label': 'Журнал изменений' });
  const editorHost = h('div', { class: 'notes-ed__editor' },
    h('div', { class: 'notes-ed__entryhead' }, entryMeta, pinMark),
    area, editRow);
  const rightPane = h('section', { class: 'notes-ed__right', 'aria-label': 'Редактор записи' },
    editorHost, journalBox);

  const body = h('div', { class: 'dialog__body notes-ed__body' }, leftPane, rightPane);

  /* подвал: лимиты, действия */
  const limitEl = h('span', { class: 'notes-ed__limit tnum' });
  const limitHint = h('span', { class: 'notes-ed__limithint small muted' });
  const newBtn = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' },
    icon('plus', { size: 12 }), 'Новая запись');
  const pinLabel = h('span', {}, 'Закрепить');
  const pinBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' },
    icon('pin', { size: 12 }), pinLabel);
  const delBtn = h('button', { class: 'btn btn--ghost btn--sm notes-ed__danger', type: 'button' },
    icon('trash', { size: 12 }), 'Удалить');
  const reviewBtn = h('button', {
    class: 'btn btn--ghost btn--sm', type: 'button',
    title: 'Слить дубли и убрать неиспользованное — закреплённые записи не трогаем',
  }, 'Сократить');
  const journalBtn = h('button', {
    class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': 'false',
  }, icon('history', { size: 12 }), 'Журнал');
  const closeFoot = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' },
    'Закрыть');

  const foot = h('div', { class: 'dialog__foot notes-ed__foot' },
    h('div', { class: 'notes-ed__limits' }, limitEl, limitHint),
    h('div', { class: 'notes-ed__actions' },
      newBtn, pinBtn, delBtn, reviewBtn, journalBtn, closeFoot));

  /* полоса «не сохранено» — вместо вложенного диалога */
  const leaveText = h('span', { class: 'notes-ed__leavetext' }, 'Есть несохранённые изменения');
  const leaveSave = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, 'Сохранить');
  const leaveDrop = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Не сохранять');
  const leaveBack = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'Вернуться');
  const leaveBar = h('div', { class: 'notes-ed__leave hidden', role: 'alertdialog',
    'aria-label': 'Несохранённые изменения' }, leaveText,
  h('div', { class: 'notes-ed__leaveacts' }, leaveSave, leaveDrop, leaveBack));

  const dialog = h('div', {
    class: 'dialog notes-ed', role: 'dialog', 'aria-modal': 'true',
    'aria-labelledby': titleId, 'aria-describedby': titleId + '-sub',
  }, head, body, leaveBar, foot);
  const overlay = h('div', { class: 'dialog-backdrop notes-ed__backdrop' }, dialog);

  /* ── отрисовка ────────────────────────────────────────────────── */

  function paintFilters() {
    for (const btn of filterBtns) {
      const on = btn.getAttribute('data-who') === who;
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.classList.toggle('notes-ed__filter--on', on);
    }
  }

  function selectedEntry() {
    return entries.find((one) => one.id && one.id === selectedId) || null;
  }

  function paintList() {
    if (destroyed) return;
    const items = filterEntries(entries, query, who);
    listBox.replaceChildren();
    if (!loaded) {
      listBox.appendChild(h('div', { class: 'notes-ed__state small muted' }, 'Загружаю…'));
      return;
    }
    if (!entries.length) {
      listBox.appendChild(h('div', { class: 'notes-ed__state small muted' },
        'Особенностей пока нет — команда работает по своей роли. Начните с «Новая запись».'));
      return;
    }
    if (!items.length) {
      listBox.appendChild(h('div', { class: 'notes-ed__state small muted' },
        query.trim() ? 'Ничего не нашлось — измените запрос.' : 'У этого фильтра записей нет.'));
      return;
    }
    for (const entry of items) {
      const on = mode === 'edit' && entry.id === selectedId;
      const item = h('button', {
        class: 'notes-ed__item' + (on ? ' notes-ed__item--on' : '') + (entry.pinned ? ' notes-ed__item--pinned' : ''),
        type: 'button', 'data-id': entry.id,
        'aria-current': on ? 'true' : 'false',
        title: previewText(entry.text, 400),
        onclick: () => { select(entry.id); },
      },
      h('span', { class: 'notes-ed__itemhead' },
        entry.pinned ? icon('pin', { size: 11 }) : null,
        h('span', { class: 'notes-ed__itemsrc' }, sourceRu(entry)),
        h('span', { class: 'notes-ed__itemdate' }, shortDate(entry.created_at))),
      h('span', { class: 'notes-ed__itemtext' }, previewText(entry.text)),
      entry.used_count > 0
        ? h('span', { class: 'notes-ed__itemused' },
          'пригодилось ' + entry.used_count + ' ' + plural(entry.used_count, ['раз', 'раза', 'раз']))
        : null);
      listBox.appendChild(item);
    }
  }

  function paintEditor() {
    if (destroyed) return;
    const entry = mode === 'edit' ? selectedEntry() : null;
    if (mode === 'new') {
      entryMeta.textContent = 'Новая запись';
      pinMark.classList.add('hidden');
    } else if (entry) {
      entryMeta.textContent = metaLine(entry) + (entry.origin ? ' · ' + entry.origin : '');
      pinMark.classList.toggle('hidden', !entry.pinned);
    } else {
      entryMeta.textContent = '';
      pinMark.classList.add('hidden');
    }
    const canEditEntry = mode === 'edit' && Boolean(entry);
    pinBtn.disabled = busy || !canEditEntry;
    delBtn.disabled = busy || !canEditEntry;
    pinLabel.textContent = entry && entry.pinned ? 'Снять закрепление' : 'Закрепить';
    saveBtn.disabled = busy;
    micBtn.disabled = busy;
  }

  function paintLimits() {
    if (destroyed) return;
    const state = limitsState(entries, limits);
    const chars = entries.reduce((sum, one) => sum + String(one.text || '').length, 0);
    limitEl.textContent = limitsText(entries, limits);
    limitEl.classList.toggle('notes-ed__limit--warn', state === 'warn');
    limitEl.classList.toggle('notes-ed__limit--over', state === 'over');
    if (state === 'over') {
      limitHint.textContent = 'За лимитом: лишнее уберётся при переоценке — лучше сократить.';
    } else if (state === 'warn') {
      limitHint.textContent = 'Лимит близко. Длинные записи лучше разбить на короткие.';
    } else if (chars > 0 && chars > limits.max_text * 0.7) {
      limitHint.textContent = 'Знаков много — агенту важнее короткие точные записи.';
    } else {
      limitHint.textContent = '';
    }
  }

  function paint() {
    paintFilters();
    paintList();
    paintEditor();
    paintLimits();
  }

  /* ── выбор записи и несохранённое ─────────────────────────────── */

  function markDirty() {
    const next = mode === 'new' ? area.value.trim().length > 0 : area.value !== stored;
    if (next === dirty) return;
    dirty = next;
    if (!dirty) hideLeave();
  }

  function loadInto(entry) {
    if (entry) {
      mode = 'edit';
      selectedId = entry.id;
      stored = String(entry.text || '');
      area.value = stored;
    } else {
      mode = 'new';
      selectedId = '';
      stored = '';
      area.value = '';
    }
    dirty = false;
    hideLeave();
    statusEl.textContent = '';
    paint();
  }

  function select(id) {
    if (busy) return;
    if (dirty) { pending = { kind: 'select', id: String(id || '') }; showLeave('Сначала сохраните правку или отмените её'); return; }
    const entry = entries.find((one) => one.id === id) || null;
    if (!entry) return;
    loadInto(entry);
    if (typeof area.focus === 'function') area.focus();
  }

  function showLeave(text) {
    leaveText.textContent = text || 'Есть несохранённые изменения';
    leaveBar.classList.remove('hidden');
    if (typeof leaveSave.focus === 'function') leaveSave.focus();
  }

  function hideLeave() {
    leaveBar.classList.add('hidden');
    pending = null;
  }

  /** Продолжить отложенное действие (сохранили или отказались от правки). */
  function resumePending() {
    const action = pending;
    pending = null;
    leaveBar.classList.add('hidden');
    if (!action) return;
    if (action.kind === 'close') { destroy(); return; }
    if (action.kind === 'select') {
      const entry = entries.find((one) => one.id === action.id) || null;
      if (entry) loadInto(entry);
    }
  }

  /* ── сохранение ──────────────────────────────────────────────── */

  function totalChars(nextText) {
    const entry = mode === 'edit' ? selectedEntry() : null;
    let sum = 0;
    for (const one of entries) {
      sum += (entry && one.id === entry.id) ? String(nextText || '').length : String(one.text || '').length;
    }
    if (mode === 'new') sum += String(nextText || '').length;
    return sum;
  }

  async function save() {
    if (busy) return false;
    const text = String(area.value || '').trim();
    if (!text) { statusEl.textContent = 'Напишите или надиктуйте особенность'; if (typeof area.focus === 'function') area.focus(); return false; }
    if (totalChars(text) > limits.max_text) {
      statusEl.textContent = 'Вместе больше ' + limits.max_text + ' знаков — сократите текст';
      return false;
    }
    if (!api) { statusEl.textContent = 'Сохранение недоступно'; return false; }
    busy = true; paint();
    try {
      let data;
      if (mode === 'edit' && selectedId) {
        data = await api.patch(base + '/notes/entries/' + enc(selectedId),
          { text, why: 'правка в блокноте комнаты' });
      } else {
        data = await api.post(base + '/notes/entries',
          { text, source: 'user', origin: 'блокнот комнаты', why: 'добавлено в блокноте' });
      }
      const next = entriesOfItem({ entries: (data && data.entries) || [] });
      entries = next;
      if (data && data.limits) limits = Object.assign({}, limits, data.limits);
      const created = (data && data.entry) || null;
      const target = created && created.id
        ? entries.find((one) => one.id === created.id) || null
        : entries.find((one) => String(one.text || '') === text) || null;
      mode = 'edit';
      selectedId = target ? target.id : '';
      stored = target ? String(target.text || '') : text;
      area.value = stored;
      dirty = false;
      hideLeave();
      statusEl.textContent = data && data.duplicate ? 'Такая запись уже есть' : 'Сохранено';
      toast(data && data.duplicate ? 'Такая особенность уже записана' : 'Особенность сохранена', 'ok');
      paint();
      notify();
      return true;
    } catch (err) {
      statusEl.textContent = (err && err.message) || 'Не сохранено — попробуйте ещё раз';
      toast((err && err.message) || 'Не удалось сохранить запись', 'error');
      return false;
    } finally {
      busy = false; paint();
    }
  }

  function notify() {
    try {
      onChange({
        profile, entries: entries.slice(), count: entries.length,
        chars: entries.reduce((sum, one) => sum + String(one.text || '').length, 0),
        text: entries.map((one) => one.text).join('\n'),
      });
    } catch { /* подписчик не должен ломать окно */ }
  }

  /* ── действия ────────────────────────────────────────────────── */

  async function togglePin() {
    const entry = selectedEntry();
    if (!entry || busy || mode !== 'edit') return;
    busy = true; paint();
    try {
      const data = await api.post(base + '/notes/entries/' + enc(entry.id) + '/pin',
        { pinned: !entry.pinned });
      entries = entriesOfItem({ entries: (data && data.entries) || [] });
      statusEl.textContent = entry.pinned ? 'Закрепление снято' : 'Запись закреплена';
      toast(entry.pinned ? 'Закрепление снято' : 'Запись закреплена: переоценка её не трогает', 'ok');
      paint();
      notify();
    } catch (err) {
      toast((err && err.message) || 'Не удалось изменить закрепление', 'error');
    } finally {
      busy = false; paint();
    }
  }

  async function removeEntry() {
    const entry = selectedEntry();
    if (!entry || busy || mode !== 'edit') return;
    const ok = await confirmDialog({
      title: 'Удалить запись',
      text: entry.pinned
        ? 'Запись закреплена. Убрать её? Вернуть можно из журнала («Откатить»).'
        : 'Убрать эту запись? Вернуть можно из журнала («Откатить»).',
      confirmLabel: 'Удалить', danger: true,
    });
    if (!ok) return;
    busy = true; paint();
    try {
      const data = await api.del(base + '/notes/entries/' + enc(entry.id) + '?force=1&why='
        + enc('удалено в блокноте'), {});
      entries = entriesOfItem({ entries: (data && data.entries) || [] });
      const fallback = entries.find((one) => one.id !== entry.id) || entries[0] || null;
      loadInto(fallback);
      statusEl.textContent = 'Запись убрана — откат есть в журнале';
      toast('Запись убрана — откат есть в журнале', 'ok');
      notify();
    } catch (err) {
      toast((err && err.message) || 'Не удалось убрать запись', 'error');
    } finally {
      busy = false; paint();
    }
  }

  async function reviewNow() {
    if (busy) return;
    busy = true; paint();
    try {
      const preview = await api.post(base + '/notes/review', { apply: false });
      const drops = (preview && preview.drops) || [];
      const merges = (preview && preview.merges) || [];
      if (!drops.length && !merges.length) {
        statusEl.textContent = 'Сокращать нечего';
        toast('Сокращать нечего: дублей и устаревшего нет', 'ok');
        return;
      }
      const lines = drops.slice(0, 6).map((one) => '• ' + previewText(one.text, 90)).join('\n');
      const ok = await confirmDialog({
        title: 'Сократить записи',
        text: 'Уберём ' + drops.length + ' ' + plural(drops.length, ['запись', 'записи', 'записей'])
          + (merges.length ? ', сольём дублей: ' + merges.length : '')
          + '. Закреплённые записи не трогаем.\n\n' + lines,
        confirmLabel: 'Сократить',
      });
      if (!ok) return;
      const data = await api.post(base + '/notes/review',
        { apply: true, by: 'user', why: 'сокращение в блокноте' });
      entries = entriesOfItem({ entries: (data && data.entries) || [] });
      if (!entries.some((one) => one.id === selectedId)) {
        loadInto(entries[0] || null);
      }
      toast('Записей ' + (data && data.before) + ' → ' + (data && data.after) + '. Откат — в журнале', 'ok');
      paint();
      notify();
      await loadJournal();
    } catch (err) {
      toast((err && err.message) || 'Не удалось сократить', 'error');
    } finally {
      busy = false; paint();
    }
  }

  async function rollback(event) {
    const ok = await confirmDialog({
      title: 'Откатить изменение',
      text: 'Вернуть записи такими, какими они были до этого изменения?',
      confirmLabel: 'Откатить',
    });
    if (!ok) return;
    try {
      const data = await api.post('/api/rooms/' + enc(roomId) + '/notes/journal/'
        + enc(event.id) + '/rollback', { why: 'откат в блокноте' });
      entries = entriesOfItem({ entries: (data && data.entries) || [] });
      const keep = entries.find((one) => one.id === selectedId) || entries[0] || null;
      loadInto(keep);
      toast('Изменение откачено', 'ok');
      notify();
      await loadJournal();
    } catch (err) {
      toast((err && err.message) || 'Не удалось откатить', 'error');
    }
  }

  async function loadJournal() {
    journalBox.replaceChildren(
      h('div', { class: 'notes-ed__jhead' },
        h('span', { class: 'notes-ed__jtitle' }, 'Журнал изменений'),
        h('button', {
          class: 'btn btn--ghost btn--icon btn--sm', type: 'button',
          title: 'Скрыть журнал', 'aria-label': 'Скрыть журнал',
          onclick: () => { hideJournal(); },
        }, icon('close', { size: 13 }))),
      h('div', { class: 'small muted' }, 'Загружаю…'));
    try {
      const data = await api.get('/api/rooms/' + enc(roomId) + '/notes/journal?limit=50');
      const events = ((data && data.events) || [])
        .filter((one) => !one.profile || one.profile === profile);
      const list = h('div', { class: 'notes-ed__jlist' });
      if (!events.length) {
        list.appendChild(h('div', { class: 'small muted' }, 'Изменений пока не было.'));
      }
      for (const event of events) {
        const when = shortDate(event.at) || '—';
        const action = ACTION_RU[event.action] || event.action || 'изменение';
        const row = h('div', { class: 'notes-journal__row' },
          h('div', { class: 'notes-journal__head small' },
            h('span', { class: 'notes-journal__who' },
              SOURCE_RU[event.source] || event.actor || 'руководитель'),
            h('span', { class: 'notes-journal__act' }, action),
            h('span', { class: 'notes-journal__when muted' }, when)),
          event.text ? h('div', { class: 'notes-journal__text small' },
            previewText(event.text, 400)) : null,
          event.why ? h('div', { class: 'small muted' }, 'зачем: ' + event.why) : null,
          event.rolled_back
            ? h('div', { class: 'small muted' }, 'изменение откачено')
            : (ROLLBACKABLE.has(event.action) ? h('button', {
              class: 'btn btn--ghost btn--sm', type: 'button',
              onclick: () => { rollback(event).catch(() => {}); },
            }, 'Откатить') : null));
        list.appendChild(row);
      }
      journalBox.replaceChildren(
        h('div', { class: 'notes-ed__jhead' },
          h('span', { class: 'notes-ed__jtitle' }, 'Журнал изменений'),
          h('button', {
            class: 'btn btn--ghost btn--icon btn--sm', type: 'button',
            title: 'Скрыть журнал', 'aria-label': 'Скрыть журнал',
            onclick: () => { hideJournal(); },
          }, icon('close', { size: 13 }))),
        list);
    } catch (err) {
      journalBox.replaceChildren(
        h('div', { class: 'notes-ed__jhead' },
          h('span', { class: 'notes-ed__jtitle' }, 'Журнал изменений')),
        h('div', { class: 'small muted' }, 'Журнал не загрузился — попробуйте ещё раз'));
    }
  }

  function showJournal() {
    journalBox.classList.remove('hidden');
    editorHost.classList.add('hidden');
    journalBtn.setAttribute('aria-expanded', 'true');
    loadJournal().catch(() => {});
    const first = journalBox.querySelector && journalBox.querySelector('button');
    if (first && typeof first.focus === 'function') first.focus();
  }

  function hideJournal() {
    journalBox.classList.add('hidden');
    editorHost.classList.remove('hidden');
    journalBtn.setAttribute('aria-expanded', 'false');
    if (typeof area.focus === 'function') area.focus();
  }

  function startNew() {
    if (busy) return;
    if (dirty) { pending = { kind: 'select', id: '' }; showLeave('Сначала сохраните правку или отмените её'); return; }
    hideJournal();
    loadInto(null);
    statusEl.textContent = 'Новая запись — напишите или надиктуйте';
    if (typeof area.focus === 'function') area.focus();
  }

  /* ── загрузка и закрытие ─────────────────────────────────────── */

  async function reload() {
    if (!api) { loaded = true; paint(); return; }
    try {
      const data = await api.get(base + '/notes');
      entries = entriesOfItem({ entries: (data && data.entries) || [] });
      if (data && data.limits) limits = Object.assign({}, DEFAULT_LIMITS, data.limits);
      loaded = true;
      const keep = entries.find((one) => one.id === selectedId) || null;
      if (keep) {
        mode = 'edit';
        stored = String(keep.text || '');
      } else if (entries.length) {
        mode = 'edit';
        selectedId = entries[0].id;
        stored = String(entries[0].text || '');
        area.value = stored;
      } else {
        mode = 'new';
        selectedId = '';
        stored = '';
        area.value = '';
      }
      dirty = false;
      paint();
    } catch (err) {
      loaded = true;
      listBox.replaceChildren(h('div', { class: 'notes-ed__state small' },
        'Не удалось прочитать записи. ', h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button',
          onclick: () => { reload().catch(() => {}); },
        }, 'Повторить')));
      statusEl.textContent = (err && err.message) || 'Записи не загрузились';
    }
  }

  function requestClose() {
    if (busy) return;
    if (dirty) { pending = { kind: 'close' }; showLeave('Закрыть окно без сохранения?'); return; }
    destroy();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    const host = globalThis.window || globalThis.document;
    if (host && typeof host.removeEventListener === 'function') host.removeEventListener('keydown', onKey, true);
    if (lastFocused && lastFocused.isConnected !== false && typeof lastFocused.focus === 'function') {
      try { lastFocused.focus(); } catch { /* узел уже исчез */ }
    }
    try { micBtn.__micDestroy && micBtn.__micDestroy(); } catch { /* необязательно */ }
  }

  /* ── клавиатура ──────────────────────────────────────────────── */

  function onKey(ev) {
    if (!ev || destroyed) return;
    // Вложенное окно (подтверждение удаления/отката) — события его, не наши.
    // Сравниваем с ПОСЛЕДНИМ backdrop: за нами может стоять диалог комнаты.
    const doc = globalThis.document;
    const all = doc && doc.querySelectorAll ? Array.from(doc.querySelectorAll('.dialog-backdrop')) : [];
    if (all.length && all[all.length - 1] !== overlay) {
      // Сверху вложенное подтверждение: Esc закрывает ЕГО (иначе обработчик
      // диалога комнаты на document закроет и комнату — он не проверяет вложенность).
      if (ev.key === 'Escape') {
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        if (typeof ev.stopImmediatePropagation === 'function') ev.stopImmediatePropagation();
        else if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
        const cancel = all[all.length - 1].querySelector
          && all[all.length - 1].querySelector('.dialog__foot button');
        if (cancel && typeof cancel.click === 'function') cancel.click();
      }
      return;
    }
    if (ev.key === 'Escape') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      // stopImmediatePropagation: иначе Esc закроет и диалог комнаты под нами —
      // он слушает keydown на document и зарегистрировался раньше.
      if (typeof ev.stopImmediatePropagation === 'function') ev.stopImmediatePropagation();
      else if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
      if (!leaveBar.classList.contains('hidden')) { hideLeave(); if (typeof area.focus === 'function') area.focus(); return; }
      requestClose();
      return;
    }
    if (ev.key === 'Tab') { trapTab(ev, dialog); return; }
    if ((ev.key === 's' || ev.key === 'S' || ev.key === 'ы' || ev.key === 'Ы')
      && (ev.ctrlKey || ev.metaKey)) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      save().then((ok) => { if (ok) resumePending(); }).catch(() => {});
    }
  }

  listBox.addEventListener('keydown', (ev) => {
    if (!ev || (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp')) return;
    const doc = globalThis.document;
    const items = Array.from(listBox.querySelectorAll('.notes-ed__item'));
    if (!items.length) return;
    const active = doc ? doc.activeElement : null;
    let idx = items.indexOf(active);
    if (idx === -1) idx = 0;
    else idx = ev.key === 'ArrowDown' ? Math.min(items.length - 1, idx + 1) : Math.max(0, idx - 1);
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    if (typeof items[idx].focus === 'function') items[idx].focus();
  });

  area.addEventListener('input', () => { markDirty(); });
  area.addEventListener('keydown', (ev) => {
    if (ev && ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      save().then((ok) => { if (ok) resumePending(); }).catch(() => {});
    }
  });

  saveBtn.addEventListener('click', () => { save().then((ok) => { if (ok) resumePending(); }).catch(() => {}); });
  newBtn.addEventListener('click', startNew);
  pinBtn.addEventListener('click', () => { togglePin().catch(() => {}); });
  delBtn.addEventListener('click', () => { removeEntry().catch(() => {}); });
  reviewBtn.addEventListener('click', () => { reviewNow().catch(() => {}); });
  journalBtn.addEventListener('click', () => {
    if (journalBox.classList.contains('hidden')) showJournal(); else hideJournal();
  });
  closeFoot.addEventListener('click', () => { requestClose(); });
  leaveSave.addEventListener('click', () => { save().then((ok) => { if (ok) resumePending(); }).catch(() => {}); });
  leaveDrop.addEventListener('click', () => {
    dirty = false;
    if (mode === 'new') area.value = '';
    else area.value = stored;
    statusEl.textContent = '';
    paint();
    resumePending();
  });
  leaveBack.addEventListener('click', () => { hideLeave(); if (typeof area.focus === 'function') area.focus(); });
  overlay.addEventListener('mousedown', (ev) => { if (ev && ev.target === overlay) requestClose(); });

  /* ── монтаж ──────────────────────────────────────────────────── */

  ensureCss('css/agent-notes.css');
  const doc = globalThis.document;
  lastFocused = doc ? doc.activeElement : null;
  if (doc && doc.body) doc.body.appendChild(overlay);
  // Слушаем window в capture: раньше всех обработчиков страницы и диалога комнаты.
  const keyHost = globalThis.window || doc;
  if (keyHost && typeof keyHost.addEventListener === 'function') keyHost.addEventListener('keydown', onKey, true);
  if (doc && doc.getElementById) {
    const app = doc.getElementById('app');
    if (app && app.classList && app.classList.contains('nav-open')) app.classList.remove('nav-open');
  }
  paint();
  if (mode === 'edit') {
    const entry = selectedEntry();
    if (entry) { stored = String(entry.text || ''); area.value = stored; }
  }
  if (typeof area.focus === 'function') area.focus();
  const loadedPromise = reload();

  return {
    element: dialog,
    loaded: loadedPromise,
    close() { requestClose(); },
    reload() { return reload(); },
    destroy,
    entries() { return entries.slice(); },
    isOpen() { return !destroyed; },
  };
}

/* ─────────────────────────────────────── ссылка «Особенности (N)» */

/**
 * Кнопка «Особенности (N)» → открывает окно-«блокнот». Для карточки участника
 * (пакет E) и списка в диалоге комнаты.
 *
 * @param {object} ctx  {api, toast}
 * @param {{roomId?: string, profile: string, label?: string, room?: object,
 *          count?: number, onChange?: Function, variant?: 'link'|'btn'}} opts
 * @returns {HTMLElement}
 */
export function notesLink(ctx, opts = {}) {
  const o = opts || {};
  const profile = String(o.profile || '');
  const roomId = String(o.roomId || (o.room && o.room.id) || '');
  const label = String(o.label || profile);
  const seed = o.room ? entriesOfItem((o.room.agent_notes || {})[profile] || null) : [];
  let count = Number.isFinite(Number(o.count)) ? Number(o.count) : seed.length;
  const onChange = typeof o.onChange === 'function' ? o.onChange : null;
  const link = h('button', {
    class: 'notes-link' + (o.variant === 'btn' ? ' btn btn--secondary btn--sm' : ''),
    type: 'button',
    'data-profile': profile,
    title: 'Открыть блокнот особенностей: записи, поиск, журнал и откат',
  });
  const paint = () => {
    link.textContent = 'Особенности (' + count + ')';
    link.setAttribute('aria-label', 'Особенности для этой комнаты: записей ' + count);
  };
  paint();
  link.addEventListener('click', () => {
    openNotesEditor({
      ctx, roomId, profile, label,
      onChange(summary) {
        count = Number(summary && summary.count) || 0;
        paint();
        if (onChange) { try { onChange(summary); } catch { /* подписчик */ } }
      },
    });
  });
  return link;
}

export default { openNotesEditor, notesLink, editorTitle, EDITOR_SUBTITLE, filterEntries,
  limitsText, limitsState, metaLine, sourceRu, shortDate, previewText, DEFAULT_LIMITS };
