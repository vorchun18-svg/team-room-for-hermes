/**
 * kick-panel.js — «Проверить доску сейчас» (WAVE13 §8, пакет F).
 *
 * Одна кнопка в трёх местах (блок «Свод команды», меню запроса, «Здоровье») и один
 * итог: что проход диспетчера сделал и почему ничего не запущено.
 *
 * Правила, которые держит этот модуль:
 *   * название и подсказка — ОДНИ на всё приложение (`KICK_LABEL`/`KICK_HINT`);
 *   * итог — панель с подробностями, а не «движок подтолкнут» одной строкой;
 *   * кнопка осмысленна, только если есть готовые к запуску или зависшие карточки;
 *     иначе интерфейс объясняет, почему её нет (`kickWhyNot`).
 *
 * Источники состояния доски:
 *   * страница запроса — дерево работ (`kickStateFromTree`), без запроса к серверу;
 *   * «Здоровье» в настройках — предпросмотр `POST /api/dispatcher/kick {preview:true}`
 *     (только чтение доски, ничего не запускает).
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { countNoun } from '../text-ru.js';

/** Единое название кнопки во всём интерфейсе. */
export const KICK_LABEL = 'Проверить доску сейчас';
/** Единая подсказка: что происходит по нажатию. */
export const KICK_HINT = 'Не ждать минутного таймера — запустить готовые задачи';

/** Работа идёт дольше — похоже, зависла (проход вернёт её в работу).
 *  По дереву работ heartbeat не видно, поэтому порог мягче серверного (15 минут). */
const LONG_RUN_S = 45 * 60;

/** Коды причин — те же, что у сервера (`server/kick_report.py`). */
export const REASON_TEXT = {
  ready: 'готова к запуску',
  stuck: 'зависла: нет отклика',
  running: 'уже идёт',
  input: 'ждёт вашего ответа',
  blocked: 'ждёт решения',
  review: 'на проверке',
  deps: 'ждёт других задач',
  triage: 'ждёт разбора',
  scheduled: 'отложена по времени',
  queued: 'в очереди',
};

const num = (value) => {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
};

const countsOf = (value) => (value && typeof value === 'object' ? value : {});

/* ─────────────────────────────────────── состояние и осмысленность */

/**
 * Состояние доски по дереву работ запроса: те же коды причин, что у сервера.
 * @param {Array} tree узлы дерева (`RequestDetail.tree`)
 * @param {number} [now] текущее время, мс (для тестов)
 * @returns {{source: string, ok: boolean, counts: object, cards: object[], useful: boolean}}
 */
export function kickStateFromTree(tree, now = Date.now()) {
  const nodes = (Array.isArray(tree) ? tree : []).filter((node) => node && node.id);
  const stamp = Number(now) / 1000;
  const counts = { ready: 0, stuck: 0, running: 0, review: 0, input: 0, blocked: 0,
    deps: 0, triage: 0, scheduled: 0, queued: 0, total: 0 };
  const cards = [];
  for (const node of nodes) {
    const status = String(node.status || '');
    let code = '';
    if (status === 'ready') code = 'ready';
    else if (status === 'running') {
      const started = Number(node.started_at);
      const long = Number.isFinite(started) && started > 0 && stamp - started > LONG_RUN_S;
      code = long ? 'stuck' : 'running';
    } else if (status === 'review') code = 'review';
    else if (status === 'blocked') code = node.needs_human ? 'input' : 'blocked';
    else if (status === 'todo') code = 'deps';
    else if (status === 'triage' || status === 'scheduled' || status === 'queued') code = status;
    if (!code || !(code in counts)) continue;
    counts[code] += 1;
    counts.total += 1;
    cards.push({ id: String(node.id), title: String(node.title || ''), assignee: node.assignee,
      status, reason_code: code, reason: REASON_TEXT[code] || code });
  }
  return { source: 'request', ok: true, counts, cards,
    useful: counts.ready + counts.stuck > 0 };
}

/**
 * Нужна ли кнопка: есть готовые к запуску или зависшие работы.
 * @param {{counts?: object}|null} state
 * @returns {{useful: boolean, ready: number, stuck: number}}
 */
export function kickUsefulness(state) {
  const c = countsOf(state && state.counts);
  const ready = num(c.ready);
  const stuck = num(c.stuck);
  return { useful: ready + stuck > 0, ready, stuck };
}

