/**
 * request-parts-model.js — чистая логика блоков страницы запроса.
 *
 * Без DOM и без зависимостей: модуль импортируется и в браузере (вьюхи),
 * и в node --test. Здесь только решения «что показать»:
 *   buildTree    — дерево карточек по parent_ids, с защитой от циклов;
 *   pipeline     — линейка этапов «Разбор → Работа → Критик → Сводка → Готово»;
 *   nodeActions  — какие действия допустимы для карточки в её статусе;
 *   warningItems — понятные русские предупреждения из detail.warnings;
 *   criticInfo   — состояние критика (чип, вердикт, раунд).
 *
 * Все функции терпимы к неполным данным: фикстуры и legacy-карточки
 * приходят с пропущенными полями, страница не должна падать.
 */

import { plainTerms, countNoun } from '../text-ru.js';

/* ─────────────────────────────────────────────────────────────── дерево работ */

const MAX_DEPTH = 8;

/** Сортировка карточек: по времени начала, затем по id. */
function byStart(a, b) {
  const sa = Number(a.started_at || a.created_at || 0);
  const sb = Number(b.started_at || b.created_at || 0);
  if (sa !== sb) return sa - sb;
  return String(a.id).localeCompare(String(b.id));
}

/**
 * Разложить плоский список карточек в дерево по parent_ids.
 * Корень (`role === "root"`) идёт первым, дети — по времени начала.
 * Циклы (a → b → a) и висячие parent_ids не ломают вывод: такая карточка
 * считается корневой, её id попадает в `cycleIds`.
 *
 * @param {Array<object>} nodes карточки TaskNode
 * @returns {{rows: Array<{id: string, node: object, depth: number, childCount: number, cycle: boolean}>,
 *            byId: Map<string, object>, roots: string[], cycleIds: string[]}}
 */
export function buildTree(nodes) {
  const list = (Array.isArray(nodes) ? nodes : []).filter((n) => n && n.id);
  const byId = new Map();
  for (const node of list) if (!byId.has(node.id)) byId.set(node.id, node);
  const unique = [...byId.values()];

  /** Первый существующий родитель, кроме самого себя. */
  const parentOf = (node) => {
    const parents = Array.isArray(node.parent_ids) ? node.parent_ids : [];
    for (const pid of parents) {
      if (pid && pid !== node.id && byId.has(pid)) return pid;
    }
    return null;
  };

  // Циклы: идём вверх по родителям; если вернулись к себе — связь рвём.
  const cycleIds = [];
  const parentCache = new Map();
  for (const node of unique) {
    let cursor = node;
    const seen = new Set([node.id]);
    let cycle = false;
    for (let guard = 0; guard < unique.length + 1; guard++) {
      const pid = parentOf(cursor);
      if (!pid) break;
      if (seen.has(pid)) { cycle = true; break; }
      seen.add(pid);
      cursor = byId.get(pid);
      if (!cursor) break;
    }
    if (cycle) {
      cycleIds.push(node.id);
      parentCache.set(node.id, null);
    } else {
      parentCache.set(node.id, parentOf(node));
    }
  }

  // Дети по вычисленному родителю.
  const childrenOf = new Map();
  for (const node of unique) {
    const pid = parentCache.get(node.id);
    if (!pid) continue;
    if (!childrenOf.has(pid)) childrenOf.set(pid, []);
    childrenOf.get(pid).push(node);
  }
  for (const kids of childrenOf.values()) kids.sort(byStart);

  const roots = unique
    .filter((n) => !parentCache.get(n.id))
    .sort((a, b) => {
      const ra = a.role === 'root' ? 0 : 1;
      const rb = b.role === 'root' ? 0 : 1;
      if (ra !== rb) return ra - rb;
      return byStart(a, b);
    });

  const rows = [];
  const visited = new Set();
  const walk = (node, depth) => {
    if (!node || visited.has(node.id)) return;
    visited.add(node.id);
    const kids = childrenOf.get(node.id) || [];
    rows.push({
      id: node.id,
      node,
      depth: Math.min(depth, MAX_DEPTH),
      childCount: kids.length,
      cycle: cycleIds.includes(node.id),
    });
    for (const kid of kids) walk(kid, depth + 1);
  };
  for (const root of roots) walk(root, 0);

  // Страховка: карточки, не достигнутые обходом, показываем в конце.
  for (const node of unique) {
    if (visited.has(node.id)) continue;
    visited.add(node.id);
    rows.push({ id: node.id, node, depth: 0, childCount: 0, cycle: true });
    if (!cycleIds.includes(node.id)) cycleIds.push(node.id);
  }

  return { rows, byId, roots: roots.map((n) => n.id), cycleIds };
}

