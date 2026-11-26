/**
 * views/proposal-card.js — карточка «Предложение руководителя» (волна 30, пакет C).
 *
 * Дефект 16: руководитель научился сам решать, разовая задача или объёмная, и
 * присылать предложение веток-подзадач, — а человеку это некуда было нажать.
 * Здесь клиентская часть контракта §2:
 *
 *   kind=single — «Согласен» / «Не согласен» / «Свой вариант» (текст + диктовка);
 *   kind=big    — список веток, у каждой «Да» / «Нет» / «Свой вариант»
 *                 (текст + диктовка), а внизу общие «Принять всё предложение» /
 *                 «Отклонить» / «Свой вариант» (текст + диктовка → «Отправить»).
 *
 * После применения — ссылки на созданные задачи (`branches[].task_id`), чтобы
 * человек видел результат, а не «готово» без адреса. Подзадачи создаёт сервер:
 * интерфейс только отправляет решение и показывает, что получилось.
 *
 * Правила: только `h()` (никакого `innerHTML` из данных), кнопки фиксированной
 * ширины (CSS), тёмная/светлая тема через токены, пустое состояние объясняет
 * «что будет после кнопки».
 *
 * Маршруты (контракт §2):
 *   POST /api/requests/{id}/proposal/decision {branch_id|null, decision, text?}
 *   POST /api/requests/{id}/proposal/resolve  {action, text?}
 *
 * Модуль чистый в части модели (`proposalModel`, `branchMeta`, `proposalTotals`,
 * `taskHref`) — это проверяют node-тесты; вид (`buildProposalCard`) работает на DOM.
 */

import { h, timeAgo, shortId } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { personName, firstLine } from './request-util.js';

/* ─────────────────────────────────────────────────────────── слова и статусы */

/** Состояния предложения (`state` контракта §2) — словами для человека. */
export const PROPOSAL_STATE_RU = {
  pending: { label: 'ждёт вашего решения', tone: 'warn' },
  approved: { label: 'принято', tone: 'ok' },
  rejected: { label: 'отклонено', tone: 'muted' },
  custom: { label: 'свой вариант отправлен', tone: 'info' },
  applied: { label: 'задачи созданы', tone: 'ok' },
};

/** Решение по одиночной задаче. */
export const SINGLE_DECISION_RU = {
  pending: 'ждёт вашего решения',
  agree: 'вы согласны',
  disagree: 'вы не согласны',
  custom: 'свой вариант',
};

/** Решение по ветке. */
export const BRANCH_DECISION_RU = {
  pending: 'ждёт решения',
  yes: 'да',
  no: 'нет',
  custom: 'свой вариант',
};

/** Что значит каждое действие — подсказки кнопок (title). */
export const PROPOSAL_HINTS = {
  agree: 'Разовая задача: команда выполнит её в одной задаче, итог придёт в «Свод».',
  disagree: 'Работа в одной задаче не подходит — напишите свой вариант.',
  yes: 'Ветка войдёт в план: после «Принять всё» по ней создадут подзадачу.',
  no: 'Ветка не войдёт в план — подзадачу по ней не создадут.',
  acceptAll: 'Все ветки — «да», затем создаются подзадачи. Подзадачи появятся только после этого нажатия.',
  rejectAll: 'Ничего не создаётся: предложение уходит в отклонённые, работа остаётся как есть.',
  customAll: 'Напишите, как разложить работу иначе — руководитель пересоберёт предложение по вашему тексту.',
  apply: 'Создать подзадачи по веткам, где вы сказали «да» или написали свой вариант.',
};

const KIND_RU = { single: 'разовая задача', big: 'объёмная задача' };

/* ────────────────────────────────────────────────────────────────── модель */

/** Строка без лишних пробелов; пустое — пустая строка. */
function txt(value, limit) {
  const s = String(value === null || value === undefined ? '' : value).trim();
  if (!s) return '';
  return limit && s.length > limit ? s.slice(0, limit) + '…' : s;
}

