/**
 * plan-req.js — «Проектный режим» на страницах брифа и направления (волна 9).
 *
 * Что здесь (общее для composer.js, composer-feed.js, request.js, journal.js):
 *  • флажок «Это бриф проекта» и его подсказка — тексты и правило «по умолчанию»;
 *  • бейдж «бриф» и мини-счётчик «направлений: N, идёт: M» из `streams_summary`;
 *  • плашка «Предложенная карта» (список направлений + «Запустить всё»);
 *  • записи журнала плана: «добавил направление «…», потому что …».
 *
 * Данные — только из ответов сервера (`GET /api/rooms/{r}/plan`,
 * `POST /api/rooms/{r}/plan/approve`), схемы — `docs/WAVE9-RULES.md` §1, §4.
 * Ничего не пишет на сервер само: действия передаются колбэком.
 */

import { h, plainTerms } from '../util.js';
import { icon } from '../icons.js';
import { countNoun, pluralVerb } from '../text-ru.js';

/** Бейдж в ленте композера: запрос-бриф проекта. */
export const BRIEF_BADGE = 'бриф';

/** Заголовок плашки на странице брифа. */
export const PLAN_TITLE = 'Предложенная карта';

/** Подпись флажка в композере. */
export const BRIEF_LABEL = 'Это бриф проекта';

/** Подсказка под флажком (дословно из ТЗ волны 9). */
export const BRIEF_HINT = 'Руководитель комнаты не будет глубоко исследовать: '
  + 'он предложит карту направлений, а запуск — по вашему «Запустить»';

/** Пустая карта: что сказать, пока направлений нет. */
export const PLAN_EMPTY_TEXT = 'Руководитель комнаты ещё не предложил направления — карта появится после его разбора.';

/** Нет связи: человеческий текст вместо кода ошибки. */
export const PLAN_ERROR_TEXT = 'Не удалось показать карту: нет связи с приложением.';

/** Максимум направлений в карте (та же цифра, что в §1 правил). */
export const STREAMS_MAX = 8;

/** Статусы направления по-русски: подпись и тон чипа. */
const STREAM_STATUS = {
  proposed: { label: 'предложено', tone: 'outline' },
  queued: { label: 'в очереди', tone: 'muted' },
  running: { label: 'идёт', tone: 'info' },
  waiting_quota: { label: 'ждёт лимита', tone: 'warn' },
  review: { label: 'проверка', tone: 'info' },
  done: { label: 'готово', tone: 'ok' },
  dropped: { label: 'убрано', tone: 'muted' },
};

/* ────────────────────────────────────────────────────────────── данные */

/**
 * Ответ `GET /api/rooms/{r}/plan` в виде плана: `{plan}` или сам план.
 * @param {*} res
 * @returns {object|null}
 */
export function planOf(res) {
  if (!res || typeof res !== 'object') return null;
  const inner = res.plan;
  if (inner && typeof inner === 'object') return inner;
  return Array.isArray(res.streams) ? res : null;
}

/** Это запрос-бриф проекта? Поле необязательное — читаем терпимо. */
export function isBrief(detail) {
  if (!detail || typeof detail !== 'object') return false;
  return detail.brief === true || detail.is_brief === true;
}

/** Направления плана (только объекты). */
export function streamsOf(plan) {
  const list = plan && Array.isArray(plan.streams) ? plan.streams : [];
  return list.filter((s) => s && typeof s === 'object');
}

/**
 * Сводка брифа: `{total, running}` или `null`, если сервер её не прислал.
 * `streams_summary` — необязательное поле RequestDetail (см. §4).
 * @param {*} source RequestDetail или элемент ленты
 * @returns {{total:number, running:number|null}|null}
 */
export function summaryOf(source) {
  if (!source || typeof source !== 'object') return null;
  const src = source.streams_summary && typeof source.streams_summary === 'object'
    ? source.streams_summary
    : null;
  if (!src) return null;
  const totalRaw = src.total !== undefined ? src.total : src.count;
  const total = Number(totalRaw);
  if (!Number.isFinite(total) || total < 0) return null;
  const runningRaw = src.running !== undefined ? src.running : src.active;
  const running = Number(runningRaw);
  return { total, running: Number.isFinite(running) && running >= 0 ? running : null };
}

