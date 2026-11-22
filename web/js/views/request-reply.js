/**
 * request-reply.js — «Написать руководителю / дать указание» и кнопки заблокированной
 * карточки (W29-P2).
 *
 * Что здесь и почему отдельным файлом
 * ------------------------------------
 * 1. **Постоянное поле внизу страницы запроса** — «Написать руководителю / дать
 *    указание» с микрофоном («Надиктовать», та же диктовка, что в остальном
 *    интерфейсе) и кнопкой отправки. Поле доступно в ЛЮБОМ статусе запроса:
 *    человеку нужно уметь двинуть работу, не разыскивая кнопку по экрану.
 * 2. **Кнопки заблокированной карточки** — один список на все места экрана:
 *    правая колонка «Нужен ваш ответ», вкладка «От вас нужно», нижняя панель.
 *    Раньше в колонке была только ссылка на вкладку, а на вкладке — одно поле
 *    ответа: заблокированная карточка считалась в числе «ждёт вашего решения»,
 *    но двинуть её было нечем (тупик).
 *
 * Новых маршрутов нет: все кнопки шлют действия, которые сервер уже принимает
 * (`ACTION_ALLOWED` в `server/app_common.py`):
 *
 * | кнопка                    | действие                            |
 * |--------------------------|-------------------------------------|
 * | «Продолжить»             | `{action:'unblock', task_id}`        |
 * | «Дать указание»          | `comment` (к карточке) + `unblock`   |
 * | «Сменить модель…»        | `{action:'card_model', task_id, model}` |
 * | «Переназначить»          | `{action:'reassign', task_id, profile}` |
 * | «Не актуально»          | `{action:'archive', task_id}`        |
 * | поле внизу страницы       | `comment` (+ `unblock`, если карточка заблокирована) |
 *
 * Модуль ничего не знает о состоянии страницы: ему дают `api`, `requestId`,
 * `taskId` и `onDone`. Поэтому его одинаково используют страница запроса,
 * правая колонка и вкладка «От вас нужно» (комната без стора запроса).
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { createMic } from '../dictation.js';

/** Подпись поля внизу страницы — одна на все места. */
export const REPLY_LABEL = 'Написать руководителю / дать указание';
/** Что произойдёт после отправки — подсказка рядом с полем (правило «без тупиков»). */
export const REPLY_HINT = 'Слова уйдут руководителю комнаты комментарием к запросу. '
  + 'Если карточка ждёт вашего решения, она продолжится сразу после отправки.';
/** Подсказка к заблокированной карточке: почему она стоит и что будет после ответа. */
export const BLOCKED_HINT = 'Работа стоит, пока вы не скажете, что делать дальше. '
  + 'Ответ уйдёт комментарием в карточку, и она продолжится.';

/**
 * Кнопки заблокированной карточки — ОДИН список для всех мест экрана.
 *
 * `id` — имя действия в терминах интерфейса (не сервера): по нему тесты и
 * `tools/check_dead_ends.py` проверяют «у пункта есть чем нажать».
 *
 * @returns {Array<{id:string,label:string,tone:string,hint:string}>}
 */
export function blockedActionList() {
  return [
    { id: 'resume', label: 'Продолжить', tone: 'btn--primary',
      hint: 'Снять паузу: карточка вернётся в очередь, участник продолжит работу' },
    { id: 'instruct', label: 'Дать указание / ответить', tone: 'btn--secondary',
      hint: 'Написать или надиктовать, что делать дальше: слова уйдут в карточку, и она продолжится' },
    { id: 'model', label: 'Сменить модель…', tone: 'btn--ghost',
      hint: 'Запустить карточку на другой модели комнаты' },
    { id: 'reassign', label: 'Переназначить', tone: 'btn--ghost',
      hint: 'Отдать карточку другому участнику комнаты' },
    { id: 'dismiss', label: 'Не актуально', tone: 'btn--ghost',
      hint: 'Карточка уйдёт в архив: ответ от вас больше не нужен' },
  ];
}

