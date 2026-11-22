/**
 * recipes-view.test.mjs — W29B-Q1: вкладка «Рецепты» (шаблоны процессов).
 *
 * Что проверяем:
 *  · граф шагов раскладывается слоями (зависимости уважаются, цикл не вешает);
 *  · подписи состояний запуска/шага по-русски;
 *  · пустой экран объясняет, что такое рецепт, и даёт кнопки;
 *  · вкладка «Рецепты» есть в TABS и её имя — из text-ru.js;
 *  · мок отвечает теми же ключами, что живой сервер (одна форма данных).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { stepLayers, stepRole, runStateRu, stepStatusRu, recipeLine } from '../web/js/views/recipes.js';
import { TABS } from '../web/js/router.js';
import { TAB_TITLES } from '../web/js/text-ru.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(join(HERE, '..', rel), 'utf8');

const STEPS = [
  { id: 's1', title: 'Проверить', depends_on: [] },
  { id: 's2', title: 'Собрать', depends_on: ['s1'] },
  { id: 's3', title: 'Ответ', depends_on: ['s1', 's2'] },
];

test('граф шагов: слои по зависимостям', () => {
  const layers = stepLayers(STEPS);
  assert.deepEqual(layers.map((l) => l.map((s) => s.id)), [['s1'], ['s2'], ['s3']]);
  assert.deepEqual(stepLayers([]), []);
  // два независимых первых шага — в одном слое
  const two = stepLayers([
    { id: 'a', depends_on: [] }, { id: 'b', depends_on: [] }, { id: 'c', depends_on: ['a'] },
  ]);
  assert.deepEqual(two[0].map((s) => s.id), ['a', 'b']);
  assert.deepEqual(two[1].map((s) => s.id), ['c']);
});

test('граф шагов: цикл и битая ссылка не вешают экран', () => {
  const cyclic = stepLayers([
    { id: 'a', depends_on: ['b'] }, { id: 'b', depends_on: ['a'] },
  ]);
  assert.ok(cyclic.length >= 1);
  assert.equal(cyclic.flat().length, 2);
  const broken = stepLayers([{ id: 'a', depends_on: ['gone'] }]);
  assert.deepEqual(broken.map((l) => l.map((s) => s.id)), [['a']]);
});

test('подписи шага и запуска — по-русски', () => {
  assert.equal(stepRole({ role: 'юрист', assignee: 'team-lawyer' }), 'юрист · team-lawyer');
  assert.equal(stepRole({ role: '', assignee: 'team-ops' }), 'team-ops');
  assert.equal(stepRole({}), 'участник');
  assert.equal(runStateRu('running'), 'идёт');
  assert.equal(runStateRu('done'), 'готово');
  assert.equal(runStateRu('failed'), 'ошибка');
  assert.equal(stepStatusRu('pending'), 'ждёт предыдущий шаг');
  assert.equal(stepStatusRu('created'), 'создан');
  assert.equal(stepStatusRu('blocked'), 'на паузе');
});

test('строка рецепта: поля и шаги с правильным числительным', () => {
  assert.equal(recipeLine({ fields: [{}, {}], steps: [{}, {}, {}] }), '2 поля · 3 шага');
  assert.equal(recipeLine({ fields: [{}], steps: [{}] }), '1 поле · 1 шаг');
  assert.equal(recipeLine({}), '0 полей · 0 шагов');
});

test('вкладка «Рецепты» объявлена и названа один раз', () => {
  const tab = TABS.find((t) => t.id === 'recipes');
  assert.ok(tab, 'вкладка есть в router.js TABS');
  assert.equal(tab.title, 'Рецепты');
  assert.equal(TAB_TITLES.recipes, 'Рецепты', 'подпись — из text-ru.js');
});

test('экран: пустое состояние с делом и кнопками, без «пока ничего нет»', () => {
  const src = read('web/js/views/recipes.js');
  assert.ok(src.includes('Рецептов пока нет'));
  assert.ok(src.includes('Создать из запроса'));
  assert.ok(src.includes('Создать с нуля'));
  assert.ok(!src.includes('Пока ничего нет'), 'пустой экран объясняет смысл');
  assert.ok(src.includes('Запустить'), 'у рецепта есть кнопка запуска');
  assert.ok(src.includes('Вернуть'), 'корзина возвращает рецепт');
});

test('мок и сервер отвечают одной формой', () => {
  const fixture = JSON.parse(read('fixtures/recipes_cafe.json'));
  for (const key of ['items', 'trash', 'runs', 'roles']) {
    assert.ok(Array.isArray(fixture[key]), `fixtures/recipes_cafe.json: ${key}`);
  }
  for (const item of fixture.items) {
    for (const key of ['id', 'name', 'fields', 'steps']) assert.ok(key in item, `у рецепта нет ${key}`);
    for (const step of item.steps) {
      assert.ok('title' in step && 'depends_on' in step, 'шаг с зависимостями');
    }
  }
  const server = read('server/recipes.py');
  for (const key of ['\"items\"', '\"trash\"', '\"runs\"', '\"roles\"']) {
    assert.ok(server.includes(key), `server/recipes.py отдаёт ${key}`);
  }
  const mock = read('tools/mock_recipes.py');
  assert.ok(mock.includes('recipes_cafe.json'));
  assert.ok(read('tools/mock_read.py').includes('mock_recipes'), 'мок подключён к чтению');
  assert.ok(read('tools/mock_write.py').includes('mock_recipes'), 'мок подключён к записи');
});
