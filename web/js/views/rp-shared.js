/**
 * rp-shared.js — мелочи, общие для блоков страницы запроса.
 *
 * Вынесено из request-parts.js, чтобы дерево работ (request-parts-tree.js)
 * и остальные блоки не дублировали вызовы API, тосты и кнопки.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';

/** Тост страницы; молча, если ctx без toast (стенд, тесты). */
export function toast(ctx, msg, kind) {
  if (ctx && typeof ctx.toast === 'function') ctx.toast(msg, kind || 'info');
}

/** Сообщить странице, что данные изменились (перечитать detail). */
export function changed(ctx, onChanged) {
  if (typeof onChanged === 'function') { onChanged(); return; }
  if (ctx && typeof ctx.refresh === 'function') { ctx.refresh(); return; }
  if (typeof document !== 'undefined' && typeof CustomEvent === 'function') {
    document.dispatchEvent(new CustomEvent('rp:changed'));
  }
}

function actionPath(rid) {
  return `/api/requests/${encodeURIComponent(String(rid || ''))}/actions`;
}

/** POST /api/requests/{rid}/actions. */
export async function postAction(ctx, rid, body) {
  if (!ctx || !ctx.api || typeof ctx.api.post !== 'function') throw new Error('Нет связи с приложением');
  return ctx.api.post(actionPath(rid), body);
}

/**
 * Кнопка проекта: вариант, размер, иконка, disabled.
 * @param {string} label
 * @param {{variant?: string, size?: 'sm', icon?: string, title?: string,
 *          disabled?: boolean, onclick?: Function}} [opts]
 * @returns {HTMLElement}
 */
export function button(label, opts = {}) {
  const o = opts || {};
  const cls = ['btn', 'btn--' + (o.variant || 'secondary')];
  if (o.size === 'sm') cls.push('btn--sm');
  const el = h('button', {
    class: cls,
    type: 'button',
    title: o.title || undefined,
    disabled: o.disabled === true,
    onclick: typeof o.onclick === 'function' ? o.onclick : undefined,
  });
  if (o.icon) el.appendChild(icon(o.icon, { size: 13 }));
  el.appendChild(h('span', {}, String(label)));
  return el;
}

/** Заблокировать все кнопки/поля внутри узла на время запроса. */
export function lock(el, on) {
  if (!el || typeof el.querySelectorAll !== 'function') return;
  for (const b of Array.from(el.querySelectorAll('button, select'))) {
    if (on) b.setAttribute('disabled', '');
    else b.removeAttribute('disabled');
  }
  if (on) el.setAttribute('aria-busy', 'true');
  else el.removeAttribute('aria-busy');
}

/** Найти карточку в дереве на этой же странице и подсветить. */
export function focusTreeTask(taskId) {
  if (typeof document === 'undefined' || !taskId) return false;
  const rows = Array.from(document.querySelectorAll('[data-rp-tree] [data-task-id]'));
  const row = rows.find((r) => r.getAttribute('data-task-id') === String(taskId));
  if (!row) return false;
  if (typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center', behavior: 'smooth' });
  row.classList.add('rp-node--flash');
  setTimeout(() => row.classList.remove('rp-node--flash'), 1800);
  const action = row.querySelector('.rp-actions .btn, .rp-node__toggle');
  if (action && typeof action.focus === 'function') action.focus({ preventScroll: true });
  return true;
}
