// Тесты чистой логики таблиц (без DOM): node --test tests/report-table.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  parseNumber, parseDate, formatNumber, formatMoney, formatPercent, formatDate,
  formatValue, statusTone, sortRows, filterRows, computeTotals, formatTotal,
  pageRows, toCSV,
} from '../web/js/report/table-model.js';

const COLS = [
  { key: 'name', label: 'Название' },
  { key: 'rev', label: 'Выручка', type: 'money', currency: 'BRL' },
  { key: 'qty', label: 'Заказы', type: 'number' },
  { key: 'margin', label: 'Маржа', type: 'percent', ratio: true },
  { key: 'date', label: 'Дата', type: 'date' },
  { key: 'status', label: 'Статус', type: 'status' },
];

// Intl в ru-RU разделяет тысячи неразрывным пробелом — для сравнений приводим к обычному.
const plain = (s) => String(s).replace(/[\u00A0\u202F]/g, ' ');

const ROWS = [
  { name: 'Instagram', rev: 41250, qty: 184, margin: 0.286, date: '2026-07-01', status: 'готово' },
  { name: 'Marketplace', rev: 18900, qty: 96, margin: 0.214, date: '2026-07-05', status: 'в работе' },
  { name: 'Pop-up', rev: null, qty: 88, margin: 0.352, date: '2026-12-01', status: 'блокировано' },
  { name: 'Корпоративы', rev: 39600, qty: 42, margin: null, date: '2026-12-10', status: 'готово' },
  { name: 'Опт', rev: 1200.5, qty: null, margin: 0.4, date: '', status: 'ждёт данных' },
];

/* ---------- разбор значений ---------- */

test('parseNumber: пробелы, запятые, валюта, проценты', () => {
  assert.equal(parseNumber('1 234,56'), 1234.56);
  assert.equal(parseNumber('R$ 1.234,56'), 1234.56);
  assert.equal(parseNumber('12,5%'), 12.5);
  assert.equal(parseNumber('1,234.56'), 1234.56);
  assert.equal(parseNumber(1234.5), 1234.5);
  assert.equal(parseNumber('1.234'), 1234);
  assert.equal(parseNumber('—'), null);
  assert.equal(parseNumber(null), null);
  assert.equal(parseNumber(''), null);
});

test('parseDate: ISO, русский формат, мусор', () => {
  assert.equal(parseDate('2026-10-28').getDate(), 28);
  assert.equal(parseDate('2026-10-28').getMonth(), 9);
  assert.equal(formatDate('2026-10-28'), '28.10.2026');
  assert.equal(formatDate('28.10.2026'), '28.10.2026');
  assert.equal(formatDate('2026-10-28T14:05:00'), '28.10.2026 14:05');
  assert.equal(formatDate('не дата'), 'не дата');
  assert.equal(parseDate(''), null);
});

/* ---------- форматирование ---------- */

test('formatValue: money / percent / number / date / status / link', () => {
  assert.equal(plain(formatValue(1234.5, { type: 'money', currency: 'BRL' })), 'R$ 1 234,50');
  assert.equal(plain(formatValue(1200, { type: 'money', currency: 'RUB', decimals: 0 })), '1 200 ₽');
  assert.equal(formatValue(0.286, { type: 'percent', ratio: true }), '28,6%');
  assert.equal(formatValue(12.5, { type: 'percent' }), '12,5%');
  assert.equal(plain(formatValue(1234.567, { type: 'number', decimals: 2 })), '1 234,57');
  assert.equal(formatValue('2026-11-05', { type: 'date' }), '05.11.2026');
  assert.equal(formatValue('в работе', { type: 'status' }), 'в работе');
  assert.equal(formatValue({ title: 'Смета', href: 'a.md' }, { type: 'link' }), 'Смета');
  assert.equal(formatValue(null, { type: 'text' }), '');
  assert.equal(formatNumber(0, 0), '0');
});

test('statusTone: русские и английские статусы', () => {
  assert.equal(statusTone('готово'), 'ok');
  assert.equal(statusTone('blocked'), 'danger');
  assert.equal(statusTone('ждёт данных'), 'warn');
  assert.equal(statusTone('в работе'), 'info');
  assert.equal(statusTone('прогноз'), 'neutral');
});

/* ---------- сортировка ---------- */

test('sortRows: числа по возрастанию/убыванию, пустые в конце', () => {
  const asc = sortRows(ROWS, COLS, { key: 'rev', dir: 'asc' }).map((r) => r.rev);
  assert.deepEqual(asc, [1200.5, 18900, 39600, 41250, null]);
  const desc = sortRows(ROWS, COLS, { key: 'rev', dir: 'desc' }).map((r) => r.rev);
  assert.deepEqual(desc, [41250, 39600, 18900, 1200.5, null]);
});

