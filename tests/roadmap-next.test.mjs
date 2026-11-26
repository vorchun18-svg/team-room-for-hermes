/**
 * roadmap-next.test.mjs — W20-U2, пункт 16 (P12): «Карта проекта».
 *
 * Аудит: «Что дальше» было дважды, у пустой карты три заглушки, «дальше» стояло
 * статусом у каждого этапа. Проверяем:
 *  • один блок «Что дальше» и правильный «следующий этап»;
 *  • шаги из запросов — по сроку, без выполненных, не больше четырёх;
 *  • пустая карта объясняется одним текстом с одним-двумя действиями;
 *  • статус этапа — значок + цвет + слово, одинаково во всей карте.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  legendItems, nextPhase, nextSteps, nextBlock, emptyPlan, stepsCountLabel,
} from '../web/js/views/roadmap-next.js';

const SOURCE = readFileSync(new URL('../web/js/views/roadmap.js', import.meta.url), 'utf8');
const CSS = readFileSync(new URL('../web/css/view-roadmap.css', import.meta.url), 'utf8');

const ROADMAP = {
  current: { id: 'legal', title: 'Легализация' },
  phases: [
    { id: 'idea', title: 'Идея и стратегия', status: 'done' },
    { id: 'legal', title: 'Легализация', status: 'active', note: 'Ждём ответа поставщика о сроках.' },
    { id: 'brand', title: 'Бренд и регистрация', status: 'blocked' },
    { id: 'pack', title: 'Упаковка', status: 'next' },
  ],
  decisions: [],
};

const BACKLOG = [
  { id: 'b3', kind: 'next_step', status: 'open', text: 'Согласовать макет с типографией', due: '2026-11-20' },
  { id: 'b1', kind: 'next_step', status: 'open', text: 'Собрать документы по договору', due: '2026-11-12' },
  { id: 'b2', kind: 'next_step', status: 'asked', text: 'Уточнить реквизиты у бухгалтера', due: '2026-11-14' },
  { id: 'b9', kind: 'next_step', status: 'done', text: 'Отправить заявку', due: '2026-11-01' },
  { id: 'q1', kind: 'question', status: 'open', text: 'Это вопрос, а не шаг', due: '2026-11-02' },
  { id: 'b4', kind: 'next_step', status: 'open', text: 'Проверить сроки производства', due: null },
];

/* ─────────────────────────────────────────────── следующий этап */

test('следующий этап: текущий, пока не готов; иначе в работе → ждёт решения → не начат', () => {
  assert.equal(nextPhase(ROADMAP).id, 'legal');
  assert.equal(nextPhase({ current: { id: 'legal' }, phases: [{ id: 'legal', status: 'done' }, { id: 'brand', status: 'blocked' }, { id: 'pack', status: 'next' }] }).id,
    'brand', 'текущий готов — берём тот, что ждёт решения');
  assert.equal(nextPhase({ phases: [{ id: 'a', status: 'next' }, { id: 'b', status: 'active' }] }).id, 'b');
  assert.equal(nextPhase({ phases: [{ id: 'a', status: 'done' }, { id: 'b', status: 'done' }] }), null,
    'все готовы — следующего нет');
  assert.equal(nextPhase({ phases: [] }), null);
  assert.equal(nextPhase(null), null);
  // current может прийти строкой (id), как у части ответов сервера.
  assert.equal(nextPhase({ current: 'pack', phases: ROADMAP.phases }).id, 'pack');
});

/* ───────────────────────────────────────────────────── шаги «что дальше» */

test('шаги: только открытые, по сроку, без выполненных и не четыре+', () => {
  const steps = nextSteps(BACKLOG, 3);
  assert.deepEqual(steps.map((s) => s.id), ['b1', 'b2', 'b3'], 'по сроку: 12.11 → 14.11 → 20.11');
  assert.equal(nextSteps(BACKLOG, 10).length, 4, 'без срока — в конце, и всё равно не больше открытых');
  assert.equal(nextSteps(BACKLOG, 10)[3].id, 'b4');
  assert.ok(!nextSteps(BACKLOG).some((s) => s.status === 'done'));
  assert.ok(!nextSteps(BACKLOG).some((s) => s.kind === 'question'));
  assert.deepEqual(nextSteps([], 4), []);
  assert.equal(stepsCountLabel(1), '1 шаг');
  assert.equal(stepsCountLabel(3), '3 шага');
  assert.equal(stepsCountLabel(11), '11 шагов');
  assert.equal(stepsCountLabel(0), 'шагов нет');
});

test('блок «Что дальше»: этап, пояснение и шаги одним ответом', () => {
  const block = nextBlock(ROADMAP, BACKLOG, { limit: 2 });
  assert.equal(block.phase.id, 'legal');
  assert.equal(block.note, 'Ждём ответа поставщика о сроках.');
  assert.equal(block.steps.length, 2);
  assert.equal(block.allDone, false);
  assert.equal(block.hasPhases, true);
  assert.equal(block.empty, false);
  assert.match(block.line, /Легализация.*в работе/);

  const done = nextBlock({ phases: [{ id: 'a', status: 'done' }] }, []);
  assert.equal(done.allDone, true);
  assert.equal(done.phase, null);
  assert.match(done.line, /Все этапы пройдены/);
  assert.equal(done.empty, true);

  const empty = nextBlock({ phases: [] }, []);
  assert.equal(empty.hasPhases, false);
  assert.match(empty.line, /Этапов пока нет/);
});

