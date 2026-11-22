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
import {
  normalizeDraft, draftFromTemplate, draftFromProposal, draftFromDelo, draftFromRequest,
  draftFromDuty, templatesFrom, outcomeLine, historyRow, historyLine, trashModel, trashTitle,
  proposalModel, createBody, patchBody, previewModel, dryRunText, scheduleHuman, draftSummary,
  SOURCE_LABELS, localCommand, modelLine, folderPickBody,
} from './duty-model.js';
import { createWizard, WIZARD_TITLES } from './duty-wizard.js';

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
  // W27-E8: полный набор действий дежурства комнаты.
  edit: { label: 'Править', short: 'Править' },
  dryrun: { label: 'Проверить сейчас', short: 'Проверить' },
  duplicate: { label: 'Дублировать', short: 'Дублировать' },
  history: { label: 'История', short: 'История' },
  delete: { label: 'Удалить', short: 'Удалить', danger: true },
  restore: { label: 'Вернуть', short: 'Вернуть' },
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
    prev: doc.prev || '',
    next: doc.next || '',
    today: doc.today || '',
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
      plannedMore: Number(day.planned_more) || 0,
      more: Number(day.more) || 0,
      jobs: Array.isArray(day.jobs) ? day.jobs.filter((j) => j && typeof j === 'object') : [],
      // W29-P5: плановые запуски дня — для панели будущего дня.
      plannedJobs: Array.isArray(day.planned_jobs)
        ? day.planned_jobs.filter((j) => j && typeof j === 'object') : [],
    })),
  };
}

