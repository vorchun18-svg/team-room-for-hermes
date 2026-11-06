/**
 * agent-notes-ui.js — «Особые указания для этой комнаты» у каждого участника.
 *
 * Требование Михаила 29.10.2026 (docs/WAVE9-RULES.md §9): для любого участника
 * комнаты (руководитель, критик, исполнитель) можно задать добавку к его роли,
 * которая действует ТОЛЬКО в этой комнате. Файлы профиля (SOUL.md) не меняются:
 * текст живёт в реестре комнаты и уходит агенту блоком в карточке и комментарием.
 *
 *   import { mountAgentNotes } from '../agent-notes-ui.js';
 *   const notes = await mountAgentNotes(host, ctx, {room, profiles, participants, onChange});
 *   notes.value();            // { профиль: текст } — только участники с текстом
 *   notes.refresh();          // состав изменился (галочки «Состав»)
 *   notes.counterFor(p);      // «120 из 8000» (для проверок и тестов)
 *
 * Второй экспорт — строка для вкладки «Команда»:
 *   notesSummaryLine(room) → «Особые указания: …» со свёрнутым списком («Показать»).
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { ensureCss, confirmDialog } from './ui.js';
import { countNoun } from './text-ru.js';
import { roleTitle } from './shell-labels.js';

export const MAX_TEXT = 8000;

/** Примеры-подсказки: с них Михаилу проще начать. */
export const EXAMPLES = [
  'Отвечай коротко и только цифрами',
  'Всегда проверяй по закону X',
  'Не обсуждай цены ниже Y',
];

const ROLE_RU = {
  orchestrator: 'руководитель комнаты',
  critic: 'критик',
  worker: 'исполнитель',
  observer: 'наблюдатель',
};

/** Указания комнаты в виде `{профиль: {text, updated_at}}` (мусор — мимо). */
export function notesOf(room) {
  const raw = room && room.agent_notes;
  const out = {};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  for (const [profile, value] of Object.entries(raw)) {
    if (!profile) continue;
    const text = typeof value === 'string' ? value
      : (value && typeof value === 'object' && typeof value.text === 'string' ? value.text : '');
    if (!String(text || '').trim() && !(value && Array.isArray(value.entries) && value.entries.length)) continue;
    out[String(profile)] = {
      text: String(text || ''),
      updated_at: Number((value && value.updated_at) || 0) || 0,
      // WAVE14 §13: записи и состояние переоценки идут вместе с текстом (склейкой).
      entries: (value && Array.isArray(value.entries)) ? value.entries : [],
      review: (value && value.review) || null,
      limits: (value && value.limits) || null,
    };
  }
  return out;
}

/** Состав комнаты по порядку: руководитель, критик, исполнители. */
export function participantsOf(room) {
  const out = [];
  const seen = new Set();
  const push = (profile, role) => {
    const name = String(profile || '').trim();
    if (!name || seen.has(name)) return;
    seen.add(name);
    out.push({ profile: name, role: role || 'worker', roleLabel: ROLE_RU[role] || 'участник' });
  };
  const lead = String((room && room.orchestrator) || '').trim();
  if (lead) push(lead, 'orchestrator');
  const critic = String((room && room.critic) || '').trim();
  if (critic) push(critic, 'critic');
  for (const member of (room && Array.isArray(room.members) ? room.members : [])) {
    if (!member) continue;
    push(member.profile, String(member.role || 'worker'));
  }
  return out;
}

function humanOf(profile, roleLabel) {
  return roleLabel ? profile + ' · ' + roleLabel : profile;
}

function shortLine(text, limit = 120) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : flat.slice(0, Math.max(0, limit - 1)).trimEnd() + '…';
}

/**
 * Поле указаний в диалоге комнаты.
 * @param {HTMLElement} container  куда монтировать (одна точка в room-dialog.js)
 * @param {object} ctx  {toast?}
 * @param {{room?: object, profiles?: Array, participants?: Function, onChange?: Function,
 *          values?: object}} [opts]
 *   • `participants()` — кто сейчас в составе (по умолчанию — participantsOf(room));
 *   • `values` — стартовые значения (по умолчанию room.agent_notes);
 *   • `onChange(profile, text, entries)` — на каждый ввод/изменение списка записей.
 */
