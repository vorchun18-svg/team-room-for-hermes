/**
 * notify-settings.js — раздел «Уведомления Windows» на странице настроек (W29B-Q6).
 *
 * Простыми словами: по умолчанию уведомления ВЫКЛЮЧЕНЫ — пока человек не включит
 * их здесь, ничего не всплывает. Включил — приходят системные уведомления о том,
 * что «ждёт ваш ответ», запрос завершён, запуск упал, дежурство нашло изменения;
 * клик открывает нужный запрос. Тихие часы и частота — здесь же.
 *
 * Данные только через сервер: ``GET /api/notify``, ``PUT /api/notify/settings``,
 * ``POST /api/notify/test`` (кнопка «Проверить»). Никаких окон в тестах: в
 * песочнице сервер пишет уведомления в файл (``TEAMROOM_NOTIFY_FAKE``).
 */

import { h } from '../util.js';
import { ensureCss, sectionHeader, errorBanner } from '../ui.js';

const VIEW_CSS = 'css/view-notify.css';

const INTERVALS = [
  { value: 300, label: 'не чаще раза в 5 минут' },
  { value: 600, label: 'не чаще раза в 10 минут' },
  { value: 900, label: 'не чаще раза в 15 минут' },
  { value: 1800, label: 'не чаще раза в 30 минут' },
];

function plain(message) {
  return String(message || '')
    .replace(/PowerShell/g, 'системных уведомлений')
    .replace(/\s+/g, ' ')
    .trim();
}

async function readJson(res) {
  const type = res.headers && res.headers.get ? res.headers.get('Content-Type') || '' : '';
  if (!type.includes('json')) return (await res.text().catch(() => '')) || null;
  return res.json().catch(() => null);
}

function errorText(data, status) {
  const e = data && typeof data === 'object' ? data.error || data : null;
  if (e && e.message) return plain(e.message);
  return 'Сервер ответил ' + status;
}

async function requestJson(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new Error('Нет связи с приложением');
  }
  const data = await readJson(res);
  if (!res.ok) throw new Error(errorText(data, res.status));
  return data;
}

function timeValue(text) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(String(text || '').trim());
  if (!match) return '22:00';
  const hh = Math.min(23, Number(match[1]));
  const mm = Math.min(59, Number(match[2]));
  return String(hh).padStart(2, '0') + ':' + String(mm).padStart(2, '0');
}

