/**
 * report-embed.js — встроенный интерактивный отчёт на странице запроса.
 *
 * Отчёт грузится лениво: первый показ карточки в окне (или клик «Развернуть»)
 * → GET /api/reports/{room}/{key}. Спецификация report.json рисуется общим
 * рендерером web/js/report/index.js (report.css + report-charts.css), свободный
 * report.html — только в <iframe sandbox="allow-scripts">.
 *
 * Ключ отчёта в URL — слаг из индекса комнаты; в detail.report его нет,
 * поэтому ключ ищется по пути/запросу в GET /api/rooms/{id}/reports.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { fileUrl } from '../api.js';
import { ensureCss, skeleton, errorBanner } from '../ui.js';
import { errorText } from '../text-ru.js';

const STYLE_SPEC = ['css/report.css', 'css/report-charts.css'];

// Общий с request-parts.css файл: превью отчёта живёт в тех же стилях.
ensureCss('css/view-request-parts.css');

/** Ключ отчёта для /api/reports/{room}/{key}. */
async function resolveKey(detail, ctx) {
  const ref = detail && detail.report ? detail.report : {};
  if (ref.key) return String(ref.key);
  if (detail && detail.slug) return String(detail.slug);

  const room = String((detail && detail.room) || (ctx && ctx.room && ctx.room.id) || '');
  if (room && ctx && ctx.api) {
    try {
      const index = await ctx.api.get(`/api/rooms/${encodeURIComponent(room)}/reports`);
      const items = Array.isArray(index && index.items) ? index.items : [];
      const hit = items.find((it) => it && (
        (ref.path && it.path === ref.path)
        || (detail && detail.id && it.request_id === detail.id)
      ));
      if (hit && hit.key) return String(hit.key);
    } catch { /* индекс необязателен: дальше пробуем слаг/root id */ }
  }
  return String((detail && (detail.slug || detail.id)) || '');
}

/** Список строк из warnings/errors валидации отчёта. */
function validationLines(payload) {
  const out = [];
  const take = (list) => {
    if (!Array.isArray(list)) return;
    for (const item of list) {
      if (item === null || item === undefined) continue;
      if (typeof item === 'string') out.push(item);
      else if (typeof item === 'object') {
        const text = item.message || item.text || item.error || item.path || '';
        const where = item.path && text !== item.path ? `${item.path}: ` : '';
        const row = `${where}${text}`.trim();
        if (row) out.push(row);
      }
    }
  };
  take(payload && payload.errors);
  take(payload && payload.warnings);
  return out;
}

function validationBanner(lines) {
  if (!lines.length) return null;
  const box = h('div', { class: 'banner banner--warn rp-embed__banner', role: 'status' },
    icon('warning', { size: 14 }),
    h('div', { class: 'banner__text' },
      h('div', {}, 'Отчёт отрисован с замечаниями'),
      h('ul', { class: 'rp-embed__banner-list small' }, lines.slice(0, 8).map((t) => h('li', {}, t)))));
  return box;
}

/**
 * Встроенный отчёт запроса.
 * @param {object} detail RequestDetail
 * @param {object} ctx
 * @returns {HTMLElement|null} null, если отчёта нет
 */
