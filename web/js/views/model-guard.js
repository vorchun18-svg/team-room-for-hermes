/**
 * model-guard.js — «Контроль моделей» (волна 23, пакет MG2).
 *
 * Запрос должен уходить ТОЛЬКО на ту модель, которая выбрана для участника.
 * Сверку и исправление делает сервер (`server/model_guard.py`, MG1); здесь —
 * то, что видит и нажимает человек:
 *
 *   * карточка участника на вкладке «Команда» — строка
 *     «Модель: <ожидаемая> · последний запуск: <фактическая> ✓/⚠»
 *     и кнопки управления работой: «Остановить», «Продолжить»,
 *     «Сменить модель…» (`guardLine`, `cardControls`);
 *   * страница запроса — лента событий контроля
 *     «Остановлено: запуск шёл на … — исправлено и перезапущено, сэкономлено ≈ $2,45»
 *     (`buildRequestFeed`), а у карточки в дереве работ те же кнопки;
 *   * настройка комнаты «Контроль моделей: строго / только предупреждать»
 *     (`guardModeField`);
 *   * пункт проверки системы (`diagnostics.js`).
 *
 * Что важно для человека (правила волны): показываем факт и стоимость спокойно,
 * без «дорогой модели» как упрёка; техника (номера карточек, имена полей,
 * провайдер в chip) наружу не идёт — только в подсказке.
 *
 * Модуль проверяется без браузера: `node --test tests/model-guard-ui.test.mjs`.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, confirmDialog } from '../ui.js';
import { MODEL_GUARD_RU } from '../text-ru.js';
import { modelLabelOf, providersOf } from '../agent-model-model.js';
import { participantLabel } from '../participant-label.js';

export const MODEL_GUARD_CSS = 'css/model-guard.css';
ensureCss(MODEL_GUARD_CSS);

/**
 * Последний увиденный каталог моделей: на странице запроса его нет, а подписи
 * моделей («Дилижанс домашний» вместо `dilizhans-home`) нужны везде одинаковые.
 */
let catalogCache = null;

/** Запомнить каталог моделей для подписей в других местах страницы. */
export function rememberCatalog(catalog) {
  if (catalog && typeof catalog === 'object' && Array.isArray(catalog.providers)) catalogCache = catalog;
  return catalogCache;
}

/** Режимы контроля: слова и объяснение — один источник для диалога и тестов. */
export const GUARD_MODES = ['enforce', 'warn'];

/**
 * Виды событий контроля — подпись и тон значка. Список ровно как в API §W23.5:
 * `fixed`, `stopped`, `unknown`, `warned`, а также действия человека
 * (`stopped_by_user`, `resumed`, `model_changed`).
 */
export const GUARD_EVENT_META = {
  fixed: { label: 'исправлено', tone: 'ok', icon: 'check' },
  stopped: { label: 'остановлено', tone: 'warn', icon: 'stop-circle' },
  unknown: { label: 'не проверить', tone: 'muted', icon: 'info' },
  warned: { label: 'предупреждение', tone: 'warn', icon: 'warning' },
  stopped_by_user: { label: 'остановлено вами', tone: 'muted', icon: 'stop-circle' },
  resumed: { label: 'продолжено', tone: 'ok', icon: 'play' },
  model_changed: { label: 'модель изменена', tone: 'info', icon: 'edit' },
};

/** Вид события в один из известных (неизвестный — нейтральный «unknown»). */
export function guardKind(kind) {
  const key = String(kind || '');
  return GUARD_EVENT_META[key] ? key : 'unknown';
}

/* ──────────────────────────────────────────────── чистые помощники (тесты) */

/** Настройки модели в один вид: `{provider, model}` из объекта или строки. */
export function settingsOf(value) {
  if (!value) return { provider: '', model: '' };
  if (typeof value === 'string') {
    const raw = value.trim();
    if (!raw) return { provider: '', model: '' };
    // «провайдер:модель» — но только если левая часть похожа на имя провайдера
    // (короткая, без цифр в конце): `deepseek-v4.1-flash:1017` — это модель.
    const cut = raw.indexOf(':');
    if (cut > 0 && cut < 24 && !/^\d/.test(raw.slice(cut + 1))) {
      return { provider: raw.slice(0, cut).trim().toLowerCase(), model: raw.slice(cut + 1).trim() };
    }
    return { provider: '', model: raw };
  }
  if (typeof value !== 'object') return { provider: '', model: '' };
  return {
    provider: String(value.provider || value.billing_provider || '').trim().toLowerCase(),
    model: String(value.model || value.name || value.id || '').trim(),
  };
}

/**
 * Имя модели в сравнимом виде: регистр и лишние пробелы не считаются,
 * `провайдер:модель` сравнивается по модели. Версии (`qwen36` ↔ `qwen36v`)
 * остаются РАЗНЫМИ моделями — ложных «совпадает» быть не должно.
 */
