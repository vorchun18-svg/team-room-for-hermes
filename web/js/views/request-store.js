/**
 * request-store.js — один стор состояния запроса на всю страницу (волна 26, пакет D2).
 *
 * Замечание Михаила (Prep §10): «одно действие — везде». Раньше разрешения
 * рисовались двумя независимыми экземплярами (`request.js` и `request-aside.js`
 * каждый вызывал `createApprovals`) со своей копией `detail`: решение уходило на
 * сервер из одного места, а второе место продолжало показывать «ждёт решения» —
 * как будто две разные истории.
 *
 * Здесь один объект на запрос:
 *   * `requestStoreFor(rid)` возвращает ТОТ ЖЕ стор для всех мест страницы;
 *   * любое действие (`dispatch`) сначала меняет состояние оптимистично, потом
 *     отправляет запрос и сверяется с ответом сервера — ответ сервера главный;
 *   * после каждого изменения подписчики (`subscribe`) перерисовываются, и в
 *     браузере дополнительно летит событие `teamroom:request-changed` — на него
 *     могут подписаться колонка, нижний лист, «Сегодня» и бейджи меню.
 *
 * Раскладка состояния — те же четыре среза, что видит человек: `approvals`,
 * `questions`, `decisions`, `steps`. Срезы — это ЖИВЫЕ массивы объекта `detail`,
 * поэтому код, который читает `R.detail` (он у всех мест общий), видит ту же
 * правку без второй копии.
 *
 * Модуль чистый: ни DOM, ни fetch. Тесты — `node --test tests/request-store.test.mjs`.
 */

// W27-E2 §1: «Не актуально»/«Вернуть» — тела по общему контракту, 409 — не ошибка.
import { dismissBody, restoreBody, isAlreadyDecided, needsYouCount } from './stale-model.js';
// W28-D7 (дефект 7): ожидание действия всегда заканчивается — иначе ключ
// «в полёте» остаётся навсегда, и все кнопки этого пункта молча не отвечают.
import { withTimeout, lockTimeout } from '../store.js';

/** Событие «состояние запроса изменилось» (браузер). */
export const REQUEST_CHANGED = 'teamroom:request-changed';

/** Четыре среза, которые показываются в нескольких местах сразу. */
export const SLICE_NAMES = ['approvals', 'questions', 'decisions', 'steps'];

/** Поле `detail` для каждого среза (как отдаёт API). */
export const SLICE_FIELDS = {
  approvals: 'approvals',
  questions: 'open_questions',
  decisions: 'decisions',
  steps: 'next_steps',
};

/** Один стор на запрос: страница, колонка и нижний лист берут отсюда. */
const STORES = new Map();

/** Идентификатор элемента внутри среза (`id` у всех четырёх видов). */
export function itemId(item) {
  return String((item && (item.id || item.task_id)) || '');
}

/** Срез деталей API: `{approvals, questions, decisions, steps}` (массивы, не null). */
export function sliceOfDetail(detail) {
  const out = {};
  for (const name of SLICE_NAMES) {
    const field = SLICE_FIELDS[name];
    const list = detail && detail[field];
    out[name] = Array.isArray(list) ? list : [];
  }
  return out;
}

/** Короткая подпись состояния элемента — для сравнения «изменилось ли». */
function itemSignature(item) {
  if (!item || typeof item !== 'object') return '';
  return [
    itemId(item),
    item.state || '',
    item.status || '',
    item.answer_state || '',
    item.open === true ? 'open' : item.open === false ? 'closed' : '',
    item.reaction_pending === true ? 'wait' : '',
    item.decided_at || '',
    item.answer_at || '',
  ].join('|');
}

/**
 * Отпечаток срезов: дешёвая проверка «поменялось ли что-то», чтобы не гонять
 * подписчиков на каждый опрос страницы без изменений.
 * @param {object} slices
 * @returns {string}
 */
export function slicesSignature(slices) {
  const parts = [];
  for (const name of SLICE_NAMES) {
    const list = Array.isArray(slices && slices[name]) ? slices[name] : [];
    parts.push(name + ':' + list.length + ':' + list.map(itemSignature).join(','));
  }
  return parts.join(';');
}

/** Оповестить браузер (в тестах мини-DOM события нет — молча пропускаем). */
function broadcast(rid, rev, source) {
  try {
    const doc = globalThis.document;
    const Ctor = globalThis.CustomEvent;
    if (!doc || typeof doc.dispatchEvent !== 'function' || typeof Ctor !== 'function') return;
    doc.dispatchEvent(new Ctor(REQUEST_CHANGED, { detail: { rid: String(rid), rev, source } }));
  } catch { /* событие — удобство, а не условие работы */ }
}

