/**
 * phases-editor.js — этапы проекта: плашка предложения и редактор (W20 §1.7, F-2).
 *
 * Один модуль на три места, чтобы не было «трёх разных редакторов»:
 *   • карта проекта (`roadmap.js`) — плашка «Руководитель предлагает этапы» и
 *     кнопки «Изменить этапы» / «Предложить этапы»;
 *   • вкладка «От вас нужно» (`backlog.js`) — строка предложения с теми же решениями;
 *   • настройки комнаты, вкладка «Проект» (`room-dialog.js`) — тот же редактор
 *     встроенным блоком (правки сохраняются кнопкой «Сохранить» диалога).
 *
 * Данные — только API (`/api/rooms/{id}` и `…/phases/*`), этапы — свойство
 * КОМНАТЫ: `PATCH /api/rooms/{id} {phases}`. После сохранения карта читает
 * `phases` комнаты, поэтому правка видна сразу, без ожидания ответа руководителя.
 *
 * Тексты — по-русски и без техники: ни id, ни «proposal», ни кодов ошибок.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, iconButton, buildDialog, confirmDialog } from '../ui.js';
import { countNoun } from '../text-ru.js';

/** Сколько этапов можно держать в комнате (то же число проверяет сервер). */
export const MAX_PHASES = 24;
/** Длина названия этапа: поле и сервер одинаково. */
export const MAX_TITLE = 80;

const CSS_HREF = 'css/phases-editor.css';

/** Подписи решений: одни и те же на плашке, в «От вас нужно» и в подсказках. */
export const DECISION_LABELS = {
  accept: 'Принять все',
  accept_selected: 'Принять выбранные',
  reject: 'Отклонить',
};

export const PROPOSAL_STATES = ['proposed', 'accepted', 'rejected'];

const PHASE_FORMS = ['этап', 'этапа', 'этапов'];

const enc = (s) => encodeURIComponent(String(s === null || s === undefined ? '' : s));

/** Добавить узлы по одному: `append` есть не во всех мини-DOM тестов. */
function mountAll(parent, ...nodes) {
  for (const node of nodes) if (node) parent.appendChild(node);
  return parent;
}

/* ─────────────────────────────────────────────────── чистые помощники ── */

/** Название → id этапа. Переименование сохраняет id: привязка данных не рвётся. */
export function slugId(title) {
  return String(title || '')
    .toLowerCase()
    .replace(/[^a-zа-яё0-9_-]+/gi, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48) || 'etap';
}

/**
 * Уникальный id этапа для нового/переименованного названия.
 * @param {string} title
 * @param {Set<string>|string[]} [taken] уже занятые id
 * @returns {string}
 */
export function phaseIdFor(title, taken) {
  const used = taken instanceof Set ? taken : new Set(Array.isArray(taken) ? taken.map(String) : []);
  const base = slugId(title);
  let candidate = base;
  let n = 2;
  while (used.has(candidate)) { candidate = base + '-' + n; n += 1; }
  return candidate;
}

/**
 * Список этапов из любых данных: только пары `{id, title}`, без мусора.
 * Пустые названия отбрасываются (это уже не этап), id делается уникальным.
 * @param {*} value
 * @returns {{id: string, title: string}[]}
 */
export function normPhases(value) {
  const out = [];
  const taken = new Set();
  for (const raw of Array.isArray(value) ? value : []) {
    if (!raw || typeof raw !== 'object') continue;
    const title = String(raw.title || '').trim();
    if (!title) continue;
    const wanted = String(raw.id || '').trim();
    const id = wanted && !taken.has(wanted) ? wanted : phaseIdFor(title, taken);
    taken.add(id);
    out.push({ id, title: title.slice(0, MAX_TITLE) });
    if (out.length >= MAX_PHASES) break;
  }
  return out;
}

/**
 * Строки редактора: пустые названия нужны, пока человек печатает, поэтому
 * `normPhases` здесь не годится.
 * @param {*} value
 * @returns {{id: string, title: string}[]}
 */
