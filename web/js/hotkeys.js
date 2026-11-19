/**
 * hotkeys.js — горячие клавиши приложения (W20-U3, пункт 17 / P13).
 *
 * Что делает:
 *   • `J` / `K` — следующий / предыдущий вопрос в правой колонке «Нужен ваш
 *     ответ»: строка прокручивается к центру, фокус встаёт в поле ответа этого
 *     вопроса. Работает по разметке, а не по знанию страницы: список помечен
 *     `data-hotkey-list="questions"`, строки — `data-hotkey-item`, поле —
 *     `data-hotkey-input`. Поэтому колонка вопросов не переписывается.
 *   • `Ctrl+Enter` (и `Cmd+Enter`) — отправить из поля. Свои обработчики полей
 *     (композер, ответ на вопрос, диалог) уже это делают и вызывают
 *     `preventDefault()`; здесь — общий запасной путь для форм и блоков с
 *     атрибутом `data-enter-submit`, чтобы не отправить дважды.
 *   • `Ctrl+B` (и `Cmd+B`) — свернуть меню в полосу значков и развернуть
 *     обратно (W26-D7); на узком окне открывает/закрывает выдвижное меню.
 *   • `?` — справка по клавишам. В полях ввода не срабатывает никогда.
 *   • `V` — простой / подробный вид (view-mode.js).
 *
 * Клавиши одиночных букв молчат, когда человек печатает: проверяется и поле
 * ввода, и `contenteditable`.
 *
 *   import { installHotkeys } from './hotkeys.js';
 *   installHotkeys({ onHelp: () => {}, onToggleView: () => {} });  // один раз
 */

import { icon } from './icons.js';
import { buildDialog, ensureCss } from './ui.js';
import { toggleViewMode, isSimple } from './view-mode.js';
import { RAIL_EVENT } from './shell-rail.js';

/**
 * Клавиши, которыми меню сворачивается (W26-D7): физическая B и буква «и»
 * русской раскладки на той же клавише.
 */
const RAIL_KEYS = new Set(['b', 'B', 'и', 'И']);

/* ── W27-E4: клавиши G, H — «Все комнаты» ───────────────────────────────── */

/**
 * Событие «перейти по адресу» (W27-E4): его шлют клавиши (G, H) и другие места,
 * которым не нужен доступ к роутеру. Слушает каркас (app.js) и вызывает
 * `navigate(detail.hash)`. Адрес — внутренний хэш (`#/…`).
 */
export const NAV_EVENT = 'teamroom:navigate';

/** Сколько ждём вторую клавишу последовательности (G → H), мс. */
export const SEQUENCE_MS = 1500;

/**
 * Попросить каркас перейти по адресу.
 * @param {string} hash
 * @param {Document} [doc]
 * @returns {boolean} событие отправлено?
 */
export function requestNavigate(hash, doc) {
  const target = doc || (typeof globalThis !== 'undefined' ? globalThis.document : null);
  const value = String(hash || '').trim();
  if (!value || !target || typeof target.dispatchEvent !== 'function') return false;
  try {
    const Ctor = typeof CustomEvent === 'function' ? CustomEvent : null;
    if (!Ctor) return false;
    target.dispatchEvent(new Ctor(NAV_EVENT, { bubbles: true, detail: { hash: value } }));
    return true;
  } catch {
    return false;
  }
}

/**
 * Попросить каркас свернуть/развернуть меню. Событие слушает `shell-rail.js`
 * (installShellRail), поэтому клавиша не зависит от каркаса app.js и работает
 * ровно там, где полоса значков существует.
 * @param {Document} doc
 * @returns {boolean} событие отправлено?
 */
export function requestRailToggle(doc) {
  const target = doc || (typeof globalThis !== 'undefined' ? globalThis.document : null);
  if (!target || typeof target.dispatchEvent !== 'function') return false;
  try {
    const Ctor = typeof CustomEvent === 'function' ? CustomEvent : null;
    if (!Ctor) return false;
    target.dispatchEvent(new Ctor(RAIL_EVENT, { bubbles: true }));
    return true;
  } catch {
    return false;
  }
}

/** Селекторы разметки вопросов — один источник для колонки и клавиш. */
export const QUESTION_LIST_SELECTOR = '[data-hotkey-list="questions"]';
export const QUESTION_ITEM_SELECTOR = '[data-hotkey-item]';
export const QUESTION_INPUT_SELECTOR = '[data-hotkey-input]';

/**
 * Справка по клавишам: короткие строки, без техники.
 * `keys` — массив клавиш одной строки (каждая рисуется как <kbd>), поэтому
 * `/` и одиночные символы печатаются ровно так, как их надо нажать.
 */
