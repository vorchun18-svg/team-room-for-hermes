/**
 * limits-chip.js — компактный индикатор расхода в шапке (W29-P9).
 *
 * Одна строка: «5 ч: 645 тыс. · 84 %». Цвет — по худшему лимиту (70 % / 90 %).
 * Цифры берутся из `GET /api/limits` (server/limits.py); клиент только
 * показывает серверную строку `chip`, поэтому шапка и панель не расходятся.
 *
 * Правила:
 *  · нет данных — чипа нет вовсе (шапка не должна пугать прочерком);
 *  · ошибка сети — тихий чип «расход: нет связи», нажатие ведёт в настройки;
 *  · нажатие открывает раздел «Расход и лимиты», а не отдельный экран:
 *    у настройки одно место, искать её по меню человек не должен.
 */

import { h } from './util.js';
import { icon } from './icons.js';

/** Как часто обновляем чип: расход меняется минутами, а не секундами. */
export const REFRESH_MS = 60000;

const state = { data: null, error: null, at: 0, inflight: null };

/** Текст чипа: серверная строка, иначе короткая сборка. */
export function chipText(payload) {
  const data = payload || {};
  const line = String(data.chip || '').trim();
  if (line) return line;
  const five = (data.windows && data.windows['5h']) || {};
  const total = Number((five.tokens || {}).total) || 0;
  const worst = (five.summary || {}).worst_limit
    || ((data.windows && data.windows.week && data.windows.week.summary) || {}).worst_limit;
  const tokens = total >= 1e6 ? (Math.round(total / 1e5) / 10).toFixed(1).replace('.', ',') + ' млн'
    : total >= 1e3 ? String(Math.round(total / 1e3)) + ' тыс.' : String(Math.round(total));
  return '5 ч: ' + tokens + (worst && worst.pct ? ' · ' + Math.round(worst.pct) + ' %' : '');
}

/** Тон чипа: по худшему лимиту (70 % — жёлтый, 90 % — красный). */
export function chipTone(payload) {
  const candidates = [];
  for (const key of ['5h', 'week']) {
    const window = (payload && payload.windows && payload.windows[key]) || {};
    const worst = (window.summary || {}).worst_limit;
    if (worst && worst.pct) candidates.push(worst);
    for (const row of window.providers || []) {
      if (row && row.limit && row.limit.pct) candidates.push(row.limit);
    }
  }
  if (!candidates.length) return 'ok';
  const worst = candidates.reduce((a, b) => (Number(b.pct) || 0) > (Number(a.pct) || 0) ? b : a);
  const status = String(worst.status || '');
  if (status === 'over' || status === 'alert') return 'alert';
  if (status === 'warn') return 'warn';
  const pct = Number(worst.pct) || 0;
  if (pct >= 90) return 'alert';
  if (pct >= 70) return 'warn';
  return 'ok';
}

/** Подсказка: полная строка расхода + куда ведёт нажатие. */
export function chipTitle(payload, error) {
  if (error) return 'Расход сейчас недоступен. Открыть «Расход и лимиты»';
  const line = String((payload && payload.line) || '').trim();
  return (line ? line + '. ' : '') + 'Открыть «Расход и лимиты»';
}

/** Подпись чипа для ключа перерисовки шапки: текст + тон. */
export function chipSignature() {
  if (!state.data) return state.error ? 'alert|' : '';
  return chipText(state.data) + '|' + chipTone(state.data);
}

/** Загрузить расход (не чаще REFRESH_MS); после обновления перерисовывает чипы. */
export function refreshLimits(api, { force = false } = {}) {
  const now = Date.now();
  if (!force && state.data && now - state.at < REFRESH_MS) return Promise.resolve(state.data);
  if (state.inflight) return state.inflight;
  if (!api || typeof api.get !== 'function') return Promise.resolve(null);
  state.inflight = Promise.resolve(api.get('/api/limits'))
    .then((payload) => {
      state.data = payload || null;
      state.error = null;
      state.at = Date.now();
      return state.data;
    })
    .catch((err) => {
      state.error = err || { message: 'нет связи' };
      state.at = Date.now();
      return null;
    })
    .finally(() => {
      state.inflight = null;
      rerenderLimitsChips();
    });
  return state.inflight;
}

/** Перерисовать все чипы на странице (шапка пересоздаётся при навигации). */
export function rerenderLimitsChips() {
  if (typeof document === 'undefined' || !document.querySelectorAll) return;
  for (const node of document.querySelectorAll('[data-limits-chip]')) {
    const payload = state.data;
    const tone = payload ? chipTone(payload) : 'ok';
    node.className = 'chip lim-chip lim-chip--' + tone;
    node.setAttribute('title', chipTitle(payload, state.error));
    if (node.firstChild && node.firstChild.nodeType === 1 && node.firstChild.classList
      && node.firstChild.classList.contains('lim-chip__text')) {
      node.firstChild.textContent = payload ? chipText(payload) : 'расход: нет связи';
    }
  }
}

/**
 * Чип для шапки. Возвращает `null`, если показывать нечего (данных ещё нет
 * и ошибки тоже нет — первый кадр после запуска).
 * @param {{navigate?: Function}} [ctx]
 * @returns {HTMLElement|null}
 */
export function limitsChip(ctx = {}) {
  const payload = state.data;
  if (!payload && !state.error) return null;
  const text = payload ? chipText(payload) : 'расход: нет связи';
  const tone = payload ? chipTone(payload) : 'alert';
  const go = (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    if (typeof ctx.navigate === 'function') ctx.navigate('#/settings');
    setTimeout(() => {
      try {
        const host = document.getElementById('set-limits');
        if (host && typeof host.scrollIntoView === 'function') host.scrollIntoView({ block: 'start' });
      } catch { /* ignore */ }
    }, 260);
  };
  return h('a', {
    class: 'chip lim-chip lim-chip--' + tone,
    href: '#/settings',
    'data-limits-chip': '1',
    role: 'link',
    title: chipTitle(payload, state.error),
    'aria-label': 'Расход токенов: ' + text + '. Открыть «Расход и лимиты»',
    onclick: go,
  }, h('span', { class: 'lim-chip__text' }, text));
}

export default { limitsChip, refreshLimits, rerenderLimitsChips, chipText, chipTone };
