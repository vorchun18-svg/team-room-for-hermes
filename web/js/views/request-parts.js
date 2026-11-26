/**
 * request-parts.js — блоки страницы запроса: линейка этапов, предупреждения,
 * критик, дерево работ, файлы (до W26-D3 — «артефакты»), хронология.
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
  pathChip, sectionHeader, copyText, ensureCss, buildDialog,
} from '../ui.js';
import {
  pipeline, warningItems, criticInfo, artifactKindMeta, formatBytes,
  timelineMeta, timelineRows, criticRoundLabel, criticProgressLabel,
} from './request-parts-model.js';
import { toast, changed, postAction, button, lock, focusTreeTask } from './rp-shared.js';
import { renderTree } from './request-parts-tree.js';
import { createNotices } from './notice-state.js';
import { firstLine, blockHead, personName } from './request-util.js';
import { countNoun, FILES_RU } from '../text-ru.js';
import { createSection, sectionDefaults, artifactsSummaryChips } from './section-collapse.js';
// W29B-Q3: предпросмотр вложения (картинка, PDF, текст, таблица, Word/Excel/
// PowerPoint, голос с расшифровкой, OCR) — своя панель вместо окна только с текстом.
import { openAttachPreview, canPreview } from './attach-preview.js';
// W23-MG2 «Контроль моделей»: лента событий сторожа (остановлено/исправлено).
import { buildRequestFeed } from './model-guard.js';

export { renderTree };

// Совместимость: `canPreview` раньше жил здесь, им пользуются кнопки «Просмотр».
export { canPreview };

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
  // W20-FB: полоса этапов называется «Этапы» — имя «Ход работы» занял живой ход
  // работы на странице запроса (один смысл — одно место, в т.ч. для чтения с экрана).
  const card = h('section', { class: 'card rp-pipeline', 'data-rp': 'pipeline', 'aria-label': 'Этапы' });

  const head = sectionHeader('Этапы', {
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
 * W25.5: пометка «вне папки проекта» одной строкой — значок, подпись,
 * «подробнее». Полный путь и совет — только в раскрытом виде, чтобы длинный
 * путь не занимал первый экран и не рвал строку.
 * Экспортируется: тест проверяет и свёрнутый вид, и раскрытие.
 * @param {{path?: string, hint?: string, text?: string}} w
 * @returns {HTMLElement}
 */
export function outsideNotice(w) {
  const item = w && typeof w === 'object' ? w : {};
  const path = String(item.path || '').trim();
  const hint = String(item.hint || '').trim()
    || 'Файл открывается, но лучше держать итоги в папке проекта комнаты.';
  const more = h('button', {
    type: 'button', class: 'btn btn--link btn--sm rp-outside__more',
    'data-rp': 'outside-more', 'aria-expanded': 'false',
  }, 'подробнее');
  const box = h('div', { class: 'rp-outside__box', 'data-rp': 'outside-box', hidden: true },
    path ? h('div', { class: 'rp-outside__path small' }, h('span', { class: 'muted' }, 'Путь: '), path) : null,
    h('div', { class: 'rp-outside__hint small muted' }, hint));
  more.addEventListener('click', () => {
    const open = box.hasAttribute('hidden');
    if (open) box.removeAttribute('hidden'); else box.setAttribute('hidden', '');
    more.setAttribute('aria-expanded', open ? 'true' : 'false');
    more.textContent = open ? 'свернуть' : 'подробнее';
  });
  return h('div', {
    class: 'rp-outside', 'data-code': 'artifact_outside', role: 'status',
    title: 'Файл открывается, но лежит не в папке проекта',
  },
  icon('warning', { size: 14, className: 'rp-outside__icon' }),
  h('span', { class: 'rp-outside__text' }, 'вне папки проекта'),
  more, box);
}

