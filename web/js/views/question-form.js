/**
 * views/question-form.js — ЕДИНСТВЕННАЯ форма ответа на вопрос.
 *
 * Аудит B15: в «От вас нужно» было шесть одинаковых форм с одинаковыми кнопками и
 * повтором длинной подсказки «Мне нужно пояснение» ×6; в правой колонке
 * запроса — своя, седьмая форма. Здесь один компонент: поле, «Отправить»,
 * «Мне нужно пояснение». Его использует вкладка «От вас нужно»; правой колонке
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

/* ───────────────── W30-C: вопрос по-человечески (контракт §1) ───────────────
 *
 * Дефект 13/14: вопрос приходил человеку строкой с внутренними кодами, без
 * «зачем это нужно» и без вариантов — отвечать приходилось текстом на догадку.
 * Здесь разбор полей контракта §1 (`why`, `context`, `options[]`, `blocking`,
 * `explanation`, `plain_ok`) и готовые блоки для любой формы вопроса: правая
 * колонка запроса и вкладка «От вас нужно» показывают одно и то же.
 *
 * Правила: ни одного `innerHTML` (только `h()`), пустое поле не рисуется,
 * «Зачем это нужно» — всегда на виду, «Откуда взялось» — раскрывается.
 */

/** Что значит «вопрос блокирует работу» — одни слова на всё приложение. */
export const Q_BLOCKING_HINT = 'Пока вы не ответите, работа по задаче стоит.';

/** Тихое пояснение к проверке «понятно человеку» (для отладки, не пугает). */
export const Q_PLAIN_HINT = 'Проверка «понятно человеку»: без внутренних кодов, с объяснением зачем.';

/** Подписи вариантов по умолчанию, если сервер не прислал свои. */
export const Q_FACTS_RU = {
  why: 'Зачем это нужно',
  context: 'Откуда взялось',
  explanation: 'Пояснение руководителя',
  options: 'Варианты ответа',
  custom: 'Свой ответ',
  explainMore: 'Пояснить ещё',
  postpone: 'Отложить',
  stale: 'Не актуально',
  restore: 'Вернуть',
  blocking: 'Блокирует работу',
  blockingQuiet: 'Не блокирует',
  explained: 'пояснено — выберите ответ',
  plainBad: 'проверка простоты не пройдена',
};

const MAX_OPTIONS = 4;

function str(value, limit) {
  const text = String(value === null || value === undefined ? '' : value).trim();
  if (!text) return '';
  return limit && text.length > limit ? text.slice(0, limit) + '…' : text;
}

/**
 * Поля вопроса по контракту §1 — в одном виде для всех мест экрана.
 *
 * Терпимо к старым данным: нет `why`/`options` — вернутся пустые значения, и
 * блоки просто не рисуются (вопрос остаётся прежним, с полем ответа).
 *
 * @param {object} q вопрос из `open_questions[]`
 * @returns {{why:string, context:string, options:Array<{id:string,label:string,consequence:string}>,
 *            blocking:boolean, explanation:string, plainOk:boolean, plainIssues:string[],
 *            explained:boolean, picked:string, hasFacts:boolean}}
 */
export function questionFacts(q) {
  const src = q && typeof q === 'object' ? q : {};
  const rawOptions = Array.isArray(src.options) ? src.options : [];
  const options = [];
  for (const item of rawOptions.slice(0, MAX_OPTIONS)) {
    if (!item || typeof item !== 'object') continue;
    const label = str(item.label, 160);
    const id = str(item.id, 60) || label;
    if (!label && !id) continue;
    options.push({ id, label: label || id, consequence: str(item.consequence, 160) });
  }
  const plainIssues = Array.isArray(src.plain_issues)
    ? src.plain_issues.map((x) => str(x, 120)).filter(Boolean).slice(0, 4)
    : [];
  const answerState = str(src.answer_state, 40);
  return {
    why: str(src.why, 300),
    context: str(src.context, 600),
    options,
    blocking: src.blocking === true,
    explanation: str(src.explanation, 900),
    plainOk: src.plain_ok === false ? false : true,
    plainIssues,
    explained: answerState === 'explained' || Boolean(src.explained === true),
    picked: str(src.answer_option || src.option_id, 60),
    hasFacts: Boolean(str(src.why, 300) || str(src.context, 600) || options.length || str(src.explanation, 900)),
  };
}

/**
 * «Зачем это нужно» — всегда на виду, сразу под текстом вопроса.
 * @param {object} q
 * @returns {HTMLElement|null}
 */
export function buildQuestionWhy(q) {
  const { why } = questionFacts(q);
  if (!why) return null;
  return h('div', { class: 'req-q__why', 'data-key': 'qwhy' },
    h('span', { class: 'req-q__cap' }, Q_FACTS_RU.why),
    h('span', { class: 'req-q__why-text' }, why));
}

