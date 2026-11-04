/**
 * reports.js — вкладка «Отчёты».
 *
 *   #/room/<id>/reports            — галерез отчётов комнаты
 *   #/room/<id>/reports/<key>      — просмотр одного отчёта (spec или html)
 *
 * Данные — только через ctx.api. Разметка — h()/текстовые узлы, без innerHTML.
 * Живое обновление галереи — api.poll с ETag: перерисовка только при изменениях.
 */

import { h, fmtDate, plural } from '../util.js';
import { icon } from '../icons.js';
import { fileUrl } from '../api.js';
import {
  ensureCss, emptyState, skeleton, errorBanner, sectionHeader, copyText, openPath,
} from '../ui.js';
import {
  requestFilterOf, requestFilterChip, filterByRequest, requestTitle,
  sourceTitle, sourceHint, scopeHash,
} from '../scope.js';

const VIEW_CSS = 'css/view-reports.css';
const REPORT_CSS = ['css/report.css', 'css/report-charts.css'];

/** Ключ отчёта из маршрута: router кладёт 4-й сегмент в params.id. */
function reportKey(ctx) {
  const params = ctx.params || {};
  const route = ctx.route || {};
  const raw = params.id || route.id || null;
  return raw ? String(raw) : null;
}

function roomIdOf(ctx) {
  const route = ctx.route || {};
  const room = ctx.room || {};
  return String(route.roomId || (ctx.params && ctx.params.roomId) || room.id || '');
}

function reportsHash(roomId, key) {
  const base = '#/room/' + encodeURIComponent(roomId) + '/reports';
  return key ? base + '/' + encodeURIComponent(key) : base;
}

/** Человеческое число: «3 отчёта», «10 блоков». */
function countLabel(n, forms) {
  return String(n) + ' ' + plural(n, forms);
}

/** Список замечаний валидации из ApiError (detail.errors[]). */
function validationIssues(err) {
  const out = [];
  const push = (value) => {
    if (value === null || value === undefined) return;
    if (typeof value === 'string') { if (value.trim()) out.push(value.trim()); return; }
    if (typeof value === 'object') {
      const where = value.path || value.where || value.loc || '';
      const message = value.message || value.msg || value.error || '';
      const line = [where, message].filter(Boolean).join(' — ');
      if (line) out.push(line);
    }
  };
  const details = err && err.details;
  const list = details && details.error && details.error.detail && details.error.detail.errors;
  if (Array.isArray(list)) list.forEach(push);
  else if (list) push(list);
  return out;
}

/** Чип вида отчёта: spec — интерактивный JSON, html — готовая страница. */
function kindChip(kind) {
  const spec = kind === 'spec';
  return h('span', {
    class: 'chip ' + (spec ? 'chip--info' : 'chip--muted'),
    title: spec ? 'Интерактивный отчёт: таблицы и графики' : 'Готовая страница отчёта',
  }, icon(spec ? 'graph' : 'globe', { size: 11 }), spec ? 'интерактивный' : 'HTML');
}

/** Карточка галереи. */
function reportCard(item, roomId, titles = {}) {
  const key = item.key || '';
  const href = reportsHash(roomId, key);
  const title = item.title || key || 'Без названия';
  const blocks = Number(item.blocks) || 0;
  const rid = item.request_id ? String(item.request_id) : '';
  const ridTitle = rid ? (titles[rid] || '') : '';

  const meta = h('div', { class: 'rep-card__meta' },
    kindChip(item.kind),
    blocks ? h('span', { class: 'chip chip--outline', title: 'Блоков в отчёте' },
      countLabel(blocks, ['блок', 'блока', 'блоков'])) : null,
    item.created_at ? h('span', { class: 'small muted nowrap rep-card__date', title: 'Создан' },
      fmtDate(item.created_at)) : null,
  );

  const card = h('article', { class: 'rep-card', dataset: { key } },
    h('div', { class: 'rep-card__main' },
      h('a', { class: 'rep-card__title', href }, title),
      item.subtitle ? h('p', { class: 'rep-card__sub' }, String(item.subtitle)) : null,
      meta,
    ),
    h('div', { class: 'rep-card__foot' },
      rid
        ? h('a', {
          class: 'rep-card__src small',
          href: '#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(rid),
          title: sourceHint(ridTitle, rid),
        }, icon('comment-discussion', { size: 11 }), sourceTitle(ridTitle, rid) || 'запрос-источник')
        : h('span', { class: 'small muted' }, 'Без запроса-источника'),
      h('span', { class: 'spacer' }),
      h('a', { class: 'rep-card__open small', href }, 'Открыть', icon('arrow-right', { size: 11 })),
    ),
  );
  // Вся карточка кликабельна, но ссылки внутри работают как обычно.
  card.addEventListener('click', (e) => {
    const target = e.target;
    if (target && typeof target.closest === 'function'
      && target.closest('a, button')) return;
    location.hash = href;
  });
  card.classList.add('rep-card--clickable');
  if (item.exists === false) {
    card.classList.add('rep-card--missing');
    card.appendChild(h('div', { class: 'rep-card__warn small' },
      icon('warning', { size: 11 }), 'Файл отчёта не найден на диске'));
  }
  return card;
}

