/**
 * delo.test.mjs — экран «Дела» и карточка «Дело» (W26-D6).
 *
 * Проверяет без браузера:
 *   • форму фикстуры `fixtures/delo.json` по контракту §1 правил волны 26
 *     (её же строит сервер D5 — расхождение поймает этот тест);
 *   • чистые функции `delo-model.js`: порядок, сроки, кнопки, «Найдено»;
 *   • маршруты `#/delos`, `#/delo/<id>`, вкладку `#/room/<id>/delos`;
 *   • память маршрута не выбрасывает карточку дела в комнату при перезагрузке.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  FILE_ROLE_LABEL, OUTBOX_STATE_LABEL, STATUS_LABEL, agoText, dayNoun, deloMeta, dueInfo, dutyState,
  fetchNeedsBadge, fileRows, foundGroups, foundTitle, foundType, foundVerdict, listCounters, needButtons,
  needsTotal, nextStepText, normalizeDelo, outboxButtons, outboxMeta, partRows, sortDelos, stageChips,
  stageProgress, stampText, waitingRows,
} from '../web/js/views/delo-model.js';
import { TABS, parseHash } from '../web/js/router.js';
import { chooseHash, parseRouteHash } from '../web/js/route-memory.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const fx = (name) => JSON.parse(readFileSync(join(ROOT, 'fixtures', name), 'utf8'));
const DELOS = fx('delo.json');
const ROOMS = fx('rooms.json').rooms.map((r) => r.id);

const STATUSES = ['open', 'waiting', 'paused', 'done', 'cancelled'];
const STAGE_STATES = ['done', 'now', 'todo'];
const PART_STATES = ['agreed', 'discussing', 'todo'];
const FILE_ROLES = ['final', 'source', 'draft'];
const OUTBOX_STATES = ['draft', 'approved', 'sent', 'failed', 'cancelled'];
const OUTBOX_KINDS = ['email', 'file', 'message'];
const NEED_KINDS = ['approve', 'question', 'permission'];
const RELATIONS = ['искомое', 'упоминает', 'смежное', 'не то'];
const ACTIONS = ['approve', 'reject', 'answer', 'set_stage', 'add_part', 'set_part', 'approve_outbox',
  'edit_outbox', 'cancel_outbox', 'mark_sent', 'pause', 'resume', 'done', 'add_waiting', 'clear_waiting'];

/* ─────────────────────────────────────────────────── фикстура и контракт §1 */

test('фикстура: три разных дела, идентификаторы и комнаты существуют', () => {
  assert.equal(DELOS.items.length, 3, 'в фикстуре ровно три дела');
  const ids = DELOS.items.map((d) => d.id);
  assert.equal(new Set(ids).size, 3, 'id дел не повторяются');
  for (const id of ids) assert.match(id, /^dl_[0-9a-z]{4,16}$/);
  for (const delo of DELOS.items) {
    assert.ok(ROOMS.includes(delo.room), 'комната дела есть в rooms.json: ' + delo.room);
  }
  assert.ok(Number(DELOS.generated_at) > 0, 'generated_at — время, а не строка');
});

