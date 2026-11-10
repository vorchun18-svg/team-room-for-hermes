import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom, createFakeDocument, FakeElement } from './fake-dom.mjs';
import {
  ensureCss, formatStatusRu, statusChip, roleChip, avatar, profileLabel,
  emptyState, skeleton, errorBanner, mdBlock, pathChip, sectionHeader,
  dueBadge, segmented, field, iconButton, lsGet, lsSet,
  confirmDialog, promptDialog,
} from '../web/js/ui.js';

/* ------------------------------------------------------------------ helpers */

/** removeChild отсутствует в минимальном fake-dom — добавляем только в тесте. */
if (typeof FakeElement.prototype.removeChild !== 'function') {
  FakeElement.prototype.removeChild = function removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx !== -1) { this.childNodes.splice(idx, 1); node.parentNode = null; }
    return node;
  };
}

async function withDialogDom(fn) {
  const d = createFakeDocument();
  d.body = d.createElement('div');
  d.head = d.createElement('head');
  const prevFocus = d.createElement('button');
  prevFocus.focus = () => { prevFocus.focused = true; };
  d.activeElement = prevFocus;
  globalThis.document = d;
  try { return await fn(d, d.body, prevFocus); } finally { delete globalThis.document; }
}

function withDom(fn) {
  const d = installFakeDom();
  try { return fn(d); } finally { uninstallFakeDom(); }
}

