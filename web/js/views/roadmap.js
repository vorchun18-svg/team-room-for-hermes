/**
 * roadmap.js — комната, вкладка «Карта проекта».
 *
 * Сверху — блок «Части работы» (карта проекта: карточки частей работы, «Запустить»,
 * «Отложить», «Изменить», «Собрать карту из запросов комнаты»), затем «Где мы
 * сейчас»: текущая фаза, прогресс «готово N из M этапов», что блокирует и что
 * дальше. Ниже — полоса этапов (степпер, на узком экране вертикально), панель
 * деталей выбранного этапа и блок «Решения» с фильтром и поиском. «Свести в
 * систему» создаёт запрос руководителю комнаты «обнови STATE.md».
 *
 * Данные — только ctx.api. Карта опрашивается часто (2 с), пока в ней что-то
 * идёт, и редко (10 с), когда всё спокойно; перерисовываются только
 * изменившиеся секции, фокус и прокрутка не сбрасываются.
 */

import { h, debounce } from '../util.js';
import { icon } from '../icons.js';
import { poll as pollApi } from '../api.js';
import {
  ensureCss, emptyState, skeleton, errorBanner, segmented,
  pathChip, confirmDialog,
} from '../ui.js';
import {
  phaseSummary, pickCurrent, decisionGroups, relatedDecisions, relatedSteps, phaseSource,
  mirrorPathOf, stageMeta, updatedLabel, lastWorkLabel, plural,
} from './team-roadmap-model.js';
import { requestTitle, sourceTitle, sourceHint } from '../scope.js';
import { openStepDialog } from './step-dialog.js';
import {
  renderStreams, projectModeHint, openStreamEditor, launchConfirm,
  streamsSummary, humanError, planBlockVisible, peopleIndex,
} from './plan-streams.js';

const POLL_MS = 15000;
/** Карта проекта: часто, пока что-то идёт; редко, когда всё спокойно. */
const PLAN_FAST_MS = 2000;
const PLAN_SLOW_MS = 10000;
const STATUS_OPTIONS = [
  { value: 'all', label: 'Все' },
  { value: 'accepted', label: 'Принято' },
  { value: 'proposed', label: 'Предложено' },
];

/** Похоже ли на системный id запроса (t_2a6f5c93) — такое в тексте не показываем. */
function looksLikeId(text) {
  return /^(t_[0-9a-z]+|[0-9a-f]{8,24})$/i.test(String(text || '').trim());
}