test('фикстура: обязательные поля дела по контракту §1', () => {
  for (const delo of DELOS.items) {
    for (const key of ['id', 'room', 'title', 'status', 'due', 'owner', 'members', 'updated_at', 'request_id',
      'stages', 'waiting_on', 'needs_you', 'result', 'outbox', 'found', 'timeline', 'duties']) {
      assert.ok(Object.prototype.hasOwnProperty.call(delo, key), delo.id + ': нет поля ' + key);
    }
    assert.ok(STATUSES.includes(delo.status), delo.id + ': статус из перечня');
    assert.ok(delo.due === null || /^\d{4}-\d{2}-\d{2}$/.test(delo.due), delo.id + ': срок — ISO-дата или null');
    assert.ok(Array.isArray(delo.members) && delo.members.length >= 1, delo.id + ': исполнители');
    assert.ok(Number(delo.updated_at) > 0, delo.id + ': updated_at');
    assert.ok(delo.request_id === null || /^t_[0-9a-f]{8}$/.test(delo.request_id), delo.id + ': request_id');

    for (const stage of delo.stages) {
      assert.ok(stage.id && stage.title, delo.id + ': этап без id/названия');
      assert.ok(STAGE_STATES.includes(stage.state), delo.id + ': состояние этапа ' + stage.state);
    }
    assert.equal(delo.stages.filter((s) => s.state === 'now').length, 1, delo.id + ': ровно один текущий этап');

    for (const need of delo.needs_you) {
      assert.ok(NEED_KINDS.includes(need.kind), delo.id + ': вид ожидания ' + need.kind);
      assert.ok(String(need.text || '').trim(), delo.id + ': пустой текст ожидания');
      assert.ok(Array.isArray(need.actions) && need.actions.length >= 1, delo.id + ': у ожидания нет действия');
      for (const action of need.actions) assert.ok(ACTIONS.includes(action), delo.id + ': действие ' + action);
      if (need.kind === 'question') {
        assert.ok(Array.isArray(need.options) && need.options.length >= 2, delo.id + ': у вопроса нет вариантов');
        for (const opt of need.options) assert.ok(opt.id && opt.label, delo.id + ': вариант без id/подписи');
      }
    }

    for (const part of delo.result.parts) {
      assert.ok(part.id && part.title, delo.id + ': часть результата без id/названия');
      assert.ok(PART_STATES.includes(part.state), delo.id + ': состояние части ' + part.state);
    }
    for (const file of delo.result.files) {
      assert.ok(file.path && file.title, delo.id + ': файл без пути/названия');
      assert.ok(FILE_ROLES.includes(file.role), delo.id + ': роль файла ' + file.role);
    }
    for (const item of delo.outbox) {
      assert.ok(OUTBOX_KINDS.includes(item.kind), delo.id + ': вид исходящего ' + item.kind);
      assert.ok(OUTBOX_STATES.includes(item.state), delo.id + ': состояние исходящего ' + item.state);
      assert.ok(Array.isArray(item.attachments), delo.id + ': вложения — массив');
      if (item.state === 'sent') assert.ok(Number(item.sent_at) > 0, delo.id + ': у отправленного нет времени');
    }
    for (const found of delo.found) {
      assert.ok(found['название'] && found['тип'], delo.id + ': «Найдено» без названия/типа');
      assert.ok(['файл', 'сайт', 'письмо', 'запись'].includes(found['тип']), delo.id + ': тип «Найдено»');
      assert.ok(RELATIONS.includes(found['отношение']), delo.id + ': отношение «Найдено» ' + found['отношение']);
    }
    for (const ev of delo.timeline) {
      assert.ok(Number(ev.at) > 0 && ev.text, delo.id + ': событие хода дела');
    }
    for (const duty of delo.duties) {
      assert.ok(duty.id && duty.title && duty.schedule_human, delo.id + ': дежурство без id/названия/расписания');
    }
  }
});

test('фикстура: три разных сценария — письмо наружу, поиск с «Найдено», код/проект', () => {
  const byId = Object.fromEntries(DELOS.items.map((d) => [d.id, d]));
  const contract = DELOS.items.find((d) => d.outbox.some((o) => o.kind === 'email'));
  assert.ok(contract, 'есть дело с письмом наружу');
  assert.ok(contract.needs_you.length >= 2, 'у договора два ожидания (утверждение и вопрос)');
  assert.ok(contract.found.length >= 2, 'у договора есть «Найдено»');

  const search = DELOS.items.find((d) => d.id !== contract.id && d.found.some((f) => f['отношение'] === 'искомое'));
  assert.ok(search, 'есть дело-поиск с найденным искомым');

  const other = DELOS.items.find((d) => d.id !== contract.id && d.id !== search.id);
  assert.ok(other, 'есть третье дело');
  assert.equal(other.needs_you.length, 0, 'третье дело идёт без решений человека');
  assert.ok(Object.keys(byId).length === 3);
});

/* ─────────────────────────────────────────────────────────────── модель */