function collect(root, tag) {
  const out = [];
  if (!root) return out;
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (String(child.tagName).toLowerCase() === tag) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

function fakeStorage() {
  const map = new Map();
  return {
    map,
    getItem(k) { return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { map.set(k, String(v)); },
    removeItem(k) { map.delete(k); },
    clear() { map.clear(); },
  };
}

function walkElements(root, fn) {
  for (const child of root.childNodes || []) {
    if (child.nodeType !== 1) continue;
    fn(child);
    walkElements(child, fn);
  }
}

/* ------------------------------------------------------------------ статусы */

test('formatStatusRu speaks Russian for request and card states', () => {
  assert.equal(formatStatusRu('queued'), 'в очереди');
  assert.equal(formatStatusRu('working'), 'в работе');
  assert.equal(formatStatusRu('reviewing'), 'проверка');
  assert.equal(formatStatusRu('synthesizing'), 'сводит');
  assert.equal(formatStatusRu('needs_you'), 'нужен ответ');
  assert.equal(formatStatusRu('done'), 'готово');
  assert.equal(formatStatusRu('failed'), 'ошибка');

  assert.equal(formatStatusRu('triage'), 'разбор');
  assert.equal(formatStatusRu('todo'), 'к выполнению');
  assert.equal(formatStatusRu('ready'), 'готова');
  assert.equal(formatStatusRu('running'), 'идёт');
  assert.equal(formatStatusRu('review'), 'проверка');
  assert.equal(formatStatusRu('blocked'), 'заблокирована');
  assert.equal(formatStatusRu('archived'), 'архив');
  assert.equal(formatStatusRu('scheduled'), 'отложена');

  assert.equal(formatStatusRu('  DONE '), 'готово');
  assert.equal(formatStatusRu(''), '');
  assert.equal(formatStatusRu(null), '');
  assert.equal(formatStatusRu(undefined), '');
  assert.equal(formatStatusRu('legacy_state'), 'legacy_state');
});

test('statusChip: тон, русская подпись и пульс только у «идёт»', () => withDom(() => {
  const working = statusChip('working');
  assert.equal(working.className, 'chip status-chip chip--info status-chip--live');
  assert.equal(working.textContent, 'в работе');
  assert.ok(working.find('span'), 'пульс-точка есть');
  assert.equal(working.getAttribute('data-status'), 'working');

  const done = statusChip('done');
  assert.ok(done.className.includes('chip--ok'));
  assert.ok(!done.className.includes('status-chip--live'));
  assert.equal(done.textContent, 'готово');

  const needs = statusChip('needs_you');
  assert.ok(needs.className.includes('chip--warn'));
  assert.equal(needs.textContent, 'нужен ответ');

  const running = statusChip('running');
  assert.ok(running.className.includes('status-chip--live'));
  assert.equal(running.textContent, 'идёт');

  const blocked = statusChip('blocked');
  assert.ok(blocked.className.includes('chip--warn'));
  assert.equal(blocked.textContent, 'заблокирована');

  const unknown = statusChip('weird_state');
  assert.ok(unknown.className.includes('chip--outline'));
  assert.equal(unknown.textContent, 'weird_state');
}));

test('roleChip covers tree and room roles', () => withDom(() => {
  const root = roleChip('root');
  assert.ok(root.className.includes('chip--accent'));
  assert.equal(root.textContent, 'корень');

  const critic = roleChip('critic');
  assert.ok(critic.className.includes('chip--warn'));
  assert.equal(critic.textContent, 'критик');

  const worker = roleChip('worker');
  assert.ok(worker.className.includes('chip--muted'));
  assert.equal(worker.textContent, 'исполнитель');

  assert.equal(roleChip('synthesizer').textContent, 'руководитель');
  assert.equal(roleChip('revision').textContent, 'доработка');
  assert.equal(roleChip('observer').textContent, 'наблюдатель');
  assert.equal(roleChip('nonsense').textContent, 'nonsense');
}));

/* ------------------------------------------------------------------ аватары */

test('avatar: инициалы, размер и детерминированный оттенок', () => withDom(() => {
  const big = avatar('Михаил Петров');
  assert.equal(big.textContent, 'МП');
  assert.equal(big.getAttribute('aria-label'), 'Михаил Петров');
  assert.equal(big.className, 'avatar');

  const sm = avatar('Михаил Петров', { size: 'sm' });
  assert.ok(sm.className.includes('avatar--sm'));
  assert.equal(sm.style['--h'], big.style['--h']);

  const px = avatar('coder', { size: 32 });
  assert.equal(px.style.width, '32px');
  assert.equal(px.textContent, 'CO');

  assert.equal(avatar('').textContent, '?');
}));

test('profileLabel показывает имя и аватар', () => withDom(() => {
  const el = profileLabel({ profile: 'biz-director', name: 'Директор' });
  assert.ok(el.className.includes('profile-label'));
  assert.ok(el.find('span'));
  assert.equal(el.getAttribute('title'), 'biz-director');
  assert.ok(el.textContent.includes('Директор'));

  const bare = profileLabel('lawyer');
  assert.ok(bare.textContent.includes('lawyer'));
}));

/* -------------------------------------------------------------------- сроки */

test('dueBadge: просрочено/сегодня/завтра/дальше и мусор', () => withDom(() => {
  const now = new Date(2026, 9, 28); // 28 окт 2026

  const overdue = dueBadge('2026-10-27', now);
  assert.ok(overdue);
  assert.ok(overdue.className.includes('chip--danger'));
  assert.equal(overdue.textContent, 'просрочено · 27 окт.');

  const today = dueBadge('2026-10-28', now);
  assert.ok(today.className.includes('chip--warn'));
  assert.equal(today.textContent, 'сегодня');

  const tomorrow = dueBadge('2026-10-29', now);
  assert.ok(tomorrow.className.includes('chip--warn'));
  assert.equal(tomorrow.textContent, 'завтра');

  const later = dueBadge('2026-11-03', now);
  assert.equal(later.textContent, 'до 3 нояб.');
  assert.ok(!/chip--(ok|warn|danger|info|muted|accent)/.test(later.className));

  const nextYear = dueBadge('2027-01-05', now);
  assert.equal(nextYear.textContent, 'до 5 янв. 2027');

  assert.equal(dueBadge('', now), null);
  assert.equal(dueBadge(null, now), null);
  assert.equal(dueBadge('не дата', now), null);
  assert.equal(dueBadge('2026-02-31', now), null, 'несуществующая дата отбрасывается');
}));

/* -------------------------------------------------------------- localStorage */

test('lsGet/lsSet: префикс, JSON и безопасные значения по умолчанию', () => {
  const store = fakeStorage();
  const had = Object.prototype.hasOwnProperty.call(globalThis, 'localStorage');
  const prev = globalThis.localStorage;
  globalThis.localStorage = store;
  try {
    assert.equal(lsGet('missing', 'def'), 'def');
    assert.equal(lsGet('missing'), null);

    assert.equal(lsSet('tab', 'backlog'), true);
    assert.equal(store.map.get('teamroom:tab'), '"backlog"');
    assert.equal(lsGet('tab'), 'backlog');

    lsSet('obj', { a: 1, b: [2, 3] });
    assert.deepEqual(lsGet('obj'), { a: 1, b: [2, 3] });
    assert.deepEqual(JSON.parse(store.map.get('teamroom:obj')), { a: 1, b: [2, 3] });

    store.setItem('teamroom:broken', '{oops');
    assert.equal(lsGet('broken', 42), 42);

    lsSet('tab', undefined);
    assert.equal(store.map.has('teamroom:tab'), false);
  } finally {
    if (had) globalThis.localStorage = prev;
    else delete globalThis.localStorage;
  }
});

/* ------------------------------------------------------------------ markdown */

test('mdBlock собирает разметку и не исполняет XSS', () => withDom(() => {
  const src = [
    '# Свод',
    '',
    '<script>window.__pwned = 1</script>',
    '',
    '<img src=x onerror="window.__pwned = 1">',
    '',
    '[клик](javascript:window.__pwned=1)',
    '',
    '[файл](docs/report.md)',
  ].join('\n');

  const block = mdBlock(src);
  assert.equal(block.className, 'markdown');
  assert.equal(block.find('script'), null);
  assert.equal(block.find('img'), null);

  walkElements(block, (el) => {
    for (const name of el.attributes.keys()) {
      assert.ok(!/^on/i.test(name), 'обработчик ' + name + ' не должен попадать в DOM');
    }
    const href = el.getAttribute('href') || '';
    assert.ok(!/^\s*javascript:/i.test(href), 'javascript: ссылка отброшена');
  });

  assert.ok(block.textContent.includes('<script>'), 'опасный текст остался текстом');
  assert.ok(block.textContent.includes('window.__pwned'));

  const anchors = collect(block, 'a');
  const fileLink = anchors.find((a) => a.getAttribute('data-path'));
  assert.ok(fileLink, 'ссылка на локальный файл сохранилась');
  assert.equal(fileLink.getAttribute('data-path'), 'docs/report.md');
}));

test('mdBlock: заголовки, списки, таблица и код на месте', () => withDom(() => {
  const src = [
    '# Заголовок',
    '',
    '- один',
    '- два',
    '',
    '| Шаг | Срок |',
    '|---|---|',
    '| Свод | 30 окт. |',
    '',
    '```js',
    'const ok = 1 < 2;',
    '```',
    '',
  ].join('\n');

  const block = mdBlock(src);
  assert.equal(block.find('h1').textContent, 'Заголовок');
  assert.equal(collect(block, 'li').length, 2);
  assert.ok(block.find('table'));
  assert.ok(block.find('th'));
  const code = block.find('pre').find('code');
  assert.equal(code.getAttribute('class'), 'language-js');
  assert.match(code.textContent, /const ok = 1 < 2;/);
}));

/* ------------------------------------------------------- пусто/ошибка/загрузка */

test('emptyState: заголовок, подсказка и действие', () => withDom(() => {
  let clicked = 0;
  const el = emptyState({
    icon: 'lightbulb',
    title: 'Пока нет запросов',
    text: 'Напишите первый — команда разберёт его сама.',
    action: { label: 'Новый запрос', onclick: () => { clicked++; } },
  });
  assert.ok(el.className.includes('empty'));
  assert.equal(el.find('div').textContent, 'Пока нет запросов');
  assert.ok(el.textContent.includes('Напишите первый'));
  const btn = collect(el, 'button')[0];
  assert.equal(btn.textContent, 'Новый запрос');
  btn.dispatch('click');
  assert.equal(clicked, 1);
}));

test('skeleton: линии и aria-busy', () => withDom(() => {
  const el = skeleton(5);
  assert.equal(el.getAttribute('aria-busy'), 'true');
  assert.equal(el.getAttribute('role'), 'status');
  assert.equal(el.childNodes.length, 5);
  assert.equal(el.childNodes[0].className, 'skeleton skeleton--title');
  assert.equal(el.childNodes[1].className, 'skeleton skeleton--line');
  assert.equal(el.childNodes[3].className, 'skeleton skeleton--line skeleton--short');

  const fallback = skeleton('не число');
  assert.equal(fallback.childNodes.length, 3);
}));

test('errorBanner: сообщение, код сети и «Повторить»', () => withDom(() => {
  let retried = 0;
  const el = errorBanner({ code: 'network', message: 'Нет связи с локальным сервером' }, () => { retried++; });
  assert.equal(el.getAttribute('role'), 'alert');
  assert.ok(el.className.includes('banner--error'));
  assert.ok(el.textContent.includes('Нет связи с локальным сервером'));
  assert.ok(el.textContent.includes('приложение запущено'));
  assert.ok(!el.textContent.includes('127.0.0.1'), 'технический адрес не показываем');

  const btn = collect(el, 'button')[0];
  assert.equal(btn.textContent, 'Повторить');
  btn.dispatch('click');
  assert.equal(retried, 1);

  assert.ok(errorBanner(new Error('Сломалось')).textContent.includes('Сломалось'));
  assert.ok(errorBanner('Текст ошибки').textContent.includes('Текст ошибки'));
  assert.equal(collect(errorBanner(new Error('x')), 'button').length, 0);
}));

/* ---------------------------------------------------------------- поля/кнопки */

test('field связывает подпись с полем и выводит подсказку', () => withDom((d) => {
  const input = d.createElement('input');
  const el = field({ label: 'Название', control: input, hint: 'Коротко и по делу' });
  assert.equal(el.tagName.toLowerCase(), 'label');
  assert.ok(input.id, 'полю выдан id');
  assert.equal(el.getAttribute('for'), input.id);
  assert.equal(el.find('input'), input);
  assert.ok(el.textContent.includes('Коротко и по делу'));

  const other = field({ label: 'Другое', control: d.createElement('select') });
  assert.notEqual(other.getAttribute('for'), input.id);
}));

test('iconButton: размер, вариант и подпись для скринридера', () => withDom(() => {
  let clicks = 0;
  const el = iconButton({ icon: 'refresh', title: 'Обновить', onclick: () => { clicks++; } });
  assert.ok(el.className.includes('btn--icon'));
  assert.ok(el.className.includes('btn--ghost'));
  assert.equal(el.getAttribute('aria-label'), 'Обновить');
  assert.equal(el.getAttribute('title'), 'Обновить');
  assert.equal(el.find('i').className, 'codicon codicon-refresh');
  el.dispatch('click');
  assert.equal(clicks, 1);

  const danger = iconButton({ icon: 'trash', title: 'Удалить', variant: 'danger', size: 'sm' });
  assert.ok(danger.className.includes('btn--danger'));
  assert.ok(danger.className.includes('btn--sm'));
}));

test('sectionHeader: счётчик и действия', () => withDom((d) => {
  const action = d.createElement('button');
  const el = sectionHeader('Бэклог', { count: 7, actions: action });
  assert.ok(el.className.includes('section-header'));
  assert.equal(el.find('h2').textContent, 'Бэклог');
  assert.equal(el.find('h2').className, 'section-header__title');
  assert.ok(el.textContent.includes('7'));
  assert.equal(collect(el, 'button').length, 1);

  const bare = sectionHeader('Журнал');
  assert.ok(!bare.textContent.includes('7'));
}));

test('pathChip: путь в title, файл — в data-атрибутах', () => withDom(() => {
  const el = pathChip('C:/TeamRoom/Team/team-room/docs/reports/cafe/report.md');
  assert.ok(el.className.includes('chip--outline'));
  assert.equal(el.tagName.toLowerCase(), 'button');
  assert.match(el.getAttribute('title'), /report\.md$/);
  assert.match(el.textContent, /report\.md$/);
  assert.equal(el.find('i').className, 'codicon codicon-file-text');
}));

/* ------------------------------------------------------------------ сегменты */

test('segmented: выбор, onchange и стрелки', () => withDom(() => {
  const calls = [];
  const el = segmented({
    options: [{ value: 'day', label: 'День' }, { value: 'week', label: 'Неделя' }, { value: 'month', label: 'Месяц' }],
    value: 'week',
    onchange: (v) => calls.push(v),
  });
  assert.equal(el.getAttribute('role'), 'radiogroup');
  const buttons = collect(el, 'button');
  assert.equal(buttons.length, 3);
  assert.equal(buttons[1].getAttribute('aria-checked'), 'true');
  assert.equal(buttons[0].getAttribute('aria-checked'), 'false');
  assert.equal(buttons[1].tabIndex, 0);
  assert.equal(buttons[0].tabIndex, -1);

  buttons[0].dispatch('click');
  assert.deepEqual(calls, ['day']);
  assert.equal(buttons[0].getAttribute('aria-checked'), 'true');
  assert.ok(buttons[0].className.includes('segmented__btn--active'));

  el.dispatch('keydown', { key: 'ArrowRight', target: buttons[0], preventDefault() {} });
  assert.deepEqual(calls, ['day', 'week']);
  assert.equal(buttons[1].getAttribute('aria-checked'), 'true');

  el.dispatch('keydown', { key: 'End', target: buttons[1], preventDefault() {} });
  assert.deepEqual(calls, ['day', 'week', 'month']);
}));

/* -------------------------------------------------------------------- прочее */

test('confirmDialog: Enter подтверждает, Esc отменяет, фокус возвращается', async () => {
  await withDialogDom(async (d, body, prevFocus) => {
    const yes = confirmDialog({ title: 'Удалить?', text: 'Точно?', danger: true });
    assert.equal(body.childNodes.length, 1, 'оверлей смонтирован');
    const overlay = body.childNodes[0];
    assert.equal(overlay.className, 'dialog-backdrop');
    const dialog = overlay.childNodes[0];
    assert.equal(dialog.getAttribute('role'), 'dialog');
    assert.equal(dialog.getAttribute('aria-modal'), 'true');
    assert.ok(dialog.textContent.includes('Удалить?'));

    overlay.dispatch('keydown', { key: 'Enter', target: dialog, preventDefault() {} });
    assert.equal(await yes, true);
    assert.equal(body.childNodes.length, 0, 'оверлей снят');
    assert.equal(prevFocus.focused, true, 'фокус вернулся');

    const no = confirmDialog({ title: 'Отменить?' });
    const ov2 = body.childNodes[0];
    ov2.dispatch('keydown', {
      key: 'Escape', target: ov2, preventDefault() {}, stopPropagation() {},
    });
    assert.equal(await no, false);
  });
});

test('promptDialog: Enter в строке, Ctrl+Enter в многострочном, Esc — null', async () => {
  await withDialogDom(async (d, body) => {
    const single = promptDialog({ title: 'Имя', label: 'Как назвать?', initial: 'Свод' });
    const ov = body.childNodes[0];
    const input = ov.find('input');
    assert.equal(input.value, 'Свод');
    input.value = 'Новый свод';
    ov.dispatch('keydown', { key: 'Enter', target: input, preventDefault() {} });
    assert.equal(await single, 'Новый свод');

    const multi = promptDialog({ title: 'Доработка', label: 'Что поправить?', multiline: true });
    const ov2 = body.childNodes[0];
    const ta = ov2.find('textarea');
    ta.value = 'Правка';
    ov2.dispatch('keydown', { key: 'Enter', target: ta, preventDefault() {} });
    ov2.dispatch('keydown', {
      key: 'Escape', target: ta, preventDefault() {}, stopPropagation() {},
    });
    assert.equal(await multi, null, 'обычный Enter не отправляет многострочное поле');

    const multi2 = promptDialog({ title: 'Доработка', multiline: true });
    const ov3 = body.childNodes[0];
    const ta2 = ov3.find('textarea');
    ta2.value = 'Правка вторая';
    ov3.dispatch('keydown', {
      key: 'Enter', target: ta2, ctrlKey: true, preventDefault() {},
    });
    assert.equal(await multi2, 'Правка вторая');
  });
});

test('ensureCss без document резолвится и не падает', async () => {
  await ensureCss('css/view-ui.css');
  await ensureCss('');
  await ensureCss(undefined);
});
