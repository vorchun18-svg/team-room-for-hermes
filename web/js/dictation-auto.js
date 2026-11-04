/**
 * dictation-auto.js — микрофон появляется сам: во всех полях, где что-то
 * вводят, и в тех, что появились позже (диалоги, палитра Ctrl+K, лента,
 * «Дальше», комментарии, настройки).
 *
 * Отдельный модуль: dictation.js — про запись и распознавание, здесь — только
 * поиск полей и расстановка кнопок. Публичные имена реэкспортирует
 * dictation.js (контракт §3 WAVE7-RULES).
 *
 * ЕДИНЫЙ КОМПОНЕНТ «поле + микрофон + действие» (см. docs/DESIGN-NOTES.md).
 * Кнопка микрофона везде одна и та же — того же вида, что в композере.
 * Правило размещения одно на всё приложение, без исключений по файлам:
 *
 *   1) рядом с полем В ТОЙ ЖЕ СТРОКЕ есть кнопка-действие («Ответить»,
 *      «Добавить», «Отправить», «Сохранить», «Искать») → микрофон —
 *      СОБСТВЕННАЯ ОТДЕЛЬНАЯ кнопка, стоит ПЕРЕД ней в потоке строки
 *      (никакого absolute: налезть на соседа физически не может);
 *   2) кнопки рядом нет (диалог комнаты, палитра Ctrl+K, настройки,
 *      указания участникам) → поле оборачивается в .mic-field
 *      (position: relative у ОБЁРТКИ САМОГО ПОЛЯ, а не у родителя-строки),
 *      кнопка встаёт в правом краю поля, текст получает padding-right.
 *
 * Пометки на полях:
 *   data-mic="off"    — не ставить никогда (ставим сами по общему правилу);
 *   data-mic="manual" — место занято явной кнопкой (композер);
 *   data-mic="auto"   — кнопку поставили мы (повторно не ставим).
 */

import { createMic } from './dictation.js';

const INPUT_TYPES = new Set(['text', 'search']);

/** Ширина, начиная с которой у отдельной кнопки видна подпись «Надиктовать». */
export const LABEL_MIN_WIDTH = 460;

/** Куда микрофон не ставим никогда: шапка, боковая панель, служебные слои. */
const NO_MIC_PLACE = '.topbar, .sidebar, .nav, .skip-link, .sr-only, .mic-panel, .toasts, [data-mic-skip]';

/**
 * Поля, где диктовка не нужна по смыслу: путь к файлу, дата, время, код.
 * Смотрим id/name/class — вид поля, а не его подпись (placeholder).
 */
const NO_MIC_NAME = /(^|[-_.\s])(path|paths|file|filename|dir|folder|url|uri|date|datetime|time|when|login|token|secret|code|hex)([-_.\s]|$)/i;

/**
 * Подпись поля тоже говорит о смысле: «Путь к файлу», «Дата», «Ссылка»,
 * «D:/Проекты/…/STATE.md» — диктовать туда нечего. Смотрим aria-label и
 * placeholder только на эти слова и на форму пути/ссылки, а не на весь текст:
 * «Напишите или надиктуйте запрос…» микрофон заслуживает.
 */
