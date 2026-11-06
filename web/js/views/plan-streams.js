/**
 * plan-streams.js — блок «Направления» на вкладке «Карта проекта» (волна 9).
 *
 * Карта проекта — это список направлений, которые руководитель комнаты
 * предложил по главному запросу (брифу). Здесь только показ и действия:
 * данные приносит roadmap.js (GET /api/rooms/{r}/plan), запись — через него же.
 *
 * Наружу:
 *   streamStatusMeta / streamStatusChip   — статус направления по-русски;
 *   streamOrigin                          — «предложил руководитель» / «добавил вы»;
 *   streamsSummary / launchableIds        — счётчики и что можно запустить;
 *   peopleIndex / ownerLabelOf            — люди комнаты и ответственный по-русски;
 *   dependencyNote                        — «после «X»»;
 *   launchConfirm                         — текст подтверждения «Запустить всё»;
 *   renderStreams(opts)                   — готовый <section> с карточками;
 *   openStreamEditor(opts)                — диалог «Изменить» / «Добавить»;
 *   planBlockVisible(room)                — показывать ли блок вообще.
 *
 * Правила: только h() (без innerHTML), только токены tokens.css, микрофон в
 * полях ставится сам (dictation-auto.js) — атрибуты data-mic не выставляем.
 */

import { h, uid } from '../util.js';
import { icon } from '../icons.js';
import { profileLabel, dueBadge } from '../ui.js';
import { roleTitle, isMachineName } from '../shell-labels.js';
import { countNoun, fromForm, errorText } from '../text-ru.js';

/* --------------------------------------------------------------- кто есть кто */

/**
 * Индекс людей комнаты: профиль (`biz-ops`) → человеческое имя и роль.
 * Принимает Map, объект `{profile: {title, role}}` или список участников
 * (`{profile, role, title}` из /team или состава комнаты). Технические id
 * наружу не показываем — они остаются только в подсказке (title).
 * @param {Map|object|Array|null} people
 * @returns {Map<string, {title: string, role: string}>}
 */
export function peopleIndex(people) {
  const out = new Map();
  const add = (item) => {
    if (!item) return;
    const profile = typeof item === 'string' ? item : String(item.profile || item.id || '').trim();
    if (!profile) return;
    const prev = out.get(profile) || { title: '', role: '' };
    // WAVE13 §11: у участника из /team уже есть готовая подпись «Имя — должность».
    const title = typeof item === 'string' ? ''
      : String(item.label || item.title || item.name || item.display_name || '').trim();
    const role = typeof item === 'string' ? '' : String(item.role || '').trim();
    out.set(profile, { title: title || prev.title, role: role || prev.role });
  };
  if (people instanceof Map) people.forEach((value, key) => add(Object.assign({ profile: key }, value || {})));
  else if (Array.isArray(people)) people.forEach(add);
  else if (people && typeof people === 'object') Object.keys(people).forEach((k) => add(Object.assign({ profile: k }, people[k] || {})));
  return out;
}

