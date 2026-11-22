/**
 * tests/vocab.test.mjs — единый словарь интерфейса (W20-U1, пункт 11, аудит P7).
 *
 * Что проверяем, глядя на слова, а не на код:
 *  • вкладка называется «От вас нужно» — и в шапке, и в крошках, и в палитре
 *    (все три берут имя из одной таблицы TABS);
 *  • старых подписей не осталось ни в одном файле интерфейса («Дальше»,
 *    «Свод команды»);
 *  • у нетривиальных слов (свод, раунд, критик, версия) есть объяснение для «?»,
 *    и оно человеческое — про смысл, а не про технику;
 *  • «раунд» — одно слово: числительное согласуется (1 раунд, 2 раунда).
 *
 * Эти тесты КРАСНЫЕ на коде до W20-U1: там вкладка называлась «Дальше», а
 * объяснений у слов не было вовсе.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  TERMS, TAB_TITLES, SCOPE_LABELS, term, termHint, termTitle,
  roundsLabel, versionsLabel, tabTitleOf,
} from '../web/js/text-ru.js';
import { TABS, TAB_IDS, tabTitle } from '../web/js/router.js';
import { TAB_ORDER } from '../web/js/views/palette-model.js';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

/** Все .js нашего интерфейса (web/js рекурсивно) — по ним ищем старые подписи. */
function uiFiles(dir = join(root, 'web', 'js'), out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) uiFiles(p, out);
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}

/* ───────────────────────── вкладка: одно имя во всех местах ─────────────── */

test('вкладка «Дальше» переименована в «От вас нужно»', () => {
  const tab = TABS.find((t) => t.id === 'backlog');
  assert.ok(tab, 'вкладка backlog на месте (маршруты не меняются)');
  assert.equal(tab.title, 'От вас нужно');
  assert.equal(tabTitle('backlog'), 'От вас нужно');
  assert.equal(TAB_TITLES.backlog, 'От вас нужно');
});

test('имена вкладок берутся из словаря — шапка, крошки и палитра не расходятся', () => {
  for (const t of TABS) {
    assert.equal(t.title, TAB_TITLES[t.id], `вкладка ${t.id}: имя из словаря`);
    assert.equal(tabTitleOf(t.id), t.title, `tabTitleOf(${t.id})`);
    assert.equal(tabTitle(t.id), t.title, `tabTitle(${t.id})`);
  }
  // W26-QA1: id семи прежних вкладок и их относительный порядок прежние — ссылки и
  // закладки не ломаются; «Дела» встали рядом с «От вас нужно», а новые служебные
  // («Входящие», «Дежурства», «Рецепты», «Архив») — в хвост, чтобы не забивать шапку.
  assert.deepEqual(TAB_IDS,
    ['composer', 'team', 'roadmap', 'backlog', 'delos', 'reports', 'journal', 'inbox', 'duties',
      'recipes', 'archive'],
    'id вкладок и их порядок: старые на месте, Дела рядом с «От вас нужно»');
  assert.equal(TAB_IDS.indexOf('delos'), TAB_IDS.indexOf('backlog') + 1,
    '«Дела» стоят сразу после «От вас нужно»');
  // Палитра держит свой запасной список вкладок — он обязан совпадать с маршрутами,
  // иначе Ctrl+K предложит разделы в другом порядке (или не все).
  assert.deepEqual(TAB_ORDER, TAB_IDS, 'запасной список палитры = список вкладок');
});

test('ни в одном файле интерфейса не осталось старых подписей', () => {
  const bad = [];
  for (const p of uiFiles()) {
    const src = readFileSync(p, 'utf8');
    if (src.includes('«Дальше»')) bad.push([p, '«Дальше»']);
    if (src.includes('Свод команды')) bad.push([p, 'Свод команды']);
    if (src.includes("title: 'Дальше'")) bad.push([p, "title: 'Дальше'"]);
  }
  assert.deepEqual(bad, [], 'старые подписи остались: ' + JSON.stringify(bad));
});

/* ─────────────────────────── объяснения у слов («?») ────────────────────── */

test('словарь объясняет нетривиальные слова: свод, раунд, критик, версия', () => {
  for (const key of ['svod', 'round', 'critic', 'version']) {
    const t = term(key);
    assert.ok(t.hint.length > 40, `${key}: объяснение есть и оно не пустое`);
    assert.ok(t.title && t.title[0] === t.title[0].toUpperCase(), `${key}: заголовок с большой буквы`);
    assert.ok(t.name, `${key}: слово для строки текста`);
  }
  assert.equal(termTitle('svod'), 'Свод');
  assert.equal(termTitle('round'), 'Раунд');
});

test('объяснение говорит о смысле, а не о технике', () => {
  const svod = termHint('svod');
  assert.match(svod, /свод/i);
  assert.ok(!/json|api|metadata|провайдер|id/i.test(svod), 'без техники: ' + svod);
  const round = termHint('round');
  assert.match(round, /раунд/i);
  assert.match(round, /критик/i, 'раунд объясняется через критика');
  const ver = termHint('version');
  assert.match(ver, /верси/i);
  const critic = termHint('critic');
  assert.match(critic, /критик/i);
});

test('неизвестное слово не ломает интерфейс', () => {
  assert.equal(termHint('нет-такого'), '');
  assert.equal(termTitle('нет-такого'), '');
  assert.deepEqual(Object.keys(term('нет-такого')).sort(), ['example', 'hint', 'key', 'name', 'title']);
});

/* ───────────────────────────── «раунд» — одно слово ────────────────────── */

test('раунды согласуются по-русски и называются одним словом', () => {
  assert.equal(roundsLabel(1), '1 раунд');
  assert.equal(roundsLabel(2), '2 раунда');
  assert.equal(roundsLabel(5), '5 раундов');
  assert.equal(roundsLabel(11), '11 раундов');
  assert.equal(roundsLabel(21), '21 раунд');
  assert.equal(TERMS.round.name, 'раунд');
  assert.equal(versionsLabel(1), '1 версия');
  assert.equal(versionsLabel(3), '3 версии');
  assert.equal(versionsLabel(7), '7 версий');
});

test('переключатель охвата говорит «Вся комната / Этот запрос»', () => {
  assert.equal(SCOPE_LABELS.whole, 'Вся комната');
  assert.equal(SCOPE_LABELS.request, 'Этот запрос');
});
