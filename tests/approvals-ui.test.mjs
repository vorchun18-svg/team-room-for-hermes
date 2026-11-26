/**
 * approvals-ui.test.mjs — «Разрешения агентам» (W22-AP2), клиент без браузера.
 *
 * Проверяем то, что человек видит и нажимает:
 *  1) карточка называет участника и причину, полная команда лежит в моноширинном
 *     блоке (3000 знаков не теряются), превью короткое;
 *  2) `floor` — плашка «разрешить нельзя» + действия «Понятно, скрыть»/«Сообщить участнику»
 *     (W26: у каждого пункта есть действие), `grantable:false` — три рычага;
 *  3) три решения с точными подписями и подсказкой про отзыв;
 *  4) двойное нажатие не создаёт второго запроса, кнопки выключены на время,
 *     после ответа виден итог («Разрешено один раз»), а не старые кнопки;
 *  5) ошибка 409 показывается человеческим текстом, кнопки снова живые;
 *  6) счётчики: разрешение — отдельный вид дела в inboxSummary/inboxCount;
 *  7) collectApprovals берёт только ждущие и не дублирует;
 *  8) настройки: список, «Отозвать» → одно POST-тело {profile, pattern_key};
 *  9) тело действия `approval` — по контракту W22 §2.2.
 *
 * DOM — tests/fake-dom.mjs. Живая проверка — Chromium на моке (tools/cdp.py).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installFakeDom, FakeElement } from './fake-dom.mjs';
import {
  approvalCard, approvalHeadline, approvalReason, approvalState, commandPreview,
  approvalGroup, decisionResultText, approvalErrorText, approvalBody, approvalsWaitingLabel,
  collectApprovals, pendingApprovals, grantsSummary, createGrantsSection,
  createApprovals, APPROVAL_DECISIONS, canDecide,
  // W26-D2: три вида блокировок — «без тупиков».
  approvalDeadEnd, approvalActionsFor, isWaiting, floorApprovals, waitingApprovals, DEAD_ENDS,
} from '../web/js/views/approvals.js';
import { resetRequestStores } from '../web/js/views/request-store.js';
import { inboxSummary, inboxCount } from '../web/js/views/backlog-model.js';
import { APPROVALS_RU } from '../web/js/text-ru.js';

/* ────────────────────────────────────────────── мини-DOM: что нужно карточке */

