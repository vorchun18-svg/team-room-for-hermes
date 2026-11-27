/**
 * recipes.js — «Рецепты» комнаты (W29B-Q1).
 *
 * Рецепт — повторяющийся процесс одной кнопкой: имя, описание, поля ввода
 * (текст, файл, срок) и заранее заданный граф шагов «роль → шаг → критик».
 * «Запустить» создаёт запрос с готовым планом и карточки шагов сразу с моделью
 * комнаты — руководитель ничего не разбирает заново (меньше токенов и разброса).
 *
 * Правила волны: у каждого пункта есть действие, пустой экран объясняет, зачем
 * он и что сделать сейчас. Всё, что выглядит ссылкой, нажимается.
 */

import { h, esc, fmtDateTime, plural } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, sectionHeader, skeleton, errorBanner, emptyState, buildDialog } from '../ui.js';

const VIEW_CSS = 'css/view-recipes.css';
const HINT_KEY = 'teamroom.recipes.hint.hidden';

/* ── чистые помощники (node --test) ───────────────────────────────────────── */

/**
 * Три строки пояснения вверху вкладки — ровно то, чего не хватало Михаилу:
 * что такое рецепт, что делает кнопка «Запустить» и что это не крон.
 */
export const EXPLAIN_LINES = [
  'Рецепт — шаблон повторяющегося дела этой комнаты: поля, шаги, кто что делает.',
  'Нажмите «Запустить», заполните поля — получите задачу с готовым планом и карточки шагов.',
  'Это не крон: само ничего не запускается, только по вашей кнопке.',
];

/** Подписи вида поля. */
export const FIELD_KIND_RU = { text: 'текст', file: 'файл или папка', due: 'срок' };

/** Состояние запуска по-русски. */
export function runStateRu(state) {
  return ({
    launching: 'запускается', running: 'идёт', done: 'готово',
    failed: 'ошибка', archived: 'в архиве',
  })[String(state || '')] || 'идёт';
}

/** Состояние шага по-русски. */
export function stepStatusRu(status) {
  return ({
    pending: 'ждёт предыдущий шаг', created: 'создан', running: 'в работе',
    done: 'готово', blocked: 'на паузе', failed: 'не создался',
  })[String(status || '')] || 'ждёт';
}

/**
 * Слои графа шагов: [[шаг без зависимостей], [шаги после них], …].
 * Пустой граф → []. Цикл/битая ссылка не вешают интерфейс: такие шаги
 * попадают в последний слой.
 */
export function stepLayers(steps) {
  const list = Array.isArray(steps) ? steps.filter((s) => s && s.id) : [];
  const ids = new Set(list.map((s) => String(s.id)));
  const done = new Set();
  const layers = [];
  let guard = 0;
  while (done.size < list.length && guard <= list.length + 1) {
    guard += 1;
    const layer = list.filter((s) => !done.has(String(s.id))
      && (s.depends_on || []).every((d) => !ids.has(String(d)) || done.has(String(d))));
    if (!layer.length) {
      const rest = list.filter((s) => !done.has(String(s.id)));
      layers.push(rest);
      break;
    }
    layers.push(layer);
    for (const s of layer) done.add(String(s.id));
  }
  return layers;
}

/** Роли в шаге для подписи: «юрист · team-lawyer». */
export function stepRole(step) {
  const role = String((step && step.role) || '').trim();
  const assignee = String((step && step.assignee) || '').trim();
  if (role && assignee && role !== assignee) return role + ' · ' + assignee;
  return assignee || role || 'участник';
}

/** Короткая строка «что за рецепт»: поля и шаги. */
export function recipeLine(item) {
  const fields = (item && item.fields) || [];
  const steps = (item && item.steps) || [];
  return fields.length + ' ' + plural(fields.length, ['поле', 'поля', 'полей']) + ' · '
    + steps.length + ' ' + plural(steps.length, ['шаг', 'шага', 'шагов']);
}

/** Стадия разговора о рецепте по-русски. */
export function dialogStageRu(stage) {
  return ({
    asked: 'руководитель готовит ответ', questions: 'нужны ваши уточнения',
    draft: 'алгоритм готов — утвердите', accepted: 'рецепт сохранён', closed: 'закрыт',
  })[String(stage || '')] || 'ждём ответа';
}

/** Что сейчас происходит в разговоре — одной строкой для человека. */
export function waitingText(dialog) {
  const stage = String((dialog && dialog.stage) || '');
  const n = ((dialog && dialog.questions) || []).length;
  if (stage === 'questions') {
    return 'Руководитель спрашивает — ответьте на ' + n + ' ' + plural(n, ['вопрос', 'вопроса', 'вопросов'])
      + ', и он соберёт алгоритм.';
  }
  if (stage === 'draft') return 'Алгоритм готов. Проверьте шаги и нажмите «Да, сохранить» или «Исправить».';
  if (stage === 'accepted') return 'Рецепт сохранён — он в списке, можно запускать.';
  if (stage === 'closed') return 'Разговор закрыт без рецепта.';
  return 'Руководитель читает описание и готовит ответ — это займёт минуту.';
}

/** Строка про присланный алгоритм: поля, шаги, проверка критиком. */
export function draftLine(draft) {
  const fields = (draft && draft.fields) || [];
  const steps = (draft && draft.steps) || [];
  const critic = steps.filter((s) => s && s.critic).length;
  return fields.length + ' ' + plural(fields.length, ['поле', 'поля', 'полей'])
    + ' · ' + steps.length + ' ' + plural(steps.length, ['шаг', 'шага', 'шагов'])
    + (critic ? ' · проверяет критик' : '');
}

