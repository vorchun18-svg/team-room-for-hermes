/**
 * project-dir-button.js — папка проекта и настройки комнаты в шапке (Д2 WAVE8, W10).
 *
 *   import { folderActionButton, roomSettingsButton, projectDirHint } from './project-dir-button.js';
 *
 * Дефекты с живой системы (W10):
 *  1. у комнаты С папкой кнопка была, у комнаты БЕЗ папки — пусто. Теперь место
 *     всегда занято: «Открыть папку» при заданном пути, «Задать папку» — когда пути
 *     нет (открывает настройки комнаты и ставит курсор в поле «Папка проекта»);
 *  2. кнопки настроек самой комнаты не было вовсе — теперь постоянная
 *     «Настройки комнаты» (шестерёнка + подпись на широком экране) на всех вкладках
 *     и на странице запроса, тот же диалог `room-dialog.js` в режиме правки;
 *  3. «Открыть папку» поднимала проводник в фоне — сервер сам выводит его окно
 *     вперёд (`server/project_dir.open_dir_report`), а здесь — понятные ошибки:
 *     «Не удалось открыть папку: …», «Скопировать путь», «Создать папку и открыть».
 *
 * Папка проекта — место, куда команда складывает ВСЕ итоговые файлы по комнате
 * (отчёты, таблицы, документы). Открывается через `POST /api/open {path, kind:"dir"}`;
 * сервер проверяет путь (`project_dir.check_dir`) — системные каталоги и Hermes
 * отклоняются там же, до любого запуска проводника.
 */

import { h } from './util.js';
import { icon } from './icons.js';
import { ensureCss } from './ui.js';
import { humanApiMessage } from './shell-labels.js';

const CSS_HREF = 'css/project-dir.css';

/** Задержка живой проверки пути: 400 мс после последнего нажатия. */
export const CHECK_DELAY_MS = 400;

/** Текст подсказки, когда папка не задана. */
export const HINT_TEXT = 'Задайте папку проекта, чтобы результаты работы собирались в одном месте';

/** Подписи кнопок: одинаковые во всей программе. */
export const OPEN_LABEL = 'Открыть папку';
export const ASK_LABEL = 'Задать папку';
export const SETTINGS_LABEL = 'Настройки комнаты';

/** Что будет со старыми файлами при смене папки (говорим прямо в диалоге). */
export const MOVE_NOTE = 'Старые файлы останутся в прежней папке; новые пойдут в новую';

/** Начало сообщения, когда проводник не открылся. */
export const OPEN_FAILED = 'Не удалось открыть папку';

/** Предложение пути: `C:\Users\Public\Documents\<Название комнаты>` (Д2).
 *
 *  Публичная папка документов есть на любом Windows и доступна на запись — путь
 *  не зависит от имени пользователя и от того, где стоит приложение. */
export function suggestProjectDir(title) {
  const root = 'C:\\Users\\Public\\Documents\\';
  const raw = String(title == null ? '' : title)
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '-')
    .replace(/\s+/g, ' ')
    .replace(/^[ .-]+|[ .-]+$/g, '')
    .slice(0, Math.max(10, 80 - root.length))
    .trim();
  return root + (raw || 'Новая комната');
}

/**
 * Вердикт живой проверки для человека: «Папка существует» / «Будет создана» /
 * «Нельзя: …». Одна строка плюс предупреждения сервера (например, про Vault).
 * @param {object|null} result ответ POST /api/rooms/check-dir
 * @returns {{tone: 'ok'|'new'|'bad'|'muted', text: string, warn: string}}
 */
export function dirVerdict(result) {
  if (!result || typeof result !== 'object') {
    return { tone: 'muted', text: 'Проверяю…', warn: '' };
  }
  const warn = Array.isArray(result.warnings) ? result.warnings.filter(Boolean).join(' ') : '';
  if (result.allowed === false) {
    const reason = String(result.reason || 'путь не подходит');
    return { tone: 'bad', text: /^нельзя/i.test(reason) ? reason : 'Нельзя: ' + reason, warn };
  }
  if (result.exists) return { tone: 'ok', text: 'Папка существует', warn };
  if (result.will_create) return { tone: 'new', text: 'Будет создана', warn };
  return { tone: 'muted', text: '', warn };
}

