/**
 * agent-model-ui.js — «Модель, провайдер и мышление» у каждого участника комнаты.
 *
 * Требование Михаила 03.11.2026 (docs/WAVE10-RULES.md): для любого участника
 * (руководитель, критик, исполнитель) можно задать модель, провайдера, включить
 * или выключить размышление и выбрать усилия. Замена действует ТОЛЬКО в этой
 * комнате; собственные настройки агента не меняются.
 *
 *   import { mountAgentModel } from '../agent-model-ui.js';
 *   const ui = await mountAgentModel(host, ctx, {
 *     room, catalog, participants: () => [{profile, role}], onChange,
 *   });
 *   ui.value();          // {профиль: {provider, model, reasoning}} — только изменённые
 *
 * Один участник на вкладке «Команда» или в отдельном диалоге:
 *   const ui = await mountAgentModel(host, ctx, {room, profile, member, mode: 'live'});
 *   await ui.save(profile);   // PUT /api/rooms/{id}/agents/{profile}/model
 *
 * Строка для карточки на вкладке «Команда»: agentModelLine(room, profile, member, opts).
 * Диалог «тот же редактор»: openAgentModelEditor(ctx, {room, profile, member}).
 *
 * Правила: только h() (без innerHTML), только токены tokens.css, поиск моделей —
 * поле-список: справа стрелка, микрофона нет (data-mic="off", см. isCombobox),
 * список моделей — блок внутри диалога, без всплывающих панелей: на узком экране
 * он не вылезает за край и открыт сразу, при любом провайдере.
 * Технические id профилей, провайдеров и моделей — только в title.
 * О стоимости моделей не предупреждаем: этого не просили.
 */

import { h, uid } from './util.js';
import { icon } from './icons.js';
import { ensureCss, iconButton } from './ui.js';
import {
  EFFORT_RU, EFFORT_ORDER, THINKING_OFF, normalizeReasoning, reasoningLabel, effortOptions,
  providersOf, providerLabelOf, modelLabelOf, defaultsText, settingsLabel, memberOf,
  modelSettingsOf, agentDefaultOf, filterModels, shortModelText,
} from './agent-model-model.js';
import { buildRow } from './agent-model-row.js';

// Наружу — те же имена, что и раньше: остальные файлы импортируют отсюда.
export {
  EFFORT_RU, EFFORT_ORDER, THINKING_OFF, normalizeReasoning, reasoningLabel, effortOptions,
  providersOf, providerLabelOf, modelLabelOf, defaultsText, settingsLabel, memberOf,
  modelSettingsOf, agentDefaultOf, filterModels,
};

const ROLE_RU = {
  orchestrator: 'руководитель комнаты',
  critic: 'критик',
  observer: 'наблюдатель',
  worker: 'исполнитель',
};

const enc = (s) => encodeURIComponent(String(s === null || s === undefined ? '' : s));

/* Мини-DOM в тестах не знает querySelector: обходим дерево сами. */
function childEls(node) {
  return (node && node.childNodes ? Array.from(node.childNodes) : [])
    .filter((n) => n && n.nodeType === 1);
}

function hasClass(node, name) {
  return String((node && node.className) || '').split(/\s+/).includes(name);
}

/** Снять узел с родителя: браузер, мини-DOM в тестах или список детей. */
function detach(node) {
  if (!node) return;
  if (typeof node.remove === 'function') { node.remove(); return; }
  const parent = node.parentNode;
  if (!parent) return;
  if (typeof parent.removeChild === 'function') { parent.removeChild(node); return; }
  const kids = parent.childNodes;
  if (Array.isArray(kids)) {
    const i = kids.indexOf(node);
    if (i !== -1) kids.splice(i, 1);
  }
  node.parentNode = null;
}

/* ─────────────────────────────────────────────────────── монтирование */

async function fetchCatalog(ctx) {
  const api = ctx && ctx.api;
  if (!api || typeof api.get !== 'function') return { catalog: null, error: '' };
  try {
    const data = await api.get('/api/models');
    return { catalog: data && typeof data === 'object' ? data : null, error: '' };
  } catch (err) {
    return { catalog: null, error: 'Список моделей недоступен — показываем настройки агента. Проверьте, что приложение запущено.' };
  }
}

/**
 * Редактор модели для одного или нескольких участников.
 *
 * @param {HTMLElement} container куда монтировать (одна точка в диалоге/вкладке)
 * @param {object} ctx {api, toast?}
 * @param {{room?: object, profile?: string, member?: object, catalog?: object,
 *          participants?: Function|Array, mode?: 'draft'|'live',
 *          onChange?: Function, onSaved?: Function, onRestart?: Function}} [opts]
 *   • `mode: 'draft'` — значения уходят в payload комнаты (диалог создания/правки);
 *   • `mode: 'live'` — кнопка «Применить» сразу сохраняет (вкладка «Команда»).
 * @returns {Promise<object>} handle
 */
