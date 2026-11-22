/**
 * duties-w29.test.mjs — W29-P5: дежурства комнаты, календарь, мастер.
 *
 * Проверяем то, что человек видит и нажимает:
 *  1) неделя листается: подпись «16–22 ноября», prev/next, панель дня с планом;
 *  2) системные задачи Hermes — отдельный блок, не дежурства комнаты;
 *  3) «куда придёт результат» и «кто выполняет и на какой модели» — словами;
 *  4) мастер: выбор модели из каталога, «своя модель», готовые скрипты, «Выбрать…»;
 *  5) тело запроса совпадает с тем, что понимает сервер (вид → script/monitor).
 *
 * DOM — tests/fake-dom.mjs.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom } from './fake-dom.mjs';

installFakeDom();

const model = await import('../web/js/views/duty-model.js');
const view = await import('../web/js/views/duties.js');

function textOf(node) {
  if (!node) return '';
  const own = node.nodeType === 3 ? node.data : '';
  const kids = Array.isArray(node.childNodes) ? node.childNodes.map(textOf).join(' ') : '';
  return (own + ' ' + kids).replace(/\s+/g, ' ').trim();
}

function walk(node, out = []) {
  if (!node) return out;
  out.push(node);
  for (const child of node.childNodes || []) walk(child, out);
  return out;
}

const WEEK = {
  start: '2026-11-16', end: '2026-11-22', prev: '2026-11-09', next: '2026-11-23',
  today: '2026-11-18', runs: 4, failed: 0, missed: 0,
  days: [
    { date: '2026-11-16', weekday: 0, label: 'пн', day: 16, month: 11, runs: 2, jobs: [] },
    { date: '2026-11-17', weekday: 1, label: 'вт', day: 17, month: 11, runs: 2, jobs: [] },
    { date: '2026-11-18', weekday: 2, label: 'ср', day: 18, month: 11, today: true, runs: 0,
      jobs: [], planned: 3, planned_jobs: [{ job_id: 'j1', name: 'Проверка сроков', runs: 3 }] },
    { date: '2026-11-19', weekday: 3, label: 'чт', day: 19, month: 11, future: true, runs: 0,
      planned: 3, planned_jobs: [{ job_id: 'j1', name: 'Проверка сроков', runs: 3 }] },
    { date: '2026-11-20', weekday: 4, label: 'пт', day: 20, month: 11, future: true, runs: 0 },
    { date: '2026-11-21', weekday: 5, label: 'сб', day: 21, month: 11, future: true, runs: 0 },
    { date: '2026-11-22', weekday: 6, label: 'вс', day: 22, month: 11, future: true, runs: 0 },
  ],
};

const CATALOG = {
  providers: [
    { id: 'fictional', title: 'Вымышленный', models: [{ id: 'small-model', title: 'Малая' },
      { id: 'big-model', title: 'Большая' }] },
  ],
};

const DUTY = {
  job_id: 'j_watch', name: 'Проверка сроков', enabled: true,
  schedule: 'every 30m', schedule_human: 'каждые 30 минут',
  bound_rooms: ['cafe'], last_run: null, next_run_at: null, missed_recent: 0, recent: [],
  excerpt: '', problem: null, kind: 'model',
  deliver: { where: 'inbox', label: 'Входящие комнаты', href: '#/room/cafe/inbox',
    text: 'Итог придёт во «Входящие» комнаты — при изменении.' },
  model: 'small-model', provider: 'fictional', workdir: '',
};

test('неделя: подпись диапазона и prev/next для стрелок', () => {
  const week = view.weekModel(WEEK);
  assert.equal(view.weekRangeText(week), '16–22 ноября 2026');
  assert.equal(week.prev, '2026-11-09');
  assert.equal(week.next, '2026-11-23');
  assert.equal(week.days[3].plannedJobs.length, 1);
  assert.equal(week.days[3].plannedJobs[0].name, 'Проверка сроков');
});

test('подпись недели на стыке месяцев не врёт', () => {
  const cross = { start: '2026-11-30', days: [
    { date: '2026-11-30', day: 30, month: 11, weekday: 0 },
    { date: '2026-12-06', day: 6, month: 12, weekday: 6 },
  ] };
  assert.equal(view.weekRangeText(cross), '30 ноября – 6 декабря 2026');
});

test('каталог моделей: плоский список, «по умолчанию» первым', () => {
  const options = model.modelOptions(CATALOG);
  assert.equal(options[0].id, 'default');
  assert.equal(options.length, 3);
  assert.equal(options[1].id, 'fictional|small-model');
  assert.match(options[1].label, /Вымышленный/);
});

test('строка «кто выполняет и на какой модели» называет модель, а не «дешёвую»', () => {
  assert.match(model.modelLine({ kind: 'model' }, CATALOG), /модель по умолчанию/);
  assert.match(model.modelLine({ kind: 'model', model: 'small-model', provider: 'fictional' }, CATALOG),
    /Вымышленный · Малая/);
  assert.match(model.modelLine({ kind: 'script' }), /без модели/);
});

test('скрипт без модели: три готовых шаблона и имя файла без путей', () => {
  assert.equal(model.SCRIPT_TEMPLATES.length, 3);
  const labels = model.SCRIPT_TEMPLATES.map((one) => one.label);
  assert.deepEqual(labels, ['изменилась веб-страница', 'появились новые файлы в папке',
    'изменился файл']);
  const draft = model.applyScriptTemplate({ title: 'Проверка', kind: 'model' }, 'folder');
  assert.equal(draft.kind, 'script');
  assert.equal(draft.task, 'room-folder-new.ps1');
  assert.equal(draft.script_template, 'folder');
  assert.equal(model.scriptName('C:/Work/tools/room-check.ps1'), 'room-check.ps1');
});

test('мастер: «Выбрать…» подставляет путь в нужное поле', () => {
  const picked = model.setPickedPath({ kind: 'model', workdir: '' }, 'workdir', 'C:/Work/Rooms/cafe');
  assert.equal(picked.workdir, 'C:/Work/Rooms/cafe');
  const script = model.setPickedPath({ kind: 'script', task: '' }, 'task', 'C:/Tools/room-new.ps1');
  assert.equal(script.task, 'room-new.ps1');
  assert.equal(model.folderPickBody('workdir', { title: 'Проверка' }).mode, 'folder');
  assert.equal(model.folderPickBody('task', { kind: 'watch' }).mode, 'file');
});

test('тело создания: вид разворачивается в поля сервера', () => {
  const base = { title: 'Проверка страницы', kind: 'watch', task: 'https://example.com/p',
    schedule: '0 9 * * *', room: 'cafe' };
  const body = model.createBody(base, { preview: true });
  assert.equal(body.kind, 'monitor');
  assert.equal(body.monitor_url, 'https://example.com/p');
  assert.equal(body.prompt, 'https://example.com/p');
  assert.equal(body.preview, true);
  assert.equal(body.confirm, undefined);

  const script = model.createBody({ ...base, kind: 'script', task: 'room-file-diff.ps1' }, {});
  assert.equal(script.kind, 'script');
  assert.equal(script.script, 'room-file-diff.ps1');
  assert.equal(script.no_agent, true);
  assert.equal(script.confirm, true);
});

test('тело создания: выбранная модель уходит в --model', () => {
  const body = model.createBody({ title: 'Обзор', kind: 'model', task: 'проверить',
    model: 'small-model', provider: 'fictional' }, {});
  assert.equal(body.model, 'small-model');
  assert.equal(body.provider, 'fictional');
  const command = model.localCommand({ title: 'Обзор', kind: 'model', task: 'проверить',
    model: 'small-model', provider: 'fictional' });
  assert.match(command, /--model "small-model"/);
  assert.match(command, /--provider "fictional"/);
});

test('карточка: куда придёт результат и на какой модели', () => {
  const card = view.renderDuty(DUTY, { roomId: 'cafe', catalog: CATALOG });
  const text = textOf(card);
  assert.match(text, /Результат: Входящие комнаты/);
  assert.match(text, /модель: Вымышленный · Малая/);
  const link = walk(card).find((n) => String(n.tagName).toLowerCase() === 'a');
  assert.ok(link);
  assert.equal(link.getAttribute('href'), '#/room/cafe/inbox');
});

test('карточка без привязки к комнате честно говорит, что результат не в комнату', () => {
  const card = view.renderDuty({
    ...DUTY, bound_rooms: [], deliver: {
      where: 'local', label: 'Не в комнату', href: '',
      text: 'Дежурство не привязано к комнате: результат остаётся в журнале Hermes.' },
  }, { roomId: '' });
  const text = textOf(card);
  assert.match(text, /Результат: Не в комнату/);
  assert.match(text, /не привязано к комнате/);
  assert.equal(walk(card).filter((n) => String(n.tagName).toLowerCase() === 'a').length, 0);
});

test('панель дня: будущий день показывает плановые запуски списком', () => {
  const grid = view.weekGrid(WEEK, () => {});
  const days = walk(grid).filter((n) => String(n.tagName).toLowerCase() === 'button');
  assert.equal(days.length, 7);
  for (const one of days) {
    assert.ok(one.getAttribute('aria-label'));
  }
  assert.equal(days[3].getAttribute('data-date'), '2026-11-19');
});

test('пауза меняется на «Возобновить» и у выключенного дежурства есть запуск', () => {
  const on = view.fullActionList({ ...DUTY, enabled: true }, 'cafe').map((one) => one.id);
  const off = view.fullActionList({ ...DUTY, enabled: false }, 'cafe').map((one) => one.id);
  assert.ok(on.includes('pause'));
  assert.ok(!on.includes('resume'));
  assert.ok(off.includes('resume'));
  assert.ok(!off.includes('pause'));
  const card = view.renderDuty({ ...DUTY, enabled: false }, { roomId: 'cafe' });
  const labels = walk(card)
    .filter((n) => String(n.tagName).toLowerCase() === 'button')
    .map((n) => textOf(n));
  assert.ok(labels.includes('Возобновить'));
  assert.ok(!labels.includes('Пауза'));
});

test('«Сделать регулярным из дела» без дел: запрос комнаты или своё дежурство', () => {
  const empty = view.deloPicker([], {
    requests: [{ id: 't_1', title: 'Сверить цены поставщика' }],
  });
  const text = textOf(empty);
  assert.match(text, /Дел в этой комнате пока нет/);
  assert.match(text, /Сверить цены поставщика/);
  assert.match(text, /Создать дежурство с нуля/);
  const buttons = walk(empty).filter((n) => String(n.tagName).toLowerCase() === 'button');
  assert.ok(buttons.some((b) => b.getAttribute('data-request') === 't_1'));
  assert.ok(buttons.some((b) => b.getAttribute('data-action') === 'duty-create'));
  // Без запросов тупика тоже нет: остаётся кнопка «с нуля».
  const bare = view.deloPicker([]);
  assert.ok(walk(bare).some((n) => String(n.tagName).toLowerCase() === 'button'
    && n.getAttribute('data-action') === 'duty-create'));
});

