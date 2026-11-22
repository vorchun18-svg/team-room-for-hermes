/**
 * recents.test.mjs — «Недавние» (10) и «Закладки» (★) в меню (W29-Q7).
 *
 * Проверяем:
 *   1) «Недавнее»: ровно 10 пунктов, свежее выше, дубли не плодятся, название
 *      не теряется при повторном открытии;
 *   2) дела и запросы живут в одном списке — «запросов/дел», как в задаче;
 *   3) слияние локального списка с последними запросами комнат;
 *   4) закладки: звезда ставится/снимается, список переживает перезапуск;
 *   5) сервер — источник правды по ★: `pinned:false` убирает закладку,
 *      `pinned:true` добавляет (звезда из другой копии окна);
 *   6) мусор в localStorage не ломает меню.
 *
 *   node --test tests/recents.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  RECENT_KEY, BOOKMARK_KEY, RECENT_MAX,
  normRecent, parseRecents, readRecents, pushRecent, renameRecent, mergeRecents,
  normBookmark, readBookmarks, isBookmarked, setBookmark, toggleBookmark, syncBookmarks,
  bookmarkLabel,
} from '../web/js/recents.js';

function storage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    dump: () => Object.fromEntries(map),
  };
}

test('«Недавнее» — ровно 10 пунктов, свежее выше', () => {
  assert.equal(RECENT_MAX, 10);
  const store = storage();
  for (let i = 1; i <= 13; i += 1) {
    pushRecent({ id: 'r' + i, roomId: 'cafe', title: 'Запрос ' + i, at: 1000 + i }, { store });
  }
  const list = readRecents(store);
  assert.equal(list.length, 10);
  assert.equal(list[0].id, 'r13');
  assert.equal(list[9].id, 'r4');
});

test('повторное открытие поднимает наверх и не теряет название', () => {
  const store = storage();
  pushRecent({ id: 'r1', roomId: 'cafe', title: 'Сверка накладных', at: 100 }, { store });
  pushRecent({ id: 'r2', roomId: 'cafe', title: 'Меню на неделю', at: 200 }, { store });
  pushRecent({ id: 'r1', roomId: 'cafe', at: 300 }, { store });
  const list = readRecents(store);
  assert.deepEqual(list.map((x) => x.id), ['r1', 'r2']);
  assert.equal(list[0].title, 'Сверка накладных', 'название сохранилось при повторном открытии');
  assert.equal(list.length, 2, 'дублей нет');
});

test('дела и запросы — в одном списке', () => {
  const store = storage();
  pushRecent({ id: 'd1', kind: 'delo', title: 'Ремонт холодильника', at: 500 }, { store });
  pushRecent({ id: 'r1', roomId: 'laundry', title: 'Претензия', at: 600 }, { store });
  const list = readRecents(store);
  assert.deepEqual(list.map((x) => x.kind), ['request', 'delo']);
  // у дела комнаты может не быть — это не мусор
  assert.equal(list[1].roomId, '');
  assert.equal(normRecent({ id: 'x' }), null, 'запрос без комнаты в «Недавнее» не попадает');
});

test('переименование запроса обновляет подпись', () => {
  const store = storage();
  pushRecent({ id: 'r1', roomId: 'cafe', title: 'Старое', at: 10 }, { store });
  renameRecent('r1', 'Новое', { store });
  assert.equal(readRecents(store)[0].title, 'Новое');
});

test('слияние локального и серверного списков', () => {
  const local = [
    { id: 'r1', roomId: 'cafe', title: 'Локальный', at: 900 },
    { id: 'd1', kind: 'delo', title: 'Дело', at: 950 },
  ];
  const server = [
    { id: 'r1', roomId: 'cafe', title: 'Серверный', at: 100 },
    { id: 'r9', roomId: 'freight', title: 'Свежий с сервера', at: 800 },
  ];
  const list = mergeRecents(local, server, 10);
  assert.deepEqual(list.map((x) => x.id), ['d1', 'r1', 'r9'], 'свежее выше, дубль по id — один');
  assert.equal(list[1].title, 'Локальный', 'локальная запись знает настоящее название');
  assert.equal(mergeRecents(local, server, 2).length, 2);
});

test('закладки: звезда ставится, снимается и переживает перезапуск', () => {
  assert.equal(BOOKMARK_KEY, 'teamroom.bookmarks');
  const store = storage();
  assert.equal(isBookmarked('r1', 0 && { store }), false);
  const on = toggleBookmark({ id: 'r1', roomId: 'cafe', title: 'Сверка накладных' }, { store });
  assert.equal(on.on, true);
  assert.equal(isBookmarked('r1', { store }), true);
  // «перезапуск»: новое чтение того же хранилища
  const again = readBookmarks(store);
  assert.deepEqual(again.map((b) => b.id), ['r1']);
  assert.equal(bookmarkLabel(again[0]), 'Сверка накладных');
  const off = toggleBookmark({ id: 'r1', roomId: 'cafe' }, { store });
  assert.equal(off.on, false);
  assert.equal(readBookmarks(store).length, 0);
  assert.equal(setBookmark(null, true, { store }).on, false, 'пустая запись не создаёт закладку');
});

test('сервер — источник правды: снятый ★ исчезает, поставленный — появляется', () => {
  const store = storage();
  setBookmark({ id: 'r1', roomId: 'cafe', title: 'Старое имя' }, true, { store });
  setBookmark({ id: 'r2', roomId: 'cafe', title: 'Сняли на сервере' }, true, { store });
  const rows = [
    { id: 'r1', roomId: 'cafe', title: 'Новое имя', pinned: true },
    { id: 'r2', roomId: 'cafe', title: 'Сняли на сервере', pinned: false },
    { id: 'r3', roomId: 'laundry', title: 'Звезда из другой копии окна', pinned: true },
  ];
  const list = syncBookmarks(rows, { store });
  const ids = list.map((b) => b.id).sort();
  assert.deepEqual(ids, ['r1', 'r3']);
  assert.equal(list.find((b) => b.id === 'r1').title, 'Новое имя', 'подпись обновилась с сервера');
  assert.deepEqual(readBookmarks(store).map((b) => b.id).sort(), ['r1', 'r3']);
});

test('мусор в localStorage не ломает меню', () => {
  assert.deepEqual(readRecents(storage({ [RECENT_KEY]: '{не json' })), []);
  assert.deepEqual(readBookmarks(storage({ [BOOKMARK_KEY]: '42' })), []);
  assert.deepEqual(parseRecents('null'), []);
  assert.deepEqual(parseRecents({ items: [null, 1, { id: '' }] }), []);
  assert.equal(normBookmark({ id: 'x', roomId: 'y' }).id, 'x');
});
