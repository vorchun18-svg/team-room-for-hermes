/**
 * request-parts.js — блоки страницы запроса: линейка этапов, предупреждения,
 * критик, дерево работ, артефакты, хронология.
 *
 * Все функции возвращают готовый Element (или null, если показывать нечего)
 * и монтируются страницей запроса (request.js). Данные — только из detail,
 * действия — только через ctx.api. Никакого innerHTML: текст идёт узлами,
 * разметка агента — только через ui.mdBlock().
 *
 * Дерево работ живёт в request-parts-tree.js и реэкспортируется отсюда,
 * чтобы страница импортировала один модуль.
 */

import { h, timeAgo, fmtDateTime, fromCount, plainTerms } from '../util.js';
import { fileUrl } from '../api.js';
import { icon } from '../icons.js';
import {
  pathChip, sectionHeader, openPath, copyText, ensureCss,
} from '../ui.js';
import {
  pipeline, warningItems, criticInfo, artifactKindMeta, formatBytes,
  timelineMeta, timelineRows,
} from './request-parts-model.js';
import { toast, changed, postAction, button, lock, focusTreeTask } from './rp-shared.js';
import { renderTree } from './request-parts-tree.js';
import { firstLine, blockHead, personName } from './request-util.js';
import { countNoun } from '../text-ru.js';

export { renderTree };

// Свои стили — отдельным файлом, лениво (ui.ensureCss дедуплицирует по href).
ensureCss('css/view-request-parts.css');

/* ─────────────────────────────────────────────────────────── линейка этапов */

const PIECE_ICON = { done: 'check', active: null, paused: 'lock', failed: 'error', todo: null };

/**
 * Линейка этапов «Разбор → Работа → Критик → Сводка → Готово».
 * @param {object} detail RequestDetail
 * @returns {HTMLElement}
 */
export function renderPipeline(detail) {
  const p = pipeline(detail);
  const card = h('section', { class: 'card rp-pipeline', 'data-rp': 'pipeline', 'aria-label': 'Ход работы' });

  const head = sectionHeader('Ход работы', {
    actions: [
      h('span', { class: 'chip chip--muted tnum rp-pipeline__count', title: `Пройдено этапов: ${p.done} из ${p.total}` },
        fromCount(p.done, p.total, ['этап', 'этапа', 'этапов'])),
    ],
  });
  card.appendChild(head);

  const bar = h('div', { class: ['progress', p.tone === 'ok' ? 'progress--ok' : p.tone === 'danger' ? 'progress--danger' : p.tone === 'warn' ? 'progress--warn' : ''] },
    h('div', { class: 'progress__bar', style: { width: `${p.percent}%` } }));
  bar.setAttribute('aria-hidden', 'true');
  card.appendChild(bar);

  const list = h('ol', { class: 'rp-pipe', style: { '--rp-steps': String(p.total) } });
  p.stages.forEach((stage, i) => {
    const li = h('li', {
      class: ['rp-pipe__step', 'rp-pipe__step--' + stage.state],
      'aria-current': stage.id === p.currentId ? 'step' : undefined,
      'data-stage': stage.id,
    });
    const dot = h('span', { class: 'rp-pipe__dot', 'aria-hidden': 'true' },
      icon(PIECE_ICON[stage.state] || stage.icon, { size: 12 }));
    li.appendChild(dot);
    li.appendChild(h('span', { class: 'rp-pipe__label' }, stage.title));
    li.appendChild(h('span', { class: 'rp-pipe__hint small' }, stage.hint));
    if (i === p.stages.length - 1) li.classList.add('rp-pipe__step--last');
    list.appendChild(li);
  });
  card.appendChild(list);

  card.appendChild(h('div', { class: ['rp-pipeline__now', p.tone === 'danger' ? 'rp-pipeline__now--danger' : p.tone === 'warn' ? 'rp-pipeline__now--warn' : ''] },
    icon(p.tone === 'danger' ? 'error' : p.tone === 'warn' ? 'lock' : p.tone === 'ok' ? 'check' : 'pulse', { size: 12 }),
    h('span', {}, p.nowText)));
  return card;
}

/* ────────────────────────────────────────────────────────── предупреждения */

/**
 * Баннеры предупреждений: detail.warnings, dispatcher.warning, карточки вне
 * состава комнаты. null, если предупреждений нет.
 * @param {object} detail RequestDetail
 * @returns {HTMLElement|null}
 */
