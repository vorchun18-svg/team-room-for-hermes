/**
 * decision-dialog.js — «Принять», «Отклонить», «Обсудить» решение руководителя
 * (WAVE19, пакет D2 §1, дефект B-20) и понятная цепочка статуса ответа (§2, B-21).
 *
 * Почему отдельным модулем: в правой колонке запроса решение было ТОЛЬКО текстом
 * с меткой «предложено» — принять или отклонить его было нельзя, хотя оно входило
 * в «Нужен ваш ответ» и в счётчик «Дальше». Теперь у решения те же действия, что
 * у вопроса и шага, и тот же механизм ответа руководителю:
 *
 *   POST /api/requests/{rid}/actions
 *        {action:"decision", decision_id, decision:"accept|reject|discuss", text?}
 *
 * «Принять» уходит сразу (одно нажатие), «Отклонить» и «Обсудить» открывают это
 * окно: в нём поле с диктовкой (микрофон ставит dictation-auto.js по общему
 * правилу) и кнопка действия. Исход реакции руководителя приходит с сервера
 * (`answer_state`: sent → received → applied/no_change) — клиент НЕ угадывает.
 *
 * Окно доступно с клавиатуры: Esc — закрыть, Tab — по кнопкам, первый фокус — на
 * заголовке. Стили — web/css/view-request.css (`.decdlg`, `.req-decision__*`).
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { enhanceAll } from '../dictation-auto.js';

/** Запасные подписи, если сервер старой версии и не прислал `decision.actions`. */
export const DECISION_ACTIONS = [
  { action: 'accept', label: 'Принять', hint: 'Согласиться с предложенным решением — руководитель увидит это сразу' },
  { action: 'reject', label: 'Отклонить', hint: 'Отклонить решение: можно сразу написать, что предложить вместо' },
  { action: 'discuss', label: 'Обсудить…', hint: 'Написать или надиктовать, что обсудить — решение останется за вами' },
];

/** Статус самого решения (кто его принял). */
export const DECISION_STATUS = {
  proposed: {
    label: 'предложено', tone: 'warn',
    hint: 'Руководитель предлагает так; решение за вами — «Принять», «Отклонить» или «Обсудить».',
  },
  accepted: {
    label: 'принято вами', tone: 'ok',
    hint: 'Вы приняли решение. Руководитель получил ваш ответ и учтёт его в работе.',
  },
  rejected: {
    label: 'отклонено вами', tone: 'muted',
    hint: 'Вы отклонили решение. Руководитель получил ответ и предложит другой вариант.',
  },
  discuss: {
    label: 'обсуждаем с руководителем', tone: 'info',
    hint: 'Вы просите обсудить: решение остаётся за вами, пока руководитель не ответит.',
  },
};

/**
 * Цепочка ответа (B-21) — ОДНИ И ТЕ ЖЕ слова, что отдаёт сервер
 * (`answers.STATE_RU`): «ответ отправлен» → «руководитель прочитал» → «учтено».
 */
export const ANSWER_STATE = {
  open: { label: 'ждёт вашего ответа', tone: 'muted' },
  waiting_explanation: { label: 'ждёт вашего решения после пояснения', tone: 'warn' },
  sent: {
    label: 'ответ отправлен', tone: 'warn',
    hint: 'Ответ ушёл руководителю. Пока он его не открыл и не отметил исход — ждите или нажмите «Напомнить руководителю».',
  },
  received: {
    label: 'руководитель прочитал', tone: 'info',
    hint: 'Руководитель прочитал ответ и взял его в работу. «Учтено» появится, когда он запишет, что именно изменил.',
  },
  applied: {
    label: 'учтено: работа изменена', tone: 'ok',
    hint: 'Руководитель учёл ответ: работа пересмотрена, карточки изменены. Их список — рядом с реакцией.',
  },
  no_change: {
    label: 'учтено: менять ничего не нужно', tone: 'muted',
    hint: 'Руководитель учёл ответ и решил, что менять в работе ничего не нужно.',
  },
};

