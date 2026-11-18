/**
 * request-worklog.js — блок «Ход работы» на странице запроса (W20-FB).
 *
 * Что показывает: кто работает прямо сейчас, что именно делает (действия
 * инструментов), о чём думает (рассуждения приглушённым курсивом), передачи
 * работы со стрелкой «Руководитель → Юрист: …» и промежуточные итоги сторожа.
 * Данные — `GET /api/requests/{id}/worklog` (docs/API.md §W20-WORKLOG), только
 * чтение: приложение ничего не запускает и не вызывает моделей.
 *
 * Правила, которые здесь важны:
 *  • Блок живёт СВОИМ узлом: страница вставляет его место один раз
 *    (`.req-worklog` с `data-keep-tree`), дальше владелец — этот модуль. Поэтому
 *    перерисовка страницы не сбрасывает ленту, прокрутку и раскрытые записи.
 *  • Никакой принудительной автопрокрутки: лента стоит там, где её оставил
 *    человек; новые записи показываются кнопкой «К новым · N». Если человек
 *    читает самый низ — новые записи просто появляются снизу (это не прыжок).
 *  • Не мигает: записи сверяются по `data-key` (id события). Совпавшая запись
 *    не заменяется, поэтому выделение текста и прокрутка не теряются.
 *  • Опрос лёгкий: 3 с пока работа идёт, 30 с после, пауза в скрытой вкладке
 *    (это делает `api.poll`). Режим «Выкл.» не опрашивает вовсе.
 *  • Переключатель «Подробно / Кратко / Выкл.» — личный, для этого экрана:
 *    хранится в localStorage и может только сузить настройку комнаты.
 *
 * Публично: `mountWorklog(host, ctx, {roomId, rid, detail, autoPoll})` →
 *   `{element, update(detail), refresh(), ready, destroy()}`.
 */

import { h, plural } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss, avatar, lsGet, lsSet } from '../ui.js';
import { poll as apiPoll } from '../api.js';

export const CSS_HREF = 'css/worklog.css';
/** Личный выбор уровня: 'full' | 'brief' | 'off'; пусто — как в комнате. */
export const LS_MODE = 'worklog:mode';
export const POLL_LIVE = 3000;
export const POLL_DONE = 30000;
export const MAX_ITEMS = 200;
export const FIRST_PAGE = 80;
export const MODE_LABEL = { full: 'Подробно', brief: 'Кратко', off: 'Выкл.' };
export const MODE_HINT = {
  full: 'Подробно: действия и рассуждения',
  brief: 'Кратко: только действия, без рассуждений',
  off: 'Не показывать и не спрашивать сервер',
};
/** Заголовок блока. Один смысл — одно место: так же называется и раздел настроек. */
export const TITLE = 'Ход работы';

const FINAL = new Set(['done', 'failed']);
const KINDS = new Set(['thought', 'action', 'result', 'message', 'status', 'handoff', 'digest']);
const ROLE_TEXT = { orchestrator: 'руководитель', critic: 'критик', observer: 'наблюдатель' };
const ROLE_SHORT = { orchestrator: 'рук.', critic: 'критик', observer: 'набл.' };

/* ─────────────────────────────── чистые помощники ─────────────────────────── */

/** Запрос закончен? (по статусу страницы запроса) */
export function isFinalStatus(status) {
  return FINAL.has(String(status === null || status === undefined ? '' : status));
}

/** Период опроса: пока работа идёт — часто, после — редко. */
export function pollDelay(status) {
  return isFinalStatus(status) ? POLL_DONE : POLL_LIVE;
}

/**
 * Что реально можно показать: личный выбор поверх настройки комнаты.
 * Сузить можно (Подробно → Кратко → Выкл.), расширить нельзя: если комната
 * выключила ленту или рассуждения, мы это уважаем и не обещаем больше.
 * @param {string} roomMode  'full' | 'brief' | 'off'
 * @param {string} local     'full' | 'brief' | 'off' | ''
 * @returns {'full'|'brief'|'off'}
 */
export function effectiveMode(roomMode, local) {
  const room = MODE_LABEL[roomMode] ? String(roomMode) : 'full';
  const own = MODE_LABEL[local] ? String(local) : '';
  if (room === 'off') return 'off';
  if (own === 'off') return 'off';
  if (room === 'brief') return 'brief';
  return own === 'brief' ? 'brief' : 'full';
}

/** Уровень для API: рассуждения только в «Подробно». */
export function levelOf(mode) {
  return mode === 'full' ? 'thoughts' : 'steps';
}

/** Отметка времени записи (сервер отдаёт `ts`; дайджест в демо — `at`). */
function tsOf(raw) {
  const n = Number(raw && (raw.ts !== undefined && raw.ts !== null ? raw.ts : raw.at));
  return isFinite(n) ? Math.floor(n) : 0;
}

/** Запись ленты в одном виде: id обязателен (он же `data-key` строки). */
export function normalizeItem(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const ts = tsOf(raw);
  const kindRaw = String(raw.kind || 'action');
  const kind = KINDS.has(kindRaw) ? kindRaw : 'action';
  const text = String(raw.text === null || raw.text === undefined ? '' : raw.text).trim();
  if (!text && kind !== 'result') return null;
  const profile = String(raw.profile || '');
  const id = String(raw.id || '').trim()
    || ['x', ts, profile, kind, text.slice(0, 40)].join('|');
  return {
    id, ts, kind, text,
    card_id: raw.card_id ? String(raw.card_id) : '',
    profile,
    label: String(raw.label || ''),
    role: String(raw.role || ''),
    tool: String(raw.tool || ''),
    ok: raw.ok === true ? true : (raw.ok === false ? false : null),
  };
}

