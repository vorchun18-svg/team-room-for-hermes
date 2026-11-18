/**
 * today.js — экран «Сегодня» (#/today), W24-N3.
 *
 * Отвечает на утренний вопрос Михаила: «что от меня ждут, что горит, что
 * изменилось, пока меня не было, и всё ли работает». Сверху вниз:
 *   плашка «Здоровье» (только когда есть warn/fail) → «Ждёт вас» → «Просрочено»
 *   → «Ближайшие 3 дня» → «Что изменилось с прошлого раза» → «Идёт сейчас»
 *   → «Дежурства».
 *
 * Правила, которые здесь соблюдаются:
 *   • блоки сворачиваются общим компонентом W21 (`section-collapse.js`), выбор
 *     человека запоминается;
 *   • обновление раз в 20 с идёт через `live-patch.js`: одинаковые узлы остаются
 *     на месте, поэтому не сбрасывается прокрутка и не теряется нажатие (B-42/B-49);
 *   • у карточек и строк ДЕТЕРМИНИРОВАННЫЕ `data-key` — иначе live-patch считал бы
 *     их новыми и пересобирал каждый опрос;
 *   • раскладка считается по ширине ВИДА (`@container view`), а не окна: на
 *     половине экрана (900–1000 px) одна колонка, две — только от 1100 px;
 *   • тексты и формы — из `text-ru.js`/`today-model.js`, без бизнес-терминов.
 *
 * Данные: GET /api/today (§3.1), GET /api/duties?room=all (§3.2),
 * GET /api/health/integrations (§3.3). Отметка захода — POST /api/today/seen
 * через 3 секунды после показа.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { api, poll } from '../api.js';
import { ensureCss, errorBanner, skeleton } from '../ui.js';
import { applyLivePatch } from '../live-patch.js';
import { createSection } from './section-collapse.js';
import { setTodayCount } from '../shell-sidebar.js';
import { countNoun } from '../text-ru.js';
import {
  EMPTY_TEXT, FORMS, NEEDS_YOU_LIMIT, agoText, changedSummary, counterChips, dueRow, dueSoonText,
  dutyProblemText, dutyRunText, dutyTone, healthProblems, isEmptyDay, needsYouCard, needsYouVisible,
  runningRow,
} from './today-model.js';

export const VIEW_CSS = 'css/view-today.css';

/** Как часто обновляем экран (ТЗ: 20 с). */
export const POLL_MS = 20000;

/** Через сколько после показа отмечаем заход (ТЗ: 3 с). */
export const SEEN_DELAY_MS = 3000;

/* ─────────────────────────────────────────────────────────── мелкие детали */

const TONE_ICON = { ok: 'check', info: 'sync', warn: 'warning', fail: 'warning', muted: 'clock' };

/** Кнопка-ссылка на запрос: клик ведёт на страницу запроса. */
function requestLink(href, text, cls) {
  return h('a', { class: cls, href }, text);
}

/** Хэш страницы запроса (пусто, если запроса нет — тогда просто текст). */
function requestHash(item) {
  const room = String((item && item.room) || '');
  const rid = String((item && item.request_id) || '');
  if (!room || !rid) return '';
  return '#/room/' + encodeURIComponent(room) + '/request/' + encodeURIComponent(rid);
}

/** Заголовок с датой: «Сегодня» + «вторник, 18 ноября · обновлено 01:45». */
export function dayTitle(nowMs = Date.now()) {
  const date = new Date(nowMs);
  const days = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
  const months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
    'сентября', 'октября', 'ноября', 'декабря'];
  return days[date.getDay()] + ', ' + date.getDate() + ' ' + months[date.getMonth()];
}

