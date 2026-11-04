/**
 * views/room-dialog.js — создание и редактирование комнаты.
 *
 *   import { openRoomDialog } from './views/room-dialog.js';
 *   await openRoomDialog(ctx, room | null);   // null — новая комната
 *
 * POST /api/rooms или PATCH /api/rooms/{id} (+ участники через
 * POST/DELETE /api/rooms/{id}/members). После успеха обновляет store.rooms
 * и (для новой комнаты) уводит на её вкладку «Запрос».
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import {
  ensureCss, field, iconButton, pathChip, confirmDialog,
} from '../ui.js';
import { displayName, profileOptionLabel, humanApiMessage } from '../shell-labels.js';
import { countNoun } from '../text-ru.js';
import {
  projectDirButton, CHECK_DELAY_MS, dirVerdict, suggestProjectDir, MOVE_NOTE,
} from '../project-dir-button.js';
// Особые указания участникам этой комнаты (WAVE9 §9): одна точка монтирования.
import { mountAgentNotes, notesOf } from '../agent-notes-ui.js';
// Модель, провайдер и мышление участников (WAVE10): одна точка монтирования.
import { mountAgentModel, normalizeReasoning } from '../agent-model-ui.js';

const BOARD = 'team';

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

/**
 * Диалог комнаты.
 * @param {object} ctx  контекст вьюхи ({api, store, navigate, toast, refresh})
 * @param {object|null} [room]  комната для правки; null/undefined — создание
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
  let full = room || null;
  if (isEdit && (full.critic_pass === undefined || !Array.isArray(full.members))) {
    try { full = Object.assign({}, full, await api.get('/api/rooms/' + enc(room.id))); } catch { /* остаёмся на сводке */ }
  }

  let profiles = [];
  try {
    const data = await api.get('/api/profiles');
    profiles = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
  } catch (err) {
    toast(err && err.message ? err.message : 'Список профилей недоступен', 'danger');
  }

  // Каталог моделей (только настроенные провайдеры). Нет ответа — работаем по
  // настройкам агента: подписи моделей возьмутся из данных комнаты.
  let catalogModel = null;
  try { catalogModel = await api.get('/api/models'); } catch { catalogModel = null; }

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
  let launchMode = String((full && full.launch_mode) || 'plan_first');
  if (launchMode !== 'plan_first' && launchMode !== 'auto') launchMode = 'plan_first';
  let maxStreams = Number(full && full.max_parallel_streams);
  if (!Number.isInteger(maxStreams) || maxStreams < 1 || maxStreams > 4) maxStreams = 2;
  const STREAM_FORMS = ['направление', 'направления', 'направлений'];

  /* ──────────────────────────────────────────────────────────────── каркас */

  const titleId = 'roomdlg-title';
  const dialog = h('div', {
    class: 'dialog dialog--wide roomdlg-shell', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
  });
  const head = h('div', { class: 'dialog__head' },
    h('h2', { class: 'dialog__title', id: titleId }, isEdit ? 'Комната: ' + String(full.title || '') : 'Новая комната'),
    iconButton({ icon: 'close', size: 'sm', title: 'Закрыть', onclick: () => close() }),
  );
  const body = h('div', { class: 'dialog__body' });
  const foot = h('div', { class: 'dialog__foot' });
  dialog.appendChild(head); dialog.appendChild(body); dialog.appendChild(foot);

  const overlay = h('div', { class: 'dialog-backdrop' }, dialog);

  const formId = 'roomdlg-form';
  const form = h('form', { class: 'roomdlg', id: formId, novalidate: true });
  body.appendChild(form);

  const errBanner = h('div', { class: 'roomdlg__error' });

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
      btn.addEventListener('click', () => { iconName = item.name; renderIcons(); });
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
      // Без дубля «default · default»: если подпись — это тот же id, она не нужна (D8).
      sel.appendChild(h('option', { value: name, title: name }, profileOptionLabel(p)));
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

  const membersHost = h('div', { class: 'roomdlg__members', role: 'group', 'aria-label': 'Состав комнаты' });
  // Интерфейсы указаний и модели монтируются ниже одной точкой; здесь только ссылки.
  let notesUi = null;
  let modelUi = null;

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

  const criticPassInput = h('input', { type: 'checkbox', id: 'roomdlg-critic-pass' });
  criticPassInput.checked = full && full.critic_pass !== undefined ? Boolean(full.critic_pass) : true;
  // Пользователь сам снял галочку — уважаем выбор; иначе она следует за критиком (D3/D16).
  let criticPassTouched = false;
  criticPassInput.addEventListener('change', () => { criticPassTouched = true; });
  const criticPassBox = h('label', { class: 'checkbox' }, criticPassInput, h('span', null, 'Проверять ответ критиком по умолчанию'));

  const roundsSelect = h('select', { class: 'select', 'aria-label': 'Раундов проверки критика' },
    h('option', { value: '1' }, '1 раунд'),
    h('option', { value: '2' }, '2 раунда'),
  );
  roundsSelect.value = String(Math.min(2, Math.max(1, Number((full && full.max_rounds) || 1))));

  const boardInput = h('input', {
    class: 'input', type: 'text', value: BOARD, disabled: true,
    'aria-label': 'Доска задач', title: 'Доска задач одна на все комнаты',
  });

  /* ── папка проекта: необязательное поле с живой проверкой пути ─────────── */

  let projectDir = String((full && full.project_dir) || '');
  const pathInput = h('input', {
    class: 'input', type: 'text', autocomplete: 'off', spellcheck: 'false',
    placeholder: 'D:\\Проекты\\Название проекта',
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
    placeholder: 'D:/Проекты/Комната/STATE.md', 'aria-label': 'Путь к файлу контекста',
  });

  function renderFiles() {
    filesHost.replaceChildren();
    if (!files.length) {
      filesHost.appendChild(h('span', { class: 'roomdlg__files-empty' }, 'Файлов пока нет — агенты прочитают только текст запроса.'));
      return;
    }
    for (const path of files) {
      filesHost.appendChild(h('span', { class: 'roomdlg__file' },
        pathChip(path),
        iconButton({
          icon: 'close', size: 'sm', title: 'Убрать «' + path + '»',
          onclick: () => { const i = files.indexOf(path); if (i >= 0) files.splice(i, 1); renderFiles(); },
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
  }

  const addFileBtn = h('button', { class: 'btn btn--secondary btn--sm', type: 'button' }, 'Добавить путь');
  addFileBtn.addEventListener('click', () => addFile(fileInput.value));
  fileInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); addFile(fileInput.value); }
  });
  renderFiles();

  /* ── проектный режим: один главный запрос → карта направлений ──────────── */

  const projectModeInput = h('input', { type: 'checkbox', id: 'roomdlg-project-mode' });
  projectModeInput.checked = projectMode;
  const projectModeBox = h('label', { class: 'checkbox' }, projectModeInput,
    h('span', null, 'Руководитель сам строит карту проекта из главного запроса'));

  const LAUNCH_CHOICES = [
    ['plan_first', 'Сначала план: направления ждут кнопки «Запустить». Рекомендуем'],
    ['auto', 'Сразу, без подтверждения'],
  ];
  const launchHost = h('div', {
    class: 'roomdlg__launch', role: 'radiogroup', 'aria-label': 'Как запускать направления',
  });
  const launchInputs = [];
  for (const [value, text] of LAUNCH_CHOICES) {
    const input = h('input', {
      type: 'radio', name: 'roomdlg-launch', value, id: 'roomdlg-launch-' + value,
    });
    input.checked = launchMode === value;
    input.addEventListener('change', () => { if (input.checked) { launchMode = value; renderModeState(); } });
    launchInputs.push(input);
    // обёртка-блок: варианты стоят друг под другом и не слипаются в строку
    launchHost.appendChild(h('div', null, h('label', { class: 'checkbox' }, input, h('span', null, text))));
  }

  const streamsSelect = h('select', { class: 'select', 'aria-label': 'Одновременно направлений' });
  for (let n = 1; n <= 4; n += 1) {
    streamsSelect.appendChild(h('option', { value: String(n) }, countNoun(n, STREAM_FORMS)));
  }
  streamsSelect.value = String(maxStreams);
  streamsSelect.addEventListener('change', () => { maxStreams = Number(streamsSelect.value) || 2; });

  const modeCostHint = h('div', { class: 'roomdlg__hint' },
    'Больше направлений одновременно — быстрее работа, но быстрее расходуется лимит модели. '
    + 'Остальные ждут в очереди.');
  const modeOffHint = h('div', { class: 'roomdlg__hint' },
    'Проектный режим выключен: руководитель комнаты ответит одним ответом, без карты направлений. '
    + 'Включить можно в любой момент.');

  function renderModeState() {
    const on = Boolean(projectModeInput.checked);
    projectMode = on;
    for (const input of launchInputs) input.disabled = !on;
    streamsSelect.disabled = !on;
    modeCostHint.classList.toggle('hidden', !on);
    modeOffHint.classList.toggle('hidden', on);
  }
  projectModeInput.addEventListener('change', renderModeState);

  function profileById(name) {
    return profiles.find((p) => profileName(p) === name) || null;
  }

  function renderHints() {
    const d = profileById(directorSelect.value);
    directorHint.textContent = d && d.description
      ? String(d.description)
      : 'Руководитель комнаты разложит запрос по команде и сведёт один ответ.';
    const c = profileById(criticSelect.value);
    criticHint.textContent = criticSelect.value
      ? (c && c.description ? String(c.description) : 'Критик проверит свод перед ответом.')
      : 'Без критика: ответ придёт сразу после свода, без проверки.';
    const hasCritic = Boolean(criticSelect.value);
    criticPassBox.classList.toggle('hidden', !hasCritic);
    roundsSelect.disabled = !hasCritic;
    criticPassInput.disabled = !hasCritic;
    // Галочка «проверять критиком» не может быть включена, когда критика нет.
    if (!hasCritic) criticPassInput.checked = false;
    else if (!criticPassTouched) criticPassInput.checked = true;
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
    if (!list.length) { if (notesUi) notesUi.refresh(); if (modelUi) modelUi.refresh(); return; }
    for (const p of list) {
      const name = profileName(p);
      const input = h('input', { type: 'checkbox', id: 'roomdlg-m-' + name });
      input.checked = selectedMembers.has(name);
      input.addEventListener('change', () => {
        if (input.checked) selectedMembers.add(name); else selectedMembers.delete(name);
        if (notesUi) notesUi.refresh();          // указания — только у выбранных
        if (modelUi) modelUi.refresh();          // модель — тоже
      });
      const note = String(p.title || p.description || '');
      // Имя — человеческое, системный id — только в подсказке (D8).
      const human = displayName(name, String(p.kind || 'worker'), p.title);
      membersHost.appendChild(h('div', { class: 'roomdlg__member' },
        h('label', { class: 'checkbox', for: input.id, title: 'Профиль: ' + name }, input,
          h('span', null, human)),
        h('span', { class: 'roomdlg__member-note', title: String(p.description || note) }, note || 'исполнитель'),
      ));
    }
    if (modelUi) modelUi.refresh();
  }

  directorSelect.addEventListener('change', () => { renderHints(); renderMembers(); });
  criticSelect.addEventListener('change', () => { renderHints(); renderMembers(); });

  form.appendChild(errBanner);
  form.appendChild(field({ label: 'Название', control: titleInput, hint: 'Коротко: проект или направление.' }));
  form.appendChild(h('div', { class: 'roomdlg__row' },
    field({ label: 'Иконка', control: iconsHost }),
    h('div', { class: 'roomdlg__section' },
      field({ label: 'Руководитель комнаты', control: directorSelect }),
      directorHint,
    ),
  ));
  form.appendChild(h('div', { class: 'roomdlg__row' },
    h('div', { class: 'roomdlg__section' },
      field({ label: 'Критик', control: criticSelect }),
      criticHint,
      h('div', { class: 'row row--wrap' }, criticPassBox, roundsSelect),
    ),
    h('div', { class: 'roomdlg__section' },
      field({ label: 'Доска', control: boardInput, hint: 'Одна доска на все комнаты — поле не меняется.' }),
    ),
  ));
  form.appendChild(h('div', { class: 'roomdlg__section' },
    field({
      label: 'Папка проекта',
      control: dirBox,
      hint: 'Сюда команда будет складывать отчёты и материалы по этой комнате. '
        + 'Не обязательно — можно выбрать позже.',
    }),
  ));
  form.appendChild(h('div', { class: 'roomdlg__section' },
    h('div', { class: 'roomdlg__label' }, 'Состав'),
    h('div', { class: 'roomdlg__hint' }, 'Кого руководитель комнаты может звать в подзадачи. '
      + 'Руководитель комнаты и критик входят отдельно.'),
    membersHost,
  ));

  /* ── ЕДИНСТВЕННАЯ точка монтирования указаний (WAVE9 §9) ──────────────── */
  const notesHost = h('div', { class: 'roomdlg__notes' });
  form.appendChild(h('div', { class: 'roomdlg__section' },
    h('div', { class: 'roomdlg__label' }, 'Особые указания участникам этой комнаты'),
    h('div', { class: 'roomdlg__hint' },
      'Работают только в этой комнате: правила, навыки, на что смотреть особенно. '
      + 'Собственные настройки участника не меняются.'),
    notesHost,
  ));
  mountAgentNotes(notesHost, ctx, {
    room: full,
    profiles,
    values: notesOf(full),
    participants: currentParticipants,
  }).then((ui) => { notesUi = ui; }).catch(() => { notesUi = null; });

  /* ── ЕДИНСТВЕННАЯ точка монтирования модели (WAVE10) ─────────────────── */
  const modelsHost = h('div', { class: 'roomdlg__models' });
  form.appendChild(h('div', { class: 'roomdlg__section' },
    h('div', { class: 'roomdlg__label' }, 'Модель, провайдер и мышление участников'),
    h('div', { class: 'roomdlg__hint' },
      'Действует только в этой комнате; у участника без изменений — его собственная настройка. '
      + 'Новая модель берётся со следующей задачи.'),
    modelsHost,
  ));
  mountAgentModel(modelsHost, ctx, {
    room: full,
    catalog: catalogModel,
    mode: 'draft',
    participants: currentParticipants,
  }).then((ui) => {
    modelUi = ui;
    if (notesUi) notesUi.refresh();
  }).catch(() => { modelUi = null; });
  form.appendChild(h('div', { class: 'roomdlg__section' },
    h('div', { class: 'roomdlg__label' }, 'Проектный режим'),
    h('div', { class: 'roomdlg__hint' },
      'Один главный запрос — руководитель комнаты сам раскладывает его на направления, '
      + 'каждое станет отдельным запросом комнаты.'),
    projectModeBox,
    field({ label: 'Как запускать направления', control: launchHost }),
    h('div', { class: 'roomdlg__row' },
      field({ label: 'Одновременно направлений', control: streamsSelect }),
      h('div', { class: 'roomdlg__section' }, modeCostHint),
    ),
    modeOffHint,
  ));
  form.appendChild(h('div', { class: 'roomdlg__section' },
    h('div', { class: 'roomdlg__label' }, 'Файлы контекста'),
    h('div', { class: 'roomdlg__hint' }, 'Агенты читают их первыми: STATE.md, брифы, договоры.'),
    filesHost,
    h('div', { class: 'row roomdlg__file-row' }, fileInput, addFileBtn),
  ));

  /* ─────────────────────────────────────────────────────────── отправка */

  const cancelBtn = h('button', { class: 'btn btn--secondary', type: 'button' }, 'Отмена');
  const submitLabel = isEdit ? 'Сохранить' : 'Создать комнату';
  const submitBtn = h('button', { class: 'btn btn--primary', type: 'submit', form: formId },
    icon(isEdit ? 'check' : 'add', { size: 14 }), h('span', null, submitLabel));
  const footNote = h('span', { class: 'roomdlg__foot-note' }, '');
  foot.appendChild(footNote);
  foot.appendChild(cancelBtn);
  foot.appendChild(submitBtn);

  function showError(message) {
    errBanner.replaceChildren();
    if (!message) return;
    errBanner.appendChild(h('div', { class: 'banner banner--error', role: 'alert' },
      icon('error', { size: 14 }), h('div', { class: 'banner__text' }, humanApiMessage(message))));
  }

  function validate() {
    const title = titleInput.value.trim();
    if (title.length < 2) return { field: titleInput, message: 'Название: минимум 2 знака.' };
    if (title.length > 80) return { field: titleInput, message: 'Название: не больше 80 знаков.' };
    if (!directorSelect.value) return { field: directorSelect, message: 'Выберите руководителя комнаты — он раздаёт работу.' };
    if (criticSelect.value && criticSelect.value === directorSelect.value) {
      return { field: criticSelect, message: 'Критик и руководитель комнаты должны быть разными профилями.' };
    }
    if (!Array.from(selectedMembers).some((p) => p !== directorSelect.value && p !== criticSelect.value)) {
      return { field: membersHost, message: 'Отметьте хотя бы одного исполнителя в блоке «Состав».' };
    }
    if (!Number.isInteger(maxStreams) || maxStreams < 1 || maxStreams > 4) {
      return { field: streamsSelect, message: 'Одновременно направлений: от 1 до 4.' };
    }
    for (const path of files) {
      if (!path.trim()) return { field: fileInput, message: 'Пустой путь в файлах контекста.' };
    }
    const dir = pathInput.value.trim();
    if (dir && lastCheck.tone === 'bad' && lastCheck.value === dir) {
      return { field: pathInput, message: lastCheck.text };
    }
    return null;
  }

  let saving = false;

  async function save() {
    if (saving) return;
    const bad = validate();
    if (bad) {
      showError(bad.message);
      if (bad.field === membersHost) {
        try { membersHost.scrollIntoView({ block: 'nearest' }); } catch { /* not in DOM */ }
      } else if (bad.field && typeof bad.field.focus === 'function') {
        bad.field.focus();
      }
      return;
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
      // Проектный режим этой комнаты (A1/A2): план → «Запустить», лимит направлений.
      project_mode: Boolean(projectModeInput.checked),
      launch_mode: launchMode === 'auto' ? 'auto' : 'plan_first',
      max_parallel_streams: Number.isInteger(maxStreams) && maxStreams >= 1 && maxStreams <= 4 ? maxStreams : 2,
    };

    try {
      if (!isEdit) {
        const created = await api.post('/api/rooms', Object.assign({}, payload, {
          board: BOARD,
          members: membersPayload,
          // Указания и модели уходят вместе с комнатой: профили уже в этом же составе.
          agent_notes: notesUi ? notesUi.value() : {},
          agent_models: modelUi ? modelUi.allValues() : {},
        }));
        await refreshRooms(api, store).catch(() => {});
        const id = created && (created.id || (created.room && created.room.id));
        close(created || { id });
        toast('Комната создана', 'ok');
        if (id) navigate('#/room/' + enc(id) + '/composer');
        else navigate('#/');
        return created || null;
      }

      const roomId = String(full.id);
      const updated = await api.patch('/api/rooms/' + enc(roomId), payload);
      const before = new Set(membersOf(full).filter((m) => String(m.role || 'worker') !== 'critic').map((m) => String(m.profile)));
      const after = new Set(selectedMembers);
      const problems = [];
      for (const profile of after) {
        if (before.has(profile)) continue;
        try { await api.post('/api/rooms/' + enc(roomId) + '/members', { profile, role: 'worker' }); }
        catch (err) { problems.push(profile + ': ' + (err && err.message ? err.message : 'не добавлен')); }
      }
      for (const profile of before) {
        if (after.has(profile)) continue;
        try { await api.del('/api/rooms/' + enc(roomId) + '/members/' + enc(profile)); }
        catch (err) { problems.push(profile + ': ' + (err && err.message ? err.message : 'не убран')); }
      }
      // Особые указания (WAVE9 §9): сохраняем ПОСЛЕ состава — новый участник уже в комнате.
      try {
        const wasNotes = notesOf(full);
        const nowNotes = notesUi ? notesUi.value() : {};
        for (const [profile, text] of Object.entries(nowNotes)) {
          if (String((wasNotes[profile] || {}).text || '') === text) continue;
          await api.put('/api/rooms/' + enc(roomId) + '/agents/' + enc(profile) + '/notes', { text });
        }
        for (const profile of Object.keys(wasNotes)) {
          if (nowNotes[profile]) continue;
          await api.put('/api/rooms/' + enc(roomId) + '/agents/' + enc(profile) + '/notes', { text: '' });
        }
      } catch (err) {
        problems.push('указания: ' + (err && err.message ? err.message : 'не сохранены'));
      }
      // Модели участников (WAVE10): у существующей комнаты — точечно, по человеку,
      // чтобы не затереть настройки тех, кого в диалоге не трогали.
      try {
        const wasModels = full && full.agent_models && typeof full.agent_models === 'object'
          ? full.agent_models : {};
        const nowModels = modelUi ? modelUi.allValues() : {};
        const people = new Set([...Object.keys(wasModels), ...Object.keys(nowModels)]);
        for (const profile of people) {
          const wasModel = wasModels[profile] ? {
            provider: String(wasModels[profile].provider || ''),
            model: String(wasModels[profile].model || ''),
            reasoning: normalizeReasoning(wasModels[profile].reasoning),
          } : null;
          const nextModel = nowModels[profile] || null;
          if (JSON.stringify(wasModel) === JSON.stringify(nextModel)) continue;
          await api.put('/api/rooms/' + enc(roomId) + '/agents/' + enc(profile) + '/model', nextModel || {});
        }
      } catch (err) {
        problems.push('модели: ' + (err && err.message ? err.message : 'не сохранены'));
      }
      await refreshRooms(api, store).catch(() => {});
      close(updated || full);
      toast(problems.length ? 'Комната сохранена, но не всё удалось обновить' : 'Комната сохранена', problems.length ? 'warn' : 'ok');
      if (problems.length) toast(problems.join('; '), 'warn');
      if (typeof ctx.refresh === 'function') ctx.refresh();
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
  cancelBtn.addEventListener('click', () => close());

  /* ───────────────────────────────────────────────────── закрытие/фокус */

  const prevFocus = document.activeElement;
  let closed = false;
  let resolveDone = () => {};
  const done = new Promise((resolve) => { resolveDone = resolve; });

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

  function onKey(ev) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      ev.stopPropagation();
      close();
      return;
    }
    if (ev.key !== 'Tab') return;
    const items = Array.from(dialog.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((node) => node.offsetParent !== null || node === document.activeElement);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }

  overlay.addEventListener('mousedown', (ev) => { if (ev.target === overlay) close(); });
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(overlay);
  // Узкое окно: выдвижное меню закрываем — диалог всегда один на экране.
  const appRoot = document.getElementById('app');
  if (appRoot && appRoot.classList.contains('nav-open')) appRoot.classList.remove('nav-open');

  renderHints();
  renderMembers();
  renderModeState();
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
        close();
        toast('Комната в архиве', 'ok');
        navigate('#/');
      } catch (err) {
        showError(err && err.message ? err.message : 'Не удалось архивировать комнату');
      }
    });
    foot.insertBefore(removeBtn, cancelBtn);
  }

  // W10: «Задать папку» из шапки открывает тот же диалог, но с курсором в поле
  // «Папка проекта» — человек сразу печатает путь, а не ищет поле глазами.
  if (String((opts && opts.focus) || '') === 'project_dir') {
    pathInput.focus();
    try { pathInput.setSelectionRange(pathInput.value.length, pathInput.value.length); } catch { /* ignore */ }
  } else {
    titleInput.focus();
    try { titleInput.setSelectionRange(titleInput.value.length, titleInput.value.length); } catch { /* ignore */ }
  }

  // Промис закрывается вместе с диалогом: созданная/изменённая комната или null.
  return done;
}

export default { openRoomDialog, refreshRooms, suggestProjectDir, dirVerdict };