function patchDom() {
  const P = FakeElement.prototype;
  if (typeof P.append !== 'function') {
    P.append = function append(...nodes) {
      for (const node of nodes) if (node && typeof node.nodeType === 'number') this.appendChild(node);
    };
  }
  if (typeof P.appendBefore !== 'function') P.appendBefore = null;
  if (typeof P.replaceChildren !== 'function') {
    P.replaceChildren = function replaceChildren(...nodes) {
      this.childNodes = [];
      for (const node of nodes) if (node) this.appendChild(node);
    };
  }
  if (typeof P.insertBefore !== 'function') {
    P.insertBefore = function insertBefore(node, ref) {
      const i = ref ? this.childNodes.indexOf(ref) : -1;
      node.parentNode = this;
      if (i === -1) this.childNodes.push(node);
      else this.childNodes.splice(i, 0, node);
      return node;
    };
  }
  if (typeof P.removeChild !== 'function') {
    P.removeChild = function removeChild(node) {
      const i = this.childNodes.indexOf(node);
      if (i !== -1) { this.childNodes.splice(i, 1); node.parentNode = null; }
      return node;
    };
  }
  if (typeof P.remove !== 'function') {
    P.remove = function remove() {
      if (this.parentNode && typeof this.parentNode.removeChild === 'function') {
        this.parentNode.removeChild(this);
      }
    };
  }
  if (typeof P.querySelector !== 'function') {
    P.querySelector = function querySelector(selector) {
      const cls = String(selector || '').replace(/^\./, '').split(/[\[\s.]/)[0];
      const walk = (node) => {
        for (const child of node.childNodes || []) {
          if (child.nodeType !== 1) continue;
          if (cls && String(child.getAttribute('class') || '').split(/\s+/).includes(cls)) return child;
          const deep = walk(child);
          if (deep) return deep;
        }
        return null;
      };
      return walk(this);
    };
  }
}

patchDom();
installFakeDom();

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function text(node) {
  if (!node) return '';
  if (node.nodeType === 3) return String(node.data || '');
  return (node.childNodes || []).map(text).join('');
}

function byAttr(root, name, value) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (child.getAttribute(name) === value) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

function buttons(root) {
  const out = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (String(child.tagName).toLowerCase() === 'button') out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

/** Длинная вымышленная команда: 3000 знаков, длинные пути без пробелов. */
function longCommand() {
  const base = 'cd C:/Users/User/Data/cafe-leto/2026/packaging/orders '
    + '&& python tools/build_report.py --input 2026-11-17/orders_full_export_v12.csv '
    + '--output C:/Users/User/Data/cafe-leto/2026/packaging/reports/final/packaging_orders_2026_11_17.html';
  const tail = Array.from({ length: 12 }, (_, i) => ' --extra-param-' + (i + 1)
    + '=C:/Users/User/Data/cafe-leto/2026/packaging/attachments/supplier_romashka_receipt_'
    + String(i + 1).padStart(2, '0') + '.csv').join('');
  let out = base + tail;
  while (out.length < 3000) out += '\n# шаг проверки ' + out.length;   // heredoc-подобный хвост
  return out.slice(0, 3000);
}

const APPROVAL = {
  id: 'ap_7f3a12cd',
  task_id: 't_5c8e4a02',
  profile: 'anna-assistant',
  member_title: 'Анна',
  kind: 'terminal',
  command: longCommand(),
  description: 'запуск скрипта через heredoc',
  pattern_key: 'script execution via heredoc',
  floor: false,
  state: 'pending',
  at: 1794940000,
  decided_at: null,
  card_state: 'blocked',
  count: 2,
  request_id: 't_5c8e4a02',
  request_title: 'Сборка отчёта по упаковке кофейни «Лето»',
};

/* ───────────────────────────────────────────────── 1. карточка и команда */

test('карточка: участник, причина и полная команда в моноширинном блоке', () => {
  const card = approvalCard(APPROVAL, {});
  const body = text(card);
  assert.match(body, /Анна хочет выполнить команду/);
  assert.match(body, /запуск скрипта через heredoc/);
  assert.match(body, /блокировалось 2 раза/);
  assert.match(body, /Карточка: Сборка отчёта по упаковке кофейни «Лето»/);

  const pre = card.find('pre');
  assert.ok(pre, 'моноширинный блок команды');
  assert.equal(text(pre), APPROVAL.command, 'команда целиком, без обрезки');
  assert.equal(text(pre).length, 3000);

  const details = card.find('details');
  assert.ok(details.getAttribute('data-long'), 'длинная команда свёрнута');
  assert.ok(!details.getAttribute('open'), 'длинная команда не раскрыта сама');

  const preview = commandPreview(APPROVAL.command, 60);
  assert.ok(preview.length <= 60, 'превью короткое: ' + preview.length);
  assert.match(preview, /…$/);
  assert.ok(APPROVAL.command.startsWith(preview.slice(0, 20)), 'превью — начало той же команды');
});

/* ─────────────────────────────────── 1б. строка-группа (W22-ST) */

const GROUPED = Object.assign({}, APPROVAL, {
  id: 'ap_bb11cc22',
  group: 'gr_1a2b3c4d',
  approval_ids: ['ap_bb11cc22', 'ap_cc33dd44', 'ap_dd55ee66'],
  commands: [
    { id: 'ap_bb11cc22', command: 'python -c "print(3)"', at: 1794940300, count: 1 },
    { id: 'ap_cc33dd44', command: 'python -c "print(2)"', at: 1794940200, count: 1 },
    { id: 'ap_dd55ee66', command: APPROVAL.command, at: 1794940000, count: 2 },
  ],
  command: 'python -c "print(3)"',
  count: 4,
});

test('группа: один вопрос на причину, в строке — последняя команда и счётчик «×N»', () => {
  const group = approvalGroup(GROUPED);
  assert.equal(group.grouped, true);
  assert.equal(group.ids.length, 3);
  assert.equal(group.count, 4);
  assert.equal(group.commandsCount, 3);
  assert.equal(group.last.command, 'python -c "print(3)"', 'в строке — последняя команда');
  assert.equal(group.rest.length, 2, 'остальные — в раскрываемом списке');

  const card = approvalCard(GROUPED, { onDecide: async () => ({ ok: true }) });
  const body = text(card);
  assert.match(body, /×4/, 'счётчик блокировок «×N»');
  assert.match(body, /команд: 3/);
  assert.match(body, /разрешение на этот вид/);
  // в теле показана ПОСЛЕДНЯЯ команда, а не первая из фикстуры
  const pres = [];
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (String(child.tagName).toLowerCase() === 'pre') pres.push(text(child));
      walk(child);
    }
  };
  walk(card);
  assert.equal(pres[0], 'python -c "print(3)"');
  const more = byAttr(card, 'data-ap-more', '2');
  assert.equal(more.length, 1, 'раскрываемый список остальных команд');
  assert.ok(!more[0].getAttribute('open'), 'свёрнут по умолчанию — строка не растягивается');
  assert.match(text(more[0]), /Ещё 2 команды с той же причиной/);
  // все команды группы видны в раскрытом списке целиком
  const all = pres.join('\n');
  assert.ok(all.includes('python -c "print(2)"'));
  assert.ok(all.includes(APPROVAL.command.slice(0, 40)));
  assert.equal(byAttr(card, 'data-decision', 'once').length
    + byAttr(card, 'data-decision', 'always').length
    + byAttr(card, 'data-decision', 'deny').length, 3,
  'три решения — по строке, а не по каждой команде');
  // W27-E2 §1: у ждущего разрешения есть и «Не актуально» — счётчик обнуляется
  // без «принятия решения» (строка-группа закрывается целиком).
  assert.equal(byAttr(card, 'data-action', 'approval-dismiss').length, 1);
});