export function normalizeModel(name) {
  const raw = String(name === null || name === undefined ? '' : name).trim().toLowerCase();
  if (!raw) return '';
  const cut = raw.indexOf(':');
  if (cut > 0 && cut < 24 && !/^\d/.test(raw.slice(cut + 1))) return raw.slice(cut + 1).trim();
  return raw;
}

/**
 * Одна и та же ли модель. Провайдер сравниваем, только когда он известен с двух
 * сторон: у профиля он может быть не задан, и это не повод для тревоги.
 * @param {object|string} a
 * @param {object|string} b
 * @returns {boolean}
 */
export function sameModel(a, b) {
  const left = settingsOf(a);
  const right = settingsOf(b);
  const lm = normalizeModel(left.model);
  const rm = normalizeModel(right.model);
  if (!lm || !rm) return false;
  if (lm !== rm) return false;
  if (left.provider && right.provider && left.provider !== right.provider) return false;
  return true;
}

/** «≈ $2,45» — стоимость спокойно, без оценки. Нет числа — пустая строка. */
export function costText(usd) {
  const value = Number(usd);
  if (!Number.isFinite(value) || value <= 0) return '';
  const rounded = value >= 100 ? Math.round(value) : Math.round(value * 100) / 100;
  const text = rounded.toFixed(value >= 100 ? 0 : 2).replace('.', ',');
  return '≈ $' + text;
}

/** «10:41» — время события в часах и минутах (как в остальном интерфейсе). */
export function clockText(at) {
  const ts = Number(at);
  if (!Number.isFinite(ts) || ts <= 0) return '';
  const date = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return pad(date.getHours()) + ':' + pad(date.getMinutes());
}

/** Короткая подпись модели: имя из каталога, иначе как есть. */
export function modelText(settings, catalog) {
  const s = settingsOf(settings);
  if (!s.model) return '';
  if (catalog) {
    const label = modelLabelOf(catalog, s.provider, s.model);
    if (label && label !== s.model) return label;
  }
  return s.model;
}

/**
 * Состояние сверки участника: ok | warn | unknown | new | pending.
 *
 * `pending` — модель сменили ПОЗЖЕ последнего запуска (W19-B-29): прошлый запуск
 * честно шёл на другой модели, но это не тревога — следующая карточка пойдёт на
 * выбранной. Сервер говорит `ok: true`; по разным именам моделей мы это отличаем
 * от «совпадает» и пишем спокойно: «ждёт запуска».
 */
export function memberState(member) {
  const m = member && typeof member === 'object' ? member : {};
  const expected = settingsOf(m.expected);
  const last = settingsOf(m.last_run);
  if (!expected.model) return 'unknown';
  if (m.ok === true) {
    return last.model && !sameModel(expected, last) ? 'pending' : 'ok';
  }
  if (m.ok === false) return 'warn';
  if (!last.model) return 'new';
  return sameModel(expected, last) ? 'ok' : 'warn';
}

/** Пометка сверки — слово для человека (в тестах и на карточке одно и то же). */
export function stateMark(state) {
  // Значок ✓/⚠ рисует иконка рядом; в тексте — только слово (без «✓✓»).
  if (state === 'ok') return MODEL_GUARD_RU.okMark;
  if (state === 'warn') return MODEL_GUARD_RU.warnMark;
  if (state === 'pending') return MODEL_GUARD_RU.pendingMark;
  if (state === 'new') return MODEL_GUARD_RU.never;
  return MODEL_GUARD_RU.unknownMark;
}

/** Подсказка к строке модели: что именно произошло и что будет дальше. */
export function stateTitle(state) {
  if (state === 'ok') return MODEL_GUARD_RU.okTitle;
  if (state === 'pending') return 'Модель сменили позже последнего запуска: следующая карточка пойдёт уже на ней';
  if (state === 'warn') return MODEL_GUARD_RU.warnTitle;
  if (state === 'new') return 'Участник ещё не запускался: сверять пока нечего';
  return MODEL_GUARD_RU.unknownTitle;
}

/**
 * «Ожидалась qwen36v (Локальный сервер), запущено claude-sonnet-5-5 (Облако-А)» —
 * человеческая причина, если сервер не прислал свою.
 */
export function problemText(member, catalog) {
  const m = member && typeof member === 'object' ? member : {};
  const own = String(m.problem || '').trim();
  if (own) return own;
  const expected = settingsOf(m.expected);
  const last = settingsOf(m.last_run);
  const expectedName = modelText(expected, catalog) || MODEL_GUARD_RU.unknownExpected;
  const lastName = modelText(last, catalog);
  if (!lastName) return 'Запусков ещё не было: ожидается ' + expectedName;
  return 'Ожидалась ' + expectedName + ', запущено ' + lastName;
}

/** Значение настройки комнаты: неизвестное считаем «строго» (как сервер). */
export function guardModeOf(room) {
  const raw = String((room && room.model_guard) || '').trim().toLowerCase();
  return GUARD_MODES.includes(raw) ? raw : 'enforce';
}