/**
 * Склеить ленту: без дублей по id, по времени, не больше `limit` записей.
 * Более новая копия записи (из опроса) побеждает старую.
 * @param {Array} current  что уже показано
 * @param {Array} incoming что пришло
 * @param {number} [limit]
 */
export function mergeItems(current, incoming, limit = MAX_ITEMS) {
  const map = new Map();
  const order = [];
  for (const list of [current, incoming]) {
    for (const raw of Array.isArray(list) ? list : []) {
      const item = normalizeItem(raw);
      if (!item) continue;
      if (!map.has(item.id)) order.push(item.id);
      map.set(item.id, item);
    }
  }
  const out = order.map((id) => map.get(id));
  out.sort((a, b) => (a.ts - b.ts) || String(a.id).localeCompare(String(b.id)));
  const cap = Math.max(1, Number(limit) || MAX_ITEMS);
  return out.length > cap ? out.slice(out.length - cap) : out;
}

/** Подпись участника: готовая с сервера, иначе профиль. */
export function whoOf(row) {
  return String((row && (row.label || row.profile)) || 'участник');
}

/**
 * Сгруппировать ленту в «подходы» участников: подряд идущие записи одного
 * профиля — одна группа. Так видно, кто чем занимался, и при этом порядок
 * событий остаётся честным (по времени).
 * @param {Array} items
 * @returns {Array<{profile: string, label: string, role: string, items: Array}>}
 */
export function runsOf(items) {
  const out = [];
  for (const item of Array.isArray(items) ? items : []) {
    if (!item) continue;
    const profile = String(item.profile || '');
    const last = out[out.length - 1];
    if (last && profile && last.profile === profile) last.items.push(item);
    else out.push({ profile, label: whoOf(item), role: String(item.role || ''), items: [item] });
  }
  return out;
}

/** «2 мин», «1 ч 5 мин», «только что» — сколько длится текущее действие. */
export function elapsedSince(since, nowMs) {
  const start = Number(since);
  if (!isFinite(start) || start <= 0) return '';
  const now = Number(nowMs) || Date.now();
  const sec = Math.max(0, Math.floor(now / 1000 - start));
  if (sec < 60) return 'только что';
  if (sec < 3600) return `${Math.floor(sec / 60)} мин`;
  const hours = Math.floor(sec / 3600);
  const mins = Math.floor((sec % 3600) / 60);
  return mins ? `${hours} ч ${mins} мин` : `${hours} ч`;
}

/** Чем занят участник — коротко, как в строке «Сейчас работают…». */
export function stateWord(row, nowMs) {
  const state = String((row && row.state) || 'working');
  if (state === 'waiting') return 'ждёт вашего ответа';
  if (state === 'stuck') {
    const mins = Number(row && row.stuck_minutes);
    return mins ? `возможно завис: ${mins} мин без событий` : 'возможно завис';
  }
  if (state === 'idle') return 'свободен';
  const span = elapsedSince(row && row.since, nowMs);
  return span || 'работает';
}

/**
 * Хвост строки «Сейчас работают: …» — «Юрист — 2 мин, Сметчик — ждёт вашего ответа».
 * @param {Array} live
 * @param {number} [nowMs]
 */
export function liveListText(live, nowMs) {
  const rows = (Array.isArray(live) ? live : []).filter((r) => r && (r.profile || r.label));
  return rows.map((r) => `${whoOf(r)} — ${stateWord(r, nowMs)}`).join(', ');
}

/**
 * Строка индикатора целиком. Пусто — «сейчас никто не работает».
 * @param {Array} live
 * @param {{final?: boolean, nowMs?: number}} [opts]
 */
export function liveLine(live, opts = {}) {
  const rows = (Array.isArray(live) ? live : []).filter((r) => r && (r.profile || r.label));
  if (!rows.length) {
    return opts.final ? 'Работа закончена — новых событий не будет' : 'Сейчас никто не работает';
  }
  const working = rows.filter((r) => r.state === 'working');
  const stuck = rows.filter((r) => r.state === 'stuck');
  const other = rows.filter((r) => r.state !== 'working' && r.state !== 'stuck');
  const parts = [];
  if (working.length) parts.push('Сейчас работают: ' + liveListText(working, opts.nowMs));
  else if (other.length) parts.push('Сейчас ждут вашего ответа: ' + liveListText(other, opts.nowMs));
  // Зависшего не ставим в один ряд с работающими: это не «работает», это повод посмотреть доску.
  if (stuck.length) {
    parts.push((parts.length ? 'Похоже, завис: ' : 'Сейчас: ') + liveListText(stuck, opts.nowMs));
  }
  return parts.join('. ');
}

/** Дайджест сторожа в одном виде (поле `digest` ответа). */
export function digestOf(payload) {
  const d = payload && payload.digest;
  if (!d || typeof d !== 'object' || !String(d.text || '').trim()) return null;
  const ts = tsOf(d);
  return {
    id: String(d.id || ('digest-' + ts)),
    ts,
    text: String(d.text || ''),
    minutes: Number(d.minutes) || 0,
    done: Number(d.done) || 0,
    total: Number(d.total) || 0,
    workers: Array.isArray(d.workers) ? d.workers : [],
  };
}

/**
 * Передача работы «Руководитель → Юрист: «…»» — разложить на части,
 * чтобы нарисовать стрелкой между именами.
 * @param {string} text
 * @returns {{from: string, to: string, what: string}|null}
 */
export function handoffParts(text) {
  const raw = String(text === null || text === undefined ? '' : text).trim();
  const m = raw.match(/^(.{1,60}?)\s*(?:→|->)\s*(.{1,60}?)\s*:\s*([\s\S]*)$/);
  if (!m) return null;
  return { from: m[1].trim(), to: m[2].trim(), what: m[3].trim() };
}

