// node --test tests/team-roadmap.test.mjs
// Чистая логика вкладок «Команда» и «Карта проекта» (web/js/views/team-roadmap-model.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  plural,
  roleLabel,
  withRoomRoles,
  memberState,
  stageMeta,
  clockLabel,
  sinceLabel,
  lastWorkLabel,
  updatedLabel,
  isRoleEditable,
  splitTeam,
  sortMembers,
  memberModel,
  countTeam,
  availableProfiles,
  pickCurrent,
  progressInfo,
  phaseSummary,
  phaseSource,
  relatedDecisions,
  relatedSteps,
  decisionGroups,
  mirrorPathOf,
  canConsolidate,
  isEmptyRoadmap,
} from '../web/js/views/team-roadmap-model.js';

const FIX = new URL('../fixtures/', import.meta.url);
const fixture = (name) => JSON.parse(readFileSync(new URL(name, FIX), 'utf8'));

// Фиксированное «сейчас» — 28.10.2026, 14:00 по локальному поясу.
const NOW = new Date(2026, 9, 28, 14, 0, 0).getTime();
const sec = (ms) => Math.floor(ms / 1000);
const ago = (ms) => sec(NOW - ms);
const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

test('plural склоняет русские существительные', () => {
  assert.equal(plural(1, ['этап', 'этапа', 'этапов']), 'этап');
  assert.equal(plural(2, ['этап', 'этапа', 'этапов']), 'этапа');
  assert.equal(plural(5, ['этап', 'этапа', 'этапов']), 'этапов');
  assert.equal(plural(11, ['этап', 'этапа', 'этапов']), 'этапов');
  assert.equal(plural(21, ['этап', 'этапа', 'этапов']), 'этап');
  assert.equal(plural(0, ['день', 'дня', 'дней']), 'дней');
  assert.equal(plural(3, ['день']), 'день', 'короткий список форм не падает');
});

test('роли и статусы переводятся по-русски', () => {
  assert.equal(roleLabel('orchestrator'), 'руководитель');
  assert.equal(roleLabel('worker'), 'исполнитель');
  assert.equal(roleLabel('critic'), 'критик');
  assert.equal(roleLabel('observer'), 'наблюдатель');
  assert.equal(roleLabel('unknown'), 'unknown');

  assert.deepEqual(memberState('working'), { state: 'working', label: 'работает', kind: 'working', pulse: true });
  assert.deepEqual(memberState('waiting'), { state: 'waiting', label: 'ждёт человека', kind: 'waiting', pulse: false });
  assert.equal(memberState('idle').label, 'свободен');
  assert.equal(memberState(undefined).state, 'idle', 'пустой статус = свободен');
  assert.equal(memberState('weird').state, 'idle');

  assert.deepEqual(stageMeta('done'), { status: 'done', label: 'готово', tone: 'ok' });
  assert.equal(stageMeta('active').tone, 'info');
  assert.equal(stageMeta('blocked').tone, 'warn');
  assert.equal(stageMeta('next').label, 'дальше');
  assert.equal(stageMeta(null).status, 'next', 'неизвестный статус этапа = дальше');
});

test('clockLabel и sinceLabel считают «уже N мин»', () => {
  assert.equal(clockLabel(new Date(2026, 9, 28, 9, 5, 0).getTime()), '09:05');
  assert.equal(clockLabel(null), '');
  assert.equal(clockLabel(0), '');

  assert.equal(sinceLabel(ago(30 * 1000), NOW), 'только что');
  assert.equal(sinceLabel(ago(4 * MIN), NOW), 'уже 4 мин');
  assert.equal(sinceLabel(ago(38 * MIN), NOW), 'уже 38 мин');
  assert.equal(sinceLabel(ago(HOUR), NOW), 'уже 1 ч');
  assert.equal(sinceLabel(ago(HOUR + 10 * MIN), NOW), 'уже 1 ч 10 мин');
  assert.equal(sinceLabel(ago(2 * DAY + HOUR), NOW), 'уже 2 дня');
  assert.equal(sinceLabel(ago(5 * DAY), NOW), 'уже 5 дней');
  assert.equal(sinceLabel(null, NOW), '', 'нет времени — нет подписи');
  assert.equal(sinceLabel(ago(-5 * MIN), NOW), 'только что', 'будущее не даёт минуса');
  assert.equal(sinceLabel(sec(NOW - 4 * MIN), NOW), 'уже 4 мин', 'секунды API тоже понимаются');
});