/**
 * Предложение из детали задачи — в одном виде для вида и тестов.
 *
 * @param {object|null} proposal `detail.proposal`
 * @returns {{present:boolean, id:string, kind:'single'|'big', kindLabel:string,
 *            state:string, summary:string, author:string, createdAt:number,
 *            single:{decision:string, text:string},
 *            branches:Array<{id:string,title:string,goal:string,assignee:string,
 *                            questions:string[], order:number, decision:string,
 *                            customText:string, taskId:string}>,
 *            resolution:{action:string, text:string, at:number}|null,
 *            applied:boolean, canDecide:boolean, canApply:boolean}}
 */
export function proposalModel(proposal) {
  const p = proposal && typeof proposal === 'object' ? proposal : null;
  if (!p) {
    return {
      present: false, id: '', kind: 'single', kindLabel: KIND_RU.single, state: '', summary: '',
      author: '', createdAt: 0, single: { decision: 'pending', text: '' }, branches: [],
      resolution: null, applied: false, canDecide: false, canApply: false,
    };
  }
  const kind = String(p.kind || 'single') === 'big' ? 'big' : 'single';
  const state = String(p.state || 'pending');
  const rawBranches = Array.isArray(p.branches) ? p.branches : [];
  const branches = rawBranches
    .filter((b) => b && typeof b === 'object')
    .slice()
    .sort((a, b) => (Number(a.order) || 0) - (Number(b.order) || 0))
    .map((b, i) => ({
      id: txt(b.id, 24) || 'b' + (i + 1),
      title: txt(b.title, 200) || 'Ветка ' + (i + 1),
      goal: txt(b.goal, 600),
      assignee: txt(b.assignee, 64),
      questions: Array.isArray(b.questions)
        ? b.questions.map((q) => txt(q, 300)).filter(Boolean).slice(0, 6) : [],
      order: Number(b.order) || i + 1,
      decision: ['yes', 'no', 'custom'].includes(String(b.decision))
        ? String(b.decision) : 'pending',
      customText: txt(b.custom_text, 1200),
      taskId: txt(b.task_id, 40),
    }));
  const single = p.single && typeof p.single === 'object' ? p.single : {};
  const res = p.resolution && typeof p.resolution === 'object' ? p.resolution : null;
  const applied = state === 'applied' || branches.some((b) => Boolean(b.taskId));
  const finished = ['approved', 'rejected', 'custom', 'applied'].includes(state);
  return {
    present: true,
    id: txt(p.id, 40),
    kind,
    kindLabel: KIND_RU[kind],
    state,
    summary: txt(p.summary, 1200),
    author: txt(p.author, 64),
    createdAt: Number(p.created_at) || 0,
    single: {
      decision: ['agree', 'disagree', 'custom'].includes(String(single.decision))
        ? String(single.decision) : 'pending',
      text: txt(single.text, 1200),
    },
    branches,
    resolution: res ? {
      action: txt(res.action, 24), text: txt(res.text, 1200), at: Number(res.at) || 0,
    } : null,
    applied,
    // Решение принимается, пока предложение не применено и не отклонено.
    canDecide: !finished && !applied,
    // Создавать подзадачи имеет смысл, когда есть что создавать.
    canApply: !applied && state !== 'rejected' && kind === 'big'
      && branches.some((b) => b.decision === 'yes' || b.decision === 'custom'),
  };
}

/** Статус предложения чипом: подпись и тон. */
export function proposalStateMeta(proposal) {
  const m = proposalModel(proposal);
  if (!m.present) return { label: '', tone: 'muted' };
  const meta = PROPOSAL_STATE_RU[m.state] || { label: m.state, tone: 'muted' };
  return { label: meta.label, tone: meta.tone };
}