/** Иконка по инструменту: одна строка, без технических имён на экране. */
export function toolIcon(tool) {
  const t = String(tool || '').toLowerCase();
  if (/web_search|browser|fetch|globe/.test(t)) return 'globe';
  if (/search|grep|find/.test(t)) return 'search';
  if (/read|file|cat|view/.test(t)) return 'file-text';
  if (/write|edit|patch|create_file/.test(t)) return 'edit';
  if (/shell|bash|terminal|exec|run/.test(t)) return 'terminal';
  if (/kanban|task|todo|checklist/.test(t)) return 'checklist';
  if (/message|send|chat/.test(t)) return 'comment';
  if (/note|memory/.test(t)) return 'note';
  return 'circle-filled';
}

/** Сколько записей писать в счётчике: «12 событий». */
export function countText(n) {
  return `${n} ${plural(n, ['событие', 'события', 'событий'])}`;
}

function setHidden(node, on) {
  if (!node) return;
  if (on) node.setAttribute('hidden', '');
  else node.removeAttribute('hidden');
}

function addAll(node, ...kids) {
  for (const kid of kids) if (kid) node.appendChild(kid);
  return node;
}

function clock(ts) {
  const sec = Number(ts) || 0;
  if (!sec) return '';
  const d = new Date(sec * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

/* ─────────────────────────────────── блок ─────────────────────────────────── */

/**
 * Смонтировать блок «Ход работы» в подготовленное место страницы.
 * @param {HTMLElement} host  место на странице (`.req-worklog`)
 * @param {object} ctx        контекст страницы (`api`, `toast`, `navigate`)
 * @param {{roomId?: string, rid: string, detail?: object, autoPoll?: boolean,
 *          poller?: Function}} [opts]
 */
export function mountWorklog(host, ctx, opts = {}) {
  const o = opts || {};
  const api = (ctx && ctx.api) || null;
  const rid = String(o.rid || '');
  const roomId = String(o.roomId || '');
  if (!host || !rid || !api) {
    return { element: null, update() {}, refresh: async () => {}, ready: Promise.resolve(null), destroy() {} };
  }

  ensureCss(CSS_HREF);

  const st = {
    roomMode: 'full',
    local: String(lsGet(LS_MODE, '') || ''),
    mode: 'full',
    level: 'thoughts',
    items: [],
    live: [],
    stuck: [],
    digest: null,
    cursor: '',
    before: '',
    more: false,
    loadedFull: false,
    final: isFinalStatus(o.detail && o.detail.status),
    open: !isFinalStatus(o.detail && o.detail.status),
    userToggled: false,
    autoCollapsed: false,
    atBottom: true,
    unseen: 0,
    expanded: new Set(),
    error: '',
    loading: true,
    loadingEarlier: false,
    disposed: false,
    lastLiveText: '',
    lastDigestId: '',
  };

  const rows = new Map();   // id записи → {node, sig}
  let poller = null;
  let seq = 0;              // ответы, пришедшие не по порядку, игнорируем

  /* ── каркас ─────────────────────────────────────────────── */

  const titleId = 'wl-title-' + String(rid).replace(/[^a-z0-9_-]/gi, '');
  const countEl = h('span', { class: 'wl__count tnum' }, '');
  const chipEl = h('span', { class: 'wl__chip' });
  const chev = icon('chevron-right', { size: 12, className: 'wl__chev' });
  const summary = h('summary', { class: 'wl__summary' },
    chev,
    h('span', { class: 'wl__title', id: titleId }, TITLE),
    countEl,
    chipEl);

  const liveHost = h('div', { class: 'wl__live', 'aria-live': 'polite' });
  const stuckHost = h('div', { class: 'wl__stuck' });
  const feed = h('div', {
    class: 'wl__feed',
    'data-scroll-key': 'worklog:' + rid,
    role: 'log',
    'aria-live': 'off',
    tabindex: '0',
    'aria-label': 'События хода работы',
  });
  const earlierBtn = h('button', { class: 'btn btn--ghost btn--sm wl__earlier', type: 'button' },
    icon('history', { size: 13 }), h('span', null, 'Показать раньше'));
  const newBtn = h('button', { class: 'wl__new', type: 'button', 'aria-hidden': 'true', hidden: true });
  const emptyEl = h('div', { class: 'wl__empty' }, 'Здесь появятся действия команды по этому запросу.');
  const errorEl = h('div', { class: 'wl__error', hidden: true });
  const digestEl = h('div', { class: 'wl__digest' });
  const hintEl = h('div', { class: 'wl__hint' },
    'Лента только читает записи агентов и ничего не запускает. Записи старше 200 не показываем.');

  const modesHost = h('div', { class: 'wl__modes', role: 'group', 'aria-label': 'Что показывать в ленте' });
  const modeBtns = new Map();
  for (const key of ['full', 'brief', 'off']) {
    const btn = h('button', {
      class: 'wl__mode', type: 'button', 'aria-pressed': 'false',
      title: MODE_HINT[key], dataset: { mode: key },
    }, MODE_LABEL[key]);
    btn.addEventListener('click', () => { setLocalMode(key); });
    modeBtns.set(key, btn);
    modesHost.appendChild(btn);
  }

  const feedWrap = h('div', { class: 'wl__feedwrap' }, earlierBtn, feed, newBtn);
  const body = h('div', { class: 'wl__body' },
    h('div', { class: 'wl__bar' },
      liveHost,
      modesHost),
    digestEl,
    stuckHost,
    feedWrap,
    emptyEl,
    errorEl,
    hintEl);

  const box = h('details', { class: 'wl__box', open: st.open ? true : null }, summary, body);
  // Итог сторожа при свёрнутом блоке должен быть виден — значит он не внутри
  // <details> (закрытый details не показывает содержимое вообще).
  setHidden(digestEl, true);
  const section = h('section', {
    class: 'card wl', 'data-worklog': '', 'data-keep-tree': '', 'aria-labelledby': titleId,
  }, box, digestEl);

  // Своё раскрытие/сворачивание: <details> + запоминание выбора человека.
  summary.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    setOpen(!st.open, { byUser: true });
  });

  feed.addEventListener('scroll', () => {
    const top = Number(feed.scrollTop) || 0;
    const total = Number(feed.scrollHeight) || 0;
    const seen = top + (Number(feed.clientHeight) || 0);
    st.atBottom = total - seen < 28;
    if (st.atBottom && st.unseen) { st.unseen = 0; paintNewBtn(); }
  });

  newBtn.addEventListener('click', () => {
    st.unseen = 0;
    st.atBottom = true;
    paintNewBtn();
    try { feed.scrollTop = Number(feed.scrollHeight) || 0; } catch { /* нет прокрутки */ }
  });

  earlierBtn.addEventListener('click', () => { loadEarlier().catch(() => {}); });

  function setOpen(open, { byUser = false } = {}) {
    st.open = Boolean(open);
    box.open = st.open;
    if (st.open) box.setAttribute('open', '');
    else box.removeAttribute('open');
    if (byUser) st.userToggled = true;
    if (st.open) {
      // Раскрыли — лента должна быть на экране целиком; ничего не грузим заново.
      try {
        if (typeof section.scrollIntoView === 'function' && byUser) {
          section.scrollIntoView({ block: 'nearest' });
        }
      } catch { /* нет прокрутки */ }
    }
  }

  /* ── режим ──────────────────────────────────────────────── */

  function roomCapHint(key) {
    if (st.roomMode === 'off') return 'Ход работы выключен в настройках комнаты';
    if (st.roomMode === 'brief' && key === 'full') {
      return 'Рассуждения выключены в настройках комнаты — включить можно на вкладке «Работа»';
    }
    return '';
  }

  function paintModes() {
    const mode = st.mode;
    for (const [key, btn] of modeBtns) {
      const roomBlocks = st.roomMode === 'off' || (st.roomMode === 'brief' && key === 'full');
      const unavailable = key === 'off' ? false : roomBlocks;
      const pressed = key === (st.local || (st.roomMode === 'brief' ? 'brief' : st.roomMode) || 'full');
      btn.classList.toggle('is-on', Boolean(pressed));
      btn.setAttribute('aria-pressed', pressed ? 'true' : 'false');
      btn.disabled = Boolean(unavailable);
      const hint = roomCapHint(key);
      if (hint) btn.setAttribute('title', hint);
      else btn.setAttribute('title', MODE_HINT[key]);
    }
    modesHost.setAttribute('data-mode', mode);
  }

  function setLocalMode(key) {
    const want = MODE_LABEL[key] ? key : 'full';
    const next = effectiveMode(st.roomMode, want);
    if (want === 'off') st.local = 'off';
    else if (want === 'brief') st.local = 'brief';
    else st.local = 'full';
    if (st.local === 'full' && st.roomMode !== 'full') st.local = '';
    lsSet(LS_MODE, st.local || undefined);
    const changed = next !== st.mode;
    st.mode = next;
    st.level = levelOf(next);
    st.loading = false;
    paintModes();
    if (changed) {
      resetFeed();
      if (next === 'off') {
        paintLive();
        paintEmpty();
      } else {
        if (poller && typeof poller.setDelay === 'function') poller.setDelay(pollDelay(st.final ? 'done' : 'working'));
        refresh({ reset: true }).catch(() => {});
      }
    }
  }

  /* ── отрисовка строки ленты ─────────────────────────────── */

  function roleChip(item) {
    const text = ROLE_SHORT[item.role] || '';
    if (!text || item.role === 'worker') return null;
    return h('span', { class: 'wl-item__role', title: ROLE_TEXT[item.role] || text }, text);
  }

  function timeEl(ts) {
    const text = clock(ts);
    if (!text) return null;
    return h('time', { class: 'wl-item__time tnum', datetime: new Date((Number(ts) || 0) * 1000).toISOString() }, text);
  }

  function thoughtNode(item) {
    const long = String(item.text).length > 210;
    const text = h('div', {
      class: 'wl-item__text wl-item__text--thought' + (long && !st.expanded.has(item.id) ? ' is-clamped' : ''),
    }, item.text);
    if (!long) return [text, null];
    const more = h('button', {
      class: 'wl-item__more', type: 'button',
      'aria-expanded': st.expanded.has(item.id) ? 'true' : 'false',
    }, st.expanded.has(item.id) ? 'свернуть' : 'ещё');
    more.addEventListener('click', () => {
      if (st.expanded.has(item.id)) st.expanded.delete(item.id);
      else st.expanded.add(item.id);
      const open = st.expanded.has(item.id);
      text.classList.toggle('is-clamped', !open);
      more.textContent = open ? 'свернуть' : 'ещё';
      more.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    return [text, more];
  }

  function digestNode(item) {
    return h('div', { class: 'wl-item wl-item--digest', dataset: { key: item.id } },
      h('div', { class: 'wl-item__digestico' }, icon('clock', { size: 13 })),
      h('div', { class: 'wl-item__body' },
        h('div', { class: 'wl-item__digesthead' },
          h('span', { class: 'wl-item__digestlabel' }, 'Промежуточный итог'),
          timeEl(item.ts)),
        h('div', { class: 'wl-item__text' }, item.text)));
  }

  function handoffNode(item, prevProfile) {
    const parts = handoffParts(item.text);
    const showWho = Boolean(item.profile) && item.profile !== prevProfile;
    const body = h('div', { class: 'wl-item__body' });
    if (showWho) {
      body.appendChild(h('div', { class: 'wl-item__head' },
        h('span', { class: 'wl-item__who' }, whoOf(item)), roleChip(item), timeEl(item.ts)));
    }
    const line = h('div', { class: 'wl-item__text wl-item__text--handoff' });
    if (parts) {
      // Обычный поток текста, без flex-gap: иначе перед двоеточием появляется
      // лишний пробел («Финансы : «…»» вместо «Финансы: «…»»).
      addAll(line,
        h('span', { class: 'wl-item__from' }, parts.from),
        h('span', { class: 'wl-item__arrow' }, ' → '),
        h('span', { class: 'wl-item__to' }, parts.to));
      if (parts.what) line.appendChild(h('span', { class: 'wl-item__what' }, ': ' + parts.what));
    } else {
      line.appendChild(h('span', null, item.text));
    }
    body.appendChild(line);
    if (!showWho) body.appendChild(h('div', { class: 'wl-item__tail' }, timeEl(item.ts)));
    return h('div', {
      class: 'wl-item wl-item--handoff', dataset: { key: item.id, wlProfile: item.profile || '' },
    }, h('div', { class: 'wl-item__ava' }), body);
  }

  function buildRow(item, prevProfile) {
    if (item.kind === 'digest') return digestNode(item);
    if (item.kind === 'handoff') return handoffNode(item, prevProfile);

    const showWho = Boolean(item.profile) && item.profile !== prevProfile;
    const body = h('div', { class: 'wl-item__body' });
    if (showWho) {
      body.appendChild(h('div', { class: 'wl-item__head' },
        h('span', { class: 'wl-item__who' }, whoOf(item)),
        roleChip(item),
        timeEl(item.ts)));
    }

    const textWrap = h('div', { class: 'wl-item__line' });
    let extra = null;
    if (item.kind === 'thought') {
      const [node, more] = thoughtNode(item);
      textWrap.appendChild(node);
      extra = more;
    } else if (item.kind === 'action') {
      addAll(textWrap,
        h('span', { class: 'wl-item__ico', title: 'Действие' }, icon(toolIcon(item.tool), { size: 13 })),
        h('span', { class: 'wl-item__text' }, item.text));
    } else if (item.kind === 'result') {
      const bad = item.ok === false;
      addAll(textWrap,
        h('span', { class: 'wl-item__ico' + (bad ? ' is-bad' : ' is-ok'), title: bad ? 'Ошибка' : 'Готово' },
          icon(bad ? 'error' : 'check', { size: 13 })),
        h('span', { class: 'wl-item__text wl-item__text--result' }, item.text));
    } else if (item.kind === 'status') {
      textWrap.appendChild(h('span', { class: 'wl-item__badge' }, item.text));
    } else {
      textWrap.appendChild(h('span', { class: 'wl-item__text' }, item.text));
    }
    body.appendChild(textWrap);
    if (extra) body.appendChild(extra);
    if (!showWho) body.appendChild(h('div', { class: 'wl-item__tail' }, timeEl(item.ts)));

    const ava = h('div', { class: 'wl-item__ava' },
      showWho ? avatar(whoOf(item), { size: 26, title: whoOf(item) }) : null);

    return h('div', {
      class: 'wl-item wl-item--' + item.kind + (showWho ? ' wl-item--start' : ''),
      dataset: { key: item.id, wlProfile: item.profile || '' },
    }, ava, body);
  }

  function sigOf(item, prevProfile) {
    return [item.kind, item.profile, item.label, item.role, item.tool,
      item.ok === null ? '' : String(item.ok), item.ts, String(item.text).length,
      st.expanded.has(item.id) ? 'x' : '', prevProfile].join('\u0000');
  }

  /* ── отрисовка ленты ────────────────────────────────────── */

  function removeNode(node) {
    if (!node) return;
    if (node.parentNode && typeof node.parentNode.removeChild === 'function') node.parentNode.removeChild(node);
    else if (typeof node.remove === 'function') node.remove();
  }

  function rebuildFeed() {
    const wasAtBottom = st.atBottom;
    for (const row of rows.values()) removeNode(row.node);
    rows.clear();
    feed.replaceChildren();
    appendRows(st.items);
    if (wasAtBottom) scrollToBottom();
    paintCount();
    paintEmpty();
  }

  /**
   * Добавить/обновить записи. Существующие строки не трогаем (не мигает).
   * @returns {number} сколько новых записей добавилось
   */
  function appendRows(list, { startPrev = null, alive: aliveList = null } = {}) {
    let added = 0;
    let prev = startPrev;
    for (const item of list) {
      const prevProfile = prev ? prev.profile : '';
      const sig = sigOf(item, prevProfile);
      const exist = rows.get(item.id);
      if (exist && exist.sig === sig) { prev = item; continue; }
      const node = buildRow(item, prevProfile);
      if (exist) {
        if (exist.node.parentNode) exist.node.parentNode.replaceChild(node, exist.node);
        else feed.appendChild(node);
        rows.set(item.id, { node, sig });
      } else {
        feed.appendChild(node);
        rows.set(item.id, { node, sig });
        added += 1;
      }
      prev = item;
    }
    // Лимит: убираем записи, которых больше нет в списке (полный список, не порция).
    const alive = new Set((aliveList || list).map((i) => i.id));
    for (const [id, row] of Array.from(rows.entries())) {
      if (alive.has(id)) continue;
      removeNode(row.node);
      rows.delete(id);
    }
    paintCount();
    paintEmpty();
    return added;
  }

  function scrollToBottom() {
    try { feed.scrollTop = Number(feed.scrollHeight) || 0; } catch { /* нет прокрутки */ }
  }

  function paintCount() {
    const n = st.items.length;
    if (!n) { countEl.textContent = ''; countEl.removeAttribute('title'); return; }
    countEl.textContent = countText(n) + (st.more ? ' · есть раньше' : '');
    if (st.more) countEl.setAttribute('title', 'Показаны последние ' + n + ' — более ранние можно догрузить кнопкой «Показать раньше»');
    else countEl.removeAttribute('title');
  }

  function paintEmpty() {
    const show = !st.items.length;
    setHidden(emptyEl, !show);
    if (!show) return;
    if (st.loading) {
      emptyEl.textContent = 'Читаю, что делала команда…';
    } else if (st.mode === 'off') {
      if (st.roomMode === 'off') {
        const open = h('button', { class: 'btn btn--secondary btn--sm wl__offbtn', type: 'button' },
          'Открыть настройки комнаты');
        open.addEventListener('click', () => { openRoomSettings().catch(() => {}); });
        emptyEl.replaceChildren(
          h('span', null, 'Ход работы выключен в настройках комнаты. Включить можно там же, на вкладке «Работа». '),
          open);
      } else {
        emptyEl.textContent = 'Ход работы выключен для этого экрана. Включить обратно — кнопка «Подробно» или «Кратко» выше.';
      }
    } else {
      emptyEl.textContent = 'Пока событий нет: команда ещё не начала работу по этому запросу.';
    }
  }

  function paintNewBtn() {
    if (st.unseen > 0) {
      setHidden(newBtn, false);
      newBtn.setAttribute('aria-hidden', 'false');
      newBtn.replaceChildren(icon('chevron-down', { size: 13 }), h('span', null, `К новым · ${st.unseen}`));
    } else {
      setHidden(newBtn, true);
      newBtn.setAttribute('aria-hidden', 'true');
      newBtn.replaceChildren();
    }
  }

  function paintOlderBtn() {
    const canOlder = st.mode !== 'off' && st.more && st.items.length < MAX_ITEMS;
    setHidden(earlierBtn, !canOlder);
    earlierBtn.disabled = st.loadingEarlier;
    const label = earlierBtn.querySelector ? earlierBtn.querySelector('span') : null;
    if (label) label.textContent = st.loadingEarlier ? 'Читаю…' : 'Показать раньше';
  }

  function paintLive() {
    const rowsLive = (Array.isArray(st.live) ? st.live : []).filter((r) => r && (r.profile || r.label));
    // «Сейчас никто не работает» при выключенной ленте — неправда: мы просто не смотрим.
    const off = st.mode === 'off';
    setHidden(liveHost, off);
    if (off) {
      liveHost.replaceChildren();
    } else {
      const text = liveLine(st.live, { final: st.final });
      const avatars = rowsLive.slice(0, 5).map((r) =>
        avatar(whoOf(r), { size: 22, title: `${whoOf(r)} — ${stateWord(r, Date.now())}` }));
      const line = h('span', {
        class: 'wl__livetext',
        title: rowsLive.map((r) => (r.last_action ? `${whoOf(r)}: ${r.last_action}` : whoOf(r))).join(' · '),
      }, text);
      liveHost.replaceChildren(...avatars, line);
      liveHost.classList.toggle('wl__live--idle', !rowsLive.length);
    }

    const busy = rowsLive.filter((r) => r.state === 'working').length;
    const waiting = rowsLive.filter((r) => r.state === 'waiting').length;
    const stuckCount = rowsLive.filter((r) => r.state === 'stuck').length;
    if (st.mode === 'off') {
      chipEl.textContent = 'выключено';
      chipEl.className = 'wl__chip wl__chip--quiet';
    } else if (busy) {
      chipEl.textContent = `работают: ${busy}`;
      chipEl.className = 'wl__chip wl__chip--live';
    } else if (waiting) {
      chipEl.textContent = `ждут ответа: ${waiting}`;
      chipEl.className = 'wl__chip wl__chip--wait';
    } else if (stuckCount) {
      chipEl.textContent = `завис: ${stuckCount}`;
      chipEl.className = 'wl__chip wl__chip--wait';
    } else if (st.final) {
      chipEl.textContent = 'готово';
      chipEl.className = 'wl__chip wl__chip--quiet';
    } else {
      chipEl.textContent = '';
      chipEl.className = 'wl__chip';
    }

    setHidden(hintEl, st.mode === 'off');
    setHidden(feedWrap, st.mode === 'off');
    paintStuck();
    paintDigestStrip();
  }

  function paintStuck() {
    const list = Array.isArray(st.stuck) ? st.stuck : [];
    if (!list.length) { stuckHost.replaceChildren(); setHidden(stuckHost, true); return; }
    setHidden(stuckHost, false);
    stuckHost.replaceChildren(...list.slice(0, 3).map((row) => {
      const kick = h('button', {
        class: 'btn btn--ghost btn--sm wl__kick', type: 'button',
        title: 'Один раз запустить готовые задачи, не дожидаясь минутного таймера',
      }, 'Проверить доску сейчас');
      kick.addEventListener('click', () => { runKick(kick).catch(() => {}); });
      const text = String(row.text || `возможно завис: ${row.minutes || 0} мин без активности`);
      return h('div', { class: 'wl__stuckrow' },
        icon('warning', { size: 13 }),
        h('span', { class: 'wl__stucktext' }, `${row.label || row.profile || 'участник'}: ${text}`),
        kick);
    }));
  }

  /** Настройки комнаты тем же диалогом, что на вкладке «Команда» (раздел «Ход работы»). */
  async function openRoomSettings() {
    const mod = await import('./room-dialog.js');
    if (!mod || typeof mod.openRoomDialog !== 'function') throw new Error('нет диалога комнаты');
    const room = await api.get('/api/rooms/' + encodeURIComponent(roomId));
    // Сразу на вкладке «Работа»: именно там раздел «Ход работы».
    await mod.openRoomDialog(ctx, room, { focus: 'work' });
    // Настройку могли поменять — перечитываем комнату ДО refresh: иначе в режиме
    // «Выключено» refresh выйдет сразу и не узнает, что ленту включили обратно.
    const after = await api.get('/api/rooms/' + encodeURIComponent(roomId)).catch(() => null);
    const mode = after && after.worklog && after.worklog.mode;
    if (mode) {
      st.roomMode = String(mode);
      const next = effectiveMode(st.roomMode, st.local);
      if (next !== st.mode) {
        st.mode = next;
        st.level = levelOf(next);
        resetFeed();
        if (poller && typeof poller.setDelay === 'function') {
          poller.setDelay(pollDelay(st.final ? 'done' : 'working'));
        }
      }
    }
    await refresh({ reset: true });
  }

  async function runKick(btn) {
    if (btn) btn.disabled = true;
    try {
      const mod = await import('./kick-panel.js');
      const report = await mod.kickNow(api, rid);
      const text = typeof mod.kickText === 'function' ? mod.kickText(report) : '';
      if (ctx && typeof ctx.toast === 'function') ctx.toast(text || 'Проверил доску', 'ok');
    } catch (err) {
      if (ctx && typeof ctx.toast === 'function') ctx.toast('Не удалось проверить доску', 'danger');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function paintDigestStrip() {
    const d = st.digest;
    const show = Boolean(d) && !st.open;
    setHidden(digestEl, !show);
    if (!show) { digestEl.replaceChildren(); return; }
    digestEl.replaceChildren(
      icon('clock', { size: 12 }),
      h('span', { class: 'wl__digesttext', title: d.text }, d.text));
  }

  function paintError() {
    setHidden(errorEl, !st.error);
    if (st.error) errorEl.replaceChildren(icon('warning', { size: 12 }), h('span', null, st.error));
  }

  function paintAll() {
    // Пустая лента при «Выключено» не должна занимать полэкрана.
    setHidden(feedWrap, st.mode === 'off');
    paintModes();
    paintLive();
    paintOlderBtn();
    paintNewBtn();
    paintError();
    paintEmpty();
    paintCount();
    paintDigestStrip();
  }

  function resetFeed() {
    st.items = [];
    st.cursor = '';
    st.before = '';
    st.more = false;
    st.loadedFull = false;
    st.unseen = 0;
    st.atBottom = true;
    rows.clear();
    feed.replaceChildren();
    paintAll();
  }

  /* ── данные ─────────────────────────────────────────────── */

  function path({ since = '', limit = FIRST_PAGE, before = '' } = {}) {
    const q = ['limit=' + encodeURIComponent(String(limit))];
    if (st.level) q.push('level=' + encodeURIComponent(st.level));
    if (since) q.push('since=' + encodeURIComponent(since));
    if (before) q.push('before=' + encodeURIComponent(before));
    return `/api/requests/${encodeURIComponent(rid)}/worklog?` + q.join('&');
  }

  /**
   * Прочитать ленту.
   * @param {{reset?: boolean}} [options]
   */
  async function refresh(options = {}) {
    if (st.disposed || !api) return null;
    if (st.mode === 'off') { paintAll(); return null; }
    const mySeq = ++seq;
    const reset = Boolean(options.reset);
    try {
      const res = await api.get(path({ since: reset ? '' : st.cursor }));
      if (st.disposed || mySeq !== seq) return null;

      const mode = String((res && res.mode) || '');
      if (MODE_LABEL[mode] && mode !== st.roomMode) {
        st.roomMode = mode;
        const next = effectiveMode(st.roomMode, st.local);
        if (next !== st.mode) {
          st.mode = next;
          st.level = levelOf(next);
          resetFeed();
          if (next === 'off') { st.loading = false; paintAll(); return res; }
        }
      }

      st.loading = false;
      st.error = '';
      st.stuck = Array.isArray(res && res.stuck) ? res.stuck : [];
      const stuckMinutes = new Map();
      for (const row of st.stuck) {
        const key = String((row && (row.card_id || row.profile)) || '');
        if (key) stuckMinutes.set(key, Number(row.minutes) || 0);
      }
      st.live = Array.isArray(res && res.live) ? res.live.map((row) => {
        const profile = String(row.profile || '');
        const cardId = row.card_id ? String(row.card_id) : '';
        return {
          profile,
          label: String(row.label || ''),
          role: String(row.role || ''),
          card_id: cardId,
          state: String(row.state || 'working'),
          since: Number(row.since) || 0,
          last_action: String(row.last_action || ''),
          stuck: Boolean(row.stuck),
          stuck_minutes: stuckMinutes.get(cardId) || stuckMinutes.get(profile) || 0,
        };
      }) : [];
      st.digest = digestOf(res);
      if (res && typeof res.cursor === 'string' && res.cursor) st.cursor = res.cursor;
      if (res && res.more !== undefined) st.more = Boolean(res.more);
      if (reset) {
        st.more = res && res.more !== undefined ? Boolean(res.more) : st.more;
        // Курсор «раньше» берём ТОЛЬКО из первого ответа: в ответе опроса он
        // посчитан по странице новых записей и сломал бы историю.
        st.before = res && typeof res.before === 'string' ? res.before : '';
      }

      let incoming = Array.isArray(res && res.items) ? res.items : [];
      // «Кратко» — только действия. Сервер в комнате с полным режимом отдаёт
      // реплики (message) даже при level=steps, а обещание переключателя — «без
      // рассуждений и реплик». Поэтому режем их здесь же.
      if (st.mode === 'brief') {
        incoming = incoming.filter((row) => String((row && row.kind) || '') !== 'message'
          && String((row && row.kind) || '') !== 'thought');
      }
      const wasAtBottom = st.atBottom;
      if (reset) {
        st.items = [];
        rows.clear();
        feed.replaceChildren();
      }
      const before = st.items.length;
      const startPrev = before ? st.items[before - 1] : null;
      st.items = mergeItems(st.items, incoming);
      const added = appendRows(st.items.slice(before), { startPrev, alive: st.items });
      if (added && !wasAtBottom) {
        st.unseen += added;
        paintNewBtn();
      } else if (added && wasAtBottom) {
        scrollToBottom();
      }
      if (reset && added) scrollToBottom();

      paintModes();
      paintLive();
      paintOlderBtn();
      paintError();
      paintEmpty();
      return res;
    } catch (err) {
      if (st.disposed || mySeq !== seq) return null;
      st.loading = false;
      // Ошибку показываем тихо и только если лента уже была — иначе человек
      // решит, что «Хода работы» нет вовсе.
      st.error = String((err && err.message) || 'Лента временно недоступна');
      paintError();
      paintEmpty();
      return null;
    }
  }

  async function loadEarlier() {
    if (st.loadingEarlier || st.mode === 'off' || !api) return;
    if (st.items.length >= MAX_ITEMS) { st.more = false; paintOlderBtn(); return; }
    st.loadingEarlier = true;
    paintOlderBtn();
    const prevHeight = Number(feed.scrollHeight) || 0;
    const prevTop = Number(feed.scrollTop) || 0;
    const mySeq = ++seq;
    try {
      // Серверный курсор «раньше» (`before` из прошлого ответа) отдаёт ровно
      // предыдущую страницу — цепочкой, без повторов и без прыжка позиции.
      // Сервер старой версии курсора не даёт — тогда расширяем окно до 200.
      const useCursor = Boolean(st.before);
      const res = useCursor
        ? await api.get(path({ before: st.before, limit: FIRST_PAGE }))
        : await api.get(path({ limit: MAX_ITEMS }));
      if (st.disposed || mySeq !== seq) return;
      let incoming = Array.isArray(res && res.items) ? res.items : [];
      if (st.mode === 'brief') {
        incoming = incoming.filter((row) => String((row && row.kind) || '') !== 'message'
          && String((row && row.kind) || '') !== 'thought');
      }
      if (res && res.more !== undefined) st.more = Boolean(res.more);
      if (useCursor) st.before = typeof res.before === 'string' ? res.before : '';
      else st.loadedFull = st.items.length + incoming.length >= MAX_ITEMS;
      st.items = mergeItems(st.items, incoming);
      if (st.items.length >= MAX_ITEMS) { st.loadedFull = true; st.more = false; }
      rebuildFeed();
      const nextHeight = Number(feed.scrollHeight) || 0;
      try { feed.scrollTop = prevTop + (nextHeight - prevHeight); } catch { /* нет прокрутки */ }
      paintAll();
    } catch (err) {
      st.error = String((err && err.message) || 'Не удалось прочитать более ранние записи');
      paintError();
    } finally {
      st.loadingEarlier = false;
      paintOlderBtn();
    }
  }

  /* ── жизнь блока ────────────────────────────────────────── */

  function update(detail) {
    if (!detail || st.disposed) return;
    const status = String(detail.status || '');
    const final = isFinalStatus(status);
    if (final === st.final) return;
    st.final = final;
    if (poller && typeof poller.setDelay === 'function') poller.setDelay(pollDelay(status));
    // Готово — сворачиваем один раз (если человек сам не трогал переключатель).
    if (final && !st.userToggled && !st.autoCollapsed) {
      st.autoCollapsed = true;
      setOpen(false);
      paintDigestStrip();
    }
    if (!final && !st.userToggled && !st.open) {
      // Запрос снова в работе (повтор/доработка) — раскрываем обратно.
      setOpen(true);
    }
    paintLive();
  }

  setOpen(st.open);
  host.replaceChildren(section);

  // Первичное чтение + опрос. `autoPoll:false` — только для тестов.
  const ready = (async () => {
    st.mode = effectiveMode(st.roomMode, st.local);
    st.level = levelOf(st.mode);
    paintAll();
    if (st.mode === 'off') { st.loading = false; paintEmpty(); }
    const res = st.mode === 'off' ? null : await refresh({ reset: true });
    if (o.autoPoll !== false) {
      const opts = { pauseHidden: true, immediate: false };
      const factory = typeof o.poller === 'function' ? o.poller : apiPoll;
      poller = factory(tick, pollDelay(st.final ? 'done' : 'working'), opts);
    }
    return res;
  })();

  async function tick() {
    if (st.disposed) return;
    if (st.mode === 'off') return;
    await refresh();
  }

  return {
    element: section,
    ready,
    update,
    refresh,
    setFinal(status) { update({ status }); },
    destroy() {
      st.disposed = true;
      seq += 1;
      if (poller && typeof poller.stop === 'function') poller.stop();
      poller = null;
      if (section.parentNode) section.parentNode.removeChild(section);
    },
  };
}

export default mountWorklog;
