/**
 * approvals.js — «Разрешения агентам» (волна 22, пакет AP2).
 *
 * Агент команды упёрся в запрет Hermes («опасная команда», режим без человека)
 * и не может двигаться дальше. В обычном чате Hermes человек видит окно
 * разрешения; здесь то же самое, но по-русски и в трёх местах:
 *
 *   * карточка «<Участник> хочет выполнить команду» в «Нужен ваш ответ»
 *     (правая колонка страницы запроса) — `createApprovals(R).pendingCards()`;
 *   * блок «Разрешения агентам» на странице запроса — `buildRequestBlock()`;
 *   * строка «Разрешение» во вкладке «От вас нужно» — тот же `approvalCard`;
 *   * раздел «Разрешения агентов» в настройках — `mountGrantsSection`.
 *
 * Что важно для человека (WAVE22 §0.3): видно ВСЮ команду (моноширинный блок
 * с прокруткой), причину человеческим языком, участника и карточку. Технику
 * (id, pattern_key, провайдер) наружу не показываем.
 *
 * Три решения: «Разрешить один раз», «Разрешать всегда», «Запретить». Кнопки
 * блокируются на время запроса — двойное нажатие не создаёт дублей; после
 * ответа карточка сразу показывает итог, не дожидаясь опроса страницы.
 * `floor` (жёсткий запрет Hermes) разрешить нельзя — серая плашка вместо кнопок.
 *
 * Модуль проверяется без браузера: `node --test tests/approvals-ui.test.mjs`.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, confirmDialog } from '../ui.js';
import { countNoun, APPROVALS_RU } from '../text-ru.js';
// W26-D2: один стор запроса на всю страницу — «одно действие — везде».
import { requestStoreFor, bindLive } from './request-store.js';
// W27-E2 §1: состояния, причины закрытия и тихий 409 — одно мнение на всю страницу.
import {
  closedReasonText, isAlreadyDecided, isGone, staleSummary, needsYouCount,
  dismissBody, restoreBody,
} from './stale-model.js';

export const APPROVALS_CSS = 'css/approvals.css';

/** Три решения по запросу разрешения — подписи и подсказки в одном месте. */
export const APPROVAL_DECISIONS = [
  {
    decision: 'once',
    label: APPROVALS_RU.once,
    tone: 'btn--primary',
    hint: 'Команда выполнится один раз: разрешение снимется, когда карточка закончится (или через 30 минут)',
  },
  {
    decision: 'always',
    label: APPROVALS_RU.always,
    tone: 'btn--secondary',
    hint: APPROVALS_RU.alwaysHint,
  },
  {
    decision: 'deny',
    label: APPROVALS_RU.deny,
    tone: 'btn--ghost',
    hint: 'Участник не выполнит команду и будет искать другой способ',
  },
];

/** Состояния запроса разрешения (`state` из API) — слова и тон чипа. */
export const APPROVAL_STATES = {
  pending: { label: APPROVALS_RU.states.pending, tone: 'warn' },
  granted_once: { label: APPROVALS_RU.states.granted_once, tone: 'info' },
  granted_always: { label: APPROVALS_RU.states.granted_always, tone: 'ok' },
  denied: { label: APPROVALS_RU.states.denied, tone: 'danger' },
  expired: { label: APPROVALS_RU.states.expired, tone: 'muted' },
  // W27-E2 §1: `closed` — сервер закрыл пункт сам (работа карточки закончена или
  // запрос выполнен). Интерфейс ОБЯЗАН знать это состояние: дефект 18.11 вечером
  // был ровно в том, что `closed` не значился в списке и строка рисовалась как
  // `pending` — «ждёт вашего решения» без единого способа решить.
  closed: { label: 'работа завершена', tone: 'muted' },
  // W26-D2: исходы «нельзя штатно» — у человека есть рычаги, и они видны словами.
  hinted: { label: 'подсказано участнику', tone: 'info' },
  dismissed: { label: 'закрыто как неактуальное', tone: 'muted' },
  acked: { label: 'скрыто', tone: 'muted' },
  // W27-E2 §1: слово, которого интерфейс не знает, — НЕ «ждёт вас». Так безопаснее:
  // счётчик и список не зовут решать то, о чём экран ничего не знает.
  unknown: { label: 'состояние неизвестно', tone: 'muted' },
};

/** Что значит состояние — одна подсказка на все карточки. */
export const APPROVAL_STATE_HINTS = {
  pending: 'Участник остановлен: команда помечена Hermes как опасная, а в запуске без человека '
    + 'он не может спросить сам. Решение — за вами.',
  granted_once: 'Команда разрешена один раз. Разрешение снимется само, когда карточка закончится '
    + '(или через 30 минут).',
  granted_always: 'Такие команды этого участника больше не спрашиваются. '
    + 'Отозвать можно в Настройках → Разрешения агентов.',
  denied: 'Вы запретили команду: участник будет искать другой способ.',
  expired: 'Время одноразового разрешения вышло — команда снова под вопросом.',
  closed: 'Пункт закрыт сервером: работа карточки закончена (или запрос выполнен). '
    + 'Решать здесь нечего — строка только для памяти; её можно вернуть кнопкой «Вернуть», '
    + 'если пункт снова стал нужен.',
  hinted: 'Участнику отправлена подсказка в ветку карточки: сделать то же через файл и терминал. '
    + 'Блокировка снята с вас, работа продолжается.',
  dismissed: 'Пункт закрыт как неактуальный: ждать его некому, в счётчиках его нет.',
  acked: 'Строка убрана с экрана: это был не вопрос к вам, а информация о жёстком запрете.',
  unknown: 'Экран не знает такого состояния — поэтому пункт не считается «ждёт вас». '
    + 'Обновите страницу: если состояние свежее, оно появится.',
};

/** Вид команды человеческими словами (без техники). */
export const APPROVAL_KIND_RU = {
  terminal: 'команда в терминале',
  execute_code: 'код на Python',
};

/** Три вида блокировки (W26 §1–2, Prep §1): у каждого — свой набор действий. */
export const DEAD_ENDS = { grantable: 'grantable', workaround: 'workaround', floor: 'floor' };

/** Подписи видов: что человек видит в чипе и подсказке. */
export const DEAD_END_RU = {
  grantable: {
    label: 'можно разрешить',
    tone: 'warn',
    hint: 'Hermes спрашивает вашего разрешения — решение за вами, работа пойдёт дальше.',
  },
  workaround: {
    label: 'нельзя штатно, но есть способ',
    tone: 'info',
    hint: 'Разрешением такую блокировку не снять, но у вас есть рычаги: подсказать участнику '
      + 'другой путь, доверить запуск кода профилю или закрыть пункт как неактуальный.',
  },
  floor: {
    label: 'жёсткий запрет Hermes',
    tone: 'muted',
    hint: 'Это не вопрос к вам: Hermes запрещает настрого. Участнику уже передано искать '
      + 'другой способ — пункт не считается в «Нужен ваш ответ».',
  },
};

/**
 * Вид блокировки: `grantable` | `workaround` | `floor`.
 *
 * Сервер отдаёт поле `dead_end` (W26, D1); старые ответы читаются по
 * `floor`/`grantable`, поэтому интерфейс работает и с ними.
 * @param {object} a запрос разрешения из API
 * @returns {'grantable'|'workaround'|'floor'}
 */
export function approvalDeadEnd(a) {
  const src = a && typeof a === 'object' ? a : {};
  const own = String(src.dead_end || '').trim().toLowerCase();
  if (src.floor === true || src.floor === 'true' || own === DEAD_ENDS.floor) return DEAD_ENDS.floor;
  if (own === DEAD_ENDS.workaround || src.grantable === false) return DEAD_ENDS.workaround;
  return DEAD_ENDS.grantable;
}

/**
 * Пункт действительно ждёт человека: `pending` и у него есть действие.
 *
 * Правило Михаила «без тупиков»: жёсткий запрет (`floor`) — информация, а не
 * вопрос, поэтому в «Нужен ваш ответ» и в счётчики он не входит.
 */
