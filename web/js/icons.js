/**
 * icons.js — Codicons локально (web/fonts/codicon.ttf), без CDN.
 * `icon('search')` → <i class="codicon codicon-search">; размер задаётся fontSize.
 */

const NAME_RE = /^[a-z0-9-]+$/;

/** Имена, для которых в components.css есть глиф (остальные рисуем заглушкой). */
const ICONS = new Set([
  'add', 'plus', 'home', 'settings-gear', 'gear', 'search', 'sync', 'loading', 'circle-filled',
  'circle-outline', 'check', 'close', 'chevron-down', 'chevron-right', 'chevron-left', 'chevron-up',
  'ellipsis', 'kebab-horizontal', 'comment-discussion', 'comment', 'organization', 'map',
  'list-unordered', 'type-hierarchy', 'history', 'clock', 'graph', 'file-text', 'file', 'calendar',
  'arrow-right', 'arrow-left', 'arrow-up', 'arrow-circle-right', 'arrow-swap', 'link-external',
  'folder-opened', 'play', 'play-circle', 'refresh', 'warning', 'error', 'info', 'bell', 'color-mode',
  'sparkle', 'hubot', 'robot', 'account', 'person', 'person-add', 'sign-out', 'filter', 'pin', 'trash',
  'edit', 'copy', 'check-all', 'pulse', 'beaker', 'microscope', 'tasklist', 'project', 'dashboard',
  'book', 'law', 'briefcase', 'circuit-board', 'database', 'server-process', 'terminal', 'send',
  'attach', 'layout', 'milestone', 'checklist', 'git-branch', 'globe', 'key', 'lock', 'lightbulb',
  'light-bulb', 'shield', 'rocket', 'zap', 'eye', 'table', 'pie-chart', 'target', 'archive', 'inbox',
  'note', 'quote', 'code', 'debug-restart', 'debug-alt', 'stop-circle', 'star', 'sort-precedence',
  'fold', 'unfold', 'symbol-event', 'symbol-class', 'issues', 'report', 'bug', 'output', 'extensions',
  'library', 'remote', 'new-folder', 'new-file', 'open-preview', 'split-horizontal', 'live-share',
  'cloud-upload', 'pass', 'pass-filled', 'verified', 'circle-slash', 'magnet', 'telescope', 'wand',
  'thumbsup', 'thumbsdown', 'feedback', 'chat-sparkle', 'comment-draft',
]);

/** Значение по умолчанию, если имя не найдено. */
export const FALLBACK_ICON = 'circle-outline';

/**
 * @param {string} name  имя codicon ('search', 'chevron-down', …)
 * @param {{size?: number|string, className?: string}} [opts]
 * @returns {HTMLElement}
 */
export function icon(name, opts = {}) {
  const { size = 16, className = '' } = opts;
  const safe = NAME_RE.test(String(name || '')) && ICONS.has(String(name)) ? String(name) : FALLBACK_ICON;
  const el = document.createElement('i');
  el.className = 'codicon codicon-' + safe + (className ? ' ' + className : '');
  el.setAttribute('aria-hidden', 'true');
  if (size) el.style.fontSize = typeof size === 'number' ? size + 'px' : String(size);
  return el;
}

/**
 * Разметка иконки строкой (для шаблонов). Имя проверяется по белому списку —
 * инъекция через имя невозможна.
 * @param {string} name
 * @param {{size?: number, className?: string}} [opts]
 * @returns {string}
 */
export function iconHtml(name, opts = {}) {
  const { size = 16, className = '' } = opts;
  const safe = NAME_RE.test(String(name || '')) && ICONS.has(String(name)) ? String(name) : FALLBACK_ICON;
  const cls = 'codicon codicon-' + safe + (className ? ' ' + className : '');
  return `<i class="${cls}" aria-hidden="true"${size ? ` style="font-size:${Number(size) || 16}px"` : ''}></i>`;
}

/** Есть ли глиф для имени. */
export function hasIcon(name) {
  return ICONS.has(String(name));
}

export default icon;