function historyLine(entry) {
  const at = Number(entry && entry.at ? entry.at : 0);
  const when = at ? new Date(at * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
  const title = String(entry && entry.title ? entry.title : 'Уведомление');
  const ok = entry && entry.ok;
  return h('li', { class: 'notify__history-item' + (ok ? '' : ' notify__history-item--fail') },
    h('span', { class: 'notify__history-time tnum' }, when),
    h('span', { class: 'notify__history-title' }, title),
    h('span', { class: 'notify__history-state' }, ok ? 'показано' : (plain(entry && entry.reason) || 'не показано')));
}

/**
 * Смонтировать раздел «Уведомления Windows».
 * @param {HTMLElement} container
 * @param {{toast?: Function}} [ctx]
 * @returns {{unmount: Function}}
 */
export async function mountNotifySettings(container, ctx = {}) {
  const doc = globalThis.document;
  const host = container || (doc && doc.body);
  if (!host || typeof host.appendChild !== 'function') return { unmount() {} };

  await ensureCss(VIEW_CSS);

  const toast = ctx && typeof ctx.toast === 'function' ? ctx.toast : () => {};
  // Свой id, не «set-notify»: этот же id носит контейнер-обёртка в настройках —
  // два одинаковых id в документе ломают переходы по якорям и getElementById.
  const el = h('section', { class: 'notify', id: 'notify-panel' });
  host.appendChild(el);

  let alive = true;
  let state = null;
  let saving = false;
  let note = null;
  const stop = () => !alive;

  /* --------------------------------------------------------------- данные */

  async function load() {
    el.replaceChildren(h('div', { class: 'notify__loading' }, 'Читаю настройки уведомлений…'));
    try {
      state = await requestJson('GET', '/api/notify');
    } catch (err) {
      if (stop()) return;
      el.replaceChildren(
        sectionHeader('Уведомления'),
        errorBanner(err, () => load()),
        h('p', { class: 'notify__hint' }, 'Без сервера настройки уведомлений не прочитать. Проверьте, что приложение запущено.'),
      );
      return;
    }
    if (stop()) return;
    render();
  }

  async function save(patch, successText) {
    if (saving) return;
    saving = true;
    note = null;
    try {
      state = await requestJson('PUT', '/api/notify/settings', patch);
      if (successText) toast(successText);
      if (state && Array.isArray(state.warnings) && state.warnings.length) {
        note = { kind: 'warn', text: state.warnings.map((one) => plain(one.message)).join('; ') };
      }
      saving = false;
      if (!stop()) render();
    } catch (err) {
      saving = false;
      note = { kind: 'error', text: plain(err && err.message ? err.message : 'Не удалось сохранить') };
      if (!stop()) render();
    }
  }

  async function sendTest() {
    note = null;
    try {
      await requestJson('POST', '/api/notify/test', {});
      note = { kind: 'ok', text: 'Пробное уведомление отправлено. Если окна не видно — проверьте «Центр уведомлений» Windows.' };
    } catch (err) {
      note = { kind: 'error', text: plain(err && err.message ? err.message : 'Уведомление не показано') };
    }
    if (!stop()) render();
  }

  /* ------------------------------------------------------------------ вид */

  function render() {
    const settings = state.settings || {};
    const channel = state.channel || { name: 'none', reason: '' };
    const quiet = settings.quiet_hours || { enabled: false, from: '22:00', to: '08:00' };
    const kinds = settings.kinds || {};

    const enabled = !!settings.enabled;
    const toggle = h('button', {
      type: 'button',
      class: 'notify__toggle' + (enabled ? ' notify__toggle--on' : ''),
      'aria-pressed': enabled ? 'true' : 'false',
      onclick: () => save({ enabled: !enabled }, enabled ? 'Уведомления выключены' : 'Уведомления включены'),
    }, enabled ? 'Включены' : 'Выключены');

    const head = h('div', { class: 'notify__head' },
      h('div', { class: 'notify__head-text' },
        // Заголовок «Уведомления» уже даёт sectionHeader выше: второй такой же
        // заголовок в карточке читался как дубль («Уведомления / Уведомления»).
        h('div', { class: 'notify__sub' }, enabled
          ? 'Приходят системные уведомления о событиях комнат. Клик открывает нужную задачу.'
          : 'Сейчас выключены: приложение работает молча. Включите — и оно сообщит о важном, даже если окно свёрнуто.')),
      h('div', { class: 'notify__head-actions' }, toggle));

    const kindRows = (state.kinds || []).map((one) => h('label', { class: 'notify__check' },
      h('input', {
        type: 'checkbox',
        checked: kinds[one.id] !== false,
        disabled: !enabled,
        onchange: (ev) => save({ kinds: { [one.id]: !!ev.target.checked } }),
      }),
      h('span', { class: 'notify__check-text' }, one.title)));

    const quietFrom = h('input', {
      type: 'time', class: 'notify__time', value: timeValue(quiet.from), disabled: !enabled,
      onchange: (ev) => save({ quiet_hours: { from: ev.target.value } }),
    });
    const quietTo = h('input', {
      type: 'time', class: 'notify__time', value: timeValue(quiet.to), disabled: !enabled,
      onchange: (ev) => save({ quiet_hours: { to: ev.target.value } }),
    });
    const quietRow = h('div', { class: 'notify__row' },
      h('label', { class: 'notify__check' },
        h('input', {
          type: 'checkbox', checked: !!quiet.enabled, disabled: !enabled,
          onchange: (ev) => save({ quiet_hours: { enabled: !!ev.target.checked } }),
        }),
        h('span', { class: 'notify__check-text' }, 'Тихие часы')),
      h('div', { class: 'notify__quiet-range' },
        h('span', { class: 'notify__muted' }, 'с'), quietFrom,
        h('span', { class: 'notify__muted' }, 'до'), quietTo),
      quiet.enabled && state.quiet_now
        ? h('span', { class: 'chip chip--muted' }, 'сейчас тихо')
        : null);

    const interval = h('select', {
      class: 'notify__select', disabled: !enabled,
      onchange: (ev) => save({ min_interval_s: Number(ev.target.value) }),
    }, ...INTERVALS.map((one) => h('option', {
      value: String(one.value),
      selected: Number(settings.min_interval_s || 300) === one.value,
    }, one.label)));

    const tests = h('button', {
      type: 'button', class: 'btn btn--ghost notify__test', onclick: () => sendTest(),
    }, 'Проверить');

    const history = (state.history || []).slice(0, 5);

    // null в replaceChildren превращается в текстовый узел «null» — на экране
    // появлялась строка «null» под кнопкой «Проверить». Собираем только реальные узлы.
    const nodes = [
      sectionHeader('Уведомления'),
      head,
      h('div', { class: 'notify__channel' + (channel.available ? '' : ' notify__channel--off') },
        h('span', { class: 'notify__channel-dot' }),
        h('span', { class: 'notify__channel-text' }, plain(channel.reason) || 'Канал уведомлений не определён')),
      h('div', { class: 'notify__group' },
        h('div', { class: 'notify__group-title' }, 'О чём сообщать'),
        h('div', { class: 'notify__checks' }, ...kindRows)),
      h('div', { class: 'notify__group' },
        h('div', { class: 'notify__group-title' }, 'Когда молчать'),
        quietRow,
        h('div', { class: 'notify__row notify__row--tight' },
          h('span', { class: 'notify__muted' }, 'Частота:'), interval)),
      h('div', { class: 'notify__row notify__row--actions' },
        tests,
        h('span', { class: 'notify__muted' },
          'За сутки показано: ' + String(state.sent_today || 0))),
      note ? h('div', { class: 'notify__note notify__note--' + note.kind }, note.text) : null,
      h('div', { class: 'notify__group' },
        h('div', { class: 'notify__group-title' }, 'Последние уведомления'),
        history.length
          ? h('ul', { class: 'notify__history' }, ...history.map(historyLine))
          : h('p', { class: 'notify__hint' },
            enabled
              ? 'Пока ничего не приходило. Первое уведомление появится, когда задача завершится или что-то будет ждать вашего ответа.'
              : 'Журнал появится после включения: здесь видно, что и когда было показано.')),
    ].filter(Boolean);
    el.replaceChildren(...nodes);
  }

  await load();

  return {
    unmount() {
      alive = false;
      el.remove();
    },
  };
}

export default mountNotifySettings;
