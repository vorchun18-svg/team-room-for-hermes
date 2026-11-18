/**
 * settings-groups.test.mjs — W20-U2, пункт 15 (P11): «Настройки» по группам.
 *
 * Проверяем то, что видит человек и что легко сломать:
 *  • путь сокращается СЛЕВА (хвост пути остаётся видимым, полный — в подсказке);
 *  • проверки раскладываются по группам, проблемы идут первыми, пустых групп нет;
 *  • один сводный статус вместо трёх «работает»;
 *  • «один сохраняющий блок»: экран показывает, что именно не сохранено;
 *  • в самой вьюхе нет старых 15 строк подряд (PID, порт, пути вперемешку).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  SETTINGS_GROUPS, settingsGroup, shortenPathLeft, baseName,
  CHECK_GROUPS, checkGroupId, checksSummary, groupChecks, topProblems, worstStatus,
  overallStatus, pendingChanges, saveSummary, PREF_LABELS,
} from '../web/js/views/settings-groups.js';

const SOURCE = readFileSync(new URL('../web/js/views/settings.js', import.meta.url), 'utf8');
const CSS = readFileSync(new URL('../web/css/view-settings.css', import.meta.url), 'utf8');

const LONG_PATH = 'C:\\Users\\User\\AppData\\Local\\hermes\\cache\\scratch\\w20u2-settings-check\\data\\state\\map';

/* ─────────────────────────────────────────────────────────── группы экрана */

test('три группы из аудита и четвёртая «сохраняющая» — по порядку', () => {
  assert.deepEqual(SETTINGS_GROUPS.map((g) => g.title).slice(0, 3),
    ['Статус', 'Пути и данные', 'Диагностика']);
  assert.equal(SETTINGS_GROUPS[3].id, 'prefs');
  assert.ok(SETTINGS_GROUPS.every((g) => g.hint.length > 20), 'у каждой группы есть пояснение');
  assert.equal(settingsGroup('status').title, 'Статус');
  assert.equal(settingsGroup('нет такой'), null);
});

/* ────────────────────────────────────────────────────── пути: хвост виден */

test('путь сокращается слева: остаётся хвост, полный путь не теряется', () => {
  const short = shortenPathLeft(LONG_PATH, 40);
  assert.ok(short.startsWith('…'), 'сокращение слева: ' + short);
  assert.ok(short.endsWith('map'), 'последняя папка видна: ' + short);
  assert.ok(short.length <= 40, 'длина в пределах: ' + short.length);
  assert.ok(short.startsWith('…\\'), 'хвост начинается с разделителя, а не с обрывка имени');
  assert.ok(!short.includes('C:'), 'начало пути убрано');

  // Короткий путь не трогаем — человек видит его целиком.
  assert.equal(shortenPathLeft('C:\\temp\\data', 40), 'C:\\temp\\data');
  assert.equal(shortenPathLeft('', 40), '');
  assert.equal(shortenPathLeft(null, 40), '');
  // Слишком маленький лимит не превращает путь в «…».
  assert.ok(shortenPathLeft(LONG_PATH, 1).length <= 13);
  // Прямые слэши тоже поддерживаются (macOS/Linux).
  const posix = shortenPathLeft('/Users/user/Library/Application Support/team-room/data/state/map', 32);
  assert.ok(posix.startsWith('…/') && posix.endsWith('map'), posix);
});

test('последняя папка пути — для строк «Содержимое»', () => {
  assert.equal(baseName(LONG_PATH), 'map');
  assert.equal(baseName('C:\\TeamRoom\\Team\\team-room-dev\\'), 'team-room-dev');
  assert.equal(baseName(''), '');
});

/* ────────────────────────────────────────────── проверки по группам */

const CHECKS = [
  { id: 'version', title: 'Версия приложения', status: 'ok', value: '1.1.0' },
  { id: 'system', title: 'Система', status: 'ok', value: 'Windows 11 · Python 3.14.9' },
  { id: 'hermes_cli', title: 'Программа hermes', status: 'warn', value: 'не найдена' },
  { id: 'board', title: 'Доска задач', status: 'fail', value: 'файл доски не найден' },
  { id: 'vault', title: 'Vault и зеркало', status: 'ok', value: 'доступно' },
  { id: 'dictation', title: 'Диктовка', status: 'off', value: 'не настроена' },
  { id: 'autostart', title: 'Автозапуск', status: 'off', value: 'не настроен' },
  { id: 'unknown_future', title: 'Новый пункт', status: 'ok', value: 'задан' },
];

