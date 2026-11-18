/**
 * tests/request-blocks.test.mjs — поведение блоков страницы запроса (W25-L2).
 *
 * Что проверяем:
 *   * умолчания блоков по ширине вида (R5 §7.2) — одна функция `sectionDefaults`;
 *   * «свод всегда первый и не сворачивается» — договором исходников;
 *   * «Что изменилось» — свёрнуто при виде < 1100 и при версии 1;
 *   * один открытый вопрос в колонке (R5 §9 п. 9) — договором разметки;
 *   * «Хронология» — последние 5, а не 6;
 *   * состояние блоков: явный выбор человека сильнее умолчания (R5 §7.7).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { sectionDefaults } from '../web/js/views/section-collapse.js';
import { createSectionState } from '../web/js/views/request-tree-state.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(join(ROOT, rel), 'utf8');

function memStore() {
  const map = new Map();
  return {
    get: (k) => (map.has(k) ? map.get(k) : null),
    set: (k, v) => { map.set(k, String(v)); },
    remove: (k) => { map.delete(k); },
  };
}

test('умолчания: диалог, ход работы, дерево, артефакты, запрос — свёрнуты на любом виде', () => {
  for (const layout of ['xl', 'l', 'm', 's', 'xs']) {
    const d = sectionDefaults({ status: 'working' }, { layout, version: 3 });
    assert.equal(d.ask, false, layout);
    assert.equal(d.thread, false, layout);
    assert.equal(d.worklog, false, layout);
    assert.equal(d.tree, false, layout);
    assert.equal(d.artifacts, false, layout);
    assert.equal(d.timeline, false, layout);
  }
});

test('«Что изменилось»: открыто только на xl/l и только при версии > 1', () => {
  assert.equal(sectionDefaults({}, { layout: 'xl', version: 3 }).changed, true);
  assert.equal(sectionDefaults({}, { layout: 'l', version: 2 }).changed, true);
  assert.equal(sectionDefaults({}, { layout: 'm', version: 3 }).changed, false);
  assert.equal(sectionDefaults({}, { layout: 's', version: 3 }).changed, false);
  assert.equal(sectionDefaults({}, { layout: 'xs', version: 3 }).changed, false);
  // Первая версия — сравнивать не с чем.
  assert.equal(sectionDefaults({}, { layout: 'xl', version: 1 }).changed, false);
  assert.equal(sectionDefaults({}, { layout: 'xl' }).changed, false);
});

test('умолчания: неизвестный режим считается узким (безопасная сторона)', () => {
  assert.equal(sectionDefaults({}, { version: 5 }).changed, false);
  assert.equal(sectionDefaults({}, { layout: '???', version: 5 }).changed, false);
});

test('выбор человека сильнее умолчания: явное значение в хранилище не перебивается', () => {
  const store = memStore();
  const state = createSectionState({ requestId: 't1', store });
  assert.equal(state.has('thread'), false);
  assert.equal(state.isOpen('thread', false), false);
  state.set('thread', true);
  assert.equal(state.has('thread'), true);
  assert.equal(state.isOpen('thread', false), true, 'явный выбор должен побеждать умолчание');
  state.set('thread', false);
  assert.equal(state.isOpen('thread', true), false);
});

test('«Свод» не сворачивается ни при какой ширине (договор исходников)', () => {
  const answer = read('web/js/views/request-answer.js');
  // Умолчание «Что изменилось» читает режим вида и версию свода.
  assert.match(answer, /sectionDefaults\(d, \{ layout, version: entry\.version \}\)/);
  // Блок оформлен общим компонентом (одна стрелка, одна разметка).
  assert.match(answer, /createSection\(\{\s*\n\s*key: 'changed'/);
});

test('«Хронология» — последние 5 и кнопка «показать все» (R5 §7.2)', () => {
  const src = read('web/js/views/request-parts.js');
  assert.match(src, /limit: 5/);
  assert.ok(src.includes('if (events.length > 5) card.appendChild(more);'));
  assert.ok(!src.includes('limit: 6'), 'старого лимита 6 быть не должно');
});

test('один открытый вопрос: строка-заголовок и скрытая форма (R5 §9 п. 9)', () => {
  const aside = read('web/js/views/request-aside.js');
  // Разметка: заголовок — «кнопка» (role=button), форма скрывается стилем.
  assert.match(aside, /class: 'req-q__head'/);
  assert.match(aside, /role: 'button'/);
  assert.match(aside, /'data-open': open \? '1' : '0'/);
  // Ровно одна форма: клик по строке разворачивает её и сворачивает прежнюю.
  assert.match(aside, /function expandQuestion\(/);
  assert.match(aside, /openQId = String\(id \|\| ''\)/);
  // Развёрнутость участвует в подписи строки — иначе опрос не пересобрал бы строку.
  assert.match(aside, /function qSig\(q, open\)/);
  assert.match(aside, /open === true,/);
  // Текст ответа не теряется: поле остаётся в DOM и значение живёт в keepValues.
  assert.match(aside, /keepValues\.set\(key, ta\.value\)/);
});

test('CSS прячет форму свёрнутого вопроса, но не удаляет поле', () => {
  const css = read('web/css/view-blocks.css');
  assert.match(css, /\.req-q\[data-open="0"\] \.req-q__form \{ display: none; \}/);
  assert.match(css, /\.req-q\[data-open="0"\] \.req-q__input \{ display: none; \}/);
});

test('свод не зависит от плотности: кегль 16 px / 1,62 отдельным токеном', () => {
  const css = read('web/css/view-blocks.css');
  assert.match(css, /--svod-fs: 16px/);
  assert.match(css, /--svod-lh: 1\.62/);
  // Плотность меняет ритм блоков, но кегль свода задаётся только токеном.
  assert.match(css, /\.req-answer__body \{ font-size: var\(--svod-fs\); line-height: var\(--svod-lh\); \}/);
});

test('«Фокус» читает и пишет только data-focus и прячет своё меню (оболочку L1 не переписываем)', () => {
  const css = read('web/css/view-blocks.css');
  // Класс меню в оболочке — `#sidebar.sidebar` (не `.app__sidebar`): одно правило
  // прячет его, второе ставит область вида в первую строку сетки — иначе при
  // одной колонке содержимое уезжает во вторую (неявную) строку нулевой высоты.
  assert.match(css, /#app\[data-focus="on"\] \.sidebar \{ display: none; \}/);
  assert.match(css, /#app\[data-focus="on"\] \.app__main \{ grid-column: 1; grid-row: 1; \}/);
  assert.match(css, /#app\[data-focus="on"\] \.req__aside/);
  assert.match(css, /#app\[data-focus="on"\] \.req \.sec\.is-collapsed/);
  const js = read('web/js/views/request.js');
  assert.match(js, /installFocusMode\(/);
  assert.match(js, /ev\.shiftKey && \(key === 'F'/);
  assert.match(js, /applyLayout\(\)/);
});

test('плотность ставится только через density.js и атрибут data-density', () => {
  const js = read('web/js/views/request.js');
  assert.match(js, /installDensity\(\{ app: appEl \}\)/);
  const header = read('web/js/views/request-header.js');
  assert.match(header, /applyDensity\(app, nextDensity\(dens\), store\)/);
});
