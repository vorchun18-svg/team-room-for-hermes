/**
 * storage.js — экран «Хранилище» (#/settings/storage) и его раздел в «Настройках».
 *
 * Отвечает на три вопроса человека: сколько занято чем, что и когда будет
 * убрано, что сделать сейчас. Данные — `GET /api/storage` (server/storage.py).
 *
 * Правила экрана:
 *  • «Очистить сейчас» сначала показывает СПИСОК (сухой прогон), и только вторая
 *    кнопка «Удалить» с подтверждением действительно удаляет;
 *  • чужое (Vault, Hermes, принесённые человеком материалы) — только цифры и
 *    «Открыть папку»: кнопки удаления у таких строк нет вовсе;
 *  • пустое состояние объясняет, зачем экран, и даёт действие.
 */

import { h, plural } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, sectionHeader, errorBanner, skeleton, confirmDialog, openPath, field } from '../ui.js';

const VIEW_CSS = 'css/view-storage.css';

/** Сколько это по-русски: «1 файл», «2 файла», «5 файлов». */
export function filesText(count) {
  return plural(Number(count) || 0, ['файл', 'файла', 'файлов']);
}

/** «Убрать нельзя» — строка честная и без кнопки (Vault, Hermes). */
export function isForeign(row) {
  return Boolean(row && (row.foreign || row.cleanable === false));
}

/** Подпись кнопки очистки по числу найденного. */
export function cleanButtonLabel(willRemove) {
  const item = willRemove || {};
  const count = Number(item.count) || 0;
  if (!count) return 'Очистить сейчас';
  return 'Очистить сейчас (' + count + ')';
}

/** Сводка «что будет убрано» одной строкой. */
export function willRemoveText(willRemove) {
  const item = willRemove || {};
  const count = Number(item.count) || 0;
  if (!count) return 'Убирать нечего — всё в пределах срока хранения';
  return 'Будет убрано: ' + count + ' ' + filesText(count) + ' · ' + (item.size_text || '0 Б');
}

/** Человеческий список настроек хранения для подписи под полем. */
export function policySummary(policy) {
  const p = policy || {};
  const parts = [];
  if (p.voice_hours !== undefined) parts.push('диктовки — ' + p.voice_hours + ' ч');
  if (p.logs_days !== undefined) parts.push('логи — ' + p.logs_days + ' дн.');
  if (p.backups_keep !== undefined) parts.push('бэкапы — последние ' + p.backups_keep);
  if (p.build_cache_days !== undefined) parts.push('сборки — ' + p.build_cache_days + ' дн.');
  return parts.join(' · ');
}

/* ------------------------------------------------------------------ разметка */

function sizeCell(row) {
  return h('div', { class: 'stor-row__size' },
    h('span', { class: 'stor-row__bytes tnum' }, row.size_text || '0 Б'),
    h('span', { class: 'stor-row__files small muted' },
      row.exists === false ? 'папки нет' : filesText(row.files || 0)));
}

function rowEl(row, ctx, actions) {
  const foreign = isForeign(row);
  const will = row.will_remove || {};
  const preview = Array.isArray(will.preview) ? will.preview : [];
  const body = h('div', { class: 'stor-row__body' },
    h('div', { class: 'stor-row__title-line' },
      h('span', { class: 'stor-row__title' }, row.title || row.id),
      foreign
        ? h('span', { class: 'chip chip--muted', title: 'Приложение эти файлы не удаляет' }, 'только показ')
        : h('span', { class: 'chip ' + (Number(will.count) ? 'chip--warn' : 'chip--ok') },
          willRemoveText(will)),
      row.truncated ? h('span', { class: 'chip chip--warn', title: 'Слишком много файлов — размер посчитан частично' }, 'частично') : null),
    h('p', { class: 'stor-row__why small muted' }, row.why || ''),
    h('p', { class: 'stor-row__policy small' },
      h('span', { class: 'muted' }, 'Хранение: '), row.policy || '—'),
    preview.length
      ? h('details', { class: 'stor-row__preview' },
        h('summary', { class: 'small muted' }, 'Что попадёт под чистку'),
        h('ul', { class: 'stor-row__list small' },
          preview.map((item) => h('li', { class: 'mono', title: item.path || '' },
            (item.name || '') + ' — ' + (item.size_text || '')))))
      : null,
    row.proposal && row.proposal.files
      ? h('p', { class: 'stor-row__note small' },
        'Можно заменить ссылками: ' + row.proposal.files + ' ' + filesText(row.proposal.files)
        + ' · ' + (row.proposal.bytes ? Math.round(row.proposal.bytes / (1024 * 1024)) + ' МБ' : '0 Б')
        + '. Копии при этом не удаляются — сначала посмотрите список.')
      : null);

  return h('div', { class: 'stor-row' + (foreign ? ' stor-row--foreign' : ''), 'data-category': row.id },
    h('div', { class: 'stor-row__head' },
      h('span', { class: 'stor-row__icon' }, icon(foreign ? 'lock' : 'database', { size: 15 })),
      body,
      sizeCell(row),
      h('div', { class: 'stor-row__actions' }, actions || []),
    ));
}