test('группа: решение уходит с необязательным group, старый вид — без него', () => {
  assert.deepEqual(approvalBody(GROUPED, 'once'), {
    action: 'approval', approval_id: 'ap_bb11cc22', decision: 'once', group: 'gr_1a2b3c4d',
  });
  assert.deepEqual(approvalBody(APPROVAL, 'deny'), {
    action: 'approval', approval_id: 'ap_7f3a12cd', decision: 'deny',
  });
});

test('группа: одна блокировка без группы — прежняя подпись и никакого списка', () => {
  const single = Object.assign({}, APPROVAL, { group: '', approval_ids: [], commands: [] });
  const group = approvalGroup(single);
  assert.equal(group.grouped, false);
  assert.equal(group.rest.length, 0);
  const card = approvalCard(single, {});
  assert.match(text(card), /блокировалось 2 раза/);
  assert.equal(byAttr(card, 'data-ap-more', '1').length, 0);
  assert.equal(byAttr(card, 'data-ap-more', '2').length, 0);
});

test('заголовок и причина: без id и профиля в тексте', () => {
  assert.equal(approvalHeadline(APPROVAL), 'Анна хочет выполнить команду');
  assert.equal(approvalReason({ kind: 'terminal' }), 'запуск команды, которую Hermes считает опасной');
  assert.equal(approvalReason({ kind: 'execute_code' }), 'выполнение произвольного кода на Python');
  const card = text(approvalCard(APPROVAL, {}));
  assert.ok(!card.includes('ap_7f3a12cd'), 'id запроса разрешения наружу не выводится');
  assert.ok(!card.includes('anna-assistant'), 'профиль наружу не выводится');
});

/* ──────────────────────────────────────────────── 2. жёсткий запрет (floor) */

test('floor (жёсткий запрет): плашка + только «Понятно, скрыть» и «Сообщить участнику»', () => {
  const seen = [];
  const card = approvalCard(Object.assign({}, APPROVAL, { floor: true }), {
    onDecide: async () => ({ ok: true }),
    onAction: async (_a, action) => { seen.push(action); return { ok: true }; },
  });
  const body = text(card);
  assert.match(body, /Запрещено политикой безопасности Hermes — разрешить нельзя/);
  // W26 §1: разрешать по-прежнему нельзя, но тупика нет — у человека есть два действия.
  assert.equal(byAttr(card, 'data-action', 'approval-once').length, 0);
  assert.equal(byAttr(card, 'data-action', 'approval-always').length, 0);
  assert.equal(byAttr(card, 'data-action', 'approval-deny').length, 0);
  assert.equal(byAttr(card, 'data-action', 'approval-ack').length, 1);
  assert.equal(byAttr(card, 'data-action', 'approval-hint_agent').length, 1);
  // В счётчики «Нужен ваш ответ» жёсткий запрет не входит: это информация.
  assert.equal(isWaiting({ floor: true, state: 'pending' }), false);
  assert.equal(floorApprovals([{ id: 'x', floor: true, state: 'pending' }]).length, 1);
  assert.equal(approvalState({ floor: true, state: 'pending' }), 'pending');
});