const pluralBits = (c) => {
  const bits = [];
  if (num(c.ready)) bits.push(countNoun(num(c.ready), ['готова к запуску', 'готовы к запуску', 'готовы к запуску']));
  if (num(c.stuck)) bits.push(countNoun(num(c.stuck), ['зависла', 'зависли', 'зависли']));
  if (num(c.running) + num(c.review)) {
    bits.push(countNoun(num(c.running) + num(c.review), ['уже идёт', 'уже идут', 'уже идут']));
  }
  if (num(c.input)) bits.push(countNoun(num(c.input), ['ждёт вашего ответа', 'ждут вашего ответа', 'ждут вашего ответа']));
  if (num(c.blocked)) bits.push(countNoun(num(c.blocked), ['ждёт решения', 'ждут решения', 'ждут решения']));
  if (num(c.deps)) bits.push(countNoun(num(c.deps), ['ждёт других задач', 'ждут других задач', 'ждут других задач']));
  if (num(c.triage) + num(c.queued)) {
    bits.push(countNoun(num(c.triage) + num(c.queued), ['ждёт разбора', 'ждут разбора', 'ждут разбора']));
  }
  if (num(c.scheduled)) bits.push(countNoun(num(c.scheduled), ['отложена по времени', 'отложены по времени', 'отложены по времени']));
  return bits;
};

/**
 * Почему кнопка не нужна — одной понятной фразой.
 * @param {{counts?: object}|null} state
 * @returns {string}
 */
export function kickWhyNot(state) {
  const bits = pluralBits(countsOf(state && state.counts));
  if (!bits.length) return 'Проверять доску сейчас не нужно: запускать нечего — всё готово.';
  return 'Проверять доску сейчас не нужно: ' + bits.join(', ') + '.';
}

/* ─────────────────────────────────────────── ответы сервера → итог */

/**
 * Любой ответ прохода → итог. Понимает и новый `report`, и старый ответ
 * (`spawned/reclaimed/promoted/planned`) — например, демо-мок.
 * @param {object|null} res
 * @returns {object}
 */
export function kickReport(res) {
  const raw = res && typeof res === 'object' ? res : {};
  if (raw.report && typeof raw.report === 'object') return raw.report;
  if (raw.kind && (raw.counts || raw.cards)) return raw;
  const spawned = num(raw.spawned);
  const reclaimed = num(raw.reclaimed);
  const promoted = num(raw.promoted);
  const planned = Array.isArray(raw.planned) ? raw.planned : [];
  return {
    ok: true,
    legacy: true,
    kind: spawned ? 'started' : (reclaimed ? 'reclaimed' : (promoted ? 'promoted' : 'nothing')),
    spawned, reclaimed, promoted,
    started: planned.filter((card) => card && card.id)
      .map((card) => ({ id: String(card.id), assignee: card.assignee, title: '' })),
    counts: {}, cards: [], useful: null,
    at: Math.floor(Date.now() / 1000),
  };
}

/** Ошибка прохода (429 «не чаще раза в 30 секунд», 409 песочница) → итог. */
export function kickError(err) {
  const message = String((err && (err.message || err.error)) || 'Не удалось проверить доску');
  return { ok: false, kind: 'error', spawned: 0, reclaimed: 0, promoted: 0,
    started: [], counts: {}, cards: [], useful: null, message,
    at: Math.floor(Date.now() / 1000) };
}

/**
 * Итог → заголовок и строки. Единственное место, где этот текст собирается.
 * @param {object} report
 * @param {{scopeIds?: Set<string>|string[], labelOf?: Function}} [opts]
 * @returns {{title: string, lines: string[], tone: string, icon: string}}
 */