/** Подсказка «что значит» для всей цепочки — одна на все экраны. */
export const CHAIN_HINT = 'Цепочка ответа: «ответ отправлен» → «руководитель прочитал» → '
  + '«учтено» (с изменением карточек или «менять ничего не нужно»). '
  + 'Исход указывает сам руководитель в метаданных реакции, а не угадывается по словам.';

/** Действия решения: с сервера (`decision.actions`), иначе — запасной список. */
export function decisionActions(decision) {
  const list = decision && Array.isArray(decision.actions) ? decision.actions : [];
  const known = DECISION_ACTIONS.map((fallback) => {
    const found = list.find((item) => item && String(item.action) === fallback.action);
    return {
      action: fallback.action,
      label: (found && found.label) || fallback.label,
      hint: (found && found.hint) || fallback.hint,
      current: Boolean(found && found.current),
    };
  });
  return known;
}

/** Статус решения с подсказкой (что он значит). */
export function decisionStatusOf(decision) {
  const d = decision || {};
  const key = String(d.status || 'proposed');
  const base = DECISION_STATUS[key] || DECISION_STATUS.proposed;
  // Решение мог закрыть сам руководитель в метаданных — не приписываем это Михаилу.
  const byLeader = String(d.status_source || '') === 'leader' && (key === 'accepted' || key === 'rejected');
  const leaderLabel = key === 'accepted' ? 'принято руководителем' : 'отклонено руководителем';
  return {
    key,
    label: d.status_label || (byLeader ? leaderLabel : base.label),
    tone: base.tone,
    hint: d.status_hint || (byLeader
      ? 'Руководитель отметил это решение сам; вашего ответа здесь не требовалось.'
      : base.hint),
  };
}

/** Ступень цепочки ответа: `sent`/`received`/`applied`/`no_change` (или null). */
export function decisionAnswerOf(decision) {
  const d = decision || {};
  const key = String(d.answer_state || '');
  // Цепочка ответа есть только там, где Михаил действительно отвечал.
  if (!key || key === 'open') return null;
  if (!d.answer_label && !d.answered && !d.reaction) return null;
  const base = ANSWER_STATE[key] || ANSWER_STATE.open;
  return {
    key,
    label: d.answer_label || base.label,
    tone: base.tone,
    hint: d.answer_hint || base.hint || CHAIN_HINT,
    text: d.answer_text || null,
    reaction: d.reaction && typeof d.reaction === 'object' ? d.reaction : null,
    pending: Boolean(d.leader_pending),
    overdue: Boolean(d.reaction_overdue),
    canRemind: Boolean(d.can_remind),
  };
}

/** Тело запроса действия над решением — ОДНО место (и для окна, и для кнопок). */
export function decisionPayload(decision, outcome, text = '') {
  const body = {
    action: 'decision',
    decision_id: String((decision && decision.id) || ''),
    decision: String(outcome || ''),
  };
  const value = String(text || '').trim();
  if (value) body.text = value;
  return body;
}

/**
 * Отправить действие над решением. Возвращает ответ сервера; бросает ошибку дальше —
 * вызывающий показывает её в тосте (`err.message` уже по-русски).
 */
export async function postDecision({ api, requestId, decision, outcome, text = '' } = {}) {
  if (!api || typeof api.post !== 'function') throw new Error('Нет связи с сервером');
  return api.post(`/api/requests/${encodeURIComponent(String(requestId || ''))}/actions`,
    decisionPayload(decision, outcome, text));
}

function outcomeMeta(outcome) {
  return DECISION_ACTIONS.find((item) => item.action === String(outcome)) || DECISION_ACTIONS[0];
}

/**
 * Окно «Отклонить» / «Обсудить» (с диктовкой). «Принять» окна не требует — это
 * одно нажатие в строке решения.
 */
