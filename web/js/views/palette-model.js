/**
 * palette-model.js — чистая логика командной палитры (Ctrl+K).
 *
 * Ни DOM, ни сети: только нормализация текста, нечёткий поиск (subsequence с
 * бонусами за начало слова и слитный фрагмент, ё≡е), сборка списка команд и
 * раскладка результатов по группам. Всё это покрыто tests/palette-model.test.mjs.
 *
 * Элемент списка (item):
 *   {id, group, title, hint?, icon?, keywords?, action, score?}
 * action — данные для вьюхи палитры:
 *   {type:'navigate', hash} | {type:'new-request', room} |
 *   {type:'create-room'} | {type:'theme', value:'auto'|'light'|'dark'}
 */

/** Разделители слов: по ним даём бонус «начало слова». */
const SEPARATORS = new Set([' ', '\t', '\n', '-', '_', '/', '\\', '.', ',', ':', ';', '(', ')', '[', ']', '«', '»', '"', "'", '·', '|', '—', '–']);

/**
 * Нормализация для сравнения: нижний регистр, ё→е, схлопнутые пробелы.
 * Длина строки не меняется (важно для подсветки), кроме обрезки краёв.
 * @param {*} value
 * @returns {string}
 */
export function normalize(value) {
  let s = value === null || value === undefined ? '' : String(value);
  s = s.replace(/ё/g, 'е').replace(/Ё/g, 'Е').toLowerCase();
  return s.replace(/\s+/g, ' ').trim();
}

/** Признак «начало слова» в позиции i нормализованной строки. */
function startsWord(text, i) {
  if (i === 0) return true;
  return SEPARATORS.has(text[i - 1]);
}

/**
 * Нечёткое совпадение query как подпоследовательности text.
 * Возвращает score (чем больше — тем лучше) и позиции совпавших символов.
 * null — не совпало.
 * @param {string} text
 * @param {string} query
 * @returns {{score:number, indices:number[]}|null}
 */
export function fuzzyMatch(text, query) {
  const hay = normalize(text);
  const needle = normalize(query);
  if (!needle) return { score: 0, indices: [] };
  if (!hay) return null;

  const indices = [];
  let cursor = 0;
  let raw = 0;
  let prev = -2;
  for (const ch of needle) {
    if (ch === ' ') continue; // пробелы в запросе не ищем посимвольно
    const found = hay.indexOf(ch, cursor);
    if (found < 0) return null;
    let bonus = 1;
    if (found === prev + 1) bonus += 2; // слитный фрагмент
    if (startsWord(hay, found)) bonus += 2; // начало слова
    if (found === 0) bonus += 2; // самое начало строки
    raw += bonus;
    indices.push(found);
    prev = found;
    cursor = found + 1;
  }
  if (!indices.length) return { score: 0, indices: [] };

  // Нормируем: короткая строка и точное вхождение — выше.
  const perChar = raw / (indices.length * 5);
  const lengthPenalty = Math.min(0.4, hay.length / 260);
  const exactBonus = hay === needle ? 0.5 : hay.startsWith(needle) ? 0.25 : 0;
  const score = Math.max(0, perChar * (1 - lengthPenalty) + exactBonus);
  return { score: Number(score.toFixed(4)), indices };
}

/**
 * Score или -1, если не совпало.
 * @param {string} text
 * @param {string} query
 * @returns {number}
 */
export function fuzzyScore(text, query) {
  const m = fuzzyMatch(text, query);
  return m ? m.score : -1;
}

