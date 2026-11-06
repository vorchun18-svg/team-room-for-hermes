/**
 * team.js — комната, вкладка «Команда».
 *
 * Сетка карточек: аватар, имя профиля, роль в комнате (правится инлайн), описание
 * профиля, модель, живой статус, текущая задача, последняя работа, бейджи.
 * Ниже — «вне состава» (если API отдаёт работающих не из комнаты) и блок
 * «Как работает комната» с руководителем комнаты, критиком, раундами критика и файлами контекста.
 * Данные — только ctx.api; опрос 5 с перерисовывает лишь изменившиеся карточки.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { poll as pollApi } from '../api.js';
import {
  ensureCss, avatar, emptyState, skeleton, errorBanner,
  sectionHeader, field, pathChip, confirmDialog, promptDialog,
} from '../ui.js';
import {
  splitTeam, sortMembers, memberModel, countTeam, availableProfiles, withRoomRoles,
  sinceLabel, updatedLabel, roleLabel, plural,
} from './team-roadmap-model.js';
import { setProfileDirectory, participantLabel, labelOf } from '../participant-label.js';
import { notesSummaryLine, notesOf, entriesOfItem, mountNotesEntries } from '../agent-notes-ui.js';
// Модель участника (WAVE10): чип «модель: …» + «Изменить» → тот же редактор.
import { agentModelLine, openAgentModelEditor } from '../agent-model-ui.js';

const POLL_MS = 5000;
const ROLE_OPTIONS = [
  { value: 'worker', label: 'исполнитель' },
  { value: 'critic', label: 'критик' },
  { value: 'observer', label: 'наблюдатель' },
];

export async function mount(el, ctx) {
  ensureCss('css/view-team.css');
  ensureCss('css/agent-notes.css');      // строка «Особые указания» (WAVE9 §9)

  const roomId = String((ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || '');
  if (!roomId) {
    el.appendChild(h('div', { class: 'view__inner' },
      emptyState({ icon: 'error', title: 'Комната не выбрана', text: 'Выберите комнату в левой колонке.' })));
    return { unmount() {} };
  }

  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const navigate = typeof ctx.navigate === 'function' ? ctx.navigate : (hash) => { location.hash = hash; };

  const state = {
    loading: true,
    error: null,
    offline: false,
    team: null,
    room: ctx.room || null,
    profiles: [],
    roots: new Set(),
    editingRole: null,
    updatedAt: 0,
    settingsSig: '',
    outsideSig: '',
    catalog: null,
    overlay: null,
    notesOpen: new Set(),   // W14-I: какие карточки с раскрытыми «Особенностями» держать открытыми
  };
  const cards = new Map(); // profile → {node, sig, since}
  let disposed = false;
  let poller = null;
  let headCountEl = null;
  const abort = new AbortController();

  /* --------------------------------------------------------------- каркас */

  const headHost = h('div', { class: 'team__head' });
  const errorHost = h('div', { class: 'team__error' });
  const gridHost = h('div', { class: 'team-grid' });
  const outsideHost = h('div', { class: 'team__outside' });
  const settingsHost = h('div', { class: 'team__settings' });

  const root = h('div', { class: 'view__inner team' }, errorHost, headHost, gridHost, outsideHost, settingsHost);
  el.appendChild(root);

  const sumEl = h('span', { class: 'small muted team__summary' }, '');
  const updated = h('span', { class: 'small muted team__updated nowrap' }, '');
  const addBtn = h('button', {
    class: 'btn btn--primary btn--sm', type: 'button',
    onclick: () => { openAddDialog().catch(() => {}); },
  }, icon('person-add', { size: 13 }), 'Добавить агента');

  /** Ссылка на задачу: страница запроса, если id — корень, иначе лента запросов. */
  function taskLink(taskId, title) {
    const label = title || taskId || 'задача';
    const isRoot = !!taskId && state.roots.has(String(taskId));
    const href = isRoot
      ? `#/room/${encodeURIComponent(roomId)}/request/${encodeURIComponent(taskId)}`
      : `#/room/${encodeURIComponent(roomId)}/composer`;
    return h('a', {
      class: 'tm-task truncate',
      href,
      title: isRoot ? 'Открыть запрос: ' + label : 'Открыть ленту запросов комнаты',
    }, label);
  }

  /* --------------------------------------------------------- карточка */

  function sigOf(model) {
    return JSON.stringify([
      model.name, model.role, model.pinned, model.state, model.description, model.model,
      model.label, model.labelName, model.labelJob, model.customLabel, model.labelPending,
      model.model_settings || null, model.agent_default || null,
      model.model_sent || null,
      // W14-I: число записей «Особенности для этой комнаты» — карточка обновится после правки
      (entriesOfItem(notesOf(state.room || {})[model.profile] || null).length),
      model.current && model.current.taskId, model.current && model.current.startedAt,
      model.last && model.last.taskId, model.last && model.last.finishedAt,
      model.counts.done, model.counts.blocked, model.counts.active,
    ]);
  }

  /** Инлайн-редактор роли: кнопка → select; сохранение через переотправку участника. */
  function roleControl(model, rerender) {
    if (!model.roleEditable) {
      return h('span', {
        class: 'tm-role-static small muted',
        title: 'Роль руководителя комнаты и критика задаётся настройками комнаты',
      }, 'роль фиксирована');
    }
    if (state.editingRole === model.profile) {
      const select = h('select', { class: 'select tm-role-select', 'aria-label': 'Роль в комнате' });
      for (const opt of ROLE_OPTIONS) {
        const o = h('option', { value: opt.value }, opt.label);
        if (opt.value === model.role) o.selected = true;
        select.appendChild(o);
      }
      let closed = false;
      const finish = (save) => {
        if (closed) return;
        closed = true;
        state.editingRole = null;
        const next = select.value;
        if (save && next && next !== model.role) {
          changeRole(model, next).catch(() => {});
        } else {
          rerender();
        }
      };
      select.addEventListener('change', () => finish(true));
      select.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(false); }
      });
      select.addEventListener('blur', () => finish(false));
      setTimeout(() => { try { select.focus(); } catch { /* noop */ } }, 0);
      return h('div', { class: 'tm-role-edit' }, select);
    }
    return h('button', {
      class: 'btn btn--ghost btn--sm tm-role-btn',
      type: 'button',
      title: 'Изменить роль в комнате',
      onclick: () => { state.editingRole = model.profile; rerender(); },
    }, h('span', { class: 'tm-role-label' }, model.roleLabel), icon('edit', { size: 11 }));
  }

  /**
   * «Особенности для этой комнаты» на карточке участника (WAVE14 §13, пункт 1).
   *
   * Видимое место редактирования: раскрыл — увидел записи и поле с диктовкой,
   * добавил запись (она сразу в реестре комнаты и в журнале). Поля профиля не меняются.
   */
  function notesBlock(model, rerender) {
    const room = state.room || {};
    const item = notesOf(room)[model.profile] || null;
    const entries = entriesOfItem(item);
    const body = h('div', { class: 'tm-card__notesbody' });
    const summaryCount = h('span', { class: 'tm-card__notescount' },
      entries.length ? entries.length + ' ' + plural(entries.length, ['запись', 'записи', 'записей']) : 'нет');
    const summary = h('summary', { class: 'tm-card__notessum', title: 'Добавка к роли участника — только в этой комнате' },
      'Особенности для этой комнаты', summaryCount);
    const det = h('details', { class: 'tm-card__notes' }, summary, body);
    if (state.notesOpen.has(model.profile)) det.setAttribute('open', '');
    let mounted = null;
    let loading = false;
    const mount = () => {
      if (mounted || loading) return;
      loading = true;
      mountNotesEntries(body, ctx, {
        room, roomId, profile: model.profile, entries, live: true, compact: true,
        onChange: (profile, text, list) => {
          // Счётчик у раскрытия обновляем сразу: карточка может перерисоваться позже.
          const n = Array.isArray(list) ? list.length : entries.length;
          summaryCount.textContent = n ? n + ' ' + plural(n, ['запись', 'записи', 'записей']) : 'нет';
          load({ silent: true }).catch(() => {});
        },
      }).then((ui) => {
        mounted = ui;
        loading = false;
      }).catch(() => { loading = false; });
    };
    det.addEventListener('toggle', () => {
      if (det.open) {
        state.notesOpen.add(model.profile);
        mount();
      } else {
        state.notesOpen.delete(model.profile);
      }
    });
    if (state.notesOpen.has(model.profile)) mount();
    return det;
  }

  function memberCard(model, rerender) {
    const sinceNode = model.current && model.current.since
      ? h('span', { class: 'tm-since tnum', 'data-since': String(model.current.startedAt || '') },
        '· ' + model.current.since)
      : null;

    const statusLine = h('div', { class: 'tm-state tm-state--' + model.stateKind },
      h('span', { class: 'tm-dot' }),
      h('span', { class: 'tm-state__label' }, model.stateLabel),
      sinceNode);

    // WAVE13 §11: заголовок карточки — имя и должность из описания профиля
    // («Анна — финансы и экономика (CFO)»), техническое имя профиля — мелко под
    // ним, слово «исполнитель» — роль бейджем, а не заголовком.
    const label = labelOf({
      profile: model.profile, name: model.labelName, job: model.labelJob, label: model.label,
      role: model.role, title: model.title || model.name,
      source: model.labelSource, pending: model.labelPending,
    });

    const head = h('div', { class: 'tm-card__head' },
      avatar(label.short, { size: 34, title: label.title + ' · ' + model.profile }),
      h('div', { class: 'tm-card__who' },
        h('div', { class: 'tm-card__name', title: label.title + ' · профиль ' + model.profile }, label.title),
        h('div', { class: 'tm-card__id small muted' },
          // Заголовок уже роль — бейдж не повторяем; у исполнителей он короткий.
          label.source === 'role'
            ? null
            : h('span', { class: 'tm-card__role small', title: 'Роль в комнате: ' + label.role }, label.roleShort),
          h('span', { class: 'tm-card__profile mono truncate', title: 'Профиль Hermes: ' + model.profile },
            model.profile))),
      model.pinned
        ? h('span', { class: 'chip chip--outline tm-badge', title: 'Закреплён в комнате: не убирается из состава' },
          icon('pin', { size: 11 }), 'всегда в составе')
        : null);

    // Описание профиля — одна короткая строка; полный текст в подсказке и по кнопке
    // «ещё» (замечание Михаила: не обрезать на «…» там, где текст можно показать).
    // Если описание — это та же «Имя — должность», второй раз не повторяем.
    const rawDesc = String(model.description || '');
    const descText = rawDesc && rawDesc !== label.title ? rawDesc : String(model.note || '');
    const longDesc = descText.length > 90;
    const desc = h('p', {
      class: 'tm-card__desc' + (descText ? '' : ' muted'),
      title: descText || 'Описание профиля не задано.',
    }, descText || 'Описание профиля не задано.');
    const moreBtn = longDesc
      ? h('button', { class: 'tm-card__more', type: 'button', 'aria-expanded': 'false' }, 'ещё')
      : null;
    if (longDesc) {
      const toggle = (ev) => {
        if (ev) ev.preventDefault();
        const open = desc.classList.toggle('tm-card__desc--open');
        moreBtn.textContent = open ? 'свернуть' : 'ещё';
        moreBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      };
      moreBtn.addEventListener('click', toggle);
    }
    const descBox = h('div', { class: 'tm-card__descbox' }, desc, moreBtn);

    // WAVE10: маленький чип «модель: …» + «Изменить» — открывает тот же редактор.
    const meta = h('div', { class: 'tm-card__meta' },
      agentModelLine(state.room || ctx.room, model.profile, {
        profile: model.profile,
        role: model.role,
        title: model.title || model.name,
        model_settings: model.model_settings,
        agent_default: model.agent_default,
        model_sent: model.model_sent,
      }, { catalog: state.catalog, onEdit: (profile) => { openMemberModel(profile).catch(() => {}); } }),
      h('span', { class: 'tm-card__counts tnum', title: 'Задачи профиля в комнате' },
        'готово ' + model.counts.done,
        model.counts.blocked ? ' · блок ' + model.counts.blocked : '',
        model.counts.active ? ' · в работе ' + model.counts.active : ''));

    const nowBox = h('div', { class: 'tm-card__line tm-card__line--now' },
      h('span', { class: 'tm-card__label' }, 'сейчас'),
      model.current
        ? taskLink(model.current.taskId, model.current.title)
        : h('span', { class: 'small muted' },
          model.stateKind === 'waiting' ? 'ждёт ответа человека' : 'активной задачи нет'));

    const lastBox = h('div', { class: 'tm-card__line' },
      h('span', { class: 'tm-card__label' }, 'последняя'),
      model.last
        ? h('span', { class: 'tm-card__last-body' },
          taskLink(model.last.taskId, model.last.title),
          h('span', { class: 'small muted nowrap' }, model.last.at ? ' · ' + model.last.at : ''))
        : h('span', { class: 'small muted' }, 'работ ещё не было'));

    // «Подпись» — как участник называется в этой комнате (WAVE13 §11). У руководителя
    // и критика подпись задаётся настройками комнаты, поэтому кнопки нет.
    const labelBtn = model.removable
      ? h('button', {
        class: 'btn btn--ghost btn--sm tm-label-btn', type: 'button',
        title: model.customLabel
          ? 'Подпись задана вручную: ' + model.customLabel + '. Изменить'
          : 'Подписать участника: имя и должность вместо технического профиля',
        onclick: () => { editLabel(model).catch(() => {}); },
      }, icon('edit', { size: 12 }), 'Подпись')
      : null;

    const foot = h('div', { class: 'tm-card__foot' },
      h('div', { class: 'tm-card__acts' }, roleControl(model, rerender), labelBtn),
      model.removable
        ? h('button', {
          class: 'btn btn--ghost btn--sm tm-remove', type: 'button', title: 'Убрать из комнаты',
          onclick: () => { removeMember(model).catch(() => {}); },
        }, icon('trash', { size: 12 }), 'Убрать')
        : h('span', { class: 'tm-card__hint small muted' },
          model.isDirector ? 'руководитель комнаты — не убирается' : 'критик комнаты — не убирается'));

    return h('article', {
      class: 'card tm-card' + (model.stateKind === 'working' ? ' tm-card--live' : ''),
      dataset: { profile: model.profile },
    }, head, statusLine, descBox, nowBox, lastBox, meta, notesBlock(model, rerender), foot);
  }

  /* ------------------------------------------------------------- отрисовка */

  function buildHead() {
    const box = sectionHeader('Команда', { count: 0, actions: [sumEl, updated, addBtn] });
    headHost.replaceChildren(box);
    headCountEl = box.querySelector('.section-header__count');
  }

  function updateHead(models) {
    const counts = countTeam(models);
    // D19: без данных не «0», а «—»: ноль читается как «в комнате никого».
    if (headCountEl) {
      const known = Boolean(state.team);
      headCountEl.textContent = known ? String(models.length) : '—';
      headCountEl.title = known ? models.length + ' ' + plural(models.length, ['участник', 'участника', 'участников']) : 'Нет данных: приложение не ответило';
      headCountEl.setAttribute('aria-label', headCountEl.title);
    }
    const parts = [];
    if (counts.working) parts.push(counts.working + ' ' + plural(counts.working, ['работает', 'работают', 'работают']));
    if (counts.waiting) parts.push(counts.waiting + ' ' + plural(counts.waiting, ['ждёт', 'ждут', 'ждут']) + ' человека');
    if (!counts.working && !counts.waiting && counts.total) parts.push('все свободны');
    sumEl.textContent = parts.join(' · ');
    updated.textContent = state.updatedAt ? updatedLabel(state.updatedAt) : '';
  }

  function paintGrid(rawList) {
    for (const stale of Array.from(gridHost.children)) {
      if (stale.classList && stale.classList.contains('skeleton-stack')) stale.remove();
    }
    if (!rawList.length) {
      cards.clear();
      gridHost.classList.add('team-grid--empty');
      gridHost.replaceChildren(emptyState({
        icon: 'organization',
        title: 'В комнате пока никого',
        text: 'Добавьте агента из списка профилей Hermes — он сразу появится в раздаче руководителя комнаты.',
        action: { label: 'Добавить агента', variant: 'primary', onclick: () => { openAddDialog().catch(() => {}); } },
      }));
      return;
    }
    gridHost.classList.remove('team-grid--empty');
    const wanted = new Set();
    let prev = null;
    for (const raw of rawList) {
      // Настройки модели (WAVE10) приходят у участника комнаты, а не у профиля:
      // сначала смотрим /team, затем состав комнаты — иначе чип после сохранения
      // останется прежним (карточка не перерисуется).
      const roomMember = (Array.isArray(state.room && state.room.members) ? state.room.members : [])
        .find((m) => m && String(m.profile || '') === String((raw && raw.profile) || '')) || null;
      const model = Object.assign(memberModel(raw, state.profiles), {
        model_settings: (raw && raw.model_settings) || (roomMember && roomMember.model_settings) || null,
        agent_default: (raw && raw.agent_default) || (roomMember && roomMember.agent_default) || null,
        // W13-A: «ушло на: …» — факт последнего запуска участника (сервер считает по сессии воркера)
        model_sent: (raw && raw.model_sent) || (roomMember && roomMember.model_sent) || null,
      });
      wanted.add(model.profile);
      const sig = sigOf(model);
      const editing = state.editingRole === model.profile;
      let entry = cards.get(model.profile);
      const needRebuild = !entry || entry.editing !== editing || (entry.sig !== sig && !editing);
      if (needRebuild) {
        const rerender = () => paintGrid(rawList);
        const node = memberCard(model, rerender);
        if (entry && entry.node.parentNode) entry.node.replaceWith(node);
        entry = { node, sig, editing };
        cards.set(model.profile, entry);
      }
      const target = entry.node;
      if (prev ? prev.nextSibling !== target : gridHost.firstChild !== target) {
        gridHost.insertBefore(target, prev ? prev.nextSibling : gridHost.firstChild);
      }
      prev = target;
    }
    for (const [profile, entry] of Array.from(cards)) {
      if (!wanted.has(profile)) {
        entry.node.remove();
        cards.delete(profile);
      }
    }
  }

  /** Только «уже N мин» — без пересборки карточек. */
  function tickTimes() {
    for (const entry of cards.values()) {
      const node = entry.node.querySelector('.tm-since');
      if (!node) continue;
      const startedAt = Number(node.getAttribute('data-since'));
      if (!startedAt) continue;
      const text = sinceLabel(startedAt);
      if (text && node.textContent !== text) node.textContent = text;
    }
  }

  function paintOutside() {
    const outside = splitTeam(state.team).outside;
    const sig = JSON.stringify(outside.map((m) => [m.profile, m.state, (m.current && m.current.title) || '']));
    if (sig === state.outsideSig) return;
    state.outsideSig = sig;
    if (!outside.length) {
      outsideHost.replaceChildren();
      return;
    }
    const rows = outside.map((raw) => {
      const model = memberModel(raw, state.profiles);
      const label = labelOf({
        profile: model.profile, name: model.labelName, job: model.labelJob, label: model.label,
        role: model.role, title: model.title || model.name, source: model.labelSource,
      });
      return h('div', { class: 'tm-outside-row' },
        avatar(label.short, { size: 26, title: label.title + ' · ' + model.profile }),
        h('div', { class: 'tm-outside-row__main' },
          h('div', { class: 'tm-outside-row__name truncate', title: 'Профиль Hermes: ' + model.profile },
            label.title,
            h('span', { class: 'small muted' }, ' · ' + label.role)),
          h('div', { class: 'tm-state tm-state--' + model.stateKind + ' small truncate' },
            h('span', { class: 'tm-dot' }),
            h('span', { class: 'tm-state__label' }, model.stateLabel),
            model.current ? h('span', { class: 'muted truncate' }, ' · ' + model.current.title) : null)),
        h('button', {
          class: 'btn btn--secondary btn--sm', type: 'button',
          title: 'Добавить профиль в состав комнаты',
          onclick: () => { addExisting(model.profile).catch(() => {}); },
        }, icon('person-add', { size: 12 }), 'в комнату'));
    });
    outsideHost.replaceChildren(h('section', { class: 'card tm-outside' },
      h('div', { class: 'card__head' },
        h('div', { class: 'card__title' }, 'Работают вне состава'),
        h('div', { class: 'card__sub small muted' }, 'Их задачи есть в комнате, а самих профилей в составе нет')),
      h('div', { class: 'card__body tm-outside__body' }, rows)));
  }

  /** Подпись профиля для строк настроек: имя и должность, иначе роль (WAVE13 §11). */
  function labelForProfile(profile, role) {
    const entry = (state.profiles || []).find((p) => String((p && p.profile) || '') === String(profile || '')) || null;
    return participantLabel(profile, { entry, role });
  }

  function paintSettings(force = false) {
    const room = state.room || {};
    const sig = JSON.stringify([room.orchestrator, room.critic, room.max_rounds, room.context_files,
      room.agent_notes || null, room.critic_mode || 'rounds', room.critic_round_cap || null]);
    if (!force && sig === state.settingsSig) return;
    state.settingsSig = sig;

    const director = room.orchestrator || '';
    const critic = room.critic || '';
    const rounds = Number(room.max_rounds) || 1;
    const untilApproved = String(room.critic_mode || 'rounds') === 'until_approved';
    const roundCap = Number(room.critic_round_cap) || 12;
    const files = Array.isArray(room.context_files) ? room.context_files : [];

    const row = (label, value) => h('div', { class: 'tm-fact' },
      h('span', { class: 'tm-fact__label' }, label),
      h('span', { class: 'tm-fact__value' }, value));

    const changeBtn = h('button', {
      class: 'btn btn--secondary btn--sm', type: 'button',
      title: 'Руководитель комнаты, критик, раунды критика, файлы контекста',
      onclick: () => { openRoomSettings(); },
    }, icon('edit', { size: 12 }), 'Изменить…');

    settingsHost.replaceChildren(h('section', { class: 'card tm-settings' },
      h('div', { class: 'card__head' },
        h('div', { class: 'card__title' }, 'Как работает комната'),
        h('div', { class: 'card__actions' }, changeBtn)),
      h('div', { class: 'card__body stack stack--tight' },
        h('p', { class: 'small muted tm-settings__intro' },
          'Запрос уходит руководителю комнаты: он раздаёт работу команде и сводит один ответ, критик проверяет свод перед показом.'),
        row('Руководитель комнаты', director
          ? h('span', { class: 'tm-fact__pair' }, avatar(labelForProfile(director, 'orchestrator').short, { size: 20 }),
            h('span', { class: 'small', title: 'Профиль Hermes: ' + director },
              labelForProfile(director, 'orchestrator').title))
          : h('span', { class: 'muted' }, 'не назначен')),
        row('Критик', critic
          ? h('span', { class: 'tm-fact__pair' }, avatar(labelForProfile(critic, 'critic').short, { size: 20 }),
            h('span', { class: 'small', title: 'Профиль Hermes: ' + critic },
              labelForProfile(critic, 'critic').title))
          : h('span', { class: 'muted' }, 'без критика')),
        row('Раундов критика', untilApproved
          ? 'пока не одобрит полностью (потолок ' + roundCap + ')'
          : rounds + ' ' + plural(rounds, ['раунд', 'раунда', 'раундов']) + ' проверки '
            + (rounds === 1 ? '— быстро' : '— тщательнее')),
        // Пояснение простым языком (п. 7): что такое раунд — рядом с настройкой.
        h('p', { class: 'small muted tm-settings__note' },
          'Критик — участник, который проверяет сводный ответ перед выдачей. Раунд — один цикл '
          + '«критик проверил → если нашёл замечания, исполнители доработали → снова проверили». '
          + (untilApproved
            ? 'Сейчас ответ выдаётся только после полного одобрения критика; если замечания '
              + 'повторяются или раундов станет больше ' + roundCap + ', цикл остановится и спросит вас.'
            : 'Один раунд — быстрее, два — тщательнее: у команды есть вторая попытка исправить '
              + 'замечания.')),
        row('Файлы контекста', files.length
          ? h('span', { class: 'tm-filelist' }, files.map((p) => pathChip(p)))
          : h('span', { class: 'muted' }, 'не заданы')),
        // Особая добавка к роли ИМЕННО в этой комнате (WAVE9 §9): свёрнуто, «Показать».
        notesSummaryLine(room))));
  }

  function paintError() {
    if (!state.error) { errorHost.replaceChildren(); return; }
    const retry = () => load({ silent: false });
    const box = errorBanner(state.error, retry);
    if (state.offline && state.team) {
      box.appendChild(h('span', { class: 'small muted team__offline' }, 'показаны последние полученные данные'));
    }
    errorHost.replaceChildren(box);
  }

  function paintAll({ forceSettings = false } = {}) {
    // Д1: подпись «руководитель комнаты» — по room.orchestrator, а не по роли из движка.
    const rawList = sortMembers(withRoomRoles(splitTeam(state.team).inside, state.room || ctx.room));
    updateHead(rawList.map((raw) => memberModel(raw, state.profiles)));
    paintGrid(rawList);
    paintOutside();
    paintSettings(forceSettings);
    paintError();
  }

  async function load({ silent = false } = {}) {
    if (disposed) return;
    if (!silent) {
      gridHost.replaceChildren(skeleton(4));
      gridHost.classList.remove('team-grid--empty');
      errorHost.replaceChildren();
    }
    try {
      const team = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/team`, { signal: abort.signal });
      state.team = team;
      state.updatedAt = (team && team.generated_at) || Math.floor(Date.now() / 1000);
      state.error = null;
      state.offline = false;

      if (!state.profiles.length) {
        try {
          const list = await api.get('/api/profiles', { signal: abort.signal });
          state.profiles = Array.isArray(list && list.items) ? list.items : [];
          setProfileDirectory(state.profiles);   // WAVE13 §11: подписи в диалоге «Добавить агента»
        } catch { /* без ростера задачи рисуем по данным /team */ }
      }
      if (!state.roots.size) {
        try {
          const feed = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/requests?limit=100`, { signal: abort.signal });
          const items = Array.isArray(feed && feed.items) ? feed.items : [];
          state.roots = new Set(items.map((r) => String((r && r.id) || '')).filter(Boolean));
        } catch { /* ссылки на задачи уйдут в ленту запросов */ }
      }
      if (!state.room || !Array.isArray(state.room.context_files)) {
        try {
          state.room = await api.get(`/api/rooms/${encodeURIComponent(roomId)}`, { signal: abort.signal });
        } catch { /* настройки покажем по данным из store */ }
      }
      if (!state.catalog) {
        // Каталог моделей нужен только для подписи чипа — без него живём на id.
        try {
          state.catalog = await api.get('/api/models', { signal: abort.signal });
        } catch { state.catalog = null; }
      }

      paintAll({ forceSettings: !silent });
    } catch (err) {
      if (disposed || (err && err.name === 'AbortError')) return;
      if (!silent || !state.team) {
        state.error = err;
        // D19: без данных счётчик показывает «—», а не «0» — рисуем шапку и здесь.
        updateHead([]);
        errorHost.replaceChildren(errorBanner(err, () => load({ silent: false })));
        if (!state.team) {
          gridHost.replaceChildren();
          outsideHost.replaceChildren();
          settingsHost.replaceChildren();
        }
      } else {
        state.offline = true;
        paintError();
      }
    }
  }

  /** Перечитать комнату: состав, настройки, файлы контекста после изменений. */
  async function refreshRoom() {
    try {
      state.room = await api.get(`/api/rooms/${encodeURIComponent(roomId)}`, { signal: abort.signal });
      state.settingsSig = '';
    } catch { /* оставим то, что уже показано */ }
  }

  async function changeRole(model, role) {
    const membersPath = `/api/rooms/${encodeURIComponent(roomId)}/members`;
    try {
      // API не умеет менять роль на месте: убираем участника и добавляем с новой ролью.
      await api.del(membersPath + '/' + encodeURIComponent(model.profile));
      try {
        await api.post(membersPath, { profile: model.profile, role, note: model.note || '', pinned: !!model.pinned });
      } catch (err) {
        try {
          await api.post(membersPath, {
            profile: model.profile, role: model.role, note: model.note || '', pinned: !!model.pinned,
          });
        } catch { /* вернуть не удалось — покажем исходную ошибку */ }
        throw err;
      }
      toast('Роль обновлена: ' + roleLabel(role), 'ok');
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось изменить роль', 'danger');
    } finally {
      state.editingRole = null;
      await refreshRoom();
      await load({ silent: true });
    }
  }

  /** Подпись участника в комнате: «Имя — должность» (WAVE13 §11). */
  async function editLabel(model) {
    const value = await promptDialog({
      title: 'Подпись участника',
      label: 'Имя и должность',
      initial: model.customLabel || model.label || '',
      placeholder: 'Анна — финансы и экономика (CFO)',
      hint: 'Так участник называется в этой комнате. Пустое поле — вернуть подпись из '
        + 'описания профиля Hermes. Файлы профиля (SOUL.md, config.yaml) не меняются.',
      submitLabel: 'Сохранить',
    });
    if (value === null) return;
    try {
      const path = `/api/rooms/${encodeURIComponent(roomId)}/agents/${encodeURIComponent(model.profile)}/label`;
      await api.put(path, { label: String(value).trim() });
      toast('Подпись сохранена', 'ok');
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось сохранить подпись', 'danger');
    }
    await refreshRoom();
    await load({ silent: true });
  }

  async function removeMember(model) {
    const ok = await confirmDialog({
      title: 'Убрать из комнаты?',
      text: model.name + ' больше не будет получать задачи этой комнаты. Задачи и история останутся.',
      confirmLabel: 'Убрать',
      cancelLabel: 'Оставить',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(model.profile)}`);
      toast('Участник убран', 'ok');
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось убрать участника', 'danger');
    }
    await refreshRoom();
    await load({ silent: true });
  }

  async function addExisting(profile) {
    try {
      await api.post(`/api/rooms/${encodeURIComponent(roomId)}/members`, { profile, role: 'worker', pinned: false });
      toast('Добавлен в комнату: ' + profile, 'ok');
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось добавить', 'danger');
    }
    await refreshRoom();
    await load({ silent: true });
  }

  /* ------------------------------------------------ диалог «Добавить агента» */

  function pickRow(profile, group) {
    const input = h('input', { type: 'radio', name: 'tm-profile', value: profile.profile });
    input.addEventListener('change', () => { if (input.checked) group.value = profile.profile; });
    const row = h('label', { class: 'tm-pick' },
      input,
      avatar(profile.title || profile.profile, { size: 28 }),
      h('span', { class: 'tm-pick__main' },
        h('span', { class: 'tm-pick__name' }, profile.title || profile.profile),
        h('span', { class: 'tm-pick__id mono small muted' }, profile.profile),
        profile.description ? h('span', { class: 'tm-pick__desc small muted' }, profile.description) : null),
      profile.model ? h('span', { class: 'tm-model mono' }, profile.model) : null,
      profile.available === false ? h('span', { class: 'chip chip--warn' }, 'нет на диске') : null);
    row.addEventListener('click', () => { input.checked = true; group.value = profile.profile; });
    return row;
  }

  function openAddDialog() {
    return new Promise((resolve) => {
      const free = availableProfiles(state.profiles, state.team, state.room).filter((p) => p.available !== false);
      if (!free.length) {
        toast(state.profiles.length ? 'Все профили уже в комнате' : 'Список профилей недоступен', 'warn');
        resolve(null);
        return;
      }
      const group = { value: free[0].profile };
      const listBox = h('div', { class: 'tm-pick-list' }, free.map((p) => pickRow(p, group)));

      const roleSelect = h('select', { class: 'select', 'aria-label': 'Роль в комнате' },
        ROLE_OPTIONS.map((o) => h('option', { value: o.value }, o.label)));
      const pinInput = h('input', { type: 'checkbox' });
      const pinLabel = h('label', { class: 'checkbox' }, pinInput, h('span', {}, 'На постоянную — не убирать из комнаты'));

      const body = h('div', { class: 'stack tm-add-body' },
        h('div', { class: 'small muted' }, 'Профили Hermes, которых ещё нет в комнате:'),
        listBox,
        field({ label: 'Роль в комнате', control: roleSelect, hint: 'Роль видна в карточке участника и в раздаче руководителя комнаты.' }),
        pinLabel);

      const cancelBtn = h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => finish(null) }, 'Отмена');
      const submitBtn = h('button', { class: 'btn btn--primary', type: 'button', onclick: () => submit() }, 'Добавить');

      const dialog = h('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Добавить агента' },
        h('div', { class: 'dialog__head' },
          h('div', { class: 'dialog__title' }, 'Добавить агента'),
          h('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', title: 'Закрыть', onclick: () => finish(null) },
            icon('close', { size: 14 }))),
        h('div', { class: 'dialog__body' }, body),
        h('div', { class: 'dialog__foot' }, cancelBtn, submitBtn));

      const overlay = h('div', { class: 'dialog-backdrop' }, dialog);
      let done = false;
      function finish(value) {
        if (done) return;
        done = true;
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        document.removeEventListener('keydown', onKey, true);
        resolve(value);
      }
      async function submit() {
        const profile = group.value;
        if (!profile) { toast('Выберите профиль', 'warn'); return; }
        submitBtn.disabled = true;
        try {
          await api.post(`/api/rooms/${encodeURIComponent(roomId)}/members`, {
            profile, role: roleSelect.value || 'worker', pinned: !!pinInput.checked,
          });
          toast('Агент добавлен: ' + profile, 'ok');
          finish(profile);
          await refreshRoom();
          await load({ silent: true });
        } catch (err) {
          submitBtn.disabled = false;
          toast(err && err.message ? err.message : 'Не удалось добавить агента', 'danger');
        }
      }
      function onKey(ev) {
        if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(null); }
      }
      overlay.addEventListener('mousedown', (ev) => { if (ev.target === overlay) finish(null); });
      document.addEventListener('keydown', onKey, true);
      document.body.appendChild(overlay);
      setTimeout(() => {
        const first = listBox.querySelector('input[type=radio]');
        if (first) { first.checked = true; try { first.focus(); } catch { /* noop */ } }
      }, 0);
    });
  }

  /* ------------------------------------------------- модель участника (WAVE10) */

  /** Тот же редактор, что в диалоге комнаты, — поверх вкладки «Команда». */
  async function openMemberModel(profile) {
    const member = (Array.isArray(state.team && state.team.members) ? state.team.members : [])
      .find((m) => m && String(m.profile || '') === String(profile)) || null;
    await openAgentModelEditor(ctx, {
      room: state.room || ctx.room,
      profile,
      member,
      catalog: state.catalog,
    });
    await refreshRoom();
    await load({ silent: true });
  }

  /* -------------------------------------------- настройки комнаты (диалог) */

  async function openRoomSettings() {
    try {
      const mod = await import('./room-dialog.js');
      if (!mod || typeof mod.openRoomDialog !== 'function') throw new Error('room-dialog.js не экспортирует openRoomDialog');
      await mod.openRoomDialog(ctx, state.room || ctx.room);
      await load({ silent: true });
    } catch (err) {
      if (typeof console !== 'undefined' && console.warn) console.warn('[team] room-dialog недоступен', err);
      toast('Настройки комнаты пока недоступны — обновите страницу и попробуйте ещё раз', 'warn');
    }
  }

  /* ------------------------------------------------------------- жизненный цикл */

  buildHead();
  await load({ silent: false });

  poller = pollApi(() => load({ silent: true }), POLL_MS, {
    pauseHidden: true,
    immediate: false,
    onError: () => { /* ошибки показываются баннером */ },
  });
  const clock = setInterval(() => { if (!disposed) tickTimes(); }, 30000);

  return {
    unmount() {
      disposed = true;
      abort.abort();
      clearInterval(clock);
      if (poller && typeof poller.stop === 'function') poller.stop();
      if (state.overlay && state.overlay.parentNode) state.overlay.parentNode.removeChild(state.overlay);
      root.remove();
    },
  };
}
