/**
 * rooms-order.test.mjs — порядок комнат, закрепление и группы левого меню (W29-P7).
 *
 * Здесь проверяется логика без DOM и без браузера: тот же порядок, что считает
 * сервер (`server/rooms.py: _menu_key`), разделы меню, перетаскивание внутри
 * группы и перенос в другую группу, клавиатура Alt+↑/↓ и тело запроса.
 * Живая проверка мышью в Chromium — `tools/probe_rooms_order.py`.
 *
 *   node --test tests/rooms-order.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  ORDER_ENDPOINT, menuRooms, menuSections, sectionIds, moveRoom, stepRoom, dropIndex,
  layoutPayload, pinPayload, groupIdFromTitle, normalizeGroups, planDrop, canDrag, orderOf,
} from '../web/js/rooms-order.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

const rooms = () => ([
  { id: 'cafe', title: 'Кофейня', order: 10, pinned: false, group: '', created_at: 100 },
  { id: 'laundry', title: 'Химчистка', order: 20, pinned: false, group: 'work', created_at: 200 },
  { id: 'freight', title: 'Грузоперевозки', order: 30, pinned: false, group: 'work', created_at: 300 },
  { id: 'flowers', title: 'Доставка цветов', order: 40, pinned: true, group: '', created_at: 400 },
]);

test('порядок меню: закреплённые сверху, затем `order`, затем создание', () => {
  const list = rooms();
  assert.deepEqual(menuRooms(list).map((r) => r.id), ['flowers', 'cafe', 'laundry', 'freight']);
  assert.equal(orderOf({ order: 0 }), 0, 'порядок не задавали');
  assert.equal(orderOf({ order: -5 }), 0, 'мусор — «не задавали»');
  assert.equal(orderOf({ order: '7' }), 7, 'число строкой понимаем');
  // Исходный массив не меняется: порядок — вычисление, а не мутация стора.
  assert.deepEqual(list.map((r) => r.id), ['cafe', 'laundry', 'freight', 'flowers']);
});

test('комнаты без `order` встают по времени создания, а не в случайном месте', () => {
  const list = [{ id: 'b', created_at: 2 }, { id: 'a', created_at: 1 }];
  assert.deepEqual(menuRooms(list).map((r) => r.id), ['a', 'b']);
});

test('разделы меню: закреплённые, группы по порядку, «без группы» последними', () => {
  const sections = menuSections(rooms(), [
    { id: 'work', title: 'Работа', collapsed: false },
    { id: 'empty', title: 'Пустая', collapsed: true },
  ]);
  assert.deepEqual(sections.map((s) => [s.kind, s.id]), [
    ['pinned', ''], ['group', 'work'], ['group', 'empty'], ['plain', ''],
  ]);
  assert.deepEqual(sections[0].rooms.map((r) => r.id), ['flowers']);
  assert.deepEqual(sections[1].rooms.map((r) => r.id), ['laundry', 'freight']);
  assert.deepEqual(sections[3].rooms.map((r) => r.id), ['cafe']);
  assert.equal(sections[2].collapsed, true, 'свёрнутость группы — с сервера');
  // Плоский порядок для сервера совпадает с тем, что видит человек.
  assert.deepEqual(sectionIds(sections), ['flowers', 'laundry', 'freight', 'cafe']);
});

test('комната с несуществующей группой не пропадает из меню', () => {
  const list = [{ id: 'ghost', order: 1, group: 'deleted', created_at: 1 }];
  const sections = menuSections(list, [{ id: 'work', title: 'Работа' }]);
  const plain = sections.find((s) => s.kind === 'plain');
  assert.ok(plain, 'раздел «без группы» есть');
  assert.deepEqual(plain.rooms.map((r) => r.id), ['ghost']);
});

test('перетаскивание: выше и ниже, за край — без изменений', () => {
  const ids = ['a', 'b', 'c', 'd'];
  assert.deepEqual(moveRoom(ids, 'd', 'b', 'before'), ['a', 'd', 'b', 'c']);
  assert.deepEqual(moveRoom(ids, 'a', 'c', 'after'), ['b', 'c', 'a', 'd']);
  assert.deepEqual(moveRoom(ids, 'a', 'a', 'before'), ids, 'сам на себя — порядок прежний');
  assert.deepEqual(moveRoom(ids, 'zzz', 'b', 'before'), ids, 'неизвестный id — без сюрпризов');
  assert.deepEqual(ids, ['a', 'b', 'c', 'd'], 'входной массив не меняется');
});

test('клавиатура: Alt+↑/↓ сдвигает на шаг и не прыгает за края', () => {
  const ids = ['a', 'b', 'c'];
  assert.deepEqual(stepRoom(ids, 'b', -1), ['b', 'a', 'c']);
  assert.deepEqual(stepRoom(ids, 'b', 1), ['a', 'c', 'b']);
  assert.deepEqual(stepRoom(ids, 'a', -1), ids, 'выше первого — некуда');
  assert.deepEqual(stepRoom(ids, 'c', 1), ids, 'ниже последнего — некуда');
});

test('место вставки по координате: середина строки — уже следующая строка', () => {
  const rects = [{ top: 100, height: 30 }, { top: 130, height: 30 }, { top: 160, height: 30 }];
  assert.equal(dropIndex(rects, 90), 0, 'выше первой');
  assert.equal(dropIndex(rects, 112), 0, 'верх первой');
  assert.equal(dropIndex(rects, 116), 1, 'низ первой — вставка перед второй');
  assert.equal(dropIndex(rects, 200), 3, 'ниже последней — в конец');
  assert.equal(dropIndex([], 50), 0, 'пустой список — индекс 0');
});

test('план перетаскивания: внутри группы, в другую группу, из группы', () => {
  const sections = menuSections(rooms(), [{ id: 'work', title: 'Работа' }]);
  // Внутри группы: грузоперевозки выше химчистки.
  const inside = planDrop(sections, 'freight', 'work', 0);
  assert.deepEqual(inside.ids, ['flowers', 'freight', 'laundry', 'cafe']);
  assert.deepEqual(inside.patch, {}, 'группа и закрепление не менялись');

  // Перенос в группу «Работа» из «без группы».
  const into = planDrop(sections, 'cafe', 'work', 1);
  assert.equal(into.patch.group, 'work', 'комната переехала в группу');
  assert.equal(into.patch.pinned, undefined, 'закрепление не тронуто');
  assert.deepEqual(into.sections.find((s) => s.id === 'work').rooms.map((r) => r.id),
    ['laundry', 'cafe', 'freight']);

  // Из группы — в «без группы».
  const out = planDrop(sections, 'laundry', '', 0);
  assert.equal(out.patch.group, '', 'группа снята');
  assert.deepEqual(out.sections.find((s) => s.kind === 'plain').rooms.map((r) => r.id),
    ['laundry', 'cafe']);
});

test('план перетаскивания: в закреплённые ставит 📌, из закреплённых — снимает', () => {
  const sections = menuSections(rooms(), [{ id: 'work', title: 'Работа' }]);
  const up = planDrop(sections, 'cafe', '__pinned__', 0);
  assert.equal(up.patch.pinned, true);
  assert.equal(up.ids[0], 'cafe', 'закреплённая — первой');
  const down = planDrop(sections, 'flowers', 'work', 0);
  assert.equal(down.patch.pinned, false, 'закрепление снято');
  assert.equal(down.patch.group, 'work');
});

test('план перетаскивания: неизвестная комната или раздел — null, не падение', () => {
  const sections = menuSections(rooms(), []);
  assert.equal(planDrop(sections, 'nope', '', 0), null);
  assert.equal(planDrop(sections, 'cafe', 'no-such-group', 0), null);
});

test('тело запроса: порядок + правки; закрепление идёт без порядка', () => {
  assert.deepEqual(layoutPayload(['a', 'b']), { order: ['a', 'b'] });
  assert.deepEqual(layoutPayload(['a'], { a: { pinned: true, group: 'work' } }),
    { order: ['a'], rooms: { a: { pinned: true, group: 'work' } } });
  assert.deepEqual(pinPayload('cafe', true), { rooms: { cafe: { pinned: true } } });
  assert.equal(ORDER_ENDPOINT, '/api/rooms/order');
});

test('группы: id из названия, повторы получают суффикс', () => {
  assert.equal(groupIdFromTitle('Работа'), 'rabota');
  assert.equal(groupIdFromTitle('Работа', ['rabota']), 'rabota-2');
  assert.equal(groupIdFromTitle('Работа', ['rabota', 'rabota-2']), 'rabota-3');
  assert.equal(groupIdFromTitle('   '), 'g-');
  assert.match(groupIdFromTitle('Свои дела 2026'), /^[a-z0-9][a-z0-9_-]{0,23}$/);
});

test('нормализация групп с сервера: мусор и повторы отбрасываются', () => {
  const list = normalizeGroups([
    { id: 'work', title: ' Работа ' },
    { id: 'work', title: 'Дубль' },
    { id: 'Плохой id', title: 'Нельзя' },
    { id: 'home', title: '' },
    { id: 'home', title: 'Дом', collapsed: 1 },
    null,
  ]);
  assert.deepEqual(list, [{ id: 'work', title: 'Работа', collapsed: false },
    { id: 'home', title: 'Дом', collapsed: false }]);
});

test('перетаскивание выключено в полосе значков и на узком экране', () => {
  assert.equal(canDrag({ width: 1200 }), true);
  assert.equal(canDrag({ width: 1200, rail: true }), false, 'полоса значков — порядок не тянут');
  assert.equal(canDrag({ width: 420 }), false, 'узкий экран — ручка скрыта');
});

test('сайдбар рисует разделы, ручку, 📌 и Alt+↑/↓ — по этой логике', () => {
  const src = read('web/js/shell-sidebar.js');
  assert.match(src, /from '\.\/rooms-order\.js'/);
  assert.match(src, /menuSections\(/, 'разделы считаются общим модулем');
  assert.match(src, /planDrop\(/, 'перетаскивание считает общий модуль');
  assert.match(src, /class: 'room-drag'/, 'ручка ⋮⋮ у названия комнаты');
  assert.match(src, /class: 'room-pin'/, 'кнопка закрепления');
  assert.match(src, /ev\.altKey[\s\S]{0,120}ArrowUp/, 'Alt+↑/↓ на строке комнаты');
  assert.match(src, /data-room-section|roomSection/, 'раздел меню — цель перетаскивания');
  assert.match(src, /api\.put\(ORDER_ENDPOINT/, 'порядок сохраняется на сервере');
  // Клик по ручке и 📌 не уводит со страницы на вкладку комнаты.
  assert.match(src, /handle\.addEventListener\('click',[\s\S]{0,120}preventDefault/);
  assert.match(src, /pin\.addEventListener\('click',[\s\S]{0,120}preventDefault/);
});

test('правила меню в CSS: полоса значков скрывает ручки, но порядок тот же', () => {
  const css = read('web/css/components.css');
  assert.match(css, /\.room-drag\s*\{[\s\S]*?cursor: grab/);
  assert.match(css, /\.room-drop\s*\{/, 'подсветка места вставки');
  assert.match(css, /\.room-group--collapsed \.room-group__body \{ display: none; \}/);
  assert.match(css, /\.app\.is-rail \.room-drag[^{]*\{\s*display: none;/);
  assert.match(css, /@media \(max-width: 639px\)[\s\S]{0,80}\.room-drag \{ display: none; \}/);
});

test('список комнат не сжимается до нуля: иначе группа не принимает перетаскивание', () => {
  // W29-FIX1 п.1. В колонке `.sidebar__scroll` правило `.nav` с `min-height: 0`
  // и flex-сжатием по умолчанию стягивало список комнат в 0 px: строки рисовались
  // ПОВЕРХ «Недавнего», а курсор при перетаскивании попадал в «Недавнее», поэтому
  // пустая группа не принимала комнату (PUT уходил без поля `group`).
  const css = read('web/css/components.css');
  const rule = css.match(/\.nav \{[^}]*\}/);
  assert.ok(rule, 'правило .nav на месте');
  assert.match(rule[0], /flex: 0 0 auto/, 'список комнат — блок с собственной высотой');
  assert.doesNotMatch(rule[0], /min-height: 0/, 'сжатие до нуля запрещено');
});