export const HOTKEY_HELP = [
  // W25-L2 (R5 §7.8): восемь сочетаний страницы запроса.
  // W26-D7: меню сворачивается в полосу значков клавишей Ctrl+B.
  { keys: ['Ctrl + B'], what: 'свернуть меню в полосу значков или развернуть обратно' },
  { keys: ['Ctrl + Shift + F'], what: 'фокус: только свод, без меню, шапки и правой колонки' },
  { keys: ['Esc'], what: 'выйти из фокуса / закрыть лист и меню' },
  { keys: ['[', ']'], what: 'предыдущий и следующий запрос в комнате' },
  { keys: ['A'], what: 'ответить: открыть первый неотвеченный вопрос (когда курсор не в поле)' },
  { keys: ['J', 'K'], what: 'следующий и предыдущий вопрос' },
  { keys: ['Ctrl + Enter'], what: 'отправить сообщение или ответ из поля' },
  { keys: ['V'], what: 'простой или подробный вид' },
  { keys: ['?'], what: 'эта справка' },
  { keys: ['Ctrl + K'], what: 'поиск и команды' },
  { keys: ['/'], what: 'поиск по комнате' },
  { keys: ['N'], what: 'новый запрос' },
  { keys: ['G', 'H'], what: 'все комнаты — список комнат, создание комнаты и быстрый запрос' },
  { keys: ['G', 'R'], what: 'вкладки по очереди: после G — R запрос, T команда, M карта, Д «от вас нужно», O отчёты, J журнал' },
  { keys: ['G', 'T'], what: 'вне комнаты — экран «Сегодня»; в комнате это вкладка «Команда» (там «Сегодня» — G, S)' },
  { keys: ['Esc'], what: 'закрыть окно, меню или выдвижное меню комнат' },
];

/**
 * Плотность — не клавиша, а пункт меню «Действия» и настройка вида: сюда
 * добавили только подсказку, чтобы шпаргалка не врала о полном наборе.
 */
export const DENSITY_HINT_ROW = { keys: ['Вид'], what: 'плотность (компактно / обычно / просторно) — меню «Действия»' };

/** Теги и типы полей, в которых одиночные клавиши не должны ничего менять. */
const TEXT_INPUT_TYPES = new Set([
  'text', 'search', 'email', 'url', 'tel', 'password', 'number', 'date', 'datetime-local',
  'month', 'week', 'time',
]);

/**
 * Печатает ли человек прямо сейчас (поле ввода, textarea, contenteditable).
 * @param {Element|null} node
 * @returns {boolean}
 */
export function isTypingTarget(node) {
  if (!node || node.nodeType !== 1) return false;
  const tag = String(node.tagName || '').toLowerCase();
  if (tag === 'textarea' || tag === 'select') return true;
  if (tag === 'input') {
    const type = String(node.type || 'text').toLowerCase();
    return TEXT_INPUT_TYPES.has(type) || !type;
  }
  return node.isContentEditable === true;
}

/** Список строк вопросов, в порядке экрана. */
export function questionItems(doc = globalThis.document) {
  if (!doc || typeof doc.querySelectorAll !== 'function') return [];
  let nodes = [];
  try {
    nodes = Array.from(doc.querySelectorAll(QUESTION_ITEM_SELECTOR));
  } catch {
    return [];
  }
  return nodes.filter((node) => node && node.isConnected !== false);
}

/**
 * Индекс строки, в которой сейчас фокус (−1, если фокус вне вопросов).
 * @param {Element[]} items
 * @param {Element|null} active
 * @returns {number}
 */
export function focusedIndex(items, active) {
  if (!Array.isArray(items) || !active) return -1;
  for (let i = 0; i < items.length; i += 1) {
    const node = items[i];
    if (node === active) return i;
    if (node && typeof node.contains === 'function' && node.contains(active)) return i;
  }
  return -1;
}

/**
 * Следующий индекс по кругу: с `−1` вперёд → первый, назад → последний.
 * @param {number} current
 * @param {number} delta +1 или −1
 * @param {number} length
 * @returns {number} индекс или −1, если вопросов нет
 */
export function stepIndex(current, delta, length) {
  const total = Math.max(0, Number(length) || 0);
  if (!total) return -1;
  const step = Number(delta) < 0 ? -1 : 1;
  if (!Number.isFinite(current) || current < 0) return step > 0 ? 0 : total - 1;
  return (current + step + total) % total;
}

/**
 * Поставить фокус в поле ответа строки вопроса (или в саму строку).
 * Прокручиваем к центру: вопрос не «прыгает» под шапку.
 * @param {Element|null} node
 * @returns {boolean}
 */
