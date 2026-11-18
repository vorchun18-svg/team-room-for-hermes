/**
 * section-collapse.js — общий компонент «сворачиваемый блок» страницы запроса
 * (У-1, волна 21).
 *
 * Одна стрелка и одна разметка заголовка для всех блоков: «Запрос», «Диалог по
 * запросу», «Дерево работ», «Артефакты». Свод и Хронология остаются без стрелки
 * (решение Михаила 11.11) — их заголовки не собираются здесь.
 *
 * Что даёт компонент:
 *   * заголовок — настоящая кнопка (`<button type="button">`): клик, Enter и
 *     Space работают сами, без ручных обработчиков клавиш (иначе Enter давал бы
 *     двойное переключение: keydown + click от браузера);
 *   * `aria-expanded` и `aria-controls` — состояние и связь с содержимым для
 *     чтения с экрана;
 *   * краткая суть в заголовке (`summary`): «18 из 18 готово · есть ошибка»,
 *     «23 файла · есть новее свода» — видна и в свёрнутом виде;
 *   * ленивая сборка содержимого: пока блок свёрнут, тяжёлое (дерево на 200
 *     узлов, 200 артефактов) НЕ строится. Строится при первом раскрытии и
 *     дальше переиспользуется;
 *   * состояние помнится на запрос (`localStorage`, см. `request-tree-state.js`)
 *     и переживает перерисовку страницы: умолчание применяется только там, где
 *     человек ещё не выбирал;
 *   * `expandAllSections()` — печать (Ctrl+P, «Печать / PDF») разворачивает всё.
 *
 * Модуль без DOM-зависимостей сверх `document.createElement` — проверяется
 * в `node --test tests/section-collapse.test.mjs`.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { countNoun } from '../text-ru.js';
import { createSectionState } from './request-tree-state.js';

/** Стили блока — отдельным файлом, лениво (ensureCss дедуплицирует по href). */
export const COLLAPSE_CSS = 'css/section-collapse.css';

/** Статусы, при которых запрос считается готовым (иначе «в работе/ждёт вас»). */
export const FINAL_STATUSES = ['done', 'failed'];

/** Статусы задач, которые считаются закрытыми (как в серверном `CLOSED`). */
const CLOSED_TASK = ['done', 'archived'];

/* ─────────────────────────────────────────────────── умолчания (одна функция) */

/**
 * Умолчания раскрытости блоков — ОДНА функция, чтобы правка была в одном месте.
 *
 * Правило волны 25 (R5 §7.2, канон для пакета L2): до свода и после него в
 * потоке остаются СТРОКИ, а не карточки. Поэтому:
 *   * «Запрос» — свёрнут (как было);
 *   * «Диалог по запросу» — свёрнут всегда, на узком виде разворачивается
 *     листом снизу (`data-sheet`);
 *   * «Ход работы», «Дерево работ», «Артефакты» — свёрнуты (80 событий и
 *     дерево на 200 узлов в потоке не нужны; счётчик живёт в заголовке);
 *   * «Хронология» — последние 5 и «показать все» (в `renderTimeline`);
 *   * «Что изменилось» — открыто только на широком виде (xl/l) и только
 *     когда версия свода > 1; на виде < 1100 — одна строка.
 *
 * Это смена прежнего умолчания W21 (дерево и артефакты были развёрнуты у
 * запроса в работе): длина страницы на 1366 с сайдбаром Hermes перестала
 * укладываться в канон R5 §9 п. 5. Выбор человека по-прежнему сильнее любого
 * умолчания (`createSection` + `createSectionState.has`).
 *
 * @param {object} detail ответ `GET /api/requests/{id}`
 * @param {{layout?: string, version?: number|string}} [opts] режим вида и версия свода
 * @returns {{ask: boolean, thread: boolean, worklog: boolean, tree: boolean,
 *            artifacts: boolean, timeline: boolean, changed: boolean}}
 */
export function sectionDefaults(detail, opts = {}) {
  const o = opts || {};
  const layout = String(o.layout || '');
  // Широкий вид — xl/l (R5 §7.1). Неизвестный режим считаем узким: свёрнутое
  // безопаснее развёрнутого (длина страницы — главная жалоба R5).
  const wide = layout === 'xl' || layout === 'l';
  const version = Number(o.version) || 0;
  return {
    ask: false,
    thread: false,
    worklog: false,
    tree: false,
    artifacts: false,
    timeline: false,
    // «Что изменилось»: по умолчанию свёрнуто при виде < 1100 (R5 §7.2).
    changed: wide && version > 1,
  };
}

/* ───────────────────────────────────────── краткая суть в заголовке блока */

