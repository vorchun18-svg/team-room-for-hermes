/**
 * request-parts-tree.js — «Дерево работ» страницы запроса: строки карточек с
 * отступом по глубине, раскрытием (сводка, комментарии, попытки, ошибка) и
 * действиями (ответить / повторить / переназначить / на доработку).
 *
 * Экспортируется через request-parts.js: страница запроса импортирует
 * renderTree оттуда, сигнатура фиксирована — (detail, ctx, {onChanged}).
 */

import { h, fmtDuration, timeAgo, fmtDateTime, plainTerms } from '../util.js';
import { fileUrl } from '../api.js';
import { icon } from '../icons.js';
import {
  statusChip, roleChip, mdBlock, emptyState, promptDialog, copyText,
} from '../ui.js';
import { buildTree, nodeActions, errorReason,
         failureStamp, attemptRows, cardFailed, failureMessage,
         guardAttempts, attemptTotal, sessionOf } from './request-parts-model.js';
import { createTreeState, problemSignature } from './request-tree-state.js';
import { createNotices, createMarks } from './notice-state.js';
import { createRetryButton } from './retry-button.js';
import { toast, changed, postAction, button, lock } from './rp-shared.js';
import { personName, profileName, devDetails } from './request-util.js';
import { participantLabel } from '../participant-label.js';
import { createSection, sectionDefaults, treeSummaryChips } from './section-collapse.js';
// W23-MG2 «Контроль моделей»: кнопки управления работой карточки и строка «Остановлено…».
import { cardControls, stoppedLine } from './model-guard.js';

/* ─────────────────────────────────────────────────────────── дерево работ */

function nodeDuration(node) {
  const started = Number(node && node.started_at) || 0;
  if (!started) return '';
  const finished = Number(node && node.finished_at) || 0;
  const live = ['running', 'review', 'ready'].includes(String(node.status || ''));
  const end = finished || (live ? Date.now() / 1000 : 0);
  if (!end || end < started) return '';
  const text = fmtDuration(end - started);
  return finished ? text : `${text} идёт`;
}

function rosterFromRoom(room) {
  const out = [];
  const seen = new Set();
  const push = (profile, role) => {
    const p = profile ? String(profile) : '';
    if (!p || seen.has(p)) return;
    seen.add(p);
    out.push({ profile: p, role: role || 'worker' });
  };
  if (!room || typeof room !== 'object') return out;
  push(room.orchestrator, 'orchestrator');
  push(room.critic, 'critic');
  for (const m of Array.isArray(room.members) ? room.members : []) {
    if (m && typeof m === 'object') push(m.profile, m.role);
    else push(m, 'worker');
  }
  return out;
}

const rosterCache = new Map();

/** Состав комнаты: из ctx.room, иначе GET /api/rooms/{id} (в сводке нет members). */
async function rosterFor(ctx, detail) {
  const fromStore = rosterFromRoom(ctx && ctx.room);
  if (fromStore.length) return fromStore;
  const roomId = String((detail && detail.room) || (ctx && ctx.room && ctx.room.id) || '');
  if (!roomId) return [];
  if (rosterCache.has(roomId)) return rosterCache.get(roomId);
  try {
    const room = await ctx.api.get(`/api/rooms/${encodeURIComponent(roomId)}`);
    const list = rosterFromRoom(room);
    rosterCache.set(roomId, list);
    return list;
  } catch {
    return [];
  }
}

const ROLE_RU = { orchestrator: 'руководитель комнаты', critic: 'критик', worker: 'исполнитель', observer: 'наблюдатель' };

