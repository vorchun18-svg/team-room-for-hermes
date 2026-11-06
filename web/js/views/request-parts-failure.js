/**
 * request-parts-failure.js — понятные объяснения сбоев страницы запроса.
 *
 * Чистый модуль без DOM: технический текст движка («timeout after 1800s»,
 * «worker exited with code 1», «KeyError('x')») превращается в причину
 * по-русски, а оригинал остаётся в details для «Подробностей для разработчика».
 * Реэкспортируется из request-parts-model.js, чтобы страница импортировала
 * один модуль.
 */

import { plainTerms, countNoun } from '../text-ru.js';

/* ──────────────────────────────────────────────────────────── поломки и коды */

/** Секунды → «30 мин» / «45 с» (для «превышено время ожидания»). */
function durationRu(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return `${s} с`;
  return `${Math.round(s / 60)} мин`;
}

/**
 * Правила разбора технического текста ошибки. score — насколько объяснение
 * конкретное: поле в данных важнее, чем код выхода процесса.
 */
const REASON_RULES = [
  {
    score: 4,
    re: /KeyError\(\s*['"]?([A-Za-zА-Яа-я0-9_.\-]+)['"]?\s*\)/i,
    make: (m) => `в данных не нашлось поле «${m[1]}»`,
  },
  { score: 4, re: /no space left on device|errno\s*28/i, make: () => 'на диске закончилось место' },
  {
    score: 3,
    re: /timeout after\s+(\d+(?:[.,]\d+)?)\s*s\b/i,
    make: (m) => `превышено время ожидания (${durationRu(String(m[1]).replace(',', '.'))})`,
  },
  { score: 3, re: /timeout|timed out|истекло время/i, make: () => 'превышено время ожидания' },
  {
    score: 2,
    re: /worker exited with code\s+(\d+)|завершился с кодом\s+(\d+)|exit\s+(\d+)/i,
    make: (m) => `исполнитель завершился с ошибкой (код ${m[1] || m[2] || m[3]})`,
  },
  { score: 1, re: /crashed|аварийн/i, make: () => 'исполнитель аварийно завершился' },
];

/**
 * Технический текст ошибки → понятная причина по-русски.
 * Возвращает пустую строку, если объяснить нечего.
 * @param {*} raw
 * @returns {string}
 */
export function errorReason(raw) {
  const found = reasonDetailed(raw);
  return found ? found.text : '';
}

function reasonDetailed(raw) {
  const s = String(raw === null || raw === undefined ? '' : raw);
  let best = null;
  for (const rule of REASON_RULES) {
    const m = rule.re.exec(s);
    if (!m) continue;
    if (!best || rule.score > best.score) best = { text: rule.make(m), score: rule.score };
  }
  return best;
}

const RUN_FAILED = new Set(['failed', 'crashed', 'error']);

/** Попытка исполнителя закончилась неудачей. */
export function isFailedRun(run) {
  if (!run || typeof run !== 'object') return false;
  return RUN_FAILED.has(String(run.status || '')) || String(run.outcome || '') === 'failed';
}

/** Сколько раз подряд не получилось: по попыткам в дереве, иначе по тексту. */
export function failureAttempts(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  let max = 0;
  for (const node of Array.isArray(d.tree) ? d.tree : []) {
    const runs = node && Array.isArray(node.runs) ? node.runs : [];
    const n = runs.filter(isFailedRun).length;
    if (n > max) max = n;
  }
  if (max) return max;
  const raw = [
    d.status_reason,
    ...(Array.isArray(d.warnings) ? d.warnings.map((w) => w && w.message) : []),
  ].filter(Boolean).join(' ');
  if (/дважды|2\s+раз/i.test(raw)) return 2;
  if (/трижды|3\s+раз/i.test(raw)) return 3;
  const m = /(\d+)\s*(?:раз|попыт)/i.exec(raw);
  return m ? Math.max(1, Number(m[1])) : 0;
}

/**
 * Понятное объяснение сбоя запроса: «Исполнитель не справился (2 попытки).
 * Причина: в данных не нашлось поле «cost_packaging».» Сырые тексты — в
 * details, их показывает только «Подробности для разработчика».
 *
 * @param {object} detail RequestDetail
 * @returns {{attempts: number, reason: string, text: string,
 *            details: Array<{label: string, text: string, code: string}>}}
 */
export function failureInfo(detail) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const nodes = (Array.isArray(d.tree) ? d.tree : []).filter(Boolean);
  const failed = nodes.filter((n) => n.error || isFailedRun({ status: n.status }));

  const raw = [];
  const warn = (Array.isArray(d.warnings) ? d.warnings : []).find((w) => w && w.code === 'card_failed');
  if (warn && warn.message) raw.push(String(warn.message));
  if (d.status_reason) raw.push(String(d.status_reason));
  for (const node of failed) {
    if (node.error && node.error.message) raw.push(String(node.error.message));
    for (const run of Array.isArray(node.runs) ? node.runs : []) {
      if (run && run.error) raw.push(String(run.error));
    }
  }

  let reason = '';
  let best = 0;
  for (const text of raw) {
    const found = reasonDetailed(text);
    if (found && found.score > best) { reason = found.text; best = found.score; }
  }

  const attempts = failureAttempts(d);
  const head = 'Исполнитель не справился'
    + (attempts ? ` (${countNoun(attempts, ['попытка', 'попытки', 'попыток'])})` : '');
  const text = reason
    ? `${head}. Причина: ${reason}.`
    : `${head}: работа остановилась.`;

  const details = [];
  const seen = new Set();
  const push = (label, value, code = '') => {
    const t = String(value === null || value === undefined ? '' : value).trim();
    if (!t || seen.has(t)) return;
    seen.add(t);
    details.push({ label, text: t, code: code ? String(code) : '' });
  };
  if (d.status_reason) push('Сообщение движка', d.status_reason);
  for (const node of failed) {
    if (!node.error) continue;
    push(`Задача «${String(node.title || node.id)}»`, node.error.message || node.error.code, node.error.code);
  }
  for (const node of failed) {
    for (const run of Array.isArray(node.runs) ? node.runs : []) {
      if (run && run.error) push(run.id != null ? `Попытка № ${run.id}` : 'Попытка', run.error);
    }
  }

  return { attempts, reason, text, details };
}

/**
 * Список попыток для плашки ошибки (B-24): «3 попытки: …» — каждая строка с временем
 * и понятной причиной. Технический текст остаётся в `details` (для разработчика).
 *
 * @param {object} detail RequestDetail
 * @param {{limit?: number}} [opts]
 * @returns {Array<{n: number, at: number, profile: string, reason: string, failed: boolean}>}
 */
export function failureAttemptsList(detail, opts = {}) {
  const d = detail && typeof detail === 'object' ? detail : {};
  const limit = Math.max(1, Number(opts.limit) || 6);
  const rows = [];
  for (const node of Array.isArray(d.tree) ? d.tree : []) {
    const runs = node && Array.isArray(node.runs) ? node.runs : [];
    runs.forEach((run, i) => {
      if (!run || typeof run !== 'object') return;
      const failed = isFailedRun(run);
      const text = String(run.error || '');
      const reason = text ? errorReason(text) : (failed ? 'исполнитель не справился' : '');
      if (!failed && !reason) return;
      rows.push({
        n: Number(run.id) || (i + 1),
        at: Number(run.started_at || run.ended_at || 0) || 0,
        profile: String(run.profile || (node && node.assignee) || ''),
        reason: reason || 'исполнитель не справился',
        failed,
        taskId: String((node && node.id) || ''),
      });
    });
  }
  rows.sort((a, b) => (a.at || 0) - (b.at || 0));
  return rows.slice(-limit).map((row, i) => Object.assign({}, row, { n: i + 1 }));
}
