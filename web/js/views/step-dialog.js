/**
 * step-dialog.js — окно шага плана руководителя (WAVE14, пакет H §2–§4).
 *
 * Замечание Михаила (п. 12): «Следующие шаги» — план РУКОВОДИТЕЛЯ, а не список
 * дел пользователя. Галочек нет: статус шага ведут руководитель и система
 * (предстоит / в работе / сделано / снят), а пользователь может ВЛИЯТЬ на план —
 * подтвердить («Согласен»), убрать («Не согласен — убрать»), прокомментировать
 * (с диктовкой) или переместить (порядок, срок, исполнитель).
 *
 * Любое действие уходит руководителю КАК ОТВЕТ НА ЭТОТ ШАГ (тот же механизм, что
 * у вопросов: `POST /api/requests/{rid}/actions {action:"step_*", step_id}`) и
 * сразу показывает видимую реакцию «принято / учтено / срок изменён / шаг убран»,
 * а до реакции — «руководитель ещё не ответил».
 *
 * Окно доступно с клавиатуры: Esc закрывает, Tab ходит по кнопкам, первый фокус —
 * на заголовке. Стили — `web/css/view-steps.css`.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { enhanceAll } from '../dictation-auto.js';
import { dueChipLabel } from './backlog-model.js';
import { participantLabel } from '../participant-label.js';

const ACTIONS = [
  { kind: 'agree', label: 'Согласен', tone: 'primary', title: 'Подтвердить шаг: руководитель увидит ответ' },
  { kind: 'comment', label: 'Прокомментировать', tone: 'secondary', title: 'Написать или надиктовать комментарий к шагу' },
  { kind: 'move', label: 'Переместить', tone: 'secondary', title: 'Изменить порядок, срок или исполнителя' },
  { kind: 'drop', label: 'Не согласен — убрать', tone: 'ghost', title: 'Убрать шаг из плана' },
];

const STATE_RU = {
  open: 'предстоит', asked: 'в работе', done: 'сделано',
  dismissed: 'убран', cancelled: 'снят по вашему ответу',
};

/** Подпись состояния шага: конфликт важнее служебного статуса. */
export function stepState(step) {
  const s = step || {};
  if (s.conflicts_with_answer) {
    return { key: 'conflict', label: 'противоречит вашему ответу', tone: 'warn' };
  }
  const key = String(s.state || 'open');
  return { key, label: s.state_label || STATE_RU[key] || 'предстоит', tone: toneOf(key) };
}

function toneOf(key) {
  if (key === 'done') return 'ok';
  if (key === 'dismissed' || key === 'cancelled') return 'muted';
  if (key === 'asked') return 'info';
  return 'outline';
}

/** Подпись состояния для строки списка (короткая). */
export function stateLabel(step) {
  return stepState(step).label;
}

/** Шаги по группам: «Дальше», «Сделано», «Снято» (п. 12: выполненные отделены). */
export function splitSteps(steps) {
  const list = Array.isArray(steps) ? steps.filter((s) => s && typeof s === 'object') : [];
  const current = [];
  const done = [];
  const removed = [];
  for (const step of list) {
    const key = stepState(step).key;
    if (key === 'done') done.push(step);
    else if (key === 'dismissed' || key === 'cancelled') removed.push(step);
    else current.push(step);
  }
  return { current, done, removed, all: list };
}

/** Строка «что происходит по шагу»: реакция руководителя или «ещё не ответил». */
export function stepReactionLine(step) {
  const s = step || {};
  const reaction = s.reaction && typeof s.reaction === 'object' ? s.reaction : null;
  if (reaction) {
    const cards = Array.isArray(reaction.cards) ? reaction.cards : [];
    return {
      kind: 'reaction',
      label: s.reaction_label || 'принято',
      text: reaction.text || '',
      cards,
    };
  }
  if (s.reaction_pending) {
    return { kind: 'pending', label: 'руководитель ещё не ответил', text: '', cards: [] };
  }
  if (String(s.state || '') === 'dismissed') {
    return { kind: 'reaction', label: 'шаг убран', text: '', cards: [] };
  }
  return null;
}