test('порядок списка: ждущие решения — первыми, затем по сроку', () => {
  // W28-QA1: сроки считаем от «сегодня», а не от зашитой даты — иначе тест
  // «истекает» по календарю (19.11 прошло, и дело 'a' стало просроченным).
  const inDays = (n) => {
    const d = new Date();
    d.setDate(d.getDate() + n);
    return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'),
      String(d.getDate()).padStart(2, '0')].join('-');
  };
  const items = [
    { id: 'a', needs_you: [], due: inDays(1), updated_at: 1 },
    { id: 'b', needs_you: [{ id: 'n' }], due: inDays(12), updated_at: 2 },
    { id: 'c', needs_you: [], due: null, updated_at: 3 },
    { id: 'd', needs_you: [{ id: 'm' }], due: inDays(3), updated_at: 1 },
  ];
  assert.deepEqual(sortDelos(items).map((d) => d.id), ['d', 'b', 'a', 'c']);
  assert.equal(needsTotal(items), 2);
  assert.deepEqual(listCounters(items), { total: 4, waiting: 2, needs: 2, overdue: 0 });
});

test('сроки: сегодня, завтра, через N дней, просрочено, без срока', () => {
  const now = new Date(2026, 10, 18, 12, 0, 0).getTime();
  assert.equal(dueInfo('2026-11-18', now).text, 'срок: сегодня');
  assert.equal(dueInfo('2026-11-18', now).tone, 'danger');
  assert.equal(dueInfo('2026-11-19', now).text, 'срок: завтра');
  assert.equal(dueInfo('2026-11-21', now).text, 'срок: сб 21.11');
  assert.equal(dueInfo('2026-11-21', now).soon, true);
  assert.equal(dueInfo('2026-11-25', now).soon, false);
  const late = dueInfo('2026-11-16', now);
  assert.equal(late.text, 'просрочено на 2 дня');
  assert.equal(late.overdue, true);
  assert.equal(late.tone, 'danger');
  assert.deepEqual(dueInfo(null, now), { text: 'без срока', tone: 'muted', overdue: false, soon: false, days: null });
  assert.equal(dayNoun(1), '1 день');
  assert.equal(dayNoun(3), '3 дня');
  assert.equal(dayNoun(5), '5 дней');
});

test('этапы: знаки, тон и прогресс', () => {
  const chips = stageChips([
    { id: 's1', title: 'Сбор', state: 'done' },
    { id: 's2', title: 'Черновик', state: 'now' },
    { id: 's3', title: 'Финал', state: 'todo' },
    { id: 's4', title: '', state: 'todo' },
  ]);
  assert.equal(chips.length, 3, 'этап без названия не рисуем');
  assert.deepEqual(chips.map((s) => s.mark), ['✓', '●', '○']);
  assert.equal(stageProgress(chips), '1 из 3');
});

test('«Ждём»: you — это «вас», давность словами', () => {
  const now = 1795022885;
  const rows = waitingRows([
    { who: 'you', what: 'решение по v3', since: now - 3600 },
    { who: 'контрагент', what: 'ответ на письмо', since: now - 86400 },
  ], now);
  assert.equal(rows[0].who, 'вас');
  assert.equal(rows[0].isYou, true);
  assert.equal(rows[1].who, 'контрагент');
  assert.equal(rows[1].since, 'вчера');
});

test('кнопки «ждёт вас»: варианты вопроса, свой вариант, запасное действие', () => {
  const q = needButtons({
    id: 'n1', kind: 'question', text: '30 или 50?', actions: ['answer'],
    options: [{ id: 'o30', label: '30 %' }, { id: 'o50', label: '50 %' }],
  });
  assert.deepEqual(q.map((b) => b.label), ['30 %', '50 %', 'Свой вариант…']);
  assert.equal(q[0].optionId, 'o30');
  assert.equal(q[2].own, true);
  assert.equal(q[0].kind, 'secondary');

  const approve = needButtons({ id: 'n2', kind: 'approve', text: 'Утвердить', actions: ['approve', 'reject'] });
  assert.deepEqual(approve.map((b) => [b.action, b.kind]), [['approve', 'primary'], ['reject', 'secondary']]);

  const empty = needButtons({ id: 'n3', kind: 'approve', text: 'нет действий', actions: [] });
  assert.equal(empty.length, 1, 'пункт без действий всё равно даёт выход');
  assert.equal(empty[0].action, 'approve');

  const perm = needButtons({ id: 'n4', kind: 'permission', text: 'разрешить', actions: ['permission'] });
  assert.equal(perm[0].label, 'Разрешить');
});

