/**
 * request-aside.js — правая колонка «Дальше» (WAVE17, пакет D: B1, B3, B13, B15, P3).
 *
 * Устройство колонки (P3): сверху и всегда раскрыто — «Нужен ваш ответ (N)»
 * (вопросы, которые ждут Михаила, решения, которые ждут его слова, и
 * заблокированные задачи этой ветки); ниже — свёрнутые блоки со счётчиками:
 * «Отвечено», «Шаги», «Решения», «Будущие вопросы», «Карта проекта». Состояние
 * свёрнутости запоминается (aside-fold.js).
 *
 * Счётчики — из ОДНОГО источника: `inboxSummary`/`blockedCards` из
 * backlog-model.js — те же функции, что считают бейдж вкладки «Дальше» (B13).
 * Заголовок блока и бейдж вкладки показывают одно число и одну расшифровку.
 *
 * Вопросы: компактная форма (поле + «Надиктовать» от dictation-auto +
 * «Отправить»), одна общая подсказка про «Пояснить…» вместо повтора длинной
 * подписи у каждого вопроса (B15). Статус ответа и реакция руководителя — только
 * из полей API (`answer_state`, `answer_label`, `reaction`), без догадок на
 * клиенте. Если «Напомнить» вернул 409 с найденной реакцией — реакция сразу
 * показывается у вопроса, а кнопка исчезает (B-12).
 *
 * Действие «Отправить руководителю накопленные ответы» (`answers_resend`) —
 * одна кнопка, только когда есть ответы без реакции (`answers.summary.can_resend`).
 *
 * Фабрика createAside(R) получает общий контекст страницы из request.js
 * и возвращает { buildAside }.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { firstLine, personName, blockHead } from './request-util.js';
import { countNoun } from '../text-ru.js';
import { createSteps } from './request-steps.js';
import { blockedCards, inboxSummary } from './backlog-model.js';
import { foldCard } from './aside-fold.js';

/** Подписи статусов ответа — те же слова, что в API (`answer_label`). */
const Q_STATE_RU = {
  open: 'ждёт вашего ответа',
  waiting_explanation: 'ждёт вашего решения после пояснения',
  sent: 'ответ отправлен — руководитель ещё не ответил',
  received: 'принято руководителем',
  applied: 'учтено: работа пересмотрена',
  no_change: 'принято: менять ничего не нужно',
};
const Q_STATE_CHIP = {
  open: 'chip--muted',
  waiting_explanation: 'chip--warn',
  sent: 'chip--warn',
  received: 'chip--info',
  applied: 'chip--ok',
  no_change: 'chip--muted',
};

