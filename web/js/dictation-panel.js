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
  let els = null;
  let panel = null;

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
    els.hint = h('div', { class: 'mic-panel__hint' });
    els.msg = h('div', { class: 'mic-panel__msg hidden' });
    els.actions = h('div', { class: 'mic-panel__actions' });

    panel = h('div', {
      class: 'mic-panel', role: 'dialog', 'aria-label': 'Диктовка', 'data-mic-panel': 'true',
    }, els.head, els.level, els.hint, els.msg, els.actions);
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
    if (!panel || !panel.style || !anchorEl) return;
    let rect = null;
    try { rect = anchorEl.getBoundingClientRect(); } catch { rect = null; }
    if (!rect) return;
    const vw = Number(globalThis.innerWidth) || 1280;
    const vh = Number(globalThis.innerHeight) || 900;
    const width = Math.min(PANEL_W, vw - 16);
    const height = Number(panel.offsetHeight) || 150;
    const left = Math.max(8, Math.min(rect.right - width, vw - width - 8));
    let top = rect.top - height - 8;
    if (top < 8) top = Math.min(rect.bottom + 8, vh - height - 8);
    panel.style.left = Math.round(left) + 'px';
    panel.style.top = Math.round(Math.max(8, top)) + 'px';
    panel.style.width = Math.round(width) + 'px';
  }

  function open(anchor) {
    if (anchor) anchorEl = anchor;
    const el = ensure();
    if (el) place();
    return el;
  }

  function close() {
    if (panel && panel.parentNode) panel.parentNode.removeChild(panel);
    panel = null;
    els = null;
    anchorEl = null;
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
   *          error?: string, canRetry?: boolean, timeText?: string}} state
   */
  function update(state) {
    if (!els) return;
    const st = state || {};
    const phase = String(st.phase || 'idle');
    els.actions.replaceChildren();
    els.spin.classList.add('hidden');
    els.dot.classList.add('hidden');
    els.level.classList.toggle('hidden', phase !== 'recording');
    els.msg.classList.toggle('hidden', !st.error);

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
      els.hint.textContent = 'Пока можно печатать вручную.';
      els.msg.textContent = String(st.error || '');
      if (st.canRetry) els.actions.appendChild(actionBtn('Повторить', 'btn--primary', () => on.onRetry && on.onRetry()));
      els.actions.appendChild(actionBtn('Закрыть', 'btn--ghost', () => on.onClose && on.onClose()));
      return;
    }

    close();
  }

  return {
    get el() { return panel; },
    open,
    close,
    place,
    setLevel,
    update,
    destroy: close,
    isOpen: () => Boolean(panel),
  };
}

export default { createPanel };
