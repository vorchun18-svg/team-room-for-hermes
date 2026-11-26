/**
 * shell-aside-sheet.js — «От вас нужно» нижним листом (волна 25, пакет L1, R5 §7.5).
 *
 * Правило: колонка не отнимает ширину у свода, если центру остаётся меньше 640 px.
 * Поэтому при виде < 1100 (режимы m/s/xs) правая колонка превращается в нижний
 * лист 56 px: «От вас нужно 5  ↑». Разворот — поверх содержания (60 % высоты,
 * `Esc` закрывает, фон не прокручивается), при виде < 900 — на весь экран
 * (`100dvh − 40px`), чтобы форма ответа была целиком видна.
 *
 * Здесь — только оболочка листа: высота, разворот, клавиши, блокировка фона.
 * Что внутри (какие вопросы, сколько форм) — пакет L2; этот модуль не трогает
 * содержимое колонки, он её только открывает и закрывает.
 *
 * Состояние «где живёт колонка» ставит layout-mode.js (`data-aside` на `#app`).
 * Ручной выбор пользователя (teamroom.aside) сильнее автоматики.
 */

import { h } from './util.js';
import { icon } from './icons.js';

/** Класс на `#app`, пока лист развёрнут. */
export const OPEN_CLASS = 'is-sheet-open';
/** Класс ручки листа. */
export const HANDLE_CLASS = 'req-sheet__handle';

/** Подпись ручки: «От вас нужно · 5» / «От вас нужно». */
export function handleText(count) {
  const n = Number(count) || 0;
  return n > 0 ? 'От вас нужно · ' + n : 'От вас нужно';
}

/**
 * Доступная подпись ручки: то же число, но с областью — на вкладке комнаты
 * число другое (по всей комнате), и человек не должен гадать, почему.
 */
export function handleAria(count) {
  const n = Number(count) || 0;
  return n > 0
    ? 'От вас нужно: ' + n + ' в этой задаче, развернуть'
    : 'От вас нужно, развернуть';
}

/**
 * Сколько ждёт ответа — по уже отрисованным строкам колонки (без новых правил
 * подсчёта: считаем то, что видит человек).
 * @param {HTMLElement} aside
 * @returns {number}
 */
export function countWaiting(aside) {
  if (!aside || !aside.querySelectorAll) return 0;
  const rows = aside.querySelectorAll('.req-q, .ap-aside__item, [data-question-id]');
  let n = 0;
  for (const row of rows) {
    if (row.querySelector && row.querySelector('textarea')) n += 1;
  }
  return n;
}

/**
 * Подключить лист к странице. Повторный вызов безопасен (старая ручка снимается).
 * @param {{app: HTMLElement, view?: HTMLElement, win?: Window}} opts
 * @returns {{sync: Function, open: Function, close: Function, isOpen: Function, destroy: Function}}
 */