function editorRows(value) {
  const rows = [];
  for (const raw of Array.isArray(value) ? value : []) {
    if (!raw || typeof raw !== 'object') continue;
    const title = String(raw.title === null || raw.title === undefined ? '' : raw.title);
    rows.push({ id: String(raw.id || ''), title });
    if (rows.length >= MAX_PHASES) break;
  }
  return rows;
}

/** Тело `PATCH`: обрезанные названия, без пустых, id уникальные. */
export function phasesPayload(list) {
  return normPhases(Array.isArray(list) ? list : []);
}

/** Перестановка: новый массив, границы не рвутся (кнопки «выше/ниже»). */
export function movePhase(list, index, delta) {
  const arr = Array.isArray(list) ? list.slice() : [];
  const from = Number(index);
  const to = from + Number(delta);
  if (!Number.isInteger(from) || from < 0 || from >= arr.length) return arr;
  if (!Number.isInteger(to) || to < 0 || to >= arr.length) return arr;
  const item = arr.splice(from, 1)[0];
  arr.splice(to, 0, item);
  return arr;
}

/** «12 из 24» — счётчик у списка. */
export function phaseCountLabel(n) {
  return String(Number(n) || 0) + ' из ' + MAX_PHASES;
}

/** «3 этапа» — подпись числа этапов. */
export function phasesCountText(n) {
  return countNoun(Number(n) || 0, PHASE_FORMS);
}

/**
 * Приведение предложения к безопасному виду. Терпимо к мусору: интерфейс не
 * падает из-за одного плохого поля.
 * @param {*} proposal
 * @returns {{state: string, items: {id: string, title: string, description: string}[], at: number}|null}
 */
export function proposalOf(proposal) {
  if (!proposal || typeof proposal !== 'object') return null;
  const state = String(proposal.state || '');
  if (!PROPOSAL_STATES.includes(state)) return null;
  const items = normPhases(proposal.items || []);
  if (!items.length) return null;
  const descriptions = new Map();
  for (const raw of Array.isArray(proposal.items) ? proposal.items : []) {
    if (!raw || typeof raw !== 'object') continue;
    const title = String(raw.title || '').trim().slice(0, MAX_TITLE);
    if (!title) continue;
    const desc = String(raw.description || '').trim();
    if (desc && !descriptions.has(title)) descriptions.set(title, desc);
  }
  return {
    state,
    at: Number(proposal.at) || 0,
    items: items.map((item) => ({
      id: item.id,
      title: item.title,
      description: descriptions.get(item.title) || '',
    })),
  };
}

/** Ждёт ли предложение решения человека. */
export function proposalPending(proposal) {
  const p = proposalOf(proposal);
  return Boolean(p && p.state === 'proposed');
}

/** Заголовок плашки: одно и то же в карте, «От вас нужно» и подсказке. */
export function proposalHeadline(proposal) {
  const p = proposalOf(proposal);
  if (!p) return '';
  return 'Руководитель предлагает этапы';
}

/** Пояснение под заголовком: что именно произойдёт с вашими этапами. */
export function proposalIntro(proposal) {
  const p = proposalOf(proposal);
  if (!p) return '';
  const n = p.items.length;
  return 'Принятые этапы допишутся в конец списка — ' + phasesCountText(n)
    + '. Ваши этапы, их порядок и названия не изменятся.';
}

/** Что уходит в `POST /phases/proposal`: без мусора и пустых решений. */
export function decidePayload(action, ids) {
  const act = String(action || '');
  if (!Object.prototype.hasOwnProperty.call(DECISION_LABELS, act)) {
    throw new Error('Неизвестное решение по этапам: ' + act);
  }
  if (act !== 'accept_selected') return { action: act };
  const list = (Array.isArray(ids) ? ids : []).map((id) => String(id || '').trim()).filter(Boolean);
  if (!list.length) throw new Error('Не выбран ни один этап');
  return { action: act, ids: Array.from(new Set(list)) };
}