function upperFirst(text) {
  const s = String(text || '');
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/**
 * Подпись ответственного по-русски: имя профиля из /team, иначе роль
 * («Руководитель комнаты», «Исполнитель»). Системный id — в `hint`.
 * @param {{owner?: string, owner_label?: string}|null} stream
 * @param {Map|object|Array|null} people
 * @returns {{name: string, hint: string}|null}
 */
export function ownerLabelOf(stream, people) {
  const raw = String((stream && stream.owner) || '').trim();
  if (!raw) return null;
  const index = people instanceof Map ? people : peopleIndex(people);
  const info = index.get(raw) || null;
  const explicit = String((stream && (stream.owner_label || stream.owner_name)) || '').trim();
  const name = explicit && !isMachineName(explicit, raw)
    ? explicit
    : (info && info.title && !isMachineName(info.title, raw) ? info.title : '');
  if (name) return { name, hint: raw };
  const role = (info && info.role) || (raw === 'manager' || raw === 'director' ? 'orchestrator' : '');
  return { name: role ? upperFirst(roleTitle(role)) : 'Исполнитель', hint: raw };
}

/** Человеческая подпись любого профиля (участники направления). */
function personName(raw, index) {
  const value = String(raw || '').trim();
  if (!value) return '';
  const info = index.get(value) || null;
  if (info && info.title && !isMachineName(info.title, value)) return info.title;
  if (value === 'manager' || value === 'director') return upperFirst(roleTitle('orchestrator'));
  return info && info.role ? upperFirst(roleTitle(info.role)) : 'Исполнитель';
}

/* ------------------------------------------------------------------ статусы */

/**
 * @typedef {{label: string, tone: string, hint: string, pulse?: boolean}} StreamStatus
 */

/** @type {Record<string, StreamStatus>} */
const STATUS = {
  proposed: {
    label: 'Предложено', tone: 'muted', hint:
    'Руководитель комнаты предложил направление — работа начнётся после «Запустить»',
  },
  queued: {
    label: 'В очереди', tone: 'outline', hint:
    'Ждёт своей очереди: впереди другие направления или места в работе нет',
  },
  running: {
    label: 'Идёт', tone: 'info', pulse: true, hint: 'Команда работает над направлением',
  },
  waiting_quota: {
    label: 'Ждёт лимит', tone: 'warn', hint:
    'Упёрлось в лимит модели — продолжится само, когда лимит снимется',
  },
  review: {
    label: 'На проверке', tone: 'info', pulse: true, hint: 'Критик проверяет результат',
  },
  done: { label: 'Готово', tone: 'ok', hint: 'Направление завершено' },
  dropped: { label: 'Отложено', tone: 'muted', hint: 'Отложено: можно вернуть и запустить позже' },
};

const STATUS_ORDER = ['proposed', 'queued', 'running', 'waiting_quota', 'review', 'done', 'dropped'];

/** Пометка плана целиком: черновик / к запуску / в работе / готово. */
const PLAN_STATUS = {
  draft: 'черновик карты',
  approved: 'к запуску',
  running: 'идёт работа',
  done: 'всё готово',
};

function norm(v) {
  return String(v === null || v === undefined ? '' : v).trim().toLowerCase();
}

/**
 * Подпись и цвет статуса направления.
 * @param {string} status proposed|queued|running|waiting_quota|review|done|dropped
 * @returns {StreamStatus & {status: string}}
 */
export function streamStatusMeta(status) {
  const key = norm(status);
  const meta = STATUS[key];
  if (meta) return Object.assign({ status: key }, meta);
  return {
    status: key, label: String(status === null || status === undefined ? '' : status).trim() || '—',
    tone: 'outline', hint: '',
  };
}

/**
 * Чип статуса направления. Неизвестный статус — нейтральная рамка.
 * @param {string} status
 * @returns {HTMLElement}
 */
export function streamStatusChip(status) {
  const meta = streamStatusMeta(status);
  const el = h('span', {
    class: ['chip', 'ps-chip', 'chip--' + meta.tone, meta.pulse ? 'ps-chip--live' : ''],
    title: meta.hint || undefined,
    'data-stream-status': meta.status || undefined,
  });
  if (meta.pulse) el.appendChild(h('span', { class: 'chip__dot chip__dot--pulse' }));
  el.appendChild(h('span', { class: 'ps-chip__text' }, meta.label));
  return el;
}

/** Все статусы в порядке показа — для тестов и легенды. */
export const STREAM_STATUSES = STATUS_ORDER.slice();

/* ------------------------------------------------------------------ источники */

/**
 * Кто добавил направление. Причина (reason) — в title и в раскрывающемся блоке.
 * @param {'manager'|'mikhail'|string} createdBy
 * @returns {{label: string, hint: string}}
 */
export function streamOrigin(createdBy) {
  const who = norm(createdBy);
  if (who === 'mikhail') {
    return { label: 'добавил вы', hint: 'Вы добавили это направление вручную' };
  }
  return {
    label: 'предложил руководитель', hint: 'Направление предложил руководитель комнаты по главному запросу',
  };
}

/* ------------------------------------------------------------------- счётчики */

function listOf(plan) {
  const streams = plan && Array.isArray(plan.streams) ? plan.streams : [];
  return streams.filter((s) => s && typeof s === 'object');
}

/**
 * Счётчики по карте.
 * @param {{streams?: object[]}|null} plan
 * @returns {{total: number, running: number, waiting: number, proposed: number,
 *            queued: number, review: number, done: number, dropped: number, active: number}}
 */
export function streamsSummary(plan) {
  const out = {
    total: 0, running: 0, waiting: 0, proposed: 0, queued: 0,
    review: 0, done: 0, dropped: 0, active: 0,
  };
  for (const s of listOf(plan)) {
    const st = norm(s.status) || 'proposed';
    out.total += 1;
    if (st === 'running') out.running += 1;
    else if (st === 'waiting_quota') out.waiting += 1;
    else if (st === 'proposed') out.proposed += 1;
    else if (st === 'queued') out.queued += 1;
    else if (st === 'review') out.review += 1;
    else if (st === 'done') out.done += 1;
    else if (st === 'dropped') out.dropped += 1;
  }
  out.active = out.running + out.waiting + out.review;
  return out;
}

/**
 * Что имеет смысл запускать: предложенное и стоящее в очереди.
 * @param {{streams?: object[]}|null} plan
 * @returns {string[]} id направлений
 */
export function launchableIds(plan) {
  return listOf(plan)
    .filter((s) => ['proposed', 'queued'].includes(norm(s.status) || 'proposed'))
    .map((s) => String(s.id || ''))
    .filter(Boolean);
}

/** Строка «3 направления · идёт 1» для шапки блока. */
export function streamsCountLine(plan) {
  const c = streamsSummary(plan);
  if (!c.total) return 'пока пусто';
  const parts = [countNoun(c.total, ['направление', 'направления', 'направлений'])];
  if (c.running) parts.push('идёт ' + c.running);
  if (c.waiting) parts.push('ждёт лимит ' + c.waiting);
  if (c.proposed) parts.push('предложено ' + c.proposed);
  return parts.join(' · ');
}

/* ------------------------------------------------------------ зависимости */

/**
 * «после «Исследование рынка»» — по названиям, без id.
 * @param {{depends_on?: string[]}|null} stream
 * @param {Record<string, object>} byId
 * @returns {string} '' — зависимостей нет
 */
export function dependencyNote(stream, byId) {
  const deps = stream && Array.isArray(stream.depends_on) ? stream.depends_on : [];
  const names = [];
  for (const raw of deps) {
    const id = String(raw || '');
    if (!id) continue;
    const dep = byId && byId[id];
    const title = dep && dep.title ? String(dep.title).trim() : '';
    names.push(title || 'направления без названия');
  }
  if (!names.length) return '';
  const quoted = names.map((n) => '«' + n + '»').join(', ');
  return (names.length === 1 ? 'после ' : 'после ') + quoted;
}

/* ------------------------------------------------------------- подтверждение */

/**
 * Текст подтверждения «Запустить всё»: сколько направлений, сколько
 * одновременно, что начнут настоящие агенты. Без технических слов.
 * @param {{streams?: object[]}|null} plan
 * @param {number} maxParallel 1..4
 * @param {string[]} [ids] что именно запускаем (по умолчанию — всё запускаемое)
 * @returns {{title: string, text: string, count: number, maxParallel: number}}
 */
export function launchConfirm(plan, maxParallel, ids) {
  const list = Array.isArray(ids) && ids.length ? ids.slice() : launchableIds(plan);
  const n = list.length;
  const limit = Math.max(1, Math.min(4, Math.floor(Number(maxParallel)) || 2));
  const one = countNoun(n, ['направление', 'направления', 'направлений']);
  const title = n === 1 ? 'Запустить направление?' : 'Запустить все направления?';
  const parts = [];
  parts.push(n
    ? `Начнут работу ${one}.`
    : 'Сейчас запускать нечего: все направления уже идут или закрыты.');
  if (n) {
    // После «до» — родительный падеж: «до 1 направления», «до 2 направлений».
    parts.push(`Одновременно — до ${limit} ${fromForm(limit, ['направление', 'направления', 'направлений'])}:`
      + ' остальные подождут своей очереди и начнутся сами.');
    parts.push('Это настоящие агенты команды: они начнут выполнять задачи и тратить время модели.');
  }
  return { title, text: parts.join(' '), count: n, maxParallel: limit };
}

/* ------------------------------------------------------------------ карточка */

function participantsNode(members, index) {
  const list = (Array.isArray(members) ? members : []).map((m) => String(m || '')).filter(Boolean);
  if (!list.length) return null;
  const label = countNoun(list.length, ['участник', 'участника', 'участников']);
  const names = list.map((m) => personName(m, index)).join(', ');
  return h('span', {
    class: 'ps-card__members',
    title: label + ': ' + names,
  },
  icon('organization', { size: 11 }),
  h('span', { class: 'ps-card__members-text truncate' }, label));
}

function originNode(stream) {
  const origin = streamOrigin(stream.created_by);
  const reason = String(stream.reason || '').trim();
  if (!reason) {
    return h('span', { class: 'ps-card__origin small muted', title: origin.hint }, origin.label);
  }
  // Раскрывается по клику: причина правки видна, но не занимает карточку.
  return h('details', { class: 'ps-origin' },
    h('summary', {
      class: 'ps-origin__summary small muted',
      title: origin.hint + ' — нажмите, чтобы увидеть причину',
    }, origin.label),
    h('p', { class: 'ps-origin__why small muted' }, reason));
}

/** Ссылка на под-запрос: свой обработчик или адрес по умолчанию (roomId + id). */
function requestHrefOf(ctx, rid) {
  const id = String(rid || '');
  if (typeof ctx.requestHref === 'function') {
    const href = ctx.requestHref(id);
    if (href) return String(href);
  }
  const roomId = String(ctx.roomId || (ctx.room && ctx.room.id) || '');
  return `#/room/${encodeURIComponent(roomId)}/request/${encodeURIComponent(id)}`;
}

function actionButton(label, opts) {
  const o = opts || {};
  return h('button', {
    class: ['btn', 'btn--sm', o.variant || 'btn--secondary', 'ps-card__btn'],
    type: 'button',
    title: o.title || undefined,
    onclick: o.onclick,
  }, o.icon ? icon(o.icon, { size: 12 }) : null, label);
}

function streamCard(stream, ctx) {
  const id = String(stream.id || '');
  const st = norm(stream.status) || 'proposed';
  const byId = ctx.byId;
  const index = ctx.people instanceof Map ? ctx.people : peopleIndex(ctx.people);
  const dep = dependencyNote(stream, byId);
  const due = stream.due ? dueBadge(stream.due, ctx.now) : null;
  // Ответственный — по-русски: имя из /team («Операции»), иначе роль
  // («Руководитель комнаты»). Системный профиль biz-ops — только в title.
  const ownerInfo = ownerLabelOf(stream, index);
  const owner = ownerInfo
    ? profileLabel({ profile: String(stream.owner || ''), name: ownerInfo.name }, { size: 'sm' })
    : null;
  if (owner && ownerInfo) owner.setAttribute('title', 'Ответственный: ' + ownerInfo.hint);
  const members = participantsNode(stream.members, index);
  const rid = String(stream.request_id || '');
  const canLaunch = (st === 'proposed' || st === 'queued') && !ctx.busy;
  const canDrop = (st !== 'dropped' && st !== 'done') && !ctx.busy;

  return h('article', {
    class: ['ps-card', 'ps-card--' + st],
    dataset: { stream: id },
  },
  h('div', { class: 'ps-card__top' },
    streamStatusChip(st),
    h('h3', { class: 'ps-card__title' }, String(stream.title || 'Без названия')),
    originNode(stream)),
  stream.goal
    ? h('p', { class: 'ps-card__goal' }, String(stream.goal))
    : null,
  h('div', { class: 'ps-card__meta small muted' },
    owner,
    members,
    due,
    dep ? h('span', { class: 'ps-card__dep', title: 'Начнётся после: ' + dep.replace(/^после /, '') },
      icon('arrow-swap', { size: 11 }), h('span', { class: 'truncate' }, dep)) : null,
    rid
      ? h('a', {
        class: 'ps-card__request',
        href: requestHrefOf(ctx, rid),
        title: 'Открыть под-запрос этого направления',
      }, icon('link-external', { size: 11 }), 'под-запрос')
      : null),
  h('div', { class: 'ps-card__actions' },
    canLaunch
      ? actionButton('Запустить', {
        variant: 'btn--secondary', icon: 'play',
        title: 'Поставить направление в работу',
        onclick: () => ctx.onLaunch(id),
      })
      : null,
    canDrop
      ? actionButton('Отложить', {
        variant: 'btn--ghost', icon: 'archive',
        title: 'Убрать из работы, не удаляя: можно вернуть позже',
        onclick: () => ctx.onDrop(id),
      })
      : null,
    actionButton('Изменить', {
      variant: 'btn--ghost', icon: 'edit',
      title: 'Поправить название, цель, ответственного и срок',
      onclick: () => ctx.onEdit(id),
    })));
}

/* -------------------------------------------------------------------- блок */

/**
 * Показывать ли блок «Направления» в комнате.
 * @param {{project_mode?: boolean}|null} room
 * @returns {boolean}
 */
export function planBlockVisible(room) {
  if (!room || typeof room !== 'object') return true;
  return room.project_mode !== false;
}

/**
 * Собрать блок «Направления».
 *
 * @param {{
 *   plan: object|null, roomId: string, room?: object|null, now?: Date,
 *   busy?: boolean, maxParallel?: number,
 *   people?: Map|object|Array|null,
 *   requestHref?: (rid: string) => string,
 *   onLaunchAll?: Function, onLaunch?: Function, onDrop?: Function,
 *   onEdit?: Function, onAdd?: Function, onRebuild?: Function,
 *   onComposer?: Function,
 * }} opts
 * @returns {HTMLElement}
 */
export function renderStreams(opts) {
  const o = opts || {};
  const plan = o.plan && typeof o.plan === 'object' ? o.plan : null;
  const streams = listOf(plan);
  const byId = {};
  for (const s of streams) byId[String(s.id || '')] = s;

  const busy = !!o.busy;
  const launchIds = launchableIds(plan);
  const summary = streamsSummary(plan);

  const header = h('div', { class: 'card__head ps-head' },
    h('div', { class: 'ps-head__titles' },
      h('div', { class: 'card__title' }, 'Направления'),
      h('div', { class: 'ps-head__sub small muted' },
        plan ? (PLAN_STATUS[norm(plan.status)] || 'карта проекта') : 'карта проекта',
        h('span', { class: 'ps-head__count tnum' }, streamsCountLine(plan)))),
    h('div', { class: 'card__actions ps-head__actions' },
      launchIds.length
        ? h('button', {
          class: 'btn btn--primary btn--sm ps-launch-all',
          type: 'button',
          disabled: busy || undefined,
          title: 'Поставить в работу все предложенные направления',
          onclick: () => o.onLaunchAll && o.onLaunchAll(launchIds.slice()),
        }, icon('rocket', { size: 13 }), 'Запустить всё')
        : null,
      h('button', {
        class: 'btn btn--secondary btn--sm',
        type: 'button',
        disabled: busy || undefined,
        title: 'Добавить направление в карту вручную',
        onclick: () => o.onAdd && o.onAdd(),
      }, icon('add', { size: 13 }), 'Добавить направление'),
      h('button', {
        class: 'btn btn--ghost btn--sm ps-rebuild',
        type: 'button',
        disabled: busy || undefined,
        title: 'Руководитель комнаты посмотрит уже сделанные запросы и соберёт карту заново',
        onclick: () => o.onRebuild && o.onRebuild(),
      }, icon('sync', { size: 12 }), 'Собрать карту из запросов комнаты')));

  const body = h('div', { class: 'card__body ps-body' });
  if (!streams.length) {
    body.appendChild(h('div', { class: 'empty ps-empty' },
      icon('map', { size: 22 }),
      h('div', { class: 'empty__title' }, 'Карты пока нет'),
      h('div', { class: 'empty__text' },
        'Напишите главный запрос — руководитель комнаты предложит направления.'),
      h('div', { class: 'empty__actions' },
        o.onComposer
          ? h('button', {
            class: 'btn btn--secondary btn--sm', type: 'button',
            onclick: () => o.onComposer(),
          }, 'Написать главный запрос')
          : null)));
  } else {
    body.appendChild(h('div', { class: 'ps-grid' }, streams.map((s) => streamCard(s, {
      byId,
      busy,
      now: o.now,
      roomId: o.roomId,
      people: o.people,
      requestHref: o.requestHref,
      onLaunch: (id) => o.onLaunch && o.onLaunch(id),
      onDrop: (id) => o.onDrop && o.onDrop(id),
      onEdit: (id) => o.onEdit && o.onEdit(id),
    }))));
    if (summary.waiting) {
      body.appendChild(h('p', { class: 'ps-note small muted' },
        'Ждёт лимит — направление продолжится само, когда лимит модели снимется.'
        + ' Пока лимит занят, новые направления не начинаются.'));
    }
  }

  return h('section', { class: 'card ps', dataset: { plan: plan ? 'yes' : 'no' } }, header, body);
}

/** Тихая ссылка, если проектный режим в комнате выключен. */
export function projectModeHint(onOpen) {
  return h('button', {
    class: 'btn btn--link btn--sm ps-off', type: 'button',
    title: 'Проектный режим включает «Карту проекта» в настройках комнаты',
    onclick: () => onOpen && onOpen(),
  }, 'Включить проектный режим в настройках комнаты');
}

/** Снять узел с родителя: браузер, мини-DOM в тестах или обычный массив детей. */
function detach(node) {
  if (!node) return;
  if (typeof node.remove === 'function') { node.remove(); return; }
  const parent = node.parentNode;
  if (!parent) return;
  if (typeof parent.removeChild === 'function') { parent.removeChild(node); return; }
  const kids = parent.childNodes;
  if (Array.isArray(kids)) {
    const i = kids.indexOf(node);
    if (i !== -1) kids.splice(i, 1);
  }
  node.parentNode = null;
}
/* ------------------------------------------------------------------ диалог */

function closeOnEscape(overlay, close) {
  overlay.addEventListener('keydown', (ev) => {
    if (ev && ev.key === 'Escape') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
      close(null);
    }
  });
}