export function renderWarnings(detail) {
  const items = warningItems(detail);
  if (!items.length) return null;

  const box = h('div', { class: 'stack stack--tight rp-warnings', role: 'status', 'data-rp': 'warnings' });
  for (const w of items) {
    const banner = h('div', {
      class: ['banner', w.bannerClass, 'rp-warning'],
      role: w.tone === 'danger' ? 'alert' : undefined,
      'data-code': w.code,
    });
    banner.appendChild(icon(w.tone === 'danger' ? 'error' : w.tone === 'info' ? 'info' : 'warning', { size: 14 }));
    const text = h('div', { class: 'banner__text rp-warning__text' },
      h('div', { class: 'rp-warning__title' }, w.title),
      h('div', { class: 'rp-warning__body' }, plainTerms(w.text)));
    if (w.hint) text.appendChild(h('div', { class: 'rp-warning__hint small' }, w.hint));
    banner.appendChild(text);

    if (w.code === 'outside_room' && w.taskId) {
      banner.appendChild(button('К дереву работ', {
        variant: 'secondary', size: 'sm',
        onclick: () => {
          if (focusTreeTask(w.taskId)) return;
          const treeCard = typeof document !== 'undefined' ? document.querySelector('[data-rp-tree]') : null;
          if (treeCard && typeof treeCard.scrollIntoView === 'function') {
            treeCard.scrollIntoView({ block: 'start', behavior: 'smooth' });
          }
        },
      }));
    }
    box.appendChild(banner);
  }
  return box;
}

/* ──────────────────────────────────────────────────────────────── критик */

function issueList(issues) {
  const ul = h('ul', { class: 'rp-issues' });
  issues.slice(0, 10).forEach((issue, i) => {
    const cells = [
      ['Где', issue.where, 'rp-issue__cell--where'],
      ['Проблема', issue.problem, 'rp-issue__cell--problem'],
      ['Исправить', issue.fix, 'rp-issue__cell--fix'],
    ];
    const li = h('li', { class: 'rp-issue' });
    li.appendChild(h('span', { class: 'rp-issue__num tnum', 'aria-hidden': 'true' }, String(i + 1)));
    for (const [label, value, cls] of cells) {
      li.appendChild(h('span', { class: ['rp-issue__cell', cls] },
        h('span', { class: 'rp-issue__label' }, label),
        h('span', { class: 'rp-issue__value' }, value ? String(value) : '—')));
    }
    ul.appendChild(li);
  });
  return ul;
}

/**
 * Карточка «Проверка критика». null — если проверки у запроса нет.
 * @param {object} detail RequestDetail
 * @param {object} ctx
 * @returns {HTMLElement|null}
 */
export function renderCritic(detail, ctx) {
  const c = criticInfo(detail);
  if (c.state === 'off' || !c.enabled) return null;

  const card = h('section', { class: 'card rp-critic', 'data-rp': 'critic', 'data-state': c.state });
  // Один чип: терминальные состояния показываем вердиктом, живые — состоянием.
  const chipLabel = c.verdictLabel || c.label;
  const chipTone = c.verdictLabel ? (c.verdict === 'approve' ? 'ok' : 'warn') : c.tone;
  const chip = h('span', { class: ['chip', 'chip--' + (chipTone === 'muted' ? 'muted' : chipTone)] },
    c.active ? h('span', { class: 'chip__dot chip__dot--pulse' }) : null,
    h('span', {}, chipLabel));
  card.appendChild(sectionHeader('Проверка критика', { actions: [chip] }));

  const meta = h('div', { class: 'row row--wrap row--tight rp-critic__meta small muted' });
  meta.appendChild(h('span', { class: 'tnum' }, `раунд ${Math.max(c.round, 0)} из ${c.maxRounds}`));
  if (c.taskId) {
    // В тексте — человекопонятное название задачи, номер — в подсказке и «Копировать» (D6).
    const node = (Array.isArray(detail && detail.tree) ? detail.tree : []).find((n) => n && n.id === c.taskId);
    const label = node && node.title ? firstLine(String(node.title), 48) : 'проверка свода';
    meta.appendChild(h('span', { class: 'rp-critic__sep', 'aria-hidden': 'true' }, '·'));
    const link = h('button', {
      class: 'btn btn--link btn--sm rp-critic__link',
      type: 'button',
      title: `Номер задачи: ${c.taskId} · нажмите, чтобы найти в дереве работ`,
      onclick: () => {
        if (!focusTreeTask(c.taskId)) toast(ctx, 'Задача критика — в дереве работ ниже', 'info');
      },
    }, `Задача критика: «${label}»`);
    meta.appendChild(link);
    meta.appendChild(h('button', {
      class: 'btn btn--ghost btn--sm btn--icon rp-critic__copy',
      type: 'button',
      title: 'Скопировать номер задачи для разработчика',
      'aria-label': 'Скопировать номер задачи',
      onclick: async () => {
        const ok = await copyText(String(c.taskId));
        toast(ctx, ok ? 'Номер задачи скопирован' : 'Не удалось скопировать номер', ok ? 'ok' : 'danger');
      },
    }, icon('copy', { size: 12 })));
  }
  card.appendChild(meta);
  if (c.note) card.appendChild(h('p', { class: 'rp-critic__note small' }, c.note));

  if (c.issues.length) {
    card.appendChild(h('div', { class: 'rp-critic__issues-title' },
      `Замечания: ${c.issues.length}`,
      h('span', { class: 'muted small rp-critic__issues-hint' }, ' где · в чём проблема · что исправить')));
    card.appendChild(issueList(c.issues));
  }

  const ready = detail && detail.synthesis && Number(detail.synthesis.version) > 0;
  const actions = h('div', { class: 'rp-critic__actions row row--wrap' });
  if (c.state === 'waiting' && ready) {
    actions.appendChild(button('Запустить критика', {
      variant: 'secondary', size: 'sm', icon: 'shield',
      onclick: (ev) => runCritique(detail, ctx, ev && ev.currentTarget),
    }));
  } else if (c.state === 'failed') {
    actions.appendChild(button('Запустить снова', {
      variant: 'secondary', size: 'sm', icon: 'refresh',
      onclick: (ev) => runCritique(detail, ctx, ev && ev.currentTarget),
    }));
  }
  if (actions.childNodes.length) card.appendChild(actions);
  return card;
}

