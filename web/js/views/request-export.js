/**
 * request-export.js — экспорт свода одним кликом (W20-U3, пункт 17 / P13).
 *
 * Три кнопки в блоке «Свод» (низ блока, одна группа):
 *
 *   • «В Obsidian»  — файл кладётся в зеркальную папку комнаты в хранилище
 *                     заметок (`POST /api/requests/{id}/export {format:"obsidian"}`);
 *   • «Скачать HTML» — автономный файл: тот же рендер, что у блока «Свод»,
 *                     без внешних ссылок (`format:"html"`, приходит вложением);
 *   • «Печать / PDF» — печать браузера со стилем `@media print`
 *                     (`web/css/request-export.css`): в «PDF» сохраняется сам
 *                     свод, без меню, ленты и правой колонки.
 *
 * Ошибки объясняются по-русски и не «показывают успех»: если сервер не смог
 * записать заметку, человек увидит причину, а не выдуманный путь.
 *
 *   import { exportGroup } from './request-export.js';
 *   foot.appendChild(exportGroup(ctx, rid));
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { SVOD_RU } from '../text-ru.js';

/** Подписи кнопок — одни и те же на странице, в подсказках и в тестах. */
export const EXPORT_LABELS = {
  obsidian: 'В Obsidian',
  html: 'Скачать HTML',
  print: 'Печать / PDF',
  md: 'Скачать .md',
};

/** Подсказки: что именно произойдёт (без технических терминов вроде MIME). */
export const EXPORT_HINTS = {
  obsidian: 'Положить свод в заметки комнаты — файл появится в папке «Запросы» этой комнаты',
  html: 'Скачать один файл: свод, шаги, вопросы и решения — откроется в любом браузере без интернета',
  print: 'Печать или сохранение в PDF: в документ пойдёт только свод, без меню и живых блоков',
  md: 'Скачать ответ с шагами и деревом работ в Markdown',
};

/** Сколько ждать сборки HTML на сервере, прежде чем сказать «не получилось». */
const EXPORT_TIMEOUT_MS = 30000;

function toastOf(ctx) {
  return typeof ctx?.toast === 'function' ? ctx.toast : () => {};
}

/** Кнопка в состоянии «работаю»: занята, но не исчезает (не дёргается вёрстка). */
function busy(btn, label) {
  if (!btn) return () => {};
  const prev = btn.querySelector('span');
  const was = prev ? prev.textContent : '';
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  if (prev) prev.textContent = label;
  return () => {
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    if (prev) prev.textContent = was;
  };
}

/**
 * «В Obsidian»: `POST /api/requests/{id}/export {format:"obsidian"}`.
 * @returns {Promise<object|null>} ответ сервера или null при ошибке
 */
export async function exportToObsidian(ctx, rid, btn) {
  const done = busy(btn, 'Сохраняю…');
  try {
    const res = await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/export`,
      { format: 'obsidian' });
    if (res && res.ok) {
      const name = String(res.name || 'заметку');
      const where = String(res.folder || '');
      toastOf(ctx)(res.changed === false
        ? `Заметка «${name}» уже актуальна — обновили без изменений`
        : `Свод в заметках: «${name}»`, 'ok');
      if (where) {
        // Путь — в консоль, не в тост: человеку достаточно названия файла.
        try { console.info('[export] заметка:', res.path); } catch { /* нет консоли */ }
      }
      return res;
    }
    // Работа/без хранилища: честно сказать, что запись не сделана.
    toastOf(ctx)(String((res && res.reason) || 'Хранилище заметок недоступно'), 'warn');
    return res || null;
  } catch (err) {
    toastOf(ctx)((err && err.message) || 'Не удалось положить свод в заметки', 'danger');
    return null;
  } finally {
    done();
  }
}

/** Имя вложения из заголовка `Content-Disposition` (или запасное). */
export function fileNameFromDisposition(header, fallback = 'svod.html') {
  const raw = String(header || '');
  const star = /filename\*=UTF-8''([^;]+)/i.exec(raw);
  if (star) {
    try { return decodeURIComponent(star[1].trim()); } catch { /* ниже обычный вариант */ }
  }
  const plain = /filename="?([^";]+)"?/i.exec(raw);
  if (plain && plain[1]) return plain[1].trim();
  return fallback;
}

/** Отдать файл браузеру как скачивание (без навигации и без потери страницы). */
function saveBlob(blob, name, doc = globalThis.document) {
  if (!doc || typeof doc.createElement !== 'function') return false;
  const url = URL.createObjectURL(blob);
  const a = doc.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  a.style.display = 'none';
  (doc.body || doc.documentElement).appendChild(a);
  a.click();
  setTimeout(() => {
    try { a.remove(); } catch { /* уже убран */ }
    try { URL.revokeObjectURL(url); } catch { /* браузер сам */ }
  }, 1000);
  return true;
}

/**
 * «Скачать HTML»: `POST … {format:"html"}` → файл вложением.
 * @returns {Promise<boolean>} сохранён ли файл
 */
export async function exportHtml(ctx, rid, btn) {
  const done = busy(btn, 'Собираю…');
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), EXPORT_TIMEOUT_MS) : null;
  try {
    const res = await fetch(`/api/requests/${encodeURIComponent(rid)}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/html' },
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify({ format: 'html' }),
      signal: ctrl ? ctrl.signal : undefined,
    });
    if (!res.ok) {
      let message = 'Не удалось собрать файл';
      try {
        const data = await res.json();
        message = String((data && data.error && data.error.message) || message);
      } catch { /* тело не JSON — остаётся общая фраза */ }
      toastOf(ctx)(message, 'danger');
      return false;
    }
    const blob = await res.blob();
    const name = fileNameFromDisposition(res.headers.get('Content-Disposition'),
      `${String(rid || 'svod')}.html`);
    const saved = saveBlob(blob, name);
    toastOf(ctx)(saved ? `Файл сохранён: ${name}` : 'Файл готов, но браузер не дал сохранить',
      saved ? 'ok' : 'warn');
    return saved;
  } catch (err) {
    const aborted = err && (err.name === 'AbortError');
    toastOf(ctx)(aborted ? 'Сборка файла заняла слишком много времени — попробуйте ещё раз'
      : ((err && err.message) || 'Не удалось скачать файл'), 'danger');
    return false;
  } finally {
    if (timer) clearTimeout(timer);
    done();
  }
}

