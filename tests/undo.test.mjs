/**
 * undo.test.mjs — общая «Отмена» на 10 секунд (W29-Q7).
 *
 * Проверяем то, ради чего компонент и написан:
 *   1) действие ОТЛОЖЕНО: до конца отсчёта на сервере ничего не происходит;
 *   2) «Вернуть» отменяет задуманное — откатывать нечего, второго запроса нет;
 *   3) по истечении срока действие выполняется ровно один раз;
 *   4) повторное действие по тому же предмету не создаёт второй тост;
 *   5) «закрыть окно» (commitAll) выполняет отложенное, а не теряет его;
 *   6) текст и остаток секунд в тосте настоящие (`3 с`, `2 с`, …).
 *
 *   node --test tests/undo.test.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installMiniDom, resetMiniDom, DOC } from './mini-dom.mjs';

installMiniDom();

const {
  createUndo, undoText, leftLabel, UNDO_MS,
} = await import('../web/js/undo.js');
const { h } = await import('../web/js/util.js');

function host() {
  resetMiniDom();
  const box = h('div', { class: 'toasts', id: 'toasts' });
  DOC.body.appendChild(box);
  return box;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('срок отмены — 10 секунд, подпись с остатком', () => {
  assert.equal(UNDO_MS, 10000);
  assert.equal(undoText('Запрос в архиве', 10), 'Запрос в архиве · 10 с');
  assert.equal(leftLabel(0), '0 с');
  assert.equal(leftLabel(-5), '0 с');
});

test('действие отложено: до конца отсчёта ничего не выполнено', async () => {
  const box = host();
  const done = [];
  const u = createUndo({ host: box, ms: 45 });
  const id = u.offer({ text: 'Запрос в архиве', action: () => done.push('archive') });
  assert.equal(u.size, 1);
  assert.equal(done.length, 0, 'на сервер ничего не ушло, пока идёт отсчёт');
  assert.ok(id);
  await wait(15);
  assert.equal(done.length, 0);
  await wait(60);
  assert.deepEqual(done, ['archive'], 'по истечении срока действие выполнено один раз');
  assert.equal(u.size, 0);
  assert.equal(box.childNodes.length, 0, 'тост ушёл вместе с действием');
});

test('«Вернуть» отменяет действие полностью', async () => {
  const box = host();
  const done = [];
  const undone = [];
  const u = createUndo({ host: box, ms: 60 });
  const id = u.offer({
    text: 'Запрос в архиве',
    action: () => done.push('archive'),
    undo: () => undone.push('revert'),
  });
  const btn = box.querySelector('.undo__btn');
  assert.ok(btn, 'у тоста есть кнопка «Вернуть»');
  assert.equal(btn.textContent, 'Вернуть');
  assert.equal(await u.revert(id), true);
  await wait(90);
  assert.deepEqual(done, [], 'действие не выполнилось — откатывать нечего');
  assert.deepEqual(undone, ['revert']);
  assert.equal(u.size, 0);
  assert.equal(box.childNodes.length, 0);
});

test('кнопка тоста «Вернуть» работает так же, как revert', async () => {
  const box = host();
  const done = [];
  const u = createUndo({ host: box, ms: 60 });
  u.offer({ text: 'Запрос в архиве', action: () => done.push('archive') });
  const btn = box.querySelector('.undo__btn');
  btn.listeners.click[0]();
  await wait(90);
  assert.deepEqual(done, []);
});

test('повтор по тому же предмету: прежнее выполняется сразу, тост один', async () => {
  const box = host();
  const done = [];
  const u = createUndo({ host: box, ms: 200 });
  u.offer({ key: 'req-1', text: 'Запрос в архиве', action: () => done.push('first') });
  u.offer({ key: 'req-1', text: 'Запрос в архиве', action: () => done.push('second') });
  assert.deepEqual(done, ['first'], 'первое действие закрыто, второе ждёт');
  assert.equal(u.size, 1);
  assert.equal(box.childNodes.length, 1, 'второй тост не появился — один предмет, одна отмена');
  await u.commit('req-1');
  assert.deepEqual(done, ['first', 'second']);
});

test('несколько отмен рядом: каждое действие ждёт своей кнопки', async () => {
  const box = host();
  const done = [];
  const u = createUndo({ host: box, ms: 40 });
  u.offer({ text: 'Запрос в архиве', action: () => done.push('a') });
  u.offer({ text: 'Дело закрыто', action: () => done.push('b') });
  assert.equal(u.size, 2);
  assert.equal(box.childNodes.length, 2);
  await wait(70);
  assert.deepEqual(done.sort(), ['a', 'b']);
  assert.equal(u.size, 0);
});

test('уход со страницы не теряет отложенное: commitAll выполняет всё', async () => {
  const box = host();
  const done = [];
  const u = createUndo({ host: box, ms: 5000 });
  u.offer({ text: 'Запрос в архиве', action: () => done.push('a') });
  u.offer({ text: 'Дело закрыто', action: () => done.push('b') });
  const n = await u.commitAll();
  assert.equal(n, 2);
  assert.deepEqual(done, ['a', 'b']);
  assert.equal(u.size, 0);
});

test('destroy снимает тосты и не выполняет действия', async () => {
  const box = host();
  const done = [];
  const u = createUndo({ host: box, ms: 40 });
  u.offer({ text: 'Запрос в архиве', action: () => done.push('a') });
  u.destroy();
  await wait(70);
  assert.deepEqual(done, []);
  assert.equal(box.childNodes.length, 0);
});

test('в тосте настоящий остаток секунд', async () => {
  const box = host();
  const u = createUndo({ host: box, ms: 1200 });
  u.offer({ text: 'Запрос в архиве', action: () => {} });
  const text = box.querySelector('.undo__text');
  assert.match(text.textContent, /^Запрос в архиве · \d с$/);
  assert.equal(u.pending()[0].left >= 0, true);
  u.destroy();
});