test('lastWorkLabel: сегодня / вчера / дата', () => {
  assert.equal(lastWorkLabel(new Date(2026, 9, 28, 9, 31, 0).getTime(), NOW), 'сегодня, 09:31');
  assert.equal(lastWorkLabel(new Date(2026, 9, 27, 22, 5, 0).getTime(), NOW), 'вчера, 22:05');
  assert.equal(lastWorkLabel(new Date(2026, 8, 14, 8, 0, 0).getTime(), NOW), '14 сен, 08:00');
  assert.equal(lastWorkLabel(new Date(2025, 11, 31, 23, 0, 0).getTime(), NOW), '31 дек 2025, 23:00');
  assert.equal(lastWorkLabel(null, NOW), '');
  assert.equal(updatedLabel(new Date(2026, 9, 27, 22, 5, 0).getTime(), NOW), 'обновлено вчера, 22:05');
  assert.equal(updatedLabel(null, NOW), '');
});

test('isRoleEditable: директора и критика не трогаем', () => {
  assert.equal(isRoleEditable('worker'), true);
  assert.equal(isRoleEditable('observer'), true);
  assert.equal(isRoleEditable('orchestrator'), false);
  assert.equal(isRoleEditable('critic'), false);
});

test('splitTeam делит состав и «вне состава»', () => {
  const team = fixture('team_cafe.json');
  const split = splitTeam(team);
  assert.equal(split.inside.length, 8);
  assert.equal(split.outside.length, 0, 'в фикстуре cafe все в комнате');

  const withFlags = {
    members: [
      { profile: 'a', role: 'worker' },
      { profile: 'b', role: 'worker', outside_room: true },
      { profile: 'c', role: 'worker', in_room: false },
      { profile: 'a', role: 'worker' },
    ],
  };
  const flagged = splitTeam(withFlags);
  assert.deepEqual(flagged.inside.map((m) => m.profile), ['a'], 'дубликаты схлопываются');
  assert.deepEqual(flagged.outside.map((m) => m.profile).sort(), ['b', 'c']);

  const topLevel = splitTeam({
    members: [{ profile: 'a' }],
    outside_members: [{ profile: 'x', role: 'worker', current: { title: 'чужак' } }, { profile: 'a' }],
  });
  assert.deepEqual(topLevel.inside.map((m) => m.profile), ['a']);
  assert.deepEqual(topLevel.outside.map((m) => m.profile), ['x'], 'top-level список тоже читается');

  assert.deepEqual(splitTeam(null), { inside: [], outside: [] });
});

test('sortMembers: директор, исполнители, критик', () => {
  const members = [
    { profile: 'critic', role: 'critic' },
    { profile: 'w2', role: 'worker' },
    { profile: 'obs', role: 'observer' },
    { profile: 'dir', role: 'orchestrator' },
    { profile: 'w1', role: 'worker' },
  ];
  assert.deepEqual(sortMembers(members).map((m) => m.profile), ['dir', 'w2', 'w1', 'obs', 'critic']);
  assert.deepEqual(sortMembers([]), []);
  assert.deepEqual(sortMembers(null), []);
});

test('memberModel соединяет участника с профилем', () => {
  const team = fixture('team_cafe.json');
  const profiles = fixture('profiles.json');
  const director = team.members.find((m) => m.profile === 'biz-director');
  const model = memberModel(director, profiles, NOW);

  assert.equal(model.name, 'Руководитель проекта');
  assert.equal(model.role, 'orchestrator');
  assert.equal(model.roleLabel, 'руководитель');
  assert.equal(model.roleEditable, false);
  assert.equal(model.removable, false);
  assert.equal(model.isDirector, true);
  assert.match(model.description, /раздаёт работу/);
  assert.equal(model.stateLabel, 'работает');
  assert.equal(model.pulse, true);
  assert.match(model.current.since, /^уже /);
  assert.equal(model.current.taskId, 't_9f3c2a10');
  assert.equal(model.last.at, lastWorkLabel(director.last.finished_at, NOW));
  assert.deepEqual(model.counts, { done: 3, blocked: 0, active: 1 });
  assert.equal(model.model, '', 'модели в фикстурах нет — поле пустое, а не выдуманное');

  const lawyer = memberModel(team.members.find((m) => m.profile === 'biz-lawyer'), profiles, NOW);
  assert.equal(lawyer.roleEditable, true);
  assert.equal(lawyer.pinned, true);
  assert.equal(lawyer.stateLabel, 'свободен');
  assert.equal(lawyer.current, null);

  const withModel = memberModel({ profile: 'x', role: 'worker', model: 'claude-sonnet-4' }, null, NOW);
  assert.equal(withModel.model, 'claude-sonnet-4');
  assert.equal(withModel.description, '', 'нет профиля — нет описания');
  assert.equal(withModel.name, 'x');

  assert.equal(memberModel(null, null, NOW).profile, undefined, 'пустой вход не падает');
});

