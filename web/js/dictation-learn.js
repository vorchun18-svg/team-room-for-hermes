/**
 * dictation-learn.js — «Запомнить исправление?»
 *
 * Whisper сам не обучается: модель одна и та же, пока её не заменят. Поэтому
 * учимся снаружи, двумя способами:
 *   1) заранее подсказываем словарь терминов (он уходит в подсказку модели);
 *   2) после распознавания правим частые ошибки по списку автозамен.
 *
 * Здесь — первая половина: сравнить продиктованное (raw) и отправленное (final)
 * и предложить пары «как распозналось → как надо». Плашка не блокирует ввод и
 * сама уходит через 15 секунд; отказ и уже показанные пары помним до перезагрузки.
 *
 * Публично ровно два имени: suggestFromEdit() и offerLearning().
 */

import { h } from './util.js';
import { ensureCss } from './ui.js';

const VIEW_CSS = 'css/view-dictation.css';

/** Сколько пар максимум предлагаем за одну правку. */
const MAX_SUGGESTIONS = 5;
/** Замена — не длиннее трёх слов с каждой стороны. */
const MAX_SPAN = 3;
/** Одиночные слова короче — предлоги и союзы, их не учим. */
const MIN_WORD = 3;
/** Сколько ждём до автоскрытия плашки. */
const DEFAULT_TTL_MS = 15000;

/* ------------------------------------------------------------------ слова */

/** Слово в сравнимом виде: без регистра, ё = е. Пунктуация отброшена токенайзером. */
function normWord(s) {
  return String(s === null || s === undefined ? '' : s).toLowerCase().replace(/ё/g, 'е');
}

/** Слова и числа; дефис и апостроф внутри слова остаются его частью. */
const WORD_RE = /[\p{L}\p{N}]+(?:['’\u2011-][\p{L}\p{N}]+)*/gu;

/**
 * Разбить текст на слова (пунктуация и пробелы — разделители, не в счёт).
 * @param {string} text
 * @returns {Array<{surface: string, norm: string}>}
 */
function tokenize(text) {
  const s = String(text === null || text === undefined ? '' : text);
  const out = [];
  WORD_RE.lastIndex = 0;
  let m = WORD_RE.exec(s);
  while (m) {
    out.push({ surface: m[0], norm: normWord(m[0]) });
    if (m.index === WORD_RE.lastIndex) WORD_RE.lastIndex += 1;
    m = WORD_RE.exec(s);
  }
  return out;
}

/**
 * Пары индексов совпавших слов (наибольшая общая подпоследовательность).
 * @param {string[]} a
 * @param {string[]} b
 * @returns {Array<[number, number]>} по возрастанию
 */
function lcsPairs(a, b) {
  const n = a.length;
  const m = b.length;
  if (!n || !m) return [];
  if (n * m > 250000) {
    // Совсем длинные тексты: сравниваем только начало, чтобы не залипнуть.
    const pairs = [];
    for (let i = 0, j = 0; i < n && j < m; i++, j++) {
      if (a[i] !== b[j]) break;
      pairs.push([i, j]);
    }
    return pairs;
  }
  const w = m + 1;
  const dp = new Uint16Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * w + j] = a[i] === b[j]
        ? dp[(i + 1) * w + (j + 1)] + 1
        : Math.max(dp[(i + 1) * w + j], dp[i * w + (j + 1)]);
    }
  }
  const pairs = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { pairs.push([i, j]); i++; j++; }
    else if (dp[(i + 1) * w + j] >= dp[i * w + (j + 1)]) i++;
    else j++;
  }
  return pairs;
}

/**
 * Одна замена из «дырки» между совпавшими словами. null — не предлагаем.
 * @param {Array<{surface: string, norm: string}>} from
 * @param {Array<{surface: string, norm: string}>} to
 * @returns {{from: string, to: string}|null}
 */