/** Кнопка «Открыть папку» — только для существующих папок. */
function openButton(row) {
  return h('button', {
    class: 'btn btn--secondary btn--sm', type: 'button',
    title: row.path || '',
    onclick: () => { openPath(row.path); },
  }, icon('folder-opened', { size: 13 }), 'Открыть');
}

/* ------------------------------------------------------------------ вьюха */

async function mountStorage(el, ctx) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  let stopped = false;
  let data = null;
  let preview = null;
  let busy = false;

  const root = h('div', { class: 'view__inner stor' });
  el.appendChild(root);

  const lead = h('p', { class: 'stor__lead muted' },
    'Сколько места занимает что, что приложение уберёт само и когда. '
    + 'Сначала — список (ничего не удаляется), удаление — только по вашей второй кнопке.');
  root.appendChild(lead);

  const summary = h('div', { class: 'stor__summary card' });
  const listHost = h('div', { class: 'stor__list' });
  const previewHost = h('div', { class: 'stor__preview' });
  const policyHost = h('div', { class: 'stor__policy' });
  const errorHost = h('div', { class: 'stor__error' });
  root.appendChild(summary);
  root.appendChild(errorHost);
  root.appendChild(listHost);
  root.appendChild(previewHost);
  root.appendChild(policyHost);

  const skeletonBox = skeleton(4);
  listHost.appendChild(skeletonBox);

  function showError(err, retry) {
    errorHost.replaceChildren(errorBanner(err, retry));
  }

  async function load() {
    try {
      data = await api.get('/api/storage');
      if (stopped) return;
      errorHost.replaceChildren();
      render();
    } catch (err) {
      if (stopped) return;
      showError(err, () => load());
    }
  }

  function renderSummary() {
    const total = data && data.total_text ? data.total_text : '0 Б';
    const children = [
      h('div', { class: 'stor__summary-row' },
        h('div', { class: 'stor__summary-item' },
          h('span', { class: 'stor__summary-label small muted' }, 'Занято всего'),
          h('strong', { class: 'stor__summary-value tnum' }, total)),
        h('div', { class: 'stor__summary-item' },
          h('span', { class: 'stor__summary-label small muted' }, 'Свободно на диске'),
          h('strong', { class: 'stor__summary-value tnum' }, (data && data.free_text) || '—')),
        h('div', { class: 'stor__summary-actions' },
          h('button', {
            class: 'btn btn--primary btn--sm', type: 'button', disabled: busy,
            onclick: () => runClean(),
          }, icon('trash', { size: 13 }), cleanButtonLabel(totalWillRemove())),
          h('button', {
            class: 'btn btn--secondary btn--sm', type: 'button', disabled: busy,
            onclick: () => load(),
          }, icon('refresh', { size: 13 }), 'Обновить'))),
      h('p', { class: 'small muted stor__summary-hint' },
        'Уборка затрагивает только то, что создало приложение: диктовки, логи, бэкапы, '
        + 'временные папки. Vault, папка Hermes и принесённые вами материалы не удаляются.'),
    ];
    if (data && data.warning) children.push(h('p', { class: 'stor__warning small' }, data.warning));
    summary.replaceChildren(...children);
  }

  function totalWillRemove() {
    const rows = (data && data.categories) || [];
    let count = 0;
    let bytes = 0;
    for (const row of rows) {
      if (isForeign(row)) continue;
      const will = row.will_remove || {};
      count += Number(will.count) || 0;
      bytes += Number(will.bytes) || 0;
    }
    return { count, bytes, size_text: bytes ? null : '0 Б' };
  }

  function render() {
    if (!data) return;
    renderSummary();
    const rows = (data.categories || []).filter((row) => row.exists !== false || (row.will_remove || {}).count);
    listHost.replaceChildren();
    if (!rows.length) {
      listHost.appendChild(h('div', { class: 'card empty' },
        icon('database', { size: 22 }),
        h('div', { class: 'empty__title' }, 'Хранилище пустое'),
        h('p', { class: 'empty__text' },
          'Пока нет ни диктовок, ни логов, ни бэкапов: убирать нечего. '
          + 'Здесь появятся размеры, как только приложение что-то сохранит.'),
        h('div', { class: 'empty__actions' },
          h('button', { class: 'btn btn--secondary btn--sm', type: 'button', onclick: () => load() },
            icon('refresh', { size: 13 }), 'Проверить снова'))));
    } else {
      for (const row of rows) {
        const actions = [];
        if (row.exists !== false && row.path) actions.push(openButton(row));
        listHost.appendChild(rowEl(row, ctx, actions));
      }
    }
    renderPreview();
    renderPolicy();
  }

  function renderPreview() {
    previewHost.replaceChildren();
    if (!preview) return;
    const items = preview.would_remove || [];
    const refused = preview.refused || [];
    const box = h('div', { class: 'card stor-preview' });
    box.appendChild(sectionHeader(preview.dry_run === false ? 'Удалено' : 'Что будет удалено',
      { count: items.length }));
    box.appendChild(h('p', { class: 'small muted' }, preview.message || ''));
    if (items.length) {
      box.appendChild(h('ul', { class: 'stor-preview__list small' },
        items.slice(0, 50).map((item) => h('li', { class: 'mono', title: item.path },
          (item.path || '') + ' — ' + (item.size_text || '')))));
      if (items.length > 50) {
        box.appendChild(h('p', { class: 'small muted' }, '…и ещё ' + (items.length - 50)));
      }
    }
    if (refused.length) {
      box.appendChild(h('p', { class: 'small stor-preview__refused' },
        'Не тронуто: ' + refused.length + '. ' + (refused[0].reason || '')));
    }
    if (preview.dry_run !== false && items.length) {
      box.appendChild(h('div', { class: 'stor-preview__actions' },
        h('button', {
          class: 'btn btn--danger btn--sm', type: 'button', disabled: busy,
          onclick: () => confirmClean(items.length, preview.bytes),
        }, icon('trash', { size: 13 }), 'Удалить ' + items.length + ' ' + filesText(items.length)),
        h('button', {
          class: 'btn btn--secondary btn--sm', type: 'button',
          onclick: () => { preview = null; renderPreview(); },
        }, 'Оставить как есть')));
    }
    previewHost.appendChild(box);
  }

  function renderPolicy() {
    const policyHostInner = h('div', { class: 'card stor-policy' });
    const policy = (data && data.policy) || {};
    const limits = {};
    policyHostInner.appendChild(sectionHeader('Что хранить и сколько'));
    policyHostInner.appendChild(h('p', { class: 'small muted' },
      'Настройка только для того, что создаёт приложение. Изменения применяются к следующей уборке.'));
    const inputs = {};
    const defs = [
      ['voice_hours', 'Голосовые диктовки, часов'],
      ['logs_days', 'Логи приложения, дней'],
      ['backups_keep', 'Бэкапы: последних штук'],
      ['build_cache_days', 'Кэш сборок, дней'],
    ];
    const grid = h('div', { class: 'stor-policy__grid' });
    for (const [key, label] of defs) {
      const input = h('input', {
        class: 'input input--sm', type: 'number', min: '0', step: '1',
        value: String(policy[key] === undefined ? '' : policy[key]),
        'data-policy': key,
      });
      inputs[key] = input;
      grid.appendChild(field({ label, control: input }));
    }
    policyHostInner.appendChild(grid);
    const note = h('p', { class: 'small muted stor-policy__note' }, '');
    policyHostInner.appendChild(note);
    policyHostInner.appendChild(h('div', { class: 'stor-policy__actions' },
      h('button', {
        class: 'btn btn--primary btn--sm', type: 'button',
        onclick: async () => {
          const patch = {};
          for (const [key, input] of Object.entries(inputs)) {
            const value = Number(input.value);
            if (Number.isFinite(value)) patch[key] = value;
          }
          try {
            const res = await api.put('/api/storage/policy', patch);
            if (stopped) return;
            data.policy = res.policy || data.policy;
            note.textContent = res.message || 'Сохранено.';
            note.classList.remove('stor-policy__note--error');
            if (typeof ctx.toast === 'function') ctx.toast('Настройки хранения сохранены', 'ok');
            await load();
          } catch (err) {
            note.textContent = String((err && err.message) || 'Не удалось сохранить');
            note.classList.add('stor-policy__note--error');
          }
        },
      }, 'Сохранить настройки')));
    policyHost.replaceChildren(policyHostInner);
  }

  async function runClean() {
    if (busy) return;
    busy = true;
    renderSummary();
    try {
      preview = await api.post('/api/storage/clean', { dry_run: true });
      if (stopped) return;
      renderPreview();
      if (preview && preview.would_remove && preview.would_remove.length === 0
        && typeof ctx.toast === 'function') {
        ctx.toast('Убирать нечего — всё в пределах срока хранения', 'ok');
      }
    } catch (err) {
      showError(err, () => runClean());
    } finally {
      busy = false;
      if (!stopped) {
        // Кнопки панели созданы, пока шёл запрос (busy=true) — перерисовываем их
        // после ответа, иначе «Удалить» остаётся недоступной навсегда.
        renderSummary();
        renderPreview();
      }
    }
  }

  async function confirmClean(count, bytes) {
    const ok = await confirmDialog({
      title: 'Удалить безвозвратно?',
      text: 'Будет удалено ' + count + ' ' + filesText(count) + '. Отменить это нельзя: '
        + 'файлы приложения (диктовки, логи, бэкапы, временные копии) будут стёрты с диска. '
        + 'Ваши документы и Vault не затрагиваются.',
      confirmLabel: 'Удалить',
      danger: true,
    });
    if (!ok) return;
    busy = true;
    try {
      preview = await api.post('/api/storage/clean', { dry_run: false, confirm: true });
      if (stopped) return;
      renderPreview();
      if (typeof ctx.toast === 'function') {
        ctx.toast(preview && preview.message ? preview.message : 'Готово', 'ok');
      }
      // Отчёт об удалении остаётся на экране (что именно стёрли), а списки
      // категорий обновляются: человек видит и результат, и новое состояние.
      await load();
    } catch (err) {
      showError(err, () => load());
    } finally {
      busy = false;
      if (!stopped) {
        renderSummary();
        renderPreview();
      }
    }
  }

  await load();
  if (stopped) return {};
  return {
    unmount() {
      stopped = true;
      root.remove();
    },
  };
}

