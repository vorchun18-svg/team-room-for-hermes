/**
 * request-util.js — чистые помощники страницы запроса (без состояния и без mount).
 * Общие для request.js, request-header.js, request-answer.js и request-aside.js.
 */

import { h } from '../util.js';
import { sectionHeader, errorBanner, copyText } from '../ui.js';
import { icon } from '../icons.js';
import { errorText } from '../text-ru.js';
import { participantLabel } from '../participant-label.js';

/** Первая непустая строка текста, обрезанная до 140 символов. */
export function firstLine(text, max = 140) {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  return s.length > max ? s.slice(0, max - 1).trimEnd() + '…' : s;
}

/**
 * Короткое имя исполнителя для текста: «biz-ops» → «операции», id — в title.
 * Английские слаги профилей — служебные, поэтому переводим их в роль.
 * @param {string} p
 * @returns {string}
 */
const ROLE_SLUG_RU = {
  director: 'руководитель комнаты', ops: 'операции', cfo: 'экономика', marketing: 'маркетинг',
  strategy: 'стратегия', lawyer: 'юрист', critic: 'критик', runner: 'раннер',
  coder: 'программист', surveyor: 'исследователь', analyst: 'аналитик', default: 'руководитель комнаты',
};

export function personName(p) {
  const s = String(p == null ? '' : p).trim();
  if (!s) return '';
  if (s === 'mikhail') return 'Михаил';
  const slug = s.replace(/^(?:biz|room)-/, '');
  return ROLE_SLUG_RU[slug] || slug;
}

/** Подпись руководителя комнаты: роль не повторяем, если профиль так и называется. */
export function directorLabel(p) {
  const name = personName(p);
  return (name === 'руководитель комнаты' || name === 'director' || name === 'default')
    ? 'руководитель комнаты'
    : 'руководитель комнаты · ' + name;
}

/**
 * Подпись исполнителя для текста: короткая роль из состава комнаты
 * («Операции: поставки…» → «Операции»), иначе — имя профиля без префикса.
 * id остаётся в title. Без комнаты работает как personName.
 * @param {string} profile
 * @param {object} [room]
 * @returns {string}
 */
export function profileName(profile, room) {
  const raw = String(profile === null || profile === undefined ? '' : profile).trim();
  if (!raw) return '';
  if (raw === 'system') return 'Система';
  if (raw === 'mikhail') return 'Михаил';
  // WAVE13 §11: одна подпись везде — имя из описания профиля («Анна»), иначе
  // прежнее правило (подпись роли из комнаты, затем слаг → русское слово).
  const label = participantLabel(raw, { room });
  if (label.source !== 'role') return label.short;
  const members = room && Array.isArray(room.members) ? room.members : [];
  const hit = members.find((m) => m && String(m.profile || m.id || '') === raw);
  const note = hit ? String(hit.note || '').split(':')[0].trim() : '';
  if (note && note.length <= 24) return note;
  const name = personName(raw);
  return name === 'director' || name === 'default' ? 'руководитель комнаты' : name;
}

/** critic в детали — объект, в старых ответах — bool. Приводим к объекту. */
export function criticOf(d) {
  const c = d && d.critic;
  if (c && typeof c === 'object') return c;
  return {
    enabled: Boolean(c), round: 0, max_rounds: 0, state: 'off',
    verdict: null, issues: [], task_id: null,
  };
}

/** synthesis с безопасными значениями по умолчанию. */
export function synthesisOf(d) {
  const s = d && d.synthesis;
  return s && typeof s === 'object' ? s : { state: 'none', text_md: '', history: [], legacy: false };
}

/** История версий; для legacy-ответа синтезируем одну запись. */
export function versionsOf(s) {
  const hist = Array.isArray(s.history) ? s.history.filter((v) => v && v.version != null) : [];
  if (hist.length) return hist.slice().sort((a, b) => a.version - b.version);
  if (s.state === 'ready') {
    return [{
      version: s.version || 1, kind: 'initial', task_id: s.source_task || null,
      author: s.author || null, at: s.at || null, verdict: null, note: '',
    }];
  }
  return [];
}

export function latestVersion(s) {
  const list = versionsOf(s);
  return list.length ? list[list.length - 1].version : 0;
}

/** Безопасный вызов блока из параллельного модуля. */
export function safePart(name, fn, ...args) {
  if (typeof fn !== 'function') return null;
  try {
    const el = fn(...args);
    return el && el.nodeType === 1 ? el : null;
  } catch (err) {
    console.warn('[request] блок ' + name + ' не отрисован:', err);
    return null;
  }
}

/**
 * Заголовок карточки: название и счётчик. Счётчик можно передать строкой
 * («3 шага», «2 вопроса») — тогда существительное уже согласовано с числом
 * (D18: числа без существительных читаются как огрызки).
 * @param {string} title
 * @param {number|string} [count]
 * @param {{actions?: HTMLElement|HTMLElement[]}} [opts]
 * @returns {HTMLElement}
 */