export async function mountAgentNotes(container, ctx = {}, opts = {}) {
  await ensureCss('css/agent-notes.css');
  const room = opts.room || (ctx && ctx.room) || null;
  const profiles = Array.isArray(opts.profiles) ? opts.profiles : [];
  const participantsFn = typeof opts.participants === 'function'
    ? opts.participants : () => participantsOf(room);
  const onChange = typeof opts.onChange === 'function' ? opts.onChange : () => {};
  const values = new Map();
  const seed = opts.values && typeof opts.values === 'object' ? opts.values : notesOf(room);
  for (const [profile, item] of Object.entries(seed || {})) {
    const text = typeof item === 'string' ? item : (item && item.text) || '';
    if (String(text || '').trim()) values.set(String(profile), String(text));
  }

  let destroyed = false;
  const host = h('div', { class: 'agent-notes' });
  container.replaceChildren(host);

  const intro = h('div', { class: 'agent-notes__intro' },
    h('span', { class: 'agent-notes__intro-text' },
      'Указания действуют только в этой комнате: они дописываются к роли участника, '
      + 'и его собственные настройки не меняются.'),
  );
  const examples = h('div', { class: 'agent-notes__examples' },
    h('span', { class: 'agent-notes__examples-label' }, 'Например: '),
    ...EXAMPLES.map((text) => h('button', {
      class: 'agent-notes__example', type: 'button', title: 'Вставить пример',
      onclick: () => {
        // пример вставляется в первое раскрытое (или первое) поле
        const row = exampleTarget();
        if (!row) return;
        const current = String(row.area.value || '');
        row.area.value = current.trim() ? current.replace(/\s*$/, '\n' + text) : text;
        row.apply();
      },
    }, text)),
  );

  const listHost = h('div', { class: 'agent-notes__list' });
  const rows = new Map();          // профиль → {details, area, count, badge, paint, apply}
  host.appendChild(intro);
  host.appendChild(examples);
  host.appendChild(listHost);

  /** Поле, в которое вставляем пример: первое раскрытое, иначе первое. */
  function exampleTarget() {
    let fallback = null;
    for (const row of rows.values()) {
      if (row.details.hasAttribute && row.details.hasAttribute('open')) return row;
      if (!fallback) fallback = row;
    }
    return fallback;
  }

  function counterText(text) {
    const n = String(text || '').length;
    return n + ' из ' + MAX_TEXT;
  }

  function makeRow(person) {
    // Раскрыто по умолчанию: поле указаний — то, зачем сюда пришли («не найти,
    // где писать»). Раньше строка была свёрнута, а микрофон в ней всё равно
    // создавался: он оказывался в скрытом содержимом <details> (content-
    // visibility: hidden), его нельзя было ни увидеть, ни нажать, ни прокрутить
    // к нему диалог. Свёрнутая строка — только по желанию пользователя.
    const details = h('details', { class: 'agent-note', 'data-profile': person.profile, open: true });
    const badge = h('span', { class: 'agent-note__badge hidden' }, icon('edit', { size: 11 }), 'есть указания');
    const summary = h('summary', { class: 'agent-note__summary' },
      h('span', { class: 'agent-note__name', title: 'Профиль: ' + person.profile },
        person.profile),
      h('span', { class: 'agent-note__role' }, person.roleLabel || ROLE_RU[person.role] || ROLE_RU.worker),
      badge,
      h('span', { class: 'agent-note__toggle' }, 'Особые указания для этой комнаты'),
    );

    const area = h('textarea', {
      class: 'input agent-note__area', rows: '4', spellcheck: 'false',
      // микрофон ставится автоматически (dictation-auto.js) — data-mic НЕ отключаем
      placeholder: 'Что делать иначе именно в этой комнате? Например: «Отвечай коротко и только цифрами».',
      'aria-label': 'Особые указания для ' + person.profile,
    });
    area.value = values.get(person.profile) || '';
    const count = h('span', { class: 'agent-note__count tnum' }, counterText(area.value));
    const warn = h('span', { class: 'agent-note__warn hidden' },
      'Длиннее ' + MAX_TEXT + ' знаков не сохранится — лишнее обрежется.');

    const paint = () => {
      const length = area.value.length;
      count.textContent = counterText(area.value);
      count.classList.toggle('agent-note__count--over', length > MAX_TEXT);
      warn.classList.toggle('hidden', length <= MAX_TEXT);
      const has = Boolean(area.value.trim());
      badge.classList.toggle('hidden', !has);
      details.classList.toggle('agent-note--has', has);
    };

    area.addEventListener('input', () => {
      if (!destroyed) {
        values.set(person.profile, area.value);
        paint();
        onChange(person.profile, area.value);
      }
    });
    area.addEventListener('blur', () => { if (!destroyed) paint(); });
    paint();

    const apply = () => {
      if (destroyed) return;
      values.set(person.profile, area.value);
      paint();
      onChange(person.profile, area.value);
    };

    const box = h('div', { class: 'agent-note__box' },
      area,
      h('div', { class: 'agent-note__foot' }, count, warn));
    details.appendChild(summary);
    details.appendChild(box);
    rows.set(person.profile, { details, area, count, badge, paint, apply });
    return details;
  }

  function refresh() {
    if (destroyed) return;
    const people = (participantsFn() || []).filter((p) => p && p.profile);
    rows.clear();
    const nodes = people.map(makeRow);
    listHost.replaceChildren(...nodes);
    if (!nodes.length) {
      listHost.appendChild(h('div', { class: 'agent-notes__empty' },
        'Отметьте участников в блоке «Состав» — у каждого появится поле для указаний.'));
    }
  }

  refresh();

  return {
    element: host,
    refresh,
    /** {профиль: текст} — только участники, у которых что-то написано. */
    value() {
      const out = {};
      const people = new Set((participantsFn() || []).map((p) => p && p.profile).filter(Boolean));
      for (const [profile, text] of values) {
        if (!people.has(profile)) continue;
        const clean = String(text || '').slice(0, MAX_TEXT).trim();
        if (clean) out[profile] = clean;
      }
      return out;
    },
    /** Полный набор (включая ещё не выбранных) — для сравнения до/после. */
    allValues() {
      const out = {};
      for (const [profile, text] of values) {
        const clean = String(text || '').trim();
        if (clean) out[profile] = clean;
      }
      return out;
    },
    textFor(profile) { return values.get(String(profile || '')) || ''; },
    has(profile) { return Boolean(String(values.get(String(profile || '')) || '').trim()); },
    counterFor(profile) {
      const row = rows.get(String(profile || ''));
      return row ? row.count.textContent : '';
    },
    /** Программно задать текст (тесты и «вставить пример»). */
    setText(profile, text) {
      const row = rows.get(String(profile || ''));
      if (!row) return false;
      row.area.value = String(text == null ? '' : text);
      row.apply();
      return true;
    },
    count() { return Object.keys(this.value()).length; },
    /** Сколько участников с указаниями (для строки-индикатора). */
    countNounText() {
      return countNoun(Object.keys(this.value()).length,
        ['участника', 'участников', 'участников']);
    },
    destroy() { destroyed = true; host.remove(); },
  };
}

