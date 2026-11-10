// node --test tests/report-currency.test.mjs
// Хвост волны 7: в веб-рендере отчётов «R$ 42,00» превращался в «$ 42,00»
// (в тесте на валюту ловился только «$»), а таблица по умолчанию считала в рублях
// и ставила знак после числа.
import test from 'node:test';
import assert from 'node:assert/strict';

import { makeFormatter, currencyOf } from '../web/js/report/charts-primitives.js';
import { formatMoney, formatValue, formatTotal } from '../web/js/report/table-model.js';

// Intl в ru-RU разделяет тысячи неразрывным пробелом — приводим к обычному.
const plain = (s) => String(s).replace(/[\u00A0\u202F]/g, ' ');

test('currencyOf: реалы не путаются с долларом', () => {
  assert.equal(currencyOf('R$'), 'BRL');
  assert.equal(currencyOf('BRL'), 'BRL');
  assert.equal(currencyOf('R$ / шт'), 'BRL');
  assert.equal(currencyOf('$'), 'USD');
  assert.equal(currencyOf('USD'), 'USD');
  assert.equal(currencyOf('€'), 'EUR');
  assert.equal(currencyOf('₽'), 'RUB');
  assert.equal(currencyOf('руб.'), 'RUB');
  assert.equal(currencyOf(''), 'BRL');   // валюта проекта по умолчанию
  assert.equal(currencyOf('%'), 'BRL');  // не деньги — решает unitKind
});

test('график с unit «R$» рисует R$, а не $', () => {
  const fmt = makeFormatter('R$');
  const out = plain(fmt(42));
  assert.equal(out, 'R$ 42,00');
  assert.ok(!/(^|\s)\$/.test(out), 'одиночного «$» быть не должно');
  assert.equal(plain(makeFormatter('R$')(1234.5)), 'R$ 1 234,50');
});

test('ось в долларах и рублях остаётся прежней', () => {
  assert.equal(plain(makeFormatter('$')(42)), '$ 42,00');
  assert.equal(plain(makeFormatter('₽')(42)), '42,00 ₽');
  assert.equal(plain(makeFormatter('%')(42)), '42,0%');
});

test('таблица: R$ перед числом, по умолчанию реалы', () => {
  assert.equal(plain(formatMoney(42)), 'R$ 42,00');
  assert.equal(plain(formatMoney(42, 'BRL')), 'R$ 42,00');
  assert.equal(plain(formatMoney(42, 'R$')), 'R$ 42,00');
  assert.equal(plain(formatMoney(42, 'RUB')), '42,00 ₽');
  assert.equal(plain(formatMoney(42, 'EUR')), '42,00 €');
  assert.equal(plain(formatMoney(42, 'BRL', 0)), 'R$ 42');
});

test('ячейка и итог таблицы не превращают R$ в $', () => {
  const col = { key: 'price', label: 'Цена', type: 'money', currency: 'R$' };
  assert.equal(plain(formatValue(42, col)), 'R$ 42,00');
  assert.equal(plain(formatTotal({ value: 42, kind: 'sum' }, col)), 'R$ 42,00');
  const rows = [{ price: 42 }, { price: 22.15 }];
  const total = rows.reduce((acc, r) => acc + r.price, 0);
  assert.equal(plain(formatTotal({ value: total, kind: 'sum' }, col)), 'R$ 64,15');
});