/** Подпись режима: «строго» / «только предупреждать». */
export function guardModeLabel(mode) {
  return MODEL_GUARD_RU.modes[GUARD_MODES.includes(String(mode)) ? String(mode) : 'enforce'];
}

/** События контроля из деталей запроса — от новых к старым, без мусора. */
export function guardEventsOf(detail) {
  const list = detail && Array.isArray(detail.model_guard) ? detail.model_guard : [];
  return list
    .filter((ev) => ev && typeof ev === 'object')
    .map((ev) => ({
      at: Number(ev.at) || 0,
      kind: guardKind(ev.kind),
      card_id: String(ev.card_id || ''),
      profile: String(ev.profile || ''),
      expected: settingsOf(ev.expected),
      actual: settingsOf(ev.actual),
      cost_usd: Number(ev.cost_usd) || 0,
      text: String(ev.text || '').trim(),
    }))
    .sort((a, b) => b.at - a.at);
}

/**
 * Одно событие одной фразой — то, что человек читает в ленте.
 *
 * По контракту (§W23.5) `text` на сервере — ГОТОВЫЙ русский текст: показываем его
 * как есть и ничего не доклеиваем. Свой текст собираем только если сервер его не
 * прислал (старые данные, чужие фикстуры).
 */
export function guardEventText(ev, catalog) {
  const e = ev && typeof ev === 'object' ? ev : {};
  const expected = modelText(e.expected, catalog) || MODEL_GUARD_RU.unknownExpected;
  const actual = modelText(e.actual, catalog);
  const cost = costText(e.cost_usd);
  const own = String(e.text || '').trim();
  if (own) return own;
  let text = '';
  if (e.kind === 'stopped') {
    text = 'Остановлено: запуск шёл на ' + (actual || 'другую модель')
      + ' вместо ' + expected + ' — исправлено и перезапущено';
    if (cost) text += ', сэкономлено ' + cost;
  } else if (e.kind === 'fixed') {
    text = 'Исправлено до запуска: карточка собиралась на ' + (actual || 'другую модель')
      + ' вместо ' + expected + ' — модель приведена к ожидаемой';
  } else if (e.kind === 'warned') {
    text = 'Расхождение найдено: ' + (actual || 'другая модель') + ' вместо ' + expected
      + ' — комната в режиме «только предупреждать», запуск не останавливали';
  } else {
    text = 'Не удалось определить ожидаемую модель — контроль не вмешивался, только предупредил';
  }
  return text;
}

/** Итог для шапки ленты: сколько исправлено и остановлено, сколько сэкономлено. */
export function guardSummary(events) {
  const out = { fixed: 0, stopped: 0, unknown: 0, warned: 0, saved_usd: 0, total: 0 };
  for (const ev of Array.isArray(events) ? events : []) {
    if (!ev || typeof ev !== 'object') continue;
    const kind = guardKind(ev.kind);
    if (kind in out && kind !== 'saved_usd' && kind !== 'total') out[kind] += 1;
    out.total += 1;
    // «Сколько не сгорело» считаем по остановленным запускам — как сервер.
    if (kind === 'stopped') out.saved_usd += Number(ev.cost_usd) || 0;
  }
  return out;
}

/* ───────────────────────────────────────────────── управление карточкой */

const RUNNING = new Set(['running', 'working', 'review', 'reviewing', 'claimed']);
const STOPPED = new Set(['paused', 'stopped', 'suspended']);

/**
 * Признак «остановлено человеком» в разных формах, какие бывают у сервера:
 * `stopped: {by, at, model, cost_usd}` (демо и наши данные), отдельные поля
 * `stopped_by`/`stopped_at`, булев `stopped: true`, либо статус `paused`.
 * Обычная `blocked` (ждёт зависимости) остановкой НЕ считается.
 */
export function stoppedInfo(node) {
  const n = node && typeof node === 'object' ? node : {};
  const raw = n.stopped;
  const status = String(n.status || '').toLowerCase();
  if (raw && typeof raw === 'object') return raw;
  if (raw === true || n.stopped_by || n.stopped_at) {
    return {
      by: String(n.stopped_by || 'user'),
      at: Number(n.stopped_at) || 0,
      model: n.stopped_model || n.model_sent || n.model_settings || null,
      cost_usd: Number(n.stopped_cost_usd) || 0,
    };
  }
  if (STOPPED.has(status)) {
    return { by: 'user', at: Number(n.stopped_at) || 0, model: n.model_sent || n.model_settings || null };
  }
  return null;
}

/** Что сейчас с карточкой: running | stopped | idle. Терпимо к разным полям. */
export function runState(node) {
  const n = node && typeof node === 'object' ? node : {};
  const status = String(n.status || '').toLowerCase();
  if (stoppedInfo(n) || STOPPED.has(status)) return 'stopped';
  if (RUNNING.has(status)) return 'running';
  return 'idle';
}

