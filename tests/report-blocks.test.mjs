// DOM-тест рендерера отчёта: markdown реально попадает в DOM (F-1),
// innerHTML в h() запрещён (F-5), XSS не исполняется и не создаёт узлов.
// Запуск: node --test tests/report-blocks.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';

import { installHtmlDom, uninstallHtmlDom, allText } from './report-dom.mjs';

installHtmlDom();
const { h } = await import('../web/js/util.js');
const { text, callout, heading, stats, timeline, checklist } = await import('../web/js/report/blocks.js');
const { renderReport } = await import('../web/js/report/index.js');

const ctx = { fileUrl: '', renderBlocks: () => document.createDocumentFragment() };

test('text: markdown превращается в узлы, а не в атрибут', () => {
  const el = text({ type: 'text', md: 'Привет, **мир** и `код`' }, ctx);
  assert.equal(el.className, 'rb-text rb-md');
  assert.equal(el.hasAttribute('innerHTML'), false);
  assert.ok(allText(el).includes('Привет'), 'текст абзаца пуст — снова innerHTML в атрибут?');
  assert.ok(allText(el).includes('мир'));
  const strong = el.querySelectorAll('strong');
  assert.equal(strong.length, 1);
  assert.equal(allText(strong[0]), 'мир');
  assert.equal(el.querySelectorAll('p').length, 1);
});

test('callout: заголовок и тело с markdown непустые', () => {
  const el = callout({ type: 'callout', tone: 'success', title: 'Решение', md: 'Берём **вариант B**' }, ctx);
  assert.equal(el.className, 'rb-callout');
  assert.equal(el.dataset.tone, 'success');
  assert.ok(allText(el).includes('Решение'));
  assert.ok(allText(el).includes('вариант B'));
  assert.equal(el.querySelectorAll('.rb-callout__body').length, 1);
});

test('timeline и heading: текст на месте', () => {
  const hd = heading({ type: 'heading', level: 2, text: 'Итоги' }, ctx);
  assert.equal(allText(hd), 'Итоги');
  const tl = timeline({ type: 'timeline', items: [{ date: '2026-10-01', title: 'Старт', md: 'поехали' }] }, ctx);
  assert.ok(allText(tl).includes('Старт'));
  assert.ok(allText(tl).includes('поехали'));
});

test('renderReport: summary и блоки отрисованы, ошибок-карточек нет', () => {
  const host = document.createElement('div');
  const spec = {
    title: 'Отчёт',
    summary: 'Короткое **резюме**',
    blocks: [
      { type: 'text', md: 'Первый абзац' },
      { type: 'callout', tone: 'info', md: 'Тело' },
      { type: 'stats', items: [{ label: 'Выручка', value: '1 000' }] },
    ],
  };
  renderReport(host, spec);
  const summary = host.querySelectorAll('.report__summary')[0];
  assert.ok(summary && allText(summary).includes('резюме'), 'summary пуст');
  const blocks = host.querySelectorAll('.rb-text');
  assert.equal(blocks.length, 1);
  assert.ok(allText(blocks[0]).includes('Первый абзац'));
  assert.equal(host.querySelectorAll('.rb-error').length, 0);
  assert.equal(host.querySelectorAll('.report__title').length, 1);
});

test('XSS: разметка из markdown остаётся текстом, on* не появляется', () => {
  const payload = '<script>globalThis.pwned=1</script><img src=x onerror="globalThis.pwned=2">'
    + '<a href="javascript:alert(1)">x</a>';
  const el = text({ type: 'text', md: payload }, ctx);
  assert.equal(el.querySelectorAll('script').length, 0);
  assert.equal(el.querySelectorAll('img').length, 0);
  const bad = el.querySelectorAll('[onerror]');
  assert.equal(bad.length, 0);
  assert.equal(globalThis.pwned, undefined);
  // escape сработал: исходный тег виден как текст
  assert.ok(allText(el).includes('<script>'));
});

test('h() бросает на innerHTML — защита от возврата F-1', () => {
  assert.throws(() => h('div', { innerHTML: '<b>x</b>' }), /innerHTML/);
  assert.throws(() => h('div', { outerHTML: '<b>x</b>' }), /outerHTML/);
});

test('checklist: счётчик и пункты собираются без innerHTML', () => {
  const el = checklist({ type: 'checklist', items: [{ text: 'Позвонить', done: true }, { text: 'Написать' }] }, ctx);
  assert.ok(allText(el).includes('1 из 2'));
  assert.equal(el.querySelectorAll('.rb-checklist__item').length, 2);
});

test('чужой тип не ломает отчёт: неизвестный блок → карточка, без исключения', () => {
  const host = document.createElement('div');
  renderReport(host, { blocks: [{ type: 'нет-такого', x: 1 }] });
  assert.equal(host.querySelectorAll('.rb-unknown').length, 1);
});

test.after(() => uninstallHtmlDom());
