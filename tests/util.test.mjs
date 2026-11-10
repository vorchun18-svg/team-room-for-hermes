import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';
import {
  esc, toDate, fmtDate, fmtDateTime, timeAgo, fmtDuration, plural, fromForm, fromCount, plainTerms,
  fmtNumber, fmtMoney, fmtPercent, debounce, clamp, uid, shortId,
  initials, hashColor, byKey, h, s, SVG_NAMESPACE,
} from '../web/js/util.js';
import { countNoun, errorText } from '../web/js/text-ru.js';

/* ------------------------------------------------------------------ escaping */

test('esc escapes the five HTML metacharacters', () => {
  assert.equal(esc('<script>alert(1)</script>'), '&lt;script&gt;alert(1)&lt;/script&gt;');
  assert.equal(esc('a & b'), 'a &amp; b');
  assert.equal(esc('"quoted"'), '&quot;quoted&quot;');
  assert.equal(esc("it's"), 'it&#39;s');
  assert.equal(esc('"><svg onload=alert(1)>'), '&quot;&gt;&lt;svg onload=alert(1)&gt;');
  assert.equal(esc(null), '');
  assert.equal(esc(undefined), '');
  assert.equal(esc(0), '0');
  assert.equal(esc('Кириллица'), 'Кириллица');
});

/* ---------------------------------------------------------------------- dates */

test('fmtDate accepts Date, epoch seconds and ISO strings', () => {
  const local = new Date(2026, 9, 28, 9, 31, 0);
  assert.equal(fmtDate(local), '28 окт 2026');
  assert.equal(fmtDate(Math.floor(local.getTime() / 1000)), '28 окт 2026');
  assert.equal(fmtDate('2026-10-28'), '28 окт 2026');
  assert.equal(fmtDate('2026-10-28T09:31:00'), '28 окт 2026');
  assert.equal(fmtDate('2026-01-05'), '5 янв 2026');
  assert.equal(fmtDate(''), '');
  assert.equal(fmtDate('не дата'), '');
  assert.equal(fmtDate(null), '');
});

test('fmtDateTime pads the clock', () => {
  assert.equal(fmtDateTime(new Date(2026, 9, 28, 9, 31)), '28 окт, 09:31');
  assert.equal(fmtDateTime(new Date(2026, 0, 1, 0, 5)), '1 янв, 00:05');
  assert.equal(fmtDateTime(Math.floor(new Date(2026, 9, 28, 18, 7).getTime() / 1000)), '28 окт, 18:07');
});

test('toDate rejects junk and handles ms vs seconds', () => {
  assert.equal(toDate('abc'), null);
  assert.equal(toDate(undefined), null);
  assert.equal(toDate(0).getTime(), 0);
  const ms = new Date(2026, 9, 28, 9, 31).getTime();
  assert.equal(toDate(ms).getTime(), ms);
  assert.equal(toDate(ms / 1000).getSeconds(), new Date(ms).getSeconds());
});

test('timeAgo speaks Russian', () => {
  const now = new Date(2026, 9, 28, 12, 0, 0);
  assert.equal(timeAgo(new Date(2026, 9, 28, 11, 59, 30), now), 'только что');
  assert.equal(timeAgo(new Date(2026, 9, 28, 12, 0, 30), now), 'только что'); // future clamps
  assert.equal(timeAgo(new Date(2026, 9, 28, 11, 55), now), '5 мин назад');
  assert.equal(timeAgo(new Date(2026, 9, 28, 10, 0), now), '2 ч назад');
  assert.equal(timeAgo(new Date(2026, 9, 27, 11, 0), now), 'вчера');
  assert.equal(timeAgo(new Date(2026, 9, 25, 12, 0), now), '3 дн назад');
  assert.equal(timeAgo(new Date(2026, 9, 15, 12, 0), now), '13 дн назад');
  assert.equal(timeAgo(new Date(2026, 6, 1, 12, 0), now), '1 июл 2026'); // >30 дней
});

test('fmtDuration picks a sensible unit', () => {
  assert.equal(fmtDuration(45), '45 с');
  assert.equal(fmtDuration(0), '0 с');
  assert.equal(fmtDuration(60), '1 мин');
  assert.equal(fmtDuration(12 * 60), '12 мин');
  assert.equal(fmtDuration(4320), '1 ч 12 мин');
  assert.equal(fmtDuration(7200), '2 ч');
  assert.equal(fmtDuration(2 * 86400 + 3 * 3600), '2 дн 3 ч');
  assert.equal(fmtDuration(86400), '1 дн');
  assert.equal(fmtDuration(-5), '—');
  assert.equal(fmtDuration(NaN), '—');
});