/** Токены по-человечески: 900 · 4,2 тыс. · 31,8 тыс. — без «0 тыс.» и «31 837 тыс.». */
export function tokensRu(value) {
  const num = Math.max(0, Math.round(Number(value) || 0));
  if (num < 1000) return String(num);
  if (num < 10000) return (num / 1000).toFixed(1).replace('.', ',') + ' тыс.';
  return String(Math.round(num / 1000)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' тыс.';
}

/** Строка настроек разбора: «раз в N дней · не меньше M задач · модель …». */
export function analysisLine(analysis) {
  const settings = (analysis && analysis.settings) || {};
  const days = Number(settings.every_days || 0);
  const min = Number(settings.min_tasks || 0);
  const model = String(settings.model_label || 'модель руководителя');
  const head = settings.enabled
    ? 'Раз в ' + days + ' ' + plural(days, ['день', 'дня', 'дней']) + ', не меньше ' + min + ' задач'
    : 'Разбор выключен';
  return head + ' · модель: ' + model;
}

/** Прикидка токенов до запуска — честная строка, без выдуманных денег. */
export function estimateLine(analysis) {
  const est = (analysis && analysis.estimate) || {};
  const tasks = Number(((analysis && analysis.candidates) || {}).count || 0);
  if (!tasks) return 'Завершённых задач за последний месяц нет — предлагать нечего.';
  return 'Задач для разбора: ' + tasks + ' · прикидка: ≈ '
    + tokensRu(est.input_tokens) + ' токенов на входе и '
    + tokensRu(est.output_tokens) + ' на выходе.';
}

/**
 * Плашка «пора разобрать задачи» — W31-FIX3.
 *
 * Сторож только помечает, что пора (`analysis.prompt.show`); карточку руководителю
 * ставит человек кнопкой «Запустить». Пока плашки нет — пустая строка, экран молчит.
 */
export function dueBanner(analysis) {
  const prompt = (analysis && analysis.prompt) || {};
  if (!prompt.show) return '';
  const count = Number(prompt.count || 0);
  const tokens = Number(prompt.tokens || 0);
  return 'Пора провести разбор задач (новых: ' + count + ', примерно '
    + tokensRu(tokens) + ' токенов)';
}

/** Что делает «Позже» — по-русски, с числом дней. */
export function laterLine(days) {
  const num = Math.max(1, Math.round(Number(days) || 7));
  return 'Отложили на ' + num + ' ' + plural(num, ['день', 'дня', 'дней'])
    + ' — напомним позже.';
}

/** Предложение из разбора — «почему» плюс задачи, по которым оно собрано. */
export function offerLine(offer) {
  const tasks = (offer && offer.tasks) || [];
  const why = String((offer && offer.why) || '').trim();
  const base = tasks.length
    ? 'по задачам: ' + tasks.slice(0, 3).join(', ') + (tasks.length > 3 ? ' и ещё ' + (tasks.length - 3) : '')
    : 'по завершённым задачам';
  return why ? why + ' — ' + base : base;
}

/** Пометка примера: «образец» и что это чужая сфера. */
export function sampleLabel(item) {
  const note = String((item && item.sample_note) || '').trim();
  return note || 'образец: пример из другой сферы, шаги можно переделать под себя';
}

/** Вопросы руководителя — текстом, чтобы их можно было скопировать и обдумать. */
export function questionsText(questions) {
  return (Array.isArray(questions) ? questions : [])
    .map((q) => String((q && q.text) || '')).filter(Boolean)
    .map((text, index) => (index + 1) + '. ' + text)
    .join('\n');
}

/* ── экран ───────────────────────────────────────────────────────────────── */

export async function mount(el, ctx = {}) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const room = ctx.room || {};
  const roomId = String((ctx.route && ctx.route.roomId) || room.id || '');
  let stopped = false;
  let data = null;
  let error = null;
  let showTrash = false;
  let showSamples = false;
  let showAnalysis = false;
  let dialog = null;
  let pollTimer = 0;

  const root = h('div', { class: 'rcp' });
  const head = h('div', { class: 'rcp__head' });
  const talk = h('div', { class: 'rcp__talk' });
  const body = h('div', { class: 'rcp__body' });
  const history = h('div', { class: 'rcp__history' });
  el.appendChild(root);
  root.appendChild(head);
  root.appendChild(talk);
  root.appendChild(body);
  root.appendChild(history);
  body.appendChild(skeleton(3));

  const apiGet = (path) => (api && typeof api.get === 'function' ? api.get(path) : Promise.reject(new Error('нет связи с приложением')));
  const apiCall = (method, path, payload) => {
    const fn = api && typeof api[method] === 'function' ? api[method] : null;
    if (!fn) return Promise.reject(new Error('нет связи с приложением'));
    return fn.call(api, path, payload);
  };
  const roomPath = '/api/rooms/' + encodeURIComponent(roomId) + '/recipes';

  function hintHidden() {
    try { return globalThis.localStorage?.getItem(HINT_KEY) === '1'; } catch (e) { return false; }
  }
  function hideHint() {
    try { globalThis.localStorage?.setItem(HINT_KEY, '1'); } catch (e) { /* приватный режим */ }
  }

  function load() {
    body.replaceChildren(skeleton(3));
    return apiGet(roomPath)
      .then((payload) => {
        if (stopped) return;
        data = payload || { items: [], samples: [], trash: [], runs: [], roles: [] };
        if (data.dialog) dialog = data.dialog;
        error = null;
        render();
        schedulePoll();
      })
      .catch((err) => {
        if (stopped) return;
        error = err || { message: 'не удалось прочитать рецепты' };
        render();
      });
  }

  function render() {
    head.replaceChildren();
    body.replaceChildren();
    history.replaceChildren();
    if (error) {
      head.appendChild(sectionHeader('Рецепты'));
      body.appendChild(errorBanner(error, load));
      return;
    }
    const items = (data && data.items) || [];
    const samples = (data && data.samples) || [];
    const trash = (data && data.trash) || [];
    const runs = (data && data.runs) || [];
    head.appendChild(sectionHeader('Рецепты', {
      count: items.length,
      actions: [
        h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => fromRequestDialog() },
          icon('copy', { size: 13 }), 'Сохранить задачу как рецепт'),
        h('button', { class: 'btn btn--primary', type: 'button', onclick: () => createDialog() },
          icon('plus', { size: 13 }), 'Создать рецепт'),
      ],
    }));
    if (!hintHidden()) head.appendChild(explainBlock());
    renderTalk();
    body.appendChild(analysisPanel());

    if (!items.length) {
      body.appendChild(emptyState({
        icon: 'book',
        title: 'Рецептов пока нет',
        text: 'Рецепт — это порядок шагов, который вы делаете регулярно: «ответ на претензию», '
          + '«сверка накладных», «меню на неделю». Заведите его один раз — дальше запуск одной кнопкой.',
        action: [
          { label: 'Рассказать руководителю', onclick: () => createDialog(true), variant: 'primary' },
          { label: 'Собрать вручную', onclick: () => editorDialog(null), variant: 'secondary' },
        ],
      }));
    } else {
      const list = h('div', { class: 'rcp__list' });
      for (const item of items) list.appendChild(recipeCard(item));
      body.appendChild(list);
    }

    if (samples.length) body.appendChild(samplesBlock(samples));

    if (trash.length) {
      const toggle = h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button',
        onclick: () => { showTrash = !showTrash; render(); },
      }, (showTrash ? '▾ ' : '▸ ') + 'Корзина (' + trash.length + ')');
      body.appendChild(h('div', { class: 'rcp__trash' }, toggle));
      if (showTrash) {
        const box = h('div', { class: 'rcp__list rcp__list--trash' });
        for (const item of trash) {
          box.appendChild(h('div', { class: 'rcp-card rcp-card--trash' },
            h('div', { class: 'rcp-card__main' },
              h('div', { class: 'rcp-card__title' }, item.name),
              h('div', { class: 'rcp-card__meta small muted' }, recipeLine(item))),
            h('button', {
              class: 'btn btn--secondary btn--sm', type: 'button',
              onclick: () => restoreRecipe(item),
            }, icon('history', { size: 12 }), 'Вернуть')));
        }
        body.appendChild(box);
      }
    }

    if (runs.length) {
      history.appendChild(sectionHeader('История запусков', { count: runs.length }));
      const list = h('div', { class: 'rcp-runs' });
      for (const run of runs) list.appendChild(runRow(run));
      history.appendChild(list);
    }
  }

  function explainBlock() {
    return h('div', { class: 'rcp-explain', 'data-testid': 'recipes-explain' },
      h('ol', { class: 'rcp-explain__list' },
        EXPLAIN_LINES.map((line) => h('li', { class: 'rcp-explain__line' }, line))),
      h('button', {
        class: 'btn btn--ghost btn--sm rcp-explain__hide', type: 'button',
        onclick: () => { hideHint(); render(); },
      }, 'Понятно'));
  }

  /* ── разговор о рецепте (W31-C) ──────────────────────────────────────── */

  function renderTalk() {
    talk.replaceChildren();
    if (!dialog) return;
    const stage = String(dialog.stage || '');
    const box = h('div', { class: 'rcp-talk rcp-talk--' + stage, 'data-testid': 'recipe-dialog' },
      h('div', { class: 'rcp-talk__head' },
        h('div', { class: 'rcp-talk__title' }, 'Рецепт с руководителем'),
        h('span', { class: 'chip chip--muted' }, dialogStageRu(stage)),
        dialog.model && dialog.model.label
          ? h('span', { class: 'small muted rcp-talk__model' }, 'модель: ' + dialog.model.label)
          : null),
      h('div', { class: 'small rcp-talk__what' }, waitingText(dialog)));
    if (dialog.text) {
      box.appendChild(h('div', { class: 'rcp-talk__said small muted' }, 'Вы рассказали: ' + dialog.text));
    }
    const messages = (dialog.messages || []).slice(-6);
    if (messages.length) {
      box.appendChild(h('div', { class: 'rcp-talk__log' }, messages.map((m) => h('div', {
        class: 'rcp-talk__msg',
      }, (m.role === 'human' ? 'Вы: ' : (m.role === 'questions' ? 'Вопрос: ' : 'Руководитель: ')),
        m.text))));
    }
    if (stage === 'asked') {
      box.appendChild(h('p', { class: 'small muted' },
        'Можно закрыть вкладку: ответ появится здесь же, когда руководитель ответит.'));
    }
    if (stage === 'questions') {
      const answers = h('textarea', {
        class: 'input', rows: '4', 'data-testid': 'recipe-answers',
        placeholder: 'Ответьте своими словами — можно по порядку вопросов',
      });
      const send = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, 'Отправить ответы');
      const line = h('div', { class: 'rcp-form__error small', role: 'alert' });
      send.addEventListener('click', () => {
        const text = String(answers.value || '').trim();
        if (!text) { line.textContent = 'Напишите ответы — без них алгоритм будет неточным.'; return; }
        send.disabled = true;
        apiCall('post', roomPath + '/dialog/' + encodeURIComponent(dialog.id) + '/answer', { text })
          .then((res) => { dialog = (res && res.dialog) || dialog; render(); schedulePoll(); })
          .catch((err) => { send.disabled = false; line.textContent = String((err && err.message) || 'не удалось отправить'); });
      });
      box.appendChild(h('div', { class: 'rcp-talk__questions', 'data-testid': 'recipe-questions' },
        (dialog.questions || []).map((q, index) => h('div', { class: 'rcp-talk__question' },
          h('span', { class: 'rcp-talk__qnum tnum' }, String(index + 1) + '.'),
          h('span', {}, q.text)))));
      box.appendChild(answers);
      box.appendChild(h('div', { class: 'rcp-talk__row' }, send));
      box.appendChild(line);
    }
    if (stage === 'draft') box.appendChild(draftView(dialog));
    if (stage === 'draft' || stage === 'accepted' || stage === 'closed') {
      const close = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'Закрыть разговор');
      close.addEventListener('click', () => {
        apiCall('post', roomPath + '/dialog/' + encodeURIComponent(dialog.id) + '/cancel', {})
          .then((res) => { dialog = (res && res.dialog) || null; render(); })
          .catch((err) => toast(String((err && err.message) || 'не удалось закрыть'), 'error'));
      });
      box.appendChild(h('div', { class: 'rcp-talk__row' }, close));
    }
    talk.appendChild(box);
  }

  function draftView(state) {
    const draft = state.draft || {};
    const wrap = h('div', { class: 'rcp-draft', 'data-testid': 'recipe-draft' },
      h('div', { class: 'rcp-draft__title' }, draft.name || 'Алгоритм'),
      draft.description ? h('div', { class: 'rcp-draft__desc small muted' }, draft.description) : null,
      h('div', { class: 'rcp-draft__meta small muted' }, draftLine(draft)));
    if (state.problem) {
      wrap.appendChild(h('div', { class: 'rcp-form__error small' },
        'Проверка не прошла: ' + state.problem + ' Нажмите «Исправить» и скажите, что поменять.'));
    }
    wrap.appendChild(h('div', { class: 'rcp-draft__block' },
      h('div', { class: 'rcp-form__subtitle' }, 'Поля при запуске'),
      h('ul', { class: 'rcp-draft__list' }, ((draft.fields) || []).map((f) => h('li', {},
        String(f.label || f.id) + (f.required ? ' *' : '')
        + (f.kind && f.kind !== 'text' ? ' (' + (FIELD_KIND_RU[f.kind] || f.kind) + ')' : ''))))));
    wrap.appendChild(h('div', { class: 'rcp-draft__block' },
      h('div', { class: 'rcp-form__subtitle' }, 'Шаги и зависимости'),
      h('ol', { class: 'rcp-draft__steps' }, ((draft.steps) || []).map((s) => h('li', {},
        h('span', { class: 'rcp-draft__step-title' }, String(s.title || 'шаг')),
        h('span', { class: 'small muted' }, ' — ' + stepRole(s)
          + ((s.depends_on || []).length ? ' · после: ' + s.depends_on.join(', ') : '')
          + (s.critic ? ' · проверяет критик' : '')),
        s.instruction ? h('div', { class: 'small muted rcp-draft__instr' }, String(s.instruction)) : null)))));
    if (state.stage === 'draft') {
      const yes = h('button', { class: 'btn btn--primary btn--sm', type: 'button', 'data-testid': 'recipe-yes' },
        'Да, сохранить');
      const fix = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Исправить');
      const line = h('div', { class: 'rcp-form__error small', role: 'alert' });
      const fixBox = h('div', { class: 'rcp-talk__fix hide' });
      const fixInput = h('textarea', { class: 'input', rows: '2', placeholder: 'что поправить: шаги, поля, порядок' });
      const fixSend = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, 'Отправить правку');
      fixBox.appendChild(fixInput);
      fixBox.appendChild(h('div', { class: 'rcp-talk__row' }, fixSend));
      yes.addEventListener('click', () => {
        yes.disabled = true;
        apiCall('post', roomPath + '/dialog/' + encodeURIComponent(dialog.id) + '/accept', {})
          .then((res) => {
            dialog = (res && res.dialog) || dialog;
            toast('Рецепт сохранён', 'ok');
            load();
          })
          .catch((err) => {
            yes.disabled = false;
            line.textContent = String((err && err.message) || 'не удалось сохранить');
          });
      });
      fix.addEventListener('click', () => { fixBox.classList.toggle('hide'); fixInput.focus(); });
      fixSend.addEventListener('click', () => {
        const text = String(fixInput.value || '').trim();
        if (!text) { fixInput.focus(); return; }
        fixSend.disabled = true;
        apiCall('post', roomPath + '/dialog/' + encodeURIComponent(dialog.id) + '/fix', { text })
          .then((res) => { dialog = (res && res.dialog) || dialog; render(); schedulePoll(); })
          .catch((err) => { fixSend.disabled = false; line.textContent = String((err && err.message) || 'не удалось отправить'); });
      });
      wrap.appendChild(h('div', { class: 'rcp-talk__row' }, yes, fix));
      wrap.appendChild(fixBox);
      wrap.appendChild(line);
    }
    if (state.recipe_id) {
      wrap.appendChild(h('div', { class: 'small muted' }, 'Рецепт в списке — можно запускать.'));
    }
    return wrap;
  }

  function schedulePoll() {
    if (stopped) return;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = 0; }
    if (!dialog || !dialog.id || String(dialog.stage || '') !== 'asked') return;
    pollTimer = setTimeout(() => {
      pollTimer = 0;
      if (stopped || !dialog) return;
      apiGet(roomPath + '/dialog/' + encodeURIComponent(dialog.id))
        .then((res) => {
          if (stopped) return;
          const next = (res && res.dialog) || null;
          if (next && JSON.stringify(next) !== JSON.stringify(dialog)) {
            dialog = next;
            render();
          }
          schedulePoll();
        })
        .catch(() => schedulePoll());
    }, 3000);
  }

  function createDialog(straight = false) {
    const wrap = h('div', { class: 'rcp-form' });
    const text = h('textarea', {
      class: 'input', rows: '4', 'data-testid': 'recipe-wish',
      placeholder: 'например: каждую пятницу сводим кассу за неделю — кассир считает, '
        + 'директор проверяет и подписывает',
    });
    const errorLine = h('div', { class: 'rcp-form__error small', role: 'alert' });
    wrap.appendChild(h('p', { class: 'small muted' },
      'Расскажите своими словами, какое дело повторяется. Руководитель задаст пару вопросов '
      + 'и пришлёт готовый алгоритм: поля, шаги, зависимости, проверку критиком.'));
    wrap.appendChild(text);
    wrap.appendChild(h('p', { class: 'small muted' },
      'Можно надиктовать: кнопка «Диктовка» есть в поле задачи — текст попадёт сюда же.'));
    wrap.appendChild(errorLine);
    const go = h('button', { class: 'btn btn--primary', type: 'button' }, 'Отправить руководителю');
    const manual = h('button', { class: 'btn btn--ghost', type: 'button' }, 'Собрать вручную');
    const shell = buildDialog({
      title: 'Рецепт из разговора',
      body: wrap,
      buttons: [h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => shell.close() }, 'Отмена'),
        manual, go],
      onCancel: () => shell.close(),
    });
    manual.addEventListener('click', () => { shell.close(); editorDialog(null); });
    go.addEventListener('click', () => {
      const value = String(text.value || '').trim();
      if (value.length < 5) { errorLine.textContent = 'Опишите дело хотя бы одним предложением.'; return; }
      go.disabled = true;
      apiCall('post', roomPath + '/dialog', { text: value })
        .then((res) => {
          shell.close();
          dialog = (res && res.dialog) || null;
          toast('Ушло руководителю: он задаст вопросы или пришлёт алгоритм', 'ok');
          render();
          schedulePoll();
        })
        .catch((err) => {
          go.disabled = false;
          errorLine.textContent = String((err && err.message) || 'не удалось отправить');
        });
    });
    if (straight) text.focus();
  }

  /* ── лёгкий разбор завершённых задач (W31-C) ─────────────────────────── */

  function analysisPanel() {
    const analysis = (data && data.analysis) || null;
    const box = h('div', { class: 'rcp-analysis', 'data-testid': 'recipes-analysis' });
    const offers = (analysis && analysis.offers) || [];
    const fresh = offers.filter((o) => o.state === 'new');
    box.appendChild(h('div', { class: 'rcp-analysis__head' },
      h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button',
        'aria-expanded': showAnalysis ? 'true' : 'false',
        onclick: () => { showAnalysis = !showAnalysis; render(); },
      }, (showAnalysis ? '▾ ' : '▸ ') + 'Разбор завершённых задач'
        + (fresh.length ? ' · предложений: ' + fresh.length : '')),
      analysis ? h('span', { class: 'small muted' }, analysisLine(analysis)) : null));
    if (!analysis) return box;
    // W31-FIX3: «пора разобрать задачи» — плашка с двумя решениями человека.
    // Карточку руководителю система сама не ставит и токены не тратит.
    const prompt = analysis.prompt || {};
    if (prompt.show) {
      const dueStatus = h('div', { class: 'rcp-form__error small', role: 'alert' });
      const dueRun = h('button', {
        class: 'btn btn--primary btn--sm', type: 'button', 'data-testid': 'analysis-due-run',
      }, 'Запустить');
      const dueLater = h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button', 'data-testid': 'analysis-due-later',
      }, 'Позже');
      dueRun.addEventListener('click', () => startAnalysis(dueRun, dueStatus));
      dueLater.addEventListener('click', () => {
        dueLater.disabled = true;
        apiCall('post', roomPath + '/analysis/later',
          { days: Number(((analysis.settings || {}).every_days) || 7) })
          .then((res) => {
            toast(laterLine((res && res.days) || ((analysis.settings || {}).every_days)), 'ok');
            load();
          })
          .catch((err) => {
            dueLater.disabled = false;
            dueStatus.textContent = String((err && err.message) || 'не удалось отложить');
          });
      });
      box.appendChild(h('div', { class: 'rcp-due', 'data-testid': 'analysis-due' },
        h('span', { class: 'rcp-due__text' }, dueBanner(analysis)),
        h('span', { class: 'rcp-due__actions' }, dueRun, dueLater),
        dueStatus));
    }
    if (!analysis.settings.enabled) {
      box.appendChild(h('p', { class: 'small muted', 'data-testid': 'analysis-off' },
        'Разбор выключен: руководитель не читает задачи и токены не тратятся. '
        + 'Включите «включён» в настройках ниже — тогда система будет предлагать разбор '
        + 'раз в N дней, но запускать его всё равно будете вы.'));
    }
    for (const offer of fresh) box.appendChild(offerRow(offer));
    if (!showAnalysis) return box;

    const settings = analysis.settings;
    const days = h('input', {
      class: 'input input--sm rcp-analysis__num', type: 'number', min: '1', max: '90',
      value: String(settings.every_days || 7), 'aria-label': 'раз в сколько дней',
    });
    const minTasks = h('input', {
      class: 'input input--sm rcp-analysis__num', type: 'number', min: '2', max: '20',
      value: String(settings.min_tasks || 3), 'aria-label': 'не меньше скольких задач',
    });
    const enabled = h('input', { type: 'checkbox', checked: settings.enabled ? 'checked' : undefined });
    const model = h('select', { class: 'input input--sm', 'aria-label': 'модель разбора' },
      h('option', { value: '', selected: settings.model && settings.model.model ? undefined : 'selected' },
        'модель руководителя' + (settings.lead_model && settings.lead_model.model
          ? ' (' + settings.lead_model.model + ')' : '')),
      (settings.models || []).map((m) => h('option', {
        value: m.provider + '|' + m.model,
        selected: settings.model && settings.model.model === m.model
          && settings.model.provider === m.provider ? 'selected' : undefined,
      }, m.label + ' · ' + m.provider)));
    const status = h('div', { class: 'rcp-form__error small', role: 'alert' });
    const save = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Сохранить настройки');
    const run = h('button', {
      class: 'btn btn--primary btn--sm', type: 'button', 'data-testid': 'analysis-run',
    }, 'Проверить сейчас');
    save.addEventListener('click', () => {
      const pair = String(model.value || '').split('|');
      const payload = {
        enabled: Boolean(enabled.checked),
        every_days: Number(days.value || 7),
        min_tasks: Number(minTasks.value || 3),
        model: pair[1] ? { provider: pair[0], model: pair[1] } : {},
      };
      save.disabled = true;
      apiCall('put', roomPath + '/analysis/settings', payload)
        .then(() => { save.disabled = false; toast('Настройки разбора сохранены', 'ok'); load(); })
        .catch((err) => { save.disabled = false; status.textContent = String((err && err.message) || 'не удалось сохранить'); });
    });
    run.addEventListener('click', () => startAnalysis(run, status));
    box.appendChild(h('div', { class: 'rcp-analysis__settings' },
      h('label', { class: 'rcp-edit-check small' }, enabled, 'включён'),
      h('label', { class: 'rcp-analysis__field small' }, 'раз в', days, 'дней'),
      h('label', { class: 'rcp-analysis__field small' }, 'не меньше', minTasks, 'задач'),
      h('label', { class: 'rcp-analysis__field small' }, 'модель', model)));
    box.appendChild(h('p', { class: 'small muted', 'data-testid': 'analysis-estimate' }, estimateLine(analysis)));
    box.appendChild(h('p', { class: 'small muted' },
      'Разбор только предлагает: рецепт появится здесь, когда вы нажмёте «Сделать рецепт». '
      + 'Похожие на уже существующие рецепты и отклонённые вами предложения он не показывает.'));
    box.appendChild(h('div', { class: 'rcp-talk__row' }, run, save));
    box.appendChild(status);
    const old = offers.filter((o) => o.state !== 'new');
    if (old.length) {
      box.appendChild(h('div', { class: 'small muted' },
        'Уже разобрано: ' + old.map((o) => o.name + ' — ' + o.state_ru).join('; ')));
    }
    return box;
  }

  function startAnalysis(btn, status) {
    btn.disabled = true;
    apiCall('post', roomPath + '/analysis/run', {})
      .then((res) => {
        toast((res && res.message) || 'Анализ поставлен руководителю', 'ok');
        load();
        pollAnalysis(0);
      })
      .catch((err) => {
        btn.disabled = false;
        if (status) status.textContent = String((err && err.message) || 'не удалось запустить');
      });
  }

  function pollAnalysis(attempt) {
    if (stopped || attempt > 8) return;
    setTimeout(() => {
      if (stopped) return;
      apiCall('post', roomPath + '/analysis/sync', {})
        .then((res) => {
          if (stopped) return;
          if (res && res.added) { toast('Руководитель прислал предложения', 'ok'); load(); }
          else if (attempt < 3) pollAnalysis(attempt + 1);
          else load();
        })
        .catch(() => { if (attempt < 3) pollAnalysis(attempt + 1); });
    }, 2500);
  }

  function offerRow(offer) {
    const yes = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, 'Сделать рецепт');
    const no = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'Не надо');
    yes.addEventListener('click', () => {
      yes.disabled = true;
      apiCall('post', roomPath + '/analysis/' + encodeURIComponent(offer.id) + '/accept', {})
        .then(() => { toast('Рецепт создан из предложения', 'ok'); load(); })
        .catch((err) => { yes.disabled = false; toast(String((err && err.message) || 'не удалось'), 'error'); });
    });
    no.addEventListener('click', () => {
      no.disabled = true;
      apiCall('post', roomPath + '/analysis/' + encodeURIComponent(offer.id) + '/dismiss', {})
        .then(() => { toast('Больше не предложим', 'ok'); load(); })
        .catch((err) => { no.disabled = false; toast(String((err && err.message) || 'не удалось'), 'error'); });
    });
    return h('div', { class: 'rcp-offer', 'data-testid': 'analysis-offer' },
      h('div', { class: 'rcp-offer__main' },
        h('div', { class: 'rcp-offer__title' }, offer.name),
        h('div', { class: 'small muted' }, offerLine(offer)),
        h('div', { class: 'small muted' }, draftLine(offer)),
        offer.problem ? h('div', { class: 'rcp-form__error small' }, offer.problem) : null),
      h('div', { class: 'rcp-card__actions' }, yes, no));
  }

  /* ── примеры чужих сфер ──────────────────────────────────────────────── */

  function samplesBlock(samples) {
    const wrap = h('div', { class: 'rcp-samples', 'data-testid': 'recipes-samples' });
    wrap.appendChild(h('button', {
      class: 'btn btn--ghost btn--sm', type: 'button',
      onclick: () => { showSamples = !showSamples; render(); },
    }, (showSamples ? '▾ ' : '▸ ') + 'Примеры из других сфер (' + samples.length + ') · помечены «образец»'));
    if (!showSamples) return wrap;
    const list = h('div', { class: 'rcp__list rcp__list--samples' });
    for (const item of samples) {
      list.appendChild(h('div', { class: 'rcp-card rcp-card--sample' },
        h('div', { class: 'rcp-card__main' },
          h('div', { class: 'rcp-card__title-row' },
            h('div', { class: 'rcp-card__title' }, item.name),
            h('span', { class: 'chip chip--muted', 'data-testid': 'sample-chip' }, 'образец')),
          h('div', { class: 'rcp-card__desc small muted' }, sampleLabel(item)),
          h('div', { class: 'rcp-card__meta small muted' }, recipeLine(item))),
        h('div', { class: 'rcp-card__actions' },
          h('button', {
            class: 'btn btn--secondary btn--sm', type: 'button',
            onclick: () => takeSample(item),
          }, 'Взять за основу'))));
    }
    wrap.appendChild(list);
    return wrap;
  }

  function takeSample(item) {
    const payload = { name: item.name + ' (моя версия)', description: item.description || '',
      fields: item.fields || [], steps: item.steps || [] };
    apiCall('post', roomPath, payload)
      .then(() => { toast('Пример стал вашим рецептом — поправьте под себя', 'ok'); load(); })
      .catch((err) => toast(String((err && err.message) || 'не удалось взять пример'), 'error'));
  }

  function recipeCard(item) {
    const layers = stepLayers(item.steps);
    const actions = h('div', { class: 'rcp-card__actions' },
      h('button', {
        class: 'btn btn--primary btn--sm', type: 'button',
        onclick: () => launchDialog(item),
      }, icon('play', { size: 12 }), 'Запустить'),
      h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => editorDialog(item),
      }, icon('edit', { size: 12 }), 'Изменить'),
      h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button', title: 'В корзину',
        onclick: () => removeRecipe(item),
      }, icon('trash', { size: 12 }), 'В корзину'));
    const chain = h('div', { class: 'rcp-card__steps' });
    layers.forEach((layer, index) => {
      const row = h('div', { class: 'rcp-card__layer' },
        h('span', { class: 'rcp-card__layer-n tnum' }, String(index + 1)),
        h('span', { class: 'rcp-card__step-text' }, layer.map((s) => s.title || stepRole(s)).join(' · ')));
      chain.appendChild(row);
    });
    return h('div', { class: 'rcp-card' },
      h('div', { class: 'rcp-card__main' },
        h('div', { class: 'rcp-card__title-row' },
          h('div', { class: 'rcp-card__title' }, item.name),
          item.sample ? h('span', { class: 'chip chip--muted' }, 'образец') : null,
          item.source === 'request' ? h('span', { class: 'chip chip--muted' }, 'из задачи') : null,
          item.source === 'dialog' ? h('span', { class: 'chip chip--muted' }, 'из разговора') : null,
          item.source === 'analysis' ? h('span', { class: 'chip chip--muted' }, 'из разбора') : null),
        item.description ? h('div', { class: 'rcp-card__desc' }, item.description) : null,
        h('div', { class: 'rcp-card__meta small muted' }, recipeLine(item)),
        chain),
      actions);
  }

  function runRow(run) {
    const steps = run.steps || [];
    const failed = steps.filter((s) => s.status === 'failed');
    const row = h('div', { class: 'rcp-run' },
      h('div', { class: 'rcp-run__main' },
        h('div', { class: 'rcp-run__title' },
          h('a', {
            class: 'rcp-run__link', href: '#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(run.request_id),
            onclick: (ev) => { if (typeof ctx.navigate === 'function') { ev.preventDefault(); ctx.navigate('#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(run.request_id)); } },
          }, run.recipe_name || 'Запуск'),
          h('span', { class: 'chip chip--muted' }, runStateRu(run.state))),
        h('div', { class: 'rcp-run__meta small muted' },
          fmtDateTime(run.at) + ' · ' + steps.length + ' '
          + plural(steps.length, ['шаг', 'шага', 'шагов'])
          + (failed.length ? ' · не создались: ' + failed.length : '')),
        h('div', { class: 'rcp-run__steps small' },
          steps.map((s) => (s.title || 'шаг') + ' — ' + stepStatusRu(s.status)).join('; ')),
        failed.length ? h('div', { class: 'rcp-run__error small' },
          'Причина: ' + (failed[0].error || 'не удалось создать карточку') +
          ' Действие: проверьте доску и профили участников, затем запустите ещё раз.') : null));
    return row;
  }

  /* ── запуск ─────────────────────────────────────────────────────────── */

  function launchDialog(item) {
    const inputs = {};
    const form = h('div', { class: 'rcp-form' });
    form.appendChild(h('p', { class: 'small muted' },
      'Заполните поля — дальше приложение само создаст задачу и карточки шагов.'));
    for (const fieldItem of item.fields || []) {
      const id = 'rcp-' + String(fieldItem.id || '');
      const control = fieldItem.kind === 'due'
        ? h('input', { class: 'input', id, type: 'date' })
        : h('input', {
          class: 'input', id, type: 'text',
          placeholder: fieldItem.kind === 'file' ? 'полный путь к файлу или папке' : (fieldItem.hint || ''),
        });
      inputs[fieldItem.id] = control;
      form.appendChild(h('label', { class: 'rcp-field', for: id },
        h('span', { class: 'rcp-field__label' },
          String(fieldItem.label || fieldItem.id),
          fieldItem.required ? h('span', { class: 'rcp-field__req' }, ' *') : null),
        control,
        fieldItem.hint && fieldItem.kind !== 'file'
          ? h('span', { class: 'rcp-field__hint small muted' }, fieldItem.hint) : null));
    }
    if (!(item.fields || []).length) {
      form.appendChild(h('p', { class: 'small muted' }, 'У этого рецепта нет полей — можно запускать сразу.'));
    }
    const errorLine = h('div', { class: 'rcp-form__error small', role: 'alert' });
    form.appendChild(errorLine);

    const submit = h('button', { class: 'btn btn--primary', type: 'button' }, icon('play', { size: 13 }), 'Запустить');
    const shell = buildDialog({
      title: 'Запустить: ' + item.name,
      body: form,
      buttons: [h('button', {
        class: 'btn btn--secondary', type: 'button',
        onclick: () => shell.close(),
      }, 'Отмена'), submit],
      onCancel: () => shell.close(),
    });
    submit.addEventListener('click', () => {
      const values = {};
      for (const [id, control] of Object.entries(inputs)) values[id] = String(control.value || '').trim();
      submit.disabled = true;
      apiCall('post', roomPath + '/' + encodeURIComponent(item.id) + '/launch', { values })
        .then((res) => {
          shell.close();
          toast((res && res.message) || 'Рецепт запущен', 'ok');
          load();
        })
        .catch((err) => {
          submit.disabled = false;
          errorLine.textContent = String((err && err.message) || 'не удалось запустить');
        });
    });
  }

  /* ── редактор ───────────────────────────────────────────────────────── */

  function editorDialog(item) {
    const isNew = !item;
    const draft = {
      name: (item && item.name) || '',
      description: (item && item.description) || '',
      fields: ((item && item.fields) || []).map((f) => ({ ...f })),
      steps: ((item && item.steps) || []).map((s) => ({ ...s, depends_on: [...(s.depends_on || [])] })),
    };
    if (!draft.fields.length) draft.fields.push({ id: 'f1', label: 'Что нужно', kind: 'text', required: true, hint: '' });
    if (!draft.steps.length) draft.steps.push({ id: 's1', role: 'руководитель', title: '', instruction: '', depends_on: [] });

    const form = h('div', { class: 'rcp-form' });
    const nameInput = h('input', { class: 'input', type: 'text', value: draft.name, placeholder: 'например: ответ клиенту на претензию' });
    const descInput = h('textarea', { class: 'input', rows: '2', placeholder: 'что делает рецепт и когда им пользоваться' }, draft.description);
    form.appendChild(h('label', { class: 'rcp-field' }, h('span', { class: 'rcp-field__label' }, 'Название *'), nameInput));
    form.appendChild(h('label', { class: 'rcp-field' }, h('span', { class: 'rcp-field__label' }, 'Описание'), descInput));

    const fieldsBox = h('div', { class: 'rcp-edit-fields' });
    const stepsBox = h('div', { class: 'rcp-edit-steps' });
    const errorLine = h('div', { class: 'rcp-form__error small', role: 'alert' });

    function drawFields() {
      fieldsBox.replaceChildren(h('div', { class: 'rcp-form__subtitle' }, 'Поля ввода'));
      draft.fields.forEach((fieldItem, index) => {
        const label = h('input', { class: 'input input--sm', type: 'text', value: fieldItem.label || '', placeholder: 'подпись поля' });
        const kind = h('select', { class: 'input input--sm' },
          Object.entries(FIELD_KIND_RU).map(([value, text]) => h('option', { value, selected: (fieldItem.kind || 'text') === value ? 'selected' : undefined }, text)));
        const required = h('input', { type: 'checkbox', checked: fieldItem.required ? 'checked' : undefined });
        label.addEventListener('input', () => { fieldItem.label = label.value; });
        kind.addEventListener('change', () => { fieldItem.kind = kind.value; });
        required.addEventListener('change', () => { fieldItem.required = Boolean(required.checked); });
        fieldsBox.appendChild(h('div', { class: 'rcp-edit-row' },
          label, kind,
          h('label', { class: 'rcp-edit-check small' }, required, 'обязательное'),
          h('button', {
            class: 'btn btn--ghost btn--sm', type: 'button', title: 'Убрать поле',
            onclick: () => { draft.fields.splice(index, 1); drawFields(); },
          }, icon('close', { size: 12 }))));
      });
      fieldsBox.appendChild(h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => {
          if (draft.fields.length >= 8) return;
          draft.fields.push({ id: 'f' + (draft.fields.length + 1), label: '', kind: 'text', required: false, hint: '' });
          drawFields();
        },
      }, icon('plus', { size: 12 }), 'Поле'));
    }

    function drawSteps() {
      stepsBox.replaceChildren(h('div', { class: 'rcp-form__subtitle' }, 'Шаги: кто, что и после кого'));
      draft.steps.forEach((step, index) => {
        const role = h('input', {
          class: 'input input--sm', type: 'text', value: step.role || step.assignee || '',
          placeholder: 'роль: юрист, оператор, руководитель',
        });
        const title = h('input', { class: 'input input--sm', type: 'text', value: step.title || '', placeholder: 'что сделать в этом шаге' });
        const instruction = h('textarea', { class: 'input input--sm', rows: '2', placeholder: 'подробнее для исполнителя (не обязательно)' }, step.instruction || '');
        const depends = h('select', {
          class: 'input input--sm', multiple: 'multiple', size: '3', title: 'после каких шагов',
        }, draft.steps.filter((s) => s !== step).map((s, i) => h('option', {
          value: s.id,
          selected: (step.depends_on || []).includes(s.id) ? 'selected' : undefined,
        }, String(indexOfStep(s) + 1) + '. ' + (s.title || 'шаг'))));
        role.addEventListener('input', () => { step.role = role.value; });
        title.addEventListener('input', () => { step.title = title.value; });
        instruction.addEventListener('input', () => { step.instruction = instruction.value; });
        depends.addEventListener('change', () => {
          step.depends_on = Array.from(depends.selectedOptions || []).map((o) => o.value);
        });
        stepsBox.appendChild(h('div', { class: 'rcp-edit-step' },
          h('div', { class: 'rcp-edit-step__head' },
            h('span', { class: 'rcp-edit-step__n' }, String(index + 1)),
            role,
            h('button', {
              class: 'btn btn--ghost btn--sm', type: 'button', title: 'Убрать шаг',
              onclick: () => { draft.steps.splice(index, 1); drawSteps(); },
            }, icon('close', { size: 12 }))),
          title, instruction,
          h('label', { class: 'rcp-field rcp-field--tight' },
            h('span', { class: 'rcp-field__label small muted' }, 'Зависит от шагов (не обязательно)'),
            depends)));
      });
      stepsBox.appendChild(h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => {
          if (draft.steps.length >= 10) return;
          draft.steps.push({ id: 's' + (draft.steps.length + 1), role: '', title: '', instruction: '', depends_on: [] });
          drawSteps();
        },
      }, icon('plus', { size: 12 }), 'Шаг'));
    }

    function indexOfStep(step) { return draft.steps.indexOf(step); }
    drawFields();
    drawSteps();
    form.appendChild(fieldsBox);
    form.appendChild(stepsBox);
    form.appendChild(errorLine);

    const save = h('button', { class: 'btn btn--primary', type: 'button' }, 'Сохранить');
    const shell = buildDialog({
      title: isNew ? 'Новый рецепт' : 'Рецепт: ' + item.name,
      body: form, wide: true,
      buttons: [h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => shell.close() }, 'Отмена'), save],
      onCancel: () => shell.close(),
    });
    save.addEventListener('click', () => {
      const payload = {
        name: String(nameInput.value || '').trim(),
        description: String(descInput.value || '').trim(),
        fields: draft.fields,
        steps: draft.steps.map((s) => ({
          id: s.id, role: s.role, assignee: '',
          title: s.title, instruction: s.instruction, depends_on: s.depends_on || [],
        })),
      };
      save.disabled = true;
      const call = isNew
        ? apiCall('post', roomPath, payload)
        : apiCall('patch', roomPath + '/' + encodeURIComponent(item.id), payload);
      call.then(() => { shell.close(); toast('Рецепт сохранён', 'ok'); load(); })
        .catch((err) => { save.disabled = false; errorLine.textContent = String((err && err.message) || 'не удалось сохранить'); });
    });
  }

  /* ── из запроса ─────────────────────────────────────────────────────── */

  function fromRequestDialog() {
    const list = h('div', { class: 'rcp-form' });
    const pick = h('div', { class: 'rcp-requests' });
    const errorLine = h('div', { class: 'rcp-form__error small', role: 'alert' });
    let chosen = '';
    list.appendChild(h('p', { class: 'small muted' },
      'Выберите завершённую задачу — руководитель прочитает её шаги и предложит рецепт. '
      + 'Вы увидите алгоритм и утвердите его.'));
    list.appendChild(pick);
    list.appendChild(errorLine);
    pick.appendChild(skeleton(2));
    apiGet('/api/rooms/' + encodeURIComponent(roomId) + '/requests?limit=20')
      .then((payload) => {
        const items = (payload && payload.items) || [];
        pick.replaceChildren();
        if (!items.length) {
          pick.appendChild(emptyState({
            icon: 'inbox', title: 'Задач пока нет',
            text: 'Рецепт можно собрать и вручную: «Собрать вручную».',
          }));
          return;
        }
        for (const item of items) {
          const btn = h('button', {
            class: 'rcp-request', type: 'button', 'data-id': item.id,
            onclick: () => {
              chosen = item.id;
              for (const other of pick.querySelectorAll('.rcp-request')) other.classList.remove('is-active');
              btn.classList.add('is-active');
            },
          }, h('span', { class: 'rcp-request__title' }, item.title || item.id),
            h('span', { class: 'rcp-request__meta small muted' }, item.status || ''));
          pick.appendChild(btn);
        }
      })
      .catch(() => {
        pick.replaceChildren(h('p', { class: 'small muted' }, 'Список задач не загрузился — можно собрать рецепт вручную.'));
      });

    const go = h('button', { class: 'btn btn--primary', type: 'button' }, 'Собрать рецепт с руководителем');
    const shell = buildDialog({
      title: 'Сохранить задачу как рецепт', body: list,
      buttons: [h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => shell.close() }, 'Отмена'), go],
      onCancel: () => shell.close(),
    });
    go.addEventListener('click', () => {
      if (!chosen) { errorLine.textContent = 'Выберите задачу из списка.'; return; }
      go.disabled = true;
      apiCall('post', roomPath + '/from-request', { request_id: chosen })
        .then((res) => {
          shell.close();
          dialog = (res && res.dialog) || null;
          toast('Задача ушла руководителю — алгоритм появится здесь', 'ok');
          render();
          schedulePoll();
        })
        .catch((err) => { go.disabled = false; errorLine.textContent = String((err && err.message) || 'не удалось отправить'); });
    });
  }

  function removeRecipe(item) {
    apiCall('del', roomPath + '/' + encodeURIComponent(item.id))
      .then((res) => { toast((res && res.message) || 'Рецепт в корзине', 'ok'); load(); })
      .catch((err) => toast(String((err && err.message) || 'не удалось убрать рецепт'), 'error'));
  }

  function restoreRecipe(item) {
    apiCall('post', roomPath + '/' + encodeURIComponent(item.id) + '/restore', {})
      .then(() => { toast('Рецепт вернулся', 'ok'); load(); })
      .catch((err) => toast(String((err && err.message) || 'не удалось вернуть рецепт'), 'error'));
  }

  load();
  return {
    unmount() {
      stopped = true;
      if (pollTimer) clearTimeout(pollTimer);
    },
  };
}

export default { mount };