function reassignPanel(node, ctx, detail, onDone) {
  const panel = h('div', { class: 'rp-actions__panel' });
  const select = h('select', { class: 'select rp-actions__select', 'aria-label': 'Новый исполнитель' });
  const apply = button('Применить', { variant: 'primary', size: 'sm', disabled: true });
  const cancel = button('Отмена', { variant: 'ghost', size: 'sm' });
  panel.appendChild(h('span', { class: 'rp-actions__label small muted' }, 'Переназначить на'));
  panel.appendChild(select);
  panel.appendChild(apply);
  panel.appendChild(cancel);

  cancel.addEventListener('click', () => { onDone(); });

  const fill = (list) => {
    select.textContent = '';
    const current = node.assignee ? String(node.assignee) : '';
    if (current && !list.some((m) => m.profile === current)) {
      select.appendChild(h('option', { value: current }, `${current} — вне состава`));
    }
    for (const m of list) {
      // WAVE13 §11: в списке — имя и должность, техническое имя профиля в подсказке.
      const suffix = m.role && m.role !== 'worker' ? ` · ${ROLE_RU[m.role] || m.role}` : '';
      const label = participantLabel(m.profile, { member: m, role: m.role });
      select.appendChild(h('option', { value: m.profile, title: 'Профиль Hermes: ' + m.profile },
        `${label.title}${suffix}`));
    }
    select.value = current || (list[0] ? list[0].profile : '');
    apply.disabled = !select.value || select.value === current;
  };

  const initial = rosterFromRoom(ctx && ctx.room);
  if (initial.length) {
    fill(initial);
  } else {
    select.appendChild(h('option', {}, 'Загрузка состава…'));
    rosterFor(ctx, detail).then((list) => {
      if (!panel.isConnected) return;
      if (!list.length) {
        select.textContent = '';
        select.appendChild(h('option', {}, 'Состав комнаты недоступен'));
        return;
      }
      fill(list);
    });
  }
  select.addEventListener('change', () => {
    const current = node.assignee ? String(node.assignee) : '';
    apply.disabled = !select.value || select.value === current;
  });

  apply.addEventListener('click', async () => {
    const profile = select.value;
    if (!profile) return;
    lock(panel, true);
    try {
      await postAction(ctx, detail.id, { action: 'reassign', task_id: node.id, profile });
      toast(ctx, `Задача назначена на ${profile}`, 'ok');
      onDone();
      changed(ctx);
    } catch (err) {
      toast(ctx, (err && err.message) || 'Не удалось переназначить', 'danger');
      lock(panel, false);
    }
  });
  return panel;
}

async function askRework(node, ctx, detail, onDone) {
  const text = await promptDialog({
    title: 'На доработку',
    label: `Что доработать в задаче «${String(node.title || node.id)}»`,
    placeholder: 'Например: добавить сроки и проверить цифры по котировкам',
    multiline: true,
    submitLabel: 'Отправить на доработку',
  });
  if (!text || !String(text).trim()) return;
  try {
    await postAction(ctx, detail.id, { action: 'rework', task_id: node.id, text: String(text).trim() });
    toast(ctx, 'Задача отправлена на доработку', 'ok');
    onDone();
    changed(ctx);
  } catch (err) {
    toast(ctx, (err && err.message) || 'Не удалось отправить на доработку', 'danger');
  }
}

async function askAnswer(node, ctx, detail, onDone) {
  const text = await promptDialog({
    title: 'Ответить на блокировку',
    label: `Что ответить по задаче «${String(node.title || node.id)}»`,
    placeholder: 'Короткий ответ: решение, вводные или «делай по варианту 2»',
    multiline: true,
    submitLabel: 'Ответить и разблокировать',
  });
  if (!text || !String(text).trim()) return;
  const payload = { action: 'answer', task_id: node.id, text: String(text).trim() };
  try {
    await postAction(ctx, detail.id, payload);
    toast(ctx, 'Ответ отправлен, задача разблокирована', 'ok');
    onDone();
    changed(ctx);
  } catch (err) {
    // Сервер со строгим контрактом ждёт question_id; для карточки это
    // эквивалентно unblock с текстом — не показываем ошибку пользователю.
    if (err && err.status === 400 && /question/i.test(String(err.message || ''))) {
      try {
        await postAction(ctx, detail.id, { action: 'unblock', task_id: node.id, text: payload.text });
        toast(ctx, 'Ответ отправлен, задача разблокирована', 'ok');
        onDone();
        changed(ctx);
        return;
      } catch (err2) {
        toast(ctx, (err2 && err2.message) || 'Не удалось ответить', 'danger');
        return;
      }
    }
    toast(ctx, (err && err.message) || 'Не удалось ответить', 'danger');
  }
}

/**
 * Кнопка повтора с откликом и идемпотентностью (B-25) — общий модуль retry-button.js:
 * в дереве и в блоке «Ответ не получился» одно и то же поведение.
 */