/* -------------------------------------------------------------------- numbers */

test('plural follows Russian rules', () => {
  const forms = ['задача', 'задачи', 'задач'];
  assert.equal(plural(1, forms), 'задача');
  assert.equal(plural(2, forms), 'задачи');
  assert.equal(plural(4, forms), 'задачи');
  assert.equal(plural(5, forms), 'задач');
  assert.equal(plural(0, forms), 'задач');
  assert.equal(plural(11, forms), 'задач');
  assert.equal(plural(12, forms), 'задач');
  assert.equal(plural(14, forms), 'задач');
  assert.equal(plural(21, forms), 'задача');
  assert.equal(plural(22, forms), 'задачи');
  assert.equal(plural(101, forms), 'задача');
  assert.equal(plural(111, forms), 'задач');
  assert.equal(plural(-3, forms), 'задачи');
  assert.equal(plural(1, ['день']), 'день'); // короткий список не падает
});

test('fromForm: после «из» число управляет родительным падежом', () => {
  const forms = ['работа', 'работы', 'работ'];
  assert.equal(fromForm(1, forms), 'работы', 'из 1 работы');
  assert.equal(fromForm(2, forms), 'работ', 'из 2 работ');
  assert.equal(fromForm(4, forms), 'работ', 'из 4 работ');
  assert.equal(fromForm(5, forms), 'работ', 'из 5 работ');
  assert.equal(fromForm(0, forms), 'работ', 'из 0 работ');
  assert.equal(fromForm(11, forms), 'работ', 'из 11 работ');
  assert.equal(fromForm(14, forms), 'работ', 'из 14 работ');
  assert.equal(fromForm(21, forms), 'работы', 'из 21 работы');
  assert.equal(fromForm(22, forms), 'работ', 'из 22 работ');
  assert.equal(fromForm(101, forms), 'работы', 'из 101 работы');
  assert.equal(fromForm(111, forms), 'работ', 'из 111 работ');
});

test('fromForm: мужской род, дробные и вырожденные входы', () => {
  const forms = ['запрос', 'запроса', 'запросов'];
  assert.equal(fromForm(1, forms), 'запроса');
  assert.equal(fromForm(3, forms), 'запросов');
  assert.equal(fromForm(1.5, forms), 'запроса', 'дробное — род. ед.');
  assert.equal(fromForm('21', forms), 'запроса', 'число строкой');
  assert.equal(fromForm(NaN, forms), 'запросов');
  assert.equal(fromForm(-1, forms), 'запроса', 'знак игнорируется');
  assert.equal(fromForm(5, ['этап']), 'этап', 'короткий список не падает');
  assert.equal(fromForm(5, undefined), '', 'без форм — пустая строка');
});

test('fromCount: «N из M <сущ>» целиком', () => {
  const forms = ['работа', 'работы', 'работ'];
  assert.equal(fromCount(1, 4, forms), '1 из 4 работ');
  assert.equal(fromCount(3, 4, forms), '3 из 4 работ');
  assert.equal(fromCount(1, 2, forms), '1 из 2 работ');
  assert.equal(fromCount(0, 1, forms), '0 из 1 работы');
  assert.equal(fromCount(5, 21, forms), '5 из 21 работы');
  assert.equal(fromCount(2, 5, forms), '2 из 5 работ');
});

test('plainTerms: технические слова сервера → понятные русские', () => {
  assert.equal(plainTerms('Запрос создан, но диспетчер не запущен — работа не начнётся сама.'),
    'Запрос создан, но движок не запущен — работа не начнётся сама.');
  assert.equal(plainTerms('Диспетчер не запущен'), 'Движок не запущен');
  assert.equal(plainTerms('Состояние диспетчера недоступно'), 'Состояние движка недоступно');
  assert.equal(plainTerms('Карточка уже в очереди — диспетчер подхватит её сам.'), 'Карточка уже в очереди — движок подхватит её сам.');
  assert.equal(plainTerms('нужно написать диспетчеру'), 'нужно написать движку');
  assert.equal(plainTerms('Шлюз Hermes (pid 123) не запущен — диспетчер не берёт карточки в работу.'),
    'Движок команды не запущен — движок не берёт карточки в работу.');
  assert.equal(plainTerms('Движок канбана недоступен'), 'Движок команды недоступен');
  assert.equal(plainTerms('Доска канбана не найдена.'), 'Доска задач не найдена.');
  assert.equal(plainTerms('Ошибка канбана'), 'Ошибка движка');
  assert.equal(plainTerms('Воркер упал 2 раз(а): timeout'), 'Исполнитель не справился 2 раза: timeout');
  assert.equal(plainTerms('Воркер упал дважды подряд'), 'Исполнитель не справился дважды подряд');
  assert.equal(plainTerms('Расчёт упал на пустом поле'), 'Расчёт не справился на пустом поле');
  assert.equal(plainTerms('нет ответа от воркера'), 'нет ответа от исполнителя');
  assert.equal(plainTerms('Всё в порядке'), 'Всё в порядке');
  assert.equal(plainTerms(null), '');
  assert.equal(plainTerms(undefined), '');
  assert.equal(plainTerms(42), '42');
});