/** Кнопка «Остановить» есть только у идущей работы. */
export function canStop(node) {
  return runState(node) === 'running' && Boolean(taskIdOf(node));
}

/**
 * «Продолжить» — у остановленной карточки. Сервер паркует остановленную карточку
 * в `blocked` + метка `stopped` (§W23.8), поэтому метку проверяем отдельно:
 * обычная `blocked` (ждёт зависимости) — не повод предлагать «Продолжить».
 */
export function canResume(node) {
  const n = node && typeof node === 'object' ? node : {};
  return Boolean(stoppedInfo(n));
}

/** Номер задачи из узла дерева или карточки участника (`current.task_id`). */
export function taskIdOf(node) {
  const n = node && typeof node === 'object' ? node : {};
  return String(n.id || n.task_id || (n.current && n.current.task_id) || '');
}

/**
 * «Остановлено вами в 10:41 · модель: qwen36v» — одна строка после остановки.
 * Причину контроля (`by: 'guard'`) называем честно, но без упрёка.
 */
export function stoppedText(node, catalog) {
  const n = node && typeof node === 'object' ? node : {};
  const stopped = stoppedInfo(n) || {};
  const at = Number(stopped.at) || 0;
  const by = String(stopped.by || 'user');
  // W23-ST: сервер отдаёт `model`/`provider` строками (`{by, at, model, provider, cost_usd}`),
  // демо раньше — объектом `{provider, model}`. Понимаем обе формы, чтобы подпись брала
  // ЧЕЛОВЕЧЕСКОЕ название модели из каталога, а не её id.
  const rawModel = stopped.model || n.model_sent || n.model_settings;
  const model = modelText(
    rawModel && typeof rawModel === 'object'
      ? rawModel
      : (rawModel ? { provider: String(stopped.provider || ''), model: String(rawModel) } : null),
    catalog);
  let text = by === 'guard' ? MODEL_GUARD_RU.stoppedByGuard : MODEL_GUARD_RU.stoppedByUser;
  const clock = clockText(at);
  if (clock) text += ' в ' + clock;
  if (model) text += ' · модель: ' + model;
  const cost = costText(stopped && stopped.cost_usd);
  if (cost) text += ' · ' + cost + ' на этой модели';
  return text;
}

function actionPath(requestId) {
  return `/api/requests/${encodeURIComponent(String(requestId || ''))}/actions`;
}

function toast(ctx, message, kind) {
  if (ctx && typeof ctx.toast === 'function') ctx.toast(message, kind || 'info');
}

function changed(ctx, onChanged) {
  if (typeof onChanged === 'function') { onChanged(); return; }
  if (ctx && typeof ctx.refresh === 'function') { ctx.refresh(); return; }
  if (typeof document !== 'undefined' && typeof CustomEvent === 'function') {
    document.dispatchEvent(new CustomEvent('rp:changed'));
  }
}

/** Текст ошибки человеческим языком (без кодов HTTP). */
export function cardErrorText(err) {
  const status = Number(err && (err.status || err.code));
  const own = String((err && err.message) || '').trim();
  if (status === 409) return 'Карточка уже изменилась: обновите страницу и посмотрите ещё раз';
  if (status === 404) return 'Карточка не найдена — возможно, её уже завершили';
  if (status === 422) return own || 'Так сейчас нельзя: приложение отказало';
  return own || 'Не получилось: приложение не ответило';
}

/**
 * Остановить работу карточки (POST actions {action:'card_stop', task_id}).
 * Кнопка блокируется на время запроса — двойное нажатие не создаёт второго.
 * @returns {Promise<boolean>} true, если остановили
 */
export async function stopCard(ctx, opts = {}) {
  const o = opts || {};
  const ask = typeof o.confirm === 'function' ? o.confirm : confirmDialog;
  const ok = await ask({
    title: MODEL_GUARD_RU.stopTitle,
    text: MODEL_GUARD_RU.stopConfirm,
    confirmLabel: MODEL_GUARD_RU.stop,
  });
  if (!ok) return false;
  if (!ctx || !ctx.api || typeof ctx.api.post !== 'function') {
    toast(ctx, 'Нет связи с приложением', 'warn');
    return false;
  }
  if (o.button && typeof o.button.setAttribute === 'function') o.button.setAttribute('disabled', '');
  try {
    const res = await ctx.api.post(actionPath(o.requestId), { action: 'card_stop', task_id: String(o.taskId || '') });
    toast(ctx, String((res && res.message) || MODEL_GUARD_RU.stopped), 'ok');
    changed(ctx, o.onChanged);
    return true;
  } catch (err) {
    toast(ctx, cardErrorText(err), 'warn');
    return false;
  } finally {
    if (o.button && typeof o.button.removeAttribute === 'function') o.button.removeAttribute('disabled');
  }
}

