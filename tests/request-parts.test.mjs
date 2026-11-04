import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  buildTree, pipeline, nodeActions, warningItems, criticInfo,
  artifactKindMeta, formatBytes, timelineMeta, timelineRows, criticEnabled, STAGES,
  errorReason, failureInfo, failureAttempts, isFailedRun,
} from '../web/js/views/request-parts-model.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const fixture = (name) => JSON.parse(readFileSync(join(HERE, '..', 'fixtures', name), 'utf8'));

/* ───────────────────────────────────────────────────────────────────── дерево */

test('buildTree: вложенность, порядок и глубина по parent_ids', () => {
  const tree = fixture('request_done.json').tree;
  const { rows, roots, cycleIds } = buildTree(tree);

  assert.equal(rows.length, tree.length);
  assert.deepEqual(roots, [tree[0].id], 'корень один и стоит первым');
  assert.equal(rows[0].id, tree[0].id);
  assert.equal(rows[0].depth, 0);
  assert.equal(rows[0].childCount, 5, 'у корня 4 среза и критик');

  const revision = rows.find((r) => r.id === 't_1000a006');
  assert.equal(revision.depth, 2, 'доработка висит под карточкой критика');
  assert.equal(revision.node.role, 'revision');
  assert.deepEqual(cycleIds, []);
});

test('buildTree: корень показывается первым, даже если начался позже', () => {
  const rows = buildTree([
    { id: 't_worker', parent_ids: ['t_root'], role: 'worker', started_at: 100 },
    { id: 't_root', parent_ids: [], role: 'root', started_at: 900 },
  ]).rows;
  assert.deepEqual(rows.map((r) => r.id), ['t_root', 't_worker']);
});

test('buildTree: цикл a↔b не вешает обход, обе карточки видны', () => {
  const { rows, cycleIds } = buildTree([
    { id: 't_a', parent_ids: ['t_b'], title: 'A' },
    { id: 't_b', parent_ids: ['t_a'], title: 'B' },
  ]);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r) => r.id).sort(), ['t_a', 't_b']);
  assert.deepEqual(rows.map((r) => r.depth), [0, 0], 'цикл разорван на верхем уровне');
  assert.deepEqual(cycleIds.sort(), ['t_a', 't_b']);
});

test('buildTree: цикл из трёх карточек и повторный id не дублируют строки', () => {
  const { rows, cycleIds } = buildTree([
    { id: 't_1', parent_ids: ['t_3'] },
    { id: 't_2', parent_ids: ['t_1'] },
    { id: 't_3', parent_ids: ['t_2'] },
    { id: 't_1', parent_ids: ['t_3'], title: 'дубль id' },
  ]);
  assert.equal(rows.length, 3);
  assert.equal(new Set(rows.map((r) => r.id)).size, 3);
  assert.equal(cycleIds.length, 3);
});

test('buildTree: сам себе родитель игнорируется, висячий родитель — верхний уровень', () => {
  const { rows, cycleIds } = buildTree([
    { id: 't_self', parent_ids: ['t_self'] },
    { id: 't_orphan', parent_ids: ['t_missing'] },
    { id: 't_child', parent_ids: ['t_orphan'] },
  ]);
  const depths = Object.fromEntries(rows.map((r) => [r.id, r.depth]));
  assert.equal(depths.t_self, 0);
  assert.equal(depths.t_orphan, 0);
  assert.equal(depths.t_child, 1);
  assert.deepEqual(cycleIds, [], 'сам себе родитель — не цикл, связь просто отброшена');
});

test('buildTree: пустой и битый вход не падает', () => {
  assert.deepEqual(buildTree([]).rows, []);
  assert.deepEqual(buildTree(null).rows, []);
  assert.deepEqual(buildTree([null, {}, { id: '' }]).rows, []);
});

/* ────────────────────────────────────────────────────────────────── этапы */

test('pipeline: queued без детей — активен «Разбор», этапов пять', () => {
  const p = pipeline(fixture('request_queued.json'));
  assert.equal(p.stages.length, 5);
  assert.equal(p.total, 5);
  assert.equal(p.done, 0);
  assert.equal(p.currentId, 'parse');
  assert.equal(p.stages[0].state, 'active');
  assert.match(p.nowText, /разбирает запрос/);
  assert.equal(p.withCritic, true);
});

test('pipeline: working — первый этап закрыт, текущий «Работа»', () => {
  const p = pipeline(fixture('request_working.json'));
  assert.equal(p.done, 1);
  assert.equal(p.currentId, 'work');
  assert.equal(p.stages[1].state, 'active');
  assert.match(p.nowText, /готово 2 из 4/);
});