export function openDecisionDialog(opts) {
  const o = opts || {};
  if (!o.api || typeof o.api.post !== 'function' || !o.decision) return null;
  ensureCss('css/view-request.css');
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const outcome = String(o.outcome || 'discuss');
  const meta = outcomeMeta(outcome);
  const discuss = outcome === 'discuss';

  const overlay = h('div', { class: 'dialog-backdrop decdlg-backdrop', 'data-decision-dialog': '' });
  const dialog = h('div', {
    class: 'dialog decdlg', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'decdlg-title',
  });
  overlay.appendChild(dialog);

  const host = overlay.parentNode || null;
  const close = () => {
    const parent = overlay.parentNode || host;
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

  const title = discuss ? 'Обсудить решение' : 'Отклонить решение';
  const head = h('div', { class: 'dialog__head' },
    h('h2', { class: 'dialog__title', id: 'decdlg-title', tabindex: '-1' }, title),
    h('button', {
      class: 'btn btn--ghost btn--sm decdlg__close', type: 'button',
      'aria-label': 'Закрыть окно', onclick: close,
    }, icon('close', { size: 13 })));

  const ta = h('textarea', {
    class: 'textarea decdlg__input', rows: 4,
    id: 'decdlg-text',
    placeholder: discuss ? 'Что обсудить с руководителем? Можно надиктовать…'
      : 'Можно сразу написать, что предложить вместо (необязательно)…',
    'aria-label': discuss ? 'Что обсудить с руководителем' : 'Что предложить вместо решения',
    'data-keep': 'dec:' + String((o.decision && o.decision.id) || ''),
  });

  const body = h('div', { class: 'dialog__body decdlg__body' },
    h('div', { class: 'decdlg__decision' },
      h('span', { class: 'decdlg__cap muted' }, 'Решение руководителя'),
      h('div', { class: 'decdlg__text' }, String((o.decision && o.decision.text) || '')),
      o.decision && o.decision.why
        ? h('div', { class: 'decdlg__why small muted' }, String(o.decision.why)) : null),
    h('div', { class: 'decdlg__hint small muted' }, meta.hint),
    ta,
    discuss
      ? h('div', { class: 'decdlg__note small muted' },
        'Решение останется за вами: руководитель ответит в ветке, и оно не закроется само.')
      : null);

  const submit = h('button', {
    type: 'button', class: 'btn btn--primary decdlg__submit', 'data-action': 'decision-' + outcome,
  }, icon('send', { size: 12 }), meta.label);

  async function send() {
    const text = String(ta.value || '').trim();
    if (discuss && !text) {
      if (typeof ta.focus === 'function') ta.focus();
      toast('Напишите или надиктуйте, что обсудить', 'warn');
      return;
    }
    submit.disabled = true;
    try {
      await postDecision({
        api: o.api, requestId: o.requestId, decision: o.decision, outcome, text,
      });
      toast(discuss
        ? 'Вопрос руководителю отправлен — решение остаётся за вами'
        : 'Ответ отправлен — руководитель увидит его у решения', 'ok');
      close();
      if (typeof o.refresh === 'function') await o.refresh();
    } catch (err) {
      submit.disabled = false;
      toast((err && err.message) || 'Не удалось отправить ответ', 'danger');
    }
  }
  submit.addEventListener('click', send);
  ta.addEventListener('keydown', (ev) => {
    if (ev && ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      send();
    }
  });

  const foot = h('div', { class: 'dialog__foot decdlg__foot' },
    h('button', { type: 'button', class: 'btn btn--secondary', onclick: close }, 'Отмена'),
    submit);
  dialog.append(head, body, foot);

  // Микрофон — по общему правилу (dictation-auto.js: рядом с кнопкой действия).
  try { enhanceAll(dialog); } catch { /* диктовка не должна ломать окно */ }

  const root = (typeof document !== 'undefined' && document.body) || null;
  if (root && typeof root.appendChild === 'function') root.appendChild(overlay);
  const first = dialog.querySelector ? dialog.querySelector('#decdlg-title') : null;
  if (first && typeof first.focus === 'function') first.focus();
  return { overlay, dialog, close };
}

export default { openDecisionDialog, postDecision, decisionActions, decisionStatusOf, decisionAnswerOf };
