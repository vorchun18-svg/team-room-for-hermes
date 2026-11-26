/**
 * views/room-dialog.js — настройки комнаты: создание и правка (волна 19, B-30…B-33).
 *
 *   import { openRoomDialog } from './views/room-dialog.js';
 *   await openRoomDialog(ctx, room | null, { focus: 'project_mode' });
 *
 * Что изменилось в 1.0.4:
 *  • B-32 — окно разбито на разделы-вкладки: «Основное», «Состав», «Работа»,
 *    «Проект», «Файлы». Модели и «Особенности» участников — одной строкой со
 *    ссылкой в карточку команды (там полный интерфейс), без второго редактора.
 *  • B-30 — поле «Доска» убрано: доска одна, выбор из одного варианта ни о чём
 *    не говорит. В тело запроса `board` по-прежнему уходит (так ждёт сервер).
 *  • B-31 — окно больше не теряет введённое. Esc и клик мимо окна не выбрасывают
 *    правки: сначала «Сохранить / Закрыть без сохранения / Вернуться». Введённое
 *    хранится в sessionStorage и восстанавливается после перезагрузки страницы.
 *  • B-33 — состояние проектного режима («Сейчас: включён / выключен») — заметной
 *    плашкой; сохраняется тем же PATCH, после сохранения карта обновляется.
 *
 * Почему окно «вылетало» (проверено в песочнице, 06.11): опрос, перерисовка
 * страницы, обновление списка комнат и смена маршрута окно НЕ закрывают — оно
 * живёт в document.body и не зависит от #view. Закрывали его три пути без
 * защиты: Esc (в том числе нажатый, чтобы закрыть выпадающий список), клик по
 * затемнению мимо окна и перезагрузка webview (после неё маршрут
 * восстанавливался, а введённое пропадало). Все три теперь безопасны.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import {
  ensureCss, field, iconButton, pathChip, confirmDialog,
} from '../ui.js';
import { profileOptionLabel, humanApiMessage } from '../shell-labels.js';
// WAVE13 §11: единая подпись участника — «Имя — должность», тех. имя профиля мелко.
import { setProfileDirectory, participantLabel } from '../participant-label.js';
// W23-MG2 «Контроль моделей»: настройка комнаты «строго / только предупреждать».
import { guardModeField } from './model-guard.js';
import { countNoun } from '../text-ru.js';
import {
  projectDirButton, CHECK_DELAY_MS, dirVerdict, suggestProjectDir, MOVE_NOTE,
} from '../project-dir-button.js';
// Особенности для этой комнаты: записи живут в большом окне-«блокноте», здесь —
// только ссылки «Особенности (N)» (WAVE17 P6, без второго редактора).
import { notesOf, entriesOfItem } from '../agent-notes-ui.js';
import { notesLink } from '../notes-editor.js';
// W20-FC §1.7 (F-2): этапы комнаты правит тот же редактор, что и на карте проекта.
import { createPhasesEditor } from './phases-editor.js';

const BOARD = 'team';

/** Разделы-вкладки окна (B-32). Порядок — от простого к подробному. */
const DLG_TABS = [
  { id: 'main', title: 'Основное', icon: 'edit' },
  { id: 'members', title: 'Состав', icon: 'organization' },
  { id: 'work', title: 'Работа', icon: 'play-circle' },
  // W27-E6: «Уровень автономии» — что участник делает сам, а что спрашивает.
  { id: 'autonomy', title: 'Автономия', icon: 'shield' },
  { id: 'project', title: 'Проект', icon: 'map' },
  { id: 'files', title: 'Файлы', icon: 'folder' },
];

/** Куда ведёт `opts.focus` (B-9: открыть настройки сразу на нужном разделе). */
const FOCUS_TAB = {
  project_mode: 'project',
  phases: 'project',
  project_dir: 'files',
  files: 'files',
  members: 'members',
  work: 'work',
  autonomy: 'autonomy',
  main: 'main',
};

/** Иконки комнаты, которые точно есть в локальном codicon-наборе. */
const ICONS = [
  { name: 'project', label: 'Проект' },
  { name: 'rocket', label: 'Запуск' },
  { name: 'briefcase', label: 'Дело' },
  { name: 'law', label: 'Право' },
  { name: 'beaker', label: 'Проверки' },
  { name: 'target', label: 'Цель' },
  { name: 'star', label: 'Главное' },
  { name: 'lightbulb', label: 'Идея' },
  { name: 'library', label: 'Знание' },
  { name: 'globe', label: 'Рынок' },
  { name: 'shield', label: 'Защита' },
  { name: 'graph', label: 'График' },
];

const ICON_NAMES = new Set(ICONS.map((i) => i.name));

const enc = (s) => encodeURIComponent(String(s == null ? '' : s));

/**
 * W20-FC §1.7: сообщить открытым вкладкам комнаты, что она сохранилась.
 * Карта проекта показывает этапы сразу, без ожидания опроса и перезагрузки.
 * Событие — удобство: если window нет (тесты), просто молчим.
 */
function notifyRoomSaved(id, room) {
  try {
    if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
    if (typeof CustomEvent !== 'function') return;
    window.dispatchEvent(new CustomEvent('teamroom:room-saved', {
      detail: { id: String(id || ''), room: room || null },
    }));
  } catch { /* событие не обязательно для сохранения */ }
}

/* ───────────────────────────────────── папка проекта (Д2 WAVE8-RULES) */

/* Проверка пути, предложение папки и вердикт живут рядом с кнопкой
   «Открыть папку проекта» — здесь только подключаем их к полю диалога. */
export { CHECK_DELAY_MS, dirVerdict, suggestProjectDir };

function roomArray(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.rooms)) return payload.rooms;
  return [];
}

/**
 * Перечитать комнаты и положить их в store.rooms (форма как в app.js).
 * @param {object} api
 * @param {object} store
 * @returns {Promise<any[]>}
 */
export async function refreshRooms(api, store) {
  const payload = await api.get('/api/rooms?include_archived=1');
  const list = roomArray(payload).map((raw) => {
    if (!raw || !raw.id) return null;
    const c = raw.counters || raw.counts || {};
    const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
    return Object.assign({}, raw, {
      id: String(raw.id),
      title: String(raw.title || raw.id),
      icon: String(raw.icon || 'home'),
      members: Array.isArray(raw.members) ? raw.members : [],
      counters: { working: num(c.working), needs_you: num(c.needs_you), done_today: num(c.done_today) },
      counts: { working: num(c.working), needs_you: num(c.needs_you), done: num(c.done_today) },
      archived: Boolean(raw.archived),
    });
  }).filter(Boolean);
  if (store && store.rooms) {
    store.rooms.set({ list: list.filter((r) => !r.archived), loading: false, error: null, loadedAt: Date.now() });
  }
  return list;
}

function membersOf(room) {
  return Array.isArray(room && room.members) ? room.members.filter(Boolean) : [];
}

function profileName(p) {
  return String((p && (p.profile || p.id)) || '');
}

/* ───────────────────────────────────────── несохранённое и черновик (B-31) */

const DRAFT_PREFIX = 'teamroom.roomDraft.v2.';
/** Черновик живёт сутки: дальше это уже не «случайно закрыл», а забытая правка. */
const DRAFT_TTL_MS = 24 * 60 * 60 * 1000;

function draftKey(roomId) {
  return DRAFT_PREFIX + (roomId ? String(roomId) : 'new');
}

function draftStore() {
  try {
    const store = globalThis.sessionStorage;
    if (!store || typeof store.getItem !== 'function') return null;
    return store;
  } catch { return null; }
}

function readDraft(key) {
  const store = draftStore();
  if (!store) return null;
  try {
    const raw = store.getItem(key);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== 'object' || !data.state) return null;
    const at = Number(data.at) || 0;
    if (at && Date.now() - at > DRAFT_TTL_MS) { store.removeItem(key); return null; }
    return data;
  } catch { return null; }
}

function writeDraft(key, state) {
  const store = draftStore();
  if (!store) return;
  try { store.setItem(key, JSON.stringify({ at: Date.now(), state })); } catch { /* переполнен */ }
}

function clearDraft(key) {
  const store = draftStore();
  if (!store) return;
  try { store.removeItem(key); } catch { /* приватный режим */ }
}

/* ─────────────────────────────────────────────────────────── сам диалог */

/**
 * Диалог комнаты.
 * @param {object} ctx  контекст вьюхи ({api, store, navigate, toast, refresh})
 * @param {object|null} [room]  комната для правки; null/undefined — создание
 * @param {{focus?: string}} [opts] 'main'|'members'|'work'|'project'|'files'|'project_mode'|'phases'|'project_dir'
 * @returns {Promise<object|null>} созданная/изменённая комната или null
 */
