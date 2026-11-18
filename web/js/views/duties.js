/**
 * duties.js — раздел «Дежурства» комнаты (#/room/<id>/duties).
 *
 * Что человек видит и может сделать:
 *  • недельная сетка как в Mission Control: дни, запуски, «+N ещё», итог и план;
 *  • список дежурств: расписание по-русски, последний и следующий запуск,
 *    красные пометки (ошибка, просрочка, ни разу не запускалось, выключено);
 *  • действия у каждого пункта: «Запустить сейчас», «Пауза»/«Возобновить»,
 *    «Изменить расписание…» (только предложение), «Привязать»/«Отвязать».
 *
 * Данные — GET /api/duties (server/duties.py). Действия — POST
 * /api/duties/{id}/actions с обязательным `confirm: true`: сервер не делает
 * ничего без подтверждения человека, а запуск/пауза/возобновление идут штатным
 * `hermes cron …` (в тестах и песочнице — подставной CLI).
 */

import { h, plural } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, errorBanner, skeleton, confirmDialog, promptDialog } from '../ui.js';

const VIEW_CSS = 'css/view-duties.css';

/** Дни недели для сетки; 0 — понедельник (как отдаёт сервер). */
export const WEEK_LABELS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const WEEK_FULL = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'];
const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
  'августа', 'сентября', 'октября', 'ноября', 'декабря'];

/** Статус запуска человеческим языком (совпадает со словарём N4 для отчётов). */
export const RUN_LABELS = {
  completed: 'выполнено',
  failed: 'ошибка',
  running: 'идёт сейчас',
  missed: 'пропущено',
  unknown: 'неизвестно',
};

/** Короткая пометка замечания (красная строка у дежурства). */
export const PROBLEM_LABELS = {
  failed: 'ошибка запуска',
  stale: 'просрочено',
  never_ran: 'ни разу не запускалось',
  disabled_but_due: 'выключено',
};

/** Действия над дежурством: подписи и порядок — одни для кнопок, подсказок и тестов. */
export const ACTIONS = {
  run: { label: 'Запустить сейчас', short: 'Запустить', primary: true },
  pause: { label: 'Пауза', short: 'Пауза' },
  resume: { label: 'Возобновить', short: 'Возобновить' },
  schedule: { label: 'Расписание…', short: 'Расписание' },
  bind: { label: 'Привязать к комнате', short: 'Привязать' },
  unbind: { label: 'Отвязать от комнаты', short: 'Отвязать' },
};

/* ─────────────────────────────────────────────────────── модель (без DOM) */

/** Русское число с формой слова: «2 запуска», «5 запусков». */
export function countText(count, forms) {
  const num = Number(count) || 0;
  return num + ' ' + plural(num, forms);
}

/**
 * Относительное время: «через 5 минут», «2 часа назад».
 * @param {number} seconds  положительное — будущее
 */
export function relText(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '';
  const future = value > 0;
  const abs = Math.abs(value);
  let core;
  if (abs < 60) core = 'меньше минуты';
  else if (abs < 3600) core = countText(Math.round(abs / 60), ['минуту', 'минуты', 'минут']);
  else if (abs < 172800) core = countText(Math.round(abs / 3600), ['час', 'часа', 'часов']);
  else core = countText(Math.round(abs / 86400), ['день', 'дня', 'дней']);
  return future ? 'через ' + core : core + ' назад';
}