/**
 * Строка «Особые указания: …» для вкладки «Команда» (свёрнуто, «Показать»).
 * @param {object} room
 * @param {{limit?: number}} [opts]
 * @returns {HTMLElement}
 */
export function notesSummaryLine(room, opts = {}) {
  const notes = notesOf(room);
  const people = participantsOf(room);
  const items = people.filter((p) => notes[p.profile]);
  const limit = Number(opts.limit) > 0 ? Number(opts.limit) : 120;

  const label = h('span', { class: 'tm-fact__label' }, 'Особые указания');
  if (!items.length) {
    return h('div', { class: 'tm-fact agent-notes-line' }, label,
      h('span', { class: 'tm-fact__value muted' }, 'не заданы'));
  }

  const howMany = countNoun(items.length, ['участника', 'участников', 'участников']);
  const list = h('div', { class: 'agent-notes-line__list hidden' },
    ...items.map((p) => h('div', { class: 'agent-notes-line__item' },
      h('span', { class: 'agent-notes-line__who', title: 'Профиль: ' + p.profile },
        p.profile + ' · ' + (p.roleLabel || roleTitle(p.role) || ROLE_RU.worker)),
      h('span', { class: 'agent-notes-line__text' }, shortLine(notes[p.profile].text, limit)))));
  const toggle = h('button', {
    class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': 'false',
    onclick: () => {
      const open = list.classList.toggle('hidden') === false;
      toggle.textContent = open ? 'Скрыть' : 'Показать';
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    },
  }, 'Показать');

  return h('div', { class: 'tm-fact agent-notes-line' }, label,
    h('span', { class: 'tm-fact__value' },
      h('span', { class: 'agent-notes-line__count' }, 'есть у ' + howMany),
      toggle),
    list);
}

