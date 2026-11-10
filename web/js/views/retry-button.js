/**
 * retry-button.js — кнопка «Повторить» с откликом и идемпотентностью (B-25).
 *
 * Почему отдельный модуль: кнопка живёт в двух местах (карточка дерева работ и блок
 * «Ответ не получился»), а состояния у неё общие и проверяются тестами:
 *
 *   обычная → «Перезапуск отправлен» (заблокирована, рядом «принято, карточка в очереди»)
 *   → снова обычная, когда началась новая попытка (вырос номер попытки) или истёк срок.
 *
 * Отметка об отправке лежит в `sessionStorage` (`createMarks`), поэтому перерисовка по
 * опросу не разблокирует кнопку и не даёт нажать её второй раз. На сервер уходит
 * `idempotency_key` — повторная доставка того же нажатия не создаёт дубль (B-25).
 *
 * Чистые решения (`retryPhase`, `retrySignature`) — в request-parts-model.js.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { confirmDialog } from '../ui.js';
import { toast, changed, postAction } from './rp-shared.js';
import { retryPhase, retrySignature, retryKey, RETRY_SENT_TEXT, RETRY_SENT_HINT } from './request-parts-model.js';

/** Ключ идемпотентности одного нажатия (уникален, повтор того же клика — тот же ключ). */
export function newIdempotencyKey(taskId) {
  return `retry-${String(taskId || 'request')}-${Date.now().toString(36)}-`
    + Math.random().toString(36).slice(2, 8);
}

/**
 * Собрать кнопку повтора.
 *
 * @param {object} opts
 * @param {object} opts.ctx        контекст страницы (api, toast)
 * @param {string} opts.rid        id запроса
 * @param {string} [opts.taskId]   id карточки (пусто — корневая карточка запроса)
 * @param {object} opts.node       данные карточки для подписи состояния
 * @param {object} [opts.marks]    хранилище отметок (createMarks)
 * @param {string} [opts.label]    подпись обычной кнопки
 * @param {string} [opts.title]    заголовок окна подтверждения
 * @param {string} [opts.text]     текст окна подтверждения
 * @param {Function} [opts.onSent] что сделать после принятого перезапуска
 * @returns {HTMLElement}
 */
export function createRetryButton(opts = {}) {
  const o = opts || {};
  const rid = String(o.rid || '');
  const taskId = String(o.taskId || '');
  const key = retryKey(rid, taskId);
  const marks = o.marks || null;
  const label = String(o.label || 'Повторить');

  const wrap = h('span', { class: 'rp-retry' });
  const btn = h('button', {
    type: 'button', class: 'btn btn--secondary btn--sm',
    title: 'Запустить задачу заново',
  }, icon('refresh', { size: 13 }), h('span', {}, label));
  const status = h('span', { class: 'rp-retry__status small', 'aria-live': 'polite' });
  wrap.append(btn, status);

  function signature() {
    return retrySignature(o.node || { id: taskId });
  }

  function paint() {
    const mark = marks ? marks.get(key) : null;
    const sent = retryPhase(mark, signature(), Date.now()) === 'sent';
    const text = btn.querySelector('span');
    if (text) text.textContent = sent ? 'Перезапуск отправлен' : label;
    btn.disabled = sent;
    btn.title = sent ? RETRY_SENT_HINT : 'Запустить задачу заново';
    if (sent) btn.setAttribute('aria-disabled', 'true');
    else btn.removeAttribute('aria-disabled');
    wrap.classList.toggle('rp-retry--sent', sent);
    status.textContent = sent ? RETRY_SENT_TEXT : '';
    // Отметка больше не нужна: либо попытка началась, либо срок вышел.
    if (marks && mark && !sent) marks.clear(key);
  }

  async function send() {
    const ok = await confirmDialog({
      title: String(o.title || 'Повторить задачу?'),
      text: String(o.text || 'Задача будет запущена заново. Это новый прогон агента.'),
      confirmLabel: 'Повторить',
    });
    if (!ok) return 'cancelled';
    const idem = newIdempotencyKey(taskId);
    const body = { action: 'retry', idempotency_key: idem };
    if (taskId) body.task_id = taskId;
    try {
      await postAction(o.ctx, rid, body);
      if (marks) marks.mark(key, { sig: signature(), at: Date.now(), key: idem, task: taskId });
      toast(o.ctx, RETRY_SENT_TEXT, 'ok');
      paint();
      if (typeof o.onSent === 'function') o.onSent();
      changed(o.ctx);
      return 'sent';
    } catch (err) {
      toast(o.ctx, (err && err.message) || 'Не удалось повторить задачу', 'danger');
      return 'error';
    }
  }

  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;                  // мгновенный отклик: второй клик не уходит
    wrap.classList.add('rp-retry--busy');
    const res = await send();
    wrap.classList.remove('rp-retry--busy');
    if (res !== 'sent') paint();          // отменили или ошибка — снова обычная
  });

  paint();
  return wrap;
}

export default { createRetryButton, newIdempotencyKey };