async function runCritique(detail, ctx, btn) {
  const holder = btn ? btn.closest('.rp-critic') : null;
  if (holder) lock(holder, true);
  try {
    await postAction(ctx, detail.id, { action: 'critique' });
    toast(ctx, 'Критик запущен — проверка займёт время', 'ok');
    changed(ctx);
  } catch (err) {
    toast(ctx, (err && err.message) || 'Не удалось запустить критика', 'danger');
    if (holder) lock(holder, false);
  }
}

/* ────────────────────────────────────────────────────────────── артефакты */

/**
 * Путь внутри папки проекта — относительно неё (Д2 WAVE8-RULES).
 * Сравнение без регистра, с обоими разделителями; вне папки — `''`.
 * @param {string} path полный путь файла
 * @param {string} dir папка проекта
 * @returns {string} «Отчёты/итог.md» или ''
 */
export function relativeToDir(path, dir) {
  const p = String(path === null || path === undefined ? '' : path).replace(/[\\/]+$/, '');
  const d = String(dir === null || dir === undefined ? '' : dir).replace(/[\\/]+$/, '');
  if (!p || !d) return '';
  const flat = (s) => s.replace(/\//g, '\\').toLowerCase();
  const np = flat(p);
  const nd = flat(d);
  if (np === nd) return '';
  if (!np.startsWith(nd + '\\')) return '';
  return p.slice(d.length + 1).replace(/^[\\/]+/, '');
}

/** Чип пути: внутри папки проекта — короткий относительный, полный в title. */
function artifactPathChip(full, rel) {
  if (!rel) return pathChip(full);
  const el = h('button', {
    class: 'chip chip--outline path-chip',
    type: 'button',
    title: full + ' — внутри папки проекта',
    'aria-label': 'Открыть: ' + full,
  }, icon('file-text', { size: 11 }), h('span', { class: 'path-chip__text mono' }, rel));
  el.addEventListener('click', () => { openPath(full).catch(() => {}); });
  return el;
}

/**
 * «Артефакты»: файлы работы с открытием, копированием пути и проводником;
 * картинки показываются превью. null, если файлов нет.
 * Пути внутри папки проекта комнаты показываются относительно неё.
 * @param {object} detail RequestDetail
 * @param {object} ctx
 * @param {{projectDir?: string}} [opts]
 * @returns {HTMLElement|null}
 */
export function renderArtifacts(detail, ctx, opts = {}) {
  const o = opts || {};
  const projectDir = String(o.projectDir || o.project_dir || '').trim();
  const items = (detail && Array.isArray(detail.artifacts) ? detail.artifacts : [])
    .filter((a) => a && typeof a === 'object' && a.path);
  if (!items.length) return null;

  const card = h('section', { class: 'card rp-artifacts', 'data-rp': 'artifacts' });
  card.appendChild(blockHead('Артефакты', countNoun(items.length, ['файл', 'файла', 'файлов'])));
  const list = h('ul', { class: 'rp-artifacts__list' });

  for (const artifact of items) {
    const meta = artifactKindMeta(artifact.kind);
    const exists = artifact.exists !== false;
    const path = String(artifact.path);
    const rel = relativeToDir(path, projectDir);
    const title = String(artifact.title || path.split(/[\\/]/).pop() || 'Файл');

    const li = h('li', { class: ['rp-art', exists ? '' : 'rp-art--missing'] });
    const thumb = exists && artifact.kind === 'image'
      ? h('button', {
        class: 'rp-art__thumb', type: 'button', title: 'Открыть картинку',
        onclick: () => { openPath(path); },
      }, h('img', { src: fileUrl(path), alt: title, loading: 'lazy' }))
      : h('span', { class: 'rp-art__icon', 'aria-hidden': 'true' }, icon(meta.icon, { size: 15 }));

    const main = h('div', { class: 'rp-art__main' },
      h('div', { class: 'rp-art__title-row' },
        h('span', { class: 'rp-art__title' }, title),
        h('span', { class: 'chip chip--muted rp-art__kind' }, meta.label),
        exists ? null : h('span', { class: 'chip chip--warn' }, 'файл не найден')),
      h('div', { class: 'rp-art__path' }, artifactPathChip(path, rel)));

    const info = h('div', { class: 'rp-art__info small muted tnum' },
      artifact.size === null || artifact.size === undefined ? '' : formatBytes(artifact.size));
    const acts = h('div', { class: 'rp-actions rp-art__actions' });
    if (exists) {
      acts.appendChild(button('Открыть', {
        variant: 'secondary', size: 'sm', icon: 'open-preview',
        onclick: () => { openPath(path); },
      }));
    }
    acts.appendChild(button('Копировать путь', {
      variant: 'ghost', size: 'sm', icon: 'copy',
      onclick: async () => {
        const ok = await copyText(path);
        toast(ctx, ok ? 'Путь скопирован' : 'Не удалось скопировать путь', ok ? 'ok' : 'danger');
      },
    }));
    if (exists) {
      acts.appendChild(button('В проводнике', {
        variant: 'ghost', size: 'sm', icon: 'folder-opened',
        onclick: async () => {
          try {
            await ctx.api.post('/api/open', { path });
            toast(ctx, 'Открыто в проводнике', 'ok');
          } catch (err) {
            toast(ctx, (err && err.message) || 'Не удалось открыть проводник', 'danger');
          }
        },
      }));
    }

    li.appendChild(thumb);
    li.appendChild(main);
    li.appendChild(info);
    li.appendChild(acts);
    list.appendChild(li);
  }
  card.appendChild(list);
  return card;
}

/* ───────────────────────────────────────────────────────────── хронология */

function timelineItem(row) {
  const ev = row.event;
  const meta = timelineMeta(ev.kind);
  const rawActor = String(ev.actor || '');
  const who = ev.actor === 'mikhail' ? 'Михаил' : ev.actor === 'system' ? 'Система' : (personName(ev.actor) || '—');
  const li = h('li', { class: ['rp-event', row.newest ? 'rp-event--newest' : ''] });
  li.appendChild(h('span', { class: 'rp-event__dot', 'aria-hidden': 'true' }, icon(meta.icon, { size: 11 })));
  const body = h('div', { class: 'rp-event__body' },
    h('div', { class: 'rp-event__head' },
      h('span', { class: 'rp-event__label' }, meta.label),
      h('span', { class: 'small muted rp-event__actor', title: rawActor || null }, who),
      h('span', { class: 'small muted tnum rp-event__time', title: fmtDateTime(ev.at) }, timeAgo(ev.at))));
  if (ev.text) body.appendChild(h('div', { class: 'rp-event__text' }, plainTerms(String(ev.text))));
  li.appendChild(body);
  return li;
}

/**
 * «Хронология»: вертикальная лента событий; свёрнута до последних 6.
 * null, если событий нет.
 * @param {object} detail RequestDetail
 * @returns {HTMLElement|null}
 */
export function renderTimeline(detail) {
  const events = Array.isArray(detail && detail.timeline) ? detail.timeline.filter(Boolean) : [];
  if (!events.length) return null;

  const card = h('section', { class: 'card rp-timeline', 'data-rp': 'timeline' });
  card.appendChild(blockHead('Хронология', countNoun(events.length, ['событие', 'события', 'событий'])));
  const list = h('ol', { class: 'rp-timeline__list' });
  const more = h('button', {
    class: 'btn btn--ghost btn--sm rp-timeline__more',
    type: 'button',
  }, `Показать все: ${countNoun(events.length, ['событие', 'события', 'событий'])}`);
  const render = (expanded) => {
    list.textContent = '';
    for (const row of timelineRows(detail, { limit: 6, expanded })) list.appendChild(timelineItem(row));
    more.textContent = expanded ? 'Свернуть до последних' : `Показать все: ${countNoun(events.length, ['событие', 'события', 'событий'])}`;
    more.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  };
  let expanded = false;
  more.addEventListener('click', () => {
    expanded = !expanded;
    render(expanded);
  });
  render(false);
  card.appendChild(list);
  if (events.length > 6) card.appendChild(more);
  return card;
}

export default {
  renderPipeline, renderWarnings, renderCritic, renderTree, renderArtifacts, renderTimeline,
};
