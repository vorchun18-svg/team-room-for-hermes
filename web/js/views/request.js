/**
 * request.js — страница запроса #/room/<id>/request/<root>.
 *
 * Верх страницы: шапка, этапы, предупреждения, крупный ответ с версиями,
 * проверка критика, встроенный отчёт, дерево работ, артефакты, хронология.
 * Справа — «От вас нужно»: шаги, вопросы к Михаилу, решения, будущие вопросы, фаза.
 *
 * Файл — каркас (данные, опрос, действия, сборка страницы); шапка и меню — в
 * request-header.js, ответ и версии — в request-answer.js, колонка «От вас нужно» —
 * в request-aside.js, общие помощники — в request-util.js.
 *
 * Блоки (2)(3)(5)(7)(8)(9)(10) приходят из соседних модулей
 * (request-parts.js, report-embed.js) через динамический import: пока файла
 * нет, страница работает без них. Своих копий этих блоков здесь нет.
 *
 * Данные — только через ctx.api. Живое обновление: ETag + сравнение JSON,
 * полная перерисовка только при изменении данных, ввод и фокус сохраняются.
 */

import { h, fromCount } from '../util.js';
import { icon } from '../icons.js';
import { poll as apiPoll } from '../api.js';
import { applyLivePatch, captureSnapshot, isEditing, stateSignature } from '../live-patch.js';
import { skeleton, confirmDialog, ensureCss } from '../ui.js';
import { firstLine, synthesisOf, versionsOf, latestVersion, safePart, requestErrorBanner, errorToast } from './request-util.js';
import { ensureProfileDirectory } from '../participant-label.js';
import { createHeader } from './request-header.js';
import { createAnswer } from './request-answer.js';
import { createAside } from './request-aside.js';
import { createApprovals } from './approvals.js';
// W26-D2: один стор запроса — «одно действие — везде» (основной блок, колонка, лист).
import { requestStoreFor } from './request-store.js';
// W28-D7: замки действий и предел ожидания — один общий модуль (store.js).
import { withTimeout, locked, lockTimeout } from '../store.js';
import { mountThread } from './request-thread.js';
import { mountWorklog } from './request-worklog.js';
import { createSection, sectionDefaults } from './section-collapse.js';
import { isBrief, buildPlanCard } from './plan-req.js';
import { briefCrumbItems, requestTitle } from '../scope.js';
// W25-L1 (R5 §7.6): липкая панель действий свода — одна строка 40 px вверху.
import { buildSvodBar } from '../shell-svod-bar.js';
// W25-L2 (R5 §7.4): «Фокус» — свой режим внутри страницы; (R5 §7.9) плотность.
import { installFocusMode, buildFocusBar } from '../focus-mode.js';
import { installDensity } from '../density.js';
import { isTypingTarget } from '../hotkeys.js';
// W27-E3: якорь шага `?step=ns_…` — раскрыть и подсветить шаг, к которому пришли.
import { focusStepWhenReady, stepFromRoute } from './step-focus.js';

const FINAL = new Set(['done', 'failed']);
// WAVE17-P4: опрос реже, чем раньше (было 2 с): 304 по ETag дешёвый, но и он
// не нужен чаще, чем человек читает экран. Завершённый запрос — совсем редко.
const POLL_ACTIVE = 5000;
const POLL_IDLE = 30000;
// Отложенное обновление: пока печатают/диктуют, проверяем не чаще раза в 0,7 с.
const DEFER_CHECK_MS = 700;

/**
 * Подпись данных для сравнения «изменилось ли что-то на экране».
 * Реализация — в live-patch.js (`stateSignature`): heartbeat не считается
 * изменением, иначе страница перерисовывалась бы каждую минуту впустую.
 */
const signatureOf = stateSignature;

/* ──────────────────────────────────────────────────────────────── mount */