/** Продолжить остановленную карточку ({action:'card_resume', task_id}). */
export async function resumeCard(ctx, opts = {}) {
  const o = opts || {};
  if (!ctx || !ctx.api || typeof ctx.api.post !== 'function') {
    toast(ctx, 'Нет связи с приложением', 'warn');
    return false;
  }
  if (o.button && typeof o.button.setAttribute === 'function') o.button.setAttribute('disabled', '');
  try {
    const res = await ctx.api.post(actionPath(o.requestId), { action: 'card_resume', task_id: String(o.taskId || '') });
    toast(ctx, String((res && res.message) || MODEL_GUARD_RU.resumed), 'ok');
    changed(ctx, o.onChanged);
    return true;
  } catch (err) {
    toast(ctx, cardErrorText(err), 'warn');
    return false;
  } finally {
    if (o.button && typeof o.button.removeAttribute === 'function') o.button.removeAttribute('disabled');
  }
}

/**
 * Сменить модель карточки и продолжить ({action:'card_model', task_id, provider, model}).
 * Пустой выбор — «по умолчанию — модель профиля»: override снимается.
 */
export async function applyCardModel(ctx, opts = {}) {
  const o = opts || {};
  if (!ctx || !ctx.api || typeof ctx.api.post !== 'function') {
    toast(ctx, 'Нет связи с приложением', 'warn');
    return false;
  }
  const model = String(o.model || '');
  const body = {
    action: 'card_model',
    task_id: String(o.taskId || ''),
    provider: model ? String(o.provider || '') : '',
    // «По умолчанию — модель профиля» сервер понимает как `model: "none"`.
    model: model || 'none',
  };
  try {
    const res = await ctx.api.post(actionPath(o.requestId), body);
    toast(ctx, String((res && res.message) || 'Модель изменена — карточка продолжится на ней'), 'ok');
    changed(ctx, o.onChanged);
    return true;
  } catch (err) {
    toast(ctx, cardErrorText(err), 'warn');
    return false;
  }
}

/* ─────────────────────────────────────────────────────── разметка: карточка */

/**
 * Строка «Модель: <ожидаемая> · последний запуск: <фактическая> ✓/⚠» для карточки
 * участника. Заменяет прежнюю тусклую подпись «проверьте»: человек сразу видит,
 * куда уйдёт следующий запуск и куда ушёл прошлый.
 *
 * @param {object} member элемент `members[]` из GET /api/rooms/{id}/model-check
 * @param {{catalog?: object, onCheck?: Function, busy?: boolean}} [opts]
 * @returns {HTMLElement}
 */
export function guardLine(member, opts = {}) {
  const o = opts || {};
  const m = member && typeof member === 'object' ? member : {};
  const catalog = rememberCatalog(o.catalog);
  const state = memberState(m);
  const expected = settingsOf(m.expected);
  const last = settingsOf(m.last_run);
  const expectedName = modelText(expected, catalog) || MODEL_GUARD_RU.unknownExpected;
  const lastName = modelText(last, catalog);
  const cost = costText(m.last_run && m.last_run.cost_usd);

  // Стоимость — отдельным неразрывным куском: «≈ $2,45» не должно ломаться
  // на «≈» и «$2,45» при переносе строки.
  const runNode = lastName
    ? h('span', { class: 'mg-line__run' },
      MODEL_GUARD_RU.lastRun + ': ' + lastName,
      cost ? h('span', { class: 'mg-line__cost nowrap', title: 'Стоимость этого запуска' }, ' · ' + cost) : null)
    : h('span', { class: 'mg-line__run' }, MODEL_GUARD_RU.lastRun + ': ' + MODEL_GUARD_RU.never);

  const checkBtn = typeof o.onCheck === 'function'
    ? h('button', {
      class: 'btn btn--ghost btn--sm mg-line__check', type: 'button',
      title: 'Сверить сейчас, на какую модель уйдёт следующий запуск',
      onclick: (ev) => {
        if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
        o.onCheck(m.profile || '');
      },
    }, icon('refresh', { size: 11 }), h('span', null, o.busy ? MODEL_GUARD_RU.checking : MODEL_GUARD_RU.checkNow))
    : null;

  return h('div', {
    class: ['mg-line', 'mg-line--' + state],
    dataset: { profile: String(m.profile || '') },
    title: stateTitle(state) + (state === 'warn' ? ' · ' + problemText(m, catalog) : ''),
  },
  h('span', { class: 'mg-line__label' }, MODEL_GUARD_RU.cardPrefix + ':'),
  h('span', { class: 'mg-line__model' }, expectedName),
  h('span', { class: 'mg-line__sep', 'aria-hidden': 'true' }, '·'),
  runNode,
  h('span', {
    class: 'mg-line__mark mg-line__mark--' + state,
    'aria-label': stateMark(state),
  }, state === 'ok' ? icon('check', { size: 11 }) : state === 'warn' ? icon('warning', { size: 11 }) : icon('info', { size: 11 }),
  h('span', { class: 'mg-line__marktext' }, stateMark(state))),
  checkBtn);
}

/* ───────────────────────────────────────────── разметка: управление работой */

