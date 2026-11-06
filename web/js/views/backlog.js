/**
 * backlog.js — комната, вкладка «Дальше».
 *
 * Сверху вниз: «Ждут вашего решения» (blocked-карточки, вопросы, решения),
 * «Следующие шаги» (чекбокс, срок, .ics), «Будущие вопросы» и повестка на
 * 14 дней. Данные — только ctx.api; опрос каждые 5 с перерисовывает лишь
 * изменившиеся секции, поэтому набранный текст и открытые формы не теряются.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { poll as pollApi } from '../api.js';
import {
  ensureCss, emptyState, skeleton, errorBanner, sectionHeader,
  segmented, dueBadge, statusChip, lsGet, lsSet,
} from '../ui.js';
import {
  partitionBacklog, blockedCards, inboxCount, inboxSummary, backlogQuestionId,
  groupSteps, buildAgenda, agendaRange, icsAllUrl,
  kindLabel, STEP_GROUP_MODES, filterByRequest,
  requestFilterMode, setRequestParam, REQUEST_FILTER_ALL, REQUEST_FILTER_ONE,
} from './backlog-model.js';
import { setInboxCounts } from '../shell-inbox.js';
import { countText, COUNT_FORMS } from '../shell-labels.js';
import {
  STEP_HINT, SAVE_FAILED, ownerName, requestHref, sourceLink, ownerChip, icsLink,
  filterBar,
} from './backlog-parts.js';
import { openStepDialog } from './step-dialog.js';

const POLL_MS = 5000;

/** Заголовок секции с числом и существительным в подсказке (D18). */
function countHeader(title, n, forms, actions) {
  const head = sectionHeader(title, { count: n, actions });
  const chip = head.querySelector('.section-header__count');
  if (chip) {
    const text = countText(n, forms);
    chip.title = text;
    chip.setAttribute('aria-label', text);
  }
  return head;
}

/**
 * @param {HTMLElement} el  контейнер вьюхи (#view)
 * @param {object} ctx  {route, api, store, navigate, toast, room}
 */