/**
 * «Откуда взялось» — 2–4 предложения контекста, раскрываемые: по умолчанию
 * свёрнуто, чтобы список вопросов оставался списком, а не диссертацией.
 * @param {object} q
 * @returns {HTMLElement|null}
 */
export function buildQuestionContext(q) {
  const { context } = questionFacts(q);
  if (!context) return null;
  return h('details', { class: 'req-q__ctx', 'data-key': 'qctx' },
    h('summary', { class: 'req-q__ctx-sum' }, icon('chevron-right', { size: 11 }), Q_FACTS_RU.context),
    h('div', { class: 'req-q__ctx-text' }, context));
}

/**
 * Пояснение руководителя — ПРЯМО под вопросом (состояние `explained`).
 * @param {object} q
 * @returns {HTMLElement|null}
 */
export function buildQuestionExplanation(q) {
  const { explanation } = questionFacts(q);
  if (!explanation) return null;
  return h('div', { class: 'req-q__explain-box', 'data-key': 'qexpl', role: 'note' },
    h('div', { class: 'req-q__explain-head' },
      icon('comment-discussion', { size: 12 }),
      h('span', { class: 'req-q__cap' }, Q_FACTS_RU.explanation)),
    h('div', { class: 'req-q__explain-text' }, explanation));
}

/**
 * Варианты ответа кнопками, с последствиями под подписью (§1 `options`).
 * Нажатие сразу отправляет `answer_option {option_id}` — без второго шага.
 *
 * @param {object} q
 * @param {{onPick?: (option:object, ev:Event)=>any, current?: string}} [opts]
 * @returns {HTMLElement|null}
 */
export function buildQuestionOptions(q, opts = {}) {
  const facts = questionFacts(q);
  if (!facts.options.length) return null;
  const onPick = opts && typeof opts.onPick === 'function' ? opts.onPick : null;
  const current = str((opts && opts.current) || facts.picked, 60);
  const box = h('div', {
    class: 'req-q__options', 'data-key': 'qopts', role: 'group',
    'aria-label': Q_FACTS_RU.options,
  });
  for (const option of facts.options) {
    const pressed = current && current === option.id;
    const btn = h('button', {
      type: 'button',
      class: 'btn btn--choice req-q__option' + (pressed ? ' is-picked' : ''),
      'data-action': 'answer-option',
      'data-option-id': option.id,
      'aria-pressed': pressed ? 'true' : 'false',
      title: option.consequence || option.label,
    },
    h('span', { class: 'req-q__option-label' }, option.label),
    option.consequence ? h('span', { class: 'req-q__option-cons' }, option.consequence) : null);
    if (onPick) {
      btn.addEventListener('click', (ev) => {
        if (ev && ev.preventDefault) ev.preventDefault();
        if (btn.disabled) return;
        onPick(option, ev);
      });
    }
    box.appendChild(btn);
  }
  return box;
}

/**
 * Тихое служебное замечание: вопрос не прошёл проверку «понятно человеку».
 * Человека не пугаем — подпись мелкая и с объяснением в title.
 * @param {object} q
 * @returns {HTMLElement|null}
 */
export function buildPlainMark(q) {
  const facts = questionFacts(q);
  if (facts.plainOk) return null;
  const issues = facts.plainIssues.length ? ' Причины: ' + facts.plainIssues.join('; ') + '.' : '';
  return h('div', {
    class: 'req-q__plain small muted', 'data-key': 'qplain',
    title: Q_PLAIN_HINT + issues,
  }, icon('info', { size: 11 }), h('span', null, Q_FACTS_RU.plainBad));
}

/**
 * @param {{question?: object, onSubmit: (text:string)=>any, onExplain?: (text:string)=>any,
 *          keepKey?: string, placeholder?: string, waiting?: boolean,
 *          hint?: string, label?: string, busy?: boolean, submitLabel?: string,
 *          showFacts?: boolean, onOption?: (option:object, ev:Event)=>any}} opts
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
  const facts = questionFacts(q);
  const showFacts = o.showFacts === undefined ? facts.hasFacts : Boolean(o.showFacts);
  const onOption = typeof o.onOption === 'function' ? o.onOption : null;
  const node = h('div', {
    class: 'qa-form' + (waiting ? ' qa-form--waiting' : ''),
    'data-q': String(q.id || ''),
  },
  // W30-C: «зачем», «откуда» и пояснение руководителя — перед полем ответа:
  // человек сначала понимает вопрос, потом отвечает. У старого вопроса без
  // этих полей блоков нет — форма остаётся прежней.
  showFacts ? buildQuestionWhy(q) : null,
  showFacts ? buildQuestionContext(q) : null,
  showFacts ? buildQuestionExplanation(q) : null,
  showFacts && onOption ? buildQuestionOptions(q, { onPick: onOption }) : null,
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
