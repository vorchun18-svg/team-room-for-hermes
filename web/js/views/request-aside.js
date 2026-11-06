/**
 * request-aside.js — правая колонка «Дальше»: шаги, вопросы, решения,
 * будущие вопросы, фаза проекта; действия отметки шага, ответа и «Задать».
 *
 * Фабрика createAside(R) получает общий контекст страницы из request.js
 * и возвращает { buildAside }. Поведение прежнее: вынесено из request.js.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { ensureCss } from '../ui.js';
import { firstLine, personName, blockHead } from './request-util.js';
import { countNoun } from '../text-ru.js';
import { createSteps } from './request-steps.js';

export function createAside(R) {
  const { ctx, rid, keepValues, refresh } = R;
  ensureCss('css/view-backlog-extras.css');
  // W14-H §12: «Следующие шаги» — план руководителя с окном шага, не галочки.
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
      ctx.toast(err?.message || 'Не удалось отправить ответ', 'danger');
    }
  }

  /** «Напомнить руководителю»: реакции на ответ до сих пор нет (W14-D §2). */
  async function remindLeader(q, btn) {
    if (btn) btn.disabled = true;
    try {
      await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, {
        action: 'answer_remind', question_id: q.id,
      });
      ctx.toast('Напоминание отправлено руководителю', 'ok');
      await refresh({ force: true });
    } catch (err) {
      if (btn) btn.disabled = false;
      ctx.toast(err?.message || 'Не удалось отправить напоминание', 'danger');
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
      ctx.toast(err?.message || 'Не удалось создать запрос', 'danger');
    }
  }

  /* ── вопросы: «Вопрос — ваш ответ», статус и реакция руководителя ────── */

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
      h('span', { class: 'chip ' + (isMaxim ? 'chip--info' : 'chip--outline'), title: isMaxim ? null : 'Вопрос профилю: ' + q.for },
        isMaxim ? 'тебе' : (personName(q.for) || String(q.for))));
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

  /** Поле ответа: как в composer — поле + отдельные «Надиктовать» и «Отправить». */
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
    const explain = h('button', {
      type: 'button', class: 'btn btn--ghost btn--sm req-q__explain', 'data-action': 'need-explanation',
      title: 'Попросить пояснить вопрос — вопрос останется открытым, пока вы не решите',
    }, 'Мне нужно пояснение');
    explain.addEventListener('click', () => answerQuestion(q, ta, explain, { needExplanation: true }));
    const isMaxim = (q.for || 'mikhail') === 'mikhail';
    return h('div', { class: 'req-q__form' },
      h('div', { class: 'req-q__row' }, ta, send),
      h('div', { class: 'req-q__actions' }, explain,
        h('span', { class: 'muted req-q__hint' }, waiting
          ? 'Вопрос остаётся открытым, пока руководитель не пояснит'
          : (isMaxim ? 'Руководитель комнаты продолжит после ответа' : 'Ответ уйдёт в задачу'))));
  }

  function questionRow(q) {
    const isMaxim = (q.for || 'mikhail') === 'mikhail';
    const state = qState(q);
    const answered = state === 'waiting_explanation' || Boolean(q.answer) || Boolean(q.answered);
    const row = h('div', {
      class: 'req-q req-q--' + state,
      'data-q': String(q.id || ''),
      'data-q-state': state,
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
  const qCard = { el: null, body: null, head: null };

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
    for (const id of Array.from(qRows.keys())) {
      if (!questions.some((q) => q && String(q.id) === id)) qRows.delete(id);
    }
  }

  function questionsCard(questions) {
    const open = questions.filter((q) => qOpen(q)).length;
    let card = qCard.el;
    if (!card) {
      qCard.body = h('div', { class: 'req-side__body req-q__list', 'data-question-list': '' });
      qCard.head = blockHead('Вопросы к тебе', 0);
      card = qCard.el = h('section', {
        class: 'card req-side',
        id: 'questions',
        'data-questions': '',
        'aria-label': 'Вопросы к тебе и ваши ответы',
      }, qCard.head);
      card.appendChild(qCard.body);
    }
    const head = blockHead('Вопросы к тебе', open ? countNoun(open, ['вопрос', 'вопроса', 'вопросов']) : null);
    if (qCard.head) {
      try { card.replaceChild(head, qCard.head); } catch { card.replaceChildren(head, qCard.body); }
      qCard.head = head;
    }
    reconcileQuestions(qCard.body, questions);
    return card;
  }

  function decisionRow(d) {
    const status = d.status || 'proposed';
    const chip = status === 'accepted'
      ? h('span', { class: 'chip chip--ok' }, 'принято')
      : status === 'rejected'
        ? h('span', { class: 'chip chip--muted' }, 'отклонено')
        : h('span', { class: 'chip chip--warn' }, 'предложено');
    return h('div', { class: 'req-decision' },
      h('div', { class: 'req-decision__top' },
        h('div', { class: 'req-decision__text' }, d.text || ''),
        chip),
      d.why ? h('div', { class: 'req-decision__why' }, d.why) : null);
  }

  function decisionsCard(decisions) {
    return h('section', { class: 'card req-side' },
      blockHead('Решения', countNoun(decisions.length, ['решение', 'решения', 'решений'])),
      h('div', { class: 'req-side__body' }, decisions.map(decisionRow)));
  }

  function futureCard(items) {
    const card = h('section', { class: 'card req-side' },
      blockHead('Будущие вопросы', countNoun(items.length, ['вопрос', 'вопроса', 'вопросов'])));
    const body = h('div', { class: 'req-side__body' });
    for (const text of items) {
      const btn = h('button', { type: 'button', class: 'btn btn--ghost btn--sm', title: 'Создать запрос из этого вопроса' }, 'Задать');
      btn.addEventListener('click', () => askFuture(String(text), btn));
      body.append(h('div', { class: 'req-future' },
        h('div', { class: 'req-future__text' }, String(text)),
        btn));
    }
    card.append(body);
    return card;
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

    return h('section', { class: 'card req-side' }, blockHead('Карта проекта'), link);
  }

  function buildAside() {
    const wrap = h('aside', { class: 'req__aside', 'aria-label': 'Дальше по запросу' });
    const steps = Array.isArray(R.detail.next_steps) ? R.detail.next_steps : [];
    const questions = Array.isArray(R.detail.open_questions) ? R.detail.open_questions : [];
    const decisions = Array.isArray(R.detail.decisions) ? R.detail.decisions : [];
    const future = Array.isArray(R.detail.future_questions) ? R.detail.future_questions : [];

    // Понятно, к чему относится колонка (WAVE8, Д3): только этот запрос + выход в комнату.
    const backlogHref = `#/room/${encodeURIComponent(R.roomId)}/backlog?request=${encodeURIComponent(R.detail.id)}`;
    wrap.append(h('div', { class: 'req-aside__scope row row--between row--wrap row--tight' },
      h('span', { class: 'req-aside__scope-title' }, 'По этому запросу'),
      h('a', { class: 'req-aside__scope-link', href: backlogHref, title: 'Все дела комнаты: шаги, вопросы, решения' },
        'Все дела комнаты →')));

    // Вопросы — первыми: это то, что ждёт Михаила (ответ и его статус должны быть
    // видны без прокрутки колонки), дальше — план руководителя.
    if (questions.length) wrap.append(questionsCard(questions));
    if (steps.length) wrap.append(stepsBlock.stepsCard(steps));
    if (decisions.length) wrap.append(decisionsCard(decisions));
    if (future.length) wrap.append(futureCard(future));
    if (R.detail.stage && Array.isArray(R.detail.stage.phases) && R.detail.stage.phases.length) wrap.append(stageCard(R.detail.stage));

    if (!wrap.childElementCount) {
      wrap.append(h('section', { class: 'card req-side' },
        h('div', { class: 'req-aside-empty' }, 'Пока ничего не ждём: команда закончит работу и добавит шаги, вопросы и решения.')));
    }
    return wrap;
  }

  return { buildAside };
}