/** Подпись недели: «16–22 ноября 2026» (для стрелок ‹ › и «Сегодня»). */
export function weekRangeText(week) {
  const doc = weekModel(week);
  if (!doc.days.length) return '';
  const first = doc.days[0];
  const last = doc.days[doc.days.length - 1];
  const from = Number(first.day) || 0;
  const to = Number(last.day) || 0;
  const fromMonth = MONTHS_GEN[Math.max(0, Math.min(11, Number(first.month || 1) - 1))];
  const toMonth = MONTHS_GEN[Math.max(0, Math.min(11, Number(last.month || 1) - 1))];
  const year = String(doc.start || '').slice(0, 4);
  if (fromMonth === toMonth) return from + '–' + to + ' ' + fromMonth + (year ? ' ' + year : '');
  return from + ' ' + fromMonth + ' – ' + to + ' ' + toMonth + (year ? ' ' + year : '');
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
    case 'dryrun':
      return 'Проверить «' + name + '» сейчас, ничего не записывая? '
        + 'Это сухой прогон: покажет, что бы нашло, но во «Входящие» ничего не попадёт.';
    case 'delete':
      return 'Убрать «' + name + '» в корзину? Задание в Hermes будет удалено по вашей команде, '
        + 'но вернуть его можно 30 дней — из корзины на этой же странице.';
    case 'restore':
      return 'Вернуть «' + name + '» из корзины? Расписание начнёт работать по-прежнему.';
    case 'duplicate':
      return 'Создать копию «' + name + '»? Копия появится на паузе — включите, когда проверите.';
    case 'edit':
      return 'Открыть правку «' + name + '»? Перед сохранением будет предпросмотр изменений.';
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

/**
 * Полный набор действий карточки (W27-E8): основные в один ряд, остальные —
 * во второй. У каждого пункта есть действие — правило «без тупиков».
 * @param {object} item дежурство
 * @param {string} roomId текущая комната ('' — общий список)
 */
export function fullActionList(item, roomId) {
  const doc = item && typeof item === 'object' ? item : {};
  const bound = Array.isArray(doc.bound_rooms) ? doc.bound_rooms.map((x) => String(x)) : [];
  const main = [
    { id: 'run', label: ACTIONS.run.label, primary: true },
    doc.enabled === false
      ? { id: 'resume', label: ACTIONS.resume.label }
      : { id: 'pause', label: ACTIONS.pause.label },
    { id: 'edit', label: ACTIONS.edit.label },
  ];
  const more = [
    { id: 'dryrun', label: ACTIONS.dryrun.label, secondary: true },
    { id: 'history', label: ACTIONS.history.label, secondary: true },
    { id: 'duplicate', label: ACTIONS.duplicate.label, secondary: true },
  ];
  if (roomId) {
    more.push(bound.includes(String(roomId))
      ? { id: 'unbind', label: ACTIONS.unbind.label, secondary: true }
      : { id: 'bind', label: ACTIONS.bind.label, secondary: true });
  }
  more.push({ id: 'delete', label: ACTIONS.delete.label, secondary: true, danger: true });
  return [...main, ...more];
}

/** Итог последнего запуска в карточке: «изменений нет» / «найдено 2 письма → предложено Дело». */
export function outcomeBlock(item) {
  const doc = item && typeof item === 'object' ? item : {};
  const text = outcomeLine(doc);
  const outcome = doc.outcome && typeof doc.outcome === 'object' ? doc.outcome : {};
  const changed = String(outcome.changed || '').toLowerCase();
  const kind = changed === 'да' || changed === 'yes' ? 'changed' : 'calm';
  return h('p', { class: 'duty__outcome duty__outcome--' + kind, 'data-role': 'duty-outcome' },
    icon(kind === 'changed' ? 'bell' : 'check', { size: 12 }),
    h('span', { class: 'duty__outcome-text' }, ' ' + text));
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

  const list = Array.isArray(opts.actions) ? opts.actions : actionList(item, roomId);
  const acts = list.filter((act) => !act.secondary);
  const extra = list.filter((act) => act.secondary);

  const actions = h('div', { class: 'duty__actions' });
  for (const act of acts) {
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
  if (extra.length) {
    const more = h('div', { class: 'duty__actions duty__actions--more' });
    for (const act of extra) {
      more.appendChild(h('button', {
        class: 'btn btn--ghost btn--sm' + (act.danger ? ' duty__danger' : ''),
        type: 'button',
        'data-action': act.id,
        'data-job-id': String((item && item.job_id) || ''),
        title: confirmText(act.id, item),
        onclick: () => { if (typeof opts.onAction === 'function') opts.onAction(item, act.id); },
      }, act.label));
    }
    actions.appendChild(more);
  }

  const chips = h('div', { class: 'duty__chips' },
    chip(item && item.enabled === false ? 'выключено' : 'включено',
      item && item.enabled === false ? 'off' : 'ok',
      'Дежурство ' + (item && item.enabled === false ? 'выключено' : 'включено') + ' в Hermes'),
    ...(problem && problem.kind !== 'disabled_but_due'
      ? [chip(PROBLEM_LABELS[problem.kind] || 'замечание', 'fail', problem.text || '')]
      : []),
    ...(item && item.source ? [chip(SOURCE_LABELS[String(item.source)] || 'своё',
      'muted', 'Откуда взялось это дежурство')] : []),
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

  // W29-P5: «Куда придёт результат» — ссылка в «Входящие» комнаты, а не догадка.
  const deliver = item && item.deliver && typeof item.deliver === 'object' ? item.deliver : null;
  if (deliver) {
    const line = h('p', { class: 'duty__deliver small', 'data-role': 'duty-deliver' },
      icon(deliver.where === 'inbox' ? 'inbox' : 'info', { size: 12 }),
      ' Результат: ' + (deliver.label || 'не в комнату'));
    if (deliver.href) {
      line.appendChild(h('a', {
        class: 'duty__deliver-link', href: deliver.href,
        title: deliver.text || 'Открыть «Входящие» комнаты',
      }, ' — открыть входящие'));
    } else if (deliver.text) {
      line.appendChild(h('span', { class: 'muted' }, ' — ' + deliver.text));
    }
    card.appendChild(line);
  }

  // W29-P5: кто выполняет и на какой модели — «дешёвая по умолчанию» больше не загадка.
  card.appendChild(h('p', { class: 'duty__model small muted', 'data-role': 'duty-model' },
    icon('hubot', { size: 12 }),
    ' ' + modelLine(item && item.kind === 'monitor' ? { ...item, kind: 'watch' } : item,
      opts.catalog),
    h('span', { class: 'duty__model-hint' }, ' · сменить — кнопкой «Править»')));

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

  // W27-E8: итог последнего запуска одной фразой — то, ради чего открывают раздел.
  if (item && item.outcome && typeof item.outcome === 'object') {
    card.appendChild(outcomeBlock(item));
  }
  if (Array.isArray(opts.history)) {
    card.appendChild(historyPanel(opts.history, {
      jobId: String((item && item.job_id) || ''),
      error: opts.historyError || '',
      loading: !!opts.historyLoading,
    }));
  }
  if (opts.dryRun) card.appendChild(dryRunBlock(opts.dryRun));
  return card;
}

/* ─────────────────────────────────────────── W27-E8: новые части экрана */

/**
 * История запусков дежурства: что нашла, что сделала, что предложила.
 * @param {Array} items строки `GET /api/duties/{id}/history`
 */
export function historyPanel(items, opts = {}) {
  const rows = (Array.isArray(items) ? items : []).map((one) => historyRow(one, timeText));
  const box = h('section', { class: 'duty__history', 'data-role': 'duty-history' });
  box.appendChild(h('h4', { class: 'duty__history-title' }, 'История запусков'));
  if (opts.loading) {
    box.appendChild(h('p', { class: 'small muted' }, 'Читаю историю…'));
    return box;
  }
  if (opts.error) {
    box.appendChild(h('p', { class: 'small duty__history-error' }, 'История не загрузилась: ' + opts.error));
    return box;
  }
  if (!rows.length) {
    box.appendChild(h('p', { class: 'small muted' },
      'Запусков пока не было. Первый появится по расписанию — или нажмите «Запустить сейчас».'));
    return box;
  }
  const list = h('ul', { class: 'duty__history-list' });
  for (const row of rows) {
    const item = h('li', {
      class: 'duty__history-row' + (row.changed ? ' duty__history-row--changed' : ''),
      'data-run-id': row.runId,
    },
    h('div', { class: 'duty__history-head' },
      h('span', { class: 'duty__history-time' }, row.time || 'время неизвестно'),
      h('span', {
        class: 'duty__history-changed' + (row.changed ? ' duty__history-changed--yes' : ''),
        title: row.changed ? 'Запуск нашёл изменение' : 'Изменений не было — тихая строка',
      }, row.changedLabel)),
    row.done ? h('p', { class: 'duty__history-line small' }, 'Что выполнено: ' + row.done) : null,
    row.found ? h('p', { class: 'duty__history-line small' }, 'Что обнаружено: ' + row.found) : null,
    ...(row.proposals.length
      ? [h('ul', { class: 'duty__history-props' },
        ...row.proposals.map((p) => h('li', { class: 'small' },
          (p.what || 'предложение') + (p.why ? ' — ' + p.why : '')
            + (p.action ? ' → ' + p.action : ''))))]
      : []),
    ...(row.created.length
      ? [h('p', { class: 'duty__history-line small muted' }, 'Создано: ' + row.created.join(', '))]
      : []));
    list.appendChild(item);
  }
  box.appendChild(list);
  return box;
}

/** Результат сухого прогона: «что бы нашла» — без побочных действий. */
export function dryRunBlock(result) {
  const doc = result && typeof result === 'object' ? result : {};
  const text = dryRunText(doc);
  return h('div', { class: 'duty__dry', 'data-role': 'duty-dry' },
    h('span', { class: 'duty__dry-title' }, 'Проверка без записи: '),
    h('span', {}, text),
    h('span', { class: 'duty__dry-note small muted' },
      ' Ничего не создано и не отправлено — это только предпросмотр.'));
}

/** Карточка «Предлагаю дежурство» с четырьмя действиями. */
export function proposalCard(proposal, opts = {}) {
  const doc = proposalModel(proposal);
  const busy = !!opts.busy;
  const card = h('article', {
    class: 'proposal', 'data-proposal-id': doc.id, 'data-role': 'duty-proposal',
  });
  card.appendChild(h('header', { class: 'proposal__head' },
    h('div', { class: 'proposal__titlebox' },
      h('span', { class: 'proposal__kicker' }, 'Предлагаю дежурство'),
      h('h3', { class: 'proposal__title' }, doc.title)),
    h('span', { class: 'proposal__source small muted' }, doc.sourceLabel)));
  if (doc.why) card.appendChild(h('p', { class: 'proposal__why' }, doc.why));
  const facts = h('dl', { class: 'proposal__facts' });
  if (doc.when) facts.appendChild(h('div', {}, h('dt', {}, 'Когда'), h('dd', {}, doc.when)));
  if (doc.check) facts.appendChild(h('div', {}, h('dt', {}, 'Что проверять'), h('dd', {}, doc.check)));
  if (doc.onChange) facts.appendChild(h('div', {}, h('dt', {}, 'При изменении'), h('dd', {}, doc.onChange)));
  card.appendChild(facts);
  const actions = h('div', { class: 'proposal__actions' });
  const buttons = [
    { id: 'accept', label: 'Создать', primary: true, title: 'Создать дежурство по этому предложению — с предпросмотром' },
    { id: 'edit', label: 'Изменить', title: 'Открыть мастер с этим черновиком: поменять и сохранить' },
    { id: 'dismiss', label: 'Не надо', title: 'Отклонить предложение. Дежурство не создаётся' },
    { id: 'never', label: 'Не предлагать такое', title: 'Больше не предлагать дежурства этого образца' },
  ];
  for (const btn of buttons) {
    actions.appendChild(h('button', {
      class: 'btn ' + (btn.primary ? 'btn--primary' : 'btn--secondary') + ' btn--sm',
      type: 'button', 'data-action': 'proposal-' + btn.id, 'data-proposal-id': doc.id,
      title: btn.title,
      ...(busy ? { disabled: true } : {}),
      onclick: () => { if (typeof opts.onAction === 'function') opts.onAction(doc, btn.id); },
    }, btn.label));
  }
  card.appendChild(actions);
  if (doc.limitText) card.appendChild(h('p', { class: 'small muted' }, doc.limitText));
  return card;
}

/** Шаблоны дежурств: карточки «взять за основу». */
export function templateCards(templates, opts = {}) {
  const list = Array.isArray(templates) ? templates : [];
  const grid = h('div', { class: 'tpl', 'data-role': 'duty-templates' });
  if (!list.length) {
    grid.appendChild(h('p', { class: 'small muted' },
      'Шаблоны не загрузились. Можно создать дежурство вручную — кнопка «Создать дежурство».'));
    return grid;
  }
  for (const tpl of list) {
    grid.appendChild(h('article', { class: 'tpl__card', 'data-template': tpl.id },
      h('h3', { class: 'tpl__title' }, tpl.title),
      tpl.for ? h('p', { class: 'tpl__for small muted' }, tpl.for) : null,
      h('dl', { class: 'tpl__facts' },
        tpl.when ? h('div', {}, h('dt', {}, 'Когда'), h('dd', {}, tpl.when)) : null,
        tpl.change ? h('div', {}, h('dt', {}, 'Изменение'), h('dd', {}, tpl.change)) : null,
        tpl.action ? h('div', {}, h('dt', {}, 'Что делает'), h('dd', {}, tpl.action)) : null),
      h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        'data-action': 'use-template', 'data-template': tpl.id,
        title: 'Открыть мастер с заполненными полями — можно поправить перед созданием',
        onclick: () => { if (typeof opts.onPick === 'function') opts.onPick(tpl); },
      }, 'Взять шаблон')));
  }
  return grid;
}

/** Корзина: что можно вернуть и сколько дней осталось. */
export function trashPanel(items, opts = {}) {
  const rows = trashModel(items, opts.now);
  const box = h('section', { class: 'trash', 'data-role': 'duty-trash' });
  box.appendChild(h('h3', { class: 'trash__title' }, trashTitle(items)));
  if (!rows.length) {
    box.appendChild(h('p', { class: 'small muted' },
      'Корзина пуста. Удалённые дежурства хранятся 30 дней — их можно вернуть.'));
    return box;
  }
  const list = h('ul', { class: 'trash__list' });
  for (const row of rows) {
    list.appendChild(h('li', { class: 'trash__row', 'data-job-id': row.jobId },
      h('div', { class: 'trash__info' },
        h('span', { class: 'trash__name' }, row.title),
        h('span', { class: 'small muted' }, ' · ' + row.daysText)),
      h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        'data-action': 'restore', 'data-job-id': row.jobId,
        title: 'Вернуть дежурство: расписание снова начнёт работать',
        onclick: () => { if (typeof opts.onRestore === 'function') opts.onRestore(row); },
      }, ACTIONS.restore.label)));
  }
  box.appendChild(list);
  return box;
}