test('grantable:false (execute_code) — «нельзя штатно», но три действенных рычага', async () => {
  const seen = [];
  const card = approvalCard(Object.assign({}, APPROVAL, {
    kind: 'execute_code', grantable: false,
    description: 'execute_code — произвольный код на Python',
    note: 'такую блокировку снять нельзя — нужен другой способ',
  }), {
    onDecide: async () => ({ ok: true }),
    onAction: async (_a, action, extra) => { seen.push([action, extra]); return { ok: true }; },
  });
  const body = text(card);
  assert.match(body, /Разрешить нельзя штатно: Hermes не сверяет разрешения для такого запуска/);
  assert.match(body, /то же через терминал/);
  assert.equal(canDecide({ grantable: false }), false);
  assert.equal(canDecide({ grantable: true }), true);
  assert.equal(canDecide({ floor: true }), false);
  // W26 §1: «нельзя штатно» — не тупик: подсказать / доверить / закрыть.
  assert.equal(approvalDeadEnd({ grantable: false }), DEAD_ENDS.workaround);
  assert.equal(isWaiting({ grantable: false, state: 'pending' }), true);
  assert.deepEqual(approvalActionsFor({ grantable: false }).map((x) => x.action),
    ['hint_agent', 'trust_profile', 'dismiss']);
  assert.equal(byAttr(card, 'data-action', 'approval-hint_agent').length, 1);
  assert.equal(byAttr(card, 'data-action', 'approval-dismiss').length, 1);
  // «Доверять…» сначала спрашивает срок: без срока доверие не выдаётся.
  const trust = byAttr(card, 'data-action', 'approval-trust_profile')[0];
  trust.dispatch('click', { type: 'click', target: trust });
  assert.equal(byAttr(card, 'data-ttl', 'card').length, 1);
  assert.equal(byAttr(card, 'data-ttl', 'min30').length, 1);
  assert.equal(byAttr(card, 'data-ttl', 'forever').length, 1);
  const forever = byAttr(card, 'data-ttl', 'forever')[0];
  forever.dispatch('click', { type: 'click', target: forever });
  await tick();
  assert.deepEqual(seen[0], ['trust_profile', { ttl: 'forever' }]);
});

/* ─────────────────────────────────────────────── 3. три решения и подписи */

test('три решения: точные подписи, у «всегда» — подсказка про отзыв', () => {
  const card = approvalCard(APPROVAL, { onDecide: async () => ({ ok: true }) });
  const labels = buttons(card).map((b) => text(b).trim());
  // W27-E2 §1: четвёртая кнопка — «Не актуально» (убрать пункт без решения,
  // с возвратом). Сами решения по-прежнему три и с теми же словами.
  assert.deepEqual(labels, ['Разрешить один раз', 'Разрешать всегда', 'Запретить', 'Не актуально']);
  assert.deepEqual(APPROVAL_DECISIONS.map((d) => d.label),
    [APPROVALS_RU.once, APPROVALS_RU.always, APPROVALS_RU.deny]);
  const always = byAttr(card, 'data-decision', 'always')[0];
  assert.match(always.getAttribute('title'), /Настройках → Разрешения агентов/);
  assert.match(text(card), /отозвать можно в Настройках → Разрешения агентов/i);
});

/* ─────────────────────────────── 4. двойное нажатие и итог после решения */