/**
 * Кнопки управления карточкой — как в самом Hermes: «Остановить», «Продолжить»,
 * «Сменить модель…». Используются и в карточке участника, и в дереве работ.
 *
 * @param {object} ctx {api, toast, refresh?}
 * @param {{node?: object, requestId?: string, taskId?: string, profile?: string,
 *          title?: string, catalog?: object, compact?: boolean,
 *          onChanged?: Function, confirm?: Function}} opts
 * @returns {HTMLElement}
 */
export function cardControls(ctx, opts = {}) {
  const o = opts || {};
  const node = o.node || {};
  const taskId = String(o.taskId || taskIdOf(node) || '');
  const requestId = String(o.requestId || node.request_id || '');
  const profile = String(o.profile || node.assignee || '');
  const catalog = rememberCatalog(o.catalog);
  const state = runState(node);
  const changedCb = o.onChanged;
  const box = h('div', { class: 'mg-actions' + (o.compact ? ' mg-actions--compact' : ''), dataset: { taskId } });

  if (!requestId) {
    // Нет номера запроса — управлять нечем; молчим, а не показываем мёртвую кнопку.
    box.hidden = true;
    return box;
  }

  if (state === 'running' && canStop(node)) {
    const stop = h('button', {
      class: 'btn btn--secondary btn--sm mg-actions__btn', type: 'button',
      title: MODEL_GUARD_RU.stopTitle,
    }, icon('stop-circle', { size: 11 }), h('span', null, MODEL_GUARD_RU.stop));
    stop.addEventListener('click', () => stopCard(ctx, {
      requestId, taskId, button: stop, confirm: o.confirm, onChanged: changedCb,
    }).catch(() => false));
    box.appendChild(stop);
  }

  // «Продолжить» у остановленной карточки рисует строка «Остановлено вами…»
  // (stoppedLine) — здесь второй такой же кнопки быть не должно.

  if (state !== 'idle' || node.error) {
    const change = h('button', {
      class: 'btn btn--ghost btn--sm mg-actions__btn', type: 'button',
      title: MODEL_GUARD_RU.changeHint,
    }, icon('edit', { size: 11 }), h('span', null, MODEL_GUARD_RU.changeModel));
    change.addEventListener('click', () => {
      const current = settingsOf(node.model_sent || node.model_settings || node.model || {});
      return openCardModelDialog(ctx, {
        requestId, taskId, profile, current, catalog: catalog || ctx.catalog,
        title: o.title || node.title || '',
        onDone: () => changed(ctx, changedCb),
      }).catch(() => false);
    });
    box.appendChild(change);
  }

  return box;
}

/** Строка «Остановлено вами в 10:41 · модель: …» с кнопкой «Продолжить». */
export function stoppedLine(ctx, opts = {}) {
  const o = opts || {};
  const node = o.node || {};
  if (runState(node) !== 'stopped') return null;
  const catalog = rememberCatalog(o.catalog);
  const box = h('div', { class: 'mg-stopped' },
    icon('stop-circle', { size: 12 }),
    h('span', { class: 'mg-stopped__text' }, stoppedText(node, catalog)));
  const requestId = String(o.requestId || node.request_id || '');
  const taskId = String(o.taskId || taskIdOf(node) || '');
  if (requestId) {
    const resume = h('button', {
      class: 'btn btn--primary btn--sm mg-stopped__btn', type: 'button',
      title: MODEL_GUARD_RU.resumeTitle,
    }, h('span', null, MODEL_GUARD_RU.resume));
    resume.addEventListener('click', () => resumeCard(ctx, {
      requestId, taskId, button: resume, onChanged: o.onChanged,
    }).catch(() => false));
    box.appendChild(resume);
  }
  return box;
}

/* ─────────────────────────────────────────── диалог «Сменить модель и продолжить» */

/**
 * Диалог выбора модели для карточки: список моделей из `/api/models` и
 * «по умолчанию — модель профиля». Выбор → POST `card_model`.
 *
 * @returns {Promise<boolean>} true, если модель сменили
 */