/** Выбор Дела для «Сделать регулярным». */
export function deloPicker(delos, opts = {}) {
  const list = Array.isArray(delos) ? delos.filter((one) => one && typeof one === 'object') : [];
  const requests = Array.isArray(opts.requests)
    ? opts.requests.filter((one) => one && typeof one === 'object') : [];
  const box = h('section', { class: 'picker', 'data-role': 'delo-picker' });
  box.appendChild(h('h3', { class: 'picker__title' }, 'Сделать регулярным из дела'));
  box.appendChild(h('p', { class: 'small muted' },
    'Выберите дело — мастер заполнится его названием. Ничего не создаётся до подтверждения.'));
  if (!list.length) {
    // W29-P5: пустой список — не тупик: предлагаем запрос комнаты или своё дежурство.
    box.appendChild(h('p', { class: 'small muted' },
      'Дел в этой комнате пока нет. Можно взять недавний запрос — из него получится '
        + 'такая же регулярная проверка — или создать дежурство с нуля.'));
    if (requests.length) {
      const rows = h('ul', { class: 'picker__list' });
      for (const request of requests.slice(0, 10)) {
        rows.appendChild(h('li', { class: 'picker__row' },
          h('button', {
            class: 'picker__btn', type: 'button', 'data-request': String(request.id || ''),
            onclick: () => { if (typeof opts.onPickRequest === 'function') opts.onPickRequest(request); },
          },
          h('span', { class: 'picker__name' }, String(request.title || 'запрос комнаты')),
          h('span', { class: 'small muted' }, ' · запрос'))));
      }
      box.appendChild(rows);
    }
    box.appendChild(h('div', { class: 'picker__actions' },
      h('button', {
        class: 'btn btn--primary btn--sm', type: 'button', 'data-action': 'duty-create',
        onclick: () => { if (typeof opts.onCreate === 'function') opts.onCreate(); },
      }, 'Создать дежурство с нуля'),
      h('button', {
        class: 'btn btn--ghost btn--sm', type: 'button', 'data-action': 'picker-cancel',
        onclick: () => { if (typeof opts.onCancel === 'function') opts.onCancel(); },
      }, 'Отмена')));
    return box;
  }
  const rows = h('ul', { class: 'picker__list' });
  for (const delo of list.slice(0, 12)) {
    rows.appendChild(h('li', { class: 'picker__row' },
      h('button', {
        class: 'picker__btn', type: 'button', 'data-delo': String(delo.id || ''),
        onclick: () => { if (typeof opts.onPick === 'function') opts.onPick(delo); },
      },
      h('span', { class: 'picker__name' }, String(delo.title || delo.name || 'дело')),
      h('span', { class: 'small muted' }, ' · ' + String(delo.status_label || delo.status || '')))));
  }
  box.appendChild(rows);
  box.appendChild(h('button', {
    class: 'btn btn--ghost btn--sm', type: 'button', 'data-action': 'picker-cancel',
    onclick: () => { if (typeof opts.onCancel === 'function') opts.onCancel(); },
  }, 'Отмена'));
  return box;
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
  const query = (ctx && ctx.route && ctx.route.query) || {};

  let stopped = false;
  let data = null;
  let templates = [];
  let onlyRoom = !!roomId;
  let busy = '';
  let expanded = '';
  let error = null;
  let mode = 'list';                 // list | wizard | templates | trash | picker
  let wizard = null;
  let openJob = '';                  // чья история раскрыта
  const historyCache = {};           // job_id → {items, loading, error}
  const dryCache = {};               // job_id → ответ сухого прогона
  let pickerDelos = null;
  let pickerRequests = [];
  let proposalBusy = '';
  // W29-P5: каталог моделей (только чтение), листание недель и «только замечания».
  let catalog = null;
  let weekStart = '';
  let onlyProblems = false;

  const title = h('h1', { class: 'duties__title' }, 'Дежурства');
  const lead = h('p', { class: 'duties__lead muted' },
    'Задания по расписанию: что идёт само, когда запускалось и что требует вас. '
      + 'Действия — только по вашему подтверждению.');

  const refreshBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-refresh',
    onclick: () => load(),
  }, icon('refresh', { size: 13 }), 'Обновить');

  const createBtn = h('button', {
    class: 'btn btn--primary', type: 'button', 'data-action': 'duty-create',
    title: 'Мастер: что проверять → когда → что считать изменением → что делать',
    onclick: () => openWizard(normalizeDraft({
      mode: 'create', room: roomId, source: 'manual',
    })),
  }, icon('add', { size: 13 }), 'Создать дежурство');

  const templatesBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-action': 'duty-templates',
    onclick: () => { mode = mode === 'templates' ? 'list' : 'templates'; renderAll(); },
  }, icon('library', { size: 13 }), 'Шаблоны');

  const trashBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-trash',
    onclick: () => { mode = mode === 'trash' ? 'list' : 'trash'; renderAll(); },
  }, icon('trash', { size: 13 }), 'Корзина');

  const regularBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-action': 'duty-from-delo',
    title: 'Взять Дело комнаты и сделать из него регулярную проверку',
    onclick: () => openPicker(),
  }, icon('checklist', { size: 13 }), 'Сделать регулярным из дела');

  const filterBtn = h('button', {
    class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-filter',
    onclick: () => { onlyRoom = !onlyRoom; renderFilter(); load(); },
  }, icon('folder-opened', { size: 13 }));

  const summaryEl = h('div', { class: 'duties__summary', role: 'status', 'aria-live': 'polite' });
  const proposalsEl = h('div', { class: 'duties__proposals', 'data-role': 'duties-proposals' });
  // W29-P5: стрелки ‹ › и «Сегодня» — неделю можно листать, как календарь.
  const weekPrev = h('button', {
    class: 'btn btn--secondary btn--sm', type: 'button', 'data-action': 'duties-week-prev',
    'aria-label': 'Предыдущая неделя', title: 'Предыдущая неделя',
    onclick: () => goWeek((data && data.week && data.week.prev) || ''),
  }, '‹');
  const weekToday = h('button', {
    class: 'btn btn--secondary btn--sm', type: 'button', 'data-action': 'duties-week-today',
    title: 'Показать текущую неделю',
    onclick: () => { weekStart = ''; expanded = ''; load(); },
  }, 'Сегодня');
  const weekNext = h('button', {
    class: 'btn btn--secondary btn--sm', type: 'button', 'data-action': 'duties-week-next',
    'aria-label': 'Следующая неделя', title: 'Следующая неделя',
    onclick: () => goWeek((data && data.week && data.week.next) || ''),
  }, '›');
  const weekLabel = h('span', { class: 'duties__week-label small', 'data-role': 'duties-week-label' }, '');
  const weekEl = h('div', { class: 'duties__week' });
  const detailEl = h('div', { class: 'duties__day', 'data-role': 'duties-day' });
  const templatesEl = h('div', { class: 'duties__templates', hidden: true });
  const wizardEl = h('div', { class: 'duties__wizard', 'data-role': 'duty-wizard-host', hidden: true });
  const pickerEl = h('div', { class: 'duties__picker', hidden: true });
  const listEl = h('div', { class: 'duties__list', 'data-role': 'duties-list' });
  const trashEl = h('div', { class: 'duties__trash', hidden: true });
  const noteEl = h('p', { class: 'duties__note small muted' },
    'Расписание ведёт Hermes: создание, правка, пауза и удаление — штатной командой и только после вашего «да». '
      + 'Перед созданием всегда есть предпросмотр точной команды.');

  const head = h('header', { class: 'duties__head' },
    h('div', { class: 'duties__headline' }, title, lead),
    h('div', { class: 'duties__head-actions' },
      createBtn, templatesBtn, trashBtn, regularBtn, filterBtn, refreshBtn));

  const weekHead = h('div', { class: 'duties__section-head' },
    h('h2', { class: 'duties__section-title' }, 'Неделя'),
    h('span', { class: 'duties__section-hint small muted' }, 'запуски по дням, как в Mission Control'),
    h('div', { class: 'duties__week-nav' }, weekPrev, weekLabel, weekNext, weekToday));

  const listHead = h('h2', { class: 'duties__section-title duties__section-title--list' }, 'Список дежурств');

  // W29-P5: системные задачи Hermes — свёрнутый блок «только чтение», не дежурства комнаты.
  const systemEl = h('details', { class: 'duties__system', 'data-role': 'duties-system', hidden: true });
  const weekBlocks = h('div', { class: 'duties__weekblock' }, weekHead, weekEl, detailEl);
  const root = h('div', { class: 'view__inner duties' },
    head, proposalsEl, summaryEl, weekBlocks,
    wizardEl, templatesEl, pickerEl, listHead, listEl, systemEl, trashEl, noteEl);
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
    const all = (data && data.items) || [];
    // W29-P5: счётчики — по дежурствам комнат; системные задачи Hermes считаются отдельно.
    const items = all.filter((one) => one && one.scope !== 'system');
    const summary = dutiesSummary(items);
    const week = weekModel(data && data.week);
    const trashCount = Array.isArray(data && data.trash) ? data.trash.length : 0;
    // W29-P5: «N с замечаниями» — кнопка: нажал → список показывает только их.
    const problemsChip = summary.problems
      ? h('button', {
        class: 'duty-sum duty-sum--fail duty-sum--button'
          + (onlyProblems ? ' duty-sum--on' : ''),
        type: 'button', 'data-action': 'duties-problems',
        'aria-pressed': onlyProblems ? 'true' : 'false',
        title: onlyProblems ? 'Показать все дежурства' : 'Показать только дежурства с замечаниями',
        onclick: () => {
          onlyProblems = !onlyProblems;
          renderAll();
          if (onlyProblems && typeof listEl.scrollIntoView === 'function') {
            listEl.scrollIntoView({ block: 'start', behavior: 'smooth' });
          }
        },
      }, countText(summary.problems, ['дежурство', 'дежурства', 'дежурств']) + ' с замечаниями',
      onlyProblems ? ' · показать все' : '')
      : null;
    summaryEl.replaceChildren(
      summaryChip(countText(summary.total, ['дежурство', 'дежурства', 'дежурств']), 'plain',
        'Всего дежурств в этом списке'),
      ...(problemsChip ? [problemsChip] : []),
      ...(summary.off ? [summaryChip(summary.off + ' выключено', 'off', 'Дежурства на паузе')] : []),
      ...(summary.running ? [summaryChip(summary.running + ' идёт сейчас', 'run', 'Запуск в работе')] : []),
      summaryChip(weekTotalsText(week), 'muted', 'Запуски за эту неделю'),
      ...(week.error ? [summaryChip('неделя не посчитана', 'fail', week.error)] : []));
    summaryEl.appendChild(h('p', { class: 'duties__verdict' }, verdictText(summary, week)));
    trashBtn.replaceChildren(icon('trash', { size: 13 }), trashTitle(data && data.trash));
    trashBtn.setAttribute('aria-pressed', mode === 'trash' ? 'true' : 'false');
    templatesBtn.setAttribute('aria-pressed', mode === 'templates' ? 'true' : 'false');
    if (trashCount) trashBtn.classList.add('btn--has-count');
    else trashBtn.classList.remove('btn--has-count');
  }

  /** W29-P5: листание недель — стрелки ‹ › берут дату из ответа сервера. */
  function goWeek(iso) {
    if (!iso) return;
    weekStart = String(iso).slice(0, 10);
    expanded = '';
    load();
  }

  /** W29-P5: системные задачи Hermes — отдельно, свёрнуто, без кнопок действий. */
  function renderSystem() {
    const list = Array.isArray(data && data.system) ? data.system : [];
    systemEl.hidden = !list.length;
    if (!list.length) {
      systemEl.replaceChildren();
      return;
    }
    const rows = list.map((item) => {
      // Одно состояние, без «на паузе · выключено»: у системных задач это одно и то же.
      let state = item.enabled === false ? { text: 'на паузе', cls: 'duties__system-problem' }
        : item.problem ? { text: PROBLEM_LABELS[item.problem.kind] || 'замечание',
          cls: 'duties__system-problem' }
          : { text: 'включено', cls: 'muted' };
      return h('li', { class: 'duties__system-row', 'data-job-id': item.job_id },
        h('span', { class: 'duties__system-name' }, item.name || item.job_id),
        h('span', { class: 'small muted' }, ' · ' + (item.schedule_human || item.schedule || 'по расписанию')),
        h('span', { class: 'small ' + state.cls }, ' · ' + state.text));
    });
    systemEl.replaceChildren(
      h('summary', { class: 'duties__system-summary' },
        'Системные задачи Hermes — ' + countText(list.length, ['задача', 'задачи', 'задач']),
        h('span', { class: 'small muted' }, ' · не относятся к комнатам, только чтение')),
      h('p', { class: 'small muted duties__system-note' },
        'Это общие задания Hermes на этой машине: обслуживание, копии, напоминания. '
          + 'Они не созданы вами в комнате и в календарь комнаты не входят. '
          + 'Показаны для полноты — действий здесь нет.'),
      h('ul', { class: 'duties__system-list' }, ...rows));
  }

  function renderProposals() {
    const list = Array.isArray(data && data.proposals) ? data.proposals : [];
    proposalsEl.replaceChildren();
    proposalsEl.hidden = !list.length;
    for (const proposal of list) {
      proposalsEl.appendChild(proposalCard(proposal, {
        busy: proposalBusy === String(proposal.id || ''),
        onAction: actOnProposal,
      }));
    }
  }

  function renderWeekGrid() {
    const week = weekModel(data && data.week);
    weekLabel.textContent = weekRangeText(week);
    weekPrev.disabled = !week.prev;
    weekNext.disabled = !week.next;
    weekToday.hidden = !weekStart;
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
    const scrollToDuty = (jobId) => {
      const target = root.querySelector('[data-job-id="' + String(jobId) + '"]');
      if (target && typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        target.classList.add('duty--picked');
        setTimeout(() => target.classList.remove('duty--picked'), 1600);
      }
    };
    const lines = day.jobs.map((job) => {
      const duty = items.find((one) => String(one.job_id) === String(job.job_id));
      return h('li', { class: 'duties__day-line' },
        h('button', {
          class: 'linkish', type: 'button', 'data-action': 'duties-day-open',
          'data-job-id': String(job.job_id),
          onclick: () => scrollToDuty(job.job_id),
        }, job.name || job.job_id),
        h('span', { class: 'small muted' }, ' — ' + countText(Number(job.runs) || 1,
          ['запуск', 'запуска', 'запусков'])),
        ...(duty ? [h('span', { class: 'small muted' },
          ' · ' + ((duty.schedule_human) || 'по расписанию'))] : []));
    });
    // W29-P5: в будущем дне запусков нет — показываем ПЛАНОВЫЕ запуски по расписанию.
    const plannedLines = day.plannedJobs.map((job) => h('li', {
      class: 'duties__day-line duties__day-line--planned', 'data-job-id': String(job.job_id),
    },
    h('button', {
      class: 'linkish', type: 'button', 'data-action': 'duties-day-open',
      'data-job-id': String(job.job_id),
      onclick: () => scrollToDuty(job.job_id),
    }, job.name || job.job_id),
    h('span', { class: 'small muted' }, ' — план: ' + countText(Number(job.runs) || 1,
      ['запуск', 'запуска', 'запусков'])),
    day.plannedMore ? h('span', { class: 'small muted' }, ' · и ещё ' + day.plannedMore) : null));
    const body = [];
    if (lines.length) {
      body.push(h('h4', { class: 'duties__day-subtitle small' }, day.future ? 'Запусков ещё не было' : 'Что запускалось'),
        h('ul', { class: 'duties__day-list' }, ...lines));
    }
    if (plannedLines.length) {
      body.push(h('h4', { class: 'duties__day-subtitle small' }, 'По расписанию в этот день'),
        h('ul', { class: 'duties__day-list duties__day-list--planned' }, ...plannedLines));
    }
    if (!body.length) {
      body.push(h('p', { class: 'small muted' },
        day.future ? 'Этот день ещё не наступил: по расписанию запусков нет.'
          : 'В этот день запусков не было.'));
    }
    detailEl.replaceChildren(h('div', { class: 'duties__day-card' },
      h('div', { class: 'duties__day-head' },
        h('strong', {}, dayTitle(day)),
        h('span', { class: 'small muted' }, ' · ' + daySummary(day)),
        h('button', {
          class: 'btn btn--secondary btn--sm duties__day-close', type: 'button',
          'data-action': 'duties-day-close',
          onclick: () => { expanded = ''; renderDayDetail(); },
        }, 'Свернуть')),
      ...body));
  }

  function renderTemplates() {
    templatesEl.hidden = mode !== 'templates';
    if (templatesEl.hidden) return;
    templatesEl.replaceChildren(
      h('div', { class: 'duties__section-head' },
        h('h2', { class: 'duties__section-title' }, 'Шаблоны дежурств'),
        h('span', { class: 'duties__section-hint small muted' },
          'готовые заготовки: откроется мастер с заполненными полями')),
      templateCards(templates, { onPick: (tpl) => openWizard(draftFromTemplate(tpl.raw || tpl, roomId), WIZARD_TITLES.template) }));
  }

  function renderTrash() {
    trashEl.hidden = mode !== 'trash';
    if (trashEl.hidden) return;
    trashEl.replaceChildren(trashPanel((data && data.trash) || [], {
      onRestore: restoreDuty,
    }));
  }

  function renderPicker() {
    pickerEl.hidden = mode !== 'picker';
    if (pickerEl.hidden) return;
    pickerEl.replaceChildren(deloPicker(pickerDelos || [], {
      requests: pickerRequests || [],
      onPick: (delo) => openWizard(draftFromDelo(delo, roomId), WIZARD_TITLES.delo),
      onPickRequest: (request) => openWizard(draftFromRequest(request, roomId), WIZARD_TITLES.request),
      onCreate: () => openWizard(normalizeDraft({ mode: 'create', room: roomId }), WIZARD_TITLES.create),
      onCancel: () => { mode = 'list'; renderAll(); },
    }));
  }

  function renderList() {
    const all = (data && data.items) || [];
    // W29-P5: системные задачи Hermes — отдельным блоком, не в списке дежурств комнаты.
    const roomItems = all.filter((one) => one && one.scope !== 'system');
    const items = onlyProblems
      ? roomItems.filter((one) => dutyState(one).kind === 'fail')
      : roomItems;
    listEl.replaceChildren();
    listHead.hidden = mode !== 'list';
    listEl.hidden = mode !== 'list';
    if (mode !== 'list') return;
    if (!items.length && onlyProblems) {
      listEl.appendChild(h('div', { class: 'card duties__empty' },
        icon('check', { size: 22 }),
        h('div', { class: 'empty__title' }, 'Замечаний нет'),
        h('p', { class: 'empty__text' },
          'Ни одно дежурство комнаты не жалуется: запуски идут по расписанию.'),
        h('div', { class: 'empty__actions' },
          h('button', {
            class: 'btn btn--primary', type: 'button', 'data-action': 'duties-problems',
            onclick: () => { onlyProblems = false; renderAll(); },
          }, 'Показать все дежурства'))));
      return;
    }
    if (!items.length) {
      listEl.appendChild(h('div', { class: 'card duties__empty' },
        icon('clock', { size: 22 }),
        h('div', { class: 'empty__title' }, onlyRoom && roomId
          ? 'К этой комнате дежурства ещё не привязаны' : 'Дежурств пока нет'),
        h('p', { class: 'empty__text' },
          'Дежурство — это регулярная проверка: «каждое утро проверять, что нового в ящике», '
            + '«следить за папкой выгрузок», «проверять, не изменилась ли страница с ценами». '
            + 'Результат придёт во «Входящие» комнаты — только если что-то изменилось. '
            + 'Начните с готового шаблона: поля уже заполнены, останется поправить.'),
        h('div', { class: 'empty__actions' },
          h('button', {
            class: 'btn btn--primary', type: 'button', 'data-action': 'duty-create',
            onclick: () => openWizard(normalizeDraft({ mode: 'create', room: roomId })),
          }, 'Создать дежурство'),
          h('button', {
            class: 'btn btn--secondary', type: 'button', 'data-action': 'duty-templates',
            onclick: () => { mode = 'templates'; renderAll(); },
          }, 'Шаблоны'),
          ...(onlyRoom && roomId
            ? [h('button', {
              class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-show-all',
              onclick: () => { onlyRoom = false; renderFilter(); load(); },
            }, 'Показать все дежурства')]
            : []),
          h('button', {
            class: 'btn btn--secondary', type: 'button', 'data-action': 'duties-refresh',
            onclick: () => load(),
          }, icon('refresh', { size: 13 }), 'Обновить'))));
      return;
    }
    const nowSec = Math.floor(Date.now() / 1000);
    const problems = items.filter((one) => dutyState(one).kind === 'fail');
    const others = items.filter((one) => dutyState(one).kind !== 'fail');
    const addGroup = (groupTitle, list) => {
      if (!list.length) return;
      listEl.appendChild(h('h3', { class: 'duties__group' }, groupTitle));
      for (const item of list) {
        const jobId = String(item.job_id || '');
        const cache = historyCache[jobId] || {};
        listEl.appendChild(renderDuty(item, {
          roomId,
          now: nowSec,
          catalog,
          actions: fullActionList(item, roomId),
          onAction: doAction,
          history: openJob === jobId ? (cache.items || []) : null,
          historyError: cache.error || '',
          historyLoading: !!cache.loading,
          dryRun: dryCache[jobId] || null,
        }));
      }
    };
    addGroup('Требуют внимания', problems);
    addGroup(problems.length ? 'Остальные дежурства' : 'Все дежурства', others);
  }

  function renderAll() {
    const wizardOpen = mode === 'wizard';
    root.setAttribute('data-mode', mode);
    createBtn.setAttribute('aria-pressed', wizardOpen ? 'true' : 'false');
    renderFilter();
    renderSummary();
    renderProposals();
    weekBlocks.hidden = wizardOpen;
    renderWeekGrid();
    renderTemplates();
    renderTrash();
    renderPicker();
    renderSystem();
    wizardEl.hidden = !wizardOpen;
    if (!wizardOpen) wizardEl.replaceChildren();
    renderList();
  }

  async function load() {
    if (!api) return;
    listEl.replaceChildren(skeleton(3));
    try {
      let params = onlyRoom && roomId ? '?room=' + encodeURIComponent(roomId) : '?room=all';
      if (weekStart) params += '&start=' + encodeURIComponent(weekStart);
      const jobs = [api.get('/api/duties' + params)];
      if (!templates.length) {
        jobs.push(api.get('/api/duty-templates').catch(() => ({ items: [] })));
      }
      // W29-P5: каталог моделей для выбора в мастере — только чтение, отказ не мешает.
      if (!catalog) {
        jobs.push(api.get('/api/models').catch(() => null));
      }
      const [fresh, tpl, models] = await Promise.all(jobs);
      if (stopped) return;
      data = fresh && typeof fresh === 'object' ? fresh : { items: [], week: {} };
      if (tpl) templates = templatesFrom(tpl);
      if (models) catalog = models;
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

  /* ── мастер ─────────────────────────────────────────────────────────── */

  function openWizard(draft, heading, opts = {}) {
    mode = 'wizard';
    const step = Number(opts.step);
    wizard = createWizard({
      draft,
      title: heading || WIZARD_TITLES[draft && draft.jobId ? 'edit' : 'create'],
      catalog,
      onPickFolder: (what, current) => pickFolder(what, current),
      onCancel: () => closeWizard(),
      onPreview: (current) => previewDraft(current),
      onSubmit: (current) => submitDraft(current),
      toast,
    });
    if (Number.isFinite(step) && step > 0) wizard.goto(step);
    wizardEl.replaceChildren(wizard.el);
    renderAll();
    if (opts.autoPreview) wizard.preview();
    if (typeof wizardEl.scrollIntoView === 'function') {
      wizardEl.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function closeWizard() {
    wizard = null;
    mode = 'list';
    wizardEl.replaceChildren();
    renderAll();
  }

  /** W29-P5: окно Windows «Выбрать…» для рабочей папки и файла слежения. */
  async function pickFolder(what, draft) {
    if (!api) return '';
    const answer = await api.post('/api/folders/pick', folderPickBody(what, draft));
    if (!answer || answer.cancelled) return '';
    const first = Array.isArray(answer.paths) ? answer.paths[0] : '';
    return String(answer.path || first || '');
  }

  /** Предпросмотр: сервер присылает точную команду; локальная копия — запасной путь. */
  async function previewDraft(draft) {
    const normalized = normalizeDraft(draft);
    if (normalized.mode === 'edit') {
      const model = previewModel({}, normalized);
      return { ok: true, command: model.command, changes: model.changes, note: model.note };
    }
    if (!roomId) {
      const model = previewModel({}, normalized);
      return { ...model, ok: false, error: 'Комната не выбрана — создавать дежурство некуда.' };
    }
    try {
      const answer = await api.post('/api/rooms/' + encodeURIComponent(roomId) + '/duties',
        createBody(normalized, { preview: true }));
      const preview = (answer && answer.preview) || answer || {};
      return { ...preview, ok: preview.ok !== false };
    } catch (err) {
      const model = previewModel({}, normalized);
      return {
        ok: true,
        command: model.command,
        changes: model.changes,
        note: 'Сервер предпросмотр не прислал (' + ((err && err.message) || 'ошибка') + '). '
          + 'Команда собрана в интерфейсе — при создании сервер выполнит свою.',
      };
    }
  }

  async function submitDraft(draft) {
    const normalized = normalizeDraft(draft);
    busy = normalized.mode === 'edit' ? 'edit' : 'create';
    try {
      if (normalized.mode === 'edit') {
        const res = await api.patch('/api/duties/' + encodeURIComponent(normalized.jobId), patchBody(normalized));
        if (stopped) return;
        const changed = Array.isArray(res && res.changed) ? res.changed : [];
        toast(changed.length ? 'Сохранено: ' + changed.join(', ') : 'Изменения сохранены', 'ok');
      } else {
        const res = await api.post('/api/rooms/' + encodeURIComponent(roomId) + '/duties',
          createBody(normalized, { preview: false }));
        if (stopped) return;
        toast((res && res.text) || 'Дежурство создано', 'ok');
        if (normalized.from && normalized.from.kind === 'proposal' && normalized.from.id) {
          try {
            await api.post('/api/duties/proposals/' + encodeURIComponent(normalized.from.id) + '/actions',
              { action: 'accept', job_id: (res && res.item && res.item.job_id) || '' });
          } catch { /* предложение могло быть уже закрыто — это не мешает созданию */ }
        }
      }
      closeWizard();
      await load();
    } finally {
      busy = '';
    }
  }

  /* ── действия над дежурством ─────────────────────────────────────────── */

  async function doAction(item, actionId) {
    if (!api || busy) return;
    const jobId = String((item && item.job_id) || '');
    if (actionId === 'edit') {
      openWizard(draftFromDuty(item, roomId), WIZARD_TITLES.edit);
      return;
    }
    if (actionId === 'duplicate') {
      const source = draftFromDuty(item, roomId);
      const copy = normalizeDraft({
        ...source, mode: 'create', jobId: '', started_paused: true,
        title: (source.title || 'дежурство') + ' (копия)',
      });
      openWizard(copy, WIZARD_TITLES.create);
      toast('Копия откроется на паузе — проверите и включите', 'info');
      return;
    }
    if (actionId === 'history') {
      await toggleHistory(jobId);
      return;
    }
    if (actionId === 'dryrun') {
      await dryRun(item);
      return;
    }
    if (actionId === 'delete') {
      const ok = await confirmDialog({
        title: ACTIONS.delete.label,
        text: confirmText('delete', item),
        confirmLabel: 'Убрать в корзину',
        danger: true,
      });
      if (!ok) return;
      busy = 'delete';
      try {
        await api.del('/api/duties/' + encodeURIComponent(jobId));
        if (stopped) return;
        toast('Дежурство в корзине — вернуть можно 30 дней', 'ok');
        openJob = '';
        await load();
      } catch (err) {
        if (!stopped) toast((err && err.message) || 'Не удалось убрать дежурство', 'danger');
      } finally {
        busy = '';
      }
      return;
    }

    let schedule = '';
    if (actionId === 'schedule') {
      const answer = await promptDialog({
        title: 'Изменить расписание — предложение',
        label: 'Новое расписание для «' + ((item && item.name) || 'дежурство') + '»',
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
      const res = await api.post('/api/duties/' + encodeURIComponent(jobId) + '/actions', body);
      if (stopped) return;
      toast((res && res.text) || 'Готово', res && res.applied ? 'ok' : 'info');
      delete dryCache[jobId];
      await load();
    } catch (err) {
      if (stopped) return;
      toast((err && err.message) || 'Действие не выполнено', 'danger');
    } finally {
      busy = '';
      root.removeAttribute('data-busy');
    }
  }

  async function toggleHistory(jobId) {
    if (!jobId) return;
    if (openJob === jobId) {
      openJob = '';
      renderList();
      return;
    }
    openJob = jobId;
    const cached = historyCache[jobId];
    if (!cached || !cached.items) {
      historyCache[jobId] = { items: [], loading: true, error: '' };
      renderList();
      try {
        const res = await api.get('/api/duties/' + encodeURIComponent(jobId) + '/history');
        if (stopped) return;
        const items = Array.isArray(res && res.items) ? res.items : [];
        historyCache[jobId] = { items, loading: false, error: '' };
      } catch (err) {
        historyCache[jobId] = {
          items: [], loading: false,
          error: (err && err.message) || 'сервер не ответил',
        };
      }
      renderList();
      return;
    }
    renderList();
  }

  async function dryRun(item) {
    const jobId = String((item && item.job_id) || '');
    busy = 'dryrun';
    try {
      const res = await api.post('/api/duties/' + encodeURIComponent(jobId) + '/dry_run', {});
      if (stopped) return;
      dryCache[jobId] = res || { ok: true };
      toast('Проверка выполнена — ничего не записано', 'info');
      renderList();
    } catch (err) {
      if (!stopped) toast((err && err.message) || 'Проверка не удалась', 'danger');
    } finally {
      busy = '';
    }
  }

  async function restoreDuty(row) {
    if (!api || busy) return;
    const ok = await confirmDialog({
      title: ACTIONS.restore.label,
      text: confirmText('restore', { name: row.title }),
      confirmLabel: 'Вернуть',
    });
    if (!ok) return;
    busy = 'restore';
    try {
      await api.post('/api/duties/' + encodeURIComponent(row.jobId) + '/restore', {});
      if (stopped) return;
      toast('Дежурство возвращено', 'ok');
      await load();
    } catch (err) {
      if (!stopped) toast((err && err.message) || 'Не удалось вернуть дежурство', 'danger');
    } finally {
      busy = '';
    }
  }

  /* ── предложения руководителя ────────────────────────────────────────── */

  async function actOnProposal(proposal, actionId) {
    if (!api || proposalBusy) return;
    const id = String(proposal.id || '');
    if (actionId === 'edit') {
      openWizard(draftFromProposal(proposal, roomId), WIZARD_TITLES.proposal);
      return;
    }
    if (actionId === 'accept') {
      // Предпросмотр обязателен: открываем мастер сразу на последнем шаге.
      openWizard(draftFromProposal(proposal, roomId), WIZARD_TITLES.proposal,
        { step: 3, autoPreview: true });
      return;
    }
    proposalBusy = id;
    renderProposals();
    try {
      const res = await api.post('/api/duties/proposals/' + encodeURIComponent(id) + '/actions',
        { action: actionId === 'never' ? 'never' : 'dismiss' });
      if (stopped) return;
      toast(actionId === 'never'
        ? 'Больше не будем предлагать такие дежурства'
        : ((res && res.text) || 'Предложение отклонено'), 'info');
      await load();
    } catch (err) {
      if (!stopped) toast((err && err.message) || 'Не удалось ответить на предложение', 'danger');
    } finally {
      proposalBusy = '';
      renderProposals();
    }
  }

  /* ── «Сделать регулярным» ────────────────────────────────────────────── */

  async function openPicker() {
    mode = 'picker';
    pickerDelos = null;
    pickerRequests = [];
    renderAll();
    if (!roomId) return;
    try {
      const res = await api.get('/api/rooms/' + encodeURIComponent(roomId) + '/delos');
      if (stopped) return;
      pickerDelos = Array.isArray(res && res.items) ? res.items : [];
    } catch {
      pickerDelos = [];
    }
    if (!pickerDelos.length) {
      // W29-P5: дел нет — не пустой список, а выбор «запрос комнаты» или своё дежурство.
      try {
        const reqs = await api.get('/api/rooms/' + encodeURIComponent(roomId) + '/requests?limit=10');
        if (stopped) return;
        pickerRequests = Array.isArray(reqs && reqs.items) ? reqs.items : [];
      } catch {
        pickerRequests = [];
      }
    }
    renderPicker();
  }

  /** Переход из Дела/запроса: `#/room/<id>/duties?delo=…` или `?request=…`. */
  async function openFromLink() {
    const deloId = String(query.delo || '').trim();
    const requestId = String(query.request || '').trim();
    if (!deloId && !requestId) return;
    try {
      if (deloId) {
        const delo = await api.get('/api/delos/' + encodeURIComponent(deloId));
        if (stopped) return;
        openWizard(draftFromDelo(delo, roomId), WIZARD_TITLES.delo);
      } else {
        const req = await api.get('/api/requests/' + encodeURIComponent(requestId));
        if (stopped) return;
        openWizard(draftFromRequest(req, roomId), WIZARD_TITLES.request);
      }
    } catch (err) {
      if (!stopped) toast((err && err.message) || 'Не удалось открыть черновик', 'warn');
    }
  }

  renderFilter();
  await load();
  await openFromLink();

  return {
    unmount() {
      stopped = true;
      root.remove();
    },
  };
}

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
