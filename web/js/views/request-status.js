/**
 * request-status.js — блок статуса в шапке задачи (W31-B2, дефект 30).
 *
 * Зачем: на доске «Задачи» задачу можно принять и вернуть, а открыв саму задачу —
 * нельзя: человек видел «На проверке» и не мог ничего сделать. Здесь тот же набор
 * действий, что на доске (волна 31, контракт §): «Принять» (отмена 10 с),
 * «Вернуть на доработку» (с комментарием), «Вернуть в работу», «Отправить на
 * проверку» — ровно те, что сервер объявил в `allowed_moves`.
 *
 * Правила (контракт волны 31):
 *   * колонки доски — `new` «Новая», `working` «В работе», `review` «На проверке»,
 *     `done` «Готово»;
 *   * перенос — `POST /api/requests/{id}/actions {action:'move', to, comment?, confirm?}`;
 *   * при открытых вопросах сервер сначала отвечает `{needs_confirm:true, open_questions:N}`,
 *     повтор с `confirm:true` принимает;
 *   * «Принять» отложено на 10 секунд общим компонентом `undo.js` — как на доске.
 *
 * Модуль без DOM-зависимостей сверх `h()`: проверяется в
 * `node --test tests/w31-b2-request-window.test.mjs`.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { confirmDialog, promptDialog } from '../ui.js';
import { undo as sharedUndo } from '../undo.js';

/** Подписи колонок доски — один словарь на страницу задачи. */
export const BOARD_LABEL = {
  new: 'Новая',
  working: 'В работе',
  review: 'На проверке',
  done: 'Готово',
};

/** Тон чипа статуса. */
export const BOARD_TONE = {
  new: 'muted',
  working: 'info',
  review: 'warn',
  done: 'ok',
};

/** Старые стадии/статусы → колонка доски (пока сервер не прислал `board_column`). */
const STAGE_TO_COLUMN = {
  new: 'new',
  working: 'working',
  needs_you: 'working',
  scouting: 'working',
  overview_ready: 'review',
  review: 'review',
  done_system: 'review',
  accepted: 'done',
  archived: 'done',
};

/**
 * Колонка доски задачи.
 *
 * Источник правды — `detail.board_column` (сервер волны 31). Пока пакет A1 не
 * готов, выводим из `stage`/`status`, чтобы блок не врал на старом сервере.
 *
 * @param {object} detail ответ `GET /api/requests/{id}`
 * @returns {'new'|'working'|'review'|'done'}
 */
export function boardColumnOf(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const direct = String(d.board_column || '').trim().toLowerCase();
  if (BOARD_LABEL[direct]) return direct;
  const stage = String((d.stage && d.stage.id) || d.stage || '').trim().toLowerCase();
  if (STAGE_TO_COLUMN[stage]) return STAGE_TO_COLUMN[stage];
  const status = String(d.status || '').trim().toLowerCase();
  if (status === 'done') return 'review';       // работа готова — ждёт приёмки
  if (status === 'new') return 'new';
  if (status === 'archived') return 'done';
  return 'working';
}

/**
 * Что сервер разрешает сделать с задачей.
 *
 * `allowed_moves` — единственный источник; без него считаем по колонке (те же
 * правила, что в контракте: review→done/working, done→working, working→review).
 *
 * @param {object} detail
 * @returns {string[]} список `to`
 */
export function allowedMovesOf(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const list = Array.isArray(d.allowed_moves) ? d.allowed_moves : null;
  if (list) {
    const out = [];
    for (const raw of list) {
      const to = String(raw || '').trim().toLowerCase();
      if (BOARD_LABEL[to] && !out.includes(to)) out.push(to);
    }
    return out;
  }
  const column = boardColumnOf(d);
  if (column === 'new') return ['working'];
  if (column === 'working') return ['review'];
  if (column === 'review') return ['done', 'working'];
  if (column === 'done') return ['working'];
  return [];
}

/** Сколько открытых вопросов (для предупреждения «принять с вопросами»). */
export function openQuestionsOf(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const list = Array.isArray(d.open_questions) ? d.open_questions : [];
  return list.filter((q) => q && !q.answered && !q.dismissed && String(q.state || 'open') === 'open').length;
}

/**
 * Кнопки блока статуса — из `allowed_moves`, без догадок.
 *
 * @param {object} detail
 * @returns {Array<{to:string, id:string, label:string, variant:string, comment?:boolean,
 *                  confirmUndo?:boolean, hint:string}>}
 */
export function statusActionsFor(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const status = String(d.status || '').toLowerCase();
  // Упавшая задача: статус ведёт повтор, а не перенос по доске.
  if (status === 'failed' || status === 'archived') return [];
  const column = boardColumnOf(d);
  const moves = allowedMovesOf(d);
  const out = [];
  for (const to of moves) {
    if (to === 'done' && column === 'review') {
      out.push({
        id: 'accept', to: 'done', label: 'Принять', variant: 'primary',
        confirmUndo: true,
        hint: 'Принять задачу: работа замораживается, результат идёт в память. 10 секунд можно передумать.',
      });
      continue;
    }
    if (to === 'working' && column === 'review') {
      out.push({
        id: 'rework', to: 'working', label: 'Вернуть на доработку', variant: 'secondary',
        comment: true,
        hint: 'Вернуть исполнителю с комментарием: что поправить',
      });
      continue;
    }
    if (to === 'working' && column === 'done') {
      out.push({
        id: 'reopen', to: 'working', label: 'Вернуть в работу', variant: 'secondary',
        hint: 'Снять приёмку: задача снова в работе, вопросы остаются',
      });
      continue;
    }
    if (to === 'review' && column === 'working') {
      out.push({
        id: 'send_review', to: 'review', label: 'Отправить на проверку', variant: 'ghost',
        hint: 'Отдать работу на проверку — принять её сможете из «На проверке»',
      });
      continue;
    }
    if (to === 'working' && column === 'new') {
      out.push({
        id: 'start', to: 'working', label: 'Взять в работу', variant: 'secondary',
        hint: 'Начать работу по задаче',
      });
    }
  }
  return out;
}

