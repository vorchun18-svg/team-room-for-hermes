/**
 * mic-panel-actions.test.mjs — W20-C1 (B-42): кнопки панели диктовки создаются
 * ОДИН раз на смену фазы, а таймер обновляет только текст.
 *
 * Дефект (подтверждён воспроизведением): `paintPanel` каждый раз чистил контейнер
 * действий и создавал «Стоп»/«Отмену» заново, а таймер записи зовёт его каждые
 * 250 мс. Клик, у которого нажатие и отпускание попали в разные перерисовки
 * (обычно 80–200 мс), браузер не доставлял: нажали на старую кнопку, отпустили на
 * новой. Поэтому здесь проверяется главное свойство: во время обновлений одной и
 * той же фазы узлы кнопок НЕ пересоздаются.
 *
 * Сам факт доставки клика при удержании 150 мс проверяется в настоящем Chromium
 * — `tools/probe_click_loss.py` (0 потерянных кликов).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, FakeElement } from './fake-dom.mjs';

const doc = installFakeDom();
doc.body = new FakeElement('body');
// Панель живёт на <body>; в мини-DOM его надо добавить явно.
globalThis.document = doc;
// В мини-DOM нет removeChild (реальная панель им пользуется при закрытии).
FakeElement.prototype.removeChild = function removeChild(node) {
  const idx = this.childNodes.indexOf(node);
  if (idx >= 0) this.childNodes.splice(idx, 1);
  if (node) node.parentNode = null;
  return node;
};
if (!Object.getOwnPropertyDescriptor(globalThis, 'requestAnimationFrame')) {
  globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(0), 0);
}
globalThis.innerWidth = 1280;
globalThis.innerHeight = 900;

const { createPanel } = await import('../web/js/dictation-panel.js');

/* ───────────────────────────────────────────────────────────── помощники */

function children(el) { return (el && el.childNodes ? el.childNodes : []).filter((n) => n.nodeType === 1); }
function labels(el) { return children(el).map((n) => String(n.textContent || '').trim()); }
function byClass(el, name) {
  const out = [];
  const walk = (node) => {
    for (const child of children(node)) {
      if (child.classList && child.classList.contains(name)) out.push(child);
      walk(child);
    }
  };
  walk(el);
  return out;
}

/** Панель, открытая «у кнопки микрофона» на вымышленном блоке вопроса. */
function openPanel() {
  const anchor = new FakeElement('div');
  anchor.setAttribute('data-mic-anchor', 'q:test');
  doc.body.appendChild(anchor);
  const hits = { stop: 0, cancel: 0, retry: 0, close: 0 };
  const panel = createPanel({
    onStop: () => { hits.stop += 1; },
    onCancel: () => { hits.cancel += 1; },
    onRetry: () => { hits.retry += 1; },
    onClose: () => { hits.close += 1; },
  });
  panel.open(anchor);
  const el = panel.el;
  const actions = byClass(el, 'mic-panel__actions')[0];
  return { panel, el, actions, hits };
}

/* ───────────────────────────────────────────────────────────────── тесты */

test('во время записи «Стоп» и «Отмена» одни и те же при обновлении таймера', () => {
  const { panel, actions } = openPanel();
  try {
    panel.update({ phase: 'recording', timeText: '0:01 / 10:00' });
    const first = children(actions);
    assert.deepEqual(labels(actions), ['Стоп', 'Отмена']);

    const time = byClass(panel.el, 'mic-panel__time')[0];
    for (let i = 2; i <= 40; i += 1) {          // как таймер: 40 обновлений по 250 мс
      panel.update({ phase: 'recording', timeText: '0:' + String(i).padStart(2, '0') + ' / 10:00' });
    }

    const later = children(actions);
    assert.equal(later.length, 2, 'кнопок по-прежнему две');
    assert.equal(later[0], first[0], '«Стоп» — ТОТ ЖЕ узел (клик не теряется)');
    assert.equal(later[1], first[1], '«Отмена» — тот же узел');
    assert.equal(String(time.textContent).trim(), '0:40 / 10:00', 'таймер обновляет текст');
  } finally {
    panel.destroy();
  }
});