export function kickText(report, opts = {}) {
  const r = report && typeof report === 'object' ? report : {};
  const o = opts || {};
  const labelOf = typeof o.labelOf === 'function' ? o.labelOf : (p) => String(p || '');
  const kind = String(r.kind || 'nothing');
  const spawned = num(r.spawned);
  const reclaimed = num(r.reclaimed);
  const promoted = num(r.promoted);
  const scope = o.scopeIds instanceof Set ? o.scopeIds : new Set(Array.isArray(o.scopeIds) ? o.scopeIds : []);
  const c = countsOf(r.counts);
  const lines = [];

  if (kind === 'error') {
    return { title: 'Не получилось проверить доску', tone: 'warn', icon: 'warning',
      lines: [String(r.message || 'Попробуйте ещё раз через полминуты.')] };
  }

  const started = Array.isArray(r.started) ? r.started : [];
  const inScope = started.filter((card) => !scope.size || scope.has(String(card.id)));
  const cardLabel = (card) => {
    const who = labelOf(card.assignee);
    const what = String(card.title || '').trim();
    return what ? `${who || 'исполнитель'} — ${what}` : (who || 'исполнитель');
  };

  let title;
  let tone = 'ok';
  let iconName = 'zap';
  if (kind === 'started') {
    title = `${spawned === 1 ? 'Запущен' : 'Запущено'} ${countNoun(spawned, ['исполнитель', 'исполнителя', 'исполнителей'])}`;
    const list = (scope.size ? inScope : started);
    const shown = list.slice(0, 5);
    if (shown.length) lines.push(shown.map(cardLabel).join('; '));
    const rest = list.length - shown.length;
    if (rest > 0) lines.push(`И ещё ${countNoun(rest, ['исполнитель', 'исполнителя', 'исполнителей'])}.`);
    if (scope.size && !inScope.length && started.length) {
      lines.push('В этом запросе новых запусков нет — работы взяты из других.');
    }
  } else if (kind === 'reclaimed') {
    title = `Возвращено зависших: ${reclaimed}`;
    lines.push('Работы, которые не отвечали, вернутся в очередь — диспетчер возьмёт их в ближайшую минуту.');
  } else if (kind === 'promoted') {
    title = `Готовы к запуску: ${promoted}`;
    tone = 'ok';
    iconName = 'arrow-up';
    lines.push('Работы переведены из ожидания в готовые — запустятся сами в течение минуты.');
  } else {
    title = 'Запускать нечего';
    tone = 'info';
    iconName = 'info';
  }

  if (kind === 'started' && reclaimed) lines.push(`Возвращено зависших: ${reclaimed}.`);
  if (kind === 'started' && promoted) lines.push(`Переведено из ожидания в готовые: ${promoted}.`);

  if (kind === 'nothing' || kind === 'started' || kind === 'reclaimed' || kind === 'promoted') {
    const rest = pluralBits({ ...c, ready: kind === 'nothing' ? 0 : c.ready,
      stuck: kind === 'nothing' ? 0 : c.stuck });
    if (rest.length) lines.push('Сейчас: ' + rest.join(', ') + '.');
  }
  if (kind === 'nothing' && !lines.length) lines.push('Все работы уже идут или готовы — проверять нечего.');
  return { title, lines, tone, icon: iconName };
}

/* ───────────────────────────────────────────────────── панель итога */

/**
 * Панель итога (заметный результат вместо однострочного тоста).
 * @param {object} report итог (`kickReport`)
 * @param {{labelOf?: Function, scopeIds?: Set<string>|string[], onClose?: Function}} [opts]
 * @returns {HTMLElement}
 */
export function kickPanel(report, opts = {}) {
  const o = opts || {};
  const text = kickText(report, o);
  const el = h('div', {
    class: 'banner banner--' + text.tone,
    'data-kick': 'result',
    role: 'status',
    'aria-live': 'polite',
  },
    icon(text.icon, { size: 14 }),
    h('div', { class: 'banner__text stack stack--tight' },
      h('div', { 'data-kick': 'title' }, text.title),
      ...text.lines.map((line) => h('div', { class: 'small', 'data-kick': 'line' }, line))));
  if (typeof o.onClose === 'function') {
    el.appendChild(h('button', {
      type: 'button', class: 'btn btn--link btn--sm', 'data-kick': 'close',
      title: 'Скрыть итог', onclick: () => o.onClose(),
    }, 'Скрыть'));
  }
  return el;
}

/** Короткая строка итога — для тоста/подписи (без подробностей). */
export function kickTitle(report, opts = {}) {
  return kickText(report, opts).title;
}

/* ───────────────────────────────────────────────────── запросы к API */

/** Предпросмотр: состояние доски без запуска (работает и в песочнице). */
export async function kickPreview(api) {
  const res = await api.post('/api/dispatcher/kick', { preview: true });
  const report = res && res.report ? res.report : null;
  return report && typeof report === 'object' ? report : null;
}

/**
 * Нажать «Проверить доску сейчас»: с страницы запроса — через действия запроса
 * (ответ обновит и сам запрос), из настроек — проходом по доске.
 * @param {{post: Function}} api
 * @param {string} [rid] id запроса (пусто — проход по всей доске)
 * @returns {Promise<object>} итог (`kickReport`), при ошибке — `kickError`
 */
export async function kickNow(api, rid = '') {
  try {
    const res = rid
      ? await api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, { action: 'dispatch' })
      : await api.post('/api/dispatcher/kick', {});
    return kickReport(res);
  } catch (err) {
    return kickError(err);
  }
}
