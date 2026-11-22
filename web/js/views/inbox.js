/**
 * inbox.js — комната, вкладка «Входящие» (W26, пакет D9).
 *
 * Что видно:
 *  - записи, надиктованные микрофоном или набранные в полосе «Надиктовать»:
 *    текст, время, источник («диктовка» / «вручную»);
 *  - разбор: «В дело», «В шаг», «Вопрос», «Дежурство», «Справка», «Удалить»;
 *  - «Разобрать руководителем» — предложение приходит шаблоном
 *    «Предлагаю оформить так: … [Принять][Изменить][Отклонить]»;
 *  - «В очереди у руководителя» — карточки-задачи, которые ещё не разобраны.
 *
 * Правило «без тупиков»: набор кнопок у записи приходит с сервера
 * (`item.actions`) и всегда непустой — включая удалённые («Вернуть») и
 * разобранные («Вернуть в работу»). Интерфейс только рисует то, что реально
 * меняет состояние, и не придумывает кнопок без действия.
 */

import { h, timeAgo, fmtDateTime } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, emptyState, errorBanner, skeleton, segmented } from '../ui.js';

const FILTERS = [
  { id: 'active', label: 'Новые' },
  { id: 'processed', label: 'Разобранные' },
  { id: 'all', label: 'Все' },
];

/** Действия, которые рисует блок предложения, а не общий ряд кнопок. */
const PROPOSAL_ACTIONS = new Set(['accept', 'edit', 'reject']);

const KIND_TITLES = {
  delo: 'Дело', step: 'Шаг дела', question: 'Вопрос руководителю',
  duty: 'Дежурство', note: 'Справка',
};

function sourceChip(item) {
  const dictation = item.source === 'dictation';
  return h('span', {
    class: 'inbox__chip' + (dictation ? ' inbox__chip--voice' : ''),
    title: dictation ? 'Записано микрофоном (диктовка)' : 'Набрано руками',
  }, icon(dictation ? 'comment' : 'edit', { size: 11 }), item.source_title || (dictation ? 'диктовка' : 'вручную'));
}

function kindChip(kind) {
  if (!kind) return null;
  return h('span', { class: 'inbox__chip inbox__chip--kind' }, KIND_TITLES[kind] || kind);
}

function statusChip(item) {
  const status = String(item.status || 'new');
  if (status === 'new') return null;
  const text = status === 'processing' ? 'у руководителя'
    : status === 'processed' ? 'разобрано' : status === 'deleted' ? 'удалено' : status;
  return h('span', { class: 'inbox__chip inbox__chip--status inbox__chip--' + status }, text);
}

/**
 * Предложение руководителя: текст, кнопки решения, при правке — поле.
 * @param {object} item
 * @param {{act:Function, edit:(item:object)=>void, editing:string, saveEdit:Function, cancelEdit:Function}} api
 */
