/**
 * plan-streams.test.mjs — блок «Части работы» на вкладке «Карта проекта».
 *
 * Что проверяем (критерии волны 9, PLAN-FE-MAP):
 *  • чипы всех семи статусов по-русски: Предложено, В очереди, Идёт, Ждёт лимит,
 *    На проверке, Готово, Отложено;
 *  • карточка: название, цель, ответственный, участники, срок, «после «X»»,
 *    ссылка на под-запрос;
 *  • правая пометка «предложил руководитель» / «добавил вы» и причина (title/раскрытие);
 *  • пустое состояние без карты; блок скрыт при выключенном проектном режиме;
 *  • подтверждение «Запустить всё»: сколько частей работы и «одновременно до N»;
 *  • кнопки зовут свои обработчики (по id части работы);
 *  • текст ошибки сети — человеческий;
 *  • в видимом тексте нет технических слов.
 *
 * DOM — мини-DOM из tests/fake-dom.mjs, браузер не нужен.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, uninstallFakeDom } from './fake-dom.mjs';
import {
  STREAM_STATUSES, streamStatusMeta, streamStatusChip, streamOrigin,
  streamsSummary, streamsCountLine, launchableIds, dependencyNote, launchConfirm,
  renderStreams, planBlockVisible, projectModeHint, humanError, openStreamEditor,
  peopleIndex, ownerLabelOf,
} from '../web/js/views/plan-streams.js';

/* --------------------------------------------------------------- помощники */

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

function all(node, out = []) {
  if (!node) return out;
  if (node.nodeType === 1) out.push(node);
  for (const child of node.childNodes || []) all(child, out);
  return out;
}

