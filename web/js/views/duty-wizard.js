/**
 * duty-wizard.js — мастер дежурства (W27-E8): четыре шага «что → когда →
 * что считать изменением → что делать» и предпросмотр точной команды.
 *
 * Один и тот же мастер служит созданию, правке (`mode: 'edit'`), шаблону,
 * предложению руководителя и «Сделать регулярным» из Дела — человек видит
 * одинаковые шаги и одинаковый предпросмотр, а не пять разных форм.
 *
 * Правило «без тупиков»: кнопка «Далее» не просто серая — рядом написано,
 * чего не хватает, и это же перечислено внизу списком по шагам.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import {
  WIZARD_STEPS, KINDS, SCHEDULE_PRESETS, CHANGE_RULES, ON_CHANGE, LEVELS, NOTIFY,
  normalizeDraft, stepProblems, draftProblems, scheduleHuman, changeText, onChangeLabel,
  previewModel, countText,
} from './duty-model.js';

/** Заголовки мастера по поводу открытия. */
export const WIZARD_TITLES = {
  create: 'Новое дежурство',
  edit: 'Правка дежурства',
  template: 'Дежурство по шаблону',
  proposal: 'Дежурство из предложения',
  delo: 'Регулярная проверка по делу',
  request: 'Регулярная проверка по запросу',
};

/** Подписи кнопок — одни для интерфейса, подсказок и тестов. */
export const WIZARD_BUTTONS = {
  next: 'Далее',
  back: 'Назад',
  cancel: 'Отмена',
  preview: 'Показать, что изменится',
  create: 'Создать дежурство',
  save: 'Сохранить',
};

/**
 * @param {object} opts
 * @param {object} opts.draft       черновик (см. duty-model.emptyDraft)
 * @param {string} [opts.title]     заголовок мастера
 * @param {Function} [opts.onCancel]
 * @param {Function} [opts.onPreview] async (draft) → предпросмотр сервера
 * @param {Function} [opts.onSubmit]  async (draft) → создание/сохранение
 * @param {Function} [opts.toast]
 */