export function isWaiting(a) {
  return approvalState(a) === 'pending' && approvalDeadEnd(a) !== DEAD_ENDS.floor;
}

/** Ждущие решения с действием (счётчики и «Нужен ваш ответ»). */
export function waitingApprovals(list) {
  return (Array.isArray(list) ? list : []).filter(isWaiting);
}

/** Жёсткие запреты — информационные строки под счётчиком. */
export function floorApprovals(list) {
  return (Array.isArray(list) ? list : [])
    .filter((a) => a && approvalState(a) === 'pending' && approvalDeadEnd(a) === DEAD_ENDS.floor);
}

/** Сроки доверия профилю (обязателен: «навсегда» — отдельный выбор человека). */
export const TRUST_TTL = [
  { ttl: 'card', label: 'на эту карточку', hint: 'Разрешение снимется, когда карточка закончится' },
  { ttl: 'min30', label: 'на 30 минут', hint: 'Через полчаса Hermes снова начнёт спрашивать' },
  { ttl: 'forever', label: 'навсегда', hint: 'До отзыва в Настройках → Разрешения агентов' },
];

/**
 * Действия пункта по его виду. У каждого вида — хотя бы одно действие, которое
 * реально что-то меняет (правило «без тупиков»).
 *
 * @param {object} a запрос разрешения
 * @returns {Array<{action:string,label:string,tone:string,hint:string,ttl?:string}>}
 */
export function approvalActionsFor(a) {
  const deadEnd = approvalDeadEnd(a);
  if (deadEnd === DEAD_ENDS.floor) {
    return [
      { action: 'ack', label: 'Понятно, скрыть', tone: 'btn--ghost',
        hint: 'Убрать строку с экрана: это информация, а не вопрос к вам' },
      { action: 'hint_agent', label: 'Сообщить участнику', tone: 'btn--secondary',
        hint: 'Team Room напишет участнику в ветку карточки: запрет жёсткий, ищи другой способ' },
    ];
  }
  if (deadEnd === DEAD_ENDS.workaround) {
    return [
      { action: 'hint_agent', label: 'Подсказать участнику обойтись терминалом', tone: 'btn--primary',
        hint: 'Team Room сам напишет участнику в ветку карточки: «execute_code в фоне недоступен — '
          + 'сделай то же через файл и терминал». Риск нулевой, безопасность не ослабляется' },
      { action: 'trust_profile', label: 'Доверять запуску кода участнику…', tone: 'btn--secondary',
        hint: 'Штатная настройка Hermes для этого профиля: в конечных запусках опасные команды '
          + 'больше не спрашиваются. Жёсткие запреты остаются. Срок обязателен' },
      { action: 'dismiss', label: 'Закрыть как неактуальное', tone: 'btn--ghost',
        hint: 'Блокировка уйдёт из счётчиков: ждать её уже некому' },
    ];
  }
  return [
    { action: 'once', decision: 'once', label: APPROVALS_RU.once, tone: 'btn--primary',
      hint: 'Команда выполнится один раз: разрешение снимется, когда карточка закончится (или через 30 минут)' },
    { action: 'always', decision: 'always', label: APPROVALS_RU.always, tone: 'btn--secondary',
      hint: APPROVALS_RU.alwaysHint },
    { action: 'deny', decision: 'deny', label: APPROVALS_RU.deny, tone: 'btn--ghost',
      hint: 'Участник не выполнит команду и будет искать другой способ' },
    // W27-E2 §1: «Не актуально» есть у КАЖДОГО ждущего пункта, не только у
    // «нельзя штатно». Счётчик обнуляется без «принятия решения» — с возвратом.
    { action: 'dismiss', label: 'Не актуально', tone: 'btn--ghost',
      hint: 'Убрать из «Нужен ваш ответ» и из счётчиков, не решая: пункт останется в «Закрытые», '
        + 'и его можно вернуть одной кнопкой' },
  ];
}

/**
 * Действия у пункта, который уже не ждёт человека (W27-E2 §1): «Вернуть».
 * Закрытое сервером и снятое человеком возвращаются одним и тем же действием.
 * @param {object} a запрос разрешения
 * @returns {Array<{action:string,label:string,tone:string,hint:string}>}
 */
export function approvalStaleActions(a) {
  if (!isRestorable(a)) return [];
  const state = approvalState(a);
  const why = closedReasonText(a) || (state === 'dismissed' ? 'закрыто как неактуальное' : '');
  return [{
    action: 'restore', label: 'Вернуть', tone: 'btn--ghost',
    hint: 'Снова поставить пункт в «Нужен ваш ответ», если он опять нужен'
      + (why ? ' — сейчас: ' + why : ''),
  }];
}



/** Запасная причина, если сервер её не прислал. */
const KIND_REASON = {
  terminal: 'запуск команды, которую Hermes считает опасной',
  execute_code: 'выполнение произвольного кода на Python',
};

/** Длиннее — раскрываем свёрнутым: в колонке такой блок не должен «съесть» экран. */
const LONG_COMMAND = 240;

/* ─────────────────────────────────────────────── чистые помощники (тесты) */

/**
 * Состояние запроса разрешения.
 *
 * W27-E2 §1 (дефект 1): раньше ЛЮБОЕ незнакомое слово считалось «ждёт» — и 20
 * закрытых разрешений висели в счётчике как «27», а кнопки решений вели в 409.
 * Теперь так: знаем слово — берём его; не знаем — `unknown` (не «ждёт вас»).
 * Записи без `state` (старый сервер) и с `answered/decided_at` читаются как решённые.
 */
export function approvalState(a) {
  const src = a && typeof a === 'object' ? a : {};
  const raw = String(src.state || src.status || '').trim().toLowerCase();
  if (raw && Object.prototype.hasOwnProperty.call(APPROVAL_STATES, raw)) return raw;
  if (src.decided_at || src.answered === true || src.grant_scope) return 'granted_once';
  if (src.card_state === 'done' || src.card_state === 'archived') return 'closed';
  return 'unknown';
}

/** Запрос ещё ждёт решения человека. */
export function isPending(a) {
  return approvalState(a) === 'pending';
}

/** Пункт закрыт сервером (`closed`) — решать нечего, но можно вернуть. */
export function isClosed(a) {
  return approvalState(a) === 'closed';
}

/** Пункт можно вернуть в работу («Вернуть» у закрытого и снятого). */
export function isRestorable(a) {
  const state = approvalState(a);
  return state === 'closed' || state === 'dismissed' || state === 'expired';
}

/** Только те, что ждут решения. */
export function pendingApprovals(list) {
  return (Array.isArray(list) ? list : []).filter(isPending);
}

/** Закрытые сервером строки — для свёрнутого списка «Закрытые». */
export function closedApprovals(list) {
  return (Array.isArray(list) ? list : []).filter(isClosed);
}

/** Сколько строк у запроса закрыто и почему — для подписи свёрнутого списка. */
export function closedSummary(list) {
  const items = Array.isArray(list) ? list.filter(isClosed) : [];
  const reasons = new Map();
  for (const a of items) {
    const text = closedReasonText(a) || 'работа завершена';
    reasons.set(text, (reasons.get(text) || 0) + 1);
  }
  // Одна причина — говорим её без числа (число уже в «Закрытые: N запросов»);
  // несколько причин — с числами, чтобы было видно, чего больше.
  const many = reasons.size > 1;
  const detail = Array.from(reasons.entries())
    .map(([text, n]) => (many ? text + ' · ' + String(n) : text))
    .join(', ');
  return { count: items.length, text: detail };
}

/**
 * Собрать ждущие разрешения из деталей запросов (`detail.approvals`) — тем же
 * приёмом, что `blockedCards`: одна строка = один запрос разрешения, с ссылкой
 * на свою карточку.
 * @param {Array} details RequestDetail[]
 * @returns {Array}
 */
