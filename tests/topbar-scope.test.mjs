/**
 * tests/topbar-scope.test.mjs — шапка: «Этап: …», переключатель «Вся комната /
 * Этот запрос», сгруппированные значки (W20-U1, пункт 12, аудит P8).
 *
 * Проверяем правила, а не картинку: что считается охватом на странице запроса,
 * на вкладке с фильтром и без него; где переключателя быть не должно; что в
 * группе инструментов ровно по одному поиску, палитре и настройкам, и что у
 * каждого значка есть подсказка (аудит: «5 кнопок разного стиля — назначение
 * неясно»).
 *
 * Тесты КРАСНЫЕ на коде до W20-U1: там не было ни phaseLabel, ни охвата.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';
import {
  FILTER_TABS, phaseLabel, scopeModel, scopeSwitcher, toolsGroup, tabName,
} from '../web/js/topbar-scope.js';
import { SCOPE_LABELS } from '../web/js/text-ru.js';

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(join(here, '..', 'web', 'js', 'app.js'), 'utf8');

/* ─────────────────────────────────────────────── помощники для DOM ──────── */

function walk(node, out = []) {
  if (!node) return out;
  if (node.nodeType === 1) out.push(node);
  for (const child of node.childNodes || []) walk(child, out);
  return out;
}

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

const byClass = (root, cls) => walk(root).filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));

/* ───────────────────────────────────────────── этап словами (P8) ────────── */

test('этап в шапке подписан: «Этап: …», а не голое название', () => {
  assert.equal(phaseLabel('Идея и стратегия'), 'Этап: Идея и стратегия');
  assert.equal(phaseLabel('  Упаковка  '), 'Этап: Упаковка', 'пробелы не мешают');
  assert.equal(phaseLabel(''), '', 'этапа нет — чипа нет');
  assert.equal(phaseLabel(null), '');
  assert.equal(phaseLabel(undefined), '');
});

test('app.js рисует этап через phaseLabel (одно место)', () => {
  assert.match(appSrc, /phaseLabel\(/);
  assert.match(appSrc, /Этап проек?та?/, 'подсказка объясняет, что это этап проекта');
  assert.ok(!/class: 'chip chip--outline topbar__phase',\s*\n\s*title: [^\n]*\n\s*\}, phase\)/.test(appSrc),
    'голое название этапа больше не подставляется');
});

/* ─────────────────────────────── переключатель «Вся комната / Этот запрос» ─ */

test('страница запроса: активно «Этот запрос», «Вся комната» ведёт в комнату', () => {
  const m = scopeModel({ roomId: 'demo', name: 'request', id: 't_a1b2c3d4', requestId: 't_a1b2c3d4' });
  assert.ok(m, 'переключатель есть');
  assert.equal(m.active, 'request');
  assert.equal(m.request.text, SCOPE_LABELS.request);
  assert.equal(m.whole.text, SCOPE_LABELS.whole);
  assert.equal(m.request.active, true);
  assert.equal(m.whole.active, false);
  assert.equal(m.whole.href, '#/room/demo/composer');
  assert.equal(m.request.href, '#/room/demo/request/t_a1b2c3d4');
});

test('вкладка с фильтром: «Этот запрос», снятие фильтра — «Вся комната»', () => {
  const m = scopeModel({ roomId: 'demo', tab: 'backlog', requestId: 't_a1b2c3d4' });
  assert.equal(m.active, 'request');
  assert.equal(m.request.href, '#/room/demo/backlog?request=t_a1b2c3d4');
  assert.equal(m.whole.href, '#/room/demo/backlog', 'у «Вся комната» фильтра нет');
});

test('вкладка без фильтра: активна «Вся комната», «Этот запрос» не предлагается', () => {
  const m = scopeModel({ roomId: 'demo', tab: 'backlog' });
  assert.equal(m.active, 'whole');
  assert.equal(m.whole.active, true);
  assert.equal(m.request, null, 'переключать некуда — второй сегмент не показываем');
});