export function installAsideSheet(opts = {}) {
  const app = opts.app;
  const win = opts.win || (typeof window !== 'undefined' ? window : null);
  const view = opts.view || (app && app.querySelector ? app.querySelector('#view') : null);
  if (!app) return { sync() {}, open() {}, close() {}, isOpen: () => false, destroy() {} };

  let handle = null;
  let observer = null;
  let onKey = null;
  let raf = 0;

  // W30-QA2: `view` приходит из оболочки один раз, а оболочка может
  // пересобрать `#view` (и страница останется с колонкой без ручки — на узком
  // экране вопросы становились недоступны). Поэтому каждый раз берём ЖИВОЙ
  // `#view` и переподключаем наблюдатель, если узел сменился.
  let watched = view;
  const liveView = () => {
    if (view && view.isConnected) return view;
    const root = (typeof document !== 'undefined') ? document : null;
    return (root && root.getElementById) ? root.getElementById('view') : null;
  };
  const asideOf = () => {
    const v = liveView();
    return (v && v.querySelector) ? v.querySelector('.req__aside') : null;
  };
  const isSheet = () => app.dataset && app.dataset.aside === 'sheet';

  function isOpen() {
    return app.classList.contains(OPEN_CLASS);
  }

  function open() {
    if (!isSheet()) return;
    app.classList.add(OPEN_CLASS);
    if (handle) handle.setAttribute('aria-expanded', 'true');
  }

  function close() {
    app.classList.remove(OPEN_CLASS);
    if (handle) handle.setAttribute('aria-expanded', 'false');
  }

  function toggle() {
    if (isOpen()) close(); else open();
  }

  /** Ручка листа: одна строка 56 px, вся — цель нажатия. */
  function buildHandle(aside) {
    const btn = h('button', {
      type: 'button',
      class: HANDLE_CLASS,
      'aria-expanded': isOpen() ? 'true' : 'false',
      'aria-controls': 'req-aside-panel',
      title: 'В этой задаче ждут ответа: ' + (Number(countWaiting(aside)) || 0)
        + '. На вкладке «От вас нужно» — по всей комнате',
      'aria-label': handleAria(countWaiting(aside)),
      onclick: (ev) => {
        if (ev && ev.preventDefault) ev.preventDefault();
        toggle();
      },
    },
    h('span', { class: 'req-sheet__label' }, handleText(countWaiting(aside))),
    h('span', { class: 'req-sheet__hint' }, 'валюта · объём · логистика'),
    icon('chevron-up', { size: 14, className: 'req-sheet__chev' }));
    return btn;
  }

  function sync() {
    const v = liveView();
    if (MO && v && v !== watched) {
      // Оболочка пересобрала #view: старый наблюдатель смотрит в оторванный узел.
      if (observer) observer.disconnect();
      observer = new MO(schedule);
      observer.observe(v, { childList: true, subtree: true });
      watched = v;
    }
    const aside = asideOf();
    if (!isSheet() || !aside) {
      if (handle && handle.isConnected) handle.remove();
      handle = null;
      close();
      return;
    }
    if (handle && handle.isConnected && handle.parentElement === aside) {
      const label = handle.querySelector('.req-sheet__label');
      if (label) label.textContent = handleText(countWaiting(aside));
      return;
    }
    if (handle && handle.isConnected) handle.remove();
    handle = buildHandle(aside);
    aside.insertBefore(handle, aside.firstChild);
    if (!aside.id) aside.id = 'req-aside-panel';
  }

  const schedule = () => {
    if (raf) return;
    // W30-QA2: `requestAnimationFrame` замирает в фоновой вкладке — тогда ручка
    // листа не появлялась вовсе и вопросы на узком экране были недоступны.
    // Обычный таймер даёт ту же задержку и работает всегда.
    const run = () => { raf = 0; sync(); };
    raf = setTimeout(run, 16);
  };

  // Страница перерисовывается точечно (живое обновление): следим за составом вида.
  const MO = win && win.MutationObserver ? win.MutationObserver : null;
  if (MO && view) {
    observer = new MO(schedule);
    observer.observe(view, { childList: true, subtree: true });
  }
  if (win && win.addEventListener) {
    onKey = (ev) => {
      if (!ev || ev.key !== 'Escape' || !isOpen()) return;
      close();
      if (handle && handle.focus) handle.focus({ preventScroll: true });
    };
    win.addEventListener('keydown', onKey);
    win.addEventListener('teamroom:layout', schedule);
  }

  sync();

  return {
    sync: schedule,
    open,
    close,
    isOpen,
    destroy() {
      if (observer) observer.disconnect();
      if (win && win.removeEventListener && onKey) {
        win.removeEventListener('keydown', onKey);
        win.removeEventListener('teamroom:layout', schedule);
      }
      if (handle && handle.isConnected) handle.remove();
      close();
    },
  };
}

export default { installAsideSheet, handleText, countWaiting };
