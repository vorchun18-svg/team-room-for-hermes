/**
 * request-aside.js — правая колонка «От вас нужно» (WAVE17, пакет D: B1, B3, B13, B15, P3).
 *
 * Устройство колонки (P3): сверху и всегда раскрыто — «Нужен ваш ответ (N)»
 * (вопросы, которые ждут Михаила, решения, которые ждут его слова, и
 * заблокированные задачи этой ветки); ниже — свёрнутые блоки со счётчиками:
 * «Отвечено», «Шаги», «Решения», «Будущие вопросы», «Карта проекта». Состояние
 * свёрнутости запоминается (aside-fold.js).
 *
 * Счётчики — из ОДНОГО источника: `inboxSummary`/`blockedCards` из
 * backlog-model.js — те же функции, что считают бейдж вкладки «От вас нужно» (B13).
 * Заголовок блока и бейдж вкладки показывают одно число и одну расшифровку.
 *
 * Вопросы: компактная форма (поле + «Надиктовать» от dictation-auto +
 * «Отправить»), одна общая подсказка про «Пояснить…» вместо повтора длинной
 * подписи у каждого вопроса (B15). Статус ответа и реакция руководителя — только
 * из полей API (`answer_state`, `answer_label`, `reaction`), без догадок на
 * клиенте. Если «Напомнить» вернул 409 с найденной реакцией — реакция сразу
 * показывается у вопроса, а кнопка исчезает (B-12).
 *
 * Ответы руководителю (W21-Q1, B-45): при включённом автообновлении свода
 * (`synthesis.live.auto`) кнопки «Отправить накопленные ответы» нет — вместо неё
 * спокойная строка «Ответы переданы руководителю — он учтёт их в ближайшем
 * обновлении свода (около 14:20)». Кнопка `answers_resend` остаётся только при
 * выключенном автообновлении; слова «без реакции» в интерфейсе не осталось.
 * «Пояснить…» (B-44) работает и с пустым полем: вопрос бывает просто непонятен.
 *
 * Фабрика createAside(R) получает общий контекст страницы из request.js
 * и возвращает { buildAside }.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
// W28-D7: общий замок кнопки и предел ожидания (дефект «кнопки умирают»).
import { locked } from '../store.js';
import { firstLine, personName, blockHead } from './request-util.js';
import { countNoun, ANSWERS_RU, LAYOUT_RU, pendingAnswersLabel } from '../text-ru.js';
import { createSteps } from './request-steps.js';
import { blockedCards, inboxSummary } from './backlog-model.js';
import { createApprovals } from './approvals.js';
import { foldCard, foldState, setFoldState } from './aside-fold.js';
import { requestStoreFor, bindLive } from './request-store.js';
// W27-E2 §1: счётчик с сервера, «Не актуально» с возвратом, тихий 409.
import {
  needsYouCount, staleSummary, dismissBody, restoreBody, isAlreadyDecided, toastOnce,
  closedReasonText, isFinishedRequest,
} from './stale-model.js';
import { noticeLine, markRequestSeen } from './reaction-notice.js';
import {
  CHAIN_HINT, decisionActions, decisionAnswerOf, decisionMutedActions, decisionNotice,
  decisionStatusOf, openDecisionDialog, postDecision,
} from './decision-dialog.js';

/**
 * Заменить содержимое узла (без `replaceChildren`: он есть не во всех местах,
 * где страницу проверяют, а `textContent = ''` работает и в браузере, и в мини-DOM).
 * @param {Node} node
 * @param {...Node} nodes
 */
function fill(node, ...nodes) {
  node.textContent = '';
  for (const child of nodes) if (child) node.appendChild(child);
}

/**
 * Подписи статусов ответа — те же слова, что в API (`answer_label`, B-21):
 * «ответ отправлен» → «руководитель прочитал» → «учтено».
 */
const Q_STATE_RU = {
  open: 'ждёт вашего ответа',
  waiting_explanation: 'ждёт вашего решения после пояснения',
  sent: 'ответ отправлен',
  received: 'руководитель прочитал',
  applied: 'учтено: работа изменена',
  no_change: 'учтено: менять ничего не нужно',
  // W20 §1.2 (B-41): ответ записан, руководитель ответил в ленте словами.
  replied_text: 'руководитель ответил текстом — посмотрите в ленте',
  dismissed: 'это не вопрос',
};
const Q_STATE_CHIP = {
  open: 'chip--muted',
  waiting_explanation: 'chip--warn',
  sent: 'chip--warn',
  received: 'chip--info',
  applied: 'chip--ok',
  no_change: 'chip--muted',
  replied_text: 'chip--info',
  dismissed: 'chip--muted',
};
/** «Что значит» для статуса ответа — одна подсказка на все вопросы, шаги, решения. */
const Q_STATE_HINT = {
  open: 'Вопрос ждёт вашего ответа — руководитель не может двигаться дальше без него.',
  waiting_explanation: 'Вы попросили пояснение: вопрос остаётся открытым, пока руководитель не объяснит, о чём речь.',
  sent: 'Ответ ушёл руководителю. Пока он его не открыл и не отметил исход — ждите или нажмите «Напомнить руководителю».',
  received: 'Руководитель прочитал ответ и взял его в работу. «Учтено» появится, когда он запишет, что именно изменил.',
  applied: 'Руководитель учёл ответ: работа пересмотрена, карточки изменены. Их список — рядом с реакцией.',
  no_change: 'Руководитель учёл ответ и решил, что менять в работе ничего не нужно.',
  replied_text: 'Руководитель ответил в ленте словами — ответа по формату реакции нет. '
    + 'Посмотрите диалог: там сказано, что решили.',
  dismissed: 'Вы отметили, что это не вопрос: он не ждёт ответа и не считается в счётчиках.',
};

/** Откуда вопрос — простыми словами (W20 §1.2, B-38). */
const Q_SOURCE_RU = {
  comment: 'из сообщения руководителя',
  candidate: 'из сообщения руководителя — проверьте, вопрос ли это',
  card: '',
};


