/**
 * request-steps.js — блок «Следующие шаги» на странице запроса (WAVE14, пакет H).
 *
 * Замечание Михаила (п. 12): шаги строит РУКОВОДИТЕЛЬ, поэтому и выполнение
 * отмечает он (или система по факту готовности карточек). У пользователя
 * галочек нет: он видит карту «что сделано / что предстоит» и может ВЛИЯТЬ на
 * любой шаг — открыть окно шага и сказать «Согласен», «Не согласен — убрать»,
 * прокомментировать (с диктовкой) или переместить (порядок, срок, исполнитель).
 *
 * Состояния: предстоит / в работе / сделано / снят. Выполненные отделены
 * («Сделано»), предстоящие — «Дальше». Метка «противоречит вашему ответу» — из
 * пакета D (вопрос ↔ шаг, ответ «нет»).
 *
 * Вынесено из request-aside.js (файл колонки правят параллельно): снаружи —
 * только `createSteps(R)` и `stepsCard(...)`.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { avatar, ensureCss } from '../ui.js';
import { countNoun } from '../text-ru.js';
import { firstLine } from './request-util.js';
import { foldCard } from './aside-fold.js';
import { openStepDialog, splitSteps, stepReactionLine, stepOwner, stepState } from './step-dialog.js';

export const STEPS_HINT = 'План руководителя на ближайшее время: поправьте любой шаг';
const STEP_FORMS = ['шаг', 'шага', 'шагов'];

/** WAVE17 (B1): иконка состояния шага — статус читается без слов (P3). */
export const STEP_ICON = {
  open: 'circle-outline', asked: 'sync', done: 'pass-filled',
  dismissed: 'circle-slash', cancelled: 'circle-slash', conflict: 'warning',
};

export { splitSteps, stepState };

const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

/**
 * Короткая подпись срока для строки шага: «сегодня», «завтра», «просрочен»,
 * «14 ноя», «14 ноя 2027». Длинное «Через 4 дня» в узкой колонке обрезалось до
 * «Через…» — а срок нужен целиком (B1). Полная дата — в подсказке чипа.
 * @param {string} due  'YYYY-MM-DD'
 * @param {Date} [now]
 * @returns {string}
 */
export function stepDueLabel(due, now) {
  const raw = String(due || '');
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  const ref = now instanceof Date ? now : new Date();
  if (!m) return raw;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const today = new Date(ref.getFullYear(), ref.getMonth(), ref.getDate());
  const diff = Math.round((d.getTime() - today.getTime()) / 86400000);
  if (diff < 0) return 'просрочен';
  if (diff === 0) return 'сегодня';
  if (diff === 1) return 'завтра';
  const month = MONTHS_SHORT[d.getMonth()] || '';
  return d.getFullYear() === today.getFullYear()
    ? d.getDate() + ' ' + month
    : d.getDate() + ' ' + month + ' ' + d.getFullYear();
}

/** Шаг «сделан» по данным шага или карточке бэклога комнаты. */
export function isStepDone(step, roomItem) {
  return stepState(step).key === 'done' || (roomItem && roomItem.status === 'done');
}

/** Счётчик предстоящих шагов — для шапки блока. */
export function openStepsCount(steps) {
  return splitSteps(steps).current.length;
}

