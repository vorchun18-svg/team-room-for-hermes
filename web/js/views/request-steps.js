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
import { avatar } from '../ui.js';
import { countNoun } from '../text-ru.js';
import { blockHead, firstLine } from './request-util.js';
import { dueChipLabel } from './backlog-model.js';
import { openStepDialog, splitSteps, stepReactionLine, stepOwner, stepState } from './step-dialog.js';

export const STEPS_HINT = 'План руководителя на ближайшее время. Можно поправить любой шаг';
const STEP_FORMS = ['шаг', 'шага', 'шагов'];

export { splitSteps, stepState };

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

  function roomOf() {
    return (ctx && ctx.room) || null;
  }

  /** Строка-исполнитель: имя и должность (пакет G), техническое имя — мелко. */
  function ownerChip(step) {
    const owner = stepOwner(step, roomOf());
    if (!owner.profile) {
      return h('span', { class: 'req-step__owner req-step__owner--empty small muted' }, 'исполнитель не назначен');
    }
    const el = avatar(owner.short || owner.profile, { size: 'sm' });
    el.textContent = (Array.from(String(owner.short || owner.profile))[0] || '?').toUpperCase();
    return h('span', { class: 'req-step__owner', title: 'Исполнитель: ' + (owner.title || owner.profile) },
      el,
      h('span', { class: 'req-step__owner-name' }, owner.short || owner.profile));
  }

  /** Одна строка шага: кликабельна целиком (кроме кнопки «Убрать» внутри). */
  function stepRow(step) {
    const state = stepState(step);
    const line = stepReactionLine(step);
    const roomItem = R.backlog ? R.backlog.get(`${R.detail.id}:${step.id}`) : null;
    const done = state.key === 'done' || (roomItem && roomItem.status === 'done');
    const due = step.due || (roomItem && roomItem.due) || null;

    const meta = h('div', { class: 'req-step__meta' });
    meta.append(ownerChip(step));
    if (due) {
      meta.append(h('span', {
        class: 'due-chip req-step__due',
        title: 'Срок: ' + due,
      }, icon('calendar', { size: 11 }), h('span', { class: 'due-chip__text' }, dueChipLabel(due, new Date()))));
    }
    if (state.key !== 'open' && !step.conflicts_with_answer) {
      meta.append(h('span', { class: 'chip chip--' + state.tone + ' req-step__state' }, state.label));
    }

    const body = h('div', { class: 'req-step__body' },
      h('div', { class: 'req-step__text' }, step.text || ''),
      meta.childElementCount ? meta : null,
      line ? h('div', { class: 'req-step__reaction req-step__reaction--' + line.kind },
        h('span', { class: 'req-step__reaction-label' }, line.label),
        line.text ? h('span', { class: 'req-step__reaction-text' }, ' · ' + firstLine(line.text, 120)) : null) : null);

    const open = h('div', {
      class: 'req-step__open',
      role: 'button',
      tabindex: '0',
      'data-step-id': step.id,
      'aria-haspopup': 'dialog',
      'aria-label': 'Шаг: ' + firstLine(step.text, 90) + ' — открыть окно шага',
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
      class: ['req-step', 'req-step--' + state.key, done ? 'is-done' : '', step.conflicts_with_answer ? 'is-conflict' : ''],
      dataset: { step: step.id },
    }, open);

    if (step.conflicts_with_answer) {
      row.append(h('div', { class: 'req-step__warn' },
        icon('warning', { size: 12 }),
        h('span', { class: 'req-step__warn-text' }, 'противоречит вашему ответу'),
        h('button', {
          class: 'btn btn--ghost btn--sm req-step__remove', type: 'button',
          title: 'Убрать шаг из плана', 'aria-label': 'Убрать шаг: ' + firstLine(step.text, 60),
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
      h('div', { class: 'req-side__body req-steps__list' }, steps.map(stepRow)));
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
    const card = h('section', { class: 'card req-side req-steps', 'data-next-steps': '' },
      blockHead('Следующие шаги', count));
    card.append(h('div', { class: 'req-side__hint small muted' }, STEPS_HINT));

    if (!parts.all.length) {
      card.append(h('div', { class: 'req-side__body' },
        h('div', { class: 'small muted' }, 'Руководитель ещё не составил план по этому запросу.')));
      return card;
    }
    const current = group('Дальше', parts.current, 'req-steps__group--next');
    if (current) card.append(current);
    const done = fold('Сделано', parts.done, 'req-steps__fold--done');
    if (done) card.append(done);
    const removed = fold('Снято', parts.removed, 'req-steps__fold--removed');
    if (removed) card.append(removed);

    const dueIds = parts.current
      .filter((step) => step.due)
      .map((step) => `${R.detail.id}:${step.id}`);
    if (dueIds.length) {
      card.append(h('div', { class: 'req-side__foot' },
        h('a', {
          class: 'btn btn--ghost btn--sm',
          href: `/api/ics?room=${encodeURIComponent(R.roomId)}&items=${dueIds.map(encodeURIComponent).join(',')}`,
          download: '',
          title: 'Все предстоящие шаги со сроками — в календарь (.ics)',
        }, icon('calendar', { size: 12 }), 'Все в календарь')));
    }
    return card;
  }

  return { stepsCard, stepRow, openStep, dropStep };
}

export default { createSteps, splitSteps, stepState, STEPS_HINT, openStepsCount, isStepDone };