export function collectApprovals(details) {
  const out = [];
  const seen = new Set();
  for (const detail of Array.isArray(details) ? details : []) {
    if (!detail || !Array.isArray(detail.approvals)) continue;
    for (const a of detail.approvals) {
      // W26-D2 (T1): в счётчики попадают только пункты с действием; жёсткий
      // запрет — информация, а не «ждёт вашего решения».
      if (!a || !isWaiting(a)) continue;
      const id = String(a.id || '');
      if (id && seen.has(id)) continue;
      if (id) seen.add(id);
      out.push(Object.assign({}, a, {
        request_id: a.request_id || detail.id || null,
        request_title: a.request_title || detail.title || '',
      }));
    }
  }
  return out.sort((x, y) => (Number(x.at) || 0) - (Number(y.at) || 0));
}

/** «Анна хочет выполнить команду» — имя участника, без профиля и id. */
export function approvalHeadline(a) {
  const who = String((a && (a.member_title || a.member || a.profile)) || 'Участник').trim();
  return who + ' хочет выполнить команду';
}

/** Причина человеческим языком: описание сервера, иначе — по виду команды. */
export function approvalReason(a) {
  const own = String((a && a.description) || '').trim();
  if (own) return own;
  const kind = String((a && a.kind) || 'terminal');
  return KIND_REASON[kind] || KIND_REASON.terminal;
}

/** Вид команды: «команда в терминале» / «код на Python». */
export function approvalKind(a) {
  return APPROVAL_KIND_RU[String((a && a.kind) || 'terminal')] || APPROVAL_KIND_RU.terminal;
}

/**
 * Короткая подпись команды для свёрнутого вида: первая строка, без хвоста.
 * ПОЛНЫЙ текст команды всегда остаётся в теле блока — здесь только превью.
 * @param {string} command
 * @param {number} [max]
 */
export function commandPreview(command, max = 120) {
  const text = String(command === null || command === undefined ? '' : command);
  const first = text.split('\n').find((line) => String(line).trim()) || '';
  const line = String(first).trim().replace(/\s+/g, ' ');
  const limit = Math.max(8, Number(max) || 120);
  if (line.length <= limit) return line;
  return line.slice(0, limit - 1).trimEnd() + '…';
}

/**
 * Строка-группа (W22-ST): одинаковые по причине блокировки сведены в один вопрос.
 *
 * Сервер отдаёт `group`, `approval_ids` и `commands[]` (свежие первыми). Здесь — одна
 * форма для вида и тестов: `last` (последняя команда, её и показываем в строке),
 * `rest` (остальные — раскрываемым списком), `count` (сколько блокировок всего) и
 * `commandsCount` (сколько разных команд в группе).
 *
 * @param {object} a Approval из API
 * @returns {{group:string, ids:string[], commands:Array, last:object|null,
 *            rest:Array, count:number, commandsCount:number, grouped:boolean}}
 */
export function approvalGroup(a) {
  const src = a && typeof a === 'object' ? a : {};
  const ids = Array.isArray(src.approval_ids)
    ? src.approval_ids.map((id) => String(id || '')).filter(Boolean) : [];
  const commands = Array.isArray(src.commands)
    ? src.commands
      .filter((c) => c && typeof c === 'object')
      .map((c) => ({
        id: String(c.id || ''),
        command: String(c.command || ''),
        at: Number(c.at) || 0,
        count: Math.max(1, Math.trunc(Number(c.count) || 1)),
      }))
      .filter((c) => c.command)
    : [];
  const count = Math.max(1, Math.trunc(Number(src.count) || 0) || commands.length || ids.length || 1);
  const last = commands.length
    ? commands[0]
    : (String(src.command || '').trim()
      ? { id: String(src.id || ''), command: String(src.command || ''), at: Number(src.at) || 0, count }
      : null);
  const rest = commands.length > 1 ? commands.slice(1) : [];
  return {
    group: String(src.group || ''),
    ids,
    commands,
    last,
    rest,
    count,
    commandsCount: commands.length || (last ? 1 : 0),
    grouped: Boolean(String(src.group || '')) || ids.length > 1,
  };
}

/** «15:40» из unix-секунд ('' — времени нет). */
export function clockLabel(at) {
  const sec = Number(at) || 0;
  if (!sec) return '';
  const date = new Date(sec * 1000);
  if (!isFinite(date.getTime())) return '';
  return String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
}

/** Подпись области разрешения: «всегда» / «один раз». */
export function grantScopeLabel(scope) {
  return String(scope || '') === 'always' ? 'всегда' : 'один раз';
}

/** Откуда взялось разрешение: выдали здесь или оно уже было в настройках Hermes. */
export function grantSourceLabel(source) {
  return String(source || '') === 'room' ? 'выдано здесь' : 'уже было в настройках Hermes';
}

/** Итог решения — одной строкой; используется и в карточке, и в настройках. */
export function decisionResultText(decision, res, a) {
  const grant = (res && res.grant) || null;
  const restarted = res ? Boolean(res.restarted) : false;
  if (decision === 'deny') return 'Запрещено — участник будет искать другой способ';
  if (decision === 'always' || (grant && grant.scope === 'always')) {
    return 'Разрешено всегда: такие команды этого участника больше не спрашиваются. '
      + 'Отозвать можно в Настройках → Разрешения агентов';
  }
  const until = clockLabel((res && res.grant && res.grant.expires_at)
    || (res && res.approval && res.approval.expires_at) || (a && a.expires_at));
  return 'Разрешено один раз' + (until ? ' — до ' + until : ' — до завершения карточки')
    + (restarted ? '; карточка перезапущена' : '; как только участник продолжит, он выполнит команду');
}

/**
 * Текст ошибки человеческим языком: 409 — уже решено, 422 — разрешить нельзя
 * (жёсткий запрет Hermes или не найдена программа Hermes), иначе — сообщение
 * сервера, из которого убраны служебные слова.
 */
export function approvalErrorText(err) {
  const status = Number(err && (err.status || (err.details && err.details.status))) || 0;
  const code = String((err && (err.code || err.error)) || '');
  if (status === 409 || code === 'conflict') {
    return 'Это разрешение уже решено — страница обновится и покажет итог';
  }
  if (status === 422 || code === 'unprocessable') {
    return 'Разрешить нельзя: команда запрещена политикой безопасности Hermes '
      + 'или не найдена программа Hermes этого участника';
  }
  if (status === 404) return 'Запрос разрешения больше не существует — обновите страницу';
  const own = String((err && err.message) || '').trim();
  if (own && !/^http_\d+$/.test(own) && own.length < 200) return own;
  return 'Не удалось передать решение — попробуйте ещё раз';
}

/**
 * Тело действия `approval` (контракт W22 §2.2).
 *
 * `group` — НЕОБЯЗАТЕЛЬНОЕ поле (W22-ST): сервер и без него решает всю группу, но с
 * ним видно, что человек решал строку-группу, а не один запрос. Старые серверы поле
 * игнорируют.
 */
export function approvalBody(a, decision) {
  const body = {
    action: 'approval',
    approval_id: String((a && a.id) || ''),
    decision: String(decision || ''),
  };
  const group = String((a && a.group) || '');
  if (group) body.group = group;
  return body;
}

/** «1 разрешение ждёт вашего решения» / «3 разрешения ждут вашего решения». */
export function approvalsWaitingLabel(n) {
  const count = Math.max(0, Math.trunc(Number(n) || 0));
  const forms = ['разрешение', 'разрешения', 'разрешений'];
  if (!count) return 'нет запросов разрешения';
  const verb = count === 1 ? 'ждёт' : 'ждут';
  return countNoun(count, forms) + ' ' + verb + ' вашего решения';
}

/** Итог по списку выданных разрешений: «2 разрешения: 1 всегда, 1 один раз». */
export function grantsSummary(items) {
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  const always = list.filter((g) => String(g.scope || '') === 'always').length;
  const once = list.length - always;
  if (!list.length) return { total: 0, always: 0, once: 0, text: '' };
  const parts = [];
  if (always) parts.push(String(always) + ' — всегда');
  if (once) parts.push(String(once) + ' — один раз');
  return {
    total: list.length,
    always,
    once,
    text: countNoun(list.length, ['разрешение', 'разрешения', 'разрешений']) + ': ' + parts.join(', '),
  };
}