export async function openRoomDialog(ctx, room = null, opts = {}) {
  if (typeof document === 'undefined' || !document.body) return null;
  await ensureCss('css/view-rooms.css');
  await ensureCss('css/project-dir.css');

  const api = ctx.api;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const store = ctx.store || {};
  const navigate = typeof ctx.navigate === 'function' ? ctx.navigate : (hash) => { location.hash = hash; };

  const isEdit = Boolean(room && room.id);
  const roomId = isEdit ? String(room.id) : '';
  const key = draftKey(roomId);

  let full = room || null;
  if (isEdit && (full.critic_pass === undefined || !Array.isArray(full.members))) {
    try { full = Object.assign({}, full, await api.get('/api/rooms/' + enc(roomId))); } catch { /* остаёмся на сводке */ }
  }

  let profiles = [];
  try {
    const data = await api.get('/api/profiles');
    profiles = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
    setProfileDirectory(profiles);       // подписи берём оттуда же, где ростер
  } catch (err) {
    toast(err && err.message ? err.message : 'Список профилей недоступен', 'danger');
  }

  // Каталог моделей — ТОЛЬКО для человеческих подписей в короткой строке состава.
  // Редактор модели живёт в карточке команды (B-32), здесь его нет.
  let catalog = null;
  try { catalog = await api.get('/api/models'); } catch { catalog = null; }
  const modelLabels = new Map();
  const effortLabels = new Map();
  for (const provider of (catalog && Array.isArray(catalog.providers) ? catalog.providers : [])) {
    for (const model of (provider && Array.isArray(provider.models) ? provider.models : [])) {
      if (model && model.id) modelLabels.set(String(model.id), String(model.label || model.id));
    }
  }
  for (const effort of (catalog && Array.isArray(catalog.efforts) ? catalog.efforts : [])) {
    if (effort && effort.id) effortLabels.set(String(effort.id), String(effort.label || effort.id));
  }

  /* ─────────────────────────────────────────────────────────────── состояние */

  const selectedMembers = new Set(membersOf(full)
    .filter((m) => String(m.role || 'worker') !== 'critic')
    .map((m) => String(m.profile)));
  let iconName = String((full && full.icon) || 'project');
  if (!ICON_NAMES.has(iconName)) iconName = 'project';
  const files = Array.isArray(full && full.context_files) ? full.context_files.slice() : [];

  /* Проектный режим — настройки ИМЕННО ЭТОЙ комнаты (A1/A2 WAVE9-RULES).
     У новой комнаты он включён, у старой (полей в записи нет) — выключен. */
  let projectMode = full && full.project_mode !== undefined ? Boolean(full.project_mode) : true;
  // Новая комната (W17-R §3, решение Михаила): запускать части сразу, без
  // подтверждения. У существующей комнаты — её сохранённый режим, без правок.
  let launchMode = String((full && full.launch_mode) || (isEdit ? 'plan_first' : 'auto'));
  if (launchMode !== 'plan_first' && launchMode !== 'auto') launchMode = 'plan_first';
  let maxStreams = Number(full && full.max_parallel_streams);
  if (!Number.isInteger(maxStreams) || maxStreams < 1 || maxStreams > 4) maxStreams = 2;
  // Один термин вместо «направление» (B-10): «часть работы».
  const STREAM_FORMS = ['часть работы', 'части работы', 'частей работы'];

  /* ──────────────────────────────────────────────────────────────── каркас */

  const titleId = 'roomdlg-title';
  const dialog = h('div', {
    class: 'dialog dialog--wide roomdlg-shell', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
  });
  const overlay = h('div', { class: 'dialog-backdrop' }, dialog);

  const headTitle = isEdit
    ? 'Настройки комнаты — «' + String(full.title || '') + '»'
    : 'Новая комната';
  const head = h('div', { class: 'dialog__head' },
    h('h2', { class: 'dialog__title', id: titleId }, headTitle),
    iconButton({ icon: 'close', size: 'sm', title: 'Закрыть', onclick: () => requestClose() }),
  );
  const body = h('div', { class: 'dialog__body' });
  const foot = h('div', { class: 'dialog__foot' });
  dialog.appendChild(head);

  /* ── вкладки-разделы (B-32) ───────────────────────────────────────────── */

  const panels = new Map();
  const tabButtons = new Map();
  let activeTab = 'main';

  const tablist = h('div', { class: 'roomdlg__tabs', role: 'tablist', 'aria-label': 'Разделы настроек комнаты' });

  function syncTabs() {
    for (const [id, btn] of tabButtons) {
      const on = id === activeTab;
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
      btn.setAttribute('tabindex', on ? '0' : '-1');
      btn.classList.toggle('roomdlg__tab--active', on);
    }
    for (const panel of panels.values()) {
      const on = panel.dataset.panel === activeTab;
      // Класс `hidden` (display: none) не трогаем: панели лежат в одной ячейке
      // grid и неактивные только невидимы — иначе высота окна = высота вкладки
      // и «Настройки комнаты» прыгают при переключении (дефект 12, W29-P3).
      panel.classList.toggle('roomdlg__panel--off', !on);
      if (on) panel.removeAttribute('hidden');
      else panel.setAttribute('hidden', '');
    }
  }

  function showTab(id, focusTab = false) {
    activeTab = panels.has(String(id)) ? String(id) : 'main';
    syncTabs();
    // Каждый раздел открывается с начала: прокрутка одной вкладки не «съедает»
    // верх другой (окно при этом не двигается).
    if (body && typeof body.scrollTop === 'number') body.scrollTop = 0;
    if (focusTab) {
      const btn = tabButtons.get(activeTab);
      if (btn && typeof btn.focus === 'function') btn.focus();
    }
  }

  for (const tab of DLG_TABS) {
    const btn = h('button', {
      class: 'roomdlg__tab', type: 'button', role: 'tab',
      id: 'roomdlg-tab-' + tab.id, 'aria-controls': 'roomdlg-panel-' + tab.id,
      'data-tab': tab.id,
    }, icon(tab.icon, { size: 13 }), h('span', null, tab.title));
    btn.addEventListener('click', () => showTab(tab.id));
    btn.addEventListener('keydown', (ev) => {
      const ids = DLG_TABS.map((t) => t.id);
      const at = ids.indexOf(activeTab);
      let next = null;
      if (ev.key === 'ArrowRight') next = ids[(at + 1) % ids.length];
      else if (ev.key === 'ArrowLeft') next = ids[(at - 1 + ids.length) % ids.length];
      else if (ev.key === 'Home') next = ids[0];
      else if (ev.key === 'End') next = ids[ids.length - 1];
      if (!next) return;
      ev.preventDefault();
      showTab(next, true);
    });
    tabButtons.set(tab.id, btn);
    tablist.appendChild(btn);
  }
  dialog.appendChild(tablist);

  const formId = 'roomdlg-form';
  const form = h('form', { class: 'roomdlg', id: formId, novalidate: true });
  // Все разделы — в одной ячейке grid (W29-P3, дефект 12): окно имеет постоянную
  // высоту по самому высокому разделу, а не «подстраивается» под активную вкладку.
  const panelsStack = h('div', { class: 'roomdlg__panels' });
  form.appendChild(panelsStack);
  body.appendChild(form);
  dialog.appendChild(body);

  for (const tab of DLG_TABS) {
    const panel = h('section', {
      class: 'roomdlg__panel', role: 'tabpanel',
      id: 'roomdlg-panel-' + tab.id, 'aria-labelledby': 'roomdlg-tab-' + tab.id,
      'data-panel': tab.id, tabindex: '-1',
    });
    panels.set(tab.id, panel);
    panelsStack.appendChild(panel);
  }
  const mainPanel = panels.get('main');
  const membersPanel = panels.get('members');
  const workPanel = panels.get('work');
  const projectPanel = panels.get('project');
  const filesPanel = panels.get('files');
  const autonomyPanel = panels.get('autonomy');

  /* ── W27-E6 «Автономия» ────────────────────────────────────────────────
     Содержимое вкладки живёт в своём файле (views/autonomy.js): уровни,
     предпросмотр «что изменится», список «только с моего да», журнал и
     критичные задачи. Здесь только место и понятный отказ, если файл не
     загрузился, — у вкладки всегда есть текст, а не пустота. */
  if (!isEdit) {
    autonomyPanel.appendChild(h('div', { class: 'roomdlg__hint' },
      'Новая комната начнёт с уровня «Рабочий»: свои скрипты и файлы комнаты участник делает сам, '
      + 'удаление и отправку наружу — спрашивает. Уровень меняется здесь же после создания комнаты.'));
  } else {
    const autonomyHost = h('div', { class: 'roomdlg__autonomy' });
    autonomyPanel.appendChild(autonomyHost);
    import('./autonomy.js')
      .then((m) => m.mountAutonomyPanel(autonomyHost, { api, toast, store, navigate }, roomId))
      .catch(() => {
        autonomyHost.replaceChildren();
        autonomyHost.appendChild(h('div', { class: 'roomdlg__hint' },
          'Не удалось открыть уровень автономии. Закройте окно и откройте «Настройки комнаты» '
          + 'заново — уровень, список «только с моего да» и журнал подтянутся.'));
      });
  }

  const errBanner = h('div', { class: 'roomdlg__error' });
  form.insertBefore(errBanner, form.firstChild);

  /* ─────────────────────────────────────────────────────── «Основное» */

  const titleInput = h('input', {
    class: 'input', type: 'text', maxlength: '120', autocomplete: 'off',
    placeholder: 'Например: Лето', 'aria-label': 'Название комнаты',
  });
  titleInput.value = String((full && full.title) || '');

  const iconsHost = h('div', { class: 'roomdlg__icons', role: 'group', 'aria-label': 'Иконка комнаты' });
  function renderIcons() {
    iconsHost.replaceChildren();
    for (const item of ICONS) {
      const btn = h('button', {
        class: 'roomdlg__icon', type: 'button',
        'aria-pressed': item.name === iconName ? 'true' : 'false',
        'aria-label': item.label, title: item.label,
      }, icon(item.name, { size: 16 }));
      btn.addEventListener('click', () => { iconName = item.name; renderIcons(); markDirty(); });
      iconsHost.appendChild(btn);
    }
  }
  renderIcons();

  const directorSelect = h('select', { class: 'select', 'aria-label': 'Руководитель комнаты' });
  const criticSelect = h('select', { class: 'select', 'aria-label': 'Критик комнаты' });
  const directorHint = h('div', { class: 'roomdlg__hint' });
  const criticHint = h('div', { class: 'roomdlg__hint' });

  function fillSelect(sel, list, current, withNone) {
    sel.replaceChildren();
    if (withNone) sel.appendChild(h('option', { value: '' }, 'Без критика'));
    for (const p of list) {
      const name = profileName(p);
      if (!name) continue;
      // WAVE13 §11: «Имя — должность · профиль»; без дубля «default · default» (D8).
      const label = participantLabel(name, { role: String(p.kind || 'worker'), entry: p });
      sel.appendChild(h('option', { value: name, title: label.title + ' · ' + name },
        profileOptionLabel(Object.assign({}, p, { title: label.title }))));
    }
    if (current && !Array.from(sel.options).some((o) => o.value === current)) {
      sel.appendChild(h('option', { value: current }, current + ' (нет в списке профилей)'));
    }
    sel.value = current || (withNone ? '' : (sel.options[0] ? sel.options[0].value : ''));
  }

  const directors = profiles.filter((p) => String(p.kind || '') === 'director');
  const critics = profiles.filter((p) => String(p.kind || '') === 'critic');
  const others = profiles.filter((p) => !['director', 'critic'].includes(String(p.kind || '')));

  fillSelect(directorSelect, directors.concat(others, critics), String((full && full.orchestrator) || ''), false);
  fillSelect(criticSelect, critics.concat(others, directors), String((full && full.critic) || ''), true);

  /* D3: для новой комнаты состав предвыбран, иначе первый клик «Создать»
     всегда упирается в «нужен хотя бы один исполнитель». Руководитель комнаты и
     критик — первые в своих группах реестра, дальше 2–3 исполнителя. */
  if (!isEdit) {
    if (!directorSelect.value && profiles.length) directorSelect.value = profileName(profiles[0]);
    if (!criticSelect.value && critics.length && profileName(critics[0])) {
      criticSelect.value = profileName(critics[0]);
    }
    const free = profiles.filter((p) => {
      const name = profileName(p);
      return name && name !== directorSelect.value && name !== criticSelect.value;
    });
    const workers = free.filter((p) => String(p.kind || '') === 'worker');
    const picked = workers.slice(0, 3);
    for (const p of free) {
      if (picked.length >= Math.min(3, Math.max(2, workers.length))) break;
      if (!picked.includes(p)) picked.push(p);
    }
    for (const p of picked) selectedMembers.add(profileName(p));
  }

  mainPanel.appendChild(field({ label: 'Название', control: titleInput, hint: 'Коротко: о чём эта комната.' }));
  mainPanel.appendChild(h('div', { class: 'roomdlg__row' },
    field({ label: 'Иконка', control: iconsHost }),
    h('div', { class: 'roomdlg__section' },
      field({ label: 'Руководитель комнаты', control: directorSelect }),
      directorHint,
    ),
  ));
  mainPanel.appendChild(h('p', { class: 'roomdlg__hint' },
    'Критик и раунды проверки — на вкладке «Работа», проектный режим — на вкладке «Проект», '
    + 'папка и файлы контекста — на вкладке «Файлы».'));

  /* ─────────────────────────────────────────────────────────── «Состав» */

  const membersHost = h('div', { class: 'roomdlg__members', role: 'group', 'aria-label': 'Состав комнаты' });
  const peopleHost = h('div', { class: 'roomdlg__people' });

  /** Кто сейчас в составе — по одному правилу для указаний и для модели. */
  function currentParticipants() {
    const people = [];
    if (directorSelect.value) people.push({ profile: directorSelect.value, role: 'orchestrator' });
    if (criticSelect.value) people.push({ profile: criticSelect.value, role: 'critic' });
    for (const profile of selectedMembers) {
      if (profile === directorSelect.value || profile === criticSelect.value) continue;
      people.push({ profile, role: 'worker' });
    }
    return people;
  }

  function roleLabelOf(role) {
    if (role === 'orchestrator') return 'руководитель комнаты';
    if (role === 'critic') return 'критик';
    return 'исполнитель';
  }

  /** Что реально применится к следующей карточке участника — одной строкой. */
  function modelLineOf(profile) {
    const meta = (full && full.agent_models_meta && full.agent_models_meta[profile]) || null;
    const settings = (meta && meta.model_settings)
      || ((membersOf(full).find((m) => String(m.profile || '') === profile) || {}).model_settings)
      || null;
    if (!settings) return '';
    // W20 §1.5: по умолчанию — то, что стоит в настройках профиля Hermes; своё значение
    // этой комнаты помечаем словами, а не цветом: «изменено для этой комнаты».
    const parts = (value) => {
      const model = String((value && value.model) || '').trim();
      if (!model) return '';
      const label = modelLabels.get(model) || model;
      const effort = String((value && value.reasoning) || '').trim();
      const effortLabel = effort ? (effortLabels.get(effort) || effort) : '';
      return effortLabel && effortLabel !== 'как в профиле'
        ? label + ' · мышление: ' + effortLabel : label;
    };
    const changed = String(settings.source || '') === 'room';
    if (changed) {
      const own = parts(settings);
      return own ? 'изменено для этой комнаты: ' + own : 'изменено для этой комнаты';
    }
    const def = (meta && (meta.agent_default_info || meta.agent_default)) || settings;
    const shared = String((def && def.source) || '') === 'global';
    const text = parts(def) || parts(settings);
    if (!text) return 'по умолчанию (из настроек агента): модель не задана';
    return 'по умолчанию (из настроек агента): ' + text
      + (shared ? ' — из общих настроек Hermes' : '');
  }

  function renderMembers() {
    const leader = directorSelect.value;
    const critic = criticSelect.value;
    membersHost.replaceChildren();
    const list = profiles.filter((p) => {
      const name = profileName(p);
      return name && name !== leader && name !== critic;
    });
    // Выбранные участники — первыми, чтобы при правке состав был виден сразу.
    list.sort((a, b) => {
      const sa = selectedMembers.has(profileName(a)) ? 0 : 1;
      const sb = selectedMembers.has(profileName(b)) ? 0 : 1;
      return sa - sb;
    });
    for (const p of list) {
      const name = profileName(p);
      const input = h('input', { type: 'checkbox', id: 'roomdlg-m-' + name });
      input.checked = selectedMembers.has(name);
      input.addEventListener('change', () => {
        if (input.checked) selectedMembers.add(name); else selectedMembers.delete(name);
        renderPeople();                          // краткие строки — только у выбранных
        markDirty();
      });
      // WAVE13 §11: заголовок — имя и должность, техническое имя — мелко справа.
      const note = String(p.description || p.title || '');
      const label = participantLabel(name, { role: String(p.kind || 'worker'), entry: p });
      membersHost.appendChild(h('div', { class: 'roomdlg__member' },
        h('label', {
          class: 'checkbox', for: input.id,
          title: note ? 'Профиль Hermes: ' + name + ' · ' + note : 'Профиль Hermes: ' + name,
        }, input,
          h('span', { class: 'roomdlg__member-name' }, label.title)),
        h('span', { class: 'roomdlg__member-note mono small muted', title: 'Профиль Hermes: ' + name }, name),
      ));
    }
    renderPeople();
  }

  /**
   * Краткие строки по людям: кто, какая модель применится, куда нажать, чтобы
   * её поменять или открыть «Особенности». Полные редакторы — в карточке команды
   * (B-32: один смысл — одно место, без дублей).
   */
  function renderPeople() {
    peopleHost.replaceChildren();
    const people = currentParticipants();
    if (!people.length) return;
    const notes = full && full.id ? notesOf(full) : {};
    const saved = new Set(membersOf(full).map((m) => String(m.profile || '')));
    const teamHash = '#/room/' + enc(full && full.id ? full.id : '') + '/team';

    for (const person of people) {
      const label = participantLabel(person.profile, { role: person.role });
      const who = String(label.title || person.profile);
      const model = modelLineOf(person.profile);
      const row = h('div', { class: 'roomdlg__person', 'data-profile': person.profile },
        h('div', { class: 'roomdlg__person-main' },
          h('span', { class: 'roomdlg__person-who', title: 'Профиль Hermes: ' + person.profile }, who),
          h('span', { class: 'roomdlg__person-role' }, roleLabelOf(person.role)),
        ),
      );
      // Строка модели обрезается по ширине колонки — подсказка несёт полный текст
      // (иначе на «Составе» читается «…: Дилиж…» и непонятно, что за модель).
      if (model) row.appendChild(h('div', { class: 'roomdlg__person-model small muted', title: model }, model));


      const actions = h('div', { class: 'roomdlg__person-actions' });
      if (full && full.id) {
        // Ссылка в карточку команды: там модель, подпись и особенности целиком.
        const cfg = h('a', {
          class: 'roomdlg__person-cfg', href: teamHash,
          title: 'Модель, подпись и «Особенности» — в карточке участника на вкладке «Команда»',
        }, 'Настроить в команде');
        cfg.addEventListener('click', (ev) => {
          if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
          requestClose(teamHash);
        });
        actions.appendChild(cfg);
        if (saved.has(person.profile)) {
          actions.appendChild(notesLink(ctx, {
            room: full,
            roomId: String(full.id),
            profile: person.profile,
            label: who,
            count: entriesOfItem(notes[person.profile] || null).length,
          }));
        }
      }
      if (actions.childNodes.length) row.appendChild(actions);
      peopleHost.appendChild(row);
    }
  }

  membersPanel.appendChild(h('div', { class: 'roomdlg__label' }, 'Кого руководитель комнаты может звать в подзадачи'));
  membersPanel.appendChild(h('div', { class: 'roomdlg__hint' },
    'Руководитель комнаты и критик входят отдельно — их выбирают на вкладках «Основное» и «Работа».'));
  membersPanel.appendChild(membersHost);
  membersPanel.appendChild(h('div', { class: 'roomdlg__label' }, 'Модели и особенности'));
  membersPanel.appendChild(h('div', { class: 'roomdlg__hint' },
    full && full.id
      ? 'Модель, мышление и «Особенности» правятся в карточке участника на вкладке «Команда» — '
        + 'здесь только короткая справка, чтобы не держать два одинаковых редактора.'
      : 'После создания комнаты модель, мышление и «Особенности» настраиваются в карточке '
        + 'участника на вкладке «Команда».'));
  membersPanel.appendChild(peopleHost);

  /* ───────────────────────────────────────────────────────────── «Работа» */

  const criticPassInput = h('input', { type: 'checkbox', id: 'roomdlg-critic-pass' });
  criticPassInput.checked = full && full.critic_pass !== undefined ? Boolean(full.critic_pass) : true;
  // Пользователь сам снял галочку — уважаем выбор; иначе она следует за критиком (D3/D16).
  let criticPassTouched = false;
  criticPassInput.addEventListener('change', () => { criticPassTouched = true; });
  const criticPassBox = h('label', { class: 'checkbox' }, criticPassInput, h('span', null, 'Проверять ответ критиком по умолчанию'));

  /* Раунды критика: 1 / 2 / до полного одобрения (W13-E §7, §9).
     Значение «approve» в select соответствует critic_mode="until_approved". */
  const roundsSelect = h('select', { class: 'select', id: 'roomdlg-critic-rounds', 'aria-label': 'Раундов проверки критика' },
    h('option', { value: '1' }, '1 раунд — быстро'),
    h('option', { value: '2' }, '2 раунда — тщательнее'),
    h('option', { value: 'approve' }, 'До полного одобрения критиком'),
  );
  const savedMode = String((full && full.critic_mode) || 'rounds');
  roundsSelect.value = savedMode === 'until_approved'
    ? 'approve'
    : String(Math.min(2, Math.max(1, Number((full && full.max_rounds) || 1))));

  const capInput = h('input', {
    class: 'input roomdlg__cap', type: 'number', min: '2', max: '30', step: '1',
    id: 'roomdlg-critic-cap', 'aria-label': 'Потолок раундов критика',
    inputmode: 'numeric',
  });
  capInput.value = String(Math.min(30, Math.max(2, Number((full && full.critic_round_cap) || 12))));
  const capBox = h('label', { class: 'roomdlg__cap-box' },
    h('span', { class: 'small muted' }, 'Страховка: остановиться после'),
    capInput,
    h('span', { class: 'small muted' }, 'раундов'));

  // «?» — короткое объяснение простым языком, чтобы не догадываться по названию.
  const roundsHelp = h('div', { class: 'roomdlg__hint roomdlg__hint--help hidden', id: 'roomdlg-critic-help' },
    h('p', null, 'Критик — отдельный участник: он читает сводный ответ команды и говорит, '
      + 'можно ли отдавать его вам.'),
    h('p', null, 'Раунд — один цикл «критик проверил → если нашёл замечания, исполнители '
      + 'доработали → снова проверили». Один раунд — быстрее: у команды одна попытка исправить '
      + 'замечания, дальше ответ придёт с пометками. Два раунда — тщательнее.'),
    h('p', null, '«До полного одобрения» — цикл идёт, пока критик не скажет «принято»: удобно '
      + 'оставить на ночь и утром получить готовое. Чтобы это не крутилось бесконечно, есть '
      + 'страховки: остановка, если замечания повторяются три раунда подряд, пауза при лимите '
      + 'провайдера и потолок раундов.'));
  const roundsHelpBtn = h('button', {
    class: 'btn btn--ghost btn--sm btn--icon roomdlg__help', type: 'button',
    'aria-expanded': 'false', 'aria-controls': 'roomdlg-critic-help',
    title: 'Что такое критик и раунд',
  }, icon('info', { size: 13 }), h('span', { class: 'sr-only' }, 'Что такое раунд'));
  roundsHelpBtn.addEventListener('click', () => {
    const open = roundsHelp.classList.toggle('hidden') === false;
    roundsHelpBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });

  function modeValue() {
    return roundsSelect.value === 'approve' ? 'until_approved' : 'rounds';
  }

  function renderRoundsState() {
    const until = modeValue() === 'until_approved';
    capBox.classList.toggle('hidden', !until || !criticSelect.value);
  }
  roundsSelect.addEventListener('change', renderRoundsState);

  /* W20 §1.1 (B-36): «Автообновление свода» — переключатель и две простые настройки. */

  const QUIET_CHOICES = [60, 120, 180, 300, 600, 900, 1800, 3600];
  const GAP_CHOICES = [300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400];
  const minutesRu = (sec) => countNoun(Math.max(1, Math.round(sec / 60)), ['минута', 'минуты', 'минут']);

  /** Ближайшее значение из списка: сервер принимает любое число в границах. */
  function pickSeconds(value, choices, fallback) {
    const n = Number(value);
    if (!isFinite(n) || n <= 0) return fallback;
    return choices.reduce((best, cur) => (Math.abs(cur - n) < Math.abs(best - n) ? cur : best), choices[0]);
  }

  const autoSvodInput = h('input', { type: 'checkbox', id: 'roomdlg-auto-svod' });
  autoSvodInput.checked = full && full.auto_svod !== undefined ? Boolean(full.auto_svod) : true;
  const autoSvodBox = h('label', { class: 'checkbox' }, autoSvodInput,
    h('span', null, 'Свод обновляется сам'));

  const quietSelect = h('select', {
    class: 'select', id: 'roomdlg-svod-quiet',
    'aria-label': 'Сколько ждать тишины перед обновлением свода',
  });
  for (const sec of QUIET_CHOICES) quietSelect.appendChild(h('option', { value: String(sec) }, minutesRu(sec)));
  quietSelect.value = String(pickSeconds((full && full.auto_svod_quiet_s) || 180, QUIET_CHOICES, 180));

  const gapSelect = h('select', {
    class: 'select', id: 'roomdlg-svod-gap',
    'aria-label': 'Как часто свод может обновляться',
  });
  for (const sec of GAP_CHOICES) gapSelect.appendChild(h('option', { value: String(sec) }, minutesRu(sec)));
  gapSelect.value = String(pickSeconds((full && full.auto_svod_min_gap_s) || 600, GAP_CHOICES, 600));

  /** W29-Q9: «Модель для рутинного сведения» — автосвод и «Уточнение» на лёгкой модели. */
  const routineSelect = h('select', {
    class: 'select', id: 'roomdlg-routine-model',
    'aria-label': 'Модель для рутинного сведения',
  });
  routineSelect.appendChild(h('option', { value: '' }, 'Как у руководителя (по умолчанию)'));
  for (const provider of (catalog && Array.isArray(catalog.providers) ? catalog.providers : [])) {
    const pid = String((provider && provider.id) || '');
    if (!pid) continue;
    for (const model of (Array.isArray(provider.models) ? provider.models : [])) {
      const mid = String((model && model.id) || '');
      if (!mid) continue;
      routineSelect.appendChild(h('option', { value: pid + '\u0001' + mid },
        String(provider.label || pid) + ' · ' + String(model.label || mid)));
    }
  }
  const routineSaved = (full && full.routine_model) || {};
  if (routineSaved.provider && routineSaved.model) {
    const desired = String(routineSaved.provider) + '\u0001' + String(routineSaved.model);
    routineSelect.value = desired;
    // Каталог мог не ответить или модель уже убрали: выбор комнаты всё равно виден,
    // иначе поле выглядело бы пустым (W29-Q9).
    if (routineSelect.selectedIndex < 0) {
      routineSelect.appendChild(h('option', { value: desired, selected: true },
        String(routineSaved.provider) + ' · ' + String(routineSaved.model) + ' (нет в каталоге)'));
    }
  } else {
    routineSelect.value = '';
  }

  const routineEstimate = (full && full.routine_estimate) || null;
  const routineNote = h('div', { class: 'roomdlg__hint small' },
    (routineEstimate && routineEstimate.text)
      || 'По умолчанию свод и «Уточнение» идут на модели руководителя.');
  routineSelect.addEventListener('change', () => {
    routineNote.textContent = routineSelect.value
      ? 'Выбрана лёгкая модель. Оценка экономии обновится после сохранения — считается по вашим сессиям, без выдуманных цифр.'
      : 'Рутинные карточки вернутся на модель руководителя.';
    markDirty();
  });

  /** Выключили автообновление — настройки частоты не к чему применять. */
  function renderSvodState() {
    const on = Boolean(autoSvodInput.checked);
    quietSelect.disabled = !on;
    gapSelect.disabled = !on;
  }
  autoSvodInput.addEventListener('change', renderSvodState);
  renderSvodState();

  const svodBox = h('div', { class: 'roomdlg__section roomdlg__svod' },
    h('div', { class: 'roomdlg__label' }, 'Живой свод'),
    h('div', { class: 'roomdlg__hint' },
      'Когда появляются ваши ответы, решения или уточнения в диалоге, свод обновляется сам: '
      + 'версия растёт без ваших нажатий, а руководитель получает задание «обновить свод».'),
    h('div', { class: 'row row--wrap' }, autoSvodBox),
    h('div', { class: 'roomdlg__svod-when' },
      h('div', { class: 'roomdlg__field-mini' },
        h('span', { class: 'field__label' }, 'Подождать тишины'),
        quietSelect),
      h('div', { class: 'roomdlg__field-mini' },
        h('span', { class: 'field__label' }, 'И не чаще раза в'),
        gapSelect)),
    h('div', { class: 'roomdlg__field-mini' },
      h('span', { class: 'field__label' }, 'Модель для рутинного сведения'),
      routineSelect,
      routineNote),
    h('div', { class: 'roomdlg__hint small' },
      '«Тишина» — сколько подождать после последнего изменения, чтобы не пересобирать свод '
      + 'после каждого шага. «Не чаще раза в» — потолок частоты: свод не будет обновляться '
      + 'чаще, даже если писать помногу.'),
    h('div', { class: 'roomdlg__hint small muted' },
      'Кнопка «Обновить сейчас» в блоке «Свод» есть всегда — ею можно пересобрать свод, '
      + 'не дожидаясь тишины.'));

  workPanel.appendChild(h('div', { class: 'roomdlg__section' },
    field({ label: 'Критик', control: criticSelect }),
    criticHint,
    h('div', { class: 'roomdlg__field-mini' },
      h('span', { class: 'field__label' }, 'Раунды проверки'),
      h('div', { class: 'roomdlg__critic-row' }, roundsSelect, roundsHelpBtn)),
    h('div', { class: 'row row--wrap' }, criticPassBox),
    capBox,
    roundsHelp,
  ));

  workPanel.appendChild(svodBox);

  /* W23-MG2: «Контроль моделей» — строго / только предупреждать. Простыми
     словами: что произойдёт, если запуск соберётся уйти на другую модель. */
  const guardField = guardModeField({ room: full || {}, idPrefix: 'roomdlg', onChange: () => markDirty() });
  workPanel.appendChild(guardField.el);

  /* ─── «Ход работы» (W20-FB; WAVE18 п.4). Простыми словами: что видно на
     странице задачи, пока команда работает. Лента только читается с диска —
     модели для этого не вызываются. Личное Подробно/Кратко/Выкл.
     есть и в самом блоке: оно действует лишь на экране того, кто нажал. ─── */

  const worklogModeSelect = h('select', {
    class: 'select', id: 'roomdlg-worklog-mode', 'aria-label': 'Что показывать в «Ходе работы»',
  },
  h('option', { value: 'full' }, 'Подробно — с рассуждениями'),
  h('option', { value: 'brief' }, 'Кратко — только действия'),
  h('option', { value: 'off' }, 'Выключено — не показывать'));
  worklogModeSelect.value = ['full', 'brief', 'off'].includes(String(full && full.worklog_mode))
    ? String(full.worklog_mode) : 'full';

  const digestSelect = h('select', {
    class: 'select', id: 'roomdlg-worklog-digest', 'aria-label': 'Промежуточный итог сторожа',
  },
  h('option', { value: '0' }, 'Не делать'),
  h('option', { value: '1' }, 'Каждую минуту'),
  h('option', { value: '2' }, 'Каждые 2 минуты'),
  h('option', { value: '5' }, 'Каждые 5 минут'));
  const digestSaved = full && full.digest_every_min !== undefined ? Number(full.digest_every_min) : 2;
  digestSelect.value = [0, 1, 2, 5].includes(digestSaved) ? String(digestSaved) : '2';

  const worklogHelp = h('div', { class: 'roomdlg__hint roomdlg__hint--help hidden', id: 'roomdlg-worklog-help' },
    h('p', null, 'Пока команда работает, на странице задачи видно, кто чем занят: кто читает файл, '
      + 'кто ищет в сети, кто передал задачу другому. Не нужно ждать готового ответа, чтобы понять, '
      + 'что работа идёт, а не встала.'),
    h('p', null, '«Подробно» — ещё и короткие рассуждения участников, приглушённым курсивом: '
      + 'видно, как они думают. «Кратко» — только действия, без рассуждений. «Выключено» — '
      + 'блок ничего не показывает и даже не читает записи: если хочется спокойного экрана.'),
    h('p', null, 'Лента только читается с диска и ничего не запускает: ни агентов, ни моделей, '
      + 'ни моделей. Переключатель прямо в самом блоке действует только на вашем экране и '
      + 'может сузить эту настройку, но не расширить её.'),
    h('p', null, '«Промежуточный итог» — короткая сводка раз в несколько минут: кто чем занят, '
      + 'сколько готово, когда было последнее событие. Пишет сторож, без модели. Итог приходит '
      + 'и при выключенной ленте — так просили: «сторож работает всегда».'));
  const worklogHelpBtn = h('button', {
    class: 'btn btn--ghost btn--sm btn--icon roomdlg__help', type: 'button',
    'aria-expanded': 'false', 'aria-controls': 'roomdlg-worklog-help',
    title: 'Что такое «Ход работы» и промежуточный итог',
  }, icon('info', { size: 13 }), h('span', { class: 'sr-only' }, 'Что такое ход работы'));
  worklogHelpBtn.addEventListener('click', () => {
    const open = worklogHelp.classList.toggle('hidden') === false;
    worklogHelpBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });

  workPanel.appendChild(h('div', { class: 'roomdlg__section roomdlg__section--worklog' },
    h('div', { class: 'roomdlg__label' }, 'Ход работы'),
    h('div', { class: 'roomdlg__hint' },
      'Что видно на странице задачи, пока команда работает. Только чтение: ничего не запускает, модель не вызывает.'),
    h('div', { class: 'roomdlg__field-mini' },
      h('span', { class: 'field__label' }, 'Показывать ход работы'),
      h('div', { class: 'roomdlg__critic-row' }, worklogModeSelect, worklogHelpBtn)),
    h('div', { class: 'roomdlg__field-mini' },
      h('span', { class: 'field__label' }, 'Промежуточный итог в ленте'),
      digestSelect),
    h('div', { class: 'roomdlg__hint' },
      'Сторож сам коротко пишет, кто чем занят и что готово, — без модели. «Не делать» убирает эти записи.'),
    worklogHelp,
  ));

  /* ──────────────────────────────────────────────────────────── «Файлы» */

  let projectDir = String((full && full.project_dir) || '');
  const pathInput = h('input', {
    class: 'input', type: 'text', autocomplete: 'off', spellcheck: 'false',
    placeholder: 'C:/Проекты/Название проекта',
    'aria-label': 'Папка проекта',
  });
  pathInput.value = projectDir;
  projectDir = null;                       // дальше живём значением поля

  const suggestDirBtn = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' },
    'Предложить');
  const dirCheck = h('div', { class: 'pd-check', 'data-pd': 'check', 'aria-live': 'polite' });

  let checkTimer = null;
  let checkSeq = 0;
  let lastCheck = { tone: '', text: '', warn: '', value: '' };

  function setVerdict(verdict, value) {
    lastCheck = Object.assign({ tone: '', text: '', warn: '', value: '' }, verdict || {}, { value: value || '' });
    dirCheck.className = 'pd-check' + (verdict && verdict.tone && verdict.tone !== 'muted' ? ' pd-check--' + verdict.tone : '');
    dirCheck.replaceChildren();
    if (!verdict || !verdict.text) return;
    dirCheck.appendChild(icon(verdict.tone === 'bad' ? 'error' : (verdict.tone === 'ok' ? 'check' : 'info'),
      { size: 12 }));
    const text = h('span', null, String(verdict.text));
    if (verdict.warn) {
      text.appendChild(h('span', { class: 'pd-check__warn' }, String(verdict.warn)));
    }
    dirCheck.appendChild(text);
  }

  async function runDirCheck() {
    const value = pathInput.value.trim();
    if (!value) { setVerdict(null); return; }
    const seq = ++checkSeq;
    setVerdict({ tone: 'muted', text: 'Проверяю…' }, value);
    try {
      const res = await api.post('/api/rooms/check-dir', { path: value });
      if (seq !== checkSeq) return;                     // пришёл устаревший ответ
      setVerdict(dirVerdict(res), value);
    } catch (err) {
      if (seq !== checkSeq) return;
      // проверка недоступна (демо/старый сервер) — не мешаем сохранять
      setVerdict({ tone: 'muted', text: 'Проверка недоступна — путь проверим при сохранении' }, value);
    }
  }

  function queueDirCheck() {
    if (checkTimer) { clearTimeout(checkTimer); checkTimer = null; }
    const value = pathInput.value.trim();
    if (!value) { setVerdict(null); return; }
    checkTimer = setTimeout(() => { checkTimer = null; runDirCheck(); }, CHECK_DELAY_MS);
  }

  pathInput.addEventListener('input', queueDirCheck);
  pathInput.addEventListener('blur', () => { if (checkTimer) { clearTimeout(checkTimer); checkTimer = null; } runDirCheck(); });
  suggestDirBtn.addEventListener('click', () => {
    pathInput.value = suggestProjectDir(titleInput.value.trim() || String((full && full.title) || ''));
    queueDirCheck();
    syncMoveNote();
    markDirty();
  });

  const dirRow = h('div', { class: 'pd-field__row' }, pathInput, suggestDirBtn);
  if (isEdit && full && full.project_dir) {
    const openFolder = projectDirButton(ctx, { roomId: full.id, projectDir: full.project_dir, showHint: false });
    if (openFolder) dirRow.appendChild(openFolder);
  }
  // W10: смена или удаление папки у существующей комнаты — говорим прямо, что
  // старую папку никто не переносит и не удаляет: файлы остаются на месте.
  const dirMoveNote = h('p', { class: 'pd-note hidden', 'data-pd': 'move-note' }, MOVE_NOTE);
  const prevDir = String((isEdit && full && full.project_dir) || '').trim();
  function syncMoveNote() {
    const now = pathInput.value.trim();
    const changed = Boolean(prevDir) && now !== prevDir;
    dirMoveNote.classList.toggle('hidden', !changed);
  }
  pathInput.addEventListener('input', syncMoveNote);
  const dirBox = h('div', { class: 'pd-field' }, dirRow, dirCheck, dirMoveNote);
  syncMoveNote();

  const filesHost = h('div', { class: 'roomdlg__files' });
  const fileInput = h('input', {
    class: 'input', type: 'text', autocomplete: 'off',
    placeholder: 'C:/Проекты/Комната/STATE.md', 'aria-label': 'Путь к файлу контекста',
  });

  function renderFiles() {
    filesHost.replaceChildren();
    if (!files.length) {
      filesHost.appendChild(h('span', { class: 'roomdlg__files-empty' }, 'Файлов пока нет — агенты прочитают только текст задачи.'));
      return;
    }
    for (const path of files) {
      filesHost.appendChild(h('span', { class: 'roomdlg__file' },
        pathChip(path),
        iconButton({
          icon: 'close', size: 'sm', title: 'Убрать «' + path + '»',
          onclick: () => { const i = files.indexOf(path); if (i >= 0) files.splice(i, 1); renderFiles(); markDirty(); },
        }),
      ));
    }
  }

  function addFile(raw) {
    const path = String(raw || '').trim();
    if (!path) return;
    if (files.includes(path)) { toast('Этот путь уже в списке', 'info'); return; }
    if (files.length >= 20) { toast('Не больше 20 файлов контекста', 'warn'); return; }
    files.push(path);
    fileInput.value = '';
    renderFiles();
    markDirty();
  }

  const addFileBtn = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Добавить путь');
  addFileBtn.addEventListener('click', () => addFile(fileInput.value));
  fileInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); addFile(fileInput.value); }
  });
  renderFiles();

  filesPanel.appendChild(h('div', { class: 'roomdlg__section' },
    field({
      label: 'Папка проекта',
      control: dirBox,
      hint: 'Сюда команда будет складывать отчёты и материалы по этой комнате. '
        + 'Не обязательно — можно выбрать позже.',
    }),
  ));
  filesPanel.appendChild(h('div', { class: 'roomdlg__section' },
    h('div', { class: 'roomdlg__label' }, 'Файлы контекста'),
    h('div', { class: 'roomdlg__hint' }, 'Агенты читают их первыми: STATE.md, брифы, договоры.'),
    filesHost,
    h('div', { class: 'row roomdlg__file-row' }, fileInput, addFileBtn),
  ));

  /* ─────────────────────────────────────────────────────────── «Проект» */

  const projectModeInput = h('input', { type: 'checkbox', id: 'roomdlg-project-mode' });
  projectModeInput.checked = projectMode;
  const projectModeBox = h('label', { class: 'checkbox' }, projectModeInput,
    h('span', null, 'Руководитель делит большую задачу на части работы'));

  const PROJECT_MODE_HINT =
    'Проектный режим: большую задачу руководитель делит на несколько частей '
    + '(например: «юридическая часть», «закупки», «упаковка»). Каждая часть — '
    + 'отдельная задача со своим ходом работы и своим исполнителем. Включайте, '
    + 'если одна задача — это целый проект; выключен — руководитель отвечает '
    + 'одним общим ответом.';
  // B-33: «Сейчас: …» — заметной плашкой, а не строчкой мелкого текста.
  const projectModeState = h('div', {
    class: 'roomdlg__hint roomdlg__state', role: 'status', 'aria-live': 'polite', 'data-state': 'off',
  });

  // Один термин вместо «направления» (B-10): «часть работы» — везде одинаково.
  const LAUNCH_CHOICES = [
    ['auto', 'Сразу, без подтверждения',
      'Команда начинает работу, как только руководитель разделил задачу.'],
    ['plan_first', 'Сначала показать план — я запускаю сам кнопкой «Запустить»',
      'Вы увидите список частей и решите, что запускать.'],
  ];
  const launchHost = h('div', {
    class: 'roomdlg__launch', role: 'radiogroup', 'aria-label': 'Как запускать части работы',
  });
  const launchInputs = [];
  for (const [value, text, hint] of LAUNCH_CHOICES) {
    const input = h('input', {
      type: 'radio', name: 'roomdlg-launch', value, id: 'roomdlg-launch-' + value,
    });
    input.checked = launchMode === value;
    input.addEventListener('change', () => { if (input.checked) { launchMode = value; renderModeState(); markDirty(); } });
    launchInputs.push(input);
    // обёртка-блок: варианты стоят друг под другом и не слипаются в строку
    launchHost.appendChild(h('div', { class: 'roomdlg__choice' },
      h('label', { class: 'checkbox' }, input, h('span', null, text)),
      h('div', { class: 'roomdlg__choice-hint' }, hint)));
  }

  const streamsSelect = h('select', { class: 'select', 'aria-label': 'Одновременно частей работы' });
  for (let n = 1; n <= 4; n += 1) {
    streamsSelect.appendChild(h('option', { value: String(n) }, countNoun(n, STREAM_FORMS)));
  }
  streamsSelect.value = String(maxStreams);
  streamsSelect.addEventListener('change', () => { maxStreams = Number(streamsSelect.value) || 2; markDirty(); });

  const modeCostHint = h('div', { class: 'roomdlg__hint' },
    'Больше частей одновременно — быстрее работа, но больше расходов и нагрузки на модели. '
    + 'Остальные ждут своей очереди.');

  /* Этапы проекта — свойство КОМНАТЫ (W17-R §1, B-8): своих шаблонов нет.
     W20 §1.7 (F-2): редактор тот же, что на карте проекта — добавить,
     переименовать, переставить стрелками, убрать и добавить этапы из
     предложения руководителя. Правки уходят этим же PATCH комнаты. */
  let phases = Array.isArray(full && full.phases)
    ? full.phases.map((p) => ({ id: String((p && p.id) || ''), title: String((p && p.title) || '') }))
        .filter((p) => p.title)
    : [];

  const phasesEditor = createPhasesEditor({
    phases,
    proposal: full && full.phases_proposal,
    toast,
    addLabel: 'Добавить этап',
    onChange: (list) => {
      phases = list;
      // initialSig появляется ниже: до него «грязным» окно не считаем.
      if (initialSig) markDirty();
    },
  });
  const addPhaseBtn = phasesEditor.addButton;
  const phasesHint = h('div', { class: 'roomdlg__hint' },
    'Необязательно. Этапы видны на вкладке «Карта проекта»: текущий этап, прогресс и '
    + 'решения по нему. Порядок меняется стрелками, этап можно переименовать или убрать. '
    + 'Предложить этапы может и руководитель — кнопкой «Предложить этапы» на карте.');

  /** Список этапов для PATCH: id сохраняется, пустые строки не отправляем. */
  function phasesPayload() {
    return phasesEditor.getPhases();
  }

  function renderModeState() {
    const on = Boolean(projectModeInput.checked);
    projectMode = on;
    for (const input of launchInputs) input.disabled = !on;
    streamsSelect.disabled = !on;
    modeCostHint.classList.toggle('hidden', !on);
    launchHost.classList.toggle('roomdlg__launch--off', !on);
    // Состояние словами (B-10, B-33): что человек увидит на вкладке «Карта проекта».
    projectModeState.dataset.state = on ? 'on' : 'off';
    projectModeState.replaceChildren(
      icon(on ? 'pass' : 'circle-slash', { size: 13 }),
      h('b', { class: 'roomdlg__state-word' }, 'Сейчас: ' + (on ? 'включён' : 'выключен')),
      h('span', null, on
        ? ' — на вкладке «Карта проекта» будут части работы, этапы и прогресс.'
        : ' — руководитель ответит одним общим ответом, карты частей не будет. Включить можно в любой момент.'),
    );
  }
  projectModeInput.addEventListener('change', () => { renderModeState(); markDirty(); });

  projectPanel.appendChild(h('div', { class: 'roomdlg__label' }, 'Проектный режим'));
  projectPanel.appendChild(h('div', { class: 'roomdlg__hint' }, PROJECT_MODE_HINT));
  projectPanel.appendChild(projectModeBox);
  projectPanel.appendChild(projectModeState);
  projectPanel.appendChild(field({ label: 'Как запускать части работы', control: launchHost }));
  projectPanel.appendChild(h('div', { class: 'roomdlg__row' },
    field({ label: 'Одновременно частей работы', control: streamsSelect }),
    h('div', { class: 'roomdlg__section' }, modeCostHint),
  ));
  projectPanel.appendChild(h('div', { class: 'roomdlg__section roomdlg__section--phases' },
    h('div', { class: 'roomdlg__label' }, 'Этапы проекта'),
    phasesHint,
    phasesEditor.el,
  ));

  /* ──────────────────────────────────────────── подсказки и состав */

  function profileById(name) {
    return profiles.find((p) => profileName(p) === name) || null;
  }

  function renderHints() {
    const d = profileById(directorSelect.value);
    directorHint.textContent = d && d.description
      ? String(d.description)
      : 'Руководитель комнаты разложит задачу по команде и сведёт один ответ.';
    const c = profileById(criticSelect.value);
    criticHint.textContent = criticSelect.value
      ? (c && c.description ? String(c.description) : 'Критик проверит свод перед ответом.')
      : 'Без критика: ответ придёт сразу после свода, без проверки.';
    const hasCritic = Boolean(criticSelect.value);
    criticPassBox.classList.toggle('hidden', !hasCritic);
    roundsSelect.disabled = !hasCritic;
    roundsHelpBtn.disabled = !hasCritic;
    capInput.disabled = !hasCritic;
    capBox.classList.toggle('hidden', !hasCritic || modeValue() !== 'until_approved');
    criticPassInput.disabled = !hasCritic;
    // Галочка «проверять критиком» не может быть включена, когда критика нет.
    if (!hasCritic) criticPassInput.checked = false;
    else if (!criticPassTouched) criticPassInput.checked = true;
  }

  directorSelect.addEventListener('change', () => { renderHints(); renderMembers(); markDirty(); });
  criticSelect.addEventListener('change', () => { renderHints(); renderMembers(); markDirty(); });

  /* ──────────────────────────────────────────── отправка и защита (B-31) */

  const cancelBtn = h('button', { class: 'btn btn--secondary', type: 'button' }, 'Отмена');
  const submitLabel = isEdit ? 'Сохранить' : 'Создать комнату';
  const submitBtn = h('button', { class: 'btn btn--primary', type: 'submit', form: formId },
    icon(isEdit ? 'check' : 'add', { size: 14 }), h('span', null, submitLabel));
  const footNote = h('span', { class: 'roomdlg__foot-note' }, '');

  // Плашка «есть несохранённое» — вместо молчаливого закрытия.
  const unsavedSave = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, 'Сохранить изменения');
  const unsavedDrop = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Закрыть без сохранения');
  const unsavedBack = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'Вернуться');
  // role ставим только когда плашка видна: скрытый постоянный «alert» ловят
  // и скринридер, и тесты, которые ищут настоящую ошибку.
  const unsavedBar = h('div', { class: 'roomdlg__unsaved hidden', 'data-test': 'unsaved' },
    icon('warning', { size: 14 }),
    h('span', { class: 'roomdlg__unsaved-text' }, 'Есть несохранённые изменения.'),
    h('div', { class: 'roomdlg__unsaved-actions' }, unsavedSave, unsavedDrop, unsavedBack));

  // Плашка «введённое восстановили» — после перезагрузки/случайного закрытия.
  const restoredRevert = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'Отменить восстановление');
  const restoredBar = h('div', { class: 'roomdlg__restored hidden', role: 'status', 'data-test': 'restored' },
    icon('history', { size: 14 }),
    h('span', { class: 'roomdlg__restored-text' },
      'Введённое восстановлено: окно закрылось до сохранения. Проверьте и сохраните.'),
    restoredRevert);

  body.insertBefore(restoredBar, form);
  foot.appendChild(footNote);
  foot.appendChild(cancelBtn);
  foot.appendChild(submitBtn);
  dialog.appendChild(unsavedBar);
  dialog.appendChild(foot);

  function showError(message) {
    errBanner.replaceChildren();
    if (!message) return;
    errBanner.appendChild(h('div', { class: 'banner banner--error', role: 'alert' },
      icon('error', { size: 14 }), h('div', { class: 'banner__text' }, humanApiMessage(message))));
  }

  /* ── состояние формы: одно место, откуда берутся и «грязно», и черновик ── */

  function formState() {
    return {
      title: String(titleInput.value || ''),
      icon: iconName,
      orchestrator: String(directorSelect.value || ''),
      critic: String(criticSelect.value || ''),
      members: Array.from(selectedMembers)
        .filter((p) => p !== directorSelect.value && p !== criticSelect.value)
        .sort(),
      criticPass: Boolean(criticPassInput.checked),
      rounds: String(roundsSelect.value || '1'),
      cap: String(capInput.value || '12'),
      projectMode: Boolean(projectModeInput.checked),
      launchMode: String(launchMode || 'plan_first'),
      streams: String(streamsSelect.value || '2'),
      projectDir: String(pathInput.value || '').trim(),
      files: files.slice(),
      phases: phases.map((p) => ({ id: String(p.id || ''), title: String(p.title || '') })),
      worklogMode: String(worklogModeSelect.value || 'full'),
      worklogDigest: String(digestSelect.value || '2'),
      modelGuard: guardField.value(),
    };
  }

  let initialSig = '';
  let dirty = false;
  let pendingAction = null;               // null | 'close' | '#/room/<id>/team'

  function markDirty() {
    const sig = JSON.stringify(formState());
    dirty = sig !== initialSig;
    footNote.textContent = dirty ? 'Есть несохранённые изменения' : '';
    footNote.classList.toggle('roomdlg__foot-note--dirty', dirty);
    if (dirty) writeDraft(key, formState());
    else clearDraft(key);
  }

  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);

  /* ── восстановление введённого (B-31) ─────────────────────────────────── */

  function applyState(state) {
    if (!state || typeof state !== 'object') return;
    if (state.title !== undefined) titleInput.value = String(state.title || '');
    if (state.icon && ICON_NAMES.has(String(state.icon))) { iconName = String(state.icon); renderIcons(); }
    if (state.orchestrator) {
      directorSelect.value = String(state.orchestrator);
      if (directorSelect.value !== String(state.orchestrator)) {
        directorSelect.appendChild(h('option', { value: String(state.orchestrator) }, String(state.orchestrator)));
        directorSelect.value = String(state.orchestrator);
      }
    }
    if (state.critic !== undefined) {
      criticSelect.value = String(state.critic || '');
      if (String(state.critic || '') && criticSelect.value !== String(state.critic)) {
        criticSelect.appendChild(h('option', { value: String(state.critic) }, String(state.critic)));
        criticSelect.value = String(state.critic);
      }
    }
    if (Array.isArray(state.members)) {
      selectedMembers.clear();
      for (const p of state.members) if (p) selectedMembers.add(String(p));
    }
    if (state.criticPass !== undefined) {
      criticPassInput.checked = Boolean(state.criticPass);
      criticPassTouched = true;
    }
    if (state.rounds) roundsSelect.value = String(state.rounds);
    if (state.cap) capInput.value = String(state.cap);
    if (state.projectMode !== undefined) projectModeInput.checked = Boolean(state.projectMode);
    if (state.launchMode === 'auto' || state.launchMode === 'plan_first') {
      launchMode = state.launchMode;
      for (const input of launchInputs) input.checked = input.value === launchMode;
    }
    if (state.streams) { streamsSelect.value = String(state.streams); maxStreams = Number(state.streams) || 2; }
    if (state.worklogMode) worklogModeSelect.value = String(state.worklogMode);
    if (state.worklogDigest) digestSelect.value = String(state.worklogDigest);
    if (state.modelGuard) guardField.set(String(state.modelGuard));
    if (state.projectDir !== undefined) { pathInput.value = String(state.projectDir || ''); syncMoveNote(); }
    if (Array.isArray(state.files)) { files.splice(0, files.length); for (const f of state.files) files.push(String(f)); }
    if (Array.isArray(state.phases)) {
      phases = state.phases.map((p) => ({ id: String((p && p.id) || ''), title: String((p && p.title) || '') }))
        .filter((p) => p.title);
      phasesEditor.setPhases(phases);
    }
    renderHints();
    renderMembers();
    renderModeState();
    renderFiles();
    renderRoundsState();
  }

  /* ── закрытие: сначала спросить, потом закрывать ─────────────────────── */

  const prevFocus = document.activeElement;
  let closed = false;
  let saving = false;
  let resolveDone = () => {};
  const done = new Promise((resolve) => { resolveDone = resolve; });

  function goTo(hash) {
    if (!hash) return;
    navigate(hash);
  }

  /** Закрыть по-настоящему: только после решения про несохранённое. */
  function close(result) {
    if (closed) return;
    closed = true;
    if (checkTimer) { clearTimeout(checkTimer); checkTimer = null; }
    document.removeEventListener('keydown', onKey, true);
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    if (prevFocus && typeof prevFocus.focus === 'function') {
      try { prevFocus.focus(); } catch { /* узел мог исчезнуть */ }
    }
    resolveDone(result === undefined ? null : result);
  }

  /**
   * Попытка закрыть окно. С несохранёнными изменениями ничего не теряем:
   * показываем выбор, окно остаётся на месте.
   * @param {string} [thenHash] куда перейти после закрытия (карточка команды)
   */
  function requestClose(thenHash) {
    if (closed || saving) return;
    if (!dirty) {
      clearDraft(key);
      close();
      goTo(thenHash);
      return;
    }
    pendingAction = thenHash || 'close';
    unsavedBar.setAttribute('role', 'alert');
    unsavedBar.classList.remove('hidden');
    unsavedBar.setAttribute('data-pending', pendingAction);
    if (typeof unsavedSave.focus === 'function') unsavedSave.focus();
  }

  function dropChanges() {
    clearDraft(key);
    const thenHash = pendingAction && pendingAction !== 'close' ? pendingAction : '';
    pendingAction = null;
    unsavedBar.classList.add('hidden');
    unsavedBar.removeAttribute('role');
    close();
    goTo(thenHash);
  }

  function stayInDialog() {
    pendingAction = null;
    unsavedBar.classList.add('hidden');
    unsavedBar.removeAttribute('role');
    if (typeof titleInput.focus === 'function') titleInput.focus();
  }

  unsavedBack.addEventListener('click', stayInDialog);
  unsavedDrop.addEventListener('click', dropChanges);
  unsavedSave.addEventListener('click', () => { save(); });

  function hasNestedDialog() {
    if (typeof document === 'undefined' || !document.querySelectorAll) return false;
    const all = Array.from(document.querySelectorAll('.dialog-backdrop'));
    return Boolean(all.length) && all[all.length - 1] !== overlay;
  }

  function onKey(ev) {
    if (ev.key === 'Escape') {
      // Esc, который принадлежит вложенному окну (блокнот, подтверждение) или
      // выпадающему списку <select>, не должен закрывать настройки: именно так
      // окно раньше «вылетало» посреди работы.
      if (ev.defaultPrevented) return;
      const tag = String((ev.target && ev.target.tagName) || '').toLowerCase();
      if (tag === 'select') return;
      if (hasNestedDialog()) return;
      ev.preventDefault();
      ev.stopPropagation();
      requestClose();
      return;
    }
    if (ev.key !== 'Tab') return;
    // Фокус-ловушка только по видимому: скрытые вкладки и недоступные поля — мимо.
    if (typeof dialog.querySelectorAll !== 'function') return;
    const items = Array.from(dialog.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((node) => node !== dialog && node.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }

  overlay.addEventListener('mousedown', (ev) => { if (ev.target === overlay) requestClose(); });
  document.addEventListener('keydown', onKey, true);

  /* ─────────────────────────────────────────────────────────── сохранение */

  function validate() {
    const title = titleInput.value.trim();
    if (title.length < 2) return { tab: 'main', field: titleInput, message: 'Название: минимум 2 знака.' };
    if (title.length > 80) return { tab: 'main', field: titleInput, message: 'Название: не больше 80 знаков.' };
    if (!directorSelect.value) return { tab: 'main', field: directorSelect, message: 'Выберите руководителя комнаты — он раздаёт работу.' };
    if (criticSelect.value && criticSelect.value === directorSelect.value) {
      return { tab: 'work', field: criticSelect, message: 'Критик и руководитель комнаты должны быть разными профилями.' };
    }
    if (!Array.from(selectedMembers).some((p) => p !== directorSelect.value && p !== criticSelect.value)) {
      return { tab: 'members', field: membersHost, message: 'Отметьте хотя бы одного исполнителя в разделе «Состав».' };
    }
    if (!Number.isInteger(maxStreams) || maxStreams < 1 || maxStreams > 4) {
      return { tab: 'project', field: streamsSelect, message: 'Одновременно частей работы: от 1 до 4.' };
    }
    for (const path of files) {
      if (!path.trim()) return { tab: 'files', field: fileInput, message: 'Пустой путь в файлах контекста.' };
    }
    const dir = pathInput.value.trim();
    if (dir && lastCheck.tone === 'bad' && lastCheck.value === dir) {
      return { tab: 'files', field: pathInput, message: lastCheck.text };
    }
    return null;
  }

  async function save() {
    if (saving || closed) return null;
    const bad = validate();
    if (bad) {
      showTab(bad.tab);
      showError(bad.message);
      if (bad.field && typeof bad.field.focus === 'function') bad.field.focus();
      return null;
    }
    showError('');
    saving = true;
    submitBtn.disabled = true;
    submitBtn.replaceChildren(icon('loading', { size: 14, className: 'codicon-modifier-spin' }), h('span', null, 'Сохраняю…'));
    footNote.textContent = '';

    const membersPayload = Array.from(selectedMembers)
      .filter((p) => p !== directorSelect.value && p !== criticSelect.value)
      .map((profile) => ({ profile, role: 'worker' }));

    const payload = {
      title: titleInput.value.trim(),
      icon: iconName,
      orchestrator: directorSelect.value,
      critic: criticSelect.value || null,
      context_files: files.slice(),
      project_dir: pathInput.value.trim(),
      critic_pass: Boolean(criticSelect.value) && criticPassInput.checked,
      max_rounds: Number(roundsSelect.value) || 1,
      // Режим критика (W13-E §7, §9): 1 / 2 раунда либо «до полного одобрения».
      critic_mode: modeValue(),
      critic_round_cap: Math.min(30, Math.max(2, Number(capInput.value) || 12)),
      // Проектный режим этой комнаты (A1/A2, B-33): сохраняется этим же PATCH.
      project_mode: Boolean(projectModeInput.checked),
      launch_mode: launchMode === 'auto' ? 'auto' : 'plan_first',
      max_parallel_streams: Number.isInteger(maxStreams) && maxStreams >= 1 && maxStreams <= 4 ? maxStreams : 2,
      // Этапы проекта (W17-R §1): у комнаты свои, пустой список — «этапов нет».
      phases: phasesPayload(),
      // W20-FB: «Ход работы» — уровень ленты и частота промежуточного итога (0…5).
      worklog_mode: ['full', 'brief', 'off'].includes(String(worklogModeSelect.value))
        ? String(worklogModeSelect.value) : 'full',
      digest_every_min: Number(digestSelect.value) || 0,
      // W20 §1.1 (B-36): живой свод — автообновление, «тишина» и потолок частоты.
      auto_svod: Boolean(autoSvodInput.checked),
      auto_svod_quiet_s: Number(quietSelect.value) || 180,
      auto_svod_min_gap_s: Number(gapSelect.value) || 600,
      // W29-Q9: модель для рутинного сведения; пусто — «как у руководителя».
      routine_model: (() => {
        const raw = String(routineSelect.value || '');
        if (!raw) return {};
        const cut = raw.indexOf('\u0001');
        if (cut <= 0) return {};
        return { provider: raw.slice(0, cut), model: raw.slice(cut + 1) };
      })(),
      // W23-MG2: контроль моделей — «строго» (по умолчанию) или «только предупреждать».
      model_guard: guardField.value(),
    };

    const projectWas = full && full.project_mode !== undefined ? Boolean(full.project_mode) : true;
    const projectNow = Boolean(projectModeInput.checked);

    try {
      if (!isEdit) {
        const created = await api.post('/api/rooms', Object.assign({}, payload, {
          board: BOARD,
          members: membersPayload,
          // Модели и «Особенности» настраиваются в карточке команды (B-32).
          agent_notes: {},
          agent_models: {},
        }));
        await refreshRooms(api, store).catch(() => {});
        const id = created && (created.id || (created.room && created.room.id));
        clearDraft(key);
        close(created || { id });
        toast('Комната создана', 'ok');
        if (id) navigate('#/room/' + enc(id) + '/composer');
        else navigate('#/');
        return created || null;
      }

      const id = String(full.id);
      const updated = await api.patch('/api/rooms/' + enc(id), payload);
      const before = new Set(membersOf(full).filter((m) => String(m.role || 'worker') !== 'critic').map((m) => String(m.profile)));
      const after = new Set(selectedMembers);
      const problems = [];
      for (const profile of after) {
        if (before.has(profile)) continue;
        try { await api.post('/api/rooms/' + enc(id) + '/members', { profile, role: 'worker' }); }
        catch (err) { problems.push(profile + ': ' + (err && err.message ? err.message : 'не добавлен')); }
      }
      for (const profile of before) {
        if (after.has(profile)) continue;
        try { await api.del('/api/rooms/' + enc(id) + '/members/' + enc(profile)); }
        catch (err) { problems.push(profile + ': ' + (err && err.message ? err.message : 'не убран')); }
      }
      await refreshRooms(api, store).catch(() => {});
      clearDraft(key);
      // Карта проекта перечитает комнату сразу: этапы — её свойство (W20-FC §1.7).
      notifyRoomSaved(id, updated);
      const thenHash = pendingAction && pendingAction !== 'close' ? pendingAction : '';
      pendingAction = null;
      close(updated || full);
      if (projectNow !== projectWas) {
        toast(projectNow
          ? 'Проектный режим включён — части работы появятся на карте проекта'
          : 'Проектный режим выключен — руководитель отвечает одним ответом',
        'ok');
      }
      toast(problems.length ? 'Комната сохранена, но не всё удалось обновить' : 'Комната сохранена',
        problems.length ? 'warn' : 'ok');
      if (problems.length) toast(problems.join('; '), 'warn');
      if (typeof ctx.refresh === 'function') ctx.refresh();
      goTo(thenHash);
      return updated || null;
    } catch (err) {
      const msg = humanApiMessage(err && err.message ? err.message : 'Не удалось сохранить комнату');
      showError(msg);
      toast(msg, 'danger');
      return null;
    } finally {
      saving = false;
      submitBtn.disabled = false;
      submitBtn.replaceChildren(icon(isEdit ? 'check' : 'add', { size: 14 }), h('span', null, submitLabel));
    }
  }

  form.addEventListener('submit', (ev) => { ev.preventDefault(); save(); });
  cancelBtn.addEventListener('click', () => requestClose());

  /* ── первичная отрисовка, черновик и фокус ───────────────────────────── */

  renderHints();
  renderMembers();
  renderModeState();
  renderRoundsState();
  renderFiles();
  showTab(FOCUS_TAB[String((opts && opts.focus) || '')] || 'main');
  initialSig = JSON.stringify(formState());
  // Что сохранено в комнате — нужно, чтобы «Отменить восстановление» вернуло
  // именно сохранённое, а не приняло черновик.
  const savedState = formState();

  const draft = readDraft(key);
  if (draft && JSON.stringify(draft.state) !== initialSig) {
    applyState(draft.state);
    restoredBar.classList.remove('hidden');
    restoredRevert.addEventListener('click', () => {
      clearDraft(key);
      applyState(savedState);
      restoredBar.classList.add('hidden');
      markDirty();
      if (typeof titleInput.focus === 'function') titleInput.focus();
    });
  }
  markDirty();

  if (isEdit) {
    const removeBtn = h('button', { class: 'btn btn--danger btn--sm', type: 'button' }, 'В архив');
    removeBtn.addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: 'Архивировать комнату?',
        text: '«' + String(full.title || full.id) + '» скроется из списка. Задачи и файлы останутся.',
        confirmLabel: 'В архив', danger: true,
      });
      if (!ok) return;
      try {
        await api.post('/api/rooms/' + enc(full.id) + '/archive', {});
        await refreshRooms(api, store).catch(() => {});
        clearDraft(key);
        close();
        toast('Комната в архиве', 'ok');
        navigate('#/');
      } catch (err) {
        showError(err && err.message ? err.message : 'Не удалось архивировать комнату');
      }
    });
    foot.insertBefore(removeBtn, cancelBtn);
  }

  // Узкое окно: выдвижное меню закрываем — диалог всегда один на экране.
  const appRoot = document.getElementById('app');
  if (appRoot && appRoot.classList.contains('nav-open')) appRoot.classList.remove('nav-open');

  // Окно сначала в документе, потом фокус: у узла вне документа focus() в браузере
  // ничего не делает (именно на этом ломался переход «Задать папку» → поле пути).
  document.body.appendChild(overlay);

  // W10: «Задать папку» из шапки открывает тот же диалог, но с курсором в поле
  // «Папка проекта» (вкладка «Файлы»). B-9: «Включить проектный режим» на карте
  // открывает НАСТРОЙКИ ЭТОЙ комнаты на вкладке «Проект».
  const focusTarget = String((opts && opts.focus) || '');
  if (focusTarget === 'project_mode') {
    if (typeof projectModeInput.focus === 'function') projectModeInput.focus();
  } else if (focusTarget === 'phases') {
    if (typeof addPhaseBtn.focus === 'function') addPhaseBtn.focus();
  } else if (focusTarget === 'project_dir') {
    pathInput.focus();
    try { pathInput.setSelectionRange(pathInput.value.length, pathInput.value.length); } catch { /* ignore */ }
  } else if (focusTarget === 'members' || focusTarget === 'work' || focusTarget === 'project'
    || focusTarget === 'autonomy') {
    const first = panels.get(focusTarget).querySelector
      ? panels.get(focusTarget).querySelector('input, select, button')
      : null;
    if (first && typeof first.focus === 'function') first.focus();
  } else {
    titleInput.focus();
    try { titleInput.setSelectionRange(titleInput.value.length, titleInput.value.length); } catch { /* ignore */ }
  }

  // Промис закрывается вместе с диалогом: созданная/изменённая комната или null.
  return done;
}

export default { openRoomDialog, refreshRooms, suggestProjectDir, dirVerdict };