function proposalBlock(item, api) {
  const proposal = item.proposal;
  if (!proposal || typeof proposal !== 'object') return null;
  const state = String(proposal.state || 'waiting');
  const waiting = state === 'waiting';
  const editing = waiting && api.editing === item.id;

  const head = h('div', { class: 'inbox__proposal-head' },
    icon('comment-draft', { size: 13 }),
    h('span', { class: 'inbox__proposal-title' }, 'Предлагаю оформить так:'),
    h('span', { class: 'inbox__chip inbox__chip--kind' }, proposal.kind_title || KIND_TITLES[proposal.kind] || ''),
    h('span', { class: 'inbox__proposal-by' }, proposal.by ? '— ' + proposal.by : ''),
  );

  let area = null;
  if (editing) {
    area = h('textarea', {
      class: 'inbox__edit-area', rows: '4', maxlength: '4000',
      'aria-label': 'Текст предложения', 'data-inbox-edit': item.id,
    });
    area.value = String(proposal.body || '');
    if (api.editNodes) api.editNodes[item.id] = area;
  }

  const body = editing
    ? h('div', { class: 'inbox__edit' },
      area,
      h('div', { class: 'inbox__edit-actions' },
        h('button', {
          class: 'btn btn--primary', type: 'button', 'data-inbox-save': item.id,
          onclick: () => api.saveEdit(item),
        }, icon('check', { size: 13 }), 'Сохранить предложение'),
        h('button', {
          class: 'btn btn--secondary', type: 'button',
          onclick: () => api.cancelEdit(item),
        }, 'Отмена'),
      ),
    )
    : h('p', { class: 'inbox__proposal-body' }, String(proposal.body || ''));

  const buttons = waiting && !editing
    ? h('div', { class: 'inbox__proposal-actions' },
      h('button', {
        class: 'btn btn--primary', type: 'button', 'data-inbox-act': 'accept', 'data-item': item.id,
        onclick: () => api.act(item, 'accept'),
      }, icon('check', { size: 13 }), 'Принять'),
      h('button', {
        class: 'btn btn--secondary', type: 'button', 'data-inbox-act': 'edit', 'data-item': item.id,
        onclick: () => api.edit(item),
      }, icon('edit', { size: 13 }), 'Изменить'),
      h('button', {
        class: 'btn btn--quiet', type: 'button', 'data-inbox-act': 'reject', 'data-item': item.id,
        onclick: () => api.act(item, 'reject'),
      }, 'Отклонить'),
    )
    : null;

  return h('div', {
    class: 'inbox__proposal inbox__proposal--' + state + (editing ? ' inbox__proposal--editing' : ''),
  }, head, body, buttons);
}

function resolutionLine(item) {
  const res = item.resolution;
  if (!res || typeof res !== 'object') return null;
  const title = res.title ? `«${res.title}»` : '';
  return h('p', { class: 'inbox__resolved' },
    icon('check-all', { size: 12 }),
    `Разобрано: ${res.kind_title || KIND_TITLES[res.kind] || 'запись'} ${title}`.trim(),
    res.kind === 'delo' ? h('span', { class: 'inbox__hint' }, ' Дело-заготовка сохранена во входящих комнаты.') : null,
    res.kind === 'duty' ? h('span', { class: 'inbox__hint' }, ' Черновик дежурства сохранён; запуск — только по вашему «да» в разделе «Дежурства».') : null,
  );
}

/**
 * Одна запись: текст, мета, действия.
 * @param {object} item
 * @param {object} api
 */
export function itemCard(item, api) {
  const meta = h('div', { class: 'inbox__meta' },
    h('span', { class: 'inbox__time', title: fmtDateTime(item.created_at) }, timeAgo(item.created_at)),
    sourceChip(item),
    kindChip(item.kind),
    statusChip(item),
  );

  const actions = (Array.isArray(item.actions) ? item.actions : [])
    .filter((a) => a && a.id && !PROPOSAL_ACTIONS.has(a.id) && !(item.proposal && item.proposal.state === 'waiting' && a.id === 'propose'));
  const row = actions.length
    ? h('div', { class: 'inbox__actions' }, ...actions.map((a) => h('button', {
      class: 'btn btn--quiet inbox__act' + (a.id === 'delete' ? ' inbox__act--danger' : ''),
      type: 'button',
      'data-inbox-act': a.id,
      'data-item': item.id,
      title: a.id === 'propose' ? 'Отдать разбор руководителю: придёт предложение с кнопками' : a.label,
      onclick: () => api.act(item, a.id),
    }, a.id === 'delete' ? icon('trash', { size: 12 }) : null, a.label)))
    : null;

  return h('div', {
    class: 'card inbox__item inbox__item--' + String(item.status || 'new'),
    'data-inbox-item': item.id,
  },
  meta,
  h('p', { class: 'inbox__text' }, String(item.text || '')),
  item.note ? h('p', { class: 'inbox__note' }, item.note) : null,
  proposalBlock(item, api),
  resolutionLine(item),
  row,
  );
}

