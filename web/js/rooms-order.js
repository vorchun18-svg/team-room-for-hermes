/**
 * rooms-order.js — устройство левого меню: порядок комнат, закрепление, группы.
 *
 * Здесь ТОЛЬКО логика без DOM: её гоняют тесты в node, а сайдбар (`shell-sidebar.js`)
 * рисует по ней. Один источник порядка для сервера и интерфейса:
 *   * `order`  — целое ≥ 1, место комнаты в общем списке (0 — «ещё не задавали»);
 *   * `pinned` — закреплённые комнаты всегда выше остальных;
 *   * `group`  — id группы из списка `groups` ("" — без группы).
 *
 * Сервер уже отдаёт комнаты в этом порядке (`rooms._menu_key`), поэтому список с
 * сервера не пересортировывается — только группируется. `menuRooms()` нужен для
 * локального (оптимистичного) порядка в момент перетаскивания, до ответа сервера.
 */

export const ORDER_ENDPOINT = '/api/rooms/order';
export const MAX_GROUP_TITLE = 60;
export const GROUP_ID_RE = /^[a-z0-9][a-z0-9_-]{0,23}$/;
/** С какого числа знак «перетащить» показывается: на узком экране он мешает. */
export const DRAG_MIN_WIDTH = 640;

/** Номер места комнаты: `order` ≥ 1, иначе «в конец» (по времени создания). */
export function orderOf(room) {
  const n = Number(room && room.order);
  return Number.isFinite(n) && n >= 1 ? Math.floor(n) : 0;
}

export function isPinned(room) {
  return Boolean(room && room.pinned === true);
}

/** Ключ сортировки меню — тот же, что на сервере (`rooms._menu_key`). */
export function menuKey(room) {
  const order = orderOf(room);
  return [
    isPinned(room) ? 0 : 1,
    order || Number.MAX_SAFE_INTEGER,
    Number((room && room.created_at) || 0),
    String((room && room.id) || ''),
  ];
}

function compareKeys(a, b) {
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

/** Комнаты в порядке меню (не меняет исходный массив). */
export function menuRooms(rooms) {
  return (Array.isArray(rooms) ? rooms.slice() : [])
    .sort((a, b) => compareKeys(menuKey(a), menuKey(b)));
}

/** Группы с сервера → чистый список: id по правилу, название, «свёрнута». */
export function normalizeGroups(raw) {
  const out = [];
  const seen = new Set();
  for (const item of Array.isArray(raw) ? raw : []) {
    if (!item || typeof item !== 'object') continue;
    const id = String(item.id || '').trim().toLowerCase();
    if (!GROUP_ID_RE.test(id) || seen.has(id)) continue;
    const title = String(item.title || '').trim().split(/\s+/).join(' ').slice(0, MAX_GROUP_TITLE);
    if (!title) continue;
    seen.add(id);
    out.push({ id, title, collapsed: item.collapsed === true });
  }
  return out;
}

/**
 * Разделы меню: «Закреплённые» (без названия), группы по порядку и «Без группы».
 *
 * Комната, чья группа не существует, показывается в «Без группы» — она не
 * пропадает из меню из-за опечатки в реестре.
 * @returns {{kind:'pinned'|'group'|'plain', id:string, title:string,
 *            collapsed:boolean, rooms:object[]}[]}
 */
export function menuSections(rooms, groups) {
  const list = menuRooms(rooms);
  const sections = [];
  const pinned = list.filter(isPinned);
  if (pinned.length) {
    sections.push({ kind: 'pinned', id: '', title: 'Закреплённые', collapsed: false, rooms: pinned });
  }
  const rest = list.filter((room) => !isPinned(room));
  const known = new Set();
  for (const group of normalizeGroups(groups)) {
    known.add(group.id);
    sections.push({ kind: 'group', id: group.id, title: group.title,
      collapsed: group.collapsed, rooms: rest.filter((r) => String(r.group || '') === group.id) });
  }
  const loose = rest.filter((r) => !known.has(String(r.group || '')));
  if (loose.length) {
    sections.push({ kind: 'plain', id: '', title: '', collapsed: false, rooms: loose });
  }
  return sections;
}

/** Плоский список id в порядке разделов — то, что уходит на сервер как `order`. */
export function sectionIds(sections) {
  return (Array.isArray(sections) ? sections : [])
    .flatMap((section) => (section.rooms || []).map((room) => String(room.id)));
}

/**
 * Переставить комнату: `dragId` перед/после `targetId`.
 *
 * `place`: 'before' | 'after'. Возвращает НОВЫЙ массив id; неизвестные id
 * игнорируются (перетаскивание за пределы списка порядок не ломает).
 */
export function moveRoom(ids, dragId, targetId, place = 'before') {
  const list = (Array.isArray(ids) ? ids : []).map(String);
  const from = list.indexOf(String(dragId));
  const to = list.indexOf(String(targetId));
  if (from < 0 || to < 0 || from === to) return list;
  const next = list.slice();
  next.splice(from, 1);
  let insert = next.indexOf(String(targetId));
  if (insert < 0) return list;
  if (place === 'after') insert += 1;
  next.splice(insert, 0, String(dragId));
  return next;
}

/** Сдвиг на шаг (Alt+↑/↓): −1 вверх, +1 вниз; за край — без изменений. */
export function stepRoom(ids, roomId, delta) {
  const list = (Array.isArray(ids) ? ids : []).map(String);
  const from = list.indexOf(String(roomId));
  const to = from + (Number(delta) || 0);
  if (from < 0 || to < 0 || to >= list.length || from === to) return list;
  const next = list.slice();
  next.splice(from, 1);
  next.splice(to, 0, String(roomId));
  return next;
}

/**
 * Куда вставить перетаскиваемую строку.
 * @param {{top:number, height:number}[]} rects прямоугольники строк СВЕРХУ ВНИЗ
 * @param {number} y координата курсора
 * @returns {number} индекс вставки 0…rects.length
 */
export function dropIndex(rects, y) {
  const rows = Array.isArray(rects) ? rects : [];
  for (let i = 0; i < rows.length; i += 1) {
    const rect = rows[i] || {};
    const top = Number(rect.top) || 0;
    const height = Number(rect.height) || 0;
    if (y < top + height / 2) return i;
  }
  return rows.length;
}

/** Тело запроса раскладки: порядок + правки комнат (закрепление, группа). */
export function layoutPayload(ids, patch = {}) {
  const body = { order: (Array.isArray(ids) ? ids : []).map(String) };
  const rooms = {};
  for (const [rid, item] of Object.entries(patch && typeof patch === 'object' ? patch : {})) {
    if (!item || typeof item !== 'object') continue;
    const one = {};
    if (typeof item.pinned === 'boolean') one.pinned = item.pinned;
    if (typeof item.group === 'string') one.group = item.group;
    if (Object.keys(one).length) rooms[String(rid)] = one;
  }
  if (Object.keys(rooms).length) body.rooms = rooms;
  return body;
}

/** Ядро тела: только закрепление (кнопка 📌) — порядок не трогаем. */
export function pinPayload(roomId, pinned) {
  return { rooms: { [String(roomId)]: { pinned: Boolean(pinned) } } };
}

/** id новой группы из названия: «Работа» → «rabota» (латиница, как id комнат). */
const TRANSLIT = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y',
  к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f',
  х: 'h', ц: 'c', ч: 'ch', ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
};