export async function mount(el, ctx) {
  ensureCss('css/view-roadmap.css');
  ensureCss('css/view-plan.css');
  // W14-H §12: шаги на карте открываются тем же окном (согласен/убрать/поправить).
  ensureCss('css/view-steps.css');

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
    error: null,
    offline: false,
    roadmap: null,
    room: ctx.room || null,
    backlog: [],
    backlogLoaded: false,
    titles: {},
    selected: null,
    filter: { status: 'all', q: '' },
    busy: false,
    plan: null,
    planAvailable: true,
    planBusy: false,
    planSig: '',
    people: new Map(),
    peopleLoaded: false,
    emptySig: '',
    heroSig: '',
    stepsSig: '',
    detailSig: '',
    decisionsSig: '',
  };
  let disposed = false;
  let poller = null;
  let planTimer = null;
  const abort = new AbortController();

  /* --------------------------------------------------------------- каркас */

  const errorHost = h('div', { class: 'roadmap__error' });
  const planHost = h('div', { class: 'roadmap__plan-block' });
  const heroHost = h('div', { class: 'roadmap__hero-block' });
  const stepsHost = h('div', { class: 'roadmap__steps-block' });
  const detailHost = h('div', { class: 'roadmap__detail-block' });
  const decisionsHost = h('div', { class: 'roadmap__decisions-block' });
  const root = h('div', { class: 'view__inner roadmap' },
    errorHost, planHost, heroHost, stepsHost, detailHost, decisionsHost);
  el.appendChild(root);

  const requestHref = (rid) => `#/room/${encodeURIComponent(roomId)}/request/${encodeURIComponent(rid)}`;

  /* ---------------------------------------------------- «Части работы» (карта) */

  const maxParallel = () => {
    const raw = Number(state.room && state.room.max_parallel_streams);
    return isFinite(raw) && raw >= 1 && raw <= 4 ? Math.floor(raw) : 2;
  };

  const planPath = `/api/rooms/${encodeURIComponent(roomId)}/plan`;

  /**
   * Люди комнаты для подписей «Ответственный»: имя профиля и роль.
   * Состав комнаты знает только роли, имена даёт /team («Операции», «Юрист»);
   * если /team недоступен — покажем роль по-русски, профиль уйдёт в title.
   */
  function mergePeople(list) {
    for (const [profile, info] of peopleIndex(list)) {
      const prev = state.people.get(profile) || { title: '', role: '' };
      state.people.set(profile, {
        title: prev.title || info.title,
        role: prev.role || info.role,
      });
    }
  }

  async function loadPeople() {
    if (state.peopleLoaded || disposed) return;
    state.peopleLoaded = true;
    const room = state.room || {};
    const base = [];
    if (room.orchestrator) base.push({ profile: room.orchestrator, role: 'orchestrator' });
    if (room.critic) base.push({ profile: room.critic, role: 'critic' });
    for (const m of (Array.isArray(room.members) ? room.members : [])) base.push(m);
    mergePeople(base);
    try {
      const team = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/team`, { signal: abort.signal });
      mergePeople((team && Array.isArray(team.members) ? team.members : []));
    } catch (err) {
      // Без /team карточки покажут роль из состава комнаты — это не ошибка экрана.
      if (err && err.name === 'AbortError') return;
    }
    // Карта ещё не загружена — её отрисуют уже с именами (лишнего «карты нет» не мигаем).
    if (state.plan) {
      state.planSig = '';
      paintPlan(true);
    }
  }

  function planSigOf(plan) {
    if (!plan) return 'none';
    return JSON.stringify([plan.updated_at, plan.status, plan.streams]);
  }

  /** Перерисовать блок, не трогая его, если ничего не изменилось и в нём фокус. */
  function paintPlan(force = false) {
    if (!state.planAvailable) { planHost.replaceChildren(); return; }
    const sig = planSigOf(state.plan) + '|' + String(state.planBusy) + '|' + String(state.people.size);
    if (!force && sig === state.planSig) return;
    if (!force) {
      const active = globalThis.document ? globalThis.document.activeElement : null;
      if (active && planHost.contains && planHost.contains(active)) return;
    }
    state.planSig = sig;
    planHost.replaceChildren(renderStreams({
      plan: state.plan,
      roomId,
      room: state.room,
      busy: state.planBusy,
      maxParallel: maxParallel(),
      people: state.people,
      requestHref,
      onLaunchAll: (ids) => launchStreams(ids, { all: true }),
      onLaunch: (id) => launchStreams([id]),
      onDrop: (id) => dropStream(id),
      onEdit: (id) => editStream(id),
      onAdd: () => addStream(),
      onRebuild: () => rebuildPlan(),
      onComposer: () => navigate(`#/room/${encodeURIComponent(roomId)}/composer`),
    }));
  }

  /** Включить проектный режим: открываем НАСТРОЙКИ ЭТОЙ комнаты (W17-R §2, B-9).
   *
   * Раньше вызывали `openRoomDialog(ctx)` без комнаты — открывался диалог
   * создания новой комнаты. Теперь передаём текущую комнату и раздел, к
   * которому надо прокрутить: «Проектный режим» или «Этапы проекта».
   */
  function openRoomSettings(section = 'project_mode') {
    const target = state.room && state.room.id
      ? state.room
      : { id: roomId, title: (ctx.room && ctx.room.title) || '' };
    import('./room-dialog.js')
      .then((mod) => {
        if (mod && typeof mod.openRoomDialog === 'function') {
          return mod.openRoomDialog(ctx, target, { focus: section });
        }
        toast('Настройки комнаты не открылись — попробуйте из шапки комнаты', 'warn');
        return null;
      })
      .then((saved) => {
        // После «Сохранить» карта обновляется без перезагрузки (B-9).
        if (saved) {
          state.room = Object.assign({}, state.room || {}, saved);
          state.planSig = '';
          state.heroSig = '';
          return load({ silent: true });
        }
        return null;
      })
      .catch(() => toast('Настройки комнаты не открылись — попробуйте из шапки комнаты', 'warn'));
  }

  function paintPlanOff() {
    planHost.replaceChildren(projectModeHint(openRoomSettings));
  }

  function planDelay() {
    const c = streamsSummary(state.plan);
    const active = c.active + c.proposed + c.queued;
    return active ? PLAN_FAST_MS : PLAN_SLOW_MS;
  }

  function schedulePlan() {
    if (disposed) return;
    clearTimeout(planTimer);
    planTimer = setTimeout(async () => {
      if (disposed) return;
      const hidden = globalThis.document && globalThis.document.hidden;
      if (!hidden) await loadPlan({ silent: true });
      schedulePlan();
    }, planDelay());
  }

  async function loadPlan({ silent = true } = {}) {
    if (disposed) return;
    try {
      const res = await api.get(planPath, { signal: abort.signal });
      const plan = res && res.plan && typeof res.plan === 'object' ? res.plan : null;
      state.plan = plan;
      state.planAvailable = true;
      paintPlan(!silent);
      // Карта этапов пуста, а части работы уже есть — текст пустого блока
      // зависит от этого, поэтому обновляем его вместе с картой.
      if (!state.roadmap || !(state.roadmap.phases || []).length) paintNoPhases(true);
    } catch (err) {
      if (disposed || (err && err.name === 'AbortError')) return;
      // Пока карты в комнате нет (или раздел ещё не подключён) — блока не показываем:
      // это не ошибка, о которой стоит сообщать баннером.
      const status = Number(err && err.status) || 0;
      state.plan = null;
      if (status === 404) {
        state.planAvailable = false;
        paintPlan(true);
        return;
      }
      state.planAvailable = true;
      paintPlan(true);
    }
  }

  function setPlanBusy(on) {
    state.planBusy = !!on;
    paintPlan(true);
  }

  /* ------------------------------------------------------------- действия */

  async function launchStreams(ids, opts = {}) {
    const list = (Array.isArray(ids) ? ids : []).filter(Boolean);
    if (state.planBusy || !list.length) return;
    const all = !!opts.all;
    const confirm = launchConfirm(state.plan, maxParallel(), list);
    // «Запустить всё» — про всю карту, даже если запускать пока одно часть работы.
    if (all && confirm.count > 1) confirm.title = 'Запустить все части работы?';
    const ok = await confirmDialog({
      title: confirm.title,
      text: confirm.text,
      confirmLabel: list.length > 1 ? 'Запустить всё' : 'Запустить',
      cancelLabel: 'Не сейчас',
    });
    if (!ok) return;
    setPlanBusy(true);
    try {
      const res = await api.post(planPath + '/approve', { streams: all ? 'all' : list });
      const plan = res && res.plan && typeof res.plan === 'object' ? res.plan : null;
      if (plan) state.plan = plan;
      else await loadPlan({ silent: true });
      const started = Array.isArray(res && res.started) ? res.started.length : list.length;
      toast(started === 1
        ? 'Часть работы запущено: команда начала работу'
        : `Запущено ${started}: команда начала работу`, 'ok');
      await load({ silent: true });
    } catch (err) {
      toast(humanError(err), 'danger');
      await loadPlan({ silent: true });
    } finally {
      setPlanBusy(false);
    }
  }

  async function dropStream(id) {
    if (state.planBusy) return;
    const stream = (state.plan && state.plan.streams || []).find((s) => String(s.id) === String(id));
    const name = stream && stream.title ? '«' + stream.title + '»' : 'часть работы';
    const ok = await confirmDialog({
      title: 'Отложить часть работы?',
      text: `Часть работы ${name} уйдёт из работы. Оно останется в карте — его можно запустить снова.`,
      confirmLabel: 'Отложить',
      cancelLabel: 'Оставить',
    });
    if (!ok) return;
    setPlanBusy(true);
    try {
      const res = await api.del(`${planPath}/streams/${encodeURIComponent(id)}`);
      const plan = res && res.plan && typeof res.plan === 'object' ? res.plan : null;
      if (plan) state.plan = plan;
      else await loadPlan({ silent: true });
      toast('Часть работы отложено', 'ok');
    } catch (err) {
      toast(humanError(err), 'danger');
    } finally {
      setPlanBusy(false);
    }
  }

  async function editStream(id) {
    if (state.planBusy) return;
    const stream = (state.plan && state.plan.streams || []).find((s) => String(s.id) === String(id));
    if (!stream) return;
    const values = await openStreamEditor({
      mode: 'edit',
      stream,
      members: (state.room && state.room.members) || [],
      people: state.people,
    });
    if (!values) return;
    setPlanBusy(true);
    try {
      const res = await api.patch(`${planPath}/streams/${encodeURIComponent(id)}`, values);
      const plan = res && res.plan && typeof res.plan === 'object' ? res.plan : null;
      if (plan) state.plan = plan;
      else await loadPlan({ silent: true });
      toast('Часть работы изменено', 'ok');
    } catch (err) {
      toast(humanError(err), 'danger');
      await loadPlan({ silent: true });
    } finally {
      setPlanBusy(false);
    }
  }

  async function addStream() {
    if (state.planBusy) return;
    const values = await openStreamEditor({
      mode: 'add',
      members: (state.room && state.room.members) || [],
      people: state.people,
    });
    if (!values) return;
    setPlanBusy(true);
    try {
      const res = await api.post(`${planPath}/streams`, values);
      const plan = res && res.plan && typeof res.plan === 'object' ? res.plan : null;
      if (plan) state.plan = plan;
      else await loadPlan({ silent: true });
      toast('Часть работы добавлено. Оно появится в работе после «Запустить»', 'ok');
    } catch (err) {
      toast(humanError(err), 'danger');
      await loadPlan({ silent: true });
    } finally {
      setPlanBusy(false);
    }
  }

  async function rebuildPlan() {
    if (state.planBusy) return;
    const ok = await confirmDialog({
      title: 'Собрать карту из запросов комнаты?',
      text: 'Руководитель комнаты посмотрит уже сделанные запросы и предложит карту заново. '
        + 'Ответ появится на странице нового запроса.',
      confirmLabel: 'Собрать карту',
      cancelLabel: 'Отмена',
    });
    if (!ok) return;
    setPlanBusy(true);
    try {
      const res = await api.post(`${planPath}/rebuild`, {});
      const req = res && (res.request || res);
      const rid = req && (req.id || req.root_id);
      toast('Запрос «Собрать карту» отправлен руководителю комнаты', 'ok');
      if (rid) navigate(requestHref(rid));
      else await loadPlan({ silent: true });
    } catch (err) {
      toast(humanError(err), 'danger');
    } finally {
      setPlanBusy(false);
    }
  }

  /* ------------------------------------------------------------ «где мы» */

  function renderHero() {
    const roadmap = state.roadmap || {};
    const summary = phaseSummary(roadmap);
    const current = summary.current;
    const mirror = mirrorPathOf(roadmap, state.room);

    // D9/Д3: «НИЧЕГО НЕ БЛОКИРУЕТ» + «ничего не блокирует» — одна фраза, без дубля.
    const blockerText = summary.blocked.length
      ? summary.blocked.map((p) => p.title).join(' · ')
      : 'ничего не блокирует';
    const blockerNote = summary.blocked.length
      ? summary.blocked.map((p) => p.note).filter(Boolean).join(' ')
      : '';
    const next = summary.upcoming || summary.nextAfter;

    const actions = h('div', { class: 'rm-hero__actions' },
      h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        title: 'Руководитель комнаты соберёт накопленное в STATE.md и обновит зеркало',
        onclick: () => { consolidate(); },
      }, icon('sync', { size: 13 }), 'Свести в систему'),
      mirror
        ? h('span', { class: 'rm-hero__mirror' },
          h('span', { class: 'small muted' }, 'Открыть STATE.md в хранилище'),
          pathChip(mirror))
        : null);

    const bar = h('div', { class: 'progress rm-hero__progress' },
      h('div', { class: 'progress__bar', style: { width: summary.progress.percent + '%' } }));

    return h('section', { class: 'card rm-hero' },
      h('div', { class: 'rm-hero__main' },
        h('div', { class: 'rm-hero__kicker' }, 'Где мы сейчас'),
        h('h2', { class: 'rm-hero__title' }, current ? current.title : 'Карта ещё не собрана'),
        h('div', { class: 'rm-hero__line' },
          current ? h('span', { class: 'chip chip--' + stageMeta(current.status).tone }, stageMeta(current.status).label) : null,
          h('span', { class: 'rm-hero__progress-label' }, summary.progress.label)),
        bar,
        h('div', { class: 'rm-hero__facts' },
          h('div', { class: 'rm-fact' },
            h('span', { class: 'rm-fact__label' }, summary.blocked.length ? 'Блокирует' : 'Блокировки'),
            h('span', { class: 'rm-fact__value' }, blockerText),
            blockerNote ? h('span', { class: 'rm-fact__note small muted' }, blockerNote) : null),
          h('div', { class: 'rm-fact' },
            h('span', { class: 'rm-fact__label' }, 'Что дальше'),
            h('span', { class: 'rm-fact__value' }, next ? next.title : 'все этапы пройдены'),
            next && next.note ? h('span', { class: 'rm-fact__note small muted' }, next.note) : null))),
      actions);
  }

  function paintHero(force = false) {
    const roadmap = state.roadmap || {};
    const sig = JSON.stringify([roadmap.current, roadmap.phases, roadmap.mirror_path, roadmap.updated_at]);
    if (!force && sig === state.heroSig) return;
    state.heroSig = sig;
    heroHost.replaceChildren(renderHero());
  }

  /* -------------------------------------------------------------- степпер */

  function stepNode(phase, index, currentId) {
    const meta = stageMeta(phase.status);
    const selected = state.selected === phase.id;
    const btn = h('button', {
      class: 'rm-step__btn',
      type: 'button',
      'aria-pressed': selected ? 'true' : 'false',
      'aria-current': currentId === phase.id ? 'step' : undefined,
      title: phase.title + ' — ' + meta.label,
      onclick: () => selectPhase(phase.id),
    },
    // W17-R §4: статус виден значком и цветом, номер остаётся порядком этапа.
    h('span', { class: 'rm-step__marker', 'aria-hidden': 'true' }, icon(meta.icon, { size: 11 })),
    h('span', { class: 'rm-step__body' },
      h('span', { class: 'rm-step__title' }, phase.title),
      h('span', { class: 'rm-step__state small' }, meta.label)));
    return h('li', {
      class: ['rm-step', 'rm-step--' + meta.status, selected ? 'rm-step--selected' : '',
        currentId === phase.id ? 'rm-step--current' : ''],
    }, btn);
  }

  function paintSteps(force = false) {
    const roadmap = state.roadmap || {};
    const summary = phaseSummary(roadmap);
    const sig = JSON.stringify([roadmap.phases, state.selected, summary.current && summary.current.id]);
    if (!force && sig === state.stepsSig) return;
    state.stepsSig = sig;

    const phases = Array.isArray(roadmap.phases) ? roadmap.phases : [];
    if (!phases.length) {
      stepsHost.replaceChildren();
      return;
    }
    const currentId = summary.current ? summary.current.id : null;
    // W17-R §4: подпись человеческая и одна — эти этапы задала комната,
    // их можно поправить в настройках (кнопка рядом), загадок про «один запрос» нет.
    stepsHost.replaceChildren(
      h('div', { class: 'rm-steps__head' },
        h('div', { class: 'rm-steps__title' }, 'Этапы комнаты'),
        h('div', { class: 'rm-steps__head-actions' },
          h('span', { class: 'rm-steps__note small muted' },
            'Этапы заданы для этой комнаты — их порядок и названия можно изменить.'),
          h('button', {
            class: 'btn btn--ghost btn--sm', type: 'button',
            title: 'Открыть настройки комнаты на разделе «Этапы проекта»',
            onclick: () => openRoomSettings('phases'),
          }, icon('settings-gear', { size: 13 }), 'Изменить этапы'))),
      h('ol', { class: 'rm-steps', 'aria-label': 'Этапы комнаты' },
        phases.map((phase, i) => stepNode(phase, i, currentId))));
  }

  function selectPhase(id) {
    state.selected = id;
    paintSteps(true);
    paintDetail(true);
  }

  /* ---------------------------------------------------- панель деталей */

  /**
   * Окно шага из «Карты проекта» (W14-H §12): то же окно, что на странице
   * запроса — план руководителя можно согласовать, убрать или поправить.
   * После действия бэклог комнаты перечитываем: срок/статус шага меняются.
   */
  function openStepWindow(requestId, itemId) {
    const rid = String(requestId || '').trim();
    const stepId = String(itemId || '').includes(':')
      ? String(itemId).split(':').pop() : String(itemId || '');
    if (!rid) {
      toast('У шага нет запроса-источника — открыть окно нельзя', 'warn');
      return;
    }
    openStepDialog({
      requestId: rid,
      stepId,
      room: state.room || null,
      roomId,
      api,
      toast,
      onDone: async () => {
        state.backlogLoaded = false;
        try { await load({ silent: true }); } catch { /* карта обновится по таймеру */ }
      },
    });
  }

  function renderDetail() {
    const roadmap = state.roadmap || {};
    const phases = Array.isArray(roadmap.phases) ? roadmap.phases : [];
    const phase = phases.find((p) => p.id === state.selected) || pickCurrent(roadmap);
    if (!phase) return null;

    const meta = stageMeta(phase.status);
    const source = phaseSource(phase);
    const sourceName = source
      ? sourceTitle((state.titles && state.titles[source.requestId]) || source.title, source.requestId)
      : '';
    const related = relatedDecisions(roadmap, phase.id);
    const steps = source ? relatedSteps(state.backlog, source.requestId).slice(0, 6) : [];
    const updated = updatedLabel(phase.updated_at || roadmap.updated_at);

    return h('section', { class: 'card rm-detail', dataset: { phase: phase.id } },
      h('div', { class: 'card__head' },
        h('div', { class: 'card__title' }, phase.title),
        h('div', { class: 'card__actions' }, h('span', { class: 'chip chip--' + meta.tone }, meta.label))),
      h('div', { class: 'card__body rm-detail__body' },
        phase.note
          ? h('p', { class: 'rm-detail__note' }, phase.note)
          : null,
        h('div', { class: 'rm-detail__meta small muted row row--tight row--wrap' },
          updated ? h('span', {}, updated) : null,
          // W17-R §4: без заглушек — строка про источник только когда источник есть.
          source
            ? h('span', { class: 'rm-detail__source' },
              'источник: ',
              h('a', { class: 'rm-source', href: requestHref(source.requestId), title: sourceHint(sourceName, source.requestId) },
                icon('link-external', { size: 10 }), sourceName || 'запрос'))
            : null),
        related.length
          ? h('div', { class: 'rm-detail__related' },
            h('div', { class: 'rm-detail__subtitle small muted' },
              'Связанные решения', h('span', { class: 'tnum' }, ' · ' + related.length)),
            h('ul', { class: 'rm-related-list' }, related.map((d) => h('li', { class: 'rm-related' },
              h('span', { class: 'chip chip--' + (d.status === 'accepted' ? 'ok' : 'info') },
                d.status === 'accepted' ? 'принято' : 'предложено'),
              h('span', { class: 'rm-related__text' }, d.text)))))
          : null,
        steps.length
          ? h('div', { class: 'rm-detail__related' },
            h('div', { class: 'rm-detail__subtitle small muted' },
              'Шаги из запроса', h('span', { class: 'tnum' }, ' · ' + steps.length)),
            h('ul', { class: 'rm-related-list' }, steps.map((item) => h('li', { class: 'rm-related' },
              h('span', { class: 'chip chip--' + (item.status === 'done' ? 'ok' : 'outline') },
                item.kind === 'future_question' ? 'вопрос' : 'шаг'),
              // W14-H §12: тот же принцип «план руководителя ↔ реакция пользователя»,
              // что на странице запроса: клик по шагу открывает окно действий.
              item.kind === 'future_question'
                ? h('span', { class: 'rm-related__text' }, item.text)
                : h('button', {
                  class: 'rm-related__text rm-related__step', type: 'button',
                  'aria-haspopup': 'dialog',
                  title: 'Открыть шаг: согласовать, убрать или поправить',
                  onclick: () => openStepWindow(source.requestId, item.id),
                }, item.text)))))
          : null));
  }

  function paintDetail(force = false) {
    const roadmap = state.roadmap || {};
    const phase = (roadmap.phases || []).find((p) => p.id === state.selected);
    const sig = JSON.stringify([state.selected, roadmap.phases, roadmap.decisions, roadmap.updated_at,
      phase ? phase.updated_at : null, state.backlog.length, state.titles]);
    if (!force && sig === state.detailSig) return;
    state.detailSig = sig;
    const node = renderDetail();
    if (node) detailHost.replaceChildren(node);
    else detailHost.replaceChildren();
  }

  /* --------------------------------------------------------------- решения */

  function decisionRow(d) {
    const tone = d.status === 'accepted' ? 'ok' : 'info';
    const when = lastWorkLabel(d.updated_at || d.at);
    return h('article', { class: 'rm-decision' },
      h('div', { class: 'rm-decision__top' },
        h('span', { class: 'chip chip--' + tone }, d.status === 'accepted' ? 'принято' : 'предложено'),
        h('div', { class: 'rm-decision__text' }, d.text)),
      d.why ? h('div', { class: 'rm-decision__why' },
        h('span', { class: 'rm-decision__k' }, 'почему: '), d.why) : null,
      h('div', { class: 'rm-decision__meta small muted row row--tight row--wrap' },
        d.owner ? h('span', {}, d.owner === 'mikhail' ? 'Михаил' : d.owner) : null,
        when ? h('span', {}, '· ' + when) : null,
        d.source_request
          ? h('a', { class: 'rm-source', href: requestHref(d.source_request),
            title: sourceHint((state.titles && state.titles[d.source_request]) || '', d.source_request) },
            icon('link-external', { size: 10 }), 'источник')
          : null));
  }

  const countsEl = h('div', { class: 'small muted rm-decisions__counts' }, '');
  const groupsHost = h('div', { class: 'rm-groups-host' });

  const searchInput = h('input', {
    class: 'input', type: 'search', value: state.filter.q,
    placeholder: 'Поиск по решениям', 'aria-label': 'Поиск по решениям',
  });
  searchInput.addEventListener('input', debounce(() => {
    const q = searchInput.value.trim();
    if (q === state.filter.q) return;
    state.filter.q = q;
    paintDecisions(true);
  }, 180));

  let decisionsBuilt = false;

  /** Секция «Решения» строится один раз: контролы не пересобираются при поиске. */
  function buildDecisions() {
    decisionsHost.replaceChildren(h('section', { class: 'card rm-decisions' },
      h('div', { class: 'card__head rm-decisions__head' },
        h('div', { class: 'rm-decisions__titles' },
          h('div', { class: 'card__title' }, 'Решения'),
          countsEl),
        h('div', { class: 'card__actions rm-decisions__controls' }, segmented({
          options: STATUS_OPTIONS,
          value: state.filter.status,
          onchange: (value) => {
            state.filter.status = value;
            paintDecisions(true);
          },
        }), h('div', { class: 'search-field rm-decisions__search' }, icon('search'), searchInput))),
      h('div', { class: 'card__body rm-decisions__body' }, groupsHost)));
    decisionsBuilt = true;
  }

  function paintDecisions(force = false) {
    const roadmap = state.roadmap || {};
    const sig = JSON.stringify([roadmap.decisions, state.filter]);
    if (!force && sig === state.decisionsSig) return;
    state.decisionsSig = sig;
    if (!decisionsBuilt || !decisionsHost.firstChild) buildDecisions();

    const all = Array.isArray(roadmap.decisions) ? roadmap.decisions : [];
    const grouped = decisionGroups(all, state.filter);
    countsEl.textContent = all.length
      ? grouped.counts.accepted + ' принято · ' + grouped.counts.proposed + ' предложено'
      : 'пока пусто';

    if (!all.length) {
      groupsHost.replaceChildren(h('div', { class: 'rm-decisions__empty small muted' },
        'Решений пока нет — они появятся, когда команда оформит первый ответ.'));
      return;
    }
    if (!grouped.shown) {
      groupsHost.replaceChildren(h('div', { class: 'rm-decisions__empty small muted' },
        'Ничего не нашлось. Измените фильтр или запрос.'));
      return;
    }
    groupsHost.replaceChildren(...grouped.groups.map((group) => h('div', { class: 'rm-group' },
      h('div', { class: 'rm-group__title' },
        group.title, h('span', { class: 'tnum' }, String(group.items.length))),
      h('div', { class: 'rm-group__items' }, group.items.map(decisionRow)))));
  }

  /* ---------------------------------------------------------------- пусто */

  /** Открытые шаги комнаты для блока «Что дальше» (по срокам, не больше пяти). */
  function openSteps() {
    const items = Array.isArray(state.backlog) ? state.backlog : [];
    return items
      .filter((i) => i && i.kind === 'next_step' && (i.status === 'open' || i.status === 'asked') && i.text)
      .sort((a, b) => String(a.due || '9999-12-31').localeCompare(String(b.due || '9999-12-31')))
      .slice(0, 5);
  }

  function nextStepsCard() {
    const steps = openSteps();
    if (!steps.length) return null;
    return h('section', { class: 'card rm-next' },
      h('div', { class: 'card__head' },
        h('div', { class: 'card__title' }, 'Что дальше'),
        h('div', { class: 'card__actions small muted' }, steps.length + ' открыто')),
      h('div', { class: 'card__body' },
        h('ul', { class: 'rm-next__list' }, steps.map((item) => h('li', { class: 'rm-next__item' },
          h('span', { class: 'chip chip--' + (item.status === 'asked' ? 'info' : 'outline') },
            item.status === 'asked' ? 'спрашивали' : 'открыт'),
          h('span', { class: 'rm-next__text' }, item.text),
          item.due ? h('span', { class: 'rm-next__due small muted tnum' }, item.due) : null)))));
  }

  /**
   * Один понятный блок вместо заглушек (W17-R §4).
   *
   * Проектный режим выключен → объясняем по-человечески и даём кнопку, которая
   * открывает настройки ЭТОЙ комнаты на разделе «Проектный режим» (B-9).
   * Режим включён, но этапов нет → то же самое, только про этапы.
   */
  function projectModeCard() {
    const room = state.room || {};
    const modeOn = Boolean(room.project_mode);
    const modeText = modeOn
      ? 'Сейчас этапов нет. Полоса этапов появится, когда вы зададите их сами: '
        + 'например «Идея», «Согласование», «Запуск». Команда будет отмечать, где мы стоим.'
      : 'Проектный режим выключен — руководитель отвечает одним общим ответом, '
        + 'без частей работы и без этапов. Включите, если одна задача — это целый проект: '
        + 'он разделит её на части, у каждой будет свой исполнитель и свой ход работы.';
    return h('section', { class: 'card rm-project-hint' },
      h('div', { class: 'card__head' },
        h('div', { class: 'card__title' }, modeOn ? 'Этапы проекта не заданы' : 'Проектный режим выключен')),
      h('div', { class: 'card__body' },
        h('p', { class: 'rm-project-hint__text' }, modeText),
        h('div', { class: 'rm-project-hint__actions' },
          h('button', {
            class: 'btn btn--primary btn--sm', type: 'button',
            onclick: () => openRoomSettings(modeOn ? 'phases' : 'project_mode'),
          }, modeOn ? 'Добавить этапы' : 'Включить проектный режим')),
        h('p', { class: 'rm-project-hint__note small muted' },
          'Откроются настройки этой комнаты. Больше ничего не меняется.')));
  }

  /** Карта без этапов: «Что дальше», «Решения» и один блок про режим. */
  function paintNoPhases(force = false) {
    const sig = JSON.stringify([Boolean(state.room && state.room.project_mode),
      openSteps().map((s) => String(s.id || s.text)), (state.roadmap && state.roadmap.decisions || []).length]);
    if (!force && sig === state.emptySig) return;
    state.emptySig = sig;
    heroHost.replaceChildren(...[nextStepsCard(), projectModeCard()].filter(Boolean));
    stepsHost.replaceChildren();
    detailHost.replaceChildren();
    state.stepsSig = '';
    state.detailSig = '';
    paintDecisions(true);
  }

  function paintError() {
    if (!state.error) { errorHost.replaceChildren(); return; }
    const box = errorBanner(state.error, () => load({ silent: false }));
    if (state.offline && state.roadmap) {
      box.appendChild(h('span', { class: 'small muted roadmap__offline' }, 'показаны последние полученные данные'));
    }
    errorHost.replaceChildren(box);
  }

  function paintAll({ force = false } = {}) {
    const roadmap = state.roadmap;
    paintError();
    // W17-R §4: нет объявленных этапов — компактный экран без заглушек.
    if (!roadmap || !Array.isArray(roadmap.phases) || !roadmap.phases.length) {
      paintNoPhases(true);
      return;
    }
    const ids = (roadmap.phases || []).map((p) => p.id);
    if (!state.selected || !ids.includes(state.selected)) {
      const current = pickCurrent(roadmap);
      state.selected = current ? current.id : null;
    }
    paintHero(force);
    paintSteps(force);
    paintDetail(force);
    paintDecisions(force);
  }

  /* -------------------------------------------------------------- загрузка */

  /** Названия запросов-источников этапов: id в тексте не показываем (D9/Д3). */
  async function loadSourceTitles(roadmap) {
    const phases = Array.isArray(roadmap && roadmap.phases) ? roadmap.phases : [];
    const ids = Array.from(new Set(phases
      .map((p) => String((p && (p.source_request || p.request_id)) || ''))
      .filter(Boolean))).slice(0, 6);
    const out = {};
    await Promise.all(ids.map(async (rid) => { out[rid] = await requestTitle(api, rid); }));
    return out;
  }

  async function load({ silent = false } = {}) {
    if (disposed) return;
    if (!silent) {
      heroHost.replaceChildren(skeleton(3));
      planHost.replaceChildren(skeleton(2));
      stepsHost.replaceChildren();
      detailHost.replaceChildren();
      decisionsHost.replaceChildren();
      errorHost.replaceChildren();
    }
    try {
      const roadmap = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/roadmap`, { signal: abort.signal });
      state.roadmap = roadmap || { phases: [], decisions: [], current: null };
      state.titles = await loadSourceTitles(state.roadmap);
      if (!state.backlogLoaded) {
        try {
          const bl = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/backlog?limit=200`, { signal: abort.signal });
          state.backlog = Array.isArray(bl && bl.items) ? bl.items : [];
          state.backlogLoaded = true;
          state.detailSig = '';
        } catch { /* без бэклога панель покажет только решения */ }
      }
      if (!state.room || !Array.isArray(state.room.context_files)) {
        try {
          state.room = await api.get(`/api/rooms/${encodeURIComponent(roomId)}`, { signal: abort.signal });
          state.heroSig = '';
        } catch { /* без комнаты путь зеркала может быть неизвестен */ }
      }
      // Имена для «Ответственного» в части работых («Операции», «Юрист»).
      if (!state.peopleLoaded) await loadPeople();
      state.error = null;
      state.offline = false;
      paintAll({ force: !silent });

      // «Части работы»: тихо, без баннера об ошибке — раздел может быть ещё не подключён.
      if (planBlockVisible(state.room)) {
        await loadPlan({ silent: true });
        schedulePlan();
      } else {
        clearTimeout(planTimer);
        paintPlanOff();
      }
    } catch (err) {
      if (disposed || (err && err.name === 'AbortError')) return;
      if (!silent || !state.roadmap) {
        state.error = err;
        errorHost.replaceChildren(errorBanner(err, () => load({ silent: false })));
        if (!state.roadmap) {
          heroHost.replaceChildren();
          stepsHost.replaceChildren();
          detailHost.replaceChildren();
          decisionsHost.replaceChildren();
        }
      } else {
        state.offline = true;
        paintError();
      }
    }
  }

  /* --------------------------------------------------------------- действия */

  async function consolidate() {
    if (state.busy) return;
    const ok = await confirmDialog({
      title: 'Свести проект в систему?',
      text: 'Руководитель комнаты получит запрос «обнови STATE.md»: соберёт этапы, решения и шаги в актуальный файл зеркала. Ответ появится на странице запроса.',
      confirmLabel: 'Отправить руководителю комнаты',
      cancelLabel: 'Отмена',
    });
    if (!ok) return;
    state.busy = true;
    try {
      const res = await api.post(`/api/rooms/${encodeURIComponent(roomId)}/consolidate`, {});
      const rootId = (res && (res.root_id || (res.request && res.request.id))) || null;
      toast(rootId ? 'Запрос руководителю комнаты создан: обновит STATE.md' : 'Запрос руководителю комнаты создан', 'ok');
      await load({ silent: true });
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось создать запрос', 'danger');
    } finally {
      state.busy = false;
    }
  }

  /* -------------------------------------------------------- жизненный цикл */

  await load({ silent: false });

  poller = pollApi(() => load({ silent: true }), POLL_MS, {
    pauseHidden: true,
    immediate: false,
    onError: () => { /* ошибки показываются баннером */ },
  });

  return {
    unmount() {
      disposed = true;
      abort.abort();
      clearTimeout(planTimer);
      if (poller && typeof poller.stop === 'function') poller.stop();
      root.remove();
    },
  };
}