/* ──────────────────────────────────────────────────────────────── этапы работы */

/** Пять этапов страницы запроса. `critic` может быть пропущен. */
export const STAGES = [
  { id: 'parse', title: 'Разбор', icon: 'git-branch', hint: 'Руководитель комнаты разбирает запрос на срезы' },
  { id: 'work', title: 'Работа', icon: 'robot', hint: 'Команда выполняет срезы' },
  { id: 'critic', title: 'Критик', icon: 'shield', hint: 'Независимая проверка свода' },
  { id: 'summary', title: 'Сводка', icon: 'note', hint: 'Руководитель комнаты сводит один ответ' },
  { id: 'done', title: 'Готово', icon: 'check', hint: 'Ответ можно читать' },
];

/** Есть ли у запроса работающий критик (legacy → false). */
export function criticEnabled(detail) {
  const c = detail && detail.critic;
  if (!c || typeof c !== 'object') return false;
  return c.enabled !== false && c.state !== 'off';
}

/**
 * Состояние линейки этапов.
 * @param {object} detail RequestDetail
 * @returns {{stages: Array<object>, done: number, total: number, percent: number,
 *            currentId: string, nowText: string, tone: string}}
 *   stage.state: done | active | paused | failed | todo | skipped
 */
export function pipeline(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const status = String(d.status || 'queued');
  const progress = d.progress && typeof d.progress === 'object' ? d.progress : {};
  const totalCards = Number(progress.total) || 0;
  const doneCards = Number(progress.done) || 0;
  const version = Number(d.synthesis && d.synthesis.version) || 0;
  const enabled = criticEnabled(d);

  const state = new Map(STAGES.map((s) => [s.id, 'todo']));
  const hint = new Map();
  let tone = 'info';
  let nowText = '';

  // Порядок без пропущенного этапа: «всё до» помечаем done, целевой — value.
  const order = STAGES.filter((s) => enabled || s.id !== 'critic');
  const fillThrough = (id, value) => {
    let idx = order.findIndex((s) => s.id === id);
    if (idx < 0) {
      const fullIdx = STAGES.findIndex((s) => s.id === id);
      idx = order.findIndex((s) => STAGES.findIndex((x) => x.id === s.id) > fullIdx);
      if (idx < 0) idx = order.length - 1;
    }
    for (let i = 0; i < idx; i++) state.set(order[i].id, 'done');
    state.set(order[idx].id, value);
  };

  if (status === 'queued') {
    if (totalCards > 0) {
      state.set('parse', 'done');
      state.set('work', 'active');
      nowText = 'сейчас: запрос разложен, работа ещё не началась';
    } else {
      state.set('parse', 'active');
      nowText = 'сейчас: руководитель комнаты разбирает запрос';
    }
    if (!enabled) state.set('critic', 'skipped');
  } else if (status === 'working') {
    fillThrough('work', 'active');
    if (totalCards > 0) {
      nowText = `сейчас: команда работает, готово ${doneCards} из ${totalCards}`;
    } else {
      nowText = 'сейчас: команда взяла запрос в работу';
    }
  } else if (status === 'reviewing') {
    fillThrough('critic', 'active');
    const c = d.critic || {};
    const round = Number(c.round) || 1;
    nowText = `сейчас: критик проверяет свод${round ? `, раунд ${round}` : ''}`;
    if (version > 0) hint.set('summary', 'черновик готов и доступен');
  } else if (status === 'synthesizing') {
    fillThrough('summary', 'active');
    const c = d.critic || {};
    if (enabled && c.state === 'revised') state.set('critic', 'done');
    nowText = version > 0
      ? 'сейчас: руководитель комнаты дорабатывает свод по замечаниям'
      : 'сейчас: руководитель комнаты сводит ответ';
  } else if (status === 'needs_you') {
    if (totalCards > 0) {
      fillThrough('work', 'paused');
      nowText = 'сейчас: работа ждёт вашего ответа';
    } else {
      state.set('parse', 'paused');
      nowText = 'сейчас: нужно ваше решение, чтобы начать';
    }
    tone = 'warn';
  } else if (status === 'failed') {
    const c = d.critic || {};
    if (enabled && c.state === 'failed') {
      fillThrough('critic', 'failed');
      nowText = 'сейчас: критик не смог проверить свод';
    } else if (version > 0) {
      fillThrough('summary', 'failed');
      nowText = 'сейчас: доработка свода остановлена';
    } else if (totalCards > 0) {
      fillThrough('work', 'failed');
      nowText = 'сейчас: работа остановлена из-за ошибки';
    } else {
      state.set('parse', 'failed');
      nowText = 'сейчас: запрос не удалось разобрать';
    }
    tone = 'danger';
  } else if (status === 'done') {
    fillThrough('done', 'done');
    const c = d.critic || {};
    if (enabled && c.state === 'revised') hint.set('critic', 'вернул на доработку');
    if (enabled && c.state === 'approved') hint.set('critic', 'принял без замечаний');
    nowText = 'сейчас: ответ готов';
    tone = 'ok';
  } else {
    state.set('parse', 'active');
    nowText = 'сейчас: состояние запроса уточняется';
  }

  if (!enabled) state.set('critic', 'skipped');

  const stages = STAGES.map((s) => ({
    ...s,
    state: state.get(s.id) || 'todo',
    hint: hint.get(s.id) || s.hint,
    skipped: !enabled && s.id === 'critic',
  })).filter((s) => s.state !== 'skipped');

  const total = stages.length;
  const done = stages.filter((s) => s.state === 'done').length;
  const current = stages.find((s) => ['active', 'paused', 'failed'].includes(s.state));
  const currentId = current ? current.id : (stages[stages.length - 1] || {}).id || 'parse';

  return {
    stages,
    done,
    total,
    percent: total ? Math.round((done / total) * 100) : 0,
    currentId,
    nowText,
    tone,
    withCritic: enabled,
  };
}