test('countNoun: число с существительным в именительном падеже', () => {
  const forms = ['шаг', 'шага', 'шагов'];
  assert.equal(countNoun(1, forms), '1 шаг');
  assert.equal(countNoun(2, forms), '2 шага');
  assert.equal(countNoun(5, forms), '5 шагов');
  assert.equal(countNoun(11, forms), '11 шагов');
  assert.equal(countNoun(21, forms), '21 шаг');
  assert.equal(countNoun(0, forms), '0 шагов');
  assert.equal(countNoun(NaN, forms), '');
  assert.equal(countNoun('3', ['вопрос', 'вопроса', 'вопросов']), '3 вопроса');
});

test('errorText: коды API → человеческие фразы, код наружу не идёт', () => {
  assert.deepEqual(errorText({ code: 'not_found', message: 'Not Found' }),
    { message: 'Запрос не найден — возможно, его убрали в архив.', code: '' });
  assert.deepEqual(errorText({ code: 'bad_request', message: 'members' }),
    { message: 'Запрос составлен неверно — обновите страницу и попробуйте снова.', code: '' });
  assert.deepEqual(errorText({ code: 'engine_error', message: 'boom' }),
    { message: 'Движок команды не смог выполнить действие. Попробуйте ещё раз.', code: '' });
  assert.deepEqual(errorText({ code: 'http_500', message: 'x' }),
    { message: 'Движок команды не смог выполнить действие. Попробуйте ещё раз.', code: '' });
  assert.deepEqual(errorText({ code: 'network', message: 'Нет связи с приложением' }),
    { message: 'Нет связи с приложением', code: 'network' });
  assert.equal(errorText({ code: 'worker_crashed', message: 'Воркер упал' }).message,
    'Исполнитель не справился');
  assert.equal(errorText(null).message, 'Не удалось выполнить действие. Попробуйте ещё раз.');
});

test('fmtNumber formats with spaces and commas', () => {
  assert.equal(fmtNumber(0), '0');
  assert.equal(fmtNumber(100), '100');
  assert.equal(fmtNumber(1234), '1 234');
  assert.equal(fmtNumber(1234567), '1 234 567');
  assert.equal(fmtNumber(1234.5), '1 234,5');
  assert.equal(fmtNumber(1234.5678), '1 234,568');
  assert.equal(fmtNumber(0.5), '0,5');
  assert.equal(fmtNumber(-1234.5), '-1 234,5');
  assert.equal(fmtNumber(1234.5, { decimals: 2 }), '1 234,50');
  assert.equal(fmtNumber(0, { decimals: 2 }), '0,00');
  assert.equal(fmtNumber(-0.001, { decimals: 2 }), '0,00');
  assert.equal(fmtNumber(99.999, { decimals: 1 }), '100,0');
  assert.equal(fmtNumber(NaN), '—');
  assert.equal(fmtNumber('1234,5'), '1 234,5'); // строка с запятой
});

test('fmtMoney handles currency placement', () => {
  assert.equal(fmtMoney(1234.5), 'R$ 1 234,50');
  assert.equal(fmtMoney(1234.5, { currency: 'RUB' }), '1 234,50 ₽');
  assert.equal(fmtMoney(1234.5, { currency: 'USD', decimals: 0 }), '$ 1 235');
  assert.equal(fmtMoney(1234.5, { currency: 'EUR' }), '1 234,50 €');
  assert.equal(fmtMoney(-1234.5), '-R$ 1 234,50');
  assert.equal(fmtMoney(0, { currency: 'BRL', decimals: 0 }), 'R$ 0');
  assert.equal(fmtMoney(NaN), '—');
});

