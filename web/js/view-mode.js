/**
 * view-mode.js — режим вида «простой / подробный» (W20-U3, пункт 18 / P14).
 *
 * «Простой» убирает из интерфейса технику: модель и её провайдера, внутренние
 * id, служебные строки, «сырые» подробности выполнения. Всё это НЕ удаляется из
 * данных — оно просто скрыто правилом CSS (`web/css/view-mode.css`) по
 * атрибуту `data-view-mode="simple"` на `<html>`, поэтому переключение мгновенное
 * и не требует перерисовки страниц.
 *
 * Где живёт выбор: только `localStorage` браузера (`teamroom.viewMode`), как тема.
 * По умолчанию — «подробный»: вид не меняется у того, кто ничего не нажимал.
 *
 *   import { attachViewModeButton, setViewMode, getViewMode } from '../view-mode.js';
 *   attachViewModeButton(document.querySelector('#viewmode-slot'));
 *
 * Модуль ничего не пишет на сервер и не зависит от данных комнаты.
 */

import { ensureCss } from './ui.js';
import { icon } from './icons.js';

/** Ключ localStorage: тот же стиль, что `teamroom.theme`. */
export const VIEW_MODE_KEY = 'teamroom.viewMode';
/** Два режима: полный (по умолчанию) и простой. */
export const VIEW_MODE_FULL = 'full';
export const VIEW_MODE_SIMPLE = 'simple';

/** Подписи кнопки и подсказки — одна формулировка на всё приложение. */
export const VIEW_MODE_TEXT = {
  full: 'Подробный вид',
  simple: 'Простой вид',
};

export const VIEW_MODE_HINT = {
  full: 'Показаны все подробности: модель и провайдер, внутренние номера, служебные строки. '
    + 'Нажмите, чтобы скрыть технику и оставить только суть.',
  simple: 'Скрыты модель и провайдер, внутренние номера и служебные строки — только то, '
    + 'что нужно для работы. Нажмите, чтобы вернуть подробности.',
};

/** Событие смены режима: на него подписан переключатель в сайдбаре. */
export const VIEW_MODE_EVENT = 'teamroom:viewmode';

/**
 * Привести значение к одному из двух режимов.
 * Неизвестное/пустое — «подробный» (обратная совместимость: было как сейчас).
 * @param {*} raw
 * @returns {'full'|'simple'}
 */
export function normalizeMode(raw) {
  return String(raw || '').trim().toLowerCase() === VIEW_MODE_SIMPLE
    ? VIEW_MODE_SIMPLE : VIEW_MODE_FULL;
}

/**
 * Прочитать сохранённый режим. Битое хранилище (приватный режим) не мешает работе.
 * @param {Storage|null} [storage]
 * @returns {'full'|'simple'}
 */
export function readStoredMode(storage) {
  const store = storage === undefined ? safeStorage() : storage;
  if (!store || typeof store.getItem !== 'function') return VIEW_MODE_FULL;
  try {
    return normalizeMode(store.getItem(VIEW_MODE_KEY));
  } catch {
    return VIEW_MODE_FULL;
  }
}

function safeStorage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

/** Последний применённый режим: `<html>` — источник истины, это запасной. */
let currentMode = null;

/** Текущий режим (по атрибуту на `<html>`, иначе из хранилища). */
export function getViewMode() {
  const root = globalThis.document && globalThis.document.documentElement;
  const applied = root && root.dataset ? root.dataset.viewMode : '';
  if (applied) return normalizeMode(applied);
  if (currentMode) return currentMode;
  return readStoredMode();
}

/** Простой ли вид сейчас. */
export function isSimple() {
  return getViewMode() === VIEW_MODE_SIMPLE;
}

/**
 * Применить режим к документу (атрибут + класс) и оповестить подписчиков.
 * @param {'full'|'simple'} [mode]
 * @param {{persist?: boolean, doc?: Document}} [opts]
 * @returns {'full'|'simple'} применённый режим
 */
