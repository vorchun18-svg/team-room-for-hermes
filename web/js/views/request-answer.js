/**
 * request-answer.js — блок «Свод»: текст ответа, версии, ожидание,
 * ошибка, пустое состояние; действия «На доработку» и «Копировать».
 *
 * Фабрика createAnswer(R) получает общий контекст страницы из request.js
 * и возвращает { buildAnswer }. Поведение прежнее: вынесено из request.js.
 */

import { h } from '../util.js';
import { icon } from '../icons.js';
import { fileUrl } from '../api.js';
import { emptyState, skeleton, mdBlock, copyText, buildDialog, ensureCss } from '../ui.js';
import { enhanceAll } from '../dictation-auto.js';
import { svodParts, headlineSplit } from '../svod-format.js';
import {
  personName, profileName, criticOf, synthesisOf, versionsOf, latestVersion, wrapTables, devDetails,
} from './request-util.js';
import { failureInfo } from './request-parts-model.js';
import { openArtifactPath } from './request-parts.js';
import { createRetryButton } from './retry-button.js';
import { createMarks } from './notice-state.js';
import { countNoun, SVOD_RU } from '../text-ru.js';
import { termHelp } from '../term-help.js';
import {
  KICK_LABEL, KICK_HINT, kickStateFromTree, kickUsefulness, kickWhyNot, kickPanel, kickNow,
} from './kick-panel.js';
import { exportGroup } from './request-export.js';
// W25-L2 (R5 §7.2): «Что изменилось» — сворачиваемый блок (общий компонент У-1).
import { createSection, sectionDefaults } from './section-collapse.js';
// W29B-Q4: различия версий считаются в браузере (слова/абзацы) — без вызовов модели.
import { diffParagraphs, diffCounts, countsText, changeIndexes } from '../svod-diff.js';
// W29B-Q4: «Второе мнение» — проверка свода на ДРУГОЙ модели (только по кнопке).
import { createSecondOpinion } from './second-opinion.js';

/** Фраза, если человек запускает доработку без своего замечания (только «учти ответы»). */
export const REVISE_NO_NOTE = 'Доработать свод с учётом моих ответов на вопросы и уточнений в диалоге';

// W24-N1: вид свода (колонка чтения, плашки, кегль) — сразу, без ожидания клика.
try { ensureCss('css/svod.css'); } catch { /* страница без CSS — не беда */ }

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

/* ── «живой свод» (W20 §1.1, B-36): строка состояния версии ─────────────── */

/** Что значит состояние свода — одна короткая мысль на строку. */
const LIVE_LABEL = {
  fresh: 'Свод актуален',
  pending: 'Свод отстаёт — обновится сам',
  updating: 'Идёт новая версия',
  // W21-S2 (B-46): работа команды ещё идёт — свод ждёт её завершения, а не «отстаёт»
  waiting_cards: 'Ждём завершения работы команды — тогда свод обновится сам',
  stale: 'Свод отстаёт — автообновление выключено',
  off: 'Свода ещё нет',
};
const LIVE_ICON = {
  fresh: 'check', pending: 'clock', updating: 'sync', waiting_cards: 'tasklist', stale: 'warning',
  off: 'info',
};

/** «2 ответа и 1 сообщение» — что не учтено показанной версией. */
export function liveCountsText(behind) {
  const b = behind && typeof behind === 'object' ? behind : {};
  const parts = [];
  const n = (v) => (Number(v) > 0 ? Number(v) : 0);
  if (n(b.answers)) parts.push(countNoun(n(b.answers), ['ответ', 'ответа', 'ответов']));
  if (n(b.notes)) parts.push(countNoun(n(b.notes), ['сообщение', 'сообщения', 'сообщений']));
  if (n(b.decisions)) parts.push(countNoun(n(b.decisions), ['решение', 'решения', 'решений']));
  if (n(b.cards)) parts.push(countNoun(n(b.cards), ['карточка', 'карточки', 'карточек']));
  if (!parts.length) return '';
  return parts.length === 1 ? parts[0] : parts.slice(0, -1).join(', ') + ' и ' + parts[parts.length - 1];
}

/**
 * Текст строки живого свода: с сервера (`live.text`), иначе — свои слова.
 * Свои нужны для старых ответов и для честного поведения, если сервер
 * прислал состояние без подписи.
 * @param {object} live `synthesis.live`
 * @returns {string}
 */