/* ────────────────────────────────── записи (WAVE14 §13): список, а не сплошной текст */

export const SOURCE_RU = { user: 'Михаил', leader: 'руководитель комнаты', auto: 'автоматически' };
export const ACTION_RU = {
  add: 'добавлено', update: 'изменено', remove: 'удалено', merge: 'объединено',
  review: 'переоценка', replace: 'заменено', rollback: 'откат',
  pinned: 'закреплено', unpin: 'снято закрепление',
  // WAVE19 B-27/B-35: служебные события журнала комнаты
  artifact_outside: 'файл вне папки проекта',
  leader_review: 'оценка работы руководителем',
  leader_reminder: 'напоминание руководителю',
};

/** События, которые реально меняли записи участника: только их можно откатить. */
export const ROLLBACKABLE = new Set(['add', 'update', 'remove', 'merge', 'review',
  'replace', 'pinned', 'unpin']);

/** Записи из значения реестра: новые `entries` или старый одиночный `text`. */
export function entriesOfItem(item) {
  if (!item) return [];
  const out = [];
  const raw = Array.isArray(item.entries) ? item.entries : null;
  if (raw) {
    for (const one of raw) {
      if (!one) continue;
      const text = typeof one === 'string' ? one : String(one.text || '');
      if (!text.trim()) continue;
      out.push({
        id: String((one && one.id) || ''),
        text,
        source: String((one && one.source) || 'user'),
        origin: String((one && one.origin) || ''),
        created_at: Number((one && one.created_at) || 0) || 0,
        used_count: Number((one && one.used_count) || 0) || 0,
        pinned: Boolean(one && one.pinned),
      });
    }
    return out;
  }
  const text = typeof item === 'string' ? item : String(item.text || '');
  if (!text.trim()) return [];
  return [{ id: '', text, source: 'user', origin: '', created_at: Number(item.updated_at || 0) || 0,
    used_count: 0, pinned: true }];
}

/** Дата записи по-русски: «05.11». */
function shortDate(ts) {
  const n = Number(ts) || 0;
  if (n <= 0) return '';
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const day = String(d.getDate()).padStart(2, '0');
  const month = String(d.getMonth() + 1).padStart(2, '0');
  return day + '.' + month;
}

/** Подпись записи: кто и когда, зачем, источник, сколько раз пригодилась. */
export function entryMetaText(entry) {
  const parts = [];
  const e = entry || {};
  parts.push(e.pinned ? 'закреплено' : (SOURCE_RU[e.source] || 'руководитель комнаты'));
  if (e.origin) parts.push('источник: ' + e.origin);
  const date = shortDate(e.created_at);
  if (date) parts.push(date);
  if (Number(e.used_count) > 0) parts.push('пригодилось ' + e.used_count + ' раз');
  return parts.join(' · ');
}