/** Статус ветки: подпись, тон и «что дальше». */
export function branchMeta(branch) {
  const b = branch && typeof branch === 'object' ? branch : {};
  const decision = ['yes', 'no', 'custom'].includes(String(b.decision))
    ? String(b.decision) : 'pending';
  const meta = {
    pending: { label: BRANCH_DECISION_RU.pending, tone: 'muted' },
    yes: { label: BRANCH_DECISION_RU.yes, tone: 'ok' },
    no: { label: BRANCH_DECISION_RU.no, tone: 'muted' },
    custom: { label: BRANCH_DECISION_RU.custom, tone: 'info' },
  }[decision];
  return Object.assign({ decision }, meta);
}

/** Сколько веток решено и сколько задач уже создано — для строки итога. */
export function proposalTotals(proposal) {
  const m = proposalModel(proposal);
  const count = (value) => m.branches.filter((b) => b.decision === value).length;
  return {
    branches: m.branches.length,
    yes: count('yes'),
    no: count('no'),
    custom: count('custom'),
    pending: count('pending'),
    tasks: m.branches.filter((b) => Boolean(b.taskId)).length,
    applied: m.applied,
  };
}

/** Адрес задачи комнаты (подзадачи или родителя) — один формат на всё приложение. */
export function taskHref(roomId, taskId) {
  const room = txt(roomId, 64);
  const id = txt(taskId, 40);
  if (!id) return '';
  if (!room) return '#/request/' + encodeURIComponent(id);
  return '#/room/' + encodeURIComponent(room) + '/request/' + encodeURIComponent(id);
}

/** «Подзадачи» — строки блока у родительской задачи (`detail.project.children`). */
export function projectChildren(project) {
  const p = project && typeof project === 'object' ? project : {};
  const list = Array.isArray(p.children) ? p.children : [];
  return list.filter((c) => c && typeof c === 'object').map((c) => ({
    id: txt(c.id, 40),
    title: txt(c.title, 200) || txt(c.id, 40),
    status: txt(c.status, 24),
    stage: txt(c.stage, 24),
  }));
}

/** Стадия подзадачи словами (контракт §3) — те же подписи, что на доске задач. */
export const STAGE_RU = {
  new: 'Новая',
  working: 'В работе',
  needs_you: 'Ждёт вас',
  review: 'На проверке',
  done_system: 'Готово',
  accepted: 'Принята',
  archived: 'В архиве',
  scouting: 'Разведка',
  overview_ready: 'Обзор сдан',
};

/** Подпись стадии: `stage`, иначе `status`, иначе «Новая». */
export function stageLabel(stage, status) {
  const key = txt(stage, 24) || txt(status, 24);
  return STAGE_RU[key] || (key ? key : STAGE_RU.new);
}

/* ───────────────────────────────────────────────────────────────────── вид */

/**
 * Карточка предложения.
 *
 * @param {object} opts
 * @param {object|null} opts.proposal `detail.proposal`
 * @param {string} opts.roomId комната (для ссылок на созданные задачи)
 * @param {string} opts.requestId id задачи (адрес API)
 * @param {object} [opts.api] `{post}` — как `ctx.api`
 * @param {Function} [opts.toast]
 * @param {Function} [opts.refresh] перечитать страницу
 * @param {Map<string,string>} [opts.keepValues] черновики «своего варианта»
 * @returns {HTMLElement|null} `null`, если предложения нет
 */