/**
 * Мини-счётчик: «направлений: 4, идёт: 2». Пустая строка — сводки нет.
 * @param {{total:number, running:number|null}|null} summary
 * @returns {string}
 */
export function summaryText(summary) {
  if (!summary || !Number.isFinite(Number(summary.total))) return '';
  const parts = ['направлений: ' + Number(summary.total)];
  if (summary.running !== null && summary.running !== undefined) parts.push('идёт: ' + Number(summary.running));
  return parts.join(', ');
}

/** Статус направления: подпись и тон (неизвестный — «предложено»). */
export function streamStatusInfo(status) {
  const key = String(status === null || status === undefined ? '' : status).trim() || 'proposed';
  return STREAM_STATUS[key] || STREAM_STATUS.proposed;
}

/** Сколько направлений ждёт запуска (status=proposed). */
export function proposedCount(plan) {
  return streamsOf(plan).filter((s) => {
    const st = String(s.status === null || s.status === undefined ? 'proposed' : s.status);
    return st === 'proposed';
  }).length;
}

/** «biz-ops» → «ops», «manager» → «руководитель комнаты»: без системных id. */
export function whoName(id) {
  const raw = String(id === null || id === undefined ? '' : id).trim();
  if (!raw) return '';
  if (raw === 'mikhail') return 'Михаил';
  if (raw === 'manager' || raw === 'director') return 'руководитель комнаты';
  return raw.replace(/^(?:biz|room)-/, '');
}

/** Одна строка из многострочного текста, не длиннее `max` знаков. */
export function oneLine(text, max = 160) {
  const flat = String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim();
  if (!flat) return '';
  return flat.length > max ? flat.slice(0, max - 1).trimEnd() + '…' : flat;
}

/* ─────────────────────────────────────────────────────── флажок брифа */

/**
 * Включать ли флажок «Это бриф проекта» по умолчанию:
 * проектный режим комнаты + пока в комнате нет ни одного запроса.
 * @param {{projectMode?:boolean, emptyRoom?:boolean, touched?:boolean}} opts
 * @returns {boolean}
 */
export function briefDefaultOn(opts = {}) {
  const o = opts || {};
  if (o.touched) return Boolean(o.brief);
  return Boolean(o.projectMode) && Boolean(o.emptyRoom);
}

/**
 * Тело запроса: флажок уходит только в проектной комнате (иначе поле не нужно).
 * @param {{projectMode?:boolean, brief?:boolean}} opts
 * @returns {{brief?:boolean}}
 */
export function briefPayload(opts = {}) {
  const o = opts || {};
  if (!o.projectMode) return {};
  return { brief: Boolean(o.brief) };
}

/* ───────────────────────────────────────────────── бейджи в ленте */

/** Бейдж «бриф» у карточки запроса-брифа. */
export function briefBadge() {
  return h('span', {
    class: 'chip chip--info plan-badge',
    title: 'Это бриф проекта: руководитель комнаты предложит карту направлений',
  }, icon('map', { size: 11 }), h('span', { class: 'plan-badge__text' }, BRIEF_BADGE));
}

/**
 * Мини-счётчик «направлений: N, идёт: M». `null` — сводки нет, не показываем.
 * @param {{total:number, running:number|null}|null} summary
 * @returns {HTMLElement|null}
 */
export function briefSummaryChip(summary) {
  const text = summaryText(summary);
  if (!text) return null;
  return h('span', {
    class: 'plan-sum small muted tnum',
    title: 'Карта проекта: ' + text,
  }, icon('map', { size: 11 }), text);
}

/* ─────────────────────────────────────────────── плашка «Предложенная карта» */

/** Адрес вкладки «Карта проекта». */
export function roadmapHash(roomId) {
  const id = String(roomId || '').trim();
  return id ? '#/room/' + encodeURIComponent(id) + '/roadmap' : '#/';
}

