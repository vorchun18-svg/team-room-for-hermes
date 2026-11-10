import test from 'node:test';
import assert from 'node:assert/strict';

import {
  normalize, fuzzyMatch, fuzzyScore, highlightSegments, buildCommandItems,
  sectionForKind, routeForHit, filterItems, composeList, flattenGroups, moveIndex,
} from '../web/js/views/palette-model.js';

/* ------------------------------------------------------------- нормализация */

test('normalize: регистр, ё→е, пробелы', () => {
  assert.equal(normalize('  Ёжик   в Тумане '), 'ежик в тумане');
  assert.equal(normalize('ЕЩЁ'), 'еще');
  assert.equal(normalize(null), '');
  assert.equal(normalize(undefined), '');
  assert.equal(normalize(0), '0');
});

test('normalize не меняет длину строки (важно для подсветки)', () => {
  const src = 'Ёлка-палка 2026';
  assert.equal(normalize(src).length, src.length);
});

/* ------------------------------------------------------------ fuzzy-скоринг */

test('fuzzyMatch: подпоследовательность, ё/е и регистр не мешают', () => {
  assert.ok(fuzzyMatch('Чек-лист легализации', 'чек'));
  assert.ok(fuzzyMatch('Чек-лист', 'ЧЕК'));
  assert.ok(fuzzyMatch('Ещё один запрос', 'еще'));
  assert.ok(fuzzyMatch('Настройки', 'нстр'));
  assert.equal(fuzzyMatch('Настройки', 'настройказ'), null);
  assert.equal(fuzzyMatch('abc', 'cba'), null);
});

test('fuzzyMatch отдаёт позиции совпавших символов по возрастанию', () => {
  const m = fuzzyMatch('Комната команды', 'ком');
  assert.deepEqual(m.indices, [0, 1, 2]);
  assert.ok(m.score > 0);
});

test('fuzzyScore: начало слова и точное вхождение выигрывают', () => {
  const exact = fuzzyScore('Отчёты', 'отчёты');
  const prefix = fuzzyScore('Отчёты комнаты', 'отч');
  const middle = fuzzyScore('Все отчёты', 'отч');
  assert.ok(exact > prefix, 'точное совпадение выше префикса');
  assert.ok(prefix >= middle, 'начало слова не хуже середины');
  assert.equal(fuzzyScore('Отчёты', 'ззз'), -1);
});

test('fuzzyScore: пустая строка не падает', () => {
  assert.equal(fuzzyScore('', 'a'), -1);
  assert.deepEqual(fuzzyMatch('', '').indices, []);
});

/* ------------------------------------------------------------------ подсветка */

test('highlightSegments режет по вхождениям и помечает match', () => {
  const parts = highlightSegments('Подача в Роспатент: класс 30', 'роспатент');
  assert.deepEqual(parts.map((p) => p.text).join(''), 'Подача в Роспатент: класс 30');
  const marked = parts.filter((p) => p.match).map((p) => p.text);
  assert.deepEqual(marked, ['Роспатент']);
});

test('highlightSegments: ё/е и несколько токенов, без наложения', () => {
  const parts = highlightSegments('Ещё раз про ещё', 'еще раз');
  const marked = parts.filter((p) => p.match).map((p) => p.text);
  assert.deepEqual(marked, ['Ещё', 'раз', 'ещё']);
  assert.equal(parts.map((p) => p.text).join(''), 'Ещё раз про ещё');
});

test('highlightSegments: нет совпадений — один сегмент без match', () => {
  assert.deepEqual(highlightSegments('текст', 'zzz'), [{ text: 'текст', match: false }]);
  assert.deepEqual(highlightSegments('', 'abc'), []);
});

/* ------------------------------------------------------------- команды */

const rooms = [
  { id: 'cafe', title: 'Лето (demo)' },
  { id: 'fn', title: 'Рабочее пространство' },
];
const tabs = [
  { id: 'composer', title: 'Запрос' },
  { id: 'roadmap', title: 'Карта проекта' },
  { id: 'reports', title: 'Отчёты' },
];