export function buildProposalCard(opts = {}) {
  const o = opts || {};
  const m = proposalModel(o.proposal);
  if (!m.present) return null;
  ensureCss('css/view-proposal.css');
  const api = o.api || null;
  const toast = typeof o.toast === 'function' ? o.toast : () => {};
  const refresh = typeof o.refresh === 'function' ? o.refresh : null;
  const keep = o.keepValues instanceof Map ? o.keepValues : new Map();
  const roomId = txt(o.roomId, 64);
  const requestId = txt(o.requestId, 40);
  const stateMeta = proposalStateMeta(o.proposal);

  /**
   * Один путь отправки: решение по ветке/одиночной задаче или общее решение.
   * `busy` на кнопке — чтобы двойное нажатие не создавало вторую подзадачу.
   */
  async function post(path, body, btn, okText) {
    if (!api || typeof api.post !== 'function') {
      toast('Нет связи с приложением — действие не отправлено', 'danger');
      return null;
    }
    if (btn && btn.disabled) return null;
    if (btn) btn.disabled = true;
    try {
      const res = await api.post('/api/requests/' + encodeURIComponent(requestId) + '/proposal/' + path, body);
      toast(okText, 'ok');
      if (refresh) await refresh({ force: true });
      return res || {};
    } catch (err) {
      toast((err && err.message) || 'Не удалось отправить решение', 'danger');
      if (btn) btn.disabled = false;
      return null;
    }
  }

  function decideBranch(branch, decision, text, btn) {
    const body = { branch_id: String(branch.id), decision: String(decision) };
    const label = String(decision) === 'yes' ? 'Ветка принята: ' + firstLine(branch.title, 60)
      : String(decision) === 'no' ? 'Ветка отклонена: ' + firstLine(branch.title, 60)
        : 'Свой вариант по ветке отправлен';
    if (text) body.text = String(text);
    return post('decision', body, btn, label);
  }

  function decideSingle(decision, text, btn) {
    const body = { branch_id: null, decision: String(decision) };
    if (text) body.text = String(text);
    const label = String(decision) === 'agree' ? 'Согласны: выполним в одной задаче'
      : String(decision) === 'disagree' ? 'Не согласны — руководитель увидит ваш ответ'
        : 'Свой вариант отправлен руководителю';
    return post('decision', body, btn, label);
  }

  /** «Принять всё предложение»: все ветки «да», затем создание подзадач. */
  async function acceptAll(btn) {
    if (btn && btn.disabled) return;
    const first = await post('resolve', { action: 'accept_all' }, btn, 'Все ветки приняты');
    if (!first) return;
    // Вторая половина одного нажатия: кнопка снова активна, иначе `post` принял
    // бы её за двойное нажатие и создание подзадач не ушло бы вовсе.
    if (btn) btn.disabled = false;
    const applied = await post('resolve', { action: 'apply' }, btn, 'Подзадачи созданы — они ниже в «Подзадачах»');
    if (btn && !applied) btn.disabled = false;
  }

  /* ── шапка ─────────────────────────────────────────────────────────── */
  const card = h('section', {
    class: 'card proposal' + (m.applied ? ' proposal--applied' : ''),
    'data-proposal': m.id || 'proposal',
    'data-proposal-kind': m.kind,
    'data-proposal-state': m.state,
    'data-key': 'blk-proposal',
    'aria-label': 'Предложение руководителя',
  });

  card.appendChild(h('div', { class: 'proposal__head' },
    h('div', { class: 'proposal__title-row' },
      icon('git-branch', { size: 14, className: 'proposal__icon' }),
      h('h2', { class: 'proposal__title' }, 'Предложение руководителя'),
      h('span', {
        class: 'chip chip--' + stateMeta.tone + ' proposal__state',
        'data-proposal-chip': m.state,
        title: 'Состояние предложения: ' + stateMeta.label,
      }, stateMeta.label)),
    h('div', { class: 'proposal__meta small muted' },
      h('span', null, m.kindLabel),
      m.author ? h('span', null, ' · ' + personName(m.author)) : null,
      m.createdAt ? h('span', null, ' · ' + timeAgo(m.createdAt)) : null)));

  if (m.summary) card.appendChild(h('p', { class: 'proposal__summary' }, m.summary));

  /* ── одиночная задача ─────────────────────────────────────────────── */
  if (m.kind === 'single') {
    const decided = m.single.decision !== 'pending';
    const block = h('div', { class: 'proposal__single', 'data-proposal-single': m.single.decision });
    block.appendChild(h('p', { class: 'proposal__lead' },
      m.canDecide
        ? 'Это разовая задача — предлагаю выполнить её в одной задаче. Работа распределится между участниками, итог придёт в «Свод».'
        : 'Разовая задача: ' + (SINGLE_DECISION_RU[m.single.decision] || '')));
    if (m.single.text) {
      block.appendChild(h('div', { class: 'proposal__answer' },
        h('span', { class: 'proposal__cap' }, 'Ваш ответ'),
        h('div', { class: 'proposal__answer-text' }, m.single.text)));
    }
    if (m.canDecide) {
      const row = h('div', { class: 'proposal__actions', role: 'group', 'aria-label': 'Решение по задаче' });
      const agree = h('button', {
        type: 'button', class: 'btn btn--primary proposal__act', 'data-action': 'proposal-agree',
        'aria-pressed': m.single.decision === 'agree' ? 'true' : 'false',
        title: PROPOSAL_HINTS.agree,
      }, icon('thumbsup', { size: 12 }), 'Согласен');
      agree.addEventListener('click', () => decideSingle('agree', '', agree));
      const disagree = h('button', {
        type: 'button', class: 'btn btn--secondary proposal__act', 'data-action': 'proposal-disagree',
        'aria-pressed': m.single.decision === 'disagree' ? 'true' : 'false',
        title: PROPOSAL_HINTS.disagree,
      }, icon('thumbsdown', { size: 12 }), 'Не согласен');
      disagree.addEventListener('click', () => decideSingle('disagree', '', disagree));
      row.appendChild(agree);
      row.appendChild(disagree);
      row.appendChild(customToggle({
        key: 'pr:' + (m.id || 'single'),
        placeholder: 'Как сделать иначе: текстом или диктовкой…',
        onSend: (text, btn) => decideSingle('custom', text, btn),
        sendLabel: 'Отправить',
      }));
      block.appendChild(row);
      if (decided) {
        block.appendChild(h('div', { class: 'proposal__decided small muted' },
          'Ваше решение: ' + (SINGLE_DECISION_RU[m.single.decision] || '') + '. Пока руководитель не ответит, можно изменить его кнопкой ниже.'));
      }
    }
    card.appendChild(block);
    return card;
  }

  /* ── объёмная задача: ветки ───────────────────────────────────────── */
  const totals = proposalTotals(o.proposal);
  card.appendChild(h('div', { class: 'proposal__branches-head' },
    h('div', { class: 'proposal__lead' },
      m.applied
        ? 'Подзадачи созданы — они ниже, в блоке «Подзадачи».'
        : 'Это объёмная задача. Руководитель предлагает разбить её на ' + totals.branches
          + ' ' + plural(totals.branches, ['подзадачу', 'подзадачи', 'подзадач'])
          + ' — подзадачи появятся ТОЛЬКО после вашего согласия.'),
    h('div', { class: 'proposal__tally small muted', 'data-proposal-tally': String(totals.branches) },
      h('span', null, 'да: ' + totals.yes),
      h('span', null, ' · нет: ' + totals.no),
      h('span', null, ' · свой вариант: ' + totals.custom),
      totals.pending ? h('span', null, ' · без решения: ' + totals.pending) : null)));

  const list = h('ol', { class: 'proposal__list', 'data-proposal-branches': String(totals.branches) });
  for (const branch of m.branches) {
    list.appendChild(branchRow(branch));
  }
  card.appendChild(list);

  // Итог применения: ссылки на созданные задачи.
  const created = m.branches.filter((b) => b.taskId);
  if (created.length) {
    card.appendChild(h('div', { class: 'proposal__created', 'data-proposal-created': String(created.length) },
      h('div', { class: 'proposal__cap' }, 'Созданные подзадачи'),
      h('ul', { class: 'proposal__created-list' },
        created.map((b) => h('li', { class: 'proposal__created-item' },
          h('a', {
            class: 'proposal__created-link', href: taskHref(roomId, b.taskId),
            title: 'Открыть подзадачу: ' + b.title,
          }, icon('arrow-right', { size: 11 }), b.title),
          h('span', { class: 'small muted tnum' }, ' · ' + shortId(b.taskId)))))));
  }

  // Общие кнопки снизу — как в задании: принять всё / отклонить / свой вариант.
  if (m.canDecide) {
    const foot = h('div', { class: 'proposal__footer', role: 'group', 'aria-label': 'Общее решение по предложению' });
    const all = h('button', {
      type: 'button', class: 'btn btn--primary proposal__foot-act', 'data-action': 'proposal-accept-all',
      title: PROPOSAL_HINTS.acceptAll,
    }, icon('check-all', { size: 12 }), 'Принять всё предложение');
    all.addEventListener('click', () => acceptAll(all));
    const no = h('button', {
      type: 'button', class: 'btn btn--secondary proposal__foot-act', 'data-action': 'proposal-reject-all',
      title: PROPOSAL_HINTS.rejectAll,
    }, icon('close', { size: 12 }), 'Отклонить');
    no.addEventListener('click', () => post('resolve', { action: 'reject_all' }, no,
      'Предложение отклонено — ничего не создаём'));
    foot.appendChild(all);
    foot.appendChild(no);
    foot.appendChild(customToggle({
      key: 'pr:' + (m.id || 'all'),
      placeholder: 'Как разложить работу иначе: текстом или диктовкой…',
      onSend: (text, btn) => post('resolve', { action: 'custom', text }, btn,
        'Свой вариант отправлен: руководитель пересоберёт предложение'),
      sendLabel: 'Отправить',
    }));
    card.appendChild(foot);
    // «Создать подзадачи» — если человек уже расставил «да» по веткам.
    if (m.canApply) {
      const applyBtn = h('button', {
        type: 'button', class: 'btn btn--ghost proposal__apply', 'data-action': 'proposal-apply',
        title: PROPOSAL_HINTS.apply,
      }, icon('play', { size: 12 }), 'Создать подзадачи по выбранным веткам');
      applyBtn.addEventListener('click', () => post('resolve', { action: 'apply' }, applyBtn,
        'Подзадачи созданы — они ниже в «Подзадачах»'));
      card.appendChild(h('div', { class: 'proposal__apply-row' }, applyBtn));
    }
  } else if (m.state === 'rejected') {
    card.appendChild(h('div', { class: 'proposal__closed small muted' },
      icon('close', { size: 11 }),
      h('span', null, m.resolution && m.resolution.text
        ? 'Отклонено: ' + m.resolution.text
        : 'Предложение отклонено — подзадачи не создавались.')));
  }

  return card;

  /* ── ветка предложения ────────────────────────────────────────────── */
  function branchRow(branch) {
    const meta = branchMeta(branch);
    const row = h('li', {
      class: 'proposal__branch proposal__branch--' + meta.decision,
      'data-branch': branch.id,
      'data-branch-decision': meta.decision,
    });
    row.appendChild(h('div', { class: 'proposal__branch-head' },
      h('span', { class: 'proposal__branch-order tnum' }, String(branch.order) + '.'),
      h('div', { class: 'proposal__branch-title' }, branch.title),
      h('span', {
        class: 'chip chip--' + meta.tone + ' proposal__branch-state',
        title: 'Решение по ветке: ' + meta.label,
      }, meta.label)));
    if (branch.goal) row.appendChild(h('div', { class: 'proposal__branch-goal' }, branch.goal));
    const metaLine = h('div', { class: 'proposal__branch-meta small muted' });
    if (branch.assignee) metaLine.appendChild(h('span', null, 'исполнитель: ' + personName(branch.assignee)));
    if (branch.taskId) {
      metaLine.appendChild(h('span', null, (branch.assignee ? ' · ' : '') + 'задача: '));
      metaLine.appendChild(h('a', {
        class: 'proposal__branch-task', href: taskHref(roomId, branch.taskId),
        title: 'Открыть созданную подзадачу',
      }, shortId(branch.taskId)));
    }
    if (metaLine.childNodes && metaLine.childNodes.length) row.appendChild(metaLine);
    if (branch.questions.length) {
      row.appendChild(h('details', { class: 'proposal__branch-questions' },
        h('summary', { class: 'proposal__cap-sm' },
          icon('chevron-right', { size: 11 }),
          'Вопросы к ветке: ' + String(branch.questions.length)),
        h('ul', { class: 'proposal__qlist' },
          branch.questions.map((q) => h('li', { class: 'proposal__q' }, q)))));
    }
    if (branch.customText) {
      row.appendChild(h('div', { class: 'proposal__branch-custom' },
        h('span', { class: 'proposal__cap' }, 'Ваш вариант'),
        h('div', { class: 'proposal__branch-custom-text' }, branch.customText)));
    }
    if (m.canDecide) {
      const acts = h('div', { class: 'proposal__branch-actions', role: 'group',
        'aria-label': 'Решение по ветке: ' + branch.title });
      const yes = h('button', {
        type: 'button', class: 'btn btn--primary btn--sm proposal__branch-act',
        'data-action': 'proposal-branch-yes', 'data-branch-id': branch.id,
        'aria-pressed': meta.decision === 'yes' ? 'true' : 'false',
        title: PROPOSAL_HINTS.yes,
      }, icon('check', { size: 11 }), 'Да');
      yes.addEventListener('click', () => decideBranch(branch, 'yes', '', yes));
      const no = h('button', {
        type: 'button', class: 'btn btn--secondary btn--sm proposal__branch-act',
        'data-action': 'proposal-branch-no', 'data-branch-id': branch.id,
        'aria-pressed': meta.decision === 'no' ? 'true' : 'false',
        title: PROPOSAL_HINTS.no,
      }, icon('close', { size: 11 }), 'Нет');
      no.addEventListener('click', () => decideBranch(branch, 'no', '', no));
      acts.appendChild(yes);
      acts.appendChild(no);
      acts.appendChild(customToggle({
        key: 'pr:' + branch.id,
        placeholder: 'Свой вариант по этой ветке: текстом или диктовкой…',
        sendLabel: 'Отправить',
        onSend: (text, btn) => decideBranch(branch, 'custom', text, btn),
      }));
      row.appendChild(acts);
    }
    return row;
  }

  /* ── «Свой вариант»: кнопка → поле (диктовка ставится сама) ────────── */
  function customToggle(cfg) {
    const key = String(cfg.key || 'pr:custom');
    const wrap = h('div', { class: 'proposal__custom' });
    const openBtn = h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm proposal__custom-open',
      'data-action': 'proposal-custom', 'aria-expanded': 'false',
      title: PROPOSAL_HINTS.customAll,
    }, icon('edit', { size: 11 }), 'Свой вариант');
    const ta = h('textarea', {
      class: 'textarea proposal__custom-input', rows: 2,
      placeholder: String(cfg.placeholder || 'Свой вариант: текстом или диктовкой…'),
      'aria-label': 'Свой вариант ответа на предложение',
      'data-keep': key,
      value: keep.get(key) || '',
    });
    ta.addEventListener('input', () => keep.set(key, ta.value));
    const send = h('button', {
      type: 'button', class: 'btn btn--primary btn--sm proposal__custom-send',
      'data-action': 'proposal-custom-send',
      title: 'Отправить свой вариант руководителю',
    }, icon('send', { size: 11 }), String(cfg.sendLabel || 'Отправить'));
    const form = h('div', { class: 'proposal__custom-form', hidden: true },
      h('div', { class: 'proposal__custom-row' }, ta, send));
    const submit = () => {
      const text = String(ta.value || '').trim();
      if (!text) {
        if (typeof ta.focus === 'function') ta.focus();
        toast('Напишите свой вариант — пустое поле отправить нельзя', 'warn');
        return;
      }
      cfg.onSend(text, send);
    };
    send.addEventListener('click', submit);
    ta.addEventListener('keydown', (ev) => {
      if (ev && ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        submit();
      }
    });
    openBtn.addEventListener('click', () => {
      const open = form.hasAttribute('hidden');
      if (open) form.removeAttribute('hidden');
      else form.setAttribute('hidden', '');
      openBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        try { ta.focus({ preventScroll: true }); } catch { /* старый браузер */ }
      }
    });
    wrap.appendChild(openBtn);
    wrap.appendChild(form);
    return wrap;
  }
}