/* ─────────────────────────────────────────────────────────── действия карточки */

const NO_ACTIONS = new Set(['archived']);

/**
 * Какие действия показывать на карточке. Порядок — порядок кнопок.
 * Правила совпадают с ограничениями сервера: завершённые и архивные карточки
 * не переназначаются, доработка недоступна для завершённых, повтор — только
 * после ошибки, ответ — только для заблокированной карточки.
 *
 * @param {object} node TaskNode
 * @returns {string[]} подмножество ['answer','retry','reassign','rework']
 */
export function nodeActions(node) {
  const n = node && typeof node === 'object' ? node : {};
  const status = String(n.status || 'todo');
  if (NO_ACTIONS.has(status)) return [];
  const out = [];

  if (status === 'blocked' || (n.needs_human === true && status !== 'done')) out.push('answer');
  if (n.error || status === 'failed') out.push('retry');
  if (status !== 'done' && status !== 'archived' && n.role !== 'root') out.push('reassign');
  if (['blocked', 'scheduled', 'failed', 'review'].includes(status)) out.push('rework');
  return out;
}

/* ─────────────────────────────────────────────────────────────── предупреждения */

const WARNING_META = {
  legacy_metadata: {
    tone: 'info',
    title: 'Ответ в старом формате',
    hint: 'Старые ответы могут быть неполными — недостающее ищите в дереве работ.',
  },
  missing_artifact: {
    tone: 'warn',
    title: 'Файл не найден',
    hint: 'Проверьте путь в блоке «Артефакты»: возможно, файл перемещён.',
  },
  no_synthesis: {
    tone: 'warn',
    title: 'Текста ответа нет',
    hint: 'Задача завершена, но свод пуст — итог ищите в дереве работ.',
  },
  report_invalid: {
    tone: 'warn',
    title: 'Отчёт не прошёл проверку',
    hint: 'Отчёт собран с ошибками — попросите команду собрать его заново.',
  },
  schema_drift: {
    tone: 'danger',
    title: 'Данные движка изменили формат',
    hint: 'Приложение только показывает данные — обновите приложение или движок команды.',
  },
  partial_ingest: {
    tone: 'warn',
    title: 'В проект перенесена часть данных',
    hint: 'Шаги и решения могли попасть не все — проверьте «Дальше» и «Карту проекта».',
  },
  dispatcher_down: {
    tone: 'warn',
    title: 'Движок команды остановлен',
    hint: 'Работа сама не начнётся: запустите движок команды в Hermes.',
  },
  card_failed: {
    tone: 'danger',
    title: 'Задача завершилась ошибкой',
    hint: 'Подробности и кнопка «Повторить» — в «Своде команды» и дереве работ.',
  },
  outside_room: {
    tone: 'warn',
    title: 'Задача вне состава комнаты',
    hint: 'Кнопка «Переназначить» — на задаче в дереве работ.',
  },
  critic_failed: {
    tone: 'danger',
    title: 'Критик не смог проверить',
    hint: 'Свод отдан как есть; проверку можно запустить повторно.',
  },
  critic_rounds_exhausted: {
    tone: 'warn',
    title: 'Раунды критика исчерпаны',
    hint: 'Ответ отдан как есть — замечания ниже.',
  },
};