/** Адрес настроек комнаты: вкладка «Команда» (там живёт кнопка настроек). */
export function projectDirSettingsHash(roomId) {
  const id = String(roomId || '').trim();
  return id ? '#/room/' + encodeURIComponent(id) + '/team' : '#/';
}

function toastOf(ctx) {
  return typeof (ctx && ctx.toast) === 'function' ? ctx.toast : () => {};
}

/**
 * Открыть папку в проводнике. Возвращает отчёт и не бросает:
 * `{ok, path, reason, can_create, foreground}`.
 * @param {object} api
 * @param {string} path
 * @param {{kind?: 'dir', mkdir?: boolean}} [opts] `kind:'dir'` — это папка (а не файл)
 * @returns {Promise<{ok: boolean, path: string, reason: string,
 *                    can_create?: boolean, foreground?: boolean}>}
 */
export async function openProjectDir(api, path, opts = {}) {
  const target = String(path || '').trim();
  const o = opts || {};
  if (!api || typeof api.post !== 'function' || !target) {
    return { ok: false, path: target, reason: '' };
  }
  const body = { path: target };
  if (o.kind === 'dir') body.kind = 'dir';
  if (o.mkdir === true) body.mkdir = true;
  let res;
  try {
    res = await api.post('/api/open', body);
  } catch (err) {
    return {
      ok: false, path: target, failed: true,
      reason: humanApiMessage(err && err.message ? err.message : OPEN_FAILED),
    };
  }
  const obj = res && typeof res === 'object' ? res : {};
  if (obj.ok === false || obj.revealed === false) {
    return {
      ok: false,
      path: String(obj.path || target),
      reason: String(obj.reason || 'Папка не открылась — возможно, её ещё нет.'),
      can_create: Boolean(obj.can_create),
    };
  }
  return {
    ok: true,
    path: String(obj.path || target),
    reason: obj.reason ? String(obj.reason) : '',
    foreground: obj.foreground !== false,
  };
}

/**
 * Тихая подсказка «Задайте папку проекта…» со ссылкой в настройки комнаты.
 * Живёт в полосе под вкладками (`#page-hint`), не в шапке (DESIGN-NOTES §10).
 * @param {object} ctx
 * @param {{roomId?: string}} [opts]
 * @returns {HTMLElement}
 */
export function projectDirHint(ctx, opts = {}) {
  ensureCss(CSS_HREF).catch(() => {});
  const roomId = String((opts && opts.roomId) || '').trim();
  const link = h('a', {
    class: 'pd-hint__link', href: projectDirSettingsHash(roomId),
    title: 'Открыть настройки комнаты', 'data-pd': 'hint-link',
  }, 'Настройки комнаты');
  link.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    openRoomSettings(ctx, { id: roomId }, { focus: 'project_dir' });
  });
  return h('div', { class: 'pd-hint', 'data-pd': 'hint' },
    icon('info', { size: 13 }),
    h('span', { class: 'pd-hint__text' }, HINT_TEXT),
    link,
  );
}

/**
 * Кнопка «Открыть папку проекта» или тихая подсказка вместо неё (Д2).
 * @param {object} ctx  контекст вьюхи ({api, toast, navigate, store})
 * @param {{roomId?: string, projectDir?: string, showHint?: boolean,
 *          label?: string, size?: 'sm'|'md', kind?: 'dir'}} [opts]
 * @returns {HTMLElement|null}
 */