export function createWizard(opts = {}) {
  const state = {
    draft: normalizeDraft(opts.draft),
    step: 0,
    preview: null,
    busy: '',
    error: '',
  };
  const isEdit = state.draft.mode === 'edit';
  const heading = String(opts.title || WIZARD_TITLES[isEdit ? 'edit' : 'create']);

  const el = h('section', { class: 'dw', 'data-role': 'duty-wizard', 'data-mode': isEdit ? 'edit' : 'create' });
  const stepper = h('ol', { class: 'dw__steps', 'aria-label': 'Шаги мастера' });
  const body = h('div', { class: 'dw__body' });
  const gate = h('p', { class: 'dw__gate small', role: 'status', 'aria-live': 'polite' });
  const foot = h('div', { class: 'dw__foot' });
  const alert = h('div', { class: 'dw__alert', hidden: true });
  const previewBox = h('div', { class: 'dw__preview', 'data-role': 'duty-preview', hidden: true });

  el.appendChild(h('header', { class: 'dw__head' },
    h('div', { class: 'dw__headline' },
      h('h2', { class: 'dw__title' }, heading),
      h('p', { class: 'dw__sub small muted' },
        isEdit ? 'Меняются только выбранные поля. Перед сохранением — предпросмотр.'
          : 'Четыре шага. Ничего не создаётся, пока вы не подтвердите.')),
    h('button', {
      class: 'btn btn--ghost btn--sm', type: 'button', 'data-action': 'wizard-cancel-top',
      title: 'Закрыть мастер без сохранения',
      onclick: () => cancel(),
    }, icon('close', { size: 13 }), 'Закрыть')));
  el.appendChild(stepper);
  el.appendChild(alert);
  el.appendChild(body);
  el.appendChild(previewBox);
  el.appendChild(gate);
  el.appendChild(foot);

  function say(message, kind) {
    if (!message) {
      alert.hidden = true;
      alert.replaceChildren();
      return;
    }
    alert.hidden = false;
    alert.className = 'dw__alert dw__alert--' + (kind || 'warn');
    alert.replaceChildren(h('span', {}, message));
  }

  function cancel() {
    if (typeof opts.onCancel === 'function') opts.onCancel(state.draft);
  }

  /** Записать поле черновика (без полной перерисовки — курсор остаётся в поле). */
  function set(patch) {
    state.draft = normalizeDraft({ ...state.draft, ...patch });
    state.preview = null;
    previewBox.hidden = true;
    previewBox.replaceChildren();
    refreshGate();
  }

  /** Кнопка по действию: в браузере `querySelector`, в тестах — обход дерева. */
  function byAction(node, action) {
    if (!node) return null;
    if (typeof node.querySelector === 'function') {
      try {
        const found = node.querySelector('[data-action="' + action + '"]');
        if (found) return found;
      } catch { /* ниже — обход дерева */ }
    }
    const stack = [node];
    while (stack.length) {
      const current = stack.pop();
      if (!current || current.nodeType !== 1) continue;
      if (current.getAttribute && current.getAttribute('data-action') === action) return current;
      for (const child of current.childNodes || []) stack.push(child);
    }
    return null;
  }

  /** Только текст «чего не хватает» и состояние кнопок. */
  function refreshGate() {
    const step = WIZARD_STEPS[state.step];
    const problems = stepProblems(state.draft, step.id);
    gate.className = 'dw__gate small ' + (problems.length ? 'dw__gate--missing' : 'dw__gate--ok');
    gate.replaceChildren(...(problems.length
      ? [h('span', { class: 'dw__gate-title' }, 'Чтобы идти дальше: '),
        h('span', {}, problems.join('; '))]
      : [h('span', {}, step.id === 'do'
        ? 'Всё заполнено — можно показать, что изменится.'
        : 'Шаг заполнен.')]));
    const nextBtn = byAction(foot, 'wizard-next');
    if (nextBtn) {
      const last = state.step === WIZARD_STEPS.length - 1;
      nextBtn.hidden = last;
      nextBtn.disabled = problems.length > 0;
      nextBtn.title = problems.length ? problems.join('; ') : '';
    }
    const previewBtn = byAction(foot, 'wizard-preview');
    if (previewBtn) {
      previewBtn.hidden = state.step !== WIZARD_STEPS.length - 1;
      previewBtn.disabled = problems.length > 0 || !!state.busy;
    }
    const submitBtn = byAction(foot, 'wizard-submit');
    if (submitBtn) {
      submitBtn.hidden = !(state.step === WIZARD_STEPS.length - 1 && state.preview);
      submitBtn.disabled = !state.preview || !!state.busy;
      submitBtn.textContent = isEdit ? WIZARD_BUTTONS.save : WIZARD_BUTTONS.create;
    }
  }

  function renderStepper() {
    stepper.replaceChildren(...WIZARD_STEPS.map((step, index) => {
      const done = index < state.step;
      const current = index === state.step;
      return h('li', {
        class: 'dw__step' + (current ? ' dw__step--now' : '')
          + (done ? ' dw__step--done' : '')
          + (done && stepProblems(state.draft, step.id).length === 0 ? ' dw__step--ok' : ''),
        'data-step': step.id,
      },
      h('button', {
        class: 'dw__step-btn', type: 'button',
        'data-action': 'wizard-goto', 'data-step': step.id,
        'aria-current': current ? 'step' : null,
        title: stepProblems(state.draft, step.id).length
          ? step.hint + ' Не хватает: ' + stepProblems(state.draft, step.id).join('; ')
          : step.hint,
        onclick: () => goto(index),
      },
      h('span', { class: 'dw__step-n' }, String(step.n)),
      h('span', { class: 'dw__step-title' }, step.title)),
      done && index + 1 < state.step
        ? h('span', { class: 'dw__step-line', 'aria-hidden': 'true' }) : null);
    }));
  }

  function goto(index) {
    const next = Math.max(0, Math.min(WIZARD_STEPS.length - 1, Number(index) || 0));
    // Вперёд — только через заполненные шаги: иначе человек уйдёт в пустой конец.
    for (let i = state.step; i < next; i += 1) {
      if (stepProblems(state.draft, WIZARD_STEPS[i].id).length) {
        state.step = i;
        render();
        return;
      }
    }
    state.step = next;
    state.preview = null;
    render();
  }

  /* ── поля ─────────────────────────────────────────────────────────────── */

  function field(label, control, hint, opts2 = {}) {
    const id = 'dw-' + (opts2.id || label.replace(/\s+/g, '-').toLowerCase());
    if (control && control.setAttribute) {
      control.setAttribute('id', id);
      control.setAttribute('aria-label', label);
    }
    return h('div', { class: 'dw__field' + (opts2.wide ? ' dw__field--wide' : '') },
      h('label', { class: 'dw__label', for: id }, label),
      control,
      hint ? h('p', { class: 'dw__hint small muted' }, hint) : null);
  }

  function textInput({ value, placeholder, onInput, multiline, rows, type }) {
    if (multiline) {
      return h('textarea', {
        class: 'dw__input dw__input--area', rows: String(rows || 4),
        placeholder: placeholder || '',
        oninput: (event) => onInput(String(event.target.value || '')),
      }, String(value || ''));
    }
    const node = h('input', {
      class: 'dw__input', type: type || 'text',
      placeholder: placeholder || '',
      oninput: (event) => onInput(String(event.target.value || '')),
    });
    node.setAttribute('value', String(value || ''));
    node.value = String(value || '');
    return node;
  }

  function choiceCards(options, current, onPick, name) {
    return h('div', { class: 'dw__choices', role: 'radiogroup', 'aria-label': name || 'выбор' },
      ...options.map((one) => h('button', {
        class: 'dw__choice' + (one.id === current ? ' dw__choice--on' : ''),
        type: 'button',
        role: 'radio',
        'aria-checked': one.id === current ? 'true' : 'false',
        'data-choice': one.id,
        'data-group': name || '',
        onclick: () => { onPick(one.id); render(); },
      },
      h('span', { class: 'dw__choice-title' }, one.label || one.title),
      one.hint ? h('span', { class: 'dw__choice-hint' }, one.hint) : null)));
  }

  function chips(options, current, onPick, name) {
    return h('div', { class: 'dw__chips', role: 'group', 'aria-label': name || 'быстрый выбор' },
      ...options.map((one) => h('button', {
        class: 'dw__chip' + (one.id === current ? ' dw__chip--on' : ''),
        type: 'button',
        'data-chip': one.id,
        'data-group': name || '',
        onclick: () => {
          if (one.id === 'custom') {
            onPick({ id: one.id, value: '' });
          } else {
            onPick({ id: one.id, value: one.value });
          }
          render();
        },
      }, one.label)));
  }

  function toggles(options, current, onToggle, name) {
    const list = Array.isArray(current) ? current : [];
    return h('div', { class: 'dw__toggles', role: 'group', 'aria-label': name || 'уведомления' },
      ...options.map((one) => h('label', { class: 'dw__toggle' },
        h('input', {
          type: 'checkbox', 'data-toggle': one.id,
          onchange: (event) => {
            const on = !!event.target.checked;
            const next = on ? [...new Set([...list, one.id])] : list.filter((x) => x !== one.id);
            onToggle(next);
            refreshGate();
          },
          ...(list.includes(one.id) ? { checked: true } : {}),
        }),
        h('span', {}, one.label))));
  }

  function checkbox(label, checked, onchange, key) {
    return h('label', { class: 'dw__check' },
      h('input', {
        type: 'checkbox', 'data-flag': key || '',
        onchange: (event) => { onchange(!!event.target.checked); refreshGate(); },
        ...(checked ? { checked: true } : {}),
      }),
      h('span', {}, label));
  }

  /* ── шаги ─────────────────────────────────────────────────────────────── */

  function stepWhat() {
    const doc = state.draft;
    const box = h('div', { class: 'dw__grid' });
    box.appendChild(field('Название', textInput({
      value: doc.title, placeholder: 'Например: проверять ящик на новое',
      onInput: (value) => set({ title: value }),
    }), 'Так дежурство будет называться в списке и в карточках.', { id: 'title', wide: true }));

    box.appendChild(h('div', { class: 'dw__field dw__field--wide' },
      h('span', { class: 'dw__label' }, 'Чем проверяем'),
      choiceCards(KINDS, doc.kind, (id) => set({ kind: id }), 'kind')));

    const taskLabel = doc.kind === 'script' ? 'Что запускать'
      : doc.kind === 'watch' ? 'За чем следить' : 'Задание';
    const taskHint = doc.kind === 'script'
      ? 'Путь к скрипту или команда. Модель не тратится.'
      : doc.kind === 'watch'
        ? 'Адрес страницы, папка или ящик. Hermes помнит прошлое состояние сам.'
        : 'Что проверить и по каким признакам понять, что «что-то изменилось».';
    const placeholder = doc.kind === 'script' ? 'C:/Work/Rooms/demo/tools/check.py'
      : doc.kind === 'watch' ? 'C:/Work/Rooms/demo/inbox или https://example.com/page'
        : 'Прочитать новые письма в ящике и коротко пересказать, что важного.';
    box.appendChild(field(taskLabel, textInput({
      value: doc.task, placeholder, multiline: true, rows: 4,
      onInput: (value) => set({ task: value }),
    }), taskHint, { id: 'task', wide: true }));

    box.appendChild(field('Рабочая папка', textInput({
      value: doc.workdir, placeholder: 'C:/Work/Rooms/demo',
      onInput: (value) => set({ workdir: value }),
    }), 'Где задание будет работать. Для папки комнаты — так и оставьте.', { id: 'workdir' }));

    if (doc.from) {
      box.appendChild(h('p', { class: 'dw__from small muted dw__field--wide' },
        (doc.from.kind === 'delo' ? 'Из дела: ' : doc.from.kind === 'request' ? 'Из запроса: ' : 'Из предложения: ')
        + (doc.from.title || doc.from.id || '')));
    }
    return box;
  }

  function stepWhen() {
    const doc = state.draft;
    const box = h('div', { class: 'dw__grid' });
    const presetId = (SCHEDULE_PRESETS.find((one) => one.value && one.value === doc.schedule) || { id: 'custom' }).id;
    box.appendChild(h('div', { class: 'dw__field dw__field--wide' },
      h('span', { class: 'dw__label' }, 'Как часто'),
      chips(SCHEDULE_PRESETS, presetId, (picked) => set({ schedule: picked.value }), 'schedule')));
    box.appendChild(field('Точное расписание', textInput({
      value: doc.schedule, placeholder: '0 9 * * 1-5 или every 2h',
      onInput: (value) => set({ schedule: value }),
    }), 'Понимает интервалы (30m, every 2h) и обычное cron-выражение. '
      + 'Сейчас это читается как: ' + (scheduleHuman(doc.schedule) || 'не распознано'), { id: 'schedule', wide: true }));
    box.appendChild(h('div', { class: 'dw__field dw__field--wide' },
      checkbox('Догнать пропущенное, если Hermes был выключен', doc.catch_up,
        (on) => set({ catch_up: on }), 'catch-up'),
      checkbox('Создать на паузе — включу сам', doc.started_paused,
        (on) => set({ started_paused: on }), 'paused')));
    return box;
  }

  function stepChange() {
    const doc = state.draft;
    const box = h('div', { class: 'dw__grid' });
    box.appendChild(h('div', { class: 'dw__field dw__field--wide' },
      h('span', { class: 'dw__label' }, 'Изменением считается'),
      choiceCards(CHANGE_RULES.map((one) => ({ id: one.id, label: one.label, hint: one.hint })),
        doc.change.rule, (id) => set({ change: { ...doc.change, rule: id } }), 'change')));
    if (doc.change.rule === 'count_over') {
      box.appendChild(field('Порог', textInput({
        value: String(doc.change.threshold || ''), type: 'number', placeholder: '3',
        onInput: (value) => set({ change: { ...doc.change, threshold: Number(value) || 0 } }),
      }), 'Сколько должно стать, чтобы это считалось изменением.', { id: 'threshold' }));
    }
    box.appendChild(h('p', { class: 'dw__note small muted dw__field--wide' },
      'Если ничего не изменилось, дежурство оставляет одну тихую строку в истории — '
      + 'и не тревожит вас.'));
    return box;
  }

  function stepDo() {
    const doc = state.draft;
    const box = h('div', { class: 'dw__grid' });
    box.appendChild(h('div', { class: 'dw__field dw__field--wide' },
      h('span', { class: 'dw__label' }, 'Что делать при изменении'),
      choiceCards(ON_CHANGE.map((one) => ({ id: one.id, label: one.label, hint: one.hint })),
        doc.onChange, (id) => set({ onChange: id }), 'on-change')));
    box.appendChild(h('div', { class: 'dw__field dw__field--wide' },
      h('span', { class: 'dw__label' }, 'Насколько самостоятельно'),
      choiceCards(LEVELS.map((one) => ({ id: one.id, label: one.label, hint: one.hint })),
        doc.level, (id) => set({ level: id }), 'level')));
    box.appendChild(h('div', { class: 'dw__field dw__field--wide' },
      h('span', { class: 'dw__label' }, 'Куда сообщить'),
      toggles(NOTIFY, doc.notify, (list) => set({ notify: list }), 'notify')));
    box.appendChild(h('p', { class: 'dw__summary small dw__field--wide', 'data-role': 'duty-summary' },
      'Получится: ' + (doc.title || 'без названия') + ' — ' + summaryText(doc)));
    return box;
  }

  function summaryText(doc) {
    const when = scheduleHuman(doc.schedule) || 'без расписания';
    return when + ' · ' + (changeText(doc.change) || 'правило не выбрано')
      + ' → ' + onChangeLabel(doc.onChange).toLowerCase();
  }

  function renderBody() {
    const step = WIZARD_STEPS[state.step];
    body.replaceChildren();
    body.setAttribute('data-step', step.id);
    const builders = { what: stepWhat, when: stepWhen, change: stepChange, do: stepDo };
    body.appendChild(h('h3', { class: 'dw__body-title' }, step.n + '. ' + step.title));
    body.appendChild(h('p', { class: 'dw__body-hint small muted' }, step.hint));
    body.appendChild((builders[step.id] || stepWhat)());
  }

  function renderPreview() {
    const model = previewModel(state.preview, state.draft);
    previewBox.hidden = false;
    previewBox.replaceChildren(
      h('h3', { class: 'dw__preview-title' },
        icon('terminal', { size: 14 }), ' Что именно выполнится'),
      h('pre', { class: 'dw__command', 'data-role': 'duty-command' }, model.command),
      h('p', { class: 'dw__preview-note small muted' }, model.note),
      model.changes.length
        ? h('div', { class: 'dw__changes' },
          h('span', { class: 'dw__changes-title small' }, 'Что изменится:'),
          h('ul', { class: 'dw__changes-list' },
            ...model.changes.map((line) => h('li', {}, line))))
        : null);
  }

  function renderFoot() {
    const last = state.step === WIZARD_STEPS.length - 1;
    foot.replaceChildren(
      h('button', {
        class: 'btn btn--ghost', type: 'button', 'data-action': 'wizard-cancel',
        onclick: () => cancel(),
      }, WIZARD_BUTTONS.cancel),
      h('button', {
        class: 'btn btn--secondary', type: 'button', 'data-action': 'wizard-back',
        onclick: () => goto(state.step - 1),
        ...(state.step === 0 ? { hidden: true } : {}),
      }, WIZARD_BUTTONS.back),
      h('button', {
        class: 'btn btn--primary', type: 'button', 'data-action': 'wizard-next',
        onclick: () => goto(state.step + 1),
        ...(last ? { hidden: true } : {}),
      }, WIZARD_BUTTONS.next),
      h('button', {
        class: 'btn btn--primary', type: 'button', 'data-action': 'wizard-preview',
        onclick: () => requestPreview(),
        ...(last ? {} : { hidden: true }),
      }, WIZARD_BUTTONS.preview),
      h('button', {
        class: 'btn btn--primary', type: 'button', 'data-action': 'wizard-submit',
        hidden: true,
        onclick: () => submit(),
      }, isEdit ? WIZARD_BUTTONS.save : WIZARD_BUTTONS.create));
  }

  function render() {
    renderStepper();
    renderBody();
    renderFoot();
    refreshGate();
    if (state.preview) renderPreview();
    else {
      previewBox.hidden = true;
      previewBox.replaceChildren();
    }
  }

  async function requestPreview() {
    const problems = stepProblems(state.draft, WIZARD_STEPS[state.step].id);
    if (problems.length) {
      say('Пока не хватает: ' + problems.join('; '), 'warn');
      return null;
    }
    state.busy = 'preview';
    refreshGate();
    say('');
    try {
      const answer = typeof opts.onPreview === 'function' ? await opts.onPreview(state.draft) : null;
      state.preview = answer && typeof answer === 'object' ? answer : { ok: true };
      if (state.preview && state.preview.ok === false) {
        say(String(state.preview.error || 'Предпросмотр не удался — сервер не ответил'), 'danger');
        state.preview = { ok: false, ...state.preview };
        renderPreview();
      } else {
        renderPreview();
      }
      refreshGate();
      return state.preview;
    } catch (err) {
      state.preview = null;
      say((err && err.message) || 'Не удалось получить предпросмотр', 'danger');
      refreshGate();
      return null;
    } finally {
      state.busy = '';
      refreshGate();
    }
  }

  async function submit() {
    if (!state.preview) return;
    state.busy = 'submit';
    refreshGate();
    try {
      if (typeof opts.onSubmit === 'function') await opts.onSubmit(state.draft);
    } catch (err) {
      say((err && err.message) || 'Не удалось сохранить дежурство', 'danger');
    } finally {
      state.busy = '';
      refreshGate();
    }
  }

  render();

  return {
    el,
    state,
    render,
    goto,
    get draft() { return state.draft; },
    /** Заменить черновик (например, выбрали шаблон) и вернуться на первый шаг. */
    setDraft(draft) {
      state.draft = normalizeDraft(draft);
      state.step = 0;
      state.preview = null;
      say('');
      render();
    },
    preview: requestPreview,
    submit,
    summaryText,
  };
}

export default { createWizard, WIZARD_STEPS, WIZARD_TITLES, WIZARD_BUTTONS };