test('countTeam считает живые статусы', () => {
  const team = fixture('team_cafe.json');
  const models = team.members.map((m) => memberModel(m, null, NOW));
  assert.deepEqual(countTeam(models), { total: 8, working: 3, waiting: 2, idle: 3 });
  assert.deepEqual(countTeam([]), { total: 0, working: 0, waiting: 0, idle: 0 });
});

test('availableProfiles исключает тех, кто уже в комнате', () => {
  const team = fixture('team_cafe.json');
  const profiles = fixture('profiles.json');
  const room = fixture('room_cafe.json');
  const free = availableProfiles(profiles, team, room).map((p) => p.profile);
  const taken = ['biz-director', 'biz-lawyer', 'biz-ops', 'biz-strategy', 'biz-marketing', 'biz-cfo', 'biz-runner', 'biz-critic'];
  const expected = profiles.items.map((p) => p.profile).filter((p) => !taken.includes(p));
  assert.deepEqual(free.sort(), expected.sort(), 'свободны ровно те, кого нет в комнате');
  assert.ok(free.includes('coder') && free.includes('lawyer') && free.includes('surveyor'));
  assert.equal(
    availableProfiles(profiles, null, { orchestrator: 'coder', members: [] }).length,
    profiles.items.length - 1,
    'директор комнаты тоже считается занятым',
  );
  assert.deepEqual(availableProfiles(null, null, null), []);
});

test('pickCurrent: объявленная фаза важнее эвристики', () => {
  const roadmap = fixture('roadmap_cafe.json');
  assert.equal(pickCurrent(roadmap).id, 'legal');

  assert.equal(pickCurrent({ phases: [{ id: 'a', status: 'done' }, { id: 'b', status: 'active' }] }).id, 'b');
  assert.equal(pickCurrent({ phases: [{ id: 'a', status: 'done' }, { id: 'b', status: 'blocked' }] }).id, 'b');
  assert.equal(
    pickCurrent({ current: { id: 'ghost' }, phases: [{ id: 'a', status: 'next' }, { id: 'b', status: 'done' }] }).id,
    'a',
    'текущая фаза не найдена — берём первую незакрытую',
  );
  assert.equal(pickCurrent({ current: 'a', phases: [{ id: 'a' }] }).id, 'a', 'строка вместо объекта тоже годится');
  assert.equal(pickCurrent({ phases: [] }), null);
  assert.equal(pickCurrent({}), null);
});

test('progressInfo и phaseSummary считают прогресс и блокеры', () => {
  const roadmap = fixture('roadmap_cafe.json');
  assert.deepEqual(progressInfo(roadmap.phases), { done: 1, total: 6, percent: 17, label: 'готово 1 из 6 этапов' });
  assert.deepEqual(progressInfo([]).label, 'этапов пока нет');

  const summary = phaseSummary(roadmap);
  assert.equal(summary.current.id, 'legal');
  assert.equal(summary.index, 1);
  assert.deepEqual(summary.blocked.map((p) => p.id), ['brand']);
  assert.equal(summary.upcoming.id, 'production');
  assert.equal(summary.nextAfter.id, 'brand', 'следующий незакрытый после текущего');
});

test('phaseSource и relatedDecisions', () => {
  const roadmap = fixture('roadmap_cafe.json');
  assert.equal(phaseSource(roadmap.phases[0]), null, 'в фикстуре источник не указан');

  const withSource = {
    phases: [
      { id: 'legal', title: 'Легализация', status: 'active', source_request: 't_a1b2c3d4' },
      { id: 'brand', title: 'Бренд', status: 'next' },
    ],
    decisions: [
      { id: 'd1', text: 'Идём так', status: 'accepted', source_request: 't_a1b2c3d4' },
      { id: 'd2', text: 'Бренд потом', status: 'proposed', phase: 'brand' },
      { id: 'd3', text: 'Из другого запроса', status: 'accepted', source_request: 't_ffffffff' },
    ],
  };
  assert.deepEqual(phaseSource(withSource.phases[0]), { requestId: 't_a1b2c3d4', title: 't_a1b2c3d4' });
  assert.deepEqual(relatedDecisions(withSource, 'legal').map((d) => d.id), ['d1'], 'по источнику');
  assert.deepEqual(relatedDecisions(withSource, 'brand').map((d) => d.id), ['d2'], 'по phase');
  assert.deepEqual(relatedDecisions(withSource, 'sales'), []);
  assert.deepEqual(relatedDecisions(roadmap, 'legal'), [], 'без связей — пусто');
});