function spanSuggestion(from, to) {
  // Вставка или удаление целой фразы — это не ошибка распознавания.
  if (!from.length || !to.length) return null;
  if (from.length > MAX_SPAN || to.length > MAX_SPAN) return null;
  // Одиночные короткие слова — предлоги, союзы, частицы: их правят по стилю.
  if (from.length === 1 && from[0].norm.length < MIN_WORD) return null;
  if (to.length === 1 && to[0].norm.length < MIN_WORD) return null;
  // «в об» → «во об»: все слова служебные.
  if (from.every((t) => t.norm.length < MIN_WORD)) return null;
  if (to.every((t) => t.norm.length < MIN_WORD)) return null;

  const rawFrom = from.map((t) => t.surface).join(' ');
  const rawTo = to.map((t) => t.surface).join(' ');
  // Только регистр или ё — это не ошибка распознавания.
  if (normWord(rawFrom) === normWord(rawTo)) return null;
  return { from: rawFrom, to: rawTo };
}

/**
 * Что пользователь поправил после диктовки: пары «как распозналось → как надо».
 *
 * Сравнение пословное, без регистра, пунктуации и разницы ё/е. Предлагаем
 * только замены в 1–3 слова; вставки и удаления фраз, одиночные предлоги и
 * союзы короче трёх букв — не предлагаем. Если текст переписан почти целиком,
 * не предлагаем ничего.
 *
 * @param {string} raw   что распознала модель
 * @param {string} final что пользователь отправил
 * @returns {Array<{from: string, to: string}>} не больше пяти
 */
export function suggestFromEdit(raw, final) {
  const A = tokenize(raw);
  const B = tokenize(final);
  if (!A.length || !B.length) return [];

  const same = A.length === B.length && A.every((t, i) => t.norm === B[i].norm);
  if (same) return [];

  const pairs = lcsPairs(A.map((t) => t.norm), B.map((t) => t.norm));
  const maxLen = Math.max(A.length, B.length);
  // Почти полностью переписанный текст — это не исправление опечатки: либо
  // общего нет вовсе, либо уцелела десятая часть слов длинной фразы.
  if (maxLen >= 3 && (pairs.length === 0 || (maxLen >= 6 && pairs.length / maxLen < 0.2))) return [];

  const out = [];
  const seen = new Set();
  let ai = 0;
  let bi = 0;
  const stops = pairs.concat([[A.length, B.length]]);
  for (const [i, j] of stops) {
    const fromToks = A.slice(ai, i);
    const toToks = B.slice(bi, j);
    ai = i + 1;
    bi = j + 1;
    const pair = spanSuggestion(fromToks, toToks);
    if (!pair) continue;
    const key = normWord(pair.from) + '\u0000' + normWord(pair.to);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(pair);
    if (out.length >= MAX_SUGGESTIONS) break;
  }
  return out;
}

/* ------------------------------------------------------- память на сессию */

/** Уже показанные пары: второй раз за сессию не пристаём. */
const shownPairs = new Set();
/** Пары, от которых отказались: до перезагрузки молчим. */
const declinedPairs = new Set();

function pairKey(s) {
  return normWord(s.from) + '\u0000' + normWord(s.to);
}

/* --------------------------------------------------------------- запросы */

/**
 * POST через ctx.api, если он есть; иначе — свой fetch. Ошибку отдаём как есть,
 * текст для человека готовит humanError().
 */
async function postLearn(ctx, path, body) {
  if (ctx && ctx.api && typeof ctx.api.post === 'function') {
    return ctx.api.post(path, body);
  }
  let res;
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    const e = new Error('Нет связи с приложением');
    e.code = 'network';
    throw e;
  }
  if (!res.ok) {
    let msg = 'Сервер ответил ' + res.status;
    try {
      const data = await res.json();
      const err = data && data.error ? data.error : data;
      if (err && err.message) msg = String(err.message);
    } catch { /* тело не JSON — оставляем статус */ }
    const e = new Error(msg);
    e.code = 'http_' + res.status;
    throw e;
  }
  return res.json().catch(() => null);
}

/** Человеческий текст ошибки: без кодов и стека. */
function humanError(err) {
  const code = err && err.code ? String(err.code) : '';
  if (code === 'network') {
    return 'Нет связи с приложением — исправление не сохранилось. Текст остался как есть.';
  }
  const msg = err && err.message ? String(err.message) : '';
  if (code === 'stt_unavailable' || /503/.test(code)) {
    return 'Распознавание сейчас не запущено, словарь недоступен. Попробуйте позже.';
  }
  return 'Не получилось сохранить' + (msg ? ': ' + msg.toLowerCase() : '') + '. Попробуйте ещё раз.';
}

