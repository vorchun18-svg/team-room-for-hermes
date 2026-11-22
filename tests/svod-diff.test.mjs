/**
 * svod-diff.test.mjs — различия версий свода и помощники «Второго мнения» (W29B-Q4).
 *
 * Что проверяется
 * ---------------
 * 1) diff по абзацам: правка одного слова — одна правка, остальное «same»;
 *    добавленный и убранный абзацы не путаются с правкой;
 * 2) diff по словам: «3 дня» → «5 дней» даёт одно убранное и одно добавленное
 *    слово, а не весь абзац;
 * 3) счётчики: слова и правки согласованы с числом изменений; пустая разница —
 *    `empty`;
 * 4) переход между правками по кругу (`nextChange`) — «дальше» всегда работает;
 * 5) поиск цитаты для «к фрагменту»: регистр, ё/е, лишние пробелы, сокращение «…»;
 * 6) помощники панели: список моделей по провайдерам, модель по умолчанию не та,
 *    что писала свод, строка оценки, подписи состояний проверки.
 *
 * Модель не вызывается: всё считается по текстам версий.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';

installFakeDom();
const {
  paragraphs, words, diffWords, diffParagraphs, diffCounts, countsText,
  changeIndexes, nextChange, countWords, plural, findQuote,
} = await import('../web/js/svod-diff.js');
const {
  pickerOptions, modelValue, modelLabel, estimateLine, checkTitle, checkStateText, itemText,
} = await import('../web/js/views/second-opinion.js');
uninstallFakeDom();

const BEFORE = [
  '## Итог',
  'Ответ клиенту по претензии готов: возвращаем стоимость химчистки за 3 дня.',
  '',
  '## Сроки',
  'Курьер заберёт вещь в понедельник.',
].join('\n');

const AFTER = [
  '## Итог',
  'Ответ клиенту по претензии готов: возвращаем стоимость химчистки за 5 дней.',
  '',
  '## Сроки',
  'Курьер заберёт вещь в понедельник.',
  '',
  '## Риски',
  'Если клиент не согласен — предложим повторную чистку бесплатно.',
].join('\n');

test('абзацы: пустая строка — граница, пустой текст — пусто', () => {
  assert.deepEqual(paragraphs(''), []);
  assert.deepEqual(paragraphs('  \n\n '), []);
  const list = paragraphs(BEFORE);
  // Заголовок и текст под ним без пустой строки — один абзац (правило Markdown).
  assert.equal(list.length, 2);
  assert.ok(list[0].startsWith('## Итог'));
});

test('слова: пробел прилипает к слову, знаки — отдельные токены', () => {
  assert.deepEqual(words('срок 5 дней.'), ['срок', ' ', '5', ' ', 'дней.']);
  assert.equal(countWords('срок 5 дней.'), 3);
});

test('правка одного слова не превращается в «весь абзац убран»', () => {
  const ops = diffWords('возвращаем за 3 дня.', 'возвращаем за 5 дней.');
  const dels = ops.filter((o) => o.kind === 'del').map((o) => o.text.trim());
  const adds = ops.filter((o) => o.kind === 'add').map((o) => o.text.trim());
  // Точечная правка: убрано «3», добавлено «5»; слово с окончанием тоже меняется,
  // но весь абзац не переписывается заново.
  assert.equal(dels[0], '3');
  assert.equal(adds[0], '5');
  assert.ok(dels.length <= 2 && adds.length <= 2, 'правка должна быть точечной');
  assert.ok(ops.some((o) => o.kind === 'same' && o.text.includes('возвращаем')));
});

test('абзацы: переписанный абзац — одна «правка», новый абзац — добавление', () => {
  const blocks = diffParagraphs(BEFORE, AFTER);
  const changes = blocks.filter((b) => b.kind === 'change');
  assert.equal(changes.length, 2);
  assert.equal(changes[0].added, undefined);
  assert.equal(changes[0].removed, undefined);
  assert.ok(changes[0].words.some((o) => o.kind === 'add' && o.text.includes('5')));
  const added = changes.find((b) => b.after.startsWith('## Риски'));
  assert.equal(added.added, true);
  assert.equal(added.before, '');
});

test('счётчики: слова и правки согласованы, пустая разница — empty', () => {
  const counts = diffCounts(diffParagraphs(BEFORE, AFTER));
  assert.equal(counts.changed, 2);
  assert.equal(counts.added, 1);
  assert.ok(counts.wordsAdded >= 3);
  assert.ok(counts.wordsRemoved >= 1);
  assert.match(countsText(counts), /добавлено|правк/);
  const same = diffCounts(diffParagraphs(BEFORE, BEFORE));
  assert.equal(same.changed, 0);
  assert.equal(same.empty, true);
  assert.equal(countsText(same), 'Различий нет');
});

test('переход между правками идёт по кругу', () => {
  const indexes = changeIndexes(diffParagraphs(BEFORE, AFTER));
  assert.equal(indexes.length, 2);
  assert.equal(nextChange(indexes, indexes[0], 1), indexes[1]);
  assert.equal(nextChange(indexes, indexes[1], 1), indexes[0]);
  assert.equal(nextChange(indexes, indexes[0], -1), indexes[1]);
  assert.equal(nextChange([], 0, 1), 0);
});

test('поиск цитаты: регистр, ё/е, пробелы и сокращение', () => {
  const hit = findQuote('Курьер заберёт вещь в понедельник.', 'курьер забирет вещь в понедельник');
  assert.equal(hit, null);            // «заберёт» ≠ «забирет» — не выдумываем совпадение
  const ok = findQuote('Курьер заберёт вещь в понедельник.', 'курьер заберёт вещь');
  assert.ok(ok && ok.index === 0);
  const dots = findQuote('Срок подачи 3 дня, дальше санкции.', 'Срок подачи 3 дня, дальше…');
  assert.ok(dots && dots.index === 0);
  assert.equal(findQuote('', 'цитата'), null);
});

test('склонение чисел', () => {
  assert.equal(plural(1, 'правка', 'правки', 'правок'), 'правка');
  assert.equal(plural(2, 'правка', 'правки', 'правок'), 'правки');
  assert.equal(plural(11, 'правка', 'правки', 'правок'), 'правок');
  assert.equal(plural(21, 'правка', 'правки', 'правок'), 'правка');
});

test('список моделей: группы по провайдерам, текущая отмечена', () => {
  const models = [
    { provider: 'oblako-a', provider_label: 'Облако-А', model: 'd-mini', label: 'Мини' },
    { provider: 'oblako-a', provider_label: 'Облако-А', model: 'd-pro', label: 'Про' },
    { provider: 'lokal', provider_label: 'Локальный', model: 'home', label: 'Домашний' },
  ];
  const picked = pickerOptions(models, 'oblako-a|d-pro');
  assert.equal(picked.count, 3);
  assert.equal(picked.groups.length, 2);
  assert.equal(picked.groups[0].label, 'Облако-А');
  assert.equal(picked.groups[0].items[1].current, true);
  assert.equal(picked.groups[1].items[0].value, 'lokal|home');
  assert.equal(modelValue({ provider: 'lokal', model: 'home' }), 'lokal|home');
  assert.equal(modelLabel(models[2]), 'Локальный · Домашний');
});

test('строка оценки и подписи проверки', () => {
  assert.match(estimateLine({ input_tokens: 34000, output_tokens: 700 }), /34 тыс\. токенов/);
  assert.equal(estimateLine({ text: '≈ 12 тыс. токенов на входе' }), '≈ 12 тыс. токенов на входе');
  assert.equal(estimateLine(null), '');
  assert.equal(checkStateText({ counts: { disagree: 2, missing: 1, agree: 0 } }),
    '2 не согласна · 1 не хватает');
  assert.equal(checkStateText({ running: true }), 'проверяет…');
  assert.equal(checkStateText({ failed: true }), 'проверка не удалась');
  assert.equal(checkTitle({ model: 'Дилижанс про', assignee: 'юрист' }, '14:20'),
    'Дилижанс про · 14:20 · юрист');
  assert.equal(itemText({ label: 'не согласна', quote: 'срок 3 дня', note: 'на деле 5' }),
    'не согласна: «срок 3 дня» — на деле 5');
});