test('проверки разложены по группам, красное — наверху, пустых групп нет', () => {
  const groups = groupChecks(CHECKS);
  const ids = groups.map((g) => g.id);
  assert.equal(ids[0], 'data', 'группа с падающим пунктом идёт первой: ' + ids.join(','));
  assert.ok(!ids.includes('other') || ids.indexOf('other') > 0);

  const data = groups.find((g) => g.id === 'data');
  assert.deepEqual(data.items.map((i) => i.id), ['board', 'vault'], 'внутри группы: проблема, потом в порядке');
  assert.equal(data.worst, 'fail');

  const program = groups.find((g) => g.id === 'program');
  assert.deepEqual(program.items.map((i) => i.id), ['version', 'system']);
  assert.equal(program.counts.ok, 2);

  const comfort = groups.find((g) => g.id === 'comfort');
  assert.deepEqual(comfort.items.map((i) => i.id), ['dictation', 'autostart']);
  assert.equal(comfort.counts.off, 2);

  // Неизвестный id не пропадает с экрана.
  assert.deepEqual(groups.find((g) => g.id === 'other').items.map((i) => i.id), ['unknown_future']);
  // Группы без пунктов не отдаются вовсе.
  assert.equal(groupChecks([{ id: 'version', status: 'ok' }]).length, 1);
  assert.deepEqual(groupChecks([]), []);
});

test('новые id проверок попадают в «Прочее», известные — по таблице', () => {
  assert.equal(checkGroupId('board'), 'data');
  assert.equal(checkGroupId('autostart'), 'comfort');
  assert.equal(checkGroupId('что-то-новое'), 'other');
  assert.equal(checkGroupId(''), 'other');
  assert.ok(CHECK_GROUPS.length >= 4);
  assert.equal(CHECK_GROUPS[CHECK_GROUPS.length - 1].id, 'other');
});

test('счётчики и худший статус — для строки «всё в порядке / есть замечания»', () => {
  const summary = checksSummary(CHECKS);
  assert.deepEqual(summary, { ok: 4, warn: 1, fail: 1, off: 2, total: 8 });
  assert.equal(worstStatus(CHECKS), 'fail');
  assert.equal(worstStatus([{ status: 'ok' }, { status: 'off' }]), 'off');
  assert.equal(worstStatus([]), null);
  assert.deepEqual(topProblems(CHECKS, 2).map((i) => i.id), ['board', 'hermes_cli'],
    'сначала падение, потом предупреждение — без «не настроено»');
});

/* ─────────────────────────────────────────────── один сводный статус */

test('сводный статус: один вывод вместо трёх «работает»', () => {
  const ok = overallStatus({
    app: { sandbox: false },
    engine: { ok: true, schema_ok: true },
    dispatcher: { state: 'running' },
    watcher: { running: true },
    data: { writable: true },
    warnings: [],
  });
  assert.equal(ok.tone, 'ok');
  assert.equal(ok.title, 'Всё работает');

  const boardBroken = overallStatus({ engine: { ok: false, schema_ok: false } });
  assert.equal(boardBroken.tone, 'danger');
  assert.match(boardBroken.title, /Движок/);

  const engineDown = overallStatus({
    engine: { ok: true, schema_ok: true }, dispatcher: { state: 'stopped' },
  });
  assert.equal(engineDown.tone, 'danger');
  assert.ok(engineDown.issues.some((t) => /движок/.test(t)));

  const warned = overallStatus({
    engine: { ok: true, schema_ok: true },
    dispatcher: { state: 'running' },
    watcher: { running: false },
    data: { writable: false },
  });
  assert.equal(warned.tone, 'warn');
  assert.equal(warned.issues.length, 2);
  assert.match(warned.text, /фоновая проверка остановлена/);

  const sandbox = overallStatus({
    app: { sandbox: true }, engine: { ok: true, schema_ok: true }, dispatcher: { state: 'running' },
  });
  assert.equal(sandbox.tone, 'muted');
  assert.equal(sandbox.title, 'Песочница');

  assert.equal(overallStatus(null).tone, 'muted', 'пока данных нет — спокойное «Проверяю…»');
});

/* ──────────────────────────────────────── один сохраняющий блок */