export function openCardModelDialog(ctx, opts = {}) {
  const o = opts || {};
  const d = globalThis.document;
  if (!d || !d.body) return Promise.resolve(false);
  const catalog = o.catalog || (ctx && ctx.catalog) || null;
  const current = settingsOf(o.current);
  const groups = providersOf(catalog);

  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      if (typeof overlay.remove === 'function') overlay.remove();
      else if (overlay.parentNode && typeof overlay.parentNode.removeChild === 'function') overlay.parentNode.removeChild(overlay);
      if (typeof d.removeEventListener === 'function') d.removeEventListener('keydown', onKey, true);
      resolve(Boolean(value));
    };
    const onKey = (ev) => {
      if (ev && ev.key === 'Escape') {
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        finish(false);
      }
    };

    const select = h('select', { class: 'select mg-dlg__select', 'aria-label': 'Модель для карточки' },
      h('option', { value: '' }, MODEL_GUARD_RU.defaultModel));
    for (const group of groups) {
      const og = h('optgroup', { label: String(group.label || group.id || '') });
      for (const model of Array.isArray(group.models) ? group.models : []) {
        og.appendChild(h('option', {
          value: String(group.id || '') + '::' + String(model.id || ''),
        }, String(model.label || model.id || '')));
      }
      select.appendChild(og);
    }
    // Текущая модель может быть не в каталоге: показываем её отдельной строкой,
    // иначе выбор «как сейчас» выглядел бы как «модель профиля».
    if (current.model && !modelInCatalog(catalog, current)) {
      select.appendChild(h('option', { value: current.provider + '::' + current.model },
        'как сейчас: ' + (current.model)));
    }
    const currentValue = current.provider + '::' + current.model;
    if (currentValue !== '::') select.value = currentValue;

    const submit = h('button', { class: 'btn btn--primary', type: 'button' },
      h('span', null, MODEL_GUARD_RU.changeAndResume));
    submit.addEventListener('click', async () => {
      const raw = String(select.value || '');
      const cut = raw.indexOf('::');
      const provider = cut === -1 ? '' : raw.slice(0, cut);
      const model = cut === -1 ? '' : raw.slice(cut + 2);
      submit.setAttribute('disabled', '');
      submit.replaceChildren(h('span', null, MODEL_GUARD_RU.savingModel));
      const ok = await applyCardModel(ctx, {
        requestId: o.requestId, taskId: o.taskId, provider, model,
      });
      if (ok && typeof o.onDone === 'function') o.onDone();
      finish(ok);
    });

    const overlay = h('div', { class: 'dialog-backdrop' },
      h('div', {
        class: 'dialog mg-dlg', role: 'dialog', 'aria-modal': 'true',
        'aria-label': MODEL_GUARD_RU.changeModel,
      },
      h('div', { class: 'dialog__head' },
        h('h2', { class: 'dialog__title' }, MODEL_GUARD_RU.changeModel),
        h('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', title: 'Закрыть', onclick: () => finish(false) },
          icon('close', { size: 13 }))),
      h('div', { class: 'dialog__body mg-dlg__body' },
        h('p', { class: 'small muted' }, MODEL_GUARD_RU.changeHint
          + (o.title ? ' Карточка: «' + String(o.title) + '».' : '')),
        h('div', { class: 'field' },
          h('span', { class: 'field__label' }, 'Модель'),
          select)),
      h('div', { class: 'dialog__foot' },
        h('button', { class: 'btn btn--secondary', type: 'button', onclick: () => finish(false) }, 'Отмена'),
        submit)));

    overlay.addEventListener('mousedown', (ev) => { if (ev && ev.target === overlay) finish(false); });
    if (typeof d.addEventListener === 'function') d.addEventListener('keydown', onKey, true);
    (d.body || d.documentElement).appendChild(overlay);
    if (typeof select.focus === 'function') select.focus();
  });
}

/** Модель есть в каталоге (для диалога: не дублировать строку «как сейчас»). */
export function modelInCatalog(catalog, settings) {
  const s = settingsOf(settings);
  if (!s.model) return false;
  for (const group of providersOf(catalog)) {
    if (s.provider && String(group.id || '') !== s.provider) continue;
    for (const model of Array.isArray(group.models) ? group.models : []) {
      if (normalizeModel(model.id) === normalizeModel(s.model)) return true;
    }
  }
  return false;
}

/* ──────────────────────────────────────────── разметка: лента на странице запроса */

/**
 * Лента событий контроля на странице запроса: что остановили, что исправили до
 * запуска и сколько это сэкономило. `null`, если событий нет — пустую карточку
 * на странице не показываем.
 *
 * @param {object} detail RequestDetail (`model_guard[]`)
 * @param {object} [ctx]
 * @param {{catalog?: object, compact?: boolean}} [opts]
 * @returns {HTMLElement|null}
 */
