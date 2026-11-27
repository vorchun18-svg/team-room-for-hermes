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

import {
  stepLayers, stepRole, runStateRu, stepStatusRu, recipeLine, EXPLAIN_LINES, dialogStageRu,
  waitingText, draftLine, analysisLine, estimateLine, offerLine, sampleLabel, tokensRu,
  dueBanner, laterLine,
} from '../web/js/views/recipes.js';
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
  assert.ok(src.includes('Рассказать руководителю'), 'пустой экран ведёт в разговор');
  assert.ok(src.includes('Собрать вручную'), 'ручной редактор остался запасным');
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

test('W31-C: вверху ровно три строки пояснения и без слова «запрос»', () => {
  assert.equal(EXPLAIN_LINES.length, 3);
  for (const line of EXPLAIN_LINES) {
    assert.ok(line.length > 30, 'строка объясняет смысл, а не «нет данных»');
    assert.ok(!/запрос/i.test(line), 'слово «запрос» в видимом тексте запрещено');
  }
  assert.ok(/не крон/i.test(EXPLAIN_LINES[2]), 'человек должен видеть: само не запускается');
  assert.ok(/Запустить/.test(EXPLAIN_LINES[1]), 'сказано, что делать');
  const src = read('web/js/views/recipes.js');
  assert.ok(src.includes("'recipes-explain'"), 'пояснение на экране, а не в коде');
});

test('W31-C: стадии разговора и строка ожидания по-русски', () => {
  assert.equal(dialogStageRu('asked'), 'руководитель готовит ответ');
  assert.equal(dialogStageRu('questions'), 'нужны ваши уточнения');
  assert.equal(dialogStageRu('draft'), 'алгоритм готов — утвердите');
  assert.equal(dialogStageRu('accepted'), 'рецепт сохранён');
  assert.match(waitingText({ stage: 'questions', questions: [{}, {}] }), /ответьте на 2 вопроса/);
  assert.match(waitingText({ stage: 'draft' }), /Да, сохранить/);
  assert.match(waitingText({}), /готовит ответ/);
});

test('W31-C: строка алгоритма — поля, шаги, критик', () => {
  assert.equal(draftLine({ fields: [{}], steps: [{}, { critic: true }] }),
    '1 поле · 2 шага · проверяет критик');
  assert.equal(draftLine({}), '0 полей · 0 шагов');
  const src = read('web/js/views/recipes.js');
  for (const needle of ['Да, сохранить', 'Исправить', 'Поля при запуске', 'Шаги и зависимости']) {
    assert.ok(src.includes(needle), 'в диалоге есть «' + needle + '»');
  }
});

test('W31-C: разбор задач — настройки, прикидка токенов, предложение', () => {
  const analysis = {
    settings: { enabled: true, every_days: 7, min_tasks: 3, model_label: 'модель руководителя' },
    estimate: { input_tokens: 4200, output_tokens: 900 },
    candidates: { count: 5 },
  };
  assert.equal(analysisLine(analysis), 'Раз в 7 дней, не меньше 3 задач · модель: модель руководителя');
  assert.match(analysisLine({ settings: { enabled: false } }), /^Разбор выключен/);
  assert.match(estimateLine(analysis), /Задач для разбора: 5 · прикидка: ≈ 4,2 тыс\. токенов/);
  assert.match(estimateLine({ candidates: { count: 0 } }), /нет — предлагать нечего/);
  assert.equal(tokensRu(900), '900', 'без «0 тыс.»');
  assert.equal(tokensRu(4200), '4,2 тыс.');
  assert.equal(tokensRu(31837), '32 тыс.');
  assert.equal(offerLine({ why: 'похожи', tasks: ['a', 'b'] }), 'похожи — по задачам: a, b');
  assert.match(offerLine({ tasks: [] }), /по завершённым задачам/);
  assert.match(sampleLabel({}), /образец/);
  assert.match(sampleLabel({ sample_note: 'образец чужой сферы' }), /чужой сферы/);
});

test('W31-C: мок отвечает той же формой, что живой сервер (диалог и разбор)', () => {
  const mock = read('tools/mock_recipes.py');
  for (const needle of ['dialog', 'analysis', 'samples']) {
    assert.ok(mock.includes(needle), 'мок умеет ' + needle);
  }
  const server = read('server/recipes.py');
  assert.ok(server.includes('"samples"'), 'сервер отдаёт samples');
  assert.ok(server.includes('"sample"'), 'у рецепта есть пометка sample');
  const agent = read('server/recipe_agent.py');
  assert.ok(agent.includes('MARK = "[Руководитель · рецепт]"'), 'разговор через комментарий руководителя');
  assert.ok(agent.includes('OFFER_MARK'), 'разбор задач — своя метка');
  assert.ok(agent.includes('TEAMROOM_RECIPE_FAKE'), 'подставной руководитель только в песочнице');
  assert.ok(agent.includes('every_days'), 'настройка «раз в N дней»');
  assert.ok(agent.includes('dismissed'), 'отклонённое помнится');
});

test('W31-FIX3: плашка «пора провести разбор» — пометка, а запуск человеком', () => {
  const due = { prompt: { show: true, count: 4, tokens: 4200, due_at: 1 } };
  assert.equal(dueBanner(due),
    'Пора провести разбор задач (новых: 4, примерно 4,2 тыс. токенов)');
  assert.equal(dueBanner({ prompt: { show: true, count: 1, tokens: 900 } }),
    'Пора провести разбор задач (новых: 1, примерно 900 токенов)');
  assert.equal(dueBanner({ prompt: { show: false } }), '', 'нет пометки — нет плашки');
  assert.equal(dueBanner({}), '');
  assert.equal(dueBanner(null), '');
  assert.match(laterLine(3), /Отложили на 3 дня/);
  assert.match(laterLine(1), /на 1 день/);
  assert.match(laterLine(7), /на 7 дней/);

  const src = read('web/js/views/recipes.js');
  for (const needle of ['analysis-due', 'analysis-due-run', 'analysis-due-later',
    "'/analysis/later'", 'Запустить', 'Позже', 'Пора провести разбор задач']) {
    assert.ok(src.includes(needle), 'в плашке есть «' + needle + '»');
  }
  // плашка не запускает разбор сама: она только рисует кнопки
  assert.ok(!/analysis\/run/.test(src.split('function startAnalysis')[0].split('function analysisPanel')[1] || ''),
    'в разметке плашки нет автозапуска');

  const agent = read('server/recipe_agent.py');
  assert.ok(agent.includes('DEFAULT_SETTINGS = {"enabled": False'),
    'напоминание о разборе по умолчанию выключено');
  const tickBody = agent.split('def tick(cfg, room)')[1].split('__all__')[0];
  assert.ok(!tickBody.includes('_analysis_card'),
    'тик не ставит карточку «Анализ задач» — это делает только run');
  assert.ok(tickBody.includes('due_at'), 'тик только помечает due');
  assert.ok(agent.includes('def later('), '«Позже» откладывает напоминание');
});