test('buildCommandItems: новый запрос, комнаты, вкладки, тема, настройки', () => {
  const items = buildCommandItems({ rooms, currentRoomId: 'cafe', tabs, theme: 'dark' });
  const ids = items.map((i) => i.id);
  assert.ok(ids.includes('new:cafe'), 'новый запрос в текущую комнату');
  assert.ok(ids.includes('room:new'), 'создать комнату');
  assert.ok(ids.includes('room:cafe') && ids.includes('room:fn'), 'обе комнаты');
  assert.ok(ids.includes('tab:cafe:reports'), 'вкладка текущей комнаты');
  assert.ok(!ids.includes('tab:fn:reports'), 'вкладки только у текущей комнаты');
  assert.ok(ids.includes('theme:light') && ids.includes('theme:dark') && ids.includes('theme:auto'));
  assert.ok(ids.includes('settings'));

  const themeDark = items.find((i) => i.id === 'theme:dark');
  assert.equal(themeDark.hint, 'включена');
  assert.equal(themeDark.action.value, 'dark');
  assert.equal(items.find((i) => i.id === 'new:cafe').action.room, 'cafe');
  assert.equal(items.find((i) => i.id === 'room:fn').action.hash, '#/room/fn/composer');
});

test('buildCommandItems: без комнаты нет «Новый запрос», но палитра жива', () => {
  const items = buildCommandItems({ rooms: [], currentRoomId: null, tabs: [], theme: 'auto' });
  assert.ok(!items.some((i) => i.id.startsWith('new:')));
  assert.ok(items.some((i) => i.id === 'settings'));
  assert.ok(items.some((i) => i.id === 'room:new'));
});

test('buildCommandItems: id уникальны', () => {
  const ids = buildCommandItems({ rooms, currentRoomId: 'fn', tabs }).map((i) => i.id);
  assert.equal(new Set(ids).size, ids.length);
});

/* ------------------------------------------------------------ фильтрация */

test('filterItems ищет по названию, подсказке и keywords', () => {
  const items = buildCommandItems({ rooms, currentRoomId: 'cafe', tabs, theme: 'auto' });
  const found = filterItems(items, 'отчет');
  assert.ok(found.length >= 1);
  assert.ok(found.every((i) => i.score >= 0));
  assert.ok(found.some((i) => i.id === 'tab:cafe:reports'), 'ё→е в запросе');

  assert.equal(filterItems(items, 'ничеготакого').length, 0);
  assert.equal(filterItems(items, '', 3).length, 3);
});

/* ------------------------------------------------------- раскладка списка */

test('composeList без запроса: последние запросы + команды', () => {
  const groups = composeList({
    commands: buildCommandItems({ rooms, currentRoomId: 'cafe', tabs }),
    recents: [{ id: 'r1', title: 'Каналы продаж', action: {} }],
    hits: [{ kind: 'request', id: 't_1', room: 'cafe', title: 'Каналы' }],
    query: '',
  });
  assert.deepEqual(groups.map((g) => g.id), ['recents', 'commands']);
  assert.equal(groups[0].items.length, 1);
});

test('composeList: 1 символ — только локальные команды, без серверных hits', () => {
  const groups = composeList({
    commands: buildCommandItems({ rooms, currentRoomId: 'cafe', tabs }),
    recents: [],
    hits: [{ kind: 'request', id: 't_1', room: 'cafe', title: 'Каналы продаж' }],
    query: 'к',
  });
  assert.deepEqual(groups.map((g) => g.id), ['commands']);
  assert.ok(groups[0].items.length <= 8);
});

test('composeList: ≥2 символов добавляет hits, сгруппированные по типу', () => {
  const groups = composeList({
    commands: buildCommandItems({ rooms, currentRoomId: 'cafe', tabs }),
    recents: [],
    hits: [
      { kind: 'request', id: 't_1', room: 'cafe', title: 'Подача в Роспатент' },
      { kind: 'decision', id: 'd1', room: 'cafe', title: 'Класс 30' },
      { kind: 'report', id: 'legal', room: 'cafe', title: 'Чек-лист' },
      { kind: 'journal', id: 'j1', room: 'cafe', title: 'Событие' },
      { kind: 'backlog', id: 'b1', room: 'cafe', title: 'Шаг' },
      { kind: 'unknown', id: 'u1', room: 'cafe', title: 'Прочее' },
    ],
    query: 'роспатент',
  });
  const ids = groups.map((g) => g.id);
  for (const id of ['hits:request', 'hits:decision', 'hits:report', 'hits:journal', 'hits:backlog', 'hits:other']) {
    assert.ok(ids.includes(id), 'группа ' + id);
  }
  const titles = groups.map((g) => g.title);
  assert.ok(titles.includes('Запросы') && titles.includes('Решения') && titles.includes('Отчёты'));
});

