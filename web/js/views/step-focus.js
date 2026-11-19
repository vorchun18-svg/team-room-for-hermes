/**
 * step-focus.js — переход к шагу и действия над ним (W27-E3).
 *
 * Два дела, одно место:
 *
 * 1. **Переход.** Строка «Просрочено»/«Ближайшие» на «Сегодня» ведёт на страницу
 *    запроса с якорем `?step=ns_…`. Здесь этот якорь читается и превращается в
 *    видимое действие: блок «Шаги» раскрывается, нужный шаг подсвечивается и
 *    оказывается в центре экрана. Без этого ссылка «в запрос» была бы переходом
 *    «куда-то туда», а не к делу.
 *
 * 2. **Действия.** `Готово`, `Перенести срок`, `Не актуально`, `Поручить` — через
 *    УЖЕ существующий механизм правки шагов, без второго источника:
 *
 *    • «Готово» → `POST /api/rooms/{room}/backlog/{item_id} {status:"done"}` —
 *      тот же путь, которым шаг отмечается сделанным в «Делах» и в бэклоге
 *      комнаты (`ingest.set_backlog_status`, журнал `step_done`);
 *    • «Перенести срок» / «Поручить» → `POST /api/requests/{rid}/actions`
 *      `{action:"step_move", due|owner}` — механизм шага (`_step_action`), он же
 *      у окна шага (`step-dialog.js`), и руководитель получает ответ по шагу;
 *    • «Не актуально» → `{action:"step_drop"}` (шаг снимается и в бэклоге).
 *
 *    Если шаг живёт только в бэклоге (попал туда не из плана запроса), сервер
 *    отвечает `404 step not found` на `step_*` — тогда срок и «неактуально»
 *    делаются тем же бэклог-эндпоинтом. Ошибку человеку не показываем: он не
 *    обязан знать, из какого файла шаг.
 *
 * Стили — `web/css/view-today.css` (строки) и `web/css/view-steps.css` (шаг).
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { localStepId } from './today-model.js';

/** Класс подсветки шага, к которому пришли по ссылке. */
export const FOCUS_CLASS = 'is-step-focus';

/** Сколько держится подсветка (мс): дальше она мешает читать. */
export const FOCUS_MS = 4000;

/**
 * Id шага из маршрута: `#/room/r/request/t_1?step=ns_2` → `ns_2`.
 * Принимает и `t_1:ns_2` (id пункта бэклога), и пустое значение.
 * @param {{query?:object, params?:object}|null} route
 * @returns {string}
 */
export function stepFromRoute(route) {
  const query = (route && route.query) || {};
  const params = (route && route.params) || {};
  const raw = String(query.step || params.step || '').trim();
  return raw ? localStepId(raw) : '';
}

/** Ближайший раскрываемый контейнер (свёрнутый блок шагов) — или null. */
function openAncestors(el) {
  let node = el;
  while (node && node !== node.ownerDocument) {
    if (node.tagName && String(node.tagName).toLowerCase() === 'details') node.open = true;
    node = node.parentNode;
  }
}

/**
 * Раскрыть и подсветить шаг на странице запроса.
 *
 * @param {Element} root где искать (обычно контейнер вида)
 * @param {string} stepId `ns_2` или `t_…:ns_2`
 * @param {{scroll?:boolean, pulse?:boolean, duration?:number}} [opts]
 * @returns {boolean} нашли ли шаг (false — его нет в разметке: покажем подсказку)
 */