function encPath(value) {
  return encodeURIComponent(String(value || ''));
}

/**
 * Редактор записей участника: список + поле «Добавить» с диктовкой, «Сократить», журнал.
 *
 * @param {HTMLElement} host
 * @param {object} ctx  {api, toast}
 * @param {{room?: object, roomId?: string, profile: string, entries?: Array, limits?: object,
 *          live?: boolean, compact?: boolean, onChange?: Function}} opts
 */
export async function mountNotesEntries(host, ctx = {}, opts = {}) {
  await ensureCss('css/agent-notes.css');
  const api = opts.api || (ctx && ctx.api) || null;
  const roomId = String(opts.roomId || (ctx && ctx.room && ctx.room.id) || '');
  const profile = String(opts.profile || '');
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const live = Boolean(opts.live && api && roomId && profile);
  const onChange = typeof opts.onChange === 'function' ? opts.onChange : () => {};
  const limits = Object.assign({ max_entries: 24, max_text: MAX_TEXT, review_every: 5,
    entry_hint: 1000 }, opts.limits || {});
  let entries = entriesOfItem({ entries: opts.entries || [] });
  let busy = false;
  let destroyed = false;

  const list = h('div', { class: 'notes-entries__list' });
  const area = h('textarea', {
    class: 'input agent-note__area notes-entries__area', rows: opts.compact ? '2' : '3',
    spellcheck: 'false',
    placeholder: 'Что делать иначе именно в этой комнате? Например: «Ornith слабая модель, '
      + 'но локальная — простые задачи поручать ей».',
    'aria-label': 'Новая особенность для ' + profile,
  });
  const addBtn = h('button', { class: 'btn btn--primary btn--sm', type: 'button' },
    icon('plus', { size: 12 }), 'Добавить');
  const countEl = h('span', { class: 'notes-entries__count tnum' }, '');
  const statusEl = h('span', { class: 'notes-entries__status small muted' }, '');
  const reviewBtn = h('button', {
    class: 'btn btn--ghost btn--sm', type: 'button',
    title: 'Слить дубли, убрать устаревшее и не использованное',
  }, 'Сократить');
  const journalBtn = h('button', {
    class: 'btn btn--ghost btn--sm', type: 'button', 'aria-expanded': 'false',
  }, 'Журнал');
  const journalBox = h('div', { class: 'notes-entries__journal hidden' });
  const warn = h('span', { class: 'agent-note__warn hidden' },
    'Длиннее ' + MAX_TEXT + ' знаков не сохранится — лишнее обрежется.');

  const intro = h('p', { class: 'notes-entries__intro small muted' },
    'Действует только в этой комнате: добавка к роли участника (SOUL.md), '
    + 'его собственные настройки не меняются.');

  // Единый стиль поля (как в композере): поле ввода на всю ширину, под ним
  // отдельные кнопки — микрофон и «Добавить». Микрофон ставит dictation-auto.js
  // правилом «рядом кнопка-действие → отдельной кнопкой ПЕРЕД ней»: он в потоке
  // строки, поэтому не может наплыть на поле или на «Добавить» и не обрезается.
  const addBox = h('div', { class: 'notes-entries__add' },
    h('div', { class: 'notes-entries__addrow' }, area, addBtn),
    h('div', { class: 'notes-entries__addfoot' }, countEl, statusEl));
  const tools = h('div', { class: 'notes-entries__tools' }, reviewBtn, journalBtn);

  const root = h('div', { class: 'notes-entries' + (opts.compact ? ' notes-entries--compact' : '') },
    opts.compact ? null : intro, list, addBox, live ? tools : null, journalBox, warn);
  host.replaceChildren(root);

  const totalChars = () => entries.reduce((sum, one) => sum + String(one.text || '').length, 0);

  function paint() {
    if (destroyed) return;
    list.replaceChildren();
    if (!entries.length) {
      list.appendChild(h('div', { class: 'notes-entries__empty small muted' },
        'Особенностей пока нет — команда работает по своей роли.'));
    }
    for (const entry of entries) {
      const delBtn = live ? h('button', {
        class: 'notes-entries__del', type: 'button',
        title: entry.pinned ? 'Убрать запись (закреплена — добавлена вами)' : 'Убрать запись',
        onclick: () => { removeEntry(entry).catch(() => {}); },
      }, icon('trash', { size: 12 })) : null;
      list.appendChild(h('div', {
        class: 'notes-entry' + (entry.pinned ? ' notes-entry--pinned' : ''),
      }, h('div', { class: 'notes-entry__text' }, entry.text),
        h('div', { class: 'notes-entry__meta small muted' }, entryMetaText(entry)),
        delBtn));
    }
    const n = entries.length;
    countEl.textContent = 'записей ' + n + ' из ' + limits.max_entries
      + ' · ' + totalChars() + ' из ' + limits.max_text + ' знаков';
    countEl.classList.toggle('agent-note__count--over',
      n > limits.max_entries || totalChars() > limits.max_text);
    warn.classList.toggle('hidden', totalChars() <= limits.max_text);
    addBtn.disabled = busy;
    reviewBtn.disabled = busy || !live;
    journalBtn.disabled = busy || !live;
    area.placeholder = n >= limits.max_entries
      ? 'Лимит записей: добавьте — и лишнее уберётся на переоценке.'
      : area.placeholder;
  }

  function setEntries(next) {
    entries = entriesOfItem({ entries: next || [] });
    paint();
  }

  async function reload() {
    if (!live) return;
    try {
      const data = await api.get('/api/rooms/' + encPath(roomId) + '/agents/' + encPath(profile) + '/notes');
      setEntries((data && data.entries) || []);
    } catch (err) {
      statusEl.textContent = 'не удалось прочитать записи';
    }
  }

  async function commit() {
    const text = String(area.value || '').trim();
    if (!text) { statusEl.textContent = 'Напишите или надиктуйте особенность'; return; }
    if (!live) {
      const entry = { id: '', text, source: 'user', origin: '', created_at: Math.floor(Date.now() / 1000),
        used_count: 0, pinned: true };
      entries = entries.concat([entry]);
      area.value = '';
      paint();
      onChange(profile, entries.map((one) => one.text).join('\n'));
      return;
    }
    busy = true; paint();
    try {
      const data = await api.post('/api/rooms/' + encPath(roomId) + '/agents/' + encPath(profile) + '/notes/entries',
        { text, source: 'user', origin: 'правка в интерфейсе' });
      area.value = '';
      statusEl.textContent = data && data.duplicate ? 'Такая запись уже есть' : 'Записано';
      setEntries((data && data.entries) || []);
      onChange(profile, String((data && data.text) || ''), entries.slice());
      toast(data && data.duplicate ? 'Такая особенность уже записана' : 'Особенность записана', 'ok');
      if (data && data.review && data.review.applied) {
        toast('Переоценка: записей ' + data.review.before + ' → ' + data.review.after
          + ' (журнал комнаты)', 'ok');
      }
    } catch (err) {
      statusEl.textContent = (err && err.message) || 'не сохранено';
      toast((err && err.message) || 'Не удалось записать', 'error');
    } finally {
      busy = false; paint();
    }
  }

  async function removeEntry(entry) {
    if (!live) {
      entries = entries.filter((one) => one !== entry);
      paint();
      onChange(profile, entries.map((one) => one.text).join('\n'));
      return;
    }
    busy = true; paint();
    try {
      const data = await api.del('/api/rooms/' + encPath(roomId) + '/agents/' + encPath(profile)
        + '/notes/entries/' + encPath(entry.id) + '?force=1', { why: 'убрано в интерфейсе' });
      setEntries((data && data.entries) || []);
      onChange(profile, String((data && data.text) || ''), entries.slice());
      toast('Запись убрана — откат есть в журнале', 'ok');
    } catch (err) {
      toast((err && err.message) || 'Не удалось убрать запись', 'error');
    } finally {
      busy = false; paint();
    }
  }

  async function review() {
    if (!live) return;
    busy = true; paint();
    try {
      const preview = await api.post('/api/rooms/' + encPath(roomId) + '/agents/' + encPath(profile)
        + '/notes/review', { apply: false });
      const drops = (preview && preview.drops) || [];
      const merges = (preview && preview.merges) || [];
      if (!drops.length && !merges.length) {
        statusEl.textContent = 'Сокращать нечего';
        toast('Сокращать нечего: дублей и устаревшего нет', 'ok');
        return;
      }
      const list_ = drops.map((d) => '• ' + String(d.text || '').slice(0, 80)).join('\n');
      const ok = await confirmDialog({
        title: 'Сократить записи',
        text: 'Уберём ' + drops.length + ' ' + countNoun(drops.length, ['запись', 'записи', 'записей'])
          + (merges.length ? ', сольём дублей: ' + merges.length : '')
          + '. Закреплённые записи не трогаем.\n\n' + list_,
        confirmLabel: 'Сократить',
      });
      if (!ok) return;
      const data = await api.post('/api/rooms/' + encPath(roomId) + '/agents/' + encPath(profile)
        + '/notes/review', { apply: true, by: 'user', why: 'сокращение в интерфейсе' });
      setEntries((data && data.entries) || []);
      onChange(profile, String((data && data.text) || ''), entries.slice());
      toast('Записей ' + data.before + ' → ' + data.after + '. Откат — в журнале', 'ok');
    } catch (err) {
      toast((err && err.message) || 'Не удалось сократить', 'error');
    } finally {
      busy = false; paint();
    }
  }

  async function loadJournal() {
    journalBox.replaceChildren(h('div', { class: 'small muted' }, 'Загружаю…'));
    try {
      const data = await api.get('/api/rooms/' + encPath(roomId) + '/notes/journal?limit=30');
      const events = ((data && data.events) || []).filter((e) => !e.profile || e.profile === profile);
      journalBox.replaceChildren();
      if (!events.length) {
        journalBox.appendChild(h('div', { class: 'small muted' }, 'Изменений пока не было.'));
        return;
      }
      for (const event of events) {
        const when = shortDate(event.at) || '—';
        journalBox.appendChild(h('div', { class: 'notes-journal__row' },
          h('div', { class: 'notes-journal__head small' },
            h('span', { class: 'notes-journal__who' }, (SOURCE_RU[event.source] || event.actor || 'руководитель')),
            h('span', { class: 'notes-journal__act' }, ACTION_RU[event.action] || event.action || ''),
            h('span', { class: 'notes-journal__when muted' }, when)),
          h('div', { class: 'notes-journal__text small' }, String(event.text || '')),
          event.why ? h('div', { class: 'notes-journal__why small muted' }, 'зачем: ' + event.why) : null,
          event.rolled_back ? h('div', { class: 'small muted' }, 'изменение откачено')
            : (ROLLBACKABLE.has(event.action) ? h('button', {
              class: 'btn btn--ghost btn--sm', type: 'button',
              onclick: () => { rollback(event).catch(() => {}); },
            }, 'Откатить') : null)));
      }
    } catch (err) {
      journalBox.replaceChildren(h('div', { class: 'small muted' }, 'Журнал не загрузился'));
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
      const data = await api.post('/api/rooms/' + encPath(roomId) + '/notes/journal/'
        + encPath(event.id) + '/rollback', { why: 'откат в интерфейсе' });
      setEntries((data && data.entries) || []);
      onChange(profile, String((data && data.text) || ''), entries.slice());
      toast('Изменение откачено', 'ok');
      await loadJournal();
    } catch (err) {
      toast((err && err.message) || 'Не удалось откатить', 'error');
    }
  }

  addBtn.addEventListener('click', () => { commit().catch(() => {}); });
  area.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      commit().catch(() => {});
    }
  });
  reviewBtn.addEventListener('click', () => { review().catch(() => {}); });
  journalBtn.addEventListener('click', () => {
    const hidden = journalBox.classList.toggle('hidden');
    journalBtn.textContent = hidden ? 'Журнал' : 'Скрыть журнал';
    journalBtn.setAttribute('aria-expanded', hidden ? 'false' : 'true');
    if (!hidden) loadJournal().catch(() => {});
  });
  paint();

  return {
    element: root,
    /** Обновить данные снаружи (карточка в «Команде» после перезагрузки комнаты). */
    refresh(next) { if (next) setEntries(next); else reload().catch(() => {}); },
    entries() { return entries.slice(); },
    /** Склейка записей — старый контракт `{профиль: текст}`. */
    value() { return entries.map((one) => one.text).join('\n'); },
    add(text) { area.value = String(text || ''); return commit(); },
    count() { return entries.length; },
    destroy() { destroyed = true; root.remove(); },
  };
}