/* --------------------------------------------------------------- галерея */

async function mountGallery(el, ctx, roomId) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  const path = '/api/rooms/' + encodeURIComponent(roomId) + '/reports';

  const root = h('div', { class: 'view__inner rep' });
  const head = h('div', { class: 'rep__head' });
  const body = h('div', { class: 'rep__body' });
  root.appendChild(head);
  root.appendChild(body);
  el.appendChild(root);

  let items = [];
  let titles = {};
  let signature = '';
  let stopped = false;
  let loading = true;
  let error = null;

  const requestId = requestFilterOf(ctx.route);
  let requestLabel = '';
  if (requestId) requestLabel = await requestTitle(api, requestId);

  /** Снять фильтр: та же вкладка без параметра ?request. */
  function clearFilter() {
    const hash = scopeHash(roomId, 'reports');
    if (typeof ctx.navigate === 'function') ctx.navigate(hash);
    else location.hash = hash;
  }

  /** id запроса-источника → название (id в тексте не показываем, только в title). */
  async function loadTitles(list) {
    const ids = Array.from(new Set((Array.isArray(list) ? list : [])
      .map((x) => String((x && x.request_id) || '')).filter(Boolean)));
    const out = {};
    await Promise.all(ids.slice(0, 12).map(async (rid) => {
      out[rid] = await requestTitle(api, rid);
    }));
    return out;
  }

  function shownItems() {
    return filterByRequest(items, requestId);
  }

  function renderHead() {
    const refresh = h('button', {
      class: 'btn btn--secondary btn--sm', type: 'button',
      onclick: () => load({ force: true }),
    }, icon('refresh', { size: 12 }), 'Обновить');
    const shown = shownItems();
    const head_box = sectionHeader('Отчёты', {
      count: loading ? undefined : shown.length,
      actions: refresh,
    });
    head.replaceChildren(head_box);
    if (requestId) {
      head.appendChild(h('div', { class: 'rep__scope row row--wrap' }, requestFilterChip({
        requestId,
        title: requestLabel,
        onClear: clearFilter,
      })));
    }
    if (!loading && !error) {
      head.appendChild(h('p', { class: 'rep__hint small muted' },
        requestId
          ? 'Здесь только отчёты этого запроса. Чип выше снимает фильтр — тогда видны отчёты всей комнаты.'
          : 'Интерактивные страницы, которые команда собрала по запросам: таблицы, графики, схемы. Открываются на весь экран.'));
    }
  }

  function renderBody() {
    body.replaceChildren();
    if (loading) {
      body.appendChild(skeleton(4));
      return;
    }
    if (error) {
      body.appendChild(errorBanner(error, () => load({ force: true })));
      return;
    }
    const shown = shownItems();
    if (!shown.length) {
      body.appendChild(requestId
        ? emptyState({
          icon: 'report',
          title: 'У этого запроса отчётов нет',
          text: 'Отчёт появляется, когда команда присылает интерактивную страницу. Снимите фильтр, чтобы увидеть отчёты всей комнаты.',
          action: { label: 'Показать все отчёты', onclick: clearFilter },
        })
        : emptyState({
          icon: 'report',
          title: 'Пока ни одного отчёта',
          text: 'Отчёт появляется, когда команда присылает интерактивную страницу. Попросите в запросе: '
            + '«сделай интерактивный отчёт с таблицей и графиком». Команда соберёт страницу — '
            + 'и она появится здесь.',
        }));
      return;
    }
    const grid = h('div', { class: 'rep-grid', role: 'list' });
    for (const item of shown) {
      const card = reportCard(item, roomId, titles);
      card.setAttribute('role', 'listitem');
      grid.appendChild(card);
    }
    body.appendChild(grid);
  }

  async function load({ force = false, silent = false } = {}) {
    if (!silent) { loading = true; renderHead(); renderBody(); }
    try {
      // Условный GET — только когда уже есть с чем сравнивать (иначе 304
      // от чужого запроса оставит галерею пустой).
      const conditional = !force && Boolean(signature);
      const payload = await api.get(path, conditional ? { etag: true } : undefined);
      if (stopped) return;
      if (payload && payload.notModified) { loading = false; return; }
      const next = Array.isArray(payload && payload.items) ? payload.items : [];
      const nextSig = JSON.stringify(next);
      loading = false;
      error = null;
      if (nextSig !== signature) {
        signature = nextSig;
        items = next;
        titles = await loadTitles(items);
        renderHead();
        renderBody();
      }
    } catch (err) {
      if (stopped) return;
      loading = false;
      error = err;
      renderHead();
      renderBody();
    }
  }

  // Первый показ и мягкое обновление раз в 15 с (без перерисовки при 304).
  await load();
  const timer = api.poll ? api.poll(() => load({ silent: true }), 15000, { pauseHidden: true, immediate: false }) : null;

  return {
    unmount() {
      stopped = true;
      if (timer && typeof timer.stop === 'function') timer.stop();
      root.remove();
    },
  };
}