function streamRow(stream, index, streams) {
  const st = streamStatusInfo(stream.status);
  const owner = whoName(stream.owner);
  const members = Array.isArray(stream.members) ? stream.members.filter(Boolean) : [];
  const deps = Array.isArray(stream.depends_on) ? stream.depends_on.filter(Boolean) : [];
  const depTitles = deps
    .map((id) => {
      const found = streams.find((s) => String(s.id) === String(id));
      return found && found.title ? String(found.title) : '';
    })
    .filter(Boolean);

  const meta = h('div', { class: 'plan-stream__meta small muted' });
  const push = (value) => {
    if (meta.childNodes.length) meta.appendChild(h('span', { class: 'plan-stream__sep', 'aria-hidden': 'true' }, '·'));
    meta.appendChild(h('span', { class: 'plan-stream__meta-item' }, value));
  };
  if (owner) push('ответственный: ' + owner);
  if (members.length) push('участников: ' + members.length);
  if (stream.due) push('срок: ' + String(stream.due));
  if (depTitles.length) push('после «' + depTitles.join('», «') + '»');

  return h('li', { class: 'plan-stream', 'data-stream': String(stream.id || '') },
    h('span', { class: 'plan-stream__num tnum', 'aria-hidden': 'true' }, String(index + 1)),
    h('div', { class: 'plan-stream__main' },
      h('div', { class: 'plan-stream__row' },
        h('span', { class: 'plan-stream__title' }, String(stream.title || 'Направление')),
        h('span', { class: ['chip', 'chip--' + st.tone, 'plan-stream__chip'] }, st.label)),
      stream.goal ? h('div', { class: 'plan-stream__goal' }, oneLine(stream.goal, 160)) : null,
      meta.childNodes.length ? meta : null));
}

/**
 * Плашка «Предложенная карта»: направления компактно, «Запустить всё»,
 * ссылка «Открыть карту проекта».
 *
 * Состояния: `error` (нет связи, кнопка «Повторить»), пустая карта, карта
 * с направлениями. Кнопки — не громче главной кнопки страницы.
 *
 * @param {{plan?:object|null, roomId?:string, error?:Error|string|null,
 *          launching?:boolean, onLaunchAll?:Function, onRetry?:Function}} opts
 * @returns {HTMLElement}
 */
export function buildPlanCard(opts = {}) {
  const o = opts || {};
  const plan = o.plan && typeof o.plan === 'object' ? o.plan : null;
  const streams = streamsOf(plan);
  const launching = Boolean(o.launching);
  const roomId = String(o.roomId || '');

  const card = h('section', { class: 'card plan-card', 'data-plan': 'card' });
  const count = streams.length
    ? h('span', { class: 'chip chip--muted plan-card__count tnum' },
      countNoun(streams.length, ['направление', 'направления', 'направлений']))
    : null;

  card.appendChild(h('div', { class: 'plan-card__head' },
    h('h2', { class: 'plan-card__title' }, PLAN_TITLE),
    count,
    h('div', { class: 'plan-card__grow' }),
    h('a', {
      class: 'btn btn--ghost btn--sm plan-card__link',
      href: roadmapHash(roomId),
      title: 'Открыть вкладку «Карта проекта»',
    }, icon('map', { size: 13 }), h('span', null, 'Открыть карту проекта'))));

  if (o.error) {
    const retry = typeof o.onRetry === 'function'
      ? h('button', {
        class: 'btn btn--secondary btn--sm', type: 'button',
        onclick: () => o.onRetry(),
      }, icon('refresh', { size: 12 }), h('span', null, 'Повторить'))
      : null;
    const msg = typeof o.error === 'string' && o.error.trim() ? plainTerms(o.error) : PLAN_ERROR_TEXT;
    card.appendChild(h('div', { class: 'banner banner--warn plan-card__error', role: 'status' },
      icon('warning', { size: 14 }),
      h('div', { class: 'banner__text' }, msg),
      retry));
    return card;
  }

  if (!streams.length) {
    card.appendChild(h('p', { class: 'plan-card__empty' }, PLAN_EMPTY_TEXT));
    return card;
  }

  const list = h('ol', { class: 'plan-card__list' });
  streams.forEach((stream, i) => list.appendChild(streamRow(stream, i, streams)));
  card.appendChild(list);

  const ready = proposedCount(plan);
  const launchBtn = h('button', {
    class: 'btn btn--primary btn--sm plan-card__launch',
    type: 'button',
    disabled: launching || !ready,
    title: ready ? 'Запустить все предложенные направления' : 'Все направления уже запущены или убраны',
  },
  icon(launching ? 'loading' : 'play', { size: 13, className: launching ? 'codicon-modifier-spin' : null }),
  h('span', null, launching ? 'Запускаю…' : 'Запустить всё'));

  if (!launching && ready && typeof o.onLaunchAll === 'function') {
    launchBtn.addEventListener('click', () => o.onLaunchAll());
  }

  card.appendChild(h('div', { class: 'plan-card__actions row row--wrap' },
    launchBtn,
    ready ? h('span', { class: 'small muted plan-card__note' },
      countNoun(ready, ['направление', 'направления', 'направлений'])
      + ' ' + pluralVerb(ready, ['ждёт', 'ждут']) + ' запуска') : null));

  return card;
}