test('relatedSteps берёт шаги того же запроса-источника', () => {
  const items = [
    { id: 't_a1b2c3d4:ns_1', kind: 'next_step', text: 'Подать заявление на ИП', source_request: 't_a1b2c3d4', status: 'open' },
    { id: 't_a1b2c3d4:fq_1', kind: 'future_question', text: 'Когда подавать в Роспатент?', source_request: 't_a1b2c3d4', status: 'open' },
    { id: 't_ffffffff:ns_1', kind: 'next_step', text: 'Чужой шаг', source_request: 't_ffffffff', status: 'open' },
    { id: 'x', kind: 'next_step', text: 'Без источника', status: 'open' },
  ];
  assert.deepEqual(relatedSteps(items, 't_a1b2c3d4').map((i) => i.id), ['t_a1b2c3d4:ns_1', 't_a1b2c3d4:fq_1']);
  assert.deepEqual(relatedSteps(items, 't_deadbeef'), []);
  assert.deepEqual(relatedSteps(items, null), [], 'без источника ничего не выдумываем');
  assert.deepEqual(relatedSteps(null, 't_a1b2c3d4'), []);
});

test('decisionGroups: группировка, фильтр и поиск', () => {
  const roadmap = fixture('roadmap_cafe.json');
  const all = decisionGroups(roadmap.decisions);
  assert.deepEqual(all.groups.map((g) => g.key), ['accepted', 'proposed']);
  assert.deepEqual(all.counts, { total: 5, accepted: 3, proposed: 2, other: 0 });
  assert.equal(all.shown, 5);
  assert.equal(all.groups[0].title, 'Принято');

  const acceptedOnly = decisionGroups(roadmap.decisions, { status: 'accepted' });
  assert.deepEqual(acceptedOnly.groups.map((g) => g.key), ['accepted']);
  assert.equal(acceptedOnly.shown, 3);

  const proposedOnly = decisionGroups(roadmap.decisions, { status: 'proposed' });
  assert.equal(proposedOnly.shown, 2);
  assert.equal(proposedOnly.counts.total, 5, 'счётчики всегда по всем решениям');

  const byWhy = decisionGroups(roadmap.decisions, { q: 'КОМИССИЯ' });
  assert.equal(byWhy.shown, 1);
  assert.match(byWhy.groups[0].items[0].text, /Telegram/);

  const byOwner = decisionGroups(roadmap.decisions, { q: 'biz-marketing' });
  assert.equal(byOwner.shown, 1);

  const empty = decisionGroups(roadmap.decisions, { q: 'нет такого' });
  assert.deepEqual(empty.groups, []);
  assert.equal(empty.shown, 0);

  assert.deepEqual(decisionGroups(null).groups, []);
});

test('mirrorPathOf: roadmap → room → context_files', () => {
  const room = fixture('room_cafe.json');
  assert.equal(mirrorPathOf({ mirror_path: 'C:/Vault/Rooms/cafe/STATE.md' }, null), 'C:/Vault/Rooms/cafe/STATE.md');
  assert.equal(
    mirrorPathOf({}, room),
    'D:/Проекты/Vault/Projects/Rooms/cafe/STATE.md',
    'путь берётся из файлов контекста комнаты',
  );
  assert.equal(mirrorPathOf({ mirror_path: '  ' }, { context_files: [] }), null);
  assert.equal(mirrorPathOf(null, null), null);
});

test('canConsolidate и isEmptyRoadmap', () => {
  const roadmap = fixture('roadmap_cafe.json');
  assert.equal(canConsolidate(roadmap), true);
  assert.equal(isEmptyRoadmap(roadmap), false);

  const fresh = {
    current: null,
    phases: [
      { id: 'idea', title: 'Идея', status: 'next', note: '' },
      { id: 'legal', title: 'Легализация', status: 'next', note: '' },
    ],
    decisions: [],
  };
  assert.equal(isEmptyRoadmap(fresh), true);
  assert.equal(canConsolidate(fresh), false);
  assert.equal(isEmptyRoadmap({ phases: [{ id: 'idea', status: 'next', note: 'ждёт' }] , decisions: [] }), false);
  assert.equal(isEmptyRoadmap({ phases: [], decisions: [] }), true);
});

test('withRoomRoles: подпись руководителя — из room.orchestrator (Д1)', () => {
  const room = { orchestrator: 'biz-director', critic: 'biz-critic' };
  const members = [
    { profile: 'biz-director', role: 'worker' },
    { profile: 'biz-critic', role: 'worker' },
    { profile: 'biz-ops', role: 'worker' },
  ];
  const out = withRoomRoles(members, room);
  assert.equal(out[0].role, 'orchestrator');
  assert.equal(out[1].role, 'critic');
  assert.equal(out[2].role, 'worker');
  assert.equal(members[0].role, 'worker', 'исходный список не меняем');
  assert.deepEqual(withRoomRoles(null, room), []);
  assert.equal(withRoomRoles([{ profile: 'x', role: 'worker' }], null)[0].role, 'worker');
});