test('composeList: hits с external не теряются, даже если не совпали с query', () => {
  const groups = composeList({
    commands: [], recents: [], query: 'zz',
    hits: [{ kind: 'request', id: 't_1', room: 'cafe', title: 'Что-то другое' }],
  });
  const flat = flattenGroups(groups);
  assert.equal(flat.length, 1);
  assert.equal(flat[0].external, true);
});

test('composeList: не дублирует запрос из «последних» в выдаче', () => {
  const groups = composeList({
    commands: [],
    recents: [{ id: 'req:t_1', title: 'Каналы', action: { hash: '#/room/cafe/request/t_1' } }],
    hits: [
      { kind: 'request', id: 't_1', room: 'cafe', title: 'Каналы' },
      { kind: 'journal', id: 'j1', room: 'cafe', title: 'Событие' },
    ],
    query: 'ка',
  });
  const flat = flattenGroups(groups);
  assert.equal(flat.filter((i) => i.title === 'Каналы').length, 1);
  assert.ok(flat.some((i) => String(i.id).startsWith('hit:journal')));
});

test('flattenGroups собирает элементы по порядку групп', () => {
  const flat = flattenGroups([
    { id: 'a', items: [{ id: 1 }, { id: 2 }] },
    { id: 'b', items: [{ id: 3 }] },
    { id: 'c' },
  ]);
  assert.deepEqual(flat.map((i) => i.id), [1, 2, 3]);
  assert.deepEqual(flattenGroups(null), []);
});

/* ------------------------------------------------------------- клавиатура */

test('moveIndex закольцовывает и переживает пустой список', () => {
  assert.equal(moveIndex(-1, 1, 3), 0);
  assert.equal(moveIndex(-1, -1, 3), 2);
  assert.equal(moveIndex(2, 1, 3), 0);
  assert.equal(moveIndex(0, -1, 3), 2);
  assert.equal(moveIndex(1, 5, 3), 0);
  assert.equal(moveIndex(0, 0, 3), 0);
  assert.equal(moveIndex(0, 1, 0), -1);
});

/* --------------------------------------------------- маршруты результатов */

test('routeForHit: канонические маршруты по типу', () => {
  assert.equal(routeForHit({ kind: 'request', id: 't_1', room: 'cafe' }), '#/room/cafe/request/t_1');
  assert.equal(routeForHit({ kind: 'decision', id: 'd1', room: 'cafe' }), '#/room/cafe/roadmap');
  assert.equal(routeForHit({ kind: 'backlog', id: 'b1', room: 'cafe', route: '#/room/cafe/next' }), '#/room/cafe/backlog');
  assert.equal(routeForHit({ kind: 'journal', id: 'j1', room: 'cafe' }), '#/room/cafe/journal');
  assert.equal(routeForHit({ kind: 'report', id: 'legal', room: 'cafe' }), '#/room/cafe/reports/legal');
  assert.equal(routeForHit({ kind: 'request', id: 't_9', room: '' , route: '#/x' }), '#/x');
  assert.equal(routeForHit({ kind: 'request', room: 'cafe' }), '#/room/cafe/composer');
});

test('sectionForKind: русские заголовки, неизвестное — «Найдено»', () => {
  assert.equal(sectionForKind('request').title, 'Запросы');
  assert.equal(sectionForKind('decision').title, 'Решения');
  assert.equal(sectionForKind('backlog').title, 'Бэклог');
  assert.equal(sectionForKind('journal').title, 'Журнал');
  assert.equal(sectionForKind('report').title, 'Отчёты');
  assert.equal(sectionForKind('nonsense').title, 'Найдено');
});
