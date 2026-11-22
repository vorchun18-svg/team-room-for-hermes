/**
 * limits.js — панель «Расход и лимиты» (W29-P9).
 *
 * Что показывает (один экран, без переходов):
 *  · расход за 5 часов / сутки / неделю по провайдерам и моделям из `state.db`
 *    профилей Hermes: вызовы, токены (ввод, кэш, вывод), оценка стоимости;
 *  · ПОЛЬЗОВАТЕЛЬСКИЕ лимиты: у провайдера — токены или вызовы за 5 ч и за неделю
 *    (по умолчанию пусто); прогресс-полосы и предупреждения на 70 % и 90 %;
 *  · прогноз «хватит ли» по среднему расходу последних запусков;
 *  · топ дорогих запросов со ссылкой на запрос и строку «потрачено зря»
 *    (запуски на «не той модели»).
 *
 * Цифры считает сервер (`GET /api/limits`, server/limits.py) — клиент ничего
 * не додумывает, кроме подписей. Сеть и модели не вызываются: только чтение.
 * Пустой экран — не «пока ничего нет», а объяснение и кнопка «Обновить».
 */

import { h, plural } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, sectionHeader, skeleton, errorBanner } from '../ui.js';

const VIEW_CSS = 'css/view-limits.css';
const WINDOWS = [
  { key: '5h', label: '5 часов', short: '5 ч' },
  { key: 'day', label: 'Сутки', short: 'сутки' },
  { key: 'week', label: 'Неделя', short: 'неделя' },
];
const KIND_OPTIONS = [
  { value: 'tokens', label: 'токены' },
  { value: 'calls', label: 'вызовы' },
];

/* ── чистые функции (проверяются node --test) ─────────────────────────────── */

/** Короткое число: 1 234 567 → «1,2 млн». */
export function tokensText(value) {
  const n = Math.abs(Number(value) || 0);
  const sign = (Number(value) || 0) < 0 ? '−' : '';
  if (n >= 1e9) return sign + (Math.round(n / 1e8) / 10).toFixed(1).replace('.', ',').replace(',0', '') + ' млрд';
  if (n >= 1e6) return sign + (Math.round(n / 1e5) / 10).toFixed(1).replace('.', ',').replace(',0', '') + ' млн';
  if (n >= 1e3) return sign + String(Math.round(n / 1e3)) + ' тыс.';
  return sign + String(Math.round(n));
}

/** Деньги: 12.9639 → «$12,96». */
export function moneyText(value) {
  const n = Number(value) || 0;
  if (n && Math.abs(n) < 0.01) return '$' + n.toFixed(3).replace('.', ',');
  return '$' + n.toFixed(2).replace('.', ',');
}

/** Процент лимита: 83.53 → «84 %». */
export function pctText(value) {
  const n = Number(value) || 0;
  if (!n) return '0 %';
  return (n >= 10 ? String(Math.round(n)) : String(Math.round(n * 10) / 10).replace('.', ',')) + ' %';
}

/** Тон прогресса: ok / warn (70 %) / alert (90 %) / over (100 %). */
export function progressTone(limit) {
  const status = limit && limit.status ? String(limit.status) : '';
  if (status === 'over' || status === 'alert' || status === 'warn' || status === 'ok') return status;
  const pct = Number(limit && limit.pct) || 0;
  if (pct >= 100) return 'over';
  if (pct >= 90) return 'alert';
  if (pct >= 70) return 'warn';
  return 'ok';
}

/** Подпись лимита словами: «600 тыс. токенов за 5 часов · 84 %». */
export function limitText(limit, windowKey) {
  if (!limit) return 'лимит не задан';
  const meta = WINDOWS.find((w) => w.key === windowKey) || { label: windowKey };
  const unit = String(limit.kind) === 'calls' ? 'вызовов' : 'токенов';
  return tokensText(limit.value) + ' ' + unit + ' ' + meta.label.toLowerCase() + ' · ' + pctText(limit.pct);
}