export function focusStep(root, stepId, opts = {}) {
  const id = localStepId(stepId);
  if (!root || !id || typeof root.querySelectorAll !== 'function') return false;
  let nodes = [];
  try {
    nodes = Array.from(root.querySelectorAll('[data-step-id]'));
  } catch { return false; }
  const target = nodes.find((el) => String(el.getAttribute('data-step-id')) === id);
  if (!target) return false;
  const row = target.closest ? (target.closest('.req-step') || target) : target;
  openAncestors(row);
  try { row.classList.add(FOCUS_CLASS); } catch { /* мини-DOM */ }
  if (opts.scroll !== false && typeof row.scrollIntoView === 'function') {
    try { row.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch {
      try { row.scrollIntoView(); } catch { /* не критично */ }
    }
  }
  if (typeof row.focus === 'function') {
    try {
      if (!row.hasAttribute('tabindex')) row.setAttribute('tabindex', '-1');
      row.focus({ preventScroll: true });
    } catch { /* фокус не критичен */ }
  }
  const duration = Number(opts.duration) || FOCUS_MS;
  if (opts.pulse !== false && duration > 0) {
    setTimeout(() => { try { row.classList.remove(FOCUS_CLASS); } catch { /* уже нет */ } }, duration);
  }
  return true;
}

/**
 * Дождаться появления шага и подсветить его (страница запроса рисуется после
 * загрузки деталей, поэтому одной попытки мало).
 *
 * @param {Element} root
 * @param {string} stepId
 * @param {{tries?:number, delay?:number, onMissing?:Function}} [opts]
 * @returns {Function} отмена ожидания
 */
export function focusStepWhenReady(root, stepId, opts = {}) {
  const tries = Math.max(1, Number(opts.tries) || 25);
  const delay = Math.max(50, Number(opts.delay) || 200);
  let count = 0;
  let stopped = false;
  const tick = () => {
    if (stopped) return;
    if (focusStep(root, stepId, opts)) return;
    count += 1;
    if (count >= tries) {
      if (typeof opts.onMissing === 'function') {
        try { opts.onMissing(stepId); } catch { /* подсказка не критична */ }
      }
      return;
    }
    setTimeout(tick, delay);
  };
  setTimeout(tick, 0);
  return () => { stopped = true; };
}

/* ─────────────────────────────────────────────────────────── действия */

/** Ответ сервера об ошибке «шага нет в плане» (404) — повод для бэклога. */
function isNotFound(err) {
  return Number(err && err.status) === 404;
}

/** `t_x:ns_1` → `t_x` (запрос, в котором живёт шаг). */
function requestOf(itemId, requestId) {
  if (requestId) return String(requestId);
  const text = String(itemId || '');
  return text.indexOf(':') >= 0 ? text.split(':', 1)[0] : '';
}

/**
 * Выполнить действие над шагом существующим механизмом.
 *
 * @param {{api:object, action:'done'|'postpone'|'dismiss'|'assign',
 *          itemId:string, requestId?:string, room?:string,
 *          due?:string, owner?:string}} opts
 * @returns {Promise<{ok:boolean, action:string, via:string, step:object|null, message:string}>}
 */
export async function applyStepAction(opts) {
  const o = opts || {};
  const api = o.api;
  if (!api || typeof api.post !== 'function') {
    return { ok: false, action: String(o.action || ''), via: '', step: null,
      message: 'Нет связи с приложением' };
  }
  const action = String(o.action || '');
  const itemId = String(o.itemId || '');
  const room = String(o.room || '');
  const rid = requestOf(itemId, o.requestId);
  const stepId = localStepId(itemId);

  const backlog = (body) => {
    if (!room || !itemId) throw new Error('Строка без комнаты или шага — действие невозможно');
    return api.post('/api/rooms/' + encodeURIComponent(room) + '/backlog/'
      + encodeURIComponent(itemId), body);
  };
  const actionCall = (body) => {
    if (!rid) throw new Error('Шаг не привязан к запросу');
    return api.post('/api/requests/' + encodeURIComponent(rid) + '/actions',
      Object.assign({ step_id: stepId }, body));
  };
  const done = (res, via) => ({
    ok: true, action, via, step: (res && res.step) || null,
    message: String((res && (res.message || res.reaction_label)) || ''),
  });

  if (action === 'done') {
    // «Готово» — статус шага в бэклоге комнаты: он же виден на странице запроса
    // и он же убирает строку из «Просрочено» (там только open/asked).
    const res = await backlog({ status: 'done' });
    return done(res, 'backlog');
  }
  if (action === 'dismiss') {
    if (rid) {
      try { return done(await actionCall({ action: 'step_drop' }), 'step'); }
      catch (err) { if (!isNotFound(err)) throw err; }
    }
    return done(await backlog({ status: 'dismissed' }), 'backlog');
  }
  if (action === 'postpone') {
    const due = String(o.due || '');
    if (rid) {
      try { return done(await actionCall({ action: 'step_move', due: due || null }), 'step'); }
      catch (err) { if (!isNotFound(err)) throw err; }
    }
    return done(await backlog({ status: 'open', due: due || null }), 'backlog');
  }
  if (action === 'assign') {
    const owner = String(o.owner || '');
    if (!owner) throw new Error('Не выбран исполнитель');
    return done(await actionCall({ action: 'step_move', owner }), 'step');
  }
  throw new Error('Неизвестное действие: ' + action);
}

/* ─────────────────────────────────────────────── режим «по очереди» */

/**
 * Режим «Разобрать по очереди»: одна карточка шага, действия, «Далее».
 *
 * Работает поверх тех же строк, что и списки «Просрочено»/«Ближайшие», и тех же
 * действий (`applyStepAction`). После действия карточка уходит из очереди: то,
 * что закрыто, второй раз не спрашиваем.
 *
 * @param {{rows:Array<object>, ctx:{api:object, toast?:Function},
 *          root?:Element, onDone?:Function, onClose?:Function}} opts
 * @returns {{close:Function, next:Function, remaining:Function}|null}
 */
export function openStepQueue(opts) {
  const o = opts || {};
  const ctx = o.ctx || {};
  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  if (!api || typeof api.post !== 'function') return null;
  ensureCss('css/view-today.css');
  const queue = (Array.isArray(o.rows) ? o.rows : []).slice();
  if (!queue.length) return null;
  const target = o.root || (globalThis.document && globalThis.document.body);
  if (!target) return null;
  let index = 0;
  let busy = false;

  const overlay = h('div', { class: 'dialog-backdrop stepq-backdrop', 'data-step-queue': '' });
  const dialog = h('div', {
    class: 'dialog stepq', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'stepq-title',
  });
  const head = h('div', { class: 'dialog__head' },
    h('h2', { class: 'dialog__title', id: 'stepq-title', tabindex: '-1' }, 'Разобрать по очереди'),
    h('span', { class: 'stepq__counter tiny muted tnum', 'data-stepq-counter': '' }, ''),
    h('button', {
      class: 'btn btn--ghost btn--sm', type: 'button', 'aria-label': 'Закрыть разбор',
      onclick: () => close(),
    }, icon('close', { size: 13 })));
  const body = h('div', { class: 'dialog__body stepq__body' });
  const foot = h('div', { class: 'dialog__foot stepq__foot' });
  dialog.append(head, body, foot);
  overlay.appendChild(dialog);
  overlay.addEventListener('keydown', (ev) => {
    if (ev && ev.key === 'Escape') { if (ev.preventDefault) ev.preventDefault(); close(); }
  });
  overlay.addEventListener('mousedown', (ev) => {
    if (ev && ev.target === overlay) close();
  });

  function close() {
    const parent = overlay.parentNode;
    if (!parent) return;
    if (typeof parent.removeChild === 'function') parent.removeChild(overlay);
    if (typeof o.onClose === 'function') { try { o.onClose(); } catch { /* уже закрыто */ } }
  }

  function current() {
    return queue[index] || null;
  }

  function paint() {
    body.replaceChildren();
    foot.replaceChildren();
    const row = current();
    const total = queue.length;
    head.querySelector('[data-stepq-counter]').textContent = total
      ? (index + 1) + ' из ' + total : '';
    if (!row) {
      body.appendChild(h('div', { class: 'stepq__done' },
        h('p', { class: 'stepq__done-title' }, 'Всё разобрали'),
        h('p', { class: 'small muted' }, 'Список пуст — на «Сегодня» больше ничего не ждёт решения.')));
      foot.appendChild(h('button', {
        class: 'btn btn--primary btn--sm', type: 'button', onclick: () => close(),
      }, 'Закрыть'));
      return;
    }
    const meta = h('div', { class: 'stepq__meta small muted' },
      h('span', { class: 'stepq__room', title: row.roomTitle }, row.roomTitle),
      row.requestTitle ? h('span', { class: 'stepq__dot' }, '·') : null,
      row.requestTitle ? h('a', { class: 'stepq__link', href: row.href }, row.requestTitle) : null,
      row.owner ? h('span', { class: 'stepq__dot' }, '·') : null,
      row.owner ? h('span', {}, 'владелец: ' + row.owner) : null,
      row.dueRaw ? h('span', { class: 'stepq__dot' }, '·') : null,
      row.dueRaw ? h('span', { class: 'tnum' }, 'срок ' + row.due) : null);
    const card = h('div', { class: ['stepq__card', 'stepq__card--' + (row.tone || 'warn')] },
      h('p', { class: 'stepq__text' }, row.text),
      h('p', { class: ['stepq__when', 'tiny', 'tnum'] }, row.when),
      meta);
    body.appendChild(card);
    body.appendChild(h('div', { class: 'stepq__acts', 'data-stepq-acts': '' }));

    const acts = body.querySelector('[data-stepq-acts]');
    for (const [kind, label, tone] of [
      ['done', 'Готово', 'primary'],
      ['postpone', 'Перенести срок…', 'secondary'],
      ['dismiss', 'Не актуально', 'ghost'],
      ['assign', 'Поручить…', 'ghost'],
    ]) {
      acts.appendChild(h('button', {
        class: 'btn btn--' + tone + ' btn--sm stepq__act',
        type: 'button', 'data-stepq-action': kind,
        onclick: () => start(kind),
      }, label));
    }
    acts.appendChild(h('a', { class: 'btn btn--ghost btn--sm stepq__open', href: row.href },
      'Открыть запрос', icon('arrow-right', { size: 12 })));

    foot.appendChild(h('button', {
      class: 'btn btn--ghost btn--sm', type: 'button', 'data-stepq-skip': '',
      title: 'Оставить как есть и перейти к следующему',
      onclick: () => { index += 1; paint(); },
    }, 'Далее'));
    foot.appendChild(h('span', { class: 'small muted stepq__hint' },
      'Действие сразу убирает пункт из «Сегодня»'));
  }

  /** Форма переноса срока: +1 день, пятница, своя дата. */
  function postponeForm(row, host) {
    import('./today-model.js').then((mod) => {
      const options = mod.postponeOptions(row.dueRaw);
      const box = h('div', { class: 'stepq__form', 'data-stepq-form': 'postpone' });
      for (const option of options) {
        box.appendChild(h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button', 'data-due': option.due,
          onclick: () => send(row, 'postpone', { due: option.due }),
        }, option.label));
      }
      const input = h('input', { class: 'input input--sm', type: 'date', 'aria-label': 'Новый срок' });
      box.appendChild(input);
      box.appendChild(h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button', 'data-due': 'custom',
        onclick: () => {
          const value = String(input.value || '');
          if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) { toast('Выберите дату', 'warn'); return; }
          send(row, 'postpone', { due: value });
        },
      }, 'В эту дату'));
      host.replaceChildren(box);
    }).catch(() => toast('Не удалось открыть перенос срока', 'warn'));
  }

  /** Список исполнителей: участники комнаты + «Михаил». */
  function assignForm(row, host) {
    const box = h('div', { class: 'stepq__form', 'data-stepq-form': 'assign' });
    host.replaceChildren(box);
    box.appendChild(h('span', { class: 'small muted' }, 'Загружаем исполнителей…'));
    api.get('/api/rooms/' + encodeURIComponent(row.room)).then((room) => {
      const members = (room && Array.isArray(room.members) ? room.members : [])
        .map((m) => String((m && m.profile) || '')).filter(Boolean);
      const names = Array.from(new Set(['mikhail', ...members])).filter((n) => n !== row.owner);
      box.replaceChildren();
      if (!names.length) {
        box.appendChild(h('span', { class: 'small muted' }, 'Некому поручить: в комнате нет участников'));
        return;
      }
      for (const name of names) {
        box.appendChild(h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button', 'data-owner': name,
          onclick: () => send(row, 'assign', { owner: name }),
        }, name === 'mikhail' ? 'Михаил' : name));
      }
    }).catch(() => {
      box.replaceChildren(h('span', { class: 'small muted' }, 'Список участников не загрузился'));
    });
  }

  function start(kind) {
    if (busy) return;
    const row = current();
    if (!row) return;
    const host = body.querySelector('[data-stepq-acts]');
    if (kind === 'postpone') { postponeForm(row, host); return; }
    if (kind === 'assign') { assignForm(row, host); return; }
    send(row, kind, {});
  }

  const TOASTS = {
    done: 'Шаг отмечен сделанным — ушёл из «Просрочено»',
    postpone: 'Срок перенесён',
    dismiss: 'Шаг снят как неактуальный',
    assign: 'Шаг поручён — руководитель увидит ответ',
  };

  async function send(row, kind, extra) {
    if (busy) return;
    busy = true;
    try {
      const res = await applyStepAction(Object.assign({
        api, action: kind, itemId: row.itemId, requestId: row.requestId, room: row.room,
      }, extra || {}));
      if (!res.ok) throw new Error(res.message || 'Не получилось');
      toast(TOASTS[kind] || 'Готово', kind === 'dismiss' ? 'warn' : 'ok');
      // Пункт ушёл из списка — из очереди тоже.
      queue.splice(index, 1);
      if (index >= queue.length) index = Math.max(0, queue.length - 1);
      if (typeof o.onDone === 'function') {
        try { o.onDone(row, res); } catch { /* перерисовка экрана не критична */ }
      }
      paint();
    } catch (err) {
      toast((err && err.message) || 'Не удалось изменить шаг', 'danger');
    } finally {
      busy = false;
    }
  }

  paint();
  if (typeof target.appendChild === 'function') target.appendChild(overlay);
  const first = dialog.querySelector('button');
  if (first && typeof first.focus === 'function') { try { first.focus(); } catch { /* ок */ } }

  return {
    close,
    next: () => { index += 1; paint(); },
    remaining: () => queue.length,
  };
}

export default {
  FOCUS_CLASS, FOCUS_MS, stepFromRoute, focusStep, focusStepWhenReady,
  applyStepAction, openStepQueue,
};
