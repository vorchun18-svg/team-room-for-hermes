/**
 * store.js — минимальный observable без зависимостей.
 * Состояние всегда объект; set() — поверхностное слияние, подписчики получают
 * новое состояние синхронно (после одного микротаска — чтобы не рвать рендер).
 */

/**
 * @template T
 * @param {T} initial
 * @returns {{
 *   get: (key?: string) => any,
 *   set: (patch: Partial<T>|((s:T)=>Partial<T>)) => void,
 *   subscribe: (fn: (s:T)=>void, opts?: {immediate?:boolean}) => () => void,
 *   reset: () => void,
 * }}
 */
export function createStore(initial = {}) {
  const base = initial && typeof initial === 'object' ? initial : {};
  let state = Object.assign({}, base);
  const subs = new Set();
  let scheduled = false;

  function emit() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      const snapshot = state;
      for (const fn of Array.from(subs)) {
        try {
          fn(snapshot);
        } catch (err) {
          // Подписчик не должен ронять остальных.
          console.error('[store] subscriber failed', err);
        }
      }
    });
  }

  return {
    get(key) {
      if (key === undefined || key === null) return state;
      return state[key];
    },
    set(patch) {
      const next = typeof patch === 'function' ? patch(state) : patch;
      if (!next || typeof next !== 'object') return;
      const merged = Object.assign({}, state, next);
      let changed = false;
      for (const k of Object.keys(merged)) {
        if (!Object.is(merged[k], state[k])) { changed = true; break; }
      }
      if (!changed) return;
      state = merged;
      emit();
    },
    subscribe(fn, opts = {}) {
      if (typeof fn !== 'function') return () => {};
      subs.add(fn);
      if (opts.immediate !== false) {
        try { fn(state); } catch (err) { console.error('[store] subscriber failed', err); }
      }
      return () => subs.delete(fn);
    },
    reset() {
      state = Object.assign({}, base);
      emit();
    },
  };
}

/* --------------------------------------------------------------------------
   Замки действий (W28-D7, дефект 7: «кнопки умирают»).

   Почему это отдельный общий модуль, а не `btn.disabled = true` в каждом
   обработчике: кнопка, которую заблокировали «на время запроса», остаётся
   мёртвой навсегда, если ответ не пришёл (сервер завис, связь оборвалась,
   ноутбук уснул) или если перерисовки не случилось и снять замок в успешной
   ветке забыли. Для человека это выглядит как «кнопки перестали реагировать,
   помогает только выйти и зайти».

   Два правила, которые здесь зашиты:
   * `locked()` — замок снимается в `finally`, при любом исходе, и кнопка
     помечается `aria-busy`, пока ждём (видно, что идёт работа);
   * `withTimeout()` — ожидание всегда заканчивается: через `ACTION_TIMEOUT_MS`
     приходит понятная ошибка «сервер не ответил», а не тишина навсегда.
   -------------------------------------------------------------------------- */

/** Сколько ждём ответа на действие, прежде чем считать, что связи нет. */
export const ACTION_TIMEOUT_MS = 20000;

/**
 * Текущий предел ожидания. Пробе и проверкам нужен короткий предел
 * (`window.__teamroomLockTimeoutMs = 2500`), человеку — длинный: за него
 * укладывается и медленный сервер, и большая запись.
 * @returns {number}
 */
export function lockTimeout() {
  const v = Number(globalThis && globalThis.__teamroomLockTimeoutMs);
  return v > 0 ? v : ACTION_TIMEOUT_MS;
}

/**
 * Ограничить ожидание промиса. Возвращает ошибку `code:'timeout'`, если ответа
 * нет дольше `ms`. Исходный промис не отменяется (браузер сам закроет запрос),
 * но вызывающий код перестаёт ждать — замки снимаются, человек видит причину.
 *
 * @template T
 * @param {Promise<T>} promise
 * @param {number} [ms]
 * @param {string} [label] что именно ждали — попадёт в текст ошибки
 * @returns {Promise<T>}
 */
export function withTimeout(promise, ms = 0, label = 'Действие') {
  const wait = Number(ms) > 0 ? Number(ms) : lockTimeout();
  return new Promise((resolve, reject) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      const err = new Error(label + ' не ответило за ' + Math.round(wait / 1000)
        + ' с — связи с приложением нет. Кнопка снова доступна: повторите действие.');
      err.code = 'timeout';
      err.status = 0;
      err.timeout = true;
      reject(err);
    }, wait);
    Promise.resolve(promise).then(
      (value) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

/**
 * Выполнить действие под замком кнопки.
 *
 * Замок снимается в `finally` — и после ошибки, и после успеха, и после
 * таймаута. Перерисовка страницы во время запроса не мешает: если узел уже
 * заменён, мы просто отпускаем старый (новый пришёл без замка).
 *
 * @template T
 * @param {HTMLButtonElement|null|undefined} btn
 * @param {() => Promise<T>} task
 * @returns {Promise<T|undefined>} `undefined`, если кнопка уже была занята
 */
export async function locked(btn, task, opts = {}) {
  if (btn && btn.disabled) return undefined;
  if (btn) {
    try {
      btn.disabled = true;
      if (btn.setAttribute) btn.setAttribute('aria-busy', 'true');
    } catch { /* кнопка не из этого мира — идём дальше */ }
  }
  try {
    // Ожидание внутри замка тоже ограничено: иначе `finally` не наступит.
    return await withTimeout(task(), opts.ms || 0,
      opts.label || 'Действие');
  } finally {
    if (btn) {
      try {
        btn.disabled = false;
        if (btn.removeAttribute) btn.removeAttribute('aria-busy');
      } catch { /* узел уже выброшен перерисовкой */ }
    }
  }
}

/* --------------------------------------------------------------------------
   Глобальные сторы приложения. Формы намеренно «плоские», чтобы вьюхам было
   достаточно store.rooms.get('list').
   -------------------------------------------------------------------------- */

/** @type {{list: any[], loadedAt: number, loading: boolean, error: string|null}} */
export const rooms = createStore({ list: [], loadedAt: 0, loading: true, error: null });

/** @type {{id: string|null, room: object|null, phase: string|null}} */
export const currentRoom = createStore({ id: null, room: null, phase: null });

/** @type {{list: Array<{id:string, text:string, kind:string, at:number}>}} */
export const toasts = createStore({ list: [] });

/** @type {{pref: 'auto'|'light'|'dark', resolved: 'light'|'dark'}} */
export const theme = createStore({ pref: 'auto', resolved: 'light' });

/** @type {{loading: boolean, ok: boolean, error: string|null, data: object|null}} */
export const health = createStore({ loading: true, ok: false, error: null, data: null });

/** Все сторы одним объектом — то, что получает вьюха в ctx.store. */
export const stores = { rooms, currentRoom, toasts, theme, health };

export default stores;