function retryControl(node, ctx, detail, ui, onChanged) {
  return createRetryButton({
    ctx,
    rid: String((detail && detail.id) || ''),
    taskId: String(node.id),
    node,
    marks: (ui && ui.marks) || null,
    title: 'Повторить задачу?',
    text: `Задача «${String(node.title || node.id)}» будет запущена заново. Это новый прогон агента.`,
    onSent: typeof onChanged === 'function' ? onChanged : null,
  });
}

/** Строки попыток под плашкой ошибки: «Попытка 2 · 12:04 · превышено время ожидания». */
function attemptsBlock(node) {
  const rows = attemptRows(node).filter((r) => r.failed);
  if (!rows.length) return null;
  const box = h('ul', { class: 'rp-node__attempts small' });
  for (const row of rows) {
    box.appendChild(h('li', { class: 'rp-node__attempt', title: row.why || null },
      h('span', { class: 'rp-node__attempt-n' }, `Попытка ${row.n}`),
      row.at ? h('span', { class: 'rp-node__attempt-at muted tnum' }, timeAgo(row.at)) : null,
      h('span', { class: 'rp-node__attempt-why' }, row.why || row.status)));
  }
  return box;
}

/**
 * «Попытка 1 остановлена контролем моделей: была не та модель (вызовов 1)» —
 * СПОКОЙНАЯ строка вместо красной плашки (W28-C2): убитый сторожем запуск не сбой
 * работы, карточка просто пошла заново.
 *
 * Ссылка «Контроль моделей» ведёт к ленте контроля на этой же странице — если она
 * есть; иначе строке достаточно текста (мёртвой ссылки не рисуем).
 *
 * @param {object} node TaskNode
 * @param {object} detail RequestDetail
 * @returns {HTMLElement|null}
 */
function guardNote(node, detail) {
  const attempts = guardAttempts(node);
  if (!attempts.length) return null;
  const feed = Array.isArray(detail && detail.model_guard) && detail.model_guard.length
    ? h('button', {
      type: 'button', class: 'btn btn--link btn--sm rp-attempt__link',
      title: 'Что сделал контроль моделей по этому запросу',
      onclick: () => {
        const target = typeof document !== 'undefined'
          ? document.querySelector('[data-model-guard="feed"], #model-guard') : null;
        if (target && typeof target.scrollIntoView === 'function') {
          target.scrollIntoView({ block: 'start', behavior: 'smooth' });
        }
      },
    }, 'Контроль моделей')
    : null;
  const box = h('div', { class: 'rp-node__attempt-note', role: 'status', 'data-rp': 'guard-attempts' });
  for (const a of attempts) {
    const model = [a.provider, a.model].filter(Boolean).join(' · ');
    box.appendChild(h('div', { class: 'rp-attempt__line' },
      icon('shield', { size: 12, className: 'rp-attempt__icon' }),
      h('span', { class: 'rp-attempt__text' },
        `Попытка ${a.n} остановлена контролем моделей: была не та модель`
        + (a.calls === null ? '' : ` (вызовов ${a.calls})`)),
      model ? h('span', { class: 'rp-attempt__model small muted' }, model) : null));
  }
  if (feed) box.appendChild(h('div', { class: 'rp-attempt__more small' }, feed));
  return box;
}

/**
 * Плашка ошибки карточки (B-24): причина, попытки и кнопка «Понятно».
 * Закрытая плашка помнится по «id карточки + номер попытки»; новая попытка показывает её снова.
 *
 * W28-C2: плашка — ТОЛЬКО по итогу карточки. Попытки, убитые контролем моделей,
 * ошибкой не считаются: у них спокойная строка `guardNote`.
 *
 * @returns {HTMLElement|null}
 */