/* ------------------------------------------------------------------ плашка */

/**
 * Строка пары «было → стало».
 * @param {{from: string, to: string}} s
 * @param {{checkbox?: boolean}} [opts]
 * @returns {HTMLElement}
 */
function pairLine(s) {
  return h('div', { class: 'dl-plate__pair' },
    h('span', { class: 'dl-plate__from' }, '«' + s.from + '»'),
    h('span', { class: 'dl-plate__arrow', 'aria-hidden': 'true' }, '→'),
    h('span', { class: 'dl-plate__to' }, '«' + s.to + '»'));
}

/**
 * Плашка «Запомнить исправление?».
 *
 * Не диалог: не затемняет экран, не перехватывает клавиатуру и не мешает
 * отправке. Живёт у поля (anchor) или внизу страницы, если якоря нет.
 * Сама прячется через `ttlMs` (по умолчанию 15 с) — наведение и фокус
 * отсрочивают скрытие, чтобы можно было прочитать.
 *
 * @param {{api?: object, toast?: Function}} ctx  контекст вью
 * @param {{raw: string, final: string, anchor?: HTMLElement, ttlMs?: number}} edit
 * @returns {{el: HTMLElement, close: Function}|null} null — предлагать нечего
 */
export function offerLearning(ctx, edit) {
  const c = ctx || {};
  const o = edit || {};
  const all = suggestFromEdit(o.raw, o.final);
  if (!all.length) return null;
  const fresh = all.filter((s) => {
    const k = pairKey(s);
    return !shownPairs.has(k) && !declinedPairs.has(k);
  });
  if (!fresh.length) return null;

  const d = globalThis.document;
  if (!d || typeof d.createElement !== 'function') return null;

  // Якорь бывает разным: узел поля, обёртка поля, документная область вставки
  // (её передаёт композер) или ничего. Во всех случаях плашка должна встать
  // рядом с полем, а не в конец страницы.
  let host = null;
  let floating = false;
  let anchor = o.anchor;
  if (anchor && typeof anchor === 'object' && !anchor.tagName && anchor.el) anchor = anchor.el;
  if (anchor && typeof anchor.appendChild === 'function') {
    const tag = String(anchor.tagName || '').toUpperCase();
    if ((tag === 'INPUT' || tag === 'TEXTAREA') && anchor.parentNode) {
      host = anchor.parentNode;
      floating = true;
    } else {
      host = anchor;
    }
  }
  if (!host) {
    const active = d.activeElement;
    const tag = active ? String(active.tagName || '').toUpperCase() : '';
    if ((tag === 'INPUT' || tag === 'TEXTAREA') && active.parentNode) {
      host = active.parentNode;
      floating = true;
    }
  }
  const toBody = !host;
  if (toBody) host = d.body;
  if (!host || typeof host.appendChild !== 'function') return null;

  for (const s of fresh) shownPairs.add(pairKey(s));

  const ttlMs = Number.isFinite(o.ttlMs) ? Math.max(0, Number(o.ttlMs)) : DEFAULT_TTL_MS;
  const multi = fresh.length > 1;

  const rows = [];
  const list = h('div', { class: 'dl-plate__items' });
  for (const s of fresh) {
    let box = null;
    let row;
    if (multi) {
      box = h('input', { class: 'dl-plate__check', type: 'checkbox' });
      box.checked = true;
      row = h('label', { class: 'dl-plate__item' }, box, pairLine(s));
    } else {
      row = pairLine(s);
    }
    rows.push({ pair: s, box });
    list.appendChild(row);
  }

  const title = multi ? 'Запомнить исправления?' : 'Запомнить исправление?';
  const errBox = h('div', { class: 'dl-plate__error', role: 'alert', hidden: true });
  const foot = h('div', { class: 'dl-plate__foot' });

  const plate = h('div', {
    class: ['dl-plate',
      floating ? 'dl-plate--floating' : '',
      toBody ? 'dl-plate--fixed' : ''],
    role: 'status',
    'aria-live': 'polite',
    'data-dictation-learn': '1',
  },
    h('div', { class: 'dl-plate__head' },
      h('span', { class: 'dl-plate__title' }, title),
      (() => {
        const x = h('button', {
          class: 'btn btn--icon btn--ghost btn--sm dl-plate__close',
          type: 'button',
          title: 'Скрыть',
          'aria-label': 'Скрыть предложение',
        }, '×');
        x.addEventListener('click', () => close());
        return x;
      })()),
    multi ? list : pairLine(fresh[0]),
    errBox,
    foot,
    h('div', { class: 'dl-plate__note' },
      'Whisper не обучается — словарь подскажет слово заранее и исправит его в следующий раз.'));

  let timer = null;
  let closed = false;
  let busy = false;

  function close() {
    if (closed) return;
    closed = true;
    if (timer) clearTimeout(timer);
    timer = null;
    if (plate.parentNode && typeof plate.parentNode.removeChild === 'function') {
      plate.parentNode.removeChild(plate);
    }
  }

  function restart() {
    if (closed || busy || ttlMs <= 0) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(close, ttlMs);
  }

  function setBusy(on) {
    busy = !!on;
    for (const b of foot.childNodes) {
      if (b && b.nodeType === 1) b.disabled = !!on;
    }
    if (on) { if (timer) clearTimeout(timer); timer = null; }
    else restart();
  }

  function showError(text, retry) {
    errBox.textContent = '';
    errBox.removeAttribute('hidden');
    const msg = h('span', { class: 'dl-plate__error-text' }, text);
    const btn = h('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, 'Повторить');
    btn.addEventListener('click', () => {
      errBox.setAttribute('hidden', '');
      retry();
    });
    errBox.appendChild(msg);
    errBox.appendChild(btn);
  }

  function checkedPairs() {
    if (!multi) return fresh.slice(0, 1);
    const picked = [];
    for (const item of rows) {
      if (!item.box || item.box.checked !== false) picked.push(item.pair);
    }
    return picked;
  }

  async function remember(items) {
    if (busy) return;
    const list2 = items && items.length ? items : fresh;
    setBusy(true);
    try {
      for (const s of list2) {
        // Одна пара — отправляем исходный текст целиком: сервер сам сверит слова.
        // Несколько — по паре, иначе сохранились бы и снятые галочки.
        const body = fresh.length === 1
          ? { raw: String(o.raw === null || o.raw === undefined ? '' : o.raw), final: String(o.final === null || o.final === undefined ? '' : o.final), apply: true }
          : { raw: s.from, final: s.to, apply: true };
        await postLearn(c, '/api/stt/learn', body);
      }
      close();
      if (typeof c.toast === 'function') c.toast('Запомнил. В следующий раз исправлю сам.', 'ok');
    } catch (err) {
      showError(humanError(err), () => remember(list2));
    } finally {
      setBusy(false);
    }
  }

  function decline() {
    for (const s of fresh) declinedPairs.add(pairKey(s));
    close();
  }

  if (multi) {
    const save = h('button', { class: 'btn btn--sm btn--primary', type: 'button' }, 'Запомнить выбранные');
    save.addEventListener('click', () => remember(checkedPairs()));
    const all2 = h('button', { class: 'btn btn--sm btn--secondary', type: 'button' }, 'Запомнить все');
    all2.addEventListener('click', () => remember(fresh));
    const no = h('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, 'Не надо');
    no.addEventListener('click', decline);
    foot.appendChild(save);
    foot.appendChild(all2);
    foot.appendChild(no);
  } else {
    const save = h('button', { class: 'btn btn--sm btn--primary', type: 'button' }, 'Запомнить');
    save.addEventListener('click', () => remember(fresh));
    const no = h('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, 'Не надо');
    no.addEventListener('click', decline);
    // Одна пара — «Запомнить все» делает то же, но фраза обещана в макете:
    // человек не должен гадать, куда делась кнопка, когда правок станет больше.
    const allOne = h('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, 'Запомнить все');
    allOne.addEventListener('click', () => remember(fresh));
    foot.appendChild(save);
    foot.appendChild(no);
    foot.appendChild(allOne);
  }

  plate.addEventListener('mouseenter', () => { if (timer) clearTimeout(timer); timer = null; });
  plate.addEventListener('mouseleave', restart);
  plate.addEventListener('focusin', () => { if (timer) clearTimeout(timer); timer = null; });
  plate.addEventListener('focusout', restart);

  host.appendChild(plate);
  ensureCss(VIEW_CSS);
  restart();
  return { el: plate, close };
}