/** «обновлено 01:45» — время последнего ответа сервера. */
export function updatedText(generatedAt) {
  const ts = Number(generatedAt);
  if (!Number.isFinite(ts) || ts <= 0) return '';
  const date = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return 'обновлено ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

/* ────────────────────────────────────────────────────────────────── разметка */

/** Бейдж вида дела: «вопрос», «сбой» — тихо, без капса. */
function kindChip(kind, label) {
  if (!label) return null;
  const tone = kind === 'failure' ? 'danger' : (kind === 'decision' || kind === 'approval' ? 'warn' : 'muted');
  return h('span', { class: ['chip', 'chip--' + tone, 'today__kind'] }, label);
}

/** Карточка «ждёт вас». */
function needsYouNode(card) {
  const href = requestHash({ room: card.room, request_id: card.requestId });
  const title = card.requestTitle || 'Запрос';
  const key = 'ny|' + card.room + '|' + card.requestId + '|' + card.kind + '|' + card.text.slice(0, 24);
  const head = h('div', { class: 'today-card__head' },
    h('span', { class: 'today-card__room', title: card.roomTitle }, card.roomTitle),
    kindChip(card.kind, card.kindLabel),
    card.urgent ? h('span', { class: ['chip', 'chip--danger', 'today__urgent'] }, 'срочно') : null,
    h('span', { class: 'today-card__ago tnum', title: 'Как давно ждёт' }, card.ago));
  const body = h('p', { class: 'today-card__text' }, card.text);
  const foot = h('div', { class: 'today-card__foot' },
    href
      ? requestLink(href, title, 'today-card__link')
      : h('span', { class: 'today-card__link today-card__link--plain' }, title),
    card.recommended ? h('span', { class: 'today-card__rec' }, icon('lightbulb', { size: 12 }), card.recommended) : null);
  const node = h(card.urgent ? 'a' : 'div', {
    class: ['today-card', card.urgent ? 'today-card--urgent' : ''],
    href: card.urgent && href ? href : null,
    'data-key': key,
    tabindex: card.urgent && href ? null : undefined,
  }, head, body, foot);
  if (!card.urgent && href) {
    // Не срочная карточка тоже открывает запрос — но обычным кликом по ссылке
    // в подвале, чтобы не превращать весь блок в ссылку (текст читается спокойнее).
    node.addEventListener('click', (ev) => {
      const target = ev.target;
      if (target && target.closest && target.closest('a')) return;
      location.hash = href;
    });
    node.classList.add('today-card--clickable');
  }
  return node;
}

/** Строка «просрочено / ближайшие 3 дня». */
function dueNode(row, kind) {
  const r = dueRow(row, kind);
  const href = requestHash({ room: row && row.room, request_id: r.requestId });
  const when = h('span', { class: ['today-row__when', 'tnum', 'today-row__when--' + r.tone] }, r.when);
  return h('div', { class: 'today-row', 'data-key': 'due|' + kind + '|' + String(row && row.item_id || r.text) },
    h('span', { class: 'today-row__mark', 'aria-hidden': 'true' }, icon(kind === 'overdue' ? 'clock' : 'calendar', { size: 13 })),
    h('div', { class: 'today-row__main' },
      h('div', { class: 'today-row__text' }, r.text),
      h('div', { class: 'today-row__meta small muted' },
        r.owner ? h('span', { class: 'today-row__owner' }, 'владелец: ' + r.owner) : null,
        r.owner ? h('span', { class: 'today-row__dot' }, '·') : null,
        href ? requestLink(href, r.roomTitle || 'запрос', 'today-row__room') : h('span', { class: 'today-row__room' }, r.roomTitle),
        r.due ? h('span', { class: 'today-row__dot' }, '·') : null,
        r.due ? h('span', { class: 'today-row__date tnum' }, 'срок ' + r.due) : null)),
    when);
}

/** «Что изменилось»: три группы одним списком. */
function changedNodes(data, now) {
  const sum = changedSummary(data);
  const nodes = [];
  if (sum.newRequests) {
    nodes.push(h('p', { class: 'today-changed__count', 'data-key': 'chg-new' },
      countNoun(sum.newRequests, FORMS.request) + ' пришло'));
  }
  if (sum.newAnswers) {
    nodes.push(h('p', { class: 'today-changed__count', 'data-key': 'chg-ans' },
      countNoun(sum.newAnswers, FORMS.answer) + ' от участников'));
  }
  for (const item of sum.finished) {
    const href = requestHash({ room: item.room, request_id: item.requestId });
    nodes.push(h('div', { class: 'today-changed__row', 'data-key': 'chg-fin|' + item.requestId, title: item.roomTitle },
      h('span', { class: 'chip chip--ok today-changed__tag' }, 'готово'),
      h('span', { class: 'today-changed__text' },
        href ? requestLink(href, item.requestTitle || 'Запрос', 'today-changed__link') : item.requestTitle),
      h('span', { class: 'today-changed__ago small muted tnum' }, agoText(item.at, now))));
  }
  for (const item of sum.failures) {
    nodes.push(h('div', { class: 'today-changed__row today-changed__row--fail', 'data-key': 'chg-fail|' + item.requestId, title: item.roomTitle },
      h('span', { class: 'chip chip--danger today-changed__tag' }, 'сбой'),
      h('span', { class: 'today-changed__text' }, item.requestTitle + (item.reason ? ' — ' + item.reason : '')),
      h('span', { class: 'today-changed__ago small muted tnum' }, agoText(item.at, now))));
  }
  if (!nodes.length) nodes.push(h('p', { class: 'today-empty small muted', 'data-key': 'chg-empty' }, EMPTY_TEXT.changed));
  return nodes;
}

/** Строка «идёт сейчас». */
function runningNode(row) {
  const r = runningRow(row);
  const href = requestHash(row);
  return h('div', { class: 'today-row', 'data-key': 'run|' + (row && row.request_id) },
    h('span', { class: 'today-row__mark today-row__mark--live', 'aria-hidden': 'true' }, h('span', { class: 'today-dot' })),
    h('div', { class: 'today-row__main' },
      h('div', { class: 'today-row__text' }, href ? requestLink(href, r.requestTitle || 'Запрос', 'today-row__link') : r.requestTitle),
      h('div', { class: 'today-row__meta small muted' },
        h('span', {}, r.roomTitle), h('span', { class: 'today-row__dot' }, '·'), h('span', {}, r.membersText))),
    h('span', { class: 'today-row__when tiny tnum' }, r.ago));
}

/** Дежурство: строка с раскрытием краткого итога. */
function dutyNode(item, now) {
  const tone = dutyTone(item);
  const problem = dutyProblemText(item);
  const excerpt = String((item && item.excerpt) || '').trim();
  const body = h('div', { class: 'today-duty__body' },
    problem ? h('p', { class: 'today-duty__problem' }, icon('warning', { size: 12 }), problem) : null,
    excerpt ? h('p', { class: 'today-duty__excerpt small' }, excerpt) : h('p', { class: 'today-duty__excerpt small muted' }, 'Итога пока нет.'),
    h('p', { class: 'today-duty__meta tiny muted' },
      item.enabled === false ? 'выключено' : 'включено',
      ' · ',
      countNoun(Number(item.missed_recent) || 0, ['пропуск', 'пропуска', 'пропусков']) + ' за 7 дней'));
  return h('details', { class: ['today-duty', 'today-duty--' + tone], 'data-key': 'duty|' + (item && item.job_id) },
    h('summary', { class: 'today-duty__summary' },
      h('span', { class: 'today-duty__name' },
        h('span', { class: 'today-duty__icon', 'aria-hidden': 'true' }, icon(TONE_ICON[tone] || 'clock', { size: 13 })),
        h('span', { class: 'today-duty__title', title: item.name }, String(item.name || item.job_id || 'Дежурство'))),
      h('span', { class: 'today-duty__schedule small muted' }, String(item.schedule_human || item.schedule || '')),
      h('span', { class: ['today-duty__run', 'tiny', 'tnum', tone === 'fail' ? 'is-fail' : ''] }, dutyRunText(item, now))),
    body);
}

/* ─────────────────────────────────────────────────────────────────── экран */

/**
 * @param {HTMLElement} el контейнер вида (router передаёт `.view`-контейнер)
 * @param {object} ctx { api, route, navigate, toast, refresh }
 */
export async function mount(el, ctx) {
  await ensureCss(VIEW_CSS);
  const apiRef = (ctx && ctx.api) || api;
  const toast = (message, kind) => {
    if (ctx && typeof ctx.toast === 'function') ctx.toast(message || '', kind || 'info');
  };

  let stopped = false;
  let data = null;
  let health = null;
  let duties = null;
  let seenTimer = null;
  let seenSent = false;
  // «Показать все» — состояние ЭКРАНА, а не разметки: опрос раз в 20 с
  // пересобирает карточки, и раскрытый список не должен схлопываться сам.
  let showAll = false;

  const chipsHost = h('div', { class: 'today__chips', id: 'today-chips' });
  const leadHost = h('p', { class: 'today__lead', id: 'today-lead' }, 'Загрузка…');
  const title = h('h1', { class: 'today__title' }, 'Сегодня');

  const head = h('header', { class: 'today__head' },
    h('div', { class: 'today__headline' }, title, leadHost),
    h('div', { class: 'today__headright' }, chipsHost));

  const notesHost = h('div', { class: 'today__notes' });
  const bodyHost = h('div', { class: 'today__grid', id: 'today-grid' });
  const root = h('div', { class: 'view__inner today', 'data-view': 'today' },
    head, notesHost, bodyHost,
    h('p', { class: 'today__foot tiny muted' },
      'Показано то, что требует вас, и то, что идёт без вас. Отметка захода ставится автоматически.'));

  el.appendChild(root);

  /* --- блоки (создаются один раз: их состояние переживает опрос) --------- */

  const sections = {};
  function makeSection(key, opts) {
    const ctl = createSection(Object.assign({
      key,
      id: 'today-' + key,
      requestId: 'today',
      // Заголовки «Ждёт вас» и «Просрочено» — не карточки-обёртки: у каждой
      // секции свой ритм, вид задаёт view-today.css.
      card: false,
      className: 'today-sec today-sec--' + key,
    }, opts));
    sections[key] = ctl;
    return ctl;
  }

  const healthSec = makeSection('health', { title: 'Здоровье', defaultOpen: false });
  const needsSec = makeSection('needs_you', { title: 'Ждёт вас', defaultOpen: true });
  const overdueSec = makeSection('overdue', { title: 'Просрочено', defaultOpen: true });
  const soonSec = makeSection('due_soon', { title: 'Ближайшие 3 дня', defaultOpen: true });
  const changedSec = makeSection('changed', { title: 'Что изменилось с прошлого раза', defaultOpen: false });
  const runningSec = makeSection('running', { title: 'Идёт сейчас', defaultOpen: true });
  const dutiesSec = makeSection('duties', { title: 'Дежурства', defaultOpen: true });

  const mainCol = h('div', { class: 'today__col today__col--main' }, needsSec.element, runningSec.element);
  const sideCol = h('div', { class: 'today__col today__col--side' },
    overdueSec.element, soonSec.element, changedSec.element, dutiesSec.element);

  /* --- точечная правка: одинаковые узлы остаются на месте --------------- */

  function patchInto(host, children) {
    if (!host) return;
    const list = (Array.isArray(children) ? children : [children]).filter(Boolean);
    // Класс узла-обёртки = класс живого узла: `applyLivePatch` переносит
    // атрибуты нового дерева на старое, и чужой класс снёс бы оформление
    // (например `.today__grid` превратился бы в служебный).
    const wrap = h('div', { class: host.getAttribute('class') || '' }, ...list);
    if (host.id) wrap.setAttribute('id', host.id);
    applyLivePatch(host, wrap, { doc: document, restore: false });
  }

  /** Секция: заголовок-суть + содержимое (или пустое состояние). */
  function fillSection(ctl, chips, children, emptyText) {
    ctl.setSummary(chips || []);
    const list = (children || []).filter(Boolean);
    patchInto(ctl.body, list.length ? list : [h('p', { class: 'today-empty small muted' }, emptyText || '')]);
  }

  /* --- сборка страницы --------------------------------------------------- */

  function buildHeader(nowSec) {
    const chips = counterChips(data || {});
    patchInto(chipsHost, chips.map((chip) => h('span', {
      class: ['chip', 'chip--' + chip.tone, 'today__chip', 'tnum'],
      'data-key': 'chip|' + chip.key,
    }, chip.text)));
    const gen = data && data.generated_at;
    const stampText = updatedText(gen);
    // Дата — обычным кеглем, «обновлено 01:45» — тише: это служебная строка,
    // а не часть названия дня (замечание к первому виду).
    const parts = [h('span', { class: 'today__day' }, dayTitle())];
    if (stampText) {
      parts.push(h('span', { class: 'today__sep', 'aria-hidden': 'true' }, '·'));
      parts.push(h('span', { class: 'today__stamp' }, stampText));
    }
    const sig = dayTitle() + '|' + stampText;
    if (leadHost.getAttribute('data-sig') !== sig) {
      leadHost.setAttribute('data-sig', sig);
      leadHost.replaceChildren(...parts);
    }
    title.setAttribute('aria-label', 'Сегодня, ' + dayTitle());
    return nowSec;
  }

  function buildHealth() {
    const problems = healthProblems(health);
    healthSec.element.hidden = !problems.shown;
    if (!problems.shown) return;
    healthSec.element.setAttribute('data-state', problems.tone);
    healthSec.element.classList.toggle('today-sec--fail', problems.tone === 'fail');
    healthSec.element.classList.toggle('today-sec--warn', problems.tone === 'warn');
    const first = problems.items[0] || null;
    // «одна строка с detail и hint» — в заголовке; полный список — в теле.
    const extra = h('span', { class: 'today-health__line small' },
      first ? (first.detail || first.title) : '',
      first && first.hint ? h('span', { class: 'today-health__hint' }, ' · ' + first.hint) : null);
    if (healthSec.extra && healthSec.extra.replaceChildren) {
      healthSec.extra.replaceChildren(extra);
    } else if (healthSec.extra) {
      healthSec.extra.appendChild(extra);
    }
    fillSection(healthSec,
      [{ text: problems.title, tone: problems.tone === 'fail' ? 'danger' : 'warn' }],
      problems.items.map((check) => h('div', {
        class: ['today-health__row', 'today-health__row--' + check.status],
        'data-key': 'health|' + check.id,
      },
      h('span', { class: 'today-health__mark', 'aria-hidden': 'true' }, icon(TONE_ICON[check.status] || 'warning', { size: 12 })),
      h('div', { class: 'today-health__text' },
        h('div', { class: 'today-health__title' }, check.title,
          check.since ? h('span', { class: 'today-health__since tiny muted tnum' }, ' · ' + agoText(check.since)) : null),
        h('div', { class: 'small muted' }, check.detail),
        check.hint ? h('div', { class: 'small today-health__hint' }, check.hint) : null))),
      'Проверки в порядке');
  }

  function buildNeedsYou(nowSec) {
    const items = (data && data.needs_you) || [];
    const view = needsYouVisible(items, NEEDS_YOU_LIMIT);
    const cards = view.visible.map((item) => needsYouNode(needsYouCard(item, nowSec)));
    if (view.rest.length) {
      const rest = view.rest.map((item) => needsYouNode(needsYouCard(item, nowSec)));
      if (showAll) cards.push(...rest);
      const more = h('button', {
        class: 'btn btn--ghost btn--sm today__more',
        type: 'button',
        'data-key': 'ny-more',
        'aria-expanded': showAll ? 'true' : 'false',
      }, showAll ? 'Свернуть' : 'Показать все ' + countNoun(view.total, FORMS.case));
      // Перерисовываем только эту секцию: состояние живёт в `showAll`, поэтому
      // следующий опрос покажет список так же, как его оставил человек.
      more.addEventListener('click', () => {
        showAll = !showAll;
        buildNeedsYou(Math.floor(Date.now() / 1000));
      });
      cards.push(more);
    }
    fillSection(needsSec,
      view.total ? [{ text: countNoun(view.total, FORMS.case), tone: 'warn' }] : [],
      view.total ? cards : [], EMPTY_TEXT.needs_you);
  }

  function buildDues(nowSec) {
    const overdue = (data && data.overdue) || [];
    const soon = (data && data.due_soon) || [];
    fillSection(overdueSec,
      overdue.length ? [{ text: countNoun(overdue.length, FORMS.case), tone: 'danger' }] : [],
      overdue.map((row) => dueNode(row, 'overdue')), EMPTY_TEXT.overdue);
    fillSection(soonSec,
      soon.length ? [{ text: countNoun(soon.length, FORMS.case), tone: 'info' }] : [],
      soon.map((row) => dueNode(row, 'soon')), EMPTY_TEXT.due_soon);
  }

  function buildChanged(nowSec) {
    const sum = changedSummary(data || {});
    const chips = sum.total ? [{ text: String(sum.total), tone: 'info' }] : [];
    fillSection(changedSec, chips, changedNodes(data || {}, nowSec), EMPTY_TEXT.changed);
  }

  function buildRunning(nowSec) {
    const items = (data && data.running) || [];
    fillSection(runningSec,
      items.length ? [{ text: countNoun(items.length, FORMS.request), tone: 'ok' }] : [],
      items.map((item) => runningNode(item)), EMPTY_TEXT.running);
  }

  function buildDuties(nowSec) {
    const items = (duties && duties.items) || [];
    const problems = items.filter((item) => item.problem && item.problem.kind).length;
    fillSection(dutiesSec,
      items.length ? [{ text: countNoun(items.length, FORMS.duty), tone: 'muted' }]
        .concat(problems ? [{ text: 'с проблемой: ' + problems, tone: 'danger' }] : []) : [],
      items.map((item) => dutyNode(item, nowSec)), EMPTY_TEXT.duties);
  }

  function render() {
    if (stopped) return;
    const nowSec = Math.floor(Date.now() / 1000);
    buildHeader(nowSec);
    buildHealth();
    buildNeedsYou(nowSec);
    buildDues(nowSec);
    buildChanged(nowSec);
    buildRunning(nowSec);
    buildDuties(nowSec);
    // Пустая «сегодняшняя» пустота: короткая строка вместо пяти заголовков.
    const quiet = isEmptyDay(data || {});
    patchInto(notesHost, quiet
      ? [h('div', { class: 'today-quiet' },
        icon('check', { size: 14 }),
        h('div', { class: 'today-quiet__text' },
          h('div', { class: 'today-quiet__title' }, EMPTY_TEXT.needs_you),
          h('div', { class: 'small muted' }, 'Просроченного нет, сроков на три дня нет, никто не работает.')))]
      : []);
  }

  /* --- загрузка ---------------------------------------------------------- */

  async function load(first) {
    if (first) {
      patchInto(bodyHost, [
        h('div', { class: 'today__col today__col--main' }, skeleton(3)),
        h('div', { class: 'today__col today__col--side' }, skeleton(2)),
      ]);
    }
    const [todayData, healthData, dutiesData] = await Promise.all([
      apiRef.get('/api/today'),
      apiRef.get('/api/health/integrations').catch(() => null),
      apiRef.get('/api/duties?room=all').catch(() => null),
    ]);
    if (stopped) return;
    data = todayData || null;
    health = healthData || null;
    duties = dutiesData || null;
    if (first) {
      bodyHost.replaceChildren(mainCol, sideCol);
    }
    render();
    const count = Number((data && data.counters && data.counters.needs_you) || 0);
    try { setTodayCount(count); } catch { /* сайдбар ещё не собран */ }
  }

  /** Отметка захода: через 3 с после показа, один раз за заход. */
  function scheduleSeen() {
    if (seenSent || seenTimer) return;
    seenTimer = setTimeout(() => {
      seenTimer = null;
      if (stopped || seenSent) return;
      seenSent = true;
      apiRef.post('/api/today/seen', {}).catch(() => { /* отметка не критична */ });
    }, SEEN_DELAY_MS);
  }

  let stopPoll = null;
  try {
    await load(true);
    if (stopped) return { unmount() {} };
    scheduleSeen();
    stopPoll = poll(() => load(false).catch((err) => {
      if (stopped) return;
      toast('Экран «Сегодня» не обновился: ' + String((err && err.message) || 'сервер не ответил'), 'warn');
    }), POLL_MS, { pauseHidden: true });
  } catch (err) {
    if (stopped) return { unmount() {} };
    bodyHost.replaceChildren(errorBanner('Не удалось открыть «Сегодня»: '
      + String((err && err.message) || 'сервер не ответил'),
    () => { bodyHost.replaceChildren(); load(true).catch(() => {}); }));
  }

  return {
    unmount() {
      stopped = true;
      if (stopPoll && typeof stopPoll.stop === 'function') stopPoll.stop();
      else if (typeof stopPoll === 'function') stopPoll();
      if (seenTimer) { clearTimeout(seenTimer); seenTimer = null; }
      for (const key of Object.keys(sections)) {
        if (sections[key] && typeof sections[key].destroy === 'function') sections[key].destroy();
      }
    },
  };
}

export default { mount, VIEW_CSS, POLL_MS, SEEN_DELAY_MS, dayTitle, updatedText };