/* ─────────────────────────────────────────────────────────── вид карточки */

function stateMeta(state) {
  return APPROVAL_STATES[state] || APPROVAL_STATES.unknown;
}

function chipTone(state) {
  return stateMeta(state).tone;
}

function stateChip(state, deadEnd) {
  // W26 §1: у жёсткого запрета это не «решение», а информация — и слово в чипе
  // должно быть честным, иначе человек опять видит тупик.
  if (deadEnd === DEAD_ENDS.floor && state === 'pending') {
    return h('span', {
      class: 'chip chip--muted ap__state',
      'data-ap-state': state,
      'data-ap-info': 'floor',
      title: 'Это не вопрос к вам: ' + DEAD_END_RU.floor.hint,
    }, 'информация');
  }
  const meta = stateMeta(state);
  return h('span', {
    class: 'chip chip--' + meta.tone + ' ap__state',
    'data-ap-state': state,
    title: 'Состояние: ' + meta.label + ' — ' + (APPROVAL_STATE_HINTS[state] || ''),
  }, meta.label);
}

function stateHint(state) {
  const text = APPROVAL_STATE_HINTS[state] || '';
  return text ? h('div', { class: 'ap__hint small muted' }, text) : null;
}

/** Блок «Команда»: свёрнут у длинных, развёрнут у коротких в широком месте. */
function commandBlock(a, compact) {
  const command = String((a && a.command) || '');
  if (!command.trim()) return null;
  const lines = command.split('\n').length;
  const long = command.length > LONG_COMMAND || lines > 6;
  const preview = commandPreview(command, compact ? 60 : 120);
  const summary = h('summary', { class: 'ap__cmd-sum' },
    icon('code', { size: 11 }),
    h('span', { class: 'ap__cmd-cap' }, 'Команда'),
    preview ? h('span', { class: 'ap__cmd-prev mono', title: preview }, preview) : null,
    long ? h('span', { class: 'ap__cmd-len small muted tnum' }, String(command.length) + ' знаков') : null);
  return h('details', { class: 'ap__cmd', open: !long ? true : null, 'data-long': long ? 'true' : null },
    summary,
    h('div', { class: 'ap__cmd-body' },
      h('pre', {
        class: 'ap__code mono', tabindex: '0',
        'aria-label': 'Команда целиком, которую просит выполнить участник',
      }, command)));
}

/** «Карточка: <название>» — ссылка, если знаем адрес запроса. */
function cardLine(a, opts) {
  const title = String((a && (a.request_title || a.card_title || a.card)) || '').trim();
  if (!title) return null;
  const rid = String((a && (a.request_id || a.task_id)) || '').trim();
  const href = opts.requestHref || (rid ? '#/room/' + encodeURIComponent(opts.roomId || '')
    + '/request/' + encodeURIComponent(rid) : '');
  const link = href
    ? h('a', { class: 'ap__card-link', href, title: 'Открыть карточку: ' + title }, title)
    : h('span', { class: 'ap__card-name' }, title);
  return h('div', { class: 'ap__card small' },
    h('span', { class: 'ap__card-cap muted' }, 'Карточка: '), link);
}

/** Плашка жёсткого запрета Hermes: разрешить нельзя никаким разрешением. */
function floorPlate(text) {
  return h('div', { class: 'ap__floor', role: 'note' },
    icon('shield', { size: 13 }),
    h('span', null, text || APPROVALS_RU.floor));
}

/** Можно ли вообще принимать решение по этому запросу. */
export function canDecide(a) {
  return Boolean(a) && !a.floor && a.grantable !== false;
}

/**
 * Подпись под заголовком: причина и — для строки-группы — сколько команд она покрывает.
 *
 * Разрешение «на вид» выдаётся на КЛАСС команд (`command_allowlist`), поэтому человеку
 * важно видеть, что он решает судьбу нескольких команд сразу, а не одной.
 */
function subtitleText(a, group) {
  const base = approvalReason(a);
  if (group.grouped) {
    return base + ' · команд: ' + String(group.commandsCount)
      + ' (разрешение на этот вид — все сразу)';
  }
  return base + (group.count > 1 ? ' · блокировалось ' + String(group.count) + ' раза' : '');
}

/**
 * «Ещё N команд» — раскрываемый список остальных команд группы (W22-ST).
 *
 * Показываем полностью, в моноширинном блоке с прокруткой: это ровно то, что человек
 * разрешает. Свёрнуто по умолчанию — строка не должна вырастать на пол-экрана.
 */
function otherCommands(group, compact) {
  const rest = group && Array.isArray(group.rest) ? group.rest : [];
  if (!rest.length) return null;
  const list = h('div', { class: 'ap__more-list' });
  for (const item of rest) {
    const clock = clockLabel(item.at);
    list.appendChild(h('div', { class: 'ap__more-item' },
      h('div', { class: 'ap__more-cap small muted' },
        (clock ? 'в ' + clock : 'раньше')
        + (item.count > 1 ? ' · повторов: ' + String(item.count) : '')),
      h('pre', { class: 'ap__code mono ap__code--small', tabindex: '0' }, item.command)));
  }
  return h('details', { class: 'ap__more', 'data-ap-more': String(rest.length) },
    h('summary', { class: 'ap__more-sum small' },
      icon('chevron-right', { size: 11 }),
      'Ещё ' + countNoun(rest.length, ['команда', 'команды', 'команд'])
      + ' с той же причиной' + (compact ? '' : ' — раскрыть')),
    list);
}

/**
 * Карточка запроса разрешения.
 *
 * @param {object} approval Approval из API
 * @param {object} opts
 * @param {(decision:string)=>Promise<object>} [opts.onDecide] отправить решение
 * @param {boolean} [opts.compact] узкий вид (правая колонка)
 * @param {string} [opts.roomId]
 * @param {string} [opts.requestHref] ссылка на карточку (страница запроса)
 * @param {(text:string, tone:string)=>void} [opts.toast]
 * @returns {HTMLElement}
 */