export function focusQuestion(node) {
  if (!node) return false;
  let field = null;
  try {
    field = typeof node.querySelector === 'function' ? node.querySelector(QUESTION_INPUT_SELECTOR) : null;
    if (!field && typeof node.querySelector === 'function') field = node.querySelector('textarea');
  } catch {
    field = null;
  }
  const target = field || node;
  try {
    if (typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  } catch { /* старый браузер без опций */ }
  try {
    if (typeof target.focus === 'function') target.focus({ preventScroll: true });
    return true;
  } catch {
    return false;
  }
}

/**
 * Перейти к вопросу на `delta` строк.
 * @param {number} delta
 * @param {Document} [doc]
 * @returns {{index: number, total: number}|null} null — вопросов на экране нет
 */
export function moveQuestion(delta, doc = globalThis.document) {
  const items = questionItems(doc);
  if (!items.length) return null;
  const active = doc && doc.activeElement ? doc.activeElement : null;
  const from = focusedIndex(items, active);
  const index = stepIndex(from, delta, items.length);
  if (index < 0) return null;
  focusQuestion(items[index]);
  return { index, total: items.length };
}

/**
 * Отправка из поля по Ctrl+Enter — только там, где обработчика ещё нет.
 * Сначала форма (`requestSubmit`), затем блок с `data-enter-submit="<selector>"`.
 * @param {KeyboardEvent} ev
 * @returns {boolean} обработали ли событие
 */
export function submitFromField(ev) {
  if (ev && ev.defaultPrevented) return false;   // поле уже отправило само
  const target = ev && ev.target;
  if (!target || typeof target.closest !== 'function') return false;
  const form = target.closest('form');
  if (form) {
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit();
      return true;
    }
    const submit = form.querySelector('button[type="submit"], input[type="submit"]');
    if (submit && !submit.disabled) { submit.click(); return true; }
    return true;
  }
  const holder = target.closest('[data-enter-submit]');
  if (!holder) return false;
  const selector = String(holder.getAttribute('data-enter-submit') || '').trim();
  const btn = selector
    ? holder.querySelector(selector)
    : holder.querySelector('button[type="submit"], .btn--primary, [data-action="send"]');
  if (!btn || btn.disabled) return false;
  if (typeof ev.preventDefault === 'function') ev.preventDefault();
  btn.click();
  return true;
}

/* ── справка по клавишам ────────────────────────────────────────────────── */

let helpShell = null;

/**
 * Окно «Горячие клавиши» (`?`). Повторный вызов не открывает второе окно.
 * @param {{doc?: Document, onClose?: Function}} [opts]
 * @returns {{close: Function}|null}
 */
export function openHotkeyHelp(opts = {}) {
  const doc = opts.doc || globalThis.document;
  if (!doc || !doc.body) return null;
  if (helpShell) { helpShell.close(); helpShell = null; }
  try { ensureCss('css/hotkeys.css'); } catch { /* стили не обязательны */ }

  const list = doc.createElement('div');
  list.className = 'hotkeys';
  for (const row of HOTKEY_HELP) {
    const line = doc.createElement('div');
    line.className = 'hotkeys__row';
    const keys = doc.createElement('span');
    keys.className = 'hotkeys__keys';
    const tokens = Array.isArray(row.keys) ? row.keys : [String(row.keys || '')];
    for (const part of tokens) {
      const text = String(part || '').trim();
      if (!text) continue;
      const kbd = doc.createElement('kbd');
      kbd.className = 'hotkeys__kbd';
      kbd.textContent = text;
      keys.appendChild(kbd);
    }
    const what = doc.createElement('span');
    what.className = 'hotkeys__what';
    what.textContent = row.what;
    line.appendChild(keys);
    line.appendChild(what);
    list.appendChild(line);
  }

  const note = doc.createElement('p');
  note.className = 'hotkeys__note';
  note.textContent = 'Пока вы печатаете в поле, одиночные клавиши не срабатывают.';

  const body = doc.createElement('div');
  body.appendChild(list);
  body.appendChild(note);

  const closeBtn = doc.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'btn btn--secondary';
  closeBtn.textContent = 'Понятно';

  const shell = buildDialog({
    title: 'Горячие клавиши',
    body,
    buttons: [closeBtn],
    initialFocus: closeBtn,
    onCancel: () => { if (helpShell) { helpShell.close(); helpShell = null; } },
  });
  closeBtn.addEventListener('click', () => {
    if (helpShell) { helpShell.close(); helpShell = null; }
  });
  helpShell = shell;
  if (typeof opts.onClose === 'function') {
    const original = shell.close;
    shell.close = () => { original(); if (helpShell === shell) helpShell = null; opts.onClose(); };
  }
  return shell;
}