export function createAside(R) {
  const { ctx, rid, keepValues, refresh } = R;
  ensureCss('css/view-backlog-extras.css');
  // W20-U4 (P9): строка «Ответ учтён: …» у вопроса.
  ensureCss('css/reaction-notice.css');
  // W14-H §12: шаги — план руководителя с окном шага, не галочки.
  const stepsBlock = createSteps(R);
  // W22-AP2: «Участник хочет выполнить команду» — отдельный вид элемента в
  // «Нужен ваш ответ»: сначала разрешения (участник стоит без работы), потом
  // вопросы и решения.
  const approvalsUI = createApprovals(R);
  // W26-D2: ОДИН стор запроса на страницу — колонка и основной блок читают его же.
  const storeRef = R.store || requestStoreFor(rid, {
    api: (ctx && ctx.api) || null,
    roomId: R.roomId,
    detail: R.detail || null,
    refresh,
  });
  const storeOf = () => storeRef;

  /**
   * Ответ на конкретный вопрос (W14-D §1–§3). `needExplanation` — встречный
   * вопрос («поясни»): вопрос при этом НЕ закрывается, руководитель обязан
   * объяснить в ветке, поэтому форма остаётся на месте.
   *
   * W21-Q1 (B-44): для «Пояснить» текст НЕ обязателен — вопрос бывает просто
   * непонятен. Пустое поле → `need_explanation:true` без `text`, сервер сам
   * ставит стандартную просьбу. «Отправить» с пустым полем — по-прежнему отказ.
   */
  async function answerQuestion(q, ta, btn, opts = {}) {
    const needExplanation = Boolean(opts && opts.needExplanation);
    // Двойное нажатие (и клик по уже занятой кнопке) — не вторая просьба.
    if (btn && btn.disabled) return;
    const text = String(ta.value || '').trim();
    if (!text && !needExplanation) {
      ta.focus();
      ctx.toast(ANSWERS_RU.answerEmpty, 'warn');
      return;
    }
    // W28-D7: замок снимается в `finally` (и по таймауту) — кнопка не «умирает».
    await locked(btn, async () => {
      try {
        const body = { action: 'answer', question_id: q.id };
        if (text) body.text = text;
        if (needExplanation) body.need_explanation = true;
        await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, body);
        keepValues.delete('q:' + q.id);
        ctx.toast(needExplanation ? ANSWERS_RU.explainSent
          : 'Ответ отправлен — руководитель увидит его у вопроса', 'ok');
        await refresh({ force: true });
      } catch (err) {
        ctx.toast((err && err.message) || 'Не удалось отправить ответ', 'danger');
      }
    }, { label: 'Отправка ответа' });
  }

  /** Реакция из тела ошибки 409 (сервер отдаёт её в `error.detail.reaction`). */
  function reactionFromError(err) {
    const data = err && err.details;
    if (!data || typeof data !== 'object') return null;
    const inner = data.error && typeof data.error === 'object' ? data.error : data;
    const detail = inner && typeof inner.detail === 'object' ? inner.detail : null;
    const reaction = detail && detail.reaction;
    if (!reaction || typeof reaction !== 'object' || !reaction.text) return null;
    return reaction;
  }

  /** Перенести найденную реакцию в данные вопроса: статус, подпись, без «Напомнить». */
  function applyReaction(q, reaction) {
    if (!q || !reaction) return;
    q.reaction = reaction;
    q.answer_state = String(reaction.status || 'received');
    q.answer_label = Q_STATE_RU[q.answer_state] || Q_STATE_RU.received;
    q.leader_pending = false;
    q.can_remind = false;
    q.reaction_overdue = false;
    q.reaction_waited_s = null;
  }

  /**
   * «Напомнить руководителю» (W14-D §2).
   *
   * B-12: если сервер отвечает 409 «руководитель уже ответил» и присылает саму
   * реакцию — показываем её у вопроса сразу и убираем кнопку. Раньше окно
   * вопроса оставалось пустым, а рядом висело «руководитель ещё не ответил».
   */
  async function remindLeader(q, btn) {
    // W28-D7: замок снимается при любом исходе — раньше ветка «руководитель уже
    // ответил» уходила с `return` до разблокировки, и кнопка оставалась мёртвой.
    await locked(btn, async () => {
      try {
        await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, {
          action: 'answer_remind', question_id: q.id,
        });
        ctx.toast('Напоминание отправлено руководителю', 'ok');
        await refresh({ force: true });
      } catch (err) {
        const reaction = reactionFromError(err);
        if (reaction) {
          applyReaction(q, reaction);
          ctx.toast('Руководитель уже ответил — его ответ показан у вопроса', 'ok');
          if (typeof R.render === 'function') R.render();
          return;
        }
        ctx.toast((err && err.message) || 'Не удалось отправить напоминание', 'danger');
      }
    }, { label: 'Напоминание руководителю' });
  }

  async function askFuture(text, btn) {
    await locked(btn, async () => {
      try {
        const res = await ctx.api.post(`/api/rooms/${encodeURIComponent(R.roomId)}/requests`, { text });
        const nextId = res?.root_id || res?.request?.id;
        ctx.toast('Запрос создан', 'ok');
        if (nextId) ctx.navigate(`#/room/${encodeURIComponent(R.roomId)}/request/${encodeURIComponent(nextId)}`);
        else await refresh({ force: true });
      } catch (err) {
        ctx.toast((err && err.message) || 'Не удалось создать запрос', 'danger');
      }
    }, { label: 'Создание запроса' });
  }

  /** «Отправить руководителю накопленные ответы» — действие `answers_resend` (W17-B §3). */
  async function resendAnswers(btn) {
    await locked(btn, async () => {
      try {
        const res = await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`,
          { action: 'answers_resend' });
        const sent = Number(res && res.sent) || 0;
        ctx.toast(sent
          ? 'Руководителю отправлено одной карточкой: '
            + countNoun(sent, ['ответ', 'ответа', 'ответов'])
          : 'Накопленные ответы отправлены руководителю', 'ok');
        await refresh({ force: true });
      } catch (err) {
        ctx.toast((err && err.message) || 'Не удалось отправить ответы', 'danger');
      }
    }, { label: 'Отправка ответов' });
  }

  /* ── вопросы: «Вопрос — ваш ответ», статус и реакция руководителя ────── */

  /** Состояние ответа на вопрос: `open|waiting_explanation|sent|received|applied|no_change|replied_text|dismissed`. */
  function qState(q) {
    // W20 §1.2: «Это не вопрос» — вопрос не ждёт ответа, даже если ответа нет.
    if (q && q.dismissed) return 'dismissed';
    const state = String((q && q.answer_state) || '').trim();
    if (state) return state;
    return q && q.answered ? 'sent' : 'open';
  }

  /** Подпись статуса: с сервера (`answer_label`), иначе своя — слова те же. */
  function qStateLabel(q, state) {
    return String((q && q.answer_label) || '').trim() || Q_STATE_RU[state] || Q_STATE_RU.open;
  }

  /** Вопрос ещё ждёт Михаила: не отвечен или просит пояснения. */
  function qOpen(q) {
    const state = qState(q);
    if (state === 'dismissed' || state === 'replied_text') return false;
    if (state === 'waiting_explanation') return true;
    return !(q && q.answered);
  }

  /**
   * Ключ строки вопроса для перерисовки: пока он не изменился, узел НЕ
   * пересобирается. Это не оптимизация — от этого зависит диктовка: поле и
   * кнопка микрофона остаются теми же узлами, панель не теряет привязку к
   * своему вопросу при опросе, «Стоп» и распознавании (W15-C, п. 4).
   */
  function qSig(q, open) {
    return JSON.stringify([
      // W25-L2: развёрнутость — часть подписи строки: смена «кто открыт»
      // пересобирает только затронутые строки.
      open === true,
      String((q && q.id) || ''), String((q && q.text) || ''), String((q && q.for) || ''),
      Boolean(q && q.answered), String((q && q.answer) || ''), Number((q && q.answered_at) || 0),
      qState(q), Boolean(q && q.needs_explanation), Boolean(q && q.leader_pending),
      Boolean(q && q.can_remind), Boolean(q && q.reaction_overdue),
      Number((q && q.reminded_at) || 0), Boolean(q && q.answer_changes_done),
      JSON.stringify((q && q.reaction) || null),
      // W20 §1.2 (B-38/B-41): откуда вопрос и снят ли он — строка пересобирается.
      String((q && q.source) || ''), Boolean(q && q.dismissed), Boolean(q && q.needs_answer),
    ]);
  }

  function questionTop(q, isMaxim) {
    return h('div', { class: 'req-q__top' },
      h('div', { class: 'req-q__text' }, q.text || ''),
      isMaxim ? null : h('span', {
        class: 'chip chip--outline',
        title: 'Вопрос адресован: ' + (personName(q.for) || String(q.for)),
      }, personName(q.for) || String(q.for)));
  }

  /**
   * «Это не вопрос» — `POST …/actions {action:"question_dismiss", question_id}`
   * (W20 §1.2, B-38): убирает кандидата из «Нужен ваш ответ» и из счётчиков.
   * Данные запроса не меняются — отметка живёт в реестре ответов.
   * @param {object} q вопрос
   * @param {HTMLElement} [btn]
   */
  async function dismissQuestion(q, btn) {
    if (!q || !q.id) return;
    // W28-D7: замок снимается в `finally` (и по таймауту), а не только в ошибке.
    await locked(btn, async () => {
      try {
        await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`,
          { action: 'question_dismiss', question_id: String(q.id) });
        ctx.toast('Убрали из «Нужен ваш ответ» — это не вопрос', 'ok');
        await refresh({ force: true });
      } catch (err) {
        ctx.toast((err && err.message) || 'Не удалось убрать вопрос', 'danger');
      }
    }, { label: 'Убрать вопрос' });
  }

  /**
   * «Не актуально» / «Вернуть» для вопроса (W27-E2 §1).
   *
   * Раньше убрать вопрос можно было только у пункта-кандидата («Это не вопрос»).
   * Теперь у каждого вопроса есть «Не актуально» с возвратом: счётчик обнуляется,
   * не отвечая, а вопрос остаётся в «Закрытые» и возвращается одной кнопкой.
   *
   * @param {object} q вопрос
   * @param {'dismiss'|'restore'} action
   * @param {HTMLElement} [btn]
   */
  async function staleQuestion(q, action, btn) {
    if (!q || !q.id) return;
    // W28-D7: «Не актуально»/«Вернуть» — под общим замком: он снимается и при
    // успехе без перерисовки (раньше кнопка оставалась disabled до перезагрузки).
    await locked(btn, async () => {
      try {
        if (storeRef && typeof storeRef.targetAction === 'function') {
          await storeRef.targetAction('questions', String(q.id), action);
        } else {
          await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`,
            action === 'restore' ? restoreBody('question', q.id) : dismissBody('question', q.id));
        }
        ctx.toast(action === 'restore'
          ? 'Вопрос вернулся в «Нужен ваш ответ»'
          : 'Вопрос убран из «Нужен ваш ответ» — вернуть можно в «Закрытые»', 'ok');
        await refresh({ force: true, quiet: true });
      } catch (err) {
        // 409 already_decided — тихо: экран просто устарел, стор уже перечитал данные.
        if (isAlreadyDecided(err)) return;
        toastOnce(ctx.toast, 'question-stale', (err && err.message) || 'Не удалось изменить вопрос', 'danger');
      }
    }, { label: action === 'restore' ? 'Возврат вопроса' : 'Снятие вопроса' });
  }

  /**
   * Строка «Не актуально» / «Вернуть» у вопроса.
   * @param {object} q
   * @returns {HTMLElement|null}
   */
  function questionStaleRow(q) {
    const state = qState(q);
    const restore = state === 'dismissed' || q.dismissed === true;
    const btn = h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-q__stale',
      'data-action': restore ? 'question-restore' : 'question-stale',
      'data-question-id': String(q.id || ''),
      title: restore
        ? 'Вернуть вопрос в «Нужен ваш ответ»'
        : 'Убрать из «Нужен ваш ответ», не отвечая: вопрос останется в «Закрытые», вернуть можно одной кнопкой',
    }, icon(restore ? 'history' : 'circle-x', { size: 11 }), restore ? 'Вернуть' : 'Не актуально');
    btn.addEventListener('click', (ev) => {
      if (ev && ev.preventDefault) ev.preventDefault();
      staleQuestion(q, restore ? 'restore' : 'dismiss', btn);
    });
    return h('div', { class: 'req-q__stale-row', 'data-key': 'qstale' }, btn);
  }

  /**
   * Строка под вопросом: откуда он и, для пункта-кандидата, кнопка «Это не вопрос».
   * @param {object} q
   * @returns {HTMLElement|null}
   */
  function sourceRow(q) {
    const source = String((q && q.source) || '');
    const text = Q_SOURCE_RU[source] || '';
    const candidate = source === 'candidate';
    if (!text && !candidate) return null;
    const dismiss = candidate ? h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-q__dismiss',
      'data-action': 'question-dismiss', 'data-question-id': String(q.id || ''),
      title: 'Убрать из «Нужен ваш ответ»: это не вопрос, отвечать не нужно',
      onclick: (e) => dismissQuestion(q, e && e.currentTarget),
    }, icon('circle-slash', { size: 11 }), 'Это не вопрос') : null;
    return h('div', { class: 'req-q__src', 'data-source': source || 'card' },
      text ? h('span', {
        class: 'chip chip--outline req-q__source',
        title: candidate
          ? 'Пункт списка из сообщения руководителя: приложение предлагает его как вопрос. Если это не вопрос — нажмите «Это не вопрос».'
          : 'Вопрос взят из сообщения руководителя в диалоге запроса',
      }, text) : null,
      dismiss);
  }

  /**
   * Реакция руководителя на ответ: строка «Ответ учтён: …» (P9), текст и карточки,
   * которых он коснулся.
   *
   * Строку собирает `views/reaction-notice.js` из полей API (`status`, `label`, `at`) —
   * теми же словами, что бейдж вкладки и отметка в «Недавнем». Как только реакция
   * показана, запрос помечается прочитанным локально: бейдж не должен гореть у того,
   * кто уже всё видит на экране.
   */
  function reactionBlock(q) {
    const r = q.reaction;
    if (!r || typeof r !== 'object') return null;
    const cards = Array.isArray(r.cards) ? r.cards.filter(Boolean).slice(0, 6) : [];
    const when = Number(r.at) || 0;
    const seenLine = r.text || r.label || when
      ? noticeLine({ status: r.status, label: r.label, at: when })
      : '';
    markSeenIfAny(r);

    const head = h('div', { class: 'req-q__notice' + (String(r.status) === 'received' ? ' req-q__notice--read' : ''),
      role: 'status' },
    icon(String(r.status) === 'received' ? 'eye' : 'check', { size: 12 }),
    h('span', { class: 'req-q__notice-text', title: 'Реакция руководителя на ваш ответ' }, seenLine));

    // Текста реакции нет — но факт «прочитал/учёл» показать всё равно нужно.
    if (!r.text) return cards.length ? h('div', { class: 'req-q__reaction' }, head,
      h('div', { class: 'req-q__cards' }, cards.map((id) => h('span', {
        class: 'req-q__card tnum', title: 'Карточка работы, которой коснулся ответ: ' + id,
      }, String(id))))) : head;

    return h('div', { class: 'req-q__reaction' },
      head,
      h('div', { class: 'req-q__reaction-head' },
        icon('comment-discussion', { size: 12 }),
        h('span', { class: 'req-q__cap' }, 'Руководитель'),
        when ? h('span', { class: 'muted req-q__when' }, timeAgo(when)) : null),
      h('div', { class: 'req-q__reaction-text' }, String(r.text)),
      cards.length
        ? h('div', { class: 'req-q__cards' }, cards.map((id) => h('span', {
          class: 'req-q__card tnum', title: 'Карточка работы, которой коснулся ответ: ' + id,
        }, String(id))))
        : null);
  }

  /** Отметка «прочитано» в браузере: реакция в этом запросе больше не новая. */
  function markSeenIfAny(reaction) {
    const roomId = String((ctx && ctx.room && ctx.room.id) || (R.roomId || ''));
    const requestId = String(rid || '');
    const at = Number(reaction && reaction.at) || 0;
    if (!roomId || !requestId || !at) return;
    try { markRequestSeen(roomId, requestId, at); } catch { /* отметка не обязательна */ }
  }

  /** Ждём реакции руководителя: подпись и кнопка «Напомнить руководителю». */
  function pendingBlock(q) {
    if (!q.leader_pending && !q.can_remind) return null;
    if (q.reaction) return null; // реакция уже есть — ждать нечего (B-12)
    const reminded = Number(q.reminded_at) || 0;
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req-q__remind',
      'data-action': 'remind', title: 'Отправить руководителю короткое напоминание про этот ответ',
    }, icon('bell', { size: 12 }), 'Напомнить руководителю');
    btn.addEventListener('click', () => remindLeader(q, btn));
    const waited = Number(q.reaction_waited_s) || 0;
    return h('div', { class: 'req-q__pending' + (q.reaction_overdue ? ' is-overdue' : '') },
      h('span', { class: 'req-q__pending-label' },
        icon('clock', { size: 12 }),
        h('span', null, q.reaction_overdue
          ? 'руководитель ещё не ответил — ждём ' + formatWait(waited)
          : 'руководитель ещё не ответил')),
      reminded
        ? h('span', { class: 'muted req-q__when' }, 'напоминание отправлено ' + timeAgo(reminded))
        : btn);
  }

  /** «5 мин», «2 ч» — сколько ждём реакции (для «нет реакции дольше разумного»). */
  function formatWait(seconds) {
    const s = Math.max(0, Number(seconds) || 0);
    if (s < 90) return Math.max(1, Math.round(s)) + ' с';
    if (s < 5400) return Math.round(s / 60) + ' мин';
    if (s < 172800) return Math.round(s / 3600) + ' ч';
    return Math.round(s / 86400) + ' дн';
  }

  /** Ответ на вопрос рядом с вопросом: «Вопрос — ваш ответ», статус, реакция. */
  function answerBlock(q, state) {
    const label = qStateLabel(q, state);
    const answeredAt = Number(q.answered_at) || 0;
    return h('div', { class: 'req-q__answer req-q__answer--' + state },
      h('div', { class: 'req-q__answer-head' },
        h('span', { class: 'req-q__pair' }, 'Вопрос — ваш ответ'),
        h('span', { class: 'chip ' + (Q_STATE_CHIP[state] || 'chip--muted') + ' req-q__state',
          title: 'Статус ответа: ' + label + ' — ' + (Q_STATE_HINT[state] || CHAIN_HINT) }, label)),
      h('div', { class: 'req-q__atext' }, String(q.answer || '')),
      h('div', { class: 'req-q__meta' },
        answeredAt ? h('span', { class: 'muted req-q__when' }, 'отвечено ' + timeAgo(answeredAt)) : null,
        q.answer_changes_done ? h('span', { class: 'req-q__note' },
          'ответ изменил уже сделанное — работа пересматривается') : null),
      reactionBlock(q),
      pendingBlock(q));
  }

  /**
   * Поле ответа: как в composer — поле и отдельные кнопки «Надиктовать»
   * (её ставит dictation-auto.js перед «Отправить») и «Отправить». Компактно
   * (P3/B15): без повтора длинной подписи и лишних пояснений у каждого вопроса.
   */
  function answerForm(q, state) {
    const key = 'q:' + q.id;
    const waiting = state === 'waiting_explanation';
    const ta = h('textarea', {
      class: 'textarea req-q__input', rows: 2, 'data-keep': key,
      'data-hotkey-input': '',
      placeholder: waiting ? 'Что пояснить в вопросе? Можно не писать' : 'Ваш ответ…',
      'aria-label': 'Ответ на вопрос: ' + firstLine(q.text, 60),
      value: keepValues.get(key) || '',
    });
    ta.addEventListener('input', () => keepValues.set(key, ta.value));
    const send = h('button', {
      type: 'button', class: 'btn btn--primary btn--sm req-q__send', 'data-action': 'answer',
      title: 'Отправить ответ руководителю (Ctrl+Enter)',
    }, icon('send', { size: 12 }), 'Отправить');
    send.addEventListener('click', () => answerQuestion(q, ta, send));
    ta.addEventListener('keydown', (ev) => {
      if (ev && ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        answerQuestion(q, ta, send);
      }
    });
    // Короткая кнопка «Пояснить…» вместо длинной подписи у каждого вопроса:
    // объяснение — в общей подсказке блока (answerHintLine) и в title.
    // W21-Q1 (B-44): текст можно не писать — подсказка так и говорит.
    const explain = h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-q__explain', 'data-action': 'need-explanation',
      title: ANSWERS_RU.explainTitle,
      'aria-label': 'Мне нужно пояснение к вопросу: ' + firstLine(q.text, 60),
    }, icon('comment-discussion', { size: 12 }), 'Пояснить…');
    explain.addEventListener('click', () => answerQuestion(q, ta, explain, { needExplanation: true }));
    return h('div', { class: 'req-q__form' },
      h('div', { class: 'req-q__row' }, ta, send),
      h('div', { class: 'req-q__actions' }, explain,
        waiting ? h('span', { class: 'muted req-q__hint' },
          'ждём пояснения руководителя — вопрос открыт') : null));
  }

  function questionRow(q, open) {
    const isMaxim = (q.for || 'mikhail') === 'mikhail';
    const state = qState(q);
    let rowEl = null; // строка — нужна обработчику заголовка (одна форма на список)
    const answered = state === 'waiting_explanation' || Boolean(q.answer) || Boolean(q.answered);
    // Части строки получают СВОИ ключи (`data-key`): `applyLivePatch` сверяет
    // детей по ключу, а без него — по порядку. Когда состояние вопроса меняется
    // («открыт» → «ждёт пояснения»), у строки появляется блок «Вопрос — ваш ответ»,
    // и порядковое сопоставление ставило его узел на место ФОРМЫ: поле ответа и
    // кнопки пропадали (E2E W21: после «Пояснить» отвечать было нечем).
    // W25-L2: заголовок вопроса — настоящая кнопка (клик, Enter, Space): по ней
    // разворачивается форма. Состояние пишем в `data-open` строки.
    const top = questionTop(q, isMaxim);
    top.setAttribute('data-key', 'qtop');
    const head = h('div', {
      class: 'req-q__head',
      role: 'button',
      tabindex: '0',
      'aria-expanded': open ? 'true' : 'false',
      title: open ? 'Свернуть вопрос' : 'Ответить на этот вопрос',
    }, top, h('span', { class: 'req-q__badge', 'aria-hidden': 'true' }, open ? 'свернуть' : 'ответить'));
    const toggleFromHead = (ev) => {
      if (ev && ev.target && ev.target.closest && ev.target.closest('.req-q__badge, .chip')) return;
      const listEl = rowEl && rowEl.parentNode ? rowEl.parentNode : null;
      if (ev && ev.preventDefault) ev.preventDefault();
      if (open) expandQuestion('', listEl);
      else expandQuestion(String(q.id || ''), listEl);
    };
    head.addEventListener('click', toggleFromHead);
    head.addEventListener('keydown', (ev) => {
      if (!ev) return;
      if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') toggleFromHead(ev);
    });
    const row = h('div', {
      class: 'req-q req-q--' + state,
      'data-q': String(q.id || ''),
      'data-q-state': state,
      // WAVE17 §1: ключ точечной перерисовки — строка вопроса не пересобирается,
      // пока не изменился сам вопрос (от этого зависит диктовка и фокус).
      'data-key': 'q' + String(q.id || ''),
      // W20-U3: строка — шаг клавиш J/K (hotkeys.js), поле — цель фокуса.
      'data-hotkey-item': '',
      // Ключ привязки панели диктовки: панель находит СВОЙ вопрос после перерисовки.
      'data-mic-anchor': 'q:' + String(q.id || ''),
      'data-open': open ? '1' : '0',
      // Ключ, по которому строку находят клавиши и «развернуть один вопрос».
      'data-q-open': open ? '1' : '0',
    }, head);
    rowEl = row;
    const src = sourceRow(q);
    if (src) {
      src.setAttribute('data-key', 'qsrc');
      row.appendChild(src);
    }
    // W27-E2 §1: «Не актуально» у каждого вопроса — убрать из «Нужен ваш ответ»,
    // не отвечая (с возвратом из «Закрытые»). Встречный вопрос не снимаем: он
    // ждёт пояснения, и это действие человека.
    if (state !== 'waiting_explanation') {
      const stale = questionStaleRow(q);
      stale.setAttribute('data-key', 'qstale');
      row.appendChild(stale);
    }
    if (answered) {
      const block = answerBlock(q, state);
      block.setAttribute('data-key', 'qans');
      row.appendChild(block);
    }
    // Встречный вопрос вопрос не закрывает: форму показываем и после ответа.
    // Снятый вопрос («Это не вопрос») формы не получает — отвечать нечего.
    if (state !== 'dismissed' && (!q.answered || state === 'waiting_explanation')) {
      const form = answerForm(q, state);
      form.setAttribute('data-key', 'qform');
      row.appendChild(form);
    }
    return row;
  }

  /**
   * W25-L2 (R5 §7.2, §9 п. 9): id ЕДИНСТВЕННОГО раскрытого вопроса.
   *
   * До этого открыты были все 7–10 форм сразу: человек с первого экрана получал
   * десять полей и ни одного ответа. Теперь на список — одна форма, остальные
   * строки-заголовки; клик по строке разворачивает её и сворачивает прежнюю.
   * `null` — «ещё не выбирали»: открывается первый вопрос списка.
   * Набранный текст при сворачивании НЕ теряется: поле остаётся в DOM (скрыто
   * стилем) и его значение лежит в `keepValues` (`data-keep`).
   */
  let openQId = null;

  /** Раскрыт ли вопрос: выбранный человеком, иначе первый в списке. */
  function isQOpen(q, firstId) {
    if (!q || !q.id) return false;
    const id = String(q.id);
    if (openQId != null) return openQId === id;
    return String(firstId || '') === id;
  }

  /** Развернуть один вопрос и свернуть остальные (одна форма на список). */
  function expandQuestion(id, listEl) {
    openQId = String(id || '');
    const doc = (typeof document !== 'undefined') ? document : null;
    const rows = listEl && listEl.querySelectorAll ? Array.from(listEl.querySelectorAll('.req-q')) : [];
    for (const row of rows) {
      const on = row.getAttribute('data-q') === openQId;
      row.setAttribute('data-open', on ? '1' : '0');
      const head = row.querySelector ? row.querySelector('.req-q__head') : null;
      if (head) head.setAttribute('aria-expanded', on ? 'true' : 'false');
    }
    const row = rows.find((r) => r.getAttribute('data-q') === openQId);
    const field = row && row.querySelector ? row.querySelector('.req-q__input') : null;
    if (field && typeof field.focus === 'function' && doc && doc.activeElement && doc.activeElement !== field) {
      try { field.focus({ preventScroll: true }); } catch { /* старый браузер */ }
    }
    return openQId;
  }

  /** Кэш строк вопросов: узел живёт между перерисовками, пока вопрос не изменился. */
  const qRows = new Map();

  /** Короткий хеш подписи строки — им помечается сам узел (`data-vsig`). */
  function sigHash(sig) {
    let h = 2166136261;
    for (let i = 0; i < sig.length; i++) {
      h ^= sig.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    return String(h >>> 0);
  }

  function reconcileQuestions(body, questions) {
    const wanted = [];
    const doc = (typeof globalThis !== 'undefined' && globalThis.document) || null;
    const firstId = questions.length ? String((questions[0] && questions[0].id) || '') : '';
    for (const q of questions) {
      if (!q || !q.id) continue;
      const id = String(q.id);
      const open = isQOpen(q, firstId);
      const sig = qSig(q, open);
      const hash = sigHash(sig);
      let entry = qRows.get(id);
      // Кэш мог разойтись с экраном: `applyLivePatch` переносит недостающие узлы
      // из НОВОЙ сборки в живую страницу, и кэшированная строка остаётся пустой
      // оболочкой, а на экране — живая строка с полем и обработчиками
      // (W21-E2E: после «Пояснить» опрос вычищал поле ответа — отвечать нечем).
      // Сверяем по `data-vsig`: атрибуты живой строки патч обновляет сам, так что
      // метка на экране всегда соответствует последней сборке.
      if (doc && typeof doc.querySelector === 'function') {
        const live = doc.querySelector('.req-q[data-q="' + id.replace(/["\\]/g, '\\$&') + '"]');
        if (live && live !== (entry && entry.row) && live.getAttribute('data-vsig') === hash) {
          entry = { sig, row: live };
          qRows.set(id, entry);
        }
      }
      if (!entry || entry.sig !== sig) {
        entry = { sig, row: questionRow(q, open) };
        entry.row.setAttribute('data-vsig', hash);
        qRows.set(id, entry);
      }
      wanted.push(entry.row);
    }
    // Лишние строки убираем; если набор тот же и порядок прежний — НИЧЕГО не
    // трогаем: поле ответа и кнопка микрофона остаются теми же узлами (фокус,
    // курсор и привязка панели диктовки переживают опрос страницы).
    const kids = Array.from(body.childNodes || []).filter((n) => n && n.nodeType === 1);
    for (const row of kids) {
      if (wanted.includes(row)) continue;
      if (typeof body.removeChild === 'function') body.removeChild(row);
      else if (typeof row.remove === 'function') row.remove();
    }
    const now = Array.from(body.childNodes || []).filter((n) => n && n.nodeType === 1);
    const sameOrder = now.length === wanted.length && now.every((n, i) => n === wanted[i]);
    if (!sameOrder) {
      for (const row of wanted) {
        if (row.parentNode === body && typeof body.removeChild === 'function') body.removeChild(row);
      }
      for (const row of wanted) body.appendChild(row);
    }
  }

  /**
   * Убрать из кэша строки вопросов, которых больше нет в запросе.
   * Вызывается ОДИН раз на сборку колонки: если чистить внутри
   * `reconcileQuestions`, второй список (ответы) выбросит из кэша строки
   * первого (открытые вопросы) — и при следующей сборке они пересоберутся,
   * потеряв фокус, набранный текст и привязку панели диктовки.
   */
  function pruneQuestionRows(all) {
    const list = Array.isArray(all) ? all : [];
    for (const id of Array.from(qRows.keys())) {
      if (!list.some((q) => q && String(q.id) === id)) qRows.delete(id);
    }
  }

  /** Одна общая подсказка блока вопросов: что делает «Пояснить…» (B15, B-44). */
  function answerHintLine() {
    return h('div', { class: 'req-side__hint req-q__common-hint small muted' },
      icon('comment-discussion', { size: 11 }),
      h('span', null, ANSWERS_RU.explainHint));
  }

  /** Время ближайшего обновления свода «14:20» из unix-секунд ('' — времени нет). */
  function nextUpdateLabel(at) {
    const sec = Number(at) || 0;
    if (!sec) return '';
    const date = new Date(sec * 1000);
    if (!isFinite(date.getTime())) return '';
    const hh = String(date.getHours()).padStart(2, '0');
    const mm = String(date.getMinutes()).padStart(2, '0');
    return hh + ':' + mm;
  }

  /**
   * Низ блока «Нужен ваш ответ» — что происходит с ответами дальше.
   *
   * W21-Q1 (B-45): пока автообновление свода включено (`synthesis.live.auto`),
   * руководитель забирает ответы сам, поэтому кнопки «Отправить руководителю
   * накопленные ответы» нет, а вместо «N ответа без реакции» — спокойная строка
   * «Ответы переданы руководителю — он учтёт их в ближайшем обновлении свода
   * (около 14:20)» с временем из `synthesis.live.next_update_at`.
   * Кнопка остаётся ТОЛЬКО при выключенном автообновлении: тогда ответы правда
   * лежат и ждут нажатия.
   */
  function resendBlock(resend) {
    if (!resend.can_resend) return null;
    if (resend.auto) {
      const at = nextUpdateLabel(resend.next_at);
      const base = resend.hint || ANSWERS_RU.delivered;
      const line = at ? base + ' (около ' + at + ')' : ANSWERS_RU.deliveredSoon;
      return h('div', {
        class: 'req-side__foot req-side__delivered small muted',
        'data-auto-svod': 'true',
        title: 'Руководитель получает ответы сам — досылать ничего не нужно.',
      }, icon('refresh', { size: 12 }), h('span', null, line));
    }
    const n = Math.max(0, Number(resend.pending) || 0);
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req-side__resend',
      'data-action': 'answers-resend',
      title: 'Одна карточка руководителю: все ваши ответы, которые ещё не учтены',
    }, icon('send', { size: 12 }), String(resend.label || 'Отправить руководителю накопленные ответы'));
    btn.addEventListener('click', () => resendAnswers(btn));
    return h('div', { class: 'req-side__foot req-side__resend-row' },
      btn,
      n ? h('span', { class: 'muted req-q__hint', title: 'Автообновление свода выключено — ответы уйдут по кнопке.' },
        pendingAnswersLabel(n)) : null);
  }

  /**
   * Закреплённый блок «Нужен ваш ответ (N)»: вопросы, ждущие ответа, решения,
   * ждущие слова Михаила, и заблокированные задачи этой ветки. Всегда раскрыт.
   * Число N и расшифровка — из `inboxSummary` (тот же счётчик, что на вкладке
   * «От вас нужно»): одно число, одни слова (B13).
   */
  function answersCard(view) {
    const total = view.inbox.total;
    const card = h('section', {
      class: 'card req-side req-side--pin',
      id: 'questions',
      'data-questions': '',
      'data-key': 'blk-answers',
      'aria-label': 'Нужен ваш ответ',
    });
    // W26-D2 (в): «Нужен ваш ответ» сворачивается — человек сам решает, держать ли
    // список перед глазами. По умолчанию раскрыт, если есть что решать.
    const details = h('details', {
      class: 'req-side__fold',
      'data-fold': 'answers',
      open: foldState(rid, 'answers', Boolean(total)) ? true : null,
    });
    const summary = h('summary', { class: 'req-side__fold-head req-side__fold-head--pin' });
    const box = h('div', { class: 'req-side__fold-body', 'data-answers-body': '' });
    details.appendChild(summary);
    details.appendChild(box);
    details.addEventListener('toggle', () => setFoldState(rid, 'answers', Boolean(details.open)));
    card.appendChild(details);
    paintAnswers(summary, box);
    // Одно действие — везде: действие в основном блоке, нижнем листе или в
    // «Сегодня» приходит сюда событием стора, и колонка перерисовывается сама.
    if (R.store || storeRef) bindLive(storeOf(), card, () => paintAnswers(summary, box));
    return card;
  }

  /**
   * Строка «Закрыть все неактуальные (N)» (W27-E2 §1).
   *
   * Завершённый запрос: пункты ждут человека, хотя работа уже сделана. Массовое
   * действие сервера закрывает их и НЕ трогает решённое человеком — поэтому это
   * не «удалить», а «убрать из счёта, оставив в истории».
   */
  function staleActionRow(total) {
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req-side__stale-btn',
      'data-action': 'dismiss-stale',
      title: 'Закрыть неактуальное у всех завершённых запросов комнаты. '
        + 'Решённое человеком останется; закрытое можно вернуть',
    }, icon('circle-x', { size: 12 }), 'Закрыть все неактуальные (' + String(total) + ')');
    btn.addEventListener('click', async (ev) => {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (btn.disabled) return;
      // W28-D7: тот же общий замок (снимается в `finally` и по таймауту).
      await locked(btn, async () => {
        try {
          let res = null;
          if (storeRef && typeof storeRef.dismissStale === 'function') res = await storeRef.dismissStale();
          else if (ctx.api) {
            res = await ctx.api.post(`/api/rooms/${encodeURIComponent(R.roomId)}/dismiss_stale`,
              { scope: 'finished_requests' });
          }
          const n = res && res.closed
            ? ['questions', 'decisions', 'approvals', 'steps']
              .reduce((sum, key) => sum + (Number(res.closed[key]) || 0), 0)
            : 0;
          toastOnce(ctx.toast, 'dismiss-stale',
            n ? 'Закрыто неактуальных: ' + String(n) + ' — вернуть можно кнопкой «Вернуть»'
              : 'Неактуальных пунктов не осталось', n ? 'ok' : 'info');
          await refresh({ force: true, quiet: true });
        } catch (err) {
          if (isAlreadyDecided(err)) return;
          toastOnce(ctx.toast, 'dismiss-stale', (err && err.message) || 'Не удалось закрыть неактуальные', 'danger');
        }
      }, { label: 'Закрытие неактуальных' });
    });
    return h('div', { class: 'req-side__stale', role: 'group', 'aria-label': 'Неактуальные пункты' },
      h('div', { class: 'req-side__stale-text small muted' },
        icon('history', { size: 12 }),
        h('span', null, 'Работа по запросу закончена. Неактуальное можно закрыть разом: '
          + 'счётчик «ждёт вас» обнулится, решённое вами останется.')),
      btn);
  }

  /**
   * «Закрытые» — свёрнутый список снятых вопросов с причиной и «Вернуть»
   * (W27-E2 §1: у каждого пункта, ушедшего из «Нужен ваш ответ», есть возврат).
   */
  function closedQuestions(list) {
    if (!Array.isArray(list) || !list.length) return null;
    const rows = list.map((q) => {
      const why = closedReasonText(q) || 'закрыто как неактуальное';
      return h('div', { class: 'req-side__closed-row', 'data-closed-question': String(q.id || '') },
        h('div', { class: 'req-side__closed-text' },
          h('div', { class: 'req-side__closed-q' }, firstLine(q.text, 120)),
          h('div', { class: 'small muted' }, why)),
        questionStaleRow(Object.assign({}, q, { state: 'dismissed', dismissed: true })));
    });
    return h('details', { class: 'req-side__closed', 'data-closed-questions': String(list.length) },
      h('summary', { class: 'req-side__closed-sum small muted' },
        icon('chevron-right', { size: 11 }),
        'Закрытые вопросы: ' + String(list.length)),
      h('div', { class: 'req-side__closed-body' }, rows));
  }

  /**
   * Содержимое «Нужен ваш ответ»: только пункты с действием (W26 §1, T1).
   * Перерисовывается из стора, не дожидаясь опроса страницы.
   */
  function paintAnswers(summary, box) {
    const view = asideData();
    const total = view.inbox.total;
    fill(summary,
      icon('chevron-right', { size: 12, className: 'req-side__fold-chev' }),
      h('span', { class: 'req-side__fold-title' }, 'Нужен ваш ответ'),
      h('span', {
        class: 'chip chip--' + (total ? 'warn' : 'muted') + ' req-side__fold-count tnum',
        'data-needs-you': 'total',
        title: (total ? 'Из чего складывается число: ' + view.inbox.full : 'Сейчас от вас ничего не нужно')
          + (view.inbox.fromServer ? ' · счёт сервера' : ''),
      }, String(total)));
    fill(box);

    // W27-E2 §1: массовое закрытие неактуального у завершённого запроса — в самом
    // верху колонки: человек видит, что счётчик можно обнулить одним действием.
    if (view.stale && view.stale.total > 0) {
      box.appendChild(staleActionRow(view.stale.total));
    }

    if (total) {
      box.appendChild(h('div', {
        class: 'req-side__count-line small muted',
        title: 'Из чего складывается число: ' + view.inbox.full,
      }, view.inbox.text));
    }

    if (!total) {
      box.appendChild(h('div', { class: 'req-side__body' },
        h('div', { class: 'req-aside-empty' }, 'Сейчас от вас ничего не нужно: команда продолжает работу.')));
      const quietClosed = closedQuestions(view.dismissedQs);
      if (quietClosed) box.appendChild(quietClosed);
      const quiet = resendBlock(view.resend);
      if (quiet) box.appendChild(quiet);
      return;
    }

    if (view.approvals && view.approvals.length) {
      // W22-AP2: сначала разрешения — участник стоит и не может работать.
      // W26-D2: у каждого пункта есть действие («нельзя штатно» — тоже).
      box.appendChild(h('div', { class: 'req-side__subhead small muted' },
        'Участник просит разрешение',
        h('span', { class: 'tnum' }, ' · ' + view.approvals.length)));
      box.appendChild(h('div', {
        class: 'req-side__body ap-aside', 'data-scroll-key': 'approvals',
        'data-approvals-list': '',
      }, approvalsUI.pendingCards(view.approvals)));
    }

    if (view.openQs.length) {
      // Первый вопрос списка открыт по умолчанию (R5 §7.2): пока человек не
      // выбрал другой, `openQId` = null и раскрыт ровно первый.
      if (openQId == null && view.openQs[0]) openQId = null;
      box.appendChild(answerHintLine());
      const list = h('div', {
        class: 'req-side__body req-q__list', 'data-question-list': '',
        'data-hotkey-list': 'questions',
        title: 'J — следующий вопрос, K — предыдущий',
        'data-scroll-key': 'questions-open',
      });
      reconcileQuestions(list, view.openQs);
      box.appendChild(list);
    }

    if (view.openDecisions.length) {
      box.appendChild(h('div', { class: 'req-side__subhead small muted' },
        'Решения, которые ждут вашего слова',
        h('span', { class: 'tnum' }, ' · ' + view.openDecisions.length)));
      box.appendChild(decisionHintLine());
      box.appendChild(h('div', { class: 'req-side__body req-decisions', 'data-scroll-key': 'decisions-open' },
        view.openDecisions.map(decisionRow)));
    }

    if (view.blocked.length) {
      // Заблокированные задачи считаются в том же числе, что и бейдж «От вас нужно»;
      // здесь — короткая строка со ссылкой: разбираются они на вкладке «От вас нужно».
      box.appendChild(h('a', {
        class: 'req-side__blocked',
        href: `#/room/${encodeURIComponent(R.roomId)}/backlog?request=${encodeURIComponent(R.detail.id)}`,
        title: view.blocked.map((b) => b.text).join('; '),
      }, icon('stop-circle', { size: 12 }),
        h('span', null, countNoun(view.blocked.length, ['задача', 'задачи', 'задач'])
          + ' ждёт вашего решения — разобрать в «От вас нужно»')));
    }

    const foot = resendBlock(view.resend);
    if (foot) box.appendChild(foot);
    // W27-E2 §1: «Закрытые» — внизу, свёрнуто, с причиной и возвратом.
    const closedBox = closedQuestions(view.dismissedQs);
    if (closedBox) box.appendChild(closedBox);
  }

  /* ── решения руководителя: принять / отклонить / обсудить (W19-D2, B-20) ── */

  /** Решение ещё ждёт слова Михаила: сервер отдаёт `open`, старая запись — по статусу. */
  function decisionOpen(d) {
    if (d && typeof d.open === 'boolean') return d.open;
    const status = String((d && d.status) || 'proposed');
    return status === 'proposed' || status === 'discuss';
  }

  /** Понятная расшифровка блока решений: что происходит после ответа (B-20, B-21, B-37). */
  function decisionHintLine() {
    return h('div', { class: 'req-side__hint req-decision__hint small muted', title: CHAIN_HINT },
      icon('comment-discussion', { size: 11 }),
      h('span', null, 'Решение за вами: «Принять» или «Отклонить» — и оно уйдёт из этого '
        + 'списка; у ответа видна цепочка «отправлен → прочитал → учтено». '
        + '«Снять — неактуально» убирает ненужное решение: оно останется серым, вернуть можно в любой момент.'));
  }

  /** Отправить «Принять» сразу: одно нажатие, без окна (остальное — с текстом и диктовкой). */
  async function acceptDecision(d, btn) {
    // W28-D7: общий замок — снимается в `finally` (и по таймауту), а не только в ошибке.
    await locked(btn, async () => {
      try {
        await postDecision({ api: ctx.api, requestId: rid, decision: d, outcome: 'accept' });
        ctx.toast('Решение принято — руководитель увидит ответ', 'ok');
        await refresh({ force: true });
      } catch (err) {
        ctx.toast((err && err.message) || 'Не удалось принять решение', 'danger');
      }
    }, { label: 'Принятие решения' });
  }

  /** «Отклонить» и «Обсудить» — окно с полем и диктовкой (decision-dialog.js). */
  function openDecisionAction(d, outcome) {
    const dlg = openDecisionDialog({
      api: ctx.api,
      requestId: rid,
      decision: d,
      outcome,
      toast: ctx.toast,
      refresh: () => refresh({ force: true }),
    });
    if (!dlg) ctx.toast('Не удалось открыть окно решения', 'danger');
    return dlg;
  }

  /** «Вернуть» снятое решение: одно нажатие, без окна (W20 §1.2, B-37). */
  async function restoreDecision(d, btn) {
    await locked(btn, async () => {
      try {
        await postDecision({ api: ctx.api, requestId: rid, decision: d, outcome: 'restore' });
        ctx.toast('Решение возвращено в работу — снова ждёт вашего слова', 'ok');
        await refresh({ force: true });
      } catch (err) {
        ctx.toast((err && err.message) || 'Не удалось вернуть решение', 'danger');
      }
    }, { label: 'Возврат решения' });
  }

  /**
   * «Снять — неактуально» / «Вернуть» (W20 §1.2, B-37). Живут отдельной строкой
   * от трёх привычных действий: снятие — уборка списка, а не ответ руководителю.
   * @param {object} d решение
   * @returns {HTMLElement|null}
   */
  function decisionMutedRow(d) {
    const items = decisionMutedActions(d);
    if (!items.length) return null;
    const box = h('div', {
      class: 'req-decision__muted-actions', role: 'group',
      'aria-label': 'Решение неактуально: ' + firstLine(d.text, 60),
    });
    for (const item of items) {
      const btn = h('button', {
        type: 'button', class: 'btn btn--ghost btn--sm req-decision__muted-act',
        'data-action': 'decision-' + item.action,
        'data-decision-id': String(d.id || ''),
        title: item.hint,
        'aria-label': item.label + ': ' + firstLine(d.text, 60),
      }, icon(item.action === 'dismiss' ? 'circle-slash' : 'history', { size: 11 }), item.label);
      btn.addEventListener('click', () => {
        if (item.action === 'restore') restoreDecision(d, btn);
        else openDecisionAction(d, 'dismiss');
      });
      box.appendChild(btn);
    }
    return box;
  }

  /** Кнопки решения. Текущее действие помечено `aria-pressed` — видно, что уже сделано. */
  function decisionActionsRow(d) {
    const box = h('div', {
      class: 'req-decision__actions', role: 'group',
      'aria-label': 'Действия по решению: ' + firstLine(d.text, 60),
    });
    for (const item of decisionActions(d)) {
      const tone = item.action === 'accept' ? 'btn--primary'
        : item.action === 'reject' ? 'btn--secondary' : 'btn--ghost';
      const label = item.action === 'discuss' && !/…$/.test(item.label)
        ? item.label + '…' : item.label;
      const btn = h('button', {
        type: 'button',
        class: 'btn btn--sm ' + tone + ' req-decision__act',
        'data-action': 'decision-' + item.action,
        'data-decision-id': String(d.id || ''),
        'aria-pressed': item.current ? 'true' : 'false',
        title: item.hint,
        'aria-label': item.label + ': ' + firstLine(d.text, 60),
      }, label);
      btn.addEventListener('click', () => {
        if (item.action === 'accept') acceptDecision(d, btn);
        else openDecisionAction(d, item.action);
      });
      box.appendChild(btn);
    }
    return box;
  }

  /** Цепочка ответа у решения: «ответ отправлен → руководитель прочитал → учтено». */
  function decisionAnswerBlock(d, answer) {
    const reaction = answer.reaction || null;
    const cards = reaction && Array.isArray(reaction.cards) ? reaction.cards.filter(Boolean).slice(0, 6) : [];
    const at = Number(d.answered_at) || 0;
    // W21-L1 (U4): «Решение учтено · 13:48» — рядом с цепочкой, без повтора её слов.
    const noticed = decisionNotice(d);
    const block = h('div', { class: 'req-decision__answer req-decision__answer--' + answer.key },
      h('div', { class: 'req-decision__answer-head' },
        h('span', { class: 'req-decision__pair small muted' }, 'Ваш ответ'),
        h('span', {
          class: 'chip chip--' + answer.tone + ' req-decision__state',
          title: 'Статус ответа: ' + answer.label + ' — ' + answer.hint,
        }, answer.label)),
      noticed
        ? h('div', {
          class: 'req-q__notice req-decision__notice', role: 'status',
          title: 'Реакция руководителя на ваш ответ по этому решению',
        }, icon('check', { size: 12 }),
        h('span', { class: 'req-q__notice-text' }, noticed))
        : null,
      answer.text ? h('div', { class: 'req-decision__atext' }, String(answer.text)) : null,
      at ? h('div', { class: 'muted req-q__when' }, 'отвечено ' + timeAgo(at)) : null);
    if (reaction && reaction.text) {
      block.appendChild(h('div', { class: 'req-q__reaction' },
        h('div', { class: 'req-q__reaction-head' },
          icon('comment-discussion', { size: 12 }),
          h('span', { class: 'req-q__cap' }, 'Руководитель'),
          Number(reaction.at) ? h('span', { class: 'muted req-q__when' }, timeAgo(Number(reaction.at))) : null),
        h('div', { class: 'req-q__reaction-text' }, String(reaction.text)),
        cards.length
          ? h('div', { class: 'req-q__cards' }, cards.map((id) => h('span', {
            class: 'req-q__card tnum', title: 'Карточка, которой коснулся ответ: ' + id,
          }, String(id))))
          : null));
    } else if (answer.pending) {
      block.appendChild(h('div', { class: 'req-decision__pending' },
        h('span', { class: 'req-q__pending-label' },
          icon('clock', { size: 12 }),
          h('span', null, answer.overdue
            ? 'руководитель ещё не ответил — ждём' : 'руководитель ещё не ответил'))));
    }
    return block;
  }

  function decisionRow(d) {
    const status = decisionStatusOf(d);
    const answer = decisionAnswerOf(d);
    // W20 §1.2 (B-37): снятое и устаревшее — серым, без ожидания ответа.
    const muted = Boolean(d.status_muted) || status.key === 'dismissed' || status.key === 'superseded';
    const cls = ['req-decision', 'req-decision--' + status.key];
    if (muted) cls.push('req-decision--muted');
    const row = h('div', {
      class: cls.join(' '),
      'data-key': 'dec' + String(d.id || ''),
      'data-decision-id': String(d.id || ''),
      'data-decision-status': status.key,
      'data-muted': muted ? 'true' : null,
    },
      h('div', { class: 'req-decision__top' },
        h('div', { class: 'req-decision__text' }, d.text || ''),
        h('span', {
          class: 'chip chip--' + status.tone + ' req-decision__status', title: status.hint,
        }, status.label)),
      d.why ? h('div', { class: 'req-decision__why' }, d.why) : null,
      status.key === 'superseded' ? supersededNote(d) : null);
    // Снятое и устаревшее не отвечают: цепочка «ваш ответ → прочитал → учтено» здесь
    // только сбивала бы с толку (сервер отдаёт для них состояние «снято»).
    if (answer && !muted) row.appendChild(decisionAnswerBlock(d, answer));
    // Действия — только пока решение ждёт слова Михаила (B-20: «предложено»).
    if (decisionOpen(d)) row.appendChild(decisionActionsRow(d));
    const mutedRow = decisionMutedRow(d);
    if (mutedRow) row.appendChild(mutedRow);
    return row;
  }

  /** Почему решение устарело — одной строкой (W20 §1.2, B-37). */
  function supersededNote(d) {
    const by = Number(d.superseded_by) || 0;
    const why = String(d.superseded_why || '');
    const reason = why === 'retracted' ? 'руководитель его отозвал'
      : why === 'absent' ? 'его нет в новой версии свода' : '';
    const parts = [];
    if (by) parts.push('с версии ' + by);
    if (reason) parts.push(reason);
    const text = parts.length ? 'Устарело: ' + parts.join(' · ') : 'Устарело — в работу не идёт';
    return h('div', { class: 'req-decision__superseded small muted', title: text }, text);
  }

  function decisionsCard(decisions) {
    return foldCard({
      rid: R.detail && R.detail.id,
      name: 'decisions',
      title: 'Решения',
      count: countNoun(decisions.length, ['решение', 'решения', 'решений']),
      hint: 'Принятые и отклонённые решения. Статус ответа — «отправлен → прочитал → учтено».',
      defaultOpen: false,
      className: 'req-decisions-card',
      key: 'blk-decisions',
      body: h('div', { class: 'req-side__body req-decisions', 'data-scroll-key': 'decisions' },
        decisions.map(decisionRow)),
    });
  }

  function futureCard(items) {
    const body = h('div', { class: 'req-side__body', 'data-scroll-key': 'future' });
    for (const text of items) {
      const btn = h('button', {
        type: 'button', class: 'btn btn--ghost btn--sm',
        title: 'Создать запрос из этого вопроса',
      }, 'Задать');
      btn.addEventListener('click', () => askFuture(String(text), btn));
      body.append(h('div', { class: 'req-future' },
        h('div', { class: 'req-future__text' }, String(text)),
        btn));
    }
    return foldCard({
      rid: R.detail && R.detail.id,
      name: 'future',
      title: 'Будущие вопросы',
      count: countNoun(items.length, ['вопрос', 'вопроса', 'вопросов']),
      hint: 'То, что стоит спросить позже — можно превратить в отдельный запрос.',
      defaultOpen: false,
      className: 'req-future-card',
      key: 'blk-future',
      body,
    });
  }

  /** «Отвечено (M)»: вопросы с ответом, статус и реакция — свёрнуто, чтобы не мешать. */
  function answeredCard(questions) {
    const body = h('div', {
      class: 'req-side__body req-q__list',
      'data-question-list': 'answered',
      'data-scroll-key': 'questions-answered',
    });
    reconcileQuestions(body, questions);
    return foldCard({
      rid: R.detail && R.detail.id,
      name: 'answered',
      title: 'Отвечено',
      count: countNoun(questions.length, ['вопрос', 'вопроса', 'вопросов']),
      defaultOpen: false,
      className: 'req-answered',
      key: 'blk-answered',
      body,
    });
  }

  /** Название этапа из строки или объекта комнаты (room.phase). */
  function phaseName(phase) {
    if (!phase) return '';
    if (typeof phase === 'string') return phase.trim();
    if (typeof phase !== 'object') return '';
    return String(phase.title || phase.name || phase.id || '').trim();
  }

  function stageCard(stage) {
    const phases = Array.isArray(stage.phases) ? stage.phases : [];
    const cur = phases.find((p) => p.id === stage.current) || null;
    const statusRu = { done: 'готово', active: 'в работе', blocked: 'ждёт', next: 'не начато' };
    const bar = h('div', {
      class: 'req-stage__bar', role: 'img',
      'aria-label': 'Фазы проекта: ' + phases.map((p) => `${p.title} — ${statusRu[p.status] || 'не начато'}`).join('; '),
    }, phases.map((p) => h('span', {
      class: `req-stage__seg req-stage__seg--${p.status || 'next'}`,
      title: `${p.title}: ${statusRu[p.status] || 'не начато'}`,
    })));

    // Два разных смысла называем явно: этап всего проекта и этап, к которому
    // относится этот запрос (D9 — раньше они выглядели как противоречие).
    const project = cur ? String(cur.title || '') : '';
    const own = phaseName(R.ctx && R.ctx.room && R.ctx.room.phase);
    const row = (label, value) => h('div', { class: 'req-stage__row' },
      h('span', { class: 'req-stage__label' }, label),
      h('span', { class: 'req-stage__value' }, value));

    const link = h('a', { class: 'req-stage', href: `#/room/${encodeURIComponent(R.roomId)}/roadmap` },
      row('Этап проекта', project || 'не выбран'),
      bar,
      own && own !== project ? row('Этот запрос', own) : null,
      cur && cur.note ? h('div', { class: 'req-stage__note' }, cur.note) : null,
      h('div', { class: 'req-stage__map' }, 'Вся карта →'));

    return foldCard({
      rid: R.detail && R.detail.id,
      name: 'stage',
      title: 'Карта проекта',
      count: null,
      defaultOpen: true,
      className: 'req-stage-card',
      key: 'blk-stage',
      body: link,
    });
  }

  /** Данные колонки в одном месте: что ждёт ответа, что уже отвечено, что свёрнуто. */
  function asideData() {
    // W26-D2: деталь берём из общего стора — он знает и правки, пришедшие из
    // других мест страницы, а не только из последнего опроса.
    const detail = (storeRef && storeRef.get().detail) || R.detail || {};
    const steps = Array.isArray(detail.next_steps) ? detail.next_steps : [];
    const questions = Array.isArray(detail.open_questions) ? detail.open_questions : [];
    const decisions = Array.isArray(detail.decisions) ? detail.decisions : [];
    const future = Array.isArray(detail.future_questions) ? detail.future_questions : [];

    const openQs = questions.filter((q) => q && qOpen(q));
    const answeredQs = questions.filter((q) => q && !qOpen(q) && (q.answer || q.answered));
    // W27-E2 §1: снятые вопросы («Не актуально»/«Это не вопрос») — в «Закрытые»,
    // с причиной и кнопкой «Вернуть», а не молча мимо экрана.
    const dismissedQs = questions.filter((q) => q && !qOpen(q)
      && (qState(q) === 'dismissed' || q.dismissed === true));
    const openDecisions = decisions.filter((d) => d && decisionOpen(d));
    const closedDecisions = decisions.filter((d) => d && !decisionOpen(d));
    // Заблокированные задачи этой ветки: тот же источник, что у вкладки «От вас нужно».
    let blocked = [];
    try { blocked = blockedCards([detail]); } catch { blocked = []; }
    // W22-AP2: запросы разрешений агентам — отдельный вид элемента «Нужен ваш ответ».
    const approvals = approvalsUI.pending();

    const inbox = inboxSummary({ questions: openQs, decisions: openDecisions, blocked, approvals,
      // W27-E2 §1: число «ждёт вас» — с сервера (`needs_you_count`), не пересчёт.
      counts: needsYouCount(detail) });
    const summary = (detail.answers && detail.answers.summary) || {};
    // W21-Q1 (B-45): автообновление свода включено? Тогда ответы забирает
    // руководитель сам, и кнопка досылки не нужна. Нет поля — считаем выключенным
    // (старый сервер): кнопка ведёт себя как раньше.
    const live = (detail.synthesis && detail.synthesis.live) || {};
    return {
      steps, questions, decisions, future,
      openQs, answeredQs, dismissedQs, openDecisions, closedDecisions, blocked, inbox, approvals,
      counts: needsYouCount(detail),
      // W27-E2 §1: сколько пунктов можно закрыть разом у завершённого запроса.
      stale: staleSummary(detail, {
        isOpenQuestion: (q) => Boolean(q) && !q.answered && !q.dismissed,
        isOpenDecision: (d) => Boolean(d) && decisionOpen(d),
      }),
      resend: {
        can_resend: Boolean(summary.can_resend),
        pending: Number(summary.resend_pending) || 0,
        label: String(summary.resend_label || 'Отправить руководителю накопленные ответы'),
        hint: String(summary.resend_hint || ''),
        auto: live.auto === true,
        next_at: Number(live.next_update_at) || 0,
      },
    };
  }

  /* ── плашка «Нужен ваш ответ · N → показать» (W21-R1, п. 4) ──────────────── */

  /**
   * Плашка над содержимым страницы: в одноколоночном режиме блок вопросов уходит
   * под свод, и без неё «Нужен ваш ответ» прятался глубоко под страницей.
   * Видимость — CSS (`@container view`): в двух колонках её нет, плашку прячет
   * стиль, а не JS (иначе при смене ширины оставался бы «мёртвый» элемент).
   */
  function pinBar(view) {
    const total = Math.max(0, Number(view.inbox.total) || 0);
    const node = h('button', {
      class: 'req-pinbar', type: 'button', 'data-pinbar': '',
      title: LAYOUT_RU.pinbarTitle,
      // Кнопка ведёт к блоку «Нужен ваш ответ» — говорим об этом явно.
      'aria-controls': 'questions',
    },
    h('span', { class: 'req-pinbar__count', 'aria-hidden': 'true' }, String(total)),
    h('span', { class: 'req-pinbar__text' }, LAYOUT_RU.pinbar),
    h('span', { class: 'req-pinbar__go' }, LAYOUT_RU.pinbarShow, icon('arrow-down', { size: 13 })));
    node.addEventListener('click', () => showQuestions());
    return node;
  }

  /** Перейти к вопросам: прокрутка к блоку, короткая подсветка, фокус в ответ. */
  function showQuestions() {
    const target = document.getElementById('questions');
    if (!target) return;
    try {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch {
      target.scrollIntoView();
    }
    target.classList.remove('is-flash');
    // Перезапуск анимации: без паузы класс не «переигрывает».
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => target.classList.add('is-flash'));
    else target.classList.add('is-flash');
    setTimeout(() => target.classList.remove('is-flash'), 1500);
    const field = target.querySelector('.req-q__input, textarea, input[type="text"]');
    if (field && typeof field.focus === 'function') {
      try { field.focus({ preventScroll: true }); } catch { /* старый браузер */ }
    }
  }

  /**
   * Поставить плашку первым элементом страницы `.req` (над сводом). Именно там,
   * а не в сетке: sticky внутри ячейки сетки не «прилипает» — у ячейки нет
   * высоты, по которой можно ехать. Вызывается после вставки колонки в DOM.
   */
  function mountPinBar(node, aside) {
    const grid = aside && aside.parentElement;
    const page = grid && grid.parentElement;
    if (!page || !page.querySelectorAll) return;
    for (const old of page.querySelectorAll('.req-pinbar')) {
      if (old !== node) old.remove();
    }
    page.insertBefore(node, page.firstChild);
  }

  function buildAside() {
    const wrap = h('aside', {
      class: 'req__aside',
      'aria-label': 'От вас нужно по этому запросу',
      // WAVE17 §1: прокрутка колонки переживает точечное обновление страницы.
      'data-scroll-key': 'request-aside',
    });
    const view = asideData();

    // Понятно, к чему относится колонка (WAVE8, Д3): только этот запрос + выход в комнату.
    const backlogHref = `#/room/${encodeURIComponent(R.roomId)}/backlog?request=${encodeURIComponent(R.detail.id)}`;
    /**
     * W26-QA2: «Дело из запроса» — дело открывается одним нажатием.
     * До этого из карточки запроса в «Дела» пути не было: человек видел «Все дела
     * комнаты →», а создать дело мог только API-вызовом (тупик по правилу «без
     * тупиков»). Одно дело на запрос: если оно уже есть, сервер отвечает 409 с id —
     * ведём в существующее, а не показываем ошибку.
     */
    async function openDelo(btn) {
      const apiRef = (ctx && ctx.api) || null;
      const go = (ctx && ctx.navigate) || null;
      const say = (ctx && ctx.toast) || (() => {});
      if (!apiRef || !R.roomId) return;
      // W28-D7: под общим замком — он снимается и в `finally`, и по таймауту
      // (раньше зависший запрос оставлял кнопку выключенной навсегда).
      await locked(btn, async () => {
        try {
          const res = await apiRef.post('/api/rooms/' + encodeURIComponent(R.roomId) + '/delos',
            { request_id: rid });
          const id = String((res && (res.id || (res.item && res.item.id))) || '');
          say(id ? 'Дело открыто — этапы, результат и письмо наружу' : 'Дело открыто', 'ok');
          if (id && go) go('#/delo/' + encodeURIComponent(id));
        } catch (err) {
          const details = (err && (err.details || err.detail)) || null;
          const id = String((details && ((details.detail && details.detail.id) || details.id)) || '');
          if (id) {
            say('Дело по этому запросу уже есть — открываю его', 'info');
            if (go) go('#/delo/' + encodeURIComponent(id));
          } else {
            say((err && err.message) || 'Не удалось открыть дело', 'error');
          }
        }
      }, { label: 'Открытие дела' });
    }

    wrap.append(h('div', { class: 'req-aside__scope row row--between row--wrap row--tight' },
      h('span', { class: 'req-aside__scope-title' }, 'По этому запросу'),
      h('span', { class: 'req-aside__scope-acts row row--tight' },
        h('button', {
          class: 'btn btn--ghost btn--sm req-aside__delo', type: 'button',
          title: 'Открыть дело по этому запросу: этапы, результат, письмо наружу',
          onclick: (e) => openDelo(e && e.currentTarget),
        }, 'Дело из запроса'),
        h('a', { class: 'req-aside__scope-link', href: backlogHref, title: 'Все дела комнаты: шаги, вопросы, решения' },
          'Все дела комнаты →'))));

    // W28-E: строка расхода запроса — «Расход: N вызовов, M тыс. токенов (база/тело/инструменты)».
    // Модуль грузится лениво: колонка не ждёт расхода и не падает, если его нет.
    import('./usage-line.js').then((m) => m.mountUsage(wrap, rid, (ctx && ctx.api) || null))
      .catch(() => {});

    // Закреплённый блок: то, что ждёт Михаила, — первым и всегда раскрытым.
   
    wrap.append(answersCard(view));

    // Ниже — свёрнутые блоки со счётчиками (P3), состояние запоминается.
    if (view.answeredQs.length) wrap.append(answeredCard(view.answeredQs));
    if (view.steps.length) wrap.append(stepsBlock.stepsCard(view.steps));
    if (view.closedDecisions.length) wrap.append(decisionsCard(view.closedDecisions));
    if (view.future.length) wrap.append(futureCard(view.future));
    if (R.detail.stage && Array.isArray(R.detail.stage.phases) && R.detail.stage.phases.length) {
      wrap.append(stageCard(R.detail.stage));
    }
    // Кэш строк вопросов чистим ОДИН раз по всему набору (а не по одному списку).
    pruneQuestionRows(view.questions);

    // W21-R1 п. 4: над сводом — плашка «Нужен ваш ответ · N → показать»
    // (только если ждёт ответа; ставится после вставки колонки в DOM).
    if (view.inbox.total) {
      const bar = pinBar(view);
      if (typeof queueMicrotask === 'function') queueMicrotask(() => mountPinBar(bar, wrap));
      else setTimeout(() => mountPinBar(bar, wrap), 0);
    }

    if (!view.openQs.length && !view.answeredQs.length && !view.steps.length
        && !view.decisions.length && !view.future.length) {
      wrap.append(h('section', { class: 'card req-side' },
        h('div', { class: 'req-aside-empty' },
          'Пока ничего не ждём: команда закончит работу и добавит шаги, вопросы и решения.')));
    }
    return wrap;
  }

  return { buildAside };
}