export function buildRequestFeed(detail, ctx, opts = {}) {
  const o = opts || {};
  const events = guardEventsOf(detail);
  if (!events.length) return null;
  const catalog = rememberCatalog(o.catalog || (ctx && ctx.catalog));
  const sum = guardSummary(events);
  const cost = costText(sum.saved_usd);

  const chips = [];
  if (sum.stopped) chips.push(h('span', { class: 'chip chip--warn mg-feed__chip tnum' },
    sum.stopped + ' ' + pluralRu(sum.stopped, ['остановка', 'остановки', 'остановок'])));
  if (sum.fixed) chips.push(h('span', { class: 'chip chip--ok mg-feed__chip tnum' },
    sum.fixed + ' ' + pluralRu(sum.fixed, ['исправление', 'исправления', 'исправлений'])));
  if (cost) chips.push(h('span', { class: 'chip chip--muted mg-feed__chip' }, 'сэкономлено ' + cost));

  const list = h('ul', { class: 'mg-feed__list' });
  for (const ev of events) {
    const meta = GUARD_EVENT_META[guardKind(ev.kind)];
    list.appendChild(h('li', { class: 'mg-feed__item mg-feed__item--' + ev.kind, dataset: { kind: ev.kind } },
      h('span', { class: 'mg-feed__icon mg-feed__icon--' + meta.tone, 'aria-hidden': 'true' },
        icon(meta.icon, { size: 12 })),
      h('div', { class: 'mg-feed__body' },
        h('p', { class: 'mg-feed__text' }, guardEventText(ev, catalog)),
        h('p', { class: 'mg-feed__meta small muted' },
          h('span', { class: 'mg-feed__kind' }, meta.label),
          ev.at ? h('span', { class: 'mg-feed__at', title: 'Когда это произошло' }, ' · ' + timeAgo(ev.at)) : null,
          ev.profile ? h('span', { class: 'mg-feed__who', title: 'Участник · профиль Hermes: ' + ev.profile },
            ' · ' + personLabel(ev.profile, ctx)) : null))));
  }

  return h('section', {
    class: 'card mg-feed', id: 'model-guard', 'data-model-guard': 'feed',
    'aria-label': MODEL_GUARD_RU.feedTitle,
  },
  h('div', { class: 'mg-feed__head' },
    h('div', { class: 'mg-feed__title-row' },
      h('span', { class: 'mg-feed__title' }, icon('shield', { size: 14 }), MODEL_GUARD_RU.feedTitle),
      h('div', { class: 'mg-feed__chips' }, chips)),
    h('p', { class: 'mg-feed__lead small muted' }, MODEL_GUARD_RU.feedLead)),
  list);
}

/** Имя участника для ленты: «Тимур», иначе имя профиля (без внутренних id). */
function personLabel(profile, ctx) {
  const id = String(profile || '');
  if (!id) return '';
  try {
    const label = participantLabel(id, { room: (ctx && ctx.room) || null });
    return String(label.title || label.short || id);
  } catch {
    return id;
  }
}

/** Склонение без лишних импортов: [один, два, много]. */
function pluralRu(n, forms) {
  const num = Math.abs(Number(n)) || 0;
  const mod10 = num % 10;
  const mod100 = num % 100;
  if (mod100 >= 11 && mod100 <= 14) return forms[2];
  if (mod10 === 1) return forms[0];
  if (mod10 >= 2 && mod10 <= 4) return forms[1];
  return forms[2];
}

/* ──────────────────────────────────────────── настройка комнаты (room-dialog) */

/** Значение поля выбора: свойство в браузере, атрибут — в мини-DOM тестов. */
function inputValue(input) {
  if (!input) return '';
  const prop = input.value;
  if (prop !== undefined && prop !== null && prop !== '') return String(prop);
  const attr = typeof input.getAttribute === 'function' ? input.getAttribute('value') : null;
  return attr === null || attr === undefined ? '' : String(attr);
}

/**
 * Поле «Контроль моделей: строго / только предупреждать» для диалога комнаты.
 * Простыми словами: что произойдёт при расхождении модели.
 *
 * @param {{room?: object, onChange?: Function, idPrefix?: string}} [opts]
 * @returns {{el: HTMLElement, value: Function, set: Function}}
 */
export function guardModeField(opts = {}) {
  const o = opts || {};
  const room = o.room || {};
  const name = (o.idPrefix || 'roomdlg') + '-model-guard';
  const value = guardModeOf(room);
  const inputs = [];
  const rows = GUARD_MODES.map((mode) => {
    const input = h('input', {
      type: 'radio', name, value: mode, id: name + '-' + mode,
    });
    input.checked = mode === value;
    if (typeof o.onChange === 'function') input.addEventListener('change', o.onChange);
    inputs.push(input);
    return h('label', { class: 'mg-mode__option', for: name + '-' + mode },
      input,
      h('span', { class: 'mg-mode__text' },
        h('span', { class: 'mg-mode__label' }, MODEL_GUARD_RU.modes[mode]),
        h('span', { class: 'mg-mode__hint small muted' }, MODEL_GUARD_RU.modeTexts[mode])));
  });

  const el = h('div', { class: 'roomdlg__section mg-mode', dataset: { modelGuard: value } },
    h('div', { class: 'roomdlg__label' }, MODEL_GUARD_RU.modeTitle),
    h('div', { class: 'roomdlg__hint' }, MODEL_GUARD_RU.modeHint),
    h('div', { class: 'mg-mode__options', role: 'radiogroup', 'aria-label': MODEL_GUARD_RU.modeTitle }, rows));

  return {
    el,
    value() {
      const picked = inputs.find((i) => i.checked);
      const mode = inputValue(picked);
      return GUARD_MODES.includes(mode) ? mode : 'enforce';
    },
    set(mode) {
      const want = GUARD_MODES.includes(String(mode)) ? String(mode) : 'enforce';
      for (const input of inputs) input.checked = inputValue(input) === want;
      el.setAttribute('data-model-guard', want);
    },
  };
}