function errorPlaque(node, ui) {
  if (!cardFailed(node)) return null;
  const notices = ui && ui.notices;
  const stamp = failureStamp(node) || `${String(node.id)}:0`;
  const box = h('div', { class: 'rp-node__error-slot' });

  const hidden = () => (notices ? notices.isDismissed('node', node.id, stamp) : false);

  const paintHidden = () => {
    box.textContent = '';
    if (!hidden()) return;
    box.appendChild(h('div', { class: 'rp-node__error-dismissed small muted' },
      h('span', {}, 'Причина ошибки скрыта.'),
      h('button', {
        type: 'button', class: 'btn btn--link btn--sm',
        onclick: () => {
          if (notices) notices.restore('node', node.id);
          paintBanner();
        },
      }, 'показать снова')));
  };

  const paintBanner = () => {
    box.textContent = '';
    if (hidden()) { paintHidden(); return; }
    const reason = errorReason(failureMessage(node));
    const attempts = attemptsBlock(node);
    const banner = h('div', { class: 'rp-node__error banner banner--error', role: 'alert' },
      icon('error', { size: 13 }),
      h('div', { class: 'banner__text' },
        h('div', {}, reason ? `Задача не справилась: ${reason}.` : 'Задача завершилась ошибкой — подробности ниже.'),
        attempts),
      h('button', {
        type: 'button',
        class: 'btn btn--ghost btn--sm rp-node__error-close',
        title: 'Понятно — больше не показывать эту ошибку',
        'aria-label': 'Понятно: скрыть сообщение об ошибке',
        onclick: () => {
          if (notices) notices.dismiss('node', node.id, stamp);
          paintHidden();
        },
      }, icon('close', { size: 12 }), 'Понятно'));
    box.appendChild(banner);
  };

  paintBanner();
  return box;
}


function commentsBlock(node) {
  const comments = Array.isArray(node.comments) ? node.comments : [];
  if (!comments.length) return null;
  const box = h('div', { class: 'rp-comments' });
  box.appendChild(h('div', { class: 'rp-block__label small muted' }, `Комментарии · ${comments.length}`));
  for (const c of comments) {
    const author = String(c.author || 'system');
    const authorName = author === 'system' ? 'Система' : (personName(author) || '—');
    box.appendChild(h('div', { class: 'rp-comment' },
      h('div', { class: 'rp-comment__head' },
        h('span', { class: 'rp-comment__author small', title: author }, authorName),
        h('span', { class: 'small muted', title: fmtDateTime(c.at) }, timeAgo(c.at))),
      h('div', { class: 'rp-comment__text' }, plainTerms(String(c.text || '')))));
  }
  return box;
}

const RUN_STATUS_RU = {
  done: 'готово', completed: 'готово', running: 'идёт', failed: 'не справился',
  crashed: 'не справился', error: 'не справился', blocked: 'ждёт',
  queued: 'в очереди', scheduled: 'отложена',
};

/** Подпись итога попытки для блока «Попытки · N» (техническое значение → слово). */
export function runStatusRu(run) {
  const key = String((run && (run.status || run.outcome)) || '');
  return RUN_STATUS_RU[key] || '';
}

function hermesSessionButton(node, ctx) {
  const sid = sessionOf(node);
  if (!sid) return null;
  return h('button', {
    type: 'button', class: 'btn btn--link btn--sm rp-node__session',
    title: 'Сеанс Hermes этого запуска: id ' + sid + '. Нажмите, чтобы скопировать id.',
    onclick: async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const ok = await copyText(sid);
      toast(ok ? 'ID сеанса скопирован — откройте Hermes → Сеансы и найдите его'
        : 'Не удалось скопировать ID сеанса: ' + sid, ok ? 'ok' : 'warn');
      if (typeof ctx === 'object' && ctx && typeof ctx.onSession === 'function') ctx.onSession(sid);
    },
  }, icon('history', { size: 11 }), 'Сеанс в Hermes');
}

function runsBlock(node, ctx) {
  const rows = attemptRows(node);
  const box = h('div', { class: 'rp-runs' });
  box.appendChild(h('div', { class: 'rp-block__label small muted' }, `Попытки · ${rows.length}`));
  for (const row of rows) {
    // W28-C2: у каждой попытки — её СВОЯ модель и число вызовов. Старые попытки
    // живут здесь, а подпись «ушло на» у карточки — только про последнюю.
    const dur = row.duration_s ? fmtDuration(row.duration_s) : '';
    box.appendChild(h('div', {
      class: ['rp-run', row.failed ? 'rp-run--failed' : '', row.guard ? 'rp-run--guard' : ''],
      title: row.why || row.detail || null,
    },
    h('span', { class: 'rp-run__n small muted tnum' }, `Попытка ${row.n}`),
    h('span', { class: 'rp-run__profile small', title: row.profile || null },
      profileName(row.profile, ctx && ctx.room) || '—'),
    h('span', { class: 'rp-run__status small' }, row.guard ? 'остановлена' : (row.status || '—')),
    h('span', { class: 'rp-run__time small muted tnum' }, [timeAgo(row.at), dur].filter(Boolean).join(' · ')),
    row.model ? h('span', { class: 'rp-run__model small muted' }, row.model) : null,
    row.calls ? h('span', { class: 'rp-run__calls small muted tnum' }, row.calls) : null,
    row.why ? h('span', { class: 'rp-run__error small' }, row.why) : null));
  }
  return box;
}