export function groupIdFromTitle(title, taken = []) {
  const used = new Set((Array.isArray(taken) ? taken : []).map((id) => String(id)));
  let slug = '';
  for (const ch of String(title || '').trim().toLowerCase()) {
    if (TRANSLIT[ch] !== undefined) slug += TRANSLIT[ch];
    else if (/[a-z0-9]/.test(ch)) slug += ch;
    else slug += '-';
  }
  slug = slug.replace(/-{2,}/g, '-').replace(/^-+|-+$/g, '').slice(0, 24);
  if (!slug || !/^[a-z0-9]/.test(slug)) slug = ('g-' + slug).slice(0, 24);
  let candidate = slug;
  let n = 2;
  while (used.has(candidate)) {
    const suffix = '-' + n;
    candidate = slug.slice(0, 24 - suffix.length) + suffix;
    n += 1;
  }
  return candidate;
}

/** Можно ли тянуть в текущем режиме: в полосе значков и на узком экране — нет. */
export function canDrag(view = {}) {
  if (view.rail) return false;
  const width = Number(view.width);
  if (Number.isFinite(width) && width > 0 && width < DRAG_MIN_WIDTH) return false;
  return true;
}

/**
 * План перетаскивания: куда встанет комната и что поменять в её полях.
 *
 * Считается по РАЗДЕЛАМ меню (закреплённые / группы / без группы), поэтому
 * один и тот же вызов обслуживает и перестановку внутри группы, и перенос
 * комнаты в другую группу, и «вытащить из группы».
 *
 * @param {object[]} sections разделы из `menuSections()`
 * @param {string} dragId  какую комнату тянут
 * @param {string} sectionKey ключ целевого раздела: '__pinned__', id группы, ''
 * @param {number} index место в целевом разделе (0…длина)
 * @returns {{sections: object[], ids: string[], patch: object}|null}
 */
export function planDrop(sections, dragId, sectionKey, index) {
  const list = Array.isArray(sections) ? sections : [];
  const keyOf = (s) => (s.kind === 'pinned' ? '__pinned__' : String(s.id || ''));
  let dragged = null;
  for (const section of list) {
    const hit = (section.rooms || []).find((r) => String(r.id) === String(dragId));
    if (hit) dragged = hit;
  }
  if (!dragged) return null;
  const copy = list.map((s) => ({ ...s, rooms: (s.rooms || []).filter((r) => String(r.id) !== String(dragId)) }));
  let target = copy.find((s) => keyOf(s) === String(sectionKey));
  if (!target) return null;
  const at = Math.max(0, Math.min(Number(index) || 0, target.rooms.length));
  target.rooms.splice(at, 0, dragged);
  const patch = {};
  const pinned = target.kind === 'pinned';
  if (pinned !== isPinned(dragged)) patch.pinned = pinned;
  const group = target.kind === 'group' ? target.id : '';
  if (String(dragged.group || '') !== group) patch.group = group;
  return { sections: copy, ids: sectionIds(copy), patch };
}