/* ───────────────────────────────────── блок «Подзадачи» у родителя ──────── */

/**
 * Блок «Подзадачи» у родительской задачи (`detail.project.children`).
 * Название, стадия, ссылка — и ничего лишнего: это навигация, а не отчёт.
 *
 * @param {object} opts
 * @param {{parent_id?:string, children?:Array<object>}|null} opts.project
 * @param {string} opts.roomId
 * @returns {HTMLElement|null}
 */
export function buildProjectBlock(opts = {}) {
  const o = opts || {};
  const children = projectChildren(o.project);
  if (!children.length) return null;
  ensureCss('css/view-proposal.css');
  const n = children.length;
  return h('section', {
    class: 'card proposal-project', 'data-project-children': String(n), 'data-key': 'blk-project',
    'aria-label': 'Подзадачи проекта',
  },
  h('div', { class: 'proposal__head' },
    h('div', { class: 'proposal__title-row' },
      icon('type-hierarchy', { size: 14, className: 'proposal__icon' }),
      h('h2', { class: 'proposal__title' }, 'Подзадачи'),
      h('span', { class: 'chip chip--muted tnum' }, String(n)))),
  h('ul', { class: 'proposal-project__list' },
    children.map((child) => h('li', { class: 'proposal-project__item', 'data-child': child.id },
      h('a', {
        class: 'proposal-project__link', href: taskHref(o.roomId, child.id),
        title: 'Открыть подзадачу',
      }, child.title),
      h('span', {
        class: 'chip chip--muted proposal-project__stage', 'data-child-stage': child.stage || child.status,
      }, stageLabel(child.stage, child.status))))));
}