export function liveLineText(live) {
  const l = live && typeof live === 'object' ? live : {};
  const own = String(l.text || '').trim();
  if (own) return own;
  const state = String(l.state || '') || 'fresh';
  const version = Number(l.since_version || 0);
  const behind = liveCountsText(l.behind);
  if (state === 'updating') return version ? `Обновляется… версия ${version + 1}` : 'Обновляется…';
  // W21-S2 (B-46): ждём карточки команды — не «отстаём», а ждём; текст сервера важнее.
  if (state === 'waiting_cards') return 'Обновится после завершения работы команды';
  if (state === 'pending') {
    return behind ? `Не учтено: ${behind} — свод обновится сам` : 'Отстаёт — свод обновится сам';
  }
  if (state === 'stale') {
    return behind ? `Не учтено: ${behind} — автообновление свода выключено, обновите вручную`
      : 'Отстаёт — автообновление выключено, обновите вручную';
  }
  if (state === 'off') return 'Свод ещё не выпущен — появится после первого ответа команды.';
  return version ? `Актуально · версия ${version}` : 'Свод актуален';
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
        previewRow(cur ? `Свод версии ${cur}` : 'Свод',
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

  /** Итог прохода — сразу под заголовком «Свод», в любом состоянии. */
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

  /**
   * W29B-Q4: «Второе мнение» — одна фабрика на страницу запроса. Блок стоит под
   * «Что изменилось», кнопка в нём открывает выбор модели; сама проверка — карточка
   * на другой модели, поэтому результат приходит деревом, а не мгновенно.
   */
  const secondOpinion = createSecondOpinion({
    ctx, rid, root, runAction,
    getDetail: () => R.detail,
  });

  /**
   * W25.5: выбрать версию свода — единственная реализация.
   * Её дёргают и переключатель в липкой панели (shell-svod-bar.js), и меню
   * «Ещё ▾» на узком виде; внутри карточки переключателя больше нет.
   * @param {number} val номер версии
   */
  function setVersion(val) {
    const s = synthesisOf(R.detail);
    const latest = latestVersion(s);
    const num = Number(val) || 0;
    state.version = num >= latest ? null : num;
    render();
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

  // W25.5: переключатель версий живёт в липкой панели свода (shell-svod-bar.js).
  // Здесь его больше нет — было два места с одним и тем же управлением; выбор
  // версии делается функцией `setVersion` выше.

  /* ── «Что изменилось» и сравнение версий (W17-S §3, W29B-Q4) ─────────── */

  /**
   * Сравнение версий по тексту: добавленное и удалённое, по абзацам и словам.
   *
   * W29B-Q4: разница считается в браузере (`web/js/svod-diff.js`), модель не
   * вызывается. Над списком — счётчики и переход «‹ ›» между правками без
   * перерисовки страницы (иначе терялась бы позиция чтения).
   * @param {string} prevText текст версии N−1
   * @param {string} curText текст версии N
   * @param {number} prevVersion
   * @param {number} curVersion
   * @returns {HTMLElement}
   */
  function compareBlock(prevText, curText, prevVersion, curVersion) {
    const blocks = diffParagraphs(prevText, curText);
    const counts = diffCounts(blocks);
    const marks = changeIndexes(blocks);
    if (!marks.length) {
      return h('div', { class: 'req-answer__diff svd', 'data-diff': 'none' },
        h('div', { class: 'small muted' }, 'Различий по абзацам и словам нет.'));
    }
    const counter = h('span', { class: 'svd__count chip chip--muted tnum' }, countsText(counts));
    const where = h('span', { class: 'svd__where small muted tnum' }, '');
    let at = 0;

    const lines = h('div', { class: 'svd__body' });
    for (const block of blocks) {
      if (block.kind === 'same') {
        lines.appendChild(h('div', { class: 'svd__line svd__line--same' },
          shortLine(block.text, 300)));
        continue;
      }
      const line = h('div', {
        class: 'svd__line svd__line--change', 'data-change': String(at),
        title: 'Абзац изменён в версии ' + curVersion,
      });
      for (const op of block.words || []) {
        if (op.kind === 'same') {
          line.appendChild(h('span', { class: 'svd__same' }, shortLine(op.text, 400)));
        } else {
          line.appendChild(h('span', {
            class: op.kind === 'add' ? 'svd__add' : 'svd__del',
            title: op.kind === 'add' ? 'Добавлено' : 'Убрано',
          }, op.kind === 'add' ? '+' : '−', shortLine(op.text, 400)));
        }
      }
      lines.appendChild(line);
      at += 1;
    }
    const total = marks.length;
    let cursor = 0;                 // номер правки, на которой стоит человек

    /** Показать правку №i: прокрутка и подсветка, без перерисовки страницы. */
    const show = (index) => {
      cursor = Math.max(0, Math.min(Number(index) || 0, total - 1));
      const node = lines.querySelector('[data-change="' + String(marks[cursor]) + '"]');
      if (!node) return;
      for (const other of Array.from(lines.querySelectorAll('.is-current'))) other.classList.remove('is-current');
      node.classList.add('is-current');
      if (typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'center', behavior: 'smooth' });
      where.textContent = 'правка ' + (cursor + 1) + ' из ' + total;
    };
    const step = (dir) => show((cursor + (Number(dir) < 0 ? -1 : 1) + total) % total);

    const nav = h('div', { class: 'svd__nav' },
      h('button', {
        type: 'button', class: 'btn btn--ghost btn--sm svd__prev', title: 'Предыдущее изменение',
        'aria-label': 'Предыдущее изменение', onclick: () => step(-1),
      }, icon('chevron-up', { size: 12 })),
      h('button', {
        type: 'button', class: 'btn btn--ghost btn--sm svd__next', title: 'Следующее изменение',
        'aria-label': 'Следующее изменение', onclick: () => step(1),
      }, icon('chevron-down', { size: 12 })),
      where);

    const box = h('div', {
      class: 'req-answer__diff svd', 'data-diff': String(total),
      'data-from': String(prevVersion), 'data-to': String(curVersion),
    },
    h('div', { class: 'svd__head' },
      h('span', { class: 'svd__title' },
        'Версии ' + prevVersion + ' → ' + curVersion + ': красное — убрано, зелёное — добавлено'),
      counter,
      h('div', { class: 'spacer' }),
      total > 1 ? nav : null),
    lines);
    // Первая правка отмечена сразу: человек видит, куда смотреть, без клика.
    show(0);
    return box;
  }

  /** Список «Что изменилось» под версией. */
  function changesBlock(entry, d, prevText, curText) {
    if (!entry || Number(entry.version) < 2) return null;
    const items = versionChanges(entry, d, prevText, curText);
    if (!items.length) return null;
    // W25-L2 (R5 §7.2): блок сворачивается. По умолчанию открыт только на
    // широком виде (xl/l) — при виде < 1100 он в одну строку и не отнимает
    // первый экран у свода. Выбор человека сильнее умолчания (section-collapse).
    const app = (typeof document !== 'undefined' && document.getElementById) ? document.getElementById('app') : null;
    const layout = String((app && app.dataset && app.dataset.layout) || 'm');
    const defaults = sectionDefaults(d, { layout, version: entry.version });
    const list = h('div', { class: 'req-answer__changes' },
      h('ul', { class: 'req-answer__changes-list' },
        ...items.map((text) => h('li', { title: text }, shortLine(text, 240)))));
    const sec = createSection({
      key: 'changed',
      requestId: String((R && R.rid) || ''),
      title: 'Что изменилось',
      card: false,
      className: 'req-answer__changes-sec',
      defaultOpen: defaults.changed,
      summary: [{ text: String(items.length), tone: 'muted' }],
      hint: 'Что поменялось в этой версии свода',
      content: () => list,
    });
    return sec.element;
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

  /* ── живой свод: строка состояния и «Обновить сейчас» (W20 §1.1) ─────── */

  let svodBusy = false;

  /**
   * «Обновить сейчас» — `POST /api/requests/{id}/actions {action:"svod_refresh"}`.
   * 409 значит «обновление уже идёт»: не ругаемся, показываем состояние и ждём.
   * @param {HTMLElement} [btn]
   */
  async function svodRefresh(btn) {
    if (svodBusy) return;
    if (!rid || !ctx || !ctx.api || typeof ctx.api.post !== 'function') return;
    svodBusy = true;
    if (btn) { btn.disabled = true; btn.classList.add('is-busy'); }
    try {
      await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, { action: 'svod_refresh' });
      ctx.toast('Обновление свода запущено — новая версия появится в этом блоке', 'ok');
    } catch (err) {
      const msg = String((err && err.message) || '').trim();
      const status = Number(err && (err.status || (err.details && err.details.status)));
      if (status === 409 || /уже идёт|уже ид/i.test(msg)) {
        ctx.toast(msg || 'Обновление свода уже идёт — дождитесь новой версии.', 'info');
      } else {
        ctx.toast(msg || 'Не удалось запустить обновление свода', 'danger');
      }
    } finally {
      svodBusy = false;
      if (btn) { btn.disabled = false; btn.classList.remove('is-busy'); }
      try { await R.refresh({ force: true }); } catch { /* страница обновится опросом */ }
    }
  }

  /**
   * Строка «Актуально на … · версия N · учтено: …» / «Обновляется…» / «Отстаёт…»
   * с кнопкой «Обновить сейчас». Заменяет прежнюю плашку «в своде не учтено».
   * @param {object} d detail
   * @param {object} s synthesis
   * @returns {HTMLElement|null}
   */
  function liveBlock(d, s) {
    const live = s && s.live && typeof s.live === 'object' ? s.live : null;
    if (!live) return null;
    const state = String(live.state || '') || 'fresh';
    const text = liveLineText(live);
    const busy = state === 'updating';
    const canRefresh = !busy && state !== 'off' && !(d && d.archived);
    // W24-N1 (§2.4): главная кнопка одна и по состоянию. Когда свод отстаёт —
    // это «Обновить сейчас» (вид primary), когда актуален — тихая пересборка.
    const leading = ['pending', 'stale', 'waiting_cards'].includes(state);
    const el = h('div', {
      class: 'req-answer__live req-answer__live--' + state,
      'data-live': state,
      role: 'status', 'aria-live': 'polite',
      title: LIVE_LABEL[state] || null,
    },
    busy ? icon('sync', { size: 12, className: 'codicon-modifier-spin' })
      : icon(LIVE_ICON[state] || 'info', { size: 12 }),
    h('span', { class: 'req-answer__live-text' }, text),
    h('div', { class: 'spacer' }),
    canRefresh ? h('button', {
      type: 'button',
      class: 'btn btn--sm req-answer__live-btn ' + (leading ? 'btn--primary' : 'btn--ghost'),
      'data-action': 'svod-refresh',
      title: SVOD_RU.refreshHint,
      onclick: (e) => svodRefresh(e && e.currentTarget),
    }, icon('refresh', { size: 12 }), leading ? SVOD_RU.refreshNow : SVOD_RU.refreshShort) : null,
    busy ? h('span', { class: 'req-answer__live-note' }, SVOD_RU.refreshNote) : null);
    return el;
  }

  /** Состояние живого свода — для иерархии кнопок под текстом. */
  function liveStateOf(s) {
    const live = s && s.live && typeof s.live === 'object' ? s.live : null;
    return live ? (String(live.state || '') || 'fresh') : '';
  }

  /** Карточка «Коротко» над сводом: одна мысль, один раз (W24-N1 §2.2).
   *
   * W28b (дефект 9): текст законченный, без «…». Если резолюция длинная,
   * видна первая часть по целым предложениям, остальное — по «Читать далее»
   * (раскрытие вместо обрыва посреди мысли). */
  function headlineBlock(headline) {
    const text = String(headline || '').trim();
    if (!text) return null;
    const { lead, rest } = headlineSplit(text);
    const box = h('div', { class: 'svod-head', 'data-svod': 'headline' },
      h('span', { class: 'svod-head__title', title: SVOD_RU.headlineTitle }, SVOD_RU.headline),
      h('span', { class: 'svod-head__text' }, lead));
    if (!rest) return box;
    const tail = h('span', { class: 'svod-head__text', hidden: true }, (lead ? ' ' : '') + rest);
    const more = h('button', {
      type: 'button', class: 'btn btn--link btn--sm svod-head__more',
      'data-svod': 'headline-more', 'aria-expanded': 'false',
    }, SVOD_RU.more);
    more.addEventListener('click', () => {
      const open = more.getAttribute('aria-expanded') !== 'true';
      more.setAttribute('aria-expanded', open ? 'true' : 'false');
      more.textContent = open ? SVOD_RU.less : SVOD_RU.more;
      if (open) tail.removeAttribute('hidden'); else tail.setAttribute('hidden', '');
    });
    box.append(tail, more);
    return box;
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

    const card = h('section', {
      class: 'card req-answer', id: 'answer', 'aria-labelledby': 'answer-title',
      // У-1: свод — единственный блок, который НЕ сворачивается (решение Михаила
      // 11.11). Атрибуты нужны проверкам и печати: стрелки здесь нет и не будет.
      'data-section': 'answer', 'data-collapsible': 'false',
    });
    // W20-U1 (P7): одно название — «Свод» (а не «Свод» / «ответ команды»),
    // и «?» рядом: слово нетривиальное, объяснение одно на всё приложение.
    // W25.5: переключатель версий уехал в липкую панель свода (одно место
    // управления); в карточке остаются текст свода и одна тихая строка статуса.
    card.append(h('div', { class: 'req-answer__head' },
      h('h2', { class: 'req-answer__title', id: 'answer-title' }, 'Свод', termHelp('svod')),
      s.legacy ? h('span', { class: 'chip chip--muted', title: 'Ответ получен из старого формата metadata' }, 'старый формат') : null,
      h('div', { class: 'spacer' })));

    const kickBox = kickBlock();
    if (kickBox) card.append(kickBox);

    if (d.status === 'failed') {
      const f = failureInfo(d);
      // B-25: кнопка повтора даёт отклик (блокируется и пишет «принято, в очереди»),
      // повторная отправка того же нажатия не создаёт дубль (idempotency_key на сервере).
      const retry = createRetryButton({
        ctx,
        rid: String(d.id || ''),
        node: { id: d.id, status: d.status, started_at: d.started_at },
        marks: createMarks({ requestId: String(d.id || '') }),
        title: 'Повторить запрос?',
        text: 'Руководитель запустит работу заново: это новый прогон агента. '
          + 'Результаты прошлых попыток останутся в дереве работ.',
        onSent: () => { if (typeof render === 'function') render(); },
      });
      card.append(h('div', { class: 'req-empty' },
        emptyState({ icon: 'error', title: 'Ответ не получился', text: f.text }),
        h('div', { class: 'req-answer__retry' }, retry)));
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

      // W17-S §3: версия подписана причиной, а не просто номером.
      const verRow = h('div', { class: 'req-answer__ver' },
        h('span', { class: 'req-answer__ver-text', title: metaBits.join(' · ') || null },
          versionLabel(entry, d)),
        entry && entry.verdict === 'approve' ? h('span', { class: 'chip chip--ok' }, 'принято критиком') : null,
        entry && entry.verdict === 'revise' ? h('span', { class: 'chip chip--warn' }, 'было на доработке') : null,
        h('div', { class: 'spacer' }),
        metaBits.length ? h('span', { class: 'req-answer__ver-meta' }, metaBits.join(' · ')) : null);
      card.append(verRow);

      // W24-N1 (§2.4): строку «Показана версия N (последняя) · время» убрали —
      // номер версии уже в подписи выше, а время и состояние — в строке живого
      // свода. Осталось только то, чего больше нигде нет: сравнение версий.
      // W25-L2 (R5 §7.3): «сравнить с версией» — справка к своду, поэтому она
      // уезжает ВНИЗ, к «Что изменилось», а не занимает строку первого экрана.
      const vcap = latest > 1 ? h('div', { class: 'req-answer__vcap' },
        h('button', {
          type: 'button', class: 'btn btn--link btn--sm', 'data-action': 'compare',
          onclick: () => { state.compare = !state.compare; render(); },
        }, state.compare ? 'скрыть сравнение' : `сравнить с версией ${Math.max(1, Number(cur) - 1)}`)) : null;

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

      // «Что изменилось» относительно предыдущей версии. Сам блок собираем здесь,
      // а ставим НИЖЕ текста: свод — первое, что читают (R5 §7.3 п. 2: «Свод —
      // первые 6–8 строк текста»), а список изменений — справка к нему.
      const changes = changesBlock(entry, d, prevText, text);

      // W20 §1.1 (B-36): живой свод — строка «Актуально / Обновляется / Отстаёт»
      // с кнопкой «Обновить сейчас» вместо прежней плашки «в своде не учтено».
      // W25.5: когда свод актуален, пометку «Актуально» несёт липкая панель —
      // в карточке строка не повторяется. Остаётся только то, что требует
      // действия: отстаёт / обновляется / ждёт команду.
      // У старого сервера (без `synthesis.live`) остаётся прежняя плашка.
      const liveNow = s && s.live && typeof s.live === 'object' ? s.live : null;
      const liveQuiet = !liveNow || ['fresh', 'off'].includes(String(liveNow.state || 'fresh'));
      const liveRow = isOld ? null : ((liveQuiet ? null : liveBlock(d, s)) || staleBlock(d, s));
      if (liveRow) card.insertBefore(liveRow, card.children[1] || null);

      if (text) {
        // W20-A1 (§1.4): ссылки на локальные файлы внутри свода открываются в
        // контексте комнаты — иначе сервер отвечает 403 «вне корней».
        const answerRoom = String(d.room || (ctx.room && ctx.room.id) || '');
        // W24-N1 (§2.1–2.2): текст нормализуется до показа — абзацы, списки,
        // плашки, пути; «Коротко» показывается один раз, карточкой над текстом.
        const meta = (s && s.metadata && typeof s.metadata === 'object' && s.metadata.svod) || {};
        let parts = { headline: '', body: text };
        try {
          parts = svodParts(text, meta);
        } catch (err) {
          console.warn('[request] оформление свода:', err);
        }
        const head = headlineBlock(parts.headline);
        if (head) card.append(head);
        if (parts.body) {
          const body = h('div', { class: 'req-answer__body' },
            mdBlock(parts.body, { fileUrl, openPath: (path) => openArtifactPath(ctx, path, answerRoom) }));
          wrapTables(body);
          card.append(body);
        }
      } else {
        card.append(h('div', { class: 'req-answer__body' },
          h('div', { class: 'banner banner--warn' },
            icon('warning', { size: 14 }),
            h('span', { class: 'banner__text' }, 'Текст этой версии не найден в задачах запроса — возможно, задача удалена.'))));
      }

      // «Что изменилось» — под текстом: на первом экране остаётся свод (R5 §7.3).
      if (vcap) card.append(vcap);
      if (changes) card.append(changes);

      // W29B-Q4: «Второе мнение» — под различиями, перед кнопками. Кнопка одна и
      // та же, что в липкой панели (там она в меню «Ещё ▾»).
      if (!isOld) {
        const soBlock = secondOpinion.buildBlock();
        if (soBlock) card.append(soBlock);
        if (!secondOpinion.data()) secondOpinion.start();
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

      // W24-N1 (§2.4): иерархия кнопок. Главная — по состоянию свода:
      // отстаёт → «Обновить сейчас» (она в строке состояния, вид primary),
      // актуален → «Доработать свод» (primary здесь). «Копировать» — рядом,
      // остальное — в одно меню «Экспорт ▾». Повторного времени в подвале нет.
      const liveState = liveStateOf(s);
      const refreshFirst = ['pending', 'stale', 'waiting_cards'].includes(liveState);
      card.append(h('div', { class: 'req-answer__foot' },
        h('button', {
          type: 'button',
          class: 'btn ' + (refreshFirst ? 'btn--secondary' : 'btn--primary'),
          disabled: Boolean(d.archived),
          title: 'Руководитель получит запрос, этот свод, ваше замечание и ваши ответы после этой версии',
          'data-action': 'revise', onclick: () => revise(),
        }, icon('comment-draft', { size: 13 }), SVOD_RU.revise),
        h('button', {
          type: 'button', class: 'btn btn--secondary', 'data-action': 'copy', onclick: copyAnswer,
        }, icon('copy', { size: 13 }), SVOD_RU.copy),
        h('div', { class: 'spacer' }),
        // W20-U3 (пункт 17) + W24-N1: «В Obsidian», «Скачать HTML», «Печать / PDF»,
        // «Скачать .md» — одно меню «Экспорт ▾» (раньше было четыре кнопки подряд).
        exportGroup(ctx, rid, { markdownHref: `/api/export/request/${encodeURIComponent(rid)}.md` })));
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

  // W25.5: панели нужны выбор версии и обновление свода — те же функции, что у
  // карточки (одна реализация на два места нажатия).
  return { buildAnswer, setVersion, refreshSvod: () => svodRefresh(null) };
}
