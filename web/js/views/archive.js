/**
 * archive.js — комната, вкладка «Архив» (W29-P4).
 *
 * Здесь живут запросы, которые убрали как неактуальные. Экран только читает:
 * список с поиском и фильтром по дате, открытие запроса в режиме чтения и
 * «Вернуть из архива» (по одному и массово). Возврат не запускает модели:
 * сервер возвращает карточки в прежние статусы, а незавершённые — «на паузу».
 *
 * Данные: GET /api/rooms/{id}/archive?q=&from=
 *         POST /api/rooms/{id}/archive/restore {root_ids}
 */

import { h, debounce, timeAgo, fmtDateTime } from '../util.js';
import { countNoun } from '../text-ru.js';
import { icon } from '../icons.js';
import { ensureCss, emptyState, skeleton, errorBanner, segmented, sectionHeader } from '../ui.js';
import { statusChip } from '../ui.js';
import { titleShortOf, titleHint } from '../request-title.js';

const FILTERS = [
  { value: 'all', label: 'Весь архив' },
  { value: 'month', label: 'За месяц' },
  { value: 'quarter', label: 'За 3 месяца' },
];

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/** Дата «30 ноября 2026» из unix-секунд ('' — нет даты). */
function whenText(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  try { return fmtDateTime(n); } catch { return ''; }
}