export async function mountAgentModel(container, ctx = {}, opts = {}) {
  await ensureCss('css/agent-model.css');
  const o = opts || {};
  const room = o.room || (ctx && ctx.room) || null;
  const mode = o.mode === 'live' ? 'live' : 'draft';
  let catalog = o.catalog && typeof o.catalog === 'object' ? o.catalog : null;
  let catalogError = '';
  if (!catalog) {
    const got = await fetchCatalog(ctx);
    catalog = got.catalog;
    catalogError = got.error;
  }
  const onChange = typeof o.onChange === 'function' ? o.onChange : null;

  const host = h('div', { class: 'agent-model agent-model--' + mode });
  container.replaceChildren(host);

  const intro = h('div', { class: 'agent-model__intro' },
    'Модель и мышление действуют только в этой комнате: собственные настройки участника не меняются.');
  host.appendChild(intro);
  if (catalogError) {
    // Не role="alert": это подсказка о недоступном списке, а не ошибка действия.
    host.appendChild(h('div', { class: 'banner banner--warn agent-model__catalog-error' },
      icon('warning', { size: 13 }), h('div', { class: 'banner__text' }, catalogError)));
  }

  const listHost = h('div', { class: 'agent-model__list' });
  host.appendChild(listHost);

  const overrides = new Map();     // профиль → {provider, model, reasoning}
  const rows = new Map();          // профиль → строка
  let destroyed = false;

  function remember(profile, value) {
    if (value) overrides.set(profile, Object.assign({}, value));
    else overrides.delete(profile);
  }

  function participants() {
    if (typeof o.participants === 'function') return o.participants() || [];
    if (Array.isArray(o.participants)) return o.participants;
    if (o.profile) {
      return [{
        profile: String(o.profile),
        role: String((o.member && o.member.role) || 'worker'),
        title: (o.member && (o.member.title || o.member.name)) || '',
      }];
    }
    const out = [];
    const raw = room || {};
    const push = (profile, role) => {
      const name = String(profile || '').trim();
      if (!name || out.some((p) => p.profile === name)) return;
      out.push({ profile: name, role: role || 'worker' });
    };
    push(raw.orchestrator, 'orchestrator');
    push(raw.critic, 'critic');
    for (const m of Array.isArray(raw.members) ? raw.members : []) {
      if (m && typeof m === 'object') push(m.profile, m.role);
      else push(m, 'worker');
    }
    return out;
  }

  function build() {
    if (destroyed) return;
    const people = participants().filter((p) => p && p.profile);
    const nodes = [];
    rows.clear();
    for (const person of people) {
      const profile = String(person.profile);
      const member = (o.member && String(o.profile || '') === profile) ? o.member : memberOf(room, profile);
      const row = buildRow(person, {
        catalog,
        mode,
        room,
        ctx,
        onChange,
        defaultsOf: (p) => agentDefaultOf(room, p, p === profile ? member : null),
        initialOf: (p) => {
          const ov = overrides.get(p);
          if (ov) return Object.assign({}, ov, { source: 'room' });
          return modelSettingsOf(room, p, p === profile ? member : null);
        },
        applyOverride: remember,
        remember,
      });
      row.element.setAttribute('data-role', String(person.role || 'worker'));
      rows.set(profile, row);
      nodes.push(row.element);
    }
    listHost.replaceChildren(...nodes);
    if (!nodes.length) {
      listHost.appendChild(h('div', { class: 'agent-model__empty' },
        'Отметьте участников в блоке «Состав» — у каждого появятся модель и мышление.'));
    }
  }

  build();

  return {
    element: host,
    mode,
    catalog,
    refresh: build,
    rowFor(profile) { return rows.get(String(profile || '')) || null; },
    /** {профиль: {provider, model, reasoning}} — только изменённое против агента. */
    value() {
      const out = {};
      const people = new Set((participants() || []).map((p) => p && p.profile).filter(Boolean));
      for (const [profile, row] of rows) {
        if (!people.has(profile)) continue;
        const st = row.state();
        if (!st.changed) continue;
        out[profile] = { provider: st.provider, model: st.model, reasoning: st.reasoning };
      }
      return out;
    },
    allValues() {
      const out = {};
      for (const [profile, row] of rows) {
        const st = row.state();
        if (!st.changed) continue;
        out[profile] = { provider: st.provider, model: st.model, reasoning: st.reasoning };
      }
      return out;
    },
    save(profile) {
      const row = rows.get(String(profile || o.profile || ''));
      return row ? row.save() : Promise.resolve(null);
    },
    destroy() {
      destroyed = true;
      rows.clear();
      host.remove();
    },
  };
}