/** Время по-русски: «16.11, 09:00». */
export function timeText(stamp) {
  const ts = Number(stamp);
  if (!Number.isFinite(ts) || ts <= 0) return '';
  const date = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return pad(date.getDate()) + '.' + pad(date.getMonth() + 1) + ', '
    + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

/** Дата дня сетки по-русски: «пн, 16 ноября». */
export function dayTitle(day) {
  if (!day || typeof day !== 'object') return '';
  const month = MONTHS_GEN[Math.max(0, Math.min(11, Number(day.month || 1) - 1))];
  return (WEEK_LABELS[Number(day.weekday) || 0] || '') + ', ' + Number(day.day || 0) + ' ' + month;
}

/** Итог дня: «12 запусков», «1 с ошибкой», в будущем — «план: 48». */
export function daySummary(day) {
  if (!day || typeof day !== 'object') return '';
  if (day.future) {
    return Number(day.planned) > 0 ? 'план: ' + Number(day.planned) : 'по плану пусто';
  }
  const runs = Number(day.runs) || 0;
  if (!runs) {
    return Number(day.planned) > 0 ? 'запусков не было (план ' + Number(day.planned) + ')' : 'запусков не было';
  }
  let text = countText(runs, ['запуск', 'запуска', 'запусков']);
  if (Number(day.failed) > 0) text += ' · ' + Number(day.failed) + ' с ошибкой';
  if (Number(day.missed) > 0) text += ' · ' + Number(day.missed) + ' пропущено';
  return text;
}

/** Нормализовать неделю: всегда 7 дней с подписями (сервер мог ответить неполно). */
export function weekModel(week) {
  const doc = week && typeof week === 'object' ? week : {};
  const days = Array.isArray(doc.days) ? doc.days.filter((d) => d && typeof d === 'object') : [];
  return {
    start: doc.start || '',
    end: doc.end || '',
    runs: Number(doc.runs) || 0,
    failed: Number(doc.failed) || 0,
    missed: Number(doc.missed) || 0,
    error: doc.error || '',
    days: days.map((day, index) => ({
      date: String(day.date || ''),
      weekday: Number.isFinite(Number(day.weekday)) ? Number(day.weekday) : index,
      label: day.label || WEEK_LABELS[index] || '',
      full: day.full || WEEK_FULL[index] || '',
      day: Number(day.day) || 0,
      month: Number(day.month) || 0,
      today: !!day.today,
      future: !!day.future,
      runs: Number(day.runs) || 0,
      failed: Number(day.failed) || 0,
      missed: Number(day.missed) || 0,
      planned: Number(day.planned) || 0,
      more: Number(day.more) || 0,
      jobs: Array.isArray(day.jobs) ? day.jobs.filter((j) => j && typeof j === 'object') : [],
    })),
  };
}

/** Сводка по списку дежурств: сколько всего, с замечаниями, выключено, идёт. */
export function dutiesSummary(items) {
  const list = Array.isArray(items) ? items.filter((i) => i && typeof i === 'object') : [];
  const out = { total: list.length, problems: 0, off: 0, running: 0, ok: 0 };
  for (const item of list) {
    const state = dutyState(item);
    if (state.kind === 'fail') out.problems += 1;
    else if (state.kind === 'off') out.off += 1;
    else out.ok += 1;
    if (item.last_run && item.last_run.status === 'running') out.running += 1;
  }
  return out;
}

/** Состояние дежурства: `fail` (красное), `off` (выключено), `ok`. */
export function dutyState(item) {
  const doc = item && typeof item === 'object' ? item : {};
  const problem = doc.problem && typeof doc.problem === 'object' ? doc.problem : null;
  if (problem) {
    const off = problem.kind === 'disabled_but_due' || doc.enabled === false;
    return { kind: off ? 'off' : 'fail', label: PROBLEM_LABELS[problem.kind] || 'замечание' };
  }
  if (doc.enabled === false) return { kind: 'off', label: PROBLEM_LABELS.disabled_but_due };
  return { kind: 'ok', label: 'по расписанию' };
}

/** Строка «последний запуск: …». */
export function lastRunLine(item, nowSec) {
  const now = Number(nowSec) || Math.floor(Date.now() / 1000);
  const last = item && item.last_run && typeof item.last_run === 'object' ? item.last_run : null;
  if (!last) return 'запусков ещё не было';
  const label = RUN_LABELS[last.status] || 'неизвестно';
  const stamp = Number(last.finished_at || last.started_at || last.scheduled_for) || 0;
  if (!stamp) return 'последний запуск: ' + label;
  return 'последний запуск: ' + relText(stamp - now) + ' · ' + label;
}

/** Строка «следующий запуск: …». */
export function nextRunLine(item, nowSec) {
  const now = Number(nowSec) || Math.floor(Date.now() / 1000);
  const next = Number(item && item.next_run_at) || 0;
  if (!next) return 'следующий запуск не назначен';
  return 'следующий запуск: ' + relText(next - now) + ' (' + timeText(next) + ')';
}

/**
 * Кнопки дежурства: у каждого пункта есть действие (правило «без тупиков»).
 * @param {object} item дежурство из /api/duties
 * @param {string} roomId текущая комната ('' — общий список)
 */
export function actionList(item, roomId) {
  const doc = item && typeof item === 'object' ? item : {};
  const bound = Array.isArray(doc.bound_rooms)
    ? doc.bound_rooms.map((x) => String(x)) : [];
  const list = [
    { id: 'run', label: ACTIONS.run.label, primary: true },
    doc.enabled === false
      ? { id: 'resume', label: ACTIONS.resume.label }
      : { id: 'pause', label: ACTIONS.pause.label },
    { id: 'schedule', label: ACTIONS.schedule.label },
  ];
  if (roomId) {
    list.push(bound.includes(String(roomId))
      ? { id: 'unbind', label: ACTIONS.unbind.label }
      : { id: 'bind', label: ACTIONS.bind.label });
  }
  return list;
}

/** Текст подтверждения — человек должен понимать, что произойдёт. */
export function confirmText(action, item, schedule) {
  const name = (item && item.name) || 'дежурство';
  const when = (item && (item.schedule_human || item.schedule)) || 'по расписанию';
  switch (action) {
    case 'run':
      return 'Запустить «' + name + '» сейчас, вне расписания (' + when + ')? '
        + 'Задание выполнится как обычный запуск: его итог появится в ведомости.';
    case 'pause':
      return 'Поставить «' + name + '» на паузу? По расписанию оно запускаться не будет, '
        + 'пока вы не нажмёте «Возобновить».';
    case 'resume':
      return 'Вернуть «' + name + '» в расписание (' + when + ')?';
    case 'bind':
      return 'Привязать «' + name + '» к этой комнате? Дежурство появится в её разделе. '
        + 'Само задание в Hermes не меняется.';
    case 'unbind':
      return 'Отвязать «' + name + '» от этой комнаты? Задание в Hermes останется и продолжит '
        + 'идти по расписанию.';
    case 'schedule':
      return 'Отправить предложение сменить расписание на «' + (schedule || '…') + '»? '
        + 'Расписание в Hermes не изменится: это только предложение, применить его — вручную.';
    default:
      return 'Подтвердить действие?';
  }
}

/** Итог недели одной строкой: «18 запусков за неделю · 1 с ошибкой». */
export function weekTotalsText(week) {
  const doc = weekModel(week);
  if (!doc.days.length) return 'неделя не посчитана';
  let text = doc.runs ? countText(doc.runs, ['запуск', 'запуска', 'запусков']) + ' за неделю'
    : 'за неделю запусков не было';
  if (doc.failed) text += ' · ' + doc.failed + ' с ошибкой';
  if (doc.missed) text += ' · ' + doc.missed + ' пропущено';
  return text;
}

/* ────────────────────────────────────────────────────────────── отрисовка */

function chip(text, kind, title) {
  return h('span', { class: 'duty-chip duty-chip--' + (kind || 'muted'), title: title || null }, text);
}

function summaryChip(text, kind, title) {
  return h('span', { class: 'duty-sum duty-sum--' + (kind || 'muted'), title: title || null }, text);
}

/** Недельная сетка: семь дней, плашки дежурств, «+N ещё», итог дня. */
export function weekGrid(week, onPickDay) {
  const doc = weekModel(week);
  const box = h('div', { class: 'week', 'data-role': 'duties-week' });
  if (!doc.days.length) {
    box.appendChild(h('p', { class: 'week__empty small muted' },
      doc.error ? 'Неделю посчитать не удалось: ' + doc.error
        : 'Недельная сетка пуста: данных о запусках нет.'));
    return box;
  }
  for (const day of doc.days) {
    const classes = ['week__day',
      day.today ? 'week__day--today' : '',
      day.future ? 'week__day--future' : '',
      day.failed ? 'week__day--fail' : ''].filter(Boolean).join(' ');
    const jobs = day.jobs.slice(0, 3).map((job) => h('span', {
      class: 'week__job' + (job.state === 'failed' ? ' week__job--fail' : ''),
      title: (job.name || '') + ' — ' + countText(Number(job.runs) || 1, ['запуск', 'запуска', 'запусков']),
    }, job.name || job.job_id || 'дежурство'));
    const card = h('button', {
      class: classes,
      type: 'button',
      'data-date': day.date,
      'aria-label': dayTitle(day) + ': ' + daySummary(day),
      onclick: () => { if (typeof onPickDay === 'function') onPickDay(day); },
    },
    h('span', { class: 'week__head' },
      h('span', { class: 'week__date' }, day.label + ' ' + day.day),
      h('span', { class: 'week__count' }, day.runs ? String(day.runs) : (day.future ? String(day.planned) : '0'))),
    h('span', { class: 'week__jobs' }, ...jobs,
      ...(day.more ? [h('span', { class: 'week__more' }, '+' + day.more + ' ещё')] : [])),
    h('span', { class: 'week__foot' }, daySummary(day)));
    box.appendChild(card);
  }
  return box;
}

/** Карточка одного дежурства со всеми действиями. */
export function renderDuty(item, opts = {}) {
  const roomId = String(opts.roomId || '');
  const nowSec = Number(opts.now) || Math.floor(Date.now() / 1000);
  const state = dutyState(item);
  const problem = item && item.problem && typeof item.problem === 'object' ? item.problem : null;
  const bound = Array.isArray(item && item.bound_rooms) ? item.bound_rooms : [];
  const card = h('article', {
    class: 'duty duty--' + state.kind,
    'data-job-id': String((item && item.job_id) || ''),
  });

  const actions = h('div', { class: 'duty__actions' });
  for (const act of actionList(item, roomId)) {
    actions.appendChild(h('button', {
      class: 'btn ' + (act.primary ? 'btn--primary' : 'btn--secondary') + ' btn--sm',
      type: 'button',
      'data-action': act.id,
      'data-job-id': String((item && item.job_id) || ''),
      title: act.id === 'schedule'
        ? 'Изменить расписание: сохранится предложение, Hermes не меняется'
        : confirmText(act.id, item),
      onclick: () => { if (typeof opts.onAction === 'function') opts.onAction(item, act.id); },
    }, act.label));
  }

  const chips = h('div', { class: 'duty__chips' },
    chip(item && item.enabled === false ? 'выключено' : 'включено',
      item && item.enabled === false ? 'off' : 'ok',
      'Дежурство ' + (item && item.enabled === false ? 'выключено' : 'включено') + ' в Hermes'),
    ...(problem && problem.kind !== 'disabled_but_due'
      ? [chip(PROBLEM_LABELS[problem.kind] || 'замечание', 'fail', problem.text || '')]
      : []),
    bound.length
      ? chip('комнат: ' + bound.length, 'muted',
        'Привязано к комнатам: ' + bound.join(', '))
      : chip('не привязано', 'muted', 'Дежурство не показывается ни в одной комнате'));

  card.appendChild(h('header', { class: 'duty__head' },
    h('div', { class: 'duty__title' },
      h('h3', { class: 'duty__name' }, (item && item.name) || 'дежурство'),
      chips),
    actions));

  card.appendChild(h('div', { class: 'duty__times' },
    h('span', { class: 'duty__when' }, icon('clock', { size: 12 }),
      ' ' + ((item && item.schedule_human) || (item && item.schedule) || 'расписание не указано')),
    h('span', { class: 'duty__dot', 'aria-hidden': 'true' }, '·'),
    h('span', { class: 'duty__last' }, lastRunLine(item, nowSec)),
    h('span', { class: 'duty__dot', 'aria-hidden': 'true' }, '·'),
    h('span', { class: 'duty__next' }, nextRunLine(item, nowSec)),
    ...(Number(item && item.missed_recent) > 0
      ? [h('span', { class: 'duty__missed' }, '· пропущено за неделю: ' + Number(item.missed_recent))]
      : [])));

  if (problem && problem.text) {
    card.appendChild(h('p', {
      class: 'duty__problem' + (problem.kind === 'disabled_but_due' ? ' duty__problem--off' : ''),
    }, problem.text));
  }

  const recent = Array.isArray(item && item.recent) ? item.recent : [];
  if (recent.length) {
    const dots = h('div', { class: 'duty__recent', 'aria-label': 'Последние запуски' });
    for (const run of recent.slice(0, 5)) {
      const status = String((run && run.status) || 'unknown');
      dots.appendChild(h('span', {
        class: 'duty__dot-run duty__dot-run--' + status,
        title: (RUN_LABELS[status] || status) + ' — ' + (timeText(run && run.started_at) || 'время неизвестно'),
      }));
    }
    card.appendChild(dots);
  }

  const excerpt = String((item && item.excerpt) || '').trim();
  if (excerpt) {
    const details = h('details', { class: 'duty__excerpt' });
    details.appendChild(h('summary', {}, 'Краткий итог последнего запуска'));
    details.appendChild(h('p', { class: 'duty__excerpt-text' }, excerpt));
    card.appendChild(details);
  }
  return card;
}

/* ─────────────────────────────────────────────────────────────── вьюха */

/**
 * @param {HTMLElement} el
 * @param {object} ctx  {route, api, toast, room, navigate}
 * @returns {Promise<{unmount: Function}>}
 */
export async function mount(el, ctx) {
  await ensureCss(VIEW_CSS);
  const api = (ctx && ctx.api) || null;
  const toast = (message, kind) => {
    if (ctx && typeof ctx.toast === 'function') ctx.toast(message, kind || 'info');
  };
  const roomId = String((ctx && ctx.route && ctx.route.roomId) || '').trim();
  const roomTitle = (ctx && ctx.room && ctx.room.title) || '';

  let stopped = false;
  let data = null;
  let onlyRoom = !!roomId;
  let busy = '';
  let expanded = '';
  let error = null;

  const title = h('h1', { class: 'duties__title' }, 'Дежурства');
  const lead = h('p', { class: 'duties__lead muted' },
    'Задания по расписанию: что идёт само, когда запускалось и что требует вас. '
      + 'Действия — только по вашему подтверждению.');

  const refreshBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-refresh',
    onclick: () => load(),
  }, icon('refresh', { size: 13 }), 'Обновить');

  const filterBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-filter',
    onclick: () => { onlyRoom = !onlyRoom; renderFilter(); load(); },
  }, icon('folder-opened', { size: 13 }));

  const summaryEl = h('div', { class: 'duties__summary', role: 'status', 'aria-live': 'polite' });
  const weekEl = h('div', { class: 'duties__week' });
  const detailEl = h('div', { class: 'duties__day', 'data-role': 'duties-day' });
  const listEl = h('div', { class: 'duties__list', 'data-role': 'duties-list' });
  const noteEl = h('p', { class: 'duties__note small muted' },
    'Запуск, пауза и возобновление идут штатной командой Hermes — и только после вашего «да». '
      + 'Правка расписания сохраняется как предложение: Hermes её не применяет.');

  const head = h('header', { class: 'duties__head' },
    h('div', { class: 'duties__headline' }, title, lead),
    h('div', { class: 'duties__head-actions' }, filterBtn, refreshBtn));

  const weekHead = h('div', { class: 'duties__section-head' },
    h('h2', { class: 'duties__section-title' }, 'Неделя'),
    h('span', { class: 'duties__section-hint small muted' }, 'запуски по дням, как в Mission Control'));

  const root = h('div', { class: 'view__inner duties' },
    head, summaryEl, weekHead, weekEl, detailEl,
    h('h2', { class: 'duties__section-title duties__section-title--list' }, 'Список дежурств'),
    listEl, noteEl);
  el.appendChild(root);

  function renderFilter() {
    if (!roomId) {
      filterBtn.hidden = true;
      return;
    }
    filterBtn.hidden = false;
    filterBtn.replaceChildren(icon('folder-opened', { size: 13 }),
      onlyRoom ? 'Только этой комнаты' : 'Все дежурства');
    filterBtn.setAttribute('aria-pressed', onlyRoom ? 'true' : 'false');
    filterBtn.title = onlyRoom
      ? 'Показать все дежурства Hermes, а не только привязанные к комнате «' + roomTitle + '»'
      : 'Показать только дежурства комнаты «' + roomTitle + '»';
  }

  function renderSummary() {
    const items = (data && data.items) || [];
    const summary = dutiesSummary(items);
    const week = weekModel(data && data.week);
    summaryEl.replaceChildren(
      summaryChip(countText(summary.total, ['дежурство', 'дежурства', 'дежурств']), 'plain',
        'Всего дежурств в этом списке'),
      ...(summary.problems ? [summaryChip(summary.problems + ' с замечаниями', 'fail',
        'Красные пометки: ошибка запуска, просрочка, ни разу не запускалось')] : []),
      ...(summary.off ? [summaryChip(summary.off + ' выключено', 'off', 'Дежурства на паузе')] : []),
      ...(summary.running ? [summaryChip(summary.running + ' идёт сейчас', 'run', 'Запуск в работе')] : []),
      summaryChip(weekTotalsText(week), 'muted', 'Запуски за текущую неделю'),
      ...(week.error ? [summaryChip('неделя не посчитана', 'fail', week.error)] : []));
    summaryEl.appendChild(h('p', { class: 'duties__verdict' }, verdictText(summary, week)));
  }

  function renderWeekGrid() {
    weekEl.replaceChildren(weekGrid((data && data.week) || {}, (day) => {
      expanded = expanded === day.date ? '' : day.date;
      renderWeekGrid();
    }));
    renderDayDetail();
  }

  function renderDayDetail() {
    const week = weekModel(data && data.week);
    const day = week.days.find((one) => one.date === expanded);
    if (!day) {
      detailEl.replaceChildren();
      detailEl.hidden = true;
      return;
    }
    detailEl.hidden = false;
    const items = (data && data.items) || [];
    const lines = day.jobs.map((job) => {
      const duty = items.find((one) => String(one.job_id) === String(job.job_id));
      return h('li', { class: 'duties__day-line' },
        h('button', {
          class: 'linkish', type: 'button',
          onclick: () => {
            const target = root.querySelector('[data-job-id="' + String(job.job_id) + '"]');
            if (target && typeof target.scrollIntoView === 'function') {
              target.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
          },
        }, job.name || job.job_id),
        h('span', { class: 'small muted' }, ' — ' + countText(Number(job.runs) || 1,
          ['запуск', 'запуска', 'запусков'])),
        ...(duty ? [h('span', { class: 'small muted' },
          ' · ' + ((duty.schedule_human) || 'по расписанию'))] : []));
    });
    detailEl.replaceChildren(h('div', { class: 'duties__day-card' },
      h('div', { class: 'duties__day-head' },
        h('strong', {}, dayTitle(day)),
        h('span', { class: 'small muted' }, ' · ' + daySummary(day)),
        h('button', {
          class: 'btn btn--secondary btn--sm duties__day-close', type: 'button',
          'data-action': 'duties-day-close',
          onclick: () => { expanded = ''; renderDayDetail(); },
        }, 'Свернуть')),
      lines.length ? h('ul', { class: 'duties__day-list' }, ...lines)
        : h('p', { class: 'small muted' },
          day.future ? 'Этот день ещё не наступил: запусков пока нет, план — ' + day.planned + '.'
            : 'В этот день запусков не было.')));
  }

  function renderList() {
    const items = (data && data.items) || [];
    listEl.replaceChildren();
    if (!items.length) {
      listEl.appendChild(h('div', { class: 'card duties__empty' },
        icon('clock', { size: 22 }),
        h('div', { class: 'empty__title' }, onlyRoom && roomId
          ? 'К этой комнате дежурства ещё не привязаны' : 'Дежурств пока нет'),
        h('p', { class: 'empty__text' },
          onlyRoom && roomId
            ? 'Покажите все дежурства Hermes и привяжите нужные — они появятся здесь.'
            : 'Дежурства берутся из Hermes: задания по расписанию (cron). Если их нет, их сначала создают в Hermes.'),
        h('div', { class: 'empty__actions' },
          ...(onlyRoom && roomId
            ? [h('button', {
              class: 'btn btn--primary', type: 'button', 'data-action': 'duties-show-all',
              onclick: () => { onlyRoom = false; renderFilter(); load(); },
            }, 'Показать все дежурства')]
            : []),
          h('button', {
            class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-refresh',
            onclick: () => load(),
          }, icon('refresh', { size: 13 }), 'Обновить'),
          h('a', { class: 'btn btn--secondary', href: '#/settings/diagnostics' },
            icon('debug-disconnect', { size: 13 }), 'Проверка системы'))));
      return;
    }
    const nowSec = Math.floor(Date.now() / 1000);
    const problems = items.filter((one) => dutyState(one).kind === 'fail');
    const others = items.filter((one) => dutyState(one).kind !== 'fail');
    const addGroup = (title, list) => {
      if (!list.length) return;
      listEl.appendChild(h('h3', { class: 'duties__group' }, title));
      for (const item of list) {
        listEl.appendChild(renderDuty(item, { roomId, now: nowSec, onAction: doAction }));
      }
    };
    addGroup('Требуют внимания', problems);
    addGroup(problems.length ? 'Остальные дежурства' : 'Все дежурства', others);
  }

  function renderAll() {
    renderSummary();
    renderWeekGrid();
    renderList();
  }

  async function load() {
    if (!api) return;
    listEl.replaceChildren(skeleton(3));
    try {
      const query = onlyRoom && roomId ? '?room=' + encodeURIComponent(roomId) : '?room=all';
      const fresh = await api.get('/api/duties' + query);
      if (stopped) return;
      data = fresh && typeof fresh === 'object' ? fresh : { items: [], week: {} };
      error = null;
      renderAll();
    } catch (err) {
      if (stopped) return;
      error = err;
      data = { items: [], week: {} };
      renderSummary();
      listEl.replaceChildren(errorBanner(err, () => load()));
      weekEl.replaceChildren(h('p', { class: 'small muted' }, 'Неделя не загрузилась.'));
      detailEl.replaceChildren();
    }
  }

  async function doAction(item, actionId) {
    if (!api || busy) return;
    const name = (item && item.name) || 'дежурство';
    let schedule = '';
    if (actionId === 'schedule') {
      const answer = await promptDialog({
        title: 'Изменить расписание — предложение',
        label: 'Новое расписание для «' + name + '»',
        initial: (item && (item.schedule || item.schedule_human)) || '',
        hint: 'Например: 0 9 * * 1-5 или every 2h. Это предложение: расписание в Hermes не меняется.',
        submitLabel: 'Предложить',
      });
      if (answer === null || answer === undefined) return;
      schedule = String(answer || '').trim();
      if (!schedule) {
        toast('Расписание не указано — изменение не предложено', 'warn');
        return;
      }
    }
    const ok = await confirmDialog({
      title: ACTIONS[actionId] ? ACTIONS[actionId].label : 'Действие',
      text: confirmText(actionId, item, schedule),
      confirmLabel: actionId === 'run' ? 'Запустить' : 'Да, подтверждаю',
      danger: actionId === 'run',
    });
    if (!ok) return;
    busy = actionId;
    root.setAttribute('data-busy', actionId);
    try {
      const body = { action: actionId, confirm: true };
      if (roomId) body.room = roomId;
      if (schedule) body.schedule = schedule;
      const res = await api.post('/api/duties/' + encodeURIComponent(String(item.job_id)) + '/actions', body);
      if (stopped) return;
      toast((res && res.text) || 'Готово', res && res.applied ? 'ok' : 'info');
      await load();
    } catch (err) {
      if (stopped) return;
      toast((err && err.message) || 'Действие не выполнено', 'danger');
    } finally {
      busy = '';
      root.removeAttribute('data-busy');
    }
  }

  renderFilter();
  await load();

  return {
    unmount() {
      stopped = true;
      root.remove();
    },
  };
}

/** Вердикт по сводке: одна строка человеческим языком. */
export function verdictText(summary, week) {
  const doc = summary || { total: 0, problems: 0, off: 0, running: 0 };
  if (!doc.total) return 'Дежурств нет — это не ошибка: просто ничего не идёт по расписанию.';
  if (doc.problems) {
    return 'Есть ' + countText(doc.problems, ['дежурство', 'дежурства', 'дежурств'])
      + ' с замечаниями — они сверху, у каждого есть действие.';
  }
  const weekDoc = weekModel(week);
  if (doc.off) return 'Красных пометок нет. На паузе: ' + doc.off + '.';
  return 'Красных пометок нет. ' + weekTotalsText(weekDoc) + '.';
}

export default { mount, weekGrid, renderDuty, actionList, confirmText };