/**
 * Тело «дать указание» для сервера: комментарий к карточке или к корню запроса.
 * @param {string} requestId
 * @param {{taskId?:string, text?:string}} o
 */
export function replyBody(requestId, o = {}) {
  const rid = String(requestId || '').trim();
  const body = { action: 'comment', text: String((o && o.text) || '') };
  const taskId = String((o && o.taskId) || '').trim();
  if (taskId) body.task_id = taskId;
  return Object.assign({ request_id: rid }, body);
}

/**
 * План отправки из нижнего поля: комментарий и (если карточка стоит) снятие паузы.
 * Отдельной функцией, чтобы контракт «клиент ↔ сервер» проверялся тестом без DOM.
 *
 * @param {string} requestId
 * @param {{text?:string, taskIds?:string[]}} o
 * @returns {Array<{path:string, body:object, what:string}>}
 */
export function replyPlan(requestId, o = {}) {
  const rid = String(requestId || '').trim();
  const text = String((o && o.text) || '');
  const tasks = (Array.isArray(o && o.taskIds) ? o.taskIds : [])
    .map((id) => String(id || '').trim()).filter(Boolean);
  const path = '/api/requests/' + encodeURIComponent(rid) + '/actions';
  const plan = [{ path, body: replyBody(rid, { text }), what: 'комментарий' }];
  for (const taskId of tasks) {
    plan.push({ path, body: { action: 'unblock', task_id: taskId, text }, what: 'снятие паузы' });
  }
  return plan;
}

/** Тело кнопки по её имени (один список — одно место правды). */
export function blockedActionBody(actionId, o = {}) {
  const taskId = String((o && o.taskId) || '');
  switch (String(actionId || '')) {
    case 'resume':
      return { action: 'unblock', task_id: taskId };
    case 'model':
      return { action: 'card_model', task_id: taskId, model: String((o && o.model) || '') };
    case 'reassign':
      return { action: 'reassign', task_id: taskId, profile: String((o && o.profile) || '') };
    case 'dismiss':
      return { action: 'archive', task_id: taskId };
    case 'instruct':
      return { action: 'comment', task_id: taskId, text: String((o && o.text) || '') };
    default:
      return {};
  }
}

/** Путь действий запроса. */
function actionsPath(requestId) {
  return '/api/requests/' + encodeURIComponent(String(requestId || '')) + '/actions';
}

/** Модели комнаты для «Сменить модель…»: уникальные пары провайдер · модель из участников. */
export function roomModels(room) {
  const out = [];
  const seen = new Set();
  for (const member of (room && Array.isArray(room.members) ? room.members : [])) {
    const settings = (member && member.model_settings) || {};
    const model = String(settings.model || '').trim();
    if (!model || seen.has(model)) continue;
    seen.add(model);
    out.push({ model, label: [settings.provider, model].filter(Boolean).join(' · ') });
  }
  return out;
}

/** Участники комнаты для «Переназначить». */
export function roomPeople(room) {
  return (room && Array.isArray(room.members) ? room.members : [])
    .map((m) => String((m && m.profile) || '').trim())
    .filter(Boolean);
}

/* ─────────────────────────────────────────── строка кнопок карточки ── */

/**
 * Кнопки заблокированной карточки: «Продолжить», «Дать указание / ответить»,
 * «Сменить модель…», «Переназначить», «Не актуально».
 *
 * Формы («указание», «модель», «переназначить») раскрываются ПОД строкой —
 * человек не уходит со страницы и не ищет ту же карточку в другом месте.
 *
 * @param {{card:object, api:object, toast?:Function, onChange?:Function,
 *          models?:Array, people?:Array, systemPause?:boolean}} deps
 * @returns {HTMLElement}
 */