/**
 * Стор запроса.
 *
 * @param {object} opts
 * @param {string} opts.rid
 * @param {string} [opts.roomId]
 * @param {object} [opts.detail] начальная деталь (если уже загружена)
 * @param {object} [opts.api] `{get, post}` — как `ctx.api`
 * @param {Function} [opts.refresh] перечитать страницу (запасной путь сверки)
 * @returns {object} стор
 */
export function createRequestStore(opts = {}) {
  const state = {
    rid: String(opts.rid || ''),
    roomId: String(opts.roomId || ''),
    detail: opts.detail || null,
    slices: sliceOfDetail(opts.detail || null),
    api: opts.api || null,
    refresh: typeof opts.refresh === 'function' ? opts.refresh : null,
    rev: 0,
    action: 0,
    waiting: new Map(),       // ключ действия → true, пока задача в полёте
    error: null,
    lastChange: null,
    signature: '',
  };
  const listeners = new Set();

  function touch(source) {
    state.rev += 1;
    state.signature = slicesSignature(state.slices);
    state.lastChange = { rev: state.rev, source: String(source || '') };
    const snapshot = get();
    for (const fn of Array.from(listeners)) {
      try {
        fn(snapshot, state.lastChange);
      } catch (err) {
        // Один сломанный вид не должен отменять перерисовку остальных.
        if (typeof console !== 'undefined' && console.warn) console.warn('[request-store]', err);
      }
    }
    broadcast(state.rid, state.rev, source);
  }

  /** Текущее состояние: один объект, копии срезов — только массивы-обёртки. */
  function get() {
    return {
      rid: state.rid,
      roomId: state.roomId,
      detail: state.detail,
      approvals: state.slices.approvals,
      questions: state.slices.questions,
      decisions: state.slices.decisions,
      steps: state.slices.steps,
      rev: state.rev,
      busy: state.waiting.size > 0,
      inflight: Array.from(state.waiting.keys()),
      error: state.error,
      lastChange: state.lastChange,
    };
  }

  function adopt(extra = {}) {
    if (!extra) return;
    if (extra.api && !state.api) state.api = extra.api;
    if (typeof extra.refresh === 'function') state.refresh = extra.refresh;
    if (extra.roomId && !state.roomId) state.roomId = String(extra.roomId);
    // Деталь, которую передало место страницы, — самая свежая на момент сборки:
    // принимаем её всегда, но перерисовку затеваем только при реальном изменении.
    if (extra.detail && typeof extra.detail === 'object' && extra.detail !== state.detail) {
      setDetail(extra.detail, { source: 'init', force: false, quiet: true });
    }
  }

  /**
   * Принять деталь с сервера. Сервер — единственный источник истины, поэтому
   * срезы заменяются целиком (локальные оптимистичные правки уже не нужны).
   * @param {object} detail
   * @param {{source?:string, force?:boolean, quiet?:boolean}} [o]
   * @returns {boolean} было ли изменение
   */
  function setDetail(detail, o = {}) {
    if (!detail || typeof detail !== 'object') return false;
    if (detail.id && !state.rid) state.rid = String(detail.id);
    if (!state.roomId && detail.room) state.roomId = String(detail.room);
    const next = sliceOfDetail(detail);
    const sig = slicesSignature(next);
    state.detail = detail;
    if (!o.force && sig === state.signature) {
      state.slices = next;
      return false;
    }
    state.slices = next;
    if (!o.quiet) touch(o.source || 'server');
    return true;
  }

  /** Список среза (тот же массив, что в детали). */
  function items(name) {
    const list = state.slices[name];
    return Array.isArray(list) ? list : [];
  }

  /** Найти элемент среза по id. */
  function find(name, id) {
    const key = String(id || '');
    if (!key) return null;
    return items(name).find((item) => itemId(item) === key) || null;
  }

  /**
   * Точечная правка элемента (оптимистично).
   * @param {string} name срез
   * @param {string} id
   * @param {object} fields что поменять
   * @param {{quiet?:boolean}} [o]
   */
  function patch(name, id, fields, o = {}) {
    const item = find(name, id);
    if (!item) return null;
    Object.assign(item, fields || {});
    // Строка-группа разрешений: решение относится ко всем её запросам.
    if (name === 'approvals' && Array.isArray(fields && fields._groupIds)) {
      for (const gid of fields._groupIds) {
        const other = find('approvals', gid);
        if (other && other !== item) Object.assign(other, fields);
      }
      delete item._groupIds;
    }
    if (!o.quiet) touch(o.source || 'optimistic');
    return item;
  }

  /** Заменить элемент целиком (откат оптимистичной правки). */
  function replace(name, id, before) {
    const list = items(name);
    const idx = list.findIndex((item) => itemId(item) === String(id || ''));
    if (idx === -1 || !before) return;
    list[idx] = before;
  }

  /** Подписка на изменения. Возвращает функцию отписки. */
  function subscribe(fn) {
    if (typeof fn !== 'function') return () => {};
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  /**
   * Универсальное действие: оптимистичная правка → запрос → сверка с ответом.
   *
   * @param {object} job
   * @param {string} job.kind срез (`approvals` | `questions` | `decisions` | `steps`)
   * @param {string} job.id
   * @param {object} [job.fields] что показать сразу (оптимистично)
   * @param {string} [job.key] ключ «в полёте» (по умолчанию kind:id)
   * @param {()=>Promise<object>} job.request запрос к серверу
   * @param {(res:object)=>void} [job.apply] применить ответ, если в нём нет детали
   * @returns {Promise<object>} ответ сервера
   */
  async function dispatch(job) {
    const kind = String((job && job.kind) || '');
    const id = String((job && job.id) || '');
    const key = String((job && job.key) || '') || kind + ':' + id;
    // Сколько ждём ответа: по умолчанию общий предел действия (store.js).
    const timeoutMs = Number(job && job.timeoutMs) > 0 ? Number(job.timeoutMs) : lockTimeout();
    const before = find(kind, id);
    const saved = before ? Object.assign({}, before) : null;
    if (state.waiting.has(key)) {
      const err = new Error('Действие уже выполняется — подождите ответа');
      err.code = 'busy';
      err.status = 409;
      err.hint = 'Предыдущее нажатие ещё в работе. Если ответа нет дольше '
        + Math.round(timeoutMs / 1000) + ' с, кнопка разблокируется сама.';
      throw err;
    }
    if (job.fields && before) patch(kind, id, job.fields, { quiet: true });
    state.waiting.set(key, true);
    state.action += 1;
    touch('optimistic');
    try {
      // W28-D7: ожидание ограничено. Зависший (не отвечающий) запрос раньше
      // держал ключ и `btn.disabled` вечно: кнопка пункта замолкала навсегда.
      const res = await withTimeout(job.request(), timeoutMs, 'Действие по задаче');
      state.error = null;
      if (res && res.detail && typeof res.detail === 'object') {
        // Сервер вернул деталь целиком — она и есть истина.
        setDetail(res.detail, { source: 'server', force: true });
      } else {
        if (typeof job.apply === 'function') job.apply(res);
        touch('server');
      }
      return res;
    } catch (err) {
      if (saved) replace(kind, id, saved);
      // W27-E2 §1: `409 already_decided` — не ошибка, а устаревший экран. Откатываем
      // оптимистичную правку, тихо сверяемся с сервером и отдаём «уже решено»
      // вместо исключения: наверху из него получалась красная плашка (девять подряд).
      if (isAlreadyDecided(err)) {
        state.error = null;
        touch('conflict');
        try { await sync(); } catch { /* сверка не удалась — опрос страницы догонит */ }
        return { ok: true, already_decided: true, detail: state.detail };
      }
      state.error = err || new Error('action failed');
      touch('rollback');
      throw err;
    } finally {
      state.waiting.delete(key);
    }
  }

  /* ── частные действия: одна форма для всех мест страницы ───────────────── */

  /** Пост в `/actions` текущего запроса. */
  function act(body) {
    if (!state.api || typeof state.api.post !== 'function') {
      const err = new Error('Нет связи с приложением');
      err.code = 'offline';
      throw err;
    }
    return state.api.post('/api/requests/' + encodeURIComponent(state.rid) + '/actions', body);
  }

  /**
   * Решение по запросу разрешения: `once` / `always` / `deny`.
   * Оптимистично ставит итоговое состояние — второе место на странице
   * показывает его сразу, не дожидаясь ответа сервера.
   */
  function decideApproval(a, decision) {
    const id = itemId(a);
    const group = Array.isArray(a && a.approval_ids) ? a.approval_ids : [];
    const next = String(decision) === 'deny' ? 'denied'
      : String(decision) === 'always' ? 'granted_always' : 'granted_once';
    const body = { action: 'approval', approval_id: id, decision: String(decision) };
    if (a && a.group) body.group = String(a.group);
    return dispatch({
      kind: 'approvals',
      id,
      key: 'approval:' + id,
      fields: { state: next, decided_at: Math.floor(Date.now() / 1000), _groupIds: group },
      request: () => act(body),
      apply: (res) => {
        const state2 = String((res && res.approval && res.approval.state) || next);
        patch('approvals', id, Object.assign({}, (res && res.approval) || {}, { state: state2 }),
          { quiet: true });
        for (const gid of (res && Array.isArray(res.applied) ? res.applied : group)) {
          const other = find('approvals', gid);
          if (other) other.state = state2;
        }
      },
    });
  }

  /**
   * Действие по «нельзя штатно» блокировке (W26 §D1):
   *   * `hint_agent` — подсказать участнику обойтись терминалом;
   *   * `trust_profile` — доверить запуску кода профилю (с обязательным сроком);
   *   * `dismiss` — закрыть как неактуальное;
   *   * `ack` — «понятно, скрыть» у жёсткого запрета (только вид, без сервера).
   */
  function approvalAction(a, action, extra = {}) {
    const id = itemId(a);
    const body = Object.assign({ action: String(action), approval_id: id }, extra || {});
    const optimistic = {
      hint_agent: { state: 'hinted', hinted_at: Math.floor(Date.now() / 1000) },
      trust_profile: { state: 'granted_always', trusted_at: Math.floor(Date.now() / 1000),
        trust_ttl: String((extra && extra.ttl) || 'card') },
      dismiss: { state: 'dismissed', decided_at: Math.floor(Date.now() / 1000) },
      ack: { state: 'acked' },
    }[String(action)] || {};
    if (String(action) === 'ack') {
      // «Понятно, скрыть» — только вид: серверу тут решать нечего.
      patch('approvals', id, optimistic, { source: 'local' });
      return Promise.resolve({ ok: true, local: true, action: 'ack' });
    }
    return dispatch({
      kind: 'approvals',
      id,
      key: 'approval-' + String(action) + ':' + id,
      fields: optimistic,
      request: () => act(body),
      apply: (res) => {
        const next = Object.assign({}, optimistic);
        if (res && res.approval && res.approval.state) next.state = String(res.approval.state);
        if (res && res.grant && res.grant.expires_at) next.expires_at = res.grant.expires_at;
        patch('approvals', id, next, { quiet: true });
      },
    });
  }

  /** Ответ на вопрос: `{action:'answer', question_id, text, need_explanation}`. */
  function answerQuestion(q, body) {
    const id = itemId(q);
    const needExplanation = Boolean(body && body.need_explanation);
    return dispatch({
      kind: 'questions',
      id,
      key: 'answer:' + id,
      fields: needExplanation
        ? { answer_state: 'waiting_explanation', answer: String((body && body.text) || '') }
        : { answer_state: 'sent', answered: true, answer: String((body && body.text) || '') },
      request: () => act(Object.assign({ action: 'answer', question_id: id }, body || {})),
      apply: (res) => {
        const q2 = res && res.question;
        if (q2) patch('questions', id, q2, { quiet: true });
      },
    });
  }

  /**
   * W30-C: действия вопроса по контракту §1 — `answer_option` (`{option_id}`),
   * `answer_custom` (`{text}`), `explain_more`, `postpone`.
   *
   * Одно место на всю страницу: колонка «Нужен ваш ответ», нижний лист и
   * «От вас нужно» получают ТО ЖЕ состояние, а не вторую историю. В отличие от
   * ответа текстом, `answer_option` не трогает `answer` до ответа сервера — он
   * главный и вернёт выбранный вариант в вопросе.
   *
   * @param {object} q вопрос
   * @param {'answer_option'|'answer_custom'|'explain_more'|'postpone'} action
   * @param {{option_id?:string, text?:string}} [extra]
   * @returns {Promise<object>}
   */
  function questionAction(q, action, extra = {}) {
    const id = itemId(q);
    const kind = String(action || '');
    if (!id) {
      const err = new Error('Неизвестный вопрос');
      err.code = 'not_found';
      throw err;
    }
    const body = Object.assign({ action: kind, question_id: id }, extra || {});
    const optimistic = kind === 'postpone' ? { answer_state: 'postponed', postponed: true }
      : kind === 'explain_more' ? { answer_state: 'waiting_explanation', needs_explanation: true }
        : kind === 'answer_option' ? { answer_state: 'sent', answered: true, answer_option: String((extra || {}).option_id || '') }
          : { answer_state: 'sent', answered: true, answer: String((extra || {}).text || '') };
    return dispatch({
      kind: 'questions',
      id,
      key: kind + ':' + id,
      fields: optimistic,
      request: () => act(body),
      apply: (res) => {
        const q2 = res && res.question;
        if (q2) patch('questions', id, q2, { quiet: true });
      },
    });
  }

  /** Действие по решению: `accept` / `reject` / `discuss` / `dismiss` / `restore`. */
  function decisionAction(d, body) {
    const id = itemId(d);
    const outcome = String((body && body.outcome) || 'accept');
    const optimistic = outcome === 'restore' ? { open: true, status: 'proposed' }
      : outcome === 'dismiss' ? { open: false, status: 'dismissed' }
        : outcome === 'discuss' ? { open: true, status: 'discuss' }
          : { open: false, status: outcome === 'reject' ? 'rejected' : 'accepted' };
    return dispatch({
      kind: 'decisions',
      id,
      key: 'decision:' + id,
      fields: optimistic,
      request: () => act(Object.assign({ action: 'decision', decision_id: id }, body || {})),
      apply: (res) => {
        const d2 = res && (res.decision || res.item);
        if (d2) patch('decisions', id, d2, { quiet: true });
      },
    });
  }

  /** Действие по шагу: `step_agree` / `step_drop` / `step_move` / `step_comment`. */
  function stepAction(s, body) {
    const id = itemId(s);
    const kind = String((body && body.action) || 'step_agree');
    const optimistic = kind === 'step_drop' ? { state: 'dismissed', state_label: 'убран' }
      : kind === 'step_agree' ? { user_action: 'agree', reaction_pending: true }
        : {};
    return dispatch({
      kind: 'steps',
      id,
      key: kind + ':' + id,
      fields: optimistic,
      request: () => act(Object.assign({ action: kind, step_id: id }, body || {})),
      apply: (res) => {
        const s2 = res && (res.step || res.item);
        if (s2) patch('steps', id, s2, { quiet: true });
      },
    });
  }

  /* ── W27-E2 §1: «Не актуально» и «Вернуть» одним путём для всех видов ───── */

  /** Вид пункта в теле действия: срез → `target.kind` контракта §1. */
  const TARGET_KIND = {
    approvals: 'approval', questions: 'question', decisions: 'decision', steps: 'step',
  };

  /** Что показать сразу, пока сервер не ответил (по виду и действию). */
  const STALE_FIELDS = {
    approvals: { dismiss: { state: 'dismissed' }, restore: { state: 'pending', decided_at: null } },
    questions: {
      dismiss: { state: 'dismissed', dismissed: true },
      restore: { state: 'open', dismissed: false, open: true },
    },
    decisions: {
      dismiss: { open: false, status: 'dismissed' },
      restore: { open: true, status: 'proposed' },
    },
    steps: { dismiss: { state: 'dismissed' }, restore: { state: 'open' } },
  };

  /** Элемент из ответа сервера (у каждого вида своё поле, порядок — от точного к общему). */
  function answeredItem(res, slice) {
    if (!res || typeof res !== 'object') return null;
    const single = { approvals: 'approval', questions: 'question', decisions: 'decision', steps: 'step' };
    return res[single[slice]] || res.item || null;
  }

  /**
   * «Не актуально» / «Вернуть» для вопроса, решения, разрешения, шага.
   *
   * `POST /api/requests/{rid}/actions {action:"dismiss"|"restore", target:{kind,id}}`.
   * Ответ сервера главный; 409 `already_decided` = «уже так» — тихо.
   *
   * @param {'approvals'|'questions'|'decisions'|'steps'} slice
   * @param {string} id
   * @param {'dismiss'|'restore'} action
   * @param {{reason?:string}} [extra]
   */
  function targetAction(slice, id, action, extra = {}) {
    const kind = String(slice || '');
    const kindName = TARGET_KIND[kind] || kind;
    const body = String(action) === 'restore'
      ? restoreBody(kindName, id)
      : dismissBody(kindName, id, extra && extra.reason);
    const optimistic = (STALE_FIELDS[kind] && STALE_FIELDS[kind][String(action)]) || {};
    return dispatch({
      kind,
      id: String(id || ''),
      key: 'stale-' + String(action) + ':' + kind + ':' + String(id || ''),
      fields: Object.assign({}, optimistic),
      request: () => act(body),
      apply: (res) => {
        const item = answeredItem(res, kind);
        if (item) patch(kind, id, item, { quiet: true });
      },
    });
  }

  /**
   * Массово «Закрыть все неактуальные» (W27-E2 §1): неактуальное у завершённых
   * запросов комнаты. Решённое человеком сервер не трогает.
   * @returns {Promise<{closed:{questions:number,decisions:number,approvals:number,steps:number}}>}
   */
  async function dismissStale(scope = 'finished_requests') {
    if (!state.api || typeof state.api.post !== 'function') {
      const err = new Error('Нет связи с приложением');
      err.code = 'offline';
      throw err;
    }
    if (!state.roomId) {
      const err = new Error('Неизвестна комната задачи');
      err.code = 'no_room';
      throw err;
    }
    const res = await state.api.post('/api/rooms/' + encodeURIComponent(state.roomId)
      + '/dismiss_stale', { scope: String(scope || 'finished_requests') });
    try { await sync(); } catch { /* опрос страницы догонит */ }
    return res || { closed: { questions: 0, decisions: 0, approvals: 0, steps: 0 } };
  }

  /** Число «ждёт вас» с сервера (`needs_you_count`) или `null` у старого сервера. */
  function needsYou() {
    return needsYouCount(state.detail);
  }

  /** Перечитать состояние с сервера (если страница умеет). */
  async function sync() {
    if (state.refresh) return state.refresh({ force: true, quiet: true });
    if (!state.api || typeof state.api.get !== 'function') return null;
    const res = await state.api.get('/api/requests/' + encodeURIComponent(state.rid), { etag: true });
    if (res && !res.notModified) setDetail(res, { source: 'sync' });
    return res;
  }

  function destroy() {
    listeners.clear();
    if (STORES.get(state.rid) === api) STORES.delete(state.rid);
  }

  const api = {
    rid: state.rid,
    get,
    adopt,
    setDetail,
    items,
    find,
    patch,
    subscribe,
    dispatch,
    decideApproval,
    approvalAction,
    answerQuestion,
    // W30-C: варианты ответа, свой ответ, «Пояснить ещё», «Отложить».
    questionAction,
    decisionAction,
    stepAction,
    // W27-E2 §1: «Не актуально» / «Вернуть» / массовое закрытие / счётчик сервера.
    targetAction,
    dismissStale,
    needsYou,
    sync,
    destroy,
    get rev() { return state.rev; },
    get roomId() { return state.roomId; },
  };
  if (state.detail) state.signature = slicesSignature(state.slices);
  return api;
}

/**
 * Стор запроса по id — один на страницу (сколько бы мест его ни показывало).
 * Повторный вызов с тем же `rid` возвращает ТОТ ЖЕ объект и догоняет его
 * свежими ссылками (`api`, `refresh`, `roomId`).
 *
 * @param {string} rid
 * @param {object} [init]
 * @returns {object} стор
 */
export function requestStoreFor(rid, init = {}) {
  const key = String(rid || '');
  let store = STORES.get(key);
  if (!store) {
    store = createRequestStore(Object.assign({ rid: key }, init || {}));
    STORES.set(key, store);
  } else {
    store.adopt(init || {});
  }
  return store;
}

/** Забыть все сторы (тесты и выход со страницы запроса). */
export function resetRequestStores() {
  STORES.clear();
}

/**
 * Привязать участок страницы к стору: `paint` вызывается при каждом изменении,
 * пока узел в документе. Узел убрали — подписка снимается сама (без утечек).
 *
 * @param {object} store
 * @param {object} node узел-контейнер
 * @param {Function} paint перерисовать содержимое
 * @returns {Function} отписка
 */
export function bindLive(store, node, paint) {
  if (!store || typeof store.subscribe !== 'function' || typeof paint !== 'function') {
    return () => {};
  }
  let off = null;
  let dead = false;
  const alive = () => !dead && !(node && node.isConnected === false);
  const run = () => {
    if (!alive()) {
      if (off) off();
      off = null;
      return;
    }
    paint();
  };
  off = store.subscribe(run);
  paint();
  return () => {
    dead = true;
    if (off) off();
    off = null;
  };
}

export default {
  REQUEST_CHANGED, SLICE_NAMES, SLICE_FIELDS, createRequestStore, requestStoreFor,
  resetRequestStores, sliceOfDetail, slicesSignature, bindLive, itemId,
};