/** Подпись после решения: из ответа сервера, иначе своя по действию. */
export function decisionToast(payload) {
  const text = payload && typeof payload.text === 'string' ? payload.text.trim() : '';
  if (text) return text;
  const action = payload && payload.action;
  if (action === 'reject') return 'Предложение этапов отклонено';
  return 'Этапы сохранены';
}

/** Причина отказа словами (409/400 от сервера — без кодов в интерфейсе). */
export function proposalErrorText(err) {
  const reason = String(
    (err && err.details && err.details.error && err.details.error.detail
      && err.details.error.detail.reason)
    || (err && err.reason) || '',
  );
  if (reason === 'proposal_pending') {
    return 'Руководитель уже предложил этапы — сначала примите их или отклоните.';
  }
  if (reason === 'no_proposal') {
    return 'Руководитель ещё не присылал этапы — принимать нечего.';
  }
  if (reason === 'already_decided') {
    return 'Решение по этому предложению уже принято.';
  }
  if (reason === 'proposal_none' || reason === 'empty') {
    return 'Предложение пустое — предложите этапы ещё раз.';
  }
  const message = err && err.message ? String(err.message) : '';
  return message || 'Не получилось — попробуйте ещё раз.';
}

/** Тело запроса «Предложить этапы». Пояснение ≤ 2000 знаков, как ждёт сервер. */
export function proposePayload(note) {
  const text = String(note === null || note === undefined ? '' : note).trim().slice(0, 2000);
  return text ? { note: text } : {};
}

/* ───────────────────────────────────────────── работа с API (тонко) ── */

/**
 * Решение по предложению.
 * @param {{api: object, roomId: string, action: string, ids?: string[]}} opts
 * @returns {Promise<object>} ответ сервера (`phases`, `proposal`, `text`, `room`)
 */
export async function decidePhases({ api, roomId, action, ids }) {
  const body = decidePayload(action, ids);
  return api.post('/api/rooms/' + enc(roomId) + '/phases/proposal', body);
}

/**
 * Попросить руководителя предложить этапы (карточка «Какие этапы у проекта»).
 * Возвращает объект с `pending: true`, если предложение уже ждёт решения.
 * @param {{api: object, roomId: string, note?: string}} opts
 * @returns {Promise<{ok: boolean, pending?: boolean, root_id?: string, request?: object}>}
 */
export async function proposePhases({ api, roomId, note }) {
  try {
    const res = await api.post('/api/rooms/' + enc(roomId) + '/phases/propose', proposePayload(note)) || {};
    return { ok: true, root_id: String(res.root_id || ''), request: res.request || null, raw: res };
  } catch (err) {
    const reason = String(
      (err && err.details && err.details.error && err.details.error.detail
        && err.details.error.detail.reason) || '',
    );
    if (reason === 'proposal_pending') return { ok: false, pending: true, error: err };
    throw err;
  }
}

/* ──────────────────────────────────────────────────────── редактор ── */

/**
 * Редактор этапов (без своего окна): добавить, переименовать, переставить,
 * убрать. Диктовку в поля ставит общий механизм (`dictation-auto.js`), поэтому
 * здесь только обычные `input`.
 *
 * @param {{phases?: any[], proposal?: any, onChange?: Function, toast?: Function,
 *          addLabel?: string, compact?: boolean}} [opts]
 * @returns {{el: HTMLElement, addButton: HTMLElement, getPhases: Function,
 *            setPhases: Function, refresh: Function, isDirty: Function,
 *            focus: Function, rowCount: Function}}
 */