test('там, где охват не выбирается, переключателя нет', () => {
  assert.equal(scopeModel({ roomId: 'demo', tab: 'team' }), null);
  assert.equal(scopeModel({ roomId: 'demo', tab: 'roadmap' }), null);
  assert.equal(scopeModel({ roomId: 'demo', tab: 'composer' }), null);
  assert.equal(scopeModel({}), null, 'без комнаты переключать нечего');
  assert.deepEqual(FILTER_TABS, ['backlog', 'reports', 'journal']);
});

test('переключатель: два сегмента, текущий помечен aria-current', () => {
  const dom = installFakeDom();
  try {
    const nav = scopeSwitcher(scopeModel({ roomId: 'demo', tab: 'journal', requestId: 't_a1b2c3d4' }));
    assert.ok(nav, 'переключатель построен');
    assert.ok((nav.getAttribute('class') || '').includes('scope-switch'));
    assert.equal(nav.getAttribute('aria-label'), 'Что показывает экран');
    const segs = byClass(nav, 'scope-switch__seg');
    assert.equal(segs.length, 2);
    assert.deepEqual(segs.map(text), [SCOPE_LABELS.whole, SCOPE_LABELS.request]);
    assert.equal(segs[0].getAttribute('aria-current'), null);
    assert.equal(segs[1].getAttribute('aria-current'), 'page');
    assert.ok((segs[0].getAttribute('title') || '').length > 10, 'у сегмента есть подсказка');
    assert.equal(scopeSwitcher(null), null);

    const single = scopeSwitcher(scopeModel({ roomId: 'demo', tab: 'backlog' }));
    assert.equal(byClass(single, 'scope-switch__seg').length, 1, 'без фильтра — только состояние');
  } finally {
    uninstallFakeDom();
  }
});

/* ─────────────────────────────────── значки: одна группа, без дублей ───── */

test('поиск, палитра и настройки — одна группа, по одному разу', () => {
  const dom = installFakeDom();
  try {
    const group = toolsGroup({ navigate: () => {}, openPalette: () => {} });
    assert.ok((group.getAttribute('class') || '').includes('topbar__group--tools'));
    assert.equal(group.getAttribute('aria-label'), 'Инструменты');
    const tools = walk(group).filter((n) => n.getAttribute && n.getAttribute('data-tool'));
    assert.deepEqual(tools.map((n) => n.getAttribute('data-tool')), ['search', 'palette', 'settings'],
      'ровно три значка, каждый один раз');
    const titles = tools.map((n) => n.getAttribute('title'));
    for (const t of titles) assert.ok(t && t.length > 8, 'у значка есть подсказка: ' + t);
    assert.match(titles[0], /Поиск/);
    assert.match(titles[1], /Палитра/);
    assert.equal(tools[1].getAttribute('title'), 'Палитра команд · Ctrl K',
      'Ctrl K стоит там, где он действительно работает');
    assert.equal(tools[2].getAttribute('href'), '#/settings');
    assert.equal(new Set(tools.map((n) => n.getAttribute('aria-label'))).size, 3, 'подписи разные');
  } finally {
    uninstallFakeDom();
  }
});

test('без палитры группа не притворяется: два значка вместо трёх', () => {
  const dom = installFakeDom();
  try {
    const group = toolsGroup({ navigate: () => {} });
    const tools = walk(group).filter((n) => n.getAttribute && n.getAttribute('data-tool'));
    assert.deepEqual(tools.map((n) => n.getAttribute('data-tool')), ['search', 'settings']);
  } finally {
    uninstallFakeDom();
  }
});

test('app.js собирает шапку из этих частей и не рисует команду аватарами', () => {
  assert.match(appSrc, /scopeSwitcher\(/);
  assert.match(appSrc, /toolsGroup\(/);
  assert.ok(!/class: 'topbar__team'/.test(appSrc), 'куча аватаров «вся команда» убрана');
  assert.ok(!/topbar__search/.test(appSrc), 'отдельной кнопки поиска с «Ctrl K» больше нет');
  assert.match(appSrc, /topbar__divider/, 'группы значков разделены');
});

test('tabName отдаёт то же имя, что и вкладка', () => {
  assert.equal(tabName('backlog'), 'От вас нужно');
  assert.equal(tabName('roadmap'), 'Карта проекта');
  assert.equal(tabName('нет-такой'), '');
});
