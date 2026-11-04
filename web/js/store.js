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