/** Закрыть окно справки, если оно открыто. */
export function closeHotkeyHelp() {
  if (!helpShell) return;
  try { helpShell.close(); } catch { /* уже закрыто */ }
  helpShell = null;
}

/* ── установка ──────────────────────────────────────────────────────────── */

/**
 * @typedef {{doc?: Document, onHelp?: Function, onToggleView?: Function,
 *            questionStep?: Function}} HotkeyOptions
 */

/**
 * Подключить горячие клавиши один раз на приложение.
 * Повторный вызов возвращает тот же контроллер (обработчик не дублируется).
 * @param {HotkeyOptions} [opts]
 * @returns {{destroy: () => void, installed: boolean}}
 */
export function installHotkeys(opts = {}) {
  if (installed && installed.doc === (opts.doc || globalThis.document)) return installed;
  const doc = opts.doc || globalThis.document;
  if (!doc || typeof doc.addEventListener !== 'function') {
    return { destroy() {}, installed: false };
  }

  const onKey = (ev) => {
    if (!ev || ev.defaultPrevented) return;              // уже обработано полем
    if (ev.isComposing) return;                          // идёт ввод иероглифов
    const mod = ev.ctrlKey || ev.metaKey;

    if (mod) {
      if (ev.key === 'Enter' || ev.keyCode === 13) { submitFromField(ev); return; }
      // W26-D7: Ctrl+B — меню в полосу значков и обратно. Реагируем и на
      // физическую клавишу (`code`), чтобы русская раскладка не мешала.
      if (RAIL_KEYS.has(String(ev.key || '')) || ev.code === 'KeyB') {
        if (typeof ev.preventDefault === 'function') ev.preventDefault();
        requestRailToggle(doc);
      }
      return;                                            // Ctrl+K и прочее — не наши
    }
    if (ev.altKey) return;
    if (isTypingTarget(ev.target)) return;                // в полях молчим
    const key = String(ev.key || '');
    const code = String(ev.code || '');

    // W27-E4: G, H — «Все комнаты» (`#/`). Первая клавиша только взводит
    // ожидание, вторая — переходит. Раскладка не важна: смотрим и букву, и
    // физическую клавишу (в русской раскладке это «п» и «р»).
    const isG = key === 'g' || key === 'G' || key === 'п' || key === 'П' || code === 'KeyG';
    const isH = key === 'h' || key === 'H' || key === 'р' || key === 'Р' || code === 'KeyH';
    const now = Date.now();
    if (isH && gotoAt && now - gotoAt <= SEQUENCE_MS) {
      gotoAt = 0;
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      requestNavigate('#/', doc);
      return;
    }
    if (isG) { gotoAt = now; return; }
    gotoAt = 0;

    if (key === '?') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (typeof opts.onHelp === 'function') opts.onHelp();
      else openHotkeyHelp({ doc });
      return;
    }
    // J/K в русской раскладке — «о» и «л» на тех же физических клавишах.
    if (key === 'j' || key === 'J' || key === 'о' || key === 'О') {
      const res = moveQuestion(1, doc);
      if (res && typeof ev.preventDefault === 'function') ev.preventDefault();
      return;
    }
    if (key === 'k' || key === 'K' || key === 'л' || key === 'Л') {
      const res = moveQuestion(-1, doc);
      if (res && typeof ev.preventDefault === 'function') ev.preventDefault();
      return;
    }
    if (key === 'v' || key === 'V' || key === 'м' || key === 'М') {
      if (typeof ev.preventDefault === 'function') ev.preventDefault();
      if (typeof opts.onToggleView === 'function') opts.onToggleView();
      else toggleViewMode();
    }
  };

  doc.addEventListener('keydown', onKey);
  installed = {
    doc,
    installed: true,
    destroy() {
      doc.removeEventListener('keydown', onKey);
      closeHotkeyHelp();
      if (installed && installed.doc === doc) installed = null;
    },
  };
  return installed;
}

let installed = null;

/** W27-E4: когда была нажата первая клавиша последовательности (G → H), мс. */
let gotoAt = 0;

/** Для тестов и перезагрузки представления: снять установленный обработчик. */
export function uninstallHotkeys() {
  if (!installed) return;
  try { installed.destroy(); } catch { /* уже снят */ }
  installed = null;
}

/** Текущее состояние вида — для подсказки «V» в справке. */
export function viewModeLabel() {
  return isSimple() ? 'простой' : 'подробный';
}

/** Иконка клавиатуры для кнопки/подсказки справки (единый вид). */
export function hotkeysIcon(size = 14) {
  return icon('key', { size });
}

export default { installHotkeys, uninstallHotkeys, openHotkeyHelp, moveQuestion, HOTKEY_HELP };
