/**
 * W17-S (SVOD, фронт): подписи версий, «Что изменилось» и предпросмотр доработки.
 *
 * Тест держит ОДНО правило с сервером (`contract.revision_digest`): число «K ваших
 * ответов» и состав пунктов, которые уйдут руководителю, считаются одинаково —
 * поэтому число в предпросмотре не может разойтись с телом карточки.
 *
 * Запуск: node --test tests/svod-versions.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(join(HERE, '..', 'fixtures', 'request_svod.json'), 'utf8'));

const {
  versionLabel, versionChanges, revisionDigest, shortLine, latestVersionAt, REVISE_NO_NOTE,
} = await import('../web/js/views/request-answer.js');

const history = FIXTURE.synthesis.history;
const v1 = history[0];
const v2 = history[1];
const v3 = history[2];

/* ── подпись версии ───────────────────────────────────────── */

test('версия 1 подписана как первый свод', () => {
  assert.equal(versionLabel(v1, FIXTURE), 'Версия 1 — первый свод');
});

test('версия 2 называет замечание Михаила', () => {
  const label = versionLabel(v2, FIXTURE);
  assert.match(label, /^Версия 2 — доработка по вашему замечанию: «Добавь прямую ответственность/);
  assert.ok(label.length < 260, 'подпись не расползается на пол-экрана');
});

test('версия 3 говорит, что учтены ответы на вопросы', () => {
  assert.equal(versionLabel(v3, FIXTURE), 'Версия 3 — после ваших ответов и уточнений (3)');
});

test('без данных руководителя подпись не врёт и не пустует', () => {
  const bare = { version: 4, kind: 'revision', task_id: 'нет-такого', note: 'Доработка после замечаний' };
  assert.equal(versionLabel(bare, FIXTURE), 'Версия 4 — доработка', 'служебная подпись не цитируется');
  assert.equal(versionLabel({}, FIXTURE), '');
  const leader = { version: 5, kind: 'revision', task_id: 't_5d0c11b4', reason: '' };
  assert.match(versionLabel(leader, FIXTURE), /по вашим ответам на вопросы/,
    'запасной путь — metadata.version_reason узла');
});

/* ── «Что изменилось» ─────────────────────────────────────── */

test('список изменений берётся из истории версий', () => {
  const list = versionChanges(v3, FIXTURE, '', '');
  assert.equal(list.length, 3);
  assert.match(list[0], /Приложение Б/);
});

test('без истории — из metadata.changes узла', () => {
  const entry = { version: 2, kind: 'revision', task_id: 't_5d0c11b3' };
  const list = versionChanges(entry, FIXTURE, '', '');
  assert.equal(list.length, 3);
  assert.match(list[1], /раздел 11\.4/);
});

test('совсем без данных — разница по разделам, а не пустота', () => {
  const entry = { version: 2, kind: 'revision', task_id: 'нет' };
  const before = '# Коротко\n\nстарый текст\n';
  const after = '# Коротко\n\nновый текст\n\n## Что осталось\n\nпроверить\n';
  const list = versionChanges(entry, { tree: [] }, before, after);
  assert.equal(list.length, 1);
  assert.match(list[0], /Добавлены разделы: Что осталось/);
});

test('первая версия не показывает «что изменилось»', () => {
  assert.equal(versionChanges(v1, FIXTURE, '', '').length, 0);
});

/* ── предпросмотр «что получит руководитель» ──────────────── */

test('предпросмотр считает 3 ответа и 3 уточнения после версии 3', () => {
  const digest = revisionDigest(FIXTURE, FIXTURE.synthesis);
  assert.equal(digest.version, 3);
  assert.equal(digest.answers.length, 3);
  assert.equal(digest.notes.length, 3);
  assert.equal(digest.count, 6, 'K в интерфейсе — как count в contract.revision_digest');
  assert.deepEqual(digest.answers.map((a) => a.id), ['q_1', 'q_2', 'q_3']);
  assert.match(digest.answers[0].answer, /коэффициент 24 штуки в коробе/);
  assert.equal(digest.decisions.length, 2);
  assert.equal(digest.steps.length, 1);
});

test('ответы до выпуска версии в предпросмотр не попадают', () => {
  const copy = JSON.parse(JSON.stringify(FIXTURE));
  copy.open_questions[0].answered_at = FIXTURE.synthesis.at - 10;
  const digest = revisionDigest(copy, copy.synthesis);
  assert.equal(digest.answers.length, 2);
  assert.equal(digest.count, 5);
});

test('сообщения команды и служебные события не считаются уточнениями', () => {
  const copy = JSON.parse(JSON.stringify(FIXTURE));
  copy.thread.push({ id: 'x1', at: FIXTURE.synthesis.at + 5000, author: 'default', kind: 'comment', text: 'принял' });
  copy.thread.push({ id: 'x2', at: FIXTURE.synthesis.at + 5000, author: 'mikhail', kind: 'event', text: 'статус изменён' });
  const digest = revisionDigest(copy, copy.synthesis);
  assert.equal(digest.notes.length, 3);
});

test('время выпуска версии — из истории, а не из synthesis.at напрямую', () => {
  assert.equal(latestVersionAt(FIXTURE.synthesis), FIXTURE.synthesis.at);
  assert.equal(latestVersionAt({ state: 'none', history: [] }), 0);
});

test('фраза для доработки без замечания понятна человеку', () => {
  assert.match(REVISE_NO_NOTE, /учётом моих ответов/);
});

/* ── мелкое ───────────────────────────────────────────────── */

test('shortLine режет по границе и ставит «…»', () => {
  assert.equal(shortLine('коротко', 20), 'коротко');
  const long = shortLine('а'.repeat(300), 50);
  assert.equal(long.length, 50);
  assert.ok(long.endsWith('…'));
});
