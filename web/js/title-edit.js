/**
 * title-edit.js — название запроса в шапке страницы: карандаш, правка на месте,
 * замок «название задано вами» (W17-G §1, B8/P5).
 *
 * Поведение:
 *  * на экране всегда короткое название (`title_short`), полный текст — в подсказке;
 *  * карандаш → поле на месте названия; Enter сохраняет, Esc отменяет;
 *  * сохранение → `PATCH /api/requests/{id} {title}` (сервер: `title_source=user`,
 *    `title_locked=true`); после сохранения появляется замок с подсказкой «сбросить»;
 *  * пустое значение в PATCH снимает блокировку — это и делает клик по замку;
 *  * пока сервер пересчитывает название сам (`llm`/`leader`), на экране остаётся
 *    прежний текст: никаких «Загружаю…» и мигания — обновление приходит обычным
 *    опросом страницы и применяется патчем на месте (live-patch.js).
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { ensureCss } from './ui.js';
import {
  titleShortOf, titleHint, titleLockedOf, lockHint, lockNote,
  renameBody, validateTitle, applyTitlePayload,
} from './request-title.js';

const enc = (s) => encodeURIComponent(String(s === null || s === undefined ? '' : s));

/**
 * Собрать блок названия для шапки запроса.
 * @param {{getDetail: Function, api: object, toast?: Function,
 *          onSaved?: Function, roomTitle?: string}} opts
 * @returns {HTMLElement}
 */
export function buildTitleBlock(opts = {}) {
  ensureCss('css/shell-titles.css');
  const o = opts || {};
  const getDetail = typeof o.getDetail === 'function' ? o.getDetail : () => ({});
  const api = o.api;
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const onSaved = typeof o.onSaved === 'function' ? o.onSaved : () => {};

  const state = { editing: false, busy: false };

  const titleEl = h('h1', { class: 'req__title', 'data-key': 'title' });
  const input = h('input', {
    class: 'input req__title-input',
    type: 'text',
    maxlength: '120',
    spellcheck: 'false',
    'data-keep': 'request-title',
    'aria-label': 'Название запроса',
  });

  const editBtn = h('button', {
    class: 'btn btn--ghost btn--icon btn--sm req__titlebtn',
    type: 'button',
    'data-act': 'rename',
    title: 'Переименовать запрос',
    'aria-label': 'Переименовать запрос',
  }, icon('edit', { size: 13 }));

  const okBtn = h('button', {
    class: 'btn btn--primary btn--sm req__titlesave',
    type: 'button',
    'data-act': 'rename-save',
    title: 'Сохранить название (Enter)',
  }, 'Сохранить');

  const cancelBtn = h('button', {
    class: 'btn btn--ghost btn--sm req__titlecancel',
    type: 'button',
    'data-act': 'rename-cancel',
    title: 'Отменить (Esc)',
  }, 'Отмена');

  const lockBtn = h('button', {
    class: 'chip chip--muted req__lock',
    type: 'button',
    'data-act': 'rename-reset',
    title: lockHint(true),
    'aria-label': lockHint(true),
  }, icon('lock', { size: 11 }), 'ваше название');

  const block = h('div', { class: 'req__titleblock', 'data-key': 'request-title' },
    titleEl,
    h('div', { class: 'req__titleedit' }, input, okBtn, cancelBtn),
    editBtn,
    lockBtn);

  function detail() {
    const d = getDetail();
    return d && typeof d === 'object' ? d : {};
  }

  function paint() {
    const d = detail();
    const short = titleShortOf(d) || 'Запрос';
    const hint = titleHint(d, o.roomTitle ? 'Комната: ' + o.roomTitle : '');
    titleEl.textContent = short;
    titleEl.setAttribute('title', hint || short);
    const locked = titleLockedOf(d);
    lockBtn.hidden = !locked;
    lockBtn.setAttribute('title', lockHint(locked));
    lockBtn.setAttribute('aria-label', lockHint(locked));
    block.classList.toggle('is-editing', state.editing);
    block.classList.toggle('is-locked', locked);
    block.classList.toggle('is-busy', state.busy);
    titleEl.hidden = state.editing;
    editBtn.hidden = state.editing;
    okBtn.hidden = !state.editing;
    cancelBtn.hidden = !state.editing;
    input.hidden = !state.editing;
    input.disabled = state.busy;
    if (state.editing && input.value === '' && !state.busy) input.value = titleShortOf(d);
    block.setAttribute('aria-busy', state.busy ? 'true' : 'false');
    // Технический id запроса в тексте не показываем — только в подсказке.
    const id = String(d.id || '');
    if (id) titleEl.setAttribute('data-rid', id);
  }

  function startEdit() {
    if (state.busy) return;
    state.editing = true;
    input.value = titleShortOf(detail()) || '';
    paint();
    if (typeof input.focus === 'function') input.focus();
    if (typeof input.select === 'function') input.select();
  }

  function stopEdit() {
    state.editing = false;
    state.busy = false;
    input.value = '';
    paint();
  }

  async function save() {
    const d = detail();
    const id = String(d.id || '');
    if (!id) { stopEdit(); return; }
    const check = validateTitle(input.value, d);
    if (!check.ok) {
      if (check.reason === 'same') { stopEdit(); return; }
      if (check.reason === 'empty') {
        toast('Название не может быть пустым. Чтобы вернуть автоматическое — нажмите на замок.', 'warn');
        if (typeof input.focus === 'function') input.focus();
        return;
      }
      toast('Слишком длинное название — до 120 знаков.', 'warn');
      return;
    }
    state.busy = true;
    paint();
    try {
      const res = await api.patch('/api/requests/' + enc(id), renameBody(check.value));
      applyTitlePayload(d, res);
      stopEdit();
      onSaved();
      toast('Название сохранено', 'ok');
    } catch (err) {
      state.busy = false;
      paint();
      const message = err && err.message ? err.message : 'Не удалось сохранить название';
      toast(message, 'danger');
    }
  }

  async function resetTitle() {
    const d = detail();
    const id = String(d.id || '');
    if (!id || state.busy) return;
    state.busy = true;
    state.editing = false;
    paint();
    try {
      const res = await api.patch('/api/requests/' + enc(id), renameBody(''));
      applyTitlePayload(d, res);
      state.busy = false;
      paint();
      onSaved();
      toast('Название снова формируется автоматически', 'ok');
    } catch (err) {
      state.busy = false;
      paint();
      toast(err && err.message ? err.message : 'Не удалось сбросить название', 'danger');
    }
  }

  editBtn.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    startEdit();
  });
  lockBtn.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    resetTitle();
  });
  okBtn.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    save();
  });
  cancelBtn.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    stopEdit();
  });
  input.addEventListener('keydown', (ev) => {
    const key = ev && ev.key;
    if (key === 'Enter') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      save();
    } else if (key === 'Escape' || key === 'Esc') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
      stopEdit();
      if (typeof editBtn.focus === 'function') editBtn.focus();
    }
  });
  input.addEventListener('blur', () => {
    // Ушли из поля, ничего не нажав — правку не сохраняем (Enter сохраняет).
    if (!state.busy && state.editing) stopEdit();
  });

  paint();
  return block;
}

export default { buildTitleBlock };