test('исходящее: состояния и кнопки — наружу только по «да»', () => {
  const draft = outboxButtons({ id: 'o1', state: 'draft' });
  assert.deepEqual(draft.map((b) => b.action), ['approve_outbox', 'edit_outbox', 'cancel_outbox']);
  assert.equal(draft[0].label, 'Отправить после утверждения');
  const approved = outboxButtons({ id: 'o1', state: 'approved' });
  assert.equal(approved[0].action, 'mark_sent');
  assert.equal(approved[0].label, 'Отметить отправленным');
  const sent = outboxButtons({ id: 'o1', state: 'sent' });
  assert.equal(sent.length, 0, 'отправленное не предлагает отправить ещё раз');
  assert.equal(OUTBOX_STATE_LABEL.draft, 'черновик · не отправлено');
  const meta = outboxMeta({ to: 'contracts@example.com', attachments: ['Результат/Договор_v3.docx'], state: 'draft' });
  assert.match(meta, /Кому: contracts@example\.com/);
  assert.match(meta, /Вложение: Договор_v3\.docx/);
});

test('«Найдено»: группы по отношению и честная плашка', () => {
  const list = [
    { 'название': 'Спецификация.pdf', 'тип': 'файл', 'отношение': 'искомое', 'где': 'Проект' },
    { 'название': 'Старый договор.pdf', 'тип': 'файл', 'отношение': 'упоминает', 'где': 'Архив' },
    { 'название': 'Сайт', 'тип': 'сайт', 'отношение': 'смежное', 'где': 'https://example.org' },
    { 'название': 'Статья', 'тип': 'сайт', 'отношение': 'не то', 'где': 'https://example.org/x' },
  ];
  const groups = foundGroups(list);
  assert.deepEqual(groups.map((g) => g.key), ['sought', 'mentions', 'related', 'notit']);
  assert.equal(groups[0].items.length, 1);
  assert.equal(foundVerdict(list), '', 'искомое есть — плашки нет');
  const noSought = list.filter((i) => i['отношение'] !== 'искомое');
  assert.match(foundVerdict(noSought), /^Искомого нет; нашли смежное: 3 записи/);
  assert.equal(foundTitle(list[0]), 'Спецификация.pdf');
  assert.equal(foundType(list[2]), 'сайт');
  assert.equal(foundGroups([]).length, 0);
});

test('результат: части и файлы с подписями', () => {
  const parts = partRows([{ id: 'p1', title: 'Оплата', state: 'agreed' }, { id: 'p2', title: 'Неустойка', state: 'discussing' }]);
  assert.deepEqual(parts.map((p) => p.label), ['согласовано', 'обсуждаем']);
  assert.equal(parts[0].mark, '✓');
  const files = fileRows([{ path: 'Результат/Договор_v3.docx', title: 'Договор_v3.docx', role: 'final' }]);
  assert.equal(files[0].roleLabel, FILE_ROLE_LABEL.final);
  assert.equal(files[0].title, 'Договор_v3.docx');
});

test('«что дальше»: решение, этап, пауза, всё сделано', () => {
  assert.equal(nextStepText({ needs_you: [{ kind: 'approve', text: 'Утвердить v3' }] }).tone, 'warn');
  assert.match(nextStepText({ needs_you: [{ kind: 'approve', text: 'Утвердить v3' }] }).text, /^Утвердить: Утвердить v3/);
  const running = nextStepText({ needs_you: [], stages: [{ id: 's', title: 'Правка', state: 'now' }] });
  assert.match(running.text, /Идёт этап «Правка»/);
  assert.match(nextStepText({ needs_you: [], status: 'paused' }).text, /на паузе/);
  assert.equal(nextStepText({ needs_you: [], stages: [], status: 'open' }).text, 'Всё сделано');
});