/** «Зачем нужен шаг»: вопрос и ответ, из которых он возник (п. 12). */
export function stepWhy(step) {
  const s = step || {};
  const question = s.question && typeof s.question === 'object' ? s.question : null;
  if (!question && !s.answer) {
    return { question: null, answer: null, text: 'Руководитель не указал источник — уточните в комментарии.' };
  }
  return { question, answer: s.answer || null, text: '' };
}

/** Подпись исполнителя шага: имя и должность из описания профиля (пакет G). */
export function stepOwner(step, room) {
  const profile = String((step && step.owner) || '').trim();
  if (!profile) return { profile: '', title: 'не назначен', short: 'не назначен', role: '' };
  if (profile === 'mikhail') return { profile, title: 'Михаил', short: 'Михаил', role: '' };
  const label = participantLabel(profile, { room });
  return { profile, title: label.title || profile, short: label.short || profile, role: label.role || '' };
}

/** Тело запроса «Переместить»: только то, что пользователь действительно изменил. */
export function movePayload({ due, owner, direction } = {}, step = {}) {
  const body = { action: 'step_move', step_id: (step && step.id) || undefined };
  const wasDue = String((step && step.due) || '');
  if (due !== undefined && String(due || '') !== wasDue) body.due = due || null;
  const wasOwner = String((step && step.owner) || '');
  // Пустое значение не отправляем: случайно «обнулить исполнителя» нельзя —
  // снять исполнителя можно отдельным действием руководителя, не кликом в окне.
  if (owner !== undefined && String(owner || '').trim() && String(owner) !== wasOwner) {
    body.owner = owner;
  }
  if (direction === 'up' || direction === 'down') body.direction = direction;
  if (!('due' in body) && !('owner' in body) && !body.direction) return null;
  return body;
}

const TONE_TOAST = {
  agree: 'Отправлено руководителю: согласовано',
  comment: 'Комментарий ушёл руководителю',
  move: 'Изменение ушло руководителю',
  drop: 'Шаг убран',
};

function mountTarget(opts) {
  const target = (opts && opts.root) || (globalThis.document && globalThis.document.body) || null;
  return target;
}

function firstFocusable(dialog) {
  try {
    if (!dialog || typeof dialog.querySelector !== 'function') return null;
    return dialog.querySelector('.stepdlg__act');
  } catch { return null; }
}

/**
 * Открыть окно шага.
 * @param {{step?: object, stepId?: string, requestId: string, roomId?: string,
 *          api: object, toast?: Function, room?: object, root?: Element,
 *          onDone?: Function}} opts
 * @returns {{close: Function, dialog: HTMLElement, overlay: HTMLElement, reload: Function}|null}
 */