test('двойное нажатие — один запрос; кнопки выключены; после ответа виден итог', async () => {
  let calls = 0;
  let release = null;
  const onDecide = () => {
    calls += 1;
    return new Promise((resolve) => { release = resolve; });
  };
  const card = approvalCard(APPROVAL, { onDecide });
  const once = byAttr(card, 'data-decision', 'once')[0];
  once.dispatch('click', { type: 'click', target: once });
  // Второй клик, пока запрос в полёте, — не второй запрос.
  once.dispatch('click', { type: 'click', target: once });
  const again = byAttr(card, 'data-decision', 'once')[0];
  again.dispatch('click', { type: 'click', target: again });
  await tick();
  assert.equal(calls, 1, 'двойное нажатие не создаёт дублей');
  for (const btn of buttons(card)) assert.equal(btn.disabled, true, 'кнопки выключены на время');
  assert.equal(card.getAttribute('aria-busy'), 'true');

  release({ ok: true, approval: { id: APPROVAL.id, state: 'granted_once' }, restarted: true });
  await tick();
  const body = text(card);
  assert.match(body, /Разрешено один раз/);
  assert.match(body, /карточка перезапущена/);
  assert.equal(byAttr(card, 'data-action', 'approval-once').length, 0, 'кнопки решения убраны');
  assert.equal(card.getAttribute('data-ap-state'), 'granted_once');
});

test('«Разрешать всегда»: итог говорит, где отозвать', async () => {
  const card = approvalCard(APPROVAL, {
    onDecide: async () => ({ ok: true, grant: { scope: 'always', pattern_key: 'script execution via heredoc' } }),
  });
  const btn = byAttr(card, 'data-decision', 'always')[0];
  btn.dispatch('click', { type: 'click', target: btn });
  await tick();
  await tick();
  const body = text(card);
  assert.match(body, /Разрешено всегда/);
  assert.match(body, /Настройках → Разрешения агентов/);
  assert.equal(card.getAttribute('data-ap-state'), 'granted_always');
});

/* ────────────────────────────────────────────── 5. ошибки человеческим языком */

test('409 already_decided: тихая перерисовка, без красной плашки и без кода', async () => {
  const card = approvalCard(APPROVAL, {
    onDecide: async () => { const e = new Error('http_409'); e.status = 409; e.code = 'already_decided'; throw e; },
  });
  const btn = byAttr(card, 'data-decision', 'deny')[0];
  btn.dispatch('click', { type: 'click', target: btn });
  await tick();
  await tick();
  const body = text(card);
  // W27-E2 §1: 409 — не ошибка человека, а устаревший экран: ни «красной» строки
  // с текстом ошибки, ни кнопок решений (решать нечего), состояние уточняется.
  assert.match(body, /Уже решено — обновляю состояние/);
  assert.ok(!/Это разрешение уже решено — страница обновится/.test(body),
    'красной плашки больше нет');
  assert.equal(card.getAttribute('data-ap-conflict'), 'already_decided');
  assert.equal(card.getAttribute('data-ap-state'), 'unknown');
  assert.ok(!/http_409/.test(body), 'служебный код наружу не попал');
  assert.equal(approvalErrorText({ status: 422 }), 'Разрешить нельзя: команда запрещена политикой '
    + 'безопасности Hermes или не найдена программа Hermes этого участника');
  assert.equal(approvalErrorText({ status: 409 }), 'Это разрешение уже решено — '
    + 'страница обновится и покажет итог');
});

test('итог решения по «Запретить» — словами, а не «deny»', () => {
  assert.match(decisionResultText('deny', {}), /Запрещено — участник будет искать другой способ/);
  assert.match(decisionResultText('once', { approval: { expires_at: 1794942400 } }), /Разрешено один раз/);
});

/* ─────────────────────────────────────────────────────── 6. счётчики */

test('счётчики: разрешение — отдельный вид дела', () => {
  const summary = inboxSummary({
    questions: [{ id: 'q1' }],
    decisions: [],
    blocked: [],
    approvals: [{ id: 'ap_1', state: 'pending' }, { id: 'ap_2', state: 'pending' }],
  });
  assert.equal(summary.total, 3);
  assert.equal(summary.approvals, 2);
  assert.equal(summary.text, '1 вопрос · 2 разрешения');
  assert.equal(inboxCount({ approvals: [{}, {}] }), 2);
  assert.equal(approvalsWaitingLabel(2), '2 разрешения ждут вашего решения');
  assert.equal(approvalsWaitingLabel(1), '1 разрешение ждёт вашего решения');
});

/* ───────────────────────────────────────── 7. сбор ждущих из деталей */

