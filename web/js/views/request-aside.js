/**
 * request-aside.js — правая колонка «Дальше»: шаги, вопросы, решения,
 * будущие вопросы, фаза проекта; действия отметки шага, ответа и «Задать».
 *
 * Фабрика createAside(R) получает общий контекст страницы из request.js
 * и возвращает { buildAside }. Поведение прежнее: вынесено из request.js.
 */

import { h, timeAgo } from '../util.js';
import { icon } from '../icons.js';
import { avatar, ensureCss } from '../ui.js';
import { firstLine, personName, profileName, blockHead } from './request-util.js';
import { countNoun } from '../text-ru.js';
import { dueChipLabel } from './backlog-model.js';

const STEP_HINT = 'Отметьте, когда шаг выполнен — он перейдёт в «Выполнено» комнаты';
const STEP_HINT_DONE = 'Шаг выполнен — снимите галочку, чтобы вернуть его в работу';
const SAVE_FAILED = 'Не удалось сохранить отметку. Обновите страницу и попробуйте ещё раз';
const STEP_FORMS = ['шаг', 'шага', 'шагов'];

function has(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj || {}, key);
}

export function createAside(R) {
  const { ctx, rid, keepValues, refresh } = R;
  ensureCss('css/view-backlog-extras.css');

  /**
   * Отметка шага. Строка меняется сразу (оптимистично): шаг живёт в бэклоге
   * комнаты, а не в детали запроса, поэтому `refresh({force:true})` часто
   * возвращается без изменений и не перерисовывает колонку «Дальше» —
   * раньше чекбокс оставался выключенным до F5 (дефект e2e S11).
   * При ошибке отметка возвращается назад, чекбокс снова доступен.
   */
  async function toggleStep(item, cb, itemId, patch = {}, row = null, onPainted = null) {
    const next = has(patch, 'status') ? patch.status : (cb && cb.checked ? 'done' : 'open');
    const body = { status: next };
    if (has(patch, 'due')) body.due = patch.due;
    if (cb) cb.disabled = true;
    try {
      await ctx.api.post(
        `/api/rooms/${encodeURIComponent(R.roomId)}/backlog/${encodeURIComponent(itemId)}`,
        body,
      );
      // Локальная правда: деталь от сервера могла не измениться, а шаг уже done.
      item.state = next;
      if (has(body, 'due')) item.due = body.due || null;
      if (R.backlog) {
        const cur = R.backlog.get(itemId) || { id: itemId, source_request: R.detail && R.detail.id };
        R.backlog.set(itemId, { ...cur, status: next, due: body.due !== undefined ? body.due : cur.due });
      }
      if (cb) cb.disabled = false;
      repaintStep(row, item);
      if (typeof onPainted === 'function') onPainted();
      if (body.due !== undefined) ctx.toast(body.due ? 'Срок сохранён' : 'Срок убран', 'ok');
      else ctx.toast(next === 'done' ? 'Шаг выполнен — он в «Выполнено» комнаты' : 'Шаг снова открыт', 'ok');
      await refresh({ force: true });
    } catch (err) {
      if (cb) { cb.checked = !cb.checked; cb.disabled = false; }
      ctx.toast(body.due !== undefined ? 'Не удалось сохранить срок. Обновите страницу и попробуйте ещё раз' : SAVE_FAILED, 'danger');
    }
  }

  /** Заменить строку шага на перерисованную: галочка, «is-done», календарь. */
  function repaintStep(row, step) {
    if (!row || !row.parentNode) return;
    const fresh = stepRow(step);
    const parent = row.parentNode;
    if (typeof row.replaceWith === 'function') row.replaceWith(fresh);
    else if (typeof parent.replaceChild === 'function') parent.replaceChild(fresh, row);
  }


  async function answerQuestion(q, ta, btn) {
    const text = String(ta.value || '').trim();
    if (!text) { ta.focus(); return; }
    btn.disabled = true;
    try {
      await ctx.api.post(`/api/requests/${encodeURIComponent(rid)}/actions`, {
        action: 'answer', question_id: q.id, text,
      });
      keepValues.delete('q:' + q.id);
      ctx.toast('Ответ отправлен — команда продолжит', 'ok');
      await refresh({ force: true });
    } catch (err) {
      btn.disabled = false;
      ctx.toast(err?.message || 'Не удалось отправить ответ', 'danger');
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

  /* ── колонка «Дальше» ──────────────────────────────────────── */

  /** Аватар-буква: «MA/BO/BD» из системных id не показываем. */
  function ownerAvatar(raw, name) {
    const el = avatar(name || raw, { size: 'sm' });
    el.textContent = (Array.from(String(name || raw))[0] || '?').toUpperCase();
    el.title = raw;
    return el;
  }

  function ownerChip(owner) {
    if (!owner) return null;
    const raw = String(owner);
    const name = profileName(raw, R.ctx && R.ctx.room) || raw;
    return h('span', { class: 'req-owner', title: 'Исполнитель: ' + raw },
      ownerAvatar(raw, name),
      h('span', { class: 'req-owner__name' }, name));
  }

  function stepRow(step) {
    const itemId = `${R.detail.id}:${step.id}`;
    const roomItem = R.backlog ? R.backlog.get(itemId) : null;
    const done = step.state === 'done' || roomItem?.status === 'done';
    const due = roomItem?.due || step.due || null;
    const cb = h('input', {
      type: 'checkbox', class: 'req-step__check', checked: done,
      title: done ? STEP_HINT_DONE : STEP_HINT,
      'aria-label': (done ? 'Шаг выполнен — вернуть в работу: ' : 'Шаг выполнен: ') + firstLine(step.text, 80),
    });
    const row = h('div', { class: 'req-step' + (done ? ' is-done' : '') });
    cb.addEventListener('change', () => toggleStep(step, cb, itemId, {}, row, () => {
      const host = row.closest ? row.closest('.req-side') : null;
      updateStepsCount(host);
    }));

    const meta = h('div', { class: 'req-step__meta' });
    const owner = ownerChip(step.owner);
    if (owner) meta.append(owner);
    // Срок — чип: клик открывает выбор даты (поле даты лежит под чипом).
    const date = h('input', {
      class: 'input due-chip__input req-step__date', type: 'date', value: due || '',
      'aria-label': 'Срок шага: ' + firstLine(step.text, 80),
      title: due ? 'Срок: ' + due + ' — нажмите, чтобы изменить' : 'Срок не задан — нажмите, чтобы выбрать',
    });
    date.addEventListener('change', () => toggleStep(step, null, itemId, {
      status: done ? 'done' : 'open', due: date.value || null,
    }, row, () => {
      const host = row.closest ? row.closest('.req-side') : null;
      updateStepsCount(host);
    }));
    meta.append(h('label', {
      class: 'due-chip' + (due ? '' : ' due-chip--empty'),
      title: due ? 'Срок: ' + due + ' — нажмите, чтобы изменить' : 'Срок не задан — нажмите, чтобы выбрать',
    }, icon('calendar', { size: 11 }),
    h('span', { class: 'due-chip__text' }, due ? dueChipLabel(due, new Date()) : 'Срок'), date));
    if (!done) {
      meta.append(h('a', {
        class: 'btn btn--ghost btn--sm req-step__ics',
        href: `/api/ics?room=${encodeURIComponent(R.roomId)}&items=${encodeURIComponent(itemId)}`,
        download: '', title: 'Добавить шаг в календарь (.ics)',
        'aria-label': 'Добавить шаг в календарь (.ics)',
      }, icon('calendar', { size: 12 }), h('span', { class: 'req-step__ics-text' }, 'в календарь (.ics)')));
    }

    row.append(
      cb,
      h('div', { class: 'req-step__body' },
        h('div', { class: 'req-step__text' }, step.text || ''),
        meta.childElementCount ? meta : null));
    return row;
  }

  /** Счётчик открытых шагов в шапке карточки — после отметки шага он меняется. */
  function updateStepsCount(card) {
    if (!card || typeof card.querySelector !== 'function') return;
    const chip = card.querySelector('.section-header__count');
    if (!chip) return;
    const steps = Array.isArray(R.detail && R.detail.next_steps) ? R.detail.next_steps : [];
    const open = steps.filter((s) => {
      const itemId = `${R.detail.id}:${s.id}`;
      const roomItem = R.backlog ? R.backlog.get(itemId) : null;
      return s.state !== 'done' && roomItem?.status !== 'done';
    }).length;
    chip.textContent = countNoun(open || steps.length, STEP_FORMS);
  }

  function stepsCard(steps) {
    const open = steps.filter((s) => {
      const itemId = `${R.detail.id}:${s.id}`;
      const roomItem = R.backlog ? R.backlog.get(itemId) : null;
      return s.state !== 'done' && roomItem?.status !== 'done';
    }).length;
    const count = countNoun(open || steps.length, STEP_FORMS);
    const card = h('section', { class: 'card req-side' }, blockHead('Следующие шаги', count));
    card.append(h('div', { class: 'req-side__hint small muted' }, STEP_HINT));
    const body = h('div', { class: 'req-side__body' }, steps.map(stepRow));
    card.append(body);
    const dueIds = steps
      .filter((s) => s.due && s.state !== 'done')
      .map((s) => `${R.detail.id}:${s.id}`);
    if (dueIds.length) {
      card.append(h('div', { class: 'req-side__foot' },
        h('a', {
          class: 'btn btn--ghost btn--sm',
          href: `/api/ics?room=${encodeURIComponent(R.roomId)}&items=${dueIds.map(encodeURIComponent).join(',')}`,
          download: '',
        }, icon('calendar', { size: 12 }), 'Все в календарь')));
    }
    return card;
  }

  function questionRow(q) {
    const isMaxim = (q.for || 'mikhail') === 'mikhail';
    const top = h('div', { class: 'req-q__top' },
      h('div', { class: 'req-q__text' }, q.text || ''),
      h('span', { class: 'chip ' + (isMaxim ? 'chip--info' : 'chip--outline'), title: isMaxim ? null : 'Вопрос профилю: ' + q.for },
        isMaxim ? 'тебе' : (personName(q.for) || String(q.for))));

    if (q.answered) {
      return h('div', { class: 'req-q' }, top,
        h('div', { class: 'req-q__answered' },
          icon('check', { size: 12 }),
          h('div', { class: 'grow' },
            h('div', null, q.answer || 'Ответ получен.'),
            h('div', { class: 'muted', style: { marginTop: '2px' } },
              'отвечено' + (q.answered_at ? ' ' + timeAgo(q.answered_at) : '')))));
    }

    const key = 'q:' + q.id;
    const ta = h('textarea', {
      class: 'textarea req-q__input', rows: 2, 'data-keep': key,
      placeholder: 'Ответить…', 'aria-label': 'Ответ на вопрос: ' + firstLine(q.text, 60),
      value: keepValues.get(key) || '',
    });
    ta.addEventListener('input', () => keepValues.set(key, ta.value));
    const btn = h('button', { type: 'button', class: 'btn btn--primary btn--sm', onclick: () => answerQuestion(q, ta, btn) }, 'Ответить');

    return h('div', { class: 'req-q' }, top,
      h('div', { class: 'req-q__form' }, ta,
        h('div', { class: 'req-q__actions' }, btn,
          h('span', { class: 'muted', style: { fontSize: 'var(--text-2xs)' } },
            isMaxim ? 'Руководитель комнаты продолжит после ответа' : 'Ответ уйдёт в задачу'))));
  }

  function questionsCard(questions) {
    const open = questions.filter((q) => !q.answered).length;
    const card = h('section', {
      class: 'card req-side',
      id: 'questions',
      'data-questions': '',
      'aria-label': 'Вопросы к тебе',
    }, blockHead('Вопросы к тебе', open ? countNoun(open, ['вопрос', 'вопроса', 'вопросов']) : null));
    card.append(h('div', { class: 'req-side__body' }, questions.map(questionRow)));
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

    if (steps.length) wrap.append(stepsCard(steps));
    if (questions.length) wrap.append(questionsCard(questions));
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
