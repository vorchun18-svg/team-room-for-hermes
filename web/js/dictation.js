/**
 * dictation.js — микрофон в любом поле ввода: запись → распознавание → вставка
 * текста В ПОЗИЦИЮ КУРСОРА (или вместо выделенного).
 *
 * Контракт сервера — §3 WAVE7-RULES (`/api/stt/status`, `/api/stt/transcribe`)
 * и §3.6 docs/API.md (части длинной записи, WAVE13 §2).
 * Аудио не покидает компьютер: браузер шлёт его на свой же 127.0.0.1.
 *
 * Как устроена длинная запись (после того как шестиминутная диктовка пропала):
 *   1. лимит записи берётся из настроек Hermes (`/api/stt/status` →
 *      `max_recording_s`), а не из своей константы; меньше 10 минут не бывает;
 *   2. запись режется на части по `part_seconds` (2 минуты). Каждая часть —
 *      самостоятельный файл: сразу сохраняется на диск сервера
 *      (`/api/stt/recordings/<rid>/parts/<n>`) и в IndexedDB браузера, и только
 *      потом распознаётся;
 *   3. достигли лимита, оборвалась сеть, страница закрылась — запись НЕ
 *      выбрасывается: части уже сохранены, текст поднимается кнопкой
 *      «Распознать ещё раз» (и «Распознать запись» для незаконченной);
 *   4. части распознаются по очереди, рядом с полем видно «Распознано 3 из 7
 *      минут», поле при этом не блокируется.
 *
 * Публично:
 *   createMic(target, opts) -> HTMLButtonElement   кнопка микрофона для поля
 *   enhanceAll(root) -> number                     добавить кнопки во все поля
 *   startAutoEnhance() -> MutationObserver|null    следить за новыми полями
 *   insertAtCursor(el, text) -> {inserted,start,end,raw}   (для тестов)
 *   formatClock(ms), nearLimit(ms,max), shouldAutoStop(ms,max)  (чистые)
 *   settingsFromStatus(stt), recordSettings(), resetSettings()  лимит из Hermes
 *
 * Правила: только токены/классы дизайн-системы, тексты — короткие по-русски,
 * одна запись на всё приложение, поле во время расшифровки НЕ блокируется.
 */

import { h, uid } from './util.js';
import { createPanel } from './dictation-panel.js';
import { enhanceAll, attachMic, startAutoEnhance, stopAutoEnhance } from './dictation-auto.js';
import * as store from './dictation-store.js';

/* ─────────────────────────────────────────────────────────── настройки */

/** Запасной лимит записи: столько же, сколько требует Михаил (10 минут). */
export const MAX_RECORD_MS = 10 * 60 * 1000;
/** Меньше этого лимит не опускаем, даже если в Hermes стоит 120 секунд. */
export const MIN_RECORD_MS = 10 * 60 * 1000;
/** Длина части записи по умолчанию (сервер присылает свою: `part_seconds`). */
export const DEFAULT_PART_MS = 2 * 60 * 1000;
const PART_MIN_MS = 10 * 1000;

const WARN_BEFORE_MS = 30 * 1000;          // за 30 с — подсказка
const TICK_MS = 250;
const SLOW_MS = 4000;                      // «первый раз — до минуты»
const CSS_HREF = 'css/dictation.css';
const TRANS_URL = '/api/stt/transcribe';
const STATUS_URL = '/api/stt/status';
const REC_URL = '/api/stt/recordings';
const SETTINGS_TTL_MS = 60 * 1000;

const ERR_PERMISSION = 'Разрешите микрофон: значок замка рядом с адресом → Микрофон → Разрешить. Пока можно печатать вручную.';
const ERR_NO_MIC = 'Микрофон не найден. Подключите его и попробуйте снова — пока можно печатать вручную.';
const ERR_BUSY_MIC = 'Микрофон отключился или занят другой программой. Проверьте, что он подключён, и нажмите «Надиктовать» ещё раз.';
const ERR_UNSUPPORTED = 'Браузер не умеет записывать с микрофона. Откройте приложение в Chrome — или печатайте вручную.';
const ERR_SERVER = 'Распознавание речи не готово — сервер ещё запускается. Попробуйте через минуту, пока можно печатать вручную.';
const ERR_EMPTY = 'Речь не обнаружена. Попробуйте ещё раз — или напечатайте вручную.';
const ERR_NET = 'Не удалось отправить запись. Текст не пропал — нажмите «Распознать ещё раз».';
const ERR_KEPT = 'Запись сохранена — нажмите «Распознать ещё раз», текст не пропадёт.';
const MSG_LOADING = 'Загружаю распознавание речи… первый раз — до минуты';
const HINT_REC = 'Говорите — текст встанет туда, где курсор.';
const HINT_KEEP = 'Поле не заблокировано — можно печатать.';

// Микрофон отвалился (выдернули, устройство заняли, драйвер упал). Браузер сам
// останавливает запись — говорим об этом и не теряем то, что успели записать.
const MSG_MIC_LOST = 'Микрофон отключился — записанное до этого сохранено.';
const MSG_MIC_LOST_EMPTY = 'Микрофон отключился до начала записи — говорить не удалось. Проверьте, что он подключён, и нажмите «Надиктовать» ещё раз.';
const MSG_MIC_LOST_OK = 'Микрофон отключился. Записанное до этого распознано — текст в поле.';
// Запись, которой уже нет на диске (сервер хранит их трое суток): строку убираем,
// чтобы она не всплывала снова и снова.
const MSG_RECORD_GONE = 'Запись уже удалена — на диске хранится трое суток. Надиктуйте заново, это быстро.';

/* ─────────────────────────────────────────────────────────── утилиты */