export function createPhasesEditor(opts = {}) {
  ensureCss(CSS_HREF);
  const o = opts || {};
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  let rows = editorRows(o.phases);
  let proposal = proposalOf(o.proposal);
  let initial = JSON.stringify(phasesPayload(rows));
  let notify = false;

  const listHost = h('div', { class: 'pe-editor__list', role: 'list' });
  const emptyNote = h('p', { class: 'pe-editor__empty small muted' },
    'Этапов пока нет. Добавьте первый — он появится на карте проекта.');
  const counter = h('span', { class: 'pe-editor__count small muted tnum', 'aria-live': 'polite' }, '');
  const intro = h('p', { class: 'pe-editor__intro small muted' },
    'Этапы комнаты видны на карте проекта и в «От вас нужно». Порядок — стрелками «выше/ниже», '
    + 'название можно изменить, ненужный этап — убрать.');

  const addButton = h('button', { class: 'btn btn--secondary btn--sm pe-editor__add', type: 'button' },
    icon('add', { size: 13 }), o.addLabel || 'Добавить этап');
  addButton.addEventListener('click', () => addRow());

  const editor = h('div', { class: 'pe-editor' + (o.compact ? ' pe-editor--compact' : '') });

  /** Список предложенных этапов: что именно добавить в свои. */
  const offerHost = h('div', { class: 'pe-offer-inline' });

  function takenIds() {
    return new Set(rows.map((row) => row.id).filter(Boolean));
  }

  function payload() {
    return phasesPayload(rows);
  }

  function changed() {
    if (!notify) return;
    if (typeof o.onChange === 'function') o.onChange(payload());
  }

  function paintOffer() {
    offerHost.replaceChildren();
    const p = proposal;
    if (!p || p.state !== 'proposed') { offerHost.classList.add('hidden'); return; }
    offerHost.classList.remove('hidden');
    const boxes = [];
    const itemsHost = h('div', { class: 'pe-offer-inline__list' });
    const addChosen = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, 'Добавить выбранные');
    const addAll = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'Добавить все');
    const sync = () => {
      const n = boxes.filter((box) => box.input.checked).length;
      addChosen.disabled = n === 0;
      addChosen.replaceChildren(
        h('span', null, n ? 'Добавить выбранные (' + n + ')' : 'Добавить выбранные'),
      );
    };
    for (const item of p.items) {
      const input = h('input', { type: 'checkbox' });
      input.checked = true;
      input.addEventListener('change', sync);
      boxes.push({ input, item });
      itemsHost.appendChild(h('label', { class: 'pe-offer-inline__item checkbox' },
        input,
        h('span', { class: 'pe-offer-inline__text' },
          h('span', { class: 'pe-offer-inline__title' }, item.title),
          item.description ? h('span', { class: 'pe-offer-inline__desc small muted' }, item.description) : null)));
    }
    const addIds = (ids) => {
      const added = [];
      const skipped = [];
      const taken = takenIds();
      for (const item of p.items) {
        if (Array.isArray(ids) && !ids.includes(item.id)) continue;
        const sameTitle = rows.some((row) => row.title.trim().toLowerCase() === item.title.toLowerCase());
        if (taken.has(item.id) || sameTitle) { skipped.push(item.title); continue; }
        const id = taken.has(item.id) ? phaseIdFor(item.title, taken) : (item.id || phaseIdFor(item.title, taken));
        taken.add(id);
        rows.push({ id, title: item.title });
        added.push(item.title);
      }
      paintRows();
      if (added.length) {
        changed();
        toast('Добавлено: ' + phasesCountText(added.length)
          + (skipped.length ? '. Уже были: ' + skipped.join(', ') : ''), skipped.length ? 'warn' : 'ok');
        paintOffer();
      } else {
        toast('Все предложенные этапы уже есть в списке', 'warn');
      }
    };
    addChosen.addEventListener('click', () => addIds(Array.from(boxes).filter((b) => b.input.checked).map((b) => b.item.id)));
    addAll.addEventListener('click', () => addIds(null));
    sync();

    mountAll(offerHost,
      h('div', { class: 'pe-offer-inline__head' },
        icon('wand', { size: 13 }),
        h('span', { class: 'pe-offer-inline__title-main' },
          'Предложение руководителя — ' + phasesCountText(p.items.length)),
        h('span', { class: 'pe-offer-inline__hint small muted' },
          'Отметьте нужные и добавьте: существующие этапы не изменятся.')),
      itemsHost,
      h('div', { class: 'pe-offer-inline__actions' }, addChosen, addAll),
    );
  }

  function focusRow(index) {
    const inputs = [];
    for (const node of listHost.childNodes || []) {
      if (node && node.nodeType === 1 && node.classList && node.classList.contains('pe-row')) {
        const input = (node.childNodes || []).find((c) => c && c.nodeType === 1
          && String(c.tagName || '').toLowerCase() === 'input');
        if (input) inputs.push(input);
      }
    }
    const target = inputs[Math.max(0, Math.min(index, inputs.length - 1))];
    if (target && typeof target.focus === 'function') target.focus();
  }

  function paintRows() {
    listHost.replaceChildren();
    counter.textContent = phaseCountLabel(rows.length);
    emptyNote.classList.toggle('hidden', rows.length > 0);
    if (!rows.length) return;
    rows.forEach((row, index) => {
      const input = h('input', {
        class: 'input pe-row__input', type: 'text', maxlength: String(MAX_TITLE),
        value: row.title, placeholder: 'Например: Согласование',
        'aria-label': 'Название этапа ' + (index + 1),
        title: row.title || 'Название этапа ' + (index + 1),
      });
      input.addEventListener('input', () => {
        row.title = String(input.value || '');
        input.title = row.title || 'Название этапа ' + (index + 1);
        changed();
      });
      const up = iconButton({
        icon: 'chevron-up', size: 'sm', title: 'Выше',
        disabled: index === 0,
        onclick: () => reorder(index, -1),
      });
      const down = iconButton({
        icon: 'chevron-down', size: 'sm', title: 'Ниже',
        disabled: index === rows.length - 1,
        onclick: () => reorder(index, 1),
      });
      const del = iconButton({
        icon: 'trash', size: 'sm', title: 'Убрать этап',
        onclick: () => removeRow(index),
      });
      listHost.appendChild(h('div', { class: 'pe-row', role: 'listitem', dataset: { phase: row.id || String(index) } },
        h('span', { class: 'pe-row__no tnum', 'aria-hidden': 'true' }, String(index + 1)),
        input,
        h('div', { class: 'pe-row__actions' }, up, down, del)));
    });
  }

  function reorder(index, delta) {
    const next = movePhase(rows, index, delta);
    if (next === rows) return;
    rows = next;
    paintRows();
    changed();
    focusRow(index + delta);
  }

  function removeRow(index) {
    rows.splice(index, 1);
    paintRows();
    changed();
    focusRow(index);
  }

  function addRow() {
    if (rows.length >= MAX_PHASES) {
      toast('Не больше ' + MAX_PHASES + ' этапов в комнате', 'warn');
      return;
    }
    const id = phaseIdFor('', takenIds());
    rows.push({ id, title: '' });
    paintRows();
    changed();
    focusRow(rows.length - 1);
  }

  function setPhases(list) {
    rows = editorRows(list);
    initial = JSON.stringify(payload());
    paintRows();
    paintOffer();
  }

  function refresh(nextProposal) {
    proposal = proposalOf(nextProposal);
    paintOffer();
  }

  paintRows();
  paintOffer();
  mountAll(editor, intro, offerHost, listHost, emptyNote,
    h('div', { class: 'pe-editor__foot' }, addButton, counter));

  // Первый вызов onChange — только после того, как человек что-то сделал:
  // иначе диалог комнаты считал бы себя «грязным» сразу после открытия.
  notify = true;

  return {
    el: editor,
    addButton,
    getPhases: payload,
    setPhases,
    refresh,
    isDirty: () => JSON.stringify(payload()) !== initial,
    focus: () => focusRow(0),
    rowCount: () => rows.length,
  };
}

