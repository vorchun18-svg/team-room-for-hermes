/**
 * dictation-panel.js — всплывающая панель диктовки.
 *
 * Отдельно от dictation.js: там запись и сеть, здесь — только вид и место.
 * Панель висит на <body> (position: fixed), поэтому её не обрезают ни диалог
 * с overflow, ни поле с маленькой высотой; над кнопкой, а если сверху нет
 * места — под ней. Никаких текстов, кроме коротких русских подсказок.
 *
 * Публично: createPanel({onStop, onCancel, onRetry, onClose}) → {
 *   el, open(anchor), close(), place(), destroy(), setLevel(pct),
 *   update({phase, elapsed, maxMs, slow, error, canRetry})
 * }
 */

import { h } from './util.js';

const PANEL_W = 300;

/**
 * Панель привязана к блоку СВОЕГО поля, а не к координатам в момент нажатия.
 *
 * Прежний дефект (W15-C, п. 4): привязались к кнопке микрофона и её
 * `getBoundingClientRect()` в момент открытия. Страница запроса перерисовывается
 * по опросу (2 с), вопрос вместе с полем и кнопкой заменялся новым узлом — старая
 * кнопка «отваливалась» от документа, её прямоугольник становился нулевым, и
 * панель уезжала в левый верхний угол (ровно то, что видел Михаил при «Стоп»).
 *
 * Теперь у привязки есть ключ: ближайший блок с `data-mic-anchor` (блок вопроса).
 * Пока панель открыта, место считается по ЖИВОМУ узлу: если прежний элемент
 * выпал из документа, ключ ищется заново — панель возвращается к блоку вопроса,
 * а не в угол. Если живого узла нет вовсе, панель остаётся на последнем верном
 * месте (никогда не прыгает в (8, 8)).
 */
export function anchorKeyOf(el) {
  let node = el;
  while (node && node.nodeType === 1) {
    const key = node.getAttribute && node.getAttribute('data-mic-anchor');
    if (key) return String(key);
    node = node.parentNode;
  }
  return '';
}

function safeQuery(selector) {
  const d = globalThis.document;
  if (!d || typeof d.querySelector !== 'function') return null;
  try { return d.querySelector(selector); } catch { return null; }
}

