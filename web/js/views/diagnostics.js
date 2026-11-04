/**
 * diagnostics.js — экран «Проверка системы» (#/settings/diagnostics).
 *
 * Отвечает на вопрос Михаила «что у меня настроено, а что нет и что сделать»:
 * список пунктов с иконкой статуса, значением и подсказкой; у большинства
 * пунктов есть одно действие — скопировать точную команду, открыть
 * документацию или перейти в нужный экран.
 *
 * Данные — GET /api/diagnostics (server/diagnostics.py). Пути и ключи сервер
 * уже приглушил; отчёт для поддержки дополнительно вычищает их на клиенте.
 */

import { h, plural } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, errorBanner, copyText, mdBlock, skeleton } from '../ui.js';

const VIEW_CSS = 'css/view-diagnostics.css';
const DOC_NAME = 'INSTALL.md';

/** Подписи и вид пункта по статусу. */
export const STATUS_LABELS = {
  ok: { label: 'в порядке', icon: 'check', chip: 'ok' },
  warn: { label: 'обратите внимание', icon: 'warning', chip: 'warn' },
  fail: { label: 'не работает', icon: 'error', chip: 'danger' },
  off: { label: 'не настроено', icon: 'circle-slash', chip: 'muted' },
};

const STATUS_ORDER = { fail: 0, warn: 1, off: 2, ok: 3 };

/** Нормализовать статус: неизвестное считаем предупреждением. */
export function normalizeStatus(status) {
  const key = String(status === null || status === undefined ? '' : status).trim().toLowerCase();
  return STATUS_LABELS[key] ? key : 'warn';
}

/** Подпись статуса человеческим языком. */
export function statusLabel(status) {
  return STATUS_LABELS[normalizeStatus(status)].label;
}

/** Пересчитать сводку на клиенте (если сервер её не прислал). */
export function summarize(checks) {
  const out = { ok: 0, warn: 0, fail: 0, off: 0, total: 0 };
  for (const item of Array.isArray(checks) ? checks : []) {
    if (!item || typeof item !== 'object') continue;
    out[normalizeStatus(item.status)] += 1;
    out.total += 1;
  }
  return out;
}

/** Проблемы сверху, «в порядке» — в конце; внутри группы порядок сервера. */
export function sortChecks(checks) {
  return (Array.isArray(checks) ? checks : [])
    .filter((item) => item && typeof item === 'object')
    .map((item, index) => ({ item, index }))
    .sort((a, b) => (STATUS_ORDER[normalizeStatus(a.item.status)] - STATUS_ORDER[normalizeStatus(b.item.status)]) || (a.index - b.index))
    .map((entry) => entry.item);
}