test('collectApprovals: только ждущие, без дублей, с карточкой', () => {
  const details = [
    { id: 't_1', title: 'Отчёт по упаковке', approvals: [
      { id: 'ap_1', state: 'pending', at: 20 },
      { id: 'ap_2', state: 'granted_always', at: 10 },
    ] },
    { id: 't_2', title: 'Договор', approvals: [
      { id: 'ap_1', state: 'pending', at: 20 },
      { id: 'ap_3', state: 'pending', at: 5 },
    ] },
  ];
  const out = collectApprovals(details);
  assert.deepEqual(out.map((a) => a.id), ['ap_3', 'ap_1'], 'по времени, без дублей');
  assert.equal(out[1].request_title, 'Отчёт по упаковке');
  assert.equal(pendingApprovals([{ state: 'denied' }, { state: 'pending' }]).length, 1);
});

/* ─────────────────────────────────── 8. настройки: список и отзыв */

test('настройки: строка разрешения; отзыв — одно тело {profile, pattern_key}', async () => {
  const posted = [];
  const api = {
    get: async () => ({ items: [
      { profile: 'anna-assistant', member_title: 'Анна', pattern_key: 'script execution via heredoc',
        description: 'запуск скрипта через heredoc', scope: 'always', granted_at: 1794930000,
        source: 'room' },
      { profile: 'lev-analyst', member_title: 'Лев', pattern_key: 'script execution via -e/-c flag',
        description: 'выполнение кода python -c', scope: 'once', granted_at: 1794936000,
        expires_at: 1794942400, source: 'manual' },
    ] }),
    post: async (url, body) => { posted.push({ url, body }); return { ok: true }; },
  };
  const ui = createGrantsSection({
    api, roomId: 'cafe', toast: () => {}, confirm: async () => true,
  });
  await tick();
  await tick();
  const body = text(ui.element);
  assert.match(body, /Анна/);
  assert.match(body, /запуск скрипта через heredoc/);
  assert.match(body, /всегда/);
  assert.match(body, /один раз/);
  assert.match(body, /до \d\d:\d\d/);
  assert.match(body, /выдано здесь/);
  assert.deepEqual(grantsSummary([{ scope: 'always' }, { scope: 'once' }]),
    { total: 2, always: 1, once: 1, text: '2 разрешения: 1 — всегда, 1 — один раз' });

  const revoke = byAttr(ui.element, 'data-action', 'approval-revoke')[0];
  revoke.dispatch('click', { type: 'click', target: revoke });
  await tick();
  await tick();
  await tick();
  assert.equal(posted.length, 1, 'один отзыв — один запрос');
  assert.equal(posted[0].url, '/api/approvals/revoke');
  assert.deepEqual(posted[0].body,
    { profile: 'anna-assistant', pattern_key: 'script execution via heredoc', room: 'cafe' });
});

test('настройки: пустой список — объяснение, а не пустое место', async () => {
  const ui = createGrantsSection({ api: { get: async () => ({ items: [] }) }, roomId: 'cafe' });
  await tick();
  await tick();
  assert.match(text(ui.element), /Пока нет ни одного разрешения/);
});

/* ─────────────────────────── 9. связка со страницей: тело действия (контракт) */

test('решение со страницы запроса уходит контрактным телом', async () => {
  const sent = [];
  const api = { post: async (url, body) => { sent.push({ url, body }); return { ok: true, approval: { state: 'granted_once' } }; } };
  // W26-D2: стор теперь общий на запрос и правит объект разрешения на месте —
  // фикстуру берём копией, чтобы соседние проверки видели её нетронутой.
  resetRequestStores();
  const one = Object.assign({}, APPROVAL);
  const ui = createApprovals({
    ctx: { api, toast: () => {} },
    rid: 't_5c8e4a02',
    roomId: 'cafe',
    detail: { id: 't_5c8e4a02', approvals: [one] },
    refresh: async () => {},
  });
  assert.equal(ui.pending().length, 1);
  assert.equal(ui.pendingCards().length, 1);
  await ui.decide(one, 'once');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, '/api/requests/t_5c8e4a02/actions');
  assert.deepEqual(sent[0].body, {
    action: 'approval', approval_id: 'ap_7f3a12cd', decision: 'once',
  });
  assert.deepEqual(approvalBody(APPROVAL, 'always'),
    { action: 'approval', approval_id: 'ap_7f3a12cd', decision: 'always' });
});