export function blockHead(title, count, opts = {}) {
  const o = opts || {};
  if (count === null || count === undefined || count === '' || count === 0 || count === '0') {
    // без счётчика — обычный sectionHeader (он же даёт aria и стили)
    if (!(Array.isArray(o.actions) ? o.actions.length : o.actions)) {
      try {
        const el = sectionHeader(title);
        if (el && el.nodeType === 1) return el;
      } catch (err) { console.warn('[request] sectionHeader:', err); }
    }
  }
  if (typeof count === 'number' && count > 0 && !o.actions) {
    try {
      const el = sectionHeader(title, { count });
      if (el && el.nodeType === 1) return el;
    } catch (err) { console.warn('[request] sectionHeader:', err); }
  }
  const left = h('div', { class: 'row row--tight section-header__main' },
    h('h2', { class: 'section-header__title' }, String(title === null || title === undefined ? '' : title)));
  const text = typeof count === 'string' ? count.trim() : (count ? String(count) : '');
  if (text) left.appendChild(h('span', { class: 'chip chip--muted section-header__count tnum' }, text));
  const right = h('div', { class: 'row row--tight section-header__actions' });
  for (const a of (Array.isArray(o.actions) ? o.actions : (o.actions ? [o.actions] : []))) if (a) right.appendChild(a);
  const el = h('div', { class: 'section-header row row--between' }, left);
  if (right.childNodes.length) el.appendChild(right);
  return el;
}

/**
 * «Подробности для разработчика»: сырой текст ошибки спрятан, но не потерян.
 * @param {Array<{label?: string, text: string, code?: string, copy?: boolean, toast?: Function}>} rows
 * @param {string} [summary]
 * @returns {HTMLElement|null}
 */
export function devDetails(rows, summary = 'Подробности для разработчика') {
  const list = (Array.isArray(rows) ? rows : []).filter((r) => r && String(r.text || '').trim());
  if (!list.length) return null;
  const det = h('details', { class: 'req-dev' },
    h('summary', { class: 'req-dev__summary' },
      icon('code', { size: 12 }),
      h('span', {}, summary)));
  const body = h('div', { class: 'req-dev__body' });
  for (const row of list) {
    const line = h('div', { class: 'req-dev__line' },
      h('div', { class: 'req-dev__text mono small' }, String(row.text)));
    if (row.copy) {
      line.appendChild(h('button', {
        class: 'btn btn--ghost btn--sm btn--icon req-dev__copy',
        type: 'button',
        title: 'Скопировать',
        'aria-label': 'Скопировать',
        onclick: async () => {
          const ok = await copyText(String(row.text));
          if (typeof row.toast === 'function') row.toast(ok ? 'Скопировано' : 'Не удалось скопировать', ok);
        },
      }, icon('copy', { size: 12 })));
    }
    body.appendChild(h('div', { class: 'req-dev__row' },
      row.label ? h('div', { class: 'req-dev__label small muted' }, row.label) : null,
      line,
      row.code ? h('div', { class: 'req-dev__code mono small muted' }, String(row.code)) : null));
  }
  det.appendChild(body);
  return det;
}

/**
 * Баннер ошибки страницы запроса: текст — по-русски, коды (not_found и пр.)
 * не показываем, только 'network' — чтобы осталась подсказка про запуск.
 * @param {*} err
 * @param {Function} [onRetry]
 * @returns {HTMLElement}
 */
export function requestErrorBanner(err, onRetry) {
  const t = errorText(err);
  if (t.code !== 'network' && err && err.code) console.warn('[request] код ошибки:', err.code);
  return errorBanner({ message: t.message, code: t.code }, onRetry);
}

/**
 * Тост об ошибке — но не хором с баннером: если на экране уже висит тот же
 * текст (обрыв связи), второй раз не повторяем (D11).
 * @param {object} ctx
 * @param {*} err
 * @param {string} [fallback]
 */
export function errorToast(ctx, err, fallback = 'Не удалось обновить данные') {
  if (!ctx || typeof ctx.toast !== 'function') return;
  const message = errorText(err).message || fallback;
  if (typeof document !== 'undefined') {
    const shown = Array.from(document.querySelectorAll('.banner--error'))
      .map((n) => String(n.innerText || n.textContent || ''));
    if (shown.some((t) => t.includes(message))) return;
  }
  ctx.toast(message, 'danger');
}

/**
 * Обернуть таблицы ответа в прокручиваемый контейнер: широкая таблица
 * скроллится внутри ответа, а не растягивает страницу.
 * @param {HTMLElement} host
 */
export function wrapTables(host) {
  if (!host || typeof host.querySelectorAll !== 'function') return;
  for (const table of Array.from(host.querySelectorAll('table'))) {
    if (!table.parentNode || (table.closest && table.closest('.req-table'))) continue;
    const wrap = h('div', { class: 'req-table', tabindex: '0', role: 'region', 'aria-label': 'Таблица ответа' });
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
  }
}