export function renderReportEmbed(detail, ctx) {
  const ref = detail && detail.report;
  if (!ref || ref.available !== true) return null;

  const room = String(detail.room || (ctx && ctx.room && ctx.room.id) || '');
  const section = h('section', { class: 'card rp-embed', 'data-rp': 'report', 'aria-label': 'Интерактивный отчёт' });

  /* ── шапка ── */
  const titleBox = h('div', { class: 'rp-embed__titles' },
    h('div', { class: 'rp-embed__kicker small muted' }, 'Интерактивный отчёт'),
    h('h2', { class: 'rp-embed__title' }, String(ref.title || 'Отчёт')),
    h('p', { class: 'rp-embed__sub small muted' }, ref.kind === 'html' ? 'HTML-страница агента' : 'Таблицы, графики и связи'));
  const head = h('div', { class: 'rp-embed__head' }, titleBox);

  const expandBtn = h('button', {
    class: 'btn btn--sm btn--secondary rp-embed__expand',
    type: 'button',
    'aria-expanded': 'false',
    hidden: true,
  }, icon('unfold', { size: 13 }), h('span', {}, 'Развернуть'));

  const openTab = h('a', {
    class: 'btn btn--sm btn--ghost rp-embed__tab',
    href: '#',
    hidden: true,
    title: 'Открыть отчёт отдельной страницей',
  }, icon('link-external', { size: 13 }), h('span', {}, 'В отдельной вкладке'));

  const actions = h('div', { class: 'rp-embed__actions' }, expandBtn, openTab);
  head.appendChild(actions);
  section.appendChild(head);

  const bannerSlot = h('div', { class: 'rp-embed__banner-slot' });
  const body = h('div', { class: 'rp-embed__body is-collapsed', 'aria-busy': 'true' });
  body.appendChild(skeleton(4));
  const more = h('div', { class: 'rp-embed__more', hidden: true },
    h('button', {
      class: 'btn btn--sm btn--secondary',
      type: 'button',
      onclick: () => { if (!loaded) load(); setExpanded(true); },
    }, icon('unfold', { size: 13 }), h('span', {}, 'Показать отчёт полностью')));
  section.appendChild(bannerSlot);
  section.appendChild(body);
  section.appendChild(more);

  /* ── состояние ── */
  let key = '';
  let loaded = false;
  let loading = false;
  let expanded = false;

  const setExpanded = (on) => {
    expanded = on;
    body.classList.toggle('is-expanded', on);
    body.classList.toggle('is-collapsed', !on);
    expandBtn.setAttribute('aria-expanded', String(on));
    expandBtn.textContent = '';
    expandBtn.appendChild(icon(on ? 'fold' : 'unfold', { size: 13 }));
    expandBtn.appendChild(h('span', {}, on ? 'Свернуть' : 'Развернуть'));
    more.hidden = on || expandBtn.hidden;
  };
  expandBtn.addEventListener('click', () => {
    if (!loaded) load();
    setExpanded(!expanded);
  });

  const showExpand = (on) => {
    expandBtn.hidden = !on;
    more.hidden = !on || expanded;
    if (!on) setExpanded(false);
  };

  const frame = (url) => {
    const iframe = h('iframe', {
      class: 'rp-embed__frame',
      src: url,
      sandbox: 'allow-scripts',
      loading: 'lazy',
      referrerpolicy: 'no-referrer',
      title: String(ref.title || 'Интерактивный отчёт'),
    });
    return iframe;
  };

  async function load() {
    if (loading || loaded) return;
    loading = true;
    body.textContent = '';
    body.appendChild(skeleton(4));
    body.setAttribute('aria-busy', 'true');
    try {
      if (!key) key = await resolveKey(detail, ctx);
      if (!key) throw new Error('Не удалось определить отчёт: в индексе комнаты его нет');
      const payload = await ctx.api.get(`/api/reports/${encodeURIComponent(room)}/${encodeURIComponent(key)}`);

      body.textContent = '';
      bannerSlot.textContent = '';
      const lines = validationLines(payload);
      const banner = validationBanner(lines);
      if (banner) bannerSlot.appendChild(banner);

      const isHtml = (payload && payload.kind === 'html') || (payload && payload.url && !payload.blocks);
      if (isHtml) {
        const url = (payload && payload.url) || fileUrl(ref.path || '');
        renderHtml(url, payload && payload.title);
      } else if (payload && Array.isArray(payload.blocks)) {
        await renderSpec(payload);
      } else {
        throw new Error('Сервер вернул отчёт неизвестного вида');
      }

      if (room && key) {
        openTab.href = `#/room/${encodeURIComponent(room)}/reports/${encodeURIComponent(key)}`;
        openTab.hidden = false;
      }
      showExpand(true);
      loaded = true;
      body.removeAttribute('aria-busy');
    } catch (err) {
      // Свободный HTML-отчёт можно открыть и напрямую файлом, если API не отдал.
      if (ref.kind === 'html' && ref.path) {
        body.textContent = '';
        renderHtml(fileUrl(ref.path), ref.title);
        showExpand(true);
        loaded = true;
        body.removeAttribute('aria-busy');
        return;
      }
      body.textContent = '';
      bannerSlot.textContent = '';
      const t = errorText(err);
      if (err && err.code && t.code !== 'network') console.warn('[report-embed] код ошибки:', err.code);
      const banner = errorBanner({ message: t.message, code: t.code }, () => { loading = false; load(); });
      banner.classList.add('rp-embed__error');
      body.appendChild(banner);
      body.removeAttribute('aria-busy');
    } finally {
      loading = false;
    }
  }

  function renderHtml(url, title) {
    if (title) titleBox.querySelector('.rp-embed__title').textContent = String(title);
    titleBox.querySelector('.rp-embed__sub').textContent = 'HTML-страница агента · sandbox';
    body.appendChild(frame(url));
    body.classList.add('rp-embed__body--html');
  }

  async function renderSpec(spec) {
    if (spec.title) titleBox.querySelector('.rp-embed__title').textContent = String(spec.title);
    titleBox.querySelector('.rp-embed__sub').textContent = String(spec.subtitle || 'Таблицы, графики и связи');
    for (const href of STYLE_SPEC) ensureCss(href);
    const mod = await import('../report/index.js');
    if (typeof mod.renderReport !== 'function') throw new Error('Рендерер отчётов не загрузился');
    const holder = h('div', { class: 'rp-embed__report' });
    body.appendChild(holder);
    body.classList.add('rp-embed__body--spec');
    mod.renderReport(holder, spec, { fileUrl });
  }

  /* ── ленивая загрузка: по появлению в окне, иначе — на простое ── */
  let idleTimer = null;
  if (typeof IntersectionObserver === 'function') {
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          io.disconnect();
          if (idleTimer) clearTimeout(idleTimer);
          load();
          return;
        }
        if (!section.isConnected) io.disconnect();
      }
    }, { rootMargin: '600px 0px' });
    io.observe(section);
    section.__rpDisconnect = () => io.disconnect();
    // Страховка для длинных страниц и печати: догружаем отчёт, если он так и
    // не попал в окно (иначе карточка остаётся пустой заглушкой).
    idleTimer = setTimeout(() => { if (!loaded) load(); }, 2500);
  } else {
    load();
  }

  return section;
}

export default renderReportEmbed;