/* ------------------------------------------------------------ просмотр */

/** Песочница для HTML-отчёта: скрипты внутри, без доступа к родителю. */
function htmlFrame(url, title) {
  return h('iframe', {
    class: 'rep-frame',
    src: String(url || ''),
    title: title || 'HTML-отчёт',
    sandbox: 'allow-scripts',
    referrerpolicy: 'no-referrer',
    loading: 'eager',
  });
}

async function mountDetail(el, ctx, roomId, key) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  const roomTitle = (ctx.room && ctx.room.title) || roomId;

  const root = h('div', { class: 'view__inner rep rep--wide' });
  const bar = h('div', { class: 'rep-toolbar' });
  const head = h('div', { class: 'rep-detail__head' });
  const body = h('div', { class: 'rep-detail__body' });

  const back = h('a', { class: 'btn btn--ghost btn--sm', href: reportsHash(roomId) },
    icon('chevron-left', { size: 13 }), 'К отчётам');
  const actions = h('div', { class: 'rep-toolbar__actions' });
  bar.appendChild(back);
  bar.appendChild(h('span', { class: 'rep-toolbar__crumbs small muted truncate' },
    roomTitle + ' · отчёты'));
  bar.appendChild(h('span', { class: 'spacer' }));
  bar.appendChild(actions);

  root.appendChild(bar);
  root.appendChild(head);
  root.appendChild(body);
  el.appendChild(root);

  let stopped = false;

  function renderLoading() {
    actions.replaceChildren();
    head.replaceChildren();
    body.replaceChildren(skeleton(5));
  }

  async function load() {
    if (stopped) return;
    renderLoading();
    let meta = null;
    // Индекс — для заголовка, пути и запроса-источника; его отсутствие не блокирует просмотр.
    try {
      const index = await api.get('/api/rooms/' + encodeURIComponent(roomId) + '/reports');
      const list = Array.isArray(index && index.items) ? index.items : [];
      meta = list.find((x) => x && String(x.key) === String(key)) || null;
    } catch { meta = null; }
    if (stopped) return;

    let report;
    try {
      report = await api.get('/api/reports/' + encodeURIComponent(roomId) + '/' + encodeURIComponent(key));
    } catch (err) {
      if (stopped) return;
      actions.replaceChildren();
      head.replaceChildren(h('h2', { class: 'rep-detail__title' }, (meta && meta.title) || key));
      body.replaceChildren(errorBanner(err, load));
      const issues = validationIssues(err);
      if (issues.length) {
        const list = h('ul', { class: 'rep-issues' });
        for (const issue of issues) list.appendChild(h('li', {}, issue));
        body.appendChild(h('div', { class: 'banner banner--warn rep-issues__banner' },
          icon('warning', { size: 14 }),
          h('div', { class: 'banner__text' },
            h('div', {}, 'Отчёт не прошёл проверку формата:'),
            list),
        ));
      }
      if (err && err.code === 'not_found') {
        body.appendChild(emptyState({
          icon: 'report',
          title: 'Отчёт не найден',
          text: 'Возможно, его переименовали или удалили. Вернитесь к списку отчётов комнаты.',
        }));
      }
      return;
    }
    if (stopped) return;

    const kind = report && report.kind === 'html' ? 'html' : 'spec';
    const title = (meta && meta.title) || (report && report.title) || key;

    // Свой заголовок нужен только HTML-отчёту: spec рисует шапку сам.
    if (kind === 'html') {
      head.replaceChildren(
        h('h2', { class: 'rep-detail__title' }, String(title)),
        h('div', { class: 'rep-detail__meta small muted' },
          'HTML-страница',
          report.subtitle ? ' · ' + String(report.subtitle) : '',
          meta && meta.updated_at ? ' · обновлён ' + fmtDate(meta.updated_at) : '',
        ),
      );
    } else {
      head.replaceChildren();
    }

    // Тулбар.
    const printBtn = h('button', {
      class: 'btn btn--secondary btn--sm', type: 'button', title: 'Печать или сохранение в PDF (Ctrl+P)',
      onclick: () => window.print(),
    }, icon('report', { size: 12 }), 'Печать');

    const tools = [printBtn];
    if (meta && meta.path) {
      tools.push(h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button', title: meta.path,
        onclick: () => openPath(meta.path),
      }, icon('file-text', { size: 12 }), kind === 'html' ? 'Открыть HTML' : 'Открыть исходные данные'));
      tools.push(h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button', title: 'Скопировать путь к файлу',
        onclick: async () => {
          const ok = await copyText(meta.path);
          if (ctx.toast) ctx.toast(ok ? 'Путь скопирован' : 'Не удалось скопировать путь', ok ? 'ok' : 'warn');
        },
      }, icon('copy', { size: 12 }), 'Копировать путь'));
    }
    if (meta && meta.request_id) {
      tools.push(h('a', {
        class: 'btn btn--ghost btn--sm',
        href: '/api/export/request/' + encodeURIComponent(meta.request_id) + '.html',
        title: 'Скачать автономный HTML со всем запросом и отчётом',
        download: 'request-' + String(meta.request_id) + '.html',
      }, icon('cloud-upload', { size: 12 }), 'Экспорт HTML'));
    }
    actions.replaceChildren(...tools);

    // Баннер предупреждений отчёта (если сервер их прислал).
    const warnings = Array.isArray(report && report.warnings) ? report.warnings : [];
    const warnBanner = warnings.length
      ? h('div', { class: 'banner banner--warn rep-warn' }, icon('warning', { size: 14 }),
        h('div', { class: 'banner__text' }, warnings.map((w) => (typeof w === 'string' ? w : (w && w.message) || '')).filter(Boolean).join(' · ')))
      : null;

    body.replaceChildren();
    if (warnBanner) body.appendChild(warnBanner);

    const doc = h('div', { class: 'rep-doc ' + (kind === 'html' ? 'rep-doc--frame' : 'rep-doc--spec') });
    body.appendChild(doc);

    if (kind === 'html') {
      const url = (report && (report.url || report.path)) || '';
      if (!url) {
        body.replaceChildren(errorBanner(new Error('Сервер не вернул адрес HTML-отчёта')));
        return;
      }
      body.classList.add('rep-detail__body--frame');
      doc.appendChild(htmlFrame(url, title));
      const note = h('p', { class: 'rep-frame__note small muted' },
        icon('lock', { size: 11 }),
        'Отчёт открыт в песочнице: скрипты работают, но не видят приложение и ваши данные.');
      body.appendChild(note);
      return;
    }

    // spec: рендерер reportkit.
    body.classList.remove('rep-detail__body--frame');
    await Promise.all(REPORT_CSS.map((href) => ensureCss(href)));
    try {
      const mod = await import('../report/index.js');
      if (typeof mod.renderReport !== 'function') throw new Error('report/index.js не экспортирует renderReport');
      mod.renderReport(doc, report, { fileUrl });
    } catch (err) {
      doc.replaceChildren(errorBanner(err, load));
    }
  }

  await load();

  return {
    unmount() {
      stopped = true;
      root.remove();
    },
  };
}

/**
 * Точка входа вьюхи.
 * @param {HTMLElement} el
 * @param {{route:object, params:object, api:object, room:object|null, toast?:Function, navigate?:Function}} ctx
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  const roomId = roomIdOf(ctx);
  if (!roomId) {
    await ensureCss(VIEW_CSS);
    const box = h('div', { class: 'view__inner rep' });
    box.appendChild(emptyState({
      icon: 'report',
      title: 'Комната не выбрана',
      text: 'Откройте комнату в левой колонке — отчёты живут внутри комнаты.',
      action: { label: 'К списку комнат', onclick: () => { location.hash = '#/'; } },
    }));
    el.appendChild(box);
    return { unmount() { box.remove(); } };
  }
  const key = reportKey(ctx);
  return key ? mountDetail(el, ctx, roomId, key) : mountGallery(el, ctx, roomId);
}

export default mount;