export async function mount(el, ctx) {
  ensureCss('css/view-backlog.css');
  ensureCss('css/view-backlog-extras.css');
  // W14-H §12: шаг — кликабельная строка и окно шага (стили общие со страницей запроса).
  ensureCss('css/view-steps.css');

  const roomId = String((ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || '');
  if (!roomId) {
    el.appendChild(h('div', { class: 'view__inner' },
      emptyState({ icon: 'error', title: 'Комната не выбрана', text: 'Откройте комнату в левой колонке.' })));
    return { unmount() {} };
  }

  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const navigate = typeof ctx.navigate === 'function' ? ctx.navigate : (hash) => { location.hash = hash; };
  // ?request=<root_id> — вкладка открыта со страницы запроса: показываем только его дела.
  const requestFilter = String(
    (ctx.route && ctx.route.query && ctx.route.query.request)
    || (ctx.params && ctx.params.request)
    || '',
  ).trim();
  const backlogHash = `#/room/${encodeURIComponent(roomId)}/backlog`;

  const state = {
    error: null,
    offline: false,
    data: null,
    requests: [],
    blocked: [],
    needsKey: '',
    answered: new Set(),
    busy: new Set(),
    groupMode: lsGet('backlog.group', 'source') === 'due' ? 'due' : 'source',
    showClosed: !!lsGet('backlog.closed', false),
    requestFilter,
  };
  const signatures = new Map();
  let disposed = false;
  let loadSeq = 0;
  let detailsAt = 0;
  let poller = null;
  const abort = new AbortController();

  /* ------------------------------------------------------------ каркас */

  const root = h('div', { class: 'view__inner backlog' });
  const errorHost = h('div', { class: 'backlog__error' });
  const filterHost = h('div', { class: 'backlog__filter-host' });
  const inboxHost = h('div', { class: 'backlog__section' });
  const stepsHost = h('div', { class: 'backlog__section' });
  const futureHost = h('div', { class: 'backlog__section' });
  const agendaHost = h('div', { class: 'backlog__section' });
  const closedHost = h('div', { class: 'backlog__section' });

  const closedLabel = h('span', {}, 'Показать закрытые');
  const closedToggle = h('label', { class: 'checkbox backlog__closed-toggle' },
    h('input', {
      type: 'checkbox',
      checked: state.showClosed,
      onchange: (ev) => {
        state.showClosed = !!(ev.target && ev.target.checked);
        lsSet('backlog.closed', state.showClosed);
        paintClosed();
      },
    }),
    closedLabel);

  const toolbar = h('div', { class: 'backlog__toolbar row row--between row--wrap' },
    h('div', { class: 'row row--wrap row--tight' },
      segmented({
        options: STEP_GROUP_MODES,
        value: state.groupMode,
        onchange: (value) => {
          state.groupMode = value === 'due' ? 'due' : 'source';
          lsSet('backlog.group', state.groupMode);
          paintSteps();
        },
      })),
    h('div', { class: 'row row--wrap row--tight' },
      closedToggle,
      h('a', {
        class: 'btn btn--secondary btn--sm',
        href: icsAllUrl(roomId),
        download: '',
        title: 'Файл календаря со всеми шагами и сроками — откроется в вашем календаре',
      }, icon('calendar', { size: 13 }), 'Скачать календарь (.ics)')));

  root.append(errorHost, filterHost, toolbar, inboxHost, stepsHost, futureHost, agendaHost, closedHost);
  el.appendChild(root);

  /* ------------------------------------------------------------ рендер */

  /** Все пункты бэклога комнаты. */
  function allItems() {
    return state.data && Array.isArray(state.data.items) ? state.data.items : [];
  }

  /** Пункты с учётом фильтра «Только этот запрос». */
  function visibleItems() {
    return filterByRequest(allItems(), state.requestFilter);
  }

  /** Название запроса-фильтра (по реестру; иначе короткий id). */
  function filterTitle() {
    const rid = state.requestFilter;
    const found = (state.requests || []).find((r) => String(r && r.id) === rid);
    return String((found && found.title) || rid);
  }

  function setFilter(rid) {
    const hash = setRequestParam(backlogHash, rid);
    if (String(rid || '').trim()) navigate(hash);
    else navigate(backlogHash);
  }

  function buildFilterBar() {
    if (!state.requestFilter) return h('div', { class: 'backlog__filter', hidden: true });
    return filterBar({
      roomId,
      requestId: state.requestFilter,
      title: filterTitle(),
      onClear: () => setFilter(''),
    });
  }

  function paintFilter() {
    paint('filter', filterHost, buildFilterBar(), state.requestFilter + '#' + filterTitle());
  }

  function paint(key, host, node, signature) {
    if (disposed) return;
    if (signatures.get(key) === signature) return;
    signatures.set(key, signature);
    host.replaceChildren(node);
  }

  function sigOf(list, extra = '') {
    return (list || []).map((i) => [
      i.id || i.task_id, i.status, i.due || '', i.asked_request || '', i.updated_at || '', i.text,
    ].join('~')).join('|') + '#' + extra;
  }

  function inlineForm({ label, placeholder, submit, onSubmit, multiline = false }) {
    const input = multiline
      ? h('textarea', { class: 'textarea backlog__answer-input', rows: 2, placeholder, 'aria-label': label })
      : h('input', { class: 'input backlog__answer-input', type: 'text', placeholder, 'aria-label': label });
    const button = h('button', { class: 'btn btn--primary btn--sm', type: 'submit' }, submit);
    const form = h('form', {
      class: 'backlog__answer',
      onsubmit: async (ev) => {
        if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
        const text = String(input.value || '').trim();
        if (!text) {
          if (typeof input.focus === 'function') input.focus();
          return;
        }
        button.disabled = true;
        try {
          await onSubmit(text);
        } finally {
          button.disabled = false;
        }
      },
    }, input, button);
    return form;
  }

  function inboxRow({ kind, iconName, title, text, hint, source, due, actions, form }) {
    return h('div', { class: 'inbox-row inbox-row--' + kind },
      h('div', { class: 'inbox-row__icon' }, icon(iconName, { size: 15 })),
      h('div', { class: 'inbox-row__main' },
        h('div', { class: 'inbox-row__top' },
          h('span', { class: 'inbox-row__kind' }, title),
          dueBadge(due) || null,
          source),
        h('div', { class: 'inbox-row__text' }, text),
        hint ? h('div', { class: 'inbox-row__hint small muted' }, hint) : null,
        form),
      actions ? h('div', { class: 'inbox-row__actions' }, actions) : null);
  }

  function inboxGroup(title, rows) {
    if (!rows.length) return null;
    return h('div', { class: 'inbox-group' },
      h('div', { class: 'inbox-group__title small muted' }, title, h('span', { class: 'tnum' }, String(rows.length))),
      h('div', { class: 'inbox-list' }, rows));
  }

  function buildInbox() {
    const parts = partitionBacklog(visibleItems());
    const questions = parts.questions.filter((q) => !state.answered.has(q.id));
    const decisions = parts.decisions.filter((d) => !state.answered.has(d.id));
    const blocked = state.blocked.filter((b) => !state.answered.has(b.task_id)
      && (!state.requestFilter || String(b.request_id) === state.requestFilter));
    const total = inboxCount({ questions, decisions, blocked });

    const rowsBlocked = blocked.map((card) => inboxRow({
      kind: 'blocked',
      iconName: 'stop-circle',
      title: 'Заблокированная задача',
      text: card.text,
      hint: card.hint || null,
      source: sourceLink(roomId, card.request_id, card.request_title),
      actions: statusChip('blocked'),
      form: inlineForm({
        label: 'Ответ по заблокированной задаче',
        placeholder: 'Что делать дальше…',
        submit: 'Ответить',
        onSubmit: async (text) => {
          try {
            await api.post(`/api/requests/${encodeURIComponent(card.request_id)}/actions`,
              { action: 'unblock', task_id: card.task_id, text });
            state.answered.add(card.task_id);
            toast('Ответ отправлен, задача разблокирована', 'ok');
          } catch (err) {
            toast(err && err.message ? err.message : 'Не удалось отправить ответ', 'danger');
          } finally {
            await load({ silent: true });
          }
        },
      }),
    }));

    const rowsQuestions = questions.map((item) => inboxRow({
      kind: 'question',
      iconName: 'comment-draft',
      title: 'Вопрос к вам',
      text: item.text,
      hint: null,
      source: sourceLink(roomId, item.source_request, item.source_title),
      due: item.due,
      actions: null,
      form: inlineForm({
        label: 'Ответ на вопрос',
        placeholder: 'Ваш ответ…',
        submit: 'Ответить',
        onSubmit: async (text) => {
          const qid = backlogQuestionId(item.id);
          if (!qid || !item.source_request) {
            toast('У вопроса нет идентификатора — ответить нельзя', 'danger');
            return;
          }
          try {
            await api.post(`/api/requests/${encodeURIComponent(item.source_request)}/actions`,
              { action: 'answer', question_id: qid, task_id: item.source_request, text });
            state.answered.add(item.id);
            toast('Ответ отправлен', 'ok');
          } catch (err) {
            toast(err && err.message ? err.message : 'Не удалось отправить ответ', 'danger');
          } finally {
            await load({ silent: true });
          }
        },
      }),
    }));

    const rowsDecisions = decisions.map((item) => inboxRow({
      kind: 'decision',
      iconName: 'lightbulb',
      title: 'Предложенное решение',
      text: item.text,
      hint: null,
      source: sourceLink(roomId, item.source_request, item.source_title),
      due: item.due,
      actions: h('div', { class: 'row row--tight row--wrap' },
        h('button', {
          class: 'btn btn--primary btn--sm',
          type: 'button',
          disabled: state.busy.has(item.id),
          onclick: () => decide(item, 'done'),
        }, icon('check', { size: 12 }), 'Принять'),
        h('button', {
          class: 'btn btn--ghost btn--sm',
          type: 'button',
          disabled: state.busy.has(item.id),
          onclick: () => decide(item, 'dismissed'),
        }, 'Отклонить')),
      form: null,
    }));

    const groups = [
      inboxGroup('Заблокированные задачи', rowsBlocked),
      inboxGroup('Вопросы', rowsQuestions),
      inboxGroup('Предложения решений', rowsDecisions),
    ].filter(Boolean);

    const stepsCount = partitionBacklog(visibleItems()).steps.length;
    const body = total === 0
      ? emptyState({
        icon: 'pass-filled',
        title: 'Ничего не ждёт вашего решения',
        text: 'Команда работает сама. Здесь появятся блокировки, вопросы и предложенные решения — как только понадобится ваш ответ.',
        action: stepsCount
          ? { label: 'К следующим шагам', onclick: () => stepsHost.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
          : null,
      })
      : h('div', { class: 'inbox-groups' }, groups);

    const summary = inboxSummary({ questions, blocked, decisions });
    const scopeSwitch = state.requestFilter
      ? segmented({
        options: [
          { value: REQUEST_FILTER_ALL, label: 'Все' },
          { value: REQUEST_FILTER_ONE, label: 'Только этот запрос' },
        ],
        value: requestFilterMode(state.requestFilter),
        onchange: (value) => setFilter(value === REQUEST_FILTER_ONE ? state.requestFilter : ''),
      })
      : null;
    const head = h('div', { class: 'backlog__card-head backlog__card-head--inbox' },
      countHeader('Ждут вашего решения', summary.total, COUNT_FORMS.case, total
        ? h('span', { class: 'chip chip--warn' }, icon('bell', { size: 11 }), 'нужен ответ')
        : null),
      summary.text ? h('div', { class: 'inbox__breakdown small muted' }, summary.text) : null,
      scopeSwitch);
    // Одно число для вкладки, сайдбара и заголовка окна (D4).
    setInboxCounts(roomId, { questions: summary.questions, blocked: summary.blocked, decisions: summary.decisions });

    return h('section', { class: 'card card--flush backlog__card inbox' },
      head,
      h('div', { class: 'backlog__card-body' }, body));
  }

  function paintInbox() {
    const parts = partitionBacklog(visibleItems());
    const signature = sigOf(parts.questions, 'q') + sigOf(parts.decisions, 'd')
      + sigOf(state.blocked, 'b') + [...state.answered].sort().join(',') + [...state.busy].sort().join(',')
      + '#' + parts.steps.length;
    paint('inbox', inboxHost, buildInbox(), signature);
  }

  /** Шаг «Дальше»: не чекбокс, а кликабельная строка — окно действий (W14-H §12). */
  function stepRow(item) {
    const busy = state.busy.has(item.id);
    const done = item.status === 'done';
    const statusRu = { open: 'предстоит', asked: 'в работе', done: 'сделано', dismissed: 'убран' };
    const open = h('div', {
      class: 'step-row__open',
      role: 'button',
      tabindex: '0',
      'aria-haspopup': 'dialog',
      'aria-label': 'Шаг: ' + item.text + ' — открыть окно шага',
      onclick: () => openStep(item),
      onkeydown: (ev) => {
        const key = ev && ev.key;
        if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
          if (typeof ev.preventDefault === 'function') ev.preventDefault();
          openStep(item);
        }
      },
    },
    h('div', { class: 'step-row__body' },
      h('div', { class: 'step-row__text' }, item.text),
      h('div', { class: 'step-row__meta' },
        ownerChip(item.owner),
        item.due ? dueBadge(item.due) : null,
        h('span', { class: 'chip chip--' + (done ? 'ok' : item.status === 'asked' ? 'info' : 'muted') },
          statusRu[item.status] || 'предстоит'),
        sourceLink(roomId, item.source_request, item.source_title))));
    return h('div', { class: 'step-row' + (busy ? ' step-row--busy' : '') + (done ? ' step-row--done' : '') },
      open,
      h('div', { class: 'step-row__actions' },
        icsLink(roomId, item.id, 'В календарь: ' + item.text)));
  }

  /** Окно шага из «Дальше»: шаг лежит в запросе-источнике. */
  function openStep(item) {
    const rid = String(item.source_request || '').trim();
    const localId = String(item.id || '').includes(':') ? String(item.id).split(':').pop() : String(item.id || '');
    if (!rid) {
      toast('У шага нет запроса-источника — открыть окно нельзя', 'warn');
      return;
    }
    openStepDialog({
      requestId: rid,
      stepId: localId,
      room: null,
      roomId,
      api,
      toast,
      onDone: () => load({ silent: true }),
    });
  }

  function buildSteps() {
    const steps = partitionBacklog(visibleItems()).steps;
    const groups = groupSteps(steps, state.groupMode, new Date());
    const head = h('div', { class: 'backlog__card-head' },
      countHeader('Следующие шаги', steps.length, ['шаг', 'шага', 'шагов']));

    let body;
    if (!steps.length) {
      body = emptyState({
        icon: 'checklist',
        title: 'Шагов пока нет',
        text: 'Когда команда закончит запрос, она предложит 3–7 конкретных следующих шагов — они появятся здесь.',
      });
    } else {
      body = h('div', { class: 'step-groups' }, groups.map((group) => h('div', { class: 'step-group' },
        h('div', { class: 'step-group__head' },
          state.groupMode === 'source'
            ? h('a', { class: 'step-group__title', href: group.request_id ? requestHref(roomId, group.request_id) : undefined, title: group.title },
              h('span', { class: 'truncate' }, group.title))
            : h('span', { class: 'step-group__title' },
              group.tone === 'danger' ? icon('warning', { size: 12 }) : null,
              h('span', {}, group.title)),
          h('span', { class: 'chip chip--muted tnum' }, String(group.items.length)),
          group.due ? dueBadge(group.due) : null),
        h('div', { class: 'step-list' }, group.items.map(stepRow)))));
    }

    return h('section', { class: 'card card--flush backlog__card' },
      head,
      h('div', { class: 'backlog__card-body' },
        steps.length ? h('div', { class: 'backlog__hint small muted' }, icon('check', { size: 11 }), STEP_HINT) : null,
        body));
  }

  function paintSteps() {
    const steps = partitionBacklog(visibleItems()).steps;
    const signature = sigOf(steps) + '#' + state.groupMode + '#' + [...state.busy].sort().join(',');
    paint('steps', stepsHost, buildSteps(), signature);
    paintAgenda();
  }

  function futureCard(item) {
    const asked = item.status === 'asked';
    const askedRid = item.asked_request;
    return h('div', { class: 'future-card' + (asked ? ' future-card--asked' : '') },
      h('div', { class: 'future-card__main' },
        h('div', { class: 'future-card__text' }, item.text),
        h('div', { class: 'future-card__meta' },
          h('span', { class: 'chip chip--muted' }, kindLabel('future_question')),
          item.due ? dueBadge(item.due) : null,
          sourceLink(roomId, item.source_request, item.source_title),
          asked && askedRid
            ? h('a', { class: 'backlog__source', href: requestHref(roomId, askedRid) },
              icon('arrow-right', { size: 11 }), h('span', { class: 'truncate' }, 'Открыть запрос'))
            : null)),
      h('div', { class: 'future-card__actions' },
        asked
          ? h('span', { class: 'chip chip--info' }, icon('check', { size: 11 }), 'Задан')
          : h('button', {
            class: 'btn btn--secondary btn--sm',
            type: 'button',
            disabled: state.busy.has(item.id),
            onclick: () => askFuture(item),
          }, icon('send', { size: 12 }), 'Задать'),
        asked ? null : h('button', {
          class: 'btn btn--ghost btn--sm',
          type: 'button',
          disabled: state.busy.has(item.id),
          onclick: () => mutate(item.id, { status: 'dismissed' }, 'Вопрос отложен'),
        }, 'Отложить')));
  }

  function buildFuture() {
    const future = partitionBacklog(visibleItems()).future;
    const body = future.length
      ? h('div', { class: 'future-list' }, future.map(futureCard))
      : emptyState({
        icon: 'chat-sparkle',
        title: 'Будущих вопросов нет',
        text: 'Вопросы, которые пока не входят в текущую беседу, команда складывает сюда. Их можно задать одним нажатием.',
      });
    return h('section', { class: 'card card--flush backlog__card future' },
      h('div', { class: 'backlog__card-head' },
        countHeader('Будущие вопросы', future.length, COUNT_FORMS.question)),
      h('div', { class: 'backlog__card-body' }, body));
  }

  function paintFuture() {
    const future = partitionBacklog(visibleItems()).future;
    paint('future', futureHost, buildFuture(), sigOf(future) + '#' + [...state.busy].sort().join(','));
  }

  function buildAgendaCard() {
    const steps = partitionBacklog(visibleItems()).steps;
    const agenda = buildAgenda(steps, new Date(), 14);
    const body = agenda.length
      ? h('div', { class: 'agenda' }, agenda.map((day) => h('div', {
        class: 'agenda-day' + (day.overdue ? ' agenda-day--overdue' : ''),
      },
      h('div', { class: 'agenda-day__head' },
        h('span', { class: 'agenda-day__date' }, day.title),
        day.overdue ? h('span', { class: 'chip chip--danger' }, icon('warning', { size: 10 }), 'просрочено') : null,
        h('span', { class: 'chip chip--muted tnum' }, String(day.items.length))),
      h('div', { class: 'agenda-day__items' }, day.items.map((item) => h('div', { class: 'agenda-item' },
        h('span', { class: 'agenda-item__dot' }),
        h('a', {
          class: 'agenda-item__text truncate' + (day.overdue ? ' agenda-item__text--overdue' : ''),
          href: item.source_request ? requestHref(roomId, item.source_request) : undefined,
          title: item.text,
        }, item.text),
        h('span', { class: 'agenda-item__owner small muted truncate', title: ownerName(item.owner) }, ownerName(item.owner))))))))
      : emptyState({
        icon: 'calendar',
        title: 'На две недели вперёд пусто',
        text: 'У шагов пока нет сроков. Поставьте дату у шага выше — он появится в повестке.',
      });

    return h('section', { class: 'card card--flush backlog__card agenda-card' },
      h('div', { class: 'backlog__card-head' },
        countHeader('Повестка', agenda.reduce((n, d) => n + d.items.length, 0), ['дела', 'дел', 'дел']),
        h('span', { class: 'small muted' }, agendaRange(14))),
      h('div', { class: 'backlog__card-body' }, body));
  }

  function paintAgenda() {
    const steps = partitionBacklog(visibleItems()).steps;
    paint('agenda', agendaHost, buildAgendaCard(), sigOf(steps, 'agenda'));
  }

  function closedRow(item) {
    return h('div', { class: 'closed-row' },
      h('span', { class: 'closed-row__icon' }, icon(item.status === 'done' ? 'check' : 'circle-slash', { size: 13 })),
      h('div', { class: 'closed-row__body' },
        h('div', { class: 'closed-row__text' + (item.status === 'dismissed' ? ' muted' : '') }, item.text),
        h('div', { class: 'closed-row__meta' },
          h('span', { class: 'chip chip--muted' }, kindLabel(item.kind)),
          item.due ? dueBadge(item.due) : null,
          sourceLink(roomId, item.source_request, item.source_title))),
      h('span', { class: 'chip ' + (item.status === 'done' ? 'chip--ok' : 'chip--muted') },
        item.status === 'done' ? 'выполнено' : 'убрано'));
  }

  function buildClosed() {
    const closed = partitionBacklog(visibleItems()).closed;
    const body = closed.length
      ? h('div', { class: 'closed-list' }, closed.map(closedRow))
      : emptyState({ icon: 'archive', title: 'Закрытых элементов нет', text: 'Выполненные и убранные шаги будут копиться здесь.' });
    return h('section', { class: 'card card--flush backlog__card closed' },
      h('div', { class: 'backlog__card-head' },
        countHeader('Закрытые', closed.length, ['элемент', 'элемента', 'элементов'])),
      h('div', { class: 'backlog__card-body' }, body));
  }

  function paintClosed() {
    closedLabel.textContent = state.showClosed ? 'Скрыть закрытые' : 'Показать закрытые';
    closedHost.hidden = !state.showClosed;
    if (!state.showClosed) {
      paint('closed', closedHost, h('div'), 'hidden');
      return;
    }
    const closed = partitionBacklog(visibleItems()).closed;
    paint('closed', closedHost, buildClosed(), sigOf(closed, 'closed'));
  }

  function paintError() {
    if (state.error) {
      errorHost.replaceChildren(errorBanner(state.error, () => load({ silent: false })));
    } else if (state.offline) {
      errorHost.replaceChildren(h('div', { class: 'banner banner--warn', role: 'status' },
        icon('warning', { size: 14 }),
        h('span', { class: 'banner__text' }, 'Нет связи с приложением. Показываю последние известные данные.')));
    } else {
      errorHost.replaceChildren();
    }
  }

  function paintAll() {
    paintError();
    paintFilter();
    paintInbox();
    paintSteps();
    paintFuture();
    paintClosed();
  }

  /* ---------------------------------------------------------- действия */

  async function mutate(itemId, body, okMessage) {
    state.busy.add(itemId);
    paintAll();
    try {
      await api.post(`/api/rooms/${encodeURIComponent(roomId)}/backlog/${encodeURIComponent(itemId)}`, body);
      if (okMessage) toast(okMessage, 'ok');
    } catch (err) {
      // Технические подробности пользователю не показываем (WAVE8, Блок B).
      toast(SAVE_FAILED, 'danger');
    } finally {
      state.busy.delete(itemId);
      await load({ silent: true });
    }
  }

  async function decide(item, status) {
    await mutate(item.id, { status }, status === 'done' ? 'Решение принято' : 'Решение отклонено');
  }

  async function askFuture(item) {
    state.busy.add(item.id);
    paintFuture();
    try {
      const res = await api.post(`/api/rooms/${encodeURIComponent(roomId)}/backlog/${encodeURIComponent(item.id)}/ask`, {});
      const rootId = (res && (res.root_id || (res.request && res.request.id))) || null;
      toast('Запрос создан', 'ok');
      if (rootId) navigate(requestHref(roomId, rootId));
      else ctx.refresh();
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось создать запрос', 'danger');
    } finally {
      state.busy.delete(item.id);
      await load({ silent: true });
    }
  }

  /* ------------------------------------------------------------- загрузка */

  async function load({ silent = false } = {}) {
    if (disposed) return;
    const seq = ++loadSeq;
    if (!silent) {
      inboxHost.replaceChildren(skeleton(3));
      stepsHost.replaceChildren(skeleton(4));
      futureHost.replaceChildren(skeleton(2));
      agendaHost.replaceChildren(skeleton(2));
    }
    try {
      const data = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/backlog`, { signal: abort.signal });
      let requests = state.requests;
      try {
        const list = await api.get(`/api/rooms/${encodeURIComponent(roomId)}/requests?limit=50`, { signal: abort.signal });
        requests = Array.isArray(list && list.items) ? list.items : [];
      } catch (err) {
        if (!silent || !state.requests.length) throw err;
      }

      const needsIds = requests
        .filter((s) => s && !s.archived && (s.needs_you || s.status === 'needs_you'))
        .map((s) => String(s.id));
      const key = needsIds.join(',');
      let blocked = state.blocked;
      if (key !== state.needsKey || (needsIds.length && Date.now() - detailsAt > 15000)) {
        detailsAt = Date.now();
        const details = [];
        for (const rid of needsIds) {
          try {
            details.push(await api.get(`/api/requests/${encodeURIComponent(rid)}`, { signal: abort.signal }));
          } catch { /* одна задача не должна ломать inbox */ }
        }
        blocked = blockedCards(details);
      }
      if (disposed || seq !== loadSeq) return;

      state.data = data || { items: [], counts: {} };
      state.requests = requests;
      state.needsKey = key;
      state.blocked = blocked.filter((c) => !state.answered.has(c.task_id));
      state.error = null;
      state.offline = false;
      paintAll();
    } catch (err) {
      if (disposed || seq !== loadSeq || (err && err.name === 'AbortError')) return;
      if (!silent || !state.data) {
        state.error = err;
        paintError();
        if (!state.data) {
          inboxHost.replaceChildren();
          stepsHost.replaceChildren();
          futureHost.replaceChildren();
          agendaHost.replaceChildren();
          closedHost.replaceChildren();
        }
      } else {
        state.offline = true;
        paintError();
      }
    }
  }

  await load({ silent: false });

  poller = pollApi(() => load({ silent: true }), POLL_MS, {
    pauseHidden: true,
    immediate: false,
    onError: () => { /* ошибки уже показаны баннером */ },
  });

  return {
    unmount() {
      disposed = true;
      abort.abort();
      if (poller && typeof poller.stop === 'function') poller.stop();
      root.remove();
    },
  };
}
