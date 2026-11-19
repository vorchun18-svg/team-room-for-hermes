/**
 * duties-full-fe.test.mjs — W27-E8: интерфейс дежурств комнаты без браузера.
 *
 * Проверяем то, что человек видит и нажимает:
 *  1) мастер: четыре шага, «чего не хватает» словами, без тупиков;
 *  2) предпросмотр точной команды `hermes cron create …` до создания;
 *  3) карточка дежурства: итог последнего запуска одной фразой, все действия;
 *  4) история запусков, сухой прогон, корзина с «Вернуть», шаблоны;
 *  5) карточка «Предлагаю дежурство» с четырьмя ответами;
 *  6) «Сделать регулярным» из дела и из запроса.
 *
 * DOM — tests/fake-dom.mjs (как duties-view.test.mjs).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom } from './fake-dom.mjs';

installFakeDom();

const model = await import('../web/js/views/duty-model.js');
const view = await import('../web/js/views/duties.js');
const wizardMod = await import('../web/js/views/duty-wizard.js');

const NOW = 1_765_000_000;

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

function buttons(node) {
  return walk(node).filter((n) => String(n.tagName).toLowerCase() === 'button');
}

function actions(node) {
  return buttons(node).map((b) => b.getAttribute('data-action')).filter(Boolean);
}

const FILLED = {
  room: 'demo', mode: 'create', title: 'Проверять ящик на новое',
  kind: 'model', task: 'Прочитать новые письма и коротко пересказать важное.',
  workdir: 'C:/Work/Rooms/demo', schedule: '0 9 * * 1-5', catch_up: true,
  change: { rule: 'new_items', threshold: 0 }, onChange: 'inbox', level: 'propose',
  notify: ['today'], source: 'manual',
};

/* ────────────────────────────────────────────────────────── расписание */

test('расписание по-русски: интервалы и cron', () => {
  assert.equal(model.scheduleHuman('30m'), 'каждые 30 минут');
  assert.equal(model.scheduleHuman('every 2h'), 'каждые 2 часа');
  assert.equal(model.scheduleHuman('0 9 * * 1-5'), 'по будням в 09:00');
  assert.equal(model.scheduleHuman('0 10 * * 1'), 'по понедельникам в 10:00');
  assert.equal(model.scheduleHuman('*/15 * * * *'), 'каждые 15 минут');
  assert.equal(model.scheduleHuman('0 9 1 * *'), '1-го числа каждого месяца в 09:00');
  assert.equal(model.scheduleHuman('0 9 * * *'), 'каждый день в 09:00');
  assert.equal(model.scheduleHuman('странное'), 'странное', 'незнакомое — как есть, без вранья');
});

/* ─────────────────────────────────────────────────────────── мастер */

test('мастер: пустой черновик честно говорит, чего не хватает', () => {
  const empty = model.normalizeDraft({ room: 'demo' });
  const problems = model.stepProblems(empty, 'what');
  assert.ok(problems.length >= 2);
  assert.match(problems.join(' '), /название/);
  assert.match(problems.join(' '), /задание/);
  assert.equal(model.draftReady(empty).ok, false);
  assert.equal(model.firstBadStep(empty), 'what');
});

test('мастер: заполненный черновик готов, порог обязателен только для «больше порога»', () => {
  assert.equal(model.draftReady(FILLED).ok, true);
  const counted = model.normalizeDraft({ ...FILLED, change: { rule: 'count_over', threshold: 0 } });
  assert.equal(model.draftReady(counted).ok, false);
  assert.match(model.stepProblems(counted, 'change').join(' '), /порог/);
  assert.equal(model.draftReady({ ...FILLED, change: { rule: 'count_over', threshold: 3 } }).ok, true);
});

test('итоговая строка черновика собирается словами', () => {
  const summary = model.draftSummary(FILLED);
  assert.match(summary, /по будням в 09:00/);
  assert.match(summary, /появилось новое/);
  assert.match(summary, /входящие/i);
  assert.match(summary, /предложить/i);
});

test('предпросмотр: точная команда, создание только с confirm', () => {
  const draft = model.normalizeDraft(FILLED);
  assert.match(model.localCommand(draft), /^hermes cron create "Проверять ящик на новое"/);
  assert.match(model.localCommand(draft), /--schedule "0 9 \* \* 1-5"/);
  const preview = model.createBody(draft, { preview: true });
  assert.equal(preview.preview, true);
  assert.equal(preview.confirm, undefined, 'предпросмотр не подтверждает создание');
  const real = model.createBody(draft, { preview: false });
  assert.equal(real.preview, false);
  assert.equal(real.confirm, true);
  const edit = model.normalizeDraft({ ...FILLED, jobId: 'j_abc' });
  assert.match(model.localCommand(edit), /^hermes cron edit j_abc/);
});