/**
 * Баннеры предупреждений: detail.warnings, dispatcher.warning, карточки вне
 * состава комнаты. null, если предупреждений нет.
 *
 * B-24: плашку ошибки можно закрыть («Понятно»), попытки перечислены строками,
 * а закрытие помнится по «id карточки + номер попытки» — после новой попытки
 * плашка показывается снова (notice-state.js).
 *
 * @param {object} detail RequestDetail
 * @returns {HTMLElement|null}
 */
export function renderWarnings(detail) {
  const notices = createNotices({ requestId: String((detail && detail.id) || '') });
  const items = warningItems(detail).filter(
    (w) => !(w.stamp && notices.isDismissed('request', w.code, w.stamp)));
  if (!items.length) return null;

  const box = h('div', { class: 'stack stack--tight rp-warnings', role: 'status', 'data-rp': 'warnings' });
  for (const w of items) {
    // W25.5: «файл сохранён вне папки проекта» — ОДНА строка, а не баннер с
    // путём на всю ширину. Путь и совет открываются по «подробнее»: в свёрнутом
    // виде путь не показываем (он длинный и ничего не решает на первом экране).
    if (w.code === 'artifact_outside') {
      box.appendChild(outsideNotice(w));
      continue;
    }
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
    // Понятный список попыток: «Попытка 1 · 12:04 — превышено время ожидания».
    if (Array.isArray(w.attempts) && w.attempts.length) {
      const ul = h('ul', { class: 'rp-warning__attempts small' });
      for (const a of w.attempts) {
        const when = a.at ? fmtDateTime(a.at) : '';
        ul.appendChild(h('li', { class: 'rp-warning__attempt', title: a.reason },
          ['Попытка ' + String(a.n), when, a.reason].filter(Boolean).join(' · ')));
      }
      text.appendChild(ul);
    }
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
    // Закрытие плашки: одна кнопка «Понятно» (с крестиком), доступная с клавиатуры.
    if (w.stamp) {
      banner.appendChild(h('button', {
        type: 'button',
        class: 'btn btn--ghost btn--sm rp-warning__close',
        title: 'Понятно — больше не показывать эту ошибку',
        'aria-label': 'Понятно: скрыть сообщение об ошибке',
        onclick: () => {
          notices.dismiss('request', w.code, w.stamp);
          banner.remove();
          if (!box.childNodes.length) box.remove();
        },
      }, icon('close', { size: 12 }), 'Понятно'));
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
  meta.appendChild(h('span', { class: 'tnum' }, criticRoundLabel(c)));
  if (c.mode === 'until_approved' && c.state !== 'approved') {
    meta.appendChild(h('span', { class: 'rp-critic__sep', 'aria-hidden': 'true' }, '·'));
    meta.appendChild(h('span', {}, 'ответ выдаётся после одобрения'));
  }
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

  // Ход цикла (W13-E §9д): «раунд N, замечаний было K, закрыто M, осталось R».
  const progressLine = criticProgressLabel(c);
  if (progressLine) {
    card.appendChild(h('p', { class: 'rp-critic__progress small', 'data-rp': 'critic-progress' },
      icon('pulse', { size: 12 }), h('span', {}, progressLine),
      Number(c.progress && c.progress.closed) > 0
        ? h('span', { class: 'muted' }, ' · закрыто: ' + (c.progress.closed_items || [])
          .slice(0, 3).join('; '))
        : null));
  }
  // Страховки: пауза на лимите провайдера и стоп (нет прогресса / потолок раундов).
  if (c.state === 'paused') {
    card.appendChild(h('div', { class: 'banner banner--warn rp-critic__banner' },
      icon('clock', { size: 14 }),
      h('div', { class: 'banner__text' }, 'Лимит провайдера: цикл на паузе и продолжится сам. '
        + 'Ничего делать не нужно.')));
  }
  if (c.stop) {
    card.appendChild(h('div', { class: 'banner banner--warn rp-critic__banner', role: 'status' },
      icon('warning', { size: 14 }),
      h('div', { class: 'banner__text' },
        h('strong', {}, 'Цикл проверки остановлен. '),
        String(c.stop.message || ''),
        c.stop.needs_user === false ? '' : ' Ответ показан как есть — нужно ваше решение: '
          + 'отправить на доработку с замечанием или принять.')));
  }

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
  } else if (['failed', 'stalled', 'capped'].includes(c.state)) {
    actions.appendChild(button(c.stop ? 'Продолжить проверку' : 'Запустить снова', {
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

/* ───────────────────────────────────────────────────────────────── файлы */

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
function artifactPathChip(full, rel, ctx, roomId) {
  if (!rel) return pathChip(full, { onOpen: () => { openFile(ctx, full, roomId).catch(() => {}); } });
  const el = h('button', {
    class: 'chip chip--outline path-chip',
    type: 'button',
    title: full + ' — внутри папки проекта',
    'aria-label': 'Открыть: ' + full,
  }, icon('file-text', { size: 11 }), h('span', { class: 'path-chip__text mono', title: full }, rel));
  el.addEventListener('click', () => { openFile(ctx, full, roomId).catch(() => {}); });
  return el;
}

/* B-22 (WAVE19): файлы карточки открываются в контексте КОМНАТЫ — тогда сервер
 * разрешает её папку проекта и зеркало в Vault. Без `room` сервер искал файл
 * только в общих корнях и отвечал «файл не найден» на существующие файлы. */
/* W29B-Q3: «Просмотр» — своя панель для любого типа (картинка, PDF, текст,
 * таблица, Word/Excel/PowerPoint, голосовое с расшифровкой). Раньше здесь было
 * окно с одним текстом, а у остальных типов кнопки не было вовсе. */

/** URL файла карточки: путь + комната (папка проекта этой комнаты — в белом списке). */
export function artifactUrl(path, roomId) {
  const base = fileUrl(path);
  const room = String(roomId || '').trim();
  return room ? base + '&room=' + encodeURIComponent(room) : base;
}

/**
 * Серверное действие с файлом (W26-D3): `open` — «Открыть», `reveal` — «Показать
 * в папке». Адрес — `/api/files/launch` с комбинацией `mode`; сервер проверяет
 * путь по белым корням комнаты и сам вызывает ShellExecute/проводник.
 * @returns {Promise<boolean>} получилось ли
 */
async function fileAction(ctx, path, roomId, mode) {
  const p = String(path === null || path === undefined ? '' : path).trim();
  if (!p) return false;
  const failText = mode === 'reveal' ? FILES_RU.revealFailed : FILES_RU.openFailed;
  try {
    await ctx.api.post('/api/files/launch', { path: p, room: String(roomId || ''), mode });
    return true;
  } catch (err) {
    toast(ctx, (err && err.message) || failText, 'danger');
    return false;
  }
}

/**
 * «Открыть»: файл открывает программа по умолчанию (Word, Excel, просмотрщик).
 * Раньше здесь был `window.open` для превью и сломанный `reveal` для остальных —
 * внутри Hermes Desktop не работало ни то, ни другое (W26-D3, §7 «Ошибка 2»).
 * @param {object} ctx
 * @param {string} path
 * @param {string} roomId
 * @returns {Promise<boolean>}
 */
export async function openFile(ctx, path, roomId) {
  const ok = await fileAction(ctx, path, roomId, 'open');
  if (ok) toast(ctx, FILES_RU.openOk, 'ok');
  return ok;
}

/**
 * «Показать в папке»: проводник с выделением файла (`reveal` на сервере).
 * @returns {Promise<boolean>}
 */
export async function revealFile(ctx, path, roomId) {
  const ok = await fileAction(ctx, path, roomId, 'reveal');
  if (ok) toast(ctx, FILES_RU.revealOk, 'ok');
  return ok;
}

/**
 * «Просмотр» (W26-D3 → W29B-Q3): вложение в панели приложения.
 *
 * Никакого `window.open`: в окне Hermes Desktop новые окна молча блокируются.
 * Панель читает файл через `/api/attach/preview` (сервер проверяет путь и корни
 * комнаты) и умеет картинку, PDF, текст, таблицу, Word/Excel/PowerPoint и
 * голосовое с расшифровкой. Раньше здесь было окно только с текстом.
 *
 * @param {object} ctx
 * @param {string} path
 * @param {string} roomId
 * @param {string} [requestId] куда отправить расшифровку («Добавить в запрос»)
 * @returns {Promise<object|null>} панель или null, если путь пустой
 */
export async function openPreview(ctx, path, roomId, requestId) {
  const p = String(path === null || path === undefined ? '' : path).trim();
  if (!p) return null;
  const name = p.split(/[\\/]/).pop() || FILES_RU.previewTitle;
  return openAttachPreview({
    path: p,
    name,
    room: String(roomId || ''),
    requestId: String(requestId || ''),
    api: (ctx && ctx.api) || undefined,
    onChanged: () => { try { changed(ctx); } catch { /* странице нечего обновлять */ } },
  });
}

/**
 * Открыть путь файла в контексте КОМНАТЫ (W20-A1, §1.4): так же, как кнопки блока
 * файлов. Используется ссылками на локальные файлы внутри свода.
 * @param {object} ctx
 * @param {string} path
 * @param {string} roomId
 * @returns {Promise<boolean>}
 */
export function openArtifactPath(ctx, path, roomId) {
  return openFile(ctx, path, roomId);
}

/** Подпись состояния файла: что именно не так и что с этим делать (B-22, B-27). */
function artifactStateChip(artifact, state) {
  if (state === 'missing') {
    return h('span', {
      class: 'chip chip--warn',
      title: artifact.reason || 'Файл удалён или перемещён — возможно, его переименовали или перенесли.',
    }, 'Файл удалён или перемещён');
  }
  if (state === 'denied') {
    return h('span', {
      class: 'chip chip--warn',
      title: artifact.reason || 'Такой тип файла или имя приложение не открывает.',
    }, 'нельзя открыть');
  }
  if (state === 'outside') {
    return h('span', {
      class: 'chip chip--warn',
      title: 'Файл сохранён вне папки проекта: ' + String(artifact.path || ''),
    }, 'вне папки проекта');
  }
  return null;
}

/* W20-A1 (B-43): группы блока «Файлы» — итоговые первыми (§1.4). */
export const ARTIFACT_GROUPS = [
  { id: 'final', title: 'Итоговые', hint: 'письмо и краткая сводка' },
  { id: 'material', title: 'Материалы', hint: 'рабочие файлы задачи' },
  { id: 'service', title: 'Служебные', hint: 'служебные файлы приложения' },
];

/** Группа файла: `group` с сервера; у старых ответов — по имени файла. */
export function artifactGroup(artifact) {
  const raw = String((artifact && artifact.group) || '').toLowerCase();
  if (raw === 'final' || raw === 'material' || raw === 'service') return raw;
  const title = String((artifact && artifact.title) || '');
  const base = String((artifact && artifact.path) || '').split(/[\/]/).pop() || '';
  const name = (title + ' ' + base).toLowerCase();
  if (name.startsWith('_') && /\.(json|html?|log)$/.test(name)) return 'service';
  if (/итог|письмо|кратко|final|summary|letter/.test(name)) return 'final';
  return 'material';
}

/** Элемент-папка: `{kind:'folder', title, count, items}` (§1.4). */
export function isFolderNode(artifact) {
  return Boolean(artifact) && artifact.kind === 'folder' && Array.isArray(artifact.items);
}

/** Сколько файлов в списке с учётом содержимого свёрнутых папок. */
export function artifactsCount(items) {
  let total = 0;
  for (const item of Array.isArray(items) ? items : []) {
    total += isFolderNode(item) ? item.items.length : 1;
  }
  return total;
}

/** Свёрнутые папки запоминают своё состояние между перерисовками страницы. */
const openFolders = new Set();

/** Время изменения файла: «4 ноября, 09:15». */
export function artifactTimeLabel(mtime) {
  const n = Number(mtime);
  if (!isFinite(n) || n <= 0) return '';
  return fmtDateTime(n);
}

/** Строка под файлом: время изменения и размер (B-43). */
function artifactMetaLine(artifact) {
  const time = artifactTimeLabel(artifact && artifact.mtime);
  const size = artifact && artifact.size !== null && artifact.size !== undefined
    ? formatBytes(artifact.size) : '';
  const parts = [time, size].filter(Boolean);
  if (!parts.length) return null;
  return h('div', { class: 'rp-art__info small muted tnum' },
    time ? h('span', { class: 'rp-art__time', title: 'Изменён: ' + time }, time) : null,
    time && size ? h('span', { class: 'rp-art__dot', 'aria-hidden': 'true' }, ' · ') : null,
    size ? h('span', { class: 'rp-art__size' }, size) : null);
}

/** Бейдж «новее свода»: файл изменён позже последней версии свода (B-43). */
function artifactNewerChip(artifact) {
  if (!artifact || !artifact.newer_than_svod) return null;
  return h('span', {
    class: 'chip chip--info rp-art__newer',
    title: 'Файл изменён позже последней версии свода — в своде его может ещё не быть',
  }, 'новее свода');
}

/** Одна строка файла: значок, название, вид, состояние, путь, время, размер, действия. */
function artifactRow(artifact, ctx, roomId, projectDir, opts = {}) {
  const meta = artifactKindMeta(artifact.kind);
  const path = String(artifact.path);
  const state = String(artifact.state || (artifact.exists === false ? 'missing' : 'ok'));
  const exists = artifact.exists !== false && state !== 'missing' && state !== 'denied';
  const outside = state === 'outside' || artifact.outside_project === true;
  const rel = relativeToDir(path, projectDir);
  const title = String(artifact.title || path.split(/[\\/]/).pop() || 'Файл');

  const li = h('li', {
    class: ['rp-art', exists ? '' : 'rp-art--missing', opts.nested ? 'rp-art--nested' : '',
      outside ? 'rp-art--outside' : ''],
    'data-key': path,
  });
  const thumb = exists && artifact.kind === 'image'
    ? h('button', {
      class: 'rp-art__thumb', type: 'button', title: 'Открыть картинку',
      onclick: () => { openFile(ctx, path, roomId).catch(() => {}); },
    }, h('img', { src: artifactUrl(path, roomId), alt: title, loading: 'lazy' }))
    : h('span', { class: 'rp-art__icon', 'aria-hidden': 'true' }, icon(meta.icon, { size: 15 }));

  const main = h('div', { class: 'rp-art__main' },
    h('div', { class: 'rp-art__title-row' },
      h('span', { class: 'rp-art__title', title }, title),
      h('span', { class: 'chip chip--muted rp-art__kind', title: meta.label }, meta.label),
      artifactNewerChip(artifact),
      artifactStateChip(artifact, state)),
    // W25.5: совет «договоритесь хранить файлы в папке проекта» больше не
    // повторяется у каждого файла — он один в строке «вне папки проекта»
    // (renderWarnings → outsideNotice) и открывается по «подробнее».
    h('div', { class: 'rp-art__path' }, artifactPathChip(path, rel, ctx, roomId)));

  const acts = h('div', { class: 'rp-actions rp-art__actions' });
  if (exists) {
    // W26-D3: «Открыть» — файл в программе по умолчанию (серверное действие).
    // Раньше здесь для превью-типов был `window.open` (в Hermes Desktop новые
    // окна блокируются), а для остальных — проводник вместо самого файла.
    acts.appendChild(button(FILES_RU.open, {
      variant: 'secondary', size: 'sm', icon: 'open-preview', title: FILES_RU.openHint,
      onclick: () => { openFile(ctx, path, roomId).catch(() => {}); },
    }));
    if (canPreview(path)) {
      acts.appendChild(button(FILES_RU.preview, {
        variant: 'ghost', size: 'sm', icon: 'eye', title: FILES_RU.previewHint,
        onclick: () => { openPreview(ctx, path, roomId, opts.requestId).catch(() => {}); },
      }));
    }
  }
  acts.appendChild(button(FILES_RU.copyPath, {
    variant: 'ghost', size: 'sm', icon: 'copy', title: FILES_RU.copyPathHint,
    onclick: async () => {
      const ok = await copyText(path);
      toast(ctx, ok ? 'Путь скопирован' : 'Не удалось скопировать путь', ok ? 'ok' : 'danger');
    },
  }));
  if (exists) {
    acts.appendChild(button(FILES_RU.reveal, {
      variant: 'ghost', size: 'sm', icon: 'folder-opened', title: FILES_RU.revealHint,
      onclick: () => { revealFile(ctx, path, roomId).catch(() => {}); },
    }));
  }

  li.appendChild(thumb);
  li.appendChild(main);
  const infoLine = artifactMetaLine(artifact);
  if (infoLine) li.appendChild(infoLine);
  li.appendChild(acts);
  return li;
}

/** Свёрнутая папка: заголовок со счётчиком и файлы внутри (B-43). */
function folderRow(folder, ctx, roomId, projectDir, opts = {}) {
  const key = String(folder.folder || folder.title || folder.path || '');
  const details = h('details', {
    class: 'rp-artifacts__folder', 'data-folder': key,
    'data-key': 'folder:' + key,
  });
  if (openFolders.has(key)) details.open = true;
  details.addEventListener('toggle', () => {
    if (details.open) openFolders.add(key); else openFolders.delete(key);
  });
  const count = Array.isArray(folder.items) ? folder.items.length : Number(folder.count || 0);
  details.appendChild(h('summary', { class: 'rp-artifacts__folder-sum' },
    h('span', { class: 'rp-artifacts__folder-caret', 'aria-hidden': 'true' },
      icon('chevron-right', { size: 12 })),
    h('span', { class: 'rp-artifacts__folder-icon', 'aria-hidden': 'true' },
      icon('folder', { size: 14 })),
    h('span', { class: 'rp-artifacts__folder-title', title: key }, String(folder.title || key)),
    h('span', { class: 'chip chip--muted tnum' }, countNoun(count, ['файл', 'файла', 'файлов'])),
    folder.newer_than_svod ? artifactNewerChip(folder) : null));
  const sublist = h('ul', { class: 'rp-artifacts__sublist' });
  for (const item of Array.isArray(folder.items) ? folder.items : []) {
    sublist.appendChild(artifactRow(item, ctx, roomId, projectDir,
      { nested: true, requestId: (opts || {}).requestId }));
  }
  details.appendChild(sublist);
  return h('li', { class: 'rp-artifacts__folder-item' }, details);
}

/**
 * «Файлы»: файлы работы — «Открыть», «Просмотр», «Показать в папке», путь;
 * картинки показываются превью. Группы «Итоговые / Материалы / Служебные»,
 * подпапки — свёрнутыми группами, у файла время изменения и размер (W20-A1, B-43).
 * null, если файлов нет.
 * @param {object} detail RequestDetail
 * @param {object} ctx
 * @param {{projectDir?: string, room?: string}} [opts]
 * @returns {HTMLElement|null}
 */
export function renderArtifacts(detail, ctx, opts = {}) {
  const o = opts || {};
  const projectDir = String(o.projectDir || o.project_dir || '').trim();
  const roomId = String((detail && detail.room) || o.room || '').trim();
  // W29B-Q3: «Добавить расшифровку в запрос» — сообщением в этот запрос.
  const requestId = String((detail && detail.id) || o.requestId || '').trim();
  const items = (detail && Array.isArray(detail.artifacts) ? detail.artifacts : [])
    .filter((a) => a && typeof a === 'object' && (a.path || isFolderNode(a)));
  if (!items.length) return null;

  const defaults = sectionDefaults(detail);

  /**
   * Строки файлов строятся только при раскрытии блока (У-1): при 200 файлах
   * это 200 строк с путями, кнопками и картинками. Счётчик в заголовке считается
   * по данным и без строк — «23 файла · есть новее свода» видно и в свёрнутом виде.
   */
  const buildGroups = () => {
    const buckets = { final: [], material: [], service: [] };
    for (const item of items) buckets[artifactGroup(item)].push(item);
    const nodes = [];
    for (const group of ARTIFACT_GROUPS) {
      const list = buckets[group.id];
      if (!list.length) continue;
      const section = h('div', { class: 'rp-artifacts__group', 'data-group': group.id });
      section.appendChild(h('div', { class: 'rp-artifacts__group-head' },
        h('h3', { class: 'rp-artifacts__group-title', title: group.hint }, group.title),
        h('span', { class: 'chip chip--muted tnum' },
          countNoun(artifactsCount(list), ['файл', 'файла', 'файлов']))));
      const ul = h('ul', { class: 'rp-artifacts__list' });
      for (const item of list) {
        ul.appendChild(isFolderNode(item)
          ? folderRow(item, ctx, roomId, projectDir, { requestId })
          : artifactRow(item, ctx, roomId, projectDir, { requestId }));
      }
      section.appendChild(ul);
      nodes.push(section);
    }
    return nodes;
  };

  const sec = createSection({
    // Ключ состояния остался 'artifacts': это внутреннее имя (память свёрнутости
    // и адрес блока), человек видит только «Файлы» (W26-D3).
    key: 'artifacts',
    requestId: String((detail && detail.id) || ''),
    title: FILES_RU.title,
    hint: FILES_RU.titleHint,
    defaultOpen: defaults.artifacts,
    className: 'rp-artifacts',
    attrs: { 'data-rp': 'artifacts' },
    summary: artifactsSummaryChips({ artifacts: items }),
    content: buildGroups,
  });
  return sec.element;
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
 * «Хронология»: вертикальная лента событий; свёрнута до последних 5 (R5 §7.2).
 * null, если событий нет.
 * @param {object} detail RequestDetail
 * @returns {HTMLElement|null}
 */
export function renderTimeline(detail) {
  const events = Array.isArray(detail && detail.timeline) ? detail.timeline.filter(Boolean) : [];
  if (!events.length) return null;

  const card = h('section', {
    class: 'card rp-timeline', 'data-rp': 'timeline',
    // У-1: хронология остаётся развёрнутой — стрелки у неё нет (решение Михаила).
    'data-section': 'timeline', 'data-collapsible': 'false',
  });
  card.appendChild(blockHead('Хронология', countNoun(events.length, ['событие', 'события', 'событий'])));
  const list = h('ol', { class: 'rp-timeline__list' });
  const more = h('button', {
    class: 'btn btn--ghost btn--sm rp-timeline__more',
    type: 'button',
  }, `Показать все: ${countNoun(events.length, ['событие', 'события', 'событий'])}`);
  const render = (expanded) => {
    list.textContent = '';
    for (const row of timelineRows(detail, { limit: 5, expanded })) list.appendChild(timelineItem(row));
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
  if (events.length > 5) card.appendChild(more);
  return card;
}

/**
 * «Контроль моделей» (W23-MG2): лента событий — что остановили, что исправили
 * до запуска, сколько это сэкономило. Своё место на странице запроса: человек
 * видит вмешательство сторожа, а не только итог работы.
 *
 * @param {object} detail RequestDetail (`model_guard[]`)
 * @param {object} [ctx]
 * @returns {HTMLElement|null} null — событий не было, пустую карточку не рисуем
 */
export function renderModelGuard(detail, ctx) {
  if (!detail || !Array.isArray(detail.model_guard) || !detail.model_guard.length) return null;
  return buildRequestFeed(detail, ctx, { catalog: (ctx && ctx.catalog) || null });
}

export default {
  renderPipeline, renderWarnings, renderCritic, renderTree, renderArtifacts, renderTimeline,
  renderModelGuard,
};