test('блок на странице запроса: ждущие сверху, решённые — свёрнуто', () => {
  resetRequestStores();
  const ui = createApprovals({
    ctx: { api: { post: async () => ({ ok: true }) }, toast: () => {} },
    rid: 't_5c8e4a02',
    roomId: 'cafe',
    detail: { id: 't_5c8e4a02', approvals: [
      APPROVAL,
      { id: 'ap_done01', state: 'granted_always', kind: 'terminal', command: 'ls',
        description: 'чтение каталога', member_title: 'Лев', floor: false },
    ] },
    refresh: async () => {},
  });
  const block = ui.buildRequestBlock();
  const body = text(block);
  assert.match(body, /Разрешения агентам/);
  assert.equal(byAttr(block, 'data-approval', 'ap_7f3a12cd').length, 1);
  assert.equal(byAttr(block, 'data-approval', 'ap_done01').length, 1);
  assert.match(body, /Решённые: 1 задача/);
});

/* ─────────── W28-B (дефект 6): «нельзя штатно» с честным состоянием сервера ─────── */

test('состояние workaround: кнопки на месте, а не «состояние неизвестно»', async () => {
  // Сервер отдаёт `workaround` своим словом (раньше экран превращал его в `unknown`,
  // и кнопки «Подсказать…»/«Доверять…» не рисовались вовсе — дефект 6).
  const seen = [];
  const card = approvalCard(Object.assign({}, APPROVAL, {
    id: 'ap_wk28b01', kind: 'execute_code', grantable: false, state: 'workaround',
    description: 'execute_code — произвольный код на Python',
  }), {
    onDecide: async () => ({ ok: true }),
    onAction: async (_a, action, extra) => { seen.push([action, extra]); return { ok: true }; },
  });
  assert.equal(approvalState({ state: 'workaround' }), 'workaround');
  const body = text(card);
  assert.doesNotMatch(body, /состояние неизвестно/);
  assert.equal(byAttr(card, 'data-action', 'approval-hint_agent').length, 1);
  assert.equal(byAttr(card, 'data-action', 'approval-trust_profile').length, 1);
  const hint = byAttr(card, 'data-action', 'approval-hint_agent')[0];
  hint.dispatch('click', { type: 'click', target: hint });
  await tick();
  assert.deepEqual(seen[0], ['hint_agent', {}]);
});

test('ошибка действия остаётся в карточке строкой с причиной и «Повторить»', async () => {
  let fail = true;
  const sent = [];
  const card = approvalCard(Object.assign({}, APPROVAL, {
    id: 'ap_err28b01', kind: 'execute_code', grantable: false, state: 'workaround',
    description: 'execute_code — произвольный код на Python',
  }), {
    onDecide: async () => ({ ok: true }),
    onAction: async (_a, action, extra) => {
      sent.push([action, extra]);
      if (fail) {
        const err = new Error('Неизвестное действие');
        err.status = 400;
        err.payload = { error: { code: 'bad_request', message: 'Неизвестное действие' } };
        throw err;
      }
      return { ok: true, approval: { id: 'ap_err28b01', state: 'hinted' } };
    },
  });
  const hint = byAttr(card, 'data-action', 'approval-hint_agent')[0];
  hint.dispatch('click', { type: 'click', target: hint });
  await tick(5);
  // Причина видна В САМОЙ карточке (не только тостом) и у неё есть «Повторить».
  const line = byAttr(card, 'data-ap-error', '1');
  assert.equal(line.length, 1, 'причина ошибки не осталась в карточке');
  assert.match(text(line[0]), /Неизвестное действие/);
  const retry = byAttr(card, 'data-action', 'approval-retry');
  assert.equal(retry.length, 1, 'у причины нет кнопки «Повторить»');
  assert.match(text(retry[0]), /Повторить/);
  // Повтор не глотается: то же действие уходит снова, и после успеха строка уходит.
  fail = false;
  retry[0].dispatch('click', { type: 'click', target: retry[0] });
  await tick(5);
  assert.deepEqual(sent.map((x) => x[0]), ['hint_agent', 'hint_agent']);
  assert.equal(byAttr(card, 'data-ap-error', '1').length, 0,
    'после успешного повтора причина осталась висеть');
  assert.match(text(card), /Подсказка отправлена участнику/);
});