test('один сохраняющий блок: показывает, что не сохранено', () => {
  assert.deepEqual(Object.keys(PREF_LABELS), ['theme', 'notify']);
  const saved = { theme: 'auto', notify: false };
  assert.deepEqual(pendingChanges(saved, { theme: 'auto', notify: false }), []);
  assert.deepEqual(pendingChanges(saved, { theme: 'dark', notify: false }), ['theme']);
  assert.deepEqual(pendingChanges(saved, { theme: 'dark', notify: true }), ['theme', 'notify']);
  assert.deepEqual(pendingChanges(saved, {}), [], 'поле не тронули — не считаем изменением');
  assert.deepEqual(pendingChanges(null, { theme: 'light' }), []);

  assert.equal(saveSummary([]).dirty, false);
  assert.match(saveSummary([]).text, /сохранены/);
  const dirty = saveSummary(['theme', 'notify']);
  assert.equal(dirty.dirty, true);
  assert.match(dirty.text, /тема, уведомления/);
});

/* ─────────────────────────────────────────── сама вьюха: разметка и порядок */

test('settings.js: группы, свёрнутая техника и единственная кнопка сохранения', () => {
  assert.ok(SOURCE.includes('function groupSection('), 'группы строятся одной функцией');
  for (const id of ['status', 'paths', 'diagnostics', 'prefs']) {
    assert.ok(SOURCE.includes("groupSection({ id: '" + id + "'"), 'группа ' + id + ' есть на экране');
  }
  assert.ok(SOURCE.includes('Технические подробности'), 'техника — в свёрнутом блоке');
  assert.ok(SOURCE.includes("h('details'"), 'свёрнутый блок — <details>');
  // PID, порт и часовой пояс живут внутри techDetails, а не в открытой части статуса.
  const techStart = SOURCE.indexOf('function techDetails');
  const techEnd = SOURCE.indexOf('function renderStatus');
  assert.ok(techStart > 0 && techEnd > techStart, 'свёрнутый блок — отдельная функция');
  const tech = SOURCE.slice(techStart, techEnd);
  const status = SOURCE.slice(techEnd, SOURCE.indexOf('/* --------------------------------------------------- пути и данные */'));
  for (const row of ['Номер процесса', 'Часовой пояс', 'Запущено']) {
    assert.ok(tech.includes(row), row + ' — в «Технических подробностях»');
  }
  // На лице статуса — только «Запущено/пояс» короткой строкой, без PID и порта.
  assert.ok(!status.includes('Номер процесса'), 'PID не показывается в открытой части');
  assert.ok(!status.includes('Часовой пояс'), 'часовой пояс — в подробностях');
  assert.ok(!status.includes('порт '), 'порт — в подробностях');

  // Один сохраняющий блок: одна кнопка сохранения на весь экран.
  const saves = SOURCE.match(/'Сохранить настройки'/g) || [];
  assert.equal(saves.length, 1, 'кнопка «Сохранить настройки» одна, найдено ' + saves.length);
  assert.equal((SOURCE.match(/data-action': 'settings-save'/g) || []).length, 1, 'один якорь сохранения');
  assert.ok(SOURCE.includes('pendingChanges('), 'экран считает несохранённые изменения');
  assert.ok(SOURCE.includes('shortenPathLeft('), 'пути рисуются с сокращением слева');
  assert.ok(SOURCE.includes('overallStatus('), 'сводный статус — один');
  assert.ok(SOURCE.includes('loadDiag('), 'группа «Диагностика» подтягивает выжимку');
});

test('настройки: пути с копированием и открытием папки сохранены', () => {
  assert.ok(SOURCE.includes('Копировать путь'), 'копирование пути осталось');
  assert.ok(SOURCE.includes('openPath('), 'открытие папки осталось');
  assert.ok(CSS.includes('.set-path'), 'для пути есть свой стиль');
  assert.ok(/\.set-path__text[^{]*\{[^}]*text-overflow/m.test(CSS), 'длинный путь не вылезает');
  assert.ok(CSS.includes('.set-group'), 'группы оформлены');
  assert.ok(CSS.includes('.set-save'), 'сохраняющий блок оформлен');
  assert.ok(CSS.includes('.set-more'), 'свёрнутые подробности оформлены');
  assert.ok(CSS.includes('@media (max-width: 720px)'), 'узкий экран учтён');
});

test('проверка системы: группировка подключена, автозапуск не потерян', async () => {
  const diagSource = readFileSync(new URL('../web/js/views/diagnostics.js', import.meta.url), 'utf8');
  assert.ok(diagSource.includes('groupChecks'), 'экран «Проверка системы» группирует пункты');
  assert.ok(diagSource.includes("data-action"), 'кнопки действий (включая автозапуск) на месте');
  assert.ok(diagSource.includes('/api/diagnostics/autostart'), 'панель автозапуска сохранена');
});