/**
 * Проблемы дерева, о которых нужно сказать в заголовке: ошибка и «ждёт человека».
 * Считаем по `detail.tree` (дёшево, без сборки строк дерева).
 *
 * @param {object} detail
 * @returns {{errors: number, waiting: number}}
 */
export function treeProblems(detail) {
  const nodes = Array.isArray(detail && detail.tree) ? detail.tree : [];
  let errors = 0;
  let waiting = 0;
  for (const node of nodes) {
    if (!node || typeof node !== 'object') continue;
    const status = String(node.status || '');
    if (node.error && !CLOSED_TASK.includes(status)) errors += 1;
    else if (node.needs_human === true && status !== 'done') waiting += 1;
  }
  return { errors, waiting };
}

/**
 * Краткая суть «Дерева работ»: «18 из 18 готово», плюс «есть ошибка» /
 * «ждёт вашего ответа».
 *
 * @param {object} detail
 * @returns {Array<{text: string, tone: string}>}
 */
export function treeSummaryChips(detail) {
  const chips = [];
  const p = (detail && detail.progress) || {};
  const total = Number(p.total) || 0;
  const done = Number(p.done) || 0;
  const rows = Array.isArray(detail && detail.tree) ? detail.tree.length : 0;
  if (total > 0) {
    const text = done + ' из ' + total + ' готово';
    chips.push({ text, tone: done >= total ? 'ok' : 'muted' });
  } else if (rows > 0) {
    chips.push({ text: countNoun(rows, ['задача', 'задачи', 'задач']), tone: 'muted' });
  }
  const problems = treeProblems(detail);
  if (problems.errors > 0) {
    chips.push({
      text: problems.errors === 1 ? 'есть ошибка' : 'ошибок: ' + problems.errors,
      tone: 'danger',
    });
  }
  if (problems.waiting > 0) {
    chips.push({
      text: problems.waiting === 1 ? 'ждёт вашего ответа' : 'ждут вас: ' + problems.waiting,
      tone: 'warn',
    });
  }
  return chips;
}

/**
 * Сколько файлов в артефактах (папки считаются по содержимому) и есть ли
 * файл новее свода. Без построения строк списка.
 *
 * @param {object} detail
 * @returns {{total: number, newer: boolean}}
 */
export function artifactsSummary(detail) {
  const items = Array.isArray(detail && detail.artifacts) ? detail.artifacts : [];
  let total = 0;
  let newer = false;
  for (const item of items) {
    if (!item || typeof item !== 'object') continue;
    if (item.newer_than_svod === true) newer = true;
    if (item.kind === 'folder' && Array.isArray(item.items)) {
      total += item.items.length;
      for (const sub of item.items) if (sub && sub.newer_than_svod === true) newer = true;
    } else {
      total += 1;
    }
  }
  return { total, newer };
}

/**
 * Краткая суть «Артефактов»: число файлов и «есть новее свода».
 *
 * @param {object} detail
 * @returns {Array<{text: string, tone: string}>}
 */
export function artifactsSummaryChips(detail) {
  const sum = artifactsSummary(detail);
  const chips = [];
  if (sum.total > 0) {
    chips.push({ text: countNoun(sum.total, ['файл', 'файла', 'файлов']), tone: 'muted' });
  }
  if (sum.newer) chips.push({ text: 'есть новее свода', tone: 'info' });
  return chips;
}

/* ──────────────────────────────────────────────────────────────── компонент */

/**
 * Живые блоки страницы: нужны «развернуть всё» перед печатью.
 *
 * Ключ — запрос + имя блока: страница перерисовывается по опросу и собирает
 * секции заново, поэтому старую запись того же блока заменяем, а не копим
 * (иначе за час работы страницы в списке набрались бы сотни мёртвых узлов).
 */
const LIVE = new Map();

/**
 * Собрать сворачиваемый блок.
 *
 * @param {object} opts
 * @param {string} opts.key           имя состояния: 'ask' | 'thread' | 'tree' | 'artifacts'
 * @param {string} [opts.requestId]   id запроса (ключ localStorage)
 * @param {string} opts.title         заголовок блока
 * @param {Node[]|Node|Function} [opts.content] содержимое; функция вызывается
 *        ТОЛЬКО при первом раскрытии (ленивая сборка тяжёлого)
 * @param {boolean} [opts.defaultOpen=false] умолчание (из `sectionDefaults`)
 * @param {Array<{text:string, tone?:string}>} [opts.summary] краткая суть в заголовке
 * @param {string} [opts.className] дополнительные классы секции
 * @param {Object} [opts.attrs] дополнительные атрибуты секции
 * @param {object} [opts.store] хранилище состояния (тесты подставляют своё)
 * @param {string} [opts.id] префикс id (иначе — из key и счётчика)
 * @param {Function} [opts.onToggle] (open) => void — после переключения
 * @returns {{element:HTMLElement, body:HTMLElement, header:HTMLElement, toggle:Function,
 *            setOpen:Function, isOpen:Function, setSummary:Function, key:string,
 *            expand:Function, collapse:Function, isBuilt:Function}}
 */