/**
 * Строка дерева работ с раскрытием (summary, комментарии, попытки, ошибка)
 * и действиями: ответить / повторить / переназначить / на доработку.
 */
/**
 * Чип статуса задачи с нейтральной подписью для «готово»: «Срез … готова»
 * читалось как рассогласование рода (D17).
 */
function treeStatusChip(node) {
  const raw = String((node && node.status) || '');
  const el = statusChip(node && node.status);
  if (raw === 'ready') {
    const text = el.querySelector('.status-chip__text');
    if (text) text.textContent = 'готово';
    el.setAttribute('title', 'Статус: готово — задача готова к запуску');
  }
  return el;
}

function nodeRow(row, ctx, detail, onChanged, ui) {
  const node = row.node;
  const actions = nodeActions(node);
  const state = (ui && ui.state) || null;
  const problem = problemSignature(node);
  const li = h('li', {
    class: ['rp-node', node.outside_room ? 'rp-node--outside' : '', row.depth > 0 ? 'rp-node--child' : ''],
    style: { '--rp-depth': String(row.depth) },
    dataset: { taskId: node.id },
    // W17-C: стабильный ключ — точечная правка узнаёт эту строку и не пересоздаёт её.
    'data-key': 'rp-node-' + String(node.id),
    title: 'Номер задачи: ' + String(node.id),
  });

  const panelId = `rp-node-${String(node.id).replace(/[^A-Za-z0-9_-]/g, '')}`;
  const head = h('div', { class: 'rp-node__head' });

  const toggle = h('button', {
    class: 'btn btn--ghost btn--icon btn--sm rp-node__toggle',
    type: 'button',
    'aria-expanded': 'false',
    'aria-controls': panelId,
    'aria-label': `Раскрыть задачу «${String(node.title || node.id)}»`,
    title: 'Подробности задачи',
    disabled: !(node.summary_md || (node.comments || []).length || attemptTotal(node) || cardFailed(node)),
  }, icon('chevron-right', { size: 13 }));
  head.appendChild(toggle);

  const main = h('div', { class: 'rp-node__main' });
  const line = h('div', { class: 'rp-node__line' },
    h('span', { class: 'rp-node__title' }, String(node.title || node.id)));
  main.appendChild(line);

  const chips = h('div', { class: 'rp-node__chips' },
    roleChip(node.role || 'worker'),
    treeStatusChip(node),
    node.outside_room ? h('span', { class: 'chip chip--warn', title: 'Исполнитель не входит в состав комнаты' }, 'вне состава') : null,
    node.needs_human ? h('span', { class: 'chip chip--warn' }, icon('person', { size: 11 }), 'ждёт тебя') : null);
  line.appendChild(chips);

  const rawAssignee = String(node.assignee || '');
  const sub = h('div', { class: 'rp-node__sub small muted' },
    h('span', { class: 'rp-node__person', title: rawAssignee || null },
      rawAssignee ? (profileName(rawAssignee, ctx && ctx.room) || rawAssignee) : 'не назначена'));
  const dur = nodeDuration(node);
  if (dur) sub.appendChild(h('span', { class: 'rp-node__dur tnum' }, dur));
  // Модель, на которой шла эта задача (WAVE10): мелко, только если сервер прислал.
  const modelLabel = String(node.model_label || '').trim();
  if (modelLabel) {
    sub.appendChild(h('span', { class: 'rp-node__model', title: 'Модель и мышление этой задачи' }, modelLabel));
  }
  // W13-A: «ушло на: …» — фактическая модель запуска (из сессии воркера), а не только выбор.
  const sent = node.model_sent && typeof node.model_sent === 'object' ? node.model_sent : null;
  const sentText = sent ? String(sent.text || '').trim() : '';
  if (sentText) {
    const why = sent.confirmed ? 'фактическая модель запуска' : 'записано в карточке, запуск ещё не подтверждён';
    sub.appendChild(h('span', {
      class: 'rp-node__model',
      title: 'Что реально ушло (' + why + ')',
    }, sentText));
    // W23-MG2: «расходится с выбором комнаты» здесь больше нет — расхождение
    // исправляет сторож моделей, а человеку показывается результат (лента
    // «Контроль моделей» на странице и строка «Остановлено…» у карточки).
  }
  // W29B-Q2: «запрос ↔ сеансы Hermes» — у карточки id сеанса её запуска
  // (копируется; deep-link Hermes Desktop на сеанс не объявляет).
  const sessionBtn = hermesSessionButton(node, ctx);
  if (sessionBtn) sub.appendChild(sessionBtn);
  main.appendChild(sub);

  // W28-C2: убитые контролем моделей попытки — спокойной строкой (не красное и не
  // в счётчик «ошибок»): «Попытка 1 остановлена контролем моделей: была не та модель».
  const guard = guardNote(node, detail);
  if (guard) main.appendChild(guard);

  // W23-MG2: управление работой — как в самом Hermes. «Остановить» у идущей
  // карточки, «Продолжить» и объяснение — у остановленной.
  const requestId = String((detail && detail.id) || '');
  const onChangedCb = () => changed(ctx, onChanged);
  const stoppedBox = stoppedLine(ctx, {
    node, requestId, catalog: (ctx && ctx.catalog) || null, onChanged: onChangedCb,
  });
  if (stoppedBox) main.appendChild(stoppedBox);
  const guardActions = cardControls(ctx, {
    node, requestId, profile: rawAssignee, title: node.title,
    catalog: (ctx && ctx.catalog) || null, compact: true, onChanged: onChangedCb,
  });
  if (guardActions && guardActions.childNodes && guardActions.childNodes.length) main.appendChild(guardActions);
  head.appendChild(main);

  const actBox = h('div', { class: 'rp-actions' });
  const panelSlot = h('div', { class: 'rp-actions__slot' });
  const rerenderActions = () => {
    actBox.textContent = '';
    panelSlot.textContent = '';
    if (actions.includes('answer')) {
      actBox.appendChild(button('Ответить', {
        variant: 'primary', size: 'sm', icon: 'comment',
        onclick: () => askAnswer(node, ctx, detail, () => rerenderActions()),
      }));
    }
    if (actions.includes('retry')) {
      actBox.appendChild(retryControl(node, ctx, detail, ui, () => rerenderActions()));
    }
    if (actions.includes('reassign')) {
      actBox.appendChild(button('Переназначить', {
        variant: 'secondary', size: 'sm', icon: 'person-add',
        onclick: () => {
          panelSlot.textContent = '';
          panelSlot.appendChild(reassignPanel(node, ctx, detail, () => rerenderActions()));
          const sel = panelSlot.querySelector('select');
          if (sel && typeof sel.focus === 'function') sel.focus();
        },
      }));
    }
    if (actions.includes('rework')) {
      actBox.appendChild(button('На доработку', {
        variant: 'ghost', size: 'sm', icon: 'edit',
        onclick: () => askRework(node, ctx, detail, () => rerenderActions()),
      }));
    }
  };
  rerenderActions();
  if (actBox.childNodes.length) head.appendChild(actBox);
  head.appendChild(panelSlot);
  li.appendChild(head);

  const panel = h('div', { class: 'rp-node__panel', id: panelId, hidden: true });
  if (node.summary_md) panel.appendChild(mdBlock(node.summary_md, { fileUrl }));
  else if (node.status === 'done') panel.appendChild(h('p', { class: 'small muted' }, 'Сводки нет — задача завершилась без текста.'));

  const idRow = {
    label: 'Номер задачи',
    text: String(node.id),
    copy: true,
    toast: (msg, ok) => toast(ctx, msg, ok ? 'ok' : 'danger'),
  };
  // W28-C2: красное — только по ИТОГУ карточки (убитые сторожем попытки уже
  // показаны спокойной строкой выше, в них нет ни плашки, ни «ошибок»).
  const failed = cardFailed(node);
  if (failed) {
    const plaque = errorPlaque(node, ui);
    if (plaque) panel.appendChild(plaque);
    const dev = devDetails([idRow, {
      label: 'Сообщение движка',
      text: String((node.error && (node.error.message || node.error.code)) || ''),
      code: node.error && node.error.code ? String(node.error.code) : '',
    }]);
    if (dev) panel.appendChild(dev);
  }
  const comments = commentsBlock(node);
  if (comments) panel.appendChild(comments);
  const runs = runsBlock(node, ctx);
  if (runs) panel.appendChild(runs);
  // Номер задачи — только в раскрывающихся деталях и по кнопке «Копировать» (D6).
  if (!failed) {
    const dev = devDetails([idRow], 'Номер задачи для разработчика');
    if (dev) panel.appendChild(dev);
  }
  li.appendChild(panel);

  const expandable = !!(node.summary_md || comments || runs || failed);
  const setOpen = (open) => {
    panel.hidden = !open;
    li.classList.toggle('rp-node--open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.textContent = '';
    toggle.appendChild(icon(open ? 'chevron-down' : 'chevron-right', { size: 13 }));
  };
  if (expandable) {
    // B-19: раскрытость берём из данных (память + sessionStorage), а не из DOM.
    // Перерисовка по опросу и точечная правка live-patch состояние больше не сбрасывают.
    setOpen(Boolean(state && state.isOpen(node.id)));
    toggle.addEventListener('click', () => {
      const next = panel.hidden;      // было свёрнуто — раскрываем, и наоборот
      setOpen(next);
      // Выбор человека главнее авто-раскрытия — запоминаем его вместе с подписью проблемы.
      if (state) state.setOpen(node.id, next, problem);
    });
    // Авто-раскрытие — один раз при первом появлении ошибки/«ждёт тебя».
    if (state && problem && state.autoOpen(node.id, problem)) setOpen(true);
  }
  return li;
}

/**
 * «Дерево работ»: карточки с отступом по глубине, чипами ролей/статусов,
 * раскрытием и действиями. null, если карточек нет.
 * @param {object} detail RequestDetail
 * @param {object} ctx
 * @param {{onChanged?: Function}} [opts]
 * @returns {HTMLElement|null}
 */
export function renderTree(detail, ctx, opts = {}) {
  const o = opts || {};
  const tree = buildTree(detail && detail.tree);
  if (!tree.rows.length) {
    return h('section', { class: 'card rp-tree', 'data-rp-tree': '', 'data-rp': 'tree' },
      sectionHeader('Дерево работ'),
      emptyState({
        icon: 'type-hierarchy',
        title: 'Задач пока нет',
        text: 'Руководитель комнаты ещё не разложил запрос на срезы — дерево появится, когда начнётся работа.',
      }));
  }

  const rid = String((detail && detail.id) || '');
  const defaults = sectionDefaults(detail);

  /**
   * Строки дерева — ТОЛЬКО при раскрытии блока (У-1): у большого запроса это
   * 200 узлов с панелями, комментариями и кнопками. Свёрнутый блок их не
   * строит вовсе, поэтому страница открывается быстро.
   */
  const buildList = () => {
    // B-19/B-24/B-25: состояние — в данных (sessionStorage), ключ — id запроса.
    // Одна копия на отрисовку: раскрытость, закрытые плашки, отметки «повтор отправлен».
    const ui = {
      state: createTreeState({ requestId: rid }),
      notices: createNotices({ requestId: rid }),
      marks: createMarks({ requestId: rid }),
    };
    ui.state.retain(tree.rows.map((row) => row.id));
    const list = h('ul', { class: 'rp-tree__list' });
    for (const row of tree.rows) list.appendChild(nodeRow(row, ctx, detail, () => changed(ctx, o.onChanged), ui));
    return list;
  };

  const note = tree.cycleIds.length
    ? h('p', { class: 'small muted rp-tree__note' }, 'Связи между задачами зациклены — такие строки показаны без отступа.')
    : null;

  // Один счётчик вместо двух: «6 из 6 задач» — и сколько всего, и сколько готово (D18),
  // плюс «есть ошибка» / «ждёт вашего ответа» (У-1) — видно и в свёрнутом виде.
  const sec = createSection({
    key: 'tree',
    requestId: rid,
    title: 'Дерево работ',
    defaultOpen: defaults.tree,
    className: 'rp-tree',
    attrs: { 'data-rp-tree': '', 'data-rp': 'tree' },
    summary: treeSummaryChips(detail),
    content: () => [note, buildList()].filter(Boolean),
  });
  return sec.element;
}

