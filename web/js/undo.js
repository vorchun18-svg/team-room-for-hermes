/**
 * undo.js — общая «Отмена» на 10 секунд: один тост, одна кнопка «Вернуть».
 *
 * Правило волны 29 (Q7): необратимые действия — в архив, «не актуально»,
 * удаление в корзину, закрытие пункта — выполняются ОТЛОЖЕННО. Пока идёт
 * отсчёт, на сервере ничего не менялось, поэтому «Вернуть» — это не «откат»
 * и не второй запрос: задуманное действие просто не выполняется. Так же
 * устроена отмена в почте и в браузере, и человек не боится нажать.
 *
 * Один компонент на всё приложение: лента запросов (P4), страница запроса,
 * меню строки списка. Тост живёт в общем месте (#toasts) и переживает
 * перерисовку страницы — вкладки и лента могут меняться под ним.
 *
 * Проверки: tests/undo.test.mjs (node --test).
 */

import { h } from './util.js';
import { icon } from './icons.js';

/** Сколько секунд даётся на отмену (одно число на всё приложение). */
export const UNDO_MS = 10000;

/** Событие «отмена показана/снята» — для проверок и подсветки в интерфейсе. */
export const UNDO_EVENT = 'teamroom:undo';

let seq = 0;

/** Текст отсчёта: «Вернуть · 7 с» → «7 с» (одно место для подписи). */
export function leftLabel(left) {
  const n = Math.max(0, Math.round(Number(left) || 0));
  return n + ' с';
}

/**
 * Заголовок тоста отмены: одно действие — одна строка, много — «N запросов».
 * @param {string} text что именно случится («Запрос в архиве»)
 * @param {number} left сколько секунд осталось
 * @returns {string}
 */
export function undoText(text, left) {
  return String(text || 'Действие') + ' · ' + leftLabel(left);
}

/**
 * Компонент отмены.
 *
 * @param {{
 *   host?: HTMLElement|(() => HTMLElement|null),
 *   ms?: number,
 *   doc?: Document,
 *   icon?: (name:string, opts?:object) => Node,
 * }} [opts]
 * @returns {{
 *   offer: (o: object) => string,
 *   commit: (id: string) => Promise<boolean>,
 *   revert: (id: string) => Promise<boolean>,
 *   commitAll: () => Promise<number>,
 *   pending: () => object[],
 *   destroy: () => void,
 *   get size(): number,
 * }}
 */