export async function mount(el, ctx) {
  await ensureCss('css/view-request.css');
  // W25-L2: поведение блоков, плотность и «Фокус» (R5 §7.2, §7.4, §7.9).
  await ensureCss('css/view-blocks.css');
  // Стили диалога нужны до первой отрисовки: иначе лента и поле ввода
  // появляются без своих размеров и прыгают при подключении стилей.
  await ensureCss('css/view-thread.css');
  // Проектный режим: плашка «Предложенная карта», крошка брифа, кнопка папки.
  await ensureCss('css/plan-req.css');

  const rid = String(ctx.route?.id || ctx.params?.id || '');
  let roomId = String(ctx.route?.roomId || ctx.room?.id || '');

  const root = h('div', { class: 'req' });
  el.replaceChildren(root);

  let detail = null;
  let error = null;
  let lastSig = '';
  let lastFetchAt = 0;
  let busy = false;
  let disposed = false;
  let poller = null;
  let parts = null;
  let reportEmbed = null;
  let partsCheckedAt = 0;
  let backlog = null; // Map<item_id, BacklogItem> | null
  let threadCtl = null; // { update, focus, destroy } — блок «Диалог по запросу» (монтируется один раз)
  let worklogCtl = null; // { update, destroy } — блок «Ход работы» (тоже монтируется один раз)
  let room = null; // деталь комнаты: project_dir, title (для кнопки папки и крошки)
  let plan = null; // карта проекта комнаты (GET /api/rooms/{r}/plan)
  let planError = null;
  let planAt = 0;
  let launching = false; // идёт «Запустить всё»
  let briefTitle = ''; // название брифа — для крошки под-запроса

  const state = { version: null, askOpen: false, pipeOpen: false };
  const keepValues = new Map(); // data-keep → введённый текст
  const listeners = [];

  /* ── W25-L2: режим вида, «Фокус», плотность (R5 §7.4, §7.7, §7.9) ─────────
   *
   * Всё это ЧИТАЕТ контракт L1 (`data-layout`, `data-density`, `data-focus` на
   * `#app`) и ничего в оболочке не переписывает. Режим нужен здесь, потому что
   * умолчания блоков зависят от ширины вида (R5 §7.2), а ширина меняется без
   * перезагрузки: блоки пересчитываются по событию `teamroom:layout`.
   */
  const appEl = (typeof document !== 'undefined' && document.getElementById) ? document.getElementById('app') : null;
  /** Текущий режим вида: единственный источник — `data-layout` (layout-mode.js). */
  function layoutMode() {
    const live = (typeof document !== 'undefined' && document.getElementById) ? document.getElementById('app') : null;
    return String((live && live.dataset && live.dataset.layout) || 'm');
  }
  const focus = installFocusMode({
    app: appEl,
    route: { name: 'request', roomId: roomId, id: rid },
  });
  const density = installDensity({ app: appEl });
  /** Блоки этой страницы: `key → api createSection` (пересчёт умолчаний). */
  const sections = new Map();
  // WAVE17-P4: обновление, отложенное на время набора текста или диктовки.
  let pendingRender = false;
  let deferTimer = null;
  let deferredSince = 0;

  /* ── модули-соседи ─────────────────────────────────────────── */

  async function importIfExists(rel) {
    try {
      return await import(new URL(rel, import.meta.url).href);
    } catch (err) {
      // Файла ещё нет — страница работает без этого блока.
      console.warn('[request] модуль недоступен:', rel, err);
      return null;
    }
  }

  async function ensureParts() {
    if (parts && reportEmbed) return;
    const now = Date.now();
    if (partsCheckedAt && now - partsCheckedAt < 20000) return;
    partsCheckedAt = now;
    const [p, r] = await Promise.all([
      parts ? parts : importIfExists('./request-parts.js'),
      reportEmbed ? reportEmbed : importIfExists('./report-embed.js'),
    ]);
    if (p) parts = p;
    if (r) reportEmbed = r;
  }

  /* ── данные ───────────────────────────────────────────────── */

  async function loadBacklog() {
    if (!roomId) return;
    try {
      const res = await ctx.api.get(`/api/rooms/${encodeURIComponent(roomId)}/backlog`);
      const items = Array.isArray(res?.items) ? res.items : [];
      backlog = new Map(items.map((i) => [String(i.id), i]));
    } catch {
      backlog = null;
    }
  }

  /** Деталь комнаты: нужна папка проекта и название для крошки. Один раз на страницу. */
  async function loadRoom() {
    if (room || !roomId) return;
    try {
      room = await ctx.api.get(`/api/rooms/${encodeURIComponent(roomId)}`);
    } catch {
      room = null; // живём без папки проекта: кнопку не покажем
    }
  }

  /**
   * Карта проекта комнаты. Нет карты — сервер отвечает `{plan:null}`,
   * это не ошибка: плашка скажет «карты пока нет».
   */
  async function loadPlan(opts = {}) {
    if (!roomId) return;
    const { force = false } = opts;
    // Опрос не должен дёргать файл плана чаще раза в 5 секунд.
    if (!force && planAt && Date.now() - planAt < 5000) return;
    planAt = Date.now();
    try {
      const res = await ctx.api.get(`/api/rooms/${encodeURIComponent(roomId)}/plan`);
      plan = res && res.plan ? res.plan : (res && Array.isArray(res.streams) ? res : null);
      planError = null;
    } catch (err) {
      planError = err;
    }
  }

  /**
   * `[` / `]` — предыдущий / следующий запрос в комнате (R5 §7.8).
   *
   * Список берём из данных комнаты (`GET /api/rooms/{id}` → `requests`), которые
   * страница и так загружает ради папки проекта: отдельного запроса клавиша не
   * делает. Порядок — как в комнате (свежие сверху); на границе списка не
   * перескакиваем: «предыдущий» с первого и «следующий» с последнего ничего не
   * делают, чтобы не уводить человека в неожиданный запрос.
   *
   * @param {number} delta −1 предыдущий, +1 следующий
   * @returns {boolean} был ли переход
   */
  function stepRequest(delta) {
    const list = Array.isArray(room && room.requests) ? room.requests : [];
    const ids = list.map((r) => String((r && (r.id || r.root_id)) || '')).filter(Boolean);
    if (!ids.length) return false;
    const i = ids.indexOf(String(rid));
    if (i < 0) return false;
    const next = ids[i + (Number(delta) < 0 ? -1 : 1)];
    if (!next || next === rid) return false;
    if (typeof ctx.navigate === 'function') {
      ctx.navigate(`#/room/${encodeURIComponent(roomId)}/request/${encodeURIComponent(next)}`);
      return true;
    }
    return false;
  }

  /** «Запустить всё»: очередь направлений, дальше их ведёт сервер. */
  async function launchAll() {
    if (launching || !roomId) return;
    launching = true;
    render();
    try {
      await ctx.api.post(`/api/rooms/${encodeURIComponent(roomId)}/plan/approve`, { streams: 'all' });
      ctx.toast('Направления запущены — пойдут по очереди', 'ok');
      await loadPlan({ force: true });
      await refresh({ force: true, quiet: true });
    } catch (err) {
      ctx.toast((err && err.message) || 'Не удалось запустить направления', 'danger');
    } finally {
      launching = false;
      render();
    }
  }

  async function refresh(opts = {}) {
    if (disposed) return;
    const { force = false, initial = false, quiet = false, fromPoll = false } = opts;
    let res;
    try {
      res = await ctx.api.get(
        `/api/requests/${encodeURIComponent(rid)}`,
        // Без детали (первая отрисовка) условный GET недопустим: ETag мог
        // остаться от прошлого визита, а 304 не даёт тела для render().
        (force || initial || !detail) ? undefined : { etag: true },
      );
    } catch (err) {
      if (initial || !detail) { error = err; render(); }
      else if (!quiet) errorToast(ctx, err, 'Не удалось обновить запрос');
      return;
    }
    lastFetchAt = Date.now();
    if (!res || res.notModified) return;

    const sig = signatureOf(res);
    if (sig === lastSig) return;
    lastSig = sig;
    const prev = detail;
    detail = res;
    // W26-D2: сервер — единственный источник истины; стор получает ту же деталь,
    // что и страница (без лишнего события: страница и так перерисовывается).
    try { store.setDetail(res, { source: 'page', quiet: true, force: false }); } catch { /* старый ответ */ }
    error = null;
    if (!roomId && detail.room) roomId = String(detail.room);

    // Пользователь листал последнюю версию — остаёмся на последней.
    const s = synthesisOf(detail);
    const latest = latestVersion(s);
    if (state.version == null || state.version > latest) state.version = null;
    if (prev && typeof state.version === 'number' && !versionsOf(s).some((v) => v.version === state.version)) {
      state.version = null;
    }

    if (initial || force) {
      await loadBacklog();
      await loadRoom();
      // WAVE13 §11: подписи участников в дереве и чипах — из общего справочника
      // (один запрос на страницу, из памяти; без него покажем роль, как раньше).
      await ensureProfileDirectory(ctx.api);
    }
    // Брифу нужна карта для плашки, направлению — для крошки «Бриф › Направление».
    if (isBrief(detail) || (detail && detail.plan_link)) await loadPlan({ force: initial || force });
    // Название брифа для крошки: сервер отдаёт только id брифа, название — из запроса.
    const briefRoot = (detail && detail.plan_link && (detail.plan_link.brief_root_id
      || (plan && plan.brief_root_id))) || '';
    if (briefRoot && !briefTitle) {
      requestTitle(ctx.api, briefRoot).then((title) => {
        if (title && title !== briefTitle && !disposed) { briefTitle = title; render(); }
      }).catch(() => { /* крошка останется без названия — это не мешает */ });
    }
    // Опрос не перебивает набор текста и диктовку: обновимся, когда закончат.
    render({ deferIfEditing: fromPoll });
  }

  async function tick() {
    if (disposed || busy) return;
    const before = Boolean(parts || reportEmbed);
    await ensureParts();
    if (!before && (parts || reportEmbed)) render({ deferIfEditing: true });
    const active = detail ? !FINAL.has(detail.status) : true;
    // Завершённый запрос нечего опрашивать каждые 5 секунд — реже.
    if (poller && typeof poller.setDelay === 'function') poller.setDelay(active ? POLL_ACTIVE : POLL_IDLE);
    if (!active && Date.now() - lastFetchAt < POLL_IDLE) return;
    await refresh({ quiet: true, fromPoll: true });
  }

  /* ── действия ─────────────────────────────────────────────── */

  async function runAction(action, body, opts = {}) {
    // W28-D7: busy не «глотает» нажатие молча и не остаётся поднятым навсегда.
    // Раньше нажатие при busy просто возвращало null (кнопка выглядела живой,
    // но не отвечала), а зависший запрос держал busy до перезагрузки страницы.
    if (busy) {
      ctx.toast('Предыдущее действие ещё выполняется — подождите пару секунд', 'warn');
      return null;
    }
    busy = true;
    try {
      const res = await withTimeout(
        ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, { action, ...(body || {}) }),
        lockTimeout(), 'Действие по запросу');
      if (opts.toast) ctx.toast(opts.toast, opts.kind || 'ok');
      await refresh({ force: true });
      return res;
    } catch (err) {
      ctx.toast(err?.message || 'Действие не выполнено', 'danger');
      return null;
    } finally {
      busy = false;
    }
  }

  async function cloneRequest() {
    if (busy) { ctx.toast('Предыдущее действие ещё выполняется — подождите пару секунд', 'warn'); return; }
    busy = true;
    try {
      const res = await withTimeout(
        ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, { action: 'clone' }),
        lockTimeout(), 'Копирование запроса');
      ctx.toast('Копия запроса создана', 'ok');
      if (res?.root_id) ctx.navigate(`#/room/${encodeURIComponent(roomId)}/request/${encodeURIComponent(res.root_id)}`);
      else await refresh({ force: true });
    } catch (err) {
      ctx.toast(err?.message || 'Не удалось клонировать запрос', 'danger');
    } finally {
      busy = false;
    }
  }

  async function decompose(btn) {
    const ok = await confirmDialog({
      title: 'Разложить запрос сейчас?',
      text: 'Это запустит модель-разборщика и потратит токены. Обычно руководитель комнаты разбирает запрос сам — действие нужно, только если работа не началась.',
      confirmLabel: 'Разложить',
      cancelLabel: 'Отмена',
    });
    if (!ok) return;
    // W28-D7: разблокировка под общим замком — иначе кнопка «Разложить» оставалась
    // выключенной, если запрос не ответил.
    await locked(btn, () => runAction('decompose', {},
      { toast: 'Разбор запущен — это может занять пару минут' }),
    { label: 'Разбор запроса' });
  }

  /* ── блоки страницы: шапка и меню, ответ, колонка «От вас нужно» ───────────── */

  // Общий контекст для модулей request-header/answer/aside: изменяемые
  // detail / roomId / backlog читаются через геттеры, остальное — стабильные ссылки.
  // W26-D2: стор создаётся ДО мест, которые его показывают: и основной блок, и
  // правая колонка получают один и тот же объект (`R.store`).
  const store = requestStoreFor(rid, {
    api: ctx && ctx.api,
    roomId,
    detail,
    refresh: (o) => refresh(o),
  });
  const R = {
    ctx, rid, root, state, keepValues, store,
    get roomId() { return roomId; },
    get detail() { return detail; },
    get backlog() { return backlog; },
    refresh, render, runAction, cloneRequest, decompose,
    // «Добавить сообщение руководителю» из меню «Действия» — фокус в поле диалога.
    focusThread() {
      try {
        if (threadCtl && typeof threadCtl.focus === 'function') threadCtl.focus();
      } catch (err) { console.warn('[request] диалог:', err); }
    },
  };
  const header = createHeader(R);
  const answer = createAnswer(R);
  const { buildAnswer } = answer;
  const { buildAside } = createAside(R);
  // W22-AP2: «Разрешения агентам» — блок под ответом руководителя.
  const approvalsUI = createApprovals(R);

  /* ── сборка страницы ──────────────────────────────────────── */

  /**
   * «Запрос» вверху страницы: сворачивается тем же общим компонентом (У-1),
   * что «Диалог», «Дерево работ» и «Артефакты» — одна стрелка, одно поведение.
   * По умолчанию свёрнут (как было); в заголовке — первая строка запроса.
   */
  function askBlock() {
    if (!detail.text) return null;
    const defaults = sectionDefaults(detail, { layout: layoutMode() });
    const sec = createSection({
      key: 'ask',
      requestId: rid,
      title: 'Запрос',
      titleClass: 'req-ask__label',
      defaultOpen: defaults.ask,
      card: false,
      className: 'req-ask',
      content: () => h('div', { class: 'req-ask__text' }, detail.text),
    });
    sections.set('ask', sec);
    // Превью первой строки — как было: в заголовке, справа от подписи.
    sec.extra.appendChild(h('span', { class: 'req-ask__preview', title: firstLine(detail.text) }, firstLine(detail.text)));
    return sec.element;
  }

  /**
   * W25-L2: пересчитать умолчания блоков под текущую ширину вида (R5 §7.2, §7.7).
   *
   * Что делает: берёт режим из `data-layout` (единственный источник, L1), считает
   * умолчания одной функцией `sectionDefaults` и применяет их к блокам, где
   * человек ещё не выбирал сам. Плюс переставляет «Диалог» в лист снизу на
   * узком виде (`data-sheet`).
   *
   * «Состояние переживает смену ширины» (R5 §9 п. 18) — именно потому, что
   * `applyDefault` уважает явный выбор и ничего в него не пишет.
   *
   * @returns {{mode: string, changed: number}}
   */
  function applyLayout() {
    const mode = layoutMode();
    const s = synthesisOf(detail || {});
    const defaults = sectionDefaults(detail, { layout: mode, version: latestVersion(s) });
    let changed = 0;
    for (const [key, sec] of sections) {
      if (defaults[key] === undefined) continue;
      const before = sec.isOpen();
      const after = sec.applyDefault(defaults[key]);
      if (before !== after) changed += 1;
    }
    // «Диалог по запросу» на узком виде открывается листом снизу (R5 §7.2).
    const threadEl = (root.querySelector ? root.querySelector('.req-thread') : null);
    if (threadEl) {
      if (mode === 'm' || mode === 's' || mode === 'xs') threadEl.setAttribute('data-sheet', '1');
      else threadEl.removeAttribute('data-sheet');
    }
    return { mode, changed };
  }

  /**
   * Линейка этапов: у завершённых и упавших — одна тихая строка с раскрытием,
   * у идущих — развёрнута. Внутренний заголовок карточки скрыт (CSS).
   * @returns {HTMLElement|null}
   */
  function pipelineBlock() {
    const card = safePart('renderPipeline', parts?.renderPipeline, detail);
    if (!card) return null;

    const final = FINAL.has(String(detail.status || ''));
    const p = detail.progress || {};
    const total = Number(p.total || 0);
    const done = Number(p.done || 0);
    const open = final ? Boolean(state.pipeOpen) : true;

    const det = h('details', { class: 'req-pipe' + (final ? ' req-pipe--quiet' : ''), open: open ? true : null });
    det.addEventListener('toggle', () => { state.pipeOpen = det.open; });
    det.append(
      h('summary', { class: 'req-pipe__summary' },
        icon('chevron-right', { size: 12, className: 'req-pipe__chev' }),
        h('span', { class: 'req-pipe__title' }, 'Этапы'),
        total ? h('span', { class: 'req-pipe__count tnum' }, fromCount(done, total, ['этап', 'этапа', 'этапов'])) : null),
      h('div', { class: 'req-pipe__body' }, card),
    );
    return det;
  }

  /**
   * Крошка под-запроса направления: `Комната › Бриф › Направление`.
   * `plan_link` приходит только у под-запросов проектного режима; название
   * брифа и его id — из карты комнаты (если сервер их прислал) или из ссылки.
   * @returns {HTMLElement|null}
   */
  function briefCrumbsBlock() {
    const link = detail && detail.plan_link;
    if (!link || typeof link !== 'object') return null;
    const items = briefCrumbItems({
      roomId,
      roomTitle: (room && (room.title || room.name)) || '',
      briefRootId: link.brief_root_id || (plan && plan.brief_root_id) || '',
      briefTitle: link.brief_title || briefTitle || (plan && plan.brief_title) || '',
      streamTitle: link.title,
    });
    if (!items.length) return null;

    const nav = h('nav', { class: 'req-scope-crumbs', 'aria-label': 'Где вы находитесь' });
    items.forEach((item, i) => {
      if (i) nav.appendChild(h('span', { class: 'req-scope-crumbs__sep', 'aria-hidden': 'true' }, '›'));
      if (item.href && !item.current) {
        nav.appendChild(h('a', { class: 'req-scope-crumbs__link', href: item.href, title: item.text }, item.text));
      } else {
        nav.appendChild(h('span', { 'aria-current': item.current ? 'page' : null }, item.text));
      }
    });
    return nav;
  }

  /**
   * Плашка «Предложенная карта» — только на странице брифа проекта.
   * До первой загрузки карты плашку не рисуем: иначе на секунду мелькает
   * «направлений ещё нет» у брифа, у которого карта уже есть.
   * @returns {HTMLElement|null}
   */
  function planBlock() {
    if (!detail || !isBrief(detail) || !planAt) return null;
    return buildPlanCard({
      plan,
      roomId,
      error: planError,
      launching,
      onLaunchAll: launchAll,
      onRetry: () => { loadPlan({ force: true }).then(() => render()); },
    });
  }

  function buildPage() {
    if (!rid) {
      return h('div', { class: 'req' }, requestErrorBanner(new Error('В адресе нет идентификатора запроса'), null));
    }
    if (!detail) {
      if (error) {
        return h('div', { class: 'req' },
          h('div', { class: 'req__crumb' }, h('a', { href: `#/room/${encodeURIComponent(roomId)}/composer` }, icon('arrow-left', { size: 12 }), 'К запросам')),
          requestErrorBanner(error, () => refresh({ force: true, initial: true })));
      }
      return h('div', { class: 'req' },
        h('div', { class: 'req__head' },
          h('div', { class: 'skeleton skeleton--title' }),
          h('div', { class: 'skeleton skeleton--short' })),
        h('div', { class: 'card req-answer' }, skeleton(6)));
    }

   
    const page = h('div', { class: 'req' });
    // Под-запрос направления: сначала путь «Комната › Бриф › Направление».
    const crumbs = briefCrumbsBlock();
    if (crumbs) page.append(crumbs);
    page.append(header.buildHeader());
    // W25-L1 (R5 §7.6): одна строка действий свода, липнет к верху прокрутки.
    // W25.5: здесь же единственное место управления — переключатель версий и
    // пометка «актуально» (в карточке их больше нет). Кнопки панели нажимают
    // кнопки карточки свода (одна реализация действия), меню «Ещё ▾» — тот же
    // `exportGroup`, что и внизу карточки (волна 24).
    // Пока свода нет — панели нет: пустая строка не нужна.
    const svodBar = buildSvodBar({
      ctx,
      rid,
      getDetail: () => detail,
      root: page,
      getVersion: () => Number(state.version) || 0,
      onVersion: (n) => answer.setVersion(n),
      onRefresh: () => answer.refreshSvod(),
    });
    if (svodBar) page.append(svodBar);
    // W25-L2 (R5 §7.4): тонкая строка «Фокуса» — видна только при `data-focus="on"`.
    const focusBar = buildFocusBar({
      total: (Array.isArray(detail.open_questions) ? detail.open_questions : [])
        .filter((q) => q && !q.answered && !q.dismissed).length,
      onExit: () => { focus.off(); },
    });
    if (focusBar) page.append(focusBar);

    const pipeline = pipelineBlock();
    if (pipeline) page.append(pipeline);
    const warnings = safePart('renderWarnings', parts?.renderWarnings, detail);
    if (warnings) page.append(warnings);

   
    const main = h('div', { class: 'req__main' });
    const ask = askBlock();
    if (ask) main.append(ask);
    // Бриф: карта направлений сразу под запросом, до ответа руководителя.
    const planCard = planBlock();
    if (planCard) main.append(planCard);
    main.append(buildAnswer());
    // W22-AP2: «Разрешения агентам» — сразу под ответом: участник упёрся в
    // запрет Hermes, и без решения человека работа стоит.
    const approvals = approvalsUI.buildRequestBlock();
    if (approvals) main.append(approvals);
    // W23-MG2: «Контроль моделей» — что сторож остановил или исправил по этой
    // карточке. Ставим сразу после разрешений: оба блока про вмешательство
    // приложения в работу команды, и оба видны до диалога.
    const guardFeed = safePart('renderModelGuard', parts?.renderModelGuard, detail, ctx);
    if (guardFeed) main.append(guardFeed);

    // Диалог по запросу — прямо под ответом: уточнить, пока команда работает,
    // или спросить после ответа. W20-C1 (B-40): блок монтируется ОДИН раз (см.
    // ensureThread) и дальше только обновляет данные; здесь остаётся его место
    // на странице — с меткой «не пересобирать», чтобы правка страницы не
    // разлучала живое поле с живой кнопкой.
    main.append(h('div', { class: 'req-thread', 'data-keep-tree': '' }));
    // W20-FB: «Ход работы» — кто что делает прямо сейчас. Своё место-узел,
    // чтобы лента жила отдельно от перерисовок страницы (как диалог выше).
    main.append(h('div', { class: 'req-worklog', 'data-keep-tree': '' }));

   
    const critic = safePart('renderCritic', parts?.renderCritic, detail, ctx);
    if (critic) main.append(critic);
    const report = safePart('renderReportEmbed', reportEmbed?.renderReportEmbed, detail, ctx);
    if (report) main.append(report);
    const tree = safePart('renderTree', parts?.renderTree, detail, ctx, { onChanged: () => refresh({ force: true }) });
    if (tree) main.append(tree);
    const artifacts = safePart('renderArtifacts', parts?.renderArtifacts, detail, ctx, { projectDir: (room && room.project_dir) || '' });
    if (artifacts) main.append(artifacts);
    const timeline = safePart('renderTimeline', parts?.renderTimeline, detail);
    if (timeline) main.append(timeline);

   
    page.append(h('div', { class: 'req__grid' }, main, buildAside()));
    return page;
  }

  /**
   * Диалог по запросу: монтируется ОДИН раз за визит страницы, дальше только
   * обновляется (`update`) — W20-C1, B-40.
   *
   * Почему не пересобирать: живая кнопка «Отправить» и поле с набранным текстом
   * обязаны принадлежать одной сборке. Раньше блок собирался на каждый render, а
   * точечная правка страницы оставляла часть узлов от старой сборки — кнопка
   * новой сборки читала своё (не вставленное) поле и отвечала «Напишите сообщение».
   *
   * Место на странице помечено `data-keep-tree` (live-patch его не трогает),
   * поэтому узел переносится между перерисовками как есть.
   */
  function ensureThread() {
    const slot = root.querySelector ? root.querySelector('.req-thread') : null;
    if (!slot) return;
    if (!threadCtl) {
      threadCtl = mountThread(slot, ctx, {
        roomId,
        rid,
        detail,
        onSent: () => refresh({ force: true, quiet: true }),
      });
      return;
    }
    const el = threadCtl.element;
    if (el && el.parentNode !== slot) slot.appendChild(el);
    if (typeof threadCtl.update === 'function') threadCtl.update(detail);
  }

  /**
   * Блок «Ход работы» (W20-FB): монтируется один раз за визит страницы, дальше
   * получает новые данные через `update(detail)` — статус запроса решает, как
   * часто опрашивать сервер (3 с в работе, 30 с после) и когда свернуть блок.
   * Место помечено `data-keep-tree`, поэтому live-patch его не заменяет: лента,
   * её прокрутка и раскрытые записи переживают перерисовку страницы.
   */
  function ensureWorklog() {
    const slot = root.querySelector ? root.querySelector('.req-worklog') : null;
    if (!slot) return;
    if (!worklogCtl) {
      worklogCtl = mountWorklog(slot, ctx, { roomId, rid, detail });
      return;
    }
    const el = worklogCtl.element;
    if (el && el.parentNode !== slot) slot.appendChild(el);
    if (typeof worklogCtl.update === 'function') worklogCtl.update(detail);
  }

  /**
   * Перерисовать страницу, сохранив состояние.
   *
   * WAVE17-P4: вместо `root.replaceChildren(...)` (снос всего дерева и сброс
   * прокрутки правой колонки и ленты) — точечная правка через live-patch.js:
   * одинаковые блоки остаются на месте, изменившиеся заменяются по одному.
   * Если человек печатает или диктует, обновление откладывается (`deferIfEditing`).
   */
  function render(opts = {}) {
    if (disposed) return;
    if (opts.deferIfEditing && isEditing(root)) {
      pendingRender = true;
      scheduleDeferredRender();
      return;
    }
    header.closeMenu();
    // W17-STAB-2: снимок состояния берём ДО сборки новой страницы. Сборка
    // переиспользует живые строки вопросов (`reconcileQuestions` переносит их в
    // новое дерево), из-за чего колонка на миг теряет содержимое и Chrome
    // сбрасывает её scrollTop в 0. Снимок после сборки запоминал бы уже ноль —
    // и прокрутка правой колонки пропадала при каждом обновлении.
    const snap = captureSnapshot(root, document);
    const next = buildPage();
    applyLivePatch(root, next, { doc: document, draftKeys: keepValues, snapshot: snap });
    // Диалог: один раз смонтировать, дальше только данные (W20-C1, B-40).
    ensureThread();
    ensureWorklog();
    pendingRender = false;
    deferredSince = 0;
    if (deferTimer) { clearTimeout(deferTimer); deferTimer = null; }
    // Поля страницы, чей черновик живёт в карте: значение важнее разметки.
    for (const input of root.querySelectorAll('[data-keep]')) {
      const key = input.dataset.keep;
      if (keepValues.has(key) && input.value !== keepValues.get(key)) input.value = keepValues.get(key);
    }
  }

  /** Отложенное обновление: ждём, пока закончат печатать или диктовать. */
  function scheduleDeferredRender() {
    if (deferTimer || disposed) return;
    if (!deferredSince) deferredSince = Date.now();
    deferTimer = setTimeout(tryDeferredRender, DEFER_CHECK_MS);
  }

  function tryDeferredRender() {
    deferTimer = null;
    if (disposed) return;
    if (!pendingRender) { deferredSince = 0; return; }
    if (isEditing(root)) {
      // Ждём дальше: обновление не должно отбирать поле или ронять прокрутку.
      deferTimer = setTimeout(tryDeferredRender, DEFER_CHECK_MS);
      return;
    }
    render();
  }

  const onFocusOut = () => { if (pendingRender) scheduleDeferredRender(); };
  root.addEventListener('focusout', onFocusOut);
  listeners.push(() => { try { root.removeEventListener('focusout', onFocusOut); } catch { /* ignore */ } });

  /* ── старт ─────────────────────────────────────────────────── */

  render();
  await ensureParts();
  await refresh({ initial: true });
  if (disposed) return { unmount() {} };

  /* ── W27-E3: пришли по ссылке на конкретный шаг ──────────────────────────
   * «Сегодня» ведёт сюда с `?step=ns_2`: раскрываем блок «Шаги» и подсвечиваем
   * этот шаг, иначе переход «в запрос» был бы переходом «куда-то туда».
   * Шаги приходят вместе с деталями (и на перерисовке могут появиться позже),
   * поэтому ждём их появления, а не надеемся на первый кадр.
   */
  const focusStepId = stepFromRoute(ctx.route)
    || String((ctx.params && ctx.params.step) || '');
  if (focusStepId) {
    focusStepWhenReady(root, focusStepId, {
      scroll: !isTypingTarget(document.activeElement),
      onMissing: () => {
        try {
          ctx.toast('Этот шаг уже не в плане: он сделан, снят или переписан', 'warn');
        } catch { /* тост не критичен */ }
      },
    });
  }

  poller = (typeof ctx.api.poll === 'function' ? ctx.api.poll : apiPoll)(tick, POLL_ACTIVE, { pauseHidden: true, immediate: false });

  /* ── W25-L2: клавиши страницы (R5 §7.8) ─────────────────────────────────
   *
   * Здесь только те, что принадлежат СТРАНИЦЕ ЗАПРОСА: «Фокус», `Esc`, `[`/`]`,
   * `A`. J/K, Ctrl+Enter, `?`, `V` и палитра уже стоят общим обработчиком
   * (`hotkeys.js`, app.js) — второй обработчик их бы дублировал.
   * Правило R5 §7.8 соблюдается в одном месте: если курсор в поле ввода, одиночные
   * клавиши молчат (`isTypingTarget`), а Ctrl/⌘-сочетания работают всегда, кроме
   * Ctrl+Shift+F, который в поле тоже полезен (увести лишнее с глаз).
   */
  const onPageKey = (ev) => {
    if (!ev || ev.defaultPrevented || ev.isComposing) return;
    const key = String(ev.key || '');
    const mod = ev.ctrlKey || ev.metaKey;
    // Ctrl/⌘ + Shift + F — вход и выход из «Фокуса» (R5 §7.4).
    if (mod && ev.shiftKey && (key === 'F' || key === 'f' || key === 'А' || key === 'а')) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      focus.toggle();
      return;
    }
    if (mod || ev.altKey) return;
    if (isTypingTarget(ev.target)) return;

    if (key === 'Escape' || key === 'Esc') {
      // Сначала — открытое меню «Действия» (поведение L1 сохраняем).
      if (header.isMenuOpen()) {
        header.closeMenu();
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        return;
      }
      // Порядок как у человека: сначала лист диалога, потом «Фокус».
      const sheet = (root.querySelector ? root.querySelector('.req-thread[data-sheet="1"][data-open="1"]') : null);
      if (sheet) {
        const ctl = sections.get('thread');
        if (ctl) ctl.collapse();
        else { const btn = sheet.querySelector ? sheet.querySelector('.sec__toggle') : null; if (btn && btn.click) btn.click(); }
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        return;
      }
      if (focus.isOn()) {
        focus.off();
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
      }
      return;
    }
    if (key === '[' || key === 'х' || key === 'Х') { stepRequest(-1); return; }
    if (key === ']' || key === 'ъ' || key === 'Ъ') { stepRequest(1); return; }
    // `A` — «Ответить»: открыть первый неотвеченный вопрос и поставить курсор.
    if (key === 'a' || key === 'A' || key === 'ф' || key === 'Ф') {
      const bar = (root.querySelector ? root.querySelector('.req-pinbar') : null);
      if (bar && bar.click) { if (typeof ev.preventDefault === 'function') ev.preventDefault(); bar.click(); return; }
      const field = (typeof document !== 'undefined' && document.querySelector)
        ? document.querySelector('#questions .req-q__input')
        : null;
      if (field && typeof field.focus === 'function') {
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        try { field.focus({ preventScroll: false }); } catch { field.focus(); }
      }
    }
  };
  document.addEventListener('keydown', onPageKey);
  listeners.push(() => document.removeEventListener('keydown', onPageKey));

  /* Смена ширины вида: пересчитать умолчания блоков (R5 §7.2, §7.7, §9 п. 18).
     Событие рассылает `layout-mode.js` — наш код только слушает. */
  const onLayout = () => {
    if (disposed) return;
    applyLayout();
  };
  window.addEventListener('teamroom:layout', onLayout);
  listeners.push(() => window.removeEventListener('teamroom:layout', onLayout));
  applyLayout();

  return {
    unmount() {
      disposed = true;
      try { store.destroy(); } catch { /* стор уже забыт */ }
      if (poller) poller.stop();
      if (deferTimer) { clearTimeout(deferTimer); deferTimer = null; }
      pendingRender = false;
      header.closeMenu();
      if (threadCtl && typeof threadCtl.destroy === 'function') {
        try { threadCtl.destroy(); } catch { /* узел уже убран */ }
      }
      threadCtl = null;
      if (worklogCtl && typeof worklogCtl.destroy === 'function') {
        try { worklogCtl.destroy(); } catch { /* узел уже убран */ }
      }
      worklogCtl = null;
      // «Фокус» не переносится на другие экраны (R5 §7.4: состояние на маршрут).
      try { focus.destroy(); } catch { /* страница уже без #app */ }
      try { density.destroy(); } catch { /* плотность живёт в атрибуте */ }
      sections.clear();
      for (const off of listeners) { try { off(); } catch { /* ignore */ } }
      listeners.length = 0;
    },
  };
}

export default mount;
