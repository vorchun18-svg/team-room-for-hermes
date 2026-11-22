/**
 * daily.js — «Итог дня» на экране «Сегодня» (W29B-Q8).
 *
 * Отвечает на вопрос, которого не задаёт «Сегодня»: «как прошли сутки». Что
 * сделано, что решено, что пришло файлами, что ждёт (с действием) и что
 * остановилось — плюс расход Hermes за сутки. Всё это собирается на сервере по
 * данным (`GET /api/daily`), без модели; платный шаг один — кнопка «Короче»,
 * и её цена написана на самой кнопке до нажатия.
 *
 * Ниже поля — поиск по памяти комнаты («почему мы так решили»): он же живёт
 * разделом «Память» в палитре Ctrl+K.
 *
 * Правила экрана: сворачиваемый блок, детерминированные `data-key` (иначе
 * live-patch пересобирал бы карточку каждые 20 с), пустое состояние объясняет
 * зачем и что сделать, каждая строка «ждёт вас» ведёт к действию.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { countNoun } from '../text-ru.js';
import { createSection } from './section-collapse.js';
import {
  DAILY_SECTIONS, FORMS, counterChips, decisionRows, doneRows, emptyText, fileRows,
  isEmptyDaily, periodTitle, shortButtonText, shortState, spentText, stoppedRows, waitingRows,
} from './daily-model.js';
import { createMemorySearch, ensureStyles as ensureMemoryStyles } from './memory-search.js';

export const VIEW_CSS = 'css/view-daily.css';

/** Как часто обновляем итог: он меняется медленнее «Сегодня». */
export const POLL_MS = 60000;

/* ───────────────────────────────────────────────────────────────── разметка */

function linkOrText(href, text, cls) {
  return href ? h('a', { class: cls, href }, text) : h('span', { class: cls }, text);
}

function rowDone(row) {
  return h('div', { class: 'day-row', 'data-key': 'day-done|' + row.requestId },
    h('span', { class: 'day-row__mark', 'aria-hidden': 'true' }, icon('check', { size: 12 })),
    h('div', { class: 'day-row__main' },
      linkOrText(row.href, row.title, 'day-row__text day-row__link'),
      h('span', { class: 'day-row__meta small muted' }, row.roomTitle)));
}

function rowDecision(row) {
  return h('div', { class: 'day-row', 'data-key': 'day-dec|' + row.requestId + '|' + row.title.slice(0, 20) },
    h('span', { class: 'day-row__mark', 'aria-hidden': 'true' }, icon('law', { size: 12 })),
    h('div', { class: 'day-row__main' },
      h('span', { class: 'day-row__text' }, row.title),
      h('span', { class: 'day-row__meta small muted' },
        row.statusLabel ? h('span', { class: 'day-row__status' }, row.statusLabel) : null,
        row.statusLabel && row.roomTitle ? h('span', { class: 'day-row__dot' }, ' · ') : null,
        row.roomTitle,
        row.href ? h('a', { class: 'day-row__open', href: row.href }, ' к запросу') : null)));
}

function rowWaiting(row) {
  return h('div', { class: 'day-row day-row--wait', 'data-key': 'day-wait|' + row.room + '|' + row.text.slice(0, 20) },
    h('span', { class: 'day-row__mark day-row__mark--warn', 'aria-hidden': 'true' }, icon('warning', { size: 12 })),
    h('div', { class: 'day-row__main' },
      row.href
        ? h('a', { class: 'day-row__text day-row__link', href: row.href }, row.text)
        : h('span', { class: 'day-row__text' }, row.text),
      h('span', { class: 'day-row__meta small muted' },
        row.kindLabel ? h('span', { class: 'day-row__kind' }, row.kindLabel) : null,
        row.kindLabel ? h('span', { class: 'day-row__dot' }, ' · ') : null,
        row.recommended ? h('span', { class: 'day-row__rec' }, row.recommended) : null,
        row.href ? h('a', { class: 'day-row__open', href: row.href }, ' Открыть') : null)));
}

function rowFile(row) {
  return h('div', { class: 'day-row', 'data-key': 'day-file|' + row.room + '|' + row.name },
    h('span', { class: 'day-row__mark', 'aria-hidden': 'true' }, icon('file', { size: 12 })),
    h('div', { class: 'day-row__main' },
      h('span', { class: 'day-row__text' }, row.name),
      h('span', { class: 'day-row__meta small muted' }, row.roomTitle)));
}

function rowStopped(row) {
  const tone = row.kind === 'archived' ? 'muted' : 'danger';
  return h('div', { class: ['day-row', 'day-row--' + tone], 'data-key': 'day-stop|' + row.requestId + '|' + row.kind },
    h('span', { class: 'day-row__mark', 'aria-hidden': 'true' }, icon(row.kind === 'archived' ? 'archive' : 'warning', { size: 12 })),
    h('div', { class: 'day-row__main' },
      linkOrText(row.href, row.title, 'day-row__text day-row__link'),
      h('span', { class: 'day-row__meta small muted' },
        row.kind === 'archived' ? 'в архиве' : 'остановлено',
        row.reason ? ' · ' + row.reason : '',
        row.roomTitle ? ' · ' + row.roomTitle : '')));
}

