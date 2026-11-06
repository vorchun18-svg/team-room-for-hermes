/**
 * tests/participant-label.test.mjs — единая подпись участника (WAVE13 §11).
 *
 * Проверяем чистую логику: «Имя — должность», приоритет источников (ручная
 * подпись → описание профиля → роль), каталог из /api/profiles и то, что
 * подпись не ломается, когда описания нет (`default`).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  assembleLabel, setProfileDirectory, clearProfileDirectory, ensureProfileDirectory,
  participantLabel, labelOf, memberOf, roleOf, profileEntry,
} from '../web/js/participant-label.js';

const PROFILES = {
  items: [
    { profile: 'biz-cfo', title: 'Финансист', name: 'Анна', job: 'финансы и экономика (CFO)',
      label: 'Анна — финансы и экономика (CFO)', label_source: 'profile',
      description: 'Анна — финансы и экономика (CFO)' },
    { profile: 'biz-ops', title: 'Операции', name: 'Марина', job: 'поставщики и доставка',
      label: 'Марина — поставщики и доставка', label_source: 'describe', description: '' },
    { profile: 'default', title: 'Базовый профиль Hermes', name: '', job: '',
      label: '', label_source: 'role', description: '' },
  ],
};

test('assembleLabel соединяет части без пустых', () => {
  assert.equal(assembleLabel('Анна', 'финансы (CFO)'), 'Анна — финансы (CFO)');
  assert.equal(assembleLabel('Анна', ''), 'Анна');
  assert.equal(assembleLabel('', 'финансы'), 'финансы');
  assert.equal(assembleLabel('  ', ''), '');
});

test('labelOf: заголовок — имя и должность, роль отдельно, профиль отдельно', () => {
  const label = labelOf({ profile: 'biz-cfo', name: 'Анна', job: 'финансы и экономика (CFO)', role: 'worker' });
  assert.equal(label.title, 'Анна — финансы и экономика (CFO)');
  assert.equal(label.short, 'Анна');
  assert.equal(label.role, 'исполнитель');       // роль — бейдж, не заголовок
  assert.equal(label.sub, 'biz-cfo');            // техническое имя — мелкой строкой
  assert.equal(label.source, 'profile');
});

test('labelOf: только должность не превращается в имя', () => {
  const label = labelOf({ profile: 'lawyer', job: 'Юридические вопросы', role: 'worker' });
  assert.equal(label.title, 'Юридические вопросы');
  assert.equal(label.name, '');
});

test('labelOf без описания отдаёт роль (профиль default)', () => {
  const label = labelOf({ profile: 'default', role: 'orchestrator', title: 'Базовый профиль Hermes' });
  assert.equal(label.title, 'Руководитель комнаты');     // подпись из роли в комнате
  assert.equal(label.roleShort, 'руководитель');
  assert.equal(label.source, 'role');
  const bare = labelOf({ profile: 'biz-ops', role: 'worker' });
  assert.equal(bare.title, 'Исполнитель');
  const critic = labelOf({ profile: 'x', role: 'critic' });
  assert.equal(critic.title, 'Критик комнаты');
  assert.equal(critic.role, 'критик комнаты');
});

test('каталог профилей: подпись приходит из /api/profiles и кэшируется', async () => {
  clearProfileDirectory();
  let calls = 0;
  const api = { get: async () => { calls += 1; return PROFILES; } };
  await ensureProfileDirectory(api);
  await ensureProfileDirectory(api);                 // второй раз — из памяти
  assert.equal(calls, 1);
  const label = participantLabel('biz-cfo', { role: 'worker' });
  assert.equal(label.title, 'Анна — финансы и экономика (CFO)');
  assert.equal(label.source, 'profile');
  assert.equal(profileEntry('biz-cfo').name, 'Анна');
  clearProfileDirectory();
});

test('участник комнаты перекрывает подпись профиля', () => {
  clearProfileDirectory();
  setProfileDirectory(PROFILES);
  const room = {
    orchestrator: 'default',
    critic: 'biz-ops',
    members: [{ profile: 'biz-cfo', role: 'worker', label: 'Ира — бухгалтер проекта',
      name: 'Ира', job: 'бухгалтер проекта', label_source: 'member' }],
  };
  const label = participantLabel('biz-cfo', { room });
  assert.equal(label.title, 'Ира — бухгалтер проекта');
  assert.equal(label.source, 'member');
  // руководитель и критик — роль по настройкам комнаты
  assert.equal(roleOf(room, 'default'), 'orchestrator');
  assert.equal(roleOf(room, 'biz-ops'), 'critic');
  assert.equal(participantLabel('biz-ops', { room }).role, 'критик комнаты');
  assert.equal(memberOf(room, 'biz-cfo').profile, 'biz-cfo');
  assert.equal(memberOf(room, 'default'), null);
  clearProfileDirectory();
});

test('ответ /team с полями подписи главнее каталога', () => {
  clearProfileDirectory();
  setProfileDirectory(PROFILES);
  const member = { profile: 'biz-cfo', role: 'worker', name: 'Анна', job: 'финансы (CFO)',
    label: 'Анна — финансы (CFO)', label_source: 'describe', label_pending: true };
  const label = participantLabel('biz-cfo', { member });
  assert.equal(label.title, 'Анна — финансы (CFO)');
  assert.equal(label.source, 'describe');
  assert.equal(label.pending, true);
  clearProfileDirectory();
});
