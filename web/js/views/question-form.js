/**
 * views/question-form.js — ЕДИНСТВЕННАЯ форма ответа на вопрос.
 *
 * Аудит B15: в «Дальше» было шесть одинаковых форм с одинаковыми кнопками и
 * повтором длинной подсказки «Мне нужно пояснение» ×6; в правой колонке
 * запроса — своя, седьмая форма. Здесь один компонент: поле, «Отправить»,
 * «Мне нужно пояснение». Его использует вкладка «Дальше»; правой колонке
 * (пакет D, request-aside.js) предлагается та же функция — разметка и
 * поведение совпадают с `answerForm()` из request-aside.js:
 *
 *   .qa-form > .qa-form__row [ textarea.qa-form__input, button.qa-form__send ]
 *              .qa-form__actions [ button.qa-form__explain, span.qa-form__hint ]
 *
 * Длинное объяснение «что значит пояснение» живёт ОДНОЙ строкой над списком
 * вопросов (backlog.js), а не под каждой формой.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';

/**
 * @param {{question?: object, onSubmit: (text:string)=>any, onExplain?: (text:string)=>any,
 *          keepKey?: string, placeholder?: string, waiting?: boolean,
 *          hint?: string, label?: string, busy?: boolean, submitLabel?: string}} opts
 * @returns {HTMLElement}
 */
export function buildQuestionForm(opts = {}) {
  const o = opts || {};
  const waiting = Boolean(o.waiting);
  const keepKey = String(o.keepKey || '');
  const onSubmit = typeof o.onSubmit === 'function' ? o.onSubmit : null;
  const onExplain = typeof o.onExplain === 'function' ? o.onExplain : null;
  const q = o.question && typeof o.question === 'object' ? o.question : {};

  const textarea = h('textarea', {
    class: 'textarea qa-form__input',
    rows: '2',
    placeholder: String(o.placeholder || (waiting ? 'Что пояснить в вопросе?' : 'Ваш ответ…')),
    'aria-label': String(o.label || 'Ответ на вопрос'),
    'data-keep': keepKey || null,
    'data-mic-anchor': keepKey || null,
  });
  const send = h('button', {
    type: 'button',
    class: 'btn btn--primary btn--sm qa-form__send',
    'data-action': 'answer',
    title: 'Отправить ответ руководителю (Ctrl+Enter)',
    disabled: o.busy ? true : null,
  }, icon('send', { size: 12 }), String(o.submitLabel || 'Отправить'));

  const explain = h('button', {
    type: 'button',
    class: 'btn btn--ghost btn--sm qa-form__explain',
    'data-action': 'need-explanation',
    title: 'Попросить пояснить вопрос — вопрос останется открытым, пока руководитель не объяснит',
    disabled: o.busy ? true : null,
  }, 'Мне нужно пояснение');

  const hint = String(o.hint || (waiting ? 'Вопрос остаётся открытым, пока руководитель не пояснит' : ''));
  const node = h('div', {
    class: 'qa-form' + (waiting ? ' qa-form--waiting' : ''),
    'data-q': String(q.id || ''),
  },
  h('div', { class: 'qa-form__row' }, textarea, send),
  h('div', { class: 'qa-form__actions' }, onExplain ? explain : null,
    hint ? h('span', { class: 'muted qa-form__hint' }, hint) : null));

  async function run(handler, btn, mode) {
    const text = String(textarea.value || '').trim();
    if (!text) {
      if (typeof textarea.focus === 'function') textarea.focus();
      return;
    }
    if (!handler) return;
    const wasSend = send.disabled;
    const wasExplain = explain.disabled;
    send.disabled = true;
    explain.disabled = true;
    node.classList.add('is-busy');
    node.setAttribute('aria-busy', 'true');
    try {
      await handler(text, mode);
    } finally {
      node.classList.remove('is-busy');
      node.removeAttribute('aria-busy');
      send.disabled = wasSend;
      explain.disabled = wasExplain;
      if (btn && !node.isConnected) { /* карточка уже убрана — состояние неважно */ }
    }
  }

  send.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    run(onSubmit, send, 'answer');
  });
  if (onExplain) {
    explain.addEventListener('click', (ev) => {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      run(onExplain, explain, 'explain');
    });
  }
  textarea.addEventListener('keydown', (ev) => {
    if (!ev) return;
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      run(onSubmit, send, 'answer');
    }
  });

  /** Значение поля (после успешной отправки вызывающий очищает). */
  node.clear = () => { textarea.value = ''; };
  node.focusInput = () => { if (typeof textarea.focus === 'function') textarea.focus(); };
  node.value = () => String(textarea.value || '');
  return node;
}

export default { buildQuestionForm };