export function applyViewMode(mode, opts = {}) {
  const value = normalizeMode(mode === undefined ? readStoredMode() : mode);
  const doc = opts.doc || globalThis.document;
  const root = doc && doc.documentElement;
  currentMode = value;
  if (root && root.dataset) {
    root.dataset.viewMode = value;
    if (root.classList && typeof root.classList.toggle === 'function') {
      root.classList.toggle('view-simple', value === VIEW_MODE_SIMPLE);
    }
  }
  if (opts.persist !== false) {
    const store = safeStorage();
    try {
      if (store && typeof store.setItem === 'function') store.setItem(VIEW_MODE_KEY, value);
    } catch { /* приватный режим — режим живёт до перезагрузки */ }
  }
  if (typeof globalThis.dispatchEvent === 'function' && typeof globalThis.CustomEvent === 'function') {
    try {
      globalThis.dispatchEvent(new globalThis.CustomEvent(VIEW_MODE_EVENT, { detail: { mode: value } }));
    } catch { /* старый браузер без CustomEvent */ }
  }
  return value;
}

/**
 * Переключить режим: «подробный» ↔ «простой».
 * @param {'full'|'simple'} [mode] явный режим (иначе — противоположный текущему)
 * @returns {'full'|'simple'}
 */
export function setViewMode(mode) {
  const next = mode === undefined
    ? (isSimple() ? VIEW_MODE_FULL : VIEW_MODE_SIMPLE)
    : normalizeMode(mode);
  return applyViewMode(next);
}

/** Переключить на противоположный режим (кнопка сайдбара, `V` на странице). */
export function toggleViewMode() {
  return setViewMode();
}

/** Подпись кнопки для текущего режима: показывает, ЧТО будет после нажатия. */
export function toggleLabel(mode) {
  return normalizeMode(mode) === VIEW_MODE_SIMPLE ? VIEW_MODE_TEXT.full : VIEW_MODE_TEXT.simple;
}

/**
 * Кнопка переключения для подвала бокового меню (рядом с темой).
 * Иконка — «глаз»: суть видна, подробности по желанию.
 * @param {{doc?: Document, onChange?: (mode: string) => void}} [opts]
 * @returns {HTMLElement|null}
 */
export function viewModeButton(opts = {}) {
  const doc = opts.doc || globalThis.document;
  if (!doc || typeof doc.createElement !== 'function') return null;
  const btn = doc.createElement('button');
  btn.type = 'button';
  btn.id = 'viewmode-toggle';
  btn.className = 'btn btn--ghost btn--sm viewmode-toggle';
  btn.dataset.action = 'view-mode';
  // Иконка «глаз» + короткая подпись: человек видит, что переключатель делает.
  if (typeof icon === 'function') btn.appendChild(icon('eye', { size: 15 }));
  const label = doc.createElement('span');
  label.setAttribute('data-viewmode-label', '');
  label.textContent = isSimple() ? 'просто' : 'подробно';
  btn.appendChild(label);
  paint(btn);
  btn.addEventListener('click', () => {
    const mode = toggleViewMode();
    paint(btn);
    if (typeof opts.onChange === 'function') opts.onChange(mode);
  });
  return btn;
}

/** Обновить подпись/иконку кнопки по текущему режиму. */
export function paint(btn) {
  if (!btn) return;
  const simple = isSimple();
  const label = simple ? 'Вид: простой (включить подробности)' : 'Вид: подробный (скрыть технику)';
  btn.setAttribute('aria-label', label);
  btn.setAttribute('title', simple ? VIEW_MODE_HINT.simple : VIEW_MODE_HINT.full);
  btn.setAttribute('aria-pressed', simple ? 'true' : 'false');
  const text = btn.querySelector ? btn.querySelector('[data-viewmode-label]') : null;
  if (text) text.textContent = simple ? 'просто' : 'подробно';
}

/**
 * Подписать готовую кнопку на события приложения: смена режима из другого места
 * (горячая клавиша, другая вкладка) сразу отражается на подписи.
 * @param {HTMLElement} btn
 * @returns {() => void} отписка
 */
export function watchViewMode(btn) {
  if (!btn) return () => {};
  const onChange = () => paint(btn);
  if (typeof globalThis.addEventListener === 'function') {
    globalThis.addEventListener(VIEW_MODE_EVENT, onChange);
    const off = () => globalThis.removeEventListener(VIEW_MODE_EVENT, onChange);
    return off;
  }
  return () => {};
}

// При загрузке модуля режим применяется сразу: страница не мигает «техникой»
// у того, кто выбрал простой вид. В среде без документа (тесты node) — молча.
if (globalThis.document && globalThis.document.documentElement) {
  applyViewMode(undefined, { persist: false });
  // Стили режима нужны до первой отрисовки — иначе простой вид «прыгнет».
  if (typeof ensureCss === 'function') {
    try { ensureCss('css/view-mode.css'); } catch { /* стили не критичны */ }
  }
}