function byKey(key) {
  if (!key) return null;
  return safeQuery('[data-mic-anchor="' + String(key).replace(/["\\]/g, '\\$&') + '"]');
}


/**
 * Чистая математика места: панель над кнопкой/полем, а если сверху нет места —
 * под ней; по ширине прижата к правому краю блока и не вылезает за окно.
 * Экспортируется для тестов (`node --test`) — та же функция, что в браузере.
 * @param {{left:number, right:number, top:number, bottom:number}} rect
 * @param {{width:number, height:number}} viewport
 * @param {number} height высота панели
 * @param {number} [panelWidth]
 * @returns {{left:number, top:number, width:number}}
 */
export function panelPlacement(rect, viewport, height, panelWidth = PANEL_W) {
  const vw = Number(viewport && viewport.width) || 1280;
  const vh = Number(viewport && viewport.height) || 900;
  const width = Math.min(Number(panelWidth) || PANEL_W, vw - 16);
  const box = Number(height) || 150;
  const left = Math.max(8, Math.min(Number(rect.right) - width, vw - width - 8));
  let top = Number(rect.top) - box - 8;
  if (top < 8) top = Math.min(Number(rect.bottom) + 8, vh - box - 8);
  return { left: Math.round(left), top: Math.round(Math.max(8, top)), width: Math.round(width) };
}

/** Иконка codicon (mic/mic-filled есть в codicon.ttf: EC12/EC1C). */
function glyph(name, size = 15) {
  const i = globalThis.document.createElement('i');
  i.className = 'codicon codicon-' + name;
  i.setAttribute('aria-hidden', 'true');
  i.style.fontSize = size + 'px';
  return i;
}

function actionBtn(label, cls, onclick) {
  return h('button', { class: 'btn btn--sm ' + cls, type: 'button', onclick }, label);
}

/**
 * @param {{onStop?: Function, onCancel?: Function, onRetry?: Function, onClose?: Function}} handlers
 */
export function createPanel(handlers = {}) {
  const on = handlers || {};
  const d = globalThis.document;
  let anchorEl = null;
  let anchorKey = '';       // ключ блока (data-mic-anchor) — переживает перерисовку
  let placed = false;       // было ли хоть одно верное место
  let els = null;
  let panel = null;         // сам узел панели (в <body>, position: fixed)
  let watcher = null;       // слежение за перерисовкой, пока панель открыта
  let tickHandle = 0;

  /** Прямоугольник элемента или null, если элемента нет/нет раскладки. */
  function rectOf(el) {
    if (!el || typeof el.getBoundingClientRect !== 'function') return null;
    try { return el.getBoundingClientRect(); } catch { return null; }
  }

  /** Прямоугольник пригоден для привязки: у выпавшего из документа он нулевой. */
  function usable(rect) {
    return Boolean(rect) && (Number(rect.width) > 0 || Number(rect.height) > 0);
  }

  /**
   * Живой узел того же блока вопроса: старая кнопка/поле могли быть заменены
   * перерисовкой, а блок с `data-mic-anchor` — тот же самый.
   */
  function liveAnchor() {
    return byKey(anchorKey);
  }

  /** Места ещё нет вовсе — показываем у нижнего края, но не в левом верхнем углу. */
  function restPosition() {
    if (!panel || !panel.style) return;
    const vw = Number(globalThis.innerWidth) || 1280;
    const vh = Number(globalThis.innerHeight) || 900;
    const width = Math.min(PANEL_W, vw - 16);
    const height = Number(panel.offsetHeight) || 150;
    panel.style.width = Math.round(width) + 'px';
    panel.style.left = Math.round(Math.max(8, vw - width - 16)) + 'px';
    panel.style.top = Math.round(Math.max(8, vh - height - 16)) + 'px';
  }

  function ensure() {
    if (panel) return panel;
    if (!d || !d.body) return null;
    els = {};
    els.dot = h('span', { class: 'mic-dot', 'aria-hidden': 'true' });
    els.spin = h('span', { class: 'mic-spin hidden', 'aria-hidden': 'true' }, glyph('loading', 14));
    if (els.spin.firstChild) els.spin.firstChild.classList.add('codicon-modifier-spin');
    els.title = h('span', { class: 'mic-panel__title' });
    els.time = h('span', { class: 'mic-panel__time tnum' });
    els.head = h('div', { class: 'mic-panel__head' }, els.dot, els.spin, els.title, els.time);
    els.bar = h('span', { class: 'mic-level__bar' });
    els.level = h('div', {
      class: 'mic-level', role: 'progressbar', 'aria-label': 'Уровень звука',
      'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0',
    }, els.bar);
    // Панель открывается и без записи (нерасшифрованная запись) — точка и
    // индикатор уровня появляются только в состоянии «Запись».
    els.dot.classList.add('hidden');
    els.level.classList.add('hidden');
    els.hint = h('div', { class: 'mic-panel__hint' });
    els.progress = h('div', { class: 'mic-panel__progress tnum hidden', role: 'status' });
    els.recover = h('div', { class: 'mic-panel__recover hidden' });
    els.msg = h('div', { class: 'mic-panel__msg hidden' });
    els.actions = h('div', { class: 'mic-panel__actions' });

    panel = h('div', {
      class: 'mic-panel', role: 'dialog', 'aria-label': 'Диктовка', 'data-mic-panel': 'true',
    }, els.head, els.level, els.hint, els.progress, els.recover, els.msg, els.actions);
    // Клик по панели не должен уводить фокус из поля — иначе вставка «не туда».
    panel.addEventListener('mousedown', (ev) => {
      const tag = String((ev && ev.target && ev.target.tagName) || '').toUpperCase();
      if (ev && typeof ev.preventDefault === 'function' && tag !== 'INPUT' && tag !== 'TEXTAREA') {
        ev.preventDefault();
      }
    });
    d.body.appendChild(panel);
    if (globalThis.addEventListener) {
      globalThis.addEventListener('resize', place);
      globalThis.addEventListener('scroll', place, true);
    }
    place();
    return panel;
  }

  function place() {
    if (!panel || !panel.style) return;
    // Привязка пересчитывается по ЖИВОМУ узлу: после перерисовки страницы
    // прежняя кнопка выпадает из документа и её прямоугольник обнуляется.
    let rect = rectOf(anchorEl);
    if (!usable(rect)) rect = null;
    if (!rect && anchorKey) {
      const live = liveAnchor();
      if (live) {
        anchorEl = live;
        rect = rectOf(live);
        if (!usable(rect)) rect = null;
      }
    }
    if (!rect) {
      // Живого узла нет (блок вопроса перерисован/уехал из документа):
      // остаёмся на последнем верном месте — в угол не прыгаем.
      if (!placed) restPosition();
      return;
    }
    const vw = Number(globalThis.innerWidth) || 1280;
    const vh = Number(globalThis.innerHeight) || 900;
    const box = panelPlacement(rect, { width: vw, height: vh }, Number(panel.offsetHeight) || 150);
    panel.style.left = box.left + 'px';
    panel.style.top = box.top + 'px';
    panel.style.width = box.width + 'px';
    placed = true;
  }

  /** Пересчитать место после перерисовки страницы (дребезг: один кадр). */
  function schedulePlace() {
    if (tickHandle) return;
    const raf = globalThis.requestAnimationFrame;
    if (typeof raf === 'function') { tickHandle = raf(() => { tickHandle = 0; place(); }); return; }
    tickHandle = setTimeout(() => { tickHandle = 0; place(); }, 80);
  }

  /** Пока панель открыта — следим за перерисовкой страницы, а не только за прокруткой. */
  function watch() {
    if (watcher || typeof globalThis.MutationObserver !== 'function' || !d || !d.body) return;
    try {
      watcher = new globalThis.MutationObserver(() => schedulePlace());
      watcher.observe(d.body, { childList: true, subtree: true });
    } catch { watcher = null; }
  }

  function unwatch() {
    if (watcher) { try { watcher.disconnect(); } catch { /* уже отключён */ } watcher = null; }
    if (tickHandle) {
      if (typeof globalThis.cancelAnimationFrame === 'function') globalThis.cancelAnimationFrame(tickHandle);
      else clearTimeout(tickHandle);
      tickHandle = 0;
    }
  }

  function open(anchor) {
    if (anchor) {
      anchorEl = anchor;
      anchorKey = anchorKeyOf(anchor);
      // Панель держится у БЛОКА вопроса, а не у кнопки микрофона внутри него:
      // кнопка стоит внизу строки, и панель наезжала бы на само поле ответа.
      if (anchorKey) {
        const host = byKey(anchorKey);
        if (host) anchorEl = host;
      }
    }
    const el = ensure();
    if (el) { placed = false; place(); watch(); }
    return el;
  }

  function close() {
    unwatch();
    if (panel && panel.parentNode) panel.parentNode.removeChild(panel);
    panel = null;
    els = null;
    anchorEl = null;
    anchorKey = '';
    placed = false;
    if (globalThis.removeEventListener) {
      globalThis.removeEventListener('resize', place);
      globalThis.removeEventListener('scroll', place, true);
    }
  }

  function setLevel(pct) {
    const p = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    if (!els) return;
    els.bar.style.width = p + '%';
    els.level.setAttribute('aria-valuenow', String(p));
  }

  /**
   * @param {{phase: string, elapsed?: number, maxMs?: number, slow?: boolean,
   *          error?: string, canRetry?: boolean, timeText?: string,
   *          progress?: string, retryLabel?: string}} state
   */
  function paintPanel(state) {
    if (!els) return;
    const st = state || {};
    const phase = String(st.phase || 'idle');
    els.actions.replaceChildren();
    els.spin.classList.add('hidden');
    els.dot.classList.add('hidden');
    els.level.classList.toggle('hidden', phase !== 'recording');
    els.msg.classList.toggle('hidden', !st.error);
    // Прогресс распознавания: «Распознано 3 из 7 минут» — рядом с полем, а не в тосте.
    const progress = String(st.progress || '');
    els.progress.textContent = progress;
    els.progress.classList.toggle('hidden', !progress);

    if (phase === 'recording') {
      els.dot.classList.remove('hidden');
      els.title.textContent = 'Запись';
      els.time.textContent = String(st.timeText || '0:00 / 10:00');
      els.time.classList.remove('hidden');
      els.hint.textContent = st.hint || 'Говорите — текст встанет туда, где курсор.';
      els.msg.textContent = '';
      els.actions.appendChild(actionBtn('Стоп', 'btn--primary', () => on.onStop && on.onStop()));
      els.actions.appendChild(actionBtn('Отмена', 'btn--ghost', () => on.onCancel && on.onCancel()));
      return;
    }

    if (phase === 'arming') {
      els.title.textContent = 'Запрос доступа к микрофону';
      els.time.classList.add('hidden');
      els.hint.textContent = 'Браузер спросит разрешение — нажмите «Разрешить».';
      els.msg.textContent = '';
      els.actions.appendChild(actionBtn('Отмена', 'btn--ghost', () => on.onCancel && on.onCancel()));
      return;
    }

    if (phase === 'transcribing') {
      els.spin.classList.remove('hidden');
      els.title.textContent = 'Расшифровываю…';
      els.time.classList.add('hidden');
      els.hint.textContent = st.hint || 'Поле не заблокировано — можно печатать.';
      els.msg.textContent = '';
      return;
    }

    if (phase === 'error') {
      els.title.textContent = 'Не получилось';
      els.time.classList.add('hidden');
      els.hint.textContent = st.hint || 'Запись сохранена — поле свободно, можно печатать.';
      els.msg.textContent = String(st.error || '');
      if (st.canRetry) {
        els.actions.appendChild(actionBtn(String(st.retryLabel || 'Распознать ещё раз'),
          'btn--primary', () => on.onRetry && on.onRetry()));
      }
      els.actions.appendChild(actionBtn('Закрыть', 'btn--ghost', () => on.onClose && on.onClose()));
      return;
    }

    close();
  }

  /**
   * Обновить вид панели и пересчитать её место: высота меняется (подсказка,
   * прогресс «распознано 3 из 7 минут», текст ошибки), а панель должна остаться
   * у своего блока вопроса — до, во время и после «Стоп».
   */
  function update(state) {
    paintPanel(state);
    if (panel) schedulePlace();
  }

  /**
   * Строка «есть нерасшифрованная запись»: показывается, пока она не разобрана.
   * @param {?{text: string, hint?: string, onRecover?: Function, onDismiss?: Function}} item
   */
  /** Заголовок панели (нужен, когда показывается только нерасшифрованная запись). */
  function setTitle(text) {
    if (els) els.title.textContent = String(text || '');
  }

  function setRecover(item) {
    if (!els) return;
    els.recover.replaceChildren();
    if (!item || !item.text) {
      els.recover.classList.add('hidden');
      return;
    }
    els.recover.classList.remove('hidden');
    els.recover.appendChild(h('div', { class: 'mic-panel__recover-text' }, String(item.text)));
    if (item.hint) {
      els.recover.appendChild(h('div', { class: 'mic-panel__recover-hint' }, String(item.hint)));
    }
    const row = h('div', { class: 'mic-panel__recover-actions' });
    if (typeof item.onRecover === 'function') {
      row.appendChild(actionBtn('Распознать запись', 'btn--secondary', () => item.onRecover()));
    }
    if (typeof item.onDismiss === 'function') {
      row.appendChild(actionBtn('Убрать', 'btn--ghost', () => item.onDismiss()));
    }
    els.recover.appendChild(row);
  }

  return {
    get el() { return panel; },
    open,
    close,
    place,
    setLevel,
    setTitle,
    setRecover,
    update,
    destroy: close,
    isOpen: () => Boolean(panel),
    /** Ключ привязки (data-mic-anchor) — для тестов и отладки. */
    key: () => anchorKey,
  };
}

export default { createPanel, panelPlacement, anchorKeyOf, findAnchorByKey: byKey };