export function approvalCard(approval, opts = {}) {
  const a = Object.assign({}, approval || {});
  const compact = Boolean(opts.compact);
  const onDecide = typeof opts.onDecide === 'function' ? opts.onDecide : null;
  let state = approvalState(a);
  let busy = false;

  const node = h('article', {
    class: 'ap ap--' + state + (compact ? ' ap--compact' : '') + (a.floor ? ' ap--floor' : ''),
    'data-approval': String(a.id || ''),
    'data-ap-state': state,
    'aria-label': approvalHeadline(a),
  });

  const group = approvalGroup(a);
  const head = h('div', { class: 'ap__head' },
    h('span', { class: 'ap__icon', 'aria-hidden': 'true' }, icon(String(a.kind) === 'execute_code' ? 'code' : 'terminal', { size: 14 })),
    h('div', { class: 'ap__who' },
      h('div', { class: 'ap__title' }, approvalHeadline(a)),
      h('div', { class: 'ap__sub small muted' }, subtitleText(a, group))),
    group.count > 1
      ? h('span', { class: 'chip chip--muted ap__count tnum',
        title: group.grouped
          ? 'Одна причина: команд ' + String(group.commandsCount) + ', блокировок ' + String(group.count)
          : 'Блокировка повторилась ' + String(group.count) + ' раза' },
      '×' + String(group.count))
      : null,
    approvalDeadEnd(a) !== DEAD_ENDS.grantable
      ? h('span', {
        class: 'chip chip--' + DEAD_END_RU[approvalDeadEnd(a)].tone + ' ap__kind',
        'data-dead-end': approvalDeadEnd(a),
        title: 'Вид блокировки: ' + DEAD_END_RU[approvalDeadEnd(a)].label + ' — '
          + DEAD_END_RU[approvalDeadEnd(a)].hint,
      }, DEAD_END_RU[approvalDeadEnd(a)].label)
      : null,
    stateChip(state, approvalDeadEnd(a)));
  node.appendChild(head);

  const body = h('div', { class: 'ap__body' });
  node.appendChild(body);

  // W22 API §W22.1: `note` — короткая подпись от сервера («разрешение на время
  // этой работы», «такую блокировку снять нельзя»); технику не дублирует.
  if (a.note && !a.floor && a.grantable !== false) {
    body.appendChild(h('div', { class: 'ap__note small muted' }, String(a.note)));
  }
  const cmd = commandBlock(group.last ? Object.assign({}, a, { command: group.last.command }) : a,
    compact);
  if (cmd) body.appendChild(cmd);
  const more = otherCommands(group, compact);
  if (more) body.appendChild(more);
  const card = cardLine(a, opts);
  if (card) body.appendChild(card);

  const actions = h('div', { class: 'ap__actions', role: 'group', 'aria-label': 'Решение по запросу разрешения' });
  const result = h('div', { class: 'ap__result', role: 'status', 'aria-live': 'polite' });
  const errorBox = h('div', { class: 'ap__error', role: 'alert' });
  body.appendChild(actions);
  body.appendChild(result);
  body.appendChild(errorBox);

  /** Заблокировать/разблокировать кнопки: двойное нажатие не даёт дублей. */
  function setBusy(value) {
    busy = value;
    node.setAttribute('aria-busy', value ? 'true' : 'false');
    for (const btn of Array.from(actions.childNodes || [])) {
      if (btn && btn.nodeType === 1) btn.disabled = value;
    }
  }

  /** Подменить кнопки итогом: человек видит, что решение принято, сразу. */
  function showResult(text, tone) {
    result.replaceChildren(h('div', { class: 'ap__result-line ap__result-line--' + (tone || 'ok') },
      icon(tone === 'danger' ? 'circle-slash' : tone === 'muted' ? 'history' : 'check', { size: 12 }),
      h('span', null, text)));
    actions.replaceChildren();
    const chip = node.querySelector ? node.querySelector('.ap__state') : null;
    if (chip) {
      const meta = stateMeta(state);
      chip.setAttribute('class', 'chip chip--' + meta.tone + ' ap__state');
      chip.setAttribute('data-ap-state', state);
      chip.setAttribute('title', 'Состояние: ' + meta.label);
      chip.textContent = meta.label;
    }
    node.setAttribute('data-ap-state', state);
    node.setAttribute('class', 'ap ap--' + state + (compact ? ' ap--compact' : '')
      + (a.floor ? ' ap--floor' : ''));
    const extra = stateHint(state);
    if (extra) body.appendChild(extra);
  }

  /**
   * Итог «пункт уже решён» (W27-E2 §1): не ошибка человека, а устаревший экран.
   * Никаких красных плашек: чип меняется на «состояние уточняется», а стор
   * перечитывает сервер — строка уходит в «Закрытые» сама.
   */
  function settleDecided(err) {
    errorBox.replaceChildren();
    state = 'unknown';
    showResult('Уже решено — обновляю состояние', 'muted');
    node.setAttribute('data-ap-conflict', 'already_decided');
    if (typeof opts.onConflict === 'function') {
      try { opts.onConflict(err); } catch { /* сверка — не условие работы */ }
    }
  }


  /** Одно нажатие = один запрос. Пока идёт — кнопки выключены. */
  async function run(decision) {
    if (busy || state !== 'pending' || !canDecide(a)) return;
    if (!onDecide) {
      showResult('Решение отправлено', 'ok');
      return;
    }
    setBusy(true);
    errorBox.replaceChildren();
    try {
      const res = await onDecide(decision);
      state = decision === 'deny' ? 'denied'
        : decision === 'always' || (res && res.grant && res.grant.scope === 'always')
          ? 'granted_always' : 'granted_once';
      if (res && res.approval && res.approval.state && APPROVAL_STATES[res.approval.state]) {
        state = String(res.approval.state);
      }
      showResult(decisionResultText(decision, res, a), decision === 'deny' ? 'danger' : 'ok');
    } catch (err) {
      setBusy(false);
      // W27-E2 §1: 409 already_decided — тихая перерисовка, НЕ красная плашка.
      if (isAlreadyDecided(err)) {
        settleDecided(err);
        return;
      }
      const text = approvalErrorText(err);
      errorBox.replaceChildren(h('div', { class: 'ap__error-line' }, icon('warning', { size: 12 }),
        h('span', null, text)));
      if (isGone(err)) state = 'unknown';
      if (typeof opts.toast === 'function') opts.toast(text, 'danger');
    }
  }

  const deadEnd = approvalDeadEnd(a);
  const canAct = typeof opts.onAction === 'function' || onDecide !== null;

  // Плашка причины: у «нельзя штатно» и жёсткого запрета — своя, над кнопками.
  if (state === 'pending' && deadEnd !== DEAD_ENDS.grantable) {
    body.insertBefore(floorPlate(deadEnd === DEAD_ENDS.floor ? '' : APPROVALS_RU.ungrantable), actions);
  }

  /** Итог действия «нельзя штатно» человеческими словами. */
  function actionResultText(action, extra, res) {
    if (action === 'hint_agent') {
      return deadEnd === DEAD_ENDS.floor
        ? 'Участнику сообщено: запрет жёсткий, ищи другой способ'
        : 'Подсказка отправлена участнику — он сделает то же через файл и терминал';
    }
    if (action === 'trust_profile') {
      const ttl = String((extra && extra.ttl) || 'card');
      const meta = TRUST_TTL.find((t) => t.ttl === ttl) || TRUST_TTL[0];
      return 'Доверено запуску кода (' + meta.label + '): такие команды этого профиля больше '
        + 'не спрашиваются. Отозвать — в Настройках → Разрешения агентов';
    }
    if (action === 'dismiss') return 'Пункт закрыт как неактуальный — в счётчиках его больше нет';
    if (action === 'ack') return 'Скрыто: это была информация, а не вопрос к вам';
    return 'Готово';
  }

  /** Действие по блокировке, которую нельзя снять разрешением (W26 §D1). */
  async function runOther(action, extra) {
    if (busy) return;
    // «Вернуть» доступно и у закрытого пункта — состояние не `pending`.
    if (action !== 'restore' && state !== 'pending') return;
    setBusy(true);
    errorBox.replaceChildren();
    try {
      const res = typeof opts.onAction === 'function' ? await opts.onAction(a, action, extra) : { ok: true };
      state = { hint_agent: 'hinted', dismiss: 'dismissed', ack: 'acked',
        trust_profile: 'granted_always', restore: 'pending' }[action] || 'pending';
      if (res && res.approval && res.approval.state && APPROVAL_STATES[res.approval.state]) {
        state = String(res.approval.state);
      }
      showResult(actionResultText(action, extra, res),
        action === 'dismiss' || action === 'ack' ? 'muted' : 'ok');
      // «Вернуть» возвращает кнопки решений — пункт снова ждёт человека.
      if (action === 'restore') paintActions();
    } catch (err) {
      setBusy(false);
      if (isAlreadyDecided(err)) {
        settleDecided(err);
        return;
      }
      const text = approvalErrorText(err);
      errorBox.replaceChildren(h('div', { class: 'ap__error-line' }, icon('warning', { size: 12 }),
        h('span', null, text)));
      if (typeof opts.toast === 'function') opts.toast(text, 'danger');
    }
  }

  /**
   * «Доверять…» — сначала срок: без срока доверие не выдаётся (W26 §D1).
   * Человек видит, что снимается защита не только с execute_code.
   */
  function askTrust() {
    const box = h('div', { class: 'ap__ttl', role: 'group', 'aria-label': 'На какой срок доверять' },
      h('div', { class: 'ap__ttl-lead small muted' },
        'На какой срок? Доверие снимает защиту со всех опасных команд этого профиля в конечных '
        + 'запусках; жёсткие запреты Hermes остаются.'));
    const row = h('div', { class: 'ap__ttl-row' });
    for (const item of TRUST_TTL) {
      const btn = h('button', {
        type: 'button', class: 'btn btn--sm ' + (item.ttl === 'card' ? 'btn--primary' : 'btn--secondary')
          + ' ap__act', 'data-action': 'approval-trust-' + item.ttl, 'data-ttl': item.ttl,
        title: item.hint,
      }, icon(item.ttl === 'forever' ? 'check-all' : 'check', { size: 12 }), item.label);
      btn.addEventListener('click', (ev) => {
        if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
        runOther('trust_profile', { ttl: item.ttl });
      });
      row.appendChild(btn);
    }
    const back = h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm ap__act', 'data-action': 'approval-trust-cancel',
      title: 'Оставить как есть — участник продолжит искать другой способ',
    }, 'Отмена');
    back.addEventListener('click', (ev) => {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      paintActions();
    });
    row.appendChild(back);
    box.appendChild(row);
    actions.replaceChildren(box);
  }

  /** Кнопки пункта: по одному набору на каждый вид блокировки. */
  function paintActions() {
    actions.replaceChildren();
    // W27-E2 §1: у закрытого/снятого пункта тоже есть действие — «Вернуть».
    if (state !== 'pending') {
      const stale = approvalStaleActions(Object.assign({}, a, { state }));
      if (!stale.length) return;
      for (const item of stale) {
        const btn = h('button', {
          type: 'button',
          class: 'btn btn--sm ' + item.tone + ' ap__act ap__act--restore',
          'data-action': 'approval-' + item.action,
          'data-ap-state': state,
          title: item.hint,
          'aria-label': item.label + ': ' + approvalHeadline(a),
        }, icon('history', { size: 12 }), item.label);
        btn.addEventListener('click', (ev) => {
          if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
          runOther(item.action, {});
        });
        actions.appendChild(btn);
      }
      return;
    }
    if (!canAct) {
      actions.appendChild(h('div', { class: 'ap__hint small muted' },
        'Действия доступны на странице запроса.'));
      return;
    }
    for (const item of approvalActionsFor(a)) {
      const btn = h('button', {
        type: 'button',
        class: 'btn btn--sm ' + item.tone + ' ap__act',
        'data-action': 'approval-' + item.action,
        'data-decision': item.decision || null,
        'data-dead-end': deadEnd,
        title: item.hint,
        'aria-label': item.label + ': ' + approvalHeadline(a),
      }, icon(item.action === 'deny' ? 'circle-slash'
        : item.action === 'always' || item.action === 'trust_profile' ? 'check-all'
          : item.action === 'dismiss' ? 'circle-x' : 'check', { size: 12 }),
      item.label);
      btn.addEventListener('click', (ev) => {
        if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
        if (item.action === 'trust_profile') askTrust();
        else if (item.decision) run(item.decision);
        else runOther(item.action, {});
      });
      actions.appendChild(btn);
    }
  }

  paintActions();

  if (state !== 'pending') {
    result.appendChild(h('div', { class: 'ap__result-line ap__result-line--' + (state === 'denied' ? 'danger' : 'ok') },
      icon(state === 'denied' ? 'circle-slash' : 'check', { size: 12 }),
      h('span', null, stateLine(a, state))));
    const hint = stateHint(state);
    if (hint) body.appendChild(hint);
  }

  // Подсказка «Разрешать всегда» — только у того, что действительно можно разрешить.
  if (state === 'pending' && deadEnd === DEAD_ENDS.grantable) {
    body.appendChild(h('div', { class: 'ap__always-hint small muted' },
      icon('info', { size: 11 }),
      h('span', null, '«' + APPROVALS_RU.always + '» — ' + APPROVALS_RU.alwaysHint + '.')));
  }

  return node;
}