/** Предупреждение словами для 70 % / 90 % / перерасхода. */
export function limitWarning(limit, providerTitle) {
  if (!limit) return '';
  const tone = progressTone(limit);
  const who = String(providerTitle || '');
  if (tone === 'over') return 'Лимит исчерпан: ' + who + ' — новые запуски упрутся в потолок';
  if (tone === 'alert') return 'Почти весь лимит: ' + who + ' — ' + pctText(limit.pct) + ' израсходовано';
  if (tone === 'warn') return 'Больше 70 % лимита: ' + who + ' — ' + pctText(limit.pct) + ' израсходовано';
  return '';
}

/** Итоговая строка окна: «46 вызовов · 645 тыс. токенов · $1,28». */
export function windowSummary(summary) {
  const s = summary || {};
  return [
    String(Number(s.calls) || 0) + ' ' + plural(Number(s.calls) || 0, ['вызов', 'вызова', 'вызовов']),
    tokensText(s.tokens) + ' токенов',
    moneyText(s.cost_usd),
  ].join(' · ');
}

/** Строка «зря потрачено» — с сервера, иначе честная заглушка. */
export function wastedText(wasted) {
  const w = wasted || {};
  const text = String(w.text || '').trim();
  if (text) return text;
  if (!Number(w.attempts)) return 'Потрачено зря: нет — перезапусков на другой модели не было';
  return 'Потрачено зря: ' + String(w.attempts) + ' ' +
    plural(Number(w.attempts) || 0, ['запуск', 'запуска', 'запусков']) + ' на ' +
    tokensText(w.tokens) + ' токенов (' + moneyText(w.cost_usd) + ')';
}

/** Черновик лимитов из ответа сервера — то, что видно в полях. */
export function draftFromLimits(limits) {
  const providers = (limits && limits.providers) || {};
  const draft = {};
  for (const [key, item] of Object.entries(providers)) {
    draft[key] = {};
    for (const w of ['5h', 'week']) {
      const limit = item && item[w];
      draft[key][w] = limit && limit.value
        ? { kind: String(limit.kind || 'tokens'), value: String(limit.value) }
        : { kind: 'tokens', value: '' };
    }
  }
  return draft;
}

/** Черновик полей → тело запроса. Пустое значение означает «лимита нет». */
export function limitsPayload(draft) {
  const providers = {};
  for (const [key, item] of Object.entries(draft || {})) {
    const row = {};
    for (const w of ['5h', 'week']) {
      const value = Number(String((item && item[w] && item[w].value) || '').replace(/\s+/g, '').replace(',', '.'));
      if (value > 0) row[w] = { kind: String((item[w] && item[w].kind) || 'tokens'), value: Math.round(value) };
    }
    if (Object.keys(row).length) providers[key] = row;
  }
  return { providers };
}

/** Список провайдеров для редактора: из данных и из уже заданных лимитов. */
export function providerList(payload) {
  const out = [];
  const seen = new Set();
  const add = (key, title) => {
    const id = String(key || '');
    if (seen.has(id)) return;
    seen.add(id);
    out.push({ key: id, title: String(title || id || 'не указан') });
  };
  for (const row of ((payload && payload.windows && payload.windows.week && payload.windows.week.providers) || [])) {
    add(row.provider, row.title);
  }
  for (const key of Object.keys((payload && payload.limits && payload.limits.providers) || {})) {
    if (!seen.has(key)) add(key, key);
  }
  return out;
}

/* ── экран ────────────────────────────────────────────────────────────────── */

/**
 * Показать панель в готовом контейнере.
 * @param {HTMLElement} host
 * @param {{api?: object, toast?: Function, navigate?: Function}} [ctx]
 * @returns {Promise<{destroy: Function, load: Function}>}
 */