/* ─────────────────────────────────────────── окно редактора этапов ── */

/**
 * Окно «Этапы проекта» — то же самое, что встроенный блок, но с кнопкой
 * «Сохранить». Сохранение — `PATCH /api/rooms/{id} {phases}`.
 *
 * @param {{api: object, roomId: string, phases?: any[], proposal?: any,
 *          toast?: Function, title?: string, onSaved?: Function}} opts
 * @returns {Promise<{room: object|null, phases: object[]}|null>} null — отмена
 */
export async function openPhasesEditor(opts = {}) {
  const o = opts || {};
  ensureCss(CSS_HREF);
  const api = o.api;
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const roomId = String(o.roomId || '');
  if (!api || !roomId) { toast('Не выбрана комната — этапы не открыть', 'warn'); return null; }
  if (typeof document === 'undefined' || !document.body) return null;

  let phases = Array.isArray(o.phases) ? o.phases : null;
  let proposal = o.proposal;
  if (!phases) {
    // «От вас нужно» и другие места не знают список этапов — читаем комнату сами.
    try {
      const room = await api.get('/api/rooms/' + enc(roomId)) || {};
      phases = Array.isArray(room.phases) ? room.phases : [];
      proposal = room.phases_proposal;
    } catch (err) {
      toast(err && err.message ? err.message : 'Не удалось открыть этапы комнаты', 'danger');
      return null;
    }
  }

  return new Promise((resolve) => {
    let done = false;
    let saving = false;
    const errorHost = h('div', { class: 'pe-editor__error' });
    const editor = createPhasesEditor({ phases, proposal, toast });

    const cancelBtn = h('button', { class: 'btn btn--secondary', type: 'button' }, 'Отмена');
    const saveBtn = h('button', { class: 'btn btn--primary', type: 'button' },
      icon('check', { size: 14 }), h('span', null, 'Сохранить этапы'));

    const shell = buildDialog({
      title: o.title || 'Этапы проекта',
      body: h('div', { class: 'pe-modal' },
        h('p', { class: 'pe-modal__lead small muted' },
          'Изменения появятся на карте проекта и в «От вас нужно» сразу после сохранения.'),
        errorHost, editor.el),
      buttons: [cancelBtn, saveBtn],
      // Широкое окно: названия этапов до 80 знаков и 12+ строк должны дышать.
      wide: true,
      onCancel: () => finish(null),
    });

    function finish(value) {
      if (done) return;
      done = true;
      shell.close();
      resolve(value);
    }

    function showError(text) {
      errorHost.replaceChildren();
      if (!text) return;
      errorHost.appendChild(h('div', { class: 'pe-editor__error-text', role: 'alert' }, text));
    }

    cancelBtn.addEventListener('click', async () => {
      if (saving) return;
      if (editor.isDirty()) {
        const ok = await confirmDialog({
          title: 'Закрыть без сохранения?',
          text: 'Изменения этапов не сохранены. Карта проекта останется прежней.',
          confirmLabel: 'Закрыть без сохранения',
          cancelLabel: 'Вернуться к этапам',
          danger: true,
        });
        if (!ok) return;
      }
      finish(null);
    });

    saveBtn.addEventListener('click', async () => {
      if (saving) return;
      saving = true;
      saveBtn.disabled = true;
      saveBtn.replaceChildren(icon('loading', { size: 14, className: 'codicon-modifier-spin' }),
        h('span', null, 'Сохраняю…'));
      showError('');
      const phasesNext = editor.getPhases();
      try {
        const res = await api.patch('/api/rooms/' + enc(roomId), { phases: phasesNext });
        const room = res && typeof res === 'object' ? res : null;
        const proposalDone = room && room.phases_proposal && room.phases_proposal.state === 'accepted';
        toast(proposalDone
          ? 'Этапы сохранены — предложение руководителя закрыто'
          : (phasesNext.length ? 'Этапы сохранены' : 'Этапы убраны: карта покажет только решения'), 'ok');
        if (typeof o.onSaved === 'function') o.onSaved(room);
        finish({ room, phases: (room && room.phases) || phasesNext });
      } catch (err) {
        showError(err && err.message ? err.message : 'Не удалось сохранить этапы');
      } finally {
        saving = false;
        saveBtn.disabled = false;
        saveBtn.replaceChildren(icon('check', { size: 14 }), h('span', null, 'Сохранить этапы'));
      }
    });

    if (typeof saveBtn.focus === 'function') saveBtn.focus();
  });
}