function warn(...args) {
  if (typeof console !== 'undefined' && console.warn) console.warn('[диктовка]', ...args);
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Двузначные секунды/минуты: 0:07, 10:00. */
export function formatClock(ms) {
  const total = Math.max(0, Math.round(Number(ms) / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m + ':' + (s < 10 ? '0' + s : String(s));
}

/** Пора предупредить, что запись скоро закончится. */
export function nearLimit(elapsedMs, maxMs = MAX_RECORD_MS) {
  const left = Number(maxMs) - Number(elapsedMs);
  return left > 0 && left <= WARN_BEFORE_MS;
}

/** Пора остановиться сами: предел записи достигнут (запись уйдёт на распознавание). */
export function shouldAutoStop(elapsedMs, maxMs = MAX_RECORD_MS) {
  return Number(elapsedMs) >= Number(maxMs);
}

/* ───────────────────────────────── лимит из настроек Hermes (не свои) */

const FALLBACK_SETTINGS = { maxMs: MAX_RECORD_MS, partMs: DEFAULT_PART_MS,
                            source: 'fallback', hermesS: null, at: 0 };
let sttSettings = { ...FALLBACK_SETTINGS };
let settingsPromise = null;

/**
 * Что берём из `/api/stt/status` (значения приходят из config.yaml Hermes).
 * Чистая функция — её проверяют тесты.
 * @param {object} stt ответ /api/stt/status
 * @param {object} [previous] прежние настройки (если сервер промолчал)
 * @returns {{maxMs: number, partMs: number, source: string, hermesS: number|null, at: number}}
 */
export function settingsFromStatus(stt, previous) {
  const prev = previous || sttSettings || FALLBACK_SETTINGS;
  const info = stt && typeof stt === 'object' ? stt : {};
  const maxS = Number(info.max_recording_s);
  const partS = Number(info.part_seconds);
  const hermesS = Number(info.max_recording_hermes_s);
  const maxMs = Number.isFinite(maxS) && maxS > 0
    ? Math.max(MIN_RECORD_MS, Math.round(maxS * 1000))
    : prev.maxMs;
  let partMs = Number.isFinite(partS) && partS > 0 ? Math.round(partS * 1000) : prev.partMs;
  partMs = Math.min(Math.max(PART_MIN_MS, partMs), maxMs);
  return {
    maxMs,
    partMs,
    source: info.max_recording_source ? String(info.max_recording_source) : prev.source,
    hermesS: Number.isFinite(hermesS) && hermesS > 0 ? hermesS : null,
    at: Date.now(),
  };
}

/** Текущие настройки записи (для тестов и подписи в настройках). */
export function recordSettings() {
  return { ...sttSettings };
}

/** Сбросить кеш настроек (тесты, смена профиля). */
export function resetSettings() {
  sttSettings = { ...FALLBACK_SETTINGS };
  settingsPromise = null;
}

async function readStatus() {
  try {
    const res = await fetch(STATUS_URL, { method: 'GET', headers: { Accept: 'application/json' },
                                          credentials: 'same-origin' });
    if (!res.ok) return null;
    return await res.json();
  } catch { return null; }
}

/** Спросить сервер про лимит записи (не чаще раза в минуту). */
function ensureSettings(force = false) {
  const fresh = settingsPromise && !force
    && (Date.now() - Number(sttSettings.at || 0)) < SETTINGS_TTL_MS;
  if (fresh) return settingsPromise;
  settingsPromise = (async () => {
    const stt = await readStatus();
    if (stt) sttSettings = settingsFromStatus(stt, sttSettings);
    return sttSettings;
  })();
  return settingsPromise;
}

let cssPromise = null;

/** Подключить dictation.css один раз; ошибка загрузки не ломает диктовку. */
function useStyles() {
  if (cssPromise) return cssPromise;
  cssPromise = new Promise((resolve) => {
    const d = globalThis.document;
    if (!d || !d.head || typeof d.createElement !== 'function') { resolve(); return; }
    try {
      const links = d.querySelectorAll ? d.querySelectorAll('link[rel="stylesheet"]') : [];
      for (const link of Array.from(links)) {
        if (link.getAttribute('href') === CSS_HREF) { resolve(); return; }
      }
    } catch { /* нет доступа — просто добавим */ }
    const link = d.createElement('link');
    link.setAttribute('rel', 'stylesheet');
    link.setAttribute('href', CSS_HREF);
    if (typeof link.addEventListener === 'function') {
      link.addEventListener('load', () => resolve());
      link.addEventListener('error', () => resolve());
    }
    d.head.appendChild(link);
    if (typeof link.addEventListener !== 'function') resolve();
  });
  return cssPromise;
}

/** Иконка codicon: mic = EC12, mic-filled = EC1C (есть в codicon.ttf). */
function glyph(name, size = 15) {
  const i = globalThis.document.createElement('i');
  i.className = 'codicon codicon-' + name;
  i.setAttribute('aria-hidden', 'true');
  i.style.fontSize = size + 'px';
  return i;
}

/** Тост в общий контейнер приложения (своих стилей не заводим). */
function notify(text, kind = 'info') {
  const d = globalThis.document;
  const host = d && d.getElementById ? d.getElementById('toasts') : null;
  if (!host) return;
  const iconName = kind === 'ok' ? 'pass' : kind === 'warn' ? 'warning'
    : kind === 'danger' ? 'error' : 'info';
  const node = h('div', { class: 'toast toast--' + kind, role: 'status' },
    glyph(iconName, 14), h('div', { class: 'toast__text' }, String(text)));
  host.appendChild(node);
  const close = () => {
    if (!node.parentNode) return;
    node.classList.add('is-leaving');
    setTimeout(() => { if (node.parentNode) node.parentNode.removeChild(node); }, 160);
  };
  node.addEventListener('click', close);
  setTimeout(close, 6000);
}

/* ──────────────────────────────────────── вставка текста в поле */

/** Событие input — чтобы счётчики, черновики и автосохранение сработали. */
function fireInput(el) {
  if (typeof el.dispatchEvent !== 'function') return;
  let ev = null;
  try { ev = new Event('input', { bubbles: true }); } catch { ev = null; }
  if (!ev) return;
  try { el.dispatchEvent(ev); } catch (err) { warn('input-событие', err); }
}

/**
 * Вставить текст в позицию курсора (выделение заменяется). Пробелы по краям —
 * только там, где иначе слова слиплись бы. Курсор встаёт после вставленного.
 * @param {HTMLTextAreaElement|HTMLInputElement} el
 * @param {string} text
 * @returns {{inserted: boolean, start: number, end: number, raw: string}}
 */
export function insertAtCursor(el, text) {
  const value = String(el.value === null || el.value === undefined ? '' : el.value);
  let start = typeof el.selectionStart === 'number' && el.selectionStart >= 0
    ? el.selectionStart : value.length;
  let end = typeof el.selectionEnd === 'number' && el.selectionEnd >= start
    ? el.selectionEnd : start;
  if (start > value.length) start = value.length;
  if (end > value.length) end = value.length;

  const ins = String(text === null || text === undefined ? '' : text).trim();
  if (!ins) return { inserted: false, start, end, raw: '' };

  const before = value.slice(0, start);
  const after = value.slice(end);
  const glueL = before && !/\s$/.test(before) ? ' ' : '';
  const glueR = after && !/^\s/.test(after) ? ' ' : '';
  const next = before + glueL + ins + glueR + after;

  el.value = next;
  const caret = (before + glueL + ins).length;
  try {
    if (typeof el.setSelectionRange === 'function') el.setSelectionRange(caret, caret);
    else { el.selectionStart = caret; el.selectionEnd = caret; }
  } catch { /* поле могло исчезнуть */ }
  fireInput(el);
  return { inserted: true, start: caret, end: caret, raw: ins };
}

/* ──────────────────────────────────────── одна запись на всё приложение */

let activeSession = null; // { flash(): void } — запись, которая сейчас идёт

/* ─────────────────────────────────────────── общие слушатели документа */

const instances = new Set();
let boundDoc = null;
let boundWindow = false;

function onDocKeydown(ev) {
  if (!ev || ev.key !== 'Escape') return;
  for (const mic of instances) {
    if (mic.isRecording()) { mic.cancelFromKey(ev); return; }
  }
}

function onDocSubmit(ev) {
  const t = ev && ev.target;
  for (const mic of instances) {
    if (mic.owns(t)) { mic.learnNow(); return; }
  }
}

/**
 * Страница уходит (закрыли окно, перезагрузили модуль, сменился адрес).
 *
 * Раньше здесь запись ВЫБРАСЫВАЛАСЬ молча — так и пропала шестиминутная
 * диктовка. Теперь наговоренное сохраняется на сервер маячком (без ожидания
 * ответа) и в список незаконченных записей: даже если окно исчезнет мгновенно,
 * аудио уже на диске, и его можно распознать.
 */
function onPageHide() {
  for (const mic of instances) {
    try { mic.flushOnHide(); } catch (err) { warn('pagehide', err); }
  }
}

function bindDoc() {
  const d = globalThis.document;
  if (!d || typeof d.addEventListener !== 'function') return;
  if (boundDoc !== d) {
    if (boundDoc && typeof boundDoc.removeEventListener === 'function') {
      boundDoc.removeEventListener('keydown', onDocKeydown, true);
      boundDoc.removeEventListener('submit', onDocSubmit, true);
    }
    d.addEventListener('keydown', onDocKeydown, true);
    d.addEventListener('submit', onDocSubmit, true);
    boundDoc = d;
  }
  if (!boundWindow && globalThis.addEventListener) {
    globalThis.addEventListener('pagehide', onPageHide);
    boundWindow = true;
  }
}

function unbindDoc() {
  if (instances.size) return;
  if (boundDoc && typeof boundDoc.removeEventListener === 'function') {
    boundDoc.removeEventListener('keydown', onDocKeydown, true);
    boundDoc.removeEventListener('submit', onDocSubmit, true);
  }
  boundDoc = null;
  if (boundWindow && globalThis.removeEventListener) {
    globalThis.removeEventListener('pagehide', onPageHide);
    boundWindow = false;
  }
}

/* ─────────────────────────────────────────── ошибки: человеческий язык */

function mediaErrorText(err) {
  const name = String((err && (err.name || err.code)) || '');
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError' || name === 'SecurityError') return ERR_PERMISSION;
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError' || name === 'OverconstrainedError') return ERR_NO_MIC;
  if (name === 'NotReadableError' || name === 'TrackStartError' || name === 'AbortError') return ERR_BUSY_MIC;
  return ERR_PERMISSION;
}

function sttErrorText(err) {
  const code = String((err && err.code) || '');
  if (code === 'network') return ERR_NET;
  if (code === 'timeout') return (err && err.message) || ERR_KEPT;
  if (code === 'stt_unavailable') return ERR_SERVER;
  if (code === 'audio_empty') return ERR_EMPTY;
  if (code === 'audio_too_big') return 'Запись слишком большая. Нажмите «Распознать ещё раз» — или разбейте её на части.';
  if (code === 'stt_failed') return 'Распознавание не получилось. ' + ERR_KEPT;
  const msg = err && err.message ? String(err.message) : '';
  return msg && msg !== 'Failed to fetch' ? msg : ERR_NET;
}

function sttError(status, data) {
  const e = data && typeof data === 'object' ? (data.error || data) : null;
  const err = new Error((e && (e.message || e.detail)) || ('Сервер ответил ' + status));
  err.code = String((e && e.code) || ('http_' + status));
  err.status = status;
  return err;
}

/* ────────────────────────────────────────────────── кнопка микрофона */

/** Номер записи для сервера: `r_` + время + случайный хвост (a-z0-9). */
function newRecordingId() {
  return 'r_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

/**
 * Кнопка микрофона для поля. Клик — начать/остановить; Ctrl+M в поле — то же;
 * Esc — отмена записи. Состояния: ожидание → доступ → запись → расшифровка.
 *
 * @param {HTMLTextAreaElement|HTMLInputElement} target
 * @param {{label?: string|false, variant?: 'secondary'|'primary', overlay?: boolean,
 *          className?: string, maxMs?: number, partMs?: number, onInsert?: Function,
 *          toast?: Function}} [opts]
 * @returns {HTMLButtonElement} у кнопки есть `.mic` с методами управления
 */
export function createMic(target, opts = {}) {
  if (!target || typeof target !== 'object') throw new TypeError('createMic: нужно поле ввода');
  const o = opts || {};
  const labeled = o.label !== false;
  const say = typeof o.toast === 'function' ? o.toast : notify;

  useStyles();
  if (!target.id) {
    try { target.id = uid('mic'); } catch { /* поле могло быть без id */ }
  }

  const st = {
    phase: 'idle', // idle | arming | recording | transcribing | error
    stream: null, rec: null, chunks: [], blob: null, error: '',
    startedAt: 0, maxMs: Number(o.maxMs) > 0 ? Number(o.maxMs) : sttSettings.maxMs,
    partMs: Number(o.partMs) > 0 ? Number(o.partMs) : sttSettings.partMs,
    rid: '', partNo: 0, parts: [], pumping: false, store: true, dropped: false,
    discard: false, slow: false, rotTimer: null, track: null, micLost: false,
    timer: null, meter: null, levelBuf: null, audioCtx: null, analyser: null,
    learn: null, destroyed: false,
  };

  // Один вид на всё приложение (docs/DESIGN-NOTES.md, «Единые компоненты»):
  // btn--secondary как у .cmp-mic; отличается только подпись — в узкой строке
  // она скрыта (mic-btn--action без mic-btn--wide) или её нет вовсе
  // (mic-btn--compact внутри .mic-field).
  const classes = ['mic-btn', 'btn', 'btn--secondary'];
  classes.push(labeled ? 'mic-btn--action' : 'mic-btn--compact');
  if (labeled) classes.push('mic-btn--wide');
  if (o.className) classes.push(String(o.className));

  const btn = h('button', {
    type: 'button', class: classes, 'data-mic-btn': 'true', 'data-state': 'idle',
    'aria-pressed': 'false',
  });
  if (target.id) btn.setAttribute('data-mic-for', String(target.id));

  /* ── панель (фиксированная, поверх диалога: не обрезается overflow-ом) ── */

  // Панель записи живёт в dictation-panel.js: вид и место — там.
  const panel = createPanel({
    onStop: () => stop(false),
    onCancel: () => cancel(),
    onRetry: () => retryParts(),
    onClose: () => { st.error = ''; st.phase = 'idle'; render(); panel.close(); },
  });

  /* ── отрисовка ── */

  function btnText() {
    if (st.phase === 'recording') return { short: 'Стоп', title: 'Остановить запись (Ctrl+M)' };
    if (st.phase === 'arming') return { short: 'Разрешите…', title: 'Ждём разрешение на микрофон' };
    if (st.phase === 'transcribing') return { short: 'Расшифровываю…', title: 'Распознаю запись' };
    if (st.phase === 'error') return { short: 'Повторить', title: 'Запись не распозналась — попробовать снова' };
    return { short: labeled ? 'Надиктовать' : 'Надиктовать голосом', title: labeled ? 'Надиктовать (Ctrl+M)' : 'Надиктовать голосом (Ctrl+M)' };
  }

  function render() {
    if (st.destroyed) return;                   // окно уже закрывается — рисовать некуда
    const info = btnText();
    btn.setAttribute('data-state', st.phase);
    btn.setAttribute('aria-pressed', st.phase === 'recording' ? 'true' : 'false');
    btn.setAttribute('title', info.title);
    btn.setAttribute('aria-label', info.title);
    btn.classList.toggle('mic-btn--rec', st.phase === 'recording');
    btn.classList.toggle('mic-btn--busy', st.phase === 'arming' || st.phase === 'transcribing');
    btn.classList.toggle('mic-btn--error', st.phase === 'error');
    btn.disabled = st.phase === 'arming';

    // Состояния одинаковы во всех местах: ожидание (микрофон), запись
    // (красная точка + таймер), распознаётся (крутилка), ошибка (значок).
    const kids = [];
    if (st.phase === 'arming' || st.phase === 'transcribing') {
      kids.push(glyph('loading', 14));
      kids[kids.length - 1].classList.add('codicon-modifier-spin');
    } else if (st.phase === 'recording') {
      kids.push(h('span', { class: 'mic-btn__dot', 'aria-hidden': 'true' }));
    } else if (st.phase === 'error') {
      kids.push(glyph('warning', 15));
    } else {
      kids.push(glyph('mic', 15));
    }
    if (labeled) kids.push(h('span', { class: 'mic-btn__text' }, info.short));
    if (st.phase === 'recording') {
      kids.push(h('span', { class: 'mic-btn__time' }, formatClock(Date.now() - st.startedAt)));
    }
    if (typeof btn.replaceChildren === 'function') btn.replaceChildren(...kids);
    else if (btn.childNodes) { btn.textContent = ''; for (const k of kids) btn.appendChild(k); }

    renderPanel();
  }

  /** «Распознано 2 из 7 минут» — прогресс по частям записи. */
  function progressText() {
    const parts = st.parts || [];
    if (!parts.length) return '';
    const done = parts.filter((job) => job.state === 'done');
    if (done.length >= parts.length) return '';
    const totalS = parts.reduce((sum, job) => sum + (Number(job.seconds) || st.partMs / 1000), 0);
    const doneS = done.reduce((sum, job) => sum + (Number(job.seconds) || st.partMs / 1000), 0);
    return 'Распознано ' + formatClock(doneS * 1000) + ' из ' + formatClock(totalS * 1000);
  }

  function partsHint() {
    const failed = st.parts.filter((job) => job.state === 'failed').length;
    if (!failed) return '';
    return failed === 1
      ? 'Одна часть не распозналась — нажмите «Распознать ещё раз».'
      : 'Не распознались части: ' + failed + ' — нажмите «Распознать ещё раз».';
  }

  /** Подсказка под заголовком панели: зависит от состояния. */
  function panelHint() {
    if (st.phase === 'recording') {
      const left = Number(st.maxMs) - (Date.now() - st.startedAt);
      if (left > 0 && left <= WARN_BEFORE_MS) {
        return 'Осталось меньше 30 секунд — запись сама уйдёт на распознавание.';
      }
      return st.slow ? MSG_LOADING : HINT_REC;
    }
    if (st.phase === 'transcribing') return st.slow ? MSG_LOADING : HINT_KEEP;
    if (st.phase === 'error') return partsHint() || 'Поле не заблокировано — можно печатать.';
    return undefined;
  }

  /** Панель: состояния и подсказки — в dictation-panel.js. */
  function renderPanel() {
    if (!panel.isOpen()) return;
    const failed = st.parts.some((job) => job.state === 'failed');
    panel.update({
      phase: st.phase,
      timeText: st.phase === 'recording'
        ? formatClock(Date.now() - st.startedAt) + ' / ' + formatClock(st.maxMs) : '',
      hint: panelHint(),
      progress: st.phase === 'transcribing' ? progressText() : '',
      error: st.error,
      canRetry: Boolean(st.blob) || failed || st.parts.some((job) => job.state === 'new'),
      retryLabel: 'Распознать ещё раз',
    });
  }

  /** Открыть панель и показать в ней незаконченные записи (если есть). */
  function openPanel() {
    panel.open(btn);
    refreshRecover();
  }

  /** Строка «есть нерасшифрованная запись» — из локального списка. */
  function refreshRecover() {
    if (!panel.isOpen()) return;
    // Пока где-то идёт запись, показывать «распознать» нечего: её части ещё
    // пишутся, и вторая панель не должна предлагать их расшифровать.
    if (activeSession && activeSession !== api && activeSession.isRecording()) {
      panel.setRecover(null);
      return;
    }
    const items = store.listPending().filter((item) => item.rid !== st.rid
      || (st.phase === 'idle' && !st.parts.length));
    if (!items.length) { panel.setRecover(null); return; }
    // Панель открыта только ради нерасшифрованной записи — нужен заголовок.
    if (st.phase === 'idle') panel.setTitle('Диктовка');
    const totalS = items.reduce((sum, item) => sum + (Number(item.seconds) || 0), 0);
    const one = items.length === 1 ? items[0] : null;
    const clock = totalS > 0 ? ' · ' + formatClock(totalS * 1000) : '';
    panel.setRecover({
      text: one
        ? 'Есть нерасшифрованная запись' + clock
        : 'Есть нерасшифрованные записи: ' + items.length + clock,
      hint: one
        ? 'Наговоренное сохранено — можно распознать и вставить в это поле.'
        : 'Наговоренное сохранено. Распознаём по одной — начнём с последней.',
      onRecover: () => { void recoverItem(items[0]); },
      // «Убрать» = убрать ВСЁ, что сейчас показано: иначе при следующем
      // открытии панели вылезала бы следующая запись — «убрал, а оно снова висит».
      onDismiss: () => { void dismissPending(items); },
      dismissLabel: one ? 'Убрать' : 'Убрать все',
    });
  }

  /** Серверная копия записи (она живёт трое суток — зачем держать зря). */
  function deleteOnServer(rid) {
    if (!rid) return;
    try {
      void fetch(REC_URL + '/' + rid, { method: 'DELETE', credentials: 'same-origin' })
        .catch(() => null);
    } catch (err) { warn('delete', err); }
  }

  /**
   * Человек нажал «Убрать»: следа, локальных частей и серверной копии больше
   * нет. Строка уходит и НЕ возвращается при следующем открытии панели.
   */
  async function dismissPending(items) {
    const rids = (Array.isArray(items) ? items : [items])
      .map((item) => String((item && item.rid) || '')).filter(Boolean);
    if (!rids.length) return;
    try { store.forgetPending(rids); } catch (err) { warn('pending', err); }
    panel.setRecover(null);
    for (const rid of rids) {
      try { void store.dropRecording(rid); } catch (err) { warn('indexeddb', err); }
      deleteOnServer(rid);
    }
    // Пока строка висела, следов могло стать больше (новая сорванная попытка) —
    // читаем список заново, а не показываем то, что уже убрали.
    refreshRecover();
  }

  /* ── запись ── */

  function streamOf(stream) {
    if (!stream || typeof stream.getTracks !== 'function') return;
    for (const track of stream.getTracks()) {
      try { if (track && typeof track.stop === 'function') track.stop(); } catch { /* уже остановлен */ }
    }
  }

  function releaseMedia() {
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
    if (st.rotTimer) { clearTimeout(st.rotTimer); st.rotTimer = null; }
    if (st.meter) { clearInterval(st.meter); st.meter = null; }
    st.levelBuf = null;
    const rec = st.rec;
    st.rec = null;
    if (rec) {
      rec.ondataavailable = null;
      rec.onstop = null;
      rec.onerror = null;
    }
    streamOf(st.stream);
    st.stream = null;
    const ac = st.audioCtx;
    st.audioCtx = null;
    st.analyser = null;
    if (ac && typeof ac.close === 'function') { try { ac.close(); } catch { /* закрыт */ } }
  }

  function isRecording() { return st.phase === 'recording'; }

  /** Живое поле: страница могла перерисоваться, пока шла запись. */
  function liveField() {
    const d = globalThis.document;
    const alive = (el) => Boolean(el) && (el.isConnected === true
      || (el.isConnected === undefined && Boolean(el.parentNode)));
    if (alive(target)) return target;
    if (!d) return target;
    const keep = target.getAttribute ? target.getAttribute('data-keep') : null;
    if (keep && d.querySelectorAll) {
      try {
        for (const el of Array.from(d.querySelectorAll('[data-keep]'))) {
          if (el.getAttribute('data-keep') === keep) return el;
        }
      } catch { /* нет доступа к DOM */ }
    }
    if (target.id && typeof d.getElementById === 'function') {
      const byId = d.getElementById(target.id);
      if (byId) return byId;
    }
    return target;
  }

  function owns(node) {
    if (!node) return false;
    return node === target || (target.contains && target.contains(node));
  }

  function recorderOpts() {
    const Rec = globalThis.MediaRecorder;
    const want = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
    if (Rec && typeof Rec.isTypeSupported === 'function') {
      for (const type of want) {
        try { if (Rec.isTypeSupported(type)) return { mimeType: type }; } catch { /* не поддерживается */ }
      }
    }
    return {};
  }

  function startMeter(stream) {
    const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!AC || !stream) return;
    try {
      const ctx = new AC();
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.7;
      source.connect(analyser);
      st.audioCtx = ctx;
      st.analyser = analyser;
      st.levelBuf = new Float32Array(analyser.fftSize);
      const read = () => {
        const an = st.analyser;
        const buf = st.levelBuf;
        if (!an || !buf || !panel.isOpen()) return;
        try {
          an.getFloatTimeDomainData(buf);
          let sum = 0;
          for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
          const rms = Math.sqrt(sum / buf.length);
          panel.setLevel(Math.sqrt(rms) * 240);
        } catch { /* уровень не критичен */ }
      };
      st.meter = setInterval(read, 80);
    } catch (err) {
      warn('уровень звука недоступен', err);
      st.audioCtx = null;
    }
  }

  function tick() {
    const elapsed = Date.now() - st.startedAt;
    if (panel.isOpen()) renderPanel();
    if (labeled) {
      const text = btnText().short;
      const span = btn.querySelector ? btn.querySelector('.mic-btn__text') : null;
      if (span) span.textContent = text;
      else btn.setAttribute('title', btnText().title);
    }
    // Таймер записи виден и в широкой кнопке, и в компактной — одинаково.
    const clock = btn.querySelector ? btn.querySelector('.mic-btn__time') : null;
    if (clock) clock.textContent = formatClock(elapsed);
    // Достигли лимита — не выбрасываем запись, а отправляем на распознавание.
    if (shouldAutoStop(elapsed, st.maxMs)) { stop(false); return; }
    // Сторож микрофона: бывает, что событие «дорожка закончилась» не приходит
    // (webview, смена устройства) — тогда рекордер просто стоит. Панель не должна
    // показывать «Запись» с бегущим таймером, когда писать уже некуда.
    if (micDead()) micLostNow();
  }

  /** Микрофон ещё работает? `false` — устройство пропало во время записи. */
  function micDead() {
    if (st.phase !== 'recording') return false;
    const track = st.track;
    if (track && String(track.readyState) === 'ended') return true;
    const rec = st.rec;
    return Boolean(rec) && String(rec.state) === 'inactive';
  }

  /** Микрофон отвалился: останавливаемся сами, сказанное не теряем. */
  function micLostNow() {
    if (st.phase !== 'recording') return;
    st.micLost = true;
    st.error = '';
    stop(false);
  }

  /** Слушать дорожку: «закончилась» = устройство отключили или заняли. */
  function watchTrack(stream) {
    st.track = null;
    const tracks = stream && typeof stream.getAudioTracks === 'function'
      ? stream.getAudioTracks()
      : (stream && typeof stream.getTracks === 'function' ? stream.getTracks() : []);
    const track = (tracks || [])[0] || null;
    if (!track) return;
    st.track = track;
    const onEnd = () => micLostNow();
    try {
      if (typeof track.addEventListener === 'function') {
        track.addEventListener('ended', onEnd);
        // На Windows выдёргивание устройства сначала даёт «mute», а «ended»
        // может не прийти вовсе — проверяем фактическое состояние дорожки.
        track.addEventListener('mute', () => {
          setTimeout(() => { if (String(track.readyState) === 'ended') onEnd(); }, 1200);
        });
      } else {
        track.onended = onEnd;
      }
    } catch (err) { warn('дорожка', err); }
  }

  /** Наш собственный стоп рекордера (не путать с «браузер остановил сам»). */
  function shutRecorder(rec) {
    if (!rec) return;
    rec.__expectedStop = true;
    try { if (String(rec.state) !== 'inactive') rec.stop(); } catch { /* уже стоим */ }
  }

  /* ── части записи ── */

  /** Текущий рекордер: каждый — отдельный файл (часть записи). */
  function openRecorder() {
    const Rec = globalThis.MediaRecorder;
    let rec = null;
    try {
      rec = new Rec(st.stream, recorderOpts());
    } catch (err) {
      try { rec = new Rec(st.stream); } catch { streamOf(st.stream); return null; }
    }
    const chunks = [];
    let closed = false;
    const close = (fromError) => {
      if (closed) return;
      closed = true;
      // Наш это стоп (кнопка, лимит, поворот части) или запись оборвал браузер?
      // Второе = микрофон пропал: об этом надо сказать, а не молча ждать.
      const expected = Boolean(rec.__expectedStop);
      if (st.rec === rec) { st.rec = null; st.chunks = []; }
      if (!fromError && !expected && st.phase === 'recording') {
        st.micLost = true;
        st.phase = 'transcribing';
      }
      closePart(rec, chunks, fromError);
      // Микрофон больше не нужен: распознавание идёт по сохранённым частям.
      if (st.phase !== 'recording') {
        releaseMedia();
        if (activeSession === api) activeSession = null;
        render();
        // Часть могла оказаться пустой (стоп сразу после поворота) — очередь
        // всё равно должна тронуться, иначе панель навсегда останется «расшифровываю».
        maybePump();
      }
    };
    rec.ondataavailable = (ev) => {
      if (ev && ev.data && Number(ev.data.size) > 0) chunks.push(ev.data);
    };
    rec.onstop = () => close(false);
    rec.onerror = (ev) => {
      const name = ev && ev.error && ev.error.name ? String(ev.error.name) : '';
      rec.__expectedStop = true;
      close(true);
      fail('Запись прервалась' + (name ? ' (' + name + ')' : '') + '. ' + ERR_KEPT);
    };
    st.rec = rec;
    st.chunks = chunks;
    try {
      rec.start(1000); // timeslice: куски каждую секунду
    } catch (err) {
      st.rec = null;
      return null;
    }
    return rec;
  }

  /** Закрытая часть: сразу в локальное хранилище и в очередь на распознавание. */
  function closePart(rec, chunks, fromError) {
    const type = (rec && rec.mimeType) || 'audio/webm';
    let blob = null;
    try { blob = new Blob(chunks, { type }); } catch { blob = null; }
    // Куски забираем один раз: часть закрывается ровно один раз, и повторный
    // вызов с тем же списком кусков не отправит одно и то же аудио дважды.
    if (Array.isArray(chunks)) chunks.length = 0;
    if (st.discard) return null;
    if (!blob || !blob.size) return null;
    const job = queuePart(blob);
    // Разговор закончился (стоп, обрыв, ошибка) — начинаем распознавать.
    if (!fromError) maybePump();
    return job;
  }

  /** Поставить часть в очередь: сохранить локально, запомнить запись, отрисовать. */
  function queuePart(blob) {
    if (st.discard) return null;
    const n = st.partNo + 1;
    st.partNo = n;
    const job = { n, blob, state: 'new', error: '', text: '', seconds: st.partMs / 1000,
                  saved: false };
    st.parts.push(job);
    try { void store.putPart(st.rid, n, blob); } catch (err) { warn('indexeddb', err); }
    // На диск сервера — сразу, не дожидаясь стопа: обрыв окна не унесёт часть.
    void savePartOnServer(job);
    try {
      store.rememberPending(st.rid, {
        parts: n, seconds: Math.max(0, (Date.now() - (st.startedAt || Date.now())) / 1000),
      });
    } catch (err) { warn('pending', err); }
    render();
    return job;
  }

  /** Пора начинать новую часть: старый файл закрываем, новый открываем сразу. */
  function rotate() {
    const rec = st.rec;
    if (!rec) return;
    shutRecorder(rec);
    if (st.phase === 'recording' && st.stream) {
      if (!openRecorder()) {
        fail('Запись прервалась на новой части. ' + ERR_KEPT);
        return;
      }
    }
    scheduleRotate();
  }

  function scheduleRotate() {
    if (st.rotTimer) { clearTimeout(st.rotTimer); st.rotTimer = null; }
    const partMs = Math.max(50, Number(st.partMs) || DEFAULT_PART_MS);
    st.rotTimer = setTimeout(() => {
      st.rotTimer = null;
      if (st.phase !== 'recording') return;
      rotate();
    }, partMs);
  }

  /* ── распознавание частей ── */

  /** Сохранить часть на сервере (не критично: копия уже есть локально). */
  async function savePartOnServer(job) {
    if (job.saved) return true;
    if (!st.rid || st.store === false) return false;
    try {
      const res = await fetch(REC_URL + '/' + st.rid + '/parts/' + job.n, {
        method: 'PUT',
        headers: { 'Content-Type': job.blob.type || 'audio/webm' },
        body: job.blob,
        credentials: 'same-origin',
      });
      if (res.status === 404 || res.status === 405 || res.status === 501) {
        st.store = false;              // сервер без частей (старая версия) — не настаиваем
        return false;
      }
      job.saved = Boolean(res.ok);
      return job.saved;
    } catch (err) {
      warn('часть не сохранилась на сервере', err);
      return false;
    }
  }

  /** Распознать одну часть. Таймаут — по длине части, а не «две минуты на всё». */
  async function transcribePart(job) {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const seconds = Math.max(1, Number(job.seconds) || st.partMs / 1000);
    const guardMs = Math.max(90 * 1000, Math.round(seconds * 6 * 1000) + 60 * 1000);
    const guard = ctrl ? setTimeout(() => ctrl.abort(), guardMs) : null;
    try {
      const res = await fetch(TRANS_URL, {
        method: 'POST',
        headers: { 'Content-Type': job.blob.type || 'audio/webm' },
        body: job.blob,
        credentials: 'same-origin',
        signal: ctrl ? ctrl.signal : undefined,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw sttError(res.status, data);
      const text = String((data && (data.text || data.raw)) || '').trim();
      if (Number(data && data.duration_s) > 0) job.seconds = Number(data.duration_s);
      job.text = text;
      if (text) insertPartText(job);
      return text;
    } catch (err) {
      if (err && err.name === 'AbortError') {
        const timedOut = new Error('Распознавание не уложилось во время. ' + ERR_KEPT);
        timedOut.code = 'timeout';
        throw timedOut;
      }
      if (!err.code && err.name === 'TypeError') err.code = 'network';
      throw err;
    } finally {
      if (guard) clearTimeout(guard);
    }
  }

  /** Часть распозналась — текст сразу в поле (поле не заблокировано). */
  function insertPartText(job) {
    if (st.dropped) return;                     // запись отменили или страница уходит
    const el = liveField();
    const res = insertAtCursor(el, job.text);
    if (!res.inserted) return;
    st.learn = {
      raw: String(el.value === null || el.value === undefined ? '' : el.value),
      el,
    };
    if (typeof o.onInsert === 'function') {
      try { o.onInsert(res, job.text); } catch (err) { warn('onInsert', err); }
    }
  }

  /** Пора распознавать? Пока идёт запись — нет; очередь занята — попросим ещё круг. */
  function maybePump() {
    if (st.phase === 'recording') return;
    if (st.pumping) { st.repump = true; return; }
    if (!st.parts.some((job) => job.state === 'new')) {
      if (st.phase === 'transcribing') allPartsDone();   // распознавать нечего
      return;
    }
    void pump();
  }

  /** Очередь распознавания: по одной части, по порядку. */
  async function pump() {
    if (st.pumping) { st.repump = true; return; }
    st.pumping = true;
    const slow = setTimeout(() => { st.slow = true; renderPanel(); }, SLOW_MS);
    try {
      for (;;) {
        const job = st.parts.find((item) => item.state === 'new');
        if (!job) break;
        job.state = 'sending';
        st.error = '';
        render();
        try {
          await sendPart(job);
          job.state = 'done';
          if (st.dropped) return;
        } catch (err) {
          job.state = 'failed';
          job.error = sttErrorText(err);
          st.error = job.error;
        }
        render();
      }
    } finally {
      clearTimeout(slow);
      st.slow = false;
      st.pumping = false;
    }
    if (st.repump) {
      // Пока шла очередь, пришла ещё одна часть — обрабатываем и её.
      st.repump = false;
      if (st.parts.some((job) => job.state === 'new')) { void pump(); return; }
    }
    allPartsDone();
  }

  /** Часть: сохранить на сервере и распознать (одна повторная попытка на сети). */
  async function sendPart(job) {
    await savePartOnServer(job);
    let attempt = 0;
    for (;;) {
      try {
        return await transcribePart(job);
      } catch (err) {
        const code = String((err && err.code) || '');
        if (attempt === 0 && (code === 'network' || code === 'timeout')) {
          attempt += 1;
          await delay(900);
          continue;
        }
        throw err;
      }
    }
  }

  /** Все части обработаны: закрыть панель или честно сказать, что осталось. */
  function allPartsDone() {
    if (st.pumping || st.dropped) return;
    if (!st.parts.length) {
      if (st.phase === 'transcribing') {
        st.phase = 'error';
        st.error = st.micLost ? MSG_MIC_LOST_EMPTY : ERR_EMPTY;
        // Звука нет вовсе — восстанавливать нечего: следа быть не должно.
        try { store.forgetPending(st.rid); } catch (err) { warn('pending', err); }
        render();
        openPanel();
      }
      return;
    }
    const failed = st.parts.filter((job) => job.state === 'failed');
    if (failed.length) {
      const doneS = st.parts.filter((job) => job.state === 'done')
        .reduce((sum, job) => sum + (Number(job.seconds) || 0), 0);
      st.phase = 'error';
      st.error = (doneS > 0 ? 'Распознано ' + formatClock(doneS * 1000) + ', часть не получилась. ' : '')
        + ERR_KEPT;
      st.slow = false;
      render();
      openPanel();
      return;
    }
    const chars = st.parts.reduce((sum, job) => sum + String(job.text || '').trim().length, 0);
    if (!chars) {
      // Тишина или шум: распознавать нечего, но запись на месте — можно повторить.
      st.phase = 'error';
      st.error = ERR_EMPTY;
      st.slow = false;
      render();
      openPanel();
      return;
    }
    finishOk();
  }

  function finishOk() {
    const wasLost = st.micLost;
    st.phase = 'idle';
    st.error = '';
    st.slow = false;
    st.blob = null;
    st.micLost = false;
    render();
    panel.close();
    // Текст уже в поле — след, локальные части и серверная копия больше не нужны.
    try { store.forgetPending(st.rid); } catch (err) { warn('pending', err); }
    try { void store.dropRecording(st.rid); } catch (err) { warn('indexeddb', err); }
    deleteOnServer(st.rid);
    const el = liveField();
    if (typeof el.focus === 'function') { try { el.focus(); } catch { /* не критично */ } }
    if (wasLost) say(MSG_MIC_LOST_OK, 'warn');
  }

  /* ── «Распознать ещё раз» ── */

  function retryParts() {
    if (st.phase === 'recording' || st.phase === 'arming' || st.pumping) return;
    const failed = st.parts.filter((job) => job.state === 'failed');
    if (!failed.length) {
      if (st.blob) {
        queuePart(st.blob);
        st.blob = null;
        st.phase = 'transcribing';
        render();
        void pump();
      }
      return;
    }
    for (const job of failed) { job.state = 'new'; job.error = ''; }
    st.error = '';
    st.phase = 'transcribing';
    render();
    openPanel();
    void pump();
  }

  /* ── незаконченная запись: распознать позже ── */

  /** Взять часть с сервера (если локальной копии нет). */
  async function fetchPart(rid, n) {
    try {
      const res = await fetch(REC_URL + '/' + rid + '/parts/' + n,
                              { credentials: 'same-origin' });
      if (!res.ok) return null;
      return await res.blob();
    } catch { return null; }
  }

  /** Распознать сохранённую запись и вставить текст в это поле. */
  async function recoverItem(item) {
    if (!item || !item.rid) return;
    if (st.phase === 'recording' || st.phase === 'arming' || st.pumping) return;
    const rid = String(item.rid);
    st.parts = [];
    st.partNo = 0;
    st.error = '';
    st.rid = rid;
    st.store = true;
    st.micLost = false;
    st.phase = 'transcribing';
    render();
    openPanel();
    const total = Math.max(1, Number(item.parts) || 1);
    const jobs = [];
    for (let n = 1; n <= total; n += 1) {
      let blob = null;
      try { blob = await store.getPart(rid, n); } catch (err) { warn('indexeddb', err); }
      if (!blob) blob = await fetchPart(rid, n);
      if (!blob) continue;
      jobs.push({ n, blob, state: 'new', error: '', text: '', seconds: st.partMs / 1000,
                  saved: true });
    }
    st.parts = jobs;
    st.partNo = jobs.length;
    panel.setRecover(null);
    if (!jobs.length) {
      // Запись уже удалена (на диске их держат трое суток) — распознавать нечего.
      // Убираем след, чтобы строка не всплывала при каждом открытии панели.
      st.phase = 'error';
      st.error = MSG_RECORD_GONE;
      try { store.forgetPending(rid); } catch (err) { warn('pending', err); }
      try { void store.dropRecording(rid); } catch (err) { warn('indexeddb', err); }
      deleteOnServer(rid);
      render();
      return;
    }
    void pump();
  }

  /** Параллельная проверка сервера: доступен ли он, идёт ли загрузка модели. */
  async function readSettingsBeforeStart() {
    const info = await readStatus();
    if (!info) return;
    const next = settingsFromStatus(info, { ...sttSettings, maxMs: st.maxMs, partMs: st.partMs });
    if (!(Number(o.maxMs) > 0)) st.maxMs = next.maxMs;
    if (!(Number(o.partMs) > 0)) st.partMs = next.partMs;
    if (info.available === false && !info.loading) {
      fail(info.reason ? ERR_SERVER + ' Причина: ' + String(info.reason) : ERR_SERVER);
      return;
    }
    if (info.loading && st.phase === 'recording') { st.slow = true; renderPanel(); }
  }

  async function start() {
    if (st.destroyed) return;
    if (st.phase === 'recording') { stop(false); return; }
    if (st.phase === 'arming' || st.phase === 'transcribing') return;
    if (st.phase === 'error') { st.error = ''; st.phase = 'idle'; }

    if (activeSession && activeSession !== api) {
      activeSession.flash();
      say('Уже идёт запись — нажмите «Стоп» в том поле, где она началась.', 'warn');
      return;
    }

    const nav = globalThis.navigator;
    const Rec = globalThis.MediaRecorder;
    if (typeof Rec !== 'function' || !nav || !nav.mediaDevices || typeof nav.mediaDevices.getUserMedia !== 'function') {
      st.phase = 'error'; st.error = ERR_UNSUPPORTED; render(); openPanel(); return;
    }

    // Лимит записи — из настроек Hermes; спрашиваем заранее, но запись не ждём.
    ensureSettings().then((cfg) => {
      if (!(Number(o.maxMs) > 0)) st.maxMs = cfg.maxMs;
      if (!(Number(o.partMs) > 0)) {
        const changed = st.partMs !== cfg.partMs;
        st.partMs = cfg.partMs;
        // Длина части пришла с сервера уже во время записи — режим заново.
        if (changed && st.phase === 'recording') scheduleRotate();
      }
      renderPanel();
    }).catch((err) => warn('настройки', err));

    st.phase = 'arming';
    st.error = '';
    st.discard = false;
    st.dropped = false;
    st.micLost = false;
    st.chunks = [];
    st.blob = null;
    st.parts = [];
    st.partNo = 0;
    st.store = true;
    st.rid = newRecordingId();
    render();
    openPanel();

    let stream = null;
    try {
      stream = await nav.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      st.phase = 'error';
      st.error = mediaErrorText(err);
      // Следа нет и не будет: писать некуда — незачем потом предлагать «Распознать».
      try { store.forgetPending(st.rid); } catch (e) { warn('pending', e); }
      render();
      return;
    }
    if (st.destroyed) { streamOf(stream); return; }

    // Сервер распознавания проверяем параллельно: запись не задерживаем.
    readSettingsBeforeStart().catch((err) => warn('статус', err));

    st.stream = stream;
    if (!openRecorder()) { releaseMedia(); fail(ERR_UNSUPPORTED); return; }
    watchTrack(stream);

    st.startedAt = Date.now();
    st.phase = 'recording';
    // След «незаконченной записи» пишется не здесь, а с первой реальной частью
    // звука (`queuePart`). Раньше он появлялся в момент старта с `parts: 0` —
    // и каждая сорванная попытка оставляла «Есть нерасшифрованная запись»,
    // по которой распознавать было нечего.
    activeSession = api;
    st.timer = setInterval(tick, TICK_MS);
    scheduleRotate();
    startMeter(stream);
    render();
    openPanel();
    if (typeof target.focus === 'function') { try { target.focus(); } catch { /* фокус не критичен */ } }
  }

  function stop(discard) {
    if (discard) st.discard = true;
    if (st.phase === 'arming') { cancel(); return; }
    if (st.phase !== 'recording') return;
    if (st.rotTimer) { clearTimeout(st.rotTimer); st.rotTimer = null; }
    const rec = st.rec;
    const chunks = st.chunks || [];
    st.slow = false;
    st.phase = discard ? 'idle' : 'transcribing';
    render();
    if (!rec || String(rec.state) === 'inactive') { closePart(rec, chunks, false); return; }
    shutRecorder(rec);
  }

  /** Ошибка: запись не выбрасываем — что успели, уходит на распознавание. */
  function fail(message) {
    if (st.rotTimer) { clearTimeout(st.rotTimer); st.rotTimer = null; }
    const rec = st.rec;
    const chunks = st.chunks || [];
    st.rec = null;
    st.chunks = [];
    if (rec) {
      shutRecorder(rec);
      if (chunks.length) closePart(rec, chunks, true);
    }
    releaseMedia();
    if (activeSession === api) activeSession = null;
    st.phase = 'error';
    st.error = String(message || ERR_SERVER);
    st.slow = false;
    render();
    openPanel();
    if (st.parts.length) void pump();
    else {
      // Сохранять нечего — след «незаконченной записи» тоже не нужен.
      try { store.forgetPending(st.rid); } catch (err) { warn('pending', err); }
    }
  }

  function cancel() {
    if (st.phase === 'recording') { stop(true); cancelCleanup(); return; }
    if (st.phase === 'arming') {
      st.phase = 'idle';
      st.error = '';
      releaseMedia();
      render();
      panel.close();
      cancelCleanup();
      return;
    }
    st.error = '';
    st.phase = 'idle';
    render();
    panel.close();
  }

  /** Отмена — единственный случай, когда наговоренное убирается (по воле человека). */
  function cancelCleanup() {
    const rid = st.rid;
    st.dropped = true;
    st.parts = [];
    st.partNo = 0;
    st.blob = null;
    if (!rid) return;
    try { void store.dropRecording(rid); } catch (err) { warn('indexeddb', err); }
    try { store.forgetPending(rid); } catch (err) { warn('pending', err); }
    deleteOnServer(rid);
  }

  /**
   * Страница закрывается: сохранить текущую часть на сервер маячком и в список.
   * Ничего не выбрасываем и не ждём ответа — окно может исчезнуть мгновенно.
   */
  function flushOnHide() {
    if (st.phase !== 'recording' && st.phase !== 'arming') return;
    const chunks = st.chunks || [];
    let blob = null;
    try { blob = new Blob(chunks, { type: (st.rec && st.rec.mimeType) || 'audio/webm' }); }
    catch { blob = null; }
    const rid = st.rid;
    const n = st.partNo + 1;
    if (rid) {
      try {
        store.rememberPending(rid, {
          parts: blob && blob.size ? n : st.partNo,
          seconds: Math.max(0, (Date.now() - (st.startedAt || Date.now())) / 1000),
        });
      } catch (err) { warn('pending', err); }
      if (blob && blob.size) {
        const nav = globalThis.navigator;
        const beacon = nav && nav.sendBeacon;
        try {
          if (typeof beacon === 'function') {
            beacon.call(nav, REC_URL + '/' + rid + '/parts/' + n, blob);
          } else {
            void fetch(REC_URL + '/' + rid + '/parts/' + n, {
              method: 'PUT', headers: { 'Content-Type': blob.type || 'audio/webm' },
              body: blob, credentials: 'same-origin', keepalive: true,
            }).catch(() => null);
          }
        } catch (err) { warn('beacon', err); }
        try { void store.putPart(rid, n, blob); } catch (err) { warn('indexeddb', err); }
      }
    }
    st.discard = false;
    st.dropped = true;
    releaseMedia();
    st.phase = 'idle';
    if (activeSession === api) activeSession = null;
    panel.close();
    render();
  }

  /* ── «запомнить исправление» ── */

  async function offerLearn(raw, final, el) {
    try {
      const mod = await import('./dictation-learn.js');
      if (!mod || typeof mod.offerLearning !== 'function') return;
      // anchor — форма поля (или само поле): плашка «Запомнить исправление?»
      // встаёт под формой, а не поверх кнопок «Надиктовать»/«Отправить».
      let anchor = el;
      if (el && typeof el.closest === 'function') anchor = el.closest('form') || el;
      mod.offerLearning({ api: null, toast: say }, { raw, final, anchor });
    } catch {
      /* модуля ещё нет или он не готов — молча пропускаем */
    }
  }

  function learnNow() {
    const rec = st.learn;
    if (!rec) return;
    if (st.phase !== 'idle' && st.phase !== 'error') return;
    const el = liveField() || rec.el || target;
    const final = String(el.value === null || el.value === undefined ? '' : el.value);
    st.learn = null;
    if (!final.trim() || final === rec.raw) return;
    offerLearn(rec.raw, final, rec.el || el);
  }

  /* ── управление ── */

  const api = {
    el: btn,
    target,
    start,
    stop: (d) => stop(Boolean(d)),
    cancel,
    learnNow,
    phase: () => st.phase,
    isRecording,
    owns,
    /** Части записи: [{n, state, seconds, chars}] — для проверок и подсказок. */
    parts: () => st.parts.map((job) => ({ n: job.n, state: job.state,
                                          seconds: Number(job.seconds) || 0,
                                          chars: String(job.text || '').length })),
    progress: () => progressText(),
    settings: () => ({ maxMs: st.maxMs, partMs: st.partMs }),
    retry: () => retryParts(),
    recover: (item) => recoverItem(item),
    pending: () => store.listPending(),
    flushOnHide,
    openPanel,
    flash() { btn.classList.add('mic-btn--flash'); setTimeout(() => btn.classList.remove('mic-btn--flash'), 900); },
    cancelFromKey(ev) {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
      cancel();
    },
    stopNow() { if (st.phase === 'recording' || st.phase === 'arming') cancel(); },
    destroy() {
      st.destroyed = true;
      cancel();
      panel.close();
      if (typeof btn.remove === 'function') btn.remove();
      else if (btn.parentNode) btn.parentNode.removeChild(btn);
      if (target.removeEventListener) {
        target.removeEventListener('keydown', onKey);
        target.removeEventListener('focusout', onBlur);
      }
      instances.delete(api);
      unbindDoc();
    },
  };
  btn.mic = api;

  /* ── события ── */

  function toggle() {
    if (st.phase === 'recording') stop(false);
    else if (st.phase === 'error' && (st.blob || st.parts.length)) retryParts();
    else start();
  }

  const onKey = (ev) => {
    const key = String(ev && ev.key ? ev.key : '').toLowerCase();
    if (!ev || !(ev.ctrlKey || ev.metaKey) || (key !== 'm' && key !== 'ь')) return;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    toggle();
  };
  const onBlur = () => { setTimeout(() => { if (!panel.isOpen()) learnNow(); }, 300); };

  btn.addEventListener('click', (ev) => {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
    toggle();
  });
  target.addEventListener('keydown', onKey);
  target.addEventListener('focusout', onBlur);

  instances.add(api);
  bindDoc();
  render();
  return btn;
}

/* ─────────────────────────────────────────── автоподключение к полям */

// Поиск полей и расстановка кнопок — в dictation-auto.js; здесь реэкспорт:
// контракт §3 требует эти имена именно из dictation.js.
export { enhanceAll, attachMic, startAutoEnhance, stopAutoEnhance };

export default { createMic, enhanceAll, startAutoEnhance, stopAutoEnhance, attachMic,
                 insertAtCursor, settingsFromStatus, recordSettings, resetSettings };
