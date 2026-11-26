/**
 * roadmap-next.js — чистая логика блока «Что дальше» на «Карте проекта»
 * (W20-U2, пункт 16 / P12).
 *
 * Аудит 06.11: «Что дальше» повторялось дважды (факт в «Где мы сейчас» и
 * отдельная карточка), у пустой карты было три заглушки, а «дальше» стояло
 * статусом у каждого этапа. Здесь одно место, которое решает:
 *   • какой этап считать следующим (текущий → в работе → ждёт решения → не начат);
 *   • какие шаги из запросов показать (по сроку, не больше четырёх);
 *   • что написать, когда карта пустая (объяснение + два действия).
 *
 * Рендер — views/roadmap.js; модуль без DOM проверяется node --test
 * (tests/roadmap-next.test.mjs).
 */

import { stageMeta } from './team-roadmap-model.js';

/** Порядок обозначений в подписи под полосой этапов. */
export const LEGEND_STATUSES = ['done', 'active', 'blocked', 'next'];

/**
 * Подпись статусов этапов: значок + цвет + слово. Одна и та же для полосы,
 * панели деталей и легенды — чтобы цвет ничего не значил «на глаз».
 * @returns {Array<{status:string,label:string,tone:string,icon:string}>}
 */
export function legendItems() {
  return LEGEND_STATUSES.map((status) => {
    const meta = stageMeta(status);
    return { status: meta.status, label: meta.label, tone: meta.tone, icon: meta.icon };
  });
}

const PHASES = (roadmap) => (Array.isArray(roadmap && roadmap.phases) ? roadmap.phases : [])
  .filter((phase) => phase && typeof phase === 'object' && phase.id !== undefined && phase.id !== null);

/** Текущий этап карты: `roadmap.current` может быть объектом или id. */
function currentOf(roadmap, phases) {
  const current = roadmap && roadmap.current;
  if (!current) return null;
  if (typeof current === 'object' && current.id !== undefined) {
    return phases.find((phase) => String(phase.id) === String(current.id)) || current;
  }
  return phases.find((phase) => String(phase.id) === String(current)) || null;
}

/**
 * Следующий этап: что человеку делать дальше.
 *
 * Текущий (если не готов) → первый «в работе» → первый «ждёт решения» →
 * первый «не начат». Все готовы — null.
 * @param {object} roadmap ответ GET /api/rooms/{id}/roadmap
 * @returns {object|null}
 */
export function nextPhase(roadmap) {
  const phases = PHASES(roadmap);
  if (!phases.length) return null;
  const statusOf = (phase) => String((phase && phase.status) || 'next');
  const current = currentOf(roadmap, phases);
  if (current && statusOf(current) !== 'done') return current;
  return phases.find((phase) => statusOf(phase) === 'active')
    || phases.find((phase) => statusOf(phase) === 'blocked')
    || phases.find((phase) => statusOf(phase) === 'next')
    || null;
}

/**
 * Открытые шаги комнаты для «Что дальше»: только то, что человек ещё не сделал,
 * по сроку, не больше `limit`.
 * @param {Array<object>} backlog элементы GET /api/rooms/{id}/backlog
 * @param {number} [limit]
 * @returns {Array<object>}
 */
export function nextSteps(backlog, limit = 4) {
  const max = Math.max(0, Math.trunc(Number(limit) || 0)) || 4;
  return (Array.isArray(backlog) ? backlog : [])
    .filter((item) => item && typeof item === 'object'
      && item.kind === 'next_step'
      && (item.status === 'open' || item.status === 'asked')
      && String(item.text || '').trim())
    .sort((a, b) => String(a.due || '9999-12-31').localeCompare(String(b.due || '9999-12-31'))
      || String(a.id || '').localeCompare(String(b.id || '')))
    .slice(0, max);
}

/**
 * Всё, что нужно блоку «Что дальше» — одним вызовом.
 * @param {object} roadmap
 * @param {Array<object>} backlog
 * @param {{limit?:number}} [opts]
 * @returns {{phase:object|null, steps:object[], allDone:boolean, hasPhases:boolean,
 *            empty:boolean, line:string, note:string, detail:string}}
 */
export function nextBlock(roadmap, backlog, opts = {}) {
  const limit = opts && Number.isFinite(Number(opts.limit)) ? Number(opts.limit) : 4;
  const phases = PHASES(roadmap);
  const hasPhases = phases.length > 0;
  const phase = nextPhase(roadmap);
  const allDone = hasPhases && !phase && phases.every((item) => String(item.status || 'next') === 'done');
  const steps = nextSteps(backlog, limit);
  const meta = phase ? stageMeta(phase.status) : null;

  let line = '';
  if (phase) line = 'Этап «' + String(phase.title || '') + '» — ' + meta.label;
  else if (allDone) line = 'Все этапы пройдены';
  else if (!hasPhases) line = 'Этапов пока нет';

  return {
    phase: phase || null,
    steps,
    allDone,
    hasPhases,
    empty: !phase && !steps.length,
    line,
    note: phase && phase.note ? String(phase.note) : '',
    detail: phase ? String(phase.title || '') : '',
  };
}

/**
 * Пустая карта: одна карточка вместо трёх заглушек.
 *
 * Режим включён — просим добавить этапы (или предложить их руководителю).
 * Режим выключен — объясняем, что он даёт, и даём одну кнопку.
 * @param {{modeOn?:boolean, proposalPending?:boolean}} [state]
 * @returns {{title:string, text:string, primary:{key:string,label:string},
 *            secondary:{key:string,label:string}|null, note:string}}
 */
export function emptyPlan(state = {}) {
  const modeOn = Boolean(state && state.modeOn);
  const proposalPending = Boolean(state && state.proposalPending);
  if (modeOn) {
    return {
      title: 'Карта ещё не собрана',
      text: 'Этапы появятся после первой завершённой задачи: тогда их можно собрать одной '
        + 'кнопкой по итогам того, что команда уже сделала. Или добавьте этапы сами '
        + '(«Идея», «Согласование», «Запуск») — карта сразу станет рабочей.',
      primary: { key: 'build', label: 'Собрать этапы по выполненным задачам' },
      secondary: proposalPending ? null : { key: 'edit', label: 'Добавить этапы вручную' },
      note: 'Редактор этапов открывается здесь же — настройки комнаты менять не нужно.',
    };
  }
  return {
    title: 'Проектный режим выключен',
    text: 'Сейчас руководитель отвечает одним общим ответом — без частей работы и без этапов. '
      + 'Включите проектный режим, если одна задача — это целый проект: он разделит её на части, '
      + 'у каждой будет свой исполнитель и свой ход работы.',
    primary: { key: 'mode', label: 'Включить проектный режим' },
    secondary: null,
    note: 'Откроются настройки этой комнаты. Больше ничего не меняется.',
  };
}

/** Короткая подпись счётчика шагов: «4 шага», «1 шаг», «шагов нет». */
export function stepsCountLabel(count) {
  const n = Math.abs(Math.trunc(Number(count) || 0));
  const tail100 = n % 100;
  const tail10 = n % 10;
  if (n === 0) return 'шагов нет';
  let word = 'шагов';
  if (tail100 < 11 || tail100 > 14) {
    if (tail10 === 1) word = 'шаг';
    else if (tail10 >= 2 && tail10 <= 4) word = 'шага';
  }
  return n + ' ' + word;
}