/** Диапазоны [start, end) вхождений токенов запроса в нормализованный текст. */
function tokenRanges(text, query) {
  const hay = normalize(text);
  const q = normalize(query);
  if (!hay || !q) return [];
  const tokens = q.split(' ').filter((t) => t.length >= 2);
  if (!tokens.length) tokens.push(q);
  const ranges = [];
  for (const token of tokens) {
    let from = 0;
    for (;;) {
      const at = hay.indexOf(token, from);
      if (at < 0) break;
      ranges.push([at, at + token.length]);
      from = at + token.length;
    }
  }
  ranges.sort((a, b) => a[0] - b[0]);
  // Склейка пересекающихся/смежных диапазонов.
  const merged = [];
  for (const [start, end] of ranges) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

/**
 * Порезать текст на сегменты для подсветки: [{text, match}] — из них вьюха
 * строит <mark> через h(), без innerHTML.
 * @param {*} text
 * @param {*} query
 * @returns {Array<{text:string, match:boolean}>}
 */
export function highlightSegments(text, query) {
  const src = text === null || text === undefined ? '' : String(text);
  const ranges = tokenRanges(src, query);
  if (!ranges.length) return src ? [{ text: src, match: false }] : [];
  const out = [];
  let at = 0;
  for (const [start, end] of ranges) {
    const s = Math.max(0, Math.min(start, src.length));
    const e = Math.max(s, Math.min(end, src.length));
    if (s > at) out.push({ text: src.slice(at, s), match: false });
    if (e > s) out.push({ text: src.slice(s, e), match: true });
    at = e;
  }
  if (at < src.length) out.push({ text: src.slice(at), match: false });
  return out;
}

const TAB_ORDER = ['composer', 'team', 'roadmap', 'backlog', 'reports', 'journal'];

/**
 * Постоянные команды палитры.
 * @param {{
 *   rooms?: Array<{id:string,title?:string}>,
 *   currentRoomId?: string|null,
 *   tabs?: Array<{id:string,title:string}>,
 *   theme?: 'auto'|'light'|'dark'
 * }} [input]
 * @returns {Array<object>}
 */
export function buildCommandItems(input = {}) {
  const rooms = Array.isArray(input.rooms) ? input.rooms : [];
  const currentRoomId = input.currentRoomId || null;
  const tabs = Array.isArray(input.tabs) && input.tabs.length
    ? input.tabs
    : TAB_ORDER.map((id) => ({ id, title: id }));
  const theme = input.theme || 'auto';
  const items = [];

  const current = rooms.find((r) => r && r.id === currentRoomId) || null;
  const currentTitle = current ? (current.title || current.id) : '';

  // Новый запрос — главное действие, поэтому первым.
  if (current) {
    items.push({
      id: 'new:' + current.id,
      group: 'Действия',
      icon: 'comment-discussion',
      title: 'Новый запрос в «' + currentTitle + '»',
      hint: 'К полю запроса',
      keywords: 'новый запрос написать ' + currentTitle,
      action: { type: 'new-request', room: current.id },
    });
  } else if (rooms.length) {
    const first = rooms[0];
    items.push({
      id: 'new:' + first.id,
      group: 'Действия',
      icon: 'comment-discussion',
      title: 'Новый запрос в «' + (first.title || first.id) + '»',
      hint: 'К полю запроса',
      keywords: 'новый запрос написать ' + (first.title || first.id),
      action: { type: 'new-request', room: first.id },
    });
  }

  items.push({
    id: 'room:new',
    group: 'Действия',
    icon: 'new-folder',
    title: 'Создать комнату',
    hint: 'Новая комната под проект',
    keywords: 'создать комнату новую проект добавить',
    action: { type: 'create-room' },
  });

  // Переходы по вкладкам текущей комнаты.
  if (current) {
    for (const tab of tabs) {
      const title = tab && tab.title ? tab.title : tab && tab.id;
      if (!title) continue;
      items.push({
        id: 'tab:' + current.id + ':' + tab.id,
        group: 'Перейти',
        icon: 'arrow-right',
        title: 'Перейти: ' + title,
        hint: currentTitle,
        keywords: 'вкладка ' + title + ' ' + currentTitle,
        action: { type: 'navigate', hash: '#/room/' + encodeURIComponent(current.id) + '/' + encodeURIComponent(tab.id) },
      });
    }
  }

  // Комнаты.
  for (const room of rooms) {
    if (!room || !room.id) continue;
    items.push({
      id: 'room:' + room.id,
      group: 'Комнаты',
      icon: 'comment-discussion',
      title: room.title || room.id,
      hint: room.id === currentRoomId ? 'текущая комната' : 'открыть комнату',
      keywords: 'комната ' + room.id + ' ' + (room.title || ''),
      action: { type: 'navigate', hash: '#/room/' + encodeURIComponent(room.id) + '/composer' },
    });
  }

  // Тема.
  const themeNames = { auto: 'как в системе', light: 'светлая', dark: 'тёмная' };
  for (const value of ['auto', 'light', 'dark']) {
    items.push({
      id: 'theme:' + value,
      group: 'Вид',
      icon: 'color-mode',
      title: 'Тема: ' + themeNames[value],
      hint: theme === value ? 'включена' : 'переключить',
      keywords: 'тема оформление ' + themeNames[value] + ' ' + value,
      action: { type: 'theme', value },
    });
  }

  items.push({
    id: 'settings',
    group: 'Приложение',
    icon: 'settings-gear',
    title: 'Настройки',
    hint: 'Здоровье, вид, уведомления, вопросы',
    keywords: 'настройки здоровье движок диспетчер уведомления вопросы клавиши',
    action: { type: 'navigate', hash: '#/settings' },
  });

  return items;
}

/** Заголовок группы для типа результата поиска. */
export function sectionForKind(kind) {
  switch (String(kind || '')) {
    case 'request': return { id: 'hits:request', title: 'Запросы' };
    case 'decision': return { id: 'hits:decision', title: 'Решения' };
    case 'backlog': return { id: 'hits:backlog', title: 'Бэклог' };
    case 'journal': return { id: 'hits:journal', title: 'Журнал' };
    case 'report': return { id: 'hits:report', title: 'Отчёты' };
    default: return { id: 'hits:other', title: 'Найдено' };
  }
}

/**
 * Канонический маршрут результата поиска (не доверяем устаревшим route).
 * @param {{kind?:string,id?:string,room?:string,route?:string}} hit
 * @returns {string}
 */
export function routeForHit(hit) {
  const room = hit && hit.room ? String(hit.room) : '';
  const id = hit && hit.id ? String(hit.id) : '';
  const kind = hit && hit.kind ? String(hit.kind) : '';
  if (!room) return (hit && hit.route) || '#/';
  const base = '#/room/' + encodeURIComponent(room);
  switch (kind) {
    case 'request': return id ? base + '/request/' + encodeURIComponent(id) : base + '/composer';
    case 'decision': return base + '/roadmap';
    case 'backlog': return base + '/backlog';
    case 'journal': return base + '/journal';
    case 'report': return id ? base + '/reports/' + encodeURIComponent(id) : base + '/reports';
    default: return (hit && hit.route) || base + '/composer';
  }
}

/** Отсортировать по score (убыв.), сохраняя исходный порядок при равенстве. */
function byScore(items) {
  return items
    .map((item, i) => ({ item, i }))
    .sort((a, b) => (b.item.score - a.item.score) || (a.i - b.i))
    .map((x) => x.item);
}

/**
 * Отфильтровать элементы по запросу (по title + keywords, fuzzy).
 * @param {Array<object>} items
 * @param {string} query
 * @param {number} [limit]
 * @returns {Array<object>}
 */
export function filterItems(items, query, limit = 8) {
  const list = Array.isArray(items) ? items : [];
  if (!query) return list.slice(0, limit);
  const scored = [];
  for (const item of list) {
    if (!item) continue;
    const text = (item.title || '') + ' ' + (item.keywords || '') + ' ' + (item.hint || '');
    const m = fuzzyMatch(text, query);
    if (!m) continue;
    scored.push(Object.assign({}, item, { score: m.score }));
  }
  return byScore(scored).slice(0, limit);
}

/**
 * Собрать группы для показа.
 * @param {{
 *   commands?: Array<object>,
 *   recents?: Array<object>,
 *   hits?: Array<object>,
 *   query?: string,
 *   limit?: number
 * }} input
 * @returns {Array<{id:string,title:string,items:Array<object>}>}
 */
export function composeList(input = {}) {
  const commands = Array.isArray(input.commands) ? input.commands : [];
  const recents = Array.isArray(input.recents) ? input.recents : [];
  const hits = Array.isArray(input.hits) ? input.hits : [];
  const query = normalize(input.query || '');
  const limit = Number.isFinite(input.limit) ? input.limit : 40;
  const groups = [];

  if (!query) {
    if (recents.length) groups.push({ id: 'recents', title: 'Последние запросы', items: recents.slice(0, 5) });
    if (commands.length) groups.push({ id: 'commands', title: 'Команды', items: commands.slice(0, limit) });
    return groups;
  }

  const cmdLimit = query.length >= 2 ? 5 : 8;
  const foundCommands = filterItems(commands, query, cmdLimit);
  const foundRecents = filterItems(recents, query, 3);
  if (foundCommands.length) groups.push({ id: 'commands', title: 'Команды', items: foundCommands });
  if (foundRecents.length) groups.push({ id: 'recents', title: 'Последние запросы', items: foundRecents });

  if (query.length >= 2 && hits.length) {
    // Один и тот же запрос не должен стоять дважды: в «последних» и в выдаче.
    const seen = new Set();
    for (const recent of recents) {
      const hash = recent && recent.action && recent.action.hash;
      if (hash) seen.add(String(hash));
    }
    const buckets = new Map();
    for (const hit of hits) {
      if (!hit) continue;
      if (seen.has(routeForHit(hit))) continue;
      const section = sectionForKind(hit.kind);
      if (!buckets.has(section.id)) buckets.set(section.id, { id: section.id, title: section.title, items: [] });
      const bucket = buckets.get(section.id);
      bucket.items.push({
        id: 'hit:' + (hit.kind || 'x') + ':' + (hit.room || '') + ':' + (hit.id || bucket.items.length),
        group: section.title,
        icon: hit.kind === 'report' ? 'report' : hit.kind === 'decision' ? 'law' : 'search',
        title: hit.title || hit.id || 'Найдено',
        hint: hit.room ? String(hit.room) : '',
        keywords: hit.snippet || '',
        external: true,
        action: { type: 'navigate', hash: routeForHit(hit) },
      });
    }
    for (const bucket of buckets.values()) groups.push(bucket);
  }

  return groups;
}

/**
 * Плоский список элементов в порядке показа (для ↑↓/Enter).
 * @param {Array<{items:Array<object>}>} groups
 * @returns {Array<object>}
 */
export function flattenGroups(groups) {
  const out = [];
  for (const group of Array.isArray(groups) ? groups : []) {
    if (group && Array.isArray(group.items)) out.push(...group.items);
  }
  return out;
}

/**
 * Сдвиг активной строки с закольцовкой.
 * @param {number} index  текущий (-1 — ничего не выбрано)
 * @param {number} delta  +1 вниз, -1 вверх
 * @param {number} length
 * @returns {number}
 */
export function moveIndex(index, delta, length) {
  const n = Number(length) || 0;
  if (n <= 0) return -1;
  const step = Number(delta) || 0;
  if (index < 0 || index >= n) return step > 0 ? 0 : n - 1;
  return ((index + step) % n + n) % n;
}

export default {
  normalize,
  fuzzyMatch,
  fuzzyScore,
  highlightSegments,
  buildCommandItems,
  sectionForKind,
  routeForHit,
  filterItems,
  composeList,
  flattenGroups,
  moveIndex,
};