test('предпросмотр берёт команду сервера, а без неё — локальную и говорит об этом', () => {
  const draft = model.normalizeDraft(FILLED);
  const fromServer = model.previewModel(
    { command: 'hermes cron create "Икс" --schedule 30m', changes: ['Расписание: каждые 30 минут'] }, draft);
  assert.equal(fromServer.command, 'hermes cron create "Икс" --schedule 30m');
  assert.deepEqual(fromServer.changes, ['Расписание: каждые 30 минут']);
  assert.match(fromServer.note, /не создано/i);
  const local = model.previewModel(null, draft);
  assert.match(local.command, /^hermes cron create/);
  assert.ok(local.changes.length >= 3, 'что изменится — списком');
});

/* ───────────────────────────────────────────────────── итог и история */

test('итог последнего запуска одной фразой', () => {
  assert.equal(model.outcomeLine({ outcome: { changed: 'нет' } }), 'изменений нет');
  assert.equal(model.outcomeLine({ outcome: { changed: 'да', found: '2 новых письма', action: 'предложено Дело' } }),
    'изменение: 2 новых письма → предложено Дело');
  assert.equal(model.outcomeLine({ last_run: { status: 'failed' } }), 'последний запуск завершился ошибкой');
  assert.equal(model.outcomeLine({}), 'ещё не запускалось');
});

test('история: строка отличает изменение от тихого запуска', () => {
  const changed = model.historyRow({ at: NOW, changed: 'да', found: '2 новых письма',
    created: ['Дело «Ответ»'], proposals: [{ what: 'Ответить', why: 'срок', action: 'создать Дело' }] },
  view.timeText);
  assert.equal(changed.changed, true);
  assert.match(model.historyLine(changed), /изменение/);
  assert.match(model.historyLine(changed), /2 новых письма/);
  assert.equal(changed.proposals[0].what, 'Ответить');
  const calm = model.historyRow({ at: NOW, changed: 'нет' }, view.timeText);
  assert.equal(calm.changed, false);
  assert.match(model.historyLine(calm), /изменений нет/);
});

test('сухой прогон говорит, что бы нашло, и что ничего не записано', () => {
  const block = view.dryRunBlock({ changed: 'да', found: ['2 новых письма'] });
  assert.match(textOf(block), /Нашло бы 1 изменение/);
  assert.match(textOf(block), /Ничего не создано/);
  assert.equal(model.dryRunText({ changed: 'нет' }), 'Изменений бы не нашло');
});

/* ──────────────────────────────────────────────────────── корзина */

test('корзина: сколько дней осталось и что вернуть можно', () => {
  const rows = model.trashModel([{ job_id: 'j_one', title: 'Резервная копия', trashed_at: NOW - 3 * 86400 }], NOW);
  assert.equal(rows[0].daysLeft, 27);
  assert.match(rows[0].daysText, /27/);
  const panel = view.trashPanel([{ job_id: 'j_one', title: 'Резервная копия', trashed_at: NOW - 3 * 86400 }], { now: NOW });
  assert.match(textOf(panel), /Резервная копия/);
  assert.match(textOf(panel), /вернуть можно ещё 27 дней/);
  assert.ok(actions(panel).includes('restore'));
  assert.match(textOf(view.trashPanel([], {})), /Корзина пуста/);
});

/* ───────────────────────────────────────────────────── предложения */

test('«Предлагаю дежурство»: четыре ответа и объяснение зачем', () => {
  const card = view.proposalCard({
    id: 'dp_1', title: 'Проверять сроки по делам', why: 'Трижды переносили срок',
    when_human: 'по будням в 09:00', check: 'Дела со сроком ближе двух дней',
    on_change_label: 'во «Входящие» комнаты', source: 'manager',
  }, { onAction: () => {} });
  assert.match(textOf(card), /Предлагаю дежурство/);
  assert.match(textOf(card), /Трижды переносили срок/);
  assert.deepEqual(actions(card),
    ['proposal-accept', 'proposal-edit', 'proposal-dismiss', 'proposal-never']);
  const labels = buttons(card).map((b) => textOf(b));
  assert.deepEqual(labels, ['Создать', 'Изменить', 'Не надо', 'Не предлагать такое']);
  for (const btn of buttons(card)) assert.ok((btn.getAttribute('title') || '').length > 8);
});

