/**
 * request-answer.js — блок «Свод команды»: текст ответа, версии, ожидание,
 * ошибка, пустое состояние; действия «На доработку» и «Копировать».
 *
 * Фабрика createAnswer(R) получает общий контекст страницы из request.js
 * и возвращает { buildAnswer }. Поведение прежнее: вынесено из request.js.
 */

import { h, fmtDateTime, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { fileUrl } from '../api.js';
import { emptyState, skeleton, mdBlock, copyText, segmented, buildDialog } from '../ui.js';
import { enhanceAll } from '../dictation-auto.js';
import {
  personName, profileName, criticOf, synthesisOf, versionsOf, latestVersion, wrapTables, devDetails,
} from './request-util.js';
import { failureInfo } from './request-parts-model.js';
import { countNoun } from '../text-ru.js';
import {
  KICK_LABEL, KICK_HINT, kickStateFromTree, kickUsefulness, kickWhyNot, kickPanel, kickNow,
} from './kick-panel.js';

/** Фраза, если человек запускает доработку без своего замечания (только «учти ответы»). */
export const REVISE_NO_NOTE = 'Доработать свод с учётом моих ответов на вопросы и уточнений в диалоге';

/** Причина версии по-русски, когда руководитель не написал свою (W17-S §3). */
const REASON_RU = {
  initial: 'первый свод',
  note: 'доработка по вашему замечанию',
  answers: 'после ваших ответов и уточнений',
  revision: 'доработка',
};

/** Короткая строка без переводов строк; длинное — с «…». */
export function shortLine(text, max = 120) {
  const one = String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim();
  if (one.length <= max) return one;
  return one.slice(0, Math.max(1, max - 1)).trimEnd() + '…';
}

/** Узел дерева по id задачи (для metadata версии). */
function nodeOf(d, taskId) {
  if (!taskId) return null;
  return (d && Array.isArray(d.tree) ? d.tree : []).find((n) => n && String(n.id) === String(taskId)) || null;
}

/** `metadata.changes` узла: массив строк (W17-S §3, запасной путь без реестра). */
function metaChanges(node) {
  const raw = node && node.metadata ? node.metadata.changes : null;
  const list = Array.isArray(raw) ? raw : (typeof raw === 'string' ? [raw] : []);
  return list.map((item) => (item && typeof item === 'object' ? item.text : item))
    .map((item) => String(item || '').trim()).filter(Boolean).slice(0, 10);
}

/** Служебные подписи, которые не стоит повторять в «Версия N — …». */
const GENERIC_NOTES = new Set(['Доработка после замечаний', 'Учтены замечания критика', 'Принято критиком']);

/**
 * Подпись версии: «Версия 2 — доработка по вашему замечанию: «…»».
 * @param {object} entry запись истории версий
 * @param {object} d detail
 * @returns {string}
 */
export function versionLabel(entry, d) {
  const e = entry || {};
  const v = Number(e.version || 0);
  if (!v) return '';
  const node = nodeOf(d, e.task_id);
  const leaderReason = shortLine(node && node.metadata ? node.metadata.version_reason : '', 140);
  const rawNote = shortLine(e.note, 140);
  const note = GENERIC_NOTES.has(rawNote) ? '' : rawNote;
  const reason = String(e.reason || '');
  let why = '';
  if (e.kind === 'initial' || reason === 'initial') why = REASON_RU.initial;
  else if (reason === 'note' && note) why = `${REASON_RU.note}: «${note}»`;
  else if (reason === 'answers') why = `${REASON_RU.answers}${e.answers_count ? ` (${e.answers_count})` : ''}`;
  else if (leaderReason) why = leaderReason;
  else if (note) why = `${REASON_RU.revision}: «${note}»`;
  else why = REASON_RU.revision;
  return `Версия ${v} — ${why}`;
}

/** Заголовки разделов Markdown — для запасного «что изменилось». */
function sectionTitles(md) {
  return String(md || '').split('\n')
    .map((line) => (/^#{1,3}\s+\S/.test(line) ? line.replace(/^#{1,3}\s+/, '').trim() : ''))
    .filter(Boolean).slice(0, 40);
}

/**
 * «Что изменилось» версии: список руководителя, иначе разница по разделам.
 * @param {object} entry
 * @param {object} d
 * @param {string} prevText текст предыдущей версии
 * @param {string} curText текст этой версии
 * @returns {string[]}
 */
export function versionChanges(entry, d, prevText, curText) {
  const e = entry || {};
  if (Array.isArray(e.changes) && e.changes.length) return e.changes.map(String).filter(Boolean);
  const own = metaChanges(nodeOf(d, e.task_id));
  if (own.length) return own;
  const before = sectionTitles(prevText);
  const after = sectionTitles(curText);
  const added = after.filter((t) => !before.includes(t));
  const removed = before.filter((t) => !after.includes(t));
  const out = [];
  if (added.length) out.push('Добавлены разделы: ' + added.join('; '));
  if (removed.length) out.push('Убраны разделы: ' + removed.join('; '));
  if (!out.length && String(prevText || '').trim() !== String(curText || '').trim()) {
    out.push('Текст переписан — откройте сравнение с предыдущей версией');
  }
  return out;
}

/** Момент выпуска последней версии свода (0 — свода ещё нет). */
export function latestVersionAt(s) {
  const list = versionsOf(s);
  return list.length ? Number(list[list.length - 1].at || 0) : 0;
}

/** «3 ответа на вопросы и 3 уточнения» — человеческий счёт без повторов числа. */
export function digestCountText(digest) {
  const d = digest || {};
  const parts = [];
  const a = (d.answers || []).length;
  const n = (d.notes || []).length;
  if (a) parts.push(countNoun(a, ['ответ на вопрос', 'ответа на вопросы', 'ответов на вопросы']));
  if (n) parts.push(countNoun(n, ['уточнение', 'уточнения', 'уточнений']));
  return parts.length ? parts.join(' и ') : 'новых ответов и уточнений нет';
}

/** Сообщения Михаила в ленте (уточнения), позже `at`. */
function maximNotes(d, at) {
  const list = Array.isArray(d && d.thread) ? d.thread : (Array.isArray(d && d.timeline) ? d.timeline : []);
  return list.filter((m) => m && !m.pending
    && String(m.author || m.actor || '') === 'mikhail'
    && !['event', 'system'].includes(String(m.kind || ''))
    && Number(m.at || 0) > at && String(m.text || '').trim());
}

/**
 * Дайджест «что появилось после версии» — ровно то, что уйдёт в карточку доработки.
 * Правило то же, что в `contract.revision_digest`: ответы на вопросы, уточнения в
 * диалоге, решения, убранные шаги, всё — строго после выпуска версии.
 * @param {object} d detail
 * @param {object} s synthesis
 * @returns {{at:number, version:number, answers:Array, notes:Array, decisions:Array, steps:Array, count:number}}
 */
export function revisionDigest(d, s) {
  const at = latestVersionAt(s || {});
  const list = versionsOf(s || {});
  const version = list.length ? Number(list[list.length - 1].version || 0) : Number((s && s.version) || 0);
  const answers = [];
  for (const q of (d && Array.isArray(d.open_questions) ? d.open_questions : [])) {
    if (!q || !q.answered || !String(q.answer || '').trim()) continue;
    const when = Number(q.answered_at || 0);
    if (when <= at) continue;
    answers.push({ id: q.id || null, question: String(q.text || ''), answer: String(q.answer || ''),
      at: when, label: String(q.answer_label || '') });
  }
  const notes = maximNotes(d, at).map((m) => ({ at: Number(m.at || 0), text: String(m.text || '') }));
  const decisions = (d && Array.isArray(d.decisions) ? d.decisions : [])
    .filter((x) => x && String(x.text || '').trim() && Number(x.at || 0) > at
      && String(x.status || 'proposed') !== 'rejected')
    .map((x) => ({ text: String(x.text), status: String(x.status || 'proposed'), at: Number(x.at || 0) }));
  const steps = (d && Array.isArray(d.next_steps) ? d.next_steps : [])
    .filter((x) => x && String(x.text || '').trim()
      && (['dismissed', 'cancelled'].includes(String(x.state || '')) || x.conflicts_with_answer))
    .map((x) => ({ text: String(x.text), label: String(x.state_label || 'убран') }));
  return { at, version, answers, notes, decisions, steps,
    count: answers.length + notes.length };
}


export function createAnswer(R) {
  const { ctx, rid, root, state, render, runAction, cloneRequest, decompose } = R;

  /* ── доработка свода: одна кнопка, предпросмотр отправки (W17-S §1–§2) ── */

  /** Строка предпросмотра «ключ — значение». */
  function previewRow(key, value, extra) {
    return h('li', { class: 'req-revise__row' },
      h('span', { class: 'req-revise__key' }, key),
      h('span', { class: 'req-revise__value', title: value || null }, value || '—'),
      extra || null);
  }

  /**
   * Окно «Доработать свод»: замечание + раскрывающийся предпросмотр того, что
   * получит руководитель. Возвращает текст замечания ('' — без замечания) или
   * null, если окно закрыли.
   * @param {string} [initial]
   * @returns {Promise<string|null>}
   */
  function openReviseDialog(initial = '') {
    const d = R.detail || {};
    const s = synthesisOf(d);
    const digest = revisionDigest(d, s);
    const cur = Number(s.version || digest.version || 0);
    const curText = answerText(s, currentVersion(s));

    return new Promise((resolve) => {
      let done = false;
      let shell = null;
      const finish = (val) => {
        if (done) return;
        done = true;
        if (shell) shell.close();
        resolve(val);
      };

      const ta = h('textarea', {
        class: 'textarea req-revise__note', rows: 5, id: 'revise-note',
        placeholder: 'Например: добавь срок подачи в Роспатент и уточни MOQ у трёх поставщиков',
        'aria-label': 'Что исправить в своде',
      });
      ta.value = String(initial || '');

      const noteValue = h('span', { class: 'req-revise__value' });
      const syncNote = () => {
        const text = String(ta.value || '').trim();
        noteValue.textContent = text ? shortLine(text, 160) : 'пока не написали — можно оставить пустым';
      };
      ta.addEventListener('input', syncNote);
      syncNote();

      const rows = [
        previewRow('Запрос', shortLine(d.text, 160) || '—'),
        previewRow(cur ? `Свод версии ${cur}` : 'Свод команды',
          shortLine(String(curText || '').replace(/[#*|>-]/g, ' '), 160) || '—'),
        h('li', { class: 'req-revise__row' },
          h('span', { class: 'req-revise__key' }, 'Ваше замечание'), noteValue),
      ];

      const detailItems = [];
      for (const a of digest.answers) {
        detailItems.push(h('li', null,
          h('span', { class: 'req-revise__q' }, `Вопрос: ${shortLine(a.question, 130)}`),
          h('span', { class: 'req-revise__a' }, `Ваш ответ: ${shortLine(a.answer, 150)}`)));
      }
      for (const n of digest.notes) {
        detailItems.push(h('li', null, h('span', { class: 'req-revise__a' },
          `Уточнение: ${shortLine(n.text, 170)}`)));
      }
      for (const x of digest.decisions) {
        detailItems.push(h('li', null, h('span', { class: 'req-revise__a' },
          `Решение: ${shortLine(x.text, 150)}${x.status === 'accepted' ? ' (принято)' : ''}`)));
      }
      for (const x of digest.steps) {
        detailItems.push(h('li', null, h('span', { class: 'req-revise__a' },
          `Шаг: ${shortLine(x.text, 150)} (${x.label})`)));
      }

      const countKey = digest.count
        ? h('li', { class: 'req-revise__row' },
          h('span', { class: 'req-revise__key' }, 'Ваши ответы и уточнения'),
          h('span', { class: 'req-revise__value req-revise__value--count' },
            `${digestCountText(digest)} после версии ${cur || 1}`))
        : h('li', { class: 'req-revise__row' },
          h('span', { class: 'req-revise__key' }, 'Ваши ответы и уточнения'),
          h('span', { class: 'req-revise__value' }, `после версии ${cur || 1} новых нет`));
      rows.push(countKey);

      const body = h('div', { class: 'req-revise' },
        h('label', { class: 'req-revise__label', for: 'revise-note' }, 'Что исправить'),
        ta,
        h('div', { class: 'dialog__hint' },
          'Ctrl+Enter — отправить, Esc — закрыть. Можно не писать: тогда руководитель доработает по вашим ответам и уточнениям.'),
        h('details', { class: 'req-revise__preview', open: true },
          h('summary', null, icon('eye', { size: 12 }), h('span', null, 'Что получит руководитель')),
          h('ul', { class: 'req-revise__list' }, ...rows),
          detailItems.length
            ? h('details', { class: 'req-revise__more' },
              h('summary', null, `Подробно: ${countNoun(detailItems.length, ['пункт', 'пункта', 'пунктов'])}`),
              h('ul', { class: 'req-revise__items' }, ...detailItems))
            : null,
          h('div', { class: 'req-revise__honest' },
            'Весь диалог целиком не пересылается — руководитель получит запрос, свод, ваше замечание и перечисленное выше.')));

      const cancelBtn = h('button', {
        class: 'btn btn--secondary', type: 'button',
        onclick: () => finish(null),
      }, 'Отмена');

      const submit = () => finish(String(ta.value || '').trim());
      const submitBtn = h('button', {
        class: 'btn btn--primary', type: 'button', 'data-action': 'revise-submit',
        onclick: submit,
      }, icon('comment-draft', { size: 13 }), 'Отправить на доработку');

      shell = buildDialog({
        title: 'Доработать свод',
        body,
        buttons: [cancelBtn, submitBtn],
        initialFocus: ta,
        onCancel: () => finish(null),
        onKey: (ev) => {
          if (!ev || ev.key !== 'Enter' || ev.isComposing) return false;
          if (!(ev.ctrlKey || ev.metaKey)) return false;
          if (typeof ev.preventDefault === 'function') ev.preventDefault();
          submit();
          return true;
        },
      });
      try { enhanceAll(body); } catch (err) { console.warn('[request] микрофон в окне доработки:', err); }
    });
  }

  /**
   * Запуск доработки свода — единственный путь (W17-S §1). Без замечания
   * отправляется понятная фраза «учти мои ответы», чтобы карточка не осталась пустой.
   * @param {{note?: string}} [opts]
   */
  async function revise(opts = {}) {
    const note = await openReviseDialog(opts.note || '');
    if (note === null) return;
    const own = String(note || '').trim();
    const text = own || REVISE_NO_NOTE;
    const res = await runAction('revise', { text }, {
      toast: own ? 'Руководитель доработает свод по вашему замечанию'
        : 'Руководитель доработает свод с учётом ваших ответов',
    });
    if (res) state.version = null;
  }

  async function copyAnswer() {
    const s = synthesisOf(R.detail);
    const text = answerText(s, currentVersion(s));
    if (!text) return;
    try {
      await copyText(text);
      ctx.toast('Ответ скопирован', 'ok');
    } catch {
      ctx.toast('Не удалось скопировать — выделите текст вручную', 'danger');
    }
  }

  /* ── «Проверить доску сейчас» (W13-F §8) ───────────────────── */

  let kickBusy = false;

  /** Запустить проход диспетчера по клику и показать итог панелью, не тостом. */
  async function runKick(btn) {
    if (kickBusy) return;
    kickBusy = true;
    if (btn) { btn.disabled = true; btn.classList.add('is-busy'); }
    state.kick = await kickNow(ctx.api, rid);
    kickBusy = false;
    if (btn) { btn.disabled = false; btn.classList.remove('is-busy'); }
    await R.refresh({ force: true });
    render();
  }

  function dismissKick() {
    state.kick = null;
    render();
  }

  /** Подписи исполнителей в итоге: имя из состава комнаты, иначе профиль. */
  const kickLabelOf = (profile) => profileName(profile, ctx.room) || String(profile || '');

  /** Итог прохода — сразу под заголовком «Свод команды», в любом состоянии. */
  function kickBlock() {
    if (!state.kick) return null;
    return h('div', { style: { marginBottom: 'var(--space-3)' } },
      kickPanel(state.kick, {
        labelOf: kickLabelOf,
        scopeIds: new Set((R.detail.tree || []).map((n) => String(n && n.id)).filter(Boolean)),
        onClose: dismissKick,
      }));
  }

  /** Кнопка «Проверить доску сейчас» — только когда есть что запускать. */
  function kickButton() {
    return h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm', title: KICK_HINT, 'data-kick': 'button',
      onclick: (e) => runKick(e.currentTarget),
    }, icon('zap', { size: 12 }), KICK_LABEL);
  }

  /* ── ответ ─────────────────────────────────────────────────── */

  function currentVersion(s) {
    const latest = latestVersion(s);
    return state.version && state.version < latest ? state.version : latest;
  }

  function answerText(s, version) {
    const list = versionsOf(s);
    const latest = latestVersion(s);
    if (!version || version >= latest) return String(s.text_md || '');
    const entry = list.find((v) => v.version === version);
    if (!entry) return '';
    if (entry.text_md) return String(entry.text_md);
    const node = (R.detail.tree || []).find((t) => t && t.id === entry.task_id);
    return node && node.summary_md ? String(node.summary_md) : '';
  }

  function versionSwitch(s, cur) {
    const list = versionsOf(s);
    if (list.length < 2) return null;
    try {
      return segmented({
        options: list.map((v) => ({ value: String(v.version), label: 'v' + v.version })),
        value: String(cur),
        onchange: (val) => {
          const num = Number(val);
          const latest = latestVersion(s);
          state.version = num >= latest ? null : num;
          render();
        },
      });
    } catch (err) {
      console.warn('[request] segmented:', err);
      return null;
    }
  }

  /* ── «Что изменилось» и сравнение версий (W17-S §3) ─────────── */

  /** Простой поабзацный diff: добавленное и удалённое (для «сравнить с версией N»). */
  function diffParagraphs(before, after) {
    const left = String(before || '').split(/\n{2,}/).map((x) => x.trim()).filter(Boolean).slice(0, 120);
    const right = String(after || '').split(/\n{2,}/).map((x) => x.trim()).filter(Boolean).slice(0, 120);
    const n = left.length;
    const m = right.length;
    const dp = [];
    for (let i = 0; i <= n; i += 1) dp.push(new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        dp[i][j] = left[i] === right[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const out = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (left[i] === right[j]) { i += 1; j += 1; continue; }
      if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ kind: 'del', text: left[i] }); i += 1; }
      else { out.push({ kind: 'add', text: right[j] }); j += 1; }
    }
    while (i < n) { out.push({ kind: 'del', text: left[i] }); i += 1; }
    while (j < m) { out.push({ kind: 'add', text: right[j] }); j += 1; }
    return out.slice(0, 200);
  }

  /** Список «Что изменилось» под версией. */
  function changesBlock(entry, d, prevText, curText) {
    if (!entry || Number(entry.version) < 2) return null;
    const items = versionChanges(entry, d, prevText, curText);
    if (!items.length) return null;
    return h('div', { class: 'req-answer__changes' },
      h('div', { class: 'req-answer__changes-head' },
        icon('diff', { size: 12 }), h('span', null, 'Что изменилось')),
      h('ul', { class: 'req-answer__changes-list' },
        ...items.map((text) => h('li', { title: text }, shortLine(text, 240)))));
  }

  /** Сравнение с предыдущей версией (по клику): добавленное и удалённое. */
  function compareBlock(prevText, curText, prevVersion, curVersion) {
    const lines = diffParagraphs(prevText, curText);
    if (!lines.length) {
      return h('div', { class: 'req-answer__diff' },
        h('div', { class: 'small muted' }, 'Различий по абзацам нет.'));
    }
    return h('div', { class: 'req-answer__diff' },
      h('div', { class: 'req-answer__diff-head' },
        `Сравнение версии ${prevVersion} и версии ${curVersion}: красное — было, зелёное — стало`),
      ...lines.map((line) => h('div', {
        class: ['req-answer__diff-line', line.kind === 'add' ? 'is-add' : 'is-del'],
      }, h('span', { class: 'req-answer__diff-sign' }, line.kind === 'add' ? '+' : '−'),
        h('span', null, shortLine(line.text, 300)))));
  }

  /** Отметка «свод устарел»: после этой версии есть ваши ответы/уточнения (W17-S §4). */
  function staleBlock(d, s) {
    const digest = revisionDigest(d, s);
    if (!digest.count) return null;
    const a = digest.answers.length;
    const n = digest.notes.length;
    const parts = [];
    if (a) parts.push(countNoun(a, ['ваш ответ', 'ваших ответа', 'ваших ответов']));
    if (n) parts.push(countNoun(n, ['уточнение', 'уточнения', 'уточнений']));
    const what = parts.join(' и ');
    return h('div', { class: 'req-answer__stale', 'data-stale': String(digest.count) },
      icon('warning', { size: 14 }),
      h('span', { class: 'req-answer__stale-text' },
        `После версии ${digest.version} есть ${what} — в своде это ещё не учтено.`),
      h('button', {
        type: 'button', class: 'btn btn--secondary btn--sm', 'data-action': 'revise-stale',
        disabled: Boolean(d && d.archived),
        onclick: () => revise({ note: '' }),
      }, icon('comment-draft', { size: 12 }), 'Доработать с учётом ответов'));
  }

  function buildAnswer() {
    const d = R.detail;
    const s = synthesisOf(d);
    const list = versionsOf(s);
    const latest = latestVersion(s);
    const cur = currentVersion(s);
    const isOld = list.length > 1 && cur > 0 && cur < latest;
    const ready = s.state === 'ready' && Boolean(s.text_md || isOld);
    const c = criticOf(d);

    const card = h('section', { class: 'card req-answer', id: 'answer', 'aria-labelledby': 'answer-title' });
    card.append(h('div', { class: 'req-answer__head' },
      h('h2', { class: 'req-answer__title', id: 'answer-title' }, 'Свод команды'),
      s.legacy ? h('span', { class: 'chip chip--muted', title: 'Ответ получен из старого формата metadata' }, 'старый формат') : null,
      h('div', { class: 'spacer' }),
      versionSwitch(s, cur)));

    const kickBox = kickBlock();
    if (kickBox) card.append(kickBox);

    if (d.status === 'failed') {
      const f = failureInfo(d);
      card.append(h('div', { class: 'req-empty' }, emptyState({
        icon: 'error',
        title: 'Ответ не получился',
        text: f.text,
        action: { label: 'Повторить', variant: 'primary', onclick: () => runAction('retry', {}, { toast: 'Перезапуск отправлен' }) },
      })));
      // Сырой лог — только по клику, для разработчика (D7).
      const dev = devDetails(f.details);
      if (dev) card.appendChild(dev);
      return card;
    }

    if (ready) {
      const entry = list.find((v) => v.version === cur) || null;
      const prevEntry = list.find((v) => v.version === Number(cur) - 1) || null;
      const text = answerText(s, cur);
      const prevText = prevEntry ? answerText(s, prevEntry.version) : '';
      const metaBits = [];
      if (entry && entry.author) metaBits.push(personName(entry.author));
      if (entry && entry.at) metaBits.push(fmtDateTime(entry.at));

      // W17-S §3: версия подписана причиной, а не просто номером.
      const verRow = h('div', { class: 'req-answer__ver' },
        h('span', { class: 'req-answer__ver-text', title: metaBits.join(' · ') || null },
          versionLabel(entry, d)),
        entry && entry.verdict === 'approve' ? h('span', { class: 'chip chip--ok' }, 'принято критиком') : null,
        entry && entry.verdict === 'revise' ? h('span', { class: 'chip chip--warn' }, 'было на доработке') : null,
        h('div', { class: 'spacer' }),
        metaBits.length ? h('span', { class: 'req-answer__ver-meta' }, metaBits.join(' · ')) : null);
      card.append(verRow);

      card.append(h('div', { class: 'req-answer__vcap' },
        h('span', null, isOld ? `Показана версия ${cur} из ${latest}` : `Показана версия ${cur} (последняя)`),
        latest > 1 ? h('span', { class: 'req-answer__vcap-sep' }, ' · ') : null,
        latest > 1 ? h('button', {
          type: 'button', class: 'btn btn--link btn--sm', 'data-action': 'compare',
          onclick: () => { state.compare = !state.compare; render(); },
        }, state.compare ? 'скрыть сравнение' : `сравнить с версией ${Math.max(1, Number(cur) - 1)}`) : null));

      if (state.compare && prevEntry) {
        card.append(compareBlock(prevText, text, prevEntry.version, cur));
      }

      if (isOld) {
        card.append(h('div', { class: 'req-answer__old' },
          h('span', { class: 'grow' }, `Показана версия ${cur} из ${latest}: это не последний ответ.`),
          h('button', {
            type: 'button', class: 'btn btn--link btn--sm',
            onclick: () => { state.version = null; render(); },
          }, 'К актуальной')));
      }

      // «Что изменилось» относительно предыдущей версии.
      const changes = changesBlock(entry, d, prevText, text);
      if (changes) card.append(changes);

      // W17-S §4: после выпуска версии есть ваши ответы/уточнения — свод устарел.
      const stale = isOld ? null : staleBlock(d, s);
      if (stale) card.append(stale);

      if (text) {
        const body = h('div', { class: 'req-answer__body' }, mdBlock(text, { fileUrl }));
        wrapTables(body);
        card.append(body);
      } else {
        card.append(h('div', { class: 'req-answer__body' },
          h('div', { class: 'banner banner--warn' },
            icon('warning', { size: 14 }),
            h('span', { class: 'banner__text' }, 'Текст этой версии не найден в задачах запроса — возможно, задача удалена.'))));
      }

      if (!isOld && (c.state === 'reviewing' || d.status === 'reviewing')) {
        card.insertBefore(
          h('div', { class: 'req-answer__notice' },
            icon('sync', { size: 12, className: 'codicon-modifier-spin' }),
            `Критик проверяет этот ответ — раунд ${c.round || 1} из ${c.max_rounds || 1}.`),
          card.children[1] || null,
        );
      } else if (!isOld && d.status === 'synthesizing') {
        card.insertBefore(
          h('div', { class: 'req-answer__notice' },
            icon('sync', { size: 12, className: 'codicon-modifier-spin' }),
            'Руководитель комнаты дорабатывает ответ — скоро появится новая версия.'),
          card.children[1] || null,
        );
      }

      card.append(h('div', { class: 'req-answer__foot' },
        h('button', {
          type: 'button', class: 'btn btn--secondary', disabled: Boolean(d.archived),
          title: 'Руководитель получит запрос, этот свод, ваше замечание и ваши ответы после этой версии',
          'data-action': 'revise', onclick: () => revise(),
        }, icon('comment-draft', { size: 13 }), 'Доработать свод'),
        h('button', { type: 'button', class: 'btn btn--ghost', onclick: copyAnswer },
          icon('copy', { size: 13 }), 'Копировать'),
        h('a', {
          class: 'btn btn--ghost', href: `/api/export/request/${encodeURIComponent(rid)}.md`,
          download: '', title: 'Скачать ответ с шагами и деревом работ',
        }, icon('markdown', { size: 13 }), 'Скачать .md'),
        h('span', { class: 'req-answer__hint' }, entry && entry.at ? 'обновлено ' + timeAgo(entry.at) : '')));
      return card;
    }

    if (s.state === 'pending') {
      const active = (d.tree || [])
        .filter((t) => t && (t.status === 'running' || t.status === 'review') && t.assignee)
        .map((t) => profileName(t.assignee, ctx.room));
      const who = [...new Set(active)].filter(Boolean);
      const queued = d.status === 'queued';
      const waiting = d.status === 'needs_you';
      const openCount = (d.open_questions || [])
        .filter((q) => q && !q.answered && (q.for || 'mikhail') === 'mikhail').length;

      const body = h('div', { class: 'req-pending' },
        h('div', { class: 'req-pending__title' },
          h('span', { class: 'req-pending__dot', 'aria-hidden': 'true' }),
          queued ? 'Запрос ждёт разбора' : waiting ? 'Ждёт твоего ответа' : 'Команда работает над ответом'));
      // Сам вопрос живёт в «Вопросах к тебе» — здесь только якорь к нему (D5).
      if (waiting && openCount) {
        body.append(h('a', {
          class: 'req-pending__jump',
          href: '#',
          'data-jump': 'questions',
          title: 'Перейти к вопросам и ответить',
          onclick: (e) => {
            if (e && typeof e.preventDefault === 'function') e.preventDefault();
            const target = root.querySelector('[data-questions]');
            if (!target) return;
            if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'start', behavior: 'smooth' });
            const field = target.querySelector('textarea');
            if (field && typeof field.focus === 'function') field.focus({ preventScroll: true });
          },
        }, `${countNoun(openCount, ['вопрос ждёт', 'вопроса ждут', 'вопросов ждут'])} вашего ответа ↓`));
      } else if (waiting) {
        body.append(h('div', { class: 'req-pending__line' }, 'Команда ждёт вашего ответа.'));
      } else {
        // Прогресс уже показан в «Ходе работы» — здесь только то, чего там нет.
        body.append(h('div', { class: 'req-pending__line' }, queued
          ? 'Руководитель комнаты ещё не разложил запрос по команде. Обычно это происходит само, когда движок запущен.'
          : (who.length ? 'Сейчас: ' + who.join(', ') : 'Движок раздаёт задачи команде.')));
        body.append(h('div', { class: 'req-pending__skel' }, skeleton(4)));
        const kstate = kickStateFromTree(d.tree);
        const kuse = kickUsefulness(kstate);
        body.append(h('div', { class: 'row', style: { marginTop: '4px' } },
          queued
            ? h('button', { type: 'button', class: 'btn btn--secondary btn--sm', onclick: (e) => decompose(e.currentTarget) },
              icon('split-horizontal', { size: 12 }), 'Разложить сейчас')
            : (kuse.useful
              ? kickButton()
              : h('span', { class: 'small muted' }, kickWhyNot(kstate)))));
      }

      card.append(body);
      return card;
    }

    // state none / missing: завершено без текста
    card.append(h('div', { class: 'req-empty' }, emptyState({
      icon: 'comment-discussion',
      title: 'Ответа нет',
      text: 'Запрос завершён, но текст ответа не сохранился. Можно повторить запрос тем же текстом или посмотреть задачи в дереве работ.',
      action: { label: 'Повторить', onclick: cloneRequest },
    })));
    return card;
  }

  return { buildAnswer };
}