test('pipeline: reviewing — критик проверяет, раунд в подписи', () => {
  const d = fixture('request_reviewing.json');
  const p = pipeline(d);
  assert.equal(p.currentId, 'critic');
  assert.equal(p.stages.find((s) => s.id === 'critic').state, 'active');
  assert.match(p.nowText, /критик проверяет свод, раунд 1/);
  assert.equal(p.done, 2);
});

test('pipeline: synthesizing — доработка свода, критик уже закрыт', () => {
  const d = fixture('request_done.json');
  d.status = 'synthesizing';
  const p = pipeline(d);
  assert.equal(p.currentId, 'summary');
  assert.equal(p.stages.find((s) => s.id === 'critic').state, 'done');
});

test('pipeline: done — все этапы закрыты, прогресс 100 %', () => {
  const p = pipeline(fixture('request_done.json'));
  assert.equal(p.done, p.total);
  assert.equal(p.percent, 100);
  assert.equal(p.currentId, 'done');
  assert.equal(p.tone, 'ok');
  assert.match(p.nowText, /ответ готов/);
});

test('pipeline: needs_you приостанавливает работу и подсвечивает ожидание', () => {
  const p = pipeline(fixture('request_needs_you.json'));
  assert.equal(p.tone, 'warn');
  assert.equal(p.currentId, 'work');
  assert.equal(p.stages.find((s) => s.id === 'work').state, 'paused');
  assert.match(p.nowText, /ждёт вашего ответа/);
});

test('pipeline: failed останавливает линейку на упавшем этапе', () => {
  const p = pipeline(fixture('request_failed.json'));
  assert.equal(p.tone, 'danger');
  assert.equal(p.currentId, 'work');
  assert.equal(p.stages.find((s) => s.id === 'work').state, 'failed');
  assert.equal(p.stages.find((s) => s.id === 'summary').state, 'todo');
});

test('pipeline: без критика этап пропускается и не считается в M', () => {
  const d = fixture('request_outside_room.json');
  assert.equal(criticEnabled(d), false);
  const p = pipeline(d);
  assert.equal(p.withCritic, false);
  assert.equal(p.total, 4);
  assert.equal(p.stages.some((s) => s.id === 'critic'), false);
  assert.equal(p.stages.length, 4);
});

test('pipeline: legacy-запрос без синтеза не считается готовым', () => {
  const d = fixture('request_done_legacy.json');
  d.status = 'working';
  d.critic = { enabled: false, round: 0, max_rounds: 1, state: 'off', verdict: null, issues: [], task_id: null };
  const p = pipeline(d);
  assert.equal(p.stages.length, 4);
  assert.equal(p.currentId, 'work');
  assert.equal(p.tone, 'info');
});

test('STAGES: фиксированный порядок и русские подписи', () => {
  assert.deepEqual(STAGES.map((s) => s.title), ['Разбор', 'Работа', 'Критик', 'Сводка', 'Готово']);
});

/* ──────────────────────────────────────────────────────── действия карточки */

test('nodeActions: завершённая карточка без действий', () => {
  assert.deepEqual(nodeActions({ id: 't_1', status: 'done', role: 'worker' }), []);
  assert.deepEqual(nodeActions({ id: 't_1', status: 'archived', role: 'worker' }), []);
});

test('nodeActions: blocked — ответ, переназначение, доработка', () => {
  const acts = nodeActions({ status: 'blocked', needs_human: true, role: 'worker' });
  assert.deepEqual(acts, ['answer', 'reassign', 'rework']);
});

test('nodeActions: упавшая карточка получает «Повторить»', () => {
  const acts = nodeActions({ status: 'blocked', error: { code: 'x', message: 'упал' }, role: 'worker' });
  assert.deepEqual(acts, ['answer', 'retry', 'reassign', 'rework']);
  assert.deepEqual(nodeActions({ status: 'failed', role: 'worker' }), ['retry', 'reassign', 'rework']);
});

test('nodeActions: running — только переназначение, без доработки и повтора', () => {
  assert.deepEqual(nodeActions({ status: 'running', role: 'worker' }), ['reassign']);
});

test('nodeActions: корень не переназначается, но заблокированный корень можно вернуть в работу', () => {
  assert.deepEqual(nodeActions({ status: 'running', role: 'root' }), []);
  assert.deepEqual(nodeActions({ status: 'blocked', role: 'root', needs_human: true }), ['answer', 'rework']);
  assert.deepEqual(nodeActions({ status: 'done', role: 'root' }), []);
});

test('nodeActions: review — доработка и переназначение', () => {
  assert.deepEqual(nodeActions({ status: 'review', role: 'worker' }), ['reassign', 'rework']);
});

/* ──────────────────────────────────────────────────────── предупреждения */

