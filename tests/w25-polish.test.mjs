/**
 * w25-polish.test.mjs — волна 25.5: четыре правки по итогам визуальной приёмки.
 *
 * 1. Дубль шапки свода: переключатель версий и пометка «актуально» живут в
 *    липкой панели (shell-svod-bar.js), в карточке их нет; на узком виде версии
 *    уходят в «Ещё ▾».
 * 2. «Файл сохранён вне папки проекта» — одна строка, путь и совет — по
 *    «подробнее».
 * 3. У нижней кромки области прокрутки — затухание 28 px, только при листе.
 * 4. У вкладки «От вас нужно» одно число; реакции руководителя — точка-метка
 *    (было «7» и «23» рядом — читалось как «7 / 23»).
 *
 * Проверки без браузера: чистые функции панели, разметка через mini-dom и
 * CSS-договор текстом. Живой вид — в docs/shots/w25-polish/.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installMiniDom, textOf, findAll, byData, byClass } from './mini-dom.mjs';

installMiniDom();

const bar = await import('../web/js/shell-svod-bar.js');
const parts = await import('../web/js/views/request-parts.js');
const model = await import('../web/js/views/request-parts-model.js');
const reaction = await import('../web/js/views/reaction-notice.js');

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(HERE, '..', p), 'utf8');
const REQ_CSS = read('web/css/view-request.css');
const PARTS_CSS = read('web/css/view-request-parts.css');

const detailWith = (versions, live) => ({
  id: 't1',
  room: 'leto',
  synthesis: {
    state: 'ready',
    version: versions[versions.length - 1],
    text_md: 'Свод',
    history: versions.map((v) => ({ version: v, text_md: 'Свод ' + v })),
    live: live || null,
  },
});

/* ── 1. Одно место управления сводом ────────────────────────────────────── */

test('панель: список версий и текущая версия', () => {
  const d = detailWith([1, 2, 3]);
  assert.deepEqual(bar.barVersions(d), [1, 2, 3]);
  assert.equal(bar.barCurrent(d, 0), 3, 'без выбора — последняя');
  assert.equal(bar.barCurrent(d, 2), 2, 'выбор человека сильнее');
  assert.equal(bar.barCurrent(d, 9), 3, 'номер больше последнего — последняя');
  assert.deepEqual(bar.barVersions({ synthesis: { state: 'none' } }), []);
});

test('панель: пометка «актуально» вместо второй строки в карточке', () => {
  assert.equal(bar.barLiveWord(detailWith([2], { state: 'fresh', text: 'Актуально на 13:48 · версия 2' })), 'актуально');
  assert.equal(bar.barLiveTitle(detailWith([2], { state: 'fresh', text: 'Актуально на 13:48 · версия 2' })),
    'Актуально на 13:48 · версия 2', 'полная подпись — в подсказке');
  assert.equal(bar.barLiveWord(detailWith([2], { state: 'stale', text: 'x' })), 'отстаёт');
  assert.equal(bar.barLiveWord(detailWith([2], { state: 'updating', text: 'x' })), 'обновляется…');
  assert.equal(bar.barLiveWord(detailWith([2], { state: 'waiting_cards', text: 'x' })), 'ждёт команду');
  assert.equal(bar.barLiveWord(detailWith([2], null)), '', 'старый сервер — пометки нет');
});

test('панель: переключатель версий и пункты «Ещё ▾» собираются', () => {
  const host = bar.buildSvodBar({
    getDetail: () => detailWith([1, 2], { state: 'fresh', text: 'Актуально · версия 2' }),
    getVersion: () => 0,
    onVersion: () => {},
    onRefresh: () => {},
  });
  assert.ok(host, 'панель есть, когда свод есть');
  const vers = byClass(host, 'req-bar__vers')[0];
  assert.ok(vers, 'переключатель версий — в панели');
  assert.equal(findAll(vers, '.segmented__btn').length, 2, 'по кнопке на версию');
  const live = byClass(host, 'req-bar__live')[0];
  assert.equal(textOf(live), 'актуально');
  assert.equal(live.getAttribute('title'), 'Актуально · версия 2');
  const items = byClass(host, 'req-bar__vers-item');
  assert.equal(items.length, 2, 'те же версии — в меню «Ещё ▾» (узкий вид)');
  assert.ok(byClass(host, 'req-bar__refresh-item').length === 1, 'обновление свода — тоже в меню');
});

test('панель: свода нет — панели нет', () => {
  assert.equal(bar.buildSvodBar({ getDetail: () => ({ synthesis: { state: 'none' } }) }), null);
});

test('карточка свода: переключателя версий и строки «Актуально» не осталось', () => {
  const src = read('web/js/views/request-answer.js');
  assert.ok(!/versionSwitch\(s, cur\)\)\)/.test(src), 'в шапке карточки нет переключателя');
  assert.match(src, /liveQuiet/, 'строка живого свода показывается только по делу');
  assert.match(src, /return \{ buildAnswer, setVersion, refreshSvod/, 'панель зовёт те же функции');
});