/* ─────────────────────────────────────────────────────────────────── карточка */

/**
 * Карточка «Итог дня».
 *
 * @param {object} opts {api, toast?, room?}
 * @returns {{element: HTMLElement, update: Function, load: Function, destroy: Function}}
 */
export function createDaily(opts = {}) {
  const api = opts.api;
  const toast = typeof opts.toast === 'function' ? opts.toast : () => {};
  const room = opts.room ? String(opts.room) : '';

  let payload = null;
  // Комната для «Короче»: заданная видом, иначе — самая занятая за сутки
  // (на общем «Сегодня» вид не знает комнаты, а сжимать нужно конкретную).
  let targetRoom = room;
  let short = null;
  let shortBusy = false;
  let stopped = false;

  let memory = null;

  const chipsHost = h('div', { class: 'day-card__chips' });
  const spentHost = h('p', { class: 'day-card__spent small muted' });
  const listsHost = h('div', { class: 'day-card__lists' });
  const shortHost = h('div', { class: 'day-card__short' });
  memory = createMemorySearch({ api, toast, room, title: 'Поиск по памяти комнаты' });

  // Тело карточки собирается ДО блока: секция зовёт `content()` сразу при
  // развороте, и ссылка на ещё не объявленную переменную уронила бы вид
  // («Cannot access 'body' before initialization»).
  const body = h('div', { class: 'day-card' },
    h('div', { class: 'day-card__head' },
      h('span', { class: 'day-card__period' }, '…'),
      chipsHost,
      spentHost),
    listsHost, shortHost, memory.element,
    h('p', { class: 'day-card__foot tiny muted' },
      'Итог собирается сам по данным комнат — без модели. «Короче» — единственный платный шаг, '
      + 'его цена написана на кнопке.'));

  const control = createSection({
    key: 'daily',
    id: 'today-daily',
    requestId: 'today',
    card: true,
    title: 'Итог дня',
    className: 'today-sec today-sec--daily',
    defaultOpen: true,
    // Содержимое собирается один раз, при первом развороте блока: карточка
    // «Сегодня» перерисовывается по опросу, и тело не должно пересобираться.
    content: () => [body],
  });

  const element = control.element;
  ensureMemoryStyles().catch(() => { /* стили поля вторичны */ });

  const periodNode = body.querySelector('.day-card__period');

  function listSection(key, rows, renderRow) {
    const meta = DAILY_SECTIONS.find((s) => s.key === key) || { key, title: key, empty: '' };
    if (!rows.length) return null;
    return h('section', { class: 'day-card__sec', 'data-key': 'day-sec|' + key },
      h('h3', { class: 'day-card__sec-title small' }, meta.title),
      h('div', { class: 'day-card__rows' }, ...rows.map(renderRow)));
  }

  function render() {
    if (stopped) return;
    const p = payload || {};
    if (periodNode) {
      // Когда «Короче» доступен только по одной комнате, честно говорим, по какой
      // именно: «Утро · 21 ноября · Кофейня». Иначе числа выглядят общими.
      const rooms = Array.isArray(p.rooms) ? p.rooms : [];
      const narrowed = targetRoom && rooms.length === 1 ? String(rooms[0].title || '') : '';
      periodNode.textContent = p.day
        ? periodTitle(p) + (narrowed ? ' · ' + narrowed : '')
        : 'Итог дня';
    }
    chipsHost.replaceChildren(...counterChips(p).map((chip) => h('span', {
      class: ['chip', 'chip--' + chip.tone, 'day-card__chip', 'tnum'],
      'data-key': 'day-chip|' + chip.key,
    }, chip.text)));
    const spent = spentText(p.spent);
    spentHost.textContent = spent ? 'Расход за сутки: ' + spent : '';
    spentHost.hidden = !spent;

    if (isEmptyDaily(p)) {
      const info = emptyText(p);
      listsHost.replaceChildren(h('div', { class: 'day-card__empty', 'data-key': 'day-empty' },
        h('div', { class: 'day-card__empty-title' }, info.title),
        h('p', { class: 'small muted' }, info.text),
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/today?focus=needs' }, 'Что ждёт сейчас')));
      renderShort();
      return;
    }

    const sections = [
      listSection('done', doneRows(p), rowDone),
      listSection('decisions', decisionRows(p), rowDecision),
      listSection('waiting', waitingRows(p), rowWaiting),
      listSection('files', fileRows(p), rowFile),
      listSection('stopped', stoppedRows(p), rowStopped),
    ].filter(Boolean);
    const totalWaiting = Number((p.counters || {}).waiting || 0);
    const shownWaiting = waitingRows(p).length;
    if (totalWaiting > shownWaiting) {
      sections.splice(2, 0, h('p', { class: 'day-card__more small muted', 'data-key': 'day-wait-more' },
        'Ещё ' + countNoun(totalWaiting - shownWaiting, FORMS.case) + ' ждёт — на вкладке «От вас нужно».'));
    }
    listsHost.replaceChildren(...sections);
    renderShort();
  }

  function renderShort() {
    shortHost.replaceChildren();
    if (!payload || isEmptyDaily(payload)) return;
    const saved = short && short.text ? shortState({ ok: true, text: short.text, fake: short.fake }) : null;
    const estimate = (shortResultCache && shortResultCache.estimate) || lastEstimate;
    const button = h('button', {
      class: 'btn btn--secondary btn--sm day-card__short-btn',
      type: 'button',
      'data-key': 'day-short',
      title: 'Сжать итог дня дешёвой моделью. Цена указана до нажатия.',
      disabled: shortBusy ? 'disabled' : null,
      onclick: () => runShort(),
    }, shortBusy ? 'Сжимаю…' : (saved ? 'Короче ещё раз' : shortButtonText(estimate)));
    const parts = [h('div', { class: 'day-card__short-row' },
      h('span', { class: 'day-card__short-label small' }, saved ? 'Кратко' : 'Пересказать коротко'),
      button)];
    if (saved && saved.text) {
      parts.push(h('p', { class: 'day-card__short-text', 'data-key': 'day-short-text' }, saved.text));
      if (saved.message) parts.push(h('p', { class: 'tiny muted' }, saved.message));
    } else if (shortResultCache && !shortResultCache.ok) {
      parts.push(h('p', { class: 'day-card__short-error small', 'data-key': 'day-short-error' },
        shortResultCache.error || '«Короче» не получился',
        h('span', { class: 'muted' }, ' ' + (shortState(shortResultCache).hint || ''))));
    }
    shortHost.replaceChildren(...parts);
  }

  let shortResultCache = null;
  let lastEstimate = null;

  async function runShort() {
    if (!targetRoom) {
      // За сутки нет ни одной комнаты с событиями — сжимать нечего.
      toast('За сутки событий нет — «Короче» не из чего собирать.', 'warn');
      return;
    }
    shortBusy = true;
    renderShort();
    try {
      const res = await api.post('/api/daily/shorten', { room: targetRoom });
      shortResultCache = Object.assign({ ok: true }, res || {});
      lastEstimate = (res && res.estimate) || lastEstimate;
      short = { text: String((res && res.text) || ''), at: (res && res.at) || null, fake: Boolean(res && res.fake) };
    } catch (err) {
      shortResultCache = { ok: false, error: String((err && err.message) || '«Короче» не получился'),
        estimate: lastEstimate };
      toast('«Короче» не получился: ' + String((err && err.message) || 'модель не ответила'), 'warn');
    } finally {
      shortBusy = false;
      renderShort();
    }
  }

  /** Самая занятая за сутки комната: по ней и считается «Короче». */
  function busiestRoom(data) {
    const rooms = Array.isArray(data && data.rooms) ? data.rooms : [];
    let best = null;
    let bestScore = 0;
    for (const item of rooms) {
      if (!item || !item.id) continue;
      const score = Number(item.done || 0) + Number(item.decisions || 0)
        + Number(item.waiting || 0) * 2;
      if (score > bestScore) { best = String(item.id); bestScore = score; }
    }
    return best;
  }

  async function load() {
    try {
      const params = targetRoom ? '?room=' + encodeURIComponent(targetRoom) : '';
      const got = await api.get('/api/daily' + params);
      if (stopped) return null;
      payload = got || null;
      // Общий итог: цена «Короче» и прошлое сжатие есть только у комнаты,
      // поэтому дозапрашиваем самую занятую — один короткий запрос, не цикл.
      if (!targetRoom && !isEmptyDaily(payload)) {
        const busiest = busiestRoom(payload);
        if (busiest) {
          targetRoom = busiest;
          const detail = await api.get('/api/daily?room=' + encodeURIComponent(busiest))
            .catch(() => null);
          if (stopped) return null;
          if (detail) payload = detail;
        }
      }
      if (payload && payload.short && payload.short.text) short = payload.short;
      const est = payload && payload.estimate;
      if (est) lastEstimate = est;
      render();
      return payload;
    } catch (err) {
      if (stopped) return null;
      listsHost.replaceChildren(h('div', { class: 'day-card__empty', 'data-key': 'day-error' },
        h('div', { class: 'day-card__empty-title' }, 'Итог дня не собрался'),
        h('p', { class: 'small muted' }, String((err && err.message) || 'сервер не ответил')),
        h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button',
          onclick: () => { load(); },
        }, 'Попробовать снова')));
      return null;
    }
  }

  render();

  return {
    element,
    load,
    update(next) {
      payload = next || payload;
      render();
    },
    destroy() {
      stopped = true;
      if (memory && typeof memory.destroy === 'function') memory.destroy();
      if (control && typeof control.destroy === 'function') control.destroy();
    },
  };
}

export default { createDaily, VIEW_CSS, POLL_MS };