export async function mountLimits(host, ctx = {}) {
  if (!host) return { destroy() {}, load() {} };
  await ensureCss(VIEW_CSS);
  const api = ctx.api || null;
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  let stopped = false;
  let data = null;
  let error = null;
  let windowKey = '5h';
  let draft = {};
  let busy = false;

  host.replaceChildren();
  host.appendChild(sectionHeader('Расход и лимиты'));
  host.appendChild(h('p', { class: 'set-group__hint small muted' },
    'Сколько вызывали модели и сколько это стоило — по провайдерам, за 5 часов, сутки и неделю. '
      + 'Здесь же ваши лимиты: приложение предупредит на 70 % и 90 %, а не после перерасхода. '
      + 'Данные читаются из журнала Hermes только для просмотра — приложение ничего не отправляет в сеть.'));

  const root = h('div', { class: 'lim' });
  host.appendChild(root);

  const head = h('div', { class: 'lim__head' });
  const body = h('div', { class: 'lim__body' });
  const edit = h('div', { class: 'lim__edit' });
  root.appendChild(head);
  root.appendChild(body);
  root.appendChild(edit);

  function load() {
    if (!api || typeof api.get !== 'function') {
      error = { message: 'Нет связи с приложением' };
      render();
      return Promise.resolve();
    }
    body.replaceChildren(skeleton(3));
    return Promise.resolve(api.get('/api/limits'))
      .then((payload) => {
        if (stopped) return;
        data = payload || null;
        error = null;
        draft = draftFromLimits(data && data.limits);
        render();
      })
      .catch((err) => {
        if (stopped) return;
        error = err || { message: 'Не удалось получить расход' };
        render();
      });
  }

  function windowSwitcher() {
    const tabs = h('div', { class: 'lim__tabs', role: 'tablist', 'aria-label': 'Период расхода' });
    for (const w of WINDOWS) {
      tabs.appendChild(h('button', {
        class: 'lim__tab' + (w.key === windowKey ? ' is-active' : ''),
        type: 'button', role: 'tab', 'data-window': w.key,
        'aria-selected': w.key === windowKey ? 'true' : 'false',
        onclick: () => { windowKey = w.key; render(); },
      }, w.label));
    }
    return tabs;
  }

  function progressBar(limit) {
    if (!limit) return h('span', { class: 'lim__no-limit small muted' }, 'лимит не задан');
    const tone = progressTone(limit);
    const pct = Math.max(0, Math.min(100, Number(limit.pct) || 0));
    return h('div', { class: 'lim__limit lim__limit--' + tone, 'data-tone': tone },
      h('div', { class: 'lim__bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
        'aria-valuenow': String(Math.round(pct)), 'aria-label': limitText(limit, windowKey) },
      h('span', { class: 'lim__bar-fill', style: 'width:' + pct + '%' })),
      h('span', { class: 'lim__limit-text small' }, limitText(limit, windowKey)));
  }

  function providerRows(window) {
    const rows = (window && window.providers) || [];
    if (!rows.length) {
      return h('div', { class: 'lim__empty' },
        h('p', {}, 'За этот период запусков не было.'),
        h('p', { class: 'small muted' },
          'Когда команда начнёт работать, здесь появятся вызовы, токены и стоимость по провайдерам.'),
        h('button', { class: 'btn btn--secondary btn--sm', type: 'button', onclick: () => load() },
          icon('refresh', { size: 12 }), 'Обновить'));
    }
    const table = h('div', { class: 'lim__table', role: 'table' });
    table.appendChild(h('div', { class: 'lim__tr lim__tr--head', role: 'row' },
      h('span', { role: 'columnheader' }, 'Провайдер и модели'),
      h('span', { role: 'columnheader' }, 'Вызовы'),
      h('span', { role: 'columnheader' }, 'Токены (ввод / кэш / вывод)'),
      h('span', { role: 'columnheader' }, 'Стоимость'),
      h('span', { role: 'columnheader' }, 'Лимит')));
    for (const row of rows) {
      const tokens = row.tokens || {};
      const models = (row.models || []).slice(0, 3).map((m) => m.model + ' — ' + tokensText(m.calls) + ' выз.')
        .join('; ');
      table.appendChild(h('div', { class: 'lim__tr', role: 'row', 'data-provider': row.provider },
        h('div', { class: 'lim__who', role: 'cell' },
          h('span', { class: 'lim__provider' }, row.title || row.provider || 'не указан'),
          models ? h('span', { class: 'lim__models small muted', title: models }, models) : null),
        h('span', { class: 'lim__num tnum', role: 'cell' }, String(Number(row.calls) || 0)),
        h('span', { class: 'lim__num tnum', role: 'cell' },
          tokensText(tokens.input) + ' / ' + tokensText(tokens.cache_read) + ' / ' + tokensText(tokens.output)),
        h('span', { class: 'lim__num tnum', role: 'cell' }, moneyText(row.cost_usd)),
        h('div', { class: 'lim__cell-limit', role: 'cell' }, progressBar(row.limit))));
      const warn = limitWarning(row.limit, row.title || row.provider);
      if (warn) {
        table.appendChild(h('div', { class: 'lim__warn-row', role: 'row' },
          h('div', { class: 'banner banner--warn lim__banner' }, icon('warning', { size: 13 }),
            h('div', { class: 'banner__text' }, warn))));
      }
    }
    return table;
  }

  function forecastBlock() {
    const fc = (data && data.forecast && data.forecast[windowKey]) || {};
    const items = Array.isArray(fc.items) ? fc.items : [];
    const text = String(fc.text || '').trim();
    if (!items.length) {
      return h('div', { class: 'lim__forecast lim__forecast--quiet' },
        h('div', { class: 'lim__forecast-title' }, icon('pulse', { size: 13 }), 'Хватит ли'),
        h('p', { class: 'small muted' }, text || 'Лимиты не заданы — прогноз появится, когда вы зададите потолок.'));
    }
    const box = h('div', { class: 'lim__forecast' },
      h('div', { class: 'lim__forecast-title' }, icon('pulse', { size: 13 }), 'Хватит ли'));
    for (const item of items) {
      box.appendChild(h('p', { class: 'lim__forecast-line' + (progressTone({ pct: item.pct }) === 'over' ? ' is-bad' : '') },
        h('strong', {}, item.provider_title || item.provider || ''),
        ' — ', String(item.text || ''),
        h('span', { class: 'small muted' }, ' (осталось ' + tokensText(item.left) + ' ' +
          (String(item.kind) === 'calls' ? 'вызовов' : 'токенов') + ')')));
    }
    if (text && items.length > 1) box.appendChild(h('p', { class: 'small muted' }, text));
    return box;
  }

  function topBlock() {
    const list = (data && data.top_requests) || [];
    const box = h('div', { class: 'lim__top' },
      h('div', { class: 'lim__section-title' }, 'Самые дорогие запросы'));
    if (!list.length) {
      box.appendChild(h('p', { class: 'small muted' },
        'Пока нечего показать: приложение сопоставит запуски с запросами, как только команда поработает.'));
      return box;
    }
    const chart = h('div', { class: 'lim__top-list' });
    for (const item of list) {
      chart.appendChild(h('a', {
        class: 'lim__top-row',
        href: '#/room/' + encodeURIComponent(item.room || '') + '/request/' + encodeURIComponent(item.rid || ''),
        title: 'Открыть запрос: ' + String(item.title || ''),
      },
      h('span', { class: 'lim__top-title' }, String(item.title || item.rid || '')),
      h('span', { class: 'small muted' }, String(item.room_title || item.room || '')),
      h('span', { class: 'lim__num tnum' }, tokensText(item.tokens)),
      h('span', { class: 'lim__num tnum' }, moneyText(item.cost_usd))));
    }
    box.appendChild(chart);
    const wasted = data && data.wasted;
    box.appendChild(h('p', { class: 'lim__wasted small' + (Number(wasted && wasted.attempts) ? ' is-bad' : '') },
      wastedText(wasted)));
    return box;
  }

  function editor() {
    const providers = providerList(data);
    edit.replaceChildren();
    edit.appendChild(h('div', { class: 'lim__section-title' }, 'Мои лимиты'));
    edit.appendChild(h('p', { class: 'small muted lim__edit-hint' },
      'Лимит — ваш потолок, а не отчёт провайдера: приложение только предупреждает, запуски не останавливает. '
        + 'Пустое поле означает «без лимита». Задавать можно в токенах или в вызовах — как вам удобнее.'));
    if (!providers.length) {
      edit.appendChild(h('p', { class: 'small muted' },
        'Провайдеры появятся здесь после первых запусков — тогда и будет что ограничивать.'));
      return;
    }
    const form = h('div', { class: 'lim__edit-grid' });
    form.appendChild(h('div', { class: 'lim__edit-head small muted' },
      h('span', {}, 'Провайдер'), h('span', {}, 'За 5 часов'), h('span', {}, 'За неделю')));
    for (const provider of providers) {
      const row = draft[provider.key] = draft[provider.key] || {
        '5h': { kind: 'tokens', value: '' }, week: { kind: 'tokens', value: '' },
      };
      const cells = ['5h', 'week'].map((w) => {
        const kind = h('select', {
          class: 'select select--sm lim__kind', 'aria-label': 'Что считаем: ' + provider.title,
          onchange: () => { row[w].kind = kind.value; },
        }, KIND_OPTIONS.map((o) => h('option', { value: o.value, selected: row[w].kind === o.value ? 'selected' : null },
          o.label)));
        kind.value = row[w].kind;
        const input = h('input', {
          class: 'input input--sm lim__value', type: 'text', inputmode: 'numeric',
          placeholder: 'без лимита', 'aria-label': 'Лимит ' + provider.title,
          oninput: () => { row[w].value = input.value; },
        });
        input.value = row[w].value;
        return h('div', { class: 'lim__cell-edit' }, input, kind);
      });
      form.appendChild(h('div', { class: 'lim__edit-row', 'data-provider': provider.key },
        h('span', { class: 'lim__provider' }, provider.title), cells[0], cells[1]));
    }
    edit.appendChild(form);
    const save = h('button', {
      class: 'btn btn--primary btn--sm', type: 'button', 'data-action': 'limits-save',
      onclick: () => saveLimits(save),
    }, icon('check', { size: 12 }), 'Сохранить лимиты');
    edit.appendChild(h('div', { class: 'lim__edit-foot' }, save,
      h('span', { class: 'small muted' }, 'Лимиты хранятся на этом компьютере.')));
  }

  async function saveLimits(btn) {
    if (busy) return;
    busy = true;
    if (btn) { btn.disabled = true; btn.classList.add('is-busy'); }
    try {
      const payload = limitsPayload(draft);
      if (!api || typeof api.put !== 'function') throw new Error('нет связи с приложением');
      await api.put('/api/limits/settings', payload);
      toast('Лимиты сохранены', 'ok');
      await load();
      const stillThere = Array.isArray(providerList(data)) && providerList(data).length;
      if (stillThere) toast('Прогресс к лимиту пересчитан', 'info');
    } catch (err) {
      const message = String((err && err.message) || 'Не удалось сохранить лимиты');
      toast(message, 'danger');
      edit.appendChild(h('div', { class: 'banner banner--error lim__banner' }, icon('error', { size: 13 }),
        h('div', { class: 'banner__text' }, message + ' — проверьте связь с приложением и попробуйте снова')));
    } finally {
      busy = false;
      if (btn) { btn.disabled = false; btn.classList.remove('is-busy'); }
    }
  }

  function render() {
    head.replaceChildren();
    body.replaceChildren();
    if (error) {
      body.appendChild(errorBanner(error, () => load()));
      edit.replaceChildren();
      return;
    }
    if (!data) {
      body.appendChild(skeleton(3));
      return;
    }
    const window = (data.windows && data.windows[windowKey]) || {};
    const summary = window.summary || {};
    head.appendChild(windowSwitcher());
    head.appendChild(h('div', { class: 'lim__summary' },
      h('span', { class: 'lim__line' }, data.line || windowSummary(summary)),
      h('span', { class: 'small muted' }, data.source === 'fixtures'
        ? 'демо-данные: журнала запусков ещё нет'
        : 'источник: журнал запусков Hermes (только чтение)'),
      h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => load() },
        icon('refresh', { size: 12 }), 'Обновить')));
    head.appendChild(h('p', { class: 'lim__window-summary small muted' }, windowSummary(summary)));

    for (const warn of (Array.isArray(data.warnings) ? data.warnings : [])) {
      const text = typeof warn === 'string' ? warn : String((warn && warn.message) || '');
      if (!text) continue;
      body.appendChild(h('div', { class: 'banner banner--warn lim__banner' }, icon('warning', { size: 13 }),
        h('div', { class: 'banner__text' }, text)));
    }
    body.appendChild(providerRows(window));
    body.appendChild(forecastBlock());
    body.appendChild(topBlock());
    editor();
  }

  await load();
  return {
    load,
    destroy() {
      stopped = true;
      host.replaceChildren();
    },
  };
}

export default mountLimits;
