/**
 * term-help.js — подсказка «?» у нетривиального слова (P7).
 *
 * Слова объясняются один раз в словаре `text-ru.js` (TERMS): «свод», «раунд»,
 * «критик», «версия», «этап». Здесь только вид: маленькая кнопка «?» рядом со
 * словом. Наведение мыши показывает короткую подсказку, нажатие открывает
 * небольшое окно с объяснением и примером — так же понятно с клавиатуры и на
 * узком экране, где всплывающих подсказок нет.
 *
 * Один текст на всё приложение: правка словаря меняет все подсказки сразу.
 */

import { h } from './util.js';
import { term } from './text-ru.js';
import { buildDialog } from './ui.js';

/** Класс кнопки — по нему считают подсказки тесты и проверка вёрстки. */
export const TERM_HELP_CLASS = 'term-help';

/**
 * Подпись для вспомогательных технологий: «Что такое раунд: <объяснение>».
 * @param {string} key ключ словаря (TERMS)
 * @returns {string} '' — термина нет
 */
export function termHelpLabel(key) {
  const t = term(key);
  if (!t.hint) return '';
  return 'Что такое ' + t.name + ': ' + t.hint;
}

/**
 * Окно с объяснением термина. Возвращает каркас диалога или null, если
 * объяснять нечего (и в среде без DOM).
 * @param {string} key
 * @returns {{close: Function, dialog: HTMLElement}|null}
 */
export function openTermHelp(key) {
  const t = term(key);
  if (!t.hint) return null;
  if (!globalThis.document || !globalThis.document.body) return null;
  const body = h('div', { class: 'term-help__text' },
    h('p', { class: 'term-help__hint' }, t.hint));
  if (t.example) body.appendChild(h('p', { class: 'small muted term-help__example' }, t.example));
  let shell = null;
  const close = () => { if (shell) shell.close(); };
  const ok = h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: close }, 'Понятно');
  shell = buildDialog({ title: t.title, body, buttons: [ok], onCancel: close, initialFocus: ok });
  return shell;
}

/**
 * Кнопка «?» рядом со словом.
 * @param {string} key ключ словаря
 * @param {{className?: string, label?: string}} [opts]
 * @returns {HTMLElement} кнопка (без обработчика, если термина нет)
 */
export function termHelp(key, opts = {}) {
  const o = opts || {};
  const t = term(key);
  const label = String(o.label || '') || termHelpLabel(key);
  const btn = h('button', {
    class: TERM_HELP_CLASS + (o.className ? ' ' + String(o.className) : ''),
    type: 'button',
    'data-term': t.key,
    'aria-label': label || ('Что такое ' + (t.name || 'это слово')),
    title: t.hint || '',
  }, '?');
  if (t.hint) {
    btn.addEventListener('click', (ev) => {
      if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
      openTermHelp(key);
    });
  }
  return btn;
}

export default { TERM_HELP_CLASS, termHelp, termHelpLabel, openTermHelp };