export function createAside(R) {
  const { ctx, rid, keepValues, refresh } = R;
  ensureCss('css/view-backlog-extras.css');
  // W14-H §12: шаги — план руководителя с окном шага, не галочки.
  const stepsBlock = createSteps(R);

  /**
   * Ответ на конкретный вопрос (W14-D §1–§3). `needExplanation` — встречный
   * вопрос («поясни»): вопрос при этом НЕ закрывается, руководитель обязан
   * объяснить в ветке, поэтому форма остаётся на месте.
   */
  async function answerQuestion(q, ta, btn, opts = {}) {
    const needExplanation = Boolean(opts && opts.needExplanation);
    const text = String(ta.value || '').trim();
    if (!text) {
      ta.focus();
      ctx.toast(needExplanation ? 'Напишите, что пояснить в вопросе' : 'Напишите ответ', 'warn');
      return;
    }
    btn.disabled = true;
    try {
      const body = { action: 'answer', question_id: q.id, text };
      if (needExplanation) body.need_explanation = true;
      await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, body);
      keepValues.delete('q:' + q.id);
      ctx.toast(needExplanation
        ? 'Просьба о пояснении отправлена — вопрос остаётся открытым'
        : 'Ответ отправлен — руководитель увидит его у вопроса', 'ok');
      await refresh({ force: true });
    } catch (err) {
      btn.disabled = false;
      ctx.toast((err && err.message) || 'Не удалось отправить ответ', 'danger');
    }
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
    if (btn) btn.disabled = true;
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
      if (btn) btn.disabled = false;
      ctx.toast((err && err.message) || 'Не удалось отправить напоминание', 'danger');
    }
  }

  async function askFuture(text, btn) {
    if (btn) btn.disabled = true;
    try {
      const res = await ctx.api.post(`/api/rooms/${encodeURIComponent(R.roomId)}/requests`, { text });
      const nextId = res?.root_id || res?.request?.id;
      ctx.toast('Запрос создан', 'ok');
      if (nextId) ctx.navigate(`#/room/${encodeURIComponent(R.roomId)}/request/${encodeURIComponent(nextId)}`);
      else await refresh({ force: true });
    } catch (err) {
      if (btn) btn.disabled = false;
      ctx.toast((err && err.message) || 'Не удалось создать запрос', 'danger');
    }
  }

  /** «Отправить руководителю накопленные ответы» — действие `answers_resend` (W17-B §3). */
  async function resendAnswers(btn) {
    if (btn) btn.disabled = true;
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
      if (btn) btn.disabled = false;
      ctx.toast((err && err.message) || 'Не удалось отправить ответы', 'danger');
    }
  }

  /* ── вопросы: «Вопрос — ваш ответ», статус и реакция руководителя ────── */

  /** Состояние ответа на вопрос: `open|waiting_explanation|sent|received|applied|no_change`. */
  function qState(q) {
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
    if (state === 'waiting_explanation') return true;
    return !(q && q.answered);
  }

  /**
   * Ключ строки вопроса для перерисовки: пока он не изменился, узел НЕ
   * пересобирается. Это не оптимизация — от этого зависит диктовка: поле и
   * кнопка микрофона остаются теми же узлами, панель не теряет привязку к
   * своему вопросу при опросе, «Стоп» и распознавании (W15-C, п. 4).
   */
  function qSig(q) {
    return JSON.stringify([
      String((q && q.id) || ''), String((q && q.text) || ''), String((q && q.for) || ''),
      Boolean(q && q.answered), String((q && q.answer) || ''), Number((q && q.answered_at) || 0),
      qState(q), Boolean(q && q.needs_explanation), Boolean(q && q.leader_pending),
      Boolean(q && q.can_remind), Boolean(q && q.reaction_overdue),
      Number((q && q.reminded_at) || 0), Boolean(q && q.answer_changes_done),
      JSON.stringify((q && q.reaction) || null),
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

  /** Реакция руководителя на ответ: текст и карточки, которых он коснулся. */
  function reactionBlock(q) {
    const r = q.reaction;
    if (!r || typeof r !== 'object' || !r.text) return null;
    const cards = Array.isArray(r.cards) ? r.cards.filter(Boolean).slice(0, 6) : [];
    const when = Number(r.at) || 0;
    return h('div', { class: 'req-q__reaction' },
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
          title: 'Статус ответа: ' + label }, label)),
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
      placeholder: waiting ? 'Что пояснить в вопросе?' : 'Ваш ответ…',
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
    const explain = h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-q__explain', 'data-action': 'need-explanation',
      title: 'Мне нужно пояснение — руководитель объяснит, вопрос останется открытым',
      'aria-label': 'Мне нужно пояснение к вопросу: ' + firstLine(q.text, 60),
    }, icon('comment-discussion', { size: 12 }), 'Пояснить…');
    explain.addEventListener('click', () => answerQuestion(q, ta, explain, { needExplanation: true }));
    return h('div', { class: 'req-q__form' },
      h('div', { class: 'req-q__row' }, ta, send),
      h('div', { class: 'req-q__actions' }, explain,
        waiting ? h('span', { class: 'muted req-q__hint' },
          'ждём пояснения руководителя — вопрос открыт') : null));
  }

  function questionRow(q) {
    const isMaxim = (q.for || 'mikhail') === 'mikhail';
    const state = qState(q);
    const answered = state === 'waiting_explanation' || Boolean(q.answer) || Boolean(q.answered);
    const row = h('div', {
      class: 'req-q req-q--' + state,
      'data-q': String(q.id || ''),
      'data-q-state': state,
      // WAVE17 §1: ключ точечной перерисовки — строка вопроса не пересобирается,
      // пока не изменился сам вопрос (от этого зависит диктовка и фокус).
      'data-key': 'q' + String(q.id || ''),
      // Ключ привязки панели диктовки: панель находит СВОЙ вопрос после перерисовки.
      'data-mic-anchor': 'q:' + String(q.id || ''),
    }, questionTop(q, isMaxim));
    if (answered) row.appendChild(answerBlock(q, state));
    // Встречный вопрос вопрос не закрывает: форму показываем и после ответа.
    if (!q.answered || state === 'waiting_explanation') row.appendChild(answerForm(q, state));
    return row;
  }

  /** Кэш строк вопросов: узел живёт между перерисовками, пока вопрос не изменился. */
  const qRows = new Map();

  function reconcileQuestions(body, questions) {
    const wanted = [];
    for (const q of questions) {
      if (!q || !q.id) continue;
      const id = String(q.id);
      const sig = qSig(q);
      let entry = qRows.get(id);
      if (!entry || entry.sig !== sig) {
        entry = { sig, row: questionRow(q) };
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

  /** Одна общая подсказка блока вопросов: что делает «Пояснить…» (B15). */
  function answerHintLine() {
    return h('div', { class: 'req-side__hint req-q__common-hint small muted' },
      icon('comment-discussion', { size: 11 }),
      h('span', null, 'Вопрос непонятен? «Пояснить…» — руководитель объяснит, вопрос останется открытым.'));
  }

  /** Кнопка досылки накопленных ответов (W17-B §3) — только по клику. */
  function resendButton(resend) {
    const n = Math.max(0, Number(resend.pending) || 0);
    const btn = h('button', {
      type: 'button', class: 'btn btn--secondary btn--sm req-side__resend',
      'data-action': 'answers-resend',
      title: 'Одна карточка руководителю: все ваши ответы, на которые ещё нет реакции',
    }, icon('send', { size: 12 }), String(resend.label || 'Отправить руководителю накопленные ответы'));
    btn.addEventListener('click', () => resendAnswers(btn));
    return h('div', { class: 'req-side__foot req-side__resend-row' },
      btn,
      n ? h('span', { class: 'muted req-q__hint' },
        n + ' ' + countNoun(n, ['ответ', 'ответа', 'ответов']) + ' без реакции руководителя') : null);
  }

  /**
   * Закреплённый блок «Нужен ваш ответ (N)»: вопросы, ждущие ответа, решения,
   * ждущие слова Михаила, и заблокированные задачи этой ветки. Всегда раскрыт.
   * Число N и расшифровка — из `inboxSummary` (тот же счётчик, что на вкладке
   * «Дальше»): одно число, одни слова (B13).
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
    card.appendChild(blockHead('Нужен ваш ответ', total || null));
    if (total) {
      card.appendChild(h('div', {
        class: 'req-side__count-line small muted',
        title: 'Из чего складывается число: ' + view.inbox.full,
      }, view.inbox.text));
    }

    if (!total) {
      card.appendChild(h('div', { class: 'req-side__body' },
        h('div', { class: 'req-aside-empty' }, 'Сейчас от вас ничего не нужно: команда продолжает работу.')));
      if (view.resend.can_resend) card.appendChild(resendButton(view.resend));
      return card;
    }

    if (view.openQs.length) {
      card.appendChild(answerHintLine());
      const list = h('div', {
        class: 'req-side__body req-q__list', 'data-question-list': '',
        'data-scroll-key': 'questions-open',
      });
      reconcileQuestions(list, view.openQs);
      card.appendChild(list);
    }

    if (view.openDecisions.length) {
      card.appendChild(h('div', { class: 'req-side__subhead small muted' },
        'Решения, которые ждут вашего слова',
        h('span', { class: 'tnum' }, ' · ' + view.openDecisions.length)));
      card.appendChild(h('div', { class: 'req-side__body req-decisions' },
        view.openDecisions.map(decisionRow)));
    }

    if (view.blocked.length) {
      // Заблокированные задачи считаются в том же числе, что и бейдж «Дальше»;
      // здесь — короткая строка со ссылкой: разбираются они на вкладке «Дальше».
      card.appendChild(h('a', {
        class: 'req-side__blocked',
        href: `#/room/${encodeURIComponent(R.roomId)}/backlog?request=${encodeURIComponent(R.detail.id)}`,
        title: view.blocked.map((b) => b.text).join('; '),
      }, icon('stop-circle', { size: 12 }),
        h('span', null, countNoun(view.blocked.length, ['задача', 'задачи', 'задач'])
          + ' ждёт вашего решения — разобрать в «Дальше»')));
    }

    if (view.resend.can_resend) card.appendChild(resendButton(view.resend));
    return card;
  }

  function decisionRow(d) {
    const status = d.status || 'proposed';
    const chip = status === 'accepted'
      ? h('span', { class: 'chip chip--ok' }, 'принято')
      : status === 'rejected'
        ? h('span', { class: 'chip chip--muted' }, 'отклонено')
        : h('span', { class: 'chip chip--warn' }, 'предложено');
    return h('div', { class: 'req-decision', 'data-key': 'dec' + String(d.id || '') },
      h('div', { class: 'req-decision__top' },
        h('div', { class: 'req-decision__text' }, d.text || ''),
        chip),
      d.why ? h('div', { class: 'req-decision__why' }, d.why) : null);
  }

  function decisionsCard(decisions) {
    return foldCard({
      rid: R.detail && R.detail.id,
      name: 'decisions',
      title: 'Решения',
      count: countNoun(decisions.length, ['решение', 'решения', 'решений']),
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
    const detail = R.detail || {};
    const steps = Array.isArray(detail.next_steps) ? detail.next_steps : [];
    const questions = Array.isArray(detail.open_questions) ? detail.open_questions : [];
    const decisions = Array.isArray(detail.decisions) ? detail.decisions : [];
    const future = Array.isArray(detail.future_questions) ? detail.future_questions : [];

    const openQs = questions.filter((q) => q && qOpen(q));
    const answeredQs = questions.filter((q) => q && !qOpen(q) && (q.answer || q.answered));
    const openDecisions = decisions.filter((d) => d && (d.status || 'proposed') === 'proposed');
    const closedDecisions = decisions.filter((d) => d && (d.status || 'proposed') !== 'proposed');
    // Заблокированные задачи этой ветки: тот же источник, что у вкладки «Дальше».
    let blocked = [];
    try { blocked = blockedCards([detail]); } catch { blocked = []; }

    const inbox = inboxSummary({ questions: openQs, decisions: openDecisions, blocked });
    const summary = (detail.answers && detail.answers.summary) || {};
    return {
      steps, questions, decisions, future,
      openQs, answeredQs, openDecisions, closedDecisions, blocked, inbox,
      resend: {
        can_resend: Boolean(summary.can_resend),
        pending: Number(summary.resend_pending) || 0,
        label: String(summary.resend_label || 'Отправить руководителю накопленные ответы'),
      },
    };
  }

  function buildAside() {
    const wrap = h('aside', {
      class: 'req__aside',
      'aria-label': 'Дальше по запросу',
      // WAVE17 §1: прокрутка колонки переживает точечное обновление страницы.
      'data-scroll-key': 'request-aside',
    });
    const view = asideData();

    // Понятно, к чему относится колонка (WAVE8, Д3): только этот запрос + выход в комнату.
    const backlogHref = `#/room/${encodeURIComponent(R.roomId)}/backlog?request=${encodeURIComponent(R.detail.id)}`;
    wrap.append(h('div', { class: 'req-aside__scope row row--between row--wrap row--tight' },
      h('span', { class: 'req-aside__scope-title' }, 'По этому запросу'),
      h('a', { class: 'req-aside__scope-link', href: backlogHref, title: 'Все дела комнаты: шаги, вопросы, решения' },
        'Все дела комнаты →')));

    // Закреплённый блок: то, что ждёт Михаила, — первым и всегда раскрытым.
   
    wrap.append(answersCard(view));

    // Дальше — свёрнутые блоки со счётчиками (P3), состояние запоминается.
    if (view.answeredQs.length) wrap.append(answeredCard(view.answeredQs));
    if (view.steps.length) wrap.append(stepsBlock.stepsCard(view.steps));
    if (view.closedDecisions.length) wrap.append(decisionsCard(view.closedDecisions));
    if (view.future.length) wrap.append(futureCard(view.future));
    if (R.detail.stage && Array.isArray(R.detail.stage.phases) && R.detail.stage.phases.length) {
      wrap.append(stageCard(R.detail.stage));
    }
    // Кэш строк вопросов чистим ОДИН раз по всему набору (а не по одному списку).
    pruneQuestionRows(view.questions);

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
