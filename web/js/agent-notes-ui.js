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
import { ensureCss } from './ui.js';
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
    if (!String(text || '').trim()) continue;
    out[String(profile)] = {
      text: String(text),
      updated_at: Number((value && value.updated_at) || 0) || 0,
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
 *   • `onChange(profile, text)` — вызывается на каждый ввод.
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
    const details = h('details', { class: 'agent-note', 'data-profile': person.profile });
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

export default { mountAgentNotes, notesSummaryLine, notesOf, participantsOf, EXAMPLES, MAX_TEXT };