const NO_MIC_TEXT = /(^|[\s.,:;()«»"'—-])(путь|пути|путём|дат[аеуы]|даты|время|расписание)([\s.,:;()«»"'—-]|$)/i;
const PATH_LIKE = /^[A-Za-z]:[\/]|^~?[\/]|:\/\/|[\/][^\/\s]+\.(?:md|txt|json|csv|ya?ml|pdf|docx?|xlsx?|png|jpe?g|html?)$/i;

function warn(...args) {
  if (typeof console !== 'undefined' && console.warn) console.warn('[диктовка]', ...args);
}

function inputType(el) {
  const raw = el.getAttribute ? el.getAttribute('type') : '';
  return String(raw === null || raw === undefined || raw === '' ? 'text' : raw).trim().toLowerCase();
}

function tagOf(el) {
  return String((el && el.tagName) || '').toLowerCase();
}

/** Поле ввода, которое вообще имеет смысл озвучивать (без учёта пометок). */
function isTextField(el) {
  if (!el || el.nodeType !== 1) return false;
  const tag = tagOf(el);
  if (tag === 'textarea') return true;
  return tag === 'input' && INPUT_TYPES.has(inputType(el));
}

/** Служебное поле: путь, дата, поиск в шапке — микрофону тут не место. */
export function noMicPlace(el) {
  if (!el || el.nodeType !== 1) return false;
  if (el.closest) {
    try { if (el.closest(NO_MIC_PLACE)) return true; } catch { /* нет DOM-API */ }
  }
  const marks = [
    el.getAttribute && el.getAttribute('id'),
    el.getAttribute && el.getAttribute('name'),
    el.getAttribute && el.getAttribute('class'),
    el.getAttribute && el.getAttribute('data-field'),
  ];
  const text = marks.filter(Boolean).join(' ');
  if (text && NO_MIC_NAME.test(text)) return true;
  const hints = [
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('placeholder'),
  ].filter(Boolean).join(' ');
  if (!hints) return false;
  return NO_MIC_TEXT.test(hints) || PATH_LIKE.test(hints.trim());
}

/** Поле, где микрофон уместен и ещё не стоит. */
export function eligible(el) {
  if (!el || el.nodeType !== 1) return false;
  if (!isTextField(el)) return false;
  if (el.hasAttribute && (el.hasAttribute('readonly') || el.hasAttribute('disabled'))) return false;
  if (el.disabled === true || el.readOnly === true) return false;
  if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
  const mode = String((el.getAttribute && el.getAttribute('data-mic')) || '').trim().toLowerCase();
  if (mode) return false; // off | manual | auto — место занято или запрещено
  if (el.hasAttribute && (el.hasAttribute('data-mic-btn') || el.hasAttribute('data-mic-skip'))) return false;
  if (noMicPlace(el)) return false;
  return true;
}

/* ────────────────────────────────── правило размещения: кнопка рядом? */

/** Кнопка действия (не наша, видимая) — «Ответить», «Добавить», «Отправить». */
export function isActionButton(node) {
  if (!node || node.nodeType !== 1) return false;
  if (node.hasAttribute && node.hasAttribute('data-mic-btn')) return false;
  const tag = tagOf(node);
  const role = node.getAttribute ? String(node.getAttribute('role') || '') : '';
  if (tag !== 'button' && role !== 'button') return false;
  if (node.hidden === true || (node.getAttribute && node.getAttribute('hidden') !== null)) return false;
  if (node.classList && (node.classList.contains('hidden') || node.classList.contains('sr-only'))) return false;
  return true;
}

/** Строка или столбец? В столбце «микрофон перед кнопкой» смысла не имеет. */
function isRow(parent) {
  const cs = globalThis.getComputedStyle;
  if (typeof cs !== 'function' || !parent) return true;
  try {
    const style = cs(parent);
    if (!style) return true;
    const display = String(style.display || '');
    if (display.includes('flex') || display.includes('grid')) {
      return !String(style.flexDirection || '').startsWith('column');
    }
    return true;
  } catch { return true; }
}

/**
 * Кнопка-действие в той же строке, что и поле, — или null.
 * Только видимые элементы-соседи, не наши микрофоны.
 * @param {Element} el
 * @returns {Element|null}
 */
export function actionSibling(el) {
  const parent = el && el.parentNode;
  if (!parent || parent.nodeType !== 1) return null;
  const kids = parent.childNodes || [];
  let found = null;
  for (const node of kids) {
    if (node === el) continue;
    if (!isActionButton(node)) continue;
    found = node;
    break;
  }
  if (!found) return null;
  return isRow(parent) ? found : null;
}

/**
 * Чистое правило размещения — его и проверяют тесты.
 * @param {Element} el
 * @param {Element|null} action кнопка-действие рядом (или null)
 * @returns {'action'|'field'} action — отдельная кнопка перед действием; field — в обёртке поля
 */
export function placement(el, action) {
  const tag = tagOf(el);
  if (tag !== 'textarea' && tag !== 'input') return 'field';
  return action ? 'action' : 'field';
}

/* ─────────────────────────────────────────────── обёртка вокруг поля */

function wrapField(el) {
  const parent = el.parentNode;
  if (!parent || parent.nodeType !== 1) return null;
  if (parent.classList && parent.classList.contains('mic-field')) return parent;
  const d = globalThis.document;
  if (!d || typeof d.createElement !== 'function') return null;
  const holder = d.createElement('div');
  holder.className = 'mic-field';
  holder.setAttribute('data-mic-wrap', 'field');
  parent.insertBefore(holder, el);
  holder.appendChild(el);
  return holder;
}

/* ─────────────────────────────────────────── подпись там, где есть место */

export function wantLabel(width) {
  return Number(width) >= LABEL_MIN_WIDTH;
}

function widthOf(el) {
  if (!el) return 0;
  try {
    const rect = typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : null;
    if (rect && Number(rect.width) > 0) return Number(rect.width);
  } catch { /* нет раскладки */ }
  return Number(el.clientWidth) || 0;
}

/** Подпись «Надиктовать» — только в широкой строке; в узкой остаётся иконка. */
function fitLabel(btn, row) {
  const apply = () => {
    const wide = wantLabel(widthOf(row));
    if (typeof btn.classList.toggle === 'function') btn.classList.toggle('mic-btn--wide', wide);
  };
  apply();
  const RO = globalThis.ResizeObserver;
  if (typeof RO === 'function' && row && !btn.__micRO) {
    try {
      const ro = new RO(apply);
      ro.observe(row);
      btn.__micRO = ro;
    } catch { /* наблюдение не обязательно */ }
  }
}

/* ─────────────────────────────────────────── поставить кнопку в поле */

/**
 * Поставить кнопку микрофона в поле (data-mic="auto", повторно не ставим).
 * Место — по общему правилу `placement()`: перед кнопкой-действием или
 * в обёртке .mic-field. Никакого absolute у родителя-строки.
 * @param {HTMLInputElement|HTMLTextAreaElement} el
 * @param {object} [opts]
 * @returns {HTMLButtonElement|null}
 */
export function attachMic(el, opts = {}) {
  if (!eligible(el)) return null;
  el.setAttribute('data-mic', 'auto');
  const parent = el.parentNode;
  if (!parent) { el.removeAttribute('data-mic'); return null; }

  const action = actionSibling(el);
  if (placement(el, action) === 'action') {
    const btn = createMic(el, Object.assign({
      label: true, variant: 'secondary', className: 'mic-btn--action',
    }, opts || {}));
    parent.insertBefore(btn, action);
    fitLabel(btn, parent);
    return btn;
  }

  const holder = wrapField(el);
  if (!holder) { el.removeAttribute('data-mic'); return null; }
  const btn = createMic(el, Object.assign({ label: false, className: 'mic-btn--field' }, opts || {}));
  btn.classList.add(tagOf(el) === 'textarea' ? 'mic-btn--area' : 'mic-btn--input');
  holder.appendChild(btn);
  return btn;
}

/**
 * Добавить кнопки микрофона во все поля под `root`.
 * data-mic="off" — не ставить, "manual" — место уже занято явной кнопкой.
 * Служебным полям (путь, дата, поиск в шапке) ставим data-mic="off" сами —
 * одно общее правило вместо пометок в каждом файле.
 * @param {Document|HTMLElement} [root=document]
 * @returns {number} сколько кнопок добавили
 */
export function enhanceAll(root) {
  const scope = root || (typeof document !== 'undefined' ? document : null);
  if (!scope || typeof scope.querySelectorAll !== 'function') return 0;
  let list = [];
  try { list = Array.from(scope.querySelectorAll('textarea, input')); } catch { return 0; }
  if (scope.nodeType === 1 && isTextField(scope)) list.unshift(scope);
  let added = 0;
  for (const el of list) {
    if (!isTextField(el)) continue;
    if (eligible(el)) {
      try {
        if (attachMic(el)) added += 1;
      } catch (err) {
        warn('не удалось поставить микрофон', err);
      }
      continue;
    }
    // Служебное поле: помечаем один раз, чтобы в разметке было видно почему.
    try {
      if (!el.hasAttribute('data-mic') && !(el.readOnly || el.disabled) && noMicPlace(el)) {
        el.setAttribute('data-mic', 'off');
      }
    } catch { /* поле могло исчезнуть */ }
  }
  return added;
}

/* ─────────────────────────────────────────── слежение за DOM */

let observer = null;
let pending = null;

function scheduleEnhance() {
  if (pending) return;
  pending = setTimeout(() => {
    pending = null;
    enhanceAll(globalThis.document);
  }, 90);
}

/**
 * Следить за новыми полями (диалоги, палитра, лента, вкладки) и ставить
 * микрофон автоматически. Идемпотентно: повторный вызов возвращает тот же
 * наблюдатель.
 * @returns {MutationObserver|null}
 */
export function startAutoEnhance() {
  const d = globalThis.document;
  if (!d || !d.body) return null;
  if (observer) return observer;

  const run = () => { try { enhanceAll(d); } catch (err) { warn('autostart', err); } };
  if (d.readyState === 'loading') d.addEventListener('DOMContentLoaded', run, { once: true });
  else run();

  if (typeof globalThis.MutationObserver !== 'function') return null;
  observer = new globalThis.MutationObserver((records) => {
    for (const rec of records || []) {
      if (rec && rec.type === 'attributes') { scheduleEnhance(); return; }
      if (rec && rec.addedNodes && rec.addedNodes.length) { scheduleEnhance(); return; }
    }
  });
  observer.observe(d.body, { childList: true, subtree: true });
  return observer;
}

/** Остановить слежение (для тестов и перезапуска). */
export function stopAutoEnhance() {
  if (observer) { observer.disconnect(); observer = null; }
  if (pending) { clearTimeout(pending); pending = null; }
}

export default {
  enhanceAll, startAutoEnhance, stopAutoEnhance, attachMic, eligible,
  actionSibling, placement, wantLabel, noMicPlace,
};