export async function mount(el, ctx) {
  ensureCss('css/view-archive.css');
  const roomId = String((ctx.route && ctx.route.roomId) || (ctx.room && ctx.room.id) || '');
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  if (!roomId) {
    el.appendChild(emptyState({ icon: 'error', title: 'Комната не выбрана',
      text: 'Откройте комнату в левой колонке.' }));
    return { unmount() {} };
  }

  const api = ctx.api;
  const state = { items: [], total: 0, q: '', range: 'all', error: null,
    loaded: false, busy: false, selected: new Set(), suggest: null, auto: false,
    hintHidden: false };
  let disposed = false;
  let seq = 0;

  const search = h('input', {
    class: 'input', type: 'search', autocomplete: 'off',
    placeholder: 'Поиск по названию и тексту задачи',
    'aria-label': 'Поиск по архиву',
  });
  const listHost = h('div', { class: 'archive__list', role: 'list' });
  const actionsHost = h('div', { class: 'archive__actions row row--wrap row--tight' });
  const noteHost = h('div', { class: 'archive__note small muted' });
  const hintHost = h('div', { class: 'archive__hint' });
  const head = sectionHeader('Архив', { count: 0 });
  const countPill = head.querySelector('.section-header__count');
  if (countPill) countPill.classList.add('count-pill');

  const filtersHost = h('div', { class: 'archive__filters' });
  const toolbar = h('div', { class: 'archive__toolbar row row--between row--wrap' },
    h('div', { class: 'archive__search search-field' }, icon('search', { size: 13 }), search),
    filtersHost);

  const root = h('div', { class: 'view__inner archive' },
    head,
    h('p', { class: 'archive__intro muted' },
      'Сюда попадают задачи, убранные как «не актуально». Работа по ним остановлена. ',
      'Откройте задачу, чтобы прочитать итог, или верните его в работу — данные и ответы сохранятся.'),
    toolbar, noteHost, hintHost, actionsHost, listHost);
  el.appendChild(root);

  let filtersEl = null;
  function renderFilters() {
    const next = segmented({
      options: FILTERS, value: state.range,
      onchange: (v) => { state.range = String(v); load(); },
    });
    if (filtersEl) filtersEl.replaceWith(next); else filtersHost.appendChild(next);
    filtersEl = next;
  }

  function rangeFrom() {
    const days = state.range === 'month' ? 31 : state.range === 'quarter' ? 92 : 0;
    if (!days) return '';
    return String(Math.floor(Date.now() / 1000) - days * 86400);
  }

  function selectedIds() {
    return [...state.selected].filter((id) => state.items.some((i) => i.id === id));
  }

  function renderActions() {
    const ids = selectedIds();
    actionsHost.replaceChildren();
    if (!ids.length) return;
    const btn = h('button', { class: 'btn btn--primary btn--sm', type: 'button' },
      icon('debug-restart', { size: 13 }), `Вернуть выбранные (${ids.length})`);
    btn.addEventListener('click', () => restore(ids, btn));
    actionsHost.append(btn,
      h('button', { class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => { state.selected.clear(); paint(); } }, 'Снять выбор'));
  }

  /** Профиль, чьи заметки — «заметка комнаты»: руководитель, иначе первый участник. */
  function noteProfile() {
    const room = ctx.room || {};
    if (room.orchestrator) return String(room.orchestrator);
    const members = Array.isArray(room.members) ? room.members : [];
    const first = members.find((m) => m && (m.profile || m.id || m.name));
    return first ? String(first.profile || first.id || first.name) : '';
  }

  /** «Скопировать итог в заметку»: итог запроса — записью в заметки руководителя. */
  async function copyToNote(item, btn) {
    const rid = String(item.id || '');
    const short = titleShortOf(item) || 'Без названия';
    const body = String(item.summary || item.preview || item.text || '').trim();
    const text = `Итог задачи «${short}»`
      + (body ? `:\n${body}` : ` — задача ${rid}`);
    const profile = noteProfile();
    if (!profile) {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          toast('Итог скопирован — вставьте его в заметку', 'ok');
          return;
        }
      } catch { /* буфер недоступен — скажем причину ниже */ }
      toast('Не нашёл, кому писать заметку: в комнате нет участников', 'danger');
      return;
    }
    if (btn) btn.disabled = true;
    try {
      await api.post(`/api/rooms/${enc(roomId)}/agents/${enc(profile)}/notes/entries`,
        { text, source: 'request', origin: rid, why: 'итог архивной задачи' });
      toast('Итог скопирован в заметку', 'ok');
    } catch (err) {
      toast((err && err.message) || 'Не удалось записать итог в заметку', 'danger');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /** «В архив» из подсказки: один запрос, без выбора. */
  async function archiveOne(rootId, btn) {
    if (!rootId) return;
    if (btn) btn.disabled = true;
    try {
      const res = await api.post(`/api/rooms/${enc(roomId)}/archive/requests`,
        { root_ids: [rootId] });
      const first = ((res && res.results) || []).find((r) => r && !r.ok);
      if (first) toast(first.message || 'Не удалось убрать задачу в архив', 'danger');
      else toast('Задача в архиве — можно вернуть в любой момент', 'ok');
      await load();
    } catch (err) {
      toast((err && err.message) || 'Не удалось убрать задачу в архив', 'danger');
      if (btn) btn.disabled = false;
    }
  }

  /** Мягкая подсказка «пора в архив» (завершён ≥ 14 дней без событий). */
  function renderHint() {
    hintHost.replaceChildren();
    const data = state.suggest;
    if (state.hintHidden || !data || !Number(data.total)) return;
    const items = Array.isArray(data.items) ? data.items : [];
    const lines = items.map((item) => {
      const rid = String(item.root_id || '');
      const short = titleShortOf(item) || 'Без названия';
      const btn = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' },
        'В архив');
      btn.addEventListener('click', () => archiveOne(rid, btn));
      return h('div', { class: 'archive__hintrow row row--between row--tight' },
        h('a', { class: 'archive__hinttitle', href: `#/room/${enc(roomId)}/request/${enc(rid)}`,
          title: short }, short),
        h('span', { class: 'archive__hintdays small muted' }, `${item.days} дн.`),
        btn);
    });
    const hide = h('button', { class: 'btn btn--ghost btn--sm', type: 'button',
      onclick: () => { state.hintHidden = true; renderHint(); } }, 'Оставить как есть');
    hintHost.appendChild(h('div', { class: 'archive__hintbox' },
      h('div', { class: 'archive__hinthead' },
        h('strong', null, `Пора в архив: ${countNoun(Number(data.total), ['задача', 'задачи', 'задач'])}`),
        h('div', { class: 'small muted' },
          `Завершены больше ${Number(data.after_days) || 14} дней назад и с тех пор без событий. `
          + 'Автоархив выключен — решаете вы.')),
      ...lines,
      h('div', { class: 'archive__hintfoot row row--tight' }, hide)));
  }

  function row(item) {
    const rid = String(item.id || '');
    const href = `#/room/${enc(roomId)}/request/${enc(rid)}`;
    const short = titleShortOf(item) || 'Без названия';
    const box = h('input', {
      type: 'checkbox', class: 'archive__check', 'aria-label': 'Выбрать «' + short + '»',
    });
    box.checked = state.selected.has(rid);
    box.addEventListener('change', () => {
      if (box.checked) state.selected.add(rid); else state.selected.delete(rid);
      renderActions();
    });
    const opened = Number(item.archived_at || item.finished_at || item.created_at || 0);
    const prev = (item.archive_prev && item.archive_prev[rid]) || '';
    const was = prev === 'done' ? 'был завершён'
      : (prev ? 'работа была остановлена' : '');
    const restoreBtn = h('button', {
      class: 'btn btn--secondary btn--sm archive__restore', type: 'button',
      title: 'Вернуть задачу в обычный список — без запуска моделей',
    }, icon('debug-restart', { size: 12 }), 'Вернуть');
    restoreBtn.addEventListener('click', () => restore([rid], restoreBtn));
    const noteBtn = h('button', {
      class: 'btn btn--ghost btn--sm archive__t-onote', type: 'button',
      title: 'Скопировать итог задачи записью в заметки комнаты',
    }, icon('note', { size: 12 }), 'Итог в заметку');
    noteBtn.addEventListener('click', () => { copyToNote(item, noteBtn).catch(() => {}); });

    return h('article', { class: 'archive__row', role: 'listitem', dataset: { rid } },
      h('div', { class: 'archive__main' },
        h('div', { class: 'archive__titlerow' },
          box,
          h('a', { class: 'archive__title', href, title: titleHint(item) || short }, short),
          statusChip('archived')),
        h('div', { class: 'archive__meta small muted' },
          h('span', null, icon('archive', { size: 11 }), ' в архиве ' + (whenText(item.archived_at) || timeAgo(opened))),
          h('span', null, '· создан ' + (whenText(item.created_at) || '—')),
          was ? h('span', null, '· ' + was) : null),
        item.preview ? h('p', { class: 'archive__snippet' }, String(item.preview)) : null),
      h('div', { class: 'archive__side' },
        h('a', { class: 'btn btn--ghost btn--sm', href }, 'Открыть', icon('chevron-right', { size: 12 })),
        noteBtn,
        restoreBtn));
  }

  function paint() {
    listHost.replaceChildren();
    if (countPill) {
      countPill.textContent = String(state.total);
      countPill.classList.toggle('hidden', !state.total);
    }
    noteHost.textContent = state.q || state.range !== 'all'
      ? `Найдено: ${state.items.length} из ${state.total}`
      : (state.total ? `Всего в архиве: ${countNoun(state.total, ['задача', 'задачи', 'задач'])}` : '');
    if (!state.loaded && !state.error) {
      listHost.appendChild(skeleton(4));
      renderActions();
      return;
    }
    if (state.error) {
      listHost.appendChild(errorBanner(state.error, () => load()));
      renderActions();
      return;
    }
    if (!state.items.length) {
      listHost.appendChild(emptyState({
        icon: 'archive',
        title: state.total ? 'Ничего не нашлось' : 'Архив пуст',
        text: state.total
          ? 'В архиве нет задач под этот поиск или срок. Сбросьте фильтр, чтобы увидеть все.'
          : 'Убранные задачи («Не актуально → в архив») попадают сюда: их можно прочитать и вернуть.',
        action: state.total
          ? { label: 'Сбросить поиск', onclick: () => { search.value = ''; state.q = ''; state.range = 'all'; renderFilters(); load(); } }
          : { label: 'К задачам комнаты', variant: 'primary',
            onclick: () => { if (typeof ctx.navigate === 'function') ctx.navigate(`#/room/${enc(roomId)}/composer`); } },
      }));
      renderActions();
      return;
    }
    const frag = document.createDocumentFragment();
    for (const item of state.items) frag.appendChild(row(item));
    listHost.appendChild(frag);
    renderActions();
  }

  async function load() {
    if (disposed) return;
    const my = ++seq;
    state.error = null;
    if (!state.items.length) state.loaded = false;
    paint();
    try {
      const params = new URLSearchParams();
      if (state.q) params.set('q', state.q);
      const from = rangeFrom();
      if (from) params.set('from', from);
      const data = await api.get(`/api/rooms/${enc(roomId)}/archive?${params.toString()}`);
      if (disposed || my !== seq) return;
      state.items = Array.isArray(data && data.items) ? data.items : [];
      state.total = Number((data && data.total) || state.items.length);
      state.suggest = (data && data.suggest) || null;
      state.auto = Boolean(data && data.auto_archive);
      state.loaded = true;
      state.selected.clear();
      paint();
      renderHint();
    } catch (err) {
      if (disposed || my !== seq) return;
      state.error = err;
      state.loaded = true;
      paint();
    }
  }

  async function restore(ids, btn) {
    if (!ids.length || state.busy) return;
    state.busy = true;
    const label = btn && btn.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Возвращаю…'; }
    try {
      const res = await api.post(`/api/rooms/${enc(roomId)}/archive/restore`, { root_ids: ids });
      const done = Number((res && res.done) || 0);
      const failed = Number((res && res.failed) || 0);
      if (done) toast(done === 1 ? 'Задача возвращена в работу' : `Возвращено задач: ${done}`, 'ok');
      if (failed) {
        const first = ((res && res.results) || []).find((r) => r && !r.ok);
        toast(first && first.message ? first.message : 'Не все задачи удалось вернуть', 'danger');
      }
      await load();
    } catch (err) {
      toast((err && err.message) || 'Не удалось вернуть из архива', 'danger');
      if (btn) { btn.disabled = false; if (label) btn.textContent = label; }
    } finally {
      state.busy = false;
    }
  }

  const runSearch = debounce(() => {
    const value = String(search.value || '').trim();
    if (value === state.q) return;
    state.q = value;
    load();
  }, 250);
  search.addEventListener('input', runSearch);

  renderFilters();
  await load();

  return {
    unmount() {
      disposed = true;
      runSearch.cancel();
      root.remove();
    },
  };
}

export default { mount };