export function createSteps(R) {
  const { ctx } = R;
  // B1: стили шагов нужны и на странице запроса — раньше их подключали только
  // бэклог и окно шага, поэтому здесь оставалась сетка под чекбокс шириной 15 px.
  ensureCss('css/view-steps.css');

  function roomOf() {
    return (ctx && ctx.room) || null;
  }

  /** Строка-исполнитель: имя и должность (пакет G), техническое имя — мелко. */
  function ownerChip(step) {
    const owner = stepOwner(step, roomOf());
    if (!owner.profile) {
      return h('span', {
        class: 'req-step__owner req-step__owner--empty',
        title: 'Исполнитель не назначен — назначьте в окне шага',
      }, 'не назначен');
    }
    const el = avatar(owner.short || owner.profile, { size: 'sm' });
    el.textContent = (Array.from(String(owner.short || owner.profile))[0] || '?').toUpperCase();
    const name = owner.short || owner.profile;
    return h('span', { class: 'req-step__owner', title: 'Исполнитель: ' + (owner.title || owner.profile) },
      el,
      h('span', { class: 'req-step__owner-name', title: name }, name));
  }

  /** Одна строка шага: иконка статуса, название (2 строки + подсказка), исполнитель, срок. */
  function stepRow(step) {
    const state = stepState(step);
    const line = stepReactionLine(step);
    const roomItem = (R.backlog && R.detail) ? R.backlog.get(`${R.detail.id}:${step.id}`) : null;
    const done = state.key === 'done' || (roomItem && roomItem.status === 'done');
    const due = step.due || (roomItem && roomItem.due) || null;
    const text = String(step.text || '');

    const meta = h('div', { class: 'req-step__meta' });
    meta.append(ownerChip(step));
    if (due) {
      meta.append(h('span', {
        class: 'due-chip req-step__due',
        title: 'Срок: ' + due,
      }, icon('calendar', { size: 11 }), h('span', { class: 'due-chip__text' }, stepDueLabel(due, new Date()))));
    }

    // Строка шага: три колонки (статус, текст, исполнитель и срок). Прежняя
    // сетка 15 px была под чекбокс — именно из-за неё текст переносился
    // побуквенно и шаг занимал 900–1300 px (B1).
    const body = [
      h('span', {
        class: 'req-step__status req-step__status--' + state.key,
        title: 'Статус шага: ' + state.label,
      }, icon(STEP_ICON[state.key] || 'circle-outline', { size: 13 })),
      h('span', { class: 'req-step__text', title: text || null }, text),
      meta.childElementCount ? meta : h('span', { class: 'req-step__meta req-step__meta--empty' }),
    ];
    if (line) {
      body.push(h('span', { class: 'req-step__reaction req-step__reaction--' + line.kind },
        h('span', { class: 'req-step__reaction-label' }, line.label),
        line.text ? h('span', { class: 'req-step__reaction-text' }, ' · ' + firstLine(line.text, 120)) : null));
    }

    const open = h('div', {
      class: 'req-step__open',
      role: 'button',
      tabindex: '0',
      'data-step-id': step.id,
      'aria-haspopup': 'dialog',
      'aria-label': 'Шаг: ' + firstLine(text, 90) + ' — открыть окно шага. Статус: ' + state.label,
      onclick: () => openStep(step),
      onkeydown: (ev) => {
        const key = ev && ev.key;
        if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
          if (typeof ev.preventDefault === 'function') ev.preventDefault();
          openStep(step);
        }
      },
    }, body);

    const row = h('div', {
      class: ['req-step', 'req-step--' + state.key, done ? 'is-done' : '',
        step.conflicts_with_answer ? 'is-conflict' : ''],
      // `data-step` — прежняя привязка; `data-key` — ключ точечной перерисовки
      // (WAVE17 §1: строка живёт между обновлениями, пока шаг не изменился).
      dataset: { step: step.id, key: step.id },
    }, open);

    if (step.conflicts_with_answer) {
      row.append(h('div', { class: 'req-step__warn' },
        icon('warning', { size: 12 }),
        h('span', { class: 'req-step__warn-text' }, 'противоречит вашему ответу'),
        h('button', {
          class: 'btn btn--ghost btn--sm req-step__remove', type: 'button',
          title: 'Убрать шаг из плана', 'aria-label': 'Убрать шаг: ' + firstLine(text, 60),
          onclick: (ev) => {
            if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
            dropStep(step);
          },
        }, 'Убрать')));
    }
    return row;
  }

  /** Окно шага: что за шаг, кто и когда, зачем, статус, история, действия (§2–§3). */
  function openStep(step) {
    openStepDialog({
      step,
      requestId: R.detail && R.detail.id,
      roomId: R.roomId,
      room: roomOf(),
      api: ctx.api,
      toast: ctx.toast,
      onDone: () => { if (typeof R.refresh === 'function') R.refresh({ force: true }); },
    });
  }

  /** Быстрое «Убрать» у конфликтного шага — тоже уходит руководителю ответом на шаг. */
  async function dropStep(step) {
    try {
      await ctx.api.post(`/api/requests/${encodeURIComponent(R.detail.id)}/actions`,
        { action: 'step_drop', step_id: step.id });
      ctx.toast('Шаг убран — руководитель получит ваш ответ', 'warn');
      if (typeof R.refresh === 'function') await R.refresh({ force: true });
    } catch (err) {
      ctx.toast((err && err.message) || 'Не удалось убрать шаг', 'danger');
    }
  }

  function group(title, steps, className) {
    if (!steps.length) return null;
    return h('div', { class: 'req-steps__group ' + className },
      h('div', { class: 'req-steps__title small muted' }, title,
        h('span', { class: 'tnum' }, ' · ' + steps.length)),
      h('div', { class: 'req-side__body req-steps__list', 'data-scroll-key': 'steps-next' },
        steps.map(stepRow)));
  }

  function fold(title, steps, className) {
    if (!steps.length) return null;
    return h('details', { class: 'req-steps__fold ' + className },
      h('summary', { class: 'req-steps__fold-head' }, title,
        h('span', { class: 'tnum' }, ' · ' + steps.length)),
      h('div', { class: 'req-side__body req-steps__list' }, steps.map(stepRow)));
  }

  function stepsCard(steps) {
    const parts = splitSteps(steps);
    // Счётчик — только предстоящие: если всё сделано, число не показываем.
    const count = parts.current.length ? countNoun(parts.current.length, STEP_FORMS) : null;
    const body = [];
    if (!parts.all.length) {
      body.push(h('div', { class: 'req-side__body' },
        h('div', { class: 'small muted' }, 'Руководитель ещё не составил план по этому запросу.')));
    } else {
      const current = group('Дальше', parts.current, 'req-steps__group--next');
      if (current) body.push(current);
      const done = fold('Сделано', parts.done, 'req-steps__fold--done');
      if (done) body.push(done);
      const removed = fold('Снято', parts.removed, 'req-steps__fold--removed');
      if (removed) body.push(removed);

      const dueIds = parts.current
        .filter((step) => step.due)
        .map((step) => `${R.detail.id}:${step.id}`);
      if (dueIds.length) {
        body.push(h('div', { class: 'req-side__foot' },
          h('a', {
            class: 'btn btn--ghost btn--sm',
            href: `/api/ics?room=${encodeURIComponent(R.roomId)}&items=${dueIds.map(encodeURIComponent).join(',')}`,
            download: '',
            title: 'Все предстоящие шаги со сроками — в календарь (.ics)',
          }, icon('calendar', { size: 12 }), 'Все в календарь')));
      }
    }

    // Свёрнутый блок «Шаги» (P3): одна строка заголовка со счётчиком, состояние
    // запоминается (aside-fold.js). Внутри — те же строки шагов.
    const card = foldCard({
      rid: R.detail && R.detail.id,
      name: 'steps',
      title: 'Шаги',
      count,
      hint: STEPS_HINT,
      defaultOpen: false,
      className: 'req-steps',
      key: 'block:steps',
      body,
    });
    card.setAttribute('data-next-steps', '');
    return card;
  }

  return { stepsCard, stepRow, openStep, dropStep };
}

export default { createSteps, splitSteps, stepState, STEPS_HINT, openStepsCount, isStepDone };