/** Чип статуса: «На проверке» и т. п. */
export function statusChipOf(column, opts = {}) {
  const c = BOARD_LABEL[column] ? column : 'working';
  return h('span', {
    class: ['chip', 'chip--' + (BOARD_TONE[c] || 'muted'), 'req-status__chip'],
    'data-board-column': c,
    title: opts.hint || 'Колонка задачи на доске «Задачи»',
  }, icon('circle-filled', { size: 8 }), BOARD_LABEL[c]);
}

/**
 * Собрать блок статуса для шапки задачи.
 *
 * @param {object} opts
 * @param {object} opts.detail     ответ `GET /api/requests/{id}`
 * @param {Function} opts.onMove   `(to, {comment, confirm}) => Promise<{ok, needs_confirm, open_questions}>`
 * @param {object} [opts.undo]     общий `undo.js` (тесты подставляют свой)
 * @param {Function} [opts.toast]  сообщение человеку
 * @param {string} [opts.requestId]
 * @returns {HTMLElement}
 */
export function buildStatusBlock(opts = {}) {
  const o = opts || {};
  const detail = o.detail || {};
  const undo = o.undo || sharedUndo;
  const say = typeof o.toast === 'function' ? o.toast : () => {};
  const onMove = typeof o.onMove === 'function' ? o.onMove : async () => ({ ok: false });
  const column = boardColumnOf(detail);
  const actions = statusActionsFor(detail);
  const requestId = String(o.requestId || detail.id || '');

  const row = h('div', {
    class: 'req-status', 'data-status-block': column,
    role: 'group', 'aria-label': 'Статус задачи и действия',
  },
  h('span', { class: 'req-status__label' }, 'Статус'),
  statusChipOf(column));

  /** «Принять»: с предупреждением об открытых вопросах и отменой 10 с. */
  function accept(btn) {
    const open = openQuestionsOf(detail);
    const run = () => {
      const key = 'accept:' + requestId;
      if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
      undo.offer({
        key,
        text: 'Задача будет принята',
        action: async () => {
          const res = await onMove('done', { confirm: true });
          if (!res || res.ok === false) {
            say((res && res.message) || 'Не удалось принять задачу', 'danger');
          }
        },
        onUndo: () => {
          if (btn) { btn.disabled = false; btn.removeAttribute('aria-busy'); }
          say('Задача осталась как была — ничего не изменилось', 'ok');
        },
      });
    };
    if (!open) { run(); return; }
    confirmDialog({
      title: 'Принять задачу с открытыми вопросами?',
      text: 'По задаче ещё висят вопросы: ' + open + '. При принятии они закроются с пометкой «задача принята». '
        + 'Вернуть задачу в работу можно будет кнопкой «Вернуть в работу».',
      confirmLabel: 'Принять',
      cancelLabel: 'Отмена',
    }).then((ok) => { if (ok) run(); });
  }

  /** «Вернуть на доработку»: комментарий, что поправить. */
  async function rework(btn) {
    const comment = await promptDialog({
      title: 'Вернуть на доработку',
      label: 'Что поправить (увидит руководитель)',
      placeholder: 'Например: нужен расчёт по двум поставщикам и срок поставки',
      multiline: true,
      submitLabel: 'Вернуть на доработку',
      cancelLabel: 'Отмена',
    });
    if (comment === null) return;
    await perform(btn, 'working', { comment: String(comment || '').trim() });
  }

  /** Остальные переносы — сразу, но с честным итогом. */
  async function perform(btn, to, extra = {}) {
    if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
    try {
      const res = await onMove(to, extra);
      if (res && res.needs_confirm) {
        const ok = await confirmDialog({
          title: 'Принять задачу с открытыми вопросами?',
          text: 'Открытых вопросов: ' + Number(res.open_questions || 0) + '. При принятии они закроются.',
          confirmLabel: 'Принять',
          cancelLabel: 'Отмена',
        });
        if (ok) await onMove(to, { ...extra, confirm: true });
        return;
      }
      if (!res || res.ok === false) say((res && res.message) || 'Действие не выполнено', 'danger');
    } finally {
      if (btn) { btn.disabled = false; btn.removeAttribute('aria-busy'); }
    }
  }

  for (const action of actions) {
    const btn = h('button', {
      type: 'button',
      class: ['btn', 'btn--' + action.variant, 'btn--sm', 'req-status__btn', 'req-status__btn--' + action.id],
      title: action.hint || null,
      'data-move': action.to,
    }, action.id === 'accept' ? icon('check', { size: 13 }) : null,
      h('span', null, action.label));
    btn.addEventListener('click', () => {
      if (action.confirmUndo) accept(btn);
      else if (action.comment) rework(btn);
      else perform(btn, action.to);
    });
    row.appendChild(btn);
  }

  if (!actions.length) {
    const failed = String(detail.status || '') === 'failed';
    row.appendChild(h('span', { class: 'req-status__hint small muted' },
      failed
        ? 'Задача не выполнена — повторить её можно в меню «Действия».'
        : 'Действий нет: статус ведут работа команды и ваши ответы.'));
  }
  return row;
}

export default { buildStatusBlock, boardColumnOf, allowedMovesOf, statusActionsFor, openQuestionsOf, BOARD_LABEL };