/**
 * Строка «модель: …» + «Изменить» для карточки участника на вкладке «Команда».
 * @param {object} room
 * @param {string} profile
 * @param {object} [member] участник из members[] (с model_settings/agent_default)
 * @param {{catalog?: object, onEdit?: Function, live?: boolean}} [opts]
 * @returns {HTMLElement}
 */
export function agentModelLine(room, profile, member, opts = {}) {
  const o = opts || {};
  const catalog = o.catalog || null;
  const settings = modelSettingsOf(room, profile, member);
  const isRoom = String(settings.source || '') === 'room';
  const ids = [settings.provider, settings.model].filter(Boolean).join(' · ');
  const title = settingsLabel(catalog, settings)
    + (isRoom ? ' · изменено для этой комнаты' : ' · по умолчанию')
    + (ids ? ' · ' + ids : '');
  const chip = h('span', {
    class: 'chip chip--outline agent-model__line-chip' + (isRoom ? ' agent-model__line-chip--room' : ''),
    title: title + ' (профиль: ' + String(profile || '') + ')',
  }, 'модель: ' + shortModelText(catalog, settings));

  const btn = h('button', {
    class: 'btn btn--ghost btn--sm agent-model__line-btn', type: 'button',
    title: 'Модель, провайдер и мышление участника',
    onclick: () => { if (typeof o.onEdit === 'function') o.onEdit(profile); },
  }, icon('edit', { size: 11 }), h('span', null, 'Изменить'));

  return h('div', { class: 'agent-model__line', dataset: { profile: String(profile || '') } }, chip, btn);
}

/**
 * Диалог «тот же редактор» для вкладки «Команда».
 * @param {object} ctx {api, toast?}
 * @param {{room: object, profile: string, member?: object, catalog?: object,
 *          onSaved?: Function}} opts
 * @returns {Promise<boolean>} true, если что-то сохранили
 */
export function openAgentModelEditor(ctx, opts = {}) {
  const o = opts || {};
  const d = globalThis.document;
  if (!d || !d.body) return Promise.resolve(false);
  const room = o.room || (ctx && ctx.room) || null;
  const profile = String(o.profile || '');
  const name = String((o.member && (o.member.title || o.member.name)) || profile);

  return new Promise((resolve) => {
    let done = false;
    let changedAny = false;
    const titleId = uid('am-dlg');

    const host = h('div', { class: 'agent-model__host' });
    const overlay = h('div', { class: 'dialog-backdrop' });

    const finish = (value) => {
      if (done) return;
      done = true;
      detach(overlay);
      if (typeof d.removeEventListener === 'function') d.removeEventListener('keydown', onKey, true);
      resolve(value === undefined ? changedAny : Boolean(value));
    };
    const onKey = (ev) => {
      if (ev && ev.key === 'Escape') {
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
        finish(changedAny);
      }
    };

    const dialog = h('div', {
      class: 'dialog dialog--wide agent-model__dialog',
      role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
    },
    h('div', { class: 'dialog__head' },
      h('h2', { class: 'dialog__title', id: titleId }, 'Модель участника «' + name + '»'),
      iconButton({ icon: 'close', size: 'sm', title: 'Закрыть', onclick: () => finish(changedAny) })),
    h('div', { class: 'dialog__body agent-model__dialog-body' }, host),
    h('div', { class: 'dialog__foot' },
      h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => finish(changedAny) }, 'Закрыть')));

    overlay.appendChild(dialog);
    overlay.addEventListener('mousedown', (ev) => { if (ev && ev.target === overlay) finish(changedAny); });
    if (typeof d.addEventListener === 'function') d.addEventListener('keydown', onKey, true);
    (d.body || d.documentElement).appendChild(overlay);

    mountAgentModel(host, ctx, {
      room,
      profile,
      member: o.member || null,
      catalog: o.catalog || null,
      mode: 'live',
      onSaved: () => { changedAny = true; if (typeof o.onSaved === 'function') o.onSaved(profile); },
    }).then((ui) => {
      if (done) { ui.destroy(); return; }
      const row = ui.rowFor(profile);
      if (row && typeof row.open === 'function') row.open();
    }).catch(() => {
      host.replaceChildren(h('div', { class: 'banner banner--error', role: 'alert' },
        icon('error', { size: 13 }),
        h('div', { class: 'banner__text' }, 'Не удалось открыть настройки модели. Обновите страницу и попробуйте снова.')));
    });
  });
}

export default {
  mountAgentModel,
  openAgentModelEditor,
  agentModelLine,
  modelSettingsOf,
  agentDefaultOf,
  filterModels,
  effortOptions,
  providersOf,
  reasoningLabel,
  normalizeReasoning,
  defaultsText,
  settingsLabel,
  modelLabelOf,
  providerLabelOf,
  memberOf,
  EFFORT_RU,
  THINKING_OFF,
};