/* ────────────────────────────────────────────── пустая карта: одна карточка */

test('пустая карта: одно объяснение и действия вместо трёх заглушек', () => {
  const on = emptyPlan({ modeOn: true });
  assert.match(on.title, /Карта ещё не собрана/);
  assert.ok(on.text.length > 180, 'объяснение длинное и человеческое');
  // W29-P6: главное действие пустой карты — «Собрать этапы по выполненным
  // запросам» (эвристика по готовым запросам, без запуска модели): раньше тут
  // была только просьба к руководителю, и карта оставалась пустой, пока человек
  // не догадается. Ручное добавление этапов осталось вторым действием.
  assert.equal(on.primary.key, 'build');
  assert.equal(on.primary.label, 'Собрать этапы по выполненным задачам');
  assert.equal(on.secondary.key, 'edit');
  assert.equal(on.secondary.label, 'Добавить этапы вручную');

  // Пока предложение ждёт решения — второй карточки руководителю не предлагаем (409).
  assert.equal(emptyPlan({ modeOn: true, proposalPending: true }).secondary, null);

  const off = emptyPlan({ modeOn: false });
  assert.match(off.title, /Проектный режим выключен/);
  assert.match(off.text, /одним общим ответом/);
  assert.equal(off.primary.key, 'mode');
  assert.equal(off.primary.label, 'Включить проектный режим');
  assert.equal(off.secondary, null);
});

/* ─────────────────────────────────────── статус этапа: значок, цвет, слово */

test('легенда: у каждого статуса значок, цвет и слово', () => {
  const items = legendItems();
  assert.deepEqual(items.map((i) => i.status), ['done', 'active', 'blocked', 'next']);
  assert.deepEqual(items.map((i) => i.label), ['готово', 'в работе', 'ждёт решения', 'не начат']);
  assert.ok(items.every((i) => i.icon && i.tone), 'у каждого — значок и цвет');
  assert.deepEqual(items.map((i) => i.tone), ['ok', 'info', 'warn', 'muted']);
});

/* ───────────────────────────────────────────────── карта: разметка и клик */

test('roadmap.js: один блок «Что дальше» и никаких старых заглушек', () => {
  const literal = /'Что дальше'/g;
  const total = (SOURCE.match(literal) || []).length;
  assert.equal(total, 2, 'заголовок «Что дальше» есть только в одном компоненте (две ветки), найдено ' + total);
  const start = SOURCE.indexOf('function renderNext()');
  const end = SOURCE.indexOf('function paintNext(');
  assert.ok(start > 0 && end > start, 'блок собирается одной функцией renderNext');
  const body = SOURCE.slice(start, end);
  const inside = (body.match(literal) || []).length;
  assert.equal(inside, total, 'заголовок встречается только внутри renderNext');
  assert.ok(!SOURCE.includes('nextStepsCard'), 'старая вторая карточка «Что дальше» убрана');
  assert.ok(!SOURCE.includes('projectModeCard'), 'одна карточка вместо «Что дальше» + «про режим»');
  assert.ok(SOURCE.includes('nextBlock('), 'блок строится из одного расчёта');
  assert.ok(SOURCE.includes('emptyPlan('), 'пустая карта объясняется тем же модулем');
  assert.ok(SOURCE.includes('legendItems('), 'под полосой этапов есть легенда статусов');
});

test('клик по этапу: кнопка на весь этап, видимое состояние и без путаницы с редактором', () => {
  assert.ok(SOURCE.includes('rm-step__btn'), 'этап — кнопка, а не карточка без действия');
  assert.ok(SOURCE.includes('aria-expanded'), 'у кнопки этапа есть состояние раскрытия');
  assert.ok(SOURCE.includes('data-phase'), 'этап помечен id — тесты и стили находят его');
  assert.ok(SOURCE.includes('selectPhase('), 'клик выбирает этап');
  assert.ok(SOURCE.includes('Изменить этапы'), 'редактор этапов остаётся отдельной кнопкой');
  // Редактор открывается только своей кнопкой: внутри кнопки этапа нет openPhases.
  const stepBtn = SOURCE.slice(SOURCE.indexOf('function stepNode'), SOURCE.indexOf('function paintSteps'));
  assert.ok(!stepBtn.includes('openPhases'), 'клик по этапу не открывает редактор (FC)');
  assert.ok(SOURCE.includes('Нажмите на этап'), 'под полосой есть подсказка, что этап нажимается');

  assert.ok(CSS.includes('.rm-legend'), 'легенда оформлена');
  assert.ok(CSS.includes('.rm-step__chevron'), 'у выбранного этапа есть указатель');
  assert.ok(CSS.includes('.rm-step--selected .rm-step__chevron'), 'указатель виден только у выбранного');
  assert.ok(CSS.includes('.rm-next'), 'блок «Что дальше» оформлен');
});