/**
 * «Печать / PDF»: печать браузера. Стили печати уже подключены модулем
 * (`ensureCss`), поэтому печатаем после их загрузки — иначе первый раз
 * документ ушёл бы с меню и правой колонкой.
 * @returns {Promise<boolean>}
 */
export async function printSummary(ctx) {
  try {
    await ensureCss('css/request-export.css');
  } catch { /* печать без наших правил, но не отказ */ }
  const win = globalThis;
  if (typeof win.print !== 'function') {
    toastOf(ctx)('Браузер не поддерживает печать', 'warn');
    return false;
  }
  // Небольшая пауза: браузер успевает применить стили перед открытием окна печати.
  setTimeout(() => {
    try { win.print(); } catch { toastOf(ctx)('Не удалось открыть печать', 'danger'); }
  }, 30);
  return true;
}

/**
 * Меню «Экспорт ▾» для низа блока «Свод» (W24-N1 §2.4).
 *
 * Раньше это была группа из трёх-четырёх кнопок подряд: рядом с главной
 * («Доработать свод») и «Копировать» они спорили за внимание. Теперь одна
 * кнопка-меню: под ней «В Obsidian», «Скачать HTML», «Печать / PDF» и
 * «Скачать .md». Атрибуты `data-export` прежние — по ним работают тесты.
 *
 * @param {object} ctx контекст страницы (api, toast)
 * @param {string} rid идентификатор запроса
 * @param {{markdownHref?: string, label?: string}} [opts] ссылка на .md и подпись меню
 * @returns {HTMLElement}
 */
export function exportGroup(ctx, rid, opts = {}) {
  const o = opts || {};
  const mdHref = String(o.markdownHref || `/api/export/request/${encodeURIComponent(rid)}.md`);
  const items = [
    h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-export__btn', 'data-export': 'obsidian',
      title: EXPORT_HINTS.obsidian,
    }, icon('book', { size: 13 }), h('span', null, EXPORT_LABELS.obsidian)),
    h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-export__btn', 'data-export': 'html',
      title: EXPORT_HINTS.html,
    }, icon('globe', { size: 13 }), h('span', null, EXPORT_LABELS.html)),
    h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-export__btn', 'data-export': 'print',
      title: EXPORT_HINTS.print,
    }, icon('output', { size: 13 }), h('span', null, EXPORT_LABELS.print)),
    h('a', {
      class: 'btn btn--ghost btn--sm req-export__btn req-answer__md', 'data-export': 'md',
      href: mdHref, download: '', title: EXPORT_HINTS.md,
    }, icon('file-text', { size: 13 }), h('span', null, EXPORT_LABELS.md)),
  ];
  const group = h('details', { class: 'req-export', 'data-export-menu': 'svod' },
    h('summary', {
      class: 'btn btn--ghost btn--sm req-export__summary',
      title: SVOD_RU.exportHint,
    }, icon('cloud-upload', { size: 13 }), h('span', null, o.label || SVOD_RU.export),
      h('span', { class: 'req-export__chev', 'aria-hidden': 'true' }, '▾')),
    h('div', { class: 'req-export__menu', role: 'menu', 'aria-label': 'Экспорт свода' }, ...items));
  // W25.5: необязательные пункты меню (версии свода, «Обновить свод сейчас»).
  // Их ставит липкая панель свода: при узком виде переключатель версий живёт
  // здесь, а не рядом с названием. Скрытие на широком виде — чистым CSS.
  if (Array.isArray(o.extra) && o.extra.length) {
    const menu = group.querySelector('.req-export__menu');
    const box = h('div', { class: 'req-export__extra' }, ...o.extra);
    if (menu) menu.appendChild(box);
  }
  const all = Array.from(group.querySelectorAll('[data-export]'));
  const pick = (name) => all.find((el) => el.getAttribute('data-export') === name) || null;
  const obsidianBtn = pick('obsidian');
  const htmlBtn = pick('html');
  const printBtn = pick('print');
  if (obsidianBtn) obsidianBtn.addEventListener('click', () => { exportToObsidian(ctx, rid, obsidianBtn); });
  if (htmlBtn) htmlBtn.addEventListener('click', () => { exportHtml(ctx, rid, htmlBtn); });
  if (printBtn) printBtn.addEventListener('click', () => { printSummary(ctx); });
  return group;
}

// Стили печати подключаем сразу при загрузке модуля: печать не должна ждать клика.
try { ensureCss('css/request-export.css'); } catch { /* страница без CSS — не беда */ }

export default { exportGroup, exportToObsidian, exportHtml, printSummary, EXPORT_LABELS };