test('warningItems: коды превращаются в понятные баннеры', () => {
  const items = warningItems(fixture('request_failed.json'));
  assert.equal(items.length, 1);
  assert.equal(items[0].code, 'card_failed');
  assert.equal(items[0].tone, 'danger');
  assert.equal(items[0].bannerClass, 'banner--error');
  assert.equal(items[0].taskId, 't_3b7e9d13');
  assert.match(items[0].hint, /Повторить/);
});

test('warningItems: outside_room несёт подсказку про дерево работ', () => {
  const items = warningItems(fixture('request_outside_room.json'));
  const w = items.find((i) => i.code === 'outside_room');
  assert.ok(w);
  assert.equal(w.tone, 'warn');
  assert.match(w.text, /analyst/);
  assert.match(w.hint, /дереве работ/);
  assert.doesNotMatch(w.text, /Переназначьте/, 'приказ сервера уходит в подсказку, а не дублируется');
});

test('warningItems: dispatcher.warning показывается отдельным баннером', () => {
  const items = warningItems({
    warnings: [],
    dispatcher: { ok: false, warning: 'Диспетчер остановлен 12 минут назад' },
  });
  assert.equal(items.length, 1);
  assert.equal(items[0].code, 'dispatcher_down');
  assert.match(items[0].text, /остановлен/);
});

test('warningItems: дубли outside_room из warnings и дерева склеиваются', () => {
  const items = warningItems({
    warnings: [{ code: 'outside_room', message: 'вне комнаты', detail: { task_id: 't_x' } }],
    tree: [{ id: 't_x', title: 'X', assignee: 'analyst', outside_room: true }],
  });
  assert.equal(items.length, 1);
  assert.equal(items[0].taskId, 't_x');
});

test('warningItems: флаг outside_room без warning синтезирует баннер', () => {
  const items = warningItems({
    tree: [{ id: 't_y', title: 'Сверка', assignee: 'analyst', outside_room: true }],
  });
  assert.equal(items.length, 1);
  assert.match(items[0].text, /Сверка/);
  assert.equal(items[0].taskId, 't_y');
});

test('warningItems: чистое состояние — пустой список', () => {
  assert.deepEqual(warningItems(fixture('request_done.json')), []);
  assert.deepEqual(warningItems({}), []);
});

test('warningItems: неизвестный код не теряется', () => {
  const items = warningItems({ warnings: [{ code: 'new_code', message: 'Что-то новое' }] });
  assert.equal(items.length, 1);
  assert.equal(items[0].title, 'Замечание');
  assert.match(items[0].text, /Что-то новое/);
});

test('warningItems: подсказка не дублирует приказ сервера', () => {
  const items = warningItems({
    warnings: [{
      code: 'outside_room',
      message: 'Карточку выполнил analyst. Переназначьте карточку или добавьте профиль в команду.',
      detail: { task_id: 't_z' },
    }],
  });
  assert.equal(items.length, 1);
  assert.equal(items[0].text, 'Карточку выполнил analyst.', 'факт остаётся');
  assert.match(items[0].hint, /дереве работ/, 'действие — в подсказке');
});

/* ────────────────────────────────────────────────────────────── критик */

test('criticInfo: состояния и вердикты', () => {
  const reviewing = criticInfo(fixture('request_reviewing.json'));
  assert.equal(reviewing.state, 'reviewing');
  assert.equal(reviewing.tone, 'info');
  assert.equal(reviewing.active, true);
  assert.equal(reviewing.round, 1);
  assert.equal(reviewing.maxRounds, 1);

  const approved = criticInfo(fixture('request_critic_approved.json'));
  assert.equal(approved.state, 'approved');
  assert.equal(approved.verdict, 'approve');
  assert.equal(approved.verdictLabel, 'принято');
  assert.equal(approved.issues.length, 0);

  const revised = criticInfo(fixture('request_done.json'));
  assert.equal(revised.state, 'revised');
  assert.equal(revised.verdict, 'revise');
  assert.equal(revised.issues.length, 3);
  assert.match(revised.issues[0].where, /Раздел/);

  const off = criticInfo(fixture('request_outside_room.json'));
  assert.equal(off.enabled, false);
  assert.equal(off.state, 'off');
  assert.equal(off.tone, 'muted');

  const failed = criticInfo({ critic: { enabled: true, state: 'failed', issues: [] } });
  assert.equal(failed.tone, 'danger');
  assert.match(failed.note, /ошибкой/);

  assert.equal(criticInfo({}).state, 'waiting', 'нет поля — ждём, но не падаем');
});

test('criticInfo: подпись ожидания учитывает состояние запроса', () => {
  const ready = criticInfo({ status: 'done', synthesis: { version: 1 }, critic: { enabled: true, state: 'waiting' } });
  assert.match(ready.note, /вручную/);

  const failed = criticInfo({ status: 'failed', synthesis: { version: 0 }, critic: { enabled: true, state: 'waiting' } });
  assert.match(failed.note, /остановлен/);

  const needsYou = criticInfo({ status: 'needs_you', synthesis: { version: 0 }, critic: { enabled: true, state: 'waiting' } });
  assert.match(needsYou.note, /после вашего ответа/);
});

