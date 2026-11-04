/**
 * dictation.js — микрофон в любом поле ввода: запись → распознавание → вставка
 * текста В ПОЗИЦИЮ КУРСОРА (или вместо выделенного).
 *
 * Контракт сервера — §3 WAVE7-RULES (`/api/stt/status`, `/api/stt/transcribe`).
 * Аудио не покидает компьютер: браузер шлёт его на свой же 127.0.0.1.
 *
 * Публично:
 *   createMic(target, opts) -> HTMLButtonElement   кнопка микрофона для поля
 *   enhanceAll(root) -> number                     добавить кнопки во все поля
 *   startAutoEnhance() -> MutationObserver|null    следить за новыми полями
 *   insertAtCursor(el, text) -> {inserted,start,end,raw}   (для тестов)
 *   formatClock(ms), nearLimit(ms,max), shouldAutoStop(ms,max)  (чистые)
 *
 * Правила: только токены/классы дизайн-системы, тексты — короткие по-русски,
 * одна запись на всё приложение, поле во время расшифровки НЕ блокируется.
 */

import { h, uid } from './util.js';
import { createPanel } from './dictation-panel.js';
import { enhanceAll, attachMic, startAutoEnhance, stopAutoEnhance } from './dictation-auto.js';

/* ─────────────────────────────────────────────────────────── настройки */

export const MAX_RECORD_MS = 10 * 60 * 1000; // 10 минут
const WARN_BEFORE_MS = 30 * 1000;          // за 30 с — подсказка
const TICK_MS = 250;
const PANEL_W = 300;
const SLOW_MS = 4000;                      // «первый раз — до минуты»
const CSS_HREF = 'css/dictation.css';
const TRANS_URL = '/api/stt/transcribe';
const STATUS_URL = '/api/stt/status';

const ERR_PERMISSION = 'Разрешите микрофон: значок замка рядом с адресом → Микрофон → Разрешить. Пока можно печатать вручную.';
const ERR_NO_MIC = 'Микрофон не найден. Подключите его и попробуйте снова — пока можно печатать вручную.';
const ERR_BUSY_MIC = 'Микрофон занят другой программой. Закройте её и попробуйте снова.';
const ERR_UNSUPPORTED = 'Браузер не умеет записывать с микрофона. Откройте приложение в Chrome — или печатайте вручную.';
const ERR_SERVER = 'Распознавание речи не готово — сервер ещё запускается. Попробуйте через минуту, пока можно печатать вручную.';
const ERR_EMPTY = 'Речь не обнаружена. Попробуйте ещё раз — или напечатайте вручную.';
const ERR_NET = 'Не удалось отправить запись. Текст не пропал — нажмите «Повторить».';
const MSG_LOADING = 'Загружаю распознавание речи… первый раз — до минуты';
const HINT_REC = 'Говорите — текст встанет туда, где курсор.';
const HINT_KEEP = 'Поле не заблокировано — можно печатать.';

/* ─────────────────────────────────────────────────────────── утилиты */

function warn(...args) {
  if (typeof console !== 'undefined' && console.warn) console.warn('[диктовка]', ...args);
}

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