export function blockedCardActions(deps = {}) {
  ensureCss('css/request-reply.css');
  const card = deps.card || {};
  const requestId = String(card.request_id || deps.requestId || '');
  const taskId = String(card.task_id || '');
  const api = deps.api || null;
  const toast = typeof deps.toast === 'function' ? deps.toast : () => {};
  const onChange = typeof deps.onChange === 'function' ? deps.onChange : () => {};
  const list = deps.actions || blockedActionList();
  const box = h('div', { class: 'req-reply__acts', role: 'group', 'data-blocked-actions': taskId });
  const formHost = h('div', { class: 'req-reply__form-host' });

  async function post(body) {
    if (!api || typeof api.post !== 'function') throw new Error('Нет связи с сервером');
    return api.post(actionsPath(requestId), body);
  }

  async function run(actionId, body, okText) {
    try {
      await post(body);
      toast(okText, 'ok');
      onChange(card, actionId);
      return true;
    } catch (err) {
      toast((err && err.message) || 'Не удалось выполнить действие', 'danger');
      return false;
    }
  }

  function closeForm() { formHost.textContent = ''; }

  /** Поле указания с микрофоном: набрал или надиктовал — «Отправить». */
  function instructForm() {
    const area = h('textarea', {
      class: 'req-reply__input', rows: '3', maxlength: '4000',
      'data-mic': 'manual',
      'aria-label': 'Что делать дальше по задаче «' + String(card.text || '') + '»',
      placeholder: 'Что делать дальше: решение, вводные или «делай по варианту 2»',
    });
    const send = h('button', { type: 'button', class: 'btn btn--primary btn--sm' },
      icon('send', { size: 12 }), 'Отправить и продолжить');
    const cancel = h('button', { type: 'button', class: 'btn btn--ghost btn--sm' }, 'Отмена');
    let mic = null;
    try {
      mic = createMic(area, { label: true, className: 'mic-btn--action', toast });
    } catch { mic = null; }
    send.addEventListener('click', async () => {
      const text = String(area.value || '').trim();
      if (!text) { toast('Напишите или надиктуйте, что делать дальше', 'warn'); return; }
      send.disabled = true;
      // Сначала слова в карточку, потом снятие паузы: если второе не пройдёт,
      // указание всё равно останется в карточке и не потеряется.
      try {
        await post({ action: 'comment', task_id: taskId, text });
      } catch (err) {
        toast((err && err.message) || 'Не удалось записать указание', 'danger');
        send.disabled = false;
        return;
      }
      const ok = await run('instruct', { action: 'unblock', task_id: taskId, text },
        'Указание отправлено, задача продолжится');
      if (!ok) { send.disabled = false; return; }
      closeForm();
    });
    cancel.addEventListener('click', closeForm);
    const row = h('div', { class: 'row row--tight row--wrap' }, send, mic, cancel);
    const form = h('div', { class: 'req-reply__form' },
      h('div', { class: 'small muted req-reply__form-hint' }, BLOCKED_HINT), area, row);
    form._input = area;                 // фокус ставим по ссылке, без поиска по DOM
    return form;
  }

  /** Смена модели: список моделей комнаты (и своё значение, если списка нет). */
  function modelForm() {
    const models = typeof deps.models === 'function'
      ? (deps.models() || []) : (Array.isArray(deps.models) ? deps.models : []);
    const pick = h('select', { class: 'req-reply__select', 'aria-label': 'Модель для задачи' });
    if (models.length) {
      for (const m of models) pick.appendChild(h('option', { value: m.model }, m.label));
    } else {
      pick.appendChild(h('option', { value: '' }, 'Модели комнаты не прочитаны — впишите имя'));
    }
    const own = h('input', {
      type: 'text', class: 'req-reply__input', maxlength: '200',
      placeholder: 'Своя модель (если её нет в списке)', hidden: models.length ? true : null,
    });
    pick.addEventListener('change', () => {
      if (pick.value === '') own.removeAttribute('hidden');
    });
    const apply = h('button', { type: 'button', class: 'btn btn--primary btn--sm' }, 'Применить и продолжить');
    const cancel = h('button', { type: 'button', class: 'btn btn--ghost btn--sm' }, 'Отмена');
    cancel.addEventListener('click', closeForm);
    apply.addEventListener('click', async () => {
      const model = String(own.value || '').trim() || String(pick.value || '').trim();
      if (!model) { toast('Выберите модель или впишите её имя', 'warn'); return; }
      apply.disabled = true;
      const ok = await run('model', blockedActionBody('model', { taskId, model }),
        'Модель сменена — карточка продолжит на ней');
      if (!ok) { apply.disabled = false; return; }
      closeForm();
    });
    return h('div', { class: 'req-reply__form' },
      h('div', { class: 'small muted req-reply__form-hint' },
        'Карточка пойдёт на выбранной модели. Если модели нет в списке — впишите её имя.'),
      pick, own, h('div', { class: 'row row--tight row--wrap' }, apply,
        h('button', {
          type: 'button', class: 'btn btn--secondary btn--sm',
          title: 'Вернуть модель профиля участника',
          onclick: () => { own.value = ''; pick.value = ''; own.removeAttribute('hidden'); },
        }, 'Модель профиля'), cancel));
  }

  /** Переназначение: участники комнаты списком. */
  function reassignForm() {
    const people = typeof deps.people === 'function'
      ? (deps.people() || []) : (Array.isArray(deps.people) ? deps.people : []);
    const pick = h('select', { class: 'req-reply__select', 'aria-label': 'Кому поручить задачу' });
    for (const name of people) pick.appendChild(h('option', { value: name }, name));
    if (!people.length) {
      pick.appendChild(h('option', { value: '' }, 'Участники комнаты не прочитаны'));
    }
    const apply = h('button', { type: 'button', class: 'btn btn--primary btn--sm' }, 'Поручить и продолжить');
    const cancel = h('button', { type: 'button', class: 'btn btn--ghost btn--sm' }, 'Отмена');
    cancel.addEventListener('click', closeForm);
    apply.addEventListener('click', async () => {
      const profile = String(pick.value || '').trim();
      if (!profile) { toast('Выберите участника комнаты', 'warn'); return; }
      apply.disabled = true;
      const ok = await run('reassign', blockedActionBody('reassign', { taskId, profile }),
        'Задача поручена: ' + profile);
      if (!ok) { apply.disabled = false; return; }
      closeForm();
    });
    return h('div', { class: 'req-reply__form' },
      h('div', { class: 'small muted req-reply__form-hint' },
        'Задача уйдёт другому участнику комнаты и продолжится на его модели.'),
      pick, h('div', { class: 'row row--tight row--wrap' }, apply, cancel));
  }

  for (const item of list) {
    const btn = h('button', {
      type: 'button', class: 'btn btn--sm ' + item.tone, 'data-blocked-action': item.id,
      title: item.hint,
    }, item.label);
    btn.addEventListener('click', async (ev) => {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (btn.disabled) return;
      if (item.id === 'instruct') {
        formHost.textContent = '';
        const form = instructForm();
        formHost.appendChild(form);
        const field = form._input;
        if (field && typeof field.focus === 'function') { try { field.focus(); } catch { /* старый браузер */ } }
        return;
      }
      if (item.id === 'model') { formHost.textContent = ''; formHost.appendChild(modelForm()); return; }
      if (item.id === 'reassign') { formHost.textContent = ''; formHost.appendChild(reassignForm()); return; }
      btn.disabled = true;
      const ok = item.id === 'resume'
        ? await run('resume', blockedActionBody('resume', { taskId }), 'Задача продолжается')
        : await run('dismiss', blockedActionBody('dismiss', { taskId }),
          'Карточка убрана в архив — ждать вашего ответа нечего');
      if (!ok) btn.disabled = false;
    });
    box.appendChild(btn);
  }
  return h('div', { class: 'req-reply__blocked' }, box, formHost);
}