test('fmtPercent multiplies ratios', () => {
  assert.equal(fmtPercent(12), '12%');
  assert.equal(fmtPercent(0.123, { ratio: true }), '12%');
  assert.equal(fmtPercent(0.1234, { ratio: true, decimals: 1 }), '12,3%');
  assert.equal(fmtPercent(-5.5, { decimals: 1 }), '-5,5%');
  assert.equal(fmtPercent(NaN), '—');
});

/* -------------------------------------------------------------------- helpers */

test('clamp bounds values', () => {
  assert.equal(clamp(5, 1, 10), 5);
  assert.equal(clamp(-1, 1, 10), 1);
  assert.equal(clamp(99, 1, 10), 10);
  assert.equal(clamp(NaN, 2, 10), 2);
});

test('uid is unique and selector safe', () => {
  const ids = new Set();
  for (let i = 0; i < 500; i++) ids.add(uid('md'));
  assert.equal(ids.size, 500);
  for (const id of ids) assert.match(id, /^md-[a-z0-9]+$/);
  assert.match(uid(), /^id-/);
});

test('shortId trims long kanban ids', () => {
  assert.equal(shortId('t_605613e0'), 't_605613e0');
  assert.equal(shortId('t_605613e0aaaa'), 't_605613e0');
  assert.equal(shortId('abcdefghijklmnop'), 'abcdefgh…');
  assert.equal(shortId(''), '');
  assert.equal(shortId(null), '');
});

test('initials build avatar labels', () => {
  assert.equal(initials('Михаил Петров'), 'МП');
  assert.equal(initials('lawyer'), 'LA');
  assert.equal(initials('biz-director'), 'BD');
  assert.equal(initials('  '), '?');
  assert.equal(initials('Василий'), 'ВА');
});

test('hashColor is deterministic and well formed', () => {
  const a = hashColor('coder');
  assert.equal(a, hashColor('coder'));
  assert.match(a, /^hsl\(\d{1,3} 58% 46%\)$/);
  assert.notEqual(hashColor('coder'), hashColor('lawyer'));
});

test('byKey groups without polluting prototypes', () => {
  const items = [
    { id: 1, status: 'done' },
    { id: 2, status: 'todo' },
    { id: 3, status: 'done' },
    { id: 4 },
  ];
  const g = byKey(items, 'status');
  assert.deepEqual(Object.keys(g).sort(), ['', 'done', 'todo']);
  assert.equal(g.done.length, 2);
  assert.equal(g.todo[0].id, 2);
  assert.equal(g[''][0].id, 4);

  const byFn = byKey(items, (it) => (it.id % 2 ? 'odd' : 'even'));
  assert.equal(byFn.odd.length, 2);

  const evil = byKey([{ k: '__proto__' }, { k: 'ok' }], 'k');
  assert.equal(Object.getPrototypeOf(evil), Object.prototype);
  assert.equal(Object.keys(evil).length, 2);
  assert.equal(byKey(null, 'x') && Object.keys(byKey(null, 'x')).length, 0);
});

test('debounce coalesces, cancels and flushes', async () => {
  const calls = [];
  const fn = debounce((v) => calls.push(v), 20);
  fn(1); fn(2); fn(3);
  assert.deepEqual(calls, []);
  await new Promise((r) => setTimeout(r, 60));
  assert.deepEqual(calls, [3]);

  fn(4);
  fn.cancel();
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(calls, [3]);

  fn(5);
  fn.flush();
  assert.deepEqual(calls, [3, 5]);
  fn(6);
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(calls, [3, 5, 6]);
});

/* ------------------------------------------------------------------------ DOM */

test('h builds elements, sets attributes and never uses innerHTML', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  const el = h('div', { class: 'card big', id: 'root', title: 'Подсказка' }, 'Привет ', 'мир');
  assert.equal(el.tagName, 'div');
  assert.equal(el.getAttribute('class'), 'card big');
  assert.equal(el.getAttribute('id'), 'root');
  assert.equal(el.textContent, 'Привет мир');
  assert.equal(el.childNodes.length, 2);
  assert.equal(el.childNodes[0].nodeType, 3, 'текст должен быть текстовым узлом');
  // геттер/сеттер innerHTML в fake-DOM бросает — значит builder его не трогал
  assert.throws(() => el.innerHTML, /innerHTML must never be used/);
});