/** Пора остановиться сами: 10 минут — предел. */
export function shouldAutoStop(elapsedMs, maxMs = MAX_RECORD_MS) {
  return Number(elapsedMs) >= Number(maxMs);
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

function onPageHide() {
  for (const mic of instances) mic.stopNow();
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
  if (code === 'stt_unavailable') return ERR_SERVER;
  if (code === 'audio_empty') return ERR_EMPTY;
  if (code === 'audio_too_big') return 'Запись слишком большая. Разбейте её на части покороче.';
  if (code === 'stt_failed') return 'Распознавание не получилось. Нажмите «Повторить» — запись сохранена.';
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

/**
 * Кнопка микрофона для поля. Клик — начать/остановить; Ctrl+M в поле — то же;
 * Esc — отмена записи. Состояния: ожидание → доступ → запись → расшифровка.
 *
 * @param {HTMLTextAreaElement|HTMLInputElement} target
 * @param {{label?: string|false, variant?: 'secondary'|'primary', overlay?: boolean,
 *          className?: string, maxMs?: number, onInsert?: Function, toast?: Function}} [opts]
 * @returns {HTMLButtonElement} у кнопки есть `.mic` с методами управления
 */
export function createMic(target, opts = {}) {
  if (!target || typeof target !== 'object') throw new TypeError('createMic: нужно поле ввода');
  const o = opts || {};
  const labeled = o.label !== false;
  const maxMs = Number(o.maxMs) > 0 ? Number(o.maxMs) : MAX_RECORD_MS;
  const say = typeof o.toast === 'function' ? o.toast : notify;

  useStyles();
  if (!target.id) {
    try { target.id = uid('mic'); } catch { /* поле могло быть без id */ }
  }

  const st = {
    phase: 'idle', // idle | arming | recording | transcribing | error
    stream: null, rec: null, chunks: [], blob: null, error: '',
    startedAt: 0, maxMs, discard: false, slow: false,
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
    onRetry: () => send(st.blob),
    onClose: () => { st.error = ''; st.phase = 'idle'; render(); panel.close(); },
  });

  /* ── отрисовка ── */

  function btnText() {
    if (st.phase === 'recording') return { short: 'Стоп', title: 'Остановить запись (Ctrl+M)' };
    if (st.phase === 'arming') return { short: 'Разрешите…', title: 'Ждём разрешение на микрофон' };
    if (st.phase === 'transcribing') return { short: 'Расшифровываю…', title: 'Распознаю запись' };
    if (st.phase === 'error') return { short: 'Повторить', title: 'Запись не отправилась — попробовать снова' };
    return { short: labeled ? 'Надиктовать' : 'Надиктовать голосом', title: labeled ? 'Надиктовать (Ctrl+M)' : 'Надиктовать голосом (Ctrl+M)' };
  }

  function render() {
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

  /** Панель: состояния и подсказки — в dictation-panel.js. */
  function renderPanel() {
    if (!panel.isOpen()) return;
    const elapsed = Date.now() - st.startedAt;
    panel.update({
      phase: st.phase,
      timeText: st.phase === 'recording'
        ? formatClock(elapsed) + ' / ' + formatClock(st.maxMs) : '',
      hint: st.phase === 'recording'
        ? (nearLimit(elapsed, st.maxMs)
          ? 'Осталось меньше 30 секунд — запись скоро закончится.'
          : (st.slow ? MSG_LOADING : HINT_REC))
        : (st.phase === 'transcribing' ? (st.slow ? MSG_LOADING : HINT_KEEP) : undefined),
      error: st.error,
      canRetry: Boolean(st.blob),
    });
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
    if (shouldAutoStop(elapsed, st.maxMs)) stop(false);
  }

  async function readStatus() {
    try {
      const res = await fetch(STATUS_URL, { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' });
      if (!res.ok) return null;
      return await res.json();
    } catch { return null; }
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
      st.phase = 'error'; st.error = ERR_UNSUPPORTED; render(); panel.open(btn); return;
    }

    st.phase = 'arming';
    st.error = '';
    st.discard = false;
    st.chunks = [];
    st.blob = null;
    render();
    panel.open(btn);

    let stream = null;
    try {
      stream = await nav.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      st.phase = 'error';
      st.error = mediaErrorText(err);
      render();
      return;
    }
    if (st.destroyed) { streamOf(stream); return; }

    // Сервер распознавания проверяем параллельно: запись не задерживаем.
    readStatus().then((stt) => {
      if (!stt) return;
      if (stt.available === false && !stt.loading) {
        fail(stt.reason ? ERR_SERVER + ' Причина: ' + String(stt.reason) : ERR_SERVER);
        return;
      }
      if (stt.loading && st.phase === 'recording') { st.slow = true; renderPanel(); }
    });

    let rec = null;
    try {
      rec = new Rec(stream, recorderOpts());
    } catch (err) {
      try { rec = new Rec(stream); } catch { streamOf(stream); fail(ERR_UNSUPPORTED); return; }
    }

    st.stream = stream;
    st.rec = rec;
    rec.ondataavailable = (ev) => {
      if (ev && ev.data && Number(ev.data.size) > 0) st.chunks.push(ev.data);
    };
    rec.onstop = () => finish();
    rec.onerror = (ev) => {
      const name = ev && ev.error && ev.error.name ? String(ev.error.name) : '';
      fail('Запись прервалась' + (name ? ' (' + name + ')' : '') + '. Попробуйте ещё раз.');
    };

    try {
      rec.start(1000); // timeslice: куски каждую секунду
    } catch (err) {
      releaseMedia();
      fail(ERR_UNSUPPORTED);
      return;
    }

    st.startedAt = Date.now();
    st.phase = 'recording';
    activeSession = api;
    st.timer = setInterval(tick, TICK_MS);
    startMeter(stream);
    render();
    panel.open(btn);
    if (typeof target.focus === 'function') { try { target.focus(); } catch { /* фокус не критичен */ } }
  }

  function stop(discard) {
    if (discard) st.discard = true;
    if (st.phase === 'arming') { cancel(); return; }
    if (st.phase !== 'recording') return;
    const rec = st.rec;
    if (!st.discard) {
      st.slow = false;
      st.phase = 'transcribing';
      render();
    }
    if (!rec || String(rec.state) === 'inactive') { finish(); return; }
    try { rec.stop(); } catch { finish(); }
  }

  function finish() {
    const rec = st.rec;
    const chunks = st.chunks;
    const type = (rec && rec.mimeType) || 'audio/webm';
    const discard = st.discard;
    st.chunks = [];
    st.discard = false;
    releaseMedia();
    if (activeSession === api) activeSession = null;

    if (discard) {
      st.blob = null;
      st.phase = 'idle';
      st.error = '';
      render();
      panel.close();
      return;
    }

    let blob = null;
    try { blob = new Blob(chunks, { type }); } catch { blob = null; }
    if (!blob || !blob.size) {
      st.blob = null;
      st.error = ERR_EMPTY;
      st.phase = 'error';
      render();
      return;
    }
    st.blob = blob;
    send(blob);
  }

  function fail(message) {
    const rec = st.rec;
    if (rec) {
      rec.ondataavailable = null;
      rec.onstop = null;
      rec.onerror = null;
      try { rec.stop(); } catch { /* уже стоим */ }
    }
    releaseMedia();
    if (activeSession === api) activeSession = null;
    st.phase = 'error';
    st.error = String(message || ERR_SERVER);
    st.slow = false;
    render();
    panel.open(btn);
  }

  function cancel() {
    if (st.phase === 'recording') { stop(true); return; }
    if (st.phase === 'arming') {
      st.phase = 'idle';
      st.error = '';
      releaseMedia();
      render();
      panel.close();
      return;
    }
    st.error = '';
    st.phase = 'idle';
    render();
    panel.close();
  }

  /* ── распознавание ── */

  async function send(blob) {
    if (!blob) return;
    st.phase = 'transcribing';
    st.error = '';
    st.slow = false;
    render();
    panel.open(btn);
    const slow = setTimeout(() => { st.slow = true; renderPanel(); }, SLOW_MS);
    // Первая загрузка модели бывает до минуты; дольше двух — уже сбой.
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const guard = ctrl ? setTimeout(() => ctrl.abort(), 120000) : null;
    try {
      const res = await fetch(TRANS_URL, {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'audio/webm' },
        body: blob,
        credentials: 'same-origin',
        signal: ctrl ? ctrl.signal : undefined,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw sttError(res.status, data);
      const text = String((data && (data.text || data.raw)) || '').trim();
      if (!text) {
        st.blob = null;
        st.error = ERR_EMPTY;
        st.phase = 'error';
        render();
        return;
      }
      applyText(text);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (!err.code && err.name === 'TypeError') err.code = 'network';
      st.error = sttErrorText(err);
      st.phase = 'error';
      render();
    } finally {
      clearTimeout(slow);
      if (guard) clearTimeout(guard);
    }
  }

  function applyText(text) {
    const el = liveField();
    const res = insertAtCursor(el, text);
    st.blob = null;
    st.phase = 'idle';
    st.error = '';
    st.slow = false;
    st.learn = res.inserted ? {
      raw: String(el.value === null || el.value === undefined ? '' : el.value),
      el,
    } : null;
    render();
    panel.close();
    if (typeof el.focus === 'function') { try { el.focus(); } catch { /* не критично */ } }
    if (typeof o.onInsert === 'function') { try { o.onInsert(res, text); } catch (err) { warn('onInsert', err); } }
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
    else if (st.phase === 'error' && st.blob) send(st.blob);
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

export default { createMic, enhanceAll, startAutoEnhance, stopAutoEnhance, attachMic, insertAtCursor };
