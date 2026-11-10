/**
 * request.js — страница запроса #/room/<id>/request/<root>.
 *
 * Верх страницы: шапка, этапы, предупреждения, крупный ответ с версиями,
 * проверка критика, встроенный отчёт, дерево работ, артефакты, хронология.
 * Справа — «Дальше»: шаги, вопросы к Михаилу, решения, будущие вопросы, фаза.
 *
 * Файл — каркас (данные, опрос, действия, сборка страницы); шапка и меню — в
 * request-header.js, ответ и версии — в request-answer.js, колонка «Дальше» —
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
import { mountThread } from './request-thread.js';
import { isBrief, buildPlanCard } from './plan-req.js';
import { briefCrumbItems, requestTitle } from '../scope.js';

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
  let threadCtl = null; // { focus() } — блок «Диалог по запросу»
  let room = null; // деталь комнаты: project_dir, title (для кнопки папки и крошки)
  let plan = null; // карта проекта комнаты (GET /api/rooms/{r}/plan)
  let planError = null;
  let planAt = 0;
  let launching = false; // идёт «Запустить всё»
  let briefTitle = ''; // название брифа — для крошки под-запроса

  const state = { version: null, askOpen: false, pipeOpen: false };
  const keepValues = new Map(); // data-keep → введённый текст
  const listeners = [];
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
    if (busy) return null;
    busy = true;
    try {
      const res = await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, { action, ...(body || {}) });
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
    if (busy) return;
    busy = true;
    try {
      const res = await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, { action: 'clone' });
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
    if (btn) btn.disabled = true;
    await runAction('decompose', {}, { toast: 'Разбор запущен — это может занять пару минут' });
  }

  /* ── блоки страницы: шапка и меню, ответ, колонка «Дальше» ───────────── */

  // Общий контекст для модулей request-header/answer/aside: изменяемые
  // detail / roomId / backlog читаются через геттеры, остальное — стабильные ссылки.
  const R = {
    ctx, rid, root, state, keepValues,
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
  const { buildAnswer } = createAnswer(R);
  const { buildAside } = createAside(R);

  /* ── сборка страницы ──────────────────────────────────────── */

  function askBlock() {
    if (!detail.text) return null;
    const det = h('details', { class: 'req-ask', open: state.askOpen || null });
    det.addEventListener('toggle', () => { state.askOpen = det.open; });
    det.append(
      h('summary', null,
        icon('chevron-right', { size: 12, className: 'req-ask__chev' }),
        h('span', { class: 'req-ask__label' }, 'Запрос'),
        h('span', { class: 'req-ask__preview', title: firstLine(detail.text) }, firstLine(detail.text))),
      h('div', { class: 'req-ask__text' }, detail.text));
    return det;
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
        h('span', { class: 'req-pipe__title' }, 'Ход работы'),
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

    // Диалог по запросу — прямо под ответом: уточнить, пока команда работает,
    // или спросить после ответа. Монтируется каждый раз заново (данные новые),
    // набранное и фокус переживают перерисовку внутри самого блока.
    const threadHost = h('div', { class: 'req-thread' });
    main.append(threadHost);
    threadCtl = mountThread(threadHost, ctx, {
      roomId,
      rid,
      detail,
      onSent: () => refresh({ force: true, quiet: true }),
    });

   
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

  poller = (typeof ctx.api.poll === 'function' ? ctx.api.poll : apiPoll)(tick, POLL_ACTIVE, { pauseHidden: true, immediate: false });

  const onKey = (e) => { if (e.key === 'Escape' && header.isMenuOpen()) header.closeMenu(); };
  document.addEventListener('keydown', onKey);
  listeners.push(() => document.removeEventListener('keydown', onKey));

  return {
    unmount() {
      disposed = true;
      if (poller) poller.stop();
      if (deferTimer) { clearTimeout(deferTimer); deferTimer = null; }
      pendingRender = false;
      header.closeMenu();
      for (const off of listeners) { try { off(); } catch { /* ignore */ } }
      listeners.length = 0;
    },
  };
}

export default mount;
