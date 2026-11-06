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
import { setProfileDirectory, participantLabel, labelOf, shortJob } from '../participant-label.js';
import { notesSummaryLine, notesOf, entriesOfItem } from '../agent-notes-ui.js';
// Пакет F: модальный редактор «Особенности для этой комнаты» — только ссылка с карточки.
import { notesLink } from '../notes-editor.js';
// Модель участника (WAVE10): чип «модель: …» + «Изменить» → тот же редактор.
import { agentModelLine, modelSent, openAgentModelEditor } from '../agent-model-ui.js';

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
      // WAVE17 B-17: подпись руководителя/критика живёт в карте комнаты —
      // меняется она, перерисовывается и карточка.
      ((state.room && state.room.role_labels) || {})[model.profile] || '',
      model.model_settings || null, model.agent_default || null,
      model.model_sent || null,
      // W14-I: число записей «Особенности для этой комнаты» — карточка обновится после правки
      (entriesOfItem(notesOf(state.room || {})[model.profile] || null).length),
      model.current && model.current.taskId, model.current && model.current.startedAt,
      model.last && model.last.taskId, model.last && model.last.finishedAt,
      model.counts.done, model.counts.blocked, model.counts.active,
    ]);
  }

  /** Живой статус одной строкой: точка, «работает / свободен / ждёт человека», «уже N мин». */
  function stateLine(model) {
    const sinceNode = model.current && model.current.since
      ? h('span', { class: 'tm-since tnum', 'data-since': String(model.current.startedAt || '') },
        '· ' + model.current.since)
      : null;
    return h('div', { class: 'tm-state tm-state--' + model.stateKind },
      h('span', { class: 'tm-dot' }),
      h('span', { class: 'tm-state__label' }, model.stateLabel),
      sinceNode);
  }

  /**
   * Меню «⋯» карточки (WAVE17 P2/B-17): у руководителя, критика и исполнителей
   * ОДИН набор действий — «Изменить модель», «Подпись», «Роль в комнате…»;
   * «Убрать из комнаты» — только у обычных исполнителей.
   */
  function cardMenu(model) {
    const wrap = h('div', { class: 'tm-menu' });
    const pop = h('div', { class: 'tm-menu__pop', role: 'menu', hidden: true });
    const btn = h('button', {
      class: 'btn btn--ghost btn--icon btn--sm tm-menu__btn', type: 'button',
      'aria-haspopup': 'menu', 'aria-expanded': 'false',
      'aria-label': 'Действия: ' + (model.labelName || model.name || model.profile),
      title: 'Действия с участником',
    }, icon('kebab-horizontal', { size: 15 }));

    const onDoc = (ev) => { if (!wrap.contains(ev.target)) close(); };
    const onKey = (ev) => {
      if (ev.key === 'Escape') {
        if (ev.stopPropagation) ev.stopPropagation();
        close();
        try { btn.focus(); } catch { /* noop */ }
      }
    };
    let closed = false;
    function close() {
      if (closed) return;
      closed = true;
      pop.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('mousedown', onDoc, true);
      document.removeEventListener('keydown', onKey, true);
    }
    function openMenu() {
      closed = false;
      pop.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
      document.addEventListener('mousedown', onDoc, true);
      document.addEventListener('keydown', onKey, true);
      const first = pop.querySelector('button');
      if (first) { try { first.focus(); } catch { /* noop */ } }
    }
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (pop.hidden) openMenu(); else close();
    });

    const item = (label, iconName, run, extra = {}) => h('button', {
      class: 'tm-menu__item' + (extra.danger ? ' tm-menu__item--danger' : '')
        + (extra.note ? ' tm-menu__item--rich' : ''),
      type: 'button', role: 'menuitem', tabindex: '-1',
      title: extra.hint || label,
      onclick: () => { close(); run(); },
    }, icon(iconName, { size: 13 }),
    h('span', { class: 'tm-menu__label' }, label,
      extra.note ? h('span', { class: 'tm-menu__note' }, extra.note) : null));

    pop.appendChild(item('Изменить модель', 'settings-gear', () => { openMemberModel(model.profile).catch(() => {}); }));
    pop.appendChild(item('Подпись', 'edit', () => { editLabel(model).catch(() => {}); }));
    if (model.roleEditable) {
      pop.appendChild(item('Роль в комнате…', 'organization', () => { openRoleDialog(model).catch(() => {}); }));
    }
    // W19 B-28: закрепление живёт здесь, с объяснением, а не значком скрепки на
    // лице карточки. У руководителя и критика закрепление не спрашивается: они
    // всегда в составе, и это видно текстом «обязательная роль» рядом с именем.
    if (!model.isDirector && !model.isCritic) {
      pop.appendChild(model.pinned
        ? item('Открепить', 'pin', () => { togglePin(model).catch(() => {}); }, {
          hint: 'Снять закрепление — участника снова можно убрать из комнаты',
          note: 'Сейчас закреплён: остаётся в составе',
        })
        : item('Закрепить в составе', 'pin', () => { togglePin(model).catch(() => {}); }, {
          hint: 'Оставить участника в комнате, даже если задачи давно не приходят',
          note: 'Останется в комнате, даже без задач',
        }));
    }
    if (model.removable) {
      pop.appendChild(item('Убрать из комнаты', 'trash', () => { removeMember(model).catch(() => {}); }, { danger: true }));
    }
    wrap.append(btn, pop);
    return wrap;
  }

  /** Роль в комнате — маленький диалог вместо инлайн-селекта на карточке. */
  function openRoleDialog(model) {
    return new Promise((resolve) => {
      const group = { value: model.role };
      const rows = ROLE_OPTIONS.map((opt) => {
        const input = h('input', { type: 'radio', name: 'tm-role', value: opt.value });
        input.checked = opt.value === model.role;
        const row = h('label', { class: 'tm-pick tm-pick--role' }, input,
          h('span', { class: 'tm-pick__main' }, h('span', { class: 'tm-pick__name' }, opt.label)));
        row.addEventListener('click', () => { input.checked = true; group.value = opt.value; });
        return row;
      });
      const cancel = h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => finish(false) }, 'Отмена');
      const save = h('button', { class: 'btn btn--primary', type: 'button', onclick: () => finish(true) }, 'Сохранить');
      const dialog = h('div', {
        class: 'dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Роль в комнате',
      },
      h('div', { class: 'dialog__head' },
        h('div', { class: 'dialog__title' }, 'Роль в комнате'),
        h('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', title: 'Закрыть', onclick: () => finish(false) },
          icon('close', { size: 14 }))),
      h('div', { class: 'dialog__body' },
        h('div', { class: 'stack stack--tight' }, rows,
          h('p', { class: 'small muted' }, 'Роль влияет на то, какие задачи участник получает в этой комнате.'))),
      h('div', { class: 'dialog__foot' }, cancel, save));
      const overlay = h('div', { class: 'dialog-backdrop' }, dialog);
      let done = false;
      function finish(ok) {
        if (done) return;
        done = true;
        const next = group.value;
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        document.removeEventListener('keydown', onKey, true);
        resolve(ok ? next : null);
        if (ok && next && next !== model.role) changeRole(model, next).catch(() => {});
      }
      function onKey(ev) {
        if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(false); }
      }
      overlay.addEventListener('mousedown', (ev) => { if (ev.target === overlay) finish(false); });
      document.addEventListener('keydown', onKey, true);
      document.body.appendChild(overlay);
      const first = rows[0] && rows[0].querySelector('input');
      if (first) { try { first.focus(); } catch { /* noop */ } }
    });
  }


  /**
   * Подпись участника для карточки: руками заданная в этой комнате (в том числе
   * для руководителя и критика — WAVE17 B-17, карта `role_labels`), иначе из
   * описания профиля Hermes, иначе роль в комнате.
   */
  function labelPartsOf(model) {
    const roomLabels = (state.room && state.room.role_labels) || {};
    const custom = String(model.customLabel || roomLabels[model.profile] || '').trim();
    const label = labelOf({
      profile: model.profile, name: model.labelName, job: model.labelJob,
      label: custom || model.label,
      role: model.role, title: model.title || model.name,
      source: custom ? 'member' : model.labelSource, pending: model.labelPending,
    });
    const name = String(label.name || '').trim();
    const job = String(label.job || '').trim();
    return { label, custom, name, job, none: !custom && !name && !job };
  }

  /** Сколько записей «Особенности для этой комнаты» у участника. */
  function notesCount(profile) {
    return entriesOfItem(notesOf(state.room || {})[profile] || null).length;
  }

  /**
   * Компактная карточка участника (WAVE17 P2 + B-17).
   *
   * На лице — только то, что нужно каждый день: аватар, имя и должность,
   * роль-бейдж, живой статус, одна строка «Сейчас: …», ОДИН чип модели,
   * ссылка «Особенности (N)» и меню «⋯». Последняя работа, счётчики задач,
   * профиль и правила роли — в «Подробнее» (подсказка/раскрытие), как просил
   * аудит: «СЕЙЧАС/ПОСЛЕДНЯЯ/готово N/роль фиксирована» с лица убрать.
   */
  function memberCard(model) {
    const { label, name, job, none } = labelPartsOf(model);
    const heading = name || (job ? shortJob(job) : label.title);
    const profileTitle = 'Профиль Hermes: ' + model.profile;
    const titleAttr = label.title + ' · ' + profileTitle;

    // «Задайте подпись» вместо сухого «Исполнитель lawyer» (B-17): у профилей
    // без описания (default, lawyer, surveyor) заголовок — роль, а рядом мягкая
    // подсказка, которая сразу открывает поле подписи.
    const jobNode = none
      ? h('button', {
        class: 'tm-card__setlabel', type: 'button',
        title: 'Задайте подпись: как называть участника в этой комнате (профиль Hermes не меняется)',
        onclick: () => { editLabel(model).catch(() => {}); },
      }, 'Задайте подпись', icon('edit', { size: 11 }))
      : (job
        ? h('div', { class: 'tm-card__job', title: job }, shortJob(job))
        : null);

    // Роль не повторяем дважды: бейдж не нужен, если должность — это сама роль
    // («руководитель комнаты», «критик») или заголовок уже роль (нет описания).
    const roleWord = String(label.roleShort || '').toLowerCase();
    const roleFull = String(label.role || '').toLowerCase();
    const jobStart = String(job || '').toLowerCase().replace(/^[^0-9a-zа-яё]+/, '');
    const headingIsRole = !name && !job;
    const startsAsRole = Boolean(jobStart) && (
      jobStart === roleWord || jobStart === roleFull
      || (roleWord && jobStart.indexOf(roleWord + ' ') === 0)
      || (roleFull && jobStart.indexOf(roleFull + ' ') === 0));
    const roleBadge = (headingIsRole || startsAsRole)
      ? null
      : h('span', { class: 'tm-role', title: 'Роль в комнате: ' + label.role }, label.roleShort);

    // W19 B-28: обязательные роли помечены ОДИНАКОВО — текстом «обязательная роль»
    // (раньше у руководителя и критика был разный набор значков без подписей).
    const mandatory = (model.isDirector || model.isCritic)
      ? h('span', { class: 'tm-mandatory', title: 'Руководителя комнаты и критика нельзя убрать из состава — они задаются настройками комнаты' },
        'обязательная роль')
      : null;

    const head = h('div', { class: 'tm-card__head' },
      avatar(label.short, { size: 32, title: titleAttr }),
      h('div', { class: 'tm-card__who' },
        h('div', { class: 'tm-card__titleline' },
          h('span', { class: 'tm-card__name', title: titleAttr }, heading),
          roleBadge),
        jobNode,
        mandatory),
      h('div', { class: 'tm-card__tools' }, cardMenu(model)));

    // Одна строка «Сейчас: …» — обрезка в одну строку, полный текст в подсказке.
    const nowLine = h('div', { class: 'tm-card__now' },
      h('span', { class: 'tm-card__nowlabel' }, 'Сейчас:'),
      model.current
        ? taskLink(model.current.taskId, model.current.title)
        : h('span', { class: 'tm-card__nowempty', title: model.stateKind === 'waiting' ? 'Ждёт ответа человека — работа продолжится после ответа' : 'Свободен: активной задачи нет' },
          model.stateKind === 'waiting' ? 'ждёт ответа человека' : 'свободен'));

    // Предупреждение о модели — ТОЛЬКО если последняя карточка участника ушла
    // не на выбранную модель (P2, пункт 2). Текст человеческий, подробности по кнопке.
    const sent = modelSent({ model_sent: model.model_sent });
    let warn = null;
    if (sent && sent.mismatch) {
      const sentDetail = h('div', { class: 'tm-card__sentdetail', hidden: true },
        h('span', { class: 'tm-card__sentsum' },
          (sent.confirmed ? 'Последняя карточка ушла на: ' : 'В карточке записано: ') + sent.text),
        h('button', {
          class: 'tm-card__linkbtn', type: 'button',
          title: 'Открыть модель, провайдера и мышление участника',
          onclick: () => { openMemberModel(model.profile).catch(() => {}); },
        }, 'Изменить модель'));
      const more = h('button', {
        class: 'tm-card__linkbtn', type: 'button', 'aria-expanded': 'false',
        title: 'Какая модель была на самом деле',
        onclick: () => {
          const open = sentDetail.hidden;
          sentDetail.hidden = !open;
          more.setAttribute('aria-expanded', open ? 'true' : 'false');
        },
      }, 'Показать модель');
      warn = h('div', { class: 'tm-card__warn' },
        icon('warning', { size: 13 }),
        h('span', { class: 'tm-card__warntext' }, 'Последний запуск шёл на другой модели — проверьте'),
        more, sentDetail);
    }

    const chip = agentModelLine(state.room || ctx.room, model.profile, {
      profile: model.profile,
      role: model.role,
      title: model.title || model.name,
      model_settings: model.model_settings,
      agent_default: model.agent_default,
      model_sent: model.model_sent,
    }, {
      catalog: state.catalog, compact: true,
      // W19 B-23: клик по чипу снова открывает редактор модели (как до редизайна).
      onEdit: (profile) => { openMemberModel(profile).catch(() => {}); },
    });

    // W19 B-29: модель сменили, но последний запуск был РАНЬШЕ смены (или в
    // очереди уже стоит карточка с верной моделью) — это не тревога, а ожидание.
    const pendingNote = (sent && sent.pending)
      ? h('div', {
        class: 'tm-card__pending',
        title: 'Модель изменена позже последнего запуска: следующая карточка пойдёт уже на ней',
      },
      icon('clock', { size: 12 }),
      h('span', { class: 'tm-card__pendingtext' },
        sent.pendingText || 'Новая модель применится со следующей задачи'))
      : null;

    // «Особенности (N)» — ссылка на модальный редактор (пакет F), счётчик
    // обновляется сам после правки записей.
    const notes = notesLink(ctx, {
      roomId, profile: model.profile, label: label.title, room: state.room || {},
      count: notesCount(model.profile),
      onChange: () => { refreshRoom().then(() => load({ silent: true })).catch(() => {}); },
    });

    const lastLine = h('div', { class: 'tm-card__line' },
      h('span', { class: 'tm-card__label' }, 'последняя'),
      model.last
        ? h('span', { class: 'tm-card__last-body' },
          taskLink(model.last.taskId, model.last.title),
          h('span', { class: 'small muted nowrap' }, model.last.at ? ' · ' + model.last.at : ''))
        : h('span', { class: 'small muted' }, 'работ ещё не было'));

    const countsLine = h('div', { class: 'tm-card__line' },
      h('span', { class: 'tm-card__label' }, 'задачи'),
      h('span', { class: 'small muted', title: 'Сколько задач профиля в этой комнате закрыто, заблокировано и идёт сейчас' },
        'готово ' + model.counts.done
        + (model.counts.blocked ? ' · блок ' + model.counts.blocked : '')
        + (model.counts.active ? ' · в работе ' + model.counts.active : '')));

    const profileLine = h('div', { class: 'tm-card__line' },
      h('span', { class: 'tm-card__label' }, 'профиль'),
      h('span', { class: 'small muted mono truncate', title: profileTitle }, model.profile));

    const details = h('details', { class: 'tm-card__more' },
      h('summary', { class: 'tm-card__moresum', title: 'Последняя работа, счётчики задач и профиль Hermes' }, 'Подробнее'),
      h('div', { class: 'tm-card__morebody' }, lastLine, countsLine, profileLine,
        model.roleEditable ? null : h('div', { class: 'small muted tm-card__rolenote' },
          model.isDirector ? 'Роль руководителя задаётся настройками комнаты' : 'Роль критика задаётся настройками комнаты')));

    return h('article', {
      class: 'card tm-card' + (model.stateKind === 'working' ? ' tm-card--live' : ''),
      dataset: { profile: model.profile },
    },
    head,
    // Свободен — эту же мысль уже говорит строка «Сейчас: свободен»; отдельная
    // строка статуса нужна только когда есть работа или ожидание человека.
    (model.stateKind === 'idle' && !model.current) ? null : stateLine(model),
    nowLine,
    warn,
    pendingNote,
    h('div', { class: 'tm-card__meta' }, chip, notes),
    details);
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
      let entry = cards.get(model.profile);
      const needRebuild = !entry || entry.sig !== sig;
      if (needRebuild) {
        const node = memberCard(model);
        if (entry && entry.node.parentNode) entry.node.replaceWith(node);
        entry = { node, sig };
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

  async function togglePin(model) {
    // W19 B-28: закрепление — флаг реестра комнаты, а не значок на карточке.
    const want = !model.pinned;
    try {
      await api.patch(`/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(model.profile)}`,
        { pinned: want });
      toast(want ? 'Закреплён в составе: не убирается из комнаты'
        : 'Закрепление снято: участника можно убрать из комнаты', 'ok');
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось изменить закрепление', 'danger');
    }
    await refreshRoom();
    await load({ silent: true });
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
      await refreshRoom();
      await load({ silent: true });
    }
  }

  /** Подпись участника в комнате: «Имя — должность» (WAVE13 §11, B-17 — у всех). */
  async function editLabel(model) {
    const roomLabels = (state.room && state.room.role_labels) || {};
    const custom = String(model.customLabel || roomLabels[model.profile] || '').trim();
    const initial = custom || (model.labelSource === 'role' ? '' : model.label || '');
    const value = await promptDialog({
      title: 'Подпись участника',
      label: 'Имя и должность',
      initial,
      placeholder: 'Анна — финансы и экономика (CFO)',
      hint: 'Так участник называется в этой комнате — у руководителя, критика и исполнителей '
        + 'одинаково. Пустое поле — вернуть подпись из описания профиля Hermes. '
        + 'Файлы профиля (SOUL.md, config.yaml) не меняются.',
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