/**
 * Диалог «Изменить» / «Добавить направление». Поля получают микрофон сами.
 *
 * @param {{mode?: 'edit'|'add', stream?: object|null, members?: Array,
 *          people?: Map|object|Array|null,
 *          title?: string, onCancel?: Function}} opts
 * @returns {Promise<{title: string, goal: string, owner: string,
 *                    members: string[], due: string|null}|null>}
 */
export function openStreamEditor(opts) {
  const o = opts || {};
  const d = globalThis.document;
  if (!d || !d.body) return Promise.resolve(null);

  const stream = o.stream && typeof o.stream === 'object' ? o.stream : {};
  const isEdit = o.mode !== 'add';
  const index = peopleIndex(o.people);
  const members = (Array.isArray(o.members) ? o.members : [])
    .map((m) => (typeof m === 'string' ? m : String((m && (m.profile || m.id)) || '')))
    .filter(Boolean);

  return new Promise((resolve) => {
    let done = false;
    const titleId = uid('ps-dlg');

    const titleInput = h('input', {
      class: 'input', type: 'text', maxlength: 120,
      placeholder: 'Например: договор с поставщиком',
    });
    titleInput.value = String(stream.title || '');

    const goalInput = h('textarea', {
      class: 'textarea', rows: 2,
      placeholder: 'Одной строкой: что должно получиться',
    });
    goalInput.value = String(stream.goal || '');

    const ownerSelect = h('select', { class: 'select' });
    const ownerList = (stream.owner && !members.includes(String(stream.owner)))
      ? [String(stream.owner)].concat(members) : members.slice();
    ownerSelect.appendChild(h('option', { value: '' }, 'Не выбран'));
    for (const m of ownerList) {
      const opt = h('option', { value: m, title: m }, personName(m, index));
      if (String(stream.owner || '') === m) opt.setAttribute('selected', '');
      ownerSelect.appendChild(opt);
    }
    // В браузере значение подставилось бы само по selected, но выставляем явно:
    // так диалог одинаково ведёт себя и в тестовом мини-DOM.
    ownerSelect.value = String(stream.owner || '');

    const memberBoxes = ownerList.map((m) => {
      const box = h('input', { type: 'checkbox', value: m, class: 'checkbox__input' });
      const checked = Array.isArray(stream.members) && stream.members.map(String).includes(m);
      if (checked) box.setAttribute('checked', '');
      box.checked = checked;
      return { profile: m, box, label: h('label', { class: 'ps-members__item', title: m }, box, h('span', {}, personName(m, index))) };
    });

    const dueInput = h('input', { class: 'input', type: 'date' });
    if (stream.due) dueInput.value = String(stream.due).slice(0, 10);

    const overlay = h('div', { class: 'dialog-backdrop' });
    const finish = (value) => {
      if (done) return;
      done = true;
      detach(overlay);
      resolve(value);
    };
    closeOnEscape(overlay, finish);
    overlay.addEventListener('mousedown', (ev) => { if (ev && ev.target === overlay) finish(null); });

    const dialog = h('div', {
      class: 'dialog dialog--wide ps-dialog',
      role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
    },
    h('div', { class: 'dialog__head' },
      h('h2', { class: 'dialog__title', id: titleId },
        isEdit ? 'Изменить направление' : 'Добавить направление'),
      h('button', {
        class: 'btn btn--icon btn--ghost btn--sm', type: 'button',
        title: 'Закрыть', 'aria-label': 'Закрыть',
        onclick: () => finish(null),
      }, icon('close', { size: 13 }))),
    h('div', { class: 'dialog__body ps-dialog__body' },
      h('label', { class: 'field', for: titleInput.id || (titleInput.id = uid('ps-t')) },
        h('span', { class: 'field__label' }, 'Название'),
        h('div', { class: 'ps-dialog__area' }, titleInput)),
      h('label', { class: 'field', for: goalInput.id || (goalInput.id = uid('ps-g')) },
        h('span', { class: 'field__label' }, 'Цель'),
        h('div', { class: 'ps-dialog__area' }, goalInput),
        h('span', { class: 'field__hint' }, 'Что должно получиться — коротко, одной фразой.')),
      h('label', { class: 'field', for: ownerSelect.id || (ownerSelect.id = uid('ps-o')) },
        h('span', { class: 'field__label' }, 'Ответственный'),
        ownerSelect),
      h('div', { class: 'field' },
        h('span', { class: 'field__label' }, 'Участники'),
        h('div', { class: 'ps-members' }, memberBoxes.map((m) => m.label))),
      h('label', { class: 'field', for: dueInput.id || (dueInput.id = uid('ps-d')) },
        h('span', { class: 'field__label' }, 'Срок'),
        dueInput),
      h('p', { class: 'dialog__hint small muted' },
        'Поля можно продиктовать — микрофон справа в поле.')),
    h('div', { class: 'dialog__foot' },
      h('button', {
        class: 'btn btn--secondary', type: 'button',
        onclick: () => finish(null),
      }, 'Отмена'),
      h('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: () => {
          const title = String(titleInput.value || '').trim();
          if (title.length < 3) {
            titleInput.focus && titleInput.focus();
            titleInput.setAttribute('aria-invalid', 'true');
            return;
          }
          const picked = memberBoxes.filter((m) => m.box.checked).map((m) => m.profile);
          finish({
            title,
            goal: String(goalInput.value || '').trim(),
            owner: String(ownerSelect.value || '').trim(),
            members: picked,
            due: String(dueInput.value || '').trim() || null,
          });
        },
      }, isEdit ? 'Сохранить' : 'Добавить')));

    overlay.appendChild(dialog);
    (d.body || d.documentElement).appendChild(overlay);
    if (typeof titleInput.focus === 'function') titleInput.focus();
  });
}

/**
 * Человеческий текст ошибки для тоста (без кодов и технических слов).
 * @param {*} err
 * @returns {string}
 */
export function humanError(err) {
  const code = String((err && err.code) || '');
  // Сеть: сообщение браузера («Failed to fetch») не показываем — говорим, что делать.
  if (code === 'network') {
    return 'Нет связи с приложением. Проверьте, что оно запущено, и повторите.';
  }
  try {
    const t = errorText(err);
    if (t && t.message) return t.message;
  } catch { /* ниже — запасной вариант */ }
  const msg = err && err.message ? String(err.message) : '';
  if (msg && !/failed to fetch|networkerror|load failed/i.test(msg)) return msg;
  return 'Не удалось выполнить действие. Попробуйте ещё раз.';
}

export default {
  STREAM_STATUSES,
  streamStatusMeta,
  streamStatusChip,
  streamOrigin,
  streamsSummary,
  streamsCountLine,
  launchableIds,
  peopleIndex,
  ownerLabelOf,
  dependencyNote,
  launchConfirm,
  planBlockVisible,
  renderStreams,
  projectModeHint,
  openStreamEditor,
  humanError,
};