/** Итог уже решённого запроса одной строкой. */
export function stateLine(a, state) {
  const st = state || approvalState(a);
  if (st === 'granted_once') {
    const until = clockLabel(a && a.expires_at);
    return 'Разрешено один раз' + (until ? ' — до ' + until : ' — до завершения карточки');
  }
  if (st === 'granted_always') return 'Разрешено всегда';
  if (st === 'hinted') return 'Подсказано участнику — он сделает то же через файл и терминал';
  if (st === 'dismissed') return 'Закрыто как неактуальное';
  if (st === 'acked') return 'Скрыто';
  if (st === 'denied') return 'Запрещено — участник будет искать другой способ';
  if (st === 'expired') return 'Срок разрешения истёк — участник снова спросит';
  // W27-E2 §1: закрытое сервером — с причиной словами, а не «ждёт вашего решения».
  if (st === 'closed') {
    const why = closedReasonText(a);
    return 'Закрыто автоматически' + (why ? ' — ' + why : ' — работа завершена');
  }
  if (st === 'unknown') return 'Состояние уточняется — обновите страницу';
  return 'Ждёт вашего решения';
}

/* ─────────────────────────────────────────── раздел в настройках (отзыв) */

/**
 * Строка выданного разрешения: кто, что, до каких пор и кнопка «Отозвать».
 * @param {object} g {profile, member_title, pattern_key, description, scope, granted_at, expires_at, source}
 * @param {{onRevoke?:Function, busy?:boolean}} opts
 */
export function grantRow(g, opts = {}) {
  const scope = String((g && g.scope) || 'always');
  const expires = clockLabel(g && g.expires_at);
  const when = Number(g && g.granted_at) || 0;
  const meta = [];
  meta.push(grantSourceLabel(g && g.source));
  if (when) meta.push('выдано ' + timeAgo(when));
  const btn = h('button', {
    type: 'button', class: 'btn btn--ghost btn--sm ap-grant__revoke',
    'data-action': 'approval-revoke',
    'data-pattern': String((g && g.pattern_key) || ''),
    title: 'Убрать разрешение: участник снова будет спрашивать перед такой командой',
    'aria-label': 'Отозвать разрешение: ' + String((g && (g.description || g.pattern_key)) || ''),
    disabled: opts.busy ? true : null,
  }, icon('circle-slash', { size: 12 }), 'Отозвать');
  if (typeof opts.onRevoke === 'function') {
    btn.addEventListener('click', () => opts.onRevoke(g, btn));
  }
  return h('div', { class: 'ap-grant', 'data-pattern': String((g && g.pattern_key) || '') },
    h('div', { class: 'ap-grant__main' },
      h('div', { class: 'ap-grant__top' },
        h('span', { class: 'ap-grant__who' }, String((g && (g.member_title || g.profile)) || 'Участник')),
        h('span', { class: 'chip chip--' + (scope === 'always' ? 'ok' : 'info') + ' ap-grant__scope' },
          grantScopeLabel(scope)),
        expires && scope === 'once' ? h('span', { class: 'muted small' }, 'до ' + expires) : null),
      h('div', { class: 'ap-grant__what' }, String((g && (g.description || g.pattern_key)) || 'Команда')),
      meta.length ? h('div', { class: 'ap-grant__meta small muted' }, meta.join(' · ')) : null),
    btn);
}

/** Пустой список — не «дырка», а объяснение, чего здесь не бывает. */
export function grantsEmpty() {
  return h('div', { class: 'ap-grants__empty' },
    icon('shield', { size: 14 }),
    h('div', null,
      h('div', { class: 'ap-grants__empty-title' }, 'Пока нет ни одного разрешения'),
      h('div', { class: 'small muted' }, 'Участники спрашивают перед каждой опасной командой — '
        + 'так безопаснее. Выданное здесь разрешение появится в этом списке.')));
}

/**
 * Раздел «Разрешения агентов» в настройках: список выданных разрешений и отзыв.
 * Читает `GET /api/approvals/grants?room=`, отзывает `POST /api/approvals/revoke`.
 *
 * @param {object} opts {api, roomId, toast, confirm?}
 * @returns {{element:HTMLElement, load:Function, destroy:Function}}
 */