test('sortRows: даты и статусы, стабильность', () => {
  const byDate = sortRows(ROWS, COLS, { key: 'date', dir: 'asc' }).map((r) => r.name);
  assert.deepEqual(byDate, ['Instagram', 'Marketplace', 'Pop-up', 'Корпоративы', 'Опт']); // пустая дата — в конце
  const byDateDesc = sortRows(ROWS, COLS, { key: 'date', dir: 'desc' }).map((r) => r.name);
  assert.deepEqual(byDateDesc, ['Корпоративы', 'Pop-up', 'Marketplace', 'Instagram', 'Опт']);
  const stable = sortRows([{ k: 1, v: 2 }, { k: 1, v: 1 }, { k: 0, v: 9 }], [{ key: 'k', type: 'number' }], { key: 'k', dir: 'asc' });
  assert.deepEqual(stable.map((r) => r.v), [9, 2, 1]);
});

test('sortRows: строки с числами внутри (natural sort)', () => {
  const rows = [{ t: 'Шаг 10' }, { t: 'Шаг 2' }, { t: 'Шаг 1' }];
  const out = sortRows(rows, [{ key: 't', type: 'text' }], { key: 't', dir: 'asc' }).map((r) => r.t);
  assert.deepEqual(out, ['Шаг 1', 'Шаг 2', 'Шаг 10']);
});

test('sortRows не мутирует вход и терпит мусор', () => {
  const input = ROWS.slice();
  sortRows(input, COLS, { key: 'qty', dir: 'desc' });
  assert.deepEqual(input, ROWS);
  assert.deepEqual(sortRows(null, COLS, { key: 'qty' }), []);
  assert.deepEqual(sortRows(ROWS, COLS, {}).length, ROWS.length);
});

/* ---------- фильтр ---------- */

test('filterRows: по тексту, статусу и отформатированным числам', () => {
  assert.equal(filterRows(ROWS, COLS, 'insta').length, 1);
  assert.equal(filterRows(ROWS, COLS, 'ГОТОВО').length, 2);
  assert.equal(filterRows(ROWS, COLS, 'блок').length, 1);
  assert.equal(filterRows(ROWS, COLS, '1 200').length, 1);   // по формату money
  assert.equal(filterRows(ROWS, COLS, '1200').length, 1);    // по «сжатому» виду
  assert.equal(filterRows(ROWS, COLS, 'готово 41250').length, 1); // все слова (И)
  assert.equal(filterRows(ROWS, COLS, '').length, ROWS.length);
  assert.equal(filterRows(ROWS, COLS, 'нет такого').length, 0);
  assert.equal(filterRows(ROWS, COLS, 'pop 2026').length, 1);
});

/* ---------- итоги ---------- */

test('computeTotals: sum / avg / count и пропуски', () => {
  const t = computeTotals(ROWS, COLS, { rev: 'sum', qty: 'count', margin: 'avg' });
  assert.equal(t.rev.value, 100950.5);
  assert.equal(t.qty.value, 4);       // null не считается
  assert.ok(Math.abs(t.margin.value - (0.286 + 0.214 + 0.352 + 0.4) / 4) < 1e-9);
  assert.deepEqual(computeTotals(ROWS, COLS, null), {});
  assert.deepEqual(computeTotals(ROWS, COLS, { nope: 'sum' }), {});
});

test('formatTotal: подписи по типу колонки', () => {
  const t = computeTotals(ROWS, COLS, { rev: 'sum', qty: 'count', margin: 'avg' });
  assert.equal(plain(formatTotal(t.rev, COLS[1])), 'R$ 100 950,50');
  assert.equal(formatTotal(t.qty, COLS[2]), '4');
  assert.equal(formatTotal(t.margin, COLS[3]), '31,3%');
});

/* ---------- пагинация ---------- */

test('pageRows: порционная выдача', () => {
  const many = Array.from({ length: 450 }, (_, i) => ({ i }));
  const first = pageRows(many, 200);
  assert.equal(first.rows.length, 200);
  assert.equal(first.total, 450);
  assert.equal(first.hasMore, true);
  const all = pageRows([{ i: 1 }], 200);
  assert.equal(all.hasMore, false);
  assert.equal(pageRows(null).total, 0);
});

test('pageRows на 5000 строк не деградирует по счёту', () => {
  const big = Array.from({ length: 5000 }, (_, i) => ({ i, v: i % 7 }));
  const sorted = sortRows(big, [{ key: 'v', type: 'number' }], { key: 'v', dir: 'desc' });
  assert.equal(sorted.length, 5000);
  assert.equal(sorted[0].v, 6);
});

/* ---------- CSV ---------- */

test('toCSV: заголовок, BOM, разделитель, экранирование', () => {
  const csv = toCSV(COLS, [{ name: 'A "quoted"', rev: 1234.5, qty: 1, margin: 0.5, date: '2026-10-28', status: 'готово;\nда' }]);
  assert.ok(csv.startsWith('\uFEFFНазвание;Выручка;Заказы;Маржа;Дата;Статус'));
  assert.ok(csv.includes('A "quoted"') === false);            // кавычки удвоены внутри поля
  assert.ok(csv.includes('"A ""quoted"""'));
  assert.ok(plain(csv).includes('R$ 1 234,50'));
  assert.ok(csv.includes('50,0%'));
  assert.ok(csv.includes('28.10.2026'));
  assert.ok(csv.includes('"готово;\nда"'));
  assert.equal(toCSV([{ key: 'a', label: 'A' }], [{ a: 1 }], { bom: false }), 'A\r\n1');
});