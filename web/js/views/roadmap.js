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
// W20-FC §1.7 (F-2): этапы предлагает руководитель — плашка решения и один
// редактор на карту, «От вас нужно» и настройки комнаты.
import {
  createProposalBanner, createProposalWaiting, createProposeButton,
  openPhasesEditor, proposalOf,
} from './phases-editor.js';
// W20-U2 §16 (P12): один блок «Что дальше» и одна карточка пустой карты —
// расчёт вынесен в чистый модуль (tests/roadmap-next.test.mjs).
import { nextBlock, emptyPlan, legendItems, stepsCountLabel } from './roadmap-next.js';

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
    nextSig: '',
    detailSig: '',
    decisionsSig: '',
    // W20-FC §1.7: плашка предложения этапов и «ждём ответа».
    proposalSig: '',
    proposeSent: false,
    proposeRoot: '',
  };
  let disposed = false;
  let poller = null;
  let planTimer = null;
  const abort = new AbortController();

  /**
   * Комнату сохранили в настройках — этапы и предложение перечитываем сразу,
   * не дожидаясь опроса (W20-FC §1.7: правки видны на карте сразу).
   */
  const onRoomSaved = (ev) => {
    const detail = (ev && ev.detail) || {};
    if (detail.room) state.room = Object.assign({}, state.room || {}, detail.room);
    if (String(detail.id || '') !== roomId) return;
    state.heroSig = '';
    state.stepsSig = '';
    state.nextSig = '';
    state.detailSig = '';
    state.emptySig = '';
    state.proposalSig = '';
    load({ silent: true });
  };
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('teamroom:room-saved', onRoomSaved);
  }

  /* --------------------------------------------------------------- каркас */

  const errorHost = h('div', { class: 'roadmap__error' });
  const planHost = h('div', { class: 'roadmap__plan-block' });
  // Плашка «Руководитель предлагает этапы» — над «Где мы сейчас»: это решение
  // человека, а не состояние проекта (W20 §1.7).
  const proposalHost = h('div', { class: 'roadmap__proposal-block' });
  const heroHost = h('div', { class: 'roadmap__hero-block' });
  const stepsHost = h('div', { class: 'roadmap__steps-block' });
  // W20-U2 §16 (P12): единственное место «что дальше» — ниже этапов, до панели
  // деталей. Раньше блок был и фактом в «Где мы сейчас», и отдельной карточкой.
  const nextHost = h('div', { class: 'roadmap__next-block' });
  const detailHost = h('div', { class: 'roadmap__detail-block' });
  const decisionsHost = h('div', { class: 'roadmap__decisions-block' });
  const root = h('div', { class: 'view__inner roadmap' },
    errorHost, planHost, proposalHost, heroHost, stepsHost, nextHost, detailHost, decisionsHost);
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

  /* --------------------------------------- предложение этапов (W20 §1.7) */

  /**
   * Плашка «Руководитель предлагает этапы»: принять все, принять выбранные,
   * изменить или отклонить. Показывается, пока предложение ждёт решения, и на
   * карте с этапами, и на карте без них.
   */
  function paintProposal(force = false) {
    const roadmap = state.roadmap || {};
    const proposal = proposalOf(roadmap.phases_proposal);
    const pending = Boolean(proposal && proposal.state === 'proposed');
    const sig = JSON.stringify([pending ? proposal : null, state.proposeSent]);
    if (!force && sig === state.proposalSig) return;
    state.proposalSig = sig;
    if (pending) {
      proposalHost.replaceChildren(createProposalBanner({
        proposal,
        roomId,
        api,
        toast,
        onEdit: () => { openPhases(); },
        onDecided: (res) => { afterProposal(res); },
      }));
      return;
    }
    if (state.proposeSent) {
      proposalHost.replaceChildren(createProposalWaiting({
        rootId: state.proposeRoot,
        onOpen: (rid) => navigate(requestHref(rid)),
      }));
      return;
    }
    proposalHost.replaceChildren();
  }

  /** После решения: комната и карта перечитываются — этапы видны сразу. */
  async function afterProposal(res) {
    if (res && res.room) state.room = Object.assign({}, state.room || {}, res.room);
    state.proposalSig = '';
    state.proposeSent = false;
    state.heroSig = '';
    state.stepsSig = '';
    state.nextSig = '';
    state.emptySig = '';
    state.detailSig = '';
    await load({ silent: true });
  }

  /**
   * Редактор этапов — один на всё приложение (W20 §1.7): добавление,
   * переименование, порядок стрелками, удаление, предложение руководителя.
   * Сохраняется свойством комнаты, поэтому карта обновляется сразу.
   */
  async function openPhases() {
    const roadmap = state.roadmap || {};
    const saved = await openPhasesEditor({
      api, roomId, toast,
      phases: (Array.isArray(roadmap.phases) ? roadmap.phases : [])
        .map((p) => ({ id: p.id, title: p.title })),
      proposal: roadmap.phases_proposal,
    });
    if (!saved) return;
    if (saved.room) state.room = Object.assign({}, state.room || {}, saved.room);
    state.proposalSig = '';
    state.proposeSent = false;
    state.heroSig = '';
    state.stepsSig = '';
    state.nextSig = '';
    state.emptySig = '';
    state.detailSig = '';
    await load({ silent: true });
  }

  /** «Предложить этапы»: с карточкой — на её страницу, иначе ждём здесь. */
  function afterProposeSent(res) {
    if (res && res.root_id) {
      navigate(requestHref(res.root_id));
      return;
    }
    state.proposeSent = Boolean(res && res.ok);
    state.proposeRoot = '';
    paintProposal(true);
    load({ silent: true });
  }

  /** Кнопка «Предложить этапы» — одна на карту и на пустое состояние. */
  function proposeButton() {
    return createProposeButton({ api, roomId, toast, onSent: afterProposeSent });
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
      // Карта этапов пуста — карточка «Что дальше» пересобирается вместе с данными.
      if (!state.roadmap || !(state.roadmap.phases || []).length) paintNext(true);
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
      title: 'Собрать карту из задач комнаты?',
      text: 'Руководитель комнаты посмотрит уже сделанные задачи и предложит карту заново. '
        + 'Ответ появится на странице новой задачи.',
      confirmLabel: 'Собрать карту',
      cancelLabel: 'Отмена',
    });
    if (!ok) return;
    setPlanBusy(true);
    try {
      const res = await api.post(`${planPath}/rebuild`, {});
      const req = res && (res.request || res);
      const rid = req && (req.id || req.root_id);
      toast('Задача «Собрать карту» отправлена руководителю комнаты', 'ok');
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
            blockerNote ? h('span', { class: 'rm-fact__note small muted' }, blockerNote) : null))),
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
      'data-phase': String(phase.id),
      // W20-U2 §16: видно, что этап раскрывает панель ниже, а не редактор этапов.
      'aria-expanded': selected ? 'true' : 'false',
      'aria-current': currentId === phase.id ? 'step' : undefined,
      title: phase.title + ' — ' + meta.label + '. Нажмите, чтобы увидеть, что в этом этапе.',
      onclick: () => selectPhase(phase.id),
    },
    // W17-R §4: статус виден значком и цветом, номер остаётся порядком этапа.
    h('span', { class: 'rm-step__marker', 'aria-hidden': 'true' }, icon(meta.icon, { size: 11 })),
    h('span', { class: 'rm-step__body' },
      // Полное название в подсказке: на узком шаге подпись обрезается тремя строками.
      h('span', { class: 'rm-step__title', title: phase.title }, phase.title),
      h('span', { class: 'rm-step__state small' }, meta.label)),
    h('span', { class: 'rm-step__chevron', 'aria-hidden': 'true' }, icon('chevron-right', { size: 12 })));
    return h('li', {
      class: ['rm-step', 'rm-step--' + meta.status, selected ? 'rm-step--selected' : '',
        currentId === phase.id ? 'rm-step--current' : ''],
      dataset: { phase: String(phase.id) },
    }, btn);
  }

  /** Подпись статусов под полосой: цвет без слова ничего не значит (P12). */
  function stepLegend() {
    return h('div', { class: 'rm-legend', role: 'list', 'aria-label': 'Обозначения статусов этапов' },
      legendItems().map((item) => h('span', { class: 'rm-legend__item rm-legend__item--' + item.status, role: 'listitem' },
        h('span', { class: 'rm-legend__icon', 'aria-hidden': 'true' }, icon(item.icon, { size: 11 })),
        item.label)));
  }

  function paintSteps(force = false) {
    const roadmap = state.roadmap || {};
    const summary = phaseSummary(roadmap);
    const pendingProposal = Boolean((proposalOf(roadmap.phases_proposal) || {}).state === 'proposed');
    const sig = JSON.stringify([roadmap.phases, state.selected,
      summary.current && summary.current.id, pendingProposal]);
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
          // Пока предложение ждёт решения, вторую карточку не ставим (409):
          // кнопку не показываем, решения есть на плашке выше.
          pendingProposal ? null : proposeButton(),
          h('button', {
            class: 'btn btn--ghost btn--sm', type: 'button',
            title: 'Редактор этапов: добавить, переименовать, переставить, убрать',
            onclick: () => { openPhases(); },
          }, icon('edit', { size: 13 }), 'Изменить этапы'))),
      h('ol', { class: 'rm-steps', 'aria-label': 'Этапы комнаты' },
        phases.map((phase, i) => stepNode(phase, i, currentId))),
      h('div', { class: 'rm-steps__foot' },
        stepLegend(),
        h('span', { class: 'rm-steps__hint small muted' },
          'Нажмите на этап — ниже будет видно, что в нём и что дальше.')));
  }

  function selectPhase(id) {
    state.selected = id;
    state.nextSig = '';
    paintSteps(true);
    paintDetail(true);
    // На узком экране панель этапа уходит под полосу — подводим её к глазам.
    if (detailHost.firstChild && typeof detailHost.scrollIntoView === 'function') {
      try { detailHost.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch { /* ignore */ }
    }
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
      toast('У шага нет задачи-источника — открыть окно нельзя', 'warn');
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
                icon('link-external', { size: 10 }), sourceName || 'задача'))
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
              'Шаги из задачи', h('span', { class: 'tnum' }, ' · ' + steps.length)),
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
  let decisionsControls = null;

  /** Секция «Решения» строится один раз: контролы не пересобираются при поиске. */
  function buildDecisions() {
    decisionsControls = h('div', { class: 'card__actions rm-decisions__controls' }, segmented({
      options: STATUS_OPTIONS,
      value: state.filter.status,
      onchange: (value) => {
        state.filter.status = value;
        paintDecisions(true);
      },
    }), h('div', { class: 'search-field rm-decisions__search' }, icon('search'), searchInput));
    decisionsHost.replaceChildren(h('section', { class: 'card rm-decisions' },
      h('div', { class: 'card__head rm-decisions__head' },
        h('div', { class: 'rm-decisions__titles' },
          h('div', { class: 'card__title' }, 'Решения'),
          countsEl),
        decisionsControls),
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
    // W20-U2 §16: пустой список не показывает бесполезные фильтры и поиск.
    if (decisionsControls) decisionsControls.classList.toggle('hidden', !all.length);

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

  /* ------------------------------------------------------- «Что дальше» */

  /** Строка шага: «открыт/спрашивали», текст, срок. */
  function nextStepItem(item) {
    return h('li', { class: 'rm-next__item', dataset: { key: String(item.id || '') } },
      h('span', { class: 'chip chip--' + (item.status === 'asked' ? 'info' : 'outline') },
        item.status === 'asked' ? 'спрашивали' : 'открыт'),
      h('span', { class: 'rm-next__text', title: item.text }, item.text),
      item.due ? h('span', { class: 'rm-next__due small muted tnum' }, item.due) : null);
  }

  /** Список шагов с подписью-разделителем (второго заголовка «Что дальше» нет). */
  function nextStepsList(steps, subtitle) {
    return h('div', { class: 'rm-next__steps' },
      subtitle ? h('div', { class: 'rm-next__subtitle small muted' }, subtitle) : null,
      h('ul', { class: 'rm-next__list' }, steps.map(nextStepItem)));
  }

  /**
   * W29-P6: «Собрать этапы по выполненным запросам» — одна дешёвая проверка по
   * названиям и итогам готовых запросов (без запуска модели). Результат — обычное
   * предложение этапов комнаты: принять/поправить/отклонить, и пункт в «От вас нужно».
   */
  function buildPhasesButton(label) {
    const btn = h('button', {
      class: 'btn btn--primary btn--sm', type: 'button',
      title: 'Собрать этапы карты по завершённым задачам комнаты — без запуска модели',
      onclick: async () => {
        btn.disabled = true;
        try {
          const res = await api.post('/api/rooms/' + encodeURIComponent(roomId) + '/phases/from-requests', {});
          toast(String((res && res.message) || 'Этапы предложены — примите или поправьте'), 'ok');
          state.proposalSig = '';
          state.emptySig = '';
          await load({ silent: true });
        } catch (err) {
          toast('Этапы не собрались: ' + String((err && err.message) || 'сервер не ответил'), 'warn');
        } finally {
          btn.disabled = false;
        }
      },
    }, icon('map', { size: 13 }), label || 'Собрать этапы по выполненным задачам');
    return btn;
  }

  /** Действие пустой карты: редактор этапов, предложение руководителю или режим. */
  function nextAction(key, label, primary = true) {
    if (key === 'build') return buildPhasesButton(label);
    if (key === 'propose') return proposeButton();
    if (key === 'mode') {
      return h('button', {
        class: 'btn btn--primary btn--sm', type: 'button',
        onclick: () => openRoomSettings('project_mode'),
      }, label);
    }
    return h('button', {
      class: (primary ? 'btn btn--primary' : 'btn btn--secondary') + ' btn--sm', type: 'button',
      title: 'Редактор этапов: добавить, переименовать, переставить, убрать',
      onclick: () => { openPhases(); },
    }, icon('edit', { size: 13 }), label);
  }

  /**
   * Один блок «Что дальше» (W20-U2 §16, P12).
   *
   * Заменяет и факт «Что дальше» в «Где мы сейчас», и вторую карточку «Что
   * дальше» на пустой карте: следующий этап, его пояснение и ближайшие шаги
   * живут в одном месте. Когда этапов нет — здесь же объяснение и действия,
   * а не три отдельные заглушки.
   */
  function renderNext() {
    const roadmap = state.roadmap || {};
    const phases = Array.isArray(roadmap.phases) ? roadmap.phases : [];
    const modeOn = Boolean((state.room || {}).project_mode);
    const proposalPending = (proposalOf(roadmap.phases_proposal) || {}).state === 'proposed';
    const plan = emptyPlan({ modeOn, proposalPending });
    const block = nextBlock(roadmap, state.backlog, { limit: 4 });
    const body = h('div', { class: 'card__body rm-next__body' });

    if (!phases.length) {
      // Пустая карта: одно объяснение + два действия (+ шаги, если они уже есть).
      body.appendChild(h('p', { class: 'rm-next__text rm-next__text--lead' },
        h('strong', { class: 'rm-next__state' }, plan.title + '. '), plan.text));
      body.appendChild(h('div', { class: 'rm-next__actions' },
        nextAction(plan.primary.key, plan.primary.label),
        plan.secondary ? nextAction(plan.secondary.key, plan.secondary.label, false) : null));
      if (plan.note) body.appendChild(h('p', { class: 'rm-next__note small muted' }, plan.note));
      if (block.steps.length) body.appendChild(nextStepsList(block.steps, 'Шаги из задач'));
      return h('section', { class: 'card rm-next rm-next--empty' },
        h('div', { class: 'card__head' },
          h('div', { class: 'card__title' }, 'Что дальше'),
          h('div', { class: 'card__actions small muted' }, block.steps.length
            ? stepsCountLabel(block.steps.length) : 'этапов нет')),
        body);
    }

    if (block.phase) {
      const meta = stageMeta(block.phase.status);
      body.appendChild(h('div', { class: 'rm-next__phase' },
        h('span', { class: 'rm-next__phase-icon rm-tone--' + meta.tone, 'aria-hidden': 'true' },
          icon(meta.icon, { size: 14 })),
        h('div', { class: 'rm-next__phase-main' },
          h('div', { class: 'rm-next__phase-title', title: block.phase.title }, block.phase.title),
          h('div', { class: 'rm-next__phase-line small muted' }, meta.label,
            block.note ? h('span', {}, ' · ' + block.note) : null)),
        h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button',
          title: 'Открыть этот этап в полосе этапов',
          onclick: () => selectPhase(block.phase.id),
        }, 'Открыть этап', icon('arrow-right', { size: 12 }))));
    } else if (block.allDone) {
      body.appendChild(h('p', { class: 'rm-next__text' },
        'Все этапы пройдены — карта закрыта. Если появился новый этап, добавьте его в «Изменить этапы».'));
    }

    if (block.steps.length) {
      body.appendChild(nextStepsList(block.steps, block.phase ? 'Ближайшие шаги' : ''));
    } else if (!block.allDone) {
      body.appendChild(h('p', { class: 'rm-next__text small muted' },
        'Открытых шагов нет — ждём, когда появится следующий.'));
    }

    return h('section', { class: 'card rm-next' },
      h('div', { class: 'card__head' },
        h('div', { class: 'card__title' }, 'Что дальше'),
        h('div', { class: 'card__actions small muted' }, stepsCountLabel(block.steps.length))),
      body);
  }

  function paintNext(force = false) {
    const roadmap = state.roadmap || {};
    const sig = JSON.stringify([
      roadmap.phases, roadmap.current, state.selected, Boolean((state.room || {}).project_mode),
      (proposalOf(roadmap.phases_proposal) || {}).state || '',
      (state.backlog || []).filter((i) => i && i.kind === 'next_step'
        && (i.status === 'open' || i.status === 'asked')).map((i) => [i.id, i.due, i.text]).slice(0, 8),
    ]);
    if (!force && sig === state.nextSig) return;
    state.nextSig = sig;
    nextHost.replaceChildren(renderNext());
  }

  /** Карта без этапов: тот же блок «Что дальше» и «Решения», без заглушек. */
  function paintNextOnly(force = false) {
    const proposalState = (proposalOf(state.roadmap && state.roadmap.phases_proposal) || {}).state || '';
    const sig = JSON.stringify([Boolean(state.room && state.room.project_mode), proposalState,
      (state.backlog || []).map((s) => String(s.id || s.text)), (state.roadmap && state.roadmap.decisions || []).length]);
    if (!force && sig === state.emptySig) return;
    state.emptySig = sig;
    heroHost.replaceChildren();
    stepsHost.replaceChildren();
    detailHost.replaceChildren();
    state.stepsSig = '';
    state.detailSig = '';
    paintProposal();
    paintNext(true);
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
    // W20 §1.7: предложение этапов видно и на карте без этапов — плашка выше.
    paintProposal(force);
    // W20-U2 §16: этапов нет — та же карточка «Что дальше» объясняет почему.
    if (!roadmap || !Array.isArray(roadmap.phases) || !roadmap.phases.length) {
      paintNextOnly(true);
      return;
    }
    const ids = (roadmap.phases || []).map((p) => p.id);
    if (!state.selected || !ids.includes(state.selected)) {
      const current = pickCurrent(roadmap);
      state.selected = current ? current.id : null;
    }
    paintHero(force);
    paintSteps(force);
    paintNext(force);
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
      nextHost.replaceChildren(skeleton(2));
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
          nextHost.replaceChildren();
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
      text: 'Руководитель комнаты получит задачу «обнови STATE.md»: соберёт этапы, решения и шаги в актуальный файл зеркала. Ответ появится на странице задачи.',
      confirmLabel: 'Отправить руководителю комнаты',
      cancelLabel: 'Отмена',
    });
    if (!ok) return;
    state.busy = true;
    try {
      const res = await api.post(`/api/rooms/${encodeURIComponent(roomId)}/consolidate`, {});
      const rootId = (res && (res.root_id || (res.request && res.request.id))) || null;
      toast(rootId ? 'Задача руководителю комнаты создана: обновит STATE.md' : 'Задача руководителю комнаты создана', 'ok');
      await load({ silent: true });
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось создать задачу', 'danger');
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
      if (typeof window !== 'undefined' && typeof window.removeEventListener === 'function') {
        window.removeEventListener('teamroom:room-saved', onRoomSaved);
      }
      abort.abort();
      clearTimeout(planTimer);
      if (poller && typeof poller.stop === 'function') poller.stop();
      root.remove();
    },
  };
}
