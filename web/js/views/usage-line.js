/**
 * usage-line.js — строка расхода запроса (W28-E, п.6 замеров 19.11.2026).
 *
 * Что показывает: «Расход: N вызовов, M тыс. токенов (база / тело / инструменты)» —
 * одна строка в правой колонке запроса. Рядом — раскрывающаяся расшифровка: по каким
 * моделям шёл расход, сколько стоило и была ли попытка «не на той модели».
 *
 * Откуда цифры: `GET /api/requests/{rid}/usage` (server/usage.py) читает `state.db`
 * профилей только на чтение и складывает `sessions` тех сессий, что Hermes завёл для
 * карточек запроса. Клиент ничего не считает сам: строка берётся из ответа (`line`),
 * чтобы сервер и интерфейс говорили одно и то же.
 *
 * Правила (как у остальных блоков колонки):
 * - нет данных — не пустой блок, а честная строка «Расход: нет данных…»;
 * - ошибка сети — тихая строка с кнопкой «Обновить» (без тупиков);
 * - песочница/демо — данные из фикстуры, и подпись говорит об этом (`source`);
 * - ничего не пишем и не меняем: только чтение.
 */

import { h } from '../util.js';

/** Сколько знаков строки терпит колонка: дальше — расшифровка, не строка. */
export const LINE_MAX = 160;

/**
 * Тысячи токенов в коротком виде: 12400 → «12», 640 → «0,6».
 * @param {number} value
 * @returns {string}
 */
export function thousands(value) {
  const n = Number(value) || 0;
  const thousandsValue = n / 1000;
  if (thousandsValue >= 10) return String(Math.round(thousandsValue));
  const short = Math.round(thousandsValue * 10) / 10;
  return String(short).replace('.', ',');
}

/**
 * Текст строки расхода: серверный `line`, иначе собираем из полей ответа.
 *
 * Серверную строку не переписываем: если она есть, интерфейс показывает её как есть —
 * иначе «Расход» в карточке и в API начнут расходиться.
 * @param {object} payload ответ `/usage`
 * @returns {string}
 */
export function usageText(payload) {
  const data = payload || {};
  const line = String(data.line || '').trim();
  if (line) return line;
  const tokens = data.tokens || {};
  const total = ['input', 'cache_read', 'cache_write', 'output']
    .reduce((sum, key) => sum + (Number(tokens[key]) || 0), 0);
  const calls = Number(data.calls) || 0;
  if (!calls && !total) return 'Расход: нет данных (карточки ещё не запускались)';
  const split = data.split || {};
  const parts = [];
  if (Number(split.base)) parts.push('база ' + thousands(split.base));
  if (Number(split.body)) parts.push('тело ' + thousands(split.body));
  if (Number(split.tools)) parts.push('инструменты ' + thousands(split.tools));
  const tail = parts.length ? ' (' + parts.join(' / ') + ' тыс.)' : '';
  return 'Расход: ' + calls + ' выз. / ' + thousands(total) + ' тыс. токенов' + tail;
}

/**
 * Что ещё важно знать про расход (расшифровка под строкой).
 *
 * @param {object} payload ответ `/usage`
 * @returns {{note: string, warning: string, models: Array<{model: string, calls: number, cost: number}>}}
 */
export function usageNote(payload) {
  const data = payload || {};
  const models = (Array.isArray(data.models) ? data.models : []).map((item) => ({
    model: String((item && item.model) || '—'),
    calls: Number((item && item.calls) || 0),
    cost: Number((item && item.cost_usd) || 0),
  }));
  const notes = [];
  const cost = Number(data.cost_usd) || 0;
  if (cost > 0) notes.push('≈ $' + cost.toFixed(2));
  if (data.source === 'fixtures') notes.push('демо-данные');
  if (data.source === 'none') notes.push('сессий ещё нет');
  const mismatched = data.mismatched || {};
  const attempts = Number(mismatched.attempts) || 0;
  let warning = '';
  if (attempts > 0) {
    warning = 'Попыток не на той модели: ' + attempts
      + ' — карточку перезапускали, токены ушли дважды.';
  }
  return { note: notes.join(' · '), warning, models };
}

/**
 * Поставить строку расхода в колонку запроса.
 *
 * Загружает ответ сам и не бросает: колонка «От вас нужно» не должна падать из-за
 * расхода. Повторный вызов на тот же `rid` ничего не дублирует.
 *
 * @param {HTMLElement} parent куда ставить
 * @param {string} rid id корневой карточки
 * @param {{get?: Function}} api клиент (request.js передаёт ctx.api)
 * @returns {HTMLElement|null} узел строки
 */
export function mountUsage(parent, rid, api) {
  if (!parent || !rid) return null;
  if (parent.querySelector && parent.querySelector('[data-usage-line="' + rid + '"]')) {
    return parent.querySelector('[data-usage-line="' + rid + '"]');
  }
  const box = h('div', { class: 'req-usage', 'data-usage-line': rid });
  const text = h('span', { class: 'req-usage__text' }, 'Расход: считается…');
  const more = h('span', { class: 'req-usage__note' }, '');
  box.appendChild(text);
  box.appendChild(more);
  if (parent.appendChild) parent.appendChild(box);

  const render = (payload) => {
    text.textContent = usageText(payload);
    const info = usageNote(payload);
    const pieces = [];
    if (info.models.length) {
      pieces.push(info.models.map((m) => m.model + ' — ' + m.calls + ' выз.').join('; '));
    }
    if (info.note) pieces.push(info.note);
    more.textContent = pieces.join(' · ');
    box.setAttribute('title', info.warning || info.note || '');
    if (info.warning) {
      box.appendChild(h('div', { class: 'req-usage__warn' }, info.warning));
    }
  };

  const load = () => {
    if (!api || typeof api.get !== 'function') {
      text.textContent = 'Расход: нет данных (нет связи с сервером)';
      return;
    }
    text.textContent = 'Расход: считается…';
    Promise.resolve(api.get('/api/requests/' + encodeURIComponent(rid) + '/usage'))
      .then((payload) => render(payload || {}))
      .catch(() => {
        text.textContent = 'Расход: не удалось получить';
        more.textContent = '';
        if (box.querySelector && box.querySelector('.req-usage__retry')) return;
        box.appendChild(h('button', {
          class: 'btn btn--ghost btn--sm req-usage__retry', type: 'button',
          onclick: () => load(),
        }, 'Обновить'));
      });
  };
  load();
  return box;
}

export default { LINE_MAX, thousands, usageText, usageNote, mountUsage };