/**
 * Указания нескольких участников (окно «Изменить» комнаты): у каждого — свой редактор.
 *
 * @param {HTMLElement} host
 * @param {{room?: object, roomId?: string, live?: boolean, api?: object}} [opts]
 * @param {Function} [participants]  () => [{profile, roleLabel}]
 */
export async function mountNotesLive(host, ctx = {}, opts = {}, participants = null) {
  await ensureCss('css/agent-notes.css');
  const api = opts.api || (ctx && ctx.api) || null;
  const roomId = String(opts.roomId || (opts.room && opts.room.id) || '');
  const live = Boolean(opts.live && api && roomId);
  const room = opts.room || (ctx && ctx.room) || {};
  const people = typeof participants === 'function' ? participants : () => participantsOf(room);
  const editors = new Map();
  let destroyed = false;
  const hostBox = h('div', { class: 'agent-notes agent-notes--entries' });
  host.replaceChildren(hostBox);

  function paint() {
    if (destroyed) return;
    hostBox.replaceChildren();
    const rows = (people() || []).filter((one) => one && one.profile);
    if (!rows.length) {
      hostBox.appendChild(h('div', { class: 'agent-notes__empty' },
        'Отметьте участников в блоке «Состав» — у каждого появится список особенностей.'));
      return;
    }
    const notes = notesOf(room);
    for (const person of rows) {
      const item = notes[person.profile] || null;
      const editorHost = h('div', { class: 'agent-note__entries' });
      const details = h('details', { class: 'agent-note' + (item ? ' agent-note--has' : '') },
        h('summary', { class: 'agent-note__summary' },
          h('span', { class: 'agent-note__name' }, person.profile),
          h('span', { class: 'agent-note__role' }, person.roleLabel || ROLE_RU[person.role] || 'участник'),
          h('span', { class: 'agent-note__toggle' }, 'Особенности для этой комнаты')),
        editorHost);
      hostBox.appendChild(details);
      const entries = entriesOfItem(item);
      mountNotesEntries(editorHost, ctx, {
        room, roomId, profile: person.profile, entries, live,
        limits: (item && item.limits) || null,
      }).then((ui) => {
        editors.set(person.profile, ui);
        if (entries.length && !destroyed) details.setAttribute('open', '');
      }).catch(() => {});
    }
  }

  paint();

  return {
    element: hostBox,
    refresh() { paint(); },
    /** `{профиль: склейка записей}` — совместимо со старым контрактом сохранения комнаты. */
    value() {
      const out = {};
      for (const [profile, ui] of editors) {
        const text = String(ui.value() || '').trim();
        if (text) out[profile] = text;
      }
      return out;
    },
    async reload() {
      for (const ui of editors.values()) ui.refresh();
    },
    destroy() {
      destroyed = true;
      for (const ui of editors.values()) ui.destroy();
      hostBox.remove();
    },
  };
}

export default { mountAgentNotes, mountNotesEntries, mountNotesLive, notesSummaryLine, notesOf,
  participantsOf, entriesOfItem, entryMetaText, EXAMPLES, MAX_TEXT };
