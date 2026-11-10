/**
 * notice-state.js — «эту плашку я уже закрыл» (B-24).
 *
 * Плашка ошибки должна закрываться, и закрытие должно пережить перерисовку по опросу —
 * но НЕ переживать новую попытку: если карточка упала снова (или появилась новая ошибка),
 * плашка показывается опять.
 *
 * Поэтому у каждой плашки есть штамп — «id карточки + номер попытки» (`failureStamp`
 * в request-parts-model.js). Состояние хранит именно штамп:
 *
 *     { "request:t_3b7e9d13": "t_3b7e9d13:2" }
 *
 * Совпал штамп — плашка скрыта; штамп другой (новая попытка) — показываем снова.
 *
 * Хранилище — `sessionStorage` по ключу запроса, иначе общая память модуля
 * (node --test, приватный режим). Модуль без DOM.
 */

/** Префикс ключа хранилища: закрытые плашки конкретного запроса. */
export const NOTICES_PREFIX = 'teamroom.notices.';

/** Префикс ключа хранилища: отметки «действие отправлено» (кнопка повтора, B-25). */
export const MARKS_PREFIX = 'teamroom.marks.';

const MEMORY = new Map();

function memoryStore() {
  return {
    get(key) { return MEMORY.has(key) ? MEMORY.get(key) : null; },
    set(key, value) { MEMORY.set(key, String(value)); },
  };
}

function defaultStore() {
  try {
    if (typeof sessionStorage !== 'undefined' && sessionStorage) {
      const probe = NOTICES_PREFIX + '__probe';
      sessionStorage.setItem(probe, '1');
      sessionStorage.removeItem(probe);
      return {
        get(key) { return sessionStorage.getItem(key); },
        set(key, value) { sessionStorage.setItem(key, String(value)); },
      };
    }
  } catch { /* нет доступа — память */ }
  return memoryStore();
}

/** Ключ хранилища для запроса. */
export function noticesKey(requestId) {
  const rid = String(requestId || '').trim();
  return NOTICES_PREFIX + (rid || 'unknown');
}

/**
 * Хранилище закрытых плашек запроса.
 *
 * @param {{requestId?: string, store?: {get:Function, set:Function}|null, key?: string}} [opts]
 */
export function createNotices(opts = {}) {
  const o = opts || {};
  const key = String(o.key || noticesKey(o.requestId));
  const store = o.store === null ? memoryStore() : (o.store || defaultStore());
  let data = read();

  function read() {
    try {
      const raw = store.get(key);
      if (!raw) return {};
      const value = JSON.parse(String(raw));
      return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    } catch { return {}; }
  }

  function save() {
    try { store.set(key, JSON.stringify(data)); } catch { /* переполнено — живём в памяти */ }
  }

  function slot(scope, id) {
    return String(scope || 'notice') + ':' + String(id || '');
  }

  return {
    key,
    /** Плашка со этим штампом уже закрыта? */
    isDismissed(scope, id, stamp) {
      const want = String(stamp || '');
      if (!want) return false;
      return data[slot(scope, id)] === want;
    },
    /** Закрыть плашку: запомнить штамп, при котором человек её закрыл. */
    dismiss(scope, id, stamp) {
      const want = String(stamp || '');
      if (!want) return false;
      data[slot(scope, id)] = want;
      save();
      return true;
    },
    /** Снять отметку (например, «показать снова» из меню). */
    restore(scope, id) {
      const k = slot(scope, id);
      if (!(k in data)) return false;
      delete data[k];
      save();
      return true;
    },
    /** Сколько плашек закрыто (тесты, отчёт). */
    size() { return Object.keys(data).length; },
    snapshot() { return Object.assign({}, data); },
  };
}

/**
 * Отметки о действиях, которые уже отправлены (B-25): кнопка помнит, что нажата.
 * Значение — произвольный объект (`{sig, at}`), живёт в той же sessionStorage-семье.
 *
 * @param {{requestId?: string, store?: {get:Function, set:Function}|null, key?: string}} [opts]
 */
export function createMarks(opts = {}) {
  const o = opts || {};
  const key = String(o.key || (MARKS_PREFIX + String(o.requestId || 'unknown')));
  const store = o.store === null ? memoryStore() : (o.store || defaultStore());
  let data = {};
  try {
    const raw = store.get(key);
    const parsed = raw ? JSON.parse(String(raw)) : {};
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) data = parsed;
  } catch { data = {}; }

  function save() {
    try { store.set(key, JSON.stringify(data)); } catch { /* переполнено — живём в памяти */ }
  }

  return {
    key,
    /** Отметка по ключу (`retryKey(rid, taskId)`), либо null. */
    get(id) {
      const value = data[String(id)];
      return value && typeof value === 'object' ? value : null;
    },
    mark(id, value) {
      data[String(id)] = Object.assign({}, value || {});
      save();
      return data[String(id)];
    },
    clear(id) {
      const k = String(id);
      if (!(k in data)) return false;
      delete data[k];
      save();
      return true;
    },
    snapshot() { return Object.assign({}, data); },
  };
}

export default { createNotices, noticesKey, createMarks };