function byClass(root, cls) {
  return all(root).filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

function byTag(root, tag) {
  return all(root).filter((n) => String(n.tagName).toLowerCase() === tag);
}

function buttonWith(root, label) {
  return all(root).find((n) => String(n.tagName).toLowerCase() === 'button' && text(n).trim() === label);
}

function cardsOf(root) {
  return byClass(root, 'ps-card');
}

const PLAN = {
  version: 1,
  brief_root_id: 't_brief001',
  updated_at: 1793100000,
  status: 'running',
  streams: [
    {
      id: 's1', title: 'Договор с поставщиком', goal: 'Подписать договор на упаковку',
      owner: 'biz-lawyer', members: ['biz-lawyer', 'biz-ops'], depends_on: [],
      due: '2026-11-15', status: 'done', request_id: 't_s1aaa', reason: '',
      created_by: 'manager',
    },
    {
      id: 's2', title: 'Партия пробной упаковки', goal: 'Получить образцы к 5 ноября',
      owner: 'biz-ops', members: ['biz-ops'], depends_on: ['s1'],
      due: '2026-11-05', status: 'running', request_id: 't_s2bbb',
      reason: 'руководитель разбил работу: договор нужен раньше партии', created_by: 'manager',
    },
    {
      id: 's3', title: 'Витрина и фото', goal: 'Снять и оформить 6 карточек',
      owner: 'biz-marketing', members: ['biz-marketing', 'biz-runner'], depends_on: ['s1'],
      due: null, status: 'waiting_quota', request_id: null,
      reason: 'добавили вручную', created_by: 'mikhail',
    },
    {
      id: 's4', title: 'Цены для маркетплейса', goal: 'Смета и наценка',
      owner: 'biz-cfo', members: [], depends_on: [], due: '2026-12-01',
      status: 'proposed', request_id: null, reason: '', created_by: 'manager',
    },
  ],
};

function render(overrides = {}) {
  return renderStreams(Object.assign({
    plan: PLAN, roomId: 'cafe', room: { id: 'cafe', project_mode: true },
    now: new Date('2026-10-30T09:00:00'), maxParallel: 2,
  }, overrides));
}

/* ------------------------------------------------------------------ статусы */

test('семь статусов части работы — по-русски, в заданном порядке', () => {
  assert.deepEqual(STREAM_STATUSES, [
    'proposed', 'queued', 'running', 'waiting_quota', 'review', 'done', 'dropped',
  ]);
  const labels = STREAM_STATUSES.map((s) => streamStatusMeta(s).label);
  assert.deepEqual(labels, [
    'Предложено', 'В очереди', 'Идёт', 'Ждёт лимит', 'На проверке', 'Готово', 'Отложено',
  ]);
  for (const s of STREAM_STATUSES) {
    assert.ok(streamStatusMeta(s).hint, 'у статуса ' + s + ' есть пояснение');
  }
});

test('неизвестный статус не ломает карточку', () => {
  const meta = streamStatusMeta('weird_state');
  assert.equal(meta.label, 'weird_state');
  assert.equal(meta.tone, 'outline');
});

test('чип статуса: класс тона и data-атрибут, «Ждёт лимит» — жёлтый', () => {
  installFakeDom();
  try {
    const chip = streamStatusChip('waiting_quota');
    assert.equal(chip.getAttribute('data-stream-status'), 'waiting_quota');
    assert.match(chip.getAttribute('class'), /chip--warn/);
    assert.equal(text(chip), 'Ждёт лимит');

    assert.match(streamStatusChip('done').getAttribute('class'), /chip--ok/);
    assert.match(streamStatusChip('running').getAttribute('class'), /chip--info/);
    assert.equal(text(streamStatusChip('отложено')), 'отложено', 'регистр и кириллица — как пришло');
    assert.equal(text(streamStatusChip('DROPPED')), 'Отложено');
  } finally {
    uninstallFakeDom();
  }
});

/* ------------------------------------------------------------------ карточки */

test('карточка показывает название, цель, ответственного, участников и срок', () => {
  installFakeDom();
  try {
    const root = render();
    const cards = cardsOf(root);
    assert.equal(cards.length, 4, 'по карточке на часть работы');

    const card = cards[1]; // s2
    assert.equal(card.getAttribute('data-stream'), 's2');
    assert.equal(byClass(card, 'ps-card__title')[0].textContent, 'Партия пробной упаковки');
    assert.equal(byClass(card, 'ps-card__goal')[0].textContent, 'Получить образцы к 5 ноября');
    // Ответственный — по-русски: без карты людей это роль, а не biz-ops.
    assert.match(text(byClass(card, 'ps-card__meta')[0]), /Исполнитель/, 'ответственный виден по-русски');
    assert.doesNotMatch(text(card), /biz-/, 'технический профиль в тексте карточки не показываем');
    assert.match(text(byClass(card, 'ps-card__members')[0]), /1 участник/);
    assert.match(text(card), /до 5 нояб\./, 'срок по-русски');
  } finally {
    uninstallFakeDom();
  }
});

test('зависимость читается словами: «после «Договор с поставщиком»»', () => {
  installFakeDom();
  try {
    const root = render();
    const card = cardsOf(root)[1];
    const dep = byClass(card, 'ps-card__dep')[0];
    assert.ok(dep, 'у s2 есть зависимость');
    assert.equal(text(dep), 'после «Договор с поставщиком»');
  } finally {
    uninstallFakeDom();
  }
  const byId = { s1: { title: 'Договор' }, s9: {} };
  assert.equal(dependencyNote({ depends_on: ['s9'] }, byId), 'после «части работы без названия»');
  assert.equal(dependencyNote({ depends_on: [] }, byId), '');
  assert.equal(dependencyNote({}, byId), '');
});

test('ссылка на под-запрос есть только у запущенных частей работы', () => {
  installFakeDom();
  try {
    const root = render();
    const cards = cardsOf(root);
    const link = byClass(cards[1], 'ps-card__request')[0];
    assert.ok(link, 'у s2 есть ссылка');
    assert.equal(link.tagName.toLowerCase(), 'a');
    assert.equal(link.getAttribute('href'), '#/room/cafe/request/t_s2bbb');
    assert.equal(byClass(cards[3], 'ps-card__request').length, 0, 'у предложенного — нет');
  } finally {
    uninstallFakeDom();
  }
});

test('пометка «предложил руководитель» / «добавил вы», причина — в title и раскрытии', () => {
  assert.equal(streamOrigin('manager').label, 'предложил руководитель');
  assert.equal(streamOrigin('mikhail').label, 'добавил вы');
  assert.equal(streamOrigin(undefined).label, 'предложил руководитель');

  installFakeDom();
  try {
    const root = render();
    const cards = cardsOf(root);

    // s2: причина есть — раскрывается по клику, подсказка в title
    const details = byClass(cards[1], 'ps-origin')[0];
    assert.ok(details, 'причина раскрывается');
    const summary = byTag(details, 'summary')[0];
    assert.equal(summary.textContent, 'предложил руководитель');
    assert.match(summary.getAttribute('title') || '', /причину/);
    assert.equal(byClass(details, 'ps-origin__why')[0].textContent,
      'руководитель разбил работу: договор нужен раньше партии');

    // s3: добавлено вручную
    assert.equal(byTag(cards[2], 'summary')[0].textContent, 'добавил вы');

    // s4: причины нет — просто подпись с подсказкой
    assert.equal(byClass(cards[3], 'ps-origin').length, 0);
    assert.equal(byClass(cards[3], 'ps-card__origin')[0].textContent, 'предложил руководитель');
  } finally {
    uninstallFakeDom();
  }
});

/* ---------------------------------------------------------------- счётчики */

test('счётчики карты: всего, идёт, ждёт лимит, предложено', () => {
  const c = streamsSummary(PLAN);
  assert.equal(c.total, 4);
  assert.equal(c.running, 1);
  assert.equal(c.waiting, 1);
  assert.equal(c.proposed, 1);
  assert.equal(c.done, 1);
  assert.equal(c.active, 2, 'идёт + ждёт лимит');
  assert.equal(streamsSummary(null).total, 0);

  assert.equal(streamsCountLine(PLAN), '4 части работы · идёт 1 · ждёт лимит 1 · предложено 1');
  assert.equal(streamsCountLine(null), 'пока пусто');
  assert.equal(streamsCountLine({ streams: [{ id: 'a', status: 'done' }] }), '1 часть работы');
});

test('запускать можно предложенное и стоящее в очереди', () => {
  assert.deepEqual(launchableIds(PLAN), ['s4']);
  assert.deepEqual(launchableIds({
    streams: [
      { id: 'a', status: 'proposed' }, { id: 'b', status: 'queued' },
      { id: 'c', status: 'running' }, { id: 'd' },
    ],
  }), ['a', 'b', 'd'], 'без статуса считаем предложенным');
  assert.deepEqual(launchableIds(null), []);
});

/* ------------------------------------------------------------ подтверждение */

test('«Запустить всё»: сколько частей работы, «одновременно до N», предупреждение', () => {
  const many = launchConfirm(PLAN, 2);
  assert.equal(many.count, 1);
  assert.equal(many.title, 'Запустить часть работы?');
  assert.match(many.text, /Начнут работу 1 часть работы/);
  assert.match(many.text, /Одновременно — до 2 частей работы/);
  assert.match(many.text, /настоящие агенты/);
  assert.match(many.text, /тратить время модели/);

  const four = launchConfirm({
    streams: ['a', 'b', 'c', 'd'].map((id) => ({ id, status: 'proposed' })),
  }, 4);
  assert.equal(four.count, 4);
  assert.equal(four.title, 'Запустить все части работы?');
  assert.match(four.text, /Начнут работу 4 части работы/);
  assert.match(four.text, /до 4 частей работы/);

  const none = launchConfirm({ streams: [{ id: 'a', status: 'done' }] }, 2);
  assert.equal(none.count, 0);
  assert.match(none.text, /запускать нечего/);
});

test('«до 1 части работы» — родительный падеж единственного, не «частей работы»', () => {
  const one = launchConfirm(PLAN, 1);
  assert.match(one.text, /Одновременно — до 1 части работы:/);
  assert.doesNotMatch(one.text, /до 1 частей работы/);
  const three = launchConfirm(PLAN, 3);
  assert.match(three.text, /Одновременно — до 3 частей работы:/);
});

/* --------------------------------------------------- ответственный по-русски */

test('ответственный: имя из /team, иначе роль; профиль — только в title', () => {
  installFakeDom();
  try {
    // people — как ответ /api/rooms/{r}/team: имя профиля и роль в комнате.
    const people = peopleIndex([
      { profile: 'biz-ops', title: 'Операции', role: 'worker' },
      { profile: 'biz-director', title: '', role: 'orchestrator' },
    ]);
    const root = render({ people });
    const card = cardsOf(root)[1]; // s2, owner biz-ops
    const meta = byClass(card, 'ps-card__meta')[0];
    assert.match(text(meta), /Операции/, 'показано человеческое имя профиля');
    assert.doesNotMatch(text(card), /biz-ops/, 'профиль не виден в тексте');
    const owner = byClass(card, 'profile-label')[0];
    assert.match(String(owner.getAttribute('title') || ''), /biz-ops/, 'профиль — в подсказке');

    // Профиль без имени в /team — показываем роль, а не огрызок id.
    assert.deepEqual(ownerLabelOf({ owner: 'biz-director' }, people),
      { name: 'Руководитель комнаты', hint: 'biz-director' });
    assert.deepEqual(ownerLabelOf({ owner: 'ghost' }, people), { name: 'Исполнитель', hint: 'ghost' });
    // Явная подпись от сервера важнее: owner_label.
    assert.deepEqual(ownerLabelOf({ owner: 'biz-ops', owner_label: 'Операции и склад' }, people),
      { name: 'Операции и склад', hint: 'biz-ops' });
    assert.equal(ownerLabelOf({ owner: '' }, people), null);
  } finally {
    uninstallFakeDom();
  }
});

test('участники части работы — по именам, без системных id', () => {
  installFakeDom();
  try {
    const root = render({ people: [{ profile: 'biz-runner', title: 'Раннер', role: 'utility' }] });
    const card = cardsOf(root)[2]; // s3, участники biz-marketing и biz-runner
    const members = byClass(card, 'ps-card__members')[0];
    assert.match(String(members.getAttribute('title') || ''), /Раннер/);
    assert.doesNotMatch(text(members), /biz-/);
  } finally {
    uninstallFakeDom();
  }
});

/* -------------------------------------------------------------------- кнопки */

test('шапка блока: «Запустить всё», «Добавить часть работы», «Собрать карту из запросов комнаты»', () => {
  installFakeDom();
  try {
    const calls = [];
    const root = render({
      onLaunchAll: (ids) => calls.push(['all', ids]),
      onAdd: () => calls.push(['add']),
      onRebuild: () => calls.push(['rebuild']),
    });
    assert.ok(byClass(root, 'ps-launch-all'), 'есть громкая кнопка запуска');
    const launchAll = buttonWith(root, 'Запустить всё');
    launchAll.dispatch('click', { type: 'click' });
    assert.deepEqual(calls[0], ['all', ['s4']], 'запускает только предложенные');

    buttonWith(root, 'Добавить часть работы').dispatch('click', { type: 'click' });
    buttonWith(root, 'Собрать карту из запросов комнаты').dispatch('click', { type: 'click' });
    assert.deepEqual(calls.slice(1), [['add'], ['rebuild']]);
  } finally {
    uninstallFakeDom();
  }
});

test('у части работы: «Запустить», «Отложить», «Изменить» зовут обработчики с id', () => {
  installFakeDom();
  try {
    const calls = [];
    const root = render({
      onLaunch: (id) => calls.push(['launch', id]),
      onDrop: (id) => calls.push(['drop', id]),
      onEdit: (id) => calls.push(['edit', id]),
    });
    const cards = cardsOf(root);

    buttonWith(cards[3], 'Запустить').dispatch('click', { type: 'click' });
    buttonWith(cards[3], 'Отложить').dispatch('click', { type: 'click' });
    buttonWith(cards[3], 'Изменить').dispatch('click', { type: 'click' });
    assert.deepEqual(calls, [['launch', 's4'], ['drop', 's4'], ['edit', 's4']]);

    assert.equal(buttonWith(cards[1], 'Запустить'), undefined, 'идущее не запускают повторно');
    assert.equal(buttonWith(cards[0], 'Отложить'), undefined, 'готовое не откладывают');
  } finally {
    uninstallFakeDom();
  }
});

test('пока идёт запрос, кнопки не нажимаются (busy)', () => {
  installFakeDom();
  try {
    const root = render({ busy: true });
    const launchAll = buttonWith(root, 'Запустить всё');
    assert.ok(launchAll.hasAttribute('disabled'));
    assert.equal(buttonWith(cardsOf(root)[3], 'Запустить'), undefined);
  } finally {
    uninstallFakeDom();
  }
});

/* --------------------------------------------------------------- пусто/скрыто */

test('пусто: «Карты пока нет» + что делать', () => {
  installFakeDom();
  try {
    const root = render({ plan: null });
    assert.equal(root.getAttribute('data-plan'), 'no');
    const empty = byClass(root, 'ps-empty')[0];
    assert.ok(empty, 'пустое состояние есть');
    assert.equal(byClass(empty, 'empty__title')[0].textContent, 'Карты пока нет');
    assert.equal(byClass(empty, 'empty__text')[0].textContent,
      'Напишите главный запрос — руководитель комнаты предложит части работы.');
    assert.equal(buttonWith(root, 'Запустить всё'), undefined, 'запускать нечего');
  } finally {
    uninstallFakeDom();
  }
});

test('пустая карта с действием «Собрать карту из запросов комнаты»', () => {
  installFakeDom();
  try {
    let rebuilt = 0;
    const root = render({ plan: null, onRebuild: () => { rebuilt += 1; } });
    const btn = buttonWith(root, 'Собрать карту из запросов комнаты');
    btn.dispatch('click', { type: 'click' });
    assert.equal(rebuilt, 1);
  } finally {
    uninstallFakeDom();
  }
});

test('проектный режим выключен — блока нет, только тихая ссылка', () => {
  assert.equal(planBlockVisible({ project_mode: false }), false);
  assert.equal(planBlockVisible({ project_mode: true }), true);
  assert.equal(planBlockVisible(null), true, 'пока поле не пришло — считаем включённым');

  installFakeDom();
  try {
    const hint = projectModeHint();
    // W17-R §2 (B-9): ссылка открывает настройки ЭТОЙ комнаты — подпись короче,
    // а куда ведёт, сказано в подсказке.
    assert.equal(text(hint), 'Включить проектный режим');
    assert.match(hint.getAttribute('title'), /настройки этой комнаты/);
    assert.match(hint.getAttribute('class'), /btn--link/);
  } finally {
    uninstallFakeDom();
  }
});

/* -------------------------------------------------------------------- ошибки */

test('ошибка сети объясняется по-человечески, без технических слов', () => {
  const msg = humanError({ code: 'network', message: 'Failed to fetch' });
  assert.equal(msg, 'Нет связи с приложением. Проверьте, что оно запущено, и повторите.');
  assert.doesNotMatch(msg, /fetch|network/i);

  const unknown = humanError({ code: 'http_500', message: 'Internal Server Error' });
  assert.match(unknown, /Движок команды/);

  assert.match(humanError({ code: 'unknown' }), /Не удалось выполнить действие/);
});

/* --------------------------------------------------- технические слова/тексты */

test('в видимом тексте нет dispatcher/kanban/tenant и id вида t_…', () => {
  installFakeDom();
  try {
    const root = render();
    const visible = text(root);
    assert.doesNotMatch(visible, /dispatcher|kanban|tenant|kick/i);
    assert.doesNotMatch(visible, /\bt_[0-9a-z]{4,}\b/i, 'id запроса только в href/title');
    // id остаётся в ссылке — по нему открывается под-запрос
    const hrefs = byClass(root, 'ps-card__request').map((a) => a.getAttribute('href')).join(' ');
    assert.match(hrefs, /t_s2bbb/);
  } finally {
    uninstallFakeDom();
  }
});

test('подсказка «Ждёт лимит» объясняет, что продолжится само', () => {
  installFakeDom();
  try {
    const root = render();
    const note = byClass(root, 'ps-note')[0];
    assert.ok(note, 'пояснение есть');
    assert.match(text(note), /продолжится сама/);
    assert.match(text(note), /лимит/);
  } finally {
    uninstallFakeDom();
  }
});

/* -------------------------------------------------------------------- диалог */

test('диалог «Изменить»: поля заполнены, микрофон не отключён, «Сохранить» отдаёт значения', async () => {
  const doc = installFakeDom();
  doc.body = doc.createElement('body');
  try {
    const promise = openStreamEditor({ mode: 'edit', stream: PLAN.streams[1], members: ['biz-ops'] });
    const dialog = byClass(doc.body, 'ps-dialog')[0];
    assert.ok(dialog, 'диалог смонтирован');
    assert.equal(byClass(dialog, 'dialog__title')[0].textContent, 'Изменить часть работы');

    const inputs = byTag(dialog, 'input').concat(byTag(dialog, 'textarea'));
    assert.ok(inputs.length >= 4, 'название, цель, ответственный, срок');
    for (const el of inputs) {
      assert.equal(el.hasAttribute('data-mic'), false, 'микрофон не отключаем');
      assert.equal(el.hasAttribute('data-mic-off'), false);
    }
    assert.equal(byTag(dialog, 'input')[0].value, 'Партия пробной упаковки');

    buttonWith(dialog, 'Сохранить').dispatch('click', { type: 'click' });
    const values = await promise;
    assert.equal(values.title, 'Партия пробной упаковки');
    assert.equal(values.goal, 'Получить образцы к 5 ноября');
    assert.equal(values.owner, 'biz-ops');
    assert.equal(values.due, '2026-11-05');
    assert.deepEqual(values.members, ['biz-ops'], 'участники — как в части работы');
  } finally {
    uninstallFakeDom();
  }
});

test('диалог «Добавить» не сохраняет пустое название', async () => {
  const doc = installFakeDom();
  doc.body = doc.createElement('body');
  try {
    const promise = openStreamEditor({ mode: 'add', members: ['biz-ops'] });
    const dialog = byClass(doc.body, 'ps-dialog')[0];
    assert.equal(byClass(dialog, 'dialog__title')[0].textContent, 'Добавить часть работы');
    buttonWith(dialog, 'Добавить').dispatch('click', { type: 'click' });
    assert.equal(byClass(doc.body, 'ps-dialog').length, 1, 'диалог остался открыт');
    assert.equal(byTag(dialog, 'input')[0].getAttribute('aria-invalid'), 'true');

    buttonWith(dialog, 'Отмена').dispatch('click', { type: 'click' });
    assert.equal(await promise, null);
  } finally {
    uninstallFakeDom();
  }
});