/**
 * Строка «Часть проекта: <родитель>» у подзадачи (`detail.project.parent_id`).
 * @param {object} opts
 * @param {{parent_id?:string}|null} opts.project
 * @param {string} opts.roomId
 * @returns {HTMLElement|null}
 */
export function buildParentNote(opts = {}) {
  const o = opts || {};
  const parentId = txt(o.project && o.project.parent_id, 40);
  if (!parentId) return null;
  ensureCss('css/view-proposal.css');
  return h('div', { class: 'proposal-parent small muted', 'data-project-parent': parentId },
    icon('type-hierarchy', { size: 11 }),
    h('span', null, 'Часть проекта: '),
    h('a', {
      class: 'proposal-parent__link', href: taskHref(o.roomId, parentId),
      title: 'Открыть родительскую задачу',
    }, shortId(parentId)));
}

/** Склонение внутри модуля (без импорта лишнего). */
function plural(n, forms) {
  const num = Math.abs(Number(n) || 0);
  const one = forms[0] || '';
  const few = forms[1] || one;
  const many = forms[2] || few;
  const tail100 = num % 100;
  const tail10 = num % 10;
  if (tail100 >= 11 && tail100 <= 14) return many;
  if (tail10 === 1) return one;
  if (tail10 >= 2 && tail10 <= 4) return few;
  return many;
}

export default {
  buildProposalCard, buildProjectBlock, buildParentNote, proposalModel, proposalStateMeta,
  branchMeta, proposalTotals, projectChildren, stageLabel, taskHref,
};