test('h treats markup in children as text', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  const el = h('div', null, '<script>alert(1)</script>', '<img src=x onerror=alert(1)>');
  assert.equal(el.childNodes.length, 2);
  assert.equal(el.childNodes[0].textContent, '<script>alert(1)</script>');
  assert.equal(el.textContent, '<script>alert(1)</script><img src=x onerror=alert(1)>');
});

test('h normalizes class, style and dataset', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  const el = h('span', {
    className: ['a', 'b'],
    dataset: { roomId: 'cafe', idx: 3 },
    style: { color: 'red', 'font-size': '12px', '--accent': '#f00' },
  });
  assert.equal(el.getAttribute('class'), 'a b');
  assert.equal(el.getAttribute('data-room-id'), 'cafe');
  assert.equal(el.getAttribute('data-idx'), '3');
  assert.equal(el.dataset.roomId, 'cafe');
  assert.equal(el.style.color, 'red');
  assert.equal(el.style.fontSize, '12px');
  assert.equal(el.style['--accent'], '#f00');

  const objClass = h('i', { class: { on: true, off: false } });
  assert.equal(objClass.getAttribute('class'), 'on');

  const strStyle = h('i', { style: 'color: blue' });
  assert.equal(strStyle.getAttribute('style'), 'color: blue');
});

test('h skips false/null and keeps 0 and empty string', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  const el = h('input', {
    type: 'checkbox',
    disabled: true,
    checked: false,
    value: null,
    'aria-label': 'выбор',
    tabindex: 0,
  });
  assert.equal(el.getAttribute('disabled'), '');
  assert.equal(el.hasAttribute('checked'), false);
  assert.equal(el.hasAttribute('value'), false);
  assert.equal(el.getAttribute('aria-label'), 'выбор');
  assert.equal(el.getAttribute('tabindex'), '0');

  const kids = h('div', null, h('span', null, 'a'), null, false, undefined, true, 0, '');
  assert.equal(kids.childNodes.length, 2); // span + "0"; пустая строка пропускается
});

test('h flattens arrays and preserves order', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  const bold = h('b', null, 'жирный');
  const el = h('p', null, ['раз ', ['два ', bold]], ' три');
  assert.equal(el.textContent, 'раз два жирный три');
  assert.equal(el.childNodes.length, 4);
  assert.equal(el.childNodes[2].tagName, 'b');
});

test('h wires on* handlers through addEventListener', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  let clicked = 0;
  const el = h('button', { onclick: () => { clicked++; }, type: 'button' }, 'Ок');
  assert.equal(typeof el.listeners.click[0], 'function');
  el.dispatch('click');
  assert.equal(clicked, 1);
  assert.equal(el.getAttribute('type'), 'button');

  // не-функция в on* игнорируется, а не превращается в атрибут
  const bad = h('button', { onclick: 'alert(1)' });
  assert.equal(bad.hasAttribute('onclick'), false);
});

test('h accepts attrs-less and child-first signatures', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  assert.equal(h('div').childNodes.length, 0);
  assert.equal(h('div', 'текст').textContent, 'текст');
  assert.equal(h('div', null, 'текст').textContent, 'текст');
  assert.equal(h('div', undefined, 'a', 'b').textContent, 'ab');
  const node = h('span', null, 'x');
  assert.equal(h('p', null, node).childNodes[0], node);
});

test('svg helpers use the SVG namespace', (t) => {
  installFakeDom();
  t.after(uninstallFakeDom);

  const svg = h('svg', { viewBox: '0 0 10 10', width: 10 }, s('circle', { cx: 5, cy: 5, r: 4 }));
  assert.equal(svg.namespaceURI, SVG_NAMESPACE);
  assert.equal(svg.tagName, 'svg');
  assert.equal(svg.getAttribute('viewBox'), '0 0 10 10');
  const circle = svg.childNodes[0];
  assert.equal(circle.namespaceURI, SVG_NAMESPACE);
  assert.equal(circle.tagName, 'circle');
  assert.equal(circle.getAttribute('r'), '4');

  const g = s('g', { class: 'layer' }, s('path', { d: 'M0 0L10 10' }));
  assert.equal(g.tagName, 'g');
  assert.equal(g.getAttribute('class'), 'layer');
  assert.equal(g.childNodes[0].getAttribute('d'), 'M0 0L10 10');
});

test('h throws a clear error without a document', (t) => {
  uninstallFakeDom();
  assert.throws(() => h('div'), /no document/);
});