test('предложение превращается в черновик мастера', () => {
  const draft = model.draftFromProposal({ id: 'dp_1', title: 'Проверять сроки', when: '0 9 * * 1-5',
    check: 'Дела со сроком ближе двух дней', on_change: 'delo', source: 'manager' }, 'demo');
  assert.equal(draft.title, 'Проверять сроки');
  assert.equal(draft.schedule, '0 9 * * 1-5');
  assert.equal(draft.onChange, 'delo');
  assert.equal(draft.from.kind, 'proposal');
  assert.equal(draft.room, 'demo');
});

/* ──────────────────────────────────────────────────────── шаблоны */

test('шаблоны: карточки с «Взять шаблон» и понятными полями', () => {
  const tpl = model.templatesFrom({ items: [
    { id: 'tpl_mail', title: 'Проверять ящик на новое', for: 'Почта', kind: 'watch',
      schedule: '30m', check: 'Новые письма', change: 'появилось новое',
      action_human: 'во «Входящие»', level: 'propose' },
    { id: 'tpl_folder', title: 'Следить за папкой', schedule: 'every 2h' },
  ] });
  assert.equal(tpl.length, 2);
  assert.equal(tpl[0].when, 'каждые 30 минут');
  const grid = view.templateCards(tpl, { onPick: () => {} });
  assert.equal(buttons(grid).filter((b) => b.getAttribute('data-action') === 'use-template').length, 2);
  assert.match(textOf(grid), /Проверять ящик на новое/);
  const draft = model.draftFromTemplate(tpl[0].raw, 'demo');
  assert.equal(draft.kind, 'watch');
  assert.equal(draft.schedule, '30m');
  assert.match(textOf(view.templateCards([], {})), /Шаблоны не загрузились/);
});

/* ────────────────────────────────────── карточка и все действия */

const DUTY = {
  job_id: 'j_mail', name: 'Проверять ящик', enabled: true,
  schedule: '30m', schedule_human: 'каждые 30 минут', bound_rooms: ['demo'],
  last_run: { status: 'completed', finished_at: NOW - 3600 },
  next_run_at: NOW + 1800, missed_recent: 0, recent: [],
  kind: 'watch', on_change: 'inbox', level: 'propose', source: 'manager',
  outcome: { changed: 'да', found: '2 новых письма', action: 'предложено Дело' },
};

test('у карточки есть все действия, у каждого — подсказка', () => {
  const list = view.fullActionList(DUTY, 'demo').map((a) => a.id);
  assert.deepEqual(list, ['run', 'pause', 'edit', 'dryrun', 'history', 'duplicate', 'unbind', 'delete']);
  const card = view.renderDuty(DUTY, {
    roomId: 'demo', now: NOW, actions: view.fullActionList(DUTY, 'demo'), onAction: () => {},
  });
  const ids = actions(card);
  assert.deepEqual(ids, list);
  for (const btn of buttons(card)) {
    assert.ok((btn.getAttribute('title') || '').length > 5, 'у кнопки есть объяснение: ' + btn.getAttribute('data-action'));
  }
  assert.match(textOf(card), /изменение: 2 новых письма → предложено Дело/);
  assert.match(textOf(card), /предложил руководитель/);
  assert.match(textOf(card), /каждые 30 минут/);
});

test('история и сухой прогон живут внутри карточки', () => {
  const card = view.renderDuty(DUTY, {
    roomId: 'demo', now: NOW, actions: view.fullActionList(DUTY, 'demo'),
    history: [{ at: NOW - 7200, changed: 'да', found: 'новое письмо' }],
    dryRun: { changed: 'да', found: ['2 новых письма'] },
  });
  assert.match(textOf(card), /История запусков/);
  assert.match(textOf(card), /новое письмо/);
  assert.match(textOf(card), /Проверка без записи/);
  assert.equal(walk(card).filter((n) => String(n.className).includes('duty__history-row--changed')).length, 1);
});

test('карточка умеет показать, что история не загрузилась', () => {
  const panel = view.historyPanel([], { error: 'сервер не ответил' });
  assert.match(textOf(panel), /История не загрузилась: сервер не ответил/);
  assert.match(textOf(view.historyPanel([], {})), /Запусков пока не было/);
});