test('нажатие «Стоп» после сотни обновлений срабатывает с первого раза', () => {
  const { panel, actions, hits } = openPanel();
  try {
    panel.update({ phase: 'recording', timeText: '0:01 / 10:00' });
    const stop = children(actions)[0];
    for (let i = 0; i < 100; i += 1) panel.update({ phase: 'recording', timeText: '0:01 / 10:00' });
    stop.dispatch('click', { type: 'click', target: stop });
    assert.equal(hits.stop, 1, 'ровно одно срабатывание');
    assert.equal(hits.cancel, 0);
  } finally {
    panel.destroy();
  }
});

test('смена фазы пересобирает кнопки: запись → расшифровка → ошибка', () => {
  const { panel, actions, hits } = openPanel();
  try {
    panel.update({ phase: 'recording', timeText: '0:03 / 10:00' });
    const stop = children(actions)[0];
    stop.dispatch('click', { type: 'click', target: stop });
    assert.equal(hits.stop, 1);

    panel.update({ phase: 'transcribing', progress: 'Распознано 1 из 7 минут' });
    assert.deepEqual(labels(actions), [], 'в «Расшифровываю…» кнопок нет');

    panel.update({ phase: 'error', error: 'Связь с распознаванием пропала', canRetry: true });
    assert.deepEqual(labels(actions), ['Распознать ещё раз', 'Закрыть']);
    const retry = children(actions)[0];
    for (let i = 0; i < 30; i += 1) panel.update({ phase: 'error', error: 'Связь с распознаванием пропала', canRetry: true, progress: 'x' + i });
    assert.equal(children(actions)[0], retry, 'кнопка повтора не пересоздаётся в той же фазе');
    retry.dispatch('click', { type: 'click', target: retry });
    assert.equal(hits.retry, 1);

    panel.update({ phase: 'error', error: 'Связь с распознаванием пропала', canRetry: false });
    assert.deepEqual(labels(actions), ['Закрыть'], 'без сохранённой записи повтора нет');
  } finally {
    panel.destroy();
  }
});

test('«Запрос доступа» и «Отмена» — одна кнопка, не пересоздаётся', () => {
  const { panel, actions } = openPanel();
  try {
    panel.update({ phase: 'arming' });
    const first = children(actions);
    assert.deepEqual(labels(actions), ['Отмена']);
    for (let i = 0; i < 20; i += 1) panel.update({ phase: 'arming' });
    assert.equal(children(actions)[0], first[0]);
  } finally {
    panel.destroy();
  }
});

test('подпись кнопки повтора меняется — тогда кнопка пересобирается (смена набора)', () => {
  const { panel, actions } = openPanel();
  try {
    panel.update({ phase: 'error', error: 'Не вышло', canRetry: true, retryLabel: 'Распознать ещё раз' });
    const first = children(actions)[0];
    panel.update({ phase: 'error', error: 'Не вышло', canRetry: true, retryLabel: 'Попробовать снова' });
    const second = children(actions)[0];
    assert.notEqual(second, first, 'другая подпись — другой набор');
    assert.equal(String(second.textContent).trim(), 'Попробовать снова');
  } finally {
    panel.destroy();
  }
});

test('смена фазы и обратно: кнопки новые, но их снова ровно две', () => {
  const { panel, actions } = openPanel();
  try {
    panel.update({ phase: 'recording', timeText: '0:01 / 10:00' });
    const stop = children(actions)[0];
    panel.update({ phase: 'transcribing', progress: 'Распознано 2 из 7 минут' });
    panel.update({ phase: 'recording', timeText: '0:02 / 10:00' });
    assert.deepEqual(labels(actions), ['Стоп', 'Отмена']);
    assert.notEqual(children(actions)[0], stop, 'после возврата в запись — свежие кнопки');
  } finally {
    panel.destroy();
  }
});