export function projectDirButton(ctx, opts = {}) {
  const o = opts || {};
  const path = String(o.projectDir || '').trim();
  const roomId = String(o.roomId || '').trim();
  const showHint = o.showHint !== false;
  const toast = toastOf(ctx);

  if (!path) {
    if (!showHint) return null;
    return projectDirHint(ctx, { roomId });
  }

  ensureCss(CSS_HREF).catch(() => {});
  const size = o.size === 'md' ? 'md' : 'sm';
  const btn = h('button', {
    class: 'btn btn--secondary' + (size === 'sm' ? ' btn--sm' : '') + ' pd-btn',
    type: 'button',
    title: path,
    'data-pd': 'open',
  }, icon('folder-opened', { size: 14 }), h('span', null, o.label || 'Открыть папку проекта'));

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const res = await openProjectDir(ctx && ctx.api, path, { kind: o.kind === 'dir' ? 'dir' : undefined });
      if (!res.ok) {
        toast('Папка не открылась — возможно, её ещё нет. Проверьте путь в настройках комнаты.', 'warn');
      }
    } catch (err) {
      toast(humanApiMessage(err && err.message ? err.message : OPEN_FAILED), 'danger');
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

/**
 * Кнопка папки для шапки комнаты: место занято ВСЕГДА.
 * Путь есть → «Открыть папку»; пути нет → «Задать папку» (настройки комнаты).
 * @param {object} ctx
 * @param {{id?: string, title?: string, project_dir?: string}} room
 * @returns {HTMLElement}
 */
export function folderActionButton(ctx, room) {
  ensureCss(CSS_HREF).catch(() => {});
  const r = room || {};
  const path = String(r.project_dir || '').trim();
  const roomId = String(r.id || '').trim();
  const has = Boolean(path);

  const btn = h('button', {
    class: 'btn ' + (has ? 'btn--secondary' : 'btn--ghost') + ' btn--sm pd-btn',
    type: 'button',
    title: has ? path : 'Папка проекта не задана — открыть настройки комнаты',
    'data-pd': has ? 'open' : 'ask',
    'aria-label': has ? OPEN_LABEL + ': ' + path : ASK_LABEL,
  }, icon(has ? 'folder-opened' : 'new-folder', { size: 14 }),
  h('span', { class: 'pd-btn__label' }, has ? OPEN_LABEL : ASK_LABEL));

  btn.addEventListener('click', async () => {
    if (!has) {
      await openRoomSettings(ctx, { id: roomId }, { focus: 'project_dir' });
      return;
    }
    btn.disabled = true;
    try {
      await openFolderWithHelp(ctx, path);
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

/**
 * Постоянная кнопка «Настройки комнаты» (шестерёнка + подпись на широком экране).
 * @param {object} ctx
 * @param {{id?: string, title?: string}} room
 * @returns {HTMLElement}
 */
export function roomSettingsButton(ctx, room) {
  ensureCss(CSS_HREF).catch(() => {});
  const r = room || {};
  const btn = h('button', {
    class: 'btn btn--ghost btn--sm room-settings',
    type: 'button',
    'aria-label': SETTINGS_LABEL,
    title: SETTINGS_LABEL + ': название, руководитель, состав, папка проекта, указания',
    'data-action': 'room-settings',
  }, icon('settings-gear', { size: 15 }), h('span', { class: 'room-settings__label' }, SETTINGS_LABEL));
  btn.addEventListener('click', () => { openRoomSettings(ctx, { id: String(r.id || '') }); });
  return btn;
}

/**
 * Открыть настройки комнаты: тот же диалог, что при создании (`room-dialog.js`).
 * @param {object} ctx
 * @param {{id?: string, title?: string, roomId?: string}} room
 * @param {{focus?: 'project_dir'|''}} [opts]
 * @returns {Promise<object|null>}
 */
export async function openRoomSettings(ctx, room, opts = {}) {
  const r = room || {};
  const id = String(r.id || r.roomId || '').trim();
  const toast = toastOf(ctx);
  if (!id) return null;
  try {
    const mod = await import('./views/room-dialog.js');
    if (mod && typeof mod.openRoomDialog === 'function') {
      return await mod.openRoomDialog(ctx, (r.id ? r : { id }), { focus: (opts && opts.focus) || '' });
    }
  } catch (err) {
    console.warn('[room-settings] диалог комнаты не открылся', err);
  }
  toast('Не удалось открыть настройки комнаты', 'danger');
  return null;
}

/** Скопировать путь в буфер обмена (без прав — честная подсказка). */
export async function copyPath(text, toast) {
  const value = String(text || '');
  if (!value) return false;
  try {
    const nav = typeof navigator !== 'undefined' ? navigator : null;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
      await nav.clipboard.writeText(value);
      if (typeof toast === 'function') toast('Путь скопирован', 'ok');
      return true;
    }
  } catch { /* браузер без прав на буфер обмена */ }
  if (typeof toast === 'function') toast('Скопируйте путь вручную: выделите его в окне', 'warn');
  return false;
}

/**
 * Открыть папку; если не вышло — показать, что делать (создать, скопировать путь).
 * @param {object} ctx
 * @param {string} path
 */
export async function openFolderWithHelp(ctx, path) {
  const toast = toastOf(ctx);
  const target = String(path || '').trim();
  const res = await openProjectDir(ctx && ctx.api, target, { kind: 'dir' });
  if (res.ok) {
    if (res.reason) toast(res.reason, 'warn');
    return res;
  }
  folderProblemDialog(ctx, {
    path: res.path || target,
    message: res.reason || OPEN_FAILED,
    canCreate: Boolean(res.can_create),
    onCreate: async () => {
      const made = await openProjectDir(ctx && ctx.api, res.path || target, { kind: 'dir', mkdir: true });
      if (made.ok) {
        toast(made.reason ? 'Папка создана. ' + made.reason : 'Папка создана и открыта',
          made.reason ? 'warn' : 'ok');
      } else {
        toast(made.reason || 'Не удалось создать папку', 'danger');
      }
      return made;
    },
  });
  return res;
}

/**
 * Окно «Не удалось открыть папку» с путём и действиями:
 * «Создать папку и открыть» (если её нет), «Скопировать путь», «Закрыть».
 * @param {object} ctx
 * @param {{path?: string, message?: string, canCreate?: boolean,
 *          onCreate?: () => Promise<any>}} [opts]
 */
export function folderProblemDialog(ctx, opts = {}) {
  const o = opts || {};
  const toast = toastOf(ctx);
  if (typeof document === 'undefined' || !document.body) return null;
  ensureCss(CSS_HREF).catch(() => {});
  const path = String(o.path || '');

  const overlay = h('div', { class: 'dialog-backdrop' });
  const head = h('div', { class: 'dialog__head' },
    h('h2', { class: 'dialog__title' }, OPEN_FAILED),
    h('button', {
      class: 'btn btn--ghost btn--icon btn--sm', type: 'button',
      'aria-label': 'Закрыть', title: 'Закрыть', onclick: () => close(),
    }, icon('close', { size: 14 })));
  const body = h('div', { class: 'dialog__body' },
    h('div', { class: 'pd-help', 'data-pd': 'help' },
      h('p', { class: 'pd-help__text' }, String(o.message || OPEN_FAILED)),
      h('p', { class: 'pd-help__path' }, icon('folder', { size: 13 }), h('code', null, path || 'путь не задан')),
      o.canCreate
        ? h('p', { class: 'pd-help__hint small muted' }, 'Можно создать её сейчас — команда будет складывать файлы туда.')
        : null,
    ));
  const foot = h('div', { class: 'dialog__foot' });
  const dialog = h('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': OPEN_FAILED },
    head, body, foot);
  overlay.appendChild(dialog);

  const createBtn = o.canCreate ? h('button', {
    class: 'btn btn--primary', type: 'button', 'data-pd': 'create',
  }, icon('folder-opened', { size: 14 }), 'Создать папку и открыть') : null;
  const copyBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-pd': 'copy',
  }, icon('copy', { size: 14 }), 'Скопировать путь');
  const closeBtn = h('button', { class: 'btn btn--ghost', type: 'button', 'data-pd': 'close' }, 'Закрыть');
  if (createBtn) foot.appendChild(createBtn);
  foot.appendChild(copyBtn);
  foot.appendChild(closeBtn);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    if (typeof document !== 'undefined' && document.removeEventListener) {
      document.removeEventListener('keydown', onKey, true);
    }
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
  }
  function onKey(ev) {
    if (ev && ev.key === 'Escape') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      close();
    }
  }

  if (createBtn) {
    createBtn.addEventListener('click', async () => {
      createBtn.disabled = true;
      copyBtn.disabled = true;
      try {
        const made = await (typeof o.onCreate === 'function' ? o.onCreate() : Promise.resolve(null));
        if (made && made.ok) close();
        else createBtn.disabled = false;
      } finally {
        copyBtn.disabled = false;
      }
    });
  }
  copyBtn.addEventListener('click', () => { copyPath(path, toast); });
  closeBtn.addEventListener('click', () => close());
  overlay.addEventListener('mousedown', (ev) => { if (ev.target === overlay) close(); });
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('keydown', onKey, true);
  }
  document.body.appendChild(overlay);
  const first = createBtn || copyBtn;
  if (first && typeof first.focus === 'function') first.focus();
  return { close, overlay, dialog };
}

export default {
  projectDirButton, folderActionButton, roomSettingsButton, projectDirHint,
  openProjectDir, openFolderWithHelp, folderProblemDialog, openRoomSettings,
  projectDirSettingsHash, HINT_TEXT, OPEN_LABEL, ASK_LABEL, SETTINGS_LABEL, MOVE_NOTE,
};