/** Убрать пути и секреты из строки отчёта (страховка поверх сервера). */
export function cleanLine(value) {
  let text = String(value === null || value === undefined ? '' : value).replace(/\s+/g, ' ').trim();
  text = text
    .replace(/\b[A-Za-z]:[\\/][^\s"'<>|]*/g, '<путь>')
    .replace(/(^|\s)~[^\s"'<>|]*/g, '$1<путь>')
    .replace(/\b(?:Bearer|token|password|api[_-]?key|secret)\S*/gi, '***');
  return text.slice(0, 200);
}

function stamp(seconds) {
  const ts = Number(seconds);
  const date = new Date((Number.isFinite(ts) && ts > 0 ? ts : Math.floor(Date.now() / 1000)) * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return pad(date.getDate()) + '.' + pad(date.getMonth() + 1) + '.' + date.getFullYear()
    + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

/**
 * Текст отчёта для поддержки: версия, система, статусы. Без путей и ключей.
 * @param {object} data ответ /api/diagnostics
 * @returns {string}
 */
export function buildSupportText(data) {
  const payload = data && typeof data === 'object' ? data : {};
  const app = payload.app && typeof payload.app === 'object' ? payload.app : {};
  const checks = Array.isArray(payload.checks) ? payload.checks : [];
  const summary = summarize(checks);
  const lines = [
    'Комната команды — проверка системы',
    'Версия: ' + (cleanLine(app.version) || 'не определена'),
    'Система: ' + (cleanLine(app.os) || 'неизвестна') + ' · Python ' + (cleanLine(app.python) || '—'),
    'Проверено: ' + stamp(payload.generated_at),
    '',
    'Итого: в порядке ' + (summary.ok || 0)
      + ', требуют внимания ' + (summary.warn || 0)
      + ', не работает ' + (summary.fail || 0)
      + ', не настроено ' + (summary.off || 0),
    '',
  ];
  for (const item of sortChecks(checks)) {
    lines.push('— ' + (cleanLine(item.title) || 'пункт') + ': ' + statusLabel(item.status)
      + ' — ' + (cleanLine(item.value) || 'без значения'));
  }
  lines.push('', 'Личные пути, имена и ключи в отчёт не включаются.');
  return lines.join('\n');
}

/* ------------------------------------------------------------------ разметка */

function summaryChip(status, count) {
  const meta = STATUS_LABELS[status];
  return h('span', {
    class: 'chip chip--' + meta.chip + ' diag__count',
    'data-status': status,
    'aria-label': count + ' ' + meta.label,
  },
  h('span', { class: 'chip__dot' }),
  h('span', { class: 'tnum' }, String(count)),
  h('span', {}, meta.label));
}

function headline(summary) {
  if (!summary || !summary.total) return 'Проверяем…';
  if (summary.fail) {
    return 'Есть ' + summary.fail + ' ' + plural(summary.fail, ['проблема', 'проблемы', 'проблем'])
      + ' — начните с красных пунктов.';
  }
  if (summary.warn) {
    return 'Обязательное работает. Обратите внимание на жёлтые пункты: ' + summary.warn + '.';
  }
  return 'Всё обязательное настроено — можно работать.';
}

/* ------------------------------------------------------------------- вьюха */

/**
 * @param {HTMLElement} el
 * @param {object} ctx
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  await ensureCss(VIEW_CSS);
  const api = ctx && ctx.api;
  const toast = (message, kind) => {
    if (ctx && typeof ctx.toast === 'function') ctx.toast(message, kind || 'info');
  };

  let stopped = false;
  let lastData = null;
  let roomId = String((ctx && ctx.route && ctx.route.roomId) || (ctx && ctx.room && ctx.room.id) || '').trim();

  const title = h('h1', { class: 'diag__title' }, 'Проверка системы');
  const lead = h('p', { class: 'diag__lead muted' },
    'Что настроено на этом компьютере, что нет и что сделать. Проверка ничего не меняет: '
      + 'только читает настройки, доску и журналы.');
  const refreshBtn = h('button', { class: 'btn btn--secondary', type: 'button', 'data-action': 'diag-refresh' },
    icon('refresh', { size: 13 }), 'Обновить');
  const copyBtn = h('button', { class: 'btn btn--primary', type: 'button', 'data-action': 'diag-copy' },
    icon('copy', { size: 13 }), 'Скопировать отчёт для поддержки');

  const head = h('header', { class: 'diag__head' },
    h('div', { class: 'diag__headline' }, title, lead),
    h('div', { class: 'diag__actions' }, refreshBtn, copyBtn));

  const summaryEl = h('div', { class: 'diag__summary', role: 'status', 'aria-live': 'polite' });
  const listEl = h('div', { class: 'diag__list', 'data-role': 'diag-list' });

  const docDetails = h('details', { class: 'diag-doc', id: 'diag-doc' });
  const docBody = h('div', { class: 'diag-doc__body' });
  docDetails.appendChild(h('summary', { class: 'diag-doc__summary' },
    icon('book', { size: 12 }), 'Установка и требования — docs/INSTALL.md'));
  docDetails.appendChild(docBody);

  const docLink = h('a', { class: 'diag__link', href: '#/settings/diagnostics', 'data-action': 'diag-doc' },
    icon('book', { size: 11 }), 'Документация');

  const foot = h('p', { class: 'diag__foot small muted' },
    h('a', { class: 'diag__link', href: '#/settings' }, icon('arrow-left', { size: 11 }), 'В настройки'),
    h('span', { class: 'diag__dot' }, '·'),
    docLink,
    h('span', { class: 'diag__dot' }, '·'),
    h('span', {}, 'Отчёт для поддержки не содержит личных путей и ключей.'));

  const root = h('div', { class: 'view__inner diag' }, head, summaryEl, listEl, docDetails, foot);
  el.appendChild(root);

  /* ---------------------------------------------------------- документация */

  let docLoaded = false;
  let docLoading = false;

  async function openDoc(scroll = true) {
    if (scroll) {
      docDetails.open = true;
      try { docDetails.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch { /* ignore */ }
    }
    if ((docLoaded && !docLoading) || !api) return;
    docLoading = true;
    docBody.replaceChildren(h('p', { class: 'small muted' }, 'Загружаю документацию…'));
    try {
      const text = await api.text('/api/docs?name=' + encodeURIComponent(DOC_NAME));
      if (stopped) return;
      docBody.replaceChildren(mdBlock(String(text || '')));
      docLoaded = true;
    } catch (err) {
      if (stopped) return;
      docBody.replaceChildren(h('div', { class: 'banner banner--warn diag-banner' },
        icon('warning', { size: 14 }),
        h('div', { class: 'banner__text' }, 'Документация не открылась: '
          + String((err && err.message) || 'файл недоступен') + '. Он лежит в установке: docs/INSTALL.md.')));
    } finally {
      docLoading = false;
    }
  }

  /* ---------------------------------------------------------------- пункт */

  function fixButton(fix) {
    const kind = String(fix.kind || '');
    const label = String(fix.label || '');
    const value = String(fix.value || '');
    if (kind === 'doc') {
      return h('button', { class: 'btn btn--secondary btn--sm', type: 'button' },
        icon('book', { size: 12 }), label || 'Открыть документацию');
    }
    if (kind === 'route') {
      return h('a', { class: 'btn btn--secondary btn--sm', href: value || '#/' },
        icon('arrow-right', { size: 12 }), label || 'Перейти');
    }
    if (kind === 'link') {
      return h('a', { class: 'btn btn--secondary btn--sm', href: value, target: '_blank', rel: 'noopener' },
        icon('link-external', { size: 12 }), label || 'Открыть');
    }
    return h('button', { class: 'btn btn--secondary btn--sm', type: 'button' },
      icon('copy', { size: 12 }), label || 'Скопировать команду');
  }

  function bindFix(button, fix) {
    const kind = String(fix.kind || '');
    if (kind === 'doc') {
      button.addEventListener('click', () => { openDoc(true).catch(() => {}); });
      return;
    }
    if (kind && kind !== 'command') return;
    button.addEventListener('click', async () => {
      const ok = await copyText(String(fix.value || ''));
      toast(ok ? 'Команда скопирована — вставьте её в терминал' : 'Не удалось скопировать', ok ? 'ok' : 'warn');
    });
  }

  function itemCard(item) {
    const status = normalizeStatus(item.status);
    const meta = STATUS_LABELS[status];
    const card = h('article', {
      class: 'diag-item diag-item--' + status,
      'data-id': String(item.id || ''),
      'data-status': status,
    });
    card.appendChild(h('span', { class: 'diag-item__icon', 'aria-hidden': 'true' }, icon(meta.icon, { size: 14 })));

    const headRow = h('div', { class: 'diag-item__head' },
      h('h2', { class: 'diag-item__title' }, String(item.title || '')),
      h('span', { class: 'chip chip--' + meta.chip + ' diag-item__chip' }, meta.label));
    const body = h('div', { class: 'diag-item__body' }, headRow);

    if (item.value) body.appendChild(h('p', { class: 'diag-item__value' }, String(item.value)));
    if (item.hint) body.appendChild(h('p', { class: 'diag-item__hint small muted' }, String(item.hint)));

    const details = Array.isArray(item.details) ? item.details.filter(Boolean) : [];
    if (details.length) {
      body.appendChild(h('ul', { class: 'diag-item__details' },
        details.map((line) => h('li', { class: 'mono' }, String(line)))));
    }

    const fix = item.fix && typeof item.fix === 'object' ? item.fix : null;
    if (fix && (fix.label || fix.value)) {
      const button = fixButton(fix);
      bindFix(button, fix);
      body.appendChild(h('div', { class: 'diag-item__actions' }, button));
    }
    card.appendChild(body);
    return card;
  }

  /* -------------------------------------------------------------- отрисовка */

  function render(data) {
    lastData = data;
    const checks = Array.isArray(data && data.checks) ? data.checks : [];
    const summary = data && data.summary && typeof data.summary === 'object'
      ? data.summary : summarize(checks);

    summaryEl.replaceChildren(
      h('p', { class: 'diag__verdict' }, headline(summary)),
      h('div', { class: 'diag__chips' },
        summaryChip('fail', summary.fail || 0),
        summaryChip('warn', summary.warn || 0),
        summaryChip('off', summary.off || 0),
        summaryChip('ok', summary.ok || 0)));

    listEl.replaceChildren();
    const sorted = sortChecks(checks);
    if (!sorted.length) {
      listEl.appendChild(h('div', { class: 'banner banner--info diag-banner' }, icon('info', { size: 14 }),
        h('div', { class: 'banner__text' }, 'Проверка ничего не вернула — нажмите «Обновить».')));
      return;
    }
    for (const item of sorted) listEl.appendChild(itemCard(item));
  }

  async function load({ silent = false } = {}) {
    if (!silent) {
      summaryEl.replaceChildren();
      listEl.replaceChildren(skeleton(5));
    }
    refreshBtn.disabled = true;
    try {
      const query = roomId ? '?room=' + encodeURIComponent(roomId) : '';
      let data = await api.get('/api/diagnostics' + query);
      if (stopped) return;
      // Комнату не передали в адресе — берём первую из списка, чтобы проверить
      // папку проекта. Пункт называет комнату, так что подмены не будет.
      if (!roomId && data && Array.isArray(data.checks)
        && !data.checks.some((item) => item && item.id === 'project')) {
        const fallback = firstRoomId();
        if (fallback) {
          roomId = fallback;
          data = await api.get('/api/diagnostics?room=' + encodeURIComponent(roomId));
          if (stopped) return;
        }
      }
      render(data);
    } catch (err) {
      if (stopped) return;
      summaryEl.replaceChildren();
      listEl.replaceChildren();
      listEl.appendChild(errorBanner(err, () => { load().catch(() => {}); }));
    } finally {
      if (!stopped) refreshBtn.disabled = false;
    }
  }

  function firstRoomId() {
    try {
      const store = (ctx && ctx.store) || {};
      const list = store.rooms && typeof store.rooms.get === 'function' ? store.rooms.get('list') : null;
      if (Array.isArray(list) && list.length && list[0] && list[0].id) return String(list[0].id);
    } catch { /* ignore */ }
    return '';
  }

  /* --------------------------------------------------------------- действия */

  refreshBtn.addEventListener('click', () => { load().catch(() => {}); });

  copyBtn.addEventListener('click', async () => {
    if (!lastData) {
      toast('Сначала дождитесь проверки', 'warn');
      return;
    }
    const ok = await copyText(buildSupportText(lastData));
    toast(ok ? 'Отчёт скопирован — вставьте его в письмо или чат' : 'Не удалось скопировать',
      ok ? 'ok' : 'warn');
  });

  docLink.addEventListener('click', (ev) => {
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    openDoc(true).catch(() => {});
  });

  await load();

  return {
    unmount() {
      stopped = true;
      root.remove();
    },
  };
}

export default mount;