export function openStepDialog(opts) {
  const o = opts || {};
  if (!o.api || typeof o.api.post !== 'function') return null;
  ensureCss('css/view-steps.css');
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const room = o.room || null;
  let step = o.step && typeof o.step === 'object' ? o.step : null;
  const requestId = String(o.requestId || '');

  const overlay = h('div', { class: 'dialog-backdrop stepdlg-backdrop', 'data-step-dialog': '' });
  const dialog = h('div', {
    class: 'dialog stepdlg', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'stepdlg-title',
  });
  overlay.appendChild(dialog);

  const close = () => {
    const parent = overlay.parentNode;
    if (!parent) return;
    if (typeof parent.removeChild === 'function') parent.removeChild(overlay);
    else if (Array.isArray(parent.childNodes)) {
      const index = parent.childNodes.indexOf(overlay);
      if (index >= 0) parent.childNodes.splice(index, 1);
      overlay.parentNode = null;
    }
  };
  const onKey = (ev) => {
    if (!ev) return;
    if (ev.key === 'Escape') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
      close();
    }
  };
  overlay.addEventListener('keydown', onKey);
  overlay.addEventListener('mousedown', (ev) => {
    if (ev && ev.target === overlay) close();
  });

  const head = h('div', { class: 'dialog__head' },
    h('h2', { class: 'dialog__title', id: 'stepdlg-title', tabindex: '-1' }, 'Шаг плана'),
    h('button', {
      class: 'btn btn--ghost btn--sm stepdlg__close', type: 'button',
      'aria-label': 'Закрыть окно шага', onclick: close,
    }, icon('close', { size: 13 })));
  const body = h('div', { class: 'dialog__body stepdlg__body' });
  const foot = h('div', { class: 'dialog__foot stepdlg__foot' });
  dialog.append(head, body, foot);

  /** Перерисовать содержимое окна (после действия данные шага обновились). */
  function paint(result = null) {
    body.replaceChildren();
    foot.replaceChildren();
    if (!step) {
      body.appendChild(h('div', { class: 'stepdlg__loading muted' },
        'Загружаем шаг…'));
      return;
    }
    const state = stepState(step);
    const owner = stepOwner(step, room);
    const due = step.due ? dueChipLabel(String(step.due), new Date()) : 'без срока';
    const why = stepWhy(step);

    const blocks = [
      h('div', { class: 'stepdlg__top row row--between row--wrap row--tight' },
        h('span', { class: 'chip chip--' + state.tone, 'data-step-state': state.key }, state.label),
        step.conflicts_with_answer
          ? h('span', { class: 'chip chip--warn' }, 'шаг появился после вашего ответа')
          : null),
      h('div', { class: 'stepdlg__text' }, step.text || ''),
      h('dl', { class: 'stepdlg__meta' },
        metaRow('Исполнитель',
          h('span', { class: 'stepdlg__owner' },
            h('span', { class: 'stepdlg__owner-name' }, owner.title),
            owner.profile && owner.profile !== owner.title
              ? h('span', { class: 'stepdlg__owner-sub small muted' }, owner.profile) : null)),
        metaRow('Срок', h('span', {}, due)),
        metaRow('Статус', h('span', {}, STEP_STATUS_TEXT(step))),
        metaRow('Зачем нужен', h('span', {}, why.question
          ? h('span', {}, 'из вопроса: «', why.question.text || '', '»')
          : why.text))),
      why.answer
        ? h('div', { class: 'stepdlg__why small' },
          h('div', { class: 'stepdlg__why-k muted' }, 'Ваш ответ, из которого возник шаг'),
          h('div', { class: 'stepdlg__why-v' }, why.answer))
        : null,
      reactionStrip(step),
      historyBlock(step),
      h('div', { class: 'stepdlg__forms', 'data-step-forms': '' }),
    ].filter(Boolean);
    // append() превращает null в строку «null» — поэтому только готовые узлы.
    body.append(...blocks);

    for (const action of ACTIONS) {
      if (action.kind === 'agree' && step.can_agree === false) continue;
      if (action.kind === 'drop' && step.can_drop === false) continue;
      foot.appendChild(h('button', {
        class: 'btn btn--' + action.tone + ' btn--sm stepdlg__act',
        type: 'button',
        'data-step-action': action.kind,
        title: action.title,
        onclick: () => startAction(action.kind),
      }, action.label));
    }
    if (result && result.reaction_label) {
      body.appendChild(h('div', { class: 'stepdlg__result', role: 'status' },
        h('span', { class: 'stepdlg__result-k small muted' }, 'Руководителю'),
        h('span', { class: 'stepdlg__result-v' }, result.reaction_label)));
    }
    const focus = firstFocusable(dialog);
    if (focus && typeof focus.focus === 'function') {
      try { focus.focus(); } catch { /* фокус не критичен */ }
    }
  }

  function metaRow(label, value) {
    return h('div', { class: 'stepdlg__row' },
      h('dt', { class: 'stepdlg__k' }, label),
      h('dd', { class: 'stepdlg__v' }, value));
  }

  function reactionStrip(current) {
    const line = stepReactionLine(current);
    if (!line) {
      return h('div', { class: 'stepdlg__pending small muted' }, STEPS_PENDING_HINT);
    }
    return h('div', { class: 'stepdlg__reaction stepdlg__reaction--' + line.kind, role: 'status' },
      h('span', { class: 'chip chip--' + (line.kind === 'pending' ? 'warn' : 'ok') }, line.label),
      line.text ? h('span', { class: 'stepdlg__reaction-text' }, line.text) : null,
      line.cards.length
        ? h('span', { class: 'stepdlg__reaction-cards small muted' }, 'карточки: ' + line.cards.join(', '))
        : null);
  }

  function historyBlock(current) {
    const events = Array.isArray(current.history) ? current.history.slice().reverse() : [];
    const det = h('details', { class: 'stepdlg__history' },
      h('summary', {}, 'История шага', h('span', { class: 'tnum' }, events.length ? ' · ' + events.length : '')));
    if (!events.length) {
      det.appendChild(h('div', { class: 'small muted stepdlg__history-empty' },
        'Пока ничего не менялось: шаг создан руководителем, действий по нему нет.'));
      return det;
    }
    det.appendChild(h('ol', { class: 'stepdlg__history-list' },
      events.map((event) => h('li', { class: 'stepdlg__history-item' },
        h('span', { class: 'stepdlg__history-label' }, event.label || event.kind || ''),
        event.text ? h('span', { class: 'stepdlg__history-text' }, ' — ' + event.text) : null,
        event.due ? h('span', { class: 'small muted' }, ' · срок ' + event.due) : null,
        event.owner ? h('span', { class: 'small muted' }, ' · ' + event.owner) : null))));
    return det;
  }

  /* ── формы действий ────────────────────────────────────────── */

  function formsHost() {
    return body.querySelector ? body.querySelector('.stepdlg__forms') : null;
  }

  function startAction(kind) {
    const host = formsHost() || body;
    host.replaceChildren();
    if (kind === 'agree') { submit('agree', {}); return; }
    if (kind === 'drop') { dropConfirm(host); return; }
    if (kind === 'comment') { commentForm(host); return; }
    if (kind === 'move') { moveForm(host); }
  }

  function dropConfirm(host) {
    let armed = false;
    const btn = h('button', {
      class: 'btn btn--danger btn--sm', type: 'button', 'data-step-confirm': 'drop',
      onclick: () => {
        if (!armed) {
          armed = true;
          btn.textContent = 'Точно убрать шаг?';
          return;
        }
        submit('drop', {});
      },
    }, 'Не согласен — убрать');
    host.append(h('div', { class: 'stepdlg__form stepdlg__form--drop' },
      h('div', { class: 'small muted' },
        'Шаг уйдёт из плана, руководитель получит ваш ответ «убрать шаг» и объяснит решение.'),
      btn));
  }

  function commentForm(host) {
    const ta = h('textarea', {
      class: 'textarea stepdlg__comment', rows: 3,
      placeholder: 'Что поправить в шаге…',
      'aria-label': 'Комментарий к шагу: ' + String(step.text || ''),
    });
    const send = h('button', {
      class: 'btn btn--primary btn--sm', type: 'button', 'data-step-send': 'comment',
      onclick: () => {
        const text = String(ta.value || '').trim();
        if (!text) { if (typeof ta.focus === 'function') ta.focus(); return; }
        submit('comment', { text });
      },
    }, 'Отправить');
    host.append(h('div', { class: 'stepdlg__form stepdlg__form--comment' }, ta,
      h('div', { class: 'row row--tight row--wrap stepdlg__form-foot' }, send,
        h('span', { class: 'small muted' }, 'Комментарий уйдёт руководителю как ответ на этот шаг'))));
    announceMic(host);
  }

  function moveForm(host) {
    const dueInput = h('input', {
      class: 'input', type: 'date', value: String(step.due || ''),
      'aria-label': 'Новый срок шага',
    });
    const members = room && Array.isArray(room.members)
      ? room.members.map((m) => String((m && m.profile) || '')).filter(Boolean) : [];
    const owners = [...new Set([String(step.owner || ''), ...members, 'default'].filter(Boolean))];
    const ownerSelect = h('select', { class: 'input', 'aria-label': 'Исполнитель шага' },
      owners.map((name) => {
        const label = stepOwner({ owner: name }, room);
        const option = h('option', { value: name }, label.title);
        if (name === String(step.owner || '')) option.setAttribute('selected', 'selected');
        return option;
      }));
    const save = h('button', {
      class: 'btn btn--primary btn--sm', type: 'button', 'data-step-send': 'move',
      onclick: () => {
        const bodyReq = movePayload({
          due: dueInput.value || null,
          owner: ownerSelect.value || null,
        }, step);
        if (!bodyReq) { toast('Ничего не изменилось', 'warn'); return; }
        submit('move', bodyReq);
      },
    }, 'Сохранить');
    host.append(h('div', { class: 'stepdlg__form stepdlg__form--move' },
      h('label', { class: 'stepdlg__field' }, h('span', { class: 'stepdlg__field-k' }, 'Срок'), dueInput),
      h('label', { class: 'stepdlg__field' }, h('span', { class: 'stepdlg__field-k' }, 'Исполнитель'), ownerSelect),
      h('div', { class: 'row row--tight row--wrap' },
        h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button', 'data-step-send': 'up',
          title: 'Поднять шаг выше', onclick: () => submit('move', { action: 'step_move', step_id: step.id, direction: 'up' }),
        }, icon('chevron-up', { size: 12 }), 'Выше'),
        h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button', 'data-step-send': 'down',
          title: 'Опустить шаг ниже', onclick: () => submit('move', { action: 'step_move', step_id: step.id, direction: 'down' }),
        }, icon('chevron-down', { size: 12 }), 'Ниже'),
        save),
      h('span', { class: 'small muted' }, 'Изменение уйдёт руководителю как ответ на этот шаг')));
  }

  /** Поставить микрофон в поле комментария общим правилом (dictation-auto). */
  function announceMic(host) {
    try {
      if (typeof enhanceAll === 'function') enhanceAll(host);
    } catch { /* без микрофона окно работает */ }
  }

  /* ── отправка действия ─────────────────────────────────────── */

  async function submit(kind, extra) {
    const payload = Object.assign({ action: 'step_' + kind, step_id: step && step.id }, extra || {});
    setBusy(true);
    try {
      const res = await o.api.post(`/api/requests/${encodeURIComponent(requestId)}/actions`, payload);
      if (res && res.step && typeof res.step === 'object') step = Object.assign({}, step, res.step);
      else step = Object.assign({}, step, { reaction_pending: kind !== 'drop' });
      if (kind === 'drop') Object.assign(step, { state: 'dismissed', state_label: 'убран' });
      paint(res || null);
      toast(TONE_TOAST[kind] || 'Отправлено руководителю', kind === 'drop' ? 'warn' : 'ok');
      if (typeof o.onDone === 'function') {
        try { o.onDone(res, step); } catch (err) { console.warn('[шаг] onDone:', err); }
      }
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось отправить действие по шагу', 'danger');
      setBusy(false);
    }
  }

  function setBusy(busy) {
    if (!foot.childNodes) return;
    for (const node of foot.childNodes) {
      if (node && node.nodeType === 1 && node.tagName === 'button') node.disabled = !!busy;
    }
    if (body.querySelectorAll) {
      try {
        for (const node of body.querySelectorAll('button')) node.disabled = !!busy;
      } catch { /* мини-DOM */ }
    }
  }

  paint();
  const target = mountTarget(o);
  if (target && typeof target.appendChild === 'function') target.appendChild(overlay);
  const first = firstFocusable(dialog);
  if (first && typeof first.focus === 'function') {
    try { first.focus(); } catch { /* не критично */ }
  }

  // Шага в данных может не быть (окно открыто из «Дальше»/карты) — загружаем.
  if (!step && requestId) {
    Promise.resolve(o.api.get(`/api/requests/${encodeURIComponent(requestId)}`))
      .then((detail) => {
        const list = detail && Array.isArray(detail.next_steps) ? detail.next_steps : [];
        step = list.find((s) => String(s.id) === String(o.stepId)) || null;
        paint();
      })
      .catch((err) => {
        paint();
        toast(err && err.message ? err.message : 'Не удалось открыть шаг', 'danger');
      });
  }

  return { close, dialog, overlay, reload: () => paint(), get step() { return step; } };
}

/** Подпись статуса простыми словами (в окне шага). */
export function STEP_STATUS_TEXT(step) {
  const state = stepState(step);
  if (state.key === 'done') return 'сделано — руководитель видел готовые карточки';
  if (state.key === 'asked') return 'в работе';
  if (state.key === 'dismissed') return 'убран из плана';
  if (state.key === 'cancelled') return 'снят по вашему ответу';
  if (state.key === 'conflict') return 'противоречит вашему ответу';
  return 'предстоит: статус ведёт руководитель';
}

export const STEPS_PENDING_HINT = 'Действий по шагу ещё не было: статус ведёт руководитель комнаты.';

export default { openStepDialog, stepState, splitSteps, stepReactionLine, stepWhy, stepOwner, movePayload };
