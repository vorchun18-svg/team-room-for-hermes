/**
 * request-tree-state.js — раскрытость узлов «Дерева работ» (B-19).
 *
 * Проблема (Defects-2026-11-06-second-round, B-19): `request-parts-tree.js` раскрывал
 * карточки с ошибкой и «ждёт тебя» при КАЖДОЙ отрисовке, а выбор пользователя жил только
 * в DOM. Страница перерисовывается по опросу — ветка захлопывалась обратно или, наоборот,
 * раскрывалась после того, как её свернули.
 *
 * Здесь состояние живёт в ДАННЫХ, а не в DOM:
 *   * `open[id]`          — раскрыт ли узел сейчас (единственный источник для отрисовки);
 *   * `user[id]`          — выбор пользователя и подпись проблемы, при которой он сделан;
 *                            его воля главнее авто-раскрытия;
 *   * `auto[id]`          — при какой подписи проблемы узел уже раскрывали автоматически
 *                            (значит, второй раз — нет).
 *
 * Подпись проблемы (`problemSignature`) меняется при новой попытке/новой ошибке — тогда
 * авто-раскрытие срабатывает снова, но только один раз (правило ТЗ: «авто-раскрывать
 * только при первом появлении ошибки»).
 *
 * Хранилище: `sessionStorage` (переживает перерисовку и F5 в пределах вкладки), а если его
 * нет (node --test, приватный режим) — общая память модуля, чтобы поведение не отличалось.
 * Модуль без DOM: проверяется в node --test.
 */

/** Префикс ключа sessionStorage: на каждый запрос — своё состояние. */
export const TREE_STATE_PREFIX = 'teamroom.tree.open.';

/** Сколько узлов помнить; больше — считаем мусором и начинаем заново. */
const MAX_IDS = 400;

const MEMORY = new Map();

function memoryStore() {
  return {
    get(key) { return MEMORY.has(key) ? MEMORY.get(key) : null; },
    set(key, value) { MEMORY.set(key, String(value)); },
  };
}

/** sessionStorage как простое хранилище; нет доступа — память (не падаем). */
export function defaultStore() {
  try {
    if (typeof sessionStorage !== 'undefined' && sessionStorage) {
      const probe = TREE_STATE_PREFIX + '__probe';
      sessionStorage.setItem(probe, '1');
      sessionStorage.removeItem(probe);
      return {
        get(key) { return sessionStorage.getItem(key); },
        set(key, value) { sessionStorage.setItem(key, String(value)); },
      };
    }
  } catch { /* приватный режим/запрет хранилища — работаем в памяти */ }
  return memoryStore();
}

/** Ключ хранилища для запроса. */
export function treeStateKey(requestId) {
  const rid = String(requestId || '').trim();
  return TREE_STATE_PREFIX + (rid || 'unknown');
}

/**
 * Подпись проблемы узла: пустая, если проблемы нет.
 * Ошибка и «ждёт тебя» — разные подписи; новая попытка или новый текст ошибки меняют её.
 *
 * @param {object} node TaskNode
 * @returns {string}
 */
export function problemSignature(node) {
  const n = node && typeof node === 'object' ? node : {};
  const bits = [];
  const error = n.error && typeof n.error === 'object' ? n.error : null;
  if (error) {
    bits.push('err:' + String(error.code || '') + ':' + String(error.message || '').slice(0, 120));
  }
  if (n.needs_human === true) bits.push('human');
  if (!bits.length) return '';
  const runs = Array.isArray(n.runs) ? n.runs : [];
  bits.push('n' + String(runs.length));
  return bits.join('|');
}

function normalize(data) {
  const out = { open: {}, user: {}, auto: {} };
  const src = data && typeof data === 'object' ? data : {};
  const open = src.open && typeof src.open === 'object' ? src.open : {};
  for (const id of Object.keys(open)) if (open[id]) out.open[String(id)] = true;
  const user = src.user && typeof src.user === 'object' ? src.user : {};
  for (const id of Object.keys(user)) {
    const item = user[id] && typeof user[id] === 'object' ? user[id] : null;
    if (!item) continue;
    out.user[String(id)] = { open: item.open === true, sig: String(item.sig || '') };
  }
  const auto = src.auto && typeof src.auto === 'object' ? src.auto : {};
  for (const id of Object.keys(auto)) out.auto[String(id)] = String(auto[id] || '');
  return out;
}

/**
 * Состояние раскрытости дерева запроса.
 *
 * @param {{requestId?: string, store?: {get:Function, set:Function}|null, key?: string}} [opts]
 */
export function createTreeState(opts = {}) {
  const o = opts || {};
  const key = String(o.key || treeStateKey(o.requestId));
  const store = o.store === null ? memoryStore() : (o.store || defaultStore());
  let data = normalize(read());

  function read() {
    try {
      const raw = store.get(key);
      if (!raw) return null;
      return JSON.parse(String(raw));
    } catch { return null; }
  }

  function save() {
    const ids = Object.keys(data.open);
    if (ids.length > MAX_IDS) data = { open: {}, user: {}, auto: {} };
    try { store.set(key, JSON.stringify(data)); } catch { /* хранилище переполнено — живём в памяти */ }
  }

  function setOpen(id, open, signature) {
    const k = String(id);
    const sig = String(signature || '');
    data.open[k] = open === true;
    data.user[k] = { open: open === true, sig };
    if (!open) data.auto[k] = sig;
    save();
    return open === true;
  }

  return {
    key,
    /** Раскрыт ли узел сейчас. */
    isOpen(id) {
      return data.open[String(id)] === true;
    },
    /** Запомнить «раскрыт/свёрнут» по выбору пользователя (главнее авто-раскрытия). */
    setOpen,
    /** Клик по кнопке раскрытия: переключает и запоминает выбор пользователя. */
    toggle(id, signature) {
      return setOpen(id, !data.open[String(id)], signature);
    },
    /**
     * Авто-раскрытие проблемного узла: один раз на подпись проблемы.
     * Выбор пользователя при той же проблеме главнее — не раскрываем.
     * @returns {boolean} изменилось ли состояние
     */
    autoOpen(id, signature) {
      const k = String(id);
      const sig = String(signature || '');
      if (!sig) return false;
      const choice = data.user[k];
      if (choice && choice.sig === sig) return false;   // человек уже решил сам
      if (data.auto[k] === sig) return false;           // эту проблему уже раскрывали
      data.auto[k] = sig;
      if (data.open[k] !== true) {
        data.open[k] = true;
        save();
        return true;
      }
      save();
      return false;
    },
    /** Убрать из памяти узлы, которых больше нет в дереве. */
    retain(ids) {
      const list = Array.isArray(ids) ? ids.map(String) : [];
      if (!list.length) return;
      const keep = new Set(list);
      let changed = false;
      for (const field of ['open', 'user', 'auto']) {
        for (const id of Object.keys(data[field])) {
          if (keep.has(id)) continue;
          delete data[field][id];
          changed = true;
        }
      }
      if (changed) save();
    },
    /** Отладочный снимок (тесты, отчёт). */
    snapshot() {
      return {
        open: Object.keys(data.open).filter((id) => data.open[id] === true).sort(),
        user: Object.assign({}, data.user),
        auto: Object.assign({}, data.auto),
      };
    },
  };
}

export default { createTreeState, problemSignature, treeStateKey, defaultStore };