export function createSection(opts = {}) {
  const o = opts || {};
  const key = String(o.key || 'section');
  const requestId = String(o.requestId || '');
  // Ключ в списке «развернуть всё»: одна запись на блок одного запроса.
  const liveKey = requestId + '|' + key;
  const store = o.store === null ? null : (o.store || stateCache(requestId));
  const defaultOpen = o.defaultOpen === true;
  ensureCss(COLLAPSE_CSS);
  hookPrint();

  // id блоков — детерминированные (key, а не порядковый номер): страница
  // перерисовывается по опросу, и live-patch сравнивает узлы. Случайный id
  // заставлял бы его заменять блок каждый раз — а вместе с блоком терялся бы
  // клик (нажатие и отпускание попадали бы в разные узлы, как в B-42).
  const uid = 'sec-' + String(o.id || key).replace(/[^\w-]/g, '-');
  const bodyId = uid + '-body';
  const titleId = uid + '-title';

  const chipsHost = h('span', { class: 'sec__chips' });
  const extraHost = h('span', { class: 'sec__extra' });
  const chev = h('span', { class: 'sec__chev', 'aria-hidden': 'true' }, icon('chevron-right', { size: 12 }));
  const titleEl = h('span', { class: ['sec__title', o.titleClass || ''], id: titleId }, String(o.title || ''));

  const btn = h('button', {
    class: 'sec__toggle', type: 'button',
    'aria-expanded': 'false', 'aria-controls': bodyId,
    title: o.hint ? String(o.hint) : null,
  }, chev, titleEl, chipsHost, extraHost);

  const head = h('h2', { class: 'sec__head' }, btn);
  const body = h('div', { class: 'sec__body', id: bodyId, role: 'region', 'aria-labelledby': titleId });
  body.setAttribute('hidden', '');

  const attrs = Object.assign({
    class: [o.card === false ? '' : 'card', 'sec', o.className || ''],
    'data-section': key,
  }, o.attrs || {});
  const el = h(o.tag || 'section', attrs, head, body);

  let built = false;
  let open = store ? store.isOpen(key, defaultOpen) : defaultOpen;

  function build() {
    if (built) return;
    built = true;
    const content = typeof o.content === 'function' ? o.content() : o.content;
    if (!content) return;
    const list = Array.isArray(content) ? content : [content];
    for (const node of list) if (node) body.appendChild(node);
  }

  function paint() {
    el.setAttribute('data-open', open ? '1' : '0');
    if (open) el.classList.remove('is-collapsed');
    else el.classList.add('is-collapsed');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      build();
      body.removeAttribute('hidden');
    } else {
      body.setAttribute('hidden', '');
    }
  }

  function setOpen(next, opts2 = {}) {
    const o2 = opts2 || {};
    const want = next === true;
    const changed = want !== open;
    open = want;
    paint();
    if (changed && o2.persist !== false && store) store.set(key, open);
    if (changed && typeof o.onToggle === 'function') {
      try { o.onToggle(open, api); } catch (err) { console.warn('[section] onToggle:', err); }
    }
    return open;
  }

  const api = {
    element: el,
    header: head,
    body,
    key,
    requestId,
    /** Переключить состояние (клик по заголовку). */
    toggle() { return setOpen(!open); },
    /** Поставить состояние; `{persist:false}` — не запоминать (печать). */
    setOpen,
    /**
     * Применить УМОЛЧАНИЕ РЕЖИМА (ширина вида), не перебивая выбор человека.
     *
     * Зачем: умолчания блоков зависят от `data-layout` (R5 §7.2), а ширина вида
     * меняется без перезагрузки. Выбор человека сильнее автоматики (R5 §7.7),
     * поэтому: есть явный выбор — не трогаем; нет — ставим новое умолчание
     * без записи в хранилище (иначе «умолчание» стало бы «выбором»).
     *
     * @param {boolean} next
     * @returns {boolean} состояние блока после применения
     */
    applyDefault(next) {
      if (store && typeof store.has === 'function' && store.has(key)) return open;
      const want = next === true;
      if (want === open) return open;
      return setOpen(want, { persist: false });
    },
    /** Есть ли явный выбор человека по этому блоку. */
    hasUserChoice() {
      return Boolean(store && typeof store.has === 'function' && store.has(key));
    },
    isOpen() { return open; },
    /** Развернуть (и запомнить, если не сказано иное). */
    expand(opts2) { return setOpen(true, opts2); },
    /** Свернуть. */
    collapse(opts2) { return setOpen(false, opts2); },
    /** Собрано ли содержимое (тесты ленивой сборки). */
    isBuilt() { return built; },
    /** Заменить краткую суть в заголовке (диалог: «есть новый ответ»). */
    setSummary(chips) { paintChips(chipsHost, chips); },
    /** Дополнительный узел в заголовке справа (метка, кнопка). */
    extra: extraHost,
    /** Убрать блок из списка «развернуть всё» (страница ушла). */
    destroy() { LIVE.delete(liveKey); },
  };

  paintChips(chipsHost, o.summary);
  btn.addEventListener('click', () => { api.toggle(); });
  paint();
  LIVE.set(liveKey, api);
  return api;
}