test('терпимость к неполному ответу сервера и мета строки', () => {
  const d = normalizeDelo({ id: 'dl_x', title: 'Тест' });
  assert.equal(d.status, 'open');
  assert.deepEqual(d.needs_you, []);
  assert.deepEqual(d.result, { parts: [], files: [] });
  assert.equal(d.next, undefined);
  assert.equal(normalizeDelo(null).title, 'Без названия');
  const meta = deloMeta({ owner: 'юрист', members: ['a', 'b'], updated_at: 1000 }, 1000 + 7200);
  assert.match(meta, /ведёт: юрист/);
  assert.match(meta, /исполнители: a, b/);
  assert.match(meta, /обновлено 2 часа назад/);
});

test('время: «12 мин назад», «сегодня 10:42», «вчера 18:10»', () => {
  const now = 1795022885;
  assert.equal(agoText(now - 60 * 12, now), '12 мин назад');
  assert.equal(agoText(now - 3600 * 2, now), '2 часа назад');
  assert.equal(agoText(now - 86400, now), 'вчера');
  assert.equal(agoText(null, now), '');
  assert.match(stampText(now - 60, now), /^сегодня \d\d:\d\d$/);
  assert.match(stampText(now - 86400, now), /^вчера \d\d:\d\d$/);
  assert.equal(dutyState({ state: 'active' }).label, 'работает');
  assert.equal(dutyState({ state: 'paused' }).tone, 'muted');
  assert.equal(STATUS_LABEL.waiting, 'ждёт ответа');
});

/* ─────────────────────────────────────────────────────────────── маршруты */

test('маршруты: список, карточка и вкладка комнаты ведут в один вид', () => {
  const list = parseHash('#/delos');
  assert.equal(list.name, 'delos');
  assert.equal(list.view, 'delo');
  const card = parseHash('#/delo/dl_26c4a1b2');
  assert.equal(card.name, 'delo');
  assert.equal(card.view, 'delo');
  assert.equal(card.id, 'dl_26c4a1b2');
  const tab = parseHash('#/room/fn/delos');
  assert.equal(tab.name, 'room');
  assert.equal(tab.roomId, 'fn');
  assert.equal(tab.tab, 'delos');
  assert.equal(tab.view, 'delo', 'вкладка «Дела» грузит views/delo.js');
  assert.ok(TABS.some((t) => t.id === 'delos' && t.title === 'Дела'), 'вкладка «Дела» есть в списке вкладок');
});

test('память маршрута: карточку дела не выбрасывает в комнату', () => {
  assert.deepEqual(parseRouteHash('#/delos'), { kind: 'delos' });
  assert.deepEqual(parseRouteHash('#/delo/dl_26c4a1b2'), { kind: 'delo', id: 'dl_26c4a1b2' });
  assert.equal(chooseHash({ urlHash: '#/delos', roomIds: ['fn'], tabIds: ['composer'] }), '#/delos');
  assert.equal(chooseHash({ urlHash: '#/delo/dl_26c4a1b2', roomIds: ['fn'], tabIds: ['composer'] }), '#/delo/dl_26c4a1b2');
});

test('значок меню: число дел с ожиданием считается по всем комнатам', async () => {
  const apiRef = {
    async get(url) {
      if (url.includes('/fn/')) return { items: [{ id: 'a', needs_you: [{ id: '1' }, { id: '2' }] }] };
      if (url.includes('/cafe/')) return { items: [{ id: 'b', needs_you: [] }] };
      throw new Error('404');
    },
  };
  assert.equal(await fetchNeedsBadge(apiRef, ['fn', 'cafe']), 2);
  const allApi = { async get(url) {
    if (url === '/api/delos?room=all') return { items: [{ id: 'a', needs_you: [{ id: '1' }] }] };
    throw new Error('лишний запрос по комнатам');
  } };
  assert.equal(await fetchNeedsBadge(allApi, ['fn']), 1, 'общий список читается одним запросом');
  assert.equal(await fetchNeedsBadge(apiRef, []), null);
  assert.equal(await fetchNeedsBadge({ get: async () => { throw new Error('x'); } }, ['fn']), 0);
});