test('CSS: узкий вид — версии в меню, широкий — в панели', () => {
  assert.match(REQ_CSS, /@container view \(max-width: 899px\) \{[^}]*\.req-bar__vers \{ display: none; \}/s);
  assert.match(REQ_CSS, /\.req-bar__vers-item, \.req-bar__refresh-item \{ display: none; \}/);
  assert.match(REQ_CSS, /\.req-bar__live \{/);
});

/* ── 2. «Вне папки проекта» — одна строка ──────────────────────────────── */

const OUTSIDE = {
  path: 'C:\\Users\\User\\AppData\\Local\\Temp\\work\\смета.xlsx',
  hint: 'Команда сохранила результат в другое место. Файл открывается, но лучше держать итоги в папке проекта комнаты.',
};

test('вне папки проекта: свёрнуто — одна строка без пути', () => {
  const host = parts.outsideNotice(OUTSIDE);
  const box = byData(host, 'data-rp', 'outside-box')[0];
  assert.ok(box.hasAttribute('hidden'), 'раскрытие закрыто');
  // Текст вне раскрытия — то, что человек видит в свёрнутой строке.
  const visible = host.childNodes.filter((n) => n !== box).map(textOf).join(' ');
  assert.match(visible, /вне папки проекта/);
  assert.match(visible, /подробнее/);
  assert.ok(!visible.includes('смета.xlsx'), 'путь в свёрнутом виде не показывается');
  assert.ok(!visible.includes('AppData'), 'и каталог тоже');
  const more = byData(host, 'data-rp', 'outside-more')[0];
  assert.equal(more.getAttribute('aria-expanded'), 'false');
});

test('вне папки проекта: «подробнее» открывает путь и совет', () => {
  const host = parts.outsideNotice(OUTSIDE);
  const more = byData(host, 'data-rp', 'outside-more')[0];
  const box = byData(host, 'data-rp', 'outside-box')[0];
  for (const fn of more.listeners.click || []) fn({});
  assert.ok(!box.hasAttribute('hidden'), 'путь и совет видны');
  assert.match(textOf(box), /смета\.xlsx/);
  assert.match(textOf(box), /держать итоги в папке проекта/);
  assert.equal(more.getAttribute('aria-expanded'), 'true');
  assert.equal(textOf(more), 'свернуть');
  for (const fn of more.listeners.click || []) fn({});
  assert.ok(box.hasAttribute('hidden'), 'закрывается обратно');
});

test('предупреждение «вне папки проекта» несёт путь отдельно от текста', () => {
  const items = model.warningItems({
    warnings: [{
      code: 'artifact_outside',
      message: 'файл лежит вне папки проекта: C:\\Temp\\work\\смета.xlsx',
      detail: { path: 'C:\\Temp\\work\\смета.xlsx', state: 'outside' },
    }],
  });
  const w = items.find((x) => x.code === 'artifact_outside');
  assert.ok(w, 'пометка есть');
  assert.equal(w.path, 'C:\\Temp\\work\\смета.xlsx', 'путь — отдельным полем');
  assert.equal(w.text, '', 'в строке нет длинного пути');
});

/* ── 3. Затухание под листом ───────────────────────────────────────────── */

test('CSS: затухание 28 px у нижней кромки, только при листе', () => {
  assert.match(REQ_CSS, /\.app\[data-aside="sheet"\] \.app__main:has\(\.req__aside\)::after \{[^}]*height: 28px/s);
  assert.match(REQ_CSS, /linear-gradient\(to bottom, transparent, var\(--background\) 92%\)/);
  assert.match(REQ_CSS, /bottom: var\(--sheet-h\)/);
  assert.match(REQ_CSS, /\.app\[data-aside="sheet"\]\[data-layout\] \.view \{ padding-bottom: calc\(var\(--sheet-h\) \+ var\(--space-6\)\)/,
    'последняя строка не уезжает под лист');
  assert.ok(!/\.app \.app__main:has/.test(REQ_CSS), 'без листа эффекта нет');
});

/* ── 4. Одно число у вкладки «От вас нужно» ────────────────────────────── */

test('вкладка: реакции руководителя — точка, а не второе число', () => {
  const node = reaction.reactionTabBadge('room-x');
  assert.equal(node, null, 'без новых реакций — ничего');
  // подписи счётчиков остались прежними (их читают скринридер и подсказка)
  const text = reaction.badgeText({ count: 23, requests: ['a', 'b'] });
  assert.equal(text.label, '23', 'число в подписи осталось');
  assert.match(text.title, /Новые реакции руководителя: 23 реакции в 2 запроса/);
});

test('вкладка: в разметке одно число — дела, ждущие вас', () => {
  const app = read('web/js/app.js');
  assert.match(app, /count-pill--you[\s\S]{0,200}String\(waits\.n\)/, 'одно число ждущих дел');
  assert.match(app, /reactionTabBadge\(room\.id\)/);
  const notice = read('web/js/views/reaction-notice.js');
  assert.match(notice, /reaction-badge reaction-badge--dot/);
  assert.ok(!/node\.textContent = text\.label/.test(notice), 'число реакций больше не печатается на вкладке');
});