export function createUndo(opts = {}) {
  const total = Math.max(0, Number(opts.ms === undefined ? UNDO_MS : opts.ms) || 0);
  const doc = opts.doc || (typeof document !== 'undefined' ? document : null);
  const hostOf = () => {
    if (typeof opts.host === 'function') return opts.host();
    if (opts.host) return opts.host;
    return doc && typeof doc.getElementById === 'function' ? doc.getElementById('toasts') : null;
  };
  const mkIcon = typeof opts.icon === 'function' ? opts.icon : icon;

  /** @type {Map<string, object>} */
  const items = new Map();

  function emit(kind, entry) {
    if (!doc || typeof doc.dispatchEvent !== 'function') return;
    if (typeof CustomEvent !== 'function') return;
    try {
      doc.dispatchEvent(new CustomEvent(UNDO_EVENT, { detail: {
        kind, id: entry.id, text: entry.text, left: Math.max(0, entry.left),
        pending: items.size,
      } }));
    } catch { /* старый браузер — событие не обязательно */ }
  }

  function clearTimers(entry) {
    if (entry.commitTimer) { clearTimeout(entry.commitTimer); entry.commitTimer = null; }
    if (entry.tickTimer) { clearInterval(entry.tickTimer); entry.tickTimer = null; }
  }

  /** Убрать тост из документа (не трогая выполнение действия). */
  function dropNode(entry) {
    const node = entry.node;
    entry.node = null;
    if (node && node.isConnected !== false && typeof node.remove === 'function') node.remove();
  }

  /** Новый узел тоста: текст, остаток секунд, кнопка «Вернуть». */
  function buildNode(entry) {
    const host = hostOf();
    if (!host) return null;
    const text = h('span', { class: 'undo__text' }, undoText(entry.text, entry.left));
    const btn = h('button', {
      class: 'btn btn--secondary btn--sm undo__btn', type: 'button',
      title: 'Отменить действие (пока идёт отсчёт ничего не изменилось)',
    }, mkIcon('debug-restart', { size: 12 }), 'Вернуть');
    btn.addEventListener('click', () => { revert(entry.id); });
    const node = h('div', {
      class: 'toast toast--undo', role: 'status', 'aria-live': 'polite',
      'data-undo-id': entry.id,
    }, mkIcon('clock', { size: 14 }), text, btn);
    entry.textNode = text;
    host.appendChild(node);
    return node;
  }

  function tick(entry) {
    const left = Math.max(0, Math.ceil((entry.deadline - Date.now()) / 1000));
    if (left === entry.left) return;
    entry.left = left;
    if (entry.textNode) entry.textNode.textContent = undoText(entry.text, left);
    emit('tick', entry);
  }

  /**
   * Показать отмену и отложить действие.
   *
   * @param {{
   *   text: string,                       — что произойдёт («Запрос в архиве»)
   *   action: () => (Promise|void),       — что выполнить через 10 с
   *   undo?: () => (Promise|void),        — что сделать при «Вернуть» (обычно ничего)
   *   onCommit?: (entry:object) => void,  — после выполнения (перерисовать список)
   *   onUndo?: (entry:object) => void,    — после отмены
   *   key?: string,                       — одно действие на ключ: прежнее выполнится сразу
   *   ms?: number,                        — свой срок (по умолчанию 10 с)
   * } o
   * @returns {string} id отмены
   */
  function offer(o) {
    const src = o || {};
    const key = src.key === undefined || src.key === null ? '' : String(src.key);
    if (key && items.has(key)) {
      // Повторное действие по тому же предмету: прежнее уже не «отменяемое» —
      // выполняем его, иначе два разных результата ждали бы одной кнопки.
      commit(key);
    }
    const id = key || ('undo-' + (++seq));
    const ms = Math.max(0, Number(src.ms === undefined ? total : src.ms) || 0);
    const entry = {
      id,
      key,
      text: String(src.text || 'Действие'),
      action: typeof src.action === 'function' ? src.action : null,
      undo: typeof src.undo === 'function' ? src.undo : null,
      onCommit: typeof src.onCommit === 'function' ? src.onCommit : null,
      onUndo: typeof src.onUndo === 'function' ? src.onUndo : null,
      ms,
      deadline: Date.now() + ms,
      left: Math.max(1, Math.ceil(ms / 1000)),
      done: false,
      node: null,
      textNode: null,
      commitTimer: null,
      tickTimer: null,
    };
    items.set(id, entry);
    entry.node = buildNode(entry);
    entry.commitTimer = setTimeout(() => { commit(id); }, ms);
    if (entry.node) {
      entry.tickTimer = setInterval(() => {
        if (!items.has(id)) { clearInterval(entry.tickTimer); return; }
        tick(entry);
      }, 1000);
    }
    emit('offer', entry);
    return id;
  }

  /** Выполнить отложенное действие сейчас (истёк срок, закрыли окно, повтор). */
  async function commit(id) {
    const entry = items.get(id);
    if (!entry) return false;
    items.delete(id);
    clearTimers(entry);
    dropNode(entry);
    if (entry.done) return false;
    entry.done = true;
    try {
      if (entry.action) await entry.action(entry);
    } catch (err) {
      if (typeof console !== 'undefined' && console.warn) {
        console.warn('[undo] действие не выполнено:', entry.text, err);
      }
    }
    if (entry.onCommit) {
      try { entry.onCommit(entry); } catch { /* перерисовка — украшение */ }
    }
    emit('commit', entry);
    return true;
  }

  /** «Вернуть»: ничего не выполняем, говорим, что отменено. */
  async function revert(id) {
    const entry = items.get(id);
    if (!entry) return false;
    items.delete(id);
    clearTimers(entry);
    dropNode(entry);
    if (entry.done) return false;
    entry.done = true;
    try {
      if (entry.undo) await entry.undo(entry);
    } catch (err) {
      if (typeof console !== 'undefined' && console.warn) console.warn('[undo] отмена не удалась', err);
    }
    if (entry.onUndo) {
      try { entry.onUndo(entry); } catch { /* ignore */ }
    }
    emit('revert', entry);
    return true;
  }

  /** Выполнить всё отложенное (страница уходит — обещание не должно пропасть). */
  async function commitAll() {
    const ids = Array.from(items.keys());
    for (const id of ids) await commit(id);
    return ids.length;
  }

  function pending() {
    return Array.from(items.values()).map((e) => ({
      id: e.id, text: e.text, left: Math.max(0, Math.ceil((e.deadline - Date.now()) / 1000)),
    }));
  }

  function destroy() {
    for (const entry of Array.from(items.values())) {
      items.delete(entry.id);
      clearTimers(entry);
      dropNode(entry);
    }
  }

  return {
    offer, commit, revert, commitAll, pending, destroy,
    get size() { return items.size; },
  };
}

/* ─────────────────────────────────────────────── общий экземпляр приложения */

let shared = null;

/** Общий компонент приложения (тосты в `#toasts`, срок — UNDO_MS). */
export function sharedUndo() {
  if (!shared) {
    shared = createUndo({
      host: () => (typeof document !== 'undefined' ? document.getElementById('toasts') : null),
    });
  }
  return shared;
}

/**
 * «Отмена» для вьюх: `import { undo } from '../undo.js'`.
 * Тонкая обёртка над общим экземпляром — один тост на всё приложение.
 */
export const undo = {
  offer: (o) => sharedUndo().offer(o),
  commit: (id) => sharedUndo().commit(id),
  revert: (id) => sharedUndo().revert(id),
  pending: () => sharedUndo().pending(),
};

/** Сбросить общий экземпляр (только тесты и повторная инициализация каркаса). */
export function resetSharedUndo() {
  if (shared) shared.destroy();
  shared = null;
}