/** Очистить узел переносимо (в тестах DOM урезан: без replaceChildren/removeChild). */
function clearNode(node) {
  if (!node) return;
  if (typeof node.replaceChildren === 'function') { node.replaceChildren(); return; }
  const kids = Array.isArray(node.childNodes) ? node.childNodes.slice() : [];
  for (const kid of kids) {
    if (typeof node.removeChild === 'function') { node.removeChild(kid); continue; }
    kid.parentNode = null;
    const i = node.childNodes.indexOf(kid);
    if (i >= 0) node.childNodes.splice(i, 1);
  }
}

/** Чипы краткой сути в заголовке: «18 из 18 готово · есть ошибка». */
function paintChips(host, chips) {
  if (!host) return;
  clearNode(host);
  const list = Array.isArray(chips) ? chips : (chips ? [chips] : []);
  for (const chip of list) {
    if (!chip) continue;
    const text = typeof chip === 'string' ? chip : String(chip.text || '');
    if (!text) continue;
    const tone = typeof chip === 'string' ? 'muted' : String(chip.tone || 'muted');
    const dot = typeof chip === 'object' && chip && chip.dot === true
      ? h('span', { class: 'chip__dot sec__dot', 'aria-hidden': 'true' }) : null;
    // Подсказка = сам текст: если чип когда-нибудь обрежется по ширине
    // (узкое окно, длинная подпись), смысл не потеряется.
    host.appendChild(h('span', { class: ['chip', 'chip--' + tone, 'sec__chip', 'tnum'], title: text }, dot, text));
  }
  host.setAttribute('data-empty', list.length ? null : '1');
  return host;
}

/** Одно хранилище состояния на запрос (не создаём на каждый блок). */
const STATE_CACHE = new Map();
function stateCache(requestId) {
  const key = String(requestId || '');
  if (!STATE_CACHE.has(key)) STATE_CACHE.set(key, createSectionState({ requestId: key }));
  return STATE_CACHE.get(key);
}

/**
 * Развернуть все блоки страницы — печать и «Печать / PDF» показывают целую
 * страницу, а не то, что человек свернул. Выборы при этом НЕ перезаписываются:
 * после печати блоки возвращаются в прежнее состояние.
 *
 * @returns {number} сколько блоков развёрнуто
 */
export function expandAllSections() {
  let n = 0;
  for (const [id, ctl] of Array.from(LIVE)) {
    try {
      if (ctl.element && ctl.element.isConnected === false) { LIVE.delete(id); continue; }
      ctl.setOpen(true, { persist: false });
      n += 1;
    } catch (err) { console.warn('[section] expandAll:', err); }
  }
  return n;
}

/** Сколько живых блоков (тесты, отчёт). */
export function liveSections() {
  return Array.from(LIVE.values()).map((ctl) => ({ key: ctl.key, requestId: ctl.requestId, open: ctl.isOpen() }));
}

let printHooked = false;
let printWas = [];
/** Перед печатью разворачиваем всё (Ctrl+P и кнопка «Печать / PDF»). */
function hookPrint() {
  if (printHooked) return;
  const g = typeof globalThis !== 'undefined' ? globalThis : null;
  const win = g && g.window ? g.window : null;
  if (!win || typeof win.addEventListener !== 'function') return;
  printHooked = true;
  try {
    win.addEventListener('beforeprint', () => {
      // Запоминаем, что человек свернул сам, — после печати вернём как было.
      printWas = Array.from(LIVE.values()).filter((ctl) => ctl && !ctl.isOpen());
      expandAllSections();
    });
    win.addEventListener('afterprint', () => {
      for (const ctl of printWas) {
        try { ctl.collapse({ persist: false }); } catch { /* блок уже убран */ }
      }
      printWas = [];
    });
  } catch { /* старый браузер — печать идёт как есть */ }
}

export default {
  createSection, sectionDefaults, treeSummaryChips, artifactsSummaryChips,
  treeProblems, artifactsSummary, expandAllSections, liveSections, COLLAPSE_CSS,
};