function queueBlock(queue) {
  if (!Array.isArray(queue) || !queue.length) return null;
  return h('details', { class: 'inbox__queue' },
    h('summary', { class: 'inbox__queue-head' },
      icon('hubot', { size: 13 }),
      `В очереди у руководителя: ${queue.length}`),
    h('div', { class: 'inbox__queue-list' },
      ...queue.map((card) => h('div', { class: 'inbox__queue-card' },
        h('span', { class: 'inbox__queue-time' }, timeAgo(card.at)),
        h('span', { class: 'inbox__queue-text' }, String(card.text || '').split('\n')[0])),
      )),
  );
}

/**
 * Экран «Входящие» комнаты.
 * @param {HTMLElement} el
 * @param {{route:object, api:object, toast?:Function, navigate?:Function, room?:object}} ctx
 */
export async function mount(el, ctx = {}) {
  ensureCss('css/view-inbox.css');

  const api = ctx.api || { get() {}, post() {} };
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const navigate = typeof ctx.navigate === 'function'
    ? ctx.navigate
    : (hash) => { location.hash = hash; };
  const roomId = String((ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || '');

  if (!roomId) {
    el.appendChild(h('div', { class: 'view__inner' },
      emptyState({ icon: 'error', title: 'Комната не выбрана', text: 'Откройте комнату в левой колонке.' })));
    return { unmount() {} };
  }

  const base = `/api/rooms/${encodeURIComponent(roomId)}/inbox`;
  const state = {
    filter: 'active', data: null, loading: true, error: null, editing: '', busy: false, alive: true,
  };

  const root = h('div', { class: 'view__inner inbox' });
  el.appendChild(root);

  function replaceItem(item) {
    const list = state.data && Array.isArray(state.data.items) ? state.data.items : [];
    const index = list.findIndex((x) => x && x.id === item.id);
    if (index !== -1) list[index] = item;
    else list.unshift(item);
  }

  async function load() {
    state.loading = true;
    state.error = null;
    draw();
    try {
      const data = await api.get(`${base}?status=${encodeURIComponent(state.filter)}`);
      if (!state.alive) return;
      state.data = data || { items: [], counts: {} };
      state.loading = false;
    } catch (err) {
      if (!state.alive) return;
      state.loading = false;
      state.error = err;
    }
    draw();
  }

  async function act(item, action, payload) {
    if (!item || !action || state.busy) return false;
    state.busy = true;
    try {
      const data = await api.post(`${base}/${encodeURIComponent(item.id)}/actions`,
        Object.assign({ action }, payload || {}));
      if (!state.alive) return true;
      if (data && data.item) replaceItem(data.item);
      if (data && data.counts && state.data) state.data.counts = data.counts;
      state.editing = '';
      if (action === 'accept') toast('Принято: запись разобрана', 'success');
      else if (action === 'reject') toast('Предложение отклонено — запись вернулась в работу', 'info');
      else if (action === 'delete') toast('Запись удалена — можно вернуть', 'info');
      draw();
      // Состояние могло измениться по фильтру: перечитываем список без мигания.
      if (action === 'accept' || action === 'delete' || action === 'restore') load();
      return true;
    } catch (err) {
      const message = String((err && err.message) || 'не удалось разобрать');
      toast('Не получилось: ' + message, 'error');
      draw();
      return false;
    } finally {
      state.busy = false;
    }
  }

  const editNodes = {};

  function saveEdit(item) {
    const node = editNodes[item.id]
      || (root.querySelector ? root.querySelector(`[data-inbox-edit="${item.id}"]`) : null);
    const text = node && typeof node.value === 'string' ? node.value : '';
    if (!String(text).trim()) { toast('Пустое предложение сохранять нельзя', 'error'); return; }
    act(item, 'edit', { text: String(text) });
  }

  function cancelEdit() {
    state.editing = '';
    draw();
  }

  function header(counts) {
    const c = counts || {};
    return h('div', { class: 'inbox__head' },
      h('div', { class: 'inbox__title-row' },
        h('h1', { class: 'inbox__title' }, icon('inbox', { size: 18 }), 'Входящие комнаты'),
        h('div', { class: 'inbox__title-actions' },
          h('button', {
            class: 'btn btn--secondary', type: 'button', 'data-inbox-dictate': 'true',
            title: 'Надиктовать новую запись (N)',
            onclick: () => {
              navigate(`#/room/${encodeURIComponent(roomId)}/composer`);
              const focus = () => {
                const field = typeof document !== 'undefined' && document.getElementById
                  ? document.getElementById('quick-capture-text') : null;
                if (field && typeof field.focus === 'function') field.focus();
              };
              if (typeof setTimeout === 'function') setTimeout(focus, 120);
            },
          }, icon('comment', { size: 13 }), 'Надиктовать'),
          h('button', {
            class: 'btn btn--ghost', type: 'button', 'data-inbox-duty': 'true',
            title: 'Создать дежурство: задание по расписанию, результат придёт сюда',
            onclick: () => navigate(`#/room/${encodeURIComponent(roomId)}/duties`),
          }, icon('clock', { size: 13 }), 'Создать дежурство'),
        ),
      ),
      h('p', { class: 'inbox__subtitle' },
        'Входящие комнаты — один ящик для того, что приходит само: результаты дежурств, '
        + 'ваши надиктовки и входящие материалы. Разбор — ваш: дело, шаг, вопрос, дежурство, '
        + 'справка. «Разобрать руководителем» приносит готовое предложение.'),
      h('div', { class: 'inbox__filters' },
        segmented({
          options: FILTERS.map((f) => ({
            value: f.id,
            label: f.label + ' · ' + String(f.id === 'active' ? Number(c.active || 0)
              : f.id === 'processed' ? Number(c.processed || 0) : Number(c.total || 0)),
          })),
          value: state.filter,
          onchange: (id) => { if (id !== state.filter) { state.filter = id; load(); } },
        }),
        c.proposals ? h('span', { class: 'inbox__chip inbox__chip--status inbox__chip--processing' },
          `предложений ждут решения: ${c.proposals}`) : null,
      ),
    );
  }

  function body() {
    if (state.loading) return skeleton(4);
    if (state.error) return errorBanner(state.error, () => load());
    const data = state.data || {};
    const items = Array.isArray(data.items) ? data.items : [];
    const parts = [];
    if (data.warning) parts.push(h('div', { class: 'inbox__warning' }, icon('warning', { size: 13 }), data.warning));
    if (!items.length) {
      parts.push(emptyState({
        icon: 'inbox',
        title: state.filter === 'processed' ? 'Разобранных записей пока нет'
          : state.filter === 'all' ? 'Входящие пусты' : 'Здесь появятся входящие комнаты',
        text: 'Сюда приходят результаты дежурств, надиктованное вами и входящие материалы. '
          + 'Пока ничего не пришло: надиктуйте мысль (кнопка ниже) или создайте дежурство — '
          + 'оно само сообщит, когда что-то изменится.',
        action: {
          label: 'Надиктовать',
          onclick: () => {
            navigate(`#/room/${encodeURIComponent(roomId)}/composer`);
            const focus = () => {
              const field = typeof document !== 'undefined' && document.getElementById
                ? document.getElementById('quick-capture-text') : null;
              if (field && typeof field.focus === 'function') field.focus();
            };
            if (typeof setTimeout === 'function') setTimeout(focus, 120);
          },
        },
      }));
    } else {
      parts.push(h('div', { class: 'inbox__list' },
        ...items.map((item) => itemCard(item, {
          act, edit: (x) => { state.editing = x.id; draw(); },
          saveEdit, cancelEdit, editing: state.editing, editNodes,
        }))));
    }
    const queue = queueBlock(data.queue);
    if (queue) parts.push(queue);
    return h('div', { class: 'inbox__body' }, ...parts);
  }

  function draw() {
    const data = state.data || {};
    root.replaceChildren(header(data.counts || {}), body());
  }

  draw();
  await load();
  return {
    unmount() { state.alive = false; },
    reload: load,
  };
}

export default { mount, itemCard };