/* ─────────────────────────────────────────────────── журнал: записи плана */

const LOG_ACTOR = {
  manager: 'руководитель комнаты',
  mikhail: 'Михаил',
  system: 'система',
};

function streamTitleOf(plan, streamId) {
  const id = String(streamId === null || streamId === undefined ? '' : streamId);
  if (!id) return '';
  const found = streamsOf(plan).find((s) => String(s.id) === id);
  return found && found.title ? String(found.title) : '';
}

function logReason(item) {
  return oneLine(item && (item.reason || item.text), 200);
}

/**
 * Записи журнала из `plan.log`: что изменил руководитель комнаты и почему.
 * `at` — epoch секунды, если сервер прислал так; иначе строка даты как есть.
 * @param {object|null} plan
 * @returns {object[]} события в форме журнала комнаты
 */
export function planLogEvents(plan) {
  const p = plan && typeof plan === 'object' ? plan : null;
  const log = p && Array.isArray(p.log) ? p.log : [];
  const briefRoot = String((p && p.brief_root_id) || '');

  return log
    .filter((item) => item && typeof item === 'object')
    .map((item, i) => {
      const kind = String(item.kind || 'changed');
      const title = streamTitleOf(p, item.stream_id);
      const name = title ? '«' + title + '»' : '';
      const why = logReason(item);
      let text;
      switch (kind) {
        case 'added':
          text = 'добавил направление ' + (name || 'в карту') + (why ? ', потому что ' + why : '');
          break;
        case 'created':
          // Текст сервера уже может быть «предложил карту: N направлений» —
          // не удваиваем первую фразу.
          text = !why ? 'предложил карту направлений'
            : (/^предложил/i.test(why) ? why : 'предложил карту: ' + why);
          break;
        case 'launched':
          text = 'запустил направление ' + (name || '');
          break;
        case 'dropped':
          text = 'убрал направление ' + (name || '') + (why ? ', потому что ' + why : '');
          break;
        case 'quota':
          text = 'направление ' + (name || '') + ' ждёт лимита — очередь подождёт';
          break;
        case 'changed':
        default:
          text = 'изменил направление ' + (name || 'в карте') + (why ? ': ' + why : '');
          break;
      }
      return {
        id: 'plan-' + i,
        at: item.at === undefined || item.at === null ? '' : item.at,
        kind: 'plan',
        actor: LOG_ACTOR[String(item.who || 'manager')] || 'руководитель комнаты',
        text: String(text).replace(/\s+/g, ' ').trim(),
        stream_id: item.stream_id || null,
        request_id: briefRoot,
      };
    });
}

export default {
  BRIEF_BADGE, BRIEF_LABEL, BRIEF_HINT, PLAN_TITLE, PLAN_EMPTY_TEXT, PLAN_ERROR_TEXT, STREAMS_MAX,
  planOf, isBrief, streamsOf, summaryOf, summaryText, streamStatusInfo, proposedCount,
  whoName, oneLine, briefDefaultOn, briefPayload, briefBadge, briefSummaryChip,
  roadmapHash, buildPlanCard, planLogEvents,
};
