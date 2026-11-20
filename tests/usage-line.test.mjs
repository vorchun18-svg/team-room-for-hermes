/**
 * usage-line.test.mjs — W28-E: строка расхода запроса в правой колонке.
 *
 * Что проверяем:
 *  * строка читается как «Расход: N выз. / M тыс. токенов (база / тело / инструменты)»;
 *  * серверная подпись (`line`) показывается как есть — интерфейс не пересчитывает;
 *  * нет данных — честная строка, а не пустой блок и не «0 токенов»;
 *  * ошибка сети — строка «не удалось получить» и кнопка «Обновить» (без тупиков);
 *  * попытка «не на той модели» видна словами, а не только цифрой;
 *  * колонка запроса действительно зовёт `mountUsage` (одна точка встраивания).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installMiniDom, resetMiniDom, DOC } from './mini-dom.mjs';

installMiniDom();

const { usageText, usageNote, thousands, mountUsage } = await import('../web/js/views/usage-line.js');

const HERE = dirname(fileURLToPath(import.meta.url));
const aside = readFileSync(join(HERE, '..', 'web', 'js', 'views', 'request-aside.js'), 'utf8');

const PAYLOAD = {
  rid: 't_demo0001',
  calls: 23,
  tokens: { input: 640, cache_read: 412000, cache_write: 96000, output: 18400 },
  cost_usd: 0.4127,
  split: { base: 124000, body: 356000, tools: 46640 },
  models: [{ model: 'модель комнаты', calls: 19, cost_usd: 0.3712 }],
  mismatched: { attempts: 1, cards: ['t_demo0003'] },
  source: 'state.db',
  line: 'Расход: 23 выз. / 527 тыс. токенов (база 124 / тело 356 / инструменты 47)',
};


test('строка расхода — серверная подпись как есть', () => {
  assert.equal(usageText(PAYLOAD), PAYLOAD.line);
  assert.ok(usageText(PAYLOAD).startsWith('Расход: '));
  assert.ok(usageText(PAYLOAD).includes('тыс. токенов'));
  for (const part of ['база', 'тело', 'инструменты']) {
    assert.ok(usageText(PAYLOAD).includes(part), 'в строке есть разбивка: ' + part);
  }
});

test('без серверной строки собираем ту же по смыслу', () => {
  const text = usageText({ ...PAYLOAD, line: '' });
  assert.ok(text.startsWith('Расход: 23 выз.'), text);
  assert.ok(text.includes('база'), text);
  assert.ok(text.includes('(база'), 'разбивка в скобках');
});

test('нет данных — честно, без нулей', () => {
  assert.match(usageText({ source: 'none', line: '' }), /нет данных/);
  assert.match(usageText({}), /нет данных/);
});

test('тысячи токенов — коротко и по-русски', () => {
  assert.equal(thousands(12400), '12');
  assert.equal(thousands(640), '0,6');
  assert.equal(thousands(0), '0');
});

test('расшифровка: стоимость, демо-данные и «не та модель»', () => {
  const info = usageNote(PAYLOAD);
  assert.ok(info.note.includes('$0.41'), info.note);
  assert.match(info.warning, /не на той модели: 1/);
  assert.equal(info.models.length, 1);
  assert.ok(info.models[0].cost > 0);
  const demo = usageNote({ source: 'fixtures', cost_usd: 0 });
  assert.ok(demo.note.includes('демо-данные'));
  assert.equal(demo.warning, '', 'без попыток предупреждения нет');
});

test('mountUsage рисует строку и просит API', async () => {
  resetMiniDom();
  const parent = DOC.createElement('div');
  const asked = [];
  const api = { get: (url) => { asked.push(url); return Promise.resolve(PAYLOAD); } };
  const box = mountUsage(parent, 't_demo0001', api);
  assert.ok(box, 'узел создан');
  assert.equal(asked[0], '/api/requests/t_demo0001/usage');
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(box.textContent.includes('Расход: 23'), box.textContent);
  assert.equal(mountUsage(parent, 't_demo0001', api).textContent, box.textContent,
    'повторный вызов не дублирует строку');
});

test('ошибка сети — причина и кнопка «Обновить», без падения колонки', async () => {
  resetMiniDom();
  const parent = DOC.createElement('div');
  const api = { get: () => Promise.reject(new Error('сеть')) };
  const box = mountUsage(parent, 't_demo0002', api);
  await new Promise((r) => setTimeout(r, 0));
  assert.match(box.textContent, /не удалось получить/);
  assert.match(box.textContent, /Обновить/);
});

test('колонка запроса встраивает строку одной точкой', () => {
  assert.ok(aside.includes("import('./usage-line.js')"), 'ленивая загрузка строки расхода');
  assert.ok(aside.includes('mountUsage(wrap, rid'), 'ставится в колонку запроса');
  assert.equal((aside.match(/mountUsage\(/g) || []).length, 1, 'одна точка встраивания');
});