/* ─────────────────────────────────────────────────────── понятные поломки */

test('errorReason: технический лог → причина по-русски', () => {
  assert.equal(errorReason('timeout after 1800s'), 'превышено время ожидания (30 мин)');
  assert.equal(errorReason('timeout after 45s'), 'превышено время ожидания (45 с)');
  assert.equal(errorReason('worker exited with code 1: OSError…'), 'исполнитель завершился с ошибкой (код 1)');
  assert.equal(errorReason("Процесс воркера завершился с кодом 1: KeyError('custo_embalagem')"),
    'в данных не нашлось поле «custo_embalagem»');
  assert.equal(errorReason('OSError: [Errno 28] No space left on device'), 'на диске закончилось место');
  assert.equal(errorReason('crashed'), 'исполнитель аварийно завершился');
  assert.equal(errorReason(''), '');
  assert.equal(errorReason(null), '');
});

test('failureInfo: две попытки, причина и сырой лог отдельно', () => {
  const d = fixture('request_failed.json');
  const f = failureInfo(d);
  assert.equal(f.attempts, 2, 'две неудачные попытки в дереве');
  assert.match(f.text, /^Исполнитель не справился \(2 попытки\)\./);
  assert.match(f.text, /Причина: в данных не нашлось поле «custo_embalagem»\./);
  assert.ok(!/timeout after|KeyError|exit 1|воркер/i.test(f.text), 'технический лог в текст не попадает');
  const raw = f.details.map((x) => x.text).join('\n');
  assert.match(raw, /Воркер расчёта упал дважды \(exit 1\)/, 'оригинал сохранён');
  assert.match(raw, /KeyError\('custo_embalagem'\)/);
  assert.equal(f.details.find((x) => x.code).code, 'worker_crashed');
});

test('failureInfo: без данных — честное «работа остановилась»', () => {
  const f = failureInfo({ status: 'failed' });
  assert.equal(f.attempts, 0);
  assert.equal(f.text, 'Исполнитель не справился: работа остановилась.');
  assert.deepEqual(f.details, []);
});

test('failureAttempts и isFailedRun: считаем только неудачи', () => {
  assert.equal(isFailedRun({ status: 'failed' }), true);
  assert.equal(isFailedRun({ outcome: 'failed' }), true);
  assert.equal(isFailedRun({ status: 'done', outcome: 'completed' }), false);
  assert.equal(isFailedRun(null), false);
  const d = {
    status_reason: 'Воркер упал дважды (exit 1)',
    tree: [
      { id: 't_1', runs: [{ status: 'failed' }, { status: 'done' }] },
      { id: 't_2', runs: [{ status: 'failed' }, { status: 'crashed' }, { status: 'failed' }] },
    ],
  };
  assert.equal(failureAttempts(d), 3);
  assert.equal(failureAttempts({ status_reason: 'упало 2 раза' }), 2);
});

/* ──────────────────────────────────────────────────────────── утилиты */

test('formatBytes: русские единицы и терпимость к мусору', () => {
  assert.equal(formatBytes(512), '512 Б');
  assert.equal(formatBytes(2048), '2,0 КБ');
  assert.equal(formatBytes(18432), '18 КБ');
  assert.equal(formatBytes(5 * 1024 * 1024), '5,0 МБ');
  assert.equal(formatBytes(null), '—');
  assert.equal(formatBytes('abc'), '—');
});

test('artifactKindMeta и timelineMeta: известные и неизвестные типы', () => {
  assert.equal(artifactKindMeta('md').icon, 'markdown');
  assert.equal(artifactKindMeta('image').label, 'Картинка');
  assert.equal(artifactKindMeta('exe').icon, 'file');
  assert.equal(timelineMeta('completed').icon, 'check');
  assert.equal(timelineMeta('что-то').label, 'событие');
});

test('timelineRows: свёрнуто до последних шести, развёрнуто — все', () => {
  const d = fixture('request_done.json');
  const collapsed = timelineRows(d);
  assert.equal(collapsed.length, 6);
  const all = timelineRows(d, { expanded: true });
  assert.equal(all.length, d.timeline.length);
  const ats = all.map((r) => r.event.at);
  assert.deepEqual(ats, ats.slice().sort((a, b) => a - b), 'по возрастанию времени');
  assert.equal(collapsed[collapsed.length - 1].newest, true);
});

test('timelineRows: короткая хронология показывается целиком', () => {
  const rows = timelineRows({ timeline: [{ at: 2, kind: 'created' }, { at: 1, kind: 'created' }] });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].event.at, 1);
  assert.equal(rows[1].newest, true);
});