export function createGrantsSection(opts = {}) {
  ensureCss(APPROVALS_CSS);
  const api = opts.api;
  const roomId = String(opts.roomId || '');
  const toast = typeof opts.toast === 'function' ? opts.toast : () => {};
  const confirm = typeof opts.confirm === 'function' ? opts.confirm : confirmDialog;
  const state = { items: [], loading: true, error: null, busy: false, seq: 0 };

  const list = h('div', { class: 'ap-grants__list' });
  const status = h('div', { class: 'ap-grants__status small muted', role: 'status', 'aria-live': 'polite' });
  const element = h('div', { class: 'ap-grants', 'data-approvals-settings': '' },
    status, list);

  function paint() {
    status.textContent = state.loading ? 'Загружаю список…'
      : state.error ? 'Не удалось загрузить список разрешений'
        : state.items.length ? grantsSummary(state.items).text
          : '';
    list.replaceChildren();
    if (state.loading) return;
    if (state.error) {
      list.appendChild(h('div', { class: 'ap-grants__error' },
        icon('warning', { size: 13 }),
        h('span', null, 'Список разрешений сейчас недоступен — попробуйте обновить страницу.'),
        h('button', {
          type: 'button', class: 'btn btn--secondary btn--sm', onclick: () => load(),
        }, 'Ещё раз')));
      return;
    }
    if (!state.items.length) {
      list.appendChild(grantsEmpty());
      return;
    }
    for (const g of state.items) {
      list.appendChild(grantRow(g, {
        busy: state.busy,
        onRevoke: (item, btn) => revoke(item, btn),
      }));
    }
  }

  async function load() {
    if (!api) return;
    const seq = ++state.seq;
    state.loading = !state.items.length;
    state.error = null;
    paint();
    try {
      const url = '/api/approvals/grants' + (roomId ? '?room=' + encodeURIComponent(roomId) : '');
      const res = await api.get(url);
      if (seq !== state.seq) return;
      const items = res && Array.isArray(res.items) ? res.items : Array.isArray(res) ? res : [];
      state.items = items.filter(Boolean);
      state.error = null;
    } catch (err) {
      if (seq !== state.seq) return;
      state.items = [];
      state.error = err || new Error('grants');
    } finally {
      if (seq === state.seq) {
        state.loading = false;
        paint();
      }
    }
  }

  async function revoke(g, btn) {
    if (!api || state.busy) return;
    const what = String((g && (g.description || g.pattern_key)) || 'эту команду');
    const who = String((g && (g.member_title || g.profile)) || 'участник');
    const ok = await confirm({
      title: 'Отозвать разрешение?',
      text: '«' + what + '» для участника «' + who + '» снова будет спрашиваться. '
        + 'Уже запущенные работы это не остановит.',
      confirmLabel: 'Отозвать',
      cancelLabel: 'Оставить',
      danger: true,
    });
    if (!ok) return;
    state.busy = true;
    if (btn) btn.disabled = true;
    paint();
    try {
      await api.post('/api/approvals/revoke', {
        profile: String(g.profile || ''),
        pattern_key: String(g.pattern_key || ''),
        room: roomId || undefined,
      });
      toast('Разрешение отозвано — участник снова будет спрашивать', 'ok');
      state.items = state.items.filter((item) => !(String(item.profile) === String(g.profile)
        && String(item.pattern_key) === String(g.pattern_key)));
    } catch (err) {
      const text = String((err && err.message) || '').trim();
      toast(text && text.length < 160 ? text : 'Не удалось отозвать разрешение', 'danger');
    } finally {
      state.busy = false;
      paint();
      load();
    }
  }

  paint();
  load();
  return {
    element,
    load,
    destroy() { state.seq += 1; element.replaceChildren(); },
  };
}

/* ──────────────────────────────────────── связка со страницей запроса (R) */

/**
 * Фабрика для страницы запроса и правой колонки. `R` — общий контекст
 * request.js: {ctx, rid, roomId, detail, refresh, keepValues}.
 */