/* ───────────────────────────────────── поле «написать руководителю» ── */

/**
 * Постоянное поле внизу страницы запроса (доступно в любом статусе).
 *
 * @param {HTMLElement} host место на странице (нижняя часть `.req`)
 * @param {{api:object, requestId:string, taskIds?:Function|string[],
 *          toast?:Function, onDone?:Function, label?:string}} deps
 * @returns {{element:HTMLElement, setBlocked:Function, destroy:Function, submit:Function}}
 */
export function mountReplyBar(host, deps = {}) {
  ensureCss('css/request-reply.css');
  const api = deps.api || null;
  const toast = typeof deps.toast === 'function' ? deps.toast : () => {};
  const onDone = typeof deps.onDone === 'function' ? deps.onDone : () => {};
  const requestId = String(deps.requestId || '');
  let blockedIds = [];

  const area = h('textarea', {
    id: 'req-reply-text',
    class: 'req-reply__input req-reply__input--main',
    rows: '2', maxlength: '4000', 'data-mic': 'manual',
    'aria-label': REPLY_LABEL,
    placeholder: 'Что сказать руководителю или что делать дальше — надиктуйте или наберите. Ctrl+Enter — отправить.',
  });
  const send = h('button', {
    type: 'button', class: 'btn btn--primary req-reply__send', 'data-reply-send': 'true',
    title: 'Отправить указание (Ctrl+Enter)',
  }, icon('send', { size: 13 }), 'Отправить');
  const state = h('span', { class: 'req-reply__state small muted', 'aria-live': 'polite' }, '');
  let mic = null;
  try {
    mic = createMic(area, { label: true, className: 'mic-btn--action', toast });
  } catch { mic = null; }

  const bar = h('div', {
    class: 'req-reply', 'data-reply-bar': 'true', role: 'region',
    'aria-label': REPLY_LABEL,
  },
  h('div', { class: 'req-reply__head' },
    h('span', { class: 'req-reply__label' }, icon('comment-draft', { size: 13 }), REPLY_LABEL),
    state),
  h('div', { class: 'req-reply__row' }, area, send, mic),
  h('div', { class: 'req-reply__hint small muted' }, REPLY_HINT));

  function say(text, tone) {
    state.textContent = String(text || '');
    state.setAttribute('data-tone', String(tone || ''));
  }

  /** Сколько карточек стоит: их ответ снимает паузу, остальные — просто комментарий. */
  function setBlocked(ids) {
    blockedIds = (Array.isArray(ids) ? ids : []).map((id) => String(id || '')).filter(Boolean);
    send.textContent = '';
    send.appendChild(icon('send', { size: 13 }));
    send.appendChild(h('span', null, blockedIds.length ? 'Отправить и продолжить' : 'Отправить'));
    send.setAttribute('title', blockedIds.length
      ? 'Слова уйдут в карточку, задача продолжится (' + blockedIds.length + ')'
      : 'Отправить указание (Ctrl+Enter)');
  }

  async function submit() {
    const text = String(area.value || '').trim();
    if (!text) { say('Напишите или надиктуйте указание — пустое отправлять нечего', 'warn'); return false; }
    if (!api || typeof api.post !== 'function') { say('Нет связи с сервером', 'danger'); return false; }
    send.disabled = true;
    say('Отправляю…');
    const plan = replyPlan(requestId, { text, taskIds: blockedIds });
    let failed = '';
    for (const step of plan) {
      try {
        await api.post(step.path, step.body);
      } catch (err) {
        failed = (err && err.message) || ('не удалось: ' + step.what);
        break;
      }
    }
    send.disabled = false;
    if (failed) {
      say(failed, 'danger');
      toast(failed, 'danger');
      return false;
    }
    area.value = '';
    say(blockedIds.length
      ? 'Отправлено — задача продолжится'
      : 'Отправлено руководителю', 'ok');
    toast(blockedIds.length ? 'Отправлено: задача разблокирована' : 'Указание отправлено', 'ok');
    onDone();
    return true;
  }

  send.addEventListener('click', () => { submit(); });
  area.addEventListener('keydown', (ev) => {
    if (ev && ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      submit();
    }
  });

  if (host && typeof host.appendChild === 'function') host.appendChild(bar);
  setBlocked([]);
  return {
    element: bar,
    setBlocked,
    submit,
    destroy() {
      if (mic && typeof mic.destroy === 'function') { try { mic.destroy(); } catch { /* уже снят */ } }
      if (bar.parentNode && typeof bar.parentNode.removeChild === 'function') bar.parentNode.removeChild(bar);
    },
  };
}