/**
 * Раздел «Хранилище» внутри «Настроек»: короткая сводка и переход на экран.
 * @param {HTMLElement} host
 * @param {object} ctx
 */
export async function mountStorageSection(host, ctx) {
  await ensureCss(VIEW_CSS);
  const api = ctx.api;
  let stopped = false;
  const box = h('div', { class: 'stor-brief' });
  host.appendChild(box);
  try {
    const data = await api.get('/api/storage');
    if (stopped) return;
    const rows = (data.categories || []).filter((row) => !isForeign(row));
    const will = rows.reduce((acc, row) => {
      const item = row.will_remove || {};
      acc.count += Number(item.count) || 0;
      acc.bytes += Number(item.bytes) || 0;
      return acc;
    }, { count: 0, bytes: 0 });
    box.replaceChildren(
      sectionHeader('Хранилище'),
      h('p', { class: 'small muted' },
        'Занято ' + (data.total_text || '0 Б') + '. ' + willRemoveText(will) + '.'),
      h('p', { class: 'small muted' },
        'Vault (Obsidian) и папка Hermes — только показ: их приложение не удаляет.'),
      h('div', { class: 'stor-brief__actions' },
        h('a', { class: 'btn btn--secondary btn--sm', href: '#/settings/storage' },
          icon('database', { size: 13 }), 'Открыть «Хранилище»')));
  } catch (err) {
    if (stopped) return;
    box.replaceChildren(sectionHeader('Хранилище'),
      h('p', { class: 'small muted' }, 'Не удалось прочитать размеры хранилища: '
        + String((err && err.message) || err)));
  }
  return { unmount() { stopped = true; box.remove(); } };
}

export async function mount(el, ctx) {
  return mountStorage(el, ctx);
}

export default mount;