/* ────────────────────────────────── «Сделать регулярным» из дела */

test('«Сделать регулярным»: из дела и из запроса получается готовый черновик', () => {
  const fromDelo = model.draftFromDelo({ id: 'dl_one', title: 'Сверить витрину', room: 'demo' }, 'demo');
  assert.match(fromDelo.title, /Сверить витрину/);
  assert.equal(fromDelo.source, 'delo');
  assert.equal(fromDelo.from.kind, 'delo');
  assert.equal(fromDelo.change.rule, 'model_compares');
  const fromRequest = model.draftFromRequest({ id: 't_1', title: 'Собрать отчёт' }, 'demo');
  assert.equal(fromRequest.from.kind, 'request');
  assert.match(fromRequest.title, /Собрать отчёт/);
  const picker = view.deloPicker([{ id: 'dl_one', title: 'Сверить витрину', status_label: 'в работе' }],
    { onPick: () => {}, onCancel: () => {} });
  assert.match(textOf(picker), /Сделать регулярным из дела/);
  assert.equal(buttons(picker).filter((b) => b.getAttribute('data-delo')).length, 1);
});

/* ────────────────────────────────────────────────────────── мастер */

test('мастер: четыре шага, «Далее» не пускает с пустыми полями', () => {
  const wizard = wizardMod.createWizard({
    draft: { room: 'demo', mode: 'create' },
    onCancel: () => {}, onPreview: async () => ({ ok: true }), onSubmit: async () => {},
  });
  const steps = walk(wizard.el).filter((n) => n.getAttribute && n.getAttribute('data-action') === 'wizard-goto');
  assert.equal(steps.length, 4);
  assert.deepEqual(steps.map((n) => n.getAttribute('data-step')), ['what', 'when', 'change', 'do']);
  assert.match(textOf(wizard.el), /Чтобы идти дальше/);
  wizard.goto(2);
  assert.equal(wizard.state.step, 0, 'без названия и задания дальше не пускает');
});

test('мастер: заполненный черновик доходит до предпросмотра и создания', async () => {
  let submitted = null;
  const wizard = wizardMod.createWizard({
    draft: FILLED,
    onCancel: () => {},
    onPreview: async (draft) => ({ ok: true, command: 'hermes cron create "' + draft.title + '" --schedule 0 9 * * 1-5',
      changes: ['Расписание: по будням в 09:00'] }),
    onSubmit: async (draft) => { submitted = draft; },
  });
  wizard.goto(3);
  assert.equal(wizard.state.step, 3);
  const previewBtn = walk(wizard.el).find((n) => n.getAttribute && n.getAttribute('data-action') === 'wizard-preview');
  assert.ok(previewBtn, 'на последнем шаге есть кнопка предпросмотра');
  await wizard.preview();
  const shown = walk(wizard.el).find((n) => n.getAttribute && n.getAttribute('data-role') === 'duty-command');
  assert.ok(shown, 'команда показана человеку');
  assert.match(textOf(shown), /^hermes cron create/);
  assert.match(textOf(wizard.el), /Что изменится/);
  await wizard.submit();
  assert.ok(submitted, 'создание вызывается только после предпросмотра');
  assert.equal(submitted.title, FILLED.title);
});

test('мастер правки: кнопка называется «Сохранить» и команда — cron edit', async () => {
  const wizard = wizardMod.createWizard({
    draft: { ...FILLED, jobId: 'j_mail' },
    onPreview: async () => null, onSubmit: async () => {},
  });
  wizard.goto(3);
  await wizard.preview();
  const submit = walk(wizard.el).find((n) => n.getAttribute && n.getAttribute('data-action') === 'wizard-submit');
  assert.equal(textOf(submit), 'Сохранить');
  assert.match(textOf(wizard.el), /hermes cron edit j_mail/);
});

test('черновик из существующего дежурства и копия', () => {
  const draft = model.draftFromDuty(DUTY, 'demo');
  assert.equal(draft.mode, 'edit');
  assert.equal(draft.jobId, 'j_mail');
  assert.equal(draft.title, 'Проверять ящик');
  assert.equal(draft.onChange, 'inbox');
  const copy = model.duplicateDraft(draft, 'demo');
  assert.equal(copy.mode, 'create');
  assert.equal(copy.jobId, '');
  assert.match(copy.title, /\(копия\)$/);
  assert.equal(copy.started_paused, true, 'копия включается вручную');
});
