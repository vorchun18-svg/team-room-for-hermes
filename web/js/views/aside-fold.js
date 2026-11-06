/**
 * aside-fold.js — свёрнутые блоки правой колонки запроса (WAVE17, P3).
 *
 * Замечание аудита B15/P3: все блоки «Дальше» были раскрыты, шесть одинаковых
 * форм подряд — визуальный шум. Теперь «Нужен ваш ответ» всегда раскрыт и стоит
 * первым, а «Отвечено», «Шаги», «Решения», «Будущие вопросы» — свёрнуты до
 * заголовка со счётчиком. Состояние запоминается в `localStorage`
 * (`teamroom.aside.folds`, ключ `<id запроса>:<имя блока>`), поэтому блоки
 * открываются так, как пользователь их оставил, в том числе после перезагрузки.
 *
 * Модуль маленький и без зависимостей от страницы: `h` + `icon` и чистое
 * состояние. В тестах (мини-DOM, нет localStorage) молча работает без памяти.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';

/** Ключ хранилища; версия в имени — чтобы старые значения не мешали. */
export const FOLD_STORE = 'teamroom.aside.folds';

function storage() {
  try {
    const ls = globalThis.localStorage;
    if (!ls || typeof ls.getItem !== 'function' || typeof ls.setItem !== 'function') return null;
    return ls;
  } catch {
    return null; // приватный режим или запрет доступа — просто без памяти
  }
}

function readAll() {
  const ls = storage();
  if (!ls) return {};
  try {
    const raw = ls.getItem(FOLD_STORE);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Ключ блока: свёрнутость запоминается для каждого запроса своя. */
export function foldKey(rid, name) {
  return String(rid || '') + ':' + String(name || '');
}

/**
 * Открыт ли блок: сохранённое значение, иначе `fallback`.
 * @param {string} rid
 * @param {string} name
 * @param {boolean} [fallback]
 * @returns {boolean}
 */
export function foldState(rid, name, fallback = false) {
  const all = readAll();
  const value = all[foldKey(rid, name)];
  return typeof value === 'boolean' ? value : Boolean(fallback);
}

/** Запомнить состояние блока (не падает ни при каких условиях). */
export function setFoldState(rid, name, open) {
  const ls = storage();
  if (!ls) return;
  const all = readAll();
  all[foldKey(rid, name)] = Boolean(open);
  try {
    ls.setItem(FOLD_STORE, JSON.stringify(all));
  } catch { /* переполнение — не повод ломать страницу */ }
}

/** Забыть все состояния блоков (диагностика/сброс). */
export function resetFolds() {
  const ls = storage();
  if (!ls) return;
  try { ls.removeItem(FOLD_STORE); } catch { /* ignore */ }
}

/**
 * Свёрнутый блок колонки: карточка-`<details>` с заголовком и счётчиком.
 *
 * @param {object} opts
 * @param {string} opts.rid        id запроса (ключ памяти)
 * @param {string} opts.name       имя блока в памяти (`steps`, `decisions`, …)
 * @param {string} opts.title      заголовок
 * @param {string|number|null} [opts.count]  счётчик в заголовке
 * @param {string|null} [opts.hint]  одна пояснительная строка под заголовком
 * @param {boolean} [opts.defaultOpen]
 * @param {Node|Node[]} [opts.body]
 * @param {string} [opts.className]
 * @param {string} [opts.key]      `data-key` для точечной перерисовки
 * @param {string} [opts.id]       якорь (`#steps`)
 * @returns {HTMLElement}
 */
export function foldCard(opts = {}) {
  const o = opts || {};
  const open = foldState(o.rid, o.name, o.defaultOpen);
  const attrs = {
    class: 'card req-side req-side--fold ' + String(o.className || ''),
    'data-fold': String(o.name || ''),
  };
  if (o.key) attrs['data-key'] = String(o.key);
  if (o.id) attrs.id = String(o.id);
  if (open) attrs.open = true;
  const details = h('details', attrs);

  const hasCount = o.count !== null && o.count !== undefined && o.count !== '';
  const summary = h('summary', { class: 'req-side__fold-head' },
    icon('chevron-right', { size: 12, className: 'req-side__fold-chev' }),
    h('span', { class: 'req-side__fold-title' }, String(o.title || '')),
    hasCount ? h('span', { class: 'chip chip--muted req-side__fold-count tnum' }, String(o.count)) : null);
  details.appendChild(summary);

  if (o.hint) details.appendChild(h('div', { class: 'req-side__hint small muted' }, String(o.hint)));
  const body = Array.isArray(o.body) ? o.body : [o.body];
  for (const node of body) if (node) details.appendChild(node);

  details.addEventListener('toggle', () => setFoldState(o.rid, o.name, Boolean(details.open)));
  return details;
}

export default { FOLD_STORE, foldCard, foldKey, foldState, setFoldState, resetFolds };