export function createApprovals(R = {}) {
  ensureCss(APPROVALS_CSS);
  const ctx = R.ctx || {};
  const roomId = String(R.roomId || (ctx.room && ctx.room.id) || '');
  const rid = String(R.rid || (R.detail && R.detail.id) || '');
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const api = ctx.api || null;
  const refresh = typeof R.refresh === 'function' ? R.refresh : null;
  // W26-D2: ОДИН стор на запрос — сколько бы мест страницы его ни показывало.
  const store = R.store || requestStoreFor(rid, {
    api, roomId,
    detail: R.detail || null,
    refresh,
  });

  /** Все разрешения запроса: из стора (общий источник), иначе из детали. */
  function all() {
    const shared = store ? store.get().approvals : null;
    if (Array.isArray(shared) && shared.length) return shared;
    return Array.isArray(R.detail && R.detail.approvals) ? R.detail.approvals : [];
  }

  /** Тот же тост, что у страницы: массовое закрытие говорит о результате. */
  const optsToast = toast;

  /**
   * «Закрыть все неактуальные» (W27-E2 §1): неактуальное у завершённых запросов
   * комнаты. Через стор, если он умеет, иначе — прямым запросом.
   * @returns {Promise<{closed:{questions:number,decisions:number,approvals:number,steps:number}}>}
   */
  function dismissStale() {
    if (store && typeof store.dismissStale === 'function') {
      return store.dismissStale().then((res) => {
        afterServer();
        return res;
      });
    }
    if (!api || typeof api.post !== 'function') throw new Error('Нет связи с приложением');
    const room = roomId || String((ctx.room && ctx.room.id) || '');
    return api.post('/api/rooms/' + encodeURIComponent(room) + '/dismiss_stale',
      { scope: 'finished_requests' }).then((res) => {
      if (refresh) refresh({ force: true, quiet: true }).catch(() => {});
      return res;
    });
  }

  /** Догнать состояние страницы после ответа сервера (тихо, без экрана загрузки). */
  function afterServer() {
    if (!store || typeof store.sync !== 'function') return;
    store.sync().catch(() => { /* ответ уже на экране — сверка догонит опросом */ });
  }

  /** Отправить решение по разрешению. Возвращает ответ сервера. */
  function decide(a, decision) {
    if (store) {
      return store.decideApproval(a, decision).then((res) => {
        afterServer();
        return res;
      });
    }
    if (!api) throw new Error('Нет связи с приложением');
    return api.post('/api/requests/' + encodeURIComponent(rid) + '/actions',
      approvalBody(a, decision)).then((res) => {
      if (refresh) refresh({ force: true, quiet: true }).catch(() => {});
      return res;
    });
  }

  /**
   * Действие по блокировке, которую нельзя снять разрешением
   * (`hint_agent` / `trust_profile` / `dismiss` / `ack` / `restore`) — W26 §D1, §1 W27.
   */
  function act(a, action, extra) {
    const id = String((a && a.id) || '');
    // W27-E2 §1: «Не актуально» и «Вернуть» — общий контракт `target:{kind,id}`,
    // один и тот же для вопроса, решения и разрешения.
    if (action === 'restore' || action === 'dismiss') {
      const body = action === 'restore' ? restoreBody('approval', id) : dismissBody('approval', id);
      if (store && typeof store.targetAction === 'function') {
        return store.targetAction('approvals', id, action).then((res) => {
          afterServer();
          return res;
        });
      }
      if (!api) throw new Error('Нет связи с приложением');
      return api.post('/api/requests/' + encodeURIComponent(rid) + '/actions', body)
        .then((res) => {
          if (refresh) refresh({ force: true, quiet: true }).catch(() => {});
          return res;
        });
    }
    if (store) {
      return store.approvalAction(a, action, extra).then((res) => {
        if (action !== 'ack') afterServer();
        return res;
      });
    }
    if (!api) throw new Error('Нет связи с приложением');
    if (action === 'ack') return Promise.resolve({ ok: true, local: true });
    return api.post('/api/requests/' + encodeURIComponent(rid) + '/actions',
      Object.assign({ action: String(action), approval_id: String((a && a.id) || '') }, extra || {}))
      .then((res) => {
        if (refresh) refresh({ force: true, quiet: true }).catch(() => {});
        return res;
      });
  }

  function cardFor(a, compact) {
    return approvalCard(a, {
      compact,
      store,
      roomId,
      requestHref: a.request_id && String(a.request_id) !== rid
        ? '#/room/' + encodeURIComponent(roomId) + '/request/' + encodeURIComponent(String(a.request_id))
        : '',
      toast,
      onDecide: (decision) => decide(a, decision),
      onAction: (item, action, extra) => act(item || a, action, extra),
      // W27-E2 §1: 409 — не ошибка, а «перерисуйся»: тихая сверка со сервером.
      onConflict: () => afterServer(),
    });
  }

  /** Ждущие разрешения текущего запроса (только те, у кого есть действие). */
  function pending() {
    return waitingApprovals(all());
  }

  /** Карточки для «Нужен ваш ответ» (правая колонка, узкий вид). */
  function pendingCards(list) {
    const items = Array.isArray(list) ? waitingApprovals(list) : pending();
    return items.map((a) => cardFor(a, true));
  }

  /**
   * Содержимое блока: «Нужен ваш ответ» — только пункты с действием; жёсткие
   * запреты (`floor`) — отдельной информационной строкой, без счётчика.
   */
  function paintBlock(block) {
    block.replaceChildren();
    const list = all();
    const detail = (store && store.get().detail) || R.detail || null;
    // W26-D2: «скрыто» (ack) — строка только для этого экрана, в список не идёт.
    const visible = list.filter((a) => approvalState(a) !== 'acked');
    const waiting = waitingApprovals(visible);
    const floor = floorApprovals(visible);
    // W27-E2 §1: закрытые сервером — отдельно от решённых человеком: у них причина.
    const closed = closedApprovals(visible);
    const done = visible.filter((a) => !isPending(a) && !isClosed(a));
    // W27-E2 §1: число в шапке блока — с сервера (`needs_you_count.approvals`),
    // а не пересчитанное по списку. Список и число не должны спорить: именно
    // расхождение дало «27» из 20 закрытых разрешений.
    const server = needsYouCount(detail);
    const count = server ? server.approvals : waiting.length;
    const stale = staleSummary(detail || {}, { isWaiting });
    block.setAttribute('class', 'card ap-block' + (count ? ' ap-block--active' : '')
      + (floor.length && !count ? ' ap-block--info' : ''));
    block.appendChild(h('div', { class: 'ap-block__head' },
      h('div', { class: 'ap-block__title-row' },
        h('span', { class: 'ap-block__title' }, icon('shield', { size: 15 }), APPROVALS_RU.requestTitle),
        count
          ? h('span', {
            class: 'chip chip--warn ap-block__count tnum',
            'data-needs-you': 'approvals',
            title: approvalsWaitingLabel(count) + (server ? ' (по счёту сервера)' : ''),
          }, String(count))
          : h('span', { class: 'chip chip--muted ap-block__count', 'data-needs-you': 'approvals' },
            'все решены')),
      h('p', { class: 'ap-block__lead small muted' },
        count
          ? 'Участник упёрся в запрет Hermes: команда помечена как опасная, а запуск без человека '
            + 'не может спросить сам. У каждого пункта есть действие — после него работа продолжится.'
          : 'Запросы разрешений по этой карточке уже решены. Отозвать постоянные разрешения можно '
            + 'в Настройках → Разрешения агентов.')));

    // W27-E2 §1: массовое «Закрыть все неактуальные (N)» — только у завершённого
    // запроса: сервер закрывает неактуальное и не трогает решённое человеком.
    if (stale.finished && stale.total > 0) {
      block.appendChild(staleRow(stale.total));
    }

    if (waiting.length) {
      const box = h('div', { class: 'ap-block__list ap-block__list--live', 'data-approvals-live': '' });
      for (const a of waiting) box.appendChild(cardFor(a, false));
      block.appendChild(box);
    }

    if (floor.length) {
      // W26 §1: жёсткий запрет — информация, а не «ждёт вашего решения».
      const box = h('div', { class: 'ap-block__list ap-block__list--info', 'data-approvals-info': '' });
      box.appendChild(h('div', { class: 'ap-block__subhead small muted' },
        'Не вопрос к вам: Hermes запрещает настрого',
        h('span', { class: 'tnum' }, ' · ' + String(floor.length))));
      for (const a of floor) box.appendChild(cardFor(a, false));
      block.appendChild(box);
    }

    // «Закрытые» — свёрнутый список с причиной (дефект 1: 20 закрытых разрешений
    // висели как «ждёт вашего решения»; теперь они здесь и молчат).
    if (closed.length) {
      const box = h('div', { class: 'ap-block__list ap-block__list--closed', 'data-approvals-closed': '' });
      for (const a of closed) box.appendChild(cardFor(a, false));
      const why = closedSummary(closed).text;
      block.appendChild(h('details', { class: 'ap-block__done ap-block__closed', 'data-ap-closed': String(closed.length) },
        h('summary', { class: 'ap-block__done-sum small muted' },
          icon('chevron-right', { size: 11 }),
          'Закрытые: ' + countNoun(closed.length, ['запрос', 'запроса', 'запросов']),
          why ? h('span', { class: 'ap-block__closed-why' }, ' · ' + why) : null),
        h('p', { class: 'ap-block__closed-hint small muted' },
          'Закрыто сервером: работа карточки закончена. У каждого пункта есть «Вернуть», '
          + 'если он снова стал нужен.'),
        box));
      return;
    }

    if (done.length) {
      const box = h('div', { class: 'ap-block__list ap-block__list--done' });
      for (const a of done) box.appendChild(cardFor(a, false));
      if (waiting.length || floor.length) {
        block.appendChild(h('details', { class: 'ap-block__done' },
          h('summary', { class: 'ap-block__done-sum small muted' },
            icon('chevron-right', { size: 11 }),
            'Решённые: ' + countNoun(done.length, ['запрос', 'запроса', 'запросов'])),
          box));
      } else {
        block.appendChild(box);
      }
    }
  }

  /**
   * Строка «Закрыть все неактуальные (N)» (W27-E2 §1).
   * Закрывает неактуальные пункты завершённых запросов комнаты и не трогает
   * решённое человеком. Ответ `{closed:{questions,decisions,approvals,steps}}`.
   */
  function staleRow(total) {
    const text_ = h('div', { class: 'ap-block__stale-text small muted' },
      icon('history', { size: 12 }),
      h('span', null, 'Работа по запросу закончена — неактуальные пункты можно закрыть разом: '
        + 'счётчик «ждёт вас» обнулится, решённое человеком останется.'));
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm ap-block__stale-btn',
      'data-action': 'dismiss-stale',
      title: 'Закрыть неактуальное у всех завершённых запросов комнаты (с возвратом)',
    }, icon('circle-x', { size: 12 }), 'Закрыть все неактуальные (' + String(total) + ')');
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      try {
        const res = await dismissStale();
        const closedN = res && res.closed
          ? [res.closed.questions, res.closed.decisions, res.closed.approvals, res.closed.steps]
            .reduce((sum, n) => sum + (Number(n) || 0), 0)
          : 0;
        if (typeof optsToast === 'function') {
          optsToast(closedN
            ? 'Закрыто неактуальных: ' + String(closedN) + ' — вернуть можно в «Закрытые»'
            : 'Неактуальных пунктов не осталось', 'ok');
        }
        afterServer();
      } catch (err) {
        btn.disabled = false;
        if (isAlreadyDecided(err)) return;
        if (typeof optsToast === 'function') {
          optsToast(String((err && err.message) || 'Не удалось закрыть неактуальные'), 'danger');
        }
      }
    });
    return h('div', {
      class: 'ap-block__stale', role: 'group', 'aria-label': 'Неактуальные пункты',
    }, text_, btn);
  }

  /**
   * Блок «Разрешения агентам» на странице запроса. Живой: любое действие в
   * любом месте страницы перерисовывает его из общего стора (W26 §10).
   * @returns {HTMLElement|null}
   */
  function buildRequestBlock() {
    if (!all().length && !(R.detail && R.detail.approvals)) return null;
    const block = h('section', {
      class: 'card ap-block', id: 'approvals', 'data-approvals': 'request',
      'aria-label': 'Разрешения агентам',
    });
    if (store && typeof store.subscribe === 'function') {
      const off = bindLive(store, block, () => paintBlock(block));
      block.__apUnsub = off;
    } else {
      paintBlock(block);
    }
    return block;
  }

  return { buildRequestBlock, pendingCards, pending, decide, act, store };
}

export default { createApprovals, approvalCard, createGrantsSection };