/* ─────────────────────────────────────── плашка предложения этапов ── */

/**
 * Плашка «Руководитель предлагает этапы»: «Принять все / Принять выбранные /
 * Изменить / Отклонить». Отмеченные этапы по умолчанию — все.
 *
 * @param {{proposal: any, roomId: string, api: object, toast?: Function,
 *          onDecided?: Function, onEdit?: Function, onReject?: Function,
 *          confirmReject?: Function}} opts
 * @returns {HTMLElement}
 */
export function createProposalBanner(opts = {}) {
  const o = opts || {};
  ensureCss(CSS_HREF);
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const api = o.api;
  const roomId = String(o.roomId || '');
  const proposal = proposalOf(o.proposal);
  if (!proposal || proposal.state !== 'proposed') return h('div', { class: 'hidden' });

  let busy = false;
  const chosen = new Set(proposal.items.map((item) => item.id));
  const boxes = [];
  const counter = h('span', { class: 'pe-offer__badge chip chip--info' }, phasesCountText(proposal.items.length));

  const acceptAll = h('button', { class: 'btn btn--primary btn--sm', type: 'button' },
    icon('check-all', { size: 13 }), DECISION_LABELS.accept);
  const acceptSelected = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' },
    icon('check', { size: 13 }), h('span', null, DECISION_LABELS.accept_selected));
  const editBtn = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' },
    icon('edit', { size: 13 }), 'Изменить');
  const rejectBtn = h('button', { class: 'btn btn--ghost btn--sm pe-offer__reject', type: 'button' },
    icon('close', { size: 13 }), DECISION_LABELS.reject);

  function syncSelected() {
    const n = chosen.size;
    acceptSelected.disabled = busy || n === 0;
    acceptSelected.replaceChildren(icon('check', { size: 13 }),
      h('span', null, n === proposal.items.length
        ? DECISION_LABELS.accept_selected
        : DECISION_LABELS.accept_selected + ' (' + n + ')'));
  }

  const itemsHost = h('div', { class: 'pe-offer__list' });
  for (const item of proposal.items) {
    const input = h('input', { type: 'checkbox' });
    input.checked = true;
    input.addEventListener('change', () => {
      if (input.checked) chosen.add(item.id); else chosen.delete(item.id);
      syncSelected();
    });
    boxes.push(input);
    itemsHost.appendChild(h('label', { class: 'pe-offer__item checkbox' },
      input,
      h('span', { class: 'pe-offer__item-text' },
        h('span', { class: 'pe-offer__item-title' }, item.title),
        item.description ? h('span', { class: 'pe-offer__item-desc small muted' }, item.description) : null)));
  }

  function setBusy(on) {
    busy = !!on;
    for (const btn of [acceptAll, editBtn, rejectBtn]) btn.disabled = busy;
    for (const input of boxes) input.disabled = busy;
    syncSelected();
  }

  async function decide(action, ids) {
    if (busy || !api) return;
    if (action === 'reject') {
      const ask = typeof o.confirmReject === 'function' ? o.confirmReject : confirmDialog;
      if (typeof o.onReject === 'function') { o.onReject(); return; }
      const ok = await ask({
        title: 'Отклонить предложение этапов?',
        text: 'Этапы комнаты останутся как есть. Отклонённое предложение не вернётся само: '
          + 'попросить этапы заново можно кнопкой «Предложить этапы».',
        confirmLabel: 'Отклонить',
        cancelLabel: 'Оставить',
        danger: true,
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      const res = await decidePhases({ api, roomId, action, ids });
      toast(decisionToast(res), 'ok');
      if (typeof o.onDecided === 'function') o.onDecided(res);
    } catch (err) {
      toast(proposalErrorText(err), 'danger');
      if (typeof o.onDecided === 'function') o.onDecided(null);
    } finally {
      setBusy(false);
    }
  }

  acceptAll.addEventListener('click', () => decide('accept'));
  acceptSelected.addEventListener('click', () => decide('accept_selected', Array.from(chosen)));
  rejectBtn.addEventListener('click', () => decide('reject'));
  if (typeof o.onEdit === 'function') editBtn.addEventListener('click', () => o.onEdit());
  else editBtn.classList.add('hidden');

  syncSelected();

  return h('section', {
    class: 'card pe-offer', role: 'region', 'aria-label': 'Предложение этапов от руководителя',
  },
  h('div', { class: 'pe-offer__head' },
    h('span', { class: 'pe-offer__icon', 'aria-hidden': 'true' }, icon('wand', { size: 15 })),
    h('h3', { class: 'pe-offer__title' }, proposalHeadline(proposal)),
    counter),
  h('p', { class: 'pe-offer__intro' }, proposalIntro(proposal)),
  itemsHost,
  h('div', { class: 'pe-offer__actions' }, acceptAll, acceptSelected, editBtn, rejectBtn),
  h('p', { class: 'pe-offer__note small muted' },
    '«Изменить» открывает редактор: можно добавить только часть этапов или переписать названия.'));
}

/* ──────────────────────────────── «ждём ответа» и кнопка «Предложить» ── */

/**
 * Карточка «Руководитель получил задание» — пока предложения ещё нет
 * (в демо ответ приходит сразу, на настоящем сервере — когда ответит модель).
 * @param {{rootId?: string, onOpen?: Function}} [opts]
 * @returns {HTMLElement}
 */
export function createProposalWaiting(opts = {}) {
  ensureCss(CSS_HREF);
  const o = opts || {};
  const open = typeof o.onOpen === 'function' && o.rootId
    ? h('button', {
      class: 'btn btn--ghost btn--sm', type: 'button',
      onclick: () => o.onOpen(o.rootId),
    }, icon('link-external', { size: 12 }), 'Открыть задачу')
    : null;
  return h('section', { class: 'card pe-wait', role: 'status' },
    h('div', { class: 'pe-wait__head' },
      icon('clock', { size: 14 }),
      h('h3', { class: 'pe-wait__title' }, 'Руководитель получил задание — предложит этапы')),
    h('p', { class: 'pe-wait__text small muted' },
      'Как только ответ будет готов, здесь появится список этапов: их можно принять все, '
      + 'выбрать нужные, поправить или отклонить.'),
    open);
}

/**
 * Кнопка «Предложить этапы». Одна на карту и на пустое состояние.
 * @param {{api: object, roomId: string, toast?: Function, onSent?: Function, small?: boolean}} opts
 * @returns {HTMLElement}
 */
export function createProposeButton(opts = {}) {
  const o = opts || {};
  ensureCss(CSS_HREF);
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const btn = h('button', {
    class: 'btn btn--secondary btn--sm pe-propose', type: 'button',
    title: 'Руководитель комнаты предложит этапы проекта — их можно принять или поправить',
  }, icon('wand', { size: 13 }), h('span', null, 'Предложить этапы'));

  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add('is-busy');
    try {
      const res = await proposePhases({ api: o.api, roomId: o.roomId });
      if (res.pending) {
        toast('Руководитель уже предложил этапы — сначала примите их или отклоните', 'warn');
        if (typeof o.onSent === 'function') o.onSent(null);
      } else {
        toast('Задача руководителю отправлена: он предложит этапы', 'ok');
        if (typeof o.onSent === 'function') o.onSent(res);
      }
    } catch (err) {
      toast(proposalErrorText(err), 'danger');
    } finally {
      btn.disabled = false;
      btn.classList.remove('is-busy');
    }
  });
  return btn;
}

export default {
  MAX_PHASES,
  MAX_TITLE,
  DECISION_LABELS,
  slugId,
  phaseIdFor,
  normPhases,
  phasesPayload,
  movePhase,
  phaseCountLabel,
  phasesCountText,
  proposalOf,
  proposalPending,
  proposalHeadline,
  proposalIntro,
  decidePayload,
  decisionToast,
  proposalErrorText,
  proposePayload,
  decidePhases,
  proposePhases,
  createPhasesEditor,
  openPhasesEditor,
  createProposalBanner,
  createProposalWaiting,
  createProposeButton,
};
