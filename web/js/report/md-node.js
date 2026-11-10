// Единственная точка вставки готового HTML в отчёте.
//
// renderMarkdown() экранирует весь текст агента (покрыто XSS-тестами в
// tests/md.test.mjs), поэтому его результат можно ставить как HTML — но
// ровно здесь и только здесь. h()/applyAttrs innerHTML не принимают вовсе
// (util.js бросает ошибку), так что случайно «просочиться» HTML в атрибут
// больше не может.

import { h } from '../util.js';
import { renderMarkdown } from '../md.js';

/**
 * Собрать div с отрендеренным markdown.
 * @param {*} src markdown-источник (агентский текст)
 * @param {{fileUrl?:string}} [ctx] базовый URL для относительных ссылок
 * @param {string} [cls] классы контейнера
 * @returns {HTMLElement}
 */
export function mdNode(src, ctx = {}, cls = 'rb-md') {
  const el = h('div', { class: cls });
  fillMarkdown(el, renderMarkdown(String(src ?? ''), { fileUrl: ctx && ctx.fileUrl }));
  return el;
}

/**
 * Вставить готовый (уже безопасный) HTML markdown в существующий элемент.
 * Единственное место в проекте, где используется innerHTML, — сознательно.
 * @param {HTMLElement} el
 * @param {string} html результат renderMarkdown()
 * @returns {HTMLElement}
 */
export function fillMarkdown(el, html) {
  el.innerHTML = html;
  return el;
}

export { renderMarkdown };