const WARNING_TONE_RU = { info: 'info', warn: 'warn', danger: 'danger', error: 'danger', ok: 'ok' };

/** Семантический тон → класс баннера (в components.css «опасность» — banner--error). */
function bannerClassFor(tone) {
  return `banner--${tone === 'danger' ? 'error' : tone}`;
}

/**
 * Предупреждения для баннеров: detail.warnings + dispatcher.warning +
 * карточки вне состава комнаты. Дубли по коду и карточке убираются.
 *
 * @param {object} detail RequestDetail
 * @returns {Array<{code: string, tone: string, title: string, text: string,
 *                  hint: string, taskId: string|null, bannerClass: string}>}
 */
export function warningItems(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const out = [];
  const seen = new Set();

  const push = (item) => {
    const key = `${item.code}|${item.taskId || ''}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(item);
  };

  for (const raw of Array.isArray(d.warnings) ? d.warnings : []) {
    if (!raw || typeof raw !== 'object') continue;
    const code = String(raw.code || 'unknown');
    const meta = WARNING_META[code] || {};
    const detailTask = raw.detail && typeof raw.detail === 'object' ? raw.detail.task_id : null;
    const tone = WARNING_TONE_RU[meta.tone || 'warn'] || 'warn';
    let text = String(raw.message || meta.hint || 'Подробности не переданы.');
    // «Карточка расчёта упала дважды» — технический пересказ; понятная причина
    // и сырой лог живут в «Своде команды» (D7).
    if (code === 'card_failed') text = 'Одна из задач не справилась — подробности ниже.';
    // «next_steps строками, stage словарём» — язык схемы, не человека.
    if (code === 'legacy_metadata') {
      text = 'Шаги, этапы и вопросы прочитаны — ответ можно читать.';
    }
    // «Сделайте то-то» сервер уже пишет в message: оставляем факт, действие — в подсказке.
    if (code === 'outside_room') {
      const fact = text.split(/\s*Переназначьте[\s\S]*$/i)[0].trim();
      if (fact) text = fact;
    }
    push({
      code,
      tone,
      title: meta.title || 'Замечание',
      text,
      hint: meta.hint || '',
      taskId: detailTask ? String(detailTask) : null,
      bannerClass: bannerClassFor(tone),
    });
  }

  // Карточки вне комнаты: сервер иногда отдаёт только флаг в дереве.
  const warnedTasks = new Set(out.filter((w) => w.code === 'outside_room').map((w) => w.taskId).filter(Boolean));
  const outside = (Array.isArray(d.tree) ? d.tree : []).filter((n) => n && n.outside_room);
  for (const node of outside) {
    if (warnedTasks.has(node.id)) continue;
    const assignee = node.assignee ? String(node.assignee) : 'неизвестный профиль';
    push({
      code: 'outside_room',
      tone: 'warn',
      title: 'Задача вне состава комнаты',
      text: `Задачу «${String(node.title || node.id)}» выполняет ${assignee} — он не входит в команду комнаты.`,
      hint: 'Кнопка «Переназначить» — на задаче в дереве работ.',
      taskId: node.id,
      bannerClass: 'banner--warn',
    });
  }

  const dispatcherWarning = d.dispatcher && typeof d.dispatcher === 'object'
    ? String(d.dispatcher.warning || '').trim()
    : '';
  if (dispatcherWarning) {
    push({
      code: 'dispatcher_down',
      tone: 'warn',
      title: 'Движок команды остановлен',
      text: dispatcherWarning,
      hint: 'Пока движок стоит, новые шаги не начнутся сами.',
      taskId: null,
      bannerClass: 'banner--warn',
    });
  }

  return out;
}

/* Понятные причины сбоев — в request-parts-failure.js (реэкспорт). */
export { errorReason, isFailedRun, failureAttempts, failureInfo } from './request-parts-failure.js';

/* ─────────────────────────────────────────────────────────────────────── критик */

const CRITIC_STATE = {
  off: { label: 'нет проверки', tone: 'muted' },
  waiting: { label: 'ещё не начинал', tone: 'muted' },
  reviewing: { label: 'проверяет', tone: 'info' },
  approved: { label: 'принял', tone: 'ok' },
  revised: { label: 'на доработку', tone: 'warn' },
  failed: { label: 'не смог', tone: 'danger' },
  // Режим «до полного одобрения» (W13-E §9): пауза на лимите провайдера и стоп страховки.
  paused: { label: 'пауза', tone: 'warn' },
  stalled: { label: 'остановлено', tone: 'warn' },
  capped: { label: 'потолок раундов', tone: 'warn' },
};

/** Человеческое описание режима проверки. */
const CRITIC_MODE_RU = {
  rounds: 'по числу раундов',
  until_approved: 'до полного одобрения',
};

/**
 * Состояние критика одним объектом: чип, вердикт, раунд, замечания.
 * @param {object} detail RequestDetail
 * @returns {{enabled: boolean, state: string, verdict: string|null,
 *            round: number, maxRounds: number, mode: string, modeLabel: string,
 *            roundCap: number, label: string, tone: string, verdictLabel: string,
 *            taskId: string|null, issues: Array<object>, active: boolean, note: string,
 *            progress: object, stop: object|null, pause: object|null}}
 */
export function criticInfo(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const c = d.critic && typeof d.critic === 'object' ? d.critic : {};
  const state = String(c.state || (c.enabled === false ? 'off' : 'waiting'));
  const meta = CRITIC_STATE[state] || { label: 'нет данных', tone: 'muted' };
  const verdict = c.verdict === 'approve' || c.verdict === 'revise' ? c.verdict : null;
  const issues = Array.isArray(c.issues) ? c.issues.filter((i) => i && typeof i === 'object') : [];
  const round = Number(c.round) || 0;
  const maxRounds = Number(c.max_rounds) || 1;
  const mode = c.mode === 'until_approved' ? 'until_approved' : 'rounds';
  const roundCap = Number(c.round_cap) || Number(c.progress && c.progress.round_cap) || 12;
  const progress = c.progress && typeof c.progress === 'object' ? c.progress : {};
  const stop = c.stopped && typeof c.stopped === 'object' ? c.stopped : null;
  const pause = c.paused && typeof c.paused === 'object' ? c.paused : null;

  let note = '';
  const ready = Number(d.synthesis && d.synthesis.version) > 0;
  if (state === 'off') note = 'Проверка критиком для этого запроса не включалась.';
  else if (state === 'waiting') {
    if (ready) note = 'Свод готов, но критик ещё не запускался — проверку можно запустить вручную.';
    else if (d.status === 'failed') note = 'Проверка не начнётся: запрос остановлен из-за ошибки.';
    else if (d.status === 'needs_you') note = 'Проверка начнётся после вашего ответа — свод ещё не готов.';
    else note = 'Критик запустится, когда будет готов свод.';
  }
  else if (state === 'reviewing') note = 'Свод уже можно читать, но он ещё проверяется.';
  else if (state === 'approved') note = 'Критик принял свод без замечаний.';
  else if (state === 'revised') note = issues.length
    ? `Критик вернул свод с замечаниями: ${issues.length}.`
    : 'Критик вернул свод на доработку.';
  else if (state === 'failed') note = 'Задача критика завершилась ошибкой.';
  else if (state === 'paused') {
    const until = Number(pause && pause.until) || 0;
    const when = until ? new Date(until * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '';
    note = 'У провайдера кончился лимит — ждём и продолжим сами' + (when ? ` (около ${when})` : '') + '.';
  }
  else if (state === 'stalled') note = (stop && stop.message) || 'Цикл остановлен: замечания повторяются.';
  else if (state === 'capped') note = (stop && stop.message) || 'Достигнут потолок раундов — цикл остановлен.';

  return {
    enabled: c.enabled !== false && state !== 'off',
    state,
    verdict,
    round,
    maxRounds,
    mode,
    modeLabel: CRITIC_MODE_RU[mode],
    roundCap,
    label: meta.label,
    tone: meta.tone,
    verdictLabel: verdict === 'approve' ? 'принято' : verdict === 'revise' ? 'на доработку' : '',
    taskId: c.task_id ? String(c.task_id) : null,
    issues,
    active: state === 'reviewing',
    note,
    progress,
    stop,
    pause,
  };
}

/** Подпись раунда для карточки критика: «раунд 2 из 2» или «раунд 3 · до одобрения». */
export function criticRoundLabel(info) {
  const c = info && typeof info === 'object' ? info : {};
  const round = Math.max(Number(c.round) || 0, 0);
  if (c.mode === 'until_approved') {
    return `раунд ${round} · до полного одобрения (потолок ${Number(c.roundCap) || 12})`;
  }
  return `раунд ${round} из ${Number(c.maxRounds) || 1}`;
}

/** Строка хода цикла: «замечаний было 5, закрыто 3, осталось 2». */
export function criticProgressLabel(info) {
  const p = (info && info.progress) || {};
  const before = Number(p.issues_before) || 0;
  const closed = Number(p.closed) || 0;
  const open = Number(p.open) || 0;
  if (!before && !open) return '';
  return `замечаний было ${before}, закрыто ${closed}, осталось ${open}`;
}

/* ──────────────────────────────────────────────────────────────────── артефакты */

const ARTIFACT_KIND = {
  md: { icon: 'markdown', label: 'Markdown' },
  json: { icon: 'json', label: 'JSON' },
  html: { icon: 'globe', label: 'HTML' },
  csv: { icon: 'table', label: 'CSV' },
  image: { icon: 'file-media', label: 'Картинка' },
  pdf: { icon: 'file-pdf', label: 'PDF' },
  docx: { icon: 'file', label: 'Word' },
  xlsx: { icon: 'table', label: 'Excel' },
  pptx: { icon: 'file', label: 'Презентация' },
  other: { icon: 'file', label: 'Файл' },
};

/** Иконка и подпись типа артефакта. */
export function artifactKindMeta(kind) {
  return ARTIFACT_KIND[String(kind || 'other')] || ARTIFACT_KIND.other;
}

/** Размер файла по-русски: '18 КБ', '1,2 МБ', '—'. */
export function formatBytes(size) {
  if (size === null || size === undefined || size === '') return '—';
  const n = Number(size);
  if (!isFinite(n) || n < 0) return '—';
  if (n < 1024) return `${Math.round(n)} Б`;
  const kb = n / 1024;
  if (kb < 1024) return `${kb < 10 ? kb.toFixed(1).replace('.', ',') : Math.round(kb)} КБ`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1).replace('.', ',') : Math.round(mb)} МБ`;
  return `${(mb / 1024).toFixed(1).replace('.', ',')} ГБ`;
}

/* ──────────────────────────────────────────────────────────────────── хронология */

const TIMELINE_KIND = {
  created: { icon: 'sparkle', label: 'запрос создан' },
  decomposed: { icon: 'git-branch', label: 'разложен' },
  promoted: { icon: 'arrow-up', label: 'в очереди' },
  claimed: { icon: 'person', label: 'взял в работу' },
  started: { icon: 'play', label: 'начал' },
  commented: { icon: 'comment', label: 'комментарий' },
  completed: { icon: 'check', label: 'завершил' },
  blocked: { icon: 'lock', label: 'заблокирован' },
  unblocked: { icon: 'key', label: 'разблокирован' },
  failed: { icon: 'error', label: 'ошибка' },
  archived: { icon: 'archive', label: 'в архиве' },
};

/** Иконка и подпись события хронологии. */
export function timelineMeta(kind) {
  return TIMELINE_KIND[String(kind || '')] || { icon: 'circle-outline', label: 'событие' };
}

/**
 * События хронологии по возрастанию времени с флагом «свёрнуто».
 * По умолчанию показываются последние `limit` событий (свежие важнее),
 * первое из них помечается `firstVisible`, чтобы нарисовать подпись.
 *
 * @param {object} detail
 * @param {{limit?: number, expanded?: boolean}} [opts]
 * @returns {Array<{event: object, hidden: boolean, newest: boolean}>}
 */
export function timelineRows(detail, opts = {}) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const limit = Math.max(1, Number(opts.limit) || 6);
  const events = (Array.isArray(d.timeline) ? d.timeline : [])
    .filter((e) => e && typeof e === 'object')
    .slice()
    .sort((a, b) => (Number(a.at) || 0) - (Number(b.at) || 0));
  const expanded = opts.expanded === true || events.length <= limit;
  const from = expanded ? 0 : Math.max(0, events.length - limit);
  return events.map((event, i) => ({
    event,
    hidden: i < from,
    newest: i === events.length - 1,
  })).filter((r) => !r.hidden);
}
